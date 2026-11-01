"""The voice listener: microphone -> wake word or push-to-talk -> command -> text.

Runs on its own thread. Privacy: while waiting, audio stays on the machine (the
local VAD and wake spotter only). Gnani receives audio only for a command, i.e.
after "Hey Zach" or a push-to-talk press, and never more than one utterance.

Which STT is used for a command:
- it starts live (push-to-talk, or "Hey Zach" then a pause) and streaming is on:
  Gnani streaming, so the text is ready right after you stop talking;
- "Hey Zach, <command>" in one breath (audio already captured): Gnani REST;
- Gnani fails (network down, credits used up, rate limited...): local whisper.cpp
  on the same audio, and the reason is reported. Gnani calls are never retried.
"""

from __future__ import annotations

import collections
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from zachary.telemetry.latency import Timeline
from zachary.voice.audio import FRAME_BYTES, EnergyVAD, MicStream, seconds
from zachary.voice.types import SAMPLE_RATE, STT, StreamingSTT, Transcript, VoiceError
from zachary.voice.wakeword import SPOT_SECONDS, WakeSpotter

log = logging.getLogger(__name__)

PREROLL_S = 0.4  # audio kept from before speech starts, so the first word isn't clipped
COMMAND_WAIT_S = 5.0  # after a bare "Hey Zach", how long to wait for the command
MAX_COMMAND_S = 12.0
MIN_COMMAND_S = 0.35
SILENT_FRAMES = int(3.0 * SAMPLE_RATE / 512)  # 3 s of exact zeros


@dataclass(slots=True)
class Heard:
    text: str
    transcript: Transcript | None
    timeline: Timeline
    error: VoiceError | None = None
    audio_s: float = 0.0
    via: str = ""  # wake | ptt
    extra: dict[str, object] = field(default_factory=dict)


class FrameSource(Protocol):
    """Anything that yields 1,024-byte frames (the mic, or a file in tests)."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def get(self, timeout: float) -> bytes | None: ...


class MicSource:
    def __init__(self, mic: MicStream | None = None, device: str = "") -> None:
        self.mic = mic or MicStream(device)
        self.auto = mic is None and not device  # free to try other mics
        self._tried: set[str] = set()

    def try_next(self) -> str | None:
        """The current mic gives digital silence (e.g. a laptop mic with no Linux
        driver): switch to the next real microphone. Returns its name, or None."""
        if not self.auto:
            return None
        from zachary.voice.audio import list_inputs

        self._tried.add(self.mic.source_name or self.mic.device or "<default>")
        try:
            current_default = [d.id for d in list_inputs() if d.is_default]
            self._tried.update(current_default if not self.mic.source_name else [])
            options = [d for d in list_inputs() if d.id not in self._tried]
        except Exception:
            return None
        # Prefer a headset mic (what the user is wearing), then anything else.
        options.sort(key=lambda d: not d.id.startswith("bluez_input"))
        for d in options:
            self._tried.add(d.id)
            self.mic.stop()
            self.mic.drain()
            self.mic = MicStream(d.id)
            try:
                self.mic.start()
            except Exception:
                continue
            return d.label
        return None

    def start(self) -> None:
        self.mic.start()

    def stop(self) -> None:
        self.mic.stop()

    def get(self, timeout: float) -> bytes | None:
        try:
            return self.mic.frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> None:
        """Drop audio that piled up while we were busy (it's already in the past)."""
        self.mic.drain()


class VoiceListener(threading.Thread):
    def __init__(
        self,
        source: FrameSource,
        *,
        on_heard: Callable[[Heard], None],
        on_state: Callable[[str], None] = lambda _s: None,
        rest_stt: STT | None,
        stream_stt: Callable[[], StreamingSTT] | None,
        offline_stt: STT | None,
        spotter: WakeSpotter | None,
        wake_word: bool = True,
        vad: EnergyVAD | None = None,
    ) -> None:
        super().__init__(name="zach-voice", daemon=True)
        self.source = source
        self.on_heard = on_heard
        self.on_state = on_state
        self.rest_stt = rest_stt
        self.stream_stt = stream_stt
        self.offline_stt = offline_stt
        self.spotter = spotter
        self.wake_word = wake_word and spotter is not None
        self.vad = vad or EnergyVAD()
        self._stop_ev = threading.Event()
        self._ptt = threading.Event()
        self._zero_frames = 0  # digital silence in a row: wrong input device or muted
        self._warned_silent = False

    # -- control (any thread) -------------------------------------------------------
    def stop(self) -> None:
        self._stop_ev.set()

    def push_to_talk(self) -> None:
        """Start a command now (no wake word needed)."""
        self._ptt.set()

    # -- thread ------------------------------------------------------------------------
    def run(self) -> None:
        try:
            self.source.start()
        except Exception as e:
            self.on_state(f"error: microphone unavailable ({e})")
            return
        try:
            self._loop()
        finally:
            self.source.stop()

    def _frame(self, timeout: float = 0.1) -> bytes | None:
        f = self.source.get(timeout)
        if f is not None:
            self._check_silent(f)
        return f

    def _check_silent(self, frame: bytes) -> None:
        """Real microphones always have some noise; exact zeros for 3 s mean the input
        is a monitor/virtual device or muted. Say so once (until sound comes back)."""
        if frame.count(0) != len(frame):
            self._zero_frames = 0
            if self._warned_silent:
                self._warned_silent = False
                self.on_state("idle")
            return
        self._zero_frames += 1
        if not self._warned_silent and self._zero_frames >= SILENT_FRAMES:
            self._zero_frames = 0
            try_next = getattr(self.source, "try_next", None)
            switched = try_next() if try_next is not None else None
            if switched:
                log.info("microphone was silent; switched to %s", switched)
                self.on_state(f"info: using microphone {switched}")
                return
            self._warned_silent = True
            self.on_state(
                "error: the microphone is silent (check the input device in Sound settings)"
            )

    def _loop(self) -> None:
        preroll: collections.deque[bytes] = collections.deque(
            maxlen=max(1, int(PREROLL_S * SAMPLE_RATE * 2 / FRAME_BYTES))
        )
        self.on_state("idle")
        while not self._stop_ev.is_set():
            if self._ptt.is_set():
                self._ptt.clear()
                self._drain()  # only what's said after the press counts
                self._command(Timeline("voice"), via="ptt", buffered=b"", live=True)
                preroll.clear()
                self.vad.reset()
                continue
            frame = self._frame()
            if frame is None:
                continue
            ev = self.vad.push(frame)
            if not self.wake_word:
                preroll.append(frame)
                continue
            if ev is not None and ev.kind == "start":
                self._utterance(b"".join(preroll) + frame)
                preroll.clear()
                self.vad.reset()
            else:
                preroll.append(frame)

    def _utterance(self, head: bytes) -> None:
        """Speech started: collect ~1.6 s, check locally for "Hey Zach"."""
        assert self.spotter is not None
        tl = Timeline("voice")
        buf = bytearray(head)
        need = int(SPOT_SECONDS * SAMPLE_RATE * 2)
        ended = False
        while len(buf) < need and not self._stop_ev.is_set():
            f = self._frame(0.5)
            if f is None:
                continue
            buf += f
            ev = self.vad.push(f)
            if ev is not None and ev.kind == "end":
                ended = True
                break
        wake = self.spotter.check(bytes(buf))
        if not wake.found:
            self._drain()  # audio queued during the check: not for us either
            if not ended:
                self._skip_until_silence()  # someone talking, not to us (cheap: VAD only)
            return
        tl.mark("wake")
        tl.info["wake_heard"] = wake.heard
        rest = bytes(buf[int(wake.cut_s * SAMPLE_RATE) * 2 :])
        if ended and seconds(rest) < MIN_COMMAND_S:
            # Just "Hey Zach": listen for the command as a fresh utterance.
            self._command(tl, via="wake", buffered=b"", live=True)
        else:
            self._command(tl, via="wake", buffered=rest, live=False, already_ended=ended)

    def _mic_name(self) -> str:
        mic = getattr(self.source, "mic", None)
        return str(
            getattr(mic, "source_name", "") or getattr(mic, "device", "") or "system default"
        )

    def _drain(self) -> None:
        drain = getattr(self.source, "drain", None)
        if drain is not None:
            drain()
        self.vad.reset()

    def _skip_until_silence(self) -> None:
        deadline = time.monotonic() + 30
        while not self._stop_ev.is_set() and time.monotonic() < deadline:
            f = self._frame(0.5)
            if f is None:
                continue
            ev = self.vad.push(f)
            if ev is not None and ev.kind == "end":
                return

    def _command(
        self,
        tl: Timeline,
        *,
        via: str,
        buffered: bytes,
        live: bool,
        already_ended: bool = False,
    ) -> None:
        self.on_state("listening")
        audio = bytearray(buffered)
        stream: StreamingSTT | None = None
        stream_error: VoiceError | None = None
        if live and self.stream_stt is not None:
            try:
                stream = self.stream_stt()
                stream.open()
            except VoiceError as e:
                stream, stream_error = None, e
        # The user pressed the key / said "Hey Zach": they're about to talk, so listen
        # more sensitively than while idle (Bluetooth headset mics are quiet).
        vad = EnergyVAD(ratio=2.0, min_rms=0.003, end_silence_ms=self.vad.end_ms)
        vad.noise = min(self.vad.noise, 0.0015)
        peak = 0.0
        started = bool(buffered)
        start_deadline = time.monotonic() + COMMAND_WAIT_S
        max_bytes = int(MAX_COMMAND_S * SAMPLE_RATE * 2)
        ended = already_ended
        while not ended and not self._stop_ev.is_set():
            if self._ptt.is_set():  # pressed again: stop listening now
                self._ptt.clear()
                break
            f = self._frame(0.2)
            if f is None:
                if not started and time.monotonic() > start_deadline:
                    break
                continue
            ev = vad.push(f)
            peak = max(peak, vad.last_rms)
            if ev is not None and ev.kind == "start":
                started = True
            if not started and not vad.in_speech:
                if time.monotonic() > start_deadline:
                    break
                continue
            audio += f
            if stream is not None:
                try:
                    stream.feed(f)
                except Exception as e:
                    stream_error = VoiceError("network", f"stream broke: {type(e).__name__}")
                    stream.abort()
                    stream = None
            if ev is not None and ev.kind == "end":
                ended = True
            if len(audio) >= max_bytes:
                break
        tl.mark("speech_end")
        pcm = bytes(audio)
        mic = self._mic_name()
        tl.info.update({"mic": mic, "mic_peak": round(peak, 4)})
        if not started:
            if stream is not None:
                stream.abort()
            tl.mark("stt")
            hint = (
                "no sound at all: pick another input with `cli voice mics`"
                if peak == 0
                else "too quiet: speak closer, or raise the input volume"
            )
            msg = f"I didn't hear anything (mic: {mic}, level {peak:.3f}; {hint})"
            heard = Heard("", None, tl, VoiceError("bad_request", msg))
            heard.via = via
            self.on_heard(heard)
            self.vad.reset()
            self.on_state("idle")
            return
        self.on_state("thinking")
        heard = self._transcribe(pcm, stream, stream_error, tl)
        heard.via = via
        heard.audio_s = round(seconds(pcm), 2)
        self.on_heard(heard)
        self.vad.reset()
        self.on_state("idle")

    def _transcribe(
        self,
        pcm: bytes,
        stream: StreamingSTT | None,
        stream_error: VoiceError | None,
        tl: Timeline,
    ) -> Heard:
        if seconds(pcm) < MIN_COMMAND_S:
            if stream is not None:
                stream.abort()
            tl.mark("stt")
            return Heard("", None, tl, VoiceError("bad_request", "I didn't hear a command"))
        err: VoiceError | None = stream_error
        if stream is not None:
            try:
                t = stream.finish()
                tl.mark("stt")
                return Heard(t.text, t, tl)
            except VoiceError as e:
                err = e
        if self.rest_stt is not None and (err is None or err.offline):
            try:
                t = self.rest_stt.transcribe(pcm)
                tl.mark("stt")
                return Heard(t.text, t, tl)
            except VoiceError as e:
                err = e
        # Any online failure (network, credits used up, rate limit...) -> local whisper:
        # free, private, and the user still gets an answer (the reason is reported).
        if self.offline_stt is not None:
            t = self.offline_stt.transcribe(pcm)
            tl.mark("stt")
            t.extra["fallback_reason"] = err.kind if err else "no online STT"
            return Heard(t.text, t, tl, extra={"offline": True})
        tl.mark("stt")
        return Heard("", None, tl, err or VoiceError("config", "no speech recognizer"))

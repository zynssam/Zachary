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
import contextlib
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
NO_SPEECH_S = 6.0  # push-to-talk: give up if nothing is said within this time
SEGMENT_GRACE_S = 0.12  # after a transcript, wait this long for more speech
PREROLL_WARM_S = 0.6  # audio kept from just before the press (covers the long-press wait)
MAX_HOLD_S = 28.0  # hold-to-talk cap (REST, the fallback, takes up to 30 s)
LOCAL_END_MS = 350  # quiet that ends a command locally (then commit immediately)
COMMIT_WAIT_S = 3.0  # after commit, wait at most this long for the transcript
SERVER_LAG_S = 1.0  # local VAD heard speech end but the server never started: commit
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
    """The microphone for voice commands.

    With no ``device`` setting it's automatic and re-decided at every command, so it
    follows what you're using: plug in wired earphones, connect Bluetooth earbuds,
    or unplug them, and the next command uses the right mic (``audio.auto_input``).
    A mic that turns out dead (digital silence) is remembered and skipped. A mic
    chosen by hand that has been unplugged falls back to automatic.
    """

    def __init__(self, mic: MicStream | None = None, device: str = "") -> None:
        self.device = device
        self.mic = mic or MicStream(device)
        self.auto = mic is None and not device
        self.fixed = mic is not None  # a stream given by the caller: never replaced
        self.dead: set[str] = set()  # source names that gave only digital silence

    def _auto_stream(self) -> MicStream:
        from zachary.voice.audio import auto_input

        if self.mic.active:
            return self.mic
        return MicStream(auto_input(exclude=self.dead))

    def start(self) -> None:
        if self.auto:
            self.mic = self._auto_stream()
            self.mic.start()
            if self.mic.source_name:
                log.info("voice: using microphone %s", self.mic.source_name)
            return
        try:
            self.mic.start()
        except Exception as e:
            if self.fixed:
                raise
            log.warning("voice: microphone %r unavailable (%s); choosing automatically",
                        self.device, e)  # fmt: skip
            self.mic = self._auto_stream()
            self.mic.start()

    def try_next(self) -> str | None:
        """The current mic gives digital silence (e.g. a laptop mic with no Linux
        driver): remember it as dead and switch to the next real microphone."""
        if self.fixed:
            return None
        from zachary.voice.audio import auto_input

        current = self.mic.source_name or self.mic.device
        if current:
            self.dead.add(current)
        try:
            name = auto_input(exclude=self.dead)
        except Exception:
            return None
        if not name or name == current:
            return None
        self.mic.stop()
        self.mic.drain()
        self.mic = MicStream(name)
        self.auto = True
        try:
            self.mic.start()
        except Exception:
            self.dead.add(name)
            return None
        return name

    def stop(self) -> None:
        self.mic.stop()

    def keep_last(self, n: int) -> None:
        """Drop all but the newest ``n`` queued frames."""
        q = self.mic.frames
        while q.qsize() > n:
            try:
                q.get_nowait()
            except queue.Empty:
                return

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
        mic_always_open: bool | None = None,
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
        # Push-to-talk only: the mic is open only during a command (privacy; and
        # Bluetooth earbuds stay in high-quality music mode the rest of the time).
        self.mic_always_open = self.wake_word if mic_always_open is None else mic_always_open
        self.vad = vad or EnergyVAD()
        self._stop_ev = threading.Event()
        self._ptt = threading.Event()
        self._hold = False
        self._released = threading.Event()
        self._warm = threading.Event()  # bar is open: keep the mic running locally
        self._warmed = False
        self._zero_frames = 0  # digital silence in a row: wrong input device or muted
        self._warned_silent = False

    # -- control (any thread) -------------------------------------------------------
    def stop(self) -> None:
        self._stop_ev.set()

    def push_to_talk(self, hold: bool = False) -> None:
        """Start a command now (no wake word needed). ``hold``: record until
        ``release()`` (hold-to-talk), however long the pauses."""
        self._hold = hold
        self._released.clear()
        self._ptt.set()

    def set_warm(self, on: bool) -> None:
        """While the quick bar is open, keep the mic open and remember the last
        PREROLL_WARM_S of audio *locally* (discarded continuously, never sent), so
        the first word said right as the key goes down isn't cut off. Nothing goes to
        Gnani until the key is pressed. Closed again when the bar hides."""
        if on:
            self._warm.set()
        else:
            self._warm.clear()

    def release(self) -> None:
        """Hold-to-talk: the key was let go, so send what was said."""
        self._released.set()

    # -- thread ------------------------------------------------------------------------
    def run(self) -> None:
        if self.mic_always_open:
            try:
                self.source.start()
            except Exception as e:
                self.on_state(f"error: microphone unavailable ({e})")
                return
        try:
            self._loop()
        finally:
            with contextlib.suppress(Exception):
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
                try:
                    self._ptt_session()
                except Exception as e:  # never let one bad command kill voice
                    log.exception("voice command failed")
                    self.on_state(f"error: voice command failed ({type(e).__name__}: {e})")
                preroll.clear()
                self.vad.reset()
                self.on_state("idle")
                continue
            if not self.mic_always_open:
                self._idle_warm()
                continue
            frame = self._frame()
            if frame is None:
                continue
            ev = self.vad.push(frame)
            if not self.wake_word:
                preroll.append(frame)
                continue
            if ev is not None and ev.kind == "start":
                try:
                    self._utterance(b"".join(preroll) + frame)
                except Exception as e:
                    log.exception("voice command failed")
                    self.on_state(f"error: voice command failed ({type(e).__name__}: {e})")
                    self.on_state("idle")
                preroll.clear()
                self.vad.reset()
            else:
                preroll.append(frame)

    # -- push-to-talk: Gnani streaming with the server's VAD --------------------------------
    def _ptt_session(self) -> None:
        tl = Timeline("voice")
        self.on_state("listening")
        opened_here = False
        try:
            if self.mic_always_open:
                self._drain()  # only what's said after the press counts
            elif self._warmed:
                # Already open (bar showing): keep the last moment before the press,
                # the start of what you said.
                self._keep_last(PREROLL_WARM_S)
                tl.info["preroll_s"] = PREROLL_WARM_S
            else:
                self.source.start()
                opened_here = True
        except Exception as e:
            log.warning("voice: microphone unavailable: %s", e)
            tl.info["error_detail"] = f"{type(e).__name__}: {e}"
            heard = Heard("", None, tl, VoiceError("config", f"microphone unavailable ({e})"))
            heard.via = "ptt"
            tl.mark("stt")
            self.on_heard(heard)
            return
        tl.mark("mic_open")
        log.info("voice: mic open (%s)", self._mic_name())
        try:
            heard = self._ptt_listen(tl)
        finally:
            if opened_here:
                with contextlib.suppress(Exception):
                    self.source.stop()
        heard.via = "ptt"
        self.on_state("thinking" if heard.text else "idle")
        self.on_heard(heard)

    def _ptt_listen(self, tl: Timeline) -> Heard:
        """Stream every frame from the press; Gnani's VAD says when speech starts and
        ends. Local energy VAD is only used for the level and if streaming fails."""
        box: dict[str, object] = {}
        box_lock = threading.Lock()

        def opener() -> None:
            try:
                assert self.stream_stt is not None
                st = self.stream_stt()
                st.open()
            except VoiceError as e:
                box["err"] = e
                return
            except Exception as e:
                box["err"] = VoiceError("network", f"stream failed: {type(e).__name__}")
                return
            with box_lock:
                if box.get("cancel"):
                    st.close()  # the command ended before we connected
                else:
                    box["stream"] = st

        connecting: threading.Thread | None = None
        if self.stream_stt is not None:
            connecting = threading.Thread(target=opener, name="zach-stt-open", daemon=True)
            connecting.start()
        stream: StreamingSTT | None = None
        stream_err: VoiceError | None = None
        stream_done = connecting is None  # finished connecting (or no streaming at all)
        backlog: collections.deque[bytes] = collections.deque()
        audio = bytearray()
        # Local end-of-speech after LOCAL_END_MS of quiet: we then commit at once instead
        # of waiting for Gnani's own silence timer (saves ~0.3-0.5 s per command).
        vad = EnergyVAD(ratio=2.0, min_rms=0.003, end_silence_ms=LOCAL_END_MS)
        vad.noise = min(self.vad.noise, 0.0015)
        peak = 0.0
        local_started = local_ended = False
        local_end_at = 0.0
        server_started = False
        segments_open = 0  # speech_start events not yet answered by a transcript
        texts: list[str] = []
        lang: str | None = None
        last_text_at: float | None = None
        commit_at: float | None = None
        gnani_ms: list[object] = []
        t0 = time.monotonic()

        def commit(now: float) -> None:
            nonlocal stream, stream_err, commit_at
            if stream is None or commit_at is not None:
                return
            commit_at = now
            try:
                while backlog:  # audio still queued from the connection catch-up
                    stream.feed(backlog.popleft())
                stream.commit()
            except Exception as e:
                stream_err = VoiceError("network", f"stream broke: {type(e).__name__}")
                stream.close()
                stream = None

        hold = self._hold
        released = False
        while not self._stop_ev.is_set():
            now = time.monotonic()
            if self._ptt.is_set() and not hold:  # pressed again: that's the end
                self._ptt.clear()
                if stream is not None:
                    commit(now)
                else:
                    break
            if hold and not released and self._released.is_set():
                released = True  # key let go: send what was said
                local_ended, local_end_at = True, now
                if stream is not None:
                    commit(now)
                    tl.info["ended_by"] = "release"
                elif stream_done or connecting is None:
                    break
            if not stream_done and connecting is not None and not connecting.is_alive():
                stream_done = True
                stream = box.get("stream")  # type: ignore[assignment]
                stream_err = box.get("err")  # type: ignore[assignment]
                if stream is not None:
                    tl.mark("stream_open")
                    log.info("voice: Gnani stream open")
                elif stream_err is not None:
                    log.warning("voice: Gnani stream unavailable: %s", stream_err)
            f = self._frame(0.03)
            if f is not None:
                audio += f
                ev = vad.push(f)
                peak = max(peak, vad.last_rms)
                if ev is not None and ev.kind == "start":
                    local_started = True
                elif ev is not None and ev.kind == "end" and local_started:
                    local_ended, local_end_at = True, now
                backlog.append(f)
            if stream is not None:
                try:  # catch up on audio captured while connecting, at 2x real time
                    for _ in range(2):
                        if not backlog:
                            break
                        stream.feed(backlog.popleft())
                except Exception as e:
                    stream_err = VoiceError("network", f"stream broke: {type(e).__name__}")
                    stream.close()
                    stream = None
            elif stream_done:
                backlog.clear()
            if stream is not None:
                while True:
                    try:
                        msg = stream.events.get_nowait()
                    except queue.Empty:
                        break
                    kind = msg.get("type")
                    if kind == "speech_start":
                        if not server_started:
                            tl.mark("speech_start")
                        server_started = True
                        segments_open += 1  # more speech: wait for its transcript too
                        if commit_at is not None:  # you kept talking after a pause
                            commit_at = None
                            local_ended = False
                    elif kind == "processing":
                        if "speech_end" not in tl.stages():
                            tl.mark("speech_end")
                    elif kind == "transcript":
                        segments_open = max(0, segments_open - 1)
                        if local_ended:  # you stopped talking LOCAL_END_MS before local_end_at
                            tl.info["text_after_speech_ms"] = round(
                                (now - local_end_at) * 1e3 + LOCAL_END_MS
                            )
                        if msg.get("text"):
                            texts.append(str(msg["text"]).strip())
                            lang = msg.get("detected_language_code") or lang
                            gnani_ms.append(msg.get("latency"))
                        last_text_at = now
                    elif kind == "error":
                        stream_err = VoiceError("server", f"Gnani: {msg.get('message', '')}")
                    elif kind == "closed":
                        if not texts:
                            stream_err = stream_err or VoiceError("network", "stream closed")
                        stream.close()
                        stream = None
                        break
                if (
                    texts
                    and (released or not hold)  # holding: pauses don't end the command
                    and segments_open == 0
                    and last_text_at is not None
                    and now - last_text_at >= SEGMENT_GRACE_S
                ):
                    break  # done: every segment is transcribed and no new speech started
                if commit_at is not None and now - commit_at > COMMIT_WAIT_S:
                    break
                if local_ended and commit_at is None and not texts and not hold:
                    if server_started:
                        commit(now)  # you stopped talking: finalize now, don't wait
                        tl.info["ended_by"] = "local"
                    elif now - local_end_at > SERVER_LAG_S:
                        commit(now)  # we heard speech the server's VAD didn't: force it
            elif (
                stream_done
                and stream_err is not None
                and not texts
                and local_ended
                and (released or not hold)
            ):
                break  # streaming failed: local endpointing, then REST
            elif (
                stream_done
                and stream_err is None
                and connecting is None
                and local_ended
                and (released or not hold)
            ):
                break  # no streaming configured
            if not (server_started or local_started) and (
                released or (not hold and now - t0 > NO_SPEECH_S)
            ):
                break  # nothing was said
            if now - t0 > (MAX_HOLD_S if hold else MAX_COMMAND_S):
                if stream is not None and commit_at is None:
                    commit(now)
                elif stream is None or (commit_at is not None and now - commit_at > COMMIT_WAIT_S):
                    break
        with box_lock:
            box["cancel"] = True
        if stream is not None:
            stream.close()
        if "speech_end" not in tl.stages():
            tl.mark("speech_end")
        mic = self._mic_name()
        tl.info.update({"mic": mic, "mic_peak": round(peak, 4)})
        if gnani_ms:
            tl.info["gnani_latency_ms"] = gnani_ms
        if texts:
            tl.mark("stt")
            text = " ".join(texts)
            return Heard(text, Transcript(text, "gnani-stream", lang), tl)
        if not (server_started or local_started):
            tl.mark("stt")
            hint = (
                "no sound at all: pick another input with `cli voice mics`"
                if peak == 0
                else "too quiet: speak closer, or raise the input volume"
            )
            why = f"I didn't hear anything (mic: {mic}, level {peak:.3f}; {hint})"
            return Heard("", None, tl, VoiceError("bad_request", why))
        pcm = bytes(audio)[-int(29 * SAMPLE_RATE * 2) :]
        return self._transcribe(pcm, None, stream_err, tl)

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

    def _idle_warm(self) -> None:
        """Push-to-talk idle: mic closed, or open (bar showing) with a short local buffer."""
        if self._warm.is_set():
            if not self._warmed:
                try:
                    self.source.start()
                    self._warmed = True
                except Exception as e:
                    log.info("voice: can't pre-open the microphone (%s)", e)
                    self._warm.clear()
            else:
                self._keep_last(PREROLL_WARM_S)
            self._ptt.wait(0.1)
            return
        if self._warmed:
            self._warmed = False
            with contextlib.suppress(Exception):
                self.source.stop()
        self._ptt.wait(0.2)

    def _keep_last(self, seconds: float) -> None:
        trim = getattr(self.source, "keep_last", None)
        if trim is not None:
            trim(max(1, int(seconds * SAMPLE_RATE * 2 / FRAME_BYTES)))

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

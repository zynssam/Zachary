"""Microphone capture, playback, WAV helpers, and a simple voice-activity detector.

Capture is 16 kHz mono PCM16 in 512-sample frames (32 ms, 1,024 bytes): exactly
the frame Gnani's streaming STT wants. sounddevice (PortAudio) is imported
lazily so the rest of Zachary works on machines without audio.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import wave
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np

from zachary.voice.types import SAMPLE_RATE

log = logging.getLogger(__name__)

FRAME_SAMPLES = 512  # 32 ms at 16 kHz
FRAME_BYTES = FRAME_SAMPLES * 2


# -- WAV / PCM helpers ----------------------------------------------------------------
def pcm_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm(data: bytes) -> tuple[bytes, int]:
    """(PCM16 mono, sample rate) from WAV bytes; stereo is mixed down."""
    with wave.open(io.BytesIO(data), "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"unsupported sample width {width}")
    x = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return x.tobytes(), sr


def resample(pcm: bytes, src: int, dst: int = SAMPLE_RATE) -> bytes:
    if src == dst or not pcm:
        return pcm
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = max(1, int(len(x) * dst / src))
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def frames(pcm: bytes, size: int = FRAME_BYTES) -> Iterable[bytes]:
    """Split PCM into fixed-size frames, padding the last one with silence."""
    for i in range(0, len(pcm), size):
        chunk = pcm[i : i + size]
        yield chunk + b"\0" * (size - len(chunk))


def seconds(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> float:
    return len(pcm) / 2 / sample_rate


# -- voice activity detection -----------------------------------------------------------
@dataclass(slots=True)
class VadEvent:
    kind: str  # "start" | "end"
    frame_index: int


class EnergyVAD:
    """Adaptive energy VAD: speech when a frame is well above the noise floor.

    Cheap enough to run on every mic frame. ``start_frames`` loud frames in a row
    start speech; ``end_silence_ms`` of quiet ends it.
    """

    def __init__(
        self,
        *,
        ratio: float = 3.0,
        min_rms: float = 0.008,
        start_frames: int = 3,
        end_silence_ms: int = 600,
    ) -> None:
        self.ratio = ratio
        self.min_rms = min_rms
        self.start_frames = start_frames
        self.end_frames = max(1, end_silence_ms * SAMPLE_RATE // 1000 // FRAME_SAMPLES)
        self.noise = 0.002
        self.in_speech = False
        self._loud = 0
        self._quiet = 0
        self.index = 0

    def reset(self) -> None:
        self.in_speech = False
        self._loud = self._quiet = 0

    def push(self, frame: bytes) -> VadEvent | None:
        x = pcm_to_float(frame)
        rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
        self.index += 1
        loud = rms > max(self.min_rms, self.noise * self.ratio)
        if not loud:  # track the room's noise floor while quiet
            self.noise = 0.95 * self.noise + 0.05 * rms
        if not self.in_speech:
            self._loud = self._loud + 1 if loud else 0
            if self._loud >= self.start_frames:
                self.in_speech = True
                self._quiet = 0
                return VadEvent("start", self.index - self._loud)
        else:
            self._quiet = 0 if loud else self._quiet + 1
            if self._quiet >= self.end_frames:
                self.in_speech = False
                self._loud = 0
                return VadEvent("end", self.index)
        return None


# -- devices ---------------------------------------------------------------------------
class AudioUnavailable(RuntimeError):
    pass


def _sd() -> Any:
    try:
        import sounddevice as sd
    except OSError as e:  # PortAudio missing
        raise AudioUnavailable(f"audio not available: {e}") from e
    return sd


# -- choosing the input device ------------------------------------------------------------
@dataclass(slots=True)
class InputDevice:
    id: str  # what goes in the mic_device setting
    label: str
    is_default: bool = False


def _pactl_sources() -> tuple[list[dict[str, Any]], str]:
    """PipeWire/PulseAudio capture sources (monitors excluded) and the default source."""
    platform: str = sys.platform  # (a plain str: checked at runtime, not by mypy)
    if not platform.startswith("linux") or not shutil.which("pactl"):
        return [], ""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True,
                             text=True, timeout=3, check=True).stdout  # fmt: skip
        default = subprocess.run(["pactl", "get-default-source"], capture_output=True,
                                 text=True, timeout=3).stdout.strip()  # fmt: skip
        sources = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return [], ""
    real = [
        x
        for x in sources
        if isinstance(x, dict) and not str(x.get("name", "")).endswith(".monitor")
    ]
    return real, default


def list_inputs() -> list[InputDevice]:
    """Microphones the user can pick, by stable name where the system has one."""
    sources, default = _pactl_sources()
    if sources:
        return [
            InputDevice(
                str(x["name"]), str(x.get("description") or x["name"]), x["name"] == default
            )
            for x in sources
        ]
    sd = _sd()
    default_in = sd.default.device[0]
    return [
        InputDevice(str(i), str(d["name"]), i == default_in)
        for i, d in enumerate(sd.query_devices())
        if d.get("max_input_channels", 0) > 0
    ]


def resolve_input(choice: str) -> tuple[int | str | None, str]:
    """(sounddevice device, PipeWire/Pulse source name) for a mic_device setting.

    "" follows the system's default input, except when that default is a *monitor*
    (the sound going to speakers/earbuds, never a microphone; it happens when
    Bluetooth earbuds connect): then the first real microphone is used, preferring
    a Bluetooth headset mic, since that's what the user is wearing.
    """
    choice = choice.strip()
    sources, default = _pactl_sources()
    names = [str(x["name"]) for x in sources]
    if choice in names:
        return None, choice
    if choice.isdigit():
        return int(choice), ""
    if choice:
        low = choice.lower()
        for x in sources:  # part of a name or description, e.g. "bullets"
            if low in str(x["name"]).lower() or low in str(x.get("description", "")).lower():
                return None, str(x["name"])
        return choice, ""  # sounddevice name match
    if sources and (default.endswith(".monitor") or default not in names):
        pick = next((n for n in names if n.startswith("bluez_input")), names[0])
        log.info("default input %r is not a microphone; using %r", default, pick)
        return None, pick
    return None, ""


def _bt_headset_profile(source: str) -> tuple[str, str] | None:
    """Put the earbuds in headset mode if they're in playback-only (A2DP) mode.
    Returns (card, previous profile) so it can be restored when listening stops."""
    if not shutil.which("pactl"):
        return None
    card = "bluez_card." + source.removeprefix("bluez_input.").split(".")[0].replace(":", "_")
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "cards"], capture_output=True,
                             text=True, timeout=3, check=True).stdout  # fmt: skip
        info = next((c for c in json.loads(out) if c.get("name") == card), None)
        if info is None or "head" in str(info.get("active_profile", "")):
            return None
        profiles = [p for p in info.get("profiles", {}) if "head-unit" in p or "hfp" in p]
        if not profiles:
            return None
        best = "headset-head-unit" if "headset-head-unit" in profiles else profiles[0]
        subprocess.run(["pactl", "set-card-profile", card, best], timeout=3, check=False)
        log.info("switched %s to %s for its microphone", card, best)
        return card, str(info.get("active_profile"))
    except (OSError, subprocess.SubprocessError, ValueError):
        log.debug("couldn't switch the Bluetooth profile", exc_info=True)
        return None


def _set_profile(card: str, profile: str) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["pactl", "set-card-profile", card, profile], timeout=3, check=False)


class MicStream:
    """16 kHz mono microphone, delivering 1,024-byte frames on a queue.

    ``device`` is the mic_device setting: "" (system default), a PipeWire/Pulse
    source name, part of a device's name, or a sounddevice index.
    """

    def __init__(self, device: str | int | None = None) -> None:
        self.device = "" if device is None else str(device)
        self.frames: queue.Queue[bytes] = queue.Queue(maxsize=600)  # ~19 s backlog
        self._stream: object | None = None
        self._buf = b""
        self.source_name = ""
        self._restore: tuple[str, str] | None = None  # (bt card, profile) to switch back to

    def _callback(self, indata, _frames, _time, status) -> None:  # type: ignore[no-untyped-def]
        if status:
            log.debug("mic status: %s", status)
        self._buf += bytes(indata)
        while len(self._buf) >= FRAME_BYTES:
            frame, self._buf = self._buf[:FRAME_BYTES], self._buf[FRAME_BYTES:]
            try:
                self.frames.put_nowait(frame)
            except queue.Full:  # consumer stalled: drop the oldest frame
                with contextlib.suppress(queue.Empty):
                    self.frames.get_nowait()
                self.frames.put_nowait(frame)

    def start(self) -> None:
        sd = _sd()
        device, source = resolve_input(self.device)
        # Route the default ALSA/PortAudio device to that source (PipeWire's ALSA
        # plugin reads PIPEWIRE_NODE, PulseAudio's reads PULSE_SOURCE).
        for var in ("PIPEWIRE_NODE", "PULSE_SOURCE"):
            if source:
                os.environ[var] = source
            else:
                os.environ.pop(var, None)
        if source.startswith("bluez_input"):
            # Bluetooth earbuds only have a working mic in headset (HFP) mode; marking
            # the stream as a call makes PipeWire switch to it (and back afterwards).
            os.environ["PIPEWIRE_PROPS"] = "{ media.role = Communication }"
            os.environ["PULSE_PROP"] = "media.role=phone"
            self._restore = _bt_headset_profile(source)
        self.source_name = source
        stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            channels=1,
            dtype="int16",
            device=device,
            callback=self._callback,
        )
        stream.start()
        self._stream = stream

    def stop(self) -> None:
        s = self._stream
        self._stream = None
        if s is not None:
            s.stop()  # type: ignore[attr-defined]
            s.close()  # type: ignore[attr-defined]
        restore, self._restore = self._restore, None
        if restore is not None:  # earbuds back to high-quality playback
            _set_profile(*restore)

    def drain(self) -> None:
        while not self.frames.empty():
            self.frames.get_nowait()


class Player:
    """Plays PCM16 mono audio without blocking; chunks can be appended while playing."""

    def __init__(self) -> None:
        self._q: queue.Queue[tuple[bytes, int] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.first_audio_at: float | None = None

    def play(self, pcm: bytes, sample_rate: int) -> None:
        self._q.put((pcm, sample_rate))
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="zach-play", daemon=True)
            self._thread.start()

    def end(self) -> None:
        """No more chunks for this utterance."""
        self._q.put(None)

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)

    def wait(self, timeout: float = 30.0) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    def _run(self) -> None:
        import time

        try:
            sd = _sd()
        except AudioUnavailable as e:
            log.warning("%s", e)
            return
        stream = None
        rate = None
        try:
            while not self._stop.is_set():
                item = self._q.get()
                if item is None:
                    break
                pcm, sr = item
                if stream is None or sr != rate:
                    if stream is not None:
                        stream.stop()
                        stream.close()
                    stream = sd.RawOutputStream(samplerate=sr, channels=1, dtype="int16")
                    stream.start()
                    rate = sr
                if self.first_audio_at is None:
                    self.first_audio_at = time.perf_counter()
                stream.write(pcm)
        finally:
            if stream is not None:
                stream.stop()
                stream.close()

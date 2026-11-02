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
from collections.abc import Iterable, Iterator
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
        self.end_ms = end_silence_ms
        self.last_rms = 0.0
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
        self.last_rms = rms
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
    virtual: bool = False  # software source (e.g. a null-sink remap), not a microphone


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


def _is_virtual(source: dict[str, Any]) -> bool:
    props = source.get("properties") or {}
    return (
        str(props.get("node.virtual", "")).lower() == "true"
        or props.get("device.class") == "filter"
        or not str(source.get("name", "")).startswith(("alsa_input", "bluez_input"))
    )


def list_inputs() -> list[InputDevice]:
    """Microphones the user can pick, by stable name where the system has one."""
    sources, default = _pactl_sources()
    if sources:
        return [
            InputDevice(
                str(x["name"]),
                str(x.get("description") or x["name"]),
                x["name"] == default,
                _is_virtual(x),
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


def _pactl_default_sink() -> str:
    if not shutil.which("pactl"):
        return ""
    try:
        return subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True,
                              timeout=3).stdout.strip()  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return ""


def device_key(node: str) -> str:
    """The physical device behind a PipeWire node name, so a headset's speaker and
    mic match: ``bluez_output.88:D6:...`` / ``bluez_input.88:D6:...`` -> the MAC;
    ``alsa_output.usb-JBL_...-00.analog-stereo`` / ``alsa_input.usb-JBL_...-00.mono-
    fallback`` -> ``usb-JBL_...-00`` (also works for ``pci-0000_03_00.6``)."""
    if node.startswith(("bluez_output.", "bluez_input.")):
        return node.split(".")[1]
    if node.startswith(("alsa_output.", "alsa_input.")) and node.count(".") >= 2:
        return node[node.index(".") + 1 : node.rindex(".")]
    return node


def auto_input(exclude: set[str] | frozenset[str] = frozenset()) -> str:
    """Pick the microphone the user is most likely talking into, right now.

    1. the mic of the device they're *listening on* (Bluetooth earbuds, a USB or
       wired headset: the default output's own mic);
    2. otherwise the system's default input, if it's a real microphone;
    3. otherwise any real mic, headsets first.
    Virtual sources, speaker monitors and ``exclude`` (mics found dead) are never
    picked. Returns a PipeWire/Pulse source name, or "" (let the system decide,
    e.g. on Windows, where the default input already follows the active device).
    """
    sources, default = _pactl_sources()
    real = [str(x["name"]) for x in sources if not _is_virtual(x)]
    names = [n for n in real if n not in exclude]
    if not names:
        return ""
    sink = _pactl_default_sink()
    if sink:
        key = device_key(sink)
        same = [n for n in names if device_key(n) == key]
        if same:
            return same[0]
    if default in names:
        return default
    names.sort(key=lambda n: (not n.startswith("bluez_input"), "usb" not in n))
    return names[0]


def resolve_input(choice: str) -> tuple[int | str | None, str]:
    """(sounddevice device, PipeWire/Pulse source name) for a mic_device setting.

    "" means automatic: see ``auto_input`` (the headset you're listening on, else
    the default input; never a monitor or a virtual source).
    """
    choice = choice.strip()
    sources, _default = _pactl_sources()
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
    if sources:
        return None, auto_input()
    return None, ""


# -- Bluetooth headset mode ----------------------------------------------------------------
# Earbuds only have a working mic in headset (HFP) mode, which also makes music sound
# like a phone call. We switch on press and switch back BT_RESTORE_GRACE_S after the
# mic closes, so quick follow-up commands don't flip the profile (each flip takes 1-3 s).
BT_RESTORE_GRACE_S = 8.0
_bt_lock = threading.Lock()
_bt_pending: tuple[threading.Timer, str, str] | None = None  # (timer, card, profile)


def _bt_card(source: str) -> str:
    return "bluez_card." + source.removeprefix("bluez_input.").split(".")[0].replace(":", "_")


def _bt_headset_profile(source: str) -> tuple[str, str] | None:
    """Put the earbuds in headset mode if they're in playback-only (A2DP) mode.
    Returns (card, profile to restore later), or None if nothing was changed."""
    global _bt_pending
    card = _bt_card(source)
    with _bt_lock:
        pending = _bt_pending
        if pending is not None and pending[1] == card:
            pending[0].cancel()  # still in headset mode from the last command
            _bt_pending = None
            return card, pending[2]
    if not shutil.which("pactl"):
        return None
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
    global _bt_pending
    with _bt_lock:
        if _bt_pending is not None and _bt_pending[1] == card:
            _bt_pending = None
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["pactl", "set-card-profile", card, profile], timeout=3, check=False)
        log.info("restored %s to %s", card, profile)


def _restore_later(card: str, profile: str, delay: float = BT_RESTORE_GRACE_S) -> None:
    global _bt_pending
    timer = threading.Timer(delay, _set_profile, (card, profile))
    timer.daemon = True
    with _bt_lock:
        if _bt_pending is not None:
            _bt_pending[0].cancel()
        _bt_pending = (timer, card, profile)
    timer.start()


_ROUTING_VARS = ("PIPEWIRE_NODE", "PULSE_SOURCE", "PIPEWIRE_PROPS", "PULSE_PROP")


@contextlib.contextmanager
def _routed_to(source: str) -> Iterator[None]:
    """Point the default ALSA/PortAudio device at ``source`` *only while the input
    stream is opened* (PipeWire's ALSA plugin reads PIPEWIRE_NODE, PulseAudio's
    PULSE_SOURCE, at open time). Restored right after, so playback streams opened
    later go to the normal output, never to a microphone node."""
    saved = {v: os.environ.get(v) for v in _ROUTING_VARS}
    try:
        if source:
            os.environ["PIPEWIRE_NODE"] = source
            os.environ["PULSE_SOURCE"] = source
            if source.startswith("bluez_input"):  # a call: lets PipeWire use the HFP mic
                os.environ["PIPEWIRE_PROPS"] = "{ media.role = Communication }"
                os.environ["PULSE_PROP"] = "media.role=phone"
        yield
    finally:
        for v, old in saved.items():
            if old is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = old


class MicStream:
    """16 kHz mono microphone, delivering 1,024-byte frames on a queue.

    ``device`` is the mic_device setting: "" (system default), a PipeWire/Pulse
    source name, part of a device's name, or a sounddevice index. Can be started
    and stopped repeatedly (push-to-talk opens it only while you talk).
    """

    def __init__(self, device: str | int | None = None) -> None:
        self.device = "" if device is None else str(device)
        self.frames: queue.Queue[bytes] = queue.Queue(maxsize=600)  # ~19 s backlog
        self._stream: object | None = None
        self._buf = b""
        self.source_name = ""
        self._restore: tuple[str, str] | None = None  # (bt card, profile) to switch back to

    @property
    def active(self) -> bool:
        return self._stream is not None

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
        if self._stream is not None:
            return
        sd = _sd()
        device, source = resolve_input(self.device)
        if source.startswith("bluez_input"):
            self._restore = _bt_headset_profile(source)
        self.source_name = source
        self._buf = b""
        self.drain()

        def open_input(dev: int | str | None) -> Any:
            s = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=FRAME_SAMPLES,
                channels=1,
                dtype="int16",
                device=dev,
                callback=self._callback,
            )
            s.start()
            return s

        try:
            with _routed_to(source):
                stream = open_input(device)
        except Exception as e:
            if not source and device is None:
                raise
            # Routing to that mic failed: take the system's default input instead.
            log.warning("can't open microphone %r (%s); using the system default",
                        source or device, e)  # fmt: skip
            self.source_name = ""
            stream = open_input(None)
        self._stream = stream

    def stop(self) -> None:
        s = self._stream
        self._stream = None
        if s is not None:
            with contextlib.suppress(Exception):
                s.stop()  # type: ignore[attr-defined]
                s.close()  # type: ignore[attr-defined]
        restore, self._restore = self._restore, None
        if restore is not None:  # earbuds back to high-quality playback, a bit later
            _restore_later(*restore)

    def drain(self) -> None:
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                return


class Player:
    """Plays PCM16 mono replies on one long-lived thread.

    Each reply is an *utterance*: ``play()`` chunks as they arrive, then ``end()``.
    ``stop()`` cancels whatever is playing or queued (talking over a reply) and
    leaves nothing behind for the next one: items carry a generation number and
    stale ones are skipped. The output stream stays open between replies (opening
    one costs ~50-300 ms, more over Bluetooth) and is closed after IDLE_CLOSE_S.
    """

    IDLE_CLOSE_S = 10.0
    BLOCK_S = 0.05  # written in small blocks so stop() takes effect within ~50 ms

    def __init__(self, open_stream: Any = None) -> None:
        self._q: queue.Queue[tuple[int, bytes | None, int]] = queue.Queue()
        self._gen = 0
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        self._open = open_stream
        self.first_audio_at: float | None = None
        self.played_bytes = 0

    # -- any thread -------------------------------------------------------------------
    def play(self, pcm: bytes, sample_rate: int) -> None:
        with self._lock:
            self._idle.clear()
            self._q.put((self._gen, pcm, sample_rate))
            self._ensure_thread()

    def end(self) -> None:
        """No more chunks for this utterance."""
        with self._lock:
            self._q.put((self._gen, None, 0))
            self._ensure_thread()

    def stop(self) -> None:
        """Cancel the current utterance and anything queued; the next play() starts fresh."""
        with self._lock:
            self._gen += 1
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            self.first_audio_at = None
            self.played_bytes = 0
            self._idle.set()

    def wait(self, timeout: float = 30.0) -> bool:
        """Wait until the current utterance has finished playing."""
        return self._idle.wait(timeout)

    @property
    def busy(self) -> bool:
        return not self._idle.is_set()

    # -- playback thread ----------------------------------------------------------------
    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="zach-play", daemon=True)
            self._thread.start()

    def _open_stream(self, sr: int) -> Any:
        if self._open is not None:
            return self._open(sr)
        sd = _sd()
        s = sd.RawOutputStream(samplerate=sr, channels=1, dtype="int16")
        s.start()
        return s

    def _run(self) -> None:
        import time

        stream: Any = None
        rate = 0
        try:
            while True:
                try:
                    gen, pcm, sr = self._q.get(timeout=self.IDLE_CLOSE_S)
                except queue.Empty:
                    if stream is not None:  # release the device when quiet
                        with contextlib.suppress(Exception):
                            stream.stop()
                            stream.close()
                        stream = None
                    continue
                if gen != self._gen:
                    continue  # cancelled
                if pcm is None:
                    with self._lock:
                        if gen == self._gen and self._q.empty():
                            self._idle.set()
                    continue
                if stream is None or sr != rate:
                    if stream is not None:
                        with contextlib.suppress(Exception):
                            stream.stop()
                            stream.close()
                    try:
                        stream = self._open_stream(sr)
                    except Exception as e:
                        log.warning("can't play audio: %s", e)
                        stream = None
                        continue
                    rate = sr
                block = max(2, int(sr * self.BLOCK_S) * 2)
                for i in range(0, len(pcm), block):
                    if gen != self._gen:
                        break
                    if self.first_audio_at is None:
                        self.first_audio_at = time.perf_counter()
                    stream.write(pcm[i : i + block])
                    self.played_bytes += min(block, len(pcm) - i)
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.stop()
                    stream.close()

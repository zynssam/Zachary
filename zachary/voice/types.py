"""Voice interfaces. Gnani specifics stay inside ``gnani_*.py`` behind these, so
the provider can be swapped (and mocked in tests)."""

from __future__ import annotations

import queue
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

SAMPLE_RATE = 16_000  # mic capture and STT input: 16 kHz mono PCM16


@dataclass(slots=True)
class Transcript:
    text: str
    provider: str  # "gnani-rest" | "gnani-stream" | "whisper" | ...
    language: str | None = None
    latency_ms: float = 0.0  # end of speech -> text
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class SpeechAudio:
    pcm: bytes  # PCM16 little-endian mono
    sample_rate: int


class VoiceError(RuntimeError):
    """A provider failure, classified so the UI can say something useful.

    kind: auth | credits | scope | rate_limited | network | server | bad_request | config
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def offline(self) -> bool:
        """Worth falling back to local speech recognition."""
        return self.kind in {"network", "server"}


class STT(Protocol):
    name: str

    def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> Transcript: ...


class StreamingSTT(Protocol):
    """Send audio while the user is still talking; get the text right after."""

    name: str

    events: queue.Queue[dict[str, Any]]  # server messages: speech_start, processing, ...

    def open(self) -> None: ...

    def feed(self, frame: bytes) -> None: ...

    def commit(self) -> None: ...

    def finish(self) -> Transcript: ...

    def close(self) -> None: ...

    def abort(self) -> None: ...


class TTS(Protocol):
    name: str

    def synthesize(self, text: str) -> SpeechAudio: ...

    def stream(self, text: str) -> Iterator[SpeechAudio]:
        """Audio chunks as soon as they are ready (lowest latency)."""
        ...


def segment_key(msg: dict[str, Any], arrival: int) -> tuple[int, int]:
    """Sort key for transcript segments: Gnani's ``segment_index`` (finals of a long
    command can arrive out of order), else the order they arrived in."""
    idx = msg.get("segment_index")
    return (idx, arrival) if isinstance(idx, int) else (1 << 30, arrival)


def join_segments(parts: list[tuple[tuple[int, int], str]]) -> str:
    return " ".join(t for _k, t in sorted(parts) if t)

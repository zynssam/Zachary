"""Local speech recognition with whisper.cpp (pywhispercpp): offline fallback for
commands, and the engine behind the local "Hey Zach" spotter. Audio never
leaves the machine here.

The model (``tiny``, ~75 MB, multilingual) is downloaded once into
``<data dir>/models/whisper``. ``audio_ctx`` is shrunk to the clip length so a
2 s clip isn't encoded as a 30 s window (measured ~3 s -> ~0.5-1 s).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zachary.voice.audio import pcm_to_float, seconds
from zachary.voice.types import SAMPLE_RATE, Transcript, VoiceError

log = logging.getLogger(__name__)

DEFAULT_MODEL = "tiny"


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    start_s: float
    end_s: float


class WhisperSTT:
    name = "whisper"

    def __init__(self, models_dir: Path, model: str = DEFAULT_MODEL, threads: int | None = None):
        self.models_dir = models_dir
        self.model = model
        self.threads = threads or max(1, min(4, os.cpu_count() or 2))
        self._m: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        if self._m is None:
            try:
                from pywhispercpp.model import Model
            except ImportError as e:  # pragma: no cover - dependency missing
                raise VoiceError("config", "pywhispercpp is not installed") from e
            self.models_dir.mkdir(parents=True, exist_ok=True)
            self._m = Model(
                self.model,
                models_dir=str(self.models_dir),
                redirect_whispercpp_logs_to=None,
                n_threads=self.threads,
                print_progress=False,
                print_realtime=False,
            )
        return self._m

    def _run(self, pcm: bytes, *, words: bool, prompt: str = "", language: str = "en") -> list[Any]:
        x = pcm_to_float(pcm)
        if len(x) < SAMPLE_RATE // 10:
            return []
        ctx = min(1500, int(len(x) / SAMPLE_RATE / 30 * 1500) + 64)
        kw: dict[str, Any] = {
            "language": language,
            "audio_ctx": ctx,
            "no_context": True,
            "single_segment": not words,
        }
        if words:
            # One segment per word with timings (single_segment would merge them).
            kw.update(max_len=1, split_on_word=True, token_timestamps=True, single_segment=False)
        if prompt:
            kw["initial_prompt"] = prompt
        with self._lock:
            return list(self._load().transcribe(x, **kw))

    def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> Transcript:
        if sample_rate != SAMPLE_RATE:
            from zachary.voice.audio import resample

            pcm = resample(pcm, sample_rate)
        t0 = time.perf_counter()
        segs = self._run(pcm, words=False, language="auto")
        text = " ".join(s.text.strip() for s in segs).strip()
        return Transcript(text, self.name, None, (time.perf_counter() - t0) * 1e3,
                          {"audio_s": round(seconds(pcm), 2)})  # fmt: skip

    def words(self, pcm: bytes, prompt: str = "") -> list[Word]:
        """Word-level transcript with timings (used to cut audio after the wake word).

        If a segment still holds several words, its time is shared out by word length,
        clamped to the real clip length (whisper may report a 30 s window end).
        """
        clip = seconds(pcm)
        out: list[Word] = []
        for s in self._run(pcm, words=True, prompt=prompt):
            text = s.text.strip()
            if not text:
                continue
            t0 = min(s.t0 / 100.0, clip)
            t1 = min(s.t1 / 100.0, clip)
            parts = text.split()
            if len(parts) == 1:
                out.append(Word(text, t0, t1))
                continue
            total = sum(len(p) for p in parts) or 1
            cur = t0
            for p in parts:
                end = cur + (t1 - t0) * len(p) / total
                out.append(Word(p, cur, end))
                cur = end
        return out

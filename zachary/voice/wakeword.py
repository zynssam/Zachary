"""Local "Hey Zach" spotting: voice activity -> whisper.cpp on the first ~1.6 s
of each utterance -> is the wake phrase there? Nothing is trained and nothing
leaves the machine; audio goes to Gnani only *after* the wake word.

"Hey Zach, open my fees" in one breath works: word timings say where "Zach"
ends, and only the audio after it is treated as the command.

Known trade-off (measured with synthetic speech): biasing whisper towards
"Hey Zach" also turns "Hey Jack" into a wake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from zachary.voice.offline_stt import WhisperSTT, Word
from zachary.voice.types import SAMPLE_RATE

SPOT_SECONDS = 1.6  # audio from speech start that is checked for the wake phrase
PROMPT = "Hey Zach,"
_NAME = re.compile(r"^(zach|zack|zac|zak|zachary|zachs?|zack's)$")
_GREETING = frozenset({"hey", "hi", "hello", "ok", "okay", "hay", "he", "yo"})


def _norm(w: str) -> str:
    return re.sub(r"[^a-z']", "", w.lower()).strip("'")


@dataclass(frozen=True, slots=True)
class WakeResult:
    found: bool
    heard: str  # what whisper heard (for logs / debugging, never sent anywhere)
    cut_s: float = 0.0  # command audio starts here (end of "Zach"), seconds from window start


def find_wake(words: list[Word]) -> WakeResult:
    heard = " ".join(w.text for w in words)
    norm = [_norm(w.text) for w in words]
    for i, n in enumerate(norm[:4]):  # the wake phrase must open the utterance
        if _NAME.match(n) and (i == 0 or norm[i - 1] in _GREETING or i <= 1):
            return WakeResult(True, heard, words[i].end_s)
    return WakeResult(False, heard)


class WakeSpotter:
    def __init__(self, whisper: WhisperSTT) -> None:
        self.whisper = whisper

    def check(self, pcm: bytes) -> WakeResult:
        """``pcm``: audio from the start of an utterance (any length; first 1.6 s used)."""
        window = pcm[: int(SPOT_SECONDS * SAMPLE_RATE) * 2]
        return find_wake(self.whisper.words(window, prompt=PROMPT))

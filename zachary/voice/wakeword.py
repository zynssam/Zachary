"""Local "Hey Zach" spotting. Fast path: ``VoskSpotter`` (streaming, a fixed phrase
list, no training; see the bottom of this file). Fallback (no Vosk model): voice
activity -> whisper.cpp on the first ~1.6 s
of each utterance -> is the wake phrase there? Nothing is trained and nothing
leaves the machine; audio goes to Gnani only *after* the wake word.

"Hey Zach, open my fees" in one breath works: word timings say where "Zach"
ends, and only the audio after it is treated as the command.

Known trade-off (measured with synthetic speech): biasing whisper towards
"Hey Zach" also turns "Hey Jack" into a wake.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zachary.voice.offline_stt import WhisperSTT, Word
from zachary.voice.types import SAMPLE_RATE

log = logging.getLogger(__name__)

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


# -- fast spotting: Vosk with a fixed phrase list (no training) -----------------------------
VOSK_MODEL = "vosk-model-small-en-in-0.4"  # 36 MB download, Indian English
VOSK_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL}.zip"
WAKE_PHRASES = (
    "hey zack", "ok zack", "okay zack",  # ("hi zack": "hey Sara" was read as it)
    # "Hey Zach, open Spotify" in one breath is often heard as "zack open ...":
    # the name followed straight by a command counts too (seen with the user's voice).
    # (not "zack what": "...they act like..." was read as it)
    "zack open", "zack show", "zack find", "zack play", "zack start", "zack search",
)  # fmt: skip
# Phrases that sound close but must *not* wake (without them the recognizer maps
# "hey Jack" to "hey zack", the nearest phrase it knows). Measured with natural speech.
DECOYS = (
    "hey jack", "hey jake", "hey sack", "hey sara", "hi", "hello", "zachary",
    # Everyday words: ordinary speech then has somewhere to go besides the wake phrase
    # (without them "open the zachary project" ended as "hey zack").
    "open", "the", "project", "what", "my", "is", "show", "find", "file",
)  # fmt: skip
STABLE_UPDATES = 3  # partial result must show the wake phrase this many times in a row
# (a partial briefly read "hey zack" for "hey Sara" once; 3 updates = ~0.1 s, measured:
# fires ~0.9 s into "Hey Zach, open ...", right after "Zach"; final results fire ~2 s)


class VoskSpotter:
    """Streaming "Hey Zach" detector: feed every 32 ms frame; ~4% of real time on one
    CPU core, ~80 MB RAM. Runs only locally; nothing is sent anywhere."""

    name = "vosk"

    def __init__(self, models_dir: Path) -> None:
        self.model_dir = models_dir / "vosk" / VOSK_MODEL
        self._model: object | None = None
        self._rec: object | None = None
        self._streak = 0
        self.last_heard = ""  # last finished phrase (for `cli voice wake-test` / logs)

    def available(self) -> bool:
        try:
            import vosk  # noqa: F401
        except ImportError:
            return False
        return (self.model_dir / "am").is_dir()

    def _recognizer(self) -> object:
        if self._rec is None:
            import json

            import vosk

            vosk.SetLogLevel(-1)
            if self._model is None:
                self._model = vosk.Model(str(self.model_dir))
            self._rec = vosk.KaldiRecognizer(
                self._model, SAMPLE_RATE, json.dumps([*WAKE_PHRASES, *DECOYS, "[unk]"])
            )
        return self._rec

    def reset(self) -> None:
        self._rec = None  # a fresh recognizer: nothing heard before carries over
        self._streak = 0

    def feed(self, frame: bytes) -> bool:
        """True once the wake phrase was clearly heard (then call ``reset``)."""
        import json

        rec: Any = self._recognizer()
        if rec.AcceptWaveform(frame):
            self._streak = 0  # an utterance ended without a (stable) wake phrase
            heard = json.loads(rec.Result()).get("text", "")
            self.last_heard = heard
            if heard and heard != "[unk]":
                # Local log only (text, never audio): why "Hey Zach" didn't fire.
                log.info("wake word: heard %r (not the wake phrase)", heard)
            return False
        partial = json.loads(rec.PartialResult()).get("partial", "")
        tail = " ".join(partial.split()[-2:])
        self._streak = self._streak + 1 if tail in WAKE_PHRASES else 0
        return self._streak >= STABLE_UPDATES


def download_vosk(models_dir: Path) -> Path:
    """Fetch the small Vosk model once (~36 MB) into ``<models>/vosk``."""
    import io
    import urllib.request
    import zipfile

    target = models_dir / "vosk" / VOSK_MODEL
    if (target / "am").is_dir():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(VOSK_URL, timeout=60) as r:  # (a fixed https URL)
        data = r.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for member in z.namelist():  # no absolute paths / ".." in a downloaded archive
            if member.startswith("/") or ".." in Path(member).parts:
                raise ValueError(f"unsafe path in the Vosk archive: {member}")
        z.extractall(target.parent)
    return target

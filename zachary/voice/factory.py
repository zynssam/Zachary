"""Build the voice components from settings. No key -> fully local (whisper)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from zachary.core.config import Config
from zachary.core.env import get_secret
from zachary.voice import gnani_stt, gnani_tts
from zachary.voice.offline_stt import WhisperSTT
from zachary.voice.types import STT, TTS, StreamingSTT
from zachary.voice.usage import UsageLedger
from zachary.voice.wakeword import VoskSpotter, WakeSpotter

log = logging.getLogger(__name__)


@dataclass(slots=True)
class VoiceKit:
    rest_stt: STT | None
    stream_stt: Callable[[], StreamingSTT] | None
    offline_stt: STT
    spotter: WakeSpotter
    tts: TTS | None
    ledger: UsageLedger
    has_key: bool
    mic_device: str = ""
    fast_spotter: VoskSpotter | None = None  # "Hey Zach" via Vosk (if its model is there)


class Keyterms:
    """Your names for Gnani's ``keyterms`` (projects, apps, topics, folders; never file
    contents), loaded in the background so a command never waits; refreshed every 10
    minutes. Approved by the user; off with ``cli set stt_keyterms false``."""

    def __init__(self, config: Config) -> None:
        # The background thread uses its *own* connection: sharing the caller's one across
        # threads crashed SQLite when it was closed mid-read (seen on CI).
        row = config.conn.execute("PRAGMA database_list").fetchone()
        self.db_path = str(row[2]) if row and row[2] else ""
        self.roots = [str(r) for r in config.index_roots()]
        self.platform = config.platform
        self._names: list[str] = []
        self._at = 0.0
        self._lock = threading.Lock()
        self._busy = False
        self._load()

    def _load(self) -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True

        def run() -> None:
            try:
                names = self._build()
                with self._lock:
                    self._names, self._at = names, time.monotonic()
            except Exception:
                log.debug("could not build keyterms", exc_info=True)
            finally:
                with self._lock:
                    self._busy = False

        threading.Thread(target=run, name="zachary-keyterms", daemon=True).start()

    def _build(self) -> list[str]:
        from zachary.brain.vocab import names
        from zachary.core.db import open_db

        if not self.db_path:
            return []
        conn = open_db(Path(self.db_path))
        try:
            return names(conn, self.roots, [a.name for a in self.platform.list_apps()])
        finally:
            conn.close()

    def __call__(self) -> list[str]:
        if time.monotonic() - self._at > 600:
            self._load()
        with self._lock:
            return list(self._names)


def build_voice(config: Config, data_dir: Path, *, api_key: str | None = None) -> VoiceKit:
    s = config.load()
    key = api_key if api_key is not None else get_secret("GNANI_API_KEY")
    ledger = UsageLedger(data_dir / "gnani_usage.jsonl")
    whisper = WhisperSTT(data_dir / "models" / "whisper", s.offline_stt_model)
    rest: STT | None = None
    stream: Callable[[], StreamingSTT] | None = None
    tts: TTS | None = None
    terms = Keyterms(config) if key and s.stt_keyterms else None
    if key:
        # A spoken command is a few seconds of audio, answered in < 1 s by the server:
        # 10 s total (both connection tries) is plenty; past that, local whisper is faster.
        rest = gnani_stt.GnaniRestSTT(
            key,
            language=s.stt_language,
            model=s.stt_model,
            ledger=ledger,
            timeout=10.0,
            keyterms=terms,
        )
        if s.stt_streaming:

            def stream() -> StreamingSTT:
                return gnani_stt.GnaniStreamingSTT(
                    key,
                    language=s.stt_language,
                    model=s.stt_model,
                    ledger=ledger,
                    keyterms=terms,
                )

        tts = gnani_tts.GnaniWsTTS(
            key, voice=s.tts_voice, model=s.tts_model, language=s.tts_language, ledger=ledger
        )
    fast = VoskSpotter(data_dir / "models")
    return VoiceKit(
        rest, stream, whisper, WakeSpotter(whisper), tts, ledger, bool(key), s.mic_device,
        fast if fast.available() else None,
    )  # fmt: skip

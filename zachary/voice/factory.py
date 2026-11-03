"""Build the voice components from settings. No key -> fully local (whisper)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from zachary.core.config import Config
from zachary.core.env import get_secret
from zachary.voice import gnani_stt, gnani_tts
from zachary.voice.offline_stt import WhisperSTT
from zachary.voice.types import STT, TTS, StreamingSTT
from zachary.voice.usage import UsageLedger
from zachary.voice.wakeword import WakeSpotter


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


def build_voice(config: Config, data_dir: Path, *, api_key: str | None = None) -> VoiceKit:
    s = config.load()
    key = api_key if api_key is not None else get_secret("GNANI_API_KEY")
    ledger = UsageLedger(data_dir / "gnani_usage.jsonl")
    whisper = WhisperSTT(data_dir / "models" / "whisper", s.offline_stt_model)
    rest: STT | None = None
    stream: Callable[[], StreamingSTT] | None = None
    tts: TTS | None = None
    if key:
        # A spoken command is a few seconds of audio, answered in < 1 s by the server:
        # 10 s total (both connection tries) is plenty; past that, local whisper is faster.
        rest = gnani_stt.GnaniRestSTT(
            key, language=s.stt_language, model=s.stt_model, ledger=ledger, timeout=10.0
        )
        if s.stt_streaming:

            def stream() -> StreamingSTT:
                return gnani_stt.GnaniStreamingSTT(
                    key, language=s.stt_language, model=s.stt_model, ledger=ledger
                )

        tts = gnani_tts.GnaniWsTTS(
            key, voice=s.tts_voice, model=s.tts_model, language=s.tts_language, ledger=ledger
        )
    return VoiceKit(
        rest, stream, whisper, WakeSpotter(whisper), tts, ledger, bool(key), s.mic_device
    )

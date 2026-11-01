"""Voice in the assistant: what you say goes through the same engine (and the same
conversation context) as typing, and the one-line reply is spoken.

Latency for every voice command is logged and appended to
``<data dir>/latency.jsonl``: wake -> speech_end -> stt -> intent_search ->
tts_first_audio -> tts_done.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from zachary.brain.handler import Reply
from zachary.telemetry.latency import Timeline
from zachary.ui.engine import Engine
from zachary.ui.quickbar import QuickBar
from zachary.voice.audio import Player
from zachary.voice.factory import VoiceKit
from zachary.voice.pipeline import FrameSource, Heard, MicSource, VoiceListener
from zachary.voice.types import VoiceError

log = logging.getLogger(__name__)

STATE_TEXT = {
    "listening": "Listening…",
    "thinking": "…",
}


class VoiceController(QObject):
    heard = Signal(object)
    state = Signal(str)
    spoken = Signal(object)  # finished Timeline record (tests, telemetry)
    _status = Signal(str)

    def __init__(
        self,
        engine: Engine,
        bar: QuickBar,
        kit: VoiceKit,
        *,
        speak: bool = True,
        wake_word: bool = True,
        latency_path: Path | None = None,
        player: Player | None = None,
    ) -> None:
        super().__init__()
        self.engine = engine
        self.bar = bar
        self.kit = kit
        self.speak = speak and kit.tts is not None
        self.wake_word = wake_word
        self.latency_path = latency_path
        self.player = player or Player()
        self.listener: VoiceListener | None = None
        self._pending: dict[int, Timeline] = {}
        self.heard.connect(self._on_heard)
        self.state.connect(self._on_state)
        self._status.connect(self.bar.status.setText)
        engine.replied.connect(self._on_reply)

    def start(self, source: FrameSource | None = None) -> None:
        self.listener = VoiceListener(
            source or MicSource(device=self.kit.mic_device),
            on_heard=self.heard.emit,
            on_state=self.state.emit,
            rest_stt=self.kit.rest_stt,
            stream_stt=self.kit.stream_stt,
            offline_stt=self.kit.offline_stt,
            spotter=self.kit.spotter,
            wake_word=self.wake_word,
        )
        self.listener.start()

    def stop(self) -> None:
        if self.listener is not None:
            self.listener.stop()
            self.listener.join(timeout=3)
            self.listener = None
        self.player.stop()

    def push_to_talk(self) -> None:
        if self.listener is not None:
            self.player.stop()  # talking over the reply interrupts it
            self.listener.push_to_talk()

    # -- UI thread -------------------------------------------------------------------
    def _on_state(self, state: str) -> None:
        if state == "listening":
            if not self.bar.isVisible():
                self.bar.show_bar()
            self.bar.status.setText(STATE_TEXT[state])
        elif state == "thinking":
            self.bar.status.setText(STATE_TEXT[state])
        elif state.startswith("info"):
            log.info("voice: %s", state)
        elif state.startswith("error"):
            log.warning("voice: %s", state)
            self.bar.status.setText(state)

    def _on_heard(self, h: Heard) -> None:
        if not h.text:
            msg = str(h.error) if h.error else "I didn't catch that."
            log.info("voice: %s", msg)
            self.bar.status.setText(msg)
            h.timeline.info["error"] = h.error.kind if h.error else "empty"
            h.timeline.finish(str(self.latency_path) if self.latency_path else None)
            return
        h.timeline.info.update(
            {"via": h.via, "audio_s": h.audio_s,
             "stt": h.transcript.provider if h.transcript else None}
        )  # fmt: skip
        if not self.bar.isVisible():
            self.bar.show_bar()
        self.bar.edit.blockSignals(True)
        self.bar.edit.setText(h.text)
        self.bar.edit.blockSignals(False)
        rid = self.engine.ask("submit", h.text)
        self._pending[rid] = h.timeline

    def _on_reply(self, rid: int, reply: Reply) -> None:
        tl = self._pending.pop(rid, None)
        if tl is None:
            return
        tl.mark("intent_search")
        tl.info["handler_ms"] = round(reply.ms, 1)
        tl.info["intent"] = reply.intent.kind
        if not self.speak or not reply.say:
            self._done(tl)
            return
        threading.Thread(target=self._say, args=(reply.say, tl), daemon=True).start()

    # -- speaking thread -------------------------------------------------------------
    def _say(self, text: str, tl: Timeline) -> None:
        assert self.kit.tts is not None
        first = True
        try:
            for chunk in self.kit.tts.stream(text):
                if first:
                    tl.mark("tts_first_audio")
                    first = False
                self.player.play(chunk.pcm, chunk.sample_rate)
            self.player.end()
            tl.mark("tts_done")
        except VoiceError as e:
            tl.mark("tts_done")
            tl.info["tts_error"] = e.kind
            self._status.emit(f"{text}  (couldn't speak: {e})")
        self._done(tl)

    def _done(self, tl: Timeline) -> None:
        rec = tl.finish(str(self.latency_path) if self.latency_path else None)
        rec["total_from_speech_end_ms"] = tl.total_ms(since="speech_end")
        self.spoken.emit(rec)

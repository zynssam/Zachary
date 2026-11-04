"""Voice in the assistant: what you say goes through the same engine (and the same
conversation context) as typing, and the one-line reply is spoken.

Latency for every voice command is logged and appended to
``<data dir>/latency.jsonl``: wake -> speech_end -> stt -> intent_search ->
tts_first_audio -> tts_done.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

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


REARM_MS = 150  # dictation: pause between a piece and listening again
DICTATION_PAUSE_MS = 1500  # dictation: a breath doesn't end a sentence, a real pause does


class VoiceController(QObject):
    heard = Signal(object)
    state = Signal(str)
    spoken = Signal(object)  # finished Timeline record (tests, telemetry)
    _status = Signal(str)
    orb_state = Signal(str)  # the bar's voice orb (from any thread; delivered on the UI)

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
        self._source: FrameSource | None = None
        self._pending: dict[int, Timeline] = {}
        # One speaker thread: replies are spoken in order, a new one cancels the old.
        self._say_q: queue.Queue[tuple[int, str, Timeline] | None] = queue.Queue()
        self._say_gen = 0
        self._speaker = threading.Thread(target=self._speak_loop, name="zach-speak", daemon=True)
        self._speaker.start()
        self.heard.connect(self._on_heard)
        self.state.connect(self._on_state)
        self._status.connect(self.bar.status.setText)
        engine.replied.connect(self._on_reply)
        # Voice dictation: listen again after each piece (after any spoken reply ended,
        # so the speaker isn't recorded) until "stop dictation".
        self.dictating = False
        self.spoken.connect(self._rearm)
        # The orb moves with the real audio: your mic while listening, Zach's voice while
        # speaking (levels are plain floats, read on the UI thread at ~60 fps).
        self.orb_state.connect(self.bar.set_voice_state)
        self.bar.orb.level_source = self._orb_level

    def _orb_level(self) -> float:
        from zachary.ui.voice_visual import level_from_rms

        if self.bar.orb.state == "speaking":
            return level_from_rms(self.player.level)
        lis = self.listener
        return level_from_rms(lis.level) if lis is not None else 0.0

    def start(self, source: FrameSource | None = None) -> None:
        self._source = source or self._source or MicSource(device=self.kit.mic_device)
        self.listener = VoiceListener(
            self._source,
            on_heard=self.heard.emit,
            on_state=self.state.emit,
            rest_stt=self.kit.rest_stt,
            stream_stt=self.kit.stream_stt,
            offline_stt=self.kit.offline_stt,
            spotter=self.kit.spotter,
            wake_word=self.wake_word,
            fast_spotter=self.kit.fast_spotter,
        )
        self.listener.start()
        warm = getattr(self.kit.tts, "warm", None)
        if self.speak and warm is not None:  # connect TTS now: the first reply is faster
            threading.Thread(target=self._warm, args=(warm,), daemon=True).start()

    @staticmethod
    def _warm(warm: object) -> None:
        try:
            warm()  # type: ignore[operator]
        except Exception as e:
            log.info("voice: TTS warm-up failed (%s); will connect on first reply", e)

    def stop(self) -> None:
        if self.listener is not None:
            self.listener.stop()
            self.listener.join(timeout=3)
            self.listener = None
        self._cancel_speech()
        self._say_q.put(None)
        close = getattr(self.kit.tts, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                close()

    def push_to_talk(self, hold: bool = False, pause_ms: int | None = None) -> None:
        log.info("voice: push-to-talk%s", " (hold)" if hold else "")
        self._cancel_speech()  # talking over the reply interrupts it
        if self.listener is None or not self.listener.is_alive():
            log.warning("voice: listener was not running; restarting it")
            self.start()
        assert self.listener is not None
        self.listener.push_to_talk(hold=hold, pause_ms=pause_ms)

    def bar_visible(self, shown: bool) -> None:
        """Keep the mic warm (local buffer only) while the bar is open."""
        if self.listener is not None and not self.wake_word:
            self.listener.set_warm(shown)
        keep = getattr(self.player, "keep_alive", None)
        if self.speak and keep is not None:
            keep(shown)  # speaker awake while the bar is open: replies start at once

    def hold_talk(self, pressed: bool) -> None:
        """Space held in the bar: record while held, send on release."""
        if pressed:
            self.push_to_talk(hold=True)
        elif self.listener is not None:
            self.listener.release()

    def _cancel_speech(self) -> None:
        self._say_gen += 1
        self.player.stop()

    # -- UI thread -------------------------------------------------------------------
    def _on_state(self, state: str) -> None:
        if state in ("listening", "thinking"):
            log.info("voice: %s", state)
        if state == "wake":
            # Every "Hey Zach" brings the bar up: opened if hidden, raised and focused if
            # it was left open behind other windows.
            self.bar.show_bar()
            self.orb_state.emit("wake")  # pops, then listening
            return
        if state in ("listening", "thinking"):
            self.orb_state.emit("dictating" if self.dictating and state == "listening"
                                else state)  # fmt: skip
        elif state == "idle" and self.bar.orb.state != "speaking":
            self.orb_state.emit("dictating" if self.dictating else "idle")
        elif state.startswith("error"):
            self.orb_state.emit("error")
        if state == "listening":
            if not self.bar.isVisible():
                self.bar.show_bar()
            held = self.listener is not None and self.listener._hold
            self.bar.status.setText("Listening… (release Space to send)" if held
                                    else STATE_TEXT[state])  # fmt: skip
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
        log.info("voice: heard %r via %s", h.text, h.transcript.provider if h.transcript else "?")
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
        self.dictating = reply.dictating
        if not self.speak or not reply.say:
            self._done(tl)
            return
        self._cancel_speech()
        self._say_q.put((self._say_gen, reply.say, tl))

    # -- speaking thread -------------------------------------------------------------
    def _speak_loop(self) -> None:
        while True:
            item = self._say_q.get()
            if item is None:
                return
            gen, text, tl = item
            if gen != self._say_gen:
                self._done(tl)  # superseded before it started
                continue
            try:
                self._say(gen, text, tl)
            except Exception:
                log.exception("voice: speaking failed")
                self._done(tl)

    def _say(self, gen: int, text: str, tl: Timeline) -> None:
        assert self.kit.tts is not None
        first = True
        self.player.stop()  # a fresh utterance
        try:
            with contextlib.closing(iter(self.kit.tts.stream(text))) as chunks:  # type: ignore[type-var]
                for chunk in chunks:
                    if gen != self._say_gen:
                        tl.info["tts_cancelled"] = True
                        break
                    if first:
                        tl.mark("tts_first_audio")
                        first = False
                        self.orb_state.emit("speaking")
                    self.player.play(chunk.pcm, chunk.sample_rate)
            self.player.end()
            tl.mark("tts_done")
            if self.player.wait(30) and self.player.first_audio_at is not None:
                tl.info["audio_out_ms"] = round(
                    (self.player.first_audio_at - tl.t0) * 1e3, 1
                )  # press -> sound actually playing
        except VoiceError as e:
            tl.mark("tts_done")
            tl.info["tts_error"] = e.kind
            self._status.emit(f"{text}  (couldn't speak: {e})")
        self._done(tl)

    def _rearm(self, _rec: object = None) -> None:
        if self.dictating:
            QTimer.singleShot(REARM_MS, self._rearm_now)

    def _rearm_now(self) -> None:
        if self.dictating:
            self.push_to_talk(pause_ms=DICTATION_PAUSE_MS)

    def _done(self, tl: Timeline) -> None:
        self.orb_state.emit("dictating" if self.dictating else "idle")
        rec = tl.finish(str(self.latency_path) if self.latency_path else None)
        rec["total_from_speech_end_ms"] = tl.total_ms(since="speech_end")
        self.spoken.emit(rec)

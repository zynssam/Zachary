"""The quick bar: a search box with results underneath, shown on a global hotkey.

The window is built once and only shown/hidden, and it shows the last list
immediately on open (then refreshes in the background), so "hotkey -> results
on screen" doesn't wait for a query. All queries go to the Engine thread.

Keys: type to search; Enter = do what the text says (open / follow-up) or open
the row you moved to with the arrows; Ctrl+Enter = show in folder; Ctrl+R =
related files; Ctrl+M = talk (voice); Ctrl+1..9 = open that row; Up/Down = move;
Esc = hide.
"""

from __future__ import annotations

import os
import time
from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QScreen,
    QShortcut,
)
from PySide6.QtWidgets import QLabel, QLineEdit, QVBoxLayout, QWidget

from zachary.brain.handler import Reply
from zachary.telemetry.latency import LATENCY
from zachary.ui import style
from zachary.ui.engine import Engine
from zachary.ui.notes import NotesWindow
from zachary.ui.plan_review import PlanReview
from zachary.ui.results import ResultsView

TYPE_DEBOUNCE_MS = 25
HIDE_AFTER_ACTION_MS = 700
WIDTH = 720
MAX_ROWS = 8
SPACE_HOLD_MS = 400  # Space held this long while typing = talk (shorter = a space)
THINKING_AFTER_MS = 300


class QuickBar(QWidget):
    opened = Signal(float)  # ms from hotkey to first results painted
    talk = Signal()  # Ctrl+M: push-to-talk
    hold_talk = Signal(bool)  # Space held in an empty box: True = pressed, False = released
    visibility = Signal(bool)  # shown / hidden (voice keeps the mic warm while shown)
    reply_applied = Signal(object)
    notes_opened = Signal(object)  # the notes window, the first time it's created

    def __init__(self, engine: Engine) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.engine = engine
        self.setWindowTitle("Zachary")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(WIDTH)

        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText(
            "Ask Zach: “fees yesterday”, “my projects”, “open the second one”"
        )
        f = QFont(self.edit.font())
        f.setPointSizeF(f.pointSizeF() * 1.45)
        self.edit.setFont(f)
        self.edit.setFrame(False)
        self.edit.setMinimumHeight(44)
        self.status = QLabel(self)
        self.status.setWordWrap(True)  # answers/explanations can be two or three sentences
        self.status.setStyleSheet("color: palette(placeholder-text); padding: 2px 4px;")
        # An answer's source passage ("ask your files"); hidden otherwise.
        self.answer = QLabel(self)
        self.answer.setWordWrap(True)
        self.answer.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.answer.setStyleSheet(style.ANSWER_CSS)
        self.answer.setVisible(False)
        self.list = ResultsView(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(6)
        lay.addWidget(self.edit)
        lay.addWidget(self.status)
        lay.addWidget(self.answer)
        lay.addWidget(self.list)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(TYPE_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._send_preview)
        self.edit.textChanged.connect(lambda _t: self._debounce.start())
        self.edit.installEventFilter(self)
        # Ctrl+M talks wherever the focus is inside the bar (edit, results list...).
        talk = QShortcut(QKeySequence("Ctrl+M"), self)
        talk.setContext(Qt.ShortcutContext.WindowShortcut)
        talk.activated.connect(self.talk.emit)
        self.list.chosen.connect(self._choose)
        self.list.painted.connect(self._on_painted)
        engine.replied.connect(self._on_reply)
        engine.failed.connect(lambda _rid, msg: self.status.setText(f"Error: {msg}"))

        self._last_applied = 0
        self.review: PlanReview | None = None
        self.notes: NotesWindow | None = (
            None  # Zachary's notes app  # clean/organize plans (Phase 7)
        )
        self._explicit_selection = False  # user moved with arrows since the last reply
        self._holding_space = False
        # In a box with text, Space is a space when tapped and talk when held longer
        # than SPACE_HOLD_MS: the space waits until we know which.
        self._space_pending = False
        self._space_timer = QTimer(self)
        self._space_timer.setSingleShot(True)
        self._space_timer.setInterval(SPACE_HOLD_MS)
        self._space_timer.timeout.connect(self._space_held)
        self._open_t0: float | None = None
        self._pending_initial = False
        self._resize_rows(0)

    # -- show / hide ---------------------------------------------------------------------
    def toggle(self, activation_token: str | None = None, t0: float | None = None) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show_bar(activation_token, t0)

    def show_bar(self, activation_token: str | None = None, t0: float | None = None) -> None:
        self._open_t0 = t0 if t0 is not None else time.perf_counter()
        if activation_token:  # Wayland: lets the compositor give us focus
            os.environ["XDG_ACTIVATION_TOKEN"] = activation_token
        self.edit.selectAll()
        self._place()
        self.show()
        self.raise_()
        self.activateWindow()
        self.edit.setFocus()
        # Fresh data in the background; the cached list is already on screen.
        self._pending_initial = True
        self.engine.ask("preview", self.edit.text())

    def _place(self) -> None:
        # Either call can return None at runtime (no screen under the cursor / headless).
        screen: QScreen | None = QGuiApplication.screenAt(self.cursor().pos())
        if not screen:
            screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        g = screen.availableGeometry()
        self.move(g.center().x() - WIDTH // 2, g.top() + g.height() // 5)

    def _resize_rows(self, n: int) -> None:
        rows = min(max(n, 0), MAX_ROWS)
        self.list.setVisible(rows > 0)
        self.list.setFixedHeight(rows * 54 + (4 if rows else 0))
        self.adjustSize()

    # -- input ---------------------------------------------------------------------------
    def _send_preview(self) -> None:
        self._explicit_selection = False
        self.engine.ask("preview", self.edit.text())

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:
        if obj is self.edit and ev.type() == QEvent.Type.KeyPress:
            assert isinstance(ev, QKeyEvent)
            return self._key(ev)
        if obj is self.edit and ev.type() == QEvent.Type.KeyRelease:
            assert isinstance(ev, QKeyEvent)
            if ev.key() == Qt.Key.Key_Space and (self._holding_space or self._space_pending):
                if not ev.isAutoRepeat():
                    if self._space_pending:
                        self._flush_space()  # a tap: just a space
                    else:
                        self._holding_space = False
                        self.hold_talk.emit(False)
                return True
        if obj is self.edit and ev.type() == QEvent.Type.FocusOut:
            if self._space_pending:
                self._flush_space()
            if self._holding_space:
                self._holding_space = False  # focus lost while holding: treat as released
                self.hold_talk.emit(False)
        if ev.type() == QEvent.Type.WindowDeactivate and obj is self:
            self._maybe_hide()
        return super().eventFilter(obj, ev)

    def _flush_space(self) -> None:
        self._space_timer.stop()
        self._space_pending = False
        self.edit.insert(" ")

    def _space_held(self) -> None:
        if self._space_pending:  # still down after SPACE_HOLD_MS: it's a talk
            self._space_pending = False
            self._holding_space = True
            self.hold_talk.emit(True)

    def _key(self, ev: QKeyEvent) -> bool:
        key = ev.key()
        ctrl = bool(ev.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if self._space_pending and key != Qt.Key.Key_Space:
            self._flush_space()  # fast typing: next key before Space came up -> a space
        plain = ev.modifiers() == Qt.KeyboardModifier.NoModifier
        if key == Qt.Key.Key_Space and (self._holding_space or self._space_pending or plain):
            # Hold Space to talk, release to send. Empty box: right away (a search
            # never starts with a space). While typing: only a long press talks.
            if ev.isAutoRepeat() or self._holding_space or self._space_pending:
                return True
            if not self.edit.text():
                self._holding_space = True
                self.hold_talk.emit(True)
            else:
                self._space_pending = True
                self._space_timer.start()
            return True
        if key == Qt.Key.Key_Escape:
            self.hide()
            return True
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            self.list.step(1 if key == Qt.Key.Key_Down else -1)
            self._explicit_selection = True
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            row = self.list.selected_row()
            if ctrl and row is not None:
                self._choose(row, "reveal")
            elif self._explicit_selection and row is not None:
                self._choose(row, "open")
            else:
                self._debounce.stop()
                rid = self.engine.ask("submit", self.edit.text())
                # Questions can take a second or two (LLM): say so if it isn't instant.
                QTimer.singleShot(THINKING_AFTER_MS, lambda: self._thinking(rid))
            return True
        if ctrl and key == Qt.Key.Key_R:
            row = self.list.selected_row()
            self._choose(0 if row is None else row, "related")
            return True
        if ctrl and Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            self._choose(key - Qt.Key.Key_1, "open")
            return True
        return False

    def _thinking(self, rid: int) -> None:
        if self._last_applied < rid:
            self.status.setText("Thinking…")

    def _choose(self, row: int, action: str) -> None:
        self.engine.ask("act", (row, action))

    def changeEvent(self, ev: QEvent) -> None:
        if ev.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            self._maybe_hide()
        super().changeEvent(ev)

    def showEvent(self, ev: Any) -> None:
        super().showEvent(ev)
        self.visibility.emit(True)

    def hideEvent(self, ev: Any) -> None:
        super().hideEvent(ev)
        self.visibility.emit(False)

    def _maybe_hide(self) -> None:
        if self.isVisible() and not self.list.menu_open:
            QTimer.singleShot(0, self.hide)

    # -- replies -------------------------------------------------------------------------
    def _review(self) -> PlanReview:
        if self.review is None:
            self.review = PlanReview()
            self.review.approved.connect(
                lambda pid, sel, dst: self.engine.ask("approve", (pid, sel, dst))
            )
            self.review.rejected.connect(lambda pid: self.engine.ask("reject", pid))
            self.review.undo_requested.connect(lambda pid: self.engine.ask("undo_plan", pid))
        return self.review

    def notes_window(self) -> NotesWindow:
        if self.notes is None:
            self.notes = NotesWindow()
            self.notes_opened.emit(self.notes)
        return self.notes

    def _on_reply(self, rid: int, reply: Reply) -> None:
        if reply.intent.kind in {"approve", "reject", "undo_plan"}:
            if reply.intent.kind != "reject":
                self._review().show_result(reply)
            self.status.setText(reply.say)
            return  # from the review window: the bar's list stays as it is
        if rid < self._last_applied:
            return  # an older reply arrived late
        self._last_applied = rid
        if reply.notes:  # "open my notes" / "new note" / "take a note"
            self.status.setText(reply.say)
            self.notes_window().present(reply.notes)
            self.hide()
            self.reply_applied.emit(reply)
            return
        if reply.plan is not None and reply.plan.status == "proposed" and reply.plan.items:
            self._review().show_plan(reply.plan)
        if reply.dictating and reply.intent.kind == "dictate" and not reply.say:
            self.edit.blockSignals(True)  # a dictated line was added: clear the box
            self.edit.clear()
            self.edit.blockSignals(False)
            self.status.setText("Dictating… say “stop dictation” when you're done.")
        else:
            self.status.setText(reply.say)
        self.answer.setText(reply.detail)
        self.answer.setVisible(bool(reply.detail))
        self.list.set_items(reply.items, reply.selected)
        self._resize_rows(len(reply.items))
        self._explicit_selection = False
        self.reply_applied.emit(reply)
        if reply.action and reply.intent.kind != "none" and _performed(reply):
            QTimer.singleShot(HIDE_AFTER_ACTION_MS, self.hide)

    def _on_painted(self) -> None:
        if self._open_t0 is not None and self.list.results.rowCount():
            ms = (time.perf_counter() - self._open_t0) * 1e3
            self._open_t0 = None
            LATENCY.record("quickbar.open_to_results", ms)
            self.opened.emit(ms)

    # -- look ----------------------------------------------------------------------------
    def paintEvent(self, e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = self.palette().window().color()
        bg.setAlpha(245)
        p.setBrush(bg)
        p.setPen(QColor(*style.BORDER_RGBA))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 14, 14)


def _performed(reply: Reply) -> bool:
    """True when the reply says an action actually ran (submit), not just proposed."""
    return reply.say.startswith(("Opening", "Showing"))

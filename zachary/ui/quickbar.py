"""The quick bar: a search box with results underneath, shown on a global hotkey.

The window is built once and only shown/hidden, and it shows the last list
immediately on open (then refreshes in the background), so "hotkey -> results
on screen" doesn't wait for a query. All queries go to the Engine thread.

Keys: type to search; Enter = do what the text says (open / follow-up) or open
the row you moved to with the arrows; Ctrl+Enter = show in folder; Ctrl+1..9 =
open that row; Up/Down = move; Esc = hide.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QScreen,
)
from PySide6.QtWidgets import QLabel, QLineEdit, QVBoxLayout, QWidget

from zachary.brain.handler import Reply
from zachary.telemetry.latency import LATENCY
from zachary.ui.engine import Engine
from zachary.ui.results import ResultsView

TYPE_DEBOUNCE_MS = 25
HIDE_AFTER_ACTION_MS = 700
WIDTH = 720
MAX_ROWS = 8


class QuickBar(QWidget):
    opened = Signal(float)  # ms from hotkey to first results painted
    reply_applied = Signal(object)

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
        self.status.setStyleSheet("color: palette(placeholder-text); padding: 2px 4px;")
        self.list = ResultsView(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(6)
        lay.addWidget(self.edit)
        lay.addWidget(self.status)
        lay.addWidget(self.list)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(TYPE_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._send_preview)
        self.edit.textChanged.connect(lambda _t: self._debounce.start())
        self.edit.installEventFilter(self)
        self.list.chosen.connect(self._choose)
        self.list.painted.connect(self._on_painted)
        engine.replied.connect(self._on_reply)
        engine.failed.connect(lambda _rid, msg: self.status.setText(f"Error: {msg}"))

        self._last_applied = 0
        self._explicit_selection = False  # user moved with arrows since the last reply
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
        if ev.type() == QEvent.Type.WindowDeactivate and obj is self:
            self._maybe_hide()
        return super().eventFilter(obj, ev)

    def _key(self, ev: QKeyEvent) -> bool:
        key = ev.key()
        ctrl = bool(ev.modifiers() & Qt.KeyboardModifier.ControlModifier)
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
                self.engine.ask("submit", self.edit.text())
            return True
        if ctrl and Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            self._choose(key - Qt.Key.Key_1, "open")
            return True
        return False

    def _choose(self, row: int, action: str) -> None:
        self.engine.ask("act", (row, action))

    def changeEvent(self, ev: QEvent) -> None:
        if ev.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            self._maybe_hide()
        super().changeEvent(ev)

    def _maybe_hide(self) -> None:
        if self.isVisible() and not self.list.menu_open:
            QTimer.singleShot(0, self.hide)

    # -- replies -------------------------------------------------------------------------
    def _on_reply(self, rid: int, reply: Reply) -> None:
        if rid < self._last_applied:
            return  # an older reply arrived late
        self._last_applied = rid
        self.status.setText(reply.say)
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
        p.setPen(QColor(128, 128, 128, 90))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 14, 14)


def _performed(reply: Reply) -> bool:
    """True when the reply says an action actually ran (submit), not just proposed."""
    return reply.say.startswith(("Opening", "Showing"))

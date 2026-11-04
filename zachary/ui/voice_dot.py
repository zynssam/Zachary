"""The voice dot: a small glowing dot left of the search box that shows what voice is
doing. Purely visual: it's fed the state strings the voice controller already emits.

| state     | look                                              |
|-----------|---------------------------------------------------|
| idle      | soft, still dot (no timer, no CPU)                |
| listening | breathes (1.6 s) with a soft halo                 |
| thinking  | a thin arc orbits the dot                         |
| speaking  | soft ripples move outward                         |
| error     | amber, shakes once, then still                    |
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from zachary.ui import style

STATES = ("idle", "listening", "thinking", "speaking", "error")
FPS = 60
BREATH_S = 1.6
SHAKE_S = 0.4


def normalize(state: str) -> str:
    """The voice controller's strings -> a dot state ("error: no mic..." -> error)."""
    if state.startswith("error"):
        return "error"
    return state if state in STATES else "idle"


class VoiceDot(QWidget):
    def __init__(self, parent: QWidget | None = None, size: int = 30) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.state = "idle"
        self._since = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(int(1000 / FPS))
        self._timer.timeout.connect(self._tick)

    def set_state(self, state: str) -> None:
        state = normalize(state)
        if state == self.state:
            return
        self.state = state
        self._since = time.monotonic()
        if state == "idle":
            self._timer.stop()
        elif not self._timer.isActive():
            self._timer.start()
        self.update()

    def animating(self) -> bool:
        return self._timer.isActive()

    def _tick(self) -> None:
        if self.state == "error" and time.monotonic() - self._since > SHAKE_S:
            self._timer.stop()  # stays amber, still
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        now = time.monotonic()
        t = now - self._since
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        if self.state == "error" and t < SHAKE_S:
            cx += math.sin(t * 50) * 2.5 * (1 - t / SHAKE_S)
        color = QColor(style.WARN if self.state == "error" else style.ACCENT)
        edge = min(w, h) / 2 - 1
        r = edge * 0.42
        breath = 0.0
        if self.state == "listening":
            breath = 0.5 - 0.5 * math.cos(2 * math.pi * t / BREATH_S)  # 0..1..0
            r *= 1.0 + 0.22 * breath
        center = QPointF(cx, cy)

        # halo
        halo_r = min(edge, r * (2.2 + 0.6 * breath))
        g = QRadialGradient(center, halo_r)
        a = 0.18 if self.state == "idle" else 0.30 + 0.25 * breath
        for pos, alpha in ((0.0, a), (1.0, 0.0)):
            c = QColor(color)
            c.setAlphaF(alpha)
            g.setColorAt(pos, c)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(center, halo_r, halo_r)

        # speaking: soft ripples
        if self.state == "speaking":
            for k in range(3):
                age = ((t / 1.2) + k / 3) % 1.0
                rr = r + age * (edge - r)
                c = QColor(color)
                c.setAlphaF(0.45 * (1 - age))
                p.setPen(QPen(c, 1.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(center, rr, rr)

        # the dot: a soft gradient, lighter top-left
        core = QRadialGradient(QPointF(cx - r * 0.35, cy - r * 0.4), r * 1.6)
        light = QColor(color).lighter(150)
        if self.state == "idle":
            light.setAlphaF(0.85)
        core.setColorAt(0.0, light)
        core.setColorAt(1.0, color)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(core)
        p.drawEllipse(center, r, r)

        # thinking: a thin orbiting arc
        if self.state == "thinking":
            rr = min(edge - 1, r * 2.0)
            c = QColor(color)
            c.setAlphaF(0.9)
            pen = QPen(c, 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            start = int(-(t * 360 / 1.1) % 360 * 16)
            p.drawArc(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr), start, 100 * 16)
        p.end()

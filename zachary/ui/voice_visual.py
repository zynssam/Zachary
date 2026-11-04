"""The voice orb: Zachary's "face" in the bar. It shows what voice is doing and moves
with the real audio (your microphone while you speak, Zach's voice while it replies).

| state     | look                                                          |
|-----------|---------------------------------------------------------------|
| idle      | small dim orb (static: no timer, no CPU)                      |
| wake      | quick pop + glow ("Hey Zach" heard), then listening           |
| listening | grows and brightens with your voice level                     |
| thinking  | a violet -> blue ring spinning around the orb                 |
| speaking  | ripples outward in time with Zach's voice                     |
| dictating | listening + a red recording dot                               |
| error     | amber, shakes once                                            |

Drawn with QPainter at ~60 fps, only while animating. ``level`` is pulled from a
callable (0..1) so no audio thread ever touches the widget.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QConicalGradient, QPainter, QPaintEvent, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from zachary.ui import style

STATES = ("idle", "wake", "listening", "thinking", "speaking", "dictating", "error")
FPS = 60
ATTACK = 0.45  # level smoothing: rises fast ...
RELEASE = 0.12  # ... falls slowly (reads as "breathing", not flicker)
WAKE_POP_S = 0.18
SHAKE_S = 0.45


def level_from_rms(rms: float) -> float:
    """Microphone / speaker RMS (0..1, speech ~0.01-0.3) -> a 0..1 visual level."""
    if rms <= 0.0:
        return 0.0
    db = 20 * math.log10(max(rms, 1e-5))  # -100..0
    return max(0.0, min(1.0, (db + 50) / 40))  # -50 dB -> 0, -10 dB -> 1


class VoiceOrb(QWidget):
    def __init__(self, parent: QWidget | None = None, size: int = 44) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.state = "idle"
        self.level = 0.0  # smoothed 0..1
        self.level_source: Callable[[], float] | None = None
        self.reduce_motion = False
        self._t0 = time.monotonic()
        self._changed_at = self._t0
        self._ripples: list[float] = []  # start times of speaking ripples
        self._timer = QTimer(self)
        self._timer.setInterval(int(1000 / FPS))
        self._timer.timeout.connect(self._tick)

    # -- control ----------------------------------------------------------------------------
    def set_state(self, state: str) -> None:
        if state not in STATES:
            state = "idle"
        if state == self.state:
            return
        self.state = state
        self._changed_at = time.monotonic()
        self._ripples.clear()
        if state == "idle" or self.reduce_motion:
            self._timer.stop()
            self.level = 0.0
        elif not self._timer.isActive():
            self._timer.start()
        self.update()

    def animating(self) -> bool:
        return self._timer.isActive()

    def _tick(self) -> None:
        now = time.monotonic()
        if self.state == "wake" and now - self._changed_at > WAKE_POP_S:
            self.state = "listening"
        if self.state == "error" and now - self._changed_at > SHAKE_S:
            self._timer.stop()  # stays amber, still
        target = 0.0
        if self.level_source is not None and self.state in {"listening", "speaking", "dictating"}:
            try:
                target = max(0.0, min(1.0, float(self.level_source())))
            except Exception:
                target = 0.0
        k = ATTACK if target > self.level else RELEASE
        self.level += (target - self.level) * k
        gap = 0.28 - 0.15 * self.level
        if (
            self.state == "speaking"
            and self.level > 0.08
            and (not self._ripples or now - self._ripples[-1] > gap)
        ):
            self._ripples.append(now)
        self._ripples = [r for r in self._ripples if now - r < 1.2]
        self.update()

    # -- drawing --------------------------------------------------------------------------------
    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        now = time.monotonic()
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        if self.state == "error" and now - self._changed_at < SHAKE_S:
            k = 1 - (now - self._changed_at) / SHAKE_S
            cx += math.sin((now - self._changed_at) * 45) * 3 * k
        base = min(w, h) * 0.22
        t = now - self._t0
        active = self.state not in {"idle", "error"}
        pulse = 1.0 + (0.04 * math.sin(t * 2 * math.pi / 4) if active else 0.0)
        r = base * pulse * (1.0 + 0.55 * self.level)
        if self.state == "wake":
            k = min(1.0, (now - self._changed_at) / WAKE_POP_S)
            r *= 1.0 + 0.35 * math.sin(k * math.pi)
        c1, c2 = (QColor(style.WARN), QColor("#F97316")) if self.state == "error" else (
            QColor(style.ACCENT), QColor(style.ACCENT_2))  # fmt: skip
        dim = self.state == "idle"

        # Speaking: ripples moving out from the orb.
        for start in self._ripples:
            age = (now - start) / 1.2
            rr = r + age * (min(w, h) / 2 - r)
            col = QColor(c2)
            col.setAlphaF(max(0.0, 0.8 * (1 - age)))
            p.setPen(QPen(col, 2.2 - age))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), rr, rr)

        # Glow (stronger with the level).
        glow = QRadialGradient(QPointF(cx, cy), r * 2.1)
        g = QColor(c1)
        g.setAlphaF(0.10 if dim else 0.22 + 0.35 * self.level)
        glow.setColorAt(0.0, g)
        g2 = QColor(c1)
        g2.setAlphaF(0.0)
        glow.setColorAt(1.0, g2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QPointF(cx, cy), r * 2.1, r * 2.1)

        # The orb itself.
        core = QRadialGradient(QPointF(cx - r * 0.3, cy - r * 0.35), r * 1.4)
        hi = QColor("#D9D0FF") if self.state != "error" else QColor("#FFE2A8")
        if dim:
            hi.setAlphaF(0.55)
            c1a, c2a = QColor(c1), QColor(c2)
            c1a.setAlphaF(0.55)
            c2a.setAlphaF(0.55)
        else:
            c1a, c2a = c1, c2
        core.setColorAt(0.0, hi)
        core.setColorAt(0.45, c1a)
        core.setColorAt(1.0, c2a)
        p.setBrush(core)
        p.drawEllipse(QPointF(cx, cy), r, r)

        # Thinking: a spinning gradient ring.
        if self.state == "thinking":
            ring = QConicalGradient(QPointF(cx, cy), -(t * 300) % 360)
            ring.setColorAt(0.0, QColor(style.ACCENT))
            ring.setColorAt(0.5, QColor(style.ACCENT_2))
            fade = QColor(style.ACCENT)
            fade.setAlphaF(0.0)
            ring.setColorAt(1.0, fade)
            pen = QPen(ring, 2.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            rr = r * 1.65
            p.drawArc(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr), 0, 300 * 16)

        # Dictating: a small red recording dot.
        if self.state == "dictating":
            on = int(t * 2) % 2 == 0
            dot = QColor(style.REC)
            dot.setAlphaF(1.0 if on else 0.45)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(dot)
            p.drawEllipse(QPointF(w - 7, 7), 4, 4)
        p.end()

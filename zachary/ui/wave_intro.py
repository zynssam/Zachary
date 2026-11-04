"""The opening animation: a glowing wave rises along the bottom of the screen and flows
into the bar as it appears ("Hey Zach" or the hotkey).

A frameless, click-through, never-focused overlay; it lives ~0.9 s and is hidden
again (no timer, no CPU after). The bar itself opens at once (you can type straight
away); it fades in while the wave gathers under it. Off with
``python -m zachary.cli set open_animation false``.

On X11 / Windows the overlay is a strip at the bottom of the screen. On Wayland a
window can't place itself, so the overlay covers the work area (transparent, input
passes through) and draws at its bottom.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QScreen,
)
from PySide6.QtWidgets import QWidget

from zachary.ui import style

DURATION_S = 0.9
STRIP = 280  # px of the screen's bottom the wave uses
FPS = 60
STEP = 16  # px between wave samples
# (frequency, speed, phase, height share) for each wave layer
LAYERS = ((0.0062, 2.6, 0.0, 1.0), (0.0094, -3.4, 1.9, 0.75), (0.0041, 1.7, 4.1, 0.85),
          (0.013, -4.6, 2.7, 0.5))  # fmt: skip


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def ease_out(x: float) -> float:
    return 1 - (1 - _clamp(x)) ** 3


def ease_in_out(x: float) -> float:
    x = _clamp(x)
    return 3 * x * x - 2 * x * x * x


class WaveIntro(QWidget):
    finished = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setWindowTitle("Zachary wave")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.level_source: Callable[[], float] | None = None
        self.target_x: float | None = None  # where the bar is (window x); None = centre
        self._t0 = 0.0
        self._level = 0.0
        self._fixed_t: float | None = None  # (screenshots: draw one moment)
        self._glow: QImage | None = None
        rnd = random.Random(11)
        self._sparks = [(rnd.random(), rnd.uniform(40, 160), rnd.uniform(0.8, 2.2),
                         rnd.random()) for _ in range(90)]  # fmt: skip
        self._timer = QTimer(self)
        self._timer.setInterval(int(1000 / FPS))
        self._timer.timeout.connect(self._tick)

    # -- control ----------------------------------------------------------------------------
    def play(self, screen: QScreen | None = None, bar_center_x: int | None = None) -> None:
        screen = screen or QGuiApplication.primaryScreen()
        if not screen:  # (None when headless)
            return
        g = screen.availableGeometry()
        if QGuiApplication.platformName() == "wayland":
            self.setGeometry(g)  # (the compositor places it; covering the area keeps
            # the bottom where it should be)
        else:
            self.setGeometry(QRect(g.left(), g.bottom() - STRIP + 1, g.width(), STRIP))
        self.target_x = None if bar_center_x is None else float(bar_center_x - g.left())
        self._t0 = time.monotonic()
        self._level = 0.0
        self.show()
        self._timer.start()

    def playing(self) -> bool:
        return self._timer.isActive()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def _tick(self) -> None:
        if time.monotonic() - self._t0 >= DURATION_S:
            self.stop()
            self.finished.emit()
            return
        if self.level_source is not None:
            try:
                lv = max(0.0, min(1.0, float(self.level_source())))
            except Exception:
                lv = 0.0
            self._level += (lv - self._level) * (0.45 if lv > self._level else 0.12)
        h = self.height()
        self.update(QRect(0, h - STRIP, self.width(), STRIP))

    # -- drawing --------------------------------------------------------------------------------
    def paintEvent(self, _e: QPaintEvent) -> None:
        t = self._fixed_t if self._fixed_t is not None else time.monotonic() - self._t0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.draw(p, self.width(), self.height(), t)
        p.end()

    def draw(self, p: QPainter, w: int, h: int, t: float) -> None:
        rise = ease_out(t / 0.32)  # comes up from below the edge
        gather = ease_in_out((t - 0.36) / 0.42)  # flows together under the bar
        fade = 1 - ease_in_out((t - 0.62) / (DURATION_S - 0.62))
        if fade <= 0:
            return
        tx = self.target_x if self.target_x is not None else w / 2
        bottom = h + 2
        base = bottom - (STRIP * 0.16) * rise + (1 - rise) * 30
        amp = STRIP * 0.2 * (1 + 0.9 * gather) * (1 + 0.6 * self._level)
        xs = np.arange(-STEP, w + 2 * STEP, STEP, dtype=float)
        width = w * (0.5 - 0.38 * gather) + 40
        env = (1 - gather) * 0.65 + (0.35 + 0.65 * gather) * np.exp(-(((xs - tx) / width) ** 2))

        c1, c2, c3 = QColor(style.ORB_BLUE), QColor(style.ORB_VIOLET), QColor(style.ORB_MAGENTA)

        def across(alpha: float) -> QLinearGradient:
            lg = QLinearGradient(QPointF(0, 0), QPointF(w, 0))
            for pos, c in ((0.0, c1), (0.5, c2), (1.0, c3)):
                cc = QColor(c)
                cc.setAlphaF(_clamp(alpha))
                lg.setColorAt(pos, cc)
            return lg

        lines: list[QPainterPath] = []
        for i, (freq, speed, phase, share) in enumerate(LAYERS):
            ys = (
                base
                - amp * share * env * (0.55 + 0.45 * np.sin(xs * freq + t * speed + phase)) * rise
            )
            line = QPainterPath()
            line.moveTo(float(xs[0]), float(ys[0]))
            for x, y in zip(xs[1:].tolist(), ys[1:].tolist(), strict=True):
                line.lineTo(x, y)
            lines.append(line)
            if i % 2:
                continue
            # a soft body under every other wave, fading downwards
            fill = QPainterPath(line)
            fill.lineTo(float(xs[-1]), bottom)
            fill.lineTo(float(xs[0]), bottom)
            fill.closeSubpath()
            body = QLinearGradient(QPointF(0, float(np.min(ys))), QPointF(0, bottom))
            cc = QColor(c2)
            cc.setAlphaF(0.16 * fade)
            body.setColorAt(0.0, cc)
            cc2 = QColor(c3)
            cc2.setAlphaF(0.05 * fade)
            body.setColorAt(1.0, cc2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(body)
            p.drawPath(fill)

        # The glow: thin lines drawn at 1/4 resolution, scaled up smoothly into a soft
        # ~8 px halo (thick antialiased strokes would cost ~6 ms a frame).
        q = 4
        gw, gh = w // q + 2, STRIP // q + 2
        if self._glow is None or self._glow.size() != QSize(gw, gh):
            self._glow = QImage(gw, gh, QImage.Format.Format_ARGB32_Premultiplied)
        self._glow.fill(0)
        gp = QPainter(self._glow)
        gp.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(across(0.55 * fade), 1.0)
        pen.setCosmetic(True)
        gp.setPen(pen)
        for dy in (0.0, 0.7):
            gp.resetTransform()
            gp.translate(0, dy)
            gp.scale(1 / q, 1 / q)
            gp.translate(0, -(h - STRIP))
            for line in lines:
                gp.drawPath(line)
        gp.end()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(QRectF(0, h - STRIP, gw * q, gh * q), self._glow)
        # ...then the bright lines.
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i, line in enumerate(lines):
            # (1 px: Qt's fast antialiased path, ~8x cheaper than thicker strokes)
            p.setPen(QPen(across((1.0 if i % 2 == 0 else 0.85) * fade), 1.0))
            p.drawPath(line)

        # Sparks drifting up from the wave, pulled in under the bar.
        p.setPen(Qt.PenStyle.NoPen)
        for x0, speed, size, ph in self._sparks:
            x = x0 * w
            x += (tx - x) * gather * 0.75
            age = (t * speed / 120 + ph) % 1.0
            y = base - amp * 0.4 * rise - age * STRIP * 0.55
            col = QColor(c1 if x < w * 0.38 else c2 if x < w * 0.66 else c3)
            col.setAlphaF(_clamp((1 - age) * 0.9 * fade * rise))
            p.setBrush(col)
            p.drawEllipse(QPointF(x, y), size, size)

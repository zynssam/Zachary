"""The opening animation, "volcanic": a glowing wave comes up at the bottom of the screen,
erupts (plumes surge up from three vents, embers shoot up, arc down and cool from
white-hot to magenta, violet and blue) and fades as the bar appears ("Hey Zach" or
the hotkey).

A frameless, click-through, never-focused overlay; it lives ~1.3 s and is hidden
again (no timer, no CPU after). The bar itself opens at once (you can type straight
away); it fades in while the wave rises. Off with
``python -m zachary.cli set open_animation false``.

On X11 / Windows the overlay is a strip at the bottom of the screen. On Wayland a
window can't place itself, so the overlay covers the work area (transparent, input
passes through) and draws at its bottom.
"""

from __future__ import annotations

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
    QRadialGradient,
    QScreen,
)
from PySide6.QtWidgets import QWidget

from zachary.ui import style

DURATION_S = 1.3
STRIP = 520  # px of the screen's bottom the animation uses (embers fly this high)
VENTS = ((0.5, 1.0), (0.22, 0.7), (0.8, 0.75))  # (x as share of width, strength)
GRAVITY = 1.9  # x STRIP per s^2: embers arc back down
HOT = "#FFE4FB"  # white-hot, cools into the orb colours
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

    def __init__(self, parent: QWidget | None = None) -> None:
        # (a parent only ties lifetimes together: it's still its own window)
        super().__init__(
            parent,
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
        self._t0 = 0.0
        self._level = 0.0
        self._fixed_t: float | None = None  # (screenshots: draw one moment)
        self._glow: QImage | None = None
        rng = np.random.default_rng(11)
        n = 240  # embers: which vent, when they leave, velocity, size
        self._e_vent = rng.choice(len(VENTS), n, p=[0.5, 0.25, 0.25])
        self._e_t0 = 0.12 + rng.beta(1.6, 3.0, n) * 0.55  # bursts early, a tail later
        self._e_vx = rng.normal(0, 0.28, n)  # x STRIP per s
        self._e_vy = rng.uniform(0.9, 1.75, n) * np.array([v[1] for v in VENTS])[self._e_vent]
        self._e_size = rng.uniform(0.8, 2.3, n)
        self._e_off = rng.normal(0, 0.012, n)  # spread around the vent
        self._timer = QTimer(self)
        self._timer.setInterval(int(1000 / FPS))
        self._timer.timeout.connect(self._tick)

    # -- control ----------------------------------------------------------------------------
    def play(self, screen: QScreen | None = None) -> None:
        screen = screen or QGuiApplication.primaryScreen()
        if not screen:  # (None when headless)
            return
        g = screen.availableGeometry()
        if QGuiApplication.platformName() == "wayland":
            self.setGeometry(g)  # (the compositor places it; covering the area keeps
            # the bottom where it should be)
        else:
            self.setGeometry(QRect(g.left(), g.bottom() - STRIP + 1, g.width(), STRIP))
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
        rise = ease_out(t / 0.22)  # the wave comes up from below the edge
        erupt = ease_out((t - 0.14) / 0.3)  # plumes surge up...
        settle = ease_in_out((t - 0.45) / 0.55)  # ...and fall back
        fade = 1 - ease_in_out((t - 0.75) / (DURATION_S - 0.75))
        if fade <= 0:
            return
        bottom = h + 2
        top = h - STRIP
        base = bottom - STRIP * 0.09 * rise + (1 - rise) * 30
        amp = STRIP * 0.07 * (1 + 0.6 * self._level) * rise
        xs = np.arange(-STEP, w + 2 * STEP, STEP, dtype=float)
        plume = erupt * (1 - settle)  # 0 -> 1 -> 0
        lift = np.zeros_like(xs)
        for vx, strength in VENTS:
            cx = vx * w
            # a pointed cone, not a round hill
            lift += strength * np.exp(-((np.abs(xs - cx) / (w * 0.05 + 36)) ** 1.25))
        lift *= STRIP * 0.42 * plume * (1 + 0.5 * self._level)
        c1, c2, c3 = QColor(style.ORB_BLUE), QColor(style.ORB_VIOLET), QColor(style.ORB_MAGENTA)
        hot = QColor(HOT)

        def across(alpha: float) -> QLinearGradient:
            lg = QLinearGradient(QPointF(0, 0), QPointF(w, 0))
            for pos, c in ((0.0, c1), (0.5, c2), (1.0, c3)):
                cc = QColor(c)
                cc.setAlphaF(_clamp(alpha))
                lg.setColorAt(pos, cc)
            return lg

        # Heat: a glow over each vent while it erupts.
        p.setPen(Qt.PenStyle.NoPen)
        for vx, strength in VENTS:
            cx, r = vx * w, STRIP * (0.25 + 0.45 * plume) * strength
            g = QRadialGradient(QPointF(cx, bottom), r)
            for pos, c, a in ((0.0, hot, 0.22), (0.35, c3, 0.16), (1.0, c2, 0.0)):
                cc = QColor(c)
                cc.setAlphaF(a * (0.35 + plume) * rise * fade)
                g.setColorAt(pos, cc)
            p.setBrush(g)
            p.drawEllipse(QPointF(cx, bottom), r, r)

        lines: list[QPainterPath] = []
        for i, (freq, speed, phase, share) in enumerate(LAYERS):
            ys = base - lift * (1 - 0.18 * i) - amp * share * (
                0.55 + 0.45 * np.sin(xs * freq + t * speed + phase)
            )  # fmt: skip
            line = QPainterPath()
            line.moveTo(float(xs[0]), float(ys[0]))
            for x, y in zip(xs[1:].tolist(), ys[1:].tolist(), strict=True):
                line.lineTo(x, y)
            lines.append(line)
            if i % 2:
                continue
            # the molten body under every other wave
            fill = QPainterPath(line)
            fill.lineTo(float(xs[-1]), bottom)
            fill.lineTo(float(xs[0]), bottom)
            fill.closeSubpath()
            body = QLinearGradient(QPointF(0, float(np.min(ys))), QPointF(0, bottom))
            cc = QColor(c3)
            cc.setAlphaF(0.2 * rise * fade)
            body.setColorAt(0.0, cc)
            cc2 = QColor(c2)
            cc2.setAlphaF(0.04 * fade)
            body.setColorAt(1.0, cc2)
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
        pen = QPen(across(0.6 * rise * fade), 1.0)
        pen.setCosmetic(True)
        gp.setPen(pen)
        for dy in (0.0, 0.7):
            gp.resetTransform()
            gp.translate(0, dy)
            gp.scale(1 / q, 1 / q)
            gp.translate(0, -top)
            for line in lines:
                gp.drawPath(line)
        gp.end()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(QRectF(0, top, gw * q, gh * q), self._glow)
        # ...then the bright lines (1 px: Qt's fast antialiased path).
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i, line in enumerate(lines):
            p.setPen(QPen(across((1.0 if i % 2 == 0 else 0.85) * rise * fade), 1.0))
            p.drawPath(line)

        # Embers: shot up from the vents, falling back under gravity, cooling as they go.
        age = t - self._e_t0
        live = age > 0
        if not live.any():
            return
        ages = age[live]
        vx0 = np.array([v[0] for v in VENTS])[self._e_vent[live]] + self._e_off[live]
        x0 = vx0 * w
        y0 = base - STRIP * 0.42 * plume * 0.6
        vy, vxs = self._e_vy[live] * STRIP, self._e_vx[live] * STRIP
        grav = GRAVITY * STRIP

        def at_age(at: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            return x0 + vxs * at, y0 - (vy * at - 0.5 * grav * at * at)

        ex, ey = at_age(ages)
        trail_x, trail_y = at_age(np.maximum(ages - 0.035, 0))  # (a short trail behind each ember)
        heat = np.clip(1 - ages / 0.7, 0, 1)  # 1 = white-hot, 0 = cooled to blue
        alpha = np.clip(1.15 - ages / 0.85, 0, 1) * fade
        sizes = self._e_size[live] * (0.6 + 0.4 * heat)
        # Batched by colour and brightness: ~16 draw calls instead of ~500.
        tone = np.select([heat > 0.75, heat > 0.45, heat > 0.2], [0, 1, 2], 3)
        level = np.minimum((alpha * 4).astype(int), 3)  # 0..3 -> alpha 0.25..1
        ok = (alpha > 0.01) & (ey < bottom)
        tones = (hot, c3, c2, c1)
        for k in np.unique((tone * 4 + level)[ok]).tolist():
            m = ok & (tone * 4 + level == k)
            col = QColor(tones[k // 4])
            col.setAlphaF((k % 4 + 1) / 4 * fade)
            trails, heads = QPainterPath(), QPainterPath()
            pts = zip(ex[m].tolist(), ey[m].tolist(), trail_x[m].tolist(),
                      trail_y[m].tolist(), sizes[m].tolist(), strict=True)  # fmt: skip
            for x, y, x1, y1, sz in pts:
                trails.moveTo(x1, y1)
                trails.lineTo(x, y)
                heads.addEllipse(QPointF(x, y), sz, sz)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(col, 1.0))
            p.drawPath(trails)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawPath(heads)

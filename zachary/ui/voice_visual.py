"""The voice orb: Zachary's "face" in the bar. A glowing wireframe sphere (blue -> violet
-> magenta) with a halo of particles; its surface ripples with the real audio (your
microphone while you speak, Zach's voice while it replies).

| state     | look                                                                |
|-----------|---------------------------------------------------------------------|
| idle      | still sphere, dim (static: no timer, no CPU)                        |
| wake      | quick pop + glow ("Hey Zach" heard), then listening                 |
| listening | slowly turning; the surface swells and wobbles with your voice      |
| thinking  | spins faster, particles orbit, size pulses                          |
| speaking  | waves travel over the surface, particles pushed out by Zach's voice |
| dictating | listening + a red recording dot                                     |
| error     | amber, shakes once                                                  |

Drawn with QPainter at ~60 fps, only while animating. ``level`` is pulled from a
callable (0..1) so no audio thread ever touches the widget.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable

import numpy as np
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from zachary.ui import style

STATES = ("idle", "wake", "listening", "thinking", "speaking", "dictating", "error")
FPS = 60
ATTACK = 0.45  # level smoothing: rises fast ...
RELEASE = 0.12  # ... falls slowly (reads as "breathing", not flicker)
WAKE_POP_S = 0.18
SHAKE_S = 0.45
TILT = 0.38  # radians: we look at the sphere slightly from above
SPIN = {"idle": 0.0, "wake": 0.9, "listening": 0.5, "thinking": 1.8, "speaking": 0.7,
        "dictating": 0.5, "error": 0.0}  # fmt: skip


def level_from_rms(rms: float) -> float:
    """Microphone / speaker RMS (0..1, speech ~0.01-0.3) -> a 0..1 visual level."""
    if rms <= 0.0:
        return 0.0
    db = 20 * math.log10(max(rms, 1e-5))  # -100..0
    return max(0.0, min(1.0, (db + 50) / 40))  # -50 dB -> 0, -10 dB -> 1


def palette(state: str = "") -> tuple[QColor, QColor, QColor]:
    if state == "error":
        return QColor(style.WARN), QColor("#F97316"), QColor("#EF4444")
    return QColor(style.ORB_BLUE), QColor(style.ORB_VIOLET), QColor(style.ORB_MAGENTA)


def sphere_lines(
    n_lat: int, n_lon: int, t: float, amp: float, travel: float, spin: float
) -> tuple[np.ndarray, np.ndarray]:
    """The wireframe: latitude and longitude polylines of a wobbling, rotated sphere.

    Returns (xy, z): arrays of shape (lines, points, 2) and (lines, points), unit
    radius, y pointing down; z > 0 is the side facing you.
    """
    seg = 36
    lat_th, lat_ph = np.meshgrid(
        np.linspace(0, math.pi, n_lat + 2)[1:-1], np.linspace(0, 2 * math.pi, seg + 1),
        indexing="ij",
    )  # fmt: skip
    lon_ph, lon_th = np.meshgrid(
        np.linspace(0, 2 * math.pi, n_lon, endpoint=False), np.linspace(0, math.pi, seg + 1),
        indexing="ij",
    )  # fmt: skip
    th = np.concatenate([lat_th, lon_th])
    ph = np.concatenate([lat_ph, lon_ph])
    # A smooth "noise" from a few sines (cheap, seamless around the sphere).
    wob = (
        0.6 * np.sin(3 * th + 2 * ph + 1.7 * t)
        + 0.45 * np.sin(5 * ph - 2 * th - 2.3 * t)
        + 0.3 * np.sin(4 * th + 3.1 * t + np.cos(2 * ph))
    ) / 1.35
    pole = np.sin(th)  # (no wobble at the poles: they'd pinch)
    r = 1.0 + pole * (amp * wob + travel * np.sin(7 * th - 9 * t) * np.sin(2 * ph + t))
    x = r * np.sin(th) * np.cos(ph + spin)
    y = r * np.cos(th)
    z = r * np.sin(th) * np.sin(ph + spin)
    ct, st = math.cos(TILT), math.sin(TILT)
    y, z = y * ct - z * st, y * st + z * ct
    return np.stack([x, -y], axis=-1), z


def wire_paths(pts: np.ndarray, front: np.ndarray) -> tuple[QPainterPath, QPainterPath]:
    """Split the polylines into the part behind the sphere and the part in front."""
    back_p, front_p = QPainterPath(), QPainterPath()
    for line, fl in zip(pts.tolist(), front.tolist(), strict=True):
        prev: tuple[float, float, bool] | None = None
        for (x, y), f in zip(line, fl, strict=True):
            path = front_p if f else back_p
            if prev is None:
                path.moveTo(x, y)
            elif prev[2] != f:
                path.moveTo(prev[0], prev[1])  # (the segment crossing the edge)
                path.lineTo(x, y)
            else:
                path.lineTo(x, y)
            prev = (x, y, f)
    return back_p, front_p


class VoiceOrb(QWidget):
    def __init__(self, parent: QWidget | None = None, size: int = 56) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.state = "idle"
        self.level = 0.0  # smoothed 0..1
        self.level_source: Callable[[], float] | None = None
        self.reduce_motion = False
        self._t0 = time.monotonic()
        self._changed_at = self._t0
        self._last_tick = self._t0
        self._spin = 0.6
        self._ripples: list[float] = []  # start times of speaking pulses
        rnd = random.Random(7)
        # Particles: angle, distance (x radius), size, phase.
        self._particles = [
            (rnd.uniform(0, 2 * math.pi), rnd.uniform(1.08, 1.5), rnd.uniform(0.35, 0.8),
             rnd.uniform(0, 2 * math.pi))
            for _ in range(max(50, min(500, size * 2)))
        ]  # fmt: skip
        self._n_lat = max(9, min(34, size // 6))  # finer mesh on bigger orbs
        self._n_lon = int(self._n_lat * 1.6)
        self._idle_px: QPixmap | None = None
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
            self._last_tick = time.monotonic()
            self._timer.start()
        self.update()

    def animating(self) -> bool:
        return self._timer.isActive()

    def _tick(self) -> None:
        now = time.monotonic()
        dt = min(0.1, now - self._last_tick)
        self._last_tick = now
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
        self._spin += dt * (SPIN.get(self.state, 0.5) + 1.2 * self.level)
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
        if self.state == "idle":  # still: draw it once, then reuse the picture
            dpr = self.devicePixelRatioF()
            if self._idle_px is None or self._idle_px.devicePixelRatio() != dpr:
                px = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
                px.setDevicePixelRatio(dpr)
                px.fill(Qt.GlobalColor.transparent)
                pp = QPainter(px)
                self._draw(pp)
                pp.end()
                self._idle_px = px
            p.drawPixmap(0, 0, self._idle_px)
        else:
            self._draw(p)
        p.end()

    def _draw(self, p: QPainter) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        now = time.monotonic()
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        since = now - self._changed_at
        if self.state == "error" and since < SHAKE_S:
            cx += math.sin(since * 45) * 3 * (1 - since / SHAKE_S)
        t = now - self._t0 if self.state != "idle" else 0.8
        scale = min(w, h) / 56
        rad = min(w, h) * 0.30 * (1.0 + 0.18 * self.level)
        if self.state == "wake":
            rad *= 1.0 + 0.3 * math.sin(min(1.0, since / WAKE_POP_S) * math.pi)
        if self.state == "thinking":
            rad *= 1.0 + 0.04 * math.sin(t * 6)
        dim = self.state == "idle"
        c1, c2, c3 = palette(self.state)
        center = QPointF(cx, cy)

        # Soft glow behind.
        edge = min(w, h) / 2 - 1  # nothing may reach the widget's edge (it'd show a square)
        gr = min(rad * 1.9, edge)
        glow = QRadialGradient(center, gr)
        a = 0.16 if dim else 0.28 + 0.3 * self.level
        for pos, alpha in ((0.0, a), (0.55, a * 0.45), (1.0, 0.0)):
            g = QColor(c2)
            g.setAlphaF(alpha)
            glow.setColorAt(pos, g)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(center, gr, gr)

        # Colour runs blue (top-left) -> violet -> magenta (bottom-right).
        def grad(alpha: float) -> QLinearGradient:
            lg = QLinearGradient(QPointF(cx - rad, cy - rad), QPointF(cx + rad, cy + rad))
            for pos, c in ((0.0, c1), (0.5, c2), (1.0, c3)):
                cc = QColor(c)
                cc.setAlphaF(alpha)
                lg.setColorAt(pos, cc)
            return lg

        # Particles around the sphere (pushed out while Zach speaks).
        push = max(((1 - (now - s) / 1.2) * 0.25 for s in self._ripples), default=0.0)
        orbit = t * (1.4 if self.state == "thinking" else 0.35)
        for ang, dist, size, ph in self._particles:
            ang2 = ang + orbit * (0.6 + 0.4 * math.sin(ph))
            d = rad * (dist + 0.06 * math.sin(t * 2 + ph) + push * (dist - 0.9) + 0.15 * self.level)
            d = min(d, edge - 1.5)
            px, py = cx + d * math.cos(ang2), cy + d * math.sin(ang2)
            u = ((px - cx) + (py - cy)) / (4 * rad) + 0.5  # position along the gradient
            col = QColor(c1 if u < 0.4 else c2 if u < 0.62 else c3)
            col.setAlphaF(max(0.0, min(1.0, (0.4 if dim else 0.6 + 0.4 * self.level)
                                       * (1.55 - dist) / 0.45)))  # fmt: skip
            p.setBrush(col)
            ps = size * (scale**0.5)  # (dots stay fine on big orbs)
            p.drawEllipse(QPointF(px, py), ps, ps)

        # The wireframe sphere: back side faint, front side bright.
        amp = (0.05 if dim else 0.08) + 0.22 * self.level
        travel = 0.12 * self.level if self.state == "speaking" else 0.0
        xy, z = sphere_lines(self._n_lat, self._n_lon, t, amp, travel, self._spin)
        back, front = wire_paths(xy * rad + np.array([cx, cy]), z > 0)
        p.setBrush(Qt.BrushStyle.NoBrush)
        lw = max(0.6, 0.8 * scale**0.4)
        p.setPen(QPen(grad(0.22 if dim else 0.32), lw))
        p.drawPath(back)
        p.setPen(QPen(grad(0.62 if dim else 0.95), lw))
        p.drawPath(front)

        # A bright rim, strongest bottom-right, like light catching the edge.
        rim = QRadialGradient(QPointF(cx - rad * 0.25, cy - rad * 0.25), rad * 1.35)
        ra = 0.35 if dim else 0.55 + 0.35 * self.level
        for pos, alpha in ((0.0, 0.0), (0.78, 0.0), (0.93, ra), (1.0, 0.0)):
            rc = QColor(c3)
            rc.setAlphaF(alpha)
            rim.setColorAt(pos, rc)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(rim)
        p.drawEllipse(center, rad * 1.08, rad * 1.08)

        # Dictating: a small red recording dot.
        if self.state == "dictating":
            dot = QColor(style.REC)
            dot.setAlphaF(1.0 if int(t * 2) % 2 == 0 else 0.45)
            p.setBrush(dot)
            p.drawEllipse(QPointF(w - 6, 6), 4, 4)

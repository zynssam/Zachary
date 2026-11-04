"""Tray icon with Open / Pause indexing / Quit.

Only shown where the desktop has a tray (Windows, KDE, most Linux desktops; GNOME
needs the AppIndicator extension). Without a tray, the hotkey and CLI still work.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from zachary.ui import style


def app_icon() -> QIcon:
    """The app icon: a soft violet dot with a halo, drawn in code (like the voice dot)."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(style.ACCENT)
    halo = QRadialGradient(QPointF(32, 32), 30)
    for pos, a in ((0.0, 0.45), (1.0, 0.0)):
        h = QColor(c)
        h.setAlphaF(a)
        halo.setColorAt(pos, h)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(halo)
    p.drawEllipse(QPointF(32, 32), 30, 30)
    core = QRadialGradient(QPointF(27, 26), 22)
    core.setColorAt(0.0, c.lighter(150))
    core.setColorAt(1.0, c)
    p.setBrush(core)
    p.drawEllipse(QPointF(32, 32), 15, 15)
    p.end()
    return QIcon(pm)


class Tray(QSystemTrayIcon):
    def __init__(
        self,
        on_open: Callable[[], None],
        on_pause: Callable[[bool], None],
        on_quit: Callable[[], None],
        paused: bool,
        parent: QWidget | None = None,
        on_settings: Callable[[], None] | None = None,
        on_exclude: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(app_icon(), parent)
        self.setToolTip("Zachary")
        menu = QMenu()
        menu.addAction("Open quick bar", on_open)
        self.pause_action = QAction("Pause indexing", menu, checkable=True)
        self.pause_action.setChecked(paused)
        self.pause_action.toggled.connect(on_pause)
        menu.addAction(self.pause_action)
        if on_exclude is not None:
            menu.addAction("Exclude a folder…", on_exclude)
        if on_settings is not None:
            menu.addAction("Settings…", on_settings)
        menu.addSeparator()
        menu.addAction("Quit", on_quit)
        self._menu = menu  # keep a reference
        self.setContextMenu(menu)
        self.activated.connect(
            lambda reason: on_open() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

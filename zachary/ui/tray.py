"""Tray icon with Open / Pause indexing / Quit.

Only shown where the desktop has a tray (Windows, KDE, most Linux desktops; GNOME
needs the AppIndicator extension). Without a tray, the hotkey and CLI still work.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget


def app_icon() -> QIcon:
    """A simple drawn icon (no asset files): a rounded square with a "Z"."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#3b82f6"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    f = QFont()
    f.setBold(True)
    f.setPixelSize(40)
    p.setFont(f)
    p.setPen(QColor("white"))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "Z")
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
    ) -> None:
        super().__init__(app_icon(), parent)
        self.setToolTip("Zachary")
        menu = QMenu()
        menu.addAction("Open quick bar", on_open)
        self.pause_action = QAction("Pause indexing", menu, checkable=True)
        self.pause_action.setChecked(paused)
        self.pause_action.toggled.connect(on_pause)
        menu.addAction(self.pause_action)
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

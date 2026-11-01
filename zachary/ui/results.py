"""Clickable result list used for files, projects and recent items.

Each row: a kind badge, the name, how long ago, the folder, and why it matched.
Double-click / Enter opens; the context menu also offers "Show in folder" and
"Copy path". Painting is cheap (no I/O); data comes from the engine.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from zachary.brain.context import Item

ItemRole = Qt.ItemDataRole.UserRole + 1

BADGES: dict[str, tuple[str, str]] = {
    "document": ("D", "#3b82f6"),
    "code": ("C", "#10b981"),
    "image": ("I", "#f59e0b"),
    "video": ("V", "#ef4444"),
    "audio": ("A", "#8b5cf6"),
    "archive": ("Z", "#64748b"),
    "binary": ("B", "#64748b"),
    "dir": ("F", "#0ea5e9"),
    "project": ("P", "#ec4899"),
    "other": ("·", "#94a3b8"),
}

_Index = QModelIndex | QPersistentModelIndex


def ago(ts: float, now: float | None = None) -> str:
    if ts <= 0:
        return ""
    d = max(0.0, (now or time.time()) - ts)
    if d < 90:
        return "now"
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{int(d // 3600)}h"
    if d < 86400 * 365:
        return f"{int(d // 86400)}d"
    return f"{int(d // (86400 * 365))}y"


def short_path(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def short_folder(path: str) -> str:
    return short_path(os.path.dirname(path))


class ResultsModel(QAbstractListModel):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[Item] = []

    def set_items(self, items: list[Item]) -> None:
        self.beginResetModel()
        self.items = list(items)
        self.endResetModel()

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.items)

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self.items):
            return None
        it = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return it.name
        if role == Qt.ItemDataRole.ToolTipRole:
            return it.path
        if role == ItemRole:
            return it
        return None


class ResultsDelegate(QStyledItemDelegate):
    ROW_H = 54

    def sizeHint(self, option: QStyleOptionViewItem, index: _Index) -> QSize:
        return QSize(option.rect.width(), self.ROW_H)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: _Index) -> None:
        it: Item | None = index.data(ItemRole)
        if it is None:
            return
        p.save()
        r: QRect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            p.fillRect(r, QColor(59, 130, 246, 46))
        # badge
        letter, color = BADGES.get(it.kind, BADGES["other"])
        badge = QRect(r.left() + 12, r.top() + 13, 28, 28)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(badge, 7, 7)
        p.setPen(QColor("white"))
        bf = QFont(option.font)
        bf.setBold(True)
        p.setFont(bf)
        p.drawText(badge, Qt.AlignmentFlag.AlignCenter, letter)
        # text
        left = badge.right() + 12
        right = r.right() - 12
        when = ago(it.ts)
        nf = QFont(option.font)
        nf.setPointSizeF(nf.pointSizeF() * 1.05)
        nf.setBold(True)
        fm = QFontMetrics(nf)
        tf = QFont(option.font)
        tf.setPointSizeF(tf.pointSizeF() * 0.85)
        tfm = QFontMetrics(tf)
        when_w = tfm.horizontalAdvance(when) + 4 if when else 0
        text_color = option.palette.text().color()
        dim = QColor(text_color)
        dim.setAlpha(150)
        p.setFont(nf)
        p.setPen(text_color)
        name = fm.elidedText(it.name, Qt.TextElideMode.ElideMiddle, right - left - when_w - 8)
        p.drawText(QRect(left, r.top() + 7, right - left, fm.height()), 0, name)
        p.setFont(tf)
        p.setPen(dim)
        if when:
            p.drawText(QRect(right - when_w, r.top() + 9, when_w, tfm.height()),
                       int(Qt.AlignmentFlag.AlignRight), when)  # fmt: skip
        sub = short_path(it.path) if it.kind == "project" else short_folder(it.path)
        extra = " · ".join([*it.why[:2], it.detail] if it.detail else it.why[:2])
        line2 = f"{sub}   {extra}" if extra else sub
        line2 = tfm.elidedText(line2, Qt.TextElideMode.ElideMiddle, right - left)
        p.drawText(QRect(left, r.top() + 9 + fm.height(), right - left, tfm.height()), 0, line2)
        p.restore()


class ResultsView(QListView):
    """List of results. Emits ``chosen(row, action)``; ``painted`` after each paint."""

    chosen = Signal(int, str)  # row, "open" | "reveal" | "related"
    painted = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.results = ResultsModel()
        self.setModel(self.results)
        self.setItemDelegate(ResultsDelegate(self))
        self.setUniformItemSizes(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # typing stays in the search box
        self.setFrameShape(QListView.Shape.NoFrame)
        # One click opens (a double-click would otherwise open twice).
        self.clicked.connect(lambda i: self.chosen.emit(i.row(), "open"))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.menu_open = False

    def set_items(self, items: list[Item], selected: int | None = None) -> None:
        self.results.set_items(items)
        if selected is not None and 0 <= selected < len(items):
            self.select_row(selected)

    def select_row(self, row: int) -> None:
        idx = self.results.index(row, 0)
        if idx.isValid():
            self.setCurrentIndex(idx)
            self.scrollTo(idx)

    def selected_row(self) -> int | None:
        idx = self.currentIndex()
        return idx.row() if idx.isValid() else None

    def step(self, delta: int) -> None:
        n = self.results.rowCount()
        if not n:
            return
        cur = self.selected_row()
        row = 0 if cur is None else max(0, min(n - 1, cur + delta))
        self.select_row(row)

    def _menu(self, pos: Any) -> None:
        idx = self.indexAt(pos)
        if not idx.isValid():
            return
        it: Item = idx.data(ItemRole)
        m = QMenu(self)
        m.addAction("Open", lambda: self.chosen.emit(idx.row(), "open"))
        m.addAction("Show in folder", lambda: self.chosen.emit(idx.row(), "reveal"))
        m.addAction("Related files", lambda: self.chosen.emit(idx.row(), "related"))
        m.addAction("Copy path", lambda: QGuiApplication.clipboard().setText(it.path))
        self.menu_open = True
        try:
            m.exec(self.viewport().mapToGlobal(pos))
        finally:
            self.menu_open = False

    def paintEvent(self, e: QPaintEvent) -> None:
        super().paintEvent(e)
        self.painted.emit()

"""Clickable result list used for files, projects and recent items.

Each row: a file-type icon, the name, how long ago, the folder, and why it matched
(as small pills). Colours and icons come from ``ui/style.py``.
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
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPaintEvent,
    QPixmap,
)
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
from zachary.ui import style

ItemRole = Qt.ItemDataRole.UserRole + 1

BADGES = style.BADGES  # (kept for importers)

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
    ROW_H = style.ROW_HEIGHT

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = style.theme()

    def sizeHint(self, option: QStyleOptionViewItem, index: _Index) -> QSize:
        return QSize(option.rect.width(), self.ROW_H)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: _Index) -> None:
        it: Item | None = index.data(ItemRole)
        if it is None:
            return
        t = self.theme
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r: QRect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        card = QRectF(r).adjusted(4, 2, -4, -2)
        if selected:
            sel = QColor(*style.ACCENT_RGBA, 52 if t.dark else 36)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(sel)
            p.drawRoundedRect(card, 10, 10)
            p.setBrush(QColor(style.ACCENT))
            p.drawRoundedRect(QRectF(card.left(), card.top() + 10, 3, card.height() - 20), 1.5, 1.5)
        elif hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.surface))
            p.drawRoundedRect(card, 10, 10)
        # icon tile
        name_icon, color = style.icon_for(it.kind, it.path)
        tile = QRectF(r.left() + 14, r.top() + 11, 32, 32)
        tint = QColor(color)
        tint.setAlpha(46 if t.dark else 34)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tint)
        p.drawRoundedRect(tile, 9, 9)
        dpr = p.device().devicePixelRatioF() if p.device() is not None else 1.0
        pix: QPixmap = style.icon_pixmap(name_icon, color, 18, dpr)
        p.drawPixmap(int(tile.left() + 7), int(tile.top() + 7), pix)
        # text
        left = int(tile.right()) + 12
        right = r.right() - 14
        when = ago(it.ts)
        base = QFont(option.font)
        if self.family:
            base.setFamily(self.family)
        nf = QFont(base)
        nf.setPointSizeF(base.pointSizeF() * 1.05)
        nf.setWeight(QFont.Weight.DemiBold)
        fm = QFontMetrics(nf)
        tf = QFont(base)
        tf.setPointSizeF(base.pointSizeF() * 0.86)
        tfm = QFontMetrics(tf)
        when_w = tfm.horizontalAdvance(when) + 4 if when else 0
        p.setFont(nf)
        p.setPen(QColor(t.text))
        name = fm.elidedText(it.name, Qt.TextElideMode.ElideMiddle, right - left - when_w - 8)
        p.drawText(QRect(left, r.top() + 8, right - left, fm.height()), 0, name)
        p.setFont(tf)
        p.setPen(QColor(t.dim))
        if when:
            p.drawText(QRect(right - when_w, r.top() + 10, when_w, tfm.height()),
                       int(Qt.AlignmentFlag.AlignRight), when)  # fmt: skip
        if it.kind in ("topic", "session", "app"):
            sub = ""  # not a place on disk: the pills say what it is
        else:
            sub = short_path(it.path) if it.kind == "project" else short_folder(it.path)
        y2 = r.top() + 11 + fm.height()
        x = left
        if sub:
            sub_w = min(
                tfm.horizontalAdvance(sub) + 6, int((right - left) * 0.55)
            )  # (+6: rounding)
            p.drawText(QRect(x, y2, sub_w, tfm.height()), 0,
                       tfm.elidedText(sub, Qt.TextElideMode.ElideMiddle, sub_w))  # fmt: skip
            x += sub_w + 10
        # "why it matched" as small pills
        pills = [w for w in ([*it.why[:2], it.detail] if it.detail else it.why[:2]) if w]
        for w in pills:
            if x > right - 40:
                break
            text = tfm.elidedText(w, Qt.TextElideMode.ElideRight, min(260, right - x - 14))
            pw = tfm.horizontalAdvance(text) + 14
            pill = QRectF(x, y2 - 1, pw, tfm.height() + 2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.chip))
            p.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
            p.setPen(QColor(t.dim))
            p.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), text)
            x += int(pw) + 6
        p.restore()

    @property
    def family(self) -> str:
        return style.load_fonts()


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
        self.setMouseTracking(True)  # hover highlight
        # One click opens (a double-click would otherwise open twice).
        self.clicked.connect(lambda i: self.chosen.emit(i.row(), "open"))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.menu_open = False

    def set_theme(self, t: style.Theme) -> None:
        delegate = self.itemDelegate()
        if isinstance(delegate, ResultsDelegate):
            delegate.theme = t
        self.setStyleSheet("QListView { background: transparent; border: none; }")
        self.viewport().update()

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

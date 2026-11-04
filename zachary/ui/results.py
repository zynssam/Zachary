"""Clickable result list used for files, projects and recent items.

Each row: a file-type icon, the name, how long ago, the folder, and why it matched
(faint pills). The selection is a soft pill that glides between rows; rows fade in
when the list changes (look only, from ``ui/style.py``).
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
    QEasingCurve,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication, QPainter, QPaintEvent
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


FADE_MS = 160  # (look) each row fades in...
STAGGER_MS = 15  # ...a little after the one above
GLIDE_MS = 120  # the selection pill moves to the new row


class ResultsDelegate(QStyledItemDelegate):
    ROW_H = 54

    def __init__(self, view: ResultsView) -> None:
        super().__init__(view)
        self.view = view

    def sizeHint(self, option: QStyleOptionViewItem, index: _Index) -> QSize:
        return QSize(option.rect.width(), self.ROW_H)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: _Index) -> None:
        it: Item | None = index.data(ItemRole)
        if it is None:
            return
        v = self.view
        t = v.theme
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(v.row_alpha(index.row()))
        r: QRect = option.rect
        if option.state & QStyle.StateFlag.State_MouseOver and not (
            option.state & QStyle.StateFlag.State_Selected
        ):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(*t.hover))
            p.drawRoundedRect(QRectF(r).adjusted(2, 2, -2, -2), 10, 10)
        # icon (thin line drawing in a faint tile)
        name_icon, color = style.icon_for(it.kind, it.path)
        tile = QRectF(r.left() + 12, r.top() + 13, 28, 28)
        tint = QColor(color)
        tint.setAlpha(34)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tint)
        p.drawRoundedRect(tile, 8, 8)
        dpr = v.devicePixelRatioF()
        p.drawPixmap(
            int(tile.left()) + 5, int(tile.top()) + 5, style.icon_pixmap(name_icon, color, 18, dpr)
        )
        # text
        left = int(tile.right()) + 12
        right = r.right() - 12
        when = ago(it.ts)
        nf = style.font(option.font, 1.04)
        fm = QFontMetrics(nf)
        tf = style.font(option.font, 0.86)
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
        if it.kind in ("topic", "session"):
            sub = ""  # not a place on disk: the "why" pills say what it is
        else:
            sub = short_path(it.path) if it.kind == "project" else short_folder(it.path)
        y2 = r.top() + 10 + fm.height()
        x = left
        if sub:
            sub_w = min(tfm.horizontalAdvance(sub) + 6, int((right - left) * 0.55))
            p.drawText(QRect(x, y2, sub_w, tfm.height()), 0,
                       tfm.elidedText(sub, Qt.TextElideMode.ElideMiddle, sub_w))  # fmt: skip
            x += sub_w + 8
        for why in [w for w in ([*it.why[:2], it.detail] if it.detail else it.why[:2]) if w]:
            if x > right - 40:
                break
            text = tfm.elidedText(why, Qt.TextElideMode.ElideRight, min(240, right - x - 14))
            pw = tfm.horizontalAdvance(text) + 14
            pill = QRectF(x, y2 - 1, pw, tfm.height() + 2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(*t.hover))
            p.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
            p.setPen(QColor(t.dim))
            p.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), text)
            x += int(pw) + 6
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
        # (look) theme, hover, row fade-in and the gliding selection pill
        self.theme = style.theme()
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setStyleSheet("QListView { background: transparent; }")
        self._shown_at = 0.0
        self._fade = QVariantAnimation(self)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.valueChanged.connect(lambda _v: self.viewport().update())
        self._pill_y = -1.0  # where the selection pill is drawn (glides to the row)
        self._glide = QVariantAnimation(self)
        self._glide.setDuration(GLIDE_MS)
        self._glide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._glide.valueChanged.connect(self._glide_to)
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

    def set_theme(self, t: style.Theme) -> None:
        self.theme = t
        self.viewport().update()

    def row_alpha(self, row: int) -> float:
        """(look) rows fade in one after another when the list changes."""
        if self._fade.state() != QVariantAnimation.State.Running:
            return 1.0
        ms = (time.monotonic() - self._shown_at) * 1e3 - row * STAGGER_MS
        return max(0.0, min(1.0, ms / FADE_MS))

    def _glide_to(self, v: object) -> None:
        self._pill_y = float(v)  # type: ignore[arg-type]
        self.viewport().update()

    def currentChanged(self, current: QModelIndex, previous: QModelIndex) -> None:  # type: ignore[override]
        super().currentChanged(current, previous)
        if not current.isValid():
            self._pill_y = -1.0
            return
        y = float(self.visualRect(current).top())
        if self._pill_y < 0 or not previous.isValid():
            self._pill_y = y  # (first selection: no glide)
            self.viewport().update()
            return
        self._glide.stop()
        self._glide.setStartValue(self._pill_y)
        self._glide.setEndValue(y)
        self._glide.start()

    def set_items(self, items: list[Item], selected: int | None = None) -> None:
        if items and [i.path for i in items] != [i.path for i in self.results.items]:
            self._shown_at = time.monotonic()
            self._fade.stop()
            self._fade.setDuration(FADE_MS + STAGGER_MS * min(len(items), 8))
            self._fade.start()
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
        idx = self.currentIndex()
        if idx.isValid():  # (look) the selection pill, under the rows
            vp = QPainter(self.viewport())
            vp.setRenderHint(QPainter.RenderHint.Antialiasing)
            r = QRectF(self.visualRect(idx))
            if self._glide.state() == QVariantAnimation.State.Running and self._pill_y >= 0:
                r.moveTop(self._pill_y)
            vp.setPen(Qt.PenStyle.NoPen)
            vp.setBrush(QColor(*self.theme.select))
            vp.drawRoundedRect(r.adjusted(2, 2, -2, -2), 10, 10)
            accent = QColor(style.ACCENT)
            vp.setBrush(accent)
            vp.drawRoundedRect(QRectF(r.left() + 3, r.top() + 15, 3, r.height() - 30), 1.5, 1.5)
            vp.end()
        super().paintEvent(e)
        self.painted.emit()

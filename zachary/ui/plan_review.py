"""The plan review window: approve, edit or reject a clean/organize plan, item by item.

Nothing changes on disk until **Approve** is clicked; then only the ticked items run
(in the engine's worker thread), and the window shows what happened with a one-click
**Undo**. Untick an item (or a whole group) to leave it alone; double-click a move's
"Move to" cell to change where it goes. Styles come from ``ui/style.py``.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zachary.actions import planner
from zachary.actions.planner import Plan
from zachary.brain.handler import Reply
from zachary.ui import style

ITEM_ROLE = Qt.ItemDataRole.UserRole
COLS = ["Name", "Where", "Size", "Why", "Move to"]


class PlanReview(QWidget):
    # object, not dict: Qt turns a dict into a QVariantMap, which silently drops int keys
    # (the user's untick would be lost and the plan's original selection used instead).
    approved = Signal(int, object, object)  # plan id, {item id: selected}, {item id: dst}
    rejected = Signal(int)
    undo_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Zachary: review changes")
        self.resize(*style.PLAN_WINDOW_SIZE)
        self.plan: Plan | None = None
        self.title = QLabel(self)
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.3)
        f.setBold(True)
        self.title.setFont(f)
        self.note = QLabel(self)
        self.note.setWordWrap(True)
        # Summary header: one big number + a card per category.
        self.theme = style.theme()
        style.load_fonts()
        self.big = QLabel(self)
        bf = self.big.font()
        bf.setPointSizeF(bf.pointSizeF() * 2.2)
        bf.setBold(True)
        self.big.setFont(bf)
        self.big.setStyleSheet(f"color: {style.ACCENT};")
        self.cards = QHBoxLayout()
        self.cards.setSpacing(10)
        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(len(COLS))
        self.tree.setHeaderLabels(COLS)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 220)
        self.tree.itemChanged.connect(self._changed)
        self.tree.itemDoubleClicked.connect(self._edit_dst)
        self.result = QLabel(self)
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result.setVisible(False)
        self.approve_btn = QPushButton("Approve", self)
        self.approve_btn.setDefault(True)
        self.approve_btn.setStyleSheet(style.PRIMARY_BUTTON_CSS)
        self.reject_btn = QPushButton("Reject", self)
        self.undo_btn = QPushButton("Undo", self)
        self.undo_btn.setVisible(False)
        self.close_btn = QPushButton("Close", self)
        self.approve_btn.clicked.connect(self._approve)
        self.reject_btn.clicked.connect(self._reject)
        self.undo_btn.clicked.connect(self._undo)
        self.close_btn.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.undo_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.close_btn)
        buttons.addWidget(self.reject_btn)
        buttons.addWidget(self.approve_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addWidget(self.big)
        lay.addLayout(self.cards)
        lay.addWidget(self.note)
        lay.addWidget(self.tree, 1)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        self.tree.setStyleSheet(style.tree_css(self.theme))
        self.tree.setAlternatingRowColors(True)
        for b in (self.reject_btn, self.undo_btn, self.close_btn):
            b.setStyleSheet(style.secondary_button_css(self.theme))
        lay.addWidget(self.result)
        lay.addLayout(buttons)
        self._filling = False

    # -- showing a plan -------------------------------------------------------------------
    def show_plan(self, plan: Plan) -> None:
        self.plan = plan
        self.title.setText(plan.title)
        self.note.setText(
            f"{plan.note} Nothing changes until you click Approve. Untick anything to keep "
            "it. Removed files go to the trash, and Undo puts everything back."
        )
        self.result.setVisible(False)
        self.undo_btn.setVisible(False)
        self.approve_btn.setVisible(True)
        self.reject_btn.setVisible(True)
        self.reject_btn.setEnabled(True)  # (a previous plan may have disabled them)
        self.undo_btn.setEnabled(True)
        self.tree.setColumnHidden(4, plan.kind != "organize")
        self._fill_summary(plan)
        self._filling = True
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for it in plan.items:
            g = groups.get(it.category)
            if g is None:
                g = QTreeWidgetItem(self.tree, [planner.LABELS[it.category]])
                icon, color = style.CATEGORY_ICONS.get(it.category, ("file", style.ACCENT))
                g.setIcon(0, QIcon(style.icon_pixmap(icon, color, 16, self.devicePixelRatioF())))
                gf = g.font(0)
                gf.setBold(True)
                g.setFont(0, gf)
                g.setFlags(g.flags() | Qt.ItemFlag.ItemIsUserCheckable
                           | Qt.ItemFlag.ItemIsAutoTristate)  # fmt: skip
                g.setExpanded(it.category != "large" or len(plan.items) < 30)
                groups[it.category] = g
            row = QTreeWidgetItem(g, [
                os.path.basename(it.src.rstrip("/\\")) + ("/" if it.is_dir else ""),
                planner._short(os.path.dirname(it.src)),
                planner.human(it.size) if it.size else "",
                it.reason,
                planner._short(it.dst) if it.dst else "",
            ])  # fmt: skip
            fi, fc = style.icon_for("dir" if it.is_dir else "", it.src)
            row.setIcon(0, QIcon(style.icon_pixmap(fi, fc, 16, self.devicePixelRatioF())))
            row.setData(0, ITEM_ROLE, it.id)
            row.setToolTip(0, it.src)
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(0, Qt.CheckState.Checked if it.selected else Qt.CheckState.Unchecked)
        self._filling = False
        self._update_button()
        self.show()
        self.raise_()
        self.activateWindow()

    def _fill_summary(self, plan: Plan) -> None:
        while self.cards.count():
            item = self.cards.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()
        cats: dict[str, tuple[int, int]] = {}
        for it in plan.items:
            n, size = cats.get(it.category, (0, 0))
            cats[it.category] = (n + 1, size + it.size)
        total = sum(it.size for it in plan.items if it.op == "trash")
        n_all = len(plan.items)
        if plan.kind == "organize":
            self.big.setText(f"{n_all} file{'s' if n_all != 1 else ''} to sort")
        else:
            self.big.setText(f"{planner.human(total)} can be freed" if total
                             else f"{n_all} item{'s' if n_all != 1 else ''} to tidy")  # fmt: skip
        for cat, (n, size) in cats.items():
            self.cards.addWidget(self._card(cat, n, size))
        self.cards.addStretch(1)

    def _card(self, cat: str, n: int, size: int) -> QFrame:
        card = QFrame(self)
        card.setObjectName("card")
        card.setStyleSheet(style.card_css(self.theme))
        icon, color = style.CATEGORY_ICONS.get(cat, ("file", style.ACCENT))
        pic = QLabel(card)
        pic.setPixmap(style.icon_pixmap(icon, color, 22, self.devicePixelRatioF()))
        head = QLabel(f"<b>{n}</b>  {planner.LABELS.get(cat, cat)}", card)
        sub = QLabel(planner.human(size) if size else "", card)
        sub.setStyleSheet(f"color: {self.theme.dim};")
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(head)
        text.addWidget(sub)
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 8, 14, 8)
        row.addWidget(pic)
        row.addLayout(text)
        return card

    def _rows(self) -> list[QTreeWidgetItem]:
        out: list[QTreeWidgetItem] = []
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            if g is None:
                continue
            for j in range(g.childCount()):
                c = g.child(j)
                if c is not None:
                    out.append(c)
        return out

    def selection(self) -> dict[int, bool]:
        return {int(r.data(0, ITEM_ROLE)): r.checkState(0) == Qt.CheckState.Checked
                for r in self._rows()}  # fmt: skip

    def destinations(self) -> dict[int, str]:
        if self.plan is None:
            return {}
        original = {i.id: planner._short(i.dst) for i in self.plan.items if i.dst}
        out = {}
        for r in self._rows():
            iid = int(r.data(0, ITEM_ROLE))
            text = r.text(4).strip()
            if iid in original and text and text != original[iid]:
                out[iid] = os.path.expanduser(text)
        return out

    def _changed(self, _item: QTreeWidgetItem, _col: int) -> None:
        if not self._filling:
            self._update_button()

    def _update_button(self) -> None:
        if self.plan is None:
            return
        sel = self.selection()
        n = sum(sel.values())
        by_id = {i.id: i for i in self.plan.items}
        size = sum(by_id[k].size for k, v in sel.items() if v and by_id[k].op == "trash")
        if self.plan.kind == "organize":
            self.approve_btn.setText(f"Move {n} file{'s' if n != 1 else ''}")
        else:
            extra = f" ({planner.human(size)})" if size else ""
            self.approve_btn.setText(f"Move {n} to the trash{extra}")
        self.approve_btn.setEnabled(n > 0)

    def _edit_dst(self, item: QTreeWidgetItem, col: int) -> None:
        if col == 4 and item.parent() is not None and item.text(4):
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.tree.editItem(item, 4)

    # -- buttons ------------------------------------------------------------------------------
    def _approve(self) -> None:
        if self.plan is None:
            return
        self.approve_btn.setEnabled(False)
        self.reject_btn.setEnabled(False)
        self.result.setText("Working…")
        self.result.setVisible(True)
        self.approved.emit(self.plan.id, self.selection(), self.destinations())

    def _reject(self) -> None:
        if self.plan is not None:
            self.rejected.emit(self.plan.id)
        self.close()

    def _undo(self) -> None:
        if self.plan is not None:
            self.undo_btn.setEnabled(False)
            self.result.setText("Undoing…")
            self.undo_requested.emit(self.plan.id)

    def show_result(self, reply: Reply) -> None:
        """After Approve / Undo: what happened, and the Undo button."""
        text = reply.say + (f"\n\n{reply.detail}" if reply.detail else "")
        self.result.setText(text)
        self.result.setVisible(True)
        self.approve_btn.setVisible(False)
        self.reject_btn.setVisible(False)
        is_undo = reply.intent.kind == "undo_plan"
        self.undo_btn.setVisible(not is_undo)
        self.undo_btn.setEnabled(not is_undo)
        for r in self._rows():
            r.setFlags(r.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        if reply.plan is not None and not is_undo:
            results = {i.id: i.result for i in reply.plan.items}
            for r in self._rows():
                res = results.get(int(r.data(0, ITEM_ROLE)), "")
                if res and res != "done":
                    r.setToolTip(3, res)
                    r.setText(3, res)

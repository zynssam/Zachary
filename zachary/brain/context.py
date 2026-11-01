"""Conversation state for follow-ups ("open the second one", "the older one",
"show its folder"). Pure Python; one instance per assistant session.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

CONTEXT_TTL_S = 15 * 60  # follow-ups refer to results from the last 15 minutes


@dataclass(slots=True)
class Item:
    """Something shown to the user: a file, a folder, or a project folder."""

    file_id: int
    path: str
    name: str
    kind: str  # file kinds from indexer/kinds.py, or "dir" / "project"
    is_dir: bool
    ts: float  # newest of mtime / last access; projects: last activity
    why: list[str] = field(default_factory=list)
    score: float = 0.0
    detail: str = ""  # e.g. project markers


@dataclass(slots=True)
class Context:
    items: list[Item] = field(default_factory=list)
    focused: int | None = None  # index into items
    query: str = ""
    view: str = "results"
    updated_at: float = 0.0

    def set_results(
        self, items: list[Item], query: str, view: str, now: float | None = None
    ) -> None:
        self.items = list(items)
        self.focused = 0 if items else None
        self.query = query
        self.view = view
        self.updated_at = time.time() if now is None else now

    def active(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.items) and now - self.updated_at < CONTEXT_TTL_S

    def focus(self, index: int) -> Item | None:
        if 0 <= index < len(self.items):
            self.focused = index
            return self.items[index]
        return None

    def focused_item(self) -> Item | None:
        if self.focused is None:
            return None
        return self.items[self.focused] if self.focused < len(self.items) else None

    def by_ordinal(self, ordinal: int) -> Item | None:
        """1-based; -1 is the last item."""
        index = len(self.items) - 1 if ordinal == -1 else ordinal - 1
        return self.focus(index)

    def older(self) -> Item | None:
        """The newest item that is older than the focused one."""
        return self._step(older=True)

    def newer(self) -> Item | None:
        """The oldest item that is newer than the focused one."""
        return self._step(older=False)

    def _step(self, *, older: bool) -> Item | None:
        cur = self.focused_item()
        if cur is None:
            return None
        if older:
            cands = [(i, it) for i, it in enumerate(self.items) if it.ts < cur.ts]
            pick = max(cands, key=lambda x: x[1].ts, default=None)
        else:
            cands = [(i, it) for i, it in enumerate(self.items) if it.ts > cur.ts]
            pick = min(cands, key=lambda x: x[1].ts, default=None)
        if pick is None:
            return None
        return self.focus(pick[0])

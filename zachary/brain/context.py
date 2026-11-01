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
    versions: list[Item] = field(default_factory=list)  # older versions, newest first


@dataclass(slots=True)
class Context:
    items: list[Item] = field(default_factory=list)
    focused: int | None = None  # index into items
    query: str = ""
    view: str = "results"
    updated_at: float = 0.0
    current: Item | None = None  # last item acted on (may be an older version)

    def set_results(
        self, items: list[Item], query: str, view: str, now: float | None = None
    ) -> None:
        self.items = list(items)
        self.focused = 0 if items else None
        self.current = None
        self.query = query
        self.view = view
        self.updated_at = time.time() if now is None else now

    def active(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.items) and now - self.updated_at < CONTEXT_TTL_S

    def focus(self, index: int) -> Item | None:
        if 0 <= index < len(self.items):
            self.focused = index
            self.current = self.items[index]
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
        """Walk by time. If the focused result has versions, walk its versions
        ("the older one" = the previous version); otherwise walk the shown list."""
        focused = self.focused_item()
        cur = self.current or focused
        if cur is None or focused is None:
            return None
        pool = [focused, *focused.versions] if focused.versions else self.items
        if older:
            pick = max((it for it in pool if it.ts < cur.ts), key=lambda it: it.ts, default=None)
        else:
            pick = min((it for it in pool if it.ts > cur.ts), key=lambda it: it.ts, default=None)
        if pick is None:
            return None
        if pick in self.items:
            self.focus(self.items.index(pick))
        self.current = pick
        return pick

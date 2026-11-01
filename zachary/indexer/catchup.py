"""Startup reconciliation: bring the DB up to date with changes made while
Zachary wasn't running.

- Windows (NTFS): read the USN change journal from the cursor saved last time
  and re-check only the paths it names. No admin rights are needed for the
  unprivileged journal read on current Windows 10/11; if the journal can't be
  read, was recreated, or wrapped past our cursor, fall back to a re-scan.
- Linux (and any volume without a journal): re-scan the roots and diff
  size/mtime/inode against the DB (see ``scanner``).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from zachary.core.db import get_state, set_state
from zachary.core.platform import JournalCursor, JournalRecord, Platform
from zachary.indexer import projects
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore, ReconcileResult, inode_key
from zachary.indexer.watcher import apply_followups

log = logging.getLogger(__name__)


def cursor_key(root: str) -> str:
    return f"journal:{root}"


@dataclass(slots=True)
class CatchupStats:
    method: dict[str, str] = field(default_factory=dict)  # root -> "journal" | "scan" | "missing"
    records: int = 0
    seconds: float = 0.0
    result: ReconcileResult = field(default_factory=ReconcileResult)


def journal_dirty_paths(store: FileStore, root: str, records: list[JournalRecord]) -> set[str]:
    """Map journal records (file ids + names) to paths that need re-checking."""
    try:
        dev: int | None = os.stat(root).st_dev or None
    except OSError:
        return set()
    by_inode: dict[int, list[str]] = {}

    def paths_for(frn: int) -> list[str]:
        key = inode_key(frn)
        if key is None:
            return []
        if key not in by_inode:
            by_inode[key] = [r.path for r in store.rows_by_inode(dev, key)]
        return by_inode[key]

    dirty: set[str] = set()
    for rec in records:
        dirty.update(paths_for(rec.frn))  # its old location (deleted / renamed / modified)
        for parent in paths_for(rec.parent_frn):  # its (new) location
            dirty.add(os.path.join(parent, rec.name))
    return {p for p in dirty if store.root_for(p) is not None}


def save_cursor(store: FileStore, root: str, cursor: JournalCursor) -> None:
    with store.batch():
        set_state(
            store.conn,
            cursor_key(root),
            {"journal_id": cursor.journal_id, "next_usn": cursor.next_usn},
        )


def load_cursor(store: FileStore, root: str) -> JournalCursor | None:
    raw = get_state(store.conn, cursor_key(root))
    if not isinstance(raw, dict):
        return None
    try:
        return JournalCursor(journal_id=int(raw["journal_id"]), next_usn=int(raw["next_usn"]))
    except (KeyError, TypeError, ValueError):
        return None


def catch_up(store: FileStore, platform: Platform, *, force_scan: bool = False) -> CatchupStats:
    t0 = time.perf_counter()
    stats = CatchupStats()
    for root in store.roots:
        if not os.path.isdir(root):
            stats.method[root] = "missing"  # e.g. unplugged drive: keep its rows
            continue
        cursor = None if force_scan else load_cursor(store, root)
        got = platform.read_journal(Path(root), cursor) if cursor is not None else None
        if got is not None:
            records, new_cursor = got
            stats.records += len(records)
            dirty = journal_dirty_paths(store, root, records)
            if dirty:
                res = store.reconcile_paths(dirty)
                apply_followups(store, sorted(dirty), res)
                stats.result.merge(res)
            save_cursor(store, root, new_cursor)
            stats.method[root] = "journal"
            continue
        # Take the journal position *before* scanning so changes during the scan replay.
        start = platform.journal_cursor(Path(root))
        s = scan(store, [root])
        projects.apply_markers(store, s.markers, scope=root)
        if s.result.changed_files:
            projects.touch(store, s.result.changed_files)
        stats.result.merge(s.result)
        if start is not None:
            save_cursor(store, root, start)
        stats.method[root] = "scan"
    stats.seconds = time.perf_counter() - t0
    return stats

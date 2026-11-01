"""Metadata scan of the index roots.

One code path serves both the first full scan and the startup catch-up
("mtime diff") on Linux: the walk is compared against a snapshot of the DB, so
unchanged entries cost a dict lookup, changed ones an UPDATE, new ones an
INSERT, vanished ones a DELETE, and renames/moves keep their rows (matched by
inode).

Walking is parallel: worker threads list folders with ``os.scandir`` (which
releases the GIL during syscalls) and hand batches to the calling thread,
which is the only DB writer.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from zachary.indexer.projects import MARKER_GLOBS, MARKER_NAMES
from zachary.indexer.store import (
    Entry,
    FileStore,
    ReconcileResult,
    Row,
    entry_from_stat,
    is_link_like,
    stat_entry,
)

log = logging.getLogger(__name__)

COMMIT_EVERY = 5000  # rows per transaction
DEFAULT_WORKERS = min(8, (os.cpu_count() or 2) * 2)


@dataclass(slots=True)
class ScanStats:
    roots: list[str] = field(default_factory=list)
    dirs: int = 0
    files: int = 0
    skipped_excluded: int = 0
    errors: int = 0
    seconds: float = 0.0
    result: ReconcileResult = field(default_factory=ReconcileResult)

    @property
    def markers(self) -> dict[str, set[str]]:
        """Folder path -> project markers seen in it (collected before excludes apply)."""
        return self.result.markers


@dataclass(slots=True)
class _Listing:
    dir_path: str
    entries: list[Entry]
    markers: set[str]
    excluded: int
    error: bool


def _markers_in(names: list[str]) -> set[str]:
    found = {n for n in names if n in MARKER_NAMES}
    for n in names:
        for g in MARKER_GLOBS:
            if fnmatch.fnmatch(n, g):
                found.add(g)
    return found


def _list_dir(store: FileStore, dir_path: str, root_dev: int | None) -> _Listing:
    entries: list[Entry] = []
    names: list[str] = []
    excluded = 0
    matcher = store.matcher
    sep = store.sep
    try:
        with os.scandir(dir_path) as it:
            for de in it:
                name = de.name
                names.append(name)
                try:
                    st = de.stat(follow_symlinks=False)
                    if is_link_like(st):
                        continue
                    is_dir = de.is_dir(follow_symlinks=False)
                    if not is_dir and not de.is_file(follow_symlinks=False):
                        continue
                    if matcher.excludes_name(name, is_dir=is_dir):
                        excluded += 1
                        continue
                    path = dir_path + name if dir_path.endswith(sep) else dir_path + sep + name
                    # Walks are top-down, so only an exact path rule can hit here.
                    if matcher.excludes_exact(path):
                        excluded += 1
                        continue
                    # On Windows scandir's stat has no inode/dev; inode() costs one call.
                    inode = st.st_ino or de.inode() or None
                    entries.append(
                        entry_from_stat(path, name, st, inode=inode, dev=st.st_dev or root_dev)
                    )
                except OSError:
                    continue
    except OSError:
        return _Listing(dir_path, [], set(), 0, True)
    return _Listing(dir_path, entries, _markers_in(names), excluded, False)


def _walk(
    store: FileStore, top: str, root_dev: int | None, workers: int
) -> queue.Queue[_Listing | None]:
    """Start worker threads listing every folder under ``top``. ``None`` marks the end."""
    out: queue.Queue[_Listing | None] = queue.Queue(maxsize=256)
    todo: queue.Queue[str | None] = queue.Queue()
    pending = [1]
    lock = threading.Lock()
    todo.put(top)

    def worker() -> None:
        while True:
            d = todo.get()
            if d is None:
                return
            listing = _list_dir(store, d, root_dev)
            # Publish the listing before queueing its subfolders, so a folder's row is
            # always written before its children's rows.
            out.put(listing)
            subdirs = [e.path for e in listing.entries if e.is_dir]
            with lock:
                pending[0] += len(subdirs) - 1
                done = pending[0] == 0
            for s in subdirs:
                todo.put(s)
            if done:
                for _ in range(workers):
                    todo.put(None)
                out.put(None)

    for _ in range(workers):
        threading.Thread(target=worker, daemon=True, name="zach-scan").start()
    return out


def _scan_tree(
    store: FileStore,
    top: str,
    root: str,
    stats: ScanStats,
    *,
    workers: int,
    delete_missing: bool,
    progress: Callable[[ScanStats], None] | None,
) -> None:
    top_entry = stat_entry(top)
    if top_entry is None or not top_entry.is_dir:
        log.info("skipping missing root/folder %s", top)
        return
    res = stats.result
    snapshot = store.snapshot(top)
    by_inode: dict[tuple[int | None, int], Row] = {
        (r.dev, r.inode): r for r in snapshot.values() if r.inode is not None
    }
    seen: set[str] = set()
    dir_ids: dict[str, int] = {}
    deferred: list[Entry] = []  # possible moves, or children of possible moves

    def apply(e: Entry, parent_id: int | None) -> None:
        row = snapshot.get(e.path)
        if row is not None:
            seen.add(e.path)
            if row.is_dir != e.is_dir:
                res.deleted += store.delete_path(e.path)
                row = None
            elif store.changed(row, e):
                store.update(row.id, e)
                res.updated += 1
                if not e.is_dir:
                    res.changed_files.append((row.id, e.path, e.mtime))
            if row is not None:
                if e.is_dir:
                    dir_ids[e.path] = row.id
                return
        if (e.inode is not None and (e.dev, e.inode) in by_inode) or parent_id is None:
            deferred.append(e)
            return
        fid = store.insert(e, parent_id, root)
        res.inserted += 1
        if e.is_dir:
            dir_ids[e.path] = fid
        else:
            res.changed_files.append((fid, e.path, e.mtime))

    # The top folder itself.
    with store.batch():
        if top == root:
            apply(top_entry, None)
            if top not in dir_ids:  # deferred (e.g. root moved) — just insert it
                deferred.remove(top_entry)
                dir_ids[top] = store.insert(top_entry, None, root)
                res.inserted += 1
        else:
            row = store.get_row(top)
            seen.add(top)
            if row is None:
                pid = store.parent_id_for(top, res)
                dir_ids[top] = store.insert(top_entry, pid, root)
                res.inserted += 1
            else:
                dir_ids[top] = row.id

    out = _walk(store, top, top_entry.dev, workers)
    in_tx = 0
    store.lock.acquire()
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        while True:
            listing = out.get()
            if listing is None:
                break
            stats.dirs += 1
            stats.skipped_excluded += listing.excluded
            stats.errors += int(listing.error)
            if listing.markers:
                stats.markers[listing.dir_path] = listing.markers
            parent_id = dir_ids.get(listing.dir_path)
            for e in listing.entries:
                if not e.is_dir:
                    stats.files += 1
                apply(e, parent_id)
            in_tx += len(listing.entries)
            if in_tx >= COMMIT_EVERY:
                store.conn.execute("COMMIT")
                store.lock.release()  # let watcher batches interleave
                if progress:
                    progress(stats)
                store.lock.acquire()
                store.conn.execute("BEGIN IMMEDIATE")
                in_tx = 0
        # Deferred entries: moves first (by depth), then plain inserts.
        deferred.sort(key=lambda x: (x.path.count(store.sep), x.path))
        for e in deferred:
            store.apply_entry(e, res)
            seen.add(e.path)
        if delete_missing:
            gone = [p for p in snapshot if p not in seen]
            for p in sorted(gone, key=len):
                n = store.delete_path(p)
                if n:
                    res.deleted += n
                    res.touched_dirs.add(os.path.dirname(p))
        store.conn.execute("COMMIT")
    except BaseException:
        store.conn.execute("ROLLBACK")
        raise
    finally:
        store.lock.release()


def scan(
    store: FileStore,
    roots: list[str] | None = None,
    *,
    workers: int = DEFAULT_WORKERS,
    progress: Callable[[ScanStats], None] | None = None,
) -> ScanStats:
    """Scan (or re-scan) whole roots, reconciling the DB with the disk."""
    t0 = time.perf_counter()
    stats = ScanStats(roots=list(roots if roots is not None else store.roots))
    for root in stats.roots:
        root = store.norm(root)
        if store.matcher.excludes_path(root):
            continue
        _scan_tree(
            store, root, root, stats, workers=workers, delete_missing=True, progress=progress
        )
    stats.seconds = time.perf_counter() - t0
    return stats


def scan_subtree(store: FileStore, folder: str, *, workers: int = DEFAULT_WORKERS) -> ScanStats:
    """Scan one folder inside a root (e.g. a folder that just appeared)."""
    t0 = time.perf_counter()
    folder = store.norm(folder)
    root = store.root_for(folder)
    stats = ScanStats(roots=[folder])
    if root is not None and not store.is_excluded(folder, True):
        _scan_tree(store, folder, root, stats, workers=workers, delete_missing=True, progress=None)
    stats.seconds = time.perf_counter() - t0
    return stats

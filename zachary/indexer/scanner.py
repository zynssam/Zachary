"""Metadata scan of the index roots.

One code path serves both the first full scan and the startup catch-up
("mtime diff") on Linux: the walk is compared against a snapshot of the DB, so
unchanged entries cost a dict lookup, changed ones an UPDATE, new ones a
batched INSERT, vanished ones a DELETE, and renames/moves keep their rows
(matched by inode).

Parallelism: each root (drive / mount / folder) gets its own walker thread, and
all roots are walked at once, so I/O on different disks overlaps. The calling
thread is the only DB writer. Measured on a warm cache, extra walker threads
*within* one root only add GIL contention (1 thread: 5.8 s, 4: 8.2 s for 216k
entries), so the default is one per root.
"""

from __future__ import annotations

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
    SnapRow,
    entry_from_stat,
    is_link_like,
    stat_entry,
)

log = logging.getLogger(__name__)

COMMIT_EVERY = 5000  # rows per transaction
DEFAULT_WORKERS = 1  # walker threads per root (see module docstring)
_MARKER_SUFFIXES = tuple(g.lstrip("*") for g in MARKER_GLOBS)  # ".sln", ".csproj"


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
        if n.endswith(_MARKER_SUFFIXES):
            found.add("*" + n[n.rfind(".") :])
    return found


def _list_dir(store: FileStore, dir_path: str, root_dev: int | None) -> _Listing:
    entries: list[Entry] = []
    names: list[str] = []
    excluded = 0
    matcher = store.matcher
    sep = store.sep
    prefix = dir_path if dir_path.endswith(sep) else dir_path + sep
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
                    if matcher.excludes_name(name, is_dir=is_dir) or (
                        is_dir and matcher.hidden_attr(st)
                    ):
                        excluded += 1
                        continue
                    path = prefix + name
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


class _Walk:
    """Walker threads for one tree, feeding listings into a queue (``None`` = done)."""

    def __init__(self, store: FileStore, top: str, root_dev: int | None, workers: int) -> None:
        self.out: queue.Queue[_Listing | None] = queue.Queue(maxsize=1024)
        self._todo: queue.Queue[str | None] = queue.Queue()
        self._pending = 1
        self._lock = threading.Lock()
        self._store = store
        self._root_dev = root_dev
        self._workers = workers
        self._todo.put(top)
        for _ in range(workers):
            threading.Thread(target=self._work, daemon=True, name="zach-scan").start()

    def _work(self) -> None:
        while True:
            d = self._todo.get()
            if d is None:
                return
            listing = _list_dir(self._store, d, self._root_dev)
            # Publish a folder's listing before queueing its subfolders, so the writer
            # always sees a folder's row before its children's rows.
            self.out.put(listing)
            subdirs = [e.path for e in listing.entries if e.is_dir]
            with self._lock:
                self._pending += len(subdirs) - 1
                done = self._pending == 0
            for s in subdirs:
                self._todo.put(s)
            if done:
                for _ in range(self._workers):
                    self._todo.put(None)
                self.out.put(None)


def _changed(t: SnapRow, e: Entry) -> bool:
    _id, _is_dir, size, mtime, inode, dev = t
    return size != e.size or mtime != e.mtime or inode != e.inode or dev != e.dev


def _scan_tree(
    store: FileStore,
    top: str,
    root: str,
    stats: ScanStats,
    *,
    walk: _Walk | None,
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
    by_inode: set[tuple[int | None, int]] = {
        (t[5], t[4]) for t in snapshot.values() if t[4] is not None
    }
    seen: set[str] = set()
    dir_ids: dict[str, int] = {}
    deferred: list[Entry] = []  # possible moves, or children of possible moves

    # The top folder itself.
    with store.batch():
        snap = snapshot.get(top)
        seen.add(top)
        if snap is not None and bool(snap[1]):
            dir_ids[top] = snap[0]
            if _changed(snap, top_entry):
                store.update(snap[0], top_entry)
        else:
            if snap is not None:
                res.deleted += store.delete_path(top)
            pid = None if top == root else store.parent_id_for(top, res)
            dir_ids[top] = store.insert(top_entry, pid, root)
            res.inserted += 1

    if walk is None:
        walk = _Walk(store, top, top_entry.dev, workers)
    in_tx = 0
    store.lock.acquire()
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        while True:
            listing = walk.out.get()
            if listing is None:
                break
            stats.dirs += 1
            stats.skipped_excluded += listing.excluded
            stats.errors += int(listing.error)
            if listing.markers:
                res.markers[listing.dir_path] = listing.markers
            parent_id = dir_ids.get(listing.dir_path)
            fresh: list[Entry] = []
            for e in listing.entries:
                if not e.is_dir:
                    stats.files += 1
                t = snapshot.get(e.path)
                if t is not None:
                    seen.add(e.path)
                    if bool(t[1]) != e.is_dir:
                        res.deleted += store.delete_path(e.path)
                    else:
                        if _changed(t, e):
                            store.update(t[0], e)
                            res.updated += 1
                            if not e.is_dir:
                                res.changed_files.append((t[0], e.path, e.mtime))
                        if e.is_dir:
                            dir_ids[e.path] = t[0]
                        continue
                if parent_id is None or (e.inode is not None and (e.dev, e.inode) in by_inode):
                    deferred.append(e)
                else:
                    fresh.append(e)
            if fresh:
                ids = store.insert_many(fresh, parent_id, root)
                res.inserted += len(ids)
                for e, fid in zip(fresh, ids, strict=True):
                    if e.is_dir:
                        dir_ids[e.path] = fid
                    else:
                        res.changed_files.append((fid, e.path, e.mtime))
            in_tx += len(listing.entries)
            if in_tx >= COMMIT_EVERY:
                store.conn.execute("COMMIT")
                store.lock.release()  # let watcher batches interleave
                if progress:
                    progress(stats)
                store.lock.acquire()
                store.conn.execute("BEGIN IMMEDIATE")
                in_tx = 0
        # Deferred entries: parents first; moves keep identity, the rest are inserted.
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
    """Scan (or re-scan) whole roots, reconciling the DB with the disk.

    All roots' walkers start immediately (I/O on different drives overlaps); the
    DB writes happen root by root in this thread.
    """
    t0 = time.perf_counter()
    stats = ScanStats(roots=list(roots if roots is not None else store.roots))
    todo: list[tuple[str, _Walk | None]] = []
    for raw in stats.roots:
        root = store.norm(raw)
        if store.matcher.excludes_path(root):
            continue
        top = stat_entry(root)
        walk = _Walk(store, root, top.dev, workers) if top and top.is_dir else None
        todo.append((root, walk))
    for root, walk in todo:
        if walk is None:
            log.info("skipping missing root %s", root)
            continue
        _scan_tree(
            store,
            root,
            root,
            stats,
            walk=walk,
            workers=workers,
            delete_missing=True,
            progress=progress,
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
        _scan_tree(
            store,
            folder,
            root,
            stats,
            walk=None,
            workers=workers,
            delete_missing=True,
            progress=None,
        )
    stats.seconds = time.perf_counter() - t0
    return stats

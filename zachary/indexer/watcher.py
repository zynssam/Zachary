"""File-system events -> debounced, batched DB updates.

watchdog delivers raw events (inotify on Linux, ReadDirectoryChangesW on
Windows). We don't trust event types much: each event only marks paths as
*dirty*. A path is processed once it has been quiet for ``debounce`` seconds
(default 1.5 s), so an editor's save storm or an unzip of thousands of files
becomes one batch. Processing re-stats the paths and lets
:meth:`FileStore.reconcile_paths` decide insert / update / move / delete.

Both ends of a move are marked with the same timestamp so they land in the
same batch, which is what lets the store keep the file's identity.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from watchdog.events import (
    EVENT_TYPE_CLOSED_NO_WRITE,
    EVENT_TYPE_OPENED,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from zachary.core.config import is_under
from zachary.core.platform import TreeWatcher
from zachary.indexer import projects
from zachary.indexer.store import FileStore, ReconcileResult

log = logging.getLogger(__name__)

DEFAULT_DEBOUNCE_S = 1.5
MAX_BATCH = 20_000

_HAS_FILE_ATTRS = hasattr(os.stat_result, "st_file_attributes")
_IGNORED = frozenset({EVENT_TYPE_OPENED, EVENT_TYPE_CLOSED_NO_WRITE})


def _s(p: str | bytes) -> str:
    return os.fsdecode(p)


def apply_followups(store: FileStore, paths: list[str], result: ReconcileResult) -> None:
    """Project detection, edit log and project activity after a reconcile batch."""
    # New folders were scanned; their markers (at any depth) are in result.markers.
    for d in result.new_dirs:
        sub = {p: m for p, m in result.markers.items() if is_under(p, d)}
        projects.apply_markers(store, sub, scope=d)
    # Marker files appearing/disappearing in existing folders.
    marker_dirs = {os.path.dirname(p) for p in paths if projects.is_marker(os.path.basename(p))}
    if marker_dirs:
        projects.refresh_folders(store, marker_dirs)
    if result.changed_files:
        store.log_edits(result.changed_files)
        projects.touch(store, result.changed_files)


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: Watcher) -> None:
        self.w = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in _IGNORED:
            return
        if event.is_directory and event.event_type == "modified" and not _HAS_FILE_ATTRS:
            return  # a folder's own mtime; its children report themselves
        # Where folders have a "hidden" attribute (Windows), hiding/un-hiding shows up only
        # as a modified event on the folder itself, so those are kept there.
        paths = [_s(event.src_path)]
        dest = getattr(event, "dest_path", "")
        if dest:
            paths.append(_s(dest))
        self.w.mark(paths)


class Watcher:
    """Watches the store's roots and applies changes in debounced batches."""

    def __init__(
        self,
        store: FileStore,
        *,
        debounce: float = DEFAULT_DEBOUNCE_S,
        on_batch: Callable[[ReconcileResult, float], None] | None = None,
    ) -> None:
        self.store = store
        self.debounce = debounce
        self.on_batch = on_batch
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._observer: BaseObserver | None = None
        self._tree: TreeWatcher | None = None
        self.backend = ""
        self.needs_catchup = False
        self._thread: threading.Thread | None = None
        self.batches = 0

    # -- event intake --------------------------------------------------------
    def mark(self, paths: list[str]) -> None:
        now = time.monotonic()
        with self._lock:
            for p in paths:
                self._pending[p] = now
        self._wake.set()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def oldest_pending(self) -> float | None:
        """Monotonic time of the oldest unprocessed event, if any."""
        with self._lock:
            return min(self._pending.values(), default=None)

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        """Start watching. Linux: exclude-aware inotify (only indexed folders get a
        watch). Elsewhere: watchdog (Windows' ReadDirectoryChangesW is one handle per
        root, so recursion costs nothing extra)."""
        self._stop.clear()
        self.needs_catchup = False
        tree = self.store.config.platform.tree_watcher(
            self.store.is_excluded, self.mark, self._overflow
        )
        if tree is not None:
            for root in self.store.roots:
                if os.path.isdir(root):
                    tree.add_tree(root)
            tree.start()
            self._tree = tree
            self.backend = tree.name
        else:
            observer = Observer()
            handler = _Handler(self)
            for root in self.store.roots:
                if os.path.isdir(root):
                    observer.schedule(handler, root, recursive=True)
            observer.daemon = True
            observer.start()
            self._observer = observer
            self.backend = "watchdog"
        self._thread = threading.Thread(target=self._run, name="zach-watch", daemon=True)
        self._thread.start()

    def _overflow(self) -> None:
        self.needs_catchup = True  # the service runs a catch-up scan

    @property
    def watch_count(self) -> int | None:
        return self._tree.watch_count if self._tree is not None else None

    @property
    def limit_hit(self) -> bool:
        return bool(self._tree is not None and self._tree.limit_hit)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._tree is not None:
            self._tree.stop()
            self._tree = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def flush(self) -> ReconcileResult | None:
        """Process everything pending now, ignoring the debounce (tests, shutdown)."""
        with self._lock:
            ready = list(self._pending)
            self._pending.clear()
        return self._process(ready) if ready else None

    # -- processing ----------------------------------------------------------------
    def _take_ready(self) -> tuple[list[str], float | None]:
        """Paths quiet for ``debounce`` seconds, and when the next one will be ready."""
        now = time.monotonic()
        ready: list[str] = []
        next_due: float | None = None
        with self._lock:
            for p, t in list(self._pending.items()):
                due = t + self.debounce
                if due <= now and len(ready) < MAX_BATCH:
                    ready.append(p)
                    del self._pending[p]
                elif next_due is None or due < next_due:
                    next_due = due
        return ready, next_due

    def _run(self) -> None:
        while not self._stop.is_set():
            ready, next_due = self._take_ready()
            if ready:
                try:
                    self._process(ready)
                except Exception:  # keep watching even if one batch fails
                    log.exception("watcher batch failed (%d paths)", len(ready))
                continue
            timeout = None if next_due is None else max(0.01, next_due - time.monotonic())
            self._wake.wait(timeout)
            self._wake.clear()

    def _process(self, paths: list[str]) -> ReconcileResult:
        t0 = time.perf_counter()
        store = self.store
        result = store.reconcile_paths(paths)
        apply_followups(store, paths, result)
        took = time.perf_counter() - t0
        self.batches += 1
        log.debug(
            "batch: %d paths -> +%d ~%d >%d -%d in %.1f ms",
            len(paths),
            result.inserted,
            result.updated,
            result.moved,
            result.deleted,
            took * 1e3,
        )
        if self.on_batch is not None:
            self.on_batch(result, took)
        return result

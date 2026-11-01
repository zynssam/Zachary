"""The indexer daemon: catch-up, live watching, recent-file ingestion, and
reacting to config changes (excludes, roots, pause) made from the CLI/UI.

Runs at low CPU/IO priority. Idle cost is a DB poll every couple of seconds.
"""

from __future__ import annotations

import errno
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from zachary.core.config import Config
from zachary.core.db import open_db, set_state
from zachary.core.platform import Platform, get_platform
from zachary.indexer.catchup import catch_up, save_cursor
from zachary.indexer.recent import ingest_recent
from zachary.indexer.store import FileStore
from zachary.indexer.watcher import DEFAULT_DEBOUNCE_S, Watcher

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ConfigSig:
    roots: tuple[str, ...]
    excludes: tuple[tuple[str, str, bool], ...]
    paused: bool


class IndexerService:
    def __init__(
        self,
        db_path: Path | str,
        platform: Platform | None = None,
        *,
        debounce: float = DEFAULT_DEBOUNCE_S,
        poll_interval: float = 2.0,
        recent_interval: float = 120.0,
        cursor_interval: float = 60.0,
        fallback_rescan_interval: float = 600.0,
        lower_priority: bool = True,
    ) -> None:
        self.platform = platform or get_platform()
        self.conn = open_db(db_path)
        self.config = Config(self.conn, self.platform)
        self.config.seed_defaults()
        self.store = FileStore(self.conn, self.config)
        self.debounce = debounce
        self.poll_interval = poll_interval
        self.recent_interval = recent_interval
        self.cursor_interval = cursor_interval
        self.fallback_rescan_interval = fallback_rescan_interval
        self.lower_priority = lower_priority
        self.watcher: Watcher | None = None
        self.watching = False
        self.ready = threading.Event()  # set after the first catch-up
        self._stop = threading.Event()

    # -- helpers ---------------------------------------------------------------
    def _sig(self) -> _ConfigSig:
        return _ConfigSig(
            roots=tuple(self.store.roots),
            excludes=tuple((p, k, e) for p, k, _s, e in self.config.list_excludes()),
            paused=bool(self.config.get_setting("indexing_paused")),
        )

    def _set_status(self, **kv: object) -> None:
        with self.store.batch():
            for k, v in kv.items():
                set_state(self.conn, f"indexer.{k}", v)

    def _start_watcher(self) -> None:
        self._stop_watcher()
        n_dirs = self.conn.execute("SELECT count(*) FROM files WHERE is_dir = 1").fetchone()[0]
        limit = self.platform.check_watch_limit(int(n_dirs))
        if not limit.ok:
            log.warning("%s", limit.message)
        self._set_status(watch_limit_ok=limit.ok, watch_limit_message=limit.message)
        w = Watcher(self.store, debounce=self.debounce)
        try:
            w.start()
        except OSError as e:
            if e.errno in (errno.ENOSPC, errno.EMFILE):
                log.error("live watching unavailable (%s); falling back to periodic rescans", e)
                w.stop()
                self.watching = False
                self._set_status(watching=False)
                return
            raise
        self.watcher = w
        self.watching = True
        self._set_status(watching=True)

    def _stop_watcher(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher.flush()
            self.watcher = None
        self.watching = False

    def _catch_up(self) -> None:
        st = catch_up(self.store, self.platform)
        r = st.result
        log.info(
            "catch-up %s: +%d ~%d >%d -%d in %.1fs",
            st.method,
            r.inserted,
            r.updated,
            r.moved,
            r.deleted,
            st.seconds,
        )
        self._set_status(last_catchup=time.time(), last_catchup_seconds=round(st.seconds, 2))

    def _ingest_recent(self) -> None:
        try:
            n = ingest_recent(self.store, self.platform)
        except Exception:
            log.exception("recent-items ingestion failed")
            return
        if n:
            log.debug("recent items: %d new access rows", n)

    def _save_cursors(self) -> None:
        """Record journal positions once the watcher has caught up to them."""
        for root in self.store.roots:
            cur = self.platform.journal_cursor(Path(root))
            if cur is None:
                continue
            if self.watcher is None or self.watcher.pending_count() == 0:
                save_cursor(self.store, root, cur)

    # -- lifecycle -------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if self.lower_priority:
            self.platform.lower_process_priority()
        self.store.ensure_fts()
        self.store.purge_excluded()
        sig = self._sig()
        if not sig.paused:
            self._catch_up()
            self._ingest_recent()
            self._start_watcher()
        self.ready.set()
        next_recent = time.monotonic() + self.recent_interval
        next_cursor = time.monotonic() + self.cursor_interval
        next_rescan = time.monotonic() + self.fallback_rescan_interval
        rev = self.config.config_rev()
        try:
            while not self._stop.wait(self.poll_interval):
                self._set_status(heartbeat=time.time())
                now = time.monotonic()
                new_rev = self.config.config_rev()
                if new_rev != rev:
                    rev = new_rev
                    sig = self._on_config_change(sig)
                if sig.paused:
                    continue
                if now >= next_recent:
                    self._ingest_recent()
                    next_recent = now + self.recent_interval
                if now >= next_cursor:
                    self._save_cursors()
                    next_cursor = now + self.cursor_interval
                if not self.watching and now >= next_rescan:
                    self._catch_up()
                    next_rescan = now + self.fallback_rescan_interval
        finally:
            self._stop_watcher()
            self.conn.close()

    def _on_config_change(self, old: _ConfigSig) -> _ConfigSig:
        self.store.reload()
        new = self._sig()
        if new.paused:
            if not old.paused:
                log.info("indexing paused")
                self._stop_watcher()
            return new
        if new.roots != old.roots or new.excludes != old.excludes or old.paused:
            log.info("config changed: purging excluded paths and catching up")
            self._stop_watcher()
            self.store.purge_excluded()
            self._catch_up()
            self._start_watcher()
        return new

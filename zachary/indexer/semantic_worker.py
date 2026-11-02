"""Background semantic work for the indexer: embeddings, vector index, relations,
version groups. One thread with its own DB connection, deliberately gentle:

- waits while indexing is paused, on battery (unless ``embed_on_battery``), or
  while the machine is busy;
- works in small batches and sleeps as long as it worked (<= ~50% of one core);
- between batches: rebuilds the IVF index when the collection changed a lot,
  updates "related files" for newly embedded files (full pass weekly), and
  regroups versions after file changes.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from zachary.core.config import Config
from zachary.core.db import open_db, set_state
from zachary.core.platform import Platform
from zachary.indexer import clustering, relations, versions
from zachary.indexer.embedder import Embedder, Model2VecEmbedder
from zachary.indexer.semantic import SemanticIndexer
from zachary.search import vectors

log = logging.getLogger(__name__)

BATCH = 32
WINDOW = 512  # pending files fetched per queue query
DUTY = 0.5  # fraction of time spent working while there is a backlog
IDLE_WAIT_S = 30.0  # nothing to do / waiting for conditions
MODEL_RETRY_S = 600.0


def default_embedder(config: Config, db_path: Path) -> Embedder:
    return Model2VecEmbedder(str(config.get_setting("embed_model")), db_path.parent / "models")


class SemanticWorker(threading.Thread):
    def __init__(
        self,
        db_path: Path,
        platform: Platform,
        *,
        embedder_factory: Callable[[Config], Embedder] | None = None,
        batch: int = BATCH,
        duty: float = DUTY,
        idle_wait: float = IDLE_WAIT_S,
    ) -> None:
        super().__init__(name="zach-semantic", daemon=True)
        self.db_path = Path(db_path)
        self.platform = platform
        self.embedder_factory = embedder_factory
        self.batch = batch
        self.duty = duty
        self.idle_wait = idle_wait
        self._stop_ev = threading.Event()
        self._wake = threading.Event()
        self._versions_dirty = threading.Event()
        self._versions_dirty.set()  # group once at start
        self.embedded_total = 0
        self.last_error: str | None = None

    # -- control (any thread) ---------------------------------------------------------
    def stop(self) -> None:
        self._stop_ev.set()
        self._wake.set()

    def nudge(self) -> None:
        """New/changed files arrived: look for work now."""
        self._wake.set()

    def mark_versions_dirty(self) -> None:
        self._versions_dirty.set()
        self._wake.set()

    def _sleep(self, seconds: float) -> bool:
        """Sleep, waking early on nudge/stop. Returns False when stopping."""
        self._wake.wait(seconds)
        self._wake.clear()
        return not self._stop_ev.is_set()

    # -- thread ------------------------------------------------------------------------
    def run(self) -> None:
        conn = open_db(self.db_path)
        config = Config(conn, self.platform)
        try:
            embedder = (
                self.embedder_factory(config)
                if self.embedder_factory
                else default_embedder(config, self.db_path)
            )
            sem = SemanticIndexer(conn, config, embedder)
            dirty_ids: list[int] = []
            window: list[sqlite3.Row] = []
            while not self._stop_ev.is_set():
                reason = self._blocked(config)
                if reason:
                    set_state(conn, "semantic.waiting", reason)
                    if not self._sleep(self.idle_wait):
                        break
                    continue
                set_state(conn, "semantic.waiting", "")
                try:
                    if not window:
                        # One queue query per window, not per batch (it sorts all files).
                        window = sem.pending(WINDOW)
                    rows, window = window[: self.batch], window[self.batch :]
                    ids = [r[0] for r in rows]
                    st = sem.process(rows) if rows else None
                except Exception as e:  # model download failure, corrupt file in parser...
                    self.last_error = f"{type(e).__name__}: {e}"
                    log.warning("semantic indexing paused: %s", self.last_error)
                    set_state(conn, "semantic.error", self.last_error)
                    window = []
                    if not self._sleep(MODEL_RETRY_S):
                        break
                    continue
                if st is not None and st.files:
                    self.embedded_total += st.files
                    dirty_ids.extend(ids)
                    set_state(conn, "semantic.last_batch", {
                        "files": st.files, "extracted": st.extracted, "reused": st.reused,
                        "chunks": st.chunks, "seconds": round(st.seconds, 3), "at": time.time(),
                    })  # fmt: skip
                    # Duty cycle: rest as long as we worked.
                    if not self._sleep(st.seconds * (1 / self.duty - 1)):
                        break
                    continue
                # Backlog done: maintenance, then idle.
                self._maintain(conn, dirty_ids)
                dirty_ids = []
                if not self._sleep(self.idle_wait):
                    break
        finally:
            conn.close()

    def _blocked(self, config: Config) -> str:
        if config.get_setting("indexing_paused"):
            return "paused"
        if not config.get_setting("embed_on_battery") and self.platform.on_battery():
            return "on battery"
        if self.platform.system_busy():
            return "system busy"
        return ""

    def _maintain(self, conn: sqlite3.Connection, dirty_ids: list[int]) -> None:
        t0 = time.perf_counter()
        if vectors.needs_rebuild(conn):
            st = vectors.build(conn)
            log.info("vector index: %d vectors, %d clusters, %.1fs", *st.__dict__.values())
            dirty_ids = []  # a full relations pass follows
            set_state(conn, "relations.full_at", 0)
        rb = relations.RelationBuilder(conn)
        if relations.due_full(conn):
            rs = rb.compute(None)
            log.info("relations (full): %d files, %d links, %.1fs", rs.files, rs.links, rs.seconds)
        elif dirty_ids:
            rb.compute(dirty_ids)
        if self._versions_dirty.is_set():
            self._versions_dirty.clear()
            versions.rebuild(conn)
        if clustering.due(conn):  # nightly: topics (virtual collections)
            try:
                roots = [str(r) for r in Config(conn, self.platform).index_roots()]
                clustering.build(conn, roots)
            except Exception as e:  # e.g. scikit-learn missing: topics just stay as they are
                log.warning("topic clustering failed: %s", e)
                set_state(conn, "topics.error", f"{type(e).__name__}: {e}")
        set_state(conn, "semantic.maintained_at", time.time())
        log.debug("semantic maintenance %.1fs", time.perf_counter() - t0)

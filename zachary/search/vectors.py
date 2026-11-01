"""Approximate nearest-neighbour search over file vectors (IVF in SQLite + numpy).

sqlite-vec 0.1 only does brute-force kNN, measured at 169 ms per query on 200k
int8 vectors (and 36 ms + 205 MB RAM for an in-memory numpy scan). Instead:

- ``build``: spherical k-means puts every file vector in one of ~sqrt(N)
  clusters (``vec_ivf.cluster``); centroids go to ``ivf_centroids``.
- ``VectorIndex.search``: compare the query with the centroids, read only the
  vectors of the nearest clusters (an indexed range read), score them with numpy.

Under ``IVF_MIN_VECTORS`` there is a single cluster (exact scan, still fast).
New vectors are assigned to their nearest existing centroid; the indexer
rebuilds when the collection has grown or shrunk a lot.
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass

import numpy as np

from zachary.core.db import EMBED_DIM, get_state, set_state
from zachary.indexer.embedder import Embedder, FloatArray, normalize

IVF_MIN_VECTORS = 2000
TARGET_SCAN = 2500  # vectors read per query (sets how many clusters are probed)
TRAIN_PER_CLUSTER = 40  # k-means trains on a sample of k * this many vectors
MIN_PROBE = 4
REBUILD_GROWTH = 0.3  # rebuild once the vector count moved 30% from the last build


@dataclass(slots=True)
class BuildStats:
    vectors: int
    clusters: int
    seconds: float


def _to_float(blob_rows: list[bytes]) -> FloatArray:
    m = np.frombuffer(b"".join(blob_rows), dtype=np.int8).reshape(len(blob_rows), EMBED_DIM)
    return normalize(m.astype(np.float32))


def _cluster_sums(x: FloatArray, labels: np.ndarray, k: int) -> FloatArray:
    """Per-cluster sum of rows (sort + reduceat; np.add.at is ~10x slower)."""
    order = np.argsort(labels, kind="stable")
    sorted_labels = labels[order]
    starts = np.flatnonzero(np.r_[True, sorted_labels[1:] != sorted_labels[:-1]])
    sums = np.zeros((k, x.shape[1]), dtype=np.float32)
    sums[sorted_labels[starts]] = np.add.reduceat(x[order], starts, axis=0)
    return sums


def load_centroids(conn: sqlite3.Connection) -> FloatArray | None:
    rows = conn.execute("SELECT embedding FROM ivf_centroids ORDER BY id").fetchall()
    if not rows:
        return None
    return np.frombuffer(b"".join(r[0] for r in rows), dtype=np.float32).reshape(
        len(rows), EMBED_DIM
    )


def nearest_cluster(centroids: FloatArray | None, v: FloatArray) -> int:
    if centroids is None or not len(centroids):
        return 0
    return int(np.argmax(centroids @ v))


def put(conn: sqlite3.Connection, file_id: int, vec: bytes, centroids: FloatArray | None) -> None:
    """Insert/replace one file vector in the IVF table (caller holds a transaction)."""
    v = normalize(np.frombuffer(vec, dtype=np.int8).astype(np.float32)[None, :])[0]
    conn.execute(
        "INSERT INTO vec_ivf(file_id, cluster, embedding) VALUES (?, ?, ?) "
        "ON CONFLICT(file_id) DO UPDATE SET cluster = excluded.cluster, "
        "embedding = excluded.embedding",
        (file_id, nearest_cluster(centroids, v), vec),
    )


def needs_rebuild(conn: sqlite3.Connection) -> bool:
    n = int(conn.execute("SELECT count(*) FROM vec_ivf").fetchone()[0])
    built = int(get_state(conn, "ivf.n_at_build", 0))
    if n < IVF_MIN_VECTORS:
        return built != 0 and n < built * (1 - REBUILD_GROWTH)
    if built == 0:
        return True
    return abs(n - built) / built > REBUILD_GROWTH


def build(conn: sqlite3.Connection, *, iters: int = 8, seed: int = 0) -> BuildStats:
    """(Re)cluster every vector. Runs in the indexer, in the background."""
    t0 = time.perf_counter()
    rows = conn.execute("SELECT file_id, embedding FROM vec_ivf ORDER BY file_id").fetchall()
    n = len(rows)
    ids = [r[0] for r in rows]
    if n < IVF_MIN_VECTORS:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM ivf_centroids")
        conn.execute("UPDATE vec_ivf SET cluster = 0")
        set_state(conn, "ivf.n_at_build", n if n >= IVF_MIN_VECTORS else 0)
        set_state(conn, "ivf.version", int(get_state(conn, "ivf.version", 0)) + 1)
        conn.execute("COMMIT")
        return BuildStats(n, 1 if n else 0, time.perf_counter() - t0)
    x = _to_float([r[1] for r in rows])
    k = max(16, min(2048, int(math.sqrt(n))))
    rng = np.random.default_rng(seed)
    # Train on a sample (k-means cost is n*k*dim per iteration), then assign all.
    sample = x[rng.choice(n, size=min(n, k * TRAIN_PER_CLUSTER), replace=False)]
    m = len(sample)
    c = sample[rng.choice(m, size=k, replace=False)].copy()
    batch = 16384
    for _ in range(iters):
        lab = np.argmax(sample @ c.T, axis=1)
        sums = _cluster_sums(sample, lab, k)
        empty = np.bincount(lab, minlength=k) == 0
        if empty.any():  # re-seed empty clusters with random points
            sums[empty] = sample[rng.choice(m, size=int(empty.sum()), replace=False)]
        c = normalize(sums)
    labels = np.zeros(n, dtype=np.int64)
    for s in range(0, n, batch):
        labels[s : s + batch] = np.argmax(x[s : s + batch] @ c.T, axis=1)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM ivf_centroids")
        conn.executemany(
            "INSERT INTO ivf_centroids(id, embedding) VALUES (?, ?)",
            ((i, c[i].astype(np.float32).tobytes()) for i in range(k)),
        )
        conn.executemany(
            "UPDATE vec_ivf SET cluster = ? WHERE file_id = ?",
            zip(labels.tolist(), ids, strict=True),
        )
        set_state(conn, "ivf.n_at_build", n)
        set_state(conn, "ivf.version", int(get_state(conn, "ivf.version", 0)) + 1)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return BuildStats(n, k, time.perf_counter() - t0)


@dataclass(slots=True)
class SemanticSearch:
    """What search needs for meaning: a query embedder and the file-vector index."""

    embedder: Embedder
    index: VectorIndex | MemoryVectorIndex


class VectorIndex:
    """Query side, used by search (one per connection; reloads after a rebuild)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._version = -1
        self._centroids: FloatArray | None = None
        self._sizes: dict[int, int] = {}

    def _refresh(self) -> None:
        v = int(get_state(self.conn, "ivf.version", 0))
        if v != self._version:
            self._centroids = load_centroids(self.conn)
            self._sizes = dict(
                self.conn.execute("SELECT cluster, count(*) FROM vec_ivf GROUP BY cluster")
            )
            self._version = v

    def score_ids(
        self, q: FloatArray, ids: set[int], *, min_sim: float = 0.0
    ) -> list[tuple[int, float]]:
        """Exact scores for a given (small) set of files, best first."""
        cur = self.conn.cursor()
        cur.row_factory = None
        id_list = list(ids)
        rows: list[tuple[int, bytes]] = []
        for i in range(0, len(id_list), 900):
            chunk = id_list[i : i + 900]
            marks = ",".join("?" * len(chunk))
            rows += cur.execute(
                f"SELECT file_id, embedding FROM vec_ivf WHERE file_id IN ({marks})", chunk
            ).fetchall()
        if not rows:
            return []
        m = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.int8).reshape(len(rows), -1)
        sims = (m.astype(np.float32) @ q) / 127.0
        order = np.argsort(-sims)
        return [(int(rows[i][0]), float(sims[i])) for i in order if sims[i] >= min_sim]

    def search(
        self, q: FloatArray, k: int = 100, *, min_sim: float = 0.0, allowed: set[int] | None = None
    ) -> list[tuple[int, float]]:
        """Top ``k`` (file_id, cosine) for a normalized query vector."""
        self._refresh()
        cur = self.conn.cursor()
        cur.row_factory = None
        c = self._centroids
        if c is None or not len(c):
            rows = cur.execute("SELECT file_id, embedding FROM vec_ivf").fetchall()
        else:
            order = np.argsort(-(c @ q))
            probe: list[int] = []
            covered = 0
            for cl in order.tolist():
                probe.append(int(cl))
                covered += self._sizes.get(int(cl), 0)
                if len(probe) >= MIN_PROBE and covered >= TARGET_SCAN:
                    break
            marks = ",".join("?" * len(probe))
            rows = cur.execute(
                f"SELECT file_id, embedding FROM vec_ivf WHERE cluster IN ({marks})", probe
            ).fetchall()
        if allowed is not None:
            rows = [r for r in rows if r[0] in allowed]
        if not rows:
            return []
        m = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.int8).reshape(len(rows), -1)
        sims = (m.astype(np.float32) @ q) / 127.0
        top = np.argsort(-sims)[:k] if len(sims) <= k else np.argpartition(-sims, k)[:k]
        top = top[np.argsort(-sims[top])]
        return [(int(rows[i][0]), float(sims[i])) for i in top if sims[i] >= min_sim]


class MemoryVectorIndex:
    """IVF index held in memory by the assistant: no disk reads per query.

    The int8 vectors (256 bytes per file: ~51 MB for 200k files) are grouped by
    cluster, so a query is a numpy scan of the nearest clusters. Under memory
    pressure SQLite pages for the on-disk index got evicted and re-read (query
    spikes of 200-900 ms on an 6 GB laptop); keeping this in RAM removes that.

    Freshness: every ``REFRESH_S`` it checks the index version (a rebuild ->
    full reload) and picks up newly embedded files incrementally. Deleted files
    can linger until the next reload; search drops ids that no longer exist.
    """

    REFRESH_S = 5.0
    FULL_RELOAD_S = 1800.0

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._version = -1
        self._loaded_at = 0.0
        self._checked_at = 0.0
        self._since = 0.0  # embedded_at high-water mark of what we have
        self._centroids: FloatArray | None = None
        self._ids = np.zeros(0, dtype=np.int64)
        self._mat = np.zeros((0, EMBED_DIM), dtype=np.int8)
        self._bounds: dict[int, tuple[int, int]] = {}  # cluster -> [start, end) rows
        self._extra: dict[int, tuple[int, np.ndarray]] = {}  # file_id -> (cluster, vec)
        self._stale: set[int] = set()  # base rows superseded by an extra

    def _cursor(self) -> sqlite3.Cursor:
        cur = self.conn.cursor()
        cur.row_factory = None
        return cur

    def _full_load(self) -> None:
        cur = self._cursor()
        self._version = int(get_state(self.conn, "ivf.version", 0))
        self._since = float(
            cur.execute("SELECT coalesce(max(embedded_at), 0) FROM files").fetchone()[0]
        )
        self._centroids = load_centroids(self.conn)
        n = int(cur.execute("SELECT count(*) FROM vec_ivf").fetchone()[0])
        # Stream into preallocated arrays: fetching every row at once peaked at ~7x
        # the final size (Python tuples + bytes) on a 200k index.
        clusters = np.empty(n, dtype=np.int64)
        ids = np.empty(n, dtype=np.int64)
        mat = np.empty((n, EMBED_DIM), dtype=np.int8)
        cur.execute("SELECT cluster, file_id, embedding FROM vec_ivf ORDER BY cluster")
        i = 0
        while i < n:
            batch = cur.fetchmany(8192)
            if not batch:
                break
            j = i + len(batch)
            clusters[i:j] = [r[0] for r in batch]
            ids[i:j] = [r[1] for r in batch]
            mat[i:j] = np.frombuffer(b"".join(r[2] for r in batch), dtype=np.int8).reshape(
                len(batch), EMBED_DIM
            )
            i = j
        clusters, ids, mat = clusters[:i], ids[:i], mat[:i]
        rows = range(i)
        if i:
            self._ids = ids
            self._mat = mat
            starts = np.flatnonzero(np.r_[True, clusters[1:] != clusters[:-1]])
            ends = np.r_[starts[1:], len(rows)]
            self._bounds = {
                int(clusters[s]): (int(s), int(e)) for s, e in zip(starts, ends, strict=True)
            }
        else:
            self._ids = np.zeros(0, dtype=np.int64)
            self._mat = np.zeros((0, EMBED_DIM), dtype=np.int8)
            self._bounds = {}
        self._extra = {}
        self._stale = set()
        self._loaded_at = time.monotonic()

    def _refresh(self) -> None:
        now = time.monotonic()
        if self._loaded_at and now - self._checked_at < self.REFRESH_S:
            return
        self._checked_at = now
        if (
            not self._loaded_at
            or int(get_state(self.conn, "ivf.version", 0)) != self._version
            or now - self._loaded_at > self.FULL_RELOAD_S
        ):
            self._full_load()
            return
        cur = self._cursor()
        rows = cur.execute(
            "SELECT f.embedded_at, v.cluster, v.file_id, v.embedding FROM files f"
            " JOIN vec_ivf v ON v.file_id = f.id WHERE f.is_dir = 0 AND f.embedded_at > ?",
            (self._since,),
        ).fetchall()
        for ts, cl, fid, blob in rows:
            self._extra[int(fid)] = (int(cl), np.frombuffer(blob, dtype=np.int8))
            self._stale.add(int(fid))
            self._since = max(self._since, float(ts))
        if len(self._extra) > max(1000, len(self._ids) // 20):
            self._full_load()  # too many patches: rebuild the arrays

    def warm(self) -> None:
        self._refresh()

    def search(
        self, q: FloatArray, k: int = 100, *, min_sim: float = 0.0, allowed: set[int] | None = None
    ) -> list[tuple[int, float]]:
        self._refresh()
        c = self._centroids
        if c is None or not len(c):
            probe = list(self._bounds)
        else:
            probe = []
            covered = 0
            for cl in np.argsort(-(c @ q)).tolist():
                probe.append(int(cl))
                s, e = self._bounds.get(int(cl), (0, 0))
                covered += e - s
                if len(probe) >= MIN_PROBE and covered >= TARGET_SCAN:
                    break
        id_parts: list[np.ndarray] = []
        sim_parts: list[np.ndarray] = []
        qf = q.astype(np.float32)
        for cl in probe:
            s, e = self._bounds.get(cl, (0, 0))
            if e > s:
                id_parts.append(self._ids[s:e])
                sim_parts.append((self._mat[s:e].astype(np.float32) @ qf) / 127.0)
        probe_set = set(probe)
        extra = [(fid, v) for fid, (cl, v) in self._extra.items() if cl in probe_set]
        if extra:
            id_parts.append(np.array([f for f, _ in extra], dtype=np.int64))
            ev = np.stack([v for _, v in extra]).astype(np.float32)
            sim_parts.append((ev @ qf) / 127.0)
        if not id_parts:
            return []
        ids = np.concatenate(id_parts)
        sims = np.concatenate(sim_parts)
        if self._stale:
            # Base rows that were re-embedded since the load: the extra copy wins.
            keep = ~np.isin(ids, np.fromiter(self._stale, dtype=np.int64))
            n_extra = len(extra)
            if n_extra:
                keep[-n_extra:] = True
            ids, sims = ids[keep], sims[keep]
        if allowed is not None:
            mask = np.isin(ids, np.fromiter(allowed, dtype=np.int64))
            ids, sims = ids[mask], sims[mask]
        if not len(ids):
            return []
        top = np.argsort(-sims)[:k] if len(sims) <= k else np.argpartition(-sims, k)[:k]
        top = top[np.argsort(-sims[top])]
        return [(int(ids[i]), float(sims[i])) for i in top if sims[i] >= min_sim]

    def score_ids(
        self, q: FloatArray, ids: set[int], *, min_sim: float = 0.0
    ) -> list[tuple[int, float]]:
        """Exact scores for a small set (time-range queries): read from the DB."""
        return VectorIndex(self.conn).score_ids(q, ids, min_sim=min_sim)

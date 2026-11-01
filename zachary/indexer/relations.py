"""Precomputed "related files": top 10 per file, so the lookup is one indexed query.

Score = weighted sum of
- content: cosine of file vectors (candidates: the file's IVF cluster + nearest clusters)
- name:    shared words in the file names (Jaccard)
- location: same folder > same parent folder > same top folder
- coaccess: opened within ``COACCESS_WINDOW_S`` of each other (access_log)

``kind`` records the strongest signal ("content", "name", "location", "coaccess").
Work runs cluster by cluster as block matrix products (no N x N matrix).
Incremental updates also offer the new file to its neighbours' lists, keeping
every list a true top-10 without a full recompute.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from zachary.core.db import EMBED_DIM, get_state, set_state
from zachary.search.tokens import words
from zachary.search.vectors import load_centroids

TOP_K = 10
CANDIDATES = 40  # content candidates per file before mixing in other signals
NEIGHBOUR_CLUSTERS = 3  # also look in this many nearest other clusters
COACCESS_WINDOW_S = 30 * 60
WEIGHTS = {"content": 0.55, "name": 0.2, "location": 0.15, "coaccess": 0.1}
MIN_SCORE = 0.12


@dataclass(slots=True)
class RelationStats:
    files: int = 0
    links: int = 0
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class Related:
    file_id: int
    path: str
    name: str
    kind: str  # file kind
    score: float
    reason: str  # content | name | location | coaccess


def _name_words(name: str) -> frozenset[str]:
    dot = name.rfind(".")
    stem = name[:dot] if dot > 0 else name
    return frozenset(w for w in words(stem) if len(w) > 1 and not w.isdigit())


def _location(a: str, b: str) -> float:
    da, db = os.path.dirname(a), os.path.dirname(b)
    if da == db:
        return 1.0
    if os.path.dirname(da) == os.path.dirname(db):
        return 0.5
    common = os.path.commonpath([da, db]) if da and db else ""
    depth = max(da.count(os.sep), 1)
    return 0.25 * (common.count(os.sep) / depth) if common else 0.0


def _coaccess(conn: sqlite3.Connection) -> dict[int, dict[int, float]]:
    """file -> partner -> strength in [0, 1], from opens within the time window."""
    rows = conn.execute(
        "SELECT file_id, ts FROM access_log WHERE source != 'edit' ORDER BY ts"
    ).fetchall()
    counts: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    j = 0
    for i, (fa, ta) in enumerate(rows):
        while rows[j][1] < ta - COACCESS_WINDOW_S:
            j += 1
        for fb, _tb in rows[j:i]:
            if fb != fa:
                counts[fa][fb] += 1
                counts[fb][fa] += 1
    return {
        a: {b: min(1.0, n / 3.0) for b, n in partners.items()} for a, partners in counts.items()
    }


class RelationBuilder:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _vectors(self, cluster_ids: list[int]) -> tuple[list[int], np.ndarray]:
        marks = ",".join("?" * len(cluster_ids))
        rows = self.conn.execute(
            f"SELECT file_id, embedding FROM vec_ivf WHERE cluster IN ({marks})", cluster_ids
        ).fetchall()
        if not rows:
            return [], np.zeros((0, EMBED_DIM), dtype=np.float32)
        m = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.int8).reshape(len(rows), -1)
        return [r[0] for r in rows], m.astype(np.float32) / 127.0

    def compute(self, file_ids: Iterable[int] | None = None) -> RelationStats:
        """Recompute top-k lists for ``file_ids`` (default: every embedded file)."""
        t0 = time.perf_counter()
        st = RelationStats()
        want: set[int] | None = None if file_ids is None else set(file_ids)
        clusters = [
            r[0] for r in self.conn.execute("SELECT DISTINCT cluster FROM vec_ivf ORDER BY cluster")
        ]
        if not clusters:
            return st
        cent = load_centroids(self.conn)
        if want is not None:
            marks = ",".join("?" * len(want)) or "NULL"
            touched = {
                r[0]
                for r in self.conn.execute(
                    f"SELECT DISTINCT cluster FROM vec_ivf WHERE file_id IN ({marks})", list(want)
                )
            }
            clusters = [c for c in clusters if c in touched]
        info = {
            r[0]: (r[1], r[2], r[3])
            for r in self.conn.execute(
                "SELECT id, path, name, parent_id FROM files WHERE is_dir = 0"
            )
        }
        co = _coaccess(self.conn)
        results: dict[int, list[tuple[int, float, str]]] = {}
        for cl in clusters:
            if cent is not None and len(cent):
                near = np.argsort(-(cent @ cent[cl]))[: NEIGHBOUR_CLUSTERS + 1].tolist()
                pool_clusters = sorted({cl, *near})
            else:
                pool_clusters = [cl]
            src_ids, src_m = self._vectors([cl])
            if want is not None:
                keep = [i for i, f in enumerate(src_ids) if f in want]
                src_ids = [src_ids[i] for i in keep]
                src_m = src_m[keep]
            if not src_ids:
                continue
            pool_ids, pool_m = self._vectors(pool_clusters)
            sims = src_m @ pool_m.T
            for row, fid in enumerate(src_ids):
                results[fid] = self._rank(fid, pool_ids, sims[row], info, co)
            st.files += len(src_ids)
        st.links = self._write(results, symmetric=want is not None)
        if want is None:
            set_state(self.conn, "relations.full_at", time.time())
        set_state(self.conn, "relations.at", time.time())
        st.seconds = time.perf_counter() - t0
        return st

    def _rank(
        self,
        fid: int,
        pool_ids: list[int],
        sims: np.ndarray,
        info: dict[int, tuple[str, str, int | None]],
        co: dict[int, dict[int, float]],
    ) -> list[tuple[int, float, str]]:
        me = info.get(fid)
        if me is None:
            return []
        k = min(CANDIDATES + 1, len(pool_ids))
        top = np.argpartition(-sims, k - 1)[:k] if len(pool_ids) > k else np.arange(len(pool_ids))
        cands: dict[int, float] = {pool_ids[i]: float(sims[i]) for i in top if pool_ids[i] != fid}
        for partner in co.get(fid, {}):
            cands.setdefault(partner, 0.0)
        my_words = _name_words(me[1])
        scored: list[tuple[int, float, str]] = []
        for other, cos in cands.items():
            o = info.get(other)
            if o is None:
                continue
            ow = _name_words(o[1])
            parts = {
                "content": max(0.0, (cos - 0.2) / 0.8),
                "name": len(my_words & ow) / len(my_words | ow) if my_words and ow else 0.0,
                "location": _location(me[0], o[0]),
                "coaccess": co.get(fid, {}).get(other, 0.0),
            }
            contrib = {k: WEIGHTS[k] * v for k, v in parts.items()}
            score = sum(contrib.values())
            if score >= MIN_SCORE:
                scored.append((other, score, max(contrib, key=lambda k: contrib[k])))
        scored.sort(key=lambda x: -x[1])
        return scored[:TOP_K]

    def _write(self, results: dict[int, list[tuple[int, float, str]]], *, symmetric: bool) -> int:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            links = 0
            ids = list(results)
            for i in range(0, len(ids), 900):
                chunk = ids[i : i + 900]
                conn.execute(
                    f"DELETE FROM relations WHERE src_id IN ({','.join('?' * len(chunk))})", chunk
                )
            rows = [
                (src, dst, score, kind) for src, lst in results.items() for dst, score, kind in lst
            ]
            conn.executemany(
                "INSERT OR REPLACE INTO relations(src_id, dst_id, score, kind) VALUES (?, ?, ?, ?)",
                rows,
            )
            links = len(rows)
            if symmetric:
                # Offer each new link to the other side's list; keep that list at top-k.
                touched: set[int] = set()
                for src, dst, score, kind in rows:
                    if dst in results:
                        continue
                    floor = conn.execute(
                        "SELECT count(*), min(score) FROM relations WHERE src_id = ?", (dst,)
                    ).fetchone()
                    if floor[0] < TOP_K or score > float(floor[1]):
                        conn.execute(
                            "INSERT OR REPLACE INTO relations(src_id, dst_id, score, kind)"
                            " VALUES (?, ?, ?, ?)",
                            (dst, src, score, kind),
                        )
                        touched.add(dst)
                for dst in touched:
                    # relations is WITHOUT ROWID: trim by the k-th best score.
                    conn.execute(
                        "DELETE FROM relations WHERE src_id = ? AND score < (SELECT score"
                        " FROM relations WHERE src_id = ? ORDER BY score DESC LIMIT 1 OFFSET ?)",
                        (dst, dst, TOP_K - 1),
                    )
            conn.execute("COMMIT")
            return links
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def related(conn: sqlite3.Connection, file_id: int, limit: int = TOP_K) -> list[Related]:
    """The precomputed related files (one indexed lookup)."""
    rows = conn.execute(
        "SELECT r.dst_id, f.path, f.name, f.kind, max(r.score), r.kind FROM relations r"
        " JOIN files f ON f.id = r.dst_id WHERE r.src_id = ?"
        " GROUP BY r.dst_id ORDER BY max(r.score) DESC LIMIT ?",
        (file_id, limit),
    ).fetchall()
    return [Related(r[0], r[1], r[2], r[3], float(r[4]), r[5]) for r in rows]


def due_full(conn: sqlite3.Connection, max_age_s: float = 7 * 86400) -> bool:
    return time.time() - float(get_state(conn, "relations.full_at", 0)) > max_age_s

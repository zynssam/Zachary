from __future__ import annotations

import sqlite3

import numpy as np

from zachary.core.db import open_db
from zachary.indexer.embedder import quantize
from zachary.search import vectors


def _fill(conn: sqlite3.Connection, x: np.ndarray) -> None:
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO files(id, path, name) VALUES (?, ?, ?)",
        ((i + 1, f"/f{i}", f"f{i}") for i in range(len(x))),
    )
    conn.executemany(
        "INSERT INTO vec_ivf(file_id, cluster, embedding) VALUES (?, 0, ?)",
        ((i + 1, quantize(x[i])) for i in range(len(x))),
    )
    conn.execute("COMMIT")


def _data(n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    c = rng.standard_normal((60, 256)).astype(np.float32)
    x = c[rng.integers(0, 60, n)] + 0.05 * rng.standard_normal((n, 256)).astype(np.float32)
    out: np.ndarray = x / np.linalg.norm(x, axis=1, keepdims=True)
    return out


def test_small_index_is_exact(tmp_path) -> None:  # type: ignore[no-untyped-def]
    conn = open_db(tmp_path / "v.db")
    rng = np.random.default_rng(3)
    x = rng.standard_normal((300, 256)).astype(np.float32)  # no near-duplicates
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    _fill(conn, x)
    st = vectors.build(conn)
    assert st.clusters == 1
    idx = vectors.VectorIndex(conn)
    got = idx.search(x[7], k=5)
    assert got[0][0] == 8 and got[0][1] > 0.99
    assert next(f for f, _ in idx.score_ids(x[7], {8, 9, 10})) == 8


def test_ivf_recall_and_rebuild_policy(tmp_path) -> None:  # type: ignore[no-untyped-def]
    conn = open_db(tmp_path / "v.db")
    x = _data(5000)
    _fill(conn, x)
    assert vectors.needs_rebuild(conn)
    st = vectors.build(conn)
    assert st.clusters >= 16 and not vectors.needs_rebuild(conn)
    idx = vectors.VectorIndex(conn)
    q8 = np.frombuffer(b"".join(quantize(v) for v in x), dtype=np.int8).reshape(len(x), -1)
    xq = q8.astype(np.float32) / 127
    rng = np.random.default_rng(1)
    recall = []
    for i in rng.choice(len(x), 40, replace=False):
        q = x[i]
        exact = set((np.argsort(-(xq @ q))[:10] + 1).tolist())
        got = {f for f, _ in idx.search(q, k=10)}
        recall.append(len(exact & got) / 10)
    assert np.mean(recall) >= 0.95
    # new vectors join an existing cluster without a rebuild
    vectors.put(conn, 1, quantize(x[0]), vectors.load_centroids(conn))


def test_memory_index_matches_and_stays_fresh(tmp_path) -> None:  # type: ignore[no-untyped-def]
    conn = open_db(tmp_path / "v.db")
    x = _data(4000)
    _fill(conn, x)
    conn.execute("UPDATE files SET embedded_at = 1")
    vectors.build(conn)
    disk = vectors.VectorIndex(conn)
    mem = vectors.MemoryVectorIndex(conn)
    mem.REFRESH_S = 0.0
    q = x[11]
    assert [f for f, _ in mem.search(q, 10)] == [f for f, _ in disk.search(q, 10)]
    # A file re-embedded with a new vector: the fresh copy wins without a full reload.
    new_vec = x[2000]
    vectors.put(conn, 12, quantize(new_vec), vectors.load_centroids(conn))
    conn.execute("UPDATE files SET embedded_at = 2 WHERE id = 12")
    got = dict(mem.search(new_vec, 5))
    assert 12 in got and got[12] > 0.99
    assert 12 not in dict(mem.search(x[11], 3)) or dict(mem.search(x[11], 3))[12] < 0.99
    # A rebuild (new version) reloads everything.
    vectors.build(conn)
    assert dict(mem.search(new_vec, 5)).get(12, 0) > 0.99
    assert {f for f, _ in mem.search(q, 10)} == {f for f, _ in disk.search(q, 10)}

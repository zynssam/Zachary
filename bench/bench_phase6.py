"""Phase 6 benchmark: "ask your files" retrieval on the 200k-file index.

    python bench/bench_phase6.py --db ~/.cache/zachary-bench/bench3.db

The DB is copied first. The Phase 3 bench DB has file vectors but no text chunks,
so this adds synthetic chunks (~3 per document, ~1,000 characters, real model
vectors for a sample and random vectors for the rest) plus one fee receipt, then
times question -> passages (hybrid search + chunk scoring), the part that runs on
every question (and on every keystroke while typing one). The LLM call itself is
network time and isn't part of this.
"""

from __future__ import annotations

import argparse
import random
import shutil
import statistics
import time
from pathlib import Path

import numpy as np

from zachary.brain import qa
from zachary.core.config import Config
from zachary.core.db import open_db
from zachary.core.platform import get_platform
from zachary.indexer.embedder import Model2VecEmbedder, normalize, quantize
from zachary.search.vectors import MemoryVectorIndex, SemanticSearch

WORDS = (
    "semester exam syllabus lecture notes assignment project report budget invoice payment "
    "tuition hostel library schedule deadline meeting summary chapter unit algorithm data "
    "network database operating system marks result attendance internship resume travel "
    "booking ticket hotel recipe insurance policy claim warranty receipt account balance"
).split()
FEE = (
    "Fee Receipt - Semester 6. Tuition fee Rs 1,00,000 received. The next installment "
    "is due on 15 January 2027. A late fee applies after the due date."
)
QUESTIONS = [
    "when is my fee due",
    "how much tuition did I pay",
    "what is the hostel fee",
    "when is the internship deadline",
    "what was the exam result",
    "how much is the insurance claim",
    "what is the library schedule",
    "fee kab bharna hai",
]


def add_chunks(conn: object, emb: Model2VecEmbedder, per_doc: int, embed_sample: int) -> int:
    import sqlite3

    assert isinstance(conn, sqlite3.Connection)
    rng = random.Random(6)
    sql = "SELECT id FROM files WHERE kind = 'document' AND is_dir = 0"
    docs = [r[0] for r in conn.execute(sql)]
    texts, ids = [], []
    for fid in docs:
        for o in range(per_doc):
            texts.append(" ".join(rng.choice(WORDS) for _ in range(140))[:1000])
            ids.append((fid, o))
    vecs = np.random.default_rng(6).standard_normal((len(texts), emb.dim)).astype(np.float32)
    k = min(embed_sample, len(texts))
    vecs[:k] = emb.encode(texts[:k])
    vecs = normalize(vecs)
    conn.execute("BEGIN")
    for (fid, o), t, v in zip(ids, texts, vecs, strict=True):
        cur = conn.execute(
            "INSERT INTO chunks(file_id, ord, text_excerpt, char_start, char_end)"
            " VALUES (?,?,?,?,?)",
            (fid, o, t, o * 1000, o * 1000 + len(t)),
        )
        conn.execute(
            "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, vec_int8(?))",
            (cur.lastrowid, quantize(v)),
        )
    # One real fee receipt, named so search finds it.
    fid = docs[len(docs) // 2]
    conn.execute("UPDATE files SET name = 'Fee Receipt Sem 6.pdf' WHERE id = ?", (fid,))
    conn.execute(
        "UPDATE files_fts SET name = 'Fee Receipt Sem 6.pdf', title = 'Fee Receipt',"
        " keywords = 'fee installment due tuition' WHERE rowid = ?",
        (fid,),
    )
    conn.execute("DELETE FROM chunks WHERE file_id = ?", (fid,))
    cur = conn.execute(
        "INSERT INTO chunks(file_id, ord, text_excerpt, char_start, char_end) VALUES (?,0,?,0,?)",
        (fid, FEE, len(FEE)),
    )
    conn.execute(
        "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, vec_int8(?))",
        (cur.lastrowid, quantize(normalize(emb.encode([FEE]))[0])),
    )
    conn.execute("COMMIT")
    return len(texts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--work", type=Path, default=None)
    ap.add_argument("--per-doc", type=int, default=3)
    ap.add_argument("--embed-sample", type=int, default=20000)
    ap.add_argument(
        "--models", type=Path, default=Path.home() / ".cache" / "zachary-bench" / "models"
    )
    args = ap.parse_args()
    work = args.work or args.db.parent
    db = work / "bench6.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    shutil.copyfile(args.db, db)
    conn = open_db(db)
    emb = Model2VecEmbedder("minishlab/potion-base-8M", args.models)
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM vec_chunks")
    t = time.perf_counter()
    n = add_chunks(conn, emb, args.per_doc, args.embed_sample)
    files = conn.execute("SELECT count(*) FROM files").fetchone()[0]
    print(f"[setup] {files} files, {n} chunks added in {time.perf_counter() - t:.0f}s")

    cfg = Config(conn, get_platform())
    index = MemoryVectorIndex(conn)
    index.warm()
    sem = SemanticSearch(emb, index)
    rules = cfg.path_rules()
    top = qa.retrieve(conn, "when is my fee due", semantic=sem, rules=rules)
    print(f"[check] top passages: {[(p.name, round(p.score, 2)) for p in top[:3]]}")
    for label, s in (("with meaning search", sem), ("keywords only", None)):
        ms: list[float] = []
        for _ in range(3):
            for q in QUESTIONS:
                t0 = time.perf_counter()
                qa.retrieve(conn, q, semantic=s, rules=rules)
                ms.append((time.perf_counter() - t0) * 1e3)
        ms.sort()
        p95 = ms[int(len(ms) * 0.95) - 1]
        print(f"[retrieve, {label}] p50 {statistics.median(ms):.1f} ms  p95 {p95:.1f} ms  "
              f"max {ms[-1]:.1f} ms  ({len(ms)} questions)")  # fmt: skip


if __name__ == "__main__":
    main()

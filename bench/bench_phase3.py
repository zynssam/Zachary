"""Phase 3 benchmarks: semantic memory with the real Model2Vec model.

    python bench/make_corpus.py ~/.cache/zachary-bench/corpus --files 2000
    python bench/bench_phase3.py --corpus ~/.cache/zachary-bench/corpus \
        --big-db ~/.cache/zachary-bench/bench.db

A. Content corpus (real text in PDF/DOCX/PPTX/XLSX/MD/TXT/PY): first-run
   extraction + embedding throughput, re-save (unchanged) cost, vector index,
   relations, versions, "related" lookup and meaning-search latency.
B. 200k-file index (from bench_phase1, copied first): name/path vectors for
   every file, IVF build, full relations pass, search p95 *with* meaning search.
"""

from __future__ import annotations

import argparse
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

from zachary.core.config import Config
from zachary.core.db import open_db
from zachary.core.platform import get_platform
from zachary.indexer import relations, versions
from zachary.indexer.embedder import Model2VecEmbedder
from zachary.indexer.scanner import scan
from zachary.indexer.semantic import EmbedStats, SemanticIndexer
from zachary.indexer.store import FileStore
from zachary.search import vectors
from zachary.search.hybrid import search
from zachary.search.vectors import SemanticSearch, VectorIndex

sys.path.insert(0, str(Path(__file__).parent))
from bench_phase1 import QUERIES

MEANING_QUERIES = [
    "show me everything on data structures",
    "when is the tuition fee due",
    "holiday itinerary",
    "how to cook paneer",
    "process scheduling and deadlocks",
    "gradient descent training",
    "how does tcp work",
    "cell biology notes",
    "linked list code",
    "budget for rent and groceries",
]


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))]


def fmt(xs: list[float]) -> str:
    return f"p50 {pct(xs, 50):.2f} ms | p95 {pct(xs, 95):.2f} ms | max {max(xs):.2f} ms"


def embed_all(sem: SemanticIndexer, batch: int = 64) -> tuple[EmbedStats, float]:
    agg = EmbedStats()
    t0 = time.perf_counter()
    while True:
        window = sem.pending(512)
        if not window:
            break
        for i in range(0, len(window), batch):
            st = sem.process(window[i : i + batch])
            agg.files += st.files
            agg.extracted += st.extracted
            agg.reused += st.reused
            agg.name_only += st.name_only
            agg.chunks += st.chunks
            agg.bytes += st.bytes
            agg.per_kind.update(st.per_kind)
    return agg, time.perf_counter() - t0


def fresh_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def part_a(corpus: Path, work: Path, models: Path) -> None:
    print("== A. content corpus ==")
    db = work / "corpus.db"
    fresh_db(db)
    conn = open_db(db)
    cfg = Config(conn, get_platform())
    cfg.seed_defaults()
    cfg.set_index_roots([corpus])
    store = FileStore(conn, cfg)
    scan(store)
    emb = Model2VecEmbedder("minishlab/potion-base-8M", models)
    t = time.perf_counter()
    emb.encode(["warm"])
    print(f"model load: {(time.perf_counter() - t) * 1e3:.0f} ms")
    sem = SemanticIndexer(conn, cfg, emb)
    st, secs = embed_all(sem)
    print(
        f"[embed] first run: {st.files} files ({dict(st.per_kind)}) in {secs:.1f}s = "
        f"{st.files / secs:.0f} files/s, {st.bytes / 1e6 / secs:.2f} MB text/s; "
        f"{st.chunks} passages, {st.extracted} with text"
    )
    # Re-save everything unchanged (mtime bump): nothing should be re-embedded.
    later = time.time()  # "saved again now" (not in the future)
    for p in corpus.rglob("*"):
        if p.is_file():
            os.utime(p, (later, later))
    scan(store)
    st2, secs2 = embed_all(sem)
    print(
        f"[resave] {st2.files} touched files: extracted {st2.extracted}, reused {st2.reused} "
        f"in {secs2:.1f}s ({st2.files / secs2:.0f} files/s)"
    )
    b = vectors.build(conn)
    r = relations.RelationBuilder(conn).compute(None)
    t = time.perf_counter()
    g = versions.rebuild(conn)
    print(
        f"[maint] IVF {b.vectors} vectors/{b.clusters} clusters {b.seconds:.2f}s; relations "
        f"{r.links} links {r.seconds:.1f}s; versions {g} groups {time.perf_counter() - t:.2f}s"
    )
    ids = [row[0] for row in conn.execute("SELECT id FROM files WHERE is_dir = 0 LIMIT 500")]
    lat = []
    for fid in ids:
        t = time.perf_counter()
        relations.related(conn, fid)
        lat.append((time.perf_counter() - t) * 1e3)
    print(f"[related] lookup ({len(lat)} files): {fmt(lat)}   (budget < 10 ms)")
    sem_s = SemanticSearch(emb, VectorIndex(conn))
    ms = []
    for _ in range(10):
        for q in MEANING_QUERIES:
            ms.append(search(conn, q, semantic=sem_s).took_ms)
    print(f"[search] meaning queries: {fmt(ms)}")
    for q in MEANING_QUERIES[:4]:
        top = search(conn, q, semantic=sem_s).results[:3]
        print(f"         {q!r:40} -> {[x.name.split('_')[0] for x in top]}")
    conn.close()


def part_b(big_db: Path, work: Path, models: Path) -> None:
    print("\n== B. 200k-file index ==")
    db = work / "bench3.db"
    fresh_db(db)
    conn0 = open_db(big_db)
    conn0.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn0.close()
    shutil.copyfile(big_db, db)
    conn = open_db(db)  # migrates to v3
    cfg = Config(conn, get_platform())
    emb = Model2VecEmbedder("minishlab/potion-base-8M", models)
    sem = SemanticIndexer(conn, cfg, emb)
    n = sem.pending_count()
    st, secs = embed_all(sem, batch=256)
    print(f"[embed] {st.files} files (name/path vectors; tree files are empty) in {secs:.1f}s "
          f"= {st.files / secs:,.0f} files/s  (pending at start: {n})")  # fmt: skip
    b = vectors.build(conn)
    print(f"[ivf] {b.vectors} vectors -> {b.clusters} clusters in {b.seconds:.1f}s")
    r = relations.RelationBuilder(conn).compute(None)
    print(f"[relations] full pass: {r.files} files, {r.links} links in {r.seconds:.1f}s")
    t = time.perf_counter()
    g = versions.rebuild(conn)
    print(f"[versions] {g} groups in {time.perf_counter() - t:.2f}s")
    sem_s = SemanticSearch(emb, VectorIndex(conn))
    for q in QUERIES:
        search(conn, q, semantic=sem_s)
    ms: list[float] = []
    for _ in range(20):
        for q in QUERIES:
            ms.append(search(conn, q, semantic=sem_s).took_ms)
    print(
        f"[search] keyword+meaning, {len(QUERIES)} queries x 20: {fmt(ms)}   (budget p95 < 50 ms)"
    )
    ids = [row[0] for row in conn.execute("SELECT id FROM files WHERE is_dir = 0 LIMIT 1000")]
    lat = []
    for fid in ids:
        t = time.perf_counter()
        relations.related(conn, fid)
        lat.append((time.perf_counter() - t) * 1e3)
    print(f"[related] lookup ({len(lat)} files): {fmt(lat)}   (budget < 10 ms)")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    print(
        f"[db] {big_db.stat().st_size / 1e6:.0f} MB -> "
        f"{db.stat().st_size / 1e6:.0f} MB with vectors"
    )
    print(f"mean search {statistics.mean(ms):.1f} ms")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--big-db", type=Path, default=None)
    ap.add_argument(
        "--models", type=Path, default=Path.home() / ".cache" / "zachary-bench" / "models"
    )
    args = ap.parse_args()
    work = args.corpus.parent
    part_a(args.corpus, work, args.models)
    if args.big_db:
        part_b(args.big_db, work, args.models)


if __name__ == "__main__":
    main()

"""Phase 1 benchmarks on a synthetic tree (see make_tree.py).

    python bench/make_tree.py ~/.cache/zachary-bench/home
    python bench/bench_phase1.py ~/.cache/zachary-bench/home

Measures: first metadata scan, catch-up (re-scan) unchanged and after offline
changes, search latency p50/p95/p99 over a mixed query set, projects/recent
lookups, live "new file -> searchable" latency with the watcher on the whole
tree, and DB size. The DB lives next to the tree (real disk, not tmpfs).
"""

from __future__ import annotations

import argparse
import os
import random
import resource
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from zachary.core.config import Config
from zachary.core.db import open_db
from zachary.core.platform import get_platform
from zachary.indexer import projects
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore
from zachary.indexer.watcher import Watcher
from zachary.search.hybrid import recent, search

QUERIES = [
    "fees yesterday",
    "fee receipt",
    "Fee Receipt Sem 5",
    "the fee document I opened yesterday",
    "kal wali fees file",
    "pichhle hafte ka report",
    "hostel fess",  # typo
    "recipt",  # typo, needs correction
    "linked list",
    "linked list code",
    "binary tree",
    "report",
    "final report",
    "notes last week",
    "assignment sem 5",
    "lab report physics",
    "exam timetable",
    "invoice",
    "bank statement",
    "tax 2024",
    "resume",
    "screenshot",
    "photo last month",
    "python notebook",
    "dataset training results",
    "what did I open today",
    "yesterday",
    "last tuesday",
    "3 days ago",
    "rep",  # as-you-type prefixes
    "hoste",
    "lin",
    "a",
    "data structures",
    "operating system lecture",
    "network database",
    "ninne fee",
    "mangalvar ko kholi thi",
    "budget xlsx",
    "proposal draft v2",
]


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return s[k]


def fmt(xs: list[float]) -> str:
    return f"p50 {pct(xs, 50):.2f} ms | p95 {pct(xs, 95):.2f} ms | p99 {pct(xs, 99):.2f} ms"


def rss_mb() -> float:
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return kb / 1024 if sys.platform != "darwin" else kb / 1024 / 1024


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tree", type=Path)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--runs", type=int, default=30, help="runs per query")
    ap.add_argument("--live", type=int, default=20, help="new-file latency samples")
    ap.add_argument("--debounce", type=float, default=1.5)
    args = ap.parse_args()
    tree = args.tree.resolve()
    db_path = args.db or tree.parent / "bench.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    rng = random.Random(7)
    churn = tree / "Churn"
    shutil.rmtree(churn, ignore_errors=True)
    churn.mkdir()
    for i in range(500):
        (churn / f"churn_{i:03d}.txt").write_text("c")

    conn = open_db(db_path)
    cfg = Config(conn, get_platform())
    cfg.seed_defaults()
    cfg.set_index_roots([tree])
    store = FileStore(conn, cfg)
    print(f"tree: {tree}\ndb:   {db_path}\n")

    # 1. first scan
    t0 = time.perf_counter()
    st = scan(store)
    t_scan = time.perf_counter() - t0
    t0 = time.perf_counter()
    n_proj = projects.apply_markers(store, st.markers, scope=None)
    t_proj = time.perf_counter() - t0
    files = st.files
    print(
        f"[scan] first metadata scan: {files:,} files, {st.dirs:,} folders, "
        f"{st.skipped_excluded:,} excluded entries skipped"
    )
    print(f"       {t_scan:.2f} s ({files / t_scan:,.0f} files/s), peak RSS {rss_mb():.0f} MB")
    print(f"[proj] project detection: {n_proj} projects in {t_proj * 1e3:.0f} ms")

    # 2. catch-up, nothing changed
    t0 = time.perf_counter()
    r = scan(store).result
    t_noop = time.perf_counter() - t0
    print(
        f"[catchup] unchanged re-scan (Linux mtime diff): {t_noop:.2f} s "
        f"(+{r.inserted} ~{r.updated} >{r.moved} -{r.deleted})"
    )

    # 3. catch-up after offline changes
    ids_before = {
        p: i for i, p in conn.execute("SELECT id, path FROM files WHERE path LIKE ?",
                                      (str(churn) + "%",))
    }  # fmt: skip
    all_files = [str(p) for (p,) in conn.execute("SELECT path FROM files WHERE is_dir = 0")]
    later = time.time() + 10
    for p in rng.sample(all_files, 500):
        if "Churn" not in p:
            os.utime(p, (later, later))
    for i in range(100):
        (churn / f"churn_{i:03d}.txt").unlink()
    renamed = {}
    for i in range(100, 200):
        src = churn / f"churn_{i:03d}.txt"
        dst = churn / f"renamed_{i:03d}.txt"
        src.rename(dst)
        renamed[str(dst)] = ids_before[str(src)]
    for i in range(300):
        (churn / f"new_{i:03d}.txt").write_text("n")
    t0 = time.perf_counter()
    r = scan(store).result
    t_change = time.perf_counter() - t0
    kept = sum(1 for p, i in renamed.items() if (row := store.get_row(p)) and row.id == i)
    print(
        f"[catchup] after 1,000 offline changes: {t_change:.2f} s "
        f"(+{r.inserted} ~{r.updated} >{r.moved} -{r.deleted}); "
        f"renames kept identity: {kept}/100"
    )

    # 4. access history: 5,000 OS-recent opens over 60 days; the fee receipt yesterday
    now = time.time()
    picks = rng.sample(all_files, 5000)
    store.record_access([(p, now - rng.uniform(0, 86400 * 60)) for p in picks], "os_recent")
    fee = str(tree / "College" / "Sem5" / "Fees" / "Fee Receipt Sem 5.pdf")
    y = (datetime.now() - timedelta(days=1)).replace(hour=14, minute=5).timestamp()
    store.record_access([(fee, y)], "os_recent")

    # 5. search latency
    for q in QUERIES:  # warm-up
        search(conn, q)
    all_ms: list[float] = []
    per_q: dict[str, list[float]] = {}
    for _ in range(args.runs):
        for q in QUERIES:
            ms = search(conn, q).took_ms
            all_ms.append(ms)
            per_q.setdefault(q, []).append(ms)
    print(f"\n[search] {len(QUERIES)} queries x {args.runs} runs = {len(all_ms)} searches")
    print(f"         {fmt(all_ms)} | max {max(all_ms):.2f} ms   (budget: p95 < 50 ms)")
    worst = sorted(per_q.items(), key=lambda kv: -pct(kv[1], 95))[:5]
    for q, xs in worst:
        print(f"         slowest: {q!r:42} p95 {pct(xs, 95):.2f} ms")
    top = search(conn, "fees yesterday").results
    print(
        f"         'fees yesterday' -> {top[0].name if top else None!r}"
        f" ({'; '.join(top[0].why) if top else ''})"
    )

    # 6. projects / recent lookups
    pj = []
    rc = []
    for _ in range(50):
        t0 = time.perf_counter()
        projects.list_projects(conn)
        pj.append((time.perf_counter() - t0) * 1e3)
        t0 = time.perf_counter()
        recent(conn, limit=20)
        rc.append((time.perf_counter() - t0) * 1e3)
    print(f"[projects] list all: {fmt(pj)}")
    print(f"[recent]   top 20:   {fmt(rc)}")

    # 7. live: new file -> searchable, with the watcher on the whole tree
    n_dirs = conn.execute("SELECT count(*) FROM files WHERE is_dir = 1").fetchone()[0]
    lim = get_platform().check_watch_limit(int(n_dirs))
    print(f"\n[watch] {n_dirs:,} folders to watch; limit {lim.current}; ok={lim.ok}")
    w = Watcher(store, debounce=args.debounce)
    t0 = time.perf_counter()
    w.start()
    print(f"[watch] watcher start (adding watches): {time.perf_counter() - t0:.2f} s")
    dirs = [str(p) for (p,) in conn.execute("SELECT path FROM files WHERE is_dir = 1")]
    lat: list[float] = []
    for i in range(args.live):
        d = rng.choice(dirs)
        name = f"zqx live probe {i} {rng.getrandbits(32):x}.txt"
        t0 = time.perf_counter()
        Path(d, name).write_text("p")
        q = f"zqx live probe {i}"
        while time.perf_counter() - t0 < 10:
            if any(x.name == name for x in search(conn, q).results):
                break
            time.sleep(0.01)
        lat.append(time.perf_counter() - t0)
        Path(d, name).unlink()
    w.stop()
    print(
        f"[watch] new file -> searchable (debounce {args.debounce}s): "
        f"p50 {pct(lat, 50):.2f} s | p95 {pct(lat, 95):.2f} s | max {max(lat):.2f} s "
        f"(target ~2 s)"
    )

    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size = sum(
        Path(str(db_path) + s).stat().st_size
        for s in ("", "-wal")
        if Path(str(db_path) + s).exists()
    )
    rows = conn.execute("SELECT count(*) FROM files").fetchone()[0]
    print(f"\n[db] {rows:,} rows, {size / 1e6:.1f} MB on disk ({size / rows:.0f} B/row)")
    conn.close()
    shutil.rmtree(churn, ignore_errors=True)


if __name__ == "__main__":
    main()

"""Phase 7 benchmark: building clean-up / organize plans on the 200k-file tree.

    python bench/bench_phase7.py --home ~/.cache/zachary-bench/home \\
        --db ~/.cache/zachary-bench/bench.db

The synthetic tree's files are empty, so this adds ``--pairs`` duplicate pairs of
real 100 KB files (+ some temp files and empty folders) under ``home/BenchDups``,
indexes them into a copy of the DB, then times each detector and a whole plan.
Nothing is executed; the added files are removed at the end.
"""

from __future__ import annotations

import argparse
import os
import shutil
import time
from pathlib import Path

from zachary.actions import planner
from zachary.core.config import Config
from zachary.core.db import open_db
from zachary.core.platform import get_platform
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", type=Path, required=True)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--pairs", type=int, default=500)
    args = ap.parse_args()
    extra = args.home / "BenchDups"
    shutil.rmtree(extra, ignore_errors=True)
    old = time.time() - 120 * 86400
    for i in range(args.pairs):
        data = os.urandom(100 * 1024)
        for name in (f"doc{i}.pdf", f"doc{i} (1).pdf"):
            p = extra / f"d{i % 50}" / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            os.utime(p, (old, old))
    for i in range(50):
        (extra / f"d{i}" / f"~$lock{i}.docx").write_bytes(b"x")
        os.utime(extra / f"d{i}" / f"~$lock{i}.docx", (old, old))
        (extra / f"empty{i}").mkdir()
    db = args.db.parent / "bench7.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    shutil.copyfile(args.db, db)
    conn = open_db(db)
    cfg = Config(conn, get_platform())
    cfg.set_index_roots([args.home])
    t = time.perf_counter()
    scan(FileStore(conn, cfg))
    files = conn.execute("SELECT count(*) FROM files").fetchone()[0]
    print(f"[setup] {files} files indexed ({time.perf_counter() - t:.0f}s)")
    try:
        now = time.time()
        t = time.perf_counter()
        guard = planner.Guard(conn, cfg)
        print(f"[guard] {(time.perf_counter() - t) * 1e3:.0f} ms "
              f"({len(guard.software)} software folders)")  # fmt: skip
        for name, fn in (
            ("duplicates", lambda: planner.find_duplicates(conn, guard)),
            ("installers", lambda: planner.find_installers(conn, guard, now)),
            ("junk", lambda: planner.find_junk(conn, guard, now)),
            ("empty folders", lambda: planner.find_empty_folders(conn, guard)),
            ("large", lambda: planner.find_large(conn, guard, now)),
        ):
            t = time.perf_counter()
            found = fn()
            print(f"[{name}] {len(found)} found in {(time.perf_counter() - t) * 1e3:.0f} ms")
        t = time.perf_counter()
        plan = planner.clean_plan(conn, cfg)
        print(f"[whole clean plan] {len(plan.items)} items in {time.perf_counter() - t:.1f}s: "
              f"{plan.note}")  # fmt: skip
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM files WHERE is_dir = 0 AND kind = 'document' LIMIT 500")]  # fmt: skip
        t = time.perf_counter()
        org = planner.organize_plan(conn, cfg, ids, "Bench topic")
        print(f"[organize 500 files] {len(org.items)} moves in "
              f"{(time.perf_counter() - t) * 1e3:.0f} ms")  # fmt: skip
    finally:
        shutil.rmtree(extra, ignore_errors=True)


if __name__ == "__main__":
    main()

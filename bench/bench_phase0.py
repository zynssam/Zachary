"""Phase 0 timings: DB create/open and exclude-matcher throughput."""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

from zachary.core.config import Config
from zachary.core.db import connect, open_db


def ms(samples: list[float]) -> str:
    s = sorted(samples)
    p95 = s[int(len(s) * 0.95) - 1]
    return f"median {statistics.median(s) * 1e3:.2f} ms, p95 {p95 * 1e3:.2f} ms"


def main() -> None:
    create: list[float] = []
    reopen: list[float] = []
    with tempfile.TemporaryDirectory() as td:
        for i in range(30):
            p = Path(td) / f"z{i}.db"
            t = time.perf_counter()
            c = open_db(p)
            create.append(time.perf_counter() - t)
            c.close()
            t = time.perf_counter()
            c = open_db(p)
            reopen.append(time.perf_counter() - t)
            c.close()
        c = open_db(Path(td) / "cfg.db")
        cfg = Config(c)
        cfg.seed_defaults()
        m = cfg.exclude_matcher()
        names = [f"file_{i}.txt" for i in range(100_000)] + ["node_modules", "x.tmp"] * 1000
        t = time.perf_counter()
        for n in names:
            m.excludes_name(n, is_dir=n == "node_modules")
        name_ns = (time.perf_counter() - t) / len(names) * 1e9
        paths = [str(Path.home() / "Documents" / f"d{i % 50}" / f"f{i}.pdf") for i in range(50_000)]
        t = time.perf_counter()
        for p in paths:
            m.is_excluded(p, is_dir=False)
        path_us = (time.perf_counter() - t) / len(paths) * 1e6
        c.close()
        ro = connect(Path(td) / "cfg.db", readonly=True)
        ro.close()
    print(f"python {sys.version.split()[0]}")
    print(f"create new DB + migration #1 (30 runs): {ms(create)}")
    print(f"open existing DB + vec load (30 runs):  {ms(reopen)}")
    print(f"excludes_name (scandir hot path):        {name_ns:.0f} ns/entry")
    print(f"is_excluded (full path check):           {path_us:.2f} us/path")


if __name__ == "__main__":
    main()

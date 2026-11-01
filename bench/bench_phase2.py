"""Phase 2 benchmarks: quick bar latency and UI responsiveness on a large index.

    python bench/bench_phase2.py --db ~/.cache/zachary-bench/bench.db
    QT_QPA_PLATFORM=wayland python bench/bench_phase2.py --db ...   # on a real display

Measures, with the real Engine/QuickBar (nothing is opened):
- cold start: QApplication + engine + first open -> results painted
- open -> results painted (hotkey path), warm, N times
- keystroke -> results painted while typing real queries character by character
- longest event-loop stall while typing (5 ms heartbeat timer)
- intent parse time
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

T_PROCESS = time.perf_counter()

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from zachary.brain.intents import parse  # noqa: E402
from zachary.core.platform import get_platform  # noqa: E402
from zachary.ui.engine import Engine  # noqa: E402
from zachary.ui.quickbar import QuickBar  # noqa: E402

QUERIES = [
    "fees yesterday",
    "open fee receipt",
    "my projects",
    "linked list code",
    "what did I open last week",
    "kal wali fees file",
    "report final",
    "photos last month",
]


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))]


def fmt(xs: list[float]) -> str:
    return f"p50 {pct(xs, 50):.1f} ms | p95 {pct(xs, 95):.1f} ms | max {max(xs):.1f} ms"


def pump_until(app: QApplication, cond: object, timeout: float = 5.0) -> None:
    end = time.perf_counter() + timeout
    while not cond() and time.perf_counter() < end:  # type: ignore[operator]
        app.processEvents()
        time.sleep(0.0005)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--opens", type=int, default=50)
    args = ap.parse_args()

    t_app = time.perf_counter()
    app = QApplication(sys.argv[:1])
    engine = Engine(args.db, get_platform())
    engine.start()
    bar = QuickBar(engine)
    opened: list[float] = []
    bar.opened.connect(opened.append)
    print(f"platform: {app.platformName()}   db: {args.db}")
    t0 = time.perf_counter()
    bar.show_bar(t0=t0)  # cold: engine still opening the DB
    pump_until(app, lambda: opened)
    total = (time.perf_counter() - T_PROCESS) * 1e3
    print(
        f"[cold] process start -> first results painted: {total:.0f} ms"
        f" (imports {(t_app - T_PROCESS) * 1e3:.0f} ms, app+engine+first open "
        f"{(time.perf_counter() - t_app) * 1e3:.0f} ms)"
    )

    # Warm opens (the hotkey path): hide, then show with t0 at the "key press".
    opened.clear()
    for _ in range(args.opens):
        bar.hide()
        app.processEvents()
        bar.show_bar(t0=time.perf_counter())
        n = len(opened)
        pump_until(app, lambda n=n: len(opened) > n)  # type: ignore[misc]
    print(
        f"[open] hotkey -> results painted ({len(opened)} opens): {fmt(opened)}   (budget < 150 ms)"
    )

    # Typing: one key at a time; measure last key -> list repainted with the reply.
    stalls: list[float] = []
    last = [time.perf_counter()]

    def tick() -> None:
        now = time.perf_counter()
        stalls.append((now - last[0]) * 1e3)
        last[0] = now

    hb = QTimer()
    hb.timeout.connect(tick)
    hb.start(5)
    per_key: list[float] = []
    applied: list[float] = []
    bar.reply_applied.connect(lambda _r: applied.append(time.perf_counter()))
    for q in QUERIES:
        bar.edit.clear()
        pump_until(app, lambda: False, timeout=0.05)
        for i in range(1, len(q) + 1):
            applied.clear()
            t_key = time.perf_counter()
            bar.edit.setText(q[:i])
            pump_until(app, lambda: bool(applied), timeout=2.0)
            if applied:
                per_key.append((applied[-1] - t_key) * 1e3)
            pump_until(app, lambda: False, timeout=0.03)  # ~30 ms between keystrokes
    hb.stop()
    print(f"[type] keystroke -> results shown ({len(per_key)} keys): {fmt(per_key)}"
          f"   (includes {bar._debounce.interval()} ms debounce)")  # fmt: skip
    print(f"[ui]   event-loop heartbeat while typing: longest stall {max(stalls):.1f} ms, "
          f"p99 {pct(stalls, 99):.1f} ms (5 ms timer; > 50 ms would feel janky)")  # fmt: skip

    phrases = [*QUERIES, "open the second one", "show its folder", "the older one",
               "mere projects dikhao", "shwo my porjects"]  # fmt: skip
    t = time.perf_counter()
    for _ in range(200):
        for p in phrases:
            parse(p)
    us = (time.perf_counter() - t) / (200 * len(phrases)) * 1e6
    print(f"[intent] parse: {us:.0f} us per phrase")
    lat = engine.handler
    if lat is not None:
        ms = [lat.preview(q).ms for q in QUERIES for _ in range(10)]
        print(f"[engine] handler.preview (parse+search+build items): {fmt(ms)}")
    bar.hide()
    engine.stop()
    print(f"mean open {statistics.mean(opened):.1f} ms")


if __name__ == "__main__":
    main()

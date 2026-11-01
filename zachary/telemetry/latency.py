"""Local-only latency measurements (never sent anywhere).

Phase 2 records quick-bar timings; Phase 4 adds voice stages
(wake -> STT -> intent -> search -> TTS).
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import defaultdict, deque

log = logging.getLogger("zachary.latency")


class LatencyLog:
    def __init__(self, keep: int = 500) -> None:
        self._data: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=keep))
        self._lock = threading.Lock()

    def record(self, name: str, ms: float) -> None:
        with self._lock:
            self._data[name].append(ms)
        log.debug("%s %.1f ms", name, ms)

    def values(self, name: str) -> list[float]:
        with self._lock:
            return list(self._data.get(name, ()))

    def summary(self, name: str) -> dict[str, float]:
        xs = sorted(self.values(name))
        if not xs:
            return {}
        k = max(0, round(0.95 * (len(xs) - 1)))
        return {"n": len(xs), "p50": statistics.median(xs), "p95": xs[k], "max": xs[-1]}


LATENCY = LatencyLog()


def now_ms() -> float:
    return time.perf_counter() * 1e3


class Timeline:
    """Per-request stage timings, e.g. wake -> STT -> intent -> search -> TTS.

    ``mark(stage)`` records the time a stage finished; ``durations()`` gives each
    stage's duration from the previous mark. Logged and appended (as one JSON line)
    to a local file; never sent anywhere.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.t0 = time.perf_counter()
        self.marks: list[tuple[str, float]] = []
        self.info: dict[str, object] = {}

    def mark(self, stage: str, at: float | None = None) -> None:
        self.marks.append((stage, at if at is not None else time.perf_counter()))

    def stages(self) -> list[str]:
        return [stage for stage, _t in self.marks]

    def durations(self) -> dict[str, float]:
        out: dict[str, float] = {}
        prev = self.t0
        for stage, t in self.marks:
            out[stage] = round((t - prev) * 1e3, 1)
            prev = t
        return out

    def total_ms(self, since: str | None = None) -> float:
        start = self.t0
        if since is not None:
            for stage, t in self.marks:
                if stage == since:
                    start = t
                    break
        return round(((self.marks[-1][1] if self.marks else start) - start) * 1e3, 1)

    def finish(self, path: str | None = None) -> dict[str, object]:
        record: dict[str, object] = {
            "kind": self.kind,
            "ts": round(time.time(), 3),
            "stages_ms": self.durations(),
            **self.info,
        }
        for stage, ms in self.durations().items():
            LATENCY.record(f"{self.kind}.{stage}", ms)
        log.info(
            "%s latency %s",
            self.kind,
            " ".join(f"{k}={v:.0f}ms" for k, v in self.durations().items()),
        )
        if path:
            import json

            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        return record

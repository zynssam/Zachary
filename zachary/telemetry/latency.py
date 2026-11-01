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

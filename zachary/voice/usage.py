"""Local ledger of real calls to the paid speech API (Gnani has a limited quota).

Every real request appends one JSON line to ``<data dir>/gnani_usage.jsonl``:
time, endpoint, seconds of audio or characters of text, status. Nothing else
(no audio, no text, no key). ``zachary.cli voice usage`` summarises it.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()


class UsageLedger:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def record(self, endpoint: str, *, status: str, **amounts: Any) -> None:
        if self.path is None:
            return
        entry = {"ts": round(time.time(), 3), "endpoint": endpoint, "status": status, **amounts}
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def summary(self) -> dict[str, dict[str, float]]:
        s: dict[str, dict[str, float]] = {}
        for e in self.entries():
            d = s.setdefault(e["endpoint"], {"calls": 0, "audio_s": 0.0, "chars": 0, "errors": 0})
            d["calls"] += 1
            d["audio_s"] += float(e.get("audio_s", 0.0))
            d["chars"] += int(e.get("chars", 0))
            d["errors"] += int(e.get("status") != "ok")
        return s

"""The assistant's worker: owns a DB connection and the Handler on a QThread, so
the UI thread never touches SQLite or the disk.

Requests carry an increasing id. Previews (as-you-type) are coalesced: if newer
text arrived while one was queued, the stale one is skipped.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot

from zachary.actions.safe_actions import SafeActions
from zachary.brain import llm
from zachary.brain.handler import Handler, Reply
from zachary.brain.intents import needs_clipboard
from zachary.core.config import Config
from zachary.core.db import open_db
from zachary.core.platform import Platform
from zachary.search.semantic import local_semantic
from zachary.search.vectors import SemanticSearch

log = logging.getLogger(__name__)


class Engine(QObject):
    replied = Signal(int, object)  # request id, Reply
    failed = Signal(int, str)
    _request = Signal(int, str, object)

    def __init__(
        self,
        db_path: Path | str,
        platform: Platform,
        semantic_factory: Callable[[sqlite3.Connection, Config], SemanticSearch | None]
        | None = None,
    ) -> None:
        super().__init__()
        self.db_path = db_path
        self.platform = platform
        self.semantic_factory = semantic_factory or local_semantic
        self.handler: Handler | None = None
        self._lock = threading.Lock()
        self._latest_preview = 0
        self._next_id = 0
        self._thread = QThread()
        self._thread.setObjectName("zach-engine")
        self.moveToThread(self._thread)
        self._request.connect(self._on_request)
        self._thread.started.connect(self._init)

    # -- UI-thread API -----------------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._thread.quit()
        self._thread.wait(3000)

    def ask(self, op: str, payload: Any = None) -> int:
        """Queue a request; returns its id.

        ``op``: preview | submit | act | pause | approve | reject | undo_plan."""
        if op == "submit" and isinstance(payload, str) and needs_clipboard(payload):
            # Read on the UI thread (Qt's clipboard lives there), and only when asked to.
            payload = (payload, clipboard_text())
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            if op == "preview":
                self._latest_preview = rid
        self._request.emit(rid, op, payload)
        return rid

    # -- worker thread -------------------------------------------------------------------
    @Slot()
    def _init(self) -> None:
        conn = open_db(self.db_path)
        config = Config(conn, self.platform)
        factory = self.semantic_factory
        self.handler = Handler(
            conn,
            SafeActions(conn, self.platform),
            config,
            lambda: factory(conn, config),
            llm.Provider(config, Path(self.db_path).parent),
            self.platform,
        )
        _ = self.handler.semantic  # load the model now, not on the first keystroke
        self.handler._correct_names("warm up")  # build the name vocabulary now (~0.15 s)

    @Slot(int, str, object)
    def _on_request(self, rid: int, op: str, payload: Any) -> None:
        if op == "preview":
            with self._lock:
                if rid < self._latest_preview:
                    return  # newer text already queued
        h = self.handler
        if h is None:
            self.failed.emit(rid, "engine not ready")
            return
        try:
            reply: Reply
            if op == "preview":
                reply = h.preview(str(payload))
            elif op == "submit":
                if isinstance(payload, tuple):
                    reply = h.submit(str(payload[0]), clipboard=payload[1])
                else:
                    reply = h.submit(str(payload))
            elif op == "act":
                index, action = payload
                if action == "related":
                    reply = h.submit(f"related to #{int(index) + 1}")
                else:
                    reply = h.act(int(index), str(action))
            elif op == "approve":  # the review window's Approve button
                plan_id, select, dst = payload
                reply = h.approve_plan(int(plan_id), dict(select), dict(dst))
            elif op == "reject":
                reply = h.reject_plan(int(payload))
            elif op == "undo_plan":
                reply = h.undo_plan(int(payload))
            elif op == "pause":
                h.config.set_setting("indexing_paused", bool(payload))
                return
            else:
                self.failed.emit(rid, f"unknown op {op}")
                return
        except Exception as e:  # never kill the worker
            log.exception("request %s failed", op)
            self.failed.emit(rid, f"{type(e).__name__}: {e}")
            return
        self.replied.emit(rid, reply)


def clipboard_text() -> str:
    """Text last copied: Qt's clipboard, else ``wl-paste`` (Wayland only lets a focused
    window read the clipboard; the bar may be hidden when you speak)."""
    text = ""
    try:
        from PySide6.QtGui import QGuiApplication

        cb = QGuiApplication.clipboard()
        text = cb.text() if cb is not None else ""
    except Exception:
        log.debug("Qt clipboard unavailable", exc_info=True)
    if not text and sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY"):
        import shutil
        import subprocess

        exe = shutil.which("wl-paste")
        if exe:
            try:
                r = subprocess.run([exe, "--no-newline", "--type", "text/plain"],
                                   capture_output=True, timeout=2)  # fmt: skip
                text = r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                pass
    return text

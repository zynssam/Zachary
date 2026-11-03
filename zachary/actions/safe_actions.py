"""The only actions the assistant (and later voice / LLM output) may trigger.

Whitelist: open, reveal, launch, open_with, search, show, suggest. Nothing here
moves, renames or deletes files, and nothing runs a shell. ``launch`` only starts an
app the desktop lists (``platform.list_apps``), through the desktop's own launcher;
``open_with`` passes it one indexed file or folder. ``open``/``reveal`` only accept items
that are in Zachary's index *and* still exist on disk, so a spoken or generated
path can never open something arbitrary.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from zachary.brain.context import Item
from zachary.core.platform import Platform
from zachary.core.platform.apps import App
from zachary.core.safety import runnable

# Scripts that are fine to open in an app the user names ("open run.py in vscode").
EDITABLE_CODE = frozenset(".py .js .sh .bash .ps1 .bat .cmd .rb .pl .jar .desktop".split())

log = logging.getLogger(__name__)

WHITELIST = frozenset({"open", "reveal", "launch", "open_with", "search", "show", "suggest"})


class ActionNotAllowed(PermissionError):
    pass


class SafeActions:
    def __init__(self, conn: sqlite3.Connection, platform: Platform) -> None:
        self.conn = conn
        self.platform = platform
        self.last_refused = ""  # a program file that was shown instead of opened

    def _verified(self, item: Item) -> Path | None:
        row = self.conn.execute(
            "SELECT path FROM files WHERE id = ? AND path = ?", (item.file_id, item.path)
        ).fetchone()
        if row is None:
            log.warning("refusing action on a path that is not indexed")
            return None
        p = Path(row[0])
        if not os.path.lexists(p):
            return None
        return p

    def _log(self, item: Item) -> None:
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO access_log(file_id, ts, source) VALUES (?, ?, 'zach')",
                (item.file_id, time.time()),
            )
        except sqlite3.Error:
            log.debug("could not write access_log", exc_info=True)

    def perform(self, action: str, item: Item) -> bool:
        if action not in WHITELIST:
            raise ActionNotAllowed(action)
        if action == "open":
            return self.open(item)
        if action == "reveal":
            return self.reveal(item)
        return True  # search / show / suggest only change what is displayed

    def open(self, item: Item) -> bool:
        """Open a verified item. A program file (installer, script, launcher) is never
        opened, since that would run it: it's shown in its folder instead."""
        p = self._verified(item)
        if p is None:
            return False
        if not p.is_dir() and runnable(p):
            log.warning("not opening a program file; showing it in its folder instead")
            self.last_refused = str(p)
            self.platform.reveal_in_file_manager(p)
            return False
        self.platform.open_path(p)
        self._log(item)
        return True

    def reveal(self, item: Item) -> bool:
        p = self._verified(item)
        if p is None:
            return False
        self.platform.reveal_in_file_manager(p)
        self._log(item)
        return True

    def _installed(self, app_id: str) -> App | None:
        """The app only if it's still one the desktop lists (never a free-form command)."""
        return next((a for a in self.platform.list_apps() if a.id == app_id), None)

    def launch(self, app_id: str) -> bool:
        app = self._installed(app_id)
        if app is None:
            log.warning("refusing to launch an app that isn't installed")
            return False
        try:
            self.platform.launch_app(app)
        except OSError as e:
            log.warning("could not launch %s: %s", app.name, e)
            return False
        return True

    def open_with(self, app_id: str, item: Item) -> bool:
        app = self._installed(app_id)
        p = self._verified(item)
        if app is None or p is None:
            return False
        if not p.is_dir() and runnable(p) and p.suffix.lower() not in EDITABLE_CODE:
            return False  # (opening a script *in an editor* is fine; running it isn't)
        try:
            self.platform.launch_app(app, p)
        except OSError as e:
            log.warning("could not open with %s: %s", app.name, e)
            return False
        self._log(item)
        return True

    def open_created(self, path: Path) -> bool:
        """Open a file/folder Zachary just created (not indexed yet): only paths in the
        creation log are accepted."""
        row = self.conn.execute(
            "SELECT 1 FROM undo_log WHERE op IN ('create', 'mkdir') AND src = ? LIMIT 1",
            (str(path),),
        ).fetchone()
        if row is None or not os.path.exists(path) or (path.is_file() and runnable(path)):
            return False
        self.platform.open_path(path)
        return True

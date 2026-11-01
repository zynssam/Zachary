"""Test doubles shared by the test modules."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from zachary.core.hotkeys import Hotkey, HotkeyBackend, HotkeyCallback
from zachary.core.platform import JournalCursor, JournalRecord, RecentItem, WatchLimit


class FakeHotkey:
    name = "fake"

    def __init__(self, callback: HotkeyCallback) -> None:
        self.callback = callback
        self.error: str | None = None

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def press(self, token: str | None = None) -> None:
        self.callback(token)


class FakePlatform:
    """Platform with controllable roots, recent items and (optionally) a journal."""

    name = "fake"
    case_insensitive_paths = sys.platform == "win32"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.recent: list[RecentItem] = []
        self.journal: tuple[list[JournalRecord], JournalCursor] | None = None
        self.cursor: JournalCursor | None = None
        self.watch_limit = WatchLimit(True, None, None, "")
        self.opened: list[Path] = []
        self.revealed: list[Path] = []
        self.spawned: list[list[str]] = []

    def data_dir(self) -> Path:
        return self.root / "data"

    def default_index_roots(self) -> list[Path]:
        return [self.root / "home"]

    def system_excludes(self) -> list[str]:
        return [str(self.root / "sys")]

    def recent_items(self) -> Iterator[RecentItem]:
        return iter(list(self.recent))

    def open_path(self, path: Path) -> None:
        self.opened.append(path)

    def reveal_in_file_manager(self, path: Path) -> None:
        self.revealed.append(path)

    def spawn_detached(self, argv: list[str]) -> None:
        self.spawned.append(argv)

    def try_lock(self, path: Path) -> object | None:
        return object()

    def ensure_app_registration(self, exec_argv: list[str]) -> None: ...

    def global_hotkey(
        self, hotkey: Hotkey, callback: HotkeyCallback, description: str
    ) -> HotkeyBackend:
        return FakeHotkey(callback)

    def hotkey_hint(self, toggle_cmd: list[str]) -> str:
        return "bind " + " ".join(toggle_cmd)

    def check_watch_limit(self, needed: int) -> WatchLimit:
        return self.watch_limit

    def lower_process_priority(self) -> None: ...

    def journal_cursor(self, root: Path) -> JournalCursor | None:
        return self.cursor

    def read_journal(
        self, root: Path, since: JournalCursor
    ) -> tuple[list[JournalRecord], JournalCursor] | None:
        return self.journal


def wait_until(pred: Callable[[], object], timeout: float = 5.0, step: float = 0.02) -> float:
    """Poll ``pred`` until truthy; return seconds waited. Raises on timeout."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return time.monotonic() - t0
        time.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s")


def write(path: Path, text: str = "x", *, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path

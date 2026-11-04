"""Test doubles shared by the test modules."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from zachary.core.hotkeys import Hotkey, HotkeyBackend, HotkeyCallback
from zachary.core.platform import (
    JournalCursor,
    JournalRecord,
    RecentItem,
    TreeWatcher,
    WatchLimit,
)
from zachary.core.platform.apps import App


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
        self.trashed: list[Path] = []
        self.apps: list[App] = [
            App("com.spotify.Client.desktop", "Spotify", "Music Player", location="/x/spotify"),
            App("code.desktop", "Visual Studio Code", "Text Editor", location="/x/code",
                accepts_files=True),
            App("org.gnome.Calculator.desktop", "Calculator", location="/x/calc"),
        ]  # fmt: skip
        self.launched: list[tuple[str, Path | None]] = []
        self.native_watcher = True
        self.battery = False
        self.busy = False

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

    def list_apps(self) -> list[App]:
        return list(self.apps)

    def launch_app(self, app: App, file: Path | None = None) -> None:
        self.launched.append((app.id, file))

    def trash(self, path: Path) -> str:
        """A fake trash under the test root (the real one is tested separately)."""
        import shutil

        bin_ = self.root / "fake-trash"
        bin_.mkdir(exist_ok=True)
        dst = bin_ / f"{len(self.trashed)}-{path.name}"
        shutil.move(str(path), dst)
        self.trashed.append(path)
        return str(dst)

    def restore(self, trashed: str, original: str) -> None:
        import shutil

        from zachary.core.platform.trash import TrashError

        if not trashed or not Path(trashed).exists():
            raise TrashError("no longer in the trash")
        if Path(original).exists():
            raise TrashError("something else now exists at its old place")
        Path(original).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(trashed, original)

    def try_lock(self, path: Path) -> object | None:
        return object()

    def ensure_app_registration(self, exec_argv: list[str]) -> None: ...

    def autostart(self, exec_argv: list[str], enabled: bool | None = None) -> tuple[bool, str]:
        if enabled is not None:
            self.autostart_on = enabled
        return getattr(self, "autostart_on", False), "fake"

    def global_hotkey(
        self,
        hotkey: Hotkey,
        callback: HotkeyCallback,
        description: str,
        shortcut_id: str = "toggle",
    ) -> HotkeyBackend:
        return FakeHotkey(callback)

    def tree_watcher(
        self,
        is_excluded: Callable[[str, bool], bool],
        on_paths: Callable[[list[str]], None],
        on_overflow: Callable[[], None],
    ) -> TreeWatcher | None:
        """Use the real native watcher where there is one (Linux), like production."""
        if self.native_watcher and sys.platform.startswith("linux"):
            from zachary.core.platform.linux_inotify import InotifyTree

            return InotifyTree(is_excluded, on_paths, on_overflow)
        return None

    def on_battery(self) -> bool:
        return self.battery

    def system_busy(self) -> bool:
        return self.busy

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

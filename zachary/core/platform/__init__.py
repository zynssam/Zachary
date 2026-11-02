"""OS-specific code lives only in this package.

Everything outside ``zachary.core.platform`` talks to the OS through the
:class:`Platform` protocol returned by :func:`get_platform`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Protocol

from zachary.core.hotkeys import Hotkey, HotkeyBackend, HotkeyCallback


@dataclass(frozen=True, slots=True)
class RecentItem:
    """A file the OS reports as recently used."""

    path: Path
    ts: float  # unix seconds


@dataclass(frozen=True, slots=True)
class WatchLimit:
    """Result of checking the OS file-watch capacity."""

    ok: bool
    current: int | None
    recommended: int | None
    message: str


@dataclass(frozen=True, slots=True)
class JournalCursor:
    """Position in a volume change journal (Windows USN)."""

    journal_id: int
    next_usn: int


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """One change-journal record. ``frn``/``parent_frn`` equal ``os.stat().st_ino``."""

    frn: int
    parent_frn: int
    name: str
    reason: int


class TreeWatcher(Protocol):
    """An exclude-aware recursive file-system watcher (see linux_inotify.py)."""

    name: str
    limit_hit: bool

    @property
    def watch_count(self) -> int: ...

    def add_tree(self, top: str, *, report_existing: bool = False) -> list[str]: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


class Platform(Protocol):
    name: str
    case_insensitive_paths: bool

    def data_dir(self) -> Path:
        """Per-user directory for Zachary's database and state."""
        ...

    def default_index_roots(self) -> list[Path]:
        """Folders indexed when the user hasn't chosen any."""
        ...

    def system_excludes(self) -> list[str]:
        """Absolute OS/system directories never worth indexing."""
        ...

    def recent_items(self) -> Iterator[RecentItem]:
        """Files the OS reports as recently used."""
        ...

    def open_path(self, path: Path) -> None:
        """Open a file or folder with the default application (non-blocking)."""
        ...

    def reveal_in_file_manager(self, path: Path) -> None:
        """Show a file selected in its folder (non-blocking)."""
        ...

    def check_watch_limit(self, needed: int) -> WatchLimit:
        """Check whether the OS can watch ``needed`` directories."""
        ...

    def lower_process_priority(self) -> None:
        """Run the current process at low CPU/IO priority."""
        ...

    def spawn_detached(self, argv: list[str]) -> None:
        """Start a background process that outlives us (no console window)."""
        ...

    def try_lock(self, path: Path) -> object | None:
        """Take an exclusive single-instance lock; ``None`` if already held."""
        ...

    def ensure_app_registration(self, exec_argv: list[str]) -> None:
        """Register the app with the desktop if a hotkey backend needs it."""
        ...

    def global_hotkey(
        self,
        hotkey: Hotkey,
        callback: HotkeyCallback,
        description: str,
        shortcut_id: str = "toggle",
    ) -> HotkeyBackend:
        """A not-yet-started global hotkey listener for this OS/session."""
        ...

    def hotkey_hint(self, toggle_cmd: list[str]) -> str:
        """How to bind the hotkey manually if the automatic way fails."""
        ...

    def tree_watcher(
        self,
        is_excluded: Callable[[str, bool], bool],
        on_paths: Callable[[list[str]], None],
        on_overflow: Callable[[], None],
    ) -> TreeWatcher | None:
        """A native watcher that skips excluded folders, or None to use watchdog."""
        ...

    def on_battery(self) -> bool:
        """Running on battery power (heavy background work waits for AC)."""
        ...

    def system_busy(self) -> bool:
        """The machine is busy right now (background work waits)."""
        ...

    def journal_cursor(self, root: Path) -> JournalCursor | None:
        """Current change-journal position for the volume holding ``root``.

        ``None`` if the OS/volume has no usable journal (then catch-up re-scans).
        """
        ...

    def read_journal(
        self, root: Path, since: JournalCursor
    ) -> tuple[list[JournalRecord], JournalCursor] | None:
        """Records since ``since``. ``None`` if unavailable, reset or wrapped (re-scan)."""
        ...


@cache
def get_platform() -> Platform:
    """Return the platform implementation for the running OS."""
    if sys.platform == "win32":
        from zachary.core.platform.windows import WindowsPlatform

        return WindowsPlatform()
    from zachary.core.platform.linux import LinuxPlatform

    return LinuxPlatform()


__all__ = [
    "JournalCursor",
    "JournalRecord",
    "Platform",
    "RecentItem",
    "TreeWatcher",
    "WatchLimit",
    "get_platform",
]

"""OS-specific code lives only in this package.

Everything outside ``zachary.core.platform`` talks to the OS through the
:class:`Platform` protocol returned by :func:`get_platform`.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Protocol


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
        """Files the OS reports as recently used (Phase 1)."""
        ...

    def open_path(self, path: Path) -> None:
        """Open a file or folder with the default application (Phase 2)."""
        ...

    def reveal_in_file_manager(self, path: Path) -> None:
        """Show a file selected in its folder (Phase 2)."""
        ...

    def check_watch_limit(self, needed: int) -> WatchLimit:
        """Check whether the OS can watch ``needed`` directories (Phase 1)."""
        ...

    def lower_process_priority(self) -> None:
        """Run the current process at low CPU/IO priority (Phase 1)."""
        ...


@cache
def get_platform() -> Platform:
    """Return the platform implementation for the running OS."""
    if sys.platform == "win32":
        from zachary.core.platform.windows import WindowsPlatform

        return WindowsPlatform()
    from zachary.core.platform.linux import LinuxPlatform

    return LinuxPlatform()


__all__ = ["Platform", "RecentItem", "WatchLimit", "get_platform"]

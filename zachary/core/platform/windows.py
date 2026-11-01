"""Windows implementation of :class:`zachary.core.platform.Platform`.

Phase 0: paths and defaults are real; OS integrations (USN journal, Recent
Items / jump lists, Explorer reveal) are stubs.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

from zachary.core.platform import RecentItem, WatchLimit

_NOT_YET = "not implemented yet (planned for phase {})"

# KNOWNFOLDERID GUIDs. Desktop/Documents may be redirected (e.g. to OneDrive).
_FOLDERID = {
    "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
}
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
# Network (4), CD-ROM (5) and RAM (6) drives are not indexed by default.
INDEXED_DRIVE_TYPES = frozenset({DRIVE_REMOVABLE, DRIVE_FIXED})


def select_drives(bitmask: int, drive_type: dict[str, int], system_drive: str) -> list[Path]:
    """Pick drive roots from a GetLogicalDrives bitmask.

    The system drive is skipped: its user data is under the home folder, and the
    rest is OS and program files.
    """
    out: list[Path] = []
    sys_letter = system_drive[:1].upper()
    for i in range(26):
        if not bitmask & (1 << i):
            continue
        letter = chr(ord("A") + i)
        if letter == sys_letter or drive_type.get(letter) not in INDEXED_DRIVE_TYPES:
            continue
        out.append(Path(f"{letter}:\\"))
    return out


def _known_folder(name: str) -> Path | None:
    if sys.platform != "win32":
        return None
    import ctypes
    import uuid
    from ctypes import wintypes

    guid = uuid.UUID(_FOLDERID[name])

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    g = GUID.from_buffer_copy(guid.bytes_le)
    out = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    if shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) != 0:
        return None
    try:
        return Path(out.value) if out.value else None
    finally:
        ole32.CoTaskMemFree(out)


def _drives() -> list[Path]:
    if sys.platform != "win32":
        return []
    import ctypes

    k32 = ctypes.windll.kernel32
    mask = int(k32.GetLogicalDrives())
    types = {
        chr(ord("A") + i): int(k32.GetDriveTypeW(f"{chr(ord('A') + i)}:\\"))
        for i in range(26)
        if mask & (1 << i)
    }
    return select_drives(mask, types, os.environ.get("SYSTEMDRIVE", "C:"))


class WindowsPlatform:
    name = "windows"
    case_insensitive_paths = True

    def data_dir(self) -> Path:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Zachary"

    def default_index_roots(self) -> list[Path]:
        """Home, Desktop/Documents/Downloads (wherever redirected), and non-system drives."""
        home = Path.home()
        roots = [home]
        for name in ("Desktop", "Documents", "Downloads"):
            roots.append(_known_folder(name) or home / name)
        roots += _drives()
        return roots

    def system_excludes(self) -> list[str]:
        windir = os.environ.get("WINDIR", r"C:\Windows")
        home = Path.home()
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return [
            windir,
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
            r"C:\$Recycle.Bin",
            r"C:\System Volume Information",
            str(local / "Temp"),
            str(local / "Packages"),
            str(local / "Microsoft"),
            str(local / "Google" / "Chrome" / "User Data"),
            str(local / "Microsoft" / "Edge" / "User Data"),
            str(local / "BraveSoftware"),
            str(roaming / "Mozilla"),
            str(roaming / "Microsoft"),
            str(self.data_dir()),
        ]

    def recent_items(self) -> Iterator[RecentItem]:
        raise NotImplementedError(_NOT_YET.format(1))

    def open_path(self, path: Path) -> None:
        raise NotImplementedError(_NOT_YET.format(2))

    def reveal_in_file_manager(self, path: Path) -> None:
        raise NotImplementedError(_NOT_YET.format(2))

    def check_watch_limit(self, needed: int) -> WatchLimit:
        # ReadDirectoryChangesW has no per-user watch cap comparable to inotify.
        return WatchLimit(ok=True, current=None, recommended=None, message="")

    def lower_process_priority(self) -> None:
        raise NotImplementedError(_NOT_YET.format(1))

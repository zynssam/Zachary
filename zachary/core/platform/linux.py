"""Linux implementation of :class:`zachary.core.platform.Platform`.

Phase 0: paths and defaults are real; OS integrations are stubs.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath

from zachary.core.platform import RecentItem, WatchLimit

_NOT_YET = "not implemented yet (planned for phase {})"

# Real on-disk filesystems worth indexing. Network (nfs, cifs, sshfs), pseudo
# (proc, tmpfs, overlay) and image (squashfs: snaps) filesystems are skipped.
DISK_FS_TYPES: frozenset[str] = frozenset(
    {
        "ext2",
        "ext3",
        "ext4",
        "btrfs",
        "xfs",
        "f2fs",
        "zfs",
        "bcachefs",
        "jfs",
        "reiserfs",
        "vfat",
        "exfat",
        "ntfs",
        "ntfs3",
        "fuseblk",
        "hfsplus",
        "apfs",
    }
)
_SYSTEM_MOUNT_PREFIXES = ("/boot", "/efi", "/var", "/usr", "/opt", "/srv", "/snap", "/tmp")
_USER_DIR_KEYS = ("XDG_DESKTOP_DIR", "XDG_DOCUMENTS_DIR", "XDG_DOWNLOAD_DIR")
_USER_DIR_RE = re.compile(r'^\s*(XDG_[A-Z]+_DIR)\s*=\s*"(.*)"\s*$')


def _unescape_mount(field: str) -> str:
    """/proc/mounts encodes space, tab, newline and backslash as octal escapes."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def parse_mounts(text: str, home: Path) -> list[Path]:
    """Mount points (from /proc/self/mounts text) that should be indexed as roots."""
    home_s = home.as_posix()  # POSIX semantics on any OS (tests run on Windows too)
    out: list[Path] = []
    seen: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] not in DISK_FS_TYPES:
            continue
        mnt = _unescape_mount(parts[1])
        if mnt in seen or mnt == "/":
            continue
        if any(mnt == p or mnt.startswith(p + "/") for p in _SYSTEM_MOUNT_PREFIXES):
            continue
        # A mount containing $HOME (e.g. a separate /home partition) is covered by $HOME.
        if home_s == mnt or home_s.startswith(mnt.rstrip("/") + "/"):
            continue
        seen.add(mnt)
        out.append(Path(mnt))
    return out


def parse_user_dirs(text: str, home: Path, keys: Iterable[str] = _USER_DIR_KEYS) -> list[Path]:
    """Resolve XDG user dirs (Desktop, Documents, Downloads) from user-dirs.dirs text."""
    wanted = set(keys)
    found: dict[str, Path] = {}
    for line in text.splitlines():
        m = _USER_DIR_RE.match(line)
        if not m or m.group(1) not in wanted:
            continue
        value = m.group(2).replace("$HOME", home.as_posix())
        p = PurePosixPath(value)
        # XDG uses $HOME itself to mean "disabled".
        if p.is_absolute() and p != PurePosixPath(home.as_posix()):
            found[m.group(1)] = Path(p)
    return [found[k] for k in keys if k in found]


class LinuxPlatform:
    name = "linux"
    case_insensitive_paths = False

    def data_dir(self) -> Path:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(base) / "zachary"

    def default_index_roots(self) -> list[Path]:
        """Home, the XDG Desktop/Documents/Downloads dirs, and mounted disks.

        User dirs are listed even though they normally live in $HOME, because they
        can be relocated. Nested roots are collapsed by the config layer.
        """
        home = Path.home()
        roots = [home]
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        try:
            user_dirs = (config_home / "user-dirs.dirs").read_text(encoding="utf-8")
        except OSError:
            user_dirs = ""
        found = parse_user_dirs(user_dirs, home)
        roots += found or [home / "Desktop", home / "Documents", home / "Downloads"]
        try:
            mounts = Path("/proc/self/mounts").read_text(encoding="utf-8")
        except OSError:
            mounts = ""
        roots += parse_mounts(mounts, home)
        return roots

    def system_excludes(self) -> list[str]:
        return [
            "/proc",
            "/sys",
            "/dev",
            "/run",
            "/tmp",
            "/var",
            "/boot",
            "/usr",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/lib32",
            "/etc",
            "/opt",
            "/snap",
            "/lost+found",
            str(Path.home() / ".cache"),
            str(Path.home() / ".local" / "share" / "Trash"),
            str(Path.home() / ".mozilla"),
            str(Path.home() / ".config" / "google-chrome"),
            str(Path.home() / ".config" / "chromium"),
            str(Path.home() / ".config" / "BraveSoftware"),
            str(Path.home() / ".var" / "app"),
            str(self.data_dir()),
        ]

    def recent_items(self) -> Iterator[RecentItem]:
        raise NotImplementedError(_NOT_YET.format(1))

    def open_path(self, path: Path) -> None:
        raise NotImplementedError(_NOT_YET.format(2))

    def reveal_in_file_manager(self, path: Path) -> None:
        raise NotImplementedError(_NOT_YET.format(2))

    def check_watch_limit(self, needed: int) -> WatchLimit:
        raise NotImplementedError(_NOT_YET.format(1))

    def lower_process_priority(self) -> None:
        raise NotImplementedError(_NOT_YET.format(1))

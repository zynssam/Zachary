"""Linux implementation of :class:`zachary.core.platform.Platform`.

Phase 0: paths and defaults are real; OS integrations are stubs.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import platform as _platform
import re
import shlex
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from zachary.core.hotkeys import Hotkey, HotkeyBackend, HotkeyCallback
from zachary.core.platform import (
    JournalCursor,
    JournalRecord,
    RecentItem,
    TreeWatcher,
    WatchLimit,
)

log = logging.getLogger(__name__)


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
# ioprio_set syscall numbers by machine (no glibc wrapper exists).
_IOPRIO_SET_NR = {"x86_64": 251, "aarch64": 30, "i686": 289, "i386": 289, "armv7l": 314}
_IOPRIO_CLASS_BE = 2
_IOPRIO_CLASS_SHIFT = 13
_IOPRIO_WHO_PROCESS = 1
WATCH_HEADROOM = 1.5  # other apps (IDEs, sync clients) share the per-user inotify budget
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


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return ts if ts > 946684800 else None  # ignore epoch/placeholder dates (< 2000)


def parse_xbel(text: str) -> list[RecentItem]:
    """Parse GTK ``recently-used.xbel``: local files with their latest use time.

    The latest of the bookmark's ``visited``/``modified`` and every application's
    ``modified`` attribute counts as the access time.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    items: list[RecentItem] = []
    for bm in root.iter("bookmark"):
        href = bm.get("href", "")
        url = urlparse(href)
        if url.scheme != "file" or url.netloc not in ("", "localhost"):
            continue
        times = [_parse_ts(bm.get(a)) for a in ("visited", "modified", "added")]
        for el in bm.iter():
            if el.tag.endswith("}application") or el.tag == "application":
                times.append(_parse_ts(el.get("modified")))
        valid = [t for t in times if t is not None]
        if not valid:
            continue
        items.append(RecentItem(Path(unquote(url.path)), max(valid)))
    return items


def watch_limit_message(current: int, needed: int, recommended: int) -> str:
    return (
        f"Zachary needs about {needed:,} folder watches, but the inotify limit is "
        f"{current:,}. Live updates will be incomplete until it is raised. Run:\n"
        f"  sudo sysctl fs.inotify.max_user_watches={recommended}\n"
        "To keep it after reboot:\n"
        f"  echo fs.inotify.max_user_watches={recommended} | "
        "sudo tee /etc/sysctl.d/90-zachary.conf\n"
        "Zachary never needs root itself; until then it re-checks changed folders periodically."
    )


def check_watch_limit_values(current: int | None, needed: int) -> WatchLimit:
    if current is None:
        return WatchLimit(ok=True, current=None, recommended=None, message="")
    want = int(needed * WATCH_HEADROOM)
    if want <= current:
        return WatchLimit(ok=True, current=current, recommended=None, message="")
    recommended = max(524288, 1 << (want - 1).bit_length())
    return WatchLimit(
        ok=False,
        current=current,
        recommended=recommended,
        message=watch_limit_message(current, needed, recommended),
    )


DESKTOP_FILE_NAME = "ai.zachary.Zachary.desktop"


def desktop_entry(exec_argv: list[str]) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Zachary\n"
        "Comment=Find and open your files by name, time or voice\n"
        f"Exec={shlex.join(exec_argv)}\n"
        "Icon=system-search\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "StartupNotify=false\n"
    )


def _show_items_dbus(uri: str) -> bool:
    """org.freedesktop.FileManager1.ShowItems (Nautilus, Dolphin, Nemo, Thunar...)."""
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection

        addr = DBusAddress(
            "/org/freedesktop/FileManager1",
            bus_name="org.freedesktop.FileManager1",
            interface="org.freedesktop.FileManager1",
        )
        with open_dbus_connection(bus="SESSION") as conn:
            reply = conn.send_and_get_reply(
                new_method_call(addr, "ShowItems", "ass", ([uri], "")), timeout=3
            )
        return bool(reply.header.message_type.name == "method_return")
    except Exception:
        return False


def _popen_quiet(argv: list[str]) -> None:
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


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
            str(Path.home() / "snap"),  # snap app data
            str(Path.home() / "go" / "pkg"),  # Go module cache
            str(Path.home() / ".local" / "share" / "Trash"),
            str(Path.home() / ".mozilla"),
            str(Path.home() / ".config" / "google-chrome"),
            str(Path.home() / ".config" / "chromium"),
            str(Path.home() / ".config" / "BraveSoftware"),
            str(Path.home() / ".var" / "app"),
            str(self.data_dir()),
        ]

    def recent_items(self) -> Iterator[RecentItem]:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        try:
            text = (Path(base) / "recently-used.xbel").read_text(encoding="utf-8")
        except OSError:
            return iter(())
        return iter(parse_xbel(text))

    def open_path(self, path: Path) -> None:
        """Open with the default app (non-blocking; xdg-open, never a shell)."""
        _popen_quiet(["xdg-open", str(path)])

    def reveal_in_file_manager(self, path: Path) -> None:
        """Select the file in the file manager; fall back to opening its folder."""

        def work() -> None:
            if not _show_items_dbus(path.as_uri()):
                _popen_quiet(["xdg-open", str(path.parent if not path.is_dir() else path)])

        threading.Thread(target=work, name="zach-reveal", daemon=True).start()

    def spawn_detached(self, argv: list[str]) -> None:
        _popen_quiet(argv)

    def try_lock(self, path: Path) -> object | None:
        """Exclusive advisory lock held for the process lifetime (single instance)."""
        if sys.platform == "win32":
            return None
        import fcntl

        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")  # noqa: SIM115 - kept open while the lock is held
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return None
        return f

    def ensure_app_registration(self, exec_argv: list[str]) -> None:
        """Install a user .desktop entry (needed for the portal to know our app id)."""
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        target = Path(base) / "applications" / DESKTOP_FILE_NAME
        content = desktop_entry(exec_argv)
        try:
            if target.exists() and target.read_text(encoding="utf-8") == content:
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as e:
            log.warning("could not write %s: %s", target, e)

    def global_hotkey(
        self,
        hotkey: Hotkey,
        callback: HotkeyCallback,
        description: str,
        shortcut_id: str = "toggle",
    ) -> HotkeyBackend:
        """Wayland: XDG GlobalShortcuts portal. X11: XGrabKey."""
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
            from zachary.core.platform.linux_portal import PortalHotkey

            return PortalHotkey(hotkey, callback, description, shortcut_id)
        from zachary.core.platform.linux_x11 import X11Hotkey

        return X11Hotkey(hotkey, callback)

    def hotkey_hint(self, toggle_cmd: list[str]) -> str:
        return (
            "If the shortcut doesn't work, add a custom shortcut in your desktop's keyboard "
            f"settings that runs:\n  {shlex.join(toggle_cmd)}"
        )

    def check_watch_limit(self, needed: int) -> WatchLimit:
        try:
            current = int(Path("/proc/sys/fs/inotify/max_user_watches").read_text().strip())
        except (OSError, ValueError):
            current = None
        return check_watch_limit_values(current, needed)

    def lower_process_priority(self) -> None:
        """nice +10 and the lowest best-effort IO class (not 'idle', which can starve)."""
        if sys.platform == "win32":  # keeps type-checking on Windows CI happy
            return
        with contextlib.suppress(OSError):
            os.nice(10)
        nr = _IOPRIO_SET_NR.get(_platform.machine())
        if nr is None:
            return
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            prio = (_IOPRIO_CLASS_BE << _IOPRIO_CLASS_SHIFT) | 7
            libc.syscall(nr, _IOPRIO_WHO_PROCESS, 0, prio)
        except (OSError, AttributeError):
            pass

    def tree_watcher(
        self,
        is_excluded: Callable[[str, bool], bool],
        on_paths: Callable[[list[str]], None],
        on_overflow: Callable[[], None],
    ) -> TreeWatcher | None:
        from zachary.core.platform.linux_inotify import InotifyTree

        try:
            return InotifyTree(is_excluded, on_paths, on_overflow)
        except OSError as e:  # e.g. too many inotify instances
            log.warning("native inotify unavailable (%s); using watchdog", e)
            return None

    def on_battery(self) -> bool:
        """True when a battery is discharging and no AC adapter is online."""
        base = Path("/sys/class/power_supply")
        try:
            supplies = list(base.iterdir())
        except OSError:
            return False
        ac_online = False
        discharging = False
        for d in supplies:
            try:
                kind = (d / "type").read_text().strip()
                if kind == "Mains" and (d / "online").read_text().strip() == "1":
                    ac_online = True
                if kind == "Battery" and (d / "status").read_text().strip() == "Discharging":
                    discharging = True
            except OSError:
                continue
        return discharging and not ac_online

    def system_busy(self) -> bool:
        """1-minute load average above 70% of the CPUs."""
        if sys.platform == "win32":  # keeps type-checking on Windows CI happy
            return False
        try:
            load = float(os.getloadavg()[0])
        except OSError:
            return False
        return bool(load > 0.7 * (os.cpu_count() or 1))

    def journal_cursor(self, root: Path) -> JournalCursor | None:
        return None  # no change journal: catch-up diffs mtimes by re-scanning

    def read_journal(
        self, root: Path, since: JournalCursor
    ) -> tuple[list[JournalRecord], JournalCursor] | None:
        return None

"""Windows implementation of :class:`zachary.core.platform.Platform`.

Implemented: data/index paths, Recent Items (.lnk parsing), background
priority, and USN change-journal reading for startup catch-up. Jump lists and
Explorer open/reveal come later (see phase notes).
"""

from __future__ import annotations

import logging
import os
import struct
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from zachary.core.hotkeys import Hotkey, HotkeyBackend, HotkeyCallback
from zachary.core.platform import JournalCursor, JournalRecord, RecentItem, WatchLimit

log = logging.getLogger(__name__)

# --- Global hotkey: RegisterHotKey on a dedicated thread with its own message loop ----
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
_HOTKEY_ID = 0x5A43  # "ZC"
_VK_NAMED = {"space": 0x20, "return": 0x0D, "escape": 0x1B, "tab": 0x09, "backspace": 0x08}


def hotkey_codes(hotkey: Hotkey) -> tuple[int, int]:
    """(modifiers, virtual-key code) for RegisterHotKey."""
    mods = MOD_NOREPEAT
    for m, bit in (("alt", MOD_ALT), ("ctrl", MOD_CONTROL), ("shift", MOD_SHIFT),
                   ("super", MOD_WIN)):  # fmt: skip
        if m in hotkey.mods:
            mods |= bit
    k = hotkey.key
    if k in _VK_NAMED:
        vk = _VK_NAMED[k]
    elif k.startswith("f") and k[1:].isdigit():
        vk = 0x70 + int(k[1:]) - 1
    else:
        vk = ord(k.upper())  # A-Z and 0-9 map to their ASCII codes
    return mods, vk


class WindowsHotkey:
    name = "win32-registerhotkey"

    def __init__(self, hotkey: Hotkey, callback: HotkeyCallback) -> None:
        self.hotkey = hotkey
        self.callback = callback
        self.error: str | None = None
        self.ready = threading.Event()
        self.thread_id: int | None = None
        self._thread = threading.Thread(target=self._run, name="zach-hotkey", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if sys.platform == "win32" and self.thread_id:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=2)

    def _run(self) -> None:
        if sys.platform != "win32":
            self.error = "not Windows"
            self.ready.set()
            return
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self.thread_id = kernel32.GetCurrentThreadId()
        mods, vk = hotkey_codes(self.hotkey)
        msg = wintypes.MSG()
        # Make sure this thread has a message queue before anyone posts to it.
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        if not user32.RegisterHotKey(None, _HOTKEY_ID, mods, vk):
            self.error = f"{self.hotkey} is already used by another application"
            self.ready.set()
            return
        self.ready.set()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    self.callback(None)
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)


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


# --- Shell links (.lnk), per [MS-SHLLINK] ---------------------------------------------
_LNK_HEADER_SIZE = 0x4C
_HAS_ID_LIST = 0x1
_HAS_LINK_INFO = 0x2
_VOLUME_ID_AND_LOCAL_BASE_PATH = 0x1


def _cstr(data: bytes, off: int) -> str:
    end = data.index(b"\0", off)
    raw = data[off:end]
    try:
        return raw.decode("mbcs")  # the ANSI code page; only exists on Windows
    except LookupError:
        return raw.decode("cp1252", errors="replace")


def _wstr(data: bytes, off: int) -> str:
    end = off
    while data[end : end + 2] != b"\0\0":
        if end + 2 > len(data):
            raise ValueError("unterminated UTF-16 string")
        end += 2
    return data[off:end].decode("utf-16-le")


def parse_lnk_target(data: bytes) -> str | None:
    """Local target path of a shell link, or ``None`` (network/virtual targets)."""
    try:
        if len(data) < _LNK_HEADER_SIZE or struct.unpack_from("<I", data, 0)[0] != 0x4C:
            return None
        flags = struct.unpack_from("<I", data, 0x14)[0]
        pos = _LNK_HEADER_SIZE
        if flags & _HAS_ID_LIST:
            pos += 2 + struct.unpack_from("<H", data, pos)[0]
        if not flags & _HAS_LINK_INFO:
            return None
        (_size, hdr_size, li_flags, _vol, lbp, _cnrl, cps) = struct.unpack_from("<7I", data, pos)
        if not li_flags & _VOLUME_ID_AND_LOCAL_BASE_PATH:
            return None
        if hdr_size >= 0x24:
            lbp_u, cps_u = struct.unpack_from("<2I", data, pos + 28)
            base = _wstr(data, pos + lbp_u)
            suffix = _wstr(data, pos + cps_u) if cps_u else ""
        else:
            base = _cstr(data, pos + lbp)
            suffix = _cstr(data, pos + cps) if cps else ""
    except (struct.error, ValueError, IndexError, UnicodeDecodeError):
        return None
    if suffix and not base.endswith("\\"):
        return base + "\\" + suffix
    return base + suffix


# --- USN change journal ------------------------------------------------------------
_FSCTL_QUERY_USN_JOURNAL = 0x000900F4
# Only the classic read is used. FSCTL_READ_UNPRIVILEGED_USN_JOURNAL (0x903AB) was
# tested on Windows Server 2022 CI: it returned records with empty names and missed
# our own creates/renames, so catch-up can't rely on it. The classic read needs a
# volume handle opened for GENERIC_READ (normally admin); without it, catch-up
# falls back to the scan-based diff (same as Linux).
_FSCTL_READ_USN_JOURNAL = 0x000900BB
_GENERIC_READ = 0x80000000
_FILE_SHARE_RW = 0x1 | 0x2
_OPEN_EXISTING = 3
_USN_RECORD_V2 = struct.Struct("<IHHQQqqIIIIHH")  # 60 bytes, 64-bit file ids
_USN_RECORD_V3 = struct.Struct("<IHH16s16sqqIIIIHH")  # 76 bytes, 128-bit file ids
_READ_BUF = 1 << 16


def parse_usn_records(buf: bytes, n: int) -> tuple[int, list[JournalRecord]]:
    """Parse a FSCTL_READ_USN_JOURNAL output buffer: (next usn, V2/V3 records).

    V3 records carry 128-bit file ids; they are returned as ints (callers fold them
    the same way as ``st_ino``).
    """
    next_usn = struct.unpack_from("<q", buf, 0)[0]
    out: list[JournalRecord] = []
    off = 8
    while off + 8 <= n:
        length, major = struct.unpack_from("<IH", buf, off)
        if length == 0 or off + length > n:
            break
        frn: int
        parent: int
        if major == 2 and length >= _USN_RECORD_V2.size:
            v2 = _USN_RECORD_V2.unpack_from(buf, off)
            frn, parent, reason, name_len, name_off = v2[3], v2[4], v2[7], v2[11], v2[12]
        elif major == 3 and length >= _USN_RECORD_V3.size:
            v3 = _USN_RECORD_V3.unpack_from(buf, off)
            frn = int.from_bytes(v3[3], "little")
            parent = int.from_bytes(v3[4], "little")
            reason, name_len, name_off = v3[7], v3[11], v3[12]
        else:
            off += length
            continue
        name = buf[off + name_off : off + name_off + name_len].decode("utf-16-le", "replace")
        out.append(JournalRecord(frn, parent, name, reason))
        off += length
    return next_usn, out


def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    k32.DeviceIoControl.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _open_volume(k32: Any, root: Path) -> Any:
    """Read handle to the volume holding ``root`` (normally needs admin; None otherwise)."""
    drive = os.path.splitdrive(str(root))[0]
    if len(drive) != 2 or drive[1] != ":":
        return None  # UNC / network paths have no local journal
    invalid = (1 << (8 * struct.calcsize("P"))) - 1
    for access in (_GENERIC_READ,):
        h = k32.CreateFileW(
            f"\\\\.\\{drive}", access, _FILE_SHARE_RW, None, _OPEN_EXISTING, 0, None
        )
        if h is not None and h != invalid:
            return h
    return None


def _query_journal(k32: Any, h: Any) -> tuple[int, int, int] | None:
    """(journal id, first usn, next usn) or None."""
    import ctypes
    from ctypes import wintypes

    buf = ctypes.create_string_buffer(80)
    ret = wintypes.DWORD()
    if not k32.DeviceIoControl(
        h, _FSCTL_QUERY_USN_JOURNAL, None, 0, buf, 80, ctypes.byref(ret), None
    ):
        return None
    jid, first, nxt = struct.unpack_from("<Qqq", buf.raw, 0)
    return int(jid), int(first), int(nxt)


def _usn_cursor(root: Path) -> JournalCursor | None:
    if sys.platform != "win32":
        return None
    k32 = _kernel32()
    h = _open_volume(k32, root)
    if h is None:
        return None
    try:
        q = _query_journal(k32, h)
    finally:
        k32.CloseHandle(h)
    return None if q is None else JournalCursor(journal_id=q[0], next_usn=q[2])


def _usn_read(root: Path, since: JournalCursor) -> tuple[list[JournalRecord], JournalCursor] | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    k32 = _kernel32()
    h = _open_volume(k32, root)
    if h is None:
        return None
    try:
        q = _query_journal(k32, h)
        if q is None:
            return None
        jid, first, _nxt = q
        if jid != since.journal_id or since.next_usn < first:
            return None  # journal recreated or our position was overwritten: re-scan
        records: list[JournalRecord] = []
        start = since.next_usn
        buf = ctypes.create_string_buffer(_READ_BUF)
        ret = wintypes.DWORD()
        while True:
            # READ_USN_JOURNAL_DATA_V0 -> V2 records (64-bit ids, same as st_ino on NTFS).
            req = struct.pack("<qIIQQQ", start, 0xFFFFFFFF, 0, 0, 0, jid)
            ok = k32.DeviceIoControl(
                h, _FSCTL_READ_USN_JOURNAL, req, len(req), buf, _READ_BUF, ctypes.byref(ret), None
            )
            if not ok:
                return None  # e.g. no volume read access (not admin): caller re-scans
            nxt, recs = parse_usn_records(buf.raw, ret.value)
            records.extend(recs)
            if ret.value <= 8 or nxt == start:
                return records, JournalCursor(journal_id=jid, next_usn=nxt)
            start = nxt
    finally:
        k32.CloseHandle(h)


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
            str(home / "AppData"),  # app data, not user files (Recent Items is read directly)
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
        """Recent Items (.lnk). Jump lists are not read yet (see the Phase 1 report)."""
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        folder = Path(appdata) / "Microsoft" / "Windows" / "Recent"
        try:
            links = list(folder.glob("*.lnk"))
        except OSError:
            return iter(())
        items: list[RecentItem] = []
        for lnk in links:
            try:
                st = lnk.stat()
                if st.st_size > 1 << 20:
                    continue
                target = parse_lnk_target(lnk.read_bytes())
            except OSError:
                continue
            if target:
                items.append(RecentItem(Path(target), st.st_mtime))
        return iter(items)

    def open_path(self, path: Path) -> None:
        """Open with the associated app (ShellExecute; returns immediately)."""
        if sys.platform == "win32":
            os.startfile(str(path))

    def reveal_in_file_manager(self, path: Path) -> None:
        """Explorer with the item selected. Paths can't contain '"' on Windows."""
        if sys.platform == "win32":
            subprocess.Popen(f'explorer /select,"{path}"', close_fds=True)

    def spawn_detached(self, argv: list[str]) -> None:
        if sys.platform != "win32":
            return
        flags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )

    def try_lock(self, path: Path) -> object | None:
        """Exclusive lock held for the process lifetime (single instance)."""
        if sys.platform != "win32":
            return None
        import msvcrt

        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")  # noqa: SIM115 - kept open while the lock is held
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            f.close()
            return None
        return f

    def ensure_app_registration(self, exec_argv: list[str]) -> None:
        """Nothing needed for hotkeys on Windows (Start-menu entry comes with packaging)."""

    def global_hotkey(
        self, hotkey: Hotkey, callback: HotkeyCallback, description: str
    ) -> HotkeyBackend:
        return WindowsHotkey(hotkey, callback)

    def hotkey_hint(self, toggle_cmd: list[str]) -> str:
        return "Pick a different hotkey in settings (e.g. ctrl+alt+space)."

    def check_watch_limit(self, needed: int) -> WatchLimit:
        # ReadDirectoryChangesW has no per-user watch cap comparable to inotify.
        return WatchLimit(ok=True, current=None, recommended=None, message="")

    def lower_process_priority(self) -> None:
        """Background mode: lowest CPU, IO and memory priority for this process."""
        if sys.platform != "win32":
            return
        import ctypes

        process_mode_background_begin = 0x00100000
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), process_mode_background_begin)

    def journal_cursor(self, root: Path) -> JournalCursor | None:
        return _usn_cursor(root)

    def read_journal(
        self, root: Path, since: JournalCursor
    ) -> tuple[list[JournalRecord], JournalCursor] | None:
        return _usn_read(root, since)

"""Exclude-aware recursive inotify watcher (Linux).

watchdog's recursive inotify mode puts a watch on *every* subfolder of a root,
including excluded ones (hidden folders, node_modules, .venv, caches). On a real
home folder that was 40,476 watches for 1,609 indexed folders: 88% of the
per-user limit, shared with every other app. This watcher only watches folders
Zachary indexes:

- one inotify instance, one thread, ``select()`` + a wake-up pipe for stop;
- new folders get a watch as soon as they appear (and are listed once, so files
  created before the watch landed are not missed);
- folder renames/moves rewrite the paths of every watch below (identity kept);
  folders moved out or deleted drop their watches;
- a kernel queue overflow asks the caller for a full catch-up.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import logging
import os
import select
import struct
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_DONT_FOLLOW = 0x02000000
IN_EXCL_UNLINK = 0x04000000
IN_ISDIR = 0x40000000
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000

WATCH_MASK = (
    IN_CLOSE_WRITE | IN_MODIFY | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
    | IN_DELETE_SELF | IN_MOVE_SELF | IN_ONLYDIR | IN_DONT_FOLLOW | IN_EXCL_UNLINK
)  # fmt: skip
_EVENT = struct.Struct("iIII")  # wd, mask, cookie, len


class InotifyTree:
    """Watches a set of directory trees; reports changed paths in batches."""

    name = "inotify-tree"

    def __init__(
        self,
        is_excluded: Callable[[str, bool], bool],
        on_paths: Callable[[list[str]], None],
        on_overflow: Callable[[], None] | None = None,
    ) -> None:
        self.is_excluded = is_excluded
        self.on_paths = on_paths
        self.on_overflow = on_overflow
        self.limit_hit = False
        libc_name = ctypes.util.find_library("c") or "libc.so.6"
        self._libc = ctypes.CDLL(libc_name, use_errno=True)
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            e = ctypes.get_errno()
            raise OSError(e, f"inotify_init1: {os.strerror(e)}")
        self.fd = fd
        self._wd_path: dict[int, str] = {}
        self._path_wd: dict[str, int] = {}
        self._lock = threading.Lock()
        self._r, self._w = os.pipe()
        self._thread = threading.Thread(target=self._run, name="zach-inotify", daemon=True)
        self._moves: dict[int, str] = {}  # cookie -> old path of a moved folder

    # -- watches -----------------------------------------------------------------------
    @property
    def watch_count(self) -> int:
        with self._lock:
            return len(self._wd_path)

    def _add(self, path: str) -> bool:
        wd = self._libc.inotify_add_watch(self.fd, os.fsencode(path), WATCH_MASK)
        if wd < 0:
            e = ctypes.get_errno()
            if e == errno.ENOSPC:
                if not self.limit_hit:
                    log.warning("inotify watch limit reached; some folders won't update live")
                self.limit_hit = True
            return False
        with self._lock:
            old = self._wd_path.get(wd)
            if old is not None and old != path:
                self._path_wd.pop(old, None)
            self._wd_path[wd] = path
            self._path_wd[path] = wd
        return True

    def add_tree(self, top: str, *, report_existing: bool = False) -> list[str]:
        """Watch ``top`` and every non-excluded folder below it. Returns entries seen
        (when ``report_existing``), so files created before the watch aren't missed."""
        seen: list[str] = []
        stack = [top]
        while stack:
            d = stack.pop()
            if not self._add(d):
                if self.limit_hit:
                    return seen
                continue
            try:
                with os.scandir(d) as it:
                    for de in it:
                        try:
                            is_dir = de.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        if self.is_excluded(de.path, is_dir):
                            continue
                        if report_existing:
                            seen.append(de.path)
                        if is_dir:
                            stack.append(de.path)
            except OSError:
                continue
        return seen

    def _drop_tree(self, top: str) -> None:
        prefix = top.rstrip(os.sep) + os.sep
        with self._lock:
            doomed = [
                (p, wd) for p, wd in self._path_wd.items() if p == top or p.startswith(prefix)
            ]
            for p, wd in doomed:
                self._path_wd.pop(p, None)
                self._wd_path.pop(wd, None)
        for _p, wd in doomed:
            self._libc.inotify_rm_watch(self.fd, wd)

    def _rename_tree(self, old: str, new: str) -> None:
        prefix = old.rstrip(os.sep) + os.sep
        with self._lock:
            moved = [(p, wd) for p, wd in self._path_wd.items() if p == old or p.startswith(prefix)]
            for p, wd in moved:
                np_ = new + p[len(old) :]
                self._path_wd.pop(p, None)
                self._path_wd[np_] = wd
                self._wd_path[wd] = np_

    # -- lifecycle -----------------------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with contextlib.suppress(OSError):
            os.write(self._w, b"x")
        self._thread.join(timeout=3)
        with contextlib.suppress(OSError):
            os.close(self.fd)
        for f in (self._r, self._w):
            with contextlib.suppress(OSError):
                os.close(f)

    def _run(self) -> None:
        while True:
            try:
                readable, _, _ = select.select([self.fd, self._r], [], [])
            except (OSError, ValueError):
                return
            if self._r in readable:
                return
            try:
                data = os.read(self.fd, 256 * 1024)
            except BlockingIOError:
                continue
            except OSError:
                return
            paths = self._parse(data)
            if paths:
                self.on_paths(paths)

    def _parse(self, data: bytes) -> list[str]:
        out: list[str] = []
        off = 0
        while off + _EVENT.size <= len(data):
            wd, mask, cookie, length = _EVENT.unpack_from(data, off)
            raw = data[off + _EVENT.size : off + _EVENT.size + length]
            off += _EVENT.size + length
            name = os.fsdecode(raw.split(b"\0", 1)[0])
            if mask & IN_Q_OVERFLOW:
                log.warning("inotify queue overflow: requesting a catch-up")
                if self.on_overflow is not None:
                    self.on_overflow()
                continue
            with self._lock:
                base = self._wd_path.get(wd)
            if mask & IN_IGNORED:
                with self._lock:
                    p = self._wd_path.pop(wd, None)
                    if p is not None and self._path_wd.get(p) == wd:
                        self._path_wd.pop(p, None)
                continue
            if base is None:
                continue
            if mask & (IN_DELETE_SELF | IN_MOVE_SELF):
                continue  # reported by the parent folder's event
            path = os.path.join(base, name) if name else base
            is_dir = bool(mask & IN_ISDIR)
            out.append(path)
            if not is_dir:
                continue
            if mask & IN_MOVED_FROM:
                self._moves[cookie] = path
            elif mask & IN_MOVED_TO:
                old = self._moves.pop(cookie, None)
                if old is not None and not self.is_excluded(path, True):
                    self._rename_tree(old, path)
                    out.append(old)
                elif old is not None:
                    self._drop_tree(old)
                elif not self.is_excluded(path, True):
                    out.extend(self.add_tree(path, report_existing=True))
            elif mask & IN_CREATE and not self.is_excluded(path, True):
                out.extend(self.add_tree(path, report_existing=True))
            elif mask & IN_DELETE:
                self._drop_tree(path)
        # Folders moved out of every watched tree: their watches must go.
        for cookie, old in list(self._moves.items()):
            self._moves.pop(cookie, None)
            self._drop_tree(old)
        return out

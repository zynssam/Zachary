"""Move to the OS trash (``send2trash``) and find it again, so a removal can be undone.

``send2trash`` only moves a file to the trash; it doesn't say where it went. Undo
needs that, so after trashing we look it up:

* **Linux** (freedesktop.org trash spec): ``<trash>/info/<name>.trashinfo`` holds the
  original ``Path`` (URL-quoted; relative to the trash's top directory for trashes on
  other drives) and the file is ``<trash>/files/<name>``. Trashes searched: the home
  trash (``$XDG_DATA_HOME/Trash``) and the drive's ``.Trash/<uid>`` / ``.Trash-<uid>``.
* **Windows** (Recycle Bin): ``<drive>\\$Recycle.Bin\\<SID>\\$I<id><ext>`` holds the
  original path, ``$R<id><ext>`` is the file.

Restoring moves the file back (never over an existing file) and removes the info entry.
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
import time
from pathlib import Path
from urllib.parse import unquote


class TrashError(OSError):
    pass


def trash(path: Path) -> str:
    """Send ``path`` to the OS trash; returns where it is now (for ``restore``)."""
    import send2trash

    original = os.path.abspath(path)
    t0 = time.time()
    send2trash.send2trash(original)
    found = _find_windows(original, t0) if sys.platform == "win32" else _find_linux(original, t0)
    if found is None:
        # It is in the trash (send2trash succeeded) but we can't tell where: the user
        # can still restore it by hand; undo will say so.
        return ""
    return found


def restore(trashed: str, original: str) -> None:
    """Put a trashed file/folder back where it was."""
    if not trashed:
        raise TrashError("its place in the trash is unknown; restore it from the trash by hand")
    src = Path(trashed)
    if not os.path.lexists(src):
        raise TrashError("it is no longer in the trash (the trash was emptied?)")
    if os.path.lexists(original):
        raise TrashError("something else now exists at its old place")
    Path(original).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), original)
    info = _info_for(src)
    if info is not None:
        info.unlink(missing_ok=True)


def _info_for(trashed: Path) -> Path | None:
    if trashed.name.startswith("$R"):  # Windows Recycle Bin
        return trashed.with_name("$I" + trashed.name[2:])
    if trashed.parent.name == "files":  # freedesktop trash
        return trashed.parent.parent / "info" / (trashed.name + ".trashinfo")
    return None


# -- Linux ------------------------------------------------------------------------------
def _mount_point(path: str) -> str:
    p = os.path.dirname(os.path.abspath(path))
    dev = os.lstat(p).st_dev
    while True:
        parent = os.path.dirname(p)
        if parent == p:
            return p
        try:
            if os.lstat(parent).st_dev != dev:
                return p
        except OSError:
            return p
        p = parent


def _linux_trashes(original: str) -> list[tuple[Path, str]]:
    """(trash dir, top dir that relative paths are relative to)."""
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    out = [(Path(data_home) / "Trash", data_home)]
    try:
        top = _mount_point(original)
    except OSError:
        return out
    uid = os.getuid() if hasattr(os, "getuid") else 0
    out += [(Path(top) / ".Trash" / str(uid), top), (Path(top) / f".Trash-{uid}", top)]
    return out


def _find_linux(original: str, since: float) -> str | None:
    best: tuple[float, Path] | None = None
    for trash_dir, top in _linux_trashes(original):
        info_dir = trash_dir / "info"
        try:
            entries = list(os.scandir(info_dir))
        except OSError:
            continue
        for e in entries:
            if not e.name.endswith(".trashinfo"):
                continue
            try:
                st = e.stat()
                if st.st_mtime < since - 2:
                    continue
                text = Path(e.path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            path = next((line[5:] for line in text.splitlines() if line.startswith("Path=")), None)
            if path is None:
                continue
            p = unquote(path)
            full = p if os.path.isabs(p) else os.path.join(top, p)
            if os.path.normpath(full) != os.path.normpath(original):
                continue
            files = trash_dir / "files" / e.name[: -len(".trashinfo")]
            if os.path.lexists(files) and (best is None or st.st_mtime > best[0]):
                best = (st.st_mtime, files)
    return str(best[1]) if best else None


# -- Windows ----------------------------------------------------------------------------
def _parse_dollar_i(data: bytes) -> str | None:
    """Original path from a Recycle Bin ``$I`` file (Vista+ v1, Windows 10+ v2)."""
    if len(data) < 24:
        return None
    version = struct.unpack_from("<q", data, 0)[0]
    if version == 2 and len(data) >= 28:
        n = struct.unpack_from("<i", data, 24)[0]
        raw = data[28 : 28 + 2 * n]
    elif version == 1:
        raw = data[24 : 24 + 520]
    else:
        return None
    return raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]


def _find_windows(original: str, since: float) -> str | None:
    drive = os.path.splitdrive(original)[0] or "C:"
    bin_root = Path(drive + "\\") / "$Recycle.Bin"
    best: tuple[float, Path] | None = None
    try:
        sids = [d for d in os.scandir(bin_root) if d.is_dir()]
    except OSError:
        return None
    for sid in sids:
        try:
            entries = list(os.scandir(sid.path))
        except OSError:
            continue  # other users' bins aren't readable
        for e in entries:
            if not e.name.startswith("$I"):
                continue
            try:
                st = e.stat()
                if st.st_mtime < since - 2:
                    continue
                path = _parse_dollar_i(Path(e.path).read_bytes())
            except OSError:
                continue
            if path is None or os.path.normcase(path) != os.path.normcase(original):
                continue
            r = Path(sid.path) / ("$R" + e.name[2:])
            if os.path.lexists(r) and (best is None or st.st_mtime > best[0]):
                best = (st.st_mtime, r)
    return str(best[1]) if best else None

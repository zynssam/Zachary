"""Hard limits that apply to everything that changes files or runs something.

* **System folders are never changed** (``/etc``, ``/usr``, ``C:\\Windows``,
  ``Program Files``, ...): not by clean-up / organize, not by "create a file".
* **New files and folders** may only be created under your home folder or an index
  root, never inside hidden, excluded, system or ``never_touch`` folders.
* **Program files are never opened** ("open setup" must not *run* an installer): an
  executable, script or launcher is shown in its folder instead. Installed apps are
  started only through the desktop's own app list (``platform/apps.py``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zachary.core.config import Config

# Opening these runs code (or installs something) on any OS: never from voice/text/LLM.
RUNNABLE_EXTS = frozenset(
    """
    .exe .msi .msix .com .scr .pif .sh .bash .zsh .fish .run .bin .appimage .desktop .deb
    .rpm .snap .flatpakref .apk .jar .command .app .dmg .pkg
    """.split()
)
# On Windows these run when opened (double-click semantics); on Linux opening a .py or
# .js shows it in an editor, so they're fine there.
WINDOWS_RUNNABLE_EXTS = frozenset(
    ".bat .cmd .ps1 .psm1 .vbs .vbe .js .jse .wsf .wsh .lnk .reg .hta .cpl .py .pyw .pl .rb".split()
)

# Always off limits (the OS itself), whatever is indexed:
_LINUX_CORE = (
    "/bin", "/boot", "/dev", "/efi", "/etc", "/lib", "/lib32", "/lib64", "/libx32", "/proc",
    "/root", "/sbin", "/sys", "/usr",
)  # fmt: skip
# Off limits unless you chose to index them (or they're under your home):
_LINUX_OTHER = ("/opt", "/run", "/snap", "/srv", "/tmp", "/var")
_PLATFORM: str = sys.platform  # (a plain str: both branches are type-checked)


def runnable(path: str | os.PathLike[str]) -> bool:
    """Would opening this run a program?"""
    p = PurePath(path)
    ext = p.suffix.lower()
    if ext in RUNNABLE_EXTS or (_PLATFORM == "win32" and ext in WINDOWS_RUNNABLE_EXTS):
        return True
    # A file with no extension that's marked executable (a Linux program/script).
    return (
        _PLATFORM != "win32" and not p.suffix and os.path.isfile(path) and os.access(path, os.X_OK)
    )


def core_folders() -> list[str]:
    if _PLATFORM == "win32":
        return [os.environ.get("WINDIR", r"C:\Windows"),
                os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")]  # fmt: skip
    return list(_LINUX_CORE)


def system_folders(config: Config | None = None) -> list[str]:
    out = core_folders()
    if config is not None:
        out += [str(x) for x in config.platform.system_excludes()]
    if _PLATFORM == "win32":
        out.append(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    else:
        out += list(_LINUX_OTHER)
    return out


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)).rstrip("/\\")


def _under(path: str, folder: str) -> bool:
    path, folder = _norm(path), _norm(folder)
    return path == folder or path.startswith(folder + os.sep)


def is_system(path: str, config: Config | None = None) -> bool:
    """Inside an OS/system folder (or the filesystem root itself)."""
    norm = _norm(path)
    if norm in {_norm(os.sep), _norm(os.path.splitdrive(norm)[0] + os.sep)}:
        return True
    if any(_under(norm, c) for c in core_folders()):
        return True
    # Your home and the folders you index are yours, wherever they are (/tmp, /mnt...).
    mine = [os.path.expanduser("~")]
    if config is not None:
        mine += [str(r) for r in config.index_roots()]
    if any(_under(norm, m) for m in mine):
        return False
    return any(_under(norm, s) for s in system_folders(config))


def creation_problem(folder: Path, config: Config) -> str | None:
    """Why Zachary may not create files in ``folder`` (None = allowed)."""
    path = str(folder)
    if is_system(path, config):
        return "that's a system folder"
    allowed = [os.path.expanduser("~"), *(str(r) for r in config.index_roots())]
    base = next((a for a in allowed if _under(path, a)), None)
    if base is None:
        return "it's outside your home folder and the folders Zachary indexes"
    rel = PurePath(_norm(path)[len(_norm(base)) :].lstrip("/\\")).parts
    if any(part.startswith(".") for part in rel):
        return "it's a hidden folder"
    if config.path_rules().is_excluded(path, True):
        return "it's an excluded folder"
    if "never_touch" in config.folder_rules_for(path):
        return "it's a never-touch folder"
    return None

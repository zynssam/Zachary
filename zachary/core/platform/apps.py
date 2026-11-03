"""Installed applications: list them, find one by name, launch it (optionally with a file).

Only apps the desktop itself lists can be launched, through the desktop's own
launcher, so a spoken or typed name can never run an arbitrary command:

* **Linux**: ``.desktop`` entries in ``$XDG_DATA_HOME`` and ``$XDG_DATA_DIRS``
  (``applications/``: system, Flatpak, Snap). Launched with ``gio launch <entry> [file]``.
* **Windows**: Start Menu shortcuts (``.lnk``) and Store apps (``Get-StartApps``).
  Launched with ``os.startfile`` / ``explorer shell:AppsFolder\\<id>``; with a file,
  the shortcut's target program is started with that file as its only argument.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# What people say vs. what the desktop calls it.
ALIASES = {
    "vscode": "visual studio code", "vs code": "visual studio code", "code": "visual studio code",
    "visual studio": "visual studio code", "chrome": "google chrome", "word": "libreoffice writer",
    "writer": "libreoffice writer", "excel": "libreoffice calc", "calc": "libreoffice calc",
    "powerpoint": "libreoffice impress", "files": "files", "file manager": "files",
    "explorer": "file explorer", "terminal": "terminal", "notepad": "text editor",
}  # fmt: skip
_FILE_CODES = re.compile(r"%[fFuU]")
OWN_IDS = ("ai.zachary.",)  # our own launcher: "open zachary" means the project


@dataclass(frozen=True, slots=True)
class App:
    id: str  # desktop id (Linux), shortcut path or Store AppID (Windows)
    name: str
    generic: str = ""
    keywords: tuple[str, ...] = ()
    location: str = ""  # .desktop file / .lnk path ("" for Store apps)
    accepts_files: bool = False
    store_id: str = ""  # Windows Store AppID


@dataclass(slots=True)
class _Cache:
    apps: list[App] = field(default_factory=list)
    loaded: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_cache = _Cache()


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


# -- Linux --------------------------------------------------------------------------------
def _xdg_dirs() -> list[Path]:
    home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    out = [Path(home)] + [Path(d) for d in dirs.split(":") if d]
    for extra in ("/var/lib/flatpak/exports/share", "/var/lib/snapd/desktop"):
        if Path(extra) not in out:
            out.append(Path(extra))
    return out


def parse_desktop(text: str) -> dict[str, str] | None:
    """The ``[Desktop Entry]`` group of a .desktop file (unlocalized keys)."""
    out: dict[str, str] = {}
    in_entry = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_entry = line == "[Desktop Entry]"
            continue
        if in_entry and "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            if "[" not in k:  # skip Name[de]= ...
                out.setdefault(k.strip(), v.strip())
    return out or None


def _linux_apps() -> list[App]:
    seen: dict[str, App] = {}
    for base in _xdg_dirs():
        root = base / "applications"
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.desktop")):
            app_id = str(p.relative_to(root)).replace(os.sep, "-")
            if app_id in seen:
                continue  # an earlier (user) directory overrides
            try:
                entry = parse_desktop(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if not entry or entry.get("Type", "Application") != "Application":
                continue
            if entry.get("NoDisplay") == "true" or entry.get("Hidden") == "true":
                seen[app_id] = App(app_id, "")  # hidden: also hides later duplicates
                continue
            name = entry.get("Name", "")
            if not name or app_id.startswith(OWN_IDS):
                continue
            seen[app_id] = App(
                app_id, name, entry.get("GenericName", ""),
                tuple(k for k in entry.get("Keywords", "").split(";") if k),
                str(p), bool(_FILE_CODES.search(entry.get("Exec", ""))),
            )  # fmt: skip
    return [a for a in seen.values() if a.name]


# -- Windows ------------------------------------------------------------------------------
_SKIP_LNK = re.compile(r"uninstall|readme|help|license|release notes|website", re.I)


def _windows_apps() -> list[App]:
    out: dict[str, App] = {}
    bases = [os.environ.get("APPDATA", ""), os.environ.get("PROGRAMDATA", "")]
    for b in bases:
        root = Path(b) / "Microsoft" / "Windows" / "Start Menu" / "Programs" if b else None
        if root is None or not root.is_dir():
            continue
        for p in root.rglob("*.lnk"):
            name = p.stem
            if _SKIP_LNK.search(name) or name.lower() in out:
                continue
            out[name.lower()] = App(str(p), name, location=str(p), accepts_files=True)
    try:  # Store apps (Spotify from the Store, Settings, ...): ~1 s, so cached
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-StartApps | ForEach-Object { $_.Name + \"`t\" + $_.AppID }"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )  # fmt: skip
        for line in r.stdout.splitlines():
            name, _, app_id = line.partition("\t")
            if name and app_id and name.lower() not in out and not _SKIP_LNK.search(name):
                out[name.lower()] = App(app_id, name, store_id=app_id)
    except (OSError, subprocess.SubprocessError):
        log.debug("Get-StartApps failed", exc_info=True)
    return list(out.values())


def _lnk_target(lnk: str) -> str | None:
    """The program a Windows shortcut starts (to open a file with it)."""
    script = f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}').TargetPath"
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
            timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    target = r.stdout.strip()
    return target if target.lower().endswith(".exe") and os.path.isfile(target) else None


# -- public -------------------------------------------------------------------------------
def list_apps(refresh: bool = False) -> list[App]:
    with _cache.lock:
        if refresh or not _cache.loaded:
            try:
                _cache.apps = _windows_apps() if sys.platform == "win32" else _linux_apps()
            except Exception:
                log.warning("could not list installed apps", exc_info=True)
                _cache.apps = []
            _cache.loaded = True
        return list(_cache.apps)


def score(query: str, app: App) -> float:
    """How well a spoken/typed name matches an app: 1 exact ... 0 no match."""
    q = " ".join(_words(query))
    if not q:
        return 0.0
    q = ALIASES.get(q, q)
    name = " ".join(_words(app.name))
    if q == name:
        return 1.0
    qw, nw = q.split(), name.split()
    if name.startswith(q + " ") or (len(qw) == 1 and qw[0] in nw[:1]):
        return 0.9  # "libreoffice" -> "LibreOffice Writer"; "spotify" -> "Spotify Premium"
    if all(w in nw for w in qw):
        return 0.8
    ratio = difflib.SequenceMatcher(None, q, name).ratio()
    if ratio >= 0.85:
        return 0.75  # "spotfy"
    if q in {" ".join(_words(k)) for k in app.keywords} or q == " ".join(_words(app.generic)):
        return 0.6  # "music player" -> Spotify (only shown, never launched on its own)
    return 0.0


def find_app(query: str, apps: list[App] | None = None) -> tuple[App, float] | None:
    best: tuple[App, float] | None = None
    for a in apps if apps is not None else list_apps():
        s = score(query, a)
        if s and (
            best is None or s > best[1] or (s == best[1] and len(a.name) < len(best[0].name))
        ):
            best = (a, s)
    return best


def launch(app: App, file: Path | None = None) -> None:
    """Start ``app`` (with ``file`` as its argument). Never through a shell."""
    if sys.platform == "win32":
        _launch_windows(app, file)
    else:
        _launch_linux(app, file)


def _launch_windows(app: App, file: Path | None) -> None:
    if app.store_id:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app.store_id}"])
    elif file is None:
        os.startfile(app.location)  # type: ignore[attr-defined,unused-ignore]
    else:
        target = _lnk_target(app.location)
        if target is None:
            raise OSError(f"can't tell which program {app.name} runs")
        subprocess.Popen([target, str(file)])


def _launch_linux(app: App, file: Path | None) -> None:
    if file is not None and not app.accepts_files:
        raise OSError(f"{app.name} doesn't open files")
    argv = ["gio", "launch", app.location] + ([str(file)] if file is not None else [])
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)  # fmt: skip

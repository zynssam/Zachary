"""Settings, index roots, exclude rules, and per-folder rules.

Everything user-tunable is stored in the database (``settings``,
``exclude_rules``, ``folder_rules``) so the indexer and the assistant
see the same configuration.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from zachary.core.db import transaction
from zachary.core.platform import Platform, get_platform

ExcludeKind = Literal["dirname", "glob", "path"]
FolderRule = Literal["never_touch", "auto_tidy", "name_only"]
FOLDER_RULES: frozenset[str] = frozenset({"never_touch", "auto_tidy", "name_only"})

# Directory names skipped wherever they appear.
DEFAULT_EXCLUDE_DIRNAMES: tuple[str, ...] = (
    "node_modules",
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "venv",
    ".venv",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "target",
    ".gradle",
    ".idea",
    ".cache",
    ".next",
    ".nuxt",
    "$RECYCLE.BIN",
    "System Volume Information",
)
# Filename globs for temp / partial files.
DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    "~$*",
    "*.tmp",
    "*.temp",
    "*.swp",
    "*.swo",
    "*~",
    "*.part",
    "*.crdownload",
    "*.partial",
    ".~lock.*#",
    "Thumbs.db",
    "desktop.ini",
    ".DS_Store",
)

# Indexed by name only, never by content.
NAME_ONLY_EXTS: frozenset[str] = frozenset(
    {
        # media
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".webp",
        ".heic",
        ".tiff",
        ".svg",
        ".ico",
        ".mp3",
        ".wav",
        ".flac",
        ".aac",
        ".ogg",
        ".m4a",
        ".opus",
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".webm",
        ".wmv",
        # archives / binaries
        ".zip",
        ".rar",
        ".7z",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".zst",
        ".exe",
        ".msi",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".iso",
        ".img",
        ".dmg",
        ".deb",
        ".rpm",
        ".appimage",
        ".apk",
        ".jar",
        ".class",
        ".o",
        ".obj",
        ".pyc",
        ".sqlite",
        ".db",
    }
)


@dataclass(slots=True)
class Settings:
    """Typed view over the ``settings`` table. Unset keys use these defaults."""

    index_roots: list[str] = field(default_factory=list)  # empty → platform defaults
    indexing_paused: bool = False
    quickbar_hotkey: str = "alt+space"
    language_code: str = "en-IN"
    llm_enabled: bool = False
    embed_max_kb: int = 64  # "embed less": first N KB per document by default


_SETTING_KEYS: tuple[str, ...] = tuple(Settings.__dataclass_fields__)


def default_db_path(platform: Platform | None = None) -> Path:
    env = os.environ.get("ZACHARY_DB")
    if env:
        return Path(env)
    return (platform or get_platform()).data_dir() / "zachary.db"


def normalize_path(path: str | Path, *, case_insensitive: bool) -> str:
    """Absolute, normalized path string used as the canonical key."""
    # abspath, not resolve(): symlinks must keep the path the user sees.
    p = os.path.normpath(os.path.abspath(os.fspath(path)))  # noqa: PTH100
    return os.path.normcase(p) if case_insensitive else p


def is_under(path: str, ancestor: str) -> bool:
    """True if normalized ``path`` equals or is inside normalized ``ancestor``."""
    if path == ancestor:
        return True
    if not path.startswith(ancestor):
        return False
    return ancestor[-1] in "/\\" or path[len(ancestor)] in "/\\"


def collapse_roots(roots: Iterable[str]) -> list[str]:
    """Dedupe normalized roots and drop any root nested inside another one."""
    out: list[str] = []
    for r in sorted(set(roots), key=lambda x: (len(x), x)):
        if not any(is_under(r, kept) for kept in out):
            out.append(r)
    return sorted(out)


@dataclass(frozen=True, slots=True)
class ExcludeMatcher:
    """Fast, precompiled exclude check used on the scanner/watcher hot path."""

    dirnames: frozenset[str]
    glob_re: re.Pattern[str] | None
    paths: tuple[str, ...]  # normalized; matched as prefixes on path boundaries
    case_insensitive: bool

    @classmethod
    def build(
        cls,
        dirnames: Iterable[str],
        globs: Iterable[str],
        paths: Iterable[str],
        *,
        case_insensitive: bool,
    ) -> ExcludeMatcher:
        fold = str.casefold if case_insensitive else str
        globs = list(globs)
        glob_re = None
        if globs:
            flags = re.IGNORECASE if case_insensitive else 0
            glob_re = re.compile("|".join(fnmatch.translate(g) for g in globs), flags)
        norm = tuple(sorted({normalize_path(p, case_insensitive=case_insensitive) for p in paths}))
        return cls(frozenset(fold(d) for d in dirnames), glob_re, norm, case_insensitive)

    def excludes_name(self, name: str, *, is_dir: bool) -> bool:
        """Check by entry name only (cheap; for use inside ``os.scandir`` loops)."""
        if is_dir and (name.casefold() if self.case_insensitive else name) in self.dirnames:
            return True
        return self.glob_re is not None and self.glob_re.match(name) is not None

    def excludes_path(self, path: str) -> bool:
        """Check whether ``path`` (already normalized) is at/under an excluded path."""
        return any(is_under(path, p) for p in self.paths)

    def is_excluded(self, path: str | Path, *, is_dir: bool) -> bool:
        """Full check for a single path: excluded path prefix, or any excluded component."""
        norm = normalize_path(path, case_insensitive=self.case_insensitive)
        if self.excludes_path(norm):
            return True
        parts = Path(norm).parts
        last = len(parts) - 1
        for i, part in enumerate(parts):
            if self.excludes_name(part, is_dir=is_dir or i < last):
                return True
        return False


class Config:
    """Reads and writes configuration stored in the database."""

    def __init__(self, conn: sqlite3.Connection, platform: Platform | None = None) -> None:
        self.conn = conn
        self.platform = platform or get_platform()

    # -- helpers -----------------------------------------------------------
    def _norm(self, path: str | Path) -> str:
        return normalize_path(path, case_insensitive=self.platform.case_insensitive_paths)

    # -- settings ----------------------------------------------------------
    def seed_defaults(self) -> None:
        """Insert default exclude rules. Idempotent; never re-enables a rule the user disabled."""
        rows: list[tuple[str, str]] = [(d, "dirname") for d in DEFAULT_EXCLUDE_DIRNAMES]
        rows += [(g, "glob") for g in DEFAULT_EXCLUDE_GLOBS]
        rows += [(self._norm(p), "path") for p in self.platform.system_excludes()]
        with transaction(self.conn):
            self.conn.executemany(
                "INSERT OR IGNORE INTO exclude_rules(pattern, kind, source) "
                "VALUES (?, ?, 'default')",
                rows,
            )

    def get_setting(self, key: str) -> Any:
        if key not in _SETTING_KEYS:
            raise KeyError(key)
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return getattr(Settings(), key)
        return json.loads(row[0])

    def set_setting(self, key: str, value: Any) -> None:
        if key not in _SETTING_KEYS:
            raise KeyError(key)
        default = getattr(Settings(), key)
        if not isinstance(value, type(default)):
            raise TypeError(f"{key} expects {type(default).__name__}")
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def load(self) -> Settings:
        stored = {
            k: json.loads(v)
            for k, v in self.conn.execute("SELECT key, value FROM settings")
            if k in _SETTING_KEYS
        }
        return Settings(**stored)

    # -- index roots -------------------------------------------------------
    def index_roots(self) -> list[Path]:
        """Configured roots (or platform defaults), normalized, with nested roots collapsed.

        Roots that don't exist right now (e.g. an unplugged USB drive) are kept;
        the scanner skips them.
        """
        stored: list[str] = self.get_setting("index_roots")
        raw = stored or [str(r) for r in self.platform.default_index_roots()]
        return [Path(r) for r in collapse_roots(self._norm(r) for r in raw)]

    def set_index_roots(self, roots: Iterable[str | Path]) -> None:
        self.set_setting("index_roots", sorted({self._norm(r) for r in roots}))

    # -- exclude rules -----------------------------------------------------
    def add_exclude(self, pattern: str | Path, kind: ExcludeKind = "path") -> None:
        pat = self._norm(pattern) if kind == "path" else str(pattern)
        self.conn.execute(
            "INSERT INTO exclude_rules(pattern, kind, source, enabled) VALUES (?, ?, 'user', 1) "
            "ON CONFLICT(pattern, kind) DO UPDATE SET enabled = 1",
            (pat, kind),
        )

    def set_exclude_enabled(self, pattern: str | Path, kind: ExcludeKind, enabled: bool) -> None:
        pat = self._norm(pattern) if kind == "path" else str(pattern)
        self.conn.execute(
            "UPDATE exclude_rules SET enabled = ? WHERE pattern = ? AND kind = ?",
            (int(enabled), pat, kind),
        )

    def remove_exclude(self, pattern: str | Path, kind: ExcludeKind = "path") -> None:
        """Delete a user rule. Default rules can only be disabled."""
        pat = self._norm(pattern) if kind == "path" else str(pattern)
        self.conn.execute(
            "DELETE FROM exclude_rules WHERE pattern = ? AND kind = ? AND source = 'user'",
            (pat, kind),
        )

    def exclude_matcher(self, roots: Iterable[str | Path] | None = None) -> ExcludeMatcher:
        """Build the matcher for scanning ``roots`` (default: :meth:`index_roots`).

        A path exclude that is a strict ancestor of a root is dropped: choosing the
        root is more specific (e.g. a USB drive under ``/run/media`` while ``/run``
        is a system exclude). An exclude equal to or inside a root still applies.
        """
        norm_roots = [self._norm(r) for r in (self.index_roots() if roots is None else roots)]
        by_kind: dict[str, list[str]] = {"dirname": [], "glob": [], "path": []}
        for pattern, kind in self.conn.execute(
            "SELECT pattern, kind FROM exclude_rules WHERE enabled = 1"
        ):
            if kind == "path" and any(r != pattern and is_under(r, pattern) for r in norm_roots):
                continue
            by_kind[kind].append(pattern)
        return ExcludeMatcher.build(
            by_kind["dirname"],
            by_kind["glob"],
            by_kind["path"],
            case_insensitive=self.platform.case_insensitive_paths,
        )

    # -- folder rules ------------------------------------------------------
    def add_folder_rule(self, path: str | Path, rule: FolderRule) -> None:
        if rule not in FOLDER_RULES:
            raise ValueError(f"unknown folder rule {rule!r}")
        self.conn.execute(
            "INSERT OR IGNORE INTO folder_rules(path, rule) VALUES (?, ?)", (self._norm(path), rule)
        )

    def remove_folder_rule(self, path: str | Path, rule: FolderRule) -> None:
        self.conn.execute(
            "DELETE FROM folder_rules WHERE path = ? AND rule = ?", (self._norm(path), rule)
        )

    def folder_rules_for(self, path: str | Path) -> set[str]:
        """Rules that apply to ``path``: those set on it or on any ancestor folder."""
        target = Path(self._norm(path))
        candidates = [str(target), *(str(p) for p in target.parents)]
        placeholders = ",".join("?" * len(candidates))
        rows = self.conn.execute(
            f"SELECT DISTINCT rule FROM folder_rules WHERE path IN ({placeholders})", candidates
        )
        return {r[0] for r in rows}

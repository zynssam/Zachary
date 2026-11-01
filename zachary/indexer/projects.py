"""Project detection via marker files, ordered by latest activity.

A folder is a project if it directly contains a marker (``.git``,
``package.json``, ``pyproject.toml``, ...). A folder inside an already-detected
project only counts if it has its own ``.git``. ``last_activity`` is the newest
mtime of any indexed file inside the project (excluded folders such as
``node_modules`` don't count, because they aren't indexed).
"""

from __future__ import annotations

import fnmatch
import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zachary.indexer.store import FileStore

MARKER_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "CMakeLists.txt",
        "Makefile",
    }
)
MARKER_GLOBS: tuple[str, ...] = ("*.sln", "*.csproj")


@dataclass(frozen=True, slots=True)
class Project:
    folder_id: int
    path: str
    name: str
    markers: list[str]
    last_activity: float


def is_marker(name: str) -> bool:
    return name in MARKER_NAMES or any(fnmatch.fnmatch(name, g) for g in MARKER_GLOBS)


def markers_in_folder(folder: str) -> set[str]:
    """List one folder and return the markers it contains (used for live refreshes)."""
    found: set[str] = set()
    try:
        with os.scandir(folder) as it:
            for de in it:
                if de.name in MARKER_NAMES:
                    found.add(de.name)
                else:
                    found.update(g for g in MARKER_GLOBS if fnmatch.fnmatch(de.name, g))
    except OSError:
        pass
    return found


def select_projects(
    candidates: dict[str, set[str]], sep: str, outer: Iterable[str] = ()
) -> dict[str, set[str]]:
    """Apply the nesting rule. ``outer`` are already-accepted projects outside the candidates."""
    accepted: dict[str, set[str]] = {}
    outer_set = set(outer)

    def inside_project(path: str) -> bool:
        p = os.path.dirname(path)
        while True:
            if p in accepted or p in outer_set:
                return True
            nxt = os.path.dirname(p)
            if nxt == p:
                return False
            p = nxt

    for path in sorted(candidates, key=lambda x: (x.count(sep), x)):
        markers = candidates[path]
        if not markers:
            continue
        if ".git" not in markers and inside_project(path):
            continue
        accepted[path] = markers
    return accepted


def _last_activity(conn: sqlite3.Connection, store: FileStore, path: str) -> float:
    lo, hi = store.descendant_range(path)
    v = conn.execute(
        "SELECT max(mtime) FROM files WHERE path > ? AND path < ? AND is_dir = 0", (lo, hi)
    ).fetchone()[0]
    return float(v) if v is not None else 0.0


def apply_markers(store: FileStore, markers: dict[str, set[str]], scope: str | None) -> int:
    """Replace projects inside ``scope`` (``None`` = everything) using scanned markers.

    Returns the number of projects now recorded inside the scope.
    """
    conn = store.conn
    with store.batch():
        existing = {
            str(r[1]): int(r[0])
            for r in conn.execute(
                "SELECT p.folder_id, f.path FROM projects p JOIN files f ON f.id = p.folder_id"
            )
        }
        if scope is None:
            in_scope = set(existing)
            outer: list[str] = []
        else:
            lo, hi = store.descendant_range(scope)
            in_scope = {p for p in existing if p == scope or lo < p < hi}
            outer = [p for p in existing if p not in in_scope]
        accepted = select_projects(markers, store.sep, outer)
        for p in in_scope - set(accepted):
            conn.execute("DELETE FROM projects WHERE folder_id = ?", (existing[p],))
        n = 0
        for path, ms in accepted.items():
            row = conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
            if row is None:
                continue
            conn.execute(
                "INSERT INTO projects(folder_id, markers, last_activity) VALUES (?, ?, ?) "
                "ON CONFLICT(folder_id) DO UPDATE SET markers = excluded.markers, "
                "last_activity = excluded.last_activity",
                (row[0], json.dumps(sorted(ms)), _last_activity(conn, store, path)),
            )
            n += 1
    return n


def refresh_folders(store: FileStore, folders: Iterable[str]) -> None:
    """Re-check folders whose children changed; rescan a subtree only if markers changed."""
    from zachary.indexer.scanner import scan_subtree  # local: scanner imports this module

    conn = store.conn
    for folder in sorted(set(folders), key=len):
        if store.root_for(folder) is None:
            continue
        now = markers_in_folder(folder)
        row = conn.execute(
            "SELECT p.markers FROM projects p JOIN files f ON f.id = p.folder_id WHERE f.path = ?",
            (folder,),
        ).fetchone()
        before = set(json.loads(row[0])) if row else set()
        if now == before:
            continue
        if not now and not before:
            continue
        # Nesting decisions below this folder may change: re-collect markers for it.
        stats = scan_subtree(store, folder)
        found = dict(stats.result.markers)
        found[folder] = now
        apply_markers(store, found, scope=folder)


def touch(store: FileStore, changed: Iterable[tuple[int, str, float]]) -> None:
    """Raise ``last_activity`` of every project containing a changed file."""
    conn = store.conn
    best: dict[str, float] = {}
    for _fid, path, mtime in changed:
        p = os.path.dirname(path)
        root = store.root_for(path)
        while root is not None and len(p) >= len(root):
            if mtime > best.get(p, 0.0):
                best[p] = mtime
            nxt = os.path.dirname(p)
            if nxt == p:
                break
            p = nxt
    if not best:
        return
    with store.batch():
        items = list(best.items())
        for i in range(0, len(items), 500):
            chunk = items[i : i + 500]
            marks = ",".join("?" * len(chunk))
            ids = {
                str(r[1]): int(r[0])
                for r in conn.execute(
                    "SELECT p.folder_id, f.path FROM projects p JOIN files f"
                    f" ON f.id = p.folder_id WHERE f.path IN ({marks})",
                    [p for p, _ in chunk],
                )
            }
            for p, ts in chunk:
                if p in ids:
                    conn.execute(
                        "UPDATE projects SET last_activity = max(last_activity, ?)"
                        " WHERE folder_id = ?",
                        (ts, ids[p]),
                    )


def list_projects(conn: sqlite3.Connection, limit: int | None = None) -> list[Project]:
    sql = (
        "SELECT p.folder_id, f.path, f.name, p.markers, p.last_activity FROM projects p"
        " JOIN files f ON f.id = p.folder_id ORDER BY p.last_activity DESC, f.path"
    )
    params: tuple[int, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return [
        Project(int(r[0]), str(r[1]), str(r[2]), json.loads(r[3]), float(r[4]))
        for r in conn.execute(sql, params)
    ]

"""The things you work on, described: projects, named by their meaningful folder.

A code project's folder is often generic ("backend", "frontend", "dummy"); the thing
you'd call it is the folder above ("Nishaan-main"). Each workspace gets a short
description from its READMEs / project files, so a question like "which project was I
working on about crime scenes?" can be matched (locally, then confirmed by the LLM).
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import PurePath

import numpy as np

from zachary.indexer.projects import list_projects
from zachary.search.tokens import words

GENERIC = frozenset(
    """
    backend frontend server client app apps api web src main dummy demo test tests hello
    world sample samples code project projects repo frontend-test backend-test 2d 3d ui
    """.split()
)
DESCRIPTION_CHARS = 600
CACHE_S = 600.0


@dataclass(slots=True)
class Workspace:
    name: str
    path: str
    folder_id: int
    last_activity: float
    description: str = ""
    parts: list[str] = field(default_factory=list)  # the project folders inside it


def _generic(name: str) -> bool:
    w = words(name)
    return not w or all(x in GENERIC or x.isdigit() for x in w)


def _display_folder(path: str, stop: set[str]) -> str:
    """The nearest folder (this one or above) whose name isn't generic."""
    p = path.rstrip("/\\")
    while _generic(os.path.basename(p)):
        parent = os.path.dirname(p)
        if parent == p or parent in stop:
            break
        p = parent
    return p


def _describe(conn: sqlite3.Connection, folder: str) -> str:
    """READMEs (shallowest first) and project files' first text, ~600 characters."""
    sep = "\\" if "\\" in folder and "/" not in folder else "/"
    lo, hi = folder + sep, folder + chr(ord(sep) + 1)
    rows = conn.execute(
        """SELECT f.path, f.name, c.text_excerpt FROM files f JOIN chunks c ON c.file_id = f.id
           WHERE f.path >= ? AND f.path < ? AND c.ord = 0
             AND (lower(f.name) LIKE 'readme%' OR lower(f.name) IN
                  ('pyproject.toml', 'package.json', 'api.md', 'claude.md', 'overview.md'))
           LIMIT 40""",
        (lo, hi),
    ).fetchall()
    rows = [r for r in rows if ".egg-info" not in r[0] and "node_modules" not in r[0]]
    rows.sort(key=lambda r: (not str(r[1]).lower().startswith("readme"),
                             str(r[0]).count(sep)))  # fmt: skip
    text = " ".join(" ".join(str(r[2]).split()) for r in rows[:4])
    return text[:DESCRIPTION_CHARS]


_cache: dict[int, tuple[float, list[Workspace]]] = {}


def workspaces(conn: sqlite3.Connection, roots: list[str]) -> list[Workspace]:
    hit = _cache.get(id(conn))
    if hit is not None and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    home = os.path.expanduser("~")
    stop = (
        {r.rstrip("/\\") for r in roots}
        | {home}
        | {os.path.join(home, n) for n in ("Documents", "Downloads", "Desktop", "Projects")}
        | {os.path.join(home, "Documents", "Projects"), os.path.join(home, "Documents", "PROJECTS")}
    )
    by_path: dict[str, Workspace] = {}
    for p in list_projects(conn):
        shown = _display_folder(p.path, stop)
        ws = by_path.get(shown)
        if ws is None:
            row = conn.execute("SELECT id FROM files WHERE path = ?", (shown,)).fetchone()
            ws = Workspace(PurePath(shown).name, shown, int(row[0]) if row else p.folder_id,
                           p.last_activity)  # fmt: skip
            by_path[shown] = ws
        ws.parts.append(p.path)
        ws.last_activity = max(ws.last_activity, p.last_activity)
    out = sorted(by_path.values(), key=lambda w: -w.last_activity)
    for ws in out:
        ws.description = _describe(conn, ws.path)
    _cache[id(conn)] = (time.monotonic(), out)
    return out


def rank(question: str, items: list[Workspace], embed: object | None = None
         ) -> list[tuple[Workspace, float]]:  # fmt: skip
    """Workspaces by how well their name + description match the question."""
    from zachary.brain.qa import question_terms

    terms = question_terms(question)
    stems = [t if len(t) <= 4 else t[: max(4, len(t) - 2)] for t in terms]
    scores = np.zeros(len(items), dtype=np.float32)
    for i, ws in enumerate(items):
        have = set(words(ws.name + " " + ws.description))
        hits = sum(1 for s in stems if any(h.startswith(s) for h in have))
        scores[i] = 0.5 * (hits / len(stems) if stems else 0.0)
    encode = getattr(embed, "encode", None)
    if encode is not None and items:
        v = encode([question] + [f"{w.name}. {w.description}" for w in items])
        v = v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
        scores += 0.5 * np.clip(v[1:] @ v[0], 0.0, 1.0)
    order = np.argsort(-scores)
    return [(items[i], float(scores[i])) for i in order]

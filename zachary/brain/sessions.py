"""Work sessions, built from what you opened and edited (``access_log``).

A session is a stretch of activity with no gap longer than SESSION_GAP_S. They are
computed on demand from the access log (a cheap indexed query), so they're always
up to date and nothing extra has to be maintained.

Used for "resume where I left off" (reopen the last session's files) and the
timeline ("what was I working on last Tuesday?").
"""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePath

from zachary.indexer.clustering import GENERIC_FOLDERS, topics_of
from zachary.search.tokens import words

SESSION_GAP_S = 45 * 60
LOOKBACK_S = 30 * 86400  # "resume" looks this far back
RESUME_MAX_FILES = 8  # never reopen more than this at once
NOISE_NAMES = frozenset(
    """
    .bash_history .zsh_history .python_history .viminfo .lesshst .wget-hsts
    recently-used.xbel desktop.ini thumbs.db
    """.split()
)


@dataclass(slots=True)
class SessionFile:
    file_id: int
    path: str
    name: str
    kind: str
    is_dir: bool
    events: int
    last_ts: float


@dataclass(slots=True)
class Session:
    start: float
    end: float
    files: list[SessionFile] = field(default_factory=list)
    label: str = ""

    @property
    def minutes(self) -> int:
        return max(1, round((self.end - self.start) / 60))


def _noise(name: str, path: str) -> bool:
    if name.lower() in NOISE_NAMES or name.startswith("."):
        return True
    # Inside a hidden folder (config, caches): not "work".
    return any(p.startswith(".") for p in PurePath(path).parts[1:-1])


def sessions(
    conn: sqlite3.Connection,
    since: float,
    until: float | None = None,
    *,
    gap: float = SESSION_GAP_S,
) -> list[Session]:
    """Sessions overlapping [since, until), oldest first."""
    until = time.time() if until is None else until
    rows = conn.execute(
        """SELECT a.file_id, a.ts, f.path, f.name, f.kind, f.is_dir FROM access_log a
           JOIN files f ON f.id = a.file_id
           WHERE a.ts >= ? AND a.ts < ? ORDER BY a.ts""",
        (since - gap, until),
    ).fetchall()
    out: list[Session] = []
    cur: Session | None = None
    files: dict[int, SessionFile] = {}
    for fid, ts, path, name, kind, is_dir in rows:
        if _noise(name, path):
            continue
        if cur is None or ts - cur.end > gap:
            if cur is not None:
                cur.files = _order(files)
                out.append(cur)
            cur, files = Session(ts, ts), {}
        cur.end = ts
        sf = files.get(fid)
        if sf is None:
            files[fid] = SessionFile(fid, path, name, kind, bool(is_dir), 1, ts)
        else:
            sf.events += 1
            sf.last_ts = ts
    if cur is not None:
        cur.files = _order(files)
        out.append(cur)
    out = [s for s in out if s.end >= since]
    for s in out:
        s.label = label(conn, s)
    return out


def _order(files: dict[int, SessionFile]) -> list[SessionFile]:
    """Most worked-on first, then most recent."""
    return sorted(files.values(), key=lambda f: (-f.events, -f.last_ts))


def label(conn: sqlite3.Connection, s: Session) -> str:
    """What the session was about: its project, else its topic, else its folder."""
    if not s.files:
        return ""
    paths = [f.path for f in s.files]
    projects = conn.execute(
        """SELECT f.path, f.name FROM projects p JOIN files f ON f.id = p.folder_id"""
    ).fetchall()
    hits: Counter[str] = Counter()
    for ppath, pname in projects:
        prefix = ppath.rstrip("/\\") + ("/" if "/" in ppath else "\\")
        n = sum(1 for p in paths if p.startswith(prefix))
        if n:
            hits[pname] += n
    if hits:
        name, n = hits.most_common(1)[0]
        if n * 2 >= len(paths):
            return str(name)
    topics = Counter(topics_of(conn, [f.file_id for f in s.files]).values())
    if topics:
        name, n = topics.most_common(1)[0]
        if n * 2 >= len(paths):
            return name
    folders: Counter[str] = Counter()
    for p in paths:
        parent = PurePath(p).parent.name
        if parent and " ".join(words(parent)) not in GENERIC_FOLDERS:
            folders[parent] += 1
    if folders:
        return folders.most_common(1)[0][0]
    return s.files[0].name


def last_session(conn: sqlite3.Connection, now: float | None = None) -> Session | None:
    """The session to resume: the latest one, or the one before it if the latest is
    the current, just-started one (e.g. only Zachary was used since)."""
    now = time.time() if now is None else now
    all_s = sessions(conn, now - LOOKBACK_S, now)
    if not all_s:
        return None
    latest = all_s[-1]
    ongoing = now - latest.end < SESSION_GAP_S
    if ongoing and len(all_s) > 1 and latest.end - latest.start < 10 * 60:
        return all_s[-2]
    return latest


def resume_files(s: Session, limit: int = RESUME_MAX_FILES) -> list[SessionFile]:
    """What to reopen: the files worked on most (folders only if no files)."""
    files = [f for f in s.files if not f.is_dir]
    return (files or s.files)[:limit]

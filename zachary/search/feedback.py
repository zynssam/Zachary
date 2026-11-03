"""Learning from choices: files you picked for a query rank higher next time for
the same or a similar query ("you chose this before").

Every time a result is opened/revealed from a list, ``record`` stores the query's
words, the file and the rank it was shown at. ``boosts`` scores files for a new
query: similarity of the words (Jaccard) times a 30-day half-life decay, summed
over past choices. Local only; nothing leaves the machine.
"""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import time

HALF_LIFE_S = 30 * 86400
MAX_AGE_S = 365 * 86400
MIN_SIM = 0.5  # share at least half the words
SCAN_LIMIT = 5000  # most recent choices considered
BONUS = 0.012  # per unit of score: one exact recent choice moves a file up several places
MAX_SCORE = 2.0  # two choices beat an exact-name match (+0.02); then it saturates


def _key(terms: list[str]) -> tuple[str, str]:
    norm = " ".join(sorted({t.lower() for t in terms if t}))
    return hashlib.sha1(norm.encode()).hexdigest()[:16], norm


def record(
    conn: sqlite3.Connection,
    terms: list[str],
    file_id: int,
    rank_shown: int,
    now: float | None = None,
) -> None:
    if not terms:
        return  # "open the second one" with no query: nothing to learn from
    h, norm = _key(terms)
    with contextlib.suppress(sqlite3.Error):  # best-effort: never break an action over it
        conn.execute(
            "INSERT INTO feedback(query_hash, file_id, rank_shown, chosen, ts, terms) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (h, file_id, rank_shown, time.time() if now is None else now, norm),
        )


class _Index:
    """Past choices in memory, indexed by word; new rows are pulled in incrementally."""

    def __init__(self) -> None:
        self.last_id = 0
        self.rows: list[tuple[int, float, frozenset[str]]] = []
        self.by_word: dict[str, list[int]] = {}

    def refresh(self, conn: sqlite3.Connection, now: float) -> None:
        for rid, fid, ts, terms in conn.execute(
            "SELECT id, file_id, ts, terms FROM feedback WHERE id > ? AND chosen = 1 "
            "AND ts > ? ORDER BY id",
            (self.last_id, now - MAX_AGE_S),
        ):
            self.last_id = rid
            t = frozenset(str(terms).split())
            if not t:
                continue
            i = len(self.rows)
            self.rows.append((fid, ts, t))
            for w in t:
                self.by_word.setdefault(w, []).append(i)
        if len(self.rows) > 2 * SCAN_LIMIT:  # keep the most recent choices only
            self.__init__()  # type: ignore[misc]
            self.last_id = 0
            self._reload_recent(conn, now)

    def _reload_recent(self, conn: sqlite3.Connection, now: float) -> None:
        rows = conn.execute(
            "SELECT id, file_id, ts, terms FROM feedback WHERE chosen = 1 AND ts > ? "
            "ORDER BY id DESC LIMIT ?",
            (now - MAX_AGE_S, SCAN_LIMIT),
        ).fetchall()
        for rid, fid, ts, terms in reversed(rows):
            self.last_id = max(self.last_id, rid)
            t = frozenset(str(terms).split())
            if t:
                i = len(self.rows)
                self.rows.append((fid, ts, t))
                for w in t:
                    self.by_word.setdefault(w, []).append(i)


_INDEXES: dict[tuple[int, str], _Index] = {}


def _index_for(conn: sqlite3.Connection) -> _Index:
    # The file path too: Python may reuse a closed connection's id for a new one.
    path = str(conn.execute("PRAGMA database_list").fetchone()[2])
    return _INDEXES.setdefault((id(conn), path), _Index())


def boosts(
    conn: sqlite3.Connection, terms: list[str], now: float | None = None
) -> dict[int, float]:
    """file id -> score (sum over similar past choices of similarity x recency)."""
    if not terms:
        return {}
    now = time.time() if now is None else now
    idx = _index_for(conn)
    idx.refresh(conn, now)
    q = {t.lower() for t in terms}
    seen: set[int] = set()
    out: dict[int, float] = {}
    for w in q:
        for i in idx.by_word.get(w, ()):
            if i in seen:
                continue
            seen.add(i)
            fid, ts, t = idx.rows[i]
            if now - ts > MAX_AGE_S:
                continue
            sim = len(q & t) / len(q | t)
            if sim < MIN_SIM:
                continue
            out[fid] = out.get(fid, 0.0) + sim * 0.5 ** (max(0.0, now - ts) / HALF_LIFE_S)
    return {k: min(v, MAX_SCORE) for k, v in out.items()}

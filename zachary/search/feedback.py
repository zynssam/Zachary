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


def boosts(
    conn: sqlite3.Connection, terms: list[str], now: float | None = None
) -> dict[int, float]:
    """file id -> score (sum over similar past choices of similarity x recency)."""
    if not terms:
        return {}
    now = time.time() if now is None else now
    q = {t.lower() for t in terms}
    out: dict[int, float] = {}
    for fid, ts, row_terms in conn.execute(
        "SELECT file_id, ts, terms FROM feedback WHERE chosen = 1 AND ts > ? "
        "ORDER BY ts DESC LIMIT ?",
        (now - MAX_AGE_S, SCAN_LIMIT),
    ):
        t = set(str(row_terms).split())
        if not t:
            continue
        sim = len(q & t) / len(q | t)
        if sim < MIN_SIM:
            continue
        decay = 0.5 ** (max(0.0, now - ts) / HALF_LIFE_S)
        out[fid] = out.get(fid, 0.0) + sim * decay
    return {k: min(v, MAX_SCORE) for k, v in out.items()}

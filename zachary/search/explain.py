"""Why a result matched: one short line per result, built only for shown results.

Pieces: "exact name", "opened yesterday", "name: fee, receipt", "folder: dsa",
"about: <best matching passage>", "latest of 3 versions", "opened 2 h ago".
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import numpy as np

from zachary.search.tokens import words

if TYPE_CHECKING:
    from zachary.indexer.embedder import FloatArray
    from zachary.search.hybrid import SearchResult

PASSAGE_CHARS = 90


def ago(ts: float, now: float) -> str:
    d = max(0.0, now - ts)
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    days = int(d // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


def term_in(term: str, ws: list[str]) -> bool:
    """Loose word match on a shared stem ("fee"/"fees", "linked"/"link"), mirroring the
    stemmed FTS match closely enough for ranking and explanations."""
    for w in ws:
        k = min(4, len(w), len(term))
        if k >= 2 and w[:k] == term[:k]:
            return True
    return False


def name_hits(name: str, terms: list[str]) -> int:
    ws = words(name)
    return sum(1 for t in terms if term_in(t, ws))


def why_terms(r: SearchResult, terms: list[str]) -> list[str]:
    name_words = words(r.name)
    path_words = words(r.path[: -len(r.name)] if r.path.endswith(r.name) else r.path)
    in_name = [t for t in terms if term_in(t, name_words)]
    in_path = [t for t in terms if t not in in_name and term_in(t, path_words)]
    why = []
    if in_name:
        why.append("name: " + ", ".join(in_name))
    if in_path:
        why.append("folder: " + ", ".join(in_path))
    return why


def best_passage(conn: sqlite3.Connection, file_id: int, q: FloatArray) -> tuple[str, float] | None:
    """The file's chunk closest to the query, as a short snippet, with its cosine."""
    rows = conn.execute(
        "SELECT c.text_excerpt, v.embedding FROM chunks c JOIN vec_chunks v ON v.chunk_id = c.id"
        " WHERE c.file_id = ?",
        (file_id,),
    ).fetchall()
    if not rows:
        return None
    m = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.int8).reshape(len(rows), -1)
    sims = (m.astype(np.float32) @ q) / 127.0
    best = int(np.argmax(sims))
    return snippet(rows[best][0]), float(sims[best])


def snippet(text: str, n: int = PASSAGE_CHARS) -> str:
    t = " ".join(text.split())
    if len(t) <= n:
        return t
    cut = t.rfind(" ", 0, n)
    return t[: cut if cut > n // 2 else n] + "…"

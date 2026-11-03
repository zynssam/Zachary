"""Ask your files: find the passages that answer a question, locally.

"when is my fee due?" -> the files that match (hybrid search: keywords + meaning),
then the chunks inside them, scored by meaning (chunk vectors from Phase 3) and by
how many of the question's words they contain. Only these few passages are ever
given to the optional LLM (``brain/llm.py``); with no LLM the best one is shown.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

import numpy as np

from zachary.core.config import PathRules
from zachary.indexer.embedder import dequantize_many, normalize
from zachary.search.hybrid import STOPWORDS, search
from zachary.search.tokens import words
from zachary.search.vectors import SemanticSearch

CANDIDATE_FILES = 12  # from hybrid search (plus older versions of the top ones)
MAX_FILES = 30
TOP_PASSAGES = 4  # sent to the LLM / shown
PER_FILE = 2
SNIPPET_CHARS = 280
W_MEANING, W_WORDS, W_RANK = 0.55, 0.35, 0.10
# Questions are usually about documents ("when is my fee due?" must not answer from a
# scheduler's "next_due" variable); code still wins when the question is about code.
KIND_WEIGHT = {"document": 1.0, "code": 0.7}
CODE_WORDS = frozenset(
    "code function class method variable bug error script program python java cpp js api".split()
)

# Words that make a question but say nothing about its subject.
QUESTION_STOP = frozenset(
    """
    when what whats how who whom whose which why where is are was were do does did can could
    should will would shall much many my mine me i the a an of in on for to at by about tell
    please kab kitna kitni kitne kaun kaunsa kya kyun hai hain tha thi mera meri mere ka ki ke
    say says said mention mentioned according file files document documents doc docs
    """.split()
)


@dataclass(slots=True)
class Passage:
    file_id: int
    path: str
    name: str
    kind: str
    mtime: float
    chunk_id: int
    text: str  # the whole chunk (<= ~1,000 characters)
    snippet: str  # the part to show
    score: float


def question_terms(question: str) -> list[str]:
    out: list[str] = []
    for w in words(question):
        if w in QUESTION_STOP or w in STOPWORDS or len(w) < 2 or w in out:
            continue
        out.append(w)
    return out


def _stem(term: str) -> str:
    """ "fees"/"fee", "installments"/"installment": compare word starts."""
    return term if len(term) <= 4 else term[: max(4, len(term) - 2)]


def _term_patterns(terms: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(r"\b" + re.escape(_stem(t)), re.IGNORECASE) for t in terms]


def _word_hits(text: str, patterns: list[re.Pattern[str]]) -> float:
    """Share of the question's words that appear (as word starts) in ``text``."""
    if not patterns:
        return 0.0
    return sum(1 for p in patterns if p.search(text)) / len(patterns)


def snippet(text: str, terms: list[str], width: int = SNIPPET_CHARS) -> str:
    """The window of ``text`` with the most question words, cut at sentence/line edges."""
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= width:
        return flat
    stems = [_stem(t) for t in terms]
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, stems)) + r")", re.I) if stems else None
    hits = [m.start() for m in pattern.finditer(flat)] if pattern else []
    best, best_n = 0, -1
    for h in hits or [0]:
        start = max(0, h - width // 3)
        n = sum(1 for x in hits if start <= x < start + width)
        if n > best_n:
            best, best_n = start, n
    start = best
    # Begin at a sentence or line start when one is close.
    cut = max(flat.rfind(". ", max(0, start - 60), start + 1), -1)
    if cut >= 0 and start - cut < 60:
        start = cut + 2
    end = min(len(flat), start + width)
    if end < len(flat):
        stop = flat.rfind(". ", start + width // 2, end)
        end = stop + 1 if stop > 0 else flat.rfind(" ", start, end)
    body = flat[start:end].strip()
    return ("…" if start > 0 else "") + body + ("…" if end < len(flat) else "")


def _candidates(
    conn: sqlite3.Connection,
    question: str,
    terms: list[str],
    semantic: SemanticSearch | None,
    rules: PathRules | None,
) -> list[int]:
    query = " ".join(terms) or question
    results = search(conn, query, limit=CANDIDATE_FILES, rules=rules, semantic=semantic).results
    ids: list[int] = []
    for r in results:
        if not r.is_dir and r.file_id not in ids:
            ids.append(r.file_id)
    # Older versions of the top files can hold the answer too ("FeeReceipt (3).pdf").
    for r in results[:3]:
        for v in r.versions:
            if v not in ids:
                ids.append(v)
    return ids[:MAX_FILES]


def retrieve(
    conn: sqlite3.Connection,
    question: str,
    *,
    semantic: SemanticSearch | None = None,
    rules: PathRules | None = None,
    k: int = TOP_PASSAGES,
) -> list[Passage]:
    """The best passages for ``question``, best first (at most ``PER_FILE`` per file)."""
    terms = question_terms(question)
    ids = _candidates(conn, question, terms, semantic, rules)
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT c.id, c.file_id, c.text_excerpt, f.path, f.name, f.kind, f.mtime
            FROM chunks c JOIN files f ON f.id = c.file_id
            WHERE c.file_id IN ({marks}) ORDER BY c.file_id, c.ord""",
        ids,
    ).fetchall()
    if not rows:
        return []
    rank = {fid: i for i, fid in enumerate(ids)}
    meaning = np.zeros(len(rows), dtype=np.float32)
    if semantic is not None:
        q = normalize(semantic.embedder.encode([question]))[0]
        # One lookup per chunk: sqlite-vec scans the whole table for "IN (...)"
        # (237 ms on 270k chunks) but finds a single id directly (~0.02 ms).
        vecs: dict[int, bytes] = {}
        for r in rows:
            got = conn.execute(
                "SELECT embedding FROM vec_chunks WHERE chunk_id = ?", (r[0],)
            ).fetchone()
            if got is not None:
                vecs[r[0]] = got[0]
        have = [i for i, r in enumerate(rows) if r[0] in vecs]
        if have:
            m = dequantize_many([vecs[rows[i][0]] for i in have])
            meaning[have] = np.clip(m @ q, 0.0, 1.0)
    code_q = any(t in CODE_WORDS for t in terms)
    patterns = _term_patterns(terms)
    excluded: dict[int, bool] = {}
    scored: list[Passage] = []
    for i, (cid, fid, text, path, name, kind, mtime) in enumerate(rows):
        if fid not in excluded:
            excluded[fid] = rules is not None and rules.is_excluded(path, False)
        if excluded[fid]:
            continue
        s = (
            W_MEANING * float(meaning[i])
            + W_WORDS * _word_hits(text, patterns)
            + W_RANK * (1.0 - rank[fid] / len(ids))
        ) * (1.0 if code_q else KIND_WEIGHT.get(kind, 0.9))
        scored.append(Passage(fid, path, name, kind, float(mtime), cid, text, "", s))
    scored.sort(key=lambda p: -p.score)
    out: list[Passage] = []
    per: dict[int, int] = {}
    seen: set[str] = set()
    for p in scored:
        if per.get(p.file_id, 0) >= PER_FILE:
            continue
        p.snippet = snippet(p.text, terms)  # only for the passages we keep
        if p.snippet in seen:
            continue
        per[p.file_id] = per.get(p.file_id, 0) + 1
        seen.add(p.snippet)
        out.append(p)
        if len(out) >= k:
            break
    return out

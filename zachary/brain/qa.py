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

GROUNDED_MIN = 0.3  # all question words in a passage this relevant: about your files
NAMED_MIN = 0.4  # ... or a question word in the best file's name
CANDIDATE_FILES = 12  # from hybrid search (plus older versions of the top ones)
MAX_FILES = 30
TOP_PASSAGES = 4  # sent to the LLM / shown
PER_FILE = 2
SNIPPET_CHARS = 280
W_MEANING, W_WORDS, W_RANK = 0.55, 0.35, 0.10
# Questions are usually about documents ("when is my fee due?" must not answer from a
# scheduler's "next_due" variable); code still wins when the question is about code.
KIND_WEIGHT = {"document": 1.0, "code": 0.5}
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
    coverage: float = 0.0  # share of the question's words found in this passage


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


FOLDER_FILES = 120  # files of a project scored for a question (docs first)
_DOC_EXT = (".md", ".txt", ".rst", ".pdf", ".docx", ".pptx")
_BUILD_DIRS = frozenset("build dist out target .venv venv __pycache__ node_modules .git".split())
_NOT_DOCS = frozenset(
    "sources.txt top_level.txt dependency_links.txt requires.txt pkg-info license license.txt"
    " license.md".split()
)


def _folder_candidates(conn: sqlite3.Connection, folder: str, terms: list[str]) -> list[int]:
    """Files inside ``folder`` that have text: READMEs, then docs, then files whose name
    has a question word, then the most recently changed ones."""
    f = folder.rstrip("/\\")
    sep = "\\" if "\\" in f and "/" not in f else "/"
    lo, hi = f + sep, f + chr(ord(sep) + 1)
    rows = conn.execute(
        """SELECT f.id, f.name, f.mtime, f.path FROM files f
           WHERE f.path >= ? AND f.path < ? AND f.is_dir = 0
             AND EXISTS (SELECT 1 FROM chunks c WHERE c.file_id = f.id)
           ORDER BY f.mtime DESC LIMIT 2000""",
        (lo, hi),
    ).fetchall()
    stems = [_stem(t) for t in terms]

    def build_output(path: str) -> bool:
        parts = path[len(lo) :].replace("\\", "/").split("/")[:-1]
        return any(x.endswith(".egg-info") or x in _BUILD_DIRS for x in parts)

    def rank(r: tuple[int, str, float, str]) -> tuple[int, int, float]:
        n = r[1].lower()
        depth = r[3][len(lo) :].replace("\\", "/").count("/")
        if n.startswith("readme"):
            pri = 0
        elif n.endswith(_DOC_EXT) and n not in _NOT_DOCS:
            pri = 1
        elif any(s in n for s in stems):
            pri = 2
        else:
            pri = 3
        return (pri, depth, -r[2])  # top-level docs (claude.md) before deep ones

    rows = [r for r in rows if not build_output(r[3]) and r[1].lower() not in _NOT_DOCS]
    return [r[0] for r in sorted(rows, key=rank)[:FOLDER_FILES]]


def retrieve(
    conn: sqlite3.Connection,
    question: str,
    *,
    semantic: SemanticSearch | None = None,
    rules: PathRules | None = None,
    k: int = TOP_PASSAGES,
    within: str | None = None,
) -> list[Passage]:
    """The best passages for ``question``, best first (at most ``PER_FILE`` per file).
    ``within``: only files inside this folder (a project), docs and READMEs first."""
    terms = question_terms(question)
    ids = (
        _folder_candidates(conn, within, terms)
        if within
        else _candidates(conn, question, terms, semantic, rules)
    )
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
        cov = _word_hits(text, patterns)
        s = (
            W_MEANING * float(meaning[i]) + W_WORDS * cov + W_RANK * (1.0 - rank[fid] / len(ids))
        ) * (1.0 if code_q else KIND_WEIGHT.get(kind, 0.9))
        scored.append(Passage(fid, path, name, kind, float(mtime), cid, text, "", s, cov))
    scored.sort(key=lambda p: -p.score)
    # Several files before a second passage from the same file: one file that happens
    # to repeat the question's words mustn't crowd out the others.
    out: list[Passage] = []
    per: dict[int, int] = {}
    seen: set[str] = set()
    for round_ in range(1, PER_FILE + 1):
        for p in scored:
            if len(out) >= k:
                break
            if per.get(p.file_id, 0) >= round_ or p in out:
                continue
            p.snippet = p.snippet or snippet(p.text, terms)  # only for passages we keep
            if p.snippet in seen:
                continue
            per[p.file_id] = per.get(p.file_id, 0) + 1
            seen.add(p.snippet)
            out.append(p)
    out.sort(key=lambda p: -p.score)
    return out


def name_hit(question: str, passage: Passage) -> bool:
    """A question word in the file's name ("hackfest pitch" -> hackfest-pitch-deck.pptx)."""
    name = words(passage.name.rsplit(".", 1)[0])
    return any(any(n.startswith(_stem(t)) for n in name) for t in question_terms(question)
               if len(t) >= 3)  # fmt: skip


def about_files(question: str, passages: list[Passage], personal: bool) -> bool:
    """Answer from the user's files (else it's a general question for the LLM)."""
    if personal:
        return True
    if not passages:
        return False
    top = passages[0]
    return (top.coverage >= 0.99 and top.score >= GROUNDED_MIN) or (
        top.score >= NAMED_MIN and name_hit(question, top)
    )

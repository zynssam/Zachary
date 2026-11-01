"""Hybrid search over Zachary's DB. Phase 1: keyword (FTS5) + recency.

Never touches the disk. Ranking:

- keyword list: FTS5 bm25 (name weighted above folder words), AND of all terms
  first, then OR to fill up; the last term is a prefix (as-you-type);
- recency list: newest of mtime and last access (``access_log``);
- the lists are merged with reciprocal rank fusion (RRF), plus a bonus for an
  exact name match;
- a time phrase ("yesterday", "kal", "last Tuesday") restricts results to files
  opened or edited in that range; if nothing matches in range, keyword results
  are returned anyway and marked as outside the range;
- if no term matches at all, terms are corrected against the indexed vocabulary
  (typos such as "recipt").

Phase 3 adds vector similarity and feedback as further RRF lists.
"""

from __future__ import annotations

import difflib
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime

from zachary.brain.timeparse import TimeRange, parse_time
from zachary.core.config import ExcludeMatcher
from zachary.search.tokens import words

RRF_K = 60
KW_WEIGHT = 1.0
RECENCY_WEIGHT = 0.5
EXACT_NAME_BONUS = 0.02
# Keyword tier bonus: all terms as whole words > all terms with the last as a prefix >
# any term whole > any term prefix. Keeps "fees" -> "Fee Receipt" above "feedback".
TIER_BONUS = (0.01, 0.004, 0.002, 0.0)
IN_RANGE_BONUS = 0.05
FTS_LIMIT = 300
RANGE_FTS_LIMIT = 5000
RANGE_LIMIT = 2000
# bm25 column weights: name, path_tokens, title, keywords
BM25 = "bm25(files_fts, 10.0, 2.0, 6.0, 3.0)"

# Filler dropped from queries (English + Hinglish). Intent words ("open", "find")
# are handled by the intent parser in Phase 2; here they'd only add noise.
STOPWORDS = frozenset(
    """
    a an the my me i i'm im it its this that these those of for to in on at by with from
    about and or is was were be been am are do did does please can could you your
    open opened find show search get give look looking locate where which what file
    files document documents doc docs folder folders thing stuff some any one ones
    zach zachary hey ok okay
    mera meri mere mujhe maine humne wo woh vo voh jo ye yeh kya kahan kidhar wala
    wali wale waala waali ka ki ke ko se mein me hai tha thi the hain kholi khola kholo
    dikhao dikha do de dena chahiye wali naam bhool gaya gayi
    nanna nange aa ee yaava elli
    """.split()
)


@dataclass(slots=True)
class SearchResult:
    file_id: int
    path: str
    name: str
    kind: str
    is_dir: bool
    mtime: float
    last_access: float | None
    score: float
    why: list[str] = field(default_factory=list)

    @property
    def last_seen(self) -> float:
        return max(self.mtime, self.last_access or 0.0)


@dataclass(slots=True)
class SearchResponse:
    query: str
    terms: list[str]
    time_range: TimeRange | None
    results: list[SearchResult]
    took_ms: float
    corrected: dict[str, str] = field(default_factory=dict)
    range_matched: bool = True  # False: nothing in the time range; results ignore it


def query_terms(text: str) -> list[str]:
    return [w for w in words(text) if w not in STOPWORDS]


def _fts_quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def fts_expression(terms: list[str], *, mode: str, prefix: bool = True) -> str:
    """AND/OR expression; with ``prefix`` the last term also matches as a prefix."""
    parts = [_fts_quote(t) for t in terms]
    if prefix and parts and len(terms[-1]) >= 2:
        parts[-1] = parts[-1] + "*"
    return (" OR " if mode == "or" else " AND ").join(parts)


def _keyword_ids(conn: sqlite3.Connection, terms: list[str], limit: int) -> dict[int, int]:
    """Matching file ids, best first, in tiers: whole words before as-you-type prefixes,
    all terms (AND) before any term (OR). Within a tier, bm25 decides.

    Tiers stop it ranking "feedback" (prefix of "fee") above "Fee Receipt" for "fees".
    """
    if not terms:
        return {}
    out: dict[int, int] = {}  # id -> tier (insertion order = rank)
    modes = ["and", "or"] if len(terms) > 1 else ["and"]
    for m, mode in enumerate(modes):
        for prefix in (False, True):
            tier = 2 * m + int(prefix)
            if prefix and len(terms[-1]) < 2:
                continue
            expr = fts_expression(terms, mode=mode, prefix=prefix)
            try:
                rows = conn.execute(
                    f"SELECT rowid FROM files_fts WHERE files_fts MATCH ? ORDER BY {BM25} LIMIT ?",
                    (expr, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for (rid,) in rows:
                out.setdefault(int(rid), tier)
        if len(out) >= 20:
            break
    return out


def correct_terms(conn: sqlite3.Connection, terms: list[str]) -> dict[str, str]:
    """Closest indexed term (same first letter) for each term with no hits."""
    fixes: dict[str, str] = {}
    for t in terms:
        if len(t) < 4:
            continue
        hit = conn.execute(
            "SELECT 1 FROM files_fts WHERE files_fts MATCH ? LIMIT 1", (_fts_quote(t),)
        ).fetchone()
        if hit:
            continue
        lo = t[0]
        hi = chr(ord(lo) + 1)
        vocab = [
            r[0]
            for r in conn.execute(
                "SELECT term FROM files_fts_vocab WHERE term >= ? AND term < ?"
                " AND length(term) BETWEEN ? AND ? ORDER BY doc DESC LIMIT 20000",
                (lo, hi, len(t) - 2, len(t) + 2),
            )
        ]
        best = difflib.get_close_matches(t, vocab, n=1, cutoff=0.75)
        if best:
            fixes[t] = best[0]
    return fixes


def _ids_in_range(conn: sqlite3.Connection, tr: TimeRange, limit: int) -> dict[int, str]:
    """file id -> how it relates to the range ('opened' beats 'edited')."""
    found: dict[int, str] = {}
    for fid, source in conn.execute(
        "SELECT file_id, source FROM access_log WHERE ts >= ? AND ts < ? ORDER BY ts DESC LIMIT ?",
        (tr.start, tr.end, limit),
    ):
        if fid not in found or source != "edit":
            found[int(fid)] = "edited" if source == "edit" else "opened"
    for (fid,) in conn.execute(
        "SELECT id FROM files WHERE mtime >= ? AND mtime < ? AND is_dir = 0"
        " ORDER BY mtime DESC LIMIT ?",
        (tr.start, tr.end, limit),
    ):
        found.setdefault(int(fid), "edited")
    return found


def _load(conn: sqlite3.Connection, ids: list[int]) -> dict[int, SearchResult]:
    out: dict[int, SearchResult] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i : i + 900]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT id, path, name, kind, is_dir, mtime FROM files WHERE id IN ({marks})", chunk
        ):
            out[int(r[0])] = SearchResult(
                int(r[0]), str(r[1]), str(r[2]), str(r[3]), bool(r[4]), float(r[5]), None, 0.0
            )
        for fid, ts in conn.execute(
            f"SELECT file_id, max(ts) FROM access_log WHERE file_id IN ({marks})"
            " AND source != 'edit' GROUP BY file_id",
            chunk,
        ):
            if int(fid) in out:
                out[int(fid)].last_access = float(ts)
    return out


def _ago(ts: float, now: float) -> str:
    d = max(0.0, now - ts)
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    days = int(d // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


def _why_terms(r: SearchResult, terms: list[str]) -> list[str]:
    name_words = words(r.name)
    path_words = words(r.path[: -len(r.name)] if r.path.endswith(r.name) else r.path)
    in_name = [t for t in terms if any(w.startswith(t[:4]) for w in name_words)]
    in_path = [
        t for t in terms if t not in in_name and any(w.startswith(t[:4]) for w in path_words)
    ]
    why = []
    if in_name:
        why.append("name: " + ", ".join(in_name))
    if in_path:
        why.append("folder: " + ", ".join(in_path))
    return why


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
    now: datetime | None = None,
    include_dirs: bool = True,
    exclude: ExcludeMatcher | None = None,
) -> SearchResponse:
    """Search the index. ``exclude`` hides rows the indexer hasn't purged yet."""
    t0 = time.perf_counter()
    now_dt = now or datetime.now().astimezone()
    now_ts = now_dt.timestamp()
    tp = parse_time(query, now_dt)
    terms = query_terms(tp.rest)
    tr = tp.range

    kw = _keyword_ids(conn, terms, RANGE_FTS_LIMIT if tr else FTS_LIMIT)
    corrected: dict[str, str] = {}
    if terms and not kw:
        corrected = correct_terms(conn, terms)
        if corrected:
            terms = [corrected.get(t, t) for t in terms]
            kw = _keyword_ids(conn, terms, RANGE_FTS_LIMIT if tr else FTS_LIMIT)

    in_range: dict[int, str] = _ids_in_range(conn, tr, RANGE_LIMIT) if tr else {}
    range_matched = True
    kw_ids = list(kw)
    if tr is not None:
        if terms:
            filtered = [i for i in kw_ids if i in in_range]
            if filtered:
                candidates = filtered
            else:
                candidates, range_matched = kw_ids[:FTS_LIMIT], False
        else:
            candidates = list(in_range)
    else:
        candidates = kw_ids

    rows = _load(conn, candidates)
    if exclude is not None:
        rows = {k: v for k, v in rows.items() if not exclude.is_excluded(v.path, is_dir=v.is_dir)}
    if not include_dirs:
        rows = {k: v for k, v in rows.items() if not v.is_dir}

    kw_rank = {fid: i for i, fid in enumerate(i for i in candidates if i in rows)} if terms else {}
    # Dense rank by recency (to the minute): equal times share a rank, so ties don't
    # get an arbitrary order that could outweigh keyword quality.
    minutes = sorted({int(r.last_seen // 60) for r in rows.values()}, reverse=True)
    dense = {m: i for i, m in enumerate(minutes)}
    rec_rank = {r.file_id: dense[int(r.last_seen // 60)] for r in rows.values()}
    phrase = " ".join(terms)
    for r in rows.values():
        s = RECENCY_WEIGHT / (RRF_K + rec_rank[r.file_id])
        if r.file_id in kw_rank:
            s += KW_WEIGHT / (RRF_K + kw_rank[r.file_id]) + TIER_BONUS[kw[r.file_id]]
        stem = r.name.rsplit(".", 1)[0] if not r.is_dir else r.name
        if phrase and " ".join(words(stem)) == phrase:
            s += EXACT_NAME_BONUS
            r.why.append("exact name")
        if r.file_id in in_range and range_matched:
            s += IN_RANGE_BONUS
            assert tr is not None
            r.why.append(f"{in_range[r.file_id]} {tr.label}")
        r.why.extend(_why_terms(r, terms))
        if not (r.file_id in in_range and range_matched):
            if r.last_access and r.last_access >= r.mtime:
                r.why.append(f"opened {_ago(r.last_access, now_ts)}")
            else:
                r.why.append(f"modified {_ago(r.mtime, now_ts)}")
        r.score = s

    results = sorted(rows.values(), key=lambda r: (-r.score, -r.last_seen))[:limit]
    return SearchResponse(
        query=query,
        terms=terms,
        time_range=tr,
        results=results,
        took_ms=(time.perf_counter() - t0) * 1e3,
        corrected=corrected,
        range_matched=range_matched,
    )


@dataclass(slots=True)
class RecentItemView:
    file_id: int
    path: str
    name: str
    kind: str
    ts: float
    how: str  # opened | edited


def recent(
    conn: sqlite3.Connection, *, limit: int = 20, time_range: TimeRange | None = None
) -> list[RecentItemView]:
    """Recently opened (OS recent / Zachary) or edited files, newest first."""
    start = time_range.start if time_range else 0.0
    end = time_range.end if time_range else float("inf")
    best: dict[int, tuple[float, str]] = {}
    for fid, ts, source in conn.execute(
        "SELECT file_id, max(ts), source FROM access_log WHERE ts >= ? AND ts < ?"
        " GROUP BY file_id, source ORDER BY max(ts) DESC LIMIT ?",
        (start, end, limit * 4),
    ):
        how = "edited" if source == "edit" else "opened"
        if fid not in best or float(ts) > best[fid][0]:
            best[int(fid)] = (float(ts), how)
    for fid, mtime in conn.execute(
        "SELECT id, mtime FROM files WHERE is_dir = 0 AND mtime >= ? AND mtime < ?"
        " ORDER BY mtime DESC LIMIT ?",
        (start, end, limit),
    ):
        if fid not in best or float(mtime) > best[fid][0]:
            best[int(fid)] = (float(mtime), "edited")
    top = sorted(best.items(), key=lambda kv: -kv[1][0])[: limit * 2]
    info = (
        {
            int(r[0]): r
            for r in conn.execute(
                f"SELECT id, path, name, kind FROM files WHERE id IN ({','.join('?' * len(top))})",
                [k for k, _ in top],
            )
        }
        if top
        else {}
    )
    out = [
        RecentItemView(fid, str(info[fid][1]), str(info[fid][2]), str(info[fid][3]), ts, how)
        for fid, (ts, how) in top
        if fid in info
    ]
    return out[:limit]

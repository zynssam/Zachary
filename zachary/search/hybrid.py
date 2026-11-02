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
from typing import TYPE_CHECKING

from zachary.brain.timeparse import TimeRange, parse_time
from zachary.core.config import PathRules
from zachary.search.explain import ago, best_passage, name_hits, why_terms
from zachary.search.tokens import words
from zachary.search.vectors import SemanticSearch

if TYPE_CHECKING:
    from zachary.indexer.embedder import FloatArray

RRF_K = 60
KW_WEIGHT = 1.0
RECENCY_WEIGHT = 0.5
EXACT_NAME_BONUS = 0.02
# Keyword tier bonus: all terms as whole words > all terms with the last as a prefix >
# any term whole > any term prefix. Keeps "fees" -> "Fee Receipt" above "feedback".
TIER_BONUS = (0.01, 0.006, 0.002, 0.0)
IN_RANGE_BONUS = 0.05
FTS_LIMIT = 120  # keyword candidates (we show ~10; RRF needs a margin, not hundreds)
VEC_K = 30  # nearest neighbours taken from the vector index
# Meaning matches: Model2Vec cosines for relevant files measured 0.25-0.73 while
# unrelated ones mostly stay < 0.1 but can reach ~0.28, so a fixed cutoff can't
# separate them. Keep a match if it is above an absolute floor AND within a
# fraction of the best match for this query.
VEC_MIN_SIM = 0.15
VEC_REL_SIM = 0.25
# Meaning alone is strong enough to act on (open without asking) from this cosine.
# Measured on a real 9.5k-file index: right answers 0.44-0.61, wrong picks 0.18-0.27.
STRONG_SIM = 0.40
VEC_WEIGHT = 0.9
RANGE_KW_ENOUGH = 50  # in-range keyword matches after which looser tiers are skipped
RANGE_LIMIT = 1000
RANGE_VEC_LIMIT = 500  # in-range files scored by meaning (most recent first)
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
    zach zachary hey ok okay everything anything all things related regarding forgot forget
    remember name named called opening opens it
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
    versions: list[int] = field(default_factory=list)  # older versions (file ids), newest first
    # A keyword/name hit, an exact name, or a close meaning: safe to act on without
    # asking. Time or recency alone never is.
    strong: bool = False

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


# Words that say what *kind* of file is wanted. They boost that kind and are not
# matched against names ("linked list code" -> terms "linked list", kind code).
KIND_HINTS: dict[str, str] = {
    "document": "document", "documents": "document", "doc": "document", "docs": "document",
    "spreadsheet": "document", "slides": "document", "presentation": "document",
    "code": "code", "script": "code", "program": "code", "source": "code",
    "photo": "image", "photos": "image", "picture": "image", "pictures": "image",
    "image": "image", "images": "image", "pic": "image", "pics": "image",
    "video": "video", "videos": "video", "movie": "video", "clip": "video",
    "song": "audio", "songs": "audio", "music": "audio", "audio": "audio",
    "folder": "dir", "folders": "dir", "directory": "dir",
}  # fmt: skip
KIND_BONUS = 0.015
# With no hint, a file-memory search mildly prefers documents and code over media.
KIND_PRIOR = {"document": 0.003, "code": 0.002, "dir": 0.001}


def split_kind_hints(
    terms: list[str], raw_words: list[str], *, keep_if_alone: bool
) -> tuple[list[str], set[str]]:
    """Separate kind words from keyword terms. ``raw_words`` include stopwords."""
    kinds = {KIND_HINTS[w] for w in raw_words if w in KIND_HINTS}
    kept = [t for t in terms if t not in KIND_HINTS]
    if not kept and keep_if_alone:
        return terms, kinds  # "photo" alone: still match names containing "photo"
    return kept, kinds


def _kind_score(r: SearchResult, kinds: set[str]) -> float:
    if kinds:
        return KIND_BONUS if r.kind in kinds else 0.0
    return KIND_PRIOR.get(r.kind, 0.0)


KIND_ONLY_LIMIT = 200


def _recent_of_kinds(conn: sqlite3.Connection, kinds: set[str], limit: int) -> list[int]:
    """Newest first: modified, or opened (access log), whichever is later."""
    marks = ",".join("?" * len(kinds))
    rows = _raw(conn).execute(
        f"""SELECT f.id FROM files f
            LEFT JOIN (SELECT file_id, max(ts) AS ts FROM access_log GROUP BY file_id) a
              ON a.file_id = f.id
            WHERE f.kind IN ({marks})
              AND NOT (f.is_dir AND f.parent_id IS NULL)  -- an index root (e.g. home)
            ORDER BY max(f.mtime, coalesce(a.ts, 0)) DESC LIMIT ?""",
        [*sorted(kinds), limit],
    )
    return [r[0] for r in rows]


def _kinds_of(conn: sqlite3.Connection, ids: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i : i + 900]
        marks = ",".join("?" * len(chunk))
        for fid, kind in _raw(conn).execute(
            f"SELECT id, kind FROM files WHERE id IN ({marks})", chunk
        ):
            out[fid] = kind
    return out


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


def _raw(conn: sqlite3.Connection) -> sqlite3.Cursor:
    """Cursor returning plain tuples (sqlite3.Row costs ~1 us per row on hot paths)."""
    cur = conn.cursor()
    cur.row_factory = None
    return cur


def _fts_ids(conn: sqlite3.Connection, expr: str, limit: int) -> list[tuple[int, float]]:
    """Best ``limit`` matches with their bm25 (lower is better)."""
    try:
        rows = _raw(conn).execute(
            f"SELECT rowid, {BM25} AS s FROM files_fts WHERE files_fts MATCH ? ORDER BY s LIMIT ?",
            (expr, limit),
        )
        return [(r[0], float(r[1])) for r in rows]
    except sqlite3.OperationalError:
        return []


DENSE_PREFIX_DOCS = 20_000  # prefixes matching more files than this skip bm25
SHORT_PREFIX_SCAN = 5000


def prefix_doc_estimate(conn: sqlite3.Connection, prefix: str) -> int:
    """Roughly how many files an as-you-type prefix matches (vocab lookup, ~1 ms)."""
    if not prefix:
        return 0
    hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    row = (
        _raw(conn)
        .execute(
            "SELECT coalesce(sum(doc), 0) FROM files_fts_vocab WHERE term >= ? AND term < ?",
            (prefix, hi),
        )
        .fetchone()
    )
    return int(row[0])


def _fts_recent(conn: sqlite3.Connection, expr: str, limit: int) -> list[tuple[int, float]]:
    """Most recently modified matches, unranked: for dense prefixes ("re*", "proj*") that
    match a large part of the index, where bm25-sorting everything costs ~100 ms and
    adds little while the user is still typing."""
    try:
        rows = _raw(conn).execute(
            "SELECT f.id FROM files f WHERE f.id IN (SELECT rowid FROM files_fts"
            " WHERE files_fts MATCH ? LIMIT ?) ORDER BY f.mtime DESC LIMIT ?",
            (expr, SHORT_PREFIX_SCAN, limit),
        )
        return [(r[0], 0.0) for r in rows]
    except sqlite3.OperationalError:
        return []


def _fts_in(conn: sqlite3.Connection, expr: str, allowed: set[int]) -> list[int]:
    """All matches inside ``allowed``, unranked: a sequential posting-list scan (~1 us/row).

    Used for time-range queries, where the allowed set is small. Ranking every match
    with bm25, or seeking FTS once per allowed rowid, are both far slower (measured).
    """
    try:
        rows = _raw(conn).execute("SELECT rowid FROM files_fts WHERE files_fts MATCH ?", (expr,))
        return [r[0] for r in rows if r[0] in allowed]
    except sqlite3.OperationalError:
        return []


def _keyword_ids(
    conn: sqlite3.Connection,
    terms: list[str],
    limit: int,
    allowed: set[int] | None = None,
    scores: dict[int, float] | None = None,
) -> dict[int, int]:
    """Matching file ids -> tier, best first (dict order = rank when ``allowed`` is None).

    Tiers (bm25 orders rows within a tier):
      0  all terms as whole words
      1  all terms, the last one as an as-you-type prefix
      2  all terms but one (a word the user added that no filename has, e.g. "code")
      3  any term: only if nothing above matched, since OR over common words can
         touch tens of thousands of rows

    Tiers also keep "fees" -> "Fee Receipt" above prefix-only "feedback". With
    ``allowed`` (time-range queries) matches are restricted to those ids, unranked.
    """
    if not terms:
        return {}
    terms = terms[:6]
    out: dict[int, int] = {}

    def add(expr: str, tier: int, short_prefix: bool = False) -> None:
        if allowed is not None:
            for rid in _fts_in(conn, expr, allowed):
                out.setdefault(rid, tier)
            return
        found = _fts_recent(conn, expr, limit) if short_prefix else _fts_ids(conn, expr, limit)
        for rid, bm in found:
            if rid not in out:
                out[rid] = tier
                if scores is not None:
                    scores[rid] = bm

    add(fts_expression(terms, mode="and", prefix=False), 0)
    if len(terms[-1]) >= 2 and len(out) < limit:
        dense = prefix_doc_estimate(conn, terms[-1]) > DENSE_PREFIX_DOCS
        add(fts_expression(terms, mode="and", prefix=True), 1, dense)
    if len(out) >= 20 or len(terms) == 1:
        return out
    if len(terms) > 2 or not out:
        for k in range(len(terms)):
            sub = terms[:k] + terms[k + 1 :]
            add(fts_expression(sub, mode="and", prefix=k != len(terms) - 1), 2)
    if not out:
        add(fts_expression(terms, mode="or", prefix=True), 3)
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
                " AND length(term) BETWEEN ? AND ? ORDER BY doc DESC LIMIT 4000",
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
    for fid, source in _raw(conn).execute(
        "SELECT file_id, source FROM access_log WHERE ts >= ? AND ts < ? ORDER BY ts DESC LIMIT ?",
        (tr.start, tr.end, limit),
    ):
        if fid not in found or source != "edit":
            found[int(fid)] = "edited" if source == "edit" else "opened"
    for (fid,) in _raw(conn).execute(
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
        for r in _raw(conn).execute(
            f"SELECT id, path, name, kind, is_dir, mtime FROM files WHERE id IN ({marks})", chunk
        ):
            out[int(r[0])] = SearchResult(
                int(r[0]), str(r[1]), str(r[2]), str(r[3]), bool(r[4]), float(r[5]), None, 0.0
            )
        for fid, ts in _raw(conn).execute(
            f"SELECT file_id, max(ts) FROM access_log WHERE file_id IN ({marks})"
            " AND source != 'edit' GROUP BY file_id",
            chunk,
        ):
            if int(fid) in out:
                out[int(fid)].last_access = float(ts)
    return out


def _collapse_versions(
    conn: sqlite3.Connection,
    ranked: list[SearchResult],
    rows: dict[int, SearchResult],
    limit: int,
) -> list[SearchResult]:
    """Show one entry per version group: its latest version, carrying the best score."""
    from zachary.indexer.versions import versions_of

    head = ranked[: limit * 3]
    groups = versions_of(conn, [r.file_id for r in head])
    if not groups:
        return ranked[:limit]
    need = [m[0] for m in groups.values() if m[0] not in rows]
    extra = _load(conn, need) if need else {}
    out: list[SearchResult] = []
    seen: set[int] = set()
    for r in ranked:
        members = groups.get(r.file_id)
        if members:
            latest_id = members[0]
            if latest_id in seen:
                continue
            seen.add(latest_id)
            latest = rows.get(latest_id) or extra.get(latest_id)
            if latest is None:
                latest = r
            latest.score = max(latest.score, r.score)
            latest.versions = [m for m in members if m != latest.file_id]
            r = latest
        elif r.file_id in seen:
            continue
        else:
            seen.add(r.file_id)
        out.append(r)
        if len(out) == limit:
            break
    return out


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
    now: datetime | None = None,
    include_dirs: bool = True,
    rules: PathRules | None = None,
    semantic: SemanticSearch | None = None,
) -> SearchResponse:
    """Search the index.

    ``rules`` hides rows newly excluded but not yet purged. ``semantic`` (an
    embedder + vector index) adds matches by meaning; without it search is
    keyword + recency only.
    """
    t0 = time.perf_counter()
    now_dt = now or datetime.now().astimezone()
    now_ts = now_dt.timestamp()
    tp = parse_time(query, now_dt)
    tr = tp.range
    terms, kinds = split_kind_hints(query_terms(tp.rest), words(tp.rest), keep_if_alone=tr is None)

    in_range: dict[int, str] = _ids_in_range(conn, tr, RANGE_LIMIT) if tr else {}
    allowed = set(in_range)
    bm25: dict[int, float] = {}
    range_matched = True
    restricted = False

    def lookup(ts: list[str]) -> dict[int, int]:
        nonlocal range_matched, restricted
        if tr is not None and allowed:
            hit = _keyword_ids(conn, ts, RANGE_KW_ENOUGH, allowed=allowed)
            if hit:
                range_matched, restricted = True, True
                return hit
        hit = _keyword_ids(conn, ts, FTS_LIMIT, scores=bm25)
        restricted = False
        range_matched = tr is None or not hit
        return hit

    kw = lookup(terms) if terms else {}
    corrected: dict[str, str] = {}
    if terms and not kw:
        corrected = correct_terms(conn, terms)
        if corrected:
            terms = [corrected.get(t, t) for t in terms]
            kw = lookup(terms)
    # Meaning: nearest file vectors to the query words (inside the time range, if any).
    vec: dict[int, float] = {}
    qvec: FloatArray | None = None
    if semantic is not None and terms:
        qvec = semantic.embedder.encode([" ".join(terms)])[0]
        if tr is not None and allowed:
            recent_ids = set(list(in_range)[:RANGE_VEC_LIMIT])  # in_range is newest first
            hits = semantic.index.score_ids(qvec, recent_ids, min_sim=VEC_MIN_SIM)
        else:
            hits = semantic.index.search(qvec, VEC_K, min_sim=VEC_MIN_SIM)
        if hits:
            floor = max(VEC_MIN_SIM, VEC_REL_SIM * hits[0][1])
            hits = [h for h in hits if h[1] >= floor]
        vec = dict(hits[:VEC_K])
        if tr is not None and vec and not restricted:
            # Nothing in range by keyword, but something in range by meaning.
            kw = {k: v for k, v in kw.items() if k in allowed}
            restricted, range_matched = True, True
    only_kind = bool(kinds) and all(t in KIND_HINTS for t in terms)
    if terms and only_kind and not kw and tr is None:
        # "photos" alone and no file is *named* photos: the most recent photos.
        terms, vec = [], {}
        candidates = _recent_of_kinds(conn, kinds, KIND_ONLY_LIMIT)
    elif terms:
        candidates = list(kw) + [i for i in vec if i not in kw]
    elif kinds and in_range:  # "photos last month": time + kind only
        kind_of = _kinds_of(conn, list(in_range))
        candidates = [i for i in in_range if kind_of.get(i) in kinds] or list(in_range)
    elif kinds and tr is None:  # just "folders" / "photos": the most recent of that kind
        candidates = _recent_of_kinds(conn, kinds, KIND_ONLY_LIMIT)
    else:
        candidates = list(in_range)

    rows = _load(conn, candidates)
    if rules is not None:
        rows = {k: v for k, v in rows.items() if not rules.is_excluded(v.path, v.is_dir)}
    if not include_dirs:
        rows = {k: v for k, v in rows.items() if not v.is_dir}

    if restricted:
        # Unranked matches inside the range: tier, then how many terms hit the *name*
        # (not just a folder), then most recent first.
        order = sorted(
            (i for i in rows if i in kw),
            key=lambda i: (
                kw[i],
                -name_hits(rows[i].name, terms),
                -_kind_score(rows[i], kinds),
                -rows[i].last_seen,
            ),
        )
    else:
        order = [i for i in candidates if i in rows and i in kw]
    if terms and not restricted:
        # Dense rank by (tier, bm25): files that match equally well share a rank, so
        # recency decides between them instead of row-id order (which depends on the
        # order the OS listed the folder).
        keys = sorted({(kw[i], round(bm25.get(i, 0.0), 4)) for i in order})
        dense_kw = {k: n for n, k in enumerate(keys)}
        kw_rank = {i: dense_kw[(kw[i], round(bm25.get(i, 0.0), 4))] for i in order}
    else:
        kw_rank = {fid: i for i, fid in enumerate(order)} if terms else {}
    # Dense rank by recency (to the minute): equal times share a rank, so ties don't
    # get an arbitrary order that could outweigh keyword quality.
    minutes = sorted({int(r.last_seen // 60) for r in rows.values()}, reverse=True)
    dense = {m: i for i, m in enumerate(minutes)}
    rec_rank = {r.file_id: dense[int(r.last_seen // 60)] for r in rows.values()}
    vec_rank = {fid: n for n, fid in enumerate(sorted(vec, key=lambda f: -vec[f]))}
    phrase = " ".join(terms)
    exact: set[int] = set()
    for r in rows.values():
        s = RECENCY_WEIGHT / (RRF_K + rec_rank[r.file_id])
        if r.file_id in kw_rank:
            s += KW_WEIGHT / (RRF_K + kw_rank[r.file_id]) + TIER_BONUS[kw[r.file_id]]
        if phrase:
            stem = r.name if r.is_dir else r.name.rsplit(".", 1)[0]
            if " ".join(words(stem)) == phrase:
                s += EXACT_NAME_BONUS
                exact.add(r.file_id)
        if range_matched and r.file_id in in_range:
            # A file the user opened in the range is a stronger signal than an mtime.
            s += IN_RANGE_BONUS if in_range[r.file_id] == "opened" else IN_RANGE_BONUS / 2
        if r.file_id in vec_rank:
            strength = min(1.0, vec[r.file_id] / 0.4)  # scale by how close the meaning is
            s += VEC_WEIGHT * strength / (RRF_K + vec_rank[r.file_id])
        s += _kind_score(r, kinds)
        r.score = s

    ranked = sorted(rows.values(), key=lambda r: (-r.score, -r.last_seen))
    results = _collapse_versions(conn, ranked, rows, limit)
    # Explanations only for what is shown.
    for r in results:
        r.strong = (
            not terms  # "what I opened yesterday": time/kind is the whole question
            or r.file_id in kw
            or r.file_id in exact
            or vec.get(r.file_id, 0.0) >= STRONG_SIM
        )
        if r.file_id in exact:
            r.why.append("exact name")
        in_r = range_matched and r.file_id in in_range
        if in_r:
            assert tr is not None
            r.why.append(f"{in_range[r.file_id]} {tr.label}")
        r.why.extend(why_terms(r, terms))
        if r.file_id in vec and qvec is not None:
            p = best_passage(conn, r.file_id, qvec)
            if p is not None and p[1] >= VEC_MIN_SIM:
                r.why.append(f"about: “{p[0]}”")
            elif not r.why or r.why == ["exact name"]:
                r.why.append("similar meaning")
        if r.versions:
            r.why.append(f"latest of {len(r.versions) + 1} versions")
        if not in_r:
            if r.last_access and r.last_access >= r.mtime:
                r.why.append(f"opened {ago(r.last_access, now_ts)}")
            else:
                r.why.append(f"modified {ago(r.mtime, now_ts)}")
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

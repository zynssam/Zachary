"""Topics: HDBSCAN over file vectors, auto-labelled, stored as *virtual* collections.

Nothing on disk changes: a topic is a row in ``topics`` plus member rows in
``topic_members`` ("Data Structures" can span many folders and drives).

Pipeline (in the indexer's idle maintenance, at most once a day):
1. file vectors of documents and code (the newest MAX_CLUSTER_FILES of them);
2. L2-normalise, PCA to PCA_DIM dimensions (density clustering works poorly in
   256-d), HDBSCAN;
3. files left out of the sample join the nearest topic if similar enough;
4. a label per topic: a shared folder name when most members live under it
   ("DSA", "Machine Learning"), otherwise the topic's most distinctive keywords
   (class-based TF-IDF over names, titles and content keywords);
5. topic ids stay stable across rebuilds (matched by member overlap), so "the
   DSA topic" doesn't change identity every night.
"""

from __future__ import annotations

import itertools
import logging
import math
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePath

import numpy as np

from zachary.core.db import EMBED_DIM, get_state, set_state
from zachary.search.hybrid import STOPWORDS
from zachary.search.tokens import words

log = logging.getLogger(__name__)

CLUSTER_KINDS = ("document",)  # subjects come from notes/docs; code is covered by projects
JOIN_KINDS = ("document", "code")  # ...but code joins a topic it clearly belongs to
# HDBSCAN on the newest MAX_CLUSTER_FILES documents; the rest joins the nearest topic.
# Measured on 200k files: 15k docs in 16-d = 9 s (40k in 32-d took 2 min).
MAX_CLUSTER_FILES = 15_000
MIN_FILES = 30  # don't bother below this
PCA_DIM = 16  # big collections (speed); small ones use PCA_DIM_SMALL (precision)
PCA_DIM_SMALL = 32
SMALL_N = 5_000
MIN_CLUSTER = 8
MIN_SAMPLES = 4
ASSIGN_SIM = 0.6  # cosine to a topic centroid for documents outside the sample / noise
LABEL_SAMPLE = 300  # members per topic used to name it
MEMBER_SIM = 0.35  # in full dimensions, a member must be at least this close to its topic
CODE_JOIN_SIM = 0.75  # code must be much closer to join a topic of notes
CODE_JOIN_MAX = 2.0  # at most this many code files per document in a topic
REBUILD_AGE_S = 24 * 3600
STABLE_JACCARD = 0.3  # a new cluster keeps an old topic's id above this member overlap
MAX_TOPIC_SHARE = 0.25  # a "topic" holding a quarter of everything isn't a topic

# Folder names that say nothing about a topic.
GENERIC_FOLDERS = frozenset(
    """
    home users user documents document desktop downloads download src source lib libs
    app apps code projects project work files file data misc stuff new folder old temp tmp
    test tests docs doc assets static public dist build out bin include scripts utils util
    main backend frontend client server web api core common shared notes pdfs pdf
    onedrive google drive dropbox mnt media run volumes c d e
    """.split()
)
_NOISE_TOKEN = re.compile(r"^(\d+|[a-f0-9]{6,}|.)$")
# File-extension and programming words: never part of a topic's name.
LABEL_STOP = frozenset(
    """
    pdf txt json md markdown docx doc xlsx xls csv pptx ppt py js ts tsx jsx html css scss
    yml yaml toml ini cfg log xml ipynb png jpg jpeg svg zip sh bat lock
    import from def class return const let var function self none true false null async
    await export default require module package app main index init service services util
    utils config test tests http https www com org localhost src lib new copy final
    """.split()
)


@dataclass(slots=True)
class TopicStats:
    files: int = 0  # vectors considered
    topics: int = 0
    members: int = 0
    noise: int = 0
    seconds: float = 0.0


def due(conn: sqlite3.Connection, now: float | None = None) -> bool:
    built = get_state(conn, "topics.built_at")
    now = time.time() if now is None else now
    return built is None or now - float(built) > REBUILD_AGE_S


def _load(
    conn: sqlite3.Connection, kinds: tuple[str, ...], exclude: set[int] | None = None
) -> tuple[np.ndarray, np.ndarray]:
    marks = ",".join("?" * len(kinds))
    # Sort just the ids (sorting rows *with* their vectors peaked at ~580 MB on 200k).
    order = [
        r[0]
        for r in conn.execute(
            f"""SELECT f.id FROM files f JOIN vec_ivf v ON v.file_id = f.id
                WHERE f.kind IN ({marks}) ORDER BY f.mtime DESC""",
            kinds,
        )
        if not exclude or r[0] not in exclude
    ]
    if not order:
        return np.zeros(0, dtype=np.int64), np.zeros((0, EMBED_DIM), dtype=np.int8)
    ids = np.array(order, dtype=np.int64)
    pos = {fid: i for i, fid in enumerate(order)}
    # Kept as int8 (1 byte per number; float32 would be 150 MB on 200k files) and
    # turned into unit float vectors in batches by ``_unit``.
    x = np.zeros((len(order), EMBED_DIM), dtype=np.int8)
    for i in range(0, len(order), 900):
        chunk = order[i : i + 900]
        q = ",".join("?" * len(chunk))
        for fid, blob in conn.execute(
            f"SELECT file_id, embedding FROM vec_ivf WHERE file_id IN ({q})", chunk
        ):
            x[pos[fid]] = np.frombuffer(blob, dtype=np.int8)
    return ids, x


def _unit(q: np.ndarray) -> np.ndarray:
    """int8 rows -> unit-length float32 rows."""
    x = q.astype(np.float32)
    x /= np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-6)
    return x


def _pca(x: np.ndarray, dim: int) -> np.ndarray:
    mean = x.mean(axis=0, keepdims=True)
    xc = x - mean
    # Principal axes from a sample (fast and plenty for 256-d).
    sample = xc[: min(len(xc), 20_000)]
    _u, _s, vt = np.linalg.svd(sample, full_matrices=False)
    out: np.ndarray = xc @ vt[: min(dim, vt.shape[0])].T
    return out


def cluster(x: np.ndarray) -> np.ndarray:
    """HDBSCAN labels (-1 = noise) for unit vectors ``x``."""
    from sklearn.cluster import HDBSCAN  # only the indexer needs scikit-learn

    z = _pca(x, PCA_DIM_SMALL if len(x) <= SMALL_N else PCA_DIM)
    z /= np.maximum(np.linalg.norm(z, axis=1, keepdims=True), 1e-6)
    labels: np.ndarray = HDBSCAN(
        min_cluster_size=MIN_CLUSTER,
        min_samples=MIN_SAMPLES,
        cluster_selection_method="leaf",  # many focused topics, not a few huge ones
        copy=False,
    ).fit_predict(z)
    return labels


# -- labels -------------------------------------------------------------------------------
def _folder_label(paths: list[str], roots: list[str]) -> str | None:
    """The most specific folder name most members share (anywhere above them)."""
    counts: Counter[str] = Counter()
    shown: dict[str, str] = {}
    depth: dict[str, int] = {}
    for p in paths:
        parts = PurePath(p).parts[:-1]
        seen: set[str] = set()
        for d, part in enumerate(parts):
            key = " ".join(words(part))
            if (
                not key
                or key in GENERIC_FOLDERS
                or key in seen
                or part.startswith(".")
                or len(key) < 2
                or any(r.endswith("/" + part) or r == part for r in roots)
            ):
                continue
            seen.add(key)
            counts[key] += 1
            shown.setdefault(key, part)
            depth[key] = max(depth.get(key, 0), d)
    shared = [k for k, n in counts.items() if n / len(paths) >= 0.6]
    if not shared:
        return None
    key = max(shared, key=lambda k: (depth[k], counts[k]))  # deepest = most specific
    name = shown[key].replace("_", " ").replace("-", " ").strip()
    return name if any(c.isupper() for c in name) else name.title()


def _bigrams(toks: list[str]) -> list[str]:
    return [f"{a} {b}" for a, b in itertools.pairwise(toks) if a != b]


def _tokens(conn: sqlite3.Connection, file_ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for i in range(0, len(file_ids), 900):
        chunk = file_ids[i : i + 900]
        marks = ",".join("?" * len(chunk))
        for fid, name, title, kw in conn.execute(
            f"SELECT rowid, name, title, keywords FROM files_fts WHERE rowid IN ({marks})", chunk
        ):
            toks = [
                t
                for t in words(f"{name or ''} {title or ''} {kw or ''}")
                if t not in STOPWORDS
                and t not in LABEL_STOP
                and not _NOISE_TOKEN.match(t)
                and len(t) > 2
            ]
            out[fid] = toks
    return out


def _keyword_labels(groups: dict[int, list[int]], toks: dict[int, list[str]]) -> dict[int, str]:
    """Class-based TF-IDF: words frequent in this topic and rare in the others."""
    df: Counter[str] = Counter()
    tf: dict[int, Counter[str]] = {}
    for label, members in groups.items():
        c: Counter[str] = Counter()
        for fid in members:
            t = toks.get(fid, [])
            c.update(set(t) | set(_bigrams(t)))  # per file: one long file doesn't dominate
        tf[label] = c
        df.update(c.keys())
    n = max(1, len(groups))
    out: dict[int, str] = {}
    for label, c in tf.items():
        size = max(1, len(groups[label]))
        scored = sorted(
            (
                (cnt / size) * math.log(1 + n / df[w]) * (1.5 if " " in w else 1.0),
                w,
            )
            for w, cnt in c.items()
            if cnt >= 2
        )
        top: list[str] = []
        for _s, w in reversed(scored):
            if any(w in t or t in w for t in top):
                continue  # "machine learning" covers "machine" and "learning"
            top.append(w)
            if len(top) == 2:
                break
        out[label] = ", ".join(t.title() for t in top) if top else "Misc"
    return out


# -- build ------------------------------------------------------------------------------------
def build(conn: sqlite3.Connection, roots: list[str] | None = None) -> TopicStats:
    t0 = time.perf_counter()
    st = TopicStats()
    ids, x = _load(conn, CLUSTER_KINDS)
    if len(ids) < MIN_FILES:
        set_state(conn, "topics.built_at", time.time())
        st.files = len(ids)
        return st
    n = min(len(ids), MAX_CLUSTER_FILES)
    # Code (and anything beyond the sample) only *joins* topics: append it after.
    code_ids, code_x = _load(conn, tuple(k for k in JOIN_KINDS if k not in CLUSTER_KINDS))
    if len(code_ids):
        ids = np.concatenate([ids[:n], code_ids, ids[n:]])
        x = np.concatenate([x[:n], code_x, x[n:]])
    st.files = len(ids)
    xs = _unit(x[:n])  # the clustered sample, as floats
    labels = cluster(xs)
    clusters = sorted(set(labels.tolist()) - {-1})
    groups: dict[int, list[int]] = {}
    cents: dict[int, np.ndarray] = {}
    for c in clusters:
        idx = np.flatnonzero(labels == c)
        if n >= 500 and len(idx) > MAX_TOPIC_SHARE * n:
            continue  # a catch-all blob, not a topic (only meaningful in big collections)
        v = xs[idx].mean(axis=0)
        v /= max(float(np.linalg.norm(v)), 1e-6)
        # PCA can make unrelated files look close: keep members that really are.
        idx = idx[(xs[idx] @ v) >= MEMBER_SIM]
        if len(idx) < MIN_CLUSTER:
            continue
        groups[c] = ids[idx].tolist()
        v = xs[idx].mean(axis=0)
        cents[c] = v / max(float(np.linalg.norm(v)), 1e-6)
    # Noise and files beyond the sample join the closest topic if they clearly belong.
    if cents:
        keys = list(cents)
        cmat = np.stack([cents[k] for k in keys])
        is_code = np.zeros(len(ids), dtype=bool)
        is_code[n : n + len(code_ids)] = True
        docs_in = {k: len(groups[k]) for k in keys}
        joined_code: Counter[int] = Counter()
        rest = np.concatenate([np.flatnonzero(labels == -1), np.arange(n, len(ids))])
        best = np.zeros(len(rest), dtype=np.int64)
        best_sim = np.zeros(len(rest), dtype=np.float32)
        for b in range(0, len(rest), 20_000):  # batches: bounded memory
            sims = _unit(x[rest[b : b + 20_000]]) @ cmat.T
            best[b : b + 20_000] = sims.argmax(axis=1)
            best_sim[b : b + 20_000] = sims.max(axis=1)
        if len(rest):
            for j in np.argsort(-best_sim).tolist():  # closest first
                i = int(rest[j])
                k = keys[int(best[j])]
                sim = float(best_sim[j])
                if is_code[i]:
                    if sim < CODE_JOIN_SIM or joined_code[k] >= CODE_JOIN_MAX * docs_in[k]:
                        continue
                    joined_code[k] += 1
                elif sim < ASSIGN_SIM:
                    continue
                groups[k].append(int(ids[i]))
    st.noise = len(ids) - sum(len(g) for g in groups.values())

    del xs
    # Names come from a sample of each topic (newest notes first): plenty for words
    # and folders, and it keeps memory flat on big collections.
    sample = {c: m[:LABEL_SAMPLE] for c, m in groups.items()}
    sample_ids = [f for m in sample.values() for f in m]
    paths: dict[int, str] = {}
    for i in range(0, len(sample_ids), 900):
        chunk = sample_ids[i : i + 900]
        q = ",".join("?" * len(chunk))
        paths.update(conn.execute(f"SELECT id, path FROM files WHERE id IN ({q})", chunk))
    toks = _tokens(conn, sample_ids)
    kw_labels = _keyword_labels(sample, toks)
    root_list = [str(r) for r in (roots or [])]
    final: list[tuple[str, list[int], np.ndarray]] = []
    chosen: dict[int, str] = {}
    for c in groups:
        label = _folder_label([paths[f] for f in sample[c] if f in paths], root_list)
        chosen[c] = label or kw_labels[c]
    dupes = Counter(chosen.values())
    for c, members in groups.items():
        label = chosen[c]
        if dupes[label] > 1 and kw_labels[c] != label:
            label = f"{label}: {kw_labels[c].split(', ')[0]}"  # "Hackathon: Docker"
        final.append((label, members, cents[c]))
    st.topics = len(final)
    st.members = sum(len(m) for _l, m, _c in final)
    _write(conn, final)
    set_state(conn, "topics.built_at", time.time())
    st.seconds = time.perf_counter() - t0
    log.info("topics: %d topics, %d files (%d unassigned), %.1fs",
             st.topics, st.members, st.noise, st.seconds)  # fmt: skip
    return st


def _write(conn: sqlite3.Connection, topics: list[tuple[str, list[int], np.ndarray]]) -> None:
    """Replace topics, keeping the id of an old topic a new one mostly overlaps."""
    old: dict[int, set[int]] = {}
    for tid, fid in conn.execute("SELECT topic_id, file_id FROM topic_members"):
        old.setdefault(tid, set()).add(fid)
    now = time.time()
    used: set[int] = set()
    mtimes: dict[int, float] = {}
    all_ids = [f for _l, m, _c in topics for f in m]
    for i in range(0, len(all_ids), 900):
        chunk = all_ids[i : i + 900]
        q = ",".join("?" * len(chunk))
        mtimes.update(conn.execute(f"SELECT id, mtime FROM files WHERE id IN ({q})", chunk))
    with conn:
        conn.execute("DELETE FROM topic_members")
        keep: list[int] = []
        for label, members, _cent in sorted(topics, key=lambda t: -len(t[1])):
            mset = set(members)
            best, best_j = None, 0.0
            for tid, oset in old.items():
                if tid in used:
                    continue
                j = len(mset & oset) / max(1, len(mset | oset))
                if j > best_j:
                    best, best_j = tid, j
            last = mtimes.get(max(members, key=lambda f: mtimes.get(f, 0.0)), 0.0)
            if best is not None and best_j >= STABLE_JACCARD:
                tid = best
                conn.execute(
                    "UPDATE topics SET label = ?, updated_at = ?, size = ?, last_ts = ? "
                    "WHERE id = ?",
                    (label, now, len(members), last, tid),
                )
            else:
                tid = int(
                    conn.execute(
                        "INSERT INTO topics(label, updated_at, size, last_ts) VALUES (?, ?, ?, ?)",
                        (label, now, len(members), last),
                    ).lastrowid
                    or 0
                )
            used.add(tid)
            keep.append(tid)
            conn.executemany(
                "INSERT OR IGNORE INTO topic_members(topic_id, file_id, weight) VALUES (?, ?, 1.0)",
                [(tid, f) for f in members],
            )
        marks = ",".join("?" * len(keep)) or "NULL"
        conn.execute(f"DELETE FROM topics WHERE id NOT IN ({marks})", keep)


# -- reading (assistant side) --------------------------------------------------------------
@dataclass(slots=True)
class Topic:
    id: int
    label: str
    size: int
    last_ts: float


def list_topics(conn: sqlite3.Connection, limit: int = 50) -> list[Topic]:
    rows = conn.execute(
        "SELECT id, label, size, last_ts FROM topics WHERE size > 0 ORDER BY last_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [Topic(r[0], r[1], r[2], r[3] or 0.0) for r in rows]


def topic_files(conn: sqlite3.Connection, topic_id: int, limit: int = 200) -> list[int]:
    return [
        r[0]
        for r in conn.execute(
            """SELECT m.file_id FROM topic_members m JOIN files f ON f.id = m.file_id
               WHERE m.topic_id = ? ORDER BY f.mtime DESC LIMIT ?""",
            (topic_id, limit),
        )
    ]


def topics_of(conn: sqlite3.Connection, file_ids: list[int]) -> dict[int, str]:
    """file id -> topic label (for "why matched")."""
    out: dict[int, str] = {}
    for i in range(0, len(file_ids), 900):
        chunk = file_ids[i : i + 900]
        marks = ",".join("?" * len(chunk))
        for fid, label in conn.execute(
            f"""SELECT m.file_id, t.label FROM topic_members m JOIN topics t ON t.id = m.topic_id
                WHERE m.file_id IN ({marks})""",
            chunk,
        ):
            out[fid] = label
    return out

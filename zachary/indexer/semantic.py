"""Content indexing: extract -> chunk -> embed -> store. Runs in the indexer,
in the background, at low priority.

Per file:
- **New or changed content** (mtime moved): a quick hash (size + first/last
  64 KB) decides. Same hash as last time (re-saved, touched) -> reuse the stored
  chunk vectors, don't extract or embed again.
- **Renamed / moved only** (``embed_path`` != path): reuse chunk vectors, only the
  name/path part of the file vector is refreshed.
- **Media, archives, binaries, ``name_only`` folders**: name + path only.

File vector = normalize((1 - NAME_SHARE) * mean(chunk vectors) + NAME_SHARE *
vector("name words + folder words + title")). Code chunks are embedded as their
words (identifiers split, punctuation dropped); the original text is stored.
Chunks and their vectors are kept for passages ("why it matched", Phase 6 Q&A).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from zachary.core.config import Config
from zachary.core.db import get_state
from zachary.indexer.chunker import chunk_text
from zachary.indexer.embedder import Embedder, FloatArray, dequantize_many, normalize, quantize
from zachary.indexer.extract import extract, extractable
from zachary.search import vectors
from zachary.search.tokens import name_text, path_text, words

log = logging.getLogger(__name__)

NAME_SHARE = 0.25  # names are often uninformative ("ll.cpp"); keyword search covers them
QUICK_BLOCK = 64 * 1024
KEYWORDS = 40
_KEYWORD_STOP = frozenset(
    """
    the and for are but not you all any can had her was one our out day get has him his how
    man new now old see two way who boy did its let put say she too use that with have this
    will your from they know want been good much some time very when come here just like long
    make many more only over such take than them well were what which their there would about
    into these those other could should also each then where after before while because being
    page http https www com org
    """.split()
)


def quick_hash(path: str, size: int) -> str:
    """Change detector: size + first and last 64 KB (not a duplicate check)."""
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(QUICK_BLOCK))
        if size > 2 * QUICK_BLOCK:
            f.seek(-QUICK_BLOCK, os.SEEK_END)
            h.update(f.read(QUICK_BLOCK))
    return h.hexdigest()


def keywords(text: str, n: int = KEYWORDS) -> str:
    """Most frequent content words, for keyword search over file contents."""
    counts = Counter(w for w in re.findall(r"[^\W\d_]{3,}", text.lower()) if w not in _KEYWORD_STOP)
    return " ".join(w for w, _ in counts.most_common(n))


@dataclass(slots=True)
class EmbedStats:
    files: int = 0
    extracted: int = 0  # files whose text was (re)extracted and embedded
    reused: int = 0  # content unchanged: stored chunk vectors reused
    name_only: int = 0
    chunks: int = 0
    bytes: int = 0  # extracted text bytes
    seconds: float = 0.0
    per_kind: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class _Job:
    id: int
    path: str
    name: str
    ext: str
    kind: str
    size: int
    mtime: float
    content_hash: str | None
    embedded_at: float | None
    embed_path: str | None
    root: str | None
    new_hash: str | None = None
    title: str | None = None
    keywords: str = ""
    chunks: list[tuple[int, int, int, str]] = field(default_factory=list)  # ord, start, end, text
    reuse: bool = False
    chunk_vecs: FloatArray | None = None


class SemanticIndexer:
    """Owns its own DB connection; safe to run on a background thread."""

    def __init__(self, conn: sqlite3.Connection, config: Config, embedder: Embedder) -> None:
        self.conn = conn
        self.config = config
        self.embedder = embedder
        self._ivf_version = -1
        self._centroids: FloatArray | None = None

    # -- queue -----------------------------------------------------------------------
    def pending_count(self) -> int:
        return int(
            self.conn.execute(
                "SELECT count(*) FROM files WHERE is_dir = 0 AND (embedded_at IS NULL"
                " OR embedded_at < mtime OR embed_path IS NOT path)"
            ).fetchone()[0]
        )

    def pending(self, limit: int) -> list[sqlite3.Row]:
        """Next files to (re)embed: documents and code first, then newest first."""
        return self.conn.execute(
            "SELECT id, path, name, ext, kind, size, mtime, content_hash, embedded_at, embed_path"
            " FROM files WHERE is_dir = 0 AND (embedded_at IS NULL OR embedded_at < mtime"
            " OR embed_path IS NOT path)"
            " ORDER BY (kind IN ('document', 'code')) DESC, mtime DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # -- work ------------------------------------------------------------------------
    def run(self, limit: int = 64) -> EmbedStats:
        """Embed up to ``limit`` pending files. Returns what was done."""
        return self.process(self.pending(limit))

    def process(self, rows: list[sqlite3.Row]) -> EmbedStats:
        """Embed the given pending rows (from :meth:`pending`) in one batch."""
        t0 = time.perf_counter()
        stats = EmbedStats()
        if not rows:
            return stats
        rules = self.config.path_rules()
        max_bytes = int(self.config.get_setting("embed_max_kb")) * 1024
        jobs = [
            _Job(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], rules.root_for(r[1]))
            for r in rows
        ]
        for job in jobs:
            self._prepare(job, max_bytes, stats)
        self._embed_and_store(jobs, stats)
        stats.files = len(jobs)
        stats.seconds = time.perf_counter() - t0
        return stats

    def _name_only(self, job: _Job) -> bool:
        if not extractable(job.ext, job.kind):
            return True
        return "name_only" in self.config.folder_rules_for(job.path)

    def _prepare(self, job: _Job, max_bytes: int, stats: EmbedStats) -> None:
        """Decide reuse vs extract, and extract text (no DB writes)."""
        stats.per_kind[job.kind] += 1
        if self._name_only(job):
            stats.name_only += 1
            return
        content_moved = job.embedded_at is None or job.embedded_at < job.mtime
        if not content_moved:
            job.reuse = True  # only the path changed
            return
        try:
            job.new_hash = quick_hash(job.path, job.size)
        except OSError:
            return  # vanished or unreadable: name-only for now; the watcher will catch up
        if job.new_hash == job.content_hash and self._has_chunks(job.id):
            job.reuse = True
            return
        ex = extract(Path(job.path), job.ext, job.kind, job.size, max_bytes)
        if ex is None or not ex.text.strip():
            return
        job.title = None if job.kind == "code" else ((ex.title or "")[:300] or None)
        job.keywords = keywords(ex.text)
        job.chunks = [(c.ord, c.start, c.end, c.text) for c in chunk_text(ex.text)]
        stats.extracted += 1
        stats.bytes += len(ex.text)

    def _has_chunks(self, file_id: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM chunks WHERE file_id = ? LIMIT 1", (file_id,)
            ).fetchone()
            is not None
        )

    def _stored_chunk_vectors(self, file_id: int) -> FloatArray | None:
        rows = self.conn.execute(
            "SELECT v.embedding FROM chunks c JOIN vec_chunks v ON v.chunk_id = c.id"
            " WHERE c.file_id = ? ORDER BY c.ord",
            (file_id,),
        ).fetchall()
        return dequantize_many([r[0] for r in rows]) if rows else None

    def _name_text(self, job: _Job, title: str | None) -> str:
        parts = [name_text(job.name), path_text(job.path, job.root)]
        if title:
            parts.append(" ".join(words(title)))
        return " ".join(p for p in parts if p)

    def _centroids_now(self) -> FloatArray | None:
        v = int(get_state(self.conn, "ivf.version", 0))
        if v != self._ivf_version:
            self._centroids = vectors.load_centroids(self.conn)
            self._ivf_version = v
        return self._centroids

    def _embed_and_store(self, jobs: list[_Job], stats: EmbedStats) -> None:
        # Titles of reused files come from the DB.
        reuse_titles = {}
        for job in jobs:
            if job.reuse:
                row = self.conn.execute(
                    "SELECT title FROM files WHERE id = ?", (job.id,)
                ).fetchone()
                reuse_titles[job.id] = row[0] if row else None
                job.chunk_vecs = self._stored_chunk_vectors(job.id)
                stats.reused += 1
        # One model call for every text in the batch.
        texts: list[str] = []
        for job in jobs:
            texts.append(self._name_text(job, job.title or reuse_titles.get(job.id)))
            if job.kind == "code":
                # Embed identifiers and comments, not braces and operators (measured: syntax
                # noise lowers similarity to the concepts the code is about).
                texts.extend(" ".join(words(c[3])) for c in job.chunks)
            else:
                texts.extend(c[3] for c in job.chunks)
        vecs = self.embedder.encode(texts)
        centroids = self._centroids_now()
        now = time.time()
        i = 0
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for job in jobs:
                name_vec = vecs[i]
                i += 1
                chunk_vecs = job.chunk_vecs
                if job.chunks:
                    chunk_vecs = vecs[i : i + len(job.chunks)]
                    i += len(job.chunks)
                alive = self.conn.execute(
                    "SELECT 1 FROM files WHERE id = ? AND path = ?", (job.id, job.path)
                ).fetchone()
                if alive is None:
                    continue  # deleted or moved while we worked; it will come back
                if job.chunks:
                    assert chunk_vecs is not None
                    self.conn.execute("DELETE FROM chunks WHERE file_id = ?", (job.id,))
                    for (ord_, start, end, text), v in zip(job.chunks, chunk_vecs, strict=True):
                        cid = self.conn.execute(
                            "INSERT INTO chunks(file_id, ord, text_excerpt, char_start, char_end)"
                            " VALUES (?, ?, ?, ?, ?) RETURNING id",
                            (job.id, ord_, text, start, end),
                        ).fetchone()[0]
                        self.conn.execute(
                            "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, vec_int8(?))",
                            (cid, quantize(v)),
                        )
                    stats.chunks += len(job.chunks)
                elif job.new_hash is not None and not job.reuse:
                    # Content changed but has no text now (e.g. emptied): drop old chunks.
                    self.conn.execute("DELETE FROM chunks WHERE file_id = ?", (job.id,))
                    chunk_vecs = None
                if chunk_vecs is not None and len(chunk_vecs):
                    content = normalize(chunk_vecs.mean(axis=0, keepdims=True))[0]
                    fv = normalize(((1 - NAME_SHARE) * content + NAME_SHARE * name_vec)[None, :])[0]
                else:
                    fv = name_vec
                blob = quantize(fv)
                self.conn.execute("DELETE FROM vec_files WHERE file_id = ?", (job.id,))
                self.conn.execute(
                    "INSERT INTO vec_files(file_id, embedding) VALUES (?, vec_int8(?))",
                    (job.id, blob),
                )
                vectors.put(self.conn, job.id, blob, centroids)
                if job.reuse:
                    self.conn.execute(
                        "UPDATE files SET embedded_at = ?, embed_path = ? WHERE id = ?",
                        (now, job.path, job.id),
                    )
                else:
                    self.conn.execute(
                        "UPDATE files SET embedded_at = ?, embed_path = ?, title = ?,"
                        " content_hash = coalesce(?, content_hash) WHERE id = ?",
                        (now, job.path, job.title, job.new_hash, job.id),
                    )
                    if job.title is not None or job.keywords or job.chunks:
                        self.conn.execute(
                            "UPDATE files_fts SET title = ?, keywords = ? WHERE rowid = ?",
                            (" ".join(words(job.title or "")), job.keywords, job.id),
                        )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

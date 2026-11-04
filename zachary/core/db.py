"""SQLite connection, numbered migrations, and schema.

Rules:
- Never change the schema without adding a new migration to ``MIGRATIONS``.
- The schema version is ``PRAGMA user_version``.
- Connections run in autocommit mode (``isolation_level=None``); use
  :func:`transaction` for multi-statement writes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sqlite_vec

# Model2Vec potion-base-8M produces 256-d vectors. Changing this needs a migration.
EMBED_DIM = 256

_MIGRATION_1 = f"""
-- files: one row per file or folder Zachary knows about.
-- inode/dev are extra to the CLAUDE.md list: they let renames/moves keep identity.
CREATE TABLE files (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL UNIQUE,
    parent_id     INTEGER REFERENCES files(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    ext           TEXT    NOT NULL DEFAULT '',
    size          INTEGER NOT NULL DEFAULT 0,
    mtime         REAL    NOT NULL DEFAULT 0,
    ctime         REAL    NOT NULL DEFAULT 0,
    inode         INTEGER,
    dev           INTEGER,
    content_hash  TEXT,
    kind          TEXT    NOT NULL DEFAULT 'other',
    is_dir        INTEGER NOT NULL DEFAULT 0,
    is_excluded   INTEGER NOT NULL DEFAULT 0,
    indexed_at    REAL,
    embedded_at   REAL
);
CREATE INDEX idx_files_parent ON files(parent_id);
CREATE INDEX idx_files_mtime  ON files(mtime);
CREATE INDEX idx_files_name   ON files(name);
CREATE INDEX idx_files_hash   ON files(content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX idx_files_inode  ON files(dev, inode) WHERE inode IS NOT NULL;
CREATE INDEX idx_files_embed  ON files(embedded_at, mtime) WHERE is_dir = 0;

-- Keyword index. rowid = files.id. Maintained by the indexer.
CREATE VIRTUAL TABLE files_fts USING fts5(
    name, path_tokens, title, keywords,
    tokenize = 'unicode61 remove_diacritics 2',
    prefix = '2 3'
);

CREATE TABLE chunks (
    id            INTEGER PRIMARY KEY,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ord           INTEGER NOT NULL,
    text_excerpt  TEXT    NOT NULL,
    char_start    INTEGER NOT NULL,
    char_end      INTEGER NOT NULL,
    UNIQUE (file_id, ord)
);

-- int8-quantized vectors (sqlite-vec). Primary keys mirror chunks.id / files.id.
CREATE VIRTUAL TABLE vec_chunks USING vec0(
    chunk_id  INTEGER PRIMARY KEY,
    embedding int8[{EMBED_DIM}] distance_metric=cosine
);
CREATE VIRTUAL TABLE vec_files USING vec0(
    file_id   INTEGER PRIMARY KEY,
    embedding int8[{EMBED_DIM}] distance_metric=cosine
);

CREATE TABLE relations (
    src_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    dst_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    score   REAL    NOT NULL,
    kind    TEXT    NOT NULL
            CHECK (kind IN ('content', 'name', 'location', 'coaccess', 'feedback')),
    PRIMARY KEY (src_id, dst_id, kind)
) WITHOUT ROWID;
CREATE INDEX idx_relations_dst ON relations(dst_id);

CREATE TABLE projects (
    folder_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    markers        TEXT NOT NULL DEFAULT '[]',   -- JSON list of marker names
    last_activity  REAL NOT NULL DEFAULT 0
);
CREATE INDEX idx_projects_activity ON projects(last_activity DESC);

CREATE TABLE version_groups (
    group_id   INTEGER NOT NULL,
    file_id    INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    is_latest  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_version_groups_group ON version_groups(group_id);

-- Topics are virtual collections only: nothing on disk changes.
CREATE TABLE topics (
    id          INTEGER PRIMARY KEY,
    label       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE topic_members (
    topic_id  INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    weight    REAL    NOT NULL DEFAULT 1.0,
    PRIMARY KEY (topic_id, file_id)
) WITHOUT ROWID;
CREATE INDEX idx_topic_members_file ON topic_members(file_id);

CREATE TABLE access_log (
    id       INTEGER PRIMARY KEY,
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ts       REAL    NOT NULL,
    source   TEXT    NOT NULL CHECK (source IN ('zach', 'os_recent', 'edit')),
    UNIQUE (file_id, ts, source)
);
CREATE INDEX idx_access_log_ts ON access_log(ts);

-- start_ts/end_ts rather than start/end: END is an SQL keyword.
CREATE TABLE sessions (
    id        INTEGER PRIMARY KEY,
    start_ts  REAL NOT NULL,
    end_ts    REAL
);
CREATE INDEX idx_sessions_start ON sessions(start_ts);
CREATE TABLE session_files (
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    PRIMARY KEY (session_id, file_id)
) WITHOUT ROWID;

CREATE TABLE feedback (
    id          INTEGER PRIMARY KEY,
    query_hash  TEXT    NOT NULL,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    rank_shown  INTEGER NOT NULL,
    chosen      INTEGER NOT NULL,
    ts          REAL    NOT NULL
);
CREATE INDEX idx_feedback_query ON feedback(query_hash);

CREATE TABLE action_plans (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,
    json         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'proposed'
                 CHECK (status IN ('proposed', 'approved', 'rejected',
                                   'executing', 'executed', 'failed', 'undone')),
    created_at   REAL NOT NULL,
    approved_at  REAL
);
-- Undo history must outlive anything else: no cascade from plans.
CREATE TABLE undo_log (
    id       INTEGER PRIMARY KEY,
    plan_id  INTEGER NOT NULL REFERENCES action_plans(id) ON DELETE RESTRICT,
    op       TEXT    NOT NULL CHECK (op IN ('move', 'rename', 'trash', 'mkdir')),
    src      TEXT    NOT NULL,
    dst      TEXT,
    ts       REAL    NOT NULL
);
CREATE INDEX idx_undo_log_plan ON undo_log(plan_id);

CREATE TABLE settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL            -- JSON
) WITHOUT ROWID;

CREATE TABLE exclude_rules (
    id       INTEGER PRIMARY KEY,
    pattern  TEXT    NOT NULL,
    kind     TEXT    NOT NULL CHECK (kind IN ('dirname', 'glob', 'path')),
    source   TEXT    NOT NULL DEFAULT 'user' CHECK (source IN ('default', 'user')),
    enabled  INTEGER NOT NULL DEFAULT 1,
    UNIQUE (pattern, kind)
);

CREATE TABLE folder_rules (
    id    INTEGER PRIMARY KEY,
    path  TEXT NOT NULL,
    rule  TEXT NOT NULL CHECK (rule IN ('never_touch', 'auto_tidy', 'name_only')),
    UNIQUE (path, rule)
);

-- Derived data that lives in virtual tables has no FK; clean it with triggers.
CREATE TRIGGER trg_files_delete AFTER DELETE ON files BEGIN
    DELETE FROM files_fts WHERE rowid = old.id;
    DELETE FROM vec_files WHERE file_id = old.id;
END;
CREATE TRIGGER trg_chunks_delete AFTER DELETE ON chunks BEGIN
    DELETE FROM vec_chunks WHERE chunk_id = old.id;
END;
"""

# Migration #2 (Phase 1):
# - files_fts gets the porter stemmer so "fees" matches "Fee", and keeps combining
#   marks (M*) inside words so Devanagari/Kannada names aren't split. Rows are rebuilt by
#   the indexer (FileStore.ensure_fts) because tokens are computed in Python.
# - files_fts_vocab exposes indexed terms for typo correction.
# - state: indexer runtime state (journal cursors, config revision), not user settings.
_MIGRATION_2 = """
DROP TABLE files_fts;
CREATE VIRTUAL TABLE files_fts USING fts5(
    name, path_tokens, title, keywords,
    tokenize = "porter unicode61 remove_diacritics 2 categories 'L* N* Co M*'",
    prefix = '2 3'
);
CREATE VIRTUAL TABLE files_fts_vocab USING fts5vocab(files_fts, 'row');
CREATE TABLE state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL            -- JSON
) WITHOUT ROWID;
"""

# Migration #3 (Phase 3, semantic memory):
# - files.title: document title from extraction (also indexed in files_fts.title).
# - files.embed_path: the path the file vector was built with; a rename/move only
#   refreshes the name/path part of the vector (content is not re-embedded).
# - vec_ivf + ivf_centroids: an IVF (inverted file) index over the file vectors.
#   sqlite-vec 0.1 only does brute-force kNN (measured 169 ms on 200k vectors); the
#   IVF lets a query read only the nearest clusters. File vectors live only in
#   vec_ivf (vec_files from migration #1 is left empty: a second copy cost 55 MB per
#   200k files and was never read). Chunk vectors stay in sqlite-vec's vec_chunks.
_MIGRATION_3 = """
ALTER TABLE files ADD COLUMN title TEXT;
ALTER TABLE files ADD COLUMN embed_path TEXT;
-- Clustered storage: a cluster's vectors sit on neighbouring pages, so a query reads
-- a few contiguous ranges instead of thousands of scattered rows.
CREATE TABLE vec_ivf (
    cluster    INTEGER NOT NULL,
    file_id    INTEGER NOT NULL UNIQUE REFERENCES files(id) ON DELETE CASCADE,
    embedding  BLOB    NOT NULL,       -- int8[EMBED_DIM]
    PRIMARY KEY (cluster, file_id)
) WITHOUT ROWID;
CREATE TABLE ivf_centroids (
    id         INTEGER PRIMARY KEY,
    embedding  BLOB NOT NULL           -- float32[EMBED_DIM], L2-normalized
);
"""

# Migration #4 (Phase 5): feedback keeps the query's words, so a choice also helps
# *similar* queries ("fee receipt" after choosing for "fee receipt sem 5"), and an
# index for the per-file lookups of the boost.
# Topics also store their size and last activity, so listing them is a lookup, not
# an aggregate over every member (106 ms on 200k files).
_MIGRATION_4 = """
ALTER TABLE feedback ADD COLUMN terms TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_feedback_ts ON feedback(ts);
ALTER TABLE topics ADD COLUMN size INTEGER NOT NULL DEFAULT 0;
ALTER TABLE topics ADD COLUMN last_ts REAL NOT NULL DEFAULT 0;
CREATE INDEX idx_topics_last ON topics(last_ts DESC);
"""

# Migration 5: files/folders Zachary creates ("create a text file ...", dictation) are
# undoable too: undo_log gets a 'create' op (dst = "size:mtime" at creation, so undo
# only trashes it if it wasn't edited since). SQLite can't alter a CHECK: rebuild.
_MIGRATION_5 = """
CREATE TABLE undo_log_new (
    id       INTEGER PRIMARY KEY,
    plan_id  INTEGER NOT NULL REFERENCES action_plans(id) ON DELETE RESTRICT,
    op       TEXT    NOT NULL CHECK (op IN ('move', 'rename', 'trash', 'mkdir', 'create')),
    src      TEXT    NOT NULL,
    dst      TEXT,
    ts       REAL    NOT NULL
);
INSERT INTO undo_log_new(id, plan_id, op, src, dst, ts)
    SELECT id, plan_id, op, src, dst, ts FROM undo_log;
DROP TABLE undo_log;
ALTER TABLE undo_log_new RENAME TO undo_log;
CREATE INDEX idx_undo_log_plan ON undo_log(plan_id);
"""

MIGRATIONS: tuple[str, ...] = (
    _MIGRATION_1, _MIGRATION_2, _MIGRATION_3, _MIGRATION_4, _MIGRATION_5,
)  # fmt: skip
SCHEMA_VERSION = len(MIGRATIONS)


class SchemaTooNewError(RuntimeError):
    """The database was written by a newer Zachary."""


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a configured connection with sqlite-vec loaded. Does not migrate."""
    if readonly:
        uri = f"{Path(path).resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
    else:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    # PRAGMAs must run outside a transaction; autocommit mode guarantees that.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -32000")  # ~32 MB
    conn.execute("PRAGMA mmap_size = 268435456")  # 256 MB
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = True) -> Iterator[None]:
    """Run a block in one transaction; roll back on error."""
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in order. Returns the resulting version."""
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"database schema v{current} is newer than this build (v{SCHEMA_VERSION})"
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        with transaction(conn):
            for stmt in _split_statements(MIGRATIONS[version - 1]):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {version}")
    return schema_version(conn)


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open the database read-write and bring it to the latest schema."""
    conn = connect(path)
    migrate(conn)
    return conn


def _split_statements(script: str) -> list[str]:
    """Split a migration script into statements (``executescript`` would commit)."""
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        if not buf and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement in migration: {buf[:80]!r}")
    return statements


def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """Read an indexer state value (JSON-decoded)."""
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row[0])


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO state(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )

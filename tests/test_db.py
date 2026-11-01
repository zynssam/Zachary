from __future__ import annotations

import sqlite3
import struct
import time
from pathlib import Path

import pytest

from zachary.core import db

EXPECTED_TABLES = {
    "files",
    "files_fts",
    "chunks",
    "vec_chunks",
    "vec_files",
    "relations",
    "projects",
    "version_groups",
    "topics",
    "topic_members",
    "access_log",
    "sessions",
    "session_files",
    "feedback",
    "action_plans",
    "undo_log",
    "settings",
    "exclude_rules",
    "folder_rules",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _vec(*vals: int) -> bytes:
    padded = list(vals) + [0] * (db.EMBED_DIM - len(vals))
    return struct.pack(f"{db.EMBED_DIM}b", *padded)


def _add_file(conn: sqlite3.Connection, path: str, parent: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO files(path, parent_id, name) VALUES (?, ?, ?)",
        (path, parent, Path(path).name),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_all_tables_created(conn: sqlite3.Connection) -> None:
    assert _tables(conn) >= EXPECTED_TABLES
    assert db.schema_version(conn) == db.SCHEMA_VERSION


def test_wal_and_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_sqlite_vec_loaded(conn: sqlite3.Connection) -> None:
    assert conn.execute("SELECT vec_version()").fetchone()[0].startswith("v")


def test_migrate_is_idempotent(db_path: Path) -> None:
    c1 = db.open_db(db_path)
    c1.close()
    c2 = db.open_db(db_path)
    assert db.migrate(c2) == db.SCHEMA_VERSION
    c2.close()


def test_refuses_newer_schema(db_path: Path) -> None:
    c = db.open_db(db_path)
    c.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    with pytest.raises(db.SchemaTooNewError):
        db.migrate(c)
    c.close()


def test_failed_migration_rolls_back(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = (*db.MIGRATIONS, "CREATE TABLE ok_table(x);\nCREATE TABLE broken(;")
    monkeypatch.setattr(db, "MIGRATIONS", bad)
    monkeypatch.setattr(db, "SCHEMA_VERSION", len(bad))
    c = db.connect(db_path)
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(c)
    assert db.schema_version(c) == len(bad) - 1
    assert "ok_table" not in _tables(c)
    c.close()


def test_int8_vector_knn(conn: sqlite3.Connection) -> None:
    a = _add_file(conn, "/a")
    b = _add_file(conn, "/b")
    conn.execute(
        "INSERT INTO vec_files(file_id, embedding) VALUES (?, vec_int8(?))", (a, _vec(1, 2, 3))
    )
    conn.execute(
        "INSERT INTO vec_files(file_id, embedding) VALUES (?, vec_int8(?))", (b, _vec(-3, 2, -1))
    )
    rows = conn.execute(
        "SELECT file_id FROM vec_files WHERE embedding MATCH vec_int8(?) AND k = 2 ORDER BY distance",
        (_vec(1, 2, 3),),
    ).fetchall()
    assert [r[0] for r in rows] == [a, b]


def test_fts_search(conn: sqlite3.Connection) -> None:
    fid = _add_file(conn, "/home/u/Fee Receipt Sem 5.pdf")
    conn.execute(
        "INSERT INTO files_fts(rowid, name, path_tokens, title, keywords) VALUES (?, ?, ?, ?, ?)",
        (fid, "Fee Receipt Sem 5.pdf", "home u", "Fee receipt", "fees semester"),
    )
    hits = conn.execute("SELECT rowid FROM files_fts WHERE files_fts MATCH 'fee*'").fetchall()
    assert [h[0] for h in hits] == [fid]


def test_delete_cascades_to_all_derived_data(conn: sqlite3.Connection) -> None:
    d = _add_file(conn, "/proj")
    f = _add_file(conn, "/proj/notes.md", parent=d)
    other = _add_file(conn, "/other.md")
    conn.execute("INSERT INTO files_fts(rowid, name) VALUES (?, 'notes')", (f,))
    conn.execute("INSERT INTO vec_files(file_id, embedding) VALUES (?, vec_int8(?))", (f, _vec(1)))
    chunk = conn.execute(
        "INSERT INTO chunks(file_id, ord, text_excerpt, char_start, char_end) VALUES (?, 0, 'x', 0, 1)",
        (f,),
    ).lastrowid
    conn.execute(
        "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, vec_int8(?))", (chunk, _vec(1))
    )
    conn.execute("INSERT INTO relations VALUES (?, ?, 0.9, 'content')", (other, f))
    conn.execute(
        "INSERT INTO access_log(file_id, ts, source) VALUES (?, ?, 'edit')", (f, time.time())
    )
    conn.execute("INSERT INTO projects(folder_id) VALUES (?)", (d,))

    conn.execute("DELETE FROM files WHERE id = ?", (d,))  # deleting the folder removes its child

    for table, col in [
        ("files", "id"),
        ("files_fts", "rowid"),
        ("vec_files", "file_id"),
        ("chunks", "file_id"),
        ("relations", "dst_id"),
        ("access_log", "file_id"),
        ("projects", "folder_id"),
    ]:
        n = conn.execute(f"SELECT count(*) FROM {table} WHERE {col} IN (?, ?)", (d, f)).fetchone()[
            0
        ]
        assert n == 0, table
    assert conn.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 1


def test_undo_log_outlives_plan_deletion_attempt(conn: sqlite3.Connection) -> None:
    pid = conn.execute(
        "INSERT INTO action_plans(kind, json, created_at) VALUES ('clean', '{}', 0)"
    ).lastrowid
    conn.execute("INSERT INTO undo_log(plan_id, op, src, ts) VALUES (?, 'trash', '/x', 0)", (pid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM action_plans WHERE id = ?", (pid,))


def test_check_constraints(conn: sqlite3.Connection) -> None:
    f = _add_file(conn, "/x")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO access_log(file_id, ts, source) VALUES (?, 0, 'bogus')", (f,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO relations VALUES (?, ?, 1, 'bogus')", (f, f))


def test_readonly_connection_sees_writes(db_path: Path, conn: sqlite3.Connection) -> None:
    _add_file(conn, "/seen")
    ro = db.connect(db_path, readonly=True)
    assert ro.execute("SELECT path FROM files").fetchone()[0] == "/seen"
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO files(path, name) VALUES ('/no', 'no')")
    ro.close()


def test_transaction_rolls_back(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), db.transaction(conn):
        _add_file(conn, "/rolled-back")
        raise RuntimeError
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 0

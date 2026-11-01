"""Content pipeline (HashEmbedder: deterministic, offline)."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from tests.semantic_corpus import build_corpus
from zachary.core.config import Config
from zachary.core.db import open_db
from zachary.indexer.embedder import HashEmbedder
from zachary.indexer.scanner import scan
from zachary.indexer.semantic import SemanticIndexer, keywords, quick_hash
from zachary.indexer.store import FileStore


@pytest.fixture
def sem(store: FileStore, home: Path, db_path: Path) -> SemanticIndexer:
    build_corpus(home)
    scan(store)
    conn = open_db(db_path)  # its own connection, like the background worker
    return SemanticIndexer(conn, Config(conn, store.config.platform), HashEmbedder())


def _count(conn: sqlite3.Connection, sql: str, *args: object) -> int:
    return int(conn.execute(sql, args).fetchone()[0])


def test_embeds_everything_and_stores_passages(sem: SemanticIndexer, store: FileStore) -> None:
    n = sem.pending_count()
    assert n == 9
    st = sem.run(64)
    assert st.files == 9 and st.extracted == 8 and st.name_only == 1  # the jpg
    c = store.conn
    assert sem.pending_count() == 0
    assert _count(c, "SELECT count(*) FROM vec_files") == 0  # file vectors live in vec_ivf
    assert _count(c, "SELECT count(*) FROM vec_ivf") == 9
    assert _count(c, "SELECT count(*) FROM chunks") == _count(c, "SELECT count(*) FROM vec_chunks")
    title, kw = c.execute(
        "SELECT title, keywords FROM files_fts WHERE rowid ="
        " (SELECT id FROM files WHERE name = 'receipt_sem5.pdf')"
    ).fetchone()
    assert title == "fee receipt semester 5" and "tuition" in kw
    # content words are now keyword-searchable
    hit = c.execute("SELECT rowid FROM files_fts WHERE files_fts MATCH 'installment'").fetchall()
    assert len(hit) == 1


def test_documents_and_code_first(sem: SemanticIndexer) -> None:
    first = sem.pending(3)
    assert {r["kind"] for r in first} <= {"document", "code"}
    assert first[0]["name"] == "ll.cpp"  # newest document/code


def test_resave_unchanged_does_not_reembed(
    sem: SemanticIndexer, store: FileStore, home: Path
) -> None:
    sem.run(64)
    notes = home / "College" / "Sem3" / "DSA" / "Unit2 notes.pdf"
    cid_before = {r[0] for r in store.conn.execute("SELECT id FROM chunks")}
    later = time.time() + 5
    os.utime(notes, (later, later))  # "saved" again, same bytes
    scan(store)
    assert sem.pending_count() == 1
    st = sem.run(64)
    assert st.extracted == 0 and st.reused == 1 and st.chunks == 0
    assert {r[0] for r in store.conn.execute("SELECT id FROM chunks")} == cid_before


def test_changed_content_is_reembedded(sem: SemanticIndexer, store: FileStore, home: Path) -> None:
    sem.run(64)
    f = home / "Personal" / "paneer.txt"
    f.write_text("Completely new content about tax returns and invoices.")
    later = time.time() + 5
    os.utime(f, (later, later))
    scan(store)
    st = sem.run(64)
    assert st.extracted == 1
    kw = store.conn.execute(
        "SELECT keywords FROM files_fts WHERE rowid = (SELECT id FROM files WHERE name = ?)",
        ("paneer.txt",),
    ).fetchone()[0]
    assert "invoices" in kw and "paneer" not in kw


def test_rename_reuses_content_vectors(sem: SemanticIndexer, store: FileStore, home: Path) -> None:
    sem.run(64)
    old = home / "Personal" / "goa trip.md"
    fid = store.get_row(str(old)).id  # type: ignore[union-attr]
    os.rename(old, home / "Personal" / "holiday plan.md")
    scan(store)
    st = sem.run(64)
    assert st.reused == 1 and st.extracted == 0
    row = store.conn.execute("SELECT embed_path FROM files WHERE id = ?", (fid,)).fetchone()
    assert row[0].endswith("holiday plan.md")


def test_name_only_folder_rule(sem: SemanticIndexer, store: FileStore, home: Path) -> None:
    store.config.add_folder_rule(home / "Personal", "name_only")
    st = sem.run(64)
    assert st.name_only == 4  # expenses.xlsx, goa trip.md, paneer.txt, IMG.jpg
    n = _count(
        store.conn,
        "SELECT count(*) FROM chunks c JOIN files f ON f.id = c.file_id WHERE f.path LIKE ?",
        str(home / "Personal") + "%",
    )
    assert n == 0


def test_delete_removes_all_derived_data(
    sem: SemanticIndexer, store: FileStore, home: Path
) -> None:
    sem.run(64)
    f = home / "College" / "Sem5" / "Fees" / "receipt_sem5.pdf"
    fid = store.get_row(str(f)).id  # type: ignore[union-attr]
    f.unlink()
    scan(store)
    c = store.conn
    for sql in [
        "SELECT count(*) FROM chunks WHERE file_id = ?",
        "SELECT count(*) FROM vec_files WHERE file_id = ?",
        "SELECT count(*) FROM vec_ivf WHERE file_id = ?",
    ]:
        assert _count(c, sql, fid) == 0, sql
    assert _count(c, "SELECT count(*) FROM chunks") == _count(c, "SELECT count(*) FROM vec_chunks")


def test_quick_hash_and_keywords(home: Path) -> None:
    f = home / "x.bin"
    f.write_bytes(b"a" * 300_000)
    h1 = quick_hash(str(f), 300_000)
    f.write_bytes(b"a" * 150_000 + b"b" + b"a" * 149_999)  # change in the middle only
    assert quick_hash(str(f), 300_000) == h1  # by design: a change detector, not a dup check
    f.write_bytes(b"b" + b"a" * 299_999)
    assert quick_hash(str(f), 300_000) != h1
    assert keywords("the fee fee fee is due due tomorrow and the receipt").startswith("fee due")


def test_future_mtime_is_not_reprocessed_forever(
    sem: SemanticIndexer, store: FileStore, home: Path
) -> None:
    sem.run(64)
    f = home / "Personal" / "paneer.txt"
    future = time.time() + 3600  # clock skew / a file from a fast clock
    os.utime(f, (future, future))
    scan(store)
    assert sem.pending_count() == 1
    sem.run(64)
    assert sem.pending_count() == 0  # embedded_at >= mtime, so it doesn't come back

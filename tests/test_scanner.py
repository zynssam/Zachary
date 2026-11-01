from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from tests.fakes import write
from zachary.indexer.scanner import scan, scan_subtree
from zachary.indexer.store import FileStore


def _ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {str(p): int(i) for i, p in conn.execute("SELECT id, path FROM files")}


def _fts_name(conn: sqlite3.Connection, fid: int) -> tuple[str, str]:
    row = conn.execute("SELECT name, path_tokens FROM files_fts WHERE rowid = ?", (fid,)).fetchone()
    return str(row[0]), str(row[1])


def make_tree(home: Path) -> None:
    write(home / "College" / "Sem5" / "Fee Receipt Sem 5.pdf")
    write(home / "College" / "Sem5" / "notes.md")
    write(home / "code" / "zach" / "pyproject.toml")
    write(home / "code" / "zach" / "main.py")
    write(home / "code" / "zach" / "node_modules" / "lib" / "index.js")
    write(home / "code" / "zach" / "__pycache__" / "main.cpython-312.pyc")
    write(home / "code" / "zach" / ".git" / "HEAD")
    write(home / "Downloads" / "movie.mkv.part")
    write(home / "Downloads" / "~$report.docx")
    write(home / "Downloads" / "setup.exe")


def test_first_scan_metadata_and_excludes(store: FileStore, home: Path) -> None:
    make_tree(home)
    st = scan(store, workers=4)
    conn = store.conn
    paths = set(_ids(conn))
    assert str(home / "College" / "Sem5" / "Fee Receipt Sem 5.pdf") in paths
    assert str(home / "code" / "zach" / "main.py") in paths
    assert str(home / "Downloads" / "setup.exe") in paths
    assert not any("node_modules" in p or "__pycache__" in p or ".git" in p for p in paths)
    assert not any(p.endswith((".part", "~$report.docx")) for p in paths)
    assert st.files == 5 and st.skipped_excluded == 5
    # Folders are stored too, with parent links.
    row = conn.execute(
        "SELECT f.kind, f.ext, p.path FROM files f JOIN files p ON p.id = f.parent_id"
        " WHERE f.name = 'Fee Receipt Sem 5.pdf'"
    ).fetchone()
    assert tuple(row) == ("document", ".pdf", str(home / "College" / "Sem5"))
    assert (
        conn.execute("SELECT count(*) FROM files").fetchone()[0]
        == conn.execute("SELECT count(*) FROM files_fts").fetchone()[0]
    )
    # Markers are seen even though .git itself is excluded.
    assert st.markers[str(home / "code" / "zach")] == {".git", "pyproject.toml"}


def _meta(conn: sqlite3.Connection) -> dict[str, tuple[object, ...]]:
    return {
        str(r[0]): tuple(r[1:])
        for r in conn.execute("SELECT path, is_dir, size, mtime, inode, dev FROM files")
    }


def test_rescan_unchanged_is_a_noop(store: FileStore, home: Path) -> None:
    make_tree(home)
    scan(store)
    before = _meta(store.conn)
    r = scan(store).result
    after = _meta(store.conn)
    diff = {p: (before[p], after[p]) for p in before if before[p] != after.get(p)}
    assert (r.inserted, r.moved, r.deleted) == (0, 0, 0), diff
    if sys.platform == "win32":
        # NTFS refreshes a folder's timestamp in its parent's index lazily, so a
        # second scandir may see a newer folder mtime. Files must never differ.
        assert all(before[p][0] == 1 for p in diff), diff
    else:
        assert r.updated == 0, diff


def test_rescan_detects_new_modified_deleted(store: FileStore, home: Path) -> None:
    make_tree(home)
    scan(store)
    write(home / "new.txt")
    notes = home / "College" / "Sem5" / "notes.md"
    notes.write_text("changed and longer")
    os.utime(notes, (time.time() + 5, time.time() + 5))
    (home / "Downloads" / "setup.exe").unlink()
    r = scan(store).result
    assert r.inserted == 1 and r.deleted == 1
    assert any(p.endswith("notes.md") for _, p, _ in r.changed_files)
    assert store.get_row(str(home / "Downloads" / "setup.exe")) is None


def test_rename_keeps_identity(store: FileStore, home: Path) -> None:
    make_tree(home)
    scan(store)
    old = home / "College" / "Sem5" / "Fee Receipt Sem 5.pdf"
    fid = _ids(store.conn)[str(old)]
    store.conn.execute("INSERT INTO access_log(file_id, ts, source) VALUES (?, 1, 'zach')", (fid,))
    new = home / "College" / "Sem5" / "Fees Paid.pdf"
    os.rename(old, new)
    r = scan(store).result
    assert r.moved == 1 and r.inserted == 0 and r.deleted == 0
    assert _ids(store.conn)[str(new)] == fid
    assert _fts_name(store.conn, fid)[0] == "fees paid pdf"
    # Derived data survives the rename.
    n = store.conn.execute("SELECT count(*) FROM access_log WHERE file_id = ?", (fid,)).fetchone()
    assert n[0] == 1


def test_folder_move_keeps_descendant_ids_and_updates_paths(store: FileStore, home: Path) -> None:
    make_tree(home)
    scan(store)
    before = _ids(store.conn)
    fee = before[str(home / "College" / "Sem5" / "Fee Receipt Sem 5.pdf")]
    os.rename(home / "College", home / "University")
    r = scan(store).result
    after = _ids(store.conn)
    assert after[str(home / "University" / "Sem5" / "Fee Receipt Sem 5.pdf")] == fee
    assert not any("College" in p for p in after)
    assert r.inserted == 0 and r.deleted == 0
    assert "university" in _fts_name(store.conn, fee)[1]
    parent = store.conn.execute(
        "SELECT p.path FROM files f JOIN files p ON p.id = f.parent_id WHERE f.id = ?", (fee,)
    ).fetchone()[0]
    assert parent == str(home / "University" / "Sem5")


def test_recycled_inode_is_not_treated_as_move(store: FileStore, home: Path) -> None:
    a = write(home / "a.txt", "aaaa")
    scan(store)
    row = store.get_row(str(a))
    assert row is not None
    # Simulate a different file that got the same inode: different size/mtime.
    b = write(home / "b.txt", "completely different content")
    a.unlink()
    store.conn.execute(
        "UPDATE files SET inode = ?, dev = ? WHERE id = ?",
        (os.lstat(b).st_ino, os.lstat(b).st_dev, row.id),
    )
    r = scan(store).result
    assert r.moved == 0 and r.inserted == 1 and r.deleted == 1


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlinks_are_not_followed(store: FileStore, home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside / "secret.txt")
    os.symlink(outside, home / "link")
    os.symlink(home, home / "loop")
    st = scan(store)
    assert st.files == 0
    assert not any("secret" in p for p in _ids(store.conn))


def test_scan_subtree_and_missing_root(store: FileStore, home: Path) -> None:
    make_tree(home)
    scan(store)
    write(home / "College" / "Sem6" / "timetable.pdf")
    st = scan_subtree(store, str(home / "College"))
    assert st.result.inserted == 2  # Sem6 folder + file
    assert scan(store, [str(home / "does-not-exist")]).dirs == 0


def test_exclude_change_and_purge(store: FileStore, home: Path) -> None:
    make_tree(home)
    scan(store)
    store.config.add_exclude(home / "College")
    store.reload()
    removed = store.purge_excluded()
    assert removed >= 3
    assert not any("College" in p for p in _ids(store.conn))
    # Purge also removes FTS rows (trigger).
    n_files = store.conn.execute("SELECT count(*) FROM files").fetchone()[0]
    assert store.conn.execute("SELECT count(*) FROM files_fts").fetchone()[0] == n_files


def test_removed_root_is_purged(store: FileStore, home: Path, tmp_path: Path) -> None:
    make_tree(home)
    other = tmp_path / "other"
    write(other / "x.txt")
    store.config.set_index_roots([home, other])
    store.reload()
    scan(store)
    assert store.get_row(store.norm(other / "x.txt")) is not None
    store.config.set_index_roots([home])
    store.reload()
    store.purge_excluded()
    assert store.get_row(store.norm(other / "x.txt")) is None


def test_ensure_fts_rebuilds(store: FileStore, home: Path) -> None:
    make_tree(home)
    scan(store)
    store.conn.execute("DELETE FROM files_fts")
    n = store.ensure_fts()
    assert n > 0
    assert store.ensure_fts() == 0


def test_reconcile_paths_replaces_file_with_dir(store: FileStore, home: Path) -> None:
    f = write(home / "thing")
    scan(store)
    f.unlink()
    write(home / "thing" / "inner.txt")
    r = store.reconcile_paths([str(home / "thing")])
    assert store.get_row(str(home / "thing" / "inner.txt")) is not None
    assert r.deleted == 1


def test_inode_key_fits_sqlite() -> None:
    from zachary.indexer.store import inode_key

    assert inode_key(None) is None and inode_key(0) is None
    assert inode_key(12345) == 12345
    big_ntfs = (0x8001 << 48) | 0x1234  # sequence number with the top bit set
    assert -(1 << 63) <= inode_key(big_ntfs) < (1 << 63)  # type: ignore[operator]
    refs_128 = (0xDEADBEEF << 64) | big_ntfs
    assert inode_key(refs_128) == inode_key(big_ntfs)  # low 64 bits = NTFS FRN

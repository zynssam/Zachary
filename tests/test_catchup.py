from __future__ import annotations

import os
from pathlib import Path

from tests.fakes import FakePlatform, write
from zachary.core.platform import JournalCursor, JournalRecord
from zachary.indexer.catchup import catch_up, load_cursor
from zachary.indexer.projects import list_projects
from zachary.indexer.store import FileStore


def test_scan_catchup_when_no_journal(
    store: FileStore, home: Path, fake_platform: FakePlatform
) -> None:
    write(home / "proj" / "Makefile")
    write(home / "a.txt")
    st = catch_up(store, fake_platform)
    assert st.method == {str(home): "scan"}
    assert store.get_row(str(home / "a.txt")) is not None
    assert [p.name for p in list_projects(store.conn)] == ["proj"]
    # Offline changes are found on the next start (mtime diff).
    (home / "a.txt").unlink()
    write(home / "b.txt")
    st = catch_up(store, fake_platform)
    assert st.result.inserted == 1 and st.result.deleted == 1


def test_journal_catchup_replays_changes(
    store: FileStore, home: Path, fake_platform: FakePlatform
) -> None:
    write(home / "docs" / "old name.txt")
    fake_platform.cursor = JournalCursor(journal_id=7, next_usn=100)
    st = catch_up(store, fake_platform)  # first run: scan, and remember the cursor
    assert st.method[str(home)] == "scan"
    assert load_cursor(store, str(home)) == JournalCursor(7, 100)

    old = home / "docs" / "old name.txt"
    row = store.get_row(str(old))
    docs = store.get_row(str(home / "docs"))
    assert row is not None and docs is not None and row.inode and docs.inode
    # Offline: rename + new file + unrelated file outside any known folder.
    new = home / "docs" / "new name.txt"
    os.rename(old, new)
    created = write(home / "docs" / "fresh.txt")
    fake_platform.journal = (
        [
            JournalRecord(row.inode, docs.inode, "old name.txt", 0x1000),
            JournalRecord(row.inode, docs.inode, "new name.txt", 0x2000),
            JournalRecord(os.lstat(created).st_ino, docs.inode, "fresh.txt", 0x100),
            JournalRecord(999_999, 888_888, "elsewhere.txt", 0x100),
        ],
        JournalCursor(7, 250),
    )
    st = catch_up(store, fake_platform)
    assert st.method[str(home)] == "journal" and st.records == 4
    moved = store.get_row(str(new))
    assert moved is not None and moved.id == row.id  # identity kept
    assert store.get_row(str(old)) is None
    assert store.get_row(str(created)) is not None
    assert load_cursor(store, str(home)) == JournalCursor(7, 250)


def test_journal_unavailable_falls_back_to_scan(
    store: FileStore, home: Path, fake_platform: FakePlatform
) -> None:
    fake_platform.cursor = JournalCursor(1, 1)
    catch_up(store, fake_platform)
    fake_platform.journal = None  # e.g. journal recreated / wrapped
    write(home / "x.txt")
    st = catch_up(store, fake_platform)
    assert st.method[str(home)] == "scan"
    assert store.get_row(str(home / "x.txt")) is not None


def test_missing_root_keeps_rows(
    store: FileStore, home: Path, fake_platform: FakePlatform, tmp_path: Path
) -> None:
    usb = tmp_path / "usb"
    write(usb / "photo.jpg")
    store.config.set_index_roots([home, usb])
    store.reload()
    catch_up(store, fake_platform)
    import shutil

    shutil.rmtree(usb)  # "unplugged"
    st = catch_up(store, fake_platform)
    assert st.method[store.norm(usb)] == "missing"
    assert store.get_row(store.norm(usb / "photo.jpg")) is not None

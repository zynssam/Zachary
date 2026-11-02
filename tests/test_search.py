from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.fakes import FakePlatform, write
from zachary.core.platform import RecentItem
from zachary.indexer.recent import ingest_recent
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore
from zachary.search.hybrid import fts_expression, query_terms, recent, search

OLD = time.time() - 86400 * 120


@pytest.fixture
def indexed(store: FileStore, home: Path, fake_platform: FakePlatform) -> FileStore:
    for name in [
        "Fee Receipt Sem 5.pdf",
        "fee_structure_2024.pdf",
        "Hostel Fees.xlsx",
        "coffee_shop_menu.pdf",  # contains "fee" inside a word: must not match "fees"
    ]:
        write(home / "College" / "Sem5" / name, mtime=OLD)
    write(home / "code" / "dsa" / "linked_list.cpp", mtime=OLD)
    write(home / "code" / "dsa" / "LinkedListNotes.md", mtime=OLD)
    write(home / "Downloads" / "feedback_form.pdf", mtime=OLD)
    write(home / "Downloads" / "report_final_v2.docx", mtime=OLD)
    write(home / "Documents" / "फीस रसीद.pdf", mtime=OLD)
    scan(store)
    return store


def _yesterday_afternoon() -> float:
    return (datetime.now() - timedelta(days=1)).replace(hour=14, minute=0).timestamp()


def _names(store: FileStore, q: str, **kw: object) -> list[str]:
    return [r.name for r in search(store.conn, q, **kw).results]  # type: ignore[arg-type]


def test_fees_yesterday_finds_doc_opened_yesterday_via_os_recent(
    indexed: FileStore, home: Path, fake_platform: FakePlatform
) -> None:
    """Phase 1 'done when': a fee doc opened yesterday (per the OS) is found."""
    target = home / "College" / "Sem5" / "Fee Receipt Sem 5.pdf"
    fake_platform.recent = [
        RecentItem(target, _yesterday_afternoon()),
        RecentItem(home / "not-indexed.pdf", _yesterday_afternoon()),
    ]
    assert ingest_recent(indexed, fake_platform) == 1
    for q in [
        "fees yesterday",
        "the fee document I opened yesterday",
        "kal wali fees file",  # Hinglish
        "fee yesterdy",  # typo
        "ninne fee",  # Kannada
    ]:
        resp = search(indexed.conn, q)
        assert resp.results, q
        top = resp.results[0]
        assert top.name == "Fee Receipt Sem 5.pdf", q
        assert "opened yesterday" in top.why
        assert resp.range_matched


def test_keyword_ranking_and_stemming(indexed: FileStore) -> None:
    names = _names(indexed, "fees")
    assert set(names[:3]) == {"Fee Receipt Sem 5.pdf", "fee_structure_2024.pdf", "Hostel Fees.xlsx"}
    assert "coffee_shop_menu.pdf" not in names
    assert _names(indexed, "fee receipt")[0] == "Fee Receipt Sem 5.pdf"
    assert _names(indexed, "linked list")[:2] == ["linked_list.cpp", "LinkedListNotes.md"] or set(
        _names(indexed, "linked list")[:2]
    ) == {"linked_list.cpp", "LinkedListNotes.md"}


def test_prefix_as_you_type(indexed: FileStore) -> None:
    assert _names(indexed, "hoste")[0] == "Hostel Fees.xlsx"
    assert "feedback_form.pdf" in _names(indexed, "feedb")


def test_folder_words_match(indexed: FileStore) -> None:
    names = _names(indexed, "dsa")
    assert "linked_list.cpp" in names


def test_typo_correction(indexed: FileStore) -> None:
    resp = search(indexed.conn, "recipt")
    assert resp.corrected == {"recipt": "receipt"}
    assert resp.results[0].name == "Fee Receipt Sem 5.pdf"


def test_devanagari_name(indexed: FileStore) -> None:
    assert _names(indexed, "रसीद") == ["फीस रसीद.pdf"]


def test_time_range_without_match_falls_back(indexed: FileStore) -> None:
    resp = search(indexed.conn, "hostel yesterday")
    assert resp.time_range is not None and not resp.range_matched
    assert resp.results[0].name == "Hostel Fees.xlsx"


def test_time_only_query_lists_recent(indexed: FileStore, home: Path) -> None:
    write(home / "today.txt")  # mtime now
    indexed.reconcile_paths([str(home / "today.txt")])
    assert _names(indexed, "today") == ["today.txt"] or "today.txt" in _names(indexed, "today")
    assert _names(indexed, "what did I open today") == ["today.txt"]


def test_recent_beats_old_for_same_keyword(indexed: FileStore, home: Path) -> None:
    write(home / "Downloads" / "fee_notice_new.pdf")  # brand new
    indexed.reconcile_paths([str(home / "Downloads" / "fee_notice_new.pdf")])
    names = _names(indexed, "fee notice")
    assert names[0] == "fee_notice_new.pdf"


def test_exclude_hides_results_before_purge(indexed: FileStore, home: Path) -> None:
    indexed.config.add_exclude(home / "College")
    names = _names(indexed, "fees", rules=indexed.config.path_rules())
    assert not any("Fee" in n or "fee_structure" in n for n in names)


def test_include_dirs_flag(indexed: FileStore) -> None:
    assert "dsa" in _names(indexed, "dsa")
    assert "dsa" not in _names(indexed, "dsa", include_dirs=False)


def test_query_terms_and_fts_expression() -> None:
    assert query_terms("open my fee document") == ["fee"]
    assert query_terms("mera fee wala file dikhao") == ["fee"]
    assert fts_expression(["fee", "receipt"], mode="and") == '"fee" AND "receipt"*'
    assert fts_expression(['a"b'], mode="or") == '"a""b"*'
    assert fts_expression(["x"], mode="or") == '"x"'  # 1-char last term: no prefix
    assert fts_expression(["fee"], mode="and", prefix=False) == '"fee"'


def test_empty_and_weird_queries(indexed: FileStore) -> None:
    assert search(indexed.conn, "").results == []
    assert search(indexed.conn, '"; DROP TABLE files; --').results == []
    assert search(indexed.conn, "*** OR NEAR(").results == []


def test_recent_view(indexed: FileStore, home: Path, fake_platform: FakePlatform) -> None:
    target = home / "Downloads" / "report_final_v2.docx"
    fake_platform.recent = [RecentItem(target, _yesterday_afternoon())]
    ingest_recent(indexed, fake_platform)
    items = recent(indexed.conn, limit=5)
    assert items[0].name == "report_final_v2.docx" and items[0].how == "opened"


def test_search_speed_small_index(indexed: FileStore) -> None:
    resp = search(indexed.conn, "fees yesterday")
    assert resp.took_ms < 50


def test_rules_ignore_excluded_names_above_the_root(
    conn: object, tmp_path: Path, fake_platform: FakePlatform
) -> None:
    """A root inside ~/.cache (or a folder named 'build') must still be searchable."""
    from zachary.core.config import Config
    from zachary.core.db import open_db

    root = tmp_path / ".cache" / "build" / "home"
    write(root / "notes" / "fee receipt.pdf")
    c = open_db(tmp_path / "rules.db")
    cfg = Config(c, fake_platform)
    cfg.seed_defaults()
    cfg.set_index_roots([root])
    st = FileStore(c, cfg)
    scan(st)
    assert [r.name for r in search(c, "fee receipt", rules=cfg.path_rules()).results] == [
        "fee receipt.pdf"
    ]
    write(root / "node_modules" / "fee.js")  # but names *below* the root still apply
    assert cfg.path_rules().is_excluded(str(root / "node_modules" / "fee.js"), False)
    c.close()


def test_kind_hints(indexed: FileStore, home: Path, fake_platform: FakePlatform) -> None:
    # "code" is not in any name: it selects the kind instead of breaking the match.
    assert _names(indexed, "linked list code")[0] == "linked_list.cpp"
    # A document opened yesterday beats a song with the same word opened later.
    write(home / "Music" / "fee song.mp3", mtime=OLD)
    indexed.reconcile_paths([str(home / "Music" / "fee song.mp3")])
    y = _yesterday_afternoon()
    fake_platform.recent = [
        RecentItem(home / "College" / "Sem5" / "Fee Receipt Sem 5.pdf", y),
        RecentItem(home / "Music" / "fee song.mp3", y + 3600),
    ]
    ingest_recent(indexed, fake_platform)
    assert _names(indexed, "the fee document I opened yesterday")[0] == "Fee Receipt Sem 5.pdf"
    assert _names(indexed, "fee song yesterday")[0] == "fee song.mp3"
    assert _names(indexed, "fees yesterday")[0] == "Fee Receipt Sem 5.pdf"  # document prior


def test_split_kind_hints() -> None:
    from zachary.search.hybrid import split_kind_hints

    assert split_kind_hints(["linked", "list", "code"], ["linked", "list", "code"],
                            keep_if_alone=True) == (["linked", "list"], {"code"})  # fmt: skip
    assert split_kind_hints(["photo"], ["photo"], keep_if_alone=True) == (["photo"], {"image"})
    assert split_kind_hints(["photo"], ["photo"], keep_if_alone=False) == ([], {"image"})
    assert split_kind_hints(["fee"], ["the", "fee", "document"], keep_if_alone=True) == (
        ["fee"],
        {"document"},
    )


def test_kind_word_alone_lists_the_most_recent_of_that_kind(store: FileStore, home: Path) -> None:
    """Voice heard only "folders" (or you typed it): recent folders, not "Nothing found"."""
    now = time.time()
    for name, age in (("old_course", 400), ("hackathon", 2), ("Screenshots", 30)):
        write(home / name / "x.txt", mtime=now - age * 86400)
    write(home / "pics" / "a.png", mtime=now - 3 * 86400)
    write(home / "pics" / "b.jpg", mtime=now - 1 * 86400)
    scan(store)
    for name, age in (("old_course", 400), ("hackathon", 2), ("Screenshots", 30), ("pics", 5)):
        import os

        os.utime(home / name, (now - age * 86400, now - age * 86400))
    scan(store)
    dirs = [r.name for r in search(store.conn, "folders", limit=10).results]
    assert dirs[:3] == ["hackathon", "pics", "Screenshots"] and "old_course" in dirs
    assert all(r.is_dir for r in search(store.conn, "folders", limit=10).results)
    photos = [r.name for r in search(store.conn, "photos").results]
    assert photos[:2] == ["b.jpg", "a.png"]

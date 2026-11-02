from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.fakes import FakePlatform, write
from zachary.actions.safe_actions import ActionNotAllowed, SafeActions
from zachary.brain.context import Item
from zachary.brain.handler import Handler, clear_winner
from zachary.core.platform import RecentItem
from zachary.indexer import projects
from zachary.indexer.recent import ingest_recent
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore
from zachary.search.hybrid import SearchResult

OLD = time.time() - 86400 * 200


@pytest.fixture
def handler(store: FileStore, home: Path, fake_platform: FakePlatform) -> Handler:
    write(home / "College" / "Fee Receipt Sem 5.pdf", mtime=OLD)
    write(home / "College" / "fee_structure_2024.pdf", mtime=OLD)
    write(home / "College" / "Hostel Fees.xlsx", mtime=OLD)
    write(home / "Work" / "report_v1.docx", mtime=OLD)
    write(home / "Work" / "report_v2.docx", mtime=OLD + 86400)
    write(home / "Work" / "report_v3.docx", mtime=OLD + 2 * 86400)
    write(home / "code" / "zachary" / "pyproject.toml", mtime=time.time() - 60)
    write(home / "code" / "dsa" / "Makefile", mtime=OLD)
    write(home / "code" / "dsa" / "linked_list.cpp", mtime=OLD)
    st = scan(store)
    projects.apply_markers(store, st.markers, scope=None)
    y = (datetime.now() - timedelta(days=1)).replace(hour=14).timestamp()
    fake_platform.recent = [RecentItem(home / "College" / "Fee Receipt Sem 5.pdf", y)]
    ingest_recent(store, fake_platform)
    return Handler(store.conn, SafeActions(store.conn, fake_platform), store.config)


def test_open_clear_winner_acts(handler: Handler, fake_platform: FakePlatform) -> None:
    r = handler.submit("open fee receipt")
    assert r.action == "open" and r.target is not None
    assert r.target.name == "Fee Receipt Sem 5.pdf"
    assert r.say == "Opening Fee Receipt Sem 5.pdf."
    assert [p.name for p in fake_platform.opened] == ["Fee Receipt Sem 5.pdf"]
    # Opening is logged as a Zachary access.
    n = handler.conn.execute("SELECT count(*) FROM access_log WHERE source = 'zach'").fetchone()
    assert n[0] == 1


def test_spec_sentence_opens_the_fee_document(
    handler: Handler, fake_platform: FakePlatform
) -> None:
    r = handler.submit(
        "Hey Zach, I opened a document about my fees yesterday, I forgot the name, open it."
    )
    assert r.target is not None and r.target.name == "Fee Receipt Sem 5.pdf"
    assert fake_platform.opened


def test_open_ambiguous_asks_and_then_follow_up(
    handler: Handler, fake_platform: FakePlatform
) -> None:
    r = handler.submit("open report")
    assert r.action is None and not fake_platform.opened
    assert r.say.startswith("Which one?") and 2 <= len(r.items) <= 5
    names = [i.name for i in r.items]
    second = names[1]
    r2 = handler.submit("open the second one")
    assert r2.action == "open" and r2.target is not None and r2.target.name == second
    assert fake_platform.opened[-1].name == second
    r3 = handler.submit("show its folder")
    assert r3.action == "reveal" and fake_platform.revealed[-1].name == second


def test_older_newer_follow_ups(handler: Handler, fake_platform: FakePlatform) -> None:
    r0 = handler.submit("open report")
    # The newest report ranks first (equal keyword match -> recency decides).
    assert r0.items[0].name == "report_v3.docx"
    top = handler.context.focused_item()
    assert top is not None
    r = handler.submit("the older one")
    assert r.target is not None and r.target.ts < top.ts
    r2 = handler.submit("the newer one")
    assert r2.target is not None and r2.target.name == top.name


def test_preview_never_acts(handler: Handler, fake_platform: FakePlatform) -> None:
    r = handler.preview("open fee receipt")
    assert r.action == "open" and r.say.startswith("Enter to open")
    handler.preview("the second one")
    assert not fake_platform.opened and not fake_platform.revealed


def test_projects_and_recent_views(handler: Handler) -> None:
    r = handler.submit("show me all my projects")
    assert r.view == "projects"
    assert [i.name for i in r.items] == ["zachary", "dsa"]
    r = handler.submit("what did I open yesterday")
    assert r.view == "recent" and r.items[0].name == "Fee Receipt Sem 5.pdf"
    r = handler.preview("")
    assert r.view == "recent" and r.items


def test_open_project_by_name(handler: Handler, fake_platform: FakePlatform) -> None:
    r = handler.submit("open the dsa project")
    assert r.target is not None and r.target.kind == "project"
    assert fake_platform.opened[-1].name == "dsa"


def test_click_act(handler: Handler, fake_platform: FakePlatform) -> None:
    handler.submit("find fees")
    r = handler.act(1, "reveal")
    assert r.action == "reveal" and fake_platform.revealed


def test_follow_up_without_context(handler: Handler) -> None:
    r = handler.submit("open the second one")
    assert r.action is None and "first" in r.say


def test_out_of_range_ordinal(handler: Handler, fake_platform: FakePlatform) -> None:
    handler.submit("open report")
    r = handler.submit("open the 5th one")
    assert r.action is None and "only" in r.say and not fake_platform.opened


def test_nothing_found(handler: Handler) -> None:
    r = handler.submit("open zzzqqq nonexistent")
    assert r.items == [] and r.say.startswith("Nothing found")


def test_clear_winner_rule() -> None:
    a = Item(1, "/a", "a", "other", False, 0, score=0.05)
    b = Item(2, "/b", "b", "other", False, 0, score=0.049)
    c = Item(3, "/c", "c", "other", False, 0, score=0.03)
    assert clear_winner([a])
    assert not clear_winner([a, b])
    assert clear_winner([a, c])
    assert not clear_winner([])


def test_clear_winner_needs_a_strong_match() -> None:
    def res(fid: int, score: float, strong: bool) -> SearchResult:
        return SearchResult(
            fid, f"/{fid}", str(fid), "other", False, 0.0, None, score, strong=strong
        )

    assert clear_winner([res(1, 0.08, True), res(2, 0.03, True)])
    # Time/recency alone made it the top result: ask, never act.
    assert not clear_winner([res(1, 0.08, False), res(2, 0.03, True)])
    assert not clear_winner([res(1, 0.08, False)])


def test_weak_match_asks_instead_of_opening(
    store: FileStore, home: Path, fake_platform: FakePlatform
) -> None:
    """Real smoke-test bug: "the fees document I opened yesterday" opened an unrelated
    spec, because it was the only thing opened yesterday."""
    write(home / "Projects" / "Roommate_Spec.docx", mtime=OLD)
    write(home / "Projects" / "budget.xlsx", mtime=OLD)
    scan(store)
    y = (datetime.now() - timedelta(days=1)).replace(hour=14).timestamp()
    fake_platform.recent = [RecentItem(home / "Projects" / "Roommate_Spec.docx", y)]
    ingest_recent(store, fake_platform)
    h = Handler(store.conn, SafeActions(store.conn, fake_platform), store.config)
    r = h.submit("open the invoice document I opened yesterday")
    assert r.action is None and not fake_platform.opened


def test_corrected_typo_asks_instead_of_opening(
    handler: Handler, fake_platform: FakePlatform
) -> None:
    r = handler.submit("open reciept")
    assert r.action is None and not fake_platform.opened
    assert r.items and r.items[0].name == "Fee Receipt Sem 5.pdf"  # still shown first
    assert r.say.startswith("Did you mean")


def test_safe_actions_refuse_unindexed_or_missing(
    store: FileStore, home: Path, fake_platform: FakePlatform
) -> None:
    f = write(home / "x.txt")
    scan(store)
    row = store.get_row(str(f))
    assert row is not None
    acts = SafeActions(store.conn, fake_platform)
    fake = Item(row.id, str(home / "elsewhere" / "evil.sh"), "evil.sh", "code", False, 0)
    assert not acts.open(fake)  # path doesn't match the indexed row
    assert not acts.open(Item(999999, str(f), "x.txt", "other", False, 0))
    os.remove(f)
    assert not acts.open(Item(row.id, str(f), "x.txt", "other", False, 0))  # gone from disk
    assert not fake_platform.opened
    with pytest.raises(ActionNotAllowed):
        acts.perform("delete", Item(row.id, str(f), "x.txt", "other", False, 0))

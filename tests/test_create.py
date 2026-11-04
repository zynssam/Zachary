"""Creating files/folders ("create a text file and paste what I copied"), sorting them into
Documents/Zachary by content, dictation, and undo."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from tests.fakes import FakePlatform
from zachary.actions import creator, planner, undo
from zachary.actions.safe_actions import SafeActions
from zachary.brain.handler import Handler
from zachary.brain.intents import parse, parse_create
from zachary.core.db import open_db
from zachary.indexer.store import FileStore


@pytest.fixture
def base(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    b = home / "Documents" / "Zachary"
    monkeypatch.setattr(creator, "base_folder", lambda: b)
    return b


@pytest.fixture
def h(store: FileStore, fake_platform: FakePlatform, base: Path) -> Handler:
    return Handler(store.conn, SafeActions(store.conn, fake_platform), store.config)


def docx_text(path: Path) -> str:
    import docx

    return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)


# -- parsing ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "what", "fmt", "name", "place", "paste"),
    [
        ("create a text file and paste whatever i have copied last", "file", "txt", "", "", True),
        ("create a folder called Hackathon Ideas", "folder", "docx", "Hackathon Ideas", "", False),
        ("make a new folder called fees 2027 in downloads", "folder", "docx", "fees 2027",
         "downloads", False),
        ("create a markdown note called Todo in the zachary project", "file", "md", "Todo",
         "zachary project", False),
        ("create a word document called Report", "file", "docx", "Report", "", False),
        ("ek text file banao aur copy kiya hua paste karo", "file", "txt", "", "", True),
        ("paste what i copied into a new file", "file", "docx", "", "", True),
    ],
)  # fmt: skip
def test_parse_create(text: str, what: str, fmt: str, name: str, place: str, paste: bool) -> None:
    assert parse(text).kind == "create"
    s = parse_create(text)
    assert (s.what, s.fmt, s.name, s.place, s.paste) == (what, fmt, name, place, paste)


@pytest.mark.parametrize(
    "text", ["make a document ill dictate", "make a document I'll dictate", "start dictation",
             "take notes", "dictate a text file called standup", "likh lo jo main bolu"],
)  # fmt: skip
def test_parse_dictate(text: str) -> None:
    assert parse(text).kind == "dictate"


def test_not_create() -> None:
    assert parse("open my notes file").kind == "open"
    assert parse("the newer one").kind == "newer"
    assert parse("new document").kind == "create"


# -- creating ---------------------------------------------------------------------------------
def test_never_overwrites(store: FileStore, base: Path) -> None:
    a = creator.create_file(store.conn, "first", fmt="txt", name="todo", folder=base)
    b = creator.create_file(store.conn, "second", fmt="txt", name="todo", folder=base)
    assert a.path.name == "todo.txt" and b.path.name == "todo (2).txt"
    assert a.path.read_text() == "first\n" and b.path.read_text() == "second\n"


def test_docx_and_name_from_content(store: FileStore, base: Path) -> None:
    c = creator.create_file(store.conn, "Meeting with the club team today\n\nSecond paragraph",
                            folder=base)  # fmt: skip
    assert c.path.suffix == ".docx" and c.path.stem == "Meeting with the club team today"
    assert docx_text(c.path) == "Meeting with the club team today\nSecond paragraph"


def test_sorted_into_existing_or_new_folder(store: FileStore, base: Path) -> None:
    (base / "Fees").mkdir(parents=True)
    (base / "Fees" / "hostel fee receipt.txt").write_text("hostel fee")
    (base / "Meeting Notes").mkdir()
    (base / "Meeting Notes" / "club meeting minutes.txt").write_text("club meeting")
    fee = creator.create_file(store.conn, "The tuition fee and hostel fee for next semester",
                              fmt="txt")  # fmt: skip
    assert fee.folder == base / "Fees" and fee.sorted_into == "existing"
    meet = creator.create_file(store.conn, "Minutes of the club meeting about the event",
                               fmt="txt")  # fmt: skip
    assert meet.folder == base / "Meeting Notes"
    new = creator.create_file(store.conn, "Grocery list: grocery milk eggs bread grocery",
                              fmt="txt")  # fmt: skip
    assert new.sorted_into == "new" and new.folder.parent == base
    assert new.folder.name == "Grocery List"


def test_llm_can_name_the_folder(store: FileStore, base: Path) -> None:
    seen: list[tuple[str, list[str]]] = []

    def namer(excerpt: str, existing: list[str]) -> str:
        seen.append((excerpt, existing))
        return "Hackathon Ideas"

    c = creator.create_file(store.conn, "x" * 2000, fmt="txt", name_folder=namer)
    assert c.folder == base / "Hackathon Ideas" and len(seen[0][0]) == 800  # excerpt only


def test_undo_create_trashes_unless_edited(
    store: FileStore, base: Path, fake_platform: FakePlatform
) -> None:
    c = creator.create_file(store.conn, "hello", fmt="txt", name="a", folder=base / "New")
    plan = planner.load(store.conn, c.plan_id)
    assert plan is not None and plan.kind == "create" and plan.status == "executed"
    res = undo.undo(store.conn, fake_platform, plan)
    assert not res.problems and fake_platform.trashed == [c.path]
    assert not (base / "New").exists()  # the folder it made is removed again
    d = creator.create_file(store.conn, "hello", fmt="txt", name="b", folder=base)
    d.path.write_text("edited a lot")
    res2 = undo.undo(store.conn, fake_platform, planner.load(store.conn, d.plan_id))  # type: ignore[arg-type]
    assert res2.problems and d.path.exists()  # your edits are never thrown away


def test_migration_5_keeps_undo_history(tmp_path: Path) -> None:
    from zachary.core import db

    path = tmp_path / "v4.db"
    import sqlite_vec

    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    for i, m in enumerate(db.MIGRATIONS[:4], 1):
        for stmt in db._split_statements(m):
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {i}")
    conn.execute("INSERT INTO action_plans(id, kind, json, status, created_at)"
                 " VALUES (1, 'clean', '{}', 'executed', 0)")  # fmt: skip
    conn.execute("INSERT INTO undo_log(plan_id, op, src, dst, ts) VALUES (1, 'trash', 'a', 'b', 0)")
    conn.commit()
    conn.close()
    c = open_db(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert tuple(c.execute("SELECT op, src, dst FROM undo_log").fetchone()) == ("trash", "a", "b")
    c.execute("INSERT INTO undo_log(plan_id, op, src, dst, ts) VALUES (1, 'create', 'x', '{}', 0)")


# -- through the assistant ------------------------------------------------------------------------
def test_paste_clipboard_into_a_text_file(h: Handler, fake_platform: FakePlatform, base: Path
                                          ) -> None:  # fmt: skip
    assert not h.preview("create a text file and paste what i copied").items
    assert not base.exists()  # typing never creates anything
    r = h.submit("create a text file and paste what i copied", clipboard="Buy milk\nCall mom")
    (made,) = list(base.rglob("*.txt"))
    assert made.read_text() == "Buy milk\nCall mom\n"
    assert fake_platform.opened == [made]  # opened only after it was written
    assert "and opened it" in r.say
    assert h.submit("create a text file and paste what i copied", clipboard="").say.startswith(
        "Your clipboard is empty")  # fmt: skip


def test_create_folder_in_a_place(h: Handler, home: Path, fake_platform: FakePlatform) -> None:
    (home / "Downloads").mkdir()
    r = h.submit("make a new folder called fees 2027 in downloads")
    assert (home / "Downloads" / "fees 2027").is_dir() and "Created the folder" in r.say
    assert h.submit("create a folder called x in nowhere land").say.startswith("I couldn't find")


def test_undo_after_create(h: Handler, base: Path, fake_platform: FakePlatform) -> None:
    h.submit("create a text file called scratch saying hello world")
    made = next(base.rglob("scratch.txt"))
    assert made.read_text() == "hello world\n"
    assert h.submit("undo").say.startswith("Undo “Create scratch.txt”")
    assert h.submit("yes").say.startswith("Undid 1 change") and fake_platform.trashed == [made]


def test_dictation(h: Handler, base: Path, fake_platform: FakePlatform, db_path: Path) -> None:
    r = h.submit("make a document I'll dictate")
    assert r.dictating and "stop dictation" in r.say
    draft = db_path.parent / "dictation-draft.txt"
    assert draft.exists()
    for line in ["hello team this is about the hackathon", "we need a pitch deck",
                 "new paragraph the demo is on friday", "scratch that"]:  # fmt: skip
        rr = h.submit(line)
        assert rr.dictating and rr.say == "" and not fake_platform.opened
    assert h.preview("open spotify").dictating  # while dictating, everything is text
    assert not list(base.rglob("*.docx"))  # nothing written until the end
    h.submit("the demo is on saturday")
    done = h.submit("that's all, stop dictation")
    (made,) = list(base.rglob("*.docx"))
    assert docx_text(made) == ("Hello team this is about the hackathon. We need a pitch deck.\n"
                               "The demo is on saturday. That's all.")  # fmt: skip
    assert fake_platform.opened == [made] and not done.dictating and not draft.exists()
    assert h.submit("open spotify").intent.kind == "open"  # back to normal


def test_dictation_cancel_and_empty(h: Handler, base: Path) -> None:
    h.submit("start dictation")
    assert h.submit("cancel dictation").say.startswith("Dictation cancelled")
    h.submit("take notes")
    assert h.submit("stop dictation").say.startswith("Nothing was dictated")
    assert not base.exists()


def test_dictation_txt_named(h: Handler, base: Path) -> None:
    h.submit("dictate a text file called standup")
    h.submit("yesterday i fixed the bug")
    h.submit("stop dictation")
    (made,) = list(base.rglob("standup.txt"))
    assert made.read_text() == "Yesterday i fixed the bug.\n"
    assert os.path.dirname(made).startswith(str(base))

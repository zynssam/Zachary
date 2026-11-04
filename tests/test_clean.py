"""Phase 7: cleaning and organizing plans, approval, execution and undo."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import pytest

from tests.fakes import FakePlatform
from zachary.actions import executor, planner, undo
from zachary.actions.executor import NotApproved
from zachary.indexer import projects
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore

DAY = 86400.0
OLD = time.time() - 90 * DAY


def put(path: Path, data: bytes, mtime: float = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def snapshot(root: Path) -> dict[str, str]:
    """Every file and folder under root -> content hash ("<dir>" for folders)."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out[os.path.relpath(os.path.join(dirpath, d), root)] = "<dir>"
        for f in filenames:
            p = os.path.join(dirpath, f)
            out[os.path.relpath(p, root)] = hashlib.sha256(Path(p).read_bytes()).hexdigest()
    return out


BIG = os.urandom(300 * 1024)


@pytest.fixture
def tree(store: FileStore, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dl = home / "Downloads"
    monkeypatch.setattr(planner, "_downloads", lambda: str(dl))
    put(home / "Documents" / "report.pdf", BIG)  # the original
    put(dl / "report (1).pdf", BIG)  # duplicate copy
    put(home / "Desktop" / "report copy.pdf", BIG)  # another
    # Same size + same first/last 64 KB, different middle: NOT a duplicate.
    tricky = bytearray(BIG)
    tricky[150 * 1024] ^= 0xFF
    put(home / "Documents" / "report_v2.pdf", bytes(tricky))
    put(home / "Documents" / "other.bin", os.urandom(300 * 1024))  # same size, different
    put(dl / "setup-tool.exe", os.urandom(40 * 1024))  # old installer
    put(dl / "fresh-installer.exe", os.urandom(40 * 1024), mtime=time.time())  # too new
    put(dl / "movie.mp4.crdownload", b"x" * 100)  # junk (excluded from the index)
    put(home / "Documents" / "~$draft.docx", b"lock")  # Office lock file
    (home / "Documents" / "empty").mkdir()
    (home / "Documents" / "has-hidden").mkdir()
    put(home / "Documents" / "has-hidden" / ".keep", b"")
    # Protected: a project and a never_touch folder, each with a duplicate inside.
    put(home / "code" / "app" / "pyproject.toml", b"[project]\nname='app'\n")
    put(home / "code" / "app" / "report.pdf", BIG)
    put(home / "Keep" / "report.pdf", BIG)
    store.config.add_folder_rule(home / "Keep", "never_touch")
    st = scan(store)
    projects.apply_markers(store, st.markers, scope=None)
    return home


def _plan(store: FileStore, **kw: object) -> planner.Plan:
    return planner.save(store.conn, planner.clean_plan(store.conn, store.config, **kw))  # type: ignore[arg-type]


# -- detection -------------------------------------------------------------------------------
def test_duplicates_are_detected_correctly(store: FileStore, tree: Path) -> None:
    plan = _plan(store, categories=["duplicate"])
    srcs = sorted(os.path.relpath(i.src, tree) for i in plan.items)
    # The copy in Downloads and the "copy" on the Desktop; the original is kept.
    assert srcs == sorted([os.path.join("Downloads", "report (1).pdf"),
                           os.path.join("Desktop", "report copy.pdf")])  # fmt: skip
    assert all("Documents" in i.reason and "(kept)" in i.reason for i in plan.items)
    # Not proposed: the same-size/same-ends file, the project's copy, the never_touch copy.
    assert not any(n in i.src for i in plan.items for n in ("report_v2", "other.bin", "code",
                                                              "Keep"))  # fmt: skip


def test_other_categories(store: FileStore, tree: Path) -> None:
    plan = _plan(store)
    by = {os.path.relpath(i.src, tree): i for i in plan.items}
    assert by[os.path.join("Downloads", "setup-tool.exe")].category == "installer"
    assert os.path.join("Downloads", "fresh-installer.exe") not in by
    assert by[os.path.join("Downloads", "movie.mp4.crdownload")].category == "junk"
    assert by[os.path.join("Documents", "~$draft.docx")].category == "junk"
    assert by[os.path.join("Documents", "empty")].category == "empty_folder"
    assert os.path.join("Documents", "has-hidden") not in by  # hidden file inside
    assert "Downloads" not in by and "Documents" not in by  # standard folders stay
    assert "duplicates" in plan.note and "old installers" in plan.note


def test_large_untouched_is_shown_but_not_preselected(
    store: FileStore, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(planner, "LARGE_BYTES", 200 * 1024)
    monkeypatch.setattr(planner, "UNTOUCHED_DAYS", 30)
    plan = _plan(store, categories=["large"])
    assert plan.items and all(i.category == "large" and not i.selected for i in plan.items)


def test_scope_limits_the_plan(store: FileStore, tree: Path) -> None:
    plan = _plan(store, scope=str(tree / "Downloads"))
    assert plan.items and all(i.src.startswith(str(tree / "Downloads")) for i in plan.items)


# -- "done when": no file is ever changed without approval -----------------------------------
def test_nothing_changes_without_approval(
    store: FileStore, tree: Path, fake_platform: FakePlatform
) -> None:
    before = snapshot(tree)
    plan = _plan(store)
    assert plan.items and plan.status == "proposed"
    assert snapshot(tree) == before  # making a plan touches nothing
    with pytest.raises(NotApproved):
        executor.execute(store.conn, fake_platform, store.config, plan)
    planner.set_status(store.conn, plan, "rejected")
    with pytest.raises(NotApproved):
        executor.execute(store.conn, fake_platform, store.config, plan)
    assert snapshot(tree) == before and not fake_platform.trashed


def test_only_selected_items_run(store: FileStore, tree: Path, fake_platform: FakePlatform
                                 ) -> None:  # fmt: skip
    plan = _plan(store)
    keep = plan.items[0]
    planner.edit(store.conn, plan, select={keep.id: False})
    planner.set_status(store.conn, plan, "approved")
    executor.execute(store.conn, fake_platform, store.config, plan)
    assert os.path.lexists(keep.src)
    assert Path(keep.src) not in fake_platform.trashed
    assert len(fake_platform.trashed) == len(plan.items) - 1


def test_changed_or_protected_at_run_time_is_skipped(
    store: FileStore, tree: Path, fake_platform: FakePlatform
) -> None:
    plan = _plan(store, categories=["duplicate"])
    first, second = plan.items
    put(Path(first.src), BIG + b"edited", mtime=time.time())  # edited after the plan
    store.config.add_folder_rule(Path(second.src).parent, "never_touch")  # protected now
    planner.set_status(store.conn, plan, "approved")
    res = executor.execute(store.conn, fake_platform, store.config, plan)
    assert res.done == 0 and res.skipped == 2 and not fake_platform.trashed
    assert "changed since" in first.result and "protected" in second.result


def test_edit_only_before_approval(store: FileStore, tree: Path) -> None:
    plan = _plan(store)
    planner.set_status(store.conn, plan, "approved")
    with pytest.raises(ValueError):
        planner.edit(store.conn, plan, select={1: False})


# -- "done when": undo fully restores every operation ---------------------------------------
def test_execute_then_undo_restores_everything(
    store: FileStore, tree: Path, fake_platform: FakePlatform
) -> None:
    before = snapshot(tree)
    plan = _plan(store)
    planner.set_status(store.conn, plan, "approved")
    res = executor.execute(store.conn, fake_platform, store.config, plan)
    assert res.done == len([i for i in plan.items if i.selected]) and res.failed == 0
    after = snapshot(tree)
    assert os.path.join("Downloads", "report (1).pdf") not in after
    assert os.path.join("Documents", "empty") not in after
    assert planner.load(store.conn, plan.id).status == "executed"  # type: ignore[union-attr]
    u = undo.undo(store.conn, fake_platform, plan)
    assert not u.problems and snapshot(tree) == before
    assert planner.load(store.conn, plan.id).status == "undone"  # type: ignore[union-attr]


def test_organize_moves_into_a_new_folder_and_undo(
    store: FileStore, home: Path, fake_platform: FakePlatform
) -> None:
    files = [put(home / "Downloads" / "bst notes.pdf", b"a" * 20_000),
             put(home / "Desktop" / "graphs.txt", b"b" * 20_000),
             put(home / "Documents" / "heaps.md", b"c" * 20_000)]  # fmt: skip
    put(home / "Documents" / "DSA" / "heaps.md", b"existing")  # name clash at the target
    scan(store)
    before = snapshot(home)
    ids = [r[0] for r in store.conn.execute(
        "SELECT id FROM files WHERE name IN ('bst notes.pdf', 'graphs.txt', 'heaps.md')"
        " AND path NOT LIKE '%DSA%'")]  # fmt: skip
    plan = planner.save(store.conn, planner.organize_plan(
        store.conn, store.config, ids, "DSA", target=str(home / "Documents" / "DSA" / "new")))  # fmt: skip
    assert len(plan.items) == 3 and all(i.op == "move" for i in plan.items)
    planner.set_status(store.conn, plan, "approved")
    res = executor.execute(store.conn, fake_platform, store.config, plan)
    assert res.done == 3 and all(not f.exists() for f in files)
    assert (home / "Documents" / "DSA" / "new" / "graphs.txt").exists()
    undo.undo(store.conn, fake_platform, plan)
    assert snapshot(home) == before  # files back, created folder removed


def test_organize_picks_the_shared_folder_and_never_overwrites(
    store: FileStore, home: Path, fake_platform: FakePlatform
) -> None:
    for n in ("a", "b", "c"):
        put(home / "Study" / "DSA" / f"{n}.md", n.encode() * 20_000)
    stray = put(home / "Downloads" / "a.md", b"stray" * 5000)
    scan(store)
    ids = [r[0] for r in store.conn.execute("SELECT id FROM files WHERE name LIKE '%.md'")]
    plan = planner.organize_plan(store.conn, store.config, ids, "DSA")
    (item,) = plan.items
    assert item.src == str(stray) and item.dst == str(home / "Study" / "DSA" / "a (2).md")
    planner.save(store.conn, plan)
    Path(item.dst).write_bytes(b"appeared")  # created after the plan
    planner.set_status(store.conn, plan, "approved")
    res = executor.execute(store.conn, fake_platform, store.config, plan)
    assert res.skipped == 1 and stray.exists() and "already exists" in item.result


def test_undo_reports_what_it_cannot_restore(
    store: FileStore, tree: Path, fake_platform: FakePlatform
) -> None:
    plan = _plan(store, categories=["duplicate"])
    planner.set_status(store.conn, plan, "approved")
    executor.execute(store.conn, fake_platform, store.config, plan)
    victim = plan.items[0].src
    put(Path(victim), b"something new")  # its old place is taken again
    u = undo.undo(store.conn, fake_platform, plan)
    assert u.restored == 1 and len(u.problems) == 1
    assert Path(victim).read_bytes() == b"something new"  # never overwritten


# -- the real OS trash ----------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform not in {"linux", "win32"}, reason="Linux/Windows trash")
def test_real_trash_and_restore(tmp_path: Path) -> None:
    from zachary.core.platform import trash

    f = put(tmp_path / "zachary trash test.txt", b"hello", mtime=time.time())
    d = tmp_path / "zachary trash dir"
    put(d / "inner.txt", b"x", mtime=time.time())
    for item in (f, d):
        where = trash.trash(item)
        assert not item.exists() and where and os.path.lexists(where)
        trash.restore(where, str(item))
        assert item.exists() and not os.path.lexists(where)
    assert f.read_bytes() == b"hello" and (d / "inner.txt").read_bytes() == b"x"


# -- protections learned from a real home folder -------------------------------------------
def test_installed_software_is_never_touched(store: FileStore, home: Path) -> None:
    app = home / "Documents" / "tools" / "agent"
    for i in range(12):  # bundled libraries: identical on purpose
        put(app / "lib" / f"mod{i}.jar", os.urandom(20_000))
    put(app / "lib" / "copy.jar", (app / "lib" / "mod0.jar").read_bytes())
    put(app / "docs" / "manual.pdf", BIG)  # a "user" kind, still inside the app
    put(home / "Documents" / "manual.pdf", BIG)
    (app / "conf").mkdir()  # empty folder the app needs
    scan(store)
    plan = planner.clean_plan(store.conn, store.config)
    assert not any(str(app) in i.src for i in plan.items)
    software = planner.Guard(store.conn, store.config).software
    assert software == [os.path.normcase(str(home / "Documents" / "tools"))]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin rights on Windows")
def test_symlinks_and_hardlinks_are_not_duplicates(store: FileStore, home: Path) -> None:
    orig = put(home / "Documents" / "photo.jpg", BIG)
    os.symlink(orig, home / "Documents" / "photo-link.jpg")
    os.link(orig, home / "Pictures-hard.jpg")
    scan(store)
    plan = planner.clean_plan(store.conn, store.config, categories=["duplicate"])
    assert plan.items == []


def test_code_and_binaries_are_not_duplicates(store: FileStore, home: Path) -> None:
    put(home / "a" / "LICENSE.txt", BIG)  # a document: a real duplicate
    put(home / "b" / "LICENSE.txt", BIG)
    put(home / "a" / "util.py", BIG)  # code
    put(home / "b" / "util.py", BIG)
    scan(store)
    plan = planner.clean_plan(store.conn, store.config, categories=["duplicate"])
    assert [os.path.basename(i.src) for i in plan.items] == ["LICENSE.txt"]


# -- through the assistant -------------------------------------------------------------------------
def test_handler_clean_is_a_plan_then_approve_and_undo(
    store: FileStore, tree: Path, fake_platform: FakePlatform
) -> None:
    from zachary.actions.safe_actions import SafeActions
    from zachary.brain.handler import Handler

    h = Handler(store.conn, SafeActions(store.conn, fake_platform), store.config)
    before = snapshot(tree)
    assert h.preview("clean up my downloads").plan is None  # typing never scans
    r = h.submit("find duplicates")
    assert r.plan is not None and r.plan.status == "proposed" and r.plan_id
    assert {i.category for i in r.plan.items} == {"duplicate"}
    assert "nothing changes until you approve" in r.say and snapshot(tree) == before
    assert h.submit("yes").say == "Nothing to confirm."  # "yes" never approves a plan
    assert snapshot(tree) == before
    done = h.approve_plan(r.plan_id, {r.plan.items[0].id: False})
    assert done.say.startswith("1 done") and len(fake_platform.trashed) == 1
    assert h.approve_plan(r.plan_id).say.startswith("That plan is no longer")  # only once
    u = h.submit("undo")
    assert u.say.startswith("Undo “Clean up”") and len(fake_platform.trashed) == 1
    assert h.submit("yes").say.startswith("Undid 1 change") and snapshot(tree) == before


def test_handler_organize_topic_or_search(
    store: FileStore, home: Path, fake_platform: FakePlatform
) -> None:
    from zachary.actions.safe_actions import SafeActions
    from zachary.brain.handler import Handler

    put(home / "Downloads" / "dsa linked list notes.pdf", b"a" * 20_000)
    put(home / "Desktop" / "dsa graphs.pdf", b"b" * 20_000)
    scan(store)
    h = Handler(store.conn, SafeActions(store.conn, fake_platform), store.config)
    r = h.submit("organize my dsa files")
    assert r.plan is not None and r.plan.kind == "organize" and r.plan.items
    assert all(i.op == "move" and i.dst for i in r.plan.items)
    assert not fake_platform.trashed and (home / "Desktop" / "dsa graphs.pdf").exists()
    h.reject_plan(r.plan_id)
    assert planner.load(store.conn, r.plan_id).status == "rejected"  # type: ignore[union-attr]


def test_rules_match_regardless_of_case(
    store: FileStore, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows stores folder rules normcased (lower case) while file paths keep their
    spelling: never_touch must still match (seen on Windows CI)."""
    monkeypatch.setattr(planner, "_nc", lambda p: p.lower())
    store.conn.execute("UPDATE folder_rules SET path = lower(path)")
    plan = planner.clean_plan(store.conn, store.config, categories=["duplicate"])
    srcs = {os.path.relpath(i.src, tree) for i in plan.items}
    assert not any(s.startswith("Keep") for s in srcs)
    assert os.path.join("Documents", "report.pdf") not in srcs  # the original is kept

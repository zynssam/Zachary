from __future__ import annotations

import os
import time
from pathlib import Path

from tests.fakes import write
from zachary.indexer import projects
from zachary.indexer.projects import list_projects, select_projects
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore

S = os.sep


def p(*parts: str) -> str:
    return S + S.join(parts)


def test_select_projects_nesting_rule() -> None:
    cands = {
        p("h", "app"): {".git", "package.json"},
        p("h", "app", "frontend"): {"package.json"},  # nested, no .git -> not a project
        p("h", "app", "vendor", "lib"): {".git"},  # nested with own .git -> project
        p("h", "app", "vendor", "lib", "sub"): {"Makefile"},
        p("h", "tools"): {"Makefile"},
        p("h", "empty"): set(),
    }
    got = select_projects(cands, S)
    assert set(got) == {p("h", "app"), p("h", "app", "vendor", "lib"), p("h", "tools")}


def test_select_projects_respects_outer() -> None:
    got = select_projects({p("h", "app", "x"): {"setup.py"}}, S, outer=[p("h", "app")])
    assert got == {}


def test_is_marker() -> None:
    assert projects.is_marker("pyproject.toml")
    assert projects.is_marker("Thing.sln")
    assert projects.is_marker("App.csproj")
    assert not projects.is_marker("README.md")


def _mk_project(root: Path, name: str, marker: str, mtime: float) -> None:
    write(root / name / marker, mtime=mtime)
    write(root / name / "src" / "main.txt", mtime=mtime)


def test_projects_ordered_by_latest_activity(store: FileStore, home: Path) -> None:
    now = time.time()
    _mk_project(home / "code", "old-site", "package.json", now - 86400 * 60)
    _mk_project(home / "code", "zachary", "pyproject.toml", now - 3600)
    _mk_project(home / "college", "dsa", "Makefile", now - 86400 * 3)
    write(home / "code" / "zachary" / ".git" / "HEAD")
    # node_modules activity must not count (it's excluded)
    write(home / "code" / "old-site" / "node_modules" / "x.js", mtime=now)
    st = scan(store)
    projects.apply_markers(store, st.markers, scope=None)
    got = list_projects(store.conn)
    assert [x.name for x in got] == ["zachary", "dsa", "old-site"]
    assert got[0].markers == [".git", "pyproject.toml"]
    assert abs(got[2].last_activity - (now - 86400 * 60)) < 2


def test_touch_raises_activity(store: FileStore, home: Path) -> None:
    now = time.time()
    _mk_project(home, "a", "Makefile", now - 1000)
    _mk_project(home, "b", "Makefile", now - 500)
    st = scan(store)
    projects.apply_markers(store, st.markers, scope=None)
    assert [x.name for x in list_projects(store.conn)] == ["b", "a"]
    f = home / "a" / "src" / "main.txt"
    row = store.get_row(str(f))
    assert row is not None
    projects.touch(store, [(row.id, str(f), now)])
    assert [x.name for x in list_projects(store.conn)] == ["a", "b"]


def test_refresh_folder_detects_new_and_removed_marker(store: FileStore, home: Path) -> None:
    write(home / "work" / "notes.txt")
    write(home / "work" / "inner" / "package.json")
    st = scan(store)
    projects.apply_markers(store, st.markers, scope=None)
    assert [x.name for x in list_projects(store.conn)] == ["inner"]
    write(home / "work" / "Makefile")  # outer becomes a project -> inner (no .git) hidden
    projects.refresh_folders(store, [str(home / "work")])
    assert [x.name for x in list_projects(store.conn)] == ["work"]
    (home / "work" / "Makefile").unlink()  # and back
    projects.refresh_folders(store, [str(home / "work")])
    assert [x.name for x in list_projects(store.conn)] == ["inner"]

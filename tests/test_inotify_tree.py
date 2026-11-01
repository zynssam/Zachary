"""The Linux exclude-aware inotify watcher: watches only indexed folders."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from tests.fakes import wait_until, write
from zachary.indexer.store import FileStore

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux inotify")


@pytest.fixture
def tree_and_events(store: FileStore, home: Path):  # type: ignore[no-untyped-def]
    from zachary.core.platform.linux_inotify import InotifyTree

    events: list[str] = []
    lock = threading.Lock()

    def on_paths(paths: list[str]) -> None:
        with lock:
            events.extend(paths)

    t = InotifyTree(store.is_excluded, on_paths)
    yield t, events, home
    t.stop()


def test_watches_only_indexed_folders(tree_and_events) -> None:  # type: ignore[no-untyped-def]
    t, _events, home = tree_and_events
    for d in ["docs", "docs/sem5", "code/app/src"]:
        (home / d).mkdir(parents=True, exist_ok=True)
    for d in ["code/app/node_modules/a/b", "code/app/.git/objects", ".cache/x/y", ".config/z"]:
        (home / d).mkdir(parents=True, exist_ok=True)
    t.add_tree(str(home))
    # home, docs, docs/sem5, code, code/app, code/app/src: excluded subtrees get no watch.
    assert t.watch_count == 6
    assert not t.limit_hit


def test_events_new_folders_and_renames(tree_and_events) -> None:  # type: ignore[no-untyped-def]
    t, events, home = tree_and_events
    (home / "docs").mkdir()
    t.add_tree(str(home))
    t.start()
    f = write(home / "docs" / "a.txt")
    wait_until(lambda: str(f) in events)
    # A new folder gets a watch, and files inside it are reported.
    (home / "new" / "deep").mkdir(parents=True)
    g = write(home / "new" / "deep" / "b.txt")
    wait_until(lambda: str(g) in events)
    before = t.watch_count
    # Renaming a folder keeps its watches (paths rewritten), events use the new path.
    os.rename(home / "new", home / "renamed")
    h = write(home / "renamed" / "deep" / "c.txt")
    wait_until(lambda: str(h) in events)
    assert t.watch_count == before
    # Excluded folders created later get no watch.
    (home / "proj" / "node_modules" / "x").mkdir(parents=True)
    write(home / "proj" / "node_modules" / "x" / "i.js")
    wait_until(lambda: str(home / "proj") in events)
    assert not any("node_modules" + os.sep + "x" in e for e in events)
    # Deleting a folder drops its watches.
    n = t.watch_count
    import shutil

    shutil.rmtree(home / "renamed")
    wait_until(lambda: t.watch_count == n - 2)


def test_folder_moved_out_drops_watches(tree_and_events, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    t, _events, home = tree_and_events
    (home / "out" / "sub").mkdir(parents=True)
    t.add_tree(str(home))
    t.start()
    n = t.watch_count
    os.rename(home / "out", tmp_path / "elsewhere")
    wait_until(lambda: t.watch_count == n - 2)

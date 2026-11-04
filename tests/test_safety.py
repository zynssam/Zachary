"""Hard limits: no system folder is ever changed, programs are never run by "open",
files are only created in allowed places."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.fakes import FakePlatform, write
from zachary.actions import planner
from zachary.actions.safe_actions import SafeActions
from zachary.brain.context import Item
from zachary.brain.handler import Handler
from zachary.core import safety
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore


@pytest.mark.parametrize("name", ["setup.exe", "install.sh", "Tool.AppImage", "evil.desktop",
                                  "x.msi", "app.jar", "pkg.deb"])  # fmt: skip
def test_program_files_are_runnable(name: str) -> None:
    assert safety.runnable(name)


@pytest.mark.parametrize("name", ["run.bat", "a.ps1", "tool.py", "x.vbs"])
def test_scripts_are_runnable_on_windows_only(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety, "_PLATFORM", "win32")
    assert safety.runnable(name)
    monkeypatch.setattr(safety, "_PLATFORM", "linux")
    assert not safety.runnable(name)  # Linux: opens in an editor


@pytest.mark.parametrize("name", ["notes.pdf", "receipt.png", "report.docx", "song.mp3"])
def test_documents_are_not_runnable(name: str) -> None:
    assert not safety.runnable(name)


@pytest.mark.skipif(sys.platform == "win32", reason="Linux paths")
@pytest.mark.parametrize("path", ["/", "/etc", "/etc/passwd", "/usr/bin", "/boot/efi", "/root"])
def test_system_folders(path: str) -> None:
    assert safety.is_system(path)


def test_home_and_index_roots_are_not_system(store: FileStore, home: Path) -> None:
    assert not safety.is_system(str(home / "Documents"), store.config)  # (an index root)
    assert not safety.is_system(os.path.expanduser("~/Documents/x"))
    if sys.platform != "win32":
        assert safety.is_system("/tmp/other", store.config) is (
            not str(home).startswith("/tmp/other")
        )


def test_open_never_runs_a_program(store: FileStore, home: Path, fake_platform: FakePlatform
                                   ) -> None:  # fmt: skip
    write(home / "Downloads" / "setup-tool.exe")
    write(home / "Downloads" / "report.pdf")
    scan(store)
    h = Handler(store.conn, SafeActions(store.conn, fake_platform), store.config)
    r = h.submit("open setup tool")
    assert not fake_platform.opened  # never executed
    assert fake_platform.revealed == [home / "Downloads" / "setup-tool.exe"]
    assert "program file" in r.say
    h.submit("open report pdf")
    assert fake_platform.opened == [home / "Downloads" / "report.pdf"]


def test_open_script_in_an_editor_is_fine(store: FileStore, home: Path,
                                          fake_platform: FakePlatform) -> None:  # fmt: skip
    write(home / "code" / "tool" / "build.sh")
    scan(store)
    acts = SafeActions(store.conn, fake_platform)
    row = store.conn.execute("SELECT id, path FROM files WHERE name = 'build.sh'").fetchone()
    item = Item(int(row[0]), row[1], "build.sh", "code", False, 0.0)
    assert not acts.open(item)  # running it: no
    assert acts.open_with("code.desktop", item)  # editing it: yes


def test_files_are_only_created_in_allowed_places(store: FileStore, home: Path) -> None:
    cfg = store.config
    assert safety.creation_problem(home / "Documents" / "Zachary", cfg) is None
    assert safety.creation_problem(home / ".config", cfg) == "it's a hidden folder"
    if sys.platform != "win32":
        assert safety.creation_problem(Path("/etc"), cfg) == "that's a system folder"
        assert "outside" in str(safety.creation_problem(Path("/mnt/elsewhere"), cfg))
    cfg.add_folder_rule(home / "Keep", "never_touch")
    assert safety.creation_problem(home / "Keep" / "sub", cfg) == "it's a never-touch folder"


def test_create_refuses_a_never_touch_folder(store: FileStore, home: Path,
                                             fake_platform: FakePlatform) -> None:  # fmt: skip
    (home / "Keep").mkdir()
    write(home / "Keep" / "a.txt")
    scan(store)
    store.config.add_folder_rule(home / "Keep", "never_touch")
    h = Handler(store.conn, SafeActions(store.conn, fake_platform), store.config)
    r = h.submit("create a text file called notes in keep folder")
    assert (
        r.say.startswith("I won't create files in") and not (home / "Keep" / "notes.txt").exists()
    )


def test_names_cannot_escape_the_folder(store: FileStore, home: Path, monkeypatch: pytest.MonkeyPatch,
                                        fake_platform: FakePlatform) -> None:  # fmt: skip
    from zachary.actions import creator

    base = home / "Documents" / "Zachary"
    monkeypatch.setattr(creator, "base_folder", lambda: base)
    h = Handler(store.conn, SafeActions(store.conn, fake_platform), store.config)
    h.submit("create a text file called ../../evil saying hi")
    h.submit("create a folder called ../../../etc")
    made = [p for p in home.rglob("*") if "evil" in p.name or p.name == "etc"]
    assert made and all(str(p).startswith(str(base)) for p in made)


@pytest.mark.skipif(sys.platform == "win32", reason="Linux paths")
def test_clean_up_never_touches_system_folders(store: FileStore) -> None:
    guard = planner.Guard(store.conn, store.config)
    for p in ("/etc/hosts", "/usr/lib/libx.so", "/", "/boot/vmlinuz"):
        assert guard.protected(p)

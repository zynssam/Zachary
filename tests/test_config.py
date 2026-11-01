from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from zachary.core.config import Config, ExcludeMatcher, Settings, default_db_path
from zachary.core.platform import RecentItem, WatchLimit


class FakePlatform:
    name = "fake"
    case_insensitive_paths = False

    def __init__(self, root: Path) -> None:
        self.root = root

    def data_dir(self) -> Path:
        return self.root / "data"

    def default_index_roots(self) -> list[Path]:
        return [self.root / "home"]

    def system_excludes(self) -> list[str]:
        return [str(self.root / "sys")]

    def recent_items(self) -> Iterator[RecentItem]:
        return iter(())

    def open_path(self, path: Path) -> None: ...

    def reveal_in_file_manager(self, path: Path) -> None: ...

    def check_watch_limit(self, needed: int) -> WatchLimit:
        return WatchLimit(True, None, None, "")

    def lower_process_priority(self) -> None: ...


@pytest.fixture
def cfg(conn: sqlite3.Connection, tmp_path: Path) -> Config:
    c = Config(conn, FakePlatform(tmp_path))
    c.seed_defaults()
    return c


def test_settings_defaults_and_roundtrip(cfg: Config) -> None:
    assert cfg.load() == Settings()
    cfg.set_setting("indexing_paused", True)
    cfg.set_setting("quickbar_hotkey", "ctrl+space")
    s = cfg.load()
    assert s.indexing_paused is True
    assert s.quickbar_hotkey == "ctrl+space"


def test_settings_validation(cfg: Config) -> None:
    with pytest.raises(KeyError):
        cfg.set_setting("nope", 1)
    with pytest.raises(TypeError):
        cfg.set_setting("indexing_paused", "yes")


def test_index_roots_default_then_custom(cfg: Config, tmp_path: Path) -> None:
    assert cfg.index_roots() == [tmp_path / "home"]
    cfg.set_index_roots([tmp_path / "a", tmp_path / "b" / ".." / "a", tmp_path / "c"])
    assert cfg.index_roots() == [tmp_path / "a", tmp_path / "c"]


def test_seed_is_idempotent_and_respects_disabled(cfg: Config, conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT count(*) FROM exclude_rules").fetchone()[0]
    cfg.set_exclude_enabled("node_modules", "dirname", False)
    cfg.seed_defaults()
    assert conn.execute("SELECT count(*) FROM exclude_rules").fetchone()[0] == n
    assert not cfg.exclude_matcher().excludes_name("node_modules", is_dir=True)


def test_default_excludes(cfg: Config, tmp_path: Path) -> None:
    m = cfg.exclude_matcher()
    home = tmp_path / "home"
    assert m.is_excluded(home / "proj" / "node_modules" / "x.js", is_dir=False)
    assert m.is_excluded(home / "proj" / ".git" / "HEAD", is_dir=False)
    assert m.is_excluded(home / "proj" / "__pycache__", is_dir=True)
    assert m.is_excluded(home / "~$report.docx", is_dir=False)
    assert m.is_excluded(home / "movie.mkv.part", is_dir=False)
    assert m.is_excluded(home / "a.crdownload", is_dir=False)
    assert m.is_excluded(tmp_path / "sys" / "kernel", is_dir=True)
    assert not m.is_excluded(home / "proj" / "main.py", is_dir=False)
    assert not m.is_excluded(home / "Fee Receipt.pdf", is_dir=False)
    # A *file* named like an excluded dir is not excluded.
    assert not m.is_excluded(home / "build", is_dir=False)
    # Path prefix must match on a boundary.
    assert not m.is_excluded(tmp_path / "system-notes.txt", is_dir=False)


def test_user_excludes(cfg: Config, tmp_path: Path) -> None:
    private = tmp_path / "home" / "Private"
    cfg.add_exclude(private)
    cfg.add_exclude("*.log", "glob")
    m = cfg.exclude_matcher()
    assert m.is_excluded(private / "diary.txt", is_dir=False)
    assert m.is_excluded(tmp_path / "home" / "app.log", is_dir=False)
    cfg.remove_exclude(private)
    assert not cfg.exclude_matcher().is_excluded(private / "diary.txt", is_dir=False)


def test_case_insensitive_matcher() -> None:
    m = ExcludeMatcher.build(["node_modules"], ["*.TMP"], [], case_insensitive=True)
    assert m.excludes_name("Node_Modules", is_dir=True)
    assert m.excludes_name("x.tmp", is_dir=False)


def test_folder_rules_inherit(cfg: Config, tmp_path: Path) -> None:
    projects = tmp_path / "home" / "Projects"
    cfg.add_folder_rule(projects, "never_touch")
    cfg.add_folder_rule(tmp_path / "home" / "Downloads", "auto_tidy")
    assert cfg.folder_rules_for(projects / "zach" / "main.py") == {"never_touch"}
    assert cfg.folder_rules_for(tmp_path / "home" / "Downloads" / "x.exe") == {"auto_tidy"}
    assert cfg.folder_rules_for(tmp_path / "home" / "Documents") == set()
    with pytest.raises(ValueError):
        cfg.add_folder_rule(projects, "delete_everything")  # type: ignore[arg-type]
    cfg.remove_folder_rule(projects, "never_touch")
    assert cfg.folder_rules_for(projects) == set()


def test_default_db_path_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ZACHARY_DB", str(tmp_path / "x.db"))
    assert default_db_path() == tmp_path / "x.db"
    monkeypatch.delenv("ZACHARY_DB")
    assert default_db_path(FakePlatform(tmp_path)) == tmp_path / "data" / "zachary.db"

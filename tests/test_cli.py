from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.fakes import write
from zachary.cli import main
from zachary.core.config import Config
from zachary.core.db import open_db


@pytest.fixture
def cli_db(tmp_path: Path, home: Path) -> str:
    now = time.time()
    write(home / "College" / "Fee Receipt Sem 5.pdf", mtime=now - 86400 * 30)
    write(home / "code" / "zach" / "pyproject.toml", mtime=now - 60)
    write(home / "code" / "old" / "Makefile", mtime=now - 86400 * 90)
    db = str(tmp_path / "cli.db")
    conn = open_db(db)
    Config(conn).set_index_roots([home])
    conn.close()
    assert main(["--db", db, "scan"]) == 0
    return db


def test_scan_search_projects_recent(cli_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", cli_db, "scan"]) == 0
    out = capsys.readouterr().out
    assert "0 deleted" in out and "index: 8 entries (5 folders)" in out
    assert main(["--db", cli_db, "search", "fee", "receipt"]) == 0
    out = capsys.readouterr().out
    assert "Fee Receipt Sem 5.pdf" in out and "ms" in out
    assert main(["--db", cli_db, "search", "--json", "fees"]) == 0
    assert '"results"' in capsys.readouterr().out
    assert main(["--db", cli_db, "projects"]) == 0
    out = capsys.readouterr().out
    assert out.index("zach") < out.index("old")
    assert main(["--db", cli_db, "recent"]) == 0
    assert "pyproject.toml" in capsys.readouterr().out
    assert main(["--db", cli_db, "recent", "last", "week"]) in (0, 1)
    assert main(["--db", cli_db, "search", "nothing-like-this-xyz"]) == 1


def test_forget_hides_immediately(
    cli_db: str, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--db", cli_db, "forget", str(home / "College")]) == 0
    capsys.readouterr()
    main(["--db", cli_db, "search", "fee", "receipt"])
    assert "Fee Receipt" not in capsys.readouterr().out
    # The next indexer pass purges it from the DB for good.
    main(["--db", cli_db, "scan"])
    conn = open_db(cli_db)
    n = conn.execute("SELECT count(*) FROM files WHERE name LIKE 'Fee Receipt%'").fetchone()[0]
    assert n == 0


def test_config_commands(cli_db: str, home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", cli_db, "exclude", "add", "*.log"]) == 0
    assert main(["--db", cli_db, "exclude", "remove", "build"]) == 0  # disables a default
    assert main(["--db", cli_db, "exclude"]) == 0
    out = capsys.readouterr().out
    assert "*.log" in out and "build" in out and "(disabled)" in out
    assert main(["--db", cli_db, "pause"]) == 0
    assert main(["--db", cli_db, "status"]) == 0
    assert "paused:    True" in capsys.readouterr().out
    assert main(["--db", cli_db, "resume"]) == 0
    assert main(["--db", cli_db, "roots"]) == 0
    assert str(home) in capsys.readouterr().out


def test_set_command(cli_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", cli_db, "set"]) == 0
    assert "index_hidden_folders = false" in capsys.readouterr().out
    assert main(["--db", cli_db, "set", "index_hidden_folders", "true"]) == 0
    assert main(["--db", cli_db, "set", "index_hidden_folders"]) == 0
    assert capsys.readouterr().out.strip().endswith("true")
    assert main(["--db", cli_db, "set", "quickbar_hotkey", "ctrl+space"]) == 0

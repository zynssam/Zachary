from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fakes import FakePlatform, wait_until, write
from zachary.core.config import Config
from zachary.core.db import open_db
from zachary.core.platform import WatchLimit
from zachary.indexer.embedder import HashEmbedder
from zachary.indexer.service import IndexerService


@pytest.fixture
def running(
    tmp_path: Path, home: Path, fake_platform: FakePlatform
) -> Iterator[tuple[IndexerService, Config]]:
    write(home / "docs" / "a.txt")
    write(home / "private" / "diary.txt")
    db = tmp_path / "svc.db"
    ctl = open_db(db)  # the "CLI": a second connection to the same DB
    cfg = Config(ctl, fake_platform)
    cfg.set_index_roots([home])
    svc = IndexerService(
        db,
        fake_platform,
        debounce=0.2,
        poll_interval=0.1,
        lower_priority=False,
        embedder_factory=lambda _c: HashEmbedder(),
    )
    t = threading.Thread(target=svc.run, daemon=True)
    t.start()
    assert svc.ready.wait(10)
    assert not svc.failed
    yield svc, cfg
    svc.stop()
    t.join(10)
    ctl.close()


def _has(cfg: Config, path: Path) -> bool:
    row = cfg.conn.execute("SELECT 1 FROM files WHERE path = ?", (cfg.canonical(path),))
    return row.fetchone() is not None


def test_service_catchup_watch_forget_pause_resume(
    running: tuple[IndexerService, Config], home: Path
) -> None:
    svc, cfg = running
    assert _has(cfg, home / "docs" / "a.txt")
    assert svc.watching
    # live update
    write(home / "docs" / "b.txt")
    wait_until(lambda: _has(cfg, home / "docs" / "b.txt"))
    # forget from another process -> indexer purges
    cfg.add_exclude(home / "private")
    wait_until(lambda: not _has(cfg, home / "private" / "diary.txt"))
    wait_until(lambda: svc.watching)  # reconfigure (purge + catch-up + restart) finished
    # pause stops watching; changes while paused are found on resume
    cfg.set_setting("indexing_paused", True)
    wait_until(lambda: not svc.watching)
    write(home / "docs" / "while-paused.txt")
    import time

    time.sleep(0.5)
    assert not _has(cfg, home / "docs" / "while-paused.txt")
    cfg.set_setting("indexing_paused", False)
    wait_until(lambda: _has(cfg, home / "docs" / "while-paused.txt"))
    wait_until(lambda: svc.watching)
    # heartbeat for `status`
    wait_until(
        lambda: cfg.conn.execute("SELECT 1 FROM state WHERE key = 'indexer.heartbeat'").fetchone()
    )


def test_service_reports_low_watch_limit(
    tmp_path: Path, home: Path, fake_platform: FakePlatform
) -> None:
    fake_platform.watch_limit = WatchLimit(False, 10, 524288, "raise the limit")
    db = tmp_path / "svc2.db"
    ctl = open_db(db)
    Config(ctl, fake_platform).set_index_roots([home])
    svc = IndexerService(db, fake_platform, poll_interval=0.1, lower_priority=False, semantic=False)
    t = threading.Thread(target=svc.run, daemon=True)
    t.start()
    assert svc.ready.wait(10)
    svc.stop()
    t.join(10)
    row = ctl.execute("SELECT value FROM state WHERE key = 'indexer.watch_limit_message'")
    assert "raise the limit" in row.fetchone()[0]
    ctl.close()


def test_service_embeds_in_background(running: tuple[IndexerService, Config], home: Path) -> None:
    svc, cfg = running
    assert svc.semantic is not None
    svc.semantic.idle_wait = 0.2
    svc.semantic.nudge()

    def embedded() -> int:
        return int(
            cfg.conn.execute(
                "SELECT count(*) FROM files WHERE is_dir = 0 AND embedded_at IS NOT NULL"
            ).fetchone()[0]
        )

    wait_until(lambda: embedded() >= 2, timeout=15)
    # A new file is embedded soon after it appears (the watcher nudges the worker).
    write(home / "docs" / "linked list notes.txt", "a linked list is a chain of nodes")
    wait_until(
        lambda: (
            cfg.conn.execute(
                "SELECT embedded_at FROM files WHERE name = 'linked list notes.txt'"
            ).fetchone()
            not in (None, (None,))
        ),
        timeout=15,
    )

from __future__ import annotations

import os

# Qt tests run headless everywhere (CI and local) unless a platform is forced.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fakes import FakePlatform
from zachary.core.config import Config
from zachary.core.db import open_db
from zachary.indexer.store import FileStore


@pytest.fixture(autouse=True)
def _no_real_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gnani and Anthropic are paid APIs: tests use mocks only. Any attempt to resolve
    the real hosts fails, and no real key is visible to the code under test."""
    import socket

    real = socket.getaddrinfo

    def guarded(host, *args, **kw):  # type: ignore[no-untyped-def]
        if isinstance(host, str) and host.endswith("vachana.ai"):
            raise OSError("tests must not call the real Gnani API")
        if isinstance(host, str) and host.endswith("anthropic.com"):
            raise OSError("tests must not call the real Anthropic API")
        return real(host, *args, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)
    monkeypatch.delenv("GNANI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("zachary.core.env.get_secret", lambda *_a, **_k: None)
    monkeypatch.setattr("zachary.voice.factory.get_secret", lambda *_a, **_k: None)
    monkeypatch.setattr("zachary.brain.llm.get_secret", lambda *_a, **_k: None)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "zachary.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_db(db_path)
    yield c
    c.close()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An index root. Real path (on Windows tmp dirs can be 8.3 short names)."""
    h = tmp_path / "home"
    h.mkdir()
    return Path(os.path.realpath(h))


@pytest.fixture
def fake_platform(tmp_path: Path) -> FakePlatform:
    return FakePlatform(tmp_path)


@pytest.fixture
def store(conn: sqlite3.Connection, home: Path, fake_platform: FakePlatform) -> FileStore:
    cfg = Config(conn, fake_platform)
    cfg.seed_defaults()
    cfg.set_index_roots([home])
    return FileStore(conn, cfg)

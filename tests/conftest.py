from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from zachary.core.db import open_db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "zachary.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_db(db_path)
    yield c
    c.close()

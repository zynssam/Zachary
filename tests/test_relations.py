from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.fakes import FakePlatform
from tests.semantic_corpus import build_corpus
from zachary.core.config import Config
from zachary.core.db import open_db
from zachary.core.platform import RecentItem
from zachary.indexer.embedder import HashEmbedder
from zachary.indexer.recent import ingest_recent
from zachary.indexer.relations import RelationBuilder, related
from zachary.indexer.scanner import scan
from zachary.indexer.semantic import SemanticIndexer
from zachary.indexer.store import FileStore

SYN = dict.fromkeys("linked list node tree binary search stack queue heap graph".split(), "dsa")


@pytest.fixture
def built(store: FileStore, home: Path, db_path: Path, fake_platform: FakePlatform) -> FileStore:
    files = build_corpus(home)
    scan(store)
    t = time.time() - 3600
    fake_platform.recent = [  # opened together
        RecentItem(files["goa trip.md"], t),
        RecentItem(files["paneer.txt"], t + 60),
    ]
    ingest_recent(store, fake_platform)
    conn = open_db(db_path)
    SemanticIndexer(conn, Config(conn, fake_platform), HashEmbedder(SYN)).run(64)
    RelationBuilder(store.conn).compute(None)
    return store


def _rel(store: FileStore, name: str) -> list[tuple[str, str]]:
    fid = store.conn.execute("SELECT id FROM files WHERE name = ?", (name,)).fetchone()[0]
    return [(r.name, r.reason) for r in related(store.conn, fid)]


def test_content_relations_cross_folders(built: FileStore) -> None:
    rel = dict(_rel(built, "ll.cpp"))
    # DSA notes live in another folder tree, years older: related by content
    assert "Unit2 notes.pdf" in rel and rel["Unit2 notes.pdf"] == "content"


def test_location_and_coaccess(built: FileStore) -> None:
    rel = dict(_rel(built, "goa trip.md"))
    assert "paneer.txt" in rel  # same folder + opened together
    assert rel["paneer.txt"] in {"location", "coaccess"}


def test_topk_and_lookup_speed(built: FileStore) -> None:
    fid = built.conn.execute("SELECT id FROM files WHERE name = 'll.cpp'").fetchone()[0]
    t0 = time.perf_counter()
    for _ in range(100):
        related(built.conn, fid)
    ms = (time.perf_counter() - t0) / 100 * 1e3
    assert ms < 10, ms
    assert len(related(built.conn, fid)) <= 10


def test_incremental_offers_new_file_to_neighbours(built: FileStore, home: Path) -> None:
    from tests.fakes import write

    new = write(
        home / "code" / "practice" / "stack.cpp", "stack and queue using a linked list node"
    )
    scan(built)
    conn = built.conn
    SemanticIndexer(conn, Config(conn, built.config.platform), HashEmbedder(SYN)).run(64)
    fid = conn.execute("SELECT id FROM files WHERE path = ?", (str(new),)).fetchone()[0]
    RelationBuilder(conn).compute([fid])
    assert "ll.cpp" in dict(_rel(built, "stack.cpp"))
    assert "stack.cpp" in dict(_rel(built, "ll.cpp"))  # symmetric update

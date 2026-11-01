"""Search by meaning, "why it matched", related files through the handler."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.fakes import FakePlatform
from tests.semantic_corpus import build_corpus
from zachary.actions.safe_actions import SafeActions
from zachary.brain.handler import Handler
from zachary.core.config import Config
from zachary.core.db import open_db
from zachary.indexer import relations, versions
from zachary.indexer.embedder import HashEmbedder, Model2VecEmbedder
from zachary.indexer.scanner import scan
from zachary.indexer.semantic import SemanticIndexer
from zachary.indexer.store import FileStore
from zachary.search import vectors
from zachary.search.hybrid import search
from zachary.search.vectors import SemanticSearch, VectorIndex

SYN = dict.fromkeys("data structures structure linked list node tree binary search stack queue heap graph".split(), "dsa")  # fmt: skip


def _index(store: FileStore, db_path: Path, emb: object) -> SemanticSearch:
    conn = open_db(db_path)
    SemanticIndexer(conn, Config(conn, store.config.platform), emb).run(64)  # type: ignore[arg-type]
    vectors.build(conn)
    relations.RelationBuilder(conn).compute(None)
    versions.rebuild(conn)
    return SemanticSearch(emb, VectorIndex(store.conn))  # type: ignore[arg-type]


@pytest.fixture
def sem_search(store: FileStore, home: Path, db_path: Path) -> SemanticSearch:
    build_corpus(home)
    scan(store)
    return _index(store, db_path, HashEmbedder(SYN))


def test_meaning_finds_old_notes_and_recent_code(
    store: FileStore, sem_search: SemanticSearch
) -> None:
    res = search(store.conn, "show me everything on data structures", semantic=sem_search)
    names = [r.name for r in res.results[:4]]
    assert "Unit2 notes.pdf" in names and "ll.cpp" in names  # old notes + recent code
    without = [r.name for r in search(store.conn, "data structures").results]
    assert "ll.cpp" not in without  # keywords alone can't find the code


def test_why_shows_matching_passage(store: FileStore, sem_search: SemanticSearch) -> None:
    res = search(store.conn, "binary search tree", semantic=sem_search)
    top = next(r for r in res.results if r.name == "Unit2 notes.pdf")
    assert any(w.startswith("about: “") for w in top.why)


def test_content_keywords_found_without_vectors(
    store: FileStore, sem_search: SemanticSearch
) -> None:
    names = [r.name for r in search(store.conn, "installment").results]
    assert names == ["receipt_sem5.pdf"]


def test_handler_related_follow_up(
    store: FileStore, sem_search: SemanticSearch, fake_platform: FakePlatform
) -> None:
    h = Handler(store.conn, SafeActions(store.conn, fake_platform), store.config,
                lambda: sem_search)  # fmt: skip
    h.submit("linked list code")
    r = h.submit("related to the first one")
    assert r.view == "related" and r.items
    assert any(i.name == "Unit2 notes.pdf" for i in r.items)
    r2 = h.submit("files related to binary search tree")
    assert r2.view == "related" and r2.items
    r3 = h.submit("open the first one")
    assert r3.action == "open" and r3.target is not None
    assert r3.target.name == r2.items[0].name


@pytest.mark.skipif(not os.environ.get("ZACHARY_TEST_MODEL"), reason="set ZACHARY_TEST_MODEL=1")
def test_real_model_acceptance(store: FileStore, home: Path, db_path: Path) -> None:
    """Phase 3 'done when' with the real Model2Vec model (downloads ~30 MB once)."""
    build_corpus(home)
    scan(store)
    models = Path(os.environ.get("ZACHARY_MODELS_DIR", Path.home() / ".cache" / "zachary-models"))
    emb = Model2VecEmbedder("minishlab/potion-base-8M", models)
    sem = _index(store, db_path, emb)
    names = [r.name for r in search(store.conn, "show me everything on data structures",
                                    semantic=sem).results[:5]]  # fmt: skip
    assert "Unit2 notes.pdf" in names and "ll.cpp" in names, names
    top = search(store.conn, "when is my fee due", semantic=sem).results[0]
    assert top.name == "receipt_sem5.pdf"
    trip = [r.name for r in search(store.conn, "holiday", semantic=sem).results[:3]]
    assert "goa trip.md" in trip, trip

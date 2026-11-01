"""Loading meaning search for the assistant / CLI (no Qt here)."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from zachary.core.config import Config
from zachary.indexer.embedder import Model2VecEmbedder
from zachary.search.vectors import SemanticSearch, VectorIndex

log = logging.getLogger(__name__)


def local_semantic(conn: sqlite3.Connection, config: Config) -> SemanticSearch | None:
    """Meaning search if the embedding model is already on disk. The assistant never
    downloads it (the indexer does); until then search is keyword + recency."""
    emb = Model2VecEmbedder(str(config.get_setting("embed_model")), _models_dir(conn))
    if not (emb.local_path() / "config.json").exists():
        return None
    try:
        emb.encode(["warm up"])
    except Exception:
        log.warning("embedding model could not be loaded", exc_info=True)
        return None
    return SemanticSearch(emb, VectorIndex(conn))


def _models_dir(conn: sqlite3.Connection) -> Path:
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    return Path(db_file).parent / "models"

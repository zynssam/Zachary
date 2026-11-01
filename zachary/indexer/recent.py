"""Ingest the OS's recently-used files into ``access_log`` (source ``os_recent``).

Linux: ``recently-used.xbel``. Windows: Recent Items. Atime is never used.
"""

from __future__ import annotations

from zachary.core.platform import Platform
from zachary.indexer.store import FileStore


def ingest_recent(store: FileStore, platform: Platform) -> int:
    """Add access_log rows for recent items Zachary has indexed. Returns rows added."""
    items = [(str(i.path), i.ts) for i in platform.recent_items()]
    return store.record_access(items, "os_recent")

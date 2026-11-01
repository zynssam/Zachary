"""``python -m zachary.indexer``: run the indexer daemon."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from zachary.core.config import default_db_path
from zachary.core.platform import get_platform
from zachary.indexer.service import IndexerService


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zachary.indexer", description="Zachary indexer daemon")
    ap.add_argument("--db", default=None, help="database path (default: per-user data dir)")
    ap.add_argument("--debounce", type=float, default=1.5, help="seconds a path must be quiet")
    ap.add_argument("--once", action="store_true", help="catch up, ingest recent files, exit")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db_path = Path(args.db) if args.db else default_db_path()
    # One indexer per database; a second start (e.g. from the assistant) exits quietly.
    lock = get_platform().try_lock(db_path.parent / (db_path.name + ".indexer.lock"))
    if lock is None and not args.once:
        logging.getLogger("zachary.indexer").info("indexer already running for %s", db_path)
        return 0
    svc = IndexerService(db_path, debounce=args.debounce)
    if args.once:
        svc.store.ensure_fts()
        svc.store.purge_excluded()
        svc._catch_up()
        svc._ingest_recent()
        svc.conn.close()
        return 0
    signal.signal(signal.SIGINT, lambda *_: svc.stop())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: svc.stop())
    svc.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

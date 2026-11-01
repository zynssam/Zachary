"""``python -m zachary.indexer``: run the indexer daemon."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from zachary.core.config import default_db_path
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
    svc = IndexerService(args.db or default_db_path(), debounce=args.debounce)
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

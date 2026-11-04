"""``python -m zachary [assistant|indexer|cli] ...`` (and the frozen executable).

With no part (or an option first) it starts the assistant: double-clicking the app opens
Zachary. ``zachary/launch.py`` builds these command lines.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    part = args.pop(0) if args and args[0] in {"assistant", "indexer", "cli"} else "assistant"
    if part == "indexer":
        from zachary.indexer.__main__ import main as run
    elif part == "cli":
        from zachary.cli import main as run
    else:
        from zachary.assistant import main as run
    return int(run(args) or 0)


if __name__ == "__main__":
    sys.exit(main())

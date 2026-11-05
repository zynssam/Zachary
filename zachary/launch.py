"""How Zachary starts its own parts (the indexer, ``--toggle``, autostart, the desktop
entry), from source or from a frozen (PyInstaller) build.

From source each part is ``python -m zachary.<part>``. In a frozen build there is one
executable and ``sys.executable`` is that executable, so the part is its first argument:
``Zachary indexer --db ...``. ``zachary/__main__.py`` dispatches it.
"""

from __future__ import annotations

import sys

PARTS = {"zachary.assistant": "assistant", "zachary.indexer": "indexer", "zachary.cli": "cli"}


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def self_command(module: str, *args: str) -> list[str]:
    """argv that starts ``module`` (``zachary.assistant`` / ``.indexer`` / ``.cli``)."""
    if module not in PARTS:
        raise ValueError(f"not a Zachary part: {module}")
    if frozen():
        return [sys.executable, PARTS[module], *args]
    return [sys.executable, "-m", module, *args]

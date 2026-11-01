"""Secrets from the environment or a ``.env`` file. Values are never logged."""

from __future__ import annotations

import os
from pathlib import Path


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def get_secret(name: str, search: list[Path] | None = None) -> str | None:
    """``name`` from the environment, else from the first ``.env`` that has it.

    Default search: the current directory, then Zachary's data directory.
    """
    value = os.environ.get(name)
    if value:
        return value
    if search is None:
        from zachary.core.platform import get_platform

        search = [Path.cwd() / ".env", get_platform().data_dir() / ".env"]
    for p in search:
        try:
            found = _parse(p.read_text(encoding="utf-8")).get(name)
        except OSError:
            continue
        if found:
            return found
    return None

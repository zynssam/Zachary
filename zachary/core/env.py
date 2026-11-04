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


SECRET_NAMES = ("GNANI_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY")


def secrets_file() -> Path:
    from zachary.core.platform import get_platform

    return get_platform().data_dir() / ".env"


def set_secret(name: str, value: str, path: Path | None = None) -> Path:
    """Save ``name=value`` in Zachary's own ``.env`` (first-run setup / settings).

    Other lines are kept; an empty value removes the key. The file is readable only by
    you (0600 on Linux; on Windows it lives in your own %LOCALAPPDATA%). The value is
    never logged.
    """
    if name not in SECRET_NAMES:
        raise ValueError(f"not a Zachary key: {name}")
    if any(c in value for c in "\r\n"):
        raise ValueError("a key can't contain line breaks")
    path = path or secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    kept = [
        ln for ln in lines if ln.strip().removeprefix("export ").partition("=")[0].strip() != name
    ]
    if value.strip():
        kept.append(f"{name}={value.strip()}")
    tmp = path.with_name(".env.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + ("\n" if kept else ""))
    os.replace(tmp, path)
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def has_secret(name: str) -> bool:
    return bool(get_secret(name))

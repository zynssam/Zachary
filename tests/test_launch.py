"""Starting Zachary's own parts from source and from a frozen (PyInstaller) build."""

from __future__ import annotations

import sys

import pytest

from zachary import __main__ as entry
from zachary.launch import self_command


def test_from_source_uses_python_dash_m() -> None:
    assert self_command("zachary.indexer", "--db", "x.db") == [
        sys.executable, "-m", "zachary.indexer", "--db", "x.db"]  # fmt: skip


def test_frozen_build_passes_the_part_to_its_own_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert self_command("zachary.assistant", "--toggle") == [
        sys.executable,
        "assistant",
        "--toggle",
    ]
    with pytest.raises(ValueError):
        self_command("os")  # only Zachary's parts


def test_entry_point_dispatches_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, list[str]]] = []
    import zachary.assistant
    import zachary.cli
    import zachary.indexer.__main__ as idx

    monkeypatch.setattr(zachary.cli, "main", lambda a: seen.append(("cli", a)) or 0)
    monkeypatch.setattr(idx, "main", lambda a: seen.append(("indexer", a)) or 0)
    monkeypatch.setattr(zachary.assistant, "main", lambda a: seen.append(("assistant", a)) or 0)
    entry.main(["cli", "status"])
    entry.main(["indexer", "--once"])
    entry.main(["--show"])  # no part: the assistant (double-clicking the app)
    assert seen == [("cli", ["status"]), ("indexer", ["--once"]), ("assistant", ["--show"])]

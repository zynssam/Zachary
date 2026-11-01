from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtNetwork import QLocalServer
from pytestqt.qtbot import QtBot

from tests.fakes import FakePlatform, wait_until
from zachary import assistant
from zachary.core.db import open_db, set_state


@pytest.fixture
def instance(monkeypatch: pytest.MonkeyPatch) -> str:
    name = f"-test-{os.getpid()}-{time.monotonic_ns()}"
    monkeypatch.setenv("ZACHARY_INSTANCE", name)
    return name


def test_send_to_running(qtbot: QtBot, instance: str) -> None:
    assert not assistant.send_to_running(b"toggle")  # nobody listening
    server = QLocalServer()
    assert server.listen(assistant.server_name())
    got: list[str] = []
    assistant.serve_commands(server, {b"toggle": lambda: got.append("toggle")})
    # The client blocks waiting for "ok", so run it off the GUI thread.
    import threading

    result: list[bool] = []
    t = threading.Thread(target=lambda: result.append(assistant.send_to_running(b"toggle")))
    t.start()
    qtbot.waitUntil(lambda: bool(result), timeout=5000)
    t.join()
    qtbot.waitUntil(lambda: bool(got), timeout=2000)  # the handler runs after the ack
    assert result == [True] and got == ["toggle"]
    server.close()


def test_ensure_indexer(tmp_path: Path) -> None:
    plat = FakePlatform(tmp_path)
    db = tmp_path / "z.db"
    open_db(db).close()
    assert assistant.ensure_indexer(plat, db)  # no heartbeat -> start it
    assert plat.spawned[-1][-3:] == ["zachary.indexer", "--db", str(db)]
    conn = open_db(db)
    set_state(conn, "indexer.heartbeat", time.time())
    conn.close()
    plat.spawned.clear()
    assert not assistant.ensure_indexer(plat, db)  # alive -> leave it
    assert plat.spawned == []


@pytest.fixture
def running_assistant(tmp_path: Path, instance: str) -> Iterator[subprocess.Popen[bytes]]:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", ZACHARY_INSTANCE=instance)
    db = tmp_path / "a.db"
    proc = subprocess.Popen(
        [sys.executable, "-m", "zachary.assistant", "--db", str(db), "--no-indexer",
         "--no-hotkey"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )  # fmt: skip
    yield proc
    proc.terminate()
    proc.wait(10)


def test_second_launch_toggles_first(
    qtbot: QtBot, running_assistant: subprocess.Popen[bytes], tmp_path: Path, instance: str
) -> None:
    # Wait until the first instance listens ("ping" is ignored by the server).
    wait_until(lambda: assistant.send_to_running(b"ping"), timeout=30, step=0.2)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", ZACHARY_INSTANCE=instance)
    t0 = time.perf_counter()
    r = subprocess.run(
        [sys.executable, "-m", "zachary.assistant", "--toggle", "--db", str(tmp_path / "a.db")],
        env=env, capture_output=True, timeout=30,
    )  # fmt: skip
    assert r.returncode == 0
    assert time.perf_counter() - t0 < 10  # handed off and exited, didn't start a second app
    assert running_assistant.poll() is None

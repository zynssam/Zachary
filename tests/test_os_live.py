"""Tests that call the real OS. Each runs only on its own platform (CI covers both)."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fakes import write
from zachary.core.config import Config
from zachary.indexer.catchup import catch_up, load_cursor
from zachary.indexer.store import FileStore

win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")


@win_only
def test_usn_journal_catchup_end_to_end(conn: sqlite3.Connection, home: Path) -> None:
    from zachary.core.platform.windows import WindowsPlatform

    plat = WindowsPlatform()
    if plat.journal_cursor(home) is None:
        pytest.skip("USN journal not readable on this volume/account")
    cfg = Config(conn, plat)
    cfg.seed_defaults()
    cfg.set_index_roots([home])
    store = FileStore(conn, cfg)
    write(home / "docs" / "old name.txt")
    st = catch_up(store, plat)
    assert st.method[str(home)] == "scan"
    old = home / "docs" / "old name.txt"
    row = store.get_row(store.norm(old))
    assert row is not None
    assert row.inode == os.stat(old).st_ino  # FRN == st_ino, which journal mapping relies on
    # Changes while "not running":
    new = home / "docs" / "new name.txt"
    os.rename(old, new)
    write(home / "docs" / "fresh.txt")
    cursor = load_cursor(store, str(home))
    assert cursor is not None
    got = plat.read_journal(home, cursor)
    assert got is not None
    names = ("old name.txt", "new name.txt", "fresh.txt", "docs")
    mine = [(r.frn, r.parent_frn, r.name, hex(r.reason)) for r in got[0] if r.name in names]
    docs = store.get_row(store.norm(home / "docs"))
    detail = (
        f"row inode={row.inode} docs inode={docs.inode if docs else None}"
        f" st_ino={os.stat(new).st_ino} total records={len(got[0])} mine={mine}"
    )
    st = catch_up(store, plat)
    assert st.method[str(home)] == "journal", st.method
    moved = store.get_row(store.norm(new))
    assert moved is not None and moved.id == row.id, detail
    assert store.get_row(store.norm(home / "docs" / "fresh.txt")) is not None
    assert store.get_row(store.norm(old)) is None


@win_only
def test_windows_recent_items_and_roots_smoke() -> None:
    from zachary.core.platform.windows import WindowsPlatform

    plat = WindowsPlatform()
    items = list(plat.recent_items())
    assert all(i.ts > 0 for i in items)
    roots = plat.default_index_roots()
    assert roots[0] == Path.home() and len(roots) >= 4


@win_only
def test_windows_lower_priority_in_subprocess() -> None:
    code = (
        "from zachary.core.platform.windows import WindowsPlatform;"
        "WindowsPlatform().lower_process_priority();print('ok')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ok"


@linux_only
def test_linux_lower_priority_in_subprocess() -> None:
    code = (
        "import os;from zachary.core.platform.linux import LinuxPlatform;"
        "b=os.nice(0);LinuxPlatform().lower_process_priority();print(os.nice(0)-b)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert int(out.stdout.strip()) in (0, 10)  # 0 if already at max niceness


@linux_only
def test_linux_watch_limit_reads_proc() -> None:
    from zachary.core.platform.linux import LinuxPlatform

    lim = LinuxPlatform().check_watch_limit(1)
    assert lim.ok and (lim.current is None or lim.current > 0)


@win_only
def test_usn_diagnostics(home: Path) -> None:
    """Which journal read works on this machine, and does it see our own changes?"""
    from zachary.core.platform import windows as w

    cur = w._usn_cursor(home)
    if cur is None:
        pytest.skip("USN journal not readable")
    a = write(home / "diag_a.txt")
    os.rename(a, home / "diag_b.txt")
    report: dict[str, object] = {}
    for label, code in (
        ("unprivileged", w._FSCTL_READ_UNPRIVILEGED_USN_JOURNAL),
        ("classic", w._FSCTL_READ_USN_JOURNAL),
    ):
        got = w._usn_read(home, cur, [code])
        if got is None:
            report[label] = None
            continue
        recs, nxt = got
        report[label] = (
            len(recs),
            nxt.next_usn - cur.next_usn,
            [(r.name, hex(r.reason)) for r in recs if r.name.startswith("diag_")],
            [r.name for r in recs][:12],
        )
    # Each read method must see our own create + rename, or catch-up silently misses changes.
    for label, v in report.items():
        assert isinstance(v, tuple) and v[2], f"{label}: cursor={cur} report={report}"

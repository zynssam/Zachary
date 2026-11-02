"""Quick bar + engine with real Qt widgets (offscreen), a real DB and a fake OS."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt, QTimer
from pytestqt.qtbot import QtBot

from tests.fakes import FakePlatform, write
from zachary.brain.handler import Reply
from zachary.indexer import projects
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore
from zachary.ui.engine import Engine
from zachary.ui.quickbar import QuickBar

OLD = time.time() - 86400 * 200


@pytest.fixture
def bar(
    qtbot: QtBot, store: FileStore, home: Path, db_path: Path, fake_platform: FakePlatform
) -> Iterator[QuickBar]:
    write(home / "College" / "Fee Receipt Sem 5.pdf", mtime=OLD)
    write(home / "College" / "Hostel Fees.xlsx", mtime=OLD)
    for i in (1, 2, 3):
        write(home / "Work" / f"report_v{i}.docx", mtime=OLD + i * 86400)
    write(home / "code" / "zachary" / "pyproject.toml")
    st = scan(store)
    projects.apply_markers(store, st.markers, scope=None)
    engine = Engine(db_path, fake_platform)
    engine.start()
    b = QuickBar(engine)
    qtbot.addWidget(b)
    with qtbot.waitSignal(engine.replied, timeout=5000):  # warm-up, like the app does
        engine.ask("preview", "")
    yield b
    b.hide()
    engine.stop()


def _type(qtbot: QtBot, bar: QuickBar, text: str) -> Reply:
    bar.edit.clear()
    with qtbot.waitSignal(bar.reply_applied, timeout=5000) as blocker:
        qtbot.keyClicks(bar.edit, text)
    reply: Reply = blocker.args[0]
    # Several previews may land while typing; wait for the one for the full text.
    qtbot.waitUntil(lambda: bar.list.results.rowCount() >= 0 and not bar._debounce.isActive())
    qtbot.wait(60)
    return reply


def _enter(qtbot: QtBot, bar: QuickBar, mods: Any = Qt.KeyboardModifier.NoModifier) -> Reply:
    with qtbot.waitSignal(bar.reply_applied, timeout=5000) as blocker:
        qtbot.keyClick(bar.edit, Qt.Key.Key_Return, mods)
    return blocker.args[0]  # type: ignore[no-any-return]


def test_open_shows_results_under_150ms(qtbot: QtBot, bar: QuickBar) -> None:
    times = []
    for _ in range(20):
        bar.hide()
        qtbot.wait(5)
        with qtbot.waitSignal(bar.opened, timeout=2000) as blocker:
            bar.show_bar(t0=time.perf_counter())
        times.append(blocker.args[0])
    times.sort()
    assert times[int(0.95 * (len(times) - 1))] < 150, times


def test_type_and_enter_opens_clear_match(
    qtbot: QtBot, bar: QuickBar, fake_platform: FakePlatform
) -> None:
    bar.show_bar()
    _type(qtbot, bar, "open fee receipt")
    r = _enter(qtbot, bar)
    assert r.say == "Opening Fee Receipt Sem 5.pdf."
    assert fake_platform.opened[-1].name == "Fee Receipt Sem 5.pdf"
    assert bar.status.text() == r.say
    qtbot.waitUntil(lambda: not bar.isVisible(), timeout=2000)  # hides after acting


def test_follow_up_flow(qtbot: QtBot, bar: QuickBar, fake_platform: FakePlatform) -> None:
    bar.show_bar()
    _type(qtbot, bar, "open report")
    r = _enter(qtbot, bar)
    assert r.say.startswith("Which one?") and not fake_platform.opened
    shown = [bar.list.results.items[i].name for i in range(bar.list.results.rowCount())]
    _type(qtbot, bar, "open the second one")
    r = _enter(qtbot, bar)
    assert fake_platform.opened[-1].name == shown[1]
    bar.show_bar()
    _type(qtbot, bar, "show its folder")
    _enter(qtbot, bar)
    assert fake_platform.revealed[-1].name == shown[1]


def test_keyboard_navigation(qtbot: QtBot, bar: QuickBar, fake_platform: FakePlatform) -> None:
    bar.show_bar()
    _type(qtbot, bar, "report")
    names = [it.name for it in bar.list.results.items]
    qtbot.keyClick(bar.edit, Qt.Key.Key_Down)  # explicit selection: row 1
    _enter(qtbot, bar)
    assert fake_platform.opened[-1].name == names[1]
    bar.show_bar()
    _type(qtbot, bar, "report")
    with qtbot.waitSignal(bar.reply_applied, timeout=5000):
        qtbot.keyClick(bar.edit, Qt.Key.Key_3, Qt.KeyboardModifier.ControlModifier)
    assert fake_platform.opened[-1].name == names[2]
    bar.show_bar()
    _type(qtbot, bar, "report")
    # Opening logs an access, so the order changed: read the list as shown now.
    top = bar.list.results.items[0].name
    _enter(qtbot, bar, Qt.KeyboardModifier.ControlModifier)
    assert fake_platform.revealed[-1].name == top
    bar.show_bar()
    qtbot.keyClick(bar.edit, Qt.Key.Key_Escape)
    assert not bar.isVisible()


def test_click_opens_and_projects_view(
    qtbot: QtBot, bar: QuickBar, fake_platform: FakePlatform
) -> None:
    bar.show_bar()
    _type(qtbot, bar, "my projects")
    assert bar.list.results.items[0].kind == "project"
    rect = bar.list.visualRect(bar.list.results.index(0, 0))
    with qtbot.waitSignal(bar.reply_applied, timeout=5000):
        qtbot.mouseClick(bar.list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert fake_platform.opened[-1].name == "zachary"  # the project folder


def test_ui_never_blocks_while_engine_is_slow(qtbot: QtBot, bar: QuickBar) -> None:
    h = bar.engine.handler
    assert h is not None
    real = h.preview

    def slow(text: str) -> Reply:
        time.sleep(0.25)  # a pathological slow query
        return real(text)

    h.preview = slow  # type: ignore[method-assign]
    gaps: list[float] = []
    last = [time.perf_counter()]

    def tick() -> None:
        now = time.perf_counter()
        gaps.append((now - last[0]) * 1e3)
        last[0] = now

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(5)
    bar.show_bar()
    qtbot.keyClicks(bar.edit, "fees hostel report", delay=15)
    qtbot.wait(800)
    timer.stop()
    h.preview = real  # type: ignore[method-assign]
    assert max(gaps) < 60, f"event loop stalled {max(gaps):.0f} ms"


def test_engine_coalesces_previews(qtbot: QtBot, bar: QuickBar) -> None:
    h = bar.engine.handler
    assert h is not None
    calls: list[str] = []
    real = h.preview

    def counting(text: str) -> Reply:
        calls.append(text)
        time.sleep(0.05)
        return real(text)

    h.preview = counting  # type: ignore[method-assign]
    for i in range(1, 21):
        bar.engine.ask("preview", "report"[: 1 + i % 6])
    qtbot.wait(600)
    h.preview = real  # type: ignore[method-assign]
    assert 1 <= len(calls) < 20  # stale previews skipped


def test_stale_reply_is_ignored(qtbot: QtBot, bar: QuickBar) -> None:
    bar.show_bar()
    r = _type(qtbot, bar, "fees")
    shown = bar.list.results.rowCount()
    old = Reply("stale", "results", [], r.intent)
    bar._on_reply(1, old)  # id 1 is long superseded
    assert bar.list.results.rowCount() == shown and bar.status.text() != "stale"


def test_hold_space_in_empty_box_talks(qtbot: QtBot, bar: QuickBar) -> None:
    """Hold Space (box empty) to talk, release to send; typing spaces still works."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    from zachary.ui import quickbar as QB

    events: list[bool] = []
    bar.hold_talk.connect(events.append)
    bar.edit.clear()

    def key(kind: QEvent.Type, repeat: bool = False) -> None:
        ev = QKeyEvent(kind, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " ", repeat)
        QApplication.sendEvent(bar.edit, ev)

    key(QEvent.Type.KeyPress)
    for _ in range(5):  # holding: the keyboard repeats press/release pairs
        key(QEvent.Type.KeyRelease, repeat=True)
        key(QEvent.Type.KeyPress, repeat=True)
    assert events == [True] and bar.edit.text() == ""  # no spaces typed
    key(QEvent.Type.KeyRelease)
    assert events == [True, False]
    # In a search with text, a tap is just a space.
    bar.edit.setText("fee")
    bar.edit.setCursorPosition(3)
    qtbot.keyClick(bar.edit, Qt.Key.Key_Space)
    assert events == [True, False] and bar.edit.text() == "fee "
    # Fast typing: the next letter goes down before Space comes up -> order kept.
    key(QEvent.Type.KeyPress)
    qtbot.keyClick(bar.edit, Qt.Key.Key_R)
    key(QEvent.Type.KeyRelease)
    assert bar.edit.text() == "fee  r" and events == [True, False]
    # A long press while typing records instead of typing a space.
    key(QEvent.Type.KeyPress)
    qtbot.wait(QB.SPACE_HOLD_MS + 150)
    key(QEvent.Type.KeyRelease, repeat=True)
    key(QEvent.Type.KeyPress, repeat=True)
    assert events == [True, False, True] and bar.edit.text() == "fee  r"
    key(QEvent.Type.KeyRelease)
    assert events == [True, False, True, False]

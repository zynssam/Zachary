"""Design v2 (look only): the voice dot, themes, the review header, every window paints."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from pytestqt.qtbot import QtBot

from zachary.actions.planner import Plan, PlanItem
from zachary.ui import style
from zachary.ui.voice_dot import STATES, VoiceDot, normalize


class _Engine(QObject):
    replied = Signal(int, object)
    failed = Signal(int, str)

    def ask(self, *_a: object) -> int:
        return 0


def test_dot_is_still_when_idle_and_animates_otherwise(qtbot: QtBot) -> None:
    dot = VoiceDot()
    qtbot.addWidget(dot)
    assert dot.state == "idle" and not dot.animating()
    for s in STATES[1:]:
        dot.set_state(s)
        assert dot.animating(), s
        assert not dot.grab().isNull()
    dot.set_state("idle")
    assert not dot.animating()
    dot.set_state("error: the microphone is silent")
    assert dot.state == "error"
    qtbot.waitUntil(lambda: not dot.animating(), timeout=2000)  # shakes once, then still
    assert normalize("info: using mic X") == "idle"


def test_bar_dot_follows_voice_states(qtbot: QtBot) -> None:
    from zachary.ui.quickbar import QuickBar

    bar = QuickBar(_Engine())  # type: ignore[arg-type]
    qtbot.addWidget(bar)
    bar.set_voice_state("listening")
    assert bar.dot.state == "listening"
    bar.set_voice_state("thinking")
    bar.set_voice_state("idle")  # (the pipeline says idle right after it heard you)
    assert bar.dot.state == "thinking"
    bar.voice_replied()
    assert bar.dot.state == "speaking"
    bar.voice_done()
    assert bar.dot.state == "idle" and not bar.dot.animating()


def test_bar_fades_in_and_is_usable_at_once(qtbot: QtBot) -> None:
    from zachary.ui.quickbar import QuickBar

    bar = QuickBar(_Engine())  # type: ignore[arg-type]
    qtbot.addWidget(bar)
    bar.show_bar()
    assert bar.isVisible() and bar._reveal < 1.0
    qtbot.waitUntil(lambda: bar._reveal == 1.0, timeout=2000)
    assert bar.edit.graphicsEffect() is None  # effects removed once shown
    bar.hide()


def test_review_header_shows_what_can_be_freed(qtbot: QtBot) -> None:
    from zachary.ui.plan_review import PlanReview

    mb = 1024 * 1024
    now = time.time()
    plan = Plan("clean", "Clean up Downloads", [
        PlanItem(1, "trash", "/h/Downloads/a (1).pdf", None, "duplicate", "copy", 4 * mb, now),
        PlanItem(2, "trash", "/h/Downloads/setup.deb", None, "installer", "old", 96 * mb, now),
        PlanItem(3, "trash", "/h/Documents/empty", None, "empty_folder", "empty", 0, now,
                 is_dir=True),
    ], id=1)  # fmt: skip
    rv = PlanReview()
    qtbot.addWidget(rv)
    rv.show_plan(plan)
    assert rv.big.text() == "100.0 MB can be freed"
    assert rv.chips.count() == 4  # 3 categories + the stretch
    assert rv.approve_btn.text() == "Move 3 to the trash (100.0 MB)"  # (unchanged behaviour)
    rv.close()


def test_every_window_paints_in_both_themes(qtbot: QtBot, tmp_path: Path) -> None:
    from zachary.ui.notes import NotesStore, NotesWindow
    from zachary.ui.quickbar import QuickBar

    for t in (style.DARK, style.LIGHT):
        bar = QuickBar(_Engine())  # type: ignore[arg-type]
        qtbot.addWidget(bar)
        bar.theme = t
        bar._apply_theme()
        assert not bar.grab().isNull()
        win = NotesWindow(NotesStore(tmp_path / "Notes"))
        qtbot.addWidget(win)
        win.set_voice_available(True)
        win.mic_btn.setChecked(True)
        assert win.rec.isVisibleTo(win)  # the pulsing dot shows while dictating
        assert not win.grab().isNull()
        win.mic_btn.setChecked(False)
        assert not win.rec.isVisibleTo(win)
    assert style.icon_for("document", "x.pdf")[0] == "file-text"
    assert not style.icon_pixmap("folder", "#ffffff", 16).isNull()

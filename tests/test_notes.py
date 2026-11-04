"""Zachary's notes app: .txt files, autosave, paste, dictation into the open note."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QGuiApplication
from pytestqt.qtbot import QtBot

from zachary.brain.intents import parse
from zachary.ui.notes import NotesStore, NotesWindow, dictated, notes_folder


@pytest.mark.parametrize(
    ("text", "action"),
    [("open my notes", "open"), ("open the notes app", "open"), ("notes app", "open"),
     ("hey zach open zachary notes", "open"), ("new note", "new"), ("create a new note", "new"),
     ("make a note", "new"), ("take a note", "dictate"), ("dictate a note", "dictate")],
)  # fmt: skip
def test_notes_commands(text: str, action: str) -> None:
    i = parse(text)
    assert (i.kind, i.action) == ("notes", action)


@pytest.mark.parametrize("text", ["open notes", "open my physics notes", "notes", "take notes"])
def test_files_called_notes_are_still_files(text: str) -> None:
    assert parse(text).kind != "notes"  # "open notes" may be a file called notes


def test_notes_live_in_documents_zachary_notes() -> None:
    assert notes_folder() == Path.home() / "Documents" / "Zachary" / "Notes"


def test_store_saves_atomically_names_after_first_line_and_stays_inside(tmp_path: Path) -> None:
    store = NotesStore(tmp_path / "Notes")
    p = store.new_path()
    store.save(p, "Groceries\nmilk, eggs")
    assert p.read_text() == "Groceries\nmilk, eggs"
    named = store.named_after(p, "Groceries\nmilk, eggs")
    assert named.name == "Groceries.txt" and named.exists() and not p.exists()
    assert store.named_after(named, "Something else") == named  # named once only
    assert store.list() == [named]
    assert not list((tmp_path / "Notes").glob(".note-*"))  # no temp files left
    with pytest.raises(ValueError):
        store.save(tmp_path / "elsewhere.txt", "x")  # never writes outside the folder
    with pytest.raises(ValueError):
        store.save(tmp_path / "Notes" / ".." / "escape.txt", "x")


def test_dictated_pieces_are_sentences() -> None:
    assert dictated("buy milk", "") == "Buy milk."
    assert dictated("and eggs", "Buy milk.") == " And eggs."
    assert dictated("then bread", "Buy milk, ") == "then bread."
    assert dictated("first new paragraph second", "Intro.") == " First.\n\nSecond."
    assert dictated("is it done?", "x\n") == "Is it done?"


def test_typing_is_saved_automatically(qtbot: QtBot, tmp_path: Path) -> None:
    win = NotesWindow(NotesStore(tmp_path / "Notes"))
    qtbot.addWidget(win)
    win.present("new")
    qtbot.keyClicks(win.editor, "Hackathon ideas")  # type: ignore[no-untyped-call]
    qtbot.waitUntil(lambda: (tmp_path / "Notes" / "Hackathon ideas.txt").exists(), timeout=3000)
    assert win.notes.count() == 1
    win.new_note()  # an empty new note is not a file
    win.close()
    assert [p.name for p in (tmp_path / "Notes").iterdir()] == ["Hackathon ideas.txt"]


def test_paste_and_dictation_go_into_the_note(qtbot: QtBot, tmp_path: Path) -> None:
    win = NotesWindow(NotesStore(tmp_path / "Notes"))
    qtbot.addWidget(win)
    win.present("new")
    QGuiApplication.clipboard().setText("copied text")
    assert win.paste_clipboard()
    assert win.insert_dictation("this is my note")
    assert win.editor.toPlainText() == "copied text this is my note."  # (continues it)
    assert win.insert_dictation("scratch that")
    assert win.editor.toPlainText() == "copied text"
    assert win.insert_dictation("paste")  # "paste" while dictating: what you copied
    assert win.editor.toPlainText() == "copied textcopied text"
    win.set_voice_available(True)
    win.mic_btn.setChecked(True)
    toggles: list[bool] = []
    win.mic_toggled.connect(toggles.append)
    assert not win.insert_dictation("the end stop dictation")
    assert toggles == [False] and not win.mic_btn.isChecked()
    assert win.editor.toPlainText().endswith(" the end.")
    saved = next((tmp_path / "Notes").glob("*.txt"))
    assert saved.read_text() == win.editor.toPlainText()  # saved when dictation stops


def test_voice_types_into_the_note_not_the_bar(qtbot: QtBot, tmp_path: Path) -> None:
    """While the note's mic is on, speech is text for the note, never a command."""
    from PySide6.QtCore import QObject, Signal

    from zachary.telemetry.latency import Timeline
    from zachary.ui.quickbar import QuickBar
    from zachary.ui.voice import VoiceController
    from zachary.voice.factory import VoiceKit
    from zachary.voice.pipeline import Heard
    from zachary.voice.usage import UsageLedger

    class Engine(QObject):
        replied = Signal(int, object)
        failed = Signal(int, str)

        def ask(self, *a: object) -> int:
            asked.append(a)
            return 0

    asked: list[tuple[object, ...]] = []
    engine = Engine()
    bar = QuickBar(engine)  # type: ignore[arg-type]
    qtbot.addWidget(bar)
    kit = VoiceKit(rest_stt=None, stream_stt=None, offline_stt=None, spotter=None,  # type: ignore[arg-type]
                   tts=None, ledger=UsageLedger(None), has_key=True)  # fmt: skip
    voice = VoiceController(engine, bar, kit, wake_word=False)  # type: ignore[arg-type]
    listens: list[object] = []
    voice.push_to_talk = lambda hold=False, pause_ms=None: listens.append(pause_ms)  # type: ignore[method-assign]
    win = bar.notes_window()
    win.store = NotesStore(tmp_path / "Notes")
    qtbot.addWidget(win)
    win.present("new")
    assert win.mic_btn.isEnabled()  # voice is on: the mic button works
    win.mic_btn.setChecked(True)
    assert listens and voice._note_session
    asked.clear()
    voice._on_heard(Heard("meeting moved to friday", None, Timeline("voice")))
    assert win.editor.toPlainText() == "Meeting moved to friday."
    assert not [a for a in asked if a[0] == "submit"]  # never sent as a command
    qtbot.waitUntil(lambda: len(listens) == 2, timeout=2000)  # listening again by itself
    voice._on_heard(Heard("stop dictation", None, Timeline("voice")))
    assert not win.mic_btn.isChecked() and not voice._note_on
    win.close()
    voice.stop()

"""Phase 8: API keys in Zachary's own .env, the settings form, first-run setup."""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from tests.fakes import FakePlatform
from zachary.brain.intents import parse
from zachary.core.config import Config
from zachary.core.env import get_secret, set_secret


def test_keys_are_saved_privately_replaced_and_removed(tmp_path: Path) -> None:
    env = tmp_path / "data" / ".env"
    env.parent.mkdir()
    env.write_text("# mine\nGROQ_API_KEY=old\nOTHER=keep\n")
    set_secret("GROQ_API_KEY", "gsk_new", env)
    set_secret("GNANI_API_KEY", "vach_1", env)
    text = env.read_text()
    assert "GROQ_API_KEY=gsk_new" in text and "old" not in text
    assert "OTHER=keep" in text and "# mine" in text and "GNANI_API_KEY=vach_1" in text
    assert get_secret("GROQ_API_KEY", [env]) == "gsk_new"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(env).st_mode) == 0o600  # only you can read it
    set_secret("GROQ_API_KEY", "", env)  # empty: removed
    assert "GROQ_API_KEY" not in env.read_text()
    with pytest.raises(ValueError):
        set_secret("PATH", "x", env)  # only Zachary's keys
    with pytest.raises(ValueError):
        set_secret("GNANI_API_KEY", "a\nEVIL=1", env)  # no line injection


def _cfg(conn: sqlite3.Connection, fake_platform: FakePlatform) -> Config:
    cfg = Config(conn, fake_platform)
    cfg.seed_defaults()
    return cfg


def test_first_run_only_on_a_fresh_install(conn: sqlite3.Connection,
                                           fake_platform: FakePlatform) -> None:  # fmt: skip
    from zachary.ui.settings import needs_first_run

    cfg = _cfg(conn, fake_platform)
    assert needs_first_run(cfg)
    conn.execute("INSERT INTO files (path, name) VALUES ('/x/a.txt', 'a.txt')")
    assert not needs_first_run(cfg)  # set up before Phase 8: skip it...
    assert cfg.get_setting("setup_done") is True  # ...and remember that


def test_settings_validate_then_save(qtbot: QtBot, conn: sqlite3.Connection,
                                     fake_platform: FakePlatform, tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from zachary.core import env
    from zachary.ui.settings import SettingsForm

    monkeypatch.setattr(env, "secrets_file", lambda: tmp_path / "zenv")
    cfg = _cfg(conn, fake_platform)
    form = SettingsForm(cfg, fake_platform)
    qtbot.addWidget(form)
    form.quickbar_hotkey.setText("super+notakey")
    assert form.apply()  # a problem: nothing saved
    assert cfg.load().quickbar_hotkey == "super+space"
    form.quickbar_hotkey.setText("ctrl+alt+space")
    form.voice_enabled.setChecked(True)
    form.ocr.setChecked(True)
    form.roots.clear()
    form.roots.addItem(str(tmp_path / "Docs"))
    form.excludes.addItem(str(tmp_path / "Docs" / "private"))
    form.keys["GNANI_API_KEY"].setText("vach_test")
    form.autostart.setChecked(True)
    assert form.apply() == []
    s = cfg.load()
    assert s.quickbar_hotkey == "ctrl+alt+space" and s.voice_enabled and s.ocr_enabled
    assert [str(r) for r in cfg.index_roots()] == [str(tmp_path / "Docs")]
    assert any(p.endswith("private") for p, *_ in cfg.list_excludes())
    assert "GNANI_API_KEY=vach_test" in (tmp_path / "zenv").read_text()
    assert form.keys["GNANI_API_KEY"].text() == ""  # never shown again
    assert getattr(fake_platform, "autostart_on", False)
    assert "restart" in form.message.text()


def test_first_run_steps_through_and_finishes(qtbot: QtBot, conn: sqlite3.Connection,
                                              fake_platform: FakePlatform,
                                              monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from zachary.ui import settings as st
    from zachary.ui.settings import FirstRun

    monkeypatch.setattr(st, "has_secret", lambda _name: False)  # (a fresh computer)
    cfg = _cfg(conn, fake_platform)
    setup = FirstRun(cfg, fake_platform)
    qtbot.addWidget(setup)
    assert setup.step.text() == "Step 1 of 5" and not setup.back.isEnabled()
    setup.next.click()
    setup.next.click()
    assert setup.step.text() == "Step 3 of 5"  # the API keys
    setup.next.click()  # none pasted: asks once, says what won't work
    assert setup.step.text() == "Step 3 of 5"
    msg = setup.form.message.text()
    assert "Gnani.ai" in msg and "Groq" in msg and "Gemini" in msg and "voice" in msg
    setup.next.click()  # Next again: add them later
    assert setup.step.text() == "Step 4 of 5"
    setup.next.click()
    assert setup.next.text() == "Finish"
    with qtbot.waitSignal(setup.finished, timeout=2000):
        setup.next.click()
    assert cfg.get_setting("setup_done") is True


def test_keys_step_has_where_to_get_each_key(qtbot: QtBot, conn: sqlite3.Connection,
                                            fake_platform: FakePlatform) -> None:  # fmt: skip
    from zachary.ui.settings import KEY_HELP, KEYS, SettingsForm

    form = SettingsForm(_cfg(conn, fake_platform), fake_platform)
    qtbot.addWidget(form)
    box = form.keys_box()
    from PySide6.QtWidgets import QLabel

    text = " ".join(lbl.text() for lbl in box.findChildren(QLabel))
    for name, _label in KEYS:
        assert KEY_HELP[name][0] in text  # a sign-up link for every key
    form.keys["GNANI_API_KEY"].setText("vach_x")
    assert "GNANI_API_KEY" not in form.missing_keys()


def test_open_settings_from_the_bar() -> None:
    assert parse("open settings").kind == "settings"
    assert parse("preferences").kind == "settings"
    assert parse("open settings.docx").kind != "settings"  # a file called settings


def test_windows_gets_shortcuts_windows_allows(conn: sqlite3.Connection,
                                               fake_platform: FakePlatform) -> None:  # fmt: skip
    """Windows keeps Win+Space (keyboard language) for itself: different defaults there."""
    fake_platform.name = "windows"
    cfg = _cfg(conn, fake_platform)
    s = cfg.load()
    assert (s.quickbar_hotkey, s.voice_hotkey) == ("ctrl+alt+space", "ctrl+shift+space")
    cfg.set_setting("quickbar_hotkey", "ctrl+alt+z")
    cfg.seed_defaults()  # (every start) never overrides your choice
    assert cfg.load().quickbar_hotkey == "ctrl+alt+z"


def test_settings_refuse_win_space_on_windows_and_offer_a_restart(
    qtbot: QtBot, conn: sqlite3.Connection, fake_platform: FakePlatform
) -> None:
    from zachary.ui.settings import SettingsWindow

    fake_platform.name = "windows"
    restarts: list[bool] = []
    win = SettingsWindow(_cfg(conn, fake_platform), fake_platform,
                         on_restart=lambda: restarts.append(True))  # fmt: skip
    qtbot.addWidget(win)
    win.form.quickbar_hotkey.setText("super+space")
    assert win.form.apply()  # refused, with a reason
    assert "Windows keeps super+space" in win.form.message.text()
    win.form.quickbar_hotkey.setText("ctrl+alt+k")
    save = next(b for b in win.findChildren(type(win.restart_btn)) if b.text() == "Save")
    save.click()
    assert not win.restart_btn.isHidden()  # a shortcut changed: offer to restart
    win.restart_btn.click()
    assert restarts == [True]


def test_everything_is_in_settings_no_terminal_needed(
    qtbot: QtBot, conn: sqlite3.Connection, fake_platform: FakePlatform, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """AI answers, provider, pausing, hidden folders and name hints are all settings."""
    from zachary.core import env
    from zachary.ui import settings as st

    monkeypatch.setattr(env, "secrets_file", lambda: tmp_path / "zenv")
    monkeypatch.setattr(st, "has_secret", lambda _n: False)
    cfg = _cfg(conn, fake_platform)
    form = st.SettingsForm(cfg, fake_platform)
    qtbot.addWidget(form)
    form.llm.setChecked(True)
    form.provider.setCurrentIndex(form.provider.findData("groq"))
    form.keys["GEMINI_API_KEY"].setText("AIza-test")  # only a Gemini key...
    form.paused.setChecked(True)
    form.hidden.setChecked(True)
    form.keyterms.setChecked(False)
    assert form.apply() == []
    s = cfg.load()
    assert s.llm_enabled and s.llm_provider == "gemini"  # ...so Gemini answers
    assert s.indexing_paused and s.index_hidden_folders and not s.stt_keyterms


def test_first_run_turns_on_answers_and_voice(qtbot: QtBot, conn: sqlite3.Connection,
                                              fake_platform: FakePlatform) -> None:  # fmt: skip
    from zachary.ui.settings import FirstRun

    setup = FirstRun(_cfg(conn, fake_platform), fake_platform)
    qtbot.addWidget(setup)
    assert setup.form.llm.isChecked() and setup.form.voice_enabled.isChecked()


def test_shortcut_status_says_what_works() -> None:
    from zachary.ui.settings import hotkey_html

    html = hotkey_html([("Open the quick bar", "ctrl+alt+space", None, 2),
                        ("Push-to-talk", "ctrl+shift+space", "already used by Windows", 0)])  # fmt: skip
    assert "working (pressed 2 times)" in html and "already used by Windows" in html
    assert "Restart now" in html


def test_each_windows_hotkey_gets_its_own_id() -> None:
    from zachary.core.hotkeys import parse_hotkey
    from zachary.core.platform.windows import WindowsHotkey

    a = WindowsHotkey(parse_hotkey("ctrl+alt+space"), lambda _t: None)
    b = WindowsHotkey(parse_hotkey("ctrl+shift+space"), lambda _t: None)
    assert a.hotkey_id != b.hotkey_id


def test_log_file_is_written_to_the_data_folder(tmp_path: Path) -> None:
    import logging

    from zachary.assistant import add_log_file

    path = add_log_file(tmp_path)
    try:
        logging.getLogger("zachary.test").info("hello from the log")
        for h in logging.getLogger().handlers:
            h.flush()
        assert path is not None and "hello from the log" in path.read_text()
    finally:
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "baseFilename", "") == str(path):
                root.removeHandler(h)
                h.close()

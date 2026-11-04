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
                                              fake_platform: FakePlatform) -> None:  # fmt: skip
    from zachary.ui.settings import FirstRun

    cfg = _cfg(conn, fake_platform)
    setup = FirstRun(cfg, fake_platform)
    qtbot.addWidget(setup)
    assert setup.step.text() == "Step 1 of 4" and not setup.back.isEnabled()
    for _ in range(3):
        setup.next.click()
    assert setup.next.text() == "Finish"
    with qtbot.waitSignal(setup.finished, timeout=2000):
        setup.next.click()
    assert cfg.get_setting("setup_done") is True


def test_open_settings_from_the_bar() -> None:
    assert parse("open settings").kind == "settings"
    assert parse("preferences").kind == "settings"
    assert parse("open settings.docx").kind != "settings"  # a file called settings

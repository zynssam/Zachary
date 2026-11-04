from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any

import pytest

from tests.fakes import wait_until
from zachary.core.hotkeys import HotkeyError, parse_hotkey
from zachary.core.platform.linux import desktop_entry
from zachary.core.platform.linux_x11 import keysym_name, modifier_mask
from zachary.core.platform.windows import MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, hotkey_codes


@pytest.mark.parametrize(
    ("spec", "text", "portal"),
    [
        ("alt+space", "alt+space", "ALT+space"),
        ("Alt + Space", "alt+space", "ALT+space"),
        ("ctrl+shift+z", "ctrl+shift+z", "CTRL+SHIFT+z"),
        ("win+space", "super+space", "LOGO+space"),
        ("cmd+k", "super+k", "LOGO+k"),
        ("f9", "f9", "f9"),
        ("ctrl+alt+enter", "ctrl+alt+return", "CTRL+ALT+return"),
    ],
)
def test_parse_and_format(spec: str, text: str, portal: str) -> None:
    hk = parse_hotkey(spec)
    assert str(hk) == text and hk.portal_trigger() == portal


@pytest.mark.parametrize("spec", ["", "space", "a", "hyper+a", "alt+", "alt+ü", "ctrl+pagedown"])
def test_bad_specs(spec: str) -> None:
    with pytest.raises(HotkeyError):
        parse_hotkey(spec)


def test_backend_codes() -> None:
    mods, vk = hotkey_codes(parse_hotkey("ctrl+alt+space"))
    assert mods == MOD_ALT | MOD_CONTROL | MOD_NOREPEAT and vk == 0x20
    assert hotkey_codes(parse_hotkey("alt+z"))[1] == ord("Z")
    assert hotkey_codes(parse_hotkey("f12"))[1] == 0x7B
    assert modifier_mask(parse_hotkey("ctrl+alt+shift+super+a")) == 4 | 8 | 1 | 64
    assert keysym_name("space") == "space" and keysym_name("f5") == "F5"
    assert keysym_name("return") == "Return"


def test_desktop_entry_quotes_exec() -> None:
    text = desktop_entry(["/opt/my python/bin/python3", "-m", "zachary.assistant"])
    assert "Exec='/opt/my python/bin/python3' -m zachary.assistant" in text
    assert text.startswith("[Desktop Entry]") and "Name=Zachary" in text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_hotkey_thread_receives_wm_hotkey() -> None:
    import ctypes

    from zachary.core.platform.windows import _HOTKEY_ID, WM_HOTKEY, WindowsHotkey

    got: list[str | None] = []
    hk = WindowsHotkey(parse_hotkey("ctrl+alt+shift+f12"), got.append)
    hk.start()
    assert hk.ready.wait(5) and hk.error is None, hk.error
    assert hk.thread_id
    ctypes.windll.user32.PostThreadMessageW(hk.thread_id, WM_HOTKEY, _HOTKEY_ID, 0)  # type: ignore[attr-defined,unused-ignore]
    wait_until(lambda: got)
    hk.stop()
    assert got == [None]


x11_only = pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or not os.environ.get("DISPLAY")
    or bool(os.environ.get("WAYLAND_DISPLAY"))
    or shutil.which("xdotool") is None,
    reason="needs an X11 session (CI runs this under Xvfb) and xdotool",
)


@x11_only
def test_x11_grab_receives_key_press() -> None:
    from zachary.core.platform.linux_x11 import X11Hotkey

    got: list[str | None] = []
    hk = X11Hotkey(parse_hotkey("ctrl+alt+shift+f12"), got.append)
    hk.start()
    assert hk.ready.wait(5) and hk.error is None, hk.error
    subprocess.run(["xdotool", "key", "ctrl+alt+shift+F12"], check=True)
    wait_until(lambda: got)
    hk.stop()


@x11_only
def test_x11_reports_key_already_taken() -> None:
    from zachary.core.platform.linux_x11 import X11Hotkey

    spec = parse_hotkey("ctrl+alt+shift+f11")
    first = X11Hotkey(spec, lambda _t: None)
    first.start()
    assert first.ready.wait(5) and first.error is None
    second = X11Hotkey(spec, lambda _t: None)
    second.start()
    assert second.ready.wait(5)
    second.stop()
    first.stop()
    assert second.error is not None and "already taken" in second.error


def test_default_hotkey_is_super_space() -> None:
    from zachary.core.config import Settings

    hk = parse_hotkey(Settings().quickbar_hotkey)
    assert str(hk) == "super+space"
    assert hk.portal_trigger() == "LOGO+space"
    assert modifier_mask(hk) == 64  # X11 Mod4


def test_portal_binds_all_shortcuts_in_one_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """GNOME binds one portal session per app: two sessions meant one key never fired."""
    import threading

    from zachary.core.hotkeys import parse_hotkey
    from zachary.core.platform import linux_portal as lp

    sessions: list[list[str]] = []
    bound = threading.Event()

    def fake_session(self, handles):  # type: ignore[no-untyped-def]
        sessions.append([sid for sid, _props in lp.shortcuts_body(handles)])
        for h in handles:
            h.ready.set()
        bound.set()

    monkeypatch.setattr(lp._PortalSession, "_session", fake_session)
    mgr = lp._PortalSession()
    monkeypatch.setattr(lp, "_manager", lambda: mgr)
    toggle = lp.PortalHotkey(parse_hotkey("super+space"), lambda _t: None, "Open", "toggle")
    voice = lp.PortalHotkey(parse_hotkey("super+alt+space"), lambda _t: None, "Talk", "voice")
    toggle.start()
    voice.start()
    assert bound.wait(3) and voice.ready.wait(3)
    assert sessions == [["toggle", "voice"]] and mgr.runs == 1
    body = dict(lp.shortcuts_body(mgr.handles))
    assert body["voice"]["preferred_trigger"] == ("s", "ALT+LOGO+space")
    toggle.stop()
    voice.stop()
    assert mgr.handles == []


def test_portal_does_not_ask_again_for_known_shortcuts() -> None:
    """BindShortcuts shows GNOME's "allow shortcuts" dialog: skip it once approved."""
    from zachary.core.hotkeys import parse_hotkey
    from zachary.core.platform import linux_portal as lp

    hs = [
        lp.PortalHotkey(parse_hotkey("super+space"), lambda _t: None, "Open", "toggle"),
        lp.PortalHotkey(parse_hotkey("super+alt+space"), lambda _t: None, "Talk", "voice"),
    ]
    known: dict[str, Any] = {"shortcuts": ("a(sa{sv})", [("toggle", {}), ("voice", {})])}
    assert not lp.needs_bind(known, hs)
    assert lp.needs_bind({"shortcuts": ("a(sa{sv})", [("toggle", {})])}, hs)  # a new one
    assert lp.needs_bind({}, hs)  # (ListShortcuts failed / first run)

"""Opening apps ("open spotify") and opening things in an app ("open zachary in vscode")."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes import FakePlatform, write
from zachary.actions.safe_actions import SafeActions
from zachary.brain.handler import Handler
from zachary.brain.intents import parse, split_app
from zachary.core.platform import apps
from zachary.indexer import projects
from zachary.indexer.scanner import scan
from zachary.indexer.store import FileStore

SPOTIFY = """[Desktop Entry]
Type=Application
Name=Spotify
Name[de]=Spotify DE
GenericName=Music Player
Exec=/usr/bin/flatpak run com.spotify.Client @@u %U @@
Keywords=music;audio;

[Desktop Action New]
Name=Should be ignored
"""
CODE = "[Desktop Entry]\nType=Application\nName=Visual Studio Code\nExec=/usr/share/code/code %F\n"
HIDDEN = "[Desktop Entry]\nType=Application\nName=Secret\nExec=secret\nNoDisplay=true\n"
CALC = "[Desktop Entry]\nType=Application\nName=Calculator\nExec=gnome-calculator\n"


@pytest.fixture
def desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    user, system = tmp_path / "user", tmp_path / "system"
    (user / "applications").mkdir(parents=True)
    (system / "applications").mkdir(parents=True)
    (system / "applications" / "com.spotify.Client.desktop").write_text(SPOTIFY)
    (system / "applications" / "code.desktop").write_text(CODE)
    (system / "applications" / "secret.desktop").write_text(CALC)  # overridden below
    (user / "applications" / "secret.desktop").write_text(HIDDEN)
    (system / "applications" / "ai.zachary.Zachary.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Zachary\nExec=zachary\n"
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(user))
    monkeypatch.setenv("XDG_DATA_DIRS", str(system))
    monkeypatch.setattr(apps, "_xdg_dirs", lambda: [user, system])
    return tmp_path


def test_desktop_entries(desktop: Path) -> None:
    found = {a.name: a for a in apps._linux_apps()}
    assert set(found) == {"Spotify", "Visual Studio Code"}  # hidden + our own left out
    assert found["Spotify"].generic == "Music Player" and found["Spotify"].accepts_files
    assert found["Visual Studio Code"].accepts_files and found["Spotify"].keywords == (
        "music", "audio")  # fmt: skip


@pytest.mark.parametrize(
    ("query", "name", "at_least"),
    [("spotify", "Spotify", 1.0), ("Spotify", "Spotify", 1.0), ("spotfy", "Spotify", 0.75),
     ("vscode", "Visual Studio Code", 1.0), ("vs code", "Visual Studio Code", 1.0),
     ("music player", "Spotify", 0.6)],
)  # fmt: skip
def test_app_names(query: str, name: str, at_least: float, fake_platform: FakePlatform) -> None:
    m = apps.find_app(query, fake_platform.apps)
    assert m is not None and m[0].name == name and m[1] >= at_least


def test_no_app_for_files(fake_platform: FakePlatform) -> None:
    for q in ("fee receipt", "dsa notes", "rm -rf /"):
        m = apps.find_app(q, fake_platform.apps)
        assert m is None or m[1] < 0.75


@pytest.mark.parametrize(
    ("text", "target", "app"),
    [("open zachary in vscode", "zachary", "vscode"),
     ("open the zachary project in vs code", "zachary", "vs code"),
     ("zachary ko vscode mein kholo", "zachary", "vscode"),  # Hinglish order
     ("open my resume with writer", "resume", "writer"),
     ("opne zachary in vscode", "zachary", "vscode")],  # typo
)  # fmt: skip
def test_parse_open_in_app(text: str, target: str, app: str) -> None:
    i = parse(text)
    assert (i.kind, i.target, i.app) == ("open", target, app)


def test_wake_word_vs_project_named_zachary() -> None:
    assert parse("hey zach open fee receipt").query == "fee receipt"
    assert parse("zachary open fee receipt").query == "fee receipt"
    assert parse("open zachary").query == "zachary"
    assert split_app("fee receipt") == ("", "")


# -- through the handler ------------------------------------------------------------------------
@pytest.fixture
def h(store: FileStore, home: Path, fake_platform: FakePlatform) -> Handler:
    write(home / "code" / "zachary" / "pyproject.toml")
    write(home / "code" / "zachary" / "README.md")
    write(home / "Notes" / "spotify playlist ideas.txt")
    write(home / "Downloads" / "notes.txt")
    st = scan(store)
    projects.apply_markers(store, st.markers, scope=None)
    return Handler(store.conn, SafeActions(store.conn, fake_platform), store.config)


def test_open_an_app(h: Handler, fake_platform: FakePlatform) -> None:
    r = h.preview("open spotify")
    assert r.say == "Enter to open Spotify." and not fake_platform.launched
    r = h.submit("open spotify")
    assert r.say == "Opening Spotify." and fake_platform.launched == [
        ("com.spotify.Client.desktop", None)]  # fmt: skip
    assert h.submit("spotify kholo").say == "Opening Spotify."  # Hinglish


def test_open_in_vscode(h: Handler, fake_platform: FakePlatform, home: Path) -> None:
    r = h.submit("open zachary in vscode")
    assert r.say == "Opening zachary in Visual Studio Code."
    assert fake_platform.launched == [("code.desktop", home / "code" / "zachary")]
    assert not fake_platform.opened  # not the default file manager


def test_in_a_place_is_still_a_search(h: Handler, fake_platform: FakePlatform) -> None:
    r = h.submit("open notes in downloads")
    assert not fake_platform.launched and r.view == "results"


def test_app_that_cannot_open_files_is_not_used(h: Handler, fake_platform: FakePlatform) -> None:
    h.submit("open zachary in calculator")
    assert all(app != "org.gnome.Calculator.desktop" for app, _ in fake_platform.launched)


def test_only_installed_apps_launch(store: FileStore, fake_platform: FakePlatform) -> None:
    acts = SafeActions(store.conn, fake_platform)
    assert not acts.launch("/bin/sh") and not acts.launch("rm -rf /")
    assert acts.launch("code.desktop") and fake_platform.launched == [("code.desktop", None)]

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from zachary.core.platform import get_platform
from zachary.core.platform.linux import LinuxPlatform
from zachary.core.platform.windows import WindowsPlatform


def test_get_platform_matches_os() -> None:
    expected = "windows" if sys.platform == "win32" else "linux"
    assert get_platform().name == expected


def test_linux_data_dir_honours_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert LinuxPlatform().data_dir() == tmp_path / "zachary"


def test_windows_data_dir_uses_localappdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert WindowsPlatform().data_dir() == tmp_path / "Zachary"


@pytest.mark.parametrize("plat", [LinuxPlatform(), WindowsPlatform()])
def test_system_excludes_include_own_data_dir(plat: LinuxPlatform | WindowsPlatform) -> None:
    assert str(plat.data_dir()) in plat.system_excludes()


def test_no_os_specific_imports_outside_platform() -> None:
    """CLAUDE.md rule: OS-specific code lives only in core/platform/."""
    pkg = Path(__file__).resolve().parents[1] / "zachary"
    banned = (
        "import win32",
        "import winreg",
        "import ctypes.wintypes",
        "from win32",
        "import fcntl",
    )
    for py in pkg.rglob("*.py"):
        if "platform" in py.relative_to(pkg).parts:
            continue
        text = py.read_text(encoding="utf-8")
        for b in banned:
            assert b not in text, f"{py}: {b}"


@pytest.mark.skipif(sys.platform == "win32", reason="XDG autostart is Linux")
def test_autostart_entry_on_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    plat = LinuxPlatform()
    argv = ["/venv/bin/python", "-m", "zachary.assistant"]
    assert plat.autostart(argv) == (
        False,
        str(tmp_path / "autostart" / "ai.zachary.Zachary.desktop"),
    )
    on, where = plat.autostart(argv, True)
    text = Path(where).read_text()
    assert on and "Exec=/venv/bin/python -m zachary.assistant" in text
    assert "X-GNOME-Autostart-enabled=true" in text and "--show" not in text
    assert plat.autostart(argv, False)[0] is False and not Path(where).exists()

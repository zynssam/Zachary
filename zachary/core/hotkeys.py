"""Hotkey specs like ``"alt+space"`` or ``"ctrl+shift+z"``: parsing and formatting.

OS-agnostic. Each platform backend (``core/platform``) maps a :class:`Hotkey` to
its own codes (Windows virtual keys, X11 keysyms, XDG portal trigger strings).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

MODIFIERS = ("ctrl", "alt", "shift", "super")
_MOD_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt",
    "shift": "shift",
    "super": "super", "win": "super", "windows": "super", "meta": "super", "cmd": "super",
    "logo": "super",
}  # fmt: skip
_KEY_ALIASES = {
    "space": "space", "spacebar": "space", "enter": "return", "return": "return",
    "esc": "escape", "escape": "escape", "tab": "tab", "backspace": "backspace",
}  # fmt: skip
NAMED_KEYS = frozenset(
    {"space", "return", "escape", "tab", "backspace"} | {f"f{i}" for i in range(1, 13)}
)


class HotkeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Hotkey:
    mods: frozenset[str]
    key: str  # lowercase: a-z, 0-9, a NAMED_KEYS entry

    def __str__(self) -> str:
        mods = [m for m in MODIFIERS if m in self.mods]
        return "+".join([*mods, self.key])

    def portal_trigger(self) -> str:
        """XDG shortcuts format used by the GlobalShortcuts portal, e.g. ``ALT+space``."""
        names = {"ctrl": "CTRL", "alt": "ALT", "shift": "SHIFT", "super": "LOGO"}
        return "+".join([*(names[m] for m in MODIFIERS if m in self.mods), self.key])


def parse_hotkey(spec: str) -> Hotkey:
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("empty hotkey")
    *mod_parts, key = parts
    mods: set[str] = set()
    for m in mod_parts:
        if m not in _MOD_ALIASES:
            raise HotkeyError(f"unknown modifier {m!r} in {spec!r}")
        mods.add(_MOD_ALIASES[m])
    key = _KEY_ALIASES.get(key, key)
    if not (len(key) == 1 and key.isascii() and key.isalnum()) and key not in NAMED_KEYS:
        raise HotkeyError(f"unsupported key {key!r} in {spec!r}")
    if not mods and key not in {f"f{i}" for i in range(1, 13)}:
        raise HotkeyError(f"{spec!r} needs a modifier (only F1-F12 may stand alone)")
    return Hotkey(frozenset(mods), key)


class HotkeyBackend(Protocol):
    """A running global-hotkey listener. ``on_press`` may be called from any thread."""

    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...


# Called with an optional activation token (Wayland focus hand-off).
HotkeyCallback = Callable[[str | None], None]

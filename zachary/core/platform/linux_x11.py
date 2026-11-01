"""Global hotkey on X11 sessions via ``XGrabKey`` (libX11 through ctypes).

Runs its own X connection on a daemon thread: ``select()`` on the X socket plus
a wake-up pipe, so :meth:`stop` is clean. The grab is made for every
combination of CapsLock/NumLock so the key works regardless of lock state.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import logging
import os
import select
import threading
from typing import Any

from zachary.core.hotkeys import Hotkey, HotkeyCallback

log = logging.getLogger(__name__)

_SHIFT, _LOCK, _CTRL, _ALT, _NUMLOCK, _SUPER = 1, 2, 4, 8, 16, 64
_MOD_MASK = {"shift": _SHIFT, "ctrl": _CTRL, "alt": _ALT, "super": _SUPER}
_KEYSYM_NAMES = {"space": "space", "return": "Return", "escape": "Escape", "tab": "Tab",
                 "backspace": "BackSpace"}  # fmt: skip
_KEY_PRESS = 2
_GRAB_MODE_ASYNC = 1


class X11Unavailable(RuntimeError):
    pass


class _XKeyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("xkey", _XKeyEvent), ("pad", ctypes.c_long * 24)]


def keysym_name(key: str) -> str:
    if key in _KEYSYM_NAMES:
        return _KEYSYM_NAMES[key]
    if key.startswith("f") and key[1:].isdigit():
        return key.upper()
    return key


def modifier_mask(hotkey: Hotkey) -> int:
    mask = 0
    for m in hotkey.mods:
        mask |= _MOD_MASK[m]
    return mask


class X11Hotkey:
    name = "x11-grab"

    def __init__(self, hotkey: Hotkey, callback: HotkeyCallback) -> None:
        self.hotkey = hotkey
        self.callback = callback
        self.error: str | None = None
        self.ready = threading.Event()
        self._r, self._w = os.pipe()
        self._thread = threading.Thread(target=self._run, name="zach-x11", daemon=True)
        self._errors: list[int] = []

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with contextlib.suppress(OSError):
            os.write(self._w, b"x")
        self._thread.join(timeout=2)

    def _run(self) -> None:
        name = ctypes.util.find_library("X11")
        if not name:
            self.error = "libX11 not found"
            self.ready.set()
            return
        x: Any = ctypes.CDLL(name)
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XStringToKeysym.restype = ctypes.c_ulong
        x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x.XKeysymToKeycode.restype = ctypes.c_ubyte
        x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XGrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int]  # fmt: skip
        x.XUngrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong]
        x.XConnectionNumber.argtypes = [ctypes.c_void_p]
        x.XPending.argtypes = [ctypes.c_void_p]
        x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XEvent)]
        x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XCloseDisplay.argtypes = [ctypes.c_void_p]

        # X errors (e.g. BadAccess: key already grabbed) must not kill the process.
        handler_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        def on_error(_d: int, ev: int) -> int:
            # XErrorEvent: type(int), display*, resourceid, serial, error_code(uchar)...
            code = ctypes.cast(
                ev + ctypes.sizeof(ctypes.c_long) * 4, ctypes.POINTER(ctypes.c_ubyte)
            )
            self._errors.append(int(code[0]))
            return 0

        self._handler = handler_t(on_error)  # keep a reference
        x.XSetErrorHandler(self._handler)

        dpy = x.XOpenDisplay(None)
        if not dpy:
            self.error = "cannot open X display"
            self.ready.set()
            return
        try:
            root = x.XDefaultRootWindow(dpy)
            sym = x.XStringToKeysym(keysym_name(self.hotkey.key).encode())
            code = x.XKeysymToKeycode(dpy, sym)
            if not sym or not code:
                self.error = f"no keycode for {self.hotkey.key!r}"
                return
            mods = modifier_mask(self.hotkey)
            extras = (0, _LOCK, _NUMLOCK, _LOCK | _NUMLOCK)
            for extra in extras:
                x.XGrabKey(dpy, code, mods | extra, root, 1, _GRAB_MODE_ASYNC, _GRAB_MODE_ASYNC)
            x.XSync(dpy, 0)
            if self._errors:
                self.error = f"{self.hotkey} is already taken by another application"
                return
            self.ready.set()
            fd = x.XConnectionNumber(dpy)
            ev = _XEvent()
            while True:
                readable, _, _ = select.select([fd, self._r], [], [])
                if self._r in readable:
                    break
                while x.XPending(dpy):
                    x.XNextEvent(dpy, ctypes.byref(ev))
                    if ev.type == _KEY_PRESS:
                        self.callback(None)
            for extra in extras:
                x.XUngrabKey(dpy, code, mods | extra, root)
        finally:
            x.XCloseDisplay(dpy)
            self.ready.set()

"""Global hotkey on Wayland via the XDG GlobalShortcuts portal (D-Bus).

Wayland doesn't let apps grab keys. The portal asks the desktop to bind a
shortcut on our behalf: the first time, GNOME/KDE show a dialog where the user
approves (or changes) the key; after that it's remembered. Activations arrive as
``Activated`` signals, optionally with an XDG activation token so our window may
take focus.

Flow: (Registry.Register) -> CreateSession -> BindShortcuts -> wait for Activated.
All D-Bus I/O runs on one background thread (jeepney, blocking).
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import threading
from typing import Any

from zachary.core.hotkeys import Hotkey, HotkeyCallback

log = logging.getLogger(__name__)

APP_ID = "ai.zachary.Zachary"
SHORTCUT_ID = "toggle"
_PORTAL = "org.freedesktop.portal.Desktop"
_PATH = "/org/freedesktop/portal/desktop"


class PortalUnavailable(RuntimeError):
    pass


def portal_available() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


class PortalHotkey:
    """One global shortcut. All shortcuts of the app share **one** portal session
    (GNOME binds only one session per app: two sessions meant one key silently never
    fired). ``start()`` registers with the shared session; registrations made close
    together are bound in a single BindShortcuts call, a later one rebinds all."""

    name = "xdg-portal"

    def __init__(
        self,
        hotkey: Hotkey,
        callback: HotkeyCallback,
        description: str,
        shortcut_id: str = SHORTCUT_ID,
    ) -> None:
        self.shortcut_id = shortcut_id
        self.hotkey = hotkey
        self.callback = callback
        self.description = description
        self.error: str | None = None
        self.bound_trigger: str | None = None
        self.ready = threading.Event()  # set once bound (or failed)

    def start(self) -> None:
        _manager().add(self)

    def stop(self) -> None:
        _manager().remove(self)


class _PortalSession:
    """The app's single GlobalShortcuts session, on one D-Bus connection/thread."""

    BATCH_S = 0.4  # wait this long after a registration for others before binding

    def __init__(self) -> None:
        self.handles: list[PortalHotkey] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._conn: Any = None
        self._stopping = False
        self.runs = 0  # sessions created (tests)

    def add(self, h: PortalHotkey) -> None:
        with self._lock:
            self.handles = [x for x in self.handles if x.shortcut_id != h.shortcut_id] + [h]
            h.ready.clear()
            self._schedule()

    def remove(self, h: PortalHotkey) -> None:
        with self._lock:
            self.handles = [x for x in self.handles if x is not h]
            if not self.handles:
                if self._timer is not None:
                    self._timer.cancel()
                self._close()

    def _schedule(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.BATCH_S, self._restart)
        self._timer.daemon = True
        self._timer.start()

    def _restart(self) -> None:
        with self._lock:
            self._close()  # a new shortcut: rebind everything in a fresh session
            handles = list(self.handles)
            if not handles:
                return
            self._stopping = False
            self.runs += 1
            threading.Thread(target=self._run, args=(handles,), name="zach-portal",
                             daemon=True).start()  # fmt: skip

    def _close(self) -> None:
        self._stopping = True
        conn, self._conn = self._conn, None
        if conn is not None:
            # Closing from another thread unblocks the pending receive.
            with contextlib.suppress(Exception):
                conn.close()

    # -- D-Bus ---------------------------------------------------------------------
    def _request(self, conn: Any, method: str, signature: str, body: tuple[Any, ...]) -> Any:
        """Call a portal method that answers via a Request.Response signal."""
        from jeepney import DBusAddress, MatchRule, new_method_call
        from jeepney.bus_messages import message_bus

        token = "zach" + secrets.token_hex(6)
        sender = conn.unique_name[1:].replace(".", "_")
        path = f"{_PATH}/request/{sender}/{token}"
        rule = MatchRule(
            type="signal", interface="org.freedesktop.portal.Request", member="Response", path=path
        )
        conn.send_and_get_reply(message_bus.AddMatch(rule))
        # handle_token goes in the trailing options dict of every Request-style call.
        options = dict(body[-1])
        options["handle_token"] = ("s", token)
        body = (*body[:-1], options)
        addr = DBusAddress(
            _PATH, bus_name=_PORTAL, interface="org.freedesktop.portal.GlobalShortcuts"
        )
        with conn.filter(rule) as queue:
            reply = conn.send_and_get_reply(new_method_call(addr, method, signature, body))
            if reply.header.message_type.name == "error":
                raise PortalUnavailable(f"{method}: {reply.body}")
            response = conn.recv_until_filtered(queue)
        code, results = response.body
        if code != 0:
            raise PortalUnavailable(
                f"{method} was {'cancelled' if code == 1 else 'refused'} (code {code})"
            )
        return results

    def _run(self, handles: list[PortalHotkey]) -> None:
        try:
            self._session(handles)
        except Exception as e:  # report, never crash the app
            if not self._stopping:
                for h in handles:
                    h.error = f"{type(e).__name__}: {e}"
                log.warning("global shortcut portal unavailable: %s", handles[0].error)
        finally:
            for h in handles:
                h.ready.set()

    def _session(self, handles: list[PortalHotkey]) -> None:
        from jeepney import DBusAddress, MatchRule, new_method_call
        from jeepney.bus_messages import message_bus
        from jeepney.io.blocking import open_dbus_connection

        conn = open_dbus_connection(bus="SESSION")
        self._conn = conn
        # Tell the portal who we are (unsandboxed apps; xdg-desktop-portal >= 1.19).
        registry = DBusAddress(
            _PATH, bus_name=_PORTAL, interface="org.freedesktop.host.portal.Registry"
        )
        conn.send_and_get_reply(new_method_call(registry, "Register", "sa{sv}", (APP_ID, {})))
        res = self._request(
            conn,
            "CreateSession",
            "a{sv}",
            ({"session_handle_token": ("s", "zach" + secrets.token_hex(4))},),
        )
        session = res["session_handle"][1]
        res = self._request(
            conn, "BindShortcuts", "oa(sa{sv})sa{sv}", (session, shortcuts_body(handles), "", {})
        )
        by_id = {h.shortcut_id: h for h in handles}
        for sid, props in res.get("shortcuts", ("", []))[1]:
            if sid in by_id:
                by_id[sid].bound_trigger = props.get("trigger_description", ("s", ""))[1] or None
        for h in handles:
            log.info("portal shortcut %r bound: %s", h.shortcut_id,
                     h.bound_trigger or "(set in system settings)")  # fmt: skip
            h.ready.set()
        rule = MatchRule(
            type="signal",
            interface="org.freedesktop.portal.GlobalShortcuts",
            member="Activated",
        )
        conn.send_and_get_reply(message_bus.AddMatch(rule))
        with conn.filter(rule) as queue:
            while not self._stopping:
                msg = conn.recv_until_filtered(queue)
                msg_session, sid, _ts, opts = msg.body
                if msg_session != session or sid not in by_id:
                    continue
                token = opts.get("activation_token", ("s", None))[1]
                try:
                    by_id[sid].callback(token or None)
                except Exception:
                    log.exception("hotkey callback failed")


def shortcuts_body(handles: list[PortalHotkey]) -> list[tuple[str, dict[str, Any]]]:
    """The BindShortcuts ``a(sa{sv})`` argument: every shortcut of the app."""
    return [
        (
            h.shortcut_id,
            {
                "description": ("s", h.description),
                "preferred_trigger": ("s", h.hotkey.portal_trigger()),
            },
        )
        for h in handles
    ]


_MANAGER: _PortalSession | None = None
_MANAGER_LOCK = threading.Lock()


def _manager() -> _PortalSession:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = _PortalSession()
        return _MANAGER

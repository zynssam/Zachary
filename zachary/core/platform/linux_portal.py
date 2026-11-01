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
    """Listens for the portal shortcut on a daemon thread."""

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
        self._conn: Any = None
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="zach-portal", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        conn = self._conn
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

    def _run(self) -> None:
        try:
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
            shortcuts = [
                (
                    self.shortcut_id,
                    {
                        "description": ("s", self.description),
                        "preferred_trigger": ("s", self.hotkey.portal_trigger()),
                    },
                )
            ]
            res = self._request(
                conn, "BindShortcuts", "oa(sa{sv})sa{sv}", (session, shortcuts, "", {})
            )
            for sid, props in res.get("shortcuts", ("", []))[1]:
                if sid == self.shortcut_id:
                    self.bound_trigger = props.get("trigger_description", ("s", ""))[1] or None
            log.info("portal shortcut bound: %s", self.bound_trigger or "(set in system settings)")
            self.ready.set()
            rule = MatchRule(
                type="signal",
                interface="org.freedesktop.portal.GlobalShortcuts",
                member="Activated",
            )
            conn.send_and_get_reply(message_bus.AddMatch(rule))
            with conn.filter(rule) as queue:
                while not self._stopping:
                    msg = conn.recv_until_filtered(queue)
                    _session, sid, _ts, opts = msg.body
                    if sid == self.shortcut_id:
                        token = opts.get("activation_token", ("s", None))[1]
                        self.callback(token or None)
        except Exception as e:  # report, never crash the app
            if not self._stopping:
                self.error = f"{type(e).__name__}: {e}"
                log.warning("global shortcut portal unavailable: %s", self.error)
        finally:
            self.ready.set()

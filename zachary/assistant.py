"""``python -m zachary.assistant``: quick bar + global hotkey + tray.

- Single instance: a second launch (or ``--toggle``) just toggles the running
  quick bar via a local socket, so any desktop can bind a shortcut to
  ``python -m zachary.assistant --toggle`` if the automatic hotkey isn't possible.
- Starts the indexer as a separate background process if it isn't running.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from zachary.core.config import Config, default_db_path
from zachary.core.db import get_state, open_db
from zachary.core.hotkeys import HotkeyError, parse_hotkey
from zachary.core.platform import Platform, get_platform
from zachary.ui.engine import Engine
from zachary.ui.quickbar import QuickBar
from zachary.ui.tray import Tray, app_icon

log = logging.getLogger("zachary.assistant")

INDEXER_STALE_S = 15.0


def server_name() -> str:
    # ZACHARY_INSTANCE lets tests (or a second profile) run side by side.
    return f"zachary-assistant-{getpass.getuser()}{os.environ.get('ZACHARY_INSTANCE', '')}"


def toggle_cmd() -> list[str]:
    return [sys.executable, "-m", "zachary.assistant", "--toggle"]


def send_to_running(message: bytes, timeout_ms: int = 3000) -> bool:
    """Deliver a command to an already-running assistant and wait for its "ok".

    Returns False only if no instance is listening. Waiting for the reply matters on
    Windows: with named pipes, a client that disconnects right after writing can
    lose its message.
    """
    sock = QLocalSocket()
    sock.connectToServer(server_name())
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(message + b"\n")
    sock.waitForBytesWritten(timeout_ms)
    got = b""
    deadline = time.monotonic() + timeout_ms / 1000
    while b"\n" not in got and time.monotonic() < deadline:
        if sock.waitForReadyRead(50):
            got += bytes(sock.readAll().data())
    sock.disconnectFromServer()
    if not got:
        # We connected, so an instance is running; it is just slow to answer. Never start
        # a second app in that case (it would fight over the hotkey and the socket).
        log.warning("running assistant did not acknowledge %r in time", message)
    return True


def serve_commands(server: QLocalServer, handlers: dict[bytes, Callable[[], None]]) -> None:
    """Answer one-line commands from other launches (``toggle``, ``ping``) with ``ok``."""

    def on_conn() -> None:
        sock: QLocalSocket | None = server.nextPendingConnection()
        if not sock:
            return
        buf = bytearray()

        def handle() -> None:
            buf.extend(bytes(sock.readAll().data()))
            if b"\n" not in buf:
                return
            cmd = bytes(buf).split(b"\n", 1)[0].strip()
            fn = handlers.get(cmd)
            # Acknowledge first, act on the next loop turn: the sender is waiting.
            sock.write(b"ok\n" if fn is not None or cmd == b"ping" else b"unknown\n")
            sock.flush()
            if fn is not None:
                QTimer.singleShot(0, fn)

        sock.readyRead.connect(handle)
        sock.disconnected.connect(sock.deleteLater)
        if sock.bytesAvailable():
            handle()

    server.newConnection.connect(on_conn)


def indexer_running(db_path: Path) -> bool:
    try:
        conn = open_db(db_path)
        hb = get_state(conn, "indexer.heartbeat")
        conn.close()
    except Exception:
        return False
    return hb is not None and time.time() - float(hb) < INDEXER_STALE_S


def ensure_indexer(platform: Platform, db_path: Path) -> bool:
    """Start the indexer daemon unless it is alive. It refuses to run twice (lock)."""
    if indexer_running(db_path):
        return False
    platform.spawn_detached([sys.executable, "-m", "zachary.indexer", "--db", str(db_path)])
    return True


class _HotkeyBridge(QObject):
    """Carries hotkey presses from backend threads to the UI thread."""

    pressed = Signal(object, float)  # activation token, perf_counter at press


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zachary.assistant", description="Zachary quick bar")
    ap.add_argument("--db", default=None)
    ap.add_argument("--toggle", action="store_true", help="toggle a running quick bar")
    ap.add_argument("--talk", action="store_true", help="push-to-talk in a running assistant")
    ap.add_argument("--hotkey", default=None, help='e.g. "super+space" (default from settings)')
    ap.add_argument("--no-hotkey", action="store_true")
    ap.add_argument("--no-indexer", action="store_true", help="don't start the indexer")
    ap.add_argument("--show", action="store_true", help="show the quick bar at start")
    ap.add_argument("--no-voice", action="store_true", help="don't open the microphone")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG
        if args.verbose > 1
        else logging.INFO
        if args.verbose
        else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Zachary")
    app.setDesktopFileName("ai.zachary.Zachary")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon())

    if send_to_running(b"talk" if args.talk else b"toggle"):
        return 0  # another instance owns the quick bar
    if args.talk:
        log.warning("no running assistant to talk to")
        return 1

    platform = get_platform()
    db_path = Path(args.db) if args.db else default_db_path(platform)
    conn = open_db(db_path)
    cfg = Config(conn, platform)
    cfg.seed_defaults()
    settings = cfg.load()
    conn.close()

    if not args.no_indexer:
        ensure_indexer(platform, db_path)

    engine = Engine(db_path, platform)
    engine.start()
    bar = QuickBar(engine)

    server = QLocalServer()
    QLocalServer.removeServer(server_name())
    server.listen(server_name())

    talk_handlers: list[Callable[[], None]] = []

    def talk() -> None:
        if talk_handlers:
            talk_handlers[0]()
        else:
            log.warning("voice is off: python -m zachary.cli set voice_enabled true")

    serve_commands(server, {b"toggle": bar.toggle, b"talk": talk})

    tray = None
    if Tray.available():
        tray = Tray(
            on_open=lambda: bar.show_bar(),
            on_pause=lambda p: (engine.ask("pause", p), None)[1],
            on_quit=app.quit,
            paused=settings.indexing_paused,
        )
        tray.show()
    else:
        log.info("no system tray on this desktop; use the hotkey or the CLI")

    bridge = _HotkeyBridge()
    bridge.pressed.connect(lambda token, t0: bar.toggle(token, t0))
    backend = None
    if not args.no_hotkey:
        try:
            hk = parse_hotkey(args.hotkey or settings.quickbar_hotkey)
            platform.ensure_app_registration([sys.executable, "-m", "zachary.assistant"])
            backend = platform.global_hotkey(
                hk, lambda tok: bridge.pressed.emit(tok, time.perf_counter()), "Open Zachary"
            )
            backend.start()
        except HotkeyError as e:
            log.error("bad hotkey setting: %s", e)

        def check_hotkey() -> None:
            err = getattr(backend, "error", None) if backend else "no hotkey configured"
            if err:
                bar.show_bar()
                bar.status.setText(
                    f"Hotkey unavailable ({err}). " + platform.hotkey_hint(toggle_cmd())
                )

        QTimer.singleShot(4000, check_hotkey)

    voice = None
    voice_backend = None
    if settings.voice_enabled and not args.no_voice:
        from zachary.ui.voice import VoiceController
        from zachary.voice.factory import build_voice

        data_dir = db_path.parent
        kit = build_voice(Config(open_db(db_path), platform), data_dir)
        if not kit.has_key:
            log.warning("GNANI_API_KEY not set: voice uses local speech recognition only")
        voice = VoiceController(
            engine,
            bar,
            kit,
            speak=settings.speak_replies,
            wake_word=settings.wake_word,
            latency_path=data_dir / "latency.jsonl",
        )
        voice.start()
        talk_handlers.append(voice.push_to_talk)
        bar.talk.connect(voice.push_to_talk)
        bar.hold_talk.connect(voice.hold_talk)
        vbridge = _HotkeyBridge()
        vbridge.pressed.connect(lambda _tok, _t0: voice.push_to_talk())
        if not args.no_hotkey:
            try:
                voice_backend = platform.global_hotkey(
                    parse_hotkey(settings.voice_hotkey),
                    lambda tok: vbridge.pressed.emit(tok, time.perf_counter()),
                    "Talk to Zachary",
                    "voice",
                )
                voice_backend.start()
            except HotkeyError as e:
                log.error("bad voice hotkey setting: %s", e)

    if args.show or args.toggle:
        QTimer.singleShot(0, bar.show_bar)
    # Warm the engine's DB connection and the cached list so the first open is fast.
    QTimer.singleShot(0, lambda: engine.ask("preview", ""))

    code = app.exec()
    if voice is not None:
        voice.stop()
    if voice_backend is not None:
        voice_backend.stop()
    if backend is not None:
        backend.stop()
    engine.stop()
    return int(code)


if __name__ == "__main__":
    sys.exit(main())

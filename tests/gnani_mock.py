"""A local stand-in for the Gnani (Vachana) API, following docs.gnani.ai.

REST (http.server) and WebSocket (websockets) endpoints on 127.0.0.1. Tests never
call the real API. Configure ``transcript``, ``fail_status`` etc., then inspect
``calls``.
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np

KEY = "test-key-123"


def tone_wav(seconds: float = 0.3, sr: int = 24000) -> bytes:
    from zachary.voice.audio import pcm_to_wav

    t = np.arange(int(seconds * sr)) / sr
    pcm = (0.2 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16).tobytes()
    return pcm_to_wav(pcm, sr)


@dataclass
class MockGnani:
    transcript: str = "open the fees document I opened yesterday"
    fail_status: int | None = None  # e.g. 402 -> credits used up
    calls: list[dict[str, Any]] = field(default_factory=list)
    http_port: int = 0
    ws_port: int = 0

    @property
    def stt_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}/stt/v3"

    @property
    def tts_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}/api/v1/tts/inference"

    @property
    def stt_ws(self) -> str:
        return f"ws://127.0.0.1:{self.ws_port}/stt/v3/stream"

    @property
    def tts_ws(self) -> str:
        return f"ws://127.0.0.1:{self.ws_port}/api/v1/tts"

    def count(self, endpoint: str) -> int:
        return sum(1 for c in self.calls if c["endpoint"] == endpoint)


def _error_body(status: int) -> dict[str, Any]:
    codes = {401: "INVALID_API_KEY", 402: "INSUFFICIENT_CREDITS", 429: "RATE_LIMITED"}
    return {
        "detail": {"error_code": codes.get(status, "ERR"), "message": codes.get(status, "error")}
    }


def _http_handler(mock: MockGnani) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            key = self.headers.get("X-API-Key-ID")
            if self.path == "/stt/v3":
                fields: dict[str, str] = {}
                for name in ("language_code", "model"):
                    marker = f'name="{name}"\r\n\r\n'.encode()
                    if marker in body:
                        fields[name] = body.split(marker, 1)[1].split(b"\r\n", 1)[0].decode()
                wav_ok = b"RIFF" in body and b"WAVE" in body
                mock.calls.append({"endpoint": "stt-rest", "key": key, "fields": fields,
                                   "wav": wav_ok, "bytes": len(body)})  # fmt: skip
                if key != KEY:
                    return self._send(
                        401, json.dumps(_error_body(401)).encode(), "application/json"
                    )
                if mock.fail_status:
                    return self._send(
                        mock.fail_status,
                        json.dumps(_error_body(mock.fail_status)).encode(),
                        "application/json",
                    )
                out = {"success": True, "request_id": "r-1", "transcript": mock.transcript,
                       "model": "prisma-v3.0", "detected_language_code": "en-IN",
                       "processing_time": 0.1}  # fmt: skip
                return self._send(200, json.dumps(out).encode(), "application/json")
            if self.path == "/api/v1/tts/inference":
                req = json.loads(body)
                mock.calls.append({"endpoint": "tts-rest", "key": key, "request": req})
                if key != KEY or mock.fail_status:
                    st = 401 if key != KEY else int(mock.fail_status or 500)
                    return self._send(st, json.dumps(_error_body(st)).encode(), "application/json")
                return self._send(200, tone_wav(), "audio/wav")
            self._send(404, b"{}", "application/json")

    return H


def _ws_handler(mock: MockGnani):  # type: ignore[no-untyped-def]
    def handler(ws) -> None:  # type: ignore[no-untyped-def]
        headers = ws.request.headers
        path = ws.request.path
        key = headers.get("x-api-key-id")
        if path == "/stt/v3/stream":
            call: dict[str, Any] = {"endpoint": "stt-stream", "key": key, "frames": 0,
                                    "bad_frames": 0, "headers": dict(headers)}  # fmt: skip
            mock.calls.append(call)
            ws.send(json.dumps({"type": "connected", "message": "ok"}))
            for msg in ws:
                if isinstance(msg, bytes):
                    call["frames"] += 1
                    call["bad_frames"] += int(len(msg) != 1024)
                    continue
                if json.loads(msg).get("type") == "commit":
                    ws.send(json.dumps({"type": "transcript", "text": mock.transcript,
                                        "is_final": True, "detected_language_code": "en-IN"}))  # fmt: skip
                    call["committed"] = True
        elif path == "/api/v1/tts":
            req = json.loads(ws.recv())
            mock.calls.append({"endpoint": "tts-ws", "key": key, "request": req})
            if mock.fail_status:
                ws.send(json.dumps({"type": "error", "message": "insufficient credits"}))
                return
            wav = tone_wav()
            ws.send(json.dumps({"type": "start", "message": "ok", "request_id": "t-1"}))
            parts = [wav[:2000], wav[2000:6000], wav[6000:]]
            for i, part in enumerate(parts):
                ws.send(json.dumps({"type": "audio", "data": {"chunk_index": i,
                    "audio": base64.b64encode(part).decode(), "is_final": False}}))  # fmt: skip
            ws.send(json.dumps({"type": "complete", "data": {"chunk_index": 3, "audio": "",
                                                             "is_final": True}}))  # fmt: skip

    return handler


def start_mock() -> Iterator[MockGnani]:
    from websockets.sync.server import serve

    mock = MockGnani()
    http = ThreadingHTTPServer(("127.0.0.1", 0), _http_handler(mock))
    mock.http_port = http.server_address[1]

    def check_key(conn, request):  # type: ignore[no-untyped-def]
        if request.headers.get("x-api-key-id") != KEY:
            return conn.respond(401, "bad key")
        return None

    ws = serve(_ws_handler(mock), "127.0.0.1", 0, process_request=check_key)
    mock.ws_port = ws.socket.getsockname()[1]
    threads = [
        threading.Thread(target=http.serve_forever, daemon=True),
        threading.Thread(target=ws.serve_forever, daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        yield mock
    finally:
        http.shutdown()
        ws.shutdown()

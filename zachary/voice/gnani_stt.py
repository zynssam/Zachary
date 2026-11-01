"""Gnani (Vachana) speech-to-text: REST and realtime WebSocket.

Verified against docs.gnani.ai (Nov 2026):
- REST: ``POST https://api.vachana.ai/stt/v3`` multipart ``audio_file`` (WAV, <= 30 s),
  ``language_code`` (e.g. ``en-IN``; v3.0 also ``hi-IN,en-IN`` / ``indic-auto``),
  ``model=prisma-v3.0``. Header ``X-API-Key-ID``. JSON reply with ``transcript``.
  ``en-IN`` accepts English-Hindi mixed speech and returns Latin script, which is
  what the intent parser understands ("kal wali fees file").
- Stream: ``wss://api.vachana.ai/stt/v3/stream``; config in upgrade headers;
  binary PCM16 frames of exactly 1,024 bytes at real-time pace; ``{"type":
  "commit"}`` closes the utterance; ``transcript`` messages are final.

The API has a limited quota: no automatic retries; every real call is recorded
in the local usage ledger. The key is never logged.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from zachary.voice import net
from zachary.voice.audio import FRAME_BYTES, frames, pcm_to_wav, seconds
from zachary.voice.types import SAMPLE_RATE, Transcript, VoiceError
from zachary.voice.usage import UsageLedger

log = logging.getLogger(__name__)

REST_URL = "https://api.vachana.ai/stt/v3"
STREAM_URL = "wss://api.vachana.ai/stt/v3/stream"
DEFAULT_MODEL = "prisma-v3.0"
DEFAULT_LANGUAGE = "en-IN"
MAX_REST_SECONDS = 30.0

_KIND_BY_STATUS = {401: "auth", 402: "credits", 403: "scope", 429: "rate_limited"}


def classify(status: int, body: Any) -> VoiceError:
    """Turn an HTTP error into a VoiceError with a message the user can act on."""
    detail = ""
    if isinstance(body, dict):
        err = body.get("error") or body.get("detail") or {}
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("type") or err.get("error_code") or "")
        else:
            detail = str(err)
    kind = _KIND_BY_STATUS.get(status) or ("server" if status >= 500 else "bad_request")
    messages = {
        "auth": "Gnani rejected the API key (check GNANI_API_KEY)",
        "credits": "Gnani credits are used up",
        "scope": "this Gnani key isn't allowed to use speech-to-text",
        "rate_limited": "Gnani rate limit reached; try again shortly",
    }
    msg = messages.get(kind, f"Gnani error {status}")
    return VoiceError(kind, f"{msg}{': ' + detail if detail else ''}")


class GnaniRestSTT:
    name = "gnani-rest"

    def __init__(
        self,
        api_key: str,
        *,
        language: str = DEFAULT_LANGUAGE,
        model: str = DEFAULT_MODEL,
        url: str = REST_URL,
        ledger: UsageLedger | None = None,
        timeout: float = 20.0,
    ) -> None:
        if not api_key:
            raise VoiceError("config", "GNANI_API_KEY is not set")
        self._key = api_key
        self.language = language
        self.model = model
        self.url = url
        self.ledger = ledger or UsageLedger(None)
        self.timeout = timeout

    def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> Transcript:
        dur = seconds(pcm, sample_rate)
        if dur > MAX_REST_SECONDS:
            raise VoiceError("bad_request", f"clip is {dur:.0f}s; Gnani REST accepts 30 s")
        t0 = time.perf_counter()
        try:
            r = net.post(
                self.url,
                headers={"X-API-Key-ID": self._key},
                files={"audio_file": ("speech.wav", pcm_to_wav(pcm, sample_rate), "audio/wav")},
                data={"language_code": self.language, "model": self.model},
                timeout=self.timeout,
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            self.ledger.record("stt-rest", status="network", audio_s=round(dur, 2))
            raise VoiceError("network", f"can't reach Gnani: {type(e).__name__}") from e
        latency = (time.perf_counter() - t0) * 1e3
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200 or not isinstance(body, dict) or body.get("success") is False:
            self.ledger.record("stt-rest", status=str(r.status_code), audio_s=round(dur, 2))
            raise classify(r.status_code, body)
        self.ledger.record("stt-rest", status="ok", audio_s=round(dur, 2))
        return Transcript(
            text=str(body.get("transcript", "")).strip(),
            provider=self.name,
            language=body.get("detected_language_code") or self.language,
            latency_ms=latency,
            extra={"request_id": body.get("request_id"), "server_s": body.get("processing_time")},
        )


class GnaniStreamingSTT:
    """Streams mic frames while the user speaks; the transcript arrives right after."""

    name = "gnani-stream"

    def __init__(
        self,
        api_key: str,
        *,
        language: str = DEFAULT_LANGUAGE,
        model: str = DEFAULT_MODEL,
        url: str = STREAM_URL,
        ledger: UsageLedger | None = None,
        connect_timeout: float = 5.0,
        result_timeout: float = 3.0,  # finals arrive in < 0.5 s after commit
    ) -> None:
        if not api_key:
            raise VoiceError("config", "GNANI_API_KEY is not set")
        self._key = api_key
        self.language = language
        self.model = model
        self.url = url
        self.ledger = ledger or UsageLedger(None)
        self.connect_timeout = connect_timeout
        self.result_timeout = result_timeout
        self._ws: Any = None
        self._stack: contextlib.ExitStack | None = None
        self._pending = b""
        self._sent = 0

    def open(self) -> None:
        from websockets.exceptions import InvalidStatus
        from websockets.sync.client import connect

        headers = {
            "x-api-key-id": self._key,
            "language_code": self.language,
            "model": self.model,
            "x-sample-rate": str(SAMPLE_RATE),
            "x-idle-timeout-sec": "15",
        }
        stack = contextlib.ExitStack()
        try:
            sock = net.ws_socket(self.url, self.connect_timeout)
            extra: dict[str, Any] = {"sock": sock} if sock is not None else {}
            if sock is not None and self.url.startswith("wss"):
                extra["server_hostname"] = urlparse(self.url).hostname
            self._ws = stack.enter_context(
                connect(
                    self.url,
                    additional_headers=headers,
                    open_timeout=self.connect_timeout,
                    close_timeout=1.0,  # don't wait 10 s on a dead/aborted stream
                    **extra,
                )
            )
            self._stack = stack
        except InvalidStatus as e:
            status = e.response.status_code
            self.ledger.record("stt-stream", status=str(status), audio_s=0)
            raise classify(status, {}) from e
        except (OSError, TimeoutError) as e:
            raise VoiceError("network", f"can't reach Gnani: {type(e).__name__}") from e
        self._pending = b""
        self._sent = 0

    def feed(self, frame: bytes) -> None:
        """Queue audio; whole 1,024-byte frames are sent as they fill up."""
        if self._ws is None:
            return
        self._pending += frame
        while len(self._pending) >= FRAME_BYTES:
            chunk, self._pending = self._pending[:FRAME_BYTES], self._pending[FRAME_BYTES:]
            self._ws.send(chunk)
            self._sent += len(chunk)

    def finish(self) -> Transcript:
        """Flush, commit the utterance, and wait for its final transcript(s)."""
        ws = self._ws
        if ws is None:
            raise VoiceError("config", "stream not open")
        t0 = time.perf_counter()
        texts: list[str] = []
        lang: str | None = None
        try:
            for f in frames(self._pending) if self._pending else []:
                ws.send(f)
                self._sent += len(f)
            ws.send(json.dumps({"type": "commit"}))
            deadline = time.monotonic() + self.result_timeout
            got_final = False
            while time.monotonic() < deadline:
                try:
                    raw = ws.recv(
                        timeout=0.25 if got_final else max(0.05, deadline - time.monotonic())
                    )
                except TimeoutError:
                    if got_final:
                        break  # no more segments after the last one
                    continue
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "transcript":
                    if msg.get("text"):
                        texts.append(str(msg["text"]).strip())
                    lang = msg.get("detected_language_code") or lang
                    got_final = True
                elif kind == "error":
                    raise VoiceError("server", f"Gnani stream error: {msg.get('message', '')}")
        except VoiceError:
            raise
        except Exception as e:
            raise VoiceError("network", f"Gnani stream failed: {type(e).__name__}") from e
        finally:
            self._close()
            self.ledger.record("stt-stream", status="ok" if texts else "empty",
                               audio_s=round(self._sent / 2 / SAMPLE_RATE, 2))  # fmt: skip
        return Transcript(
            text=" ".join(t for t in texts if t),
            provider=self.name,
            language=lang or self.language,
            latency_ms=(time.perf_counter() - t0) * 1e3,
        )

    def abort(self) -> None:
        self._close()

    def _close(self) -> None:
        self._ws = None
        stack, self._stack = self._stack, None
        if stack is not None:
            with contextlib.suppress(Exception):
                stack.close()

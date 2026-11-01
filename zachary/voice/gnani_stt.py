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
import queue
import threading
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
    """Realtime STT: frames go up while the user speaks; Gnani's own VAD (silero)
    decides where speech starts and ends and sends events back:

    ``speech_start`` -> ``processing`` (end of speech) -> ``transcript`` (final).

    ``open()`` connects and starts a receiver thread; ``events`` is a queue of the
    server's JSON messages (plus ``{"type": "closed"}`` when the socket ends).
    ``feed()`` sends whole 1,024-byte frames; ``commit()`` finalizes now;
    ``close()`` always ends the session (Gnani caps concurrent sessions).
    ``finish()`` = commit + wait for the transcript, for callers that do their own
    endpointing.
    """

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
        vad_threshold: float = 0.5,  # docs default 0.7; earbud/laptop mics are quiet
        min_silence_ms: int = 450,  # end of speech after this much quiet
        max_speech_s: float = 15.0,
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
        self.vad_threshold = vad_threshold
        self.min_silence_ms = min_silence_ms
        self.max_speech_s = max_speech_s
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.server_config: dict[str, Any] = {}
        self._ws: Any = None
        self._stack: contextlib.ExitStack | None = None
        self._send_lock = threading.Lock()
        self._pending = b""
        self._sent = 0
        self._recorded = False
        self._texts: list[str] = []

    def headers(self) -> dict[str, str]:
        return {
            "x-api-key-id": self._key,
            "language_code": self.language,
            "model": self.model,
            "x-model": self.model,  # the legacy name too: unknown -> silent fallback to v2.5
            "x-sample-rate": str(SAMPLE_RATE),
            "x-idle-timeout-sec": "15",
            "x-vad-threshold": f"{self.vad_threshold:g}",
            "x-min-silence-ms": str(self.min_silence_ms),
            "x-max-speech-s": f"{self.max_speech_s:g}",
        }

    def open(self) -> None:
        from websockets.exceptions import InvalidStatus
        from websockets.sync.client import connect

        stack = contextlib.ExitStack()
        try:
            sock = net.ws_socket(self.url, self.connect_timeout)
            extra: dict[str, Any] = {"sock": sock} if sock is not None else {}
            if sock is not None and self.url.startswith("wss"):
                extra["server_hostname"] = urlparse(self.url).hostname
            self._ws = stack.enter_context(
                connect(
                    self.url,
                    additional_headers=self.headers(),
                    open_timeout=self.connect_timeout,
                    close_timeout=1.0,
                    **extra,
                )
            )
            self._stack = stack
        except InvalidStatus as e:
            status = e.response.status_code
            self.ledger.record("stt-stream", status=str(status), audio_s=0)
            self._recorded = True
            raise classify(status, {}) from e
        except (OSError, TimeoutError) as e:
            raise VoiceError("network", f"can't reach Gnani: {type(e).__name__}") from e
        self._pending = b""
        self._sent = 0
        threading.Thread(target=self._receive, args=(self._ws,), name="zach-stt-rx",
                         daemon=True).start()  # fmt: skip

    def _receive(self, ws: Any) -> None:
        try:
            for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("type") == "connected":
                    self.server_config = dict(msg.get("config") or {})
                    model = self.server_config.get("model") or msg.get("model")
                    if model and model != self.model:
                        log.warning("Gnani stream is using %s, not %s", model, self.model)
                elif msg.get("type") == "transcript" and msg.get("text"):
                    self._texts.append(str(msg["text"]).strip())
                self.events.put(msg)
        except Exception:
            pass
        finally:
            self.events.put({"type": "closed"})

    @property
    def is_open(self) -> bool:
        return self._ws is not None

    def feed(self, frame: bytes) -> None:
        """Queue audio; whole 1,024-byte frames are sent as they fill up."""
        ws = self._ws
        if ws is None:
            return
        self._pending += frame
        with self._send_lock:
            while len(self._pending) >= FRAME_BYTES:
                chunk, self._pending = self._pending[:FRAME_BYTES], self._pending[FRAME_BYTES:]
                ws.send(chunk)
                self._sent += len(chunk)

    def commit(self) -> None:
        """Send what's left and close the utterance now (don't wait for silence)."""
        ws = self._ws
        if ws is None:
            return
        with self._send_lock:
            for f in frames(self._pending) if self._pending else []:
                ws.send(f)
                self._sent += len(f)
            self._pending = b""
            ws.send(json.dumps({"type": "commit"}))

    def finish(self) -> Transcript:
        """Commit, then wait for the final transcript(s)."""
        if self._ws is None:
            raise VoiceError("config", "stream not open")
        t0 = time.perf_counter()
        lang: str | None = None
        texts: list[str] = []
        try:
            self.commit()
            deadline = time.monotonic() + self.result_timeout
            got_final = False
            while time.monotonic() < deadline:
                try:
                    msg = self.events.get(timeout=0.25 if got_final else 0.05)
                except queue.Empty:
                    if got_final:
                        break  # no more segments after the last one
                    continue
                kind = msg.get("type")
                if kind == "transcript":
                    if msg.get("text"):
                        texts.append(str(msg["text"]).strip())
                    lang = msg.get("detected_language_code") or lang
                    got_final = True
                elif kind == "error":
                    raise VoiceError("server", f"Gnani stream error: {msg.get('message', '')}")
                elif kind == "closed":
                    break
        except VoiceError:
            raise
        except Exception as e:
            raise VoiceError("network", f"Gnani stream failed: {type(e).__name__}") from e
        finally:
            self.close()
        return Transcript(
            text=" ".join(t for t in texts if t),
            provider=self.name,
            language=lang or self.language,
            latency_ms=(time.perf_counter() - t0) * 1e3,
        )

    def close(self) -> None:
        """End the session (idempotent) and record it in the usage ledger."""
        self._ws = None
        stack, self._stack = self._stack, None
        if stack is not None:
            with contextlib.suppress(Exception):
                stack.close()
            if not self._recorded:
                self._recorded = True
                self.ledger.record("stt-stream", status="ok" if self._texts else "empty",
                                   audio_s=round(self._sent / 2 / SAMPLE_RATE, 2))  # fmt: skip

    def abort(self) -> None:
        self.close()

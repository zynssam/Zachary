"""Gnani (Vachana) text-to-speech: REST and WebSocket (lowest latency).

Verified against docs.gnani.ai (Nov 2026):
- REST: ``POST https://api.vachana.ai/api/v1/tts/inference`` JSON ``{text, voice,
  model, language, speed, audio_config}`` -> raw WAV bytes.
- WebSocket: ``wss://api.vachana.ai/api/v1/tts``; send the same JSON; receive JSON
  ``start`` / ``audio`` (``data.audio`` = base64) / ``complete`` / ``error``.
- Voices are per model; ``timbre-v3.0`` voices speak all 10 languages.
- Numbers, IDs and file extensions should be spoken words: see ``speakable``.

Replies are one short line, so a reply costs only a few dozen characters.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import httpx

from zachary.voice import net
from zachary.voice.audio import wav_to_pcm
from zachary.voice.gnani_stt import classify
from zachary.voice.types import SpeechAudio, VoiceError
from zachary.voice.usage import UsageLedger

REST_URL = "https://api.vachana.ai/api/v1/tts/inference"
WS_URL = "wss://api.vachana.ai/api/v1/tts"
DEFAULT_MODEL = "timbre-v3.0"
DEFAULT_VOICE = "Shivani"
DEFAULT_LANGUAGE = "en-IN"
SAMPLE_RATE = 24_000  # plenty for speech, half the bytes of 48 kHz

_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()  # noqa: E501
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_EXT = re.compile(
    r"\.(pdf|docx?|pptx?|xlsx?|txt|md|csv|py|cpp|c|js|ts|java|json|png|jpe?g|mp[34]|zip)\b", re.I
)


def _number_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + ("" if rest == 0 else " " + _number_words(rest))
    if n < 10000 and n % 100 == 0 and n % 1000 != 0:
        return _number_words(n // 100) + " hundred"
    if 1900 <= n <= 2099:  # read years naturally
        return (
            _number_words(n // 100) + " " + ("hundred" if n % 100 == 0 else _number_words(n % 100))
        )
    return " ".join(_ONES[int(d)] for d in str(n))


def speakable(text: str) -> str:
    """Make a reply pleasant to hear: no file extensions, separators or raw digits."""
    t = _EXT.sub("", text)
    t = re.sub(r"[_]+", " ", t)
    t = re.sub(r"(?<=\w)-(?=\w)", " ", t)
    t = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", t)  # "v2" -> "v 2"
    t = re.sub(r"\b\d{1,4}\b", lambda m: _number_words(int(m.group(0))), t)
    t = re.sub(r"[“”\"]", "", t)
    return re.sub(r"\s+", " ", t).strip()


class _Base:
    def __init__(
        self,
        api_key: str,
        *,
        voice: str = DEFAULT_VOICE,
        model: str = DEFAULT_MODEL,
        language: str = DEFAULT_LANGUAGE,
        speed: float = 1.0,
        ledger: UsageLedger | None = None,
    ) -> None:
        if not api_key:
            raise VoiceError("config", "GNANI_API_KEY is not set")
        self._key = api_key
        self.voice = voice
        self.model = model
        self.language = language
        self.speed = speed
        self.ledger = ledger or UsageLedger(None)

    def _request(self, text: str, container: str) -> dict[str, Any]:
        return {
            "text": text,
            "voice": self.voice,
            "model": self.model,
            "language": self.language,
            "speed": self.speed,
            "audio_config": {
                "sample_rate": SAMPLE_RATE,
                "num_channels": 1,
                "sample_width": 2,
                "encoding": "linear_pcm",
                "container": container,
            },
        }


class GnaniRestTTS(_Base):
    name = "gnani-tts-rest"

    def __init__(self, api_key: str, *, url: str = REST_URL, timeout: float = 20.0, **kw: Any):
        super().__init__(api_key, **kw)
        self.url = url
        self.timeout = timeout

    def synthesize(self, text: str) -> SpeechAudio:
        text = speakable(text)
        try:
            r = net.post(
                self.url,
                headers={"X-API-Key-ID": self._key, "Content-Type": "application/json"},
                json=self._request(text, "wav"),
                timeout=self.timeout,
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            self.ledger.record("tts-rest", status="network", chars=len(text))
            raise VoiceError("network", f"can't reach Gnani: {type(e).__name__}") from e
        if r.status_code != 200 or not r.content.startswith(b"RIFF"):
            try:
                body = r.json()
            except ValueError:
                body = {}
            self.ledger.record("tts-rest", status=str(r.status_code), chars=len(text))
            raise classify(r.status_code, body)
        self.ledger.record("tts-rest", status="ok", chars=len(text))
        pcm, sr = wav_to_pcm(r.content)
        return SpeechAudio(pcm, sr)

    def stream(self, text: str) -> Iterator[SpeechAudio]:
        yield self.synthesize(text)


def _strip_wav_header(chunk: bytes) -> bytes:
    """Safety net: if a WAV header ever arrives in the stream, drop it."""
    if chunk.startswith(b"RIFF") and b"data" in chunk[:200]:
        i = chunk.index(b"data", 12)
        return chunk[i + 8 :]
    return chunk


END_GRACE_S = 0.3  # after the audio's "complete", wait this long for the closing one


def _drain(ws: Any) -> int:
    """Discard messages already waiting on the socket. Returns how many."""
    n = 0
    while True:
        try:
            ws.recv(timeout=0)
        except TimeoutError:
            return n
        n += 1


class GnaniWsTTS(_Base):
    """WebSocket TTS on one persistent connection (docs: a connection serves many
    requests in sequence), so a reply skips the TCP + TLS + upgrade handshake.

    ``warm()`` connects ahead of time. ``stream(text)`` yields raw PCM chunks as
    they arrive (``container: raw``). If the server closed the idle socket, it
    reconnects once *before anything was synthesized*; a request is never repeated
    after Gnani started on it. Abandoning a stream midway drops the connection
    (its remaining audio would otherwise arrive with the next reply).
    """

    name = "gnani-tts-ws"

    def __init__(
        self,
        api_key: str,
        *,
        url: str = WS_URL,
        timeout: float = 15.0,
        keep_alive: bool = True,
        first_audio_timeout: float = 4.0,
        connect_timeout: float = 5.0,
        **kw: Any,
    ) -> None:
        super().__init__(api_key, **kw)
        self.url = url
        self.timeout = timeout
        # On a bad network a reply spoken many seconds late (after the next command,
        # even) is worse than none: the text is already on screen. Give up instead.
        self.first_audio_timeout = first_audio_timeout
        self.connect_timeout = connect_timeout
        self.keep_alive = keep_alive
        self._lock = threading.Lock()
        self._ws: Any = None
        self._stack: contextlib.ExitStack | None = None
        self.connects = 0  # for tests / telemetry

    # -- connection -------------------------------------------------------------------
    def _connect(self) -> Any:
        from websockets.exceptions import InvalidStatus
        from websockets.sync.client import connect

        stack = contextlib.ExitStack()
        try:
            sock = net.ws_socket(self.url, self.connect_timeout)
            extra: dict[str, Any] = {"sock": sock} if sock is not None else {}
            if sock is not None and self.url.startswith("wss"):
                extra["server_hostname"] = urlparse(self.url).hostname
            ws = stack.enter_context(
                connect(
                    self.url,
                    additional_headers={
                        "X-API-Key-ID": self._key,
                        "Content-Type": "application/json",
                    },
                    open_timeout=self.connect_timeout,
                    close_timeout=1.0,
                    **extra,
                )
            )
        except InvalidStatus as e:
            self.ledger.record("tts-ws", status=str(e.response.status_code), chars=0)
            raise classify(e.response.status_code, {}) from e
        except (OSError, TimeoutError) as e:
            raise VoiceError("network", f"can't reach Gnani: {type(e).__name__}") from e
        self._ws, self._stack = ws, stack
        self.connects += 1
        return ws

    def _drop(self) -> None:
        self._ws = None
        stack, self._stack = self._stack, None
        if stack is not None:
            with contextlib.suppress(Exception):
                stack.close()

    def _alive(self) -> Any:
        ws = self._ws
        if ws is None:
            return None
        try:
            from websockets.protocol import State

            if ws.protocol.state is not State.OPEN:
                self._drop()
                return None
        except Exception:
            pass
        return ws

    def warm(self) -> None:
        """Connect now (in the background at startup) so the first reply is fast."""
        with self._lock:
            if self._alive() is None:
                self._connect()

    def close(self) -> None:
        with self._lock:
            self._drop()

    # -- synthesis ----------------------------------------------------------------------
    def synthesize(self, text: str) -> SpeechAudio:
        chunks = list(self.stream(text))
        return SpeechAudio(b"".join(c.pcm for c in chunks), SAMPLE_RATE)

    def stream(self, text: str) -> Iterator[SpeechAudio]:
        from websockets.exceptions import ConnectionClosed

        text = speakable(text)
        request = json.dumps(self._request(text, "raw"))
        with self._lock:
            status = "ok"
            done = False
            started = False
            try:
                for attempt in (1, 2):
                    ws = self._alive() or self._connect()
                    try:
                        _drain(ws)  # anything left from an earlier reply is stale
                        ws.send(request)
                        first = True
                        carry = b""
                        audio_done = False
                        deadline = time.monotonic() + self.first_audio_timeout
                        while True:
                            left = deadline - time.monotonic()
                            if audio_done:
                                left = min(left, END_GRACE_S)
                            if left <= 0:
                                if audio_done:
                                    done = True  # no closing message: fine, drained next time
                                    return
                                status = "timeout" if not first else "late"
                                raise VoiceError(
                                    "network",
                                    "Gnani TTS timed out" if not first
                                    else "Gnani TTS too slow (no audio in time); not spoken",
                                )  # fmt: skip
                            try:
                                raw = ws.recv(timeout=left)
                            except TimeoutError:
                                if audio_done:
                                    done = True
                                    return
                                continue  # the deadline check above decides
                            if isinstance(raw, bytes):
                                continue
                            msg = json.loads(raw)
                            kind = msg.get("type")
                            if kind == "start":
                                started = True
                            elif not started:
                                continue  # before our "start": left over from an earlier reply
                            elif kind == "audio":
                                data = base64.b64decode(msg.get("data", {}).get("audio") or b"")
                                if first:
                                    data = _strip_wav_header(data)
                                    first = False
                                    deadline = time.monotonic() + self.timeout  # streaming
                                data = carry + data
                                cut = len(data) - len(data) % 2  # whole 16-bit samples
                                carry = data[cut:]
                                if cut:
                                    yield SpeechAudio(data[:cut], SAMPLE_RATE)
                            elif kind == "complete":
                                # Gnani sends two: {"data": {..., "is_final": true}} and
                                # then {"message", "request_id"}. Stop at the second, or
                                # the next reply would read it first (and get silence,
                                # then play *this* reply's leftovers: replies one late).
                                if "request_id" in msg or audio_done:
                                    done = True
                                    return
                                audio_done = True
                            elif kind == "error":
                                status = "error"
                                done = True  # the connection stays usable
                                raise VoiceError(
                                    "server", f"Gnani TTS error: {msg.get('message', '')}"
                                )
                    except ConnectionClosed as e:
                        self._drop()
                        if attempt == 1 and not started:
                            continue  # idle socket closed by the server: reconnect once
                        status = "network"
                        raise VoiceError("network", "Gnani TTS connection closed") from e
                    except TimeoutError as e:
                        status = "timeout"
                        raise VoiceError("network", "Gnani TTS timed out") from e
            finally:
                if started or status != "ok":
                    self.ledger.record("tts-ws", status=status, chars=len(text))
                if not done or not self.keep_alive:
                    self._drop()  # abandoned midway / one-shot: don't reuse

"""Connections to the speech API: prefer IPv4, reuse connections.

Measured on a real network: Gnani (behind Cloudflare) answered in 0.11 s over
IPv4 but 0.96-1.88 s over IPv6, which most clients prefer. A 5.7 s speech-to-
text round trip was 0.37 s of server time. So: try IPv4 first, fall back to the
system default (IPv6) only if IPv4 can't connect. A failed *connect* never
reaches the server, so the fallback can't use quota.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx

CONNECT_TIMEOUT_S = 4.0  # a slow/broken network fails fast; the read can take longer

_clients: dict[str, httpx.Client] = {}
_lock = threading.Lock()


def http_client(timeout: float) -> httpx.Client:
    """A shared keep-alive client (one TLS handshake, then reused), IPv4-first."""
    with _lock:
        key = f"v4:{timeout}"
        c = _clients.get(key)
        if c is None:
            c = httpx.Client(
                transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=0),
                timeout=httpx.Timeout(timeout, connect=min(CONNECT_TIMEOUT_S, timeout)),
            )
            _clients[key] = c
        return c


def post(url: str, *, timeout: float, **kw: Any) -> httpx.Response:
    """POST over IPv4 (reused connection); if IPv4 can't connect at all, use the default.

    ``timeout`` is the total for both tries: the fallback only gets what's left (on a
    flaky hotspot, one slow call used to cost two full timeouts: 33 s)."""
    t0 = time.monotonic()
    try:
        return http_client(timeout).post(url, **kw)
    except httpx.ConnectError:
        left = timeout - (time.monotonic() - t0)
        if left < 1.0:
            raise
        t = httpx.Timeout(left, connect=min(CONNECT_TIMEOUT_S, left))
        return httpx.post(url, timeout=t, **kw)  # e.g. an IPv6-only network


def ws_socket(url: str, timeout: float) -> socket.socket | None:
    """An IPv4 TCP socket to the URL's host, or None to let websockets decide."""
    u = urlparse(url)
    host = u.hostname or ""
    port = u.port or (443 if u.scheme in ("wss", "https") else 80)
    if host in ("127.0.0.1", "localhost"):
        return None
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    for family, kind, proto, _name, addr in infos:
        s = socket.socket(family, kind, proto)
        s.settimeout(timeout)
        try:
            s.connect(addr)
            s.settimeout(None)
            return s
        except OSError:
            s.close()
    return None

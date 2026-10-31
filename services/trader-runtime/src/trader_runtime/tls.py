"""One TLS client context for the short-lived httpx clients (2026-10-13 第二轮省 CPU).

Every `httpx.Client(...)` / `httpx.AsyncClient(...)` builds a fresh `ssl.SSLContext` and parses the
whole certifi bundle into it, even for a plain-http peer; the research poller (RSSHub routes, exchange
and industry pages) and the paper-engine control calls build one per request, which was 6% of the
Runtime at 60 readers. They now pass `verify=shared_ssl_context()`: the very context httpx would
have built (`httpx.create_ssl_context()` — certifi, or SSL_CERT_FILE / SSL_CERT_DIR from the
environment), built once. An SSLContext is safe to share between threads and connections; each client
keeps its own connections, cookies, proxies and timeouts exactly as before."""
from __future__ import annotations

import functools
import ssl

import httpx


@functools.cache
def shared_ssl_context() -> ssl.SSLContext:
    return httpx.create_ssl_context()

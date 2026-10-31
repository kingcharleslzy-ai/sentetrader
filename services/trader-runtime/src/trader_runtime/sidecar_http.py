"""Reusable localhost HTTP pools. No proxy, redirects or command retries.

2026-10-12 容量测试（连接池永久卡死）: httpx 0.28.1 / httpcore 1.0.9 leave a connection stuck in
CONNECTING in the pool when an asyncio cancellation reaches a request while it waits for its new
connection (encode/httpcore#1093; the fix, encode/httpcore#1099, is unmerged and encode/httpx makes
no new releases). A few overload rounds filled the 20 slots and every market read timed out until
the Runtime restarted. httpx2 — the maintained fork, already locked through `mcp` — drops such a
connection (pydantic/httpx2#983, in 2.12.0), so these pools use it; nothing else changes the
cancellation semantics. Three more rules live here, the one entry every sidecar read goes through:

- httpx's own timeouts bound a request (connect / read / write / pool, connect capped short for a
  localhost peer); no outer asyncio deadline cancels it mid-connect.
- A K-line long poll (`waitMs=`, parked up to 10 s) runs on its own pool, so the 48 parked channels
  per source can never starve the short reads (quotes, board, catalog, account, orders).
- A caller's cancellation that anyio turned into an httpx timeout (it happens when the cancel lands
  on the timeout's own deadline) is restored as CancelledError instead of reading as a failed request.

Self-heal (defence only): a pool whose requests have done nothing but wait out the pool timeout for
WEDGE_SECONDS is rebuilt — a pool that serves anything at all is never touched."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx2

from . import fast_json

log = logging.getLogger("uvicorn.error.sidecar_http")

# Short reads (quotes, board, catalog, calendar, account snapshot, orders) per sidecar.
SHORT_CONNECTIONS = 48
# K-line long polls per sidecar: one per channel (48 per source) plus room for a channel moving in.
LONG_POLL_CONNECTIONS = 64
# A localhost connect is instant unless the peer's accept queue is full.
CONNECT_SECONDS = 2.0
WEDGE_SECONDS = 30.0


class SidecarHttpError(OSError):
    def __init__(self, code: str, *, status: int | None = None, payload=None):
        super().__init__(code)
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {}


def is_long_poll(path: str) -> bool:
    """A public-market workbench read that parks until the revision changes (`waitMs=`)."""
    return "waitMs=" in path


def request_timeout(seconds: float) -> httpx2.Timeout:
    return httpx2.Timeout(seconds, connect=min(seconds, CONNECT_SECONDS))


def _restore_cancellation() -> None:
    """anyio's `fail_after` (httpcore's pool wait and connect) can turn a cancellation that lands on
    its own deadline into a timeout; the task still carries the request (`cancelling()`), so a retired
    K-line producer or an abandoned shared read stops instead of carrying on as if the read failed."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


class SidecarHttp:
    def __init__(self, base_url: str, token: str, *, short_connections: int = SHORT_CONNECTIONS,
                 long_poll_connections: int = LONG_POLL_CONNECTIONS):
        self._options = dict(base_url=base_url, headers={"Authorization": f"Bearer {token}"},
                             trust_env=False, follow_redirects=False)
        self._limits = {"async_client": short_connections, "long_poll_client": long_poll_connections}
        self.sync = httpx2.Client(**self._options, limits=self._pool_limits(short_connections))
        self.async_client = self._async_client("async_client")
        self.long_poll_client = self._async_client("long_poll_client")
        self.wedge_seconds = WEDGE_SECONDS
        self.clock = time.monotonic
        self._pool_timeouts_since: dict[str, float] = {}
        self._retired: set[asyncio.Task] = set()

    @staticmethod
    def _pool_limits(connections: int) -> httpx2.Limits:
        return httpx2.Limits(max_connections=connections, max_keepalive_connections=connections)

    def _async_client(self, name: str) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(**self._options, limits=self._pool_limits(self._limits[name]))

    @staticmethod
    def decode(response) -> Any:
        try:
            payload = fast_json.loads(response.content)
        except ValueError as error:
            raise SidecarHttpError("sidecar_invalid_json", status=response.status_code) from error
        if not response.is_success:
            raise SidecarHttpError("sidecar_http_error", status=response.status_code, payload=payload)
        return payload

    def request(self, method, path, payload=None, *, timeout):
        try:
            return self.decode(self.sync.request(method, path, json=payload, timeout=request_timeout(timeout)))
        except httpx2.HTTPError as error:
            raise SidecarHttpError("sidecar_transport_unavailable") from error

    async def request_async(self, method, path, payload=None, *, timeout, long_poll: bool | None = None):
        name = "long_poll_client" if (is_long_poll(path) if long_poll is None else long_poll) else "async_client"
        client = getattr(self, name)
        try:
            response = await client.request(method, path, json=payload, timeout=request_timeout(timeout))
        except httpx2.HTTPError as error:
            if isinstance(error, httpx2.TimeoutException):
                _restore_cancellation()
            if isinstance(error, httpx2.PoolTimeout):
                self._pool_timed_out(name, client)
            else:
                self._pool_timeouts_since.pop(name, None)
            raise SidecarHttpError("sidecar_transport_unavailable") from error
        self._pool_timeouts_since.pop(name, None)
        return self.decode(response)

    def _pool_timed_out(self, name: str, client) -> None:
        """Rebuild a pool that has served nothing but pool timeouts for `wedge_seconds`."""
        now = self.clock()
        since = self._pool_timeouts_since.setdefault(name, now)
        if now - since < self.wedge_seconds or getattr(self, name) is not client:
            return
        self._pool_timeouts_since.pop(name, None)
        log.warning("sidecar_http pool rebuilt name=%s base=%s: only pool timeouts for %.0fs",
                    name, self._options["base_url"], now - since)
        setattr(self, name, self._async_client(name))
        closing = asyncio.get_running_loop().create_task(client.aclose())
        self._retired.add(closing)
        closing.add_done_callback(self._retired.discard)

    async def close(self):
        await self.async_client.aclose()
        await self.long_poll_client.aclose()
        self.sync.close()
        for client in (self.sync, self.async_client, self.long_poll_client):
            client.headers.clear()

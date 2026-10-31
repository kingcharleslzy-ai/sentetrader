"""The sidecar HTTP pools (2026-10-12 容量测试): a request cancelled while it is still connecting
leaves no connection behind, a cancellation anyio turned into a timeout stays a cancellation, a
long poll and a short read never share a pool, and a pool that only ever times out is rebuilt."""
from __future__ import annotations

import asyncio
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx2
import pytest

from trader_runtime.sidecar_http import SidecarHttp, SidecarHttpError

TOKEN = "sidecar-http-fixture-token-0123456789"


class Answer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


async def test_requests_cancelled_while_connecting_leave_no_connection_behind():
    """The sidecar stops accepting (backlog full), callers give up mid-connect, round after round:
    once it accepts again, the next read succeeds. Under httpx 0.28 / httpcore 1.0.9 the cancelled
    connects stayed CONNECTING in the pool and every later read timed out until a restart."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    http = SidecarHttp(f"http://127.0.0.1:{port}", TOKEN, short_connections=4, long_poll_connections=4)
    server = None
    try:
        async def give_up(after):
            async with asyncio.timeout(after):  # a caller's own deadline: the read is cancelled
                await http.request_async("GET", "/x", timeout=5)
        for _ in range(4):
            # More readers than slots, one shared deadline: the slots the connecting readers free go to
            # queued readers that are cancelled in the same instant, before they connect.
            await asyncio.gather(*(give_up(.05) for _ in range(12)), return_exceptions=True)
        server = HTTPServer(("127.0.0.1", port), Answer, bind_and_activate=False)
        server.socket = listener
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
        assert await http.request_async("GET", "/x", timeout=1.5) == {}
    finally:
        if server is not None:
            server.shutdown()
        listener.close()
        await http.close()


class ScriptedClient:
    """An AsyncClient stand-in answering each request from a script of outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.is_closed = False
        self.headers: dict[str, str] = {}

    async def request(self, method, path, json=None, timeout=None):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "ok":
            return httpx2.Response(200, json={"ok": True})
        if outcome == "swallowed-cancel":
            # anyio's fail_after can catch a cancellation landing on its own deadline and raise its
            # timeout instead; the task still carries the cancellation request.
            asyncio.current_task().cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise httpx2.PoolTimeout("pool timeout") from None
        raise outcome

    async def aclose(self):
        self.is_closed = True


async def test_a_cancellation_turned_into_a_timeout_stays_a_cancellation():
    http = SidecarHttp("http://127.0.0.1:9", TOKEN)
    await http.async_client.aclose()
    http.async_client = ScriptedClient(["swallowed-cancel", httpx2.PoolTimeout("plain timeout")])
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.create_task(http.request_async("GET", "/v1/public-market/board", timeout=2))
        # A timeout nobody cancelled is an ordinary transport failure.
        with pytest.raises(SidecarHttpError, match="sidecar_transport_unavailable"):
            await http.request_async("GET", "/v1/public-market/board", timeout=2)
    finally:
        await http.close()


async def test_long_polls_and_short_reads_use_separate_pools():
    http = SidecarHttp("http://127.0.0.1:9", TOKEN)
    assert (http.async_client._transport._pool._max_connections,
            http.long_poll_client._transport._pool._max_connections) == (48, 64)
    for client in (http.async_client, http.long_poll_client):
        await client.aclose()
    http.async_client, http.long_poll_client = ScriptedClient([]), ScriptedClient([])
    await http.request_async("GET", "/v1/public-market/SHFE.rb2610/workbench?duration=60&after=r1&waitMs=10000",
                             timeout=15)
    await http.request_async("GET", "/v1/public-market/quotes?symbols=SHFE.RB2610", timeout=15)
    await http.request_async("GET", "/v1/public-market/SHFE.rb2610/workbench?duration=60", timeout=15)
    assert (http.long_poll_client.calls, http.async_client.calls) == (1, 2)
    await http.close()


async def test_a_pool_that_only_times_out_is_rebuilt_and_a_served_one_never_is():
    http = SidecarHttp("http://127.0.0.1:9", TOKEN)
    now = [100.0]
    http.clock = lambda: now[0]
    await http.async_client.aclose()
    timeout = httpx2.PoolTimeout("pool timeout")
    # Timeouts with a served read in between: busy, not wedged — the pool stays.
    busy = http.async_client = ScriptedClient([timeout, "ok", timeout, "ok", timeout])
    for _ in range(5):
        try:
            await http.request_async("GET", "/v1/public-market/board", timeout=2)
        except SidecarHttpError:
            pass
        now[0] += http.wedge_seconds / 2
    assert http.async_client is busy
    # Nothing but pool timeouts for wedge_seconds: a fresh pool takes over and the stuck one closes.
    wedged = http.async_client = ScriptedClient([timeout] * 4)
    for _ in range(3):
        with pytest.raises(SidecarHttpError):
            await http.request_async("GET", "/v1/public-market/board", timeout=2)
        now[0] += http.wedge_seconds / 2
    assert http.async_client is not wedged and isinstance(http.async_client, httpx2.AsyncClient)
    await asyncio.sleep(0)
    assert wedged.is_closed
    await http.close()

"""Pure-ASGI HTTP middlewares: no BaseHTTPMiddleware task groups in the request path.

A function middleware (`@app.middleware("http")`) runs every request through
anyio task groups and memory object streams. A client that disconnects at the
wrong moment surfaces from there as a framework-only ``ExceptionGroup`` that the
error middleware answers with a 500 and a traceback — an hourly noise line with
no project frame to act on. Plain ASGI middlewares leave nothing in the request
path that can turn a disconnect into a server error; the guard below completes
such requests quietly instead.
"""
from __future__ import annotations

import logging
from typing import Any

import anyio
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..client_compat import MIN_CLIENT_VERSION, compat_state, parse_client_header, runtime_build

# What a transport raises when the client is already gone. uvicorn's own
# httptools/h11 implementations drop such writes silently; these cover every
# other way the disconnect can surface out of `send`.
DISCONNECT_ERRORS = (
    BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
    anyio.BrokenResourceError, anyio.ClosedResourceError,
)


class _ClientGone(Exception):
    """Internal only: the client disconnected before the response finished."""


class DisconnectGuard:
    """Treat a vanished client as a handled outcome: no 500, no traceback."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def guarded_send(message: Message) -> None:
            try:
                await send(message)
            except DISCONNECT_ERRORS as error:
                raise _ClientGone() from error

        try:
            await self.app(scope, receive, guarded_send)
        except _ClientGone:
            pass  # nobody is left to answer; that is not a server error


class OriginAndClientCompat:
    """The origin gate for state-changing API calls and the client-compat answer.

    The two former function middlewares as plain ASGI: a disallowed origin is
    refused with the same 403 body, and every `/api/` and `/healthz` response —
    including that refusal — still carries the compat verdict plus the
    first-seen client build log line.
    """

    def __init__(self, app: ASGIApp, *, origins: set[str], builds_seen: set[tuple[str, str, str | None]],
                 logger: logging.Logger | None = None):
        self.app = app
        self.origins = origins
        self.builds_seen = builds_seen
        self.log = logger or logging.getLogger(__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        headers = {key: value for key, value in scope["headers"]}
        client = parse_client_header(_header_value(headers, b"x-ai-trader-client"))
        state = compat_state(client)
        answers = path.startswith("/api/") or path == "/healthz"

        async def send_with_compat(message: Message) -> None:
            if answers and message["type"] == "http.response.start":
                extra: list[tuple[bytes, bytes]] = [
                    (b"x-ai-trader-client-compat", state.encode()),
                    (b"x-ai-trader-runtime", runtime_build().encode()),
                ]
                if state == "outdated":
                    extra.append((b"x-ai-trader-min-client", MIN_CLIENT_VERSION.encode()))
                message = {**message, "headers": [*message.get("headers", []), *extra]}
                _remember_build(self.builds_seen, client, state, self.log)
            await send(message)

        if path.startswith("/api/") and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            if headers.get(b"origin", b"").decode("latin-1") not in self.origins:
                await JSONResponse(status_code=403, content={"detail": "origin_not_allowed"})(scope, receive, send_with_compat)
                return
        await self.app(scope, receive, send_with_compat)


def _header_value(headers: dict[bytes, bytes], name: bytes) -> str | None:
    value = headers.get(name)
    return value.decode("latin-1") if value is not None else None


def _remember_build(seen: set[tuple[str, str, str | None]], client: Any, state: str,
                    log: logging.Logger) -> None:
    if client is None:
        return
    key = (client.platform, client.version, client.commit)
    if key in seen:
        return
    # Bounded: beyond the cap, forget earlier builds rather than grow forever.
    if len(seen) >= 200:
        seen.clear()
    seen.add(key)
    log.info("client_build_seen platform=%s version=%s build=%s compat=%s",
             client.platform, client.version, client.commit or "-", state)

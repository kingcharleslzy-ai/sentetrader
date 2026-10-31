"""Loopback HTTP API of the read-only MT5 bridge (GET only, Bearer token on every route).

Routes:
  GET /v1/status                                  terminal/account state (always 200)
  GET /v1/symbols?path=Exchange-Futures&root=GC   contract list with properties
  GET /v1/symbols/<name>                          one contract's properties
  GET /v1/quotes?symbols=GCEZ26,CLEZ26            latest quotes (max 50 symbols)
  GET /v1/bars?symbol=GCEZ26&timeframe=M1&count=500&start=0   M1/M5/M15/H1/D1
  GET /v1/depth?symbol=GCEZ26                     market depth (subscribed on first use)
A terminal that is not running, not attachable or disconnected answers 503
{"status": "unavailable", "reason": ...}.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .terminal import DEFAULT_TERMINAL_PATH, BadRequest, Terminal, Unavailable

LOG = logging.getLogger("mt5_bridge")
DEFAULT_PORT = 18930
MIN_TOKEN_LENGTH = 32


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int(query.get(key, [default])[0])
    except ValueError as error:
        raise BadRequest(f"{key}_invalid") from error


def route(terminal: Terminal, path: str, query: dict[str, list[str]]) -> tuple[int, Any]:
    first = lambda key: (query.get(key) or [""])[0].strip()  # noqa: E731
    if path == "/v1/status":
        return 200, terminal.status()
    if path == "/v1/symbols":
        return 200, {"symbols": terminal.symbols(first("path") or None, first("root") or None,
                                                 first("expired") == "1")}
    if path.startswith("/v1/symbols/"):
        record = terminal.symbol(unquote(path.removeprefix("/v1/symbols/")))
        return (404, {"error": "unknown_symbol"}) if record is None else (200, record)
    if path == "/v1/quotes":
        return 200, terminal.quotes(name.strip() for name in first("symbols").split(","))
    if path == "/v1/bars":
        if not first("symbol"):
            raise BadRequest("symbol_required")
        return 200, terminal.bars(first("symbol"), first("timeframe") or "M1",
                                  _int(query, "count", 500), _int(query, "start", 0))
    if path == "/v1/depth":
        if not first("symbol"):
            raise BadRequest("symbol_required")
        return 200, terminal.depth(first("symbol"))
    return 404, {"error": "not_found"}


def make_handler(terminal: Terminal, token: str) -> type[BaseHTTPRequestHandler]:
    expected = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ai-trader-mt5-bridge"
        sys_version = ""

        def _send(self, status: int, body: Any) -> None:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            supplied = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(supplied, expected):
                self._send(401, {"error": "unauthorized"})
                return
            parts = urlsplit(self.path)
            try:
                status, body = route(terminal, parts.path, parse_qs(parts.query))
            except Unavailable as error:
                status, body = 503, {"status": "unavailable", "reason": error.reason, "detail": error.detail}
            except BadRequest as error:
                status, body = 400, {"error": error.reason}
            except Exception:  # one bad read must not take the bridge down
                LOG.exception("request failed: %s", parts.path)
                status, body = 500, {"error": "internal_error"}
            self._send(status, body)

        def _method_not_allowed(self) -> None:
            self._send(405, {"error": "read_only"})

        do_POST = do_PUT = do_PATCH = do_DELETE = _method_not_allowed

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            # Failures only (the Runtime polls); request line and status, never the headers (token).
            if str(code).isdigit() and int(code) >= 400:
                LOG.warning("%s %s %s", self.address_string(), self.requestline, code)

        def log_message(self, format: str, *args: Any) -> None:
            LOG.warning("%s %s", self.address_string(), format % args)

    return Handler


def make_server(terminal: Terminal, token: str, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("mt5_bridge_host_must_be_loopback")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError("mt5_bridge_token_too_short")
    server = ThreadingHTTPServer((host, port), make_handler(terminal, token))
    server.daemon_threads = True
    return server


def main() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_dir = os.environ.get("MT5_BRIDGE_LOG_DIR")
    if log_dir:
        handlers.append(TimedRotatingFileHandler(Path(log_dir) / "bridge.log", when="midnight",
                                                 backupCount=14, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    token_file = os.environ.get("MT5_BRIDGE_TOKEN_FILE")
    if not token_file:
        raise SystemExit("MT5_BRIDGE_TOKEN_FILE is required")
    token = Path(token_file).read_text(encoding="utf-8").strip()
    import MetaTrader5  # Windows-only official package; imported here so tests run anywhere

    terminal = Terminal(MetaTrader5, os.environ.get("MT5_TERMINAL_PATH", DEFAULT_TERMINAL_PATH))
    host = os.environ.get("MT5_BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("MT5_BRIDGE_PORT", DEFAULT_PORT))
    server = make_server(terminal, token, host, port)
    LOG.info("mt5 bridge listening on %s:%s (read-only)", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()

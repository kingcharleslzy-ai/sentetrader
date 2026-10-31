"""TqKq 接入池：一个控制面进程，按需起停 `main.py` 接入子进程。

多人接入 M1a（2026-09-22）：每个快期账号一个接入进程，由池经 stdin 传入凭据
（不落盘、不进命令行、不进环境变量），子进程监听池分配的端口；Runtime 按账户
动态装配/回收。控制面沿用 sidecar 的 Bearer token 鉴权。

每个子进程每次启动各有一枚随机 token（start 答给调用方）和 childId（列表可见）：
子进程的账户、持仓、委托接口只认自己的 token，池的控制 token 在子进程上只开公共
行情。端口换了主人后，拿旧 token 的调用一律 401——串号不靠“端口不复用”来防
（2026-10-12 审查）。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import selectors
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from typing import Any

from .main import process_is_alive, tqkq_error_code, utc_now

POOL_PORT_ENV = "AI_TRADER_TQKQ_POOL_PORT"
POOL_MAX_ENV = "AI_TRADER_TQKQ_POOL_MAX"
POOL_BASE_PORT_ENV = "AI_TRADER_TQKQ_POOL_BASE_PORT"
POOL_FIXTURE_ENV = "AI_TRADER_TQKQ_POOL_FIXTURE"
TOKEN_FILE_ENV = "AI_TRADER_READONLY_SIDECAR_TOKEN_FILE"
TOKEN_ENV = "AI_TRADER_READONLY_SIDECAR_TOKEN"
SYMBOLS_ENV = "AI_TRADER_TQKQ_SYMBOLS"
DEFAULT_SYMBOLS = "SHFE.au2612,SHFE.rb2610"
DEFAULT_PORT = 19010
DEFAULT_MAX = 8
DEFAULT_BASE_PORT = 19100
# The pool waits for the child's first snapshot (login included); the verify
# child only proves the credentials.
START_TIMEOUT_SECONDS = 60.0
VERIFY_TIMEOUT_SECONDS = 60.0
READY_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 10.0
PROBE_INTERVAL_SECONDS = 0.5
# A rejected login is cached so a bad password cannot become a restart storm.
AUTH_FAILURE_CACHE_SECONDS = 600.0
# A port a child just left is not handed to the next child for a while. Not the
# security boundary (each child's own token is: a stale caller is refused there);
# it only keeps a crashed account's leftover traffic off a neighbour.
PORT_QUARANTINE_SECONDS = 300.0
ACCOUNT_ID_PATTERN = re.compile(r"[A-Za-z0-9:_-]{4,120}")
# Child status codes that mean the credentials themselves are refused.
FATAL_START_CODES = frozenset({"tqkq_authentication_failed", "tqkq_credentials_missing"})
READY_DATA_STATES = frozenset({"normal", "simulated"})
# Child verdicts → the control-plane reasons the Runtime and the login page understand.
VERIFY_REASONS = {"tqkq_authentication_failed": "auth_failed", "tqkq_credentials_missing": "auth_failed",
                  "tqkq_permission_denied": "permission_denied"}


class PoolStartError(RuntimeError):
    """A child that could not be started; `reason` is one of auth_failed /
    permission_denied / timeout / capacity_full / start_failed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class PoolEntry:
    account_id: str
    port: int
    pid: int
    started_at: str
    process: Any = field(repr=False, default=None)
    exit_code: int | None = None
    # This spawn's own identity: a random id (listed) and a random Bearer token
    # (handed only to whoever starts or takes the account over). A caller that
    # reaches any other child with this token is refused there, so a port that
    # changed hands can never serve one account's caller another account's data.
    child_id: str = ""
    token: str = field(repr=False, default="")
    # 行情专用 (2026-10-13): started for a market-source account — a data-only session that
    # serves public market data and no account, never a TqKq trading session.
    market_only: bool = False

    def alive(self) -> bool:
        process = self.process
        if process is not None:
            return process.poll() is None
        return process_is_alive(self.pid)


def _rss_kib(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    try:
        output = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2.0, check=False,
        ).stdout.strip()
        return int(output) if output.isdigit() else None
    except (OSError, ValueError):
        return None


def pool_symbols() -> list[str]:
    raw = os.getenv(SYMBOLS_ENV, DEFAULT_SYMBOLS)
    return [item.strip() for item in raw.split(",") if item.strip()] or [DEFAULT_SYMBOLS.split(",")[0]]


def _read_config() -> dict[str, Any]:
    token_file = os.getenv(TOKEN_FILE_ENV, "")
    if token_file:
        with open(token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
    else:
        token = os.getenv(TOKEN_ENV, "").strip()
    if len(token) < 32:
        raise RuntimeError("pool_token_required")
    return {
        "token": token,
        "port": int(os.getenv(POOL_PORT_ENV, str(DEFAULT_PORT))),
        "max_accounts": max(1, int(os.getenv(POOL_MAX_ENV, str(DEFAULT_MAX)))),
        "base_port": int(os.getenv(POOL_BASE_PORT_ENV, str(DEFAULT_BASE_PORT))),
        "symbols": pool_symbols(),
        "fixture": os.getenv(POOL_FIXTURE_ENV) == "1",
    }


class PoolManager:
    """Owns one child process per TqKq account; every method is thread-safe
    (the control plane serves each request on its own thread)."""

    def __init__(
        self, *, token: str, port: int, max_accounts: int, base_port: int,
        symbols: list[str], fixture: bool = False,
    ) -> None:
        self.token = token
        self.port = port
        self.max_accounts = max_accounts
        self.base_port = base_port
        self.symbols = tuple(symbols)
        self.fixture = fixture
        self._lock = threading.RLock()
        self._entries: dict[str, PoolEntry] = {}
        self._auth_failures: dict[str, float] = {}
        self._released_ports: dict[int, float] = {}

    # -- child processes -----------------------------------------------------

    def _spawn(self, account_id: str, port: int, username: str, password: str,
               symbols: list[str], child_token: str, market_only: bool = False) -> Any:
        config = {
            # The child serves its account only to this spawn's token; the
            # pool's control token opens nothing there but the public market.
            "token": child_token,
            "publicToken": self.token,
            "port": port,
            "symbols": symbols,
            # The child requires >= 32 chars; account ids are shorter, so the
            # pool derives a stable per-account id instead.
            "instanceId": hashlib.sha256(f"tqkq-pool:{account_id}".encode()).hexdigest(),
            "parentPid": 0 if os.getpid() == 1 else os.getpid(),
            "mode": "fixture" if self.fixture else "tqkq-simulation",
            "marketOnly": market_only,
            "username": username,
            "password": password,
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "tqkq_readonly_sidecar.main", "--config-stdin"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, close_fds=True,
        )
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(config, ensure_ascii=False) + "\n")
        process.stdin.flush()
        process.stdin.close()
        return process

    def _probe(self, port: int, token: str) -> dict[str, Any] | None:
        """The child's /healthz status document, or None while its HTTP is down."""
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/healthz",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=2.0) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def _wait_ready(self, process: Any) -> None:
        """Wait for the child's readiness line (its HTTP server has bound)."""
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            deadline = time.monotonic() + READY_TIMEOUT_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or process.poll() is not None:
                    raise PoolStartError("start_failed")
                if selector.select(remaining):
                    line = process.stdout.readline()
                    if not line:
                        raise PoolStartError("start_failed")
                    try:
                        ready = json.loads(line)
                    except ValueError:
                        continue
                    if ready.get("status") == "ready":
                        return
        finally:
            selector.close()

    def _release_port(self, port: int) -> None:
        self._released_ports[port] = time.monotonic()

    def _allocate_port(self) -> int:
        now = time.monotonic()
        self._released_ports = {
            port: at for port, at in self._released_ports.items()
            if now - at < PORT_QUARANTINE_SECONDS
        }
        # An exited entry keeps its port until its own account restarts; a freed
        # port rests in quarantine before another child may take it.
        used = {entry.port for entry in self._entries.values()} | set(self._released_ports)
        used.add(self.port)
        candidate = self.base_port
        while candidate in used:
            candidate += 1
        return candidate

    # -- control-plane operations --------------------------------------------

    def start(self, account_id: str, username: str, password: str,
              symbols: list[str] | None = None, *, market_only: bool = False) -> dict[str, Any]:
        """Start (or take over) the account's child. `market_only` asks for a 行情专用 child: a
        running trading child already serves the public market and is taken over as it is; a
        running market-only child asked for trading is replaced by a trading one."""
        with self._lock:
            entry = self._entries.get(account_id)
            if entry is not None and entry.alive() and (market_only or not entry.market_only):
                # Idempotent takeover: the running child, with its token.
                return {**self._payload(entry), "token": entry.token}
            if entry is not None:
                del self._entries[account_id]  # crashed child (or market-only → trading): restart below
                self._release_port(entry.port)
                if entry.alive():
                    self._terminate(entry)
            failed_at = self._auth_failures.get(account_id)
            if failed_at is not None:
                if time.monotonic() - failed_at < AUTH_FAILURE_CACHE_SECONDS:
                    raise PoolStartError("auth_failed")
                del self._auth_failures[account_id]
            running = sum(1 for item in self._entries.values() if item.alive())
            if running >= self.max_accounts:
                raise PoolStartError("capacity_full")
            port = self._allocate_port()
            child_symbols = list(symbols or self.symbols)
            child_token = secrets.token_urlsafe(32)
            process = self._spawn(account_id, port, username, password, child_symbols, child_token,
                                  market_only)
            entry = PoolEntry(
                account_id=account_id, port=port, pid=process.pid,
                started_at=utc_now(), process=process,
                child_id=secrets.token_hex(16), token=child_token, market_only=market_only,
            )
            self._entries[account_id] = entry
        try:
            self._wait_ready(process)
            self._wait_healthy(entry)
        except PoolStartError as error:
            self._terminate(entry)
            # Control-plane reasons are the stable contract (auth_failed /
            # permission_denied / timeout / start_failed); the child's own codes
            # are normalized into it. A credential refusal is cached.
            reason = VERIFY_REASONS.get(error.reason, error.reason)
            if reason in {"auth_failed", "permission_denied"}:
                self._auth_failures[account_id] = time.monotonic()
            with self._lock:
                if self._entries.get(account_id) is entry:
                    del self._entries[account_id]
                self._release_port(entry.port)
            raise PoolStartError(reason) from None
        return {**self._payload(entry), "token": entry.token}

    def _wait_healthy(self, entry: PoolEntry) -> None:
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not entry.alive():
                raise PoolStartError("start_failed")
            status = self._probe(entry.port, entry.token)
            if status is not None:
                data_status = str(status.get("dataStatus") or "")
                if data_status in READY_DATA_STATES:
                    return
                if data_status == "unavailable":
                    code = str(status.get("dataErrorCode") or "")
                    if code in FATAL_START_CODES:
                        raise PoolStartError(code)
                    if code == "tqkq_permission_denied":
                        raise PoolStartError("permission_denied")
            time.sleep(PROBE_INTERVAL_SECONDS)
        raise PoolStartError("timeout")

    def stop(self, account_id: str) -> dict[str, Any]:
        with self._lock:
            entry = self._entries.pop(account_id, None)
            if entry is not None:
                self._release_port(entry.port)
        if entry is None:
            return {"accountId": account_id, "stopped": False}
        self._terminate(entry)
        return {"accountId": account_id, "stopped": True}

    def _terminate(self, entry: PoolEntry) -> None:
        process = entry.process
        if process is None or process.poll() is not None:
            entry.exit_code = process.returncode if process is not None else None
            return
        try:
            process.terminate()
        except OSError:
            pass
        try:
            entry.exit_code = process.wait(timeout=STOP_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
            entry.exit_code = process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except OSError:
            entry.exit_code = None

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._payload(entry) for entry in self._entries.values()]

    @staticmethod
    def _payload(entry: PoolEntry) -> dict[str, Any]:
        alive = entry.alive()
        if not alive and entry.exit_code is None and entry.process is not None:
            entry.exit_code = entry.process.poll()  # a crash noticed from outside
        return {
            "accountId": entry.account_id,
            "port": entry.port,
            "childId": entry.child_id,
            "pid": entry.pid,
            "startedAt": entry.started_at,
            "rssKb": _rss_kib(entry.pid) if alive else None,
            "status": "running" if alive else "exited",
            "marketOnly": entry.market_only,
            **({"exitCode": entry.exit_code} if not alive and entry.exit_code is not None else {}),
        }

    def stop_all(self) -> None:
        with self._lock:
            entries = [self._entries.pop(account_id) for account_id in list(self._entries)]
        for entry in entries:
            self._terminate(entry)

    # -- credential verification --------------------------------------------

    def _spawn_verify(self, username: str, password: str) -> Any:
        return subprocess.Popen(
            [sys.executable, "-m", "tqkq_readonly_sidecar.pool", "--verify-stdin"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, close_fds=True,
        )

    def verify(self, username: str, password: str) -> dict[str, Any]:
        """A throwaway login-only TqApi in its own process: nothing is kept, and
        a hang can be killed outright."""
        if self.fixture:
            # Fixture pools run without real TqKq credentials; accept any pair
            # so the M1b login flow can be exercised end-to-end locally.
            return {"ok": True, "kqUsername": username}
        try:
            process = self._spawn_verify(username, password)
        except OSError:
            return {"ok": False, "reason": "start_failed"}
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(json.dumps({"username": username, "password": password}) + "\n")
            process.stdin.flush()
            process.stdin.close()
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            try:
                deadline = time.monotonic() + VERIFY_TIMEOUT_SECONDS
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return {"ok": False, "reason": "timeout"}
                    if selector.select(remaining):
                        line = process.stdout.readline()
                        if line:
                            try:
                                result = json.loads(line)
                            except ValueError:
                                continue
                            if isinstance(result, dict) and "ok" in result:
                                if not result.get("ok"):
                                    # Same stable contract as start(): the login page maps
                                    # auth_failed / permission_denied / timeout, not raw
                                    # tqkq_* codes (09-22 production acceptance showed the
                                    # generic fallback text on a wrong password).
                                    result["reason"] = VERIFY_REASONS.get(str(result.get("reason") or ""),
                                                                          str(result.get("reason") or "start_failed"))
                                return result
                    if process.poll() is not None:
                        return {"ok": False, "reason": "start_failed"}
            finally:
                selector.close()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
        return {"ok": False, "reason": "start_failed"}


def run_verify_login() -> int:
    """`--verify-stdin` child: prove the credentials with one TqApi and exit."""
    line = sys.stdin.readline(16 * 1024)
    try:
        payload = json.loads(line or "{}")
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        if not username or not password:
            raise RuntimeError("tqkq_credentials_missing")
    except ValueError:
        print(json.dumps({"ok": False, "reason": "tqkq_credentials_missing"}), flush=True)
        return 0
    try:
        from tqsdk import TqApi, TqAuth, TqKq  # type: ignore[import-not-found]

        api = TqApi(TqKq(), auth=TqAuth(username, password), debug=False, disable_print=True)
        try:
            api.wait_update(deadline=time.time() + 5.0)
        finally:
            api.close()
        print(json.dumps({"ok": True, "kqUsername": username}, ensure_ascii=False), flush=True)
    except Exception as error:  # noqa: BLE001 - the reason is the product of this child
        print(json.dumps({"ok": False, "reason": tqkq_error_code(error)}), flush=True)
    return 0


class PoolHandler(BaseHTTPRequestHandler):
    server: PoolServer

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def _json(self, status: int, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return bool(supplied) and hmac.compare_digest(supplied, self.server.manager.token)

    def _body(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid_content_length") from None
        if size <= 0 or size > 16 * 1024:
            raise ValueError("invalid_request_size")
        payload = json.loads(self.rfile.read(size))
        if not isinstance(payload, dict):
            raise ValueError("invalid_json_object")
        return payload

    @staticmethod
    def _credentials(payload: dict[str, Any]) -> tuple[str, str]:
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        if not username or not password or len(username) > 200 or len(password) > 400:
            raise ValueError("invalid_credentials")
        return username, password

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "sidecar_auth_required"})
            return
        path = self.path.split("?", 1)[0]
        if path == "/pool/accounts":
            accounts = self.server.manager.entries()
            self._json(HTTPStatus.OK, {
                "accounts": accounts,
                "capacity": {"max": self.server.manager.max_accounts,
                             "running": sum(1 for item in accounts if item["status"] == "running")},
            })
            return
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok", "pool": True})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "pool_route_not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "sidecar_auth_required"})
            return
        path = self.path.split("?", 1)[0]
        manager = self.server.manager
        try:
            if path == "/pool/verify":
                username, password = self._credentials(self._body())
                self._json(HTTPStatus.OK, manager.verify(username, password))
                return
            match = re.fullmatch(r"/pool/accounts/([A-Za-z0-9:_-]{4,120})/(start|stop)", path)
            if match is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "pool_route_not_found"})
                return
            account_id, action = match.group(1), match.group(2)
            if action == "start":
                payload = self._body()
                username, password = self._credentials(payload)
                symbols = payload.get("symbols")
                if symbols is not None and (
                    not isinstance(symbols, list)
                    or not all(isinstance(item, str) and item for item in symbols)
                ):
                    raise ValueError("invalid_symbols")
                market_only = payload.get("marketOnly", False)
                if not isinstance(market_only, bool):
                    raise ValueError("invalid_market_only")
                result = manager.start(account_id, username, password, symbols, market_only=market_only)
                self._json(HTTPStatus.OK, result)
                return
            self._json(HTTPStatus.OK, manager.stop(account_id))
        except ValueError:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "invalid_pool_request"})
        except PoolStartError as error:
            status = (HTTPStatus.SERVICE_UNAVAILABLE if error.reason == "capacity_full"
                      else HTTPStatus.CONFLICT)
            self._json(status, {"error": error.reason})

    def _unsupported_write(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})

    do_PUT = _unsupported_write
    do_PATCH = _unsupported_write
    do_DELETE = _unsupported_write


class PoolServer(ThreadingHTTPServer):
    daemon_threads = True
    # listen backlog (同 PaperServer #663, 10-12 容量测试): the default 5 drops connections from a burst
    # of Runtime calls instead of queueing them.
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], manager: PoolManager) -> None:
        super().__init__(address, PoolHandler)
        self.manager = manager

    def server_bind(self) -> None:
        # Same as the sidecar: a localhost control plane never needs reverse DNS.
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


def serve(manager: PoolManager, *, port: int | None = None) -> int:
    server = PoolServer(("127.0.0.1", port if port is not None else manager.port), manager)
    actual_port = int(server.server_address[1])
    stop = threading.Event()

    def shutdown(*_: Any) -> None:
        if not stop.is_set():
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(json.dumps({"status": "ready", "port": actual_port, "pool": True}), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        manager.stop_all()
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Trader TqKq access pool")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--verify-stdin", action="store_true")
    arguments = parser.parse_args()
    if arguments.verify_stdin:
        return run_verify_login()
    try:
        config = _read_config()
        manager = PoolManager(
            token=config["token"], port=config["port"], max_accounts=config["max_accounts"],
            base_port=config["base_port"], symbols=config["symbols"], fixture=config["fixture"],
        )
        return serve(manager)
    except Exception as error:
        print(json.dumps({"status": "failed", "error": type(error).__name__}), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

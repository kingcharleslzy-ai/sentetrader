"""Host-only application session shared by CLI and MCP; never returned to models."""
import asyncio
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import httpx


def default_connection_file() -> Path:
    if sys.platform == "win32":
        root = Path(os.getenv("APPDATA") or Path.home() / "AppData/Roaming")
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local/share")
    return root / "com.kingcharles.ai-trader/private-credentials/agent-cli.json"


DEFAULT_CONNECTION_FILE = default_connection_file()

ACCOUNT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,99}$")


def account_headers(account: str | None) -> dict[str, str]:
    """The header naming which of the user's accounts a call acts on (an id from GET /api/accounts);
    none keeps the session's or default account. Business data, unlike the credentials above."""
    if not account:
        return {}
    if not ACCOUNT_ID.match(account):
        raise ValueError("账户 id 格式不对，请用 GET /api/accounts 列出的 id")
    return {"X-AI-Trader-Account": account}


class HostHttpPool:
    """Reuse connections without pinning an old account, port or login session."""
    def __init__(self, path: Path, client_factory=httpx.AsyncClient):
        self.path, self.factory = path, client_factory
        self.lock = asyncio.Lock()
        self.options = None
        self.client = None
        self.borrowers = {}

    @asynccontextmanager
    async def borrow(self):
        # Credentials are read by the host at each call, never supplied by a tool.
        options = connection_options(self.path)
        retired = None
        async with self.lock:
            if self.client is None or options != self.options:
                previous = self.client
                self.client = self.factory(**options)
                self.options = options
                self.borrowers[self.client] = 0
                if previous is not None and self.borrowers.get(previous, 0) == 0:
                    retired = previous
                    self.borrowers.pop(previous, None)
            client = self.client
            self.borrowers[client] += 1
        if retired is not None:
            await retired.aclose()
        try:
            yield client
        finally:
            retired = None
            async with self.lock:
                self.borrowers[client] -= 1
                if client is not self.client and not self.borrowers[client]:
                    self.borrowers.pop(client)
                    retired = client
            if retired is not None:
                await retired.aclose()

    async def request(self, method, path, **kwargs):
        started = monotonic()
        async with self.borrow() as client:
            response = await client.request(method, path, **kwargs)
        logging.getLogger(__name__).info("agent_http method=%s path=%s status=%s elapsed_ms=%.1f",
            method, path, response.status_code, (monotonic() - started) * 1000)
        return response

    async def aclose(self):
        async with self.lock:
            clients = list(self.borrowers)
            self.client, self.options = None, None
        for client in clients:
            await client.aclose()


def connection_options(path: Path) -> dict:
    with path.open() as stream:
        if os.name != "nt" and os.fstat(stream.fileno()).st_mode & 0o077:
            raise ValueError("连接文件权限必须仅限当前用户")
        connection = json.load(stream)
    if connection.get("authMode") == "guest":
        parsed = urlsplit(connection.get("url", ""))
        local = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
                 and not (parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment))
        if not local and connection.get("url") != "https://ai-runtime.qilinshuzhi.com:8443":
            raise ValueError("游客连接地址不受支持")
        token = connection.get("token", "")
        if not isinstance(token, str) or not token.startswith("guest.") or not 32 <= len(token[6:]) <= 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in token[6:]):
            raise ValueError("游客会话无效或已过期")
        return {"base_url": connection["url"], "timeout": 180, "trust_env": False,
                "follow_redirects": False,
                "headers": {"authorization": "Bearer " + token, "origin": "tauri://localhost"}}
    if connection.get("authMode") == "owner-password":
        if connection.get("url") != "https://ai-runtime.qilinshuzhi.com:8443":
            raise ValueError("云端连接地址不受支持")
        token = connection.get("token", "")
        if not isinstance(token, str) or not token.startswith("v1.") or len(token) > 4096 or any(c.isspace() for c in token):
            raise ValueError("请先在客户端登录 Sente 账号")
        return {"base_url": connection["url"], "timeout": 180, "trust_env": False,
                "follow_redirects": False,
                "headers": {"authorization": "Bearer " + token, "origin": "tauri://localhost"}}
    parsed = urlsplit(connection["url"])
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("连接地址必须是本机服务")
    token = connection["token"]
    if not isinstance(token, str) or not token:
        raise ValueError("本机连接尚未就绪")
    return {"base_url": connection["url"], "timeout": 180, "trust_env": False,
            "follow_redirects": False,
            "headers": {"authorization": "Bearer " + token, "origin": "tauri://localhost"}}

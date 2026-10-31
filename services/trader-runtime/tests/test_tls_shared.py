"""短命 httpx 客户端共用一份 TLS 上下文（2026-10-13 第二轮省 CPU）：RSSHub 路由、交易所/行业页面、
模拟引擎控制调用不再每次请求现建 SSLContext；其余行为不变——仍是每次请求自己的连接与 cookie、
同样的请求头与超时、同样的证书校验配置。"""
from __future__ import annotations

import json
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from trader_runtime import research_live
from trader_runtime.paper_accounts import PaperEngineClient
from trader_runtime.research_live import LiveSources
from trader_runtime.tls import shared_ssl_context


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict[str, str | None]] = []

    def log_message(self, *args) -> None:  # noqa: D401 - quiet test server
        pass

    def _answer(self) -> None:
        type(self).seen.append({"path": self.path, "cookie": self.headers.get("Cookie"),
                                "ua": self.headers.get("User-Agent"),
                                "auth": self.headers.get("Authorization")})
        body = (json.dumps({"items": [], "ok": True}).encode() if not self.path.startswith("/page")
                else "<html>公告</html>".encode())
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if not self.path.startswith("/page") else "text/html; charset=utf-8")
        self.send_header("Set-Cookie", "visit=1; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer
    do_POST = _answer


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_shared_context_is_the_one_httpx_would_build() -> None:
    context = shared_ssl_context()
    assert context is shared_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    import httpx
    fresh = httpx.create_ssl_context()
    assert context.cert_store_stats() == fresh.cert_store_stats()
    assert context.minimum_version == fresh.minimum_version and context.options == fresh.options


async def test_requests_build_no_tls_context_and_keep_their_own_cookies(
    server: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_ssl_context()  # built once, before counting
    built: list[int] = []
    real = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: built.append(1) or real(*a, **k))
    monkeypatch.setattr(research_live.GENTLE, "sleep", lambda seconds: None)
    sources = LiveSources()
    for _ in range(3):
        assert sources.rsshub(server, "/jin10") == {"items": [], "ok": True}
        assert sources._fetch_page("127.0.0.1", f"{server}/page") == "<html>公告</html>"
    engine = PaperEngineClient(server, "engine-token")
    assert (await engine.health()) == {"items": [], "ok": True}
    assert built == []
    # Each request is still its own client: the cookie a response set never rides the next request.
    assert [entry["cookie"] for entry in _Handler.seen] == [None] * 7
    pages = [entry for entry in _Handler.seen if entry["path"] == "/page"]
    assert pages and all(entry["ua"] == research_live.NOTICE_HEADERS["User-Agent"] for entry in pages)
    assert _Handler.seen[-1]["auth"] == "Bearer engine-token"
    assert _Handler.seen[0]["path"] == f"/jin10?format=json&limit={research_live.RSSHUB_LIMIT}"

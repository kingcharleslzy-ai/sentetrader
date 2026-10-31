"""后台「服务器」：`GET /api/admin/servers` 的管理员只读视图。

The assertions describe what a deployment observes: the owner reads the
project hosts' gauges shaped from the infra console's metrics API (never the
network in these tests — a fake fetch stands in), a member does not even learn
the surface exists, a missing token answers honestly with `unconfigured`, and
an unreachable console serves the last good copy marked stale instead of
inventing freshness.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import trader_runtime.routes.system as system_routes
from trader_runtime.auth import hash_owner_password
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.infra_console import InfraConsoleMetrics, InfraConsoleUnavailable
from trader_runtime.main import create_app
from trader_runtime.store.trading_accounts import scoped_owner_user_id

ORIGIN = "https://qilinshuzhi.com"
PASSWORD = "correct-demo-password"
PASSWORD_HASH = hash_owner_password(PASSWORD, salt=b"0123456789abcdef")
SESSION_SECRET = "owner-session-secret-for-tests-more-than-32-bytes"
FRIEND_PASSWORD = "a-solid-password-123"


def owner_headers() -> dict[str, str]:
    return {"origin": ORIGIN}


def console_payload() -> dict[str, Any]:
    """What the console's /api/metrics answers for our two sample hosts."""
    return {
        "generatedAt": "2026-10-04T08:00:00.000Z",
        "hosts": [
            {"id": "host-a", "name": "主机甲", "platform": "Ubuntu 24.04.4 LTS",
             "role": "AI Trader：生产Runtime/Sidecar",
             "checkedAt": "2026-10-04T07:58:00.000Z", "reachability": "online",
             "resources": {"cpuCores": 8, "cpuPercent": 41.5, "load1": 0.84, "load5": 0.9, "load15": 0.95,
                           "memTotalMB": 16000.0, "memUsedMB": 8000.0, "diskTotalGB": 100.0,
                           "diskUsedGB": 40.0, "uptimeSeconds": 864000, "containers": 3},
             "services": [{"id": "svc-1", "status": "running"}, {"id": "svc-2", "status": "failed"}],
             "series": [{"at": "2026-10-04T07:00:00.000Z", "cpu": 40.0, "mem": 50.0, "disk": 40.0},
                        {"at": "2026-10-04T07:05:00.000Z", "cpu": 42.0, "mem": 51.0, "disk": 40.0}]},
            {"id": "host-b", "name": "后台 Mac mini", "platform": "macOS",
             "role": "AI Trader：CI runner",
             "checkedAt": "2026-10-04T07:57:00.000Z", "reachability": "online",
             "resources": {"cpuCores": 10, "cpuPercent": 12.0, "load1": 2.1,
                           "memTotalMB": 16384.0, "memUsedMB": 10516.4,
                           "diskTotalGB": 228.3, "diskUsedGB": 189.0, "uptimeSeconds": 643572},
             "services": [{"id": "runner", "status": "running"}],
             "series": [{"at": "2026-10-04T07:00:00.000Z", "cpu": 10.0, "mem": 64.0, "disk": 82.7}]},
        ],
    }


class FakeConsole:
    """The HTTP seam with recorded calls, a mutable clock and a failure switch."""

    def __init__(self, payload: dict[str, Any] | None, *, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail
        self.calls: list[dict[str, str]] = []
        self.now = 0.0

    async def fetch(self, url: str, *, params: dict[str, str], headers: dict[str, str]) -> dict[str, Any]:
        self.calls.append({"url": url, "range": params["range"], "hosts": params["hosts"],
                           "auth": headers.get("Authorization", "")})
        if self.fail or self.payload is None:
            raise InfraConsoleUnavailable("infra_console_unreachable")
        return copy.deepcopy(self.payload)

    def client(self) -> InfraConsoleMetrics:
        return InfraConsoleMetrics("https://console.test/houtai", "token-zzz",
                                   ("host-b", "host-a"), fetch=self.fetch, clock=lambda: self.now)


@pytest.fixture
def servers_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-zhipu-fixture")
    application = create_app(
        tmp_path / "servers.sqlite3",
        agent_mode="fake",
        auth_mode="owner-password",
        owner_username="charles",
        owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET,
        cors_origins={ORIGIN},
        registration_enabled=True,
        data_mode="tqkq-sidecar",
        data_provider=FakeReadOnlyDataProvider(),
        agent_provider="deepseek",
        deepseek_api_key="sk-test-deepseek-key-000001",
    )
    yield application
    application.state.store.close()


@pytest.fixture
async def friend_and_owner(servers_app):
    transport = ASGITransport(app=servers_app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as friend,
        AsyncClient(transport=transport, base_url="http://test") as owner,
    ):
        login = await owner.post(
            "/api/auth/owner", json={"username": "charles", "password": PASSWORD}, headers=owner_headers())
        assert login.status_code == 200
        code = (await owner.post(
            "/api/auth/owner/invite-codes", json={"daysValid": 14}, headers=owner_headers(),
        )).json()["inviteCode"]
        registered = await friend.post(
            "/api/auth/owner/register",
            json={"username": "friend", "password": FRIEND_PASSWORD, "inviteCode": code},
            headers=owner_headers(),
        )
        assert registered.status_code == 200
        yield friend, owner


def use_console(monkeypatch: pytest.MonkeyPatch, console: FakeConsole) -> None:
    monkeypatch.setattr(system_routes, "infra_console_from_env", console.client)


async def test_servers_is_owner_only(servers_app, friend_and_owner):
    friend, owner = friend_and_owner
    # Not even discoverable for a member; anonymous gets 401.
    assert (await friend.get("/api/admin/servers", headers=owner_headers())).status_code == 404
    async with AsyncClient(transport=ASGITransport(app=servers_app), base_url="http://test") as anonymous:
        assert (await anonymous.get("/api/admin/servers")).status_code == 401


async def test_without_a_token_the_surface_answers_unconfigured(
        servers_app, friend_and_owner, monkeypatch):
    _friend, owner = friend_and_owner
    monkeypatch.delenv("AI_TRADER_INFRA_CONSOLE_TOKEN", raising=False)
    monkeypatch.delenv("AI_TRADER_INFRA_CONSOLE_TOKEN_FILE", raising=False)
    response = await owner.get("/api/admin/servers", headers=owner_headers())
    assert response.status_code == 200
    assert response.json() == {"status": "unconfigured"}


async def test_maps_console_hosts_in_the_configured_order(
        servers_app, friend_and_owner, monkeypatch):
    _friend, owner = friend_and_owner
    console = FakeConsole(console_payload())
    use_console(monkeypatch, console)
    response = await owner.get("/api/admin/servers", headers=owner_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["updatedAt"] == "2026-10-04T08:00:00.000Z"
    assert body["fetchedAt"]
    # The configured order decides the display order, not the console's listing.
    assert [host["id"] for host in body["hosts"]] == ["host-b", "host-a"]
    first, second = body["hosts"]
    assert first["displayName"] == "后台 Mac mini"
    assert first["platform"] == "macOS"
    assert first["role"] == "AI Trader：CI runner"
    assert first["checkedAt"] == "2026-10-04T07:57:00.000Z"
    assert first["reachability"] == "online"
    assert first["resources"] == console_payload()["hosts"][1]["resources"]
    assert first["series"] == console_payload()["hosts"][1]["series"]
    assert first["servicesSummary"] == {"total": 1, "running": 1, "failed": 0}
    assert second["servicesSummary"] == {"total": 2, "running": 1, "failed": 1}
    assert console.calls == [{"url": "https://console.test/houtai/api/metrics", "range": "24h",
                              "hosts": "host-b,host-a", "auth": "Bearer token-zzz"}]


async def test_rejects_unknown_range_and_passes_valid_ones_through(
        servers_app, friend_and_owner, monkeypatch):
    _friend, owner = friend_and_owner
    console = FakeConsole(console_payload())
    use_console(monkeypatch, console)
    assert (await owner.get("/api/admin/servers", params={"range": "8h"},
                            headers=owner_headers())).status_code == 400
    assert (await owner.get("/api/admin/servers", params={"range": "7d"},
                            headers=owner_headers())).status_code == 200
    assert [call["range"] for call in console.calls] == ["7d"]


async def test_a_failing_console_serves_the_stale_copy(
        servers_app, friend_and_owner, monkeypatch):
    _friend, owner = friend_and_owner
    console = FakeConsole(console_payload())
    use_console(monkeypatch, console)
    fresh = (await owner.get("/api/admin/servers", headers=owner_headers())).json()
    assert "stale" not in fresh
    console.now += 61.0  # past the 60-second cache
    console.fail = True
    stale = (await owner.get("/api/admin/servers", headers=owner_headers())).json()
    assert stale["stale"] is True
    assert stale["hosts"] == fresh["hosts"]
    assert stale["updatedAt"] == fresh["updatedAt"]
    assert len(console.calls) == 2


async def test_a_console_that_never_answered_is_a_bad_gateway(
        servers_app, friend_and_owner, monkeypatch):
    _friend, owner = friend_and_owner
    use_console(monkeypatch, FakeConsole(None, fail=True))
    response = await owner.get("/api/admin/servers", headers=owner_headers())
    assert response.status_code == 502
    assert response.json()["detail"] == "infra_console_unavailable"


async def test_the_sixty_second_cache_prevents_repeat_fetches(
        servers_app, friend_and_owner, monkeypatch):
    _friend, owner = friend_and_owner
    console = FakeConsole(console_payload())
    use_console(monkeypatch, console)
    for _ in range(3):
        assert (await owner.get("/api/admin/servers", headers=owner_headers())).status_code == 200
    assert len(console.calls) == 1
    # A different range is a different cache entry, so it fetches once more.
    assert (await owner.get("/api/admin/servers", params={"range": "1h"},
                            headers=owner_headers())).status_code == 200
    assert [call["range"] for call in console.calls] == ["24h", "1h"]
    # Nothing about the reads leaked into the store.
    assert servers_app.state.store.list_account_grants(user_id=scoped_owner_user_id("friend")) == []


KEPT: list[bytes] = []


async def test_memory_diagnostics_names_the_line_that_keeps_memory(servers_app, friend_and_owner):
    """后台内存诊断 (2026-10-26): off until the owner starts it; then the read names the source line whose
    allocations stayed alive since the baseline; a member cannot see or start it; stopping turns it off."""
    friend, owner = friend_and_owner
    assert (await friend.post("/api/admin/memory/trace", headers=owner_headers())).status_code == 403
    assert (await friend.get("/api/admin/memory", headers=owner_headers())).status_code == 404
    off = (await owner.get("/api/admin/memory", headers=owner_headers())).json()
    assert off["tracing"] is False and "top" not in off
    try:
        started = await owner.post("/api/admin/memory/trace", params={"frames": 5}, headers=owner_headers())
        assert started.status_code == 200 and started.json()["tracing"] is True
        KEPT.extend(bytes(64 * 1024) for _ in range(64))  # 4 MB kept alive past the baseline
        body = (await owner.get("/api/admin/memory", params={"types": "true"}, headers=owner_headers())).json()
        assert body["growthKb"] >= 4000 and body["types"]["dict"] > 0
        assert "test_admin_servers.py" in body["top"][0]["stack"][0] and body["top"][0]["sizeKb"] >= 4000
    finally:
        KEPT.clear()
        stopped = (await owner.delete("/api/admin/memory/trace", headers=owner_headers())).json()
    assert stopped["tracing"] is False

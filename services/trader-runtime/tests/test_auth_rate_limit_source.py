"""The login/register failure budget keys on the real visitor address.

The cloud deployment runs uvicorn with `--proxy-headers --forwarded-allow-ips=*`
behind the 8443 nginx. That nginx *overwrites* X-Forwarded-For with the address
realip restored (direct peer, or the real visitor named by the trusted website
relay), so `request.client` is the visitor on both public paths and no request
header can name another address. These tests pin that contract with the real
uvicorn ProxyHeadersMiddleware around the real app (#735); the middleware here
stands in for the deployed uvicorn flags, not for a real nginx run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from trader_runtime.auth import hash_owner_password
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.main import create_app

ORIGIN = "https://qilinshuzhi.com"
PASSWORD = "correct-demo-password"
PASSWORD_HASH = hash_owner_password(PASSWORD, salt=b"0123456789abcdef")
SESSION_SECRET = "owner-session-secret-for-tests-more-than-32-bytes"
PEER = "203.0.113.9"


@pytest.fixture
def auth_app(tmp_path: Path):
    application = create_app(
        tmp_path / "accounts.sqlite3",
        agent_mode="fake",
        auth_mode="owner-password",
        owner_username="charles",
        owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET,
        cors_origins={ORIGIN},
        data_mode="tqkq-sidecar",
        data_provider=FakeReadOnlyDataProvider(),
        agent_provider="deepseek",
        registration_enabled=True,
        deepseek_api_key="sk-test-deepseek-key-000001",
    )
    yield application
    application.state.store.close()


def canonical_client(application, *, trust: str = "*") -> AsyncClient:
    """The cloud shape: uvicorn's own ProxyHeadersMiddleware (deployed flags)
    in front of the real app, TCP peer fixed like one nginx exit."""
    transport = ASGITransport(
        app=ProxyHeadersMiddleware(application, trusted_hosts=trust),
        client=(PEER, 51000),
    )
    return AsyncClient(transport=transport, base_url="http://test", headers={"origin": ORIGIN})


async def _register(client: AsyncClient, i: int, *, forwarded: str, real_ip: str | None = None):
    headers = {"x-forwarded-for": forwarded}
    if real_ip is not None:
        # nginx overwrites this before the app; sending it anyway proves the
        # app never takes a raw header as an identity.
        headers["x-real-ip"] = real_ip
    return await client.post(
        "/api/auth/owner/register",
        json={"username": f"guess{i}", "password": "a-solid-password-123", "inviteCode": "wrong-code"},
        headers=headers,
    )


async def test_register_budget_survives_header_claims_under_canonical_proxy(auth_app):
    # The deployed nginx emits exactly one XFF value: the peer address it
    # restored. The attacker may rotate any other header; the budget stays on
    # the one real address (register: 20 failures per IP, unchanged).
    async with canonical_client(auth_app) as client:
        statuses = [
            (await _register(client, i, forwarded=PEER, real_ip=f"10.255.{i}.1")).status_code
            for i in range(21)
        ]
    assert statuses[:20] == [403] * 20
    assert statuses[20] == 429


async def test_owner_lock_survives_header_claims_under_canonical_proxy(auth_app):
    async with canonical_client(auth_app) as client:
        statuses = [
            (await client.post(
                "/api/auth/owner",
                json={"username": "charles", "password": "not-the-password"},
                headers={"x-forwarded-for": PEER, "x-real-ip": f"10.255.{i}.1"},
            )).status_code
            for i in range(6)
        ]
    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429


async def test_untrusted_peer_ignores_forged_forwarded_headers(auth_app):
    # uvicorn's default trust list does not name this peer: the middleware
    # leaves request.client alone and every forwarded header is ignored —
    # the dev/direct-connection semantics.
    async with canonical_client(auth_app, trust="127.0.0.1") as client:
        statuses = [
            (await _register(client, i, forwarded=f"10.9.{i}.1, {PEER}", real_ip=f"10.255.{i}.1")).status_code
            for i in range(21)
        ]
    assert statuses[:20] == [403] * 20
    assert statuses[20] == 429


async def test_bare_asgi_connection_keys_on_the_tcp_peer(auth_app):
    # No proxy middleware at all: only the real peer exists.
    transport = ASGITransport(app=auth_app, client=(PEER, 51000))
    async with AsyncClient(transport=transport, base_url="http://test", headers={"origin": ORIGIN}) as client:
        statuses = [
            (await _register(client, i, forwarded=f"10.9.{i}.1", real_ip=f"10.255.{i}.1")).status_code
            for i in range(21)
        ]
    assert statuses[:20] == [403] * 20
    assert statuses[20] == 429


async def test_distinct_visitors_behind_one_exit_keep_separate_budgets(auth_app):
    # Both public paths name the real visitor in the single XFF value: two
    # people behind the same exit (direct 8443 or the website relay) each get
    # their own register budget.
    async with canonical_client(auth_app) as client:
        for i in range(20):
            response = await _register(client, i, forwarded="198.51.100.1")
            assert response.status_code == 403
        # A second visitor from the same exit still has its own budget...
        fresh = await _register(client, 99, forwarded="198.51.100.2")
        assert fresh.status_code == 403
        # ...while the first one is locked out by its own 20 failures.
        locked = await _register(client, 100, forwarded="198.51.100.1")
        assert locked.status_code == 429

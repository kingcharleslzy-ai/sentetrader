from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.auth import hash_owner_password
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.main import create_app
from trader_runtime.store import RuntimeStore
from trader_runtime.store.trading_accounts import scoped_owner_user_id

ORIGIN = "https://qilinshuzhi.com"
PASSWORD = "correct-demo-password"
PASSWORD_HASH = hash_owner_password(PASSWORD, salt=b"0123456789abcdef")
SESSION_SECRET = "owner-session-secret-for-tests-more-than-32-bytes"


@pytest.fixture
def cloud_app(tmp_path: Path):
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


@pytest.fixture
async def cloud_client(cloud_app):
    application = cloud_app
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def owner_headers():
    return {"origin": ORIGIN}


async def login(client: AsyncClient, username: str, password: str):
    return await client.post(
        "/api/auth/owner", json={"username": username, "password": password},
        headers=owner_headers(),
    )


async def test_invite_code_registration_and_isolated_sessions(cloud_client: AsyncClient):
    created = await cloud_client.post(
        "/api/auth/owner/invite-codes", headers=owner_headers(),
    )
    assert created.status_code == 401  # anonymous caller has no owner session

    owner_login = await login(cloud_client, "charles", PASSWORD)
    assert owner_login.status_code == 200
    assert owner_login.json()["role"] == "owner"

    created = await cloud_client.post(
        "/api/auth/owner/invite-codes", headers=owner_headers(),
    )
    assert created.status_code == 200
    invite_code = created.json()["inviteCode"]

    bad = await cloud_client.post(
        "/api/auth/owner/register",
        json={"username": "friend", "password": "a-solid-password-123", "inviteCode": "wrong"},
        headers=owner_headers(),
    )
    assert bad.status_code == 403
    assert bad.json()["detail"] == "invite_code_invalid"

    registered = await cloud_client.post(
        "/api/auth/owner/register",
        json={"username": "friend", "password": "a-solid-password-123", "inviteCode": invite_code},
        headers=owner_headers(),
    )
    assert registered.status_code == 200
    assert registered.json()["role"] == "user"
    assert registered.json()["userId"] != owner_login.json()["userId"]

    reused = await cloud_client.post(
        "/api/auth/owner/register",
        json={"username": "other", "password": "a-solid-password-123", "inviteCode": invite_code},
        headers=owner_headers(),
    )
    assert reused.status_code == 403

    friend_login = await login(cloud_client, "friend", "a-solid-password-123")
    assert friend_login.status_code == 200
    assert friend_login.json()["role"] == "user"

    # Registration logged the new account in; the owner cookie on this client
    # is replaced by the friend session.
    friend_session = await cloud_client.get("/api/auth/owner/status")
    assert friend_session.json()["username"] == "friend"
    assert friend_session.json()["authenticated"] is True
    assert friend_session.json()["role"] == "user"


async def test_registered_user_sessions_are_isolated(cloud_app, cloud_client: AsyncClient):
    store = cloud_app.state.store
    await login(cloud_client, "charles", PASSWORD)
    code = (
        await cloud_client.post("/api/auth/owner/invite-codes", headers=owner_headers())
    ).json()["inviteCode"]
    registered = await cloud_client.post(
        "/api/auth/owner/register",
        json={"username": "friend", "password": "a-solid-password-123", "inviteCode": code},
        headers=owner_headers(),
    )
    friend_id = registered.json()["userId"]
    # 2026-09-23 isolation: an unbound member gets the business state, never
    # the owner's default account.
    session = await cloud_client.post(
        "/api/sessions", json={"mode": "local", "agentId": "native"},
        headers=owner_headers(),
    )
    assert session.status_code == 403
    assert session.json()["detail"] == "tqkq_account_required"
    # With an explicit operator grant (the accounts-file path), the session
    # runs under the member's own id.
    store.grant_account(
        account_id=store.default_trading_account_id, user_id=friend_id,
        role="operator", granted_by=scoped_owner_user_id("charles"),
    )
    session = await cloud_client.post(
        "/api/sessions", json={"mode": "local", "agentId": "native"},
        headers=owner_headers(),
    )
    assert session.status_code == 201
    assert store.get_session(session.json()["sessionId"])["userId"] == friend_id


async def test_models_catalog_member_gating_and_preference(cloud_app, cloud_client: AsyncClient):
    await login(cloud_client, "charles", PASSWORD)
    catalog = await cloud_client.get("/api/models")
    assert catalog.status_code == 200
    payload = catalog.json()
    slugs = {entry["slug"]: entry for entry in payload["models"]}
    assert slugs["deepseek-flash"]["selectable"] is True
    assert slugs["deepseek-flash"]["memberOnly"] is False
    assert payload["current"] == "deepseek-flash"

    chosen = await cloud_client.put(
        "/api/models/preference", json={"model": "deepseek-flash"},
        headers=owner_headers(),
    )
    assert chosen.status_code == 200
    assert chosen.json()["current"] == "deepseek-flash"

    code = (
        await cloud_client.post("/api/auth/owner/invite-codes", headers=owner_headers())
    ).json()["inviteCode"]
    await cloud_client.post(
        "/api/auth/owner/register",
        json={"username": "friend", "password": "a-solid-password-123", "inviteCode": code},
        headers=owner_headers(),
    )
    member_view = await cloud_client.get("/api/models")
    member_entry = {
        entry["slug"]: entry for entry in member_view.json()["models"]
    }.get("k3.1-pro")
    if member_entry is not None:
        assert member_entry["selectable"] is False
        assert member_entry["memberOnly"] is True


async def test_daily_token_quota_blocks_new_messages(cloud_app, cloud_client: AsyncClient, monkeypatch):
    monkeypatch.setenv("AI_TRADER_DAILY_TOKEN_LIMIT", "100")
    await login(cloud_client, "charles", PASSWORD)
    session = await cloud_client.post(
        "/api/sessions", json={"mode": "local", "agentId": "native"},
        headers=owner_headers(),
    )
    assert session.status_code == 201, session.text
    session_id = session.json()["sessionId"]
    from datetime import UTC, datetime
    cloud_app.state.store.add_token_usage(
        cloud_app.state.store.get_session(session_id)["userId"],
        datetime.now(UTC).date().isoformat(), 100,
    )
    blocked = await cloud_client.post(
        f"/api/sessions/{session_id}/messages", json={"text": "你好"},
        headers=owner_headers(),
    )
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "daily_token_quota_exceeded"


async def test_invite_code_reads_follow_the_safe_method_origin_contract(cloud_app, cloud_client: AsyncClient):
    """#529: a same-origin browser GET carries no Origin (Fetch §3.2 — the web
    build moved to the same-origin /trader-runtime relay in #733), so the pure
    read keeps the shared safe-method Origin contract that `auth.user` already
    follows: a missing Origin is the browser default, an explicit one is still
    checked. Writes keep the strict per-request check."""
    anonymous = await cloud_client.get("/api/auth/owner/invite-codes")
    assert anonymous.status_code == 401  # anonymous stays out before any origin talk

    await login(cloud_client, "charles", PASSWORD)
    created = await cloud_client.post("/api/auth/owner/invite-codes", headers=owner_headers())
    assert created.status_code == 200
    invite_code = created.json()["inviteCode"]
    code_id = next(
        item["id"] for item in cloud_app.state.store.list_invite_codes()
        if item["code"] == invite_code
    )

    listed = await cloud_client.get("/api/auth/owner/invite-codes")
    assert listed.status_code == 200
    assert [item["code"] for item in listed.json()] == [invite_code]

    explicit = await cloud_client.get("/api/auth/owner/invite-codes", headers=owner_headers())
    assert explicit.status_code == 200

    hostile = await cloud_client.get(
        "/api/auth/owner/invite-codes", headers={"origin": "https://evil.example"}
    )
    assert hostile.status_code == 403
    assert hostile.json()["detail"] == "origin_not_allowed"

    # Writes still demand the header, missing or hostile.
    assert (await cloud_client.post("/api/auth/owner/invite-codes")).status_code == 403
    assert (await cloud_client.post(
        "/api/auth/owner/invite-codes", headers={"origin": "https://evil.example"}
    )).status_code == 403
    assert (await cloud_client.post(
        f"/api/auth/owner/invite-codes/{code_id}/disable"
    )).status_code == 403
    assert (await cloud_client.post("/api/auth/owner/logout")).status_code == 403

    # A member session never reaches the owner list, with or without Origin.
    registered = await cloud_client.post(
        "/api/auth/owner/register",
        json={"username": "friend", "password": "a-solid-password-123", "inviteCode": invite_code},
        headers=owner_headers(),
    )
    assert registered.status_code == 200
    assert (await cloud_client.get("/api/auth/owner/invite-codes")).status_code == 403
    assert (await cloud_client.get(
        "/api/auth/owner/invite-codes", headers=owner_headers()
    )).status_code == 403


async def test_web_target_position_on_a_cloud_session_reaches_the_card_path(cloud_client: AsyncClient):
    """下单台「目标持仓」在网页（cloud 会话）上走账户自己的模拟执行服务：审计 10-13 发现路由建工具时
    没带账户的 autonomy，cloud 会话一律回 proposal_service_unavailable，卡永远出不来。"""
    assert (await login(cloud_client, "charles", PASSWORD)).status_code == 200
    session = (await cloud_client.post("/api/sessions", json={"mode": "cloud", "agentId": "native"},
                                       headers=owner_headers())).json()
    response = await cloud_client.post("/api/simulation/target-position/proposals", json={
        "sessionId": session["sessionId"], "symbol": "SHFE.au2612", "targetNet": 2, "reason": "目标持仓"},
        headers=owner_headers())
    assert response.status_code == 201
    body = response.json()
    assert body.get("errorCode") != "proposal_service_unavailable"
    # 假数据账户已持有 2 手多单：目标 +2 = 已是目标持仓，如实说不用下单。
    assert body["cardIssued"] is False
    assert body["userFacing"]["candidateStatus"] == "当前已经是目标持仓，不用下单"


async def test_failed_user_insert_leaves_the_invite_code_unspent(cloud_app, cloud_client: AsyncClient, monkeypatch):
    # #932 X4: spending the code and creating the user are one transaction. A
    # name taken between the pre-check and the insert must not burn the code.
    assert (await login(cloud_client, "charles", PASSWORD)).status_code == 200
    invite_code = (await cloud_client.post(
        "/api/auth/owner/invite-codes", headers=owner_headers())).json()["inviteCode"]
    store = cloud_app.state.store
    store.create_cloud_user(username="racer", password_hash="x", invited_by=None)
    real_get = store.get_cloud_user
    monkeypatch.setattr(store, "get_cloud_user",
                        lambda name: None if name == "racer" else real_get(name))

    lost = await cloud_client.post(
        "/api/auth/owner/register",
        json={"username": "racer", "password": "a-solid-password-123", "inviteCode": invite_code},
        headers=owner_headers(),
    )
    assert lost.status_code == 409 and lost.json()["detail"] == "username_taken"
    [code] = store.list_invite_codes()
    assert (code["status"], code["useCount"], code["usedBy"]) == ("active", 0, [])

    won = await cloud_client.post(
        "/api/auth/owner/register",
        json={"username": "friend", "password": "a-solid-password-123", "inviteCode": invite_code},
        headers=owner_headers(),
    )
    assert won.status_code == 200
    [code] = store.list_invite_codes()
    assert (code["status"], code["useCount"]) == ("used_up", 1)
    assert [use["username"] for use in code["usedBy"]] == ["friend"]


def test_one_shot_invite_code_admits_one_of_two_concurrent_registrations(tmp_path: Path):
    # #932 X4: two connections (as two workers would hold) race on one one-shot
    # code; BEGIN IMMEDIATE serialises check-and-spend, so exactly one user lands.
    path = tmp_path / "race.sqlite3"
    stores = [RuntimeStore(path), RuntimeStore(path)]
    try:
        code_hash = sha256(b"one-shot").hexdigest()
        assert stores[0].create_invite_code(
            code_hash=code_hash, created_by="charles",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        barrier = threading.Barrier(2)
        results: dict[str, str | None] = {}

        def register(store: RuntimeStore, username: str) -> None:
            barrier.wait()
            results[username] = store.register_invited_user(
                code_hash=code_hash, username=username, password_hash="x",
                invited_by="invite-code", landing_plan=lambda plan: plan,
            )

        threads = [threading.Thread(target=register, args=(store, name))
                   for store, name in zip(stores, ("alice", "bob"), strict=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        winners = [name for name, plan in results.items() if plan is not None]
        assert len(results) == 2 and len(winners) == 1
        loser = ({"alice", "bob"} - set(winners)).pop()
        assert stores[0].get_cloud_user(winners[0]) is not None
        assert stores[0].get_cloud_user(loser) is None
        [code] = stores[0].list_invite_codes()
        assert code["useCount"] == 1
        assert [use["username"] for use in code["usedBy"]] == winners
    finally:
        for store in stores:
            store.close()

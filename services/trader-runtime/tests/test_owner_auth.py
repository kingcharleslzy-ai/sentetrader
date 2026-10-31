from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.auth import (
    OWNER_COOKIE_NAME,
    TQKQ_REGISTER_URL,
    OwnerAuth,
    hash_owner_password,
    verify_owner_password,
)
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.main import create_app

ORIGIN = "https://qilinshuzhi.com"
PASSWORD = "correct-demo-password"
PASSWORD_HASH = hash_owner_password(PASSWORD, salt=b"0123456789abcdef")
SESSION_SECRET = "owner-session-secret-for-tests-more-than-32-bytes"


@pytest.fixture
def owner_app(tmp_path: Path, request: pytest.FixtureRequest):
    application = create_app(
        tmp_path / "owner.sqlite3",
        agent_mode="fake",
        auth_mode="owner-password",
        owner_username="charles",
        owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET,
        cors_origins={ORIGIN, "tauri://localhost"} if getattr(request, "param", False) else {ORIGIN},
        data_mode="tqkq-sidecar",
        data_provider=FakeReadOnlyDataProvider(),
        agent_provider="deepseek",
        agent_credential_configured=True,
    )
    yield application
    application.state.store.close()


def test_owner_password_hash_is_salted_and_verified_without_plaintext():
    encoded = hash_owner_password(PASSWORD, salt=b"fedcba9876543210")

    assert PASSWORD not in encoded
    assert verify_owner_password(PASSWORD, encoded) is True
    assert verify_owner_password("wrong", encoded) is False
    assert verify_owner_password(PASSWORD, "not-a-valid-hash") is False


@pytest.mark.asyncio
async def test_owner_login_binds_server_side_profiles_and_uses_secure_cookie(owner_app):
    async with AsyncClient(
        transport=ASGITransport(app=owner_app),
        base_url="https://runtime.example.test",
        headers={"Origin": ORIGIN},
    ) as client:
        anonymous = await client.get("/api/auth/owner/status")
        assert anonymous.json() == {
            "authenticated": False,
            "authMode": "owner-password",
            "authProvider": "owner-password",
            # M1b: the login page reads this to offer the TqKq direct form.
            "tqkqLogin": {
                "enabled": False,
                "inviteRequired": True,
                "registerUrl": TQKQ_REGISTER_URL,
            },
            # 本站模拟 (2026-10-03): anonymous register copy reads the deployment
            # flag before any login — same field the post-login bindings carry.
            "paperConfigured": False,
        }
        assert (await client.get("/api/runtime/status?mode=cloud")).status_code == 401

        login = await client.post(
            "/api/auth/owner",
            json={"username": "charles", "password": PASSWORD},
        )
        assert login.status_code == 200
        payload = login.json()
        assert payload["username"] == "charles"
        assert payload["bindings"] == {
            "profileId": "owner-default",
            "tqkqConfigured": True,
            "tqkqMode": "read-only",
            # 本站模拟 (2026-10-03): 没配引擎就是 False —— 登录页如实展示。
            "paperConfigured": False,
            "modelProvider": "deepseek",
            "modelCredentialConfigured": True,
            "liveTrading": False,
        }
        cookie = login.cookies.get(OWNER_COOKIE_NAME)
        assert cookie and PASSWORD not in cookie and PASSWORD_HASH not in cookie
        set_cookie = login.headers["set-cookie"].lower()
        assert "httponly" in set_cookie
        assert "secure" in set_cookie
        assert "samesite=lax" in set_cookie

        status = await client.get("/api/auth/owner/status")
        assert status.json()["authenticated"] is True
        assert status.json()["role"] == "owner"
        runtime = await client.get("/api/runtime/status?mode=cloud")
        assert runtime.status_code == 200
        assert runtime.json()["liveTrading"] is False

        logout = await client.post("/api/auth/owner/logout")
        assert logout.status_code == 200
        assert (await client.get("/api/runtime/status?mode=cloud")).status_code == 401


@pytest.mark.asyncio
async def test_owner_login_rejects_wrong_origin_and_rate_limits_failures(owner_app):
    async with AsyncClient(
        transport=ASGITransport(app=owner_app),
        base_url="https://runtime.example.test",
        headers={"Origin": "https://evil.example"},
    ) as wrong_origin:
        rejected = await wrong_origin.post(
            "/api/auth/owner",
            json={"username": "charles", "password": PASSWORD},
        )
        assert rejected.status_code == 403

    async with AsyncClient(
        transport=ASGITransport(app=owner_app),
        base_url="https://runtime.example.test",
        headers={"Origin": ORIGIN},
    ) as client:
        for _ in range(5):
            rejected = await client.post(
                "/api/auth/owner",
                json={"username": "charles", "password": "wrong"},
            )
            assert rejected.status_code == 401
        locked = await client.post(
            "/api/auth/owner",
            json={"username": "charles", "password": PASSWORD},
        )
        assert locked.status_code == 429


@pytest.mark.asyncio
async def test_owner_login_random_usernames_share_one_bounded_ip_limit(owner_app):
    async with AsyncClient(
        transport=ASGITransport(app=owner_app),
        base_url="https://runtime.example.test",
        headers={"Origin": ORIGIN},
    ) as client:
        for index in range(20):
            rejected = await client.post(
                "/api/auth/owner",
                json={"username": f"unknown-{index}", "password": "wrong"},
            )
            assert rejected.status_code == 401
        locked = await client.post(
            "/api/auth/owner",
            json={"username": "charles", "password": PASSWORD},
        )

    assert locked.status_code == 429
    assert len(owner_app.state.auth._failures) == 1


@pytest.mark.asyncio
async def test_owner_profile_can_be_loaded_from_secret_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    values = {
        "owner-password-hash": PASSWORD_HASH,
        "owner-session-secret": SESSION_SECRET,
        "deepseek-api-key": "sk-deepseek-test-only-value",
        "sidecar-token": "sidecar-token-for-tests-at-least-32-bytes",
    }
    for name, value in values.items():
        (secret_root / name).write_text(value, encoding="utf-8")
    monkeypatch.setenv("AI_TRADER_OWNER_USERNAME", "charles")
    monkeypatch.setenv("AI_TRADER_OWNER_PASSWORD_HASH_FILE", str(secret_root / "owner-password-hash"))
    monkeypatch.setenv("AI_TRADER_OWNER_SESSION_SECRET_FILE", str(secret_root / "owner-session-secret"))
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(secret_root / "deepseek-api-key"))
    monkeypatch.setenv("AI_TRADER_READONLY_SIDECAR_TOKEN_FILE", str(secret_root / "sidecar-token"))
    monkeypatch.setenv("AI_TRADER_READONLY_SIDECAR_URL", "http://127.0.0.1:43123")
    monkeypatch.setenv("AI_TRADER_SIMULATION_TRADING", "1")

    application = create_app(
        tmp_path / "secret-owner.sqlite3",
        agent_mode="fake",
        auth_mode="owner-password",
        cors_origins={ORIGIN},
        data_mode="tqkq-sidecar",
        agent_provider="deepseek",
        runtime_state_root=tmp_path / "state",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="https://runtime.example.test",
            headers={"Origin": ORIGIN},
        ) as client:
            login = await client.post(
                "/api/auth/owner",
                json={"username": "charles", "password": PASSWORD},
            )
            assert login.status_code == 200
            assert login.json()["bindings"] == {
                "profileId": "owner-default",
                "tqkqConfigured": True,
                "tqkqMode": "simulation",
                "paperConfigured": False,
                "modelProvider": "deepseek",
                "modelCredentialConfigured": True,
                "liveTrading": False,
            }
            assert application.state.simulation_orders.enabled is True
    finally:
        application.state.store.close()


@pytest.mark.parametrize("owner_app", [True], indirect=True)
async def test_native_owner_sessions_roam_without_credentials_and_logout_one_device(owner_app):
    from trader_runtime.auth import OwnerAuth
    native_origin = 'tauri://localhost'
    owner_app.state.auth.allowed_login_origins.add(native_origin)
    async with AsyncClient(transport=ASGITransport(app=owner_app), base_url='https://runtime.example',
                           headers={'origin': native_origin}) as a, AsyncClient(
        transport=ASGITransport(app=owner_app), base_url='https://runtime.example',
        headers={'origin': native_origin}) as b:
        login_a = await a.post('/api/auth/owner', json={'username':'charles', 'password':PASSWORD})
        login_b = await b.post('/api/auth/owner', json={'username':'charles', 'password':PASSWORD})
        token_a, token_b = login_a.json()['accessToken'], login_b.json()['accessToken']
        assert token_a != token_b
        assert login_a.headers['cache-control'] == 'no-store'
        assert login_a.json()['bindings'] == login_b.json()['bindings']
        assert 'password' not in login_a.json()['bindings'] and 'apiKey' not in login_a.json()['bindings']
        a.cookies.clear(); b.cookies.clear()
        a.headers['authorization'] = 'Bearer '+token_a
        b.headers['authorization'] = 'Bearer '+token_b
        assert (await a.get('/api/auth/owner/status')).json()['authenticated']
        assert (await b.get('/api/auth/owner/status')).json()['authenticated']
        # MCP and embedded tools use the same authenticated operation boundary.
        assert (await a.post('/api/agent/call', json={'operation':'GET /api/app/config'})).status_code == 200
        assert (await b.post('/api/agent/call', json={'operation':'GET /api/app/config'})).status_code == 200
        assert (await a.post('/api/auth/owner/logout')).status_code == 200
        assert (await a.post('/api/agent/call', json={'operation':'GET /api/app/config'})).status_code == 401
        assert (await b.post('/api/agent/call', json={'operation':'GET /api/app/config'})).status_code == 200
        # Recreating authentication does not revive a revoked device token.
        restarted = OwnerAuth(username='charles', password_hash=PASSWORD_HASH, session_secret=SESSION_SECRET,
                              allowed_login_origins={native_origin}, session_store=owner_app.state.store)
        with pytest.raises(Exception) as rejected:
            restarted.validate(token_a)
        assert rejected.value.status_code == 401
        assert restarted.validate(token_b).username == 'charles'


async def test_web_login_keeps_http_only_cookie_and_does_not_export_session(owner_app):
    async with AsyncClient(transport=ASGITransport(app=owner_app), base_url='https://runtime.example',
                           headers={'origin':ORIGIN}) as client:
        result = await client.post('/api/auth/owner', json={'username':'charles','password':PASSWORD})
        assert result.status_code == 200 and 'accessToken' not in result.json()
        assert 'httponly' in result.headers['set-cookie'].lower()
        denied = await client.post('/api/auth/owner', headers={'origin':'tauri://localhost'},
                                   json={'username':'charles','password':PASSWORD})
        assert denied.status_code == 403


async def test_cloud_owner_can_use_its_simulation_gateway_without_relaxing_other_cloud_modes(tmp_path):
    from test_simulation_orders import Gateway, NormalProvider
    gateway = Gateway()
    app = create_app(tmp_path/'owner-simulation.sqlite3', agent_mode='fake', auth_mode='owner-password',
        owner_username='charles', owner_password_hash=PASSWORD_HASH, owner_session_secret=SESSION_SECRET,
        cors_origins={ORIGIN}, data_provider=NormalProvider(), simulation_gateway=gateway)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='https://runtime.example', headers={'origin':ORIGIN}) as c:
            assert (await c.post('/api/auth/owner', json={'username':'charles','password':PASSWORD})).status_code == 200
            runtime = (await c.get('/api/runtime/status?mode=cloud')).json()
            assert runtime['simulationTrading'] is True and runtime['liveTrading'] is False
            sid = (await c.post('/api/sessions', json={'mode':'cloud','agentId':'native'})).json()['sessionId']
            started = await c.post('/api/ai-execution/start', json={'sessionId':sid,'symbols':['SHFE.au2612'],
                'permissions':['open','cancel','close','stop'],'maxOrderVolume':1,'maxOrders':1,'durationMinutes':5})
            assert started.status_code == 200
            assert started.json()['status'] == 'ACTIVE' and started.json()['liveTrading'] == 'disabled'
            from trader_runtime.trader_tools import TraderReadOnlyTools
            user = app.state.store.get_session(sid)['userId']
            tools = TraderReadOnlyTools(app.state.data_provider, lambda mode: {}, proposals=app.state.proposals, autonomy=app.state.autonomy)
            tools.app_capabilities = app.state.app_capabilities
            tools.app_capabilities.bind_session(sid, user, {'cookie':OWNER_COOKIE_NAME+'='+c.cookies.get(OWNER_COOKIE_NAME)})
            actual, _ = await tools.call_async(tool='get_ai_execution_status', arguments={}, turn_id='owner-test',
                                               mode='cloud', session_id=sid, user_id=user)
            assert actual['success'] is True
            blocked, _ = await tools.call_async(tool='get_ai_execution_status', arguments={}, turn_id='owner-test',
                                                mode='untrusted', session_id=sid, user_id=user)
            assert blocked['success'] is False
            assert gateway.submit_count == 0
    finally:
        await app.state.runs.shutdown(); await app.state.adapter.shutdown(); await app.state.broker.shutdown(); app.state.store.close()
    local = create_app(tmp_path/'local.sqlite3', agent_mode='fake', data_provider=NormalProvider(), simulation_gateway=Gateway())
    try:
        assert not local.state.autonomy.supports_mode('cloud')
    finally:
        local.state.store.close()


def test_session_of_cloud_account_without_password_is_rejected_not_500():
    # 2026-09-25 production: GET /api/auth/owner/status answered 500 ('NoneType'.encode) for a
    # session whose cloud account has no password hash (created but never set up).
    class Users:
        record = {"passwordHash": "set-at-issue", "role": "member"}

        def get_cloud_user(self, username):
            return dict(self.record) if username == "member-a" else None

    users = Users()
    auth = OwnerAuth(username="charles", password_hash=PASSWORD_HASH, session_secret=SESSION_SECRET,
                     allowed_login_origins={ORIGIN}, user_store=users)
    token = auth._issue_session("member-a", "set-at-issue")
    assert auth.validate(token).username == "member-a"
    users.record = {"passwordHash": None, "role": "member"}
    with pytest.raises(Exception) as rejected:
        auth.validate(token)
    assert rejected.value.status_code == 401


async def test_owner_changes_password_keeps_this_device_and_logs_out_others(owner_app):
    # 设置 → 账户 → 修改密码（2026-10-21）：这台设备换新会话继续用，其他设备随旧密码失效；
    # 当前密码输错与登录同一套锁定。
    new_password = "brand-new-password-2026"
    async with AsyncClient(transport=ASGITransport(app=owner_app), base_url="https://runtime.example",
                           headers={"origin": ORIGIN}) as here, AsyncClient(
            transport=ASGITransport(app=owner_app), base_url="https://runtime.example",
            headers={"origin": ORIGIN}) as other:
        for client in (here, other):
            assert (await client.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
        wrong = await here.post("/api/auth/owner/password",
                                json={"currentPassword": "wrong", "newPassword": new_password})
        assert wrong.status_code == 401 and wrong.json()["detail"] == "invalid_current_password"
        short = await here.post("/api/auth/owner/password",
                                json={"currentPassword": PASSWORD, "newPassword": "short"})
        assert short.status_code == 422
        changed = await here.post("/api/auth/owner/password",
                                  json={"currentPassword": PASSWORD, "newPassword": new_password})
        assert changed.status_code == 200 and changed.json()["status"] == "password_changed"
        assert "accessToken" not in changed.json()
        assert (await here.get("/api/auth/owner/status")).json()["authenticated"] is True
        assert (await other.get("/api/auth/owner/status")).json()["authenticated"] is False
        other.cookies.clear()
        old = await other.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})
        assert old.status_code == 401
        assert (await other.post("/api/auth/owner", json={"username": "charles",
                                                          "password": new_password})).status_code == 200
    # 重启后仍用改过的密码（存在库里，不靠部署配置）。
    restarted = OwnerAuth(username="charles", password_hash=PASSWORD_HASH, session_secret=SESSION_SECRET,
                          allowed_login_origins={ORIGIN}, user_store=owner_app.state.store)
    assert verify_owner_password(new_password, restarted._owner_hash())


async def test_wrong_current_password_counts_toward_login_lockout(owner_app):
    async with AsyncClient(transport=ASGITransport(app=owner_app), base_url="https://runtime.example",
                           headers={"origin": ORIGIN}) as client:
        assert (await client.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
        for _ in range(5):
            assert (await client.post("/api/auth/owner/password", json={
                "currentPassword": "wrong", "newPassword": "brand-new-password-2026"})).status_code == 401
        locked = await client.post("/api/auth/owner/password", json={
            "currentPassword": PASSWORD, "newPassword": "brand-new-password-2026"})
        assert locked.status_code == 429


async def test_admin_resets_member_password_and_tqkq_accounts_are_refused(owner_app):
    store = owner_app.state.store
    store.create_cloud_user(username="member-a", password_hash=hash_owner_password("member-old-password"),
                            invited_by="charles")
    store.create_cloud_user(username="tq-user", password_hash="!unusable", invited_by="tqkq-login")
    async with AsyncClient(transport=ASGITransport(app=owner_app), base_url="https://runtime.example",
                           headers={"origin": ORIGIN}) as admin, AsyncClient(
            transport=ASGITransport(app=owner_app), base_url="https://runtime.example",
            headers={"origin": ORIGIN}) as member:
        assert (await member.post("/api/auth/owner", json={"username": "member-a",
                                                           "password": "member-old-password"})).status_code == 200
        # 成员自己不能重置别人的密码（后台接口对非管理员不存在）。
        assert (await member.post("/api/admin/users/member-a/password",
                                  json={"newPassword": "member-new-password"})).status_code == 404
        assert (await admin.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
        reset = await admin.post("/api/admin/users/member-a/password", json={"newPassword": "member-new-password"})
        assert reset.status_code == 200 and reset.json() == {"status": "password_reset", "username": "member-a"}
        assert (await member.get("/api/auth/owner/status")).json()["authenticated"] is False
        member.cookies.clear()
        assert (await member.post("/api/auth/owner", json={"username": "member-a",
                                                           "password": "member-new-password"})).status_code == 200
        refused = await admin.post("/api/admin/users/tq-user/password", json={"newPassword": "member-new-password"})
        assert refused.status_code == 409 and refused.json()["detail"] == "password_managed_by_tqkq"
        assert (await admin.post("/api/admin/users/charles/password",
                                 json={"newPassword": "member-new-password"})).status_code == 409


def test_tqkq_login_account_cannot_change_password_here():
    class Users:
        def get_cloud_user(self, username):
            return {"passwordHash": "!unusable", "role": "member", "invitedBy": "tqkq-login"}

    class Req:
        headers = {"origin": ORIGIN}
        client = type("C", (), {"host": "127.0.0.1"})()

    auth = OwnerAuth(username="charles", password_hash=PASSWORD_HASH, session_secret=SESSION_SECRET,
                     allowed_login_origins={ORIGIN}, user_store=Users())
    with pytest.raises(Exception) as refused:
        auth.change_password(Req(), None, username="tq-user", current_password="x",
                             new_password="brand-new-password-2026")
    assert refused.value.status_code == 409 and refused.value.detail == "password_managed_by_tqkq"


def test_password_routes_are_not_agent_capabilities(owner_app):
    routes = owner_app.state.app_capabilities.routes()
    assert "POST /api/auth/owner/password" not in routes
    assert "POST /api/admin/users/{username}/password" not in routes
    assert "PUT /api/admin/users/{username}" in routes

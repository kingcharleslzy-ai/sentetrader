from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from test_stream_revocation import Reader, serving
from trader_runtime.auth import CloudAuth
from trader_runtime.data_providers import (
    FakeReadOnlyDataProvider,
    SidecarReadOnlyDataProvider,
    TqKqReadOnlyProvider,
)
from trader_runtime.main import create_app
from trader_runtime.tenant_adapters import CloudWorkspaceManager, UserDataProviderRouter

ORIGIN = "http://127.0.0.1:5173"
SECRET = "phase-16-test-cloud-secret-with-more-than-32-bytes"


@pytest.fixture
def cloud_app(tmp_path: Path):
    application = create_app(
        tmp_path / "cloud.sqlite3",
        agent_mode="fake",
        auth_mode="cloud-jwt",
        cloud_jwt_secret=SECRET,
        cors_origins={ORIGIN},
    )
    yield application
    application.state.store.close()


async def cloud_client(application, *, tenant: str, subject: str):
    client = AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
        headers={"Origin": ORIGIN},
    )
    token = application.state.auth.issue_for_test(tenant_id=tenant, subject=subject)
    response = await client.post("/api/auth/cloud", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    return client


@pytest.mark.asyncio
async def test_cloud_sessions_are_isolated_by_tenant_and_user(cloud_app):
    first = await cloud_client(cloud_app, tenant="tenant-a", subject="alice")
    second = await cloud_client(cloud_app, tenant="tenant-b", subject="alice")
    try:
        created = await first.post("/api/sessions", json={"mode": "cloud", "agentId": "native"})
        assert created.status_code == 201
        session_id = created.json()["sessionId"]
        assert (await first.get(f"/api/sessions/{session_id}")).status_code == 200
        assert (await second.get(f"/api/sessions/{session_id}")).status_code == 404
        second_session = await second.post("/api/sessions", json={"mode": "cloud", "agentId": "native"})
        assert second_session.status_code == 201
        assert len(cloud_app.state.adapter.adapters) == 2
        assert len({id(adapter) for adapter in cloud_app.state.adapter.adapters.values()}) == 2
        status = await first.get("/api/runtime/status", params={"mode": "cloud"})
        assert status.json()["authMode"] == "cloud-jwt"
        assert status.json()["liveTrading"] is False
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_cloud_runtime_rejects_local_sessions_and_local_auth(cloud_app):
    client = await cloud_client(cloud_app, tenant="tenant-a", subject="alice")
    try:
        local_session = await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})
        assert local_session.status_code == 422
        local_login = await client.post("/api/auth/local", headers={"Authorization": "Bearer anything"})
        assert local_login.status_code == 404
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cloud_auth_status_probe_is_non_erroring_but_does_not_bypass_auth(cloud_app):
    async with AsyncClient(
        transport=ASGITransport(app=cloud_app),
        base_url="http://test",
        headers={"Origin": ORIGIN},
    ) as client:
        anonymous = await client.get("/api/auth/cloud/status")
        assert anonymous.status_code == 200
        assert anonymous.json() == {
            "authenticated": False,
            "authMode": "cloud-jwt",
            "authProvider": "development-hs256",
        }
        assert (await client.get("/api/runtime/status?mode=cloud")).status_code == 401

        token = cloud_app.state.auth.issue_for_test(
            tenant_id="tenant-a", subject="alice"
        )
        assert (
            await client.post(
                "/api/auth/cloud", headers={"Authorization": f"Bearer {token}"}
            )
        ).status_code == 200
        authenticated = await client.get("/api/auth/cloud/status")
        assert authenticated.status_code == 200
        assert authenticated.json()["authenticated"] is True
        assert authenticated.json()["tenantId"] == "tenant-a"
        assert authenticated.json()["userId"] == "alice"


@pytest.mark.asyncio
async def test_cloud_protected_auth_verification_does_not_block_event_loop(
    cloud_app, monkeypatch,
):
    client = await cloud_client(cloud_app, tenant="tenant-a", subject="alice")
    verification_started = threading.Event()
    release_verification = threading.Event()
    original_user = cloud_app.state.auth.user

    def slow_user(request):
        verification_started.set()
        if not release_verification.wait(timeout=2):
            raise RuntimeError("test_auth_verification_timeout")
        return original_user(request)

    monkeypatch.setattr(cloud_app.state.auth, "user", slow_user)
    protected_request = asyncio.create_task(
        client.get("/api/runtime/status?mode=cloud")
    )
    try:
        assert await asyncio.to_thread(verification_started.wait, 1)
        health = await asyncio.wait_for(client.get("/healthz"), timeout=0.5)
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
    finally:
        release_verification.set()
    assert (await protected_request).status_code == 200
    await client.aclose()


def test_cloud_tokens_expire_and_tenant_claim_is_enforced():
    auth = CloudAuth(SECRET, required_tenant="tenant-a")
    expired = auth.issue_for_test(tenant_id="tenant-a", subject="alice", expires_in=-1)
    with pytest.raises(Exception) as expired_error:
        auth.validate(expired)
    assert "cloud_token_expired" in str(expired_error.value)
    other_tenant = auth.issue_for_test(tenant_id="tenant-b", subject="alice")
    with pytest.raises(Exception) as tenant_error:
        auth.validate(other_tenant)
    assert "cloud_tenant_not_allowed" in str(tenant_error.value)


@pytest.mark.asyncio
async def test_explicit_tauri_origin_gets_secure_cross_site_cookie(tmp_path: Path):
    tauri_origin = "http://tauri.localhost:4174"
    application = create_app(
        tmp_path / "tauri-cloud.sqlite3",
        agent_mode="fake",
        auth_mode="cloud-jwt",
        cloud_jwt_secret=SECRET,
        cors_origins={tauri_origin},
    )
    token = application.state.auth.issue_for_test(tenant_id="tenant-a", subject="alice")
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="https://runtime.example.test",
        headers={"Origin": tauri_origin},
    ) as client:
        response = await client.post(
            "/api/auth/cloud", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=none" in cookie
        assert "Secure" in cookie
        created = await client.post(
            "/api/sessions", json={"mode": "cloud", "agentId": "native"}
        )
        assert created.status_code == 201
    application.state.store.close()


@pytest.mark.asyncio
async def test_explicit_https_web_origin_gets_secure_cross_site_cookie(tmp_path: Path):
    web_origin = "https://app.example.test"
    application = create_app(
        tmp_path / "web-cloud.sqlite3",
        agent_mode="fake",
        auth_mode="cloud-jwt",
        cloud_jwt_secret=SECRET,
        cors_origins={web_origin},
    )
    token = application.state.auth.issue_for_test(tenant_id="tenant-a", subject="alice")
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="https://runtime.example.test",
        headers={"Origin": web_origin},
    ) as client:
        response = await client.post(
            "/api/auth/cloud", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "SameSite=none" in cookie
        assert "Secure" in cookie
        assert (await client.get("/api/runtime/status?mode=cloud")).status_code == 200
    application.state.store.close()


@pytest.mark.asyncio
async def test_tauri_cross_site_cookie_fails_closed_without_https(tmp_path: Path):
    tauri_origin = "http://tauri.localhost:4174"
    application = create_app(
        tmp_path / "tauri-insecure.sqlite3",
        agent_mode="fake",
        auth_mode="cloud-jwt",
        cloud_jwt_secret=SECRET,
        cors_origins={tauri_origin},
    )
    token = application.state.auth.issue_for_test(tenant_id="tenant-a", subject="alice")
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://runtime.example.test",
        headers={"Origin": tauri_origin},
    ) as client:
        response = await client.post(
            "/api/auth/cloud", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "cloud_cross_site_https_required"
    application.state.store.close()


def test_cloud_workspace_manager_creates_separate_secret_free_roots(tmp_path: Path):
    template = tmp_path / "template"
    strategy = template / "workspace/strategies/rb_trend.py"
    strategy.parent.mkdir(parents=True)
    strategy.write_text("READ_ONLY_TEMPLATE = True\n")
    strategy.chmod(0o444)
    manager = CloudWorkspaceManager(template_root=template, state_root=tmp_path / "state")
    first_project = manager.prepare("tenant-a/alice")
    second_project = manager.prepare("tenant-a/bob")
    assert first_project != second_project
    assert (first_project / "workspace/strategies/rb_trend.py").is_file()
    assert (first_project / "workspace/strategies/rb_trend.py").stat().st_mode & 0o200
    assert not (first_project / ".env").exists()
    assert not (first_project / ".runtime").exists()


def test_cloud_workspace_template_rejects_symlinks(tmp_path: Path):
    template_workspace = tmp_path / "template/workspace"
    template_workspace.mkdir(parents=True)
    secret = tmp_path / "outside-secret"
    secret.write_text("must-not-copy")
    (template_workspace / "linked-secret").symlink_to(secret)
    manager = CloudWorkspaceManager(
        template_root=tmp_path / "template", state_root=tmp_path / "state"
    )
    with pytest.raises(RuntimeError, match="cloud_workspace_symlink_forbidden"):
        manager.prepare("tenant-a/alice")
    assert not any((tmp_path / "state/cloud-users").glob("*/project/linked-secret"))


def test_cloud_account_provider_modes_fail_closed_without_user_router(
    tmp_path: Path, monkeypatch,
):
    for mode in ("tqkq-readonly", "tqkq-sidecar"):
        with pytest.raises(RuntimeError, match="cloud_account_provider_router_required"):
            create_app(
                tmp_path / f"{mode}.sqlite3",
                agent_mode="fake",
                auth_mode="cloud-jwt",
                data_mode=mode,
                cloud_jwt_secret=SECRET,
                cors_origins={ORIGIN},
            )

    account_provider = TqKqReadOnlyProvider(username=None, password=None)
    with pytest.raises(RuntimeError, match="cloud_account_provider_router_required"):
        create_app(
            tmp_path / "injected-provider.sqlite3",
            agent_mode="fake",
            auth_mode="cloud-jwt",
            data_provider=account_provider,
            cloud_jwt_secret=SECRET,
            cors_origins={ORIGIN},
        )

    monkeypatch.setenv("AI_TRADER_READONLY_SIDECAR_URL", "http://127.0.0.1:43123")
    monkeypatch.setenv(
        "AI_TRADER_READONLY_SIDECAR_TOKEN", "local-fixture-token-with-32-bytes-minimum"
    )
    local = create_app(
        tmp_path / "local-sidecar.sqlite3",
        local_token="local-token",
        agent_mode="fake",
        auth_mode="local-token",
        data_mode="tqkq-sidecar",
    )
    try:
        assert isinstance(local.state.data_provider, SidecarReadOnlyDataProvider)
    finally:
        local.state.store.close()


def test_cloud_fake_provider_is_allowed_and_future_router_contract_is_explicit(tmp_path: Path):
    application = create_app(
        tmp_path / "cloud-fake.sqlite3",
        agent_mode="fake",
        auth_mode="cloud-jwt",
        data_provider=FakeReadOnlyDataProvider(),
        cloud_jwt_secret=SECRET,
        cors_origins={ORIGIN},
    )
    try:
        assert application.state.data_provider.mode == "fake"
        assert {
            "for_user", "for_session", "runtime_status_for_user", "start", "stop",
            "subscribe_status",
        } <= set(UserDataProviderRouter.__dict__)
    finally:
        application.state.store.close()


@pytest.mark.asyncio
async def test_cloud_fake_exposes_isolation_notice_and_user_scoped_backtests(cloud_app):
    first = await cloud_client(cloud_app, tenant="tenant-a", subject="alice")
    second = await cloud_client(cloud_app, tenant="tenant-b", subject="bob")
    try:
        first_user = cloud_app.state.auth.validate(
            cloud_app.state.auth.issue_for_test(tenant_id="tenant-a", subject="alice")
        ).scoped_user_id
        second_user = cloud_app.state.auth.validate(
            cloud_app.state.auth.issue_for_test(tenant_id="tenant-b", subject="bob")
        ).scoped_user_id
        first_session = (await first.post(
            "/api/sessions", json={"mode": "cloud", "agentId": "native"}
        )).json()["sessionId"]
        second_session = (await second.post(
            "/api/sessions", json={"mode": "cloud", "agentId": "native"}
        )).json()["sessionId"]
        first_status = (await first.get("/api/runtime/status", params={"mode": "cloud"})).json()
        assert first_status["agentNotice"] == "云端 AI 尚未配置隔离执行环境。"
        assert first_status["agentNotice"]
        first_result = await first.post(
            "/api/backtests/reproducible", json={"sessionId": first_session}
        )
        second_result = await second.post(
            "/api/backtests/reproducible", json={"sessionId": second_session}
        )
        assert first_result.status_code == second_result.status_code == 201
        first_id = first_result.json()["backtestId"]
        second_id = second_result.json()["backtestId"]
        first_record = await first.get(f"/api/backtests/{first_id}")
        assert first_record.status_code == 200
        assert "artifactPath" not in first_record.json()
        assert "userId" not in first_record.json()
        assert (await first.get(f"/api/backtests/{second_id}")).status_code == 404
        first_service = cloud_app.state.backtest_router.for_user(first_user)
        second_service = cloud_app.state.backtest_router.for_user(second_user)
        assert first_service is not second_service
        assert first_service.root != second_service.root
        assert first_service.releases.releases_root != second_service.releases.releases_root
        assert (
            cloud_app.state.backtest_service.releases.releases_root
            not in {first_service.releases.releases_root, second_service.releases.releases_root}
        )
        assert cloud_app.state.adapter.adapters[first_user].backtests is first_service
        assert cloud_app.state.adapter.adapters[second_user].backtests is second_service
        deleted = await first.delete(f"/api/sessions/{first_session}")
        assert deleted.status_code == 200
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_cloud_modified_strategies_and_escape_attempts_require_isolated_runner(
    cloud_app, tmp_path: Path,
):
    first = await cloud_client(cloud_app, tenant="tenant-a", subject="alice")
    second = await cloud_client(cloud_app, tenant="tenant-b", subject="bob")
    marker = tmp_path / "strategy-executed"
    try:
        first_user = cloud_app.state.auth.validate(
            cloud_app.state.auth.issue_for_test(tenant_id="tenant-a", subject="alice")
        ).scoped_user_id
        second_user = cloud_app.state.auth.validate(
            cloud_app.state.auth.issue_for_test(tenant_id="tenant-b", subject="bob")
        ).scoped_user_id
        first_session = (await first.post(
            "/api/sessions", json={"mode": "cloud", "agentId": "native"}
        )).json()["sessionId"]
        second_session = (await second.post(
            "/api/sessions", json={"mode": "cloud", "agentId": "native"}
        )).json()["sessionId"]
        manager = cloud_app.state.cloud_workspace_manager
        first_workspace = manager.prepare(first_user)
        second_workspace = manager.prepare(second_user)
        first_strategy = first_workspace / "workspace/strategies/rb_trend.py"
        second_strategy = second_workspace / "workspace/strategies/rb_trend.py"
        first_strategy.write_text(
            "import pathlib, socket, subprocess\n"
            f"pathlib.Path({str(marker)!r}).write_text('escaped')\n"
            "socket.create_connection(('127.0.0.1', 9))\n"
            "subprocess.run(['true'])\n"
        )
        second_strategy.write_text("raise RuntimeError('tenant-b strategy must not execute')\n")
        first_result = await first.post(
            "/api/backtests/reproducible", json={"sessionId": first_session}
        )
        second_result = await second.post(
            "/api/backtests/reproducible", json={"sessionId": second_session}
        )
        assert first_result.status_code == second_result.status_code == 422
        assert first_result.json()["detail"] == "isolated_runner_required"
        assert second_result.json()["detail"] == "isolated_runner_required"
        assert not marker.exists()

        database_escape = await first.post("/api/backtests/reproducible", json={
            "sessionId": first_session,
            "dataPath": str(cloud_app.state.store.database_path),
        })
        other_workspace_escape = await first.post("/api/backtests/reproducible", json={
            "sessionId": first_session,
            "dataPath": str(second_workspace / "workspace/backtests/fixtures/rb_monthly_v1.csv"),
        })
        artifact_escape = await first.post("/api/backtests/reproducible", json={
            "sessionId": first_session,
            "dataPath": str(cloud_app.state.backtest_router.for_user(second_user).root),
        })
        assert database_escape.status_code == 422
        assert other_workspace_escape.status_code == 422
        assert artifact_escape.status_code == 422
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_cloud_agent_status_is_scoped_to_authenticated_user(cloud_app, monkeypatch):
    first = await cloud_client(cloud_app, tenant="tenant-a", subject="alice")
    second = await cloud_client(cloud_app, tenant="tenant-b", subject="bob")
    try:
        first_user = cloud_app.state.auth.validate(
            cloud_app.state.auth.issue_for_test(tenant_id="tenant-a", subject="alice")
        ).scoped_user_id
        second_user = cloud_app.state.auth.validate(
            cloud_app.state.auth.issue_for_test(tenant_id="tenant-b", subject="bob")
        ).scoped_user_id
        first_session = (await first.post(
            "/api/sessions", json={"mode": "cloud", "agentId": "native"}
        )).json()["sessionId"]
        second_session = (await second.post(
            "/api/sessions", json={"mode": "cloud", "agentId": "native"}
        )).json()["sessionId"]
        second_adapter = cloud_app.state.adapter.adapters[second_user]
        original = second_adapter.runtime_status
        monkeypatch.setattr(
            second_adapter, "runtime_status",
            lambda mode: {**original(mode), "agentConnected": False, "connected": False},
        )
        first_status = (await first.get("/api/runtime/status", params={"mode": "cloud"})).json()
        second_status = (await second.get("/api/runtime/status", params={"mode": "cloud"})).json()
        assert first_status["agentConnected"] is True
        assert second_status["agentConnected"] is False
        assert cloud_app.state.adapter.runtime_status_for_session(first_session)["agentConnected"] is True
        assert cloud_app.state.adapter.runtime_status_for_session(second_session)["agentConnected"] is False
        assert first_user != second_user
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_cloud_browser_flow_keeps_agent_notice_without_grants(cloud_app):
    """2026-09-23 #288 回归：cloud-jwt 单租户用户没有任何 grant 行，仍要按
    IdP 兜底落在默认（fake）账户上。复刻浏览器启动链路——登录、workspace、
    sessions/restore、SSE 首条 runtime.status——每一步都不得因隔离拒绝，
    agentNotice 与默认账户摘要/持仓照常可读。"""
    client = await cloud_client(cloud_app, tenant="tenant-a", subject="browser")
    try:
        assert (await client.get("/api/workspace")).status_code == 200
        restored = await client.post(
            "/api/sessions/restore", json={"mode": "cloud", "agentId": "native"}
        )
        assert restored.status_code == 200, restored.text
        session_id = restored.json()["sessionId"]
        browser_user = cloud_app.state.auth.validate(cloud_app.state.auth.issue_for_test(
            tenant_id="tenant-a", subject="browser")).scoped_user_id
        assert cloud_app.state.store.list_account_grants(user_id=browser_user) == []
        status = await client.get("/api/runtime/status", params={"mode": "cloud"})
        assert status.status_code == 200, status.text
        payload = status.json()
        assert payload["agentNotice"] == "云端 AI 尚未配置隔离执行环境。"
        assert payload["account"]["id"] == cloud_app.state.store.default_trading_account_id
        # A null role here made the client's event schema drop the whole
        # runtime.status — the #288 regression this test pins.
        assert payload["account"]["role"] == "operator"
        directory = await client.get("/api/accounts")
        assert directory.status_code == 200, directory.text
        assert [item["id"] for item in directory.json()["accounts"]] == [
            cloud_app.state.store.default_trading_account_id]
        summary = await client.get("/api/account/summary")
        assert summary.status_code == 200, summary.text
        assert summary.json()["accountName"] == "假数据模拟账户"
        assert (await client.get("/api/positions")).status_code == 200

        # serving() 的 uvicorn 会把 "uvicorn" logger 的 propagate 关掉，污染后续
        # 测试的 caplog；与 test_member_isolation 一样退出时原样还原。
        saved_logging = {
            name: (logging.getLogger(name).level, logging.getLogger(name).propagate,
                   list(logging.getLogger(name).handlers))
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
        }
        try:
            async with serving(cloud_app) as base, AsyncClient(
                base_url=base, headers={"Origin": ORIGIN}, cookies=dict(client.cookies),
                trust_env=False,
            ) as stream_client:
                reader = Reader(stream_client, session_id)
                task = asyncio.create_task(reader.run())
                try:
                    await asyncio.wait_for(reader.opened.wait(), 2)
                    statuses: list[dict] = []
                    async with asyncio.timeout(2):
                        while not statuses:
                            await asyncio.sleep(0.05)
                            statuses = [item for item in reader.messages
                                        if item.get("name") == "runtime.status"]
                    assert statuses, reader.messages
                    assert statuses[0]["payload"]["agentNotice"] == "云端 AI 尚未配置隔离执行环境。"
                    assert statuses[0]["payload"]["account"]["role"] == "operator"
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        finally:
            for name, (level, propagate, handlers) in saved_logging.items():
                logger = logging.getLogger(name)
                logger.setLevel(level)
                logger.propagate = propagate
                logger.handlers = handlers
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_owner_status_answers_instead_of_404_without_owner_auth(cloud_app):
    """The client asks for the signed-in tier on every cloud Runtime. A cloud-JWT
    Runtime has no owner auth, and a 404 there showed up as a browser console
    error with nothing for the user to fix."""
    client = await cloud_client(cloud_app, tenant="tenant-a", subject="alice")
    try:
        response = await client.get("/api/auth/owner/status")
        assert response.status_code == 200
        assert response.json() == {"authenticated": False, "ownerAuth": False}
    finally:
        await client.aclose()


def test_cloud_jwt_subject_runs_on_the_default_tier_not_unlimited(cloud_app):
    """#754：cloud-jwt 用户（`cloud-<sha>`）没有 cloud_users 行。旧判定把“查不到行”
    一律当成所有者，JWT 用户全落到无限档、还能用所有者专属技能；现按默认档。"""
    from trader_runtime.plans import plan_for_user
    from trader_runtime.skill_registry import all_skills, availability_map

    auth = cloud_app.state.auth
    user_id = auth.validate(auth.issue_for_test(tenant_id="tenant-a", subject="alice")).scoped_user_id
    store = cloud_app.state.store
    plan = plan_for_user(store, user_id)
    assert not plan.unlimited
    assert plan.name == store.default_plan_name() == "basic"
    owner_only = [skill.skill_id for skill in all_skills() if skill.owner_only]
    assert owner_only
    reasons = availability_map(store, user_id)
    assert all(reasons[skill_id] == "owner_only" for skill_id in owner_only)

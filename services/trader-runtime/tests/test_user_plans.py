"""M2 user tiers: the seeded plan table, per-tier model/token/voice limits and
feature switches, the owner-only tier administration, tier-carrying invite
codes, and the old-database migration.

Every test drives the public HTTP surface of an owner-password Runtime with a
registered `friend` cloud user, so the assertions describe what a deployment
observes: what the catalog offers, what send_message refuses, what the owner's
pages list and what disabling an account ends.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.auth import hash_owner_password
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.main import create_app
from trader_runtime.model_provider import KIMI_MODEL
from trader_runtime.plans import FEATURES, plan_for_user
from trader_runtime.skill_registry import tier_gated_skills
from trader_runtime.store import RuntimeStore
from trader_runtime.store.trading_accounts import scoped_owner_user_id

ORIGIN = "https://qilinshuzhi.com"
PASSWORD = "correct-demo-password"
PASSWORD_HASH = hash_owner_password(PASSWORD, salt=b"0123456789abcdef")
SESSION_SECRET = "owner-session-secret-for-tests-more-than-32-bytes"
FRIEND_PASSWORD = "a-solid-password-123"


def owner_headers() -> dict[str, str]:
    return {"origin": ORIGIN}


@pytest.fixture
def plan_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi-fixture")
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-zhipu-fixture")  # a second configured model to gate
    application = create_app(
        tmp_path / "plans.sqlite3",
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
    # Tier gating needs configured voice, independent of a developer machine key.
    voice_key = tmp_path / "voice-test.key"
    voice_key.write_text("test-only-voice-key", encoding="utf-8")
    application.state.voice_realtime.key_file = voice_key
    yield application
    application.state.store.close()


@pytest.fixture
async def friend_and_owner(plan_app):
    """A registered default-tier user and the owner, each on their own client so
    switching sessions never interferes with the other's assertions."""
    transport = ASGITransport(app=plan_app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as friend,
        AsyncClient(transport=transport, base_url="http://test") as owner,
    ):
        login = await owner.post(
            "/api/auth/owner", json={"username": "charles", "password": PASSWORD},
            headers=owner_headers(),
        )
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
        # 2026-09-23 isolation: registration no longer grants the default
        # account; these tier tests need the member on an account, so grant
        # one explicitly — the same path an accounts-file declaration takes.
        plan_app.state.store.grant_account(
            account_id=plan_app.state.store.default_trading_account_id,
            user_id=friend_user_id(), role="operator",
            granted_by=scoped_owner_user_id("charles"),
        )
        yield friend, owner


async def create_session(client: AsyncClient) -> str:
    response = await client.post(
        "/api/sessions", json={"mode": "local", "agentId": "native"}, headers=owner_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()["sessionId"]


def friend_user_id() -> str:
    return scoped_owner_user_id("friend")


# -- the seeded tier table ----------------------------------------------------

def test_tiers_seed_once_and_deleted_tiers_stay_deleted(tmp_path: Path):
    database = tmp_path / "seed.sqlite3"
    store = RuntimeStore(database)
    names = [tier["name"] for tier in store.list_plan_tiers()]
    assert names == ["basic", "pro", "test"]
    basic = store.get_plan_tier("basic")
    assert basic["label"] == "基础档" and basic["isDefault"] is True
    # The registry owns the seed lists (SKILL.md `ai-trader-default-tiers`):
    # basic keeps everything the pre-registry features list used to open —
    # every tier-gated skill except ai-execution and web-research.
    assert basic["config"] == {
        "models": ["deepseek-flash"], "dailyTokens": 300_000,
        "voiceSecondsPerDay": 1800, "briefProducts": 3,
        "skills": sorted(skill.skill_id for skill in tier_gated_skills()
                         if "basic" in skill.default_tiers),
    }
    assert "ai-execution" not in basic["config"]["skills"]
    assert "web-research" not in basic["config"]["skills"]
    assert store.get_plan_tier("pro")["config"]["models"] is None
    assert store.get_plan_tier("test")["config"]["dailyTokens"] == 1_000_000
    store.delete_plan_tier("test")
    store.close()

    # A restart never resurrects the owner's deletions or edits.
    reopened = RuntimeStore(database)
    try:
        assert [tier["name"] for tier in reopened.list_plan_tiers()] == ["basic", "pro"]
        assert reopened.default_plan_name() == "basic"
    finally:
        reopened.close()


async def test_plan_resolution_follows_the_cloud_user_row(plan_app, friend_and_owner):
    _, owner = friend_and_owner
    store = plan_app.state.store
    assert store.get_user_plan(friend_user_id()) == "basic"
    assert plan_for_user(store, friend_user_id()).name == "basic"
    # The owner and anything that is not a cloud_users row (the local-token
    # user, an unknown id) run on the unlimited tier.
    assert plan_for_user(store, None).name == "owner"
    assert plan_for_user(store, "user-without-a-row").name == "owner"
    owner_cookie = owner.cookies.get("ai_trader_owner_auth")
    assert plan_for_user(store, plan_app.state.auth.validate(owner_cookie).scoped_user_id).name == "owner"

    assert (await owner.put(
        "/api/admin/users/friend", json={"plan": "pro"}, headers=owner_headers(),
    )).status_code == 200
    assert plan_for_user(store, friend_user_id()).name == "pro"

    # A stale tier name on a user row falls back to the default tier, never to
    # an unlimited or crashing plan.
    store.set_user_plan("friend", "deleted-tier")
    fallback = plan_for_user(store, friend_user_id())
    assert fallback.name == "basic" and fallback.label == "基础档"


# -- models and the catalog ---------------------------------------------------

async def test_model_catalog_and_preference_follow_the_tier(
    plan_app, friend_and_owner,
):
    friend, owner = friend_and_owner
    catalog = (await friend.get("/api/models")).json()
    by_slug = {entry["slug"]: entry for entry in catalog["models"]}
    assert set(by_slug) >= {"deepseek-flash", "glm-5.3-flash"}
    assert by_slug["deepseek-flash"]["selectable"] is True
    assert by_slug["glm-5.3-flash"]["selectable"] is False
    assert by_slug["glm-5.3-flash"]["inPlan"] is False
    assert catalog["current"] == "deepseek-flash"
    assert catalog["quota"]["plan"] == "basic"
    assert catalog["quota"]["dailyTokenLimit"] == 300_000
    assert catalog["quota"]["voiceSecondsPerDay"] == 30 * 60

    denied = await friend.put(
        "/api/models/preference", json={"model": "glm-5.3-flash"}, headers=owner_headers(),
    )
    assert denied.status_code == 403 and denied.json()["detail"] == "model_not_in_plan"

    assert (await owner.put(
        "/api/admin/users/friend", json={"plan": "pro"}, headers=owner_headers(),
    )).status_code == 200
    chosen = await friend.put(
        "/api/models/preference", json={"model": "glm-5.3-flash"}, headers=owner_headers(),
    )
    assert chosen.status_code == 200 and chosen.json()["current"] == "glm-5.3-flash"
    assert chosen.json()["quota"]["plan"] == "pro"

    # Downgraded again, an out-of-plan preference falls back for display without
    # being rewritten — an upgrade brings it right back.
    assert (await owner.put(
        "/api/admin/users/friend", json={"plan": "basic"}, headers=owner_headers(),
    )).status_code == 200
    basic_view = (await friend.get("/api/models")).json()
    assert basic_view["current"] == "deepseek-flash"
    assert plan_app.state.store.get_model_pref(friend_user_id()) == "glm-5.3-flash"


async def test_kimi_member_access_and_legacy_plan_model_are_resolved_on_read(plan_app, friend_and_owner):
    friend, owner = friend_and_owner
    catalog = (await friend.get("/api/models")).json()
    kimi = next(item for item in catalog["models"] if item["slug"] == KIMI_MODEL)
    assert kimi["memberOnly"] and not kimi["selectable"] and not kimi["inPlan"]
    denied = await friend.put("/api/models/preference", json={"model": KIMI_MODEL}, headers=owner_headers())
    assert denied.status_code == 403
    assert (await owner.put("/api/admin/users/friend", json={"plan": "pro"}, headers=owner_headers())).status_code == 200
    chosen = await friend.put("/api/models/preference", json={"model": KIMI_MODEL}, headers=owner_headers())
    assert chosen.status_code == 200 and chosen.json()["current"] == KIMI_MODEL
    from trader_runtime.plans import plan_from_config
    legacy = plan_from_config("legacy", "Legacy", {"models": ["gpt-6-astra"]})
    assert legacy.allows_model(KIMI_MODEL) and legacy.config()["models"] == [KIMI_MODEL]


async def test_daily_token_quota_blocks_by_tier(plan_app, friend_and_owner):
    friend, owner = friend_and_owner
    assert (await owner.put("/api/admin/plans/basic", json={"dailyTokens": 100},
                            headers=owner_headers())).status_code == 200
    session_id = await create_session(friend)
    plan_app.state.store.add_token_usage(
        friend_user_id(), datetime.now(UTC).date().isoformat(), 100,
    )
    blocked = await friend.post(
        f"/api/sessions/{session_id}/messages", json={"text": "你好"}, headers=owner_headers(),
    )
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "daily_token_quota_exceeded"

    assert (await owner.put(
        "/api/admin/users/friend", json={"plan": "pro"}, headers=owner_headers(),
    )).status_code == 200
    allowed = await friend.post(
        f"/api/sessions/{session_id}/messages", json={"text": "你好"}, headers=owner_headers(),
    )
    assert allowed.status_code == 202


async def test_an_explicit_zero_daily_token_cap_means_zero_not_unlimited(plan_app, friend_and_owner):
    """529-B1: the owner switching a tier's AI off with an explicit 0 must not
    turn the tier unlimited; null keeps the "no tier cap" meaning."""
    friend, owner = friend_and_owner
    assert (await owner.put("/api/admin/plans/basic", json={"dailyTokens": 0},
                            headers=owner_headers())).status_code == 200
    session_id = await create_session(friend)
    blocked = await friend.post(
        f"/api/sessions/{session_id}/messages", json={"text": "你好"}, headers=owner_headers(),
    )
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "daily_token_quota_exceeded"

    assert (await owner.put("/api/admin/plans/basic", json={"dailyTokens": None},
                            headers=owner_headers())).status_code == 200
    allowed = await friend.post(
        f"/api/sessions/{session_id}/messages", json={"text": "你好"}, headers=owner_headers(),
    )
    assert allowed.status_code == 202


# -- voice minutes -----------------------------------------------------------

async def test_voice_daily_minutes_report_and_refuse_the_socket(
    plan_app, friend_and_owner, monkeypatch,
):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("AI_TRADER_VOICE_REALTIME_KEY", "env-key-for-test")
    friend, owner = friend_and_owner
    store = plan_app.state.store
    today = datetime.now(UTC).date().isoformat()
    assert (await owner.put("/api/admin/plans/basic", json={"voiceSecondsPerDay": 60},
                            headers=owner_headers())).status_code == 200

    assert (await friend.get("/api/voice/realtime/status")).json()["reason"] is None
    store.add_voice_seconds(friend_user_id(), today, 59)
    assert (await friend.get("/api/voice/realtime/status")).json()["reason"] is None
    store.add_voice_seconds(friend_user_id(), today, 1)
    exhausted = (await friend.get("/api/voice/realtime/status")).json()
    assert exhausted["enabled"] is True and exhausted["reason"] == "voice_daily_limit"
    # The owner is unlimited: the same meter never flips.
    assert (await owner.get("/api/voice/realtime/status")).json()["reason"] is None

    cookie = friend.cookies.get("ai_trader_owner_auth")
    with TestClient(plan_app) as ws_client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with ws_client.websocket_connect(
                "/api/voice/realtime",
                headers={"Origin": ORIGIN, "Cookie": f"ai_trader_owner_auth={cookie}"},
            ):
                pass
        assert closed.value.code == 4430


# -- feature switches --------------------------------------------------------

async def test_ai_execution_start_is_refused_for_basic_but_stop_stays_open(plan_app, friend_and_owner):
    friend, owner = friend_and_owner
    session_id = await create_session(friend)
    start = {"sessionId": session_id, "symbols": ["SHFE.au2612"],
             "permissions": ["open", "cancel", "close", "stop"],
             "maxOrderVolume": 1, "maxOrders": 1, "durationMinutes": 5}
    refused = await friend.post("/api/ai-execution/start", json=start, headers=owner_headers())
    assert refused.status_code == 403 and refused.json()["detail"] == "feature_not_in_plan"
    limits = await friend.post(
        "/api/ai-execution/limits", json={"revision": 1, "maxOrderVolume": 1, "maxOrders": 5},
        headers=owner_headers(),
    )
    assert limits.status_code == 403 and limits.json()["detail"] == "feature_not_in_plan"
    # Stopping is never tier-gated: a left-over authorization can always be ended.
    stopped = await friend.post("/api/ai-execution/stop", json={"emergency": False}, headers=owner_headers())
    assert stopped.status_code == 200

    assert (await owner.put(
        "/api/admin/users/friend", json={"plan": "pro"}, headers=owner_headers(),
    )).status_code == 200
    started = await friend.post("/api/ai-execution/start", json=start, headers=owner_headers())
    # This fixture has no simulation gateway, so a pro start passes the tier gate
    # and stops at the environment boundary — not at the plan again. (The full
    # pro start path runs in test_owner_auth with a gateway.)
    assert started.status_code == 409
    assert started.json()["detail"]["code"] == "simulation_gateway_disabled"


async def test_every_feature_off_refuses_its_entries_and_hides_its_tools(
    plan_app, friend_and_owner,
):
    """A tier with every switch off: each business entry refuses with
    feature_not_in_plan (or its surface's own refuse shape) while the owner
    stays unlimited — and the capability catalog offers none of the gated
    tools."""
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    friend, owner = friend_and_owner
    assert (await friend.get("/api/voice/realtime/status")).json()["reason"] is None

    created = await owner.post(
        "/api/admin/plans", json={"label": "全关档"}, headers=owner_headers(),
    )
    assert created.status_code == 201
    off_tier = created.json()["name"]
    # A new tier starts as a copy of the default tier (basic here).
    assert created.json()["features"] == {
        "aiExecution": False, "webResearch": False, "marketBriefs": True,
        "strategies": True, "backtests": True, "replays": True, "voice": True,
    }
    switch = await owner.put(
        f"/api/admin/plans/{off_tier}",
        json={"features": {feature.camel_key: False for feature in FEATURES}},
        headers=owner_headers(),
    )
    assert switch.status_code == 200 and switch.json()["features"] == {
        feature.camel_key: False for feature in FEATURES}
    assert (await owner.put(
        "/api/admin/users/friend", json={"plan": off_tier}, headers=owner_headers(),
    )).status_code == 200

    session_id = await create_session(friend)
    # AI 自主执行
    start = await friend.post("/api/ai-execution/start", json={
        "sessionId": session_id, "symbols": ["SHFE.au2612"],
        "permissions": ["open"], "maxOrderVolume": 1, "maxOrders": 1,
        "durationMinutes": 5}, headers=owner_headers())
    assert start.status_code == 403 and start.json()["detail"] == "feature_not_in_plan"
    # 策略
    strategies = await friend.get("/api/strategies")
    assert strategies.status_code == 200  # reads stay open
    monitor = await friend.post("/api/strategies/some-id/monitor/start", headers=owner_headers())
    assert monitor.status_code == 403 and monitor.json()["detail"] == "feature_not_in_plan"
    # 回测（三种提交入口）
    for path, body in (
        ("/api/backtests", {"sessionId": session_id}),
        ("/api/backtests/reproducible", {"sessionId": session_id}),
    ):
        backtest = await friend.post(path, json=body, headers=owner_headers())
        assert backtest.status_code == 403, (path, backtest.text)
        assert backtest.json()["detail"] == "feature_not_in_plan"
    # 回放
    replay = await friend.post("/api/replays", json={
        "symbol": "SHFE.rb2610", "tradingDay": "2026-09-18", "initialBalance": 200000,
    }, headers=owner_headers())
    assert replay.status_code == 403 and replay.json()["detail"] == "feature_not_in_plan"
    # 语音：status 说出原因，播报接口 403（socket 拒绝在恢复检查之后做）。
    voice_status = (await friend.get("/api/voice/realtime/status")).json()
    assert voice_status["enabled"] is True and voice_status["reason"] == "feature_not_in_plan"
    announce = await friend.post("/api/voice/realtime/announce",
                                 json={"text": "播报一句"}, headers=owner_headers())
    assert announce.status_code == 403 and announce.json()["detail"] == "feature_not_in_plan"
    # 行情简讯：读取返回可识别的 disabled，刷新不再发起任何模型调用。
    briefs = await friend.get("/api/app/briefs", params={"symbol": "SHFE.rb2610"})
    assert briefs.status_code == 200 and briefs.json() == {"items": [], "status": "disabled"}
    refreshed = await friend.post("/api/app/briefs/refresh", json={"symbol": "SHFE.rb2610"},
                                  headers=owner_headers())
    assert refreshed.status_code == 200 and refreshed.json()["status"] == "disabled"
    # 能力目录：管理面对该用户不可见（工具按档位隐藏的行为在 native 档测试里钉）。
    catalog = (await friend.get("/api/agent/capabilities")).json()
    assert "GET /api/admin/plans" not in {item["operation"] for item in catalog["operations"]}
    owner_catalog = (await owner.get("/api/agent/capabilities")).json()
    assert "GET /api/admin/plans" in {item["operation"] for item in owner_catalog["operations"]}
    # 邀请码是主人在后台网页的人工动作，不在任何人的目录里（529-A2b）；提议风险规则也不在
    # （模拟账户无风险规则，所有者决定；AI 审计 2026-10-13 删掉了这个工具）。
    owner_operations = {item["operation"] for item in owner_catalog["operations"]}
    assert not {op for op in owner_operations if "/api/auth/" in op}
    assert "tool:create_risk_rule_proposal" not in owner_operations
    # 档位编辑对该用户 404（不可发现）。
    assert (await friend.get("/api/admin/plans")).status_code == 404

    # 恢复 pro 后一切立即可用（档位是每请求读库的事实，无需重启）。
    assert (await owner.put(
        "/api/admin/users/friend", json={"plan": "pro"}, headers=owner_headers(),
    )).status_code == 200
    assert (await friend.get("/api/voice/realtime/status")).json()["reason"] is None

    # 语音 socket：全关档的通话握手以 4431 + feature_not_in_plan 拒绝（最后做：
    # TestClient 的生命周期退出会关闭应用，其后的 HTTP 请求不可用）。
    assert (await owner.put(
        "/api/admin/users/friend", json={"plan": off_tier}, headers=owner_headers(),
    )).status_code == 200
    cookie = friend.cookies.get("ai_trader_owner_auth")
    with TestClient(plan_app) as ws_client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with ws_client.websocket_connect(
                "/api/voice/realtime",
                headers={"Origin": ORIGIN, "Cookie": f"ai_trader_owner_auth={cookie}"},
            ):
                pass
        assert closed.value.code == 4431


async def test_basic_tier_turns_run_without_the_online_tools(tmp_path: Path, monkeypatch):
    from fixtures.fake_responses_server import FakeResponsesServer
    from test_native_agent import wait_for_run

    server = FakeResponsesServer()
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-fixture-zhipu-not-real")
    application = create_app(
        tmp_path / "native-plans.sqlite3", agent_mode="native", auth_mode="owner-password",
        owner_username="charles", owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET, cors_origins={ORIGIN},
        data_mode="tqkq-sidecar", data_provider=FakeReadOnlyDataProvider(),
        agent_provider="deepseek", deepseek_api_key="sk-fixture-deepseek-not-real",
        registration_enabled=True, native_transport=server.transport(),
        engineering_root=tmp_path / "ai-trader-project", runtime_state_root=tmp_path,
    )
    try:
        transport = ASGITransport(app=application)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as friend,
            AsyncClient(transport=transport, base_url="http://test") as owner,
        ):
            assert (await owner.post(
                "/api/auth/owner", json={"username": "charles", "password": PASSWORD},
                headers=owner_headers(),
            )).status_code == 200
            code = (await owner.post(
                "/api/auth/owner/invite-codes", headers=owner_headers(),
            )).json()["inviteCode"]
            assert (await friend.post(
                "/api/auth/owner/register",
                json={"username": "friend", "password": FRIEND_PASSWORD, "inviteCode": code},
                headers=owner_headers(),
            )).status_code == 200
            # 2026-09-23 isolation: the member needs an explicit grant (the
            # accounts-file path) before it can open a session.
            application.state.store.grant_account(
                account_id=application.state.store.default_trading_account_id,
                user_id=friend_user_id(), role="operator",
                granted_by=scoped_owner_user_id("charles"),
            )
            session_id = await create_session(friend)
            run = await friend.post(
                f"/api/sessions/{session_id}/messages", json={"text": "只做只读策略分析"},
                headers=owner_headers(),
            )
            assert run.status_code == 202
            assert (await wait_for_run(application, run.json()["runId"]))["status"] == "completed"
            request = server.requests[-1]
            names = {tool["name"] for tool in request["tools"]}
            assert "web_search" not in names and "get_research_brief" not in names
            # The notice rides the turn's model text (the preference line), so the
            # model knows it must answer from its own knowledge.
            assert "未开通联网研究" in json.dumps(request, ensure_ascii=False)
            # The capability catalog offers the same toolset the turns get: basic
            # keeps the strategy/backtest tools but never the online ones.
            friend_ops = {item["operation"] for item in
                          (await friend.get("/api/agent/capabilities")).json()["operations"]}
            assert "tool:web_search" not in friend_ops and "tool:get_research_brief" not in friend_ops
            assert {"tool:run_reproducible_backtest", "tool:create_strategy_draft"} <= friend_ops
            owner_ops = {item["operation"] for item in
                         (await owner.get("/api/agent/capabilities")).json()["operations"]}
            assert "tool:web_search" in owner_ops
    finally:
        await application.state.runs.shutdown()
        await application.state.adapter.shutdown()
        await application.state.broker.shutdown()
        application.state.store.close()


# -- the owner's tier administration ------------------------------------------

async def test_admin_plans_crud_and_guards(plan_app, friend_and_owner):
    friend, owner = friend_and_owner
    # 非 owner 404、匿名 401：管理面不可发现。
    assert (await friend.get("/api/admin/plans")).status_code == 404
    async with AsyncClient(transport=ASGITransport(app=plan_app), base_url="http://test") as anonymous:
        assert (await anonymous.get("/api/admin/plans")).status_code == 401

    listed = (await owner.get("/api/admin/plans")).json()
    assert [item["name"] for item in listed["items"]] == ["basic", "pro", "test", "guest"]
    basic = listed["items"][0]
    assert basic["label"] == "基础档" and basic["isDefault"] is True
    assert basic["models"] == ["deepseek-flash"] and basic["userCount"] == 1
    assert [feature["key"] for feature in listed["featureCatalog"]] == [
        feature.camel_key for feature in FEATURES]
    assert {model["slug"] for model in listed["modelCatalog"]} >= {
        "deepseek-flash", "glm-5.3-flash"}

    # 新建：copyFrom 缺省复制默认档；未知来源 400。
    created = await owner.post("/api/admin/plans", json={"label": "内测"}, headers=owner_headers())
    assert created.status_code == 201
    tier_name = created.json()["name"]
    assert tier_name.startswith("tier-") and len(tier_name) == 11
    assert created.json()["models"] == ["deepseek-flash"]  # copied the default tier
    assert (await owner.post(
        "/api/admin/plans", json={"label": "坏", "copyFrom": "ghost"}, headers=owner_headers(),
    )).status_code == 400

    # 部分更新：数值、开关、名称；未知模型/功能键 400。
    updated = await owner.put(
        f"/api/admin/plans/{tier_name}",
        json={"label": "内测二档", "dailyTokens": None, "voiceSecondsPerDay": 300,
              "models": None, "features": {"aiExecution": True, "webResearch": False}},
        headers=owner_headers(),
    )
    assert updated.status_code == 200
    payload = updated.json()
    assert payload["label"] == "内测二档" and payload["dailyTokens"] is None
    assert payload["voiceSecondsPerDay"] == 300 and payload["models"] is None
    assert payload["features"]["aiExecution"] is True and payload["features"]["webResearch"] is False
    assert payload["features"]["voice"] is True  # unmentioned keys keep their value
    for bad_body in ({"models": ["not-a-model"]}, {"features": {"notAFeature": True}}):
        refused = await owner.put(f"/api/admin/plans/{tier_name}", json=bad_body,
                                  headers=owner_headers())
        assert refused.status_code == 400, bad_body
    assert (await owner.put(
        "/api/admin/plans/ghost", json={"label": "x"}, headers=owner_headers(),
    )).status_code == 404

    # 默认档唯一：设新默认后旧默认立即不再是。
    flipped = await owner.put(f"/api/admin/plans/{tier_name}", json={"isDefault": True},
                              headers=owner_headers())
    assert flipped.json()["isDefault"] is True
    rerun = (await owner.get("/api/admin/plans")).json()
    defaults = [item["name"] for item in rerun["items"] if item["isDefault"]]
    assert defaults == [tier_name]
    assert plan_app.state.store.default_plan_name() == tier_name
    await owner.put("/api/admin/plans/basic", json={"isDefault": True}, headers=owner_headers())

    # 删除守卫：有人用的档位、默认档、未知档位。
    in_use = await owner.delete("/api/admin/plans/basic", headers=owner_headers())
    assert in_use.status_code == 409 and in_use.json()["detail"] == "plan_is_default"
    assert (await owner.put(
        "/api/admin/users/friend", json={"plan": "pro"}, headers=owner_headers(),
    )).status_code == 200
    pro_in_use = await owner.delete("/api/admin/plans/pro", headers=owner_headers())
    assert pro_in_use.status_code == 409 and pro_in_use.json()["detail"] == "plan_in_use"
    assert (await owner.put(
        "/api/admin/users/friend", json={"plan": tier_name}, headers=owner_headers(),
    )).status_code == 200
    now_in_use = await owner.delete(f"/api/admin/plans/{tier_name}", headers=owner_headers())
    assert now_in_use.status_code == 409 and now_in_use.json()["detail"] == "plan_in_use"
    await owner.put("/api/admin/users/friend", json={"plan": "basic"}, headers=owner_headers())
    deleted = await owner.delete(f"/api/admin/plans/{tier_name}", headers=owner_headers())
    assert deleted.status_code == 200 and deleted.json()["status"] == "deleted"
    assert (await owner.delete("/api/admin/plans/ghost", headers=owner_headers())).status_code == 404


async def test_plan_features_update_merges_instead_of_replacing(plan_app, friend_and_owner):
    """`features` 按键合并：CLI/AI 只传一个键（如关语音）时，其余六个开关保持
    原值，不会被误关；空对象则原样不动。"""
    _, owner = friend_and_owner
    before = next(
        item for item in (await owner.get("/api/admin/plans")).json()["items"]
        if item["name"] == "basic")["features"]
    voice_off = await owner.put(
        "/api/admin/plans/basic", json={"features": {"voice": False}}, headers=owner_headers())
    assert voice_off.status_code == 200
    merged = voice_off.json()["features"]
    assert merged["voice"] is False
    assert {k: v for k, v in merged.items() if k != "voice"} == {
        k: v for k, v in before.items() if k != "voice"}

    untouched = await owner.put(
        "/api/admin/plans/basic", json={"features": {}}, headers=owner_headers())
    assert untouched.status_code == 200 and untouched.json()["features"] == merged

    assert (await owner.put("/api/admin/plans/basic", json={"features": {"voice": True}},
                            headers=owner_headers())).status_code == 200


async def test_invite_codes_carry_a_tier(plan_app, friend_and_owner):
    _, owner = friend_and_owner
    # 带档位的邀请码：第一次登录（注册路径）落在该档位。
    invited = await owner.post("/api/auth/owner/invite-codes",
                               json={"daysValid": 14, "plan": "pro"}, headers=owner_headers())
    assert invited.status_code == 200 and invited.json()["plan"] == "pro"
    code = invited.json()["inviteCode"]
    async with AsyncClient(transport=ASGITransport(app=plan_app), base_url="http://test") as guest:
        registered = await guest.post(
            "/api/auth/owner/register",
            json={"username": "guest", "password": FRIEND_PASSWORD, "inviteCode": code},
            headers=owner_headers(),
        )
        assert registered.status_code == 200
        assert plan_app.state.store.get_user_plan(scoped_owner_user_id("guest")) == "pro"
    # 不带档位（旧调用形状）落在默认档。
    plain = await owner.post("/api/auth/owner/invite-codes", headers=owner_headers())
    assert plain.json()["plan"] == ""
    async with AsyncClient(transport=ASGITransport(app=plan_app), base_url="http://test") as plain_guest:
        assert (await plain_guest.post(
            "/api/auth/owner/register",
            json={"username": "plain", "password": FRIEND_PASSWORD,
                  "inviteCode": plain.json()["inviteCode"]},
            headers=owner_headers(),
        )).status_code == 200
        assert plan_app.state.store.get_user_plan(scoped_owner_user_id("plain")) == "basic"
    # 未知档位名 400；邀请码列表带档位。
    assert (await owner.post(
        "/api/auth/owner/invite-codes", json={"plan": "ghost"}, headers=owner_headers(),
    )).status_code == 400
    codes = (await owner.get("/api/auth/owner/invite-codes", headers=owner_headers())).json()
    assert next(item for item in codes
                if any(use["username"] == "guest" for use in item["usedBy"]))["plan"] == "pro"


async def _register_with(plan_app, code: str, username: str):
    """One invited registration through the public surface."""
    async with AsyncClient(transport=ASGITransport(app=plan_app), base_url="http://test") as guest:
        response = await guest.post(
            "/api/auth/owner/register",
            json={"username": username, "password": FRIEND_PASSWORD, "inviteCode": code},
            headers=owner_headers(),
        )
        assert response.status_code == 200, response.text


async def test_custom_invite_codes_validate_and_collide(plan_app, friend_and_owner):
    _, owner = friend_and_owner
    # 自定义码：能用（区分大小写），格式错 422，同码重复 409。
    minted = await owner.post(
        "/api/auth/owner/invite-codes",
        json={"code": "Team_2026-Q3", "daysValid": 14}, headers=owner_headers(),
    )
    assert minted.status_code == 200
    assert minted.json()["inviteCode"] == "Team_2026-Q3" and minted.json()["uses"] == "once"
    await _register_with(plan_app, "Team_2026-Q3", "teammate")
    assert plan_app.state.store.get_user_plan(scoped_owner_user_id("teammate")) == "basic"
    # 大小写敏感：小写变体是另一个码，不是同一个。
    lowercase = await owner.post(
        "/api/auth/owner/invite-codes",
        json={"code": "team_2026-q3", "daysValid": 14}, headers=owner_headers(),
    )
    assert lowercase.status_code == 200
    assert (await owner.post(
        "/api/auth/owner/invite-codes",
        json={"code": "Team_2026-Q3", "daysValid": 14}, headers=owner_headers(),
    )).status_code == 409
    for bad in ("abc", "too-long-" * 5, "has space", "含中文"):
        assert (await owner.post(
            "/api/auth/owner/invite-codes",
            json={"code": bad, "daysValid": 14}, headers=owner_headers(),
        )).status_code == 422
    # 列表能看到自定义码原文；首尾空格在生成时就去掉。
    trimmed = await owner.post(
        "/api/auth/owner/invite-codes",
        json={"code": "  padded-code  ", "daysValid": 14}, headers=owner_headers(),
    )
    assert trimmed.status_code == 200 and trimmed.json()["inviteCode"] == "padded-code"


async def test_unlimited_invite_code_admits_many_users(plan_app, friend_and_owner):
    _, owner = friend_and_owner
    minted = await owner.post(
        "/api/auth/owner/invite-codes",
        json={"code": "open-house", "uses": "unlimited", "plan": "pro",
              "daysValid": 14}, headers=owner_headers(),
    )
    assert minted.status_code == 200 and minted.json()["uses"] == "unlimited"
    code = minted.json()["inviteCode"]
    await _register_with(plan_app, code, "first")
    await _register_with(plan_app, code, "second")
    assert plan_app.state.store.get_user_plan(scoped_owner_user_id("second")) == "pro"
    codes = (await owner.get("/api/auth/owner/invite-codes", headers=owner_headers())).json()
    row = next(item for item in codes if item["code"] == "open-house")
    assert row["uses"] == "unlimited" and row["useCount"] == 2 and row["status"] == "active"
    assert [use["username"] for use in row["usedBy"]] == ["second", "first"]  # 新到旧


async def test_one_shot_invite_code_refuses_a_second_use(plan_app, friend_and_owner):
    _, owner = friend_and_owner
    code = (await owner.post(
        "/api/auth/owner/invite-codes", headers=owner_headers(),
    )).json()["inviteCode"]
    await _register_with(plan_app, code, "first")
    async with AsyncClient(transport=ASGITransport(app=plan_app), base_url="http://test") as guest:
        refused = await guest.post(
            "/api/auth/owner/register",
            json={"username": "second", "password": FRIEND_PASSWORD, "inviteCode": code},
            headers=owner_headers(),
        )
        assert refused.status_code == 403
    codes = (await owner.get("/api/auth/owner/invite-codes", headers=owner_headers())).json()
    row = next(item for item in codes if item["code"] == code)
    assert row["status"] == "used_up" and row["useCount"] == 1


async def test_never_expiring_invite_code_survives_a_passing_expiry(plan_app, friend_and_owner):
    _, owner = friend_and_owner
    minted = await owner.post(
        "/api/auth/owner/invite-codes",
        json={"code": "forever-key", "neverExpires": True, "uses": "unlimited"},
        headers=owner_headers(),
    )
    assert minted.status_code == 200 and minted.json()["expiresAt"] is None
    # Its stored expiry is already in the past (creation time); only the flag keeps it alive.
    store = plan_app.state.store
    with store.lock, store.connection:
        store.connection.execute(
            "UPDATE invite_codes SET expires_at = '2020-01-01T00:00:00+00:00'"
            " WHERE code = 'forever-key'")
    await _register_with(plan_app, "forever-key", "timeless")
    codes = (await owner.get("/api/auth/owner/invite-codes", headers=owner_headers())).json()
    row = next(item for item in codes if item["code"] == "forever-key")
    assert row["expiresAt"] is None and row["status"] == "active" and row["useCount"] == 1


async def test_disabling_an_invite_code_blocks_and_is_idempotent(plan_app, friend_and_owner):
    friend, owner = friend_and_owner
    code = (await owner.post(
        "/api/auth/owner/invite-codes",
        json={"code": "to-be-cut", "uses": "unlimited"}, headers=owner_headers(),
    )).json()["inviteCode"]
    listed = (await owner.get("/api/auth/owner/invite-codes", headers=owner_headers())).json()
    row = next(item for item in listed if item["code"] == "to-be-cut")
    # 非 owner 一律 403，未知 id 404。
    assert (await friend.post(
        f"/api/auth/owner/invite-codes/{row['id']}/disable", headers=owner_headers(),
    )).status_code == 403
    assert (await owner.post(
        "/api/auth/owner/invite-codes/unknown-hash/disable", headers=owner_headers(),
    )).status_code == 404
    disabled = await owner.post(
        f"/api/auth/owner/invite-codes/{row['id']}/disable", headers=owner_headers())
    assert disabled.status_code == 200 and disabled.json()["status"] == "disabled"
    again = await owner.post(
        f"/api/auth/owner/invite-codes/{row['id']}/disable", headers=owner_headers())
    assert again.status_code == 200 and again.json() == disabled.json()  # 幂等：同一行
    async with AsyncClient(transport=ASGITransport(app=plan_app), base_url="http://test") as guest:
        refused = await guest.post(
            "/api/auth/owner/register",
            json={"username": "latecomer", "password": FRIEND_PASSWORD, "inviteCode": code},
            headers=owner_headers(),
        )
        assert refused.status_code == 403


async def test_invite_tier_deleted_before_first_login_lands_on_default(
    plan_app, friend_and_owner,
):
    """档位被删后邀请码才被使用：新成员落在默认档，cloud_users 不落悬空档位名。"""
    _, owner = friend_and_owner
    invited = await owner.post("/api/auth/owner/invite-codes",
                               json={"daysValid": 14, "plan": "test"}, headers=owner_headers())
    assert invited.status_code == 200
    deleted = await owner.delete("/api/admin/plans/test", headers=owner_headers())
    assert deleted.status_code == 200 and deleted.json()["status"] == "deleted"
    async with AsyncClient(transport=ASGITransport(app=plan_app), base_url="http://test") as guest:
        registered = await guest.post(
            "/api/auth/owner/register",
            json={"username": "latecomer", "password": FRIEND_PASSWORD,
                  "inviteCode": invited.json()["inviteCode"]},
            headers=owner_headers(),
        )
        assert registered.status_code == 200
    store = plan_app.state.store
    assert store.get_user_plan(scoped_owner_user_id("latecomer")) == "basic"
    # 不再写回已删除的档位名：用户列表/档位计数都不会出现悬空行。
    assert "test" not in store.count_users_by_plan()
    assert plan_for_user(store, scoped_owner_user_id("latecomer")).name == "basic"


# -- the owner's user table ---------------------------------------------------

async def test_admin_users_surface_is_owner_only_and_lists_the_table(plan_app, friend_and_owner):
    friend, owner = friend_and_owner
    assert (await friend.get("/api/admin/users")).status_code == 404  # not even discoverable
    async with AsyncClient(transport=ASGITransport(app=plan_app), base_url="http://test") as anonymous:
        assert (await anonymous.get("/api/admin/users")).status_code == 401

    listed = (await owner.get("/api/admin/users")).json()
    row = next(item for item in listed["items"] if item["username"] == "friend")
    assert row["plan"] == "basic" and row["planLabel"] == "基础档"
    assert row["role"] == "user" and row["invitedBy"] == "invite-code"
    assert row["tokensToday"] == 0 and row["voiceSecondsToday"] == 0
    assert row["disabled"] is False and row["tqkqBound"] is False

    today = datetime.now(UTC).date().isoformat()
    plan_app.state.store.add_token_usage(friend_user_id(), today, 30)
    plan_app.state.store.add_voice_seconds(friend_user_id(), today, 120)
    row = next(
        item for item in (await owner.get("/api/admin/users")).json()["items"]
        if item["username"] == "friend"
    )
    assert row["tokensToday"] == 30 and row["voiceSecondsMonth"] == 120
    assert row["estimatedCostToday"] is not None

    # 档位下拉接受任何现存档位名；未知名 400。
    assert (await owner.put(
        "/api/admin/users/friend", json={"plan": "test"}, headers=owner_headers(),
    )).status_code == 200
    assert (await owner.put(
        "/api/admin/users/friend", json={"plan": "vip"}, headers=owner_headers(),
    )).status_code == 400
    assert (await owner.put(
        "/api/admin/users/ghost", json={"plan": "pro"}, headers=owner_headers(),
    )).status_code == 404

    invited = await owner.post(
        "/api/auth/owner/invite-codes", json={"daysValid": 90}, headers=owner_headers(),
    )
    assert invited.status_code == 200
    expires = datetime.fromisoformat(invited.json()["expiresAt"].removesuffix("Z"))
    assert 89 <= (expires - datetime.now(UTC)).days <= 91


async def test_disabling_a_user_ends_their_session_and_their_login(plan_app, friend_and_owner):
    friend, owner = friend_and_owner
    assert (await friend.get("/api/models")).status_code == 200
    disabled = await owner.put(
        "/api/admin/users/friend", json={"disabled": True}, headers=owner_headers(),
    )
    assert disabled.status_code == 200 and disabled.json()["disabled"] is True
    # The existing session dies on its next request…
    assert (await friend.get("/api/models")).status_code == 401
    status = (await friend.get("/api/auth/owner/status")).json()
    assert status["authenticated"] is False
    # …and so does a fresh login, with the account named as the reason.
    refused = await friend.post(
        "/api/auth/owner", json={"username": "friend", "password": FRIEND_PASSWORD},
        headers=owner_headers(),
    )
    assert refused.status_code == 401 and refused.json()["detail"] == "account_disabled"

    assert (await owner.put(
        "/api/admin/users/friend", json={"disabled": False}, headers=owner_headers(),
    )).status_code == 200
    assert (await friend.post(
        "/api/auth/owner", json={"username": "friend", "password": FRIEND_PASSWORD},
        headers=owner_headers(),
    )).status_code == 200


async def test_owner_status_reports_the_callers_plan_and_features(plan_app, friend_and_owner):
    friend, owner = friend_and_owner
    friend_status = (await friend.get("/api/auth/owner/status")).json()
    assert friend_status["plan"] == "basic" and friend_status["planLabel"] == "基础档"
    # 1.6.7 客户端只知道三个键；新键是加法，strategyTools 保留。
    assert friend_status["features"] == {
        "aiExecution": False, "webResearch": False, "strategyTools": True,
        "marketBriefs": True, "strategies": True, "backtests": True,
        "replays": True, "voice": True,
    }
    owner_status = (await owner.get("/api/auth/owner/status")).json()
    assert owner_status["plan"] == "owner" and owner_status["planLabel"] == "管理员"
    assert owner_status["features"]["strategyTools"] is True
    assert all(owner_status["features"][feature.camel_key] for feature in FEATURES)


# -- migration ---------------------------------------------------------------

def test_old_invite_database_gains_the_four_capabilities(tmp_path: Path):
    """A pre-2026-10-01 database opens with its codes intact: old rows carry no
    code text (only the hash was stored), a consumed one-shot counts as one use
    by its recorded user, an untouched one stays active, and re-opening the
    store never duplicates the backfilled ledger."""
    from hashlib import sha256

    database = tmp_path / "old-invites.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE invite_codes ("
        " code_hash TEXT PRIMARY KEY, created_by TEXT NOT NULL,"
        " created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_by TEXT, used_at TEXT)"
    )
    used_hash = sha256(b"legacy-used").hexdigest()
    fresh_hash = sha256(b"legacy-fresh").hexdigest()
    connection.executemany(
        "INSERT INTO invite_codes (code_hash, created_by, created_at, expires_at, used_by, used_at)"
        " VALUES (?, 'charles', ?, ?, ?, ?)",
        [
            (used_hash, "2026-09-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00",
             "early-friend", "2026-09-02T00:00:00+00:00"),
            (fresh_hash, "2026-09-20T00:00:00+00:00", "2099-01-01T00:00:00+00:00", None, None),
        ],
    )
    connection.commit()
    connection.close()

    store = RuntimeStore(database)
    try:
        items = store.list_invite_codes()
        used = next(item for item in items if item["id"] == used_hash)
        fresh = next(item for item in items if item["id"] == fresh_hash)
        assert used["code"] is None and used["status"] == "used_up" and used["useCount"] == 1
        assert [use["username"] for use in used["usedBy"]] == ["early-friend"]
        assert fresh["code"] is None and fresh["status"] == "active" and fresh["useCount"] == 0
        assert fresh["uses"] == "once" and fresh["expiresAt"] == "2099-01-01T00:00:00+00:00"
    finally:
        store.close()
    # Opening again is a no-op (the ledger backfill ran once).
    store = RuntimeStore(database)
    try:
        used = next(item for item in store.list_invite_codes() if item["id"] == used_hash)
        assert used["useCount"] == 1 and len(used["usedBy"]) == 1
    finally:
        store.close()


def test_old_database_without_plan_column_defaults_to_basic(tmp_path: Path):
    database = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE cloud_users ("
        " username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,"
        " role TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL, invited_by TEXT)"
    )
    connection.execute(
        "CREATE TABLE invite_codes ("
        " code_hash TEXT PRIMARY KEY, created_by TEXT NOT NULL,"
        " created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_by TEXT, used_at TEXT)"
    )
    connection.execute(
        "INSERT INTO cloud_users (username, password_hash, role, created_at, invited_by)"
        " VALUES ('legacy', 'x', 'user', '2026-09-01T00:00:00+00:00', 'charles')"
    )
    connection.commit()
    connection.close()

    store = RuntimeStore(database)
    try:
        rows = store.list_cloud_users()
        assert [row["username"] for row in rows] == ["legacy"]
        assert rows[0]["plan"] == "basic" and rows[0]["disabled"] is False
        assert store.get_user_plan(scoped_owner_user_id("legacy")) == "basic"
        # The tier table seeds next to the old rows; old members keep their tier.
        assert store.default_plan_name() == "basic"
        assert [tier["name"] for tier in store.list_plan_tiers()] == ["basic", "pro", "test"]
        invites = store.list_invite_codes()
        assert invites == []  # the plan column arrives without touching old rows
    finally:
        store.close()


async def test_guest_market_history_admin_contract(plan_app, friend_and_owner):
    friend, owner = friend_and_owner
    store = plan_app.state.store
    baseline = {name: store.get_plan_tier(name)['config'] for name in ('basic', 'pro', 'test')}
    for days in (1, 3, 5):
        response = await owner.put('/api/admin/plans/guest', json={'marketHistoryTradingDays': days}, headers=owner_headers())
        assert response.status_code == 200
        assert response.json()['marketHistoryTradingDays'] == days
        assert store.get_plan_tier('guest')['config']['marketHistoryTradingDays'] == days
        listed = (await owner.get('/api/admin/plans')).json()['items']
        assert next(item for item in listed if item['name'] == 'guest')['marketHistoryTradingDays'] == days
    for days in (None, 0, 2, 4, 6, True, '3', 3.5):
        response = await owner.put('/api/admin/plans/guest', json={'marketHistoryTradingDays': days}, headers=owner_headers())
        assert response.status_code == 422
        assert store.get_plan_tier('guest')['config']['marketHistoryTradingDays'] == 5
    assert (await owner.put('/api/admin/plans/guest', json={'isDefault': True}, headers=owner_headers())).status_code == 409
    assert (await owner.put('/api/admin/users/friend', json={'plan': 'guest'}, headers=owner_headers())).status_code == 409
    assert (await owner.post('/api/auth/owner/invite-codes', json={'plan': 'guest'}, headers=owner_headers())).status_code == 409
    assert store.get_user_plan(friend_user_id()) == 'basic'
    for name in baseline:
        assert (await owner.put('/api/admin/plans/' + name, json={'marketHistoryTradingDays': 3}, headers=owner_headers())).status_code == 422
        assert store.get_plan_tier(name)['config'] == baseline[name]
    copied = await owner.post('/api/admin/plans', json={'label': 'Guest copy', 'copyFrom': 'guest'}, headers=owner_headers())
    assert copied.status_code == 201 and copied.json()['marketHistoryTradingDays'] is None
    assert not copied.json()['isDefault']
    for c in (friend, owner):
        response = (await c.get('/api/auth/owner/status')).json()
        assert 'marketHistoryTradingDays' in response and response['marketHistoryTradingDays'] is None
    # An unsupported stored member cap cannot be silently reported as enforced.
    store.update_plan_tier('basic', config={**baseline['basic'], 'marketHistoryTradingDays': 3})
    assert (await friend.get('/api/auth/owner/status')).status_code == 503
    assert (await owner.get('/api/admin/plans')).status_code == 503
    repair = await owner.put('/api/admin/plans/basic', json={'marketHistoryTradingDays': None}, headers=owner_headers())
    assert repair.status_code == 200 and repair.json()['marketHistoryTradingDays'] is None


async def test_existing_member_cannot_claim_reserved_guest_plan(plan_app, friend_and_owner):
    friend, owner = friend_and_owner
    plan_app.state.store.set_user_plan('friend', 'guest')
    assert (await friend.get('/api/auth/owner/status')).status_code == 503
    assert (await owner.put('/api/admin/users/friend', json={'plan': 'basic'}, headers=owner_headers())).status_code == 200
    assert (await friend.get('/api/auth/owner/status')).json()['marketHistoryTradingDays'] is None

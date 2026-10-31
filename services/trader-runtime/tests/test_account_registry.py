"""4.2 多账户装配：AccountRegistry、请求账户解析与每账户循环隔离。

无 accounts 文件时一切与单账户部署等价（T1）；有文件时每个账户一套
provider/订单/自主执行/监控/账本实例，请求按 `X-AI-Trader-Account` 头或
会话账户解析（T2），后台循环只动本账户的数据（T3）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from test_stream_revocation import ORIGIN, Reader, serving
from trader_runtime.broker import EventBroker
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.main import create_app
from trader_runtime.proposals import ProposalConflict, ProposalService
from trader_runtime.store import RuntimeStore
from trader_runtime.wiring.accounts import resolve_account

TOKEN = "registry-test-token"
DEFAULT_ACCOUNT = "tqkq-sim:default"
BETA_ACCOUNT = "tqkq-sim:beta"


class NamedProvider(FakeReadOnlyDataProvider):
    """A fake provider whose account card says which account it serves."""

    def __init__(self, account_name: str) -> None:
        super().__init__()
        self.account_name = account_name

    def account_summary(self, turn_id: str) -> dict[str, object]:
        return {**super().account_summary(turn_id), "accountName": self.account_name}


def write_accounts_file(path: Path, accounts: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps({"accounts": accounts}, ensure_ascii=False), encoding="utf-8")
    return path


def dual_account_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: str):
    """An app with two fake accounts; each provider names itself in the account card."""
    monkeypatch.setenv(
        "AI_TRADER_ACCOUNTS_FILE",
        str(write_accounts_file(tmp_path / "accounts.json", [
            {"id": DEFAULT_ACCOUNT, "displayName": "账户甲", "dataMode": "fake"},
            {"id": BETA_ACCOUNT, "displayName": "账户乙", "dataMode": "fake"},
        ])),
    )
    provider_a = NamedProvider("账户甲")
    provider_b = NamedProvider("账户乙")
    application = create_app(
        tmp_path / database, local_token=TOKEN, agent_mode="fake", data_mode="fake",
        account_providers={DEFAULT_ACCOUNT: provider_a, BETA_ACCOUNT: provider_b},
    )
    return application, provider_a, provider_b


async def local_client(application) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
        headers={"Origin": ORIGIN, "Authorization": f"Bearer {TOKEN}"},
    )
    response = await client.post("/api/auth/local")
    assert response.status_code == 200
    return client


async def test_t1_single_account_registry_matches_today(tmp_path: Path) -> None:
    application = create_app(tmp_path / "single.sqlite3", local_token=TOKEN, agent_mode="fake")
    try:
        registry = application.state.accounts
        assert [services.account_id for services in registry.all()] == [DEFAULT_ACCOUNT]
        # 旧入口保留：app.state 上的单例视图就是默认账户的实例。
        assert application.state.data_provider is registry.default.provider
        assert application.state.simulation_orders is registry.default.simulation_orders
        assert application.state.autonomy is registry.default.autonomy
        assert application.state.strategy_monitor is registry.default.strategy_monitor
        assert application.state.account_ledger is registry.default.account_ledger
        client = await local_client(application)
        try:
            response = await client.get("/api/account/summary")
            assert response.status_code == 200
            assert response.json() == FakeReadOnlyDataProvider().account_summary("")
        finally:
            await client.aclose()
    finally:
        application.state.store.close()


async def test_t2_two_fake_accounts_resolve_by_header_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, provider_a, provider_b = dual_account_app(tmp_path, monkeypatch, "dual.sqlite3")
    try:
        store = application.state.store
        registry = application.state.accounts
        assert [services.account_id for services in registry.all()] == [DEFAULT_ACCOUNT, BETA_ACCOUNT]
        assert registry.get(BETA_ACCOUNT).provider is provider_b
        assert store.get_trading_account(BETA_ACCOUNT)["display_name"] == "账户乙"
        assert sorted((grant["account_id"], grant["role"]) for grant in store.list_account_grants(user_id="local-user")) == [
            (BETA_ACCOUNT, "owner"), (DEFAULT_ACCOUNT, "owner"),
        ]
        client = await local_client(application)
        try:
            # 无头：默认账户（文件里第一个）。
            assert (await client.get("/api/account/summary")).json()["accountName"] == "账户甲"
            # 头部指到乙账户。
            headed = await client.get("/api/account/summary", headers={"X-AI-Trader-Account": BETA_ACCOUNT})
            assert headed.status_code == 200 and headed.json()["accountName"] == "账户乙"
            # 未授权的账户 id 一律 403。
            denied = await client.get("/api/account/summary", headers={"X-AI-Trader-Account": "tqkq-sim:nope"})
            assert denied.status_code == 403 and denied.json()["detail"] == "account_not_granted"
            # 会话绑定请求时的账户；该会话的 feed 账户卡读乙账户的 provider。
            created = await client.post(
                "/api/sessions", json={"mode": "local", "agentId": "native"},
                headers={"X-AI-Trader-Account": BETA_ACCOUNT},
            )
            assert created.status_code == 201
            session_id = created.json()["sessionId"]
            assert store.get_session(session_id)["tradingAccountId"] == BETA_ACCOUNT
            # 该会话的 SSE 订阅拿到乙账户的账户卡。ASGITransport 会缓冲完整响应体，
            # 无限的 SSE 流只能走真端口；serving() 会重排进程级 uvicorn 日志配置
            # （propagate=False + critical），退出时原样还原，保护后续 caplog 测试。
            saved_logging = {
                name: (logging.getLogger(name).level, logging.getLogger(name).propagate,
                       list(logging.getLogger(name).handlers))
                for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
            }
            try:
                async with serving(application) as base, httpx.AsyncClient(
                    base_url=base, headers={"Origin": ORIGIN, "Authorization": f"Bearer {TOKEN}"},
                    trust_env=False,
                ) as stream_client:
                    reader = Reader(stream_client, session_id)
                    task = asyncio.create_task(reader.run())
                    try:
                        await asyncio.wait_for(reader.opened.wait(), 2)
                        await asyncio.sleep(0.1)
                        await application.state.runtime_feed.tick()
                        await asyncio.sleep(0.2)
                        account = next(
                            item for item in reader.messages
                            if item.get("name") == "feed.account"
                        )
                        assert account["payload"]["account"]["accountName"] == "账户乙"
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
    finally:
        application.state.store.close()


async def test_t3_loops_only_touch_their_own_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _, _ = dual_account_app(tmp_path, monkeypatch, "loops.sqlite3")
    try:
        store = application.state.store
        services_a = application.state.accounts.get(DEFAULT_ACCOUNT)
        services_b = application.state.accounts.get(BETA_ACCOUNT)
        store.save_strategy_draft(
            strategy_id="strategy-a", user_id="local-user", source_session_id="seed",
            name="甲账户均线策略", symbol="SHFE.au2612", version=1, status="monitoring",
            definition={"template": "ma_cross"}, definition_digest="digest-a",
            trading_account_id=DEFAULT_ACCOUNT,
        )
        # 乙账户的监控循环看不到甲账户的策略，也不会为它写监控状态。
        assert await services_b.strategy_monitor.run_once() == []
        assert store.strategy_monitor_state(strategy_id="strategy-a", user_id="local-user") is None
        results = await services_a.strategy_monitor.run_once()
        assert [item["strategyId"] for item in results] == ["strategy-a"]
        # 自主执行限额：甲账户的写不落入乙账户的控制行。
        services_a.autonomy.update_limits(
            user_id="local-user", revision=0, max_order_volume=5, max_orders=None,
        )
        assert services_a.autonomy.controls(user_id="local-user")["maxOrderVolume"] == 5
        assert services_b.autonomy.controls(user_id="local-user")["maxOrderVolume"] is None
    finally:
        application.state.store.close()


async def test_t4_accounts_file_validation_and_disabled_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = write_accounts_file(tmp_path / "accounts.json", [
        {"id": DEFAULT_ACCOUNT, "displayName": "账户甲", "dataMode": "fake"},
        {"id": BETA_ACCOUNT, "displayName": "账户乙", "dataMode": "fake"},
    ])
    monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(valid))
    application = create_app(tmp_path / "validate.sqlite3", local_token=TOKEN, agent_mode="fake", data_mode="fake")
    try:
        store = application.state.store
        assert sorted(account["id"] for account in store.list_trading_accounts()) == [BETA_ACCOUNT, DEFAULT_ACCOUNT]
        assert all(account["status"] == "active" for account in store.list_trading_accounts())
    finally:
        application.state.store.close()
    # 下次启动文件里只剩甲：乙账户的行被标记 disabled，不再装配实例。
    write_accounts_file(valid, [{"id": DEFAULT_ACCOUNT, "displayName": "账户甲", "dataMode": "fake"}])
    slimmed = create_app(tmp_path / "validate.sqlite3", local_token=TOKEN, agent_mode="fake", data_mode="fake")
    try:
        store = slimmed.state.store
        assert [services.account_id for services in slimmed.state.accounts.all()] == [DEFAULT_ACCOUNT]
        assert store.get_trading_account(DEFAULT_ACCOUNT)["status"] == "active"
        assert store.get_trading_account(BETA_ACCOUNT)["status"] == "disabled"
    finally:
        slimmed.state.store.close()
    # 结构与内容校验。
    for payload, code in (
        ("not json", "accounts_file_invalid"),
        (json.dumps({"accounts": []}), "accounts_file_empty"),
        (json.dumps({"accounts": [
            {"id": DEFAULT_ACCOUNT, "dataMode": "fake"},
            {"id": DEFAULT_ACCOUNT, "dataMode": "fake"},
        ]}), "accounts_file_duplicate_id"),
        (json.dumps({"accounts": [
            {"id": DEFAULT_ACCOUNT, "dataMode": "fake"},
            {"id": BETA_ACCOUNT, "dataMode": "tqkq-sidecar"},
        ]}), "accounts_file_requires_sidecar"),
    ):
        (tmp_path / "broken.json").write_text(payload, encoding="utf-8")
        monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(tmp_path / "broken.json"))
        with pytest.raises(RuntimeError) as raised:
            create_app(tmp_path / f"broken-{code}.sqlite3", local_token=TOKEN, agent_mode="fake", data_mode="fake")
        assert code in str(raised.value)


def test_t5_directly_built_proposal_service_uses_the_default_account(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "direct.sqlite3")
    try:
        broker = EventBroker(store)
        provider = FakeReadOnlyDataProvider()
        proposals = ProposalService(store, broker, data_provider=provider)
        session = broker.create_session("local", "native", "local-user")
        assert store.get_session(session.session_id)["tradingAccountId"] == DEFAULT_ACCOUNT
        draft = proposals.create_strategy_draft(
            session.session_id, "turn-t5", name="甲均线", symbol="SHFE.au2612",
            template="ma_cross", timeframe_seconds=300, direction="both",
            parameters={"fastPeriod": 5, "slowPeriod": 20, "breakoutPeriod": 10,
                        "kdPeriod": 5, "macdFast": 4, "macdSlow": 9, "macdSignal": 3,
                        "minimumVolumeRatio": 1.0},
            max_position=1, max_orders_per_day=3, stop_loss_ticks=20,
            take_profit_ticks=40, cooldown_bars=2, rationale="T5 直接构造",
        )
        assert store.get_strategy_draft(draft["strategyId"], user_id="local-user")["tradingAccountId"] == DEFAULT_ACCOUNT
    finally:
        store.close()


async def test_t6_removed_account_is_refused_not_fallen_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """乙账户移出 accounts 文件后：指向它的请求、绑定它的会话与提案行一律拒绝，
    不得静默落到默认账户上读写或下单。"""
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(accounts_file))
    database = tmp_path / "rework.sqlite3"
    application, _, _ = dual_account_app(tmp_path, monkeypatch, "rework.sqlite3")
    try:
        store = application.state.store
        client = await local_client(application)
        try:
            created = await client.post(
                "/api/sessions", json={"mode": "local", "agentId": "native"},
                headers={"X-AI-Trader-Account": BETA_ACCOUNT},
            )
            assert created.status_code == 201
            beta_session = created.json()["sessionId"]
            assert store.get_session(beta_session)["tradingAccountId"] == BETA_ACCOUNT
        finally:
            await client.aclose()
        # 旧库里一条绑定乙账户的提案行。
        store.create(
            proposal_id="proposal-beta", session_id=beta_session, proposal_type="trade",
            status="approval_required", payload={"symbol": "SHFE.au2612"},
            user_id="local-user", version=1, approval_id="approval-beta",
            approval_expires_at="2999-01-01T00:00:00Z", trading_account_id=BETA_ACCOUNT,
        )
    finally:
        application.state.store.close()
    # 重启：文件里只剩甲，乙账户不再装配。
    write_accounts_file(accounts_file, [{"id": DEFAULT_ACCOUNT, "displayName": "账户甲", "dataMode": "fake"}])
    slimmed = create_app(database, local_token=TOKEN, agent_mode="fake", data_mode="fake")
    try:
        store = slimmed.state.store
        registry = slimmed.state.accounts
        assert [services.account_id for services in registry.all()] == [DEFAULT_ACCOUNT]
        # (a) 头部仍指向乙：403，不读默认账户。
        client = await local_client(slimmed)
        try:
            headed = await client.get("/api/account/summary", headers={"X-AI-Trader-Account": BETA_ACCOUNT})
            assert headed.status_code == 403 and headed.json()["detail"] == "account_not_granted"
        finally:
            await client.aclose()
        # (b) 绑定乙的会话解析：授权仍在（grants 未删）但账户未装配 → 403；
        #     用户只有甲授权时会话指向乙 → 同样 403，不回退默认账户。
        store.grant_account(account_id=DEFAULT_ACCOUNT, user_id="half-user",
                            role="operator", granted_by="local-user")
        for user in ("local-user", "half-user"):
            with pytest.raises(HTTPException) as raised:
                resolve_account(registry, store, request=None, user_id=user,
                                session_account_id=BETA_ACCOUNT)
            assert raised.value.status_code == 403
            assert raised.value.detail == "account_not_granted"
        # (c) 确认绑定乙账户的提案：拒绝，不在默认账户上风控/下单。
        with pytest.raises(ProposalConflict) as conflict:
            await slimmed.state.proposals.confirm(
                "proposal-beta", user_id="local-user", approval_id="approval-beta",
                proposal_version=1, idempotency_key="t6-confirm",
            )
        assert conflict.value.code == "account_not_assembled"
        # (d) 无任何 grant 的用户（IdP 回退）落在默认账户，角色是 operator。
        context = resolve_account(registry, store, request=None, user_id="idp-user")
        assert context.account.account_id == DEFAULT_ACCOUNT
        assert context.role == "operator"
        # The fallback also holds when the client names the default account explicitly
        # (the switcher sends X-AI-Trader-Account once runtime.status carries it); any
        # other account stays refused for a user without grants.
        headed = resolve_account(registry, store, request=SimpleNamespace(headers={
            "X-AI-Trader-Account": DEFAULT_ACCOUNT}), user_id="idp-user")
        assert headed.account.account_id == DEFAULT_ACCOUNT and headed.role == "operator"
        with pytest.raises(HTTPException) as refused:
            resolve_account(registry, store, request=SimpleNamespace(headers={
                "X-AI-Trader-Account": BETA_ACCOUNT}), user_id="idp-user")
        assert refused.value.status_code == 403 and refused.value.detail == "account_not_granted"
    finally:
        slimmed.state.store.close()


async def test_t8_non_default_account_turn_tools_reach_the_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非默认账户（受邀用户自己的快期账户）的回合工具集必须接上能力目录与调研简报；
    09-23 起漏接，受邀用户的每个工具都以 app_capabilities_unavailable 失败。"""
    monkeypatch.setenv(
        "AI_TRADER_ACCOUNTS_FILE",
        str(write_accounts_file(tmp_path / "accounts.json", [
            {"id": DEFAULT_ACCOUNT, "displayName": "账户甲", "dataMode": "fake"},
            {"id": BETA_ACCOUNT, "displayName": "账户乙", "dataMode": "fake"},
        ])),
    )
    application = create_app(
        tmp_path / "tools.sqlite3", local_token=TOKEN, agent_mode="native",
        agent_provider="deepseek", data_mode="fake",
        account_providers={DEFAULT_ACCOUNT: NamedProvider("账户甲"), BETA_ACCOUNT: NamedProvider("账户乙")},
    )
    try:
        adapter = application.state.adapter
        adapter = adapter.adapter_for_user("local-user") if hasattr(adapter, "adapter_for_user") else adapter
        shared = adapter.trader_tools
        tools = adapter.tools_for_account(BETA_ACCOUNT)
        assert tools is not shared and shared.app_capabilities is not None
        assert tools.app_capabilities is shared.app_capabilities
        assert tools.research_brief is shared.research_brief
        session_id = application.state.broker.create_session(
            "local", "native", "local-user", trading_account_id=BETA_ACCOUNT).session_id
        result, _ = await tools.call_async(
            tool="get_preferences", arguments={}, turn_id="t8", mode="local",
            session_id=session_id, user_id="local-user")
        assert result["success"] is True, result
    finally:
        application.state.store.close()


async def test_t9_estimate_order_tool_reads_the_turn_account_not_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """estimate_order 在非默认账户的回合里必须读该账户的可用资金；10-02 线上读成了主账号。"""
    class RichProvider(NamedProvider):
        def account_summary(self, turn_id: str) -> dict[str, object]:
            return {**super().account_summary(turn_id), "available": 9_999_999.0}

    monkeypatch.setenv(
        "AI_TRADER_ACCOUNTS_FILE",
        str(write_accounts_file(tmp_path / "accounts.json", [
            {"id": DEFAULT_ACCOUNT, "displayName": "账户甲", "dataMode": "fake"},
            {"id": BETA_ACCOUNT, "displayName": "账户乙", "dataMode": "fake"},
        ])),
    )
    application = create_app(
        tmp_path / "estimate.sqlite3", local_token=TOKEN, agent_mode="native",
        agent_provider="deepseek", data_mode="fake",
        account_providers={DEFAULT_ACCOUNT: NamedProvider("账户甲"), BETA_ACCOUNT: RichProvider("账户乙")},
    )
    try:
        adapter = application.state.adapter
        adapter = adapter.adapter_for_user("local-user") if hasattr(adapter, "adapter_for_user") else adapter
        session_id = application.state.broker.create_session(
            "local", "native", "local-user", trading_account_id=BETA_ACCOUNT).session_id
        result, _ = await adapter.tools_for_account(BETA_ACCOUNT).call_async(
            tool="estimate_order", arguments={"symbol": "SHFE.au2612", "side": "buy"},
            turn_id="t9", mode="local", session_id=session_id, user_id="local-user")
        assert result["success"] is True, result
        data = json.loads(result["contentItems"][0]["text"])["data"]
        assert data["available"] == 9_999_999.0
    finally:
        application.state.store.close()


@pytest.mark.parametrize("revoke", [False, True])
async def test_abandon_from_another_account_has_no_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revoke: bool) -> None:
    from test_simulation_orders import Gateway, NormalProvider, proposal
    monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(write_accounts_file(tmp_path / "accounts.json", [
        {"id": DEFAULT_ACCOUNT, "dataMode": "fake"}, {"id": BETA_ACCOUNT, "dataMode": "fake"}])))
    gateway = Gateway()
    gateway.submit_report = {"commandStatus": "NOT_DISPATCHED", "ordersInitialized": False, "found": False}
    app = create_app(tmp_path / "abandon.sqlite3", local_token=TOKEN, agent_mode="fake",
                     data_provider=NormalProvider(), simulation_gateway=gateway)
    client = await local_client(app)
    try:
        card = proposal(app)
        confirmed = await client.post(f"/api/proposals/{card['proposalId']}/confirm", json={
            "approvalId": card["approvalId"], "proposalVersion": card["version"], "idempotencyKey": "abandon-account"})
        assert confirmed.status_code == 200, confirmed.text
        coid = confirmed.json()["clientOrderId"]
        original = app.state.simulation_orders.get(coid, user_id="local-user")
        assert original["submitPhase"] == "PREPARED"
        if revoke:
            with app.state.store.transaction() as connection:
                connection.execute("DELETE FROM account_grants WHERE account_id = ? AND user_id = ?", (DEFAULT_ACCOUNT, "local-user"))
        for _ in range(2):
            denied = await client.post(f"/api/simulation/orders/{coid}/abandon", headers={"X-AI-Trader-Account": BETA_ACCOUNT})
            assert denied.status_code == 404, denied.text
            assert app.state.simulation_orders.get(coid, user_id="local-user") == original
        if revoke:
            from trader_runtime.simulation_orders import SimulationOrderError
            with pytest.raises(SimulationOrderError, match="account_not_granted"):
                app.state.simulation_orders.abandon(coid, user_id="local-user")
            assert app.state.simulation_orders.get(coid, user_id="local-user") == original
        else:
            abandoned = await client.post(f"/api/simulation/orders/{coid}/abandon", headers={"X-AI-Trader-Account": DEFAULT_ACCOUNT})
            assert abandoned.status_code == 200 and abandoned.json()["submitPhase"] == "ABANDONED"
            again = await client.post(f"/api/simulation/orders/{coid}/abandon", headers={"X-AI-Trader-Account": DEFAULT_ACCOUNT})
            assert again.status_code == 409
            assert gateway.submit_count == 1
    finally:
        await client.aclose()
        app.state.store.close()

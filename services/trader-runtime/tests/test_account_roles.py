"""4.3 执行归属与角色权限：一账户一执行负责人，owner/operator/viewer 各自的边界。

乙账户的执行负责人是别人（`executionOwnerUsername: someone-else`）时：local-user 在乙上
是 operator——AI 自主动作（下单、恢复、挂止损、建租约、策略监控下单）一律
`not_execution_owner`，手工执行路径照常放行（T1）；viewer 一切写操作 `account_read_only`，
读不受影响（T2）；operator 可急停与只停新开仓（T3）；工具返回带中文 userFacing（T4）；
策略监控只监控不消耗 K 线（T5）；保护单授权按账户过滤（T6）；无 accounts 文件时与今天
完全一致（T7）。
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from test_account_registry import (
    BETA_ACCOUNT,
    DEFAULT_ACCOUNT,
    TOKEN,
    local_client,
    write_accounts_file,
)
from test_simulation_orders import (
    USER,
    Gateway,
    NormalProvider,
    _tool_status,
    _tool_text,
    application,
)
from trader_runtime.autonomy import AutonomyError
from trader_runtime.main import create_app
from trader_runtime.store.trading_accounts import scoped_owner_user_id
from trader_runtime.trader_tools import TraderReadOnlyTools

OTHER_OWNER = scoped_owner_user_id("someone-else")
BAR_TIME = "1767225600000000000"


class RolesProvider(NormalProvider):
    """Names its account in the account card; the activity feed is editable for fill tests."""

    def __init__(self, account_name: str) -> None:
        super().__init__()
        self.account_name = account_name
        self.activity_feed: dict[str, Any] | None = None

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        return {**super().account_summary(turn_id), "accountName": self.account_name}

    def account_activity(self) -> dict[str, Any]:
        return self.activity_feed if self.activity_feed is not None else super().account_activity()


def roles_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: str, *,
    beta_role: str = "operator", beta_owner: str | None = "someone-else",
):
    """两账户：甲的执行负责人是 local-user；乙按测试需要换执行负责人（默认是别人），
    local-user 在乙上另授 operator 或 viewer（乙不会自动授：local-user 不在 cloud_users）。"""
    beta_entry: dict[str, Any] = {"id": BETA_ACCOUNT, "displayName": "账户乙", "dataMode": "fake"}
    if beta_owner is not None:
        beta_entry["executionOwnerUsername"] = beta_owner
    monkeypatch.setenv(
        "AI_TRADER_ACCOUNTS_FILE",
        str(write_accounts_file(tmp_path / "accounts.json", [
            {"id": DEFAULT_ACCOUNT, "displayName": "账户甲", "dataMode": "fake"},
            beta_entry,
        ])),
    )
    gateway_a, gateway_b = Gateway(), Gateway()
    app = create_app(
        tmp_path / database, local_token=TOKEN, agent_mode="fake", data_mode="fake",
        simulation_gateway=gateway_a,
        account_providers={DEFAULT_ACCOUNT: RolesProvider("账户甲"), BETA_ACCOUNT: RolesProvider("账户乙")},
    )
    app.state.store.grant_account(
        account_id=BETA_ACCOUNT, user_id=USER, role=beta_role, granted_by=OTHER_OWNER,
    )
    services_b = app.state.accounts.get(BETA_ACCOUNT)
    assert services_b is not None
    # 乙账户没有共享网关注入通道（4.2 只把 create_app 的网关接到默认账户），测试直接接线。
    services_b.simulation_orders.gateway = gateway_b
    return app, gateway_a, gateway_b


def beta_session(app) -> str:
    return app.state.broker.create_session(
        "local", "native", USER, trading_account_id=BETA_ACCOUNT,
    ).session_id


async def test_t1_ai_initiated_actions_belong_to_the_execution_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, gateway_a, gateway_b = roles_app(tmp_path, monkeypatch, "ownership.sqlite3")
    try:
        store = app.state.store
        assert store.is_execution_owner(account_id=DEFAULT_ACCOUNT, user_id=USER) is True
        assert store.is_execution_owner(account_id=BETA_ACCOUNT, user_id=USER) is False
        assert store.account_role(account_id=BETA_ACCOUNT, user_id=USER) == "operator"
        autonomy_b = app.state.accounts.get(BETA_ACCOUNT).autonomy
        session_id = beta_session(app)
        for action in (
            lambda: autonomy_b.execute_trade(
                user_id=USER, session_id=session_id, symbol="SHFE.au2612", side="buy_open",
                volume=1, price_intent="passive", reason="T1 归属检查", ai_initiated=True),
            lambda: autonomy_b.resume_runtime_access(
                user_id=USER, session_id=session_id, symbol="SHFE.au2612", ai_initiated=True),
            lambda: autonomy_b.create_stop(
                user_id=USER, session_id=session_id, symbol="SHFE.au2612",
                position_direction="long", position_bucket="today", volume=1,
                trigger_price=800, ai_initiated=True),
            lambda: autonomy_b.activate(
                user_id=USER, session_id=session_id, symbols=["SHFE.au2612"],
                permissions=["open", "close", "stop", "cancel"], max_order_volume=1,
                max_orders=5, duration_minutes=30),
        ):
            with pytest.raises(AutonomyError) as raised:
                await action()
            assert str(raised.value) == "not_execution_owner"
        assert gateway_b.submit_count == 0
        # operator 的手工执行路径（回放同款 ai_initiated=False）照常走到网关提交。
        await autonomy_b.execute_trade(
            user_id=USER, session_id=session_id, symbol="SHFE.au2612", side="buy_open",
            volume=1, price_intent="passive", reason="T1 手工路径", ai_initiated=False)
        assert gateway_b.submit_count == 1
        # 甲账户 local-user 是执行负责人：AI 自主动作放行（与今天一致）。
        session_a = app.state.broker.create_session("local", "native", USER).session_id
        await app.state.autonomy.execute_trade(
            user_id=USER, session_id=session_a, symbol="SHFE.au2612", side="buy_open",
            volume=1, price_intent="passive", reason="T1 负责人路径", ai_initiated=True)
        assert gateway_a.submit_count == 1
    finally:
        app.state.store.close()


async def test_t2_viewer_reads_but_cannot_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, _ = roles_app(tmp_path, monkeypatch, "viewer.sqlite3", beta_role="viewer")
    try:
        store = app.state.store
        autonomy_b = app.state.accounts.get(BETA_ACCOUNT).autonomy
        with pytest.raises(AutonomyError) as raised:
            await autonomy_b.deactivate(user_id=USER)
        assert str(raised.value) == "account_read_only"
        session_id = beta_session(app)
        store.create(
            proposal_id="proposal-beta-viewer", session_id=session_id, proposal_type="trade",
            status="approval_required", payload={"symbol": "SHFE.au2612"}, user_id=USER,
            version=1, approval_id="approval-beta-viewer",
            approval_expires_at="2999-01-01T00:00:00Z", trading_account_id=BETA_ACCOUNT,
        )
        client = await local_client(app)
        try:
            header = {"X-AI-Trader-Account": BETA_ACCOUNT}
            stopped = await client.post("/api/ai-execution/stop", headers=header,
                                        json={"emergency": False, "scope": "all"})
            assert stopped.status_code == 403 and stopped.json()["detail"] == "account_read_only"
            cancelled = await client.post(
                "/api/simulation/orders/order-beta/cancel", headers=header)
            assert cancelled.status_code == 403 and cancelled.json()["detail"] == "account_read_only"
            confirmed = await client.post(
                "/api/proposals/proposal-beta-viewer/confirm", headers=header,
                json={"approvalId": "approval-beta-viewer", "proposalVersion": 1,
                      "idempotencyKey": "t2-viewer-confirm"})
            assert confirmed.status_code == 403 and confirmed.json()["detail"] == "account_read_only"
            summary = await client.get("/api/account/summary", headers=header)
            assert summary.status_code == 200 and summary.json()["accountName"] == "账户乙"
        finally:
            await client.aclose()
    finally:
        app.state.store.close()


@pytest.mark.parametrize(("emergency", "scope", "status"), [
    (False, "opens", "HALT_OPENS"), (False, "all", "STOPPED"), (True, "all", "EMERGENCY_STOP"),
])
async def test_t3_operator_stops_the_account_not_just_their_own_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, emergency: bool, scope: str, status: str,
) -> None:
    app, gateway_a, gateway_b = roles_app(tmp_path, monkeypatch, "operator-stop.sqlite3")
    try:
        store = app.state.store
        autonomy = app.state.accounts.get(BETA_ACCOUNT).autonomy
        owner_session = app.state.broker.create_session("local", "native", OTHER_OWNER,
                                                       trading_account_id=BETA_ACCOUNT).session_id
        await autonomy.execute_trade(user_id=OTHER_OWNER, session_id=owner_session, symbol="SHFE.au2612",
                                     side="buy_open", volume=1, price_intent="passive", reason="owner before stop",
                                     ai_initiated=True)
        store.save_strategy_draft(strategy_id="owner-beta", user_id=OTHER_OWNER, source_session_id=owner_session,
            name="Owner strategy", symbol="SHFE.au2612", version=1, status="monitoring",
            definition={"template": "ma_cross"}, definition_digest="owner-digest", trading_account_id=BETA_ACCOUNT)
        client = await local_client(app)
        try:
            header = {"X-AI-Trader-Account": BETA_ACCOUNT}
            stopped = await client.post("/api/ai-execution/stop", headers=header,
                                        json={"emergency": emergency, "scope": scope})
            assert stopped.status_code == 200, stopped.text
            assert autonomy.controls(user_id=OTHER_OWNER)["stopStatus"] == status
            assert autonomy.controls(user_id=USER)["stopStatus"] == status
            assert store.get_strategy_draft("owner-beta", user_id=OTHER_OWNER)["status"] == ("monitoring" if scope == "opens" else "paused")
            assert ("owner-beta" in stopped.json()["pausedStrategyIds"]) == (scope == "all")
            assert gateway_b.cancel_count == (1 if emergency else 0)
            # Retrying never weakens the stop or sends another order.
            repeated = await client.post("/api/ai-execution/stop", headers=header,
                                         json={"emergency": emergency, "scope": scope})
            assert repeated.status_code == 200
        finally:
            await client.aclose()
        generation = autonomy.controls(user_id=OTHER_OWNER)["generation"]
        autonomy.recover_interrupted_execution()
        assert autonomy.controls(user_id=OTHER_OWNER)["stopStatus"] == status
        assert autonomy.controls(user_id=OTHER_OWNER)["generation"] == generation
        with pytest.raises(AutonomyError):
            await autonomy.execute_trade(user_id=OTHER_OWNER, session_id=owner_session, symbol="SHFE.au2612",
                side="buy_open", volume=1, price_intent="passive", reason="owner after operator stop", ai_initiated=True)
        assert gateway_b.submit_count == 1
        if scope == "opens":
            # The shared fence still permits protective closes, but never a new open.
            autonomy._require_not_stopped(OTHER_OWNER, action="close")
        with pytest.raises(AutonomyError, match="not_execution_owner"):
            autonomy.lift_stop(user_id=USER)
        with pytest.raises(AutonomyError, match="not_execution_owner"):
            autonomy.resume_runtime_access(user_id=USER, session_id=beta_session(app), symbol="SHFE.au2612")
        # Another account remains executable throughout.
        session_a = app.state.broker.create_session("local", "native", USER).session_id
        await app.state.autonomy.execute_trade(user_id=USER, session_id=session_a, symbol="SHFE.au2612",
            side="buy_open", volume=1, price_intent="passive", reason="other account", ai_initiated=True)
        assert gateway_a.submit_count == 1
        # Only the owner can explicitly resume; existing strategies stay paused.
        autonomy.resume_runtime_access(user_id=OTHER_OWNER, session_id=owner_session, symbol="SHFE.au2612")
        assert autonomy.controls(user_id=USER)["stopStatus"] is None
        await autonomy.execute_trade(user_id=OTHER_OWNER, session_id=owner_session, symbol="SHFE.au2612",
            side="buy_open", volume=1, price_intent="passive", reason="owner resumed", ai_initiated=True)
        assert gateway_b.submit_count == 2
    finally:
        app.state.store.close()


def _b_bound_tools(app) -> TraderReadOnlyTools:
    services_b = app.state.accounts.get(BETA_ACCOUNT)
    tools = TraderReadOnlyTools(
        services_b.provider, _tool_status(app),
        proposals=app.state.proposals, autonomy=services_b.autonomy,
    )
    tools.app_capabilities = app.state.app_capabilities
    return tools


async def test_t4_tools_explain_the_fence_in_chinese(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, _ = roles_app(tmp_path, monkeypatch, "tool-copy.sqlite3")
    try:
        session_id = beta_session(app)
        tools = _b_bound_tools(app)
        refused, _ = await tools.call_async(
            tool="execute_simulation_trade",
            arguments={"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1,
                       "priceIntent": "passive", "reason": "T4 operator 出卡引导"},
            turn_id="turn-t4-operator", mode="local", session_id=session_id, user_id=USER)
        assert refused["success"] is False
        body = _tool_text(refused)
        assert body["errorCode"] == "not_execution_owner"
        assert "方案卡" in body["data"]["userFacing"]
        # viewer 调急停：查看权限文案。
        app.state.store.grant_account(
            account_id=BETA_ACCOUNT, user_id=USER, role="viewer", granted_by=OTHER_OWNER)
        stopped, _ = await tools.call_async(
            tool="stop_simulation_execution", arguments={},
            turn_id="turn-t4-viewer", mode="local", session_id=session_id, user_id=USER)
        assert stopped["success"] is False
        body = _tool_text(stopped)
        assert body["errorCode"] == "account_read_only"
        assert "查看权限" in body["data"]["userFacing"]
    finally:
        app.state.store.close()


def _save_beta_strategy(store, *, status: str = "monitoring") -> str:
    store.save_strategy_draft(
        strategy_id="strategy-beta-t5", user_id=USER, source_session_id="seed-beta",
        name="乙账户策略", symbol="SHFE.au2612", version=1, status=status,
        definition={"template": "ma_cross"}, definition_digest="digest-beta",
        trading_account_id=BETA_ACCOUNT,
    )
    return "strategy-beta-t5"


async def test_t5_strategy_monitor_only_runs_for_the_execution_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, _ = roles_app(tmp_path, monkeypatch, "monitor.sqlite3")
    try:
        store = app.state.store
        strategy_id = _save_beta_strategy(store)
        services_b = app.state.accounts.get(BETA_ACCOUNT)
        results = await services_b.strategy_monitor.run_once()
        assert results[0]["status"] == "blocked"
        assert "not_execution_owner" in results[0]["detail"]
        # 不建租约、不保留执行尝试：K 线未被消耗。
        assert services_b.autonomy.current(user_id=USER) is None
        assert store.list_strategy_execution_attempts(
            strategy_id=strategy_id, user_id=USER, limit=10) == []
        client = await local_client(app)
        try:
            header = {"X-AI-Trader-Account": BETA_ACCOUNT}
            started = await client.post(f"/api/strategies/{strategy_id}/monitor/start",
                                        headers=header)
            assert started.status_code == 403 and started.json()["detail"] == "not_execution_owner"
            paused = await client.post(f"/api/strategies/{strategy_id}/monitor/pause",
                                       headers=header)
            assert paused.status_code == 200, paused.text
        finally:
            await client.aclose()
    finally:
        app.state.store.close()


async def test_t6_protection_reads_the_accounts_own_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """乙账户的保护单只看乙账户的租约：甲账户上的急停行不能替乙决定 emergency。
    这里 local-user 同时是两个账户的执行负责人，监控路径本身不受 4.3 影响。"""
    from test_strategy_monitor import ProposalStub, _definition
    app, gateway_a, gateway_b = roles_app(
        tmp_path, monkeypatch, "protection.sqlite3", beta_role="owner", beta_owner=None)
    try:
        store = app.state.store
        store.save_strategy_draft(
            strategy_id="strategy-beta-t6", user_id=USER, source_session_id="seed-beta",
            name="乙账户保护策略", symbol="SHFE.au2612", version=1, status="monitoring",
            definition=_definition(), definition_digest="a" * 64,
            trading_account_id=BETA_ACCOUNT,
        )
        intent_id = "intent-beta-t6"
        intent = {
            "intentId": intent_id,
            "strategyId": "strategy-beta-t6",
            "signalSnapshot": {"signal": "buy", "barTime": BAR_TIME},
            "positionIntent": {"action": "open_long", "requestedVolume": 2,
                               "reason": "已完成K线产生买入信号。"},
            "riskAdjustedIntent": {"status": "allowed", "action": "open_long", "volume": 2,
                                   "reason": "已完成K线产生买入信号。"},
        }
        store.save_strategy_intent_snapshot(
            intent_id=intent_id, strategy_id="strategy-beta-t6", user_id=USER,
            strategy_digest="a" * 64, signal_bar_time=BAR_TIME, context_digest="b" * 64,
            payload=intent,
        )
        services_b = app.state.accounts.get(BETA_ACCOUNT)
        services_b.strategy_monitor.proposals = ProposalStub(store, intent)
        results = await services_b.strategy_monitor.run_once()
        assert results[0]["status"] == "submitted", results
        entry = dict(gateway_b.request or {})
        provider_b = services_b.provider
        provider_b.activity_feed = {
            **provider_b.account_activity(),
            "trades": [{
                "tradeId": "fill-beta", "clientOrderId": entry["clientOrderId"],
                "symbol": entry["symbol"], "direction": entry["direction"], "offset": "OPEN",
                "volume": entry["volume"], "price": 822.0,
                "tradeDateTime": int(datetime.now(UTC).timestamp() * 1e9),
                "tradingDay": "2026-08-25",
            }],
            "orders": [{**entry, "status": "FINISHED", "volumeLeft": 0}],
        }
        # 甲账户上一条更晚的 EMERGENCY_STOP 租约：不过滤账户时会盖过乙的 ACTIVE 租约。
        store.connection.execute(
            """INSERT INTO ai_execution_authorizations
            (id, user_id, session_id, status, symbols_json, permissions_json,
             max_order_volume, max_orders, used_orders, expires_at, created_at, updated_at,
             trading_account_id)
            VALUES ('auth-alpha-emergency', ?, 'session-alpha', 'EMERGENCY_STOP',
                    '["SHFE.au2612"]', '["open"]', 1, 5, 0, '9999-12-31T23:59:59+00:00',
                    ?, ?, ?)""",
            (USER, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(), DEFAULT_ACCOUNT),
        )
        store.connection.commit()
        services_b.autonomy.sync_strategy_protection()
        row = store.connection.execute(
            """SELECT status, last_error, trading_account_id FROM ai_stop_orders
            WHERE strategy_metadata_json IS NOT NULL"""
        ).fetchone()
        assert row is not None and row["status"] == "ARMED", dict(row) if row else None
        assert row["last_error"] is None
        # The protective stop is booked on the account whose fill it guards, not the default.
        assert row["trading_account_id"] == BETA_ACCOUNT
        assert gateway_a.submit_count == 0
    finally:
        app.state.store.close()


async def test_t7_single_account_deployment_is_unchanged(tmp_path: Path) -> None:
    gateway = Gateway()
    app = application(tmp_path / "single-equal.sqlite3", gateway)
    try:
        session = app.state.broker.create_session("local", "native", USER)
        app.state.autonomy.activate(
            user_id=USER, session_id=session.session_id, symbols=["SHFE.au2612"],
            permissions=["open", "close", "stop", "cancel"], max_order_volume=1,
            max_orders=5, duration_minutes=30)
        await app.state.autonomy.execute_trade(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612",
            side="buy_open", volume=1, price_intent="passive", reason="T7 单账户等价",
            ai_initiated=True)
        assert gateway.submit_count == 1
        await app.state.autonomy.deactivate(user_id=USER)
        assert app.state.autonomy.controls(user_id=USER)["stopStatus"] == "STOPPED"
    finally:
        app.state.store.close()


async def test_shared_cny_operator_stops_its_owners_usd_ledger_without_acquiring_usd_write_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trader_runtime.execution_stop import stop_execution
    cny, usd, other = "paper:shared", "paperusd:shared", "paperusd:other"
    monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(write_accounts_file(tmp_path / "paper-accounts.json", [
        {"id": DEFAULT_ACCOUNT, "dataMode": "fake"},
        *[{"id": account_id, "dataMode": "fake", "executionOwnerUsername": "someone-else"}
          for account_id in (cny, usd, other)],
    ])))
    app = create_app(tmp_path / "shared-paper-stop.sqlite3", local_token=TOKEN, agent_mode="fake", data_mode="fake")
    try:
        store, accounts = app.state.store, app.state.accounts
        store.grant_account(account_id=cny, user_id=USER, role="operator", granted_by=OTHER_OWNER)
        resolved: list[str] = []
        async def foreign_account(user_id: str) -> str:
            resolved.append(user_id)
            return usd
        accounts.foreign_account = foreign_account
        main, foreign, unrelated = (accounts.get(key).autonomy for key in (cny, usd, other))
        # The existing native simulation gateway is enough; no PaperEngine or live service is touched.
        for service in (main, foreign, unrelated):
            service.simulation_orders.gateway = Gateway()
        with pytest.raises(AutonomyError, match="account_not_granted"):
            await foreign.deactivate(user_id=USER, emergency=True)
        result = await stop_execution(autonomy=main, proposals=app.state.proposals, accounts=accounts,
                                      user_id=USER, emergency=True, scope="all")
        assert result["companion"]["accountId"] == usd and "error" not in result["companion"]
        assert resolved == [OTHER_OWNER]
        assert main.controls(user_id=OTHER_OWNER)["stopStatus"] == "EMERGENCY_STOP"
        assert foreign.controls(user_id=OTHER_OWNER)["stopStatus"] == "EMERGENCY_STOP"
        assert unrelated.controls(user_id=OTHER_OWNER)["stopStatus"] is None
        assert store.account_role(account_id=usd, user_id=USER) is None
        with pytest.raises(AutonomyError, match="not_execution_owner"):
            foreign.lift_stop(user_id=USER)
        with pytest.raises(AutonomyError, match="account_not_granted"):
            await unrelated.deactivate(user_id=USER, emergency=True, parent_account_id=cny)
        foreign.lift_stop(user_id=OTHER_OWNER)
        assert foreign.controls(user_id=OTHER_OWNER)["stopStatus"] is None
    finally:
        app.state.store.close()

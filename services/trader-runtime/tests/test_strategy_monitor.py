from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.broker import EventBroker
from trader_runtime.main import create_app
from trader_runtime.proposals import ProposalConflict, ProposalService
from trader_runtime.store import RuntimeStore
from trader_runtime.strategy_monitor import (
    MARKET_WAIT_STALLED_SECONDS,
    StrategyMonitorService,
    _bar_gap,
)
from trader_runtime.trader_tools import TraderReadOnlyTools

USER = "local-user"
STRATEGY_ID = "strategy-monitor-test"
BAR_TIME = "1767225600000000000"


def _definition(*, max_orders: int = 20, cooldown: int = 0) -> dict[str, Any]:
    return {
        "timeframeSeconds": 300,
        "riskLimits": {
            "maxPosition": 2,
            "maxOrdersPerDay": max_orders,
            "stopLossTicks": 2,
            "takeProfitTicks": 4,
            "cooldownBars": cooldown,
        },
    }


def _save_strategy(
    store: RuntimeStore, *, status: str = "monitoring", max_orders: int = 20,
) -> dict[str, Any]:
    # Every real boot registers the default account with its execution owner
    # (4.2 wiring); the monitor's 4.3 ownership check reads that row.
    store.ensure_default_trading_account(
        execution_owner_user_id=USER, provider_kind="fake",
        display_name="假数据模拟账户", sidecar_url=None,
    )
    return store.save_strategy_draft(
        strategy_id=STRATEGY_ID,
        user_id=USER,
        source_session_id="session-strategy-monitor",
        name="已验证模拟策略",
        symbol="SHFE.au2612",
        version=1,
        status=status,
        definition=_definition(max_orders=max_orders),
        definition_digest="a" * 64,
    )


def _intent(store: RuntimeStore) -> dict[str, Any]:
    intent_id = "intent-strategy-monitor"
    payload = {
        "intentId": intent_id,
        "strategyId": STRATEGY_ID,
        "signalSnapshot": {"signal": "buy", "barTime": BAR_TIME},
        "positionIntent": {
            "action": "open_long",
            "requestedVolume": 2,
            "reason": "已完成K线产生买入信号。",
        },
        "riskAdjustedIntent": {
            "status": "allowed",
            "action": "open_long",
            "volume": 2,
            "reason": "已完成K线产生买入信号。",
        },
    }
    store.save_strategy_intent_snapshot(
        intent_id=intent_id,
        strategy_id=STRATEGY_ID,
        user_id=USER,
        strategy_digest="a" * 64,
        signal_bar_time=BAR_TIME,
        context_digest="b" * 64,
        payload=payload,
    )
    return payload


class ProposalStub:
    def __init__(self, store: RuntimeStore, intent: dict[str, Any]) -> None:
        self.store = store
        self.intent = intent

    def preview_strategy_intent(self, **_: Any) -> dict[str, Any]:
        return self.intent

    def transition_strategy_status(
        self, *, strategy_id: str, user_id: str, target: str,
    ) -> dict[str, Any]:
        current = self.store.get_strategy_draft(strategy_id, user_id=user_id)
        return self.store.update_strategy_draft_status(
            strategy_id,
            user_id=user_id,
            expected_status=current["status"],
            status=target,
        )


class FailingProposalStub(ProposalStub):
    def preview_strategy_intent(self, **_: Any) -> dict[str, Any]:
        raise OSError("provider_protocol_changed")


class DataStub:
    def runtime_status(self) -> dict[str, Any]:
        return {"marketDataStatus": "normal", "accountStatus": "normal"}

    def __init__(
        self, *, equity: float = 10_000, pnl: float = 0,
        trading_day: str | None = None,
    ) -> None:
        self.equity = equity
        self.pnl = pnl
        self.activity: dict[str, Any] = {"orders": [], "trades": []}
        self.position_rows: list[dict[str, Any]] = []
        self.trading_day = trading_day or datetime.now(UTC).date().isoformat()

    def account_summary(self, _: str) -> dict[str, Any]:
        return {
            "equity": self.equity,
            "todayNetPnl": self.pnl,
            "tradingDay": self.trading_day,
        }

    def account_activity(self) -> dict[str, Any]:
        return self.activity

    def market_snapshot(self, _: str, __: str) -> dict[str, Any]:
        return {"price": 823.5}

    def positions(self) -> list[dict[str, Any]]:
        return self.position_rows


class AutonomyStub:
    enabled = True

    def __init__(
        self, store: RuntimeStore, data: DataStub,
        outcomes: list[str | Exception] | None = None,
    ) -> None:
        self.data_provider = data
        # The tool's start path reads the same attributes the real autonomy carries.
        self.enabled = True
        self.account_id = store.default_trading_account_id
        self.broker = EventBroker(store)
        self.simulation_orders = SimpleNamespace(
            policies=SimpleNamespace(
                get=lambda **_: SimpleNamespace(max_daily_loss=Decimal("1000"))
            )
        )
        self.calls: list[dict[str, Any]] = []
        self.outcomes = list(outcomes or ["已受理"])
        # 设置 → 交易 → AI 自主执行 (user_preferences.trade.aiAutonomy); the monitor reads it before
        # every submission.
        self.ai_autonomy = True
        # The stop fence the monitor reads before opens (HALT_OPENS blocks opens only).
        self.stop_status: str | None = None

    def autonomy_enabled(self, user_id: str) -> bool:
        return self.ai_autonomy

    def controls(self, *, user_id: str) -> dict[str, Any]:
        return {"stopStatus": self.stop_status}

    async def execute_trade(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(arguments)
        outcome = self.outcomes.pop(0) if self.outcomes else "已受理"
        if isinstance(outcome, Exception):
            raise outcome
        return {
            "clientOrderId": f"strategy-order-{len(self.calls)}",
            "simulationOrder": {"displayStatus": outcome},
        }


@pytest.mark.asyncio
async def test_monitor_submits_at_most_once_for_one_completed_bar(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "monitor.sqlite3")
    _save_strategy(store)
    intent = _intent(store)
    autonomy = AutonomyStub(store, DataStub())
    monitor = StrategyMonitorService(
        store=store,
        proposals=ProposalStub(store, intent),  # type: ignore[arg-type]
        autonomy=autonomy,  # type: ignore[arg-type]
    )

    first = await monitor.run_once()
    second = await monitor.run_once()

    assert first[0]["status"] == "submitted"
    assert second[0]["status"] in {"blocked", "duplicate"}
    assert len(autonomy.calls) == 1
    attempts = store.list_strategy_execution_attempts(
        strategy_id=STRATEGY_ID, user_id=USER
    )
    assert len(attempts) == 1
    assert attempts[0]["status"] == "SUBMITTED"
    assert store.connection.execute(
        "SELECT COUNT(*) FROM ai_execution_authorizations"
    ).fetchone()[0] == 0
    store.close()


@pytest.mark.asyncio
async def test_latest_results_count_the_run_for_the_conversation_banner(tmp_path: Path) -> None:
    """1.6.6: the 对话面板 banner shows 运行时长 and 判断次数 from `decision.since` / `decision.checks`."""
    store = RuntimeStore(tmp_path / "banner.sqlite3")
    _save_strategy(store)
    intent = _intent(store)
    monitor = StrategyMonitorService(
        store=store,
        proposals=ProposalStub(store, intent),  # type: ignore[arg-type]
        autonomy=AutonomyStub(store, DataStub()),  # type: ignore[arg-type]
    )

    await monitor.run_once()
    first = monitor.latest_results[STRATEGY_ID]
    assert first["checks"] == 1 and first["since"] == first["checkedAt"]
    await monitor.run_once()
    second = monitor.latest_results[STRATEGY_ID]
    assert second["checks"] == 2
    assert second["since"] == first["since"]  # the run's start, not this judgement's time
    # Starting the monitor again drops the entry, so both restart with the new run.
    monitor.latest_results.pop(STRATEGY_ID, None)
    await monitor.run_once()
    assert monitor.latest_results[STRATEGY_ID]["checks"] == 1
    store.close()


@pytest.mark.asyncio
async def test_monitor_records_not_dispatched_without_consuming_cooldown_bar(
    tmp_path: Path,
) -> None:
    store = RuntimeStore(tmp_path / "not-dispatched.sqlite3")
    _save_strategy(store)
    intent = _intent(store)
    autonomy = AutonomyStub(store, DataStub(), ["待发送"])
    monitor = StrategyMonitorService(
        store=store,
        proposals=ProposalStub(store, intent),  # type: ignore[arg-type]
        autonomy=autonomy,  # type: ignore[arg-type]
    )

    result = await monitor.run_once()

    assert result[0]["status"] == "not_dispatched"
    attempt = store.list_strategy_execution_attempts(
        strategy_id=STRATEGY_ID, user_id=USER
    )[0]
    assert attempt["status"] == "NOT_DISPATCHED"
    assert attempt["legs"][0]["status"] == "NOT_DISPATCHED"
    state = store.strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER)
    assert state is not None and state["lastExecutedBar"] is None
    store.close()


@pytest.mark.asyncio
async def test_monitor_preserves_successful_close_leg_when_second_leg_fails(
    tmp_path: Path,
) -> None:
    store = RuntimeStore(tmp_path / "partial-close.sqlite3")
    _save_strategy(store)
    intent = _intent(store)
    intent["riskAdjustedIntent"].update({"action": "close_long", "volume": 4})
    data = DataStub()
    data.position_rows = [{
        "symbol": "SHFE.au2612",
        "direction": "long",
        "volume": 5,
        "todayVolume": 2,
        "historicalVolume": 3,
    }]
    autonomy = AutonomyStub(
        store, data, ["已受理", RuntimeError("second_leg_failed")]
    )
    monitor = StrategyMonitorService(
        store=store,
        proposals=ProposalStub(store, intent),  # type: ignore[arg-type]
        autonomy=autonomy,  # type: ignore[arg-type]
    )

    result = await monitor.run_once()

    assert result[0]["status"] == "partial"
    attempt = store.list_strategy_execution_attempts(
        strategy_id=STRATEGY_ID, user_id=USER
    )[0]
    assert attempt["status"] == "PARTIAL"
    assert [leg["status"] for leg in attempt["legs"]] == ["SUBMITTED", "FAILED"]
    assert attempt["legs"][0]["clientOrderId"] == "strategy-order-1"
    store.close()


@pytest.mark.asyncio
async def test_monitor_persists_unknown_failure_for_operator_visibility(
    tmp_path: Path,
) -> None:
    store = RuntimeStore(tmp_path / "monitor-error.sqlite3")
    _save_strategy(store)
    intent = _intent(store)
    monitor = StrategyMonitorService(
        store=store,
        proposals=FailingProposalStub(store, intent),  # type: ignore[arg-type]
        autonomy=AutonomyStub(store, DataStub()),  # type: ignore[arg-type]
    )

    result = await monitor.run_once()

    assert result[0]["status"] == "error"
    state = store.strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER)
    assert state is not None
    assert state["consecutiveFailures"] == 1
    assert "provider_protocol_changed" in state["lastError"]
    assert state["nextRetryAt"] is not None

    # 普通策略（没有同轮清算/停机状态行）的失败照旧计数：第三次自动暂停——#698 复审的
    # 清算隔离只在 run_once 的异常归类入口按持久状态行判同轮，不放宽普通失败路径。
    for expected in ("error", "paused"):
        with store.lock, store.connection:
            store.connection.execute(
                "UPDATE strategy_monitor_states SET next_retry_at = '2000-01-01T00:00:00+00:00'")
        result = await monitor.run_once()
        assert result[0]["status"] == expected
    assert result[0]["status"] == "paused"
    assert "策略监控连续失败三次" in result[0]["detail"]
    assert store.get_strategy_draft(STRATEGY_ID, user_id=USER)["status"] == "paused"
    store.close()


@pytest.mark.asyncio
async def test_monitor_pauses_when_account_drawdown_reaches_budget(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "drawdown.sqlite3")
    _save_strategy(store)
    intent = _intent(store)
    today = datetime.now(UTC).date().isoformat()
    store.save_strategy_monitor_state(
        strategy_id=STRATEGY_ID,
        user_id=USER,
        trading_date=today,
        high_watermark_equity="10000",
        last_today_pnl="0",
        consecutive_losses=0,
        last_trade_id=None,
        last_executed_bar=None,
        pause_reason=None,
    )
    autonomy = AutonomyStub(store, DataStub(equity=8_900))
    monitor = StrategyMonitorService(
        store=store,
        proposals=ProposalStub(store, intent),  # type: ignore[arg-type]
        autonomy=autonomy,  # type: ignore[arg-type]
    )

    result = await monitor.run_once()

    assert result[0]["status"] == "paused"
    assert autonomy.calls == []
    assert store.get_strategy_draft(STRATEGY_ID, user_id=USER)["status"] == "paused"
    state = store.strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER)
    assert state is not None and "回撤" in str(state["pauseReason"])
    store.close()


def test_bar_gap_counts_the_bars_between_two_bar_times() -> None:
    assert _bar_gap("1767226200000000000", BAR_TIME, 300) == 2


def test_strategy_monitor_tools_start_and_pause_without_authorization(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "monitor-tools.sqlite3")
    _save_strategy(store, status="ready")
    store.save_strategy_validation_report(
        report_id="validation-monitor-tools",
        strategy_id=STRATEGY_ID,
        user_id=USER,
        strategy_digest="a" * 64,
        status="passed",
        checks=[],
        evidence={},
    )
    proposals = ProposalService(store, EventBroker(store))
    # The tool's start path now delegates to the account's monitor (the same shared entry
    # HTTP monitor/start uses, 2026-10-10 #692): a real monitor with the stub autonomy.
    autonomy = AutonomyStub(store, DataStub())
    StrategyMonitorService(store=store, proposals=proposals, autonomy=autonomy)
    provider = SimpleNamespace(mode="fake")
    tools = TraderReadOnlyTools(
        provider,  # type: ignore[arg-type]
        lambda mode: {"mode": mode},
        proposals=proposals,
        autonomy=autonomy,  # type: ignore[arg-type]
    )

    started, _ = tools.call(
        tool="start_strategy_monitoring",
        arguments={"strategyId": STRATEGY_ID},
        turn_id=str(uuid4()),
        mode="local",
        user_id=USER,
    )
    paused, _ = tools.call(
        tool="pause_strategy_monitoring",
        arguments={"strategyId": STRATEGY_ID},
        turn_id=str(uuid4()),
        mode="local",
        user_id=USER,
    )

    assert started["success"] is True
    assert paused["success"] is True
    assert store.get_strategy_draft(STRATEGY_ID, user_id=USER)["status"] == "paused"
    assert store.connection.execute(
        "SELECT COUNT(*) FROM ai_execution_authorizations"
    ).fetchone()[0] == 0
    store.close()


@pytest.mark.asyncio
async def test_strategy_monitor_api_starts_and_pauses_without_authorization(
    tmp_path: Path,
) -> None:
    app = create_app(
        tmp_path / "monitor-api.sqlite3",
        local_token="test-local-token",
        agent_mode="fake",
        simulation_gateway=SimpleNamespace(),
    )
    _save_strategy(app.state.store, status="ready")
    app.state.store.save_strategy_validation_report(
        report_id="validation-monitor-api",
        strategy_id=STRATEGY_ID,
        user_id=USER,
        strategy_digest="a" * 64,
        status="passed",
        checks=[],
        evidence={},
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as client:
        authenticated = await client.post(
            "/api/auth/local",
            headers={"Authorization": "Bearer test-local-token"},
        )
        assert authenticated.status_code == 200

        started = await client.post(
            f"/api/strategies/{STRATEGY_ID}/monitor/start", json={}
        )
        paused = await client.post(
            f"/api/strategies/{STRATEGY_ID}/monitor/pause", json={}
        )
        status = await client.get(f"/api/strategies/{STRATEGY_ID}/monitor")

    assert started.status_code == 200
    assert started.json()["status"] == "monitoring"
    assert paused.status_code == 200
    assert status.json()["status"] == "paused"
    assert status.json()["recentAttempts"] == []
    assert status.json()["opensToday"] is None  # no monitor pass yet: no trading day to count on
    assert app.state.store.connection.execute(
        "SELECT COUNT(*) FROM ai_execution_authorizations"
    ).fetchone()[0] == 0
    app.state.store.close()


@pytest.mark.asyncio
async def test_monitor_payload_counts_todays_opens_like_the_daily_cap(tmp_path: Path) -> None:
    """opensToday is the count the monitor checks maxOrdersPerDay with: today's dispatched
    opens only, all of them (not just the ten recent attempts the payload lists)."""
    app = create_app(tmp_path / "opens-today.sqlite3", local_token="test-local-token", agent_mode="fake",
                     simulation_gateway=SimpleNamespace())
    store = app.state.store
    _save_strategy(store, status="monitoring")
    _intent(store)
    # "Today" is the account's own trading day (the fake account reports 2026-08-25).
    today = app.state.data_provider.account_summary("test")["tradingDay"]
    store.save_strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER, trading_date=today, high_watermark_equity="10000",
                                      last_today_pnl="0", consecutive_losses=0, last_trade_id=None, last_executed_bar=None, pause_reason=None)
    now = datetime.now(UTC).isoformat()
    rows = [(f"open-{i}", f"bar-{i}", today, "open_long", "SUBMITTED") for i in range(12)]
    rows += [("close-1", "bar-c", today, "close_long", "SUBMITTED"), ("failed-open", "bar-f", today, "open_short", "FAILED"),
             ("yesterday-open", "bar-y", "2000-01-01", "open_long", "SUBMITTED")]
    for attempt_id, bar, day, action, status in rows:
        store.connection.execute("""INSERT INTO strategy_execution_attempts (id, strategy_id, user_id, intent_id, signal_bar_time,
            trading_day, action, volume, status, created_at, updated_at) VALUES (?, ?, ?, 'intent-strategy-monitor', ?, ?, ?, 1, ?, ?, ?)""",
            (attempt_id, STRATEGY_ID, USER, bar, day, action, status, now, now))
    store.connection.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        payload = (await client.get(f"/api/strategies/{STRATEGY_ID}/monitor")).json()
    assert len(payload["recentAttempts"]) == 10
    assert payload["opensToday"] == 12 == store.count_strategy_attempts_today(strategy_id=STRATEGY_ID, user_id=USER, trading_date=today)
    store.close()


# -- 策略只管自己开的仓 (所有者 2026-10-08) --------------------------------------------------------


class CapturingProposals(ProposalStub):
    """Records what the monitor asks the preview for — here: the own-position 口径 by mode."""

    def __init__(self, store: RuntimeStore, intent: dict[str, Any]) -> None:
        super().__init__(store, intent)
        self.seen: dict[str, Any] = {}

    def preview_strategy_intent(self, **kwargs: Any) -> dict[str, Any]:
        self.seen = kwargs
        return self.intent


def _hold_intent(store: RuntimeStore) -> dict[str, Any]:
    intent = _intent(store)
    intent["riskAdjustedIntent"] = {"status": "no_action", "action": "hold", "volume": 0, "checks": [],
                                    "reason": "当前没有新的开平仓动作。"}
    return intent


def _own_protection_row(store: RuntimeStore, *, volume: int = 1) -> None:
    """An armed strategy protection row: what 停止并平仓 and the monitor's position read count."""
    now = datetime.now(UTC).isoformat()
    store.connection.execute("PRAGMA foreign_keys = OFF")   # the authorization / session rows are not the point here
    store.connection.execute(
        "INSERT INTO ai_stop_orders (id,user_id,authorization_id,session_id,symbol,position_direction,position_bucket,"
        "volume,trigger_price,price_intent,status,strategy_metadata_json,remaining_volume,created_at,updated_at)"
        " VALUES ('own-stop',?,'auth','s','SHFE.au2612','long','today',?,'820','passive','ARMED',?,?,?,?)",
        (USER, volume, '{"strategyId":"strategy-monitor-test"}', volume, now, now))
    store.connection.commit()


@pytest.mark.asyncio
async def test_auto_and_confirm_judge_off_the_strategys_own_lots(tmp_path: Path) -> None:
    """自动下单与出卡确认把策略自己的仓（保护行口径、按账户封顶）交给意图判断，不是账户持仓。"""
    store = RuntimeStore(tmp_path / "own-auto.sqlite3")
    _save_strategy(store)
    proposals = CapturingProposals(store, _hold_intent(store))
    data = DataStub()
    data.position_rows = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 2}]  # 手动/AI 的 2 手
    autonomy = AutonomyStub(store, data)
    monitor = StrategyMonitorService(store=store, proposals=proposals, autonomy=autonomy)  # type: ignore[arg-type]

    await monitor.run_once()
    assert proposals.seen["own_position"] == (None, 0)   # 不是账户的 2 手

    _own_protection_row(store)                          # 策略自己的 1 手（账户 2 手封顶之下）
    await monitor.run_once()
    assert proposals.seen["own_position"] == ("long", 1)

    # AI 自主执行关掉后 auto 降级为 confirm：口径不变。
    autonomy.ai_autonomy = False
    await monitor.run_once()
    assert proposals.seen["own_position"] == ("long", 1)
    store.close()


@pytest.mark.asyncio
async def test_alert_keeps_the_account_wide_read_and_spreads_keep_their_legs(tmp_path: Path) -> None:
    """只提醒仍按账户该合约持仓判断（手动开仓后还能收到平仓提醒）；价差不适用策略自己的仓。"""
    store = RuntimeStore(tmp_path / "own-alert.sqlite3")
    _save_strategy(store)
    store.set_strategy_signal_action(STRATEGY_ID, user_id=USER, signal_action="alert", session_id=None)
    _own_protection_row(store)
    proposals = CapturingProposals(store, _hold_intent(store))
    monitor = StrategyMonitorService(
        store=store, proposals=proposals, autonomy=AutonomyStub(store, DataStub()),  # type: ignore[arg-type]
    )

    await monitor.run_once()
    assert proposals.seen["own_position"] is None       # 账户口径，保护行不参与
    strategy = store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    assert monitor._own_position(strategy, plan=SimpleNamespace(spread=object())) is None
    store.close()


class ScriptedProposalStub(ProposalStub):
    """Raises each scripted error once, in order, then returns the intent."""

    def __init__(self, store: RuntimeStore, intent: dict[str, Any], errors: list[Exception]) -> None:
        super().__init__(store, intent)
        self.errors = list(errors)

    def preview_strategy_intent(self, **_: Any) -> dict[str, Any]:
        if self.errors:
            raise self.errors.pop(0)
        return self.intent


@pytest.mark.asyncio
@pytest.mark.parametrize("make_error", [
    # K 线新订阅还在载入：evaluate_plan 保留 sidecar 的码，经 ProposalConflict 到监控
    lambda: ProposalConflict("market_series_not_ready", "5 分钟行情读取失败，请刷新行情后重试"),
    # 订阅名额已满
    lambda: ProposalConflict("market_subscription_limit", "5 分钟行情读取失败，请刷新行情后重试"),
    # 夜盘开盘头几秒：交易时段内报价还没新鲜
    lambda: ProposalConflict("strategy_market_not_fresh", "5 分钟：strategy_market_not_fresh"),
    # 旧版策略直接读工作台：provider 的 RuntimeError 本身就是码
    lambda: RuntimeError("market_quote_not_ready"),
])
async def test_market_not_ready_waits_without_pausing_then_trades(tmp_path: Path, make_error) -> None:
    store = RuntimeStore(tmp_path / "monitor-wait.sqlite3")
    _save_strategy(store)
    intent = _intent(store)
    autonomy = AutonomyStub(store, DataStub())
    monitor = StrategyMonitorService(
        store=store,
        proposals=ScriptedProposalStub(store, intent, [make_error() for _ in range(6)]),  # type: ignore[arg-type]
        autonomy=autonomy,  # type: ignore[arg-type]
    )

    for _ in range(6):  # 两倍于失败暂停门槛
        result = (await monitor.run_once())[0]
        assert result["status"] == "waiting", result
        assert result["detail"].startswith("等待行情：") and result["stalled"] is False
    assert store.get_strategy_draft(STRATEGY_ID, user_id=USER)["status"] == "monitoring"
    state = store.strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER)
    assert state is None or (state["consecutiveFailures"] == 0 and state["nextRetryAt"] is None)
    assert autonomy.calls == []

    # 行情恢复：同一轮询照常评估并下单
    assert (await monitor.run_once())[0]["status"] == "submitted"
    assert len(autonomy.calls) == 1
    store.close()


@pytest.mark.asyncio
async def test_long_market_wait_says_so_and_closed_session_never_stalls(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "monitor-stall.sqlite3")
    _save_strategy(store)
    intent = _intent(store)
    data = DataStub()
    monitor = StrategyMonitorService(
        store=store,
        proposals=ScriptedProposalStub(store, intent, [  # type: ignore[arg-type]
            ProposalConflict("market_series_not_ready", "x"), ProposalConflict("market_series_not_ready", "x"),
            ProposalConflict("strategy_market_not_fresh", "x"), ProposalConflict("strategy_market_not_fresh", "x"),
        ]),
        autonomy=AutonomyStub(store, data),  # type: ignore[arg-type]
    )
    first = (await monitor.run_once())[0]
    assert first["waitReason"] == "market_series_not_ready" and first["stalled"] is False
    since, _ = monitor._waiting_since[STRATEGY_ID]
    monitor._waiting_since[STRATEGY_ID] = (since - timedelta(seconds=MARKET_WAIT_STALLED_SECONDS + 60), False)
    stalled = (await monitor.run_once())[0]
    assert stalled["status"] == "waiting" and stalled["stalled"] is True
    assert stalled["detail"].startswith("等待行情已 11 分钟：所需周期 K 线正在载入")
    assert store.get_strategy_draft(STRATEGY_ID, user_id=USER)["status"] == "monitoring"

    # 休市中行情不新鲜：照旧是休市等待，不算行情卡住
    data.market_snapshot = lambda *_: {"price": 823.5, "marketSessionStatus": "closed"}  # type: ignore[method-assign]
    closed = (await monitor.run_once())[0]
    assert closed["detail"] == "market_session_closed" and closed["stalled"] is False
    # 开盘后仍未新鲜：等待时钟从开盘重新算
    data.market_snapshot = lambda *_: {"price": 823.5, "marketSessionStatus": "trading"}  # type: ignore[method-assign]
    opening = (await monitor.run_once())[0]
    assert opening["waitReason"] == "strategy_market_not_fresh" and opening["stalled"] is False
    assert (await monitor.run_once())[0]["status"] == "submitted"
    assert STRATEGY_ID not in monitor._waiting_since
    store.close()


@pytest.mark.asyncio
async def test_paused_strategy_resumes_with_a_fresh_wait_clock(tmp_path: Path) -> None:
    """暂停/停止期间不算等待：恢复后第一次等待行情从头计时，不被报成卡住。"""
    store = RuntimeStore(tmp_path / "monitor-wait-resume.sqlite3")
    _save_strategy(store)
    monitor = StrategyMonitorService(
        store=store,
        proposals=ScriptedProposalStub(store, _intent(store), [  # type: ignore[arg-type]
            ProposalConflict("market_series_not_ready", "x"), ProposalConflict("market_series_not_ready", "x"),
        ]),
        autonomy=AutonomyStub(store, DataStub()),  # type: ignore[arg-type]
    )
    assert (await monitor.run_once())[0]["status"] == "waiting"
    since, _ = monitor._waiting_since[STRATEGY_ID]
    monitor._waiting_since[STRATEGY_ID] = (since - timedelta(hours=3), False)
    store.update_strategy_draft_status(STRATEGY_ID, user_id=USER, expected_status="monitoring", status="paused")
    assert await monitor.run_once() == []
    store.update_strategy_draft_status(STRATEGY_ID, user_id=USER, expected_status="paused", status="monitoring")
    resumed = (await monitor.run_once())[0]
    assert resumed["status"] == "waiting" and resumed["stalled"] is False
    assert resumed["detail"].startswith("等待行情：")
    store.close()


def test_workbench_read_failure_keeps_the_provider_code() -> None:
    """evaluate_plan 不再把 sidecar 的码吞成无码 ValueError：码留给监控分类，句子留给人。"""
    from trader_runtime.strategy_rules import StrategyRuleError, workbench_read_error

    for raised, code in ((RuntimeError("market_series_not_ready"), "market_series_not_ready"),
                         (RuntimeError("market_subscription_limit"), "market_subscription_limit"),
                         (RuntimeError("sidecar 断了 bad payload"), "market_workbench_unavailable"),
                         (KeyError("SHFE.xx"), "market_symbol_unknown")):
        error = workbench_read_error(raised, "5 分钟行情读取失败，请刷新行情后重试")
        assert isinstance(error, StrategyRuleError) and isinstance(error, ValueError)
        assert error.code == code and str(error) == "5 分钟行情读取失败，请刷新行情后重试"

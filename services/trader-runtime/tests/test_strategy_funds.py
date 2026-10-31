"""按资金比例定手数与回撤停机 (所有者 2026-10-09) 的行为测试。

预算核心的算术与拒绝口径（不足一手、maxLots 帽、陈旧缺数据）、意图定手数与发送前复核、
回撤只按本策略归属净值（含 unattributed 的如实 unavailable）、回测同口径（缺保证金拒绝），
以及走模拟网关的完整回撤轮：触发 → 撤开仓/保护性平仓 → 成交回读 → stopped → 显式重置，
普通启动不重置、liquidating 拒绝重置、急停保留待平。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from test_simulation_orders import USER, Gateway, NormalProvider, application
from trader_runtime.autonomy import AutonomyError
from trader_runtime.store import RuntimeStore
from trader_runtime.strategy_backtest import BacktestSetup, execute, run_backtest
from trader_runtime.strategy_engine import MarketContext, equity_position_size
from trader_runtime.strategy_funds import (
    compute_budget,
    curve_append,
    drawdown_percent,
    live_budget,
    pending_open_lots,
    sample_curve,
    strategy_round_facts,
)
from trader_runtime.strategy_intents import build_strategy_intent
from trader_runtime.strategy_monitor import StrategyMonitorService
from trader_runtime.strategy_rules import StrategyPlan, StrategyRuleError
from trader_runtime.strategy_workbench import definition_for

SYMBOL = "SHFE.au2612"
DIGEST = "e" * 64
ALWAYS = [{"label": "总是", "left": {"indicator": "close", "timeframe": 300}, "operator": "gt",
           "right": {"indicator": "constant", "value": 0}}]
NEVER = [{"label": "从不", "left": {"indicator": "close", "timeframe": 300}, "operator": "lt",
          "right": {"indicator": "constant", "value": 0}}]


def _plan(**fields: Any) -> StrategyPlan:
    return StrategyPlan.model_validate({
        "name": "资金比例", "description": "资金比例测试", "direction": "long", "timeframeSeconds": 300,
        "maxPosition": 1, "stopLossTicks": 2, "takeProfitTicks": None, "cooldownBars": 0,
        "rules": {"entryLong": {"conditions": ALWAYS}, "exitLong": {"conditions": NEVER}}, **fields})


# -- 预算核心：算术与拒绝 ----------------------------------------------------------------------------


def test_budget_arithmetic_off_the_owners_example():
    # 权益 100 万、比例 10%、已占 4 万、每手保证金 2 万：剩 6 万 → 3 手（向下取整，不四舍五入）。
    funds = compute_budget(equity=1_000_000, equity_percent=10, margin_per_lot=20_000,
                           used_lots=2, pending_lots=0, available=1_000_000, fee_per_lot=0,
                           max_lots=None)
    assert funds["lots"] == 3
    assert funds["usedMargin"] == 40_000 and funds["pendingMargin"] == 0
    assert funds["remainingBudget"] == 60_000 and funds["unavailableReason"] is None


def test_pending_open_orders_occupy_the_budget():
    funds = compute_budget(equity=1_000_000, equity_percent=10, margin_per_lot=20_000,
                           used_lots=0, pending_lots=2, available=1_000_000, fee_per_lot=0,
                           max_lots=None)
    assert funds["lots"] == 3 and funds["pendingMargin"] == 40_000


def test_maxlots_caps_the_strategys_total_and_says_why():
    capped = compute_budget(equity=1_000_000, equity_percent=10, margin_per_lot=20_000,
                            used_lots=1, pending_lots=0, available=1_000_000, fee_per_lot=0,
                            max_lots=2)
    assert capped["lots"] == 1
    full = compute_budget(equity=1_000_000, equity_percent=10, margin_per_lot=20_000,
                          used_lots=1, pending_lots=1, available=1_000_000, fee_per_lot=0,
                          max_lots=2)
    assert full["lots"] == 0 and "总手数上限" in full["unavailableReason"]


def test_below_one_lot_and_affordability_are_refused_with_reasons():
    small = compute_budget(equity=100_000, equity_percent=15, margin_per_lot=20_000,
                           used_lots=0, pending_lots=0, available=1_000_000, fee_per_lot=0,
                           max_lots=None)
    assert small["lots"] == 0 and "不足 1 手" in small["unavailableReason"]
    # 可用资金要盖住保证金＋开仓手续费：手续费让第 2 手买不起。
    fees = compute_budget(equity=1_000_000, equity_percent=10, margin_per_lot=20_000,
                          used_lots=0, pending_lots=0, available=20_500, fee_per_lot=600,
                          max_lots=None)
    assert fees["lots"] == 0 and "可用资金" in fees["unavailableReason"]


def test_missing_or_stale_inputs_never_open():
    for funds in (
        compute_budget(equity=None, equity_percent=10, margin_per_lot=20_000, used_lots=0,
                       pending_lots=0, available=1_000_000, fee_per_lot=0, max_lots=None),
        compute_budget(equity=1_000_000, equity_percent=10, margin_per_lot=None, used_lots=0,
                       pending_lots=0, available=1_000_000, fee_per_lot=0, max_lots=None),
        compute_budget(equity=1_000_000, equity_percent=10, margin_per_lot=20_000, used_lots=0,
                       pending_lots=0, available=None, fee_per_lot=0, max_lots=None),
        compute_budget(equity=1_000_000, equity_percent=10, margin_per_lot=20_000, used_lots=0,
                       pending_lots=0, available=1_000_000, fee_per_lot=None, max_lots=None),
    ):
        assert funds["lots"] == 0 and funds["unavailableReason"]


# -- 规则与意图：旧默认不变、新模式的定手数 ------------------------------------------------------------


def test_old_modes_and_defaults_are_unchanged_while_equity_percent_rejects_gaps():
    # 旧 JSON：sizing 缺省 = fixed，手数仍是 maxPosition；equity_percent 必须给 equityPercent。
    plain = StrategyPlan.model_validate({
        "name": "旧策略", "description": "旧默认", "direction": "long", "timeframeSeconds": 300,
        "maxPosition": 3, "stopLossTicks": 2, "takeProfitTicks": None, "cooldownBars": 0,
        "rules": {"entryLong": {"conditions": ALWAYS}}})
    assert plain.sizing is None
    lots, reason = equity_position_size(plain, MarketContext(price_tick=0.5, volume_multiple=1000))
    assert (lots, reason) == (3, None)
    with pytest.raises(ValueError, match="按资金比例定手数须设置"):
        StrategyPlan.model_validate({
            "name": "缺比例", "description": "缺比例", "direction": "long", "timeframeSeconds": 300,
            "maxPosition": 1, "stopLossTicks": 2, "cooldownBars": 0,
            "sizing": {"mode": "equity_percent"},
            "rules": {"entryLong": {"conditions": ALWAYS}}})
    # maxPosition 的旧默认 1 不许悄悄卡住新模式（10 万 × 40% ÷ 1 000 = 40 手）。
    funds_plan = _plan(sizing={"mode": "equity_percent", "equityPercent": 40})
    sized, block = equity_position_size(
        funds_plan, MarketContext(price_tick=0.5, volume_multiple=1000,
                                  funds=compute_budget(equity=100_000, equity_percent=40,
                                                       margin_per_lot=1_000, used_lots=0, pending_lots=0,
                                                       available=100_000, fee_per_lot=0, max_lots=None)))
    assert (sized, block) == (40, None)
    empty, why = equity_position_size(
        funds_plan, MarketContext(price_tick=0.5, volume_multiple=1000,
                                  funds={"lots": 0, "unavailableReason": "行情或账户数据不新鲜"}))
    assert empty == 0 and why == "行情或账户数据不新鲜"


def test_spread_refuses_both_new_features_in_chinese():
    spread = {"legs": [{"symbol": "SHFE.au2612", "lots": 1}, {"symbol": "SHFE.ag2612", "lots": 1}]}
    with pytest.raises(ValueError, match="价差策略暂不支持按资金比例定手数"):
        _plan(spread=spread, sizing={"mode": "equity_percent", "equityPercent": 10}).check_executable()
    with pytest.raises(ValueError, match="价差策略暂不支持回撤停机"):
        _plan(spread=spread, maxDrawdownPercent=5).check_executable()


def test_intent_sizes_the_entry_off_the_shared_budget():
    strategy = {
        "strategyId": "strategy-funds", "symbol": SYMBOL, "definitionDigest": DIGEST,
        "definition": {"schemaVersion": 2, "riskLimits": {
            "maxPosition": 1, "maxOrdersPerDay": 20, "stopLossTicks": 2,
            "takeProfitTicks": 4, "cooldownBars": 0},
            "plan": _plan(sizing={"mode": "equity_percent", "equityPercent": 10}).model_dump()},
    }
    evaluation = {"signal": "buy", "evaluatedBarTime": "1767225600000000000", "dataStatus": "simulated",
                  "rules": {"entryLong": True, "entryShort": False, "exitLong": False, "exitShort": False},
                  "engine": {}}
    market = {"symbol": SYMBOL, "price": 823.5, "priceTick": 0.5, "volumeMultiple": 1000.0,
              "ageSeconds": 0, "dataStatus": "simulated"}
    account = {"equity": 1_026_580, "todayNetPnl": 0, "ageSeconds": 0, "dataStatus": "simulated",
               "isCached": False}
    activity = {"ageSeconds": 0, "dataStatus": "simulated", "isCached": False, "ordersInitialized": True}
    from trader_runtime.risk import InstrumentSpecProvider, RiskPolicyProvider
    opened = build_strategy_intent(
        strategy=strategy, evaluation=evaluation, market=market, account=account,
        positions=[], activity=activity, user_id=USER,
        policies=RiskPolicyProvider(), instruments=InstrumentSpecProvider(),
        funds=compute_budget(equity=1_000_000, equity_percent=10, margin_per_lot=20_000,
                             used_lots=0, pending_lots=0, available=1_000_000, fee_per_lot=0,
                             max_lots=None))
    assert opened["positionIntent"]["action"] == "open_long"
    assert opened["positionIntent"]["requestedVolume"] == 5   # 预算说 5 手，maxPosition=1 不再卡
    blocked = build_strategy_intent(
        strategy=strategy, evaluation=evaluation, market=market, account=account,
        positions=[], activity=activity, user_id=USER,
        policies=RiskPolicyProvider(), instruments=InstrumentSpecProvider(),
        funds=compute_budget(equity=100_000, equity_percent=10, margin_per_lot=20_000,
                             used_lots=0, pending_lots=0, available=1_000_000, fee_per_lot=0,
                             max_lots=None))
    assert blocked["positionIntent"]["action"] == "hold"
    assert "不足 1 手" in str(blocked["riskAdjustedIntent"]["reason"])


# -- live 预算与净值归属 ------------------------------------------------------------------------------


class Provider(NormalProvider):
    def __init__(self) -> None:
        super().__init__()
        self.day = "2026-08-25"
        self.activity: dict[str, Any] = {"dataStatus": "normal", "isCached": False, "ageSeconds": 0,
                                         "ordersInitialized": True, "orders": [], "trades": []}
        self.position_items: list[dict[str, Any]] = []

    def account_activity(self) -> dict[str, Any]:
        return self.activity

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        return {**super().account_summary(turn_id), "tradingDay": self.day}


def test_live_budget_off_fresh_facts_and_refused_when_stale(tmp_path):
    store = RuntimeStore(tmp_path / "funds.sqlite3")
    store.ensure_default_trading_account(execution_owner_user_id=USER, provider_kind="fake",
                                         display_name="假数据模拟账户", sidecar_url=None)
    provider = Provider()
    # 假行情：权益 1 026 580、每手保证金 82 350、每手手续费 24.71 → 10% 只够 1 手。
    funds = live_budget(store, provider, user_id=USER, strategy_id="strategy-new",
                        symbol=SYMBOL, account=None, market=None, equity_percent=10)
    assert funds["lots"] == 1 and funds["feeSource"] == "estimated"
    assert funds["marginPerLot"] == 82_350 and funds["equity"] == 1_026_580
    provider.age_seconds = 60
    stale = live_budget(store, provider, user_id=USER, strategy_id="strategy-new",
                        symbol=SYMBOL, account=None, market=None, equity_percent=10)
    assert stale["lots"] == 0 and "不新鲜" in stale["unavailableReason"]
    store.close()


def _armed_row(store, strategy_id: str, *, direction: str = "long", volume: int = 2,
               entry: str = "822", account_id: str | None = None):
    # the authorization / session rows are not the point here (monitor tests' own helper does the same)
    store.connection.execute("PRAGMA foreign_keys = OFF")
    with store.lock, store.connection:
        store.connection.execute(
            """INSERT INTO ai_stop_orders (id, user_id, authorization_id, session_id, symbol,
               position_direction, position_bucket, volume, remaining_volume, trigger_price,
               price_intent, status, strategy_metadata_json, created_at, updated_at,
               trading_account_id)
               VALUES (?, ?, 'auth-funds', 'session-funds', ?, ?, 'today', ?, ?, 820,
               'aggressive', 'ARMED', ?, datetime('now'), datetime('now'), ?)""",
            (f"stop-{strategy_id}-{direction}", USER, SYMBOL, direction, volume, volume,
             json.dumps({"strategyId": strategy_id, "entryPrice": entry, "contractMultiplier": 1000}),
             account_id or store.default_trading_account_id))


def test_round_facts_count_only_this_strategy_and_mark_unattributed(tmp_path):
    store = RuntimeStore(tmp_path / "round.sqlite3")
    store.ensure_default_trading_account(execution_owner_user_id=USER, provider_kind="fake",
                                         display_name="假数据模拟账户", sidecar_url=None)
    account_id = store.default_trading_account_id
    mine, other = "strategy-mine", "strategy-other"

    def event(event_id, order_id, ns_time, payload):
        store.connection.execute(
            """INSERT INTO strategy_fill_events (user_id, event_id, trade_time, trading_day,
               client_order_id, payload_json, trading_account_id) VALUES (?, ?, ?, '2026-08-25', ?, ?, ?)""",
            (USER, event_id, ns_time, order_id, json.dumps(payload), account_id))
        store.connection.commit()

    # 本策略的开仓成交（commission 实际值）→ 记实际费用；归属平仓 → 记已实现盈亏。
    event("e1", "order-mine-open", 100, {"trade": {"symbol": SYMBOL, "offset": "OPEN", "volume": 2,
                                                   "price": 822, "commission": 50},
                                         "strategyEntry": mine})
    event("e2", "order-mine-close", 200, {"trade": {"symbol": SYMBOL, "offset": "CLOSETODAY",
                                                    "volume": 2, "price": 812, "commission": 60},
                                          "realizedGrossPnl": {mine: "-20000"}})
    # 他人策略的开仓成交：不进本策略的费用与盈亏。
    event("e3", "order-other-open", 300, {"trade": {"symbol": SYMBOL, "offset": "OPEN", "volume": 5,
                                                    "price": 822},
                                          "strategyEntry": other})
    _armed_row(store, mine, volume=2)
    _armed_row(store, other, volume=5)
    facts = strategy_round_facts(store, user_id=USER, strategy_id=mine, symbol=SYMBOL,
                                 account_id=account_id, since_ns=0, latest_price=802,
                                 multiplier=1000)
    assert facts["realized"] == -20000 and facts["fees"] == 110
    assert facts["feeSource"] == "actual"          # 逐笔 commission 有则用
    assert facts["unrealized"] == (802 - 822) * 2 * 1000
    assert facts["heldLots"] == 2 and facts["unattributed"] is None

    # 归属不明的手动平仓消耗了本策略的持仓 → 如实 unknown，不当零损益。
    event("e4", "order-manual-close", 400, {"trade": {"symbol": SYMBOL, "offset": "CLOSE",
                                                      "volume": 1, "price": 801},
                                            "unattributedStrategyIds": [mine]})
    unknown = strategy_round_facts(store, user_id=USER, strategy_id=mine, symbol=SYMBOL,
                                   account_id=account_id, since_ns=0, latest_price=802,
                                   multiplier=1000)
    assert unknown["unattributed"] and "归属不明" in unknown["unattributed"]
    store.close()


def test_curve_and_percent_helpers():
    assert drawdown_percent(100, 92) == pytest.approx(8.0)
    assert drawdown_percent(0, 92) == 0
    curve = [{"time": 1_000_000_000_000, "equity": 100.0}]
    throttled = curve_append(curve, time_ns=1_000_000_000_000 + 30_000_000_000, value=99.0)
    assert len(throttled) == 1                       # 一分钟内不加点
    grown = curve_append(throttled, time_ns=1_000_000_000_000 + 61_000_000_000, value=99.0)
    assert len(grown) == 2
    assert len(sample_curve([{"time": i, "equity": 1} for i in range(1000)])) <= 300


# -- 回测：同口径预算与回撤，缺保证金拒绝 -------------------------------------------------------------


def _bars(closes: list[float]) -> list[dict[str, Any]]:
    start = 1_800_000_000_000_000_000
    bars = []
    for index, close in enumerate(closes):
        timestamp = start + index * 300 * 1_000_000_000
        bars.append({"datetime": timestamp, "open": closes[index - 1] if index else close,
                     "high": max(closes[index - 1] if index else close, close) + 1,
                     "low": min(closes[index - 1] if index else close, close) - 1,
                     "close": close, "volume": 100, "tradingDay": "2026-09-23"})
    return bars


NO_FEES = {"source": "account", "default": {"mode": "per_lot", "open": 0, "closeToday": 0,
                                            "closeYesterday": 0}, "overrides": []}


def test_backtest_sizes_off_the_same_budget_ignoring_maxposition_default():
    p = _plan(maxPosition=1, sizing={"mode": "equity_percent", "equityPercent": 10})
    result = run_backtest(BacktestSetup(
        plan=p, symbol=SYMBOL, series={300: _bars([100, 100, 101, 101, 101, 101])},
        price_tick=1, volume_multiple=10, fee_schedule=NO_FEES, initial_balance=100_000,
        margin_per_lot=1_000))
    trade = result["trades"][0]
    assert trade["volume"] == 10           # 10 万 × 10% ÷ 1 000 = 10 手；maxPosition=1 不卡新模式
    assert any("按当前每手保证金 1000" in note for note in result["notes"])


def test_backtest_drawdown_triggers_and_blocks_later_opens():
    # 止损放宽（500 跳够不着），让回撤先于止损触发。
    p = _plan(maxPosition=1, stopLossTicks=500, sizing={"mode": "equity_percent", "equityPercent": 10},
              maxDrawdownPercent=0.5)
    result = run_backtest(BacktestSetup(
        plan=p, symbol=SYMBOL, series={300: _bars([100, 100, 101, 60, 60, 60, 60, 60, 60])},
        price_tick=1, volume_multiple=10, fee_schedule=NO_FEES, initial_balance=100_000,
        margin_per_lot=1_000))
    closes = [trade["exitReason"] for trade in result["trades"]]
    assert closes == ["drawdown_stop"]
    assert result["trades"][0]["exitLabel"] == "回撤停机"
    stop = result["drawdownStop"]
    assert stop["limitPercent"] == 0.5 and stop["blockedOpens"] >= 1
    assert result["trades"][0]["volume"] == 10


def test_backtest_refuses_equity_percent_without_margin():
    class NoMargin:
        @staticmethod
        def _history(symbol: str, seconds: int, count: int) -> dict[str, Any]:
            return {"bars": _bars([100] * 6), "priceTick": 1, "volumeMultiple": 10}

        def market_history(self, symbol: str, seconds: int, count: int) -> dict[str, Any]:
            return self._history(symbol, seconds, count)

        def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
            return {"priceTick": 1, "volumeMultiple": 10}   # 没有每手保证金

    p = _plan(sizing={"mode": "equity_percent", "equityPercent": 10})
    with pytest.raises(StrategyRuleError) as error:
        execute(NoMargin(), plan=p, symbol=SYMBOL, fee_schedule=NO_FEES, trading_days=None,
                slippage_ticks=0, initial_balance=None)
    assert error.value.code == "backtest_margin_unavailable"
    assert "按资金比例定手数需要每手保证金" in str(error.value)


# -- 实盘链路：发送前复核与回撤围栏 ---------------------------------------------------------------------


def _save_with_plan(store, plan: StrategyPlan, *, status: str = "monitoring") -> dict[str, Any]:
    store.ensure_default_trading_account(execution_owner_user_id=USER, provider_kind="fake",
                                         display_name="假数据模拟账户", sidecar_url=None)
    saved = store.save_strategy_draft(
        strategy_id="strategy-funds-live", user_id=USER, source_session_id="session-funds",
        name="资金比例实盘", symbol=SYMBOL, version=1, status=status,
        definition=definition_for(plan), definition_digest=DIGEST)
    # 上线 needs the current version's passed validation (monitor/start 的门)，种子一份。
    store.save_strategy_validation_report(
        report_id="validation-funds", strategy_id="strategy-funds-live", user_id=USER,
        strategy_digest=DIGEST, status="passed", checks=[], evidence={})
    return saved

def _seed_pending_attempt(store, attempt_id: str, *, bar_time: str,
                          order_id: str | None = None, leg: bool = True) -> None:
    """手动种一笔本策略的未终态执行尝试（意图快照 → RESERVED → SUBMITTED；默认带执行腿，
    leg=False 模拟确认卡——委托号只在 attempts.client_order_id 上，没有腿）。"""
    store.save_strategy_intent_snapshot(
        intent_id=f"intent-{attempt_id}", strategy_id=LIVE_ID, user_id=USER,
        strategy_digest=DIGEST, signal_bar_time=bar_time,
        context_digest=f"ctx-{attempt_id}", payload={})
    assert store.reserve_strategy_execution_attempt(
        attempt_id=attempt_id, strategy_id=LIVE_ID, user_id=USER,
        intent_id=f"intent-{attempt_id}", signal_bar_time=bar_time,
        trading_day="2026-08-25", action="open_long", volume=1)
    with store.lock, store.connection:
        if order_id:
            store.connection.execute(
                """UPDATE strategy_execution_attempts SET status = 'SUBMITTED',
                   client_order_id = ? WHERE id = ?""", (order_id, attempt_id))
            if leg:
                store.connection.execute(
                    """INSERT INTO strategy_execution_legs (id, attempt_id, leg_index, position_bucket,
                       volume, client_order_id, status, created_at, updated_at)
                       VALUES (?, ?, 0, 'today', 1, ?, 'SUBMITTED', datetime('now'), datetime('now'))""",
                    (f"leg-{attempt_id}", attempt_id, order_id))


def test_send_time_recheck_refuses_an_over_budget_open(tmp_path):
    app = application(tmp_path / "send-time.sqlite3", Gateway(), Provider())
    store = app.state.store
    _save_with_plan(store, _plan(sizing={"mode": "equity_percent", "equityPercent": 10}))
    authorization = {"authorizationId": "auth-funds", "sessionId": "session-funds"}
    # 假行情 10% 只够 1 手（权益 1 026 580、每手 82 350、手续费 24.71、可用 835 570）。
    within = app.state.autonomy._strategy_protection(
        user_id=USER, session_id="session-funds", strategy_id="strategy-funds-live",
        symbol=SYMBOL, side="buy_open", volume=1, position_bucket="today",
        authorization=authorization, strategy_bar_time=None)
    assert within is not None                       # 预算内照常带保护
    with pytest.raises(AutonomyError) as error:
        app.state.autonomy._strategy_protection(
            user_id=USER, session_id="session-funds", strategy_id="strategy-funds-live",
            symbol=SYMBOL, side="buy_open", volume=2, position_bucket="today",
            authorization=authorization, strategy_bar_time=None)
    assert error.value.code == "strategy_funds_budget_exceeded"
    assert "资金比例预算" in str(error.value.message)
    store.close()


def test_drawdown_state_fences_new_opens_until_reset(tmp_path):
    app = application(tmp_path / "fence.sqlite3", Gateway(), Provider())
    store = app.state.store
    _save_with_plan(store, _plan(maxDrawdownPercent=5))
    authorization = {"authorizationId": "auth-funds", "sessionId": "session-funds"}
    # 无状态时不挡；触发后（digest 匹配）持久挡新开仓，重置后恢复。
    assert app.state.autonomy._strategy_protection(
        user_id=USER, session_id="session-funds", strategy_id="strategy-funds-live",
        symbol=SYMBOL, side="buy_open", volume=1, position_bucket="today",
        authorization=authorization, strategy_bar_time=None) is not None
    store.save_strategy_drawdown_state(
        strategy_id="strategy-funds-live", user_id=USER, status="stopped", limit_percent=5.0,
        initial_equity="1000000", peak_equity="1000000", current_equity="940000",
        strategy_digest=DIGEST, round_started_at="2026-10-09T00:00:00+00:00",
        reason="回撤停机已触发。")
    with pytest.raises(AutonomyError) as error:
        app.state.autonomy._strategy_protection(
            user_id=USER, session_id="session-funds", strategy_id="strategy-funds-live",
            symbol=SYMBOL, side="buy_open", volume=1, position_bucket="today",
            authorization=authorization, strategy_bar_time=None)
    assert error.value.code == "strategy_drawdown_locked"
    store.reset_strategy_drawdown_state(strategy_id="strategy-funds-live", user_id=USER)
    assert store.strategy_drawdown_state(strategy_id="strategy-funds-live", user_id=USER) is None
    store.close()


# -- 完整回撤轮：模拟网关上的触发、平仓、停止与恢复 ----------------------------------------------------

from datetime import UTC, datetime  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402

from test_strategy_monitor import ProposalStub  # noqa: E402

LIVE_ID = "strategy-funds-live"


def _intent(store, action: str = "open_long", *, volume: int = 1,
            intent_id: str = "intent-funds", bar_time: str = "1767225600000000000") -> dict[str, Any]:
    payload = {
        "intentId": intent_id, "strategyId": LIVE_ID,
        "signalSnapshot": {"signal": "buy", "barTime": bar_time},
        "positionIntent": {"action": action, "requestedVolume": volume, "reason": "条件已满足"},
        "riskAdjustedIntent": {"status": "allowed", "action": action, "volume": volume,
                               "reason": "条件已满足"},
    }
    store.save_strategy_intent_snapshot(
        intent_id=intent_id, strategy_id=LIVE_ID, user_id=USER, strategy_digest=DIGEST,
        signal_bar_time=bar_time, context_digest=f"ctx-{intent_id}", payload=payload)
    return payload


def _now_ns() -> int:
    return int(datetime.now(UTC).timestamp() * 1_000_000_000)


async def _round_setup(tmp_path, plan: StrategyPlan, *, volume: int = 1):
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / "round.sqlite3", gateway, provider)
    _save_with_plan(app.state.store, plan)
    intent = _intent(app.state.store, volume=volume)
    monitor = StrategyMonitorService(store=app.state.store, proposals=ProposalStub(app.state.store, intent),
                                     autonomy=app.state.autonomy)
    return app, provider, gateway, monitor


async def _fill_order(app, provider, gateway, request, *, price: float, trade_id: str,
                       volume: int = 1, commission: float | None = 24.71) -> None:
    trade = {
        "tradeId": trade_id, "clientOrderId": request["clientOrderId"], "symbol": request["symbol"],
        "direction": request["direction"], "offset": request["offset"], "volume": volume,
        "price": price, "tradeDateTime": _now_ns(), "tradingDay": provider.day}
    if commission is not None:
        trade["commission"] = commission          # None = 无逐笔佣金（费用口径测试）
    provider.activity["trades"].append(trade)
    provider.activity["orders"] = [
        *(order for order in provider.activity["orders"] if order.get("clientOrderId") != request["clientOrderId"]),
        {**request, "status": "FINISHED", "volumeLeft": 0}]
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(str(request["clientOrderId"]), user_id=USER)
    app.state.autonomy.sync_strategy_protection()


async def _entry_filled(app, provider, gateway, monitor, *, volume: int = 1) -> str:
    assert (await monitor.run_once())[0]["status"] == "submitted"
    request = dict(gateway.request)
    assert request["volume"] == volume
    provider.position_items = [{"symbol": request["symbol"], "direction": "long", "volume": volume,
                                "todayVolume": volume, "historicalVolume": 0, "frozenToday": 0,
                                "frozenHistorical": 0}]
    await _fill_order(app, provider, gateway, request, price=822.0, trade_id="entry-fill",
                      volume=volume)
    return request["clientOrderId"]


async def test_drawdown_round_triggers_closes_stops_and_resets(tmp_path):
    app, provider, gateway, monitor = await _round_setup(tmp_path, _plan(maxDrawdownPercent=5))
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor)
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "running"
    first_round = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["roundStartedAt"]

    provider.last = 770.0        # 822 → 770：1 手 × 1000 ≈ −52 000，权益回撤 ≈5.1% ≥ 5%
    breach = await monitor.run_once()
    assert breach[0]["status"] == "blocked" and "平仓处理中" in breach[0]["detail"]
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "liquidating" and state["triggeredAt"] is not None
    assert gateway.submit_count == 2                      # 保护性平仓已发出
    close_request = dict(gateway.request)
    assert close_request["offset"] == "CLOSETODAY"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})
        # 平仓未确认完成前，重置回撤被拒绝。
        refused = await client.post(f"/api/strategies/{LIVE_ID}/monitor/start",
                                    json={"resetDrawdown": True})
        assert refused.status_code == 409 and "strategy_drawdown_liquidating" in refused.text

    provider.position_items = []
    await _fill_order(app, provider, gateway, close_request, price=770.0, trade_id="close-fill")
    done = await monitor.run_once()
    assert done[0]["status"] == "stopped" and "重置回撤并恢复" in done[0]["detail"]
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "stopped"
    # 终态补最后一个真实净值点 (2026-10-10 审查)：图尾与最终数值一致，不被一分钟节流吞掉。
    assert float(state["curve"][-1]["equity"]) == float(state["currentEquity"])
    assert [point["time"] for point in state["curve"]] == sorted(
        point["time"] for point in state["curve"])
    assert store.get_strategy_draft(LIVE_ID, user_id=USER)["status"] == "paused"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})
        payload = (await client.get(f"/api/strategies/{LIVE_ID}/monitor")).json()
        assert payload["drawdown"]["status"] == "stopped"
        assert payload["drawdown"]["canResume"] is True
        assert payload["drawdown"]["drawdownPercent"] >= 5
        assert payload["funds"] is None                    # 没按资金比例定手数：不报预算

        # 普通启动不重置 (2026-10-09 审查修正)：停机中的一轮保持停机——不开新仓、不再评估，
        # 也不再“重新触发一次停机”；只有显式 resetDrawdown 才开新一轮。
        plain = await client.post(f"/api/strategies/{LIVE_ID}/monitor/start", json={})
        assert plain.status_code == 200
    again = await monitor.run_once()
    assert again[0]["status"] == "blocked" and again[0]["action"] == "hold"
    assert "回撤停机" in again[0]["detail"]
    same_round = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert same_round["roundStartedAt"] == first_round and same_round["status"] == "stopped"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})
        reset = await client.post(f"/api/strategies/{LIVE_ID}/monitor/start",
                                  json={"resetDrawdown": True})
        assert reset.status_code == 200
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER) is None
    await monitor.run_once()
    fresh = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert fresh["status"] == "running"
    assert fresh["roundStartedAt"] != first_round           # 新一轮：新基准新高点
    assert float(fresh["peakEquity"]) == float(fresh["currentEquity"]) == 1_026_580
    store.close()


async def test_unattributed_manual_close_pauses_new_opens_as_unavailable(tmp_path):
    app, provider, gateway, monitor = await _round_setup(tmp_path, _plan(maxDrawdownPercent=5))
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor)
    entry = dict(gateway.request)

    # 手动平仓把这一策略的持仓消耗掉：归属不明 → 净值不可用，如实暂停新开仓。
    manual = {"clientOrderId": "manual-close-1", "symbol": entry["symbol"], "direction": "SELL",
              "offset": "CLOSETODAY", "volume": 1, "price": 810.0, "limitPrice": 810.0}
    provider.activity["trades"].append({
        "tradeId": "manual-fill", "clientOrderId": "manual-close-1", "symbol": entry["symbol"],
        "direction": "SELL", "offset": "CLOSETODAY", "volume": 1, "price": 810.0,
        "tradeDateTime": _now_ns(), "tradingDay": provider.day})
    provider.activity["orders"] = [*provider.activity["orders"], {**manual, "status": "FINISHED",
                                                                  "volumeLeft": 0}]
    provider.position_items = []
    app.state.autonomy.sync_strategy_protection()
    provider.last = 770.0
    await monitor.run_once()
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "unavailable"
    assert "归属不明" in str(state["reason"])
    # 开仓被挡（新意图给了新的 bar 也不会开）。
    _intent(store)
    store.reset_strategy_signal(strategy_id=LIVE_ID, user_id=USER)
    store.connection.execute("UPDATE strategy_monitor_states SET last_executed_bar = NULL")
    store.connection.commit()
    blocked = await monitor.run_once()
    assert blocked[0]["status"] == "blocked" and "归属不明" in blocked[0]["detail"]
    store.close()


async def test_emergency_stop_keeps_the_position_for_manual_handling(tmp_path):
    app, provider, gateway, monitor = await _round_setup(tmp_path, _plan(maxDrawdownPercent=5))
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor)
    provider.last = 770.0
    await app.state.autonomy.deactivate(user_id=USER, emergency=True)

    result = await monitor.run_once()
    assert result[0]["status"] == "blocked" and "平仓处理中" in result[0]["detail"]
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "liquidating"
    assert gateway.submit_count == 1                        # 急停挡住了保护性平仓
    assert monitor.held_position(store.get_strategy_draft(LIVE_ID, user_id=USER))["volume"] == 1
    assert store.get_strategy_draft(LIVE_ID, user_id=USER)["status"] == "monitoring"
    store.close()


# -- 修正轮 (2026-10-10 审查)：真实 ProposalService 链、占用预算细节与恢复围栏 ----------------------------


def test_pending_open_lots_excludes_only_the_attempt_being_sent(tmp_path):
    """本笔预留不算两遍，其他预留照算：发送中的那笔 attempt 免重复计入，别的 RESERVED 仍占用。"""
    store = RuntimeStore(tmp_path / "pending.sqlite3")
    _save_with_plan(store, _plan())
    for attempt_id in ("attempt-mine", "attempt-other"):
        _seed_pending_attempt(store, attempt_id, bar_time=f"176722560000000000{0 if attempt_id == 'attempt-mine' else 1}")
        with store.lock, store.connection:
            store.connection.execute("UPDATE strategy_execution_attempts SET volume = 2 WHERE id = ?",
                                     (attempt_id,))
    assert pending_open_lots(store, USER, LIVE_ID) == 4
    assert pending_open_lots(store, USER, LIVE_ID, exclude_attempt_id="attempt-mine") == 2
    assert pending_open_lots(store, USER, LIVE_ID, exclude_attempt_id="attempt-other") == 2
    store.close()


def test_positions_read_failure_leaves_the_budget_unavailable(tmp_path):
    """持仓读取失败不能当 0 占用：预算明确 unavailable，而不是释出可开手数。"""
    class BrokenPositions(Provider):
        def positions(self) -> list[dict[str, Any]]:
            raise RuntimeError("positions_unavailable")

    app = application(tmp_path / "broken.sqlite3", Gateway(), BrokenPositions())
    store = app.state.store
    _save_with_plan(store, _plan(sizing={"mode": "equity_percent", "equityPercent": 10}))
    budget = live_budget(store, app.state.data_provider, user_id=USER, strategy_id=LIVE_ID,
                         symbol=SYMBOL, equity_percent=10, max_lots=None)
    assert budget["lots"] == 0
    assert "持仓读取失败" in budget["unavailableReason"]
    store.close()


@pytest.mark.asyncio
async def test_real_proposal_service_chain_sizes_and_sends_within_budget(tmp_path):
    """真实入口回归 (审查实证阻塞)：monitor.run_once 走真实 ProposalService（此前
    `_engine_inputs` 抛 TypeError）与本笔 RESERVED 不重复计入预算（此前 budget_exceeded）：
    自动单照常发出；第二次同根 K 线不重复下单。"""
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / "real-chain.sqlite3", gateway, provider)
    _save_with_plan(app.state.store, _plan(sizing={"mode": "equity_percent", "equityPercent": 10}))
    monitor = StrategyMonitorService(store=app.state.store, proposals=app.state.proposals,
                                     autonomy=app.state.autonomy)
    first = await monitor.run_once()
    assert first[0]["status"] == "submitted", first
    assert gateway.submit_count == 1
    request = dict(gateway.request)
    assert request["volume"] == 1          # 10% 权益（≈102 658）只够 1 手，本笔预留不挤占自己
    attempts = app.state.store.list_strategy_execution_attempts(
        strategy_id=LIVE_ID, user_id=USER)
    assert len(attempts) == 1 and attempts[0]["status"] == "SUBMITTED"
    second = await monitor.run_once()      # 同一根 K 线：不重复下单
    assert second[0]["status"] in {"blocked", "duplicate"}
    assert gateway.submit_count == 1
    app.state.store.close()


def test_round_fees_split_close_today_and_yesterday_by_offset(tmp_path):
    """无逐笔佣金的平仓按真实 offset 估费：平今用 closeToday、平昨用 closeYesterday，
    不再把平昨当平今。"""
    store = RuntimeStore(tmp_path / "fees.sqlite3")
    store.ensure_default_trading_account(execution_owner_user_id=USER, provider_kind="fake",
                                         display_name="假数据模拟账户", sidecar_url=None)
    account_id = store.default_trading_account_id

    def event(event_id, order_id, offset, price):
        store.connection.execute(
            """INSERT INTO strategy_fill_events (user_id, event_id, trade_time, trading_day,
               client_order_id, payload_json, trading_account_id) VALUES (?, ?, ?, '2026-08-25', ?, ?, ?)""",
            (USER, event_id, 100, order_id, json.dumps({
                "trade": {"symbol": SYMBOL, "offset": offset, "volume": 1, "price": price},
                "strategyEntry": LIVE_ID}), account_id))
        store.connection.commit()

    event("open-1", "order-open", "OPEN", 822)
    event("close-today", "order-close-today", "CLOSETODAY", 812)
    event("close-yesterday", "order-close-yesterday", "CLOSE", 802)
    _armed_row(store, LIVE_ID, volume=3)
    class _Fees:
        root = tmp_path

        def get(self, user_id: str) -> dict[str, Any]:
            return {"source": "custom", "default": {"mode": "per_lot", "open": 1,
                                                    "closeToday": 2, "closeYesterday": 4},
                    "overrides": []}

    facts = strategy_round_facts(store, user_id=USER, strategy_id=LIVE_ID, symbol=SYMBOL,
                                 account_id=account_id, since_ns=0, latest_price=802,
                                 multiplier=1000, fee_settings=_Fees())
    assert facts["fees"] == 1 + 2 + 4      # 开 1 + 平今 2 + 平昨 4（此前平昨也按 2 算）
    assert facts["feeSource"] == "estimated"
    store.close()


@pytest.mark.asyncio
async def test_drawdown_recovery_checks_flat_and_terminal_orders(tmp_path):
    """resetDrawdown 的前提与 canResume 同源：仍有持仓 / 仍有未终态委托单 → 409 且围栏保留；
    没有停机状态 → 409 not_stopped。"""
    app, provider, gateway, monitor = await _round_setup(tmp_path, _plan(maxDrawdownPercent=5))
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor)
    provider.last = 770.0
    await monitor.run_once()                       # 触发 → liquidating
    close_request = dict(gateway.request)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})
        # running 状态下 resetDrawdown：没有停机状态可重置。
        provider.position_items = []
        await _fill_order(app, provider, gateway, close_request, price=770.0, trade_id="close-fill")
        await monitor.run_once()                   # → stopped
        store.save_strategy_drawdown_state(
            strategy_id=LIVE_ID, user_id=USER, status="stopped", limit_percent=5.0,
            initial_equity="1026580", peak_equity="1026580", current_equity="974530.58",
            strategy_digest=DIGEST, round_started_at="2026-10-09T00:00:00+00:00",
            reason="回撤停机已触发。")

        # 恢复前提失败之一：本策略又有一手持仓（手动开回来的）。
        provider.position_items = [{"symbol": SYMBOL, "direction": "long", "volume": 1,
                                    "todayVolume": 1, "historicalVolume": 0,
                                    "frozenToday": 0, "frozenHistorical": 0}]
        _armed_row(store, LIVE_ID, volume=1)
        payload = (await client.get(f"/api/strategies/{LIVE_ID}/monitor")).json()
        assert payload["drawdown"]["canResume"] is False
        assert "持仓" in payload["drawdown"]["resumeBlockedReason"]
        refused = await client.post(f"/api/strategies/{LIVE_ID}/monitor/start",
                                    json={"resetDrawdown": True})
        assert refused.status_code == 409 and "strategy_drawdown_recovery_precondition" in refused.text
        assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "stopped"

        # 恢复前提失败之二：仍有未到终态的本策略委托单（开仓单挂着没撤干净）。
        provider.position_items = []
        store.connection.execute("DELETE FROM ai_stop_orders WHERE id = ?", (f"stop-{LIVE_ID}-long",))
        store.connection.commit()
        _seed_pending_attempt(store, "attempt-pending", bar_time="1767225601000000000",
                              order_id="order-still-alive")
        provider.activity["orders"] = [{**dict(gateway.request), "clientOrderId": "order-still-alive",
                                        "symbol": SYMBOL, "offset": "OPEN", "status": "ALIVE",
                                        "volumeLeft": 1}]
        refused_pending = await client.post(f"/api/strategies/{LIVE_ID}/monitor/start",
                                            json={"resetDrawdown": True})
        assert refused_pending.status_code == 409
        assert "strategy_drawdown_recovery_precondition" in refused_pending.text
        assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "stopped"
    store.close()


@pytest.mark.asyncio
async def test_drawdown_cancel_failure_keeps_the_round_fenced(tmp_path):
    """开仓挂单撤销失败：这一轮保持 liquidating/围栏，不前进到 stopped。"""
    app, provider, gateway, monitor = await _round_setup(tmp_path, _plan(maxDrawdownPercent=5))
    store = app.state.store
    assert (await monitor.run_once())[0]["status"] == "submitted"
    entry_request = dict(gateway.request)
    # 这一策略还有一张未成交的开仓挂单：另一根 K 线的意图真实提交，停在网关上。
    intent2 = _intent(store, intent_id="intent-funds-2", bar_time="1767225601000000000")
    second = StrategyMonitorService(store=store, proposals=ProposalStub(store, intent2),
                                    autonomy=app.state.autonomy)
    assert (await second.run_once())[0]["status"] == "submitted"
    provider.position_items = [{"symbol": SYMBOL, "direction": "long", "volume": 1,
                                "todayVolume": 1, "historicalVolume": 0,
                                "frozenToday": 0, "frozenHistorical": 0}]
    await _fill_order(app, provider, gateway, entry_request, price=822.0, trade_id="entry-fill")
    gateway.cancel_error = True
    provider.last = 770.0
    breach = await monitor.run_once()
    assert breach[0]["status"] == "blocked" and "平仓处理中" in breach[0]["detail"]
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "liquidating"
    assert gateway.cancel_count >= 1             # 撤单真实尝试过且失败
    again = await monitor.run_once()             # 第二轮：仍围栏、无重复触发副作用
    assert again[0]["status"] == "blocked"
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "liquidating"
    store.close()


@pytest.mark.asyncio
async def test_drawdown_partial_close_stays_fenced_until_flat(tmp_path):
    """部分平仓：持仓没平完，这一轮保持 blocked/liquidating，净值不写终态。"""
    app, provider, gateway, monitor = await _round_setup(
        tmp_path, _plan(sizing=None, maxPosition=2, maxDrawdownPercent=5), volume=2)
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor, volume=2)
    provider.last = 770.0
    breach = await monitor.run_once()
    assert breach[0]["status"] == "blocked" and "平仓处理中" in breach[0]["detail"]
    close_request = dict(gateway.request)
    assert close_request["volume"] == 2
    # 平仓只成交 1 手。
    provider.activity["trades"].append({
        "tradeId": "close-partial", "clientOrderId": close_request["clientOrderId"],
        "symbol": SYMBOL, "direction": "SELL", "offset": "CLOSETODAY", "volume": 1,
        "price": 770.0, "tradeDateTime": _now_ns(), "tradingDay": provider.day, "commission": 24.71})
    provider.activity["orders"] = [
        *(order for order in provider.activity["orders"]
          if order.get("clientOrderId") != close_request["clientOrderId"]),
        {**close_request, "status": "ALIVE", "volumeLeft": 1}]
    gateway.query_report = gateway.report(close_request, status="ALIVE", volume_left=1)
    await app.state.simulation_orders.query(str(close_request["clientOrderId"]), user_id=USER)
    provider.position_items = [{"symbol": SYMBOL, "direction": "long", "volume": 1,
                                "todayVolume": 1, "historicalVolume": 0,
                                "frozenToday": 0, "frozenHistorical": 0}]
    still = await monitor.run_once()
    assert still[0]["status"] == "blocked"
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "liquidating"
    # 剩余 1 手也平完 → 终态净值按完成成交回读（initial + realized − fees − baseline）。
    provider.position_items = []
    await _fill_order(app, provider, gateway, close_request, price=770.0, trade_id="close-rest")
    done = await monitor.run_once()
    assert done[0]["status"] == "stopped"
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "stopped" and state["feeSource"] == "actual"
    assert float(state["currentEquity"]) == pytest.approx(
        float(state["initialEquity"]) + (770.0 - 822.0) * 2 * 1000 - 24.71 * 3
        - float(state["baselinePnl"] or 0), abs=0.01)
    store.close()


def test_pyramid_add_shares_the_live_budget():
    """加仓与首次开仓共用同一份预算：funds 不足 1 手或数据缺失就不加，maxLots 仍约束总量。"""
    from trader_runtime.strategy_engine import MarketContext, PositionState, _pyramid_add
    plan = _plan(sizing={"mode": "equity_percent", "equityPercent": 10},
                 pyramiding={"trigger": "profit_ticks", "step": 1, "maxAdds": 3, "volume": 5})
    state = PositionState(direction="long", volume=2, initial_volume=2, last_entry_price=820.0)
    engine = {"close": 830}
    market = MarketContext(price_tick=0.5, volume_multiple=1000.0,
                           funds={"lots": 1, "unavailableReason": None})
    added = _pyramid_add(plan, state, "long", engine, market, {})
    assert added is not None and added[0] == 1 and "加仓" in added[1]
    market = MarketContext(price_tick=0.5, volume_multiple=1000.0, funds={"lots": 0})
    assert _pyramid_add(plan, state, "long", engine, market, {}) is None
    market = MarketContext(price_tick=0.5, volume_multiple=1000.0, funds=None)
    assert _pyramid_add(plan, state, "long", engine, market, {}) is None


# -- 修正轮 (2026-10-10 审查)：funds-preview 的账户归属（真实 HTTP，本站模拟账户） --------------------


@pytest.mark.asyncio
async def test_funds_preview_estimates_per_the_selected_account(tmp_path, monkeypatch):
    """funds-preview 走 ctx.account_context：同一用户在默认账户与本站模拟账户得到各自的
    预估，绝不默认落到全局 owner 账户；未授权/未知账户直接拒绝。"""
    from test_paper_accounts import ORIGIN, PASSWORD, close_paper_app, paper_app

    application, engine = paper_app(tmp_path, monkeypatch, "funds-preview.sqlite3")
    try:
        # 默认账户在这个夹具里没有真实 sidecar 轮询：按类设计把账户/行情读数接到假数据源
        # （市场共享是 RecordingMarketSource 的本意），纸面账户等共享 poller 出第一拍快照。
        import asyncio

        from trader_runtime.data_providers import FakeReadOnlyDataProvider

        fake_market = FakeReadOnlyDataProvider()
        default_provider = application.state.accounts.default.provider
        monkeypatch.setattr(default_provider, "account_summary", fake_market.account_summary)
        monkeypatch.setattr(default_provider, "positions", fake_market.positions)
        monkeypatch.setattr(default_provider, "market_snapshot", fake_market.market_snapshot)
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test",
                               headers={"Origin": ORIGIN}) as client:
            assert (await client.post("/api/auth/owner",
                                      json={"username": "charles", "password": PASSWORD})).status_code == 200
            paper_id = (await client.post("/api/accounts/open-paper")).json()["accountId"]
            # 主人选定主账号为默认（#1205：不选时先用自己的本站模拟）。
            owner_id = str((await client.get("/api/auth/owner/status")).json()["userId"])
            application.state.store.set_user_default_account(
                user_id=owner_id, account_id=application.state.accounts.default.account_id)
            # 先发一次带账户头的请求让运行时装配该账户，再接到共享行情源并等第一拍快照。
            await client.get("/api/strategies/funds-preview",
                             params={"symbol": SYMBOL, "equityPercent": 100},
                             headers={"X-AI-Trader-Account": paper_id})
            paper_provider = application.state.accounts.get(paper_id).provider
            monkeypatch.setattr(paper_provider, "market_snapshot",
                                paper_provider.market_source.market_snapshot)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2.0
            while paper_provider.runtime_status().get("accountStatus") != "normal":
                assert loop.time() < deadline, paper_provider.runtime_status()
                await asyncio.sleep(0.02)
            # 默认（假数据）账户：权益 1 026 580、可用 835 570 → 100% 预算 10 手。
            default_response = await client.get(
                "/api/strategies/funds-preview",
                params={"symbol": SYMBOL, "equityPercent": 100})
            assert default_response.status_code == 200, default_response.text[:300]
            default_view = default_response.json()
            assert default_view["funds"]["equity"] == pytest.approx(1_026_580)
            assert default_view["funds"]["availableLots"] == 10
            assert default_view["funds"]["unavailableReason"] is None
            # 带账户头：切到本站模拟账户（权益 1 000 000、无持仓、可用足额）→ 12 手。
            paper_view = (await client.get(
                "/api/strategies/funds-preview",
                params={"symbol": SYMBOL, "equityPercent": 100},
                headers={"X-AI-Trader-Account": paper_id})).json()
            assert paper_view["funds"]["equity"] == pytest.approx(1_000_000)
            assert paper_view["funds"]["availableLots"] == 12
            assert paper_view["funds"]["unavailableReason"] is None
            # 未授权/未知账户：拒绝，不回落到默认账户。
            refused = await client.get(
                "/api/strategies/funds-preview",
                params={"symbol": SYMBOL, "equityPercent": 100},
                headers={"X-AI-Trader-Account": "paper:no-such-account"})
            assert refused.status_code in (403, 404)
    finally:
        await close_paper_app(application)


@pytest.mark.asyncio
async def test_funds_preview_separates_library_drafts_from_instances(tmp_path, monkeypatch):
    """终审修正 (2026-10-10)：strategyId 是执行实例（definition 带 libraryStrategyId）才绑定
    自身账户并扣自身占用；是库策略（可以在别的账户上线）就按当前请求账户估新实例，不被
    记录上的 tradingAccountId 覆盖。别人的 strategyId 404，未知 strategyId 404。"""
    from test_paper_accounts import ORIGIN, PASSWORD, close_paper_app, paper_app

    application, engine = paper_app(tmp_path, monkeypatch, "funds-preview-bound.sqlite3")
    try:
        import asyncio

        from trader_runtime.data_providers import FakeReadOnlyDataProvider

        fake_market = FakeReadOnlyDataProvider()
        default_provider = application.state.accounts.default.provider
        monkeypatch.setattr(default_provider, "account_summary", fake_market.account_summary)
        monkeypatch.setattr(default_provider, "positions", fake_market.positions)
        monkeypatch.setattr(default_provider, "market_snapshot", fake_market.market_snapshot)
        store = application.state.store
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test",
                               headers={"Origin": ORIGIN}) as client:
            assert (await client.post("/api/auth/owner",
                                      json={"username": "charles", "password": PASSWORD})).status_code == 200
            owner_id = str((await client.get("/api/auth/owner/status")).json()["userId"])
            paper_id = (await client.post("/api/accounts/open-paper")).json()["accountId"]
            # 主人选定主账号为默认（#1205：不选时先用自己的本站模拟）。
            store.set_user_default_account(user_id=owner_id, account_id=application.state.accounts.default.account_id)
            # 先装配纸面账户并接到共享行情源（与 test_funds_preview_estimates… 同一做法）。
            await client.get("/api/strategies/funds-preview",
                             params={"symbol": SYMBOL, "equityPercent": 100},
                             headers={"X-AI-Trader-Account": paper_id})
            paper_provider = application.state.accounts.get(paper_id).provider
            monkeypatch.setattr(paper_provider, "market_snapshot",
                                paper_provider.market_source.market_snapshot)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2.0
            while paper_provider.runtime_status().get("accountStatus") != "normal":
                assert loop.time() < deadline, paper_provider.runtime_status()
                await asyncio.sleep(0.02)

            instance_definition = {**definition_for(_plan()), "libraryStrategyId": "strategy-library-src"}
            store.save_strategy_draft(
                strategy_id="strategy-funds-instance", user_id=owner_id,
                source_session_id="session-funds", name="绑定模拟账户的实例", symbol=SYMBOL,
                version=1, status="monitoring", definition=instance_definition,
                definition_digest="digest-funds-instance", trading_account_id=paper_id)
            store.save_strategy_draft(
                strategy_id="strategy-funds-library", user_id=owner_id,
                source_session_id="session-funds", name="库策略本身", symbol=SYMBOL,
                version=1, status="draft", definition=definition_for(_plan()),
                definition_digest="digest-funds-library", trading_account_id=paper_id)

            # 执行实例：即使不带账户头也按它自己绑定的模拟账户算（1 000 000 / 12 手）。
            bound = (await client.get(
                "/api/strategies/funds-preview",
                params={"symbol": SYMBOL, "equityPercent": 100,
                        "strategyId": "strategy-funds-instance"})).json()
            assert bound["funds"]["equity"] == pytest.approx(1_000_000)
            assert bound["funds"]["availableLots"] == 12
            assert bound["accountId"] == paper_id

            # 库策略：记录上写着模拟账户，但编辑预览按当前请求账户（默认假数据账户）估新实例。
            library = (await client.get(
                "/api/strategies/funds-preview",
                params={"symbol": SYMBOL, "equityPercent": 100,
                        "strategyId": "strategy-funds-library"})).json()
            assert library["funds"]["equity"] == pytest.approx(1_026_580)
            assert library["funds"]["availableLots"] == 10
            assert library["accountId"] != paper_id

            # 未知 strategyId 与别人的 strategyId：404，不回落到无策略口径。
            missing = await client.get(
                "/api/strategies/funds-preview",
                params={"symbol": SYMBOL, "equityPercent": 100, "strategyId": "strategy-none"})
            assert missing.status_code == 404
            store.save_strategy_draft(
                strategy_id="strategy-funds-foreign", user_id="user-someone-else",
                source_session_id="session-funds", name="别人的策略", symbol=SYMBOL,
                version=1, status="draft", definition=definition_for(_plan()),
                definition_digest="digest-funds-foreign")
            foreign = await client.get(
                "/api/strategies/funds-preview",
                params={"symbol": SYMBOL, "equityPercent": 100, "strategyId": "strategy-funds-foreign"})
            assert foreign.status_code == 404
    finally:
        await close_paper_app(application)


# -- 终审修正轮 (2026-10-10)：可信持仓、完整未终态委托、费用今昨分配、正式链路与成员隔离 --------------------


@pytest.mark.asyncio
async def test_recovery_refuses_when_positions_read_fails(tmp_path):
    """持仓读取失败不能当空仓 (终审实证 1)：canResume=false、resetDrawdown 409，停机围栏保留；
    读取恢复后才能重置。"""
    app, provider, gateway, monitor = await _round_setup(tmp_path, _plan(maxDrawdownPercent=5))
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor)
    provider.last = 770.0
    await monitor.run_once()                      # 触发 → liquidating
    close_request = dict(gateway.request)
    provider.position_items = []
    await _fill_order(app, provider, gateway, close_request, price=770.0, trade_id="close-fill")
    assert (await monitor.run_once())[0]["status"] == "stopped"

    def broken() -> list[dict[str, Any]]:
        raise RuntimeError("positions_unavailable")

    provider.positions = broken
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})
        payload = (await client.get(f"/api/strategies/{LIVE_ID}/monitor")).json()
        assert payload["drawdown"]["canResume"] is False
        assert "持仓读取失败" in str(payload["drawdown"]["resumeBlockedReason"])
        refused = await client.post(f"/api/strategies/{LIVE_ID}/monitor/start",
                                    json={"resetDrawdown": True})
        assert refused.status_code == 409
        assert "strategy_drawdown_recovery_precondition" in refused.text
        assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "stopped"
    store.close()


@pytest.mark.asyncio
async def test_liquidating_waits_for_the_close_order_stream(tmp_path):
    """不同流到达顺序 (终审实证 2)：持仓流先报空仓、保护平仓单仍在网关上 ALIVE——不能误报
    stopped；平仓单真实成交回读后才进终态。"""
    app, provider, gateway, monitor = await _round_setup(tmp_path, _plan(maxDrawdownPercent=5))
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor)
    provider.last = 770.0
    breach = await monitor.run_once()
    assert breach[0]["status"] == "blocked" and "平仓处理中" in breach[0]["detail"]
    close_request = dict(gateway.request)
    # 持仓流先到空仓；订单/成交流仍只有开仓那笔，平仓单 ALIVE 挂在网关上。
    provider.position_items = []
    provider.activity["orders"] = [
        *(order for order in provider.activity["orders"]
          if order.get("clientOrderId") != close_request["clientOrderId"]),
        {**close_request, "status": "ALIVE", "volumeLeft": 1}]
    early = await monitor.run_once()
    assert early[0]["status"] == "blocked"
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "liquidating"          # 不是 stopped：平仓单还没到终态
    # 平仓单成交回读 → 才进终态。
    await _fill_order(app, provider, gateway, close_request, price=770.0, trade_id="close-fill")
    done = await monitor.run_once()
    assert done[0]["status"] == "stopped"
    store.close()


def test_pending_orders_see_card_attempts_without_legs(tmp_path):
    """确认卡的委托号只在 attempts.client_order_id 上（没有执行腿）：未终态核对也要看到它，
    未知单同样算“还有委托没到终态”。"""
    app = application(tmp_path / "card-only.sqlite3", Gateway(), Provider())
    store = app.state.store
    _save_with_plan(store, _plan(maxDrawdownPercent=5))
    _seed_pending_attempt(store, "attempt-card", bar_time="1767225601000000000",
                          order_id="order-card-confirmed", leg=False)
    monitor = StrategyMonitorService(store=store, proposals=app.state.proposals,
                                     autonomy=app.state.autonomy)
    pending = monitor._strategy_open_orders_pending(user_id=USER, strategy_id=LIVE_ID)
    assert [(item["clientOrderId"], item.get("unknown")) for item in pending] == \
        [("order-card-confirmed", True)]
    blocker = monitor.recovery_blockers(store.get_strategy_draft(LIVE_ID, user_id=USER))
    assert blocker and "委托单" in blocker
    store.close()


def test_close_fee_allocation_splits_today_and_yesterday_by_entry_day(tmp_path):
    """费用口径 (终审 3)：非 SHFE 的同一笔 CLOSE 按保护行真实分配——2 手开仓日=今日、
    1 手昨日，费用按各自费率估算；SHFE 的 CLOSETODAY 只吃今日桶；费率读取失败 → 净值
    费用不可用，不当 0。"""
    from decimal import Decimal

    from trader_runtime.strategy_protection import _close

    store = RuntimeStore(tmp_path / "split.sqlite3")
    store.ensure_default_trading_account(execution_owner_user_id=USER, provider_kind="fake",
                                         display_name="假数据模拟账户", sidecar_url=None)
    account_id = store.default_trading_account_id

    def armed(stop_id, symbol, volume, entry_day, close_order=None):
        store.connection.execute("PRAGMA foreign_keys = OFF")
        with store.lock, store.connection:
            store.connection.execute(
                """INSERT INTO ai_stop_orders (id, user_id, authorization_id, session_id, symbol,
                   position_direction, position_bucket, volume, remaining_volume, trigger_price,
                   price_intent, status, strategy_metadata_json, created_at, updated_at,
                   trading_account_id, close_client_order_id)
                   VALUES (?, ?, 'auth-funds', 'session-funds', ?, 'long', 'today', ?, ?, 820,
                   'aggressive', 'ARMED', ?, datetime('now'), datetime('now'), ?, ?)""",
                (stop_id, USER, symbol, volume, volume,
                 json.dumps({"strategyId": LIVE_ID, "entryPrice": "822",
                             "contractMultiplier": 1000, "entryTradingDay": entry_day,
                             "timeframeSeconds": 300}),
                 account_id, close_order))

    def close_trade(symbol, offset, volume, day):
        return {"clientOrderId": f"order-close-{symbol}-{offset}", "symbol": symbol,
                "direction": "SELL", "offset": offset, "volume": volume, "price": 812,
                "tradeDateTime": 200, "tradeId": f"t-{symbol}-{offset}"}

    # CFFEX：同一笔保护平仓单同时吃今日/昨日桶 → 2 手平今 + 1 手平昨（归属明确，可计费）。
    cffex_order = "order-close-CFFEX.if2612-CLOSE"
    armed("stop-if-today", "CFFEX.if2612", 2, "2026-08-25", close_order=cffex_order)
    armed("stop-if-yesterday", "CFFEX.if2612", 1, "2026-08-24", close_order=cffex_order)
    pnl, close_lots, unattributed = _close(
        store.connection, USER, account_id, close_trade("CFFEX.if2612", "CLOSE", 3, "2026-08-25"),
        "2026-08-25", Decimal("812"), 3)
    assert close_lots == {LIVE_ID: {"closeToday": 2, "closeYesterday": 1}}
    assert pnl and not unattributed
    # SHFE：CLOSETODAY 只吃今日桶（昨日那行碰都不碰）。
    shfe_order = "order-close-SHFE.au2612-CLOSETODAY"
    armed("stop-au-today", SYMBOL, 2, "2026-08-25", close_order=shfe_order)
    armed("stop-au-yesterday", SYMBOL, 1, "2026-08-24", close_order=shfe_order)
    _, shfe_lots, _ = _close(
        store.connection, USER, account_id, close_trade(SYMBOL, "CLOSETODAY", 2, "2026-08-25"),
        "2026-08-25", Decimal("812"), 2)
    assert shfe_lots == {LIVE_ID: {"closeToday": 2, "closeYesterday": 0}}

    def event(event_id, order_id, payload):
        store.connection.execute(
            """INSERT INTO strategy_fill_events (user_id, event_id, trade_time, trading_day,
               client_order_id, payload_json, trading_account_id) VALUES (?, ?, 100, '2026-08-25', ?, ?, ?)""",
            (USER, event_id, order_id, json.dumps(payload), account_id))
        store.connection.commit()

    class _Fees:
        root = tmp_path

        def get(self, user_id: str) -> dict[str, Any]:
            return {"source": "custom", "default": {"mode": "per_lot", "open": 1,
                                                    "closeToday": 2, "closeYesterday": 4},
                    "overrides": []}

    # 开 3 手 + 上面 CFFEX 平仓（2 平今 + 1 平昨）按真实分配估费：3×1 + 2×2 + 1×4 = 11。
    event("open-1", "order-open", {"trade": {"symbol": "CFFEX.if2612", "offset": "OPEN",
                                             "volume": 3, "price": 822},
                                   "strategyEntry": LIVE_ID})
    event("close-1", "order-close-CFFEX.if2612-CLOSE",
          {"trade": {"symbol": "CFFEX.if2612", "offset": "CLOSE", "volume": 3, "price": 812},
           "realizedGrossPnl": {LIVE_ID: "-30000"}, "strategyCloseLots": close_lots})
    facts = strategy_round_facts(store, user_id=USER, strategy_id=LIVE_ID,
                                 symbol="CFFEX.if2612", account_id=account_id, since_ns=0,
                                 latest_price=None, multiplier=1000, fee_settings=_Fees())
    assert facts["fees"] == 11 and facts["feeSource"] == "estimated"
    # 同样的成交没有佣金、也拿不到费率 → 费用不可用，不按 0 冒充扣过费。
    no_rate = strategy_round_facts(store, user_id=USER, strategy_id=LIVE_ID,
                                   symbol="CFFEX.if2612", account_id=account_id, since_ns=0,
                                   latest_price=None, multiplier=1000)
    assert no_rate["feeSource"] == "unavailable"
    store.close()


@pytest.mark.asyncio
async def test_formal_http_chain_saves_previews_starts_and_orders_within_budget(tmp_path):
    """验收缺口：全部走正式 HTTP——保存库策略 → 验证（生成执行实例与验证报告）→
    funds-preview → monitor/start → 真实 ProposalService 在预算内自动下单。"""
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / "http-chain.sqlite3", gateway, provider)
    store = app.state.store
    plan = _plan(sizing={"mode": "equity_percent", "equityPercent": 10})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})
        saved = await client.post("/api/strategy-plans", json={"plan": plan.model_dump()})
        assert saved.status_code == 201, saved.text[:300]
        validated = await client.post(
            f"/api/strategy-plans/{saved.json()['strategyId']}/validate",
            json={"symbol": SYMBOL, "chartTimeframeSeconds": 300})
        assert validated.status_code == 200, validated.text[:300]
        instance_id = validated.json()["strategyId"]
        assert store.get_strategy_draft(instance_id, user_id=USER)["definition"].get(
            "libraryStrategyId") == saved.json()["strategyId"]
        preview = (await client.get(
            "/api/strategies/funds-preview",
            params={"symbol": SYMBOL, "equityPercent": 10, "strategyId": instance_id})).json()
        assert preview["funds"]["availableLots"] == 1        # 10% 权益只够 1 手
        started = await client.post(f"/api/strategies/{instance_id}/monitor/start", json={})
        assert started.status_code == 200, started.text[:300]
    monitor = StrategyMonitorService(store=store, proposals=app.state.proposals,
                                     autonomy=app.state.autonomy)
    first = await monitor.run_once()
    assert first[0]["status"] == "submitted", first
    assert gateway.request["volume"] == 1                   # 预算内自动单
    store.close()


@pytest.mark.asyncio
async def test_funds_preview_member_never_reads_the_owner_account(tmp_path, monkeypatch):
    """验收缺口：普通用户不带 strategyId、不带账户头——预览按他自己的模拟账户算，
    绝不泄漏全局 owner 账户的资金。"""
    from test_paper_accounts import ORIGIN, PASSWORD, close_paper_app, paper_app, register_user

    application, engine = paper_app(tmp_path, monkeypatch, "funds-preview-member.sqlite3")
    try:
        import asyncio

        from trader_runtime.data_providers import FakeReadOnlyDataProvider

        fake_market = FakeReadOnlyDataProvider()
        default_provider = application.state.accounts.default.provider
        monkeypatch.setattr(default_provider, "account_summary", fake_market.account_summary)
        monkeypatch.setattr(default_provider, "positions", fake_market.positions)
        monkeypatch.setattr(default_provider, "market_snapshot", fake_market.market_snapshot)
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test",
                               headers={"Origin": ORIGIN}) as client:
            assert (await client.post("/api/auth/owner",
                                      json={"username": "charles", "password": PASSWORD})).status_code == 200
            friend, friend_id = await register_user(application)
            # 第一次请求装配他的模拟账户（此时可能还没有行情快照）。
            first = await friend.get("/api/strategies/funds-preview",
                                     params={"symbol": SYMBOL, "equityPercent": 100})
            assert first.status_code == 200, first.text[:300]
            account_id = first.json()["accountId"]
            paper_provider = application.state.accounts.get(account_id).provider
            monkeypatch.setattr(paper_provider, "market_snapshot",
                                paper_provider.market_source.market_snapshot)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2.0
            while paper_provider.runtime_status().get("accountStatus") != "normal":
                assert loop.time() < deadline, paper_provider.runtime_status()
                await asyncio.sleep(0.02)
            view = (await friend.get("/api/strategies/funds-preview",
                                     params={"symbol": SYMBOL, "equityPercent": 100})).json()
            # 他自己的模拟账户（权益 1 000 000），不是 owner 默认账户的 1 026 580。
            assert view["funds"]["equity"] == pytest.approx(1_000_000)
            assert view["funds"]["equity"] != pytest.approx(1_026_580)
            assert view["accountId"] == account_id
    finally:
        await close_paper_app(application)


# -- 修正轮三 (2026-10-10)：成交回报对齐、可信账户快照、费率恢复与真实确认卡链 --------------------------


@pytest.mark.asyncio
async def test_liquidating_waits_for_the_close_fill_attribution(tmp_path):
    """父代理 probe 复现 (FINISHED 先到、fills 后到)：网关已回读 FINISHED、持仓已空，但平仓
    成交还没进策略归属流（保护行未消耗）→ 仍是 liquidating，不落终态净值；回报对齐后才
    stopped，最终净值 = initial − 52 000 − 两笔佣金。"""
    app, provider, gateway, monitor = await _round_setup(tmp_path, _plan(maxDrawdownPercent=5))
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor)
    provider.last = 770.0
    breach = await monitor.run_once()
    assert breach[0]["status"] == "blocked" and "平仓处理中" in breach[0]["detail"]
    close_request = dict(gateway.request)
    # 平仓单网关侧已 FINISHED、持仓已空——但成交事件还没进归属流。
    provider.position_items = []
    provider.activity["orders"] = [
        *(order for order in provider.activity["orders"]
          if order.get("clientOrderId") != close_request["clientOrderId"]),
        {**close_request, "status": "FINISHED", "volumeLeft": 0}]
    gateway.query_report = gateway.report(close_request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(str(close_request["clientOrderId"]), user_id=USER)
    waiting = await monitor.run_once()
    assert waiting[0]["status"] == "blocked" and "成交回报未对齐" in waiting[0]["detail"]
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "liquidating"
    row = store.connection.execute(
        "SELECT remaining_volume FROM ai_stop_orders WHERE close_client_order_id = ?",
        (str(close_request["clientOrderId"]),)).fetchone()
    assert row is not None and int(row[0]) == 1        # 保护行还没被消耗：不能当已平完
    blocker = monitor.recovery_blockers(store.get_strategy_draft(LIVE_ID, user_id=USER))
    assert blocker and "成交回报未对齐" in blocker
    # 平仓成交回报到达并对齐 → 终态净值含平仓亏损与两笔佣金。
    await _fill_order(app, provider, gateway, close_request, price=770.0, trade_id="close-fill")
    done = await monitor.run_once()
    assert done[0]["status"] == "stopped"
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "stopped"
    assert float(state["currentEquity"]) == pytest.approx(1_026_580 - 52_000 - 24.71 * 2)
    assert float(state["initialEquity"]) == pytest.approx(1_026_580)
    store.close()


@pytest.mark.asyncio
async def test_fill_stream_before_terminal_report_keeps_the_fence(tmp_path):
    """反序 (fills 先到、FINISHED 后到)：平仓成交已归属（保护行已消耗），委托回读仍 ALIVE →
    仍不判停；终态回读后才 stopped，期间没有重复平仓副作用。"""
    app, provider, gateway, monitor = await _round_setup(tmp_path, _plan(maxDrawdownPercent=5))
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor)
    provider.last = 770.0
    await monitor.run_once()                       # 触发 → liquidating，平仓已发出
    close_request = dict(gateway.request)
    assert gateway.submit_count == 2
    provider.position_items = []
    provider.activity["trades"].append({
        "tradeId": "close-fill-early", "clientOrderId": close_request["clientOrderId"],
        "symbol": close_request["symbol"], "direction": "SELL", "offset": "CLOSETODAY",
        "volume": 1, "price": 770.0, "tradeDateTime": _now_ns(), "tradingDay": provider.day,
        "commission": 24.71})
    provider.activity["orders"] = [
        *(order for order in provider.activity["orders"]
          if order.get("clientOrderId") != close_request["clientOrderId"]),
        {**close_request, "status": "ALIVE", "volumeLeft": 0}]
    gateway.query_report = gateway.report(close_request, status="ALIVE", volume_left=0)
    await app.state.simulation_orders.query(str(close_request["clientOrderId"]), user_id=USER)
    app.state.autonomy.sync_strategy_protection()
    still = await monitor.run_once()
    assert still[0]["status"] == "blocked"
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "liquidating"
    assert gateway.submit_count == 2               # 没有因为“持仓没了”重复发平仓
    # 委托终态回读到达 → 才 stopped。
    provider.activity["orders"] = [
        *(order for order in provider.activity["orders"]
          if order.get("clientOrderId") != close_request["clientOrderId"]),
        {**close_request, "status": "FINISHED", "volumeLeft": 0}]
    gateway.query_report = gateway.report(close_request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(str(close_request["clientOrderId"]), user_id=USER)
    done = await monitor.run_once()
    assert done[0]["status"] == "stopped"
    assert gateway.submit_count == 2
    store.close()


@pytest.mark.asyncio
async def test_stale_account_snapshot_is_not_a_trusted_flat(tmp_path):
    """账户快照不新鲜不能当可信空仓：canResume=false、resetDrawdown 409、围栏保留；
    快照恢复新鲜后同一入口可重置。"""
    app, provider, gateway, monitor = await _round_setup(tmp_path, _plan(maxDrawdownPercent=5))
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor)
    provider.last = 770.0
    await monitor.run_once()
    close_request = dict(gateway.request)
    provider.position_items = []
    await _fill_order(app, provider, gateway, close_request, price=770.0, trade_id="close-fill")
    assert (await monitor.run_once())[0]["status"] == "stopped"

    fresh_summary = provider.account_summary
    provider.account_summary = lambda turn_id: {**fresh_summary(turn_id), "isCached": True}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})
        payload = (await client.get(f"/api/strategies/{LIVE_ID}/monitor")).json()
        assert payload["drawdown"]["canResume"] is False
        assert "不新鲜" in str(payload["drawdown"]["resumeBlockedReason"])
        refused = await client.post(f"/api/strategies/{LIVE_ID}/monitor/start",
                                    json={"resetDrawdown": True})
        assert refused.status_code == 409
        assert "strategy_drawdown_recovery_precondition" in refused.text
        assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "stopped"
        # 快照恢复新鲜 → 同一入口可重置。
        provider.account_summary = fresh_summary
        reset = await client.post(f"/api/strategies/{LIVE_ID}/monitor/start",
                                  json={"resetDrawdown": True})
        assert reset.status_code == 200, reset.text[:300]
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER) is None
    store.close()


@pytest.mark.asyncio
async def test_fee_basis_unavailable_recovers_into_the_same_round(tmp_path):
    """父代理 probe 复现（费用口径）：成交无逐笔佣金也无费率 → unavailable、暂停新开仓且
    不重复刷提示；费率恢复 → 沿用本轮基准接着判（未触线回 running），已触线照常停机。"""
    app, provider, gateway, monitor = await _round_setup(tmp_path, _plan(maxDrawdownPercent=5))
    store = app.state.store
    session = app.state.broker.create_session("local", "strategy-monitor", USER)
    store.set_strategy_signal_action(LIVE_ID, user_id=USER, signal_action=None,
                                     session_id=session.session_id)
    # 开仓成交不带逐笔佣金（也无费率可得）：本轮净值不可用。
    assert (await monitor.run_once())[0]["status"] == "submitted"
    entry_request = dict(gateway.request)
    provider.position_items = [{"symbol": SYMBOL, "direction": "long", "volume": 1,
                                "todayVolume": 1, "historicalVolume": 0, "frozenToday": 0,
                                "frozenHistorical": 0}]
    await _fill_order(app, provider, gateway, entry_request, price=822.0, trade_id="entry-no-fee",
                      commission=None)
    provider.last = 820.0
    await monitor.run_once()
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "unavailable" and "手续费依据" in str(state["reason"])
    round_started, initial = str(state["roundStartedAt"]), str(state["initialEquity"])
    await monitor.run_once()                       # 持续不可用：状态与提示都不重复刷
    again = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert again["status"] == "unavailable" and str(again["roundStartedAt"]) == round_started
    warnings = [event for _, event in store.list_events(session.session_id)
                if "手续费依据" in str((event.get("payload") or {}).get("text", ""))]
    assert len(warnings) == 1
    # 费率恢复（用户自定义费率生效）→ 同一轮接着判：820 还没触线 → running，基准沿用。
    app.state.simulation_orders.fee_settings.save(
        USER, {"source": "custom",
               "default": {"mode": "per_lot", "open": 10.0, "closeToday": 10.0, "closeYesterday": 10.0},
               "overrides": []})
    provider.last = 820.0
    await monitor.run_once()
    recovered = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert recovered["status"] == "running"
    assert str(recovered["roundStartedAt"]) == round_started and str(recovered["initialEquity"]) == initial
    assert float(recovered["currentEquity"]) == pytest.approx(1_026_580 - 2_000 - 10.0)
    assert recovered["feeSource"] == "estimated" and not str(recovered["reason"] or "")
    # 恢复后已触线仍停机：770 → 回撤超线 → liquidating。
    provider.last = 770.0
    await monitor.run_once()
    triggered = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert triggered["status"] == "liquidating"
    store.close()


@pytest.mark.asyncio
async def test_unavailable_round_resets_only_when_flat_and_aligned(tmp_path):
    """unavailable 的显式恢复：归属不明不可恢复，但已平仓且委托/成交对齐时 resetDrawdown
    可以开新一轮（canResume 同源）；running 状态下 resetDrawdown 仍被拒。"""
    app, provider, gateway, monitor = await _round_setup(tmp_path, _plan(maxDrawdownPercent=5))
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor)
    provider.last = 770.0
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})
        # running：没有停机状态可重置。
        running_refused = await client.post(f"/api/strategies/{LIVE_ID}/monitor/start",
                                            json={"resetDrawdown": True})
        assert running_refused.status_code == 409
        assert "strategy_drawdown_not_stopped" in running_refused.text
    # 手动平仓归属不明 → unavailable；持仓与保护行都已消耗、无未终态委托 → 可显式重置。
    manual = {"clientOrderId": "manual-close-2", "symbol": SYMBOL, "direction": "SELL",
              "offset": "CLOSETODAY", "volume": 1, "price": 810.0, "limitPrice": 810.0}
    provider.activity["trades"].append({
        "tradeId": "manual-fill-2", "clientOrderId": "manual-close-2", "symbol": SYMBOL,
        "direction": "SELL", "offset": "CLOSETODAY", "volume": 1, "price": 810.0,
        "tradeDateTime": _now_ns(), "tradingDay": provider.day, "commission": 24.71})
    provider.activity["orders"] = [*provider.activity["orders"], {**manual, "status": "FINISHED",
                                                                  "volumeLeft": 0}]
    provider.position_items = []
    app.state.autonomy.sync_strategy_protection()
    provider.last = 770.0
    await monitor.run_once()
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "unavailable" and "归属不明" in str(state["reason"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})
        payload = (await client.get(f"/api/strategies/{LIVE_ID}/monitor")).json()
        assert payload["drawdown"]["canResume"] is True
        reset = await client.post(f"/api/strategies/{LIVE_ID}/monitor/start",
                                  json={"resetDrawdown": True})
        assert reset.status_code == 200, reset.text[:300]
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER) is None
    store.close()


@pytest.mark.asyncio
async def test_real_confirmation_card_chain_reserves_budget_and_confirms_once(tmp_path):
    """真实确认卡链 (2026-10-10 验收缺口)：equity_percent → ProposalService 出卡 → 正式 confirm
    入口 → 模拟网关下单。本笔预留不重复挤占自己；另一笔 RESERVED 仍占预算（确认被拒、不发单、
    卡保留）；预留终结后确认发出 1 手；第二次确认不重复成交（同 key 幂等、换 key 409）。"""
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / "card-chain.sqlite3", gateway, provider)
    store = app.state.store
    _save_with_plan(store, _plan(sizing={"mode": "equity_percent", "equityPercent": 10}))
    session = app.state.broker.create_session("local", "strategy-monitor", USER)
    store.set_strategy_signal_action(LIVE_ID, user_id=USER, signal_action="confirm",
                                     session_id=session.session_id)
    monitor = StrategyMonitorService(store=store, proposals=app.state.proposals,
                                     autonomy=app.state.autonomy)
    proposed = await monitor.run_once()
    assert proposed[0]["status"] == "proposed", proposed
    card_id = str(proposed[0]["proposalIds"][0])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})
        card = (await client.get(f"/api/proposals/{card_id}")).json()
        # 另一笔本策略的执行中预留（还没拿到委托号）占掉全部 10% 预算：确认被拒、没有发单。
        _seed_pending_attempt(store, "attempt-other", bar_time="1767225601000000000", order_id=None)
        refused = await client.post(f"/api/proposals/{card_id}/confirm", json={
            "approvalId": card["approvalId"], "proposalVersion": card["version"],
            "idempotencyKey": "card-confirm-0001"})
        assert refused.status_code == 409, refused.text[:300]
        assert "strategy_funds_budget_exceeded" in refused.text
        assert gateway.submit_count == 0
        # 那笔预留终结后，正式确认在预算内发出 1 手（本笔预留不重复计入）。
        store.finish_strategy_execution_attempt(attempt_id="attempt-other", status="SKIPPED",
                                                detail="测试终结")
        fresh = (await client.get(f"/api/proposals/{card_id}")).json()
        confirmed = await client.post(f"/api/proposals/{card_id}/confirm", json={
            "approvalId": fresh["approvalId"], "proposalVersion": fresh["version"],
            "idempotencyKey": "card-confirm-0002"})
        assert confirmed.status_code == 200, confirmed.text[:300]
        assert gateway.submit_count == 1 and gateway.request["volume"] == 1
        # 同 key 幂等重放：不重复发单；换 key 再确认：409，不重复成交。
        replay = await client.post(f"/api/proposals/{card_id}/confirm", json={
            "approvalId": fresh["approvalId"], "proposalVersion": fresh["version"],
            "idempotencyKey": "card-confirm-0002"})
        assert replay.status_code == 200 and gateway.submit_count == 1
        second = await client.post(f"/api/proposals/{card_id}/confirm", json={
            "approvalId": fresh["approvalId"], "proposalVersion": fresh["version"],
            "idempotencyKey": "card-confirm-0003"})
        assert second.status_code == 409 and "invalid_state_transition" in second.text
        assert gateway.submit_count == 1
        assert len(app.state.simulation_orders.list(user_id=USER)) == 1
        attempts = store.list_strategy_execution_attempts(strategy_id=LIVE_ID, user_id=USER)
        card_attempt = next(item for item in attempts
                            if item["attemptId"] != "attempt-other")
        assert card_attempt["status"] not in {"RESERVED", "SKIPPED"}
        assert card_attempt["clientOrderId"]
    store.close()


# -- 一致性轮 (2026-10-10 #692/#694)：同步失败围栏、图表周期同轮、清算不被错误计数、终态释放与工具同口径 --

import hashlib  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from trader_runtime.proposals import ProposalConflict  # noqa: E402
from trader_runtime.trader_tools import TraderReadOnlyTools  # noqa: E402

CHART_ALWAYS = [{"label": "总是", "left": {"indicator": "close", "timeframe": 0}, "operator": "gt",
                 "right": {"indicator": "constant", "value": 0}}]
CHART_NEVER = [{"label": "从不", "left": {"indicator": "close", "timeframe": 0}, "operator": "lt",
                "right": {"indicator": "constant", "value": 0}}]


def _chart_plan(**fields: Any) -> StrategyPlan:
    """跟随图表周期的计划（条件周期 0）：切图表周期只改派生的执行周期部分。"""
    return _plan(rules={"entryLong": {"conditions": CHART_ALWAYS},
                        "exitLong": {"conditions": CHART_NEVER}},
                 timeframeSeconds=0, **fields)


def _bind(unresolved: StrategyPlan, timeframe: int) -> dict[str, Any]:
    from trader_runtime.strategy_chart_period import bind_definition
    return bind_definition({**definition_for(unresolved), "chartPlan": unresolved.model_dump()}, timeframe)


def _definition_digest(definition: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()


async def test_drawdown_exit_zero_dispatch_while_fill_sync_fails(tmp_path):
    """#692：迟到成交归属没落定（sync 失败）时，回撤停机出口与止损命中共用同一段围栏——
    零派发、保护行保持 ARMED；同步恢复后补发一次，随后照常收敛到 stopped。"""
    app, provider, gateway, monitor = await _round_setup(tmp_path / "sync-fence.sqlite3",
                                                         _plan(maxDrawdownPercent=5))
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor)
    fired_before = gateway.submit_count
    real_sync = app.state.autonomy.sync_strategy_protection

    def failing_sync(*, user_id=None, symbol=None):
        real_sync(user_id=user_id, symbol=symbol)
        return {(USER, SYMBOL): "strategy_fill_history_out_of_order"}

    app.state.autonomy.sync_strategy_protection = failing_sync
    provider.last = 770.0
    breach = await monitor.run_once()
    assert breach[0]["status"] == "blocked"
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "liquidating" and state["triggeredAt"] is not None
    assert gateway.submit_count == fired_before            # 围栏：零派发
    row = store.connection.execute(
        "SELECT status FROM ai_stop_orders WHERE user_id = ? AND symbol = ?",
        (USER, SYMBOL)).fetchone()
    assert row["status"] == "ARMED"                        # 行保持 ARMED，没进 TRIGGERING

    app.state.autonomy.sync_strategy_protection = real_sync
    progress = await monitor.run_once()
    assert progress[0]["status"] == "blocked" and "平仓处理中" in progress[0]["detail"]
    assert gateway.submit_count == fired_before + 1        # 恢复后补发一次
    close_request = dict(gateway.request)
    assert close_request["offset"] == "CLOSETODAY"
    provider.position_items = []
    await _fill_order(app, provider, gateway, close_request, price=770.0, trade_id="close-fill")
    done = await monitor.run_once()
    assert done[0]["status"] == "stopped"
    assert gateway.submit_count == fired_before + 1        # 只补发这一次
    store.close()


def _chart_round_setup(db_path, plan: StrategyPlan):
    provider, gateway = Provider(), Gateway()
    app = application(db_path, gateway, provider)
    store = app.state.store
    store.ensure_default_trading_account(execution_owner_user_id=USER, provider_kind="fake",
                                         display_name="假数据模拟账户", sidecar_url=None)
    definition = _bind(plan, 300)
    digest = _definition_digest(definition)
    store.save_strategy_draft(
        strategy_id=LIVE_ID, user_id=USER, source_session_id="session-funds",
        name="图表周期同轮", symbol=SYMBOL, version=1, status="monitoring",
        definition=definition, definition_digest=digest)
    store.save_strategy_validation_report(
        report_id="validation-chart", strategy_id=LIVE_ID, user_id=USER,
        strategy_digest=digest, status="passed", checks=[], evidence={})
    intent = _intent(store)
    monitor = StrategyMonitorService(store=store, proposals=ProposalStub(store, intent),
                                     autonomy=app.state.autonomy)
    return app, provider, gateway, monitor, definition


async def _rebind(monitor, store, provider, timeframe: int) -> None:
    """正式切周期入口（与路由同一 rebind_items；调用方持 period_lock）。"""
    from trader_runtime.strategy_chart_period import rebind_items
    item = store.get_strategy_draft(LIVE_ID, user_id=USER)
    async with monitor.period_lock:
        changed = await rebind_items([item], timeframe, store=store, provider=provider)
    assert changed == [LIVE_ID]


async def _drive_round_to(stage: str, app, provider, gateway, monitor) -> None:
    await _entry_filled(app, provider, gateway, monitor)            # → running
    if stage == "running":
        return
    provider.last = 770.0
    breach = await monitor.run_once()
    assert breach[0]["status"] == "blocked"                          # → liquidating
    if stage == "liquidating":
        return
    close_request = dict(gateway.request)
    provider.position_items = []
    await _fill_order(app, provider, gateway, close_request, price=770.0, trade_id="close-fill")
    done = await monitor.run_once()
    assert done[0]["status"] == "stopped"                            # → stopped


async def test_chart_period_switch_keeps_the_same_risk_round(tmp_path):
    """#692：切图表周期（300→900→300，正式 rebind_items 入口）是同一风险轮——起点/峰值/轮起点
    保留、旧浮亏不重算，停机中的一轮换周期后仍锁定；真正改了定义才开新轮（新起点与基准同批写入）。"""
    from trader_runtime.strategy_funds import round_identity_digest
    app, provider, gateway, monitor, _ = _chart_round_setup(tmp_path / "chart-round.sqlite3",
                                                            _chart_plan(maxDrawdownPercent=5))
    store = app.state.store

    def redefine(plan: StrategyPlan, timeframe: int) -> None:
        # 真正改定义（编辑器保存路径之外的直接改写）：与切周期无关的规则参数变化。
        bound = _bind(plan, timeframe)
        with store.lock, store.connection:
            store.connection.execute(
                """UPDATE strategy_drafts SET definition_json = ?, definition_digest = ?,
                   updated_at = datetime('now') WHERE id = ? AND user_id = ?""",
                (json.dumps(bound, sort_keys=True, ensure_ascii=False),
                 _definition_digest(bound), LIVE_ID, USER))

    await _entry_filled(app, provider, gateway, monitor)
    first = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert first["status"] == "running"
    provider.last = 800.0                       # 822 → 800：浮亏约 2.1%，未到 5% 线
    await monitor.run_once()
    dipped = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert dipped["status"] == "running" and float(dipped["currentEquity"]) < float(dipped["peakEquity"])

    for timeframe in (900, 300):                # 切过去再切回来：同一轮，不重建基准
        await _rebind(monitor, store, provider, timeframe)
        result = await monitor.run_once()
        assert result[0]["status"] not in {"paused", "error"}
        state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
        assert state["status"] == "running"
        assert state["roundStartedAt"] == first["roundStartedAt"]
        assert float(state["initialEquity"]) == float(first["initialEquity"])
        assert float(state["peakEquity"]) == float(dipped["peakEquity"])
        assert abs(float(state["currentEquity"]) - float(dipped["currentEquity"])) < 0.5
        assert str(state["strategyDigest"]) == round_identity_digest(
            store.get_strategy_draft(LIVE_ID, user_id=USER)["definition"])

    provider.last = 770.0                        # 过线 → 停机
    breach = await monitor.run_once()
    assert breach[0]["status"] == "blocked"
    close_request = dict(gateway.request)
    provider.position_items = []
    await _fill_order(app, provider, gateway, close_request, price=770.0, trade_id="close-fill")
    done = await monitor.run_once()
    assert done[0]["status"] == "stopped"
    stopped = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert stopped["status"] == "stopped"

    await _rebind(monitor, store, provider, 900)  # 停机中的一轮换周期：仍锁定，不假恢复
    monitor.proposals.transition_strategy_status(
        strategy_id=LIVE_ID, user_id=USER, target="monitoring")
    held = await monitor.run_once()
    assert held[0]["status"] == "blocked" and held[0]["action"] == "hold"
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert state["status"] == "stopped" and state["roundStartedAt"] == first["roundStartedAt"]

    # SQLite 重开：同一轮的起点/峰值/停机锁原样持久（另一个连接读同一个库文件）。
    reopened = RuntimeStore(tmp_path / "chart-round.sqlite3")
    persisted = reopened.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert persisted["status"] == "stopped"
    assert persisted["roundStartedAt"] == first["roundStartedAt"]
    assert float(persisted["initialEquity"]) == float(stopped["initialEquity"])
    assert float(persisted["peakEquity"]) == float(stopped["peakEquity"])
    reopened.close()

    # 真正改了定义（chartPlan 的规则参数）才是另一轮：新起点与基准同批写入。
    edited = _chart_plan(maxDrawdownPercent=5, takeProfitTicks=4)
    redefine(edited, 900)
    monitor.proposals.transition_strategy_status(
        strategy_id=LIVE_ID, user_id=USER, target="monitoring")
    fresh_result = await monitor.run_once()
    assert fresh_result[0]["status"] not in {"paused", "error"}
    fresh = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert fresh["status"] == "running"
    assert fresh["roundStartedAt"] != first["roundStartedAt"]
    assert float(fresh["initialEquity"]) == float(fresh["peakEquity"]) == float(fresh["currentEquity"])
    store.close()


@pytest.mark.parametrize("stage", ["running", "liquidating", "stopped"])
async def test_old_full_digest_rows_migrate_on_the_formal_period_switch(tmp_path, stage):
    """#692 复审：部署前写的回撤行存完整定义摘要——正式 rebind_items 在同一个改周期事务里把
    匹配的旧行迁成身份摘要（部署后第一次轮询之前就切周期也覆盖，停机实例本来不扫描）；
    轮次/峰值/净值/费用/状态原样，重开数据库一致，autonomy 开仓入口仍按停机/清算围栏拒绝。"""
    from trader_runtime.strategy_funds import round_identity_digest
    db_path = tmp_path / f"old-digest-{stage}.sqlite3"
    app, provider, gateway, monitor, definition = _chart_round_setup(db_path, _chart_plan(maxDrawdownPercent=5))
    store = app.state.store
    await _drive_round_to(stage, app, provider, gateway, monitor)
    before = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert before["status"] == stage
    old_full_digest = _definition_digest(definition)

    # 部署前的旧行：状态行存的是当时的完整定义摘要（切周期前）。
    with store.lock, store.connection:
        store.connection.execute(
            "UPDATE strategy_drawdown_states SET strategy_digest = ? WHERE strategy_id = ?",
            (old_full_digest, LIVE_ID))

    for timeframe in (900, 300):        # 切过去再切回来，期间不跑任何监控轮询
        await _rebind(monitor, store, provider, timeframe)
        after = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
        assert after["status"] == stage                       # 轮次原样：不解锁、不重建
        assert after["roundStartedAt"] == before["roundStartedAt"]
        assert float(after["initialEquity"]) == float(before["initialEquity"])
        assert float(after["peakEquity"]) == float(before["peakEquity"])
        assert abs(float(after["currentEquity"]) - float(before["currentEquity"])) < 0.01
        assert after["feeSource"] == before["feeSource"]
        assert str(after["strategyDigest"]) == round_identity_digest(
            store.get_strategy_draft(LIVE_ID, user_id=USER)["definition"])

    # 迁移后的行被监控认作同一轮：running 继续、liquidating 继续推进、stopped 保持锁定。
    if store.get_strategy_draft(LIVE_ID, user_id=USER)["status"] != "monitoring":
        monitor.proposals.transition_strategy_status(strategy_id=LIVE_ID, user_id=USER, target="monitoring")
    polled = await monitor.run_once()
    state = store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    if stage == "running":
        assert polled[0]["status"] not in {"paused", "error"}
        assert state["status"] == "running"
    elif stage == "liquidating":
        assert polled[0]["status"] == "blocked" and "平仓处理中" in polled[0]["detail"]
        assert state["status"] == "liquidating"
    else:
        assert polled[0]["status"] == "blocked" and polled[0]["action"] == "hold"
        assert state["status"] == "stopped"
    assert state["roundStartedAt"] == before["roundStartedAt"]

    # SQLite 重开：迁移后的同一轮原样持久（running 的一轮轮询会合法推进峰值，按最后写入比对）。
    reopened = RuntimeStore(db_path)
    persisted = reopened.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)
    assert persisted["status"] == state["status"]
    assert persisted["roundStartedAt"] == before["roundStartedAt"]
    assert float(persisted["initialEquity"]) == float(before["initialEquity"])
    assert float(persisted["peakEquity"]) == float(state["peakEquity"])
    reopened.close()

    # autonomy 开仓入口：停机/清算中的一轮换周期后仍不放行开仓。停机的一轮（仓位已平、保护行
    # 已消耗）由回撤围栏挡下——身份摘要迁移后围栏认得同一轮；清算中的一轮还持有保护中的仓位，
    # 持仓围栏先挡。running 的一轮开仓与回撤围栏无关，不在此时点验。
    authorization = {"authorizationId": "auth-funds", "sessionId": "session-funds"}
    if stage == "stopped":
        with pytest.raises(AutonomyError) as error:
            app.state.autonomy._strategy_protection(
                user_id=USER, session_id="session-funds", strategy_id=LIVE_ID, symbol=SYMBOL,
                side="buy_open", volume=1, position_bucket="today",
                authorization=authorization, strategy_bar_time=None)
        assert error.value.code == "strategy_drawdown_locked"
    elif stage == "liquidating":
        with pytest.raises(AutonomyError) as error:
            app.state.autonomy._strategy_protection(
                user_id=USER, session_id="session-funds", strategy_id=LIVE_ID, symbol=SYMBOL,
                side="buy_open", volume=1, position_bucket="today",
                authorization=authorization, strategy_bar_time=None)
        assert error.value.code == "strategy_position_already_protected"
    store.close()


class _StalePreviewStub(ProposalStub):
    """模拟行情不新鲜时正式评估的失败（strategy_market_not_fresh）。"""

    def preview_strategy_intent(self, **_: Any) -> dict[str, Any]:
        raise ProposalConflict("strategy_market_not_fresh")


async def test_liquidating_round_is_not_error_paused_on_stale_market(tmp_path):
    """#692：清算中的一轮先于新鲜度判断被识别——行情/评估连续失败三次也不落进普通错误
    计数把实例 paused（清单只扫 monitoring，会永久搁置）；事实恢复后自动接着清算到 stopped。"""
    app, provider, gateway, monitor = await _round_setup(tmp_path / "stale-liquidating.sqlite3",
                                                         _plan(maxDrawdownPercent=5))
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor)
    provider.last = 770.0
    breach = await monitor.run_once()
    assert breach[0]["status"] == "blocked"
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "liquidating"
    close_request = dict(gateway.request)
    assert close_request["offset"] == "CLOSETODAY"

    intent = _intent(store)
    monitor.proposals = _StalePreviewStub(store, intent)
    for _ in range(3):
        result = await monitor.run_once()
        assert result[0]["status"] == "blocked" and "平仓处理中" in result[0]["detail"]
    assert store.get_strategy_draft(LIVE_ID, user_id=USER)["status"] == "monitoring"
    monitor_state = store.strategy_monitor_state(strategy_id=LIVE_ID, user_id=USER)
    assert not (monitor_state or {}).get("consecutiveFailures")
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "liquidating"

    monitor.proposals = ProposalStub(store, intent)
    provider.position_items = []
    await _fill_order(app, provider, gateway, close_request, price=770.0, trade_id="close-fill")
    done = await monitor.run_once()
    assert done[0]["status"] == "stopped"
    assert store.get_strategy_draft(LIVE_ID, user_id=USER)["status"] == "paused"
    store.close()


@pytest.mark.parametrize("summary_fault", ["raises", "day_missing", "day_malformed", "not_a_mapping"])
async def test_persisted_rounds_survive_unreadable_provider_reads(tmp_path, summary_fault):
    """#692 复审 / #698 复审：清算中的一轮带着仍在的保护（ARMED）推进时，sync_protection 在
    per-symbol try 之前读 account_summary.tradingDay——缺失/非法/形状错误抛出任何异常（不只是
    RuntimeError），都在 run_once 的异常归类入口按同轮清算隔离：三次也不落进普通错误计数自动
    暂停（清单只扫 monitoring，暂停即永久搁置清算）；零非法派发；事实恢复后沿原保护行发出平仓
    并完成到 stopped。停机的一轮保持锁定，不自动恢复新开仓。故障前提是真实 provider 读取失败
    （一笔在途平仓单的出口就绪围栏把回撤触发的保护性平仓挡下、行留在 ARMED，清算每轮都真实
    走进 strptime），不用 Stub。"""
    app, provider, gateway, monitor = await _round_setup(
        tmp_path / f"unreadable-{summary_fault}.sqlite3", _plan(maxDrawdownPercent=5))
    store = app.state.store
    await _entry_filled(app, provider, gateway, monitor)

    def unreadable(*_args, **_kwargs):
        raise RuntimeError("provider read lost")

    real_activity, real_summary, real_positions = (
        provider.account_activity, provider.account_summary, provider.positions)
    fault_type = {"raises": "RuntimeError", "day_missing": "ValueError",
                  "day_malformed": "ValueError", "not_a_mapping": "AttributeError"}[summary_fault]

    # 一笔同方向、同持仓类别的在途平仓单把回撤触发的保护性平仓挡在出口就绪上：轮进入
    # liquidating，保护行保持 ARMED——之后每轮清算推进都会真实走进 sync_protection 的
    # 账户读取（tradingDay 的 strptime 在 per-symbol try 之前，异常会直接逃出派发链）。
    provider.activity["orders"] = [*provider.activity["orders"], {
        "symbol": SYMBOL, "clientOrderId": "manual-close-hold", "direction": "SELL",
        "offset": "CLOSETODAY", "status": "ALIVE", "volume": 1, "volumeLeft": 1}]
    provider.last = 770.0
    breach = await monitor.run_once()
    assert breach[0]["status"] == "blocked" and "平仓处理中" in breach[0]["detail"]
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "liquidating"
    fired = gateway.submit_count
    assert fired == 1                              # 只有开仓那一笔；保护被出口就绪挡下
    provider.activity["orders"] = [order for order in provider.activity["orders"]
                                   if order.get("clientOrderId") != "manual-close-hold"]

    # 账户摘要故障（读取失败 / 交易日缺失 / 非法格式 / 形状错误）：曾经三次即自动暂停、
    # 清算被永久搁置。读取失败沿用陈旧活动的组合；交易日/形状故障保持活动新鲜，
    # 确保 sync 确实走到 strptime 那一行。
    if summary_fault == "raises":                  # 既有案例：读取本身 RuntimeError
        provider.account_activity = lambda: {**real_activity(), "ordersInitialized": False}
        provider.account_summary = unreadable
    elif summary_fault == "not_a_mapping":         # 另一种数据形状错误：不是特判 ValueError
        provider.account_summary = lambda *_a, **_k: None
    else:
        provider.day = "" if summary_fault == "day_missing" else "2026/08/25"
    provider.positions = unreadable
    for _ in range(3):
        result = await monitor.run_once()
        assert result[0]["status"] == "blocked" and result[0]["action"] == "close"
        assert fault_type in result[0]["detail"]   # 真实故障如实展示，不假报清算完成
    assert store.get_strategy_draft(LIVE_ID, user_id=USER)["status"] == "monitoring"
    assert not (store.strategy_monitor_state(strategy_id=LIVE_ID, user_id=USER) or {}).get(
        "consecutiveFailures")
    assert gateway.submit_count == fired          # 围栏：读不出来就零派发，不撞运气
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "liquidating"

    # 事实恢复：原保护行接着推进，平仓成交回读后到 stopped。
    provider.account_activity, provider.account_summary, provider.positions = (
        real_activity, real_summary, real_positions)
    provider.day = "2026-08-25"
    recovered = await monitor.run_once()
    assert recovered[0]["status"] == "blocked" and "平仓处理中" in recovered[0]["detail"]
    close_request = dict(gateway.request)
    assert close_request["offset"] == "CLOSETODAY" and gateway.submit_count == fired + 1
    provider.position_items = []
    await _fill_order(app, provider, gateway, close_request, price=770.0, trade_id="close-fill")
    done = await monitor.run_once()
    assert done[0]["status"] == "stopped"
    assert store.get_strategy_draft(LIVE_ID, user_id=USER)["status"] == "paused"

    # 停机中的一轮遇到同样的读取失败：保持锁定、不进错误计数，恢复后也不自动重开新仓。
    monitor.proposals.transition_strategy_status(strategy_id=LIVE_ID, user_id=USER, target="monitoring")
    provider.account_activity = lambda: {**real_activity(), "ageSeconds": 999, "isCached": True}
    provider.account_summary = unreadable
    provider.positions = unreadable
    for _ in range(3):
        held = await monitor.run_once()
        assert held[0]["status"] == "blocked" and held[0]["action"] == "hold"
    assert store.get_strategy_draft(LIVE_ID, user_id=USER)["status"] == "monitoring"
    assert not (store.strategy_monitor_state(strategy_id=LIVE_ID, user_id=USER) or {}).get(
        "consecutiveFailures")
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "stopped"
    provider.account_activity, provider.account_summary, provider.positions = (
        real_activity, real_summary, real_positions)
    resumed = await monitor.run_once()
    assert resumed[0]["status"] == "blocked" and resumed[0]["action"] == "hold"   # 不自动恢复
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "stopped"
    store.close()


def _tools_for(app) -> TraderReadOnlyTools:
    return TraderReadOnlyTools(
        SimpleNamespace(mode="fake"),  # type: ignore[arg-type]
        lambda mode: {"mode": mode},
        proposals=app.state.proposals,
        autonomy=app.state.autonomy,
    )


async def test_builtin_tool_start_and_status_match_the_http_contract(tmp_path):
    """#692：内置工具与 HTTP monitor/start 共用同一启动/重置/状态链路——普通 start 不假报
    已恢复、resetDrawdown 前提失败被拒、重复 reset 不重复行为、他账户策略被隔离。"""
    app, provider, gateway, monitor = await _round_setup(
        tmp_path / "tool-parity.sqlite3",
        _plan(maxDrawdownPercent=5, sizing={"mode": "equity_percent", "equityPercent": 10}))
    store = app.state.store
    tools = _tools_for(app)
    await _entry_filled(app, provider, gateway, monitor)
    provider.last = 770.0
    await monitor.run_once()
    close_request = dict(gateway.request)
    provider.position_items = []
    await _fill_order(app, provider, gateway, close_request, price=770.0, trade_id="close-fill")
    done = await monitor.run_once()
    assert done[0]["status"] == "stopped"
    assert store.get_strategy_draft(LIVE_ID, user_id=USER)["status"] == "paused"

    # 普通 start：如实说停机一轮保持停止，不假报已恢复；不带执行方式/会话时回读的是迁移后的行
    # （status=monitoring，与库里一致——不再是迁移前的旧 paused）。
    plain, _ = tools.call(tool="start_strategy_monitoring", arguments={"strategyId": LIVE_ID},
                          turn_id="tool-plain", mode="local", user_id=USER)
    assert plain["success"] is True
    assert "保持停止" in plain["contentItems"][0]["text"]
    plain_payload = json.loads(plain["contentItems"][0]["text"])
    assert plain_payload["data"]["status"] == "monitoring"
    assert store.get_strategy_draft(LIVE_ID, user_id=USER)["status"] == "monitoring"
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "stopped"

    # 状态与 HTTP 的 monitor 载荷同一份 funds / drawdown / canResume。
    status, _ = tools.call(tool="get_strategy_monitor_status", arguments={"strategyId": LIVE_ID},
                           turn_id="tool-status", mode="local", user_id=USER)
    assert status["success"] is True
    tool_payload = json.loads(status["contentItems"][0]["text"])["data"]
    assert tool_payload["drawdown"]["status"] == "stopped"
    assert tool_payload["drawdown"]["canResume"] is True
    assert tool_payload["drawdown"]["drawdownPercent"] >= 5
    assert tool_payload["funds"]["availableLots"] >= 0
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})
        http_payload = (await client.get(f"/api/strategies/{LIVE_ID}/monitor")).json()
    assert http_payload["drawdown"]["status"] == tool_payload["drawdown"]["status"]
    assert http_payload["drawdown"]["canResume"] == tool_payload["drawdown"]["canResume"]
    assert http_payload["drawdown"]["drawdownPercent"] == tool_payload["drawdown"]["drawdownPercent"]
    assert http_payload["funds"]["availableLots"] == tool_payload["funds"]["availableLots"]

    # 恢复前提失败（还有一笔执行中的开仓预留）：拒绝且停机状态原样保留。
    _seed_pending_attempt(store, "attempt-hold", bar_time="1767225602000000000", order_id=None)
    refused, _ = tools.call(tool="start_strategy_monitoring",
                            arguments={"strategyId": LIVE_ID, "resetDrawdown": True},
                            turn_id="tool-refused", mode="local", user_id=USER)
    assert refused["success"] is False
    assert "strategy_drawdown_recovery_precondition" in refused["contentItems"][0]["text"]
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "stopped"

    store.finish_strategy_execution_attempt(attempt_id="attempt-hold", status="SKIPPED",
                                            detail="测试终结")
    reset, _ = tools.call(tool="start_strategy_monitoring",
                          arguments={"strategyId": LIVE_ID, "resetDrawdown": True},
                          turn_id="tool-reset", mode="local", user_id=USER)
    assert reset["success"] is True
    assert "已重置回撤" in reset["contentItems"][0]["text"]
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER) is None
    # 重复 reset：没有停机中的一轮可重置，拒绝且不产生任何重复行为。
    repeat, _ = tools.call(tool="start_strategy_monitoring",
                           arguments={"strategyId": LIVE_ID, "resetDrawdown": True},
                           turn_id="tool-repeat", mode="local", user_id=USER)
    assert repeat["success"] is False
    assert "strategy_drawdown_not_stopped" in repeat["contentItems"][0]["text"]

    # 他账户的策略：启动被拒，状态不给读数（不用本账户行情算另一账户的占用）。
    store.connection.execute("PRAGMA foreign_keys = OFF")
    store.save_strategy_draft(
        strategy_id="strategy-other-acct", user_id=USER, source_session_id="session-funds",
        name="他账户策略", symbol=SYMBOL, version=1, status="ready",
        definition=definition_for(_plan()), definition_digest="f" * 64,
        trading_account_id="tqkq-sim:other")
    isolated, _ = tools.call(tool="start_strategy_monitoring",
                             arguments={"strategyId": "strategy-other-acct"},
                             turn_id="tool-isolated", mode="local", user_id=USER)
    assert isolated["success"] is False
    assert "strategy_account_not_bound" in isolated["contentItems"][0]["text"]
    other_status, _ = tools.call(tool="get_strategy_monitor_status",
                                 arguments={"strategyId": "strategy-other-acct"},
                                 turn_id="tool-other-status", mode="local", user_id=USER)
    other_payload = json.loads(other_status["contentItems"][0]["text"])["data"]
    assert other_payload["funds"] is None and other_payload["drawdown"] is None
    assert store.get_strategy_draft("strategy-other-acct", user_id=USER)["status"] == "ready"

    # 非执行负责人：operator 拥有本账户上的策略记录，但执行负责人是 USER——启动/重置都拒绝，
    # 状态与回撤行原样（与 HTTP monitor/start 的 require_execution_owner 同一口径）。
    operator = "user-operator"
    store.save_strategy_draft(
        strategy_id="strategy-operator", user_id=operator, source_session_id="session-operator",
        name="operator 的策略", symbol=SYMBOL, version=1, status="paused",
        definition=definition_for(_plan(maxDrawdownPercent=5)), definition_digest="a" * 64)
    store.save_strategy_drawdown_state(
        strategy_id="strategy-operator", user_id=operator, status="stopped", limit_percent=5.0,
        initial_equity="1000000", peak_equity="1000000", current_equity="940000",
        strategy_digest="a" * 64, round_started_at="2026-10-09T00:00:00+00:00",
        reason="回撤停机已触发。")
    operator_start, _ = tools.call(tool="start_strategy_monitoring",
                                   arguments={"strategyId": "strategy-operator"},
                                   turn_id="tool-operator", mode="local", user_id=operator)
    assert operator_start["success"] is False
    assert "not_execution_owner" in operator_start["contentItems"][0]["text"]
    operator_reset, _ = tools.call(tool="start_strategy_monitoring",
                                   arguments={"strategyId": "strategy-operator", "resetDrawdown": True},
                                   turn_id="tool-operator-reset", mode="local", user_id=operator)
    assert operator_reset["success"] is False
    assert "not_execution_owner" in operator_reset["contentItems"][0]["text"]
    assert store.get_strategy_draft("strategy-operator", user_id=operator)["status"] == "paused"
    assert store.strategy_drawdown_state(
        strategy_id="strategy-operator", user_id=operator)["status"] == "stopped"
    store.close()


async def test_plain_start_reports_a_liquidating_round_honestly(tmp_path):
    """#692 复审：清算中的一轮普通 start 如实说仍在平仓、新开仓锁定——不声称已恢复；
    liquidating 也不是可重置的停机轮，显式 resetDrawdown 仍被拒。"""
    app, provider, gateway, monitor = await _round_setup(tmp_path / "tool-liquidating.sqlite3",
                                                         _plan(maxDrawdownPercent=5))
    store = app.state.store
    tools = _tools_for(app)
    await _entry_filled(app, provider, gateway, monitor)
    provider.last = 770.0
    await monitor.run_once()
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "liquidating"

    plain, _ = tools.call(tool="start_strategy_monitoring", arguments={"strategyId": LIVE_ID},
                          turn_id="tool-liquidating-plain", mode="local", user_id=USER)
    assert plain["success"] is True
    payload = json.loads(plain["contentItems"][0]["text"])
    assert payload["data"]["status"] == "monitoring"
    assert payload["data"]["drawdownStatus"] == "liquidating"
    assert "仍在平仓" in payload["warning"] and "新开仓" in payload["warning"]
    assert "已重置" not in payload["warning"]

    reset, _ = tools.call(tool="start_strategy_monitoring",
                          arguments={"strategyId": LIVE_ID, "resetDrawdown": True},
                          turn_id="tool-liquidating-reset", mode="local", user_id=USER)
    assert reset["success"] is False
    assert "strategy_drawdown_liquidating" in reset["contentItems"][0]["text"]
    assert store.strategy_drawdown_state(strategy_id=LIVE_ID, user_id=USER)["status"] == "liquidating"
    store.close()


class _GatewayReads:
    """pending_open_lots 的网关读数替身：与在途扫描同一身份来源的原单回读。"""

    def __init__(self, orders: dict[str, Any]) -> None:
        self.orders = orders

    def get(self, identifier: str, *, user_id: str | None = None) -> dict[str, Any]:
        order = self.orders[identifier]
        if isinstance(order, Exception):
            raise order
        return order


def _seed_fill_event(store: RuntimeStore, order_id: str, volume: int) -> None:
    with store.lock, store.connection:
        store.connection.execute(
            """INSERT INTO strategy_fill_events
               (user_id, event_id, trade_time, trading_day, client_order_id, payload_json)
               VALUES (?, ?, ?, '2026-08-25', ?, ?)""",
            (USER, f"event-{order_id}-{volume}", _now_ns(), order_id,
             json.dumps({"trade": {"volume": volume, "price": 822.0, "tradeId": f"t-{order_id}"}})))


def test_terminal_cancelled_remainder_releases_the_budget(tmp_path):
    """#694：原单可信终态（FINISHED）时已撤余量释放——全撤释放全部、部分成交释放未成交部分；
    已成交但成交流还没归属的数量继续占用。重复查询与重开数据库读数一致。"""
    db_path = tmp_path / "terminal-release.sqlite3"
    store = RuntimeStore(db_path)
    _save_with_plan(store, _plan())
    seeds = [
        ("attempt-all-cancel", "order-all-cancel", 5),
        ("attempt-partial", "order-partial", 5),
        ("attempt-filled", "order-filled", 5),
    ]
    for attempt_id, order_id, volume in seeds:
        _seed_pending_attempt(store, attempt_id, bar_time=f"176722560000000000{len(attempt_id)}",
                              order_id=order_id)
        with store.lock, store.connection:
            store.connection.execute(
                "UPDATE strategy_execution_attempts SET volume = ? WHERE id = ?",
                (volume, attempt_id))
    _seed_fill_event(store, "order-partial", 2)      # 部分成交的 2 手已归属
    gateway = _GatewayReads({
        "order-all-cancel": {"submitPhase": "CONFIRMED", "remoteStatus": "FINISHED",
                             "filledVolume": 0, "request": {"volume": 5}},
        "order-partial": {"submitPhase": "CONFIRMED", "remoteStatus": "FINISHED",
                          "filledVolume": 2, "request": {"volume": 5}},
        "order-filled": {"submitPhase": "CONFIRMED", "remoteStatus": "FINISHED",
                         "filledVolume": 2, "request": {"volume": 5}},
    })
    # 全撤 5 释放；部分成交 2（已归属）+ 撤 3 → 0 占用；成交 2 未归属 → 2 手继续占用。
    assert pending_open_lots(store, USER, LIVE_ID, gateway=gateway) == 2
    assert pending_open_lots(store, USER, LIVE_ID, gateway=gateway) == 2   # 重复查询一致
    store.close()

    reopened = RuntimeStore(db_path)
    assert pending_open_lots(reopened, USER, LIVE_ID,
                             gateway=gateway) == 2                          # 重开数据库一致
    reopened.close()


def test_unterminal_and_unreadable_orders_still_occupy(tmp_path):
    """#694：ALIVE、UNKNOWN、读取失败与没拿到委托号的 RESERVED 一律保守占用，不因查询失败放松。"""
    store = RuntimeStore(tmp_path / "conservative.sqlite3")
    _save_with_plan(store, _plan())
    for attempt_id, order_id in (("attempt-alive", "order-alive"),
                                 ("attempt-unknown", "order-unknown"),
                                 ("attempt-read-fail", "order-read-fail")):
        _seed_pending_attempt(store, attempt_id,
                              bar_time=f"176722560000000000{len(attempt_id)}", order_id=order_id)
        with store.lock, store.connection:
            store.connection.execute(
                "UPDATE strategy_execution_attempts SET volume = 3 WHERE id = ?", (attempt_id,))
    _seed_pending_attempt(store, "attempt-no-id", bar_time="1767225600000000009", order_id=None)
    with store.lock, store.connection:
        store.connection.execute(
            "UPDATE strategy_execution_attempts SET volume = 3 WHERE id = 'attempt-no-id'")
    gateway = _GatewayReads({
        "order-alive": {"submitPhase": "CONFIRMED", "remoteStatus": "ALIVE",
                        "filledVolume": 0, "request": {"volume": 3}},
        "order-unknown": {"submitPhase": "UNKNOWN", "remoteStatus": "UNKNOWN",
                          "filledVolume": 0, "request": {"volume": 3}},
        "order-read-fail": TimeoutError("query lost"),
    })
    assert pending_open_lots(store, USER, LIVE_ID, gateway=gateway) == 12
    store.close()

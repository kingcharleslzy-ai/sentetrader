"""策略积木第六批 (2026-09-28): the live chain end to end.

The real monitor over the real proposals service (evaluate + live_position_state + decide) with
the fake gateway / provider: an add judged off a first-open-only protection row, a 分批止盈 tier
closing its true lot count from the peak, the position identity surviving the FIFO drain of the
earliest rows (also across a restart), the three 执行方式 firing an add and a tier once per bar,
and a partial close reducing the protection rows FIFO with the gross PnL on the right rows.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from test_simulation_orders import USER, Gateway, NormalProvider, application
from test_strategy_monitor import STRATEGY_ID
from trader_runtime.data_providers import fake_market_workbench
from trader_runtime.strategy_engine import MarketContext, decide
from trader_runtime.strategy_monitor import StrategyMonitorService
from trader_runtime.strategy_protection import live_position_state
from trader_runtime.strategy_rules import StrategyPlan, evaluate_plan
from trader_runtime.strategy_workbench import definition_for
from trader_runtime.trading_calendar import trading_day_of

SHFE = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
        "night": [["21:00:00", "25:00:00"]]}
ALWAYS = [{"label": "总是", "left": {"indicator": "close", "timeframe": 300}, "operator": "gt",
           "right": {"indicator": "constant", "value": 0}}]
NEVER = [{"label": "永不", "left": {"indicator": "close", "timeframe": 300}, "operator": "lt",
          "right": {"indicator": "constant", "value": 0}}]
DIGEST = "d" * 64
TICK = 0.5
MARKET = MarketContext(price_tick=TICK, volume_multiple=1000, equity=100_000)


def _plan(**fields: Any) -> StrategyPlan:
    plan = StrategyPlan.model_validate({
        "name": "第六批实盘", "description": "加仓与分批止盈实盘链路", "direction": "long",
        "timeframeSeconds": 300, "maxPosition": 2, "stopLossTicks": 2, "takeProfitTicks": None,
        "cooldownBars": 0,
        "rules": {"entryLong": {"conditions": ALWAYS}, "exitLong": {"conditions": NEVER}}, **fields})
    plan.check_executable()
    return plan


class Provider(NormalProvider):
    """The fake account with controllable completed bars: `walk_to(close)` appends one completed
    300s bar closing there (every workbench series ends with a still-forming bar, which is never
    a signal input, so each walk also extends the forming tail)."""

    def __init__(self) -> None:
        super().__init__()
        self.day = "2026-09-01"
        self.activity: dict[str, Any] = {"dataStatus": "normal", "isCached": False, "ageSeconds": 0,
                                         "ordersInitialized": True, "orders": [], "trades": []}
        self.position_items: list[dict[str, Any]] = []
        self.walked: list[float] = []

    def account_activity(self) -> dict[str, Any]:
        return self.activity

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        return {**super().account_summary(turn_id), "tradingDay": self.day}

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        workbench = fake_market_workbench(symbol, duration_seconds)
        bars = list(workbench["bars"])
        for close in self.walked:
            bars.append(_next_bar(bars[-1], close, duration_seconds))
        if self.walked:
            bars.append(_next_bar(bars[-1], self.walked[-1], duration_seconds))   # the forming tail
        workbench["bars"] = bars
        workbench["quote"]["tradingSchedule"] = SHFE
        return workbench

    def walk_to(self, close: float) -> None:
        self.walked.append(close)
        self.last = close


def _next_bar(previous: dict[str, Any], close: float, duration_seconds: int) -> dict[str, Any]:
    timestamp = int(previous["datetime"]) + duration_seconds * 1_000_000_000
    opening = round(close - 0.3, 2)
    return {"datetime": timestamp, "open": opening, "high": round(max(opening, close) + 0.1, 2),
            "low": round(min(opening, close) - 0.1, 2), "close": close, "volume": 100,
            "openInterest": previous["openInterest"], "tradingDay": trading_day_of(timestamp)}


def _position(volume: int) -> list[dict[str, Any]]:
    return [{"symbol": "SHFE.au2612", "direction": "long", "volume": volume, "todayVolume": volume,
             "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]


def _app(path, provider, gateway, plan: StrategyPlan, *, signal_action: str = "auto"):
    app = application(path, gateway, provider)
    store = app.state.store
    store.ensure_default_trading_account(execution_owner_user_id=USER, provider_kind="fake",
                                         display_name="假数据模拟账户", sidecar_url=None)
    store.save_strategy_draft(strategy_id=STRATEGY_ID, user_id=USER, source_session_id="session-batch6-live",
                              name="第六批实盘", symbol="SHFE.au2612", version=1, status="monitoring",
                              definition=definition_for(plan), definition_digest=DIGEST)
    store.save_strategy_validation_report(report_id="report-batch6-live", strategy_id=STRATEGY_ID,
                                          user_id=USER, strategy_digest=DIGEST, status="passed",
                                          checks=[], evidence={})
    session = app.state.broker.create_session("local", "native", USER)
    store.set_strategy_signal_action(STRATEGY_ID, user_id=USER, signal_action=signal_action,
                                     session_id=session.session_id)
    monitor = StrategyMonitorService(store=store, proposals=app.state.proposals, autonomy=app.state.autonomy)
    return app, monitor


def _restart(path, provider, gateway):
    app = application(path, gateway, provider)
    monitor = StrategyMonitorService(store=app.state.store, proposals=app.state.proposals,
                                     autonomy=app.state.autonomy)
    return app, monitor


async def _fill_order(app, provider, gateway, fills, *, request=None) -> dict[str, Any]:
    """Fill the last submitted order (one trade per entry of `fills`), settle it and sync."""
    order = dict(request or gateway.request)
    for trade_id, volume, price, when in fills:
        provider.activity["trades"].append(
            {"tradeId": trade_id, "clientOrderId": order["clientOrderId"], "symbol": order["symbol"],
             "direction": order["direction"], "offset": order["offset"], "volume": volume,
             "price": price, "tradeDateTime": when, "tradingDay": provider.day})
    provider.activity["orders"] = [row for row in provider.activity["orders"]
                                   if row.get("clientOrderId") != order["clientOrderId"]]
    provider.activity["orders"].append({**order, "status": "FINISHED", "volumeLeft": 0})
    gateway.query_report = gateway.report(order, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(order["clientOrderId"], user_id=USER)
    app.state.autonomy.sync_strategy_protection()
    return order


def _rows(app, *columns):
    return app.state.store.connection.execute(
        f"SELECT {', '.join(columns)} FROM ai_stop_orders ORDER BY created_at, id").fetchall()


def _live_state(app, provider, *, volume: int):
    strategy = app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    state = live_position_state(store=app.state.store, provider=provider, strategy=strategy,
                                symbol=strategy["symbol"], account=provider.account_summary(""))
    return replace(state, direction="long", volume=volume)


def _decide(app, provider, state):
    strategy = app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    evaluation = evaluate_plan(provider, strategy["definition"], "SHFE.au2612")
    return decide(StrategyPlan.model_validate(strategy["definition"]["plan"]), evaluation, state, MARKET)


def _count(app, name: str) -> int:
    return sum(1 for session in app.state.store.connection.execute("SELECT id FROM sessions").fetchall()
               for _, event in app.state.store.list_events(session[0]) if event.get("name") == name)


# -- (a) 加仓 judged off the first-open row ---------------------------------------------------------------


async def test_live_add_is_judged_from_the_first_open_row_and_joins_the_protection(tmp_path):
    provider, gateway = Provider(), Gateway()
    app, monitor = _app(tmp_path / "add.sqlite3", provider, gateway,
                        _plan(maxPosition=2, pyramiding={"maxAdds": 1, "trigger": "profit_ticks",
                                                         "step": 5, "volume": 1, "moveStop": True}))
    assert (await monitor.run_once())[0]["status"] == "submitted"    # 首笔给加仓留空间：只开 1 手
    assert gateway.request["volume"] == 1
    await _fill_order(app, provider, gateway, [("fill-1", 1, 822.0, 1)])
    provider.position_items = _position(1)

    # 只有一行首次开仓：聚合状态已带上加仓要读的字段（修复前全是 0 / None，第一次加仓永远不发生）。
    state = _live_state(app, provider, volume=1)
    assert (state.adds, state.initial_volume, state.peak_volume) == (0, 1, 1)
    assert state.last_entry_price == 822.0 and not state.scaled_out

    provider.walk_to(826.0)                                          # 822 + 5 跳 × 0.5 = 824.5 已达
    result = (await monitor.run_once())[0]
    assert result["status"] == "submitted", result
    snapshot = json.loads(app.state.store.connection.execute(
        "SELECT payload_json FROM strategy_intent_snapshots ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()[0])
    assert snapshot["signalSnapshot"].get("add") is True
    assert snapshot["positionIntent"]["requestedVolume"] == 1
    assert "加仓第 1 次" in snapshot["positionIntent"]["reason"]
    await _fill_order(app, provider, gateway, [("fill-2", 1, 826.0, 2)])
    provider.position_items = _position(2)

    rows = _rows(app, "trigger_price", "strategy_metadata_json")
    assert len(rows) == 2
    metas = [json.loads(row["strategy_metadata_json"]) for row in rows]
    assert [int(meta.get("addIndex") or 0) for meta in metas] == [0, 1]
    assert metas[1]["positionKey"] == metas[0]["positionKey"]          # 身份随行
    assert float(rows[0]["trigger_price"]) == 825.0                    # 原有行止损收紧到 826 − 2×0.5
    state = _live_state(app, provider, volume=2)
    assert (state.adds, state.initial_volume, state.peak_volume) == (1, 1, 2)
    assert state.entry_price == 824.0 and state.last_entry_price == 826.0

    provider.walk_to(828.0)                                           # 已到 maxAdds：不再加
    assert (await monitor.run_once())[0]["status"] == "waiting"
    assert gateway.submit_count == 2
    app.state.store.close()


# -- (b) 分批止盈 sizes the tier from the peak --------------------------------------------------------------


async def test_live_scale_out_sizes_the_tier_from_the_peak_not_the_remainder(tmp_path):
    provider, gateway = Provider(), Gateway()
    app, monitor = _app(tmp_path / "scale.sqlite3", provider, gateway,
                        _plan(maxPosition=3, scaleOut=[{"profitTicks": 20, "percent": 67}]))
    assert (await monitor.run_once())[0]["status"] == "submitted"
    assert gateway.request["volume"] == 3
    await _fill_order(app, provider, gateway, [("fill-1", 3, 822.0, 1)])
    provider.position_items = _position(3)

    state = _live_state(app, provider, volume=3)      # 只设分批止盈的计划同样读出峰值与最近成交
    assert (state.initial_volume, state.peak_volume, state.last_entry_price) == (3, 3, 822.0)
    assert not state.scaled_out

    provider.walk_to(832.0)                                           # 822 + 20 跳 × 0.5
    result = (await monitor.run_once())[0]
    assert result["status"] == "submitted", result
    assert (gateway.request["volume"], gateway.request["offset"]) == (2, "CLOSETODAY")   # ⌊3×67%⌋ = 2 手
    key = f"scale_out:0:{_rows(app, 'id')[0][0]}"
    await _fill_order(app, provider, gateway, [("fill-close", 2, 832.0, 2)])
    provider.position_items = _position(1)

    assert _rows(app, "remaining_volume")[0][0] == 1
    assert key in app.state.store.strategy_exit_keys(strategy_id=STRATEGY_ID, user_id=USER)
    state = _live_state(app, provider, volume=1)
    assert state.scaled_out and state.peak_volume == 3               # 峰值不随部分平仓缩水
    provider.walk_to(834.0)
    assert _decide(app, provider, state).action == "hold"            # 这档已触发，不再平
    assert (await monitor.run_once())[0]["status"] == "waiting"
    app.state.store.close()


# -- (c) identity and peak survive the FIFO drain, and a restart --------------------------------------------


async def test_live_identity_and_peak_survive_the_fifo_drain_and_a_restart(tmp_path):
    provider, gateway = Provider(), Gateway()
    database = tmp_path / "identity.sqlite3"
    app, monitor = _app(database, provider, gateway,
                        _plan(maxPosition=4, scaleOut=[{"profitTicks": 10, "percent": 25},
                                                       {"profitTicks": 20, "percent": 50}]))
    assert (await monitor.run_once())[0]["status"] == "submitted"
    # 一笔 4 手的首次开仓分两次成交：两行保护，同一身份（第二行抄第一行的 positionKey）。
    await _fill_order(app, provider, gateway, [("fill-1", 1, 822.0, 1), ("fill-2", 3, 822.0, 2)])
    provider.position_items = _position(4)
    rows = _rows(app, "id", "strategy_metadata_json")
    assert len(rows) == 2
    metas = [json.loads(row["strategy_metadata_json"]) for row in rows]
    assert [meta["positionKey"] for meta in metas] == [rows[0]["id"], rows[0]["id"]]
    state = _live_state(app, provider, volume=4)
    key, entry_time = state.position_key, state.entry_time
    assert (state.initial_volume, state.peak_volume) == (4, 4)

    provider.walk_to(827.0)                       # 第 1 档：822 + 10 跳 × 0.5，平 ⌊4×25%⌋ = 1 手
    assert (await monitor.run_once())[0]["status"] == "submitted"
    assert gateway.request["volume"] == 1
    await _fill_order(app, provider, gateway, [("fill-3", 1, 827.0, 3)])
    provider.position_items = _position(3)
    assert _rows(app, "remaining_volume")[0][0] == 0                 # 最早的一行已被先开先平平完
    state = _live_state(app, provider, volume=3)
    assert (state.position_key, state.entry_time) == (key, entry_time)   # 身份不随行平掉而变
    assert state.scaled_out and state.peak_volume == 4

    provider.walk_to(832.0)                       # 第 2 档按真实峰值 4 算：⌊4×50%⌋ = 2 手
    assert (await monitor.run_once())[0]["status"] == "submitted"
    assert gateway.request["volume"] == 2                          # 按剩余 3 手算只会平 1 手
    await _fill_order(app, provider, gateway, [("fill-4", 2, 832.0, 4)])
    provider.position_items = _position(1)
    provider.walk_to(834.0)
    state = _live_state(app, provider, volume=1)
    assert _decide(app, provider, state).action == "hold"            # 两档都已触发
    app.state.store.close()

    app, monitor = _restart(database, provider, gateway)             # 重启：同样成立
    state = _live_state(app, provider, volume=1)
    assert (state.position_key, state.entry_time) == (key, entry_time)
    assert state.scaled_out and (state.initial_volume, state.peak_volume) == (4, 4)
    fired = app.state.store.strategy_exit_keys(strategy_id=STRATEGY_ID, user_id=USER)
    assert {f"scale_out:0:{key}", f"scale_out:1:{key}"} <= fired
    provider.walk_to(836.0)
    assert _decide(app, provider, state).action == "hold"
    assert (await monitor.run_once())[0]["status"] == "waiting"
    app.state.store.close()


# -- (d) the three 执行方式: an add and a tier, once per bar ------------------------------------------------


def _switch_mode(app, signal_action: str) -> None:
    session = app.state.broker.create_session("local", "native", USER)
    app.state.store.set_strategy_signal_action(STRATEGY_ID, user_id=USER, signal_action=signal_action,
                                               session_id=session.session_id)


@pytest.mark.parametrize("signal_action", ["alert", "confirm", "auto"])
async def test_live_each_execution_mode_offers_the_add_once_per_bar(tmp_path, signal_action):
    provider, gateway = Provider(), Gateway()
    app, monitor = _app(tmp_path / f"add-{signal_action}.sqlite3", provider, gateway,
                        _plan(pyramiding={"maxAdds": 1, "trigger": "profit_ticks", "step": 5,
                                          "volume": 1, "moveStop": True}))
    assert (await monitor.run_once())[0]["status"] == "submitted"     # 首笔开仓（自动）建仓
    await _fill_order(app, provider, gateway, [("fill-1", 1, 822.0, 1)])
    provider.position_items = _position(1)
    _switch_mode(app, signal_action)

    provider.walk_to(826.0)
    first = (await monitor.run_once())[0]
    assert first["status"] == {"alert": "alerted", "confirm": "proposed", "auto": "submitted"}[signal_action], first
    store = app.state.store
    attempts = store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)
    assert [(item["action"], item["volume"]) for item in attempts] == [("open_long", 1)] * 2  # 首开 + 加仓
    if signal_action == "auto":
        assert gateway.submit_count == 2 and gateway.request["volume"] == 1
        await _fill_order(app, provider, gateway, [("fill-2", 1, 826.0, 2)])
        provider.position_items = _position(2)
        metas = [json.loads(row["strategy_metadata_json"]) for row in _rows(app, "strategy_metadata_json")]
        assert [int(meta.get("addIndex") or 0) for meta in metas] == [0, 1]
    else:
        assert gateway.submit_count == 1
    cards, submits = _count(app, "proposal.trade"), gateway.submit_count
    again = (await monitor.run_once())[0]                             # 同一根K线只处理一次
    assert again["status"] in {"waiting", "duplicate"} and _count(app, "proposal.trade") == cards
    assert gateway.submit_count == submits
    if signal_action == "confirm":
        assert cards == 1
    store.close()


@pytest.mark.parametrize("signal_action", ["alert", "confirm", "auto"])
async def test_live_each_execution_mode_fires_the_scale_out_tier_once_per_bar(tmp_path, signal_action):
    provider, gateway = Provider(), Gateway()
    app, monitor = _app(tmp_path / f"tier-{signal_action}.sqlite3", provider, gateway,
                        _plan(maxPosition=2, scaleOut=[{"profitTicks": 20, "percent": 50}]))
    assert (await monitor.run_once())[0]["status"] == "submitted"     # 首笔开仓（自动）建仓
    await _fill_order(app, provider, gateway, [("fill-1", 2, 822.0, 1)])
    provider.position_items = _position(2)
    key = f"scale_out:0:{_rows(app, 'id')[0][0]}"
    _switch_mode(app, signal_action)

    provider.walk_to(832.0)                                           # 822 + 20 跳 × 0.5
    first = (await monitor.run_once())[0]
    assert first["status"] == {"alert": "alerted", "confirm": "proposed", "auto": "submitted"}[signal_action], first
    store = app.state.store
    attempts = store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)
    assert [(item["action"], item["volume"]) for item in attempts] == [("close_long", 1), ("open_long", 2)]
    assert key in store.strategy_exit_keys(strategy_id=STRATEGY_ID, user_id=USER)   # 提醒 / 卡片也算已触发
    if signal_action == "auto":
        assert (gateway.request["volume"], gateway.request["offset"]) == (1, "CLOSETODAY")
        await _fill_order(app, provider, gateway, [("fill-close", 1, 832.0, 2)])
        provider.position_items = _position(1)
    cards, submits = _count(app, "proposal.trade"), gateway.submit_count
    again = (await monitor.run_once())[0]                             # 同一根K线只处理一次
    assert again["status"] in {"waiting", "duplicate"} and _count(app, "proposal.trade") == cards
    assert gateway.submit_count == submits
    if signal_action == "confirm":
        assert cards == 1
    provider.walk_to(834.0)                                           # 这档已消费，价格再走也不再触发
    assert _decide(app, provider, _live_state(app, provider, volume=2 if signal_action != "auto" else 1)
                   ).action == "hold"
    assert (await monitor.run_once())[0]["status"] == "waiting"
    store.close()


# -- (e) a partial close reduces the rows FIFO with the gross PnL on the right rows --------------------------


async def test_live_partial_close_drains_rows_fifo_and_attributes_the_gross_pnl(tmp_path):
    provider, gateway = Provider(), Gateway()
    app, monitor = _app(tmp_path / "fifo.sqlite3", provider, gateway,
                        _plan(maxPosition=3, scaleOut=[{"profitTicks": 16, "percent": 67}]))
    assert (await monitor.run_once())[0]["status"] == "submitted"
    await _fill_order(app, provider, gateway, [("fill-1", 1, 822.0, 1), ("fill-2", 2, 822.5, 2)])
    provider.position_items = _position(3)

    provider.walk_to(831.0)                     # 均价 822.33 + 16 跳 × 0.5 ≈ 830.33 已达，平 ⌊3×67%⌋ = 2 手
    assert (await monitor.run_once())[0]["status"] == "submitted"
    assert gateway.request["volume"] == 2
    await _fill_order(app, provider, gateway, [("fill-3", 2, 831.0, 3)])
    provider.position_items = _position(1)

    rows = _rows(app, "remaining_volume", "status", "strategy_metadata_json")
    assert (rows[0]["remaining_volume"], rows[0]["status"]) == (0, "CANCELLED")   # 先开先平：最早一行平完
    assert rows[1]["remaining_volume"] == 1
    # 毛盈亏按各行的开仓价归属：1 手 × (831−822) + 1 手 × (831−822.5)，乘数 1000。
    event = json.loads(app.state.store.connection.execute(
        """SELECT payload_json FROM strategy_fill_events
        WHERE json_extract(payload_json, '$.trade.tradeId') = 'fill-3'""").fetchone()[0])
    assert float(event["realizedGrossPnl"][STRATEGY_ID]) == pytest.approx(17500.0)
    state = _live_state(app, provider, volume=1)
    assert state.position_key == json.loads(rows[0]["strategy_metadata_json"])["positionKey"]
    assert state.entry_price == pytest.approx((822.0 * 1 + 822.5 * 2) / 3)
    assert state.scaled_out and (state.initial_volume, state.peak_volume) == (3, 3)
    app.state.store.close()

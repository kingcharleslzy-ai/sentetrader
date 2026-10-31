"""策略积木第六批 (2026-09-28): 加仓 (pyramiding) 与 分批止盈 (scale-out).

Engine triggers / caps / gates / validation, the scale-out math (keep one lot, once per tier),
the multi-fill backtest sequence and its bar-by-bar parity with the live judgement, and the
live chain: the add's protection row (addIndex / moveStop), the aggregated live position state,
the autonomy gates for adds, and a scale-out tier that stays fired across a restart.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from test_strategy_monitor import STRATEGY_ID
from trader_runtime.autonomy import AutonomyError
from trader_runtime.strategy_backtest import LIVE_WINDOW, BacktestSetup, run_backtest
from trader_runtime.strategy_engine import (
    MarketContext,
    PositionState,
    decide,
    open_position,
    partial_after_close,
    step,
)
from trader_runtime.strategy_monitor import StrategyMonitorService
from trader_runtime.strategy_protection import live_position_state
from trader_runtime.strategy_rules import StrategyPlan, evaluate_rules
from trader_runtime.trading_calendar import trading_day_of

CST = timezone(timedelta(hours=8))
SHFE = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
        "night": [["21:00:00", "25:00:00"]]}
MARKET = MarketContext(price_tick=1, volume_multiple=10, equity=100_000)


def ns(year, month, day, hour=0, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=CST).timestamp() * 1_000_000_000)


def ohlc(rows, start=0, step_seconds=300):
    return [{"datetime": start + i * step_seconds * 1_000_000_000, "open": o, "high": h, "low": low, "close": c,
             "volume": 100} for i, (o, h, low, c) in enumerate(rows)]


def _cond(label, left, operator, right, timeframe=300, **left_params):
    return {"label": label, "left": {"indicator": left, "timeframe": timeframe, **left_params},
            "operator": operator, "right": {"timeframe": timeframe, **right}}


def _plan(entry=None, exit_=None, **fields):
    rules = {"entryLong": {"conditions": entry or [_cond("总是", "close", "gt", {"indicator": "constant", "value": 0})]}}
    if exit_:
        rules["exitLong"] = {"conditions": exit_}
    plan = StrategyPlan.model_validate({"name": "积木", "description": "积木测试", "direction": "long",
                                        "timeframeSeconds": 300, "maxPosition": 3, "stopLossTicks": 10,
                                        "takeProfitTicks": None, "cooldownBars": 0, "rules": rules, **fields})
    plan.check_executable()
    return plan


def _held(**fields):
    base = dict(direction="long", volume=1, entry_price=100.0, entry_time=ns(2026, 9, 23, 9, 0),
                highest=100.0, lowest=100.0, bars_held=5, stop_price=90.0, trading_day="2026-09-23",
                position_key="p1", initial_volume=1, peak_volume=1, last_entry_price=100.0)
    base.update(fields)
    return PositionState(**base)


def _evaluation(close, *, entry_long=True, exit_long=False, atr=None, schedule=None,
                bar_close=None, bar_time=None):
    """A hand-built bar-close evaluation: rules are group booleans, engine the bar facts decide reads."""
    return {"rules": {"entryLong": entry_long, "exitLong": exit_long, "entryShort": False, "exitShort": False},
            "engine": {"barTime": str(bar_time or ns(2026, 9, 23, 10, 0)),
                       "barClose": bar_close or ns(2026, 9, 23, 14, 0),
                       "tradingDay": "2026-09-23", "close": close, "schedule": schedule,
                       "atr": dict(atr or {})}}


# -- 校验 ------------------------------------------------------------------------------------------------


def test_pyramiding_and_scale_out_validation():
    pyramid = {"maxAdds": 3, "trigger": "profit_ticks", "step": 5, "volume": 1, "moveStop": True}
    _plan(pyramiding=pyramid)   # 本身合法
    _plan(scaleOut=[{"profitTicks": 20, "percent": 50}, {"profitTicks": 40, "percent": 25}])
    with pytest.raises(ValueError, match="加仓暂不和保本/追踪止损/分段止盈一起用"):
        _plan(pyramiding=pyramid, breakeven={"triggerTicks": 10, "offsetTicks": 0})
    with pytest.raises(ValueError, match="加仓暂不和保本/追踪止损/分段止盈一起用"):
        _plan(pyramiding=pyramid, trailingStop={"mode": "percent", "value": 1})
    with pytest.raises(ValueError, match="加仓暂不和保本/追踪止损/分段止盈一起用"):
        _plan(pyramiding=pyramid, timeTakeProfit=[{"afterBars": 5, "profitTicks": 20}])
    with pytest.raises(ValueError, match="加仓须最大持仓至少 2 手"):
        _plan(maxPosition=1, pyramiding=pyramid)
    with pytest.raises(ValueError, match="分批止盈各档须同用跳数或同用 ATR 倍数"):
        _plan(scaleOut=[{"profitTicks": 20, "percent": 50}, {"profitAtr": 2, "percent": 25}])
    with pytest.raises(ValueError, match="分批止盈各档目标须由近到远递增"):
        _plan(scaleOut=[{"profitTicks": 40, "percent": 50}, {"profitTicks": 20, "percent": 25}])


# -- 引擎：加仓 ------------------------------------------------------------------------------------------


def test_first_open_reserves_room_for_adds():
    reserved = _plan(maxPosition=4, pyramiding={"maxAdds": 3, "trigger": "profit_ticks", "step": 5, "volume": 1})
    assert decide(reserved, _evaluation(101.0), PositionState(), MARKET).volume == 1
    plain = _plan(maxPosition=4)
    assert decide(plain, _evaluation(101.0), PositionState(), MARKET).volume == 4
    # 按 ATR 定出的手数同样被预留空间裁小（100000×1% ÷ (10×2×20) = 2.5 → 2 手，预留后只剩 1）。
    sized = {"mode": "atr_risk", "riskPercent": 1}
    evaluation = _evaluation(101.0, atr={"14": 20})
    assert decide(_plan(maxPosition=4, sizing=sized, atrStop={"multiple": 2}), evaluation,
                  PositionState(), MARKET).volume == 2
    assert decide(_plan(maxPosition=4, sizing=sized, atrStop={"multiple": 2},
                        pyramiding={"maxAdds": 3, "trigger": "profit_ticks", "step": 5, "volume": 1}),
                  evaluation, PositionState(), MARKET).volume == 1


def test_add_triggers():
    held = _held()
    atr_plan = _plan(pyramiding={"maxAdds": 3, "trigger": "profit_atr", "step": 0.5, "atrPeriod": 20, "volume": 1})
    decision = decide(atr_plan, _evaluation(102.0, atr={"20": 4.0}), held, MARKET)
    assert decision.action == "open_long" and decision.add and decision.volume == 1
    assert decision.reason == "加仓第 1 次：收盘 102.0 比上次成交 100 高出 0.5 倍 ATR20，加 1 手。"
    assert decide(atr_plan, _evaluation(101.9, atr={"20": 4.0}), held, MARKET).action == "hold"
    assert decide(atr_plan, _evaluation(102.0, atr={}), held, MARKET).action == "hold"   # 无 ATR 不加
    tick_plan = _plan(pyramiding={"maxAdds": 2, "trigger": "profit_ticks", "step": 5, "volume": 1})
    assert decide(tick_plan, _evaluation(105.0), held, MARKET).add is True
    assert decide(tick_plan, _evaluation(104.9), held, MARKET).add is False
    signal_plan = _plan(pyramiding={"maxAdds": 1, "trigger": "signal", "volume": 1})
    assert decide(signal_plan, _evaluation(103.0, entry_long=True), held, MARKET).add is True
    assert decide(signal_plan, _evaluation(103.0, entry_long=False), held, MARKET).action == "hold"
    for evaluation in (_evaluation(99.0), _evaluation(99.0, atr={"20": 4.0})):
        assert decide(tick_plan, evaluation, held, MARKET).add is False   # 亏了摊平不支持


def test_open_position_merges_the_add():
    plan = _plan(takeProfitTicks=20,
                 pyramiding={"maxAdds": 3, "trigger": "profit_ticks", "step": 5, "volume": 1, "moveStop": True})
    held = _held(stop_price=90.0, take_profit_price=120.0)
    decision = decide(plan, _evaluation(105.0), held, MARKET)
    assert decision.add and decision.volume == 1
    merged = open_position(plan, decision, fill_price=103.0, fill_time=ns(2026, 9, 23, 10, 0),
                           price_tick=1, previous=held)
    assert (merged.volume, merged.adds, merged.peak_volume) == (2, 1, 2)
    assert merged.entry_price == 101.5 and merged.last_entry_price == 103.0
    assert merged.entry_time == held.entry_time and merged.position_key == "p1"
    assert merged.stop_price == 93.0 and merged.take_profit_price == 123.0   # 只收紧 / 按新成交重算
    frozen = _plan(takeProfitTicks=20,
                   pyramiding={"maxAdds": 3, "trigger": "profit_ticks", "step": 5, "volume": 1, "moveStop": False})
    kept = open_position(frozen, decision, fill_price=103.0, fill_time=ns(2026, 9, 23, 10, 0),
                         price_tick=1, previous=held)
    assert (kept.stop_price, kept.take_profit_price, kept.volume) == (90.0, 120.0, 2)


def test_add_caps_and_gates():
    plan = _plan(pyramiding={"maxAdds": 3, "trigger": "profit_ticks", "step": 5, "volume": 1})
    assert decide(plan, _evaluation(120.0), _held(adds=3), MARKET).action == "hold"      # 加满
    assert decide(plan, _evaluation(120.0), _held(volume=3, initial_volume=3, peak_volume=3),
                  MARKET).action == "hold"                                              # 到最大持仓
    assert decide(plan, _evaluation(120.0), _held(scaled_out=True), MARKET).action == "hold"   # 分批后不加
    windowed = _plan(entryWindows=[{"start": "10:00", "end": "11:00"}],
                     pyramiding={"maxAdds": 2, "trigger": "profit_ticks", "step": 5, "volume": 1})
    blocked = decide(windowed, _evaluation(105.0, schedule=SHFE), _held(), MARKET)      # 14:00 不在窗口
    assert blocked.action == "hold" and not blocked.add
    ending = _plan(closeBeforeEndMinutes=5,
                   pyramiding={"maxAdds": 2, "trigger": "profit_ticks", "step": 5, "volume": 1})
    due = decide(ending, _evaluation(105.0, schedule=SHFE), _held(), replace(MARKET, now=ns(2026, 9, 23, 14, 57)))
    assert (due.action, due.exitReason, due.add) == ("close_long", "close_before_end", False)
    exiting = _plan(exit_=[_cond("离场", "close", "gt", {"indicator": "constant", "value": 104})],
                    pyramiding={"maxAdds": 2, "trigger": "profit_ticks", "step": 5, "volume": 1})
    rule = decide(exiting, _evaluation(105.0, exit_long=True), _held(), MARKET)          # 规则出场优先
    assert (rule.action, rule.exitReason, rule.add) == ("close_long", "rule", False)
    both = _plan(pyramiding={"maxAdds": 2, "trigger": "profit_ticks", "step": 5, "volume": 1},
                 scaleOut=[{"profitTicks": 20, "percent": 50}])
    tiered = decide(both, _evaluation(125.0), _held(volume=2, initial_volume=2, peak_volume=2), MARKET)
    assert (tiered.action, tiered.volume, tiered.exitReason, tiered.exitKey, tiered.add) == \
        ("close_long", 1, "scale_out", "scale_out:0:p1", False)                          # 分批止盈优先于加仓


# -- 引擎：分批止盈 --------------------------------------------------------------------------------------


def test_scale_out_math_keep_one_lot_and_once():
    plan = _plan(scaleOut=[{"profitTicks": 20, "percent": 50}, {"profitTicks": 40, "percent": 50}])
    held = _held(volume=4, initial_volume=4, peak_volume=4)
    first = decide(plan, _evaluation(120.0), held, MARKET)
    assert (first.action, first.volume, first.exitReason, first.exitKey) == \
        ("close_long", 2, "scale_out", "scale_out:0:p1")        # floor(4×50%) = 2
    after = partial_after_close(held, 2, first.exitKey)
    assert after.volume == 2 and after.scaled_out and first.exitKey in after.fired_exits
    assert decide(plan, _evaluation(120.0), after, MARKET).action == "hold"   # 同一档只触发一次
    second = decide(plan, _evaluation(140.0), after, MARKET)
    assert (second.volume, second.exitKey) == (1, "scale_out:1:p1")           # 只平得动 1 手，留 1 手
    one = partial_after_close(_held(volume=2, initial_volume=2, peak_volume=2), 1, "scale_out:0:p1")
    assert one.volume == 1
    skipped = decide(plan, _evaluation(140.0), one, MARKET)                   # 只剩 1 手：该档作废
    assert skipped.action == "hold" and skipped.exitKey == "scale_out:1:p1"
    assert "只剩 1 手" in skipped.reason
    # ATR 档按开仓时冻结的 exit_atr；判定对持仓均价而非最近一次成交。
    atr_plan = _plan(scaleOut=[{"profitAtr": 2.0, "percent": 50}])
    assert decide(atr_plan, _evaluation(106.0), _held(volume=2, initial_volume=2, peak_volume=2,
                                                      exit_atr=3.0), MARKET).volume == 1
    average = _held(volume=2, initial_volume=2, peak_volume=2, entry_price=101.5, last_entry_price=103.0)
    assert decide(_plan(scaleOut=[{"profitTicks": 20, "percent": 50}]),
                  _evaluation(121.6), average, MARKET).action == "close_long"   # 121.6−101.5 = 20.1 ≥ 20
    assert decide(_plan(scaleOut=[{"profitTicks": 20, "percent": 50}]),
                  _evaluation(121.4), average, MARKET).action == "hold"         # 离均价还差 0.1


def test_step_records_the_skipped_tier_as_fired():
    plan = _plan(scaleOut=[{"profitTicks": 20, "percent": 50}, {"profitTicks": 40, "percent": 50}])
    bars = [{**bar, "tradingDay": trading_day_of(bar["datetime"])}
            for bar in ohlc([(100, 101, 99, 100)] * 3 + [(125, 145, 124, 140)], ns(2026, 9, 23, 9, 0))]
    state = PositionState(direction="long", volume=1, entry_price=100.0, entry_time=bars[0]["datetime"],
                          highest=140.0, lowest=99.0, bars_held=3, stop_price=80.0, trading_day="2026-09-23",
                          position_key="p1", initial_volume=2, peak_volume=2, last_entry_price=100.0,
                          fired_exits=frozenset({"scale_out:0:p1"}))
    decision = step(plan, {300: bars}, state, MARKET, schedule=SHFE)
    assert decision.action == "hold" and decision.exitKey == "scale_out:1:p1"
    assert decision.state is not None
    assert decision.state.fired_exits == frozenset({"scale_out:0:p1", "scale_out:1:p1"})
    assert decision.state.scaled_out


# -- 回测：一笔持仓多次成交 ------------------------------------------------------------------------------


FEES = {"source": "custom", "default": {"mode": "per_lot", "open": 2, "closeToday": 3, "closeYesterday": 3},
        "overrides": []}
NO_FEES = {"source": "account", "default": {"mode": "per_lot", "open": 0, "closeToday": 0, "closeYesterday": 0},
           "overrides": []}
# 12 根：k2 开 1 手（k3 开盘 101），k3/k4 各加 1 手（106 / 111），k7 触第 1 档（k8 开盘 126 平 1 手），
# k9 触第 2 档（k10 开盘 146 平 1 手），k11 跌破随加仓收紧的止损 96（96 保护性平 1 手）。
ROWS = [(99, 100, 98, 99), (99, 100, 98, 99), (99, 102, 98, 101), (101, 103, 100, 106), (106, 108, 105, 111),
        (111, 113, 110, 116), (116, 118, 115, 121), (121, 123, 120, 126), (126, 128, 125, 127),
        (127, 129, 126, 146), (146, 148, 145, 147), (147, 148, 95, 95)]


def _bars(rows, start=None, step=300):
    start = ns(2026, 9, 23, 9, 0) if start is None else start
    result = []
    for i, (o, h, low, c) in enumerate(rows):
        timestamp = start + i * step * 1_000_000_000
        result.append({"datetime": timestamp, "open": o, "high": h, "low": low, "close": c, "volume": 100,
                       "tradingDay": datetime.fromtimestamp(timestamp / 1e9, tz=CST).date().isoformat()})
    return result


def _backtest_plan():
    return _plan(entry=[_cond("突破", "close", "gt", {"indicator": "constant", "value": 100})],
                 maxPosition=3, stopLossTicks=15,
                 pyramiding={"maxAdds": 2, "trigger": "profit_ticks", "step": 5, "volume": 1},
                 scaleOut=[{"profitTicks": 20, "percent": 50}, {"profitTicks": 40, "percent": 50}])


def _run(plan, bars, fees=NO_FEES):
    return run_backtest(BacktestSetup(plan=plan, symbol="SHFE.rb2610", series={300: bars}, price_tick=1,
                                      volume_multiple=10, fee_schedule=fees, initial_balance=100_000))


def test_backtest_sequence_multi_fill_position_and_scale_out_rows():
    result = _run(_backtest_plan(), _bars(ROWS))
    trades = result["trades"]
    assert [t["exitReason"] for t in trades] == ["scale_out", "scale_out", "stop_loss"]
    assert [t["volume"] for t in trades] == [1, 1, 1]
    assert [t["partial"] for t in trades] == [True, True, False]
    assert all(t["adds"] == 2 for t in trades)
    assert all(t["openPrice"] == 106.0 for t in trades)          # 三次成交的加权平均：(101+106+111)/3
    assert all(t["openTime"] == _bars(ROWS)[3]["datetime"] for t in trades)
    assert [t["closePrice"] for t in trades] == [126.0, 146.0, 96.0]
    assert [t["net"] for t in trades] == [200.0, 400.0, -100.0]
    assert result["summary"]["netProfit"] == 500.0
    scale_row = next(item for item in result["exitReasons"] if item["reason"] == "scale_out")
    assert (scale_row["label"], scale_row["count"], scale_row["net"]) == ("分批止盈", 2, 600.0)


def test_backtest_fees_are_prorated_per_lot_through_the_fills():
    result = _run(_backtest_plan(), _bars(ROWS), fees=FEES)
    assert [t["fees"] for t in result["trades"]] == [5.0, 5.0, 5.0]   # 每手：开 2 + 平今 3（FIFO 分摊）
    assert [t["net"] for t in result["trades"]] == [195.0, 395.0, -105.0]
    assert result["summary"]["netProfit"] == 485.0 and result["summary"]["fees"] == 15.0
    scale_row = next(item for item in result["exitReasons"] if item["reason"] == "scale_out")
    assert scale_row["net"] == 590.0


def test_backtest_and_live_judge_adds_and_scale_outs_the_same():
    """同一段K线：回测每根的判定 == 实盘 evaluate_rules（带正在形成的K线）+ decide 的判定。"""
    plan, bars = _backtest_plan(), _bars(ROWS)
    seen = []
    result = run_backtest(BacktestSetup(plan=plan, symbol="SHFE.rb2610", series={300: bars}, price_tick=1,
                                        volume_multiple=10, fee_schedule=NO_FEES, initial_balance=100_000),
                          trace=lambda k, state, decision: seen.append((k, state, decision)))
    assert len(result["trades"]) == 3
    assert any(decision.add for _, _, decision in seen)
    assert any(decision.exitReason == "scale_out" for _, _, decision in seen)
    judged = 0
    for k, state, decision in seen:
        if decision.fillPrice is not None:
            continue   # 保护性触发在K线内成交，实盘由已挂保护逐笔执行
        cutoff = bars[k]["datetime"] + 300 * 1_000_000_000
        forming = {**bars[k], "datetime": cutoff}
        workbenches = {300: {"bars": bars[max(0, k + 1 - LIVE_WINDOW):k + 1] + [forming], "quote": {}}}
        evaluation = evaluate_rules(plan, workbenches)
        equity = 100_000 + sum(t["net"] for t in result["trades"] if t["closeTime"] <= bars[k]["datetime"])
        live = decide(plan, evaluation, state, MarketContext(price_tick=1, volume_multiple=10, equity=equity))
        assert (live.action, live.volume, live.exitReason, live.exitKey, live.add) == \
            (decision.action, decision.volume, decision.exitReason, decision.exitKey, decision.add), k
        judged += 1
    assert judged >= 10


# -- 实盘：保护行、聚合状态、自主执行护栏、重启 ------------------------------------------------------------

BAR1, BAR2, BAR3 = "1767225600000000000", "1767225900000000000", "1767226200000000000"
PYRAMID = {"maxAdds": 1, "trigger": "profit_ticks", "step": 5, "volume": 1, "moveStop": True}


def _definition(risk_limits=None, **plan_fields):
    plan = {"name": "第六批", "description": "加仓与分批止盈", "direction": "long", "timeframeSeconds": 300,
            "maxPosition": 4, "stopLossTicks": 2, "takeProfitTicks": None, "cooldownBars": 0,
            "rules": {"entryLong": {"conditions": [_cond("总是", "close", "gt", {"indicator": "constant", "value": 0})]}},
            **plan_fields}
    return {"timeframeSeconds": 300,
            "riskLimits": {"maxPosition": 4, "maxOrdersPerDay": 20, "stopLossTicks": 2,
                           "takeProfitTicks": 4, "cooldownBars": 0, **(risk_limits or {})},
            "plan": plan}


def _save(store, definition):
    from test_simulation_orders import USER
    store.ensure_default_trading_account(execution_owner_user_id=USER, provider_kind="fake",
                                         display_name="假数据模拟账户", sidecar_url=None)
    return store.save_strategy_draft(strategy_id=STRATEGY_ID, user_id=USER,
                                     source_session_id="session-batch6", name="第六批积木", symbol="SHFE.au2612",
                                     version=1, status="monitoring", definition=definition,
                                     definition_digest="c" * 64)


def _intent_at(store, bar, *, action="open_long", volume=1, add=False, exit_key=None, exit_reason=None):
    from test_simulation_orders import USER
    snapshot = {"signal": "sell" if action.startswith(("close", "open_short")) else "buy", "barTime": bar}
    if add:
        snapshot["add"] = 1
    if exit_key is not None:
        snapshot["exitKey"] = exit_key
    if exit_reason is not None:
        snapshot["exitReason"] = exit_reason
    intent_id = f"intent-batch6-{bar}{'-add' if add else '-exit' if exit_key else ''}"
    payload = {"intentId": intent_id, "strategyId": STRATEGY_ID, "signalSnapshot": snapshot,
               "positionIntent": {"action": action, "requestedVolume": volume, "reason": "第六批测试。"},
               "riskAdjustedIntent": {"status": "allowed", "action": action, "volume": volume,
                                      "reason": "第六批测试。"}}
    store.save_strategy_intent_snapshot(intent_id=intent_id, strategy_id=STRATEGY_ID, user_id=USER,
                                        strategy_digest="c" * 64, signal_bar_time=bar,
                                        context_digest=f"batch6-{intent_id}",   # 每条意图唯一，INSERT OR IGNORE 才不吞行
                                        payload=payload)
    return payload


def _position_items(symbol, *, volume, today=None):
    return [{"symbol": symbol, "direction": "long", "volume": volume, "todayVolume": today or volume,
             "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]


def _rows(app, *columns):
    return app.state.store.connection.execute(
        f"SELECT {', '.join(columns)} FROM ai_stop_orders ORDER BY id").fetchall()


async def test_add_fill_joins_the_protection_rows_and_the_engine_state(tmp_path):
    from test_simulation_orders import USER, Gateway, application
    from test_strategy_monitor import ProposalStub
    from test_strategy_protection import Provider, entry_fill

    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / "batch6.sqlite3", gateway, provider)
    _save(app.state.store, _definition(pyramiding=PYRAMID))
    monitor = StrategyMonitorService(store=app.state.store,
                                     proposals=ProposalStub(app.state.store, _intent_at(app.state.store, BAR1)),
                                     autonomy=app.state.autonomy)
    assert (await monitor.run_once())[0]["status"] == "submitted"
    first_request = dict(gateway.request)
    entry_fill(provider, first_request)
    provider.position_items = _position_items(first_request["symbol"], volume=1)
    provider.activity["orders"] = [{**first_request, "status": "FINISHED", "volumeLeft": 0}]
    gateway.query_report = gateway.report(first_request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(first_request["clientOrderId"], user_id=USER)
    app.state.autonomy.sync_strategy_protection()
    first_rows = _rows(app, "trigger_price", "take_profit_price")
    assert len(first_rows) == 1
    assert (float(first_rows[0]["trigger_price"]), float(first_rows[0]["take_profit_price"])) == (821.0, 824.0)

    monitor.proposals.intent = _intent_at(app.state.store, BAR2, add=True)   # 第二根：加仓
    assert (await monitor.run_once())[0]["status"] == "submitted"
    add_request = dict(gateway.request)
    assert add_request["clientOrderId"] != first_request["clientOrderId"]
    provider.position_items[0].update(volume=2, todayVolume=2)
    entry_fill(provider, add_request, trade_id="fill-2", price=824.0, time=2)
    provider.activity["orders"] = [{**first_request, "status": "FINISHED", "volumeLeft": 0},
                                   {**add_request, "status": "FINISHED", "volumeLeft": 0}]
    gateway.query_report = gateway.report(add_request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(add_request["clientOrderId"], user_id=USER)
    app.state.autonomy.sync_strategy_protection()
    rows = sorted(_rows(app, "id", "trigger_price", "take_profit_price", "strategy_metadata_json"),
                  key=lambda row: int(json.loads(row["strategy_metadata_json"]).get("addIndex") or 0))
    assert len(rows) == 2
    metas = [json.loads(row["strategy_metadata_json"]) for row in rows]
    assert [int(meta.get("addIndex") or 0) for meta in metas] == [0, 1]
    assert metas[1]["entryPrice"] == "824.0"
    assert (float(rows[1]["trigger_price"]), float(rows[1]["take_profit_price"])) == (823.0, 826.0)
    assert (float(rows[0]["trigger_price"]), float(rows[0]["take_profit_price"])) == (823.0, 826.0)  # 只收紧
    audit = json.loads(app.state.store.connection.execute(
        "SELECT result_json FROM proposal_audit WHERE action = 'strategy-protection-pyramid'").fetchone()[0])
    assert (audit["addIndex"], audit["tightenedRows"]) == (1, 1)

    strategy = app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    state = live_position_state(store=app.state.store, provider=provider, strategy=strategy,
                                symbol=strategy["symbol"], account=provider.account_summary(""))
    assert (state.adds, state.initial_volume, state.peak_volume) == (1, 1, 2)
    assert state.entry_price == 823.0 and state.last_entry_price == 824.0
    assert state.stop_price == 823.0 and state.take_profit_price == 826.0
    assert state.position_key == str(rows[0]["id"]) and not state.scaled_out

    monitor.proposals.intent = _intent_at(app.state.store, BAR3, add=True)   # 第三根：超过 maxAdds
    result = (await monitor.run_once())[0]
    assert result["status"] == "failed" and "strategy_add_limit_reached" in result["detail"]
    assert gateway.submit_count == 2
    app.state.store.close()


async def test_adds_require_a_pyramiding_plan_and_plain_reopens_stay_blocked(tmp_path):
    from test_simulation_orders import USER
    from test_strategy_protection import filled_entry

    app, _, _, _, request = await filled_entry(tmp_path)
    kwargs = dict(user_id=USER, session_id="s", strategy_id=STRATEGY_ID, symbol=request["symbol"],
                  side="buy_open", volume=1, position_bucket=None,
                  authorization={"authorizationId": "auth-1"}, strategy_bar_time=None)
    with pytest.raises(AutonomyError, match="strategy_add_not_enabled"):
        app.state.autonomy._strategy_protection(**kwargs, strategy_add=True)
    with pytest.raises(AutonomyError, match="strategy_position_already_protected"):
        app.state.autonomy._strategy_protection(**kwargs, strategy_add=False)
    app.state.store.close()


async def test_a_manual_or_other_strategys_protected_lot_does_not_block_a_fresh_open(tmp_path):
    """10-09 night: a manual 沪金 lot with its own stop refused the strategy's entry
    (strategy_position_already_protected). Only the strategy's own protected position blocks."""
    from test_simulation_orders import USER
    from test_strategy_protection import _extra_protection_row, filled_entry

    app, provider, _, _, request = await filled_entry(tmp_path)
    store = app.state.store
    # The strategy's own lot is gone (closed); a manual stop and another strategy's row remain.
    store.connection.execute("UPDATE ai_stop_orders SET remaining_volume = 0, status = 'CANCELLED'")
    store.connection.commit()
    _extra_protection_row(store, row_id="manual-stop", strategy_id=None, day=provider.day)
    _extra_protection_row(store, row_id="other-strategy", strategy_id="other-strategy-id", day=provider.day)
    kwargs = dict(user_id=USER, session_id="s", strategy_id=STRATEGY_ID, symbol=request["symbol"],
                  side="buy_open", volume=1, position_bucket=None,
                  authorization={"authorizationId": "auth-1"}, strategy_bar_time=None)
    protection = app.state.autonomy._strategy_protection(**kwargs, strategy_add=False)
    assert protection is not None and protection["strategyId"] == STRATEGY_ID
    app.state.store.close()


async def test_scale_out_exit_keys_survive_a_restart_and_block_the_tier(tmp_path):
    from test_simulation_orders import USER, Gateway, application
    from test_strategy_monitor import ProposalStub
    from test_strategy_protection import Provider, entry_fill

    definition = _definition(risk_limits={"maxPosition": 2}, maxPosition=2,
                             scaleOut=[{"profitTicks": 20, "percent": 50}])
    plan = StrategyPlan.model_validate(definition["plan"])
    provider, gateway = Provider(), Gateway()
    database = tmp_path / "batch6-scale.sqlite3"
    app = application(database, gateway, provider)
    _save(app.state.store, definition)
    monitor = StrategyMonitorService(store=app.state.store,
                                     proposals=ProposalStub(app.state.store,
                                                            _intent_at(app.state.store, BAR1, volume=2)),
                                     autonomy=app.state.autonomy)
    assert (await monitor.run_once())[0]["status"] == "submitted"
    request = dict(gateway.request)
    entry_fill(provider, request, volume=2)
    provider.position_items = _position_items(request["symbol"], volume=2)
    provider.activity["orders"] = [{**request, "status": "FINISHED", "volumeLeft": 0}]
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(request["clientOrderId"], user_id=USER)
    app.state.autonomy.sync_strategy_protection()
    stop_id = _rows(app, "id")[0][0]
    key = f"scale_out:0:{stop_id}"
    assert key not in app.state.store.strategy_exit_keys(strategy_id=STRATEGY_ID, user_id=USER)

    def live_state(application_instance, volume):
        strategy = application_instance.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
        return replace(live_position_state(store=application_instance.state.store, provider=provider,
                                           strategy=strategy, symbol=strategy["symbol"],
                                           account=provider.account_summary("")),
                       direction="long", volume=volume)

    market = MarketContext(price_tick=0.5, volume_multiple=10, equity=100_000)
    evaluation = _evaluation(833.0)   # 均价 822 + 20 跳 × 0.5 = 832 已达
    decision = decide(plan, evaluation, live_state(app, 2), market)
    assert (decision.action, decision.volume, decision.exitReason, decision.exitKey) == \
        ("close_long", 1, "scale_out", key)

    monitor.proposals.intent = _intent_at(app.state.store, BAR2, action="close_long", volume=1,
                                          exit_key=key, exit_reason="scale_out")
    assert (await monitor.run_once())[0]["status"] == "submitted"
    assert gateway.request["offset"] == "CLOSETODAY"
    close_request = dict(gateway.request)
    provider.position_items[0].update(volume=1, todayVolume=1)
    entry_fill(provider, close_request, trade_id="fill-close", volume=1, price=833.0, time=3)
    provider.activity["orders"] = [{**request, "status": "FINISHED", "volumeLeft": 0},
                                   {**close_request, "status": "FINISHED", "volumeLeft": 0}]
    gateway.query_report = gateway.report(close_request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(close_request["clientOrderId"], user_id=USER)
    app.state.autonomy.sync_strategy_protection()
    assert _rows(app, "remaining_volume")[0][0] == 1
    assert key in app.state.store.strategy_exit_keys(strategy_id=STRATEGY_ID, user_id=USER)
    assert decide(plan, evaluation, live_state(app, 1), market).action == "hold"   # 同档不再触发

    app.state.store.close()
    app = application(database, gateway, provider)   # 重启：exitKey 持久，档位不重触
    assert key in app.state.store.strategy_exit_keys(strategy_id=STRATEGY_ID, user_id=USER)
    state = live_state(app, 1)
    assert state.scaled_out
    assert decide(plan, evaluation, state, market).action == "hold"
    app.state.store.close()

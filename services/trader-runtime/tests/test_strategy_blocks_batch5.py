"""第五批积木 (2026-09-28): 分时均价 (`vwap`) and 盘中触价入场 (an entry group's `trigger`).

分时均价 against hand figures, its trading-day reset, a window that starts inside the day completed
from the longer series exactly like the whole series, and an unknown day start staying empty. 盘中触价:
the level on the tick grid, the fill inside the next bar (touch, gap, never), a lapsed trigger that
counts for nothing, 双向触价 (OCO) with the nearer-to-open reading, the live side picked by the latest
price, the monitor that waits until the price gets there and then chases with the opposite price, and
a strategy using both judged bar by bar like live.
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from test_backtest_template_parity import FEES, parity_history
from test_simulation_orders import USER, Gateway, application
from test_strategy_backtest import cond, gapless, plan, series
from test_strategy_blocks_live import Provider, _save
from test_strategy_monitor import STRATEGY_ID, ProposalStub
from trader_runtime.strategy_backtest import LIVE_WINDOW, BacktestSetup, run_backtest
from trader_runtime.strategy_engine import MarketContext, PositionState, decide, trigger_fill, trigger_level
from trader_runtime.strategy_monitor import StrategyMonitorService
from trader_runtime.strategy_plain import describe_plan
from trader_runtime.strategy_rules import (
    Operand,
    SeriesContext,
    SeriesFacts,
    SeriesWindow,
    StrategyPlan,
    evaluate_completed,
    evaluate_rules,
    indicator_values,
    plan_feature_lines,
    resolve_chart_timeframe,
    session_timeframes,
)

NS = 1_000_000_000
DIGEST = "c" * 64
DAY_ONLY = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]], "night": []}


def typical(bar: dict[str, Any]) -> float:
    return (bar["high"] + bar["low"] + bar["close"]) / 3


# -- 分时均价 ----------------------------------------------------------------------------------------------

def test_vwap_is_the_days_volume_weighted_typical_price():
    bars = series([(10, 12, 8, 11), (11, 14, 10, 13), (13, 15, 12, 12)])      # 2026-09-23 09:00, one day
    for bar, volume in zip(bars, (100, 300, 0), strict=True):
        bar["volume"] = volume
    vwap = indicator_values(bars, Operand(indicator="vwap", timeframe=300), SeriesContext(schedule=DAY_ONLY))
    first, second = typical(bars[0]), typical(bars[1])
    assert vwap[0] == pytest.approx(first)                      # the day opens here (09:00 = the first bell)
    assert vwap[1] == pytest.approx((first * 100 + second * 300) / 400)
    assert vwap[2] == vwap[1]                                    # no volume: unchanged


def test_vwap_restarts_each_trading_day_and_is_empty_before_a_known_day_start():
    history = parity_history()[60]
    whole = SeriesFacts(history)
    change = next(i for i in range(600, len(history)) if history[i]["tradingDay"] != history[i - 1]["tradingDay"])
    start = change - 100                                       # the window straddles a day change
    window = SeriesWindow(whole, start, start + LIVE_WINDOW)
    operand = Operand(indicator="vwap", timeframe=60)
    alone = indicator_values(list(window), operand, SeriesContext())
    # The window starts inside a day: without the earlier bars that day stays empty, the next day starts fresh.
    labels = [bar["tradingDay"] for bar in window]
    first_day = labels[0]
    assert all(value is None for value, day in zip(alone, labels, strict=True) if day == first_day)
    later = [i for i, day in enumerate(labels) if day != first_day]
    assert later and alone[later[0]] == pytest.approx(typical(window[later[0]]))
    # Completed from the whole series (a backtest) = from a stitched longer list (live) = the whole series.
    full = indicator_values(history, operand, SeriesContext())
    backtest = indicator_values(window, operand, SeriesContext(extended={60: history}))
    live = indicator_values(list(window), operand, SeriesContext(extended={60: history[:start + LIVE_WINDOW]}))
    assert backtest == pytest.approx(full[start:start + LIVE_WINDOW]) and live == pytest.approx(backtest)
    assert all(value is not None for value in backtest)
    vwap_plan = plan(entry=[cond("站上均价", "close", "gt", {"indicator": "vwap"}, tf=300)])
    assert session_timeframes(vwap_plan) == {300}


# -- 盘中触价: levels and fills -----------------------------------------------------------------------------

def test_levels_round_so_they_are_never_easier_to_reach():
    assert trigger_level(100.2, 1, long=True, kind="stop") == 101 and trigger_level(100.8, 1, long=False, kind="stop") == 100
    assert trigger_level(100.8, 1, long=True, kind="limit") == 100 and trigger_level(100.2, 1, long=False, kind="limit") == 101


@pytest.mark.parametrize("kind, long, bar, expected", [
    ("stop", True, {"open": 100, "high": 106, "low": 99}, 105.0),     # touched inside the bar
    ("stop", True, {"open": 108, "high": 110, "low": 107}, 108.0),    # opened past it: the open
    ("stop", True, {"open": 100, "high": 104, "low": 95}, None),      # never reached
    ("stop", False, {"open": 110, "high": 111, "low": 104}, 105.0),
    ("limit", True, {"open": 110, "high": 111, "low": 104}, 105.0),   # a buy limit waits for a dip
    ("limit", True, {"open": 103, "high": 104, "low": 100}, 103.0),
    ("limit", False, {"open": 100, "high": 104, "low": 99}, None),
])
def test_fills_inside_the_next_bar(kind, long, bar, expected):
    assert trigger_fill({"kind": kind, "price": 105.0}, bar, long=long) == expected


def trigger_plan(entry: list[dict[str, Any]], price: dict[str, Any], *, kind: str = "stop", **fields: Any) -> StrategyPlan:
    return StrategyPlan.model_validate({
        **plan(entry=entry).model_dump(), **fields,
        "rules": {"entryLong": {"conditions": entry, "trigger": {"kind": kind, "price": {"timeframe": 300, **price}}}}})


def test_the_decision_carries_the_armed_level_and_measures_the_preview_stop_from_it():
    bars = series(gapless([100, 101, 102, 103]))
    p = trigger_plan([cond("总是", "close", "gt", {"indicator": "constant", "value": 0})], {"indicator": "high", "barsAgo": 1},
                     stopLossTicks=5)
    evaluation = evaluate_completed(p, {300: bars})
    assert evaluation["triggers"] == {"entryLong": 103.0}          # the previous bar's high (102 + 1)
    decision = decide(p, evaluation, PositionState(), MarketContext(price_tick=1, volume_multiple=10))
    assert decision.action == "open_long" and decision.entryTrigger == {"kind": "stop", "price": 103.0}
    assert decision.stopPrice == 98.0                                 # 103 − 5 ticks, not from the 103 close
    assert "盘中触价" in dict(plan_feature_lines(p)) and "盘中价格涨到上一根K线的最高价就开多" in describe_plan(p).replace(" ", "")


def test_trigger_limits():
    base = plan().model_dump()
    with pytest.raises(ValueError, match="触价价位须使用执行周期"):
        StrategyPlan.model_validate({**base, "rules": {"entryLong": {"conditions": base["rules"]["entryLong"]["conditions"],
            "trigger": {"price": {"indicator": "high", "timeframe": 3600}}}}}).check_executable()
    with pytest.raises(ValidationError):
        StrategyPlan.model_validate({**base, "rules": {"entryLong": {"conditions": base["rules"]["entryLong"]["conditions"],
            "trigger": {"kind": "market", "price": {"indicator": "high"}}}}})
    follow = trigger_plan([cond("x", "close", "gt", {"indicator": "constant", "value": 0}, tf=0)], {"indicator": "prev_high"})
    raw = follow.model_dump()
    raw["timeframeSeconds"] = 0
    raw["rules"]["entryLong"]["trigger"]["price"]["timeframe"] = 0
    resolved = resolve_chart_timeframe(StrategyPlan.model_validate(raw), 900)
    assert resolved.rules.entryLong.trigger.price.timeframe == 900


def test_backtest_fills_at_the_level_and_a_lapsed_trigger_counts_for_nothing():
    # Setup on every bar; buy stop one tick above the bar's high. Closes rise 100 → 110 and then drift.
    closes = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 110, 110, 110, 110, 110]
    bars = series(gapless(closes))
    always = [cond("总是", "close", "gt", {"indicator": "constant", "value": 0})]
    p = trigger_plan(always, {"indicator": "high", "offset": None}, maxEntriesPerDirection=1, stopLossTicks=50)
    result = run_backtest(BacktestSetup(plan=p, symbol="SHFE.rb2610", series={300: bars}, price_tick=1,
                                        volume_multiple=10, fee_schedule=FEES, initial_balance=100_000, slippage_ticks=0))
    first = result["trades"][0]
    # Bar 1 (the first judged bar: high 102) arms 102; bar 2 (open 101, high 103) reaches it inside the bar.
    assert first["openPrice"] == 102.0 and first["openTime"] == bars[2]["datetime"]
    flat = trigger_plan(always, {"indicator": "constant", "value": 200}, stopLossTicks=50)
    flat_bars = series([(100, 101, 99, 100)] * 12)                      # never near the 200 buy stop
    lapsed = run_backtest(BacktestSetup(plan=flat, symbol="SHFE.rb2610", series={300: flat_bars}, price_tick=1,
                                        volume_multiple=10, fee_schedule=FEES, initial_balance=100_000, slippage_ticks=0))
    assert lapsed["summary"]["trades"] == 0 and any("盘中触价单" in note and "作废" in note for note in lapsed["notes"])


def test_both_ways_the_first_level_reached_fills():
    always = [cond("总是", "close", "gt", {"indicator": "constant", "value": 0})]
    raw = plan(direction="both").model_dump()
    raw["rules"] = {"entryLong": {"conditions": always, "trigger": {"price": {"indicator": "high", "timeframe": 300}}},
                    "entryShort": {"conditions": always, "trigger": {"price": {"indicator": "low", "timeframe": 300}}}}
    p = StrategyPlan.model_validate(raw)
    bars = series([(100, 104, 99, 103)] * 3)                             # close 103 nearer the 104 high
    evaluation = evaluate_completed(p, {300: bars})
    decision = decide(p, evaluation, PositionState(), MarketContext(price_tick=1, volume_multiple=10))
    assert decision.action == "open_long" and decision.entryTrigger["price"] == 104.0
    assert decision.alternate is not None and decision.alternate.action == "open_short"
    assert decision.alternate.entryTrigger == {"kind": "stop", "price": 99.0}
    # Live: the latest price at 98 has reached the sell stop — the short side is the decision.
    live = decide(p, evaluation, PositionState(), MarketContext(price_tick=1, volume_multiple=10, price=98.0))
    assert live.action == "open_short" and live.alternate is not None and live.alternate.action == "open_long"
    # Backtest: the next bar reaches both; the one nearer its open (99 from an open of 100) fills.
    both = [*bars, {**bars[-1], "datetime": bars[-1]["datetime"] + 300 * NS, "open": 100, "high": 105, "low": 97, "close": 101},
            {**bars[-1], "datetime": bars[-1]["datetime"] + 600 * NS}]
    result = run_backtest(BacktestSetup(plan=p, symbol="SHFE.rb2610", series={300: both}, price_tick=1,
                                        volume_multiple=10, fee_schedule=FEES, initial_balance=100_000, slippage_ticks=0))
    assert result["trades"][0]["direction"] == "short" and result["trades"][0]["openPrice"] == 99.0


# -- live: the monitor waits for the price ----------------------------------------------------------------

def _trigger_intent(reached: bool) -> dict[str, Any]:
    return {"intentId": "intent-trigger", "strategyId": STRATEGY_ID,
            "signalSnapshot": {"signal": "buy", "barTime": "1767225600000000000",
                               "entryTrigger": {"kind": "stop", "price": 823.0, "reached": reached,
                                                "other": {"kind": "stop", "price": 810.0, "action": "open_short"}}},
            "positionIntent": {"action": "open_long", "requestedVolume": 1, "reason": "条件已满足"},
            "riskAdjustedIntent": {"status": "allowed", "action": "open_long", "volume": 1, "reason": "条件已满足"}}


async def test_the_monitor_sends_nothing_until_the_level_is_reached_then_chases(tmp_path):
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / "trigger.sqlite3", gateway, provider)
    store = app.state.store
    _save(store, StrategyStub.plan())
    for reached in (False, True):
        intent = _trigger_intent(reached)
        store.save_strategy_intent_snapshot(intent_id=intent["intentId"], strategy_id=STRATEGY_ID, user_id=USER,
                                            strategy_digest=DIGEST, signal_bar_time=intent["signalSnapshot"]["barTime"],
                                            context_digest="d" * 64, payload=intent)
    stub = ProposalStub(store, _trigger_intent(False))
    monitor = StrategyMonitorService(store=store, proposals=stub, autonomy=app.state.autonomy)
    waiting = (await monitor.run_once())[0]
    assert waiting["status"] == "waiting" and gateway.submit_count == 0
    assert "挂触价 823.0" in waiting["detail"] and "另一边挂 810.0" in waiting["detail"] and "跌到" in waiting["detail"]
    stub.intent = _trigger_intent(True)
    submitted = (await monitor.run_once())[0]
    assert submitted["status"] == "submitted" and gateway.submit_count == 1, submitted
    quote = provider.market_snapshot("", "SHFE.au2612")
    assert quote["askPrice1"] != quote["bidPrice1"]
    assert float(gateway.request["limitPrice"]) == float(quote["askPrice1"])     # a reached buy stop takes the ask
    store.close()


class StrategyStub:
    @staticmethod
    def plan() -> StrategyPlan:
        always = [{"label": "总是", "left": {"indicator": "close", "timeframe": 300}, "operator": "gt",
                   "right": {"indicator": "constant", "value": 0}}]
        return StrategyPlan.model_validate({
            "name": "触价实盘", "description": "触价实盘测试", "direction": "long", "timeframeSeconds": 300,
            "maxPosition": 1, "stopLossTicks": 2, "takeProfitTicks": None, "cooldownBars": 0,
            "rules": {"entryLong": {"conditions": always, "trigger": {"price": {"indicator": "high", "timeframe": 300}}}}})


# -- backtest == live, bar by bar ------------------------------------------------------------------------

def _operand(name: str, **params: Any) -> dict[str, Any]:
    return {"indicator": name, "timeframe": 60, **params}


def blocks_plan() -> StrategyPlan:
    return StrategyPlan.model_validate({
        "name": "第五批积木", "description": "站上 / 跌破分时均价时，盘中突破前 10 根高点做多、跌破低点做空（双向触价）",
        "direction": "both", "timeframeSeconds": 60, "maxPosition": 1, "stopLossTicks": 30, "takeProfitTicks": None,
        "cooldownBars": 0, "maxLoss": 1e8, "maxOrdersPerDay": 50,
        "rules": {
            "entryLong": {"conditions": [{"label": "离均价不远", "left": _operand("close"), "operator": "gt",
                                          "right": _operand("vwap", offset={"factor": -2, "indicator": "atr", "period": 14})}],
                          "trigger": {"price": _operand("high", highest=10)}},
            "exitLong": {"conditions": [{"label": "跌破均价", "left": _operand("close"), "operator": "lt", "right": _operand("vwap")}]},
            "entryShort": {"conditions": [{"label": "离均价不远", "left": _operand("close"), "operator": "lt",
                                           "right": _operand("vwap", offset={"factor": 2, "indicator": "atr", "period": 14})}],
                           "trigger": {"price": _operand("low", lowest=10)}},
            "exitShort": {"conditions": [{"label": "升破均价", "left": _operand("close"), "operator": "gt", "right": _operand("vwap")}]},
        }})


def test_fifth_batch_blocks_judge_every_bar_like_live():
    """每根K线：回测的判定 == 实盘在同一时刻的 evaluate_rules（带正在形成的K线）+ decide。"""
    from test_backtest_template_parity import _ReadOnlyHistory, live_session_history
    p = blocks_plan()
    base = parity_history()[60][-2_400:]
    setup = BacktestSetup(plan=p, symbol="SHFE.rb2701", series={60: base}, price_tick=1, volume_multiple=10,
                          fee_schedule=FEES, initial_balance=1_000_000)
    seen: list[tuple[int, Any, Any]] = []
    result = run_backtest(setup, trace=lambda k, state, decision: seen.append((k, state, decision)))
    assert result["summary"]["trades"] >= 10
    assert {trade["direction"] for trade in result["trades"]} == {"long", "short"}
    judged = 0
    for k, state, decision in seen:
        if decision.fillPrice is not None:
            continue
        forming = {**base[k], "datetime": base[k]["datetime"] + 60 * NS}
        workbenches = {60: {"bars": base[max(0, k + 1 - LIVE_WINDOW):k + 1] + [forming], "quote": {}}}
        extended, pending = live_session_history(_ReadOnlyHistory(base[:k + 1] + [forming]), p, setup.symbol, workbenches)
        if pending:
            continue
        equity = 1_000_000 + sum(t["net"] for t in result["trades"] if t["closeTime"] <= base[k]["datetime"])
        live = decide(p, evaluate_rules(p, workbenches, extended=extended), state,
                      MarketContext(price_tick=1, volume_multiple=10, equity=equity))
        assert (live.action, live.volume, live.stopPrice, live.reason, live.entryTrigger,
                live.alternate and live.alternate.entryTrigger) == \
            (decision.action, decision.volume, decision.stopPrice, decision.reason, decision.entryTrigger,
             decision.alternate and decision.alternate.entryTrigger), k
        judged += 1
    assert judged > 1_000

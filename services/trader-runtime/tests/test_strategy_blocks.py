"""策略积木·第一期 A (2026-09-25): indicators, trading days and the pure strategy engine.

Indicator values are checked against hand-worked numbers or an independent reference loop
(pandas is not a Runtime dependency): Donchian excludes the current bar, ATR / RSI use Wilder
smoothing, Bollinger the population standard deviation, SuperTrend flips like TradingView's.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from trader_runtime.strategy_engine import (
    MarketContext,
    PositionState,
    atr_position_size,
    bars_held,
    decide,
    open_position,
    step,
    trail_stop,
)
from trader_runtime.strategy_rules import (
    Condition,
    Operand,
    SeriesContext,
    StrategyPlan,
    describe_condition,
    evaluate_completed,
    indicator_values,
    plan_feature_lines,
    plan_timeframes,
    resolve_chart_timeframe,
)
from trader_runtime.trading_calendar import (
    first_bars_of_day,
    is_night,
    session_position,
    trading_day_of,
)

CST = timezone(timedelta(hours=8))
SHFE = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
        "night": [["21:00:00", "25:00:00"]]}
DAY_ONLY = {"day": [["09:30:00", "11:30:00"], ["13:00:00", "15:00:00"]], "night": []}


def ns(year, month, day, hour=0, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=CST).timestamp() * 1_000_000_000)


def ohlc(rows, start=0, step_seconds=300):
    return [{"datetime": start + i * step_seconds * 1_000_000_000, "open": o, "high": h, "low": low, "close": c,
             "volume": 100} for i, (o, h, low, c) in enumerate(rows)]


def closes(values):
    return ohlc([(v, v + 1, v - 1, v) for v in values])


def values(bars, indicator, **params):
    return indicator_values(bars, Operand(indicator=indicator, **params))


# -- indicators ------------------------------------------------------------------------------------------

def test_ema_matches_pandas_ewm_adjust_false_after_warmup():
    series = [3.0, 5.0, 4.0, 8.0, 7.0, 9.0, 12.0, 10.0]
    alpha, expected, level = 2 / 4, [], None
    for x in series:
        level = x if level is None else alpha * x + (1 - alpha) * level
        expected.append(level)
    got = values(closes(series), "ema", period=3)
    assert got[:2] == [None, None]
    assert got[2:] == pytest.approx(expected[2:])


def test_atr_uses_wilder_smoothing_seeded_by_the_mean():
    bars = ohlc([(9, 10, 8, 9), (10, 11, 9, 10), (10, 12, 9, 11), (12, 13, 10, 12), (11, 12, 10, 11)])
    # True ranges 2, 2, 3, 3, 2 → seed 7/3, then (prev × 2 + TR) / 3.
    assert values(bars, "atr", period=3) == pytest.approx([None, None, 7 / 3, 23 / 9, 64 / 27])


def test_cci_of_a_flat_decimal_window_is_zero_not_a_rounding_residue():
    """#998: a flat window of decimal prices (4123.2 × 14) must read 0 in the strategy engine, never ±66.7 from a
    rounding residue (the chart's and the AI reading's CCI had that residue; this guards the engine's)."""
    from trader_runtime.indicators import cci
    for price in (4123.2, 3.1, 57.305, 6.602):  # 6.602 × 14 leaves a residue in a plain Python mean
        bars = ohlc([(price, price, price, price)] * 16)
        assert values(bars, "cci", period=14)[-3:] == [0.0, 0.0, 0.0]
        flat = [price] * 16
        assert cci(flat, flat, flat, 14)[-3:] == [0.0, 0.0, 0.0]  # the AI reading: the same 0, not 样本不足


def test_donchian_excludes_the_current_bar():
    bars = ohlc([(1, h, low, 1) for h, low in ((1, 0), (5, 2), (3, 1), (9, 4), (2, 0))])
    assert values(bars, "donchian_high", period=2) == [None, None, 5, 5, 9]
    assert values(bars, "donchian_low", period=2) == [None, None, 0, 1, 1]


def test_bollinger_uses_population_standard_deviation():
    bars = closes([1, 2, 3, 4])
    deviation = math.sqrt(5 / 4)  # population; the sample deviation would be sqrt(5/3)
    assert values(bars, "boll_mid", period=4)[-1] == pytest.approx(2.5)
    assert values(bars, "boll_up", period=4)[-1] == pytest.approx(2.5 + 2 * deviation)
    assert values(bars, "boll_down", period=4, k=1)[-1] == pytest.approx(2.5 - deviation)


def test_keltner_is_ema_or_ma_middle_plus_k_atr():
    bars = ohlc([(10 + i, 11 + i + (i % 3), 9 + i - (i % 2), 10.5 + i) for i in range(30)])
    middle, atr = values(bars, "ema", period=10), values(bars, "atr", period=10)
    upper = values(bars, "keltner_up", period=10)
    assert upper[-1] == pytest.approx(middle[-1] + 1.5 * atr[-1])
    lower = values(bars, "keltner_down", period=10, k=2, basis="ma")
    assert lower[-1] == pytest.approx(values(bars, "ma", period=10)[-1] - 2 * atr[-1])


def test_rsi_matches_wilders_worked_example():
    # The classic StockCharts RSI(14) data. Its table rounds the averages (70.53, 66.32, …); at full
    # precision the first averages are 3.34/14 and 1.40/14 → RS 2.3857 → 70.46, as TA-Lib prints.
    series = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
              46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
    got = values(closes(series), "rsi", period=14)
    assert got[13] is None
    assert got[14] == pytest.approx(100 - 100 / (1 + (3.34 / 14) / (1.40 / 14)))
    assert got[14:] == pytest.approx([70.46, 66.25, 66.48, 69.35, 66.29, 57.92], abs=0.01)


def _reference_supertrend(bars, period, k):
    """TradingView's SuperTrend, transcribed with nz(): trend starts 1, flips on the previous band."""
    ranges, atr, level = [], [], None
    for i, b in enumerate(bars):
        ranges.append(b["high"] - b["low"] if i == 0 else max(b["high"] - b["low"], abs(b["high"] - bars[i - 1]["close"]),
                                                              abs(b["low"] - bars[i - 1]["close"])))
        if i == period - 1:
            level = sum(ranges) / period
        elif i >= period:
            level = (level * (period - 1) + ranges[-1]) / period
        atr.append(level)
    out, up_prev, dn_prev, trend = [], None, None, 1
    for i, b in enumerate(bars):
        if atr[i] is None:
            out.append(None)
            continue
        hl2 = (b["high"] + b["low"]) / 2
        up, dn = hl2 - k * atr[i], hl2 + k * atr[i]
        up1 = up if up_prev is None else up_prev
        dn1 = dn if dn_prev is None else dn_prev
        up = max(up, up1) if bars[i - 1]["close"] > up1 else up
        dn = min(dn, dn1) if bars[i - 1]["close"] < dn1 else dn
        trend = 1 if trend == -1 and b["close"] > dn1 else -1 if trend == 1 and b["close"] < up1 else trend
        out.append(up if trend == 1 else dn)
        up_prev, dn_prev = up, dn
    return out


def test_supertrend_flips_with_price_and_matches_the_reference():
    path = [100 + i for i in range(15)] + [114 - 2 * i for i in range(15)] + [86 + 3 * i for i in range(12)]
    bars = ohlc([(c, c + 1.5, c - 1.5, c) for c in path])
    got = values(bars, "supertrend", period=5, k=2)
    assert got == pytest.approx(_reference_supertrend(bars, 5, 2))
    below = [i for i, v in enumerate(got) if v is not None and v < bars[i]["close"]]
    above = [i for i, v in enumerate(got) if v is not None and v > bars[i]["close"]]
    assert below and above and min(above) > min(below) and max(below) > max(above)  # up → down → up again


def test_average_and_offset_compose_on_the_same_series():
    bars = ohlc([(10 + i, 12 + i, 9 + i, 11 + i) for i in range(40)])
    atr = values(bars, "atr", period=14)
    smoothed = indicator_values(bars, Operand(indicator="atr", period=14, average=5))
    assert smoothed[-1] == pytest.approx(sum(atr[-5:]) / 5)
    band = indicator_values(bars, Operand.model_validate(
        {"indicator": "ma", "period": 10, "offset": {"factor": 1.5, "indicator": "atr"}}))
    assert band[-1] == pytest.approx(values(bars, "ma", period=10)[-1] + 1.5 * atr[-1])


# -- trading days and sessions -------------------------------------------------------------------------------

def test_friday_night_session_belongs_to_monday():
    assert trading_day_of(ns(2026, 9, 25, 21, 30)) == "2026-09-28"   # Friday night
    assert trading_day_of(ns(2026, 9, 26, 1, 0)) == "2026-09-28"     # after midnight, Saturday
    assert trading_day_of(ns(2026, 9, 29, 1, 0)) == "2026-09-29"     # Monday night's tail → Tuesday
    assert trading_day_of(ns(2026, 9, 28, 10, 0)) == "2026-09-28"
    # A holiday: the known trading days snap the naive day forward.
    assert trading_day_of(ns(2026, 9, 30, 21, 0), ["2026-09-30", "2026-10-08"]) == "2026-10-08"


def test_session_position_counts_minutes_to_the_session_and_day_end():
    close = session_position(ns(2026, 9, 23, 14, 56), SHFE)
    assert close.in_session and not close.night and close.minutes_to_day_end == pytest.approx(4)
    lunch = session_position(ns(2026, 9, 23, 11, 28), SHFE)
    assert lunch.minutes_to_session_end == pytest.approx(2) and lunch.minutes_to_day_end == pytest.approx(212)
    night = session_position(ns(2026, 9, 25, 22, 0), SHFE)
    assert night.in_session and night.night and night.trading_day == "2026-09-28"
    assert night.minutes_to_session_end == pytest.approx(180)
    assert not session_position(ns(2026, 9, 23, 12, 0), SHFE).in_session
    assert not is_night(ns(2026, 9, 23, 10, 0), DAY_ONLY) and is_night(ns(2026, 9, 23, 21, 30), SHFE)


def _day(bars_rows, day: str, start: int):
    return [{**bar, "tradingDay": day} for bar in ohlc(bars_rows, start=start)]


def test_night_product_today_open_is_the_night_open_and_anchors_follow_trading_days():
    # Trading day 2026-09-28 (Monday) opens on Friday 21:00. Its daily bar (TqSdk: 00:00 CST of the
    # trading day) opens at the night open; the previous trading day is Friday 09-25.
    daily = [{"datetime": ns(2026, 9, 24), "open": 90, "high": 99, "low": 88, "close": 95, "tradingDay": "2026-09-24"},
             {"datetime": ns(2026, 9, 25), "open": 96, "high": 110, "low": 94, "close": 100, "tradingDay": "2026-09-25"},
             {"datetime": ns(2026, 9, 28), "open": 103, "high": 104, "low": 101, "close": 102, "tradingDay": "2026-09-28"}]
    day_bars = _day([(99, 100, 98, 100)] * 3, "2026-09-25", ns(2026, 9, 25, 14, 45))
    night = _day([(103, 104, 101, 102), (102, 103, 100, 101), (101, 108, 100, 107)], "2026-09-28", ns(2026, 9, 25, 21, 0))
    bars = day_bars + night
    context = SeriesContext(daily=daily, schedule=SHFE)
    get = lambda name, **p: indicator_values(bars, Operand(indicator=name, **p), context)  # noqa: E731
    # Friday-night bars read Friday as 昨 and the night open as 今开 — never a calendar-day split
    # (which would mix the night into "Friday" and give a 108 high).
    assert get("prev_high")[3:] == [110] * 3 and get("prev_close")[-1] == 100
    assert get("today_open")[3:] == [103] * 3 and get("today_open")[:3] == [96] * 3
    assert get("gap_percent")[-1] == pytest.approx(3.0)
    assert get("range_nday", period=2)[-1] == pytest.approx(max(110 - 95, 100 - 88))
    assert get("range_nday", period=3)[-1] is None  # not enough prior days
    # Without the day's own daily bar yet, 今开 is the first bar of that trading day in the series.
    assert indicator_values(bars, Operand(indicator="today_open"), SeriesContext(daily=daily[:2], schedule=SHFE))[-1] == 103
    assert get("opening_range_high", period=2)[3:] == [None, 104, 104]
    assert first_bars_of_day(night, 0, 1, schedule=SHFE)[0]["open"] == 103  # starts on the night bell


def test_day_only_product_has_no_night_session():
    daily = [{"datetime": ns(2026, 9, 22), "open": 1, "high": 5, "low": 1, "close": 4},
             {"datetime": ns(2026, 9, 23), "open": 6, "high": 7, "low": 5, "close": 6}]
    bars = ohlc([(6, 7, 5, 6), (6, 8, 6, 7)], start=ns(2026, 9, 23, 9, 30))
    context = SeriesContext(daily=daily, schedule=DAY_ONLY)
    assert indicator_values(bars, Operand(indicator="today_open"), context) == [6, 6]
    assert indicator_values(bars, Operand(indicator="prev_high"), context) == [5, 5]
    assert session_position(ns(2026, 9, 23, 21, 30), DAY_ONLY).in_session is False
    assert first_bars_of_day(bars, 0, 1, schedule=DAY_ONLY)[0]["open"] == 6


def test_daily_conditions_in_the_night_session_use_the_just_finished_day():
    raw = _plan(entry=[_cond("日线收盘高于10", "close", "gt", {"indicator": "constant", "value": 10}, timeframe=86400)])
    raw["timeframeSeconds"] = 300
    plan = StrategyPlan.model_validate(raw)
    daily = [{"datetime": ns(2026, 9, 24 + i), "open": 5, "high": 20, "low": 1, "close": c, "tradingDay": f"2026-09-{24 + i}"}
             for i, c in enumerate([5, 11])]
    night = _day([(10, 11, 9, 10)] * 3, "2026-09-28", ns(2026, 9, 25, 21, 0))
    # Friday (09-25, close 11) is complete at 15:00 even though its bar ends at Saturday midnight.
    assert evaluate_completed(plan, {300: night, 86400: daily})["rules"]["entryLong"] is True


# -- labels, schema ------------------------------------------------------------------------------------------

def test_new_blocks_have_chinese_labels():
    condition = Condition.model_validate({"label": "突破", "left": {"indicator": "close"}, "operator": "gt",
        "right": {"indicator": "today_open", "offset": {"factor": 0.2, "indicator": "range_nday", "period": 5}}})
    assert describe_condition(condition) == "收盘价 > 今开 + 0.2×5日振幅"
    trail = Condition.model_validate({"label": "偏离", "left": {"indicator": "close"}, "operator": "gte",
        "right": {"indicator": "ma", "period": 20, "offset": {"factor": 1.5, "indicator": "atr"}}})
    assert describe_condition(trail) == "收盘价 ≥ MA20 + 1.5×ATR14"
    volatility = Condition.model_validate({"label": "波动", "left": {"indicator": "atr"}, "operator": "gt",
        "right": {"indicator": "atr", "average": 20}})
    assert describe_condition(volatility) == "ATR14 > ATR14的20均线"
    plan = StrategyPlan.model_validate({**_plan(), "atrStop": {"multiple": 2}, "closeBeforeEndMinutes": 5,
        "trailingStop": {"mode": "percent", "value": 0.8, "activateAfter": 0.5}, "maxHoldBars": 30,
        "sizing": {"mode": "atr_risk", "riskPercent": 1}, "entryWindows": [{"start": "09:05", "end": "14:30"}],
        "allowNightEntries": False, "maxEntriesPerDirection": 2})
    kinds = dict(plan_feature_lines(plan))
    assert kinds["收盘前平仓"] == "当日最后一个时段收盘前 5 分钟平仓"
    assert "回撤 0.8%" in kinds["追踪止损"] and "浮盈达 0.5%" in kinds["追踪止损"]
    assert set(kinds) >= {"止损", "最长持仓", "仓位", "开仓时段", "开仓次数"}


def test_old_definitions_still_validate_and_stops_are_exclusive():
    old = {"name": "旧", "description": "旧策略", "rules": {"entryLong": {"conditions": [
        {"label": "上穿", "left": {"indicator": "ma", "period": 5, "timeframe": 60}, "operator": "cross_up",
         "right": {"indicator": "ma", "period": 10, "timeframe": 60}}]}}, "direction": "long"}
    plan = StrategyPlan.model_validate(old)
    assert plan.rules.entryLong.conditions[0].left.period == 5 and plan.atrStop is None and plan.allowNightEntries
    assert Operand(indicator="atr").period == 14 and Operand(indicator="ma").period == 5
    plan.check_executable()
    with pytest.raises(ValueError, match="只能选一种"):
        StrategyPlan.model_validate({**old, "atrStop": {"multiple": 2}, "stopLookbackBars": 5,
                                     "perTradeRisk": 100}).check_executable()
    with pytest.raises(ValueError):
        Operand.model_validate({"indicator": "constant", "offset": {"factor": 1, "indicator": "atr"}})
    with pytest.raises(ValueError):
        StrategyPlan.model_validate({**old, "sizing": {"mode": "atr_risk"}})
    anchored = StrategyPlan.model_validate(_plan(entry=[_cond("跳空", "gap_percent", "gt", {"indicator": "constant", "value": 1})]))
    assert plan_timeframes(anchored) == {300, 86400}
    follow = _plan(entry=[_cond("突破", "close", "gt", {"indicator": "today_open", "offset": {"factor": 0.2, "indicator": "range_nday"}}, timeframe=0)])
    follow["timeframeSeconds"] = 0
    bound = resolve_chart_timeframe(StrategyPlan.model_validate(follow), 60)
    assert bound.rules.entryLong.conditions[0].right.timeframe == 60 and bound.rules.entryLong.conditions[0].right.offset.period == 5


# -- the engine ----------------------------------------------------------------------------------------------

def _cond(label, left, operator, right, timeframe=300, **left_params):
    return {"label": label, "left": {"indicator": left, "timeframe": timeframe, **left_params}, "operator": operator,
            "right": {"timeframe": timeframe, **right}}


def _plan(entry=None, exit_=None, **fields):
    rules = {"entryLong": {"conditions": entry or [_cond("价格", "close", "gt", {"indicator": "constant", "value": 0})]}}
    if exit_:
        rules["exitLong"] = {"conditions": exit_}
    return {"name": "积木", "description": "积木测试", "direction": "long", "timeframeSeconds": 300, "maxPosition": 3,
            "stopLossTicks": 10, "takeProfitTicks": None, "cooldownBars": 0, "rules": rules, **fields}


MARKET = MarketContext(price_tick=1, volume_multiple=10, equity=100_000)


def _session_bars(rows, start):
    return [{**bar, "tradingDay": trading_day_of(bar["datetime"])} for bar in ohlc(rows, start=start)]


def test_entry_trailing_exit_sequence_is_deterministic():
    raw = _plan(entry=[_cond("突破", "close", "gt", {"indicator": "donchian_high", "period": 5})],
                trailingStop={"mode": "ticks", "value": 5})
    plan = StrategyPlan.model_validate(raw)
    rows = [(100, 101, 99, 100)] * 10 + [(100, 104, 100, 103), (103.5, 106, 103, 105), (105, 110, 104, 109),
                                          (109, 109.5, 106, 107), (107, 107, 104, 104.5)]
    series = _session_bars(rows, ns(2026, 9, 23, 9, 0))

    def run():
        state, log = PositionState(), []
        for k in range(9, len(series)):
            decision = step(plan, {300: series[:k + 1]}, state, MARKET, schedule=SHFE)
            log.append(decision.as_dict())
            if decision.action == "open_long":
                nxt = series[k + 1]
                state = open_position(plan, decision, fill_price=nxt["open"], fill_time=nxt["datetime"], price_tick=1,
                                      previous=state)
            elif decision.action == "close_long":
                state = PositionState(entries_long=state.entries_long, trading_day=state.trading_day)
            else:
                state = decision.state or state
        return log

    first = run()
    assert first == run()  # same input, same output: no clock, no database
    actions = [(item["action"], item["exitReason"]) for item in first]
    assert actions == [("hold", None), ("open_long", None), ("hold", None), ("hold", None), ("hold", None),
                       ("close_long", "trailing_stop")]
    held = first[2:5]
    assert [item["stopPrice"] for item in held] == [101, 105, 105]   # only tightens, never back to 104.5-5
    assert first[-1]["fillPrice"] == 105 and "追踪止损" in first[-1]["reason"]


def test_close_before_end_fires_once_per_position_and_trading_day():
    plan = StrategyPlan.model_validate(_plan(exit_=[_cond("永不", "close", "lt", {"indicator": "constant", "value": 0})],
                                             closeBeforeEndMinutes=5))
    series = _session_bars([(100, 101, 99, 100)] * 12, ns(2026, 9, 23, 14, 0))  # last bar closes 15:00
    state = PositionState(direction="long", volume=2, entry_price=100, entry_time=series[0]["datetime"],
                          highest=100, lowest=100, stop_price=90, position_key="p1")
    early = step(plan, {300: series[:-2]}, state, MARKET, schedule=SHFE)  # closes 14:50
    assert early.action == "hold"
    due = step(plan, {300: series[:-1]}, state, MARKET, schedule=SHFE)   # closes 14:55
    assert (due.action, due.volume, due.exitReason) == ("close_long", 2, "close_before_end")
    assert due.exitKey == "close_before_end:2026-09-23:day:p1"
    again = step(plan, {300: series[:-1]}, replace(state, fired_exits=frozenset({due.exitKey})), MARKET, schedule=SHFE)
    assert again.action == "hold"
    # Live, the trading clock decides: 14:57 on a 15-minute bar that closed at 14:45 still fires.
    evaluation = evaluate_completed(plan, {300: series[:-3]}, schedule=SHFE)
    live = decide(plan, evaluation, state, replace(MARKET, now=ns(2026, 9, 23, 14, 57)))
    assert live.exitReason == "close_before_end"
    per_session = StrategyPlan.model_validate({**_plan(), "closeBeforeEndMinutes": 5, "closeBeforeEndScope": "session"})
    lunch = decide(per_session, evaluation, state, replace(MARKET, now=ns(2026, 9, 23, 11, 26)))
    assert lunch.action == "close_long" and ":day:" not in lunch.exitKey


def test_max_hold_bars_closes_once():
    plan = StrategyPlan.model_validate(_plan(maxHoldBars=3))
    series = _session_bars([(100, 101, 99, 100)] * 8, ns(2026, 9, 23, 9, 0))
    state = PositionState(direction="long", volume=1, entry_price=100, entry_time=series[4]["datetime"],
                          highest=100, lowest=100, stop_price=90, position_key="p")
    assert bars_held(series[:7], series[4]["datetime"], 300) == 3
    assert step(plan, {300: series[:6]}, state, MARKET, schedule=SHFE).action == "hold"
    due = step(plan, {300: series[:7]}, state, MARKET, schedule=SHFE)
    assert (due.action, due.exitReason, due.exitKey) == ("close_long", "max_hold", "max_hold:p")
    assert step(plan, {300: series[:8]}, replace(state, fired_exits=frozenset({"max_hold:p"})), MARKET,
                schedule=SHFE).action == "hold"


def test_entry_window_night_and_daily_count_gate_entries_but_not_exits():
    evaluation = {"rules": {"entryLong": True, "exitLong": True, "entryShort": False, "exitShort": False},
                  "engine": {"barClose": ns(2026, 9, 23, 14, 0), "tradingDay": "2026-09-23", "schedule": SHFE,
                             "close": 100.0, "atr": {}}}
    windowed = StrategyPlan.model_validate(_plan(entryWindows=[{"start": "09:00", "end": "10:00"}]))
    flat = decide(windowed, evaluation, PositionState(), MARKET)
    assert flat.action == "hold" and "不在允许开仓的时段" in flat.reason
    held = decide(windowed, evaluation, PositionState(direction="long", volume=1), MARKET)
    assert held.action == "close_long"  # exits are never gated
    night = {**evaluation, "engine": {**evaluation["engine"], "barClose": ns(2026, 9, 23, 21, 30)}}
    no_night = StrategyPlan.model_validate(_plan(allowNightEntries=False))
    assert "夜盘" in decide(no_night, night, PositionState(), MARKET).reason
    assert decide(no_night, evaluation, PositionState(), MARKET).action == "open_long"
    counted = StrategyPlan.model_validate(_plan(maxEntriesPerDirection=1))
    used = PositionState(entries_long=1, trading_day="2026-09-23")
    assert decide(counted, evaluation, used, MARKET).action == "hold"
    # Counts belong to a trading day: yesterday's entry does not block today.
    assert decide(counted, evaluation, replace(used, trading_day="2026-09-22"), MARKET).action == "open_long"


def test_gap_filter_blocks_until_the_open_gaps_enough():
    plan = StrategyPlan.model_validate(_plan(entry=[_cond("高开超过1%", "gap_percent", "gt", {"indicator": "constant", "value": 1})]))
    bars = _session_bars([(0, 0, 0, 0)] * 3, ns(2026, 9, 23, 9, 0))
    daily = [{"datetime": ns(2026, 9, 22), "open": 99, "high": 101, "low": 98, "close": 100},
             {"datetime": ns(2026, 9, 23), "open": 102, "high": 103, "low": 101, "close": 102}]
    for bar in bars:
        bar.update(open=102, high=103, low=101, close=102)
    assert evaluate_completed(plan, {300: bars}, daily=daily)["rules"]["entryLong"] is True
    daily[-1]["open"] = 100.5
    assert evaluate_completed(plan, {300: bars}, daily=daily)["rules"]["entryLong"] is False


def test_atr_sizing_caps_and_refuses_zero_lots_with_a_reason():
    plan = StrategyPlan.model_validate(_plan(sizing={"mode": "atr_risk", "riskPercent": 1, "atrMultiple": 2}))
    assert atr_position_size(plan, 5, MARKET) == (3, None)             # 1000 / (10×2×5) = 10 → maxPosition 3
    assert atr_position_size(plan, 20, MARKET) == (2, None)            # 1000 / 400
    lots, reason = atr_position_size(plan, 200, MARKET)
    assert lots == 0 and "不足 1 手" in reason
    lots, reason = atr_position_size(plan, 5, replace(MARKET, equity=None))
    assert lots == 0 and "不新鲜" in reason
    evaluation = {"rules": {"entryLong": True, "exitLong": False, "entryShort": False, "exitShort": False},
                  "engine": {"barClose": ns(2026, 9, 23, 10, 0), "tradingDay": "2026-09-23", "close": 100.0,
                             "atr": {"14": 200.0}}}
    refused = decide(plan, evaluation, PositionState(), MARKET)
    assert (refused.action, refused.volume) == ("hold", 0) and "不足 1 手" in refused.reason
    sized = decide(plan, {**evaluation, "engine": {**evaluation["engine"], "atr": {"14": 20.0}}}, PositionState(), MARKET)
    assert (sized.action, sized.volume) == ("open_long", 2) and "ATR" in sized.reason


def test_atr_stop_and_trail_levels():
    plan = StrategyPlan.model_validate(_plan(atrStop={"multiple": 2}, takeProfitTicks=40))
    evaluation = {"rules": {"entryLong": True, "exitLong": False, "entryShort": False, "exitShort": False},
                  "engine": {"barClose": ns(2026, 9, 23, 10, 0), "tradingDay": "2026-09-23", "close": 100.0,
                             "atr": {"14": 2.3}}}
    decision = decide(plan, evaluation, PositionState(), MARKET)
    assert decision.entryStopDistance == pytest.approx(4.6) and decision.stopPrice == 95  # floor(95.4)
    state = open_position(plan, decision, fill_price=101, fill_time=1, price_tick=1)
    assert (state.stop_price, state.take_profit_price) == (96, 141)   # re-anchored on the fill: floor(96.4)
    missing = decide(plan, {**evaluation, "engine": {**evaluation["engine"], "atr": {"14": None}}}, PositionState(), MARKET)
    assert missing.action == "hold" and "ATR" in missing.reason
    chandelier = {"mode": "atr_chandelier", "value": 3, "activateAfter": 1}
    assert trail_stop(chandelier, long=True, entry_price=100, extreme=101, price_tick=1, current_stop=95, entry_atr=2) == 95
    assert trail_stop(chandelier, long=True, entry_price=100, extreme=110, price_tick=1, current_stop=95, entry_atr=2) == 104
    assert trail_stop({"mode": "percent", "value": 1}, long=False, entry_price=100, extreme=90, price_tick=0.5,
                      current_stop=95) == pytest.approx(91)   # ceil(90.9 / 0.5) × 0.5
    assert trail_stop({"mode": "ticks", "value": 5}, long=True, entry_price=100, extreme=102, price_tick=1,
                      current_stop=99) == 99  # never loosens


# 策略编辑卡第二版 (2026-10-08): the client's 指标选择器 disables what the engine refuses (strategyEditor.test.ts
# “指标选择器…与引擎的校验一致” holds the same cases); this side proves the engine refuses / takes exactly those.
def _side(indicator: str, **extra) -> dict:
    return {"indicator": indicator, **extra}


EDITOR_OK = [
    (_side("adx", period=50), "gt", _side("constant", value=25), {}),
    (_side("oi_quadrant", period=1), "eq", _side("constant", value=4), {}),
    (_side("close"), "rising", _side("constant", value=20), {}),
    (_side("rsi", period=14), "lt", _side("constant", value=30), {"withinBars": 5, "minCount": 5}),
    (_side("close", highest=2, barsAgo=50), "gt", _side("keltner_up", period=20, k=10, basis="ma"), {}),
]
EDITOR_REFUSED = [
    (_side("adx", period=51), "gt", _side("constant", value=25), {}),
    (_side("trix", period=61), "gt", _side("constant", value=0), {}),
    (_side("ma", period=1), "gt", _side("constant", value=0), {}),
    (_side("cci", period=201), "gt", _side("constant", value=0), {}),
    (_side("close"), "eq", _side("constant", value=1), {}),
    (_side("oi_quadrant", period=1), "gt", _side("constant", value=1), {}),
    (_side("oi_quadrant", period=1), "eq", _side("constant", value=5), {}),
    (_side("close"), "rising", _side("constant", value=21), {}),
    (_side("close"), "rising", _side("ma", period=20), {}),
    (_side("close"), "cross_up", _side("ma", period=20), {"consecutive": 2}),
    (_side("close"), "gt", _side("ma", period=20), {"withinBars": 3, "minCount": 4}),
    (_side("constant", value=1), "gt", _side("constant", value=0), {}),
    (_side("close", barsAgo=51), "gt", _side("constant", value=0), {}),
    (_side("close", average=1), "gt", _side("constant", value=0), {}),
    (_side("boll_up", period=20, k=11), "gt", _side("constant", value=0), {}),
    (_side("pivot", value=4), "gt", _side("constant", value=0), {}),
]


@pytest.mark.parametrize(("left", "operator", "right", "extra"), EDITOR_OK)
def test_editor_picker_allows_what_the_engine_takes(left, operator, right, extra):
    Condition.model_validate({"label": "x", "left": left, "operator": operator, "right": right, **extra})


@pytest.mark.parametrize(("left", "operator", "right", "extra"), EDITOR_REFUSED)
def test_editor_picker_refuses_what_the_engine_refuses(left, operator, right, extra):
    with pytest.raises(ValueError):
        Condition.model_validate({"label": "x", "left": left, "operator": operator, "right": right, **extra})


def test_editor_fixed_period_cannot_be_faster_than_the_strategy():
    """The picker's 固定周期 offers only periods no faster than the strategy's; a faster one is refused at execution."""
    def plan_with(timeframe: int) -> StrategyPlan:
        condition = {"label": "x", "left": {"indicator": "ma", "period": 5, "timeframe": timeframe}, "operator": "gt",
                     "right": {"indicator": "constant", "value": 1, "timeframe": timeframe}}
        return StrategyPlan.model_validate({"name": "x", "description": "x", "direction": "long", "timeframeSeconds": 300,
                                            "rules": {"entryLong": {"conditions": [condition]}}})
    plan_with(900).check_executable()
    plan_with(300).check_executable()
    with pytest.raises(ValueError, match="执行周期"):
        plan_with(60).check_executable()


def test_editor_alignment_and_rearm_rules_match_the_engine():
    """方向确认 / 复位 are editable in the card (rowContext in strategyEditor.ts): with 方向确认 the whole group reads current
    states joined by 全部满足 on the strategy's own period; 复位 uses that period too."""
    def condition(operator: str = "gt", timeframe: int = 300, **extra) -> dict:
        return {"label": "x", "left": {"indicator": "close", "timeframe": timeframe}, "operator": operator,
                "right": {"indicator": "ma", "period": 20, "timeframe": timeframe}, **extra}

    def plan_with(group: dict) -> StrategyPlan:
        return StrategyPlan.model_validate({"name": "x", "description": "x", "direction": "long", "timeframeSeconds": 300,
                                            "rules": {"entryLong": group}})
    plan_with({"alignmentConditions": [condition("gte")], "conditions": [condition("lt")], "rearm": [condition("cross_down")]}).check_executable()
    for refused in ({"alignmentConditions": [condition()], "conditions": [condition("cross_up")]},
                    {"alignmentConditions": [condition()], "conditions": [condition(consecutive=2)]},
                    {"alignmentConditions": [condition()], "conditions": [condition()], "join": "any"}):
        with pytest.raises(ValueError):
            plan_with(refused)
    with pytest.raises(ValueError):
        plan_with({"alignmentConditions": [condition(timeframe=900)], "conditions": [condition(timeframe=900)]}).check_executable()
    with pytest.raises(ValueError, match="复位"):
        plan_with({"conditions": [condition()], "rearm": [condition(timeframe=900)]}).check_executable()

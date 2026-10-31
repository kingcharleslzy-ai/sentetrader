"""策略积木·第三期 A (2026-09-25): the second batch of blocks in the engine.

Indicators against hand-worked or reference figures (CCI, Wilder ADX / DI, parabolic SAR, pivot and
the R-Breaker lines of TqSdk's demo, 当日最高 / 最低, open-interest change and quadrants), the backtest's
whole-series computation against the plain live windows, 连续 N 根 / 上升下降, 复位, 保本 and 分段止盈 in
the backtester, and every decision of a strategy using them equal to the live judgement.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from test_backtest_template_parity import CST, FEES, RB_SCHEDULE, parity_history
from test_strategy_backtest import cond, gapless, plan, run, series
from trader_runtime.data_providers import fake_market_history
from trader_runtime.strategy_backtest import DAILY_WINDOW, LIVE_WINDOW, BacktestSetup, run_backtest
from trader_runtime.strategy_engine import MarketContext, PositionState, breakeven_stop, decide
from trader_runtime.strategy_rules import (
    Condition,
    Operand,
    SeriesContext,
    SeriesFacts,
    SeriesWindow,
    StrategyPlan,
    StrategyRuleError,
    describe_condition,
    describe_operand,
    evaluate_completed,
    evaluate_rules,
    indicator_values,
    plan_feature_lines,
    session_history,
)
from trader_runtime.trading_calendar import bar_trading_day, daily_bar_day, daily_index

NS = 1_000_000_000


def values(bars: list[dict[str, Any]], context: SeriesContext | None = None, **operand: Any) -> list[float | None]:
    return indicator_values(bars, Operand.model_validate({"timeframe": 300, **operand}), context)


def rounded(items: list[float | None], digits: int = 4) -> list[float | None]:
    return [None if item is None else round(item, digits) for item in items]


# -- indicators ----------------------------------------------------------------------------------------

def test_cci_matches_the_hand_calculation():
    # H = L = C, so TP is the close: [10, 11, 13, 12]. Bar 2: mean 11.333, mean deviation 1.111 →
    # (13 − 11.333) ÷ (0.015 × 1.111) = 100. Bar 3: mean 12, TP 12 → 0.
    bars = series([(c, c, c, c) for c in (10, 11, 13, 12)])
    assert rounded(values(bars, indicator="cci", period=3)) == [None, None, 100.0, 0.0]
    assert values(bars, indicator="cci")[-1] is None          # default N = 20 needs 20 bars


def _wilder_sum(inputs: list[float], n: int) -> float:
    total = sum(inputs[:n])                  # Wilder's own bookkeeping: running sums, prior − prior/n + x
    for x in inputs[n:]:
        total = total - total / n + x
    return total


def _reference_di(bars: list[dict[str, Any]], i: int, n: int, history: int = 100) -> tuple[float, float]:
    rows = []
    for j in range(i - history + 1, i + 1):
        high, low = bars[j]["high"], bars[j]["low"]
        up, down = high - bars[j - 1]["high"], bars[j - 1]["low"] - low
        close = bars[j - 1]["close"]
        rows.append((up if up > down and up > 0 else 0.0, down if down > up and down > 0 else 0.0,
                     max(high - low, abs(high - close), abs(low - close))))
    plus, minus, true_range = (_wilder_sum([row[k] for row in rows], n) for k in range(3))
    return 100 * plus / true_range, 100 * minus / true_range


def _reference_adx(bars: list[dict[str, Any]], i: int, n: int, history: int = 100) -> float:
    dx = []
    for j in range(i - history + 1, i + 1):
        plus, minus = _reference_di(bars, j, n, history)
        dx.append(100 * abs(plus - minus) / (plus + minus) if plus + minus else 0.0)
    average = sum(dx[:n]) / n
    for x in dx[n:]:
        average = (average * (n - 1) + x) / n
    return average


def _whole_history_adx(bars: list[dict[str, Any]], n: int) -> float:
    """The textbook ADX seeded at the first bar of the whole series."""
    rows = []
    for j in range(1, len(bars)):
        high, low = bars[j]["high"], bars[j]["low"]
        up, down = high - bars[j - 1]["high"], bars[j - 1]["low"] - low
        close = bars[j - 1]["close"]
        rows.append((up if up > down and up > 0 else 0.0, down if down > up and down > 0 else 0.0,
                     max(high - low, abs(high - close), abs(low - close))))
    sums = [sum(row[k] for row in rows[:n]) for k in range(3)]
    dx = []
    for index in range(n, len(rows) + 1):
        if index > n:
            sums = [total - total / n + rows[index - 1][k] for k, total in enumerate(sums)]
        plus, minus = 100 * sums[0] / sums[2], 100 * sums[1] / sums[2]
        dx.append(100 * abs(plus - minus) / (plus + minus) if plus + minus else 0.0)
    return _wilder_sum(dx, n) / n


def test_adx_and_directional_indices_follow_wilder_over_a_fixed_history():
    bars = fake_market_history("SHFE.rb2610", 300, 600)["bars"]
    adx, plus, minus = (values(bars, indicator=name) for name in ("adx", "plus_di", "minus_di"))
    assert adx[198] is None and adx[199] is not None and plus[99] is None and plus[100] is not None
    for i in (199, 300, 599):
        assert adx[i] == pytest.approx(_reference_adx(bars, i, 14), abs=1e-9)
        assert (plus[i], minus[i]) == pytest.approx(_reference_di(bars, i, 14), abs=1e-9)
    assert (round(adx[-1], 4), round(plus[-1], 4), round(minus[-1], 4)) == (47.3213, 16.0385, 14.5415)
    # The fixed 100-bar history stays within a hair of the whole-history ADX (47.3143 here).
    assert abs(adx[-1] - _whole_history_adx(bars, 14)) < 0.05
    with pytest.raises(ValidationError, match="ADX / DI 周期最多 50"):
        Operand(indicator="adx", period=60)


def test_parabolic_sar_matches_the_hand_steps():
    # (high, low, close). Bar 1 closes up: rising, SAR seeded at bar 0's low 9 with EP 11, AF 0.02.
    # Bar 2: 9 + 0.02 × 2 = 9.04, capped by the lows before → 9; new high 12 → EP 12, AF 0.04.
    # Bar 3: 9 + 0.04 × 3 = 9.12; EP 12.5, AF 0.06. Bar 4: 9.12 + 0.06 × 3.38 = 9.3228.
    # Bar 5: 9.3228 + 0.06 × 3.1772 = 9.5134 > low 9 → reverses: SAR = EP 12.5, EP 9, AF 0.02.
    # Bar 6: 12.5 + 0.02 × (9 − 12.5) = 12.43; new low 8.5.
    rows = [(10, 9, 9.5), (11, 9.5, 10.5), (12, 10.5, 11.8), (12.5, 11.5, 12.2), (12.4, 10, 10.2), (10.5, 9, 9.2),
            (9.8, 8.5, 8.6)]
    bars = series([(close, high, low, close) for high, low, close in rows])
    assert rounded(values(bars, indicator="sar")) == [None, 9.0, 9.0, 9.12, 9.3228, 12.5, 12.43]


def _daily(day: str, high: float, low: float, close: float) -> dict[str, Any]:
    midnight = int(datetime.fromisoformat(day).replace(tzinfo=CST).timestamp()) * NS
    return {"datetime": midnight, "open": (high + low) / 2, "high": high, "low": low, "close": close,
            "volume": 1, "tradingDay": day}


def test_pivot_levels_are_the_r_breaker_lines_of_the_previous_trading_day():
    daily = [_daily("2026-09-23", 110, 100, 106), _daily("2026-09-24", 120, 90, 95)]
    minute = series([(107, 108, 106, 107)], start=int(datetime(2026, 9, 24, 9, 0, tzinfo=CST).timestamp()) * NS, step=60)
    context = SeriesContext(daily=daily)
    levels = {level: values(minute, context, indicator="pivot", value=level)[0] for level in range(-3, 4)}
    high, low, close = 110, 100, 106                       # tqsdk/demo/example/rbreaker.py get_index_line
    pivot = (high + low + close) / 3
    tqsdk = {"b_break": high + 2 * (pivot - low), "s_setup": pivot + (high - low), "s_enter": 2 * pivot - low,
             "b_enter": 2 * pivot - high, "b_setup": pivot - (high - low), "s_break": low - 2 * (high - pivot)}
    assert levels[0] == pytest.approx(pivot) and round(pivot, 4) == 105.3333
    assert [levels[3], levels[2], levels[1], levels[-1], levels[-2], levels[-3]] == pytest.approx(
        [tqsdk[k] for k in ("b_break", "s_setup", "s_enter", "b_enter", "b_setup", "s_break")])
    assert rounded([levels[k] for k in (3, 2, 1, -1, -2, -3)]) == [120.6667, 115.3333, 110.6667, 100.6667, 95.3333, 90.6667]
    assert describe_operand(Operand(indicator="pivot", value=3)) == "枢轴R3（突破买入价）"
    with pytest.raises(ValidationError, match="枢轴位"):
        Operand(indicator="pivot", value=4)


def test_session_high_and_low_run_from_the_trading_days_first_bar():
    def bar(when: datetime, high: float, low: float, day: str) -> dict[str, Any]:
        return {"datetime": int(when.timestamp()) * NS, "open": low, "high": high, "low": low, "close": high,
                "volume": 1, "tradingDay": day}
    at = lambda d, h, m: datetime(2026, 9, d, h, m, tzinfo=CST)  # noqa: E731
    bars = [bar(at(23, 21, 0), 101, 99, "2026-09-24"), bar(at(23, 21, 1), 103, 100, "2026-09-24"),
            bar(at(24, 9, 0), 102, 97, "2026-09-24"),          # the night session belongs to the next day
            bar(at(24, 21, 0), 98, 96, "2026-09-25"), bar(at(24, 21, 1), 99, 95, "2026-09-25")]
    context = SeriesContext(schedule=RB_SCHEDULE)
    assert values(bars, context, indicator="session_high") == [101, 103, 103, 98, 99]
    assert values(bars, context, indicator="session_low") == [99, 99, 97, 96, 95]
    # A series starting inside a day cannot know that day's extreme; the next day starts afresh.
    assert values(bars[1:], context, indicator="session_high") == [None, None, 98, 99]
    assert values(bars, None, indicator="session_high")[:3] == [None] * 3   # no schedule: no session bell


# 沪金式交易时段：夜盘 21:00–02:30（330 根）+ 日盘 225 根 = 一个交易日 555 根 1 分钟线，远超 255 根的实盘窗口。
AU_SCHEDULE = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
               "night": [["21:00:00", "26:30:00"]]}


def au_minutes(days: int, *, seed: int = 3) -> list[dict[str, Any]]:
    """`days` weekday trading days of 1-minute bars ending 2026-09-24, each with its night session on
    the previous weekday's evening. No tradingDay annotation (like the sidecar's): the calendar rule."""
    import random
    from datetime import date, timedelta
    rng, price, bars = random.Random(seed), 600.0, []
    day, trading = date(2026, 9, 24), []
    while len(trading) < days:
        if day.weekday() < 5:
            trading.append(day)
        day -= timedelta(days=1)
    for current in reversed(trading):
        previous = current - timedelta(days=3 if current.weekday() == 0 else 1)
        periods = [(datetime(previous.year, previous.month, previous.day, 21, 0, tzinfo=CST), 330)]
        periods += [(datetime(current.year, current.month, current.day, h, m, tzinfo=CST), length)
                    for h, m, length in ((9, 0, 75), (10, 30, 60), (13, 30, 90))]
        for start, length in periods:
            for minute in range(length):
                opening = price
                price = round(price + rng.choice((-1, 0, 1)) * rng.random() * 1.5, 2)
                bars.append({"datetime": int(start.timestamp()) * NS + minute * 60 * NS, "open": opening,
                             "high": max(opening, price) + round(rng.random(), 2),
                             "low": min(opening, price) - round(rng.random(), 2), "close": price, "volume": 10})
    return bars


def test_session_high_and_low_reach_back_to_the_days_first_bar_beyond_the_live_window():
    bars = au_minutes(3)
    first = 2 * 555                                                   # the third trading day's first bar
    facts = SeriesFacts(bars)
    for nth in (300, 500):
        k = first + nth - 1
        today = bars[first:k + 1]
        reference = (max(bar["high"] for bar in today), min(bar["low"] for bar in today))
        live_window = bars[k + 1 - LIVE_WINDOW:k + 1]
        history = bars[:k + 1][-1_500:]                                 # the provider's read-only history
        stitched = [bar for bar in history if bar["datetime"] < live_window[0]["datetime"]] + live_window
        for name, expected in zip(("session_high", "session_low"), reference, strict=True):
            operand = Operand(indicator=name, timeframe=60)
            live = indicator_values(live_window, operand, SeriesContext(schedule=AU_SCHEDULE, extended={60: stitched}))
            backtest = indicator_values(SeriesWindow(facts, k + 1 - LIVE_WINDOW, k + 1), operand,
                                        SeriesContext(schedule=AU_SCHEDULE, extended={60: facts.bars}))
            assert live[-1] == backtest[-1] == expected, (nth, name)
            assert live == backtest
            # The window alone cannot see the day's first bar: that is what the extension is for.
            assert indicator_values(live_window, operand, SeriesContext(schedule=AU_SCHEDULE))[-1] is None


class _HistoryProvider:
    def __init__(self, bars: list[dict[str, Any]], *, fail: bool = False) -> None:
        self.bars, self.fail, self.reads = bars, fail, 0

    def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        self.reads += 1
        if self.fail:
            raise RuntimeError("market_history_timeout")
        return {"bars": self.bars[-length:]}


def session_plan() -> StrategyPlan:
    near = lambda name, factor: _operand(name, offset={"factor": factor, "indicator": "atr"})  # noqa: E731
    return StrategyPlan.model_validate({
        "name": "当日新高突破", "description": "收盘贴近当日最高开多、贴近当日最低开空", "direction": "both",
        "timeframeSeconds": 60, "maxPosition": 1, "stopLossTicks": 60, "takeProfitTicks": 80, "cooldownBars": 0,
        "maxLoss": 1e8, "maxOrdersPerDay": 50, "maxHoldBars": 30,
        "rules": {"entryLong": {"conditions": [_condition("当日新高", _operand("close"), "gte", near("session_high", -0.2))]},
                  "entryShort": {"conditions": [_condition("当日新低", _operand("close"), "lte", near("session_low", 0.2))]}}})


def test_session_extremes_judge_every_bar_like_live_with_the_history_read():
    """每根K线：回测（整序列）== 实盘（255 根窗口 + 历史读取拼接）的判定，含交易日第 300、500 根之后。"""
    bars = au_minutes(3)
    p = session_plan()
    seen: list[tuple[int, Any, Any]] = []
    result = run_backtest(BacktestSetup(plan=p, symbol="SHFE.au2612", series={60: bars}, price_tick=0.02,
                                        volume_multiple=1000, fee_schedule=FEES, schedule=AU_SCHEDULE),
                          trace=lambda k, state, decision: seen.append((k, state, decision)))
    in_day = lambda k: (k % 555) + 1  # noqa: E731
    assert any(in_day(bars.index(next(b for b in bars if b["datetime"] == t["openTime"]))) > 300
               for t in result["trades"]), "no entry late in a trading day"
    judged = 0
    for k, state, decision in seen:
        if decision.fillPrice is not None:
            continue
        forming = {**bars[k], "datetime": bars[k]["datetime"] + 60 * NS}
        workbenches = {60: {"bars": bars[max(0, k + 1 - LIVE_WINDOW):k + 1] + [forming],
                            "quote": {"tradingSchedule": AU_SCHEDULE}}}
        provider = _HistoryProvider(bars[:k + 1] + [forming])
        extended, pending = session_history(provider, p, "SHFE.au2612", workbenches)
        # Read only when the window lies inside one day and does not start on its bell.
        assert not pending and (extended is not None) == (in_day(k) > LIVE_WINDOW), k
        live = decide(p, evaluate_rules(p, workbenches, extended=extended), state,
                      MarketContext(price_tick=0.02, volume_multiple=1000, equity=None))
        assert (live.action, live.volume, live.exitReason, live.stopPrice, live.reason) == \
            (decision.action, decision.volume, decision.exitReason, decision.stopPrice, decision.reason), k
        judged += 1
    assert judged > 1_300
    # History not there yet (or not reaching the window): 数据未就绪, the caller judges no rule this time.
    late = 2 * 555 + 499
    workbenches = {60: {"bars": bars[late + 1 - LIVE_WINDOW:late + 2], "quote": {"tradingSchedule": AU_SCHEDULE}}}
    assert session_history(_HistoryProvider(bars, fail=True), p, "SHFE.au2612", workbenches) == (None, True)
    assert session_history(object(), p, "SHFE.au2612", workbenches) == (None, True)
    old = _HistoryProvider(bars[:late + 1 - LIVE_WINDOW - 30])            # an old read ending before the window
    assert session_history(old, p, "SHFE.au2612", workbenches) == (None, True)


def test_open_interest_change_and_the_four_quadrants():
    bars = series([(c, c + 1, c - 1, c) for c in (100, 101, 100, 101, 100, 100)])
    for bar, interest in zip(bars, (10, 12, 14, 13, 12, 13), strict=True):
        bar["openInterest"] = interest
    assert values(bars, indicator="oi_change") == [None, 2, 2, -1, -1, 1]
    assert values(bars, indicator="oi_change", period=2) == [None, None, 4, 1, -2, 0]
    # 价涨仓增 多头增仓 1, 价跌仓增 空头增仓 2, 价涨仓减 空头减仓 3, 价跌仓减 多头减仓 4, 价平 0.
    assert values(bars, indicator="oi_quadrant") == [None, 1, 2, 3, 4, 0]
    del bars[3]["openInterest"]
    assert values(bars, indicator="oi_change")[3:5] == [None, None]
    quadrant = cond("多头增仓", "oi_quadrant", "eq", {"indicator": "constant", "value": 1})
    assert describe_condition(Condition.model_validate(quadrant)) == "持仓四象限 = 多头增仓"
    for bad in (cond("x", "oi_quadrant", "gt", {"indicator": "constant", "value": 1}),
                cond("x", "close", "eq", {"indicator": "constant", "value": 1}),
                cond("x", "oi_quadrant", "eq", {"indicator": "constant", "value": 5})):
        with pytest.raises(ValidationError, match="持仓四象限"):
            Condition.model_validate(bad)


NEW_INDICATORS = [
    {"indicator": "cci"}, {"indicator": "cci", "period": 14, "average": 3}, {"indicator": "adx"},
    {"indicator": "plus_di", "period": 10}, {"indicator": "minus_di"}, {"indicator": "sar"},
    {"indicator": "session_high"}, {"indicator": "session_low"}, {"indicator": "pivot", "value": 3},
    {"indicator": "pivot", "value": -2}, {"indicator": "oi_change", "period": 3}, {"indicator": "oi_quadrant"},
    {"indicator": "sar", "offset": {"factor": 0.5, "indicator": "atr"}},
]


def _with_open_interest(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**bar, "openInterest": 10_000 + (i * 37) % 211 - (i % 13) * 5} for i, bar in enumerate(bars)]


def test_backtest_windows_compute_the_new_indicators_like_plain_live_lists():
    history = parity_history()
    minutes, daily = _with_open_interest(history[60][-700:]), history[86400]
    facts, daily_facts = SeriesFacts(minutes), SeriesFacts(daily)
    windows = [(0, 1), (0, 12), (0, 255), (100, 355), (300, 555), (445, 700), (690, 700)]
    windows += [(end - 255, end) for end in range(400, 700)]            # sliding by one, as a backtest does
    for start, end in windows:
        window = SeriesWindow(facts, start, end)
        day = bar_trading_day(minutes[end - 1], daily_index(daily)[0])
        daily_end = sum(1 for bar in daily if daily_bar_day(bar) <= day)
        daily_window = SeriesWindow(daily_facts, max(0, daily_end - DAILY_WINDOW), daily_end)
        for raw in NEW_INDICATORS:
            operand = Operand.model_validate({**raw, "timeframe": 60})
            plain = indicator_values(list(window), operand, SeriesContext(daily=list(daily_window), schedule=RB_SCHEDULE))
            fast = indicator_values(window, operand, SeriesContext(daily=daily_window, schedule=RB_SCHEDULE))
            assert fast == plain, (raw, start, end)
            assert any(item is not None for item in plain) or end - start < 200 or raw["indicator"].startswith("session")


# -- conditions -----------------------------------------------------------------------------------------

def _first_entry(p: StrategyPlan, bars: list[dict[str, Any]]) -> int | None:
    """The first bar whose close confirms the entry (bars before the required history are not judged)."""
    for end in range(1, len(bars) + 1):
        try:
            if evaluate_completed(p, {300: bars[:end]})["rules"]["entryLong"]:
                return end - 1
        except StrategyRuleError:
            continue
    return None


def test_consecutive_bars_and_rising_falling():
    #          0    1    2    3   4    5    6    7    8    9
    closes = [100, 100, 101, 99, 101, 102, 103, 102, 101, 100]
    bars = series(gapless(closes))
    above = cond("收盘在 100.5 上方", "close", "gt", {"indicator": "constant", "value": 100.5})
    assert _first_entry(plan(entry=[above]), bars) == 2
    assert _first_entry(plan(entry=[{**above, "consecutive": 3}]), bars) == 6       # bars 4, 5, 6
    rising = cond("收盘较 2 根前上升", "close", "rising", {"indicator": "constant", "value": 2})
    assert _first_entry(plan(entry=[rising]), bars) == 5                            # 102 > 99 (bar 4: 101 = 101)
    step_up = cond("收盘较 1 根前上升", "close", "rising", {"indicator": "constant", "value": 1})
    assert _first_entry(plan(entry=[{**step_up, "consecutive": 3}]), bars) == 6     # 101>99, 102>101, 103>102
    falling = cond("收盘较 1 根前下降", "close", "falling", {"indicator": "constant", "value": 1})
    assert _first_entry(plan(entry=[{**falling, "consecutive": 3}]), bars) == 9      # 102, 101, 100
    assert describe_condition(Condition.model_validate({**rising, "consecutive": 3})) == "收盘价 较2根前上升（连续 3 根）"
    with pytest.raises(ValidationError, match="交叉是一次性事件"):
        Condition.model_validate({**cond("x", "close", "cross_up", {"indicator": "ma", "period": 5}), "consecutive": 2})
    with pytest.raises(ValidationError, match="上升 / 下降比较"):
        Condition.model_validate(cond("x", "close", "rising", {"indicator": "ma", "period": 5}))
    with pytest.raises(ValidationError, match="上升 / 下降比较"):
        Condition.model_validate(cond("x", "close", "rising", {"indicator": "constant", "value": 1.5}))


# -- 复位 -----------------------------------------------------------------------------------------------

REARM_CLOSES = [110, 110, 110, 99, 103, 99, 104, 99, 106, 99, 103, 103]
BELOW_100 = [cond("跌破 100", "close", "lt", {"indicator": "constant", "value": 100})]
ABOVE_102 = [cond("回到 102 上方", "close", "gt", {"indicator": "constant", "value": 102})]


def test_rearm_holds_a_direction_until_its_condition_held_once():
    bars = series(gapless(REARM_CLOSES))
    plain = run(plan(entry=BELOW_100, exit_=ABOVE_102), bars)["trades"]
    assert [bars.index(next(b for b in bars if b["datetime"] == t["openTime"])) for t in plain] == [4, 6, 8, 10]
    rearm = [cond("回到 105 上方才复位", "close", "gt", {"indicator": "constant", "value": 105})]
    p = plan(entry=BELOW_100, exit_=ABOVE_102)
    p = StrategyPlan.model_validate({**p.model_dump(), "rules": {**p.model_dump()["rules"],
                                     "entryLong": {"conditions": BELOW_100, "rearm": rearm}}})
    seen: list[tuple[int, Any]] = []
    held = run_backtest(BacktestSetup(plan=p, symbol="SHFE.rb2610", series={300: bars}, price_tick=1, volume_multiple=10,
                                      fee_schedule=FEES, initial_balance=100_000),
                        trace=lambda k, state, decision: seen.append((k, decision)))["trades"]
    # Opened on bar 3's signal; the signals of bars 5 and 7 wait (no close above 105 since); bar 8's
    # 106 rearms, so bar 9's signal opens again.
    assert [bars.index(next(b for b in bars if b["datetime"] == t["openTime"])) for t in held] == [4, 10]
    blocked = {k: decision.reason for k, decision in seen if decision.blocked}
    assert set(blocked) == {5, 7} and "复位条件「收盘价 > 105」还没成立" in blocked[5]
    assert ("复位", "开多后须等「收盘价 > 105」成立一次才再开多") in plan_feature_lines(p)


def test_decide_blocks_an_entry_until_a_rearm_hit_after_the_latch():
    p = StrategyPlan.model_validate({**plan().model_dump(), "rules": {"entryLong": {
        "conditions": BELOW_100, "rearm": [cond("复位", "close", "gt", {"indicator": "constant", "value": 105})]}}})
    evaluation = {"rules": {"entryLong": True, "exitLong": False, "entryShort": False, "exitShort": False},
                  "engine": {"barTime": "500", "barClose": 800, "close": 99.0, "rearm": {"entryLong": 300}}}
    market = MarketContext(price_tick=1, volume_multiple=10, equity=100_000)
    assert decide(p, evaluation, PositionState(), market).action == "open_long"                 # never opened
    assert decide(p, evaluation, PositionState(last_open_long=200), market).action == "open_long"  # rearmed at 300
    waiting = decide(p, evaluation, PositionState(last_open_long=300), market)
    assert waiting.action == "hold" and "复位" in waiting.reason
    assert decide(p, evaluation, PositionState(last_open_long=500), market).action == "open_long"  # the latch's own bar
    assert decide(p, {**evaluation, "engine": {**evaluation["engine"], "rearm": {"entryLong": None}}},
                  PositionState(last_open_long=100), market).action == "hold"
    with pytest.raises(ValidationError):   # exits have no rearm
        StrategyPlan.model_validate({**p.model_dump(), "rules": {**p.model_dump()["rules"],
                                     "exitLong": {"conditions": ABOVE_102, "rearm": BELOW_100}}})
    fixed = {**p.model_dump(), "timeframeSeconds": 300}
    fixed["rules"]["entryLong"]["rearm"][0]["left"]["timeframe"] = 900
    fixed["rules"]["entryLong"]["rearm"][0]["right"]["timeframe"] = 900
    with pytest.raises(ValueError, match="复位条件须使用执行周期"):
        StrategyPlan.model_validate(fixed).check_executable()


# -- 保本 / 分段止盈 --------------------------------------------------------------------------------------

def test_breakeven_moves_the_stop_to_the_entry_and_only_tightens():
    p = plan(exit_=[cond("从不", "close", "lt", {"indicator": "constant", "value": 0})], stopLossTicks=10,
             maxOrdersPerDay=1, breakeven={"triggerTicks": 4, "offsetTicks": 1})
    # The first judged bar is bar 1: entry at bar 2's open 100 (stop 90). Bar 3's high 105 is 5 ticks in
    # profit → stop 101; bar 4's low hits it.
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 102, 99, 101), (101, 105, 101, 104),
            (104, 104, 100, 100), (100, 100, 100, 100)]
    trade = run(p, series(rows))["trades"][0]
    assert (trade["openPrice"], trade["closePrice"], trade["exitReason"], trade["exitLabel"]) == (100, 101, "breakeven", "保本止损")
    # With a 2-tick trail as well the trail's 103 is tighter than breakeven's 101, and it stands.
    both = plan(exit_=[cond("从不", "close", "lt", {"indicator": "constant", "value": 0})], stopLossTicks=10,
                maxOrdersPerDay=1, breakeven={"triggerTicks": 4, "offsetTicks": 1}, trailingStop={"mode": "ticks", "value": 2})
    trade = run(both, series(rows))["trades"][0]
    assert (trade["closePrice"], trade["exitReason"]) == (103, "trailing_stop")
    # The pure function: ATR trigger from the frozen ATR, never loosening an already tighter stop.
    config = {"triggerAtr": 2, "offsetTicks": 0}
    assert breakeven_stop(config, long=True, entry_price=100, extreme=102.5, price_tick=0.5, exit_atr=1.5) is None
    assert float(breakeven_stop(config, long=True, entry_price=100, extreme=103, price_tick=0.5, exit_atr=1.5)) == 100
    assert float(breakeven_stop(config, long=True, entry_price=100, extreme=103, price_tick=0.5, current_stop=101,
                                exit_atr=1.5)) == 101
    assert float(breakeven_stop({"triggerTicks": 4, "offsetTicks": 2}, long=False, entry_price=100, extreme=97.5,
                                price_tick=0.5, current_stop=104)) == 99
    with pytest.raises(ValidationError, match="保本"):
        StrategyPlan.model_validate({**p.model_dump(), "breakeven": {"triggerTicks": 4, "triggerAtr": 1}})
    with pytest.raises(ValidationError, match="offsetTicks 小于 triggerTicks"):
        StrategyPlan.model_validate({**p.model_dump(), "breakeven": {"triggerTicks": 4, "offsetTicks": 4}})


def test_time_take_profit_uses_the_tier_for_the_bars_held_once_per_position():
    never = [cond("从不", "close", "lt", {"indicator": "constant", "value": 0})]
    tiers = [{"afterBars": 0, "profitTicks": 20}, {"afterBars": 3, "profitTicks": 5}, {"afterBars": 6, "profitTicks": 0}]
    p = plan(exit_=never, stopLossTicks=50, maxOrdersPerDay=5, timeTakeProfit=tiers)
    # Entry at bar 2's open 100. Held 1, 2 bars: the 20-tick tier; bar 4 closes 106 after 3 bars → 5-tick tier met.
    closes = [100, 100, 102, 103, 106, 106, 100, 100, 100, 100, 100, 100, 101, 101]
    result = run(p, series(gapless(closes, first_open=100)))
    first = result["trades"][0]
    assert (first["openPrice"], first["closePrice"], first["barsHeld"], first["exitReason"], first["exitLabel"]) == (
        100, 106, 3, "time_take_profit", "分段止盈")
    # The next position (opened at bar 5's open 106) is under water until bar 11 — after 6 bars the
    # zero-profit tier waits for the close to reach the entry: 106 is never regained, so it stays open.
    assert [t["exitReason"] for t in result["trades"]] == ["time_take_profit", "end_of_data"]
    assert ("分段止盈", "K线收盘时判断：持仓满 0 根起浮盈 ≥ 20 跳；持仓满 3 根起浮盈 ≥ 5 跳；持仓满 6 根起浮盈 ≥ 0 跳，达到就平仓") \
        in plan_feature_lines(p)
    with pytest.raises(ValidationError, match="不能重复"):
        StrategyPlan.model_validate({**p.model_dump(), "timeTakeProfit": [tiers[0], tiers[0]]})
    # An ATR tier needs the signal bar's ATR14: without enough bars it does not open.
    atr = plan(exit_=never, timeTakeProfit=[{"afterBars": 0, "profitAtr": 1}])
    assert run(atr, series(gapless([100] * 10)))["trades"] == []


# -- backtest == live, bar by bar ------------------------------------------------------------------------

def _operand(name: str, **params: Any) -> dict[str, Any]:
    return {"indicator": name, "timeframe": 60, **params}


def _condition(label: str, left: dict[str, Any], operator: str, right: dict[str, Any], **params: Any) -> dict[str, Any]:
    return {"label": label, "left": left, "operator": operator, "right": right, **params}


def blocks_plan() -> StrategyPlan:
    constant = lambda value: {"indicator": "constant", "value": value}  # noqa: E731
    return StrategyPlan.model_validate({
        "name": "第二批积木", "description": "SAR 顺势 + ADX 过滤 + 枢轴方向 + 复位 + 保本 + 分段止盈",
        "direction": "both", "timeframeSeconds": 60, "maxPosition": 1, "stopLossTicks": 30, "takeProfitTicks": None,
        "cooldownBars": 0, "maxLoss": 1e8, "maxOrdersPerDay": 50,
        "breakeven": {"triggerAtr": 1.5, "offsetTicks": 1},
        "timeTakeProfit": [{"afterBars": 0, "profitAtr": 6}, {"afterBars": 40, "profitTicks": 3}],
        "rules": {
            "entryLong": {"conditions": [
                _condition("站上 SAR", _operand("close"), "gt", _operand("sar"), consecutive=2),
                _condition("ADX 有趋势", _operand("adx"), "gt", constant(15)),
                _condition("在枢轴上方", _operand("close"), "gt", _operand("pivot", value=0)),
                _condition("CCI 上升", _operand("cci"), "rising", constant(2))],
                "rearm": [_condition("回到 EMA 下方复位", _operand("close"), "lt", _operand("ema", period=20))]},
            "exitLong": {"conditions": [_condition("跌破 SAR", _operand("close"), "lt", _operand("sar"))]},
            "entryShort": {"conditions": [
                _condition("跌破 SAR", _operand("close"), "lt", _operand("sar"), consecutive=2),
                _condition("ADX 有趋势", _operand("adx"), "gt", constant(15)),
                _condition("在枢轴下方", _operand("close"), "lt", _operand("pivot", value=0)),
                _condition("当日低点附近", _operand("close"), "lte", _operand("session_low", offset={
                    "factor": 1, "indicator": "atr"}))],
                "rearm": [_condition("回到 EMA 上方复位", _operand("close"), "gt", _operand("ema", period=20))]},
            "exitShort": {"conditions": [_condition("站上 SAR", _operand("close"), "gt", _operand("sar"))]},
        }})


def test_second_batch_blocks_judge_every_bar_like_live():
    """每根K线：回测的判定 == 实盘在同一时刻的 evaluate_rules（带正在形成的K线）+ decide。"""
    history = parity_history()
    p = blocks_plan()
    base = history[60][-2_400:]
    day_count = len({bar["tradingDay"] for bar in base})
    daily_series = history[86400][-(day_count + DAILY_WINDOW + 2):]
    setup = BacktestSetup(plan=p, symbol="SHFE.rb2701", series={60: base, 86400: daily_series}, price_tick=1,
                          volume_multiple=10, fee_schedule=FEES, initial_balance=1_000_000, schedule=RB_SCHEDULE)
    seen: list[tuple[int, Any, Any]] = []
    result = run_backtest(setup, trace=lambda k, state, decision: seen.append((k, state, decision)))
    reasons = {trade["exitReason"] for trade in result["trades"]}
    assert result["summary"]["trades"] >= 5 and {"rule", "breakeven", "time_take_profit"} <= reasons, reasons
    assert any("复位条件" in decision.reason for _, _, decision in seen)
    judged = 0
    for k, state, decision in seen:
        if decision.fillPrice is not None:
            continue
        cutoff = base[k]["datetime"] + 60 * NS
        forming = {**base[k], "datetime": cutoff}
        daily = [b for b in daily_series if b["tradingDay"] <= base[k]["tradingDay"]][-DAILY_WINDOW:]
        workbenches = {60: {"bars": base[max(0, k + 1 - LIVE_WINDOW):k + 1] + [forming],
                            "quote": {"tradingSchedule": RB_SCHEDULE}},
                       86400: {"bars": daily, "quote": {}}}
        extended, _ = session_history(_HistoryProvider(base[:k + 1] + [forming]), p, "SHFE.rb2701", workbenches)
        evaluation = evaluate_rules(p, workbenches, extended=extended)
        equity = 1_000_000 + sum(t["net"] for t in result["trades"] if t["closeTime"] <= base[k]["datetime"])
        live = decide(p, evaluation, state, MarketContext(price_tick=1, volume_multiple=10, equity=equity))
        assert (live.action, live.volume, live.exitReason, live.exitKey, live.stopPrice, live.reason, live.exitAtr) == \
            (decision.action, decision.volume, decision.exitReason, decision.exitKey, decision.stopPrice,
             decision.reason, decision.exitAtr), k
        judged += 1
    assert judged > 1_000


def test_ten_thousand_one_minute_bars_with_adx_sar_and_pivot_run_within_budget():
    import time
    constant = lambda value: {"indicator": "constant", "value": value}  # noqa: E731
    long = [_condition("ADX", _operand("adx"), "gt", constant(20)), _condition("SAR", _operand("close"), "gt", _operand("sar")),
            _condition("枢轴", _operand("close"), "gt", _operand("pivot", value=0))]
    short = [_condition("ADX", _operand("adx"), "gt", constant(20)), _condition("SAR", _operand("close"), "lt", _operand("sar")),
             _condition("枢轴", _operand("close"), "lt", _operand("pivot", value=0))]
    p = StrategyPlan.model_validate({"name": "性能", "description": "ADX+SAR+枢轴", "direction": "both",
        "timeframeSeconds": 60, "cooldownBars": 0, "maxLoss": 1e8, "maxOrdersPerDay": 50,
        "rules": {"entryLong": {"conditions": long}, "exitLong": {"conditions": short},
                  "entryShort": {"conditions": short}, "exitShort": {"conditions": long}}})
    setup = BacktestSetup(plan=p, symbol="SHFE.au2612", series={60: fake_market_history("SHFE.au2612", 60, 10_000)["bars"],
                                                               86400: fake_market_history("SHFE.au2612", 86400, 300)["bars"]},
                          price_tick=0.5, volume_multiple=1000, fee_schedule=FEES)
    started = time.perf_counter()
    result = run_backtest(setup)
    elapsed = time.perf_counter() - started
    print(f"10k 1m bars ADX+SAR+pivot: {elapsed:.2f}s, {result['summary']['trades']} trades")
    assert result["summary"]["trades"] > 10 and elapsed < 6   # target ≤ 1.5 s locally (~0.8 s); CI machines vary


def test_ten_thousand_one_minute_bars_with_rsi_rearm_run_within_budget(monkeypatch):
    from trader_runtime import strategy_rules
    constant = lambda value: {"indicator": "constant", "value": value}  # noqa: E731
    rsi = lambda operator, value: _condition(f"RSI {operator} {value}", _operand("rsi"), operator, constant(value))  # noqa: E731
    p = StrategyPlan.model_validate({"name": "RSI 迟滞", "description": "RSI14<30 多、>70 空，回到 35 / 65 复位",
        "direction": "both", "timeframeSeconds": 60, "cooldownBars": 0, "maxLoss": 1e8, "maxOrdersPerDay": 50,
        "stopLossTicks": 40, "takeProfitTicks": 40,
        "rules": {"entryLong": {"conditions": [rsi("lt", 30)], "rearm": [rsi("gt", 35)]},
                  "exitLong": {"conditions": [rsi("gt", 70)]},
                  "entryShort": {"conditions": [rsi("gt", 70)], "rearm": [rsi("lt", 65)]},
                  "exitShort": {"conditions": [rsi("lt", 30)]}}})
    setup = BacktestSetup(plan=p, symbol="SHFE.au2612", series={60: fake_market_history("SHFE.au2612", 60, 10_000)["bars"]},
                          price_tick=0.5, volume_multiple=1000, fee_schedule=FEES)
    # The budget is the work done, not the wall clock (a loaded runner stretched the old 6 s limit): the
    # engine hands the recursive RSI a trailing window of a few hundred bars per judged bar (measured
    # 2.5 M closes in total over 9.7 k windows, longest 255); a window growing with the series
    # (quadratic work) would be 50 M.
    window_sizes: list[int] = []
    real_rsi = strategy_rules._rsi
    monkeypatch.setattr(strategy_rules, "_rsi", lambda closes, period: (window_sizes.append(len(closes)), real_rsi(closes, period))[1])
    result = run_backtest(setup)
    assert result["summary"]["trades"] > 10
    assert window_sizes and max(window_sizes) <= 1_000 and sum(window_sizes) <= 10_000_000


def test_ten_thousand_one_minute_bars_with_session_extremes_run_within_budget():
    import time
    bars = au_minutes(18)                                          # 18 × 555 = 9 990 bars, nights to 02:30
    setup = BacktestSetup(plan=session_plan(), symbol="SHFE.au2612", series={60: bars}, price_tick=0.02,
                          volume_multiple=1000, fee_schedule=FEES, schedule=AU_SCHEDULE)
    started = time.perf_counter()
    result = run_backtest(setup)
    elapsed = time.perf_counter() - started
    print(f"10k 1m bars session_high/low: {elapsed:.2f}s, {result['summary']['trades']} trades")
    assert result["summary"]["trades"] > 10 and elapsed < 6


def test_old_definitions_validate_unchanged():
    raw = plan().model_dump()
    assert "rearm" in raw["rules"]["entryLong"] and raw["rules"]["entryLong"]["rearm"] == []
    legacy = {"name": "旧", "description": "旧定义", "rules": {"entryLong": {"conditions": [
        {"label": "金叉", "left": {"indicator": "ma", "timeframe": 300, "period": 5}, "operator": "cross_up",
         "right": {"indicator": "ma", "timeframe": 300, "period": 10}, "withinBars": 2}]}}}
    parsed = StrategyPlan.model_validate(legacy)
    assert parsed.breakeven is None and parsed.timeTakeProfit == [] and parsed.rules.entryLong.conditions[0].consecutive == 1

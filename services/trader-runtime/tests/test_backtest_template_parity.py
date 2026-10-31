"""规则回测性能优化的一致性保护 (2026-09-25).

A session-shaped history (SHFE rb: 21:00–23:00 night, three day periods, a holiday without its night
session, every bar annotated with its trading day like the sidecar's) runs every template. The trade
lists and summaries must equal the ones the unoptimised backtester produced on the same data
(``fixtures/backtest_template_parity.json``), and for the templates reading daily anchors / sessions
every decision must equal the live judgement (``evaluate_rules`` + ``decide``) on the live windows.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from trader_runtime.strategy_backtest import DAILY_WINDOW, LIVE_WINDOW, BacktestSetup, run_backtest
from trader_runtime.strategy_engine import MarketContext, bars_held, decide
from trader_runtime.strategy_rules import (
    Operand,
    SeriesContext,
    SeriesFacts,
    SeriesWindow,
    evaluate_completed,
    evaluate_rules,
    indicator_values,
    plan_timeframes,
)
from trader_runtime.strategy_rules import (
    session_history as live_session_history,
)
from trader_runtime.strategy_templates import build_plan, templates
from trader_runtime.trading_calendar import bar_trading_day, daily_bar_day, daily_index

CST = timezone(timedelta(hours=8))
NS = 1_000_000_000
RB_SCHEDULE = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
               "night": [["21:00:00", "23:00:00"]]}
DAY_PERIODS = ((9 * 60, 10 * 60 + 15), (10 * 60 + 30, 11 * 60 + 30), (13 * 60 + 30, 15 * 60))
NIGHT_PERIOD = (21 * 60, 23 * 60)
FEES = {"source": "custom", "default": {"mode": "per_lot", "open": 3, "closeToday": 3, "closeYesterday": 3}, "overrides": []}
GOLDEN = Path(__file__).parent / "fixtures" / "backtest_template_parity.json"


def _midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=CST).timestamp()) * NS


def trading_calendar(count: int, last: date = date(2026, 9, 24)) -> list[date]:
    """`count` weekdays ending on `last`, every 23rd weekday a holiday (no trading, no night before it)."""
    days: list[date] = []
    current, weekday_index = last, 0
    while len(days) < count:
        if current.weekday() < 5:
            if weekday_index % 23 != 11:
                days.append(current)
            weekday_index += 1
        current -= timedelta(days=1)
    return days[::-1]


def session_history(minute_days: int, daily_days: int, *, seed: int = 7) -> dict[int, list[dict[str, Any]]]:
    """1-minute bars for the last `minute_days` trading days (night session on the previous trading
    day's evening, dropped after a holiday), 5/15/60-minute bars aggregated from them, and a daily
    series of `daily_days` trading days whose last `minute_days` bars aggregate the minutes."""
    rng = random.Random(seed)
    calendar = trading_calendar(daily_days)
    price = 3500.0
    daily: list[dict[str, Any]] = []
    for day in calendar[:-minute_days]:
        opening = price + rng.randint(-15, 15)
        close = opening + rng.randint(-60, 60)
        high, low = max(opening, close) + rng.randint(0, 40), min(opening, close) - rng.randint(0, 40)
        daily.append({"datetime": _midnight(day), "open": opening, "high": high, "low": low, "close": close,
                      "volume": 100_000 + rng.randint(0, 50_000), "tradingDay": day.isoformat()})
        price = close
    minutes: list[dict[str, Any]] = []
    drift = 0.0
    for index in range(len(calendar) - minute_days, len(calendar)):
        day, previous = calendar[index], calendar[index - 1]
        opens: list[tuple[int, int]] = []
        if (day - previous).days == 1 or (day.weekday() == 0 and (day - previous).days == 3):
            opens.append((_midnight(previous) + NIGHT_PERIOD[0] * 60 * NS, NIGHT_PERIOD[1] - NIGHT_PERIOD[0]))
        for start, end in DAY_PERIODS:
            opens.append((_midnight(day) + start * 60 * NS, end - start))
        price += rng.choice((-1, 1)) * rng.randint(40, 70) if rng.random() < 0.5 else rng.randint(-20, 20)   # overnight gap
        drift = rng.choice((-0.6, -0.3, 0.0, 0.3, 0.6))
        for session_start, length in opens:
            for minute in range(length):
                if rng.random() < 0.02:
                    drift = rng.choice((-0.6, -0.3, 0.0, 0.3, 0.6))
                opening = price
                shock = rng.choice((-1, 1)) * rng.randint(12, 30) if rng.random() < 0.004 else 0
                close = round(opening + drift + shock + rng.gauss(0, 2.2))
                high = max(opening, close) + rng.randint(0, 3)
                low = min(opening, close) - rng.randint(0, 3)
                minutes.append({"datetime": session_start + minute * 60 * NS, "open": opening, "high": high, "low": low,
                                "close": close, "volume": 50 + rng.randint(0, 400), "tradingDay": day.isoformat()})
                price = close
    series: dict[int, list[dict[str, Any]]] = {60: minutes}
    for period in (300, 900, 3600):
        buckets: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for bar in minutes:
            buckets.setdefault((bar["tradingDay"], bar["datetime"] // (period * NS)), []).append(bar)
        series[period] = [_merge(group, group[0]["datetime"] // (period * NS) * period * NS) for group in buckets.values()]
    by_day: dict[str, list[dict[str, Any]]] = {}
    for bar in minutes:
        by_day.setdefault(bar["tradingDay"], []).append(bar)
    daily += [_merge(group, _midnight(date.fromisoformat(day))) for day, group in by_day.items()]
    series[86400] = daily
    return series


def _merge(group: list[dict[str, Any]], timestamp: int) -> dict[str, Any]:
    return {"datetime": timestamp, "open": group[0]["open"], "high": max(b["high"] for b in group),
            "low": min(b["low"] for b in group), "close": group[-1]["close"],
            "volume": sum(b["volume"] for b in group), "tradingDay": group[0]["tradingDay"]}


def template_setup(template_id: str, history: dict[int, list[dict[str, Any]]], bars: int | None = None, *,
                   timeframe: int | None = None,
                   leg_history: dict[int, list[dict[str, Any]]] | None = None) -> BacktestSetup:
    """The template at its default (or `timeframe`) period over the last `bars` execution bars, the other
    series cut the way ``load_series`` sizes them (the daily series keeps the last trading day's bar).
    A spread template takes the B leg's own history in `leg_history` and both legs are joined like
    ``load_spread_series`` does."""
    plan, _ = build_plan(templates()[template_id], timeframe=timeframe)
    timeframe = plan.timeframeSeconds
    base = history[timeframe][-(bars or PARITY_BARS[timeframe]):]
    series = {timeframe: base}

    def leg_slice(source: dict[int, list[dict[str, Any]]], other: int) -> list[dict[str, Any]]:
        """One leg's own bars of `other`, cut the way load_series / load_spread_series sizes them."""
        if other == timeframe:
            return source[other][-(bars or PARITY_BARS[other]):]
        if other == 86400:
            day_count = len({bar["datetime"] // (86400 * NS) for bar in base})
            return source[86400][-(day_count + DAILY_WINDOW + 2):]
        span = base[-1]["datetime"] - base[0]["datetime"]
        return source[other][-(span // (other * NS) + LIVE_WINDOW + 2):]

    if plan.spread is not None:
        from trader_runtime.strategy_spread import join_legs
        spread_raw: dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
        spread_indexes: dict[int, tuple[list[int], list[int]]] = {}
        for other in plan_timeframes(plan):
            own_a, own_b = leg_slice(history, other), leg_slice(leg_history or history, other)
            joined, ja, jb = join_legs(plan.spread, own_a, own_b)
            series[other] = joined
            spread_raw[other] = (own_a, own_b)
            spread_indexes[other] = (ja, jb)
        return BacktestSetup(plan=plan, symbol="SHFE.rb2701", series=series, price_tick=1, volume_multiple=10,
                             fee_schedule=FEES, initial_balance=1_000_000, schedule=RB_SCHEDULE,
                             spread_raw=spread_raw, spread_indexes=spread_indexes, leg_ticks=(1, 1),
                             leg_multiples=(10, 10))
    for other in plan_timeframes(plan) - {timeframe}:
        series[other] = leg_slice(history, other)
    return BacktestSetup(plan=plan, symbol="SHFE.rb2701", series=series, price_tick=1, volume_multiple=10,
                         fee_schedule=FEES, initial_balance=1_000_000, schedule=RB_SCHEDULE)


# Execution bars per period on the small fixture: enough to trade after the 254-bar warm-up.
PARITY_BARS = {60: 2_400, 300: 900, 900: 600, 3600: 640, 86400: 420}


def parity_history() -> dict[int, list[dict[str, Any]]]:
    return session_history(minute_days=40, daily_days=440)


def spread_histories() -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
    """The spread template's two legs (the same calendar, two price paths). Longer than
    ``parity_history``: the 1-hour clock needs more than 300 bars to trade after the warm-up."""
    return (session_history(minute_days=90, daily_days=490, seed=7),
            session_history(minute_days=90, daily_days=490, seed=11))


def unannotated(history: dict[int, list[dict[str, Any]]]) -> dict[int, list[dict[str, Any]]]:
    """No tradingDay on any bar: trading days come from the calendar rule and the daily series."""
    return {tf: [{k: v for k, v in bar.items() if k != "tradingDay"} for bar in bars] for tf, bars in history.items()}


def variants(history: dict[int, list[dict[str, Any]]]) -> dict[str, BacktestSetup]:
    """Every template at its default period, plus the anchor/session paths that only edge data reaches."""
    near, far = spread_histories()
    setups = {template_id: (template_setup(template_id, near, leg_history=far)
                            if templates()[template_id].plan.get("spread") is not None
                            else template_setup(template_id, history))
              for template_id in sorted(templates())}
    setups["sky_garden@60"] = template_setup("sky_garden", history, timeframe=60)
    bare = unannotated(history)
    for template_id in ("dual_thrust", "fairy_four_price", "sky_garden"):
        setups[f"{template_id}@unannotated"] = template_setup(template_id, bare)
    for template_id in ("dual_thrust", "fairy_four_price"):   # today's daily bar not there yet: 今开 from the minutes
        setup = template_setup(template_id, history)
        setups[f"{template_id}@no_today_daily"] = replace(setup, series={**setup.series, 86400: setup.series[86400][:-1]})
    return setups


def comparable(result: dict[str, Any]) -> dict[str, Any]:
    """Trades (open/close time, price, volume, exit reason, fees), summary and coverage as they are; the
    300-point equity curve as a digest."""
    curve = hashlib.sha256(json.dumps(result["equityCurve"], sort_keys=True).encode()).hexdigest()
    return {"summary": result["summary"], "trades": result["trades"], "exitReasons": result["exitReasons"],
            "coverage": result["coverage"], "notes": result["notes"], "equityCurve": curve}


def run_all(history: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
    return {name: comparable(run_backtest(setup)) for name, setup in variants(history).items()
            if setup.plan.closeBeforeEndMinutes is None}


def test_every_template_trades_exactly_as_before_the_optimisation():
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    actual = json.loads(json.dumps(run_all(parity_history())))
    assert set(actual) == {name for name, setup in variants(parity_history()).items()
                           if setup.plan.closeBeforeEndMinutes is None}
    for name, result in expected.items():
        if name not in actual:
            continue  # prior fixture counted an unrepresentable time exit as a later session's open
        # This fixture predates full position-bar counting; its trade/summary count fields were
        # clipped at 255. The new long-hold tests assert the corrected counts directly. Entries
        # from before the sixth batch (加仓 / 分批止盈) predate the trades' adds / partial fields too.
        new_fields = set() if all("adds" in trade for trade in result["trades"]) else {"adds", "partial"}
        # #520-A books the open fee on its fill bar: every trade, the totals and the fees
        # stay fixed, but the equity curve — and the drawdown read from it — move a
        # little. The golden predates that, so the curve hash and the drawdown fields
        # are not invariants here; the trades and the remaining summary are.
        dropped = {"averageBarsHeld", "maxDrawdown", "maxDrawdownPercent"}
        previous = {**result, "summary": {k: v for k, v in result["summary"].items() if k not in dropped},
                    "equityCurve": None,
                    "trades": [{k: v for k, v in trade.items() if k not in {"barsHeld"} | new_fields}
                               for trade in result["trades"]]}
        current = {**actual[name], "summary": {k: v for k, v in actual[name]["summary"].items()
                                                 if k not in dropped},
                   "equityCurve": None,
                   "trades": [{k: v for k, v in trade.items() if k not in {"barsHeld"} | new_fields}
                              for trade in actual[name]["trades"]]}
        assert current == previous, name
    assert all(result["summary"]["trades"] for result in actual.values())


def test_templates_whose_close_time_has_no_bar_close_the_same_day_with_a_note():
    """收盘前平仓 that falls where the history has no bar (a gap before the close) is not carried to the
    next session: it closes on the last bar before the gap, and the result says it was estimated."""
    setups = {name: setup for name, setup in variants(parity_history()).items()
              if setup.plan.closeBeforeEndMinutes is not None}
    estimated = {"fairy_four_price", "fairy_four_price@unannotated", "fairy_four_price@no_today_daily", "r_breaker"}
    assert estimated < set(setups)
    for name, setup in setups.items():
        result = run_backtest(setup)
        assert result["summary"]["trades"] >= 1, name
        closes = [trade for trade in result["trades"] if trade["exitReason"] == "close_before_end"]
        assert all(trade["closeTime"] - trade["openTime"] < 18 * 3600 * NS for trade in closes), name
        if name in estimated:
            assert any("收盘前平仓" in note and "估算" in note for note in result["notes"]), name


class _ReadOnlyHistory:
    """The provider's read-only history for 当日最高 / 最低 (the bars up to the forming one)."""

    def __init__(self, bars: list[dict[str, Any]]) -> None:
        self.bars = bars

    def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        return {"bars": self.bars[-length:]}


# 策略积木·第三期 B: the five templates built from the second batch of blocks (枢轴、当日最高/最低、复位、
# 连续 N 根、上升/下降、ATR 吊灯与百分比追踪) are judged bar by bar like the session templates.
SECOND_BATCH = ["r_breaker", "rsi_reversal", "pivot_reversal", "squeeze", "king_keltner"]
# 第三批积木 (2026-09-27): Z-Score 均值回归 (布林倍数 + 任一离场 + 最长持仓) and 连涨连跌 (连续 N 根上升 / 下降).
THIRD_BATCH = ["z_score", "consecutive_updown"]
# 第四批积木 (2026-09-27): 恒温器 (条件组合 + CMI + 开盘价偏移), NR7 (日线振幅区间最低 + 昨高昨低), 威廉 %R (N 根内).
FOURTH_BATCH = ["thermostat", "nr7_breakout", "williams_r"]


@pytest.mark.parametrize("template_id", ["dual_thrust", "fairy_four_price", "sky_garden", *SECOND_BATCH, *THIRD_BATCH,
                                         *FOURTH_BATCH, "spread_boll_reversion"])
def test_session_templates_judge_every_bar_like_live(template_id):
    """每根K线：回测的判定 == 实盘在同一时刻的 evaluate_rules（带正在形成的K线）+ decide。"""
    history = parity_history()
    if templates()[template_id].plan.get("spread") is not None:
        near, far = spread_histories()
        setup = template_setup(template_id, near, leg_history=far)
    else:
        setup = template_setup(template_id, history)
    plan, timeframe = setup.plan, setup.plan.timeframeSeconds
    base = setup.series[timeframe]
    seen: list[tuple[int, Any, Any]] = []
    result = run_backtest(setup, trace=lambda k, state, decision: seen.append((k, state, decision)))
    assert result["summary"]["trades"] >= 3
    judged = 0
    for k, state, decision in seen:
        if decision.fillPrice is not None:
            continue
        if timeframe == 86400:
            # A daily execution clock: the forming bar is the next trading day's, the window is the daily series.
            if k + 1 >= len(base):
                continue
            forming = dict(base[k + 1])
            workbenches = {86400: {"bars": base[max(0, k + 1 - LIVE_WINDOW):k + 1] + [forming],
                                   "quote": {"tradingSchedule": RB_SCHEDULE}}}
        elif plan.spread is not None:
            # 价差: each leg's own live workbench (its last 255 completed bars plus its forming bar),
            # joined the way strategy_spread.spread_workbenches does for the monitor.
            from trader_runtime.strategy_spread import spread_workbenches
            assert setup.spread_raw is not None and setup.spread_indexes is not None
            own_a, own_b = setup.spread_raw[timeframe]
            ja, jb = setup.spread_indexes[timeframe]
            ia, ib = ja[k], jb[k]
            bench = []
            for own, index in ((own_a, ia), (own_b, ib)):
                forming_leg = {**own[index], "datetime": own[index]["datetime"] + timeframe * NS}
                bench.append({"bars": own[max(0, index + 1 - LIVE_WINDOW):index + 1] + [forming_leg],
                              "quote": {"tradingSchedule": RB_SCHEDULE} if own is own_a else {}})
            workbenches = {timeframe: spread_workbenches(plan.spread, bench[0], bench[1])}
            forming = {**base[k], "datetime": base[k]["datetime"] + timeframe * NS}
        else:
            forming = {**base[k], "datetime": base[k]["datetime"] + timeframe * NS}
            workbenches = {timeframe: {"bars": base[max(0, k + 1 - LIVE_WINDOW):k + 1] + [forming],
                                       "quote": {"tradingSchedule": RB_SCHEDULE}}}
            if 86400 in setup.series:   # daily anchors (今开 / 昨高昨低 / 枢轴)
                day = base[k]["tradingDay"]
                daily = [b for b in setup.series[86400] if b["tradingDay"] <= day][-DAILY_WINDOW:]
                workbenches[86400] = {"bars": daily, "quote": {}}
        extended, pending = live_session_history(_ReadOnlyHistory(base[:k + 1] + [forming]), plan, setup.symbol, workbenches)
        if pending:
            # Only on the fixture's first, partial trading day: neither side has seen that day's first bar.
            assert base[k]["tradingDay"] == base[0]["tradingDay"] and decision.action == "hold", k
            continue
        evaluation = evaluate_rules(plan, workbenches, extended=extended)
        equity = 1_000_000 + sum(t["net"] for t in result["trades"] if t["closeTime"] <= base[k]["datetime"])
        live = decide(plan, evaluation, state, MarketContext(price_tick=1, volume_multiple=10, equity=equity))
        assert (live.action, live.volume, live.exitReason, live.exitKey, live.stopPrice, live.reason) == \
            (decision.action, decision.volume, decision.exitReason, decision.exitKey, decision.stopPrice,
             decision.reason), k
        judged += 1
    assert judged > {60: 1_000, 300: 400, 900: 300, 3600: 300, 86400: 100}[timeframe]


INDICATORS = [
    {"indicator": "close"}, {"indicator": "volume"}, {"indicator": "volume_ratio"}, {"indicator": "ma", "period": 10},
    {"indicator": "ema", "period": 12}, {"indicator": "macd_dif"}, {"indicator": "macd_dea"}, {"indicator": "macd_hist"},
    {"indicator": "kd_k"}, {"indicator": "kd_d"}, {"indicator": "atr", "period": 14}, {"indicator": "rsi", "period": 9},
    {"indicator": "donchian_high", "period": 20}, {"indicator": "donchian_low", "period": 20},
    {"indicator": "boll_mid", "period": 20}, {"indicator": "boll_up", "period": 20, "k": 2.5}, {"indicator": "boll_down"},
    {"indicator": "keltner_mid"}, {"indicator": "keltner_up", "basis": "ma"}, {"indicator": "keltner_down", "k": 2},
    {"indicator": "supertrend", "period": 7}, {"indicator": "prev_high"}, {"indicator": "prev_low"},
    {"indicator": "prev_close"}, {"indicator": "today_open"}, {"indicator": "range_nday", "period": 3},
    {"indicator": "gap_percent"}, {"indicator": "opening_range_high", "period": 3}, {"indicator": "opening_range_low"},
    {"indicator": "ma", "period": 5, "average": 4}, {"indicator": "volume_ratio", "average": 3},
    {"indicator": "rsi", "average": 5}, {"indicator": "today_open", "offset": {"factor": 0.3, "indicator": "range_nday"}},
    {"indicator": "ma", "period": 20, "offset": {"factor": -1.5, "indicator": "atr", "period": 10}},
    # 第三批积木 (2026-09-27): ROC, KDJ 的 J, TRIX (+ its average line), bar fields and 前 N 根 on any operand.
    {"indicator": "roc", "period": 5}, {"indicator": "kd_j"}, {"indicator": "trix", "period": 9},
    {"indicator": "trix", "average": 5}, {"indicator": "high", "barsAgo": 1}, {"indicator": "low", "barsAgo": 2},
    {"indicator": "open"}, {"indicator": "close", "barsAgo": 3}, {"indicator": "rsi", "average": 3, "barsAgo": 2},
    {"indicator": "prev_high", "barsAgo": 1},
    {"indicator": "ma", "period": 10, "barsAgo": 5, "offset": {"factor": 1, "indicator": "atr", "period": 5}},
    # 第四批积木 (2026-09-27): 威廉 %R, 乖离率, MFI, BBI, CMI, K线形态 and 区间最高 / 最低 on any operand.
    {"indicator": "williams_r"}, {"indicator": "williams_r", "period": 5}, {"indicator": "bias", "period": 12},
    {"indicator": "mfi"}, {"indicator": "mfi", "period": 6}, {"indicator": "bbi"}, {"indicator": "cmi"},
    {"indicator": "bar_range"}, {"indicator": "body"}, {"indicator": "upper_shadow"}, {"indicator": "lower_shadow"},
    {"indicator": "close", "highest": 20}, {"indicator": "bar_range", "lowest": 7, "barsAgo": 1},
    {"indicator": "rsi", "lowest": 5}, {"indicator": "prev_high", "highest": 3},
]


@pytest.mark.parametrize("bare", [False, True])
def test_backtest_windows_compute_every_indicator_like_plain_live_lists(bare):
    """The backtest's windows slice per-bar facts computed once for the whole series; every indicator
    (with average and offset) must equal the plain-list computation the live monitor runs."""
    history = parity_history()
    if bare:
        history = unannotated(history)
    minutes, daily = history[60][-700:], history[86400]
    facts, daily_facts = SeriesFacts(minutes), SeriesFacts(daily)
    for start, end in [(0, 1), (0, 12), (0, 255), (100, 355), (300, 555), (445, 700), (690, 700)]:
        window = SeriesWindow(facts, start, end)
        day = bar_trading_day(minutes[end - 1], daily_index(daily)[0])
        daily_end = sum(1 for bar in daily if daily_bar_day(bar) <= day)
        daily_window = SeriesWindow(daily_facts, max(0, daily_end - DAILY_WINDOW), daily_end)
        for raw in INDICATORS:
            operand = Operand.model_validate({**raw, "timeframe": 60})
            plain = indicator_values(list(window), operand, SeriesContext(daily=list(daily_window), schedule=RB_SCHEDULE))
            fast = indicator_values(window, operand, SeriesContext(daily=daily_window, schedule=RB_SCHEDULE))
            assert fast == plain, (raw, start, end)
        entry = minutes[(start + end) // 2]["datetime"]
        assert bars_held(window, entry, 60) == bars_held(list(window), entry, 60)


def test_backtest_windows_evaluate_like_plain_lists_even_when_a_slower_bar_is_not_done():
    history = parity_history()
    plan, _ = build_plan(templates()["multi_timeframe"])   # 5-minute RSI, 15-minute MA filter
    fives, fifteens = history[300], history[900]
    five_facts, fifteen_facts = SeriesFacts(fives), SeriesFacts(fifteens)
    for end in (1_500, 1_501, 1_502, 2_700):
        cutoff = fives[end - 1]["datetime"] + 300 * NS
        done = sum(1 for bar in fifteens if bar["datetime"] + 900 * NS <= cutoff)
        for slow_end in (done, done + 1):   # the second one holds a 15-minute bar still forming
            slow_start = max(0, slow_end - 255)
            fast = {300: SeriesWindow(five_facts, end - 255, end), 900: SeriesWindow(fifteen_facts, slow_start, slow_end)}
            plain = {300: fives[end - 255:end], 900: fifteens[slow_start:slow_end]}
            assert evaluate_completed(plan, fast) == evaluate_completed(plan, plain), (end, slow_end)

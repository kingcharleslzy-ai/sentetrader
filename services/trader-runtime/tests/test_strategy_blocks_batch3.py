"""第三批积木 (2026-09-27): ROC, KDJ 的 J 线, TRIX, the bar's high / low / open and 前 N 根 (`barsAgo`).

Indicators against hand-worked figures and their warm-up, `barsAgo` edge cases (beyond the history,
constants, bounds, a slower clock only reading its completed bars), no look-ahead (a value never
changes when later bars arrive), a strategy using every new block judged bar by bar like live, the
plain words.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from test_backtest_template_parity import FEES, parity_history
from test_strategy_backtest import CST, cond, gapless, plan, series
from trader_runtime.strategy_backtest import LIVE_WINDOW, BacktestSetup, run_backtest
from trader_runtime.strategy_engine import MarketContext, decide
from trader_runtime.strategy_plain import condition_words, describe_plan, operand_words
from trader_runtime.strategy_rules import (
    Condition,
    Operand,
    StrategyPlan,
    StrategyRuleError,
    condition_test,
    describe_condition,
    describe_operand,
    evaluate_completed,
    evaluate_rules,
    indicator_values,
    required_condition_bars,
)

NS = 1_000_000_000


def values(bars: list[dict[str, Any]], **operand: Any) -> list[float | None]:
    return indicator_values(bars, Operand.model_validate({"timeframe": 300, **operand}))


def rounded(items: list[float | None], digits: int = 4) -> list[float | None]:
    return [None if item is None else round(item, digits) for item in items]


# -- indicators ----------------------------------------------------------------------------------------

def test_roc_is_the_percent_change_against_n_bars_ago():
    bars = series(gapless([100, 102, 105, 99]))
    # (105 ÷ 100 − 1) × 100 = 5; (99 ÷ 102 − 1) × 100 = −2.9412. The first N bars have nothing to compare.
    assert rounded(values(bars, indicator="roc", period=2)) == [None, None, 5.0, -2.9412]
    assert values(bars, indicator="roc")[-1] is None                  # default N = 12 needs 13 bars
    assert values(series([(0, 1, 0, 0), (5, 5, 5, 5), (10, 10, 10, 10)]), indicator="roc", period=1) == [None, None, 100.0]


def test_kdj_j_is_three_k_minus_two_d_on_the_kd_smoothing():
    bars = series([(10, 12, 8, 11), (11, 14, 10, 13)])
    # Bar 0: RSV = (11 − 8) ÷ 4 = 75 → K = (2×50 + 75) ÷ 3 = 58.3333, D = (2×50 + K) ÷ 3 = 52.7778, J = 69.4444.
    # Bar 1: RSV = (13 − 8) ÷ 6 = 83.3333 → K = 66.6667, D = 57.4074, J = 85.1852.
    assert rounded(values(bars, indicator="kd_j")) == [69.4444, 85.1852]
    history = parity_history()[60][-300:]
    k, d, j = (values(history, indicator=name) for name in ("kd_k", "kd_d", "kd_j"))
    assert j == pytest.approx([3 * a - 2 * b for a, b in zip(k, d, strict=True)], abs=1e-9)
    assert max(j) > 100 or min(j) < 0     # J leaves 0–100 where K and D cannot


def test_trix_is_the_one_bar_change_of_a_triple_ema():
    closes = [10, 13, 10, 13, 16, 16]
    # EMA(2) has α = 2/3, seeded with the first close. EMA³: 10, 10.8889, 10.8889, 11.4815, 12.9959, 14.3128.
    # TRIX from bar 3 × (2 − 1) + 1 = 4: (12.9959 ÷ 11.4815 − 1) × 100 = 13.19, then 10.13.
    trix = values(series(gapless(closes)), indicator="trix", period=2)
    assert trix[:4] == [None] * 4 and trix[4] == pytest.approx(13.19, abs=0.01) and trix[5] == pytest.approx(10.13, abs=0.01)

    def ema(items: list[float], n: int) -> list[float]:
        out = [items[0]]
        for x in items[1:]:
            out.append(out[-1] + (x - out[-1]) * 2 / (n + 1))
        return out
    history = parity_history()[60][-255:]
    closes = [bar["close"] for bar in history]
    triple = ema(ema(ema(closes, 12), 12), 12)
    trix = values(history, indicator="trix")                         # default N = 12
    assert trix[33] is None and trix[34] is not None
    assert trix[34:] == pytest.approx([(triple[i] / triple[i - 1] - 1) * 100 for i in range(34, 255)], rel=1e-12)
    # The signal line (TqSdk TRMA) is the plain N-bar average of TRIX.
    signal = values(history, indicator="trix", average=9)
    assert signal[41] is None and signal[42] == pytest.approx(sum(trix[34:43]) / 9)
    with pytest.raises(ValidationError, match="TRIX 周期最多 60"):
        Operand(indicator="trix", period=61)
    with pytest.raises(ValidationError, match="指标周期至少 2 根"):
        Operand(indicator="trix", period=1)


def test_bar_fields_and_bars_ago():
    bars = series([(10, 12, 8, 11), (11, 14, 10, 13), (13, 15, 12, 14), (14, 16, 13, 15)])
    assert values(bars, indicator="high") == [12, 14, 15, 16] and values(bars, indicator="open") == [10, 11, 13, 14]
    assert values(bars, indicator="high", barsAgo=1) == [None, 12, 14, 15]
    assert values(bars, indicator="close", barsAgo=2) == [None, None, 11, 13]
    assert values(bars, indicator="low", barsAgo=4) == [None] * 4                  # beyond the history
    assert values(bars, indicator="low", barsAgo=50) == [None] * 4
    # The shift applies after the average (and offset): the 2-bar MA as it stood one bar ago.
    assert values(bars, indicator="ma", period=2, barsAgo=1) == [None, None, 12.0, 13.5]
    assert values(bars, indicator="close", barsAgo=1, offset={"factor": 1, "indicator": "atr", "period": 2}) == [
        None, None, *values(bars, indicator="close", offset={"factor": 1, "indicator": "atr", "period": 2})[1:3]]
    with pytest.raises(ValidationError, match="常数没有"):
        Operand(indicator="constant", value=1, barsAgo=1)
    with pytest.raises(ValidationError):
        Operand(indicator="close", barsAgo=51)
    with pytest.raises(ValidationError):
        Operand(indicator="close", barsAgo=-1)
    # Its history need counts the bars back: close vs 3 bars ago needs 4 bars + the judged bar's window.
    shifted = Condition.model_validate(cond("高于 3 根前", "close", "gt", {"indicator": "close", "barsAgo": 3}))
    assert required_condition_bars(shifted) == 5
    with pytest.raises(StrategyRuleError, match="strategy_bars_insufficient"):
        evaluate_completed(plan(entry=[shifted.model_dump()]), {300: bars})


def test_new_values_never_change_when_later_bars_arrive():
    """No look-ahead: every new block's value at bar i is the same whether or not later bars exist."""
    bars = parity_history()[60][-255:]
    for raw in ({"indicator": "roc", "period": 5}, {"indicator": "kd_j"}, {"indicator": "trix", "average": 9},
                {"indicator": "high", "barsAgo": 1}, {"indicator": "rsi", "barsAgo": 3},
                {"indicator": "ma", "period": 10, "barsAgo": 7, "offset": {"factor": -1, "indicator": "atr"}}):
        whole = values(bars, **raw)
        for end in (1, 2, 40, 128, 254):
            assert values(bars[:end], **raw) == whole[:end], (raw, end)


def test_a_slower_clock_shifts_over_its_own_completed_bars_only():
    """前 N 根 on an hourly operand in a 5-minute plan counts hourly bars, and the hour still forming at
    the decision time is not one of them."""
    start = int(datetime(2026, 9, 23, 9, 0, tzinfo=CST).timestamp()) * NS
    fives = series(gapless([100] * 36), start=start)                            # 09:00 … 11:55 → decision at 12:00
    hours = series([(c, c + 1, c - 1, c) for c in (100, 101, 103, 90)], start=start, step=3600)   # 12:00 still forming
    rising = cond("小时收盘高于上一小时", "close", "gt", {"indicator": "close", "barsAgo": 1}, tf=3600)
    p = plan(entry=[rising])
    result = evaluate_completed(p, {300: fives, 3600: hours})
    assert result["rules"]["entryLong"] and result["conditions"][0]["detail"] == "60 分钟 · 103.0000 / 101.0000"
    deep = plan(entry=[cond("x", "close", "gt", {"indicator": "close", "barsAgo": 3}, tf=3600)])
    with pytest.raises(StrategyRuleError, match="60 分钟"):
        evaluate_completed(deep, {300: fives, 3600: hours})


def test_higher_than_the_previous_bar_matches_rising_by_one():
    bars = parity_history()[60][-200:]
    above = Condition.model_validate(cond("比前一根高", "close", "gt", {"indicator": "close", "barsAgo": 1}, tf=60))
    rising = Condition.model_validate(cond("较 1 根前上升", "close", "rising", {"indicator": "constant", "value": 1}, tf=60))
    tests = [condition_test(c, indicator_values(bars, c.left), indicator_values(bars, c.right)) for c in (above, rising)]
    assert [tests[0](i) for i in range(1, 200)] == [tests[1](i) for i in range(1, 200)]


# -- backtest == live, bar by bar ------------------------------------------------------------------------

def _operand(name: str, **params: Any) -> dict[str, Any]:
    return {"indicator": name, "timeframe": 60, **params}


def _condition(label: str, left: dict[str, Any], operator: str, right: dict[str, Any], **params: Any) -> dict[str, Any]:
    return {"label": label, "left": left, "operator": operator, "right": right, **params}


def blocks_plan() -> StrategyPlan:
    constant = lambda value: {"indicator": "constant", "value": value}  # noqa: E731
    return StrategyPlan.model_validate({
        "name": "第三批积木", "description": "TRIX 在信号线上方 + ROC 动量 + 突破上一根高点；J 线极值或跌破前两根低点离场",
        "direction": "both", "timeframeSeconds": 60, "maxPosition": 1, "stopLossTicks": 30, "takeProfitTicks": None,
        "cooldownBars": 0, "maxLoss": 1e8, "maxOrdersPerDay": 50,
        "rules": {
            "entryLong": {"conditions": [
                _condition("TRIX 在信号线上方", _operand("trix", period=9), "gt", _operand("trix", period=9, average=5)),
                _condition("ROC 动量为正", _operand("roc", period=10), "gt", constant(0.05)),
                _condition("突破上一根最高", _operand("close"), "gt", _operand("high", barsAgo=1))]},
            "exitLong": {"join": "any", "conditions": [
                _condition("J 线过热", _operand("kd_j"), "gt", constant(100)),
                _condition("跌破前两根最低", _operand("close"), "lt", _operand("low", barsAgo=2))]},
            "entryShort": {"conditions": [
                _condition("TRIX 在信号线下方", _operand("trix", period=9), "lt", _operand("trix", period=9, average=5)),
                _condition("ROC 动量为负", _operand("roc", period=10), "lt", constant(-0.05)),
                _condition("跌破上一根最低", _operand("close"), "lt", _operand("low", barsAgo=1))]},
            "exitShort": {"join": "any", "conditions": [
                _condition("J 线过冷", _operand("kd_j"), "lt", constant(0)),
                _condition("升破前两根最高", _operand("close"), "gt", _operand("high", barsAgo=2))]},
        }})


def test_third_batch_blocks_judge_every_bar_like_live():
    """每根K线：回测的判定 == 实盘在同一时刻的 evaluate_rules（带正在形成的K线）+ decide。"""
    p = blocks_plan()
    base = parity_history()[60][-2_400:]
    setup = BacktestSetup(plan=p, symbol="SHFE.rb2701", series={60: base}, price_tick=1, volume_multiple=10,
                          fee_schedule=FEES, initial_balance=1_000_000)
    seen: list[tuple[int, Any, Any]] = []
    result = run_backtest(setup, trace=lambda k, state, decision: seen.append((k, state, decision)))
    assert result["summary"]["trades"] >= 10 and "rule" in {trade["exitReason"] for trade in result["trades"]}
    judged = 0
    for k, state, decision in seen:
        if decision.fillPrice is not None:
            continue
        forming = {**base[k], "datetime": base[k]["datetime"] + 60 * NS}
        workbenches = {60: {"bars": base[max(0, k + 1 - LIVE_WINDOW):k + 1] + [forming], "quote": {}}}
        equity = 1_000_000 + sum(t["net"] for t in result["trades"] if t["closeTime"] <= base[k]["datetime"])
        live = decide(p, evaluate_rules(p, workbenches), state, MarketContext(price_tick=1, volume_multiple=10, equity=equity))
        assert (live.action, live.volume, live.exitReason, live.stopPrice, live.reason) == \
            (decision.action, decision.volume, decision.exitReason, decision.stopPrice, decision.reason), k
        judged += 1
    assert judged > 1_000


# -- words ------------------------------------------------------------------------------------------------

def test_labels_and_plain_words_name_the_new_blocks():
    assert describe_operand(Operand(indicator="roc", timeframe=60)) == "12根涨跌幅%"
    assert describe_operand(Operand(indicator="kd_j", timeframe=60)) == "KDJ J"
    assert describe_operand(Operand(indicator="trix", average=9, timeframe=60)) == "TRIX12的9均线"
    assert describe_operand(Operand(indicator="high", barsAgo=1, timeframe=60)) == "1根前的最高价"
    assert describe_condition(Condition.model_validate(
        cond("x", "close", "gt", {"indicator": "close", "barsAgo": 2}))) == "收盘价 > 2根前的收盘价"
    assert operand_words(Operand(indicator="high", barsAgo=1, timeframe=60)) == "上一根K线的最高价"
    assert operand_words(Operand(indicator="close", barsAgo=3, timeframe=60)) == "3 根前的收盘价"
    assert condition_words(Condition.model_validate(cond("x", "roc", "gt", {"indicator": "constant", "value": 2}, period=10))) \
        == "10 根涨跌幅高于2%"
    mirrored = StrategyPlan.model_validate({"name": "动量", "description": "动量", "direction": "both", "timeframeSeconds": 300,
        "rules": {"entryLong": {"conditions": [
                      cond("涨幅", "roc", "gt", {"indicator": "constant", "value": 2}),
                      cond("J", "kd_j", "lt", {"indicator": "constant", "value": 20}),
                      cond("破高", "close", "gt", {"indicator": "high", "barsAgo": 1})]},
                  "entryShort": {"conditions": [
                      cond("跌幅", "roc", "lt", {"indicator": "constant", "value": -2}),
                      cond("J", "kd_j", "gt", {"indicator": "constant", "value": 80}),
                      cond("破低", "close", "lt", {"indicator": "low", "barsAgo": 1})]}}})
    text = describe_plan(mirrored)
    assert "上一根K线的最高价" in text and "KDJ 的 J 值" in text and "反过来就开空" in text, text

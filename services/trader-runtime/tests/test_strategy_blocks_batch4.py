"""第四批积木 (2026-09-27): 条件组合 (branches), N 根里至少 M 根 (minCount), 区间最高 / 最低 (highest /
lowest), 威廉 %R, 乖离率, MFI, BBI, CMI and K线形态.

Indicators against hand-worked figures and the TqSdk / Pruitt formulas, no look-ahead, the validation
limits, (A且B)或(C且D) judged exactly like its truth table, a strategy using every new block judged bar
by bar like live, 策略体检 skipping groups with branches, and the plain words.
"""
from __future__ import annotations

from itertools import product
from typing import Any

import pytest
from pydantic import ValidationError

from test_backtest_template_parity import FEES, parity_history
from test_strategy_backtest import cond, gapless, plan, series
from trader_runtime.strategy_backtest import LIVE_WINDOW, BacktestSetup, run_backtest
from trader_runtime.strategy_engine import MarketContext, decide
from trader_runtime.strategy_health import diagnosable_groups
from trader_runtime.strategy_plain import condition_words, describe_plan, operand_words
from trader_runtime.strategy_rules import (
    Condition,
    Operand,
    RuleGroup,
    StrategyPlan,
    describe_condition,
    describe_operand,
    evaluate_completed,
    evaluate_rules,
    group_passes,
    indicator_values,
    required_condition_bars,
    resolve_chart_timeframe,
)

NS = 1_000_000_000


def rule(label: str, left: str, operator: str, right: dict[str, Any], tf: int = 300, *, within: int = 1,
         least: int = 1, **params: Any) -> dict[str, Any]:
    """`cond` with the condition's own withinBars / minCount (cond's extra keywords go to the left operand)."""
    return {**cond(label, left, operator, right, tf=tf, **params), "withinBars": within, "minCount": least}


def group_plan(group: dict[str, Any] | list[dict[str, Any]]) -> StrategyPlan:
    """plan() with a whole entry group (join / branches / rearm) instead of a condition list."""
    raw = plan().model_dump()
    raw["rules"] = {"entryLong": group if isinstance(group, dict) else {"conditions": group}}
    return StrategyPlan.model_validate(raw)


def values(bars: list[dict[str, Any]], **operand: Any) -> list[float | None]:
    return indicator_values(bars, Operand.model_validate({"timeframe": 300, **operand}))


def rounded(items: list[float | None], digits: int = 4) -> list[float | None]:
    return [None if item is None else round(item, digits) for item in items]


# -- indicators ----------------------------------------------------------------------------------------

def test_williams_r_and_bias_follow_tqsdk():
    bars = series([(10, 12, 8, 11), (11, 14, 10, 13), (13, 15, 12, 12), (12, 13, 9, 9)])
    # %R(3) at bar 2: HH 15, LL 8 → (15 − 12) ÷ 7 × −100 = −42.8571; bar 3: HH 15, LL 9 → (15 − 9) ÷ 6 × −100 = −100.
    assert rounded(values(bars, indicator="williams_r", period=3)) == [None, None, -42.8571, -100.0]
    flat = series([(5, 5, 5, 5)] * 3)
    assert values(flat, indicator="williams_r", period=2) == [None, None, None]        # no range: no value
    # BIAS(2) = (C − MA2) ÷ MA2 × 100: bar 1 (13 − 12) ÷ 12 = 8.3333%, bar 2 (12 − 12.5) ÷ 12.5 = −4%.
    assert rounded(values(bars, indicator="bias", period=2)) == [None, 8.3333, -4.0, -14.2857]


def test_mfi_matches_tqsdk_from_its_first_whole_window():
    bars = series([(10, 11, 9, 10), (10, 13, 10, 12), (12, 12, 9, 10), (10, 14, 10, 13)])
    for bar, volume in zip(bars, (100, 200, 300, 400), strict=True):
        bar["volume"] = volume
    typical = [10, 35 / 3, 31 / 3, 37 / 3]
    # MFI(2) from bar 2: flows read the bar before, so bar 1 (the first possible) would use bar 0's missing flow.
    positive = typical[1] * 200
    negative = typical[2] * 300
    first = 100 - 100 / (1 + positive / negative)
    second = 100 - 100 / (1 + typical[3] * 400 / negative)
    assert rounded(values(bars, indicator="mfi", period=2)) == [None, None, round(first, 4), round(second, 4)]
    rising = series(gapless([10, 11, 12, 13]))
    assert values(rising, indicator="mfi", period=2)[-1] == 100.0                      # no outflow at all


def test_bbi_and_cmi():
    closes = list(range(100, 130))
    bars = series(gapless(closes))
    bbi = values(bars, indicator="bbi")
    assert bbi[22] is None and bbi[23] == pytest.approx(sum(sum(closes[24 - n:24]) / n for n in (3, 6, 12, 24)) / 4)
    # CMI(30) (Pruitt & Hill): |C − C[29]| ÷ (HHV(H,30) − LLV(L,30)) × 100. A straight climb is nearly all
    # trend: 29 of the 31 points between the lowest low (99) and the highest high (130).
    cmi = values(bars, indicator="cmi")
    assert cmi[28] is None and cmi[29] == pytest.approx(29 / 31 * 100)
    chop = series(gapless([100, 110] * 15))                                             # highs 111, lows 99
    assert values(chop, indicator="cmi")[29] == pytest.approx(10 / 12 * 100)
    assert values(chop, indicator="cmi", period=29)[29] == 0.0                          # same close 28 bars back


def test_candle_fields():
    bars = series([(10, 15, 8, 12), (12, 13, 7, 9)])
    assert values(bars, indicator="bar_range") == [7, 6]
    assert values(bars, indicator="body") == [2, -3]
    assert values(bars, indicator="upper_shadow") == [3, 1]
    assert values(bars, indicator="lower_shadow") == [2, 2]


def test_highest_and_lowest_of_any_operand():
    bars = series(gapless([10, 13, 11, 9, 12]))
    assert values(bars, indicator="close", highest=3) == [None, None, 13, 13, 12]
    assert values(bars, indicator="close", lowest=2) == [None, 10, 11, 9, 9]
    # Applied after the average and before 前 N 根: “the 2-bar MA's 2-bar high, one bar ago”.
    assert values(bars, indicator="ma", period=2, highest=2, barsAgo=1) == [None, None, None, 12.0, 12.0]
    # NR7 in words: this bar's range is the smallest of the last 3.
    ranges = series([(10, 16, 9, 12), (12, 14, 10, 13), (13, 14, 12, 13)])
    nr = Condition.model_validate(cond("窄幅", "bar_range", "lte", {"indicator": "bar_range", "lowest": 3}))
    assert required_condition_bars(nr) == 4 and values(ranges, indicator="bar_range", lowest=3) == [None, None, 2]
    with pytest.raises(ValidationError, match="三者只能选一种"):
        Operand(indicator="rsi", average=3, highest=5)
    with pytest.raises(ValidationError, match="常数没有区间最高"):
        Operand(indicator="constant", value=1, lowest=5)
    with pytest.raises(ValidationError):
        Operand(indicator="close", highest=1)


def test_new_values_never_change_when_later_bars_arrive():
    bars = parity_history()[60][-255:]
    for raw in ({"indicator": "williams_r"}, {"indicator": "bias", "period": 12}, {"indicator": "mfi"},
                {"indicator": "bbi"}, {"indicator": "cmi"}, {"indicator": "body"}, {"indicator": "lower_shadow"},
                {"indicator": "close", "highest": 20}, {"indicator": "bar_range", "lowest": 7, "barsAgo": 1},
                {"indicator": "rsi", "lowest": 10}):
        whole = values(bars, **raw)
        assert any(item is not None for item in whole), raw
        for end in (1, 2, 40, 128, 254):
            assert values(bars[:end], **raw) == whole[:end], (raw, end)


# -- N 根里至少 M 根 ------------------------------------------------------------------------------------

def test_min_count_needs_m_holding_bars_in_the_window():
    closes = [10, 10, 10, 12, 11, 13, 12, 14]         # above the previous close on bars 3, 5, 7
    bars = series(gapless(closes))
    up = lambda **extra: rule("收阳", "close", "gt", {"indicator": "close", "barsAgo": 1}, **extra)  # noqa: E731
    judged = lambda raw: evaluate_completed(group_plan([raw]), {300: bars})["rules"]["entryLong"]  # noqa: E731
    assert judged(up(within=4, least=2))          # bars 5 and 7 among the last 4
    assert not judged(up(within=4, least=3))
    assert judged(up(within=5, least=3))           # bars 3, 5, 7
    assert judged(up(within=5))                    # minCount 1 = the old “最近 N 根内出现过”
    with pytest.raises(ValidationError, match="M 不能大于 N"):
        Condition.model_validate(up(within=3, least=4))
    words = Condition.model_validate(up(within=5, least=3))
    assert describe_condition(words) == "收盘价 > 1根前的收盘价（5根内至少3根）"
    assert condition_words(words).endswith("（最近 5 根里至少 3 根）")


def test_rearm_counts_min_count_too():
    closes = [10, 12, 11, 13, 12, 11, 10]
    bars = series(gapless(closes))
    entry = cond("低于 20", "close", "lt", {"indicator": "constant", "value": 20})
    rearm = rule("收阳", "close", "gt", {"indicator": "close", "barsAgo": 1}, within=3, least=2)
    p = group_plan({"conditions": [entry], "rearm": [rearm]})
    engine = evaluate_completed(p, {300: bars})["engine"]
    # 2 rising bars among bars 1–3 (1 and 3) is the latest window where the rearm holds: bar 3.
    assert engine["rearm"]["entryLong"] == bars[3]["datetime"]


# -- 条件组合 -------------------------------------------------------------------------------------------

def _flag(label: str, on: bool) -> dict[str, Any]:
    return cond(label, "close", "gt" if on else "lt", {"indicator": "constant", "value": 0})


def test_branches_follow_the_truth_table():
    bars = series(gapless([10, 11, 12]))
    for a, b, c, d in product([True, False], repeat=4):
        group = {"join": "any", "branches": [{"conditions": [_flag("A", a), _flag("B", b)]},
                                              {"conditions": [_flag("C", c), _flag("D", d)]}]}
        result = evaluate_completed(group_plan(group), {300: bars})
        assert result["rules"]["entryLong"] == ((a and b) or (c and d)), (a, b, c, d)
        assert [row["label"] for row in result["conditions"] if row["group"] == "entryLong"] == ["A", "B", "C", "D"]
    for t, a, b in product([True, False], repeat=3):          # T 且 (A 或 B)
        group = {"conditions": [_flag("T", t)], "branches": [{"join": "any", "conditions": [_flag("A", a), _flag("B", b)]}]}
        assert evaluate_completed(group_plan(group), {300: bars})["rules"]["entryLong"] == (t and (a or b))


def test_group_passes_combines_flags_in_signal_order():
    group = RuleGroup.model_validate({"join": "all", "conditions": [_flag("T", True)],
        "branches": [{"join": "any", "conditions": [_flag("A", True), _flag("B", True)]},
                     {"conditions": [_flag("C", True), _flag("D", True)]}]})
    assert group_passes(group, [True, False, True, True, True])
    assert not group_passes(group, [True, False, False, True, True])
    assert not group_passes(group, [True, True, False, True, False])


def test_branch_limits():
    with pytest.raises(ValidationError, match="至少要有一条条件"):
        RuleGroup.model_validate({"conditions": []})
    with pytest.raises(ValidationError, match="不与先后顺序"):
        RuleGroup.model_validate({"sequence": True, "branches": [{"conditions": [_flag("A", True)]}]})
    with pytest.raises(ValidationError, match="不与先后顺序、方向确认同用"):
        RuleGroup.model_validate({"conditions": [_flag("A", True)], "alignmentConditions": [_flag("B", True)],
                                  "branches": [{"conditions": [_flag("C", True)]}]})
    with pytest.raises(ValidationError, match="最多 16 条"):
        RuleGroup.model_validate({"conditions": [_flag(str(i), True) for i in range(10)],
                                  "branches": [{"conditions": [_flag(f"b{i}", True) for i in range(7)]}]})
    with pytest.raises(ValidationError):
        RuleGroup.model_validate({"branches": [{"conditions": []}]})
    base = group_plan({"join": "any", "branches": [{"conditions": [_flag("A", True)]}]}).model_dump()
    with pytest.raises(ValueError, match="条件组合"):
        StrategyPlan.model_validate({**base, "perTradeRisk": 1000,
                                     "pullback": {"trendPeriod": 60, "touchPeriod": 10, "guardPeriod": 30}}).check_executable()


def test_follow_chart_and_timeframe_checks_reach_branch_conditions():
    group = {"join": "any", "branches": [{"conditions": [cond("x", "close", "gt", {"indicator": "ma", "period": 5}, tf=0)]}]}
    raw = group_plan(group).model_dump()
    raw["timeframeSeconds"] = 0
    resolved = resolve_chart_timeframe(StrategyPlan.model_validate(raw), 900)
    branch = resolved.rules.entryLong.branches[0].conditions[0]
    assert resolved.timeframeSeconds == 900 and branch.left.timeframe == branch.right.timeframe == 900
    fast = group_plan({"join": "any", "branches": [{"conditions": [cond("x", "close", "gt", {"indicator": "ma"}, tf=60)]}]})
    fast.timeframeSeconds = 300
    with pytest.raises(ValueError, match="执行周期不能慢于条件周期"):
        fast.check_executable()


def test_health_skips_branch_groups():
    up = cond("站上均线", "close", "gt", {"indicator": "ma", "period": 10}, tf=60)
    down = cond("跌破均线", "close", "lt", {"indicator": "ma", "period": 10}, tf=60)
    hot = cond("RSI 高", "rsi", "gt", {"indicator": "constant", "value": 55}, tf=60)
    cold = cond("RSI 低", "rsi", "lt", {"indicator": "constant", "value": 45}, tf=60)
    group = RuleGroup.model_validate({"join": "any", "branches": [{"conditions": [up, hot]}, {"conditions": [down, cold]}]})
    both = group_plan(group.model_dump())
    assert diagnosable_groups(both) == []        # 去掉一条条件的建议对组合没有意义


# -- backtest == live, bar by bar ------------------------------------------------------------------------

def _operand(name: str, **params: Any) -> dict[str, Any]:
    return {"indicator": name, "timeframe": 60, **params}


def _condition(label: str, left: dict[str, Any], operator: str, right: dict[str, Any], **params: Any) -> dict[str, Any]:
    return {"label": label, "left": left, "operator": operator, "right": right, **params}


def blocks_plan() -> StrategyPlan:
    constant = lambda value: {"indicator": "constant", "value": value}  # noqa: E731
    return StrategyPlan.model_validate({
        "name": "第四批积木", "description": "趋势时突破 20 根新高、震荡时威廉超卖反弹；乖离过大或 MFI 过热离场",
        "direction": "both", "timeframeSeconds": 60, "maxPosition": 1, "stopLossTicks": 30, "takeProfitTicks": None,
        "cooldownBars": 0, "maxLoss": 1e8, "maxOrdersPerDay": 50,
        "rules": {
            "entryLong": {"join": "any", "branches": [
                {"conditions": [_condition("有趋势", _operand("cmi", period=20), "gte", constant(30)),
                                _condition("20 根新高", _operand("close"), "gte", _operand("close", highest=20)),
                                _condition("阳线", _operand("body"), "gt", constant(0))]},
                {"conditions": [_condition("震荡", _operand("cmi", period=20), "lt", constant(30)),
                                _condition("威廉超卖过", _operand("williams_r"), "lt", constant(-80), withinBars=6, minCount=2),
                                _condition("在 BBI 上方", _operand("close"), "gt", _operand("bbi"))]}]},
            "exitLong": {"join": "any", "conditions": [
                _condition("乖离过大", _operand("bias", period=12), "gt", constant(0.4)),
                _condition("MFI 过热", _operand("mfi"), "gt", constant(85))]},
            "entryShort": {"join": "any", "branches": [
                {"conditions": [_condition("有趋势", _operand("cmi", period=20), "gte", constant(30)),
                                _condition("20 根新低", _operand("close"), "lte", _operand("close", lowest=20)),
                                _condition("阴线", _operand("body"), "lt", constant(0))]},
                {"conditions": [_condition("震荡", _operand("cmi", period=20), "lt", constant(30)),
                                _condition("威廉超买过", _operand("williams_r"), "gt", constant(-20), withinBars=6, minCount=2),
                                _condition("在 BBI 下方", _operand("close"), "lt", _operand("bbi"))]}]},
            "exitShort": {"join": "any", "conditions": [
                _condition("乖离过大", _operand("bias", period=12), "lt", constant(-0.4)),
                _condition("MFI 过冷", _operand("mfi"), "lt", constant(15)),
                _condition("长下影", _operand("lower_shadow"), "gt", _operand("bar_range", lowest=5, barsAgo=1))]},
        }})


@pytest.mark.timeout(120)   # hang guard only: ~8 s of CPU alone, which a loaded runner stretches past the 30 s default
def test_fourth_batch_blocks_judge_every_bar_like_live():
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
    assert describe_operand(Operand(indicator="williams_r", timeframe=60)) == "威廉%R14"
    assert describe_operand(Operand(indicator="close", highest=20, timeframe=60)) == "收盘价的20根最高"
    assert describe_operand(Operand(indicator="bar_range", lowest=7, barsAgo=1, timeframe=60)) == "1根前的K线振幅的7根最低"
    assert operand_words(Operand(indicator="bar_range", lowest=7, timeframe=86400)) == "最近 7 根K线振幅的最低值"
    assert operand_words(Operand(indicator="cmi", timeframe=60)) == "30 根震荡指数 CMI"
    assert condition_words(Condition.model_validate(cond("x", "bias", "gt", {"indicator": "constant", "value": 3}))) \
        == "6 根乖离率高于3%"
    group = RuleGroup.model_validate({"join": "any", "branches": [
        {"conditions": [_flag("A", True), cond("B", "rsi", "lt", {"indicator": "constant", "value": 30})]},
        {"join": "any", "conditions": [_flag("C", True), _flag("D", True)]}]})
    mirrored = StrategyPlan.model_validate({"name": "反转", "description": "反转", "direction": "both", "timeframeSeconds": 300,
        "rules": {"entryLong": {"conditions": [cond("超卖", "williams_r", "lt", {"indicator": "constant", "value": -80}),
                                               cond("新高", "close", "gte", {"indicator": "close", "highest": 20}),
                                               cond("长下影", "lower_shadow", "gt", {"indicator": "body"})]},
                  "entryShort": {"conditions": [cond("超买", "williams_r", "gt", {"indicator": "constant", "value": -20}),
                                                cond("新低", "close", "lte", {"indicator": "close", "lowest": 20}),
                                                cond("长上影", "upper_shadow", "lt", {"indicator": "body"})]}}})
    assert "反过来就开空" in describe_plan(mirrored), describe_plan(mirrored)
    combo = group_plan(group.model_dump())
    assert "（收盘价高于0且RSI14低于30）或（收盘价高于0或收盘价高于0）就开多" in describe_plan(combo).replace(" ", ""), \
        describe_plan(combo)



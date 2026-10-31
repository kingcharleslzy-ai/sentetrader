"""价差策略第一期 (2026-09-29): the spread chain end to end.

The two legs' K-lines inner-joined into one spread series (and the live 255-bar window agreeing
with the whole series at every bar), the SpreadSpec validation refusals, the engine's close-judged
价差止损/止盈, the two-leg backtest (each leg fills at its own next open, fees / slippage / multiplier
per leg), live 只提醒 (both legs read, one alert per bar), 出卡确认 proposing one card for both
legs, the start route accepting every 执行方式, and the 跨期价差回归（布林）template.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from test_simulation_orders import USER, Gateway, NormalProvider, application
from test_strategy_monitor import STRATEGY_ID
from trader_runtime.data_providers import fake_market_workbench
from trader_runtime.strategy_backtest import BacktestSetup, execute, run_backtest
from trader_runtime.strategy_engine import MarketContext, PositionState, decide, open_position
from trader_runtime.strategy_monitor import StrategyMonitorService
from trader_runtime.strategy_rules import (
    Operand,
    SeriesFacts,
    SeriesWindow,
    SpreadSpec,
    StrategyPlan,
    StrategyRuleError,
    evaluate_completed,
    indicator_values,
)
from trader_runtime.strategy_spread import (
    join_legs,
    joined_window_start,
    spread_evaluation,
    spread_legs_words,
    spread_position,
    spread_price,
    spread_words,
    spread_workbenches,
)
from trader_runtime.strategy_templates import TemplateError, build_plan, resolve_params, summary, templates
from trader_runtime.strategy_workbench import definition_for
from trader_runtime.trading_calendar import trading_day_of

SYMBOL_A, SYMBOL_B = "SHFE.rb2701", "SHFE.rb2705"
NS = 1_000_000_000
T0 = 1_788_192_000_000_000_000
SHFE = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
        "night": [["21:00:00", "25:00:00"]]}
FEE = {"source": "custom", "default": {"mode": "per_lot", "open": 3, "closeToday": 3, "closeYesterday": 3},
       "overrides": []}
MARKET = MarketContext(price_tick=1.0, volume_multiple=10.0, equity=100_000)


def _condition(left: str, operator: str, value: float) -> dict[str, Any]:
    return {"label": "价差条件", "left": {"indicator": left, "timeframe": 300},
            "operator": operator, "right": {"indicator": "constant", "value": value}}


ALWAYS = _condition("close", "gt", -10_000)          # a spread close is tiny: always true
NEVER = _condition("close", "lt", -10_000)
GT_ZERO = _condition("close", "gt", 0)               # true only once the walked spread leaves 0.0


def _plan(**fields: Any) -> StrategyPlan:
    plan = StrategyPlan.model_validate({
        "name": "价差测试", "description": "价差第一期测试", "direction": "long", "timeframeSeconds": 300,
        "maxPosition": 1, "stopLossTicks": 20, "takeProfitTicks": None, "cooldownBars": 0,
        "rules": {"entryLong": {"conditions": [ALWAYS]}, "exitLong": {"conditions": [NEVER]}},
        "spread": {"kind": "difference", "legs": [{"symbol": SYMBOL_A}, {"symbol": SYMBOL_B}]},
        **fields})
    plan.check_executable()
    return plan


def _spec(**fields: Any) -> SpreadSpec:
    return SpreadSpec.model_validate({"legs": [{"symbol": SYMBOL_A}, {"symbol": SYMBOL_B}], **fields})


def _bars(closes: list[float], *, base: float = 100.0) -> list[dict]:
    """One 300s bar per close, open == close (the spread of the opens is the spread of the closes)."""
    return [{"datetime": T0 + index * 300 * NS, "open": base + close, "high": base + close + 0.5,
             "low": base + close - 0.5, "close": base + close, "volume": 10, "tradingDay": "2026-09-01"}
            for index, close in enumerate(closes)]


def _setup(plan: StrategyPlan, bars_a: list[dict], bars_b: list[dict], *, slippage: int = 0,
           leg_ticks: tuple[float, float] = (1.0, 2.0),
           leg_multiples: tuple[float, float] = (10.0, 20.0)) -> BacktestSetup:
    joined, ja, jb = join_legs(plan.spread, bars_a, bars_b)
    return BacktestSetup(plan=plan, symbol=SYMBOL_A, series={300: joined}, price_tick=leg_ticks[0],
                         volume_multiple=leg_multiples[0], fee_schedule=FEE, initial_balance=100_000.0,
                         slippage_ticks=slippage, spread_raw={300: (bars_a, bars_b)},
                         spread_indexes={300: (ja, jb)}, leg_ticks=leg_ticks, leg_multiples=leg_multiples)


# -- (1) the spread series --------------------------------------------------------------------------------


def test_join_inner_joins_by_time_and_carries_both_legs():
    bars_a = _bars([10.0, 11.0, 12.0])
    bars_b = [_bars([7.0])[0], _bars([9.0])[0] | {"datetime": T0 + 2 * 300 * NS}]   # B 缺中间那根
    joined, ja, jb = join_legs(_spec(), bars_a, bars_b)
    assert [bar["datetime"] for bar in joined] == [T0, T0 + 2 * 300 * NS]      # 缺一根：整根不判定
    assert (ja, jb) == ([0, 2], [0, 1])
    assert [bar["close"] for bar in joined] == [3.0, 3.0]
    assert [bar["open"] for bar in joined] == [3.0, 3.0]
    assert joined[0]["high"] == 3.0 and joined[0]["low"] == 3.0                # high/low 只有开收盘
    assert joined[0]["volume"] == 0.0 and joined[0]["tradingDay"] == "2026-09-01"
    assert joined[0]["legA"] == {"open": 110.0, "close": 110.0}
    assert joined[0]["legB"] == {"open": 107.0, "close": 107.0}


def test_join_weights_and_ratio():
    spec = _spec(legs=[{"symbol": SYMBOL_A, "weight": 2}, {"symbol": SYMBOL_B, "weight": 3}])
    joined, _, _ = join_legs(spec, _bars([10.0]), _bars([4.0]))
    assert joined[0]["close"] == 2 * 110.0 - 3 * 104.0
    ratio = _spec(kind="ratio", legs=[{"symbol": SYMBOL_A, "weight": 2}, {"symbol": SYMBOL_B, "weight": 3}])
    joined, _, _ = join_legs(ratio, _bars([10.0]), _bars([4.0]))
    assert joined[0]["close"] == pytest.approx(110.0 / 104.0)                  # ratio 无视权重
    with pytest.raises(StrategyRuleError) as error:
        spread_price("ratio", 110.0, 0.0, 1, 1)
    assert error.value.code == "spread_ratio_zero"


def test_workbench_tail_is_the_true_forming_bar_or_a_valueless_sentinel():
    spec = _spec()
    forming = {**_bars([12.0])[0], "datetime": T0 + 2 * 300 * NS}
    workbench = spread_workbenches(spec, {"bars": [*_bars([10.0, 11.0]), forming], "quote": {}},
                                   {"bars": [*_bars([10.0, 11.0]), forming], "quote": {}})
    assert [bar["close"] for bar in workbench["bars"]] == [0.0, 0.0, 0.0]      # 尾部 = 两腿共同形成中的K线
    earlier = {**_bars([8.0])[0], "datetime": T0 + 300 * NS}                   # B 腿慢一根：只有哨兵
    workbench = spread_workbenches(spec, {"bars": [*_bars([10.0, 11.0]), forming], "quote": {}},
                                   {"bars": [*_bars([6.0]), earlier], "quote": {}})
    first, tail = workbench["bars"]
    assert (first["datetime"], first["close"]) == (T0, 4.0)                    # 已完成的判定K线还在
    assert (tail["datetime"], tail["close"]) == (T0 + 2 * 300 * NS, 0.0)       # 哨兵的值永不被读


class _LegProvider:
    """Both legs' workbenches from canned dicts, by symbol."""

    def __init__(self, workbenches: dict[str, dict]) -> None:
        self.workbenches = workbenches

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict:
        return self.workbenches[symbol]


def _fresh_workbench(bars: list[dict]) -> dict:
    return {"bars": bars, "quote": {}, "dataStatus": "simulated", "isCached": False, "ageSeconds": 0}


def test_evaluation_names_the_leg_that_is_stale_or_unreadable():
    plan = _plan()
    bars = _bars([10.0, 11.0]) + [{**_bars([12.0])[0], "datetime": T0 + 2 * 300 * NS}]
    fresh = _LegProvider({SYMBOL_A: _fresh_workbench(bars), SYMBOL_B: _fresh_workbench(bars)})
    assert spread_evaluation(fresh, plan)["rules"]["entryLong"] is True        # 两腿都新鲜时正常判定
    stale_b = _LegProvider({SYMBOL_A: _fresh_workbench(bars), SYMBOL_B: _fresh_workbench(bars) | {"dataStatus": "stale"}})
    with pytest.raises(StrategyRuleError) as error:
        spread_evaluation(stale_b, plan)
    assert f"B腿{SYMBOL_B} 5 分钟" in str(error.value)

    class _Broken:
        def market_workbench(self, symbol: str, duration_seconds: int) -> dict:
            raise KeyError(symbol)

    with pytest.raises(ValueError) as error:
        spread_evaluation(_Broken(), plan)
    assert f"A腿{SYMBOL_A} 5 分钟行情读取失败，请刷新行情后重试" in str(error.value)


def test_live_window_matches_the_whole_series_at_every_bar():
    """The join of each leg's own last 255 bars — what live reads — values identical at every bar."""
    bars_a = _bars([100.0 + (index % 37) * 0.25 for index in range(300)])
    bars_b = [bar for index, bar in enumerate(_bars([(index % 11) * 0.5 for index in range(300)]))
              if index % 7 != 3]                                                # B 腿缺每第 7 根
    spec = _spec()
    joined, ja, jb = join_legs(spec, bars_a, bars_b)
    facts = SeriesFacts(joined)
    ma = Operand.model_validate({"indicator": "ma", "period": 20, "timeframe": 300})
    for k in [*range(0, len(joined), 17), len(joined) - 1]:
        window = SeriesWindow(facts, joined_window_start(ja, jb, k, 255), k + 1)
        live, _, _ = join_legs(spec, bars_a[max(0, ja[k] + 1 - 255):ja[k] + 1],
                               bars_b[max(0, jb[k] + 1 - 255):jb[k] + 1])
        assert [bar["datetime"] for bar in live] == [bar["datetime"] for bar in window]
        assert [bar["close"] for bar in live] == [bar["close"] for bar in window]
        assert indicator_values(window, ma)[-1] == pytest.approx(indicator_values(live, ma)[-1])


# -- (2) validation ---------------------------------------------------------------------------------------


def _plan_error(**fields: Any) -> str:
    with pytest.raises(ValueError) as error:
        _plan(**fields)
    return str(error.value)


def test_same_legs_are_refused():
    assert "价差两条腿不能是同一个合约" in _plan_error(
        spread={"kind": "difference", "legs": [{"symbol": SYMBOL_A}, {"symbol": SYMBOL_A.upper()}]})


@pytest.mark.parametrize("fields, message", [
    ({"atrStop": {"multiple": 2}}, "价差策略不使用ATR 止损"),
    ({"stopLookbackBars": 5, "perTradeRisk": 2000}, "价差策略不使用最近K线结构止损"),
    ({"pullback": {"touchPeriod": 5, "guardPeriod": 10, "trendPeriod": 30}, "perTradeRisk": 2000},
     "价差策略不使用回调结构入场"),
    ({"trailingStop": {"mode": "percent", "value": 2}}, "价差策略不使用追踪止损"),
    ({"breakeven": {"triggerTicks": 10}}, "价差策略不使用保本"),
    ({"timeTakeProfit": [{"afterBars": 5, "profitTicks": 20}]}, "价差策略不使用分段止盈"),
    ({"pyramiding": {"maxAdds": 1, "trigger": "profit_ticks", "step": 5}, "maxPosition": 2},
     "价差策略不使用加仓"),
    ({"scaleOut": [{"profitTicks": 20, "percent": 50}]}, "价差策略不使用分批止盈"),
    ({"closeBeforeEndMinutes": 10}, "价差策略不使用收盘前平仓"),
    ({"perTradeRisk": 2000}, "价差策略不使用单笔预算"),
    ({"sizing": {"mode": "atr_risk", "riskPercent": 1}}, "价差策略不按 ATR 定手数"),
])
def test_first_phase_blocks_are_refused(fields: dict[str, Any], message: str):
    assert message in _plan_error(**fields)


def test_intraday_trigger_and_off_whitelist_indicators_are_refused():
    trigger = {"kind": "stop", "price": {"indicator": "close", "timeframe": 300}}
    assert "价差策略不使用盘中触价" in _plan_error(
        rules={"entryLong": {"conditions": [ALWAYS], "trigger": trigger}, "exitLong": {"conditions": [NEVER]}})
    atr_condition = {"label": "ATR", "left": {"indicator": "atr", "timeframe": 300}, "operator": "gt",
                     "right": {"indicator": "constant", "value": 1}}
    message = _plan_error(rules={"entryLong": {"conditions": [atr_condition]}, "exitLong": {"conditions": [NEVER]}})
    assert "价差策略的指标只能用只看开/收盘价的" in message and "「ATR14」" in message
    offset_condition = {"label": "偏移", "left": {"indicator": "close", "timeframe": 300, "offset": {"factor": 1.5,
                        "indicator": "atr"}}, "operator": "gt", "right": {"indicator": "constant", "value": 0}}
    assert "价差策略的指标不带系数偏移" in _plan_error(
        rules={"entryLong": {"conditions": [offset_condition]}, "exitLong": {"conditions": [NEVER]}})


def test_ratio_ignores_weights_and_tick_stops_do_not_apply():
    plan = _plan(spread={"kind": "ratio", "stopLoss": 0.2, "takeProfit": 0.5,
                         "legs": [{"symbol": SYMBOL_A, "weight": 2}, {"symbol": SYMBOL_B, "weight": 3}]},
                 stopLossTicks=30)
    assert plan.spread.kind == "ratio" and plan.spread.stopLoss == pytest.approx(0.2)
    assert plan.stopLossTicks == 30                                        # 记录在但不生效（价差用自己的点数）


# -- (3) the engine ---------------------------------------------------------------------------------------


def _decide(plan: StrategyPlan, closes: list[float], state: PositionState) -> Any:
    """Pad the front with inert bars (the evaluator needs a minimum window); only the last bar decides."""
    closes = [10.0] * 11 + list(closes)
    joined, _, _ = join_legs(plan.spread, _bars(closes), _bars([0.0] * len(closes)))
    return decide(plan, evaluate_completed(plan, {300: joined}), state, MARKET)


def test_open_is_max_position_units_with_no_armed_price_stop():
    decision = _decide(_plan(maxPosition=3), [20.0], PositionState())
    assert decision.action == "open_long" and decision.volume == 3           # 一份价差 = maxPosition 份
    assert decision.stopPrice is None and decision.takeProfitPrice is None   # 价差不挂价格止损止盈


def test_spread_stop_and_take_are_judged_on_the_close():
    plan = _plan(spread={"kind": "difference", "stopLoss": 5, "takeProfit": 5,
                         "legs": [{"symbol": SYMBOL_A}, {"symbol": SYMBOL_B}]})
    opened = open_position(plan, _decide(plan, [20.0], PositionState()), fill_price=15.0,
                           fill_time=T0, price_tick=1.0, volume=1)
    stopped = _decide(plan, [8.0], opened)                                  # 收盘价差 8 − 15 = −7 ≤ −5
    assert stopped.exitReason == "spread_stop" and stopped.exitKey and stopped.exitKey.startswith("spread_stop:")
    assert _decide(plan, [22.0], opened).exitReason == "spread_take"         # 22 − 15 = 7 ≥ 5
    once = _decide(plan, [8.0], replace(opened, fired_exits=frozenset({stopped.exitKey})))
    assert once.exitReason is None and once.action == "hold"                 # 同一持仓只判一次


def test_rule_exit_and_max_hold_around_the_spread_stop():
    plan = _plan(spread={"kind": "difference", "stopLoss": 5,
                         "legs": [{"symbol": SYMBOL_A}, {"symbol": SYMBOL_B}]},
                 rules={"entryLong": {"conditions": [ALWAYS]}, "exitLong": {"conditions": [ALWAYS]}})
    opened = open_position(plan, _decide(plan, [20.0], PositionState()), fill_price=15.0,
                           fill_time=T0, price_tick=1.0, volume=1)
    assert _decide(plan, [8.0], opened).exitReason == "rule"                 # 规则出场在前
    hold = _plan(maxHoldBars=2, rules={"entryLong": {"conditions": [ALWAYS]}, "exitLong": {"conditions": [NEVER]}})
    held = replace(open_position(hold, _decide(hold, [20.0], PositionState()), fill_price=15.0,
                                 fill_time=T0, price_tick=1.0, volume=1), bars_held=2)
    assert _decide(hold, [16.0], held).exitReason == "max_hold"             # 价差止损未到，最长持仓到期


def test_short_side_flips_the_sign():
    plan = _plan(direction="short",
                 spread={"kind": "difference", "stopLoss": 5, "takeProfit": 5,
                         "legs": [{"symbol": SYMBOL_A}, {"symbol": SYMBOL_B}]},
                 rules={"entryShort": {"conditions": [ALWAYS]}, "exitShort": {"conditions": [NEVER]}})
    opened = open_position(plan, _decide(plan, [10.0], PositionState()), fill_price=15.0,
                           fill_time=T0, price_tick=1.0, volume=1)
    assert _decide(plan, [22.0], opened).exitReason == "spread_stop"        # 空头：15 − 22 = −7
    assert _decide(plan, [8.0], opened).exitReason == "spread_take"         # 空头：15 − 8 = 7


# -- (4) the two-leg backtest -----------------------------------------------------------------------------


def test_each_leg_fills_at_its_own_next_open_and_the_row_carries_both_legs():
    plan = _plan(rules={"entryLong": {"conditions": [_condition("close", "gt", 5.5)]},
                        "exitLong": {"conditions": [NEVER]}})
    bars_a = _bars([float(index) for index in range(40)])                    # 价差收盘 = index
    bars_b = [bar for index, bar in enumerate(_bars([0.0] * 40)) if index != 7]  # B 缺决策后的第一根
    result = run_backtest(_setup(plan, bars_a, bars_b))
    [trade] = result["trades"]
    assert trade["direction"] == "long" and trade["volume"] == 1 and trade["exitReason"] == "end_of_data"
    assert trade["openPrice"] == 7.0 and trade["closePrice"] == 39.0         # 行内价格 = 价差数值
    leg_a, leg_b = trade["legs"]
    assert leg_a["symbol"] == SYMBOL_A and leg_a["direction"] == "buy" and leg_a["volume"] == 1
    assert leg_a["openPrice"] == 107.0 and leg_a["openTime"] == T0 + 7 * 300 * NS     # A 腿自己的下一根
    assert leg_b["symbol"] == SYMBOL_B and leg_b["direction"] == "sell"
    assert leg_b["openPrice"] == 100.0 and leg_b["openTime"] == T0 + 8 * 300 * NS     # B 缺 t7：落在 t8
    assert leg_a["closePrice"] == 139.0 and leg_b["closePrice"] == 100.0
    assert leg_a["gross"] == pytest.approx(320.0) and leg_b["gross"] == 0.0  # 各腿自己的乘数
    assert leg_a["fees"] == pytest.approx(6.0) and leg_b["fees"] == pytest.approx(6.0)
    assert trade["net"] == pytest.approx(308.0)                              # 320 − 12
    assert result["spread"] == {"kind": "difference", "legs": [
        {"symbol": SYMBOL_A, "lots": 1, "weight": 1, "priceTick": 1.0, "volumeMultiple": 10.0},
        {"symbol": SYMBOL_B, "lots": 1, "weight": 1, "priceTick": 2.0, "volumeMultiple": 20.0}]}
    assert result["spreadCurve"][-1]["value"] == pytest.approx(39.0)


def test_slippage_and_fees_are_per_leg_and_units_follow_the_legs_lots():
    plan = _plan(maxPosition=2, spread={"kind": "difference",
                                        "legs": [{"symbol": SYMBOL_A, "lots": 2}, {"symbol": SYMBOL_B, "lots": 3}]},
                 rules={"entryLong": {"conditions": [_condition("close", "gt", 5.5)]},
                        "exitLong": {"conditions": [NEVER]}})
    result = run_backtest(_setup(plan, _bars([float(index) for index in range(40)]),
                                 _bars([0.0] * 40), slippage=1))
    [trade] = result["trades"]
    assert trade["volume"] == 2                                               # 2 份价差
    leg_a, leg_b = trade["legs"]
    assert (leg_a["volume"], leg_b["volume"]) == (4, 6)                       # 每份 A 2 手、B 3 手
    assert leg_a["openPrice"] == 107.0 + 1 * 1.0                              # 买入 +1 跳（tick 1）
    assert leg_b["openPrice"] == 100.0 - 1 * 2.0                              # 卖出 −1 跳（tick 2）
    assert leg_a["closePrice"] == 139.0 - 1 * 1.0 and leg_b["closePrice"] == 100.0 + 1 * 2.0
    assert leg_a["gross"] == pytest.approx((138.0 - 108.0) * 4 * 10.0)
    assert leg_b["gross"] == pytest.approx((102.0 - 98.0) * 6 * 20.0 * -1)
    assert leg_a["fees"] == pytest.approx(24.0) and leg_b["fees"] == pytest.approx(36.0)
    assert trade["net"] == pytest.approx(leg_a["gross"] + leg_b["gross"] - 60.0)


def test_backtest_closes_the_whole_spread_on_the_spread_stop():
    plan = _plan(spread={"kind": "difference", "stopLoss": 5,
                         "legs": [{"symbol": SYMBOL_A}, {"symbol": SYMBOL_B}]},
                 rules={"entryLong": {"conditions": [_condition("close", "gt", 5.5)]},
                        "exitLong": {"conditions": [NEVER]}})
    closes = [float(index) for index in range(10)] + [0.0] * 30              # t10 收盘 0 ≤ 7 − 5：止损
    result = run_backtest(_setup(plan, _bars(closes), _bars([0.0] * 40)))
    [trade] = result["trades"]
    assert trade["exitReason"] == "spread_stop" and trade["exitLabel"] == "价差止损"
    assert trade["openPrice"] == 7.0 and trade["closePrice"] == 0.0           # t10 判定，t11 两腿开盘


def test_end_of_data_spread_close_counts_its_fees_in_the_curve_and_drawdown():
    """#505：价差回测结束强平同样把两腿最后一笔费用计进权益曲线与最大回撤，不只改最终权益。"""
    result = run_backtest(_setup(_plan(), _bars([0.0] * 40), _bars([0.0] * 40)))
    [trade] = result["trades"]
    assert trade["exitReason"] == "end_of_data" and trade["fees"] == 12.0     # 两腿各 开3 + 平3
    assert result["summary"]["netProfit"] == -12.0 and result["equityCurve"][-1]["equity"] == 99_988.0
    assert result["summary"]["maxDrawdown"] == 12.0 and result["summary"]["maxDrawdownPercent"] == 0.01


class _HistoryProvider:
    """Both legs' history for `execute` (it drops each leg's last bar as the forming one)."""

    def __init__(self, bars_a: list[dict], bars_b: list[dict]) -> None:
        self.bars = {SYMBOL_A: bars_a, SYMBOL_B: bars_b}

    def market_history(self, symbol: str, timeframe: int, length: int) -> dict:
        return {"bars": self.bars[symbol], "priceTick": 1.0, "volumeMultiple": 10.0, "tradingSchedule": SHFE}


def test_execute_coerces_the_symbol_to_the_a_leg():
    plan = _plan(rules={"entryLong": {"conditions": [_condition("close", "gt", 5.5)]},
                        "exitLong": {"conditions": [NEVER]}})
    provider = _HistoryProvider(_bars([float(index) for index in range(40)]), _bars([0.0] * 40))
    result = execute(provider, plan=plan, symbol=SYMBOL_B, fee_schedule=FEE, trading_days=None,
                     slippage_ticks=0, initial_balance=100_000.0)
    assert result["symbol"] == SYMBOL_A and result["summary"]["trades"] == 1


# -- (5) live: 只提醒 -------------------------------------------------------------------------------------


class Provider(NormalProvider):
    """The fake account with per-leg walked bars: `walk_to(symbol, close)` completes one 300s bar of
    that leg only (both legs start from the identical 80-bar fixture, so an un-walked joined spread
    close is exactly 0.0)."""

    def __init__(self) -> None:
        super().__init__()
        self.day = "2026-09-01"
        self.activity: dict[str, Any] = {"dataStatus": "normal", "isCached": False, "ageSeconds": 0,
                                         "ordersInitialized": True, "orders": [], "trades": []}
        self.position_items: list[dict[str, Any]] = []
        self.walked: dict[str, list[float]] = {}

    def account_activity(self) -> dict[str, Any]:
        return self.activity

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        return {**super().account_summary(turn_id), "tradingDay": self.day}

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        workbench = fake_market_workbench(symbol, duration_seconds)
        bars = list(workbench["bars"])
        for close in self.walked.get(symbol, []):
            bars.append(_walked_bar(bars[-1], close, duration_seconds))
        if self.walked.get(symbol):
            bars.append(_walked_bar(bars[-1], self.walked[symbol][-1], duration_seconds))   # forming tail
        workbench["bars"] = bars
        workbench["quote"]["tradingSchedule"] = SHFE
        return workbench

    def walk_to(self, symbol: str, close: float) -> None:
        self.walked.setdefault(symbol, []).append(close)


def _walked_bar(previous: dict[str, Any], close: float, duration_seconds: int) -> dict[str, Any]:
    timestamp = int(previous["datetime"]) + duration_seconds * NS
    opening = round(close - 0.3, 2)
    return {"datetime": timestamp, "open": opening, "high": round(max(opening, close) + 0.1, 2),
            "low": round(min(opening, close) - 0.1, 2), "close": close, "volume": 100,
            "openInterest": previous["openInterest"], "tradingDay": trading_day_of(timestamp)}


def _app(path, provider, gateway, plan: StrategyPlan, *, signal_action: str = "alert"):
    app = application(path, gateway, provider)
    store = app.state.store
    store.ensure_default_trading_account(execution_owner_user_id=USER, provider_kind="fake",
                                         display_name="假数据模拟账户", sidecar_url=None)
    store.save_strategy_draft(strategy_id=STRATEGY_ID, user_id=USER, source_session_id="session-spread",
                              name="价差测试", symbol=SYMBOL_A, version=1, status="monitoring",
                              definition=definition_for(plan), definition_digest="d" * 64)
    store.save_strategy_validation_report(report_id="report-spread", strategy_id=STRATEGY_ID,
                                          user_id=USER, strategy_digest="d" * 64, status="passed",
                                          checks=[], evidence={})
    session = app.state.broker.create_session("local", "native", USER)
    store.set_strategy_signal_action(STRATEGY_ID, user_id=USER, signal_action=signal_action,
                                     session_id=session.session_id)
    monitor = StrategyMonitorService(store=store, proposals=app.state.proposals, autonomy=app.state.autonomy)
    return app, monitor


def _events(app, name: str) -> list[dict[str, Any]]:
    return [event["payload"]
            for session in app.state.store.connection.execute("SELECT id FROM sessions").fetchall()
            for _, event in app.state.store.list_events(session[0]) if event.get("name") == name]


async def test_live_open_alert_names_both_legs_and_sends_nothing(tmp_path):
    provider, gateway = Provider(), Gateway()
    app, monitor = _app(tmp_path / "spread.sqlite3", provider, gateway,
                        _plan(rules={"entryLong": {"conditions": [GT_ZERO]}, "exitLong": {"conditions": [NEVER]}}))
    provider.walk_to(SYMBOL_A, 830.0)
    provider.walk_to(SYMBOL_B, 810.0)                      # 价差收盘 20 > 0：第一次成立
    result = (await monitor.run_once())[0]
    assert result["status"] == "alerted", result
    assert "做多价差条件满足（只提醒，未下单）" in result["detail"]
    assert "买SHFE.rb2701 1 手、卖SHFE.rb2705 1 手" in result["detail"]
    assert "SHFE.rb2701 − SHFE.rb2705" in result["detail"]
    assert gateway.submit_count == 0
    assert len([item for item in _events(app, "message.completed") if "只提醒，未下单" in str(item.get("text", ""))]) == 1

    again = (await monitor.run_once())[0]                  # 同一根已完成K线只提醒一次
    assert again["status"] in {"waiting", "duplicate"} and gateway.submit_count == 0
    assert len([item for item in _events(app, "message.completed") if "只提醒，未下单" in str(item.get("text", ""))]) == 1

    restarted = StrategyMonitorService(store=app.state.store, proposals=app.state.proposals,
                                       autonomy=app.state.autonomy)
    after = (await restarted.run_once())[0]                # 重启后同一根K线不重复
    assert after["status"] in {"waiting", "duplicate"} and gateway.submit_count == 0
    assert len([item for item in _events(app, "message.completed") if "只提醒，未下单" in str(item.get("text", ""))]) == 1
    app.state.store.close()


async def test_confirm_mode_proposes_one_card_for_both_legs(tmp_path):
    provider, gateway = Provider(), Gateway()
    app, monitor = _app(tmp_path / "confirm.sqlite3", provider, gateway,
                        _plan(rules={"entryLong": {"conditions": [GT_ZERO]}, "exitLong": {"conditions": [NEVER]}}),
                        signal_action="confirm")
    provider.walk_to(SYMBOL_A, 830.0)
    provider.walk_to(SYMBOL_B, 810.0)
    result = (await monitor.run_once())[0]
    assert result["status"] == "proposed" and gateway.submit_count == 0
    cards = _events(app, "proposal.spread")
    assert len(cards) == 1 and _events(app, "proposal.trade") == []   # 一张卡带两腿，不再每腿一张
    assert [leg["symbol"] for leg in cards[0]["spread"]["legs"]] == [SYMBOL_A, SYMBOL_B]
    app.state.store.close()


async def test_live_exit_alert_reads_the_legs_from_the_account(tmp_path):
    provider, gateway = Provider(), Gateway()
    app, monitor = _app(tmp_path / "exit.sqlite3", provider, gateway,
                        _plan(rules={"entryLong": {"conditions": [NEVER]}, "exitLong": {"conditions": [ALWAYS]}}))
    provider.position_items = [
        {"symbol": SYMBOL_A, "direction": "long", "volume": 1, "openPrice": 820.0},
        {"symbol": SYMBOL_B, "direction": "short", "volume": 1, "openPrice": 815.0},
    ]
    result = (await monitor.run_once())[0]
    assert result["status"] == "alerted", result
    assert "平掉做多价差条件满足（只提醒，未下单）" in result["detail"]
    assert "卖SHFE.rb2701 1 手、买SHFE.rb2705 1 手" in result["detail"]
    assert gateway.submit_count == 0
    app.state.store.close()


async def test_start_route_accepts_every_execution_mode(tmp_path):
    provider, gateway = Provider(), Gateway()
    app, _ = _app(tmp_path / "route.sqlite3", provider, gateway, _plan())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173",
                                    "Authorization": "Bearer test-local-token"}) as client:
        assert (await client.post("/api/auth/local")).status_code == 200
        for action in ("alert", "confirm", "auto"):
            response = await client.post(f"/api/strategies/{STRATEGY_ID}/monitor/start",
                                         json={"signalAction": action})
            assert response.status_code == 200
            assert response.json()["signalAction"] == action
    app.state.store.close()


def test_spread_position_from_the_accounts_legs():
    spec = _spec(legs=[{"symbol": SYMBOL_A, "lots": 2}, {"symbol": SYMBOL_B, "lots": 3}])
    positions = [{"symbol": SYMBOL_A, "direction": "long", "volume": 6, "openPrice": 10.0},
                 {"symbol": SYMBOL_A, "direction": "long", "volume": 2, "openPrice": 14.0},
                 {"symbol": SYMBOL_B, "direction": "short", "volume": 9, "openPrice": 4.0}]
    held = spread_position(spec, positions)                # A 多 8（均价 11）、B 空 9：3 份
    assert held["direction"] == "long" and held["volume"] == 3
    assert held["entry_price"] == pytest.approx(11.0 - 4.0)
    assert spread_position(spec, positions[:1]) == {"direction": None, "volume": 0, "entry_price": None}
    mixed = [*positions, {"symbol": SYMBOL_A, "direction": "short", "volume": 1, "openPrice": 9.0}]
    assert spread_position(spec, mixed)["direction"] is None               # A 腿两向都有：不读作价差持仓
    short = [{"symbol": SYMBOL_A, "direction": "short", "volume": 2, "openPrice": 10.0},
             {"symbol": SYMBOL_B, "direction": "long", "volume": 3, "openPrice": 5.0}]
    held = spread_position(_spec(kind="ratio", legs=[{"symbol": SYMBOL_A, "lots": 2},
                                                     {"symbol": SYMBOL_B, "lots": 3}]), short)
    assert held["direction"] == "short" and held["volume"] == 1
    assert held["entry_price"] == pytest.approx(10.0 / 5.0)


def test_legs_and_words():
    spec = _spec(legs=[{"symbol": SYMBOL_A, "lots": 2}, {"symbol": SYMBOL_B, "lots": 3}])
    assert spread_legs_words(spec, "open_long", 2) == "买SHFE.rb2701 4 手、卖SHFE.rb2705 6 手"
    assert spread_legs_words(spec, "close_long", 1) == "卖SHFE.rb2701 2 手、买SHFE.rb2705 3 手"
    assert spread_words(_spec()) == "SHFE.rb2701 − SHFE.rb2705"
    assert spread_words(_spec(legs=[{"symbol": SYMBOL_A, "weight": 2}, {"symbol": SYMBOL_B}])) == \
        "SHFE.rb2701×2 − SHFE.rb2705×1"
    assert spread_words(_spec(kind="ratio")) == "SHFE.rb2701 ÷ SHFE.rb2705"


# -- (6) the template ------------------------------------------------------------------------------------


def test_template_builds_resolves_and_summarizes():
    template = templates()["spread_boll_reversion"]
    plan, values = build_plan(template, {"near": "SHFE.rb2705", "far": "SHFE.rb2710"})
    assert [leg.symbol for leg in plan.spread.legs] == ["SHFE.rb2705", "SHFE.rb2710"]
    assert plan.spread.stopLoss == 60 and values["near"] == "SHFE.rb2705"
    with pytest.raises(TemplateError) as error:
        resolve_params(template, {"near": "rb2705"})
    assert str(error.value) == "近月合约须是具体合约代码，例如 SHFE.rb2701"
    with pytest.raises(TemplateError) as error:
        build_plan(template, {"near": "SHFE.rb2705"})      # 与默认远月同合约
    assert "价差两条腿不能是同一个合约" in str(error.value)
    near = next(item for item in summary(template)["params"] if item["key"] == "near")
    assert near["type"] == "contract" and near["default"] == "SHFE.rb2701"
    assert near["min"] is None and near["max"] is None and near["step"] is None

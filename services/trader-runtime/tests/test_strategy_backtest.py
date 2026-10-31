"""规则回测器 (策略积木·第一期 B, 2026-09-25).

Known results on constructed K-lines, bar-by-bar parity with the live judgement (evaluate_rules +
decide over the live windows), the data-coverage report, the routes' ownership and the timing of
a 10 000-bar one-minute run.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.data_providers import fake_market_history
from trader_runtime.main import create_app
from trader_runtime.strategy_backtest import LIVE_WINDOW, BacktestDataError, BacktestSetup, run_backtest
from trader_runtime.strategy_engine import MarketContext, decide
from trader_runtime.strategy_health import apply_advice
from trader_runtime.strategy_rules import StrategyPlan, evaluate_rules

CST = timezone(timedelta(hours=8))
SHFE = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
        "night": [["21:00:00", "25:00:00"]]}
FEES = {"source": "custom", "default": {"mode": "per_lot", "open": 2, "closeToday": 3, "closeYesterday": 3}, "overrides": []}
NO_FEES = {"source": "account", "default": {"mode": "per_lot", "open": 0, "closeToday": 0, "closeYesterday": 0}, "overrides": []}


def ns(year, month, day, hour=0, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=CST).timestamp() * 1_000_000_000)


def series(rows, start=None, step=300):
    """rows: (open, high, low, close); every bar carries its trading day like the sidecar's."""
    start = ns(2026, 9, 23, 9, 0) if start is None else start
    result = []
    for i, (o, h, low, c) in enumerate(rows):
        timestamp = start + i * step * 1_000_000_000
        local = datetime.fromtimestamp(timestamp / 1e9, tz=CST)
        result.append({"datetime": timestamp, "open": o, "high": h, "low": low, "close": c, "volume": 100,
                       "tradingDay": local.date().isoformat()})
    return result


def gapless(closes, first_open=None):
    rows, previous = [], closes[0] if first_open is None else first_open
    for close in closes:
        rows.append((previous, max(previous, close) + 1, min(previous, close) - 1, close))
        previous = close
    return rows


def cond(label, left, operator, right, tf=300, **params):
    return {"label": label, "left": {"indicator": left, "timeframe": tf, **params}, "operator": operator,
            "right": {"timeframe": tf, **right}}


ALWAYS = [cond("总是", "close", "gt", {"indicator": "constant", "value": 0})]
NEVER = [cond("从不", "close", "lt", {"indicator": "constant", "value": 0})]


def plan(entry=None, exit_=None, **fields):
    rules = {"entryLong": {"conditions": entry or ALWAYS}}
    if exit_:
        rules["exitLong"] = {"conditions": exit_}
    return StrategyPlan.model_validate({"name": "回测", "description": "回测测试", "direction": "long",
                                        "timeframeSeconds": 300, "maxPosition": 1, "stopLossTicks": 20,
                                        "takeProfitTicks": None, "cooldownBars": 0, "rules": rules, **fields})


def run(p, bars, fees=NO_FEES, **fields):
    return run_backtest(BacktestSetup(plan=p, symbol="SHFE.rb2610", series={300: bars}, price_tick=1,
                                      volume_multiple=10, fee_schedule=fees, initial_balance=100_000, **fields))


DOUBLE_MA = plan(entry=[cond("金叉", "ma", "cross_up", {"indicator": "ma", "period": 4}, period=2)],
                 exit_=[cond("死叉", "ma", "cross_down", {"indicator": "ma", "period": 4}, period=2)])
# MA2 crosses MA4 up on bar 6 and 18, down on bar 12 and 20 (worked in the test below).
DOUBLE_MA_CLOSES = [100] * 6 + [104, 108, 112, 116, 118, 116, 112, 104, 100, 100, 100, 100, 104, 102, 96, 96, 96]


def test_double_moving_average_one_winner_one_loser_with_fees():
    result = run(DOUBLE_MA, series(gapless(DOUBLE_MA_CLOSES)), fees=FEES)
    trades = result["trades"]
    # Signals confirm on the close; both legs fill at the next bar's open (= previous close here).
    assert [(t["openPrice"], t["closePrice"], t["exitReason"]) for t in trades] == [(104, 112, "rule"), (104, 96, "rule")]
    assert [t["gross"] for t in trades] == [80, -80]
    assert [t["fees"] for t in trades] == [5, 5]            # 开仓 2 + 平今 3
    assert [t["net"] for t in trades] == [75, -85]
    summary = result["summary"]
    assert summary["netProfit"] == -10 and summary["trades"] == 2 and summary["winRate"] == 50
    assert summary["payoffRatio"] == pytest.approx(0.88)
    # Equity peaks at 100 138 (bar 10's close, the paid 开仓费 already in) and bottoms at 99 990.
    assert summary["maxDrawdown"] == 148
    assert summary["fees"] == 10
    assert result["exitReasons"] == [{"reason": "rule", "label": "规则出场", "count": 2, "net": -10}]
    assert result["equityCurve"][-1]["equity"] == 99_990
    assert any("按收盘确认" in line for line in result["assumptions"])


def test_atr_stop_hit_fills_at_the_stop_price():
    p = plan(atrStop={"multiple": 2, "period": 2})
    bars = series([(100, 101, 99, 100)] * 3 + [(100, 101, 99, 100), (100, 100, 95, 97)])
    # Signal on bar 2 (ATR2 = 2) → fill bar 3 open 100 → stop 100 − 4 = 96, hit by bar 4's low 95.
    trade = run(p, bars)["trades"][0]
    assert (trade["openPrice"], trade["closePrice"], trade["exitReason"]) == (100, 96, "stop_loss")


def test_trailing_stop_tightens_then_hits_the_trailed_level():
    p = plan(trailingStop={"mode": "ticks", "value": 5}, stopLossTicks=50)
    rows = [(100, 101, 99, 100)] * 2 + [(100, 104, 99, 103), (103, 108, 102, 107), (107, 111, 106, 110), (110, 110, 104, 105)]
    trade = run(p, series(rows))["trades"][0]
    # Entry 100 at bar 2's open; highs 104 → 108 → 111 move the stop 99 → 103 → 106; bar 5's low 104 hits 106.
    assert (trade["openPrice"], trade["closePrice"], trade["exitReason"]) == (100, 106, "trailing_stop")


def test_gap_through_the_stop_fills_at_the_open():
    p = plan(stopLossTicks=5)
    rows = [(100, 101, 99, 100)] * 2 + [(100, 101, 99, 100), (90, 91, 88, 89)]
    trade = run(p, series(rows))["trades"][0]
    assert (trade["closePrice"], trade["exitReason"]) == (90, "stop_loss")   # stop 95, opened at 90


def test_one_bar_touching_stop_and_take_counts_as_the_stop():
    p = plan(stopLossTicks=5, takeProfitTicks=5)
    rows = [(100, 101, 99, 100)] * 2 + [(100, 101, 99, 100), (100, 106, 94, 100)]
    trade = run(p, series(rows))["trades"][0]
    assert (trade["closePrice"], trade["exitReason"]) == (95, "stop_loss")


def test_close_before_end_exits_at_the_next_open_inside_the_session():
    p = plan(exit_=NEVER, closeBeforeEndMinutes=5, maxOrdersPerDay=1)
    rows = [(100 + i, 101 + i, 99 + i, 100 + i) for i in range(12)]
    bars = series(rows, start=ns(2026, 9, 23, 14, 0))   # the last bar opens 14:55, closes 15:00
    result = run(p, bars, schedule=SHFE)
    trade = result["trades"][0]
    assert trade["exitReason"] == "close_before_end"
    assert trade["closeTime"] == ns(2026, 9, 23, 14, 55) and trade["closePrice"] == 111


def always_long(timeframe):
    return plan(timeframeSeconds=timeframe, closeBeforeEndMinutes=5, maxOrdersPerDay=1,
                rules={"entryLong": {"conditions": [cond("总是", "close", "gt", {"indicator": "constant", "value": 0}, tf=timeframe)]},
                       "exitLong": {"conditions": [cond("从不", "close", "lt", {"indicator": "constant", "value": 0}, tf=timeframe)]}})


def test_close_before_end_inside_a_coarse_bar_fills_at_that_minute():
    """收盘前平仓 closes by the clock live (14:55): a 15-minute bar 14:45–15:00 holds that time, so the
    backtest fills at the 14:55 one-minute bar's open — never at the next day's open (90)."""
    coarse = always_long(900)
    bars = series([(100, 101, 99, 100)] * 4, start=ns(2026, 9, 23, 14), step=900)
    bars.append({**bars[-1], "datetime": ns(2026, 9, 24, 9), "open": 90, "close": 90})
    minutes = series([(100 + i, 101 + i, 99 + i, 100 + i) for i in range(60)], start=ns(2026, 9, 23, 14), step=60)
    result = run_backtest(BacktestSetup(plan=coarse, symbol="SHFE.rb2610", series={900: bars, 60: minutes}, price_tick=1,
                                        volume_multiple=10, fee_schedule=NO_FEES, schedule=SHFE))
    trade = result["trades"][0]
    assert (trade["exitReason"], trade["closeTime"], trade["closePrice"]) == ("close_before_end", ns(2026, 9, 23, 14, 55), 155)
    assert not any("估算" in note for note in result["notes"])

    # Without that minute (only ~29 trading days of 1-minute bars exist): this bar's close, and the note says so.
    estimated = run_backtest(BacktestSetup(plan=coarse, symbol="SHFE.rb2610", series={900: bars}, price_tick=1,
                                           volume_multiple=10, fee_schedule=NO_FEES, schedule=SHFE))
    trade = estimated["trades"][0]
    assert trade["exitReason"] == "close_before_end" and trade["closePrice"] == 100
    assert trade["closeTime"] < ns(2026, 9, 24) and any("估算" in note for note in estimated["notes"])


def test_close_before_end_in_a_history_gap_closes_before_the_gap():
    fine = plan(exit_=NEVER, closeBeforeEndMinutes=5, maxOrdersPerDay=1)
    missing = series([(100, 101, 99, 100)] * 11, start=ns(2026, 9, 23, 14))   # 14:00 … 14:50 bars
    missing.append({**missing[-1], "datetime": ns(2026, 9, 23, 21), "open": 90, "close": 90})
    trade = run(fine, missing, schedule=SHFE)["trades"][0]
    assert trade["exitReason"] == "close_before_end" and trade["closePrice"] == 100
    assert trade["closeTime"] < ns(2026, 9, 23, 15)


def test_rule_exit_does_not_hide_a_close_time_inside_the_bar():
    entry = [cond("入场", "close", "gt", {"indicator": "constant", "value": 101}, tf=900)]
    exit_rule = [cond("规则出场", "close", "gt", {"indicator": "constant", "value": 102}, tf=900)]
    p = plan(timeframeSeconds=900, closeBeforeEndMinutes=5, maxOrdersPerDay=1,
             rules={"entryLong": {"conditions": entry}, "exitLong": {"conditions": exit_rule}})
    bars = series([(100, 101, 99, 100), (100, 101, 99, 100),
                   (100, 103, 99, 102), (100, 104, 99, 103)],
                  start=ns(2026, 9, 23, 14), step=900)
    bars.append({**bars[-1], "datetime": ns(2026, 9, 24, 9), "open": 90, "close": 90})
    minutes = series([(200, 201, 199, 200)] * 60, start=ns(2026, 9, 23, 14), step=60)
    result = run_backtest(BacktestSetup(plan=p, symbol="SHFE.rb2610", series={900: bars, 60: minutes}, price_tick=1,
                                        volume_multiple=10, fee_schedule=NO_FEES, schedule=SHFE))
    trade = result["trades"][0]
    assert (trade["exitReason"], trade["closeTime"], trade["closePrice"]) == ("close_before_end", ns(2026, 9, 23, 14, 55), 200)

    # A rule exit already filled at 14:45 leaves no position crossing 14:55.
    earlier = [dict(bar) for bar in bars]
    earlier[1]["close"] = 102
    earlier[2]["close"] = 103
    early_result = run_backtest(BacktestSetup(plan=p, symbol="SHFE.rb2610", series={900: earlier},
                                            price_tick=1, volume_multiple=10, fee_schedule=NO_FEES,
                                            schedule=SHFE))
    assert early_result["trades"][0]["closeTime"] == ns(2026, 9, 23, 14, 45)


def test_a_close_decided_before_an_overnight_gap_fills_that_day():
    p = plan(entry=[cond("入场", "close", "gt", {"indicator": "constant", "value": 101})],
             exit_=[cond("规则出场", "close", "gt", {"indicator": "constant", "value": 102})],
             closeBeforeEndMinutes=5, maxOrdersPerDay=1)
    bars = series([(100, 101, 99, 100), (100, 101, 99, 100),
                   (100, 103, 99, 102), (100, 104, 99, 103)],
                  start=ns(2026, 9, 23, 14, 35))
    bars.append({**bars[-1], "datetime": ns(2026, 9, 24, 9), "open": 90, "close": 90})
    trade = run(p, bars, schedule=SHFE)["trades"][0]
    assert trade["closePrice"] != 90 and trade["closeTime"] < ns(2026, 9, 24)


@pytest.mark.parametrize("scope,expected", [("session", ns(2026, 9, 23, 11, 25)),
                                             ("day", ns(2026, 9, 23, 14, 55))])
def test_close_before_end_respects_lunch_and_day_scope(scope, expected):
    p = plan(exit_=NEVER, closeBeforeEndMinutes=5, closeBeforeEndScope=scope, maxOrdersPerDay=1)
    morning = series([(100, 101, 99, 100)] * 12, start=ns(2026, 9, 23, 10, 30))
    afternoon = series([(100, 101, 99, 100)] * 18, start=ns(2026, 9, 23, 13, 30))
    result = run(p, morning + afternoon, schedule=SHFE)
    assert result["trades"][0]["closeTime"] == expected


def test_close_before_end_one_minute_and_midnight_night_session():
    minute = plan(timeframeSeconds=60, closeBeforeEndMinutes=5, maxOrdersPerDay=1,
                  rules={"entryLong": {"conditions": [cond("总是", "close", "gt",
                                                               {"indicator": "constant", "value": 0}, tf=60)]}})
    bars = series([(100, 101, 99, 100)] * 12, start=ns(2026, 9, 23, 14, 48), step=60)
    result = run_backtest(BacktestSetup(plan=minute, symbol="SHFE.rb2610", series={60: bars}, price_tick=1,
                                        volume_multiple=10, fee_schedule=NO_FEES, schedule=SHFE))
    assert result["trades"][0]["closeTime"] == ns(2026, 9, 23, 14, 55)

    night = plan(closeBeforeEndMinutes=5, closeBeforeEndScope="session", maxOrdersPerDay=1)
    night_bars = series([(100, 101, 99, 100)] * 6, start=ns(2026, 9, 24, 0, 30))
    assert run(night, night_bars, schedule=SHFE)["trades"][0]["closeTime"] == ns(2026, 9, 24, 0, 55)


@pytest.mark.parametrize("timeframe", [3600, 86400])
def test_close_before_end_backtests_hourly_and_daily(timeframe):
    """Hourly and daily strategies with 收盘前平仓 backtest (and get their 体检) like the others."""
    result = run_backtest(BacktestSetup(plan=always_long(timeframe), symbol="SHFE.rb2610",
                                        series={timeframe: series([(100, 101, 99, 100)] * 6, start=ns(2026, 9, 21, 9), step=timeframe)},
                                        price_tick=1, volume_multiple=10, fee_schedule=NO_FEES, schedule=SHFE))
    assert result["checkLine"]
    for trade in result["trades"]:
        assert trade["exitReason"] in {"close_before_end", "end_of_data"}


@pytest.mark.asyncio
async def test_close_before_end_backtests_over_http(tmp_path):
    app = create_app(tmp_path / "time-limit.sqlite3", runtime_state_root=tmp_path,
                     agent_mode="fake", local_token="bt-token")
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer bt-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            for timeframe in (300, 900):
                response = await client.post("/api/strategy-backtests", json={
                    "plan": always_long(timeframe).model_dump(), "symbol": "SHFE.au2612"})
                assert response.status_code == 200, response.text
                assert response.json()["checkLine"]


@pytest.mark.parametrize("threshold", [255, 256, 300, 500])
def test_time_take_profit_counts_full_position_history(threshold):
    p = plan(exit_=NEVER, timeTakeProfit=[{"afterBars": threshold, "profitTicks": 1}],
             maxOrdersPerDay=1, stopLossTicks=20)
    result = run(p, series([(100, 101, 99, 101)] * (620 if threshold <= 300 else 850)))
    assert (result["trades"][0]["exitReason"], result["trades"][0]["barsHeld"]) == (
        "time_take_profit", threshold)
    losing = run(p, series([(100, 101, 99, 100)] * 620))
    assert losing["trades"][0]["exitReason"] == "end_of_data"


def test_max_hold_closes_after_n_bars():
    p = plan(exit_=NEVER, maxHoldBars=3, maxOrdersPerDay=1)
    rows = [(100, 101, 99, 100)] * 8
    trade = run(p, series(rows))["trades"][0]
    assert (trade["exitReason"], trade["barsHeld"]) == ("max_hold", 3)
    assert trade["closeTime"] - trade["openTime"] == 3 * 300 * 1_000_000_000   # decided on the 3rd bar, filled next open


def test_atr_sizing_sets_the_volume():
    rows = [(100, 101, 99, 100)] * 4
    sized = plan(maxPosition=20, sizing={"mode": "atr_risk", "riskPercent": 0.5, "atrMultiple": 2, "atrPeriod": 2})
    # equity 100 000 × 0.5% ÷ (multiplier 10 × 2 × ATR2 2) = 12.5 → 12 lots.
    assert run(sized, series(rows))["trades"][0]["volume"] == 12
    capped = plan(maxPosition=5, sizing={"mode": "atr_risk", "riskPercent": 1, "atrMultiple": 2, "atrPeriod": 2})
    assert run(capped, series(rows))["trades"][0]["volume"] == 5          # 25 lots, capped by maxPosition


def test_entries_per_direction_count_per_trading_day():
    p = plan(stopLossTicks=1, maxEntriesPerDirection=1, maxOrdersPerDay=20)
    day1 = series([(100, 101, 99, 100), (100, 101, 99, 100), (100, 100, 98, 99), (99, 99, 97, 98), (98, 98, 96, 97)],
                  start=ns(2026, 9, 23, 9, 0))
    day2 = series([(97, 98, 96, 97), (97, 98, 96, 97), (97, 97, 95, 96)], start=ns(2026, 9, 24, 9, 0))
    trades = run(p, day1 + day2)["trades"]
    # Day 1: one entry, stopped out; later signals that day are refused. Day 2 opens again.
    assert [t["openTime"] for t in trades][:2] == [day1[2]["datetime"], day2[1]["datetime"]]
    assert all(t["exitReason"] == "stop_loss" for t in trades[:2])


def test_fees_follow_the_schedule_and_account_default_charges_nothing():
    bars = series(gapless(DOUBLE_MA_CLOSES))
    charged = run(DOUBLE_MA, bars, fees=FEES)
    free = run(DOUBLE_MA, bars)
    assert charged["summary"]["netProfit"] == free["summary"]["netProfit"] - 10
    assert charged["feeEstimate"]["source"] == "custom" and not charged["feeEstimate"]["warning"]
    assert free["feeEstimate"]["source"] == "none" and free["feeEstimate"]["warning"]
    assert "未扣手续费，结果偏乐观" in free["feeEstimate"]["text"]


@pytest.mark.parametrize("direction,slippage,net,drawdown,percent", [
    ("long", 0, -5, 5, 0.05), ("long", 1, -25, 25, 0.25),
    ("short", 0, -5, 5, 0.05), ("short", 1, -25, 25, 0.25)])
def test_end_of_data_close_counts_its_fees_and_slippage_in_the_drawdown(direction, slippage, net, drawdown, percent):
    """#505：回测结束强平的那一笔手续费与滑点要同时进权益曲线和最大回撤（与最终权益同口径），
    不能只改最后一个点。持到 end_of_data：0 跳时只有最后一笔费用，1 跳时两边滑点也在内。"""
    side = direction.capitalize()
    p = plan(direction=direction, stopLossTicks=100, maxOrdersPerDay=1,
             rules={f"entry{side}": {"conditions": ALWAYS}, f"exit{side}": {"conditions": NEVER}})
    result = run_backtest(BacktestSetup(plan=p, symbol="SHFE.rb2610", series={300: series([(100, 101, 99, 100)] * 10)},
                                        price_tick=1, volume_multiple=10, fee_schedule=FEES,
                                        initial_balance=10_000, slippage_ticks=slippage))
    summary = result["summary"]
    assert summary["trades"] == 1 and result["trades"][0]["exitReason"] == "end_of_data"
    assert summary["netProfit"] == net and result["equityCurve"][-1]["equity"] == 10_000 + net
    assert summary["maxDrawdown"] == drawdown and summary["maxDrawdownPercent"] == percent


def test_end_of_data_close_does_not_replace_a_larger_earlier_drawdown():
    """最终清仓造成的回撤不是最大回撤时，不覆盖更早的更大回撤（最后只扣那笔费用）。"""
    p = plan(exit_=NEVER, stopLossTicks=100, maxOrdersPerDay=1)
    rows = [(100, 101, 99, 100)] * 3 + [(100, 101, 89, 90), *[(90, 91, 89, 90)] * 3,
            (90, 101, 99, 100), (100, 101, 99, 100)]
    result = run(p, series(rows), fees=FEES)
    assert result["summary"]["netProfit"] == -5 and result["equityCurve"][-1]["equity"] == 99_995
    assert result["summary"]["maxDrawdown"] == 102 and result["summary"]["maxDrawdownPercent"] == 0.1


@pytest.mark.parametrize("slippage,net", [(0, 75), (1, 55)])
def test_end_of_data_replacement_takes_the_replaced_points_peak_back_out(slippage, net):
    """#520-B：最后一根K线的浮盈权益点是全曲线最高、又被结束强平替换掉时，被替换那一点
    立下的峰值要跟着退场——最大回撤按替换后的曲线算，不再多出“平仓费＋滑点”那一段。
    #520-A 后成交K线自己的收盘标记已含开仓费，曲线起点之后仍然只升不降。"""
    p = plan(exit_=NEVER, stopLossTicks=100, maxOrdersPerDay=1)
    # 持仓逐根新高，最后一根收盘的浮盈标记是曲线最高点；强平只差平仓费（和滑点）。
    result = run(p, series(gapless([100, 100, 102, 104, 106, 108])), fees=FEES,
                 slippage_ticks=slippage)
    summary = result["summary"]
    assert summary["trades"] == 1 and result["trades"][0]["exitReason"] == "end_of_data"
    assert summary["netProfit"] == net
    curve = result["equityCurve"]
    assert curve[-1]["equity"] == 100_000 + net
    assert max(point["equity"] for point in curve) == 100_000 + net   # 被替换的点不在曲线上
    assert summary["maxDrawdown"] == 0 and summary["maxDrawdownPercent"] == 0


def test_max_loss_counts_the_open_fee_and_the_floating_move():
    """#520-A：当日亏损上限与实盘 todayNetPnl 同口径——开仓费在成交时计入、持仓浮盈亏也计入：
    开仓费 30＋浮盈 10 已到 −10 上限时，顺势加仓被挡；无费用时同一根K线的加仓放行。"""
    bars = series([(100, 101, 99, 100), (100, 102, 99, 101), (100, 102, 99, 101), (100, 102, 99, 101)])
    p = plan(exit_=NEVER, maxOrdersPerDay=10, maxLoss=10, maxPosition=2,
             pyramiding={"trigger": "profit_ticks", "step": 1, "maxAdds": 1})
    heavy = {"source": "custom", "default": {"mode": "per_lot", "open": 30, "closeToday": 3, "closeYesterday": 3},
             "overrides": []}
    charged = run(p, bars, fees=heavy)["trades"][0]
    assert charged["volume"] == 1          # −30 开仓费＋10 浮盈 ＝ −20 ≤ −10：加仓被挡
    free = run(p, bars)["trades"][0]
    assert free["volume"] == 2 and free["openPrice"] == 100.0   # 无费用放行，按下一根开盘 100 加仓
    # 不设当日亏损上限（maxLoss=None，10-08 用户决定 A）：同样 −20 也不挡加仓。
    unlimited = plan(exit_=NEVER, maxOrdersPerDay=10, maxLoss=None, maxPosition=2,
                     pyramiding={"trigger": "profit_ticks", "step": 1, "maxAdds": 1})
    assert run(unlimited, bars, fees=heavy)["trades"][0]["volume"] == 2


def test_backtest_and_live_judge_every_bar_the_same():
    """同一段K线：回测每根的判定 == 实盘 evaluate_rules（带正在形成的K线）+ decide 的判定。"""
    p = StrategyPlan.model_validate({
        "name": "一致性", "description": "双均线加大周期过滤", "direction": "both", "timeframeSeconds": 300,
        "maxPosition": 3, "stopLossTicks": 30, "takeProfitTicks": 60, "cooldownBars": 0, "maxLoss": 1e8,
        "atrStop": {"multiple": 2.5}, "trailingStop": {"mode": "atr_chandelier", "value": 3, "activateAfter": 1},
        "maxHoldBars": 40, "sizing": {"mode": "atr_risk", "riskPercent": 1},
        "rules": {
            "entryLong": {"conditions": [cond("金叉", "ema", "cross_up", {"indicator": "ema", "period": 21}, period=8),
                                         cond("15分钟向上", "close", "gt", {"indicator": "ma", "period": 20}, tf=900)]},
            "exitLong": {"conditions": [cond("死叉", "ema", "cross_down", {"indicator": "ema", "period": 21}, period=8)]},
            "entryShort": {"conditions": [cond("死叉", "ema", "cross_down", {"indicator": "ema", "period": 21}, period=8),
                                          cond("15分钟向下", "close", "lt", {"indicator": "ma", "period": 20}, tf=900)]},
            "exitShort": {"conditions": [cond("金叉", "ema", "cross_up", {"indicator": "ema", "period": 21}, period=8)]},
        }})
    base = fake_market_history("SHFE.rb2610", 300, 900)["bars"]
    slow = fake_market_history("SHFE.rb2610", 900, 400)["bars"]
    setup = BacktestSetup(plan=p, symbol="SHFE.rb2610", series={300: base, 900: slow}, price_tick=0.5,
                          volume_multiple=10, fee_schedule=NO_FEES, initial_balance=1_000_000)
    seen = []
    result = run_backtest(setup, trace=lambda k, state, decision: seen.append((k, state, decision)))
    assert result["summary"]["trades"] >= 4 and len(seen) > 500
    judged = 0
    for k, state, decision in seen:
        if decision.fillPrice is not None:
            continue   # a protective hit inside the bar: live runs it tick by tick on the armed protection
        cutoff = base[k]["datetime"] + 300 * 1_000_000_000
        forming = {**base[k], "datetime": cutoff}
        slow_done = [b for b in slow if b["datetime"] + 900 * 1_000_000_000 <= cutoff][-LIVE_WINDOW:]
        workbenches = {300: {"bars": base[max(0, k + 1 - LIVE_WINDOW):k + 1] + [forming], "quote": {}},
                       900: {"bars": slow_done + [{**slow_done[-1], "datetime": slow_done[-1]["datetime"] + 900 * 10**9}], "quote": {}}}
        evaluation = evaluate_rules(p, workbenches)
        equity = 1_000_000 + sum(t["net"] for t in result["trades"] if t["closeTime"] <= base[k]["datetime"])
        live = decide(p, evaluation, state, MarketContext(price_tick=0.5, volume_multiple=10, equity=equity))
        assert (live.action, live.volume, live.exitReason, live.exitKey, live.stopPrice) == \
            (decision.action, decision.volume, decision.exitReason, decision.exitKey, decision.stopPrice), k
        judged += 1
    assert judged > 500


def test_coverage_reports_what_the_data_actually_covers():
    bars = series(gapless([100 + (i % 7) for i in range(40)]))
    result = run(DOUBLE_MA, bars, trading_days=30)
    coverage = result["coverage"]
    assert coverage["tradingDays"] == 1 and coverage["limited"] is True and coverage["requestedTradingDays"] == 30
    assert any("只覆盖 1 个交易日" in note for note in result["notes"])
    with pytest.raises(BacktestDataError):
        run(DOUBLE_MA, bars[:1])


def test_ten_thousand_one_minute_bars_with_daily_anchor_run_within_budget():
    p = StrategyPlan.model_validate({
        "name": "性能", "description": "1 分钟双均线 + 昨收过滤", "direction": "both", "timeframeSeconds": 60,
        "maxPosition": 2, "stopLossTicks": 40, "takeProfitTicks": None, "cooldownBars": 0, "maxLoss": 1e8,
        "atrStop": {"multiple": 2}, "trailingStop": {"mode": "atr_chandelier", "value": 3},
        "rules": {
            "entryLong": {"conditions": [cond("金叉", "ma", "cross_up", {"indicator": "ma", "period": 20}, tf=60, period=10),
                                         cond("昨收上方", "close", "gt", {"indicator": "prev_close"}, tf=60)]},
            "exitLong": {"conditions": [cond("死叉", "ma", "cross_down", {"indicator": "ma", "period": 20}, tf=60, period=10)]},
            "entryShort": {"conditions": [cond("死叉", "ma", "cross_down", {"indicator": "ma", "period": 20}, tf=60, period=10)]},
            "exitShort": {"conditions": [cond("金叉", "ma", "cross_up", {"indicator": "ma", "period": 20}, tf=60, period=10)]},
        }})
    setup = BacktestSetup(plan=p, symbol="SHFE.au2612", series={60: fake_market_history("SHFE.au2612", 60, 10_000)["bars"],
                                                               86400: fake_market_history("SHFE.au2612", 86400, 300)["bars"]},
                          price_tick=0.5, volume_multiple=1000, fee_schedule=NO_FEES)
    started = time.perf_counter()
    result = run_backtest(setup)
    elapsed = time.perf_counter() - started
    print(f"10k 1m bars + daily: {elapsed:.2f}s, {result['summary']['trades']} trades")
    assert result["coverage"]["bars"] == 10_000 - (LIVE_WINDOW - 1) and elapsed < 6   # target ≤ 3 s; CI machines vary


@pytest.mark.asyncio
async def test_routes_run_saved_and_unsaved_plans_and_hide_other_users_strategies(tmp_path):
    app = create_app(tmp_path / "bt.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="bt-token")
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer bt-token"}
    raw = plan(entry=[cond("金叉", "ma", "cross_up", {"indicator": "ma", "period": 20}, tf=300, period=5)],
               exit_=[cond("死叉", "ma", "cross_down", {"indicator": "ma", "period": 20}, tf=300, period=5)]).model_dump()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            saved = (await client.post("/api/strategy-plans", json={"plan": raw})).json()
            missing_symbol = await client.post(f"/api/strategies/{saved['strategyId']}/backtest", json={})
            assert missing_symbol.status_code == 422
            response = await client.post(f"/api/strategies/{saved['strategyId']}/backtest",
                                         json={"symbol": "SHFE.au2612", "slippageTicks": 1})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["kind"] == "rule" and body["strategyId"] == saved["strategyId"] and body["symbol"] == "SHFE.au2612"
            assert body["summary"]["trades"] > 0 and body["params"]["slippageTicks"] == 1
            assert len(body["equityCurve"]) <= 300 and body["coverage"]["tradingDays"] > 0
            unsaved = await client.post("/api/strategy-backtests", json={"plan": raw, "symbol": "SHFE.au2612",
                                                                         "slippageTicks": 1, "strategyId": saved["strategyId"]})
            assert unsaved.status_code == 200 and unsaved.json()["summary"]["trades"] == body["summary"]["trades"]
            recent = (await client.get("/api/strategy-backtests", params={"strategyId": saved["strategyId"]})).json()
            assert len(recent["items"]) == 2
            # With a sessionId the result card lands in that conversation (AI ▾ 回测这条策略).
            session_id = (await client.post("/api/sessions?chat=true", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
            posted = await client.post(f"/api/strategies/{saved['strategyId']}/backtest",
                                       json={"symbol": "SHFE.au2612", "sessionId": session_id})
            assert posted.status_code == 200
            names = [event["name"] for _, event in app.state.store.list_events(session_id)]
            assert names[-2:] == ["message.completed", "card.strategy_backtest"]
            assert (await client.post(f"/api/strategies/{saved['strategyId']}/backtest",
                                      json={"symbol": "SHFE.au2612", "sessionId": "nobody"})).status_code == 404
            assert app.state.store.connection.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0
            with app.state.store.connection:
                app.state.store.connection.execute("UPDATE strategy_drafts SET user_id='other-user' WHERE id=?",
                                                   (saved["strategyId"],))
            hidden = await client.post(f"/api/strategies/{saved['strategyId']}/backtest", json={"symbol": "SHFE.au2612"})
            assert hidden.status_code == 404


def test_backtest_card_command_validates_and_is_judged_by_the_reported_card():
    from pydantic import ValidationError

    from trader_runtime.ui_control import UiChanges, changes_match
    command = UiChanges.model_validate({"strategyBacktest": {"tradingDays": 20, "slippageTicks": 1}})
    assert command.strategyBacktest.run is None
    for bad in ({}, {"run": False}, {"slippageTicks": 9}, {"mark": None}, {"other": 1}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate({"strategyBacktest": bad})
    card = {"strategyId": "s", "running": False, "params": {"tradingDays": 20, "slippageTicks": 1},
            "summary": {"trades": 3}, "marked": True, "tradesOpen": False}
    assert changes_match({"strategyBacktest": card}, {"strategyBacktest": {"tradingDays": 20, "slippageTicks": 1}})
    assert changes_match({"strategyBacktest": card}, {"strategyBacktest": {"mark": True}})
    assert not changes_match({"strategyBacktest": card}, {"strategyBacktest": {"trades": True}})
    assert changes_match({"strategyBacktest": card}, {"strategyBacktest": {"run": True}})
    assert changes_match({}, {"strategyBacktest": {"mark": True}})   # an older client reports no card
    # 策略体检's 按这个改: applied once the card reports it applying or applied.
    assert UiChanges.model_validate({"strategyBacktest": {"applyAdvice": "drop-1"}}).strategyBacktest.applyAdvice == "drop-1"
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"strategyBacktest": {"applyAdvice": "drop 1; x"}})
    assert not changes_match({"strategyBacktest": card}, {"strategyBacktest": {"applyAdvice": "drop-1"}})
    assert changes_match({"strategyBacktest": {**card, "applying": True}}, {"strategyBacktest": {"applyAdvice": "drop-1"}})
    assert changes_match({"strategyBacktest": {**card, "appliedAdvice": "drop-1"}}, {"strategyBacktest": {"applyAdvice": "drop-1"}})


@pytest.mark.asyncio
async def test_ai_tool_backtests_saved_and_unsaved_plans_through_the_catalog(tmp_path):
    from test_ai_annotations import with_tools
    app = create_app(tmp_path / "tool.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="bt-token")
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer bt-token"}
    raw = plan(entry=[cond("金叉", "ma", "cross_up", {"indicator": "ma", "period": 20}, tf=300, period=5)],
               exit_=[cond("死叉", "ma", "cross_down", {"indicator": "ma", "period": 20}, tf=300, period=5)]).model_dump()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            saved = (await client.post("/api/strategy-plans", json={"plan": raw})).json()
            with_tools(app)
            catalog = (await client.get("/api/agent/capabilities")).json()["operations"]
            operations = {item["operation"]: item for item in catalog}
            for name in ("tool:run_strategy_backtest", "POST /api/strategies/{strategy_id}/backtest",
                         "POST /api/strategy-backtests"):
                assert operations[name]["readOnly"], name
            before = (await client.post("/api/agent/call", json={"operation": "tool:run_strategy_backtest",
                "body": {"strategyId": saved["strategyId"], "symbol": "SHFE.au2612"}})).json()
            changed = {**raw, "stopLossTicks": 5}
            after = (await client.post("/api/agent/call", json={"operation": "tool:run_strategy_backtest",
                "body": {"plan": changed, "symbol": "SHFE.au2612"}})).json()
            assert before["ok"] and after["ok"]
            first, second = before["data"]["data"], after["data"]["data"]
            assert first["summary"]["trades"] > 0 and first["coverage"]["tradingDays"] > 0 and len(first["lastTrades"]) <= 5
            assert first["summary"] != second["summary"]     # a tighter stop changes the outcome: before → after
            assert first["health"]["headline"].startswith("体检通过：") and first["health"]["advice"] == []   # 回测与体检：通过也说
            # 策略体检: a plan that never opens tells the AI where it is stuck and what to change.
            blocked = {**raw, "rules": {**raw["rules"], "entryLong": {**raw["rules"]["entryLong"],
                       "conditions": raw["rules"]["entryLong"]["conditions"] + NEVER}}}
            stuck = (await client.post("/api/agent/call", json={"operation": "tool:run_strategy_backtest",
                "body": {"plan": blocked, "symbol": "SHFE.au2612"}})).json()["data"]["data"]
            assert stuck["summary"]["trades"] == 0 and "卡在「从不」" in stuck["health"]["headline"]
            assert stuck["health"]["advice"][0].startswith("去掉「从不」：开多约 ")
            both = (await client.post("/api/agent/call", json={"operation": "tool:run_strategy_backtest",
                "body": {"strategyId": saved["strategyId"], "plan": changed}})).json()
            assert not both["ok"] and "二选一" in both["data"]["warning"]


def test_history_is_cached_per_contract_and_period_so_a_rerun_asks_the_sidecar_once():
    from trader_runtime.data_providers import HistoryCache, SidecarReadOnlyDataProvider
    now = [0.0]
    provider = SidecarReadOnlyDataProvider(base_url="http://127.0.0.1:1", token="x" * 32, symbols=("SHFE.au2612",))
    provider._history_cache = HistoryCache(ttl=300, clock=lambda: now[0])
    calls = []

    def query(path, *, unavailable_code, timeout):
        calls.append(path)
        return {"schemaVersion": 1, "bars": [{"datetime": i, "close": len(calls)} for i in range(10_000)],
                "priceTick": 0.02}
    provider._query_sidecar = query
    first = provider.market_history("SHFE.au2612", 300, 10_000)
    again = provider.market_history("shfe.AU2612", 300, 500)          # same pair, fewer bars: from the cache
    assert len(calls) == 1 and "length=10000" in calls[0]
    assert len(first["bars"]) == 10_000 and [bar["datetime"] for bar in again["bars"]] == list(range(9_500, 10_000))
    provider.market_history("SHFE.au2612", 60, 500)                    # another period: its own request
    assert len(calls) == 2
    now[0] = 301.0                                                     # expired after five minutes
    refreshed = provider.market_history("SHFE.au2612", 300, 500)
    assert len(calls) == 3
    assert first["bars"][-1]["close"] == 1 and refreshed["bars"][-1]["close"] == 3


def test_history_cache_deletes_old_entries_and_keeps_at_most_twelve():
    """内存调查 10-13：缓存上限 12；过期条目在 nowait（实时监控的当日最高/最低）的刷新宽限内仍在，
    超过 TTL + 宽限就删掉——访问时删，也由后台一次性定时清扫删，不等上限挤出。"""
    from trader_runtime.data_providers import HISTORY_CACHE_SIZE, HistoryCache
    assert HISTORY_CACHE_SIZE == 12
    now = [0.0]
    cache = HistoryCache(ttl=300, stale_grace=300, clock=lambda: now[0])
    entry = {"schemaVersion": 1, "bars": [{"datetime": i} for i in range(10)]}
    for index in range(15):
        now[0] = float(index)
        cache.put(f"SHFE.c{index}", 60, entry)
    assert len(cache) == 12 and cache.peek("SHFE.c0", 60, 5) == (None, False)  # oldest pushed out
    assert cache._timer is not None  # armed while anything is cached
    now[0] = 400.0                    # expired, inside the grace: still answered (stale) by peek
    stale, fresh = cache.peek("SHFE.c14", 60, 5)
    assert stale is not None and not fresh and cache.get("SHFE.c14", 60, 5) is None
    cache.put("SHFE.au2612", 300, entry)
    now[0] = 620.0                    # past ttl + grace for every old entry: the sweep deletes them
    cache._timer.cancel()
    cache._timer_fired()
    assert len(cache) == 1 and cache.peek("SHFE.au2612", 300, 5)[0] is not None
    assert cache._timer is not None   # re-armed for the survivor
    now[0] = 1001.0
    assert cache.peek("SHFE.au2612", 300, 5) == (None, False) and len(cache) == 0  # access deletes too
    cache.put("SHFE.au2612", 300, entry)
    timer = cache._timer
    cache.close()                     # the provider stopping: entries and the timer go at once
    assert len(cache) == 0 and cache._timer is None and timer is not None and timer.finished.is_set()


@pytest.mark.asyncio
async def test_a_broken_history_session_gives_a_clear_chinese_error(tmp_path, monkeypatch):
    app = create_app(tmp_path / "err.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="bt-token")
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer bt-token"}

    def broken(symbol, duration, length):
        raise RuntimeError("history_session_failed")
    async with app.router.lifespan_context(app):
        monkeypatch.setattr(app.state.data_provider, "market_history", broken)
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            response = await client.post("/api/strategy-backtests",
                                         json={"plan": DOUBLE_MA.model_dump(), "symbol": "SHFE.au2612"})
            assert response.status_code == 503
            assert response.json()["detail"] == "历史行情会话暂时出错，已自动重建，请稍后重试；实盘行情不受影响。"
            # The live chart keeps working.
            assert (await client.get("/api/markets/SHFE.au2612/workbench", params={"duration": 300})).status_code == 200


# -- 策略体检 (2026-09-26): which entry condition blocks the entries, and structural advice ----------------

def constant(value):
    return {"indicator": "constant", "value": value}


# 98 → 102 → 106, then three bars at 98, six times: the close crosses 100 on each 102 bar and 104 on
# each 106 bar — never on the same bar; within 3 bars of each other once per cycle.
STEPS = [98, 102, 106, 98, 98, 98] * 6
CROSS_100 = cond("收盘上穿100", "close", "cross_up", constant(100))
CROSS_104 = cond("收盘上穿104", "close", "cross_up", constant(104))
BELOW_50 = cond("收盘低于50", "close", "lt", constant(50))
ABOVE_100 = cond("收盘高于100", "close", "gt", constant(100))


def test_health_counts_occurrences_not_bars():
    # 收盘高于100 holds on a 3-bar run and then on one bar: two occurrences, not four bars.
    result = run(plan(entry=[ABOVE_100, BELOW_50]), series(gapless([98, 102, 103, 104, 98, 102, 98])))
    long = result["diagnosis"]["entryLong"]
    assert long["judgedBars"] == 6                              # judgedBars counts bars (the first one only warms up)
    assert long["conditions"][0] == {"label": "收盘高于100", "alone": 2, "without": 0, "cross": False}
    assert long["conditions"][1]["without"] == 2 and long["allPass"] == 0
    assert result["advice"][0]["text"] == "去掉「收盘低于50」：开多约 2 次"


def test_health_names_the_blocking_condition_and_advice_counts_entries_not_profit():
    p = plan(entry=[CROSS_100, BELOW_50])
    result = run(p, series(gapless(STEPS)))
    assert result["summary"]["trades"] == 0
    long = result["diagnosis"]["entryLong"]
    assert long["allPass"] == 0 and long["judgedBars"] == len(STEPS) - 1
    assert long["conditions"] == [{"label": "收盘上穿100", "alone": 6, "without": 0, "cross": True},
                                  {"label": "收盘低于50", "alone": 0, "without": 6, "cross": False}]
    assert long["relaxedCrossPass"] == 0                        # relaxing the cross cannot help here
    [drop] = result["advice"]
    assert drop == {"id": "drop-1", "kind": "drop", "groups": ["entryLong"], "index": 1, "label": "收盘低于50",
                    "passes": {"entryLong": 6}, "text": "去掉「收盘低于50」：开多约 6 次"}
    assert result["headline"].startswith(
        f"按现在的规则，近 {result['coverage']['tradingDays']} 个交易日开多条件同时满足 0 次，卡在「收盘低于50」。")
    assert "只按开仓次数给，不按盈亏挑参数" in result["headline"]
    json.dumps(result, allow_nan=False)                         # plain JSON: the worker process returns it as is
    fixed = apply_advice(p.model_dump(), drop)
    assert [c["label"] for c in fixed["rules"]["entryLong"]["conditions"]] == ["收盘上穿100"]
    rerun = run(StrategyPlan.model_validate(fixed), series(gapless(STEPS)))
    assert rerun["diagnosis"]["entryLong"]["allPass"] == 6 and rerun["summary"]["trades"] > 0
    assert rerun["advice"] == [] and rerun["headline"] is None
    single = plan(entry=[BELOW_50])                             # a drop never empties a group
    with pytest.raises(ValueError):
        apply_advice(single.model_dump(), {**drop, "index": 0})
    assert run(single, series(gapless(STEPS)))["advice"] == []


def test_health_relaxes_crosses_that_never_land_on_the_same_bar():
    p = plan(entry=[CROSS_100, CROSS_104])
    result = run(p, series(gapless(STEPS)))
    long = result["diagnosis"]["entryLong"]
    assert result["summary"]["trades"] == 0 and long["allPass"] == 0
    assert long["relaxedCrossPass"] == 6 and all(item["cross"] for item in long["conditions"])
    drop, relax = result["advice"]
    assert drop["kind"] == "drop" and drop["index"] == 0      # a tie keeps the first condition
    assert relax == {"id": "relax-cross", "kind": "relaxCross", "groups": ["entryLong"], "withinBars": 3,
                     "passes": {"entryLong": 6},
                     "text": "“上穿/下穿”放宽到最近 3 根K线内发生（不必同一根）：开多约 6 次"}
    relaxed = apply_advice(p.model_dump(), relax)
    assert [c["withinBars"] for c in relaxed["rules"]["entryLong"]["conditions"]] == [3, 3]
    rerun = run(StrategyPlan.model_validate(relaxed), series(gapless(STEPS)))
    assert rerun["diagnosis"]["entryLong"]["allPass"] == 6 and rerun["summary"]["trades"] > 0


def test_health_merges_the_same_drop_for_both_directions_and_skips_healthy_or_ordered_groups():
    both = plan(direction="both", rules={
        "entryLong": {"conditions": [CROSS_100, BELOW_50]},
        "entryShort": {"conditions": [cond("收盘下穿100", "close", "cross_down", constant(100)),
                                      cond("收盘高于500", "close", "gt", constant(500))]}})
    result = run(both, series(gapless(STEPS)))
    [drop] = result["advice"]
    assert drop["groups"] == ["entryLong", "entryShort"] and drop["id"] == "drop-1"
    assert drop["text"] == "去掉「收盘低于50」和「收盘高于500」：开多约 6 次、开空约 6 次"
    assert "开多条件同时满足 0 次、开空 0 次" in result["headline"]

    healthy = run(DOUBLE_MA, series(gapless(DOUBLE_MA_CLOSES)))
    assert healthy["summary"]["trades"] == 2 and healthy["advice"] == [] and healthy["headline"] is None
    assert healthy["diagnosis"]["entryLong"]["allPass"] == 2

    ordered = run(plan(rules={"entryLong": {"conditions": [CROSS_100, CROSS_104], "sequence": True}}),
                  series(gapless(STEPS)))
    assert ordered["diagnosis"] == {} and ordered["advice"] == [] and ordered["headline"] is None
    either = run(plan(rules={"entryLong": {"conditions": [CROSS_100, BELOW_50], "join": "any"}}), series(gapless(STEPS)))
    # 任一满足 (2026-10-08): counted for the 策略编辑卡's per-row numbers, never judged — no advice, no headline.
    assert set(either["diagnosis"]) == {"entryLong"} and either["diagnosis"]["entryLong"]["join"] == "any"
    assert either["diagnosis"]["entryLong"]["anyPass"] > 0 and either["advice"] == [] and either["headline"] is None
    aligned = run(plan(rules={"entryLong": {"alignmentConditions": [cond("站上100", "close", "gt", constant(100))],
                                            "conditions": [BELOW_50]}}), series(gapless(STEPS)))
    assert aligned["diagnosis"] == {}


@pytest.mark.asyncio
async def test_apply_advice_saves_a_new_version_and_never_empties_a_group(tmp_path):
    """按这个改 on 策略详情: POST /api/strategy-plans/{id}/apply-advice applies the backtest's suggestion
    to the saved plan and saves the next version of the same name; the original stays."""
    app = create_app(tmp_path / "advice.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="bt-token")
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer bt-token"}
    golden = cond("金叉", "ma", "cross_up", {"indicator": "ma", "period": 20}, tf=300, period=5)
    raw = plan(entry=[golden, *NEVER], exit_=[cond("死叉", "ma", "cross_down", {"indicator": "ma", "period": 20},
                                                  tf=300, period=5)]).model_dump()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            operations = {item["operation"]: item for item in (await client.get("/api/agent/capabilities")).json()["operations"]}
            assert not operations["POST /api/strategy-plans/{identifier}/apply-advice"]["readOnly"]
            saved = (await client.post("/api/strategy-plans", json={"plan": raw})).json()
            result = (await client.post(f"/api/strategies/{saved['strategyId']}/backtest",
                                        json={"symbol": "SHFE.au2612"})).json()
            assert result["summary"]["trades"] == 0 and "卡在「从不」" in result["headline"]
            [drop] = result["advice"]
            assert drop["index"] == 1 and drop["text"].startswith("去掉「从不」：开多约 ")

            applied = await client.post(f"/api/strategy-plans/{saved['strategyId']}/apply-advice", json={"advice": drop})
            assert applied.status_code == 201, applied.text
            version = applied.json()
            assert version["strategyId"] != saved["strategyId"] and version["name"] == saved["name"]
            assert version["version"] == saved["version"] + 1 and version["status"] == "draft"
            new_plan = version["definition"]["plan"]
            assert [c["label"] for c in new_plan["rules"]["entryLong"]["conditions"]] == ["金叉"]
            assert "按体检建议改：去掉「从不」" in new_plan["description"]
            listing = (await client.get("/api/strategy-plans")).json()["items"]
            assert {item["strategyId"] for item in listing} >= {saved["strategyId"], version["strategyId"]}
            rerun = (await client.post(f"/api/strategies/{version['strategyId']}/backtest", json={"symbol": "SHFE.au2612"})).json()
            assert rerun["summary"]["trades"] > 0 and rerun["headline"] is None

            relaxed = await client.post(f"/api/strategy-plans/{saved['strategyId']}/apply-advice", json={"advice": {
                "id": "relax-cross", "kind": "relaxCross", "groups": ["entryLong"], "withinBars": 3}})
            assert relaxed.status_code == 201
            assert relaxed.json()["definition"]["plan"]["rules"]["entryLong"]["conditions"][0]["withinBars"] == 3
            # A drop that would empty a group, or names no condition there, is refused; nothing is saved.
            emptied = await client.post(f"/api/strategy-plans/{version['strategyId']}/apply-advice", json={"advice": {
                "id": "drop-0", "kind": "drop", "groups": ["entryLong"], "index": 0}})
            assert emptied.status_code == 422 and "变空" in emptied.json()["detail"]
            assert (await client.post(f"/api/strategy-plans/{saved['strategyId']}/apply-advice", json={"advice": {
                "id": "drop-5", "kind": "drop", "groups": ["entryShort"], "index": 5}})).status_code == 422
            assert len((await client.get("/api/strategy-plans")).json()["items"]) == 3
            assert (await client.post("/api/strategy-plans/missing/apply-advice", json={"advice": drop})).status_code == 404
            assert app.state.store.connection.execute("SELECT COUNT(*) FROM strategy_monitor_states").fetchone()[0] == 0

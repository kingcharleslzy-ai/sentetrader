"""回答卡读数表 (结构化, 2026-10-17): the readings per (contract, field, timeframe) and the states derived from them
with the thresholds the tools' own labels use. How a card quotes them is tests/answer_corpus."""
from __future__ import annotations

from trader_runtime.answer_readings import Facts, analysis_labels, day_move_label, move_values
from trader_runtime.trader_tools import _market_analysis


def _analysis(symbol: str, duration: int, **fields: object) -> dict[str, object]:
    return {"symbol": symbol, "durationSeconds": duration, "barCount": 60, "lastClose": 800.0, "periodReturnPercent": 0.0,
            "rangePercent": 5.0, "sma5": 800.0, "sma20": 800.0, "volumeRatio": 1.0, "openInterestChange": 0, "trend": "range",
            **fields}


def test_the_tools_carry_the_same_labels_the_readings_use():
    assert day_move_label({"changePercent": -0.81}) == "当日下跌（对昨结算）"
    assert day_move_label({"changePercent": 0.04}) == "当日持平（对昨结算）" and day_move_label({}) is None
    assert analysis_labels(volume_ratio=1.09, open_interest_change=-10, period_return=-2.1, trend="range",
                           rsi_reading="超买区") == {
        "volume": "放量", "openInterest": "区间减仓", "period": "区间下跌（不是当日涨跌，当日看行情快照）",
        "trend": "震荡", "rsi": "超买"}
    bars = [{"datetime": index, "open": 100 + index, "high": 101 + index, "low": 99 + index, "close": 100 + index,
             "volume": 10.0 if index < 29 else 30.0, "openInterest": 1000 + index} for index in range(30)]
    summary = _market_analysis({"schemaVersion": 1, "liveTrading": "disabled", "dataStatus": "normal",
                                "quote": {"symbol": "SHFE.au2612"}, "durationSeconds": 300, "bars": bars}, lookback=60)
    assert summary["labels"]["volume"] == "放量" and summary["labels"]["openInterest"] == "区间增仓"
    assert summary["labels"]["trend"] == "上行"
    facts = Facts()
    facts.add(summary)
    assert facts.state("SHFE.au2612", "volumeState", 300) == {"up"} and facts.state("SHFE.au2612", "trend", 300) == {"up"}
    assert facts.detail("SHFE.au2612", "maOrder", 300) == "5 分钟线 MA5 高于 MA20"


def test_moves_flat_and_near_flat_keep_their_thresholds():
    assert move_values(-0.81) == {"down"}  # 10-14: never 持平
    assert move_values(-0.15) == {"down", "nearflat"} and move_values(0.30) == {"up", "nearflat"}  # 0.30% inclusive
    assert move_values(0.04) == {"up", "flat", "nearflat"} and move_values(0) == {"flat", "nearflat"}
    assert move_values(None) == set()


def test_each_reading_is_kept_per_contract_field_and_timeframe_the_latest_read_winning():
    facts = Facts()
    facts.add({"rows": [_analysis("SHFE.rb2701", 300, volumeRatio=1.4),
                        _analysis("SHFE.rb2701", 86400, volumeRatio=0.7, sma5=790.0,
                                  indicators={"rsi": {"value": 72.3, "reading": "超买区"},
                                              "boll": {"top": 830, "mid": 800, "bottom": 770, "reading": "价格在布林带内运行"}})]})
    assert facts.state("SHFE.rb2701", "volumeState", 300) == {"up"} and facts.state("SHFE.rb2701", "volumeState", 86400) == {"down"}
    assert facts.state("SHFE.rb2701", "maOrder", 86400) == {"down"} and facts.state("SHFE.rb2701", "maOrder", 300) == set()
    assert facts.state("SHFE.rb2701", "rsiZone", 86400) == {"overbought"} and facts.detail("SHFE.rb2701", "rsiZone", 86400) == "日线 RSI 72.3"
    assert facts.state("SHFE.rb2701", "bollPlace", 86400) == {"inside"} and facts.values[("SHFE.rb2701", "bollBottom", 86400)] == 770
    facts.add({"symbol": "SHFE.rb2701", "price": 3072, "changePercent": -0.81, "openInterestChange": 1200}, quote=True)
    facts.add({"symbol": "SHFE.rb2701", "last": 3070, "changePercent": -0.9})  # a board row read later
    assert facts.values[("SHFE.rb2701", "price", 0)] == 3070 and facts.detail("SHFE.rb2701", "dayMove", 0) == "对昨结算 -0.90%"
    assert facts.state("SHFE.rb2701", "oiDayState", 0) == {"up"}  # the earlier quote's, the board row had none


def test_a_pre_open_placeholder_quote_is_no_reading_of_the_day():
    facts = Facts()
    facts.add({"symbol": "DCE.j2701", "price": 2000.0, "changePercent": 0, "dataStatus": "stale",
               "open": None, "highest": None, "lowest": None, "volume": None}, quote=True)
    assert "DCE.j2701" in facts.pre_open and ("DCE.j2701", "day", 0) not in facts.values
    assert facts.values[("DCE.j2701", "price", 0)] == 2000.0

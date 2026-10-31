"""习惯对照 (habit_compare, adapted from Vibe-Trading's shadow attribution): the gap to "只做计划内" splits exactly
by the 计划外 trades' open tag; the holding estimates stand apart and never exceed a trade's own P&L."""
from __future__ import annotations

from trader_runtime.habit_compare import MIN_PLANNED, habit_compare


def trade(index: int, *, tag: str | None, pnl: float, minutes: int, symbol: str = "SHFE.rb2701") -> dict:
    return {"id": f"r{index}", "symbol": symbol, "direction": "long", "volume": 1, "tradingDay": f"2026-10-{index:02d}",
            "openTag": tag, "net": pnl, "gross": pnl + 10, "holdingMinutes": minutes}


def dataset() -> list[dict]:
    planned = [trade(1, tag="plan", pnl=600, minutes=40), trade(2, tag="plan", pnl=400, minutes=35),
               trade(3, tag="plan", pnl=-200, minutes=20), trade(4, tag="plan", pnl=-300, minutes=40),
               trade(5, tag="plan", pnl=500, minutes=60)]
    return planned + [trade(6, tag="adhoc", pnl=-1380, minutes=30), trade(7, tag="remind", pnl=-960, minutes=180),
                      trade(8, tag="plan", pnl=420, minutes=8), trade(9, tag="adhoc", pnl=200, minutes=50)]


def test_the_gap_splits_exactly_by_the_unplanned_trades_tag() -> None:
    result = habit_compare(dataset(), basis="net")
    assert result["status"] == "ok" and result["basis"] == "net"
    assert result["actual"] == {"count": 9, "pnl": -720.0}
    assert result["planned"] == {"count": 6, "pnl": 1420.0}
    assert result["gap"] == 2140.0
    assert result["split"] == [{"tag": "remind", "label": "提醒后", "count": 1, "pnl": -960.0},
                               {"tag": "adhoc", "label": "临时", "count": 2, "pnl": -1180.0}]
    assert -sum(item["pnl"] for item in result["split"]) == result["gap"]


def test_holding_estimates_are_bounded_and_named_by_the_planned_extremes() -> None:
    result = habit_compare(dataset(), basis="net")
    late, early = result["holding"]["lateLosers"], result["holding"]["earlyWinners"]
    # Longest 计划内 loser 40 min: the 180-minute loser's extra = 960 × 140/180; never more than the loss itself.
    assert late == {"count": 1, "extra": round(960 * 140 / 180, 2), "limitMinutes": 40} and late["extra"] < 960
    # Shortest 计划内 winner is the 8-minute one itself (it is 计划内): nothing closed earlier.
    assert early is None
    picks = result["picks"]
    assert [pick["id"] for pick in picks] == ["r6", "r7", "r9"]
    assert picks[0]["reason"] == "计划外亏损" and picks[0]["pnl"] == -1380.0
    assert picks[1]["reason"] == "计划外亏损"   # its |impact| 960 beats the 747 holding estimate
    assert picks[2]["reason"] == "计划外盈利"


def test_early_winners_and_untagged_rounds_and_the_minimum() -> None:
    rounds = [trade(1, tag="plan", pnl=500, minutes=35)] * 1 + [
        trade(index, tag="plan", pnl=300, minutes=35 + index) for index in range(2, 6)]
    rounds += [trade(6, tag="adhoc", pnl=420, minutes=7), trade(7, tag=None, pnl=-50, minutes=10)]
    result = habit_compare(rounds, basis="gross")
    assert result["holding"]["earlyWinners"] == {"count": 1, "missed": round(420 * 28 / 35, 2), "limitMinutes": 35}
    assert result["holding"]["lateLosers"] is None  # no 计划内 loser to measure against
    assert result["split"][-1] == {"tag": None, "label": "没有标签", "count": 1, "pnl": -50.0}
    assert -sum(item["pnl"] for item in result["split"]) == result["gap"]
    assert next(pick for pick in result["picks"] if pick["id"] == "r6")["reason"] == "计划外盈利"
    few = habit_compare(rounds[:MIN_PLANNED - 1], basis="net")
    assert few == {"status": "insufficient", "planned": MIN_PLANNED - 1, "needed": MIN_PLANNED, "basis": "net",
                   "rules": few["rules"]}

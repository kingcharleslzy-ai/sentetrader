"""引导复盘: which trades the review asks about, and the question for each."""
from __future__ import annotations

from datetime import datetime

from trader_runtime.review_picks import review_picks

NOW = int(datetime.fromisoformat("2026-09-29T16:00:00+08:00").timestamp() * 1e9)
NAMES = {"SHFE.rb2701": "螺纹", "SHFE.au2612": "沪金", "DCE.m2701": "豆粕"}


def trade(trade_id, symbol, *, day="2026-09-29", pnl, minutes=30, direction="long", stance=None, note=None, hours_ago=2):
    close = NOW - int(hours_ago * 3600e9)
    return {"id": trade_id, "symbol": symbol, "direction": direction, "gross": pnl, "net": None, "tradingDay": day,
            "openTime": close - int(minutes * 60e9), "closeTime": close, "holdingMinutes": minutes,
            "note": {"text": note, "via": "typed", "updatedAt": ""} if note else None,
            "deskView": {"stance": stance, "confidence": "中", "headline": None} if stance else None}


def test_the_biggest_loss_then_trades_against_the_desk_then_long_held_losers_at_most_three():
    history = [trade(f"w{i}", "SHFE.au2612", day="2026-09-26", pnl=500, minutes=20, hours_ago=80 + i) for i in range(3)]
    today = [
        trade("small-loss", "SHFE.rb2701", pnl=-300, minutes=90),                      # held 90 min vs winners' 20
        trade("big-loss", "SHFE.rb2701", pnl=-2300, minutes=40),
        trade("against", "DCE.m2701", pnl=800, direction="short", stance="偏多"),       # a winner, but against the desk
        trade("noted", "SHFE.rb2701", pnl=-5000, note="追高了"),                         # already has a 补充
        trade("fine", "SHFE.au2612", pnl=200, stance="偏多"),
    ]
    result = review_picks(history + today, scope="day", now_ns=NOW, names=NAMES)
    assert [(p["id"], p["reasons"]) for p in result["picks"]] == [
        ("big-loss", ["loss"]), ("against", ["against_desk"]), ("small-loss", ["held_loser"])]
    assert result["unnoted"] == 4 and result["total"] == 5
    big, against, held = result["picks"]
    assert big["question"] == "螺纹这笔做多，亏了 2,300 元，拿了 40 分钟。当时是怎么想的？"
    assert against["question"] == "豆粕这笔做空，赚了 800 元，拿了 30 分钟。那天调研看偏多，你反着做，当时是怎么考虑的？"
    assert held["question"] == "螺纹这笔做多，亏了 300 元，拿了 1 小时 30 分钟，比你平时拿盈利单久不少。当时为什么没走？"


def test_a_loss_against_the_desk_says_both_and_the_scope_picks_the_latest_trading_day_or_the_week():
    friday = [trade("fri", "SHFE.rb2701", day="2026-09-26", pnl=-900, direction="short", stance="偏多", hours_ago=70)]
    older = [trade("mon", "SHFE.rb2701", day="2026-09-22", pnl=-4000, hours_ago=24 * 6)]
    day = review_picks(friday + older, scope="day", now_ns=NOW, names=NAMES)
    # Nothing closed on the 29th: the latest trading day in the last three days (Friday) is "today".
    assert [(p["id"], p["reasons"]) for p in day["picks"]] == [("fri", ["loss", "against_desk"])]
    assert day["picks"][0]["question"] == "螺纹这笔做空，亏了 900 元，拿了 30 分钟。那天调研看偏多，当时是怎么想的？"
    week = review_picks(friday + older, scope="week", now_ns=NOW, names=NAMES)
    assert [p["id"] for p in week["picks"]] == ["mon", "fri"]


def test_nothing_to_ask():
    assert review_picks([], scope="day", now_ns=NOW) == {"scope": "day", "picks": [], "unnoted": 0, "total": 0}
    # Only winners in line with the desk: nothing is worth a question, but they still count as unnoted.
    calm = review_picks([trade("ok", "SHFE.rb2701", pnl=300, stance="偏多")], scope="day", now_ns=NOW)
    assert calm["picks"] == [] and calm["unnoted"] == 1
    # Too few winners to know the user's usual holding time: no held-too-long pick.
    lone = review_picks([trade("a", "SHFE.rb2701", pnl=-100, minutes=500), trade("b", "SHFE.rb2701", pnl=-50, minutes=600)],
                        scope="day", now_ns=NOW)
    assert [(p["id"], p["reasons"]) for p in lone["picks"]] == [("a", ["loss"])]

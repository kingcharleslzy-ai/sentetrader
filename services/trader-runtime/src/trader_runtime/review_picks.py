"""引导复盘 (2026-09-29): which closed trades are worth one question, chosen by rules, not by a model.

At most three per review, trades that already carry the user's 补充 skipped: the biggest loss; trades
made against what the research desk said about the product that day; losers held clearly longer than
the user's own winners. Each comes with the one question the voice review (or the chat AI) asks.
"""
from __future__ import annotations

import statistics
from typing import Any

from .instruments import foreign_display_name, money_unit, symbol_currency

MAX_PICKS = 3
# A loser held longer than this many times the median winner is "held too long" (needs 3 winners to judge).
HELD_FACTOR = 2.0
HELD_MIN_WINNERS = 3
HELD_MIN_MINUTES = 10
DAY_NS = 86_400 * 1_000_000_000


def _pnl(trade: dict[str, Any]) -> float:
    return float(trade["net"]) if trade.get("net") is not None else float(trade["gross"])


def _holding(minutes: int | None) -> str:
    if minutes is None:
        return ""
    if minutes < 60:
        return f"{minutes} 分钟"
    if minutes < 24 * 60:
        hours, rest = divmod(minutes, 60)
        return f"{hours} 小时" + (f" {rest} 分钟" if rest else "")
    return f"{round(minutes / (24 * 60))} 天"


def _against_desk(trade: dict[str, Any]) -> str | None:
    """The desk's stance when the trade went the other way ("偏多" on a short), else None."""
    stance = str((trade.get("deskView") or {}).get("stance") or "")
    if (stance == "偏多" and trade["direction"] == "short") or (stance == "偏空" and trade["direction"] == "long"):
        return stance
    return None


def in_scope(trades: list[dict[str, Any]], scope: str, now_ns: int) -> list[dict[str, Any]]:
    """`day`: the latest trading day with closes in the last three days (a Friday's trades still on the
    weekend); `week`: everything closed in the last seven days."""
    if scope == "week":
        return [t for t in trades if int(t["closeTime"]) >= now_ns - 7 * DAY_NS]
    recent = [t for t in trades if int(t["closeTime"]) >= now_ns - 3 * DAY_NS]
    if not recent:
        return []
    latest = max(str(t["tradingDay"]) for t in recent)
    return [t for t in recent if str(t["tradingDay"]) == latest]


def question(trade: dict[str, Any], name: str, reasons: list[str]) -> str:
    side = "做多" if trade["direction"] == "long" else "做空"
    value = _pnl(trade)
    unit = money_unit(symbol_currency(trade.get("symbol")))   # 外盘合约（美元子账户）的盈亏是美元
    result = f"亏了 {abs(round(value)):,} {unit}" if value < 0 else f"赚了 {round(value):,} {unit}"
    held = _holding(trade.get("holdingMinutes"))
    head = f"{name}这笔{side}，{result}" + (f"，拿了 {held}" if held else "")
    stance = _against_desk(trade)
    if reasons[0] == "against_desk":
        return f"{head}。那天调研看{stance}，你反着做，当时是怎么考虑的？"
    context = f"那天调研看{stance}，" if stance else ""
    if reasons[0] == "held_loser":
        return f"{head}，比你平时拿盈利单久不少。{context}当时为什么没走？"
    return f"{head}。{context}当时是怎么想的？"


def review_picks(trades: list[dict[str, Any]], *, scope: str, now_ns: int,
                 names: dict[str, str] | None = None) -> dict[str, Any]:
    """The picks for a review: `trades` are the account's round trips (the trade review's, with
    holdingMinutes / note / deskView). Returns the picks (id, symbol, name, reasons, question) plus how
    many trades in scope have no 补充 yet."""
    names = names or {}
    scoped = in_scope(trades, scope, now_ns)
    open_ones = [t for t in scoped if not t.get("note")]
    winners = [int(t["holdingMinutes"]) for t in trades
               if _pnl(t) > 0 and t.get("holdingMinutes") is not None and int(t["closeTime"]) >= now_ns - 30 * DAY_NS]
    typical = statistics.median(winners) if len(winners) >= HELD_MIN_WINNERS else None
    reasons: dict[str, list[str]] = {}
    losers = sorted((t for t in open_ones if _pnl(t) < 0), key=_pnl)
    if losers:
        reasons.setdefault(losers[0]["id"], []).append("loss")
    for trade in sorted(open_ones, key=_pnl):
        if _against_desk(trade):
            reasons.setdefault(trade["id"], []).append("against_desk")
    if typical is not None:
        for trade in losers:
            minutes = trade.get("holdingMinutes")
            if minutes is not None and minutes >= HELD_MIN_MINUTES and minutes > HELD_FACTOR * typical:
                reasons.setdefault(trade["id"], []).append("held_loser")
    by_id = {t["id"]: t for t in open_ones}
    picks = []
    for trade_id, why in list(reasons.items())[:MAX_PICKS]:
        trade = by_id[trade_id]
        # spoken: "rb2701", not "SHFE."; a foreign month out of the catalog by its Chinese name (COMEX黄金2612)
        name = names.get(trade["symbol"]) or foreign_display_name(trade["symbol"]) or str(trade["symbol"]).split(".")[-1]
        picks.append({"id": trade_id, "symbol": trade["symbol"], "name": name, "reasons": why,
                      "question": question(trade, name, why)})
    return {"scope": scope, "picks": picks, "unnoted": len(open_ones), "total": len(scoped)}

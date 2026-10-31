"""习惯对照 (2026-10-25, owner-approved mock-up): the account's closed round trips against "只做计划内" — the
same trades with only those the user opened 计划内 (review_tags: a strategy, a stop, the AI's own order, or an
AI card confirmed with no reminder before it). Pure: the rounds come from account_trade_review, already tagged.

The gap between the two is exactly the 计划外 trades, split by their open tag (提醒后 / 临时). Beside it, two
estimates that are not added to the gap: losers held longer than the longest 计划内 loser, and winners closed
sooner than the shortest 计划内 winner.

Adapted from HKUDS/Vibe-Trading (MIT, commit 41f333b6) `agent/src/shadow_account/backtester.py`
(`_compute_attribution`): its shadow is a rule set mined from the profitable trades and backtested; here the
shadow is the user's own 计划内 trades, so the gap needs no model and splits exactly. Its early-exit estimate is
kept (P&L × the share of the shortest 计划内 winner's holding time not held); its late-exit estimate, which grew
without bound (P&L × excess ÷ limit), is bounded here to the share of this trade's own time held past the limit.
"""
from __future__ import annotations

from typing import Any

from .review_picks import _holding as _hold_text  # the voice review's wording (1 小时 20 分钟)

MIN_PLANNED = 5
MAX_PICKS = 5
TAG_LABELS = {"plan": "计划内", "remind": "提醒后", "adhoc": "临时"}
RULES = (
    "只做计划内：同一段时间里只留开仓标为计划内的单（策略、止损止盈与条件单、AI 自主执行，或确认 AI 的卡且下单前 15 分钟内没有同品种提醒）",
    "相差的钱就是计划外的单（提醒后、临时）的盈亏之和，按开仓标签拆开",
    "拿单习惯是估算，不和上面相加：亏单拿得比计划内亏单最长的还久，按超出的那段时间占这笔持仓的比例估多亏的钱；"
    "赚单比计划内赚单最短的还早平，按差的那段时间占那个最短时间的比例估少赚的钱",
    "计划内的单不到 5 笔时不对照；盈亏按复盘同一口径（有逐笔手续费时扣费后，否则毛收益）",
)


def _pnl(trade: dict[str, Any]) -> float:
    value = trade.get("net")
    return float(value if value is not None else trade.get("gross") or 0.0)


def _minutes(trade: dict[str, Any]) -> int:
    return int(trade.get("holdingMinutes") or 0)


def habit_compare(trades: list[dict[str, Any]], *, basis: str) -> dict[str, Any]:
    """`trades`: the window's closed round trips (`openTag` from annotate_round_tags, `holdingMinutes`, net/gross).
    `basis` is "net" or "gross", as the review's fee mask decided."""
    planned = [trade for trade in trades if trade.get("openTag") == "plan"]
    if len(planned) < MIN_PLANNED:
        return {"status": "insufficient", "planned": len(planned), "needed": MIN_PLANNED, "basis": basis, "rules": list(RULES)}
    actual = sum(map(_pnl, trades))
    shadow = sum(map(_pnl, planned))
    split: list[dict[str, Any]] = []
    for tag in ("remind", "adhoc"):
        group = [trade for trade in trades if trade.get("openTag") == tag]
        split.append({"tag": tag, "label": TAG_LABELS[tag], "count": len(group), "pnl": round(sum(map(_pnl, group)), 2)})
    # A round with no open tag (an older fill nothing could tag) is still in 实际, so the split says so.
    untagged = [trade for trade in trades if trade.get("openTag") not in TAG_LABELS]
    if untagged:
        split.append({"tag": None, "label": "没有标签", "count": len(untagged), "pnl": round(sum(map(_pnl, untagged)), 2)})

    planned_losers = [_minutes(trade) for trade in planned if _pnl(trade) < 0]
    planned_winners = [_minutes(trade) for trade in planned if _pnl(trade) > 0]
    longest_loss = max(planned_losers) if planned_losers else None
    shortest_win = min(planned_winners) if planned_winners else None
    impacts: dict[str, tuple[float, str]] = {}

    def note(trade: dict[str, Any], impact: float, reason: str) -> None:
        if abs(impact) > abs(impacts.get(trade["id"], (0.0, ""))[0]):
            impacts[trade["id"]] = (impact, reason)

    late = early = 0.0
    late_count = early_count = 0
    for trade in trades:
        pnl, held = _pnl(trade), _minutes(trade)
        if trade.get("openTag") in ("remind", "adhoc"):
            note(trade, -pnl, "计划外亏损" if pnl < 0 else "计划外盈利")
        if longest_loss is not None and pnl < 0 and held > longest_loss and held > 0:
            extra = -pnl * (held - longest_loss) / held
            late += extra
            late_count += 1
            note(trade, extra, f"拿了 {_hold_text(held)}，计划内亏单最长 {_hold_text(longest_loss)}")
        if shortest_win and pnl > 0 and held < shortest_win:
            missed = pnl * (shortest_win - held) / shortest_win
            early += missed
            early_count += 1
            note(trade, missed, f"{_hold_text(held)}就平，计划内赚单最短 {_hold_text(shortest_win)}")
    by_id = {trade["id"]: trade for trade in trades}
    ranked = sorted(impacts.items(), key=lambda item: (-abs(item[1][0]), item[0]))[:MAX_PICKS]
    picks = [{"id": key, "tradingDay": by_id[key].get("tradingDay"), "symbol": by_id[key]["symbol"],
              "direction": by_id[key]["direction"], "volume": int(by_id[key]["volume"]),
              "tag": by_id[key].get("openTag"), "pnl": round(_pnl(by_id[key]), 2), "reason": reason}
             for key, (_, reason) in ranked]
    return {
        "status": "ok", "basis": basis, "rules": list(RULES),
        "actual": {"count": len(trades), "pnl": round(actual, 2)},
        "planned": {"count": len(planned), "pnl": round(shadow, 2)},
        "gap": round(shadow - actual, 2), "split": split,
        "holding": {
            "lateLosers": {"count": late_count, "extra": round(late, 2), "limitMinutes": longest_loss} if late_count else None,
            "earlyWinners": {"count": early_count, "missed": round(early, 2), "limitMinutes": shortest_win} if early_count else None,
        },
        "picks": picks,
    }

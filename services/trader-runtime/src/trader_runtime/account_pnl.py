"""Account-wide profit and loss for the review page: realized round trips grouped
by closing trading day and month, beside the account's equity history.

Realized figures come from the trade review (fee schedule or account fees);
the total is equity against the configured starting balance, so it also covers
open positions and days the store never saw.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .store.trades import carries_day_pnl

DEFAULT_INITIAL_BALANCE = 1_000_000.0
CHINA = ZoneInfo("Asia/Shanghai")


def ordered_review_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The review page's order, shared by full history and the limited journal."""
    return sorted(trades, key=lambda trade: (-trade["closeTime"], trade["id"]))


def _realized(trade: dict[str, Any]) -> float:
    return float(trade["net"]) if trade.get("net") is not None else float(trade["gross"])


def _bucket(trades: list[dict[str, Any]], key: str, label: str) -> dict[str, Any]:
    realized = [_realized(t) for t in trades]
    fees = [t.get("fees") for t in trades]
    wins = sum(1 for value in realized if value > 0)
    return {label: key, "realizedNet": round(sum(realized), 2),
            "commission": round(sum(float(f) for f in fees if f is not None), 2) if any(f is not None for f in fees) else None,
            "tradeCount": len(trades), "winRate": round(wins / len(trades), 4) if trades else None}


def account_pnl(review: dict[str, Any], *, daily: list[dict[str, Any]], initial_balance: float | None,
                months: int, now: datetime | None = None) -> dict[str, Any]:
    today = (now or datetime.now(CHINA)).astimezone(CHINA)
    account = review.get("account") or {}
    data_status = str(account.get("dataStatus") or "unavailable")
    initial = float(initial_balance) if initial_balance else DEFAULT_INITIAL_BALANCE
    latest_daily = daily[-1] if daily else None
    equity = (float(account["equity"]) if data_status in {"normal", "stale", "simulated"}
              else float(latest_daily["equity"]) if latest_daily and latest_daily.get("equity") is not None else None)
    trading_day = account.get("tradingDay") if data_status in {"normal", "stale", "simulated"} else None
    today_net = float(account["todayNetPnl"]) if trading_day else (
        float(latest_daily["todayNetPnl"]) if latest_daily and latest_daily.get("todayNetPnl") is not None else None)
    if trading_day and not carries_day_pnl(account):
        # After the 15:15 settlement the realtime summary clears the day's P&L; the daily row
        # for the same trading day still holds the result, a booked 0 included.
        settled = next((row for row in daily if row["tradingDay"] == trading_day
                        and row.get("todayNetPnl") is not None and carries_day_pnl(row)), None)
        if settled is not None:
            today_net = float(settled["todayNetPnl"])
    trades = ordered_review_trades(review.get("trades") or [])
    by_day: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        by_day.setdefault(str(trade["tradingDay"]), []).append(trade)
    equity_by_day = {row["tradingDay"]: row.get("equity") for row in daily}
    # Per-day net P&L fallback when the settled row was lost: the equity move against the
    # previous stored trading day (`daily` is ascending).
    prior_equity: dict[str, float | None] = {}
    previous: float | None = None
    for row in daily:
        prior_equity[row["tradingDay"]] = previous
        if row.get("equity") is not None:
            previous = float(row["equity"])

    def equity_delta(day: str) -> float | None:
        value, prior = equity_by_day.get(day), prior_equity.get(day)
        return round(value - prior, 2) if value is not None and prior is not None else None

    days = [{**_bucket(items, day, "day"), "equity": equity_by_day.get(day), "equityDelta": equity_delta(day)}
            for day, items in sorted(by_day.items(), reverse=True)]
    def day_pnl(row: dict[str, Any]) -> tuple[float | None, bool]:
        # The booked figure; a missing one, or a row the old overwrite flattened to all zeros,
        # falls back to the equity move — estimated, because a gap in the stored days or a
        # deposit/withdrawal would ride along in that move (#514-9).
        if row.get("todayNetPnl") is not None and carries_day_pnl(row):
            return float(row["todayNetPnl"]), False
        delta = equity_delta(row["tradingDay"])
        return (delta, True) if delta is not None else (row.get("todayNetPnl"), False)

    # 权益曲线 (2026-10-03): every stored trading day, closes or not, oldest first. Points whose
    # pnl came from the equity move carry pnlEstimated so a reader can tell them from booked days.
    curve: list[dict[str, Any]] = []
    for row in [item for item in daily if item.get("equity") is not None][-366:]:
        pnl, estimated = day_pnl(row)
        point: dict[str, Any] = {"day": row["tradingDay"], "equity": float(row["equity"]), "pnl": pnl}
        if estimated:
            point["pnlEstimated"] = True
        curve.append(point)
    by_month: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        by_month.setdefault(str(trade["tradingDay"])[:7], []).append(trade)
    month_keys = sorted(by_month, reverse=True)[:months]
    month_rows = [_bucket(by_month[month], month, "month") for month in month_keys]
    this_month = today.strftime("%Y-%m")
    total_count = len(trades)
    wins = sum(1 for t in trades if _realized(t) > 0)
    commissions = [(row["tradingDay"], float(row["commission"])) for row in daily if row.get("commission") is not None]
    return {
        "initialBalance": initial, "equity": equity, "dataStatus": data_status, "tradingDay": trading_day,
        "accountName": account.get("accountName"),
        # Account-reported fees, one figure per trading day the store has seen.
        "cumulativeCommission": round(sum(value for _, value in commissions), 2) if commissions else None,
        "commissionSince": min(day for day, _ in commissions) if commissions else None,
        "totalPnl": round(equity - initial, 2) if equity is not None else None,
        "today": today_net, "thisMonth": round(sum(_realized(t) for t in by_month.get(this_month, [])), 2),
        "thisMonthTradeCount": len(by_month.get(this_month, [])),
        "tradeCount": total_count, "winRate": round(wins / total_count, 4) if total_count else None,
        "realizedNet": round(sum(_realized(t) for t in trades), 2),
        "costSource": review.get("costSource"), "skippedSymbols": review.get("skippedSymbols") or [],
        "months": month_rows, "days": days, "trades": trades, "equityCurve": curve,
        # 未配平的平仓手数 (#514-11): the review's unmatched closes ride along so a reader can
        # tell an incomplete day from a fully paired one.
        "unmatched": review.get("unmatched") or [],
        # 未关联的补充 (#514): the review's read-only list, shown at the end of the 复盘 page.
        "unlinkedNotes": review.get("unlinkedNotes") or [],
    }

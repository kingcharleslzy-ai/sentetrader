"""Fills synthesized from finished simulation order links.

The sidecar only reports the current trading day's fills, so an order that was
filled before `account_trades` existed (or while the runtime was down) has no
fill row. Its order link still records the confirmed request and the FINISHED
report, which is enough to pair the round trip for review: price is the limit
price, the time is the order's own (`fill_time`), volume is what the exchange
actually filled; the row is marked approximate. Review and P&L display only;
order checks never read these rows.

The fill's trading day follows the Runtime's one rule (`trading_calendar`, 18:00 turn, holidays from
the exchange calendar via `exchange_calendar`). With the calendar unreadable the weekend rule
stands in — a backfill on a holiday may then land on the holiday's date; the rows are re-derived
on every pass, so the next pass with a calendar corrects them.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .exchange_calendar import calendar_trading_days
from .trading_calendar import trading_day_at
from .trading_costs import contract_multiplier


def fill_time(link: dict[str, Any]) -> datetime | None:
    """When a backfilled fill happened, as near as the order link knows (#503): the broker's insert
    time in the report, else the moment the order was placed — never when its report was read back,
    which a recovery reads newest order first. No order of this kind reports its fill time, so the
    time is an estimate like the price (the row's `approximate`)."""
    order = (link.get("report") or {}).get("order") or {}
    try:
        inserted = int(order.get("insertDateTime") or 0)
    except (TypeError, ValueError):
        inserted = 0
    if inserted > 0:
        return datetime.fromtimestamp(inserted / 1e9, UTC)
    placed = link.get("createdAt")
    return datetime.fromisoformat(str(placed).replace("Z", "+00:00")) if placed else None


def synthesize_fills(links: list[dict[str, Any]], multiplier_for: Callable[[str], float | None],
                     trading_days: list[str] | None = None) -> list[dict[str, Any]]:
    fills = []
    for link in links:
        request, report = link["request"], link["report"]
        order = report.get("order") if isinstance(report, dict) else None
        if link.get("submitPhase") != "CONFIRMED" or not isinstance(order, dict) or order.get("status") != "FINISHED":
            continue
        try:
            volume = int(order["volume"]) - int(order["volumeLeft"])
        except (KeyError, TypeError, ValueError):
            continue
        when = fill_time(link)
        if volume <= 0 or when is None:
            continue
        symbol = str(request["symbol"])
        fills.append({
            "clientOrderId": str(request["clientOrderId"]), "symbol": symbol,
            "direction": str(request["direction"]), "offset": str(request["offset"]),
            "price": float(request["limitPrice"]), "volume": volume,
            "tradeDateTime": int(when.timestamp() * 1_000_000) * 1000,
            "tradingDay": trading_day_at(when, trading_days),
            "volumeMultiple": multiplier_for(symbol),
        })
    return fills


def finished_links_without_real_fills(connection: sqlite3.Connection, trading_account_id: str) -> list[dict[str, Any]]:
    """Finished order links with no real fill: the ones still to backfill and the ones already
    backfilled (re-derived each pass, so a row written by an older rule is corrected)."""
    rows = connection.execute(
        """SELECT l.client_order_id, l.request_json, l.last_report_json, l.created_at, l.submit_phase
           FROM tqkq_sim_order_links l
           WHERE l.submit_phase = 'CONFIRMED' AND l.last_remote_status = 'FINISHED'
             AND l.trading_account_id = ?
             AND NOT EXISTS (SELECT 1 FROM account_trades t
                             WHERE t.client_order_id = l.client_order_id
                               AND t.trading_account_id = l.trading_account_id AND t.source != 'order_backfill')""",
        (trading_account_id,),
    ).fetchall()
    return [{"clientOrderId": row[0], "request": json.loads(row[1]), "report": json.loads(row[2]) if row[2] else None,
             "createdAt": row[3], "submitPhase": row[4]} for row in rows]


def stored_multiplier(store, provider, symbol: str, trading_account_id: str | None = None) -> float | None:
    # Expired contracts leave the provider; a fill the store already holds keeps its multiple.
    for trade in store.list_account_trades(symbol=symbol, trading_account_id=trading_account_id):
        if trade.get("volumeMultiple"):
            return float(trade["volumeMultiple"])
    return contract_multiplier(provider, symbol)


def backfill_order_fills(store, provider, trading_account_id: str | None = None) -> int:
    """Write synthesized fills for every finished, filled order link that has no
    real fill; repeated runs write nothing unless the link says something new. A
    later real fill for the same client order deletes the synthesized row
    (upsert_account_trades). Errors never reach the caller: the review is display only."""
    account_id = trading_account_id or store.default_trading_account_id
    try:
        with store.lock:
            links = finished_links_without_real_fills(store.connection, account_id)
        multipliers: dict[str, float | None] = {}

        def multiplier_for(symbol: str) -> float | None:
            if symbol not in multipliers:
                multipliers[symbol] = stored_multiplier(store, provider, symbol, trading_account_id=account_id)
            return multipliers[symbol]
        if not links:
            return 0  # 没有要补录的单：不读交易所日历，白占一次共享行情源
        fills = synthesize_fills(links, multiplier_for, calendar_trading_days(provider))
        return store.backfill_account_trades(fills, trading_account_id=account_id) if fills else 0
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        return 0

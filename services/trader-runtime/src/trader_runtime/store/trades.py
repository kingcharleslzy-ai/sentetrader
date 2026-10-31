from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..trading_costs import review_trades
from .base import StoreBase

_PNL_FIELDS = ("closeProfit", "positionProfit", "commission", "todayNetPnl")


def carries_day_pnl(snapshot: dict[str, Any]) -> bool:
    """Whether an account snapshot (or a stored daily row) holds the day's P&L. TqKq's 15:15
    settlement clears all four fields at once; a normal reading keeps at least one non-zero (a
    net of exactly 0 still has the commission or the position profit behind it). The one test
    both the daily ledger and the review headline/curve use."""
    for key in _PNL_FIELDS:
        try:
            if snapshot.get(key) is not None and float(snapshot[key]) != 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


# 迁移证明只用配对结构，不用任何费率：零费率表让 review_trades 只回答“这些成交
# 组成了哪些回合、每回合几手”，费用一概为 0，两个口径都算不出差别。
_PROOF_FEES = {"source": "custom", "default": {"mode": "per_lot", "open": 0.0, "closeToday": 0.0,
                                               "closeYesterday": 0.0}, "overrides": []}


def _fill_row(row: Any) -> dict[str, Any]:
    """A stored account_trades row back in the shape the review pairing reads."""
    return {"tradeId": row["trade_id"], "clientOrderId": row["client_order_id"], "symbol": row["symbol"],
            "direction": row["direction"], "offset": row["offset"], "price": row["price"],
            "volume": row["volume"], "tradeDateTime": row["trade_date_time"], "tradingDay": row["trading_day"],
            "volumeMultiple": row["volume_multiple"], "source": row["source"],
            "approximate": bool(row["approximate"])}


def _compose_round_id(round_id: str, mapping: dict[str, str]) -> str:
    """`openTradeId:closeTradeId` with every side the batch replaced rewritten to its real fill."""
    for old, new in mapping.items():
        if round_id.startswith(old + ":"):
            round_id = new + round_id[len(old):]
        elif round_id.endswith(":" + old):
            round_id = round_id[:-len(old)] + new
    return round_id


def relink_replaced_backfill_notes(connection: Any, trading_account_id: str,
                                   replaced: dict[str, dict[str, Any]]) -> int:
    """Move the 补充 keyed on a backfilled fill (`link:<order>`) onto its real fill, but only where the
    replacement provably kept the same trade (#502/#514): the order's whole backfilled volume came back
    as one real fill, and re-pairing the symbol's fills (FIFO, 零费率) brings every round trip the
    backfill took part in back under the real id with the same other side and the same volume. Split
    fills, re-ordered FIFO or a changed counterpart leave the 补充 on its original key — the review
    lists it as unlinkedNotes instead of guessing. Runs inside the caller's transaction over one
    upsert's worth of replaced orders. Returns how many 补充 moved."""
    by_symbol: dict[str, dict[str, str]] = {}   # symbol → link:<order> → real fill id（1 笔、同手数的候选）
    for client_order_id, backfill in replaced.items():
        real = connection.execute(
            "SELECT trade_id, volume FROM account_trades WHERE trading_account_id = ? AND client_order_id = ?"
            " AND source = 'activity'", (trading_account_id, client_order_id)).fetchall()
        if len(real) == 1 and int(real[0]["volume"]) == int(backfill["volume"]):
            by_symbol.setdefault(str(backfill["symbol"]), {})[f"link:{client_order_id}"] = str(real[0]["trade_id"])
    moved = 0
    for symbol, mapping in sorted(by_symbol.items()):
        after_fills = [_fill_row(row) for row in connection.execute(
            "SELECT " + ", ".join(_COLUMNS) + " FROM account_trades WHERE trading_account_id = ? AND symbol = ?",
            (trading_account_id, symbol)).fetchall()]
        # 把换掉的成交放回去：替换前该品种的成交（补录行 + 其余真实成交），重算替换前的回合。
        before_fills = [backfill for backfill in replaced.values() if backfill["symbol"] == symbol] + [
            fill for fill in after_fills
            if fill["source"] != "activity" or fill["clientOrderId"] not in replaced]
        multiplier = next((float(fill["volumeMultiple"]) for fill in [*before_fills, *after_fills]
                           if fill.get("volumeMultiple")), 1.0)
        try:
            before_rounds = {item["id"]: item["volume"] for item in
                             review_trades(before_fills, _PROOF_FEES, multiplier)["trades"]}
            after_rounds = {item["id"]: item["volume"] for item in
                            review_trades(after_fills, _PROOF_FEES, multiplier)["trades"]}
        except (KeyError, TypeError, ValueError):
            continue   # 某笔成交配对器读不了（缺交易日等）：证明不了，一条都不动
        proven = {old: new for old, new in mapping.items()
                  if all(after_rounds.get(_compose_round_id(round_id, mapping)) == volume
                         for round_id, volume in before_rounds.items()
                         if round_id.startswith(old + ":") or round_id.endswith(":" + old))}
        if not proven:
            continue
        # 打法本 (2026-10-25): a round trip's 补标的打法 moves with its 补充, by the same proof.
        for table in ("trade_notes", "round_playbooks"):
            for (round_id,) in connection.execute(
                    f"SELECT round_id FROM {table} WHERE trading_account_id = ?", (trading_account_id,)).fetchall():
                key = str(round_id)
                # 只动确实落在替换前某个回合上的补充；目标回合里已有补充的不覆盖（UPDATE OR IGNORE）。
                if key not in before_rounds or not any(
                        key.startswith(old + ":") or key.endswith(":" + old) for old in proven):
                    continue
                changed = connection.execute(
                    f"UPDATE OR IGNORE {table} SET round_id = ? WHERE trading_account_id = ? AND round_id = ?",
                    (_compose_round_id(key, mapping), trading_account_id, key)).rowcount
                moved += changed if table == "trade_notes" else 0
    return moved


_COLUMNS = ("trade_id", "client_order_id", "exchange_trade_id", "symbol", "direction", "offset",
            "price", "volume", "trade_date_time", "trading_day", "volume_multiple", "captured_at",
            "source", "approximate")


class TradeStore(StoreBase):
    """Account fills as the provider reported them. The sidecar only serves the
    current trading day, so every read of its activity feed is persisted here and
    the trade review pairs across days from this table."""

    def users_with_trades_between(self, start_ns: int, end_ns: int) -> list[str]:
        """窗口 [start, end) 内有成交的账户的执行主人（3b 整理的活动用户之一）。"""
        rows = self.connection.execute(
            """SELECT DISTINCT a.execution_owner_user_id FROM trading_accounts a
            JOIN account_trades t ON t.trading_account_id = a.id
            WHERE t.trade_date_time >= ? AND t.trade_date_time < ?""",
            (start_ns, end_ns),
        ).fetchall()
        return [row["execution_owner_user_id"] for row in rows]

    def upsert_account_trades(
        self, trades: list[dict[str, Any]], multipliers: dict[str, float] | None = None,
        trading_account_id: str | None = None,
    ) -> int:
        """`multipliers` (symbol → volumeMultiple) is stored with the fill so a
        contract that later expires can still be paired and valued."""
        account_id = trading_account_id or self.default_trading_account_id
        rows = []
        captured_at = datetime.now(UTC).isoformat()
        for trade in trades:
            identifier = str(trade.get("tradeId") or "")
            symbol = str(trade.get("symbol") or "")
            if not identifier or not symbol or trade.get("offset") not in {"OPEN", "CLOSE", "CLOSETODAY"}:
                continue
            try:
                volume, when = int(trade["volume"]), int(trade["tradeDateTime"])
                price = None if trade.get("price") is None else float(trade["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if volume <= 0 or when <= 0:
                continue
            trading_day = trade.get("tradingDay")
            multiplier = (multipliers or {}).get(symbol)
            rows.append((account_id, identifier, str(trade.get("clientOrderId") or ""),
                         str(trade.get("exchangeTradeId") or ""),
                         symbol, str(trade.get("direction") or ""), str(trade["offset"]), price, volume, when,
                         trading_day if isinstance(trading_day, str) else None,
                         float(multiplier) if multiplier else None, captured_at, "activity", 0))
        if not rows:
            return 0
        with self.lock, self.connection:
            # Fills already on record before this batch: a re-read must not shrink a backfill twice.
            known = {row[0] for row in self.connection.execute(
                "SELECT trade_id FROM account_trades WHERE trading_account_id = ? AND trade_id IN ("
                + ", ".join("?" * len(rows)) + ")", (account_id, *(row[1] for row in rows)))}
            self.connection.executemany(
                "INSERT INTO account_trades (trading_account_id, " + ", ".join(_COLUMNS) + ")"
                " VALUES (" + ", ".join("?" * (len(_COLUMNS) + 1)) + ")"
                " ON CONFLICT(trading_account_id, trade_id) DO UPDATE SET"
                " trading_day = COALESCE(excluded.trading_day, trading_day),"
                " volume_multiple = COALESCE(excluded.volume_multiple, volume_multiple)",
                rows,
            )
            # A real fill report for an order replaces the fill synthesized from its order link; the row
            # is captured first, and the 补充 on that fill's round trips follow it in the same
            # transaction wherever the replacement provably kept the same trade (#502/#514).
            # Partial fills arrive tranche by tranche (#514-10): the backfilled row shrinks to the
            # still-unmatched remainder instead of vanishing on the first tranche, and is deleted
            # (with the 补充 migration above) only once the real fills cover its whole volume.
            replaced: dict[str, dict[str, Any]] = {}
            for client_order_id in sorted({row[2] for row in rows if row[2]}):
                backfill = self.connection.execute(
                    "SELECT " + ", ".join(_COLUMNS) + " FROM account_trades WHERE source = 'order_backfill'"
                    " AND client_order_id = ? AND trading_account_id = ?",
                    (client_order_id, account_id)).fetchone()
                if backfill is None:
                    continue
                # The backfilled row already holds only the remainder: subtract just this batch's new fills.
                filled = sum(row[8] for row in rows
                             if row[2] == client_order_id and row[1] not in known)
                if filled <= 0:
                    continue
                if filled >= int(backfill["volume"]):
                    replaced[client_order_id] = _fill_row(backfill)
                    self.connection.execute(
                        "DELETE FROM account_trades WHERE source = 'order_backfill' AND client_order_id = ?"
                        " AND trading_account_id = ?", (client_order_id, account_id))
                else:
                    self.connection.execute(
                        "UPDATE account_trades SET volume = ? WHERE trading_account_id = ? AND trade_id = ?",
                        (int(backfill["volume"]) - filled, account_id, backfill["trade_id"]))
            if replaced:
                relink_replaced_backfill_notes(self.connection, account_id, replaced)
        return len(rows)

    def backfill_account_trades(
        self, trades: list[dict[str, Any]], trading_account_id: str | None = None,
    ) -> int:
        """Fills synthesized from finished order links: only where no real fill for the same
        client order exists, never overwriting one. A backfilled row is re-derived in place, so
        it always reflects its order link. Returns how many rows were written or changed."""
        account_id = trading_account_id or self.default_trading_account_id
        written = 0
        captured_at = datetime.now(UTC).isoformat()
        with self.lock, self.connection:
            for trade in trades:
                client_order_id = str(trade.get("clientOrderId") or "")
                if not client_order_id:
                    continue
                real = self.connection.execute(
                    "SELECT 1 FROM account_trades WHERE trading_account_id = ?"
                    " AND client_order_id = ? AND source = 'activity' LIMIT 1", (account_id, client_order_id)
                ).fetchone()
                if real is not None:
                    continue
                written += self.connection.execute(
                    "INSERT INTO account_trades (trading_account_id, " + ", ".join(_COLUMNS) + ")"
                    " VALUES (" + ", ".join("?" * (len(_COLUMNS) + 1)) + ")"
                    " ON CONFLICT(trading_account_id, trade_id) DO UPDATE SET price = excluded.price,"
                    " volume = excluded.volume, trade_date_time = excluded.trade_date_time,"
                    " trading_day = excluded.trading_day,"
                    " volume_multiple = COALESCE(excluded.volume_multiple, volume_multiple)"
                    " WHERE price IS NOT excluded.price OR volume IS NOT excluded.volume"
                    " OR trade_date_time IS NOT excluded.trade_date_time OR trading_day IS NOT excluded.trading_day"
                    " OR (volume_multiple IS NULL AND excluded.volume_multiple IS NOT NULL)",
                    (account_id, f"link:{client_order_id}", client_order_id, "", str(trade["symbol"]),
                     str(trade["direction"]), str(trade["offset"]), float(trade["price"]),
                     int(trade["volume"]), int(trade["tradeDateTime"]), trade.get("tradingDay"),
                     trade.get("volumeMultiple"), captured_at, "order_backfill", 1),
                ).rowcount
        return written

    def list_account_trades(
        self, *, symbol: str | None = None, trading_account_id: str | None = None,
        close_direction: str | None = None, offsets: tuple[str, ...] | set[str] | None = None,
        trading_day: str | None = None,
    ) -> list[dict[str, Any]]:
        """With `close_direction` / `offsets` / `trading_day` (all three, the stop-close
        chain's own read #700 review) the filter runs in the query: only that leg's own
        close fills come back. A fill is excluded by its day only when SQLite can prove
        the stored value is a normalized valid calendar date (`date(trading_day)` equals
        the stored text — SQLite `date()` rolls impossible dates like 2026-02-30 forward
        instead of rejecting them) AND that date is another trading day. Missing,
        unparseable or rolled-over values stay in the query; the caller re-judges them
        with the same strict normalization in Python (unknown is never guessed safe)."""
        account_id = trading_account_id or self.default_trading_account_id
        if offsets is not None and not offsets:
            return []
        query = "SELECT " + ", ".join(_COLUMNS) + " FROM account_trades WHERE trading_account_id = ?"
        params: list[Any] = [account_id]
        if symbol:
            query += " AND symbol = ? COLLATE NOCASE"
            params.append(symbol)
        if close_direction is not None and offsets is not None:
            query += " AND direction = ? AND offset IN (" + ", ".join("?" * len(offsets)) + ")"
            params.extend([close_direction, *offsets])
        if trading_day is not None:
            query += (" AND (trading_day IS NULL OR date(trading_day, '+0 days') IS NULL"
                      " OR date(trading_day, '+0 days') <> trading_day"
                      " OR date(trading_day, '+0 days') < '0001-01-01'"
                      " OR date(trading_day, '+0 days') = date(?))")
            params.append(trading_day)
        query += " ORDER BY trade_date_time, trade_id"
        return [{
            "tradeId": row["trade_id"], "clientOrderId": row["client_order_id"],
            "exchangeTradeId": row["exchange_trade_id"], "symbol": row["symbol"],
            "direction": row["direction"], "offset": row["offset"], "price": row["price"],
            "volume": row["volume"], "tradeDateTime": row["trade_date_time"],
            "tradingDay": row["trading_day"], "volumeMultiple": row["volume_multiple"],
            "source": row["source"], "approximate": bool(row["approximate"]),
        } for row in self.connection.execute(query, params)]

    def upsert_account_daily(
        self, trading_day: str, account: dict[str, Any], trading_account_id: str | None = None,
    ) -> None:
        """One row per trading day from the account snapshot; intraday reads overwrite the day.

        The day's P&L columns move together, as one snapshot: a reading that carries P&L
        (`carries_day_pnl`) replaces all four — a net of exactly 0 included — while TqKq's 15:15
        settlement, which clears all four, keeps what the day already booked. A snapshot that
        does not report the whole group (any of the four NULL) keeps the stored four rather than
        stitching its present fields onto values read at another moment (#514-8)."""
        account_id = trading_account_id or self.default_trading_account_id

        def number(key: str) -> float | None:
            value = account.get(key)
            try:
                return None if value is None else float(value)
            except (TypeError, ValueError):
                return None
        booked = 1 if carries_day_pnl(account) else 0
        # The four P&L columns swap as one unit only when the snapshot reports them all.
        whole = ("excluded.close_profit IS NOT NULL AND excluded.position_profit IS NOT NULL"
                 " AND excluded.commission IS NOT NULL AND excluded.today_net_pnl IS NOT NULL")
        pnl = ", ".join(f"{column} = CASE WHEN ? AND {whole} THEN excluded.{column}"
                        f" ELSE account_daily.{column} END"
                        for column in ("close_profit", "position_profit", "commission", "today_net_pnl"))
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO account_daily (trading_account_id, trading_day, equity, static_balance, close_profit,"
                " position_profit, commission, today_net_pnl, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(trading_account_id, trading_day) DO UPDATE SET equity = excluded.equity,"
                " static_balance = excluded.static_balance, " + pnl + ", captured_at = excluded.captured_at",
                (account_id, trading_day, number("equity"), number("staticBalance"), number("closeProfit"),
                 number("positionProfit"), number("commission"), number("todayNetPnl"),
                 datetime.now(UTC).isoformat(), booked, booked, booked, booked),
            )

    def list_account_daily(
        self, *, since_day: str | None = None, trading_account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        account_id = trading_account_id or self.default_trading_account_id
        query = ("SELECT trading_day, equity, static_balance, close_profit, position_profit, commission,"
                 " today_net_pnl, captured_at FROM account_daily WHERE trading_account_id = ?")
        params: list[Any] = [account_id]
        if since_day:
            query += " AND trading_day >= ?"
            params.append(since_day)
        query += " ORDER BY trading_day"
        return [{"tradingDay": row[0], "equity": row[1], "staticBalance": row[2], "closeProfit": row[3],
                 "positionProfit": row[4], "commission": row[5], "todayNetPnl": row[6], "capturedAt": row[7]}
                for row in self.connection.execute(query, params)]

    # 复盘补写 (2026-09-29): one line the user adds to a round trip (typed, spoken in the voice review, or
    # written for them by the AI in chat), keyed by the review's round id `openTradeId:closeTradeId`.

    def list_trade_notes(self, *, trading_account_id: str | None = None) -> dict[str, dict[str, Any]]:
        account_id = trading_account_id or self.default_trading_account_id
        return {row["round_id"]: {"text": row["text"], "via": row["via"], "updatedAt": row["updated_at"],
                                  "volumeAtWrite": row["volume_at_write"]}
                for row in self.connection.execute(
                    "SELECT round_id, text, via, updated_at, volume_at_write FROM trade_notes"
                    " WHERE trading_account_id = ?", (account_id,))}

    def list_unlinked_trade_notes(self, *, trading_account_id: str | None = None) -> list[dict[str, Any]]:
        """补充 still keyed on a backfilled fill (`link:<order>`) that a real fill has since replaced
        (#514): with that row gone no current round trip can carry the key, so the review lists them
        as unlinkedNotes — 原文、via、更新时间,只读,绝不在这里改写或删除。A note whose link row still
        exists (the order keeps its backfill) stays out: it can still pair today."""
        account_id = trading_account_id or self.default_trading_account_id
        out: list[dict[str, Any]] = []
        for row in self.connection.execute(
                "SELECT round_id, text, via, updated_at FROM trade_notes WHERE trading_account_id = ?"
                " AND round_id LIKE '%link:%' ORDER BY updated_at, round_id", (account_id,)):
            key = str(row["round_id"])
            # A round id is `openTradeId:closeTradeId`; pull the link: order out of either side.
            orders = [key[len("link:"):].split(":", 1)[0]] if key.startswith("link:") else []
            tail = key.rfind(":link:")
            if tail >= 0:
                orders.append(key[tail + len(":link:"):])
            if any(self.connection.execute(
                    "SELECT 1 FROM account_trades WHERE trading_account_id = ? AND trade_id = ?",
                    (account_id, f"link:{order}")).fetchone() is None for order in orders):
                out.append({"roundId": key, "text": row["text"], "via": row["via"],
                            "updatedAt": row["updated_at"]})
        return out

    def set_trade_note(self, round_id: str, text: str, *, via: str, user_id: str,
                       volume_at_write: int | None = None,
                       trading_account_id: str | None = None) -> dict[str, Any]:
        """`volume_at_write` stamps the round trip's lot count as the writer saw it (#514-2):
        a late-arriving earlier open can re-pair the same round id onto a different volume, and
        the reader then marks the 补充 as no longer describing the same lots."""
        account_id = trading_account_id or self.default_trading_account_id
        # 毫秒：记忆整理按这个时间与删除时间比先后（#524），秒级会把同一秒里的先后弄混。
        updated_at = datetime.now(UTC).isoformat(timespec="milliseconds")
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO trade_notes (trading_account_id, round_id, text, via, user_id, updated_at,"
                " volume_at_write) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(trading_account_id, round_id) DO UPDATE SET"
                " text = excluded.text, via = excluded.via, user_id = excluded.user_id,"
                " updated_at = excluded.updated_at, volume_at_write = excluded.volume_at_write",
                (account_id, round_id, text, via, user_id, updated_at,
                 int(volume_at_write) if volume_at_write is not None else None))
        return {"text": text, "via": via, "updatedAt": updated_at,
                "volumeAtWrite": int(volume_at_write) if volume_at_write is not None else None}

    def delete_trade_note(self, round_id: str, *, trading_account_id: str | None = None) -> bool:
        account_id = trading_account_id or self.default_trading_account_id
        with self.lock, self.connection:
            cursor = self.connection.execute("DELETE FROM trade_notes WHERE trading_account_id = ? AND round_id = ?",
                                             (account_id, round_id))
        return cursor.rowcount > 0

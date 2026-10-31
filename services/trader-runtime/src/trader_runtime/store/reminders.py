"""提醒记录 (#932, 2026-10-15): one row per reminder that went out — the claim that makes it go out once
(unique user × kind × dedupe_key, written in the same transaction as the conversation message it
announces), and the 提醒中心's record (read / state / where it jumps to). Rows live
AI_TRADER_REMINDER_RETAIN_DAYS (30) days; the retention sweep also clears the older dedupe ledgers that
still mirror a claim for a slot running the previous build (reminder_notices, delivery_notices,
push_notices) and the triggered 到价提醒 rows."""
from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from .base import StoreBase, utc_now

_UNSET: Any = object()


def _decode(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def reminder_view(row: sqlite3.Row) -> dict[str, Any]:
    """The 提醒中心 item (GET /api/reminders): actions only while the reminder still waits for one."""
    item: dict[str, Any] = {
        "id": row["id"], "kind": row["kind"], "tradingDay": row["trading_day"], "createdAt": row["created_at"],
        "accountId": row["account_id"] or None, "symbol": row["symbol"] or None, "name": row["name"] or None, "title": row["title"], "body": row["body"],
        "state": _decode(row["state_json"], None), "target": _decode(row["target_json"], {"type": "chat"}),
        "read": row["read_at"] is not None,
    }
    actions = _decode(row["actions_json"], None)
    if actions:
        item["actions"] = actions
    return item


class ReminderStore(StoreBase):

    def claim_reminder(self, connection: sqlite3.Connection, *, user_id: str, kind: str, dedupe_key: str,
                       trading_day: str, title: str, body: str, account_id: str = "", symbol: str = "",
                       name: str = "", session_id: str | None = None, message_id: str | None = None,
                       card_id: str | None = None, target: dict[str, Any] | None = None,
                       state: Any = None, actions: list[dict[str, Any]] | None = None,
                       created_at: str | None = None) -> dict[str, Any] | None:
        """The once-only claim, in the caller's transaction: the new row's id when (user, kind, key) is
        written for the first time, None when this reminder already went out (nothing is written)."""
        reminder_id = uuid4().hex
        cursor = connection.execute(
            """INSERT OR IGNORE INTO reminders (id, user_id, account_id, kind, trading_day, created_at, symbol, name,
            title, body, session_id, message_id, card_id, target_json, state_json, actions_json, dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (reminder_id, user_id, account_id or "", kind, trading_day, created_at or utc_now(), symbol or "",
             name or "", title[:80], body[:500], session_id, message_id, card_id,
             json.dumps(target or {"type": "chat"}, ensure_ascii=False),
             json.dumps(state, ensure_ascii=False) if state is not None else None,
             json.dumps(actions, ensure_ascii=False) if actions else None, dedupe_key))
        if cursor.rowcount != 1:
            return None
        return reminder_view(connection.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone())

    @staticmethod
    def _legacy_told_fills(connection: sqlite3.Connection, *, user_id: str, prefix: str) -> set[str]:
        """Reminders told before the per-fill claims existed name their fills in the key (account:order:t1,t2)."""
        rows = connection.execute(
            "SELECT dedupe_key FROM reminders WHERE user_id = ? AND kind = 'fill' AND substr(dedupe_key, 1, ?) = ?",
            (user_id, len(prefix), prefix)).fetchall()
        told: set[str] = set()
        for row in rows:
            told.update(item for item in str(row["dedupe_key"])[len(prefix):].split(",") if item)
        return told

    def told_fill_trades(self, *, user_id: str, account_id: str, order_id: str) -> set[str]:
        """成交提醒的稳定身份 (#932): the trade ids of this order the user was already told about — however a
        restart or the other slot grouped them into reminders. A read before the claim: it only spares a
        transaction; what is told is decided by `claim_fill_trades` inside it."""
        prefix = f"{account_id}:{order_id}:"
        with self.lock:
            rows = self.connection.execute(
                "SELECT trade_id FROM reminder_fill_claims WHERE user_id = ? AND account_id = ? AND order_id = ?",
                (user_id, account_id, order_id)).fetchall()
            legacy = self._legacy_told_fills(self.connection, user_id=user_id, prefix=prefix)
        return {str(row["trade_id"]) for row in rows} | legacy

    def claim_fill_trades(self, connection: sqlite3.Connection, *, user_id: str, account_id: str, order_id: str,
                          trade_ids: list[str]) -> list[str]:
        """Claim each fill once per user, account and order, in the reminder's own transaction: the ids of
        `trade_ids` (in their order) this transaction claimed for the first time and no older set key told
        — exactly the fills the reminder may count (#932: another holder that read the same fills earlier
        and committed first keeps its own; empty = nothing new)."""
        legacy = self._legacy_told_fills(connection, user_id=user_id, prefix=f"{account_id}:{order_id}:")
        now = utc_now()
        claimed: list[str] = []
        for trade_id in dict.fromkeys(trade_ids):
            inserted = connection.execute(
                """INSERT OR IGNORE INTO reminder_fill_claims (user_id, account_id, order_id, trade_id, created_at)
                VALUES (?, ?, ?, ?, ?)""", (user_id, account_id, order_id, trade_id, now)).rowcount == 1
            if inserted and trade_id not in legacy:
                claimed.append(trade_id)
        return claimed

    def reminder_claimed(self, *, user_id: str, kind: str, dedupe_key: str) -> bool:
        with self.lock:
            return self.connection.execute(
                "SELECT 1 FROM reminders WHERE user_id = ? AND kind = ? AND dedupe_key = ?",
                (user_id, kind, dedupe_key)).fetchone() is not None

    def reminders_page(self, user_id: str, *, trading_day: str, scope: str, unread_only: bool = False,
                       before: str | None = None, limit: int = 50) -> tuple[list[dict[str, Any]], str | None]:
        """Newest first. `scope` today = the trading day itself, earlier = every day before it; `before`
        is the previous page's cursor (created_at|id)."""
        clauses = ["user_id = ?", "trading_day = ?" if scope == "today" else "trading_day < ?"]
        values: list[Any] = [user_id, trading_day]
        if unread_only:
            clauses.append("read_at IS NULL")
        if before:
            at, _, last_id = before.partition("|")
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            values += [at, at, last_id]
        with self.lock:
            rows = self.connection.execute(
                f"SELECT * FROM reminders WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id DESC LIMIT ?",
                (*values, limit + 1)).fetchall()
        items = [reminder_view(row) for row in rows[:limit]]
        cursor = f"{rows[limit - 1]['created_at']}|{rows[limit - 1]['id']}" if len(rows) > limit else None
        return items, cursor

    def unread_reminder_count(self, user_id: str, *, trading_day: str) -> int:
        with self.lock:
            return int(self.connection.execute(
                "SELECT COUNT(*) FROM reminders WHERE user_id = ? AND trading_day = ? AND read_at IS NULL",
                (user_id, trading_day)).fetchone()[0])

    def mark_reminders_read(self, user_id: str, ids: list[str] | None) -> int:
        """Marks the user's own reminders read (`ids` None = all of them); returns how many changed."""
        now = utc_now()
        with self.lock, self.connection:
            if ids is None:
                return self.connection.execute(
                    "UPDATE reminders SET read_at = ? WHERE user_id = ? AND read_at IS NULL", (now, user_id)).rowcount
            changed = 0
            for chunk in range(0, len(ids), 200):
                part = ids[chunk:chunk + 200]
                changed += self.connection.execute(
                    f"UPDATE reminders SET read_at = ? WHERE user_id = ? AND read_at IS NULL"
                    f" AND id IN ({','.join('?' for _ in part)})", (now, user_id, *part)).rowcount
            return changed

    def update_reminder_state(self, *, card_id: str, state: Any = _UNSET, actions: Any = _UNSET,
                              user_id: str | None = None) -> list[dict[str, Any]]:
        """A reminder's state / actions follow its card (平仓单 status, 保本 moved / declined); returns the rows
        that actually changed, as (userId, item) pairs flattened into the item with `userId`."""
        if not card_id or (state is _UNSET and actions is _UNSET):
            return []
        columns: list[str] = []
        values: list[str | None] = []
        if state is not _UNSET:
            columns.append("state_json")
            values.append(json.dumps(state, ensure_ascii=False) if state is not None else None)
        if actions is not _UNSET:
            columns.append("actions_json")
            values.append(json.dumps(actions, ensure_ascii=False) if actions else None)
        sets = [f"{column} = ?" for column in columns]
        where, keys = "card_id = ?", [card_id]
        if user_id is not None:
            where += " AND user_id = ?"
            keys.append(user_id)
        # Only rows whose stored value differs: an unchanged state is not re-announced.
        changed_where = " OR ".join(f"{column} IS NOT ?" for column in columns)
        with self.lock, self.connection:
            rows = self.connection.execute(
                f"SELECT id FROM reminders WHERE {where} AND ({changed_where})", (*keys, *values)).fetchall()
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            self.connection.execute(
                f"UPDATE reminders SET {', '.join(sets)} WHERE id IN ({','.join('?' for _ in ids)})", (*values, *ids))
            updated = self.connection.execute(
                f"SELECT * FROM reminders WHERE id IN ({','.join('?' for _ in ids)})", ids).fetchall()
        return [{**reminder_view(row), "userId": row["user_id"]} for row in updated]

    def purge_reminders(self, *, before: str) -> dict[str, int]:
        """The retention sweep: reminders, the legacy dedupe ledgers, settled 保本 questions and triggered
        到价提醒 older than `before` (ISO UTC; the ledgers keyed by trading day compare the day)."""
        day = before[:10]
        counts: dict[str, int] = {}
        with self.lock, self.connection:
            for table, sql, value in (
                ("reminders", "DELETE FROM reminders WHERE created_at < ?", before),
                ("reminder_notices", "DELETE FROM reminder_notices WHERE created_at < ?", before),
                ("delivery_notices", "DELETE FROM delivery_notices WHERE created_at < ?", before),
                ("push_notices", "DELETE FROM push_notices WHERE created_at < ?", before),
                ("breakeven_notices", "DELETE FROM breakeven_notices WHERE trade_day < ? AND status != 'pending'", day),
                ("price_alerts", "DELETE FROM price_alerts WHERE status = 'triggered' AND triggered_at < ?", before),
                ("unfilled_watch_entries", "DELETE FROM unfilled_watch_entries WHERE confirmed_at < ?", before),
                ("reminder_fill_claims", "DELETE FROM reminder_fill_claims WHERE created_at < ?", before),
            ):
                counts[table] = self.connection.execute(sql, (value,)).rowcount
        return counts

    def reminders_epoch(self) -> str:
        """When this database first had the reminders table (ISO UTC): a fill the account ledger first sees
        after a restart is announced only if it happened after this — before it, the previous build's own
        in-memory detector already told the user."""
        with self.lock:
            row = self.connection.execute("SELECT value FROM reminder_meta WHERE key = 'epoch'").fetchone()
        return str(row["value"]) if row else ""

    def migrate_legacy_reminders(self) -> int:
        """Once per database (inside the schema migration's transaction): the last 30 days of the older
        once-only ledgers become read 提醒中心 rows under the same dedupe keys the new delivery uses, so a
        reminder already sent before the switch is never sent again and still shows in 更早."""
        from datetime import UTC, datetime, timedelta

        from ..trading_calendar import trading_day_at
        connection = self.connection
        # A reminders table made by an earlier build of this table (or this one) is kept: the columns are
        # checked one by one and the missing ones added, never assumed fresh.
        for definition in ("account_id TEXT NOT NULL DEFAULT ''", "symbol TEXT NOT NULL DEFAULT ''",
                           "name TEXT NOT NULL DEFAULT ''", "session_id TEXT", "message_id TEXT", "card_id TEXT",
                           "target_json TEXT NOT NULL DEFAULT '{\"type\":\"chat\"}'", "state_json TEXT",
                           "actions_json TEXT", "read_at TEXT"):
            self._add_column("reminders", definition)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_reminders_card ON reminders(card_id)")
        if connection.execute("SELECT 1 FROM reminder_meta WHERE key = 'legacy_migrated'").fetchone():
            return 0
        now = utc_now()
        connection.execute("INSERT OR IGNORE INTO reminder_meta (key, value) VALUES ('epoch', ?)", (now,))
        cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        rows: list[tuple[Any, ...]] = []

        def add(user_id: str, kind: str, key: str, day: str, at: str, title: str, body: str, *,
                account_id: str = "", symbol: str = "", target: dict[str, Any] | None = None) -> None:
            rows.append((uuid4().hex, user_id, account_id, kind, day, at, symbol, title, body,
                         json.dumps(target or {"type": "chat"}, ensure_ascii=False), now, key))

        def day_of(at: str) -> str:
            try:
                return trading_day_at(at)
            except ValueError:
                return at[:10]

        for row in connection.execute("SELECT * FROM reminder_notices WHERE created_at >= ?", (cutoff,)):
            close = str(row["kind"]).startswith("market_close")
            half = "night" if row["kind"] == "market_close_night" else "day"
            key = f"{row['account_id']}:{row['trade_day']}" + (f":{half}" if close else "")
            add(row["user_id"], "market_close" if close else "risk_ratio", key, row["trade_day"], row["created_at"],
                "收盘提醒" if close else "风险度提醒", row["text"], account_id=row["account_id"])
        for row in connection.execute("SELECT * FROM delivery_notices WHERE created_at >= ?", (cutoff,)):
            add(row["user_id"], "delivery", f"{row['symbol']}:{row['trade_day']}", row["trade_day"], row["created_at"],
                "交割提醒", row["text"], symbol=row["symbol"])
        for row in connection.execute("SELECT * FROM push_notices WHERE created_at >= ?", (cutoff,)):
            key = str(row["key"])
            if key.startswith("market:"):
                symbol = key.split(":")[1] if len(key.split(":")) > 1 else ""
                add(row["user_id"], "market_movement", key, day_of(row["created_at"]), row["created_at"],
                    "盘中异动", str(row["text"] or "盘中异动"), symbol=symbol)
            elif key.startswith("morning:"):
                day = key.split(":", 1)[1]
                add(row["user_id"], "morning_desk", day, day, row["created_at"], "开盘前判断", "今日判断好了",
                    target={"type": "research"})
        for row in connection.execute("SELECT * FROM breakeven_notices WHERE created_at >= ?", (cutoff,)):
            card = _decode(row["card_json"], {})
            add(row["user_id"], "breakeven", row["id"], row["trade_day"], row["created_at"],
                str(card.get("title") or "保本提醒"), str(card.get("summary") or "保本提醒"),
                account_id=row["trading_account_id"], symbol=row["symbol"],
                target={"type": "chat", "cardId": card.get("cardId"), "sessionId": row["session_id"] or None})
        for row in connection.execute(
                "SELECT * FROM daily_reviews WHERE pushed_at IS NOT NULL AND pushed_at >= ?", (cutoff,)):
            add(row["user_id"], "daily_review", f"{row['account_id']}:{row['trading_day']}", row["trading_day"],
                row["pushed_at"], f"今日复盘 · {row['trading_day'][5:]}", "今天的复盘好了",
                account_id=row["account_id"], target={"type": "review", "tradingDay": row["trading_day"]})
        for row in connection.execute(
                "SELECT * FROM price_alerts WHERE status = 'triggered' AND triggered_at >= ?", (cutoff,)):
            add(row["user_id"], "price_alert", row["id"], day_of(row["triggered_at"]), row["triggered_at"],
                "到价提醒", f"{row['name'] or row['symbol']} 到了 {row['price']}", symbol=row["symbol"])
        connection.executemany(
            """INSERT OR IGNORE INTO reminders (id, user_id, account_id, kind, trading_day, created_at, symbol, title,
            body, target_json, read_at, dedupe_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
        connection.execute("INSERT OR IGNORE INTO reminder_meta (key, value) VALUES ('legacy_migrated', ?)", (now,))
        return len(rows)

    # -- the unfilled-order watch's own rows (order_watch.py) -------------------------------------------

    def save_order_watch(self, entry: dict[str, Any]) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO unfilled_watch_entries (client_order_id, user_id, account_id, session_id, payload_json,
                confirmed_at) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET user_id = excluded.user_id, account_id = excluded.account_id,
                session_id = excluded.session_id, payload_json = excluded.payload_json, confirmed_at = excluded.confirmed_at""",
                (entry["clientOrderId"], entry["userId"], entry["accountId"], entry.get("sessionId") or "",
                 json.dumps(entry, ensure_ascii=False), entry["confirmedAt"]))

    def delete_order_watch(self, client_order_id: str) -> None:
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM unfilled_watch_entries WHERE client_order_id = ?", (client_order_id,))

    def unfilled_watch_entries(self, *, since: str) -> list[dict[str, Any]]:
        """The watched orders confirmed after `since` (older ones are past the watch's cap and dropped)."""
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM unfilled_watch_entries WHERE confirmed_at < ?", (since,))
            rows = self.connection.execute("SELECT payload_json FROM unfilled_watch_entries ORDER BY confirmed_at").fetchall()
        return [entry for row in rows if isinstance(entry := _decode(row["payload_json"], None), dict)]

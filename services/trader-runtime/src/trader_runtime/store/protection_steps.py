"""第四批 (2026-10-06): the durable state of two protection follow-ups.

- `card_protection_steps`: a confirmed close card's second step (剩余仓位的止损/止盈改到哪). One row per
  card, written in the confirmation's own transaction; the relay (card_protection) moves it from
  awaiting_close to done / failed / skipped exactly once — the final write rides the same transaction
  that replaces the protection rows, so a crash or a second confirm never replaces twice.
- `breakeven_notices`: 保本提醒, one row per user × account × trading day × contract × direction
  (INSERT OR IGNORE: at most one question per position per day, restarts never re-ask); the row is also
  what the card's 移到成本 / 不用 act on (pending → moved / declined / failed, once)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .base import StoreBase, utc_now


def _step_view(row: Any) -> dict[str, Any]:
    return {"proposalId": row["proposal_id"], "userId": row["user_id"], "tradingAccountId": row["trading_account_id"],
            "sessionId": row["session_id"], "symbol": row["symbol"], "direction": row["direction"],
            "plan": json.loads(row["plan_json"]), "status": row["status"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "readySince": row["ready_since"], "createdAt": row["created_at"], "updatedAt": row["updated_at"]}


def _notice_view(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "userId": row["user_id"], "tradingAccountId": row["trading_account_id"],
            "tradeDay": row["trade_day"], "symbol": row["symbol"], "direction": row["direction"],
            "sessionId": row["session_id"], "card": json.loads(row["card_json"]), "status": row["status"],
            "result": row["result"], "createdAt": row["created_at"], "updatedAt": row["updated_at"]}


class ProtectionStepStore(StoreBase):

    # -- 合并卡第二步 ---------------------------------------------------------------------------

    def create_card_protection_step(self, *, connection: sqlite3.Connection, proposal_id: str, user_id: str,
                                    trading_account_id: str, session_id: str, symbol: str, direction: str,
                                    plan: dict[str, Any]) -> None:
        now = utc_now()
        connection.execute(
            """INSERT OR IGNORE INTO card_protection_steps
            (proposal_id, user_id, trading_account_id, session_id, symbol, direction, plan_json, status,
             result_json, ready_since, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_close', NULL, NULL, ?, ?)""",
            (proposal_id, user_id, trading_account_id, session_id, symbol, direction,
             json.dumps(plan, ensure_ascii=False), now, now))

    def card_protection_step(self, proposal_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM card_protection_steps WHERE proposal_id = ?", (proposal_id,)).fetchone()
        return _step_view(row) if row is not None else None

    def awaiting_card_protection_steps(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM card_protection_steps WHERE status = 'awaiting_close' ORDER BY created_at").fetchall()
        return [_step_view(row) for row in rows]

    def mark_card_protection_ready(self, proposal_id: str, at: str) -> None:
        """The first moment the close was seen finished with a fill (the account's own report may lag)."""
        with self.lock, self.connection:
            self.connection.execute(
                """UPDATE card_protection_steps SET ready_since = ?, updated_at = ?
                WHERE proposal_id = ? AND status = 'awaiting_close' AND ready_since IS NULL""",
                (at, utc_now(), proposal_id))

    def finish_card_protection_step(self, proposal_id: str, *, status: str, result: dict[str, Any],
                                    connection: sqlite3.Connection | None = None) -> bool:
        """awaiting_close → done / failed / skipped, once: False when the step already ended."""
        sql = """UPDATE card_protection_steps SET status = ?, result_json = ?, updated_at = ?
            WHERE proposal_id = ? AND status = 'awaiting_close'"""
        values = (status, json.dumps(result, ensure_ascii=False), utc_now(), proposal_id)
        if connection is not None:
            return connection.execute(sql, values).rowcount == 1
        with self.lock, self.connection:
            return self.connection.execute(sql, values).rowcount == 1

    # -- 保本提醒 ------------------------------------------------------------------------------

    def claim_breakeven_notice(self, *, connection: sqlite3.Connection, notice_id: str, user_id: str,
                               trading_account_id: str, trade_day: str, symbol: str, direction: str,
                               session_id: str, card: dict[str, Any]) -> bool:
        """True only for the first question about this position today (the dedup row and the card)."""
        now = utc_now()
        cursor = connection.execute(
            """INSERT OR IGNORE INTO breakeven_notices
            (id, user_id, trading_account_id, trade_day, symbol, direction, session_id, card_json, status,
             result, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)""",
            (notice_id, user_id, trading_account_id, trade_day, symbol, direction, session_id,
             json.dumps(card, ensure_ascii=False), now, now))
        return cursor.rowcount == 1

    def breakeven_notice(self, notice_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute("SELECT * FROM breakeven_notices WHERE id = ?", (notice_id,)).fetchone()
        return _notice_view(row) if row is not None else None

    def breakeven_notices(self, *, user_id: str, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql = "SELECT * FROM breakeven_notices WHERE user_id = ?"
        values: list[Any] = [user_id]
        if status is not None:
            sql += " AND status = ?"
            values.append(status)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        values.append(limit)
        with self.lock:
            rows = self.connection.execute(sql, values).fetchall()
        return [_notice_view(row) for row in rows]

    def settle_breakeven_notice(self, notice_id: str, *, status: str, result: str, card: dict[str, Any],
                                connection: sqlite3.Connection | None = None) -> bool:
        """pending → moved / declined / failed, once (False when the question was already answered)."""
        sql = """UPDATE breakeven_notices SET status = ?, result = ?, card_json = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'"""
        values = (status, result, json.dumps(card, ensure_ascii=False), utc_now(), notice_id)
        if connection is not None:
            return connection.execute(sql, values).rowcount == 1
        with self.lock, self.connection:
            return self.connection.execute(sql, values).rowcount == 1

"""到价提醒 rows: per user, account-independent — the quote has no account, neither does the alert."""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any
from uuid import uuid4

from .base import StoreBase, utc_now


def alert_view(row: Any) -> dict[str, Any]:
    """One alert in the shape the routes, the tools and the client read."""
    return {
        "id": row["id"], "symbol": row["symbol"], "name": row["name"],
        "direction": row["direction"], "price": row["price"], "note": row["note"],
        "status": row["status"], "via": row["created_via"], "sessionId": row["session_id"],
        "createdAt": row["created_at"], "triggeredAt": row["triggered_at"],
        "triggerPrice": row["trigger_price"],
    }


class PriceAlertStore(StoreBase):
    """Create / list / cancel / trigger. Status moves are conditional UPDATEs so the
    monitor can never trigger one alert twice, whatever the loop timing."""

    def create_price_alert(self, *, user_id: str, symbol: str, name: str, direction: str,
                           price: Decimal, note: str, via: str, session_id: str | None,
                           max_active: int) -> dict[str, Any] | str:
        """Insert one active alert. Returns the row view, or the Chinese refusal reason
        (the per-user active cap) — the check and the insert share one locked transaction."""
        alert_id, now = uuid4().hex, utc_now()
        with self.lock, self.connection:
            active = self.connection.execute(
                "SELECT COUNT(*) FROM price_alerts WHERE user_id = ? AND status = 'active'",
                (user_id,)).fetchone()[0]
            if active >= max_active:
                return f"生效中的提醒已有 {active} 条，达到上限 {max_active} 条；先删掉一条再设"
            self.connection.execute(
                """INSERT INTO price_alerts
                (id, user_id, symbol, name, direction, price, note, status, created_via, session_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (alert_id, user_id, symbol, name, direction, str(price), note, via, session_id, now))
        row = self.connection.execute("SELECT * FROM price_alerts WHERE id = ?", (alert_id,)).fetchone()
        return alert_view(row)

    def list_price_alerts(self, *, user_id: str, triggered_limit: int = 20) -> dict[str, Any]:
        """The user's active alerts (newest first) and the most recent triggered ones."""
        with self.lock:
            active = self.connection.execute(
                "SELECT * FROM price_alerts WHERE user_id = ? AND status = 'active' ORDER BY created_at DESC, id",
                (user_id,)).fetchall()
            triggered = self.connection.execute(
                """SELECT * FROM price_alerts WHERE user_id = ? AND status = 'triggered'
                ORDER BY triggered_at DESC, id LIMIT ?""", (user_id, triggered_limit)).fetchall()
        return {"active": [alert_view(row) for row in active],
                "triggered": [alert_view(row) for row in triggered]}

    def active_price_alerts(self) -> list[dict[str, Any]]:
        """Every active alert of every user — the monitor's one read per tick."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM price_alerts WHERE status = 'active' ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def cancel_price_alert(self, *, user_id: str, alert_id: str) -> dict[str, Any] | str | None:
        """Active → cancelled. A triggered alert is archived history (最近已触发, no delete): the
        Chinese refusal saying so (#932). None (the caller's 404) when the user has no such alert
        left — someone else's, never existed, or already cancelled."""
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE price_alerts SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'active'",
                (alert_id, user_id))
            if cursor.rowcount != 1:
                fired = self.connection.execute(
                    "SELECT 1 FROM price_alerts WHERE id = ? AND user_id = ? AND status = 'triggered'",
                    (alert_id, user_id)).fetchone()
                return "这条提醒已经触发过，已归入“最近已触发”，不用删除" if fired else None
        row = self.connection.execute("SELECT * FROM price_alerts WHERE id = ?", (alert_id,)).fetchone()
        return alert_view(row)

    def trigger_price_alert(self, *, alert_id: str, trigger_price: Decimal,
                            now: str | None = None, connection: sqlite3.Connection | None = None,
                            ) -> dict[str, Any] | None:
        """Active → triggered with the crossing price; None when another tick already won it.
        `now` carries the trigger moment's clock — the replay clock in a replay child, so
        triggered_at reads 回放里的“现在” instead of the wall clock. `connection` runs the claim
        inside the caller's transaction (#521): the dedup row and the staged notice commit or
        roll back together, so a crash between them retries next round instead of losing the
        persistent message while the alert reads already-triggered."""
        database = connection or self.connection
        if connection is None:
            with self.lock, self.connection:
                cursor = self.connection.execute(
                    """UPDATE price_alerts SET status = 'triggered', triggered_at = ?, trigger_price = ?
                    WHERE id = ? AND status = 'active'""",
                    (now or utc_now(), str(trigger_price), alert_id))
                won = cursor.rowcount == 1
        else:
            cursor = connection.execute(
                """UPDATE price_alerts SET status = 'triggered', triggered_at = ?, trigger_price = ?
                WHERE id = ? AND status = 'active'""",
                (now or utc_now(), str(trigger_price), alert_id))
            won = cursor.rowcount == 1
        if not won:
            return None
        row = database.execute("SELECT * FROM price_alerts WHERE id = ?", (alert_id,)).fetchone()
        return alert_view(row)

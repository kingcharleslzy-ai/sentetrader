"""锁屏推送 (2026-10-06): the Web Push subscription of every device a user turned it on for, and the
"once" ledger for push-born events that have no other dedup row (盘中异动, 开盘前判断)."""
from __future__ import annotations

import sqlite3
from typing import Any

from .base import StoreBase, utc_now


def subscription_view(row: Any) -> dict[str, Any]:
    return {"userId": row["user_id"], "deviceId": row["device_id"], "endpoint": row["endpoint"],
            "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}, "platform": row["platform"],
            "createdAt": row["created_at"], "lastOkAt": row["last_ok_at"], "lastError": row["last_error"]}


class PushSubscriptionStore(StoreBase):

    def save_push_subscription(self, *, user_id: str, device_id: str, endpoint: str, p256dh: str, auth: str,
                               platform: str) -> None:
        """One subscription per device: a device that subscribes again (a new endpoint) replaces its
        old row, and an endpoint another user registered on this browser leaves that user — the
        browser now belongs to whoever signed in and turned it on."""
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM push_subscriptions WHERE user_id = ? AND device_id = ? AND endpoint <> ?",
                                    (user_id, device_id, endpoint))
            self.connection.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id <> ?", (endpoint, user_id))
            self.connection.execute(
                """INSERT INTO push_subscriptions (user_id, device_id, endpoint, p256dh, auth, platform, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, endpoint) DO UPDATE SET device_id = excluded.device_id, p256dh = excluded.p256dh,
                auth = excluded.auth, platform = excluded.platform, last_error = NULL""",
                (user_id, device_id, endpoint, p256dh, auth, platform, now))

    def list_push_subscriptions(self, user_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM push_subscriptions WHERE user_id = ? ORDER BY created_at, device_id", (user_id,)).fetchall()
        return [subscription_view(row) for row in rows]

    def push_user_ids(self) -> list[str]:
        with self.lock:
            return [row[0] for row in self.connection.execute(
                "SELECT DISTINCT user_id FROM push_subscriptions ORDER BY user_id").fetchall()]

    def delete_push_subscriptions(self, *, user_id: str, device_id: str | None = None,
                                  endpoint: str | None = None) -> int:
        clauses, values = ["user_id = ?"], [user_id]
        if device_id is not None:
            clauses.append("device_id = ?")
            values.append(device_id)
        if endpoint is not None:
            clauses.append("endpoint = ?")
            values.append(endpoint)
        with self.lock, self.connection:
            return self.connection.execute(
                f"DELETE FROM push_subscriptions WHERE {' AND '.join(clauses)}", values).rowcount

    def retire_device_push_subscriptions(self, *, device_id: str, keep_user_id: str | None = None) -> int:
        """A device's lock-screen push belongs to whoever is signed in on it now: a user's heartbeat
        from this device (`keep_user_id`) drops every other user's subscription here; nothing else
        about those users changes."""
        with self.lock, self.connection:
            return self.connection.execute(
                "DELETE FROM push_subscriptions WHERE device_id = ? AND user_id <> ?",
                (device_id, keep_user_id or "")).rowcount

    def record_push_result(self, *, user_id: str, endpoint: str, error: str | None) -> None:
        """A delivery's outcome on its row: the last success time, or the last failure's short reason."""
        with self.lock, self.connection:
            if error is None:
                self.connection.execute("UPDATE push_subscriptions SET last_ok_at = ?, last_error = NULL"
                                        " WHERE user_id = ? AND endpoint = ?", (utc_now(), user_id, endpoint))
            else:
                self.connection.execute("UPDATE push_subscriptions SET last_error = ? WHERE user_id = ? AND endpoint = ?",
                                        (error[:200], user_id, endpoint))

    def claim_push_notice(self, *, user_id: str, key: str, connection: sqlite3.Connection | None = None,
                          at: str | None = None, text: str | None = None) -> bool:
        """True when this (user, key) is written for the first time — the event has not gone out yet.
        `connection` writes it inside the caller's transaction, beside the staged message; `at` is the
        caller's clock (ISO, UTC), so the 15-minute spacing follows the same clock that judged it; `text`
        keeps what the event said (今日复盘 quotes a 盘中异动 as the reason a later order was placed)."""
        sql = "INSERT OR IGNORE INTO push_notices (user_id, key, created_at, text) VALUES (?, ?, ?, ?)"
        values = (user_id, key, at or utc_now(), text)
        if connection is not None:
            return connection.execute(sql, values).rowcount == 1
        with self.lock, self.connection:
            return self.connection.execute(sql, values).rowcount == 1

    def last_push_notice_at(self, *, user_id: str, prefix: str,
                            connection: sqlite3.Connection | None = None) -> str | None:
        """The newest claim under `prefix` (盘中异动's per-product 15-minute spacing)."""
        sql = "SELECT MAX(created_at) FROM push_notices WHERE user_id = ? AND substr(key, 1, ?) = ?"
        values = (user_id, len(prefix), prefix)
        if connection is not None:
            return connection.execute(sql, values).fetchone()[0]
        with self.lock:
            return self.connection.execute(sql, values).fetchone()[0]

    def account_owner_ids(self, account_id: str) -> list[str]:
        """Who an account's events are pushed to: its owners only (只推本人账户的事)."""
        with self.lock:
            return [row[0] for row in self.connection.execute(
                "SELECT user_id FROM account_grants WHERE account_id = ? AND role = 'owner' ORDER BY user_id",
                (account_id,)).fetchall()]

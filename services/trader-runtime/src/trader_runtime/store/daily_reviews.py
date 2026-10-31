"""今日复盘 (2026-10-06): one review per user × account × trading day, and the user's answers to the
reviews' 习惯 observations."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .base import StoreBase, utc_now


def _row(row: Any) -> dict[str, Any]:
    return {"tradingDay": row["trading_day"], "accountId": row["account_id"], "payload": json.loads(row["payload_json"]),
            "generatedAt": row["generated_at"], "version": row["version"], "pushedAt": row["pushed_at"]}


class DailyReviewStore(StoreBase):

    def get_daily_review(self, *, user_id: str, account_id: str, trading_day: str | None = None) -> dict[str, Any] | None:
        """That day's review, or (no day) the latest one; None when there is none."""
        with self.lock:
            if trading_day:
                row = self.connection.execute(
                    "SELECT * FROM daily_reviews WHERE user_id = ? AND account_id = ? AND trading_day = ?",
                    (user_id, account_id, trading_day)).fetchone()
            else:
                row = self.connection.execute(
                    "SELECT * FROM daily_reviews WHERE user_id = ? AND account_id = ? ORDER BY trading_day DESC LIMIT 1",
                    (user_id, account_id)).fetchone()
        return _row(row) if row is not None else None

    def daily_review_days(self, *, user_id: str, account_id: str, limit: int = 60) -> list[str]:
        """The trading days that have a review, newest first."""
        with self.lock:
            return [row[0] for row in self.connection.execute(
                "SELECT trading_day FROM daily_reviews WHERE user_id = ? AND account_id = ?"
                " ORDER BY trading_day DESC LIMIT ?", (user_id, account_id, limit))]

    def recent_daily_reviews(self, *, user_id: str, account_id: str, before: str, limit: int = 5) -> list[dict[str, Any]]:
        """The reviews of the trading days before `before`, newest first (the observation's history)."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM daily_reviews WHERE user_id = ? AND account_id = ? AND trading_day < ?"
                " ORDER BY trading_day DESC LIMIT ?", (user_id, account_id, before, limit)).fetchall()
        return [_row(row) for row in rows]

    def save_daily_review(self, *, user_id: str, account_id: str, trading_day: str, payload: dict[str, Any],
                          version: int, connection: sqlite3.Connection | None = None) -> None:
        """Write (or overwrite) the day's review; the push mark stays as it was."""
        sql = ("INSERT INTO daily_reviews (user_id, account_id, trading_day, payload_json, generated_at, version)"
               " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, account_id, trading_day) DO UPDATE SET"
               " payload_json = excluded.payload_json, generated_at = excluded.generated_at, version = excluded.version")
        values = (user_id, account_id, trading_day, json.dumps(payload, ensure_ascii=False),
                  str(payload.get("generatedAt") or utc_now()), version)
        if connection is not None:
            connection.execute(sql, values)
            return
        with self.lock, self.connection:
            self.connection.execute(sql, values)

    def claim_daily_review_push(self, *, user_id: str, account_id: str, trading_day: str,
                                connection: sqlite3.Connection) -> bool:
        """True exactly once per review: the 15:15 delivery's dedup mark, in the caller's transaction."""
        return connection.execute(
            "UPDATE daily_reviews SET pushed_at = ? WHERE user_id = ? AND account_id = ? AND trading_day = ?"
            " AND pushed_at IS NULL", (utc_now(), user_id, account_id, trading_day)).rowcount == 1

    @staticmethod
    def _generation_key(account_id: str, trading_day: str) -> str:
        return f"daily-review-generate:{account_id}:{trading_day}"

    def claim_daily_review_generation(self, *, user_id: str, account_id: str, trading_day: str,
                                      stale_before: str) -> bool:
        """The 15:15 run's right to generate this review (2026-10-12), on the push-notice claim table: True for
        the first claimer, or when an earlier claim is older than `stale_before` (its process died mid-call).
        Only the claimer calls the model and saves; the others deliver what it saved."""
        with self.lock, self.connection:
            return self.connection.execute(
                "INSERT INTO push_notices (user_id, key, created_at) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id, key) DO UPDATE SET created_at = excluded.created_at"
                " WHERE push_notices.created_at < ?",
                (user_id, self._generation_key(account_id, trading_day), utc_now(), stale_before)).rowcount == 1

    def release_daily_review_generation(self, *, user_id: str, account_id: str, trading_day: str) -> None:
        """Give the claim back (nothing was saved: the account was not readable yet); the next round claims again."""
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM push_notices WHERE user_id = ? AND key = ?",
                                    (user_id, self._generation_key(account_id, trading_day)))

    def record_observation_verdict(self, *, user_id: str, key: str, verdict: str, text: str,
                                   account_id: str, trading_day: str,
                                   connection: sqlite3.Connection | None = None) -> None:
        """The user's answer to an observation; with `connection`, on the caller's transaction."""
        if connection is None:
            with self.lock, self.connection:
                self.record_observation_verdict(user_id=user_id, key=key, verdict=verdict, text=text,
                                                account_id=account_id, trading_day=trading_day,
                                                connection=self.connection)
            return
        connection.execute(
            "INSERT INTO review_observation_verdicts (user_id, key, verdict, text, account_id, trading_day, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, key) DO UPDATE SET verdict = excluded.verdict,"
            " text = excluded.text, account_id = excluded.account_id, trading_day = excluded.trading_day,"
            " created_at = excluded.created_at",
            (user_id, key, verdict, text, account_id, trading_day, utc_now()))

    def observation_verdicts(self, user_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return [{"key": row["key"], "verdict": row["verdict"], "text": row["text"]} for row in self.connection.execute(
                "SELECT key, verdict, text FROM review_observation_verdicts WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,))]

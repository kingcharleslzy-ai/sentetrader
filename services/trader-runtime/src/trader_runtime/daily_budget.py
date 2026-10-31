"""Platform model-call allowances per Beijing day, counted in the shared state database.

The news screener, the title translator and the research desk each spend a daily allowance of
platform model calls. The count lives in one shared table, so a restart, a blue-green handoff or
a second process on the same database never hands the day a second full allowance. Without a
database (unit tests that build a service bare) the count stays in memory.
"""
from __future__ import annotations

import sqlite3
import threading


class DailyCallBudget:
    """One scope's calls per day: `take` spends one if the day still has any, atomically."""

    def __init__(self, scope: str, connection: sqlite3.Connection | None = None,
                 lock: threading.Lock | threading.RLock | None = None) -> None:
        self.scope = scope
        self.connection = connection if lock is not None else None
        self.lock = lock
        self._memory: tuple[str, int] = ("", 0)
        if self.connection is not None and self.lock is not None:
            with self.lock, self.connection:
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS model_call_days (scope TEXT NOT NULL, day TEXT NOT NULL,"
                    " calls INTEGER NOT NULL, PRIMARY KEY (scope, day))")

    def used(self, day: str) -> int:
        if self.connection is None or self.lock is None:
            return self._memory[1] if self._memory[0] == day else 0
        with self.lock:
            row = self.connection.execute("SELECT calls FROM model_call_days WHERE scope = ? AND day = ?",
                                          (self.scope, day)).fetchone()
        return int(row[0]) if row else 0

    def take(self, day: str, limit: int) -> bool:
        """Spend one call of `day` if fewer than `limit` were spent; one statement, so two processes
        racing for the last call cannot both get it."""
        if limit <= 0:
            return False
        if self.connection is None or self.lock is None:
            used = self.used(day)
            if used >= limit:
                return False
            self._memory = (day, used + 1)
            return True
        with self.lock, self.connection:
            taken = self.connection.execute(
                "INSERT INTO model_call_days (scope, day, calls) VALUES (?, ?, 1)"
                " ON CONFLICT(scope, day) DO UPDATE SET calls = calls + 1 WHERE calls < ?",
                (self.scope, day, limit)).rowcount
            # Days before this one are only history; a week is plenty to read back.
            self.connection.execute("DELETE FROM model_call_days WHERE scope = ? AND day < date(?, '-7 day')",
                                    (self.scope, day))
        return taken == 1

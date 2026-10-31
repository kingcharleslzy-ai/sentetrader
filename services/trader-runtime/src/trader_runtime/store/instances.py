"""The single-row runtime lease: which process may run the ordering loops."""
from __future__ import annotations

from typing import Any

from .base import StoreBase


class InstanceLeaseStore(StoreBase):
    """The `runtime_lease` singleton row; `BEGIN IMMEDIATE` makes acquisition atomic
    across processes, so two runtimes on one state database cannot both win."""

    def try_acquire_lease(
        self, *, instance_id: str, host: str, pid: int, build_sha: str | None,
        now: str, stale_before: str,
    ) -> bool:
        """Insert when free, refresh our own heartbeat, or take over a stale holder.

        `now` and `stale_before` are `utc_now()`-format ISO strings; identical
        formats compare correctly as plain text.
        """
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT instance_id, heartbeat_at FROM runtime_lease WHERE id = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO runtime_lease"
                    " (id, instance_id, host, pid, build_sha, acquired_at, heartbeat_at)"
                    " VALUES (1, ?, ?, ?, ?, ?, ?)",
                    (instance_id, host, pid, build_sha, now, now),
                )
                return True
            if row["instance_id"] == instance_id:
                connection.execute(
                    "UPDATE runtime_lease SET host = ?, pid = ?, build_sha = ?, heartbeat_at = ?"
                    " WHERE id = 1",
                    (host, pid, build_sha, now),
                )
                return True
            if row["heartbeat_at"] < stale_before:
                connection.execute(
                    "UPDATE runtime_lease SET instance_id = ?, host = ?, pid = ?, build_sha = ?,"
                    " acquired_at = ?, heartbeat_at = ? WHERE id = 1",
                    (instance_id, host, pid, build_sha, now, now),
                )
                return True
            return False

    def heartbeat_lease(self, instance_id: str, now: str) -> bool:
        """Refresh the heartbeat; False means another instance has taken over."""
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE runtime_lease SET heartbeat_at = ? WHERE id = 1 AND instance_id = ?",
                (now, instance_id),
            )
            return cursor.rowcount == 1

    def live_lease_holder(self, *, stale_before: str) -> dict[str, Any] | None:
        """The current holder when its heartbeat is still fresh — the same
        staleness verdict `try_acquire_lease` applies, without acquiring."""
        with self.lock:
            row = self.connection.execute(
                "SELECT instance_id, host, pid, build_sha, acquired_at, heartbeat_at"
                " FROM runtime_lease WHERE id = 1"
            ).fetchone()
        if row is None or row["heartbeat_at"] < stale_before:
            return None
        return dict(row)

    def release_lease(self, instance_id: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM runtime_lease WHERE id = 1 AND instance_id = ?", (instance_id,),
            )

    def read_lease(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT instance_id, host, pid, build_sha, acquired_at, heartbeat_at"
                " FROM runtime_lease WHERE id = 1"
            ).fetchone()
        return dict(row) if row is not None else None

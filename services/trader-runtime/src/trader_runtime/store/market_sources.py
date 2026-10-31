"""公共行情源清单 (2026-10-04): the ordered relay accounts public kline channels
allocate over. The table is authoritative once seeded from the environment (first
boot with a non-empty list) or written by the admin PUT; no restart is needed to
change it."""
from __future__ import annotations

from collections.abc import Iterable

from .base import StoreBase, utc_now


class MarketSourceStore(StoreBase):
    """Read the ordered list, seed it once from the environment, replace it whole."""

    def list_market_sources(self) -> list[str]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT account_id FROM market_sources ORDER BY position, account_id"
            ).fetchall()
        return [str(row["account_id"]) for row in rows]

    def market_sources_seeded(self) -> bool:
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM market_sources_seeded WHERE id = 1"
            ).fetchone()
        return row is not None

    def seed_market_sources(self, account_ids: Iterable[str]) -> None:
        """The one-time environment bootstrap: rows plus the seeded marker, so a
        later empty-list decision by the admin is never overwritten on restart."""
        ids = [str(item).strip() for item in account_ids if str(item).strip()]
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM market_sources")
            self.connection.executemany(
                "INSERT INTO market_sources (account_id, position, added_at) VALUES (?, ?, ?)",
                [(account_id, position, now) for position, account_id in enumerate(ids)],
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO market_sources_seeded (id, seeded_at) VALUES (1, ?)",
                (now,),
            )

    def set_market_sources(self, account_ids: Iterable[str]) -> None:
        """Replace the whole ordered list (admin PUT); also marks the table seeded
        so the environment never seeds over it again."""
        self.seed_market_sources(account_ids)

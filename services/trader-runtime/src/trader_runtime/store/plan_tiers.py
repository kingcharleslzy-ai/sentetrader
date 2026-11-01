from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from ..plans import TIER_SEED_META
from ..skill_registry import seed_tier_configs
from .base import StoreBase, utc_now

log = logging.getLogger("uvicorn.error.agent")


class PlanTierStore(StoreBase):
    """The `plan_tiers` rows: every tier fact is read from here per request, so
    an owner's edit takes effect on the next call without a restart."""

    # `config` JSON: {models: list[str]|null, dailyTokens: int|null,
    # voiceSecondsPerDay: int|null, briefProducts: int|null, skills: list[str]}
    # — `Plan.config()` is the only writer, `plan_from_config` the only reader.
    _TIER_COLUMNS = "name, label, config, is_default, sort_order, created_at, updated_at"

    @staticmethod
    def _tier_row(row: Any) -> dict[str, Any]:
        return {
            "name": row[0], "label": row[1], "config": json.loads(row[2]),
            "isDefault": bool(row[3]), "sortOrder": row[4],
            "createdAt": row[5], "updatedAt": row[6],
        }

    def list_plan_tiers(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                f"SELECT {self._TIER_COLUMNS} FROM plan_tiers ORDER BY sort_order, created_at"
            ).fetchall()
        return [self._tier_row(row) for row in rows]

    def get_plan_tier(self, name: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                f"SELECT {self._TIER_COLUMNS} FROM plan_tiers WHERE name = ?", (name,),
            ).fetchone()
        return self._tier_row(row) if row is not None else None

    def get_default_plan_tier(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                f"SELECT {self._TIER_COLUMNS} FROM plan_tiers WHERE is_default = 1"
            ).fetchone()
        return self._tier_row(row) if row is not None else None

    def default_plan_name(self) -> str | None:
        """The tier new users land on; `None` only for a database whose tier
        table is somehow empty (first startup seeds it, so callers treat it as
        a broken-state fallback, not an expected value)."""
        with self.lock:
            row = self.connection.execute(
                "SELECT name FROM plan_tiers WHERE is_default = 1"
            ).fetchone()
        return str(row[0]) if row is not None else None

    def count_users_by_plan(self) -> dict[str, int]:
        """Cloud users per tier name — the admin page's user counts and the
        delete guard both read this."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT plan, COUNT(*) FROM cloud_users GROUP BY plan"
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def create_plan_tier(self, *, label: str, copy_of: dict[str, Any]) -> dict[str, Any]:
        """A new tier with an opaque name (`tier-<hex>`), `copy_of` being the
        source tier's config: names are primary keys shared with
        `cloud_users.plan` and invite rows, so a display-name edit never has to
        touch a user row."""
        now = utc_now()
        name = f"tier-{secrets.token_hex(3)}"
        with self.lock, self.connection:
            sort_order = self.connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM plan_tiers"
            ).fetchone()[0]
            self.connection.execute(
                "INSERT INTO plan_tiers (name, label, config, is_default, sort_order, created_at, updated_at)"
                " VALUES (?, ?, ?, 0, ?, ?, ?)",
                (name, label, json.dumps(copy_of, ensure_ascii=False), sort_order, now, now),
            )
        tier = self.get_plan_tier(name)
        assert tier is not None
        return tier

    def update_plan_tier(
        self, name: str, *, label: str | None = None, config: dict[str, Any] | None = None,
        is_default: bool | None = None,
    ) -> dict[str, Any] | None:
        """Partial update; setting the default clears the previous one in the
        same transaction (exactly one default tier at any moment)."""
        with self.lock, self.connection:
            if self.connection.execute(
                "SELECT 1 FROM plan_tiers WHERE name = ?", (name,),
            ).fetchone() is None:
                return None
            if label is not None:
                self.connection.execute(
                    "UPDATE plan_tiers SET label = ?, updated_at = ? WHERE name = ?",
                    (label, utc_now(), name),
                )
            if config is not None:
                self.connection.execute(
                    "UPDATE plan_tiers SET config = ?, updated_at = ? WHERE name = ?",
                    (json.dumps(config, ensure_ascii=False), utc_now(), name),
                )
            if is_default:
                self.connection.execute("UPDATE plan_tiers SET is_default = 0")
                self.connection.execute(
                    "UPDATE plan_tiers SET is_default = 1, updated_at = ? WHERE name = ?",
                    (utc_now(), name),
                )
        return self.get_plan_tier(name)

    def delete_plan_tier(self, name: str) -> bool:
        """The DELETE itself refuses a tier a cloud user still carries: the
        route's count check above only explains *why* (plan_in_use vs
        plan_is_default), the statement is the atomic guard against a user
        landing on the tier between the check and the delete."""
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "DELETE FROM plan_tiers WHERE name = ? AND is_default = 0"
                " AND NOT EXISTS (SELECT 1 FROM cloud_users WHERE plan = ?)",
                (name, name),
            )
        return cursor.rowcount > 0

    def seed_plan_tiers(self) -> None:
        """Insert the three built-in tiers once, only into an empty table: an
        owner's edits and deletions are data, never reset by a restart. The
        tier's skill list comes from the registry (`ai-trader-default-tiers`),
        so a SKILL.md edit is the single place to change what a fresh install
        opens."""
        with self.lock, self.connection:
            if self.connection.execute("SELECT 1 FROM plan_tiers LIMIT 1").fetchone() is not None:
                return
            now = datetime.now(UTC).isoformat()
            skills_by_tier = seed_tier_configs()
            self.connection.executemany(
                "INSERT INTO plan_tiers (name, label, config, is_default, sort_order, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(tier_name, label,
                  json.dumps({**meta, "skills": skills_by_tier.get(tier_name, [])},
                             ensure_ascii=False),
                  1 if is_default else 0, sort_order, now, now)
                 for tier_name, label, is_default, sort_order, meta in TIER_SEED_META],
            )

from __future__ import annotations

from typing import Any

from .base import StoreBase, utc_now


class SkillSettingsStore(StoreBase):
    """The `skill_settings` rows: the owner's global on/off switch per skill.
    An absent row means enabled — the registry's default — so the table starts
    empty and a fresh skill needs no row. The switch binds everyone, the owner
    included (their plan is unlimited, not exempt)."""

    def skill_enabled(self, skill_id: str) -> bool:
        """`False` only for a row the owner explicitly disabled."""
        with self.lock:
            row = self.connection.execute(
                "SELECT enabled FROM skill_settings WHERE skill_id = ?", (skill_id,)
            ).fetchone()
        return row is None or bool(row["enabled"])

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> dict[str, Any]:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO skill_settings (skill_id, enabled, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(skill_id) DO UPDATE SET enabled = excluded.enabled,"
                " updated_at = excluded.updated_at",
                (skill_id, 1 if enabled else 0, utc_now()),
            )
        return {"skillId": skill_id, "enabled": enabled}

    def skill_settings_all(self) -> dict[str, bool]:
        """Only the rows that exist; `skill_enabled` fills the enabled default
        for everything else (the registry lists the full set)."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT skill_id, enabled FROM skill_settings"
            ).fetchall()
        return {str(row["skill_id"]): bool(row["enabled"]) for row in rows}

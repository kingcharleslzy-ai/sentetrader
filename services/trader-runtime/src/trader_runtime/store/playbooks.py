"""打法本 (owner 2026-10-25): the user's own setups — a name and their entry / exit / stop rules in words,
optionally linked to one of their strategies — and the tag a card carries when an order is placed under one,
so the trade review knows which setup each round trip was opened under. Every row is scoped to its user."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from .base import StoreBase, utc_now


def playbook_view(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "entryRules": row["entry_rules"], "exitRules": row["exit_rules"],
            "stopRules": row["stop_rules"], "strategyId": row["strategy_id"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"]}


class PlaybookStore(StoreBase):
    def list_playbooks(self, *, user_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM playbooks WHERE user_id = ? ORDER BY created_at, id", (user_id,)).fetchall()
        return [playbook_view(row) for row in rows]

    def get_playbook(self, playbook_id: str, *, user_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM playbooks WHERE id = ? AND user_id = ?", (playbook_id, user_id)).fetchone()
        return playbook_view(row) if row is not None else None

    def create_playbook(self, *, user_id: str, fields: dict[str, Any], max_count: int) -> dict[str, Any] | str:
        """Insert one setup. Returns its view, or the Chinese refusal (the per-user cap, a name already
        used) — checked and written in one locked transaction."""
        playbook_id, now = uuid4().hex, utc_now()
        with self.lock, self.connection:
            held = self.connection.execute("SELECT COUNT(*) FROM playbooks WHERE user_id = ?", (user_id,)).fetchone()[0]
            if held >= max_count:
                return f"交易笔记最多 {max_count} 条，先删掉一条再加"
            if self.connection.execute("SELECT 1 FROM playbooks WHERE user_id = ? AND name = ?",
                                       (user_id, fields["name"])).fetchone():
                return f"已经有叫“{fields['name']}”的交易笔记了"
            self.connection.execute(
                """INSERT INTO playbooks (id, user_id, name, entry_rules, exit_rules, stop_rules, strategy_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (playbook_id, user_id, fields["name"], fields["entryRules"], fields["exitRules"], fields["stopRules"],
                 fields.get("strategyId"), now, now))
            row = self.connection.execute("SELECT * FROM playbooks WHERE id = ?", (playbook_id,)).fetchone()
        return playbook_view(row)

    def update_playbook(self, playbook_id: str, *, user_id: str, fields: dict[str, Any]) -> dict[str, Any] | str | None:
        """Replace one setup's fields. None = not the user's; a string = the Chinese refusal (name taken)."""
        with self.lock, self.connection:
            if self.connection.execute("SELECT 1 FROM playbooks WHERE id = ? AND user_id = ?",
                                       (playbook_id, user_id)).fetchone() is None:
                return None
            if self.connection.execute("SELECT 1 FROM playbooks WHERE user_id = ? AND name = ? AND id != ?",
                                       (user_id, fields["name"], playbook_id)).fetchone():
                return f"已经有叫“{fields['name']}”的交易笔记了"
            self.connection.execute(
                """UPDATE playbooks SET name = ?, entry_rules = ?, exit_rules = ?, stop_rules = ?, strategy_id = ?, updated_at = ?
                WHERE id = ? AND user_id = ?""",
                (fields["name"], fields["entryRules"], fields["exitRules"], fields["stopRules"], fields.get("strategyId"),
                 utc_now(), playbook_id, user_id))
            row = self.connection.execute("SELECT * FROM playbooks WHERE id = ?", (playbook_id,)).fetchone()
        return playbook_view(row)

    def delete_playbook(self, playbook_id: str, *, user_id: str) -> bool:
        """Remove the setup. Trades already tagged with it keep the tag row but no longer name a setup."""
        with self.lock, self.connection:
            cursor = self.connection.execute("DELETE FROM playbooks WHERE id = ? AND user_id = ?", (playbook_id, user_id))
        return cursor.rowcount > 0

    def set_round_playbook(self, round_id: str, playbook_id: str, *, user_id: str, trading_account_id: str) -> None:
        """A closed round trip tagged afterwards in the review (overrides the card's tag)."""
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO round_playbooks (trading_account_id, round_id, playbook_id, user_id, updated_at)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(trading_account_id, round_id) DO UPDATE SET
                playbook_id = excluded.playbook_id, user_id = excluded.user_id, updated_at = excluded.updated_at""",
                (trading_account_id, round_id, playbook_id, user_id, utc_now()))

    def clear_round_playbook(self, round_id: str, *, trading_account_id: str) -> bool:
        with self.lock, self.connection:
            cursor = self.connection.execute("DELETE FROM round_playbooks WHERE trading_account_id = ? AND round_id = ?",
                                             (trading_account_id, round_id))
        return cursor.rowcount > 0

    def list_round_playbooks(self, *, trading_account_id: str, user_id: str) -> dict[str, dict[str, str]]:
        """round id → the setup it was tagged with afterwards; a deleted setup names none."""
        with self.lock:
            rows = self.connection.execute(
                """SELECT r.round_id, p.id, p.name FROM round_playbooks r
                JOIN playbooks p ON p.id = r.playbook_id AND p.user_id = ?
                WHERE r.trading_account_id = ?""", (user_id, trading_account_id)).fetchall()
        return {row["round_id"]: {"playbookId": row["id"], "playbookName": row["name"]} for row in rows}

    def tag_proposal_playbook(self, proposal_id: str, playbook_id: str, *, user_id: str) -> None:
        """The card an order is placed from carries the setup it was placed under (the ticket's choice)."""
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO proposal_playbooks (proposal_id, playbook_id, user_id, created_at) VALUES (?, ?, ?, ?)",
                (proposal_id, playbook_id, user_id, utc_now()))

from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from .base import StoreBase, utc_now


class ProposalStore(StoreBase):
    """Risk policies, artifacts, trade proposals, their audit trail and idempotency keys."""

    def active_risk_policy(
        self, *, user_id: str, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        row = (connection or self.connection).execute(
            """SELECT versions.* FROM active_risk_policies active
            JOIN risk_policy_versions versions ON versions.id = active.policy_id
            WHERE active.user_id = ?""",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "policyId": row["id"], "userId": row["user_id"],
            "version": row["version"], "rules": json.loads(row["rules_json"]),
            "rulesDigest": row["rules_digest"],
            "sourceProposalId": row["source_proposal_id"],
            "createdAt": row["created_at"],
        }

    def apply_risk_policy(
        self, *, user_id: str, expected_version: int, proposal_id: str,
        rules: dict[str, Any], rules_digest: str, connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        current = self.active_risk_policy(user_id=user_id, connection=connection)
        current_version = int(current["version"]) if current else 0
        if current_version != expected_version:
            raise ValueError("risk_policy_version_conflict")
        policy_id, version, now = str(uuid4()), current_version + 1, utc_now()
        connection.execute(
            """INSERT INTO risk_policy_versions
            (id, user_id, version, rules_json, rules_digest, source_proposal_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (policy_id, user_id, version, json.dumps(rules, ensure_ascii=False, sort_keys=True),
             rules_digest, proposal_id, now),
        )
        connection.execute(
            """INSERT INTO active_risk_policies (user_id, policy_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET policy_id = excluded.policy_id,
            updated_at = excluded.updated_at""",
            (user_id, policy_id, now),
        )
        return {
            "policyId": policy_id, "version": version, "rules": rules,
            "rulesDigest": rules_digest, "createdAt": now,
        }

    def create(self, *, proposal_id: str, session_id: str, proposal_type: str, status: str,
               payload: dict[str, Any], user_id: str = "local-user", version: int = 1,
               approval_id: str | None = None, approval_expires_at: str | None = None,
               connection: sqlite3.Connection | None = None,
               trading_account_id: str | None = None) -> dict[str, Any]:
        now = utc_now()
        statement = ("""INSERT INTO proposals
                (id, session_id, proposal_type, status, payload_json, created_at, updated_at,
                 user_id, version, approval_id, approval_expires_at, approval_used_at, trading_account_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                     (proposal_id, session_id, proposal_type, status, json.dumps(payload, ensure_ascii=False),
                      now, now, user_id, version, approval_id, approval_expires_at,
                      trading_account_id or self.default_trading_account_id))
        if connection is not None:  # inside the caller's transaction
            connection.execute(*statement)
            return self.get(proposal_id, connection=connection)
        with self.lock, self.connection:
            self.connection.execute(*statement)
        return self.get(proposal_id)

    def _proposal_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "sessionId": row["session_id"], "userId": row["user_id"],
                "proposalType": row["proposal_type"], "status": row["status"], "version": row["version"],
                "approvalId": row["approval_id"], "approvalExpiresAt": row["approval_expires_at"],
                "approvalUsedAt": row["approval_used_at"], "payload": json.loads(row["payload_json"]),
                "createdAt": row["created_at"], "updatedAt": row["updated_at"],
                "tradingAccountId": row["trading_account_id"]}

    def get(self, proposal_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        row = (connection or self.connection).execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        return self._proposal_from_row(row)

    def get_many(self, proposal_ids: list[str], *, user_id: str) -> list[dict[str, Any]]:
        """The user's proposals among `proposal_ids` (any order); ids that are not theirs or do not exist are left out."""
        if not proposal_ids:
            return []
        marks = ",".join("?" * len(proposal_ids))
        rows = self.connection.execute(
            f"SELECT * FROM proposals WHERE id IN ({marks}) AND user_id = ?", (*proposal_ids, user_id)).fetchall()
        return [self._proposal_from_row(row) for row in rows]

    def pending_proposals(self, session_id: str) -> list[dict[str, Any]]:
        """Cards still waiting for the user in this chat session, newest first."""
        rows = self.connection.execute(
            "SELECT * FROM proposals WHERE session_id = ? AND status = 'approval_required' ORDER BY created_at DESC",
            (session_id,)).fetchall()
        return [self._proposal_from_row(row) for row in rows]

    def due_approvals(self, now: str) -> list[dict[str, Any]]:
        """Cards still marked awaiting confirmation whose approval window has closed (ISO UTC strings)."""
        rows = self.connection.execute(
            "SELECT * FROM proposals WHERE status = 'approval_required' AND approval_expires_at IS NOT NULL"
            " AND approval_expires_at <= ? ORDER BY created_at", (now,)).fetchall()
        return [self._proposal_from_row(row) for row in rows]

    def recent_proposals(self, session_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """The latest proposal cards of a chat session in any status, newest first."""
        rows = self.connection.execute(
            "SELECT * FROM proposals WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit)).fetchall()
        return [self._proposal_from_row(row) for row in rows]

    def mark_voice_confirm(self, proposal_id: str, confirm_by: str | None) -> dict[str, Any]:
        """The spoken confirmation window shown on the card (ISO time, None clears it); nothing else changes."""
        with self.lock, self.connection:
            record = self.get(proposal_id)
            payload = dict(record["payload"])
            if confirm_by is None:
                payload.pop("voiceConfirmBy", None)
            else:
                payload["voiceConfirmBy"] = confirm_by
            self.connection.execute("UPDATE proposals SET payload_json = ?, updated_at = ? WHERE id = ?",
                                    (json.dumps(payload, ensure_ascii=False), utc_now(), proposal_id))
        return self.get(proposal_id)

    def link_proposals(self, proposal_ids: list[str]) -> None:
        """本站模拟两本账的全平（2026-10-21）：人民币账一张、美元账一张平仓卡，互相记下对方（`linkedProposalIds`），
        确认或放弃其中一张时另一张跟着走（见 routes/execution.py）。只写这一个字段。"""
        with self.lock, self.connection:
            for proposal_id in proposal_ids:
                record = self.get(proposal_id)
                payload = {**record["payload"], "linkedProposalIds": [other for other in proposal_ids if other != proposal_id]}
                self.connection.execute("UPDATE proposals SET payload_json = ?, updated_at = ? WHERE id = ?",
                                        (json.dumps(payload, ensure_ascii=False), utc_now(), proposal_id))

    def refuse_pending(self, proposal_id: str, *, expected_version: int, payload: dict[str, Any],
                       approval_id: str | None) -> bool:
        """条件写回 (#700 review): a confirm-time refusal only lands on a card that is still
        awaiting confirmation, at the very version and approval the refuser read. A card
        another confirmation already took (terminal status, spent approval, moved version)
        is left exactly as that winner wrote it — no overwrite, no stale republish; the
        caller answers through its own conflict / idempotent read instead."""
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE proposals SET payload_json = ?, updated_at = ?
                   WHERE id = ? AND status = 'approval_required' AND version = ?
                   AND approval_used_at IS NULL AND approval_id IS ?""",
                (json.dumps(payload, ensure_ascii=False), utc_now(), proposal_id,
                 expected_version, approval_id))
        return cursor.rowcount == 1

    def update(self, proposal_id: str, *, status: str, payload: dict[str, Any], version: int | None = None,
               approval_id: str | None = None, approval_expires_at: str | None = None,
               approval_used_at: str | None = None, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        database = connection or self.connection
        values = (status, json.dumps(payload, ensure_ascii=False), utc_now(), version,
                  approval_id, approval_expires_at, approval_used_at, proposal_id)
        cursor = database.execute(
            """UPDATE proposals SET status = ?, payload_json = ?, updated_at = ?,
               version = COALESCE(?, version), approval_id = ?, approval_expires_at = ?, approval_used_at = ?
               WHERE id = ?""", values)
        if connection is None:
            database.commit()
        if cursor.rowcount != 1:
            raise KeyError(proposal_id)
        return self.get(proposal_id, connection=database)

    def audit(self, proposal_id: str, action: str, result: dict[str, Any], *, connection: sqlite3.Connection | None = None) -> str:
        audit_id = str(uuid4())
        database = connection or self.connection
        database.execute("INSERT INTO proposal_audit VALUES (?, ?, ?, ?, ?)",
                         (audit_id, proposal_id, action, json.dumps(result, ensure_ascii=False), utc_now()))
        if connection is None:
            database.commit()
        return audit_id

    def get_idempotent(self, *, user_id: str, key: str, action: str, proposal_id: str,
                       connection: sqlite3.Connection) -> dict[str, Any] | None:
        row = connection.execute("SELECT * FROM idempotency_keys WHERE user_id = ? AND key = ?", (user_id, key)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["proposal_id"] != proposal_id:
            raise ValueError("idempotency_key_conflict")
        return json.loads(row["response_json"])

    def save_idempotent(self, *, user_id: str, key: str, action: str, proposal_id: str,
                        response: dict[str, Any], connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO idempotency_keys VALUES (?, ?, ?, ?, ?, ?)",
                           (user_id, key, action, proposal_id, json.dumps(response, ensure_ascii=False), utc_now()))


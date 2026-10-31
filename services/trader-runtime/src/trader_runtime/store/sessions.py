from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .base import StoreBase, log, utc_after, utc_now

# A chat's last user turn: a text or delegated-voice task (agent run) or the user's own message, which
# quick voice turns write without a run. Reminders and other pushes are not the user's and don't count.
_LAST_RUN = "(SELECT MAX(r.created_at) FROM agent_runs r WHERE r.session_id = s.id)"
_LAST_SAID = ("(SELECT MAX(e.timestamp) FROM events e WHERE e.session_id = s.id AND e.name = 'message.completed'"
              " AND json_extract(e.payload_json, '$.role') = 'user')")
# SQLite's two-argument MAX is NULL when either side is; NULL here means no user turn at all.
_LAST_USER_TURN = f"MAX(COALESCE({_LAST_RUN}, {_LAST_SAID}), COALESCE({_LAST_SAID}, {_LAST_RUN}))"


def _with_card_account(payload: Any, account_id: str | None) -> Any:
    if account_id and isinstance(payload, dict) and not payload.get("accountId"):
        return {**payload, "accountId": account_id}
    return payload


class SessionStore(StoreBase):
    """Chat sessions, their events/outbox and runs, and session lifecycle cleanup."""

    def create_session(
        self, *, user_id: str, mode: str, agent_id: str, ttl_seconds: int = 86400,
        trading_account_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = str(uuid4())
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO sessions
                (id, user_id, mode, agent_id, status, created_at, updated_at, expires_at, codex_thread_id,
                 trading_account_id)
                VALUES (?, ?, ?, ?, 'active', ?, ?, ?, NULL, ?)""",
                (session_id, user_id, mode, agent_id, now, now, utc_after(ttl_seconds),
                 trading_account_id or self.default_trading_account_id),
            )
        return self.get_session(session_id, user_id=user_id)

    def get_session(self, session_id: str, *, user_id: str | None = None) -> dict[str, Any]:
        query = "SELECT * FROM sessions WHERE id = ?"
        values: tuple[Any, ...] = (session_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            values += (user_id,)
        row = self.connection.execute(query, values).fetchone()
        if row is None:
            raise KeyError(session_id)
        return {
            "sessionId": row["id"], "userId": row["user_id"], "mode": row["mode"],
            # Sessions written before 1.5.0 carry the Codex agent id; they are the native agent's now.
            "agentId": "native" if row["agent_id"] == "codex" else row["agent_id"], "status": row["status"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            "expiresAt": row["expires_at"],
            "codexThreadId": row["codex_thread_id"],
            "codexToolProtocolVersion": row["codex_tool_protocol_version"],
            "tradingAccountId": row["trading_account_id"],
            "accountRevision": int(row["account_revision"] or 0),
        }

    def owner_call_facts(
        self, *, token_hash: str, username: str | None, session_id: str, user_id: str,
    ) -> dict[str, Any]:
        """One read for the per-event identity check of an owner-password stream:
        whether the token is revoked, the cloud user record (as `get_cloud_user`)
        and the session's status/expiry (the row `get_session` filters on)."""
        with self.lock:
            row = self.connection.execute(
                "SELECT EXISTS(SELECT 1 FROM owner_session_revocations WHERE token_hash = ?),"
                " u.username, u.password_hash, u.role, u.created_at, u.invited_by, u.plan, u.disabled,"
                " s.id, s.status, s.expires_at"
                " FROM (SELECT 1)"
                " LEFT JOIN cloud_users u ON u.username = ?"
                " LEFT JOIN sessions s ON s.id = ? AND s.user_id = ?",
                (token_hash, username, session_id, user_id),
            ).fetchone()
        return {
            "revoked": bool(row[0]),
            "user": None if row[1] is None else {
                "username": row[1], "passwordHash": row[2], "role": row[3],
                "createdAt": row[4], "invitedBy": row[5], "plan": row[6],
                "disabled": bool(row[7]),
            },
            "session": None if row[8] is None else {"status": row[9], "expiresAt": row[10]},
        }

    def resume_session(self, session_id: str, *, user_id: str) -> dict[str, Any]:
        session = self.get_session(session_id, user_id=user_id)
        if session["status"] != "active" or session["expiresAt"] <= utc_now():
            raise KeyError(session_id)
        return session

    def set_session_trading_account(self, session_id: str, account_id: str, *, user_id: str) -> tuple[int, str]:
        """Bind an existing session to another granted account (4.4a); feed and
        agent turns follow the session row from the next read on. A real move
        advances the session's account revision (unconditional: the unbind
        fallback and switches that name no revision). Returns (the revision this
        write left, the account the row had before it) — both from inside its own
        write transaction, so a caller decides "did the account change" from what
        this write replaced, never from an earlier read (#932 follow-up)."""
        with self.transaction() as connection:
            before = connection.execute(
                "SELECT trading_account_id FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)).fetchone()
            if before is None:
                raise KeyError(session_id)
            connection.execute(
                """UPDATE sessions SET account_revision = account_revision + (trading_account_id != ?),
                trading_account_id = ?, updated_at = ? WHERE id = ? AND user_id = ?""",
                (account_id, account_id, utc_now(), session_id, user_id),
            )
            row = connection.execute(
                "SELECT account_revision FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)).fetchone()
        return int(row["account_revision"]), str(before["trading_account_id"])

    def session_account_state(self, session_id: str, *, user_id: str | None = None) -> tuple[int, str]:
        """(how many times the session's account was switched — the row's own counter, shared by every Runtime slot —
        and the account it is on), read together in one query."""
        query = "SELECT account_revision, trading_account_id FROM sessions WHERE id = ?"
        values: tuple[Any, ...] = (session_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            values += (user_id,)
        with self.lock:
            row = self.connection.execute(query, values).fetchone()
        if row is None:
            raise KeyError(session_id)
        return int(row["account_revision"] or 0), str(row["trading_account_id"])

    def switch_session_account(self, session_id: str, account_id: str, *, user_id: str,
                               expected: int) -> tuple[bool, int, str, str]:
        """#932 follow-up: the switch that names the revision it was decided on — one conditional UPDATE inside one
        write transaction, atomic across Runtime slots on the same database: applied (and the revision advanced, a
        repeat of the same account too) only while the row still has `expected`. Returns (applied, the revision now,
        the account now, the account before this write)."""
        with self.transaction() as connection:
            before = connection.execute(
                "SELECT trading_account_id FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)).fetchone()
            if before is None:
                raise KeyError(session_id)
            cursor = connection.execute(
                """UPDATE sessions SET trading_account_id = ?, account_revision = account_revision + 1, updated_at = ?
                WHERE id = ? AND user_id = ? AND account_revision = ?""",
                (account_id, utc_now(), session_id, user_id, expected),
            )
            row = connection.execute(
                "SELECT account_revision, trading_account_id FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id)).fetchone()
        return (cursor.rowcount == 1, int(row["account_revision"]), str(row["trading_account_id"]),
                str(before["trading_account_id"]))

    def user_sessions_on_account(self, *, user_id: str, trading_account_id: str) -> list[str]:
        """The user's active sessions bound to one account — the unbind fallback
        (2026-10-03) repoints exactly these onto the paper account."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT id FROM sessions WHERE user_id = ? AND trading_account_id = ? AND status = 'active'",
                (user_id, trading_account_id),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def list_chat_sessions(self, *, user_id: str, mode: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                """SELECT id, created_at FROM sessions WHERE user_id = ? AND mode = ?
                AND agent_id IN ('native', 'codex') AND status = 'active' AND expires_at > ?
                AND expires_at = '9999-12-31T00:00:00+00:00'
                ORDER BY created_at DESC LIMIT 50""", (user_id, mode, utc_now()),
            ).fetchall()
        return [{"sessionId": row["id"], "createdAt": row["created_at"]} for row in rows]

    def recent_chat_session(
        self, *, user_id: str, mode: str | None, trading_account_id: str | None = None,
    ) -> dict[str, Any] | None:
        """The user's most recently active live chat (an expired one never: #523, a voice call fell back on it
        and wrote into it); `mode` None = any mode, and a trading account narrows it to chats acting on that
        account (a strategy's reminders stay on its account)."""
        row = self.connection.execute(
            f"""SELECT s.id FROM sessions s WHERE s.user_id = ? AND (? IS NULL OR s.mode = ?)
            AND (? IS NULL OR s.trading_account_id = ?)
            AND s.agent_id IN ('native', 'codex') AND s.status = 'active' AND s.expires_at > ?
            ORDER BY {_LAST_USER_TURN} IS NOT NULL DESC, COALESCE({_LAST_USER_TURN}, s.created_at) DESC LIMIT 1""",
            (user_id, mode, mode, trading_account_id, trading_account_id, utc_now()),
        ).fetchone()
        return self.get_session(row["id"], user_id=user_id) if row else None

    def chat_sessions_by_recency(
        self, *, user_id: str, mode: str | None = None, trading_account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """The user's chat sessions, most recently active first (the same ordering as
        `recent_chat_session`), each with the account it acts on — the input for picking the
        conversation a reminder lands in (2026-10-08: the one the user is looking at, not
        merely the newest)."""
        rows = self.connection.execute(
            f"""SELECT s.id, s.trading_account_id FROM sessions s WHERE s.user_id = ?
            AND (? IS NULL OR s.mode = ?)
            AND (? IS NULL OR s.trading_account_id = ?)
            AND s.agent_id IN ('native', 'codex') AND s.status = 'active' AND s.expires_at > ?
            ORDER BY {_LAST_USER_TURN} IS NOT NULL DESC, COALESCE({_LAST_USER_TURN}, s.created_at) DESC
            LIMIT 50""",
            (user_id, mode, mode, trading_account_id, trading_account_id, utc_now()),
        ).fetchall()
        return [{"sessionId": str(row["id"]), "accountId": row["trading_account_id"]} for row in rows]

    def retain_chat_session(self, session_id: str, *, user_id: str) -> None:
        # Chat retention is independent of authorization/order expiry. Explicit
        # deletion still goes through SessionLifecycleManager and revokes work.
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE sessions SET expires_at = '9999-12-31T00:00:00+00:00' WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            )

    def chat_retention_users(self) -> list[str]:
        return [row["user_id"] for row in self.connection.execute(
            "SELECT DISTINCT user_id FROM sessions WHERE expires_at = '9999-12-31T00:00:00+00:00'")]

    def stale_chat_sessions(self, user_id: str, days: int, *, session_id: str | None = None) -> list[str]:
        """One user's retained chat sessions whose last user turn (or creation) is older than their
        `data.sessionRetentionDays`; `session_id` re-checks just that one right before it is released."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self.connection.execute(
            f"""SELECT s.id FROM sessions s WHERE s.user_id = ? AND s.expires_at = '9999-12-31T00:00:00+00:00'
            AND (? IS NULL OR s.id = ?) AND COALESCE({_LAST_USER_TURN}, s.created_at) < ? ORDER BY s.created_at""",
            (user_id, session_id, session_id, cutoff)).fetchall()
        return [row["id"] for row in rows]

    def active_sessions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id, user_id FROM sessions WHERE status = 'active' AND expires_at > ? ORDER BY created_at",
            (utc_now(),),
        ).fetchall()
        return [self.get_session(row["id"], user_id=row["user_id"]) for row in rows]

    def _append_event(self, connection: sqlite3.Connection, session_id: str, event: dict[str, Any]) -> int:
        cursor = connection.execute(
                """INSERT INTO events
                (id, session_id, schema_version, kind, name, timestamp, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event["id"], session_id, event["schemaVersion"], event["kind"], event["name"],
                 event["timestamp"], json.dumps(event["payload"], ensure_ascii=False)),
            )
        sequence = int(cursor.lastrowid or 0)
        # Cards (artifacts) are kept, so the result that settles one is kept with it (#932 P2: 77 old order cards
        # whose proposal.result was trimmed read 等待确认 again on every reload).
        connection.execute(
            """DELETE FROM events WHERE session_id = ?
            AND name NOT IN ('message.completed', 'task.completed', 'proposal.result')
            AND NOT (name = 'error' AND (json_extract(payload_json, '$.runId') IS NOT NULL
                     OR json_extract(payload_json, '$.turnId') IS NOT NULL))
            AND kind NOT IN ('artifact', 'chart', 'approval') AND sequence NOT IN
            (SELECT sequence FROM events WHERE session_id = ? ORDER BY sequence DESC LIMIT ?)""",
            (session_id, session_id, self.max_events_per_session),
        )
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (utc_now(), session_id)
        )
        if event["kind"] in {"artifact", "chart", "diff"}:
            if event["name"] in {"agent.diff", "codex.diff"}:
                connection.execute(
                    """DELETE FROM artifacts
                    WHERE session_id = ? AND artifact_type = ?
                    AND json_extract(payload_json, '$.taskId') = ?
                    AND json_extract(payload_json, '$.itemId') = ?""",
                    (session_id, event["name"], event["payload"].get("taskId"),
                     event["payload"].get("itemId")),
                )
            connection.execute(
                "INSERT INTO artifacts VALUES (?, ?, NULL, ?, ?, ?)",
                (str(uuid4()), session_id, event["name"],
                 json.dumps(event["payload"], ensure_ascii=False), utc_now()),
            )
        return sequence

    def append_event(self, session_id: str, event: dict[str, Any]) -> int:
        with self.transaction() as connection:
            return self._append_event(connection, session_id, event)

    def stage_outbox_event(
        self, session_id: str, event: dict[str, Any], *, connection: sqlite3.Connection | None = None
    ) -> None:
        # Only for a session that still exists: a producer finishing after the session was deleted
        # (a voice result, a background task) drops its event instead of queueing an orphan.
        values = (event["id"], session_id, json.dumps(event, ensure_ascii=False), utc_now(), session_id)
        statement = ("INSERT OR IGNORE INTO outbox_events SELECT ?, ?, ?, ?, NULL"
                     " WHERE EXISTS (SELECT 1 FROM sessions WHERE id = ?)")
        if connection is not None:
            connection.execute(statement, values)
            return
        with self.lock, self.connection:
            self.connection.execute(statement, values)

    def deliver_outbox(self, *, limit: int = 100) -> set[str]:
        delivered_sessions: set[str] = set()
        with self.transaction() as connection:
            # Remove rows written by versions that stamped delivered_at instead of pruning.
            connection.execute("DELETE FROM outbox_events WHERE delivered_at IS NOT NULL")
            rows = connection.execute(
                "SELECT * FROM outbox_events WHERE delivered_at IS NULL ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                if connection.execute("SELECT 1 FROM sessions WHERE id = ?", (row["session_id"],)).fetchone() is None:
                    # Left by an older version for a deleted session: it can never land, and must not
                    # roll back every other session's delivery with it.
                    log.warning("outbox_orphan_dropped event=%s session=%s", row["id"], row["session_id"])
                    connection.execute("DELETE FROM outbox_events WHERE id = ?", (row["id"],))
                    continue
                event = json.loads(row["event_json"])
                existing = connection.execute(
                    "SELECT 1 FROM events WHERE id = ?", (event["id"],)
                ).fetchone()
                if existing is None:
                    self._append_event(connection, row["session_id"], event)
                connection.execute("DELETE FROM outbox_events WHERE id = ?", (row["id"],))
                delivered_sessions.add(row["session_id"])
        return delivered_sessions

    def list_events(self, session_id: str, *, after_sequence: int = 0) -> list[tuple[int, dict[str, Any]]]:
        # An order card names the trading account its proposal row belongs to (#1005): the card reads its
        # order status from that account, never from whichever account the window is on now. Read from the
        # proposal record, so a card published before the field existed carries it too.
        rows = self.connection.execute(
            """SELECT e.*, CASE WHEN e.name IN ('proposal.trade', 'proposal.close_batch', 'proposal.spread')
                   THEN (SELECT p.trading_account_id FROM proposals p
                         WHERE p.id = json_extract(e.payload_json, '$.proposalId')) END AS card_account
               FROM events e WHERE e.session_id = ? AND e.sequence > ? ORDER BY e.sequence""",
            (session_id, after_sequence),
        ).fetchall()
        return [(int(row["sequence"]), {
            "schemaVersion": row["schema_version"], "id": row["id"],
            "sessionId": row["session_id"], "kind": row["kind"], "name": row["name"],
            "timestamp": row["timestamp"], "payload": _with_card_account(json.loads(row["payload_json"]), row["card_account"]),
        }) for row in rows]

    def create_run(
        self, *, session_id: str, user_id: str, instruction: str,
        model_slug: str | None = None,
    ) -> dict[str, Any]:
        run_id, now = str(uuid4()), utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO agent_runs"
                " (id, session_id, user_id, status, instruction, result_json, error, created_at, updated_at, model_slug)"
                " VALUES (?, ?, ?, 'queued', ?, NULL, NULL, ?, ?, ?)",
                (run_id, session_id, user_id, instruction, now, now, model_slug),
            )
        return self.get_run(run_id, user_id=user_id)

    def get_run(self, run_id: str, *, user_id: str | None = None) -> dict[str, Any]:
        query = "SELECT * FROM agent_runs WHERE id = ?"
        values: tuple[str, ...] = (run_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            values += (user_id,)
        row = self.connection.execute(query, values).fetchone()
        if row is None:
            raise KeyError(run_id)
        return {"runId": row["id"], "sessionId": row["session_id"], "status": row["status"],
                "instruction": row["instruction"], "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "error": row["error"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
                "userId": row["user_id"], "modelSlug": row["model_slug"]}

    def run_user_id(self, run_id: str) -> str:
        row = self.connection.execute(
            "SELECT user_id FROM agent_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return str(row["user_id"])

    def update_run(
        self, run_id: str, *, status: str, result: dict[str, Any] | None = None,
        error: str | None = None, expected_statuses: set[str] | None = None,
    ) -> dict[str, Any]:
        where = "id = ?"
        values: list[Any] = [status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                             error, utc_now(), run_id]
        if expected_statuses:
            placeholders = ",".join("?" for _ in expected_statuses)
            where += f" AND status IN ({placeholders})"
            values.extend(sorted(expected_statuses))
        with self.lock, self.connection:
            cursor = self.connection.execute(
                f"UPDATE agent_runs SET status = ?, result_json = ?, error = ?, updated_at = ? WHERE {where}",
                values,
            )
        if cursor.rowcount != 1 and expected_statuses is None:
            raise KeyError(run_id)
        return self.get_run(run_id)

    def recover_incomplete_runs(self, *, exclude: set[str] | frozenset[str] = frozenset()) -> list[dict[str, str]]:
        """End every queued/running run as runtime_restarted, except `exclude` — the runs the
        recovering process itself accepted (a slot that took the lease mid-deploy, 525-A)."""
        with self.lock, self.connection:
            rows = [row for row in self.connection.execute(
                "SELECT id, session_id FROM agent_runs WHERE status IN ('queued', 'running')"
            ).fetchall() if row["id"] not in exclude]
            now = utc_now()
            for row in rows:
                self.connection.execute(
                    "UPDATE agent_runs SET status = 'failed', error = 'runtime_restarted', updated_at = ?"
                    " WHERE id = ? AND status IN ('queued', 'running')", (now, row["id"]))
        return [{"runId": row["id"], "sessionId": row["session_id"]} for row in rows]

    def runs_for_session(
        self, session_id: str, *, statuses: set[str] | None = None
    ) -> list[dict[str, Any]]:
        query, values = "SELECT id FROM agent_runs WHERE session_id = ?", [session_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            values.extend(sorted(statuses))
        rows = self.connection.execute(query, values).fetchall()
        return [self.get_run(row["id"]) for row in rows]

    def expired_session_ids(self) -> list[str]:
        return [row["id"] for row in self.connection.execute(
            "SELECT id FROM sessions WHERE expires_at <= ?", (utc_now(),)
        ).fetchall()]

    def delete_session(self, session_id: str, *, stale_before: str | None = None) -> bool:
        """Delete one session and its cascades. `stale_before` (#515 F-c) is the retention sweep's
        second look: the delete only lands while the session's last user turn (or creation) is still
        older than the cutoff — a message that arrived while the release was in flight keeps the
        conversation alive instead of being deleted out from under its new run."""
        guard = ""
        values: tuple[str, ...] = (session_id,)
        if stale_before is not None:
            guard = f" AND COALESCE({_LAST_USER_TURN}, s.created_at) < ?"
            values = (session_id, stale_before)
        with self.transaction() as connection:
            connection.execute("DELETE FROM outbox_events WHERE session_id = ?", (session_id,))
            cursor = connection.execute(f"DELETE FROM sessions AS s WHERE s.id = ?{guard}", values)
        return cursor.rowcount == 1

    def reject_pending_proposals_for_session(self, session_id: str) -> None:
        with self.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM proposals WHERE session_id = ?
                AND status IN ('draft', 'risk_blocked', 'approval_required')""",
                (session_id,),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                payload["status"] = "rejected"
                connection.execute(
                    """UPDATE proposals SET status = 'rejected', payload_json = ?,
                    approval_used_at = ?, updated_at = ? WHERE id = ?""",
                    (json.dumps(payload, ensure_ascii=False), utc_now(), utc_now(), row["id"]),
                )
                connection.execute(
                    "INSERT INTO proposal_audit VALUES (?, ?, 'session_released', ?, ?)",
                    (str(uuid4()), row["id"], json.dumps({"status": "rejected"}), utc_now()),
                )

    def cleanup_expired_sessions(self) -> int:
        expired = self.expired_session_ids()
        if not expired:
            return 0
        # Session events and in-flight work are transient. Proposal, approval and
        # idempotency audit records intentionally outlive the conversation TTL.
        for session_id in expired:
            self.delete_session(session_id)
        return len(expired)

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..model_provider import canonical_model_slug
from .base import StoreBase, utc_now
from .trading_accounts import scoped_owner_user_id


class AccountStore(StoreBase):
    """Cloud users, invite codes, model preference and token usage."""

    # PlanTierStore, composed in `RuntimeStore`: the tier a first login lands on.
    default_plan_name: Callable[[], str | None]

    def get_owner_password_hash(self) -> str | None:
        """管理员在设置里改过的登录密码；没改过为 None（用部署配置里的）。"""
        row = self.connection.execute("SELECT password_hash FROM owner_password WHERE id = 1").fetchone()
        return str(row[0]) if row is not None else None

    def set_owner_password_hash(self, password_hash: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO owner_password (id, password_hash, updated_at) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET password_hash = excluded.password_hash, updated_at = excluded.updated_at",
                (password_hash, utc_now()))

    def set_cloud_user_password_hash(self, username: str, password_hash: str) -> bool:
        """改一个成员的登录密码（本人修改或管理员重置）；没有这个成员为 False。"""
        with self.transaction() as connection:
            changed = connection.execute(
                "UPDATE cloud_users SET password_hash = ? WHERE username = ?", (password_hash, username)).rowcount
        return bool(changed)

    def get_cloud_user(self, username: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT username, password_hash, role, created_at, invited_by, plan, disabled"
            " FROM cloud_users WHERE username = ?", (username,),
        ).fetchone()
        if row is None:
            return None
        return {
            "username": row[0], "passwordHash": row[1], "role": row[2],
            "createdAt": row[3], "invitedBy": row[4], "plan": row[5],
            "disabled": bool(row[6]),
        }

    def list_cloud_users(self) -> list[dict[str, Any]]:
        """Every cloud user with their tier and disable flag, oldest first."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT username, role, created_at, invited_by, plan, disabled"
                " FROM cloud_users ORDER BY created_at"
            ).fetchall()
        return [
            {"username": row[0], "role": row[1], "createdAt": row[2],
             "invitedBy": row[3], "plan": row[4], "disabled": bool(row[5])}
            for row in rows
        ]

    def get_user_plan(self, user_id: str) -> str | None:
        """The tier name a scoped user id runs on, or None when it names no
        `cloud_users` row (the owner, the local-token user, a cloud-jwt
        subject — `plans.plan_for_user` decides what a miss means).
        `cloud_users` is keyed by username, so the scan compares the same
        `owner-<sha256>` id the sessions carry; user counts stay small."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT username, plan FROM cloud_users"
            ).fetchall()
        for row in rows:
            if scoped_owner_user_id(str(row[0])) == user_id:
                return str(row[1])
        return None

    def set_user_plan(self, username: str, plan: str) -> bool:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE cloud_users SET plan = ? WHERE username = ?", (plan, username),
            )
        return cursor.rowcount > 0

    def set_user_disabled(self, username: str, disabled: bool) -> bool:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE cloud_users SET disabled = ? WHERE username = ?",
                (1 if disabled else 0, username),
            )
        return cursor.rowcount > 0

    def last_session_activity(self, user_id: str) -> str | None:
        """The newest `sessions.updated_at` of a user — the admin table's
        fallback "last seen" when the in-memory presence has no heartbeat."""
        with self.lock:
            row = self.connection.execute(
                "SELECT updated_at FROM sessions WHERE user_id = ?"
                " ORDER BY updated_at DESC LIMIT 1", (user_id,),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def create_cloud_user(
        self, *, username: str, password_hash: str, invited_by: str | None,
        plan: str | None = None,
    ) -> None:
        with self.lock, self.connection:
            self._insert_cloud_user(
                self.connection, username=username, password_hash=password_hash,
                invited_by=invited_by, plan=plan,
            )

    def register_invited_user(
        self, *, code_hash: str, username: str, password_hash: str, invited_by: str,
        landing_plan: Callable[[str], str | None],
    ) -> str | None:
        """Spend one use of an invite code and create the user it admits in one
        transaction (#932 X4): either both land or neither does, so a failed
        user insert (a name taken by a concurrent registration) leaves the code
        unspent, and two people racing on one one-shot code cannot both pass —
        `BEGIN IMMEDIATE` serialises the check-and-spend. `landing_plan` maps
        the code's tier to the tier the user lands on. Returns the code's tier
        (`""` for a tier-less code), or None when the code was refused and
        nothing was written; a failed insert raises `sqlite3.IntegrityError`."""
        with self.transaction() as connection:
            invite_plan = self._spend_invite_code(connection, code_hash, username)
            if invite_plan is None:
                return None
            self._insert_cloud_user(
                connection, username=username, password_hash=password_hash,
                invited_by=invited_by, plan=landing_plan(invite_plan) or None,
            )
        return invite_plan

    def _insert_cloud_user(
        self, connection: sqlite3.Connection, *, username: str, password_hash: str,
        invited_by: str | None, plan: str | None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        # A tier-carrying invite lands the user on that tier; every other first
        # login (and every pre-existing row) starts on the default tier.
        tier = plan if plan is not None else (self.default_plan_name() or "basic")
        connection.execute(
            "INSERT INTO cloud_users (username, password_hash, role, created_at, invited_by, plan)"
            " VALUES (?, ?, 'user', ?, ?, ?)",
            (username, password_hash, now, invited_by, tier),
        )
        # Registration creates no grant: a new user owns no trading account
        # until they bind their own TqKq account or the owner declares a
        # grant for them in the accounts file. The pre-isolation behaviour
        # of handing every registration operator on the owner's default
        # account was removed with the 2026-09-23 isolation fix.

    def create_invite_code(
        self, *, code_hash: str, created_by: str, expires_at: datetime,
        plan: str | None = None, code: str | None = None,
        unlimited: bool = False, never_expires: bool = False,
    ) -> bool:
        """Insert one code; False when its hash already exists (a custom code
        someone picked before). A never-expiring code stores its creation time
        in `expires_at` — the column stays NOT NULL, reads judge by the flag."""
        now = datetime.now(UTC)
        with self.lock, self.connection:
            if self.connection.execute(
                "SELECT 1 FROM invite_codes WHERE code_hash = ?", (code_hash,),
            ).fetchone() is not None:
                return False
            self.connection.execute(
                "INSERT INTO invite_codes"
                " (code_hash, created_by, created_at, expires_at, plan, code, unlimited, never_expires)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (code_hash, created_by, now.isoformat(),
                 now.isoformat() if never_expires else expires_at.isoformat(),
                 plan, code, int(unlimited), int(never_expires)),
            )
            return True

    def _spend_invite_code(self, connection: sqlite3.Connection, code_hash: str, used_by: str) -> str | None:
        """Spend one use of a code inside the caller's transaction and report
        the tier it carries: the tier name, `""` for a tier-less code (land on
        the default tier), `None` when the code was refused (unknown, disabled,
        expired, one-shot already spent) and must not create a user."""
        row = connection.execute(
            "SELECT expires_at, plan, unlimited, use_count, never_expires, disabled_at"
            " FROM invite_codes WHERE code_hash = ?",
            (code_hash,),
        ).fetchone()
        if row is None or row["disabled_at"] is not None:
            return None
        if not row["never_expires"] and datetime.now(UTC) >= datetime.fromisoformat(row["expires_at"]):
            return None
        if not row["unlimited"] and row["use_count"] >= 1:
            return None
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "UPDATE invite_codes SET use_count = use_count + 1 WHERE code_hash = ?",
            (code_hash,),
        )
        connection.execute(
            "INSERT INTO invite_code_uses (code_hash, username, used_at) VALUES (?, ?, ?)",
            (code_hash, used_by, now),
        )
        if not row["unlimited"]:
            # One-shot codes keep the legacy used_by/used_at fields in step.
            connection.execute(
                "UPDATE invite_codes SET used_by = ?, used_at = ? WHERE code_hash = ?",
                (used_by, now, code_hash),
            )
        return str(row["plan"]) if row["plan"] is not None else ""

    def invite_code_row(self, code_hash: str) -> dict[str, Any] | None:
        """One code's list-shaped row, or None when the hash is unknown. Used
        by the disable route (idempotent) and available for direct reads."""
        rows = self._invite_code_items([code_hash])
        return rows[0] if rows else None

    def disable_invite_code(self, code_hash: str) -> dict[str, Any] | None:
        """Stamp a code disabled; an already-disabled code returns its row
        unchanged, an unknown hash returns None."""
        with self.lock, self.connection:
            if self.connection.execute(
                "SELECT 1 FROM invite_codes WHERE code_hash = ?", (code_hash,),
            ).fetchone() is None:
                return None
            self.connection.execute(
                "UPDATE invite_codes SET disabled_at = ? WHERE code_hash = ? AND disabled_at IS NULL",
                (datetime.now(UTC).isoformat(), code_hash),
            )
        return self.invite_code_row(code_hash)

    def list_invite_codes(self) -> list[dict[str, Any]]:
        return self._invite_code_items(limit=200)

    def _invite_code_items(self, code_hashes: list[str] | None = None, *, limit: int = 1) -> list[dict[str, Any]]:
        """The owner's code list: the code text (null for rows that predate it),
        both use semantics, the newest users, and a server-computed status —
        disabled beats expired beats used up beats active."""
        if code_hashes is not None:
            if not code_hashes:
                return []
            marks = ",".join("?" for _ in code_hashes)
            rows = self.connection.execute(
                f"SELECT code_hash, code, created_by, created_at, expires_at, plan,"
                " unlimited, use_count, never_expires, disabled_at FROM invite_codes"
                f" WHERE code_hash IN ({marks})",
                code_hashes,
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT code_hash, code, created_by, created_at, expires_at, plan,"
                " unlimited, use_count, never_expires, disabled_at FROM invite_codes"
                " ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        uses_by_code: dict[str, list[dict[str, str]]] = {}
        for use in self.connection.execute(
            "SELECT code_hash, username, used_at FROM invite_code_uses ORDER BY used_at DESC"
        ).fetchall():
            uses_by_code.setdefault(use["code_hash"], []).append(
                {"username": str(use["username"]), "usedAt": str(use["used_at"])})
        now = datetime.now(UTC)
        items: list[dict[str, Any]] = []
        for row in rows:
            never_expires = bool(row["never_expires"])
            unlimited = bool(row["unlimited"])
            used_by = uses_by_code.get(str(row["code_hash"]), [])[:20]
            if row["disabled_at"] is not None:
                status = "disabled"
            elif not never_expires and now >= datetime.fromisoformat(str(row["expires_at"])):
                status = "expired"
            elif not unlimited and row["use_count"] >= 1:
                status = "used_up"
            else:
                status = "active"
            items.append({
                "id": str(row["code_hash"]),
                "code": row["code"],
                "uses": "unlimited" if unlimited else "once",
                "useCount": int(row["use_count"]),
                "usedBy": used_by,
                "plan": row["plan"],
                "createdBy": str(row["created_by"]),
                "createdAt": str(row["created_at"]),
                "expiresAt": None if never_expires else str(row["expires_at"]),
                "disabledAt": row["disabled_at"],
                "status": status,
            })
        if code_hashes is None:
            return items
        return sorted(items, key=lambda item: item["createdAt"], reverse=True)

    def get_model_pref(self, user_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT model_slug FROM user_model_prefs WHERE user_id = ?", (user_id,),
        ).fetchone()
        return canonical_model_slug(row[0]) if row else None

    def set_model_pref(self, user_id: str, model_slug: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO user_model_prefs (user_id, model_slug) VALUES (?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET model_slug = excluded.model_slug",
                (user_id, model_slug),
            )

    def add_token_usage(self, user_id: str, day: str, tokens: int) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO user_token_usage (user_id, day, tokens) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id, day) DO UPDATE SET tokens = tokens + excluded.tokens",
                (user_id, day, tokens),
            )

    def token_usage(self, user_id: str, day: str) -> int:
        row = self.connection.execute(
            "SELECT tokens FROM user_token_usage WHERE user_id = ? AND day = ?",
            (user_id, day),
        ).fetchone()
        return int(row[0]) if row else 0

    def token_usage_totals(self, day_prefix: str) -> dict[str, int]:
        """Tokens summed over every day starting with `day_prefix` (the admin
        table's "today"/"this month" reads), keyed by scoped user id."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT user_id, SUM(tokens) FROM user_token_usage"
                " WHERE day LIKE ? GROUP BY user_id",
                (day_prefix + "%",),
            ).fetchall()
        return {str(row[0]): int(row[1] or 0) for row in rows}

    TURN_USAGE_KEEP_DAYS = 30
    TURN_USAGE_FIELDS = ("requests", "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")

    def add_turn_usage(self, *, run_id: str, user_id: str, session_id: str, route: str, origin: str | None,
                       model: str | None, status: str, usage: dict[str, int], first_text_ms: int | None,
                       elapsed_ms: int) -> None:
        """One AI turn's token split (input / output / provider cache hits / reasoning) and its request
        count. The daily quota still counts in user_token_usage; this is the per-turn measurement."""
        now = datetime.now(UTC)
        cutoff = (now - timedelta(days=self.TURN_USAGE_KEEP_DAYS)).date().isoformat()
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO agent_turn_usage (run_id, user_id, session_id, day, finished_at, route,"
                " origin, model, status, requests, input_tokens, output_tokens, cached_tokens, reasoning_tokens,"
                " first_text_ms, elapsed_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, user_id, session_id, now.date().isoformat(), now.isoformat(), route, origin, model,
                 status, *(int(usage.get(key) or 0) for key in self.TURN_USAGE_FIELDS),
                 first_text_ms, int(elapsed_ms)),
            )
            self.connection.execute("DELETE FROM agent_turn_usage WHERE day < ?", (cutoff,))

    def turn_usage(self, *, user_id: str | None = None, since: str | None = None,
                   limit: int = 500) -> list[dict[str, Any]]:
        """Per-turn rows, newest first: one user's (or everyone's) since an ISO timestamp (UTC)."""
        clauses: list[str] = []
        values: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            values.append(user_id)
        if since is not None:
            clauses.append("finished_at >= ?")
            values.append(since)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.lock:
            cursor = self.connection.execute(
                f"SELECT * FROM agent_turn_usage{where} ORDER BY finished_at DESC LIMIT ?", (*values, limit))
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def set_turn_status(self, run_id: str, status: str) -> None:
        """The row is written when the turn closes, before its answer is published: publishing that is
        then cancelled or fails corrects the recorded status (the run did not complete)."""
        with self.lock, self.connection:
            self.connection.execute("UPDATE agent_turn_usage SET status = ? WHERE run_id = ?", (status, run_id))

    def set_turn_cards(self, run_id: str, *, card_count: int, numeric_text: bool) -> None:
        """回答卡片化 (2026-10-14): the cards a turn showed and whether its answer stated figures, onto the
        turn's row — the answer is published right after the row is written."""
        with self.lock, self.connection:
            self.connection.execute("UPDATE agent_turn_usage SET card_count = ?, numeric_text = ? WHERE run_id = ?",
                                    (int(card_count), int(bool(numeric_text)), run_id))

    def card_coverage(self, *, user_id: str | None = None, since: str | None = None,
                      recent: int = 20) -> dict[str, Any]:
        """回答卡片化统计 (2026-10-14): of the finished turns since `since` (one user's or everyone's),
        how many answered with figures in the text, and how many of those showed no card at all — the
        gaps to cover next. Turns recorded before the counters existed (NULL) are left out."""
        clauses = ["numeric_text IS NOT NULL"]
        values: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            values.append(user_id)
        if since is not None:
            clauses.append("finished_at >= ?")
            values.append(since)
        where = " WHERE " + " AND ".join(clauses)
        with self.lock:
            total, numeric, carded, gaps = self.connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(numeric_text), 0),"
                " COALESCE(SUM(CASE WHEN numeric_text = 1 AND card_count > 0 THEN 1 ELSE 0 END), 0),"
                " COALESCE(SUM(CASE WHEN numeric_text = 1 AND COALESCE(card_count, 0) = 0 THEN 1 ELSE 0 END), 0)"
                f" FROM agent_turn_usage{where}", values).fetchone()
            by_route = [{"route": row[0], "numeric": row[1], "withoutCard": row[2]} for row in self.connection.execute(
                "SELECT route, SUM(numeric_text), SUM(CASE WHEN COALESCE(card_count, 0) = 0 THEN 1 ELSE 0 END)"
                f" FROM agent_turn_usage{where} AND numeric_text = 1 GROUP BY route ORDER BY 3 DESC", values)]
            samples = [{"runId": row[0], "sessionId": row[1], "route": row[2], "finishedAt": row[3]}
                       for row in self.connection.execute(
                           "SELECT run_id, session_id, route, finished_at FROM agent_turn_usage"
                           f"{where} AND numeric_text = 1 AND COALESCE(card_count, 0) = 0"
                           " ORDER BY finished_at DESC LIMIT ?", (*values, recent))]
        return {"turns": int(total), "numericTurns": int(numeric), "numericWithCard": int(carded),
                "numericWithoutCard": int(gaps),
                "cardRate": round(carded / numeric, 4) if numeric else None,
                "byRoute": by_route, "recentGaps": samples}

    def add_voice_seconds(self, user_id: str, day: str, seconds: int) -> None:
        if seconds <= 0:
            return
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO user_voice_usage (user_id, day, seconds) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id, day) DO UPDATE SET seconds = seconds + excluded.seconds",
                (user_id, day, int(seconds)),
            )

    def voice_seconds(self, user_id: str, day: str) -> int:
        row = self.connection.execute(
            "SELECT seconds FROM user_voice_usage WHERE user_id = ? AND day = ?",
            (user_id, day),
        ).fetchone()
        return int(row[0]) if row else 0

    def voice_seconds_totals(self, day_prefix: str) -> dict[str, int]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT user_id, SUM(seconds) FROM user_voice_usage"
                " WHERE day LIKE ? GROUP BY user_id",
                (day_prefix + "%",),
            ).fetchall()
        return {str(row[0]): int(row[1] or 0) for row in rows}

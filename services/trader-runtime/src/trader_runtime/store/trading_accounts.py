"""Trading accounts and per-user grants; 4.2 assembles one account set per boot."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from .base import StoreBase, utc_now

GRANT_ROLES = ("owner", "operator", "viewer")
# Every table whose `trading_account_id` column scopes rows to one account; the
# fingerprint rename rewrites them all so no row survives on the old id.
ACCOUNT_SCOPED_TABLES = (
    "sessions", "account_trades", "account_daily", "proposals", "strategy_drafts",
    "strategy_monitor_states", "strategy_execution_attempts", "tqkq_sim_order_links",
    "ai_execution_controls", "ai_execution_authorizations", "ai_stop_orders",
    "strategy_fill_events", "trade_notes", "target_position_plans",
    "card_protection_steps", "breakeven_notices", "strategy_drawdown_states",
)
# The same for tables whose account column is named `account_id` (526-D, #526): a table left out here
# keeps pointing at the old id (reminder dedupe stops, the daily review and the user's default account
# vanish for the renamed account).
ACCOUNT_ID_COLUMN_TABLES = (
    "reminder_notices", "daily_reviews", "review_observation_verdicts", "user_default_accounts",
    "reminders", "unfilled_watch_entries",
)


# 行情专用快期账户 (2026-10-12): an account the owner adds in 后台「行情源」 only to feed
# the public market relay. Its execution owner is a system id of its own — no person
# logs in as it, so it sits in nobody's account directory and no session can act on
# (or order through) it — and the per-user credential row hangs off that id, one
# system id per account, the same sealed-password storage a member's binding uses.
MARKET_SOURCE_OWNER_PREFIX = "market-source:"


def market_source_owner_id(account_id: str) -> str:
    """The system owner of one market-only account (also its credential row's key)."""
    return f"{MARKET_SOURCE_OWNER_PREFIX}{account_id}"


def is_market_source_owner(user_id: object) -> bool:
    return str(user_id or "").startswith(MARKET_SOURCE_OWNER_PREFIX)


def scoped_owner_user_id(username: str) -> str:
    """`owner-<sha256(username)>` — the same identity `OwnerIdentity` resolves.
    Kept local so the store layer never imports the auth layer."""
    return f"owner-{hashlib.sha256(username.encode()).hexdigest()}"


class TradingAccountStore(StoreBase):
    """The `trading_accounts` registry and its `account_grants`; every other
    account-scoped table references these ids by column value, not by foreign key."""

    def ensure_trading_account(
        self, account_id: str, *, execution_owner_user_id: str, provider_kind: str,
        display_name: str, sidecar_url: str | None, sidecar_token_file: str | None = None,
        grants: Iterable[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Create (or refresh) an account and its grants.

        The execution owner is decided once, on first creation; a later boot only
        refreshes how the account is reached and shown, never re-assigns it.
        `grants` is the accounts file's declared set: the account is reconciled
        to exactly the owner plus it — extra rows are deleted, differing roles
        rewritten — so the file stays the single source of truth. Without a
        declaration nothing is taken away: no user ever gains a role here
        implicitly; sharing the owner's account with a member is a declaration.
        """
        now = utc_now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM trading_accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO trading_accounts
                    (id, environment, provider_kind, display_name, sidecar_url, sidecar_token_file,
                     execution_owner_user_id, status, created_at, updated_at)
                    VALUES (?, 'simulation', ?, ?, ?, ?, ?, 'active', ?, ?)""",
                    (account_id, provider_kind, display_name, sidecar_url,
                     sidecar_token_file, execution_owner_user_id, now, now),
                )
            else:
                connection.execute(
                    """UPDATE trading_accounts
                    SET provider_kind = ?, display_name = ?, sidecar_url = ?,
                        sidecar_token_file = ?, status = 'active', updated_at = ?
                    WHERE id = ?""",
                    (provider_kind, display_name, sidecar_url, sidecar_token_file, now, account_id),
                )
            if grants is None:
                insert = [(execution_owner_user_id, "owner")]
                connection.executemany(
                    """INSERT OR IGNORE INTO account_grants (account_id, user_id, role, granted_by, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    [(account_id, user_id, role, execution_owner_user_id, now)
                     for user_id, role in insert],
                )
            else:
                declared = {(execution_owner_user_id, "owner"), *grants}
                connection.execute(
                    "DELETE FROM account_grants WHERE account_id = ?", (account_id,))
                connection.executemany(
                    """INSERT INTO account_grants (account_id, user_id, role, granted_by, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    [(account_id, user_id, role, execution_owner_user_id, now)
                     for user_id, role in sorted(declared)],
                )
        account = self.get_trading_account(account_id)
        assert account is not None
        return account

    def ensure_default_trading_account(
        self, *, execution_owner_user_id: str, provider_kind: str,
        display_name: str, sidecar_url: str | None,
    ) -> dict[str, Any]:
        """The one account a deployment without an accounts file runs."""
        return self.ensure_trading_account(
            self.default_trading_account_id, execution_owner_user_id=execution_owner_user_id,
            provider_kind=provider_kind, display_name=display_name,
            sidecar_url=sidecar_url, sidecar_token_file=None,
        )

    def purge_default_account_auto_grants(self, *, account_id: str | None = None) -> int:
        """The 2026-09-23 isolation migration: delete the operator rows the two
        removed auto-grant mechanisms wrote on the default account — every boot
        granted each cloud user operator (`granted_by` = the account's execution
        owner) and every password registration did the same (`granted_by` =
        `system` / `invite-code`). Rows a person granted on purpose carry other
        authors and survive; an accounts file that declares the account's grants
        rewrites the whole set anyway and must not run this. Idempotent: with the
        rows gone (or never written) the delete matches nothing."""
        target = account_id or self.default_trading_account_id
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """DELETE FROM account_grants
                WHERE account_id = ? AND role != 'owner'
                AND (granted_by IN (SELECT execution_owner_user_id FROM trading_accounts WHERE id = ?)
                     OR granted_by IN ('system', 'invite-code'))""",
                (target, target),
            )
        return int(cursor.rowcount or 0)

    def rename_trading_account(self, *, old_id: str, new_id: str) -> bool:
        """One transaction, whole-table rename of an account id (the 4.4a
        fingerprint rename off `tqkq-sim:default`).

        `False` when the old id no longer exists — an already-applied or
        never-existing rename is a no-op, so restarts are idempotent. Both ids
        existing is a hard conflict: two live accounts were never declared as
        one, and merging them silently is not something a rename may decide.
        """
        with self.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM trading_accounts WHERE id = ?", (old_id,)
            ).fetchone() is None:
                return False
            if connection.execute(
                "SELECT 1 FROM trading_accounts WHERE id = ?", (new_id,)
            ).fetchone() is not None:
                raise RuntimeError("account_rename_conflict")
            # `account_grants.account_id` is the one real foreign key; deferring
            # it for this transaction lets every row move with its account. The
            # terminal-proposal immutability trigger guards business mutations;
            # an id rewrite changes no proposal fact, so it steps aside for the
            # migration and comes back identical inside the same transaction.
            connection.execute("PRAGMA defer_foreign_keys = ON")
            connection.execute("DROP TRIGGER proposals_terminal_immutable")
            connection.execute(
                "UPDATE trading_accounts SET id = ?, updated_at = ? WHERE id = ?",
                (new_id, utc_now(), old_id),
            )
            connection.execute(
                "UPDATE account_grants SET account_id = ? WHERE account_id = ?", (new_id, old_id))
            for table in ACCOUNT_SCOPED_TABLES:
                connection.execute(
                    f"UPDATE {table} SET trading_account_id = ? WHERE trading_account_id = ?",
                    (new_id, old_id),
                )
            for table in ACCOUNT_ID_COLUMN_TABLES:
                connection.execute(
                    f"UPDATE {table} SET account_id = ? WHERE account_id = ?", (new_id, old_id))
            # One statement, not executescript: that would commit mid-transaction.
            connection.execute("""
                CREATE TRIGGER proposals_terminal_immutable
                BEFORE UPDATE ON proposals
                WHEN OLD.status IN ('candidate_recorded', 'risk_rules_applied', 'rejected', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'terminal_proposal_immutable');
                END
            """)
        return True

    def disable_trading_accounts_except(self, account_ids: list[str]) -> None:
        """Accounts dropped from the accounts file stop being assembled; their
        rows and history stay for audit. Pool-managed (`tqkq-pool`) and
        site-hosted paper accounts live outside the file — their lifecycle is
        bind/unbind and registration-time provisioning, never this."""
        keep = list(dict.fromkeys(account_ids))
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE trading_accounts SET status = 'disabled', updated_at = ?"
                " WHERE status = 'active' AND provider_kind NOT IN ('tqkq-pool', 'paper')"
                f" AND id NOT IN ({','.join('?' * len(keep))})",
                (utc_now(), *keep),
            )

    def disable_trading_account(self, account_id: str) -> None:
        """One account stops being assembled (unbind); history stays for audit."""
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE trading_accounts SET status = 'disabled', updated_at = ? WHERE id = ?",
                (utc_now(), account_id),
            )

    def list_account_grantees(self, *, account_id: str) -> list[str]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT user_id FROM account_grants WHERE account_id = ?", (account_id,)
            ).fetchall()
        return [str(row["user_id"]) for row in rows]

    def delete_account_grants(self, *, account_id: str) -> None:
        """Unbind: the account's grants leave with it, so a user's session
        resolution falls back to the default account instead of a dead id."""
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM account_grants WHERE account_id = ?", (account_id,)
            )

    def set_user_default_account(self, *, user_id: str, account_id: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO user_default_accounts (user_id, account_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                account_id = excluded.account_id, updated_at = excluded.updated_at""",
                (user_id, account_id, utc_now()),
            )

    def get_user_default_account(self, *, user_id: str) -> str | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT account_id FROM user_default_accounts WHERE user_id = ?", (user_id,)
            ).fetchone()
        return str(row["account_id"]) if row is not None else None

    def claim_paper_account(self, *, user_id: str, account_id: str) -> str:
        """The paper account id this user holds: `account_id` when this call
        claims first, otherwise the id an earlier claim holds. One row per user,
        written in one transaction, so concurrent provisioning from any
        connection or process agrees on a single id before the engine is asked."""
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO paper_account_claims (user_id, account_id, created_at)
                VALUES (?, ?, ?) ON CONFLICT(user_id) DO NOTHING""",
                (user_id, account_id, utc_now()),
            )
            row = connection.execute(
                "SELECT account_id FROM paper_account_claims WHERE user_id = ?", (user_id,)
            ).fetchone()
        return str(row["account_id"])

    def list_paper_account_claims(self) -> list[tuple[str, str]]:
        """Every (user, 本站模拟 account) claim — the 外盘二期 boot backfill opens each one's USD sibling."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT user_id, account_id FROM paper_account_claims ORDER BY created_at").fetchall()
        return [(str(row["user_id"]), str(row["account_id"])) for row in rows]

    def find_user_paper_account(self, *, user_id: str) -> dict[str, Any] | None:
        """The user's active paper account — the one site-hosted account each
        user holds (their claim). Registration, the 403 backfill and the unbind
        fallback all land here; `None` means this user has none (yet), or a
        claim whose provisioning has not finished."""
        with self.lock:
            row = self.connection.execute(
                """SELECT t.* FROM paper_account_claims c
                JOIN trading_accounts t ON t.id = c.account_id
                JOIN account_grants g ON g.account_id = t.id AND g.user_id = c.user_id
                WHERE c.user_id = ? AND t.provider_kind = 'paper' AND t.status = 'active'""",
                (user_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_busy_paper_accounts(self, *, provider_kind: str = "paper") -> list[str]:
        """Active paper accounts with monitoring strategies, armed stops or
        unfinished orders — the one verdict both the boot scan (re-assemble so
        their loops do not wait for the user's first request) and the idle scan
        (never detach) read; with `provider_kind="tqkq-pool"`, the 快期 accounts
        the trading retirement must leave alone (tqkq_retirement). An order is
        unfinished while its submit is not settled, a cancel is not, or the
        exchange last reported it resting
        (ALIVE, partly filled or not); a FINISHED one is history."""
        with self.lock:
            rows = self.connection.execute(
                """SELECT DISTINCT t.id FROM trading_accounts t
                WHERE t.provider_kind = ? AND t.status = 'active' AND (
                    EXISTS (SELECT 1 FROM strategy_drafts s
                            WHERE s.trading_account_id = t.id AND s.status = 'monitoring')
                    OR EXISTS (SELECT 1 FROM ai_stop_orders o
                               WHERE o.trading_account_id = t.id AND o.status IN ('ARMED', 'TRIGGERING'))
                    OR EXISTS (SELECT 1 FROM tqkq_sim_order_links l
                               WHERE l.trading_account_id = t.id
                               AND (l.submit_phase IN ('PREPARED', 'CALL_STARTED', 'UNKNOWN')
                                    OR l.cancel_phase IN ('CALL_STARTED', 'UNKNOWN')
                                    OR l.last_remote_status = 'ALIVE'))
                )""", (provider_kind,)
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def clear_user_default_account(self, *, user_id: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM user_default_accounts WHERE user_id = ?", (user_id,)
            )

    def save_tqkq_credentials(
        self, *, user_id: str, account_id: str, kq_username: str,
        ciphertext: bytes, nonce: bytes,
    ) -> None:
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO tqkq_credentials
                (user_id, account_id, kq_username, ciphertext, nonce, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                account_id = excluded.account_id, kq_username = excluded.kq_username,
                ciphertext = excluded.ciphertext, nonce = excluded.nonce,
                updated_at = excluded.updated_at""",
                (user_id, account_id, kq_username, ciphertext, nonce, now, now),
            )

    def load_tqkq_credentials(self, *, user_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT user_id, account_id, kq_username, ciphertext, nonce"
                " FROM tqkq_credentials WHERE user_id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def delete_tqkq_credentials(self, *, user_id: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM tqkq_credentials WHERE user_id = ?", (user_id,)
            )

    def get_trading_account(self, account_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM trading_accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_trading_accounts(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM trading_accounts ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_account_grants(self, *, user_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT account_id, user_id, role, granted_by, created_at"
                " FROM account_grants WHERE user_id = ? ORDER BY account_id",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_all_account_grants(self) -> list[dict[str, Any]]:
        """Every grant, account by account — the owner's read-only isolation
        check (`GET /api/admin/accounts`). Nothing here writes or decides."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT account_id, user_id, role, granted_by, created_at"
                " FROM account_grants ORDER BY account_id, user_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def grant_account(self, *, account_id: str, user_id: str, role: str, granted_by: str) -> None:
        if role not in GRANT_ROLES:
            raise ValueError("invalid_account_grant_role")
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO account_grants (account_id, user_id, role, granted_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id, user_id) DO UPDATE SET
                role = excluded.role, granted_by = excluded.granted_by""",
                (account_id, user_id, role, granted_by, utc_now()),
            )

    def account_role(self, *, account_id: str, user_id: str) -> str | None:
        """The caller's role on one account — the single source of truth for what a
        request may write. No grant row means no role: no user reaches an account
        implicitly, and the owner's default account is no exception. The
        single-tenant IdP fallback (the shared default account of local-token and
        cloud-jwt modes) is a resolution decision made by the caller, not a role."""
        with self.lock:
            row = self.connection.execute(
                "SELECT role FROM account_grants WHERE account_id = ? AND user_id = ?",
                (account_id, user_id),
            ).fetchone()
        return str(row["role"]) if row is not None else None

    def is_execution_owner(self, *, account_id: str, user_id: str) -> bool:
        """One account, one execution owner: the row's `execution_owner_user_id`."""
        with self.lock:
            row = self.connection.execute(
                "SELECT execution_owner_user_id FROM trading_accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
        return row is not None and row["execution_owner_user_id"] == user_id

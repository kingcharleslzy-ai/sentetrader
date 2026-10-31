"""目标持仓 plans: one row per (user, contract) attempt, from the first leg's card to the final
fill. The relay (order_watch) advances them; nothing here places orders.

The `current_*` columns name the leg in play right now — the first leg when the plan starts, the
second one after a reverse plan's first leg fills."""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from .base import StoreBase, utc_now

# A plan that still owes a card or a fill: a new plan for the same contract supersedes these.
ACTIVE_TARGET_PLAN_STATUSES = ("awaiting_confirm", "awaiting_fill")

_UNSET: Any = object()


def _row_to_plan(row: Any) -> dict[str, Any]:
    plan = dict(row)
    plan["leg"] = json.loads(plan["leg_json"])
    plan["currentClientOrderIds"] = json.loads(plan["current_client_order_ids"])
    return plan


class TargetPlanStore(StoreBase):
    def create_target_plan(
        self, *, user_id: str, trading_account_id: str, symbol: str, target_net: int,
        leg_json: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Insert a plan at awaiting_confirm and abandon any active plan for the same user and
        contract (the newest request replaces the old one). Returns (new plan, superseded plan or
        None) — the caller notes a superseded plan in the conversation. The plan keeps the
        catalog's spelling of the contract (its next leg's card and quote read use it as stored;
        10-13 production stored SHFE.RB2701 and that name broke the paper engine's market
        subscription); "the same contract" still matches case-insensitively, old rows included."""
        now = utc_now()
        with self.lock, self.connection:
            # Only this account's active plan is superseded (2026-10-04 #518): another
            # account's plan for the same contract is a different book, not a replacement.
            previous = self.connection.execute(
                "SELECT * FROM target_position_plans WHERE user_id = ? AND UPPER(symbol) = ?"
                " AND trading_account_id = ? AND status IN (?, ?) ORDER BY created_at DESC",
                (user_id, symbol.upper(), trading_account_id, *ACTIVE_TARGET_PLAN_STATUSES),
            ).fetchone()
            if previous is not None:
                self.connection.execute(
                    "UPDATE target_position_plans SET status = 'abandoned', updated_at = ?"
                    " WHERE id = ?",
                    (now, previous["id"]),
                )
            plan_id = str(uuid4())
            self.connection.execute(
                "INSERT INTO target_position_plans"
                " (id, user_id, trading_account_id, symbol, target_net, leg_index, leg_json,"
                "  current_client_order_ids, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 0, ?, '[]', 'awaiting_confirm', ?, ?)",
                (plan_id, user_id, trading_account_id, symbol, int(target_net),
                 json.dumps(leg_json, ensure_ascii=False), now, now),
            )
        plan = self.get_target_plan(plan_id, user_id=user_id)
        assert plan is not None  # inserted in the block above
        return (plan, _row_to_plan(previous) if previous is not None else None)

    def get_target_plan(self, plan_id: str, *, user_id: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM target_position_plans WHERE id = ?"
        values: tuple[Any, ...] = (plan_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            values += (user_id,)
        row = self.connection.execute(query, values).fetchone()
        return _row_to_plan(row) if row is not None else None

    def active_target_plan(self, *, user_id: str, symbol: str,
                           trading_account_id: str | None = None) -> dict[str, Any] | None:
        """The one plan still owing a card or a fill for this user and contract (the newest);
        the account defaults to the store's own when the caller does not say (2026-10-04 #518)."""
        account = trading_account_id or self.default_trading_account_id
        row = self.connection.execute(
            "SELECT * FROM target_position_plans WHERE user_id = ? AND UPPER(symbol) = ?"
            " AND trading_account_id = ? AND status IN (?, ?) ORDER BY created_at DESC LIMIT 1",
            (user_id, symbol.upper(), account, *ACTIVE_TARGET_PLAN_STATUSES),
        ).fetchone()
        return _row_to_plan(row) if row is not None else None

    def plan_by_proposal(self, proposal_id: str, *, user_id: str) -> dict[str, Any] | None:
        """The awaiting-confirmation plan whose current card is this proposal — the confirm route's
        link from a card the user just confirmed back to its plan."""
        row = self.connection.execute(
            "SELECT * FROM target_position_plans WHERE user_id = ? AND current_proposal_id = ?"
            " AND status = 'awaiting_confirm' LIMIT 1",
            (user_id, proposal_id),
        ).fetchone()
        return _row_to_plan(row) if row is not None else None

    def end_plan_of_card(self, proposal_id: str, *, connection: Any = None) -> bool:
        """The plan still waiting on this card ends with it (#932): a card given up (放弃 / 取消) or timed out takes
        its 目标持仓 plan along at once — not 30 minutes later by the stall sweep. Inside the caller's transaction
        when `connection` is given. True when a plan was ended."""
        statement = ("UPDATE target_position_plans SET status = 'abandoned', updated_at = ?"
                     " WHERE current_proposal_id = ? AND status = 'awaiting_confirm'")
        if connection is not None:
            return connection.execute(statement, (utc_now(), proposal_id)).rowcount > 0
        with self.lock, self.connection:
            return self.connection.execute(statement, (utc_now(), proposal_id)).rowcount > 0

    def awaiting_fill_plans(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM target_position_plans WHERE status = 'awaiting_fill'"
            " ORDER BY created_at ASC"
        ).fetchall()
        return [_row_to_plan(row) for row in rows]

    def update_target_plan(
        self, plan_id: str, *, status: str | None = None, leg_index: int | None = None,
        leg_json: dict[str, Any] | None = None, current_proposal_id: Any = _UNSET,
        current_client_order_ids: list[str] | None = None,
        expect: tuple[str, int] | None = None,
    ) -> dict[str, Any] | None:
        """Update any subset (a `current_proposal_id` of None clears the link — pass it explicitly;
        every other None means leave unchanged). `expect` is the relay's compare-and-set (#518):
        the row must still be at that (status, leg_index) when the lock is taken for the write to
        land, so a plan that was superseded or ended while the relay's order queries were in flight
        is never revived — None comes back and the caller drops the card it was about to issue.
        The check is read once up front (not appended per statement): this call itself writes the
        guarded columns, so a per-statement guard would trip on this call's own earlier writes."""
        now = utc_now()
        writes: list[tuple[str, tuple[Any, ...]]] = []
        if status is not None:
            writes.append(("UPDATE target_position_plans SET status = ?, updated_at = ? WHERE id = ?",
                           (status, now, plan_id)))
        if leg_index is not None:
            writes.append(("UPDATE target_position_plans SET leg_index = ?, updated_at = ? WHERE id = ?",
                           (leg_index, now, plan_id)))
        if leg_json is not None:
            writes.append(("UPDATE target_position_plans SET leg_json = ?, updated_at = ? WHERE id = ?",
                           (json.dumps(leg_json, ensure_ascii=False), now, plan_id)))
        if current_proposal_id is not _UNSET:
            writes.append(("UPDATE target_position_plans SET current_proposal_id = ?, updated_at = ? WHERE id = ?",
                           (current_proposal_id, now, plan_id)))
        if current_client_order_ids is not None:
            writes.append(("UPDATE target_position_plans SET current_client_order_ids = ?, updated_at = ? WHERE id = ?",
                           (json.dumps(current_client_order_ids), now, plan_id)))
        with self.lock, self.connection:
            if expect is not None:
                current = self.connection.execute(
                    "SELECT status, leg_index FROM target_position_plans WHERE id = ?", (plan_id,)
                ).fetchone()
                if current is None or (current["status"], int(current["leg_index"])) != expect:
                    return None
            for statement, values in writes:
                self.connection.execute(statement, values)
        row = self.connection.execute(
            "SELECT * FROM target_position_plans WHERE id = ?", (plan_id,)).fetchone()
        return _row_to_plan(row) if row is not None else None

    def stale_awaiting_plans(self, *, older_than_iso: str) -> list[dict[str, Any]]:
        """Plans still awaiting a confirmation or a fill past the timeout window; the relay sweep
        abandons them with one honest conversation line."""
        rows = self.connection.execute(
            "SELECT * FROM target_position_plans WHERE status IN (?, ?) AND updated_at < ?",
            (*ACTIVE_TARGET_PLAN_STATUSES, older_than_iso),
        ).fetchall()
        return [_row_to_plan(row) for row in rows]

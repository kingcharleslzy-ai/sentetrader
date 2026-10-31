from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from .broker import EventBroker
from .runs import RunManager
from .store import RuntimeStore


class SessionLifecycleManager:
    def __init__(
        self, *, store: RuntimeStore, broker: EventBroker, runs: RunManager,
        adapter: Any, interval_seconds: float = 30.0,
    ) -> None:
        self.store, self.broker, self.runs, self.adapter = store, broker, runs, adapter
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self.replay_router: Any | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._closing = False
            await self.cleanup_expired()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def release(self, session_id: str, *, stale_before: str | None = None) -> bool:
        # #515 F-d: the whole release window — run teardown through the row delete — answers a
        # run enqueued under it with session_releasing (the routes say 409); unmark even on failure.
        self.runs.mark_releasing(session_id)
        try:
            await self.runs.release_session(session_id)
            release_adapter = getattr(self.adapter, "release_session", None)
            if release_adapter is not None:
                await release_adapter(session_id)
            await self.broker.release_session(session_id)
            self.store.reject_pending_proposals_for_session(session_id)
            return self.store.delete_session(session_id, stale_before=stale_before)
        finally:
            self.runs.unmark_releasing(session_id)

    async def cleanup_expired(self) -> int:
        if self.replay_router is not None:
            await self.replay_router.cleanup_expired()
        count = 0
        for session_id in self.store.expired_session_ids():
            count += int(await self.release(session_id))
        # Retained chats live until the user's `data.sessionRetentionDays` of inactivity has passed.
        for user_id in self.store.chat_retention_users():
            days = self.store.user_preferences(user_id).data.sessionRetentionDays
            for session_id in self.store.stale_chat_sessions(user_id, days):
                # Releasing the previous ones awaited: a turn may have landed here since the scan.
                if self.store.stale_chat_sessions(user_id, days, session_id=session_id):
                    # #515 F-c: the release itself awaits too — the delete re-checks the cutoff
                    # (recomputed now) so a message that landed mid-release keeps the session.
                    count += int(await self.release(
                        session_id,
                        stale_before=(datetime.now(UTC) - timedelta(days=days)).isoformat()))
        return count

    async def expire_approvals(self) -> int:
        """确认卡超时：确认窗口已过、仍标着“等待确认”的卡按“取消”同样的方式作废（状态 rejected，同一条
        proposal.result 事件），这样卡片、AI 看到的“待确认的卡”和迟到的确认说的是同一件事。"""
        from .proposals import APPROVAL_EXPIRED_TEXT, PLAN_ENDED_TEXT
        count = 0
        for due in self.store.due_approvals(datetime.now(UTC).isoformat()):
            with self.store.transaction() as connection:
                record = self.store.get(due["id"], connection=connection)
                if record["status"] != "approval_required":
                    continue
                payload = record["payload"]
                audit_id = self.store.audit(record["id"], "expire", {"status": "rejected", "reason": "approval_expired"},
                                            connection=connection)
                payload.update({"status": "rejected", "auditId": audit_id, "expired": True})
                source = payload.get("strategySource")
                if isinstance(source, dict) and source.get("attemptId"):
                    self.store.settle_strategy_card_attempt(
                        attempt_id=str(source["attemptId"]), detail="确认卡已超时作废，未下单", connection=connection)
                self.store.update(record["id"], status="rejected", payload=payload, version=record["version"],
                                  approval_id=record["approvalId"], approval_expires_at=record["approvalExpiresAt"],
                                  approval_used_at=record["approvalUsedAt"], connection=connection)
                plan_ended = self.store.end_plan_of_card(record["id"], connection=connection)
                self.broker.stage(record["sessionId"], kind="result", name="proposal.result",
                                  payload={"proposalId": record["id"], "status": "rejected", "auditId": audit_id,
                                           "message": APPROVAL_EXPIRED_TEXT + (PLAN_ENDED_TEXT if plan_ended else ""),
                                           "reason": "expired"}, connection=connection)
            count += 1
        if count:
            await self.broker.flush()
        return count

    async def _loop(self) -> None:
        while not self._closing:
            try:
                await self.cleanup_expired()
                await self.expire_approvals()
            except Exception:
                # A later sweep retries cleanup; one stale session must not stop the Runtime.
                pass
            await asyncio.sleep(self.interval_seconds)

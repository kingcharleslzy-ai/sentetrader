"""The instance lease: only one process per state database runs the ordering loops.

The lease is the single `runtime_lease` row: the holder heartbeats it every
`heartbeat_seconds`, and a holder silent for `lease_seconds` can be taken over.
The ledger, the stop monitor and the strategy monitor start through
`on_acquired` and stop through `on_lost`, so a standby keeps serving HTTP and
the push feed but never places an order or writes a book twice.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from .store import RuntimeStore, utc_now

log = logging.getLogger("uvicorn.error.agent")

HEARTBEAT_SECONDS = 10.0
LEASE_SECONDS = 30.0


class InstanceLease:
    """Leader/standby election over the shared SQLite state database."""

    def __init__(
        self, *, store: RuntimeStore, instance_id: str, build_sha: str | None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS, lease_seconds: float = LEASE_SECONDS,
        on_acquired: Callable[[], Awaitable[None]], on_lost: Callable[[], Awaitable[None]],
    ) -> None:
        self.store = store
        self.instance_id = instance_id
        self.build_sha = build_sha
        self.heartbeat_seconds = heartbeat_seconds
        self.lease_seconds = lease_seconds
        self.on_acquired = on_acquired
        self.on_lost = on_lost
        self.role: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._renew_task: asyncio.Task[None] | None = None
        # A start may have started only some components. Until a complete
        # on_lost succeeds, treat all of them as potentially running.
        self._loops_started: bool = False
        self._start_complete = False
        self._stopping = False
        self._last_heartbeat = time.monotonic()
        self._transition_lock = asyncio.Lock()
        self._lost_at: float | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        if self._try_acquire():
            self.role = "leader"
            self._last_heartbeat = time.monotonic()
            self._renew_task = asyncio.create_task(self._renew_during_transition(), name="ai-trader-lease-renew")
            self._task = asyncio.create_task(self._loop(), name="ai-trader-instance-lease")
            try:
                async with self._transition_lock:
                    await self._start_loops()
            except asyncio.CancelledError:
                try:
                    await self._stop_loops()
                except Exception:
                    log.exception("instance_lease_cancel_cleanup_failed instance_id=%s", self.instance_id)
                    os._exit(1)
                self.store.release_lease(self.instance_id)
                self.role = None
                for task in (self._task, self._renew_task):
                    if task is not None:
                        task.cancel()
                await asyncio.gather(*(task for task in (self._task, self._renew_task) if task is not None),
                                     return_exceptions=True)
                self._task = self._renew_task = None
                raise
            except Exception:
                # The lease is held; the loop retries the starts on its ticks.
                log.exception("instance_lease_start_loops_failed instance_id=%s", self.instance_id)
        else:
            self.role = "standby"
            self._renew_task = asyncio.create_task(self._renew_during_transition(), name="ai-trader-lease-renew")
            self._task = asyncio.create_task(self._loop(), name="ai-trader-instance-lease")
            leader = self.store.read_lease()
            log.warning(
                "instance_lease_standby leader=%s", leader["instance_id"] if leader else None,
            )

    async def stop(self) -> None:
        self._stopping = True
        # The loops stop before the lease is released, so the instance taking
        # over never runs them alongside these. The stops below in the lifespan
        # still run as the idempotent fallback before the store closes.
        async with self._transition_lock:
            await self._stop_loops()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        renew, self._renew_task = self._renew_task, None
        if renew is not None:
            renew.cancel()
            await asyncio.gather(renew, return_exceptions=True)
        if self.role == "leader":
            # A clean shutdown hands the lease over immediately instead of
            # making the next instance wait out the stale window.
            self.store.release_lease(self.instance_id)
        self.role = None

    def snapshot(self) -> dict[str, str | None]:
        row = self.store.read_lease()
        return {
            "instanceId": self.instance_id,
            "role": self.role,
            "leaderId": row["instance_id"] if row is not None else None,
            "leaderSince": row["acquired_at"] if row is not None else None,
        }

    def _try_acquire(self) -> bool:
        stale_before = (datetime.now(UTC) - timedelta(seconds=self.lease_seconds)).isoformat()
        return self.store.try_acquire_lease(
            instance_id=self.instance_id, host=socket.gethostname(), pid=os.getpid(),
            build_sha=self.build_sha, now=utc_now(), stale_before=stale_before,
        )

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                await self._tick()
            except Exception:
                # One locked database or a failing loop start must not kill
                # the heartbeat: the loops would keep running on a dead task
                # and a standby would take over into a double-run.
                log.exception("instance_lease_tick_failed role=%s", self.role)
                if self.role != "leader" and self._loops_started:
                    # The row has already moved to another holder. Continuing
                    # these ordering loops could double execute orders.
                    os._exit(1)
                if self.role == "leader" and self._loops_started and (
                    time.monotonic() - self._last_heartbeat >= self.lease_seconds - self.heartbeat_seconds
                ):
                    try:
                        await asyncio.wait_for(self._stop_loops(), timeout=max(self.heartbeat_seconds, 1.0))
                    except Exception:
                        log.exception("instance_lease_expired_stop_failed instance_id=%s", self.instance_id)
                        os._exit(1)

    async def _renew_during_transition(self) -> None:
        """Keep the row live while start/stop holds up the ordinary tick loop."""
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if self.role != "leader":
                if (self._loops_started and self._lost_at is not None
                        and time.monotonic() - self._lost_at >= max(self.heartbeat_seconds, 1.0)):
                    log.error("instance_lease_lost_cleanup_timeout instance_id=%s", self.instance_id)
                    os._exit(1)
                continue
            if not (self._transition_lock.locked() or self._task is None or self._stopping):
                continue
            try:
                if self.store.heartbeat_lease(self.instance_id, utc_now()):
                    self._last_heartbeat = time.monotonic()
                    continue
                self.role = "standby"
                self._lost_at = time.monotonic()
                log.error("instance_lease_lost_during_transition instance_id=%s", self.instance_id)
                if self._loops_started:
                    # A blocked lifecycle callback cannot be allowed to run
                    # beside the new holder's ordering loops.
                    os._exit(1)
            except Exception:
                log.exception("instance_lease_transition_heartbeat_failed instance_id=%s", self.instance_id)
                if self._loops_started and time.monotonic() - self._last_heartbeat >= self.lease_seconds - self.heartbeat_seconds:
                    os._exit(1)

    async def _tick(self) -> None:
        async with self._transition_lock:
            await self._tick_locked()

    async def _tick_locked(self) -> None:
        if self.role == "leader":
            if self.store.heartbeat_lease(self.instance_id, utc_now()):
                self._last_heartbeat = time.monotonic()
                # A heartbeat failure above is just a failed write: the lease
                # is held. A rejected heartbeat — another instance holds the
                # row — is what demotes, and only that does.
                if not self._start_complete and not self._stopping:
                    await self._start_loops()
                return
            self.role = "standby"
            self._lost_at = time.monotonic()
            log.error("instance_lease_lost instance_id=%s", self.instance_id)
            try:
                await asyncio.wait_for(self._stop_loops(), timeout=max(self.heartbeat_seconds, 1.0))
            except Exception:
                log.exception("instance_lease_lost_cleanup_failed instance_id=%s", self.instance_id)
                os._exit(1)
            self._lost_at = None
            return
        # Standby: finish a stop that failed on an earlier tick, then compete.
        await self._stop_loops()
        if not self._stopping and self._try_acquire():
            self.role = "leader"
            self._lost_at = None
            self._last_heartbeat = time.monotonic()
            await self._start_loops()

    async def _start_loops(self) -> None:
        # A previous partial start must be fully cleaned before retrying.
        await self._stop_loops()
        self._loops_started = True
        try:
            await self.on_acquired()
        except BaseException as error:
            try:
                await self._stop_loops()
            except Exception:
                if isinstance(error, asyncio.CancelledError):
                    log.exception("instance_lease_cancel_cleanup_failed instance_id=%s", self.instance_id)
                    os._exit(1)
                raise
            raise
        self._start_complete = True

    async def _stop_loops(self) -> None:
        if not self._loops_started:
            return
        # The stops are idempotent no-ops on never-started tasks; a failure
        # leaves the flag up so the next tick retries — a standby must never
        # keep the ordering loops running.
        await self.on_lost()
        self._loops_started = False
        self._start_complete = False

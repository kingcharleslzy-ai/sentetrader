from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4


@dataclass
class BacktestLease:
    request_id: str
    user_id: str
    cancel_event: threading.Event
    deadline: float

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise RuntimeError("backtest_cancelled")
        if time.monotonic() >= self.deadline:
            raise RuntimeError("backtest_timeout")


class BacktestCoordinator:
    """Bounded global/per-user queue with cancellation and deterministic cleanup."""

    def __init__(
        self, *, global_limit: int = 2, per_user_limit: int = 1,
        global_queue_limit: int = 32, per_user_queue_limit: int = 4,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.global_limit = max(1, global_limit)
        self.per_user_limit = max(1, per_user_limit)
        self.global_queue_limit = max(self.global_limit, global_queue_limit)
        self.per_user_queue_limit = max(self.per_user_limit, per_user_queue_limit)
        self.timeout_seconds = max(1.0, timeout_seconds)
        self._global = threading.BoundedSemaphore(self.global_limit)
        self._users: dict[str, threading.BoundedSemaphore] = {}
        self._pending_by_user: dict[str, int] = defaultdict(int)
        self._jobs: dict[str, BacktestLease] = {}
        self._lock = threading.RLock()

    @contextmanager
    def lease(
        self, *, user_id: str, request_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Iterator[BacktestLease]:
        job_id = request_id or str(uuid4())
        with self._lock:
            if job_id in self._jobs:
                raise RuntimeError("backtest_request_conflict")
            if len(self._jobs) >= self.global_queue_limit:
                raise RuntimeError("backtest_global_queue_full")
            if self._pending_by_user[user_id] >= self.per_user_queue_limit:
                raise RuntimeError("backtest_user_queue_full")
            lease = BacktestLease(
                request_id=job_id, user_id=user_id, cancel_event=threading.Event(),
                deadline=time.monotonic() + (
                    self.timeout_seconds if timeout_seconds is None else max(1.0, timeout_seconds)
                ),
            )
            self._jobs[job_id] = lease
            self._pending_by_user[user_id] += 1
            user_semaphore = self._users.setdefault(
                user_id, threading.BoundedSemaphore(self.per_user_limit)
            )
        user_acquired = False
        global_acquired = False
        try:
            while not user_acquired:
                lease.check()
                user_acquired = user_semaphore.acquire(timeout=0.05)
            while not global_acquired:
                lease.check()
                global_acquired = self._global.acquire(timeout=0.05)
            lease.check()
            yield lease
            lease.check()
        finally:
            if global_acquired:
                self._global.release()
            if user_acquired:
                user_semaphore.release()
            with self._lock:
                self._jobs.pop(job_id, None)
                self._pending_by_user[user_id] -= 1
                if self._pending_by_user[user_id] <= 0:
                    self._pending_by_user.pop(user_id, None)
                    self._users.pop(user_id, None)

    def cancel(self, request_id: str, *, user_id: str) -> bool:
        with self._lock:
            lease = self._jobs.get(request_id)
            if lease is None or lease.user_id != user_id:
                return False
            lease.cancel_event.set()
            return True

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "jobs": len(self._jobs),
                "users": len(self._users),
                "queuedUsers": len(self._pending_by_user),
            }

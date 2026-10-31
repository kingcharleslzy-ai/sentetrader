from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Protocol

from .broker import EventBroker
from .store import RuntimeStore


class RunnableAdapter(Protocol):
    async def handle_message(
        self, session_id: str, text: str, run_id: str, *,
        user_id: str | None = None, model_slug: str | None = None,
    ) -> None: ...


class RunManager:
    """One FIFO worker per session prevents overlapping Agent mutations."""

    def __init__(
        self, store: RuntimeStore, broker: EventBroker, adapter: RunnableAdapter,
        *, idle_worker_seconds: float = 30.0,
    ) -> None:
        self.store, self.broker, self.adapter = store, broker, adapter
        self.idle_worker_seconds = idle_worker_seconds
        self.queues: dict[str, asyncio.Queue[str]] = {}
        self.workers: dict[str, asyncio.Task[None]] = {}
        self.active: dict[str, asyncio.Task[None]] = {}
        self._closing = False
        self._releasing_sessions: set[str] = set()
        # Every run this process accepted and has not finished: a lease taken over mid-deploy
        # must not end them as the previous holder's leftovers (525-A).
        self.accepted: set[str] = set()

    def enqueue(
        self, *, session_id: str, user_id: str, instruction: str,
        model_slug: str | None = None,
    ) -> dict[str, object]:
        if session_id in self._releasing_sessions:
            # #515 F-d: the session is mid-release (its rows are about to go). A run enqueued now
            # would be deleted under the worker's feet (KeyError on the cascaded row); the caller
            # answers 409 so the client re-sends into whatever session it opens next.
            raise ValueError("session_releasing")
        run = self.store.create_run(
            session_id=session_id, user_id=user_id, instruction=instruction,
            model_slug=model_slug,
        )
        queue = self.queues.setdefault(session_id, asyncio.Queue())
        self.accepted.add(str(run["runId"]))
        queue.put_nowait(run["runId"])
        worker = self.workers.get(session_id)
        if worker is None or worker.done():
            self.workers[session_id] = asyncio.create_task(self._worker(session_id))
        return run

    async def _worker(self, session_id: str) -> None:
        queue = self.queues[session_id]
        current_worker = asyncio.current_task()
        try:
            while True:
                try:
                    run_id = await asyncio.wait_for(queue.get(), timeout=self.idle_worker_seconds)
                except TimeoutError:
                    if queue.empty():
                        break
                    continue
                try:
                    run = self.store.get_run(run_id)
                    if run["status"] == "cancelled":
                        continue
                    running = self.store.update_run(
                        run_id, status="running", expected_statuses={"queued"}
                    )
                    if running["status"] != "running":
                        continue
                    task = asyncio.create_task(
                        self.adapter.handle_message(
                            session_id, run["instruction"], run_id,
                            user_id=str(run.get("userId") or ""),
                            model_slug=str(run["modelSlug"]) if run.get("modelSlug") else None,
                        )
                    )
                    self.active[run_id] = task
                    try:
                        await task
                        self.store.update_run(
                            run_id, status="completed", result={"eventStream": "completed"},
                            expected_statuses={"running"},
                        )
                    except asyncio.CancelledError:
                        if self._closing:
                            # 525-A: this process is shutting down — the user did
                            # not cancel the run; the restart terminated it.
                            self.store.update_run(
                                run_id, status="failed", error="runtime_restarted",
                                expected_statuses={"running"},
                            )
                        else:
                            self.store.update_run(
                                run_id, status="cancelled", error="cancelled_by_user",
                                expected_statuses={"running"},
                            )
                        if self._closing or session_id in self._releasing_sessions:
                            raise
                    except Exception as error:
                        failed = self.store.update_run(
                            run_id, status="failed", error=type(error).__name__,
                            expected_statuses={"running"},
                        )
                        if failed["status"] == "failed":
                            error_payload = getattr(self.adapter, "error_payload", None)
                            payload = error_payload(error, run_id) if error_payload else {
                                "code": "agent_run_failed",
                                "message": "任务执行失败，已安全停止。",
                                "runId": run_id,
                                "scope": "task",
                            }
                            await self.broker.publish(
                                session_id, kind="error", name="error",
                                payload=payload,
                            )
                    finally:
                        self.active.pop(run_id, None)
                finally:
                    self.accepted.discard(str(run_id))
                    queue.task_done()
        finally:
            if self.workers.get(session_id) is current_worker:
                self.workers.pop(session_id, None)
                if queue.empty():
                    self.queues.pop(session_id, None)
                elif not self._closing and session_id not in self._releasing_sessions:
                    self.workers[session_id] = asyncio.create_task(self._worker(session_id))

    async def cancel(self, run_id: str, *, user_id: str) -> dict[str, object]:
        run = self.store.get_run(run_id, user_id=user_id)
        if run["status"] not in {"queued", "running"}:
            return run
        updated = self.store.update_run(
            run_id, status="cancelled", error="cancelled_by_user",
            expected_statuses={"queued", "running"},
        )
        if updated["status"] != "cancelled":
            return updated
        cancel_adapter = getattr(self.adapter, "cancel_run", None)
        if cancel_adapter is not None:
            await cancel_adapter(run_id)
        task = self.active.get(run_id)
        if task is not None:
            task.cancel()
        tasks = getattr(self.adapter, "tasks", {})
        payload = tasks.get(run_id)
        if payload is not None:
            payload = deepcopy(payload)
            payload["status"] = "cancelled"
            for step in payload["steps"]:
                if step["status"] == "active":
                    step.update({"status": "failed", "detail": "已停止"})
            tasks[run_id] = deepcopy(payload)
            await self.broker.publish(run["sessionId"], kind="progress", name="task.completed", payload=payload)
        return updated

    async def shutdown(self) -> None:
        """End the runs only this process accepted — running and queued — as
        terminated by the restart, each with one session notice (525-A). Runs
        that finished before the cancel keep their own outcome."""
        self._closing = True
        closing = list(self.active)
        for task in self.active.values():
            task.cancel()
        for worker in self.workers.values():
            worker.cancel()
        await asyncio.gather(*self.active.values(), *self.workers.values(), return_exceptions=True)
        # Runs still queued here never started — the workers that would have run
        # them are gone. End them now instead of leaving them for the next boot.
        for queue in list(self.queues.values()):
            while not queue.empty():
                try:
                    run_id = queue.get_nowait()
                    queue.task_done()
                except asyncio.QueueEmpty:
                    break
                if self._end_as_restarted(run_id, expected={"queued"}):
                    closing.append(run_id)
        # A worker that died without marking its run leaves it running: end it
        # here too, or the safety net (the next boot's recovery) picks it up.
        for run_id in closing:
            if self.store.get_run(run_id)["status"] == "running":
                self._end_as_restarted(run_id, expected={"running"})
        for run_id in closing:
            run = self.store.get_run(run_id)
            if run["status"] == "failed" and run["error"] == "runtime_restarted":
                await self.broker.publish(
                    run["sessionId"], kind="error", name="error",
                    payload={
                        "code": "runtime_restarted",
                        "message": "Runtime 重启，未完成任务已安全终止。",
                        "runId": run_id,
                        "scope": "task",
                    },
                )

    def _end_as_restarted(self, run_id: str, *, expected: set[str]) -> bool:
        """CAS the run to failed/runtime_restarted; False when another outcome
        had already landed (completed, user-cancelled, a real failure)."""
        updated = self.store.update_run(
            run_id, status="failed", error="runtime_restarted", expected_statuses=expected,
        )
        return updated["status"] == "failed"

    def mark_releasing(self, session_id: str) -> None:
        """#515 F-d: mark the session as mid-release for the release's whole window (run
        teardown through the row delete) — enqueue answers `session_releasing`, which the
        routes turn into 409 so the client re-sends into whatever session it opens next.
        The lifecycle that drives the release owns the mark; this class only reads it."""
        self._releasing_sessions.add(session_id)

    def unmark_releasing(self, session_id: str) -> None:
        self._releasing_sessions.discard(session_id)

    async def release_session(self, session_id: str) -> None:
        runs = self.store.runs_for_session(session_id, statuses={"queued", "running"})
        cancel_adapter = getattr(self.adapter, "cancel_run", None)
        for run in runs:
            run_id = str(run["runId"])
            self.store.update_run(
                run_id, status="cancelled", error="session_released",
                expected_statuses={"queued", "running"},
            )
            if cancel_adapter is not None:
                try:
                    await cancel_adapter(run_id)
                except Exception:
                    pass
            task = self.active.get(run_id)
            if task is not None:
                task.cancel()
        worker = self.workers.pop(session_id, None)
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        queue = self.queues.pop(session_id, None)
        if queue is not None:
            while not queue.empty():
                try:
                    queue.get_nowait()
                    queue.task_done()
                except asyncio.QueueEmpty:
                    break

"""Keep bounded business work and its result alive through caller cancellation."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def owned_operation[T](
    call: Callable[[], Awaitable[T]], *, lock: asyncio.Lock | None = None,
    authorize: Callable[[], Awaitable[None]] | None = None,
    committed: bool = False,
) -> T:
    """Cancel queued work; drain started work, even through repeated cancellation.

    The business service owns network deadlines and UNKNOWN/order recovery.
    This helper neither retries nor imposes a timeout that could abandon a
    still-running thread. Its task includes result persistence, not just I/O.
    committed means the caller already persisted CALL_STARTED without yielding;
    cancellation must drain that work even before this task first gets to run.
    """
    started = committed

    async def invoke():
        nonlocal started
        if authorize is not None:
            await authorize()
        started = True
        return await call()

    async def run():
        if lock is None:
            return await invoke()
        async with lock:
            return await invoke()

    task = asyncio.create_task(run())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not started:
            task.cancel()
        # Shield *every* cleanup wait. A second cancellation must not propagate
        # into the owner task or release a lock while to_thread is still writing.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Consume a failed result when its waiter is gone.
        raise

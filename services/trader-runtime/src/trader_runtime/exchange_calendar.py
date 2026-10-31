"""The Runtime's one exchange-calendar source (2026-10-12).

Every trading-day judgment that needs holidays — order expiry, backfilled fills, the direct-stop
overnight bucket, delivery reminders — reads the same calendar: the sidecar's TqSdk calendar
summary (``provider.market_calendar()``, ``/v1/public-market/calendar``), turned into known trading
days by ``trading_calendar.summary_trading_days`` and fed to ``trading_calendar``'s pure functions.
The shared market (``SharedMarket.trading_days``) reads the same endpoint through its relay.

A read is remembered for a minute and never across the 18:00 turn, so hot loops do not hit the
sidecar on every pass. How it is read (2026-10-15, #754):

- A sidecar read runs on this module's own threads, in a fresh context: never inside an account read
  snapshot (``provider_read_snapshot``), whose prefetched cut does not hold the calendar — a read
  there used to fail as ``sidecar_read_not_prefetched`` and its ``[]`` was cached for every other
  reader of the account. An account snapshot starts the read (``read_soon``) beside its own
  prefetch, so the readers inside it normally find the calendar already in memory.
- On the event loop a reader never waits for the sidecar: a cold read is started in the background
  and the reader gets the last good calendar meanwhile. Off the loop the reader waits for it.
- A failed read keeps the last good calendar (retried after ``CALENDAR_RETRY_SECONDS``); only with
  none is the answer an empty list, and each caller states its own fallback (see the callers'
  docstrings) instead of guessing silently.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .trading_calendar import CST, TRADING_DAY_TURN_HOUR, summary_trading_days

log = logging.getLogger("uvicorn.error.calendar")

CALENDAR_CACHE_SECONDS = 60.0
CALENDAR_RETRY_SECONDS = 10.0
# How long a provider's last good calendar outlives its freshness: it stands in while reads fail, and an
# entry past it is dropped (pool accounts and replays get a new provider object each time they are
# assembled; a kept entry would hold the released one forever — #790 review).
LAST_GOOD_SECONDS = 6 * 3600.0
# How long a reader off the event loop waits for a sidecar read (the HTTP read itself times out at 2 s).
READ_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class _Entry:
    provider: Any
    fresh_until: float          # monotonic
    bucket: tuple[str, bool]    # natural day and the 18:00 turn
    days: list[str]             # the last good calendar ([] when there never was one)
    kept_until: float           # monotonic; the entry is dropped after it


_lock = threading.Lock()
_cache: dict[int, _Entry] = {}
_inflight: dict[int, tuple[Any, Future[list[str]]]] = {}
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="exchange-calendar")


def _bucket() -> tuple[str, bool]:
    now = datetime.now(UTC).astimezone(CST)
    return now.date().isoformat(), now.hour >= TRADING_DAY_TURN_HOUR


def _entry(provider: Any) -> _Entry | None:
    entry = _cache.get(id(provider))
    return entry if entry is not None and entry.provider is provider else None


def _fresh(provider: Any) -> list[str] | None:
    with _lock:
        entry = _entry(provider)
        if entry is not None and entry.fresh_until > time.monotonic() and entry.bucket == _bucket():
            return list(entry.days)
    return None


def _last_good(provider: Any) -> list[str]:
    with _lock:
        entry = _entry(provider)
        return list(entry.days) if entry is not None else []


def _remote(provider: Any) -> bool:
    """A provider whose calendar is an HTTP read of the sidecar (the paper accounts' delegates to the
    main sidecar); fixture and in-process providers answer from memory and are read in place."""
    from .data_providers import SidecarReadOnlyDataProvider  # lazy: data_providers imports this module
    return isinstance(provider, SidecarReadOnlyDataProvider)


def _read(provider: Any) -> list[str]:
    try:
        days = summary_trading_days(provider.market_calendar() or {})
    except Exception as error:  # noqa: BLE001 - an unreadable calendar is the callers' fallback
        log.debug("exchange calendar unavailable: %s", error)
        days = []
    now = time.monotonic()
    with _lock:
        for stale in [key for key, entry in _cache.items() if entry.kept_until <= now]:
            del _cache[stale]
        previous = _entry(provider)
        if days:
            fresh = now + CALENDAR_CACHE_SECONDS
            _cache[id(provider)] = _Entry(provider, fresh, _bucket(), days, fresh + LAST_GOOD_SECONDS)
        else:
            # A failed read never replaces a good calendar: the last one stands in until the retry.
            days = previous.days if previous is not None else []
            retry = now + CALENDAR_RETRY_SECONDS
            _cache[id(provider)] = _Entry(provider, retry, _bucket(), days,
                                          previous.kept_until if days and previous is not None else retry)
    return list(days)


def _refresh(provider: Any) -> Future[list[str]]:
    """The provider's sidecar read, at most one at a time, on this module's threads (a fresh context:
    it never sees the caller's account snapshot)."""
    key = id(provider)
    with _lock:
        running = _inflight.get(key)
        if running is not None and running[0] is provider:
            return running[1]
        future = _executor.submit(_read, provider)
        _inflight[key] = (provider, future)

    def done(_: Future[list[str]]) -> None:
        with _lock:
            if _inflight.get(key, (None, None))[1] is future:
                del _inflight[key]
    future.add_done_callback(done)
    return future


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def calendar_trading_days(provider: Any) -> list[str]:
    """Sorted ISO trading days the provider's exchange calendar knows (recent ended days, the
    current one and, from a sidecar that sends them, the coming ones); the last good calendar while
    a read fails; [] when there has never been one. On the event loop it never waits for the sidecar."""
    days = _fresh(provider)
    if days is not None:
        return days
    if not _remote(provider):
        return _read(provider)
    future = _refresh(provider)
    if _on_event_loop():
        return _last_good(provider)   # the background read serves the next reader
    try:
        return list(future.result(timeout=READ_WAIT_SECONDS))
    except Exception:  # noqa: BLE001 - a stuck read: the last good calendar stands in
        return _last_good(provider)


async def trading_days(provider: Any) -> list[str]:
    """``calendar_trading_days`` for async callers: a cold sidecar read is awaited off the loop."""
    days = _fresh(provider)
    if days is not None:
        return days
    if not _remote(provider):
        return _read(provider)
    try:
        # Shielded: a waiter that gives up must not cancel the read other readers share.
        read = asyncio.shield(asyncio.wrap_future(_refresh(provider)))
        return list(await asyncio.wait_for(read, READ_WAIT_SECONDS))
    except Exception:  # noqa: BLE001 - a stuck read: the last good calendar stands in
        return _last_good(provider)


def read_soon(provider: Any) -> None:
    """Start a sidecar read unless the calendar is fresh (``provider_read_snapshot``, beside its prefetch);
    nobody waits for it — the readers find it in memory once it lands."""
    if _remote(provider) and _fresh(provider) is None:
        _refresh(provider)

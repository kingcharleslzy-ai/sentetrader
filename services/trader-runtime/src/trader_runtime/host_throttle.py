"""Polite pacing for the public sites the research readers scrape.

Every request to a paced host takes a turn: turns start in order, each at least
`interval` plus a random 0.1–0.5 s after the previous one on that host (the
pattern TradingAgents-astock's `_em_get` uses against Eastmoney's rate limits).
Sync callers (AKShare on worker threads) also hold the host's lock for the whole
request, so their requests never overlap; async callers queue on the host's
asyncio lock the same way. The clock and the sleeps are injectable: tests pace
on a fake clock instead of sleeping.

`EASTMONEY` is the single entry for every Eastmoney request (news search, data
center, article pages, the AKShare flash and inventory readers): one lane per
Eastmoney host, ≥ 1 s apart. `GENTLE` paces the other scraped hosts (同花顺, 富途,
上海金属网, RSSHub) at ≥ 0.5 s — they are read about once a minute anyway.
"""
from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager


class HostThrottle:
    def __init__(self, interval: float, jitter: tuple[float, float] = (0.1, 0.5), *,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 jitter_draw: Callable[[float, float], float] = random.uniform) -> None:
        self.interval, self.jitter = interval, jitter
        self.clock, self.sleep, self.async_sleep, self.jitter_draw = clock, sleep, async_sleep, jitter_draw
        self._guard = threading.Lock()
        self._next: dict[str, float] = {}
        self._thread_locks: dict[str, threading.Lock] = {}
        self._async_locks: dict[tuple[int, str], asyncio.Lock] = {}

    def reserve(self, host: str) -> float:
        """Book the host's next turn; the seconds to wait before it starts."""
        with self._guard:
            now = self.clock()
            start = max(now, self._next.get(host, now))
            self._next[host] = start + self.interval + self.jitter_draw(*self.jitter)
            return start - now

    @contextmanager
    def turn(self, host: str) -> Iterator[None]:
        """A sync request's turn (worker threads only — it sleeps)."""
        with self._guard:
            lock = self._thread_locks.setdefault(host, threading.Lock())
        with lock:
            wait = self.reserve(host)
            if wait > 0:
                self.sleep(wait)
            yield

    @asynccontextmanager
    async def async_turn(self, host: str) -> AsyncIterator[None]:
        """An async request's turn on the running loop."""
        with self._guard:
            lock = self._async_locks.setdefault((id(asyncio.get_running_loop()), host), asyncio.Lock())
        async with lock:
            wait = self.reserve(host)
            if wait > 0:
                await self.async_sleep(wait)
            yield


EASTMONEY = HostThrottle(1.0)
GENTLE = HostThrottle(0.5, (0.0, 0.2))


def host_of(url: str) -> str:
    """`https://search-api-web.eastmoney.com/x` → `search-api-web.eastmoney.com`."""
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0].split("?", 1)[0].lower()


def throttle_for(url: str) -> HostThrottle:
    return EASTMONEY if host_of(url).endswith("eastmoney.com") else GENTLE

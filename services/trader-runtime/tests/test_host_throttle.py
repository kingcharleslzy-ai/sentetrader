"""Eastmoney pacing: requests take ordered turns ≥ 1 s + 0.1–0.5 s apart, on a fake clock (no real sleep)."""
from __future__ import annotations

import asyncio
import threading

from trader_runtime.host_throttle import EASTMONEY, HostThrottle, host_of, throttle_for


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    async def async_sleep(self, seconds: float) -> None:
        self.sleep(seconds)


def paced(clock: FakeClock, draws: list[float]) -> HostThrottle:
    """Jitter draws come from the list, checked against the requested 0.1–0.5 s range."""
    def draw(low: float, high: float) -> float:
        assert (low, high) == (0.1, 0.5)
        return draws.pop(0)
    return HostThrottle(1.0, clock=clock, sleep=clock.sleep, async_sleep=clock.async_sleep, jitter_draw=draw)


def test_sync_turns_are_serial_and_spaced_by_interval_plus_jitter():
    clock = FakeClock()
    throttle = paced(clock, [0.1, 0.5, 0.3, 0.3, 0.3])
    starts: list[float] = []
    lock = threading.Lock()
    for _ in range(3):
        with throttle.turn("np-weblist.eastmoney.com"):
            assert lock.acquire(blocking=False)  # nobody else is inside the host's turn
            starts.append(clock.now)
            clock.now += 0.2  # the request itself
            lock.release()
    assert starts == [100.0, 101.1, 102.6]
    assert [round(b - a, 6) for a, b in zip(starts, starts[1:])] == [1.1, 1.5]
    # A turn reserved after a long quiet spell starts at once.
    clock.now += 60
    waits = len(clock.slept)
    with throttle.turn("np-weblist.eastmoney.com"):
        assert len(clock.slept) == waits
    # Another host has its own lane.
    assert throttle.reserve("datacenter-web.eastmoney.com") == 0


def test_async_turns_queue_in_order_on_the_same_lane():
    clock = FakeClock()
    throttle = paced(clock, [0.2, 0.2, 0.2])
    starts: list[float] = []

    async def request() -> None:
        async with throttle.async_turn("search-api-web.eastmoney.com"):
            starts.append(clock.now)

    async def main() -> None:
        await asyncio.gather(*(request() for _ in range(3)))

    asyncio.run(main())
    assert starts == [100.0, 101.2, 102.4]


def test_every_eastmoney_host_takes_the_eastmoney_lane():
    assert host_of("https://search-api-web.eastmoney.com/search/jsonp?cb=x") == "search-api-web.eastmoney.com"
    assert throttle_for("https://datacenter-web.eastmoney.com/api/data/v1/get") is EASTMONEY
    assert throttle_for("https://www.stats.gov.cn/sj/zxfb/") is not EASTMONEY

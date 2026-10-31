"""扩容 ② (2026-10-13): the incremental renderer must equal the full render on every step, and a
reader applying its deltas must hold exactly the full window."""
from __future__ import annotations

import bisect
import math
import random
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from tqkq_readonly_sidecar.kline_series import SeriesRenderer, bars_digest
from tqkq_readonly_sidecar.main import OfficialTqKqSession, detach_klines

HOLIDAYS = {date(2026, 10, day) for day in range(1, 8)}
NS = 1_000_000_000


def trading_dates(start: datetime, end: datetime) -> set[str]:
    days, day = set(), start.date()
    while day <= end.date():
        if day.weekday() < 5 and day not in HOLIDAYS:
            days.add(day.isoformat())
        day += timedelta(days=1)
    return days


def sorted_days(start: datetime, end: datetime) -> list[date]:
    return sorted(date.fromisoformat(value) for value in trading_dates(start, end))


def full_render(frame: pd.DataFrame, limit: int) -> list[dict]:
    session = SimpleNamespace(_trading_dates=trading_dates)
    return OfficialTqKqSession._annotate_trading_days(session, detach_klines(frame, limit=limit))


class Serial:
    """A TqSdk-shaped kline window: `size` rows, NaN rows at the head until history fills them."""

    def __init__(self, size: int, start: datetime, step: int = 60) -> None:
        self.size, self.step = size, step
        self.rows: list[dict | None] = [None] * size
        self.next_time = int(start.timestamp()) * NS
        self.random = random.Random(7)

    def bar(self, time_ns: int) -> dict:
        price = 3000 + self.random.randint(-50, 50)
        return {"datetime": time_ns, "id": 0, "open": price, "high": price + 3, "low": price - 2,
                "close": price + 1, "volume": self.random.randint(0, 500), "close_oi": 8000.0}

    def append(self, gap: int | None = None) -> None:
        self.rows = self.rows[1:] + [self.bar(self.next_time)]
        self.next_time += (gap if gap is not None else self.step) * NS

    def frame(self) -> pd.DataFrame:
        nan = {"datetime": math.nan, "id": math.nan, "open": math.nan, "high": math.nan, "low": math.nan,
               "close": math.nan, "volume": math.nan, "close_oi": math.nan}
        return pd.DataFrame([row or nan for row in self.rows])


class Reader:
    """What the Runtime holds: apply a delta (trim before `first`, upsert by time) or take the whole."""

    def __init__(self) -> None:
        self.token: str | None = None
        self.bars: list[dict] = []
        self.deltas = self.fulls = 0

    def read(self, bars: list[dict], meta) -> None:
        base = meta.base_of(self.token)
        if base is None:
            self.bars, self.fulls = list(bars), self.fulls + 1
        else:
            first = bars[0]["datetime"]
            held = [bar for bar in self.bars if bar["datetime"] >= first]
            times = [bar["datetime"] for bar in held]
            for bar in meta.changed_since(bars, base):
                index = bisect.bisect_left(times, bar["datetime"])
                if index < len(held) and times[index] == bar["datetime"]:
                    held[index] = bar
                else:
                    held.insert(index, bar)
                    times.insert(index, bar["datetime"])
            assert len(held) == len(bars) and bars_digest(held) == meta.digest
            self.bars, self.deltas = held, self.deltas + 1
        self.token = meta.token
        assert self.bars == bars


def test_incremental_render_equals_full_render_through_fill_ticks_rolls_corrections_and_replacement():
    size = 40
    # Friday 14:30 (UTC+8): the window crosses the day close, the night session, the weekend and the
    # National Day holiday — every bar's trading day must match the full render's.
    serial = Serial(size, datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    renderer = SeriesRenderer(size, sorted_days)
    every_step, every_fifth = Reader(), Reader()
    chance = random.Random(11)
    for _ in range(25):  # history arrives from the right, NaN rows at the head
        serial.append()
    for step in range(600):
        roll = chance.random()
        live = [index for index, row in enumerate(serial.rows) if row]
        if roll < 0.55 and live:  # the forming bar ticks
            last = serial.rows[-1]
            last["close"] += chance.choice([-1, 1])
            last["high"] = max(last["high"], last["close"])
            last["volume"] += chance.randint(1, 9)
        elif roll < 0.75:  # a new bar, sometimes across the close / the weekend / the holiday
            serial.append(chance.choice([None, None, None, 6 * 3600, 2 * 86400, 9 * 86400]))
        elif roll < 0.8:  # two bars at once (a burst after a reconnect)
            serial.append()
            serial.append()
        elif roll < 0.85 and serial.rows[0] is None:  # history fills the head
            first = next(index for index, row in enumerate(serial.rows) if row)
            serial.rows[first - 1] = serial.bar(serial.rows[first]["datetime"] - serial.step * NS)
        elif roll < 0.9 and len(live) > 3:  # the source corrects an older bar
            serial.rows[live[len(live) // 2]]["close"] += 1
        elif roll < 0.92 and len(live) > 3:  # an older bar goes bad: it leaves the middle
            serial.rows[live[1]]["close"] = math.nan
        elif roll < 0.94:  # the whole history is replaced (another contract month, a resend)
            offset = chance.choice([60, -60]) * NS
            serial.rows = [dict(row, datetime=row["datetime"] + offset) if row else None for row in serial.rows]
            serial.next_time += offset
        frame = serial.frame()
        bars, meta = renderer.update(frame)
        assert bars == full_render(frame, size), step
        assert bars_digest(bars) == meta.digest
        every_step.read(bars, meta)
        if step % 5 == 0:
            every_fifth.read(bars, meta)
    # Almost every step travels as a delta; only replaced or broken windows go whole again.
    assert every_step.deltas > every_step.fulls * 5
    assert every_fifth.deltas > every_fifth.fulls


def test_unchanged_serial_keeps_the_same_objects_and_a_tick_changes_one_bar():
    serial = Serial(30, datetime(2026, 10, 12, 1, 0, tzinfo=UTC))
    for _ in range(30):
        serial.append()
    renderer = SeriesRenderer(30, sorted_days)
    bars, meta = renderer.update(serial.frame())
    again, same = renderer.update(serial.frame())
    assert again is bars and same is meta
    serial.rows[-1]["close"] += 1
    ticked, after = renderer.update(serial.frame())
    assert after.version == meta.version + 1 and after.floor == meta.floor
    assert meta.changed_since(bars, meta.version) == []
    assert after.changed_since(ticked, meta.version) == [ticked[-1]]
    assert ticked[:-1] == bars[:-1] and all(a is b for a, b in zip(ticked[:-1], bars[:-1]))
    serial.append()
    rolled, rolled_meta = renderer.update(serial.frame())
    assert rolled[0]["datetime"] > ticked[0]["datetime"]
    assert rolled_meta.changed_since(rolled, after.version) == [rolled[-1]]


@pytest.mark.parametrize("token", [None, "", "other:1", "x", "abc:notanumber"])
def test_foreign_or_malformed_reader_tokens_get_the_whole_window(token):
    serial = Serial(10, datetime(2026, 10, 12, 1, 0, tzinfo=UTC))
    for _ in range(10):
        serial.append()
    _, meta = SeriesRenderer(10, sorted_days).update(serial.frame())
    assert meta.base_of(token) is None
    assert meta.base_of(meta.token) == meta.version
    assert meta.base_of(f"{meta.epoch}:{meta.version + 1}") is None  # from the future


def test_digest_matches_the_runtime_and_client_vector():
    # The same vector is pinned in services/trader-runtime/tests (bars_digest) — the three digests
    # (sidecar, Runtime, marketCache.ts) must build one text.
    bars = [{"datetime": 1_788_192_000_000_000_000, "open": 3000.5, "high": 3003.25, "low": 2999.0,
             "close": 3001.0, "volume": 120.0, "openInterest": 8000.0, "tradingDay": "2026-09-01"},
            {"datetime": 1_788_192_060_000_000_000, "open": 3001.0, "high": 3002.0, "low": 3000.0,
             "close": 3001.5, "volume": 0.0, "openInterest": 8001.0, "tradingDay": "2026-09-01"}]
    assert bars_digest(bars) == "c7364d3e"


def test_a_missing_low_or_high_written_as_minus_one_is_filled_from_the_bar_and_a_real_negative_price_kept():
    """2026-10-21：快期外盘主连迷你纳指日线 20 根里 3 根最低价是 -1（缺数占位），调研算出“振幅 107%”、图上拖出长影线。
    其余三个价为正时按缺数补（低 = 开收较低者、高 = 开收较高者）；整根为负的真负价（2020 年 WTI）照旧。"""
    day = 86_400 * NS
    rows = [{"datetime": 1_789_920_000 * NS, "id": 0, "open": 29840.5, "high": 30264.0, "low": -1.0, "close": 30234.25,
             "volume": 80855, "close_oi": 0.0},
            {"datetime": 1_789_920_000 * NS + day, "id": 1, "open": 30237.0, "high": -1.0, "low": 29904.0, "close": 30837.25,
             "volume": 102736, "close_oi": 0.0},
            {"datetime": 1_789_920_000 * NS + 2 * day, "id": 2, "open": -14.0, "high": -10.0, "low": -40.32, "close": -37.63,
             "volume": 1, "close_oi": 0.0}]
    frame = pd.DataFrame(rows)
    full = full_render(frame, limit=10)
    assert [bar["low"] for bar in full] == [29840.5, 29904.0, -40.32]
    assert [bar["high"] for bar in full] == [30264.0, 30837.25, -10.0]
    renderer = SeriesRenderer(10, sorted_days)
    bars, _ = renderer.update(frame)
    assert [(bar["low"], bar["high"]) for bar in bars] == [(bar["low"], bar["high"]) for bar in full]

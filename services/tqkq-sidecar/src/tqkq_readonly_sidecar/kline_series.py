"""Incremental K-line rendering for the public market worker (扩容 ②, 2026-10-13).

TqSdk keeps a kline serial as one numpy array it updates in place: once a serial is initialized
only its last row changes, and a new bar shifts the window up by one (`TqApi._update_serial_single`).
Re-rendering the whole 256–600 bar window on every tick (pandas rows → dicts → trading day →
full comparison) was 22 % of a pool child's busy time. `SeriesRenderer` instead keeps a copy of
the serial's columns, finds the rows that really changed with a vectorised comparison (any row,
not just the last — a resent or corrected history bar is caught the same way) and renders only
those, through the same per-bar rules as `main.detach_klines` + `_annotate_trading_days`, so the
bars are identical to a full render (tests/test_kline_series.py drives both).

Every bar carries the version at which it last changed. A reader holding version V receives only
the bars changed after V (`SeriesVersion.changed_since`), plus the window's first bar time, its bar
count and digest so it can prove its copy equals this one. Anything the reader could not rebuild
from that — a bar dropping out of the middle, a window that no longer lines up with the previous
one — raises the series floor and every older reader gets the whole window again.
"""
from __future__ import annotations

import math
import uuid
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from .main import CHINA_TIMEZONE, finite_number, number, repaired_prices, trading_day_from_dates

# Serial column → bar field. A serial without a volume / open-interest column renders 0 there,
# without a time or price column no bar at all — the defaults detach_klines' getattr uses.
_PRICE_COLUMNS = ("open", "high", "low", "close")
_DIGEST_FIELDS = ("open", "high", "low", "close", "volume", "openInterest")


def _digest_number(value: Any, divisor: float = 1.0, multiplier: float = 1.0) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "-"
    return str(math.floor(float(value) / divisor * multiplier + 0.5))


def bar_digest_text(bar: dict[str, Any]) -> str:
    """One bar's canonical digest text — the Runtime's `shared_market.bars_digest` and the client's
    `barsDigest` (marketCache.ts) build the same text: the time in whole seconds, OHLC/volume/open
    interest in 1/10000 units and the trading day, `,` between fields."""
    trading_day = bar.get("tradingDay")
    return ",".join([_digest_number(bar.get("datetime"), divisor=1e9),
                     *(_digest_number(bar.get(name), multiplier=10_000) for name in _DIGEST_FIELDS),
                     trading_day if isinstance(trading_day, str) else ""])


def digest_of_texts(texts: Sequence[str]) -> str:
    """CRC-32 (hex) of the bars' digest texts joined by `;` — `shared_market.bars_digest`."""
    return f"{zlib.crc32(';'.join(texts).encode('utf-8')):08x}"


def bars_digest(bars: Sequence[dict[str, Any]]) -> str:
    return digest_of_texts([bar_digest_text(bar) for bar in bars])


@dataclass(frozen=True, slots=True)
class SeriesVersion:
    """One published state of a series' bars. `versions[i]` is the version at which `bars[i]`
    last changed; readers older than `floor` (or of another epoch) get the whole window."""

    epoch: str
    version: int
    floor: int
    versions: tuple[int, ...]
    digest: str

    @property
    def token(self) -> str:
        return f"{self.epoch}:{self.version}"

    def base_of(self, token: Any) -> int | None:
        """The version a reader's token names, when this series can answer it with a delta."""
        if not isinstance(token, str):
            return None
        epoch, _, raw = token.rpartition(":")
        if epoch != self.epoch or not raw.isdigit():
            return None
        version = int(raw)
        return version if self.floor <= version <= self.version else None

    def changed_since(self, bars: Sequence[dict[str, Any]], base: int) -> list[dict[str, Any]]:
        return [bar for bar, version in zip(bars, self.versions, strict=True) if version > base]


def _column(serial: Any, name: str, default: float) -> np.ndarray:
    if name in serial.columns:
        return np.array(serial[name].to_numpy(), dtype=np.float64, copy=True)
    return np.full(len(serial), default, dtype=np.float64)


def _base_bar(row: Sequence[float]) -> dict[str, Any] | None:
    """`detach_klines`' rule for one row: whole-number time > 0 and four finite prices, or no bar."""
    timestamp = int(number(row[0]))
    prices = [finite_number(value) for value in row[1:5]]
    if timestamp <= 0 or any(value is None for value in prices):
        return None
    repaired = repaired_prices(*(float(value) for value in prices))  # type: ignore[arg-type]
    return {"datetime": timestamp, **dict(zip(_PRICE_COLUMNS, repaired, strict=True)),
            "volume": number(row[5]), "openInterest": number(row[6])}


def _same(new: np.ndarray, old: np.ndarray) -> np.ndarray:
    return (new == old) | (np.isnan(new) & np.isnan(old))


class SeriesRenderer:
    """One (symbol, duration) serial's rendered bars, kept in step with the SDK row by row. Used on
    the worker's SDK thread only; what it returns (bars list, SeriesVersion) is never mutated."""

    def __init__(self, limit: int,
                 sorted_days: Callable[[datetime, datetime], list[date]]) -> None:
        self.limit = limit
        self._sorted_days = sorted_days
        self.epoch = uuid.uuid4().hex[:12]
        self.version = 0
        self.floor = 0
        self._columns: list[np.ndarray] | None = None
        self._rows: list[dict[str, Any] | None] = []
        self._row_versions: list[int] = []
        self._row_texts: list[str | None] = []
        self._calendar_key: tuple[date, date] | None = None
        self.bars: list[dict[str, Any]] = []
        self.meta: SeriesVersion | None = None

    def _shift(self, columns: list[np.ndarray]) -> int | None:
        """How many rows the window moved up since the last update, or None when it does not line
        up with the previous one (first update, resized, history replaced): render it all."""
        previous = self._columns
        if previous is None or len(previous[0]) != len(columns[0]):
            return None
        old, new = previous[0], columns[0]
        size = len(new)
        if size == 0:
            return 0
        last = old[-1]
        if not math.isfinite(last) or new[-1] == last:
            shift = 0
        else:
            hits = np.flatnonzero(new == last)
            if len(hits) != 1:
                return None
            shift = size - 1 - int(hits[0])
        if not np.array_equal(new[:size - shift], old[shift:], equal_nan=True):
            return None
        return shift

    def update(self, serial: Any) -> tuple[list[dict[str, Any]], SeriesVersion]:
        columns = [_column(serial, "datetime", math.nan),
                   *(_column(serial, name, math.nan) for name in _PRICE_COLUMNS),
                   _column(serial, "volume", 0.0), _column(serial, "close_oi", 0.0)]
        size = len(columns[0])
        shift = self._shift(columns)
        if shift is None:
            rows: list[dict[str, Any] | None] = [None] * size
            versions = [0] * size
            texts: list[str | None] = [None] * size
            compute = list(range(size))
        else:
            rows = self._rows[shift:] + [None] * shift
            versions = self._row_versions[shift:] + [0] * shift
            texts = self._row_texts[shift:] + [None] * shift
            overlap = size - shift
            differs = np.zeros(overlap, dtype=bool)
            for new, old in zip(columns[1:], self._columns[1:], strict=True):
                differs |= ~_same(new[:overlap], old[shift:])
            compute = np.flatnonzero(differs).tolist() + list(range(overlap, size))

        # The window detach_klines keeps: the last `limit` valid rows; their time span picks the
        # calendar exactly as _annotate_trading_days does (first − 7 days … last + 21 days).
        times = columns[0]
        valid = np.isfinite(times) & (times >= 1)
        for prices in columns[1:5]:
            valid &= np.isfinite(prices)
        window = np.flatnonzero(valid)[-self.limit:].tolist()
        calendar_key = None
        days: list[date] = []
        if window:
            span = times[window]
            first = datetime.fromtimestamp(int(span.min()) / 1_000_000_000, tz=UTC).astimezone(CHINA_TIMEZONE)
            last = datetime.fromtimestamp(int(span.max()) / 1_000_000_000, tz=UTC).astimezone(CHINA_TIMEZONE)
            start, end = first - timedelta(days=7), last + timedelta(days=21)
            calendar_key = (start.date(), end.date())
            days = self._sorted_days(start, end)
            if calendar_key != self._calendar_key:
                compute = list(range(size))  # another calendar window: re-attribute every bar

        version = self.version + 1
        changed = shift is None
        removed = shift is None
        row_values = np.column_stack(columns) if compute else None
        start_row = window[0] if window else size  # rows left of the window render no bar
        for index in compute:
            bar = _base_bar(row_values[index].tolist()) if index >= start_row else None
            if bar is not None:
                local = datetime.fromtimestamp(bar["datetime"] / 1_000_000_000, tz=UTC).astimezone(CHINA_TIMEZONE)
                bar["tradingDay"] = trading_day_from_dates(local, days)
            previous = rows[index]
            if bar == previous:
                continue
            changed = True
            if previous is not None and bar is None:
                removed = True
            rows[index] = bar
            versions[index] = version
            texts[index] = bar_digest_text(bar) if bar is not None else None
        bars = [rows[index] for index in window]
        if not changed and (len(bars) != len(self.bars) or (bars and bars[0] is not self.bars[0])):
            changed = True  # only the left edge moved
        self._columns, self._calendar_key = columns, calendar_key
        self._rows, self._row_versions, self._row_texts = rows, versions, texts
        if changed or self.meta is None:
            self.version = version
            if removed or self.meta is None:
                self.floor = version
            self.bars = bars
            self.meta = SeriesVersion(
                epoch=self.epoch, version=version, floor=self.floor,
                versions=tuple(versions[index] for index in window),
                digest=digest_of_texts([texts[index] for index in window]),
            )
        return self.bars, self.meta

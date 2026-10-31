"""Bounded public display data, isolated from the simulation trading actor.

SDK subscriptions live until their owning API closes. A single serving worker
and at most one warm replacement therefore bound both retained subscriptions
and cold downloads, without ever touching the transaction command queue.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .history import data_only_api
from .main import (
    ALLOWED_KLINE_DURATIONS,
    CHINA_TIMEZONE,
    MAX_BOARD_QUOTES,
    MAX_EXTRA_QUOTES,
    FixtureSession,
    OfficialTqKqSession,
    ReaderGone,
    board_product_name,
    calendar_bucket,
    calendar_summary,
    finite_number,
    fixture_instrument_catalog,
    instrument_catalog_from_records,
    kline_data_length,
    last_outside_day_range,
    market_data_status,
    utc_now,
)

if TYPE_CHECKING:  # numpy (kline_series) ships with the tqkq extra; a fixture sidecar runs without it
    from .kline_series import SeriesRenderer, SeriesVersion

Key = tuple[str, int]
# A rendered workbench carries its bar versions under this key (kline_series.SeriesVersion); it never
# crosses the wire — the view publishes `barsVersion` and answers deltas from it.
SERIES_KEY = "_series"

# A quote-only subscription's first data can arrive a moment after get_quote: its row waits for
# that data (or this long) instead of answering an empty 盘前 row to the first reader.
BOARD_FIRST_DATA_SECONDS = 5.0
# A quote-only read that failed is tried again while the symbol is still wanted: after this long,
# doubling per failure up to BOARD_RETRY_MAX_SECONDS (one worker-owned attempt, whatever the readers).
BOARD_RETRY_SECONDS = 5.0
BOARD_RETRY_MAX_SECONDS = 300.0
# Wait topics (号池长轮询, 2026-10-14): a parked reader waits on its own topic and is woken only by
# a change to it — a series' topic is its Key; the quote-only rows (报价总表 and the extras channel),
# the catalog and the calendar have one each. A change with no topic (health, error, a new or closed
# worker) wakes every topic and the supervisor.
BOARD_TOPIC = "board"
CATALOG_TOPIC = "catalog"
CALENDAR_TOPIC = "calendar"
# The longest a parked reader sleeps between checks that its connection is still open (it used to
# wake every 250 ms; topic wake-ups now carry every data change, so this only bounds how long a
# thread outlives a reader that left).
READER_CHECK_SECONDS = 1.0


def _same_payload(previous: dict[str, Any] | None, payload: dict[str, Any]) -> bool:
    """Whether a fresh render changes nothing a reader sees. An incremental render returns the very
    same bars and SeriesVersion objects when no bar changed, so only its quote needs comparing."""
    if previous is None:
        return False
    meta = payload.get(SERIES_KEY)
    if meta is not None:
        return previous.get(SERIES_KEY) is meta and previous["quote"] == payload["quote"]
    return previous == payload


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class _WorkbenchView:
    """One published state of a series as readers see it, encoded once (扩容 ②): the whole window,
    and per reader base version the delta. Readers never receive this object itself, only bytes; the
    per-request `ageSeconds` is appended to the cached encoding."""

    def __init__(self, signature: tuple[Any, ...], body: dict[str, Any], meta: SeriesVersion | None) -> None:
        self.signature, self.body, self.meta = signature, body, meta
        self._lock = threading.Lock()
        self._full: bytes | None = None
        self._deltas: dict[int, bytes] = {}

    def encoded(self, bars_version: str | None) -> bytes:
        """The whole window, or — for a reader naming a version of this series still answerable —
        only the bars changed after it (`barsDelta`: those bars, the window's first bar time, its
        bar count and digest). Everything else in the view is always sent whole."""
        base = self.meta.base_of(bars_version) if self.meta is not None else None
        with self._lock:
            if base is None:
                if self._full is None:
                    self._full = _encode(self.body)
                return self._full
            cached = self._deltas.get(base)
            if cached is None:
                bars = self.body["bars"]
                cached = _encode({**{k: v for k, v in self.body.items() if k != "bars"}, "barsDelta": {
                    "base": bars_version, "bars": self.meta.changed_since(bars, base),
                    "first": bars[0]["datetime"] if bars else None, "count": len(bars),
                    "digest": self.meta.digest,
                }})
                if len(self._deltas) >= 8:
                    self._deltas.clear()
                self._deltas[base] = cached
            return cached


def _with_age(encoded: bytes, age: float) -> bytes:
    # A quote without a usable time has no age: null, never the non-JSON Infinity.
    value = json.dumps(age).encode() if math.isfinite(age) else b"null"
    return encoded[:-1] + b',"ageSeconds":' + value + b"}"


def _quote_has_data(quote: Any) -> bool:
    """The SDK entity has received its first data: a quote time or a 昨结算."""
    return bool(str(getattr(quote, "datetime", "") or "").strip()) or finite_number(
        getattr(quote, "pre_settlement", None)) is not None


def _board_row_has_data(row: dict[str, Any]) -> bool:
    """A board row rendered from a quote that had received data — `_quote_has_data` read off the row.
    A failed get_quote's placeholder has none, and neither has an entity still empty when
    BOARD_FIRST_DATA_SECONDS published it as-is (盘前/无成交 status, no quote time, no 昨结算)."""
    return row.get("status") != "market_quote_unavailable" and (
        bool(row.get("quoteTime")) or row.get("preSettlement") is not None)


class PublicMarketWorker:
    """All SDK calls, including constructor and close, run on this thread."""

    # Reuse the display renderer and calendar/session semantics. These helpers
    # never read private positions/orders or call trading methods.
    _annotate_trading_days = OfficialTqKqSession._annotate_trading_days
    _market_session_status = OfficialTqKqSession._market_session_status
    _session_trading_dates = OfficialTqKqSession._session_trading_dates
    _trading_status_fields = OfficialTqKqSession._trading_status_fields
    _connections_healthy = OfficialTqKqSession._connections_healthy
    _critical_connection = staticmethod(lambda connection_id: connection_id == "md")
    _workbench_quote = OfficialTqKqSession._workbench_quote
    _resolve_symbol = OfficialTqKqSession._resolve_symbol

    def __init__(self, credentials: tuple[str, str], changed: Callable[..., None], *,
                 fixture: bool = False, max_series: int = 48, max_cold: int = 8,
                 api_factory: Callable[..., Any] = data_only_api,
                 cold_slots: threading.BoundedSemaphore | None = None,
                 cold_timeout: float = 30, cold_retry_seconds: float = 1) -> None:
        self._credentials, self._changed = credentials, changed
        self.fixture, self.max_series, self.max_cold = fixture, max_series, max_cold
        self._factory = api_factory
        self._cold_slots = cold_slots or threading.BoundedSemaphore(max_cold)
        self._reserved = 0
        self.cold_timeout, self.cold_retry_seconds = cold_timeout, cold_retry_seconds
        self.failed_keys: set[Key] = set()
        self._attempts: dict[Key, int] = {}
        self._retry_at: dict[Key, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.keys: set[Key] = set()
        self.snapshots: dict[Key, dict[str, Any]] = {}
        self.catalog: list[dict[str, Any]] | None = None
        self.error: str | None = None
        self.healthy = True
        self.closed = False
        self.started = time.monotonic()
        self.loading = 0
        # 报价总表: quote-only rows for the main contracts, one get_quote each
        # — never inside the kline serial accounting above. The extras channel
        # (2026-10-03) rents the same storage for any catalog contract.
        self.board_rows: dict[str, dict[str, Any]] = {}
        self._board_wanted: set[str] = set()
        self._catalog_index: dict[str, dict[str, Any]] = {}
        self._board_quotes: dict[str, Any] = {}
        self._board_attempted: set[str] = set()
        self._board_dirty: set[str] = set()
        self._board_pending: dict[str, float] = {}
        self._board_failed: dict[str, tuple[float, int]] = {}  # symbol → (monotonic retry time, failures)
        self._board_inconsistent: set[str] = set()  # rows withheld for a last outside 最高/最低 (logged once)
        # The API never unsubscribes a quote: every symbol this worker ever
        # opened one for stays retained until the whole API closes. Rotating
        # extras leases therefore grow this set past the active-lease cap —
        # the supervisor replaces the generation once it exceeds its budget.
        self._quote_symbols: set[str] = set()
        self._calendar_summary: dict[str, Any] | None = None
        self._calendar_summary_key: Any = None
        self._calendar_wanted = False
        # What changed since the readers were last woken (series keys, BOARD_TOPIC). Written and sent
        # only on this worker's own thread: one wake-up per coalescing batch, never one per row.
        self._unsent: set[Any] = set()
        self._thread = threading.Thread(target=self._run, name="public-market-data", daemon=True)
        self._thread.start()

    def subscribe(self, key: Key) -> None:
        with self._lock:
            if key not in self.keys and len(self.keys) >= self.max_series:
                raise RuntimeError("market_subscription_limit")
            self.keys.add(key)

    def subscribe_board(self, symbols: set[str] | frozenset[str]) -> None:
        """Replace the quote-only channel's desired symbol set — the union of the
        board's mains (capped by the service) and the extras leases. The owner
        loop reconciles subscriptions and rows to it."""
        with self._lock:
            self._board_wanted = set(symbols)

    def board_read(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self.board_rows)

    def quotes_retained(self) -> int:
        """Distinct quote subscriptions this worker's API retains — the SDK keeps
        every get_quote until the API closes, so expired board/extras leases and
        rolled-away mains stay counted. This, not the live wanted set, is the
        generation's real quote footprint."""
        with self._lock:
            return len(self._quote_symbols)

    def calendar_read(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._calendar_summary) if self._calendar_summary else None

    def subscribe_calendar(self) -> None:
        """Mark the calendar summary as wanted (the service sets this while a
        read lease is live); the owner loop then keeps it current. Without it a
        worker never asks the SDK for the calendar window."""
        with self._lock:
            self._calendar_wanted = True

    def read(self) -> tuple[Any, dict[Key, dict[str, Any]], set[Key], str | None, bool]:
        with self._lock:
            return self.catalog, dict(self.snapshots), set(self.keys), self.error, self.healthy

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _trading_dates(self, start: Any, end: Any) -> set[str]:
        key = (start.date(), end.date())
        if key not in self._calendar_cache:
            if len(self._calendar_cache) >= self.max_series:
                self._calendar_cache.clear()
            self._calendar_cache[key] = OfficialTqKqSession._trading_dates(self, start, end)
        return self._calendar_cache[key]

    def _refresh_calendar_summary(self) -> None:
        """The exchange calendar summary, computed on the owner thread once per
        natural day and 18:00 boundary — HTTP readers only ever see the snapshot.
        Fixture mode knows only the weekend rule."""
        now = datetime.now(CHINA_TIMEZONE)
        key = calendar_bucket(now)
        if self._calendar_summary_key == key:
            return
        try:
            if self.fixture:
                summary = calendar_summary(now, None)
            else:
                # 60 days back for the recent days and backfill, 45 ahead so a
                # delivery reminder's ten trading days still fit across a long holiday.
                summary = calendar_summary(now, self._trading_dates(
                    now - timedelta(days=60), now + timedelta(days=45)))
        except Exception:
            return  # the next tick retries; readers keep the previous summary
        with self._lock:
            self._calendar_summary = summary
            self._calendar_summary_key = key
        self._changed()

    def _quote_for_symbol(self, symbol: str) -> Any:
        # Every dependency was awaited by _load, so the renderer cannot trigger
        # the synchronous SDK download path.
        return self.quotes[symbol]

    async def _catalog(self) -> None:
        try:
            symbols = sorted(set(await self.api.query_quotes(ins_class="FUTURE", expired=False)))
            main = set(await self.api.query_cont_quotes())
            frame = await self.api.query_symbol_info(symbols)
            catalog = instrument_catalog_from_records(frame.to_dict(orient="records"), main)
            self._catalog_symbols = {item["symbol"].upper(): item["symbol"] for item in catalog}
            self._catalog_index = {item["symbol"]: item for item in catalog}
            with self._lock:
                self.catalog = catalog
        except Exception:
            with self._lock:
                self.error = "instrument_catalog_unavailable"
        self._changed()

    def _note(self, topic: Any) -> None:
        self._unsent.add(topic)

    def _send_changes(self) -> None:
        """Wake the readers once for everything published since the last call."""
        if self._unsent:
            topics, self._unsent = self._unsent, set()
            self._changed(topics)

    def _publish(self, key: Key) -> None:
        try:
            if not self.api.is_serial_ready(self._kline_serials[key]):
                raise RuntimeError("market_series_not_ready")
            payload = self._render(*key)
        except Exception:
            self._fail_render(key)
            return
        with self._lock:
            self.failed_keys.discard(key)
            if _same_payload(self.snapshots.get(key), payload):
                return
            self.snapshots[key] = payload
        self._note(key)

    def _render(self, symbol: str, duration: int) -> dict[str, Any]:
        """The series' workbench: its bars kept in step with the SDK serial row by row (only rows
        that changed are rendered — kline_series.SeriesRenderer), its display quote rendered fresh.
        Same fields and values as OfficialTqKqSession.market_workbench renders in full."""
        from .kline_series import SeriesRenderer

        key = (symbol, duration)
        renderer = self._series.get(key)
        if renderer is None:
            renderer = self._series[key] = SeriesRenderer(kline_data_length(duration), self._sorted_trading_days)
        bars, meta = renderer.update(self._kline_serials[key])
        if not bars:
            raise RuntimeError("market_series_empty")
        return {"schemaVersion": 1, "durationSeconds": duration,
                "quote": self._workbench_quote(symbol, self._quote_for_symbol(symbol), bars),
                "bars": bars, SERIES_KEY: meta}

    def _sorted_trading_days(self, start: datetime, end: datetime) -> list[Any]:
        """The exchange calendar between two times, parsed and sorted once per window (the renderer
        bisects it per bar; see trading_day_from_dates)."""
        key = (start.date(), end.date())
        days = self._sorted_days_cache.get(key)
        if days is None:
            if len(self._sorted_days_cache) >= self.max_series:
                self._sorted_days_cache.clear()
            days = self._sorted_days_cache[key] = sorted(
                date.fromisoformat(value) for value in self._trading_dates(start, end))
        return days

    def _fail_render(self, key: Key) -> None:
        # One malformed series must not terminate the shared publisher. Keep
        # any usable old chart explicitly stale and request bounded recovery.
        with self._lock:
            self.failed_keys.add(key)
            previous = self.snapshots.get(key)
            if previous is not None:
                self.snapshots[key] = {**previous, "quote": {
                    **previous["quote"], "dataStatus": "stale",
                    "dataErrorCode": "market_series_render_failed",
                }}
        self._note(key)

    def _mark_dirty(self, key: Key) -> None:
        self._dirty.add(key)
        self._dirty_event.set()

    async def _flush(self) -> None:
        while True:
            await self._dirty_event.wait()
            # One bounded coalescing window, advanced by the SDK event loop:
            # a quiet socket cannot add an outer wait_update deadline to it.
            await asyncio.sleep(0.01)
            self._dirty_event.clear()
            dirty, self._dirty = self._dirty, set()
            board_dirty, self._board_dirty = self._board_dirty, set()
            for key in dirty:
                self._publish(key)
            for symbol in board_dirty:
                # The subscription's own first update can still carry no quote data.
                if symbol in self._board_pending and not _quote_has_data(self._board_quotes.get(symbol)):
                    continue
                self._board_pending.pop(symbol, None)
                self._publish_board(symbol)
            self._send_changes()

    def _refresh_session_status(self) -> None:
        # Session schedules advance even if a quiet market sends no ticks and
        # the account lacks the optional trading-status subscription permission.
        with self._lock:
            for key, payload in list(self.snapshots.items()):
                status = self._market_session_status(key[0])
                if payload["quote"].get("marketSessionStatus") != status:
                    self.snapshots[key] = {**payload, "quote": {
                        **payload["quote"], "marketSessionStatus": status,
                    }}
                    self._note(key)

    async def _load(self, key: Key) -> None:
        symbol, duration = key
        # Inside the SDK coroutine this public call returns its live entity
        # without a synchronous wait. Awaiting it would wait for today's first
        # quote and block a perfectly usable pre-open historical chart.
        self.quotes[symbol] = self.api.get_quote(symbol)
        with self._lock:
            self._quote_symbols.add(symbol)
        self._kline_serials[key] = await self.api.get_kline_serial(
            symbol, duration, data_length=kline_data_length(duration),
        )
        self._mark_dirty(key)
        # Publish first; optional status still uses this bounded cold slot.
        if symbol not in self._status_attempted:
            self._status_attempted.add(symbol)
            await self._load_status(symbol, key)

    async def _load_status(self, symbol: str, key: Key) -> None:
        try:
            self._trading_statuses[symbol] = await asyncio.wait_for(
                self.api.get_trading_status(symbol), timeout=5,
            )
            self._mark_dirty(key)
        except Exception:
            pass

    async def _load_board_batch(self, symbols: list[str]) -> None:
        # Like _load: inside the SDK coroutine get_quote returns its live
        # entity without a synchronous wait, so the board's quote-only
        # subscriptions stay off the transaction command queue.
        for symbol in symbols:
            try:
                self.quotes[symbol] = self.api.get_quote(symbol)
                with self._lock:
                    self._quote_symbols.add(symbol)
                self._board_quotes[symbol] = self.quotes[symbol]
                self._board_failed.pop(symbol, None)
                if _quote_has_data(self.quotes[symbol]):
                    self._publish_board(symbol)
                else:  # published by its first update, or as-is after BOARD_FIRST_DATA_SECONDS
                    self._board_pending[symbol] = time.monotonic()
            except Exception:
                failures = self._board_failed.get(symbol, (0.0, 0))[1] + 1
                delay = min(BOARD_RETRY_SECONDS * 2 ** (failures - 1), BOARD_RETRY_MAX_SECONDS)
                self._board_failed[symbol] = (time.monotonic() + delay, failures)
                self._publish_row(symbol, self._board_failed_row(symbol))
        self._send_changes()

    def _board_failed_row(self, symbol: str) -> dict[str, Any]:
        with self._lock:
            item = self._catalog_index.get(symbol) or {}
        instrument_name = str(item.get("instrumentName") or symbol)
        return {
            "symbol": symbol,
            "exchangeId": str(item.get("exchangeId") or symbol.partition(".")[0]),
            "productId": str(item.get("productId") or ""),
            "instrumentId": str(item.get("instrumentId") or symbol.partition(".")[2]),
            "instrumentName": instrument_name,
            "productName": board_product_name(instrument_name),
            "last": None, "preSettlement": None, "change": None, "changePercent": None,
            "volume": None, "openInterest": None, "openInterestChange": None,
            "high": None, "low": None, "upperLimit": None, "lowerLimit": None,
            "bidPrice1": None, "askPrice1": None,
            "quoteTime": None,
            "status": "market_quote_unavailable",
        }

    def _publish_row(self, symbol: str, row: dict[str, Any]) -> None:
        with self._lock:
            if self.board_rows.get(symbol) == row:
                return
            self.board_rows[symbol] = row
        self._note(BOARD_TOPIC)

    def _publish_board(self, symbol: str) -> None:
        # One symbol's bad quote must not take the board or the shared
        # publisher down: the row is marked failed, the rest stay normal.
        try:
            row = self._render_board_row(symbol)
        except Exception:
            row = self._board_failed_row(symbol)
        # 最新价须落在当日最高/最低之间 (#806, 2026-10-13): a row that breaks it is not shown — it is
        # published as unavailable (no price at all, never another number) and logged once per episode.
        # A guard only: the row's figures all come from one quote entity, so a real one never trips it.
        if last_outside_day_range(row.get("last"), row.get("high"), row.get("low")):
            if symbol not in self._board_inconsistent:
                self._board_inconsistent.add(symbol)
                sys.stderr.write(
                    f"tqkq-public-market board_quote_inconsistent symbol={symbol} last={row.get('last')} "
                    f"high={row.get('high')} low={row.get('low')} quoteTime={row.get('quoteTime')}\n")
                sys.stderr.flush()
            row = self._board_failed_row(symbol)
        else:
            self._board_inconsistent.discard(symbol)
        self._publish_row(symbol, row)

    def _render_board_row(self, symbol: str) -> dict[str, Any]:
        """One board row from the live quote entity: prices versus 昨结算, the
        same quote status semantics as the chart (正常 / 当日无成交 / 盘前)."""
        with self._lock:
            item = self._catalog_index.get(symbol) or {}
        quote = self._board_quotes[symbol]
        quote_time = str(getattr(quote, "datetime", "") or "").strip()
        pre_settlement = finite_number(getattr(quote, "pre_settlement", None))
        last = finite_number(getattr(quote, "last_price", None))
        open_interest = finite_number(getattr(quote, "open_interest", None))
        pre_open_interest = finite_number(getattr(quote, "pre_open_interest", None))
        instrument_name = str(item.get("instrumentName") or symbol)
        row = {
            "symbol": symbol,
            "exchangeId": str(item.get("exchangeId") or symbol.partition(".")[0]),
            "productId": str(item.get("productId") or ""),
            "instrumentId": str(item.get("instrumentId") or symbol.partition(".")[2]),
            "instrumentName": instrument_name,
            "productName": board_product_name(instrument_name),
            "last": last if last is not None and quote_time else None,
            "preSettlement": pre_settlement,
            "change": None,
            "changePercent": None,
            "volume": finite_number(getattr(quote, "volume", None)),
            "openInterest": open_interest,
            "openInterestChange": (open_interest - pre_open_interest
                                   if open_interest is not None and pre_open_interest is not None else None),
            "high": finite_number(getattr(quote, "highest", None)),
            "low": finite_number(getattr(quote, "lowest", None)),
            "upperLimit": finite_number(getattr(quote, "upper_limit", None)),
            "lowerLimit": finite_number(getattr(quote, "lower_limit", None)),
            # 手机报价列表的买价 / 卖价列 (2026-10-09): 一档，没有挂单时为 None.
            "bidPrice1": finite_number(getattr(quote, "bid_price1", None)),
            "askPrice1": finite_number(getattr(quote, "ask_price1", None)),
            "quoteTime": quote_time or None,
        }
        if row["last"] is None:
            # No live trade yet: the session clock picks the same code the
            # chart uses (当日无成交 while trading, 盘前 before the open).
            session_on = self._market_session_status(symbol) == "trading"
            row["status"] = "market_quote_no_trade" if session_on else "market_quote_pre_open"
            return row
        if pre_settlement:
            row["change"] = round(row["last"] - pre_settlement, 2)
            row["changePercent"] = round(row["change"] / pre_settlement * 100, 2)
        row["status"] = "normal"
        return row

    def _mark_changed(self) -> None:
        """After an update: every series whose quote, K-lines or trading status changed in it is
        re-rendered — the SDK's own ``is_changing`` on the owner thread. Update-notify channels
        (register_update_notify) did not fire in production on 2026-09-28, the first trading day of
        this worker: every series froze after its first publish while the actors' quotes moved."""
        for key, serial in list(self._kline_serials.items()):
            watched = (self.quotes.get(key[0]), serial, self._trading_statuses.get(key[0]))
            if any(item is not None and self.api.is_changing(item) for item in watched):
                self._mark_dirty(key)
        for symbol, quote in list(self._board_quotes.items()):
            if quote is not None and self.api.is_changing(quote):
                self._board_dirty.add(symbol)
        if self._board_dirty:
            self._dirty_event.set()

    async def _load_bounded(self, key: Key) -> None:
        try:
            await asyncio.wait_for(self._load(key), timeout=self.cold_timeout)
        except Exception:
            # The same SDK (symbol, duration) request reuses its serial.
            # Limit retries within one session; exhausted keys request a warm
            # session replacement without invalidating unrelated hot snapshots.
            if self._attempts[key] < 3:
                self._retry_at[key] = time.monotonic() + self.cold_retry_seconds * self._attempts[key]
            else:
                self.failed_keys.add(key)
            self._changed()
        finally:
            self.loading -= 1
            self._reserved -= 1
            self._cold_slots.release()

    def _run(self) -> None:
        api = None
        try:
            if self.fixture:
                self.catalog = fixture_instrument_catalog()
                session = FixtureSession([])
            else:
                if not all(self._credentials):
                    raise RuntimeError("tqkq_credentials_missing")
                api = self.api = self._factory(self._credentials)
                from tqsdk import TqNotify
                self._notifier = TqNotify(api)
                # SDK 3.10.2 initializes its calendar through synchronous HTTP
                # once. Do that on the API owner before any hot subscriptions;
                # rendering then only reads the SDK's initialized calendar.
                today = datetime.now(CHINA_TIMEZONE).date()
                api.get_trading_calendar(today, today)
                self._disconnected_connections: set[str] = set()
                self.quotes: dict[str, Any] = {}
                self._kline_serials: dict[Key, Any] = {}
                self._trading_statuses: dict[str, Any] = {}
                self._catalog_symbols: dict[str, str] = {}
                self._calendar_cache: dict[Any, set[str]] = {}
                self._sorted_days_cache: dict[Any, list[Any]] = {}
                self._series: dict[Key, SeriesRenderer] = {}
                self._dirty: set[Key] = set()
                self._dirty_event = asyncio.Event()
                self._status_attempted: set[str] = set()
                status_checked_at = 0.0
                api.create_task(self._flush())
                api.create_task(asyncio.wait_for(self._catalog(), timeout=30))
            attempted: set[Key] = set()
            self._changed()
            while not self._stop.is_set():
                if self._calendar_wanted:
                    self._refresh_calendar_summary()
                for key, retry_at in list(self._retry_at.items()):
                    if time.monotonic() >= retry_at:
                        attempted.discard(key)
                        del self._retry_at[key]
                with self._lock:
                    pending = sorted(self.keys - attempted)
                for key in pending[:max(0, self.max_cold - self.loading)]:
                    if not self.fixture and not self._cold_slots.acquire(blocking=False):
                        break
                    attempted.add(key)
                    if self.fixture:
                        with self._lock:
                            self.snapshots[key] = session.market_workbench(*key)
                        self._note(key)
                    else:
                        self._attempts[key] = self._attempts.get(key, 0) + 1
                        self.loading += 1
                        self._reserved += 1
                        api.create_task(self._load_bounded(key))
                # 报价总表 + 只订报价的 extras: reconcile the worker's quote-only
                # subscriptions to the wanted set (the service refreshes it with
                # every board/extras read and after each roll; symbols no longer
                # wanted — an expired extras lease, the old main after a roll —
                # drop their rows here).
                with self._lock:
                    board_wanted = set(self._board_wanted)
                    board_rendered = set(self.board_rows)
                board_removed = (self._board_attempted | board_rendered) - board_wanted
                for symbol in board_removed:
                    self._board_attempted.discard(symbol)
                    self._board_quotes.pop(symbol, None)
                    self._board_pending.pop(symbol, None)
                    self._board_failed.pop(symbol, None)
                for symbol, (retry_at, failures) in self._board_failed.items():
                    if time.monotonic() >= retry_at:  # due again: into this batch once (row stays meanwhile)
                        self._board_failed[symbol] = (math.inf, failures)
                        self._board_attempted.discard(symbol)
                for symbol, since in list(self._board_pending.items()):
                    if time.monotonic() - since >= BOARD_FIRST_DATA_SECONDS:
                        del self._board_pending[symbol]
                        self._publish_board(symbol)
                board_batch = sorted(board_wanted - self._board_attempted)
                if not self.fixture:
                    board_batch = board_batch[:self.max_cold]
                if board_removed or board_batch:
                    self._board_attempted.update(board_batch)
                    with self._lock:
                        for symbol in board_removed:
                            self.board_rows.pop(symbol, None)
                    if self.fixture:
                        with self._lock:
                            self.board_rows.update(session.market_board_rows(board_batch))
                    elif board_batch:
                        api.create_task(self._load_board_batch(board_batch))
                    self._note(BOARD_TOPIC)
                self._send_changes()
                if self.fixture:
                    self._stop.wait(0.05)
                else:
                    if api.wait_update(deadline=time.time() + 0.2):
                        self._mark_changed()
                    if time.monotonic() - status_checked_at >= 1:
                        self._refresh_session_status()
                        status_checked_at = time.monotonic()
                    healthy = self._connections_healthy()
                    with self._lock:
                        changed = self.healthy != healthy
                        self.healthy = healthy
                    if changed:
                        self._changed()
                self._send_changes()
        except Exception:
            with self._lock:
                self.error = "public_market_unavailable"
                self.healthy = False
            self._changed()
        finally:
            if api is not None:
                api.close()
            while self._reserved:
                self._reserved -= 1
                self._cold_slots.release()
            self.closed = True
            self._changed()


class PublicMarketService:
    def __init__(self, credentials: Callable[[], tuple[str, str]], *, fixture: bool = False,
                 worker_factory: Callable[..., Any] = PublicMarketWorker,
                 max_series: int = 48, max_cold: int = 8, lease_seconds: float = 30,
                 ready_wait: float = 5, recycle_seconds: float = 60,
                 warm_timeout: float = 15, retry_seconds: float = 30,
                 stale_after_seconds: float = 15) -> None:
        self._credentials, self._factory, self.fixture = credentials, worker_factory, fixture
        self.max_series, self.max_cold = max_series, max_cold
        self._cold_slots = threading.BoundedSemaphore(max_cold)
        self.lease_seconds, self.ready_wait = lease_seconds, ready_wait
        self.recycle_seconds, self.warm_timeout = recycle_seconds, warm_timeout
        self.retry_seconds, self.stale_after_seconds = retry_seconds, stale_after_seconds
        # One service lock. `_condition` on it wakes the supervisor; each wait topic has its own
        # condition on the same lock (the queue.Queue pattern: several conditions, one mutex), so a
        # change to one series wakes only that series' readers instead of every parked request.
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._topics: dict[Any, threading.Condition] = {}
        self._topic_waiters: dict[Any, int] = {}
        self._stop = False
        self._thread: threading.Thread | None = None
        self._worker: Any = None
        self._candidate: Any = None
        # A stopped worker stays referenced until its owning thread has closed
        # the SDK; a slow constructor/close can never create unlimited orphans.
        self._retiring: list[Any] = []
        self._leases: dict[Key, float] = {}
        self._catalog_lease = 0.0
        self._retry_at = 0.0
        self._views: dict[Key, tuple[Any, dict[str, Any]]] = {}
        self._board_lease = 0.0
        self._board_symbols: frozenset[str] = frozenset()
        self._board_view: tuple[list[dict[str, Any]], str] | None = None
        self._extra_leases: dict[str, float] = {}
        self._calendar_lease = 0.0
        self._calendar_cache: tuple[Any, dict[str, Any]] | None = None
        self._epoch = uuid.uuid4().hex
        self._revision = 0
        self._indexed_catalog: Any = None
        self._symbol_index: dict[str, str] = {}

    def _changed(self, topics: Iterable[Any] | None = None) -> None:
        """A worker's change: the readers of those topics wake; no topics wakes everyone."""
        with self._lock:
            if topics is None:
                self._wake_all()
                return
            for topic in topics:
                condition = self._topics.get(topic)
                if condition is not None:
                    condition.notify_all()

    def _wake_all(self) -> None:
        for condition in self._topics.values():
            condition.notify_all()
        self._condition.notify_all()

    def _wait(self, topic: Any, timeout: float) -> None:
        """Park the calling reader (service lock held) until its topic changes, everything wakes,
        or `timeout` passes. A topic's condition lives only while someone waits on it."""
        condition = self._topics.get(topic)
        if condition is None:
            condition = self._topics[topic] = threading.Condition(self._lock)
        self._topic_waiters[topic] = self._topic_waiters.get(topic, 0) + 1
        try:
            condition.wait(max(0.0, timeout))
        finally:
            left = self._topic_waiters[topic] - 1
            if left:
                self._topic_waiters[topic] = left
            else:
                del self._topic_waiters[topic]
                del self._topics[topic]

    def _new_worker(self, *, candidate: bool = False) -> Any:
        return self._factory(self._credentials(), self._changed, fixture=self.fixture,
                             max_series=self.max_series,
                             max_cold=min(2, self.max_cold) if candidate else self.max_cold,
                             cold_slots=self._cold_slots)

    def _ensure(self, wake: bool = True) -> None:
        """Start the supervisor; `wake` (a new interest — a key, symbol or lease that was not live)
        has it act now instead of on its next 0.2 s round. A renewed lease changes nothing it does."""
        if self._stop:
            raise RuntimeError("public_market_closed")
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="public-market-supervisor", daemon=True)
            self._thread.start()
        elif wake:
            self._condition.notify_all()

    def instruments(self) -> dict[str, Any]:
        snapshot = self._catalog_snapshot()
        return {**snapshot, "items": copy.deepcopy(snapshot["items"])}

    def _catalog_snapshot(self) -> dict[str, Any]:
        deadline = time.monotonic() + self.ready_wait
        with self._condition:
            now = time.monotonic()
            renewed = self._catalog_lease > now
            self._catalog_lease = now + self.lease_seconds
            self._ensure(wake=not renewed)
            while not self._stop:
                if self._worker:
                    catalog, _, _, error, healthy = self._worker.read()
                    if catalog is not None:
                        if self._indexed_catalog is not catalog:
                            self._indexed_catalog = catalog
                            self._symbol_index = {item["symbol"].upper(): item["symbol"] for item in catalog}
                        return {"schemaVersion": 1, "capturedAt": utc_now(),
                                "dataStatus": "normal" if healthy else "stale",
                                "isCached": not healthy, "catalogStatus": "ready",
                                "items": catalog}
                    if error:
                        raise RuntimeError(error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # The first SDK login/calendar/catalog download can exceed
                    # this bounded wait. Existing clients retry loading catalogs.
                    return {"schemaVersion": 1, "capturedAt": utc_now(),
                            "dataStatus": "connecting", "isCached": False,
                            "catalogStatus": "loading", "items": []}
                self._wait(CATALOG_TOPIC, remaining)
            raise RuntimeError("instrument_catalog_unavailable")

    def workbench(self, symbol: str, duration: int, *, after: str | None = None,
                  wait_ms: int = 0, bars_version: str | None = None) -> dict[str, Any]:
        """The workbench as a dict — the HTTP route sends workbench_body's bytes as they are."""
        return json.loads(self.workbench_body(symbol, duration, after=after, wait_ms=wait_ms,
                                              bars_version=bars_version))

    def workbench_body(self, symbol: str, duration: int, *, after: str | None = None, wait_ms: int = 0,
                       bars_version: str | None = None,
                       reader_gone: Callable[[], bool] | None = None) -> bytes:
        """One encoded workbench answer. `after` (the revision the reader holds) parks the request up
        to `wait_ms` until the view changes; `bars_version` (the barsVersion the reader holds) turns
        the bars into a delta when this series can still answer it. `reader_gone` ends a parked
        request whose reader has left (ReaderGone) instead of answering into a closed socket."""
        if duration not in ALLOWED_KLINE_DURATIONS:
            raise ValueError("invalid_market_duration")
        if self._catalog_snapshot()["catalogStatus"] != "ready":
            raise RuntimeError("market_series_not_ready")
        actual = self._symbol_index.get(symbol.upper())
        if actual is None:
            raise KeyError(symbol)
        key = (actual, duration)
        deadline = time.monotonic() + self.ready_wait
        wait_deadline: float | None = None
        with self._condition:
            now = time.monotonic()
            active = {key for key, until in self._leases.items() if until > now}
            if key not in active and len(active) >= self.max_series:
                raise RuntimeError("market_subscription_limit")
            # Do not retain rejected keys or expired leases: request volume does
            # not increase the bounded interest set.
            self._leases = {k: until for k, until in self._leases.items() if until > now}
            self._leases[key] = now + self.lease_seconds
            self._ensure(wake=key not in active)
            answer: tuple[_WorkbenchView, float] | None = None
            while not self._stop:
                found = self._view_entry(key)
                if found is not None:
                    if after != found[0].body["revision"]:
                        answer = found
                        break
                    if wait_deadline is None:
                        wait_deadline = time.monotonic() + min(10000, max(0, wait_ms)) / 1000
                    remaining = wait_deadline - time.monotonic()
                    if remaining <= 0:
                        answer = found
                        break
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if self._worker and key not in self._worker.read()[2]:
                            raise RuntimeError("market_subscription_limit")
                        raise RuntimeError("market_series_not_ready")
                if reader_gone is not None and reader_gone():
                    raise ReaderGone()
                self._wait(key, min(remaining, READER_CHECK_SECONDS))
            if answer is None:
                raise RuntimeError("public_market_closed")
        if wait_deadline is not None and reader_gone is not None and reader_gone():
            raise ReaderGone()  # it parked, and its reader left meanwhile: nothing to write into
        # Encoded outside the service lock, once per view (and per reader base for a delta).
        view, age = answer
        return _with_age(view.encoded(bars_version), age)

    def board(self) -> dict[str, Any]:
        """Every product's main contract in one table: one quote-only row each,
        catalog order, capped at MAX_BOARD_QUOTES (overflow truncated by that
        order and flagged). Reading refreshes the board lease — the same 30s
        interest window the workbench uses — after which the supervisor stops
        resubscribing and the quotes retire with the worker."""
        snapshot = self._catalog_snapshot()
        if snapshot["catalogStatus"] != "ready":
            raise RuntimeError("market_series_not_ready")
        mains = [item for item in snapshot["items"] if item.get("isMain") is True]
        truncated = len(mains) > MAX_BOARD_QUOTES
        wanted = [item["symbol"] for item in mains[:MAX_BOARD_QUOTES]]
        deadline = time.monotonic() + self.ready_wait
        rows: list[dict[str, Any]] = []
        status = "connecting"
        error: str | None = None
        with self._condition:
            now = time.monotonic()
            renewed = self._board_lease > now and self._board_symbols == frozenset(wanted)
            self._board_lease = now + self.lease_seconds
            self._board_symbols = frozenset(wanted)
            self._ensure(wake=not renewed)
            while not self._stop:
                if self._worker is not None:
                    _, _, _, error, healthy = self._worker.read()
                    reader = getattr(self._worker, "board_read", None)
                    rendered = reader() if callable(reader) else {}
                    rows = [rendered[symbol] for symbol in wanted if symbol in rendered]
                    status = "normal" if healthy and not error else "stale"
                    if len(rows) == len(wanted):
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._wait(BOARD_TOPIC, min(remaining, READER_CHECK_SECONDS))
            if self._stop:
                raise RuntimeError("public_market_closed")
        if not rows:
            raise RuntimeError(error or "market_series_not_ready")
        view = self._board_view
        if view is None or view[0] != rows:
            self._revision += 1
            view = (rows, f"{self._epoch}:{self._revision}")
            self._board_view = view
        return {"schemaVersion": 1, "capturedAt": utc_now(), "dataStatus": status,
                "revision": view[1], "truncated": truncated, "rows": rows}

    def quotes(self, symbols: Sequence[str]) -> dict[str, Any]:
        """只订报价的补充通道 (2026-10-03): any catalog contract — a non-main
        included — as a quote-only row through the board's renderer and storage,
        its own per-symbol 30s leases and its own cap (MAX_EXTRA_QUOTES), never a
        kline serial. An unknown symbol or a capped request answers that symbol
        with an errorCode row; the read as a whole only fails when the catalog or
        the worker is down. A worker generation change resubscribes through the
        same board path (the supervisor pushes the union of both wanted sets)."""
        snapshot = self._catalog_snapshot()
        if snapshot["catalogStatus"] != "ready":
            raise RuntimeError("market_series_not_ready")
        requested: list[str] = []
        seen: set[str] = set()
        for raw in symbols:
            text = str(raw).strip()
            if text and text.upper() not in seen:
                seen.add(text.upper())
                requested.append(text)
        rejected: list[dict[str, Any]] = []
        deadline = time.monotonic() + self.ready_wait
        rows: list[dict[str, Any]] = []
        status = "connecting"
        error: str | None = None
        with self._condition:
            now = time.monotonic()
            active = {symbol for symbol, until in self._extra_leases.items() if until > now}
            resolved: list[str] = []
            fresh = False
            for text in requested:
                actual = self._symbol_index.get(text.upper())
                if actual is None:
                    rejected.append({"symbol": text, "errorCode": "symbol_not_found"})
                elif actual not in active and len(active) >= MAX_EXTRA_QUOTES:
                    rejected.append({"symbol": actual, "errorCode": "market_subscription_limit"})
                else:
                    fresh = fresh or actual not in active
                    active.add(actual)
                    self._extra_leases[actual] = now + self.lease_seconds
                    resolved.append(actual)
            self._ensure(wake=fresh)
            while not self._stop:
                if self._worker is not None:
                    _, _, _, error, healthy = self._worker.read()
                    reader = getattr(self._worker, "board_read", None)
                    rendered = reader() if callable(reader) else {}
                    rows = [rendered[symbol] for symbol in resolved if symbol in rendered]
                    status = "normal" if healthy and not error else "stale"
                    if len(rows) == len(resolved):
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._wait(BOARD_TOPIC, min(remaining, READER_CHECK_SECONDS))
            if self._stop:
                raise RuntimeError("public_market_closed")
        if resolved and not rows:
            raise RuntimeError(error or "market_series_not_ready")
        return {"schemaVersion": 1, "capturedAt": utc_now(), "dataStatus": status,
                "rows": [*rows, *rejected]}

    def calendar(self) -> dict[str, Any]:
        """The exchange calendar summary for the replay default and the 休市
        refusal (2026-10-03). The worker computes it on its own thread once per
        natural day / 18:00 boundary; a read keeps the worker interested for one
        lease window, and the last summary of the current bucket stays served
        even after the worker retires."""
        bucket = calendar_bucket(datetime.now(CHINA_TIMEZONE))
        deadline = time.monotonic() + self.ready_wait
        with self._condition:
            now = time.monotonic()
            renewed = self._calendar_lease > now
            self._calendar_lease = now + self.lease_seconds
            self._ensure(wake=not renewed)
            while not self._stop:
                reader = getattr(self._worker, "calendar_read", None) if self._worker else None
                summary = reader() if callable(reader) else None
                if summary is not None:
                    self._calendar_cache = (bucket, summary)
                    return dict(summary)
                cached = self._calendar_cache
                if cached is not None and cached[0] == bucket:
                    return dict(cached[1])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._wait(CALENDAR_TOPIC, min(remaining, READER_CHECK_SECONDS))
            raise RuntimeError("market_calendar_unavailable")

    def _view(self, key: Key) -> dict[str, Any] | None:
        found = self._view_entry(key)
        return None if found is None else {**found[0].body, "ageSeconds": found[1]}

    def _view_entry(self, key: Key) -> tuple[_WorkbenchView, float] | None:
        if not self._worker:
            return None
        _, snapshots, _, error, healthy = self._worker.read()
        payload = snapshots.get(key)
        if payload is None:
            return None
        quote = payload["quote"]
        session = quote.get("marketSessionStatus", "unknown")
        status, age = market_data_status(
            (quote.get("dataStatus", "simulated" if self.fixture else "normal")
             if healthy and not error else "stale"),
            session, quote.get("quoteDatetime"), self.stale_after_seconds,
        )
        display_error = (error or ("public_market_disconnected" if not healthy else None)
                         or quote.get("dataErrorCode") or ("market_quote_stale" if status == "stale" else None))
        signature = (id(self._worker), id(payload), status, display_error, healthy)
        previous = self._views.get(key)
        if previous is None or previous.signature != signature:
            self._revision += 1
            meta = payload.get(SERIES_KEY)
            self._views[key] = previous = _WorkbenchView(signature, {
                **{name: value for name, value in payload.items() if name != SERIES_KEY},
                "quote": {**quote, "dataStatus": status, "dataErrorCode": display_error},
                "revision": f"{self._epoch}:{self._revision}",
                "capturedAt": utc_now(), "dataStatus": status,
                "marketSessionStatus": session, "isCached": status == "stale",
                "dataErrorCode": display_error,
                "liveTrading": "disabled",
                # The bars' own version (扩容 ②): a reader sending it back as `barsVersion` gets
                # only the bars changed since. Only incrementally rendered series carry one.
                **({"barsVersion": meta.token} if meta is not None else {}),
            }, meta)
        return previous, age

    def _run(self) -> None:
        while True:
            closing: list[Any] = []
            with self._condition:
                serving = self._worker
                if self._stop:
                    closing = [w for w in [self._worker, self._candidate, *self._retiring] if w]
                    self._worker = self._candidate = None
                else:
                    now = time.monotonic()
                    self._leases = {key: until for key, until in self._leases.items() if until > now}
                    active = set(self._leases)
                    self._extra_leases = {symbol: until for symbol, until in self._extra_leases.items()
                                          if until > now}
                    self._retiring = [worker for worker in self._retiring if not worker.closed]
                    self._views = {key: value for key, value in self._views.items() if key in active}
                    # The board's and the extras' wanted sets follow their own interest
                    # leases; the worker receives the union (both render into the same
                    # quote-only rows), and once every lease expires the push goes empty
                    # and the worker retires the rows.
                    board_active = self._board_lease > now and bool(self._board_symbols)
                    board_wanted = (self._board_symbols if board_active else frozenset()) | frozenset(
                        self._extra_leases)
                    interested = (bool(active) or self._catalog_lease > now
                                  or self._board_lease > now or self._calendar_lease > now
                                  or bool(self._extra_leases))
                    calendar_wanted = self._calendar_lease > now
                    if not interested:
                        closing = [w for w in [self._worker, self._candidate] if w]
                        self._worker = self._candidate = None
                    elif self._worker is None and not self._retiring and now >= self._retry_at:
                        self._worker = self._new_worker()
                    if self._worker:
                        _, snapshots, keys, error, healthy = self._worker.read()
                        for key in sorted(active - keys)[:max(0, self.max_series - len(keys))]:
                            self._worker.subscribe(key)
                        board_sync = getattr(self._worker, "subscribe_board", None)
                        if callable(board_sync):
                            board_sync(board_wanted)
                        calendar_sync = getattr(self._worker, "subscribe_calendar", None)
                        if callable(calendar_sync) and calendar_wanted:
                            calendar_sync()
                        retained = getattr(self._worker, "quotes_retained", None)
                        quotes_retired = callable(retained) and retained() > (
                            MAX_BOARD_QUOTES + 2 * MAX_EXTRA_QUOTES)
                        recycle = quotes_retired or (bool(keys - active) and (
                            len(keys) >= self.max_series or now - self._worker.started >= self.recycle_seconds))
                        if ((recycle or error or self._worker.closed or getattr(self._worker, "failed_keys", set())) and self._candidate is None
                                and not self._retiring and now >= self._retry_at):
                            self._candidate = self._new_worker(candidate=True)
                            self._retry_at = now + self.retry_seconds
                        if self._candidate:
                            catalog, warmed, candidate_keys, candidate_error, candidate_healthy = self._candidate.read()
                            for key in sorted(active - candidate_keys)[:max(0, self.max_series - len(candidate_keys))]:
                                self._candidate.subscribe(key)
                            candidate_board_sync = getattr(self._candidate, "subscribe_board", None)
                            if callable(candidate_board_sync):
                                candidate_board_sync(board_wanted)
                            candidate_calendar_sync = getattr(self._candidate, "subscribe_calendar", None)
                            if callable(candidate_calendar_sync) and calendar_wanted:
                                candidate_calendar_sync()
                            candidate_board_read = getattr(self._candidate, "board_read", None)
                            # board_wanted, not board_active: an extras-only interest (the board
                            # lease gone, leases live) still must be prewarmed on the candidate
                            # before the switch — the #456 replacement is exactly that case.
                            board_ready = not board_wanted or (
                                callable(candidate_board_read)
                                # A failed get_quote still publishes a placeholder row
                                # (#516-1), and an entity still empty after its first-data
                                # wait is published as-is (#516): only rows with data count —
                                # 盘前 rows carry preSettlement with a pre-open status and pass.
                                and board_wanted <= {symbol for symbol, row in candidate_board_read().items()
                                                     if _board_row_has_data(row)})
                            if (board_ready and catalog is not None and active <= warmed.keys() and candidate_healthy
                                    and not candidate_error and not (active & getattr(self._candidate, "failed_keys", set()))):
                                closing.append(self._worker)
                                self._worker, self._candidate = self._candidate, None
                            elif candidate_error or self._candidate.closed or now - self._candidate.started >= self.warm_timeout:
                                closing.append(self._candidate)
                                self._candidate = None
                    self._retiring.extend(closing)
                if self._worker is not serving:
                    # Readers re-read from the new worker (or see none); data changes wake them
                    # through their own topics, not through this loop.
                    self._wake_all()
            for worker in closing:
                worker.close()
            if self._stop:
                return
            with self._condition:
                self._condition.wait(0.2)

    def close(self) -> None:
        with self._condition:
            self._stop = True
            self._wake_all()
        if self._thread:
            self._thread.join(timeout=15)

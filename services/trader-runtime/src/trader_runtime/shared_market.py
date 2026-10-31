"""Public market sources and one producer per symbol/period, shared by all readers.

2026-10-04 多账号接力: the public market reads over an ordered source list
(`market_sources`, seeded once from the environment, then maintained online by
the admin). Every kline channel is pinned to one source at creation and stays
there; a source at its subscription limit or one that cannot be reached loses
channels to the next healthy source and never reclaims them after recovery.
Each source holds at most `market_capacity` kline channels, so the total is
48 × sources; only when every source is full does `guest_market_capacity` fire.
2026-10-13 扩容 ③: a new channel takes the first source still below the spread point
(`market_spread`, 16), else the least loaded one, so charts spread over several pool
processes; ② each producer reads only the bars its source changed since its last read.

2026-10-07 新浪备用: after the relay, and only when every source is down — in its
connection backoff, or answering that its TqKq connection is lost — quotes, the
board and chart K-lines come from Sina (`sina_market`), marked `dataStatus:
"backup"`. A closed market on a connected source is not down. A source whose read
says its TqKq connection is lost (within SOURCE_SIGNAL_SECONDS) is skipped like an
unreachable one: reads go to the next source that really serves, a chart pinned to
it moves to a healthy source with room (taking that slot before reading), and the
backup ends when a read is served by TqKq again. The status then says what that read
was: the provider's own status (`normal`) only once a healthy read takes over; while a
connected source that is full or warming up answers with its stale packet, `stale`
(not `backup`, not `normal`)."""
from __future__ import annotations

import asyncio
import bisect
import inspect
import logging
import math
import os
import secrets
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from . import fast_json
from .account_pool import PoolError
from .credential_box import CredentialKeyError
from .data_providers import _age_label, _utc_now
from .delivery_rules import contract_payload, delivery_notice, trading_today
from .foreign_market import CHART_SOURCE_PREFIX as FOREIGN_CHART_SOURCE
from .foreign_market import cme_chart
from .instruments import FOREIGN_PRODUCTS, cme_hours_of, foreign_product_of, foreign_tradable, strategy_scope_symbol
from .last_good import read_json, write_json_atomic
from .sina_market import (
    BACKUP_NOTICE,
    BACKUP_SOURCE,
    BACKUP_STATUS,
    SinaBackup,
    SinaUnavailable,
    board_row,
    quote_payload,
)
from .sina_market import revision as backup_revision
from .trading_calendar import bar_trading_day, cme_rule, cme_schedule, daily_bar_day, summary_trading_days

log = logging.getLogger(__name__)

# 30 分 (1800) 与周线 (604800) joined the display periods 2026-10-02 (TqSdk takes both directly).
# The most bars a chart packet carries: one full trading day of 1-minute bars for 分时 (the sidecar's
# TIMESHARE_DATA_LENGTH; a gold night+day runs ~555 minutes). Other periods arrive with 256.
MAX_BARS = 600
DURATIONS = {60, 180, 300, 900, 1800, 3600, 86400, 604800}
# preSettlement crosses for the 分时图's 昨结价 reference line and its change-% baseline.
QUOTE_FIELDS = set("symbol name price lastPrice changePercent updatedAt capturedAt ageSeconds dataStatus dataErrorCode trend quoteDatetime priceTick volumeMultiple upperLimit lowerLimit tradingStatus tradingStatusLabel open highest lowest volume openInterest preSettlement marketSessionStatus openLimit marginPerLot commissionPerLot source".split()) | {f"{prefix}{i}" for prefix in ("bidPrice", "askPrice", "bidVolume", "askVolume") for i in range(1, 6)}
BAR_FIELDS = set("datetime open high low close volume openInterest tradingDay".split())
INSTRUMENT_FIELDS = set("symbol exchangeId instrumentId instrumentName productId priceTick volumeMultiple deliveryYear deliveryMonth expireRestDays hasNight isMain openLimit".split())
META_FIELDS = set("schemaVersion capturedAt dataStatus dataErrorCode isCached ageSeconds durationSeconds marketSessionStatus catalogStatus source".split())
# A source read answering with one of these says its own TqKq connection is lost
# (public_market.py `_view`): the source is down for the backup decision.
SOURCE_DOWN_CODES = {"public_market_disconnected", "public_market_unavailable", "instrument_catalog_unavailable"}
# 外盘只认一份目录 (所有者 2026-10-19): of 快期's 海外主连 (exchange KQD) the catalog lists only the products
# instruments.py carries — the ones the 外盘 board, the AI and the chart all read the same way.
FOREIGN_CHART_SYMBOLS = frozenset(item.kqd.upper() for item in FOREIGN_PRODUCTS if item.kqd)


def foreign_listed(item: dict) -> bool:
    return str(item.get("exchangeId", "")).upper() != "KQD" or str(item.get("symbol", "")).upper() in FOREIGN_CHART_SYMBOLS


# Relay errors the backup may stand in for; capacity and unknown symbols never.
BACKUP_TRIGGERS = {"guest_market_source_unavailable", "market_series_not_ready"}
# How long one observation of a source (healthy or lost) stands before it is read again.
SOURCE_SIGNAL_SECONDS = 30
# 选源分散 (扩容 ③, 2026-10-13): a source takes new K-line channels in list order only up to this many;
# past it every new channel goes to the least loaded source with room, so the charts spread over
# several pool processes (each its own core) instead of all filling the first one up to its 48.
# AI_TRADER_MARKET_SOURCE_SPREAD overrides; ≥ the hard cap restores the old fill-the-first order.
DEFAULT_SOURCE_SPREAD = 16
# A source this close to the spread point warms the next idle source (pool child login, catalog),
# so the chart that spills over does not wait for a cold login.
SOURCE_PREWARM_MARGIN = 2
# Backup failures that mean Sina itself is unusable (vs. one contract Sina does not carry,
# which must not switch the whole market's status off).
BACKUP_DOWN_REASONS = {"sina_quote_unavailable", "sina_bars_unavailable", "backup_quotes_empty",
                       "backup_catalog_unavailable"}
BOARD_ROW_FIELDS = set("symbol instrumentId instrumentName productName exchangeId productId last preSettlement change changePercent volume openInterest openInterestChange high low upperLimit lowerLimit bidPrice1 askPrice1 quoteTime status".split())


def project(value, fields):
    # Only scalar fields cross the public boundary, never nested account payloads.
    return {k: v for k, v in value.items() if k in fields and (v is None or isinstance(v, (str, int, float, bool)))}


def trading_schedule(value):
    """The quote's trading-time schedule ({"day": [["09:00:00", "10:15:00"], …],
    "night": […]}) — the one nested quote field allowed across the boundary, and only
    when every period is a pair of time strings; anything else is dropped. A CME schedule
    keeps its ``"dayRule": "cme"`` and ``"cmeHours"`` (外盘：交易日按美东 18:00 换日、时段按品种，外盘盘点 D / F2)."""
    if not isinstance(value, dict):
        return None
    result = {}
    for part in ("day", "night"):
        periods = value.get(part)
        if not isinstance(periods, list):
            return None
        clean = []
        for period in periods:
            if not (isinstance(period, (list, tuple)) and len(period) == 2
                    and all(isinstance(item, str) and item for item in period)):
                return None
            clean.append([period[0][:5], period[1][:5]])  # HH:MM is all the card shows
        result[part] = clean
    result.update(cme_rule(value))
    return result


def event(name, value):
    return "event: " + name + "\ndata: " + fast_json.dumps(value) + "\n\n"


def cme_listed(symbol: object) -> bool:
    """快期外盘主连里能在美元子账户交易（按 CME 时段撮合）的品种。"""
    text = str(symbol or "")
    product = foreign_product_of(text) if text.upper().startswith("KQD.") else None
    return product is not None and foreign_tradable(product)


class MarketPacket(dict):
    """A published chart packet. Its guest SSE texts (`snapshot`, `update`) are encoded on first use
    and kept (2026-10-13 扩容①): only a guest stream reads them, so a Runtime without guest readers
    never encodes them — members get their frames from runtime_feed."""

    def __missing__(self, key):
        if key == "snapshot":
            value = event("snapshot", self["value"])
        elif key == "update":
            patch = self["patch"]
            value = self["snapshot"] if patch is None else event("update", patch)
        else:
            raise KeyError(key)
        self[key] = value
        return value


def market_patch(previous, current, bars_change=None):
    """No existing feed patch contract: send changed market scalars and changed bars only.

    `bars_change` (扩容 ②): when the producer built the new bars from a source delta it already knows
    which bars differ from the previous packet's (in window order) and whether the window's left edge
    moved — the same barsPatch then comes without comparing the whole window."""
    volatile = {"capturedAt", "ageSeconds", "updatedAt"}
    patch = {}
    for section in ("quote", "workbench"):
        old, new = previous[section], current[section]
        changes = {k: v for k, v in new.items() if k not in volatile | {"bars"} and old.get(k) != v}
        if section == "workbench" and bars_change is not None:
            changed_bars, trimmed = bars_change
            if changed_bars or trimmed:
                changes["barsPatch"] = {"bars": changed_bars, "trimBefore": new["bars"][0]["datetime"]}
        elif section == "workbench":
            old_bars, new_bars = old.get("bars", []), new.get("bars", [])
            if old_bars != new_bars:
                old_by_time = {bar["datetime"]: bar for bar in old_bars}
                new_times = {bar["datetime"] for bar in new_bars}
                if not new_bars or any(t >= new_bars[0]["datetime"] and t not in new_times for t in old_by_time):
                    return None  # history replaced or cleared: one fresh snapshot
                changes["barsPatch"] = {"bars": [bar for bar in new_bars if old_by_time.get(bar["datetime"]) != bar],
                                        "trimBefore": new_bars[0]["datetime"]}
        if changes:
            patch[section] = new if section == "quote" else changes
    return patch


# 本地 K 线缓存的增量读取 (2026-10-12): the browser keeps a chart's closed bars (IndexedDB, the client's
# marketCache.ts) and asks only for what came after the last one it holds. The digest lets it prove its
# copy still equals the Runtime's window — a corrected bar, a changed trading-day attribution or a source
# switch shows as a mismatch and the client reads the whole window again.
_DIGEST_FIELDS = ("open", "high", "low", "close", "volume", "openInterest")


def _digest_number(value, divisor: float = 1.0, multiplier: float = 1.0) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "-"
    # The same IEEE double steps the browser takes (its JSON numbers are doubles): scale, + 0.5, floor.
    return str(math.floor(float(value) / divisor * multiplier + 0.5))


def bar_digest_text(bar) -> str:
    return ",".join([_digest_number(bar.get("datetime"), divisor=1e9),
                     *(_digest_number(bar.get(name), multiplier=10_000) for name in _DIGEST_FIELDS),
                     bar.get("tradingDay") if isinstance(bar.get("tradingDay"), str) else ""])


def _digest_of_texts(texts) -> str:
    return f"{zlib.crc32(';'.join(texts).encode('utf-8')):08x}"


def bars_digest(bars) -> str:
    """CRC-32 (zlib, hex) of the bars' canonical text: per bar the time in whole seconds, OHLC/volume/open
    interest in 1/10000 units and the trading day; `,` between fields, `;` between bars. The client's
    `barsDigest` (marketCache.ts) and the sidecar's (kline_series.bars_digest, the source deltas'
    window digest) build the same text."""
    return _digest_of_texts([bar_digest_text(bar) for bar in bars])


class SourceBars:
    """The bars a K-line producer holds from its pinned source, with the source's bars version (扩容 ②,
    2026-10-13). A source that versions its bars (`barsVersion`) answers a reader naming its version
    with only the bars changed since (`barsDelta`): this applies them — bars before the window's new
    first bar dropped, the rest replaced or inserted by time — and proves the result is the source's
    window (bar count, first bar, digest). Never mutated: every applied delta is a new instance, so a
    packet already published keeps its bars."""

    __slots__ = ("token", "bars", "times", "texts")

    def __init__(self, token: str, bars: list, times: list, texts: list):
        self.token, self.bars, self.times, self.texts = token, bars, times, texts

    @classmethod
    def whole(cls, token, bars):
        """The state after a whole-window answer; None when the source does not version its bars
        (an older sidecar) or the window is longer than a packet carries — no deltas then."""
        if not isinstance(token, str) or not token or len(bars) > MAX_BARS:
            return None
        times = [bar.get("datetime") for bar in bars]
        if not all(isinstance(t, int) for t in times) or any(a >= b for a, b in zip(times, times[1:])):
            return None
        return cls(token, bars, times, [bar_digest_text(bar) for bar in bars])

    def apply(self, token, delta):
        """(new state, (bars that differ from this state's, whether the left edge moved)), or None when
        the delta does not rebuild the source's window — the producer then reads the whole window."""
        if not isinstance(token, str) or not token or not isinstance(delta, dict):
            return None
        first, count, digest, upserts = delta.get("first"), delta.get("count"), delta.get("digest"), delta.get("bars")
        if not isinstance(count, int) or not isinstance(upserts, list) or not 0 < count <= MAX_BARS:
            return None
        if not isinstance(first, int):
            return None
        cut = bisect.bisect_left(self.times, first)
        bars, times, texts = self.bars[cut:], self.times[cut:], self.texts[cut:]
        changed = []
        for raw in upserts:
            if not isinstance(raw, dict) or not isinstance(raw.get("datetime"), int):
                return None
            bar = project(raw, BAR_FIELDS)
            index = bisect.bisect_left(times, bar["datetime"])
            if index < len(times) and times[index] == bar["datetime"]:
                if bars[index] == bar:
                    continue
                bars[index], texts[index] = bar, bar_digest_text(bar)
            else:
                bars.insert(index, bar)
                times.insert(index, bar["datetime"])
                texts.insert(index, bar_digest_text(bar))
            changed.append(bar)
        if len(bars) != count or times[0] != first or _digest_of_texts(texts) != digest:
            return None
        return SourceBars(token, bars, times, texts), (changed, cut > 0)


def workbench_since(workbench, since):
    """The workbench with only the bars after `since` (the datetime of the last closed bar the reader holds),
    plus `barsAfter` and `barsWindow` {first, count, digest of the bars up to and including `since`}. A
    `since` this window does not hold (too old, unknown) answers the whole workbench unchanged."""
    bars = workbench.get("bars") or []
    index = next((i for i, bar in enumerate(bars) if bar.get("datetime") == since), None)
    if index is None:
        return workbench
    return {**workbench, "bars": bars[index + 1:], "barsAfter": since,
            "barsWindow": {"first": bars[0]["datetime"], "count": len(bars), "digest": bars_digest(bars[:index + 1])}}


@dataclass
class MarketChannel:
    symbol: str
    duration: int
    refs: int = 0
    packet: dict | None = None
    projections: dict = field(default_factory=dict)
    error: HTTPException | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    retirement: asyncio.TimerHandle | None = None
    source: str | None = None  # the relay source this channel is pinned to
    # Set once by retire_market: the producer stops and nothing pins the channel again, even when its
    # cancellation was lost on the way (2026-10-12: retired channels kept counting against a source).
    retired: bool = False
    # Since when the channel has been without a good read (its producer's start, a failed read or a move
    # to another source): within series_wait_seconds a loading series or a source move is "not yet".
    waiting_since: float | None = None

    def notify(self):
        changed, self.changed = self.changed, asyncio.Event()
        changed.set()


@dataclass
class SourceState:
    """One relay source's live load and health (2026-10-04): how many channels
    are pinned to it, whether it reported its subscription limit, and the
    exponential-backoff recheck of an unreachable source."""

    account_id: str
    channels: int = 0
    full_until: float = 0
    retry_at: float = 0
    failures: int = 0
    quote_symbols: int = 0
    last_error: str | None = None
    last_error_at: str | None = None
    observed_at: float = 0  # the last read that told whether its TqKq connection is up
    connection_lost: bool = False


class SharedMarketService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.market_account_id = (os.getenv("AI_TRADER_SHARED_MARKET_ACCOUNT_ID", "").strip()
                                  or os.getenv("AI_TRADER_GUEST_MARKET_ACCOUNT_ID", "").strip())
        self.env_source_ids = [item.strip() for item in
                               os.getenv("AI_TRADER_SHARED_MARKET_ACCOUNT_IDS", "").split(",") if item.strip()]
        self.cache = OrderedDict()
        self._since_answers: OrderedDict = OrderedDict()
        self.inflight = {}
        self.shared_waiters = {}
        self.markets: dict[tuple[str, int], MarketChannel] = {}
        self.market_tasks: set[asyncio.Task] = set()
        self.market_capacity = 48
        try:
            self.market_spread = max(1, int(os.getenv("AI_TRADER_MARKET_SOURCE_SPREAD", "") or DEFAULT_SOURCE_SPREAD))
        except ValueError:
            self.market_spread = DEFAULT_SOURCE_SPREAD
        self._prewarming: set[str] = set()
        # Upstream reads in flight, per kind (2026-10-12): catalog, board, calendar, quotes and the pool's
        # source lookups each have their own budget, so a burst of one kind never 429s another. K-line
        # long polls are not shared reads: their channels are capped per source (market_capacity) and
        # ride their own connection pool (sidecar_http).
        self.inflight_limit = 32
        # Quote rows are cached per contract, and a contract already being read joins that read.
        self.quote_pending: dict[str, tuple[tuple, asyncio.Task]] = {}
        self.cache_limit = 512  # one entry per quoted contract plus the few shared reads
        self.market_idle_seconds = 2
        self.fake_interval = .1
        # 冷序列 (2026-10-14): how long a chart's readers wait for its first frame, and how long a series
        # its source is still loading (`market_series_not_ready`, or a move to another source) counts as
        # "not yet" rather than a failure — see produce_market.
        self.series_wait_seconds = 20
        self.closed = False
        self.clock = time.time
        # 接力状态：有序源清单（库为准，5 秒内复用一次读取）、每源负载与后端健康。
        self._source_order: list[str] = []
        self._source_kinds: dict[str, str] = {}
        self._sources_read = 0.0
        self.source_states: dict[str, SourceState] = {}
        self.full_recheck_seconds = 60
        self._producing: ContextVar[MarketChannel | None] = ContextVar("producing_market_channel")
        # 新浪备用 (2026-10-07): on by default for the real data modes, off for fake and
        # replay; AI_TRADER_MARKET_BACKUP=off|sina overrides.
        backup = os.getenv("AI_TRADER_MARKET_BACKUP", "").strip().lower()
        if not backup:
            backup = "off" if getattr(getattr(ctx, "settings", None), "data_mode", "fake") == "fake" else "sina"
        self.backup: SinaBackup | None = SinaBackup() if backup == "sina" else None
        self.backup_interval = 2.0
        # 外盘图表 (所有者 2026-10-19 方案 A): ForeignMarketService.chart, bound in lifespan. A 快期外盘主连 chart at
        # a minute-to-hour period is read from MT5's main month; after an MT5 failure that symbol stays on 快期 for
        # foreign_backoff seconds so a flaky bridge never flips one chart between two sources.
        self.foreign_chart = None
        # 外盘盘点 D (2026-10-21): ForeignMarketService.chart_quote, bound in lifespan — the MT5 main-month quote a
        # tradable 外盘主连 chart's daily/weekly snapshot carries (foreign_market.cme_chart); such a channel's long
        # poll is cut to foreign_wait_ms so that quote stays as fresh as the minute charts'.
        self.foreign_quote = None
        self.foreign_wait_ms = 2000
        self.foreign_interval = 2.0
        self.foreign_backoff = 60.0
        self._foreign_down: dict[str, float] = {}
        # What the shared market last served, as the status shows it: BACKUP_STATUS (Sina),
        # "stale" (a stale TqKq packet right after the backup, until a healthy read) or None.
        self.served_status: str | None = None
        self.backup_listeners: list[Callable[[], None]] = []
        self._last_catalog: dict | None = None
        # The last good catalog also lives on disk, so a Runtime started during an outage
        # still knows the contracts' names, ticks, sessions and mains.
        state_root = getattr(getattr(ctx, "settings", None), "state_root", None)
        self._catalog_file = Path(state_root) / "market-backup-catalog.json" if state_root else None
        self._catalog_saved: int | None = None
        # The last good trading calendar, same idea: a read that fails (sidecar down) serves it marked stale.
        self._calendar_file = Path(state_root) / "market-calendar.json" if state_root else None
        self._calendar_saved: dict | None = None

    def _purge_cache(self):
        now = self.clock()
        for old in [key for key, (expires, _) in self.cache.items() if expires <= now]:
            del self.cache[old]

    def _start_shared(self, key, fetch, ttl):
        """The in-flight read for `key`, started when there is none: at most `inflight_limit` reads of
        one kind (`key[0]`) at a time, past that 429 — other kinds keep their own budget."""
        if key not in self.inflight:
            if sum(1 for other in self.inflight if other[0] == key[0]) >= self.inflight_limit:
                raise HTTPException(429, "guest_market_capacity")

            async def run():
                try:
                    value = fetch() if inspect.iscoroutinefunction(fetch) else await asyncio.to_thread(fetch)
                    if inspect.isawaitable(value):
                        value = await value
                    if ttl > 0:
                        self._cache_put(key, value, ttl)
                    return value
                finally:
                    if self.inflight.get(key) is asyncio.current_task():
                        self.inflight.pop(key, None)
            self.inflight[key] = asyncio.create_task(run())
        return self.inflight[key]

    def _cache_put(self, key, value, ttl):
        self.cache[key] = (self.clock() + ttl, value)
        self.cache.move_to_end(key)
        while len(self.cache) > self.cache_limit:
            self.cache.popitem(last=False)

    async def _join_shared(self, key, task):
        """Wait for a shared read; the last waiter to leave cancels it."""
        self.shared_waiters[task] = self.shared_waiters.get(task, 0) + 1
        try:
            return await asyncio.shield(task)
        finally:
            self.shared_waiters[task] -= 1
            if not self.shared_waiters[task]:
                self.shared_waiters.pop(task)
                if not task.done():
                    if self.inflight.get(key) is task:
                        self.inflight.pop(key)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def shared(self, key, fetch, ttl=1):
        if self.closed:
            raise HTTPException(503, "guest_market_closed")
        self._purge_cache()
        if key in self.cache:
            return self.cache[key][1]
        return await self._join_shared(key, self._start_shared(key, fetch, ttl))

    def _fallback_source_ids(self) -> list[str]:
        """The pre-seed view: the plural env list, else the legacy single id
        (`market_account_id` stays a plain writable attribute so deploy-time
        overrides keep working until the list is seeded or admin-managed)."""
        return list(self.env_source_ids) or ([self.market_account_id] if self.market_account_id else [])

    def _ordered_sources(self) -> list[str]:
        """The ordered source list, database-first (5s reuse). Empty fallback
        lists are not seeded: a later env or attribute change still applies."""
        if self.clock() - self._sources_read < 5:
            return self._source_order
        self._sources_read = self.clock()
        services = getattr(self.ctx, "services", None)
        store = getattr(services, "store", None)
        fallback = self._fallback_source_ids()
        if store is None:
            self._source_order = fallback
        elif store.market_sources_seeded():
            self._source_order = store.list_market_sources()
        elif fallback:
            store.seed_market_sources(fallback)
            self._source_order = store.list_market_sources()
        else:
            self._source_order = []
        for account_id in self._source_order:
            self.source_states.setdefault(account_id, SourceState(account_id))
            record = store.get_trading_account(account_id) if store is not None else None
            # 没有账户行的源按池路由（历史绑定账户在库里，测试桩在库外）。
            self._source_kinds[account_id] = str((record or {}).get("provider_kind") or "tqkq-pool")
        return self._source_order

    def reload_sources(self):
        """Drop the list cache so the next read sees the admin's new order."""
        self._sources_read = 0

    def ordered_sources(self) -> list[str]:
        """The ordered source list as the relay reads it — also the paper engine's
        market-account order (paper_accounts.PaperMarketSourceSync)."""
        return list(self._ordered_sources())

    def _source_state(self, account_id: str) -> SourceState:
        return self.source_states.setdefault(account_id, SourceState(account_id))

    def _ready_sources(self, sources) -> list[str]:
        now = self.clock()
        return [account_id for account_id in sources if now >= self._source_state(account_id).retry_at]

    def _known_lost(self, account_id: str) -> bool:
        """Its last read (within SOURCE_SIGNAL_SECONDS) said its TqKq connection is lost."""
        state = self._source_state(account_id)
        return state.connection_lost and self.clock() - state.observed_at <= SOURCE_SIGNAL_SECONDS

    def _pick_source(self, *, connected_only: bool = False) -> str | None:
        """A healthy (past its backoff) source below its channel cap for a new
        channel — healthy-but-full sources still serve reads, just never take a
        new channel. Among them (扩容 ③): the first in list order still below the
        spread point, else the least loaded (ties in list order). A source known
        to have lost its TqKq connection comes only after every other one (or
        never, with `connected_only`)."""
        now = self.clock()
        with_room = [account_id for account_id, state in
                     ((account_id, self._source_state(account_id)) for account_id in self._ordered_sources())
                     if now >= state.retry_at and now >= state.full_until and state.channels < self.market_capacity]
        connected = [account_id for account_id in with_room if not self._known_lost(account_id)]
        lost = [account_id for account_id in with_room if account_id not in connected]
        for tier in (connected,) if connected_only else (connected, lost):
            if tier:
                return self._spread_pick(tier)
        return None

    def _spread_pick(self, candidates: list[str]) -> str:
        spread = min(self.market_spread, self.market_capacity)
        below = next((account_id for account_id in candidates if self._source_state(account_id).channels < spread), None)
        if below is not None:
            return below
        return min(candidates, key=lambda account_id: self._source_state(account_id).channels)

    def _prewarm_next(self, picked: str) -> None:
        """When `picked` nears the spread point, read the catalog once from the next idle source in list
        order: a pool account's child starts (login) and its public worker loads, before a chart needs it.
        A 行情专用 account's child therefore still starts only when the sources before it fill up."""
        spread = min(self.market_spread, self.market_capacity)
        if self.closed or self._source_state(picked).channels < spread - SOURCE_PREWARM_MARGIN:
            return
        now = self.clock()
        target = next((account_id for account_id in self._ordered_sources()
                       if account_id != picked and self._source_state(account_id).channels < spread
                       and now >= self._source_state(account_id).retry_at), None)
        if target is None or self._source_state(target).channels or target in self._prewarming:
            return  # the next one already serves charts (it is warm), or nothing is left to warm
        self._prewarming.add(target)

        async def warm():
            try:
                await self._read_source(target, "/v1/public-market/instruments")
            except Exception:  # noqa: BLE001 — a failed warm-up is recorded on the source's state, nothing more
                pass
            finally:
                self._prewarming.discard(target)
        task = asyncio.get_running_loop().create_task(warm())
        self.market_tasks.add(task)
        task.add_done_callback(self.market_tasks.discard)

    def _serving_order(self, sources) -> list[str]:
        """Ready sources for an unpinned read: connected (or unknown) ones first in
        order, then the ones known to have lost their TqKq connection."""
        ready = self._ready_sources(sources)
        return ([account_id for account_id in ready if not self._known_lost(account_id)]
                + [account_id for account_id in ready if self._known_lost(account_id)])

    async def _read_serving(self, sources, path: str) -> tuple[str, dict, list[str]]:
        """One unpinned read from the first source that really serves: a source
        answering that its TqKq connection is lost is passed over for the next
        one; its stale answer stands only when no other source does better (the
        backup decision then sees every source down). Returns the source, its
        answer and the connected sources after it."""
        order = self._serving_order(sources)
        failure: HTTPException | None = None
        stale: tuple[str, dict, list[str]] | None = None
        for index, account_id in enumerate(order):
            if stale is not None and self._known_lost(account_id):
                break  # already holding a stale answer: another lost source adds nothing
            try:
                result = await self._read_source(account_id, path)
            except HTTPException as error:
                failure = error
                continue
            rest = [later for later in order[index + 1:] if not self._known_lost(later)]
            if self._payload_disconnected(path, result):
                stale = stale or (account_id, result, rest)
                continue
            return account_id, result, rest
        if stale is not None:
            return stale
        raise failure if failure is not None else HTTPException(503, "guest_market_source_unavailable")

    def _claim_source_slot(self) -> str | None:
        """Pin a new channel to the first source with room, reclaiming one idle
        channel anywhere to free its source's slot; the capacity or unavailable
        error fires only when nothing can serve a new channel. No configured
        sources returns None (the legacy single-pool/global-cap path)."""
        if not self._ordered_sources():
            return None
        if self.backup is not None and not self._ready_sources(self._source_order):
            return None  # every source in backoff: an unpinned channel the backup can serve
        picked = self._pick_source()
        while picked is None:
            idle = next((item for item in self.markets.values() if not item.refs), None)
            if idle is None:
                if self._ready_sources(self._source_order):
                    raise HTTPException(503, "guest_market_capacity")
                raise HTTPException(503, "guest_market_source_unavailable")
            self.retire_market(idle)
            picked = self._pick_source()
        self._source_state(picked).channels += 1
        self._prewarm_next(picked)
        return picked

    def _release_source_slot(self, channel: MarketChannel) -> None:
        """Drop the channel's pin and keep the accounting true. The full flag
        survives a move — the source reported its own subscription limit, and
        one channel leaving frees no room there; it clears only when one of
        this source's channels retires (see retire_market) or on recheck."""
        if channel.source is None:
            return
        state = self.source_states.get(channel.source)
        if state is not None:
            state.channels = max(0, state.channels - 1)
        channel.source = None

    def _move_channel(self, channel: MarketChannel) -> str | None:
        """Re-pin a channel whose source failed (or was removed from the list):
        the first healthy source with room takes it; a recovered source never
        reclaims it. Returns the new source, or None when nothing can take the
        channel — the pin is then simply dropped and the producer retries over
        the remaining sources."""
        if channel.source is None:
            return None
        previous = channel.source
        self._release_source_slot(channel)
        target = self._pick_source()
        if target is None or target == previous or channel.retired:
            return None
        self._repin(channel, target)
        return target

    def _repin(self, channel: MarketChannel, target: str) -> None:
        """Pin a channel to another source (#893 follow-up). The new source's first read may be a cold
        load that waits seconds before it answers, so make-before-break starts at the switch, not after
        that read fails: the chart's last packet is marked stale now, and the series_wait grace counts
        from now."""
        channel.source = target
        self._source_state(target).channels += 1
        if channel.waiting_since is None:
            channel.waiting_since = asyncio.get_running_loop().time()
        self._mark_stale(channel, "stale")

    def _mark_stale(self, channel: MarketChannel, status: str) -> None:
        """A previously good price immediately loses its normal/fresh status. A backup packet stays
        labelled backup while a TqKq source warms up (`stale`): it is still Sina's."""
        if channel.packet is None:
            return
        previous = channel.packet["value"]
        if status == "stale" and previous["workbench"].get("dataStatus") == BACKUP_STATUS:
            return
        stale_quote = {**previous["quote"], "dataStatus": status}
        self.publish_market(channel, {"quote": stale_quote, "workbench": {
            **previous["workbench"], "quote": stale_quote, "dataStatus": status, "isCached": True,
        }})

    def _source_failure(self, state: SourceState, reason: str) -> HTTPException:
        state.last_error, state.last_error_at = reason, _utc_now()
        if reason == "market_subscription_limit":
            # Room may reappear when one of this source's channels retires; the
            # flag clears early on retire, otherwise recheck after the window.
            state.full_until = self.clock() + self.full_recheck_seconds
            return HTTPException(503, reason)
        if reason == "market_series_not_ready":
            return HTTPException(503, reason)  # a cold series on this source: no state change
        state.failures += 1
        state.retry_at = self.clock() + min(60, .5 * 2 ** min(state.failures - 1, 7))
        self.cache.pop(("source", state.account_id), None)
        return HTTPException(503, "guest_market_source_unavailable")

    async def _read_source(self, account_id: str, path: str):
        """One read from one configured source, classified for the relay. The
        main sidecar account serves the same /v1/public-market family directly
        through its own provider; pool accounts go through the shared pool start."""
        state = self._source_state(account_id)
        kind = self._source_kinds.get(account_id, "tqkq-pool")
        try:
            if kind == "tqkq-sidecar":
                services = self.ctx.services.accounts.get(account_id)
                if services is None:
                    raise PoolError("account_not_assembled")
                result = await services.provider.public_market(path)
            else:
                pool = self.ctx.account_pool
                if pool is None:
                    raise PoolError("market_pool_unavailable")
                # Shared across symbols and visitors; the pool's start is idempotent.
                port = await self.shared(("source", account_id), lambda: pool.market_source(account_id), 2)
                result = await pool.client.market(port, path)
        except HTTPException:
            raise
        except (PoolError, CredentialKeyError, RuntimeError) as error:
            raise self._source_failure(state, str(getattr(error, "reason", "") or error)) from None
        state.failures = state.retry_at = 0
        lost = self._payload_disconnected(path, result)
        if lost is not None:
            state.observed_at, state.connection_lost = self.clock(), lost
        if path.startswith("/v1/public-market/quotes?"):
            state.quote_symbols = len(path.split("symbols=", 1)[1].split(","))
        elif path.startswith("/v1/public-market/board"):
            state.quote_symbols = len(result.get("rows", []) or [])
        return result

    async def _read_pinned(self, channel: MarketChannel, path: str):
        """One pinned read: the channel keeps its source. A source at its
        subscription limit loses the channel to the next one and the read
        retries once there; an unreachable source gives the channel away for
        the producer's next round; a cold series stays put. A source answering
        that its TqKq connection is lost gives the channel to a connected source
        with room, which serves this read; with none, its stale answer stands."""
        source = channel.source
        if source is None:  # 已被并发移动/退役的通道按无源可用拒绝
            raise HTTPException(503, "guest_market_source_unavailable")
        fresh = path.split("&after=", 1)[0]  # another source's revision means nothing there
        try:
            result = await self._read_source(source, path)
        except HTTPException as error:
            if error.detail != "market_subscription_limit":
                if error.detail == "guest_market_source_unavailable":
                    self._move_channel(channel)
                raise
            target = self._move_channel(channel)
            if target is None:
                raise
            return await self._read_source(target, fresh)
        if not self._payload_disconnected(path, result) or channel.source != source:
            return result
        target = self._pick_source(connected_only=True)
        if target is None or channel.retired:
            return result
        self._release_source_slot(channel)  # the slot moves before the read: never a read without one
        self._repin(channel, target)
        return await self._read_source(target, fresh)

    async def _foreign_read(self, path: str) -> dict | None:
        """A 快期外盘主连 chart read MT5 serves (the whole chart from it), or None: not such a read, MT5 behind 快期
        in the source order, an MT5 failure (then 快期 for ``foreign_backoff`` seconds)."""
        if self.foreign_chart is None or "/workbench" not in path or not path.startswith("/v1/public-market/KQD"):
            return None
        head, _, query = path.partition("?")
        symbol = unquote(head.split("/")[-2])
        try:
            duration = int(query.split("duration=", 1)[1].split("&", 1)[0])
        except (IndexError, ValueError):
            return None
        if self.clock() < self._foreign_down.get(symbol, 0.0):
            return None
        catalog = self._last_catalog or {}
        item = next((i for i in catalog.get("items", []) if str(i.get("symbol", "")).upper() == symbol.upper()), None)
        try:
            return await asyncio.to_thread(self.foreign_chart, symbol, duration, item)
        except Exception as error:  # noqa: BLE001 — any MT5 failure: this chart is 快期's for a while
            self._foreign_down[symbol] = self.clock() + self.foreign_backoff
            log.info("foreign_chart_fallback symbol=%s duration=%s error=%s", symbol, duration, type(error).__name__)
            return None

    async def read_market(self, path, fake_fetch):
        foreign = await self._foreign_read(path)
        if foreign is not None:
            return foreign
        sources = self._ordered_sources()
        if not sources:
            # 无接力源：本部署自己的 provider（fake 数据）或明确拒绝。
            if self.ctx.settings.data_mode == "fake":
                try:
                    return await asyncio.to_thread(fake_fetch)
                except KeyError as error:
                    raise HTTPException(404, "symbol_not_found") from error
                except RuntimeError as error:
                    raise HTTPException(503, str(error)) from error
            raise HTTPException(503, "guest_market_source_unavailable")
        channel = self._producing.get(None)

        async def relay():
            if channel is not None and channel.source is not None:
                return await self._read_pinned(channel, path)
            # 非绑定读取（目录/总表/日历/报价）：第一个真正可服务的源，失败或断流换下一个。
            return (await self._read_serving(sources, path))[1]
        return await self._backed(path, relay)

    # -- 新浪备用 (2026-10-07) --------------------------------------------------

    @staticmethod
    def _payload_disconnected(path: str, result) -> bool | None:
        """Whether a successful source read says its own TqKq connection is lost
        (None: this read carries no such signal). A stale quote on a live
        connection — a quiet contract, a closed market — is not a lost connection."""
        if not isinstance(result, dict):
            return None
        if "/workbench" in path:
            body = result.get("quote")
            codes = {result.get("dataErrorCode"), body.get("dataErrorCode") if isinstance(body, dict) else None}
            return bool(codes & SOURCE_DOWN_CODES)
        if path.startswith(("/v1/public-market/board", "/v1/public-market/quotes", "/v1/public-market/instruments")):
            # These reads go stale only when the source's connection is unhealthy or errored.
            return result.get("dataStatus") == "stale"
        return None

    async def _all_sources_down(self) -> bool:
        """True only when no configured TqKq source can serve: each is in its
        connection backoff or its last read (within SOURCE_SIGNAL_SECONDS) said its
        TqKq connection is lost. A source with no recent observation is asked once
        through the catalog read — the cheapest one that reports connection health."""
        sources = self._ordered_sources()
        if not sources:
            return False
        for account_id in sources:
            state = self._source_state(account_id)
            if self.clock() < state.retry_at:
                continue
            if not (state.observed_at and self.clock() - state.observed_at <= SOURCE_SIGNAL_SECONDS):
                try:
                    await self._read_source(account_id, "/v1/public-market/instruments")
                except HTTPException:
                    if self.clock() < state.retry_at:
                        continue  # the probe itself could not reach it
                    return False  # full or warming up: reachable, so not down
            if not state.connection_lost:
                return False
        return True

    @property
    def backup_active(self) -> bool:
        """The last market read was served by the backup."""
        return self.served_status == BACKUP_STATUS

    def _set_served(self, status: str | None) -> None:
        if status != self.served_status:
            self.served_status = status
            for listener in list(self.backup_listeners):
                try:
                    listener()
                except Exception:  # noqa: BLE001 — a status listener never breaks a market read
                    pass

    async def _backed(self, path: str, relay):
        """The relay's answer, or — only when every source is down — the backup's.
        When the backup cannot answer either, the relay's own answer (or error)
        stands exactly as before. The catalog is not market data: a lost source's
        catalog still names the contracts, and only an unreadable one falls back to
        the last good copy — neither switches the market to backup."""
        catalog = path == "/v1/public-market/instruments"
        try:
            result = await relay()
        except HTTPException as error:
            if (self.backup is None or error.detail not in BACKUP_TRIGGERS
                    or not await self._all_sources_down()):
                raise
            try:
                served = await self._backup_read(path)
            except (SinaUnavailable, HTTPException, KeyError, ValueError, TypeError) as failure:
                if not catalog and str(failure) in BACKUP_DOWN_REASONS:
                    self._set_served(None)
                raise error from None
            if not catalog:
                self._set_served(BACKUP_STATUS)
            return served
        lost = self._payload_disconnected(path, result)
        if self.backup is not None and lost and not catalog:
            # What leaves the backup here is a stale TqKq answer: say `stale`, not `normal`.
            # Outside the backup a mixed market (one chart's source lost) keeps the status;
            # that chart's own packet already says stale.
            left = "stale" if self.served_status is not None else None
            if not await self._all_sources_down():
                # A connected source exists but cannot take this read (full, cold): the
                # stale TqKq answer is what is served, so the status is not `backup`.
                self._set_served(left)
                return result
            try:
                served = await self._backup_read(path)
            except (SinaUnavailable, HTTPException, KeyError, ValueError, TypeError) as failure:
                if str(failure) in BACKUP_DOWN_REASONS:
                    self._set_served(left)
                return result
            self._set_served(BACKUP_STATUS)
            return served
        if lost is False and not catalog:
            self._set_served(None)  # a healthy TqKq read: the status is the provider's own again
        return result

    def _save_catalog(self, catalog: dict) -> None:
        if self._catalog_file is None:
            return
        try:
            write_json_atomic(self._catalog_file, catalog)
        except OSError:
            pass  # only a restart during an outage needs it; the in-memory copy still serves

    async def _backup_catalog(self) -> dict:
        """The contract facts the backup needs (names, ticks, schedules, mains): the
        last catalog a source served (in memory, else the copy saved on disk), else
        the deployment provider's own."""
        if self._last_catalog is None and self._catalog_file is not None:
            saved = await asyncio.to_thread(read_json, self._catalog_file)
            if isinstance(saved, dict) and isinstance(saved.get("items"), list) and saved["items"]:
                self._last_catalog = saved
        if self._last_catalog is not None:
            return self._last_catalog
        provider = self.ctx.services.accounts.default.provider
        raw = await asyncio.to_thread(provider.instrument_catalog)
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            raise SinaUnavailable("backup_catalog_unavailable")
        return {**raw, "items": [{**i, "tradingSchedule": trading_schedule(i.get("tradingSchedule"))}
                                 for i in raw["items"] if isinstance(i, dict)]}

    async def _backup_read(self, path: str) -> dict:
        """One read answered from Sina, in the source's own raw shape."""
        if self.backup is None:
            raise SinaUnavailable("backup_disabled")
        if path == "/v1/public-market/instruments":
            catalog = await self._backup_catalog()
            return {**catalog, "isCached": True}
        if not (path.startswith(("/v1/public-market/board", "/v1/public-market/quotes")) or "/workbench" in path):
            raise SinaUnavailable("backup_unsupported_read")
        catalog = await self._backup_catalog()
        items = {str(i.get("symbol", "")).upper(): i for i in catalog.get("items", [])}
        now = datetime.now(UTC)
        meta = {"schemaVersion": 1, "capturedAt": now.isoformat(), "dataStatus": BACKUP_STATUS,
                "isCached": False, "source": BACKUP_SOURCE, "liveTrading": "disabled"}
        if path.startswith(("/v1/public-market/board", "/v1/public-market/quotes")):
            if path.startswith("/v1/public-market/board"):
                symbols = [str(i["symbol"]) for i in catalog.get("items", []) if i.get("isMain") is True]
                unknown: list[str] = []
            else:
                wanted = [s for s in path.split("symbols=", 1)[1].split(",") if s]
                symbols = [str(items[s.upper()]["symbol"]) for s in wanted if s.upper() in items]
                unknown = [s for s in wanted if s.upper() not in items]
            quotes = await self.backup.quotes(symbols)
            if symbols and not quotes:
                raise SinaUnavailable("backup_quotes_empty")
            rows = [board_row(symbol, quotes.get(symbol), items[symbol.upper()]) for symbol in symbols]
            rows += [{"symbol": s, "errorCode": "symbol_not_found"} for s in unknown]
            return {**meta, "revision": backup_revision(rows), "truncated": False, "rows": rows}
        parts = path.split("?", 1)
        symbol = parts[0].split("/")[-2]
        duration = int(parts[1].split("duration=", 1)[1].split("&", 1)[0])
        item = items.get(symbol.upper())
        if item is None:
            raise SinaUnavailable("backup_unknown_symbol")
        symbol = str(item["symbol"])
        raw_quote = (await self.backup.quotes([symbol])).get(symbol)
        if raw_quote is None:
            raise SinaUnavailable("backup_quote_unavailable")
        bars = await self.backup.bars(symbol, duration)
        quote = quote_payload(symbol, raw_quote, item, now)
        return {**meta, "ageSeconds": quote["ageSeconds"], "durationSeconds": duration,
                "marketSessionStatus": quote["marketSessionStatus"],
                "revision": backup_revision(quote["lastPrice"], quote["quoteDatetime"], quote.get("volume"),
                                            quote["marketSessionStatus"], len(bars), bars[-1:] if bars else None),
                "quote": quote, "bars": bars}

    def present_status(self, status: dict) -> dict:
        """runtime.status as the user should read it: while the shared market serves
        Sina because TqKq is down, `marketDataStatus` says `backup` (and where it
        comes from); right after it, while a connected but full or warming-up source
        serves its stale packet, `stale`. Status a gate reads (trader_tools, strategy
        monitor, orders) comes from the account's own provider and never passes through here."""
        if (self.served_status is None or not isinstance(status, dict)
                or status.get("marketDataStatus") in {"normal", "simulated"}):
            return status
        if self.served_status != BACKUP_STATUS:
            return {**status, "marketDataStatus": self.served_status}
        return {**status, "marketDataStatus": BACKUP_STATUS, "marketDataSource": BACKUP_SOURCE}

    def market_source_status(self) -> list[dict]:
        """The relay's view of every configured source (admin GET): load, health,
        the quote demand it last served, and the last classified error."""
        items = []
        for position, account_id in enumerate(self._ordered_sources()):
            state = self._source_state(account_id)
            now = self.clock()
            health = ("full" if now < state.full_until else
                      "unavailable" if now < state.retry_at else "healthy")
            items.append({
                "accountId": account_id, "position": position,
                "providerKind": self._source_kinds.get(account_id, "tqkq-pool"),
                "health": health,
                "channels": state.channels, "channelLimit": self.market_capacity,
                "quoteSymbols": state.quote_symbols,
                "lastError": state.last_error, "lastErrorAt": state.last_error_at,
            })
        return items

    async def instruments(self):
        async def fetch():
            raw = await self.read_market("/v1/public-market/instruments", self.ctx.services.accounts.default.provider.instrument_catalog)
            days = tuple(await self.trading_days())  # one hashable calendar for every item's cached notice
            today = trading_today(trading_days=days)
            # tradingSchedule 与快照的时段同源同规整（project 只放标量，这里显式带上）：
            # 收盘提醒等顺风车逻辑读目录拿时段，不占 K 线订阅。
            catalog = {**project(raw, META_FIELDS),
                       "items": [{**project(i, INSTRUMENT_FIELDS), "deliveryNotice": delivery_notice(i, today, days),
                                  # 能在美元子账户交易的外盘主连（CME 集团品种）：快期给的是全天 00:00–23:59，改成 CME 的
                                  # 时段与交易日规则，与外盘撮合引擎同一口径（外盘三期）；伦铜、美棉等不归 CME 的照旧。
                                  "tradingSchedule": (cme_schedule(hours=cme_hours_of(i["symbol"])) if cme_listed(i.get("symbol"))
                                                      else trading_schedule(i.get("tradingSchedule"))),
                                  # 这个外盘主连在策略适用范围里的写法（CME.GCE），网页上线框据此核对范围。
                                  **({"strategyScope": strategy_scope_symbol(i["symbol"])} if cme_listed(i.get("symbol")) else {})}
                                 for i in raw.get("items", []) if foreign_listed(i)]}
            if (self.backup is not None and self._ordered_sources() and catalog["items"]
                    and not catalog.get("isCached") and catalog.get("catalogStatus") not in {"loading", "partial"}):
                self._last_catalog = catalog  # what the backup names contracts by while TqKq is down
                digest = hash(tuple((i.get("symbol"), i.get("isMain")) for i in catalog["items"]))
                if digest != self._catalog_saved and self._catalog_file is not None:
                    self._catalog_saved = digest
                    await asyncio.to_thread(self._save_catalog, catalog)
            return catalog
        return await self.shared(("catalog",), fetch, 2)

    async def board(self):
        """Every product's main contract in one read; one second of sharing keeps
        concurrent board polls to a single upstream hit."""
        async def fetch():
            raw = await self.read_market("/v1/public-market/board", self.ctx.services.accounts.default.provider.market_board)
            return {**project(raw, META_FIELDS | {"revision", "truncated"}),
                    "rows": [project(i, BOARD_ROW_FIELDS) for i in raw.get("rows", [])]}
        return await self.shared(("board",), fetch, 1)

    async def quotes(self, symbols):
        """只订报价的 extras 通道 (2026-10-03): quote-only rows for any catalog
        contract (a non-main included) — never a chart serial, so a watched
        non-main no longer takes one of the 48 public kline slots. An errorCode
        row (unknown symbol, cap) passes through untouched.
        多账号接力 (2026-10-04)：行带 market_subscription_limit 时向下一个健康
        源补读并原位替换，其余行为不变。
        按合约缓存 (2026-10-12): each row is cached one second per contract, and a
        contract another request is already reading joins that read, so readers
        holding different sets share their common contracts instead of each set
        being its own upstream read. Only the contracts nobody holds go up, in one
        read. The answer's capturedAt / dataStatus come from the least fresh read
        it is made of (a non-normal status first, then the oldest capture)."""
        wanted = tuple(sorted({str(s).strip().upper() for s in symbols if str(s).strip()}))
        if not wanted:
            return {"schemaVersion": 1, "capturedAt": _utc_now(), "dataStatus": "simulated", "rows": []}
        if self.closed:
            raise HTTPException(503, "guest_market_closed")
        self._purge_cache()
        parts: dict[str, tuple[dict, dict]] = {}
        joins: dict[tuple, tuple[asyncio.Task, list[str]]] = {}
        missing: list[str] = []
        for symbol in wanted:
            hit = self.cache.get(("quote", symbol))
            pending = self.quote_pending.get(symbol)
            if hit is not None:
                parts[symbol] = hit[1]
            elif pending is not None and not pending[1].done():
                joins.setdefault(pending[0], (pending[1], []))[1].append(symbol)
            else:
                missing.append(symbol)
        if missing:
            key = ("quotes", *missing)

            async def fetch():
                return await self._read_quotes(missing)
            task = self._start_shared(key, fetch, 0)
            for symbol in missing:
                self.quote_pending[symbol] = (key, task)
            task.add_done_callback(self._quote_read_done)
            joins[key] = (task, missing)
        reads = list(joins.items())
        answers = await asyncio.gather(*(self._join_shared(key, task) for key, (task, _) in reads))
        metas = [meta for meta, _ in parts.values()]
        extras: dict[str, dict] = {}
        for (_, (_, asked)), (meta, by_symbol, unmatched) in zip(reads, answers):
            metas.append(meta)
            for symbol in asked:
                if symbol in by_symbol:
                    parts[symbol] = by_symbol[symbol]
            if any(symbol not in by_symbol for symbol in asked):
                # Asked by another name (the sidecar answers with the catalog symbol): pass it through.
                extras.update({str(row.get("symbol")): row for row in unmatched})
        meta = min(metas, key=lambda m: (m.get("dataStatus") == "normal", str(m.get("capturedAt") or "")))
        rows = [parts[symbol][1] for symbol in wanted if symbol in parts]
        return {**meta, "rows": rows + [row for name, row in extras.items() if name.upper() not in parts]}

    def _quote_read_done(self, task: asyncio.Task) -> None:
        for symbol, (_, pending) in list(self.quote_pending.items()):
            if pending is task:
                self.quote_pending.pop(symbol)

    async def _read_quotes(self, wanted: list[str]) -> tuple[dict, dict[str, tuple[dict, dict]], list[dict]]:
        """One upstream quotes read for the contracts nobody holds; every row is cached under its own
        contract. Returns the read's meta, its rows by symbol and the rows naming no asked contract."""
        def quote_path(symbols) -> str:
            return "/v1/public-market/quotes?symbols=" + ",".join(symbols)

        provider = self.ctx.services.accounts.default.provider
        sources = self._ordered_sources()
        if not sources:
            raw = await self.read_market(quote_path(wanted), lambda: provider.market_quotes(list(wanted)))
        else:
            # 接力 (2026-10-04)：第一个健康源整批读；满额（订阅上限）的行按序向
            # 后续源补读并原位替换。
            async def relay():
                _, raw, later = await self._read_serving(sources, quote_path(wanted))
                rows = [i for i in raw.get("rows", []) if isinstance(i, dict)]
                capped = [str(i.get("symbol")) for i in rows if i.get("errorCode") == "market_subscription_limit"]
                for fallback in later:
                    if not capped:
                        break
                    try:
                        topup = await self._read_source(fallback, quote_path(capped))
                    except HTTPException:
                        continue
                    served = {i.get("symbol"): i for i in topup.get("rows", []) if isinstance(i, dict)}
                    rows = [served.get(i.get("symbol"), i) if i.get("errorCode") == "market_subscription_limit" else i
                            for i in rows]
                    still = {i.get("symbol") for i in topup.get("rows", [])
                             if isinstance(i, dict) and i.get("errorCode") == "market_subscription_limit"}
                    capped = [s for s in capped if s in still]
                return {**raw, "rows": rows}
            raw = await self._backed(quote_path(wanted), relay)
        meta = project(raw, META_FIELDS)
        asked = set(wanted)
        by_symbol: dict[str, tuple[dict, dict]] = {}
        unmatched: list[dict] = []
        for row in raw.get("rows", []):
            if not isinstance(row, dict):
                continue
            item = row if "errorCode" in row else project(row, BOARD_ROW_FIELDS)
            name = str(row.get("symbol", "")).upper()
            if name in asked:
                by_symbol[name] = (meta, item)
                self._cache_put(("quote", name), (meta, item), 1)
            else:
                unmatched.append(item)
        return meta, by_symbol, unmatched

    async def calendar(self):
        """交易日历摘要 (2026-10-03): the current trading day, the latest ended one
        and the recent ended ones — the replay dialog's default and the 休市
        refusal — and `latestClosedTradingDay`, the latest day that may be replayed
        (the current one after its 15:30, the sidecar's rule; #754). Ten seconds of sharing. The last good calendar is kept in
        state_root: a failed or empty read (sidecar down) serves it with
        `stale: true` and its `savedAt`; only with no saved copy does the failure
        surface (503) and the callers fall back (the client to its weekend rule,
        the route to its own detail)."""
        async def fetch():
            try:
                raw = await self.read_market("/v1/public-market/calendar",
                                             self.ctx.services.accounts.default.provider.market_calendar)
                calendar = {"tradingDay": raw.get("tradingDay"), "previousTradingDay": raw.get("previousTradingDay"),
                            "recentTradingDays": [str(day) for day in raw.get("recentTradingDays", [])],
                            **({"latestClosedTradingDay": raw["latestClosedTradingDay"]}
                               if raw.get("latestClosedTradingDay") else {}),
                            **({"tradingDays": [str(day) for day in raw["tradingDays"]]}
                               if isinstance(raw.get("tradingDays"), list) else {})}
            except Exception:
                saved = await self._saved_calendar()
                if saved is None:
                    raise
                return saved
            if not calendar["tradingDay"] and not calendar["recentTradingDays"]:
                # An empty answer is no calendar either: the saved one, else unavailable (503) — never an
                # empty calendar served (and shared for ten seconds) as a good one (#788).
                saved = await self._saved_calendar()
                if saved is None:
                    raise RuntimeError("market_calendar_unavailable")
                return saved
            if calendar != self._calendar_saved and self._calendar_file is not None:
                self._calendar_saved = calendar
                try:
                    await asyncio.to_thread(write_json_atomic, self._calendar_file,
                                            {**calendar, "savedAt": _utc_now()})
                except OSError:
                    pass  # only a read during an outage needs it
            return calendar
        return await self.shared(("calendar",), fetch, 10)

    async def _saved_calendar(self) -> dict | None:
        """The last good calendar, only while it still names the current trading day (or a later one, a
        holiday): an older copy would hand the replay dialog a default day sessions behind, so it is no
        calendar and the callers fall back as without one."""
        saved = await asyncio.to_thread(read_json, self._calendar_file)
        if not isinstance(saved, dict) or not saved.get("tradingDay"):
            return None
        if str(saved["tradingDay"]) < trading_today().isoformat():
            return None
        return {"tradingDay": saved["tradingDay"], "previousTradingDay": saved.get("previousTradingDay"),
                "recentTradingDays": [str(day) for day in saved.get("recentTradingDays") or []],
                # A saved copy cannot vouch that a day closed since: the previous day is the latest closed one.
                **({"latestClosedTradingDay": saved["previousTradingDay"]} if saved.get("previousTradingDay") else {}),
                **({"tradingDays": [str(day) for day in saved["tradingDays"]]}
                   if isinstance(saved.get("tradingDays"), list) else {}),
                "stale": True, "savedAt": saved.get("savedAt")}

    async def trading_days(self) -> list[str]:
        """The exchange calendar's known trading days (sorted ISO) for the shared surfaces (交割提醒),
        the same summary `exchange_calendar` reads for the accounts; [] when unreadable — the
        callers then estimate by weekdays and say so."""
        try:
            return summary_trading_days(await self.calendar())
        except Exception:  # noqa: BLE001 - no calendar: the weekday estimate stands in
            return []

    async def canonical_symbol(self, symbol):
        catalog = await self.instruments()
        items = catalog["items"]
        canonical = next((i["symbol"] for i in items if (i.get("symbol", "").lower() == symbol.lower() or i.get("instrumentId", "").lower() == symbol.lower())), None)
        if canonical is None:
            if catalog.get("catalogStatus") in {"loading", "partial"}:
                raise HTTPException(503, "market_series_not_ready")
            raise HTTPException(422, "guest_unknown_symbol")
        return canonical

    async def acquire_market(self, symbol, duration):
        if duration not in DURATIONS:
            raise HTTPException(422, "guest_invalid_duration")
        canonical = await self.canonical_symbol(symbol)
        if self.closed:
            raise HTTPException(503, "guest_market_closed")
        key = (canonical, duration)
        channel = self.markets.get(key)
        if channel is None:
            source = self._claim_source_slot()
            if source is None and len(self.markets) >= self.market_capacity:
                # 无接力源时的全局上限（fake/单池路径）：先回收一个闲置通道。
                idle = next((item for item in self.markets.values() if not item.refs), None)
                if idle is None:
                    raise HTTPException(503, "guest_market_capacity")
                self.retire_market(idle)
            channel = MarketChannel(canonical, duration, source=source)
            self.markets[key] = channel
            channel.task = asyncio.create_task(self.produce_market(channel))
            self.market_tasks.add(channel.task)
            channel.task.add_done_callback(self.market_tasks.discard)
        if channel.retirement is not None:
            channel.retirement.cancel()
            channel.retirement = None
        channel.refs += 1
        try:
            async with asyncio.timeout(self.series_wait_seconds):
                while channel.packet is None and channel.error is None:
                    await channel.changed.wait()
            if channel.error is not None:
                raise channel.error
            return channel
        except TimeoutError:
            self.release_market(channel)
            raise HTTPException(503, "market_series_not_ready") from None
        except BaseException:
            self.release_market(channel)
            raise

    def release_market(self, channel):
        channel.refs -= 1
        if not channel.refs:
            if channel.packet is None or self.closed:
                self.retire_market(channel)
            else:
                channel.retirement = asyncio.get_running_loop().call_later(
                    self.market_idle_seconds, self.retire_market, channel,
                )

    def retire_market(self, channel):
        channel.retired = True
        key = (channel.symbol, channel.duration)
        if self.markets.get(key) is channel:
            self.markets.pop(key)
        source = channel.source
        self._release_source_slot(channel)
        if source is not None:
            state = self.source_states.get(source)
            if state is not None and state.channels < self.market_capacity:
                state.full_until = 0  # 通道退役释放了订阅名额：满额标记随之解除
        if channel.retirement is not None:
            channel.retirement.cancel()
            channel.retirement = None
        if channel.task is not None:
            channel.task.cancel()
        channel.error = HTTPException(503, "guest_market_closed")
        channel.notify()

    def publish_market(self, channel, current, bars_change=None):
        previous = channel.packet
        patch = market_patch(previous["value"], current, bars_change) if previous else None
        if previous and patch == {}:
            return
        channel.packet = MarketPacket(value=current, patch=patch, previous=previous["id"] if previous else None,
                                      id=secrets.token_hex(8), receivedAt=self.clock())
        channel.notify()

    def project_packet(self, channel, trading_days):
        """Cache one history-limited packet per source version and tier limit.

        The original producer remains untouched for unrestricted members. A
        slow reader still gets a complete limited snapshot, never raw history.
        """
        packet = channel.packet
        if packet is None or trading_days is None:
            return packet
        previous = channel.projections.get(trading_days)
        if previous and previous[0] == packet["id"]:
            return previous[1]
        value = packet["value"]
        bars = value["workbench"].get("bars", [])
        dated = []
        day_of = daily_bar_day if channel.duration == 86400 else bar_trading_day
        for bar in bars[-MAX_BARS:]:
            try:
                day = date.fromisoformat(day_of(bar)).isoformat()
            except (ValueError, TypeError, KeyError, OverflowError, OSError):
                continue  # An unclassifiable bar cannot cross the limited boundary.
            dated.append((bar, day))
        days = set(sorted({day for _, day in dated})[-trading_days:])
        limited_bars = [bar for bar, day in dated if day in days]
        # The limit travels with the bars (#734): the chart's "最近 N 个交易日" reads it from the same
        # frame, so an administrator's change reaches an open stream's label with the re-trimmed bars.
        # Every limited packet therefore gets its own encoding, even when no bar was cut.
        limited = {**value, "workbench": {**value["workbench"], "bars": limited_bars, "historyTradingDays": trading_days}}
        prior = previous[1] if previous and previous[0] == packet["previous"] else None
        patch = market_patch(prior["value"], limited) if prior else None
        snapshot = event("snapshot", limited)
        result = {**packet, "value": limited, "snapshot": snapshot,
                  "update": snapshot if patch is None else event("update", patch),
                  "id": packet["id"] + ":" + str(trading_days),
                  "previous": prior["id"] if prior else None}
        channel.projections[trading_days] = (packet["id"], result)
        return result

    async def produce_market(self, channel):
        revision = None
        failures = 0
        from_source = None
        # The pinned source's bars and their version (扩容 ②): sent back as `barsVersion`, a versioning
        # source answers with only the bars changed since; anything that does not line up reads whole.
        held: SourceBars | None = None
        # Its first frame is pending from the start: the series_wait grace counts from here (channel.waiting_since).
        loop = asyncio.get_running_loop()
        channel.waiting_since = loop.time()
        pinned = channel.source  # the source the current read went to
        cme = cme_listed(channel.symbol)  # 外盘可下单主连：快照按 CME 口径（foreign_market.cme_chart）
        cme_base = None  # its last view before that, re-applied on a round the source did not change
        # A retired channel's producer stops here even when its cancellation got lost in a read (an
        # HTTP timeout firing in the same instant); it must never pin itself to a source again.
        while not channel.retired:
            try:
                # 管理员把本源移出清单：通道转去现清单，转不动就退回无绑定读取。
                if channel.source is not None and channel.source not in self._ordered_sources():
                    self._move_channel(channel)
                elif channel.source is None and not channel.retired and self._ordered_sources():
                    # Lost its source with nowhere to go earlier: pin again once a source has room,
                    # so the channel is counted against a source's cap instead of floating.
                    target = self._pick_source()
                    if target is not None:
                        self._repin(channel, target)
                if channel.source != from_source:
                    revision = held = None  # 接力换源后旧源的 revision / 版本不可比，先取全量
                path = f"/v1/public-market/{quote(channel.symbol, safe='.')}/workbench?duration={channel.duration}"
                if revision is not None:
                    # Everything after `&after=` is dropped when a read moves to another source (_read_pinned).
                    path += f"&after={quote(revision, safe='')}&waitMs={self.foreign_wait_ms if cme else 10000}"
                    if held is not None:
                        path += f"&barsVersion={quote(held.token, safe='')}"
                real_source = channel.source or self.market_account_id or bool(self._source_order)
                pinned = channel.source
                self._producing.set(channel)
                raw = await self.read_market(path, lambda: self.ctx.services.accounts.default.provider.market_workbench(channel.symbol, channel.duration))
                current_revision = raw.get("revision")
                if real_source and (not isinstance(current_revision, str) or not current_revision):
                    raise HTTPException(503, "guest_market_source_unavailable")
                if (revision is None or current_revision != revision or channel.error is not None
                        or channel.source != pinned):  # moved during the read: its stale mark must go
                    delta, bars_change = raw.get("barsDelta"), None
                    if delta is not None:
                        applied = (held.apply(raw.get("barsVersion"), delta)
                                   if held is not None and isinstance(delta, dict) and delta.get("base") == held.token
                                   else None)
                        if applied is None:
                            revision = held = None  # the delta does not rebuild the window: read it whole now
                            continue
                        held, bars_change = applied
                        bars = held.bars
                    else:
                        bars = [project(i, BAR_FIELDS) for i in raw.get("bars", [])[-MAX_BARS:]]
                        held = SourceBars.whole(raw.get("barsVersion"), bars)
                    workbench = {**project(raw, META_FIELDS), "liveTrading": "disabled", "isCached": bool(raw.get("isCached", False)),
                                 "quote": {**project(raw.get("quote", {}), QUOTE_FIELDS),
                                           # The schedule is the quote's one nested field (合约信息卡的时间).
                                           "tradingSchedule": trading_schedule(raw.get("quote", {}).get("tradingSchedule"))},
                                 "bars": bars}
                    if cme:
                        cme_base = workbench
                        workbench = await self._cme_workbench(channel, workbench)
                    channel.error = None
                    self.publish_market(channel, {"quote": workbench["quote"], "workbench": workbench},
                                        bars_change=None if cme else bars_change)  # CME: cme_chart may re-date bars
                else:
                    if held is not None and raw.get("barsVersion") != held.token:
                        held = None  # defensive: the same view must carry the same bars version
                    if cme_base is not None:
                        # 外盘 (CME): the session status follows CME's clock and a daily/weekly chart's quote is
                        # MT5's — neither moves the 快期 revision, so every round re-applies them to the same view.
                        workbench = await self._cme_workbench(channel, cme_base)
                        self.publish_market(channel, {"quote": workbench["quote"], "workbench": workbench})
                revision = current_revision
                from_source = channel.source
                failures = 0
                channel.waiting_since = None
                # A backup round has no long poll to wait on: poll Sina on an interval.
                await asyncio.sleep(self.backup_interval if raw.get("dataStatus") == BACKUP_STATUS
                                    else self.foreign_interval if str(raw.get("source") or "").startswith(FOREIGN_CHART_SOURCE)
                                    else self.fake_interval if not real_source else 0)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failure = error if isinstance(error, HTTPException) else HTTPException(503, "guest_market_source_unavailable")
                if channel.waiting_since is None:
                    channel.waiting_since = loop.time()
                # 冷序列 / 换源 (2026-10-14): one read of a series its source is still loading, or of a source
                # the channel just left, is this producer's to retry — the coalesced readers keep waiting for
                # the first frame, and a chart already serving keeps its last packet, marked stale, until the
                # new source answers (make-before-break). Past series_wait_seconds it is a failure as any other.
                loading = ((failure.detail == "market_series_not_ready" or channel.source not in (None, pinned))
                           and loop.time() - channel.waiting_since < self.series_wait_seconds)
                channel.error = None if loading else failure
                self._mark_stale(channel, "stale" if loading else "unavailable")
                if not loading:
                    channel.notify()
                revision = held = cme_base = None
                failures += 1
                await asyncio.sleep(min(2, .1 * 2 ** min(failures - 1, 5)))

    async def _cme_workbench(self, channel, workbench):
        """A tradable 外盘主连 chart's workbench in CME's terms (foreign_market.cme_chart, the one entry): the MT5
        main-month quote for a 快期-drawn (daily/weekly, or MT5 down) chart; an MT5 chart already carries its own."""
        row = None
        if self.foreign_quote is not None and not str(workbench.get("source") or "").startswith(FOREIGN_CHART_SOURCE):
            try:
                row = await asyncio.to_thread(self.foreign_quote, channel.symbol)
            except Exception as error:  # noqa: BLE001 — no MT5 quote: the 快期 quote stays, its age says so
                log.info("foreign_chart_quote_unavailable symbol=%s error=%s", channel.symbol, type(error).__name__)
        return cme_chart(workbench, row, datetime.fromtimestamp(self.clock(), UTC), symbol=channel.symbol,
                         duration=channel.duration)

    def free_market_slots(self) -> int:
        """Kline channels the public market can still open (Σ each source's cap − its channels;
        without sources, the global cap − the open channels). 盘中异动 opens one only above a reserve."""
        sources = self._ordered_sources()
        if not sources:
            return max(0, self.market_capacity - len(self.markets))
        return sum(max(0, self.market_capacity - self._source_state(source).channels) for source in sources)

    def served_workbench(self, symbol, duration):
        """The workbench a live channel is serving right now, or None — never acquires one (盘中异动,
        2026-10-06): a background check may read only what readers already hold open, because the
        public sources cap kline series (48 each) and TqSdk never unsubscribes one."""
        wanted = str(symbol or "").upper()
        for (served, period), channel in list(self.markets.items()):
            if period == duration and served.upper() == wanted and channel.packet is not None and channel.error is None:
                return channel.packet["value"]["workbench"]
        return None

    async def market_packet(self, symbol, duration):
        channel = await self.acquire_market(symbol, duration)
        try:
            return channel.packet
        finally:
            self.release_market(channel)

    async def market(self, symbol, duration):
        return (await self.market_packet(symbol, duration))["value"]

    async def workbench_since(self, symbol, duration, since):
        """The member workbench after `since` (see `workbench_since`); one answer per packet and `since`
        is kept, so the readers polling one chart share the digest."""
        packet = await self.market_packet(symbol, duration)
        key = (packet["id"], since)
        cached = self._since_answers.get(key)
        if cached is None:
            cached = workbench_since(packet["value"]["workbench"], since)
            self._since_answers[key] = cached
            while len(self._since_answers) > 256:
                self._since_answers.popitem(last=False)
        return cached

    async def snapshot(self, symbol, turn_id=""):
        packet = await self.market_packet(symbol, 300)
        value = packet["value"]
        workbench, quote_value = value["workbench"], value["quote"]
        price = quote_value.get("lastPrice", quote_value.get("price"))
        if price is None:
            raise HTTPException(503, "market_quote_unavailable")
        change = quote_value.get("changePercent") or 0
        now = self.clock()
        ages = []
        captured_age = None
        for timestamp in (workbench.get("capturedAt"), quote_value.get("quoteDatetime")):
            try:
                parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                elapsed = max(0, now - parsed.timestamp())
                ages.append(elapsed)
                if timestamp == workbench.get("capturedAt"):
                    captured_age = elapsed
            except (ValueError, TypeError, OverflowError):
                pass
        source_age = workbench.get("ageSeconds")
        if isinstance(source_age, (int, float)) and math.isfinite(source_age) and source_age >= 0:
            ages.append(source_age + (captured_age if captured_age is not None else max(0, now - packet["receivedAt"])))
        if not ages:
            raise HTTPException(503, "market_quote_time_unavailable")
        age = max(ages)
        # 合约信息卡与交割提醒 (2026-10-02): the one contract's per-lot money / trading hours,
        # and the delivery notice off the catalog (both None-safe: missing data shows “—”).
        contract = contract_payload(quote_value)
        notice = None
        try:
            catalog = await self.instruments()
            item = next((i for i in catalog.get("items", [])
                         if str(i.get("symbol", "")).upper() == str(quote_value.get("symbol", "")).upper()), None)
            notice = (item or {}).get("deliveryNotice")
        except (HTTPException, KeyError, RuntimeError, ValueError):
            notice = None
        return {**quote_value, "turnId": turn_id, "price": price,
                "name": quote_value.get("name") or symbol,
                "changePercent": change, "updatedAt": _age_label(age),
                "capturedAt": workbench.get("capturedAt"), "ageSeconds": round(age, 3),
                "dataStatus": quote_value.get("dataStatus", workbench.get("dataStatus")),
                "trend": "up" if change > 0 else "down" if change < 0 else "flat",
                "contract": contract, "deliveryNotice": notice,
                "summary": (BACKUP_NOTICE if quote_value.get("dataStatus") == BACKUP_STATUS
                            else "来自共享行情源的只读行情快照。"), "priceCurve": [price]}

    async def close(self):
        self.closed = True
        for channel in list(self.markets.values()):
            self.retire_market(channel)
        tasks = list(self.market_tasks) + list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.backup is not None:
            await self.backup.close()
        self.market_tasks.clear()
        self.inflight.clear()
        self.cache.clear()

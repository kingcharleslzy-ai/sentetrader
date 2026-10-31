"""AI screening of news for one futures product: is it about this product, and what does it say?

The readers (快讯 feed, 相关报道, the research desk's 快讯 facts) pass recalled candidates
through `screen_view`: a candidate the keyword rules already rule out (苹果公司 for 苹果) is
dropped with its group name; one with a stored verdict is kept with its one-line reading
(event · direction · strength · reason) or dropped with the model's group name; one without a
verdict yet is queued. The background loop judges queued (item, product) pairs in batches with
one cheap structured model call each, through the Runtime's own adapter and the platform usage
marker — never a new provider. Each pair is judged once and stored (`news_screen`).

A kept verdict must say how the news reaches the product (`transmission`); one that cannot is
dropped. It also names the event (`eventLabel`, reused across sources: the prompt lists the
product's labels of the research day), so `build_events` merges the 6–8 sources that ran the same
story into one event and sorts events into 重要 / 其他 / 次要 by strength and direction.

A round's batch takes turns across products (`_take`): the products a reader is looking at (read in
the last LOOKED_SECONDS, in any process: `looked`) fill up to half of it first, then every queued product
takes one pair at a time, the one served longest ago first — a product with hundreds of queued
pairs (隆众's quotes tagged 原油, 10-14) cannot keep another waiting an hour. Over MAX_PENDING the
product with the most queued pairs gives up its oldest.

The screener alone owns each pair's state — queued, in flight (taken by a round, model not yet
answered), judged (a stored verdict) or failed (MAX_ATTEMPTS model failures) — and queueing,
taking a batch, storing, failing and the readers' pending count all read that one state: a pair
re-read while its call is in flight is not queued again, a round re-checks stored verdicts before
calling the model, and a failed pair stays on its keywords, marked not AI-screened, instead of
being reported as still being screened.

Only the instance-lease holder runs the background loop (wiring/producers.py); every process
queues and reads verdicts from the shared table. The day's call count and the products readers
looked at (`touch`, the poller's active list) live in the shared database too, so a handoff or a
read served by the other slot is seen by the holder.

Model calls are capped per Beijing day. Over the cap, without an authenticated adapter, or while
the model fails, readers fall back to the keyword result: items recalled for the product stay,
nothing is dropped beyond the keyword rules, and the view says it is not AI-screened. A reader
never waits on the model except the desk's bounded `settle`. The model reads only the news
text: it is a reading of the news, not trading advice.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from datetime import time as clock_time
from typing import Any

from pydantic import BaseModel, ConfigDict

from .daily_budget import DailyCallBudget

log = logging.getLogger("uvicorn.error.research")

CHINA = timezone(timedelta(hours=8))
EVENTS = ("库存", "政策", "天气·供给", "供给", "需求", "宏观", "价格", "持仓资金", "其他")
DIRECTIONS = ("偏多", "偏空", "中性")
STRENGTHS = ("弱", "中", "强")
REASON_CHARS = 30
TRANSMISSION_CHARS = 30
LABEL_CHARS = 16
GROUP_CHARS = 14
DEFAULT_GROUP = "其他无关内容"
# A verdict that cannot say how the news reaches the product (no transmission) is not kept.
NO_PATH_GROUP = "说不清怎么影响"
# The fold counts drops by group, so the model picks from fixed names (plus the product's own 排除词
# groups, e.g. 苹果公司新品与财报) instead of coining a new one per item.
DROP_GROUPS = ("无关国际新闻", "无关宏观数据", "其他品种新闻", "股票与公司新闻", "同名但不是该品种", NO_PATH_GROUP,
               DEFAULT_GROUP)
# The judgments' tokens book against this marker, never the quota of whoever opened the page.
USAGE_USER = "platform:news-screen"
BATCH_PAIRS = 24
TEXT_CHARS = 200
CALL_TIMEOUT_SECONDS = 60
FAILURE_BACKOFF_SECONDS = 5 * 60
MAX_ATTEMPTS = 2
MAX_PENDING = 1500
# A reader waiting (a view found unjudged items) starts a round within this; queued background
# work (the poller's new items for recently read products) waits for the longer gap so it batches.
URGENT_GAP_SECONDS = 10.0
BACKGROUND_GAP_SECONDS = 5 * 60.0
ROUNDS_PER_BURST = 4
# A product read by a reader (an urgent want) within this many seconds goes first in each batch, up to
# half of the batch: the rest is shared in turn by every queued product.
LOOKED_SECONDS = 10 * 60.0
ACTIVE_SECONDS = 3 * 60 * 60
RETAIN_DAYS = 4
# The research day turns at 08:30 Beijing (as the desk's 今日判断): events merge within one research day.
RESEARCH_DAY_START = clock_time(8, 30)
KNOWN_LABELS = 60
# 重要 = 中/强; 其他 = 弱 with a direction; 次要 = 弱 and 中性 (folded). An event whose sources disagree is 分歧.
SPLIT = "分歧"
MINOR_GROUPS_NAMED = 3

INSTRUCTIONS = """你是期货资讯筛选员。输入是若干条（新闻，国内期货品种）配对，每对有一个 key。逐对判断这条新闻对该品种的价格有没有实际影响，只根据新闻本身判断，不编造新闻里没有的事实，不给交易建议。
- relevant：新闻讲的是该品种自己的产业链（产区、天气、种植/开工、库存、供需、进出口、政策与交易所规则、现货价格、仓单与持仓资金），或会明显影响它的宏观事件时为 true。名字相同但不是同一个东西（例如苹果公司、iPhone 之于苹果期货；白银市之于白银）、只顺带提到、股票与上市公司新闻、与该品种无关的国际政治或其他行业新闻为 false。
- event：库存 / 政策 / 天气·供给 / 需求 / 宏观 / 价格 / 持仓资金 / 其他，选一个。
- direction：对该品种价格的方向，偏多 / 偏空 / 中性。
- strength：影响强度，弱 / 中 / 强。直接、确定、量大为强；间接、待确认为弱。
- strength：影响强度，弱 / 中 / 强。直接、确定、量大为强；间接、待确认为弱；官员的一般性表态、例行数据的前瞻与点评一律为弱。
- reason：不超过 30 个字的一句话，说清这条新闻为什么影响该品种。
- transmission：不超过 30 个字，说清这条新闻经过什么途径影响到该品种的价格（例如“美元走强，以美元计价的金价承压”“冷库出库慢，现货压力传到期货”）。说不出具体途径的，relevant 一律为 false，dropReason 选“说不清怎么影响”。
- eventLabel：不超过 16 个字的规范事件名，写“发生了什么”，不写来源和观点（例如“现货黄金日内下跌”“美债收益率创新高”）。knownLabels 列出了该品种今天已有的事件名，讲的是同一件事就原样复用那个名字；同一批里讲同一件事的几条也用同一个名字。
- dropReason：relevant 为 false 时从 groups 里选一个分组名。productGroups 是某品种的“同名但不是它”专属分组，只用于名字相同、讲的却是别的东西的新闻（苹果公司之于苹果、黄金周之于黄金）；宏观、国际、其他品种的新闻不要放进去。relevant 为 true 时留空。
每个 key 都要回答一次。"""


class ScreenVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str
    relevant: bool
    event: str | None = None
    direction: str | None = None
    strength: str | None = None
    reason: str | None = None
    transmission: str | None = None
    eventLabel: str | None = None
    dropReason: str | None = None


class ScreenBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    verdicts: list[ScreenVerdict]


def _event(value: str | None) -> str:
    text = (value or "").strip()
    if text in {"天气", "供给", "天气·供给", "天气/供给", "天气供给"}:
        return "天气·供给"
    return text if text in EVENTS else "其他"


def normalise_verdict(verdict: ScreenVerdict, groups: tuple[str, ...] = DROP_GROUPS) -> dict[str, Any]:
    transmission = (verdict.transmission or "").strip()[:TRANSMISSION_CHARS]
    if verdict.relevant and not transmission:
        return {"relevant": False, "dropReason": NO_PATH_GROUP}  # no path to the product: not kept
    if not verdict.relevant:
        group = (verdict.dropReason or "").strip()
        return {"relevant": False, "dropReason": group if group in groups else DEFAULT_GROUP}
    return {"relevant": True, "event": _event(verdict.event),
            "direction": verdict.direction if verdict.direction in DIRECTIONS else "中性",
            "strength": verdict.strength if verdict.strength in STRENGTHS else "弱",
            "reason": (verdict.reason or "").strip()[:REASON_CHARS], "transmission": transmission,
            "eventLabel": (verdict.eventLabel or "").strip()[:LABEL_CHARS] or None}


def research_day(moment: datetime | str | None) -> str:
    """The research day (北京 08:30 起算) of a moment; a date-only or unreadable stamp is its own day."""
    if isinstance(moment, str):
        text = moment.strip()
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text[:10]
        if len(text) <= 10:
            return text[:10]  # a date without a time: that day
    if moment is None:
        return ""
    local = (moment if moment.tzinfo else moment.replace(tzinfo=CHINA)).astimezone(CHINA)
    day = local.date() if local.time() >= RESEARCH_DAY_START else local.date() - timedelta(days=1)
    return day.isoformat()


def day_start(day: str) -> str:
    """The research day's first instant as stored (Beijing ISO), for string comparison in SQLite."""
    return f"{day}T{RESEARCH_DAY_START.isoformat(timespec='seconds')}+08:00"


@dataclass
class NewsScreener:
    connection: sqlite3.Connection
    lock: threading.Lock | threading.RLock
    # The Runtime's shared model adapter (set by the research routes); None: keyword only.
    generator: Callable[[], Any] | None = None
    # Product → its own drop groups the model may also choose (the 排除词 group names).
    product_groups: Callable[[str], tuple[str, ...]] = lambda product: ()
    # Called (on the loop) after a round stored verdicts: the news analyst looks for new 重要 events.
    on_judged: Callable[[set[str]], None] | None = None
    model: str = field(default_factory=lambda: os.getenv(
        "AI_TRADER_NEWS_SCREEN_MODEL", os.getenv("AI_TRADER_RESEARCH_DESK_MODEL", "deepseek-flash")).strip())
    daily_calls: int = field(default_factory=lambda: int(os.getenv("AI_TRADER_NEWS_SCREEN_DAILY_CALLS", "300") or 0))
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    _pending: OrderedDict[tuple[str, str], dict[str, Any]] = field(default_factory=OrderedDict, repr=False)
    _inflight: set[tuple[str, str]] = field(default_factory=set, repr=False)
    _attempts: dict[tuple[str, str], int] = field(default_factory=dict, repr=False)
    # product → monotonic time of its last urgent want (a reader waiting) / of its last pair taken.
    _looked: dict[str, float] = field(default_factory=dict, repr=False)
    _served: dict[str, float] = field(default_factory=dict, repr=False)
    _guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _failure: tuple[float, str] | None = field(default=None, repr=False)
    _urgent: bool = field(default=False, repr=False)
    _last_round: float = field(default=-1e9, repr=False)
    # product → monotonic time this process last wrote its touch (one write a minute per product).
    _touched: dict[str, float] = field(default_factory=dict, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _wake: asyncio.Event | None = field(default=None, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _burst: asyncio.Future[None] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS news_screen (item_id TEXT NOT NULL, product TEXT NOT NULL,"
                " relevant INTEGER NOT NULL, event TEXT, direction TEXT, strength TEXT, reason TEXT,"
                " drop_reason TEXT, model TEXT, judged_at TEXT NOT NULL, PRIMARY KEY (item_id, product))")
            # 第六批 (2026-10-07): how the news reaches the product, the event name and the item's time.
            # Rows judged before carry NULLs: kept as they were, each its own event.
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(news_screen)")}
            for column in ("transmission", "event_label", "published_at"):
                if column not in columns:
                    self.connection.execute(f"ALTER TABLE news_screen ADD COLUMN {column} TEXT")
            # 2026-10-12: the products readers looked at, shared by every process (wall-clock seconds).
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS news_screen_active (product TEXT PRIMARY KEY, name TEXT NOT NULL,"
                " seen_at REAL NOT NULL)")
        self._budget = DailyCallBudget("news_screen", self.connection, self.lock)

    # -- state readers use

    def _adapter(self) -> tuple[Any | None, str | None]:
        generator = self.generator() if self.generator is not None else None
        if generator is None or not hasattr(generator, "complete_structured") or not getattr(generator, "agent_authenticated", False):
            return None, None
        endpoints = getattr(generator, "endpoints", None)
        model = self.model if self.model and (not isinstance(endpoints, dict) or self.model in endpoints) else None
        return generator, model

    def _today(self) -> str:
        return self.now().astimezone(CHINA).date().isoformat()

    def usable(self) -> tuple[bool, str | None]:
        """Whether new items can be judged now; the reason (a stable code) when not."""
        if self._adapter()[0] is None:
            return False, "news_screen_model_unavailable"
        if self._budget.used(self._today()) >= self.daily_calls:
            return False, "news_screen_budget_exhausted"
        if self._failure is not None and self._failure[0] > self.clock():
            return False, "news_screen_model_failing"
        return True, None

    def usage(self) -> dict[str, Any]:
        return {"day": self._today(), "calls": self._budget.used(self._today()), "dailyCalls": self.daily_calls, "pending": len(self._pending) + len(self._inflight)}

    def verdicts(self, product: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        found: dict[str, dict[str, Any]] = {}
        with self.lock:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                rows = self.connection.execute(
                    "SELECT item_id, relevant, event, direction, strength, reason, drop_reason, transmission, event_label"
                    f" FROM news_screen WHERE product = ? AND item_id IN ({','.join('?' * len(chunk))})",
                    (product, *chunk)).fetchall()
                for item_id, relevant, event, direction, strength, reason, drop_reason, transmission, label in rows:
                    found[item_id] = ({"relevant": True, "event": event, "direction": direction, "strength": strength,
                                       "reason": reason or "", "transmission": transmission or "", "eventLabel": label or None}
                                      if relevant else {"relevant": False, "dropReason": drop_reason})
        return found

    def event_labels(self, product: str, day: str) -> list[str]:
        """The product's event names of this research day (newest first): the prompt asks the model to reuse them."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT event_label, MAX(judged_at) AS last FROM news_screen WHERE product = ? AND relevant = 1"
                " AND event_label IS NOT NULL AND published_at >= ? GROUP BY event_label ORDER BY last DESC LIMIT ?",
                (product, day_start(day), KNOWN_LABELS)).fetchall()
        return [row[0] for row in rows]

    def event_products(self, event_id: str, since: str) -> list[str]:
        """The products whose judgments could make this event id (judged items published at or after `since`):
        a named event's id hashes (product, research day, name) — the same key `build_events` groups on — and
        an unnamed one is its item's own id. Candidates only; the caller confirms against the product's events."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT DISTINCT item_id, product, event_label, published_at FROM news_screen"
                " WHERE relevant = 1 AND published_at >= ?", (since,)).fetchall()
        found: list[str] = []
        for item_id, product, label, published in rows:
            made = _event_id(product, research_day(published), label) if label else item_id
            if made == event_id and product not in found:
                found.append(product)
        return found

    def want(self, pairs: list[tuple[dict[str, Any], str, str]], *, urgent: bool) -> int:
        """Queue (item, product, product name) pairs for judging; the number newly queued."""
        added = 0
        with self._guard:
            for item, product, name in pairs:
                key = (item["id"], product)
                if self._state(key) is not None:
                    continue
                self._pending[key] = {"title": item.get("title") or "", "text": item.get("text") or "",
                                      "source": item.get("source") or "", "publishedAt": item.get("publishedAt") or "",
                                      "name": name}
                added += 1
            if urgent:
                for product in {product for _, product, _ in pairs}:
                    self._looked[product] = self.clock()
            self._trim()
        if added:
            self._signal(urgent)
        return added

    def _trim(self) -> None:
        """Over MAX_PENDING the product with the most queued pairs gives up its oldest (they wait for the
        next read): one busy product cannot push out another's few. Called under `_guard`."""
        over = len(self._pending) - MAX_PENDING
        if over <= 0:
            return
        queued: dict[str, list[tuple[str, str]]] = {}
        for key in self._pending:
            queued.setdefault(key[1], []).append(key)
        heads = {product: 0 for product in queued}
        for _ in range(over):
            product = max(queued, key=lambda name: len(queued[name]) - heads[name])
            self._pending.pop(queued[product][heads[product]], None)
            heads[product] += 1

    def looked(self) -> set[str]:
        """Products a reader looked at in the last LOOKED_SECONDS: this process's reads, and the shared
        table's (a read the other slot served reaches the lease holder that runs the loop)."""
        now = self.clock()
        with self._guard:
            found = {product for product, at in self._looked.items() if now - at <= LOOKED_SECONDS}
        cutoff = self.now().timestamp() - LOOKED_SECONDS
        with self.lock:
            rows = self.connection.execute("SELECT product FROM news_screen_active WHERE seen_at >= ?", (cutoff,)).fetchall()
        return found | {str(row[0]) for row in rows}

    def _take(self, looked: set[str]) -> list[tuple[tuple[str, str], dict[str, Any]]]:
        """The next batch, taken out of the queue (see the module note): looked-at products first (up to
        half), then one pair per product in turn, least recently served first; each product's oldest
        first. Called under `_guard`."""
        queued: dict[str, list[tuple[str, str]]] = {}
        for key in self._pending:
            queued.setdefault(key[1], []).append(key)
        now = self.clock()
        first = sorted((product for product in queued if product in looked),
                       key=lambda product: -self._looked.get(product, -1e9))
        order = sorted(queued, key=lambda product: self._served.get(product, -1e9))  # stable: FIFO on ties
        picked: list[tuple[str, str]] = []

        def take_turns(products: list[str], room: int) -> None:
            while len(picked) < room and any(queued[product] for product in products):
                for product in products:
                    if queued[product] and len(picked) < room:
                        picked.append(queued[product].pop(0))

        take_turns(first, BATCH_PAIRS // 2)
        take_turns(order, BATCH_PAIRS)
        batch = [(key, self._pending.pop(key)) for key in picked]
        for key in picked:
            self._inflight.add(key)
            self._served[key[1]] = now
        return batch

    def _state(self, key: tuple[str, str]) -> str | None:
        """queued / inflight / failed, or None (judged — the store says which — or not yet seen)."""
        if key in self._inflight:
            return "inflight"
        if key in self._pending:
            return "queued"
        return "failed" if self._attempts.get(key, 0) >= MAX_ATTEMPTS else None

    def state(self, item_id: str, product: str) -> str | None:
        with self._guard:
            return self._state((item_id, product))

    def _busy(self, product: str) -> bool:
        with self._guard:
            return any(key[1] == product for key in (*self._pending, *self._inflight))

    def touch(self, product: str, name: str) -> None:
        """A reader looked at this product: the poller keeps screening its new items for a while. Written to
        the shared table (at most once a minute per product), so the lease holder's poller sees reads the
        other slot served. In this process the product also goes first in the next batches (`_take`)."""
        with self._guard:
            self._looked[product] = self.clock()
        if self.clock() - self._touched.get(product, -1e9) < 60:
            return
        self._touched[product] = self.clock()
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO news_screen_active (product, name, seen_at) VALUES (?, ?, ?)"
                " ON CONFLICT(product) DO UPDATE SET name = excluded.name, seen_at = excluded.seen_at",
                (product, name, self.now().timestamp()))

    def active(self) -> list[tuple[str, str]]:
        cutoff = self.now().timestamp() - ACTIVE_SECONDS
        with self.lock:
            rows = self.connection.execute("SELECT product, name FROM news_screen_active WHERE seen_at >= ?"
                                           " ORDER BY seen_at DESC", (cutoff,)).fetchall()
        return [(str(product), str(name)) for product, name in rows]

    def prune(self, keep: Iterable[str] = ()) -> None:
        """Drop judgments older than RETAIN_DAYS, except those of the `keep` items (still in the feed:
        an official notice stays a week, longer than its first judgment's window)."""
        cutoff = (self.now() - timedelta(days=RETAIN_DAYS)).isoformat(timespec="seconds")
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM news_screen WHERE judged_at < ? AND item_id NOT IN (SELECT value FROM json_each(?))",
                (cutoff, json.dumps(list(keep))))
            self.connection.execute("DELETE FROM news_screen_active WHERE seen_at < ?",
                                    (self.now().timestamp() - ACTIVE_SECONDS,))

    # -- the background loop

    def wake(self) -> None:
        """Start a round soon (the reader's 10-second gap): a looked-at product has pairs waiting."""
        self._signal(True)

    def _signal(self, urgent: bool) -> None:
        if urgent:
            self._urgent = True
        loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            pass  # the loop is closing

    def start(self) -> None:
        if self._task is None:
            self._loop = asyncio.get_running_loop()
            self._wake = asyncio.Event()
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _due(self) -> float | None:
        if not self._pending or not self.usable()[0]:
            return None
        return self._last_round + (URGENT_GAP_SECONDS if self._urgent else BACKGROUND_GAP_SECONDS)

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            due = self._due()
            delay = 60.0 if due is None else due - self.clock()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=min(delay, 60.0))
                except TimeoutError:
                    pass
                self._wake.clear()
                continue
            try:
                await self.burst()
            except Exception:  # noqa: BLE001 — one bad burst never ends the loop
                log.warning("news_screen_burst_failed", exc_info=True)

    async def burst(self) -> int:
        """Up to ROUNDS_PER_BURST model calls while work is queued and the model is usable."""
        self._urgent = False
        future = self._burst = asyncio.get_running_loop().create_future() if self._burst is None or self._burst.done() else self._burst
        judged = 0
        try:
            for _ in range(ROUNDS_PER_BURST):
                if not self._pending or not self.usable()[0]:
                    break
                judged += await self.run_round()
        finally:
            self._last_round = self.clock()
            if not future.done():
                future.set_result(None)
        return judged

    async def settle(self, product: str, timeout: float) -> None:
        """Wait (bounded) until this product's queued pairs are judged: the desk's 快讯 facts."""
        if self._task is None or not self._busy(product) or not self.usable()[0]:
            return
        self._signal(True)
        deadline = self.clock() + timeout
        while self._busy(product) and self.usable()[0]:
            left = deadline - self.clock()
            if left <= 0:
                return
            loop = asyncio.get_running_loop()
            if self._burst is None or self._burst.done():
                self._burst = loop.create_future()
            try:
                await asyncio.wait_for(asyncio.shield(self._burst), timeout=left)
            except TimeoutError:
                return

    async def run_round(self) -> int:
        """One model call over a batch taken in turn across products (`_take`); the number of pairs judged."""
        generator, model = self._adapter()
        if generator is None:
            return 0
        looked = self.looked()
        with self._guard:
            batch = self._take(looked)
        if not batch:
            return 0
        try:
            return await self._judge(generator, model, batch)
        finally:  # whatever happened, nothing this round took stays in flight
            with self._guard:
                self._inflight.difference_update(key for key, _ in batch)

    async def _judge(self, generator: Any, model: str | None, batch: list[tuple[tuple[str, str], dict[str, Any]]]) -> int:
        # A pair judged since it was queued (another round, a restart's store) is not judged again.
        stored: set[tuple[str, str]] = set()
        for product in {key[1] for key, _ in batch}:
            stored.update((item_id, product) for item_id in self.verdicts(product, [key[0] for key, _ in batch if key[1] == product]))
        if stored:
            self._settled(stored)
            batch = [(key, entry) for key, entry in batch if key not in stored]
            if not batch:
                return 0
        today_research = research_day(self.now())
        if not self._budget.take(self._today(), self.daily_calls):
            self._requeue(batch, count=False)  # the day's last call went elsewhere: the pairs wait
            return 0
        keyed = {str(index + 1): (key, entry) for index, (key, entry) in enumerate(batch)}
        labels = {key[1]: f"{entry['name']}（{key[1]}）" for _, (key, entry) in keyed.items()}
        own = {product: self.product_groups(product) for product in labels}
        known = {product: self.event_labels(product, today_research) for product in labels}
        payload = {"groups": list(DROP_GROUPS),
                   "productGroups": {labels[product]: list(groups) for product, groups in own.items() if groups},
                   "knownLabels": {labels[product]: names for product, names in known.items() if names},
                   "pairs": [{"key": label, "product": labels[key[1]], "source": entry["source"],
                              "time": entry["publishedAt"][:16], "title": entry["title"][:80],
                              "text": entry["text"][:TEXT_CHARS]} for label, (key, entry) in keyed.items()]}
        started = self.clock()
        try:
            result = await generator.complete_structured(
                instructions=INSTRUCTIONS, input_text=json.dumps(payload, ensure_ascii=False), schema=ScreenBatch,
                timeout_seconds=CALL_TIMEOUT_SECONDS, model=model, usage_user=USAGE_USER)
        except asyncio.CancelledError:
            self._requeue(batch, count=False)
            raise
        except Exception as error:  # noqa: BLE001 — a failing model only pauses screening
            code = str(error) if isinstance(error, RuntimeError | ValueError) and str(error) else type(error).__name__
            log.warning("news_screen_failed pairs=%d error=%s", len(batch), code)
            self._failure = (self.clock() + FAILURE_BACKOFF_SECONDS, code)
            self._requeue(batch, count=True)
            return 0
        self._failure = None
        answered: dict[tuple[str, str], dict[str, Any]] = {}
        for verdict in result.value.verdicts:
            entry = keyed.get(str(verdict.key).strip())
            if entry is not None:
                answered[entry[0]] = normalise_verdict(verdict, DROP_GROUPS + own.get(entry[0][1], ()))
        judged_at = self.now().isoformat(timespec="seconds")
        keyed_entries = {key: entry for key, entry in batch}
        with self.lock, self.connection:
            for (item_id, product), value in answered.items():
                self.connection.execute(
                    "INSERT INTO news_screen (item_id, product, relevant, event, direction, strength, reason, drop_reason,"
                    " model, judged_at, transmission, event_label, published_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(item_id, product) DO UPDATE SET"
                    " relevant = excluded.relevant, event = excluded.event, direction = excluded.direction,"
                    " strength = excluded.strength, reason = excluded.reason, drop_reason = excluded.drop_reason,"
                    " model = excluded.model, judged_at = excluded.judged_at, transmission = excluded.transmission,"
                    " event_label = excluded.event_label, published_at = excluded.published_at",
                    (item_id, product, int(value["relevant"]), value.get("event"), value.get("direction"),
                     value.get("strength"), value.get("reason"), value.get("dropReason"),
                     model or getattr(generator, "model", None) or "default", judged_at, value.get("transmission"),
                     value.get("eventLabel"), keyed_entries[(item_id, product)]["publishedAt"] or None))
        self._settled(set(answered))  # stored first: a reader never sees a judged pair as unseen
        self._requeue([(key, entry) for key, entry in batch if key not in answered], count=True)
        if answered and self.on_judged is not None:
            self.on_judged({product for _, product in answered})
        log.info("news_screen_round pairs=%d judged=%d dropped=%d tokens=%d seconds=%.1f", len(batch), len(answered),
                 sum(1 for value in answered.values() if not value["relevant"]),
                 int(getattr(result, "tokens", 0) or 0), self.clock() - started)
        return len(answered)

    def _settled(self, keys: set[tuple[str, str]]) -> None:
        """Judged pairs: out of flight, any duplicate queued meanwhile cleared, failures forgotten."""
        with self._guard:
            for key in keys:
                self._inflight.discard(key)
                self._pending.pop(key, None)
                self._attempts.pop(key, None)

    def _requeue(self, batch: list[tuple[tuple[str, str], dict[str, Any]]], *, count: bool) -> None:
        with self._guard:
            for key, entry in batch:
                self._inflight.discard(key)
                tries = self._attempts.get(key, 0) + (1 if count else 0)
                self._attempts[key] = tries
                if tries < MAX_ATTEMPTS:
                    self._pending[key] = entry
            if len(self._attempts) > MAX_PENDING * 4:
                for key in list(self._attempts)[:MAX_PENDING]:
                    self._attempts.pop(key, None)


def keyword_note(reason: str | None) -> str:
    return {"news_screen_budget_exhausted": "今天的 AI 筛选额度已用完",
            "news_screen_model_failing": "AI 筛选暂时失败，稍后自动重试",
            }.get(reason or "", "AI 筛选暂不可用") + "；新条目按关键词列出，未经 AI 筛选。"


def screen_note(view: dict[str, Any]) -> str | None:
    """The view's note: why it is keyword-only, or how many shown items the model failed to judge."""
    if view["mode"] != "ai":
        return keyword_note(view["reason"])
    if view.get("unscreened"):
        return f"{view['unscreened']} 条 AI 判断失败，按关键词显示，未经 AI 筛选。"
    return None


def screen_summary(view: dict[str, Any], dropped: list[dict[str, Any]],
                   visible: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A reader's `screen`: mode, pending, `unscreened` (the items actually shown — `visible`, by
    default all kept — that the model failed to judge twice and carry no reading, each news once:
    Codex #516 10-07 counted failed news×product pairs, even ones another product judged or the
    limit left out), the dropped fold and, when there is one, the note the page shows."""
    shown = view["kept"] if visible is None else visible
    unscreened = len({item["id"] for item in shown if item.get("ai") is None and item.get("unjudged") == "failed"})
    view = {**view, "unscreened": unscreened}
    screen: dict[str, Any] = {"mode": view["mode"], "pending": view["pending"], "unscreened": unscreened,
                              "dropped": drop_summary(dropped)}
    note = screen_note(view)
    if note:
        screen["note"] = note
    return screen


AI_KEYS = ("event", "direction", "strength", "reason", "transmission", "eventLabel")


def screen_view(screener: NewsScreener | None, product: str, name: str, candidates: list[tuple[dict[str, Any], bool]],
                keyword_drop: Callable[[dict[str, Any]], str | None], *, urgent: bool = True) -> dict[str, Any]:
    """One product's view of recalled candidates, newest first as given.

    `candidates` pairs each item with `direct`: recalled for this product by its words (else a
    macro item offered for it). Returns kept items (each with `ai` or None), dropped items (with
    dropReason / droppedBy), the pending count (queued or in flight), the shown items the model
    failed to judge (`unscreened`) and the screening mode."""
    usable, reason = screener.usable() if screener is not None else (False, "news_screen_model_unavailable")
    verdicts = screener.verdicts(product, [item["id"] for item, _ in candidates]) if screener is not None else {}
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    queue: list[tuple[dict[str, Any], str, str]] = []
    pending = unscreened = 0
    for item, direct in candidates:
        group = keyword_drop(item) if direct else None
        if group is not None:
            dropped.append({**item, "ai": None, "dropReason": group, "droppedBy": "keyword"})
            continue
        verdict = verdicts.get(item["id"])
        if verdict is not None:
            if verdict["relevant"]:
                kept.append({**item, "ai": {**{key: verdict[key] for key in AI_KEYS}, "product": product}, "direct": direct})
            else:
                dropped.append({**item, "ai": None, "dropReason": verdict["dropReason"], "droppedBy": "ai"})
            continue
        state = screener.state(item["id"], product) if screener is not None else None
        unjudged = "keyword"
        if state == "inflight" or (state == "queued" and usable):
            pending += 1
            unjudged = "pending"
        elif state is None and usable:
            queue.append((item, product, name))
            pending += 1
            unjudged = "pending"
        elif state == "failed" and direct:
            unscreened += 1
            unjudged = "failed"
        if direct:  # a recalled item stays on its keywords until judged; a macro item waits for the model
            # `unjudged`: pending (the model will judge it — the events leave it out meanwhile), failed (judged
            # twice without an answer) or keyword (no model this time); both of the latter show on keywords.
            kept.append({**item, "ai": None, "direct": True, "unjudged": unjudged})
    if screener is not None:
        if urgent:  # a reader looked; the poller's own refill must not keep a product active forever
            screener.touch(product, name)
        if queue:
            screener.want(queue, urgent=urgent)
    return {"kept": kept, "dropped": dropped, "pending": pending, "unscreened": unscreened,
            "mode": "ai" if usable else "keyword", "reason": reason}


def drop_summary(dropped: list[dict[str, Any]], limit: int = 100) -> dict[str, Any]:
    groups: dict[str, int] = {}
    for item in dropped:
        groups[item["dropReason"]] = groups.get(item["dropReason"], 0) + 1
    return {"count": len(dropped),
            "groups": [{"reason": reason, "count": count}
                       for reason, count in sorted(groups.items(), key=lambda pair: (-pair[1], pair[0]))],
            "items": dropped[:limit]}


_STRENGTH_RANK = {"弱": 1, "中": 2, "强": 3}


def _tier(ai: dict[str, Any] | None) -> str:
    if ai is None:
        return "unscreened"
    if _STRENGTH_RANK.get(ai.get("strength") or "", 0) >= 2:
        return "major"
    return "minor" if ai.get("direction") == "中性" else "other"


def _event_id(*parts: str) -> str:
    return "e" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


# Round-up posts ("美股盘前要闻一览：……", "隔夜要闻汇总") carry many stories: never an event's title.
_ROUNDUP = re.compile(r"一览|汇总|要闻|早报|晚报|午报|速递|精选|图示|集锦|^\s*\d+[、.．]")
_TITLE_NOISE = re.compile(r"[\s\W_]+")


def _title_key(title: str) -> str:
    return _TITLE_NOISE.sub("", title)


def _merge_key(member: dict[str, Any]) -> str:
    """The key a report's title lends the cross-group merge — "" when the title is research_live's
    truncated stand-in (新浪/上海金属网 have no title: the body's first 18 chars + …). Two different
    stories can share that lead, so it may never say two reports are the same story (#618); a real
    title, or a body so short the stand-in is the whole of it, still merges word for word."""
    title = str(member.get("title") or "")
    text = str(member.get("text") or "")
    if len(text) > 18 and title == text[:18] + "…":
        return ""
    return _title_key(title)


def _headline(members: list[dict[str, Any]], label: str | None = None) -> dict[str, Any]:
    """The report whose title says the event best (members newest first): not a round-up post, as much
    of the event's name in it as possible, not cut ("…"), with figures, then the fuller one (up to 60
    characters); ties keep the newest. 10-07: the longest title alone picked "美股盘前要闻一览：……"
    for a gold story, and the newest alone picked a bare "美债收益率"."""
    wanted = {ch for ch in (label or "") if not ch.isspace()}

    def score(member: dict[str, Any]) -> tuple[int, int, int, int, int]:
        title = str(member.get("title") or "").strip()
        overlap = round(10 * len(wanted & set(title)) / len(wanted)) if wanted else 0
        return (0 if _ROUNDUP.search(title) else 1, overlap, 0 if title.endswith(("…", "...")) else 1,
                1 if any(ch.isdigit() for ch in title) else 0, min(len(title), 60))
    return max(members, key=score)


def build_events(items: list[dict[str, Any]], *, hide_pending: bool) -> list[dict[str, Any]]:
    """Kept items (newest first) merged into events: one product's items of one research day that the
    model named the same (`eventLabel`) are one event. The event shows its newest report's time, the
    most informative title (`_headline`), the strongest reading (direction 分歧 when its reports point
    both ways), how many outlets
    ran it and every report with its own link; `tier` is 重要 (major: 中/强), 其他 (other: 弱 with a
    direction) or 次要 (minor: 弱 and 中性). An item without a label (judged before labels existed) is
    its own event. `hide_pending`: items the model has not judged yet stay out (ai mode); items on
    keywords only (failed twice, or no model) are their own `unscreened` events."""
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for item in items:
        ai = item.get("ai")
        if ai is None and hide_pending and item.get("unjudged") == "pending":
            continue
        day = research_day(item.get("publishedAt"))
        label = (ai or {}).get("eventLabel")
        key: tuple[str, ...] = (str((ai or {}).get("product") or ""), day, label) if label else ("item", item["id"])
        groups.setdefault(key, []).append(item)
    # The same headline under two names (the model labelled one story twice) is one event.
    owner: dict[str, tuple[str, ...]] = {}
    for group_key in list(groups):
        members = groups[group_key]
        titled = [_merge_key(member) for member in members]
        target = next((owner[title] for title in titled if title and owner.get(title) not in (None, group_key)
                       and owner[title] in groups), None)
        # Word for word the same report is one story even across the 08:30 turn (同花顺 filed it at
        # 08:10, the rest after); a recurring name alone ("现货黄金日内下跌") stays per research day.
        if target is not None and target[0] != "item" and group_key[0] != "item":
            groups[target].extend(members)
            del groups[group_key]
            group_key = target
        for title in titled:
            if title:
                owner.setdefault(title, group_key)
    events: list[dict[str, Any]] = []
    for group_key, members in groups.items():
        members.sort(key=lambda member: (member.get("publishedAt") or "", member["id"]), reverse=True)
        newest = members[0]
        headline = _headline(members, group_key[2] if group_key[0] != "item" else None)
        readings = [member["ai"] for member in members if member.get("ai")]
        strongest = max(readings, key=lambda ai: _STRENGTH_RANK.get(ai.get("strength") or "", 0)) if readings else None
        ai = None
        if strongest is not None:
            directions = {reading.get("direction") for reading in readings}
            ai = {**strongest, "direction": SPLIT if {"偏多", "偏空"} <= directions else strongest.get("direction")}
        sources: list[str] = []
        tags: list[str] = []
        for member in members:
            for source in member.get("sources") or [member.get("source") or ""]:
                if source and source not in sources:
                    sources.append(source)
            for tag in member.get("tags") or []:
                if tag not in tags:
                    tags.append(tag)
        events.append({
            "id": _event_id(*group_key) if group_key[0] != "item" else newest["id"],
            "eventLabel": group_key[2] if group_key[0] != "item" else None,
            "researchDay": research_day(newest.get("publishedAt")),
            "publishedAt": newest.get("publishedAt"), "firstAt": members[-1].get("publishedAt"),
            "title": headline.get("title") or "", "text": headline.get("text") or "",
            "origTitle": headline.get("origTitle"), "tags": tags,
            "ai": ai, "tier": _tier(ai), "reports": max(len(sources), 1), "sources": sources,
            "direct": any(member.get("direct", True) for member in members),
            "items": [{"id": member["id"], "source": member.get("source") or "",
                       "sources": member.get("sources") or [member.get("source") or ""],
                       "publishedAt": member.get("publishedAt"), "title": member.get("title") or "",
                       "text": member.get("text") or "", "link": member.get("link"),
                       "origTitle": member.get("origTitle"), "tags": member.get("tags") or []}
                      for member in members],
        })
    events.sort(key=lambda event: (event["publishedAt"] or "", event["id"]), reverse=True)
    return events


def layer_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    """How many events each tier holds; the folded 次要 tier also counts its reports by event name
    (the three biggest named, the rest as 其他)."""
    counts = {tier: sum(1 for event in events if event["tier"] == tier) for tier in ("major", "other", "minor", "unscreened")}
    minor = [event for event in events if event["tier"] == "minor"]
    named: dict[str, int] = {}
    for event in minor:
        name = event["eventLabel"] or "其他"
        named[name] = named.get(name, 0) + len(event["items"])
    ranked = sorted(named.items(), key=lambda pair: (pair[0] == "其他", -pair[1], pair[0]))
    shown = [(name, count) for name, count in ranked if name != "其他"][:MINOR_GROUPS_NAMED]
    rest = sum(count for name, count in ranked if (name, count) not in shown)
    groups = [{"label": name, "count": count} for name, count in shown] + ([{"label": "其他", "count": rest}] if rest else [])
    return {**counts, "minorReports": sum(len(event["items"]) for event in minor), "minorGroups": groups}

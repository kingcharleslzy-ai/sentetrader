"""Batch translation of English flash titles (FinancialJuice and later English wires).

The poller offers every untranslated English item of the retain window that carries a product or
macro tag; the background loop translates queued titles in one cheap structured model call per
batch, through the Runtime's own adapter and the platform usage marker `platform:news-translate`
— never a new provider. The Chinese title is written into `news_flash.title` and the original
stays in `orig_title` (the page folds it behind the row); each translation is also kept in
`news_translation`, so a restart does not translate an item twice.

Calls are capped per Beijing day (`AI_TRADER_NEWS_TRANSLATE_DAILY_CALLS`, default 100). Over the
cap, without an authenticated adapter, or while the model fails, the queue simply waits: readers
keep seeing the original English title, which is always a valid state, never an error.

Only the instance-lease holder runs the background loop (wiring/producers.py): a standby slot
queues offers but never calls the model; a queued item another process translated meanwhile is
dropped before the call (the news_translation re-check below).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from json import dumps as json_dumps
from typing import Any

from pydantic import BaseModel, ConfigDict

from .daily_budget import DailyCallBudget

log = logging.getLogger("uvicorn.error.research")

# The translations' tokens book against this marker, never the quota of whoever opened the page.
USAGE_USER = "platform:news-translate"
CHINA_TZ = timezone(timedelta(hours=8))
BATCH_TITLES = 24
TITLE_CHARS = 120
CALL_TIMEOUT_SECONDS = 60
FAILURE_BACKOFF_SECONDS = 5 * 60
MAX_PENDING = 1000
TRANSLATE_GAP_SECONDS = 30.0

INSTRUCTIONS = """你是快讯标题翻译。输入是若干条英文快讯标题，每条有一个 key。逐条把标题翻成简体中文：
- 翻成简洁的新闻标题中文，保留数字、单位与百分比；机构与人物名用通行中文译名（如 Fed→美联储、OPEC→欧佩克、Treasury→美国财政部、LME→伦交所）。
- 只译标题本身，不添加标题里没有的信息，不解释、不扩展。
- title 是翻译后的中文标题；已是中文或译不动的原样返回。
每个 key 都要回答一次。"""


class TitleTranslation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str
    title: str | None = None


class TranslationBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    translations: list[TitleTranslation]


def is_english_title(title: Any) -> bool:
    """Mostly Latin letters with real words, not a Chinese headline with an English ticker in it."""
    text = str(title or "")
    letters = sum(1 for ch in text if "a" <= ch <= "z" or "A" <= ch <= "Z")
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return letters >= 8 and letters > cjk * 3


@dataclass
class NewsTranslator:
    """One background loop over queued English titles; see the module docstring."""

    connection: sqlite3.Connection
    lock: threading.Lock | threading.RLock
    # The Runtime's shared model adapter (set by the research routes); None: originals shown.
    generator: Callable[[], Any] | None = None
    model: str = field(default_factory=lambda: os.getenv(
        "AI_TRADER_NEWS_TRANSLATE_MODEL", os.getenv(
            "AI_TRADER_NEWS_SCREEN_MODEL", os.getenv("AI_TRADER_RESEARCH_DESK_MODEL", "deepseek-flash"))).strip())
    daily_calls: int = field(default_factory=lambda: int(os.getenv("AI_TRADER_NEWS_TRANSLATE_DAILY_CALLS", "100") or 0))
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    _pending: OrderedDict[str, str] = field(default_factory=OrderedDict, repr=False)
    _guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _failure: tuple[float, str] | None = field(default=None, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _wake: asyncio.Event | None = field(default=None, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS news_translation (item_id TEXT PRIMARY KEY,"
                " title_zh TEXT NOT NULL, model TEXT, translated_at TEXT NOT NULL)")
        # The Beijing-day call counter lives in the shared database: a restart or a blue-green
        # handoff must not hand the day a second full allowance.
        self._budget = DailyCallBudget("news_translate", self.connection, self.lock)
        # 2026-10-12: the instance lease decides who translates (one owner for every background
        # producer), so this version no longer uses the translator's own `news_translate_lease` nor
        # the `news_translate_day` counter. Both tables stay: during a blue-green switch the previous
        # version still runs on the same database and queries them (its translate loop and close()),
        # and dropping them would fail its stop and hold the instance lease until the stale window.
        # The old counter's days are copied once into the shared budget.
        with self.lock, self.connection:
            if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'news_translate_day'").fetchone():
                self.connection.execute("INSERT OR IGNORE INTO model_call_days (scope, day, calls)"
                                        " SELECT 'news_translate', day, calls FROM news_translate_day")

    # -- state

    def _adapter(self) -> tuple[Any | None, str | None]:
        generator = self.generator() if self.generator is not None else None
        if generator is None or not hasattr(generator, "complete_structured") or not getattr(generator, "agent_authenticated", False):
            return None, None
        endpoints = getattr(generator, "endpoints", None)
        model = self.model if self.model and (not isinstance(endpoints, dict) or self.model in endpoints) else None
        return generator, model

    def _today(self) -> str:
        return self.now().astimezone(CHINA_TZ).date().isoformat()

    def _used(self) -> int:
        """Today's model calls, counted across processes in the shared store."""
        return self._budget.used(self._today())

    def usable(self) -> tuple[bool, str | None]:
        """Whether titles can be translated now; the reason (a stable code) when not."""
        if self._adapter()[0] is None:
            return False, "news_translate_model_unavailable"
        if self._used() >= self.daily_calls:
            return False, "news_translate_budget_exhausted"
        if self._failure is not None and self._failure[0] > self.clock():
            return False, "news_translate_model_failing"
        return True, None

    def usage(self) -> dict[str, Any]:
        return {"day": self._today(), "calls": self._used(), "dailyCalls": self.daily_calls, "pending": len(self._pending)}

    # -- queue

    def offer(self, items: list[dict[str, Any]]) -> int:
        """Queue (id, title) pairs of untranslated items; the number newly queued. Already
        translated or non-English titles are skipped — the queue only ever holds work."""
        candidates = {str(item.get("id") or ""): str(item.get("title") or "") for item in items}
        candidates = {key: title for key, title in candidates.items() if key and is_english_title(title)}
        if not candidates:
            return 0
        with self.lock:
            rows = self.connection.execute(
                "SELECT item_id FROM news_translation WHERE item_id IN"
                f" ({','.join('?' * len(candidates))})", tuple(candidates)).fetchall()
        translated = {row[0] for row in rows}
        added = 0
        with self._guard:
            for key, title in candidates.items():
                if key in self._pending or key in translated:
                    continue
                self._pending[key] = title
                added += 1
            while len(self._pending) > MAX_PENDING:
                self._pending.popitem(last=False)
        if added:
            self._signal()
        return added

    def _signal(self) -> None:
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

    def prune(self) -> None:
        """Follow `news_flash`'s retention: a translation whose item is gone goes with it."""
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM news_translation WHERE item_id NOT IN (SELECT id FROM news_flash)")

    # -- the background loop

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            if self._pending and self.usable()[0]:
                # Work is queued and the model is usable: translate now, not on the next wake.
                try:
                    await self.burst()
                except Exception:  # noqa: BLE001 — one bad burst never ends the loop
                    log.warning("news_translate_burst_failed", exc_info=True)
                # Between bursts: a startup backlog must not burn the day's calls in one minute.
                # A new offer (or close()'s cancel) ends the gap early instead of sleeping blind.
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=TRANSLATE_GAP_SECONDS)
                except TimeoutError:
                    pass
                self._wake.clear()
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=60.0)
            except TimeoutError:
                pass
            self._wake.clear()

    async def burst(self) -> int:
        """Up to two rounds per wake while work is queued and the model is usable."""
        translated = 0
        for _ in range(2):
            if not self._pending or not self.usable()[0]:
                break
            translated += await self.run_round()
        return translated

    async def run_round(self) -> int:
        """One model call over the oldest queued titles; the number of titles translated."""
        if not self.usable()[0]:
            return 0
        generator, model = self._adapter()
        if generator is None:
            return 0
        with self._guard:
            batch = list(self._pending.items())[:BATCH_TITLES]
            for key, _ in batch:
                self._pending.pop(key, None)
        if not batch:
            return 0
        # Only the lease holder calls, but every process queues: items another process translated
        # since they were queued (a handoff) are dropped here instead of being paid for twice — and so
        # are items the feed's retention deleted while they waited (#516): no call is spent on them.
        marks = ",".join("?" * len(batch))
        keys_ = tuple(key for key, _ in batch)
        with self.lock:
            done = {row[0] for row in self.connection.execute(
                f"SELECT item_id FROM news_translation WHERE item_id IN ({marks})", keys_)}  # noqa: S608 — placeholders only
            present = {row[0] for row in self.connection.execute(
                f"SELECT id FROM news_flash WHERE id IN ({marks})", keys_)}  # noqa: S608 — placeholders only
        batch = [(key, title) for key, title in batch if key not in done and key in present]
        if not batch:
            return 0
        if not self._budget.take(self._today(), self.daily_calls):
            self._requeue(batch)  # the day's last call went elsewhere: the batch waits for tomorrow
            return 0
        keys = [key for key, _ in batch]
        try:
            count = await self._translate(generator, model, batch)
        except asyncio.CancelledError:
            self._requeue(batch)
            raise
        except Exception as error:  # noqa: BLE001 — a failing model only pauses translation
            code = str(error) if isinstance(error, RuntimeError | ValueError) and str(error) else type(error).__name__
            log.warning("news_translate_failed titles=%d error=%s", len(batch), code)
            self._failure = (self.clock() + FAILURE_BACKOFF_SECONDS, code)
            self._requeue(batch)
            return 0
        self._failure = None
        self._forget(keys)
        return count

    async def _translate(self, generator: Any, model: str | None, batch: list[tuple[str, str]]) -> int:
        keyed = {str(index + 1): (key, title) for index, (key, title) in enumerate(batch)}
        # sent: the title as the model actually receives it (a headline over 200 characters goes out
        # truncated) — the unchanged test must compare with this, not the full queued original.
        sent = {label: title[:200] for label, (_, title) in keyed.items()}
        payload = {"titles": [{"key": label, "title": title} for label, title in sent.items()]}
        started = self.clock()
        result = await generator.complete_structured(
            instructions=INSTRUCTIONS, input_text=json_dumps(payload), schema=TranslationBatch,
            timeout_seconds=CALL_TIMEOUT_SECONDS, model=model, usage_user=USAGE_USER)
        # (title 截断后, original, 模型原样返回) — the unchanged test compares the model's unsliced
        # answer with the title it was sent, before TITLE_CHARS shortens either side of it.
        answered: dict[str, tuple[str, str, bool]] = {}
        for translation in result.value.translations:
            label = str(translation.key).strip()
            entry = keyed.get(label)
            raw = (translation.title or "").strip()
            if entry is not None and raw:
                answered[entry[0]] = (raw[:TITLE_CHARS], entry[1], raw == sent[label])
        if not answered:
            raise LookupError("news_translate_empty")
        judged_at = self.now().isoformat(timespec="seconds")
        recorded = 0
        with self.lock, self.connection:
            for item_id, (title, original, unchanged) in answered.items():
                if unchanged:
                    # The model returned the title as it was (已是中文或译不动): processed, not
                    # translated — but only while the row still carries that title.
                    still = self.connection.execute(
                        "SELECT 1 FROM news_flash WHERE id = ? AND title = ?", (item_id, original)).fetchone()
                    if still is None:
                        continue
                else:
                    # The original title is kept (folded behind the row); an item translated meanwhile
                    # (another round, a restart) keeps its first translation. The queued-title guard
                    # ties the answer to the headline it translated: a merge that replaced the row's
                    # title mid-call leaves it for the next offer instead of overwriting it.
                    applied = self.connection.execute(
                        "UPDATE news_flash SET title = ?, orig_title = title"
                        " WHERE id = ? AND orig_title IS NULL AND title = ?",
                        (title, item_id, original)).rowcount
                    if not applied:
                        continue
                self.connection.execute(
                    "INSERT INTO news_translation (item_id, title_zh, model, translated_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(item_id) DO UPDATE SET title_zh = excluded.title_zh, model = excluded.model,"
                    " translated_at = excluded.translated_at",
                    (item_id, title, model or getattr(generator, "model", None) or "default", judged_at))
                recorded += 1
        log.info("news_translate_round titles=%d translated=%d tokens=%d seconds=%.1f", len(batch),
                 sum(1 for _, original, unchanged in answered.values() if not unchanged),
                 int(getattr(result, "tokens", 0) or 0), self.clock() - started)
        return recorded

    def _requeue(self, batch: list[tuple[str, str]]) -> None:
        with self._guard:
            for key, title in batch:
                self._pending[key] = title

    def _forget(self, keys: list[str]) -> None:
        with self._guard:
            for key in keys:
                self._pending.pop(key, None)

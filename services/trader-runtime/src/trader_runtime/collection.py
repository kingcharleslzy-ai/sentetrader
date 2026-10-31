"""统一采集协调器（2026-10-18，#976 第 1 步）：外部数据集的“读一次、大家共用”只有这一个实现。

所有者要求行情与新闻采集走统一的接口与模块。采集分三层职责，本模块只管中间那层：

* 数据源函数（各 research_* / sina_* 模块里现有的取数与解析）只负责“取回来、解析成 dict”，
  不管缓存、调度与失败记账；
* 本模块的 ``Collector``：同一个键同一时刻只有一次取数（单飞），调用方最多等 ``wait`` 秒，
  成功按 TTL、失败或空结果按短的重试间隔排下一次，好值由 ``LastGood`` 保存（有数据库时持久化、
  重启和另一槽位读回），失败永不覆盖好值、只把它标成 stale；
* 使用方（路由、AI 工具、调研台）只调数据集的读函数，不直接碰数据源。

不在本模块：后台轮询的启停（仍归租约持有者，wiring/producers.py）、快讯入库与去重（research_live）、
快期实时订阅与号池接力（shared_market + sidecar）。一个 ``Collector`` 是一条独立车道（自己的线程池）：
慢网站只拖住自己那条车道，报价类与新闻类不共用。

取数函数的约定：返回 dict。``status == "ok"`` 是成功；带 ``partial`` 的成功照存，但按重试间隔再取；
其余（含空结果，由数据源自己判成非 ok）是失败，``error`` 写明原因。抛异常也是失败，记成
``<error_prefix>:<异常类名>``；``passthrough`` 里的错误（如没装 akshare）原样返回、不排重试。
调用方等到 ``wait`` 还没结束时，取数继续跑（不取消、不再并起第二个），本键按重试间隔退避，
之后的调用方加入这次仍在跑的取数。按值判新鲜度（``valid_for``）的读者可再给 ``epoch``（如交易日）：
失败退避只在发起那次取数的 epoch 内有效，epoch 换了（交易日翻篇）立即可取（#932）。
``close()`` 之后不再起新的取数，仍在跑的取数结束时也不再写入存储（#932）。
异步源用 ``AsyncCollector``（同样的规则、同一个 ``LastGood``）；一个请求能带多个键的源（新浪报价列表）用
``AsyncCollector.get_many``：还新鲜的键直接给存下的值，别人正在取的键加入那次取数，其余到期的键合成一次请求（#976 第 7 步）。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

from .last_good import Kept, LastGood

log = logging.getLogger(__name__)

FETCH_TIMEOUT = "fetch_timeout"


@dataclass(frozen=True)
class Request:
    """One key of a batch read: its fetch and the error it reports with no value at all."""

    key: str
    fetch: Callable[[], dict[str, Any]]
    unavailable: str


@dataclass
class Collector:
    """One lane of shared, single-flight reads over a ``LastGood`` store."""

    kept: LastGood
    lane: str
    workers: int = 1
    error_prefix: str = "source_error"
    passthrough: frozenset[str] = frozenset()
    timeout_error: str = FETCH_TIMEOUT  # the error a reader that stopped waiting sees (the fetch keeps running)
    _pool: ThreadPoolExecutor = field(init=False, repr=False)
    _inflight: dict[str, Future[dict[str, Any]]] = field(default_factory=dict, init=False, repr=False)
    _epochs: dict[str, object] = field(default_factory=dict, init=False, repr=False)  # the epoch each fetch started in
    _closed: bool = field(default=False, init=False, repr=False)
    # Re-entrant: a future that is already done runs its done-callback (which takes the guard) inline.
    _guard: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix=f"collect-{self.lane}")

    def read(self, key: str, fetch: Callable[[], dict[str, Any]], *, ttl: float, retry: float, wait: float,
             unavailable: str, valid_for: Callable[[Kept], float] | None = None,
             epoch: object = None) -> dict[str, Any]:
        """``key``'s payload: the kept value until it is due, else one shared fetch (waited on for at most
        ``wait`` seconds). A failure keeps the good value, served stale; with none, ``unavailable``.
        ``valid_for`` (instead of the fixed ``ttl``): how long the stored value stays fresh, from its fetch
        time, decided by the value itself (a trading day turning over, a settlement published). ``epoch`` (with
        ``valid_for``): a failure's back-off holds only within the epoch its fetch started in (the trading day)."""
        return self.read_many([Request(key, fetch, unavailable)], ttl=ttl, retry=retry, wait=wait,
                              valid_for=valid_for, epoch=epoch)[key]

    def read_many(self, requests: list[Request], *, ttl: float, retry: float, wait: float,
                  valid_for: Callable[[Kept], float] | None = None, epoch: object = None) -> dict[str, dict[str, Any]]:
        """Several keys at once: every due fetch starts before any is waited on, all within one ``wait``."""
        success_ttl = 0.0 if valid_for is not None else ttl  # freshness comes from the value, not a schedule
        answers: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + wait
        todo = requests
        # Round two only for a fetch joined from an earlier epoch (started before the trading day turned): once it
        # has settled, the key is checked again and the current epoch's fetch starts, within the same ``wait``.
        for _ in range(2):
            pending: list[tuple[Request, Future[dict[str, Any]], bool]] = []
            for request in todo:
                key = request.key
                with self._guard:
                    # A finished fetch is recorded by its done-callback; settling here too covers one that finished
                    # an instant ago. The back-off stops new work, it never hides a result that has already arrived.
                    self._settle(key, success_ttl, retry, request.unavailable)
                    if self._closed or not self._due(key, ttl, retry, valid_for, epoch):
                        answers[key] = self.served(key, request.unavailable)
                        continue
                    future = self._inflight.get(key)
                    if future is None:
                        future = self._inflight[key] = self._pool.submit(request.fetch)
                        self._epochs[key] = epoch
                        # Recorded the moment it finishes, whether or not anyone still waits on it.
                        future.add_done_callback(functools.partial(self._settled, key, success_ttl, retry, request.unavailable))
                    pending.append((request, future, epoch is not None and self._epochs.get(key) != epoch))
            again: list[Request] = []
            for request, future, earlier in pending:
                try:
                    future.result(timeout=max(0.0, deadline - time.monotonic()))
                except FutureTimeout:
                    pass
                except Exception:  # noqa: BLE001 — recorded once by _settle below
                    pass
                key = request.key
                with self._guard:
                    # One decision under the guard: done (at any moment before this check) → recorded now, whether
                    # or not its callback got here first; still running → back off, later callers join it.
                    if future.done():
                        self._settle(key, success_ttl, retry, request.unavailable)
                    elif self._inflight.get(key) is future and not self._closed:
                        self.kept.failed(key, self.timeout_error, retry)
                    # Every reader of this fetch (not only the one that recorded it) answers a passthrough error.
                    answers[key] = self._passthrough(future) or self.served(key, request.unavailable)
                if earlier and future.done():
                    again.append(request)
            if not again:
                break
            todo = again
        return answers

    def _due(self, key: str, ttl: float, retry: float, valid_for: Callable[[Kept], float] | None,
             epoch: object = None) -> bool:
        if valid_for is None:
            return self.kept.due(key, ttl, retry)
        # A failure in an earlier epoch (yesterday's trading day) no longer holds the key back.
        turned = epoch is not None and self._epochs.get(key, epoch) != epoch
        if self.kept.backing_off(key) and not turned:
            return False
        kept = self.kept.get(key)
        return kept is None or self.kept.wall() >= kept.saved_at + valid_for(kept)

    def served(self, key: str, unavailable: str) -> dict[str, Any]:
        """The kept payload (marked stale after a failure), else unavailable with the latest failure."""
        kept = self.kept.get(key)
        if kept is None:
            return {"status": "unavailable", "error": self.kept.error(key) or unavailable}
        return kept.mark(kept.value)

    def close(self) -> None:
        """No fetch starts after this, and one still running records nothing when it ends (the store may be
        closing): under the guard, so no write is under way once this returns."""
        with self._guard:
            self._closed = True
            self._inflight.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _settled(self, key: str, ttl: float, retry: float, unavailable: str, _done: object = None) -> None:
        with self._guard:
            self._settle(key, ttl, retry, unavailable)

    def _passthrough(self, future: Future[dict[str, Any]]) -> dict[str, Any] | None:
        if not future.done() or future.cancelled():
            return None
        error = future.exception()
        code = str(error) if error is not None else future.result().get("error")
        return {"status": "unavailable", "error": code} if code in self.passthrough else None

    def _settle(self, key: str, ttl: float, retry: float, unavailable: str) -> None:
        """Record ``key``'s finished in-flight fetch once (guard held); a passthrough error schedules nothing."""
        future = self._inflight.get(key)
        if future is None or not future.done() or self._closed:
            return
        del self._inflight[key]
        if future.cancelled():  # the lane closed under it
            return
        passed = self._passthrough(future)
        if passed is not None:
            # Not scheduled: due again at once, replacing a fetch_timeout back-off this fetch may have left.
            self.kept.failed(key, passed["error"], 0.0)
            return
        error = future.exception()
        if error is not None:
            log.warning("collect_failed lane=%s key=%s error=%s", self.lane, key, type(error).__name__)
            self.kept.failed(key, f"{self.error_prefix}:{type(error).__name__}", retry)
            return
        payload = future.result()
        if payload.get("status") != "ok":
            self.kept.failed(key, payload.get("error") or unavailable, retry)
            return
        self.kept.succeeded(key, payload, retry if payload.get("partial") else ttl)


class SourceFailed(Exception):
    """An async fetch that did not produce a value: its error code and, when it differs from the read's own,
    how soon to try again (an empty search waits longer than a network error)."""

    def __init__(self, code: str, retry: float | None = None) -> None:
        super().__init__(code)
        self.code, self.retry = code, retry


@dataclass
class AsyncCollector:
    """The event-loop twin of ``Collector`` for async sources (httpx.AsyncClient readers): the same rules over the
    same ``LastGood`` store — one fetch per key at a time, joined by every reader; success kept for ``ttl``; a
    failure (``SourceFailed``, or the time limit) keeps the good value, read stale, and is retried after its retry
    interval. A reader that is cancelled does not cancel the fetch others wait on (``asyncio.shield``); the fetch
    itself is cancelled at ``timeout``."""

    kept: LastGood
    lane: str
    _inflight: dict[str, asyncio.Task[Kept | None]] = field(default_factory=dict, init=False, repr=False)
    _batches: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def get(self, key: str, fetch: Callable[[], Awaitable[Any]], *, ttl: float, retry: float,
                  timeout: float | None, timeout_error: str, unavailable: str) -> Kept:
        """``key``'s kept value (``stale`` after a failed refresh); ``SourceFailed`` only when there is none.
        After ``close()`` no fetch starts: the kept value, or ``SourceFailed``."""
        if self._closed or not self.kept.due(key, ttl):
            return self._kept_or_raise(key, unavailable)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._fill(key, fetch, ttl, retry, timeout, timeout_error, unavailable))
            self._inflight[key] = task
            task.add_done_callback(functools.partial(self._finished, key))
        await asyncio.shield(task)  # possibly a batch read's (get_many) fetch, which answers nothing itself
        return self._kept_or_raise(key, unavailable)

    async def get_many(self, keys: list[str], fetch: Callable[[list[str]], Awaitable[dict[str, Any]]], *,
                       ttl: float, retry: float, timeout: float | None, timeout_error: str) -> dict[str, Kept | None]:
        """Several keys from one source request (a quote list): keys still fresh are served as kept, keys another
        read is already fetching join that fetch, and every other key goes into ONE ``fetch(keys)`` call. Its
        answer maps key → value; a key it leaves out is the source's legitimate "nothing for this key" and is kept
        as ``None`` like any other value. A failure (``SourceFailed``, the time limit, or any exception, recorded as
        ``<lane>:<exception class>``) is recorded on every key of the request: good values stay and read stale.
        The answer has every key: its ``Kept`` (``value`` None for "nothing"), or None when there is none."""
        wanted = list(dict.fromkeys(keys))
        due = [key for key in wanted if not self._closed and key not in self._inflight and self.kept.due(key, ttl)]
        if due:
            batch = asyncio.create_task(self._fill_many(due, fetch, ttl, retry, timeout, timeout_error))
            self._batches.add(batch)
            batch.add_done_callback(self._batches.discard)
            for key in due:
                task = asyncio.create_task(self._joined(batch, key))
                self._inflight[key] = task
                task.add_done_callback(functools.partial(self._finished, key))
        waiting = [running for key in wanted if (running := self._inflight.get(key)) is not None]
        if waiting:
            await asyncio.shield(asyncio.gather(*waiting, return_exceptions=True))
        return {key: self.kept.get(key) for key in wanted}

    async def _fill_many(self, keys: list[str], fetch: Callable[[list[str]], Awaitable[dict[str, Any]]], ttl: float,
                         retry: float, timeout: float | None, timeout_error: str) -> None:
        try:
            values = await (asyncio.wait_for(fetch(keys), timeout) if timeout is not None else fetch(keys))
        except TimeoutError:
            code, wait = timeout_error, retry
        except SourceFailed as failure:
            code, wait = failure.code, retry if failure.retry is None else failure.retry
        except Exception as error:  # noqa: BLE001 — a parse or transport surprise is still this request failing
            code, wait = f"{self.lane}:{type(error).__name__}", retry
        else:
            for key in keys:
                self.kept.succeeded(key, values.get(key), ttl)
            return
        log.warning("collect_failed lane=%s keys=%d error=%s", self.lane, len(keys), code)
        for key in keys:
            self.kept.failed(key, code, wait)

    async def _joined(self, batch: asyncio.Task[None], key: str) -> Kept | None:
        await batch
        return self.kept.get(key)

    async def _fill(self, key: str, fetch: Callable[[], Awaitable[Any]], ttl: float, retry: float,
                    timeout: float | None, timeout_error: str, unavailable: str) -> Kept:
        try:
            value = await (asyncio.wait_for(fetch(), timeout) if timeout is not None else fetch())
        except TimeoutError:
            code, wait = timeout_error, retry
        except SourceFailed as failure:
            code, wait = failure.code, retry if failure.retry is None else failure.retry
        else:
            return self.kept.succeeded(key, value, ttl)
        log.warning("collect_failed lane=%s key=%s error=%s", self.lane, key, code)
        self.kept.failed(key, code, wait)
        return self._kept_or_raise(key, unavailable)

    def _finished(self, key: str, done: asyncio.Task[Kept]) -> None:
        if self._inflight.get(key) is done:
            del self._inflight[key]

    def _kept_or_raise(self, key: str, unavailable: str) -> Kept:
        kept = self.kept.get(key)
        if kept is None:
            raise SourceFailed(self.kept.error(key) or unavailable)
        return kept

    def pending(self) -> list[asyncio.Task[Kept | None]]:
        return list(self._inflight.values())

    async def close(self) -> None:
        self._closed = True
        tasks = [*self.pending(), *self._batches]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

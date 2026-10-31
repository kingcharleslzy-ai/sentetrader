"""回测并发配置 (2026-10-12)：一处解析，规则回测队列/进程池与 AI 官方回测共用。"""
from __future__ import annotations

import asyncio

import pytest

from trader_runtime import strategy_backtest
from trader_runtime.backtest_settings import load_backtest_settings
from trader_runtime.strategy_backtest import QUEUE_TIMEOUT_MESSAGE, BacktestQueue, BacktestQueueTimeout

NAMES = ("GLOBAL_LIMIT", "USER_LIMIT", "QUEUE_WAIT_SECONDS", "WORKERS", "GLOBAL_QUEUE_LIMIT",
         "USER_QUEUE_LIMIT", "TIMEOUT_SECONDS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(f"AI_TRADER_BACKTEST_{name}", raising=False)


def test_defaults_keep_the_old_behavior():
    settings = load_backtest_settings()
    assert (settings.global_limit, settings.user_limit, settings.queue_wait_seconds, settings.workers) == (2, 1, 60.0, 2)
    assert (settings.global_queue_limit, settings.user_queue_limit, settings.timeout_seconds) == (32, 4, 30.0)
    queue = BacktestQueue()
    assert queue.limits() == {"global": 2, "perUser": 1, "waitSeconds": 60.0, "running": 0, "queued": 0}


def test_workers_follow_the_global_limit_unless_set():
    assert load_backtest_settings({"AI_TRADER_BACKTEST_GLOBAL_LIMIT": "5"}).workers == 5
    assert load_backtest_settings({"AI_TRADER_BACKTEST_GLOBAL_LIMIT": "5", "AI_TRADER_BACKTEST_WORKERS": "3"}).workers == 3


@pytest.mark.parametrize("raw", ["", "  ", "abc", "0", "-1", "1.5", "nan"])
def test_invalid_integers_fall_back_to_the_default_never_zero(raw):
    env = {f"AI_TRADER_BACKTEST_{name}": raw for name in ("GLOBAL_LIMIT", "USER_LIMIT", "WORKERS",
                                                          "GLOBAL_QUEUE_LIMIT", "USER_QUEUE_LIMIT")}
    settings = load_backtest_settings(env)
    assert (settings.global_limit, settings.user_limit, settings.workers) == (2, 1, 2)
    assert (settings.global_queue_limit, settings.user_queue_limit) == (32, 4)


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "nan", "inf"])
def test_invalid_seconds_fall_back_to_the_default(raw):
    settings = load_backtest_settings({"AI_TRADER_BACKTEST_QUEUE_WAIT_SECONDS": raw,
                                       "AI_TRADER_BACKTEST_TIMEOUT_SECONDS": raw})
    assert (settings.queue_wait_seconds, settings.timeout_seconds) == (60.0, 30.0)


async def _enter(queue: BacktestQueue, user: str, request_id: str, started: list[str], release: asyncio.Event):
    async with queue.slot(user, request_id=request_id):
        started.append(request_id)
        await release.wait()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_configured_limits_apply_to_the_queue(monkeypatch):
    monkeypatch.setenv("AI_TRADER_BACKTEST_GLOBAL_LIMIT", "3")
    monkeypatch.setenv("AI_TRADER_BACKTEST_USER_LIMIT", "2")
    queue = BacktestQueue()
    assert queue.limits()["global"] == 3 and queue.limits()["perUser"] == 2
    started: list[str] = []
    gate = asyncio.Event()
    # One user may run two at once, the third waits; another user takes the last global slot.
    tasks = [asyncio.create_task(_enter(queue, user, name, started, gate))
             for name, user in (("a1", "a"), ("a2", "a"), ("a3", "a"), ("b1", "b"), ("c1", "c"))]
    await _settle()
    assert started == ["a1", "a2", "b1"]
    gate.set()
    await asyncio.gather(*tasks)
    assert sorted(started) == ["a1", "a2", "a3", "b1", "c1"]


@pytest.mark.asyncio
async def test_configured_wait_gives_up_with_the_same_message(monkeypatch):
    monkeypatch.setenv("AI_TRADER_BACKTEST_QUEUE_WAIT_SECONDS", "0.1")
    queue = BacktestQueue()
    assert queue.wait_seconds == 0.1
    release = asyncio.Event()
    first = asyncio.create_task(_enter(queue, "u", "first", [], release))
    await _settle()
    with pytest.raises(BacktestQueueTimeout) as error:
        async with queue.slot("u", request_id="second"):
            pytest.fail("must not start while the user's first backtest runs")
    assert str(error.value) == QUEUE_TIMEOUT_MESSAGE
    release.set()
    await first


def test_worker_pool_size_comes_from_the_settings(monkeypatch):
    sizes: list[int] = []

    class FakePool:
        def __init__(self, workers, **_kwargs):
            sizes.append(workers)

    monkeypatch.setattr(strategy_backtest, "ProcessPoolExecutor", FakePool)
    monkeypatch.setattr(strategy_backtest, "_workers", None)
    monkeypatch.setenv("AI_TRADER_BACKTEST_GLOBAL_LIMIT", "3")
    strategy_backtest._worker_pool()
    monkeypatch.setattr(strategy_backtest, "_workers", None)
    monkeypatch.setenv("AI_TRADER_BACKTEST_WORKERS", "4")
    strategy_backtest._worker_pool()
    monkeypatch.setattr(strategy_backtest, "_workers", None)
    monkeypatch.setenv("AI_TRADER_BACKTEST_WORKERS", "0")
    strategy_backtest._worker_pool()
    assert sizes == [3, 4, 3]

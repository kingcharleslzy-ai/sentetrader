"""统一采集协调器（collection.Collector）的不变量：单飞、有界等待、失败不覆盖好值、重启读回、退避后加入仍在跑的取数。"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

import pytest

from trader_runtime.collection import FETCH_TIMEOUT, Collector
from trader_runtime.last_good import LastGood


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _collector(tmp_path, clock: _Clock, wall: list[float] | None = None) -> Collector:
    connection = sqlite3.connect(tmp_path / "kept.sqlite3", check_same_thread=False)
    kept = LastGood(connection, threading.Lock(), clock=clock)
    if wall is not None:
        kept.wall = lambda: wall[0]
    return Collector(kept, lane="test", workers=2, error_prefix="source_error",
                     passthrough=frozenset({"dependency_missing"}))


def _read(collector: Collector, fetch, wait: float = 5.0) -> dict[str, Any]:
    return collector.read("k", fetch, ttl=600, retry=60, wait=wait, unavailable="k_unavailable")


def test_concurrent_readers_share_one_fetch(tmp_path):
    clock = _Clock()
    collector = _collector(tmp_path, clock)
    entered, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def fetch() -> dict[str, Any]:
        calls.append(1)
        entered.set()
        release.wait(5)
        return {"status": "ok", "value": 1}

    results: list[dict[str, Any]] = []
    readers = [threading.Thread(target=lambda: results.append(_read(collector, fetch))) for _ in range(6)]
    readers[0].start()
    assert entered.wait(5)
    for reader in readers[1:]:
        reader.start()
    release.set()
    for reader in readers:
        reader.join(5)
    assert len(calls) == 1 and [item["value"] for item in results] == [1] * 6
    assert _read(collector, fetch)["value"] == 1 and len(calls) == 1  # inside the TTL: no fetch


def test_failure_and_empty_keep_the_good_value_stale_and_retry_after_the_short_interval(tmp_path):
    clock = _Clock()
    collector = _collector(tmp_path, clock)
    assert _read(collector, lambda: {"status": "ok", "value": 1})["value"] == 1
    clock.now += 601

    def broken() -> dict[str, Any]:
        raise TimeoutError("down")

    stale = _read(collector, broken)
    assert stale["status"] == "stale" and stale["value"] == 1 and stale["error"] == "source_error:TimeoutError"
    calls: list[int] = []

    def empty() -> dict[str, Any]:
        calls.append(1)
        return {"status": "unavailable"}

    clock.now += 30
    assert _read(collector, empty)["status"] == "stale" and calls == []  # backing off: no fetch yet
    clock.now += 31
    again = _read(collector, empty)
    assert calls == [1] and again["status"] == "stale" and again["value"] == 1 and again["error"] == "k_unavailable"
    clock.now += 61
    fresh = _read(collector, lambda: {"status": "ok", "value": 2})
    assert fresh == {"status": "ok", "value": 2}


def test_cold_failure_is_unavailable_and_a_passthrough_error_is_not_scheduled(tmp_path):
    clock = _Clock()
    collector = _collector(tmp_path, clock)

    def broken() -> dict[str, Any]:
        raise ValueError("bad")

    assert _read(collector, broken) == {"status": "unavailable", "error": "source_error:ValueError"}
    calls: list[int] = []

    def missing() -> dict[str, Any]:
        calls.append(1)
        raise RuntimeError("dependency_missing")

    other = Collector(collector.kept, lane="test-2", passthrough=frozenset({"dependency_missing"}))
    for _ in range(2):  # not scheduled: every call asks again (the check is local and cheap)
        assert other.read("m", missing, ttl=600, retry=60, wait=5, unavailable="m_unavailable") == {
            "status": "unavailable", "error": "dependency_missing"}
    assert calls == [1, 1]


def test_a_partial_answer_is_kept_but_retried_after_the_short_interval(tmp_path):
    clock = _Clock()
    collector = _collector(tmp_path, clock)
    calls: list[int] = []

    def partial() -> dict[str, Any]:
        calls.append(1)
        return {"status": "ok", "partial": {"failed": ["a"]}, "value": 1}

    assert _read(collector, partial)["partial"] == {"failed": ["a"]}
    clock.now += 61
    _read(collector, partial)
    assert calls == [1, 1]


def test_a_reader_that_stops_waiting_backs_off_and_the_next_one_joins_the_running_fetch(tmp_path):
    clock = _Clock()
    collector = _collector(tmp_path, clock)
    release = threading.Event()
    calls: list[int] = []

    def hung() -> dict[str, Any]:
        calls.append(1)
        release.wait(5)
        return {"status": "ok", "value": 7}

    first = _read(collector, hung, wait=0.05)
    assert first == {"status": "unavailable", "error": FETCH_TIMEOUT}
    assert _read(collector, hung, wait=0.05)["error"] == FETCH_TIMEOUT and calls == [1]  # backing off
    clock.now += 61
    results: list[dict[str, Any]] = []
    joiner = threading.Thread(target=lambda: results.append(_read(collector, hung)))
    joiner.start()
    release.set()
    joiner.join(5)
    assert calls == [1] and results == [{"status": "ok", "value": 7}]  # joined, never a second fetch


def test_a_restart_over_the_same_store_reuses_the_value_inside_its_ttl_and_serves_it_stale_after(tmp_path):
    clock, wall = _Clock(), [5000.0]
    collector = _collector(tmp_path, clock, wall)
    assert _read(collector, lambda: {"status": "ok", "value": 1})["value"] == 1
    restarted = Collector(LastGood(collector.kept.connection, threading.Lock(), clock=clock), lane="test")
    restarted.kept.wall = lambda: wall[0]
    calls: list[int] = []

    def fetch() -> dict[str, Any]:
        calls.append(1)
        raise ConnectionError("down")

    assert _read(restarted, fetch)["value"] == 1 and calls == []  # #788: no refetch inside the stored TTL
    wall[0] += 601
    clock.now += 601
    stale = _read(restarted, fetch)
    assert calls == [1] and stale["status"] == "stale" and stale["value"] == 1


def test_every_joined_reader_answers_the_passthrough_error(tmp_path):
    collector = _collector(tmp_path, _Clock())
    entered, release = threading.Event(), threading.Event()

    def missing() -> dict[str, Any]:
        entered.set()
        release.wait(5)
        raise RuntimeError("dependency_missing")

    results: list[dict[str, Any]] = []
    readers = [threading.Thread(target=lambda: results.append(_read(collector, missing))) for _ in range(4)]
    readers[0].start()
    assert entered.wait(5)
    for reader in readers[1:]:
        reader.start()
    release.set()
    for reader in readers:
        reader.join(5)
    assert results == [{"status": "unavailable", "error": "dependency_missing"}] * 4


def test_a_fetch_that_finishes_after_its_reader_stopped_waiting_is_served_inside_the_back_off(tmp_path):
    clock = _Clock()
    collector = _collector(tmp_path, clock)
    release, done = threading.Event(), threading.Event()

    def slow() -> dict[str, Any]:
        release.wait(5)
        done.set()
        return {"status": "ok", "value": 3}

    assert _read(collector, slow, wait=0.05)["error"] == FETCH_TIMEOUT
    future = collector._inflight["k"]
    release.set()
    assert done.wait(5)
    future.result(timeout=5)
    clock.now += 1  # well inside the 60 s back-off
    assert _read(collector, slow) == {"status": "ok", "value": 3}
    assert "k" not in collector._inflight


def test_a_finished_fetch_is_recorded_without_another_read(tmp_path):
    clock = _Clock()
    collector = _collector(tmp_path, clock)
    release = threading.Event()

    def slow() -> dict[str, Any]:
        release.wait(5)
        return {"status": "ok", "value": 4}

    assert _read(collector, slow, wait=0.05)["error"] == FETCH_TIMEOUT
    future = collector._inflight["k"]
    release.set()
    future.result(timeout=5)
    for _ in range(100):  # the done-callback runs on the worker right after the result is set
        if "k" not in collector._inflight:
            break
        threading.Event().wait(0.01)
    assert "k" not in collector._inflight and collector.kept.value("k") == {"status": "ok", "value": 4}


def test_a_passthrough_that_finishes_after_a_timeout_clears_the_back_off(tmp_path):
    clock = _Clock()
    collector = _collector(tmp_path, clock)
    release = threading.Event()
    calls: list[int] = []

    def missing() -> dict[str, Any]:
        calls.append(1)
        release.wait(5)
        raise RuntimeError("dependency_missing")

    assert _read(collector, missing, wait=0.05)["error"] == FETCH_TIMEOUT
    future = collector._inflight["k"]
    release.set()
    try:
        future.result(timeout=5)
    except RuntimeError:
        pass
    clock.now += 1  # inside the timeout's back-off: due again at once, answering the stable code
    assert _read(collector, missing) == {"status": "unavailable", "error": "dependency_missing"}
    assert calls == [1, 1]


def test_read_many_starts_every_due_fetch_before_waiting_on_any(tmp_path):
    from trader_runtime.collection import Request

    collector = _collector(tmp_path, _Clock())
    started: list[str] = []
    both = threading.Barrier(2, timeout=5)

    def fetch(name: str):
        def run() -> dict[str, Any]:
            started.append(name)
            both.wait()  # fails (BrokenBarrierError) unless the two fetches run at the same time
            return {"status": "ok", "value": name}
        return run

    answers = collector.read_many([Request("a", fetch("a"), "na"), Request("b", fetch("b"), "nb")],
                                  ttl=600, retry=60, wait=5)
    assert sorted(started) == ["a", "b"] and answers == {"a": {"status": "ok", "value": "a"}, "b": {"status": "ok", "value": "b"}}


def test_valid_for_decides_freshness_from_the_stored_value_and_a_failure_still_backs_off(tmp_path):
    clock, wall = _Clock(), [5000.0]
    collector = _collector(tmp_path, clock, wall)
    calls: list[int] = []
    day = ["d1"]

    def fetch() -> dict[str, Any]:
        calls.append(1)
        return {"status": "ok", "day": day[0]}

    def valid_for(kept) -> float:
        return 0.0 if kept.value.get("day") != day[0] else 3600.0

    read = lambda fn=fetch: collector.read("k", fn, ttl=600, retry=60, wait=5, unavailable="na", valid_for=valid_for)  # noqa: E731
    assert read()["day"] == "d1" and calls == [1]
    wall[0] += 1800
    assert read()["day"] == "d1" and calls == [1]  # inside its validity
    day[0] = "d2"  # the trading day turned: the stored value is no longer fresh, whatever its age
    assert read()["day"] == "d2" and calls == [1, 1]

    def broken() -> dict[str, Any]:
        calls.append(1)
        raise ConnectionError("down")

    wall[0] += 7200
    assert read(broken)["status"] == "stale" and len(calls) == 3
    assert read(broken)["status"] == "stale" and len(calls) == 3  # backing off: no fetch inside the retry
    clock.now += 61
    read(broken)
    assert len(calls) == 4


def test_a_partial_value_read_back_after_a_restart_is_due_after_the_short_interval(tmp_path):
    """#932: a partial value is kept for the retry interval only; a restart must not schedule it for the full TTL."""
    clock, wall = _Clock(), [5000.0]
    collector = _collector(tmp_path, clock, wall)
    _read(collector, lambda: {"status": "ok", "partial": {"failed": ["a"]}, "value": 1})
    restarted = Collector(LastGood(collector.kept.connection, threading.Lock(), clock=clock), lane="test")
    restarted.kept.wall = lambda: wall[0]
    calls: list[int] = []

    def whole() -> dict[str, Any]:
        calls.append(1)
        return {"status": "ok", "value": 2}

    assert _read(restarted, whole)["value"] == 1 and calls == []  # inside the stored retry interval
    wall[0] += 61
    clock.now += 61
    assert _read(restarted, whole)["value"] == 2 and calls == [1]  # not 600 s: the partial value's retry


def test_a_reader_that_joins_an_earlier_epochs_fetch_also_gets_the_current_epochs(tmp_path):
    """#932 (Codex r4307878775): a fetch started before the trading day turned and still running when the first
    new-day reader arrives is joined; once it settles the new day's fetch starts within the same wait."""
    clock, wall = _Clock(), [5000.0]
    collector = _collector(tmp_path, clock, wall)
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def fetch_for(day: str):
        def fetch() -> dict[str, Any]:
            calls.append(day)
            if day == "d1":
                entered.set()
                release.wait(5)
            return {"status": "ok", "day": day}
        return fetch

    def valid_for(kept) -> float:
        return 3600.0 if kept.value.get("day") == current[0] else 0.0

    current = ["d1"]
    first = collector.read("k", fetch_for("d1"), ttl=600, retry=60, wait=0.05, unavailable="na", valid_for=valid_for,
                           epoch="d1")
    assert first["error"] == FETCH_TIMEOUT and entered.wait(5)
    current[0] = "d2"  # the trading day turns while d1's fetch still runs
    threading.Timer(0.1, release.set).start()
    answer = collector.read("k", fetch_for("d2"), ttl=600, retry=60, wait=5, unavailable="na", valid_for=valid_for,
                            epoch="d2")
    assert answer == {"status": "ok", "day": "d2"} and calls == ["d1", "d2"]


def test_after_close_no_fetch_starts_and_a_running_one_records_nothing(tmp_path):
    """#932: a fetch still running at shutdown must not write to the store when it ends."""
    clock = _Clock()
    collector = _collector(tmp_path, clock)
    entered, release = threading.Event(), threading.Event()

    def slow() -> dict[str, Any]:
        entered.set()
        release.wait(5)
        return {"status": "ok", "value": 5}

    assert _read(collector, slow, wait=0.05)["error"] == FETCH_TIMEOUT
    assert entered.wait(5)
    running = collector._inflight["k"]
    collector.close()
    writes: list[str] = []
    collector.kept.succeeded = lambda key, *args: writes.append(key)  # type: ignore[method-assign]
    collector.kept.failed = lambda key, *args: writes.append(key)  # type: ignore[method-assign]
    release.set()
    assert running.result(5)["value"] == 5
    time.sleep(0.1)  # its done-callback runs on the worker right after the result is set
    clock.now += 61
    calls: list[int] = []
    after = collector.read("k2", lambda: calls.append(1) or {"status": "ok"}, ttl=600, retry=60, wait=1,
                           unavailable="k2_unavailable")
    assert after == {"status": "unavailable", "error": "k2_unavailable"} and calls == [] and writes == []
    assert collector.kept.get("k") is None


def test_async_close_stops_new_fetches(tmp_path):
    import asyncio

    from trader_runtime.collection import SourceFailed

    async def scenario():
        collector = _async_collector(tmp_path, _Clock())
        await collector.close()
        calls: list[int] = []
        with pytest.raises(SourceFailed) as closed:
            await _get(collector, _counted(calls))
        assert closed.value.code == "k_unavailable" and calls == [] and collector.pending() == []

    asyncio.run(scenario())


def _async_collector(tmp_path, clock: _Clock):
    from trader_runtime.collection import AsyncCollector

    connection = sqlite3.connect(tmp_path / "async.sqlite3", check_same_thread=False)
    return AsyncCollector(LastGood(connection, threading.Lock(), clock=clock), lane="test-async")


def _get(collector, fetch, **over):
    options = {"ttl": 600, "retry": 60, "timeout": 5.0, "timeout_error": "k_timeout", "unavailable": "k_unavailable", **over}
    return collector.get("k", fetch, **options)


def test_async_readers_share_one_fetch_and_a_cancelled_reader_leaves_it_running(tmp_path):
    import asyncio

    async def scenario():
        collector = _async_collector(tmp_path, _Clock())
        release = asyncio.Event()
        calls: list[int] = []

        async def fetch():
            calls.append(1)
            await release.wait()
            return {"v": 1}

        first = asyncio.create_task(_get(collector, fetch))
        second = asyncio.create_task(_get(collector, fetch))
        await asyncio.sleep(0)
        first.cancel()  # one reader gives up: the fetch the other waits on goes on
        await asyncio.sleep(0)
        release.set()
        kept = await second
        assert kept.value == {"v": 1} and calls == [1] and first.cancelled()
        assert (await _get(collector, fetch)).value == {"v": 1} and calls == [1]  # inside the TTL
        await collector.close()

    asyncio.run(scenario())


def test_async_failures_keep_the_good_value_stale_with_their_own_retry(tmp_path):
    import asyncio

    from trader_runtime.collection import SourceFailed

    async def scenario():
        clock = _Clock()
        collector = _async_collector(tmp_path, clock)
        with pytest.raises(SourceFailed) as cold:
            await _get(collector, _raising(SourceFailed("k_empty", 300)))
        assert cold.value.code == "k_empty"
        clock.now += 301  # the empty answer's own (longer) retry
        assert (await _get(collector, _value({"v": 2}))).value == {"v": 2}
        clock.now += 601

        async def hung():
            await asyncio.sleep(10)

        stale = await _get(collector, hung, timeout=0.01)
        assert stale.stale and stale.value == {"v": 2} and stale.error == "k_timeout"
        calls: list[int] = []
        assert (await _get(collector, _counted(calls))).stale and calls == []  # backing off for the read's retry
        await collector.close()

    asyncio.run(scenario())


def _raising(error):
    async def fetch():
        raise error
    return fetch


def _value(value):
    async def fetch():
        return value
    return fetch


def _counted(calls):
    async def fetch():
        calls.append(1)
        return {"v": 3}
    return fetch


def test_get_many_batches_due_keys_joins_running_ones_and_records_failures_per_key(tmp_path):
    """#976 第 7 步：一个请求能带多个键的源（新浪报价列表）——到期的键合成一次请求，别人正在取的键加入那次请求、
    不再请求第二次；源没给的键按"没有"存（None）；失败记在这次请求的每个键上，好值保留、读成 stale，重试间隔内不再请求。"""
    import asyncio

    async def scenario():
        clock = _Clock()
        collector = _async_collector(tmp_path, clock)
        release = asyncio.Event()
        asked: list[list[str]] = []

        async def fetch(keys):
            asked.append(sorted(keys))
            await release.wait()
            return {key: {"v": key} for key in keys if key != "none"}

        options = {"ttl": 2, "retry": 5, "timeout": None, "timeout_error": "k_timeout"}
        first = asyncio.create_task(collector.get_many(["a", "b", "none"], fetch, **options))
        await asyncio.sleep(0)
        second = asyncio.create_task(collector.get_many(["b", "c"], fetch, **options))  # b is already being fetched
        await asyncio.sleep(0)
        release.set()
        got, joined = await first, await second
        assert asked == [["a", "b", "none"], ["c"]]
        assert got["a"].value == {"v": "a"} and got["none"] is not None and got["none"].value is None
        assert joined["b"].value == {"v": "b"} and joined["c"].value == {"v": "c"}
        assert (await collector.get_many(["a", "c"], fetch, **options))["a"].value == {"v": "a"} and len(asked) == 2

        clock.now += 3  # past the TTL: the source fails for the whole request

        async def broken(keys):
            asked.append(sorted(keys))
            raise ValueError("garbled")

        failed = await collector.get_many(["a", "c", "new"], broken, **options)
        assert failed["a"].stale and failed["a"].value == {"v": "a"} and failed["a"].error == "test-async:ValueError"
        assert failed["new"] is None and asked[-1] == ["a", "c", "new"]
        again = await collector.get_many(["a", "new"], broken, **options)  # inside the retry interval: no request
        assert again["a"].stale and again["new"] is None and len(asked) == 3
        await collector.close()

    asyncio.run(scenario())

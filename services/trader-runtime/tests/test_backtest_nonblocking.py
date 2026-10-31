"""规则回测不影响其他请求 + 并发上限 (2026-09-25).

Production: while a one-minute Dual Thrust backtest ran, ``GET /api/runtime/status`` stalled 1.4 s and
2.2 s. The replay ran in a thread, and a CPU-bound thread holds the Runtime's GIL: the event loop waited
for it at every I/O step. The replay now runs in a worker process (``run_in_worker``), and
``BacktestQueue`` lets 2 run at once, 1 per user, first come first served, 60 s in the queue at most.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time

import pytest
from httpx import ASGITransport, AsyncClient

from test_strategy_backtest import DOUBLE_MA, DOUBLE_MA_CLOSES, gapless, run, series
from trader_runtime import strategy_workbench
from trader_runtime.data_providers import fake_market_history
from trader_runtime.main import create_app
from trader_runtime.strategy_backtest import (
    QUEUE_TIMEOUT_MESSAGE,
    BacktestQueue,
    BacktestQueueTimeout,
    BacktestSetup,
    run_backtest,
    run_in_worker,
)
from trader_runtime.strategy_templates import build_plan, templates

HEADERS = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer bt-token"}
FEES = {"source": "custom", "default": {"mode": "per_lot", "open": 3, "closeToday": 3, "closeYesterday": 3}, "overrides": []}
FAKE_SCHEDULE = {"day": [["00:00:00", "23:59:00"]], "night": []}


def test_the_worker_gives_the_result_a_direct_run_gives():
    bars = series(gapless(DOUBLE_MA_CLOSES))
    setup = BacktestSetup(plan=DOUBLE_MA, symbol="SHFE.rb2610", series={300: bars}, price_tick=1,
                          volume_multiple=10, fee_schedule=FEES, initial_balance=100_000)
    assert run_in_worker(setup) == run_backtest(setup) == run(DOUBLE_MA, bars, fees=FEES)
    plan, _ = build_plan(templates()["dual_thrust"], {})
    minute = {60: fake_market_history("SHFE.au2612", 60, 3_000)["bars"],
              86400: fake_market_history("SHFE.au2612", 86400, 300)["bars"]}
    heavy = BacktestSetup(plan=plan, symbol="SHFE.au2612", series=minute, price_tick=0.02, volume_multiple=1000,
                          fee_schedule=FEES, schedule=FAKE_SCHEDULE)
    assert run_in_worker(heavy) == run_backtest(heavy)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process check")
def test_a_worker_ends_when_the_runtime_dies_without_shutting_it_down(tmp_path):
    """The desktop watchdog ends the Runtime with os._exit: its backtest worker must not live on."""
    script = tmp_path / "runtime.py"
    script.write_text("import os\nfrom trader_runtime import strategy_backtest\n"
                      "if __name__ == '__main__':\n"
                      "    print(strategy_backtest._worker_pool().submit(os.getpid).result(), flush=True)\n"
                      "    os._exit(0)\n")
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60, check=True)
    worker = int(done.stdout.strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(worker, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(worker, signal.SIGKILL)
    pytest.fail("the backtest worker outlived its Runtime")


@pytest.mark.asyncio
async def test_the_event_loop_keeps_answering_while_a_backtest_runs(tmp_path, monkeypatch):
    """Probe: a task asking to wake every 20 ms records how late it wakes, while other requests keep
    coming. Before the worker process the loop fell behind by 100–400 ms on a quiet laptop (seconds
    in production); now it stays within a few ms. The bound is loose for slow CI machines."""
    from trader_runtime.data_providers import FakeReadOnlyDataProvider

    original = FakeReadOnlyDataProvider.market_history

    def history(self, symbol, duration_seconds, length):
        return {**original(self, symbol, duration_seconds, length), "tradingSchedule": FAKE_SCHEDULE}

    monkeypatch.setattr(FakeReadOnlyDataProvider, "market_history", history)
    app = create_app(tmp_path / "loop.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="bt-token")
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            body = {"symbol": "SHFE.au2612", "timeframe": 60, "params": {}}
            # Start the worker processes with a tiny backtest: the pool spawn is what the warm-up
            # exists for, and a full dual-thrust preview here would double the test's real compute.
            warm_setup = BacktestSetup(plan=DOUBLE_MA, symbol="SHFE.rb2610",
                                       series={300: series(gapless(DOUBLE_MA_CLOSES))},
                                       price_tick=1, volume_multiple=10, fee_schedule=FEES,
                                       initial_balance=100_000)
            await asyncio.to_thread(run_in_worker, warm_setup)
            lags: list[float] = []
            done = asyncio.Event()

            async def probe() -> None:
                while not done.is_set():
                    started = time.perf_counter()
                    await asyncio.sleep(0.02)
                    lags.append(time.perf_counter() - started - 0.02)

            async def poll() -> None:
                while not done.is_set():
                    assert (await client.get("/api/runtime/status")).status_code == 200
                    await asyncio.sleep(0.01)

            tasks = [asyncio.create_task(probe()), *(asyncio.create_task(poll()) for _ in range(4))]
            started = time.perf_counter()
            response = await client.post("/api/strategy-templates/dual_thrust/preview", json=body)
            elapsed = time.perf_counter() - started
            done.set()
            await asyncio.gather(*tasks)
    assert response.status_code == 200 and response.json()["backtest"]["coverage"]["bars"] > 9_000
    print(f"backtest {elapsed:.2f}s, loop max lag {max(lags) * 1000:.0f} ms over {len(lags)} wakes")
    # A backtest run on the loop would stall it for about the whole run; scheduling noise on a
    # shared CI runner stays well under half of it (a fixed 0.25 s bound flaked at 0.253 s).
    assert elapsed > 0.2 and max(lags) < elapsed / 2


# -- the queue -------------------------------------------------------------------------------------------------

async def _enter(queue: BacktestQueue, user: str, request_id: str, started: list[str], release: asyncio.Event):
    async with queue.slot(user, request_id=request_id):
        started.append(request_id)
        await release.wait()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_two_run_at_once_one_per_user_the_rest_wait_in_arrival_order():
    queue = BacktestQueue()
    started: list[str] = []
    gates = {name: asyncio.Event() for name in ("a", "b", "c", "a2", "d")}
    tasks = {name: asyncio.create_task(_enter(queue, name[0], name, started, gates[name])) for name in ("a", "b", "c")}
    await _settle()
    # Three users at once: the first two start, the third waits behind both.
    assert started == ["a", "b"]
    assert queue.items("c") == [{"requestId": "c", "state": "queued", "ahead": 2, "queuedAt": queue.items("c")[0]["queuedAt"]}]
    assert [item["state"] for item in queue.items("a")] == ["running"]
    # The same user again waits (one per user) and does not hold up a later user when a slot frees.
    tasks["a2"] = asyncio.create_task(_enter(queue, "a", "a2", started, gates["a2"]))
    tasks["d"] = asyncio.create_task(_enter(queue, "d", "d", started, gates["d"]))
    await _settle()
    assert [(item["requestId"], item["ahead"]) for item in queue.items("a") if item["state"] == "queued"] == [("a2", 3)]
    gates["b"].set()                  # b finishes: c (first in line) starts; a2 still waits for a
    await _settle()
    assert started == ["a", "b", "c"]
    gates["c"].set()                  # c finishes: a2 is blocked by a, so d (behind it) starts
    await _settle()
    assert started == ["a", "b", "c", "d"]
    gates["a"].set()
    await _settle()
    assert started == ["a", "b", "c", "d", "a2"]
    gates["a2"].set()
    gates["d"].set()
    await asyncio.gather(*tasks.values())
    assert queue.limits() == {"global": 2, "perUser": 1, "waitSeconds": 60.0, "running": 0, "queued": 0}


@pytest.mark.asyncio
async def test_a_backtest_waiting_too_long_gives_up_and_leaves_the_queue():
    queue = BacktestQueue(wait_seconds=0.1)
    release = asyncio.Event()
    started: list[str] = []
    first = asyncio.create_task(_enter(queue, "u", "first", started, release))
    await _settle()
    with pytest.raises(BacktestQueueTimeout) as error:
        async with queue.slot("u", request_id="second"):
            pytest.fail("must not start while the user's first backtest runs")
    assert str(error.value) == QUEUE_TIMEOUT_MESSAGE == "回测排队中，稍后再试"
    assert [item["requestId"] for item in queue.items("u")] == ["first"]
    release.set()
    await first
    # A cancelled waiter (the caller went away) leaves the queue too.
    release.clear()
    first = asyncio.create_task(_enter(queue, "u", "first", started, release))
    await _settle()
    waiter = asyncio.create_task(_enter(queue, "u", "gone", started, asyncio.Event()))
    await _settle()
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert [item["requestId"] for item in queue.items("u")] == ["first"]
    release.set()
    await first


@pytest.mark.asyncio
async def test_routes_queue_per_user_show_the_position_and_time_out_in_chinese(tmp_path, monkeypatch):
    """Through the HTTP routes (what the detail page, the template preview and the AI tool call):
    three users, a user's second backtest, the queue endpoint, the 429 after the wait, and a status
    request answered while backtests queue."""
    app = create_app(tmp_path / "queue.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="bt-token")
    release = threading.Event()
    real_execute = strategy_workbench.execute

    def held_execute(*args, **kwargs):
        release.wait(10)
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(strategy_workbench, "execute", held_execute)
    auth_user = app.state.auth.user
    monkeypatch.setattr(app.state.auth, "user", lambda request: request.headers.get("X-Test-User") or auth_user(request))
    raw = DOUBLE_MA.model_dump()
    async with app.router.lifespan_context(app):
        queue = app.state.strategy_backtest_queue
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS, timeout=30) as client:
            async def backtest(user: str, request_id: str):
                return await client.post("/api/strategy-backtests", headers={"X-Test-User": user},
                                         json={"plan": raw, "symbol": "SHFE.au2612", "requestId": request_id})

            async def until(check) -> None:
                for _ in range(1000):  # a bound on a hang, not a timing claim: it returns as soon as `check()` holds
                    if check():
                        return
                    await asyncio.sleep(0.005)
                raise AssertionError(queue.limits())

            # Arrival order decides who is queued, so release the three requests one at a time: the
            # third (carol) is the one that waits, whatever the scheduler does with concurrent tasks.
            runs = [asyncio.create_task(backtest("alice", "r-alice"))]
            await until(lambda: queue.limits()["running"] == 1)
            runs.append(asyncio.create_task(backtest("bob", "r-bob")))
            await until(lambda: queue.limits()["running"] == 2)
            runs.append(asyncio.create_task(backtest("carol", "r-carol")))
            await until(lambda: queue.limits()["running"] == 2 and queue.limits()["queued"] == 1)
            carol = (await client.get("/api/strategy-backtests/queue", params={"requestId": "r-carol"},
                                      headers={"X-Test-User": "carol"})).json()
            assert [(item["state"], item["ahead"]) for item in carol["items"]] == [("queued", 2)]
            assert carol["limits"]["global"] == 2 and carol["limits"]["perUser"] == 1
            # Nobody sees another user's backtests.
            assert (await client.get("/api/strategy-backtests/queue", headers={"X-Test-User": "dave"})).json()["items"] == []
            again = asyncio.create_task(backtest("alice", "r-alice-2"))
            await until(lambda: queue.limits()["queued"] == 2)
            alice = (await client.get("/api/strategy-backtests/queue", headers={"X-Test-User": "alice"})).json()["items"]
            assert sorted((item["requestId"], item["state"]) for item in alice) == [("r-alice", "running"), ("r-alice-2", "queued")]
            # Other requests are answered at once while backtests run and queue.
            assert (await client.get("/api/runtime/status")).status_code == 200
            assert not any(task.done() for task in [*runs, again])  # answered while every backtest is still held
            release.set()
            responses = await asyncio.gather(*runs, again)
            assert [response.status_code for response in responses] == [200, 200, 200, 200]
            assert responses[2].json()["timings"]["queueMs"] > 0 and "runMs" in responses[2].json()["timings"]
            assert queue.limits()["running"] == queue.limits()["queued"] == 0
            # Waiting longer than the queue allows: 429 with the reason in Chinese.
            release.clear()
            queue.wait_seconds = 0.2
            first = asyncio.create_task(backtest("alice", "r-long"))
            await until(lambda: queue.limits()["running"] == 1)
            refused = await backtest("alice", "r-late")
            assert refused.status_code == 429 and refused.json()["detail"] == "回测排队中，稍后再试"
            release.set()
            assert (await first).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_cancelled_running_route_keeps_slot_until_thread_finishes(tmp_path, monkeypatch, failed):
    app = create_app(tmp_path / "cancel.sqlite3", runtime_state_root=tmp_path,
                     agent_mode="fake", local_token="bt-token")
    entered = threading.Event()
    release = threading.Event()
    calls = [0]
    real_execute = strategy_workbench.execute

    def held_execute(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            entered.set()
            assert release.wait(5)
            if failed:
                raise RuntimeError("worker failed")
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(strategy_workbench, "execute", held_execute)
    auth_user = app.state.auth.user
    monkeypatch.setattr(app.state.auth, "user", lambda request: request.headers.get("X-Test-User") or auth_user(request))
    raw = DOUBLE_MA.model_dump()
    async with app.router.lifespan_context(app):
        queue = app.state.strategy_backtest_queue
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            async def backtest(user, request_id):
                return await client.post("/api/strategy-backtests", headers={"X-Test-User": user},
                    json={"plan": raw, "symbol": "SHFE.au2612", "requestId": request_id})

            async def until(check):
                async with asyncio.timeout(3):
                    while not check():
                        await asyncio.sleep(0.01)

            first = asyncio.create_task(backtest("alice", "first"))
            try:
                await until(lambda: entered.is_set())
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
                assert queue.limits()["running"] == 1
                second = asyncio.create_task(backtest("alice", "second"))
                await until(lambda: queue.limits()["queued"] == 1)
                queued = asyncio.create_task(backtest("alice", "queued-cancel"))
                await until(lambda: queue.limits()["queued"] == 2)
                queued.cancel()
                await asyncio.gather(queued, return_exceptions=True)
                assert queue.limits()["queued"] == 1
                other = await backtest("bob", "other")
                assert other.status_code == 200
                assert calls[0] == 2 and queue.limits()["running"] == 1
                release.set()
                assert (await second).status_code == 200
                await queue.shutdown()
                assert queue.limits()["running"] == queue.limits()["queued"] == 0
            finally:
                release.set()


@pytest.mark.asyncio
async def test_queue_shutdown_drains_cancelled_request_work():
    queue = BacktestQueue()
    release = asyncio.Event()

    async def request():
        async with queue.slot("alice") as ticket:
            ticket.work = asyncio.create_task(release.wait())
            await asyncio.shield(ticket.work)

    first = asyncio.create_task(request())
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert queue.limits()["running"] == 1
    closing = asyncio.create_task(queue.shutdown())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    assert queue.limits()["running"] == queue.limits()["queued"] == 0

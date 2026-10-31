"""The instance lease: one Runtime per state database runs the ordering loops.

A second process on the same SQLite state database still serves its HTTP
interfaces, but the ledger, the stop monitor and the strategy monitor — the
loops that can place orders and write books — only run on the lease holder.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from test_replay import IDENTIFIER, HistoricalSource
from trader_runtime import instance_lease as lease_module
from trader_runtime.instance_lease import InstanceLease
from trader_runtime.main import create_app
from trader_runtime.news_screen import research_day
from trader_runtime.replay import ReplayProvider
from trader_runtime.store import RuntimeStore, utc_now

TOKEN = "lease-test-token"


def make_app(database, instance_id):
    return create_app(
        database, local_token=TOKEN, agent_mode="fake", data_mode="fake",
        engineering_root=database.parent / "project", runtime_instance_id=instance_id,
        simulation_gateway=SimpleNamespace(),
    )


def health_client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime")


def loops_running(app) -> bool:
    tasks = (
        app.state.account_ledger._task,
        app.state.autonomy._monitor_task,
        app.state.strategy_monitor._task,
    )
    return all(task is not None and not task.done() for task in tasks)


async def test_second_instance_stands_by_while_the_leader_runs_the_loops(tmp_path):
    database = tmp_path / "lease.sqlite3"
    first = make_app(database, "instance-first")
    second = make_app(database, "instance-second")
    async with first.router.lifespan_context(first):
        assert first.state.instance_lease.role == "leader"
        assert loops_running(first)
        async with health_client(first) as client:
            health = (await client.get("/healthz")).json()
        assert health["status"] == "ok"
        assert health["runtimeInstanceId"] == "instance-first"
        assert health["instance"]["role"] == "leader"
        assert health["instance"]["instanceId"] == "instance-first"

        async with second.router.lifespan_context(second):
            assert second.state.instance_lease.role == "standby"
            assert second.state.account_ledger._task is None
            assert second.state.autonomy._monitor_task is None
            assert second.state.strategy_monitor._task is None
            # The push feed only reads; a standby keeps serving it.
            feed_task = second.state.runtime_feed._task
            assert feed_task is not None and not feed_task.done()
            async with health_client(second) as client:
                standby_health = (await client.get("/healthz")).json()
            assert standby_health["status"] == "ok"
            assert standby_health["instance"]["role"] == "standby"
            assert standby_health["instance"]["leaderId"] == "instance-first"
            assert standby_health["runtimeInstanceId"] == "instance-second"
    # The leader released the lease on shutdown.
    store = RuntimeStore(database)
    try:
        assert store.read_lease() is None
    finally:
        store.close()


async def test_standby_takes_over_when_the_leader_shuts_down(tmp_path):
    database = tmp_path / "handover.sqlite3"
    first = make_app(database, "instance-first")
    second = make_app(database, "instance-second")
    second.state.instance_lease.heartbeat_seconds = 0.05
    first_lifespan = first.router.lifespan_context(first)
    await first_lifespan.__aenter__()
    try:
        second_lifespan = second.router.lifespan_context(second)
        await second_lifespan.__aenter__()
    except BaseException:
        await first_lifespan.__aexit__(None, None, None)
        raise
    try:
        assert second.state.instance_lease.role == "standby"
        # A clean leader shutdown releases the lease instead of forcing the
        # standby through the stale window.
        await first_lifespan.__aexit__(None, None, None)
        async with asyncio.timeout(1):
            while second.state.instance_lease.role != "leader":
                await asyncio.sleep(0.01)
        assert loops_running(second)
    finally:
        await second_lifespan.__aexit__(None, None, None)


async def test_stale_lease_is_taken_over_at_startup(tmp_path):
    database = tmp_path / "stale.sqlite3"
    stale = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    store = RuntimeStore(database)
    try:
        with store.lock, store.connection:
            store.connection.execute(
                "INSERT INTO runtime_lease"
                " (id, instance_id, host, pid, build_sha, acquired_at, heartbeat_at)"
                " VALUES (1, 'ghost-instance', 'gone-host', 1, NULL, ?, ?)",
                (stale, stale),
            )
    finally:
        store.close()
    app = make_app(database, "instance-fresh")
    async with app.router.lifespan_context(app):
        assert app.state.instance_lease.role == "leader"
        assert loops_running(app)
        takeover = RuntimeStore(database)
        try:
            assert takeover.read_lease()["instance_id"] == "instance-fresh"
        finally:
            takeover.close()


async def test_leader_stops_loops_when_the_lease_is_taken_away(tmp_path):
    database = tmp_path / "lost.sqlite3"
    app = make_app(database, "instance-leader")
    app.state.instance_lease.heartbeat_seconds = 0.05

    def outside(sql: str) -> None:
        connection = sqlite3.connect(database)
        try:
            connection.execute(sql)
            connection.commit()
        finally:
            connection.close()

    async with app.router.lifespan_context(app):
        assert loops_running(app)
        # Another process seizes the row: the next heartbeat fails and the
        # ordering loops stop before another order can leave.
        outside("UPDATE runtime_lease SET instance_id = 'instance-usurper' WHERE id = 1")
        # The role flips before the loops finish stopping, so wait for the stop.
        lease = app.state.instance_lease
        async with asyncio.timeout(1):
            while lease.role != "standby" or lease._loops_started:
                await asyncio.sleep(0.01)
        assert app.state.account_ledger._task is None
        assert app.state.autonomy._monitor_task is None
        assert app.state.strategy_monitor._task is None
        # The lease gone entirely, the same process leads again and the loops return.
        outside("DELETE FROM runtime_lease WHERE id = 1")
        async with asyncio.timeout(1):
            while lease.role != "leader" or not lease._start_complete:
                await asyncio.sleep(0.01)
        assert loops_running(app)


async def test_a_locked_database_does_not_kill_the_heartbeat(tmp_path, caplog):
    database = tmp_path / "locked.sqlite3"
    app = make_app(database, "instance-leader")
    lease = app.state.instance_lease
    lease.heartbeat_seconds = 0.05
    real_heartbeat = app.state.store.heartbeat_lease
    failures_left = [1]

    def sometimes_locked(instance_id, now):
        if failures_left[0]:
            failures_left[0] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real_heartbeat(instance_id, now)

    app.state.store.heartbeat_lease = sometimes_locked
    with caplog.at_level("ERROR", logger="uvicorn.error.agent"):
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.5)
            # A single locked write is not a lost lease: the leader stays the
            # leader, the heartbeat task survives and the loops keep running.
            assert lease.role == "leader"
            assert loops_running(app)
            assert lease._task is not None and not lease._task.done()
    assert sum("instance_lease_tick_failed" in record.getMessage() for record in caplog.records) == 1


async def test_a_failed_loop_start_is_retried_by_the_heartbeat(tmp_path):
    database = tmp_path / "start-failed.sqlite3"
    app = make_app(database, "instance-retry")
    lease = app.state.instance_lease
    lease.heartbeat_seconds = 0.05
    real_on_acquired = lease.on_acquired
    calls = [0]

    async def flaky_on_acquired():
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("loop start failed")
        await real_on_acquired()

    lease.on_acquired = flaky_on_acquired
    async with app.router.lifespan_context(app):
        # The lease was acquired despite the failed start; the next ticks
        # retry the idempotent starts until the loops are up.
        async with asyncio.timeout(1):
            while not loops_running(app):
                await asyncio.sleep(0.01)
        assert lease.role == "leader"
    assert calls[0] >= 2


async def test_partial_start_and_failed_cleanup_keep_the_lease_until_retry(tmp_path):
    database = tmp_path / "partial.sqlite3"
    first_store, second_store = RuntimeStore(database), RuntimeStore(database)
    running = [False]
    starts, stops = [0], [0]

    async def start_loops():
        starts[0] += 1
        running[0] = True
        if starts[0] == 1:
            raise RuntimeError("later component did not start")

    async def stop_loops():
        stops[0] += 1
        if stops[0] == 1:
            raise RuntimeError("cleanup failed")
        running[0] = False

    lease = InstanceLease(store=first_store, instance_id="first", build_sha=None,
                          heartbeat_seconds=10, on_acquired=start_loops, on_lost=stop_loops)
    other = InstanceLease(store=second_store, instance_id="second", build_sha=None,
                          on_acquired=lambda: asyncio.sleep(0), on_lost=lambda: asyncio.sleep(0))
    try:
        await lease.start()
        assert running[0] and lease._loops_started and not lease._start_complete
        assert first_store.read_lease()["instance_id"] == "first"
        assert not other._try_acquire()
        await lease._tick()
        assert starts[0] == 2 and stops[0] == 2 and running[0]
        await lease.stop()
        assert not running[0] and stops[0] == 3
        assert other._try_acquire()
    finally:
        if lease._task is not None:
            await lease.stop()
        first_store.close()
        second_store.close()


async def test_failed_stop_keeps_heartbeat_and_prevents_takeover_until_retry(tmp_path):
    database = tmp_path / "failed-stop.sqlite3"
    first_store, second_store = RuntimeStore(database), RuntimeStore(database)
    running = [False]
    fail_stop = [True]

    async def start_loops():
        running[0] = True

    async def stop_loops():
        if fail_stop[0]:
            raise RuntimeError("stop failed")
        running[0] = False

    lease = InstanceLease(store=first_store, instance_id="first", build_sha=None,
                          heartbeat_seconds=0.05, lease_seconds=0.2,
                          on_acquired=start_loops, on_lost=stop_loops)
    other = InstanceLease(store=second_store, instance_id="second", build_sha=None,
                          lease_seconds=0.2, on_acquired=lambda: asyncio.sleep(0),
                          on_lost=lambda: asyncio.sleep(0))
    try:
        await lease.start()
        with pytest.raises(RuntimeError, match="stop failed"):
            await lease.stop()
        await asyncio.sleep(0.25)  # Beyond expiry: the failed shutdown still renews its lease.
        assert running[0] and lease._task is not None and not lease._task.done()
        assert not other._try_acquire()
        fail_stop[0] = False
        await lease.stop()
        assert not running[0] and other._try_acquire()
    finally:
        fail_stop[0] = False
        if lease._task is not None:
            await lease.stop()
        first_store.close()
        second_store.close()


class LeaseClock:
    """The wall clock of the lease's staleness verdict, moving only when the test moves it: a
    scheduler stall on a loaded runner can no longer age a heartbeat past the lease."""

    def __init__(self):
        # Non-zero microseconds: isoformat() then keeps one width, so stamps compare as text.
        self.value = datetime(2026, 10, 15, 12, 0, 0, 500_000, tzinfo=UTC)

    def now(self, tz=None):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)

    def stamp(self):
        return self.value.isoformat()


async def test_slow_start_and_shutdown_keep_renewing_until_loops_are_stopped(tmp_path, monkeypatch):
    """While a start or a stop callback is stuck, the ordinary tick loop is blocked on the transition
    lock, so only the renew task can keep the row live. Time is virtual: the test jumps the clock many
    lease periods ahead and waits (by event, not by sleeping) for the heartbeat to reach the new time;
    had renewal stopped, the row would be stale and the other instance could take over."""
    clock = LeaseClock()
    monkeypatch.setattr(lease_module, "utc_now", clock.stamp)
    monkeypatch.setattr(lease_module, "datetime", clock)

    async def renewed_to_now():
        async with asyncio.timeout(5):   # a hang guard, not a timing claim
            while (first_store.read_lease() or {}).get("heartbeat_at") != clock.stamp():
                await asyncio.sleep(0.005)

    database = tmp_path / "slow-transition.sqlite3"
    first_store, second_store = RuntimeStore(database), RuntimeStore(database)
    started, finish_start = asyncio.Event(), asyncio.Event()
    stopping, finish_stop = asyncio.Event(), asyncio.Event()
    running = [False]

    async def start_loops():
        running[0] = True
        started.set()
        await finish_start.wait()

    async def stop_loops():
        stopping.set()
        await finish_stop.wait()
        running[0] = False

    lease = InstanceLease(store=first_store, instance_id="first", build_sha=None,
                          heartbeat_seconds=0.01, lease_seconds=0.12,
                          on_acquired=start_loops, on_lost=stop_loops)
    other = InstanceLease(store=second_store, instance_id="second", build_sha=None,
                          lease_seconds=0.12, on_acquired=lambda: asyncio.sleep(0),
                          on_lost=lambda: asyncio.sleep(0))
    try:
        start = asyncio.create_task(lease.start())
        await started.wait()
        clock.advance(5)                      # ~40 lease periods: unrenewed, the row would be stale
        await renewed_to_now()
        assert running[0] and not other._try_acquire()
        finish_start.set()
        await start
        shutdown = asyncio.create_task(lease.stop())
        await stopping.wait()
        clock.advance(5)
        await renewed_to_now()
        assert running[0] and not other._try_acquire()
        finish_stop.set()
        await shutdown
        assert not running[0] and other._try_acquire()
    finally:
        finish_start.set()
        finish_stop.set()
        if lease._task is not None:
            await lease.stop()
        first_store.close()
        second_store.close()


_LOST_LEASE_STUCK_CLEANUP_CHILD = """\
import asyncio
import pathlib
import sqlite3
import sys
import time

from trader_runtime.instance_lease import InstanceLease
from trader_runtime.store import RuntimeStore


async def main(database: pathlib.Path) -> None:
    store = RuntimeStore(database)
    release = asyncio.Event()

    async def start_loops() -> None:
        pass

    async def stop_loops() -> None:
        await release.wait()  # Nothing ever sets it: the cleanup is stuck.

    lease = InstanceLease(store=store, instance_id="first", build_sha=None,
                          heartbeat_seconds=0.03, lease_seconds=0.12,
                          on_acquired=start_loops, on_lost=stop_loops)
    await lease.start()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE runtime_lease SET instance_id = 'second' WHERE id = 1")
    print(f"lease-stolen-monotonic {time.monotonic()}", flush=True)
    # Nothing here ends the process: only the lease's own os._exit may.
    await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main(pathlib.Path(sys.argv[1])))
"""


def test_lost_lease_with_stuck_cleanup_exits_in_bounded_time(tmp_path):
    # os._exit ends the process from whichever task hits it. The old in-process
    # double raised SystemExit instead, so passing needed the tick's stuck
    # cleanup wait_for to beat the background renew daemon to their shared
    # max(heartbeat, 1s) deadline; when the daemon won, its SystemExit tore out
    # of the event loop past every try/except (main CI run 36573314751). The
    # real lease in a child process makes both protection paths — the
    # cleanup_failed and the cleanup_timeout exit — a correct process death.
    database = tmp_path / "lost-stuck.sqlite3"
    script = tmp_path / "lost_stuck_child.py"
    script.write_text(_LOST_LEASE_STUCK_CLEANUP_CHILD, encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), str(database)],
                            capture_output=True, text=True, timeout=30)
    finished = time.monotonic()
    # The heartbeat was rejected, the cleanup stayed stuck and the process died
    # through one of the lease's hard exits, about a second after the loss.
    assert "instance_lease_lost instance_id=first" in result.stderr, result.stderr
    assert ("instance_lease_lost_cleanup_failed instance_id=first" in result.stderr
            or "instance_lease_lost_cleanup_timeout instance_id=first" in result.stderr), result.stderr
    assert result.returncode == 1, result.stderr
    # The window starts at the child's steal marker, not at process launch, so
    # interpreter startup, imports and DB setup cannot eat into the bound.
    marker = next((line for line in result.stdout.splitlines()
                   if line.startswith("lease-stolen-monotonic ")), None)
    assert marker is not None, result.stderr
    elapsed = finished - float(marker.removeprefix("lease-stolen-monotonic "))
    assert elapsed < 10, elapsed


async def test_shutdown_stops_the_loops_before_releasing_the_lease(tmp_path):
    database = tmp_path / "release-order.sqlite3"
    app = make_app(database, "instance-shutdown")
    real_release = app.state.store.release_lease
    loops_running_at_release = []

    def release_after_stop(instance_id):
        loops_running_at_release.append(loops_running(app))
        real_release(instance_id)

    app.state.store.release_lease = release_after_stop
    async with app.router.lifespan_context(app):
        assert app.state.instance_lease.role == "leader"
        assert loops_running(app)
    # The new holder may start its loops the moment the lease is free, so the
    # loops must already be down when the release happens — exactly once.
    assert loops_running_at_release == [False]


def producers(app) -> dict[str, bool]:
    """Which background producers (wiring/producers.py) have a task on this app."""
    services, state = app.state.ctx.services, app.state
    analyst = services.research_live.analyst
    return {
        "research_live": services.research_live._loop is not None,
        "news_screen": services.research_live.screener._task is not None,
        "news_analyst": analyst._task is not None,
        "news_translate": services.research_live.translator._task is not None,
        "research_desk": services.research_desk._loop is not None,
        "news_search": services.news_search._task is not None,
        "research_debate": services.research_debate._loop is not None,
        "price_alerts": state.price_alerts._task is not None,
        "account_reminder_loop": state.account_reminder_loop._task is not None,
        "movement_watch": state.movement_watch._task is not None,
        "memory_consolidator": state.memory_consolidator._task is not None,
        "daily_reviews": state.daily_reviews._task is not None,
    }


async def test_only_the_holder_runs_the_producers_and_a_standby_reads_what_it_writes(tmp_path):
    """2026-10-12: two slots on one state database — the news, research, reminder and review loops run on
    the lease holder only; the standby serves reads from the rows the holder writes; the standby starts
    them when it takes the lease over."""
    database = tmp_path / "producers.sqlite3"
    first = make_app(database, "instance-first")
    second = make_app(database, "instance-second")
    second.state.instance_lease.heartbeat_seconds = 0.05
    first_lifespan = first.router.lifespan_context(first)
    await first_lifespan.__aenter__()
    try:
        second_lifespan = second.router.lifespan_context(second)
        await second_lifespan.__aenter__()
    except BaseException:
        await first_lifespan.__aexit__(None, None, None)
        raise
    try:
        assert all(producers(first).values()), producers(first)
        assert not any(producers(second).values()), producers(second)
        # Not producers: every process keeps the heartbeat of its own debates and its order watch.
        for app in (first, second):
            assert app.state.ctx.services.research_debate._beat is not None
            assert app.state.order_watch._task is not None

        # The standby reads what the holder writes: the desk's judgment, the news brief, the day's budget.
        holder, standby = first.state.ctx.services, second.state.ctx.services
        day = holder.research_desk.research_day(holder.research_desk.now())
        holder.research_desk._store("SHFE.rb", {"value": {"stance": "偏多"}, "generatedAt": "2026-10-12T02:00:00+00:00",
                                                "researchDay": day, "model": "test", "name": "螺纹钢"})
        assert standby.research_desk.day_judgments()["SHFE.rb"] == {"stance": "偏多", "name": "螺纹钢", "researchDay": day}
        assert standby.research_desk._current("SHFE.rb")["researchDay"] == day
        # The brief table keeps the last 7 days only: a fixed date here expired on 2026-10-20 (main went red).
        news_day = research_day(holder.research_live.analyst.now())
        holder.research_live.analyst._save("rb", news_day, {"brief": None, "calls": 3})
        assert standby.research_live.analyst._entry("rb", news_day)["calls"] == 3
        screener_day = holder.research_live.screener._today()
        assert holder.research_live.screener._budget.take(screener_day, 5)
        assert standby.research_live.screener.usage()["calls"] == 1
        standby.research_live.screener.touch("rb", "螺纹钢")  # a read served by the standby
        assert ("rb", "螺纹钢") in holder.research_live.screener.active()

        # The holder leaves: its producers stop before the release, the standby takes over and starts them.
        await first_lifespan.__aexit__(None, None, None)
        assert not any(producers(first).values()), producers(first)
        async with asyncio.timeout(2):
            while second.state.instance_lease.role != "leader":
                await asyncio.sleep(0.01)
        assert all(producers(second).values()), producers(second)
    finally:
        await second_lifespan.__aexit__(None, None, None)
    assert not any(producers(second).values())


def test_try_acquire_lease_admits_exactly_one_of_two_racers(tmp_path):
    database = tmp_path / "atomic.sqlite3"
    racers = {"instance-a": RuntimeStore(database), "instance-b": RuntimeStore(database)}
    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def race(instance_id: str) -> None:
        store = racers[instance_id]
        barrier.wait()
        results[instance_id] = store.try_acquire_lease(
            instance_id=instance_id, host="race-host", pid=os.getpid(), build_sha=None,
            now=utc_now(), stale_before=(datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        )

    threads = [threading.Thread(target=race, args=(name,)) for name in racers]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert sorted(results.values()) == [False, True]
        holder = next(name for name, won in results.items() if won)
        # The winner refreshes its own heartbeat; the loser cannot muscle in.
        assert racers[holder].try_acquire_lease(
            instance_id=holder, host="race-host", pid=os.getpid(), build_sha=None,
            now=utc_now(), stale_before=(datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        ) is True
        loser = next(name for name in racers if name != holder)
        assert racers[loser].try_acquire_lease(
            instance_id=loser, host="race-host", pid=os.getpid(), build_sha=None,
            now=utc_now(), stale_before=(datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        ) is False
    finally:
        for store in racers.values():
            store.close()


async def test_replay_children_never_create_a_lease(tmp_path):
    source = HistoricalSource()
    provider = ReplayProvider(source, IDENTIFIER)
    await provider.command("state")
    replay_app = create_app(
        tmp_path / "replay.sqlite3", runtime_state_root=tmp_path, agent_mode="fake",
        data_provider=provider, simulation_gateway=provider, _replay_provider=provider,
    )
    async with replay_app.router.lifespan_context(replay_app):
        assert replay_app.state.instance_lease is None
        assert replay_app.state.account_ledger._task is None
        async with health_client(replay_app) as client:
            health = (await client.get("/healthz")).json()
        assert health["instance"]["role"] == "replay"

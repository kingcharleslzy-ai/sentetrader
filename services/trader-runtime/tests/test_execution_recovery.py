from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest

from test_simulation_orders import USER, Gateway, HangingGateway, NormalProvider, application
from test_strategy_monitor import STRATEGY_ID, ProposalStub, _intent, _save_strategy
from trader_runtime import autonomy as autonomy_module
from trader_runtime.strategy_monitor import StrategyMonitorService


async def make_stop(app):
    session = app.state.broker.create_session("local", "native", USER)
    app.state.autonomy.activate(
        user_id=USER, session_id=session.session_id, symbols=["SHFE.au2612"],
        permissions=["stop", "close"], max_order_volume=1, max_orders=2,
        duration_minutes=30,
    )
    return await app.state.autonomy.create_stop(
        user_id=USER, session_id=session.session_id, symbol="SHFE.au2612",
        position_direction="long", position_bucket="today", volume=1,
        trigger_price=823.5,
    )


async def trigger(app, stop):
    row = app.state.store.connection.execute(
        "SELECT * FROM ai_stop_orders WHERE id = ?", (stop["stopId"],)
    ).fetchone()
    await app.state.autonomy._check_stop(row)
    return app.state.autonomy.list_stops(user_id=USER)[0]


def monitor_for(app):
    _save_strategy(app.state.store)
    intent = _intent(app.state.store)
    intent["riskAdjustedIntent"]["volume"] = 1
    # This fixture exercises configured risk limits, not the separate unconfigured-policy path.
    policies = app.state.simulation_orders.policies
    get_policy = policies.get
    policies.get = lambda **kwargs: replace(get_policy(**kwargs), max_daily_loss=Decimal("10000"))
    return StrategyMonitorService(
        store=app.state.store, proposals=ProposalStub(app.state.store, intent),
        autonomy=app.state.autonomy,
    )


async def test_explicit_resume_with_identical_clock_ticks(tmp_path, monkeypatch):
    app = application(tmp_path / "clock.sqlite3", Gateway())
    fixed = autonomy_module.utc_now()
    monkeypatch.setattr(autonomy_module, "utc_now", lambda: fixed)
    await make_stop(app)
    await app.state.autonomy.deactivate(user_id=USER, emergency=True)
    session = app.state.broker.create_session("local", "native", USER)
    resumed = app.state.autonomy.resume_runtime_access(
        user_id=USER, session_id=session.session_id, symbol="SHFE.au2612",
    )
    assert resumed["status"] == "ACTIVE"
    assert app.state.autonomy.current(user_id=USER)["authorizationId"] == resumed["authorizationId"]
    app.state.store.close()


async def test_restarted_stop_keeps_limits_and_executes(tmp_path):
    path = tmp_path / "restart.sqlite3"
    gateway, provider = Gateway(), NormalProvider()
    app = application(path, gateway, provider)
    stop = await make_stop(app)
    auth = app.state.autonomy.current(user_id=USER)
    app.state.store.connection.execute(
        "UPDATE ai_execution_authorizations SET used_orders = 1 WHERE id = ?",
        (auth["authorizationId"],),
    )
    app.state.store.connection.commit()
    app.state.store.close()
    restarted = application(path, gateway, provider)
    provider.last = 823.0
    result = await trigger(restarted, stop)
    assert result["status"] == "SUBMITTED", result
    current = restarted.state.autonomy.current(user_id=USER)
    assert current["authorizationId"] == auth["authorizationId"]
    # 保护性退出不消耗开仓额度（2026-09-20 用户决定）：重启前后 usedOrders 都保持 1。
    assert current["usedOrders"] == 1 and current["maxOrders"] == 2
    assert gateway.submit_count == 1
    restarted.state.store.close()


async def test_stop_retries_explicit_unsent_using_original_order(tmp_path):
    gateway, provider = Gateway(), NormalProvider()
    app = application(tmp_path / "stop-retry.sqlite3", gateway, provider)
    stop = await make_stop(app)
    provider.last = 823.0
    gateway.submit_report = {"commandStatus": "NOT_DISPATCHED"}
    first = await trigger(app, stop)
    assert first["status"] == "ARMED"
    assert first["closeClientOrderId"]
    assert app.state.autonomy.current(user_id=USER)["usedOrders"] == 0
    gateway.submit_report = None
    second = await trigger(app, stop)
    assert second["status"] == "SUBMITTED"
    assert second["closeClientOrderId"] == first["closeClientOrderId"]
    assert gateway.submit_count == 2
    assert len(app.state.simulation_orders.list(user_id=USER)) == 1
    app.state.store.close()


async def test_strategy_retries_unsent_without_new_attempt_or_order(tmp_path):
    gateway = Gateway()
    app = application(tmp_path / "strategy-retry.sqlite3", gateway)
    monitor = monitor_for(app)
    gateway.submit_report = {"commandStatus": "NOT_DISPATCHED"}
    assert (await monitor.run_once())[0]["status"] == "not_dispatched"
    first = app.state.store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)[0]
    gateway.submit_report = None
    assert (await monitor.run_once())[0]["status"] == "submitted"
    attempts = app.state.store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)
    assert len(attempts) == 1
    assert attempts[0]["attemptId"] == first["attemptId"]
    assert attempts[0]["clientOrderId"] == first["clientOrderId"]
    assert gateway.submit_count == 2
    assert len(app.state.simulation_orders.list(user_id=USER)) == 1
    app.state.store.close()


@pytest.mark.parametrize("outcome, expected", [("accepted", "CANCELLED"), ("filled", "SUBMITTED"), ("unknown", "UNKNOWN")])
async def test_strategy_keeps_order_identity_when_emergency_races_dispatch(tmp_path, outcome, expected):
    class RacingGateway(HangingGateway):
        async def submit_order_once(self, request):
            await super().submit_order_once(request)
            if outcome == "filled":
                return self.report(request, status="FINISHED", volume_left=0)
            if outcome == "unknown":
                raise TimeoutError("reply lost")
            return self.report(request)

    gateway = RacingGateway()
    app = application(tmp_path / "strategy-race.sqlite3", gateway)
    monitor = monitor_for(app)
    task = asyncio.create_task(monitor.run_once())
    await asyncio.wait_for(gateway.submit_started.wait(), timeout=1)
    during = app.state.store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)[0]
    # Identity must be durable before a remote reply, including process interruption.
    assert during["legs"][0]["clientOrderId"] == gateway.request["clientOrderId"]
    await app.state.autonomy.deactivate(user_id=USER, emergency=True)
    gateway.submit_release.set()
    await task
    after = app.state.store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)[0]
    assert after["clientOrderId"] == gateway.request["clientOrderId"]
    assert after["legs"][0]["status"] == expected
    assert after["status"] == expected
    assert gateway.submit_count == 1
    assert gateway.cancel_count == (1 if outcome == "accepted" else 0)
    assert app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)["status"] == "paused"
    app.state.store.close()


@pytest.mark.parametrize("boundary", ["emergency_before_restart", "emergency_after_restart", "expired", "budget", "position", "frozen", "stale"])
async def test_restart_never_bypasses_stop_boundaries(tmp_path, boundary):
    path = tmp_path / "boundary.sqlite3"
    gateway, provider = Gateway(), NormalProvider()
    app = application(path, gateway, provider)
    stop = await make_stop(app)
    if boundary == "emergency_before_restart":
        await app.state.autonomy.deactivate(user_id=USER, emergency=True)
    elif boundary == "expired":
        app.state.store.connection.execute("UPDATE ai_execution_authorizations SET expires_at = '2000-01-01T00:00:00+00:00'")
    elif boundary == "budget":
        app.state.store.connection.execute("UPDATE ai_execution_authorizations SET used_orders = max_orders")
    app.state.store.connection.commit()
    app.state.store.close()
    restarted = application(path, gateway, provider)
    if boundary == "emergency_after_restart":
        await restarted.state.autonomy.deactivate(user_id=USER, emergency=True)
    elif boundary == "position":
        provider.position_items = []
    elif boundary == "frozen":
        provider.position_items[0]["frozenToday"] = 1
    elif boundary == "stale":
        provider.age_seconds = 60
    provider.last = 823.0
    result = await trigger(restarted, stop)
    if boundary in {"expired", "budget"}:
        # 租约过期与额度耗尽不再拦截保护性退出（2026-09-20 用户决定：只被急停拦），
        # 重启后同样执行；其余边界（急停/持仓/冻结/行情）仍然失败关闭。
        assert gateway.submit_count == 1
        assert result["status"] == "SUBMITTED"
        restarted.state.store.close()
        return
    assert gateway.submit_count == 0
    if boundary.startswith("emergency"):
        session = restarted.state.broker.create_session("local", "native", USER)
        restarted.state.autonomy.resume_runtime_access(user_id=USER, session_id=session.session_id, symbol="SHFE.au2612")
        result = await trigger(restarted, stop)
        assert result["status"] == "CANCELLED"
        assert gateway.submit_count == 0
    restarted.state.store.close()


async def test_reconcile_legacy_unsent_stop_recovers_original_identity(tmp_path):
    gateway, provider = Gateway(), NormalProvider()
    app = application(tmp_path / "legacy-unsent.sqlite3", gateway, provider)
    stop = await make_stop(app)
    provider.last = 823.0
    gateway.submit_report = {"commandStatus": "NOT_DISPATCHED"}
    first = await trigger(app, stop)
    app.state.store.connection.execute("UPDATE ai_stop_orders SET status = 'SUBMITTED'")
    app.state.store.connection.commit()
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._reconcile_stop(row)
    assert gateway.query_count == 0
    assert app.state.autonomy.list_stops(user_id=USER)[0]["status"] == "ARMED"
    gateway.submit_report = None
    second = await trigger(app, stop)
    assert second["status"] == "SUBMITTED"
    assert first["closeClientOrderId"] == second["closeClientOrderId"]
    app.state.store.close()


async def test_a_trigger_reserved_but_never_dispatched_re_arms_after_restart(tmp_path):
    """#528-C：进程在预定了止损触发、还没写出平仓单身份时中断——重启恢复按“没发过”处理：
    止损回到 ARMED 并写明 runtime_restarted_before_dispatch，之后还能正常触发（只发一单）。"""
    gateway, provider = Gateway(), NormalProvider()
    app = application(tmp_path / "reserved-not-sent.sqlite3", gateway, provider)
    stop = await make_stop(app)
    # The interruption landed between reserving the trigger and dispatching the close.
    with app.state.store.connection:
        app.state.store.connection.execute(
            "UPDATE ai_stop_orders SET status = 'TRIGGERING' WHERE id = ?", (stop["stopId"],))
    app.state.autonomy.recover_interrupted_execution()
    rearmed = app.state.autonomy.list_stops(user_id=USER)[0]
    assert rearmed["status"] == "ARMED"
    assert rearmed["lastError"] == "runtime_restarted_before_dispatch"
    provider.last = 823.0
    result = await trigger(app, stop)
    assert result["status"] == "SUBMITTED" and gateway.submit_count == 1
    app.state.store.close()


async def test_strategy_unknown_submission_never_retries(tmp_path):
    gateway = Gateway()
    gateway.submit_error = True
    app = application(tmp_path / "unknown.sqlite3", gateway)
    monitor = monitor_for(app)
    assert (await monitor.run_once())[0]["status"] == "unknown"
    gateway.submit_error = False
    await monitor.run_once()
    order = app.state.simulation_orders.list(user_id=USER)[0]
    with pytest.raises(autonomy_module.SimulationOrderError, match="order_not_prepared"):
        await app.state.autonomy.retry_trade(user_id=USER, client_order_id=order["clientOrderId"])
    assert gateway.submit_count == 1
    app.state.store.close()


@pytest.mark.parametrize("boundary", ["stale", "tick", "position", "emergency"])
async def test_unsent_stop_retry_revalidates_live_boundaries(tmp_path, boundary):
    gateway, provider = Gateway(), NormalProvider()
    app = application(tmp_path / "retry-boundary.sqlite3", gateway, provider)
    stop = await make_stop(app)
    provider.last = 823.0
    gateway.submit_report = {"commandStatus": "NOT_DISPATCHED"}
    first = await trigger(app, stop)
    gateway.submit_report = None
    if boundary == "stale":
        provider.age_seconds = 60
    elif boundary == "tick":
        provider.price_tick = 2.0
    elif boundary == "position":
        provider.position_items = []
    else:
        await app.state.autonomy.deactivate(user_id=USER, emergency=True)
    second = await trigger(app, stop)
    assert gateway.submit_count == 1
    assert second["closeClientOrderId"] == first["closeClientOrderId"]
    assert app.state.autonomy.current(user_id=USER)["usedOrders"] == 0
    app.state.store.close()


async def test_limits_conflict_and_update_preserve_stop_and_consumption(tmp_path):
    app = application(tmp_path / "limits.sqlite3", Gateway())
    await make_stop(app)
    auth = app.state.autonomy.current(user_id=USER)
    app.state.store.connection.execute("UPDATE ai_execution_authorizations SET used_orders = 1 WHERE id = ?", (auth["authorizationId"],))
    app.state.store.connection.commit()
    await app.state.autonomy.deactivate(user_id=USER, emergency=True)
    original = app.state.autonomy.controls(user_id=USER)
    tightened = app.state.autonomy.update_limits(user_id=USER, revision=original["revision"], max_order_volume=1, max_orders=2)
    with pytest.raises(autonomy_module.AutonomyError, match="simulation_limits_conflict"):
        app.state.autonomy.update_limits(user_id=USER, revision=original["revision"], max_order_volume=50, max_orders=100)
    assert app.state.autonomy.controls(user_id=USER) == tightened
    assert tightened["stopStatus"] == original["stopStatus"] == "EMERGENCY_STOP"
    assert tightened["generation"] == original["generation"]
    current = app.state.autonomy.current(user_id=USER)
    assert current["status"] == "EMERGENCY_STOP" and current["usedOrders"] == 1
    assert current["authorizationId"] == auth["authorizationId"]
    app.state.store.close()

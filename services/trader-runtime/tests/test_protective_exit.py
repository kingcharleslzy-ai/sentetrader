"""Protective exits answer only to an emergency stop (owner decision 2026-09-20).

Three stop tiers: EMERGENCY_STOP cancels AI orders and voids armed stops; HALT_OPENS
(scope=opens) refuses new opens only; STOPPED blocks every AI-initiated action but
still lets armed protective stops and take-profits execute.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from test_simulation_orders import USER, Gateway, NormalProvider, application, quote_now
from test_strategy_monitor import (
    BAR_TIME,
    STRATEGY_ID,
    AutonomyStub,
    DataStub,
    ProposalStub,
    _intent,
    _save_strategy,
)
from trader_runtime.autonomy import AutonomyError
from trader_runtime.store import RuntimeStore
from trader_runtime.strategy_monitor import StrategyMonitorService


def _activate(app, *, permissions: list[str], max_order_volume: int = 1,
              max_orders: int = 5, duration_minutes: int = 30) -> str:
    session = app.state.broker.create_session("local", "native", USER)
    app.state.autonomy.activate(
        user_id=USER, session_id=session.session_id, symbols=["SHFE.au2612"],
        permissions=permissions, max_order_volume=max_order_volume,
        max_orders=max_orders, duration_minutes=duration_minutes,
    )
    return session.session_id


async def _arm_stop(app, session_id: str, *, volume: int = 1) -> dict[str, Any]:
    return await app.state.autonomy.create_stop(
        user_id=USER, session_id=session_id, symbol="SHFE.au2612",
        position_direction="long", position_bucket="today", volume=volume,
        trigger_price=823.5,
    )


def _stop_row(app, stop: dict[str, Any]):
    return app.state.store.connection.execute(
        "SELECT * FROM ai_stop_orders WHERE id = ?", (stop["stopId"],)
    ).fetchone()


async def _trigger(app, stop: dict[str, Any]) -> dict[str, Any]:
    await app.state.autonomy._check_stop(_stop_row(app, stop))
    return app.state.autonomy.list_stops(user_id=USER)[0]


def _armable_market(provider: NormalProvider) -> None:
    provider.last = 823.0
    provider.quote_datetime = quote_now()


async def test_protective_stop_executes_after_normal_stop(tmp_path: Path) -> None:
    """T4: a normal stop freezes AI actions, not the armed protective exit."""
    gateway, provider = Gateway(), NormalProvider()
    app = application(tmp_path / "protective-normal-stop.sqlite3", gateway, provider)
    session_id = _activate(app, permissions=["stop", "close"])
    stop = await _arm_stop(app, session_id)

    stopped = await app.state.autonomy.deactivate(user_id=USER, emergency=False)

    assert app.state.autonomy.controls(user_id=USER)["stopStatus"] == "STOPPED"
    _armable_market(provider)
    result = await _trigger(app, stop)
    assert result["status"] == "SUBMITTED", result["lastError"]
    assert gateway.submit_count == 1
    assert gateway.request is not None and gateway.request["offset"] == "CLOSETODAY"
    assert app.state.autonomy.current(user_id=USER)["usedOrders"] == 0
    assert stopped["scope"] == "all"
    app.state.store.close()


async def test_protective_stop_ignores_exhausted_order_budget(tmp_path: Path) -> None:
    """T5: a triggered protective exit neither checks nor consumes the order budget."""
    gateway, provider = Gateway(), NormalProvider()
    app = application(tmp_path / "protective-exhausted.sqlite3", gateway, provider)
    session_id = _activate(app, permissions=["open", "close", "stop"], max_orders=1)
    await app.state.autonomy.execute_trade(
        user_id=USER, session_id=session_id, symbol="SHFE.au2612",
        side="buy_open", volume=1, price_intent="passive", reason="用掉唯一开仓额度",
    )
    assert app.state.autonomy.current(user_id=USER)["usedOrders"] == 1
    stop = await _arm_stop(app, session_id)

    _armable_market(provider)
    result = await _trigger(app, stop)

    assert result["status"] == "SUBMITTED", result["lastError"]
    assert gateway.submit_count == 2
    assert app.state.autonomy.current(user_id=USER)["usedOrders"] == 1
    app.state.store.close()


async def test_protective_stop_ignores_per_order_volume_cap(tmp_path: Path) -> None:
    """T5: arming and triggering a protective stop ignores the per-order volume cap."""
    gateway, provider = Gateway(), NormalProvider()
    provider.position_items[0].update({"volume": 2, "todayVolume": 2, "historicalVolume": 0})
    app = application(tmp_path / "protective-volume.sqlite3", gateway, provider)
    session_id = _activate(app, permissions=["stop", "close"], max_order_volume=1)
    stop = await _arm_stop(app, session_id, volume=2)

    assert stop["status"] == "ARMED"
    _armable_market(provider)
    result = await _trigger(app, stop)

    assert result["status"] == "SUBMITTED", result["lastError"]
    assert gateway.submit_count == 1
    assert gateway.request is not None and gateway.request["volume"] == 2
    app.state.store.close()


async def test_protective_stop_executes_after_lease_expiry(tmp_path: Path) -> None:
    """T6: lease expiry does not disarm an armed protective exit."""
    gateway, provider = Gateway(), NormalProvider()
    app = application(tmp_path / "protective-expired.sqlite3", gateway, provider)
    session_id = _activate(app, permissions=["stop", "close"], duration_minutes=5)
    stop = await _arm_stop(app, session_id)
    app.state.store.connection.execute(
        "UPDATE ai_execution_authorizations SET expires_at = '2000-01-01T00:00:00+00:00'"
    )
    app.state.store.connection.commit()
    assert app.state.autonomy.current(user_id=USER)["status"] == "EXPIRED"

    _armable_market(provider)
    result = await _trigger(app, stop)

    assert result["status"] == "SUBMITTED", result["lastError"]
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_halt_opens_blocks_only_new_opens(tmp_path: Path) -> None:
    """T1: scope=opens refuses opens; closes, stop placement and the lease continue."""
    gateway = Gateway()
    app = application(tmp_path / "halt-opens.sqlite3", gateway)
    session_id = _activate(app, permissions=["open", "cancel", "close", "stop"])

    halted = await app.state.autonomy.deactivate(user_id=USER, emergency=False, scope="opens")

    assert halted["scope"] == "opens"
    assert halted["emergency"] is False
    assert halted["cancelledOrders"] == []
    assert app.state.autonomy.controls(user_id=USER)["stopStatus"] == "HALT_OPENS"
    assert app.state.autonomy.current(user_id=USER)["status"] == "ACTIVE"
    with pytest.raises(AutonomyError, match="ai_new_opens_halted"):
        await app.state.autonomy.execute_trade(
            user_id=USER, session_id=session_id, symbol="SHFE.au2612",
            side="buy_open", volume=1, price_intent="passive",
            reason="只停新开仓时不得开仓", ai_initiated=True,
        )
    assert gateway.submit_count == 0
    closed = await app.state.autonomy.execute_trade(
        user_id=USER, session_id=session_id, symbol="SHFE.au2612",
        side="sell_close", volume=1, price_intent="passive", position_bucket="today",
        reason="只停新开仓时平仓照常", ai_initiated=True,
    )
    assert closed["simulationOrder"]["displayStatus"] == "已受理"
    assert gateway.submit_count == 1
    stop = await _arm_stop(app, session_id)
    assert stop["status"] == "ARMED"

    resumed = app.state.autonomy.resume_runtime_access(
        user_id=USER, session_id=session_id, symbol="SHFE.au2612", ai_initiated=True,
    )

    assert resumed["status"] == "ACTIVE"
    assert app.state.autonomy.controls(user_id=USER)["stopStatus"] is None
    reopened = await app.state.autonomy.execute_trade(
        user_id=USER, session_id=session_id, symbol="SHFE.au2612",
        side="buy_open", volume=1, price_intent="passive",
        reason="恢复后开仓继续", ai_initiated=True,
    )
    assert reopened["simulationOrder"]["displayStatus"] == "已受理"
    assert gateway.submit_count == 2
    app.state.store.close()


async def test_stop_route_scope_opens_keeps_strategies_monitoring(tmp_path: Path) -> None:
    """T2: scope=opens halts opens without pausing strategies; emergency+opens is rejected."""
    app = application(tmp_path / "halt-opens-route.sqlite3", Gateway())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as client:
        assert (await client.post(
            "/api/auth/local", headers={"Authorization": "Bearer test-local-token"},
        )).status_code == 200
        session = (await client.post(
            "/api/sessions", json={"mode": "local", "agentId": "native"},
        )).json()
        app.state.store.save_strategy_draft(
            strategy_id="strategy-halt-opens",
            user_id=USER,
            source_session_id=session["sessionId"],
            name="只停新开仓策略",
            symbol="SHFE.au2612",
            version=1,
            status="monitoring",
            definition={"timeframeSeconds": 300, "riskLimits": {}},
            definition_digest="a" * 64,
        )
        halted = await client.post("/api/ai-execution/stop", json={"scope": "opens"})
        assert halted.status_code == 200
        assert halted.json()["scope"] == "opens"
        assert halted.json()["emergency"] is False
        assert halted.json()["pausedStrategyIds"] == []
        assert app.state.store.get_strategy_draft(
            "strategy-halt-opens", user_id=USER
        )["status"] == "monitoring"
        assert app.state.autonomy.controls(user_id=USER)["stopStatus"] == "HALT_OPENS"

        invalid = await client.post(
            "/api/ai-execution/stop", json={"emergency": True, "scope": "opens"},
        )
        assert invalid.status_code == 409
        assert invalid.json()["detail"]["code"] == "invalid_stop_scope"

        default = await client.post("/api/ai-execution/stop", json={})
        assert default.status_code == 200
        assert default.json()["scope"] == "all"
        assert default.json()["pausedStrategyIds"] == ["strategy-halt-opens"]
        assert app.state.store.get_strategy_draft(
            "strategy-halt-opens", user_id=USER
        )["status"] == "paused"
    app.state.store.close()


async def test_monitor_blocks_only_opens_while_new_opens_halted(tmp_path: Path) -> None:
    """T3: under HALT_OPENS the strategy keeps its bar for opens and still closes."""
    store = RuntimeStore(tmp_path / "halt-opens-monitor.sqlite3")
    _save_strategy(store)
    intent = _intent(store)
    autonomy = AutonomyStub(store, DataStub())
    autonomy.stop_status = "HALT_OPENS"
    monitor = StrategyMonitorService(
        store=store,
        proposals=ProposalStub(store, intent),  # type: ignore[arg-type]
        autonomy=autonomy,  # type: ignore[arg-type]
    )

    blocked = await monitor.run_once()

    assert blocked[0]["status"] == "blocked"
    assert blocked[0]["action"] == "open_long"
    assert "只停新开仓" in blocked[0]["detail"]
    assert autonomy.calls == []
    assert store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER) == []
    assert store.get_strategy_draft(STRATEGY_ID, user_id=USER)["status"] == "monitoring"

    intent["riskAdjustedIntent"].update({"action": "close_long", "volume": 2})
    autonomy.data_provider.position_rows = [{
        "symbol": "SHFE.au2612", "direction": "long",
        "volume": 2, "todayVolume": 2, "historicalVolume": 0,
    }]
    closed = await monitor.run_once()
    assert closed[0]["status"] == "submitted"
    assert len(autonomy.calls) == 1

    autonomy.stop_status = None
    intent["riskAdjustedIntent"].update({"action": "open_long", "volume": 2})
    # The close above consumed the original bar; the resumed open gets the next one.
    intent["signalSnapshot"]["barTime"] = str(int(BAR_TIME) + 300_000_000_000)
    resumed = await monitor.run_once()
    assert resumed[0]["status"] == "submitted"
    assert len(autonomy.calls) == 2
    store.close()


async def test_emergency_stop_still_blocks_protective_exit(tmp_path: Path) -> None:
    """T7: the emergency stop remains the only fence protective exits cannot cross."""
    gateway, provider = Gateway(), NormalProvider()
    app = application(tmp_path / "protective-emergency.sqlite3", gateway, provider)
    session_id = _activate(app, permissions=["stop", "close"])
    stop = await _arm_stop(app, session_id)

    await app.state.autonomy.deactivate(user_id=USER, emergency=True)

    with pytest.raises(AutonomyError, match="ai_execution_stopped"):
        app.state.autonomy._stop_authorization(_stop_row(app, stop))
    _armable_market(provider)
    result = await _trigger(app, stop)
    assert result["status"] == "CANCELLED"
    assert gateway.submit_count == 0
    app.state.store.close()


async def test_stop_fence_only_strengthens(tmp_path: Path) -> None:
    """T8: 只停新开仓 never overwrites a stronger fence; on HALT_OPENS it stays idempotent."""
    app = application(tmp_path / "stop-fence-strength.sqlite3", Gateway())
    _activate(app, permissions=["open", "close", "stop"])

    await app.state.autonomy.deactivate(user_id=USER, emergency=False, scope="opens")
    again = await app.state.autonomy.deactivate(user_id=USER, emergency=False, scope="opens")

    assert again["scope"] == "opens"
    assert app.state.autonomy.controls(user_id=USER)["stopStatus"] == "HALT_OPENS"

    await app.state.autonomy.deactivate(user_id=USER, emergency=False)
    assert app.state.autonomy.controls(user_id=USER)["stopStatus"] == "STOPPED"
    with pytest.raises(AutonomyError, match="ai_execution_stopped"):
        await app.state.autonomy.deactivate(user_id=USER, emergency=False, scope="opens")
    assert app.state.autonomy.controls(user_id=USER)["stopStatus"] == "STOPPED"

    await app.state.autonomy.deactivate(user_id=USER, emergency=True)
    with pytest.raises(AutonomyError, match="ai_execution_stopped"):
        await app.state.autonomy.deactivate(user_id=USER, emergency=False, scope="opens")
    assert app.state.autonomy.controls(user_id=USER)["stopStatus"] == "EMERGENCY_STOP"
    app.state.store.close()


async def test_plain_stop_never_downgrades_emergency_stop(tmp_path: Path) -> None:
    """T9: re-clicking a plain stop after an emergency stop keeps the emergency fence."""
    gateway, provider = Gateway(), NormalProvider()
    app = application(tmp_path / "stop-fence-emergency.sqlite3", gateway, provider)
    session_id = _activate(app, permissions=["stop", "close"])
    stop = await _arm_stop(app, session_id)

    await app.state.autonomy.deactivate(user_id=USER, emergency=True)
    assert app.state.autonomy.list_stops(user_id=USER)[0]["status"] == "CANCELLED"

    restopped = await app.state.autonomy.deactivate(user_id=USER, emergency=False)

    assert restopped["emergency"] is False
    assert restopped["scope"] == "all"
    assert restopped["cancelledOrders"] == []
    assert restopped["authorization"]["status"] == "EMERGENCY_STOP"
    assert app.state.autonomy.controls(user_id=USER)["stopStatus"] == "EMERGENCY_STOP"
    assert app.state.autonomy.list_stops(user_id=USER)[0]["status"] == "CANCELLED"
    _armable_market(provider)
    result = await _trigger(app, stop)
    assert result["status"] == "CANCELLED"
    assert gateway.submit_count == 0

    resumed = app.state.autonomy.resume_runtime_access(
        user_id=USER, session_id=session_id, symbol="SHFE.au2612", ai_initiated=True,
    )

    assert resumed["status"] == "ACTIVE"
    assert app.state.autonomy.controls(user_id=USER)["stopStatus"] is None
    app.state.store.close()


async def test_stop_route_opens_conflicts_with_emergency_stop(tmp_path: Path) -> None:
    """T10: scope=opens on an emergency-stopped account returns 409 and keeps the fence."""
    app = application(tmp_path / "stop-fence-route.sqlite3", Gateway())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as client:
        assert (await client.post(
            "/api/auth/local", headers={"Authorization": "Bearer test-local-token"},
        )).status_code == 200
        stopped = await client.post("/api/ai-execution/stop", json={"emergency": True})
        assert stopped.status_code == 200
        assert app.state.autonomy.controls(user_id=USER)["stopStatus"] == "EMERGENCY_STOP"

        conflict = await client.post("/api/ai-execution/stop", json={"scope": "opens"})

        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "ai_execution_stopped"
        assert app.state.autonomy.controls(user_id=USER)["stopStatus"] == "EMERGENCY_STOP"
    app.state.store.close()

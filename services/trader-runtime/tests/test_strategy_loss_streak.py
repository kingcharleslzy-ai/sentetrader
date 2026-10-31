from __future__ import annotations

import json
from decimal import Decimal

import pytest

from test_simulation_orders import USER, application
from test_strategy_monitor import STRATEGY_ID, ProposalStub, _intent
from test_strategy_protection import entry_fill, setup
from trader_runtime.strategy_monitor import LOSS_STREAK_PAUSE_REASON, StrategyMonitorService
from trader_runtime.strategy_protection import closed_results


async def unconfirmed_closes(tmp_path, prices):
    """Real fill attribution, with complete fills but delayed order terminals."""
    app, provider, gateway, monitor = setup(tmp_path)
    volume = len(prices)
    strategy = app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    # Keep the entry inside the existing 2,000-unit account risk budget:
    # at most four lots, one 0.5-unit tick, and the gold multiplier of 1,000.
    strategy["definition"]["riskLimits"].update(maxPosition=volume, stopLossTicks=1)
    app.state.store.connection.execute("UPDATE strategy_drafts SET definition_json = ? WHERE id = ?",
                                       (json.dumps(strategy["definition"]), STRATEGY_ID))
    app.state.store.connection.commit()
    monitor.proposals.intent["riskAdjustedIntent"]["volume"] = volume
    result = await monitor.run_once()
    assert result[0]["status"] == "submitted", result
    entry = dict(gateway.request)
    entry_fill(provider, entry, volume=volume)
    provider.position_items = [{"symbol": entry["symbol"], "direction": "long", "volume": volume,
        "todayVolume": volume, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    provider.activity["orders"] = [{**entry, "status": "FINISHED", "volumeLeft": 0}]
    gateway.query_report = gateway.report(entry, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(entry["clientOrderId"], user_id=USER)
    assert app.state.autonomy.sync_strategy_protection() == {}
    for index, price in enumerate(prices):
        identifier = f"close-{index}"
        attempt_id, leg_id = f"attempt-{index}", f"leg-{index}"
        assert app.state.store.reserve_strategy_execution_attempt(
            attempt_id=attempt_id, strategy_id=STRATEGY_ID, user_id=USER,
            intent_id=monitor.proposals.intent["intentId"], signal_bar_time=str(index + 2),
            trading_day=provider.day, action="close_long", volume=1,
        )
        app.state.store.reserve_strategy_execution_leg(
            leg_id=leg_id, attempt_id=attempt_id, leg_index=0, position_bucket="today", volume=1,
        )
        app.state.store.bind_strategy_execution_leg(leg_id=leg_id, client_order_id=identifier)
        close = {**entry, "clientOrderId": identifier, "direction": "SELL", "offset": "CLOSETODAY", "volume": 1}
        entry_fill(provider, close, trade_id=identifier, price=price, time=index + 2)
        provider.activity["orders"].append({**close, "status": "ALIVE", "volumeLeft": 0})
    provider.position_items = []
    assert app.state.autonomy.sync_strategy_protection() == {}
    assert closed_results(app.state.store, USER, STRATEGY_ID, provider.day) == []
    return app, provider, gateway, monitor


def confirm(app, provider, indices):
    identifiers = {f"close-{index}" for index in indices}
    for order in provider.activity["orders"]:
        if order["clientOrderId"] in identifiers:
            order["status"] = "FINISHED"
    assert app.state.autonomy.sync_strategy_protection() == {}


def state(app, provider, monitor):
    strategy = app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    return monitor._state(strategy=strategy, account=provider.account_summary(""), activity=provider.activity)[0]


def restart(app, provider, gateway, tmp_path):
    app.state.store.close()
    app = application(tmp_path / "protection.sqlite3", gateway, provider)
    monitor = StrategyMonitorService(store=app.state.store,
        proposals=ProposalStub(app.state.store, _intent(app.state.store)), autonomy=app.state.autonomy)
    return app, monitor


@pytest.mark.parametrize("batches", [[[0, 1, 2, 3]], [[1, 2], [0, 3]], [[3], [0], [2], [1]]])
@pytest.mark.parametrize("reopen", [False, True])
async def test_completed_fill_order_is_independent_of_terminal_batches_and_restart(tmp_path, batches, reopen):
    app, provider, gateway, monitor = await unconfirmed_closes(tmp_path, [823, 821, 821, 821])
    try:
        for index, batch in enumerate(batches):
            confirm(app, provider, batch)
            state(app, provider, monitor)
            if reopen and index == 0:
                app, monitor = restart(app, provider, gateway, tmp_path)
        result = state(app, provider, monitor)
        assert result["consecutiveLosses"] == 3
        assert result["lastTradeId"] == "close:close-3"
        assert result["pauseReason"] == LOSS_STREAK_PAUSE_REASON
        assert closed_results(app.state.store, USER, STRATEGY_ID, provider.day) == [
            ("close:close-0", Decimal("1000")), ("close:close-1", Decimal("-1000")),
            ("close:close-2", Decimal("-1000")), ("close:close-3", Decimal("-1000")),
        ]
        assert gateway.submit_count == 1
    finally:
        app.state.store.close()


@pytest.mark.parametrize("early_pause", [False, True])
async def test_late_profit_corrects_the_actual_streak_but_does_not_release_an_existing_pause(tmp_path, early_pause):
    app, provider, gateway, monitor = await unconfirmed_closes(tmp_path, [821, 823, 821, 821])
    try:
        if early_pause:
            confirm(app, provider, [0, 2, 3])
            assert (await monitor.run_once())[0]["status"] == "paused"
            assert state(app, provider, monitor)["consecutiveLosses"] == 3
            app, monitor = restart(app, provider, gateway, tmp_path)
        confirm(app, provider, [0, 1, 2, 3])
        result = state(app, provider, monitor)
        assert result["consecutiveLosses"] == 2
        assert result["pauseReason"] == (LOSS_STREAK_PAUSE_REASON if early_pause else None)
        expected_status = "paused" if early_pause else "monitoring"
        assert app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)["status"] == expected_status
        assert gateway.submit_count == 1
    finally:
        app.state.store.close()


async def test_v2_exit_error_and_resume_preserve_daily_pause_while_next_day_resets_it(tmp_path):
    from test_strategy_workbench import plan
    from trader_runtime.strategy_workbench import definition_for

    app, provider, gateway, monitor = await unconfirmed_closes(tmp_path, [821, 821, 821, 823])
    try:
        confirm(app, provider, [0, 1, 2, 3])
        definition = definition_for(plan())
        app.state.store.connection.execute("UPDATE strategy_drafts SET definition_json = ? WHERE id = ?",
                                           (json.dumps(definition), STRATEGY_ID))
        app.state.store.connection.commit()
        result = state(app, provider, monitor)
        assert result["consecutiveLosses"] == 0
        assert result["pauseReason"] == LOSS_STREAK_PAUSE_REASON
        assert (await monitor.run_once())[0]["status"] == "blocked"
        assert gateway.submit_count == 1

        # An existing position may still exit, and that successful save must not
        # discard the daily entry pause just because the latest close was a win.
        provider.position_items = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 1,
            "todayVolume": 1, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
        monitor.proposals.intent["signalSnapshot"]["barTime"] = "1767225660000000000"
        monitor.proposals.intent["riskAdjustedIntent"].update(action="close_long", volume=1)
        assert (await monitor.run_once())[0]["status"] == "submitted"
        assert gateway.request["offset"] == "CLOSETODAY"
        assert gateway.submit_count == 2
        assert app.state.store.strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER)["pauseReason"] == LOSS_STREAK_PAUSE_REASON

        original = monitor.proposals.preview_strategy_intent
        def unavailable(**kwargs):
            raise RuntimeError("fixture_quote_unavailable")
        monitor.proposals.preview_strategy_intent = unavailable
        for _ in range(3):
            await monitor.run_once()
            app.state.store.connection.execute("UPDATE strategy_monitor_states SET next_retry_at = NULL")
            app.state.store.connection.commit()
        assert app.state.store.strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER)["pauseReason"] == LOSS_STREAK_PAUSE_REASON
        monitor.proposals.preview_strategy_intent = original
        monitor.proposals.transition_strategy_status(strategy_id=STRATEGY_ID, user_id=USER, target="monitoring")
        monitor.proposals.intent["riskAdjustedIntent"].update(action="open_long", volume=1)
        assert (await monitor.run_once())[0]["status"] == "blocked"
        assert gateway.submit_count == 2

        provider.day = "2026-08-26"
        result = state(app, provider, monitor)
        assert result["consecutiveLosses"] == 0 and result["pauseReason"] is None
    finally:
        app.state.store.close()


async def test_next_day_terminal_keeps_already_recorded_fills_on_their_business_day(tmp_path):
    app, provider, gateway, monitor = await unconfirmed_closes(tmp_path, [821, 821, 821])
    try:
        original_day = provider.day
        app, monitor = restart(app, provider, gateway, tmp_path)
        provider.day = "2026-08-26"
        confirm(app, provider, [0, 1, 2])
        assert len(closed_results(app.state.store, USER, STRATEGY_ID, original_day)) == 3
        result = state(app, provider, monitor)
        assert result["consecutiveLosses"] == 0 and result["pauseReason"] is None
    finally:
        app.state.store.close()


def test_cross_contract_results_use_last_fill_time_and_day_for_the_whole_order(tmp_path):
    app, _, _, _ = setup(tmp_path)
    try:
        first_day, last_day = "2026-08-25", "2026-08-26"
        fills = [
            ("gold-1", "gold", "SHFE.au2612", 10, first_day, -20, 1),
            ("gold-2", "gold", "SHFE.au2612", 30, last_day, 10, 1),
            ("silver", "silver", "SHFE.ag2612", 20, last_day, 5, 3),
            ("old", "old", "SHFE.ag2612", 15, first_day, -8, 4),
            ("pending", "pending", "SHFE.ag2612", 25, last_day, -9, 0),
        ]
        for event, order, symbol, time, day, pnl, settled in fills:
            app.state.store.connection.execute("""INSERT INTO strategy_fill_events
                (user_id, event_id, client_order_id, trade_time, trading_day, payload_json, settled, settled_day)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (USER, event, order, time, day,
                json.dumps({"trade": {"symbol": symbol}, "realizedGrossPnl": {STRATEGY_ID: str(pnl)}}), settled, last_day))
        app.state.store.connection.commit()
        assert closed_results(app.state.store, USER, STRATEGY_ID, first_day) == [("close:old", Decimal(-8))]
        assert closed_results(app.state.store, USER, STRATEGY_ID, last_day) == [
            ("close:silver", Decimal(5)), ("close:gold", Decimal(-10)),
        ]
    finally:
        app.state.store.close()


@pytest.mark.parametrize("last_trade_id, protected", [("close:legacy-order", True), ("old-equity-id", False)])
def test_legacy_daily_pause_only_migrates_fill_backed_state(tmp_path, last_trade_id, protected):
    app, provider, _, monitor = setup(tmp_path)
    try:
        app.state.store.save_strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER,
            trading_date=provider.day, high_watermark_equity="1000000", last_today_pnl="0",
            consecutive_losses=3, last_trade_id=last_trade_id, last_executed_bar=None, pause_reason=None)
        result = state(app, provider, monitor)
        assert result["consecutiveLosses"] == 0
        assert result["pauseReason"] == (LOSS_STREAK_PAUSE_REASON if protected else None)
    finally:
        app.state.store.close()

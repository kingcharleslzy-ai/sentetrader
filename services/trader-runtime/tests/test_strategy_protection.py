from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from test_simulation_orders import USER, Gateway, NormalProvider, application, quote_now
from test_strategy_monitor import STRATEGY_ID, ProposalStub, _intent, _save_strategy
from trader_runtime.strategy_monitor import StrategyMonitorService


class Provider(NormalProvider):
    def __init__(self):
        super().__init__()
        self.day = "2026-08-25"
        self.activity = {"dataStatus": "normal", "isCached": False, "ageSeconds": 0,
                         "ordersInitialized": True, "orders": [], "trades": []}
        self.position_items = []

    def account_activity(self):
        return self.activity

    def account_summary(self, turn_id):
        return {**super().account_summary(turn_id), "tradingDay": self.day}


def setup(tmp_path):
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / "protection.sqlite3", gateway, provider)
    _save_strategy(app.state.store)
    intent = _intent(app.state.store)
    intent["riskAdjustedIntent"]["volume"] = 1
    monitor = StrategyMonitorService(store=app.state.store,
        proposals=ProposalStub(app.state.store, intent), autonomy=app.state.autonomy)
    return app, provider, gateway, monitor


async def test_unconfigured_daily_loss_does_not_crash_monitor(tmp_path):
    app, _, gateway, monitor = setup(tmp_path)
    result = await monitor.run_once()
    assert result[0]["status"] == "submitted", result
    assert gateway.submit_count == 1
    app.state.store.close()


@pytest.mark.parametrize("error_type", [RuntimeError, TypeError, ValueError, KeyError])
async def test_real_provider_errors_accumulate_and_pause(tmp_path, error_type):
    app, _, _, monitor = setup(tmp_path)
    def unavailable(**kwargs):
        raise error_type("provider_broken")
    monitor.proposals.preview_strategy_intent = unavailable
    for index in range(3):
        result = await monitor.run_once()
        assert result[0]["status"] == ("paused" if index == 2 else "error"), result
        app.state.store.connection.execute("UPDATE strategy_monitor_states SET next_retry_at = NULL")
        app.state.store.connection.commit()
    assert app.state.store.strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER)["consecutiveFailures"] == 3
    app.state.store.close()


@pytest.mark.parametrize("kind", ["stop", "take"])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("short", [False, True])
async def test_actual_fill_is_protected_without_another_signal(tmp_path, kind, restart, short):
    app, provider, gateway, monitor = setup(tmp_path)
    if short:
        monitor.proposals.intent["riskAdjustedIntent"]["action"] = "open_short"
    result = await monitor.run_once()
    assert result[0]["status"] == "submitted", result
    entry = dict(gateway.request)
    provider.position_items = [{"symbol": entry["symbol"], "direction": "short" if short else "long", "volume": 1,
        "todayVolume": 1, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    provider.activity["trades"] = [{"tradeId": "entry-fill", "clientOrderId": entry["clientOrderId"],
        "symbol": entry["symbol"], "direction": entry["direction"], "offset": "OPEN", "volume": 1,
        "price": 822.0, "tradeDateTime": int(datetime.now(UTC).timestamp() * 1e9), "tradingDay": provider.day}]
    provider.activity["orders"] = [{**entry, "status": "FINISHED", "volumeLeft": 0}]
    gateway.query_report = gateway.report(entry, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(entry["clientOrderId"], user_id=USER)
    # Protection must not require a new signal or a running strategy.
    app.state.store.update_strategy_draft_status(STRATEGY_ID, user_id=USER,
        expected_status="monitoring", status="paused")
    app.state.autonomy.sync_strategy_protection()
    stops = app.state.autonomy.list_stops(user_id=USER)
    assert len(stops) == 1
    assert stops[0]["triggerPrice"] == (823.0 if short else 821.0)
    assert stops[0]["takeProfitPrice"] == (820.0 if short else 824.0)
    if restart:
        app.state.store.close()
        app = application(tmp_path / "protection.sqlite3", gateway, provider)
        app.state.autonomy.sync_strategy_protection()
        assert len(app.state.autonomy.list_stops(user_id=USER)) == 1
    provider.last = (823.5 if kind == "stop" else 819.5) if short else (820.5 if kind == "stop" else 824.5)
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._check_stop(row)
    assert gateway.submit_count == 2
    assert gateway.request["offset"] == "CLOSETODAY"
    assert gateway.request["volume"] == 1
    await app.state.autonomy._check_stop(row)
    assert gateway.submit_count == 2
    app.state.store.close()


def entry_fill(provider, request, *, trade_id="fill-1", volume=1, price=822.0, time=1):
    trade = {"tradeId": trade_id, "clientOrderId": request["clientOrderId"], "symbol": request["symbol"],
             "direction": request["direction"], "offset": request["offset"], "volume": volume,
             "price": price, "tradeDateTime": time, "tradingDay": provider.day}
    provider.activity["trades"].insert(0, trade)
    return trade


async def filled_entry(tmp_path, *, volume=1):
    app, provider, gateway, monitor = setup(tmp_path)
    monitor.proposals.intent["riskAdjustedIntent"]["volume"] = volume
    assert (await monitor.run_once())[0]["status"] == "submitted"
    request = dict(gateway.request)
    entry_fill(provider, request, volume=volume)
    provider.position_items = [{"symbol": request["symbol"], "direction": "long", "volume": volume,
        "todayVolume": volume, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    provider.activity["orders"] = [{**request, "status": "FINISHED", "volumeLeft": 0}]
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(request["clientOrderId"], user_id=USER)
    app.state.autonomy.sync_strategy_protection()
    return app, provider, gateway, monitor, request


async def test_strategy_distance_is_checked_before_sending(tmp_path):
    app, _, gateway, monitor = setup(tmp_path)
    strategy = app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    strategy["definition"]["riskLimits"]["stopLossTicks"] = 20
    strategy["definition"]["riskLimits"]["perTradeRisk"] = "100"
    app.state.store.connection.execute("UPDATE strategy_drafts SET definition_json = ?", (json.dumps(strategy["definition"]),))
    app.state.store.connection.commit()
    result = await monitor.run_once()
    assert result[0]["status"] == "failed", result
    assert "strategy_trade_budget_exceeded" in result[0]["detail"]
    assert gateway.submit_count == 0
    app.state.store.close()


async def test_manual_close_disarms_old_lot_even_if_new_position_exists(tmp_path):
    app, provider, gateway, _, request = await filled_entry(tmp_path)
    close = {**request, "clientOrderId": "manual-close", "direction": "SELL", "offset": "CLOSETODAY"}
    entry_fill(provider, close, trade_id="manual-fill", price=821, time=2)
    # A later unrelated position must not be closed by this old protection.
    provider.last = 820
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._check_stop(row)
    stop = app.state.autonomy.list_stops(user_id=USER)[0]
    assert stop["remainingVolume"] == 0 and stop["status"] == "CANCELLED"
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_unknown_protective_close_only_queries_original_order(tmp_path):
    app, provider, gateway, _, _ = await filled_entry(tmp_path)
    provider.last = 820
    gateway.submit_error = True
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._check_stop(row)
    unknown = app.state.autonomy.list_stops(user_id=USER)[0]
    assert unknown["status"] == "UNKNOWN"
    gateway.submit_error = False
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await app.state.autonomy._check_stop(row)
    current = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._reconcile_stop(current)
    assert gateway.submit_count == 2
    assert app.state.autonomy.list_stops(user_id=USER)[0]["closeClientOrderId"] == unknown["closeClientOrderId"]
    app.state.store.close()


async def test_a_settlement_pending_strategy_close_is_repriced_through_the_same_entry(tmp_path):
    """#750：策略保护的平仓单等结算价被拒后，价格变化不触发重新规划；重派仍走保护触发的
    同一入口，按当时买一改写同一笔单。"""
    from test_order_protection import retry_due, settlement_pending

    app, provider, gateway, _, _ = await filled_entry(tmp_path)
    state = settlement_pending(gateway)
    provider.last, provider.bid, provider.quote_datetime = 820, 820.0, quote_now()
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._check_stop(row)
    stop = app.state.autonomy.list_stops(user_id=USER)[0]
    assert stop["status"] == "ARMED" and stop["lastError"] == "paper_settlement_pending"
    state["pending"] = False
    retry_due(app)
    provider.last, provider.bid, provider.quote_datetime = 812, 811.5, quote_now()
    await app.state.autonomy.run_once()
    stop = app.state.autonomy.list_stops(user_id=USER)[0]
    assert stop["status"] == "SUBMITTED"
    assert [item["limitPrice"] for item in state["sent"]] == [820.0, 811.5]
    assert {item["clientOrderId"] for item in state["sent"]} == {stop["closeClientOrderId"]}
    app.state.store.close()


async def test_a_report_anomaly_on_the_protective_close_reads_unknown_not_submitted(tmp_path):
    """#528：止损平仓单读回“回报异常”时，对账不再兜底成 SUBMITTED（好像保护单还挂在场上）——
    与首次分类同一张表：UNKNOWN + stop_close_result_unknown，只查询原单，绝不重发。"""
    app, provider, gateway, _, _request = await filled_entry(tmp_path)
    provider.last = 820
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._check_stop(row)
    assert app.state.autonomy.list_stops(user_id=USER)[0]["status"] == "SUBMITTED"
    gateway.query_report = gateway.report(dict(gateway.request), volume_left=99)  # 回报异常
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._reconcile_stop(row)
    anomaly = app.state.autonomy.list_stops(user_id=USER)[0]
    assert anomaly["status"] == "UNKNOWN" and anomaly["lastError"] == "stop_close_result_unknown"
    assert gateway.submit_count == 2  # 进场 + 平仓；异常只查询原单，绝不重发
    app.state.store.close()


async def test_partial_entry_cancels_unfilled_remainder_before_exit(tmp_path):
    app, provider, gateway, monitor = setup(tmp_path)
    monitor.proposals.intent["riskAdjustedIntent"]["volume"] = 2
    assert (await monitor.run_once())[0]["status"] == "submitted"
    request = dict(gateway.request)
    entry_fill(provider, request)
    provider.position_items = [{"symbol": request["symbol"], "direction": "long", "volume": 1,
        "todayVolume": 1, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    provider.activity["orders"] = [{**request, "status": "ALIVE", "volumeLeft": 1}]
    app.state.autonomy.sync_strategy_protection()
    provider.last = 820
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    gateway.cancel_report = gateway.report(request, status="FINISHED", volume_left=1)
    await app.state.autonomy._check_stop(row)
    assert gateway.cancel_count == 1 and gateway.submit_count == 1
    provider.activity["orders"][0].update({"status": "FINISHED", "volumeLeft": 1})
    await app.state.autonomy._check_stop(row)
    assert gateway.submit_count == 2 and gateway.request["volume"] == 1
    assert gateway.request["offset"] == "CLOSETODAY"
    app.state.store.close()


async def test_partial_close_continues_only_remaining_volume_and_records_gross_pnl(tmp_path):
    app, provider, gateway, monitor, request = await filled_entry(tmp_path, volume=2)
    provider.last = 820
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._check_stop(row)
    first_close = dict(gateway.request)
    entry_fill(provider, first_close, trade_id="close-fill-1", price=820, volume=1, time=2)
    provider.activity["orders"].insert(0, {**first_close, "status": "FINISHED", "volumeLeft": 1})
    provider.position_items[0].update({"volume": 1, "todayVolume": 1})
    gateway.query_report = gateway.report(first_close, status="FINISHED", volume_left=1)
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._reconcile_stop(row)
    app.state.autonomy.sync_strategy_protection()
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    assert row["remaining_volume"] == 1 and row["status"] == "ARMED"
    await app.state.autonomy._check_stop(row)
    assert gateway.submit_count == 3 and gateway.request["volume"] == 1
    assert gateway.request["clientOrderId"] != first_close["clientOrderId"]
    strategy = app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    state, _, _ = monitor._state(strategy=strategy, account=provider.account_summary(""), activity=provider.activity)
    assert state["consecutiveLosses"] == 1
    # Reordered/repeated snapshots cannot count the same close twice.
    state, _, _ = monitor._state(strategy=strategy, account=provider.account_summary(""), activity=provider.activity)
    assert state["consecutiveLosses"] == 1
    app.state.store.close()


async def test_emergency_does_not_rearm_late_fills_or_partial_exit(tmp_path):
    app, provider, gateway, _, request = await filled_entry(tmp_path, volume=2)
    provider.last = 820
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._check_stop(row)
    close = dict(gateway.request)
    gateway.cancel_report = gateway.report(close, status="FINISHED", volume_left=1)
    await app.state.autonomy.deactivate(user_id=USER, emergency=True)
    entry_fill(provider, close, trade_id="close-fill", price=820, volume=1, time=2)
    provider.activity["orders"].insert(0, {**close, "status": "FINISHED", "volumeLeft": 1})
    gateway.query_report = gateway.cancel_report
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._reconcile_stop(row)
    session = app.state.broker.create_session("local", "native", USER)
    app.state.autonomy.resume_runtime_access(user_id=USER, session_id=session.session_id, symbol=request["symbol"])
    app.state.autonomy.sync_strategy_protection()
    await app.state.autonomy._check_stop(row)
    assert gateway.submit_count == 2
    assert json.loads(app.state.store.connection.execute("SELECT strategy_metadata_json FROM ai_stop_orders").fetchone()[0])["disarmed"]
    app.state.store.close()


async def test_batch_closed_losses_pause_but_floating_pnl_does_not(tmp_path):
    app, provider, gateway, monitor = setup(tmp_path)
    strategy = app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    account = provider.account_summary("")
    for index in range(3):
        account["todayNetPnl"] = -100 * index
        provider.activity["trades"].insert(0, {"tradeId": str(index)})
        state, _, _ = monitor._state(strategy=strategy, account=account, activity=provider.activity)
        assert state["consecutiveLosses"] == 0
    for index, pnl in enumerate([-10, -20, -30, 10]):
        app.state.store.connection.execute("""INSERT INTO strategy_fill_events
            (user_id, event_id, trade_time, trading_day, client_order_id, payload_json, settled, settled_day)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (USER, str(index), index, account["tradingDay"], f"closed-{index}",
            json.dumps({"realizedGrossPnl": {STRATEGY_ID: str(pnl)}}), index + 1, account["tradingDay"]))
    app.state.store.connection.commit()
    result = await monitor.run_once()
    assert result[0]["status"] == "paused"
    assert gateway.submit_count == 0
    app.state.store.close()


async def test_warmup_keeps_failure_history_and_success_clears_it(tmp_path):
    from trader_runtime.proposals import ProposalConflict
    app, _, _, monitor = setup(tmp_path)
    original = monitor.proposals.preview_strategy_intent
    def broken(**kwargs):
        raise RuntimeError("provider_broken")
    monitor.proposals.preview_strategy_intent = broken
    assert (await monitor.run_once())[0]["status"] == "error"
    app.state.store.connection.execute("UPDATE strategy_monitor_states SET next_retry_at = NULL")
    app.state.store.connection.commit()
    def warming(**kwargs):
        raise ProposalConflict("strategy_bars_insufficient")
    monitor.proposals.preview_strategy_intent = warming
    assert (await monitor.run_once())[0]["status"] == "waiting"
    assert app.state.store.strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER)["consecutiveFailures"] == 1
    monitor.proposals.preview_strategy_intent = original
    assert (await monitor.run_once())[0]["status"] == "submitted"
    assert app.state.store.strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER)["consecutiveFailures"] == 0
    app.state.store.close()


async def test_connecting_waits_without_counting_failure(tmp_path):
    app, provider, gateway, monitor = setup(tmp_path)
    provider.runtime_status = lambda: {"marketDataStatus": "connecting", "accountStatus": "connecting"}
    assert (await monitor.run_once())[0]["status"] == "waiting"
    assert gateway.submit_count == 0
    assert app.state.store.strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER) is None
    app.state.store.close()


async def test_protection_rolls_to_historical_bucket(tmp_path):
    app, provider, gateway, _, _ = await filled_entry(tmp_path)
    provider.day = "2026-08-26"
    provider.position_items[0].update({"todayVolume": 0, "historicalVolume": 1})
    provider.last = 820
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._check_stop(row)
    assert gateway.submit_count == 2
    assert gateway.request["offset"] == "CLOSE"
    app.state.store.close()


async def test_old_schema_migration_preserves_existing_stop_and_authorization(tmp_path):
    import sqlite3

    from test_execution_recovery import make_stop
    app, provider, gateway, _ = setup(tmp_path)
    provider.position_items = NormalProvider().position_items
    stop = await make_stop(app)
    app.state.store.close()
    path = tmp_path / "protection.sqlite3"
    with sqlite3.connect(path) as db:
        # An old database had no 改动留痕 triggers (they name the columns dropped below); startup reinstalls them.
        for kind in ("insert", "update", "delete"):
            db.execute(f"DROP TRIGGER state_changes_ai_stop_orders_{kind}")
        db.execute("ALTER TABLE tqkq_sim_order_links DROP COLUMN protection_json")
        for column in ("take_profit_price", "strategy_metadata_json", "remaining_volume"):
            db.execute(f"ALTER TABLE ai_stop_orders DROP COLUMN {column}")
        db.execute("DROP TABLE strategy_fill_events")
    restarted = application(path, gateway, provider)
    assert restarted.state.autonomy.list_stops(user_id=USER)[0]["stopId"] == stop["stopId"]
    assert restarted.state.autonomy.list_stops(user_id=USER)[0]["status"] == "ARMED"
    restarted.state.store.close()


async def test_partial_exit_waits_for_delayed_fill_evidence(tmp_path):
    app, provider, gateway, _, _ = await filled_entry(tmp_path, volume=2)
    provider.last = 820
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._check_stop(row)
    close = dict(gateway.request)
    gateway.query_report = gateway.report(close, status="FINISHED", volume_left=1)
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._reconcile_stop(row)
    app.state.autonomy.sync_strategy_protection()
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    assert row["status"] == "PARTIAL"
    await app.state.autonomy._check_stop(row)
    assert gateway.submit_count == 2
    entry_fill(provider, close, trade_id="delayed-close-fill", price=820, volume=1, time=2)
    provider.activity["orders"].insert(0, {**close, "status": "FINISHED", "volumeLeft": 1})
    provider.position_items[0].update({"volume": 1, "todayVolume": 1})
    app.state.autonomy.sync_strategy_protection()
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._check_stop(row)
    assert gateway.submit_count == 3 and gateway.request["volume"] == 1
    app.state.store.close()


@pytest.mark.parametrize("late_fault", [False, True])
async def test_contract_history_failure_is_isolated_and_settlements_keep_time_order(tmp_path, late_fault):
    from test_simulation_orders import prepare_only, proposal
    app, provider, _, _, gold = await filled_entry(tmp_path)
    db = app.state.store.connection
    protection = db.execute("SELECT protection_json FROM tqkq_sim_order_links WHERE client_order_id = ?",
        (gold["clientOrderId"],)).fetchone()[0]
    # A second persisted strategy order whose exchange history arrives later.
    silver_link = prepare_only(app, proposal(app, turn="silver-order"))
    silver = {**silver_link["request"], "symbol": "SHFE.ag2612"}
    db.execute("UPDATE tqkq_sim_order_links SET request_json = ?, protection_json = ? WHERE client_order_id = ?",
        (json.dumps(silver), protection, silver["clientOrderId"]))
    db.commit()
    provider.position_items.append({**provider.position_items[0], "symbol": silver["symbol"]})
    entry_fill(provider, silver, trade_id="silver-entry", time=0)
    provider.activity["orders"].append({**silver, "status": "FINISHED", "volumeLeft": 0})
    assert app.state.autonomy.sync_strategy_protection() == {}
    stops = {item["symbol"]: item for item in app.state.autonomy.list_stops(user_id=USER)}
    assert stops[gold["symbol"]]["remainingVolume"] == stops[silver["symbol"]]["remainingVolume"] == 1
    # An unrelated manual OPEN must not advance the gold strategy's cursor.
    entry_fill(provider, {**gold, "clientOrderId": "manual-open"}, trade_id="manual-new", time=500)
    assert app.state.autonomy.sync_strategy_protection() == {}
    closes = []
    for request, timestamp in ((gold, 2), (silver, 3)):
        close = {**request, "clientOrderId": "close-" + request["symbol"], "offset": "CLOSETODAY", "direction": "SELL"}
        closes.append(close)
        entry_fill(provider, close, trade_id="close", price=821, time=timestamp)
        provider.activity["orders"].append({**close, "status": "FINISHED", "volumeLeft": 0})
    if late_fault:
        entry_fill(provider, {**closes[0], "clientOrderId": "late-gold-close"}, trade_id="late", time=0)
    failures = app.state.autonomy.sync_strategy_protection()
    stops = {item["symbol"]: item for item in app.state.autonomy.list_stops(user_id=USER)}
    assert stops[silver["symbol"]]["remainingVolume"] == 0
    assert not stops[silver["symbol"]]["lastError"]
    if late_fault:
        assert failures == {(USER, gold["symbol"]): "strategy_fill_history_out_of_order"}
        assert stops[gold["symbol"]]["remainingVolume"] == 1
        assert stops[gold["symbol"]]["lastError"]
    else:
        assert failures == {} and stops[gold["symbol"]]["remainingVolume"] == 0
        settlements = [row[0] for row in db.execute("SELECT client_order_id FROM strategy_fill_events WHERE client_order_id LIKE 'close-%' ORDER BY settled")]
        assert settlements == [item["clientOrderId"] for item in closes]
    app.state.store.close()


async def test_revoking_interactive_identity_does_not_disarm_background_protection(tmp_path):
    app, provider, gateway, _, _ = await filled_entry(tmp_path)
    app.state.app_capabilities.forget_user(USER)
    provider.last = 820
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._check_stop(row)
    assert gateway.submit_count == 2
    assert gateway.request["offset"] == "CLOSETODAY"
    app.state.store.close()


@pytest.mark.parametrize("anomaly", ["REPORT_INVALID", "IDENTITY_MISMATCH", "UNKNOWN", "ALIVE", "FINISHED"])
@pytest.mark.parametrize("same_symbol", [True, False])
async def test_exit_reconciles_anomalous_original_close(tmp_path, anomaly, same_symbol):
    app, provider, gateway, _, _ = await filled_entry(tmp_path)
    provider.last = 820
    gateway.submit_error = anomaly == "UNKNOWN"
    row = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
    await app.state.autonomy._check_stop(row)
    close = dict(gateway.request)
    if anomaly == "REPORT_INVALID":
        gateway.query_report = gateway.report(close, volume_left=close["volume"] + 1)
    elif anomaly == "IDENTITY_MISMATCH":
        gateway.query_report = gateway.report(close, symbol="SHFE.rb2610")
    elif anomaly == "UNKNOWN":
        gateway.query_report = {"ordersInitialized": True, "found": False}
    else:
        gateway.query_report = gateway.report(close, status=anomaly)
    await app.state.simulation_orders.query(close["clientOrderId"], user_id=USER)
    before = app.state.simulation_orders.get(close["clientOrderId"], user_id=USER)
    if anomaly in {"REPORT_INVALID", "IDENTITY_MISMATCH"}:
        assert before["remoteStatus"] == anomaly
    # An ordinary later report must not erase the anomaly; activity still lacks this close.
    gateway.query_report = gateway.report(close)
    queries, submissions = gateway.query_count, gateway.submit_count
    checked_row = dict(row)
    if not same_symbol:
        checked_row["symbol"] = "DCE.m2609"
    ready = await app.state.autonomy._strategy_exit_ready(checked_row)
    assert ready is (not same_symbol or anomaly == "FINISHED")
    unresolved = anomaly in {"REPORT_INVALID", "IDENTITY_MISMATCH", "UNKNOWN"}
    assert gateway.query_count == queries + int(same_symbol and unresolved)
    assert gateway.submit_count == submissions
    after = app.state.simulation_orders.get(close["clientOrderId"], user_id=USER)
    assert after["clientOrderId"] == close["clientOrderId"]
    if anomaly in {"REPORT_INVALID", "IDENTITY_MISMATCH"}:
        assert after["remoteStatus"] == anomaly
    app.state.store.close()


# -- 策略只管自己开的仓 (所有者 2026-10-08): the strategy's close spends its own row -------------------


async def test_a_strategy_close_spends_its_own_protection_row_not_the_manual_one(tmp_path):
    """策略的平仓（确认卡的平仓单挂在 attempt 上，不是执行 leg）先扣这个策略自己的保护行；
    同合约更早的手动保护行手数不动。"""
    app, provider, gateway, monitor, request = await filled_entry(tmp_path)
    store = app.state.store
    # A manual lot protected earlier on the same contract (an ordinary card's own stop, no strategyId).
    store.connection.execute("PRAGMA foreign_keys = OFF")
    store.connection.execute(
        "INSERT INTO ai_stop_orders (id,user_id,authorization_id,session_id,symbol,position_direction,position_bucket,"
        "volume,trigger_price,price_intent,status,strategy_metadata_json,remaining_volume,created_at,updated_at)"
        " VALUES ('manual-stop',?,'auth','s','SHFE.au2612','long','today',1,'810','passive','ARMED',?,1,?,?)",
        (USER, '{"fixedStopPrice":"810","priceTick":"0.5","contractMultiplier":"1000",'
                '"entryPrice":"822","entryVolume":1,"entryTradingDay":"2026-08-25","entryTime":1}',
         "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"))
    # 出卡确认: the confirmed close card's order rides on the attempt row, not on an execution leg.
    account_id = store.default_trading_account_id
    now = datetime.now(UTC).isoformat()
    store.connection.execute("""INSERT INTO strategy_execution_attempts (id, strategy_id, user_id, intent_id,
        signal_bar_time, trading_day, action, volume, status, client_order_id, trading_account_id, created_at, updated_at)
        VALUES ('close-attempt', ?, ?, 'i', 'bar-c', ?, 'close_long', 1, 'SUBMITTED', 'close-1', ?, ?, ?)""",
        (STRATEGY_ID, USER, provider.day, account_id, now, now))
    store.connection.commit()
    strategy = store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    assert monitor.held_position(strategy) == {"direction": "long", "volume": 1, "legs": [("SHFE.au2612", "long", 1)]}
    close = {**request, "clientOrderId": "close-1", "direction": "SELL", "offset": "CLOSETODAY"}
    entry_fill(provider, close, trade_id="strategy-close", price=821, time=2)
    provider.activity["orders"].append({**close, "status": "FINISHED", "volumeLeft": 0})
    app.state.autonomy.sync_strategy_protection()
    rows = {row["id"]: row for row in store.connection.execute(
        "SELECT id, remaining_volume, status FROM ai_stop_orders").fetchall()}
    # 策略那手：保护释放；手动那手：手数和状态都不动。
    strategy_row = next(row for row in rows.values() if row["id"] != "manual-stop")
    assert strategy_row["remaining_volume"] == 0 and strategy_row["status"] == "CANCELLED"
    assert rows["manual-stop"]["remaining_volume"] == 1 and rows["manual-stop"]["status"] == "ARMED"
    app.state.store.close()


# -- 停止并平仓的归属 (#654): the card carries its strategy's attempt, the fill spends only its own rows --


def _extra_protection_row(store, *, row_id: str, strategy_id: str | None, day: str,
                          volume: int = 1, direction: str = "long", bucket: str = "today",
                          status: str = "ARMED",
                          created: str = "2020-01-01T00:00:00+00:00") -> None:
    """A protection row beside the strategy's own: an earlier manual lot (no strategyId), or
    another strategy's lot — the rows #654's stop-close must never spend."""
    now = datetime.now(UTC).isoformat()
    if strategy_id:
        metadata = {"strategyId": strategy_id, "entryPrice": "822", "entryVolume": volume,
                    "entryTradingDay": day, "entryTime": 1, "timeframeSeconds": 300,
                    "contractMultiplier": "1000", "positionKey": row_id}
    else:
        metadata = {"fixedStopPrice": "810", "priceTick": "0.5", "contractMultiplier": "1000",
                    "entryPrice": "822", "entryVolume": volume, "entryTradingDay": day, "entryTime": 1}
    store.connection.execute("PRAGMA foreign_keys = OFF")
    store.connection.execute(
        "INSERT INTO ai_stop_orders (id,user_id,authorization_id,session_id,symbol,position_direction,position_bucket,"
        "volume,trigger_price,price_intent,status,strategy_metadata_json,remaining_volume,created_at,updated_at)"
        " VALUES (?,?,'auth','s','SHFE.au2612',?,?,?, '810','passive',?, ?,?,?,?)",
        (row_id, USER, direction, bucket, volume, status, json.dumps(metadata), volume, created, now))
    store.connection.commit()


def _stop_rows(store) -> dict[str, Any]:
    return {row["id"]: row for row in store.connection.execute(
        "SELECT id, remaining_volume, status FROM ai_stop_orders").fetchall()}


async def _stop_close_card(app, provider, *, session_id: str) -> dict[str, Any]:
    """停止并平仓 on the (already stopped) strategy: the real card path, attribution included
    (the app's own monitor, whose proposals service makes real cards)."""
    store = app.state.store
    store.connection.execute("UPDATE strategy_drafts SET status = 'ready' WHERE id = ?", (STRATEGY_ID,))
    store.connection.commit()
    provider.quote_datetime = quote_now()
    outcome = await app.state.strategy_monitor.propose_close_after_stop(
        store.get_strategy_draft(STRATEGY_ID, user_id=USER), session_id=session_id)
    assert outcome["cards"], outcome
    return outcome["cards"][0]


async def _confirm_card(app, card: dict[str, Any], key: str):
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local",
                                  headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        app.state.data_provider.quote_datetime = quote_now()
        return await client.post(f"/api/proposals/{card['proposalId']}/confirm", json={
            "approvalId": card["approvalId"], "proposalVersion": card["version"], "idempotencyKey": key})


async def test_stop_close_spends_only_its_own_row_and_survives_restart(tmp_path):
    """#654 主链路：同账户同合约同今仓桶里手动保护（较早）+ 策略保护（较晚），停止并平仓 → 确认 →
    成交 1 手 → 同步：只扣策略自己的保护行，手动行仍 1 手 ARMED，held_position 归零；
    重复同步、重启后再同步，结果都不变。"""
    app, provider, gateway, monitor, _ = await filled_entry(tmp_path)
    store = app.state.store
    _extra_protection_row(store, row_id="manual-stop", strategy_id=None, day=provider.day)
    provider.position_items = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 2,
                                "todayVolume": 2, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    session = app.state.broker.create_session("local", "native", USER)
    card = await _stop_close_card(app, provider, session_id=session.session_id)
    assert (card["volume"], card.get("positionBucket")) == (1, "today")
    assert card["strategySource"]["purpose"] == "stop_close" and card["strategySource"]["attemptId"]
    attempt = store.connection.execute("SELECT * FROM strategy_execution_attempts WHERE id = ?",
                                       (card["strategySource"]["attemptId"],)).fetchone()
    assert (attempt["action"], attempt["status"]) == ("stop_close", "SKIPPED")
    confirmed = await _confirm_card(app, card, "stop-close-main")
    assert confirmed.status_code == 200, confirmed.text
    client_order_id = confirmed.json()["clientOrderId"]
    attempt = store.connection.execute("SELECT * FROM strategy_execution_attempts WHERE id = ?",
                                       (card["strategySource"]["attemptId"],)).fetchone()
    assert attempt["status"] == "SUBMITTED" and client_order_id in str(attempt["client_order_id"])
    assert gateway.submit_count == 2   # the entry + the confirmed stop-close
    close = dict(gateway.request)
    assert (close["direction"], close["offset"], close["volume"]) == ("SELL", "CLOSETODAY", 1)
    entry_fill(provider, close, trade_id="stop-close-fill", price=821, time=2)
    provider.activity["orders"].append({**close, "status": "FINISHED", "volumeLeft": 0})
    app.state.autonomy.sync_strategy_protection()
    rows = _stop_rows(store)
    strategy_row = next(row for row in rows.values() if row["id"] != "manual-stop")
    assert strategy_row["remaining_volume"] == 0 and strategy_row["status"] == "CANCELLED"
    assert rows["manual-stop"]["remaining_volume"] == 1 and rows["manual-stop"]["status"] == "ARMED"
    assert monitor.held_position(store.get_strategy_draft(STRATEGY_ID, user_id=USER))["volume"] == 0
    # 重复同步：event 去重，结果不变。
    app.state.autonomy.sync_strategy_protection()
    rows = _stop_rows(store)
    assert rows["manual-stop"]["remaining_volume"] == 1 and rows["manual-stop"]["status"] == "ARMED"
    assert next(row for row in rows.values() if row["id"] != "manual-stop")["remaining_volume"] == 0
    # 重启（重开数据库）后再同步：归属与结果都不变。
    store.close()
    app = application(tmp_path / "protection.sqlite3", gateway, provider)
    app.state.autonomy.sync_strategy_protection()
    rows = _stop_rows(app.state.store)
    assert rows["manual-stop"]["remaining_volume"] == 1 and rows["manual-stop"]["status"] == "ARMED"
    strategy_row = next(row for row in rows.values() if row["id"] != "manual-stop")
    assert strategy_row["remaining_volume"] == 0 and strategy_row["status"] == "CANCELLED"
    monitor = app.state.accounts.default.strategy_monitor
    assert monitor.held_position(
        app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER))["volume"] == 0
    app.state.store.close()


async def test_stop_close_partial_fill_finishes_without_touching_the_manual_row(tmp_path):
    """partial fill：先成交 1 手只扣策略行 1 手（手动行不动），第二笔成交后策略行清零；
    每一步重复同步都幂等。"""
    app, provider, gateway, _, _ = await filled_entry(tmp_path, volume=2)
    store = app.state.store
    _extra_protection_row(store, row_id="manual-stop", strategy_id=None, day=provider.day)
    provider.position_items = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 3,
                                "todayVolume": 3, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    session = app.state.broker.create_session("local", "native", USER)
    card = await _stop_close_card(app, provider, session_id=session.session_id)
    assert (card["volume"], card.get("positionBucket")) == (2, "today")
    confirmed = await _confirm_card(app, card, "stop-close-partial")
    assert confirmed.status_code == 200, confirmed.text
    close = dict(gateway.request)
    entry_fill(provider, close, trade_id="partial-1", volume=1, price=821, time=2)
    provider.activity["orders"].append({**close, "status": "ALIVE", "volumeLeft": 1})
    app.state.autonomy.sync_strategy_protection()
    rows = _stop_rows(store)
    strategy_row = next(row for row in rows.values() if row["id"] != "manual-stop")
    assert strategy_row["remaining_volume"] == 1 and strategy_row["status"] == "ARMED"
    assert rows["manual-stop"]["remaining_volume"] == 1 and rows["manual-stop"]["status"] == "ARMED"
    app.state.autonomy.sync_strategy_protection()   # 重复同步：不变
    rows = _stop_rows(store)
    assert next(row for row in rows.values() if row["id"] != "manual-stop")["remaining_volume"] == 1
    entry_fill(provider, close, trade_id="partial-2", volume=1, price=821, time=3)
    provider.activity["orders"] = [order for order in provider.activity["orders"]
                                   if order.get("clientOrderId") != close["clientOrderId"]]
    provider.activity["orders"].append({**close, "status": "FINISHED", "volumeLeft": 0})
    app.state.autonomy.sync_strategy_protection()
    rows = _stop_rows(store)
    strategy_row = next(row for row in rows.values() if row["id"] != "manual-stop")
    assert strategy_row["remaining_volume"] == 0 and strategy_row["status"] == "CANCELLED"
    assert rows["manual-stop"]["remaining_volume"] == 1 and rows["manual-stop"]["status"] == "ARMED"
    app.state.store.close()


async def test_stop_close_of_one_strategy_spares_the_other_strategy_and_manual_rows(tmp_path):
    """两个策略共仓：停止其中一个，成交只扣它的保护行；另一个策略的行和手动行都不动。"""
    app, provider, gateway, _, _ = await filled_entry(tmp_path)
    store = app.state.store
    store.save_strategy_draft(strategy_id="strategy-other", user_id=USER, source_session_id="s",
                              name="另一个策略 · SHFE.au2612", symbol="SHFE.au2612", version=1,
                              status="ready", definition={"timeframeSeconds": 300, "riskLimits": {}},
                              definition_digest="b" * 64)
    _extra_protection_row(store, row_id="other-strategy-stop", strategy_id="strategy-other", day=provider.day)
    _extra_protection_row(store, row_id="manual-stop", strategy_id=None, day=provider.day)
    provider.position_items = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 3,
                                "todayVolume": 3, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    session = app.state.broker.create_session("local", "native", USER)
    card = await _stop_close_card(app, provider, session_id=session.session_id)
    confirmed = await _confirm_card(app, card, "stop-close-two-strategies")
    assert confirmed.status_code == 200, confirmed.text
    close = dict(gateway.request)
    entry_fill(provider, close, trade_id="two-strategies-fill", price=821, time=2)
    provider.activity["orders"].append({**close, "status": "FINISHED", "volumeLeft": 0})
    app.state.autonomy.sync_strategy_protection()
    rows = _stop_rows(store)
    strategy_row = next(row for row in rows.values() if row["id"] not in {"manual-stop", "other-strategy-stop"})
    assert strategy_row["remaining_volume"] == 0 and strategy_row["status"] == "CANCELLED"
    assert rows["other-strategy-stop"]["remaining_volume"] == 1 and rows["other-strategy-stop"]["status"] == "ARMED"
    assert rows["manual-stop"]["remaining_volume"] == 1 and rows["manual-stop"]["status"] == "ARMED"
    app.state.store.close()


async def test_stop_close_fill_observed_inside_dispatch_is_already_attributed(tmp_path):
    """dispatch 窗口内提前观察到成交：确认事务里委托已挂到 stop_close attempt，同步在 dispatch
    进行中就只扣策略自己的行（#652 的竞态在停止平仓上不复现）。"""
    app, provider, gateway, _, _ = await filled_entry(tmp_path)
    store = app.state.store
    _extra_protection_row(store, row_id="manual-stop", strategy_id=None, day=provider.day)
    provider.position_items = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 2,
                                "todayVolume": 2, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    session = app.state.broker.create_session("local", "native", USER)
    card = await _stop_close_card(app, provider, session_id=session.session_id)

    inner_submit = gateway.submit_order_once

    async def observing_submit(request):
        result = await inner_submit(request)
        if request["offset"] == "CLOSETODAY":
            # The fill lands while dispatch is still in flight.
            entry_fill(provider, request, trade_id="in-dispatch-fill", price=821, time=2)
            provider.activity["orders"].append({**request, "status": "FINISHED", "volumeLeft": 0})
            app.state.autonomy.sync_strategy_protection()
        return result

    gateway.submit_order_once = observing_submit
    confirmed = await _confirm_card(app, card, "stop-close-in-dispatch")
    assert confirmed.status_code == 200, confirmed.text
    rows = _stop_rows(store)
    strategy_row = next(row for row in rows.values() if row["id"] != "manual-stop")
    assert strategy_row["remaining_volume"] == 0 and strategy_row["status"] == "CANCELLED"
    assert rows["manual-stop"]["remaining_volume"] == 1 and rows["manual-stop"]["status"] == "ARMED"
    attempt = store.connection.execute("SELECT * FROM strategy_execution_attempts WHERE id = ?",
                                       (card["strategySource"]["attemptId"],)).fetchone()
    assert attempt["status"] == "SUBMITTED"
    app.state.store.close()


async def test_stop_close_card_refused_when_the_strategy_position_already_left(tmp_path):
    """确认前策略自己的仓已被平掉（行耗尽）而账户还剩手动手数：明确拒绝，不拿账户剩余仓代平。"""
    app, provider, gateway, _, _ = await filled_entry(tmp_path)
    store = app.state.store
    _extra_protection_row(store, row_id="manual-stop", strategy_id=None, day=provider.day)
    provider.position_items = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 2,
                                "todayVolume": 2, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    session = app.state.broker.create_session("local", "native", USER)
    card = await _stop_close_card(app, provider, session_id=session.session_id)
    # 策略那手已被止损平掉；账户的 2 手今仓还在（手动 1 手 + 剩下的 1 手）。
    store.connection.execute(
        """UPDATE ai_stop_orders SET remaining_volume = 0, status = 'CANCELLED'
           WHERE json_extract(strategy_metadata_json, '$.strategyId') = ?""", (STRATEGY_ID,))
    store.connection.commit()
    refused = await _confirm_card(app, card, "stop-close-gone")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "stop_close_position_gone"
    assert gateway.submit_count == 1   # 只有原来的开仓，平仓没有发
    record = store.get(card["proposalId"])
    assert record["status"] == "approval_required"
    assert record["payload"]["confirmBlocked"]["code"] == "stop_close_position_gone"
    assert _stop_rows(store)["manual-stop"]["remaining_volume"] == 1
    app.state.store.close()


@pytest.mark.parametrize("fault", ["rolled", "in_flight"])
async def test_stop_close_card_refused_when_the_bucket_rolled_or_a_close_is_in_flight(
        tmp_path, fault):
    """今昨桶变化（隔日，卡上的平今不再匹配）或在途/UNKNOWN 的保护性平仓：确认被拒，卡写明原因。"""
    app, provider, gateway, _, _ = await filled_entry(tmp_path)
    store = app.state.store
    _extra_protection_row(store, row_id="manual-stop", strategy_id=None, day=provider.day)
    provider.position_items = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 2,
                                "todayVolume": 2, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    session = app.state.broker.create_session("local", "native", USER)
    card = await _stop_close_card(app, provider, session_id=session.session_id)
    if fault == "rolled":
        provider.day = "2026-08-26"   # 隔日：策略那手成了昨仓
        expected = "stop_close_position_changed"
    else:
        store.connection.execute(
            """UPDATE ai_stop_orders SET status = 'SUBMITTED'
               WHERE json_extract(strategy_metadata_json, '$.strategyId') = ?""", (STRATEGY_ID,))
        store.connection.commit()
        expected = "stop_close_protection_in_flight"
    refused = await _confirm_card(app, card, f"stop-close-{fault}")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == expected
    assert gateway.submit_count == 1
    assert store.get(card["proposalId"])["payload"]["confirmBlocked"]["code"] == expected
    assert _stop_rows(store)["manual-stop"]["remaining_volume"] == 1
    app.state.store.close()


async def test_legacy_stop_close_card_without_attribution_is_invalidated_and_manual_close_still_works(tmp_path):
    """归属修复前出的旧停止平仓卡（只有原因文本、没有 strategySource）：确认时明确失效并提示重新
    停止并平仓，绝不从 reason / 会话 / 最老保护行猜归属。正控：普通手动平仓卡不受影响。"""
    app, provider, gateway, _, _ = await filled_entry(tmp_path)
    store = app.state.store
    _extra_protection_row(store, row_id="manual-stop", strategy_id=None, day=provider.day)
    provider.position_items = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 2,
                                "todayVolume": 2, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    session = app.state.broker.create_session("local", "native", USER)
    provider.quote_datetime = quote_now()
    legacy = app.state.proposals.create_trade(
        session.session_id, "turn-legacy-stop-close", symbol="SHFE.au2612", side="sell_close",
        volume=1, price_intent="aggressive", reason="停止策略「已验证模拟策略」后平掉它的持仓",
        position_bucket="today")
    refused = await _confirm_card(app, legacy, "legacy-stop-close")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "stop_close_card_unattributed"
    assert gateway.submit_count == 1
    assert store.get(legacy["proposalId"])["payload"]["confirmBlocked"]["code"] == "stop_close_card_unattributed"
    # 正控：普通手动平仓卡（不同原因、无 strategySource）照常确认发单。
    provider.quote_datetime = quote_now()
    manual = app.state.proposals.create_trade(
        session.session_id, "turn-manual-close", symbol="SHFE.au2612", side="sell_close",
        volume=1, price_intent="aggressive", reason="手动平掉 1 手", position_bucket="today")
    confirmed = await _confirm_card(app, manual, "manual-close-control")
    assert confirmed.status_code == 200, confirmed.text
    assert gateway.submit_count == 2
    app.state.store.close()


# -- 停止并平仓按实例自己的账户出卡 (#1011) ----------------------------------------------------------


OTHER_ACCOUNT = "paper:other"


def _other_account(app) -> Gateway:
    """A second assembled account (the conversation's, after a switch) that holds nothing."""
    from trader_runtime.simulation_orders import SimulationOrderService
    from trader_runtime.wiring.accounts import AccountServices
    store, default_orders = app.state.store, app.state.simulation_orders
    store.ensure_trading_account(OTHER_ACCOUNT, execution_owner_user_id=USER, provider_kind="paper",
                                 display_name="本站模拟", sidecar_url=None, grants=[(USER, "owner")])
    other_provider, other_gateway = Provider(), Gateway()
    other_provider.mode = "paper"   # the conversation's account reads 本站模拟账户, the instance's 快期模拟账户
    other_orders = SimulationOrderService(
        store=store, data_provider=other_provider, gateway=other_gateway, policies=default_orders.policies,
        instruments=default_orders.instruments, trading_account_id=OTHER_ACCOUNT)
    app.state.accounts.add(AccountServices(account_id=OTHER_ACCOUNT, provider=other_provider,
                                           simulation_orders=other_orders, autonomy=app.state.autonomy))
    return other_gateway


async def test_stop_close_card_acts_on_the_instance_account_after_the_conversation_switched(tmp_path):
    """#1011：策略在 A 账户开了仓，它的对话后来切到了 B 账户（B 没有这个仓）。停止并平仓仍按实例
    自己的 A 账户出卡（以前按对话当前账户出卡 → close_position_not_found、不出卡），确认也只在 A 下单。"""
    app, provider, gateway, _, _ = await filled_entry(tmp_path)
    store = app.state.store
    other_gateway = _other_account(app)
    session = app.state.broker.create_session("local", "native", USER)
    store.set_session_trading_account(session.session_id, OTHER_ACCOUNT, user_id=USER)
    card = await _stop_close_card(app, provider, session_id=session.session_id)
    assert (card["symbol"], card["side"], card["volume"], card.get("positionBucket")) == (
        "SHFE.au2612", "sell_close", 1, "today")
    instance_account = store.get_strategy_draft(STRATEGY_ID, user_id=USER)["tradingAccountId"]
    row = store.connection.execute("SELECT trading_account_id FROM proposals WHERE id = ?",
                                   (card["proposalId"],)).fetchone()
    assert row["trading_account_id"] == instance_account != OTHER_ACCOUNT
    assert card["accountOverride"]["accountId"] == instance_account   # the card says it is not the chat's account
    assert card["accountLabel"] == "快期模拟账户"   # the instance account's label, which the client shows when it differs
    confirmed = await _confirm_card(app, card, "stop-close-instance-account")
    assert confirmed.status_code == 200, confirmed.text
    assert gateway.submit_count == 2 and other_gateway.submit_count == 0   # never sent to the other account
    assert (gateway.request["direction"], gateway.request["offset"]) == ("SELL", "CLOSETODAY")
    app.state.store.close()


async def test_stop_close_without_a_card_says_so_in_one_readable_sentence(tmp_path, caplog):
    """#1011：出不了平仓卡时提示是一句能照着做的中文（不单独露内部代码），代码留在日志里；
    没有持仓时照旧说不用平。停止从不直接下单。"""
    from trader_runtime.simulation_orders import SimulationOrderError
    app, provider, gateway, _, _ = await filled_entry(tmp_path)
    store = app.state.store
    session = app.state.broker.create_session("local", "native", USER)
    store.connection.execute("UPDATE strategy_drafts SET status = 'ready' WHERE id = ?", (STRATEGY_ID,))
    store.connection.commit()
    provider.quote_datetime = quote_now()
    monitor = app.state.strategy_monitor

    def gone(*args, **kwargs):   # the position went between the strategy's read and the card's own re-read
        raise SimulationOrderError("close_position_not_found")
    app.state.proposals.create_trade = gone
    strategy = store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    with caplog.at_level("WARNING", logger="trader_runtime.strategy_monitor"):
        outcome = await monitor.propose_close_after_stop(strategy, session_id=session.session_id)
    assert outcome["cards"] == []
    assert outcome["detail"].startswith("平仓卡没有生成：没找到这个策略在") and "SHFE.au2612" in outcome["detail"]
    assert outcome["detail"].endswith("请到持仓里手动平。") and "close_position_not_found" not in outcome["detail"]
    assert "code=close_position_not_found" in caplog.text
    attempt = next(item for item in store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)
                   if item["action"] == "stop_close")
    assert attempt["status"] == "FAILED" and "close_position_not_found" not in str(attempt["detail"])
    provider.position_items = []
    assert (await monitor.propose_close_after_stop(strategy, session_id=session.session_id))["detail"] == (
        "这个策略现在没有持仓，不用平仓。")
    assert gateway.submit_count == 1   # only the entry: stopping never sends an order
    app.state.store.close()


async def test_the_protection_sync_rests_until_something_it_reads_changes(tmp_path, monkeypatch):
    """止损监控只处理新变化 (所有者 2026-10-22: 每 0.5 秒重扫全部带保护的委托历史 → 只处理新成交): with the same
    fills, finished orders, trading day, protection links, stops and recorded fills as the last clean sync, a sync
    does nothing; any of them changing — a new day, a stop changed elsewhere, an order's report, a new fill — runs it
    again, and a sync that failed for a contract never counts as clean."""
    from trader_runtime import strategy_protection
    app, provider, _, _, request = await filled_entry(tmp_path)
    store, account = app.state.store, app.state.store.default_trading_account_id
    runs: list[str] = []
    original = strategy_protection._roll_positions

    def counted(db, user, account_, symbol, day, store_):
        runs.append(symbol)
        return original(db, user, account_, symbol, day, store_)
    monkeypatch.setattr(strategy_protection, "_roll_positions", counted)
    sync = app.state.autonomy.sync_strategy_protection

    def settled() -> list[str]:
        sync(), sync()  # a run's own writes take one more run, then it rests
        runs.clear()
        assert sync() == {} and sync() == {}
        return list(runs)

    assert settled() == []
    provider.day = "2026-08-26"
    sync()
    assert runs == [request["symbol"]]
    assert settled() == []
    with store.lock, store.connection:
        store.connection.execute("UPDATE ai_stop_orders SET last_error = 'elsewhere' WHERE trading_account_id = ?", (account,))
    sync()
    assert runs == [request["symbol"]]
    assert settled() == []
    with store.lock, store.connection:  # the order service records a new report (fact_revision moves)
        store.connection.execute("UPDATE tqkq_sim_order_links SET cancel_requested_at = 'x' WHERE client_order_id = ?",
                                 (request["clientOrderId"],))
    sync()
    assert runs == [request["symbol"]]
    assert settled() == []
    # A fill the sync cannot place (a trading day after the account's) fails for the contract: never clean.
    entry_fill(provider, request, trade_id="fill-future", time=2)
    provider.activity["trades"][0]["tradingDay"] = "2026-08-27"
    assert sync() == {(USER, request["symbol"]): "strategy_fill_trading_day_in_future"}
    assert account not in strategy_protection._CLEAN_SYNC[store]
    assert sync() == {(USER, request["symbol"]): "strategy_fill_trading_day_in_future"}
    app.state.store.close()

"""Late first observations use the fill's business day, through actual services."""
import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from test_simulation_orders import USER
from test_strategy_monitor import STRATEGY_ID
from test_strategy_protection import entry_fill, setup
from trader_runtime.strategy_protection import closed_results
from trader_runtime.trading_costs import FeeSchedule, review_trades


def timestamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1e9)


@pytest.mark.parametrize("old_day,new_day,clock", [
    ("2026-08-24", "2026-08-25", "2026-08-24T14:00:00"),
    ("2026-08-24", "2026-08-25", "2026-08-21T21:05:00"),
    ("2026-08-21", "2026-08-24", "2026-08-21T14:00:00"),
])
async def test_first_observed_historical_entry_rolls_to_historical_without_duplicate(tmp_path, old_day, new_day, clock):
    app, provider, gateway, monitor = setup(tmp_path)
    try:
        provider.day = old_day
        assert (await monitor.run_once())[0]["status"] == "submitted"
        entry = dict(gateway.request)
        entry_fill(provider, entry, time=timestamp(clock))
        provider.day = new_day
        provider.activity["orders"] = [{**entry, "status": "FINISHED", "volumeLeft": 0}]
        for _ in range(2):
            assert app.state.autonomy.sync_strategy_protection() == {}
        rows = app.state.store.connection.execute("SELECT trading_day FROM strategy_fill_events").fetchall()
        assert [row[0] for row in rows] == [old_day]
        stop = app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()
        assert stop["position_bucket"] == "historical"
        assert json.loads(stop["strategy_metadata_json"])["entryTradingDay"] == old_day
        assert gateway.submit_count == 1
    finally:
        app.state.store.close()


async def test_late_close_allocates_position_and_pnl_to_original_day(tmp_path):
    app, provider, gateway, monitor = setup(tmp_path)
    try:
        provider.day = "2026-08-24"
        assert (await monitor.run_once())[0]["status"] == "submitted"
        entry = dict(gateway.request)
        entry_fill(provider, entry, time=timestamp("2026-08-24T10:00:00"))
        provider.activity["orders"] = [{**entry, "status": "FINISHED", "volumeLeft": 0}]
        assert app.state.autonomy.sync_strategy_protection() == {}
        assert app.state.store.reserve_strategy_execution_attempt(attempt_id="late-close", strategy_id=STRATEGY_ID,
            user_id=USER, intent_id=monitor.proposals.intent["intentId"], signal_bar_time="late-close",
            trading_day=provider.day, action="close_long", volume=1)
        app.state.store.reserve_strategy_execution_leg(leg_id="late-close-leg", attempt_id="late-close",
            leg_index=0, position_bucket="today", volume=1)
        app.state.store.bind_strategy_execution_leg(leg_id="late-close-leg", client_order_id="late-close-order")
        close = {**entry, "clientOrderId": "late-close-order", "direction": "SELL", "offset": "CLOSETODAY"}
        entry_fill(provider, close, trade_id="close-fill", price=821, time=timestamp("2026-08-24T14:00:00"))
        provider.activity["orders"].append({**close, "status": "FINISHED", "volumeLeft": 0})
        provider.day = "2026-08-25"
        for _ in range(2):
            assert app.state.autonomy.sync_strategy_protection() == {}
        assert app.state.store.connection.execute("SELECT remaining_volume FROM ai_stop_orders").fetchone()[0] == 0
        assert closed_results(app.state.store, USER, STRATEGY_ID, "2026-08-24") == [("close:late-close-order", Decimal("-1000"))]
        assert closed_results(app.state.store, USER, STRATEGY_ID, "2026-08-25") == []
        assert gateway.submit_count == 1
    finally:
        app.state.store.close()


@pytest.mark.parametrize("day", [None, "", "2026-02-30", "2026-8-25", "2026-08-26"])
async def test_unknown_or_future_day_blocks_contract_without_persisting_fill(tmp_path, day):
    app, provider, gateway, monitor = setup(tmp_path)
    try:
        assert (await monitor.run_once())[0]["status"] == "submitted"
        entry = dict(gateway.request)
        fill = entry_fill(provider, entry)
        fill["tradingDay"] = day
        failures = app.state.autonomy.sync_strategy_protection()
        assert (USER, entry["symbol"]) in failures
        assert app.state.store.connection.execute("SELECT COUNT(*) FROM strategy_fill_events").fetchone()[0] == 0
        assert not app.state.autonomy.list_stops(user_id=USER)
    finally:
        app.state.store.close()


def test_review_does_not_guess_missing_day_and_matches_historical_close():
    schedule = FeeSchedule(source="custom").model_dump()
    entry = dict(tradeId="a", symbol="SHFE.au2612", direction="BUY", offset="OPEN", price=822, volume=1, tradeDateTime=1)
    with pytest.raises(ValueError, match="trade_trading_day_unavailable"):
        review_trades([entry], schedule, 1000)
    entry["tradingDay"] = "2026-08-24"
    close = {**entry, "tradeId": "b", "direction": "SELL", "offset": "CLOSE", "price": 821,
             "tradeDateTime": 2, "tradingDay": "2026-08-25"}
    result = review_trades([close, entry, close], schedule, 1000)
    assert len(result["trades"]) == 1
    assert result["trades"][0]["gross"] == -1000

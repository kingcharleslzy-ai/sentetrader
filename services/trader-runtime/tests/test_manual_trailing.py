"""手动持仓的追踪止损 (2026-10-25 第 4 批): an order card's stop and a position row's stop can trail the price —
the strategy's own trailing and breakeven, only ever tighter — and 浮盈 1R 后移到成本 moves the stop to the entry
once the profit reaches the entry-to-stop distance. A position row's stop stays a direct stop (its position check
keys on strategy_metadata_json being empty); both need a stop price."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_order_protection import Provider, _long_items, fill_entry, stop_row
from test_simulation_orders import USER, Gateway, application, confirm, quote_now
from trader_runtime.autonomy import AutonomyError
from trader_runtime.proposals import ProposalConflict

TICKS_2 = {"mode": "ticks", "value": 2, "activateAfter": 0, "atrPeriod": 14, "atrTimeframe": 300}


async def test_a_cards_trailing_stop_tightens_with_the_price_and_never_loosens(tmp_path: Path) -> None:
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / "trail-card.sqlite3", gateway, provider)
    session = app.state.broker.create_session("local", "native", USER)
    card = app.state.proposals.create_trade(
        session.session_id, "turn-trail", symbol="SHFE.au2612", side="buy_open", volume=1,
        price_intent="passive", reason="追踪止损",
        protection={"stopLossPrice": 822.5, "takeProfitPrice": None, "trailing": TICKS_2, "breakevenAtR": True})
    assert card["protection"]["trailing"]["mode"] == "ticks" and card["protection"]["breakevenAtR"] is True
    await confirm(app, card, "confirm-trail")
    request = dict(gateway.request)
    provider.order_reports = [{**request, "status": "FINISHED", "volumeLeft": 0}]
    fill_entry(provider, request, price=823.0)
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(request["clientOrderId"], user_id=USER)
    app.state.autonomy.sync_strategy_protection()
    stop = app.state.autonomy.list_stops(user_id=USER)[0]
    # 1R = 823.0 − 822.5 = 1 tick: the fill itself turns 浮盈 1R 后移到成本 into a 1-tick breakeven.
    assert stop["trailing"] == {"mode": "ticks", "value": 2, "activateAfter": 0} and stop["breakeven"] is True
    assert json.loads(stop_row(app)[0]["strategy_metadata_json"])["breakeven"] == {"triggerTicks": 1, "offsetTicks": 0}
    provider.last, provider.quote_datetime = 825.0, quote_now()
    await app.state.autonomy._check_stop(stop_row(app)[0])
    assert app.state.autonomy.list_stops(user_id=USER)[0]["triggerPrice"] == 824.0   # 825 − 2 ticks
    provider.last, provider.quote_datetime = 824.5, quote_now()
    await app.state.autonomy._check_stop(stop_row(app)[0])
    assert app.state.autonomy.list_stops(user_id=USER)[0]["triggerPrice"] == 824.0   # never looser
    assert gateway.submit_count == 1   # the entry only: tightening places nothing
    app.state.store.close()


async def test_a_position_rows_stop_trails_from_the_open_average_and_keeps_its_position_check(tmp_path: Path) -> None:
    app = application(tmp_path / "trail-direct.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = [{**item, "openPrice": 821.0} for item in _long_items(2, today=2)]
    session = app.state.broker.create_session("local", "native", USER)
    provider.last, provider.quote_datetime = 823.0, quote_now()
    stop = await app.state.autonomy.create_stop(
        user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
        position_bucket="today", volume=2, trigger_price=820.0, trailing=TICKS_2, breakeven_after_r=True)
    row = stop_row(app, stop["stopId"])[0]
    # Still a direct stop (its 原仓核对 keys on strategy_metadata_json being empty); the moving state sits apart.
    assert row["strategy_metadata_json"] is None
    state = json.loads(row["trailing_json"])
    assert (float(state["entryPrice"]), float(state["trailExtreme"]), state["breakeven"]) == (
        821.0, 823.0, {"triggerTicks": 2, "offsetTicks": 0})
    assert stop["trailing"]["mode"] == "ticks" and stop["breakeven"] is True
    provider.last, provider.quote_datetime = 826.0, quote_now()
    await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
    assert app.state.autonomy.list_stops(user_id=USER)[0]["triggerPrice"] == 825.0
    assert gateway.submit_count == 0
    # Closing the position still retires the stop through the direct-stop check.
    provider.position_items = []
    await app.state.autonomy.run_once()
    assert app.state.autonomy.list_stops(user_id=USER)[0]["status"] == "CANCELLED"
    app.state.store.close()


async def test_trailing_and_breakeven_need_a_stop_price(tmp_path: Path) -> None:
    app = application(tmp_path / "trail-needs-stop.sqlite3", Gateway(), Provider())
    session = app.state.broker.create_session("local", "native", USER)
    with pytest.raises(ProposalConflict) as raised:
        app.state.proposals.create_trade(
            session.session_id, "turn-no-stop", symbol="SHFE.au2612", side="buy_open", volume=1,
            price_intent="passive", reason="没止损",
            protection={"stopLossPrice": None, "takeProfitPrice": 823.5, "trailing": TICKS_2})
    assert raised.value.code == "protection_trailing_needs_stop"
    provider = app.state.data_provider
    provider.position_items = [{**item, "openPrice": 821.0} for item in _long_items(1, today=1)]
    provider.last, provider.quote_datetime = 823.0, quote_now()
    with pytest.raises(AutonomyError) as refused:
        await app.state.autonomy.create_stop(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
            position_bucket="today", volume=1, take_profit_price=824.0, breakeven_after_r=True)
    assert refused.value.code == "trailing_needs_stop"
    app.state.store.close()


def _bars(lows: list[float]) -> dict:
    """Completed bars with these lows, plus the forming bar (always excluded) at the end."""
    return {"bars": [{"low": low, "high": low + 2} for low in lows] + [{"low": 1.0, "high": 9999.0}]}


async def test_a_stop_can_follow_the_last_n_bars_low_and_never_loosens(tmp_path: Path) -> None:
    app = application(tmp_path / "trail-bars.sqlite3", Gateway(), Provider())
    provider, autonomy = app.state.data_provider, app.state.autonomy
    provider.position_items = [{**item, "openPrice": 821.0} for item in _long_items(1, today=1)]
    session = app.state.broker.create_session("local", "native", USER)
    provider.last, provider.quote_datetime = 823.0, quote_now()
    provider.market_workbench = lambda symbol, timeframe: _bars([818.0, 819.0, 820.0])
    bars_3 = {"mode": "bar_extreme", "value": 3, "atrTimeframe": 300}
    stop = await autonomy.create_stop(
        user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
        position_bucket="today", volume=1, trigger_price=817.0, trailing=bars_3)
    assert stop["trailing"] == {"mode": "bar_extreme", "value": 3, "atrTimeframe": 300}
    await autonomy._check_stop(stop_row(app, stop["stopId"])[0])
    assert autonomy.list_stops(user_id=USER)[0]["triggerPrice"] == 818.0   # lowest of the 3 completed bars
    provider.market_workbench = lambda symbol, timeframe: _bars([820.5, 821.0, 822.0])
    autonomy._bar_levels.clear()   # a new bar has completed
    await autonomy._check_stop(stop_row(app, stop["stopId"])[0])
    assert autonomy.list_stops(user_id=USER)[0]["triggerPrice"] == 820.5
    provider.market_workbench = lambda symbol, timeframe: _bars([815.0, 821.0, 822.0])
    autonomy._bar_levels.clear()
    await autonomy._check_stop(stop_row(app, stop["stopId"])[0])
    assert autonomy.list_stops(user_id=USER)[0]["triggerPrice"] == 820.5   # never looser
    # Fewer completed bars than asked: refused at arming, not silently fixed.
    provider.market_workbench = lambda symbol, timeframe: _bars([818.0])
    with pytest.raises(AutonomyError) as refused:
        await autonomy.create_stop(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
            position_bucket="today", volume=1, trigger_price=817.0, trailing=bars_3)
    assert refused.value.code == "trailing_bars_unavailable"
    # #1207: the price already broke the last 3 bars' low — arming would close the position at once, so it is refused.
    provider.market_workbench = lambda symbol, timeframe: _bars([823.0, 824.0, 825.0])
    with pytest.raises(AutonomyError) as breached:
        await autonomy.create_stop(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
            position_bucket="today", volume=1, trigger_price=817.0, trailing=bars_3)
    assert breached.value.code == "trailing_bars_breached" and "跌破前 3 根 K 线的低点 823" in str(breached.value.message)
    app.state.store.close()


def test_bar_trailing_counts_whole_bars() -> None:
    from trader_runtime.strategy_rules import ProtectionTrailing
    assert ProtectionTrailing(mode="bar_extreme", value=5).atrTimeframe == 300
    with pytest.raises(ValueError):
        ProtectionTrailing(mode="bar_extreme", value=2.5)

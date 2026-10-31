"""合并卡 (第四批, 2026-10-06): a close card carries the remaining position's new stop / take; one 确认
authorizes both steps. Step ② runs after the close finished, for what the account then holds, and
REPLACES the position's user-set protection (never two sets); any failed check changes nothing."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from test_order_protection import Provider, armed_card, fill_entry, lagging_poll_cache, manual_add, stop_row
from test_simulation_orders import USER, Gateway, application, confirm, quote_now
from trader_runtime import card_protection
from trader_runtime.proposals import ProposalConflict


def _long(volume: int, *, today: int, open_price: float = 822.3) -> list[dict]:
    return [{"symbol": "SHFE.au2612", "direction": "long", "volume": volume, "todayVolume": today,
             "historicalVolume": volume - today, "frozenToday": 0, "frozenHistorical": 0, "openPrice": open_price}]


async def _setup(tmp_path: Path, name: str, *, stop: float | None = 822.0, take: float | None = 825.0):
    """A long 2 lots (today) with a direct stop 822 / take 825 armed from the 持仓面板."""
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / name, gateway, provider)
    provider.position_items = _long(2, today=2)
    provider.last = 823.0
    provider.quote_datetime = quote_now()
    session = app.state.broker.create_session("local", "native", USER)
    if stop is not None or take is not None:
        await app.state.autonomy.create_stop(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
            position_bucket="today", volume=2, trigger_price=stop, take_profit_price=take)
    return app, provider, gateway, session


def _close_card(app, session, *, volume: int = 1, remaining=None, side: str = "sell_close"):
    return app.state.proposals.create_trade(
        session.session_id, "turn-combo", symbol="SHFE.au2612", side=side, volume=volume,
        price_intent="passive", position_bucket="today", reason="平一半，剩下的止损挪到成本",
        remaining_protection=remaining)


def _fill(app, provider, gateway, *, filled: int, volume: int = 1, held_after: int | None = None) -> None:
    """The close order ended: `filled` lots traded, the account report and the position follow."""
    request = dict(gateway.request)
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=volume - filled)
    provider.order_reports = [{**request, "status": "FINISHED", "volumeLeft": volume - filled}]
    if filled:
        fill_entry(provider, request, trade_id="close-1", volume=filled, price=823.0, time=5)
    held = (2 - filled) if held_after is None else held_after
    provider.position_items = _long(held, today=held) if held else []
    provider.last = 823.0
    provider.quote_datetime = quote_now()


async def _advance(app) -> int:
    return await app.state.order_watch.card_protection.advance_steps()


def _live_stops(app) -> list:
    return [row for row in stop_row(app) if row["status"] in {"ARMED", "PARTIAL"}]


def _last_card(app, session) -> dict:
    cards = [event["payload"] for _, event in app.state.store.list_events(session.session_id)
             if event.get("name") == "proposal.trade"]
    return cards[-1]


async def test_one_confirm_closes_then_replaces_the_remaining_protection(tmp_path: Path) -> None:
    app, provider, gateway, session = await _setup(tmp_path, "combo.sqlite3")
    old_id = _live_stops(app)[0]["id"]
    card = _close_card(app, session, remaining={"stopLoss": 822.5})
    plan = card["remainingProtection"]
    assert plan["remainingVolume"] == 1 and plan["stopLoss"] == {"action": "set", "price": "822.5", "atCost": True}
    assert plan["takeProfit"] == {"action": "keep", "price": "825.0"}
    assert plan["original"] == {"stopLoss": "822.0", "takeProfit": "825.0"} and plan["cost"] == "822.5"
    # AI 自主执行 off: the user's own confirmation still carries both steps.
    app.state.store.update_preferences(USER, {"trade": {"aiAutonomy": False}})
    app.state.autonomy.apply_autonomy_preference(USER, enabled=False)
    result = await confirm(app, card, "combo-confirm")
    assert result["remainingProtection"]["status"] == "awaiting_close"
    assert gateway.submit_count == 1
    assert await _advance(app) == 0  # the close is still on the book: nothing yet
    assert _live_stops(app)[0]["id"] == old_id

    _fill(app, provider, gateway, filled=1)
    assert await _advance(app) == 1
    rows = stop_row(app)
    old = next(row for row in rows if row["id"] == old_id)
    assert old["status"] == "CANCELLED" and old["last_error"] == "replaced_by_card"
    live = _live_stops(app)
    assert len(live) == 1  # replaced, never two sets
    new = live[0]
    assert (new["trigger_price"], new["take_profit_price"], new["volume"]) == ("822.5", "825.0", 1)
    assert new["strategy_metadata_json"] is None and new["position_bucket"] == "today"
    # #528 anchor taken after the close: the close's own fill is behind the watermark.
    assert int(new["anchor_volume"]) == 1 and int(new["closed_since"]) == 0
    assert any("close-1" in item for item in json.loads(new["fill_watermark_json"])["ids"])
    published = _last_card(app, session)["remainingProtection"]
    assert published["status"] == "done" and published["result"]["remainingVolume"] == 1
    assert published["result"]["previousStopLoss"] == "822.0"
    # A second sweep (or a restart) does nothing more.
    assert await _advance(app) == 0
    assert len(_live_stops(app)) == 1
    assert gateway.submit_count == 1  # step ② never sends an order
    app.state.store.close()


async def test_keep_preserves_today_and_historical_prices_after_the_real_close(tmp_path: Path) -> None:
    app, provider, gateway, session = await _setup(tmp_path, "combo-bucket-keep.sqlite3")
    provider.position_items = _long(4, today=2)
    await app.state.autonomy.create_stop(
        user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
        position_bucket="historical", volume=2, trigger_price=822.0, take_profit_price=830.0)
    card = _close_card(app, session, remaining={"stopLoss": 822.5})
    plan = card["remainingProtection"]
    assert plan["takeProfit"] == {"action": "keep", "price": None}
    assert plan["original"] == {"stopLoss": "822.0", "takeProfit": None}
    assert [(item["positionBucket"], item["volume"], item["takeProfit"])
            for item in plan["perBucketOriginal"]] == [("today", 2, "825.0"), ("historical", 2, "830.0")]
    await confirm(app, card, "combo-bucket-keep")
    _fill(app, provider, gateway, filled=1)
    provider.position_items = _long(3, today=1)
    assert await _advance(app) == 1
    by_bucket = {row["position_bucket"]: row for row in _live_stops(app)}
    assert [(by_bucket[bucket]["volume"], by_bucket[bucket]["trigger_price"], by_bucket[bucket]["take_profit_price"])
            for bucket in ("today", "historical")] == [(1, "822.5", "825.0"), (2, "822.5", "830.0")]
    result = _last_card(app, session)["remainingProtection"]["result"]
    assert result["takeProfit"] is None and result["previousTakeProfit"] is None
    assert [(item["positionBucket"], item["volume"], item["takeProfit"], item["previousTakeProfit"])
            for item in result["perBucketProtection"]] == [
                ("today", 1, "825.0", "825.0"), ("historical", 2, "830.0", "830.0")]
    ids = result["stopIds"]
    assert await _advance(app) == 0
    assert {row["id"] for row in _live_stops(app)} == set(ids) and gateway.submit_count == 1
    app.state.store.close()


@pytest.mark.parametrize("source", ["direct", "entry_fill"])
@pytest.mark.parametrize("new_today", [False, True])
async def test_overnight_card_reads_original_buckets_before_any_monitor(tmp_path: Path, new_today: bool, source: str) -> None:
    if source == "direct":
        app, provider, gateway, session = await _setup(tmp_path, "combo-overnight.sqlite3")
    else:
        app, provider, gateway, session, _ = await armed_card(
            tmp_path, volume=2, stop=822.0, take=825.0, name="combo-overnight.sqlite3")
    before_submits = gateway.submit_count
    old_id = _live_stops(app)[0]["id"]
    with app.state.store.lock, app.state.store.connection:
        app.state.store.connection.execute(
            "UPDATE ai_stop_orders SET created_at = ? WHERE id = ?",
            ("2026-08-25T02:00:00+00:00" if source == "direct" else "2026-08-26T02:00:00+00:00", old_id))
    # Fill-backed metadata carries the actual entry day; its later row creation time is irrelevant.
    provider.position_items = _long(4 if new_today else 2, today=2 if new_today else 0)
    if new_today:
        fresh = await app.state.autonomy.create_stop(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
            position_bucket="today", volume=2, trigger_price=822.0, take_profit_price=830.0)
        with app.state.store.lock, app.state.store.connection:
            app.state.store.connection.execute(
                "UPDATE ai_stop_orders SET created_at = ?, account_trading_day = '2026-08-26' WHERE id = ?",
                ("2026-08-26T02:00:00+00:00", fresh["stopId"]))  # armed on the new account day
    replay_now = datetime(2026, 8, 26, 2, tzinfo=UTC)
    app.state.simulation_orders.trading_clock = lambda: replay_now
    provider.quote_datetime = replay_now.isoformat()
    provider.day = "2026-08-26"
    card = app.state.proposals.create_trade(
        session.session_id, "overnight", symbol="SHFE.au2612", side="sell_close", volume=1,
        price_intent="passive", position_bucket="historical", reason="平一手昨仓，止盈不动",
        remaining_protection={"stopLoss": 822.5})
    expected = [("today", "830.0"), ("historical", "825.0")] if new_today else [("historical", "825.0")]
    assert [(item["positionBucket"], item["takeProfit"]) for item in card["remainingProtection"]["perBucketOriginal"]] == expected
    # Preview maps the existing rule only; it never rolls or shrinks the persisted row.
    assert next(row for row in stop_row(app) if row["id"] == old_id)["position_bucket"] == "today"
    assert gateway.submit_count == before_submits
    await confirm(app, card, "overnight-confirm")
    _fill(app, provider, gateway, filled=1)
    provider.position_items = _long(3 if new_today else 1, today=2 if new_today else 0)
    provider.quote_datetime = replay_now.isoformat()
    assert await _advance(app) == 1
    actual = _last_card(app, session)["remainingProtection"]
    assert actual["status"] == "done"
    assert [(item["positionBucket"], item["takeProfit"]) for item in actual["result"]["perBucketProtection"]] == expected
    assert await _advance(app) == 0 and gateway.submit_count == before_submits + 1
    app.state.store.close()


@pytest.mark.parametrize("ambiguity", ["different_prices", "unprotected", "overlapping_direct"])
async def test_keep_ambiguity_is_422_before_a_card_or_order(tmp_path: Path, ambiguity: str) -> None:
    app, provider, gateway, session = await _setup(tmp_path, "combo-ambiguous.sqlite3")
    if ambiguity == "different_prices":
        provider.position_items = _long(4, today=4)
        await app.state.autonomy.create_stop(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
            position_bucket="today", volume=2, trigger_price=822.0, take_profit_price=830.0)
    elif ambiguity == "overlapping_direct":
        provider.position_items = _long(3, today=3)
        await app.state.autonomy.create_stop(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
            position_bucket="today", volume=2, trigger_price=822.0, take_profit_price=825.0)
    else:
        provider.position_items = _long(3, today=3)  # only two lots have a known take
    before = [(row["id"], row["status"], row["take_profit_price"]) for row in stop_row(app)]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        session_view = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
        body = {"sessionId": session_view["sessionId"], "symbol": "SHFE.au2612", "side": "sell_close", "volume": 1,
                "positionBucket": "today", "priceIntent": "passive", "reason": "止盈不动",
                "remainingProtection": {"stopLoss": 822.5}}
        response = await client.post("/api/simulation/proposals", json=body)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "remaining_protection_ambiguous"
        assert "明确设置或撤掉" in response.json()["detail"]["message"]
        body["remainingProtection"]["takeProfit"] = 830  # explicit uniform set remains available
        assert (await client.post("/api/simulation/proposals", json=body)).status_code == 201
    assert gateway.submit_count == 0
    assert [(row["id"], row["status"], row["take_profit_price"]) for row in stop_row(app)] == before
    app.state.store.close()


async def test_keep_becoming_ambiguous_after_confirmation_keeps_old_protection(tmp_path: Path) -> None:
    app, provider, gateway, session = await _setup(tmp_path, "combo-later-ambiguity.sqlite3")
    card = _close_card(app, session, remaining={"stopLoss": 822.5})
    await confirm(app, card, "combo-later-ambiguity")
    await app.state.autonomy.create_stop(
        user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
        position_bucket="today", volume=1, trigger_price=822.0, take_profit_price=830.0)
    before = [(row["id"], row["trigger_price"], row["take_profit_price"], row["remaining_volume"]) for row in _live_stops(app)]
    _fill(app, provider, gateway, filled=1)
    assert await _advance(app) == 1
    after = [(row["id"], row["trigger_price"], row["take_profit_price"], row["remaining_volume"])
             for row in _live_stops(app)]
    assert [item[:3] for item in after] == [item[:3] for item in before]
    assert [item[3] for item in after] == [1, 1]  # normal fill convergence, not replacement
    published = _last_card(app, session)["remainingProtection"]
    assert published["status"] == "failed" and "无法确定" in published["reason"]
    assert await _advance(app) == 0 and gateway.submit_count == 1
    app.state.store.close()


@pytest.mark.parametrize("source", ["direct", "entry_fill"])
async def test_keep_rechecks_close_then_new_unprotected_lots_without_background(tmp_path: Path, source: str) -> None:
    if source == "direct":
        app, provider, gateway, session = await _setup(tmp_path, "combo-new-lots.sqlite3")
    else:
        app, provider, gateway, session, _ = await armed_card(
            tmp_path, volume=2, stop=822.0, take=825.0, name="combo-new-lots.sqlite3")
        provider.position_items = _long(2, today=2)
    original = _live_stops(app)[0]["id"]
    card = _close_card(app, session, remaining={"stopLoss": 822.5})
    await confirm(app, card, "combo-new-lots")
    _fill(app, provider, gateway, filled=1, held_after=2)
    provider.trades.insert(0, {"tradeId": "unprotected-add", "clientOrderId": "external-add",
                               "symbol": "SHFE.au2612", "direction": "BUY", "offset": "OPEN", "volume": 1,
                               "price": 823.0, "tradeDateTime": 6, "tradingDay": provider.day})
    # No run_once: the cached old row still says two; the action itself must prove only one remains.
    assert _live_stops(app)[0]["remaining_volume"] == 2
    assert await _advance(app) == 1
    live = _live_stops(app)
    assert len(live) == 1 and live[0]["id"] == original
    assert (live[0]["remaining_volume"], live[0]["trigger_price"], live[0]["take_profit_price"]) == (1, "822.0", "825.0")
    published = _last_card(app, session)["remainingProtection"]
    assert published["status"] == "failed" and "无法确定" in published["reason"]
    assert await _advance(app) == 0
    assert _live_stops(app)[0]["id"] == original and _live_stops(app)[0]["remaining_volume"] == 1
    app.state.store.close()


async def test_a_partial_close_protects_what_is_actually_left(tmp_path: Path) -> None:
    app, provider, gateway, session = await _setup(tmp_path, "combo-partial.sqlite3")
    provider.position_items = _long(3, today=3)
    card = _close_card(app, session, volume=2, remaining={"stopLoss": 822.5, "takeProfit": None})
    await confirm(app, card, "combo-partial")
    _fill(app, provider, gateway, filled=1, volume=2, held_after=2)
    assert await _advance(app) == 1
    live = _live_stops(app)
    assert len(live) == 1 and live[0]["volume"] == 2  # 2 lots left, not the planned 1
    assert live[0]["trigger_price"] == "822.5" and live[0]["take_profit_price"] is None  # take removed
    app.state.store.close()


async def test_the_replacement_protects_the_combined_reads_position_not_the_lagging_poll_cache(
    tmp_path: Path, monkeypatch,
) -> None:
    """#754 (same root as #528): step ② paired the close's fresh fills with the poll cache's positions. After the
    close the user added 2 lots: the poll cache still shows 1, the combined account read 3 — the replacement must
    protect the 3 lots actually held, anchored on that same read, not 1 lot behind a watermark that holds the add."""
    app, provider, gateway, session = await _setup(tmp_path, "combo-cut.sqlite3")
    card = _close_card(app, session, remaining={"stopLoss": 822.5, "takeProfit": None})
    await confirm(app, card, "combo-cut")
    _fill(app, provider, gateway, filled=1)  # the poll cache: 1 lot left
    manual_add(provider)
    cuts = lagging_poll_cache(monkeypatch, provider, _long(3, today=3))
    assert await _advance(app) == 1
    live = _live_stops(app)
    assert len(live) == 1 and (live[0]["volume"], int(live[0]["anchor_volume"])) == (3, 3)
    assert any("add-1" in item for item in json.loads(live[0]["fill_watermark_json"])["ids"])
    assert True in cuts and set(cuts) == {True}
    app.state.store.close()


async def test_a_close_that_never_filled_leaves_the_protection_alone(tmp_path: Path) -> None:
    app, provider, gateway, session = await _setup(tmp_path, "combo-unfilled.sqlite3")
    card = _close_card(app, session, remaining={"stopLoss": 822.5})
    await confirm(app, card, "combo-unfilled")
    _fill(app, provider, gateway, filled=0, held_after=2)
    assert await _advance(app) == 1
    assert [row["trigger_price"] for row in _live_stops(app)] == ["822.0"]
    published = _last_card(app, session)["remainingProtection"]
    assert published["status"] == "skipped" and "没有成交" in published["reason"]
    app.state.store.close()


async def test_an_unknown_close_waits_and_then_gives_up_without_touching_anything(
        tmp_path: Path, monkeypatch) -> None:
    app, provider, gateway, session = await _setup(tmp_path, "combo-unknown.sqlite3")
    card = _close_card(app, session, remaining={"stopLoss": 822.5})
    gateway.submit_error = True  # the submit's answer is lost: the close's outcome is unknown
    await confirm(app, card, "combo-unknown")
    gateway.query_report = {"ordersInitialized": True, "found": False}
    assert await _advance(app) == 0  # unknown: wait, never guess
    assert app.state.store.card_protection_step(card["proposalId"])["status"] == "awaiting_close"
    later = datetime.now(UTC) + timedelta(seconds=card_protection.UNKNOWN_STALL_SECONDS + 5)
    monkeypatch.setattr(card_protection, "_now", lambda: later)
    assert await _advance(app) == 1
    assert app.state.store.card_protection_step(card["proposalId"])["status"] == "skipped"
    assert [row["trigger_price"] for row in _live_stops(app)] == ["822.0"]
    app.state.store.close()


async def test_a_crossed_new_stop_fails_step_two_and_keeps_the_old_stop(tmp_path: Path) -> None:
    app, provider, gateway, session = await _setup(tmp_path, "combo-crossed.sqlite3")
    card = _close_card(app, session, remaining={"stopLoss": 822.5})
    await confirm(app, card, "combo-crossed")
    _fill(app, provider, gateway, filled=1)
    provider.last = 822.5  # the price fell onto the new stop before step ② ran
    provider.quote_datetime = quote_now()
    assert await _advance(app) == 1
    live = _live_stops(app)
    assert [row["trigger_price"] for row in live] == ["822.0"]  # the old stop stands, no other price
    published = _last_card(app, session)["remainingProtection"]
    assert published["status"] == "failed" and published["reason"] == "现价已穿过 822.5"
    assert [action["label"] for action in published["actions"]] == ["按现价重新设", "问 AI"]
    app.state.store.close()


def test_a_card_that_closes_everything_cannot_carry_it(tmp_path: Path) -> None:
    import asyncio
    app, _, _, session = asyncio.run(_setup(tmp_path, "combo-all.sqlite3"))
    with pytest.raises(ProposalConflict) as raised:
        _close_card(app, session, volume=2, remaining={"stopLoss": 822.5})
    assert raised.value.code == "remaining_protection_nothing_left"
    assert getattr(raised.value, "status_code", None) == 422
    with pytest.raises(ProposalConflict) as raised:  # wrong side of the market
        _close_card(app, session, remaining={"stopLoss": 823.5})
    assert raised.value.code == "remaining_protection_wrong_side" and "低于现价 823" in str(raised.value)
    with pytest.raises(ProposalConflict) as raised:  # an open card uses `protection`, not this
        app.state.proposals.create_trade(
            session.session_id, "turn-open", symbol="SHFE.au2612", side="buy_open", volume=1,
            price_intent="passive", reason="开仓", remaining_protection={"stopLoss": 822.0})
    assert raised.value.code == "remaining_protection_close_only"
    # The open card's own protection is unchanged by all this.
    card = app.state.proposals.create_trade(
        session.session_id, "turn-open-2", symbol="SHFE.au2612", side="buy_open", volume=1,
        price_intent="passive", reason="开仓", protection={"stopLossPrice": 822.5, "takeProfitPrice": None})
    assert card["protection"] == {"stopLossPrice": "822.5"} and "remainingProtection" not in card
    app.state.store.close()


async def test_the_route_answers_422_for_a_card_that_closes_everything(tmp_path: Path) -> None:
    app, _, _, _ = await _setup(tmp_path, "combo-route.sqlite3")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        session = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
        body = {"sessionId": session["sessionId"], "symbol": "SHFE.au2612", "side": "sell_close", "volume": 2,
                "positionBucket": "today", "priceIntent": "passive", "reason": "全平",
                "remainingProtection": {"stopLoss": 822.5}}
        response = await client.post("/api/simulation/proposals", json=body)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "remaining_protection_nothing_left"
        body["volume"] = 1
        response = await client.post("/api/simulation/proposals", json=body)
        assert response.status_code == 201 and response.json()["remainingProtection"]["stopLoss"]["price"] == "822.5"
    app.state.store.close()


async def test_a_repeated_confirm_and_a_restart_never_replace_twice(tmp_path: Path) -> None:
    app, provider, gateway, session = await _setup(tmp_path, "combo-restart.sqlite3")
    card = _close_card(app, session, remaining={"stopLoss": 822.5})
    await confirm(app, card, "combo-key")
    await confirm(app, card, "combo-key")  # the same click again: the cached answer
    assert gateway.submit_count == 1
    steps = app.state.store.connection.execute("SELECT COUNT(*) FROM card_protection_steps").fetchone()[0]
    assert steps == 1
    _fill(app, provider, gateway, filled=1)
    # A "restart": a fresh relay over the same database.
    relay = card_protection.CardProtectionRelay(app)
    assert await relay.advance_steps() == 1
    assert await relay.advance_steps() == 0
    assert await _advance(app) == 0
    assert len(_live_stops(app)) == 1
    app.state.store.close()


async def test_open_card_protection_still_arms_on_its_fill(tmp_path: Path) -> None:
    """Regression: the open card's protection path is untouched (the rows it arms are what a later
    merged card replaces)."""
    app, provider, gateway, session = await _setup(tmp_path, "combo-open.sqlite3", stop=None, take=None)
    card = app.state.proposals.create_trade(
        session.session_id, "turn-open", symbol="SHFE.au2612", side="buy_open", volume=1,
        price_intent="passive", reason="开仓", protection={"stopLossPrice": 822.5, "takeProfitPrice": None})
    await confirm(app, card, "open-key")
    request = dict(gateway.request)
    provider.order_reports = [{**request, "status": "FINISHED", "volumeLeft": 0}]
    fill_entry(provider, request, volume=1)
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(request["clientOrderId"], user_id=USER)
    app.state.autonomy.sync_strategy_protection()
    rows = _live_stops(app)
    assert len(rows) == 1 and rows[0]["trigger_price"] == "822.5"
    assert json.loads(rows[0]["strategy_metadata_json"])["source"] == "manual"
    app.state.store.close()


class PerOrderGateway(Gateway):
    """Answers each client order with its own report (a 今+昨 batch sends two)."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: dict[str, dict] = {}
        self.finished: set[str] = set()

    async def submit_order_once(self, request: dict) -> dict:
        self.submit_count += 1
        self.request = dict(request)
        self.requests[request["clientOrderId"]] = dict(request)
        return self.report(request)

    async def query_order(self, client_order_id: str) -> dict:
        request = self.requests[client_order_id]
        if client_order_id in self.finished:
            return self.report(request, status="FINISHED", volume_left=0)
        return self.report(request)


async def test_a_today_and_yesterday_batch_close_replaces_the_protection_once_both_ended(tmp_path: Path) -> None:
    """The SHFE split close (one card, a 平今 and a 平昨 order) through its real entry: confirm → both orders
    end filled → one new protection for what is actually left; the closed bucket ends by its anchor,
    the surviving bucket is replaced_by_card."""
    provider, gateway = Provider(), PerOrderGateway()
    app = application(tmp_path / "combo-batch.sqlite3", gateway, provider)
    provider.position_items = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 3, "todayVolume": 1,
                                "historicalVolume": 2, "frozenToday": 0, "frozenHistorical": 0, "openPrice": 822.3}]
    provider.last, provider.quote_datetime = 823.0, quote_now()
    session = app.state.broker.create_session("local", "native", USER)
    for bucket, volume in (("today", 1), ("historical", 2)):
        await app.state.autonomy.create_stop(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
            position_bucket=bucket, volume=volume, trigger_price=822.0, take_profit_price=825.0)
    old_ids = {row["id"] for row in _live_stops(app)}
    app.state.store.update_preferences(USER, {"trade": {"closePreference": "today"}})
    card = app.state.proposals.create_close_batch(
        session.session_id, "turn-batch", symbols=["SHFE.au2612"], direction="long", max_volume=2,
        reason="平两手，剩下的止损挪到 822.5", remaining_protection={"stopLoss": 822.5})
    assert [order["positionBucket"] for order in card["orders"]] == ["today", "historical"]
    assert card["remainingProtection"]["remainingVolume"] == 1
    await confirm(app, card, "combo-batch")
    assert gateway.submit_count == 2
    ids = list(gateway.requests)
    gateway.finished.add(ids[0])  # only the 平今 has ended: wait for the other
    fill_entry(provider, gateway.requests[ids[0]], trade_id="close-t", volume=1, time=5)
    assert await _advance(app) == 0 and {row["id"] for row in _live_stops(app)} == old_ids
    gateway.finished.add(ids[1])
    fill_entry(provider, gateway.requests[ids[1]], trade_id="close-h", volume=1, time=6)
    provider.position_items = [{**provider.position_items[0], "volume": 1, "todayVolume": 0, "historicalVolume": 1}]
    provider.order_reports = [{**gateway.requests[i], "status": "FINISHED", "volumeLeft": 0} for i in ids]
    provider.quote_datetime = quote_now()
    assert await _advance(app) == 1
    rows = {row["id"]: row for row in stop_row(app)}
    assert all(rows[i]["status"] == "CANCELLED" for i in old_ids)
    assert {rows[i]["last_error"] for i in old_ids} == {"anchor_position_closed", "replaced_by_card"}
    live = _live_stops(app)
    assert len(live) == 1 and (live[0]["position_bucket"], live[0]["volume"], live[0]["trigger_price"]) == ("historical", 1, "822.5")
    assert live[0]["take_profit_price"] == "825.0" and int(live[0]["anchor_volume"]) == 1
    cards = [event["payload"] for _, event in app.state.store.list_events(session.session_id)
             if event.get("name") == "proposal.close_batch"]
    assert cards[-1]["remainingProtection"]["status"] == "done"
    assert await _advance(app) == 0 and len(_live_stops(app)) == 1
    app.state.store.close()


async def test_a_strategys_own_protection_is_named_on_the_card_and_left_alone(tmp_path: Path) -> None:
    app, provider, gateway, session = await _setup(tmp_path, "combo-strategy.sqlite3")
    strategy_row = _live_stops(app)[0]["id"]
    app.state.store.connection.execute(
        "UPDATE ai_stop_orders SET strategy_metadata_json = ? WHERE id = ?",
        (json.dumps({"strategyId": "strategy-x", "entryPrice": "822.3"}), strategy_row))
    app.state.store.connection.commit()
    card = _close_card(app, session, remaining={"stopLoss": 822.5})
    assert card["remainingProtection"]["strategyNote"].startswith("策略「策略」的保护不受影响")
    await confirm(app, card, "combo-strategy")
    _fill(app, provider, gateway, filled=1)
    assert await _advance(app) == 1
    kept = next(row for row in stop_row(app) if row["id"] == strategy_row)
    assert kept["status"] == "ARMED" and kept["last_error"] is None  # the strategy's stop is untouched
    app.state.store.close()


TICKS_2 = {"mode": "ticks", "value": 2, "activateAfter": 0, "atrPeriod": 14, "atrTimeframe": 300}
PERCENT_1 = {"mode": "percent", "value": 1, "activateAfter": 0, "atrPeriod": 14, "atrTimeframe": 300}


async def test_keep_carries_a_ticket_stops_trailing_and_breakeven_not_its_fill_ownership(tmp_path: Path) -> None:
    """#1193 A: a ticket's stop keeps its motion in strategy_metadata_json; 止损不变 used to read only trailing_json
    and replaced it with a fixed stop. The kept stop now trails as before; the ticket's fill ownership stays behind."""
    app, provider, gateway, session, _ = await armed_card(
        tmp_path, volume=2, stop=822.0, take=825.0, name="combo-ticket-motion.sqlite3")
    provider.position_items = _long(2, today=2)
    row = _live_stops(app)[0]
    metadata = json.loads(row["strategy_metadata_json"])
    motion = {"trailing": {"mode": "ticks", "value": 2.0, "activateAfter": 0}, "entryPrice": "822.3",
              "priceTick": "0.02", "trailExtreme": "823.0", "breakeven": {"triggerTicks": 15, "offsetTicks": 0}}
    with app.state.store.lock, app.state.store.connection:
        app.state.store.connection.execute("UPDATE ai_stop_orders SET strategy_metadata_json = ? WHERE id = ?",
                                           (json.dumps({**metadata, **motion}), row["id"]))
    card = _close_card(app, session, remaining={"takeProfit": 826.0})
    await confirm(app, card, "combo-ticket-motion")
    _fill(app, provider, gateway, filled=1)
    assert await _advance(app) == 1
    new = _live_stops(app)[0]
    assert (new["trigger_price"], new["take_profit_price"], new["strategy_metadata_json"]) == ("822.0", "826", None)
    assert json.loads(new["trailing_json"]) == motion   # the motion only: no source / fixed prices / authorization
    app.state.store.close()


async def test_keep_carries_each_buckets_own_trailing_and_refuses_one_bucket_moving_two_ways(tmp_path: Path) -> None:
    """#1193 B: today trails by ticks, yesterday by percent — each bucket keeps its own rule (it used to copy the
    first one everywhere). Two stops in one bucket that move differently are refused before anything changes."""
    app, provider, gateway, session = await _setup(tmp_path, "combo-bucket-motion.sqlite3", stop=None, take=None)
    provider.position_items = [{**item, "openPrice": 821.0} for item in _long(4, today=2)]
    autonomy = app.state.autonomy
    for bucket, rule in (("today", TICKS_2), ("historical", PERCENT_1)):
        await autonomy.create_stop(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
            position_bucket=bucket, volume=2, trigger_price=820.0, take_profit_price=830.0, trailing=rule)
    card = _close_card(app, session, remaining={"takeProfit": 829.0})
    await confirm(app, card, "combo-bucket-motion")
    _fill(app, provider, gateway, filled=1)
    provider.position_items = [{**item, "openPrice": 821.0} for item in _long(3, today=1)]
    assert await _advance(app) == 1
    modes = {row["position_bucket"]: json.loads(row["trailing_json"])["trailing"]["mode"] for row in _live_stops(app)}
    assert modes == {"today": "ticks", "historical": "percent"}
    app.state.store.close()

    rows = [{"position_bucket": "today", "trigger_price": "820", "strategy_metadata_json": None,
             "trailing_json": json.dumps({"trailing": TICKS_2})},
            {"position_bucket": "today", "trigger_price": "820", "strategy_metadata_json": None,
             "trailing_json": json.dumps({"trailing": PERCENT_1})}]
    with pytest.raises(card_protection.RemainingProtectionInvalid) as refused:
        card_protection.bucket_stop_motion([("today", 2)], rows)
    assert refused.value.code == "remaining_protection_ambiguous"

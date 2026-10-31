"""保本提醒 (第四批, 2026-10-06): a position whose user-set stop is still worse than its cost is asked once a
trading day — when its profit covers its risk (1 R by default, or k R, or a fixed amount) — whether to
move the stop to the cost; 移到成本 moves the stops the card was asked about (止盈不动) once their
original position is re-proven and the price and cost basis still hold, 不用 ends the day's question.
A strategy's protection, a stale snapshot, no stop or a stop already at the cost ask nothing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from test_account_reminders import login, reminder_cards, reminder_notices
from test_order_protection import Provider, fill_entry, lagging_poll_cache, manual_add, stop_row
from test_simulation_orders import USER, Gateway, application, confirm, quote_now
from test_web_push import APPLE, body, configure
from trader_runtime import breakeven
from trader_runtime.web_push import breakeven_message, kind_enabled


def _long(*, open_price: float = 820.0, pnl: float = 6000.0) -> list[dict]:
    return [{"symbol": "SHFE.au2612", "direction": "long", "volume": 2, "todayVolume": 2, "historicalVolume": 0,
             "frozenToday": 0, "frozenHistorical": 0, "openPrice": open_price, "unrealizedPnl": pnl}]


async def _setup(tmp_path: Path, name: str, *, stop: float | None = 818.0, take: float | None = 830.0):
    """Long 2 at 820 with a direct stop 818 (R = 2) / take 830, price 823 (profit 3 ≥ 1 R)."""
    provider = Provider()
    app = application(tmp_path / name, Gateway(), provider)
    provider.position_items = _long()
    provider.last, provider.quote_datetime = 823.0, quote_now()
    session = app.state.broker.create_session("local", "native", USER)
    if stop is not None:
        await app.state.autonomy.create_stop(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
            position_bucket="today", volume=2, trigger_price=stop, take_profit_price=take)
    return app, provider, session


async def _check(app) -> int:
    services = app.state.accounts.default
    provider = services.provider
    return await breakeven.check_breakeven(app, user_id=USER, account_id=services.account_id, services=services,
                                           positions=provider.positions(), summary=provider.account_summary(""))


def _set_rule(app, **notify) -> None:
    app.state.store.update_preferences(USER, {"notify": notify})


async def test_one_r_asks_once_a_day_with_the_step_and_both_buttons(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-1r.sqlite3")
    assert await _check(app) == 1
    notices = reminder_notices(app, session.session_id, "breakeven-")
    assert len(notices) == 1 and "保本提醒：沪金2612浮盈已超过止损距离" in notices[0]["payload"]["text"]
    card = reminder_cards(app, session.session_id, notices[0]["payload"]["turnId"])[0]
    assert card["kind"] == "breakeven" and card["title"] == "保本提醒 · 沪金2612"
    assert card["summary"] == "浮盈已超过止损距离：成本 820，现价 823，止损 818。"
    assert card["step"] == {"title": "止损 818 → 820（成本）", "detail": "止盈 830 不变"}
    assert [(a["kind"], a["label"], a["choice"]) for a in card["actions"]] == [
        ("breakeven", "移到成本", "move"), ("breakeven", "不用", "decline")]
    assert await _check(app) == 0  # once per position per trading day
    assert len(reminder_notices(app, session.session_id, "breakeven-")) == 1
    app.state.store.close()


async def test_nothing_is_asked_below_the_threshold_or_without_a_worse_stop(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-none.sqlite3")
    provider.last, provider.quote_datetime = 821.5, quote_now()  # profit 1.5 < R 2
    assert await _check(app) == 0
    provider.position_items = _long(open_price=817.5)  # the stop 818 is already better than the cost
    provider.last, provider.quote_datetime = 823.0, quote_now()
    assert await _check(app) == 0
    app.state.store.close()
    app, provider, session = await _setup(tmp_path, "be-nostop.sqlite3", stop=None)
    assert await _check(app) == 0  # no stop at all: nothing to move
    app.state.store.close()


async def test_a_strategys_own_protection_is_not_asked_about(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-strategy.sqlite3")
    app.state.store.connection.execute(
        "UPDATE ai_stop_orders SET strategy_metadata_json = ?",
        (json.dumps({"strategyId": "strategy-1", "entryPrice": "820"}),))
    app.state.store.connection.commit()
    assert await _check(app) == 0
    app.state.store.close()


async def test_multiple_and_amount_thresholds(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-rule.sqlite3")
    _set_rule(app, breakEvenRule={"mode": "multiple", "multiple": 2})
    assert await _check(app) == 0  # profit 3 < 2 × 2
    provider.last, provider.quote_datetime = 824.0, quote_now()
    assert await _check(app) == 1
    card = reminder_cards(app, session.session_id, reminder_notices(app, session.session_id, "breakeven-")[0]["payload"]["turnId"])[0]
    assert card["summary"].startswith("浮盈已到止损距离的 2 倍")
    app.state.store.close()
    app, provider, session = await _setup(tmp_path, "be-amount.sqlite3")
    # The 美元 threshold is the USD sub-account's alone: the 元 account still compares with 8,000 元.
    _set_rule(app, breakEvenRule={"mode": "amount", "amount": 8000, "amountUsd": 100})
    assert await _check(app) == 0  # floating 6,000 < 8,000
    provider.position_items = _long(pnl=9000.0)
    assert await _check(app) == 1
    card = reminder_cards(app, session.session_id, reminder_notices(app, session.session_id, "breakeven-")[0]["payload"]["turnId"])[0]
    assert card["summary"].startswith("浮盈已到 9,000 元（你设的 8,000 元）")
    app.state.store.close()


def test_the_dollar_threshold_is_validated_like_the_yuan_one_and_clears_with_null(tmp_path: Path) -> None:
    app = application(tmp_path / "be-usd.sqlite3", Gateway(), Provider())
    store = app.state.store
    assert store.get_preferences(USER)["preferences"]["notify"]["breakEvenRule"]["amountUsd"] is None  # no default
    saved = store.update_preferences(USER, {"notify": {"breakEvenRule": {"mode": "amount", "amountUsd": 500}}})
    assert saved["preferences"]["notify"]["breakEvenRule"] == {"mode": "amount", "multiple": 1.0, "amount": 1000, "amountUsd": 500}
    for bad in (0, 10_000_001, 2.5, "500"):
        with pytest.raises(HTTPException):
            store.update_preferences(USER, {"notify": {"breakEvenRule": {"amountUsd": bad}}})
    cleared = store.update_preferences(USER, {"notify": {"breakEvenRule": {"amountUsd": None}}})
    assert cleared["preferences"]["notify"]["breakEvenRule"]["amountUsd"] is None
    store.close()


async def test_a_stale_quote_or_snapshot_judges_nothing_and_records_nothing(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-stale.sqlite3")
    provider.age_seconds = 40.0
    assert await _check(app) == 0
    assert app.state.store.breakeven_notices(user_id=USER) == []
    provider.age_seconds = 0.0
    provider.quote_datetime = quote_now()
    services = app.state.accounts.default
    stale = {**provider.account_summary(""), "isCached": True}
    assert await breakeven.check_breakeven(app, user_id=USER, account_id=services.account_id, services=services,
                                           positions=provider.positions(), summary=stale) == 0
    assert await _check(app) == 1  # fresh again: the one question goes out then
    app.state.store.close()


async def test_move_to_cost_moves_the_stop_and_leaves_the_take(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-move.sqlite3")
    await _check(app)
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    http = await login(app)
    response = await http.post(f"/api/ai-execution/breakeven/{notice['id']}/move")
    assert response.status_code == 200 and response.json()["status"] == "moved"
    assert response.json()["result"] == "止损已上移到 820"
    row = stop_row(app)[0]
    assert (row["trigger_price"], row["take_profit_price"], row["status"]) == ("820.0", "830.0", "ARMED")
    card = reminder_cards(app, session.session_id, notice["id"])[-1]
    assert card["breakeven"]["state"] == "moved" and card["actions"] == []
    again = await http.post(f"/api/ai-execution/breakeven/{notice['id']}/move")  # a second click: same answer
    assert again.json()["status"] == "moved" and stop_row(app)[0]["trigger_price"] == "820.0"
    listed = (await http.get("/api/ai-execution/breakeven")).json()["items"]
    assert listed[0]["id"] == notice["id"] and listed[0]["status"] == "moved"
    await http.aclose()
    app.state.store.close()


async def test_move_refuses_when_the_price_is_back_below_the_cost(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-crossed.sqlite3")
    await _check(app)
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    provider.last, provider.quote_datetime = 819.5, quote_now()
    result = await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="move")
    assert result["status"] == "failed" and "已回到成本 820 以下" in result["result"]
    assert stop_row(app)[0]["trigger_price"] == "818.0"  # nothing moved
    # A stale quote leaves the question open instead (409 with the reason).
    app2, provider2, _ = await _setup(tmp_path, "be-stale-move.sqlite3")
    await _check(app2)
    notice2 = app2.state.store.breakeven_notices(user_id=USER)[0]
    provider2.age_seconds = 40.0
    http = await login(app2)
    response = await http.post(f"/api/ai-execution/breakeven/{notice2['id']}/move")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "quote_not_current"
    assert app2.state.store.breakeven_notice(notice2["id"])["status"] == "pending"
    await http.aclose()
    app.state.store.close()
    app2.state.store.close()


async def test_decline_ends_the_days_question_and_survives_a_restart(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-decline.sqlite3")
    await _check(app)
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    result = await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="decline")
    assert result["status"] == "declined"
    assert stop_row(app)[0]["trigger_price"] == "818.0"
    provider.last, provider.quote_datetime = 825.0, quote_now()
    assert await _check(app) == 0
    path = Path(app.state.store.path) if hasattr(app.state.store, "path") else tmp_path / "be-decline.sqlite3"
    app.state.store.close()
    reopened = application(path, Gateway(), provider)
    assert await _check(reopened) == 0  # the ledger is in the database: a restart never re-asks
    reopened.state.store.close()


async def test_switch_off_asks_nothing_and_the_push_follows_its_switch(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-off.sqlite3")
    _set_rule(app, breakEven=False)
    assert await _check(app) == 0
    _set_rule(app, breakEven=True)
    sender = configure(app)
    http = await login(app)
    assert (await http.put("/api/app/push/subscription", json=body("phone-0001", APPLE))).status_code == 200
    assert await _check(app) == 1
    await app.state.push.drain()
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    assert [(payload["kind"], payload["url"], payload["title"]) for _, payload in sender.sent] == [
        ("breakeven", f"?open=card:reminder-{notice['id']}", "保本提醒 · 沪金2612")]
    defaults = app.state.store.user_preferences(USER).model_dump()
    assert kind_enabled(defaults, "breakeven") is True
    defaults["notify"]["breakEven"] = False
    assert kind_enabled(defaults, "breakeven") is False
    message = breakeven_message(name="沪金", body="浮盈已超过止损距离", card_id="reminder-x")
    assert message.short == "沪金的浮盈够了，要不要把止损移到成本？"  # no price without amounts
    await http.aclose()
    app.state.store.close()


async def test_spoken_answers_need_the_read_out_the_window_and_no_waiting_card(tmp_path: Path) -> None:
    import time

    from trader_runtime.voice_realtime import CallState, VoiceRealtimeService, breakeven_route
    # Only explicit words answer; "好" goes to the agent like any other sentence and moves nothing.
    for said in ("移", "移吧", "移到成本", "保本"):
        assert breakeven_route(said, pending=False, heard=True) == "breakeven_move", said
    for said in ("不用", "不移"):
        assert breakeven_route(said, pending=False, heard=True) == "breakeven_decline", said
    for said in ("好", "好的", "可以", "移到3100", "算了"):
        assert breakeven_route(said, pending=False, heard=True) is None, said
    # A proposal card waiting for 确认 comes first: 移 is not taken by the 保本提醒 then.
    assert breakeven_route("移", pending=True, heard=True) is None
    assert breakeven_route("移", pending=False, heard=False) is None
    app, provider, session = await _setup(tmp_path, "be-voice.sqlite3")
    await _check(app)
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    service = VoiceRealtimeService(app, key_file=tmp_path / "missing-key")
    state = CallState(user_id=USER)
    assert service.breakeven_target(state) is None  # not read out on this call: the agent decides
    state.breakeven_heard = {"id": notice["id"], "at": time.monotonic()}
    assert service.breakeven_target(state)["id"] == notice["id"]
    window = state.preferences.voice.confirmWindowSeconds
    state.breakeven_heard = {"id": notice["id"], "at": time.monotonic() - window - 1}
    assert service.breakeven_target(state) is None  # the confirm window is over: 移 no longer moves
    state.breakeven_heard = {"id": notice["id"], "at": time.monotonic()}
    await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="decline")
    assert service.breakeven_target(state) is None  # already answered
    app.state.store.close()


async def test_the_card_names_a_strategys_protection_that_stays(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-strategy-note.sqlite3")
    services = app.state.accounts.default
    await services.autonomy.create_stop(
        user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
        position_bucket="today", volume=1, trigger_price=817.0)
    second = [row for row in stop_row(app) if row["trigger_price"] == "817.0"][0]["id"]
    app.state.store.connection.execute("UPDATE ai_stop_orders SET strategy_metadata_json = ? WHERE id = ?",
                                       (json.dumps({"strategyId": "s-1", "entryPrice": "820"}), second))
    app.state.store.connection.commit()
    assert await _check(app) == 1
    card = reminder_cards(app, session.session_id, app.state.store.breakeven_notices(user_id=USER)[0]["id"])[0]
    assert card["note"].startswith("策略「策略」的保护不受影响")
    app.state.store.close()


# -- 旧卡的稳定目标绑定 (#528): 卡只动它问过的那几行止损，换代/换依据都拒 -------------------------------------


def _reopen(provider: Provider, *, b_cost: float) -> None:
    """A went flat between two snapshots and B (same symbol+direction) opened at `b_cost`."""
    provider.trades[:0] = [
        {"tradeId": "open-b", "clientOrderId": "manual-open-b", "symbol": "SHFE.au2612", "direction": "BUY",
         "offset": "OPEN", "volume": 2, "price": b_cost, "tradeDateTime": 3, "tradingDay": provider.day},
        {"tradeId": "close-a", "clientOrderId": "manual-close-a", "symbol": "SHFE.au2612", "direction": "SELL",
         "offset": "CLOSETODAY", "volume": 2, "price": 823.0, "tradeDateTime": 2, "tradingDay": provider.day}]
    provider.position_items = _long(open_price=b_cost)
    provider.last, provider.quote_datetime = 828.0, quote_now()


@pytest.mark.parametrize("clamp_first,b_cost", [(True, 825.0), (True, 820.0), (False, 825.0)])
async def test_an_old_card_never_moves_a_reopened_positions_stop(
        tmp_path: Path, clamp_first: bool, b_cost: float) -> None:
    """#528: the card was asked about position A; A went flat and B opened later on the same symbol and
    direction. Whether or not the background clamp has retired A's stop yet, the old card must not
    touch B's new stop — even when B's cost equals A's (the target is the bound rows, not a price)."""
    app, provider, session = await _setup(tmp_path, "be-reopen.sqlite3")
    await _check(app)
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    _reopen(provider, b_cost=b_cost)
    if clamp_first:
        await app.state.autonomy.run_once()  # #603 retires A's stop (anchor_position_closed)
        assert stop_row(app)[0]["status"] == "CANCELLED"
    await app.state.autonomy.create_stop(  # B arms its own new stop
        user_id=USER, session_id=session.session_id, symbol="SHFE.au2612", position_direction="long",
        position_bucket="today", volume=2, trigger_price=819.0, take_profit_price=None)
    result = await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="move")
    statuses = {row["status"] for row in stop_row(app)}
    armed = [row for row in stop_row(app) if row["status"] == "ARMED"]
    assert result["status"] == "failed" and "没移" in result["result"]
    assert [row["trigger_price"] for row in armed] == ["819.0"]  # B's stop is exactly what B armed
    if not clamp_first:  # the answer's own verification retired the stale row, not the card
        assert statuses == {"CANCELLED", "ARMED"}
    app.state.store.close()


async def test_a_card_from_before_the_binding_fails_closed_but_decline_still_works(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-legacy.sqlite3")
    await _check(app)
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    card = dict(notice["card"])
    state = dict(card.get("breakeven") or {})
    state.pop("stopIds", None), state.pop("triggers", None)  # a card made before #528 bound nothing
    with app.state.store.lock, app.state.store.connection:
        app.state.store.connection.execute(
            "UPDATE breakeven_notices SET card_json = ? WHERE id = ?",
            (json.dumps({**card, "breakeven": state}, ensure_ascii=False), notice["id"]))
    result = await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="move")
    assert result["status"] == "failed" and "没有绑定" in result["result"]
    assert (stop_row(app)[0]["trigger_price"], stop_row(app)[0]["status"]) == ("818.0", "ARMED")
    app.state.store.close()
    app, provider, session = await _setup(tmp_path, "be-legacy-decline.sqlite3")
    await _check(app)
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    card, state = dict(notice["card"]), dict((notice["card"]).get("breakeven") or {})
    state.pop("stopIds", None), state.pop("triggers", None)
    with app.state.store.lock, app.state.store.connection:
        app.state.store.connection.execute(
            "UPDATE breakeven_notices SET card_json = ? WHERE id = ?",
            (json.dumps({**card, "breakeven": state}, ensure_ascii=False), notice["id"]))
    assert (await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="decline"))["status"] == "declined"
    app.state.store.close()


async def test_a_bound_row_without_an_anchor_is_refused_and_never_rebased(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-noanchor.sqlite3")
    await _check(app)
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    with app.state.store.lock, app.state.store.connection:
        app.state.store.connection.execute(  # a pre-#528 row carries no proof of its position
            "UPDATE ai_stop_orders SET anchor_volume = NULL, fill_watermark_json = NULL")
    result = await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="move")
    row = stop_row(app)[0]
    assert result["status"] == "failed" and "持仓锚点" in result["result"]
    assert row["status"] == "ARMED" and row["trigger_price"] == "818.0"
    assert row["anchor_volume"] is None and row["fill_watermark_json"] is None  # not rebased either
    app.state.store.close()


@pytest.mark.parametrize("sync_fault", [None, "invalid_fill", "missing_source"])
async def test_the_card_still_moves_entry_fill_rows_of_the_same_target(tmp_path: Path, sync_fault) -> None:
    """The binding follows a manual open card's fill-armed protection too: same target, sync first,
    then the move — 止盈不动."""
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / "be-manual.sqlite3", gateway, provider)
    provider.position_items = _long(open_price=823.0)
    provider.last, provider.quote_datetime = 823.0, quote_now()
    session = app.state.broker.create_session("local", "native", USER)
    card = app.state.proposals.create_trade(
        session.session_id, "turn-manual", symbol="SHFE.au2612", side="buy_open", volume=2,
        price_intent="passive", reason="开仓带保护", protection={"stopLossPrice": 822.0, "takeProfitPrice": 825.0})
    await confirm(app, card, "be-manual")
    request = dict(gateway.request)
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=0)
    provider.order_reports = [{**request, "status": "FINISHED", "volumeLeft": 0}]
    fill_entry(provider, request, trade_id="open-1", volume=2, price=823.0)
    await app.state.simulation_orders.query(request["clientOrderId"], user_id=USER)
    app.state.autonomy.sync_strategy_protection()
    provider.last, provider.quote_datetime = 824.0, quote_now()  # profit 1 ≥ 1 R
    assert await _check(app) == 1
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    if sync_fault == "invalid_fill":
        fill_entry(provider, request, trade_id="invalid-extra-fill", volume=0, price=823.0, time=5)
    elif sync_fault == "missing_source":
        with app.state.store.transaction() as db:
            db.execute("UPDATE tqkq_sim_order_links SET protection_json = NULL")
    if sync_fault == "invalid_fill":
        for _ in range(2):
            with pytest.raises(breakeven.BreakEvenAnswerError) as raised:
                await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="move")
            assert raised.value.code == "activity_not_current"
        assert app.state.store.breakeven_notice(notice["id"])["status"] == "pending"
        row = stop_row(app)[0]
        assert row["trigger_price"] == "822.0" and row["take_profit_price"] == "825.0"
        app.state.store.close()
        return
    result = await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="move")
    row = stop_row(app)[0]
    if sync_fault:
        assert result["status"] == "failed"
        assert row["trigger_price"] == "822.0" and row["take_profit_price"] == "825.0"
        assert await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="move") == result
        app.state.store.close()
        return
    assert result["status"] == "moved" and result["result"] == "止损已上移到 823"
    assert (row["trigger_price"], row["take_profit_price"]) == ("823.0", "825.0")
    assert json.loads(row["strategy_metadata_json"])["source"] == "manual"
    app.state.store.close()


async def test_an_overnight_bucket_roll_is_still_the_same_target(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-roll.sqlite3")
    await _check(app)
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    with app.state.store.lock, app.state.store.connection:
        app.state.store.connection.execute(  # armed on yesterday's account day: today the lots are 昨仓
            "UPDATE ai_stop_orders SET created_at = '2026-08-24T01:00:00+00:00', account_trading_day = '2026-08-24'")
    provider.position_items = [{**_long()[0], "todayVolume": 0, "historicalVolume": 2}]
    provider.last, provider.quote_datetime = 824.0, quote_now()
    await app.state.autonomy.run_once()  # the clamp rolls the row today→historical; the id survives
    assert stop_row(app)[0]["position_bucket"] == "historical"
    result = await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="move")
    row = stop_row(app)[0]
    assert result["status"] == "moved"
    assert (row["trigger_price"], row["take_profit_price"], row["position_bucket"]) == ("820.0", "830.0", "historical")
    app.state.store.close()


async def test_the_cost_check_reads_the_same_combined_account_cut_as_the_fills(tmp_path: Path, monkeypatch) -> None:
    """#528/#754: 移到成本 checked the card's cost against the poll cache's average while the fills came from a
    fresh read — after an add the stale cache still showed the old 820 and the stop moved to a cost that no
    longer existed. The average now comes from the same combined account read (account=True) as the fills."""
    app, provider, session = await _setup(tmp_path, "be-cut.sqlite3")
    await _check(app)
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    manual_add(provider)
    provider.position_items = _long()  # the poll cache has not caught up with the add: 2 lots at 820
    provider.last, provider.quote_datetime = 824.0, quote_now()
    cuts = lagging_poll_cache(monkeypatch, provider, [{  # the combined read: 4 lots averaging 817
        "symbol": "SHFE.au2612", "direction": "long", "volume": 4, "todayVolume": 4, "historicalVolume": 0,
        "frozenToday": 0, "frozenHistorical": 0, "openPrice": 817.0, "unrealizedPnl": 6000.0}])
    result = await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="move")
    assert stop_row(app)[0]["trigger_price"] == "818.0"  # never moved to the stale 820
    assert result["status"] == "failed" and "价格依据过期" in result["result"], result
    assert cuts == [True]
    app.state.store.close()


async def test_a_changed_cost_basis_or_an_edited_trigger_fails_the_old_card(tmp_path: Path) -> None:
    app, provider, session = await _setup(tmp_path, "be-basis.sqlite3")
    await _check(app)
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    provider.trades.insert(0, {"tradeId": "add-1", "clientOrderId": "manual-add-1", "symbol": "SHFE.au2612",
                               "direction": "BUY", "offset": "OPEN", "volume": 2, "price": 814.0,
                               "tradeDateTime": 3, "tradingDay": provider.day})
    provider.position_items = [  # 4 lots: average (820×2+814×2)/4 = 817
        {"symbol": "SHFE.au2612", "direction": "long", "volume": 4, "todayVolume": 4, "historicalVolume": 0,
         "frozenToday": 0, "frozenHistorical": 0, "openPrice": 817.0, "unrealizedPnl": 6000.0}]
    provider.last, provider.quote_datetime = 824.0, quote_now()
    result = await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="move")
    assert result["status"] == "failed" and "价格依据过期" in result["result"]
    assert stop_row(app)[0]["trigger_price"] == "818.0"  # the old basis is never silently applied
    app.state.store.close()
    app, provider, session = await _setup(tmp_path, "be-edited.sqlite3")
    await _check(app)
    notice = app.state.store.breakeven_notices(user_id=USER)[0]
    with app.state.store.lock, app.state.store.connection:
        app.state.store.connection.execute("UPDATE ai_stop_orders SET trigger_price = '817.5'")  # user edited it
    result = await breakeven.answer(app, user_id=USER, notice_id=notice["id"], choice="move")
    assert result["status"] == "failed" and "改过价位" in result["result"]
    assert stop_row(app)[0]["trigger_price"] == "817.5"
    app.state.store.close()

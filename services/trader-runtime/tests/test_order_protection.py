"""下单顺带保护 (2026-10-02): an open card can carry the user's own stop / take (validated against
the card when it is made, arming on the fill through the order link — partial fills arm their own
volume, no fill arms nothing); stops themselves accept either level alone; a trigger notifies the
order-time session and a live call; the activity feed and cancellation read one account's book only."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from fixtures.card_tools import legacy_card_args
from test_account_registry import BETA_ACCOUNT, DEFAULT_ACCOUNT
from test_account_roles import beta_session, roles_app
from test_simulation_orders import (
    USER,
    Gateway,
    NormalProvider,
    _tool_status,
    _tool_text,
    application,
    confirm,
    quote_now,
)
from trader_runtime.autonomy import AutonomyError
from trader_runtime.proposals import ProposalConflict
from trader_runtime.trader_tools import TraderReadOnlyTools


class Provider(NormalProvider):
    """An editable activity feed and a trading day, so a confirmed card's own fill can arm the
    protection riding its order link (the chart-side fixture of the fill-backed sync)."""

    def __init__(self) -> None:
        super().__init__()
        self.day = "2026-08-25"
        self.trades: list = []
        self.order_reports: list = []

    def account_activity(self) -> dict:
        return {**super().account_activity(), "orders": self.order_reports, "trades": self.trades}

    def account_summary(self, turn_id: str) -> dict:
        return {**super().account_summary(turn_id), "tradingDay": self.day}


def open_card(app, *, volume: int = 1, stop=None, take=None, side: str = "buy_open",
              turn: str = "turn-protect") -> dict:
    """A manual open card, optionally carrying the user's stop / take (a passive buy prices at the
    bid 823.0, so a long card's valid levels are stop < 823 < take on the 0.5 tick grid)."""
    session = app.state.broker.create_session("local", "native", USER)
    return app.state.proposals.create_trade(
        session.session_id, turn, symbol="SHFE.au2612", side=side, volume=volume,
        price_intent="passive", reason="下单顺带保护测试",
        protection={"stopLossPrice": stop, "takeProfitPrice": take},
    )


def fill_entry(provider: Provider, request: dict, *, trade_id: str = "fill-1",
               volume: int = 1, price: float = 823.0, time: int = 1) -> dict:
    trade = {"tradeId": trade_id, "clientOrderId": request["clientOrderId"], "symbol": request["symbol"],
             "direction": request["direction"], "offset": request["offset"], "volume": volume,
             "price": price, "tradeDateTime": time, "tradingDay": provider.day}
    provider.trades.insert(0, trade)
    return trade


def manual_add(provider: Provider, *, volume: int = 2, price: float = 814.0, trade_id: str = "add-1") -> None:
    """The user added to the long by hand: the fill is in the account's report."""
    provider.trades.insert(0, {"tradeId": trade_id, "clientOrderId": f"manual-{trade_id}", "symbol": "SHFE.au2612",
                               "direction": "BUY", "offset": "OPEN", "volume": volume, "price": price,
                               "tradeDateTime": 3, "tradingDay": provider.day})


def lagging_poll_cache(monkeypatch, provider: Provider, current: list[dict]) -> list[bool]:
    """#528/#754: the poll cache (`provider.position_items`) has not caught up with an add, while the combined
    account read (`provider_read_snapshot(account=True)`) already shows `current` — the positions as of the same
    moment as the fills. Returns the `account` flag of every snapshot autonomy opened."""
    import contextlib

    from trader_runtime import autonomy as autonomy_module
    cuts: list[bool] = []

    @contextlib.asynccontextmanager
    async def snapshot(source, *, account=False, **_kwargs):
        cuts.append(account)
        stale = source.position_items
        if account:
            source.position_items = current
        try:
            yield
        finally:
            source.position_items = stale

    monkeypatch.setattr(autonomy_module, "provider_read_snapshot", snapshot)
    return cuts


async def test_a_new_stop_anchors_on_the_combined_read_not_the_lagging_poll_cache(tmp_path: Path, monkeypatch) -> None:
    """#754 (same root as #528): arming a direct stop paired a fresh fill report with the poll cache's positions
    from before an add — the 4 lots now held could not be protected, and the anchor recorded 2 lots behind a
    watermark that already holds the add. The stop now reads both from one combined account read."""
    app = application(tmp_path / "anchor-cut.sqlite3", Gateway(), Provider())
    provider = app.state.data_provider
    provider.position_items = _long_items(2, today=2)
    manual_add(provider)
    cuts = lagging_poll_cache(monkeypatch, provider, _long_items(4, today=4))
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=4)
    row = stop_row(app, stop["stopId"])[0]
    assert (int(row["volume"]), int(row["anchor_volume"])) == (4, 4)
    assert "add-1" in json.dumps(json.loads(row["fill_watermark_json"]))
    assert cuts == [True]
    app.state.store.close()


async def armed_card(tmp_path: Path, *, volume: int = 1, stop=822.5, take=823.5,
                     filled: int | None = None, name: str = "protection.sqlite3"):
    """A confirmed open card whose order link carries its protection, then the fill reported and the
    sync run — the state every fill-side assertion below starts from."""
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / name, gateway, provider)
    session = app.state.broker.create_session("local", "native", USER)
    card = app.state.proposals.create_trade(
        session.session_id, "turn-protect", symbol="SHFE.au2612", side="buy_open", volume=volume,
        price_intent="passive", reason="下单顺带保护测试",
        protection={"stopLossPrice": stop, "takeProfitPrice": take})
    await confirm(app, card, "confirm-protect")
    request = dict(gateway.request)
    traded = volume if filled is None else filled
    provider.order_reports = [{**request, "status": "FINISHED", "volumeLeft": volume - traded}]
    if traded:
        fill_entry(provider, request, volume=traded)
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=volume - traded)
    await app.state.simulation_orders.query(request["clientOrderId"], user_id=USER)
    app.state.autonomy.sync_strategy_protection()
    return app, provider, gateway, session, request


def stop_row(app, stop_id: str | None = None):
    sql = "SELECT * FROM ai_stop_orders" + (" WHERE id = ?" if stop_id else "")
    return app.state.store.connection.execute(sql, () if stop_id is None else (stop_id,)).fetchall()


def test_card_validates_the_users_levels_against_its_own_price(tmp_path: Path) -> None:
    app = application(tmp_path / "card-rules.sqlite3", Gateway(), Provider())

    def refused(**levels) -> tuple[str, str]:
        with pytest.raises(ProposalConflict) as raised:
            open_card(app, **levels)
        return str(raised.value), str(raised.value.code)

    message, code = refused(stop=823.5)  # a long stop at/above the bid 823
    assert code == "protection_stop_wrong_side" and "止损要低于委托价 823" in message
    message, code = refused(take=823.0)  # a long take at the bid
    assert code == "protection_take_wrong_side" and "止盈要高于委托价 823" in message
    message, code = refused(stop=822.3)  # off the 0.5 tick grid
    assert code == "protection_price_invalid" and "最小变动价位" in message
    # A close card never carries protection — it reduces a position, it does not open one.
    session = app.state.broker.create_session("local", "native", USER)
    with pytest.raises(ProposalConflict) as raised:
        app.state.proposals.create_trade(
            session.session_id, "turn-close-protect", symbol="SHFE.au2612", side="sell_close",
            volume=1, price_intent="passive", position_bucket="today", reason="平仓不带保护",
            protection={"stopLossPrice": 822.5, "takeProfitPrice": None})
    assert str(raised.value).startswith("只有开仓能顺带挂止损/止盈")

    card = open_card(app, stop=822.5, take=823.5)
    # The card carries the normalized levels, and the user's own stop replaces the budget line.
    assert card["protection"] == {"stopLossPrice": "822.5", "takeProfitPrice": "823.5"}
    assert card["stopPrice"] == 822.5
    # Short mirrors every rule against the ask-priced (824.0) sell card.
    session = app.state.broker.create_session("local", "native", USER)
    with pytest.raises(ProposalConflict) as raised:
        app.state.proposals.create_trade(
            session.session_id, "turn-short", symbol="SHFE.au2612", side="sell_open", volume=1,
            price_intent="passive", reason="空单保护方向", protection={"stopLossPrice": 823.5,
                                                                      "takeProfitPrice": None})
    assert "止损要高于委托价 824" in str(raised.value)
    app.state.store.close()


async def test_a_full_fill_arms_the_cards_protection_as_given(tmp_path: Path) -> None:
    app, _, gateway, _, request = await armed_card(tmp_path)
    rows = stop_row(app)
    assert len(rows) == 1
    stop = app.state.autonomy.list_stops(user_id=USER)[0]
    assert stop["status"] == "ARMED"
    assert stop["triggerPrice"] == 822.5 and stop["takeProfitPrice"] == 823.5
    assert stop["volume"] == 1 and stop["remainingVolume"] == 1
    # A manual protection is not a strategy's: the public row says where it came from.
    assert stop["source"] == "manual" and stop["strategyId"] is None
    link = app.state.store.connection.execute(
        "SELECT protection_json FROM tqkq_sim_order_links WHERE client_order_id = ?",
        (request["clientOrderId"],)).fetchone()
    config = json.loads(link["protection_json"])
    assert config["source"] == "manual"
    assert config["fixedStopPrice"] == "822.5" and config["fixedTakeProfitPrice"] == "823.5"
    assert gateway.submit_count == 1  # the entry order only; the protection itself arms silently
    app.state.store.close()


async def test_a_partial_fill_arms_partial_volume_and_resync_adds_nothing(tmp_path: Path) -> None:
    app, provider, _, _, request = await armed_card(tmp_path, volume=2, filled=1,
                                                    name="partial.sqlite3")
    stops = app.state.autonomy.list_stops(user_id=USER)
    assert len(stops) == 1 and stops[0]["volume"] == 1 and stops[0]["remainingVolume"] == 1
    app.state.autonomy.sync_strategy_protection()  # a repeated sync never re-arms the same fill
    assert len(stop_row(app)) == 1
    fill_entry(provider, request, trade_id="fill-2", volume=1, time=2)
    app.state.autonomy.sync_strategy_protection()
    stops = app.state.autonomy.list_stops(user_id=USER)
    assert len(stops) == 2  # the second fill arms its own row, same fixed levels
    assert {stop["triggerPrice"] for stop in stops} == {822.5}
    app.state.store.close()


async def test_a_cancelled_card_arms_nothing(tmp_path: Path) -> None:
    app, _, _, _, _ = await armed_card(tmp_path, filled=0, name="cancelled.sqlite3")
    assert app.state.autonomy.list_stops(user_id=USER) == []
    app.state.store.close()


async def test_take_profit_only_and_stop_only_stops(tmp_path: Path) -> None:
    app = application(tmp_path / "levels.sqlite3", Gateway(), Provider())
    provider = app.state.data_provider
    provider.last = 823.0
    provider.quote_datetime = quote_now()
    session = app.state.broker.create_session("local", "native", USER)

    async def arm(**levels) -> dict:
        return await app.state.autonomy.create_stop(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612",
            position_direction="long", position_bucket="today", volume=1, **levels)

    take_only = await arm(take_profit_price=823.5)
    assert take_only["triggerPrice"] is None and take_only["takeProfitPrice"] == 823.5
    assert take_only["source"] == "manual"
    both = await arm(trigger_price=822.5, take_profit_price=823.5)
    assert both["triggerPrice"] == 822.5 and both["takeProfitPrice"] == 823.5
    stop_only = await arm(trigger_price=822.0)
    assert stop_only["triggerPrice"] == 822.0 and stop_only["takeProfitPrice"] is None

    async def refused(code: str, fragment: str, **levels) -> None:
        with pytest.raises(AutonomyError) as raised:
            await arm(**levels)
        assert str(raised.value) == code
        assert fragment in str(raised.value.message)

    await refused("stop_price_required", "止损价和止盈价至少填一个")
    await refused("long_stop_must_be_below_market", "止损要低于现价 823", trigger_price=823.5)
    await refused("long_take_profit_must_be_above_market", "止盈要高于现价 823", take_profit_price=822.5)
    await refused("invalid_stop_price", "最小变动价位", trigger_price=822.3)
    short_session = app.state.broker.create_session("local", "native", USER)
    with pytest.raises(AutonomyError) as raised:
        await app.state.autonomy.create_stop(
            user_id=USER, session_id=short_session.session_id, symbol="SHFE.au2612",
            position_direction="short", position_bucket="today", volume=1, trigger_price=822.5)
    assert str(raised.value) == "short_stop_must_be_above_market"
    assert "止损要高于现价 823" in str(raised.value.message)
    app.state.store.close()


async def test_the_stop_route_answers_a_bad_direction_with_the_chinese_reason(tmp_path: Path) -> None:
    app = application(tmp_path / "stop-route.sqlite3", Gateway(), Provider())
    provider = app.state.data_provider
    provider.last = 823.0
    provider.quote_datetime = quote_now()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as http:
        await http.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})
        session = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
        body = {"sessionId": session["sessionId"], "symbol": "SHFE.au2612",
                "positionDirection": "long", "positionBucket": "today", "volume": 1,
                "triggerPrice": 823.5}
        refused = await http.post("/api/ai-execution/stops", json=body)
        assert refused.status_code == 422  # a validation refusal, not a conflict
        assert refused.json()["detail"] == {
            "code": "long_stop_must_be_below_market", "message": "止损要低于现价 823"}
        body.pop("triggerPrice")
        body["takeProfitPrice"] = 823.5
        made = await http.post("/api/ai-execution/stops", json=body)
        assert made.status_code == 201
        assert made.json()["triggerPrice"] is None
        assert made.json()["takeProfitPrice"] == 823.5
    app.state.store.close()


async def test_a_take_profit_trigger_closes_once(tmp_path: Path) -> None:
    app = application(tmp_path / "take-trigger.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.last = 823.0
    provider.quote_datetime = quote_now()
    session = app.state.broker.create_session("local", "native", USER)
    stop = await app.state.autonomy.create_stop(
        user_id=USER, session_id=session.session_id, symbol="SHFE.au2612",
        position_direction="long", position_bucket="today", volume=1, take_profit_price=823.5)
    provider.last = 825.0  # a long take fires at or above its level
    await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
    fired = app.state.autonomy.list_stops(user_id=USER)[0]
    assert fired["status"] == "SUBMITTED", fired["lastError"]
    assert gateway.submit_count == 1
    assert gateway.request["offset"] == "CLOSETODAY" and gateway.request["volume"] == 1
    await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
    assert gateway.submit_count == 1  # the crossing is never traded twice
    app.state.store.close()


async def test_a_trigger_notifies_the_order_session_and_a_live_call(tmp_path: Path) -> None:
    app, provider, gateway, session, _ = await armed_card(tmp_path, name="notify.sqlite3")
    session_id = session.session_id
    app.state.broker.subscribers[session_id] = 1  # an attached SSE reader
    app.state.voice_realtime.calls["test-call"] = SimpleNamespace(user_id=USER)
    provider.position_items = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 1,
                                "todayVolume": 1, "historicalVolume": 0,
                                "frozenToday": 0, "frozenHistorical": 0}]
    stop = app.state.autonomy.list_stops(user_id=USER)[0]
    provider.last = 820.5  # below the armed 822.5 stop
    provider.quote_datetime = quote_now()
    await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])

    notices = [event.get("payload") for _, event in app.state.store.list_events(session_id)
               if event.get("name") == "message.completed"
               and str(event.get("payload", {}).get("turnId", "")).startswith("protection-")]
    assert len(notices) == 1
    assert notices[0]["turnId"] == f"protection-{stop['stopId']}"
    assert "1 手止损触发" in str(notices[0]["text"]) and "平仓单" in str(notices[0]["text"])
    cards = reminder_cards(app, session_id, f"protection-{stop['stopId']}")
    assert len(cards) == 1 and cards[0]["kind"] == "protection" and cards[0]["title"] == "止损触发"
    figures = {f["label"]: f["value"] for f in cards[0]["figures"]}
    assert figures["触发价"] == "822.5" and figures["最新价"] == "820.5" and figures["手数"] == "1 手"
    assert figures["平仓单"]  # the close order's read-back, whatever it reads (结果未知 stays 结果未知)
    assert_no_order_action(cards[0])
    announcements = app.state.voice_realtime._pending_announcements[USER]
    assert announcements[-1]["kind"] == f"protection-{stop['stopId']}"
    assert "止损触发" in announcements[-1]["text"]

    # A take-only protection says 止盈 in the same one line.
    provider2 = Provider()
    app2 = application(tmp_path / "notify-take.sqlite3", Gateway(), provider2)
    provider2.last = 823.0
    provider2.quote_datetime = quote_now()
    session2 = app2.state.broker.create_session("local", "native", USER)
    take = await app2.state.autonomy.create_stop(
        user_id=USER, session_id=session2.session_id, symbol="SHFE.au2612",
        position_direction="long", position_bucket="today", volume=1, take_profit_price=823.5)
    provider2.last = 824.0
    await app2.state.autonomy._check_stop(stop_row(app2, take["stopId"])[0])
    notices2 = [event.get("payload") for _, event in app2.state.store.list_events(session2.session_id)
                if event.get("name") == "message.completed"]
    assert len(notices2) == 1 and "1 手止盈触发" in str(notices2[0]["text"])
    assert gateway.submit_count == 2  # the first app: its entry order plus the one protective close
    app.state.store.close()
    app2.state.store.close()


async def test_a_trigger_cards_close_order_figure_follows_the_order_to_its_fill(tmp_path: Path) -> None:
    """#932: the 止损/止盈触发 card's 平仓单 reads 已报 when the close goes out and is republished
    under the same cardId once the close order reads filled — not left at 已报."""
    app = application(tmp_path / "trigger-follow.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.last = 823.0
    provider.quote_datetime = quote_now()
    session = app.state.broker.create_session("local", "native", USER)
    stop = await app.state.autonomy.create_stop(
        user_id=USER, session_id=session.session_id, symbol="SHFE.au2612",
        position_direction="long", position_bucket="today", volume=1, trigger_price=822.5)
    provider.last = 820.5
    provider.quote_datetime = quote_now()
    await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
    turn_id = f"protection-{stop['stopId']}"
    first = reminder_cards(app, session.session_id, turn_id)
    assert len(first) == 1 and {f["label"]: f["value"] for f in first[0]["figures"]}["平仓单"] == "已报"

    tracker = app.state.broker.execution_cards
    assert first[0]["cardId"] in tracker.tracking()
    tracker._cards[first[0]["cardId"]].registered -= 10  # past the producer's own publish
    await tracker.tick()
    assert len(reminder_cards(app, session.session_id, turn_id)) == 1  # unchanged read-back: no republish

    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await tracker.tick()
    cards = reminder_cards(app, session.session_id, turn_id)
    assert len(cards) == 2 and cards[-1]["cardId"] == first[0]["cardId"]
    figure = next(f for f in cards[-1]["figures"] if f["label"] == "平仓单")
    assert figure == {"label": "平仓单", "value": "已成交", "tone": "positive"}
    assert {f["label"] for f in cards[-1]["figures"]} == {f["label"] for f in first[0]["figures"]}
    assert first[0]["cardId"] not in tracker.tracking()  # terminal: no more reads
    # The 提醒中心 row of the trigger follows the same read-back (#932).
    from trader_runtime import reminders
    [item] = app.state.store.reminders_page(USER, trading_day=reminders.current_trading_day(), scope="today")[0]
    assert item["kind"] == "stop_triggered" and item["target"]["cardId"] == first[0]["cardId"]
    assert item["state"]["tone"] == "ok" and item["state"]["text"].startswith("平仓单 已成交")
    app.state.store.close()


async def test_a_trigger_with_its_reminder_switched_off_says_nothing_anywhere(tmp_path: Path) -> None:
    """#932: 止损止盈触发 off — the close still goes out; no message, no card, no line, no 提醒中心 row."""
    app = application(tmp_path / "trigger-off.sqlite3", Gateway(), Provider())
    provider = app.state.data_provider
    provider.last = 823.0
    provider.quote_datetime = quote_now()
    app.state.store.update_preferences(USER, {"notify": {"stopTriggered": False}})
    session = app.state.broker.create_session("local", "native", USER)
    app.state.voice_realtime.calls["test-call"] = SimpleNamespace(user_id=USER)
    stop = await app.state.autonomy.create_stop(
        user_id=USER, session_id=session.session_id, symbol="SHFE.au2612",
        position_direction="long", position_bucket="today", volume=1, trigger_price=822.5)
    provider.last = 820.5
    provider.quote_datetime = quote_now()
    await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
    assert stop_row(app, stop["stopId"])[0]["status"] != "ARMED"   # the close went out all the same
    assert reminder_cards(app, session.session_id, f"protection-{stop['stopId']}") == []
    assert not app.state.voice_realtime._pending_announcements.get(USER)
    assert app.state.store.reminders_page(USER, trading_day="9999-12-31", scope="earlier")[0] == []
    app.state.store.close()


async def test_stops_read_and_cancel_through_one_account_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, _ = roles_app(tmp_path, monkeypatch, "isolation.sqlite3", beta_role="owner", beta_owner=None)
    try:
        provider = app.state.accounts.get(DEFAULT_ACCOUNT).provider
        provider.last = 823.0
        provider.quote_datetime = quote_now()
        session_id = app.state.broker.create_session("local", "native", USER).session_id
        stop = await app.state.autonomy.create_stop(
            user_id=USER, session_id=session_id, symbol="SHFE.au2612",
            position_direction="long", position_bucket="today", volume=1, trigger_price=822.5)
        from trader_runtime.agent_activity import activity_snapshot
        activity = {"trades": [], "orders": []}
        own = activity_snapshot(app, USER, "", activity)
        assert [item["stopId"] for item in own["protections"]] == [stop["stopId"]]
        other = activity_snapshot(app, USER, "", activity, trading_account_id=BETA_ACCOUNT)
        assert other["protections"] == []  # 乙's feed never lists 甲's armed stop
        # And cancel is scoped the same way: 乙's autonomy cannot touch 甲's row.
        autonomy_b = app.state.accounts.get(BETA_ACCOUNT).autonomy
        beta_session(app)
        with pytest.raises(KeyError):
            autonomy_b.cancel_stop(user_id=USER, stop_id=stop["stopId"])
        assert app.state.autonomy.list_stops(user_id=USER)[0]["status"] == "ARMED"
    finally:
        app.state.store.close()


def test_the_tools_carry_the_protection_through_the_catalog(tmp_path: Path) -> None:
    app = application(tmp_path / "tools.sqlite3", Gateway(), Provider())
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(
        app.state.data_provider, _tool_status(app), proposals=app.state.proposals,
        autonomy=app.state.autonomy)
    definitions = {item["name"]: item for item in tools.definitions()}
    trade_schema = definitions["propose_order"]["inputSchema"]
    assert {"protection", "remainingProtection"} <= set(trade_schema["properties"])
    stop_schema = definitions["set_simulation_stop_loss"]["inputSchema"]
    assert set(stop_schema["properties"]) >= {"triggerPrice", "takeProfitPrice"}

    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1,
                   "priceIntent": "passive", "reason": "开仓顺带保护",
                   "protection": {"stopLossPrice": 822.5, "takeProfitPrice": 823.5}}),
        turn_id="turn-tool-protect", mode="local", session_id=session.session_id)
    assert result["success"] is True
    assert event is not None and event[1]["protection"] == {
        "stopLossPrice": "822.5", "takeProfitPrice": "823.5"}
    # A wrong level comes back as the tool's own refusal line, not a thrown error.
    refused, none = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1,
                   "priceIntent": "passive", "reason": "止损方向不对",
                   "protection": {"stopLossPrice": 823.5}}),
        turn_id="turn-tool-refused", mode="local", session_id=session.session_id)
    assert none is None
    payload = _tool_text(refused)
    assert payload["errorCode"] == "protection_stop_wrong_side"
    assert payload["data"]["userFacing"]["candidateIssues"] == ["止损要低于委托价 823"]
    app.state.store.close()


async def _arm_on(
    app, services, gateway, session_id: str, *, turn: str, key: str, time: int,
    sync: bool = True,
) -> dict:
    """同一用户在某个账户上开 2 手并挂上顺带保护，回报成交（跨账户回归的装配段）。
    `sync=False` 留给需要控制同步顺序的用例。"""
    card = app.state.proposals.create_trade(
        session_id, turn, symbol="SHFE.au2612", side="buy_open", volume=2,
        price_intent="passive", reason="跨账户保护回归",
        protection={"stopLossPrice": 822.5, "takeProfitPrice": 823.5})
    await confirm(app, card, key)
    request = dict(gateway.request)
    fill = {"tradeId": f"{turn}-fill", "clientOrderId": request["clientOrderId"],
            "symbol": request["symbol"], "direction": request["direction"],
            "offset": request["offset"], "volume": 2, "price": 823.0,
            "tradeDateTime": time, "tradingDay": "2026-08-25"}
    services.provider.activity_feed = {
        **NormalProvider.account_activity(services.provider),
        "orders": [{**request, "status": "FINISHED", "volumeLeft": 0}],
        "trades": [fill]}
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=0)
    await services.simulation_orders.query(request["clientOrderId"], user_id=USER)
    if sync:
        services.autonomy.sync_strategy_protection()
    return request


def _feed(services, *, orders, trades) -> None:
    services.provider.activity_feed = {
        **NormalProvider.account_activity(services.provider), "orders": orders, "trades": trades}


async def test_one_accounts_close_never_consumes_the_others_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一用户两账户各在 au2612 持 2 手带保护：甲平今 1 手、只同步甲，乙的保护原样 ARMED。"""
    app, gateway_a, gateway_b = roles_app(
        tmp_path, monkeypatch, "dual-close.sqlite3", beta_role="owner", beta_owner=None)
    try:
        services_a = app.state.accounts.get(DEFAULT_ACCOUNT)
        services_b = app.state.accounts.get(BETA_ACCOUNT)
        session_a = app.state.broker.create_session("local", "native", USER).session_id
        # 乙先挂、甲后挂：修复前 _close 不分账户，甲的平仓会先吃掉创建更早的乙那行。
        await _arm_on(app, services_b, gateway_b, beta_session(app),
                                  turn="turn-b", key="confirm-b", time=1)
        request_a = await _arm_on(app, services_a, gateway_a, session_a,
                                  turn="turn-a", key="confirm-a", time=1)
        assert [stop["remainingVolume"] for stop in services_b.autonomy.list_stops(user_id=USER)] == [2]
        assert [stop["remainingVolume"] for stop in services_a.autonomy.list_stops(user_id=USER)] == [2]

        close = {"tradeId": "close-a-1", "clientOrderId": "manual-close-a",
                 "symbol": request_a["symbol"], "direction": "SELL", "offset": "CLOSETODAY",
                 "volume": 1, "price": 823.0, "tradeDateTime": 2, "tradingDay": "2026-08-25"}
        _feed(services_a, orders=[{**request_a, "status": "FINISHED", "volumeLeft": 0}],
              trades=[close])
        services_a.autonomy.sync_strategy_protection()

        own = services_a.autonomy.list_stops(user_id=USER)[0]
        assert own["status"] == "ARMED" and own["remainingVolume"] == 1  # 甲被自己的平仓消耗
        other = services_b.autonomy.list_stops(user_id=USER)[0]
        assert other["status"] == "ARMED" and other["remainingVolume"] == 2  # 乙原样
        assert other["lastError"] is None
        # 甲的平仓成交只落在甲的账本上，乙的 strategy_fill_events 一行不多。
        rows = app.state.store.connection.execute(
            "SELECT trading_account_id FROM strategy_fill_events WHERE client_order_id = ?",
            ("manual-close-a",)).fetchall()
        assert [row["trading_account_id"] for row in rows] == [DEFAULT_ACCOUNT]
    finally:
        app.state.store.close()


async def test_fill_times_of_one_account_do_not_bound_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两账户成交时间交错：乙第 20 秒的成交先同步，甲第 10 秒的更早成交不能被判乱序。"""
    app, gateway_a, gateway_b = roles_app(
        tmp_path, monkeypatch, "dual-times.sqlite3", beta_role="owner", beta_owner=None)
    try:
        services_a = app.state.accounts.get(DEFAULT_ACCOUNT)
        services_b = app.state.accounts.get(BETA_ACCOUNT)
        session_a = app.state.broker.create_session("local", "native", USER).session_id
        await _arm_on(app, services_a, gateway_a, session_a,
                      turn="turn-a", key="confirm-a", time=10, sync=False)
        await _arm_on(app, services_b, gateway_b, beta_session(app),
                      turn="turn-b", key="confirm-b", time=20, sync=False)
        services_b.autonomy.sync_strategy_protection()
        assert services_b.autonomy.list_stops(user_id=USER)[0]["status"] == "ARMED"
        services_a.autonomy.sync_strategy_protection()
        # 修复前成交游标不分账户，甲的 10 秒落在乙的 20 秒之后 → 乱序报错、保护挂不上。
        stop = services_a.autonomy.list_stops(user_id=USER)[0]
        assert stop["status"] == "ARMED" and stop["remainingVolume"] == 2
        assert stop["lastError"] is None
    finally:
        app.state.store.close()


@pytest.mark.parametrize("late_account", [BETA_ACCOUNT, DEFAULT_ACCOUNT])
async def test_gateway_identical_fills_of_two_accounts_no_longer_collide(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_account: str,
) -> None:
    """#453：同一用户两个账户各收到一份网关同号的成交回报（同 clientOrderId + tradeId →
    同 event_id，同一快期账户挂在两个账户位下的形状）。修复前第二份在 (user, event_id)
    主键上 IntegrityError 直接炸穿同步；现在第二份照常入账，两边保护都 ARMED、lastError
    干净，重复同步幂等。两个同步顺序都验。"""
    app, gateway_a, gateway_b = roles_app(
        tmp_path, monkeypatch, "dual-identical-fills.sqlite3", beta_role="owner", beta_owner=None)
    try:
        services = {DEFAULT_ACCOUNT: app.state.accounts.get(DEFAULT_ACCOUNT),
                    BETA_ACCOUNT: app.state.accounts.get(BETA_ACCOUNT)}
        gateways = {DEFAULT_ACCOUNT: gateway_a, BETA_ACCOUNT: gateway_b}
        sessions = {BETA_ACCOUNT: beta_session(app),
                    DEFAULT_ACCOUNT: app.state.broker.create_session("local", "native", USER).session_id}
        early_account = BETA_ACCOUNT if late_account == DEFAULT_ACCOUNT else DEFAULT_ACCOUNT
        await _arm_on(app, services[early_account], gateways[early_account], sessions[early_account],
                      turn="turn-early", key="confirm-early", time=1)
        await _arm_on(app, services[late_account], gateways[late_account], sessions[late_account],
                      turn="turn-late", key="confirm-late", time=1, sync=False)
        # 早账户的开仓成交原样塞进晚账户的回报：同号事件的第二份。
        early_open = next(t for t in services[early_account].provider.account_activity()["trades"]
                          if t["offset"] == "OPEN")
        feed = services[late_account].provider.account_activity()
        services[late_account].provider.activity_feed = {
            **feed, "trades": [*feed["trades"], dict(early_open)]}

        failures = services[late_account].autonomy.sync_strategy_protection()
        assert failures == {}
        event_id = f"{early_open['clientOrderId']}:{early_open['tradeId']}"
        rows = app.state.store.connection.execute(
            "SELECT trading_account_id FROM strategy_fill_events WHERE event_id = ?",
            (event_id,)).fetchall()
        assert sorted(row["trading_account_id"] for row in rows) == sorted(
            [DEFAULT_ACCOUNT, BETA_ACCOUNT])
        for account in (DEFAULT_ACCOUNT, BETA_ACCOUNT):
            stop = services[account].autonomy.list_stops(user_id=USER)[0]
            assert stop["status"] == "ARMED" and stop["lastError"] is None, account

        # 重复同步幂等：同号回报不再多记一行，保护不动。
        assert services[late_account].autonomy.sync_strategy_protection() == {}
        assert len(app.state.store.connection.execute(
            "SELECT trading_account_id FROM strategy_fill_events WHERE event_id = ?",
            (event_id,)).fetchall()) == 2
        assert services[late_account].autonomy.list_stops(user_id=USER)[0]["lastError"] is None
    finally:
        app.state.store.close()


async def test_a_database_failure_rolls_back_and_lands_in_last_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#453：成交入账撞上数据库故障（约束/损坏类）——这一笔的事务回滚（平仓不消耗保护、
    事件不落半行），错误以 strategy_protection_sync:strategy_fill_database_error 落在保护自己的
    lastError 里，保护保持 ARMED；故障消失后同一份回报照常入账、lastError 清空。"""
    app, provider, _, _, request = await armed_card(
        tmp_path, volume=2, name="fill-db-error.sqlite3")
    try:
        provider.trades.insert(0, {
            "tradeId": "close-1", "clientOrderId": "manual-close", "symbol": request["symbol"],
            "direction": "SELL", "offset": "CLOSETODAY", "volume": 1, "price": 823.0,
            "tradeDateTime": 2, "tradingDay": provider.day})
        with app.state.store.connection:
            app.state.store.connection.execute(
                """CREATE TRIGGER abort_fill_insert BEFORE INSERT ON strategy_fill_events
                BEGIN SELECT RAISE(ABORT, 'disk image malformed'); END""")

        failures = app.state.autonomy.sync_strategy_protection()
        assert failures == {(USER, "SHFE.au2612"): "strategy_fill_database_error"}
        stop = app.state.autonomy.list_stops(user_id=USER)[0]
        assert stop["status"] == "ARMED" and stop["remainingVolume"] == 2
        assert stop["lastError"] == "strategy_protection_sync:strategy_fill_database_error"
        # 回滚生效：这次平仓既没入账，也没消耗保护。
        assert app.state.store.connection.execute(
            "SELECT COUNT(*) FROM strategy_fill_events WHERE client_order_id = 'manual-close'"
        ).fetchone()[0] == 0

        with app.state.store.connection:
            app.state.store.connection.execute("DROP TRIGGER abort_fill_insert")
        assert app.state.autonomy.sync_strategy_protection() == {}
        stop = app.state.autonomy.list_stops(user_id=USER)[0]
        assert stop["status"] == "ARMED" and stop["remainingVolume"] == 1
        assert stop["lastError"] is None
        assert app.state.store.connection.execute(
            "SELECT COUNT(*) FROM strategy_fill_events WHERE client_order_id = 'manual-close'"
        ).fetchone()[0] == 1
    finally:
        app.state.store.close()


# -- 直接保护跟住持仓 (2026-10-04 #528): 平掉就撤、减仓就缩、隔夜滚桶；未派发不通知 ------------------


def _long_items(volume: int, *, today: int) -> list[dict]:
    return [{"symbol": "SHFE.au2612", "direction": "long", "volume": volume,
             "todayVolume": today, "historicalVolume": volume - today,
             "frozenToday": 0, "frozenHistorical": 0}]


async def _direct_stop(app, session_id: str, *, volume: int, bucket: str = "today",
                       trigger: float = 822.5) -> dict:
    provider = app.state.data_provider
    provider.last = 823.0
    provider.quote_datetime = quote_now()
    return await app.state.autonomy.create_stop(
        user_id=USER, session_id=session_id, symbol="SHFE.au2612",
        position_direction="long", position_bucket=bucket, volume=volume,
        trigger_price=trigger)


def _stop_notices(app, session_id: str, keyword: str) -> list[str]:
    return [str(event.get("payload", {}).get("text"))
            for _, event in app.state.store.list_events(session_id)
            if event.get("name") == "message.completed"
            and str(event.get("payload", {}).get("turnId", "")).startswith("protection-cancel-")
            and keyword in str(event.get("payload", {}).get("text", ""))]


def _session_texts(app, session_id: str) -> list[str]:
    return [str(event.get("payload", {}).get("text"))
            for _, event in app.state.store.list_events(session_id)
            if event.get("name") == "message.completed"]


async def test_a_direct_stop_is_cancelled_once_the_position_is_fully_closed(tmp_path: Path) -> None:
    app = application(tmp_path / "clamp-closed.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    await _direct_stop(app, session.session_id, volume=2)
    provider.position_items = []  # 用户自己把仓全平了
    await app.state.autonomy.run_once()

    fired = app.state.autonomy.list_stops(user_id=USER)[0]
    assert fired["status"] == "CANCELLED" and fired["lastError"] == "position_closed"
    assert gateway.submit_count == 0  # 撤保护不下任何新单
    assert len(_stop_notices(app, session.session_id, "持仓已平，保护自动撤销")) == 1

    provider.position_items = _long_items(1, today=1)  # 重新开仓：已撤销的保护不追杀新仓
    provider.last = 820.0
    provider.quote_datetime = quote_now()
    await app.state.autonomy.run_once()
    assert gateway.submit_count == 0
    assert app.state.autonomy.list_stops(user_id=USER)[0]["status"] == "CANCELLED"
    app.state.store.close()


async def test_a_direct_stop_shrinks_when_the_position_is_partially_closed(tmp_path: Path) -> None:
    app = application(tmp_path / "clamp-shrink.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    await _direct_stop(app, session.session_id, volume=2)
    provider.position_items = _long_items(1, today=1)  # 用户自己平了 1 手
    provider.last = 823.0
    provider.quote_datetime = quote_now()
    await app.state.autonomy.run_once()

    clamped = app.state.autonomy.list_stops(user_id=USER)[0]
    assert clamped["status"] == "ARMED"
    assert (clamped["volume"], clamped["remainingVolume"]) == (1, 1)
    assert not _stop_notices(app, session.session_id, "持仓已平")  # 减仓只跟随，不打扰

    provider.last = 820.5  # 触发：只平当前还持有的 1 手
    provider.quote_datetime = quote_now()
    await app.state.autonomy.run_once()
    assert gateway.submit_count == 1 and gateway.request["volume"] == 1
    assert gateway.request["offset"] == "CLOSETODAY"
    app.state.store.close()


async def test_a_direct_stop_survives_a_limit_close_order_that_freezes_the_position(tmp_path: Path) -> None:
    """用户自己挂了整仓的限价平仓单（仓位全部冻结、仍在）：保护既不撤也不缩 —— 限价单没成交时止损还得在。"""
    app = application(tmp_path / "clamp-frozen.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    await _direct_stop(app, session.session_id, volume=2)
    provider.position_items = [{**_long_items(2, today=2)[0], "frozenToday": 2}]
    provider.last = 823.0
    provider.quote_datetime = quote_now()
    await app.state.autonomy.run_once()
    kept = app.state.autonomy.list_stops(user_id=USER)[0]
    assert kept["status"] == "ARMED" and (kept["volume"], kept["remainingVolume"]) == (2, 2)
    assert not _stop_notices(app, session.session_id, "持仓已平")
    assert gateway.submit_count == 0
    app.state.store.close()


async def test_an_overnight_today_bucket_rolls_to_historical_before_it_fires(tmp_path: Path) -> None:
    app = application(tmp_path / "clamp-roll.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(1, today=1)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=1)
    provider.position_items = _long_items(1, today=0)  # 隔夜：昨天的今仓成了昨仓
    with app.state.store.lock, app.state.store.connection:
        app.state.store.connection.execute(  # 造出“创建于上一个交易日”的旧行（没有记账户交易日）
            "UPDATE ai_stop_orders SET created_at = '2026-08-24T01:00:00+00:00', account_trading_day = NULL WHERE id = ?",
            (stop["stopId"],))
    provider.last = 823.0
    provider.quote_datetime = quote_now()
    await app.state.autonomy.run_once()
    clamped = app.state.autonomy.list_stops(user_id=USER)[0]
    assert clamped["status"] == "ARMED" and clamped["positionBucket"] == "historical"

    provider.last = 820.5  # 触发：平昨仓，offset 是 CLOSE
    provider.quote_datetime = quote_now()
    await app.state.autonomy.run_once()
    assert gateway.submit_count == 1 and gateway.request["offset"] == "CLOSE"
    app.state.store.close()


async def test_a_direct_stop_waits_for_the_account_to_roll_before_it_moves_to_historical(tmp_path: Path) -> None:
    """#790 review：日历已过 18 点换日、本站模拟盘还在等结算价（账户交易日与今昨分仓都没变）——
    今仓保护不能先按日历滚成昨仓（昨仓是 0 → 误判持仓已平、把还持着的仓的保护撤掉）；平仓仍按平今。
    账户结算换日、今仓变成昨仓后才滚桶，触发平昨。"""
    app = application(tmp_path / "clamp-account-day.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.day = "2026-08-24"
    provider.position_items = _long_items(1, today=1)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=1)
    try:
        with app.state.store.lock, app.state.store.connection:
            app.state.store.connection.execute(  # 日历上已经是更晚的交易日
                "UPDATE ai_stop_orders SET created_at = '2026-08-24T01:00:00+00:00' WHERE id = ?",
                (stop["stopId"],))
        _fresh(provider)
        await app.state.autonomy.run_once()
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "ARMED" and row["position_bucket"] == "today"
        assert not _stop_notices(app, session.session_id, "持仓已平")

        _fresh(provider, 820.5)
        await app.state.autonomy.run_once()
        assert gateway.submit_count == 1 and gateway.request["offset"] == "CLOSETODAY"
    finally:
        app.state.store.close()


async def test_a_stop_armed_before_settlement_rolls_once_the_account_settles(tmp_path: Path) -> None:
    """18 点后、结算前挂的今仓保护（日历已是下一交易日，账户还在当天）：结算换日后今仓成了昨仓，
    保护跟着滚成昨仓，不因“日历上同一天”而留在今仓、找不到仓被撤。"""
    app = application(tmp_path / "clamp-armed-evening.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.day = "2026-08-24"
    provider.position_items = _long_items(1, today=1)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=1)
    try:
        provider.day = "2026-08-25"  # 结算完成：账户换日，今仓成昨仓
        provider.position_items = _long_items(1, today=0)
        _fresh(provider)
        await app.state.autonomy.run_once()
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "ARMED" and row["position_bucket"] == "historical"

        _fresh(provider, 820.5)
        await app.state.autonomy.run_once()
        assert gateway.submit_count == 1 and gateway.request["offset"] == "CLOSE"
    finally:
        app.state.store.close()


class StaleActivityProvider(Provider):
    """持仓读数已经全平，但账户活动不新鲜：直接保护一概不动 —— 不确定时绝不撤保护。"""

    stale = True

    def account_activity(self) -> dict:
        current = super().account_activity()
        return {**current, "dataStatus": "stale", "isCached": True, "ageSeconds": 90} if self.stale else current


async def test_a_stale_activity_snapshot_leaves_direct_stops_alone(tmp_path: Path) -> None:
    app = application(tmp_path / "clamp-stale.sqlite3", Gateway(), StaleActivityProvider())
    provider = app.state.data_provider
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    # #754: a stop is armed only against a current account read — refused while the report is stale.
    with pytest.raises(AutonomyError, match="activity_not_current"):
        await _direct_stop(app, session.session_id, volume=2)
    provider.stale = False
    stop = await _direct_stop(app, session.session_id, volume=2)
    provider.stale = True
    provider.position_items = []
    await app.state.autonomy.run_once()
    row = stop_row(app, stop["stopId"])[0]
    assert row["status"] == "ARMED" and int(row["remaining_volume"]) == 2
    assert not _stop_notices(app, session.session_id, "持仓已平")
    app.state.store.close()


async def test_fill_backed_protections_are_never_clamped(tmp_path: Path) -> None:
    """对照：顺带/策略保护（带 strategy_metadata）不由持仓跟随撤销 —— 仍由成交同步负责。"""
    app, provider, gateway, session, _ = await armed_card(tmp_path, volume=1,
                                                          name="clamp-metadata.sqlite3")
    provider.position_items = []  # 持仓没了：带来源的保护不动
    provider.last = 823.0
    provider.quote_datetime = quote_now()
    await app.state.autonomy.run_once()
    row = stop_row(app)[0]
    assert row["status"] == "ARMED" and int(row["remaining_volume"]) == 1
    assert gateway.submit_count == 1  # 只有最初的入场单
    app.state.store.close()


async def test_a_partial_direct_stop_rearms_once_its_close_order_has_ended(tmp_path: Path) -> None:
    app = application(tmp_path / "clamp-partial.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=2)
    provider.last = 820.5
    provider.quote_datetime = quote_now()
    await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])  # 平仓单已发出
    assert gateway.submit_count == 1 and gateway.request["volume"] == 2
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=1)  # 成交 1 手后结束
    provider.position_items = _long_items(1, today=1)
    provider.last = 823.0
    provider.quote_datetime = quote_now()
    with app.state.store.lock, app.state.store.connection:  # 让回读立即到期：去网关读，不是读存的状态
        app.state.store.connection.execute(
            "UPDATE ai_stop_orders SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (stop["stopId"],))
    await app.state.autonomy.run_once()  # 回读：PARTIAL
    await app.state.autonomy.run_once()  # 收敛：回 ARMED、剩余 1、旧平仓单身份清掉
    row = stop_row(app, stop["stopId"])[0]
    assert row["status"] == "ARMED" and int(row["volume"]) == 1 and int(row["remaining_volume"]) == 1
    assert row["close_client_order_id"] is None

    provider.last = 820.5  # 再触发：平的还是当前那 1 手
    provider.quote_datetime = quote_now()
    await app.state.autonomy.run_once()
    assert gateway.submit_count == 2 and gateway.request["volume"] == 1
    app.state.store.close()


async def test_a_direct_stops_full_close_clears_its_remaining_volume(tmp_path: Path) -> None:
    """保护单触发并全部成交后 remainingVolume 归零（2026-10-09 对账：平完仍显示剩 1 手）。
    直接保护（无策略元数据）的剩余量由状态机清；带策略元数据的行仍由成交同步归零并记盈亏。"""
    app = application(tmp_path / "executed-remaining.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(1, today=1)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=1)
    _fresh(provider, 820.5)
    await app.state.autonomy.run_once()  # 触发：平仓单已发出
    assert gateway.submit_count == 1
    close = dict(gateway.request)
    gateway.query_report = gateway.report(close, status="FINISHED", volume_left=0)
    provider.order_reports.append({**close, "status": "FINISHED", "volumeLeft": 0})
    provider.trades.insert(0, {"tradeId": "close-fill", "clientOrderId": close["clientOrderId"],
                               "symbol": close["symbol"], "direction": close["direction"],
                               "offset": close["offset"], "volume": 1, "price": 820.5,
                               "tradeDateTime": 9, "tradingDay": provider.day})
    provider.position_items = []
    with app.state.store.lock, app.state.store.connection:
        app.state.store.connection.execute(
            "UPDATE ai_stop_orders SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (stop["stopId"],))
    await app.state.autonomy.run_once()   # 回读：全部成交 → EXECUTED
    await app.state.autonomy.run_once()   # 幂等：EXECUTED 不再进轮询
    fired = app.state.autonomy.list_stops(user_id=USER)[0]
    assert fired["status"] == "EXECUTED" and fired["remainingVolume"] == 0
    assert fired["volume"] == 1  # 触发时的手数保留，只清剩余
    app.state.store.close()


# -- 直接保护锚定原仓 (2026-10-06 #528 P1-1): 原仓平完就撤、开仓不加量、平仓先算原仓 ------------------


def _fill(provider: Provider, trade_id: str, *, offset: str, direction: str, volume: int,
          day: str | None = None, order: str | None = None, time: int = 5) -> None:
    provider.trades.insert(0, {
        "tradeId": trade_id, "clientOrderId": order or f"manual-{trade_id}", "symbol": "SHFE.au2612",
        "direction": direction, "offset": offset, "volume": volume, "price": 823.0,
        "tradeDateTime": time, "tradingDay": day or provider.day})


def _fresh(provider: Provider, last: float = 823.0) -> None:
    provider.last = last
    provider.quote_datetime = quote_now()


async def test_two_stops_on_one_closed_position_get_one_notice_with_the_contract_name(tmp_path: Path) -> None:
    """全平后同一持仓的几条保护只发一句，用品种名而不是代码名 (#754)。"""
    app = application(tmp_path / "clamp-closed-merged.sqlite3", Gateway(), Provider())
    provider = app.state.data_provider
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    await _direct_stop(app, session.session_id, volume=1, trigger=822.5)
    await _direct_stop(app, session.session_id, volume=1, trigger=822.0)
    stops = [stop for stop in app.state.autonomy.list_stops(user_id=USER) if stop["status"] == "ARMED"]
    assert len(stops) == 2
    provider.position_items = []
    await app.state.autonomy.run_once()

    assert all(stop["status"] == "CANCELLED" for stop in app.state.autonomy.list_stops(user_id=USER))
    assert _stop_notices(app, session.session_id, "持仓已平，保护自动撤销") == [
        "沪金2612 多单持仓已平，保护自动撤销"]
    # One 保护单撤销 reminder for the two stops, and the next round repeats nothing (#932).
    rows = app.state.store.reminders_page(USER, trading_day="9999-12-31", scope="earlier")[0]
    assert [item["kind"] for item in rows] == ["protection_cancelled"] and "止损" in rows[0]["body"]
    await app.state.autonomy.run_once()
    assert len(app.state.store.reminders_page(USER, trading_day="9999-12-31", scope="earlier")[0]) == 1
    app.state.store.close()


async def test_a_direct_stop_does_not_carry_over_to_a_reopened_position_it_never_saw_flat(tmp_path: Path) -> None:
    app = application(tmp_path / "clamp-reopen.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=2)
    try:
        provider.trades[:0] = [  # 两次快照之间：外部平今 2 手，再新开 2 手
            {"tradeId": "open-2", "clientOrderId": "manual-open-2", "symbol": "SHFE.au2612", "direction": "BUY",
             "offset": "OPEN", "volume": 2, "price": 822.0, "tradeDateTime": 3, "tradingDay": provider.day},
            {"tradeId": "close-1", "clientOrderId": "manual-close-1", "symbol": "SHFE.au2612", "direction": "SELL",
             "offset": "CLOSETODAY", "volume": 2, "price": 823.0, "tradeDateTime": 2, "tradingDay": provider.day}]
        provider.position_items = _long_items(2, today=2)
        provider.last = 820.0
        provider.quote_datetime = quote_now()
        await app.state.autonomy.run_once()
        assert gateway.submit_count == 0  # 旧保护不平它没见过的新仓
        cancelled = app.state.autonomy.list_stops(user_id=USER)[0]
        assert cancelled["status"] == "CANCELLED" and cancelled["lastError"] == "anchor_position_closed"
        # 告诉用户新仓没保护，并给“重新挂”：只问 AI / 打开持仓，不下单、不自动挂。
        texts = _stop_notices(app, session.session_id, "没有保护")
        assert texts == ["沪金2612 原来那 2 手多单已平，止损已撤；现在这 2 手多单没有保护，要按原价位重新挂吗？"]
        card = reminder_cards(app, session.session_id, f"protection-cancel-{stop['stopId']}")[-1]
        assert_no_order_action(card)
        ask = next(action for action in card["actions"] if action["kind"] == "ask")
        assert ask["label"] == "重新挂" and "现在这 2 手" in ask["prompt"] and "止损 822.5" in ask["prompt"]
        await app.state.autonomy.run_once()  # 撤了就不再提醒、不再作用
        assert gateway.submit_count == 0 and len(_stop_notices(app, session.session_id, "没有保护")) == 1
    finally:
        app.state.store.close()


async def test_each_stop_of_a_reopened_position_keeps_its_own_rehang_card(tmp_path: Path) -> None:
    """The 重新挂 proposal carries its own stop's price and lots: it is not merged across stops (only the
    plain cancel notice is)."""
    app = application(tmp_path / "clamp-reopen-two.sqlite3", Gateway(), Provider())
    provider = app.state.data_provider
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    first = await _direct_stop(app, session.session_id, volume=1, trigger=822.5)
    second = await _direct_stop(app, session.session_id, volume=1, trigger=822.0)
    try:
        provider.trades[:0] = [
            {"tradeId": "open-2", "clientOrderId": "manual-open-2", "symbol": "SHFE.au2612", "direction": "BUY",
             "offset": "OPEN", "volume": 2, "price": 822.0, "tradeDateTime": 3, "tradingDay": provider.day},
            {"tradeId": "close-1", "clientOrderId": "manual-close-1", "symbol": "SHFE.au2612", "direction": "SELL",
             "offset": "CLOSETODAY", "volume": 2, "price": 823.0, "tradeDateTime": 2, "tradingDay": provider.day}]
        provider.position_items = _long_items(2, today=2)
        provider.last = 820.0
        provider.quote_datetime = quote_now()
        await app.state.autonomy.run_once()
        assert sorted(_stop_notices(app, session.session_id, "没有保护")) == [
            "沪金2612 原来那 1 手多单已平，止损已撤；现在这 2 手多单没有保护，要按原价位重新挂吗？"] * 2
        prompts = []
        for stop in (first, second):
            card = reminder_cards(app, session.session_id, f"protection-cancel-{stop['stopId']}")[-1]
            assert_no_order_action(card)
            prompts.append(next(a for a in card["actions"] if a["kind"] == "ask")["prompt"])
        assert "止损 822.5" in prompts[0] and "822.0" not in prompts[0]
        assert "止损 822" in prompts[1] and "822.5" not in prompts[1]
    finally:
        app.state.store.close()


async def test_adding_to_a_position_never_grows_a_direct_stop_and_a_close_counts_against_the_original(
    tmp_path: Path,
) -> None:
    """所有者决定①：原 2 + 加开 2 + 平今 2 → 视为原仓平完、撤旧保护；加开本身不加保护量。"""
    app = application(tmp_path / "clamp-add.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    _fill(provider, "open-1", offset="OPEN", direction="BUY", volume=2, time=1)  # 原仓自己的开仓成交
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=2)
    try:
        _fill(provider, "open-2", offset="OPEN", direction="BUY", volume=2, time=2)
        provider.position_items = _long_items(4, today=4)
        _fresh(provider)
        await app.state.autonomy.run_once()
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "ARMED" and (int(row["volume"]), int(row["remaining_volume"])) == (2, 2)
        assert int(row["anchor_volume"]) == 2 and int(row["closed_since"]) == 0

        _fill(provider, "close-1", offset="CLOSETODAY", direction="SELL", volume=2, time=3)
        provider.position_items = _long_items(2, today=2)
        _fresh(provider, 820.0)
        await app.state.autonomy.run_once()
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "CANCELLED" and row["last_error"] == "anchor_position_closed"
        assert gateway.submit_count == 0
    finally:
        app.state.store.close()


async def test_an_external_one_lot_close_shrinks_the_stop_once_and_it_fires_one_lot(tmp_path: Path) -> None:
    """外部先减 1 手：保护缩到 1、按 1 手平成功；同一批成交重复读不重复计；之后加开 1 手也不长回 2。"""
    app = application(tmp_path / "clamp-external.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=2)
    try:
        _fill(provider, "close-1", offset="CLOSETODAY", direction="SELL", volume=1, time=2)
        _fill(provider, "other-side", offset="CLOSETODAY", direction="BUY", volume=1, time=3)  # 平空单不算
        provider.position_items = _long_items(1, today=1)
        _fresh(provider)
        await app.state.autonomy.run_once()
        await app.state.autonomy.run_once()  # 同一批成交再读一遍：幂等
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "ARMED" and int(row["remaining_volume"]) == 1 and int(row["closed_since"]) == 1

        _fill(provider, "open-2", offset="OPEN", direction="BUY", volume=1, time=4)  # 加开 1 手
        provider.position_items = _long_items(2, today=2)
        _fresh(provider)
        await app.state.autonomy.run_once()
        assert int(stop_row(app, stop["stopId"])[0]["remaining_volume"]) == 1

        _fresh(provider, 820.5)  # 触发：只平原仓剩下的 1 手
        await app.state.autonomy.run_once()
        assert gateway.submit_count == 1 and gateway.request["volume"] == 1
        assert gateway.request["offset"] == "CLOSETODAY"
    finally:
        app.state.store.close()


async def test_an_overnight_direct_stop_keeps_its_anchor_across_the_trading_day(tmp_path: Path) -> None:
    """今仓跨交易日变昨仓：滚桶后新交易日的 activity 只剩当日成交，旧成交不再出现也不误判；
    新交易日的平昨计入原仓、平今（新开的今仓）不计；触发用 CLOSE。"""
    app = application(tmp_path / "clamp-overnight.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.day = "2026-08-24"
    provider.position_items = _long_items(2, today=2)
    _fill(provider, "open-1", offset="OPEN", direction="BUY", volume=2, time=1)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=2)
    try:
        with app.state.store.lock, app.state.store.connection:
            app.state.store.connection.execute(
                "UPDATE ai_stop_orders SET created_at = '2026-08-24T01:00:00+00:00' WHERE id = ?",
                (stop["stopId"],))
        provider.day = "2026-08-25"
        provider.trades = []  # 新交易日：只剩当日成交
        provider.position_items = _long_items(2, today=0)
        _fresh(provider)
        await app.state.autonomy.run_once()
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "ARMED" and row["position_bucket"] == "historical"
        assert int(row["remaining_volume"]) == 2 and int(row["closed_since"]) == 0

        _fill(provider, "open-today", offset="OPEN", direction="BUY", volume=1, time=10)
        _fill(provider, "close-today", offset="CLOSETODAY", direction="SELL", volume=1, time=11)  # 平的是新今仓
        _fill(provider, "close-yd", offset="CLOSE", direction="SELL", volume=1, time=12)  # 平昨：原仓
        provider.position_items = _long_items(1, today=0)
        _fresh(provider)
        await app.state.autonomy.run_once()
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "ARMED" and int(row["closed_since"]) == 1 and int(row["remaining_volume"]) == 1
        assert json.loads(row["fill_watermark_json"])["day"] == "2026-08-25"

        _fresh(provider, 820.5)
        await app.state.autonomy.run_once()
        assert gateway.submit_count == 1 and gateway.request["offset"] == "CLOSE" and gateway.request["volume"] == 1
    finally:
        app.state.store.close()


async def test_a_direct_stops_own_partial_close_counts_once_and_still_anchors(tmp_path: Path) -> None:
    """保护自己的平仓只成交 1 手：这笔成交只计一次，PARTIAL 续接照旧回 ARMED 1 手；之后外部平掉原仓
    最后 1 手、又新开 1 手（两次快照之间）→ 撤，不作用到新仓。"""
    app = application(tmp_path / "clamp-own-partial.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=2)
    try:
        _fresh(provider, 820.5)
        await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
        assert gateway.submit_count == 1 and gateway.request["volume"] == 2
        own = gateway.request["clientOrderId"]
        gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=1)
        _fill(provider, "own-1", offset="CLOSETODAY", direction="SELL", volume=1, order=own, time=2)
        provider.position_items = _long_items(1, today=1)
        _fresh(provider)
        with app.state.store.lock, app.state.store.connection:
            app.state.store.connection.execute(
                "UPDATE ai_stop_orders SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (stop["stopId"],))
        await app.state.autonomy.run_once()  # 回读：PARTIAL
        await app.state.autonomy.run_once()  # 续接：ARMED 1 手
        await app.state.autonomy.run_once()  # 再读同一批成交
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "ARMED" and int(row["remaining_volume"]) == 1 and row["close_client_order_id"] is None
        assert int(row["closed_since"]) == 1

        _fill(provider, "close-2", offset="CLOSETODAY", direction="SELL", volume=1, time=3)
        _fill(provider, "open-2", offset="OPEN", direction="BUY", volume=1, time=4)
        _fresh(provider, 820.0)
        await app.state.autonomy.run_once()
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "CANCELLED" and row["last_error"] == "anchor_position_closed"
        assert gateway.submit_count == 1
    finally:
        app.state.store.close()


async def test_untrusted_fills_leave_a_direct_stop_alone(tmp_path: Path) -> None:
    """本合约有一笔读不懂的成交（没有交易日）：这一行这一轮什么都不改，即使持仓读数已经空了。"""
    app = application(tmp_path / "clamp-untrusted.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=2)
    try:
        _fill(provider, "close-1", offset="CLOSETODAY", direction="SELL", volume=2)
        provider.trades[0]["tradingDay"] = None
        provider.position_items = []
        _fresh(provider)
        await app.state.autonomy.run_once()
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "ARMED" and int(row["remaining_volume"]) == 2 and int(row["closed_since"]) == 0
        assert gateway.submit_count == 0 and not _stop_notices(app, session.session_id, "保护")
    finally:
        app.state.store.close()


async def test_a_direct_stop_armed_before_the_anchor_starts_from_the_first_clamp(tmp_path: Path) -> None:
    """升级前已挂的直接保护（锚点列为空）：第一轮以当时持仓与成交为起点、不撤不缩；之后照新规则跟。"""
    app = application(tmp_path / "clamp-legacy.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=2)
    try:
        with app.state.store.lock, app.state.store.connection:
            app.state.store.connection.execute(
                """UPDATE ai_stop_orders SET anchor_volume = NULL, closed_since = NULL,
                fill_watermark_json = NULL WHERE id = ?""", (stop["stopId"],))
        _fill(provider, "old-close", offset="CLOSETODAY", direction="SELL", volume=2, time=1)  # 升级前的成交
        _fresh(provider)
        await app.state.autonomy.run_once()
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "ARMED" and int(row["remaining_volume"]) == 2
        assert (int(row["anchor_volume"]), int(row["closed_since"])) == (2, 0)

        _fill(provider, "close-1", offset="CLOSETODAY", direction="SELL", volume=1, time=2)
        provider.position_items = _long_items(1, today=1)
        _fresh(provider)
        await app.state.autonomy.run_once()
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "ARMED" and int(row["remaining_volume"]) == 1 and int(row["closed_since"]) == 1
        assert gateway.submit_count == 0
    finally:
        app.state.store.close()


async def test_an_undispatched_trigger_notice_waits_until_the_close_really_goes_out(
    tmp_path: Path,
) -> None:
    """提交被挡在网关之前（NOT_DISPATCHED）：行回 ARMED、原因留在行上、不喊“已发出平仓单”；
    下一轮正常派发时仍是同一笔单，通知只有一条。"""
    app = application(tmp_path / "undispatched-trigger.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(1, today=1)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=1)
    gateway.submit_report = {"commandStatus": "NOT_DISPATCHED"}  # 提交在网关之前就被挡下
    provider.last = 820.5
    provider.quote_datetime = quote_now()
    await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])

    row = stop_row(app, stop["stopId"])[0]
    assert row["status"] == "ARMED" and row["last_error"] == "order_not_dispatched"
    assert _session_texts(app, session.session_id) == []  # 没发出去的单不喊“已发出平仓单”
    assert gateway.submit_count == 1  # 试过一次，没派发

    gateway.submit_report = None  # 下一轮正常派发：仍是同一笔单
    provider.quote_datetime = quote_now()
    await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
    row = stop_row(app, stop["stopId"])[0]
    assert row["status"] == "SUBMITTED" and row["last_error"] is None
    assert gateway.submit_count == 2  # 同一个 client_order_id 的第二次尝试
    notices = _session_texts(app, session.session_id)
    assert len(notices) == 1 and "已按对手价发出平仓单" in notices[0]
    app.state.store.close()


async def test_an_orders_proposal_row_lands_on_the_account_that_executed_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, gateway_a, gateway_b = roles_app(tmp_path, monkeypatch, "proposal-account.sqlite3")
    try:
        session_b = beta_session(app)
        await app.state.accounts.get(BETA_ACCOUNT).autonomy.execute_trade(
            user_id=USER, session_id=session_b, symbol="SHFE.au2612", side="buy_open",
            volume=1, price_intent="passive", reason="乙账户手工执行", ai_initiated=False)
        assert gateway_b.submit_count == 1
        row = app.state.store.connection.execute(
            "SELECT trading_account_id FROM proposals ORDER BY created_at DESC").fetchone()
        assert row["trading_account_id"] == BETA_ACCOUNT  # 不再落进默认账户的台账
    finally:
        app.state.store.close()


async def test_the_activity_feed_reads_only_one_accounts_ai_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trader_runtime.agent_activity import activity_snapshot

    app, gateway_a, gateway_b = roles_app(tmp_path, monkeypatch, "activity-accounts.sqlite3",
                                          beta_role="owner", beta_owner=None)
    try:
        provider_a = app.state.accounts.get(DEFAULT_ACCOUNT).provider
        provider_a.quote_datetime = quote_now()
        session_a = app.state.broker.create_session("local", "native", USER).session_id
        await app.state.autonomy.execute_trade(
            user_id=USER, session_id=session_a, symbol="SHFE.au2612", side="buy_open",
            volume=1, price_intent="passive", reason="甲的AI单", ai_initiated=True)
        services_b = app.state.accounts.get(BETA_ACCOUNT)
        services_b.provider.quote_datetime = quote_now()
        await services_b.autonomy.execute_trade(
            user_id=USER, session_id=beta_session(app), symbol="SHFE.au2612", side="buy_open",
            volume=1, price_intent="passive", reason="乙的AI单", ai_initiated=True)
        own = activity_snapshot(app, USER, "", {"trades": [], "orders": []})
        assert [order["reason"] for order in own["orders"]] == ["甲的AI单"]  # 乙的单不再串进来
        other = activity_snapshot(app, USER, "", {"trades": [], "orders": []},
                                  simulation_orders=services_b.simulation_orders,
                                  trading_account_id=BETA_ACCOUNT)
        assert [order["reason"] for order in other["orders"]] == ["乙的AI单"]
    finally:
        app.state.store.close()
def reminder_cards(app, session_id: str, turn_id: str) -> list[dict]:
    """提醒卡 (2026-10-04) published on a reminder's own turn."""
    return [event["payload"] for _, event in app.state.store.list_events(session_id)
            if event.get("name") == "card.reminder" and event.get("payload", {}).get("turnId") == turn_id]


def assert_no_order_action(card: dict) -> None:
    """A reminder card only asks the AI or opens a panel — never an order."""
    assert card["actions"] and {action["kind"] for action in card["actions"]} <= {"ask", "ui"}
    for action in card["actions"]:
        assert set(action.get("changes") or {}) <= {"panel", "news", "positionsTab", "symbol", "view"}


# ---------------------------------------------------------------------------------------------
# #750：保护平仓遇到“等结算价”拒收后，重派按原价格意图用当时行情重新定价。


def settlement_pending(gateway: Gateway) -> dict:
    """The paper engine refuses every submit with 409 paper_settlement_pending (no command row)
    until `state["pending"]` is cleared; `state["sent"]` keeps every request that reached it."""
    from trader_runtime.sidecar_http import SidecarHttpError

    state: dict = {"pending": True, "sent": []}
    original = gateway.submit_order_once

    async def submit(request: dict) -> dict:
        state["sent"].append(dict(request))
        if not state["pending"]:
            return await original(request)
        gateway.submit_count += 1
        gateway.request = dict(request)
        raise SidecarHttpError("sidecar_http_error", status=409, payload={"error": "paper_settlement_pending"})

    gateway.submit_order_once = submit  # type: ignore[method-assign]
    return state


def retry_due(app) -> None:
    """The last refusal is older than the stop scan's retry cadence."""
    with app.state.store.lock, app.state.store.connection:
        app.state.store.connection.execute(
            "UPDATE tqkq_sim_order_links SET updated_at = '2000-01-01T00:00:00+00:00'")


def quote(provider: Provider, *, last: float, bid: float | None = None, ask: float | None = None) -> None:
    provider.last = last
    provider.bid = bid if bid is not None else provider.bid
    provider.ask = ask if ask is not None else provider.ask
    provider.quote_datetime = quote_now()


def audits(app, action: str) -> list[dict]:
    return [json.loads(row[0]) for row in app.state.store.connection.execute(
        "SELECT result_json FROM proposal_audit WHERE action = ? ORDER BY created_at", (action,))]


def protection_notices(app, session_id: str) -> list[str]:
    return [text for text in _session_texts(app, session_id) if "触发" in text]


@pytest.mark.parametrize("short", [False, True])
async def test_a_settlement_pending_stop_close_is_repriced_on_the_quote_it_finally_goes_out_on(
    tmp_path: Path, short: bool,
) -> None:
    """多/空直接止损：引擎等结算价拒收后行回 ARMED（原因 paper_settlement_pending），重试按
    节奏而不是每条行情；每次重派按当时对手价改写同一笔单（同一 clientOrderId），连续拒收
    不通知，恢复后只一条通知、写实际委托价。"""
    app = application(tmp_path / f"reprice-{short}.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    try:
        direction = "short" if short else "long"
        provider.position_items = [{"symbol": "SHFE.au2612", "direction": direction, "volume": 1,
                                    "todayVolume": 1, "historicalVolume": 0,
                                    "frozenToday": 0, "frozenHistorical": 0}]
        session = app.state.broker.create_session("local", "native", USER)
        quote(provider, last=823.0)
        stop = await app.state.autonomy.create_stop(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612",
            position_direction=direction, position_bucket="today", volume=1,
            trigger_price=824.5 if short else 822.5)
        state = settlement_pending(gateway)
        field = "ask" if short else "bid"  # buy_close 对手价 = 卖一；sell_close = 买一
        quote(provider, last=825.0 if short else 820.5, **{field: 825.0 if short else 820.0})
        await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "ARMED" and row["last_error"] == "paper_settlement_pending"
        assert [item["limitPrice"] for item in state["sent"]] == [825.0 if short else 820.0]

        # 拒收后的下一条行情不再立刻重发同一笔注定被拒的提交。
        quote(provider, last=826.0 if short else 819.5, **{field: 826.0 if short else 819.0})
        await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
        assert len(state["sent"]) == 1

        retry_due(app)
        quote(provider, last=830.0 if short else 815.5, **{field: 830.0 if short else 815.0})
        await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
        assert state["sent"][-1]["limitPrice"] == (830.0 if short else 815.0)
        assert protection_notices(app, session.session_id) == []  # 连续拒收不喊“已发出”

        state["pending"] = False  # 结算价公布
        retry_due(app)
        target = 833.5 if short else 811.5
        quote(provider, last=834.0 if short else 812.0, **{field: target})
        await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "SUBMITTED" and row["last_error"] is None
        assert state["sent"][-1]["limitPrice"] == target
        assert len({item["clientOrderId"] for item in state["sent"]}) == 1  # 原 ID 改价，不换单
        order = app.state.simulation_orders.get(row["close_client_order_id"], user_id=USER)
        assert order["request"]["limitPrice"] == target and order["remoteStatus"] == "ALIVE"
        assert [item["limitPrice"] for item in audits(app, "protective-reprice")] == [
            830.0 if short else 815.0, target]
        notices = protection_notices(app, session.session_id)
        assert len(notices) == 1 and "已按对手价发出平仓单" in notices[0]
        assert f"委托价 {target:g}" in notices[0]
        await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
        assert len(state["sent"]) == 3 and len(protection_notices(app, session.session_id)) == 1
    finally:
        app.state.store.close()


async def test_a_take_profit_that_eases_back_while_pending_goes_out_at_the_current_bid(tmp_path: Path) -> None:
    """止盈触发后价格回落但仍满足止盈：不发触发时更高的旧卖价，按当时买一。"""
    app = application(tmp_path / "reprice-take.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    try:
        provider.position_items = _long_items(1, today=1)
        session = app.state.broker.create_session("local", "native", USER)
        quote(provider, last=823.0)
        take = await app.state.autonomy.create_stop(
            user_id=USER, session_id=session.session_id, symbol="SHFE.au2612",
            position_direction="long", position_bucket="today", volume=1, take_profit_price=823.5)
        state = settlement_pending(gateway)
        quote(provider, last=825.0, bid=825.0)
        await app.state.autonomy._check_stop(stop_row(app, take["stopId"])[0])
        assert state["sent"][-1]["limitPrice"] == 825.0
        state["pending"] = False
        retry_due(app)
        quote(provider, last=823.5, bid=823.0)  # 回落，仍在止盈价上
        await app.state.autonomy._check_stop(stop_row(app, take["stopId"])[0])
        assert state["sent"][-1]["limitPrice"] == 823.0
        notices = protection_notices(app, session.session_id)
        assert len(notices) == 1 and "止盈触发" in notices[0] and "委托价 823" in notices[0]
    finally:
        app.state.store.close()


async def test_an_entry_fill_protection_reprices_after_pending_and_after_a_restart(tmp_path: Path) -> None:
    """开仓卡成交挂上的保护：等结算价拒收后重启 Runtime，恢复后发出的仍是当时的买一。"""
    app, provider, gateway, session, _ = await armed_card(tmp_path, name="reprice-entry.sqlite3")
    provider.position_items = _long_items(1, today=1)
    state = settlement_pending(gateway)
    stop = app.state.autonomy.list_stops(user_id=USER)[0]
    quote(provider, last=820.5, bid=820.0)
    await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
    assert stop_row(app, stop["stopId"])[0]["last_error"] == "paper_settlement_pending"
    app.state.store.close()

    app = application(tmp_path / "reprice-entry.sqlite3", gateway, provider)
    try:
        state["pending"] = False
        retry_due(app)
        quote(provider, last=812.0, bid=811.5)
        await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
        assert stop_row(app, stop["stopId"])[0]["status"] == "SUBMITTED"
        assert [item["limitPrice"] for item in state["sent"]] == [820.0, 811.5]
        assert len({item["clientOrderId"] for item in state["sent"]}) == 1
    finally:
        app.state.store.close()


async def test_an_unknown_protective_close_is_only_queried_never_repriced_or_resent(tmp_path: Path) -> None:
    """负控：回包丢失（UNKNOWN）的保护平仓只按原 ID 查询，不改价、不重发。"""
    app = application(tmp_path / "reprice-unknown.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    try:
        provider.position_items = _long_items(1, today=1)
        session = app.state.broker.create_session("local", "native", USER)
        stop = await _direct_stop(app, session.session_id, volume=1)
        gateway.submit_error = True
        quote(provider, last=820.5, bid=820.0)
        await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "UNKNOWN"
        gateway.submit_error = False
        quote(provider, last=812.0, bid=811.5)
        await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
        await app.state.autonomy._reconcile_stop(stop_row(app, stop["stopId"])[0])
        assert gateway.submit_count == 1 and gateway.query_count >= 1
        order = app.state.simulation_orders.get(row["close_client_order_id"], user_id=USER)
        assert order["request"]["limitPrice"] == 820.0 and audits(app, "protective-reprice") == []
    finally:
        app.state.store.close()


async def test_a_users_own_limit_order_is_never_repriced(tmp_path: Path) -> None:
    """用户确认的平仓卡等结算价被拒：价格不被静默追；重定价入口只认保护平仓。"""
    from test_simulation_orders import close_proposal
    from trader_runtime.order_boundaries import SimulationOrderError

    app = application(tmp_path / "reprice-manual.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    try:
        state = settlement_pending(gateway)
        order = (await confirm(app, close_proposal(app)))["simulationOrder"]
        assert order["submitPhase"] == "PREPARED"
        original = order["request"]["limitPrice"]
        quote(provider, last=812.0, bid=811.5, ask=812.0)
        with pytest.raises(SimulationOrderError, match="reprice_not_protective"):
            app.state.simulation_orders.reprice_protective_close(
                order["clientOrderId"], user_id=USER, price_intent="aggressive",
                volume=1, position_bucket="today")
        state["pending"] = False
        await app.state.autonomy.run_once()
        assert len(state["sent"]) == 1  # 保护扫描不碰用户自己的单
        await app.state.simulation_orders.dispatch(order["clientOrderId"], user_id=USER)  # 用户点“继续”
        assert state["sent"][-1]["limitPrice"] == original
    finally:
        app.state.store.close()


async def test_a_failed_reprice_write_rolls_back_and_sends_nothing(tmp_path: Path) -> None:
    """重定价写入故障：请求、提案都保持原样，不发单、不通知；下一轮写入成功后照常发出。"""
    app = application(tmp_path / "reprice-fault.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    try:
        provider.position_items = _long_items(1, today=1)
        session = app.state.broker.create_session("local", "native", USER)
        stop = await _direct_stop(app, session.session_id, volume=1)
        state = settlement_pending(gateway)
        quote(provider, last=820.5, bid=820.0)
        await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
        state["pending"] = False
        store, audit = app.state.store, app.state.store.audit

        def broken(proposal_id, action, result, **kwargs):
            if action == "protective-reprice":
                raise RuntimeError("disk_full")
            return audit(proposal_id, action, result, **kwargs)

        store.audit = broken
        retry_due(app)
        quote(provider, last=812.0, bid=811.5)
        await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "ARMED" and row["last_error"] == "disk_full"
        order = app.state.simulation_orders.get(row["close_client_order_id"], user_id=USER)
        assert order["submitPhase"] == "PREPARED" and order["request"]["limitPrice"] == 820.0
        assert store.get(order["proposalId"])["payload"]["limitPrice"] == 820.0
        assert len(state["sent"]) == 1 and protection_notices(app, session.session_id) == []

        store.audit = audit
        quote(provider, last=812.0, bid=811.5)
        await app.state.autonomy._check_stop(stop_row(app, stop["stopId"])[0])
        assert stop_row(app, stop["stopId"])[0]["status"] == "SUBMITTED"
        assert state["sent"][-1]["limitPrice"] == 811.5
        assert len(protection_notices(app, session.session_id)) == 1
    finally:
        app.state.store.close()


async def test_a_shrunk_position_replans_the_undispatched_close_under_a_new_identity(tmp_path: Path) -> None:
    """今昨/数量变化：未派发的保护平仓手数已不成立时，放弃旧单、按新手数和当时价格重建。"""
    app = application(tmp_path / "reprice-shrink.sqlite3", Gateway(), Provider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    try:
        provider.position_items = _long_items(2, today=2)
        session = app.state.broker.create_session("local", "native", USER)
        stop = await _direct_stop(app, session.session_id, volume=2)
        state = settlement_pending(gateway)
        quote(provider, last=820.5, bid=820.0)
        await app.state.autonomy.run_once()
        first = stop_row(app, stop["stopId"])[0]["close_client_order_id"]
        assert state["sent"][-1]["volume"] == 2

        _fill(provider, "close-1", offset="CLOSETODAY", direction="SELL", volume=1, time=2)
        provider.position_items = _long_items(1, today=1)
        state["pending"] = False
        retry_due(app)
        quote(provider, last=812.0, bid=811.5)
        await app.state.autonomy.run_once()
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "SUBMITTED" and row["close_client_order_id"] != first
        assert (state["sent"][-1]["volume"], state["sent"][-1]["limitPrice"]) == (1, 811.5)
        assert app.state.simulation_orders.get(first, user_id=USER)["submitPhase"] == "ABANDONED"
        assert len(audits(app, "protective-close-replan")) == 1
    finally:
        app.state.store.close()

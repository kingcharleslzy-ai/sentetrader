"""下单规划器（docs/ORDER_INTENT_V2.md 第 1 步）：every card entry goes through `order_planner.plan`.

The two safety gaps the step closes, each red on the pre-planner main: an external MCP/CLI caller's
POST /api/simulation/proposals skipped the opening checks and the 今昨 allocation; a 目标持仓 plan's
second (opening) leg skipped the gate. Plus the unified defaults the HTTP route now shares with the AI's tool,
and the strategy monitor's 今昨 split now being the one `close_allocation`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient

from fixtures.card_tools import legacy_card_args
from test_simulation_orders import USER, Gateway, application, quote_now
from test_target_position_flow import (
    _active_plan,
    _approval_events,
    _confirm_stored_card,
    _confirmed_target_first_leg,
    _long,
    _plan_notices,
    _provider_with,
)
from trader_runtime.order_intent import OrderIntent, Price
from trader_runtime.order_planner import Cards, Refuse, close_legs
from trader_runtime.trader_tools import TraderReadOnlyTools


def _expire(provider: Any, symbol: str = "SHFE.au2612") -> None:
    """The catalog says the contract has expired: the opening readiness gate must refuse (instrument_expired)."""
    original = provider.instrument_catalog

    def expired() -> dict[str, Any]:
        catalog = original()
        return {**catalog, "items": [{**item, "expireRestDays": 0} if item.get("symbol") == symbol else item
                                     for item in catalog["items"]]}
    provider.instrument_catalog = expired


async def _post(app, path: str, body: dict[str, Any]) -> tuple[Any, str]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        session = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
        response = await client.post(path, json={"sessionId": session["sessionId"], **body})
    return response, session["sessionId"]


# -- 安全缺口 1：外部 MCP/CLI 的单笔出卡 ---------------------------------------------------------------

async def test_an_http_opening_card_passes_the_order_safety_check(tmp_path: Path) -> None:
    """The ticket and a caller of the raw route share one login token, so the route gets the order-safety
    layer (contract in the catalog and not expired; quote, session, tick, limits in the builder) — an expired
    contract is refused before any card (it used to become a card)."""
    provider = _provider_with([])
    app = application(tmp_path / "http-gate.sqlite3", Gateway(), provider)
    _expire(provider)
    response, _ = await _post(app, "/api/simulation/proposals", {
        "symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "priceIntent": "aggressive", "reason": "外部 AI"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "instrument_expired"
    assert not app.state.store.connection.execute("SELECT 1 FROM proposals").fetchall()
    app.state.store.close()


async def test_stale_bars_stop_the_ai_card_but_not_the_ticket(tmp_path: Path) -> None:
    """Two layers (#947 review): the AI decision readiness (bars, catalog, account freshness) is for AI-made
    cards — the built-in tool here, also voice and an external AI through the capability catalog's tool —
    while the route keeps only the order-safety layer."""
    provider = _provider_with([])
    app = application(tmp_path / "http-two-layers.sqlite3", Gateway(), provider)
    bars = provider.market_workbench
    provider.market_workbench = lambda symbol, duration: {**bars(symbol, duration), "dataStatus": "stale"}
    tools = TraderReadOnlyTools(provider, app.state.adapter.runtime_status, proposals=app.state.proposals)
    session = app.state.broker.create_session("local", "native", USER)
    refused, card = tools.call(
        tool="propose_order", arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1,
                                                 "reason": "AI 开一手"}),
        turn_id="turn-ai", mode="local", session_id=session.session_id, user_id=USER)
    body = json.loads(refused["contentItems"][0]["text"])
    assert card is None and body["errorCode"] == "proposal_gate_not_ready"
    assert "analysis_not_fresh" in body["data"]["proposalReadinessReasons"]
    ticket, _ = await _post(app, "/api/simulation/proposals", {
        "symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "priceIntent": "aggressive", "reason": "下单票"})
    assert ticket.status_code == 201, ticket.text
    assert ticket.json()["readiness"]["source"] == "runtime_order_safety_v1"
    app.state.store.close()


async def test_an_http_shfe_close_without_a_bucket_is_allocated_like_the_ai_tool(tmp_path: Path) -> None:
    provider = _provider_with([_long(2, today=1, hist=1)])
    app = application(tmp_path / "http-bucket.sqlite3", Gateway(), provider)
    # One lot: the 平今平昨 preference (auto) over the closable volumes — the whole lot from 昨仓.
    one, _ = await _post(app, "/api/simulation/proposals", {
        "symbol": "SHFE.au2612", "side": "sell_close", "volume": 1, "reason": "外部 AI 平一手"})
    assert one.status_code == 201, one.text
    assert one.json()["positionBucket"] == "historical"
    # Both lots span 今+昨: one batch card, one confirmation (was close_bucket_required).
    both, session_id = await _post(app, "/api/simulation/proposals", {
        "symbol": "SHFE.au2612", "side": "sell_close", "volume": 2, "reason": "外部 AI 平两手"})
    assert both.status_code == 201, both.text
    assert sorted(o["positionBucket"] for o in both.json()["orders"]) == ["historical", "today"]
    assert [p.get("proposalId") for p in _approval_events(app, session_id)] == [both.json()["proposalId"]]
    # More than is closable: the planner's own line, no card.
    too_many, _ = await _post(app, "/api/simulation/proposals", {
        "symbol": "SHFE.au2612", "side": "sell_close", "volume": 3, "reason": "外部 AI 平三手"})
    assert too_many.status_code == 409 and too_many.json()["detail"]["code"] == "close_volume_exceeds_position"
    app.state.store.close()


async def test_an_http_card_without_a_price_takes_the_one_default_rule(tmp_path: Path) -> None:
    """Close = 对价; open = the account's default price mode (the route used to default every card to 挂单)."""
    provider = _provider_with([_long(1, hist=1)])
    app = application(tmp_path / "http-price.sqlite3", Gateway(), provider)
    app.state.store.update_preferences(USER, {"trade": {"defaultPriceIntent": "aggressive"}})
    close, _ = await _post(app, "/api/simulation/proposals", {
        "symbol": "SHFE.au2612", "side": "sell_close", "volume": 1, "reason": "平一手"})
    assert close.status_code == 201 and close.json()["priceIntent"] == "aggressive"
    opening, _ = await _post(app, "/api/simulation/proposals", {
        "symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "reason": "开一手"})
    assert opening.status_code == 201, opening.text
    assert opening.json()["priceIntent"] == "aggressive" and opening.json()["readiness"]["passed"] is True
    app.state.store.update_preferences(USER, {"trade": {"defaultPriceIntent": "passive"}})
    queued, _ = await _post(app, "/api/simulation/proposals", {
        "symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "reason": "开一手"})
    assert queued.json()["priceIntent"] == "passive"
    app.state.store.close()


async def test_the_http_batch_close_card_is_the_planners(tmp_path: Path) -> None:
    provider = _provider_with([_long(2, today=1, hist=1)])
    app = application(tmp_path / "http-batch.sqlite3", Gateway(), provider)
    response, session_id = await _post(app, "/api/simulation/close-batch-proposals", {"reason": "全平"})
    assert response.status_code == 201, response.text
    card = response.json()
    assert card["priceIntent"] == "aggressive" and card["totalOrders"] == 2
    assert [p["proposalId"] for p in _approval_events(app, session_id)] == [card["proposalId"]]
    provider.position_items = []
    empty, _ = await _post(app, "/api/simulation/close-batch-proposals", {"reason": "全平"})
    assert empty.status_code == 409 and empty.json()["detail"]["code"] == "no_position_to_close"
    app.state.store.close()


# -- 安全缺口 2：目标持仓第 2 段开仓 -----------------------------------------------------------------

async def test_the_relays_opening_leg_passes_the_readiness_gate(tmp_path: Path) -> None:
    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "relay-gate.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=-2, key="relay-gate-first")
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    _expire(provider)  # between the legs the contract stops being tradable
    await app.state.order_watch._sweep_target_plans()
    assert _active_plan(app) is None  # the plan ends instead of offering an opening the gate refuses
    assert not [p for p in _approval_events(app, made["sessionId"]) if p.get("targetPosition", {}).get("step") == 2]
    notices = _plan_notices(app, made["sessionId"])
    assert len(notices) == 1 and "第 2 段没能生成确认卡" in notices[0] and "计划已取消" in notices[0]
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_the_relays_opening_leg_still_comes_when_ready(tmp_path: Path) -> None:
    """The same relay with a ready market: the second leg's card (readiness recorded on it), sent on the first
    leg's confirmation (反手确认一次)."""
    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "relay-ready.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=-2, key="relay-ready-first", intent="passive")
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    provider.quote_datetime = quote_now()
    await app.state.order_watch._sweep_target_plans()
    second = [p for p in _approval_events(app, made["sessionId"]) if p.get("targetPosition", {}).get("step") == 2]
    assert len(second) == 1 and second[0]["side"] == "sell_open" and second[0]["volume"] == 2
    assert second[0]["priceIntent"] == "passive"  # the plan's own price mode (#941)
    assert second[0]["readiness"]["passed"] is True
    assert gateway.submit_count == 2
    app.state.store.close()


# -- 一句两单（第 2 步，主代理审 #950：规划器统一决定，不看确认方式） ------------------------------------

def _legs(*legs: OrderIntent) -> OrderIntent:
    return OrderIntent(act=legs[0].act, legs=legs, reason="一句两单")


async def _plan_sentence(app, provider, intent: OrderIntent) -> tuple[Any, str]:
    tools = TraderReadOnlyTools(provider, app.state.adapter.runtime_status, proposals=app.state.proposals)
    session = app.state.broker.create_session("local", "native", USER)
    outcome = await tools.plan_order(intent, mode="local", session_id=session.session_id, turn_id="turn-legs",
                                     user_id=USER, origin="ai")
    return outcome, session.session_id


async def test_independent_legs_are_all_carded_now_in_order(tmp_path: Path) -> None:
    """Two openings on one contract affect nothing of each other: both cards at once, each its own confirmation —
    no relay plan, nothing waiting on how the first is confirmed."""
    provider = _provider_with([])
    app = application(tmp_path / "legs-independent.sqlite3", Gateway(), provider)
    outcome, _ = await _plan_sentence(app, provider, _legs(
        OrderIntent(act="open", symbol="SHFE.au2612", dir="long", vol=1),
        OrderIntent(act="open", symbol="SHFE.au2612", dir="short", vol=2)))
    assert isinstance(outcome, Cards) and outcome.target_position is None
    assert [(card.proposal["side"], card.proposal["volume"]) for card in outcome.cards] == [
        ("buy_open", 1), ("sell_open", 2)]
    assert all(card.proposal["status"] == "approval_required" for card in outcome.cards)
    assert _active_plan(app) is None
    app.state.store.close()


async def test_dependent_legs_relay_the_next_card_after_the_first_leg_fills(tmp_path: Path) -> None:
    """平掉沪金多单再开多两手: the opening is planned on what the close leaves, so it waits for the close to fill —
    the 目标持仓 relay issues it then. The first card is confirmed through the card's own confirm route, the one
    a click and a spoken 确认 both use: the relay never looks at which it was."""
    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "legs-relay.sqlite3", gateway, provider)
    outcome, session_id = await _plan_sentence(app, provider, _legs(
        OrderIntent(act="close_all", symbol="SHFE.au2612", dir="long"),
        OrderIntent(act="open", symbol="SHFE.au2612", dir="long", vol=2, price=Price(mode="queue"))))
    assert isinstance(outcome, Cards) and len(outcome.cards) == 1
    first = outcome.cards[0].proposal
    assert (first["side"], first["volume"]) == ("sell_close", 1)
    annotation = first["targetPosition"]
    assert annotation["sentence"] is True and annotation["totalSteps"] == 2
    assert [(leg["side"], leg["volume"]) for leg in annotation["legs"]] == [("sell_close", 1), ("buy_open", 2)]
    assert _active_plan(app)["current_proposal_id"] == first["proposalId"]
    await _confirm_stored_card(app, provider, first["proposalId"], "legs-first")
    assert _active_plan(app)["status"] == "awaiting_fill"
    assert not [p for p in _approval_events(app, session_id) if p.get("targetPosition", {}).get("step") == 2]
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    provider.quote_datetime = quote_now()
    await app.state.order_watch._sweep_target_plans()
    second = [p for p in _approval_events(app, session_id) if p.get("targetPosition", {}).get("step") == 2]
    assert len(second) == 1 and (second[0]["side"], second[0]["volume"]) == ("buy_open", 2)
    assert second[0]["priceIntent"] == "passive"  # the leg's own price, carried by the relay
    assert gateway.submit_count == 1  # the relay makes a card, never an order
    app.state.store.close()


async def test_legs_that_tangle_one_contract_with_another_are_refused_before_any_card(tmp_path: Path) -> None:
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "legs-tangled.sqlite3", Gateway(), provider)
    outcome, session_id = await _plan_sentence(app, provider, _legs(
        OrderIntent(act="close", symbol="SHFE.au2612", dir="long", vol=1),
        OrderIntent(act="target", symbol="SHFE.au2612", target=-1)))
    assert isinstance(outcome, Refuse) and outcome.code == "legs_entangled"
    assert not _approval_events(app, session_id) and _active_plan(app) is None
    app.state.store.close()


# -- 策略出卡的今昨：与 AI 平仓卡同一份 close_allocation ---------------------------------------------

def test_strategy_close_legs_use_the_one_allocation() -> None:
    rows = [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 3, "todayVolume": 1, "historicalVolume": 2,
             "frozenToday": 0, "frozenHistorical": 1}]
    # auto: 昨仓 (2, one frozen → 1 closable) is not enough for 2 lots → 今先昨后.
    assert close_legs(rows, symbol="SHFE.rb2701", direction="long", volume=2, preference="auto") == [
        ("today", 1), ("historical", 1)]
    assert close_legs(rows, symbol="SHFE.rb2701", direction="long", volume=1, preference="auto") == [("historical", 1)]
    assert close_legs(rows, symbol="SHFE.rb2701", direction="long", volume=1, preference="today") == [("today", 1)]
    # Frozen lots are never planned (the old split planned them and the builder refused the card).
    assert close_legs(rows, symbol="SHFE.rb2701", direction="long", volume=3, preference="auto") == [
        ("today", 1), ("historical", 1)]
    dce = [{"symbol": "DCE.i2701", "direction": "short", "volume": 2, "todayVolume": 1, "historicalVolume": 1,
            "frozenToday": 1, "frozenHistorical": 0}]
    assert close_legs(dce, symbol="DCE.i2701", direction="short", volume=2, preference="auto") == [("all", 1)]
    assert close_legs(dce, symbol="DCE.i2701", direction="long", volume=1, preference="auto") == []


# -- 第 3 步：合约用词的唯一读法、追问卡的快捷回答 --------------------------------------------------------------

CATALOG = [
    {"symbol": "SHFE.rb2701", "exchangeId": "SHFE", "instrumentId": "rb2701", "instrumentName": "螺纹钢2701",
     "productId": "rb", "isMain": True},
    {"symbol": "SHFE.rb2705", "exchangeId": "SHFE", "instrumentId": "rb2705", "instrumentName": "螺纹钢2705",
     "productId": "rb"},
    {"symbol": "SHFE.au2612", "exchangeId": "SHFE", "instrumentId": "au2612", "instrumentName": "沪金2612",
     "productId": "au", "isMain": True},
]
NAMES = {item["symbol"].lower(): item["instrumentName"] for item in CATALOG}


def _name(symbol: str) -> str:
    return NAMES.get(symbol.lower(), symbol)


def test_contract_words_are_read_one_way_for_every_entry():
    """A code or a named month is that contract only; a product word (or the window's contract, for anything but an
    opening) is whichever month is held; a 全平 list's product words expand over the held months; an unknown word is
    refused, never guessed — the parser's words and the trading AI's propose_order alike."""
    from trader_runtime.order_planner import AccountSnapshot, Ask, resolve_scope, resolve_words
    exact = resolve_words(OrderIntent(act="close", symbol="SHFE.RB2705", dir="long", vol=1), CATALOG)
    assert exact.symbol == "SHFE.rb2705" and exact.product is None
    month = resolve_words(OrderIntent(act="close", symbol="螺纹钢2705", dir="long", vol=1), CATALOG)
    assert month.symbol == "SHFE.rb2705" and month.product is None
    product = resolve_words(OrderIntent(act="close", product="螺纹钢", dir="long", vol=1), CATALOG)
    assert product.symbol == "SHFE.rb2701" and product.product == "rb"
    opening = resolve_words(OrderIntent(act="open", dir="long", vol=1), CATALOG, chart="SHFE.rb2705")
    assert opening.symbol == "SHFE.rb2705" and opening.product is None
    charted = resolve_words(OrderIntent(act="close", dir="long", vol=1), CATALOG, chart="SHFE.rb2705")
    assert charted.symbol == "SHFE.rb2705" and charted.product == "rb"
    assert resolve_words(OrderIntent(act="close_all"), CATALOG, chart="SHFE.rb2705").symbol is None  # every position
    missing = resolve_words(OrderIntent(act="open", symbol="豆二", dir="long", vol=1), CATALOG)
    assert isinstance(missing, Refuse) and missing.issues == ("没找到叫“豆二”的合约，请说品种名或代码",)
    listed = resolve_words(OrderIntent(act="close_all", symbols=("螺纹钢", "SHFE.au2612")), CATALOG)
    assert listed.symbols == ("rb", "SHFE.au2612")
    held = AccountSnapshot.of([{"symbol": "SHFE.rb2705", "direction": "long", "volume": 1},
                               {"symbol": "SHFE.rb2701", "direction": "short", "volume": 2}], None)
    assert resolve_scope(listed, held).symbols == ("SHFE.au2612", "SHFE.rb2701", "SHFE.rb2705")
    nobody = resolve_scope(OrderIntent(act="close_all", symbols=("au",)), AccountSnapshot.of([], None))
    assert isinstance(nobody, Refuse) and nobody.code == "close_position_not_found"  # never widened to everything
    two = resolve_scope(product, AccountSnapshot.of([{"symbol": "SHFE.rb2701", "direction": "long", "volume": 1},
                                                     {"symbol": "SHFE.rb2705", "direction": "long", "volume": 1}], None))
    assert isinstance(two, Ask) and two.missing == "symbol"


def test_the_models_slots_must_rest_on_the_users_words_or_the_context():
    """意图重构第 3 步（槽位出处）：propose_order's intent keeps only what the words — or the window, the account's one
    position, the latest card — vouch for; a contract nothing names is asked with its candidates, an unsaid direction
    or lot count is dropped for the planner to ask, a 全平 needs positions as its object (“…的面板” is interface)."""
    from trader_runtime.order_planner import Ask, ground
    products = ("螺纹钢", "沪金")
    long_au = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 2}]

    def check(intent, said, **kwargs):
        return ground(intent, said, products=products, chart=kwargs.get("chart"),
                      positions=kwargs.get("positions", ()), cards=kwargs.get("cards", ()))

    panel = check(OrderIntent(act="close_all"), "关掉所有持仓的面板", positions=long_au)
    assert isinstance(panel, Ask) and panel.missing == "product" and panel.options == ("SHFE.au2612",)
    assert check(OrderIntent(act="close_all"), "全部平仓") == OrderIntent(act="close_all")
    unnamed = check(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=2), "那就按你说的开两手",
                    chart="SHFE.rb2701", positions=long_au)
    assert isinstance(unnamed, Ask) and unnamed.options == ("SHFE.rb2701", "SHFE.au2612")
    pointed = check(OrderIntent(act="open", dir="long", vol=2), "这个开多两手", chart="SHFE.rb2701")
    assert pointed == OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=2)  # “这个” is the chart's
    guessed = check(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1), "螺纹钢来一手")
    assert guessed == OrderIntent(act="open", symbol="螺纹钢", dir=None, vol=1)  # 买开 was the model's guess
    lots = check(OrderIntent(act="close", symbol="沪金", dir="long", vol=2), "沪金多单平掉")
    assert lots.vol is None and lots.dir == "long"  # how many: the planner asks
    only = check(OrderIntent(act="close", dir="long", vol=1), "平一手", positions=long_au)
    assert only.symbol == "SHFE.au2612"  # the account's one position
    card = [{"symbol": "SHFE.rb2701", "side": "buy_open", "volume": 1}]
    corrected = check(OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=3), "改成三手", cards=card)
    assert corrected == OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=3)  # the card's contract and side
    target = check(OrderIntent(act="target", symbol="螺纹钢", target=3), "螺纹钢的目标持仓调一下")
    assert isinstance(target, Ask) and target.missing == "vol"
    assert check(OrderIntent(act="target", symbol="螺纹钢", target=5), "螺纹钢做到五手多").target == 5
    unclear = check(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=250), "买两百五手螺纹钢")
    assert isinstance(unclear, Ask) and unclear.missing == "vol"  # 250 or 205: asked, never the default size
    bare = check(OrderIntent(act="close_all", symbol="螺纹钢"), "ma螺纹钢全", positions=long_au)
    assert isinstance(bare, Ask)  # no close or position word: no 全平
    two_ways = check(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1, price=Price(mode="counter")),
                     "买一手螺纹钢对手价挂单价")
    assert isinstance(two_ways, Ask) and two_ways.missing == "price"
    assert isinstance(check(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1, price=Price(mode="queue")),
                            "限价买一手螺纹钢"), Ask)
    own = check(OrderIntent(act="open", symbol="沪金", dir="long", vol=1, price=Price(limit=820)), "八百二挂一手沪金多单")
    assert own.price.limit == 820
    unsaid = check(OrderIntent(act="open", symbol="沪金", dir="long", vol=1, price=Price(mode="queue")), "买一手沪金")
    assert unsaid.price == Price()  # a way the words never said: the planner's default


def test_the_models_slots_are_checked_by_value_not_by_mention():
    """#952 Codex: a named product is not any product — the model's contract, a target's side and a stop or take-profit
    must be the ones the words state; otherwise the planner asks."""
    from trader_runtime.order_planner import Ask, ground
    products = ("螺纹钢", "沪金")

    def check(intent, said):
        return ground(intent, said, products=products, chart=None, positions=(), items=CATALOG)

    wrong = check(OrderIntent(act="open", symbol="沪金", dir="long", vol=1), "螺纹钢开多一手，止损3000")
    assert isinstance(wrong, Ask) and wrong.missing == "product"
    wrong_code = check(OrderIntent(act="open", symbol="SHFE.au2612", dir="long", vol=1), "螺纹钢开多一手")
    assert isinstance(wrong_code, Ask)
    assert check(OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=1), "螺纹钢开多一手").symbol == "SHFE.rb2701"
    other_month = check(OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=1), "rb2705开多一手")
    assert isinstance(other_month, Ask)
    flipped = check(OrderIntent(act="target", symbol="螺纹钢", target=-3), "螺纹钢调到三手多单")
    assert isinstance(flipped, Ask) and flipped.missing == "vol"
    assert check(OrderIntent(act="target", symbol="螺纹钢", target=3), "螺纹钢调到三手多单").target == 3
    invented = check(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1,
                                 protection={"stopLossPrice": 3000}), "螺纹钢开多一手")
    assert isinstance(invented, Ask) and invented.missing == "price"
    moved = check(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1, protection={"stopLossPrice": 3050}),
                  "螺纹钢开多一手，止损3000")
    assert isinstance(moved, Ask)  # the price the words gave is 3000
    said = check(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1, protection={"stopLossPrice": 3000}),
                 "螺纹钢开多一手，止损3000")
    assert said.protection == {"stopLossPrice": 3000}
    take = check(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1, protection={"takeProfitPrice": 3000}),
                 "螺纹钢开多一手，止损3000")
    assert isinstance(take, Ask)  # a take-profit the words never asked for
    # 挪到成本: the value must be the held position's open average on the tick grid (多单向上), not any number.
    ticked = [{**item, "priceTick": 1} for item in CATALOG]
    held = [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 2, "openPrice": 3070.4}]

    def at_cost(stop):
        return ground(OrderIntent(act="close", symbol="螺纹钢", dir="long", frac=0.5, remaining_protection={"stopLoss": stop}),
                      "螺纹钢多单平一半，剩下的止损挪到成本", products=products, chart=None, positions=held, items=ticked)

    assert at_cost(3071).remaining_protection == {"stopLoss": 3071}
    assert isinstance(at_cost(3200), Ask) and isinstance(at_cost(3070), Ask)
    assert isinstance(check(OrderIntent(act="close", symbol="螺纹钢", dir="long", frac=0.5,
                                        remaining_protection={"stopLoss": 3071}), "螺纹钢多单平一半，剩下的止损挪到成本"),
                      Ask)  # no position to read the cost from: asked


def test_the_models_action_and_numbers_are_the_ones_the_words_say():
    """#952 Codex: the action is the verb said (平 / 开 / 调到·做到 / 反手) — “螺纹钢多单平一手” read as an opening asks;
    the digits after a product name are its month, never a limit price; an earlier card is no evidence of a size
    (the caller passes a card only for an explicit correction of the one waiting card)."""
    from trader_runtime import order_planner
    from trader_runtime.order_planner import Ask, ground
    products = ("螺纹钢", "沪金")

    def check(intent, said):
        return ground(intent, said, products=products, chart=None, positions=(), items=CATALOG)

    opened = check(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1), "螺纹钢多单平一手")
    assert isinstance(opened, Ask) and opened.missing == "act"
    assert check(OrderIntent(act="close", symbol="螺纹钢", dir="long", vol=1), "螺纹钢多单平一手").act == "close"
    assert isinstance(check(OrderIntent(act="reverse", symbol="螺纹钢"), "螺纹钢调到三手多单"), Ask)
    assert isinstance(check(OrderIntent(act="close_all"), "螺纹钢开多一手"), Ask)
    assert check(OrderIntent(act="open", symbol="螺纹钢", dir="short", vol=2), "空两手螺纹钢").vol == 2
    month = check(OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=1, price=Price(limit=2701)),
                  "螺纹钢2701开多一手")
    assert isinstance(month, Ask) and month.missing == "price"
    code = check(OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=1, price=Price(limit=2701)),
                 "rb2701开多一手")
    assert isinstance(code, Ask) and code.missing == "price"
    sized = check(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=5, protection={"stopLossPrice": 3000}),
                  "螺纹钢开多，止损 3000")
    assert sized.vol is None  # 5 lots from an unrelated card: the planner asks (or takes the default size)
    # A condition the model dropped is restored (no card now); a 平今/平昨 the words never said goes back to auto.
    dropped = check(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1, protection={"stopLossPrice": 2900}),
                    "跌到3000就开多一手螺纹钢，止损2900")
    assert dropped.stance == "conditional" and dropped.cond == {"type": "price", "op": "<=", "value": 3000}
    assert isinstance(order_planner.plan(dropped, None, order_planner.Prefs(), None), Refuse)
    bucket = check(OrderIntent(act="close", symbol="螺纹钢", dir="long", vol=1, bucket="yesterday"), "螺纹钢今仓平一手")
    assert bucket.bucket == "today"  # the 平昨 nobody said is dropped; the 今仓 the words said is the one (#932)
    assert check(OrderIntent(act="close", symbol="螺纹钢", dir="long", vol=1, bucket="today"),
                 "螺纹钢今仓平一手").bucket == "today"
    assert check(OrderIntent(act="close", symbol="螺纹钢", dir="long", vol=1, bucket="today"),
                 "螺纹钢多单平一手").bucket == "auto"


def test_only_an_explicit_correction_of_the_one_waiting_card_lends_its_slots():
    """#952 Codex: propose_order keeps a card's contract, side and size only when this sentence corrects the one card
    still waiting (改成三手 / 不对，是卖); a confirmed, rejected or second card, or an unrelated sentence, lends nothing.
    A spoken reply to the card the voice front just retired (card_reply) corrects that card."""
    from types import SimpleNamespace
    later = "2999-01-01T00:00:00+00:00"

    def card(status, volume, expires=later):
        return {"status": status, "approvalExpiresAt": expires,
                "payload": {"symbol": "SHFE.rb2701", "side": "buy_open", "volume": volume}}

    def lent(records, said, **kwargs):
        tools = TraderReadOnlyTools.__new__(TraderReadOnlyTools)
        tools.proposals = SimpleNamespace(store=SimpleNamespace(recent_proposals=lambda session_id, limit: records))
        return tools._correction_card("s", said, ("螺纹钢",), **kwargs)

    waiting = card("approval_required", 5)
    assert lent([waiting], "改成三手") == [{"symbol": "SHFE.rb2701", "side": "buy_open", "volume": 5,
                                          "priceIntent": None, "limitPrice": None}]
    assert lent([waiting], "不对，是卖")
    assert lent([waiting], "螺纹钢开多，止损 3000") == []  # not a correction
    assert lent([card("executed", 5)], "改成三手") == []
    assert lent([card("approval_required", 5, "2000-01-01T00:00:00+00:00")], "改成三手") == []  # expired
    assert lent([waiting, card("approval_required", 2)], "改成三手") == []  # which one: not this rule's to guess
    assert lent([card("rejected", 5)], "换成螺纹钢") == []
    assert lent([card("rejected", 5)], "换成螺纹钢", card_reply=True)


def test_an_unsized_opening_takes_the_accounts_default_size_when_set():
    """#952 Codex: 设置 → 交易 → 默认手数 above 1 sizes an opening that names none (the fast path and propose_order
    both plan through it); the factory 1 keeps asking 开几手."""
    from types import SimpleNamespace

    from trader_runtime.order_planner import prefs_for

    def prefs(volume):
        store = SimpleNamespace(user_preferences=lambda user_id: SimpleNamespace(trade=SimpleNamespace(defaultVolume=volume)))
        return prefs_for(store, "u")

    from test_intent_corpus import NAMED, _corpus_snapshot, _CorpusProposals, _CorpusProvider
    from trader_runtime import order_planner
    assert prefs(4).default_volume == 4 and prefs(1).default_volume is None
    ready = {"userFacing": {}, "proposalReady": True, "proposalReadinessReasons": [], "openLimit": {},
             "source": "corpus", "market": {}}
    snapshot = _corpus_snapshot(NAMED["short1_rb"])
    context = order_planner.PlanContext(provider=_CorpusProvider(), proposals=_CorpusProposals(list(snapshot.positions)),
                                        session_id="s", turn_id="t", user_id="u",
                                        decision_context=lambda symbol: (ready, None))
    unsized = OrderIntent(act="open", symbol="SHFE.rb2701", dir="long")
    asked = order_planner.plan(unsized, snapshot, prefs(1), context)
    assert isinstance(asked, order_planner.Ask) and asked.missing == "vol"
    carded = order_planner.plan(unsized, snapshot, prefs(4), context)
    assert isinstance(carded, Cards)
    assert [order["vol"] for card in carded.cards for order in card.proposal["orders"]] == [4]


def test_a_target_limit_rides_every_step_of_the_relay():
    """#952 Codex: “把螺纹调到三手多单，限价 3100” — the user's limit prices the first card and is stored on every
    later step the relay issues (order_watch reads the step's limitPrice)."""
    from test_intent_corpus import NAMED, _corpus_snapshot, _CorpusProposals, _CorpusProvider
    from trader_runtime import order_planner
    ready = {"userFacing": {}, "proposalReady": True, "proposalReadinessReasons": [], "openLimit": {},
             "source": "corpus", "market": {}}
    snapshot = _corpus_snapshot(NAMED["short1_rb"])
    proposals = _CorpusProposals(list(snapshot.positions))
    stored: dict[str, Any] = {}
    proposals.store.create_target_plan = lambda **kwargs: (stored.update(kwargs) or {"id": "plan-1"}, None)
    context = order_planner.PlanContext(provider=_CorpusProvider(), proposals=proposals, session_id="s", turn_id="t",
                                        user_id="u", decision_context=lambda symbol: (ready, None))
    intent = OrderIntent(act="target", symbol="SHFE.rb2701", target=3, price=Price(limit=3100))
    outcome = order_planner.plan(intent, snapshot, order_planner.Prefs(), context)
    assert isinstance(outcome, Cards)
    assert [order["price"] for card in outcome.cards for order in card.proposal["orders"]] == ["limit:3100"]
    legs = stored["leg_json"]["plan"]["legs"]
    assert len(legs) == 2 and all(leg["limitPrice"] == 3100 for leg in legs)


def test_the_question_cards_quick_answers_are_whole_orders_the_parser_reads():
    """The planner's Ask (方向, two months held) offers each answer as the user's own order with the slot filled, said in
    words the fast parser reads — so a tap is a sure order, planned at once, never a second question."""
    from trader_runtime.order_intent_parse import ParseContext, parse
    from trader_runtime.order_planner import Ask, ask_choices
    context = ParseContext(products=("螺纹钢", "沪金"))
    opening = OrderIntent(act="open", symbol="SHFE.rb2701", vol=1, price=Price(limit=3100))
    choices = ask_choices(opening, Ask("dir", "开多还是开空？", ("long", "short")), _name)
    assert [choice["label"] for choice in choices] == ["买开（做多）", "卖开（做空）"]
    read = [parse(choice["prompt"], context) for choice in choices]
    assert [(item.act, item.dir, item.vol, item.symbol, item.price.limit) for item in read] == [
        ("open", "long", 1, "螺纹钢2701", 3100), ("open", "short", 1, "螺纹钢2701", 3100)]
    close = OrderIntent(act="close", symbol="SHFE.rb2701", product="rb", dir="long", frac=0.5)
    months = ask_choices(close, Ask("symbol", "哪个月份？", ("SHFE.rb2701", "SHFE.rb2705")), _name)
    assert [choice["label"] for choice in months] == ["螺纹钢2701", "螺纹钢2705"]
    month = parse(months[1]["prompt"], context)
    assert (month.act, month.symbol, month.dir, month.frac) == ("close", "螺纹钢2705", "long", 0.5)
    target = OrderIntent(act="target", symbol="SHFE.rb2701", product="rb", target=-2)
    said = parse(ask_choices(target, Ask("symbol", "哪个？", ("SHFE.rb2705",)), _name)[0]["prompt"], context)
    assert (said.act, said.target, said.symbol) == ("target", -2, "螺纹钢2705")
    assert ask_choices(opening, Ask("vol", "开几手？"), _name) == []  # a free answer: no buttons


# -- #932 P2 集中修：意图解析与规划器 ----------------------------------------------------------------------

def _ground(intent, said, *, positions=(), cards=(), items=CATALOG):
    from trader_runtime.order_planner import ground
    return ground(intent, said, products=("螺纹钢", "沪金", "沪银", "豆二"), chart=None, positions=positions, cards=cards,
                  items=items)


def test_a_month_said_after_a_product_name_binds_the_models_contract():
    """“螺纹钢2705开多一手”: the model's rb2701 is not the month said — asked, never carded."""
    from trader_runtime.order_planner import Ask
    said = "螺纹钢2705开多一手"
    assert isinstance(_ground(OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=1), said), Ask)
    # The one contract the words leave is the card's: a bare product word of it is pinned to the month said.
    assert _ground(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1), said).symbol == "SHFE.rb2705"
    assert _ground(OrderIntent(act="open", symbol="SHFE.rb2705", dir="long", vol=1), said).symbol == "SHFE.rb2705"
    assert _ground(OrderIntent(act="open", symbol="螺纹钢2705", dir="long", vol=1), said).symbol == "SHFE.rb2705"


def test_a_correction_changes_only_the_slots_it_states():
    """改单（#932）：“改成三手” keeps the waiting card's action, side and price — the model's 平仓 is put back to the
    card's 开仓 — and the size it states is the words', never the card's old 5 lots; a contract it names is its own."""
    from trader_runtime.order_planner import Ask
    card = [{"symbol": "SHFE.rb2701", "side": "buy_open", "volume": 5, "priceIntent": "aggressive", "limitPrice": 3067}]
    kept_old = _ground(OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=5), "改成三手", cards=card)
    assert kept_old.vol == 3  # the one size the words say (A15): never the card's 5, never cleared for the default
    left_out = _ground(OrderIntent(act="open", symbol="SHFE.rb2701", dir="long"), "改成三手", cards=card)
    assert left_out.vol == 3
    two_sizes = _ground(OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=5), "三手还是两手吧", cards=card)
    assert isinstance(two_sizes, Ask) and two_sizes.missing == "vol"
    # A16: a correction naming nothing keeps the card's exact contract — another month of the product is no card's.
    month = _ground(OrderIntent(act="open", symbol="SHFE.rb2705", dir="long", vol=3), "改成三手", cards=card)
    assert isinstance(month, Ask) and month.missing == "product"
    product = _ground(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=3), "改成三手", cards=card)
    assert product.symbol == "SHFE.rb2701"  # the product word is the card's own month, not the main contract
    unnamed = _ground(OrderIntent(act="open", dir="long", vol=3), "改成三手", cards=card)
    assert unnamed.symbol == "SHFE.rb2701"
    # PR #997 Codex: “改成这个合约” points at the chart's contract, never the card's old one.
    from trader_runtime.order_planner import ground
    pointed = ground(OrderIntent(act="open", dir="long", vol=3), "改成这个合约三手", products=("螺纹钢", "沪金"),
                     chart="SHFE.au2612", positions=(), cards=card, items=CATALOG)
    assert pointed.symbol == "SHFE.au2612"  # the chart's contract, never the card's
    old = ground(OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=3), "改成这个合约三手",
                 products=("螺纹钢", "沪金"), chart="SHFE.au2612", positions=(), cards=card, items=CATALOG)
    assert isinstance(old, Ask) and old.missing == "product"
    swapped = _ground(OrderIntent(act="close", symbol="SHFE.rb2701", dir="long", vol=3, price=Price(mode="counter")),
                      "改成三手", cards=card)
    assert (swapped.act, swapped.dir, swapped.vol, swapped.price) == ("open", "long", 3, Price(mode="counter"))
    flipped = _ground(OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=5), "不对，是卖", cards=card)
    assert flipped.dir is None and flipped.vol == 5  # the side said is 卖: the card's 买 does not stand in
    other = _ground(OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=5), "换成沪金", cards=card)
    assert isinstance(other, Ask) and other.missing == "product"


def test_each_protection_rests_on_its_own_words_and_its_own_contracts_cost():
    """#932: a stop and a take-profit each take the number said with them (never swapped), a 撤 said of one is not the
    other's, and 挪到成本 on a named month is that month's cost, not another month's of the product."""
    from dataclasses import replace

    from trader_runtime.order_planner import Ask
    said = "螺纹钢开多一手，止损3000止盈3200"
    both = {"stopLossPrice": 3000, "takeProfitPrice": 3200}
    assert _ground(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1, protection=both), said).protection == both
    swapped = {"stopLossPrice": 3200, "takeProfitPrice": 3000}
    assert isinstance(_ground(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1, protection=swapped), said), Ask)
    held = [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 2, "openPrice": 3070.4}]
    moved = "螺纹钢多单平一手，撤掉止损，止盈改到3200"
    fine = OrderIntent(act="close", symbol="螺纹钢", dir="long", vol=1,
                       remaining_protection={"stopLoss": None, "takeProfit": 3200})
    assert _ground(fine, moved, positions=held).remaining_protection == {"stopLoss": None, "takeProfit": 3200}
    dropped_other = replace(fine, remaining_protection={"takeProfit": None})
    assert isinstance(_ground(dropped_other, moved, positions=held), Ask)  # the 撤 was said of the stop
    ticked = [{**item, "priceTick": 1} for item in CATALOG]
    two = [*held, {"symbol": "SHFE.rb2705", "direction": "long", "volume": 1, "openPrice": 3200.2}]

    def at_cost(symbol, stop):
        return _ground(OrderIntent(act="close", symbol=symbol, dir="long", vol=1, remaining_protection={"stopLoss": stop}),
                       "rb2705平一手，剩下的止损挪到成本", positions=two, items=ticked)

    assert at_cost("SHFE.rb2705", 3201).remaining_protection == {"stopLoss": 3201}
    assert isinstance(at_cost("SHFE.rb2705", 3071), Ask)  # rb2701's cost is not rb2705's
    # The guard's boundary (PR #997): a 撤 / 成本 said in the words counts as said; which protection it belongs to is the
    # model's call and the card the user's check — it never asks over grammar (“止损取消止盈3200”“止损移到成本止盈3200”).
    for said, protection in (("螺纹钢多单平一手，止损取消止盈3200", {"stopLoss": None, "takeProfit": 3200}),
                             ("螺纹钢多单平一手，止损给我撤掉止盈3200", {"stopLoss": None, "takeProfit": 3200}),
                             ("螺纹钢多单平一手，止损3000撤掉止盈", {"stopLoss": 3000, "takeProfit": None})):
        kept = _ground(replace(fine, remaining_protection=protection), said, positions=held)
        assert kept.remaining_protection == protection, said
    costed = _ground(OrderIntent(act="close", symbol="螺纹钢", dir="long", vol=1,
                                 remaining_protection={"stopLoss": 3071, "takeProfit": 3200}),
                     "平一手螺纹钢多单，剩下三手止损移到成本止盈3200", positions=held, items=ticked)
    assert costed.vol == 1 and costed.remaining_protection == {"stopLoss": 3071, "takeProfit": 3200}
    # Still never made up: a 撤 nobody said.
    assert isinstance(_ground(replace(fine, remaining_protection={"takeProfit": None}), "螺纹钢多单平一手，止盈3200",
                              positions=held), Ask)


def test_a_bucket_or_price_way_said_once_is_put_back_and_a_limit_beside_a_way_is_asked():
    """#932: 平今 / 对手价 the model left out comes back from the words (not the preference or the account default);
    “限价3000按对手价开多” says two prices — asked."""
    from trader_runtime.order_planner import Ask
    bucket = _ground(OrderIntent(act="close", symbol="螺纹钢", dir="long", vol=1), "螺纹钢平今一手")
    assert bucket.bucket == "today"
    way = _ground(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1), "对手价开多一手螺纹钢")
    assert way.price == Price(mode="counter")
    own = _ground(OrderIntent(act="open", symbol="沪金", dir="long", vol=1, price=Price(mode="queue", limit=820)),
                  "八百二挂一手沪金多单")
    assert own.price == Price(limit=820)  # the way nobody said goes; the user's own limit stays
    for price in (Price(mode="counter", limit=3000), Price(limit=3000), Price(mode="counter")):
        both = _ground(OrderIntent(act="open", symbol="螺纹钢", dir="long", vol=1, price=price), "限价3000按对手价开多一手螺纹钢")
        assert isinstance(both, Ask) and both.missing == "price"


def test_one_order_from_words_with_two_actions_or_two_products_is_asked():
    """#932 A11（模型路径离线评测误出卡）：“卖一手螺纹平掉”（卖开还是卖平）、“豆二挂一手沪金多单”（“八百二”被听成
    “豆二”）——模型只给一单，哪一单是用户的，追问；全平点名几个品种照常。"""
    from trader_runtime.order_planner import Ask
    long_rb = [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 1}]
    for intent in (OrderIntent(act="close", symbol="螺纹钢", dir="long", vol=1),
                   OrderIntent(act="open", symbol="螺纹钢", dir="short", vol=1)):
        asked = _ground(intent, "卖一手螺纹钢平掉", positions=long_rb)
        assert isinstance(asked, Ask) and asked.missing == "act", intent
    for symbol in ("沪金", "豆二"):
        asked = _ground(OrderIntent(act="open", symbol=symbol, dir="long", vol=1), "豆二挂一手沪金多单")
        assert isinstance(asked, Ask) and asked.missing == "product", symbol
    both = _ground(OrderIntent(act="close_all", symbols=("螺纹钢", "沪金")), "把螺纹钢和沪金都平了", positions=long_rb)
    assert both == OrderIntent(act="close_all", symbols=("螺纹钢", "沪金"))
    part = _ground(OrderIntent(act="close_all", symbols=("螺纹钢",)), "把螺纹钢和沪金都平了", positions=long_rb)
    assert isinstance(part, Ask) and part.missing == "product"  # less than said (PR #997 review)
    months = [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 1},
              {"symbol": "SHFE.rb2705", "direction": "long", "volume": 1}]
    bare = _ground(OrderIntent(act="close_all", symbols=("螺纹钢",)), "把rb2701和rb2705都平了", positions=months)
    assert isinstance(bare, Ask)  # a product word would close every month held, not the two named
    exact = OrderIntent(act="close_all", symbols=("SHFE.rb2701", "SHFE.rb2705"))
    assert _ground(exact, "把rb2701和rb2705都平了", positions=months) == exact


def test_a_close_all_picked_by_profit_or_loss_is_asked_not_widened():
    """#932 A12（所有者 2026-10-19）：“平掉盈利的单子”“把亏的都平了”——意图没有按盈亏挑持仓的槽位，模型读成全平会
    多平：追问平哪些，不出卡；说了合约的照常。"""
    from trader_runtime.order_planner import Ask
    held = [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 1},
            {"symbol": "SHFE.au2612", "direction": "short", "volume": 1}]
    for said in ("平掉盈利的单子", "把亏的都平了", "关掉所有持仓里盈利的", "浮亏的都平掉"):
        asked = _ground(OrderIntent(act="close_all"), said, positions=held)
        assert isinstance(asked, Ask) and asked.missing == "product", said
        assert asked.options == ("SHFE.rb2701", "SHFE.au2612")
    one = _ground(OrderIntent(act="close", dir="long", vol=1), "平掉亏的那一手", positions=held[:1])
    assert isinstance(one, Ask)
    assert _ground(OrderIntent(act="close_all"), "全部平仓", positions=held) == OrderIntent(act="close_all")
    named = OrderIntent(act="close_all", symbols=("螺纹钢",))
    picked = _ground(named, "把螺纹钢里亏的都平了", positions=held)
    assert isinstance(picked, Ask)  # a pick inside the named product: still more than was said
    # PR #997 Codex: a loss said as the reason, in its own clause, picks nothing — the named 全平 stands.
    assert _ground(named, "螺纹钢亏钱了，把螺纹钢全平了", positions=held) == named


def test_a_spoken_alias_the_catalog_does_not_name_finds_its_contract():
    """#932：本站账户“卖一手白银”交模型——the model writes 白银, the catalog names 沪银: the same contract, not 没找到."""
    from trader_runtime.order_planner import resolve_words
    catalog = [*CATALOG, {"symbol": "SHFE.ag2612", "exchangeId": "SHFE", "instrumentId": "ag2612",
                          "instrumentName": "沪银2612", "productId": "ag", "isMain": True}]
    assert resolve_words(OrderIntent(act="open", symbol="白银", dir="short", vol=1), catalog).symbol == "SHFE.ag2612"
    assert resolve_words(OrderIntent(act="open", symbol="白银2612", dir="short", vol=1), catalog).symbol == "SHFE.ag2612"
    # #932 F4: the catalog also lists the foreign main contracts (外盘图表) — 白银 is still 沪银, never 美白银主连.
    foreign = [*catalog, {"symbol": "KQD.m@COMEX.SI", "exchangeId": "KQD", "instrumentId": "m@COMEX.SI",
                          "instrumentName": "美白银主连", "productId": "COMEX.SI", "isMain": False}]
    assert resolve_words(OrderIntent(act="open", product="白银", dir="short", vol=1), foreign).symbol == "SHFE.ag2612"
    # The USD sub-account's catalog has no 沪金: 黄金 still finds its COMEX month by name (#1034 review).
    usd = [{"symbol": "CME.GCEZ26", "exchangeId": "CME", "instrumentId": "GCEZ26", "instrumentName": "COMEX黄金2612",
            "productId": "GCE", "isMain": True}]
    assert resolve_words(OrderIntent(act="open", product="黄金", dir="long", vol=1), usd).symbol == "CME.GCEZ26"


def test_an_unreadable_catalog_is_said_not_read_as_an_unknown_contract():
    """#932 F4 (10-19 23:32 production: 本站持沪银多，“卖一手白银”回“没找到叫‘白银’的合约”): one failed catalog read
    made the propose_order words go on as said, and the planner's next read refused 白银 as unknown. The sidecar
    provider now serves its last catalog through a failed read (marked cached); a provider that never read one, or
    an empty catalog, is said as 合约目录暂时读不到 — for the words that need it, not for a 全平 naming nothing."""
    import pytest

    from trader_runtime.data_providers import SidecarReadOnlyDataProvider
    from trader_runtime.order_planner import CATALOG_UNREADABLE, resolve_words
    from trader_runtime.sidecar_http import SidecarHttpError

    assert resolve_words(OrderIntent(act="open", symbol="白银", dir="short", vol=1), []) == CATALOG_UNREADABLE
    assert resolve_words(OrderIntent(act="close_all", symbols=("螺纹钢",)), []) == CATALOG_UNREADABLE
    assert resolve_words(OrderIntent(act="close_all"), []) == OrderIntent(act="close_all")

    provider = SidecarReadOnlyDataProvider(base_url="http://127.0.0.1:9", token="t" * 32, symbols=())
    answers: list[Any] = [SidecarHttpError("timeout")]

    def fetch(path: str) -> Any:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    provider._fetch = fetch  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="sidecar_instrument_catalog_unavailable"):
        provider.instrument_catalog()  # never read: nothing to serve
    catalog = {"schemaVersion": 1, "dataStatus": "normal", "isCached": False, "items": CATALOG}
    answers += [catalog, SidecarHttpError("timeout"), {"schemaVersion": 1, "items": []}, {"bad": True}]
    assert provider.instrument_catalog() == catalog
    for _ in range(3):  # a timeout, a still-loading empty answer, a malformed one: the last catalog, marked
        served = provider.instrument_catalog()
        assert served["items"] == CATALOG and served["isCached"] is True


def _corpus_context(provider=None, proposals=None, positions=()):
    from test_intent_corpus import _CorpusProposals, _CorpusProvider
    from trader_runtime import order_planner
    ready = {"userFacing": {}, "proposalReady": True, "proposalReadinessReasons": [], "openLimit": {},
             "source": "corpus", "market": {}}
    return order_planner.PlanContext(provider=provider or _CorpusProvider(),
                                     proposals=proposals or _CorpusProposals(list(positions)),
                                     session_id="s", turn_id="t", user_id="u", decision_context=lambda symbol: (ready, None))


def test_two_months_of_one_product_are_independent_legs():
    """#932: 平 rb2701 + 开 rb2705 are two books — two cards now, not legs_entangled."""
    from test_intent_corpus import NAMED, _corpus_snapshot, _CorpusProvider
    from trader_runtime import order_planner

    class TwoMonths(_CorpusProvider):
        def instrument_catalog(self):
            items = super().instrument_catalog()["items"]
            return {"items": [*items, {"symbol": "SHFE.rb2705", "exchangeId": "SHFE", "instrumentId": "rb2705",
                                       "instrumentName": "螺纹2705", "productId": "rb"}]}

    snapshot = _corpus_snapshot(NAMED["long1_rb"])
    outcome = order_planner.plan(_legs(OrderIntent(act="close", symbol="SHFE.rb2701", dir="long", vol=1),
                                       OrderIntent(act="open", symbol="SHFE.rb2705", dir="long", vol=1)),
                                 snapshot, order_planner.Prefs(),
                                 _corpus_context(TwoMonths(), positions=snapshot.positions))
    assert isinstance(outcome, Cards) and outcome.target_position is None
    assert [(o["symbol"], o["side"]) for card in outcome.cards for o in card.proposal["orders"]] == [
        ("SHFE.rb2701", "sell_close"), ("SHFE.rb2705", "buy_open")]


def test_the_planner_fails_closed_on_an_unreadable_catalog_account_or_batch_read():
    """#947 review / #932: an opening on a catalog that cannot be read is refused (not carded on invented quotes); a
    今仓全平 on an unreadable account says 读不到 (not 没有今仓); a 跨今昨 close whose batch read fails is a refusal,
    never an exception (an HTTP 500)."""
    from test_intent_corpus import NAMED, _corpus_snapshot, _CorpusProposals, _CorpusProvider
    from trader_runtime import order_planner

    class Unreadable(_CorpusProvider):
        def instrument_catalog(self):
            raise RuntimeError("catalog down")

    opening = order_planner.plan(OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=1), None,
                                 order_planner.Prefs(), _corpus_context(Unreadable()))
    assert isinstance(opening, Refuse) and opening.code == "instrument_catalog_unavailable"
    today = order_planner.plan(OrderIntent(act="close_all", bucket="today"), order_planner.AccountSnapshot.unreadable(),
                               order_planner.Prefs(), _corpus_context())
    assert isinstance(today, Refuse) and today.code == "account_positions_unreadable"
    snapshot = _corpus_snapshot(NAMED["long3_rb_today1_yd2"])
    proposals = _CorpusProposals(list(snapshot.positions))

    def broken(*args, **kwargs):
        raise KeyError("SHFE.rb2701")
    proposals.create_close_batch = broken  # type: ignore[method-assign]
    span = order_planner.plan(OrderIntent(act="close", symbol="SHFE.rb2701", dir="long", vol=3), snapshot,
                              order_planner.Prefs(), _corpus_context(proposals=proposals))
    assert isinstance(span, Refuse) and span.code == "invalid_close_batch_proposal" and span.form == "unavailable"


def test_replay_and_the_local_fake_still_card_openings_from_their_own_catalog():
    """#968 review: the fail-closed catalog check refuses no opening on a source that works — the replay child's
    catalog is the instrument the sidecar exports with every replay state, the local fake has its own; an opening on
    either still cards (the replay ticket's HTTP card: test_replay.test_replay_http_orders_and_accounts_are_isolated).
    A replay before its first state has no catalog and no quotes either: refused."""
    import time

    from test_replay import IDENTIFIER, SYMBOL, HistoricalSource
    from trader_runtime import order_planner
    from trader_runtime.data_providers import FakeReadOnlyDataProvider
    from trader_runtime.replay import ReplayProvider

    replay = ReplayProvider(HistoricalSource(), IDENTIFIER)
    opening = OrderIntent(act="open", symbol=SYMBOL, dir="long", vol=1)
    loading = order_planner.plan(opening, None, order_planner.Prefs(), _corpus_context(replay))
    assert isinstance(loading, Refuse) and loading.code == "instrument_catalog_unavailable"
    replay.snapshot, replay.received_at = HistoricalSource().data, time.monotonic()
    for provider in (replay, FakeReadOnlyDataProvider()):
        assert isinstance(order_planner.catalog_item(provider, SYMBOL), dict)
        carded = order_planner.plan(opening, None, order_planner.Prefs(), _corpus_context(provider))
        assert isinstance(carded, Cards), carded


def test_propose_order_says_which_argument_is_missing(tmp_path: Path) -> None:
    """#932 实测：a propose_order call the schema rejects says the field by name, not only invalid_tool_arguments."""
    provider = _provider_with([])
    app = application(tmp_path / "args.sqlite3", Gateway(), provider)
    tools = TraderReadOnlyTools(provider, app.state.adapter.runtime_status, proposals=app.state.proposals)
    session = app.state.broker.create_session("local", "native", USER)
    response, card = tools.call(tool="propose_order", arguments={"act": "open", "symbol": "SHFE.au2612"},
                                turn_id="t", mode="local", session_id=session.session_id, user_id=USER)
    body = json.loads(response["contentItems"][0]["text"])
    assert card is None and body["errorCode"] == "invalid_tool_arguments"
    assert "缺少 reason" in body["warning"]
    response, _ = tools.call(tool="propose_order", arguments={"reason": "买"}, turn_id="t", mode="local",
                             session_id=session.session_id, user_id=USER)
    assert "act 必填" in json.loads(response["contentItems"][0]["text"])["warning"]

    def unreadable() -> dict[str, Any]:
        raise RuntimeError("sidecar_instrument_catalog_unavailable")
    provider.instrument_catalog = unreadable  # #932 F4: no catalog ever read — said, not 没找到叫“白银”
    response, card = tools.call(tool="propose_order", arguments={"act": "open", "symbol": "白银", "dir": "short",
                                                                 "vol": 1, "reason": "卖一手白银"},
                                turn_id="t", mode="local", session_id=session.session_id, user_id=USER)
    assert card is None and "合约目录暂时读不到" in json.dumps(json.loads(response["contentItems"][0]["text"]),
                                                               ensure_ascii=False)
    app.state.store.close()


async def test_a_target_card_given_up_or_timed_out_ends_its_plan_at_once(tmp_path: Path) -> None:
    """#932 发布回读：目标持仓卡作废后计划拖 30 分钟才取消——now the plan ends with the card (放弃 or 超时)."""
    from datetime import UTC, datetime, timedelta

    from test_target_position_flow import _call, _tools
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "plan-ends.sqlite3", Gateway(), provider)
    _, card = _call(app, _tools(app, provider), target=-1)
    record = app.state.store.get(card[1]["proposalId"])
    assert _active_plan(app)["current_proposal_id"] == record["id"]
    await app.state.proposals.reject(record["id"], user_id=USER, proposal_version=record["version"],
                                     idempotency_key="give-up-target-card")
    assert _active_plan(app) is None
    results = [p for p in _approval_events(app, record["sessionId"]) if p.get("status") == "rejected"]
    assert "目标持仓计划也已取消" in results[-1]["message"]
    _, card = _call(app, _tools(app, provider), target=-1, turn="turn-again")
    proposal_id = card[1]["proposalId"]
    app.state.store.connection.execute("UPDATE proposals SET approval_expires_at = ? WHERE id = ?",
                                       ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), proposal_id))
    app.state.store.connection.commit()
    assert await app.state.lifecycle.expire_approvals() >= 1
    assert _active_plan(app) is None
    app.state.store.close()


async def test_a_named_account_is_explicit_even_when_it_is_the_conversations(tmp_path: Path) -> None:
    """#932: the 账户头 naming the conversation's own account still marks the 目标持仓 plan accountExplicit — its next
    leg stays on that account if the conversation switches — and the card carries no override note."""
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "named-same.sqlite3", Gateway(), provider)
    own = app.state.accounts.default.account_id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        session = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
        response = await client.post("/api/simulation/target-position/proposals",
                                     headers={"X-AI-Trader-Account": own}, json={
                                         "sessionId": session["sessionId"], "symbol": "SHFE.au2612", "targetNet": -1,
                                         "reason": "外部 CLI 指定本账户"})
    assert response.status_code == 201, response.text
    assert _active_plan(app)["leg"].get("accountExplicit") is True
    assert "accountOverride" not in json.dumps(response.json(), ensure_ascii=False)
    app.state.store.close()


def test_a_relay_frozen_to_its_account_while_the_conversation_moved_stays_bound_to_it():
    """#932 A6 review (PR #997 Codex): the plan was frozen to account A (its reads, its first card) while the conversation
    had already moved to B. The plan records that it is bound to A (accountExplicit), so after leg one fills its next
    leg still goes out on A — a reversal does not stop after only closing. Same account: not bound (a later switch of
    the conversation still ends an implicit plan, as before)."""
    from dataclasses import replace

    from test_intent_corpus import _CorpusProposals
    from trader_runtime import order_planner
    long1 = [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 1, "todayVolume": 1, "historicalVolume": 0,
              "frozenToday": 0, "frozenHistorical": 0}]
    snapshot = order_planner.AccountSnapshot.of(long1, {"orders": [], "ordersInitialized": True, "isCached": False,
                                                        "dataStatus": "normal"})
    for session_account, bound in (("acct-B", True), ("acct-A", False)):
        proposals = _CorpusProposals(long1)
        plans: list[dict[str, Any]] = []
        proposals.store.get_session = lambda session_id, account=session_account: {"tradingAccountId": account}
        proposals.store.create_target_plan = lambda **kwargs: (plans.append(kwargs) or {"id": "plan-1"}, None)
        ctx = replace(_corpus_context(proposals=proposals), resolved_account_id="acct-A")
        outcome = order_planner.plan(OrderIntent(act="reverse", symbol="SHFE.rb2701", dir="long"), snapshot,
                                     order_planner.Prefs(), ctx)
        assert isinstance(outcome, Cards), outcome
        assert plans[0]["trading_account_id"] == "acct-A"
        assert bool(plans[0]["leg_json"].get("accountExplicit")) is bound, session_account

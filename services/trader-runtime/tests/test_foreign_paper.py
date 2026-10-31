"""外盘本站模拟（外盘二期，2026-10-19）：美元子账户的开户与补开、外盘单落到美元子账户。

撮合引擎沿用 test_paper_accounts 的假引擎（同一个替身同时充当外盘引擎，账户按编号分开记账）；
外盘可下单目录用固定的两个黄金月份顶替 MT5。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from test_paper_accounts import (
    FakePaperClient,
    close_paper_app,
    open_session,
    paper_app,
    register_user,
    wait_paper_positions,
    wait_paper_ready,
)
from trader_runtime.instruments import foreign_named, foreign_product_of, foreign_spoken
from trader_runtime.order_planner import product_of
from trader_runtime.trader_tools import account_card_tools

FOREIGN_URL = "http://paper-foreign.test"
MAIN = "CME.GCEZ26"
CATALOG = {"catalogStatus": "normal", "dataStatus": "normal", "items": [
    {"symbol": MAIN, "exchangeId": "CME", "instrumentId": "GCEZ26", "instrumentName": "COMEX黄金2612",
     "productId": "GCE", "productName": "COMEX黄金", "foreignKey": "COMEX.GC", "currency": "USD",
     "priceTick": 0.1, "volumeMultiple": 100.0, "deliveryYear": 2026, "deliveryMonth": 12,
     "expireRestDays": 70, "isMain": True, "hasNight": True, "chartSymbol": "KQD.m@COMEX.GC"},
    {"symbol": "CME.GCEG27", "exchangeId": "CME", "instrumentId": "GCEG27", "instrumentName": "COMEX黄金2702",
     "productId": "GCE", "productName": "COMEX黄金", "foreignKey": "COMEX.GC", "currency": "USD",
     "priceTick": 0.1, "volumeMultiple": 100.0, "deliveryYear": 2027, "deliveryMonth": 2,
     "expireRestDays": 128, "isMain": False, "hasNight": True, "chartSymbol": "KQD.m@COMEX.GC"},
]}


def foreign_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: str):
    monkeypatch.setenv("AI_TRADER_PAPER_FOREIGN_ENGINE_URL", FOREIGN_URL)
    application, engine = paper_app(tmp_path, monkeypatch, database)
    manager = application.state.paper_accounts
    client = FakePaperClient(engine)
    client.base_url = FOREIGN_URL  # type: ignore[misc]
    manager.foreign_client = client
    assert manager.foreign_poller is not None
    manager.foreign_poller._http.sync = httpx2.Client(base_url=FOREIGN_URL,
                                                      transport=httpx2.MockTransport(engine.handler))
    monkeypatch.setattr(manager.services.foreign_market, "trade_catalog", lambda: CATALOG)
    # The month contract's own MT5 quote and K-lines (外盘三期): the fake data's series stands in for the bridge.
    from trader_runtime.data_providers import fake_market_workbench
    monkeypatch.setattr(manager.services.foreign_market, "workbench",
                        lambda symbol, duration: fake_market_workbench("KQD.m@COMEX.GC", duration))
    return application, engine


def test_foreign_words_pick_the_tradable_product():
    assert foreign_named("买一手美黄金").key == "COMEX.GC"
    assert foreign_named("买一手美豆粕").key == "CBOT.ZM"  # not 美豆
    assert foreign_named("卖两手GCEZ26").key == "COMEX.GC"
    assert foreign_named("买一手黄金") is None  # 黄金 stays 沪金
    assert foreign_named("买一手伦铜") is None  # no MT5 month: watch only
    assert foreign_named("买一手美猪") is None  # no 快期主连 chart: not tradable
    assert foreign_named("美黄金和美油各一手") is None  # two products: not one order
    assert foreign_spoken("买一手美黄金", foreign_named("美黄金")) == "买一手COMEX黄金"
    assert foreign_product_of("KQD.m@COMEX.GC").key == "COMEX.GC"
    assert foreign_product_of("CME.HEZ26").key == "CME.HE"
    assert foreign_product_of("SHFE.au2612") is None
    # 外盘月份合约的品种是 MT5 根代码，各月份一样（换月后“平掉美黄金”仍找得到旧月份的仓）
    assert product_of("CME.GCEZ26") == product_of("CME.GCEG27") == "gce"
    assert product_of("SHFE.rb2701") == "rb"


async def test_registration_opens_a_usd_sub_account_and_old_users_are_backfilled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, engine = foreign_app(tmp_path, monkeypatch, "foreign-open.sqlite3")
    try:
        store = application.state.store
        manager = application.state.paper_accounts
        friend, friend_id = await register_user(application)
        try:
            paper_id = str(store.find_user_paper_account(user_id=friend_id)["id"])
            usd_id = paper_id.replace("paper:", "paperusd:")
            assert (usd_id, 100_000.0) in engine.creates
            record = store.get_trading_account(usd_id)
            assert record["display_name"] == "本站模拟 · 美元" and record["provider_kind"] == "paper"
            directory = (await friend.get("/api/accounts")).json()["accounts"]
            currencies = {item["id"]: item["currency"] for item in directory}
            assert currencies[paper_id] == "CNY" and currencies[usd_id] == "USD"
            # 默认账户不动：仍是人民币本站模拟账户
            assert store.get_user_default_account(user_id=friend_id) == paper_id
        finally:
            await friend.aclose()
        # 老用户（外盘引擎上线前开的户）：启动补开
        store.connection.execute("DELETE FROM account_grants WHERE account_id = ?", (usd_id,))
        store.connection.execute("DELETE FROM trading_accounts WHERE id = ?", (usd_id,))
        store.connection.commit()
        assert manager.foreign_account_of(paper_id) is None
        assert await manager.backfill_foreign_accounts() == 1
        assert manager.foreign_account_of(paper_id) == usd_id
        assert await manager.backfill_foreign_accounts() == 0
    finally:
        await close_paper_app(application)


async def test_a_ticket_on_the_usd_sub_account_trades_on_the_foreign_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, engine = foreign_app(tmp_path, monkeypatch, "foreign-ticket.sqlite3")
    try:
        store = application.state.store
        friend, friend_id = await register_user(application)
        try:
            usd_id = str(store.find_user_paper_account(user_id=friend_id)["id"]).replace("paper:", "paperusd:")
            await friend.get("/api/account/summary", headers={"X-AI-Trader-Account": usd_id})
            await wait_paper_ready(application, usd_id)
            session_id = await open_session(friend)
            response = await friend.post("/api/simulation/proposals", headers={"X-AI-Trader-Account": usd_id}, json={
                "sessionId": session_id, "symbol": MAIN, "side": "buy_open", "volume": 1,
                "priceIntent": "aggressive", "reason": "外盘下单票"})
            assert response.status_code == 201, response.text[:300]
            card = response.json()
            assert card["accountOverride"]["accountId"] == usd_id
            confirmed = await friend.post(f"/api/proposals/{card['proposalId']}/confirm", json={
                "approvalId": card["approvalId"], "proposalVersion": card["version"], "idempotencyKey": "usd-ticket-1"})
            assert confirmed.status_code == 200, confirmed.text[:300]
            order = confirmed.json()["simulationOrder"]
            assert order["accountId"] == usd_id
            assert order["clientOrderId"] in engine.book(usd_id)["orders"]
            headers = {"X-AI-Trader-Account": usd_id}
            listed = await friend.get("/api/simulation/orders?recoveryWait=false", headers=headers)
            assert listed.status_code == 200, listed.text[:300]
            assert next(item for item in listed.json()["items"]
                        if item["clientOrderId"] == order["clientOrderId"])["accountId"] == usd_id
            queried = await friend.post(f"/api/simulation/orders/{order['clientOrderId']}/query", headers=headers)
            assert queried.status_code == 200, queried.text[:300]
            assert queried.json()["accountId"] == usd_id
            # 国内费率表是人民币口径，外盘单不带（引擎按每手美元手续费扣）
            assert "feeRate" not in engine.submits[-1]
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


async def test_a_ticket_on_a_foreign_chart_in_a_cny_conversation_cards_the_usd_main_month(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外盘图表的下单票送快期外盘主连代码，对话在人民币账户：卡出在美元子账户的主力月份上，估算也读那里。"""
    application, engine = foreign_app(tmp_path, monkeypatch, "foreign-chart-ticket.sqlite3")
    try:
        store = application.state.store
        friend, friend_id = await register_user(application)
        try:
            paper_id = str(store.find_user_paper_account(user_id=friend_id)["id"])
            usd_id = paper_id.replace("paper:", "paperusd:")
            await friend.get("/api/account/summary")
            await wait_paper_ready(application, paper_id)
            session_id = await open_session(friend)
            # 美元子账户先装配好、读到第一份账户快照（估算要读可用资金；刚按需装配的那一刻还没读到，网页下一次刷新再取）。
            await friend.get("/api/account/summary", headers={"X-AI-Trader-Account": usd_id})
            await wait_paper_ready(application, usd_id)
            # 网页每个请求都带当前账户头（这里是人民币账户）：外盘单照样转到美元子账户。
            header = {"X-AI-Trader-Account": paper_id}
            estimate = await friend.get("/api/simulation/order-estimate", headers=header, params={
                "symbol": "KQD.m@COMEX.GC", "side": "buy", "offset": "open", "volume": 1})
            assert estimate.status_code == 200, estimate.text[:300]
            assert estimate.json()["units"]["margin"].startswith("美元")
            response = await friend.post("/api/simulation/proposals", headers=header, json={
                "sessionId": session_id, "symbol": "KQD.m@COMEX.GC", "side": "buy_open", "volume": 1,
                "priceIntent": "aggressive", "reason": "外盘图表下单票"})
            assert response.status_code == 201, response.text[:300]
            card = response.json()
            assert card["symbol"] == MAIN and card["currency"] == "USD"
            assert card["name"] == "COMEX黄金2612"  # the catalog's name, not the MT5 description
            assert str(store.get(card["proposalId"])["tradingAccountId"]) == usd_id
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


async def _card_tools(application, user_id: str, account_id: str, session_id: str):
    """The CNY account's card tools with the session's own status — what the AI turn on it plans with."""
    services = application.state.accounts.get(account_id)
    assert services is not None
    ctx = application.state.ctx
    return account_card_tools(application, user_id, services, lambda mode: ctx.status_for_session(session_id))


@pytest.mark.parametrize(("said", "arguments", "chart"), [
    ("买一手美黄金", {"act": "open", "dir": "long", "vol": 1, "symbol": "美黄金", "reason": "用户要买"}, None),
    ("买一手", {"act": "open", "dir": "long", "vol": 1, "reason": "用户要买"}, "KQD.m@COMEX.GC"),
])
async def test_a_foreign_order_said_on_the_cny_account_is_carded_on_the_usd_sub_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, said: str, arguments: dict[str, Any], chart: str | None,
) -> None:
    application, engine = foreign_app(tmp_path, monkeypatch, "foreign-redirect.sqlite3")
    try:
        store = application.state.store
        friend, friend_id = await register_user(application)
        try:
            paper_id = str(store.find_user_paper_account(user_id=friend_id)["id"])
            usd_id = paper_id.replace("paper:", "paperusd:")
            await friend.get("/api/account/summary")
            await wait_paper_ready(application, paper_id)
            session_id = await open_session(friend)
            assert str(store.get_session(session_id)["tradingAccountId"]) == paper_id
            tools = await _card_tools(application, friend_id, paper_id, session_id)
            response, event = await tools.call_order_card(
                tool="propose_order", arguments=arguments, turn_id="turn-foreign", mode="local",
                session_id=session_id, user_id=friend_id, chart=chart, said=said)
            assert event is not None, response
            card = event[1]
            assert card["symbol"] == MAIN
            assert str(store.get(card["proposalId"])["tradingAccountId"]) == usd_id
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


async def test_without_the_foreign_engine_nothing_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    application, engine = paper_app(tmp_path, monkeypatch, "foreign-off.sqlite3")
    try:
        store = application.state.store
        friend, friend_id = await register_user(application)
        try:
            paper_id = str(store.find_user_paper_account(user_id=friend_id)["id"])
            assert store.get_trading_account(paper_id.replace("paper:", "paperusd:")) is None
            # The inviting administrator's login opens their own paper account too (2026-10-31); no USD one.
            created = [account for account, _ in engine.creates]
            assert paper_id in created and not any(account.startswith("paperusd:") for account in created)
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


def test_strategies_accept_foreign_contracts_and_charge_usd_per_lot():
    """外盘三期：策略认外盘月份合约与品种范围；回测费率按引擎行情的每手美元手续费。"""
    from trader_runtime.instruments import product_scope
    from trader_runtime.strategy_scope import allows_symbol, normalize_scope
    from trader_runtime.trading_costs import backtest_fee_schedule

    assert normalize_scope({"mode": "selected", "symbols": ["CME.GCE", "CME.GCEZ26"]})["symbols"] == ["CME.GCE", "CME.GCEZ26"]
    scoped = {"symbol": "CME.GCEZ26", "definition": {"instrumentScope": {"mode": "selected", "symbols": ["CME.GCE"]}}}
    assert allows_symbol(scoped, "CME.GCEG27") and not allows_symbol(scoped, "CME.SIEZ26")
    assert product_scope("CME.GCEZ26") == "CME.GCE" and product_scope("SHFE.rb2701") == "SHFE.rb"

    class Quoting:
        def market_snapshot(self, turn_id, symbol):
            return {"symbol": symbol, "commissionPerLot": 2.5}
    schedule = backtest_fee_schedule(Quoting(), {"source": "custom", "default": {"mode": "per_lot", "open": 9}, "overrides": []},
                                     MAIN)
    assert schedule["source"] == "foreign" and schedule["default"]["open"] == 2.5  # never the CNY custom rate
    from trader_runtime.strategy_backtest import fee_estimate
    assert fee_estimate(schedule)["source"] == "foreign" and not fee_estimate(schedule)["warning"]

    class Unquoted:
        def market_snapshot(self, turn_id, symbol):
            return {"symbol": symbol}
    missing = backtest_fee_schedule(Unquoted(), {"source": "custom", "default": {"mode": "per_lot", "open": 9},
                                                 "overrides": [{"symbol": "x"}]}, MAIN)
    assert missing["default"]["open"] == 0 and missing["overrides"] == []  # no CNY rate charged on a USD contract
    assert fee_estimate(missing)["warning"] and "外盘" in fee_estimate(missing)["text"]


async def test_a_foreign_chart_symbol_resolves_to_the_usd_main_month_for_strategies():
    from trader_runtime.strategy_workbench import concrete_symbol

    class Catalog:
        def instrument_catalog(self):
            return CATALOG
    assert await concrete_symbol(Catalog(), "KQD.m@COMEX.GC") == MAIN
    assert await concrete_symbol(Catalog(), "CME.GCEG27") == "CME.GCEG27"
    assert await concrete_symbol(Catalog(), "SHFE.rb2701") == "SHFE.rb2701"


async def test_the_usd_account_never_blocks_the_event_loop_on_an_mt5_read():
    """外盘三期（Codex 安全审查）：策略监控在事件循环里读美元子账户的行情时不等 MT5 桥——第一次按“K 线正在载入”
    等下一轮、后台去读，之后给读到的那份；工作线程里（回测、校验）照常直接读。"""
    import asyncio
    import threading

    from trader_runtime.paper_accounts import ForeignPaperMarket
    gate = threading.Event()
    calls: list[str] = []

    class Foreign:
        def workbench(self, symbol, duration):
            calls.append(threading.current_thread().name)
            gate.wait(2)
            return {"symbol": symbol, "ageSeconds": 0.5, "quote": {"lastPrice": 4200.0, "ageSeconds": 0.5}, "bars": []}

    market = ForeignPaperMarket(Foreign(), lambda: None)
    try:
        market.market_workbench(MAIN, 300)
        raise AssertionError("an event-loop read must not wait for the bridge")
    except RuntimeError as error:
        assert str(error) == "market_series_not_ready"
    gate.set()
    for _ in range(100):
        if market._reads:
            break
        await asyncio.sleep(0.01)
    packet = market.market_workbench(MAIN, 300)
    assert packet["quote"]["lastPrice"] == 4200.0 and packet["ageSeconds"] >= 0.5
    assert calls and all(name == "foreign-mt5-read" for name in calls)
    direct = await asyncio.to_thread(market.market_workbench, MAIN, 300)   # a worker thread reads directly
    assert direct["quote"]["lastPrice"] == 4200.0


def test_a_spread_never_mixes_a_foreign_and_a_domestic_leg():
    """外盘只在美元子账户成交：一腿外盘一腿国内的价差在保存时就拒绝，不会把国内腿发到外盘引擎。"""
    from trader_runtime.strategy_rules import SpreadSpec
    with pytest.raises(ValueError, match="同是国内合约或同是外盘合约"):
        SpreadSpec(legs=[{"symbol": MAIN}, {"symbol": "SHFE.au2612"}])
    assert SpreadSpec(legs=[{"symbol": MAIN}, {"symbol": "CME.SIEZ26"}])


def test_a_foreign_spread_charges_each_leg_its_own_usd_fee():
    """两腿都是外盘的价差：每条腿按自己的每手美元手续费回测；任一腿取不到就整体不扣并点名。"""
    from trader_runtime.trading_costs import foreign_backtest_schedule, rate_for

    class Quoting:
        def market_snapshot(self, turn_id, symbol):
            return {"symbol": symbol, **({"commissionPerLot": 2.5} if symbol == MAIN else
                                        {"commissionPerLot": 3.1} if symbol == "CME.SIEZ26" else {})}
    custom = {"source": "custom", "default": {"mode": "per_lot", "open": 9}, "overrides": []}
    schedule = foreign_backtest_schedule(Quoting(), custom, [MAIN, "CME.SIEZ26"])
    assert rate_for(schedule, MAIN)["open"] == 2.5 and rate_for(schedule, "CME.SIEZ26")["open"] == 3.1
    missing = foreign_backtest_schedule(Quoting(), custom, [MAIN, "CME.CLEZ26"])
    assert missing["missingReference"] == ["CME.CLEZ26"] and rate_for(missing, MAIN)["open"] == 0


async def test_a_bound_month_without_mt5_data_waits_instead_of_reading_the_main_series():
    """判定和撮合必须是同一份价格：这个月份此刻没有 MT5 数据时等它回来，不拿快期主连顶替。"""
    import asyncio

    from trader_runtime.paper_accounts import ForeignPaperMarket

    class Foreign:
        def workbench(self, symbol, duration):
            return None

    class Default:
        def market_workbench(self, symbol, duration):
            raise AssertionError("never the KQD main series for a CME month")
    market = ForeignPaperMarket(Foreign(), Default)
    with pytest.raises(RuntimeError, match="market_series_not_ready"):
        await asyncio.to_thread(market.market_workbench, MAIN, 300)


async def test_the_ai_starting_a_foreign_strategy_assembles_an_idle_usd_account_first():
    """美元子账户闲置被卸下后，AI 启动挂在它上面的策略：先经 foreign_account 按需装配（与网页请求同一入口），
    不回“外盘账户不可用”。"""
    from trader_runtime.trader_tools import TraderReadOnlyTools
    assembled: list[str] = []

    class Registry:
        def get(self, account_id):
            return None

        async def foreign_account(self, user_id):
            assembled.append(user_id)
            return "paperusd:abc"

    class Store:
        def get_strategy_draft(self, strategy_id, *, user_id):
            return {"tradingAccountId": "paperusd:abc" if strategy_id == "usd" else "paper:abc"}

    class Proposals:
        accounts = Registry()
        store = Store()

    tools = TraderReadOnlyTools.__new__(TraderReadOnlyTools)
    tools.proposals = Proposals()
    await tools._assemble_strategy_account({"strategyId": "cny"}, "u1")
    assert assembled == []
    await tools._assemble_strategy_account({"strategyId": "usd"}, "u1")
    assert assembled == ["u1"]


async def test_a_foreign_daily_strategy_reads_the_months_own_mt5_daily_bars():
    """外盘尾项 (2026-10-21)：日线策略也读这个月份自己的 MT5 行情（小时线按 CME 交易日合成的日线，报价与撮合同一份），
    不再读快期外盘主连（它的日线按国内日历切）；MT5 还没有这份 K 线时等它，不拿快期顶替。"""
    import asyncio

    from trader_runtime.paper_accounts import ForeignPaperMarket

    answers: list[dict | None] = [{"symbol": MAIN, "dataStatus": "normal", "ageSeconds": 1.2,
                                   "quote": {"lastPrice": 4162.5}, "bars": [{"close": 4162.5, "tradingDay": "2026-10-21"}]}]

    class Foreign:
        def workbench(self, symbol, duration):
            assert symbol == MAIN and duration == 86400
            return answers[0]

    class Default:
        def market_workbench(self, symbol, duration):
            raise AssertionError("快期外盘主连 never serves a foreign month's daily strategy")
    market = ForeignPaperMarket(Foreign(), Default)
    daily = await asyncio.to_thread(market.market_workbench, MAIN, 86400)
    assert daily["bars"] == [{"close": 4162.5, "tradingDay": "2026-10-21"}] and daily["quote"]["lastPrice"] == 4162.5
    answers[0] = None
    with pytest.raises(RuntimeError, match="market_series_not_ready"):
        await asyncio.to_thread(ForeignPaperMarket(Foreign(), Default).market_workbench, MAIN, 86400)


async def test_a_quote_only_foreign_chart_says_so_instead_of_blaming_mt5():
    """伦铜、美棉只接了行情（MT5 里没有它的月份合约）：从它的图表建策略如实说，不说成“MT5 行情不通”。"""
    from fastapi import HTTPException

    from trader_runtime.strategy_workbench import concrete_symbol
    with pytest.raises(HTTPException) as caught:
        await concrete_symbol(object(), "KQD.m@NYBOT.CT")
    assert caught.value.status_code == 422 and "只接了行情" in str(caught.value.detail)
    assert "MT5" not in str(caught.value.detail)


def test_a_foreign_symbol_reads_the_news_of_its_domestic_counterpart():
    """切到外盘品种快讯不再是空的：外盘代码读作它对照的国内品种（快讯、事件、日历都按国内品种标注）；没有国内
    对照的（美国天然气）用自己的快讯键（2026-10-21）。"""
    from trader_runtime.research_live import product_key
    assert product_key("KQD.m@COMEX.GC") == product_key("CME.GCEZ26") == "au"
    assert product_key("KQD.m@NYMEX.CL") == "sc" and product_key("KQD.m@LMEOTC.CA") == "cu"
    assert product_key("KQD.m@NYBOT.CT") == "cf"
    assert product_key("KQD.m@NYMEX.NG") == "natgas" and product_key("KQD.m@COMEX.HG") == "cu"
    assert product_key("SHFE.au2612") == "au" and product_key("KQ.m@SHFE.rb") == "rb"


def test_the_mt5_cme_products_added_on_10_20_are_named_without_taking_domestic_words():
    """10-20 接入 MT5 上其余 CME 美元品种：标普、纳指、道指、罗素（迷你与微型）、美铂金、微型金银、燃油汽油、迷你原油
    与天然气、美麦、外汇与美债期货都能说出、能在美元子账户交易；“铂金”“黄金”“原油”仍指国内。"""
    from trader_runtime.instruments import FOREIGN_PRODUCTS, foreign_tradable
    said = {"买一手标普": "CME.ES", "买一手微标普": "CME.MES", "卖两手纳指": "CME.NQ", "道指来一手": "CBOT.YM",
            "买一手美铂金": "NYMEX.PL", "买一手微黄金": "COMEX.MGC", "买一手小原油": "NYMEX.QM", "买一手美燃油": "NYMEX.HO",
            "买一手欧元期货": "CME.6E", "十年美债来一手": "CBOT.ZN", "买一手美麦": "CBOT.ZW"}
    for text, key in said.items():
        product = foreign_named(text)
        assert product is not None and product.key == key and foreign_tradable(product), text
    for text in ("买一手铂金", "买一手黄金", "买一手原油", "买一手白银"):
        assert foreign_named(text) is None, text
    assert foreign_product_of("CME.EPZ26").key == "CME.ES" and foreign_product_of("CME.MCLEZ26").key == "NYMEX.MCL"
    roots = [item.mt5 for item in FOREIGN_PRODUCTS if item.mt5]
    assert len(roots) == len(set(roots))


async def test_an_emergency_stop_on_the_cny_account_also_stops_its_usd_sub_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-10-21：从人民币对话里启动的外盘策略跑在美元子账户上；急停只停当前账户时它照跑。现在人民币本站模拟
    账户急停连同美元子账户一并停（停自动执行、暂停监控中的策略），用户在人民币账户上明确恢复时一并解除停止。"""
    application, _engine = foreign_app(tmp_path, monkeypatch, "foreign-estop.sqlite3")
    try:
        store = application.state.store
        friend, friend_id = await register_user(application)
        try:
            paper_id = str(store.find_user_paper_account(user_id=friend_id)["id"])
            usd_id = paper_id.replace("paper:", "paperusd:")
            await friend.get("/api/account/summary", headers={"X-AI-Trader-Account": usd_id})
            await wait_paper_ready(application, usd_id)
            usd = application.state.accounts.get(usd_id)
            paused: list[tuple[str | None, str]] = []
            proposals = application.state.proposals
            original = proposals.pause_monitoring_strategies
            monkeypatch.setattr(proposals, "pause_monitoring_strategies", lambda *, user_id, trading_account_id: (
                paused.append((user_id, trading_account_id)), original(user_id=user_id, trading_account_id=trading_account_id))[1])
            stopped = await friend.post("/api/ai-execution/stop", headers={"X-AI-Trader-Account": paper_id},
                                        json={"emergency": True, "scope": "all"})
            assert stopped.status_code == 200, stopped.text[:300]
            assert stopped.json()["companion"]["accountId"] == usd_id
            assert usd.autonomy.controls(user_id=friend_id)["stopStatus"] == "EMERGENCY_STOP"
            assert (None, paper_id) in paused and (None, usd_id) in paused
            # 明确恢复（POST /api/ai-execution/start 成功后调用同一函数；测试用户的套餐没有自主执行，直接调）。
            from trader_runtime.execution_stop import lift_companion_stop
            await lift_companion_stop(accounts=application.state.accounts, user_id=friend_id, account_id=paper_id)
            assert not usd.autonomy.controls(user_id=friend_id)["stopStatus"]
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


async def test_the_cny_account_names_its_usd_half_and_the_ai_reads_its_foreign_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-10-21 所有者：迷你纳指下单成功了，持仓里却没有——外盘单在美元子账户成交，人民币账户的持仓面板和 AI
    都只看自己。现在人民币本站模拟账户的运行状态写明它的美元子账户（客户端据此合并显示），AI 读持仓时带上那一半。"""
    import json

    application, _engine = foreign_app(tmp_path, monkeypatch, "foreign-half.sqlite3")
    try:
        store = application.state.store
        friend, friend_id = await register_user(application)
        try:
            paper_id = str(store.find_user_paper_account(user_id=friend_id)["id"])
            usd_id = paper_id.replace("paper:", "paperusd:")
            await friend.get("/api/account/summary")
            await wait_paper_ready(application, paper_id)
            await friend.get("/api/account/summary", headers={"X-AI-Trader-Account": usd_id})
            await wait_paper_ready(application, usd_id)
            status = (await friend.get("/api/runtime/status", headers={"X-AI-Trader-Account": paper_id})).json()
            assert status["account"]["id"] == paper_id and status["account"]["foreignAccountId"] == usd_id
            usd_status = (await friend.get("/api/runtime/status", headers={"X-AI-Trader-Account": usd_id})).json()
            assert "foreignAccountId" not in usd_status["account"]

            session_id = await open_session(friend)
            # 网页读的是会话推送的 runtime.status（不是 GET /api/runtime/status）：同样写明美元子账户。
            session_status = application.state.ctx.status_for_session(session_id)
            assert session_status["account"]["id"] == paper_id
            assert session_status["account"]["foreignAccountId"] == usd_id
            # 引擎持仓的真实形状（生产 /api/positions 读回的一行）；假引擎只记委托，不出这些字段。
            held = [{"symbol": MAIN, "exchangeId": "CME", "instrumentId": "GCEZ26", "direction": "long", "volume": 1,
                     "todayVolume": 1, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0,
                     "openPrice": 4143.7, "lastPrice": 4132.8, "margin": 580.0, "unrealizedPnl": -109.0}]
            usd = application.state.accounts.get(usd_id)
            _engine.book(usd_id)["positions"] = held
            usd.provider._poll_once(usd.provider._fetch)
            listed = (await friend.get("/api/positions", headers={"X-AI-Trader-Account": usd_id})).json()["items"]
            assert listed[0]["instrumentName"] == "COMEX黄金2612"
            tools = await _card_tools(application, friend_id, paper_id, session_id)
            tools.account_id = paper_id
            response, _ = await tools._call_async(tool="get_positions", arguments={}, turn_id="turn-half", mode="local",
                                                 session_id=session_id, user_id=friend_id)
            data = json.loads(response["contentItems"][0]["text"])["data"]
            assert data is not None, response
            half = data["foreignAccount"]
            assert half["accountId"] == usd_id and half["currency"] == "USD" and half["status"] == "ok", half
            assert [item["symbol"] for item in half["positions"]] == [MAIN]
            assert all(item["symbol"] != MAIN for item in data["positions"])
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


def test_foreign_products_without_a_domestic_counterpart_screen_their_own_news():
    """2026-10-21 所有者：打开迷你纳指，快讯的内容跟外盘无关——没有国内对照的外盘品种（美股指数、外汇、美债、
    天然气）以前不按品种筛，显示全部快讯。现在每个外盘品种都有快讯键：讲纳指的快讯标给纳指、筛给纳指，
    日历只列美国的利率与经济数据；键不和国内品种撞（10 年美债不是锌）。"""
    from trader_runtime.instruments import FOREIGN_PRODUCTS
    from trader_runtime.research_live import (
        CALENDAR_CATEGORY,
        FLASH_ALIASES,
        calendar_related,
        product_key,
        tag_flash_item,
    )
    for item in FOREIGN_PRODUCTS:
        key = item.news_product
        assert key, item.key
        assert key in FLASH_ALIASES, (item.key, key)
    assert product_key("KQD.m@CME.NQ") == product_key("CME.ENQZ26") == product_key("KQD.m@CME.MNQ") == "nasdaq"
    assert product_key("KQD.m@CBOT.ZN") == "ust" and product_key("SHFE.zn2612") == "zn"
    assert product_key("KQD.m@CBOT.ZW") == "wh"
    aliases = sorted(FLASH_ALIASES.items())
    item = tag_flash_item({"title": "纳斯达克100指数期货盘前涨1%，美国科技股走强", "text": "", "source": "财联社"},
                          [(key, tuple(words)) for key, words in aliases])
    assert "nasdaq" in item["products"] and "zn" not in item["products"]
    payrolls = {"region": "美国", "event": "美国10月非农就业人数变化(万人)"}
    assert CALENDAR_CATEGORY["nasdaq"] == "us_macro" and calendar_related(payrolls, "nasdaq")
    assert calendar_related({"region": "欧元区", "event": "欧洲央行利率决议"}, "eur")
    assert not calendar_related({"region": "中国", "event": "中国9月社会融资规模"}, "nasdaq")


async def test_the_cny_account_activity_on_a_foreign_chart_carries_the_usd_sub_accounts_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外盘盘点 D (2026-10-21)：外盘图表上的操作记录（GET /api/app/activity，人民币本站模拟账户）原来不含美元子账户的
    委托与成交——外盘单都在子账户里。现在同一品种的那一半并进来、每条写明子账户与美元；国内图表不带。"""
    application, _engine = foreign_app(tmp_path, monkeypatch, "foreign-activity.sqlite3")
    try:
        store = application.state.store
        friend, friend_id = await register_user(application)
        try:
            paper_id = str(store.find_user_paper_account(user_id=friend_id)["id"])
            usd_id = paper_id.replace("paper:", "paperusd:")
            await friend.get("/api/account/summary")
            await wait_paper_ready(application, paper_id)
            await friend.get("/api/account/summary", headers={"X-AI-Trader-Account": usd_id})
            await wait_paper_ready(application, usd_id)
            session_id = await open_session(friend)
            card = (await friend.post("/api/simulation/proposals", headers={"X-AI-Trader-Account": usd_id}, json={
                "sessionId": session_id, "symbol": MAIN, "side": "buy_open", "volume": 1,
                "priceIntent": "aggressive", "reason": "外盘开仓"})).json()
            with store.connection:  # an AI-initiated order: the activity lists the automated ones
                store.connection.execute(
                    "UPDATE proposals SET payload_json = json_set(payload_json, '$.automationSource', 'ai_autonomous')"
                    " WHERE id = ?", (card["proposalId"],))
            confirmed = await friend.post(f"/api/proposals/{card['proposalId']}/confirm", json={
                "approvalId": card["approvalId"], "proposalVersion": card["version"], "idempotencyKey": "usd-activity-1"})
            assert confirmed.status_code == 200, confirmed.text[:300]
            client_order_id = confirmed.json()["simulationOrder"]["clientOrderId"]

            async def read(symbol: str) -> dict[str, Any]:
                response = await friend.get("/api/app/activity", params={"symbol": symbol},
                                            headers={"X-AI-Trader-Account": paper_id})
                assert response.status_code == 200, response.text[:300]
                return response.json()

            chart = await read("KQD.m@COMEX.GC")
            assert chart["foreignAccount"] == {"accountId": usd_id, "currency": "USD", "status": "ok"}
            order = next(item for item in chart["orders"] if item["clientOrderId"] == client_order_id)
            assert (order["accountId"], order["currency"], order["request"]["symbol"]) == (usd_id, "USD", MAIN)
            assert any(item["clientOrderId"] == client_order_id for item in (await read(MAIN))["orders"])
            # another foreign product's chart and a domestic chart do not carry it
            assert not (await read("KQD.m@NYMEX.CL"))["orders"]
            domestic = await read("SHFE.rb2701")
            assert not domestic["orders"] and domestic.get("foreignAccount") is None
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


async def test_the_cny_review_on_a_foreign_chart_carries_the_usd_sub_accounts_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2 (2026-10-21)：操作记录卡给成交配盈亏读的是 GET /api/trade-review（人民币本站模拟账户）——外盘回合都在美元
    子账户里，外盘图表上的成交一直没有盈亏。现在同一品种的回合并进来（外盘引擎每手美元手续费、每条写明子账户与
    美元），别的外盘品种、国内图表与整账户复盘都不带。"""
    application, _engine = foreign_app(tmp_path, monkeypatch, "foreign-review.sqlite3")
    try:
        store = application.state.store
        friend, friend_id = await register_user(application)
        try:
            paper_id = str(store.find_user_paper_account(user_id=friend_id)["id"])
            usd_id = paper_id.replace("paper:", "paperusd:")
            await friend.get("/api/account/summary")
            await wait_paper_ready(application, paper_id)
            await friend.get("/api/account/summary", headers={"X-AI-Trader-Account": usd_id})
            await wait_paper_ready(application, usd_id)
            session_id = await open_session(friend)
            for side, key in (("buy_open", "usd-review-open"), ("sell_close", "usd-review-close")):
                if side == "sell_close":
                    await wait_paper_positions(application, usd_id, count=1)
                candidate = await friend.post("/api/simulation/proposals", headers={"X-AI-Trader-Account": usd_id}, json={
                    "sessionId": session_id, "symbol": MAIN, "side": side, "volume": 1, "priceIntent": "aggressive",
                    "reason": "外盘回合"})
                assert candidate.status_code == 201, candidate.text[:300]
                card = candidate.json()
                confirmed = await friend.post(f"/api/proposals/{card['proposalId']}/confirm", json={
                    "approvalId": card["approvalId"], "proposalVersion": card["version"], "idempotencyKey": key})
                assert confirmed.status_code == 200, confirmed.text[:300]

            async def review(symbol: str | None) -> dict[str, Any]:
                response = await friend.get("/api/trade-review", params={"symbol": symbol} if symbol else {},
                                            headers={"X-AI-Trader-Account": paper_id})
                assert response.status_code == 200, response.text[:300]
                return response.json()

            chart = await review("KQD.m@COMEX.GC")
            assert chart["foreignAccount"] == {"accountId": usd_id, "currency": "USD", "status": "ok"}
            [trip] = chart["trades"]
            assert (trip["symbol"], trip["accountId"], trip["currency"]) == (MAIN, usd_id, "USD")
            assert trip["fees"] > 0 and trip["net"] == pytest.approx(trip["gross"] - trip["fees"])
            assert (chart["currency"], chart["costSource"]) == ("USD", "foreign")
            assert [item["id"] for item in (await review(MAIN))["trades"]] == [trip["id"]]
            # another foreign product, a domestic chart and the whole-account review do not carry it
            assert not (await review("KQD.m@NYMEX.CL"))["trades"]
            domestic = await review("SHFE.rb2701")
            assert not domestic["trades"] and "foreignAccount" not in domestic
            whole = await review(None)
            assert not whole["trades"] and whole["currency"] == "CNY" and "foreignAccount" not in whole
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


async def test_usd_ledger_pnl_counts_against_its_own_100k_not_the_rmb_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本站模拟合成一个账户 (2026-10-21): the 复盘 panel reads the 外盘 USD ledger's 总盈亏 beside the RMB one. Its 本金
    is the 10 万美元 it opened with — the 设置 本金 (RMB, default 100 万) once made it read about -90 万."""
    application, engine = foreign_app(tmp_path, monkeypatch, "foreign-pnl.sqlite3")
    try:
        store = application.state.store
        friend, friend_id = await register_user(application)
        try:
            paper_id = str(store.find_user_paper_account(user_id=friend_id)["id"])
            usd_id = paper_id.replace("paper:", "paperusd:")
            await friend.get("/api/account/summary", headers={"X-AI-Trader-Account": usd_id})
            await wait_paper_ready(application, usd_id)
            usd = await friend.get("/api/account/pnl", headers={"X-AI-Trader-Account": usd_id})
            assert usd.status_code == 200, usd.text[:300]
            assert usd.json()["currency"] == "USD" and usd.json()["initialBalance"] == 100_000
            assert usd.json()["totalPnl"] == pytest.approx(usd.json()["equity"] - 100_000)
            cny = await friend.get("/api/account/pnl", headers={"X-AI-Trader-Account": paper_id})
            assert cny.json()["currency"] == "CNY" and cny.json()["initialBalance"] == 1_000_000
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


def _hold(engine, account_id: str, symbol: str, volume: int = 1, direction: str = "long") -> None:
    engine.book(account_id)["positions"].append({
        "symbol": symbol, "direction": direction, "volume": volume, "todayVolume": volume, "historicalVolume": 0,
        "frozenToday": 0, "frozenHistorical": 0})


async def _two_ledgers(application, engine, friend, friend_id):
    store = application.state.store
    paper_id = str(store.find_user_paper_account(user_id=friend_id)["id"])
    usd_id = paper_id.replace("paper:", "paperusd:")
    await friend.get("/api/account/summary")
    await wait_paper_ready(application, paper_id)
    await friend.get("/api/account/summary", headers={"X-AI-Trader-Account": usd_id})
    await wait_paper_ready(application, usd_id)
    session_id = await open_session(friend)
    return paper_id, usd_id, session_id


async def test_one_account_two_ledgers_the_ai_sees_and_closes_the_usd_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本站模拟两本账（所有者 2026-10-21：“看螺纹时语音说平仓，助手回答没有持仓”）：AI 的账户现状两本账都有；
    什么都没点名的“平仓”人民币账没仓就平美元账的；“全平”两本账各一张卡、互相关联，确认一张两张都发。"""
    application, engine = foreign_app(tmp_path, monkeypatch, "two-ledgers.sqlite3")
    try:
        store = application.state.store
        friend, friend_id = await register_user(application)
        try:
            paper_id, usd_id, session_id = await _two_ledgers(application, engine, friend, friend_id)
            _hold(engine, usd_id, MAIN)
            await wait_paper_positions(application, usd_id, count=1)
            tools = await _card_tools(application, friend_id, paper_id, session_id)

            # 合并读数（2026-10-21 上线后发现：账户摘要自带 status 字段，与美元那一半的 status 撞名，500）。
            class Summary:
                autonomy = None

                def call(self, **_):
                    text = json.dumps({"data": {"status": "normal", "equity": 100_031.0, "currency": "USD"}})
                    return {"success": True, "contentItems": [{"type": "inputText", "text": text}]}, None

            async def summary_half(_user):
                return Summary()
            own = {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(
                {"data": {"status": "normal", "equity": 1_000_000.0}})}]}
            original = tools._foreign_tools
            tools._foreign_tools = summary_half  # type: ignore[method-assign]
            merged = await tools._with_foreign_half("get_account_summary", own, arguments={}, turn_id="t", mode="local",
                                                    session_id=session_id, user_id=friend_id)
            tools._foreign_tools = original  # type: ignore[method-assign]
            half = json.loads(merged["contentItems"][0]["text"])["data"]["foreignAccount"]
            assert half["status"] == "ok" and half["equity"] == 100_031.0
            state = await tools.account_state(friend_id, mode="local")
            assert state["available"] is True
            assert [(row["symbol"], row.get("ledger")) for row in state["positions"]] == [(MAIN, "USD")]
            assert state["foreignAccount"]["status"] == "ok"

            # 看着螺纹说“平仓”：人民币账没有仓，美元账有一手黄金 → 美元账的平仓卡。
            response, event = await tools.call_order_card(
                tool="propose_order", arguments={"act": "close", "vol": 1, "reason": "平一手"}, turn_id="turn-close",
                mode="local", session_id=session_id, user_id=friend_id, chart="SHFE.rb2701", said="平一手")
            assert event is not None, response
            assert str(store.get(event[1]["proposalId"])["tradingAccountId"]) == usd_id

            # 两本账都有仓：全平出两张卡，互相关联；确认人民币那张，美元那张跟着发。
            _hold(engine, paper_id, "SHFE.rb2701")
            await wait_paper_positions(application, paper_id, count=1)
            outcome = await tools.plan_order(
                __import__("trader_runtime.order_intent", fromlist=["OrderIntent"]).OrderIntent(act="close_all", reason="全平"),
                mode="local", session_id=session_id, turn_id="turn-all", user_id=friend_id, origin="ai")
            cards = list(getattr(outcome, "cards", ()))
            assert len(cards) == 2, outcome
            accounts = {str(store.get(card.proposal["proposalId"])["tradingAccountId"]) for card in cards}
            assert accounts == {paper_id, usd_id}
            ids = [card.proposal["proposalId"] for card in cards]
            for card in cards:
                assert card.proposal["linkedProposalIds"] == [other for other in ids if other != card.proposal["proposalId"]]
            own = next(card.proposal for card in cards if str(store.get(card.proposal["proposalId"])["tradingAccountId"]) == paper_id)
            confirmed = await friend.post(f"/api/proposals/{own['proposalId']}/confirm", headers={"X-AI-Trader-Account": paper_id},
                                          json={"approvalId": own["approvalId"], "proposalVersion": own["version"],
                                                "idempotencyKey": "two-ledgers-all"})
            assert confirmed.status_code == 200, confirmed.text[:300]
            assert [item["status"] for item in confirmed.json()["linkedResults"]] == ["ok"]
            assert {store.get(proposal_id)["status"] for proposal_id in ids} == {"candidate_recorded"}
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


async def test_a_usd_working_order_is_cancelled_from_the_rmb_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI 看得到美元账的挂单（账户现状 / 委托读数），也撤得掉：撤单、查单、挂止损、AI 自主下单都落在那张委托
    或那个合约自己的账本上——不是人民币账的委托就交给美元账回答，外盘合约归美元账，国内合约留在人民币账。"""
    application, engine = foreign_app(tmp_path, monkeypatch, "two-ledgers-cancel.sqlite3")
    try:
        friend, friend_id = await register_user(application)
        try:
            paper_id, usd_id, session_id = await _two_ledgers(application, engine, friend, friend_id)
            tools = await _card_tools(application, friend_id, paper_id, session_id)
            autonomy = await tools._ledger_autonomy(friend_id, client_order_id="no-such-order")
            assert autonomy is not tools.autonomy  # not this ledger's order → the USD ledger answers for it
            assert await tools._ledger_autonomy(friend_id, symbol="SHFE.rb2701") is tools.autonomy
            assert await tools._ledger_autonomy(friend_id, symbol=MAIN) is not tools.autonomy
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


def test_answer_cards_list_the_usd_ledger_apart_from_the_rmb_one():
    """持仓 / 挂单卡把工具结果里的美元子账户（foreignAccount）分开列：行上写“外盘 · 美元”，浮动盈亏两个数不相加。"""
    from trader_runtime.answer_cards import orders_card, positions_card
    data = {"dataStatus": "normal", "positions": [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 2,
                                                   "unrealizedPnl": -300.0}],
            "foreignAccount": {"status": "ok", "currency": "USD", "positions": [
                {"symbol": MAIN, "direction": "long", "volume": 1, "unrealizedPnl": 160.0}]}}
    card = positions_card(data, account_word="本站模拟")
    assert card["rows"][1]["secondary"].endswith("外盘 · 美元") and "secondary" not in card["rows"][0]
    figures = {figure["label"]: (figure["value"], figure.get("unit")) for figure in card["figures"]}
    assert figures["浮动盈亏"] == ("-300", "元") and figures["外盘浮动盈亏"] == ("+160", "美元")
    orders = orders_card({"ordersInitialized": True, "orders": [], "protections": [], "trades": [],
                          "foreignAccount": {"status": "ok", "orders": [
                              {"symbol": MAIN, "direction": "BUY", "offset": "OPEN", "volume": 1, "volumeLeft": 1,
                               "limitPrice": 4100.0, "status": "ALIVE"}]}})
    assert len(orders["rows"]) == 1 and "外盘 · 美元" in orders["rows"][0]["secondary"]
    # 子账户读不到时只列本账户的（不编造美元那一半）
    assert len(positions_card({**data, "foreignAccount": {"status": "unavailable"}}, account_word="本站模拟")["rows"]) == 1


async def test_usd_position_protection_is_set_listed_and_cancelled_from_the_rmb_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持仓面板（人民币会话）给外盘仓挂止损：落在美元子账户的保护服务上；保护列表两本账一起列（美元那条带
    accountId）。保护服务本身不变，这里核对请求落到哪本账（撤销按保护记录自己的账户，同一查询见 _ledger_autonomy 的测试）。"""
    application, engine = foreign_app(tmp_path, monkeypatch, "two-ledgers-stops.sqlite3")
    try:
        friend, friend_id = await register_user(application)
        try:
            paper_id, usd_id, session_id = await _two_ledgers(application, engine, friend, friend_id)
            usd = application.state.accounts.get(usd_id).autonomy
            calls: list[tuple[str, Any]] = []
            stop = {"stopId": "usd-stop-1", "symbol": MAIN, "positionDirection": "long", "status": "ARMED"}

            async def create_stop(**kwargs):
                calls.append(("create", kwargs["symbol"]))
                return stop

            monkeypatch.setattr(usd, "create_stop", create_stop)
            monkeypatch.setattr(usd, "list_stops", lambda *, user_id: [stop])
            rmb = {"X-AI-Trader-Account": paper_id}
            created = await friend.post("/api/ai-execution/stops", headers=rmb, json={
                "sessionId": session_id, "symbol": MAIN, "positionDirection": "long", "positionBucket": "all",
                "volume": 1, "triggerPrice": 700.0})
            assert created.status_code == 201 and calls == [("create", MAIN)], created.text[:300]
            listed = (await friend.get("/api/ai-execution", headers=rmb)).json()["stops"]
            assert [(row["stopId"], row.get("accountId")) for row in listed] == [("usd-stop-1", usd_id)]
            # 国内合约的保护仍在人民币账（不经美元那本）
            rmb_create = await friend.post("/api/ai-execution/stops", headers=rmb, json={
                "sessionId": session_id, "symbol": "SHFE.rb2701", "positionDirection": "long", "positionBucket": "all",
                "volume": 1, "triggerPrice": 3000.0})
            assert calls == [("create", MAIN)] and rmb_create.status_code != 201  # no rb position: the RMB ledger refuses
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


async def _assembled(application, account_id: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while application.state.accounts.get(account_id) is None:
        assert loop.time() < deadline, account_id
        await asyncio.sleep(0.02)


async def test_the_two_ledgers_are_looked_after_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """两本账第三批（2026-10-22）：收盘前 / 风险度 / 保本 / 交割提醒、收盘复盘推送和异动提醒只看已装配的账户——
    人民币账一装配，美元账跟着装配；闲置时一起卸载，另一本有事在做（策略、止损、挂单）就都留着。美元账的提醒落在
    人民币账的对话里（用户在那里看到两本账），风险度金额写美元。"""
    from trader_runtime.account_reminders import risk_message
    from trader_runtime.conversation_cards import reminder_target_session

    application, engine = foreign_app(tmp_path, monkeypatch, "two-ledgers-together.sqlite3")
    try:
        store = application.state.store
        manager = application.state.paper_accounts
        friend, friend_id = await register_user(application)
        try:
            paper_id = str(store.find_user_paper_account(user_id=friend_id)["id"])
            usd_id = paper_id.replace("paper:", "paperusd:")
            await friend.get("/api/account/summary")  # only the RMB ledger is asked for
            await _assembled(application, usd_id)
            session_id = await open_session(friend)
            # 一本还有事在做：两本都不卸载
            manager.idle_seconds = 0.0
            monkeypatch.setattr(store, "list_busy_paper_accounts", lambda: [usd_id])
            assert await manager.scan_idle() == []
            monkeypatch.setattr(store, "list_busy_paper_accounts", lambda: [])
            assert sorted(await manager.scan_idle()) == sorted([paper_id, usd_id])
            assert application.state.accounts.get(usd_id) is None
            target = reminder_target_session(manager.services.broker, store, friend_id, account_id=usd_id)
            assert target is not None and target["sessionId"] == session_id
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)
    text = risk_message({"riskRatio": 85, "margin": 85_000, "equity": 100_000, "available": 15_000,
                         "dataStatus": "simulated"}, "本站模拟 · 美元：", usd=True)
    assert text is not None and "权益 10 万美元" in text and "可用 1.5 万美元" in text


async def test_chat_suggestions_see_the_usd_ledger_of_the_rmb_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两本账第三批：输入框建议和持仓面板一样看两本账——美元账的持仓带 currency USD 进模型输入，持仓变了重新生成。"""
    from types import SimpleNamespace

    from trader_runtime.chat_suggestions import SuggestedQuestions

    application, engine = foreign_app(tmp_path, monkeypatch, "two-ledgers-suggest.sqlite3")
    inputs: list[dict[str, Any]] = []

    async def complete_structured(*, input_text: str, **_kwargs: Any) -> Any:
        inputs.append(json.loads(input_text))
        return SimpleNamespace(value=SuggestedQuestions(items=["美黄金挂个止损"]))

    try:
        adapter = application.state.adapter
        monkeypatch.setattr(adapter, "complete_structured", complete_structured, raising=False)
        monkeypatch.setattr(adapter, "agent_authenticated", True, raising=False)
        friend, friend_id = await register_user(application)
        try:
            paper_id, usd_id, session_id = await _two_ledgers(application, engine, friend, friend_id)
            _hold(engine, usd_id, MAIN)
            await wait_paper_positions(application, usd_id, count=1)
            response = await friend.post("/api/agent/suggestions", json={
                "view": "trade", "symbol": "SHFE.rb2701", "trigger": "open", "sessionId": session_id})
            assert response.status_code == 200, response.text[:300]
            assert inputs, response.json()
            assert [(row["symbol"], row.get("currency")) for row in inputs[-1]["positions"]] == [(MAIN, "USD")]
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)

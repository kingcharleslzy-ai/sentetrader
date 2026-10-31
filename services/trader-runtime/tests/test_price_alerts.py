"""到价提醒 (2026-10-02): the creation rules at the HTTP route (a real contract from the catalog,
the tick grid, the direction judged against the live quote, the 20-active cap), the monitor's
one-shot crossing and its three deliveries, survival across a restart, and the three AI tools
going through the same capability-catalog routes the chart button uses."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from httpx import ASGITransport, AsyncClient

from test_simulation_orders import SHANGHAI, USER, Gateway, NormalProvider, application, quote_now
from trader_runtime.data_providers import fake_market_board, fake_market_quotes, fake_market_workbench
from trader_runtime.skill_registry import skill_by_id
from trader_runtime.trader_tools import TOOL_ARGUMENT_MODELS, TraderReadOnlyTools


class AlertProvider(NormalProvider):
    """The shared-market snapshot reads the workbench quote, so the crossing price and the quote
    clock are steered through market_workbench (NormalProvider only overrides market_snapshot,
    which the shared source never reads). A fresh revision per call keeps the fake produce loop
    republishing, so a steered price reaches the next snapshot instead of the first packet
    living forever."""

    def __init__(self) -> None:
        super().__init__()
        self._revision = 0
        self.quotes_reads = 0

    def market_board(self) -> dict[str, Any]:
        # 报价总表 rows follow the same steering: main-contract alerts are watched through it.
        board = fake_market_board()
        rows = [{**row, "last": self.last, "quoteTime": self.quote_datetime}
                if row["symbol"] == "SHFE.au2612" else row for row in board["rows"]]
        return {**board, "rows": rows}

    def market_quotes(self, symbols: Any) -> dict[str, Any]:
        # The extras channel follows the same steering: creation judges direction on it
        # (2026-10-03) and non-main alerts are watched through it.
        self.quotes_reads += 1
        quotes = fake_market_quotes(symbols)
        rows = [{**row, "last": self.last, "quoteTime": self.quote_datetime}
                if row["symbol"] == "SHFE.au2612" else row for row in quotes["rows"]]
        return {**quotes, "rows": rows}

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        self.workbench_reads = getattr(self, "workbench_reads", 0) + 1
        self._revision += 1
        workbench = fake_market_workbench(symbol, duration_seconds)
        quote = {**workbench["quote"], "lastPrice": self.last, "price": self.last,
                 "quoteDatetime": self.quote_datetime, "priceTick": self.price_tick}
        return {**workbench, "quote": quote, "revision": str(self._revision)}


async def settle(app: Any = None) -> None:
    """The fake produce loop republishes every 0.1 s; let one round pass after steering the
    provider so the monitor's snapshot reads the steered price, not the previous packet. With
    the app, also drop the board's and the extras channel's 1 s shared reads so alerts see
    the new price."""
    await asyncio.sleep(0.15)
    if app is not None:
        for key in [item for item in app.state.shared_market.cache
                    if item[0] in ("board", "quotes")]:
            app.state.shared_market.cache.pop(key, None)


def stale_quote(seconds: float = 120) -> str:
    return (datetime.now(SHANGHAI) - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S.%f")


async def login(app) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                         headers={"Origin": "http://127.0.0.1:5173"})
    assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
    return client


async def create(client: AsyncClient, **overrides) -> Any:
    body: dict[str, Any] = {"symbol": "SHFE.au2612", "price": 824.5, "direction": "above"}
    body.update(overrides)
    return await client.post("/api/market/alerts", json=body)


def detail(response: Any) -> str:
    return str(response.json().get("detail") or "")


def tool_payload(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["contentItems"][0]["text"])


async def test_create_list_cancel_and_every_creation_refusal(tmp_path: Path) -> None:
    app = application(tmp_path / "alerts-rules.sqlite3", Gateway(), AlertProvider())
    provider = app.state.data_provider
    provider.quote_datetime = quote_now()
    http = await login(app)
    session = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()

    # The refusals are Chinese sentences the UI shows inline.
    assert "主连" in detail(await create(http, symbol="KQ.m@SHFE.rb", price=3300))
    assert "目录里没有" in detail(await create(http, symbol="SHFE.xx9999", price=1.0))
    assert "最小变动价位" in detail(await create(http, price=824.51))  # au2612 ticks by 0.02
    assert "已在" in detail(await create(http, price=820.0))  # above while the last is 824
    assert "已在" in detail(await create(http, price=830.0, direction="below"))
    equal = await create(http, price=824.0, direction=None)  # it is the live price
    assert equal.status_code == 422 and "现价就是" in detail(equal)
    bad_note = await create(http, note="一" * 61)
    assert bad_note.status_code == 422 and "60" in detail(bad_note)
    unknown = await http.post("/api/market/alerts", json={"symbol": "SHFE.au2612", "price": 824.5, "wat": 1})
    assert unknown.status_code == 422  # extra=forbid
    provider.quote_datetime = stale_quote()
    await settle()  # the shared packet must republish before the route reads the stale clock
    # After the close / on a holiday the quote is stale: still settable, judged on the last price.
    stale = await create(http, price=826.0)
    assert stale.status_code == 201 and stale.json()["alert"]["direction"] == "above", stale.text
    assert (await http.delete(f"/api/market/alerts/{stale.json()['alert']['id']}")).status_code == 200
    provider.quote_datetime = quote_now()
    await settle()

    # Omitting the direction infers it from the live price both ways.
    above = await create(http, price=826.0)
    assert above.status_code == 201
    made_above = above.json()["alert"]
    assert made_above["direction"] == "above" and made_above["name"] == "沪金2612" and made_above["status"] == "active"
    below = await create(http, price=822.0, direction=None, note="回落就走",
                         via="chat", sessionId=session["sessionId"])
    made_below = below.json()["alert"]
    assert made_below["direction"] == "below" and made_below["via"] == "chat" and made_below["sessionId"] == session["sessionId"]

    listed = (await http.get("/api/market/alerts")).json()
    assert [item["id"] for item in listed["active"]] == [made_below["id"], made_above["id"]]  # newest first
    assert listed["triggered"] == []

    # Cancel is only ever one's own active alert: someone else's row is a 404, and so is a repeat.
    stranger = app.state.store.create_price_alert(
        user_id="someone-else", symbol="SHFE.au2612", name="沪金2612", direction="above",
        price=Decimal("826"), note="", via="ui", session_id=None, max_active=20)
    assert (await http.delete(f"/api/market/alerts/{stranger['id']}")).status_code == 404
    cancelled = await http.delete(f"/api/market/alerts/{made_above['id']}")
    assert cancelled.status_code == 200 and cancelled.json()["alert"]["status"] == "cancelled"
    assert (await http.delete(f"/api/market/alerts/{made_above['id']}")).status_code == 404
    assert [item["id"] for item in (await http.get("/api/market/alerts")).json()["active"]] == [made_below["id"]]

    # The cap is a count, nothing more: the 21st active alert is refused until one is removed.
    for index in range(19):  # 1 active + 19 = 20
        assert (await create(http, price=824.5 + 0.04 * (index + 1))).status_code == 201
    capped = await create(http, price=830.0)
    assert capped.status_code == 422 and "上限" in detail(capped)
    await http.delete(f"/api/market/alerts/{made_below['id']}")
    assert (await create(http, price=830.0)).status_code == 201
    await http.aclose()
    app.state.store.close()


async def test_the_monitor_fires_each_alert_once_and_delivers_all_three_ways(tmp_path: Path) -> None:
    app = application(tmp_path / "alerts-monitor.sqlite3", Gateway(), AlertProvider())
    provider = app.state.data_provider
    provider.quote_datetime = quote_now()
    http = await login(app)
    session = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
    session_id = session["sessionId"]
    app.state.broker.subscribers[session_id] = 1  # an attached SSE reader
    app.state.voice_realtime.calls["test-call"] = SimpleNamespace(user_id=USER)
    above = (await create(http, price=824.5, note="到价看看")).json()["alert"]
    below = (await create(http, price=822.0, direction=None)).json()["alert"]  # below the last
    monitor = app.state.price_alerts

    await monitor._tick()  # the last (824) sits between the two targets: nothing fires
    assert len(app.state.store.list_price_alerts(user_id=USER)["active"]) == 2

    provider.last = 825.0
    await settle(app)
    await monitor._tick()
    await monitor._tick()  # the crossing round is never delivered twice
    listed = app.state.store.list_price_alerts(user_id=USER)
    assert [item["id"] for item in listed["active"]] == [below["id"]]
    fired = listed["triggered"][0]
    assert fired["id"] == above["id"] and fired["triggerPrice"] == "825.0" and fired["status"] == "triggered"
    notices = [event.get("payload") for _, event in app.state.store.list_events(session_id)
               if event.get("name") == "message.completed"
               and str(event.get("payload", {}).get("turnId", "")).startswith("pricealert-")]
    assert len(notices) == 1 and str(notices[0]["turnId"]) == f"pricealert-{above['id']}"
    assert "到价提醒：沪金2612 涨到 824.5（现 825）" in str(notices[0]["text"])
    assert "备注：到价看看。" in str(notices[0]["text"])
    cards = reminder_cards(app, session_id, f"pricealert-{above['id']}")
    assert len(cards) == 1 and cards[0]["kind"] == "price_alert" and cards[0]["title"] == "到价提醒"
    assert [(f["label"], f["value"]) for f in cards[0]["figures"]] == [("目标价", "824.5"), ("最新价", "825")]
    assert_no_order_action(cards[0])
    feed = [event for _, event in app.state.broker.transient_events[session_id]
            if event.get("name") == "feed.price-alert"]
    assert len(feed) == 1 and feed[0]["payload"]["id"] == above["id"]
    assert feed[0]["payload"]["lastPrice"] == 825.0 and feed[0]["payload"]["direction"] == "above"
    announcements = app.state.voice_realtime._pending_announcements[USER]
    assert announcements[-1]["kind"] == f"price-alert-{above['id']}" and len(announcements) == 1
    assert "到价提醒" in announcements[-1]["text"]

    provider.last = 821.0  # the below alert crosses on its own
    await settle(app)
    await monitor._tick()
    listed = app.state.store.list_price_alerts(user_id=USER)
    assert not listed["active"] and len(listed["triggered"]) == 2
    assert len(app.state.voice_realtime._pending_announcements[USER]) == 2

    # #932: a triggered alert is archived history, not a missing row — deleting it says so (409 with
    # the Chinese reason the AI and the CLI relay) instead of 404, and the row stays triggered.
    refused = await http.delete(f"/api/market/alerts/{above['id']}")
    assert refused.status_code == 409 and "已触发" in refused.json()["detail"]
    assert app.state.store.list_price_alerts(user_id=USER)["triggered"][-1]["status"] == "triggered"
    assert (await http.delete("/api/market/alerts/no-such-alert")).status_code == 404
    await http.aclose()
    app.state.store.close()


async def test_a_stale_or_unreadable_quote_skips_the_round_without_losing_the_alert(tmp_path: Path) -> None:
    app = application(tmp_path / "alerts-stale.sqlite3", Gateway(), AlertProvider())
    provider = app.state.data_provider
    provider.quote_datetime = quote_now()
    http = await login(app)
    made = (await create(http, price=824.5)).json()["alert"]

    provider.last = 825.0
    provider.quote_datetime = stale_quote()  # the price crossed, the quote did not refresh
    await settle(app)
    await app.state.price_alerts._tick()
    assert len(app.state.store.list_price_alerts(user_id=USER)["active"]) == 1

    provider.quote_datetime = quote_now()
    provider.last = None  # a source with no price at all: the read fails, not the alert
    await settle(app)
    await app.state.price_alerts._tick()
    assert len(app.state.store.list_price_alerts(user_id=USER)["active"]) == 1

    provider.last = 825.0  # a fresh readable quote crosses and the same alert still fires
    await settle(app)
    await app.state.price_alerts._tick()
    listed = app.state.store.list_price_alerts(user_id=USER)
    assert not listed["active"] and listed["triggered"][0]["id"] == made["id"]
    await http.aclose()
    app.state.store.close()


async def test_active_alerts_survive_a_restart_of_the_runtime(tmp_path: Path) -> None:
    database = tmp_path / "alerts-restart.sqlite3"
    first = application(database, Gateway(), AlertProvider())
    first.state.data_provider.quote_datetime = quote_now()
    http = await login(first)
    made = (await create(http, price=824.5)).json()["alert"]
    await http.aclose()
    first.state.store.close()

    second = application(database, Gateway(), AlertProvider())
    second.state.data_provider.quote_datetime = quote_now()
    second.state.data_provider.last = 825.0
    await second.state.price_alerts._tick()
    listed = second.state.store.list_price_alerts(user_id=USER)
    assert not listed["active"] and listed["triggered"][0]["id"] == made["id"]
    second.state.store.close()


async def test_the_three_ai_tools_use_the_same_catalog_routes(tmp_path: Path) -> None:
    app = application(tmp_path / "alerts-tools.sqlite3", Gateway(), AlertProvider())
    app.state.data_provider.quote_datetime = quote_now()
    http = await login(app)
    session = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
    tools = TraderReadOnlyTools(app.state.data_provider, app.state.adapter.runtime_status)
    tools.app_capabilities = app.state.app_capabilities

    async def invoke(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result, _ = await tools.call_async(tool=tool, arguments=arguments, turn_id="alerts-tools",
                                           mode="local", session_id=session["sessionId"], user_id=USER)
        return result

    made = await invoke("create_price_alert", {"symbol": "沪金2612", "price": 825.0, "note": "突破追"})
    assert made["success"]
    body = tool_payload(made)
    assert body["readOnly"] is False
    alert = body["data"]["alert"]
    assert alert["symbol"] == "SHFE.au2612" and alert["via"] == "chat"  # the spoken name resolved to the contract
    assert alert["sessionId"] == session["sessionId"]
    assert body["data"]["summary"] == "沪金2612 涨到 825 提醒，现价 824，备注：突破追"

    listed = await invoke("list_price_alerts", {})
    assert listed["success"]
    assert tool_payload(listed)["data"]["lines"] == ["沪金2612 涨到 825 提醒，备注：突破追"]

    cancelled = await invoke("cancel_price_alert", {"alertId": alert["id"]})
    assert cancelled["success"]
    assert tool_payload(await invoke("list_price_alerts", {}))["data"]["lines"] == []

    refused = await invoke("create_price_alert", {"symbol": "KQ.m@SHFE.rb", "price": 3300})
    assert not refused["success"] and "主连" in tool_payload(refused)["warning"]
    await http.aclose()
    app.state.store.close()


def test_the_price_alerts_skill_claims_exactly_its_tools_and_routes() -> None:
    skill = skill_by_id("price-alerts")
    assert skill is not None and skill.category == "行情"
    assert set(skill.tools) == {"create_price_alert", "list_price_alerts", "cancel_price_alert"}
    for name in skill.tools:
        assert name in TOOL_ARGUMENT_MODELS
    assert {"GET /api/market/alerts", "POST /api/market/alerts",
            "DELETE /api/market/alerts/{alert_id}"} <= set(skill.routes)


async def test_a_main_contract_alert_is_watched_through_the_board_not_a_chart_series(tmp_path: Path) -> None:
    """10-02 负载复核 + 2026-10-03：盯价只需最新价。主力合约走报价总表、非主力合约走只订报价
    的 extras 通道（都不占 K 线名额，全站只有 48 个），监控循环不再为任何被盯合约读图表数据。"""
    app = application(tmp_path / "alerts-board.sqlite3", Gateway(), AlertProvider())
    provider = app.state.data_provider
    provider.quote_datetime = quote_now()
    http = await login(app)
    made = (await create(http, price=824.5)).json()["alert"]  # SHFE.au2612 is a main contract
    shared = app.state.shared_market
    chart_reads: list[str] = []
    original = shared.snapshot

    async def counting(symbol, turn_id=""):
        if turn_id == "pricealert-monitor":
            chart_reads.append(symbol)
        return await original(symbol, turn_id)

    shared.snapshot = counting
    quotes_reads: list[str] = []
    original_quotes = shared.quotes

    async def counting_quotes(symbols):
        quotes_reads.extend(symbols)
        return await original_quotes(symbols)

    shared.quotes = counting_quotes
    for _ in range(3):
        await settle(app)
        await app.state.price_alerts._tick()
    assert chart_reads == []  # three monitor rounds, no chart packet read for a main contract
    provider.last = 825.0
    await settle(app)
    await app.state.price_alerts._tick()
    listed = app.state.store.list_price_alerts(user_id=USER)
    assert not listed["active"] and listed["triggered"][0]["id"] == made["id"]
    assert chart_reads == []
    # A non-main contract has no board row: its alert rides the quote-only extras
    # channel — still no chart packet, no kline serial held.
    app.state.store.create_price_alert(
        user_id=USER, symbol="SHFE.hc2610", name="热卷2610", direction="above", price=Decimal("9999"),
        note="", via="ui", session_id=None, max_active=20)
    await app.state.price_alerts._tick()
    assert quotes_reads == ["SHFE.hc2610"] and chart_reads == []
    await http.aclose()
    app.state.store.close()


class PreOpenProvider(AlertProvider):
    """hc2610 answers 盘前: no live trade, only 昨结算 — the reference creation judges on."""

    def market_quotes(self, symbols: Any) -> dict[str, Any]:
        quotes = super().market_quotes(symbols)
        rows = [{**row, "last": None, "preSettlement": 3400.0}
                if row["symbol"] == "SHFE.hc2610" else row for row in quotes["rows"]]
        return {**quotes, "rows": rows}


async def test_the_quotes_channel_shares_one_read_a_second_and_judges_pre_open_on_pre_settlement(tmp_path: Path) -> None:
    """只订报价的 extras 通道 (2026-10-03)：同一符号集合的一秒共享读（顺序与大小写不拆缓存），
    行结构与报价总表一致；盘前行没有最新价，创建方向按昨结算判定。"""
    app = application(tmp_path / "alerts-quotes.sqlite3", Gateway(), PreOpenProvider())
    provider = app.state.data_provider
    provider.quote_datetime = quote_now()
    http = await login(app)
    shared = app.state.shared_market
    first = await shared.quotes(["SHFE.au2612", "shfe.hc2610"])
    again = await shared.quotes(["SHFE.hc2610", "SHFE.AU2612"])  # order and case never split the cache
    assert provider.quotes_reads == 1  # one upstream read per second for one symbol set
    assert first == again
    rows = {row["symbol"]: row for row in first["rows"]}
    assert rows["SHFE.au2612"]["last"] == provider.last  # steered like the board
    assert set(rows["SHFE.hc2610"]) == set(rows["SHFE.au2612"])  # the same row projection
    unknown = await shared.quotes(["SHFE.xx9999"])
    assert provider.quotes_reads == 2  # a different set is its own shared read
    # The channel normalizes to the catalog's uppercase symbols, unknown ones included.
    assert unknown["rows"] == [{"symbol": "SHFE.XX9999", "errorCode": "symbol_not_found"}]
    # 盘前: no live trade — creation falls back to 昨结算 to infer the direction.
    below = await create(http, symbol="SHFE.hc2610", price=3399.0, direction=None)
    assert below.status_code == 201 and below.json()["alert"]["direction"] == "below", below.text
    equal = await create(http, symbol="SHFE.hc2610", price=3400.0, direction=None)
    assert equal.status_code == 422 and "现价就是" in detail(equal)
    await http.aclose()
    app.state.store.close()


async def test_the_calendar_route_serves_the_weekend_rule(tmp_path: Path) -> None:
    """交易日历路由 (2026-10-03)：回放对话框默认日期的数据源——当前交易日、最近已结束交易日
    与最近十个已结束交易日（新→旧）；fake 模式按周末规则给出。"""
    from datetime import date
    app = application(tmp_path / "alerts-calendar.sqlite3", Gateway(), AlertProvider())
    http = await login(app)
    response = await http.get("/api/market/calendar")
    assert response.status_code == 200, response.text
    calendar = response.json()
    assert set(calendar) == {"tradingDay", "previousTradingDay", "recentTradingDays"}
    assert len(calendar["recentTradingDays"]) == 10
    assert calendar["recentTradingDays"] == sorted(calendar["recentTradingDays"], reverse=True)
    assert calendar["previousTradingDay"] == calendar["recentTradingDays"][0]
    assert all(day < calendar["tradingDay"] for day in calendar["recentTradingDays"])
    assert all(date.fromisoformat(day).weekday() < 5 for day in calendar["recentTradingDays"])
    await http.aclose()
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

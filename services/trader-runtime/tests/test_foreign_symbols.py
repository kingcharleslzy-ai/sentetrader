"""外盘代码统一（2026-10-21，外盘第 2 批 2b）：快期外盘主连（KQD.m@CME.NQ）与美元子账户的月份合约（CME.GCEZ26）
在界面命令、AI 工具、自选、简报、回放、提醒、推送与语音里都按同一份规则（instruments.py）认得：界面命令切到外盘图表
不再 422；AI 读外盘月份合约的行情读 MT5；外盘品种能加自选；回放对外盘给中文原因；美元子账户的外盘仓按 CME 时段
提醒收盘、按 MT5 最后交易日提醒到期、按 MT5 1 分钟线报异动；外盘到价提醒挂在 MT5 月份上并按它的报价判；推送与语音写中文名。"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx

from test_account_reminders import ReminderProvider, login, reminder_notices
from test_ai_annotations import HEADERS, _acknowledge, observation, ui_state
from test_foreign_market import _chart_service, _ChartBridge, _mt5_quote
from test_movement_alerts import Shared, moving_workbench
from test_simulation_orders import USER, Gateway, application
from trader_runtime import account_reminders, delivery_reminders
from trader_runtime.delivery_rules import foreign_expiry_notice
from trader_runtime.main import create_app
from trader_runtime.replay_requests import FOREIGN_REPLAY_REFUSAL
from trader_runtime.trader_tools import TOOL_ARGUMENT_MODELS, TraderReadOnlyTools
from trader_runtime.trading_calendar import CST, cme_trading_day
from trader_runtime.voice_realtime import _speakable
from trader_runtime.web_push import PushHub

NQ_CHART = "KQD.m@CME.NQ"
GOLD_CHART, GOLD_MONTH = "KQD.m@COMEX.GC", "CME.GCEZ26"


class FakeForeign:
    """The one foreign entry as these readers see it: the board's main month, the tradable month catalog (with its
    last trading day), each month's own MT5 quote, and a month's 1-minute workbench."""

    def __init__(self, *, last: float = 4186.7, expire: date | None = None) -> None:
        self.last = {GOLD_MONTH: last}
        self.expire = expire or date(2026, 12, 28)
        self.workbenches: dict[str, dict[str, Any]] = {}
        self.month_reads: list[list[str]] = []

    async def board(self) -> dict[str, Any]:
        return {"status": "ok", "rows": [{"key": "COMEX.GC", "source": "mt5", "contract": "GCEZ26", "last": self.last[GOLD_MONTH]},
                                         {"key": "LMEOTC.CA", "source": "sina", "last": 10000.0}]}

    def trade_catalog(self) -> dict[str, Any]:
        return {"items": [{"symbol": GOLD_MONTH, "instrumentName": "COMEX黄金2612", "priceTick": 0.1, "volumeMultiple": 100,
                           "foreignKey": "COMEX.GC", "expireDate": self.expire.isoformat()}]}

    def month_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        self.month_reads.append(list(symbols))
        now = datetime.now(CST).isoformat(timespec="seconds")
        return {symbol.upper(): {"last": self.last[symbol.upper()], "quoteTime": now, "contract": symbol.partition(".")[2]}
                for symbol in symbols if symbol.upper() in self.last}

    def workbench(self, symbol: str, duration: int) -> dict[str, Any] | None:
        return self.workbenches.get(symbol.upper())


def install(app: Any, foreign: Any) -> Any:
    app.state.ctx.services.foreign_market = foreign
    return foreign


# -- 界面命令与 AI 工具 ----------------------------------------------------------------------------------


async def test_the_ai_and_voice_switch_a_window_to_a_foreign_chart(tmp_path: Path) -> None:
    """“切到纳指”与 AI 切图表：symbol KQD.m@CME.NQ 以前在格式这一层 422，现在派到窗口并回 applied。"""
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="annotation-test-token")
    window_id = str(uuid4())
    pulse = {"windowId": window_id, "revision": 0, "state": ui_state(), "observation": observation()}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            assert (await client.post("/api/ui/pulse", json=pulse)).status_code == 200
            pending = asyncio.create_task(client.post("/api/app/ui/commands", params={"waitMs": 3000}, json={
                "commandId": str(uuid4()), "windowId": window_id, "expectedRevision": pulse["revision"],
                "changes": {"symbol": NQ_CHART}}))
            command = await _acknowledge(client, pulse, lambda changes: {**pulse["state"], "symbol": changes["symbol"]})
            assert command["changes"] == {"symbol": NQ_CHART}
            result = (await pending).json()
            assert result["status"] == "applied" and result["state"]["symbol"] == NQ_CHART, result
            # The other contract fields of the same kind take it too (queued, not a format 422).
            for changes in ({"chartAnnotations": {"op": "clear", "symbol": NQ_CHART}}, {"news": {"pinnedSymbol": NQ_CHART}},
                            {"contractProduct": "COMEX.GC"}, {"mobileMarket": {"cell": {"index": 1, "symbol": GOLD_CHART}}},
                            {"pinnedSymbols": {"add": NQ_CHART}}):
                body = {"commandId": str(uuid4()), "windowId": window_id, "expectedRevision": pulse["revision"], "changes": changes}
                response = await client.post("/api/app/ui/commands", params={"waitMs": 0}, json=body)
                assert response.status_code != 422, (changes, response.text)
            # A malformed code is still refused at the format.
            bad = await client.post("/api/app/ui/commands", json={"commandId": str(uuid4()), "windowId": window_id,
                                                                  "expectedRevision": pulse["revision"], "changes": {"symbol": "KQD.m@x/../y"}})
            assert bad.status_code == 422


def test_ai_tools_take_the_foreign_chart_and_month_codes() -> None:
    for tool, body in (("get_market_snapshot", {"symbol": NQ_CHART}), ("get_market_analysis", {"symbol": GOLD_MONTH}),
                       ("get_chart_bars", {"symbol": NQ_CHART}), ("annotate_chart", {"op": "clear", "symbol": NQ_CHART}),
                       ("get_research_brief", {"symbol": NQ_CHART, "topic": "news"}),
                       ("run_strategy_backtest", {"strategyId": "s1", "symbol": GOLD_CHART})):
        assert TOOL_ARGUMENT_MODELS[tool].model_validate(body).symbol == body["symbol"], tool


async def test_the_ai_reads_a_foreign_month_quote_from_mt5() -> None:
    """get_market_snapshot / get_market_analysis on CME.GCEG27 used to answer invalid_or_unavailable_provider_snapshot
    (the 快期 catalog has no foreign month): now the month's own MT5 quote and bars, named in Chinese."""
    bridge = _ChartBridge({"GCEZ26": _mt5_quote("GCEZ26", 4186.7, volume=150_000),
                           "GCEG27": _mt5_quote("GCEG27", 4201.3, volume=20_000)})
    service = _chart_service(("mt5", "sina", "kqd"), bridge)
    app = SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace(services=SimpleNamespace(foreign_market=service)),
                                                shared_market=SimpleNamespace()))
    tools = TraderReadOnlyTools(SimpleNamespace(), lambda mode: {})
    tools.app_capabilities = SimpleNamespace(app=app)
    result, card = await tools.shared_market_tool("get_market_snapshot", {"symbol": "CME.GCEG27"}, turn_id="t1", user_id="u")
    data = json.loads(result["contentItems"][0]["text"])["data"]
    assert result["success"], result
    assert data["symbol"] == "CME.GCEG27" and data["price"] == 4201.3 and data["name"] == "COMEX黄金2702"
    assert data["source"] == "MT5 · GCEG27" and data["currency"] == "USD" and card is not None
    result, _ = await tools.shared_market_tool("get_market_analysis", {"symbol": "CME.GCEG27", "durationSeconds": 300},
                                               turn_id="t2", user_id="u")
    assert result["success"], result
    # MT5 down: a plain unavailable answer naming the contract, never another source's price.
    service.sources[0].get_json = lambda path: (_ for _ in ()).throw(ConnectionError("down"))
    result, _ = await tools.shared_market_tool("get_market_snapshot", {"symbol": "CME.GCEG27"}, turn_id="t3", user_id="u")
    assert not result["success"] and "COMEX黄金2702" in result["contentItems"][0]["text"]


# -- 自选、简报、回放 -------------------------------------------------------------------------------------


async def test_foreign_products_star_into_the_watchlist_and_a_foreign_chart_saves_into_a_phone_cell(tmp_path: Path) -> None:
    app = application(tmp_path / "watch.sqlite3", Gateway())
    http = await login(app)
    saved = await http.put("/api/preferences", json={"watchlist": {"products": ["SHFE.rb", "COMEX.GC", "CME.6E"]}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["preferences"]["watchlist"]["products"] == ["SHFE.rb", "COMEX.GC", "CME.6E"]
    cells = [{"symbol": GOLD_CHART, "duration": 300}] + [{"symbol": None, "duration": 300}] * 5
    saved = await http.put("/api/preferences", json={"ui": {"mobileMarket": {"cells": cells}}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["preferences"]["ui"]["mobileMarket"]["cells"][0]["symbol"] == GOLD_CHART
    # A starred foreign product is an AI 简报 default too (外盘简报 2026-10-21: its major screened 快讯).
    subscriptions = (await http.get("/api/app/brief-subscriptions", params={"symbol": NQ_CHART})).json()
    assert [item["product"] for item in subscriptions["products"]] == ["SHFE.rb", "COMEX.GC", "CME.6E"]
    assert (await http.get("/api/app/briefs", params={"symbol": NQ_CHART})).status_code == 200
    assert (await http.put("/api/preferences", json={"watchlist": {"products": ["COMEX.GC/x"]}})).status_code == 422
    await http.aclose()
    app.state.store.close()


async def test_a_foreign_replay_is_refused_with_its_reason(tmp_path: Path) -> None:
    app = application(tmp_path / "replay.sqlite3", Gateway())
    http = await login(app)
    for symbol in (NQ_CHART, GOLD_MONTH):
        response = await http.post("/api/replays", json={"symbol": symbol, "tradingDay": "2026-10-16", "initialBalance": 1000000})
        assert response.status_code == 422 and response.json()["detail"] == FOREIGN_REPLAY_REFUSAL, response.text
    # A domestic main series is still a format refusal (replay needs one contract).
    assert (await http.post("/api/replays", json={"symbol": "KQ.m@SHFE.rb", "tradingDay": "2026-10-16",
                                                   "initialBalance": 1000000})).status_code == 422
    await http.aclose()
    app.state.store.close()


# -- 提醒 ---------------------------------------------------------------------------------------------


def _cme_moment(day: date, hour: int, minute: int) -> int:
    from trader_runtime.trading_calendar import NEW_YORK
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK).timestamp()) * 1_000_000_000


async def test_a_foreign_position_gets_the_cme_close_reminder(tmp_path: Path, monkeypatch) -> None:
    """美元子账户的外盘仓以前查国内目录查不到、静默不提醒：现在按 CME 时段（美东 17:00 收盘前 5 分钟）提醒，写中文名。"""
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: _cme_moment(date(2026, 10, 6), 16, 56))
    provider = ReminderProvider()
    provider.position_items = [{"symbol": GOLD_MONTH, "direction": "long", "volume": 1, "unrealizedPnl": 120,
                                "todayVolume": 1, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    app = application(tmp_path / "close-cme.sqlite3", Gateway(), provider)
    http = await login(app)
    assert (await http.put("/api/preferences", json={"notify": {"marketCloseReminder": True}})).status_code == 200
    session_id = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
    assert (await http.get("/api/positions")).status_code == 200
    notices = reminder_notices(app, session_id, "reminder-market_close_day")
    assert len(notices) == 1
    text = str(notices[0]["payload"]["text"])
    assert "收盘提醒：4 分钟后外盘收盘" in text and "COMEX黄金2612 多 1 手（浮盈 +120）" in text, text
    assert notices[0]["payload"]["turnId"].endswith("-2026-10-06")  # the CME trading day
    await http.aclose()
    app.state.store.close()


def test_a_foreign_month_expiry_notice_counts_cme_trading_days() -> None:
    expire = date(2026, 12, 28)  # Monday
    assert foreign_expiry_notice(expire, date(2026, 12, 1)) is None  # far off
    warning = foreign_expiry_notice(expire, date(2026, 12, 16))
    assert warning is not None and warning["level"] == "warning" and warning["tradingDaysLeft"] == 8  # 12-25 is closed
    danger = foreign_expiry_notice(expire, date(2026, 12, 24))
    assert danger is not None and danger["level"] == "danger" and danger["tradingDaysLeft"] == 2
    assert "12 月 28 日到期" in danger["text"] and "换月" in danger["text"]
    assert foreign_expiry_notice(expire, date(2026, 12, 29)) is None


async def test_a_foreign_position_gets_its_expiry_reminder(tmp_path: Path) -> None:
    app = application(tmp_path / "delivery-cme.sqlite3", Gateway(), ReminderProvider())
    install(app, FakeForeign(expire=date.fromisoformat(cme_trading_day(time.time_ns()))))  # a CME day even on weekends
    http = await login(app)
    session_id = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
    positions = [{"symbol": GOLD_MONTH, "direction": "long", "volume": 1}]
    sent = await delivery_reminders.check_positions(app, user_id=USER, positions=positions, account_id="paperusd:test")
    assert sent == 1
    notices = reminder_notices(app, session_id, f"delivery-{GOLD_MONTH}")
    assert len(notices) == 1 and "交割提醒：COMEX黄金2612（CME.GCEZ26）" in str(notices[0]["payload"]["text"])
    assert "到期" in str(notices[0]["payload"]["text"])
    await http.aclose()
    app.state.store.close()


async def test_a_foreign_position_movement_is_read_from_mt5_without_a_shared_channel(tmp_path: Path) -> None:
    provider = ReminderProvider()
    provider.position_items = [{"symbol": GOLD_MONTH, "direction": "long", "volume": 1, "unrealizedPnl": 120}]
    app = application(tmp_path / "movement-cme.sqlite3", Gateway(), provider)
    shared = Shared()
    app.state.shared_market = shared
    start = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
    foreign = install(app, FakeForeign())
    moving = moving_workbench(GOLD_MONTH, start)
    foreign.workbenches[GOLD_MONTH] = {**moving, "quote": {**moving["quote"], "name": "COMEX黄金2612"}}
    http = await login(app)
    session_id = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
    assert (await http.put("/api/preferences", json={"notify": {"movement": True}})).status_code == 200
    watch = app.state.movement_watch
    watch.now = lambda: start
    counts = await watch.run_once()
    assert counts["fired"] == 1 and shared.opened == []   # MT5's own bars; no 快期 channel for a foreign month
    notices = reminder_notices(app, session_id, "movement-")
    assert len(notices) == 1
    text = str(notices[0]["payload"]["text"])
    assert text.startswith("盘中异动：COMEX黄金2612") and "美元" in text, text
    await http.aclose()
    app.state.store.close()


async def test_a_foreign_price_alert_sits_on_the_mt5_month_and_fires_on_its_quote(tmp_path: Path) -> None:
    from test_price_alerts import AlertProvider
    from test_simulation_orders import quote_now
    app = application(tmp_path / "alerts-cme.sqlite3", Gateway(), AlertProvider())
    app.state.data_provider.quote_datetime = quote_now()
    foreign = install(app, FakeForeign(last=4186.7))
    http = await login(app)
    created = await http.post("/api/market/alerts", json={"symbol": GOLD_CHART, "price": 4200})
    assert created.status_code == 201, created.text
    alert = created.json()["alert"]
    assert alert["symbol"] == GOLD_MONTH and alert["name"] == "COMEX黄金2612" and alert["direction"] == "above"
    refused = await http.post("/api/market/alerts", json={"symbol": "KQD.m@LMEOTC.CA", "price": 9000})
    assert refused.status_code == 422 and "只接了参考行情" in refused.json()["detail"]
    off_tick = await http.post("/api/market/alerts", json={"symbol": GOLD_MONTH, "price": 4200.05})
    assert off_tick.status_code == 422 and "最小变动价位" in off_tick.json()["detail"]

    monitor = app.state.price_alerts
    await monitor._tick()
    assert [item["id"] for item in app.state.store.list_price_alerts(user_id=USER)["active"]] == [alert["id"]]
    foreign.last[GOLD_MONTH] = 4200.3
    await monitor._tick()
    listed = app.state.store.list_price_alerts(user_id=USER)
    assert listed["active"] == [] and listed["triggered"][0]["id"] == alert["id"]
    assert all(read == [GOLD_MONTH] for read in foreign.month_reads)
    await http.aclose()
    app.state.store.close()


# -- 名称 ---------------------------------------------------------------------------------------------


async def test_pushes_and_the_voice_name_foreign_contracts_in_chinese() -> None:
    hub = PushHub(SimpleNamespace(state=SimpleNamespace(shared_market=None)), None, sender=lambda *a, **k: None)
    assert await hub.instrument_name(GOLD_MONTH) == "COMEX黄金2612"
    assert await hub.instrument_name("CME.ENQZ26") == "迷你纳指100 2612"
    assert await hub.instrument_name(GOLD_CHART) == "COMEX黄金"
    spoken = _speakable("已成交：COMEX黄金2612（CME.GCEZ26）买开 1 手；GCEZ26 还有挂单，SHFE.rb2610 不变")
    assert "CME.GCEZ26" not in spoken and "COMEX黄金2612还有挂单" in spoken.replace(" ", "") and "SHFE.rb2610" in spoken

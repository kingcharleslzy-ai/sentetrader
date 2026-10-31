"""M3: the three sidecar-backed query tools, the intraday open-position limit on
create_trade_proposal, and the call-auction readiness notice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fixtures.card_tools import legacy_card_args
from test_simulation_orders import (  # noqa: F401 - the shared app test scaffolding
    USER,
    Gateway,
    NormalProvider,
    _tool_status,
    application,
)
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.trader_tools import TraderReadOnlyTools


def _tool_text(result: dict) -> dict:
    return json.loads(result["contentItems"][0]["text"])


def bare_tools(provider: Any = None) -> TraderReadOnlyTools:
    base = provider or FakeReadOnlyDataProvider()
    status = lambda mode: {  # noqa: E731 - mirrors _tool_status without an app
        "mode": mode, "adapter": "native-agent", "runtimeConnected": True,
        "agentConnected": True, "connected": True, "version": "1.6.5",
        **base.runtime_status(),
    }
    return TraderReadOnlyTools(base, status)


class M3Provider(NormalProvider):
    """Normal quotes plus the M3 knobs: the exchange's intraday open-position cap,
    today's OPEN fills, the raw trading status, and (for one test) a product that
    appears on a second exchange so the bare-product spelling is ambiguous."""

    def __init__(
        self,
        *,
        open_limit: int = 0,
        opened_volume: int = 0,
        trading_status: str | None = None,
        trading_day: str | None = None,
        duplicate_product: str | None = None,
    ) -> None:
        super().__init__()
        self.open_limit = open_limit
        self.opened_volume = opened_volume
        self.trading_status = trading_status
        self.trading_day = trading_day
        self.duplicate_product = duplicate_product

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        summary = super().account_summary(turn_id)
        if self.trading_day:
            summary["tradingDay"] = self.trading_day
        return summary

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        snapshot = super().market_snapshot(turn_id, symbol)
        if self.open_limit:
            snapshot["openLimit"] = self.open_limit
        if self.trading_status:
            snapshot["tradingStatus"] = self.trading_status
        return snapshot

    def account_activity(self) -> dict[str, Any]:
        activity = super().account_activity()
        if self.opened_volume:
            activity["trades"] = [{
                "symbol": "SHFE.au2612", "offset": "OPEN",
                "volume": self.opened_volume, "tradingDay": self.trading_day,
            }]
        return activity

    def instrument_catalog(self) -> dict[str, Any]:
        catalog = super().instrument_catalog()
        if self.duplicate_product:
            au_row = next(
                item for item in catalog["items"] if item["productId"] == "au"
            )
            twin = {
                **au_row,
                "symbol": f"DCE.{self.duplicate_product}2612",
                "exchangeId": "DCE",
                "productId": self.duplicate_product,
            }
            catalog = {**catalog, "items": [*catalog["items"], twin]}
        return catalog


def test_settlement_cont_history_and_ranking_tools_expose_the_sidecar_queries():
    tools = bare_tools()

    result, event = tools.call(
        tool="get_settlement_prices",
        arguments={"symbols": ["SHFE.rb2610", "SHFE.au2612"], "days": 3},
        turn_id="turn-settle", mode="local",
    )
    assert event is None and result["success"] is True
    data = _tool_text(result)["data"]
    assert data["schemaVersion"] == 1 and data["days"] == 3
    assert len(data["items"]) == 6
    assert data["asOf"] == max(item["tradingDay"] for item in data["items"])
    assert {item["symbol"] for item in data["items"]} == {"SHFE.rb2610", "SHFE.au2612"}

    result, _ = tools.call(
        tool="get_main_contract_history",
        arguments={"symbol": "SHFE.rb", "days": 20},
        turn_id="turn-cont", mode="local",
    )
    data = _tool_text(result)["data"]
    assert data["contSymbol"] == "KQ.m@SHFE.rb"
    assert len(data["rows"]) == 20
    assert len(data["rollDates"]) == 1  # 2510 → 2610 inside the window

    result, _ = tools.call(
        tool="get_symbol_ranking",
        arguments={"symbol": "SHFE.rb2610", "rankingType": "LONG", "days": 2},
        turn_id="turn-rank", mode="local",
    )
    data = _tool_text(result)["data"]
    assert data["rankingType"] == "LONG" and len(data["rows"]) == 20  # 10 brokers × 2 days
    assert data["rows"][0]["rank"] == 1 and data["rows"][0]["broker"]
    assert data["asOf"] == max(item["tradingDay"] for item in data["rows"])

    for tool, arguments in (
        ("get_settlement_prices", {"symbols": ["SHFE.rb9999"]}),
        ("get_symbol_ranking", {"symbol": "SHFE.rb9999"}),
    ):
        result, _ = tools.call(tool=tool, arguments=arguments, turn_id="t", mode="local")
        assert result["success"] is False
        assert _tool_text(result)["errorCode"] == "market_symbol_not_found"


def test_main_contract_history_accepts_every_symbol_spelling():
    tools = bare_tools()
    payloads = []
    for symbol in ("rb", "SHFE.rb", "KQ.m@SHFE.rb"):
        result, _ = tools.call(
            tool="get_main_contract_history",
            arguments={"symbol": symbol, "days": 5},
            turn_id="turn-spelling", mode="local",
        )
        assert result["success"] is True, symbol
        payloads.append(_tool_text(result)["data"])
    assert {payload["contSymbol"] for payload in payloads} == {"KQ.m@SHFE.rb"}
    assert payloads[0]["rows"] == payloads[2]["rows"]


def test_main_contract_history_rejects_ambiguous_and_unknown_products():
    tools = bare_tools(M3Provider(duplicate_product="au"))
    result, _ = tools.call(
        tool="get_main_contract_history",
        arguments={"symbol": "au", "days": 5},
        turn_id="turn-ambiguous", mode="local",
    )
    assert result["success"] is False
    assert _tool_text(result)["errorCode"] == "main_contract_product_ambiguous"

    tools = bare_tools()
    result, _ = tools.call(
        tool="get_main_contract_history",
        arguments={"symbol": "zz", "days": 5},
        turn_id="turn-unknown", mode="local",
    )
    assert result["success"] is False
    assert _tool_text(result)["errorCode"] == "main_contract_product_not_found"


def test_the_three_queries_are_registered_read_only_tools():
    tools = bare_tools()
    names = {definition["name"] for definition in tools.definitions()}
    assert {"get_settlement_prices", "get_main_contract_history", "get_symbol_ranking"} <= names
    assert {"get_settlement_prices", "get_main_contract_history", "get_symbol_ranking"} <= tools.read_only_names()


def test_market_board_tool_sorts_filters_and_limits_for_the_ai():
    from trader_runtime.trader_tools import BOARD_ROW_VIEW_FIELDS
    tools = bare_tools()
    assert "get_market_board" in tools.NAMES
    assert "get_market_board" in tools.read_only_names()

    result, _ = tools.call(tool="get_market_board", arguments={}, turn_id="t-board", mode="local")
    assert result["success"] is True, result
    data = _tool_text(result)["data"]
    assert [row["productName"] for row in data["rows"]] == ["铁矿石", "沪金", "沪深300股指", "螺纹钢"]  # 默认涨跌幅从高到低
    assert data["sort"] == "changePercent" and data["order"] == "desc" and data["exchange"] is None
    assert data["truncated"] is False and set(data["rows"][0]) == set(BOARD_ROW_VIEW_FIELDS)

    result, _ = tools.call(tool="get_market_board",
                           arguments={"sort": "volume", "limit": 2}, turn_id="t-board", mode="local")
    data = _tool_text(result)["data"]
    assert [row["productName"] for row in data["rows"]] == ["螺纹钢", "铁矿石"]

    result, _ = tools.call(tool="get_market_board",
                           arguments={"exchange": "shfe", "order": "asc"}, turn_id="t-board", mode="local")
    data = _tool_text(result)["data"]
    assert data["exchange"] == "SHFE" and [row["productName"] for row in data["rows"]] == ["螺纹钢", "沪金"]

    result, _ = tools.call(tool="get_market_board",
                           arguments={"exchange": "GFEX"}, turn_id="t-board", mode="local")
    assert result["success"] is True and _tool_text(result)["data"]["rows"] == []

    # Without a board-capable provider the tool is simply not offered.
    without = FakeReadOnlyDataProvider()
    without.market_board = None
    assert "get_market_board" not in bare_tools(without).NAMES
    # A provider whose board disappeared after registration fails honestly, per call.
    tools.provider.market_board = None
    result, _ = tools.call(tool="get_market_board", arguments={}, turn_id="t-board", mode="local")
    assert result["success"] is False
    assert _tool_text(result)["errorCode"] == "market_board_unavailable"


def test_market_snapshot_composes_the_open_limit_with_opened_today():
    tools = bare_tools(M3Provider(open_limit=6, opened_volume=2, trading_day="2026-09-22"))
    result, _ = tools.call(
        tool="get_market_snapshot",
        arguments={"symbol": "SHFE.au2612"},
        turn_id="turn-limit", mode="local",
    )
    data = _tool_text(result)["data"]
    assert data["openLimit"] == 6 and data["openedToday"] == 2
    assert data["openRemaining"] == 4

    # No exchange cap: openLimit stays 0 (unlimited) and no quota fields imply a limit.
    result, _ = bare_tools().call(
        tool="get_market_snapshot",
        arguments={"symbol": "SHFE.au2612"},
        turn_id="turn-plain", mode="local",
    )
    data = _tool_text(result)["data"]
    assert data["openLimit"] == 0
    assert "openedToday" not in data and "openRemaining" not in data
    # 五档盘口：工具结果整档带出（fake 源按 priceTick 外推、量递减）。
    assert data["bidPrice1"] == 823.0 and data["bidVolume1"] == 12
    assert data["bidPrice5"] == 823.0 - 0.5 * 4 and data["bidVolume5"] == 4
    assert data["askPrice5"] == 824.0 + 0.5 * 4 and data["askVolume5"] == 2


def test_opening_beyond_the_open_limit_is_refused_with_the_numbers(tmp_path: Path) -> None:
    app = application(
        tmp_path / "runtime-open-limit.sqlite3",
        Gateway(),
        M3Provider(open_limit=2, opened_volume=1, trading_day="2026-09-22"),
    )
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(
        app.state.data_provider, _tool_status(app), proposals=app.state.proposals
    )

    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 2,
                   "priceIntent": "aggressive", "reason": "开多"}),
        turn_id="turn-limit-refuse", mode="local", session_id=session.session_id,
    )
    payload = _tool_text(result)
    assert result["success"] is False and event is None
    assert payload["errorCode"] == "open_limit_exceeded"
    issues = payload["data"]["userFacing"]["candidateIssues"]
    assert "今日还能开 1 手" in issues[0]
    assert "日内开仓限额 2 手，已开 1 手" in issues[0]
    assert app.state.store.recent_proposals(session.session_id, limit=5) == []

    # Within the remaining quota the card is still created.
    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1,
                   "priceIntent": "aggressive", "reason": "开多"}),
        turn_id="turn-limit-ok", mode="local", session_id=session.session_id,
    )
    assert result["success"] is True and event is not None and event[0] == "proposal.trade"
    assert app.state.store.recent_proposals(session.session_id, limit=5) != []
    app.state.store.close()


def test_call_auction_adds_a_readiness_note_without_refusing(tmp_path: Path) -> None:
    app = application(
        tmp_path / "runtime-auction.sqlite3",
        Gateway(),
        M3Provider(trading_status="AUCTIONORDERING"),
    )
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(
        app.state.data_provider, _tool_status(app), proposals=app.state.proposals
    )

    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1,
                   "priceIntent": "aggressive", "reason": "集合竞价开多"}),
        turn_id="turn-auction", mode="local", session_id=session.session_id,
    )
    assert result["success"] is True and event is not None and event[0] == "proposal.trade"
    readiness = event[1]["readiness"]
    assert readiness["passed"] is True and readiness["issues"] == []
    assert readiness["summary"].endswith("集合竞价中，开盘撮合后才有成交")
    assert _tool_text(result)["data"]["readiness"] == readiness
    app.state.store.close()


def test_market_analysis_carries_the_four_new_indicators():
    result, _ = bare_tools().call(
        tool="get_market_analysis",
        arguments={"symbol": "SHFE.au2612"},
        turn_id="turn-indicators", mode="local",
    )
    indicators = _tool_text(result)["data"]["indicators"]
    assert set(indicators) == {"boll", "atr", "rsi", "cci"}
    assert indicators["boll"]["mid"] > 0 and indicators["boll"]["reading"]
    assert indicators["atr"]["value"] > 0 and indicators["atr"]["reading"]
    assert 0 <= indicators["rsi"]["value"] <= 100 and indicators["rsi"]["reading"]
    assert indicators["cci"]["reading"]

"""The "money" a beginner needs before an order: the estimate endpoint, the estimate_order tool
and the opening card's estimate annotation. Open gives per-lot margin / fee / max openable lots
and an honest insufficient flag; close gives the margin the matching position would release;
missing data is null plus reason codes, never an invented number and never a 500."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from conftest import per_lot_entry, seed_exchange_fees
from trader_runtime.contracts import AccountSummary, MarketSnapshot
from trader_runtime.data_providers import (
    FakeReadOnlyDataProvider,
    fake_account_summary,
    fake_market_snapshot,
)
from trader_runtime.main import create_app
from trader_runtime.trader_tools import TraderReadOnlyTools
from trader_runtime.trading_costs import trade_fee

TOKEN = "test-local-token"
ORIGIN = "http://127.0.0.1:5173"
USER_ID = "local-user"


@pytest.fixture
def application(tmp_path: Path, request: pytest.FixtureRequest):
    # 默认种一份当天新鲜的交易所标准缓存（au/m 每手 24.71，与行情源一致）；
    # 标了 no_exchange_fees 的用例不种，测“拿不到交易所标准”的兜底路径。
    if "no_exchange_fees" not in request.keywords:
        seed_exchange_fees(tmp_path, {
            "SHFE.au2612": per_lot_entry("SHFE", "au2612", "au", 1000, 24.71, 24.71, 24.71),
            # 大商所 m 的平今 = 10 × 平昨（#711）：估算按“先昨后今”拆，今仓那段收平今。
            "DCE.m2612": per_lot_entry("DCE", "m2612", "m", 10, 24.71, 24.71, 247.1),
            "SHFE.rb2701": {"exchange": "SHFE", "contract": "rb2701", "product": "rb", "multiplier": 10,
                            "rate": {"mode": "notional", "open": 1.0, "closeYesterday": 1.0, "closeToday": 1.0,
                                     "openPerLotExtra": 0.0, "closeYesterdayPerLotExtra": 0.0,
                                     "closeTodayPerLotExtra": 0.0}}})
    application = create_app(tmp_path / "test.sqlite3", runtime_state_root=tmp_path,
                             local_token=TOKEN, agent_mode="fake")
    yield application
    application.state.store.close()


@pytest.fixture
async def client(application: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test",
        headers={"Origin": ORIGIN},
    ) as test_client:
        response = await test_client.post("/api/auth/local", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200
        yield test_client


def _status(mode: str) -> dict[str, Any]:
    return {"mode": mode, **FakeReadOnlyDataProvider().runtime_status()}


async def _estimate(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    params = {"symbol": "AU2612", "side": "buy", "offset": "open", "volume": 1, **overrides}
    response = await client.get("/api/simulation/order-estimate", params=params)
    assert response.status_code == 200
    return response.json()


async def test_open_estimate_reports_margin_fee_and_max_open_volume(client: AsyncClient) -> None:
    data = await _estimate(client)
    assert data["symbol"] == "AU2612"
    assert data["marginPerLot"] == 82_350.0
    assert data["commissionPerLot"] == 24.71
    assert data["margin"] == 82_350.0
    # 未自定义费率 → 交易所标准（种的缓存），不是行情源每手手续费的兜底。
    assert data["fee"] == 24.71
    # (835570 available − 24.71 fee) ÷ 82350 per lot = 10.14 → 10 lots.
    assert data["available"] == 835_570
    assert data["maxOpenVolume"] == 10
    assert data["insufficient"] is False
    assert data["releasedMargin"] is None
    assert data["missing"] == []
    assert "保证金" in data["basis"] and "估算" in data["basis"]


async def test_open_estimate_flags_available_short_of_the_volume(client: AsyncClient) -> None:
    data = await _estimate(client, volume=11)
    assert data["maxOpenVolume"] == 10
    assert data["insufficient"] is True


async def test_close_estimate_reports_released_margin_and_no_new_margin(client: AsyncClient) -> None:
    data = await _estimate(client, side="sell", offset="close", volume=1)
    # The fake au2612 long holds 2 lots at 164700 total margin: one lot releases half.
    assert data["margin"] == 0.0
    assert data["releasedMargin"] == 82_350.0
    assert data["maxOpenVolume"] is None
    assert data["insufficient"] is False
    assert data["missing"] == []


class _NoMarginProvider(FakeReadOnlyDataProvider):
    """A quote the contract service never filled in: no per-lot amounts."""

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        snapshot = dict(fake_market_snapshot(turn_id, symbol))
        snapshot.pop("marginPerLot")
        snapshot.pop("commissionPerLot")
        return snapshot


def test_missing_market_amounts_are_null_with_reasons(application: FastAPI) -> None:
    data = application.state.proposals.order_estimate(
        user_id=USER_ID, symbol="AU2612", side="buy", offset="open",
        volume=1, provider=_NoMarginProvider(),
    )
    assert data["marginPerLot"] is None
    # 每手手续费不读行情源（#770）：交易所标准给得出就照给，与下单票同一个数。
    assert data["commissionPerLot"] == 24.71 and data["commissionSource"] == "exchange_standard"
    assert data["margin"] is None
    assert data["maxOpenVolume"] is None
    assert data["insufficient"] is False
    assert "margin_per_lot_unavailable" in data["missing"]


class _AccountDownProvider(FakeReadOnlyDataProvider):
    def account_summary(self, turn_id: str) -> dict[str, Any]:
        summary = dict(fake_account_summary(turn_id))
        summary["dataStatus"] = "unavailable"
        return summary


def test_unavailable_account_keeps_available_null(application: FastAPI) -> None:
    data = application.state.proposals.order_estimate(
        user_id=USER_ID, symbol="AU2612", side="buy", offset="open",
        volume=1, provider=_AccountDownProvider(),
    )
    assert data["available"] is None
    assert data["maxOpenVolume"] is None
    assert "account_unavailable" in data["missing"]


async def test_estimate_fee_matches_the_trading_cost_canon(application: FastAPI, client: AsyncClient) -> None:
    schedule = application.state.proposals.fee_settings.save(USER_ID, {
        "source": "custom",
        "default": {"mode": "notional", "open": 1.0, "closeYesterday": 1.0, "closeToday": 2.0},
    })
    data = await _estimate(client, volume=2, price=823.5)
    expected = trade_fee(
        schedule, symbol="AU2612", price=823.5, volume=2, multiplier=1000.0,
        offset="OPEN", today=True,
    )
    assert data["price"] == 823.5
    assert data["fee"] == expected == 164.7  # 万分之1 of 823.5 × 1000 × 2 lots


@pytest.mark.no_exchange_fees
async def test_without_exchange_standard_the_quote_commission_is_the_fallback(application: FastAPI) -> None:
    """种的缓存也没有、测试又不联网：交易所标准拿不到时按行情源每手手续费估算并注明。"""
    data = await _estimate_via_app(application)
    assert data["fee"] == 24.71 and data["commissionPerLot"] == 24.71
    assert data["commissionSource"] == "market_source"
    assert data["maxOpenVolume"] == 10
    assert "exchange_fees_unavailable:交易所标准暂不可用，按行情源每手手续费估算" in [
        str(item) for item in data["missing"]]


async def _estimate_via_app(application: FastAPI) -> dict[str, Any]:
    return application.state.proposals.order_estimate(
        user_id=USER_ID, symbol="AU2612", side="buy", offset="open", volume=1)


class _RbProvider(FakeReadOnlyDataProvider):
    """rb2701 @3071（乘数 10）：快期行情的每手手续费只有交易所标准的约 1/10（0.3116）。"""

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        snapshot = dict(fake_market_snapshot(turn_id, "SHFE.rb2701"))
        snapshot.update({"price": 3071.0, "volumeMultiple": 10.0,
                         "marginPerLot": 4000.0, "commissionPerLot": 0.3116})
        return snapshot


async def test_rb_open_estimates_the_exchange_standard_notional_fee(application: FastAPI) -> None:
    data = application.state.proposals.order_estimate(
        user_id=USER_ID, symbol="SHFE.rb2701", side="buy", offset="open", volume=1,
        provider=_RbProvider())
    assert data["missing"] == []
    assert data["fee"] == 3.07        # 万分之1 × 3071 × 10，不是行情源的 0.31
    assert data["maxOpenVolume"] is not None
    # 估算里的每手手续费也是这个数，不再回显行情源的 0.3116（#770：AI 照它答成“一手 0.31”）。
    assert data["commissionPerLot"] == 3.07
    assert data["commissionSource"] == "exchange_standard"
    assert data["units"]["commissionPerLot"].startswith("元/手")


def _tool_data(tools: TraderReadOnlyTools, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response, _ = tools.call(tool=tool, arguments=arguments, turn_id="turn-770", mode="local", user_id=USER_ID)
    assert response["success"] is True
    return json.loads(response["contentItems"][0]["text"])["data"]


async def test_ai_snapshot_and_estimate_report_the_ticket_fee(application: FastAPI) -> None:
    """#770：问“做多一手螺纹要多少保证金”，AI 的行情快照 contract 与 estimate_order 报的每手
    手续费，和下单票（同一 order_estimate 入口）是同一个数——不是行情源的 0.31。"""
    provider = _RbProvider()
    ticket = application.state.proposals.order_estimate(
        user_id=USER_ID, symbol="SHFE.rb2701", side="buy", offset="open", volume=1, provider=provider)
    tools = TraderReadOnlyTools(provider, _status, proposals=application.state.proposals)
    snapshot = _tool_data(tools, "get_market_snapshot", {"symbol": "SHFE.rb2701"})
    estimate = _tool_data(tools, "estimate_order",
                          {"symbol": "SHFE.rb2701", "side": "buy", "offset": "open", "volume": 1})
    assert ticket["fee"] == 3.07
    assert snapshot["contract"]["commissionPerLot"] == snapshot["commissionPerLot"] == ticket["fee"]
    assert snapshot["contract"]["commissionSource"] == "exchange_standard"
    assert snapshot["contract"]["commissionUnit"].startswith("元/手")
    assert estimate["commissionPerLot"] == estimate["fee"] == ticket["fee"]
    assert snapshot["contract"]["marginPerLot"] == estimate["marginPerLot"] == ticket["marginPerLot"]


async def test_ai_snapshot_fee_follows_the_users_custom_schedule(application: FastAPI) -> None:
    """自定义了费率：快照里的每手手续费跟下单票一样按自设费率算，并标 custom。"""
    application.state.proposals.fee_settings.save(USER_ID, {
        "source": "custom",
        "default": {"mode": "notional", "open": 2.0, "closeYesterday": 2.0, "closeToday": 2.0},
    })
    provider = _RbProvider()
    ticket = application.state.proposals.order_estimate(
        user_id=USER_ID, symbol="SHFE.rb2701", side="buy", offset="open", volume=1, provider=provider)
    snapshot = _tool_data(TraderReadOnlyTools(provider, _status, proposals=application.state.proposals),
                          "get_market_snapshot", {"symbol": "SHFE.rb2701"})
    assert ticket["fee"] == 6.14      # 万分之2 × 3071 × 10
    assert snapshot["contract"]["commissionPerLot"] == ticket["commissionPerLot"] == 6.14
    assert snapshot["contract"]["commissionSource"] == "custom"


def test_snapshot_without_fee_service_marks_the_market_source_value() -> None:
    """没有费率服务（无 proposals）时不冒充交易所标准：保留行情源值并标 market_source。"""
    tools = TraderReadOnlyTools(_RbProvider(), _status)
    snapshot = _tool_data(tools, "get_market_snapshot", {"symbol": "SHFE.rb2701"})
    assert snapshot["contract"]["commissionPerLot"] == 0.3116
    assert snapshot["contract"]["commissionSource"] == "market_source"


def test_estimate_order_tool_is_read_only(application: FastAPI) -> None:
    tools = TraderReadOnlyTools(
        FakeReadOnlyDataProvider(), _status, proposals=application.state.proposals,
    )
    names = {item["name"] for item in tools.definitions() if item.get("type") == "function"}
    assert "estimate_order" in names
    response, event = tools.call(
        tool="estimate_order",
        arguments={"symbol": "AU2612", "side": "buy", "offset": "open", "volume": 2},
        turn_id="turn-estimate", mode="local", user_id=USER_ID,
    )
    payload = json.loads(response["contentItems"][0]["text"])
    assert response["success"] is True
    assert event is None
    assert payload["readOnly"] is True
    assert payload["data"]["margin"] == 164_700.0
    assert payload["data"]["maxOpenVolume"] == 10
    assert payload["data"]["basis"]


async def test_capability_catalog_lists_the_estimate_route(client: AsyncClient) -> None:
    manifest = await client.get("/api/agent/capabilities")
    operations = {item["operation"] for item in manifest.json()["operations"]}
    assert "GET /api/simulation/order-estimate" in operations
    # The fake adapter carries no proposal tools in its catalog (neither does
    # propose_order appear here); where the proposal tools are listed,
    # the estimate travels with them — pinned by the tool test above.
    if "tool:propose_order" in operations:
        assert "tool:estimate_order" in operations


async def test_opening_card_carries_the_money_estimate(application: FastAPI, client: AsyncClient) -> None:
    created = await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})
    assert created.status_code == 201
    session_id = created.json()["sessionId"]
    card = application.state.proposals.create_trade(
        session_id, "turn-money", symbol="AU2612", side="buy_open", volume=2,
        price_intent="passive", reason="test candidate",
    )
    estimate = card["estimate"]
    assert estimate["margin"] == 164_700.0
    assert estimate["maxOpenVolume"] == 10
    assert estimate["insufficient"] is False
    assert estimate["missing"] == []


async def test_account_summary_carries_available_margin_and_risk_ratio(client: AsyncClient) -> None:
    response = await client.get("/api/account/summary")
    assert response.status_code == 200
    data = response.json()
    assert data["available"] == 835_570
    assert data["margin"] == 191_010
    assert data["riskRatio"] == pytest.approx(18.6)
    summary = AccountSummary.model_validate(fake_account_summary("turn-contract"))
    assert summary.available == 835_570
    assert summary.margin == 191_010
    assert summary.riskRatio == 18.6


def test_market_snapshot_contract_carries_per_lot_amounts() -> None:
    market = MarketSnapshot.model_validate(fake_market_snapshot("turn-contract", "AU2612"))
    assert market.marginPerLot == 82_350.0
    assert market.commissionPerLot == 24.71
    quote = FakeReadOnlyDataProvider().market_workbench("AU2612", 60)["quote"]
    assert quote["marginPerLot"] == 82_350.0
    assert quote["commissionPerLot"] == 24.71


class _FixedFundsProvider(FakeReadOnlyDataProvider):
    """资金 100、每手保证金 10、每手手续费 20：可开手数口径的回归夹具。"""

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        summary = dict(fake_account_summary(turn_id))
        summary["available"] = 100
        return summary

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        snapshot = dict(fake_market_snapshot(turn_id, symbol))
        snapshot["marginPerLot"] = 10.0
        snapshot["commissionPerLot"] = 20.0
        return snapshot


@pytest.mark.no_exchange_fees
def test_max_open_volume_is_independent_of_the_input_volume(application: FastAPI) -> None:
    estimates = [
        application.state.proposals.order_estimate(
            user_id=USER_ID, symbol="AU2612", side="buy", offset="open",
            volume=volume, provider=_FixedFundsProvider(),
        )
        for volume in (1, 4, 5)
    ]
    # 100 ÷ (10 保证金 + 20 手续费) = 3.33 → 3 手，与问的是几手无关；不足按同一口径判。
    assert {item["maxOpenVolume"] for item in estimates} == {3}
    assert [item["insufficient"] for item in estimates] == [False, True, True]
    assert [item["fee"] for item in estimates] == [20.0, 80.0, 100.0]


class _NoFeeProvider(FakeReadOnlyDataProvider):
    """行情没给出每手手续费：未知不能当 0 算进可开手数。"""

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        snapshot = dict(fake_market_snapshot(turn_id, symbol))
        snapshot.pop("commissionPerLot")
        return snapshot


@pytest.mark.no_exchange_fees
def test_unknown_fee_leaves_max_open_volume_null_with_reason(application: FastAPI) -> None:
    data = application.state.proposals.order_estimate(
        user_id=USER_ID, symbol="AU2612", side="buy", offset="open",
        volume=1, provider=_NoFeeProvider(),
    )
    assert data["fee"] is None
    assert data["maxOpenVolume"] is None
    assert data["insufficient"] is False
    assert "commission_per_lot_unavailable" in data["missing"]


class _SplitTodayYesterdayProvider(FakeReadOnlyDataProvider):
    """au2612 今 1 昨 1（上期所分今昨），另持大商所 m2612 今 3（指令不分今昨，费用先昨后今拆）。"""

    def positions(self) -> list[dict[str, Any]]:
        return [
            {"symbol": "SHFE.au2612", "exchangeId": "SHFE", "instrumentId": "au2612",
             "direction": "long", "volume": 2, "unrealizedPnl": 2670,
             "todayVolume": 1, "historicalVolume": 1, "frozenToday": 0, "frozenHistorical": 0,
             "margin": 164_700.0},
            {"symbol": "DCE.m2612", "exchangeId": "DCE", "instrumentId": "m2612",
             "direction": "long", "volume": 3, "unrealizedPnl": -120,
             "todayVolume": 3, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0,
             "margin": 9_000.0},
        ]


def _custom_per_lot_rates(application: FastAPI) -> None:
    application.state.proposals.fee_settings.save(USER_ID, {
        "source": "custom",
        "default": {"mode": "per_lot", "open": 0.0, "closeYesterday": 1.0, "closeToday": 100.0},
    })


def test_close_fee_splits_today_and_historical_by_preference(application: FastAPI) -> None:
    _custom_per_lot_rates(application)
    data = application.state.proposals.order_estimate(
        user_id=USER_ID, symbol="SHFE.au2612", side="sell", offset="close",
        volume=2, provider=_SplitTodayYesterdayProvider(),
    )
    # 默认偏好 auto、跨今昨两侧 → 今先昨后：1×100 + 1×1 = 101，拆分明细同口径。
    assert data["fee"] == 101.0
    assert data["feeBreakdown"] == {"today": {"volume": 1, "fee": 100.0},
                                    "historical": {"volume": 1, "fee": 1.0}}
    assert data["releasedMargin"] == 164_700.0
    assert data["missing"] == []

    application.state.store.update_preferences(USER_ID, {"trade": {"closePreference": "today"}})
    preferred = application.state.proposals.order_estimate(
        user_id=USER_ID, symbol="SHFE.au2612", side="sell", offset="close",
        volume=2, provider=_SplitTodayYesterdayProvider(),
    )
    # 显式“先平今”偏好：同一笔算下来仍是今 1 + 昨 1，只是按用户顺序取量。
    assert preferred["fee"] == 101.0
    assert preferred["feeBreakdown"]["today"] == {"volume": 1, "fee": 100.0}


def test_close_fee_honors_the_position_bucket_parameter(application: FastAPI) -> None:
    _custom_per_lot_rates(application)
    yesterday_only = application.state.proposals.order_estimate(
        user_id=USER_ID, symbol="SHFE.au2612", side="sell", offset="close",
        volume=1, position_bucket="historical", provider=_SplitTodayYesterdayProvider(),
    )
    assert yesterday_only["fee"] == 1.0
    assert yesterday_only["feeBreakdown"] == {"today": {"volume": 0, "fee": 0.0},
                                              "historical": {"volume": 1, "fee": 1.0}}
    today_only = application.state.proposals.order_estimate(
        user_id=USER_ID, symbol="SHFE.au2612", side="sell", offset="close",
        volume=1, position_bucket="today", provider=_SplitTodayYesterdayProvider(),
    )
    assert today_only["fee"] == 100.0
    assert today_only["feeBreakdown"] == {"today": {"volume": 1, "fee": 100.0},
                                          "historical": {"volume": 0, "fee": 0.0}}


def test_close_fee_on_other_exchanges_splits_yesterday_first(application: FastAPI) -> None:
    """#711：非上期所的平仓指令不分今昨，但费用按“先昨后今”拆，与本站模拟盘实扣
    paper_book._fee_rate_commission 同口径。m2612 今 3 昨 0 → 整笔收平今（10 × 平昨），
    修复前整笔按平昨、还少一个数量级。"""
    data = application.state.proposals.order_estimate(
        user_id=USER_ID, symbol="DCE.m2612", side="sell", offset="close",
        volume=2, provider=_SplitTodayYesterdayProvider(),
    )
    assert data["fee"] == 494.2  # 今仓 2 手 × 247.1，不是按平昨的 2 × 24.71
    assert data["feeBreakdown"] == {"today": {"volume": 2, "fee": 494.2},
                                    "historical": {"volume": 0, "fee": 0.0}}
    assert data["missing"] == []


async def test_close_estimate_accepts_position_bucket_over_http(client: AsyncClient) -> None:
    response = await client.get("/api/simulation/order-estimate", params={
        "symbol": "SHFE.au2612", "side": "sell", "offset": "close", "volume": 2,
    })
    assert response.status_code == 200
    data = response.json()
    # 假数据 au2612 今 2 昨 0：auto 偏好下整笔落在今仓，HTTP 入口同样给拆分。
    assert data["fee"] == 49.42
    assert data["feeBreakdown"]["today"] == {"volume": 2, "fee": 49.42}
    assert data["feeBreakdown"]["historical"] == {"volume": 0, "fee": 0.0}
    bucketed = await client.get("/api/simulation/order-estimate", params={
        "symbol": "SHFE.au2612", "side": "sell", "offset": "close", "volume": 2,
        "positionBucket": "historical",
    })
    assert bucketed.status_code == 200
    assert bucketed.json()["feeBreakdown"]["historical"]["volume"] == 0


@pytest.fixture
def split_application(tmp_path: Path):
    application = create_app(
        tmp_path / "split.sqlite3", local_token=TOKEN, agent_mode="fake",
        data_provider=_SplitTodayYesterdayProvider())
    yield application
    application.state.store.close()


@pytest.fixture
async def split_client(split_application: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=split_application), base_url="http://test",
        headers={"Origin": ORIGIN},
    ) as test_client:
        response = await test_client.post("/api/auth/local", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200
        yield test_client


async def test_close_estimate_position_bucket_wire_name_and_conflict_over_http(
        split_application: FastAPI, split_client: AsyncClient) -> None:
    """#454：HTTP 的平仓侧参数名是 positionBucket（与工具、文档同一写法；position_bucket
    下划线写法兼容旧调用方），同值只算一个；两写法给不同值明确 422，绝不悄悄挑一个；
    非法值 422——修复前 camelCase 写法被悄悄忽略。工具与 HTTP 同一参数同一结果。"""
    _custom_per_lot_rates(split_application)
    base = {"symbol": "SHFE.au2612", "side": "sell", "offset": "close", "volume": 2}
    bucketed = await split_client.get("/api/simulation/order-estimate",
                                      params={**base, "positionBucket": "historical"})
    assert bucketed.status_code == 200
    assert bucketed.json()["feeBreakdown"]["historical"] == {"volume": 1, "fee": 1.0}
    # 下划线写法同样生效（同一参数，不是第二个开关）。
    snake = await split_client.get("/api/simulation/order-estimate",
                                   params={**base, "position_bucket": "today"})
    assert snake.status_code == 200
    assert snake.json()["feeBreakdown"]["today"] == {"volume": 1, "fee": 100.0}
    # 两写法同值：只算一个。
    both = await split_client.get("/api/simulation/order-estimate",
                                  params={**base, "positionBucket": "today", "position_bucket": "today"})
    assert both.status_code == 200
    assert both.json()["feeBreakdown"]["today"] == {"volume": 1, "fee": 100.0}
    # 两写法不同值：明确 422。
    conflict = await split_client.get(
        "/api/simulation/order-estimate",
        params={**base, "positionBucket": "today", "position_bucket": "historical"})
    assert conflict.status_code == 422
    assert conflict.json()["detail"]["code"] == "position_bucket_conflict"
    # 非法值：422。
    invalid = await split_client.get("/api/simulation/order-estimate",
                                     params={**base, "positionBucket": "yesterday"})
    assert invalid.status_code == 422

    # 工具与 HTTP 用的是同一个参数、同一份结果。
    tools = TraderReadOnlyTools(
        _SplitTodayYesterdayProvider(), _status, proposals=split_application.state.proposals)
    response, event = tools.call(
        tool="estimate_order",
        arguments={"symbol": "SHFE.au2612", "side": "sell", "offset": "close", "volume": 2,
                   "positionBucket": "historical"},
        turn_id="turn-estimate-bucket", mode="local", user_id=USER_ID,
    )
    payload = json.loads(response["contentItems"][0]["text"])
    assert event is None
    assert payload["data"]["feeBreakdown"] == bucketed.json()["feeBreakdown"]
    assert payload["data"]["fee"] == bucketed.json()["fee"]


def test_close_estimate_allocates_only_closable_volume_with_notes(application: FastAPI) -> None:
    """#455：手续费与释放保证金共用同一份“这次实际可平”的手数分配（今昨各扣冻结）——
    请求超出可平量按可平量估算并在 missing 写明，不拒绝；显式侧、auto、跨侧同口径。"""
    _custom_per_lot_rates(application)
    estimate = application.state.proposals.order_estimate
    # issue 现场：au2612 今 1 昨 1、合计保证金 164700，要平昨仓 2 手 → 只有 1 手可平。
    historical = estimate(user_id=USER_ID, symbol="SHFE.au2612", side="sell", offset="close",
                          volume=2, position_bucket="historical", provider=_SplitTodayYesterdayProvider())
    assert historical["fee"] == 1.0
    assert historical["feeBreakdown"] == {"today": {"volume": 0, "fee": 0.0},
                                          "historical": {"volume": 1, "fee": 1.0}}
    assert historical["releasedMargin"] == 82_350.0  # 只释放那 1 手的，不按请求的 2 手
    assert historical["missing"] == ["close_volume_exceeds_closable:昨仓只有 1 手可平，按 1 手估算"]

    today = estimate(user_id=USER_ID, symbol="SHFE.au2612", side="sell", offset="close",
                     volume=2, position_bucket="today", provider=_SplitTodayYesterdayProvider())
    assert today["fee"] == 100.0
    assert today["releasedMargin"] == 82_350.0
    assert today["missing"] == ["close_volume_exceeds_closable:今仓只有 1 手可平，按 1 手估算"]

    auto = estimate(user_id=USER_ID, symbol="SHFE.au2612", side="sell", offset="close",
                    volume=5, provider=_SplitTodayYesterdayProvider())
    assert auto["fee"] == 101.0
    assert auto["releasedMargin"] == 164_700.0
    assert auto["missing"] == [
        "close_volume_exceeds_closable:今仓 1 手、昨仓 1 手可平，合计只有 2 手，按 2 手估算"]


class _FrozenTodayProvider(_SplitTodayYesterdayProvider):
    """au2612 今仓 1 手全冻结、昨仓 1 手：今侧实际可平 0。"""

    def positions(self) -> list[dict[str, Any]]:
        positions = super().positions()
        return [{**positions[0], "frozenToday": 1}, positions[1]]


def test_close_estimate_subtracts_frozen_and_caps_other_exchanges(application: FastAPI) -> None:
    """#455 的边界：冻结先扣、空侧给 0 手估算、不分今昨的交易所按总持仓扣冻结。"""
    estimate = application.state.proposals.order_estimate
    frozen = estimate(user_id=USER_ID, symbol="SHFE.au2612", side="sell", offset="close",
                      volume=2, provider=_FrozenTodayProvider())
    assert frozen["fee"] == 24.71  # 只有昨仓 1 手可平
    assert frozen["feeBreakdown"]["historical"] == {"volume": 1, "fee": 24.71}
    assert frozen["releasedMargin"] == 82_350.0
    assert frozen["missing"] == [
        "close_volume_exceeds_closable:今仓 0 手、昨仓 1 手可平，合计只有 1 手，按 1 手估算"]

    application.state.store.update_preferences(USER_ID, {"trade": {"closePreference": "today"}})
    explicit_today = estimate(user_id=USER_ID, symbol="SHFE.au2612", side="sell", offset="close",
                              volume=2, position_bucket="today", provider=_FrozenTodayProvider())
    assert explicit_today["fee"] == 0.0
    assert explicit_today["feeBreakdown"] == {"today": {"volume": 0, "fee": 0.0},
                                              "historical": {"volume": 0, "fee": 0.0}}
    assert explicit_today["releasedMargin"] == 0.0
    assert explicit_today["missing"] == ["close_volume_exceeds_closable:今仓只有 0 手可平，按 0 手估算"]

    dce = estimate(user_id=USER_ID, symbol="DCE.m2612", side="sell", offset="close",
                   volume=5, provider=_SplitTodayYesterdayProvider())
    assert dce["fee"] == 741.3  # 今仓 3 手 × 247.1（平今 = 10 × 平昨）
    assert dce["feeBreakdown"] == {"today": {"volume": 3, "fee": 741.3},
                                   "historical": {"volume": 0, "fee": 0.0}}
    assert dce["releasedMargin"] == 9_000.0
    assert dce["missing"] == ["close_volume_exceeds_closable:持仓只有 3 手可平，按 3 手估算"]


class _FrozenOtherExchangeProvider(_SplitTodayYesterdayProvider):
    """大商所持仓 3 手、挂着的平仓单冻住 1 手今仓：可平量与费用拆分同口径扣冻结。"""

    def positions(self) -> list[dict[str, Any]]:
        positions = super().positions()
        return [positions[0], {**positions[1], "frozenToday": 1}]


def test_close_estimate_on_other_exchanges_subtracts_frozen_lots(application: FastAPI) -> None:
    """#518：非上期所的交易所，估算的可平量=总持仓−冻结（与手续费同口径，#711 起同一份
    “先昨后今”拆分）。修复前冻结不扣、按全仓放行；今昨可平双 0 的全冻结还被当成“字段
    缺失”按全仓估算。"""
    estimate = application.state.proposals.order_estimate
    partly = estimate(user_id=USER_ID, symbol="DCE.m2612", side="sell", offset="close",
                      volume=3, provider=_FrozenOtherExchangeProvider())
    assert partly["fee"] == 494.2  # 今仓可平 2 手 × 247.1：手续费与可平量同口径
    assert partly["feeBreakdown"]["today"] == {"volume": 2, "fee": 494.2}
    assert partly["releasedMargin"] == 6_000.0
    assert partly["missing"] == ["close_volume_exceeds_closable:持仓只有 2 手可平，按 2 手估算"]

    class _AllFrozen(_FrozenOtherExchangeProvider):
        def positions(self) -> list[dict[str, Any]]:
            positions = super().positions()
            return [positions[0], {**positions[1], "frozenToday": 3}]

    nothing = estimate(user_id=USER_ID, symbol="DCE.m2612", side="sell", offset="close",
                       volume=1, provider=_AllFrozen())
    assert nothing["fee"] == 0.0 and nothing["releasedMargin"] == 0.0
    assert nothing["missing"] == ["close_volume_exceeds_closable:持仓只有 0 手可平，按 0 手估算"]


class _CffexMixedProvider(FakeReadOnlyDataProvider):
    """中金所 IF2612 多头今 1 昨 3：非上期所的今昨混合、全昨、昨仓有冻结各用例。"""

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        snapshot = dict(fake_market_snapshot(turn_id, symbol))
        if str(symbol).upper().endswith("IF2612"):
            snapshot.update({"price": 4000.0, "volumeMultiple": 300.0,
                             "marginPerLot": 600_000.0, "commissionPerLot": 247.1})
        return snapshot

    def positions(self) -> list[dict[str, Any]]:
        return [{"symbol": "CFFEX.IF2612", "exchangeId": "CFFEX", "instrumentId": "IF2612",
                 "direction": "long", "volume": 4, "unrealizedPnl": 1200,
                 "todayVolume": 1, "historicalVolume": 3, "frozenToday": 0, "frozenHistorical": 0,
                 "margin": 2_400_000.0}]


def test_close_fee_on_cffex_covers_all_historical_mixed_and_frozen(application: FastAPI) -> None:
    """#711 中金所（股指平今远高于平昨的典型场景）：“先昨后今”分别按平昨、平今费率估算。"""
    _custom_per_lot_rates(application)  # 每手：平昨 1.0 / 平今 100.0
    estimate = application.state.proposals.order_estimate

    # 全昨仓：昨 3 可平 → 整笔收平昨。
    historical = estimate(user_id=USER_ID, symbol="CFFEX.IF2612", side="sell", offset="close",
                          volume=3, provider=_CffexMixedProvider())
    assert historical["fee"] == 3.0
    assert historical["feeBreakdown"] == {"today": {"volume": 0, "fee": 0.0},
                                          "historical": {"volume": 3, "fee": 3.0}}
    assert historical["missing"] == []

    # 今昨混合：先昨 3 × 1.0，再今 1 × 100.0。
    mixed = estimate(user_id=USER_ID, symbol="CFFEX.IF2612", side="sell", offset="close",
                     volume=4, provider=_CffexMixedProvider())
    assert mixed["fee"] == 103.0
    assert mixed["feeBreakdown"] == {"today": {"volume": 1, "fee": 100.0},
                                     "historical": {"volume": 3, "fee": 3.0}}

    class _FrozenHistorical(_CffexMixedProvider):
        def positions(self) -> list[dict[str, Any]]:
            return [{**position, "frozenHistorical": 2} for position in super().positions()]

    # 昨仓有冻结：昨侧只剩 1 手可平 → 昨 1 + 今 1，超出部分按可平量估算并写明。
    frozen = estimate(user_id=USER_ID, symbol="CFFEX.IF2612", side="sell", offset="close",
                      volume=4, provider=_FrozenHistorical())
    assert frozen["fee"] == 101.0
    assert frozen["feeBreakdown"] == {"today": {"volume": 1, "fee": 100.0},
                                      "historical": {"volume": 1, "fee": 1.0}}
    assert frozen["releasedMargin"] == 1_200_000.0
    assert frozen["missing"] == ["close_volume_exceeds_closable:持仓只有 2 手可平，按 2 手估算"]

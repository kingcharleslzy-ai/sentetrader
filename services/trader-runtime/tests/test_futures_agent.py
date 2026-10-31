from __future__ import annotations

import json

from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.trader_tools import TraderReadOnlyTools


def _status(mode: str) -> dict:
    provider = FakeReadOnlyDataProvider()
    return {
        "mode": mode,
        "adapter": "native-agent",
        "runtimeConnected": True,
        "agentConnected": True,
        "connected": True,
        "version": "1.3.1",
        **provider.runtime_status(),
    }


def _tool_payload(response: dict) -> dict:
    return json.loads(response["contentItems"][0]["text"])


def test_market_analysis_is_deterministic_and_never_enables_live_trading() -> None:
    tools = TraderReadOnlyTools(FakeReadOnlyDataProvider(), _status)
    response, event = tools.call(
        tool="get_market_analysis",
        arguments={
            "symbol": "SHFE.au2612", "durationSeconds": 300, "lookbackBars": 40,
        },
        turn_id="turn-analysis",
        mode="local",
    )
    payload = _tool_payload(response)
    assert response["success"] is True
    assert payload["data"]["barCount"] == 40
    assert payload["data"]["trend"] == "up"
    assert payload["data"]["openInterestChange"] == 78
    assert payload["data"]["liveTrading"] == "disabled"
    assert payload["data"]["method"] == "deterministic_ohlcv_oi_summary_v1"
    assert event is None


def test_decision_context_joins_verified_futures_facts_and_keeps_execution_closed() -> None:
    tools = TraderReadOnlyTools(FakeReadOnlyDataProvider(), _status)
    response, event = tools.call(
        tool="get_futures_decision_context",
        arguments={
            "symbol": "SHFE.au2612", "durationSeconds": 300, "lookbackBars": 40,
        },
        turn_id="turn-decision-context",
        mode="local",
    )
    payload = _tool_payload(response)
    context = payload["data"]
    assert response["success"] is True
    assert context["source"] == "runtime_verified_futures_context_v1"
    assert context["symbol"] == "SHFE.au2612"
    assert context["analysisReady"] is True
    assert context["proposalReady"] is True
    assert context["simulationGatewayReady"] is False
    assert context["simulationGatewayReadinessReasons"] == [
        "simulation_gateway_disabled"
    ]
    assert context["userFacing"] == {
        "analysisStatus": "当前信息足够分析",
        "candidateStatus": "可以生成待确认方案",
        "simulationStatus": "模拟交易通道当前不可用",
        "analysisIssues": [],
        "candidateIssues": [],
        "simulationIssues": ["模拟交易通道未开启"],
        "safetyBoundary": "实盘功能未开启",
    }
    assert context["instrument"]["expireRestDays"] == 90
    assert context["analysis"]["method"] == "deterministic_ohlcv_oi_summary_v1"
    assert context["account"]["accountName"] == "假数据模拟账户"
    assert context["positions"]["positions"][0]["symbol"] == "SHFE.au2612"
    assert context["activity"]["activeOrderCount"] == 0
    assert context["candidateOnlyDefault"] is True
    assert context["liveTrading"] == "disabled"
    assert event is None


def test_decision_context_separates_market_analysis_from_account_readiness() -> None:
    def stale_account_status(mode: str) -> dict:
        payload = _status(mode)
        payload["accountStatus"] = "stale"
        return payload

    tools = TraderReadOnlyTools(FakeReadOnlyDataProvider(), stale_account_status)
    response, _ = tools.call(
        tool="get_futures_decision_context",
        arguments={"symbol": "SHFE.au2612"},
        turn_id="turn-stale-decision-context",
        mode="local",
    )
    context = _tool_payload(response)["data"]
    assert response["success"] is True
    assert context["analysisReady"] is True
    assert context["proposalReady"] is False
    assert context["analysisReadinessReasons"] == []
    assert context["proposalReadinessReasons"] == ["account_not_fresh"]
    assert context["userFacing"]["candidateIssues"] == ["账户数据不是最新状态"]


def test_decision_context_can_analyze_market_while_account_is_unavailable() -> None:
    def unavailable_account_status(mode: str) -> dict:
        payload = _status(mode)
        payload["accountStatus"] = "unavailable"
        return payload

    tools = TraderReadOnlyTools(FakeReadOnlyDataProvider(), unavailable_account_status)
    response, _ = tools.call(
        tool="get_futures_decision_context",
        arguments={"symbol": "SHFE.au2612"},
        turn_id="turn-no-account-decision-context",
        mode="local",
    )
    context = _tool_payload(response)["data"]
    assert response["success"] is True
    assert context["analysisReady"] is True
    assert context["proposalReady"] is False
    assert context["account"] is None
    assert context["positions"] == {"dataStatus": "unavailable", "positions": []}
    assert context["activity"]["ordersInitialized"] is False
    assert context["proposalReadinessReasons"] == [
        "account_not_fresh", "activity_not_fresh", "orders_not_initialized"
    ]
    assert context["userFacing"]["candidateIssues"] == [
        "账户数据不是最新状态", "委托和成交数据不是最新状态", "委托数据尚未读取完成"
    ]


def test_analysis_arguments_are_bounded() -> None:
    tools = TraderReadOnlyTools(FakeReadOnlyDataProvider(), _status)
    invalid_analysis, _ = tools.call(
        tool="get_market_analysis",
        arguments={"symbol": "SHFE.au2612", "durationSeconds": 7, "lookbackBars": 500},
        turn_id="turn-invalid-analysis", mode="local",
    )
    assert invalid_analysis["success"] is False

    invalid_context, _ = tools.call(
        tool="get_futures_decision_context",
        arguments={"symbol": "SHFE.au2612", "lookbackBars": 121},
        turn_id="turn-invalid-context", mode="local",
    )
    assert invalid_context["success"] is False

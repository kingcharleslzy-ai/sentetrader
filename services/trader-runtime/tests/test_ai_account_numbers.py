"""#794：AI 报账户数字与保护单状态都来自工具层的权威字段，不靠模型自己从成交加总或猜保护是否还在。
工具层用固定的假账户/假保护单直接调用，不经过真实模型。"""
from __future__ import annotations

import json
from typing import Any

from trader_runtime.contracts import AccountSummary
from trader_runtime.conversation_cards import protection_state
from trader_runtime.data_providers import FakeReadOnlyDataProvider, fake_account_summary
from trader_runtime.trader_tools import TraderReadOnlyTools

USER_ID = "local-user"


def _status(mode: str) -> dict[str, Any]:
    return {"mode": mode, **FakeReadOnlyDataProvider().runtime_status()}


class _OvernightAccountProvider(FakeReadOnlyDataProvider):
    """有昨仓的一天：持仓盈亏（对昨结算价）≠ 浮动盈亏（对开仓价），手续费不为 0。
    “今日 − 浮动”推不出平仓盈亏——#794 里 −640 的推算就是这样得来的。"""

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        summary = dict(fake_account_summary(turn_id))
        summary.update({"closeProfit": 860.0, "positionProfit": 1_620.0, "floatProfit": 3_067.0,
                        "commission": 53.0, "todayNetPnl": 2_427.0, "todayPnl": 2_427.0})
        return summary


def _call(tools: TraderReadOnlyTools, tool: str) -> tuple[dict[str, Any], Any]:
    response, event = tools.call(tool=tool, arguments={}, turn_id="turn-794", mode="local", user_id=USER_ID)
    assert response["success"] is True
    return json.loads(response["contentItems"][0]["text"])["data"], event


def test_account_summary_tool_returns_the_authoritative_pnl_fields_with_their_basis() -> None:
    provider = _OvernightAccountProvider()
    tools = TraderReadOnlyTools(provider, _status)
    data, event = _call(tools, "get_account_summary")
    summary = AccountSummary.model_validate(provider.account_summary("turn-794")).model_dump()
    # 每个报数字段都是账户摘要原值，并带口径说明。
    for key in ("closeProfit", "positionProfit", "floatProfit", "commission", "todayNetPnl"):
        assert data["pnlFields"][key]["value"] == summary[key] == data[key]
        assert data["pnlFields"][key]["meaning"]
    assert "不含手续费" in data["pnlFields"]["closeProfit"]["meaning"]
    assert data["pnlFields"]["todayNetPnl"]["value"] == round(
        summary["closeProfit"] + summary["positionProfit"] - summary["commission"], 2)
    # 说明里明确：不从成交/交易日志加总，不用 今日 − 浮动 推算。
    assert "get_trade_journal" in data["pnlRule"] and "floatProfit" in data["pnlRule"]
    # 卡片仍是原账户摘要，不带给模型的说明字段。
    assert event[0] == "card.account" and "pnlFields" not in event[1]
    description = next(item["description"] for item in tools.definitions()
                       if item["name"] == "get_account_summary")
    assert "pnlFields" in description


def test_trade_journal_tool_says_it_is_not_the_account_close_profit() -> None:
    tools = TraderReadOnlyTools(FakeReadOnlyDataProvider(), _status)
    tools.app_capabilities = object()  # journal tools are listed with the app catalog
    descriptions = {item["name"]: item["description"] for item in tools.definitions()}
    assert "pnlFields" in descriptions["get_trade_journal"]


def _stop(stop_id: str, status: str, trigger: float, *, remaining: int | None = 1,
          last_error: str | None = None, updated: str = "2026-10-12T04:50:00Z") -> dict[str, Any]:
    """`AutonomousSimulationService._stop_public` 的形状。"""
    return {"stopId": stop_id, "authorizationId": "auth", "sessionId": "s", "symbol": "SHFE.rb2601",
            "positionDirection": "long", "positionBucket": "all", "volume": 1, "triggerPrice": trigger,
            "takeProfitPrice": None, "remainingVolume": remaining, "strategyId": None, "source": "manual",
            "priceIntent": "aggressive", "status": status, "closeProposalId": None,
            "closeClientOrderId": None, "lastError": last_error, "triggeredAt": None,
            "createdAt": updated, "updatedAt": updated, "liveTrading": "disabled"}


class _Autonomy:
    enabled = False  # 只读：不挂执行类工具，本测试只看 list_stops

    def __init__(self, stops: list[dict[str, Any]]) -> None:
        self.stops = stops

    def list_stops(self, *, user_id: str) -> list[dict[str, Any]]:
        assert user_id == USER_ID
        return list(self.stops)


def test_protection_state_marks_only_armed_with_volume_as_effective() -> None:
    assert protection_state(_stop("a", "ARMED", 3050))["effective"] is True
    cancelled = protection_state(_stop("b", "CANCELLED", 2499, remaining=0, last_error="replaced_by_card"))
    assert cancelled == {"effective": False, "statusLabel": "已撤销",
                         "statusDetail": "改挂了新的保护，这一条已撤销。"}
    for status in ("TRIGGERING", "SUBMITTED", "PARTIAL", "EXECUTED", "FAILED", "UNKNOWN"):
        assert protection_state(_stop("c", status, 2499))["effective"] is False
    spent = protection_state(_stop("d", "ARMED", 2499, remaining=0))
    assert spent["effective"] is False and spent["statusLabel"] == "已失效"


def test_account_activity_tool_returns_protection_status_and_never_a_stale_stop_as_effective() -> None:
    """#794 P3：用户问“保护单 2499 是不是设太低了”，2499 那条已被撤销——工具返回里它标明
    effective=false 与原因，仍挂着的只有 3050。"""
    stops = [_stop("live", "ARMED", 3050, updated="2026-10-12T04:55:00Z"),
             _stop("old", "CANCELLED", 2499, remaining=0, last_error="replaced_by_card")]
    stops += [_stop(f"done-{index}", "EXECUTED", 2400 + index, remaining=0) for index in range(12)]
    tools = TraderReadOnlyTools(FakeReadOnlyDataProvider(), _status, autonomy=_Autonomy(stops))
    data, _ = _call(tools, "get_account_activity")
    effective = [row for row in data["protections"] if row["effective"]]
    assert [row["triggerPrice"] for row in effective] == [3050]
    stale = next(row for row in data["protections"] if row["triggerPrice"] == 2499)
    assert stale["effective"] is False and stale["status"] == "CANCELLED"
    assert stale["statusLabel"] == "已撤销" and "撤销" in stale["statusDetail"]
    # 已失效的只留最近几条，有效的排在前面。
    assert data["protections"][0]["stopId"] == "live"
    assert len(data["protections"]) == 1 + 10
    assert "effective" in data["protectionRule"] and "已经不在" in data["protectionRule"]
    # 委托与成交事实照旧。
    assert isinstance(data["orders"], list) and isinstance(data["trades"], list)
    description = next(item["description"] for item in tools.definitions()
                       if item["name"] == "get_account_activity")
    assert "protections" in description


def test_account_activity_without_autonomy_keeps_the_old_shape() -> None:
    data, _ = _call(TraderReadOnlyTools(FakeReadOnlyDataProvider(), _status), "get_account_activity")
    assert "protections" not in data


class _PaperModeProvider(FakeReadOnlyDataProvider):
    """本站模拟账户的提供方：账户在本站撮合，行情是真实快期行情（PaperProvider.mode == "paper"）。"""

    mode = "paper"


class _TqkqModeProvider(FakeReadOnlyDataProvider):
    mode = "tqkq-readonly"


def _market_description(provider: FakeReadOnlyDataProvider) -> str:
    tools = TraderReadOnlyTools(provider, _status)
    return next(item["description"] for item in tools.definitions() if item["name"] == "get_market_snapshot")


def test_paper_account_market_tool_is_not_described_as_synthetic() -> None:
    """#822：本站模拟账户的 get_market_snapshot 说明曾落到“合成假行情、不得描述为真实行情”，
    模型会对用户说行情是假数据。只有假数据提供方才用那句说明。"""
    for provider in (_PaperModeProvider(), _TqkqModeProvider()):
        description = _market_description(provider)
        assert "合成假行情" not in description and "TqSdk/TqKq" in description
    assert "合成假行情" in _market_description(FakeReadOnlyDataProvider())

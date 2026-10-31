"""Structured conversation cards (2026-10-04): execution cards read back from the account and
followed by the tracker; the AI execution tool hands its card to the conversation."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from trader_runtime.contracts import validate_event
from trader_runtime.conversation_cards import (
    ExecutionCardTracker,
    execution_card,
    execution_status,
    stop_card_update,
    stop_execution_card,
)
from trader_runtime.trader_tools import TraderReadOnlyTools

# card.execution's own contract literal set (executionCardSchema).
CONTRACT_STATUSES = {"submitted", "partial", "filled", "unfilled", "rejected", "unknown", "armed", "failed"}


def _order(status: str, *, filled: int = 0, client_order_id: str = "AIT-1") -> dict[str, Any]:
    return {"clientOrderId": client_order_id, "displayStatus": status, "filledVolume": filled,
            "request": {"symbol": "SHFE.rb2610", "direction": "buy", "offset": "OPEN", "volume": 2,
                        "limitPrice": 3200.0}}


def _envelope(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"schemaVersion": 1, "id": "e1", "sessionId": "s1", "kind": "artifact", "name": name,
            "timestamp": "2026-10-04T01:00:00Z", "payload": payload}


def test_every_read_back_state_maps_and_unknown_is_never_success() -> None:
    assert execution_status("已受理") == ("submitted", "已报")
    assert execution_status("部分成交") == ("partial", "部分成交")
    assert execution_status("已成交") == ("filled", "已成交")
    assert execution_status("已撤销")[0] == "unfilled"
    assert execution_status("已拒绝") == ("rejected", "已拒绝")
    for unknown in ("结果未知", "身份异常", "回报异常", "", "新状态"):
        assert execution_status(unknown) == ("unknown", "结果未知")
    card = execution_card(turn_id="t1", source="ai", source_label="AI 自主", order=_order("结果未知"),
                          reason="突破", name="螺纹钢2610")
    assert (card["status"], card["statusLabel"], card["sideLabel"], card["price"]) == ("unknown", "结果未知", "买开", "3200")
    assert "不会重发" in card["detail"] and card["cardId"] == "execution-AIT-1"
    validate_event(_envelope("card.execution", card))
    stop = stop_execution_card(turn_id="t2", stop={"stopId": "S1", "symbol": "SHFE.rb2610", "positionDirection": "long",
                                                    "volume": 1, "triggerPrice": 3150.0, "takeProfitPrice": None,
                                                    "status": "ARMED"})
    assert (stop["orderKind"], stop["status"], stop["price"]) == ("stop", "armed", "止损 3150")
    validate_event(_envelope("card.execution", stop))


class _Orders:
    def __init__(self, states: list[tuple[str, int]]) -> None:
        self.states = states
        self.queries: list[str] = []

    async def query(self, client_order_id: str, *, user_id: str) -> dict[str, Any]:
        self.queries.append(client_order_id)
        status, filled = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return _order(status, filled=filled, client_order_id=client_order_id)


class _Broker:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, session_id: str, *, kind: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.published.append((session_id, name, payload))
        return {}


@pytest.mark.asyncio
async def test_the_tracker_republishes_on_change_only_and_stops_at_a_terminal_state() -> None:
    broker = _Broker()
    tracker = ExecutionCardTracker(broker)
    orders = _Orders([("已受理", 0), ("部分成交", 1), ("部分成交", 1), ("已成交", 2)])
    card = execution_card(turn_id="t1", source="strategy", source_label="策略 · 均线", order=_order("已受理"))
    tracker.track("s1", card, simulation_orders=orders, user_id="u1")
    await tracker.tick()
    assert orders.queries == []  # the producer's own publish goes first
    tracker._cards[card["cardId"]].registered -= 10
    for _ in range(4):
        await tracker.tick()
    statuses = [(payload["status"], payload["filledVolume"]) for _, _, payload in broker.published]
    assert statuses == [("partial", 1), ("filled", 2)]
    assert all(name == "card.execution" and payload["cardId"] == card["cardId"] for _, name, payload in broker.published)
    assert tracker.tracking() == []  # filled: no more reads
    reads = len(orders.queries)
    await tracker.tick()
    assert len(orders.queries) == reads


@pytest.mark.asyncio
async def test_a_partial_fill_that_ended_says_so_and_ends_the_follow() -> None:
    """#932：部分成交后撤了剩余的单，卡面不再写“其余仍在挂单”，写已结束并停止跟读。"""
    broker = _Broker()
    tracker = ExecutionCardTracker(broker)
    orders = _Orders([("部分成交", 1), ("部分成交后已结束", 1)])
    card = execution_card(turn_id="t1", source="strategy", source_label="策略 · 均线", order=_order("已受理"))
    tracker.track("s1", card, simulation_orders=orders, user_id="u1")
    tracker._cards[card["cardId"]].registered -= 10
    await tracker.tick()
    await tracker.tick()
    [(_, _, live), (_, _, ended)] = broker.published
    assert (live["statusLabel"], live["detail"]) == ("部分成交", "已成交 1 手，其余仍在挂单。")
    assert (ended["status"], ended["statusLabel"]) == ("partial", "部分成交后已结束")
    assert ended["detail"] == "已成交 1 手，其余已撤或已失效，不再挂单。"
    assert tracker.tracking() == []
    already = execution_card(turn_id="t2", source="ai", source_label="AI 自主",
                             order=_order("部分成交后已结束", filled=1, client_order_id="AIT-2"))
    tracker.track("s1", already, simulation_orders=orders, user_id="u1")
    assert tracker.tracking() == []


@pytest.mark.asyncio
async def test_an_unknown_outcome_is_only_read_again_never_resent_and_terminal_cards_are_not_tracked() -> None:
    broker = _Broker()
    tracker = ExecutionCardTracker(broker)
    orders = _Orders([("结果未知", 0)])
    card = execution_card(turn_id="t1", source="ai", source_label="AI 自主", order=_order("已受理"))
    tracker.track("s1", card, simulation_orders=orders, user_id="u1")
    tracker._cards[card["cardId"]].registered -= 10
    await tracker.tick()
    await tracker.tick()
    assert [payload["statusLabel"] for _, _, payload in broker.published] == ["结果未知"]
    assert tracker.tracking() == [card["cardId"]]  # still read back, nothing else (the fake has only query)
    tracker.track("s1", execution_card(turn_id="t2", source="ai", source_label="AI 自主",
                                       order=_order("已成交", client_order_id="AIT-2")),
                  simulation_orders=orders, user_id="u1")
    assert "execution-AIT-2" not in tracker.tracking()


def test_the_ai_execution_tool_returns_its_card_and_hands_it_to_the_tracker() -> None:
    tracked: list[tuple[str, dict[str, Any]]] = []
    broker = SimpleNamespace(execution_cards=SimpleNamespace(
        track=lambda session_id, payload, **_: tracked.append((session_id, payload)),
        track_stop=lambda session_id, payload, **_: tracked.append((session_id, payload))))
    autonomy = SimpleNamespace(broker=broker, simulation_orders=object())
    provider = SimpleNamespace(market_snapshot=lambda turn_id, symbol: {"name": "螺纹钢2610"})
    tools = TraderReadOnlyTools.__new__(TraderReadOnlyTools)
    tools.autonomy = autonomy  # type: ignore[attr-defined]
    tools.provider = provider  # type: ignore[attr-defined]
    card = tools._execution_card("execute_simulation_trade", {"reason": "突破前高"},
                                 {"clientOrderId": "AIT-9", "simulationOrder": _order("已受理", client_order_id="AIT-9")},
                                 turn_id="turn-9", session_id="s9", user_id="u9")
    assert card is not None and card[0] == "card.execution"
    assert card[1]["turnId"] == "turn-9" and card[1]["source"] == "ai" and card[1]["name"] == "螺纹钢2610"
    assert card[1]["reason"] == "突破前高" and tracked == [("s9", card[1])]
    # 下保护 (2026-10-08): the arm turn's stop card joins the follow the same way — the tracker
    # reads its ai_stop_orders row, so a later auto-cancel republishes this same cardId.
    stop = tools._execution_card("set_simulation_stop_loss", {},
                                 {"stopId": "ST-9", "symbol": "SHFE.rb2610", "positionDirection": "long",
                                  "volume": 1, "triggerPrice": 3150.0, "takeProfitPrice": 3190.0,
                                  "status": "ARMED"},
                                 turn_id="turn-10", session_id="s9", user_id="u9")
    assert stop is not None and stop[1]["cardId"] == "execution-stop-ST-9" and stop[1]["orderKind"] == "stop"
    assert tracked[-1] == ("s9", stop[1])
    assert tools._execution_card("query_simulation_order", {}, {"displayStatus": "已成交"},
                                 turn_id="t", session_id="s", user_id="u") is None


def _stop_public(stop_id: str, status: str, **overrides: Any) -> dict[str, Any]:
    stop = {"stopId": stop_id, "symbol": "SHFE.rb2701", "positionDirection": "long", "positionBucket": "today",
            "volume": 1, "remainingVolume": 1, "triggerPrice": 3076.0, "takeProfitPrice": None,
            "source": "manual", "status": status}
    stop.update(overrides)
    return stop


async def test_a_protection_or_order_write_reports_what_is_left_read_after_it() -> None:
    """22:31 线上：撤掉止盈后 AI 说“只剩 3076 那条止损还生效”——那条 22:30:54 已触发平仓。撤保护/挂保护/撤单
    的回报带上写完之后现场重读的持仓、挂着的委托和仍生效的保护（accountAfter），已触发的、刚撤的都不在里面；
    读不到时明说不陈述。这份读回计入本轮证据（不再标“本轮未读取实时数据”）。"""
    from trader_runtime.native_agent import NativeAgentAdapter

    stops = {"stop-trig": _stop_public("stop-trig", "TRIGGERED"),
             "stop-take": _stop_public("stop-take", "ARMED", volume=2, remainingVolume=2, triggerPrice=None, takeProfitPrice=3078.0),
             "stop-hc-1": _stop_public("stop-hc-1", "ARMED", symbol="SHFE.hc2701", positionDirection="short", triggerPrice=3260.0)}

    def cancel_stop(*, user_id: str, stop_id: str) -> dict[str, Any]:
        stops[stop_id] = {**stops[stop_id], "status": "CANCELLED"}
        return stops[stop_id]

    autonomy = SimpleNamespace(cancel_stop=cancel_stop, list_stops=lambda *, user_id: list(stops.values()),
                               supports_mode=lambda mode: True, broker=None, enabled=True)
    activity = {"dataStatus": "normal", "orders": [
        {"symbol": "SHFE.rb2701", "direction": "BUY", "offset": "OPEN", "volume": 1, "volumeLeft": 1,
         "limitPrice": 3080.0, "status": "ALIVE"},
        {"symbol": "SHFE.rb2701", "direction": "BUY", "offset": "OPEN", "volume": 1, "volumeLeft": 0,
         "limitPrice": 3079.0, "status": "ALIVE"},  # filled, report not yet FINISHED: not working
        {"symbol": "SHFE.hc2701", "direction": "SELL", "offset": "OPEN", "volume": 1, "volumeLeft": 1,
         "limitPrice": 3270.0, "status": "ALIVE"},
        {"symbol": "SHFE.rb2701", "direction": "BUY", "offset": "OPEN", "volume": 1, "volumeLeft": 0,
         "limitPrice": 3050.0, "status": "FINISHED"}]}
    provider = SimpleNamespace(
        positions=lambda: [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 2, "todayVolume": 2, "historicalVolume": 0},
                           {"symbol": "SHFE.hc2701", "direction": "short", "volume": 1}],
        account_activity=lambda: activity)
    status = {"accountStatus": "normal"}
    tools = TraderReadOnlyTools(provider, lambda mode: status, autonomy=autonomy)  # type: ignore[arg-type]

    response, _ = await tools._call_async(tool="cancel_simulation_stop_loss", arguments={"stopId": "stop-take"},
                                          turn_id="t", mode="local", session_id="s", user_id="u")
    assert response["success"] is True
    data = json.loads(response["contentItems"][0]["text"])["data"]
    after = data["accountAfter"]
    assert data["status"] == "CANCELLED" and after["available"] is True and after["asOfLocal"]
    # The whole account (a remainder question may be about any contract); every effective protection,
    # neither the triggered stop nor the one just cancelled.
    assert [row["stopId"] for row in after["protections"]] == ["stop-hc-1"]
    assert [row["symbol"] for row in after["positions"]] == ["SHFE.rb2701", "SHFE.hc2701"]
    assert [order["limitPrice"] for order in after["workingOrders"]] == [3080.0, 3270.0]
    assert NativeAgentAdapter._account_after_read(response)

    # Stale or unreadable account data fails closed: no state, and the model is told not to state it.
    for stale in ({"accountStatus": "stale"}, {"accountStatus": "unavailable"}):
        status.clear()
        status.update(stale)
        stops["stop-hc-2"] = _stop_public("stop-hc-2", "ARMED", symbol="SHFE.hc2701")
        response, _ = await tools._call_async(tool="cancel_simulation_stop_loss", arguments={"stopId": "stop-hc-2"},
                                              turn_id="t", mode="local", session_id="s", user_id="u")
        after = json.loads(response["contentItems"][0]["text"])["data"]["accountAfter"]
        assert after["available"] is False and "不要陈述" in after["rule"] and "protections" not in after
        assert not NativeAgentAdapter._account_after_read(response)
    status.update({"accountStatus": "normal"})
    activity["dataStatus"] = "stale"
    assert (await tools.account_state("u", mode="local"))["available"] is False

    # Arming a stop: its card (and the stop tracker the card registers) comes only after the awaited
    # post-write read, so the tracker never starts before the card it follows is out.
    order: list[str] = []

    async def create_stop(**_: Any) -> dict[str, Any]:
        return _stop_public("stop-new-1", "ARMED")

    async def read(user_id: str | None, *, mode: str) -> dict[str, Any]:
        order.append("read")
        return {"available": False}

    autonomy.create_stop = create_stop
    tools.account_state = read  # type: ignore[method-assign]
    tools._execution_card = lambda *args, **kwargs: (order.append("card"), None)[1]  # type: ignore[method-assign]
    response, _ = await tools._call_async(
        tool="set_simulation_stop_loss", turn_id="t", mode="local", session_id="s", user_id="u",
        arguments={"symbol": "SHFE.rb2701", "positionDirection": "long", "positionBucket": "today",
                   "volume": 1, "triggerPrice": 3060.0})
    assert response["success"] is True and order == ["read", "card"]

    # 急停 cancels AI orders and every armed protection: its result carries the account read after it too.
    async def deactivate(**_: Any) -> dict[str, Any]:
        return {"status": "stopped"}

    autonomy.deactivate = deactivate
    response, _ = await tools._call_async(tool="stop_simulation_execution", turn_id="t", mode="local",
                                          session_id="s", user_id="u", arguments={"emergency": True})
    assert response["success"] is True
    assert "accountAfter" in json.loads(response["contentItems"][0]["text"])["data"]


# -- stop cards (2026-10-08): followed from the stop's own row, live same as after refresh --------


def _stop_row(**overrides: Any) -> dict[str, Any]:
    row = {"id": "ST-1", "status": "ARMED", "symbol": "SHFE.rb2701", "volume": 1,
           "trigger_price": 3060.0, "take_profit_price": 3090.0, "last_error": None}
    row.update(overrides)
    return row


def _stop_store(row: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(connection=SimpleNamespace(
        execute=lambda query, params=(): SimpleNamespace(fetchone=lambda: row)))


def test_every_stop_state_maps_into_the_card_contract_and_a_cancel_says_why() -> None:
    base = stop_execution_card(turn_id="t1", stop={"stopId": "ST-1", "symbol": "SHFE.rb2701",
                                                   "positionDirection": "long", "volume": 1,
                                                   "triggerPrice": 3060.0, "takeProfitPrice": 3090.0,
                                                   "status": "ARMED"})
    for status in ("ARMED", "TRIGGERING", "SUBMITTED", "PARTIAL", "EXECUTED", "FAILED", "CANCELLED", "UNKNOWN"):
        card = stop_card_update(base, _stop_row(status=status))
        assert card["cardId"] == base["cardId"] and card["status"] in CONTRACT_STATUSES
        validate_event(_envelope("card.execution", card))
    # The user's own symptom: the position closed, the protection auto-cancelled — the card that
    # said 已挂保护 must now say 已撤销 with the reason, under the same cardId.
    cancelled = stop_card_update(base, _stop_row(status="CANCELLED", last_error="anchor_position_closed"))
    assert (cancelled["status"], cancelled["statusLabel"]) == ("unfilled", "已撤销")
    assert cancelled["detail"] == "持仓已平，保护自动撤销。" and cancelled["price"] == "止损 3060 / 止盈 3090"
    assert stop_card_update(base, _stop_row(status="EXECUTED"))["statusLabel"] == "已平"
    assert stop_card_update(base, _stop_row(status="UNKNOWN"))["status"] == "unknown"


@pytest.mark.asyncio
async def test_the_tracker_follows_a_stop_row_and_drops_it_once_terminal() -> None:
    broker = _Broker()
    row = _stop_row()
    tracker = ExecutionCardTracker(broker, store=_stop_store(row))
    base = stop_execution_card(turn_id="t1", stop={"stopId": "ST-1", "symbol": "SHFE.rb2701",
                                                   "positionDirection": "long", "volume": 1,
                                                   "triggerPrice": 3060.0, "takeProfitPrice": 3090.0,
                                                   "status": "ARMED"})
    tracker.track_stop("s1", base, stop_id="ST-1", user_id="u1")
    await tracker.tick()
    assert broker.published == []  # the arm turn's own publish goes first
    tracker._cards[base["cardId"]].registered -= 10
    await tracker.tick()
    assert broker.published == []  # ARMED unchanged: no republish
    row.update(status="CANCELLED", last_error="position_closed")
    await tracker.tick()
    await tracker.tick()
    assert [payload["statusLabel"] for _, _, payload in broker.published] == ["已撤销"]
    republished = broker.published[0][2]
    assert republished["cardId"] == base["cardId"] and republished["detail"] == "持仓已平，保护自动撤销。"
    assert tracker.tracking() == []  # terminal: no more reads
    row.update(status="EXECUTED")
    await tracker.tick()
    assert len(broker.published) == 1

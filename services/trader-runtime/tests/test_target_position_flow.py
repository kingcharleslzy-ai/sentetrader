"""The 目标持仓 ticket end to end at the service layer: the tool plans the difference and issues
only the first leg's card (unchanged says so honestly), and the order-watch relay advances a plan
when its leg fills — a reverse plan's opening leg is sent on the first leg's confirmation (反手确认一次,
2026-10-24) through the same confirm service, never after a partial or failed close. UNKNOWN never
advances; a plan that stalls 30 minutes is abandoned with one honest line."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from fixtures.card_tools import legacy_card_args
from test_simulation_orders import USER, Gateway, NormalProvider, application, quote_now
from trader_runtime.trader_tools import TraderReadOnlyTools


def _provider_with(positions: list[dict[str, Any]]) -> NormalProvider:
    provider = NormalProvider()
    provider.position_items = positions
    return provider


def _long(volume: int, *, today: int = 0, hist: int = 0, frozen_today: int = 0, frozen_hist: int = 0):
    return {"symbol": "SHFE.au2612", "direction": "long", "volume": volume,
            "todayVolume": today, "historicalVolume": hist,
            "frozenToday": frozen_today, "frozenHistorical": frozen_hist}


def _short(volume: int, *, today: int = 0, hist: int = 0):
    return {"symbol": "SHFE.au2612", "direction": "short", "volume": volume,
            "todayVolume": today, "historicalVolume": hist, "frozenToday": 0, "frozenHistorical": 0}


def _tools(app, provider) -> TraderReadOnlyTools:
    return TraderReadOnlyTools(provider, app.state.adapter.runtime_status, proposals=app.state.proposals)


def _call(app, tools, *, target: int, reason: str = "目标持仓", turn: str = "turn-target",
          intent: str | None = None):
    session = app.state.broker.create_session("local", "native", USER)
    return tools.call(tool="propose_order",
                      arguments=legacy_card_args("create_target_position_proposal", {"symbol": "SHFE.au2612", "targetNet": target, "reason": reason,
                                 **({"priceIntent": intent} if intent else {})}),
                      turn_id=turn, mode="local", session_id=session.session_id, user_id=USER)


def _dump(response: dict[str, Any]) -> dict[str, Any]:
    """The TraderToolResult the response carries (errorCode top-level, card facts in `data`)."""
    return json.loads(response["contentItems"][0]["text"])


def _active_plan(app) -> dict[str, Any] | None:
    return app.state.store.active_target_plan(user_id=USER, symbol="SHFE.au2612")


def test_same_direction_add_is_one_open_card(tmp_path: Path) -> None:
    app = application(tmp_path / "target-add.sqlite3", Gateway(), _provider_with([_long(1, today=1)]))
    tools = _tools(app, app.state.data_provider)
    response, card = _call(app, tools, target=3)
    assert card is not None and card[0] == "proposal.trade"
    proposal = card[1]
    assert (proposal["side"], proposal["volume"]) == ("buy_open", 2)
    annotation = proposal["targetPosition"]
    assert annotation["step"] == 1 and annotation["totalSteps"] == 1 and "note" not in annotation
    assert annotation["summary"] == "目标 +3 手，当前 +1 手 → 买开 2 手"
    assert _dump(response)["data"]["targetPosition"]["summary"] == annotation["summary"]
    plan = _active_plan(app)
    assert plan["status"] == "awaiting_confirm" and plan["current_proposal_id"] == proposal["proposalId"]
    assert plan["leg_index"] == 0 and plan["leg"]["plan"]["targetNet"] == 3
    app.state.store.close()


def test_unchanged_target_says_so_and_creates_no_card_no_plan(tmp_path: Path) -> None:
    app = application(tmp_path / "target-same.sqlite3", Gateway(), _provider_with([_long(1, today=1)]))
    tools = _tools(app, app.state.data_provider)
    response, card = _call(app, tools, target=1)
    assert card is None
    data = _dump(response)["data"]
    assert data["cardIssued"] is False
    assert data["userFacing"]["candidateStatus"] == "当前已经是目标持仓，不用下单"
    assert _active_plan(app) is None
    app.state.store.close()


def test_reduce_takes_the_close_path_with_the_historical_bucket(tmp_path: Path) -> None:
    app = application(tmp_path / "target-reduce.sqlite3", Gateway(),
                      _provider_with([_long(3, today=1, hist=2)]))
    tools = _tools(app, app.state.data_provider)
    _, card = _call(app, tools, target=1)
    assert card[0] == "proposal.trade"
    assert (card[1]["side"], card[1]["volume"], card[1]["positionBucket"]) == ("sell_close", 2, "historical")
    app.state.store.close()


def test_flatten_is_one_batch_card_of_every_bucket(tmp_path: Path) -> None:
    app = application(tmp_path / "target-flat.sqlite3", Gateway(),
                      _provider_with([_long(2, today=1, hist=1), _short(1, hist=1)]))
    tools = _tools(app, app.state.data_provider)
    response, card = _call(app, tools, target=0)
    assert card[0] == "proposal.close_batch" and card[1]["totalOrders"] == 3
    assert card[1]["targetPosition"]["step"] == 1 and card[1]["targetPosition"]["totalSteps"] == 1
    assert _dump(response)["data"]["targetPosition"]["summary"] == "目标 0 手，当前 +1 手 → 平掉全部 3 手"
    assert _active_plan(app)["status"] == "awaiting_confirm"
    app.state.store.close()


def test_reverse_issues_only_the_first_leg_with_the_two_step_note(tmp_path: Path) -> None:
    app = application(tmp_path / "target-reverse.sqlite3", Gateway(), _provider_with([_long(1, today=1)]))
    tools = _tools(app, app.state.data_provider)
    response, card = _call(app, tools, target=-2)
    assert card[0] == "proposal.close_batch" and card[1]["totalOrders"] == 1
    annotation = card[1]["targetPosition"]
    assert annotation["crossesZero"] is True and annotation["step"] == 1 and annotation["totalSteps"] == 2
    assert "确认一次" in annotation["note"] and "自动发出第 2 步" in annotation["note"]
    assert annotation["summary"] == "目标 -2 手，当前 +1 手 → 平多 1 手，成交后自动开空 2 手（确认一次）"
    assert _dump(response)["data"]["targetPosition"]["crossesZero"] is True
    app.state.store.close()


def test_insufficient_available_volume_refuses_and_no_plan_survives(tmp_path: Path) -> None:
    app = application(tmp_path / "target-frozen.sqlite3", Gateway(),
                      _provider_with([_long(3, today=1, hist=2, frozen_today=1, frozen_hist=2)]))
    tools = _tools(app, app.state.data_provider)
    response, card = _call(app, tools, target=1)
    assert card is None
    dump = _dump(response)
    assert dump["errorCode"] == "close_volume_exceeds_position"
    assert "平不了" in dump["data"]["userFacing"]["candidateIssues"][0]
    assert _active_plan(app) is None  # refused before its card: the plan is not left dangling
    app.state.store.close()


def test_a_new_plan_for_the_same_contract_supersedes_the_old_one(tmp_path: Path) -> None:
    app = application(tmp_path / "target-supersede.sqlite3", Gateway(), _provider_with([_long(1, today=1)]))
    tools = _tools(app, app.state.data_provider)
    _, first_card = _call(app, tools, target=3, turn="turn-one")
    plan_id = _active_plan(app)["id"]
    response, second_card = _call(app, tools, target=2, turn="turn-two")
    assert second_card is not None and second_card[1]["side"] == "buy_open"
    assert "已作废" in _dump(response)["data"]["supersededPlan"]["note"]
    assert app.state.store.get_target_plan(plan_id, user_id=USER)["status"] == "abandoned"
    assert _active_plan(app)["id"] != plan_id
    app.state.store.close()


# --- the relay: fills advance a plan, cards only -------------------------------------------------


async def _confirmed_target_first_leg(app, provider, *, target: int, key: str,
                                      intent: str | None = None) -> dict[str, Any]:
    """The plan's first leg issued by the tool and confirmed through the real HTTP route — that is
    where the plan links and the leg's orders join the watch."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        session = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
        tools = _tools(app, provider)
        _, card = tools.call(tool="propose_order",
                             arguments=legacy_card_args("create_target_position_proposal", {"symbol": "SHFE.au2612", "targetNet": target, "reason": "目标持仓",
                                        **({"priceIntent": intent} if intent else {})}),
                             turn_id="turn-relay", mode="local",
                             session_id=session["sessionId"], user_id=USER)
        provider.quote_datetime = quote_now()
        confirmed = await client.post(f"/api/proposals/{card[1]['proposalId']}/confirm", json={
            "approvalId": card[1]["approvalId"], "proposalVersion": card[1]["version"],
            "idempotencyKey": key})
        assert confirmed.status_code == 200
        return {"card": card[1], "result": confirmed.json(), "sessionId": session["sessionId"]}


def _approval_events(app, session_id: str) -> list[dict[str, Any]]:
    return [event.get("payload", {}) for _, event in app.state.store.list_events(session_id)
            if str(event.get("name", "")).startswith("proposal.")]


def _plan_notices(app, session_id: str) -> list[str]:
    return [str(event.get("payload", {}).get("text")) for _, event in app.state.store.list_events(session_id)
            if event.get("name") == "message.completed"
            and str(event.get("payload", {}).get("turnId", "")).startswith("targetplan-")]


async def _confirm_stored_card(app, provider, proposal_id: str, key: str) -> None:
    """Confirm the relay's own card (its approvalId/version live on the stored record)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        record = app.state.store.get(proposal_id)
        provider.quote_datetime = quote_now()
        confirmed = await client.post(f"/api/proposals/{proposal_id}/confirm", json={
            "approvalId": record["approvalId"], "proposalVersion": record["version"],
            "idempotencyKey": key})
        assert confirmed.status_code == 200


def _second_cards(app, session_id: str) -> list[dict[str, Any]]:
    return [payload for payload in _approval_events(app, session_id)
            if payload.get("targetPosition", {}).get("step") == 2]


async def test_one_confirmation_sends_the_close_then_the_open_after_the_fill(tmp_path: Path) -> None:
    """反手确认一次 (2026-10-24): the user confirms the close through the HTTP route once; when it has
    filled, the relay confirms the opening leg through the same confirm service, authorized by that
    confirmation — no second card to confirm. Its fill completes the plan."""
    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-relay.sqlite3", gateway, provider)
    from types import SimpleNamespace
    app.state.voice_realtime.calls["test-call"] = SimpleNamespace(user_id=USER)
    made = await _confirmed_target_first_leg(app, provider, target=-2, key="relay-first")
    watch = app.state.order_watch
    assert _active_plan(app)["status"] == "awaiting_fill"
    assert _active_plan(app)["currentClientOrderIds"]  # the leg's order(s) are linked
    assert gateway.submit_count == 1 and gateway.request["offset"] != "OPEN"  # the close only, so far
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await watch._sweep_target_plans()
    # The opening leg went out on the first confirmation.
    assert gateway.submit_count == 2
    assert (gateway.request["direction"], gateway.request["offset"], gateway.request["volume"]) == ("SELL", "OPEN", 2)
    plan = _active_plan(app)
    assert plan["status"] == "awaiting_fill" and plan["leg_index"] == 1
    [second] = _second_cards(app, made["sessionId"])
    assert (second["side"], second["volume"], second["status"]) == ("sell_open", 2, "candidate_recorded")
    assert second["targetPosition"]["authorizedByProposalId"] == made["card"]["proposalId"]
    assert "自动发出" in second["targetPosition"]["note"]
    assert plan["current_proposal_id"] == second["proposalId"]
    record = app.state.store.get(second["proposalId"])
    assert record["status"] == "candidate_recorded" and record["approvalUsedAt"] is not None
    notices = _plan_notices(app, made["sessionId"])
    assert len(notices) == 1 and "第 1 段已成交" in notices[0] and "自动发出" in notices[0]
    # The notice and the second card share one turn: a card whose turn no message created was
    # dropped by the client (the 10-04 lost-card bug).
    notice_turns = [event["payload"]["turnId"] for _, event in app.state.store.list_events(made["sessionId"])
                    if event.get("name") == "message.completed"
                    and str(event["payload"].get("turnId", "")).startswith("targetplan-")]
    assert notice_turns == [second["turnId"]]
    assert app.state.voice_realtime._pending_announcements[USER][-1]["kind"] == "target_plan"
    # A sweep while the opening leg is still being read sends nothing more.
    gateway.query_report = gateway.report(gateway.request, status="ALIVE")
    await watch._sweep_target_plans()
    assert gateway.submit_count == 2
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await watch._sweep_target_plans()
    assert _active_plan(app) is None  # done plans no longer count as active
    assert app.state.store.get_target_plan(plan["id"], user_id=USER)["status"] == "done"
    assert any("目标持仓完成" in text for text in _plan_notices(app, made["sessionId"]))
    assert gateway.submit_count == 2
    app.state.store.close()


async def test_a_plan_from_before_one_confirmation_still_gets_its_own_second_card(tmp_path: Path) -> None:
    """#1140: a plan stored before 反手确认一次 (its first card promised a second card; no `confirmOnce`) and
    still awaiting its close when the new Runtime starts is never confirmed by the relay — its second leg comes
    as a card for the user to confirm, as that first card said."""
    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-relay-old.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=-2, key="relay-old")
    plan = _active_plan(app)
    assert plan["leg"]["confirmOnce"] is True  # a plan made now records the one-confirmation contract
    app.state.store.connection.execute(
        "UPDATE target_position_plans SET leg_json = json_remove(leg_json, '$.confirmOnce') WHERE id = ?", (plan["id"],))
    app.state.store.connection.commit()
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await app.state.order_watch._sweep_target_plans()
    assert gateway.submit_count == 1  # nothing sent on the first card's confirmation
    [second] = _second_cards(app, made["sessionId"])
    assert "authorizedByProposalId" not in second["targetPosition"] and "确认后才会发单" in second["targetPosition"]["note"]
    assert _active_plan(app)["status"] == "awaiting_confirm"
    app.state.store.close()


async def test_a_restart_between_the_legs_never_sends_the_opening_twice(tmp_path: Path) -> None:
    """The process dies after the relay moved the plan past leg 1 but before leg 2 was confirmed; a new
    process's watch (restore + sweep) must not send the opening leg — the plan waits for the stall sweep.
    And the leg's confirm itself is once: the same plan-and-leg key replays, the approval is single-use."""
    from trader_runtime.order_watch import OrderWatch

    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-restart.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=-2, key="restart-first")
    watch = app.state.order_watch
    plan_id = _active_plan(app)["id"]

    async def crash(*args: Any, **kwargs: Any) -> None:
        raise SystemExit("process died between the legs")

    watch._issue_next_leg_card = crash  # type: ignore[method-assign]
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    with pytest.raises(SystemExit):
        await watch._advance_target_plan(app.state.store.awaiting_fill_plans()[0])
    assert app.state.store.get_target_plan(plan_id, user_id=USER)["status"] == "awaiting_confirm"
    rebooted = OrderWatch(app)
    rebooted.restore()
    await rebooted._sweep_target_plans()
    await rebooted._sweep_target_plans()
    assert gateway.submit_count == 1  # the opening leg was never sent by the new process
    assert not _second_cards(app, made["sessionId"])
    # The leg's send is once even when the relay runs it twice for the same plan and leg.
    plan_row = app.state.store.get_target_plan(plan_id, user_id=USER)
    provider.quote_datetime = quote_now()
    await rebooted._issue_next_leg_card(plan_row, authorized_by=made["card"]["proposalId"])
    await rebooted._issue_next_leg_card(plan_row, authorized_by=made["card"]["proposalId"])
    assert gateway.submit_count == 2
    [second] = _second_cards(app, made["sessionId"])
    record = app.state.store.get(second["proposalId"])
    with pytest.raises(Exception):
        await app.state.proposals.confirm(second["proposalId"], user_id=USER, approval_id=record["approvalId"],
                                          proposal_version=record["version"], idempotency_key="someone-else")
    replay = await app.state.proposals.confirm(
        second["proposalId"], user_id=USER, approval_id=record["approvalId"], proposal_version=record["version"],
        idempotency_key=f"targetplan:{plan_id}:leg2")
    assert replay["clientOrderId"] == record["payload"]["clientOrderId"]
    assert gateway.submit_count == 2
    app.state.store.close()


async def test_a_refused_opening_leg_ends_the_plan_with_one_notice(tmp_path: Path) -> None:
    """The quote goes stale exactly when the opening leg is sent: the confirm service's own send-time check
    refuses it; nothing is sent, the card is voided (nobody can confirm it later), one line says why."""
    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-refused.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=-2, key="refused-first")
    plan_id = _active_plan(app)["id"]
    orders = app.state.simulation_orders
    real_prepare = orders.prepare

    def stale_at_send(**kwargs: Any) -> dict[str, Any]:
        provider.quote_datetime = "2000-01-01 09:00:00.000000"
        return real_prepare(**kwargs)

    orders.prepare = stale_at_send  # type: ignore[method-assign]
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await app.state.order_watch._sweep_target_plans()
    assert gateway.submit_count == 1
    assert app.state.store.get_target_plan(plan_id, user_id=USER)["status"] == "abandoned"
    notices = _plan_notices(app, made["sessionId"])
    assert len(notices) == 1 and "第 2 段" in notices[0] and "没有发出" in notices[0] and "计划已取消" in notices[0]
    assert not _second_cards(app, made["sessionId"])  # no card was shown
    [voided] = [row for row in app.state.store.connection.execute(
        "SELECT id, status FROM proposals WHERE id != ? AND status = 'rejected'", (made["card"]["proposalId"],))]
    assert voided["status"] == "rejected"
    await app.state.order_watch._sweep_target_plans()
    assert gateway.submit_count == 1 and len(_plan_notices(app, made["sessionId"])) == 1
    app.state.store.close()


async def test_a_partially_filled_close_never_opens(tmp_path: Path) -> None:
    """反手 from long 3: the close ends after filling only 2 lots — the opening leg is never sent."""
    gateway = Gateway()
    provider = _provider_with([_long(3, today=0, hist=3)])
    app = application(tmp_path / "target-partial-reverse.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=-3, key="partial-reverse-first")
    plan_id = _active_plan(app)["id"]
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=1)
    await app.state.order_watch._sweep_target_plans()
    await app.state.order_watch._sweep_target_plans()
    assert gateway.submit_count == 1
    assert app.state.store.get_target_plan(plan_id, user_id=USER)["status"] == "abandoned"
    assert not _second_cards(app, made["sessionId"])
    [notice] = _plan_notices(app, made["sessionId"])
    assert "部分成交 2 手后已结束" in notice
    app.state.store.close()


async def test_a_plan_superseded_while_the_relay_was_reading_never_comes_back(tmp_path: Path) -> None:
    """#518：中继读委托结果的在途窗口里，用户对同一合约发起了新的目标持仓——旧计划已作废；
    中继的结论（推进到第 2 步、再出一张卡、说一句已成交）必须整体落空，不把作废计划复活。"""
    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-mid-query.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=-2, key="mid-query-first")
    watch = app.state.order_watch
    old_plan_id = _active_plan(app)["id"]
    real_query = app.state.simulation_orders.query

    async def supersede_mid_query(client_order_id: str, *, user_id: str):
        # Between the relay reading the plan and writing its verdict, a new target for the
        # same contract supersedes the old plan (the tool's own real entry).
        _call(app, _tools(app, provider), target=3, turn="turn-supersede")
        return await real_query(client_order_id, user_id=user_id)

    app.state.simulation_orders.query = supersede_mid_query  # type: ignore[method-assign]
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await watch._sweep_target_plans()
    app.state.simulation_orders.query = real_query  # type: ignore[method-assign]

    superseded = app.state.store.get_target_plan(old_plan_id, user_id=USER)
    assert superseded["status"] == "abandoned" and superseded["leg_index"] == 0  # 没被复活
    active = _active_plan(app)
    assert active is not None and active["id"] != old_plan_id                   # 活着的是新计划
    assert not [payload for payload in _approval_events(app, made["sessionId"])
                if payload.get("targetPosition", {}).get("step") == 2]         # 没有第二张卡
    assert _plan_notices(app, made["sessionId"]) == []                         # 也没有“已成交”
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_a_plan_superseded_while_its_opening_leg_is_being_confirmed_sends_nothing(tmp_path: Path) -> None:
    """#518：中继正要用第 1 步的确认发第 2 段时，用户对同一合约发起了新目标——旧计划作废、它的第 2 段卡
    随之作废（rejected + 作废说明），确认服务在事务里看到卡已作废就不发单；作废计划不再说话。"""
    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-stale-card.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=-2, key="stale-card-first")
    old_plan_id = _active_plan(app)["id"]
    real_confirm = app.state.proposals.confirm
    voided: list[str] = []

    async def superseded_first(proposal_id: str, **kwargs: Any) -> dict[str, Any]:
        if str(kwargs.get("idempotency_key", "")).startswith("targetplan:"):
            voided.append(proposal_id)
            _call(app, _tools(app, provider), target=3, turn="turn-new-target")  # 新目标替代旧计划
        return await real_confirm(proposal_id, **kwargs)

    app.state.proposals.confirm = superseded_first  # type: ignore[method-assign]
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await app.state.order_watch._sweep_target_plans()
    app.state.proposals.confirm = real_confirm  # type: ignore[method-assign]

    assert len(voided) == 1
    stale = app.state.store.get(voided[0])
    assert stale["status"] == "rejected" and stale["payload"].get("superseded") is True
    assert gateway.submit_count == 1                       # 第 2 段没有发出
    assert app.state.store.get_target_plan(old_plan_id, user_id=USER)["status"] == "abandoned"
    assert _active_plan(app)["id"] != old_plan_id
    assert _plan_notices(app, made["sessionId"]) == []     # 作废的计划不再说话
    assert app.state.store.get(made["card"]["proposalId"])["status"] == "candidate_recorded"
    app.state.store.close()


async def test_cancelled_first_leg_abandons_the_plan_with_an_honest_line(tmp_path: Path) -> None:
    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-cancelled.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=-2, key="cancelled-first")
    watch = app.state.order_watch
    plan_id = _active_plan(app)["id"]
    gateway.query_report = gateway.report(gateway.request, status="FINISHED")  # cancelled, nothing filled
    await watch._sweep_target_plans()
    assert _active_plan(app) is None
    assert app.state.store.get_target_plan(plan_id, user_id=USER)["status"] == "abandoned"
    notices = _plan_notices(app, made["sessionId"])
    assert len(notices) == 1 and "已撤销" in notices[0] and "请先在委托里确认原单状态" in notices[0]
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_unknown_never_advances_and_the_stall_sweep_abandons(tmp_path: Path) -> None:
    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-unknown.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=-2, key="unknown-first")
    watch = app.state.order_watch
    plan_id = _active_plan(app)["id"]
    # The read comes back with another order's identity: 身份异常 — unknown family, never an advance.
    gateway.query_report = gateway.report(gateway.request, symbol="SHFE.wr2612")
    await watch._sweep_target_plans()
    assert app.state.store.get_target_plan(plan_id, user_id=USER)["status"] == "awaiting_fill"
    assert not _plan_notices(app, made["sessionId"])
    # 30 minutes without progress: the sweep abandons the plan with one honest line.
    with app.state.store.lock, app.state.store.connection:
        app.state.store.connection.execute(
            "UPDATE target_position_plans SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", plan_id))
    await watch._sweep_target_plans()
    assert app.state.store.get_target_plan(plan_id, user_id=USER)["status"] == "abandoned"
    assert any("30 分钟" in text for text in _plan_notices(app, made["sessionId"]))
    assert gateway.submit_count == 1  # nothing was ever re-sent
    app.state.store.close()


# -- 执行与保护链修复 (2026-10-04 #518): 还挂着的腿继续等，只有结束的腿才放弃；账户隔离 --------------------


async def test_a_resting_leg_keeps_waiting_then_fills_into_the_second_card(tmp_path: Path) -> None:
    """还挂在场上的第一段（已受理、未成交）继续等：不放弃、不发第 2 段；首段全部成交后
    跨零计划的第 2 段才按第 1 步的确认发出。收尾交给 30 分钟停滞清扫。"""
    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-resting.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=-2, key="resting-first")
    watch = app.state.order_watch
    plan_id = _active_plan(app)["id"]
    await watch._sweep_target_plans()  # 已受理、还挂着：继续等
    assert app.state.store.get_target_plan(plan_id, user_id=USER)["status"] == "awaiting_fill"
    assert not _plan_notices(app, made["sessionId"])  # 不说话：单还在场上
    assert gateway.submit_count == 1
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await watch._sweep_target_plans()  # 首段全部成交：第 2 段才发
    plan = _active_plan(app)
    assert plan["status"] == "awaiting_fill" and plan["leg_index"] == 1
    second = _second_cards(app, made["sessionId"])
    assert len(second) == 1 and second[0]["side"] == "sell_open"
    assert gateway.submit_count == 2
    app.state.store.close()


async def test_a_partially_filled_leg_that_is_still_alive_keeps_waiting(tmp_path: Path) -> None:
    """部分成交但单子还在场上（ALIVE）：还在挂，继续等 —— 只有已结束的单才放弃计划。"""
    gateway = Gateway()
    provider = _provider_with([_long(3, today=0, hist=3)])
    app = application(tmp_path / "target-alive-partial.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=1, key="alive-partial-first")
    watch = app.state.order_watch
    plan_id = _active_plan(app)["id"]
    gateway.query_report = gateway.report(gateway.request, status="ALIVE", volume_left=1)
    await watch._sweep_target_plans()
    assert app.state.store.get_target_plan(plan_id, user_id=USER)["status"] == "awaiting_fill"
    assert not _plan_notices(app, made["sessionId"])
    app.state.store.close()


async def test_an_ended_partial_leg_abandons_with_the_partial_line_and_no_reissue_invite(
    tmp_path: Path,
) -> None:
    """部分成交后已结束：放弃计划，说清“第 1 段部分成交 1 手后已结束”，不再邀请重新发起。"""
    gateway = Gateway()
    provider = _provider_with([_long(3, today=0, hist=3)])
    app = application(tmp_path / "target-ended-partial.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=1, key="ended-partial-first")
    watch = app.state.order_watch
    plan_id = _active_plan(app)["id"]
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=1)
    await watch._sweep_target_plans()
    assert app.state.store.get_target_plan(plan_id, user_id=USER)["status"] == "abandoned"
    notices = _plan_notices(app, made["sessionId"])
    assert len(notices) == 1
    assert "第 1 段部分成交 1 手后已结束" in notices[0]
    assert "请先在委托里确认原单状态" in notices[0] and "重新发起" not in notices[0]
    app.state.store.close()


def test_plans_of_different_accounts_never_supersede_each_other(tmp_path: Path) -> None:
    """同一用户在不同账户的同一合约计划互不替代；替代只发生在同账户内。"""
    app = application(tmp_path / "target-plan-accounts.sqlite3", Gateway(),
                      _provider_with([_long(1, today=1)]))
    store = app.state.store
    account = store.default_trading_account_id
    leg_json = {"plan": {"legs": []}, "sessionId": "session-a", "turnId": "turn-a", "reason": "目标持仓"}
    plan_a, superseded_a = store.create_target_plan(
        user_id=USER, trading_account_id=account, symbol="SHFE.au2612", target_net=3, leg_json=leg_json)
    plan_b, superseded_b = store.create_target_plan(
        user_id=USER, trading_account_id="tqkq-sim:other", symbol="SHFE.au2612", target_net=2,
        leg_json=leg_json)
    assert superseded_a is None and superseded_b is None  # 两个账户各有各的进行中计划
    assert store.get_target_plan(plan_a["id"], user_id=USER)["status"] == "awaiting_confirm"
    assert store.active_target_plan(
        user_id=USER, symbol="SHFE.au2612", trading_account_id=account)["id"] == plan_a["id"]
    assert store.active_target_plan(
        user_id=USER, symbol="SHFE.au2612", trading_account_id="tqkq-sim:other")["id"] == plan_b["id"]
    _, superseded = store.create_target_plan(  # 同账户的新计划仍替代旧计划
        user_id=USER, trading_account_id=account, symbol="SHFE.au2612", target_net=1, leg_json=leg_json)
    assert superseded["id"] == plan_a["id"]
    app.state.store.close()


async def test_a_session_switched_to_another_account_abandons_before_the_next_card(
    tmp_path: Path, monkeypatch,
) -> None:
    """会话切到另一个账户后，中继不再把第 2 步的卡发进别的账户：计划取消，一句话说清。"""
    from test_account_registry import BETA_ACCOUNT, DEFAULT_ACCOUNT
    from test_account_roles import roles_app

    app, gateway_a, gateway_b = roles_app(tmp_path, monkeypatch, "target-account-switch.sqlite3")
    try:
        provider = app.state.accounts.get(DEFAULT_ACCOUNT).provider
        provider.position_items = [_long(1, today=1)]
        session = app.state.broker.create_session("local", "native", USER)
        tools = _tools(app, provider)
        _, card = tools.call(tool="propose_order",
                             arguments=legacy_card_args("create_target_position_proposal", {"symbol": "SHFE.au2612", "targetNet": -2, "reason": "目标持仓"}),
                             turn_id="turn-switch", mode="local", session_id=session.session_id,
                             user_id=USER)
        plan_id = card[1]["targetPosition"]["planId"]
        provider.quote_datetime = quote_now()
        confirmed = await app.state.proposals.confirm(
            card[1]["proposalId"], user_id=USER, approval_id=card[1]["approvalId"],
            proposal_version=card[1]["version"], idempotency_key="switch-first")
        await app.state.order_watch.link_confirmed_plan(
            confirmed, user_id=USER, proposal_id=card[1]["proposalId"])  # 确认路由的同一步
        assert gateway_a.submit_count == 1
        watch = app.state.order_watch
        gateway_a.query_report = gateway_a.report(gateway_a.request, status="FINISHED", volume_left=0)
        app.state.store.set_session_trading_account(session.session_id, BETA_ACCOUNT, user_id=USER)
        await watch._sweep_target_plans()
        second = [payload for payload in _approval_events(app, session.session_id)
                  if payload.get("targetPosition", {}).get("step") == 2]
        assert second == []  # 第 2 步没有出卡
        assert gateway_b.submit_count == 0
        assert app.state.store.get_target_plan(plan_id, user_id=USER)["status"] == "abandoned"
        notices = _plan_notices(app, session.session_id)
        assert len(notices) == 1 and "账户已切换" in notices[0] and "第 2 步未出卡" in notices[0]
    finally:
        app.state.store.close()


def test_an_upper_cased_contract_from_the_model_is_carried_as_the_catalogs_spelling(tmp_path: Path) -> None:
    """10-13 production: the model's (or the plan's) SHFE.RB2701 reached the paper engine's quote
    read as given and broke its market subscription for every contract. The tool now reads the
    contract under the catalog's spelling: every quote read, the card and the stored plan."""
    provider = _provider_with([])
    reads: list[str] = []
    real_snapshot = provider.market_snapshot

    def snapshot(turn_id: str, symbol: str) -> dict[str, Any]:
        reads.append(symbol)
        return real_snapshot(turn_id, symbol)

    provider.market_snapshot = snapshot  # type: ignore[method-assign]
    app = application(tmp_path / "target-case.sqlite3", Gateway(), provider)
    tools = _tools(app, app.state.data_provider)
    session = app.state.broker.create_session("local", "native", USER)
    _, card = tools.call(tool="propose_order",
                         arguments=legacy_card_args("create_target_position_proposal", {"symbol": "SHFE.AU2612", "targetNet": 2, "reason": "目标持仓"}),
                         turn_id="turn-case", mode="local", session_id=session.session_id, user_id=USER)
    assert card is not None and card[1]["symbol"] == "SHFE.au2612"
    assert reads and set(reads) == {"SHFE.au2612"}
    plan = _active_plan(app)
    assert plan["symbol"] == "SHFE.au2612" and plan["leg"]["plan"]["symbol"] == "SHFE.au2612"
    # An old row stored upper-cased (before 10-13) is still the same contract's plan.
    assert app.state.store.active_target_plan(user_id=USER, symbol="SHFE.AU2612")["id"] == plan["id"]
    app.state.store.close()


# -- #940 / #941 (10-15 夜盘): 差额与价格方式都由计划决定，两种账户一个口径，贯穿两步计划的每一步 -------------

# 盘口 买一 823 / 卖一 824：买单挂单价 = 买一、对手价 = 卖一；卖单挂单价 = 卖一、对手价 = 买一。
_BUY_PRICE = {"passive": 823.0, "aggressive": 824.0}
_SELL_PRICE = {"passive": 824.0, "aggressive": 823.0}
_ACCOUNTS = pytest.mark.parametrize("mode, label", [("paper", "本站模拟账户"), ("tqkq-readonly", "快期模拟账户")])
_INTENTS = pytest.mark.parametrize("intent", ["aggressive", "passive"])


def _account_provider(mode: str, positions: list[dict[str, Any]]) -> NormalProvider:
    provider = _provider_with(positions)
    provider.mode = mode  # type: ignore[assignment]
    return provider


@_ACCOUNTS
@_INTENTS
def test_held_long_one_to_long_two_is_one_lot_at_the_stated_price(tmp_path: Path, mode, label, intent) -> None:
    """#940: 已持多 1“做到多 2 手”——两种账户都是买开差额 1 手，不是 2 手；价格按用户说的方式。"""
    app = application(tmp_path / "target-940.sqlite3", Gateway(), _account_provider(mode, [_long(1, today=1)]))
    response, card = _call(app, _tools(app, app.state.data_provider), target=2, intent=intent)
    proposal = card[1]
    assert (proposal["side"], proposal["volume"], proposal["accountLabel"]) == ("buy_open", 1, label)
    assert (proposal["priceIntent"], proposal["limitPrice"]) == (intent, _BUY_PRICE[intent])
    assert proposal["targetPosition"]["summary"] == "目标 +2 手，当前 +1 手 → 买开 1 手"
    assert proposal["targetPosition"]["priceIntent"] == intent
    assert _dump(response)["data"]["volume"] == 1
    app.state.store.close()


@_ACCOUNTS
@_INTENTS
def test_held_short_to_long_flattens_first_at_the_stated_price(tmp_path: Path, mode, label, intent) -> None:
    """#941: 已持空做到多——第 1 步全平（买平）也按用户说的价格方式，不再固定对手价。"""
    app = application(tmp_path / "target-941-short.sqlite3", Gateway(),
                      _account_provider(mode, [_short(1, today=1)]))
    _, card = _call(app, _tools(app, app.state.data_provider), target=1, intent=intent)
    assert card[0] == "proposal.close_batch" and card[1]["accountLabel"] == label
    [order] = card[1]["orders"]
    assert (order["side"], order["volume"], order["priceIntent"]) == ("buy_close", 1, intent)
    assert order["limitPrice"] == _BUY_PRICE[intent]
    assert card[1]["targetPosition"]["legs"] == [{"action": "close", "side": "close_all", "volume": 1},
                                                 {"action": "open", "side": "buy_open", "volume": 1}]
    app.state.store.close()


@_ACCOUNTS
@_INTENTS
async def test_both_steps_of_a_reverse_plan_keep_the_stated_price(tmp_path: Path, mode, label, intent) -> None:
    """#941: 多 1 → 空 1 的两步计划：第 1 步全平与接力出卡的第 2 步卖开，都按用户说的价格方式定价。"""
    gateway = Gateway()
    provider = _account_provider(mode, [_long(1, today=1)])
    app = application(tmp_path / "target-941-reverse.sqlite3", gateway, provider)
    made = await _confirmed_target_first_leg(app, provider, target=-1, key=f"reverse-{intent}", intent=intent)
    [first] = made["card"]["orders"]
    assert (first["side"], first["priceIntent"], first["limitPrice"]) == ("sell_close", intent, _SELL_PRICE[intent])
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await app.state.order_watch._sweep_target_plans()
    [second] = [payload for payload in _approval_events(app, made["sessionId"])
                if payload.get("targetPosition", {}).get("step") == 2]
    assert (second["side"], second["volume"], second["accountLabel"]) == ("sell_open", 1, label)
    assert (second["priceIntent"], second["limitPrice"]) == (intent, _SELL_PRICE[intent])
    assert gateway.submit_count == 2  # the opening leg went out on the first leg's confirmation, at its own price
    app.state.store.close()


async def test_an_unstated_price_keeps_the_per_side_defaults_through_the_relay(tmp_path: Path) -> None:
    """没说价格方式：第 1 步全平对手价，第 2 步开仓按账号默认价格方式（不被第 1 步带偏）。"""
    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-941-default.sqlite3", gateway, provider)
    default_open = app.state.store.user_preferences(USER).trade.defaultPriceIntent
    made = await _confirmed_target_first_leg(app, provider, target=-1, key="reverse-default")
    assert made["card"]["orders"][0]["priceIntent"] == "aggressive"
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await app.state.order_watch._sweep_target_plans()
    [second] = [payload for payload in _approval_events(app, made["sessionId"])
                if payload.get("targetPosition", {}).get("step") == 2]
    assert second["priceIntent"] == default_open
    app.state.store.close()


def _with_orders(provider: NormalProvider, orders: list[dict[str, Any]]) -> None:
    base = provider.account_activity()
    provider.account_activity = lambda: {**base, "orders": orders}  # type: ignore[method-assign]


def _order(direction: str, offset: str, volume: int, left: int, *, status: str = "ALIVE") -> dict[str, Any]:
    return {"clientOrderId": f"o-{direction}-{offset}-{left}", "symbol": "SHFE.au2612", "direction": direction,
            "offset": offset, "volume": volume, "volumeLeft": left, "status": status, "limitPrice": 823.0}


@_ACCOUNTS
def test_a_working_opening_order_refuses_until_it_is_cancelled(tmp_path: Path, mode, label) -> None:
    """#942 review: 已持多 1、还挂着买开 1 手（部分成交剩 1 手也算）时“做到多 2 手”不出卡、不建计划，
    说清几手什么方向的挂单；撤掉后照常出差额 1 手的卡。差额从不把挂单算进去。"""
    provider = _account_provider(mode, [_long(1, today=1)])
    app = application(tmp_path / "target-working.sqlite3", Gateway(), provider)
    tools = _tools(app, app.state.data_provider)
    _with_orders(provider, [_order("BUY", "OPEN", 2, 1)])
    response, card = _call(app, tools, target=2, intent="passive")
    assert card is None and _active_plan(app) is None
    dump = _dump(response)
    assert dump["errorCode"] == "target_position_orders_working"
    [line] = dump["data"]["userFacing"]["candidateIssues"]
    assert "还有 1 手买开挂单没成交" in line and "先撤掉或等成交后再调目标持仓" in line and "撤单" in line
    _with_orders(provider, [_order("BUY", "OPEN", 2, 1, status="FINISHED")])  # 撤掉了
    _, card = _call(app, tools, target=2, intent="passive", turn="turn-after-cancel")
    assert card is not None and (card[1]["side"], card[1]["volume"], card[1]["accountLabel"]) == ("buy_open", 1, label)
    app.state.store.close()


@_ACCOUNTS
def test_a_working_closing_order_refuses_but_other_contracts_do_not_block(tmp_path: Path, mode, label) -> None:
    """#942 Codex review: 还挂着的平仓单一成交也会改净持仓（多 1 挂卖平 1，做到多 2 买开 1 手，成交后只剩多 1）——
    同样拒绝；别的合约的挂单不受影响。"""
    provider = _account_provider(mode, [_long(1, today=1)])
    app = application(tmp_path / "target-working-close.sqlite3", Gateway(), provider)
    tools = _tools(app, app.state.data_provider)
    _with_orders(provider, [_order("SELL", "CLOSETODAY", 1, 1)])
    response, card = _call(app, tools, target=2)
    assert card is None and _active_plan(app) is None
    assert _dump(response)["errorCode"] == "target_position_orders_working"
    assert "1 手卖平挂单没成交" in _dump(response)["data"]["userFacing"]["candidateIssues"][0]
    _with_orders(provider, [{**_order("BUY", "OPEN", 3, 3), "symbol": "SHFE.ag2612"}])
    _, card = _call(app, tools, target=2, turn="turn-other-contract")
    assert card is not None and (card[1]["side"], card[1]["volume"], card[1]["accountLabel"]) == ("buy_open", 1, label)
    app.state.store.close()


def test_unreadable_orders_refuse_the_target_card(tmp_path: Path) -> None:
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-orders-unreadable.sqlite3", Gateway(), provider)

    def unreadable() -> dict[str, Any]:
        raise RuntimeError("sidecar_account_activity_unavailable")

    provider.account_activity = unreadable  # type: ignore[method-assign]
    response, card = _call(app, _tools(app, app.state.data_provider), target=2)
    assert card is None and _active_plan(app) is None
    assert _dump(response)["errorCode"] == "target_position_orders_unreadable"
    app.state.store.close()


def test_a_reduce_split_across_today_and_yesterday_keeps_the_stated_price(tmp_path: Path) -> None:
    """#941 同根：减仓跨今昨拆成批量平仓卡时，每张都按用户说的价格方式，不退回对手价。"""
    app = application(tmp_path / "target-941-split.sqlite3", Gateway(),
                      _provider_with([_long(4, today=2, hist=2)]))
    _, card = _call(app, _tools(app, app.state.data_provider), target=1, intent="passive")
    assert card[0] == "proposal.close_batch" and card[1]["totalVolume"] == 3
    assert {(order["priceIntent"], order["limitPrice"]) for order in card[1]["orders"]} == {("passive", 824.0)}
    assert {order["positionBucket"] for order in card[1]["orders"]} == {"today", "historical"}
    app.state.store.close()


@pytest.mark.parametrize("patch", [
    {"dataStatus": "stale"}, {"dataStatus": None}, {"dataStatus": "weird"}, {"ordersInitialized": False},
    {"ordersInitialized": None}, {"isCached": True},
])
def test_stale_or_uninitialized_orders_refuse_the_target_card(tmp_path: Path, patch: dict[str, Any]) -> None:
    """#942 Codex review: 陈旧/缓存/委托未初始化的委托表不能当“没有挂单”用（也不能让一笔幽灵单挡住用户）。"""
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-orders-stale.sqlite3", Gateway(), provider)
    base = provider.account_activity()
    provider.account_activity = lambda: {**base, **patch}  # type: ignore[method-assign]
    response, card = _call(app, _tools(app, app.state.data_provider), target=2)
    assert card is None and _active_plan(app) is None
    assert _dump(response)["errorCode"] == "target_position_orders_unreadable"
    app.state.store.close()


async def test_the_target_tool_reads_positions_and_orders_inside_one_account_snapshot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#942 Codex review: 差额的持仓和挂单检查的委托出自同一次整账户读取（account=True 的组合快照）。"""
    from contextlib import asynccontextmanager

    from trader_runtime import order_planner

    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-one-snapshot.sqlite3", Gateway(), provider)
    tools = _tools(app, app.state.data_provider)
    seen: list[tuple[bool, bool]] = []

    @asynccontextmanager
    async def recording(prov, **kwargs):
        base_positions, base_activity = prov.positions, prov.account_activity
        prov.positions = lambda: (seen.append((True, kwargs.get("account", False))), base_positions())[1]  # type: ignore[method-assign]
        prov.account_activity = lambda: (seen.append((False, kwargs.get("account", False))), base_activity())[1]  # type: ignore[method-assign]
        # A real sidecar provider inside a combined account cut only has /v1/snapshot prefetched: any quote,
        # workbench or catalog read there fails "sidecar_read_not_prefetched" — card building must stay outside.
        market_names = [name for name in ("market_snapshot", "market_workbench", "instrument_catalog")
                        if hasattr(prov, name)]
        bases = {name: getattr(prov, name) for name in market_names}

        def not_prefetched(*args: Any, **kw: Any) -> Any:
            raise RuntimeError("sidecar_read_not_prefetched")

        for name in market_names:
            setattr(prov, name, not_prefetched)
        try:
            yield
        finally:
            prov.positions, prov.account_activity = base_positions, base_activity
            for name, base in bases.items():
                setattr(prov, name, base)

    monkeypatch.setattr(order_planner, "provider_read_snapshot", recording)
    session = app.state.broker.create_session("local", "native", USER)
    response, card = await tools._call_async(
        tool="propose_order",
        arguments=legacy_card_args("create_target_position_proposal", {"symbol": "SHFE.au2612", "targetNet": 2, "reason": "目标持仓"}),
        turn_id="turn-one-snapshot", mode="local", session_id=session.session_id, user_id=USER)
    assert card is not None and card[1]["side"] == "buy_open"
    assert seen == [(True, True), (False, True)]  # exactly one positions read then one orders read, both account=True
    app.state.store.close()


async def test_the_http_route_also_plans_inside_one_account_snapshot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#942 Codex review: 网页/CLI 走的 POST /api/simulation/target-position/proposals 与内置工具同一入口，
    同样在整账户组合快照里读持仓和委托。"""
    from contextlib import asynccontextmanager

    from trader_runtime import order_planner

    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-route-snapshot.sqlite3", Gateway(), provider)
    entered: list[dict[str, Any]] = []

    @asynccontextmanager
    async def recording(prov, **kwargs):
        entered.append(kwargs)
        yield

    monkeypatch.setattr(order_planner, "provider_read_snapshot", recording)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        session = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
        provider.quote_datetime = quote_now()
        response = await client.post("/api/simulation/target-position/proposals", json={
            "sessionId": session["sessionId"], "symbol": "SHFE.au2612", "targetNet": 2, "reason": "目标持仓"})
    assert response.status_code == 201 and response.json()["cardIssued"] is True
    assert entered and all(item.get("account") is True for item in entered)
    app.state.store.close()


async def test_the_quick_reverse_path_is_the_route_then_one_confirm(tmp_path: Path) -> None:
    """快捷区反手 (2026-10-24): POST target-position at −net answers the card's approval; one POST confirm on it,
    the close fills, and the relay sends the opening leg — nothing else from the client."""
    gateway = Gateway()
    provider = _provider_with([_long(1, today=1)])
    app = application(tmp_path / "target-quick-reverse.sqlite3", gateway, provider)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        session = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
        provider.quote_datetime = quote_now()
        answer = (await client.post("/api/simulation/target-position/proposals", json={
            "sessionId": session["sessionId"], "symbol": "SHFE.au2612", "targetNet": -1, "priceIntent": "aggressive",
            "reason": "快捷区反手"})).json()
        assert answer["cardIssued"] is True and answer["approvalId"] and answer["proposalVersion"] == 1
        assert answer["targetPosition"]["summary"].endswith("平多 1 手，成交后自动开空 1 手（确认一次）")
        confirmed = await client.post(f"/api/proposals/{answer['proposalId']}/confirm", json={
            "approvalId": answer["approvalId"], "proposalVersion": answer["proposalVersion"], "idempotencyKey": "quick-reverse"})
        assert confirmed.status_code == 200
    assert gateway.submit_count == 1
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await app.state.order_watch._sweep_target_plans()
    assert gateway.submit_count == 2
    assert (gateway.request["direction"], gateway.request["offset"], gateway.request["volume"]) == ("SELL", "OPEN", 1)
    app.state.store.close()


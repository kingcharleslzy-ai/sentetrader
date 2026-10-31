"""语音前台的归属与门控（#523 R1c、#838/#840/#851 审查，2026-10-13）：前台查询与下单类出卡在接纳时冻结会话/账户
绑定与代次，提交前复核（切会话、账户 A→B→A、撤权、挂断）；未念出的卡不可口头确认、作废经回读核实；技能门；
撤单与全平按点名的月份；免口头确认先确认再记录；目录 ok 但为空也回退。"""
from __future__ import annotations

import asyncio
import json

from fastapi import HTTPException

from test_voice_front_action import _action_app
from test_voice_realtime import WINDOW, _scripted_call, _voice_app
from trader_runtime.order_intent import OrderIntent
from trader_runtime.voice_front import FRONT_ACTION_ENV, FRONT_QUERY_ENV
from trader_runtime.voice_realtime import CallState


def moved(app, session_id: str) -> None:
    """The session is bound to another trading account now (a real switch: its account and its revision move)."""
    store = app.state.store
    with store.lock, store.connection:
        store.connection.execute(
            "UPDATE sessions SET trading_account_id = 'tqkq-sim:moved', account_revision = account_revision + 1 WHERE id = ?",
            (session_id,))


def repeated(app, session_id: str) -> None:
    """A switch to the account the session is already on (#932 follow-up): only the write-order revision moves."""
    store = app.state.store
    with store.lock, store.connection:
        store.connection.execute("UPDATE sessions SET account_revision = account_revision + 1 WHERE id = ?", (session_id,))

# The sentences these tests once fed the voice front's own regexes (negated / reported cancels, advice, unfinished
# corrections, side words inside a product name…) are rows of the shared corpus now (tests/intent_corpus).


async def test_an_unheard_card_is_never_confirmed_by_voice_and_a_failed_reject_keeps_it_so(tmp_path):
    app, service, state, session = await _action_app(tmp_path, "scope-unseen.sqlite3")
    outcome = await service.front_action(OrderIntent(act="open", product="沪金", dir="long", vol=1), state, "买一手沪金")
    proposal_id = outcome["card"][1]["proposalId"]
    # Made but not read out yet: the server's waiting-card lookup and a spoken 确认 skip it.
    assert service._server_pending(state) is None
    assert (await service._confirm(state))["error"] == "nothing_pending"

    async def broken_reject(item):  # the reject route fails (network, 5xx): the card must stay unconfirmable
        return None
    service._reject = broken_reject
    proposals = app.state.proposals
    owner_reject = proposals.reject

    async def broken_owner(*args, **kwargs):
        raise RuntimeError("store unavailable")
    proposals.reject = broken_owner
    assert await service.drop_action_card(state, outcome) is False
    assert proposal_id in state.unseen_cards and service._server_pending(state) is None
    # The route alone failing: the card's Owner rejects it — server state every later call sees.
    proposals.reject = owner_reject
    assert await service.drop_action_card(state, outcome) is True
    assert app.state.store.get(proposal_id)["status"] == "rejected"
    later_call = CallState(window_id=WINDOW, chat_session_id=session.session_id)
    assert service._server_pending(later_call) is None
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_card_is_retired_when_the_account_moved_or_was_revoked_before_it_was_read(tmp_path):
    app, service, state, session = await _action_app(tmp_path, "scope-retire.sqlite3")
    # The session moved to another account while the card was being made.
    outcome = await service.front_action(OrderIntent(act="open", product="沪金", dir="long", vol=1), state, "买一手沪金")
    original_account = app.state.store.get_session(session.session_id)["tradingAccountId"]
    moved(app, session.session_id)
    assert await service.adopt_action(state, "买一手沪金", outcome) is None
    assert app.state.store.get(outcome["card"][1]["proposalId"])["status"] == "rejected"
    assert state.pending is None
    assert not [e for _, e in app.state.store.list_events(session.session_id) if e["name"] == "proposal.trade"]
    app.state.store.set_session_trading_account(session.session_id, original_account, user_id=session.user_id)
    # Revoked: the account context refuses the user.
    second = await service.front_action(OrderIntent(act="open", product="沪金", dir="long", vol=1), state, "买一手沪金")
    ctx = app.state.app_capabilities.ctx
    original = ctx.account_context

    async def revoked(*args, **kwargs):
        raise HTTPException(403, "account_not_granted")
    ctx.account_context = revoked
    assert await service.adopt_action(state, "买一手沪金", second) is None
    ctx.account_context = original
    # The conversation moved (a hello / the window shows another session).
    third = await service.front_action(OrderIntent(act="open", product="沪金", dir="long", vol=1), state, "买一手沪金")
    state.session_gen += 1
    assert await service.adopt_action(state, "买一手沪金", third) is None
    # The call ended.
    fourth = await service.front_action(OrderIntent(act="open", product="沪金", dir="long", vol=1), state, "买一手沪金")
    state.closed = True
    assert await service.adopt_action(state, "买一手沪金", fourth) is None
    statuses = {app.state.store.get(o["card"][1]["proposalId"])["status"] for o in (second, third, fourth)}
    assert statuses == {"rejected"}
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_move_while_the_window_is_read_retires_the_sentence(tmp_path):
    app, service, state, session = await _action_app(tmp_path, "scope-accepted.sqlite3")
    accepted = service.binding(state)
    original_window = service._window

    async def window_then_move(s):
        moved(app, session.session_id)  # A → B lands during the read
        return await original_window(s)
    service._window = window_then_move
    outcome = await service.front_action(OrderIntent(act="open", product="沪金", dir="long", vol=1), state, "买一手沪金", accepted=accepted)
    assert outcome.get("retired") is True and outcome["card"] is None
    assert not app.state.store.connection.execute("SELECT 1 FROM proposals").fetchall()
    await app.state.shared_market.close()
    app.state.store.close()


async def test_the_card_goes_to_the_conversation_the_window_shows(tmp_path):
    """#851 review: an order spoken after the user switched conversation in the window lands there."""
    from test_voice_realtime import _register_window
    app, service, state, session = await _action_app(tmp_path, "scope-window.sqlite3")
    other = app.state.broker.create_session("local", "native", "local-user")
    await _register_window(app, chat_session_id=other.session_id, revision=1)
    outcome = await service.front_action(OrderIntent(act="open", product="沪金", dir="long", vol=1), state, "买一手沪金")
    assert outcome["binding"].session_id == other.session_id and state.session_gen == 1
    assert app.state.store.get(outcome["card"][1]["proposalId"])["sessionId"] == other.session_id
    await service.adopt_action(state, "买一手沪金", outcome)
    assert [e for _, e in app.state.store.list_events(other.session_id) if e["name"] == "proposal.trade"]
    assert not [e for _, e in app.state.store.list_events(session.session_id) if e["name"] == "proposal.trade"]
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_disabled_skill_makes_no_card_and_news_follows_the_web_research_gate(tmp_path):
    app, service, state, session = await _action_app(tmp_path, "scope-skill.sqlite3")
    app.state.store.set_skill_enabled("order-proposal", False)
    refused = await service.front_action(OrderIntent(act="open", product="沪金", dir="long", vol=1), state, "买一手沪金")
    assert refused["card"] is None and refused["error"] == "skill_unavailable" and refused["line"]
    assert not app.state.store.connection.execute("SELECT 1 FROM proposals").fetchall()
    app.state.store.set_skill_enabled("web-research", False)
    news = await service.run_tool("query", {"what": "news", "query": "沪金"}, state)
    assert news["error"] == "skill_unavailable" and news["say"]
    await app.state.shared_market.close()
    app.state.store.close()


class _FakeTools:
    """The card tools as the voice uses them: the account cut it scopes the sentence against, and the intents it
    hands the planner (captured); the planner answers with a refusal (no card)."""
    def __init__(self, rows, real):
        self.rows = rows
        self.intents = []
        self.outcome_response = real.outcome_response

    async def account_snapshot(self):
        from trader_runtime.order_planner import AccountSnapshot
        return AccountSnapshot.of(self.rows, None)

    async def plan_order(self, intent, **kwargs):
        from trader_runtime.order_planner import Refuse
        self.intents.append(intent)
        return Refuse("close_position_not_found", ("没有可平的持仓",))


async def test_a_named_month_limits_close_all_and_cancel_to_that_contract(tmp_path):
    from trader_runtime.trader_tools import TraderReadOnlyTools
    app, service, state, session = await _action_app(tmp_path, "scope-month.sqlite3")
    rows = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 1}, {"symbol": "SHFE.au2702", "direction": "long", "volume": 1}]
    tools = _FakeTools(rows, TraderReadOnlyTools(app.state.data_provider, app.state.adapter.runtime_status,
                                                 proposals=app.state.proposals))

    async def card_tools(session_id, identity):
        return tools
    service._card_tools = card_tools
    original = service._http
    working = {"clientOrderId": "c-12345678", "submitPhase": "CONFIRMED", "remoteStatus": "ALIVE", "cancelPhase": None,
               "request": {"symbol": "SHFE.au2612", "direction": "BUY", "offset": "OPEN", "volume": 1}}

    async def http(method, path, body=None, *, account_id=""):
        if path.startswith("/api/simulation/orders?"):
            return 200, {"items": [working]}
        return await original(method, path, body, account_id=account_id)
    service._http = http
    await service.front_action(OrderIntent(act="close_all", product="沪金", symbol="沪金2612"), state, "把沪金2612全平了")
    assert tools.intents[-1].symbols == ("SHFE.au2612",)
    await service.front_action(OrderIntent(act="close_all", product="沪金"), state, "把沪金全平了")
    assert tools.intents[-1].symbols == ("SHFE.au2612", "SHFE.au2702")
    # The only working 沪金 order is 2612: "撤掉沪金2702的单" must not cancel it.
    other_month = await service.front_action(OrderIntent(act="cancel", product="沪金", symbol="沪金2702"), state, "撤掉沪金2702的单")
    assert other_month.get("cancel") is None
    same = await service.front_action(OrderIntent(act="cancel", product="沪金", symbol="沪金2612"), state, "撤掉沪金2612的单")
    assert same["cancel"]["clientOrderId"] == "c-12345678"
    await app.state.shared_market.close()
    app.state.store.close()


async def test_with_voice_auto_confirm_the_record_says_what_happened_and_waits_out_the_continuation(tmp_path):
    app, service, state, session = await _action_app(tmp_path, "scope-auto.sqlite3")
    state.preferences = state.preferences.model_copy(update={
        "trade": state.preferences.trade.model_copy(update={"voiceOrderConfirm": False})})
    service._simulation_only = lambda: True
    outcome = await service.front_action(OrderIntent(act="open", product="沪金", dir="long", vol=1), state, "买一手沪金")
    waited = []

    async def settle():
        waited.append(True)
        return True
    line = await service.adopt_action(state, "买一手沪金", outcome, settle=settle)
    assert waited and line.endswith("已按你的设置直接下单。")
    events = [e for _, e in app.state.store.list_events(session.session_id)]
    said = [e["payload"] for e in events if e["name"] == "message.completed" and e["payload"].get("role") == "assistant"]
    assert said[-1]["text"] == line
    # One turn: the line and its card share the card's turn id; the card is never re-published after the confirm.
    card_id = outcome["card"][1]["proposalId"]
    assert said[-1]["turnId"] == outcome["card"][1]["turnId"]
    names = [e["name"] for e in events if (e["payload"] or {}).get("proposalId") == card_id]
    assert names.index("proposal.trade") < names.index("proposal.result")
    assert "proposal.trade" not in names[names.index("proposal.result"):]
    # A continuation came (settle says the turn moved on): nothing is confirmed, the card is dropped.
    second = await service.front_action(OrderIntent(act="open", product="沪金", dir="long", vol=1), state, "买一手沪金")

    async def moved():
        return False
    assert await service.adopt_action(state, "买一手沪金", second, settle=moved) is None
    assert app.state.store.get(second["card"][1]["proposalId"])["status"] == "rejected"
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_direct_cancel_follows_the_order_ops_gate_and_is_not_sent_once_the_account_moved(tmp_path):
    app, service, state, session = await _action_app(tmp_path, "scope-cancel-gate.sqlite3")
    working = {"clientOrderId": "c-12345678", "submitPhase": "CONFIRMED", "remoteStatus": "ALIVE", "cancelPhase": None,
               "request": {"symbol": "SHFE.au2612", "direction": "BUY", "offset": "OPEN", "volume": 1}}
    posts: list[str] = []
    original = service._http

    async def http(method, path, body=None, *, account_id=""):
        if path.startswith("/api/simulation/orders?"):
            return 200, {"items": [working]}
        if path.endswith("/cancel"):
            posts.append(path)
            return 200, {"displayStatus": "已撤销"}
        return await original(method, path, body, account_id=account_id)
    service._http = http
    app.state.store.set_skill_enabled("order-ops", False)
    refused = await service.front_action(OrderIntent(act="cancel"), state, "撤单")
    assert refused["error"] == "skill_unavailable" and refused.get("cancel") is None
    app.state.store.set_skill_enabled("order-ops", True)
    outcome = await service.front_action(OrderIntent(act="cancel"), state, "撤单")
    original_catalog = service._catalog

    async def catalog_then_switch(search=None):
        moved(app, session.session_id)  # the account moves while the order is named
        return await original_catalog(search)
    service._catalog = catalog_then_switch
    assert await service.adopt_action(state, "撤单", outcome) is None and not posts
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_failed_position_read_makes_no_close_card_and_a_partial_batch_says_what_it_leaves_out(tmp_path):
    from trader_runtime.voice_front import card_line
    app, service, state, session = await _action_app(tmp_path, "scope-positions.sqlite3")
    from trader_runtime.order_planner import AccountSnapshot
    real_card_tools = service._card_tools

    async def broken(session_id, identity):  # the account cut the planner scopes against cannot be read
        tools = await real_card_tools(session_id, identity)

        async def unreadable():
            return AccountSnapshot.unreadable()
        tools.account_snapshot = unreadable
        tools.plan_order = None  # never reached: no plan from a failed read
        return tools
    service._card_tools = broken
    refused = await service.front_action(OrderIntent(act="close", product="沪金", vol=1), state, "把沪金那一手平了")
    assert refused["card"] is None and "持仓数据暂时取不到" in refused["line"]
    assert not app.state.store.connection.execute("SELECT 1 FROM proposals").fetchall()
    batch = {"status": "approval_required", "approvalId": "a1", "totalOrders": 1,
             "orders": [{"symbol": "SHFE.rb2701", "name": "螺纹2701", "side": "sell_close", "volume": 1, "limitPrice": 3070.0,
                         "priceIntent": "aggressive"}],
             "skipped": [{"symbol": "SHFE.au2612", "reason": "quote_stale"}]}
    line = card_line(OrderIntent(act="close_all"), "proposal.close_batch", batch, {"shfe.au2612": "沪金2612"})
    assert "沪金2612（行情不是最新）这次平不了，不在这张卡里" in line and line.endswith("确认吗？")
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_new_target_waits_out_the_continuation_before_superseding_a_plan_in_progress(tmp_path):
    app, service, state, session = await _action_app(tmp_path, "scope-target.sqlite3")
    first = await service.front_action(OrderIntent(act="target", product="沪金", target=3), state, "沪金调到三手多单")
    await service.adopt_action(state, "沪金调到三手多单", first)
    plan = app.state.store.active_target_plan(user_id="local-user", symbol="SHFE.au2612")
    assert plan is not None

    async def moved_on():
        return False
    held = await service.front_action(OrderIntent(act="target", product="沪金", target=0), state, "沪金目标持仓清零",
                                      settle=moved_on)
    assert held.get("retired") is True and held["card"] is None
    assert app.state.store.active_target_plan(user_id="local-user", symbol="SHFE.au2612")["id"] == plan["id"]
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_sentence_of_legs_on_one_contract_waits_out_the_continuation_before_replacing_a_plan(tmp_path):
    """#932 A10：一句多单在同一合约上先平再开（“平掉沪金多单，再开多两手沪金”）由规划器接力成一个新的目标持仓计划，
    会作废这个合约上进行中的旧计划——和目标持仓一样，先等这句话说完（续说、“算了”）再规划；另一个品种的多单不等。"""
    app, service, state, session = await _action_app(tmp_path, "scope-legs.sqlite3")
    first = await service.front_action(OrderIntent(act="target", product="沪金", target=3), state, "沪金调到三手多单")
    await service.adopt_action(state, "沪金调到三手多单", first)
    plan = app.state.store.active_target_plan(user_id="local-user", symbol="SHFE.au2612")
    assert plan is not None
    waited: list[str] = []

    async def moved_on():
        waited.append("settle")
        return False
    legs = OrderIntent(act="close_all", legs=(OrderIntent(act="close_all", product="沪金", dir="long"),
                                              OrderIntent(act="open", product="沪金", dir="long", vol=2)))
    held = await service.front_action(legs, state, "平掉沪金多单，再开多两手沪金", settle=moved_on)
    assert waited and held.get("retired") is True and held["card"] is None
    assert app.state.store.active_target_plan(user_id="local-user", symbol="SHFE.au2612")["id"] == plan["id"]
    other = OrderIntent(act="open", legs=(OrderIntent(act="open", product="沪金", dir="long", vol=1),
                                          OrderIntent(act="open", product="螺纹", dir="long", vol=1)))
    waited.clear()
    await service.front_action(other, state, "买一手沪金，再买一手螺纹", settle=moved_on)
    assert not waited  # two openings never replace a plan: no wait
    # PR #997 Codex: the planner's books are exact contracts — 平 au2612 + 开 au2706 are two independent cards, no relay
    # replaces the au2612 plan, so nothing waits; the same month twice does.
    binding = service.binding(state)

    def legs_on(*symbols):
        legs = (OrderIntent(act="close", symbol=symbols[0], dir="long", vol=1),
                OrderIntent(act="open", symbol=symbols[1], dir="long", vol=1))
        return OrderIntent(act="close", legs=legs), [{"symbol": symbol} for symbol in symbols]
    action, read = legs_on("SHFE.au2612", "SHFE.au2706")
    assert await service._may_replace_plan(action, action, read, "SHFE.au2612", state, binding, None) is False
    action, read = legs_on("SHFE.au2612", "SHFE.au2612")
    assert await service._may_replace_plan(action, action, read, "SHFE.au2612", state, binding, None) is True
    # A close named by its product reaches the month held; when that cannot be read now, the sentence still settles
    # first — the planner reads the account again (PR #997 review).
    from trader_runtime.order_planner import AccountSnapshot

    class Unreadable:
        async def account_snapshot(self):
            return AccountSnapshot.unreadable()
    by_product = OrderIntent(act="close_all", legs=(OrderIntent(act="close_all", symbol="SHFE.au2701", product="au"),
                                                    OrderIntent(act="open", symbol="SHFE.au2701", dir="long", vol=1)))
    read = [{"symbol": "SHFE.au2701"}, {"symbol": "SHFE.au2701"}]
    assert await service._may_replace_plan(by_product, by_product, read, "SHFE.au2701", state, binding, Unreadable()) is True
    await app.state.shared_market.close()
    app.state.store.close()


async def test_an_empty_catalog_answer_falls_back_to_the_shared_catalog(tmp_path, monkeypatch):
    """#840 review r4255069324: ok:true with no items is the failure too."""
    app, _, service = await _voice_app(tmp_path, "scope-catalog.sqlite3")
    original = service._call

    async def empty(operation, **kwargs):
        if operation == "GET /api/instruments" and not (kwargs.get("query") or {}).get("search"):
            return {"ok": True, "status": 200, "data": {"items": []}}
        return await original(operation, **kwargs)
    monkeypatch.setattr(service, "_call", empty)
    context = await service.asr_context()
    assert "沪金" in [item["word"] for item in context["hotwords"]]
    await app.state.shared_market.close()
    app.state.store.close()


# -- in the call: a front query accepted on one conversation is not delivered to another --------------

async def _query_call(monkeypatch, tmp_path, name, utterances, *, between=None):
    """Doubao finalises each utterance and ends its turn silently (the Runtime reads the query itself);
    `between(app, session, inbound)` runs after the first final, before its result is delivered."""
    monkeypatch.setenv(FRONT_QUERY_ENV, "local-user")
    monkeypatch.delenv(FRONT_ACTION_ENV, raising=False)
    app, _, service = await _voice_app(tmp_path, name)
    session = app.state.broker.create_session("local", "native", "local-user")
    queue = list(utterances)
    holder: dict = {}
    original_tool = service.run_tool

    async def slow_tool(tool, arguments, state, **kwargs):
        result = await original_tool(tool, arguments, state, **kwargs)
        if between is not None and not holder.get("done"):
            holder["done"] = True
            await between(app, session, holder["inbound"])
            await asyncio.sleep(0.2)
        return result
    service.run_tool = slow_tool

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append" and queue and not holder.get("started"):
            holder["started"] = True
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": queue.pop(0)})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started", "tts_type": "chat_tts_text", "response_id": "rt"})
            await emit({"type": "response.output_audio.done", "response_id": "rt"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
            if queue:
                await asyncio.sleep(0.05)
                await emit({"type": "conversation.item.input_audio_transcription.completed", "text": queue.pop(0)})
                await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    holder["inbound"] = inbound
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    return app, session, service, upstream, client_out, run


async def test_a_query_result_is_not_delivered_after_the_call_moved_to_another_conversation(tmp_path, monkeypatch):
    other: dict = {}

    async def move(app, session, inbound):
        other["session"] = app.state.broker.create_session("local", "native", "local-user")
        await inbound.put({"type": "websocket.receive", "text": json.dumps(
            {"type": "hello", "windowId": WINDOW, "sessionId": other["session"].session_id})})
    app, session, service, upstream, client_out, run = await _query_call(
        monkeypatch, tmp_path, "scope-move.sqlite3", ["沪金现在多少钱？"], between=move)
    await run(service, until=lambda: False, timeout=1.0)  # the read, the move and the retirement take ~0.3 s
    timeline = client_out[-1]["timeline"]
    assert any(e["event"] == "frontRetired" for e in timeline) and any(e["event"] == "sessionMoved" for e in timeline)
    assert not any(p["type"] == "speech_text_buffer.commit" for p in upstream)  # not spoken
    for sid in (session.session_id, other["session"].session_id):  # recorded in neither conversation
        assert not [e for _, e in app.state.store.list_events(sid) if e["name"] == "message.completed"
                    and e["payload"].get("origin") == "voice"]
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_query_result_survives_a_repeat_switch_of_its_account_but_not_a_real_switch(tmp_path, monkeypatch):
    """#932 follow-up (#1003 Codex 4309759897): a switch to the account the session is already on (reminder navigation
    sends one) only moves the write-order revision — the answer is still spoken; a real switch still retires it."""
    async def same_account(app, session, inbound):
        repeated(app, session.session_id)
    app, session, service, upstream, client_out, run = await _query_call(
        monkeypatch, tmp_path, "scope-repeat.sqlite3", ["沪金现在多少钱？"], between=same_account)
    await run(service, until=lambda: any(p["type"] == "speech_text_buffer.commit" for p in upstream), timeout=3)
    assert not any(e["event"] == "frontRetired" for e in client_out[-1]["timeline"])
    assert any(p["type"] == "speech_text_buffer.commit" for p in upstream)
    await app.state.shared_market.close()
    app.state.store.close()

    async def real_switch(app, session, inbound):
        moved(app, session.session_id)
    app, session, service, upstream, client_out, run = await _query_call(
        monkeypatch, tmp_path, "scope-switch.sqlite3", ["沪金现在多少钱？"], between=real_switch)
    await run(service, until=lambda: False, timeout=1.5)
    assert any(e["event"] == "frontRetired" for e in client_out[-1]["timeline"])
    assert not any(p["type"] == "speech_text_buffer.commit" for p in upstream)
    await app.state.shared_market.close()
    app.state.store.close()


async def test_it_asks_for_the_product_just_named_when_the_next_sentence_says_it(tmp_path, monkeypatch):
    """#838 review r4254730768: "黄金有什么新闻" then "它多少钱" asks about 沪金 (the catalog name)."""
    app, session, service, upstream, client_out, run = await _query_call(
        monkeypatch, tmp_path, "scope-pronoun.sqlite3", ["黄金有什么新闻？", "它现在多少钱？"])
    service.product_names = ["沪金", "螺纹钢"]
    await run(service, until=lambda: sum(1 for e in client_out if e.get("type") == "tool_result" and e.get("name") == "query") >= 2,
              timeout=5)
    asked = [e["arguments"] for e in client_out if e.get("type") == "tool_result" and e.get("name") == "query"]
    assert asked[1] == {"what": "quote", "query": "沪金"}
    await app.state.shared_market.close()
    app.state.store.close()

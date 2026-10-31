"""语音第 2 步：下单类直接出卡（AI_TRADER_VOICE_FRONT_ACTION，2026-10-13）。从原话判定出哪张卡、各类出卡与拒绝、
豆包工具轮里同轮念卡、豆包先开口或不调工具时 Runtime 自己念、卡片生成中说“算了”与续句丢卡、念完卡“不做了”放弃、
开关关时与之前一致。"""
from __future__ import annotations

import asyncio
import json

import pytest

from test_voice_realtime import WINDOW, _scripted_call, _voice_app
from trader_runtime.agent_api import CHAT_SEND
from trader_runtime.order_intent import OrderIntent, Price
from trader_runtime.order_intent_parse import ParseContext, parse
from trader_runtime.voice_front import (
    ACTION_TOOL,
    FRONT_ACTION_ENV,
    FRONT_ACTION_INSTRUCTIONS,
    FRONT_INSTRUCTIONS,
    FRONT_QUERY_ENV,
    card_line,
    front_action_enabled,
    front_order,
    refusal_line,
    reopen_words,
)
from trader_runtime.voice_realtime import FRONT_ACTION_TOOLS, FRONT_TOOLS, CallState

NAMES = ("螺纹", "沪金", "沪银", "白糖", "铁矿", "焦炭", "原油")


def test_the_switch_is_off_unless_named(monkeypatch):
    monkeypatch.delenv(FRONT_ACTION_ENV, raising=False)
    assert not front_action_enabled("owner-1")
    monkeypatch.setenv(FRONT_ACTION_ENV, "owner-1")
    assert front_action_enabled("owner-1") and not front_action_enabled("owner-2")


# What each sentence means is the parser's (tests/intent_corpus); here only which readings the front desk cards itself.
@pytest.mark.parametrize(("text", "carded"), [
    ("帮我买一手螺纹", True), ("螺纹做到多两手", True), ("刚才那个单撤了", True), ("全平", True), ("螺纹反手", True),
    ("平一半螺纹", True),  # 平一半 (owner 2026-10-16): the planner floors it, at least one lot
    ("平掉一手螺纹今仓", True), ("挂单价把螺纹全平", True),  # 今昨 and the batch's price: the planner carries them
    ("买两手螺纹然后买一手沪金", True),  # 一句多单 (owner 2026-10-16): each clause its own card, in order
    ("来一手螺纹", False), ("开三手螺纹", False), ("螺纹做多", False),  # no side, or no lots: the trading AI asks
    ("撤单之后再买一手", False),  # a cancel among the legs: the trading AI does it step by step
    ("螺纹跌到3000就买一手", False), ("白糖能不能买", False), ("不要全部平仓", False), ("我刚买了一手螺纹", False),
    ("买一手螺纹和沪金", False), ("开三手螺纹多单止损3100", False), ("开三手，不对，两手沪金多单，算了。", False),
])
def test_the_front_desk_cards_only_a_complete_order_the_parser_read(text, carded):
    assert (front_order(parse(text, ParseContext(products=NAMES))) is not None) is carded


def test_the_brief_and_tools_are_the_front_desks_plus_action():
    assert [tool["name"] for tool in FRONT_ACTION_TOOLS] == ["confirm", "cancel", "read_screen", "query", "action"]
    assert FRONT_ACTION_TOOLS[:4] == FRONT_TOOLS
    assert "action" in FRONT_ACTION_INSTRUCTIONS and "action" not in FRONT_INSTRUCTIONS
    # Doubao's context window is 12K: the brief and the tool texts stay short.
    assert len(FRONT_ACTION_INSTRUCTIONS) <= 450 and len(ACTION_TOOL["description"]) <= 60
    assert all(len(spec.get("description") or "") <= 60 for spec in ACTION_TOOL["parameters"]["properties"].values())


def test_the_card_line_says_contract_side_lots_and_price_then_asks():
    open_card = {"name": "螺纹2701", "side": "buy_open", "volume": 1, "limitPrice": 3078.0, "priceIntent": "aggressive",
                 "status": "approval_required", "approvalId": "a1"}
    assert card_line(OrderIntent(act="open", product="螺纹", dir="long", vol=1), "proposal.trade", open_card) == "螺纹2701，买开 1 手，对手价 3078，确认吗？"
    own = {**open_card, "limitPrice": 820.0, "priceIntent": "last"}
    assert card_line(OrderIntent(act="open", product="沪金", dir="long", vol=1, price=Price(limit=820.0)), "proposal.trade", own).endswith("限价 820，确认吗？")
    batch = {"status": "approval_required", "approvalId": "a2", "totalOrders": 2, "orders": [
        {"name": "螺纹2701", "side": "sell_close", "volume": 1, "positionBucket": "today", "limitPrice": 3070.0, "priceIntent": "aggressive"},
        {"name": "螺纹2701", "side": "sell_close", "volume": 1, "positionBucket": "historical", "limitPrice": 3070.0, "priceIntent": "aggressive"}]}
    assert card_line(OrderIntent(act="close_all", product="螺纹"), "proposal.close_batch", batch) == \
        "2 张平仓单：螺纹2701，卖平 1 手（平今），对手价 3070；螺纹2701，卖平 1 手（平昨），对手价 3070，确认吗？"
    target = {**open_card, "side": "sell_open", "volume": 2, "targetPosition": {"targetNet": -2, "currentNet": 0, "totalSteps": 1, "legs": [{}]}}
    assert card_line(OrderIntent(act="target", product="螺纹", target=-2), "proposal.trade", target) == \
        "螺纹调到空 2 手，现在0 手。螺纹2701，卖开 2 手，对手价 3078，确认吗？"
    blocked = {**open_card, "status": "risk_blocked", "approvalId": None, "riskCheck": {"passed": False, "reason": "可用资金不足"}}
    assert card_line(OrderIntent(act="open", product="螺纹", dir="long", vol=1), "proposal.trade", blocked) == "这单下不了：可用资金不足。"


def test_a_refusal_is_the_tools_own_words_and_a_closed_market_says_when_it_opens():
    from datetime import datetime

    from trader_runtime.voice_front import CHINA
    assert refusal_line(["当前没有螺纹钢2610（SHFE.rb2610）的持仓，平不了"]) == "当前没有螺纹钢2610的持仓，平不了。"
    afternoon = datetime(2026, 10, 13, 16, 0, tzinfo=CHINA)
    assert reopen_words(has_night=True, exchange="SHFE", now=afternoon) == "晚上九点开夜盘"
    assert reopen_words(has_night=False, exchange="CZCE", now=afternoon) == "下个交易日上午九点开盘"
    assert refusal_line([], label="螺纹", reopen="晚上九点开夜盘") == "螺纹现在不在交易时段，晚上九点开夜盘才能下单。"


# -- the service: the card through the trading AI's own card tools ---------------------------------

async def _action_app(tmp_path, name):
    app, gateway, service = await _voice_app(tmp_path, name)
    session = app.state.broker.create_session("local", "native", "local-user")
    state = CallState(window_id=WINDOW, chat_session_id=session.session_id)
    service.product_names = ["沪金"]
    return app, service, state, session


async def test_an_open_card_is_the_card_tools_own_card_and_joins_the_conversation_when_read(tmp_path):
    app, service, state, session = await _action_app(tmp_path, "action-open.sqlite3")
    outcome = await service.front_action(OrderIntent(act="open", product="沪金", dir="long", vol=1, price=Price(mode="counter")), state, "买一手沪金对手价")
    name, card = outcome["card"]
    assert name == "proposal.trade" and card["symbol"] == "SHFE.au2612" and card["side"] == "buy_open" and card["volume"] == 1
    assert card["origin"] == "ai" and card["readiness"]["passed"] is True  # the card tool's own readiness gate ran
    assert outcome["line"].startswith("沪金2612，买开 1 手，对手价") and outcome["line"].endswith("确认吗？")
    # Nothing is published before the line is read.
    assert not [e for _, e in app.state.store.list_events(session.session_id) if e["name"].startswith("proposal.")]
    line = await service.adopt_action(state, "买一手沪金对手价", outcome)
    assert line == outcome["line"]
    # Published, but not yet the call's waiting card: a "好" before the read-out is committed confirms nothing.
    assert state.pending is None and service._server_pending(state) is None
    service.arm_action_card(state, outcome)  # the read-out is committed (speak / inject)
    assert state.pending["proposals"][0]["proposalId"] == card["proposalId"]
    assert service._server_pending(state)["proposals"][0]["proposalId"] == card["proposalId"]
    events = [(e["name"], e["payload"]) for _, e in app.state.store.list_events(session.session_id)]
    assert [n for n, _ in events][-3:] == ["message.completed", "message.completed", "proposal.trade"]
    assert events[-3][1]["text"] == "买一手沪金对手价" and events[-3][1]["origin"] == "voice" and events[-2][1]["text"] == line
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_close_takes_the_held_direction_and_the_batch_and_target_cards_come_from_their_tools(tmp_path):
    app, service, state, session = await _action_app(tmp_path, "action-close.sqlite3")
    # Long and short are both held (the test account): which one is the user's to say.
    assert (await service.front_action(OrderIntent(act="close", product="沪金", vol=1), state, "平一手沪金"))["delegate"] is True
    close = await service.front_action(OrderIntent(act="close", product="沪金", dir="long", vol=1), state, "平掉一手沪金多单")
    assert close["card"][1]["side"] == "sell_close" and close["card"][1]["volume"] == 1
    batch = await service.front_action(OrderIntent(act="close_all", product="沪金", dir="short"), state, "沪金空单都平了")
    assert batch["card"][0] == "proposal.close_batch" and {o["side"] for o in batch["card"][1]["orders"]} == {"buy_close"}
    target = await service.front_action(OrderIntent(act="target", product="沪金", target=3), state, "沪金调到三手多单")
    assert target["card"][1]["targetPosition"]["targetNet"] == 3 and "调到多 3 手" in target["line"]
    # Too many lots to close: the tool's own refusal, no card.
    refused = await service.front_action(OrderIntent(act="close", product="沪金", dir="long", vol=9), state, "平掉九手沪金多单")
    assert refused["card"] is None and "平不了" in refused["line"] and "SHFE" not in refused["line"]
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_cancel_takes_only_the_one_working_order_through_the_order_panels_route(tmp_path):
    app, service, state, session = await _action_app(tmp_path, "action-cancel.sqlite3")
    nothing = await service.front_action(OrderIntent(act="cancel"), state, "刚才那个单撤了")
    assert nothing["line"] == "现在没有挂着没成交的委托。" and nothing.get("cancel") is None
    working = {"clientOrderId": "c-12345678", "submitPhase": "CONFIRMED", "remoteStatus": "ALIVE", "cancelPhase": None,
               "request": {"symbol": "SHFE.au2612", "direction": "BUY", "offset": "OPEN", "volume": 1, "limitPrice": 820.0}}
    calls: list[tuple[str, str]] = []
    original = service._http

    async def http(method, path, body=None, *, account_id=""):
        calls.append((method, path))
        if path.startswith("/api/simulation/orders?"):
            return 200, {"items": [working, {**working, "clientOrderId": "c-done", "remoteStatus": "FINISHED"}]}
        if path.endswith("/cancel"):
            return 200, {"displayStatus": "已撤销"}
        return await original(method, path, body, account_id=account_id)
    service._http = http
    one = await service.front_action(OrderIntent(act="cancel", product="沪金"), state, "沪金那个单撤了")
    assert one["cancel"]["clientOrderId"] == "c-12345678"
    assert not any(path.endswith("/cancel") for _, path in calls)  # looked up only; sent when the round is read
    line = await service.adopt_action(state, "沪金那个单撤了", one)
    assert line == "沪金2612 买开 1 手的挂单已撤销。"
    assert ("POST", "/api/simulation/orders/c-12345678/cancel") in calls
    two = {"items": [working, {**working, "clientOrderId": "c-87654321"}]}

    async def two_working(method, path, body=None, *, account_id=""):
        return (200, two) if path.startswith("/api/simulation/orders?") else await original(method, path, body, account_id=account_id)
    service._http = two_working
    assert (await service.front_action(OrderIntent(act="cancel"), state, "撤单"))["delegate"] is True
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_dropped_card_is_rejected_and_never_published(tmp_path):
    app, service, state, session = await _action_app(tmp_path, "action-drop.sqlite3")
    outcome = await service.front_action(OrderIntent(act="open", product="沪金", dir="short", vol=2), state, "空两手沪金")
    assert await service.drop_action_card(state, outcome) is True
    assert app.state.store.get(outcome["card"][1]["proposalId"])["status"] == "rejected"
    # The card itself never reached the conversation (only the reject route's own result line).
    assert not [e for _, e in app.state.store.list_events(session.session_id) if e["name"] in {"proposal.trade", "proposal.close_batch"}]
    await app.state.shared_market.close()
    app.state.store.close()


# -- the call: in Doubao's tool turn, or read by the Runtime -----------------------------------------

ORDER: list[str] = []  # the fake upstream's own event order (cancel done vs the Runtime's commit)

def _action_call(monkeypatch, utterances, *, tool_call=True, reply_text=None, second_after=None, cancel_done_after=0.0):
    """Doubao finalises each utterance in turn (the next one `second_after` seconds after the first, or when
    the Runtime has spoken); for an order it calls `action` (or says `reply_text` first)."""
    monkeypatch.setenv(FRONT_QUERY_ENV, "local-user")
    monkeypatch.setenv(FRONT_ACTION_ENV, "local-user")
    queue = list(utterances)

    async def say_next(emit):
        text = queue.pop(0)
        await emit({"type": "conversation.item.input_audio_transcription.completed", "text": text})
        await asyncio.sleep(0.05)
        if reply_text is not None:
            await emit({"type": "response.output_text.delta", "delta": reply_text})
            await emit({"type": "response.output_audio.started", "tts_type": "default"})
        elif tool_call and text.rstrip("。") not in {"算了", "不做了"}:
            await emit({"type": "response.function_call_arguments.done", "items": [
                {"call_id": "c1", "name": "action", "arguments": json.dumps({"kind": "open", "direction": "long", "product": "螺纹", "volume": 1})}]})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
        else:
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append" and queue and not getattr(on_send, "started", False):
            on_send.started = True
            await say_next(emit)
            if second_after is not None and queue:
                await asyncio.sleep(second_after)
                await say_next(emit)
        elif kind == "response.cancel":
            await emit({"type": "response.canceled"})
            if cancel_done_after:
                async def done_later():  # the cancelled turn ends a while later (production ~0.5 s)
                    await asyncio.sleep(cancel_done_after)
                    ORDER.append("cancel_done")
                    await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
                asyncio.ensure_future(done_later())
            else:
                await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
        elif kind == "speech_text_buffer.replacement.commit" or kind == "speech_text_buffer.commit":
            ORDER.append(kind)
            tts = "default" if kind.startswith("speech_text_buffer.replacement") else "chat_tts_text"
            await emit({"type": "response.output_audio.started", "tts_type": tts, "response_id": "r" + str(len(queue))})
            # Like production: audio frames carry no response id of their own.
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done", "response_id": "r" + str(len(queue))})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
            if queue and second_after is None:
                await asyncio.sleep(0.05)
                await say_next(emit)
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    return _scripted_call(monkeypatch, on_send)


async def _run_action(monkeypatch, tmp_path, name, utterances, *, until, **script):
    app, _, service = await _voice_app(tmp_path, name)
    service.product_names = ["沪金"]
    session = app.state.broker.create_session("local", "native", "local-user")
    chat_sends = []
    original = service._call

    async def call(operation, **kwargs):
        if operation == CHAT_SEND:
            chat_sends.append(kwargs.get("body"))
        return await original(operation, **kwargs)
    service._call = call
    upstream, client_out, inbound, run = _action_call(monkeypatch, utterances, **script)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: until(upstream, client_out))
    events = [(e["name"], e["payload"]) for _, e in app.state.store.list_events(session.session_id)]
    proposals = {row["id"]: row["status"] for row in app.state.store.connection.execute("SELECT id, status FROM proposals").fetchall()}
    timeline = client_out[-1]["timeline"]
    await app.state.shared_market.close()
    app.state.store.close()
    return upstream, client_out, events, proposals, timeline, chat_sends


def _turn_mode(monkeypatch):
    """AI_TRADER_VOICE_ACTION_SPEAK=turn (measurement switch): Doubao's own tool turn reads the card."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "ACTION_SPEAK_DIRECT", False)


async def test_by_default_the_runtime_reads_the_card_at_once_and_doubaos_own_reply_is_dropped(tmp_path, monkeypatch):
    """Default: the line is committed as soon as the card is made (measured 0.75 s sooner than Doubao's tool
    turn); Doubao's `action` call for the same final is answered with nothing and muted."""
    upstream, client_out, events, proposals, timeline, chat_sends = await _run_action(
        monkeypatch, tmp_path, "call-direct.sqlite3", ["空两手沪金。"],
        until=lambda up, out: any(e.get("event") == "lateToolDropped" for e in (out[-1].get("timeline") or []))
        if out and out[-1].get("type") == "closed" else any(p["type"] == "speech_text_buffer.commit" for p in up) and len(up) > 6)
    commit = next(p for p in upstream if p["type"] == "speech_text_buffer.commit")
    assert commit["text"].startswith("沪金2612，卖开 2 手") and commit["text"].endswith("确认吗？")
    assert not any(p["type"] == "speech_text_buffer.replacement.commit" for p in upstream)
    assert next(e for e in timeline if e["event"] == "actionRead")["inTurn"] is False
    assert any(e["event"] == "lateToolDropped" for e in timeline) and not chat_sends
    assert not any(p["type"] == "session.update" for p in upstream)
    # 10-13 production: the line's frames (no response id of their own) were dropped while Doubao's late
    # `action` turn was being cancelled — whichever came first, none of the line is dropped.
    assert next(e for e in timeline if e["event"] == "audioPath")["dropFrames"] == 0


async def test_the_line_waits_until_doubaos_cancelled_tool_turn_is_over(tmp_path, monkeypatch):
    """10-13 production: Doubao's `action` call came 0.08 s after the final, its cancel was still open when the
    card line was committed, and every frame of the line was dropped — the user heard nothing, then
    "没有听到确认，已放弃". The read now waits for the cancelled turn's response.done."""
    ORDER.clear()
    import trader_runtime.voice_realtime as module
    original = module.VoiceRealtimeService.front_action

    async def slower(self, *args, **kwargs):  # the card is ready after Doubao's call (production: 0.29 s vs 0.08 s)
        await asyncio.sleep(0.2)
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(module.VoiceRealtimeService, "front_action", slower)
    upstream, client_out, events, proposals, timeline, chat_sends = await _run_action(
        monkeypatch, tmp_path, "call-cancel-wait.sqlite3", ["空两手沪金。"], cancel_done_after=0.3,
        until=lambda up, out: any(p["type"] == "speech_text_buffer.commit" for p in up))
    assert ORDER.index("cancel_done") < ORDER.index("speech_text_buffer.commit")
    assert any(e["event"] == "actionCancelWait" for e in timeline)
    dropped = next(e for e in timeline if e["event"] == "audioPath")
    assert dropped["dropFrames"] == 0  # the line's own audio was forwarded


async def test_an_in_turn_line_whose_commit_failed_is_said_again_on_the_live_session(tmp_path, monkeypatch):
    """PR #997 review: in Doubao's tool turn the card line goes out as replacement.append + commit. The append was sent,
    the commit failed: the line was never said. It joins the unheard lines and the upstream watchdog says it again on
    the replaced session — the caller is never left with silence and a card stuck unasked."""
    _turn_mode(monkeypatch)
    import sys

    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "UPSTREAM_REPLY_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(module, "UPSTREAM_WATCH_TICK_SECONDS", 0.02)
    monkeypatch.setattr(module, "UPSTREAM_RECONNECT_BACKOFF_SECONDS", (0.0, 0.05, 0.05))
    here = sys.modules[__name__]
    scripted, failed = here._scripted_call, []

    def failing_commit(patch, on_send):
        async def wrapped(payload, emit):
            if payload["type"] == "speech_text_buffer.replacement.commit" and not failed:
                failed.append(True)
                raise ConnectionError("upstream dropped")
            await on_send(payload, emit)
        return scripted(patch, wrapped)
    monkeypatch.setattr(here, "_scripted_call", failing_commit)
    upstream, client_out, events, proposals, timeline, chat_sends = await _run_action(
        monkeypatch, tmp_path, "call-inject-failed.sqlite3", ["空两手沪金。"],
        until=lambda up, out: any(p["type"] == "speech_text_buffer.commit" for p in up))
    line = "".join(p["text"] for p in upstream if p["type"] == "speech_text_buffer.replacement.append")
    assert failed and line.startswith("沪金2612，卖开 2 手")
    said_again = [p["text"] for p in upstream if p["type"] == "speech_text_buffer.commit"]
    assert said_again and said_again[0] == line


async def test_an_order_is_read_in_doubaos_tool_turn_with_the_runtimes_line_and_never_reaches_the_trading_ai(tmp_path, monkeypatch):
    _turn_mode(monkeypatch)
    upstream, client_out, events, proposals, timeline, chat_sends = await _run_action(
        monkeypatch, tmp_path, "call-open.sqlite3", ["空两手沪金。"],
        until=lambda up, out: any(p["type"] == "speech_text_buffer.replacement.commit" for p in up))
    created = next(p for p in upstream if p["type"] == "session.create")
    assert [tool["name"] for tool in created["session"]["tools"]] == ["confirm", "cancel", "read_screen", "query", "action"]
    assert created["session"]["instructions"].startswith(FRONT_ACTION_INSTRUCTIONS)
    # Declared for the whole call: never re-mounted mid-call (session.update cut the ASR segment, 10-13 probe).
    assert not any(p["type"] == "session.update" for p in upstream)
    assert not chat_sends and not any(p["type"] == "response.cancel" for p in upstream)
    line = "".join(p["text"] for p in upstream if p["type"] == "speech_text_buffer.replacement.append")
    # Doubao asked for a long 螺纹; the words said 空两手沪金 — the card is that.
    assert line.startswith("沪金2612，卖开 2 手") and line.endswith("确认吗？")
    tool_reply = next(p for p in upstream if p["type"] == "conversation.item.create" and p["items"][0].get("role") == "tool")
    assert json.loads(tool_reply["items"][0]["content"][0]["text"])["spoken"] == line
    assert next(e for e in timeline if e["event"] == "routed")["by"] == "action"
    assert next(e for e in timeline if e["event"] == "actionRead")["inTurn"] is True
    names = [n for n, _ in events]
    first_card = names.index("proposal.trade")
    assert names[first_card - 2:first_card] == ["message.completed", "message.completed"]
    assert [p["text"] for n, p in events[first_card - 2:first_card]] == ["空两手沪金。", line]
    # The call ended with the card unanswered: it is dropped like any card left at hang-up.
    assert list(proposals.values()) == ["rejected"]


async def test_doubao_talking_first_is_cancelled_and_the_runtime_reads_the_card(tmp_path, monkeypatch):
    _turn_mode(monkeypatch)
    upstream, client_out, events, proposals, timeline, chat_sends = await _run_action(
        monkeypatch, tmp_path, "call-talk.sqlite3", ["空两手沪金。"], reply_text="好的，帮你下单。",
        until=lambda up, out: any(p["type"] == "speech_text_buffer.commit" for p in up))
    kinds = [p["type"] for p in upstream]
    assert kinds.index("response.cancel") < kinds.index("speech_text_buffer.commit")
    assert next(p for p in upstream if p["type"] == "speech_text_buffer.commit")["text"].startswith("沪金2612，卖开 2 手")
    assert "好的，帮你下单。" not in [e.get("delta") for e in client_out if e.get("type") == "text"]
    assert any(e["event"] == "actionFallback" for e in timeline) and not chat_sends


async def test_doubao_staying_silent_hands_the_read_to_the_runtime(tmp_path, monkeypatch):
    _turn_mode(monkeypatch)
    upstream, client_out, events, proposals, timeline, chat_sends = await _run_action(
        monkeypatch, tmp_path, "call-silent.sqlite3", ["空两手沪金。"], tool_call=False,
        until=lambda up, out: any(p["type"] == "speech_text_buffer.commit" for p in up))
    assert any(e["event"] == "actionSilent" for e in timeline)
    assert next(p for p in upstream if p["type"] == "speech_text_buffer.commit")["text"].startswith("沪金2612，卖开 2 手")


async def test_saying_buzuole_after_the_card_drops_it(tmp_path, monkeypatch):
    upstream, client_out, events, proposals, timeline, chat_sends = await _run_action(
        monkeypatch, tmp_path, "call-cancel.sqlite3", ["空两手沪金。", "不做了。"],
        until=lambda up, out: any(e.get("type") == "tool_result" and e.get("name") == "cancel" for e in out))
    assert list(proposals.values()) == ["rejected"] and not chat_sends
    assert [e["action"] for e in timeline if e["event"] == "routed"] == ["action", "cancel"]


async def test_suanle_while_the_card_is_being_made_stops_and_rejects_it(tmp_path, monkeypatch):
    """"算了" before the line was read: the round stops (已停止) and the card it made is rejected unseen."""
    import trader_runtime.voice_realtime as module
    original = module.VoiceRealtimeService.front_action

    async def slow(self, *args, **kwargs):
        await asyncio.sleep(0.3)
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(module.VoiceRealtimeService, "front_action", slow)
    seen: list[float] = []

    def stopped(up, out):
        if not seen and any(p["type"] == "speech_text_buffer.commit" and p["text"] == "已停止。" for p in up):
            seen.append(asyncio.get_running_loop().time())
        return bool(seen) and asyncio.get_running_loop().time() - seen[0] > 0.5  # the card is made, then dropped
    upstream, client_out, events, proposals, timeline, chat_sends = await _run_action(
        monkeypatch, tmp_path, "call-stop.sqlite3", ["空两手沪金。", "算了。"], tool_call=False, second_after=0.1,
        until=stopped)
    assert any(e["event"] == "turnCancelled" for e in timeline)
    assert any(e["event"] == "actionDropped" for e in timeline)
    assert list(proposals.values()) == ["rejected"]
    assert not [n for n, _ in events if n in {"proposal.trade", "proposal.close_batch"}]


@pytest.mark.parametrize("why", ["switch_off", "auto_confirm_setting"])
async def test_switch_off_keeps_orders_with_the_trading_ai(tmp_path, monkeypatch, why):
    """Switch off: exactly the step-1 call. With 语音下单免口头确认 on, orders keep the trading AI too (its card is
    confirmed by the setting as before) — the words-only path never makes a card that confirms itself."""
    monkeypatch.setenv(FRONT_QUERY_ENV, "local-user")
    if why == "switch_off":
        monkeypatch.delenv(FRONT_ACTION_ENV, raising=False)
    else:
        monkeypatch.setenv(FRONT_ACTION_ENV, "local-user")
    app, _, service = await _voice_app(tmp_path, "call-off.sqlite3")
    if why == "auto_confirm_setting":
        prefs = app.state.store.user_preferences("local-user")
        auto = prefs.model_copy(update={"trade": prefs.trade.model_copy(update={"voiceOrderConfirm": False})})
        monkeypatch.setattr(app.state.store, "user_preferences", lambda user_id: auto)
    session = app.state.broker.create_session("local", "native", "local-user")

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif payload["type"] == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "空两手沪金。"})
        elif payload["type"] == "session.close":
            await emit({"type": "session.closed"})
    upstream, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any(e.get("type") == "tool_result" and e.get("name") == "delegate" for e in client_out), timeout=8)
    created = next(p for p in upstream if p["type"] == "session.create")
    if why == "switch_off":
        assert created["session"]["tools"] == FRONT_TOOLS and created["session"]["instructions"] == FRONT_INSTRUCTIONS
    assert next(e for e in client_out[-1]["timeline"] if e["event"] == "routed")["action"] == "delegate"
    await app.state.shared_market.close()
    app.state.store.close()


# -- 同句撤回 (#523 / #754, 2026-10-14) -------------------------------------------------------------------

# 同句撤回的句子是测试集的行（tests/intent_corpus，c215 起）；这里只测通话里的路由与丢卡。
RETRACTED = "开三手，不对，两手沪金多单，算了。"


async def test_a_sentence_taken_back_with_no_card_waiting_makes_no_card_and_reaches_no_trading_ai(tmp_path, monkeypatch):
    """#523: as one final, nothing goes to the trading AI (no chat_send, no delegate), no card is made, and the
    Runtime says it plainly — the pair of saying 算了 on its own with nothing waiting. Doubao's own `action` call for
    the same final is answered with nothing."""
    upstream, client_out, events, proposals, timeline, chat_sends = await _run_action(
        monkeypatch, tmp_path, "call-retract.sqlite3", [RETRACTED],
        until=lambda up, out: any(p["type"] == "speech_text_buffer.commit" and p["text"] == "好，这单不下了。" for p in up))
    assert not chat_sends and proposals == {}
    assert [e["action"] for e in timeline if e["event"] == "routed"] == ["retract"]
    assert not any(e.get("type") == "tool_result" and e.get("name") in {"delegate", "action"} for e in client_out)
    assert not [n for n, _ in events if n.startswith("proposal.")]


async def test_a_watched_orders_cancel_taken_back_in_the_same_sentence_cancels_nothing(tmp_path, monkeypatch):
    """#900 review: with an unfilled order watched, "撤掉这个单，算了" was the watchdog's 撤 — it must not cancel."""
    import trader_runtime.voice_realtime as module
    watched = {"clientOrderId": "c-12345678", "symbol": "SHFE.au2612"}
    monkeypatch.setattr(module.VoiceRealtimeService, "order_watch_target", lambda self, user_id: watched)

    async def never(self, state):
        raise AssertionError("the watched order was cancelled")
    monkeypatch.setattr(module.VoiceRealtimeService, "_cancel_watched_order", never)
    upstream, client_out, events, proposals, timeline, chat_sends = await _run_action(
        monkeypatch, tmp_path, "call-retract-watch.sqlite3", ["撤掉这个单，算了。"], tool_call=False,
        until=lambda up, out: any(p["type"] == "speech_text_buffer.commit" and p["text"] == "好，这单不下了。" for p in up))
    assert [e["action"] for e in timeline if e["event"] == "routed"] == ["retract"] and not chat_sends


async def test_a_sentence_taken_back_with_a_card_waiting_drops_that_card_like_buzuole(tmp_path, monkeypatch):
    """The pair of test_saying_buzuole_after_the_card_drops_it: the waiting card is rejected and no second card."""
    upstream, client_out, events, proposals, timeline, chat_sends = await _run_action(
        monkeypatch, tmp_path, "call-retract-pending.sqlite3", ["空两手沪金。", RETRACTED],
        until=lambda up, out: any(e.get("type") == "tool_result" and e.get("name") == "cancel" for e in out))
    assert list(proposals.values()) == ["rejected"] and not chat_sends
    assert [e["action"] for e in timeline if e["event"] == "routed"] == ["action", "cancel"]


@pytest.mark.parametrize("gap", [0.1, 0.5])
async def test_a_sentence_taken_back_while_a_card_is_being_made_stops_and_rejects_it(tmp_path, monkeypatch, gap):
    """The pair of test_suanle_while_the_card_is_being_made_stops_and_rejects_it: the earlier order's card is still
    being made. Within the continuation window the two finals are one sentence (merged) that ends taken back; after
    it, the running round is stopped like a bare 算了. Either way no card is published and the made one is rejected."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "CONTINUATION_SECONDS", 0.3)  # the window, shortened: no real 3 s wait
    original = module.VoiceRealtimeService.front_action

    async def slow(self, *args, **kwargs):
        await asyncio.sleep(gap + 0.3)
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(module.VoiceRealtimeService, "front_action", slow)
    seen: list[float] = []
    line = "好，这单不下了。" if gap < module.CONTINUATION_SECONDS else "已停止。"

    def stopped(up, out):
        if not seen and any(p["type"] == "speech_text_buffer.commit" and p["text"] == line for p in up):
            seen.append(asyncio.get_running_loop().time())
        return bool(seen) and asyncio.get_running_loop().time() - seen[0] > 0.8  # the card is made, then dropped
    upstream, client_out, events, proposals, timeline, chat_sends = await _run_action(
        monkeypatch, tmp_path, "call-retract-making.sqlite3", ["空两手沪金。", RETRACTED], tool_call=False,
        second_after=gap, until=stopped)
    routed = [e["action"] for e in timeline if e["event"] == "routed"]
    assert routed == ["action", "retract"]
    if gap < module.CONTINUATION_SECONDS:
        assert any(e["event"] == "utteranceMerged" for e in timeline)
    else:
        assert any(e["event"] == "turnCancelled" for e in timeline)
    assert set(proposals.values()) <= {"rejected"} and not chat_sends
    assert not [n for n, _ in events if n in {"proposal.trade", "proposal.close_batch"}]


async def test_with_the_front_desk_off_a_sentence_taken_back_still_reaches_no_trading_ai(tmp_path, monkeypatch):
    """#754: the take-back is not the action switch's — with both front switches off the sentence used to be
    delegated whole and the trading AI made the card."""
    monkeypatch.delenv(FRONT_QUERY_ENV, raising=False)
    monkeypatch.delenv(FRONT_ACTION_ENV, raising=False)
    app, _, service = await _voice_app(tmp_path, "call-retract-off.sqlite3")
    session = app.state.broker.create_session("local", "native", "local-user")

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif payload["type"] == "input_audio_buffer.append" and not getattr(on_send, "said", False):
            on_send.said = True
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": RETRACTED})
        elif payload["type"] == "session.close":
            await emit({"type": "session.closed"})
    upstream, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any(e.get("type") == "tool_result" and e.get("name") == "cancel" for e in client_out), timeout=8)
    assert not any(e.get("type") == "tool_result" and e.get("name") == "delegate" for e in client_out)
    assert next(e for e in client_out[-1]["timeline"] if e["event"] == "routed")["action"] == "retract"
    assert not app.state.store.connection.execute("SELECT id FROM proposals").fetchall()
    await app.state.shared_market.close()
    app.state.store.close()


# -- #754 语音小项 ---------------------------------------------------------------------------------------


async def test_after_the_close_a_refused_card_says_when_the_market_opens(tmp_path, monkeypatch):
    """#754 (23:00 螺纹): the card tool refused with proposal_gate_not_ready and market_not_fresh among its decision
    context reasons; the line read "当前行情不是最新状态" instead of when the market opens."""
    app, service, state, session = await _action_app(tmp_path, "action-closed.sqlite3")

    from trader_runtime.order_planner import Refuse
    from trader_runtime.trader_tools import TraderReadOnlyTools
    real = TraderReadOnlyTools(app.state.data_provider, app.state.adapter.runtime_status, proposals=app.state.proposals)

    class Refusing:
        """The planner's opening gate refusing (the card tool's own answer shape comes from outcome_response)."""
        outcome_response = real.outcome_response

        async def plan_order(self, intent, **kwargs):
            return Refuse("proposal_gate_not_ready", form="gate", reasons=("market_not_fresh", "analysis_not_fresh"),
                          user_facing={"candidateIssues": ["当前行情不是最新状态"]})

    async def tools(session_id, identity):
        return Refusing()

    async def closed_market(symbol):
        return {"marketSessionStatus": "closed"}
    monkeypatch.setattr(service, "_card_tools", tools)
    monkeypatch.setattr(service, "_market_for", closed_market)
    outcome = await service.front_action(OrderIntent(act="open", product="沪金", dir="long", vol=1), state, "买一手沪金")
    assert outcome["card"] is None and "不在交易时段" in outcome["line"] and "行情不是最新" not in outcome["line"]

    async def open_market(symbol):
        return {"marketSessionStatus": "trading"}
    monkeypatch.setattr(service, "_market_for", open_market)  # a stale quote while trading keeps the tool's words
    outcome = await service.front_action(OrderIntent(act="open", product="沪金", dir="long", vol=1), state, "买一手沪金")
    assert "行情不是最新" in outcome["line"]
    await app.state.shared_market.close()
    app.state.store.close()


async def test_an_empty_catalog_is_read_again_before_routing_at_most_every_half_minute(tmp_path, monkeypatch):
    """#754 (10-13 after a deploy): the catalog was empty when the call started (products=0), so the whole call
    named no product. Routing reads it again — once per CATALOG_RETRY_SECONDS while it stays empty."""
    app, _, service = await _voice_app(tmp_path, "names-reread.sqlite3")
    reads: list[int] = []
    catalog: list[dict] = []

    async def read(search=None):
        reads.append(1)
        return list(catalog)
    monkeypatch.setattr(service, "_catalog", read)
    service.product_names = []
    await service.ensure_product_names()
    await service.ensure_product_names()
    assert len(reads) == 1 and service.product_names == []  # still empty: not read again within the window
    catalog.append({"instrumentName": "螺纹2701", "symbol": "SHFE.rb2701"})
    service._names_read_at -= 31
    await service.ensure_product_names()
    assert len(reads) == 2 and service.product_names == ["螺纹"]
    await service.ensure_product_names()
    assert len(reads) == 2  # names known: no more reads
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_take_back_with_no_card_is_recorded_in_the_conversation(tmp_path, monkeypatch):
    """#754 (#900 review): "好，这单不下了。" answered the take-back but the pair never reached the conversation."""
    app, _, service = await _voice_app(tmp_path, "retract-record.sqlite3")
    service.product_names = ["沪金"]
    session = app.state.broker.create_session("local", "native", "local-user")
    upstream, client_out, inbound, run = _action_call(monkeypatch, [RETRACTED])
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})

    def recorded():
        return [e["payload"] for _, e in app.state.store.list_events(session.session_id) if e["name"] == "message.completed"]
    await run(service, until=lambda: len(recorded()) >= 2)
    messages = recorded()
    assert [(m["role"], m["text"], m.get("origin")) for m in messages] == [
        ("user", RETRACTED, "voice"), ("assistant", "好，这单不下了。", "voice")]
    await app.state.shared_market.close()
    app.state.store.close()


# -- 意图重构第 2 步：解析器读出的句子由语音前台出卡 ------------------------------------------------------

def _said(text: str) -> OrderIntent:
    parsed = parse(text, ParseContext(products=NAMES))
    assert isinstance(parsed, OrderIntent), parsed
    return parsed


async def test_a_bare_buy_or_sell_with_the_opposite_side_held_goes_to_the_trading_ai(tmp_path):
    """Corpus c020: “卖一手” with a long held may mean 卖平 — the parser's rule, checked once the account is read. The
    test account holds long and short 沪金: a bare 买 goes to the trading AI (it asks), while 开多 is carded."""
    app, service, state, session = await _action_app(tmp_path, "action-bare.sqlite3")
    assert (await service.front_action(_said("买一手沪金"), state, "买一手沪金"))["delegate"] is True
    opened = await service.front_action(_said("开多一手沪金"), state, "开多一手沪金")
    assert opened["card"][1]["side"] == "buy_open" and opened["card"][1]["volume"] == 1
    await app.state.shared_market.close()
    app.state.store.close()


def _several_cards(service, *extra: OrderIntent, blocked_first: bool = False) -> None:
    """The planner making several cards of one sentence (a 今仓全平 over several contracts, or the parser's legs): the
    sentence's own card plus `extra`, made by the same real card tools."""
    from dataclasses import replace

    from trader_runtime.order_planner import Card, Cards
    real_tools = service._card_tools

    async def card_tools(session_id, identity):
        tools = await real_tools(session_id, identity)
        plan_order = tools.plan_order

        async def several(intent, **kwargs):
            outcomes = [await plan_order(intent, **kwargs)] + [
                await plan_order(replace(more, symbol="SHFE.au2612"), **kwargs) for more in extra]
            cards = [card for outcome in outcomes for card in outcome.cards]
            if blocked_first:
                head = {**cards[0].proposal, "status": "risk_blocked", "approvalId": None,
                        "riskCheck": {"passed": False, "reason": "可用资金不足"}}
                cards[0] = Card(cards[0].event, head)
            return Cards(tuple(cards))
        tools.plan_order = several
        return tools
    service._card_tools = card_tools


async def _two_cards(tmp_path, name):
    """Two cards from one sentence: both published at once (each clickable), the first one read and waiting."""
    app, service, state, session = await _action_app(tmp_path, name)
    _several_cards(service, OrderIntent(act="open", dir="short", vol=1))
    text = "开多一手沪金"
    outcome = await service.front_action(_said(text), state, text)
    cards = [outcome["card"], *outcome["more"]]
    assert [card[1]["side"] for card in cards] == ["buy_open", "sell_open"]
    assert outcome["line"].startswith("一共 2 张卡，一张一张确认。第 1 张：沪金") and outcome["line"].endswith("确认吗？")
    await service.adopt_action(state, text, outcome)
    service.arm_action_card(state, outcome)
    published = [e["payload"]["proposalId"] for _, e in app.state.store.list_events(session.session_id)
                 if e["name"] == "proposal.trade"]
    first, second = (str(card[1]["proposalId"]) for card in cards)
    assert published == [first, second]
    assert [item["proposalId"] for item in state.pending["proposals"]] == [first]  # only the one read out waits
    assert second in state.unseen_cards  # not confirmable by voice before it is asked
    return app, service, state, first, second


async def test_two_independent_cards_are_confirmed_one_at_a_time_by_voice(tmp_path):
    """所有者口径“各自确认”（#950 复审）：each spoken 确认 confirms the card just read, through that card's own confirm
    route (the one a click uses); then the next card is read and asked on its own."""
    app, service, state, first, second = await _two_cards(tmp_path, "action-legs-confirm.sqlite3")
    posted: list[str] = []
    original = service._http

    async def http(method, path, body=None, *, account_id=""):
        if path.endswith("/confirm"):
            posted.append(path)
            return 200, {"status": "confirmed", "simulationOrder": None, "message": "已记录"}
        return await original(method, path, body, account_id=account_id)
    service._http = http
    one = await service.run_tool("confirm", {}, state)
    assert one["ok"] is True and posted == [f"/api/proposals/{first}/confirm"]
    assert "第 2 张：沪金" in one["say"] and one["say"].endswith("确认吗？")
    # #932 复核: the second card's question is said, not yet committed — it is still unheard, nothing waits.
    assert state.pending is None and second in state.unseen_cards
    service.arm_next_card(state, one)  # the line is committed (speak / inject)
    assert [item["proposalId"] for item in state.pending["proposals"]] == [second]
    assert second not in state.unseen_cards
    two = await service.run_tool("confirm", {}, state)
    assert two["ok"] is True and posted == [f"/api/proposals/{first}/confirm", f"/api/proposals/{second}/confirm"]
    assert state.pending is None and "第" not in two["say"]
    await app.state.shared_market.close()
    app.state.store.close()


async def test_cancelling_the_first_card_still_asks_the_second(tmp_path):
    app, service, state, first, second = await _two_cards(tmp_path, "action-legs-cancel.sqlite3")
    result = await service.run_tool("cancel", {}, state)
    assert result["say"].startswith("好，已放弃。第 2 张：沪金") and result["say"].endswith("确认吗？")
    assert app.state.store.get(first)["status"] == "rejected"
    assert app.state.store.get(second)["status"] == "approval_required"
    service.arm_next_card(state, result)
    assert [item["proposalId"] for item in state.pending["proposals"]] == [second]
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_call_that_ends_rejects_the_queued_cards_too(tmp_path):
    """#950 review: the cards queued behind the one being asked were never read; a call ending (or moving on) rejects
    them with it, so a later call's server-side lookup never finds a card nobody heard."""
    app, service, state, first, second = await _two_cards(tmp_path, "action-legs-hangup.sqlite3")
    assert await service._abandon_pending(state) is True  # the teardown's own call
    assert {app.state.store.get(card)["status"] for card in (first, second)} == {"rejected"}
    later = CallState(window_id=WINDOW, chat_session_id=state.chat_session_id)
    assert service._server_pending(later) is None
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_second_confirm_before_the_next_cards_question_is_committed_never_reaches_it(tmp_path):
    """#932 复核 (P1): 确认 A, and the second card's question is said but not yet committed (the filler, the provider)
    when the user says 确认 again. That 确认 answers nothing the user heard: B is not confirmed, its question is asked
    again, and only the line actually committed makes B the waiting card — the stale round's commit arms nothing."""
    app, service, state, first, second = await _two_cards(tmp_path, "action-legs-double-confirm.sqlite3")
    posted: list[str] = []
    original = service._http

    async def http(method, path, body=None, *, account_id=""):
        if path.endswith("/confirm"):
            posted.append(path)
            # the confirmed card no longer waits on the server (a stand-in for the real confirm's transition)
            await original("POST", path.removesuffix("/confirm") + "/reject",
                           {"proposalVersion": 1, "idempotencyKey": path}, account_id=account_id)
            return 200, {"status": "confirmed", "simulationOrder": None, "message": "已记录"}
        return await original(method, path, body, account_id=account_id)
    service._http = http
    one = await service.run_tool("confirm", {}, state)
    assert posted == [f"/api/proposals/{first}/confirm"] and one.get("nextCard")
    assert service._has_pending(state)  # the words are about the cards: routed to 确认 / 取消, never chat
    again = await service.run_tool("confirm", {}, state)
    assert posted == [f"/api/proposals/{first}/confirm"]  # B was never heard: not confirmed
    assert again["say"].startswith("第 2 张：沪金") and again["say"].endswith("确认吗？")
    assert state.pending is None and second in state.unseen_cards
    service.requeue_next_card(state, one)  # the first round, superseded: its token is spent — nothing moves
    service.arm_next_card(state, one)
    assert state.pending is None and state.next_card is not None
    service.arm_next_card(state, again)  # the line that is actually committed
    assert [item["proposalId"] for item in state.pending["proposals"]] == [second]
    await service.run_tool("confirm", {}, state)
    assert posted[-1] == f"/api/proposals/{second}/confirm"
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_second_confirm_while_the_next_cards_line_waits_to_be_committed_never_reaches_it(tmp_path, monkeypatch):
    """#932 复核 (P1, 10-16 16:56): on a real call. 确认 A — Doubao starts its own reply, the Runtime cancels it, and the
    line asking B waits inside speak for that cancel to be acknowledged. The user's second 确认 lands in that window:
    B was already made the waiting card before speak (arm, then await speak) and that 确认 confirmed it, unheard. The
    card now becomes the waiting one only when its question is actually committed."""
    monkeypatch.setenv(FRONT_QUERY_ENV, "local-user")
    monkeypatch.setenv(FRONT_ACTION_ENV, "local-user")
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "FILLER_WAIT_SECONDS", 0.05)  # Doubao's reply outlasts the filler wait: speak waits
    app, _, service = await _voice_app(tmp_path, "action-legs-commit-boundary.sqlite3")
    service.product_names = ["沪金"]
    _several_cards(service, OrderIntent(act="open", dir="short", vol=1))
    session = app.state.broker.create_session("local", "native", "local-user")
    posted: list[str] = []
    original = service._http

    async def http(method, path, body=None, *, account_id=""):
        if path.endswith("/confirm"):
            posted.append(path)
            await original("POST", path.removesuffix("/confirm") + "/reject",
                           {"proposalVersion": 1, "idempotencyKey": path}, account_id=account_id)
            return 200, {"status": "confirmed", "simulationOrder": None, "message": "已记录"}
        return await original(method, path, body, account_id=account_id)
    service._http = http
    script = {"commits": 0, "said": False}

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append" and not script["said"]:
            script["said"] = True
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "开多一手沪金"})
            await emit({"type": "response.done", "response": {"usage": {}}})
        elif kind == "speech_text_buffer.commit":
            script["commits"] += 1
            await emit({"type": "response.output_audio.started", "tts_type": "chat_tts_text", "response_id": f"r{script['commits']}"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done", "response_id": f"r{script['commits']}"})
            await emit({"type": "response.done", "response": {"usage": {}}})
            if script["commits"] == 1:  # A asked: the user says 确认 and Doubao starts a reply of its own
                await asyncio.sleep(0.05)
                await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "确认"})
                await emit({"type": "response.output_text.delta", "delta": "好的"})
                await emit({"type": "response.output_audio.started", "tts_type": "default", "response_id": "own"})
        elif kind == "response.cancel" and not script.get("cancelled"):
            script["cancelled"] = True

            async def second_confirm_then_ack():
                await asyncio.sleep(0.15)  # B's question is waiting for this cancel's acknowledgement
                await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "确认"})
                await asyncio.sleep(0.45)
                await emit({"type": "response.canceled"})
                await emit({"type": "response.done", "response": {"usage": {}}})
            asyncio.ensure_future(second_confirm_then_ack())
        elif kind == "response.cancel":
            await emit({"type": "response.canceled"})
            await emit({"type": "response.done", "response": {"usage": {}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: script["commits"] >= 3, timeout=6)
    cards = [e["payload"]["proposalId"] for _, e in app.state.store.list_events(session.session_id) if e["name"] == "proposal.trade"]
    first, second = cards[0], cards[1]
    commits = [p["text"] for p in upstream if p["type"] == "speech_text_buffer.commit"]
    assert posted == [f"/api/proposals/{first}/confirm"]  # the second 确认 answered nothing the user had heard
    assert f"/api/proposals/{second}/confirm" not in posted
    assert len(commits) >= 3 and all("第 2 张：沪金" in line for line in commits[1:3])  # B asked, then asked again
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_next_cards_question_whose_commit_failed_to_send_never_makes_it_confirmable(tmp_path, monkeypatch):
    """#932 B4: the line asking card B is committed by speak / inject; the card becomes the waiting one only once that
    commit has actually been sent. Here the send fails (the upstream drops): B was armed before the send, and the user's
    确认 after the line was said again on the new session confirmed a card whose question the call never committed
    with it. Now B stays prepared and the 确认 asks it again."""
    monkeypatch.setenv(FRONT_QUERY_ENV, "local-user")
    monkeypatch.setenv(FRONT_ACTION_ENV, "local-user")
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "FILLER_WAIT_SECONDS", 0.05)
    app, _, service = await _voice_app(tmp_path, "action-legs-commit-failed.sqlite3")
    service.product_names = ["沪金"]
    _several_cards(service, OrderIntent(act="open", dir="short", vol=1))
    session = app.state.broker.create_session("local", "native", "local-user")
    posted: list[str] = []
    original = service._http

    async def http(method, path, body=None, *, account_id=""):
        if path.endswith("/confirm"):
            posted.append(path)
            await original("POST", path.removesuffix("/confirm") + "/reject",
                           {"proposalVersion": 1, "idempotencyKey": path}, account_id=account_id)
            return 200, {"status": "confirmed", "simulationOrder": None, "message": "已记录"}
        return await original(method, path, body, account_id=account_id)
    service._http = http
    script = {"commits": 0, "said": False, "sessions": 0}

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            script["sessions"] += 1
            await emit({"type": "session.created", "session": {"id": f"s{script['sessions']}"}})
        elif kind == "input_audio_buffer.append" and not script["said"]:
            script["said"] = True
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "开多一手沪金"})
            await emit({"type": "response.done", "response": {"usage": {}}})
        elif kind == "speech_text_buffer.commit":
            script["commits"] += 1
            if script["commits"] == 2:  # the line asking B: its send fails (the session is replaced)
                raise ConnectionError("upstream dropped")
            await emit({"type": "response.output_audio.started", "tts_type": "chat_tts_text", "response_id": f"r{script['commits']}"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done", "response_id": f"r{script['commits']}"})
            await emit({"type": "response.done", "response": {"usage": {}}})
            if script["commits"] in {1, 3}:  # A asked / B's unheard line said again on the new session: 确认
                await asyncio.sleep(0.05)
                await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "确认"})
        elif kind == "response.cancel":
            await emit({"type": "response.canceled"})
            await emit({"type": "response.done", "response": {"usage": {}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: script["commits"] >= 4, timeout=8)
    cards = [e["payload"]["proposalId"] for _, e in app.state.store.list_events(session.session_id) if e["name"] == "proposal.trade"]
    cards = list(dict.fromkeys(cards))  # a card is published again when it is asked again
    assert len(cards) == 2
    first = cards[0]
    commits = [p["text"] for p in upstream if p["type"] == "speech_text_buffer.commit"]
    assert posted == [f"/api/proposals/{first}/confirm"]  # B's question never left with its commit: not confirmed
    assert len(commits) >= 4 and commits[3].startswith("第 2 张：沪金")  # the 确认 asked B again
    await app.state.shared_market.close()
    app.state.store.close()


async def test_hanging_up_while_a_timed_out_card_is_being_retired_still_retires_it(tmp_path):
    """#932 复核 (P1, 10-16 16:56): the confirm window ran out and the announcer was rejecting A when the call ended —
    the teardown cancels the announcer, and A, already off the call (a local of the cancelled retire), stayed
    approval_required for a later call. An interrupted retire leaves its cards to the call's teardown."""
    app, service, state, first, second = await _two_cards(tmp_path, "action-legs-timeout-cancelled.sqlite3")
    entered = asyncio.Event()
    original = service._reject

    async def reject(item):
        if str(item.get("proposalId")) == first and not entered.is_set():
            entered.set()
            await asyncio.Event().wait()  # cut off by the teardown's cancel
        await original(item)
    service._reject = reject
    task = asyncio.ensure_future(service._abandon_pending(state, keep_queue=True))  # the announcer's timeout path
    await entered.wait()
    state.closed = True  # the call's teardown: its own tasks are cancelled, then the cards retired
    task.cancel()
    await asyncio.wait([task])
    await service._abandon_pending(state)
    assert {app.state.store.get(card)["status"] for card in (first, second)} == {"rejected"}
    later = CallState(window_id=WINDOW, chat_session_id=state.chat_session_id)
    assert service._server_pending(later) is None and service._server_cards(later) == []
    await app.state.shared_market.close()
    app.state.store.close()


@pytest.mark.parametrize("handover", ["cancel", "timeout"])
async def test_hanging_up_during_a_cancel_or_timeout_hand_over_retires_the_queued_card(tmp_path, handover):
    """#932 复核 (P1): 算了 (or the confirm window running out) rejects A, and the user hangs up while that reject is
    on its way. The queued B stays on the call across the await, so the teardown retires it; the hand-over then asks
    nothing, and a new call on the conversation finds no card nobody heard."""
    app, service, state, first, second = await _two_cards(tmp_path, f"action-legs-{handover}-hangup.sqlite3")
    entered, release = asyncio.Event(), asyncio.Event()
    original = service._reject

    async def reject(item):
        if str(item.get("proposalId")) == first:
            entered.set()
            await release.wait()
        await original(item)
    service._reject = reject

    async def hand_over():
        if handover == "cancel":
            return await service.run_tool("cancel", {}, state)
        await service._abandon_pending(state, keep_queue=True)  # the announcer's timeout path
        return service._with_next_card(state, {"say": "没有听到确认，已放弃。"})
    task = asyncio.ensure_future(hand_over())
    await entered.wait()
    state.closed = True  # the call's teardown
    await service._abandon_pending(state)
    assert app.state.store.get(second)["status"] == "rejected"
    release.set()
    result = await task
    assert "第 2 张" not in result["say"] and state.pending is None and state.next_card is None
    assert {app.state.store.get(card)["status"] for card in (first, second)} == {"rejected"}
    later = CallState(window_id=WINDOW, chat_session_id=state.chat_session_id)
    assert service._server_pending(later) is None and service._server_cards(later) == []
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_risk_blocked_first_card_is_read_and_the_next_confirmable_card_is_asked(tmp_path):
    """#950 review: the first card was blocked (no approval), the others wait. The blocked one's reason is read, the
    first confirmable card is the one asked; the cards behind it stay unheard — a 确认 reaches only the one read."""
    app, service, state, session = await _action_app(tmp_path, "action-legs-blocked.sqlite3")
    _several_cards(service, OrderIntent(act="open", dir="short", vol=1), OrderIntent(act="open", dir="long", vol=2),
                   blocked_first=True)
    text = "开多一手沪金"
    outcome = await service.front_action(_said(text), state, text)
    assert outcome["line"].startswith("一共 3 张卡，一张一张确认。第 1 张：这单下不了：可用资金不足。第 2 张：沪金")
    assert outcome["line"].endswith("确认吗？") and "第 3 张" not in outcome["line"]
    second, third = (str(made[1]["proposalId"]) for made in outcome["more"])
    await service.adopt_action(state, text, outcome)
    service.arm_action_card(state, outcome)
    assert [item["proposalId"] for item in state.pending["proposals"]] == [second]
    assert third in state.unseen_cards
    posted: list[str] = []
    original = service._http

    async def http(method, path, body=None, *, account_id=""):
        if path.endswith("/confirm"):
            posted.append(path)
            return 200, {"status": "confirmed", "simulationOrder": None, "message": "已记录"}
        return await original(method, path, body, account_id=account_id)
    service._http = http
    result = await service.run_tool("confirm", {}, state)
    assert posted == [f"/api/proposals/{second}/confirm"] and "第 3 张：沪金" in result["say"]
    service.arm_next_card(state, result)
    assert [item["proposalId"] for item in state.pending["proposals"]] == [third]
    await app.state.shared_market.close()
    app.state.store.close()


async def test_the_trading_ais_cards_of_one_sentence_are_asked_one_by_one_like_the_front_desks(tmp_path):
    """意图重构第 3 步 (#950 第 2 步回读): a delegated sentence the trading AI carded as several cards (propose_order legs)
    is asked the front desk's way — only the first card waits for 确认, the rest stay unheard (a generic 确认 never
    reaches them, nor a later call's server-side lookup), nothing is confirmed on the user's behalf even with
    语音下单免口头确认 on, and each 确认 reads the next card."""
    from trader_runtime.user_preferences import UserPreferences
    app, service, state, session = await _action_app(tmp_path, "delegate-legs.sqlite3")
    state.preferences = UserPreferences.model_validate({"trade": {"voiceOrderConfirm": False}})
    proposals = app.state.proposals
    first = proposals.create_trade(session.session_id, "turn-legs", symbol="SHFE.au2612", side="buy_open", volume=1,
                                   price_intent="passive", reason="一句两单")
    second = proposals.create_trade(session.session_id, "turn-legs", symbol="SHFE.au2612", side="sell_open", volume=2,
                                    price_intent="passive", reason="一句两单")
    pending = [{"proposalId": card["proposalId"], "approvalId": card["approvalId"], "version": card["version"],
                "type": "trade", "summary": summary} for card, summary in ((first, "沪金 买入开仓 1 手"), (second, "沪金 卖出开仓 2 手"))]
    original_call = service._call

    async def window(state=None):
        return {"windowId": WINDOW, "revision": 1, "online": True, "state": {"chatSessionId": session.session_id}}

    async def call(operation, *, path=None, query=None, body=None, **kwargs):
        if operation == CHAT_SEND:
            return {"data": {"sessionId": session.session_id, "runStatus": "completed", "timedOut": False,
                             "reply": "出了两张卡。", "spoken": "出了两张卡。", "pendingProposals": pending + pending[1:]}}
        return await original_call(operation, path=path, query=query, body=body, **kwargs)
    service._window = window
    service._call = call
    service._simulation_only = lambda: True
    posted: list[str] = []
    original_http = service._http

    async def http(method, path, body=None, *, account_id=""):
        if path.endswith("/confirm"):
            posted.append(path)
            return 200, {"status": "confirmed", "simulationOrder": None, "message": "已记录"}
        return await original_http(method, path, body, account_id=account_id)
    service._http = http
    said = await service.run_tool("delegate", {"utterance": "买一手沪金然后空两手沪金"}, state)
    assert said["pendingConfirmation"] is True and posted == []  # never confirmed on the user's behalf
    assert said["spoken"] == "出了两张卡。一共 2 张卡，一张一张确认。第 1 张：沪金 买入开仓 1 手，确认吗？"
    assert [item["proposalId"] for item in state.pending["proposals"]] == [first["proposalId"]]
    assert second["proposalId"] in state.unseen_cards
    # Another call on the same conversation: two cards wait and nobody pointed at one — a bare 确认 takes none.
    assert service._server_pending(CallState(window_id=WINDOW, chat_session_id=session.session_id)) is None
    one = await service.run_tool("confirm", {}, state)
    assert posted == [f"/api/proposals/{first['proposalId']}/confirm"]
    assert one["say"].endswith("第 2 张：沪金 卖出开仓 2 手")
    service.arm_next_card(state, one)
    assert [item["proposalId"] for item in state.pending["proposals"]] == [second["proposalId"]]
    await service.run_tool("confirm", {}, state)
    assert posted[-1] == f"/api/proposals/{second['proposalId']}/confirm" and state.pending is None
    await app.state.shared_market.close()
    app.state.store.close()


@pytest.mark.parametrize("source,confirmed", [("model", False), ("parser", True), ("parser_legs", False), (None, False)])
async def test_only_a_card_the_parser_read_is_confirmed_without_the_spoken_word(tmp_path, source, confirmed):
    """意图重构第 3 步: 语音下单免口头确认 confirms only a card the fast parser read from the words. A card from the
    trading AI's propose_order (a model's reading), one of a sentence of several orders (`parser_legs`: each confirmed
    by the user, owner 2026-10-16) — or one whose source is unknown — is read out and waits."""
    from trader_runtime.user_preferences import UserPreferences
    app, service, state, session = await _action_app(tmp_path, f"delegate-source-{source}.sqlite3")
    state.preferences = UserPreferences.model_validate({"trade": {"voiceOrderConfirm": False}})
    card = app.state.proposals.create_trade(session.session_id, "turn-one", symbol="SHFE.au2612", side="buy_open",
                                            volume=1, price_intent="passive", reason="一张", intent_source=source)
    assert card.get("intentSource") == source
    pending = [{"proposalId": card["proposalId"], "approvalId": card["approvalId"], "version": card["version"],
                "type": "trade", "summary": "沪金 买入开仓 1 手", "intentSource": source}]
    original_call = service._call

    async def window(state=None):
        return {"windowId": WINDOW, "revision": 1, "online": True, "state": {"chatSessionId": session.session_id}}

    async def call(operation, *, path=None, query=None, body=None, **kwargs):
        if operation == CHAT_SEND:
            return {"data": {"sessionId": session.session_id, "runStatus": "completed", "timedOut": False,
                             "reply": "出了一张卡。", "spoken": "出了一张卡。", "pendingProposals": pending}}
        return await original_call(operation, path=path, query=query, body=body, **kwargs)
    service._window = window
    service._call = call
    service._simulation_only = lambda: True
    posted: list[str] = []
    original_http = service._http

    async def http(method, path, body=None, *, account_id=""):
        if path.endswith("/confirm"):
            posted.append(path)
            return 200, {"status": "confirmed", "simulationOrder": None, "message": "已记录"}
        return await original_http(method, path, body, account_id=account_id)
    service._http = http
    said = await service.run_tool("delegate", {"utterance": "沪金买一手"}, state)
    assert bool(posted) is confirmed
    if not confirmed:
        assert said["pendingConfirmation"] is True and said["spoken"].endswith("确认吗？")
    await app.state.shared_market.close()
    app.state.store.close()


async def test_several_waiting_cards_take_no_bare_confirm_but_cancel_all_reaches_every_one(tmp_path):
    """#952 Codex P2: two cards wait on the conversation and the call read neither. A card is waiting (确认 / 取消 are
    about cards, never chat), but no single card is the target: a bare 确认 confirms none and says so; “都不要”
    rejects them all."""
    app, service, state, session = await _action_app(tmp_path, "several-waiting.sqlite3")
    cards = [app.state.proposals.create_trade(session.session_id, f"turn-{side}", symbol="SHFE.au2612", side=side,
                                              volume=1, price_intent="passive", reason="两张")
             for side in ("buy_open", "sell_open")]
    assert service._has_pending(state) and service._server_pending(state) is None
    posted: list[str] = []
    original = service._http

    async def http(method, path, body=None, *, account_id=""):
        if path.endswith("/confirm"):
            posted.append(path)
        return await original(method, path, body, account_id=account_id)
    service._http = http
    bare = await service.run_tool("confirm", {}, state)
    assert posted == [] and bare["say"] == "有 2 张卡在等确认，请在卡上点，或说“都不要”全部作废。"
    every = await service.run_tool("cancel", {"all": True}, state)
    assert every["say"].startswith("好，都放弃了")
    assert {app.state.store.get(card["proposalId"])["status"] for card in cards} == {"rejected"}
    assert not service._has_pending(state)
    await app.state.shared_market.close()
    app.state.store.close()


async def test_saying_none_of_them_drops_every_waiting_card(tmp_path):
    from trader_runtime.voice_realtime import CANCEL_ALL_WORDS
    assert all(CANCEL_ALL_WORDS.fullmatch(words) for words in ("都不要", "全部取消", "都不要了", "全都算了"))
    assert not CANCEL_ALL_WORDS.fullmatch("不要")  # a plain 不要 is this card only
    app, service, state, first, second = await _two_cards(tmp_path, "action-legs-none.sqlite3")
    result = await service.run_tool("cancel", {"all": True}, state)
    assert result["say"] == "好，都放弃了。" and state.pending is None
    assert {app.state.store.get(card)["status"] for card in (first, second)} == {"rejected"}
    await app.state.shared_market.close()
    app.state.store.close()


@pytest.mark.parametrize("text", [
    "螺纹2701、沪金2612各买一手", "买两手螺纹然后再买一手", "撤单之后再买一手",
    "平买的一手螺纹，就平卖的一手螺纹", "平一手螺纹今仓，就平一手螺纹昨仓",
])
def test_several_orders_the_rules_cannot_split_stay_with_the_trading_ai(text):
    """#950 review: a shared “各买”, a clause missing its own contract (never borrowed from the one before), a cancel
    among the orders and a “就…” that changes a qualifier are Uncertain — the trading AI asks for one order at a time."""
    from trader_runtime.order_intent_parse import Uncertain
    parsed = parse(text, ParseContext(products=NAMES))
    assert isinstance(parsed, Uncertain) and front_order(parsed) is None


@pytest.mark.parametrize("text", ["买两手螺纹然后买一手沪金", "平掉沪金多单，再开多两手沪金", "全部平仓，然后买一手螺纹"])
def test_several_orders_said_clause_by_clause_reach_the_front_desk_as_legs(text):
    """所有者 2026-10-16: 一句两单 = `legs`, each read from its own clause; the planner cards them in order (or relays one
    contract, or refuses an unscoped 全平 among them with legs_entangled)."""
    parsed = parse(text, ParseContext(products=NAMES))
    assert isinstance(parsed, OrderIntent) and len(parsed.legs) == 2 and front_order(parsed) is parsed


async def test_a_sentence_of_two_orders_cards_each_from_its_own_clause_and_asks_them_one_by_one(tmp_path):
    """所有者 2026-10-16（一句两单按顺序出两张卡、各自确认）: the parser splits the sentence and each card takes the
    side and lots of its own clause; both are made at once, the first is read and waits, the second is queued unheard —
    and with 语音下单免口头确认 on neither is confirmed for the user (`parser_legs`)."""
    from trader_runtime.user_preferences import UserPreferences
    app, service, state, session = await _action_app(tmp_path, "action-legs-parsed.sqlite3")
    state.preferences = UserPreferences.model_validate({"trade": {"voiceOrderConfirm": False}})
    service._simulation_only = lambda: True
    posted: list[str] = []
    original = service._http

    async def http(method, path, body=None, *, account_id=""):
        if path.endswith("/confirm"):
            posted.append(path)
        return await original(method, path, body, account_id=account_id)
    service._http = http
    text = "开多一手沪金，然后开空两手沪金"
    said = _said(text)
    assert front_order(said) is said and len(said.legs) == 2
    outcome = await service.front_action(said, state, text)
    cards = [outcome["card"], *outcome["more"]]
    assert [(card[1]["side"], card[1]["volume"], card[1]["intentSource"]) for card in cards] == [
        ("buy_open", 1, "parser_legs"), ("sell_open", 2, "parser_legs")]
    assert outcome["line"].startswith("一共 2 张卡，一张一张确认。第 1 张：沪金") and "买开 1 手" in outcome["line"]
    assert "卖开 2 手" in outcome["lines"][1] and "卖开 2 手" not in outcome["line"]  # asked after the first
    await service.adopt_action(state, text, outcome)
    assert posted == []
    first, second = (str(card[1]["proposalId"]) for card in cards)
    assert [item["proposalId"] for item in outcome["pending"]["proposals"]] == [first]
    assert [entry["item"]["proposalId"] for entry in outcome["pending"]["queue"]] == [second]
    assert second in state.unseen_cards
    await app.state.shared_market.close()
    app.state.store.close()

"""Real-time voice V1: the front desk delegates every business request to the agent; confirmations,
window binding and auth protect the user."""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.agent_api import CHAT_SEND
from trader_runtime.main import create_app
from trader_runtime.voice_realtime import (
    CHINA,
    INSTRUCTIONS,
    TOOLS,
    CallState,
    VoiceRealtimeService,
    clean_transcript,
    first_sentence,
    route_utterance,
)

WINDOW = "3f1e4d5a-1111-4222-8333-444444444444"


@pytest.fixture(autouse=True)
def _fast_speech_bounds(monkeypatch):
    """The scripted upstream answers immediately, so a bound that waits an event out (the filler,
    one sentence's audio, the session close ack) should never take real seconds. Only bounds the
    script never lets elapse are shortened; windows that choose a path (INTERIM_AFTER,
    LATE_FINAL_WINDOW, CONTINUATION, QUOTE_REMARK…) stay at their real values — a test that
    needs them small patches them itself and wins over this fixture."""
    import trader_runtime.voice_realtime as module
    for name in ("FILLER_QUIET_SECONDS", "FILLER_WAIT_SECONDS", "BRIDGE_AUDIO_WAIT_SECONDS",
                 "SENTENCE_AUDIO_WAIT_SECONDS", "ROUTED_RESPONSE_WAIT_SECONDS",
                 "SESSION_CLOSE_WAIT_SECONDS", "ANNOUNCE_POLL_SECONDS"):
        monkeypatch.setattr(module, name, 0.05)
    monkeypatch.setattr(module, "ASR_WATCHDOG_TICK", 0.01)


async def _register_window(app, window_id=WINDOW, *, chat_session_id=None, revision=0):
    """A real UI window heartbeat (what the desktop sends), so the agent turn's uiWindowId verifies."""
    from trader_runtime.ui_control import ChartOptions
    state = dict(symbol="SHFE.au2612", duration=60, view="trade", panel=None, settingsOpen=False, theme="dark", dockSide="right",
                 dockWidth=400, replay=False, settingsPage="connection", fontScale="large", density="standard",
                 chart={k: True for k in ChartOptions.model_fields}, chartSizes=dict(main=58, kd=19, macd=23), strategy=None,
                 **({"chatSessionId": chat_session_id} if chat_session_id else {}))
    body = {"windowId": window_id, "revision": revision, "state": state,
            "device": {"deviceId": "device-a", "platform": "macos", "version": "test", "nativeCapture": True},
            "observation": {"capturedAt": "2026-09-08T00:00:00Z", "viewport": {"width": 1280, "height": 720},
                            "text": "交易台", "truncated": False, "regions": [], "controls": [], "context": {}}}
    headers = {"Authorization": "Bearer test-local-token", "Origin": "http://127.0.0.1:5173"}
    async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
        assert (await client.post("/api/ui/pulse", json=body)).status_code == 200


async def _voice_app(tmp_path, name="voice.sqlite3"):
    from test_simulation_orders import Gateway, NormalProvider, application

    class VoiceProvider(NormalProvider):
        def market_workbench(self, symbol, duration_seconds):
            # Voice reads the same shared workbench quote as the page.
            quote = self.market_snapshot("", symbol)
            return {**super().market_workbench(symbol, duration_seconds),
                    "ageSeconds": self.age_seconds,
                    "quote": {**quote, "lastPrice": quote["price"]}}

    gateway = Gateway()
    app = application(tmp_path / name, gateway, VoiceProvider())
    await _register_window(app)
    return app, gateway, VoiceRealtimeService(app, key_file=tmp_path / "missing-key")


def test_the_front_desk_only_has_confirm_cancel_read_screen_and_the_quick_lookups():
    # V1.1: delegate is the Runtime's own routed action, not a model tool (Doubao kept re-calling it).
    # UI work is owned by the shared agent; only the read-only lookups stay at the front desk.
    assert [tool["name"] for tool in TOOLS] == ["confirm", "cancel", "read_screen", "get_quote", "get_positions", "get_account"]
    # Doubao's context window is 12K: the brief and every description stay short (V1.4 budget).
    assert len(INSTRUCTIONS) <= 450
    for tool in TOOLS:
        assert len(tool["description"]) <= 60, tool["name"]
        for name, schema in (tool["parameters"].get("properties") or {}).items():
            assert len(schema.get("description") or "") <= 60, (tool["name"], name)
    assert first_sentence("**结论**：沪金短线上行。\n\n为什么：……") == "结论：沪金短线上行。"
    assert first_sentence("") == ""
    # A contract code in parentheses is letter soup aloud: the whole parenthesis goes, other
    # parentheses keep their content（“（今仓可平 2 手）”照念）.
    from trader_runtime.voice_realtime import _speakable
    assert _speakable("螺纹钢（SHFE.rb2610）短线偏弱") == "螺纹钢短线偏弱"
    assert _speakable("沪金(DCE.au2612)：今仓可平 2 手（已核对）") == "沪金：今仓可平 2 手已核对"


async def test_delegate_is_one_turn_of_the_users_chat_session_and_reads_the_spoken_line(tmp_path):
    app, gateway, service = await _voice_app(tmp_path)
    state = CallState(window_id=WINDOW)
    # No hello → no window → refused before anything reaches the agent.
    refused = await service.run_tool("delegate", {"utterance": "看看行情"}, CallState())
    assert refused == {"ok": False, "error": "no_window", "say": "软件窗口现在不在线。"}
    result = await service.run_tool("delegate", {"utterance": "看看行情"}, state)
    assert result["ok"] is True and result["pendingConfirmation"] is False
    # The fake agent writes no `spoken`: the answer itself is read (markdown stripped, bounded), not just its first sentence.
    assert result["content"].startswith("沪金当前保持短线上行") and result["spoken"].startswith("沪金当前保持短线上行，我只展开了本次判断需要的价格、涨跌和方向")
    session_id = result["sessionId"]
    assert state.chat_session_id == session_id
    # The turn is in the conversation like a typed one, tagged with its origin.
    events = [event for _, event in app.state.store.list_events(session_id)]
    user_turns = [e["payload"] for e in events if e["name"] == "message.completed" and e["payload"].get("role") == "user"]
    assert user_turns[-1]["text"] == "看看行情" and user_turns[-1]["origin"] == "voice"
    # The next request joins the same session; the session survives for the whole call.
    again = await service.run_tool("delegate", {"utterance": "账户怎么样"}, state)
    assert again["sessionId"] == session_id and "今天盈利" in again["spoken"]
    # Nothing waiting: confirm and cancel say so instead of acting.
    assert (await service.run_tool("confirm", {}, state))["error"] == "nothing_pending"
    assert (await service.run_tool("cancel", {}, state)) == {"ok": True, "dropped": False, "say": "现在没有等确认的操作。"}
    assert gateway.submit_count == 0
    app.state.store.close()


async def test_delegate_failures_and_timeouts_are_spoken_never_fatal(tmp_path, monkeypatch):
    from fastapi import HTTPException
    app, gateway, service = await _voice_app(tmp_path)
    state = CallState(window_id=WINDOW)

    async def registered(state=None):
        return {"windowId": WINDOW, "revision": 1, "online": True, "state": {}}
    monkeypatch.setattr(service, "_window", registered)

    async def failing(operation, *, path=None, query=None, body=None):
        if operation == "chat:send":
            raise HTTPException(500, "agent_down")
        return {"data": {}}
    monkeypatch.setattr(service, "_call", failing)
    failed = await service.run_tool("delegate", {"utterance": "看看行情"}, state)
    assert failed["ok"] is False and failed["error"] == "agent_down" and failed["say"] == "后台没有回应，请稍后再说一遍。"

    async def slow(operation, *, path=None, query=None, body=None):
        return {"data": {"sessionId": "s1", "runStatus": "running", "timedOut": True, "reply": "", "pendingProposals": []}}
    monkeypatch.setattr(service, "_call", slow)
    timed_out = await service.run_tool("delegate", {"utterance": "帮我查新闻"}, state)
    assert timed_out["ok"] is False and timed_out["timedOut"] is True and "还在处理" in timed_out["spoken"]

    async def broken(operation, *, path=None, query=None, body=None):
        return {"data": {"sessionId": "s1", "runStatus": "failed", "timedOut": False, "reply": "模型请求失败。请稍后重试。", "pendingProposals": []}}
    monkeypatch.setattr(service, "_call", broken)
    broken_turn = await service.run_tool("delegate", {"utterance": "帮我查新闻"}, state)
    assert broken_turn["ok"] is False and broken_turn["spoken"] == "模型请求失败。"
    # The progress hook fires while the agent is still working (every PROGRESS_SECONDS).
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "PROGRESS_SECONDS", 0.05)
    ticks = []

    async def working(operation, *, path=None, query=None, body=None):
        import asyncio
        await asyncio.sleep(0.2)
        return {"data": {"sessionId": "s1", "runStatus": "completed", "timedOut": False, "reply": "好了。", "spoken": "查到了。", "pendingProposals": []}}
    monkeypatch.setattr(service, "_call", working)

    async def progress():
        ticks.append(1)
    done = await service.run_tool("delegate", {"utterance": "帮我查新闻"}, state, progress=progress)
    assert done["spoken"] == "查到了。" and len(ticks) >= 2
    app.state.store.close()


async def test_a_spoken_order_is_the_agents_proposal_card_confirmed_by_voice(tmp_path):
    app, gateway, service = await _voice_app(tmp_path)
    state = CallState(window_id=WINDOW)
    # The fake agent answers "买入" with a trade proposal card (approval_required).
    prepared = await service.run_tool("delegate", {"utterance": "买入一手黄金"}, state)
    assert prepared["ok"] is True and prepared["pendingConfirmation"] is True
    assert prepared["spoken"].endswith("确认吗？") and "买入开仓" in prepared["spoken"]
    proposal_id = state.pending["proposals"][0]["proposalId"]
    assert app.state.store.get(proposal_id)["status"] == "approval_required" and gateway.submit_count == 0
    # The 15 s window opens when the read-back has played and is shown on the card.
    import time
    assert service._pending_expired(state) is False
    await service._open_confirm_window(state, time.monotonic() + state.preferences.voice.confirmWindowSeconds)
    confirm_by = app.state.store.get(proposal_id)["payload"]["voiceConfirmBy"]
    assert confirm_by
    assert state.preferences.voice.confirmWindowSeconds == 15
    # The card is re-sent to the conversation with the window, so the open client shows the countdown.
    session_id = app.state.store.get(proposal_id)["sessionId"]
    resent = [event for _, event in app.state.store.list_events(session_id) if event["name"] == "proposal.trade" and event["payload"]["proposalId"] == proposal_id]
    assert resent[-1]["payload"]["voiceConfirmBy"] == confirm_by and resent[-1]["payload"]["status"] == "approval_required"
    # Pure (fixed-table) navigation leaves the existing card and its confirmation window untouched.
    waiting = state.pending
    before = app.state.store.get(proposal_id)
    await service.run_tool("delegate", {"utterance": "打开持仓清单"}, state)
    after = app.state.store.get(proposal_id)
    assert after["status"] == "approval_required" and after["approvalUsedAt"] is None
    assert after["version"] == before["version"] and after["payload"]["voiceConfirmBy"] == confirm_by
    assert state.pending is waiting and gateway.submit_count == 0
    # "确认" = the card's confirm button: the same route, the same gateway.
    confirmed = await service.run_tool("confirm", {}, state)
    # The contract is said by its catalog name, never read out as a code (10-16: "SHFE.ag2612 已挂单").
    assert "沪金2612 已挂单" in confirmed["say"] and "SHFE." not in confirmed["say"]
    assert confirmed["ok"] is True and gateway.submit_count == 1 and "已挂单" in confirmed["say"] and "15 秒没成交我会提醒你" in confirmed["say"]
    assert app.state.store.get(proposal_id)["status"] == "candidate_recorded"
    assert "voiceConfirmBy" not in app.state.store.get(proposal_id)["payload"] and state.pending is None
    # "算了" rejects the card; a timed-out window rejects it too.
    second = await service.run_tool("delegate", {"utterance": "再买入一手"}, state)
    second_id = state.pending["proposals"][0]["proposalId"]
    assert second["pendingConfirmation"] is True and app.state.store.pending_proposals(second["sessionId"]) != []
    assert (await service.run_tool("cancel", {}, state))["dropped"] is True
    assert app.state.store.get(second_id)["status"] == "rejected" and app.state.store.pending_proposals(second["sessionId"]) == []
    third = await service.run_tool("delegate", {"utterance": "买入一手"}, state)
    third_id = state.pending["proposals"][0]["proposalId"]
    state.pending["deadline"] = time.monotonic() - 1
    expired = await service.run_tool("confirm", {}, state)
    assert expired["error"] == "confirmation_expired" and app.state.store.get(third_id)["status"] == "rejected"
    assert gateway.submit_count == 1 and third["ok"] is True
    # A new request supersedes a card still waiting for "确认".
    await service.run_tool("delegate", {"utterance": "买入一手"}, state)
    fourth_id = state.pending["proposals"][0]["proposalId"]
    await service.run_tool("delegate", {"utterance": "看看行情"}, state)
    assert app.state.store.get(fourth_id)["status"] == "rejected" and state.pending is None
    app.state.store.close()


async def test_tools_act_on_the_callers_window_only_and_never_guess_another_device(tmp_path):
    app = create_app(tmp_path / "voice-window.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="voice-test-token")
    service = VoiceRealtimeService(app, key_file=tmp_path / "missing-key")

    async def two_devices(operation, *, path=None, query=None, body=None):
        return {"data": {"windows": [{"windowId": "other-mac", "online": True, "revision": 9, "state": {}},
                                     {"windowId": "this-mac", "online": True, "revision": 4, "state": {"symbol": "SHFE.rb2701"}}]}}
    service._call = two_devices  # type: ignore[method-assign]
    assert (await service._window(CallState(window_id="this-mac")))["windowId"] == "this-mac"
    assert (await service._window(CallState(window_id="gone"))) is None
    # No hello at all (pre-1.5.7 client, bare harness): nothing is guessed, other devices are never touched.
    assert (await service._window(None)) is None
    assert (await service._window(CallState())) is None
    refused = await service.run_tool("read_screen", {}, CallState())
    assert refused["ok"] is False and refused["say"] == "软件窗口现在不在线。"
    assert (await service.run_tool("submit_order", {}, CallState(window_id="this-mac")))["refused"] is True
    app.state.store.close()


async def test_the_voice_is_a_per_user_choice_from_the_documented_list(tmp_path):
    from trader_runtime.voice_realtime import VOICE, VOICES
    app = create_app(tmp_path / "voice-pref.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="voice-test-token")
    headers = {"Authorization": "Bearer voice-test-token", "Origin": "http://127.0.0.1:5173"}
    async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
        listed = (await client.get("/api/voice/realtime/voices")).json()
        assert listed["current"] == VOICE and [item["id"] for item in listed["voices"]] == [item["id"] for item in VOICES]
        assert (await client.put("/api/voice/realtime/voice", json={"voice": "zh_male_robot_from_nowhere"})).status_code == 404
        changed = await client.put("/api/voice/realtime/voice", json={"voice": "zh_male_xiaotian_jupiter_bigtts"})
        assert changed.status_code == 200 and changed.json()["current"] == "zh_male_xiaotian_jupiter_bigtts"
        status = (await client.get("/api/voice/realtime/status")).json()
        assert status["voice"] == "zh_male_xiaotian_jupiter_bigtts"
        # The catalog carries both routes so an agent can change the voice when asked to.
        catalog = (await client.get("/api/agent/capabilities")).json()
        operations = {item["operation"] for item in catalog["operations"]}
        assert {"GET /api/voice/realtime/voices", "PUT /api/voice/realtime/voice", "POST /api/simulation/close-batch-proposals"} <= operations
    app.state.store.close()


async def test_voice_endpoints_need_local_auth_and_a_key(tmp_path):
    app = create_app(tmp_path / "voice2.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="voice-test-token")
    app.state.voice_realtime.key_file = tmp_path / "missing-key"
    headers = {"Authorization": "Bearer voice-test-token", "Origin": "http://127.0.0.1:5173"}
    async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime") as client:
        assert (await client.get("/api/voice/realtime/status")).status_code == 401
        status = await client.get("/api/voice/realtime/status", headers=headers)
        assert status.json()["enabled"] is False and status.json()["reason"] == "voice_key_missing"
        assert (await client.post("/api/voice/realtime/announce", headers=headers, json={"kind": "decision", "text": "测试"})).status_code == 409
        (tmp_path / "key").write_text("not-a-real-key")
        app.state.voice_realtime.key_file = tmp_path / "key"
        status = await client.get("/api/voice/realtime/status", headers=headers)
        assert status.json()["enabled"] is True and status.json()["activeCalls"] == 0
        queued = await client.post("/api/voice/realtime/announce", headers=headers, json={"kind": "decision", "text": "测试"})
        assert queued.json() == {"queued": True, "activeCalls": 0, "note": "没有通话时这条播报不会发出；按住说话的回答由软件自动读出，不需要调用播报。"}
    app.state.store.close()


@pytest.mark.parametrize("token", ["wrong", None])
async def test_voice_socket_rejects_a_bad_token(tmp_path, token):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    app = create_app(tmp_path / "voice3.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="voice-test-token")
    (tmp_path / "key").write_text("k"); app.state.voice_realtime.key_file = tmp_path / "key"
    url = "/api/voice/realtime" + (f"?token={token}" if token else "")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(url, headers={"Origin": "http://127.0.0.1:5173"}):
                pass
        assert closed.value.code == 4401
    app.state.store.close()


# -- cloud: a signed-in user calls as themselves; one call per user; the same daily meter ----

async def test_cloud_voice_uses_the_signed_in_user_and_limits_one_call_per_user(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from test_owner_auth import ORIGIN, PASSWORD, PASSWORD_HASH, SESSION_SECRET
    from trader_runtime.data_providers import FakeReadOnlyDataProvider
    from trader_runtime.voice_realtime import CallIdentity, _identity
    monkeypatch.setenv("AI_TRADER_VOICE_REALTIME_KEY", "env-key-for-test")
    app = create_app(tmp_path / "voice-cloud.sqlite3", agent_mode="fake", auth_mode="owner-password", owner_username="charles",
                     owner_password_hash=PASSWORD_HASH, owner_session_secret=SESSION_SECRET, cors_origins={ORIGIN},
                     data_mode="tqkq-sidecar", data_provider=FakeReadOnlyDataProvider(), agent_provider="deepseek",
                     agent_credential_configured=True, runtime_state_root=tmp_path)
    service = app.state.voice_realtime
    assert service.local_mode is False
    async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers={"Origin": ORIGIN}) as client:
        assert (await client.get("/api/voice/realtime/status")).status_code == 401
        login = await client.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})
        cookie = login.cookies.get("ai_trader_owner_auth")
        status = (await client.get("/api/voice/realtime/status")).json()
        # Cloud mode is enabled by the environment key; the source is reported, the value is not.
        assert status["enabled"] is True and status["localMode"] is False and status["keySource"] == "environment"
        assert "env-key-for-test" not in str(status)
        # Business calls run as the signed-in user with their own cookie, not as the local user.
        identity = CallIdentity("owner-x", {"cookie": f"ai_trader_owner_auth={cookie}"})
        token = _identity.set(identity)
        try:
            seen = {}
            async def fake_call(args, *, user, headers=None, session_id=None):
                seen.update(user=user, headers=headers); return {"data": {"items": []}}
            monkeypatch.setattr(app.state.app_capabilities, "call", fake_call)
            await service._call("GET /api/instruments")
            assert seen == {"user": "owner-x", "headers": {"cookie": f"ai_trader_owner_auth={cookie}"}}
        finally:
            _identity.reset(token)
        # Announcements are scoped to the user who queued them.
        queued = await client.post("/api/voice/realtime/announce", json={"kind": "decision", "text": "测试"})
        assert queued.json() == {"queued": True, "activeCalls": 0, "note": "没有通话时这条播报不会发出；按住说话的回答由软件自动读出，不需要调用播报。"}
        assert list(service._pending_announcements) == [app.state.auth.validate(cookie).scoped_user_id]
    # A second concurrent call by the same user is refused with 4429; a stranger's socket with 4401.
    user_id = app.state.auth.validate(cookie).scoped_user_id
    service.calls["busy"] = CallState(user_id=user_id)
    # The caller's own active call keeps voice "enabled" (the client polls this mid-call); only the
    # reason and the count tell the story. The limit itself is enforced at socket accept below.
    busy_status = service.status(user_id)
    assert busy_status["enabled"] is True and busy_status["reason"] == "cloud_call_limit" and busy_status["activeCalls"] == 1
    with TestClient(app) as ws_client:
        # The query string is never an identity in cloud mode (it would end up in the access log).
        # The desktop offers the token as a WebSocket subprotocol (browsers cannot set Authorization).
        for headers, code in (({"Authorization": f"Bearer {cookie}"}, 4429), ({"Cookie": f"ai_trader_owner_auth={cookie}"}, 4429),
                              ({"Sec-WebSocket-Protocol": f"ai-trader-token, bearer.{cookie}"}, 4429),
                              ({"Sec-WebSocket-Protocol": "ai-trader-token, bearer.bogus"}, 4401),
                              ({"Sec-WebSocket-Protocol": f"bearer.{cookie}"}, 4401),
                              ({"Authorization": "Bearer bogus"}, 4401), ({}, 4401)):
            with pytest.raises(WebSocketDisconnect) as closed:
                with ws_client.websocket_connect(f"/api/voice/realtime?token={cookie}", headers={"Origin": ORIGIN, **headers}):
                    pass
            assert closed.value.code == code
    service.calls.clear()


def test_the_runtime_routes_each_utterance_itself():
    """The user's 1.6.0 call (voice call 1f5e0d96): three business requests got Doubao's filler and no
    tool call. The Runtime now decides from the ASR final; these are the call's own sentences."""
    from trader_runtime.voice_realtime import route_utterance
    assert route_utterance("哎，买多一手，我现在看的这个。", pending=False) == "delegate"
    assert route_utterance("卡到哪了？", pending=False) == "delegate"
    assert route_utterance("为什么我看我的持仓还是只有一个？", pending=False) == "delegate"
    assert route_utterance("现在看的是什么界面？好，再下一首多单。", pending=False) == "delegate"
    assert route_utterance("你能把我的这个界面改成深色吗？", pending=False) == "delegate"
    # Confirmation words act on a waiting card only; without one they are small talk for Doubao.
    assert route_utterance("确认。", pending=True) == "confirm"
    assert route_utterance("好的", pending=True) == "confirm"
    assert route_utterance("取消，不下了。", pending=True) == "cancel"
    assert route_utterance("算了", pending=True) == "cancel"
    assert route_utterance("好的", pending=False) == "chat"
    assert route_utterance("确认。", pending=False) == "chat"
    # Greetings and one-word acknowledgements stay with Doubao.
    assert route_utterance("你好。你好。", pending=False) == "chat"
    assert route_utterance("在吗", pending=False) == "chat"
    assert route_utterance("嗯", pending=True) == "chat"
    assert route_utterance("", pending=True) == "chat"
    # A long sentence that merely starts with 好 is a request, not a confirmation.
    assert route_utterance("好，帮我把螺纹钢全部平掉", pending=True) == "delegate"
    # V1.1: hanging up and stopping the read-out are decided here, never delegated to the agent.
    for said in ("好了，关闭语音。", "关闭语音", "结束通话", "挂断吧", "不说了", "退出语音", "好的，再见"):
        assert route_utterance(said, pending=False) == "hangup", said
    assert route_utterance("不说了", pending=True) == "hangup"
    # Two-character orders are requests, not small talk (09-19: "全平" used to reach Doubao).
    for said in ("全平", "清仓", "撤单", "平仓"):
        assert route_utterance(said, pending=False) == "delegate", said
    for said in ("停", "等一下", "别说了", "停一下。", "先别说"):
        assert route_utterance(said, pending=False) == "stop", said
    assert route_utterance("等一下", pending=True) == "stop"
    # Business sentences containing those words still go to the agent.
    assert route_utterance("别说了，先把螺纹平掉", pending=False) == "delegate"
    # In a guided tour the step words move the tour; without one they route as before.
    for said, route in (("下一步", "tour_next"), ("继续。", "tour_next"), ("往下", "tour_next"), ("上一步", "tour_prev"),
                        ("结束引导", "tour_stop"), ("不看了", "tour_stop"), ("结束", "tour_stop")):
        assert route_utterance(said, pending=False, tour_active=True) == route, said
    assert route_utterance("下一步", pending=False) == "delegate"  # no tour: the agent hears it as before
    assert route_utterance("继续", pending=False) == "chat"
    assert route_utterance("结束引导", pending=False) == "delegate"
    assert route_utterance("下一步把螺纹钢平掉", pending=False, tour_active=True) == "delegate"
    assert route_utterance("挂断", pending=False, tour_active=True) == "hangup"
    assert route_utterance("关闭语音之前先看一下账户", pending=False) == "delegate"
    # 09-20 eval ui-05: a sentence that names a page ("我要看持仓那个页") is interface work, not a
    # quick market read — the quick query must miss it so the intent table answers.
    from trader_runtime.voice_intents import match_quick_query
    assert match_quick_query("我要看持仓那个页") is None
    assert route_utterance("我要看持仓那个页", pending=False) == "delegate"
    assert route_utterance("上一次聊的在哪", pending=False) == "delegate"
    # A productId spoken as the product ("IF现在多少钱") is a quick quote when the catalog has it.
    assert route_utterance("IF现在多少钱", pending=False, product_names=["沪深300股指", "IF"]) == "quick"
    assert route_utterance("if现在多少钱", pending=False, product_names=["沪深300股指", "IF"]) == "quick"


def test_the_watchdogs_two_commands_accept_natural_short_sentences():
    """09-22: 撤单/追单 are short spoken sentences around the word, not fixed spellings — but a
    negation or a second trading action keeps the sentence with the agent."""
    from trader_runtime.voice_realtime import is_watch_cancel_command, is_watch_chase_command
    for said in ("撤单", "撤了", "把刚才那张委托撤掉", "撤了吧", "取消挂单", "取消它", "把挂单撤了。"):
        assert is_watch_cancel_command(said.rstrip("。")), said
    for said in ("追单", "追一下", "追价", "追上去", "按对手价追一下", "改成对手价", "用对手价", "把挂单改成对手价"):
        assert is_watch_chase_command(said.rstrip("。")), said
    # Negation, another action, or a long compound sentence: the agent hears it instead.
    for said in ("别撤单", "不要撤了", "不用追了", "先别撤", "买一手螺纹", "撤单之后再买一手",
                 "追加一手", "撤单改价", "帮我看看沪金的行情走势再撤单"):
        assert not is_watch_cancel_command(said), said
        assert not is_watch_chase_command(said), said
    # 没有看护挂单时这些句子不会进任何看护路由（routing 侧还有 watched 门槛，这里只测词面）。
    assert not is_watch_cancel_command("螺纹钢怎么看")
    assert not is_watch_chase_command("螺纹钢怎么看")


def test_the_opening_line_matches_the_kind_of_request_that_was_made():
    """V1.4 (user, 09-19): "他每次都说'我要查一下'，这明明只是个命令". The opening line now comes from the
    request's own type, and an interface command never opens with "查一下"."""
    from trader_runtime.voice_realtime import _NOT_A_CHARACTER, OPENING_LIMIT, interim_line
    by_kind = {
        # 界面 / 导航: the object is taken from the user's own words.
        "好，正在打开图表": ("把图表调出来", "帮我打开那个K线图", "图表给我显示出来"),
        "好，正在打开设置": ("把设置那个东西打开", "帮我进入设置里面看看", "设置那一页打开"),
        "好，正在关设置": ("把设置那个页面关掉", "设置那一块关闭掉", "关闭设置那个东西"),
        "好，正在收起持仓": ("把持仓那块收起来", "持仓那个面板收一下", "隐藏持仓那一块"),
        "好，正在切到深色": ("帮我切换到深色那个模式", "换成深色那种", "调成深色那个样子"),
        # 2026-10-07: 阶梯 / 盘口 / 五档 are the 下单面板 (the 价格阶梯 panel merged into it).
        "好，正在打开下单台": ("把价格阶梯那个东西打开", "阶梯那块显示出来", "帮我展开盘口阶梯", "把五档那块显示出来"),
        # 下单 / 平仓 / 撤单
        "好，这就来": ("买多一手我现在看的这个", "帮我把螺纹钢全部平掉", "把刚才那个委托撤单", "搞一手螺纹多的"),
        # 查询（“平仓记录”是要看的一页账目，不是平仓）
        "我看一下": ("我的持仓现在是什么情况", "账户里还剩多少", "今天盈亏怎么样", "我手里有啥",
                  "帮我看看平仓记录有哪些"),
        # 讲图 / 指标
        "我看一下图": ("为什么不结合KD和MACD一起看", "讲讲现在的均线", "这个形态是什么意思"),
        # 新闻 / 原因 / 影响
        "我查一下": ("最近有什么新闻影响铜", "昨天为什么跌这么多", "有没有政策消息"),
        # 改偏好
        "好，这就改": ("说话再慢一点，把语速调下来", "默认手数改成两手", "成交播报关掉不要推送"),
        # 其它
        "好的": ("帮我随便做点别的", "刚才那个事情再来一次", "你自己看着办吧"),
    }
    for line, utterances in by_kind.items():
        for utterance in utterances:
            assert interim_line(utterance) == line, (utterance, interim_line(utterance))
    # Every line is ≤ 8 字 (punctuation does not count) and an interface request never says "查一下".
    for utterances in by_kind.values():
        for utterance in utterances:
            said = interim_line(utterance)
            assert len(_NOT_A_CHARACTER.sub("", said)) <= OPENING_LIMIT, said
    for utterance in ("打开图表", "把图表调出来", "切到五分钟那个周期", "把持仓那块收起来"):
        assert "查一下" not in interim_line(utterance), utterance
    # Interface work whose object has no short name of ours: the turn still opens, without inventing one.
    assert interim_line("帮我把那个面板打开") == "好的，马上"
    assert interim_line("把右边那个侧栏收起来") == "好的，马上"
    # #738 审查收口: 尚在 general/delegate 语义判断阶段的句子（正式 caller 传入实际 route），等待
    # 口播只说中性的“我先看看”——不再为复述动作另做一套清仓/混合句词面判断，也不承诺必出卡。
    # 「关掉所有持仓啦」词面仍是界面口播「正在关持仓」的形状，正因如此等待线必须来自 route 而非词面。
    assert interim_line("关掉所有持仓啦", route="delegate") == "我先看看"
    assert interim_line("关掉所有持仓然后打开面板", route="delegate") == "我先看看"
    assert interim_line("关掉所有持仓啦", route="general") == "我先看看"
    # 已派发的明确动作不传语义判断 route，沿原词面路径。
    assert interim_line("打开图表", route="settings") == "好，正在打开图表"


def test_the_sentence_streamer_reads_the_first_paragraph_sentence_by_sentence():
    from trader_runtime.voice_realtime import SentenceStreamer
    streamer = SentenceStreamer()
    assert streamer.push("结论：螺纹短线偏弱。KD 已经") == ["结论：螺纹短线偏弱"]
    assert streamer.push("结论：螺纹短线偏弱。KD 已经死叉，MACD 还在零轴上方；两者不一致") == ["KD 已经死叉，MACD 还在零轴上方；"]
    full = "结论：螺纹短线偏弱。KD 已经死叉，MACD 还在零轴上方；两者不一致，所以没结合着看。\n\n| 指标 | 状态 |\n|---|---|\n| KD | 死叉 |"
    assert streamer.push(full) == ["两者不一致，所以没结合着看"]
    assert streamer.finish(full) == [] and streamer.truncated is True
    assert streamer.sentences == ["结论：螺纹短线偏弱", "KD 已经死叉，MACD 还在零轴上方；", "两者不一致，所以没结合着看"]
    # An answer without a terminator is flushed at the end; markdown is stripped; nothing after a table is read.
    short = SentenceStreamer()
    assert short.finish("**好的**，已经切到螺纹2701") == ["好的，已经切到螺纹2701"] and short.truncated is False
    tabled = SentenceStreamer()
    assert tabled.finish("| 合约 | 手数 |\n|---|---|\n| rb | 2 |") == [] and tabled.truncated is True


def test_the_closing_remark_only_when_a_table_or_real_prose_was_skipped():
    """09-18 call: "细节在对话里" closed nearly every answer. Three shapes: a table → "表格我放对话里了"; ≥80
    chars of prose past the first paragraph → "后面的我放对话里了"; a short tail (blank line + one line, a
    folded progress block) or no tail → nothing."""
    from trader_runtime.voice_realtime import SentenceStreamer
    tabled = SentenceStreamer()
    tabled.finish("KD 死叉，MACD 还在零轴上方。\n\n| 指标 | 状态 |\n|---|---|\n| KD | 死叉 |")
    assert tabled.skipped_line() == "表格我放对话里了。"
    prose = SentenceStreamer()
    prose.finish("结论：短线偏弱。\n\n" + "从日线看，价格跌破 20 日均线后没有回补，成交量放大说明空头主动；" * 3)
    assert prose.truncated and prose.skipped_line() == "后面的我放对话里了。"
    short = SentenceStreamer()
    assert short.finish("好的，已经切到螺纹2701。\n\n（已完成）") == ["好的，已经切到螺纹2701"]
    assert short.truncated and short.skipped_line() is None
    plain = SentenceStreamer()
    plain.finish("设置已经打开了。")
    assert plain.skipped_line() is None
    folded = SentenceStreamer()
    folded.finish("持仓两手多单。\n\n```\nget_positions → ok\n```")
    assert folded.skipped_line() is None


async def test_delegate_streams_the_answer_sentence_by_sentence_while_the_turn_runs(tmp_path, monkeypatch):
    """The voice reads the agent's first paragraph as it streams (V1.1): each sentence reaches
    `on_sentence` from the in-process turn-text listener, and the end is marked with None."""
    app, _, service = await _voice_app(tmp_path)
    state = CallState(window_id=WINDOW)
    listeners: dict[str, list] = {}

    class StreamingAdapter:
        def subscribe_turn_text(self, session_id, listener):
            listeners.setdefault(session_id, []).append(listener)
            return lambda: listeners[session_id].remove(listener)
    app.state.adapter = StreamingAdapter()
    original = service._call

    async def call_with_deltas(operation, **kwargs):
        if operation == "chat:send":
            session_id = kwargs["body"]["sessionId"]
            assert session_id in listeners and listeners[session_id], "the listener must be attached before the turn starts"
            for listener in list(listeners[session_id]):
                # An earlier question's answer still streaming is not ours.
                listener({"turnId": "t0", "role": "assistant", "text": "上一题的答案。", "final": False})
                listener({"turnId": "t0", "role": "assistant", "text": "上一题的答案。", "final": True})
                listener({"turnId": "run-user", "role": "user", "text": "看看行情", "final": True})
                listener({"turnId": "t1", "role": "assistant", "text": "正在处理这个请求…", "final": False})
                listener({"turnId": "t1", "role": "assistant", "text": "沪金现在 823 元。", "final": False})
                listener({"turnId": "t1", "role": "assistant", "text": "沪金现在 823 元。今天涨了 0.7%，", "final": False})
                listener({"turnId": "t1", "role": "assistant", "text": "沪金现在 823 元。今天涨了 0.7%，短线偏强\n\n| 指标 | 值 |\n|---|---|\n| KD | 金叉 |", "final": True})
        return await original(operation, **kwargs)
    monkeypatch.setattr(service, "_call", call_with_deltas)
    heard: list = []
    result = await service.run_tool("delegate", {"utterance": "看看行情"}, state, on_sentence=heard.append, follow_up=True)
    assert result["ok"] is True
    assert heard == ["沪金现在 823 元", "今天涨了 0.7%，短线偏强", "表格我放对话里了。", None]
    # The listener is detached after the turn; the chat session survives for the next request.
    assert not listeners[state.chat_session_id]
    app.state.store.close()


async def test_a_same_text_typed_message_is_not_claimed_as_the_delegates_own_turn(tmp_path):
    """#523: 键盘输入了同样的话——文本匹配只是兜底绑定。run 开始(task.started)时核对本次 run 自己的
    用户轮 (`{run_id}-user`)：没见过就撤回误认领，键盘那轮的答案不再被当作语音轮读出；语音轮自己的
    用户轮到达后重新认领，答案照常逐句读出。"""
    app, _, service = await _voice_app(tmp_path, "subscribe-misclaim.sqlite3")
    captured: dict[str, Any] = {}

    class ScriptedAdapter:
        def subscribe_turn_text(self, session_id, listener, *, on_run_id=None):
            captured["listener"] = listener
            captured["started"] = on_run_id
            return lambda: None
    app.state.adapter = ScriptedAdapter()

    heard: list = []
    runs: list[str] = []
    service._subscribe_turn_text("session-x", heard.append, "看看行情", on_run_id=runs.append)
    listener = captured["listener"]

    listener({"turnId": "typed-1-user", "role": "user", "text": "看看行情", "final": True})  # 键盘同文：先被误认领
    captured["started"]("typed-run")  # 键盘那轮开始，它的 run-user 没被我们见过 —— 认领被撤回
    listener({"turnId": "t-typed", "role": "assistant", "text": "键盘那轮的答案。", "final": True})
    assert heard == []  # 不把键盘那轮的答案读给语音用户

    listener({"turnId": "voice-run-user", "role": "user", "text": "看看行情", "final": True})
    captured["started"]("voice-run")  # 语音轮自己的用户轮已见：认领有效，run 也可被"停"取消
    listener({"turnId": "t-voice", "role": "assistant", "text": "这是语音轮的答案。", "final": True})
    assert heard == ["这是语音轮的答案", None]  # 句尾的。读出来前被剥掉，与其余朗读一致
    assert runs == ["typed-run", "voice-run"]
    app.state.store.close()


async def test_delegate_follows_the_conversation_the_window_shows(tmp_path):
    """The user pressed 新对话 mid-call (1.6.3 call 4655016f): the next spoken turn belongs to the new
    conversation the window shows (its pulse's chatSessionId), not to the session named at hello."""
    app, _, service = await _voice_app(tmp_path)
    state = CallState(window_id=WINDOW)
    first = await service.run_tool("delegate", {"utterance": "看看行情"}, state)
    old_session = first["sessionId"]
    new_session = app.state.broker.create_session("local", "native", "local-user").session_id
    await _register_window(app, chat_session_id=new_session, revision=1)
    second = await service.run_tool("delegate", {"utterance": "再看看持仓"}, state)
    assert second["sessionId"] == new_session and state.chat_session_id == new_session
    user_turns = [e["payload"]["text"] for _, e in app.state.store.list_events(new_session)
                  if e["name"] == "message.completed" and e["payload"].get("role") == "user"]
    assert user_turns == ["再看看持仓"]
    assert "再看看持仓" not in [e["payload"].get("text") for _, e in app.state.store.list_events(old_session) if e["name"] == "message.completed"]


async def test_tour_words_move_the_tour_the_window_shows_and_speak_each_step(tmp_path):
    """Replay: the agent started a tour (window reports tour.active); "下一步" advances it and the voice
    reads that step's `say`; past the last step or on "结束" the tour ends; without a tour the words are
    ordinary speech."""
    app = create_app(tmp_path / "voice-tour.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="voice-test-token")
    service = VoiceRealtimeService(app, key_file=tmp_path / "missing-key")
    steps = [{"target": "rail:order", "label": "下单区", "say": "下单区在这里，点确认才会提交。"},
             {"target": "positions-bar", "label": "底栏", "say": "底栏随时能看持仓和委托。"}]
    window = {"windowId": "this-mac", "online": True, "revision": 4, "state": {"tour": {"active": True, "step": 1, "total": 2, "current": steps[0]}}}
    sent: list[dict] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        assert operation == "POST /api/app/ui/commands" and body["windowId"] == "this-mac" and body["expectedRevision"] == window["revision"]
        sent.append(body["changes"])
        op = body["changes"]["tour"]["op"]
        tour = window["state"]["tour"]
        if op == "next" and tour["step"] < tour["total"]:
            tour = {**tour, "step": tour["step"] + 1, "current": steps[tour["step"]]}
        elif op == "prev" and tour["step"] > 1:
            tour = {**tour, "step": tour["step"] - 1, "current": steps[tour["step"] - 2]}
        else:
            tour = {"active": False, "step": 0, "total": 0, "current": None}
        window["state"] = {"tour": tour}; window["revision"] += 1
        return {"ok": True, "data": {"status": "applied", "state": window["state"], "observedRevision": window["revision"]}}
    service._call = fake_call  # type: ignore[method-assign]
    state = CallState(window_id="this-mac")
    assert await service.tour_active(state) is True
    said = await service.run_tool("tour_next", {}, state)
    assert said["ok"] and said["say"] == "第2步，底栏随时能看持仓和委托。" and said["tour"]["step"] == 2
    said = await service.run_tool("tour_prev", {}, state)
    assert said["say"] == "第1步，下单区在这里，点确认才会提交。"
    said = await service.run_tool("tour_next", {}, state)
    said = await service.run_tool("tour_next", {}, state)  # past the last step
    assert said["ok"] and said["say"] == "引导结束了。" and not said["tour"]["active"]
    assert [c["tour"]["op"] for c in sent] == ["next", "prev", "next", "next"]
    assert await service.tour_active(state) is False
    assert (await service.run_tool("tour_stop", {}, state))["say"] == "现在没有在引导。"
    assert (await service.run_tool("tour_next", {}, CallState()))["say"] == "软件窗口现在不在线。"
    app.state.store.close()


def test_simple_interface_requests_match_the_fixed_intent_table():
    """09-18 call 015c71f4: "打开设置" went through Doubao's filler, a bridge line and the agent (5 s). The
    table answers each class without a model; anything with business content stays with the agent."""
    from trader_runtime.voice_intents import Intent, match_intent

    def hit(said: str) -> Intent:
        intent = match_intent(said)
        assert intent is not None, said
        return intent
    assert hit("帮我打开设置。").changes == {"settingsOpen": True}
    assert hit("把设置关了").changes == {"settingsOpen": False}
    assert hit("打开语音设置").changes == {"settingsPage": "voice"}
    assert hit("看一下设置里的图表").changes == {"settingsPage": "chart"}
    # 设置拆页 (2026-09-30): the split pages need the 设置/页 suffix like the others ("看看账户" stays an
    # account question, "打开对话" / "打开对话页" stay the panel).
    assert hit("打开对话").changes == {"panel": "ai"} and hit("打开对话页").changes == {"panel": "ai"}
    assert hit("打开模型设置").changes == {"settingsPage": "ai"} and hit("打开对话设置").changes == {"settingsPage": "ai"}
    assert hit("设置里的对话").changes == {"settingsPage": "ai"}
    assert hit("打开记忆设置").changes == {"settingsPage": "memory"} and hit("打开画像设置").changes == {"settingsPage": "memory"}
    assert hit("打开关于页").changes == {"settingsPage": "about"}
    for said in ("打开问题与建议", "帮我打开反馈", "进入反馈页面", "设置里的问题与建议"):
        assert hit(said).changes == {"settingsPage": "feedback"}, said
    assert hit("打开账户设置").changes == {"settingsPage": "account"} and hit("打开账号设置").changes == {"settingsPage": "account"}
    for bare in ("看看账户", "打开记忆", "打开更新"):
        intent = match_intent(bare)
        assert intent is None or intent.kind != "settingsPage", bare
    assert hit("换成深色。").changes == {"theme": "dark"} and hit("切换到浅色主题").changes == {"theme": "light"} and hit("跟随系统").changes == {"theme": "system"}
    assert hit("切到复盘").changes == {"view": "review"} and hit("回交易台").changes == {"view": "trade"}
    # 报价总表 (2026-10-01): 报价/报价表/总表/行情表/全部品种 open the board view; 行情页 stays the trade view.
    assert hit("打开报价").changes == {"view": "board"} and hit("看全部品种").changes == {"view": "board"}
    assert hit("报价表").changes == {"view": "board"} and hit("打开行情表").changes == {"view": "board"}
    assert hit("回到报价总表").changes == {"view": "board"} and hit("打开报价页").changes == {"view": "board"}
    assert hit("打开行情页").changes == {"view": "trade"}
    # 自选 (2026-10-02): the board already filtered to the starred products (“自选” never names a
    # contract, so this lands before the product-word matching).
    assert hit("打开自选").changes == {"view": "board"} and hit("打开自选").follow == [{"boardExchange": "watchlist"}]
    assert hit("看看我的自选").follow == [{"boardExchange": "watchlist"}]
    assert hit("只看自选").follow == [{"boardExchange": "watchlist"}] and hit("回自选列表").follow == [{"boardExchange": "watchlist"}]
    # 2026-09-25: the 对话面板 has no filter any more — opening it is the whole command.
    assert hit("打开语音").changes == {"panel": "ai"} and hit("打开语音").follow == []
    assert hit("关闭对话").changes == {"panel": None}
    assert hit("打开价格阶梯").changes == {"panel": "order"} and hit("看五档").changes == {"panel": "order"}
    assert hit("打开盘口").changes == {"panel": "order"} and hit("收起下单台").changes == {"panel": None}
    # 1.6.6: 持仓 is a side panel; only a sentence that names the bottom bar expands the bar.
    assert hit("打开持仓").changes == {"panel": "positions"} and hit("收起持仓").changes == {"panel": None}
    assert hit("打开底栏").changes == {"positionsBar": "expanded"} and hit("收起底栏").changes == {"positionsBar": "collapsed"}
    assert hit("打开策略面板").changes == {"panel": "strategy"}
    # 盯盘 is the 策略面板's 运行中 tab; 平仓记录 / 成交 is the 持仓 成交 tab.
    assert hit("打开AI盯盘").changes == {"panel": "strategy"} and hit("打开AI盯盘").follow == [{"strategyTab": "running"}]
    assert hit("打开AI").changes == {"panel": "ai"} and hit("打开AI").follow == []
    assert hit("看一下委托").follow == [{"positionsTab": "orders"}] and hit("看平仓记录").follow == [{"positionsTab": "fills"}]
    assert hit("打开帮助").changes == {"dialog": {"name": "help", "open": True}}
    assert hit("看五分钟").changes == {"duration": 300} and hit("切到日线").changes == {"duration": 86400} and hit("换成1小时的").changes == {"duration": 3600}
    # 30 分 / 周线 / 分时 (2026-10-02): 分时是图表形态不是周期；回 K 线走原有的 view 话和周期话。
    assert hit("看30分钟图").changes == {"duration": 1800} and hit("切到三十分钟").changes == {"duration": 1800}
    assert hit("看周线").changes == {"duration": 604800} and hit("换成周K").changes == {"duration": 604800}
    assert hit("切到分时").changes == {"chartView": "timeshare"} and hit("看分时图").changes == {"chartView": "timeshare"} and hit("分时").changes == {"chartView": "timeshare"}
    assert hit("返回原图").changes == {"chartFocus": None}
    assert hit("清除AI标注").changes == {"chartAnnotations": {"op": "clear"}} and hit("把标注清掉").changes == {"chartAnnotations": {"op": "clear"}}
    assert hit("开个新对话").changes == {"chatSession": {"new": True}}
    # 历史对话 (09-20 eval ui-10): the AI panel's session list, a disclosure switch, both modes.
    assert hit("上一次聊的在哪").kind == "chatHistory" and hit("历史对话").kind == "chatHistory"
    assert hit("打开历史对话").changes == {"disclosure": {"name": "chat-history", "open": True}}
    assert hit("之前聊的看看").kind == "chatHistory" and hit("历史会话列表").kind == "chatHistory"
    assert hit("切到螺纹").symbol_query == "螺纹" and hit("换到沪金2612合约").symbol_query == "沪金2612"
    # 合约信息卡 (2026-10-02)：行情头部的 ⓘ 弹层；“打开合约信息”不能被当成合约名解析。
    assert hit("打开合约信息").changes == {"disclosure": {"name": "contract-info", "open": True}}
    assert hit("看看这个合约的信息").changes == {"disclosure": {"name": "contract-info", "open": True}}
    assert hit("把合约信息关了").changes == {"disclosure": {"name": "contract-info", "open": False}}
    # Loosely worded sayings from the 09-19 fuzzy evaluation.
    assert hit("太亮了").changes == {"theme": "dark"} and hit("屏幕太暗了").changes == {"theme": "light"}
    assert hit("换个颜色").changes == {"theme": "toggle"} and "换涨跌色" in hit("换个颜色").say
    assert hit("字太小").changes == {"fontScale": "larger"} and hit("字大了").changes == {"fontScale": "smaller"}
    # 09-20 eval ui-03/18 additions: the missing verb/object-first wordings of a font-size step.
    assert hit("字放大").changes == {"fontScale": "larger"} and hit("字缩小").changes == {"fontScale": "smaller"}
    assert hit("大点字").changes == {"fontScale": "larger"} and hit("小点字").changes == {"fontScale": "smaller"}
    assert hit("字大点").changes == {"fontScale": "larger"} and hit("字小点").changes == {"fontScale": "smaller"}
    assert hit("我要看持仓那个页").changes == {"panel": "positions"} and hit("开一下下单的地方").changes == {"panel": "order"}
    assert hit("把刚才画的擦了").changes == {"chartAnnotations": {"op": "clear"}} and hit("帮我开个新的聊天").kind == "chatSession"
    assert hit("把设置打开").changes == {"settingsOpen": True} and hit("设置开一下").changes == {"settingsOpen": True}
    assert hit("回到K线").changes == {"view": "trade"}
    # V1.4 (user, 09-19: "我让他打开图表，还要反应好多秒"): the chart view answers to its everyday names, and
    # an object followed by a closing verb closes it.
    for said in ("打开图表", "把图表调出来", "看图", "回到图表", "图表页", "看一下K线图", "切到图表", "显示图表"):
        assert hit(said).changes == {"view": "trade"}, said
    assert hit("把持仓收起来").changes == {"panel": None} and hit("对话关掉").changes == {"panel": None}
    assert hit("看看沪金").symbol_query == "沪金" and hit("看一下螺纹").symbol_query == "螺纹"
    # The chart SETTINGS page still needs the 设置 word; it is never what "打开图表" means.
    assert hit("打开图表设置").changes == {"settingsPage": "chart"} and hit("看一下设置里的图表").changes == {"settingsPage": "chart"}
    for said in ("买一手螺纹", "现在怎么看", "把说完判停改成1秒", "设置一下止损", "深色的K线怎么看", "帮我看看持仓亏了多少", "切到螺纹然后买一手",
                 "为什么持仓只有一个", "打开设置把语速调快", "我有一个建议", "反馈这个订单的问题", "打开反馈并发送邮件"):
        assert match_intent(said) is None, said


async def test_a_matched_intent_is_one_ui_command_with_a_receipt_and_a_line(tmp_path):
    """The fast path sends the command to the caller's window (follow-ups after), speaks the result and
    resolves a spoken contract name through the catalog; anything not applied or ambiguous reports
    ok=False so the sentence falls back to the agent."""
    from trader_runtime.voice_intents import match_intent, resolve_symbol
    app, _, service = await _voice_app(tmp_path)
    window = {"windowId": "this-mac", "online": True, "revision": 4, "state": {"settingsOpen": False}}
    sent: list[dict] = []
    catalog = {"items": [{"symbol": "SHFE.rb2701", "instrumentId": "rb2701", "instrumentName": "螺纹钢2701", "productId": "rb", "isMain": True},
                         {"symbol": "SHFE.rb2705", "instrumentId": "rb2705", "instrumentName": "螺纹钢2705", "productId": "rb", "isMain": False},
                         {"symbol": "SHFE.au2612", "instrumentId": "au2612", "instrumentName": "沪金2612", "productId": "au", "isMain": True}]}

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == "GET /api/instruments":
            return {"data": {"items": [i for i in catalog["items"] if query["search"].lower() in (i["symbol"] + i["instrumentName"] + i["productId"]).lower()]}}
        assert operation == "POST /api/app/ui/commands" and body["windowId"] == "this-mac"
        sent.append((body["expectedRevision"], body["changes"]))
        if "panel" in body["changes"] and body["changes"]["panel"] == "strategy":
            return {"data": {"status": "rejected"}}
        window["revision"] += 1
        return {"data": {"status": "applied", "observedRevision": window["revision"], "state": {}}}
    service._call = fake_call  # type: ignore[method-assign]
    state = CallState(window_id="this-mac")
    opened = await service.run_intent(match_intent("打开设置"), state)
    assert opened == {"ok": True, "changes": {"settingsOpen": True}, "say": "设置打开了"}
    orders = await service.run_intent(match_intent("看一下委托"), state)
    assert orders["ok"] and sent[-2:] == [(5, {"positionsBar": "expanded"}), (6, {"positionsTab": "orders"})]  # follow-up uses the receipt's revision
    assert (await service.run_intent(match_intent("打开策略面板"), state))["ok"] is False
    # Font size steps from the window's current scale; at the top it says so without a command.
    window["state"]["fontScale"] = "large"
    bigger = await service.run_intent(match_intent("字太小"), state)
    assert bigger["changes"] == {"fontScale": "extra-large"} and bigger["say"] == "字放大了"
    window["state"]["fontScale"] = "extra-large"
    before = len(sent)
    assert (await service.run_intent(match_intent("字太小"), state))["say"] == "字已经最大了" and len(sent) == before
    window["state"]["fontScale"] = "standard"
    assert (await service.run_intent(match_intent("字大了"), state))["say"] == "字已经最小了"
    window["state"]["theme"] = "dark"
    toggled = await service.run_intent(match_intent("换个颜色"), state)
    assert toggled["changes"] == {"theme": "light"} and "换涨跌色" in toggled["say"]
    rb = await service.run_intent(match_intent("切到螺纹"), state)
    assert rb["ok"] and rb["changes"] == {"symbol": "SHFE.rb2701"} and rb["say"] == "切到螺纹钢2701了"
    assert (await service.run_intent(match_intent("切到白银"), state)) == {"ok": False, "error": "symbol_unresolved", "uiCommandAttempted": False}
    assert resolve_symbol("rb2705", catalog["items"])["symbol"] == "SHFE.rb2705"
    assert resolve_symbol("rb", catalog["items"])["symbol"] == "SHFE.rb2701"  # product: the main contract
    assert resolve_symbol("2", catalog["items"]) is None  # ambiguous
    assert CallState(window_id=None) and (await service.run_intent(match_intent("打开设置"), CallState(window_id=None)))["ok"] is False
    app.state.store.close()


async def test_one_voice_per_turn_the_runtime_mutes_doubaos_filler_and_answers_a_simple_request_itself(tmp_path, monkeypatch):
    """09-18 call 015c71f4 ("打开设置" → Doubao "好" at 2.4 s → bridge "我查一下" at 3 s → answer at 5 s): the
    turn is now one voice. A scripted upstream plays Doubao: after the ASR final it starts a filler; the
    Runtime cancels it before any audio reaches the client, sends the UI command with a receipt and
    speaks "设置打开了" once. The filler's words never reach the transcript."""
    import sys
    import types

    import trader_runtime.voice_realtime as module
    from trader_runtime.voice_realtime import CallIdentity, _run_call
    # The scripted filler starts 0.15 s after the final: the mute wait must outlive it, so the
    # production bound (2 s) stands here — it ends early on the filler's response.done anyway.
    monkeypatch.setattr(module, "ROUTED_RESPONSE_WAIT_SECONDS", 2.0)
    app, _, service = await _voice_app(tmp_path)
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {"settingsOpen": False}}
    commands: list[dict] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        assert operation == "POST /api/app/ui/commands"
        commands.append(body["changes"]); window["revision"] += 1
        return {"data": {"status": "applied", "observedRevision": window["revision"], "state": {}}}
    service._call = fake_call  # type: ignore[method-assign]

    upstream_sent: list[dict] = []
    to_model: asyncio.Queue = asyncio.Queue()

    class FakeUpstream:
        async def send(self, raw: str) -> None:
            payload = json.loads(raw)
            upstream_sent.append(payload)
            kind = payload["type"]
            if kind == "session.create":
                await to_model.put({"type": "session.created", "session": {"id": "s1"}})
            elif kind == "input_audio_buffer.append":
                # The user said "打开设置": ASR final, then Doubao starts its own filler (text, then audio).
                await to_model.put({"type": "conversation.item.input_audio_transcription.completed", "text": "打开设置。"})
                await asyncio.sleep(0.15)
                await to_model.put({"type": "response.output_text.delta", "delta": "好"})
                await to_model.put({"type": "response.output_audio.started"})
                await to_model.put({"type": "response.output_audio.delta", "delta": "AAAA"})
            elif kind == "response.cancel":
                await to_model.put({"type": "response.canceled"})
                # Doubao still delivers the audio it already synthesised after `canceled`; none of it may play.
                await to_model.put({"type": "response.output_audio.delta", "delta": "AAAA"})
                await to_model.put({"type": "response.output_audio.done"})
                await to_model.put({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
            elif kind == "speech_text_buffer.commit":
                await to_model.put({"type": "response.output_audio.started"})
                await to_model.put({"type": "response.output_audio.delta", "delta": "QUJD"})
                await to_model.put({"type": "response.output_audio.done"})
                await to_model.put({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
            elif kind == "session.close":
                await to_model.put({"type": "session.closed"})

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = await to_model.get()
            return json.dumps(item)

    class Connect:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return FakeUpstream()
        async def __aexit__(self, *a): return False
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=Connect))

    client_out: list[dict] = []
    inbound: asyncio.Queue = asyncio.Queue()

    class FakeClient:
        async def send_text(self, text: str) -> None:
            client_out.append(json.loads(text))
        async def receive(self) -> dict:
            return await inbound.get()
        async def close(self) -> None: ...
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})

    async def finish_later():
        for _ in range(60):
            await asyncio.sleep(0.1)
            if any(e.get("type") == "text" and e.get("delta") == "设置打开了" for e in client_out):
                break
        await inbound.put({"type": "websocket.disconnect"})
    asyncio.create_task(finish_later())
    await asyncio.wait_for(_run_call(service, FakeClient(), "key", CallIdentity(user_id="local-user", headers={})), timeout=15)

    assert commands == [{"settingsOpen": True}], client_out
    kinds = [p["type"] for p in upstream_sent]
    assert "response.cancel" in kinds and kinds.index("response.cancel") < kinds.index("speech_text_buffer.commit")
    assert [p["text"] for p in upstream_sent if p["type"] == "speech_text_buffer.commit"] == ["设置打开了"]
    texts = [e["delta"] for e in client_out if e.get("type") == "text"]
    assert texts == ["设置打开了"]  # the muted filler "好" is not in the transcript
    assert not any(e.get("type") == "audio" and e.get("data") == "AAAA" for e in client_out)  # its audio never played
    timeline = [e["event"] for e in client_out[-1]["timeline"]]
    assert "modelReplyMuted" in timeline and "intentApplied" in timeline and "interimSpoken" not in timeline
    routed = next(e for e in client_out[-1]["timeline"] if e["event"] == "routed")
    assert routed["by"] == "intent" and routed["action"] == "settings"
    app.state.store.close()


async def test_the_delegate_wait_bridge_is_the_neutral_line_from_the_actual_route(tmp_path, monkeypatch):
    """#730/#738 收口: 「关掉所有持仓啦」走 delegate，第一句迟迟不来时，等待口播由正式 `read_out`
    传入的实际 route（delegate）决定——中性的“我先看看”，不是词面推断的“正在关持仓”，也不承诺
    必出卡。已派发的界面动作（上一用例）不传语义判断 route，仍说它自己的那一行。"""
    import sys
    import types

    import trader_runtime.voice_realtime as module
    from trader_runtime.voice_realtime import CallIdentity, _run_call
    # The scripted chat:send answer is 0.4 s away: the bridge must fire first.
    monkeypatch.setattr(module, "INTERIM_AFTER_SECONDS", 0.05)
    app, _, service = await _voice_app(tmp_path, "bridge-delegate.sqlite3")
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == "POST /api/sessions":
            return {"data": {"sessionId": "s-bridge"}}
        assert operation == CHAT_SEND, operation
        await asyncio.sleep(0.4)  # the agent is still judging: the bridge speaks into this silence
        return {"data": {"sessionId": "s-bridge", "runStatus": "completed", "timedOut": False,
                         "reply": "已为你生成批量平仓卡。", "spoken": "已生成批量平仓卡，请确认。",
                         "pendingProposals": []}}
    service._call = fake_call  # type: ignore[method-assign]

    class QuietAdapter:
        # The stream never sends a sentence: the turn's own spoken line closes it instead.
        def subscribe_turn_text(self, session_id, listener, *, on_run_id=None):
            return lambda: None
    app.state.adapter = QuietAdapter()

    to_model: asyncio.Queue = asyncio.Queue()

    class FakeUpstream:
        async def send(self, raw: str) -> None:
            payload = json.loads(raw)
            kind = payload["type"]
            if kind == "session.create":
                await to_model.put({"type": "session.created", "session": {"id": "s1"}})
            elif kind == "input_audio_buffer.append":
                await to_model.put({"type": "conversation.item.input_audio_transcription.completed", "text": "关掉所有持仓啦。"})
            elif kind == "response.cancel":
                await to_model.put({"type": "response.canceled"})
                await to_model.put({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
            elif kind == "speech_text_buffer.commit":
                await to_model.put({"type": "response.output_audio.started"})
                await to_model.put({"type": "response.output_audio.delta", "delta": "QUJD"})
                await to_model.put({"type": "response.output_audio.done"})
                await to_model.put({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
            elif kind == "session.close":
                await to_model.put({"type": "session.closed"})

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = await to_model.get()
            return json.dumps(item)

    class Connect:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return FakeUpstream()
        async def __aexit__(self, *a): return False
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=Connect))

    client_out: list[dict] = []
    inbound: asyncio.Queue = asyncio.Queue()

    class FakeClient:
        async def send_text(self, text: str) -> None:
            client_out.append(json.loads(text))
        async def receive(self) -> dict:
            return await inbound.get()
        async def close(self) -> None: ...

    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})

    async def finish_later():
        for _ in range(60):
            await asyncio.sleep(0.1)
            if any(e.get("type") == "text" and "批量平仓卡" in e.get("delta", "") for e in client_out):
                break
        await inbound.put({"type": "websocket.disconnect"})
    asyncio.create_task(finish_later())
    await asyncio.wait_for(_run_call(service, FakeClient(), "key", CallIdentity(user_id="local-user", headers={})), timeout=15)

    texts = [e["delta"] for e in client_out if e.get("type") == "text"]
    assert texts[0] == "我先看看", texts  # the route's neutral wait, never “正在关持仓”
    timeline = client_out[-1]["timeline"]
    interim = next(e for e in timeline if e["event"] == "interimSpoken")
    assert interim["tool"] == "delegate" and interim["text"] == "我先看看"
    routed = next(e for e in timeline if e["event"] == "routed")
    assert routed["action"] == "delegate"
    assert any("批量平仓卡" in text for text in texts)  # the turn's own line closed it
    app.state.store.close()


# -- Interface work shares the Native UI Owner ------------------------------------------------------

def test_interface_work_the_fixed_table_misses_shares_the_native_ui_owner():
    """Anything the fixed table already answers keeps its zero-LLM path; a plainly worded interface
    request it does not recognise goes to the shared Native owner; business queries keep their route."""
    from trader_runtime.voice_intents import looks_like_ui, match_intent
    ui = ["帮我把右边那个面板打开", "把下面那个阶梯展开一下", "麻烦把策略那一块显示出来",
          "能不能把设置那个页面打开", "把对话那一栏收起来", "帮我换一下界面的配色", "把字体调一调",
          "我想进入复盘那个页面", "帮我把底栏那条展开", "把持仓那个面板隐藏掉", "切换一下K线的周期"]
    names = ["螺纹钢", "沪金", "铁矿石"]  # the catalog's product names, as the call passes them
    for said in ui:
        assert match_intent(said) is None, said  # the fixed table does not claim it …
        assert looks_like_ui(said) and route_utterance(said, pending=False, product_names=names) == "delegate", said
    not_ui = ["买多一手我现在看的这个", "帮我把螺纹钢全部平掉", "把刚才那个委托撤单", "为什么我的持仓还是只有一个",
              "最近有什么新闻影响铜", "结合KD和MACD讲讲现在的走势", "这一波是不是要跌", "打开设置把语速调快",
              "现在看的是什么界面？好，再下一首多单。", "关闭语音之前先看一下账户", "帮我看看持仓亏了多少",
              "螺纹的K线怎么样", "该不该在这里加仓",
              # #713: 口语的批量平仓/撤单说法是交易语义，不是收起面板的界面话。
              "关掉所有持仓", "把持仓都关了", "关闭所有仓位", "把仓位都关掉", "退出所有持仓",
              "关闭全部委托", "把委托都关了", "挂单都撤了",
              # 讲图：动词和对象都像界面话，要的却是讲解，不是把图表打开（审查 #233）。
              "看看图上这根线", "帮我看看这波", "给我讲讲图", "说说这段行情", "把那波走势标一下"]
    for said in not_ui:
        assert route_utterance(said, pending=False) in {"delegate", "quick"}, said
    # A waiting card, a tour, hangup and stop words all still win over the new route.
    assert route_utterance("确认", pending=True) == "confirm" and route_utterance("关闭语音", pending=False) == "hangup"
    assert route_utterance("打开图表", pending=False) == "delegate" and match_intent("打开图表") is not None  # the fast path, not `ui`


def test_close_all_and_cancel_all_wording_goes_to_the_agent_not_the_ui_route():
    """#713 的另一半：路由后的去向。批量平仓说法（语音路）交给交易 agent 出卡；委托说法交给
    agent 撤单；「关掉持仓 / 关掉所有持仓面板」仍是界面话（固定表或共享 UI Owner），不是交易。"""
    from trader_runtime.order_intent_parse import states_order
    from trader_runtime.voice_intents import looks_like_ui, match_intent
    names = ["螺纹钢", "沪金", "铁矿石"]
    for said in ("关掉所有持仓", "把持仓都关了", "关闭所有仓位", "持仓全部关掉", "把仓位都关掉", "退出所有持仓",
                 "关闭全部委托", "把委托都关了", "挂单都撤了", "关掉所有委托",
                 # #722 审查：客气话结尾仍是批量平仓；限定范围的撤单与持仓提醒设置交给 agent（界面工具做不了）。
                 "关掉所有持仓好吗", "关掉所有持仓一下", "关掉所有持仓了吧", "关闭所有委托中的螺纹单",
                 "关掉全部委托里价格高于3500的", "关闭所有持仓提醒", "关掉全部持仓预警",
                 "关掉所有持仓哦", "关掉所有持仓里盈利的", "关掉所有仓位里螺纹的",
                 # #730: 句尾带白名单外口语词的说法不出确定性卡，也不是界面话，交给 agent 判断。
                 "关掉所有持仓啦", "关掉所有持仓呗", "关掉所有持仓吧，谢谢你", "关掉所有持仓马上",
                 # #738 root r4240520024: 前置界面背景只是清仓发生的场景，不能覆盖清仓动作。
                 "在持仓面板里把所有仓位都关掉",
                 "把仓位都关掉啦", "全部仓位都关掉呗"):
        assert route_utterance(said, pending=False, product_names=names) == "delegate", said
        assert looks_like_ui(said, names) is False, said
    # 编辑卡开着时这句话也不能当补一句（是不是一张单由解析器判断，与编辑入口无关）。
    assert route_utterance("在持仓面板里把所有仓位都关掉", pending=False, product_names=names,
                           editor_open=True) == "delegate"
    for said in ("打开所有持仓清单", "打开所有持仓平仓记录",
                 "关掉所有持仓清单", "关掉所有持仓平仓记录"):
        assert not states_order(said, names), said
        for editing in (False, True):
            assert route_utterance(said, pending=False, product_names=names, editor_open=editing) == "delegate", said
    for said in ("平仓所有持仓然后看平仓记录", "把所有持仓平掉然后看平仓记录"):
        assert route_utterance(said, pending=False, product_names=names) == "delegate", said
    # #722: 持仓后面还跟着界面或提醒词的「关」字句仍是界面话，绝不当批量平仓（免口头确认时会立即平掉）。
    # 「关掉所有持仓页」的持仓页是 `_PANELS` 既有别名（surface 表的「页面?」），后置仍是界面话。
    for said in ("关掉持仓", "关闭持仓面板", "关掉所有持仓面板", "把持仓那个面板隐藏掉", "关掉所有持仓的面板",
                 "退出所有持仓视图", "关掉所有持仓列表", "关闭全部持仓图层", "持仓都关上面板",
                 "关掉所有持仓页", "关掉所有持仓页面"):
        assert not states_order(said, names), said  # never a batch-close card
        assert match_intent(said) is not None or looks_like_ui(said, names), said  # still interface work


# -- V1.3: quick queries Doubao answers itself, ASR hotwords, conversation context --------------------

def test_quick_queries_go_to_doubaos_tools_and_anything_with_an_action_or_opinion_does_not():
    from trader_runtime.voice_intents import match_quick_query
    quick = {"现在螺纹多少钱": ("quote", "螺纹"), "螺纹钢涨了没": ("quote", "螺纹钢"), "沪金现在什么价": ("quote", "沪金"),
             "那沪金呢": ("quote", "沪金"), "行情怎么样": ("quote", None), "刚才那个多少钱": ("quote", None),
             "手里有什么": ("positions", None), "我的持仓呢": ("positions", None), "今天亏了多少": ("positions", None),
             "手里有螺纹吗": ("positions", "螺纹"), "账户还剩多少钱": ("account", None), "权益多少": ("account", None),
             "保证金占了多少": ("account", None), "持仓": ("positions", None), "行情": ("quote", None)}
    for text, (kind, symbol) in quick.items():
        assert route_utterance(text, pending=False) == "quick", text
        match = match_quick_query(text)
        assert match is not None and (match.kind, match.symbol_query) == (kind, symbol), (text, match)
    for text in ["买一手螺纹", "螺纹怎么看", "为什么跌了", "帮我看看沪金的走势", "打开设置", "螺纹和沪金都多少钱", "撤单", "全平",
                 "止损多少", "沪金的新闻", "该不该加仓", "把持仓平掉", "螺纹的K线怎么样", "切到沪金", "沪金能不能买", "这波是不是要跌"]:
        assert route_utterance(text, pending=False) != "quick", text
    # With the catalog's product names a leftover that is not a contract name is not a quote question
    # (review of #230: "铁矿今天量大不大" left "铁矿大不大" and would have been read as a missing contract).
    names = ["铁矿石", "沪金", "螺纹钢", "沪深300股指"]
    for text in ["铁矿今天量大不大", "涨停价多少", "沪金成交量大不大", "那随便什么呢"]:
        assert match_quick_query(text, names) is None and route_utterance(text, pending=False, product_names=names) != "quick", text
    for text, symbol in [("螺纹多少钱", "螺纹"), ("那沪金呢", "沪金"), ("沪金涨了没", "沪金"), ("行情怎么样", None), ("螺纹钢2610多少钱", "螺纹钢2610"), ("手里有铁矿吗", "铁矿")]:
        match = match_quick_query(text, names)
        assert match is not None and match.symbol_query == symbol, (text, match)
    # A waiting card still takes "确认"; a tour still takes "下一步"; hangup and stop words keep their routes.
    assert route_utterance("确认", pending=True) == "confirm" and route_utterance("关闭语音", pending=False) == "hangup"


async def test_quick_tools_return_compact_chinese_fields_and_fail_closed_on_stale_data(tmp_path):
    app, _, service = await _voice_app(tmp_path, "quick-tools.sqlite3")
    provider = app.state.data_provider
    state = CallState(window_id=WINDOW)
    quote = await service.run_tool("get_quote", {"query": "沪金"}, state)
    assert quote["合约"] == "沪金2612" and quote["最新价"] == "824" and quote["涨跌"].endswith("%") and quote["买一卖一"] == "823 / 824"
    assert set(quote) <= {"合约", "最新价", "涨跌", "买一卖一", "状态", "更新于"} and len(quote) <= 6
    # No query: the contract the window shows.
    on_screen = await service.run_tool("get_quote", {}, state)
    assert on_screen["合约"] == "沪金2612"
    assert (await service.run_tool("get_quote", {"query": "不存在的品种"}, state))["error"].startswith("没找到")
    positions = await service.run_tool("get_positions", {}, state)
    assert "沪金2612 多 2 手" in positions["持仓"] and "沪金2612 空 2 手" in positions["持仓"] and positions["合计浮动盈亏"].endswith("元")
    assert (await service.run_tool("get_positions", {"query": "螺纹"}, state))["持仓"] == "没有螺纹的持仓"
    account = await service.run_tool("get_account", {}, state)
    assert account["权益"] == "1,026,580 元" and account["今日盈亏"] == "+3,420 元" and account["保证金占用"] == "18.6%" and len(account) <= 7
    # Stale quotes and accounts return only an error: nothing for the model to read but the refusal.
    from datetime import datetime, timedelta
    channel = app.state.shared_market.markets[("SHFE.au2612", 300)]
    changed = channel.changed
    provider.age_seconds = 30.0
    provider.quote_datetime = (datetime.fromisoformat(provider.quote_datetime) - timedelta(seconds=30)).isoformat()
    await asyncio.wait_for(changed.wait(), timeout=2)
    assert await service.run_tool("get_quote", {"query": "沪金"}, state) == {"error": "行情数据过时/不可用"}
    provider.age_seconds = 0.0
    original = provider.account_summary
    provider.account_summary = lambda turn_id: {**original(turn_id), "dataStatus": "stale", "ageSeconds": 40}  # type: ignore[method-assign]
    assert await service.run_tool("get_account", {}, state) == {"error": "账户数据过时/不可用"}
    assert await service.run_tool("get_positions", {}, state) == {"error": "账户数据过时/不可用"}
    await app.state.shared_market.close()
    app.state.store.close()


async def test_dialog_context_is_the_last_rounds_of_the_conversation_without_tables_and_within_limits(tmp_path):
    app, _, service = await _voice_app(tmp_path, "context.sqlite3")
    broker = app.state.broker
    session = broker.create_session("local", "native", "local-user")
    state = CallState(window_id=WINDOW, chat_session_id=session.session_id)
    assert service.dialog_context(state) == []
    for index in range(8):
        await broker.publish(session.session_id, kind="message", name="message.completed", payload={"turnId": f"t{index}-user", "role": "user", "text": f"问题{index}"})
        answer = f"**回答{index}**\n\n| 列 | 值 |\n|---|---|\n| a | 1 |\n\n细节段落{index}" + ("长" * 400 if index == 7 else "")
        await broker.publish(session.session_id, kind="message", name="message.completed", payload={"turnId": f"t{index}", "role": "assistant", "text": answer})
    # An unanswered question (the turn was cancelled) is not context.
    await broker.publish(session.session_id, kind="message", name="message.completed", payload={"turnId": "t9-user", "role": "user", "text": "没答的问题"})
    items = service.dialog_context(state)
    assert [item["role"] for item in items] == ["user", "assistant"] * 6
    assert items[0]["text"] == "问题2" and items[-2]["text"] == "问题7"
    assert "|" not in items[1]["text"] and "**" not in items[1]["text"] and items[1]["text"].startswith("回答2 细节段落2")
    assert len(items[-1]["text"]) == 200 and sum(len(item["text"]) for item in items) <= 2000
    # The hotword list: catalog product names (no month digits, deduplicated) plus the trading words.
    context = await service.asr_context()
    words = [item["word"] for item in context["hotwords"]]
    assert "沪金" in words and "螺纹钢" in words and "全平" in words and len(words) == len(set(words)) <= 200
    assert context["correct_words"]["全屏"] == "全平" and context["correct_words"]["告一首"] == "搞一手"
    app.state.store.close()


def _scripted_call(monkeypatch, on_send):
    """A fake Doubao (`on_send(payload, emit)` scripts the upstream) and a fake desktop client; returns
    (upstream_sent, client_out, inbound, run) for one _run_call."""
    import sys
    import types

    from trader_runtime.voice_realtime import CallIdentity, _run_call
    upstream_sent: list[dict] = []
    to_model: asyncio.Queue = asyncio.Queue()

    class FakeUpstream:
        async def send(self, raw: str) -> None:
            payload = json.loads(raw)
            upstream_sent.append(payload)
            await on_send(payload, to_model.put)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return json.dumps(await to_model.get())

    class Connect:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return FakeUpstream()
        async def __aexit__(self, *a): return False
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=Connect))
    client_out: list[dict] = []
    inbound: asyncio.Queue = asyncio.Queue()

    class FakeClient:
        async def send_text(self, text: str) -> None:
            client_out.append(json.loads(text))
        async def receive(self) -> dict:
            return await inbound.get()
        async def close(self) -> None: ...

    async def run(service, *, until, timeout=15):
        async def finish_later():
            for _ in range(int(timeout * 10)):
                await asyncio.sleep(0.1)
                if until():
                    break
            await inbound.put({"type": "websocket.disconnect"})
        asyncio.create_task(finish_later())
        await asyncio.wait_for(_run_call(service, FakeClient(), "key", CallIdentity(user_id="local-user", headers={})), timeout=timeout + 5)
    return upstream_sent, client_out, inbound, run


@pytest.mark.parametrize("phrase", ["打开所有持仓清单", "打开持仓清单切到委托页"])
async def test_pure_ui_voice_delegates_the_whole_request_once_and_keeps_read_screen(tmp_path, monkeypatch, phrase):
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "EARLY_FINAL_SECONDS", 0.02)
    app, _, service = await _voice_app(tmp_path, "shared-ui.sqlite3")
    chat = app.state.broker.create_session("local", "native", "local-user")
    calls = []

    async def call(operation, *, path=None, query=None, body=None):
        calls.append((operation, body))
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [{"windowId": WINDOW, "online": True, "revision": 0, "state": {"panel": None}}]}}
        if operation == "GET /api/app/ui/windows/{window_id}":
            return {"data": {"windowId": WINDOW, "online": True, "revision": 0, "state": {"panel": None}, "observation": {"text": "交易台"}}}
        if operation == CHAT_SEND:
            assert body["text"] == phrase and body["uiWindowId"] == WINDOW
            return {"data": {"runStatus": "completed", "reply": "已有 2 步界面操作确认生效。", "sessionId": chat.session_id}}
        if operation == "GET /api/account/review-picks":
            return {"data": {"items": []}}
        raise AssertionError(operation)
    service._call = call
    emitted = False

    async def on_send(payload, emit):
        nonlocal emitted
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "shared-ui"}})
        elif payload["type"] == "input_audio_buffer.append" and not emitted:
            emitted = True
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            for partial in ("打开持仓。", "打开持仓清单。"):
                await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": partial})
                await asyncio.sleep(0.12)  # longer than the configured early-final window
                assert not any(operation in {CHAT_SEND, "POST /api/app/ui/commands"} for operation, _ in calls)
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": phrase})
            await emit({"type": "response.function_call_arguments.done", "items": [
                {"name": "read_screen", "call_id": "screen", "arguments": "{}"},
                {"name": "get_positions", "call_id": "wrong-query", "arguments": "{}"}]})
    upstream, output, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": chat.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, timeout=1, until=lambda: any(e.get("type") == "tool_result" and e.get("name") == "delegate" for e in output))
    assert sum(operation == CHAT_SEND for operation, _ in calls) == 1
    assert not any(operation in {"POST /api/app/ui/commands", "GET /api/account/positions"} for operation, _ in calls)
    assert any(e.get("type") == "tool_result" and e.get("name") == "read_screen" for e in output)
    assert not any(e.get("type") == "tool_result" and e.get("name") == "get_positions" for e in output)
    assert any(e["event"] == "routed" and e["action"] == "delegate" for e in output[-1]["timeline"])
    assert not any(e["event"] == "asrEarlyFinal" for e in output[-1]["timeline"])
    app.state.store.close()


async def test_session_create_carries_the_memory_profile_after_the_fixed_instructions(tmp_path, monkeypatch):
    """私人记忆 3a: the voice front desk's instructions = the untouched INSTRUCTIONS plus the
    account's 画像 (≤150 chars, whole items) — nothing appended when there is no memory."""
    from trader_runtime import user_memory
    app, _, service = await _voice_app(tmp_path, "memory-profile.sqlite3")

    async def scripted_call(session_id):
        async def on_send(payload, emit):
            if payload["type"] == "session.create":
                await emit({"type": "session.created", "session": {"id": session_id}})
        upstream_sent, _, inbound, run = _scripted_call(monkeypatch, on_send)
        await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "sessionId": ""})})
        await run(service, until=lambda: any(p["type"] == "session.create" for p in upstream_sent))
        return next(p for p in upstream_sent if p["type"] == "session.create")["session"]["instructions"]

    assert await scripted_call("s1") == INSTRUCTIONS  # no memory yet: byte-identical
    user_memory.add(app.state.store, "local-user", layer="profile", text="只做螺纹和铁矿，仓位不重",
                    quote=None, products=None, via="settings")
    instructions = await scripted_call("s2")
    assert instructions.startswith(INSTRUCTIONS) and "关于用户：只做螺纹和铁矿，仓位不重" in instructions
    app.state.store.close()


async def test_a_quick_turn_is_doubaos_own_tool_call_read_out_and_recorded_in_the_conversation(tmp_path, monkeypatch):
    """"沪金多少钱" → routed quick (not muted) → Doubao calls get_quote → the Runtime answers through the same
    route as the UI → Doubao reads the numbers → the pair lands in the conversation as a voice turn."""
    app, _, service = await _voice_app(tmp_path, "quick-turn.sqlite3")
    session = app.state.broker.create_session("local", "native", "local-user")
    for index in range(2):
        await app.state.broker.publish(session.session_id, kind="message", name="message.completed", payload={"turnId": f"h{index}-user", "role": "user", "text": f"之前的问题{index}"})
        await app.state.broker.publish(session.session_id, kind="message", name="message.completed", payload={"turnId": f"h{index}", "role": "assistant", "text": f"之前的回答{index}"})

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "沪金多少钱？"})
            await asyncio.sleep(0.1)
            await emit({"type": "response.function_call_arguments.done", "items": [{"call_id": "c1", "name": "get_quote", "arguments": json.dumps({"query": "沪金"})}]})
        elif kind == "conversation.item.create" and payload["items"][0].get("role") == "tool":
            result = json.loads(payload["items"][0]["content"][0]["text"])
            await emit({"type": "response.output_text.delta", "delta": f"沪金现在{result['最新价']}，涨跌{result['涨跌']}。"})
            await emit({"type": "response.output_audio.started"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 3}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any(e.get("type") == "usage" for e in client_out))

    created = next(p for p in upstream_sent if p["type"] == "session.create")
    extra = created["extension"]["asr"]["extra"]
    assert {"word": "沪金"} in extra["context"]["hotwords"] and extra["context"]["correct_words"]["全屏"] == "全平" and extra["enable_asr_twopass"] is True
    assert created["extension"]["dialog"]["dialog_context"] == [
        {"role": "user", "text": "之前的问题0"}, {"role": "assistant", "text": "之前的回答0"},
        {"role": "user", "text": "之前的问题1"}, {"role": "assistant", "text": "之前的回答1"}]
    assert not any(p["type"] == "response.cancel" for p in upstream_sent)  # Doubao was never muted
    tool_result = json.loads(next(p for p in upstream_sent if p["type"] == "conversation.item.create")["items"][0]["content"][0]["text"])
    assert tool_result["合约"] == "沪金2612" and tool_result["最新价"] == "824"
    assert [e["delta"] for e in client_out if e.get("type") == "text"] == ["沪金现在824，涨跌+0.68%。"]
    timeline = client_out[-1]["timeline"]
    assert next(e for e in timeline if e["event"] == "routed")["by"] == "quick"
    assert [e["event"] for e in timeline if e["event"] in {"quickToolCall", "quickDone", "quickQueryFallback"}] == ["quickToolCall", "quickDone"]
    events = [e["payload"] for _, e in app.state.store.list_events(session.session_id) if e["name"] == "message.completed"]
    assert events[-2] == {"turnId": events[-2]["turnId"], "role": "user", "text": "沪金多少钱？", "origin": "voice"}
    assert events[-1]["role"] == "assistant" and events[-1]["text"] == "沪金现在824，涨跌+0.68%。" and events[-1]["origin"] == "voice"
    assert client_out[-1]["tools"][-1]["routedBy"] == "quick" if client_out[-1].get("tools") else True
    app.state.store.close()


async def test_a_quick_turn_falls_back_to_the_agent_when_doubao_talks_before_calling_a_tool(tmp_path, monkeypatch):
    """Fail closed: on a quick turn Doubao starts answering from memory (text, no tool call) → its reply is
    cancelled before any audio plays and the utterance goes to the agent like any delegate turn; the pair
    is then appended to Doubao's context."""
    app, _, service = await _voice_app(tmp_path, "quick-fallback.sqlite3")

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "沪金多少钱"})
            await asyncio.sleep(0.1)
            await emit({"type": "response.output_text.delta", "delta": "沪金大概八百"})
            await emit({"type": "response.output_audio.started"})
            await emit({"type": "response.output_audio.delta", "delta": "AAAA"})
        elif kind == "response.cancel":
            await emit({"type": "response.canceled"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: {"conversation.item.create", "speech_text_buffer.commit"} <= {p["type"] for p in upstream_sent}, timeout=20)

    kinds = [p["type"] for p in upstream_sent]
    assert "response.cancel" in kinds and kinds.index("response.cancel") < kinds.index("speech_text_buffer.commit")
    assert not any(e.get("type") == "audio" and e.get("data") == "AAAA" for e in client_out)  # the guessed answer never played
    assert "沪金大概八百" not in [e.get("delta") for e in client_out if e.get("type") == "text"]
    timeline = client_out[-1]["timeline"]
    assert next(e for e in timeline if e["event"] == "quickQueryFallback")["reason"] == "text"
    assert any(e["event"] == "toolDone" and e.get("tool") == "delegate" for e in timeline)
    context = next(p for p in upstream_sent if p["type"] == "conversation.item.create")
    assert [item["role"] for item in context["items"]] == ["user", "assistant"] and context["items"][0]["content"][0]["text"] == "沪金多少钱"
    assert context["items"][1]["content"][0]["text"] and "timestamp" not in context["items"][0]
    app.state.store.close()


async def test_a_quick_turn_that_doubbao_ends_silently_still_goes_to_the_agent(tmp_path, monkeypatch):
    """#523 静默快捷轮次: 工具没调、话也没说，Doubao 直接结束轮次 —— 这句照样转给 agent（与先说话的
    回退同路）；轮次已结束，无需再取消什么。"""
    app, _, service = await _voice_app(tmp_path, "quick-silent.sqlite3")

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "沪金多少钱"})
            await asyncio.sleep(0.1)
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})  # 静默结束
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: len([e for e in client_out if e.get("type") == "usage"]) >= 2, timeout=20)

    timeline = client_out[-1]["timeline"]
    assert next(e for e in timeline if e["event"] == "quickQueryFallback")["reason"] == "no_tool_call"
    assert any(e["event"] == "toolDone" and e.get("tool") == "delegate" for e in timeline)
    assert not any(p["type"] == "response.cancel" for p in upstream_sent)  # 轮次已结束：没有可取消的
    context = next(p for p in upstream_sent if p["type"] == "conversation.item.create")
    assert context["items"][0]["content"][0]["text"] == "沪金多少钱"  # 这句作为 delegate 轮进了对话
    app.state.store.close()


async def test_a_quick_turn_runs_the_tool_the_words_asked_for_even_when_doubao_picks_another(tmp_path, monkeypatch):
    """09-19 real run: "权益多少" → Doubao called get_quote(螺纹钢2610) and read a price instead of the account.
    The Runtime heard the words too: the routed kind decides the tool, the spoken contract decides the query."""
    app, _, service = await _voice_app(tmp_path, "quick-remap.sqlite3")

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "权益多少？"})
            await asyncio.sleep(0.1)
            await emit({"type": "response.function_call_arguments.done", "items": [{"call_id": "c1", "name": "get_quote", "arguments": json.dumps({"query": "螺纹钢2610"})}]})
        elif kind == "conversation.item.create" and payload["items"][0].get("role") == "tool":
            result = json.loads(payload["items"][0]["content"][0]["text"])
            await emit({"type": "response.output_text.delta", "delta": f"权益{result.get('权益')}。"})
            await emit({"type": "response.output_audio.started"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 3}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any(e.get("type") == "usage" for e in client_out))
    tool_result = json.loads(next(p for p in upstream_sent if p["type"] == "conversation.item.create")["items"][0]["content"][0]["text"])
    assert tool_result["权益"] == "1,026,580 元" and "最新价" not in tool_result
    assert [e["delta"] for e in client_out if e.get("type") == "text"] == ["权益1,026,580 元。"]
    remapped = next(e for e in client_out[-1]["timeline"] if e["event"] == "quickToolRemapped")
    assert (remapped["asked"], remapped["used"]) == ("get_quote", "get_account")
    app.state.store.close()


# -- V1.5 (09-21): server-side endpointing window, continuation merge --------------------------------

async def test_a_mid_sentence_pause_merges_the_two_halves_into_one_round(tmp_path, monkeypatch):
    """续句: "帮我看看" …(停一拍)… "沪金的行情走势" — Doubao ends each half as its own final. The first
    round (delegate, the agent turn still running, nothing spoken yet) is cancelled and the merged
    sentence is routed once. session.create also carries the custom endpointing window and the
    loudness normaliser."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "CONTINUATION_SECONDS", 5.0)
    app, _, service = await _voice_app(tmp_path, "continuation.sqlite3")
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    delegated: list[str] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            if len(delegated) == 1:
                await asyncio.sleep(30)  # the first agent turn is still running when the sentence continues
            return {"data": {"runStatus": "completed", "reply": "沪金持仓如下。", "spoken": "沪金持仓如下。", "sessionId": "s-c"}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "帮我看看。"})  # Doubao ends the half with 。
            await asyncio.sleep(0.05)
            # The real 09-21 call: Doubao starts its own filler for the first half; it is muted, the
            # user hears nothing — that must not count as "already answering" and block the merge.
            await emit({"type": "response.output_text.delta", "delta": "好"})
            await asyncio.sleep(0.2)  # the pause, well inside the continuation window
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "沪金的行情走势"})
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any(e.get("type") == "usage" for e in client_out), timeout=25)

    # The custom endpointing window stays off by default (the real 09-21 A/B: with the keys the next
    # utterance's recognition stalls — started on silence, no completed even after a forced commit);
    # the loudness normaliser still rides on session.create.
    created = next(p for p in upstream_sent if p["type"] == "session.create")
    extra = created["extension"]["asr"]["extra"]
    assert "enable_custom_vad" not in extra and "end_smooth_window_ms" not in extra
    assert created["extension"]["dialog"]["extra"] == {"enable_loudness_norm": True}
    timeline = client_out[-1]["timeline"]
    merged = next(e for e in timeline if e["event"] == "utteranceMerged")
    assert merged["parts"] == 2 and merged["chars"] == len("帮我看看沪金的行情走势")
    # The first delegate was cancelled mid-flight; the merged sentence is the round that is answered.
    assert delegated[-1] == "帮我看看沪金的行情走势" and len(delegated) == 2
    assert [e for e in timeline if e["event"] == "toolDone" and e.get("tool") == "delegate" and e.get("routedBy") == "runtime"]
    finals = [e["text"] for e in client_out if e.get("type") == "transcript" and e.get("final")]
    assert finals == ["帮我看看。", "帮我看看沪金的行情走势"]  # the client sees one merged final
    app.state.store.close()


async def test_a_merged_half_question_is_marked_superseded_and_its_run_cancelled(tmp_path, monkeypatch):
    """The merged-away half's agent run is cancelled server-side and the half user message is
    marked message.superseded, so the conversation hides the half-question instead of leaving a
    duplicate question with a 已停止 stub behind it."""
    app, _, service = await _voice_app(tmp_path, "continuation-superseded.sqlite3")
    session = app.state.broker.create_session("local", "native", "local-user")
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    delegated: list[str] = []
    cancelled: list[str] = []
    run_id_callbacks: list[Any] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            if len(delegated) == 1:
                await asyncio.sleep(30)  # the first agent turn is still running when the sentence continues
            return {"data": {"runStatus": "completed", "reply": "沪金持仓如下。", "spoken": "沪金持仓如下。", "sessionId": session.session_id}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]

    async def fake_http(method, path, body=None, *, account_id=""):
        cancelled.append(path)
        return 200, {}
    service._http = fake_http  # type: ignore[method-assign]

    def fake_subscribe(session_id, on_sentence, utterance, *, on_run_id=None):
        if on_run_id is not None:
            run_id_callbacks.append(on_run_id)
        return lambda: None
    service._subscribe_turn_text = fake_subscribe  # type: ignore[method-assign]

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "帮我看看。"})
            await asyncio.sleep(0.15)
            if run_id_callbacks:
                run_id_callbacks[0]("run-merge-1")  # the half-question's agent run names itself
            await asyncio.sleep(0.2)  # the pause, inside the continuation window
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "沪金的行情走势"})
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any(e.get("delta") == "沪金持仓如下。" for e in client_out if e.get("type") == "text"), timeout=25)

    # The half-question's run was cancelled server-side, and the superseded event names its
    # user turn with the merged sentence that replaced it.
    assert cancelled == ["/api/runs/run-merge-1/cancel"]
    superseded = [e["payload"] for _, e in app.state.store.list_events(session.session_id) if e["name"] == "message.superseded"]
    assert superseded == [{"turnId": "run-merge-1-user", "supersededBy": "帮我看看沪金的行情走势"}]
    assert delegated[-1] == "帮我看看沪金的行情走势" and len(delegated) == 2
    assert next(e for e in client_out[-1]["timeline"] if e["event"] == "utteranceMerged")["parts"] == 2
    app.state.store.close()


async def test_a_pause_past_the_window_answers_each_half_as_its_own_round(tmp_path, monkeypatch):
    """A pause longer than the continuation window (or a round that already started answering) does not
    merge: both halves are answered, each as its own question."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "CONTINUATION_SECONDS", 0)  # past the window whatever the gap: never merged
    app, _, service = await _voice_app(tmp_path, "continuation-late.sqlite3")
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    delegated: list[str] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            return {"data": {"runStatus": "completed", "reply": "好了。", "spoken": "好了。", "sessionId": "s-c"}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "帮我看看"})
            for _ in range(500):  # the first round has reached the agent (an event, not a timed gap; #932 B5)
                if delegated:
                    break
                await asyncio.sleep(0.01)
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "沪金的行情走势"})
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any(e.get("type") == "usage" for e in client_out), timeout=25)

    timeline = client_out[-1]["timeline"]
    assert not any(e["event"] == "utteranceMerged" for e in timeline)
    assert delegated == ["帮我看看", "沪金的行情走势"]  # two rounds, no merge
    finals = [e["text"] for e in client_out if e.get("type") == "transcript" and e.get("final")]
    assert finals == ["帮我看看", "沪金的行情走势"]
    app.state.store.close()


# -- 09-21: a failed confirm keeps the card; the server's card is the truth ----------------------

async def test_a_quote_stale_confirm_keeps_the_card_and_the_window_and_says_why(tmp_path):
    """The 09-21 call: 2 ms after the read-back the quote went quiet, the confirm failed quote_stale,
    the card was dropped and the next two 确认 landed as chat. The card now stays (the server's own
    terminal verdicts are the only things that clear it), the window restarts, and the reason is
    spoken in Chinese."""
    app, gateway, service = await _voice_app(tmp_path, "confirm-fail.sqlite3")
    state = CallState(window_id=WINDOW)
    await service.run_tool("delegate", {"utterance": "买入一手黄金"}, state)
    proposal_id = state.pending["proposals"][0]["proposalId"]
    rejected: list[str] = []

    async def fake_http(method, path, body=None, *, account_id=""):
        if path.endswith("/confirm"):
            return 409, {"detail": {"code": "quote_stale"}}
        if path.endswith("/reject"):
            rejected.append(path)
            return 200, {}
        return 200, {}
    service._http = fake_http  # type: ignore[method-assign]

    async def stale_market(symbol):
        return {"symbol": symbol, "quoteDatetime": "2026-09-21 22:45:00.000"}
    service._market_for = stale_market  # type: ignore[method-assign]

    failed = await service.run_tool("confirm", {}, state)
    assert failed["ok"] is False and failed["error"] == "confirm_failed" and failed["detail"] == ["quote_stale"]
    assert "没有新报价" in failed["say"] and "卡还在" in failed["say"] and "说确认" in failed["say"]
    assert state.pending and state.pending["proposals"]  # the card waits for the next 确认
    assert not service._pending_expired(state)  # the window restarted with the failure line
    assert state.pending.get("stale_quote", {}).get("symbol")
    assert rejected == []  # a repairable failure never rejects the card
    assert app.state.store.get(proposal_id)["status"] == "approval_required"
    assert service._has_pending(state) is True  # the router still hears a confirm

    # The call's own record can be lost entirely: the server's waiting card rebuilds it, and the
    # spoken 确认 then works (the 09-21 sequence, repaired).
    state.pending = None
    assert service._has_pending(state) is True
    rebuilt = service._server_pending(state)
    assert rebuilt["proposals"][0]["proposalId"] == proposal_id

    async def working_http(method, path, body=None, *, account_id=""):
        assert path == f"/api/proposals/{proposal_id}/confirm"
        return 200, {"status": "candidate_recorded", "message": "ok", "simulationOrder": {
            "clientOrderId": "AIT1", "displayStatus": "已受理", "request": {"symbol": "SHFE.au2612", "limitPrice": 823}}}
    service._http = working_http  # type: ignore[method-assign]
    service._market_for = stale_market  # type: ignore[method-assign]
    state.pending = None
    again = await service.run_tool("confirm", {}, state)
    assert again["ok"] is True and "已挂单" in again["say"] and "15 秒没成交我会提醒你" in again["say"]
    assert state.pending is None
    app.state.store.close()


async def test_a_spoken_cancel_acts_on_the_servers_waiting_card_when_the_calls_record_is_gone(tmp_path):
    """#717: 路由用 _has_pending（含服务端仍开着的卡）判定「取消」有卡可取消，取消就必须和确认读
    同一个来源。通话自己的记录没了（键盘下的卡、确认失败后被清）时，说「取消」不再听到
    “现在没有等确认的操作”而卡仍能被下一句「确认」下出去——现在它把服务端那张卡拒掉。"""
    app, _, service = await _voice_app(tmp_path, "cancel-server-card.sqlite3")
    state = CallState(window_id=WINDOW)
    await service.run_tool("delegate", {"utterance": "买入一手黄金"}, state)
    proposal_id = state.pending["proposals"][0]["proposalId"]
    session_id = app.state.store.get(proposal_id)["sessionId"]
    state.pending = None  # the call's own record is gone — the server's card is the truth
    assert service._has_pending(state) is True
    assert route_utterance("取消", pending=True) == "cancel"
    out = await service.run_tool("cancel", {}, state)
    assert out == {"ok": True, "dropped": True, "say": "好，已放弃。"}
    assert app.state.store.get(proposal_id)["status"] == "rejected"
    assert app.state.store.pending_proposals(session_id) == []
    assert service._has_pending(state) is False  # a later 确认 can no longer send that card
    app.state.store.close()


async def test_a_market_closed_confirm_rejects_the_card_and_promises_nothing(tmp_path):
    """#717: 休市时确认失败，旧文案承诺“卡还在，开盘后再说确认”，但确认窗口 15 秒后就把卡自动
    拒掉，开盘后价格也已变、旧卡不能下单。现在文案不承诺保留，这张卡当场放弃——说的和做的一致。"""
    app, _, service = await _voice_app(tmp_path, "confirm-market-closed.sqlite3")
    state = CallState(window_id=WINDOW)
    await service.run_tool("delegate", {"utterance": "买入一手黄金"}, state)
    proposal_id = state.pending["proposals"][0]["proposalId"]
    session_id = app.state.store.get(proposal_id)["sessionId"]
    rejected: list[str] = []
    real_http = service._http

    async def fake_http(method, path, body=None, *, account_id=""):
        if path.endswith("/confirm"):
            return 409, {"detail": {"code": "market_session_closed"}}
        if path.endswith("/reject"):
            rejected.append(path)
        return await real_http(method, path, body, account_id=account_id)
    service._http = fake_http  # type: ignore[method-assign]

    failed = await service.run_tool("confirm", {}, state)
    assert failed["ok"] is False and failed["error"] == "confirm_failed"
    assert failed["detail"] == ["market_session_closed"]
    assert "不在交易时段" in failed["say"] and "卡还在" not in failed["say"] and "开盘后再跟我说一遍" in failed["say"]
    assert state.pending is None  # no window reopened: the promise and the card agree
    assert rejected == [f"/api/proposals/{proposal_id}/reject"]
    assert app.state.store.get(proposal_id)["status"] == "rejected"
    assert app.state.store.pending_proposals(session_id) == []
    assert service._has_pending(state) is False
    app.state.store.close()


async def test_a_terminal_confirm_verdict_clears_the_card_and_rejects_it(tmp_path):
    """The codes the server itself calls final — approval_expired and friends — do clear the card:
    no countdown and no zombie card behind a verdict that cannot change."""
    app, _, service = await _voice_app(tmp_path, "confirm-terminal.sqlite3")
    state = CallState(window_id=WINDOW)
    await service.run_tool("delegate", {"utterance": "买入一手黄金"}, state)
    proposal_id = state.pending["proposals"][0]["proposalId"]
    rejected: list[str] = []
    real_http = service._http

    async def fake_http(method, path, body=None, *, account_id=""):
        if path.endswith("/confirm"):
            return 409, {"detail": {"code": "approval_expired"}}
        if path.endswith("/reject"):
            rejected.append(path)
        return await real_http(method, path, body, account_id=account_id)
    service._http = fake_http  # type: ignore[method-assign]

    failed = await service.run_tool("confirm", {}, state)
    assert failed["ok"] is False and failed["detail"] == ["approval_expired"]
    assert "已经失效" in failed["say"]
    assert state.pending is None and rejected == [f"/api/proposals/{proposal_id}/reject"]
    assert service._has_pending(state) is False
    app.state.store.close()


async def test_the_confirm_window_belongs_to_the_last_line_heard(tmp_path):
    """The window restarts whenever a line finishes playing or the user barges in while a card waits;
    a deadline that moved less than a second is not re-sent, so a streamed multi-sentence read-back
    does not flood the conversation with card updates."""
    import time as _time
    app, _, service = await _voice_app(tmp_path, "confirm-window.sqlite3")
    state = CallState(window_id=WINDOW)
    await service.run_tool("delegate", {"utterance": "买入一手黄金"}, state)
    proposal_id = state.pending["proposals"][0]["proposalId"]
    session_id = app.state.store.get(proposal_id)["sessionId"]

    def card_events() -> int:
        return len([e for _, e in app.state.store.list_events(session_id)
                    if e["name"] == "proposal.trade" and e["payload"]["proposalId"] == proposal_id])

    # Database/scheduler latency must not change the interval under test.
    deadline = _time.monotonic() + 15
    await service._open_confirm_window(state, deadline)
    first = card_events()
    assert app.state.store.get(proposal_id)["payload"].get("voiceConfirmBy")
    await service._open_confirm_window(state, deadline + 0.3)  # <1 s: not re-sent
    assert card_events() == first
    await service._open_confirm_window(state, deadline + 5)  # barge-in: a full new window
    assert card_events() == first + 1
    assert state.pending["deadline"] == deadline + 5
    app.state.store.close()


# -- 09-21: 打断 — "停" cancels the round that is still running, a business sentence supersedes it --

async def test_stop_cancels_the_running_round_and_its_agent_run(tmp_path, monkeypatch):
    """"停" while the agent turn is still running: the local round stops, the agent run is cancelled
    server-side (task.started's run id), and the voice says 已停止 — not just the read-out."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "CONTINUATION_SECONDS", 0.05)  # the interrupt is its own sentence, not a continuation
    app, _, service = await _voice_app(tmp_path, "stop-inflight.sqlite3")
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    session = app.state.broker.create_session("local", "native", "local-user")
    delegated: list[str] = []
    cancelled: list[str] = []
    run_id_callbacks: list[Any] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            await asyncio.sleep(30)  # the round is still running when the user says 停
            return {"data": {"runStatus": "completed", "reply": "好了。", "spoken": "好了。", "sessionId": session.session_id}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]

    async def fake_http(method, path, body=None, *, account_id=""):
        cancelled.append(path)
        return 200, {}
    service._http = fake_http  # type: ignore[method-assign]

    def fake_subscribe(session_id, on_sentence, utterance, *, on_run_id=None):
        if on_run_id is not None:
            run_id_callbacks.append(on_run_id)
        return lambda: None
    service._subscribe_turn_text = fake_subscribe  # type: ignore[method-assign]

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "帮我查一下螺纹的库存"})
            await asyncio.sleep(0.2)
            if run_id_callbacks:
                run_id_callbacks[0]("run-stop-1")  # the agent's task.started names its run
            await asyncio.sleep(0.2)  # past the patched continuation window
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "停"})
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})  # the business sentence, then 停
    await run(service, until=lambda: any(e.get("delta") == "已停止。" for e in client_out if e.get("type") == "text"), timeout=20)

    timeline = client_out[-1]["timeline"]
    assert cancelled == ["/api/runs/run-stop-1/cancel"]  # the agent run itself, not only the audio
    assert delegated == ["帮我查一下螺纹的库存"]  # the round was cancelled, never re-run
    stopped = next(e for e in timeline if e["event"] == "turnCancelled")
    assert stopped["by"] == "stop"
    assert [e["delta"] for e in client_out if e.get("type") == "text"] == ["已停止。"]
    app.state.store.close()


async def test_a_cancelled_readouts_late_frames_are_dropped_not_played(tmp_path, monkeypatch):
    """#523: 用户打断了正在播放的朗读（客户端 cancel）——合成器随后仍晚到的帧不再播出去；
    已播的部分不受影响。"""
    app, _, service = await _voice_app(tmp_path, "cancel-readout.sqlite3")
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    box: dict[str, Any] = {"inbound": None}

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            return {"data": {"runStatus": "completed", "reply": "螺纹持仓如下。", "spoken": "螺纹持仓如下。",
                             "sessionId": "s-c"}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "帮我查一下螺纹的库存"})
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started", "response_id": "resp-9", "tts_type": "chat_tts_text"})
            await emit({"type": "response.output_audio.delta", "response_id": "resp-9", "delta": "QUJD"})
            await asyncio.sleep(0.5)  # 正在播放
            box["inbound"].put_nowait({"type": "websocket.receive", "text": json.dumps({"type": "cancel"})})
            await asyncio.sleep(0.5)  # 打断已处理：response.cancel 上行，朗读标记作废
            await emit({"type": "response.output_audio.delta", "response_id": "resp-9", "delta": "TEFU"})  # 迟到的帧
            await emit({"type": "response.output_audio.done", "response_id": "resp-9"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    box["inbound"] = inbound
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any(e.get("type") == "usage" for e in client_out), timeout=20)

    played = [e.get("data") for e in client_out if e.get("type") == "audio"]
    assert played == ["QUJD"]  # 已播的保留，作废后晚到的帧不播
    kinds = [p["type"] for p in upstream_sent]
    assert kinds.index("response.cancel") > kinds.index("speech_text_buffer.commit")
    app.state.store.close()


async def test_a_business_sentence_supersedes_the_running_round_without_extra_words(tmp_path, monkeypatch):
    """A new business request while the previous round is still running takes its place: the old round
    is cancelled (turnSuperseded, not turnCancelled) and the new one is answered — no 已停止 spoken."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "CONTINUATION_SECONDS", 0)  # a new sentence, never a continuation
    app, _, service = await _voice_app(tmp_path, "supersede.sqlite3")
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    delegated: list[str] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            if len(delegated) == 1:
                await asyncio.sleep(30)  # still running when the user moves on
            return {"data": {"runStatus": "completed", "reply": "沪金持仓如下。", "spoken": "沪金持仓如下。", "sessionId": "s-c"}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "帮我查一下螺纹的库存"})
            for _ in range(500):  # the first round has reached the agent (an event, not a timed gap; #932 B5)
                if delegated:
                    break
                await asyncio.sleep(0.01)
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "再帮我看看沪金的库存"})
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any(e.get("delta") == "沪金持仓如下。" for e in client_out if e.get("type") == "text"), timeout=20)

    timeline = client_out[-1]["timeline"]
    assert delegated == ["帮我查一下螺纹的库存", "再帮我看看沪金的库存"]  # the first round was cancelled, not completed
    assert next(e for e in timeline if e["event"] == "turnSuperseded")["by"] == "new"
    assert not any(e["event"] == "turnCancelled" for e in timeline)
    assert not any(e.get("delta") == "已停止。" for e in client_out if e.get("type") == "text")
    app.state.store.close()


async def test_small_talk_cancels_nothing(tmp_path, monkeypatch):
    """A "谢谢" while a round is running is chat: the round keeps running and is answered."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "CONTINUATION_SECONDS", 0)  # the small talk is its own sentence
    app, _, service = await _voice_app(tmp_path, "smalltalk.sqlite3")
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    delegated: list[str] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            await asyncio.sleep(1.0)  # still running when the small talk arrives
            return {"data": {"runStatus": "completed", "reply": "沪金持仓如下。", "spoken": "沪金持仓如下。", "sessionId": "s-c"}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "帮我查一下螺纹的库存"})
            for _ in range(500):  # the first round has reached the agent (an event, not a timed gap; #932 B5)
                if delegated:
                    break
                await asyncio.sleep(0.01)
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "谢谢"})
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    # The chat turn supersedes the read-out (the answer stays in the conversation), so the wait
    # below only guards against a late cancellation: the round's own events land by ~1.1 s and
    # quiet after that proves it keeps running.
    await run(service, until=lambda: any(e.get("delta") == "沪金持仓如下。" for e in client_out if e.get("type") == "text"), timeout=1.5)

    timeline = client_out[-1]["timeline"]
    assert delegated == ["帮我查一下螺纹的库存"]  # the small talk cancelled nothing
    assert not any(e["event"] in {"turnCancelled", "turnSuperseded"} for e in timeline)
    assert not any(e.get("delta") == "已停止。" for e in client_out if e.get("type") == "text")
    app.state.store.close()


# -- 09-21: the quote comes back while the card waits --------------------------------------------

async def test_the_first_fresh_quote_after_a_stale_confirm_says_so_once(tmp_path, monkeypatch):
    """End to end through the real routes: the card is made on a live quote, the quote goes quiet,
    the spoken 确认 fails quote_stale — the card stays and the reason is spoken; the moment the quote
    is current again the voice says 有新报价了 (once), and the second 确认 places the order."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "QUOTE_WATCH_POLL_SECONDS", 0.2)
    app, gateway, service = await _voice_app(tmp_path, "quote-back.sqlite3")
    provider = app.state.data_provider
    from datetime import datetime, timedelta, timezone
    shanghai = timezone(timedelta(hours=8))
    quotes: list[str] = [datetime.now(shanghai).isoformat()]  # fresh at card time

    real_snapshot = provider.market_snapshot

    def scripted_snapshot(turn_id, symbol):
        market = dict(real_snapshot(turn_id, symbol))
        market["quoteDatetime"] = quotes[-1]
        return market
    provider.market_snapshot = scripted_snapshot  # type: ignore[method-assign]

    stale = datetime.now(shanghai) - timedelta(minutes=10)

    async def wait_until(predicate, timeout=10.0):
        for _ in range(int(timeout * 20)):
            if predicate():
                return True
            await asyncio.sleep(0.05)
        return False

    driven: list[bool] = []

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            if driven:
                return
            driven.append(True)
            # One buffer drives the whole three-sentence call; each half waits for the round before
            # it, the way a person listens (a final that races the read-back would be merged).
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "买入一手黄金"})
            await wait_until(lambda: any(p["type"] == "speech_text_buffer.commit" and str(p["text"]).endswith("确认吗？")
                                         for p in upstream_sent))
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "确认"})
            await wait_until(lambda: any("没有新报价" in str(e.get("delta")) for e in client_out if e.get("type") == "text"))
            await wait_until(lambda: any(e.get("delta") == "有新报价了，说确认就下。" for e in client_out if e.get("type") == "text"))
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "确认"})
        elif kind == "speech_text_buffer.commit":
            if payload["text"].endswith("确认吗？"):
                quotes.append(stale.isoformat())  # the quote goes quiet behind the read-back
            elif "没有新报价" in payload["text"] or "已挂单" in payload["text"]:
                quotes.append(datetime.now(shanghai).isoformat())
            await emit({"type": "response.output_audio.started"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any("已确认" in str(e.get("delta")) for e in client_out if e.get("type") == "text"), timeout=30)

    texts = [e["delta"] for e in client_out if e.get("type") == "text"]
    assert any("没有新报价" in text and "卡还在" in text for text in texts)  # the failure speaks Chinese
    assert texts.count("有新报价了，说确认就下。") == 1  # once
    assert any("已确认" in text and "已挂单" in text for text in texts)
    assert gateway.submit_count == 1  # the second 确认 sent the order
    timeline = client_out[-1]["timeline"]
    assert any(e["event"] == "quoteFreshAgain" for e in timeline)
    app.state.store.close()


def test_leaked_function_call_tag_is_replaced_by_the_word_it_wraps() -> None:
    # 09-24 long-session A/B: the twopass final twice wrapped a product name in the recognizer's entity tag.
    leak = '<FunctionCall>| "name : "getMusicEntity, "parameters : "content :"甲醇]| <FunctionCall>| 820 挂一手多。'
    assert clean_transcript(leak) == "甲醇820 挂一手多。"
    assert clean_transcript("螺纹钢买两手。") == "螺纹钢买两手。"



async def test_voice_sends_field_sentences_to_the_open_strategy_editor(tmp_path):
    """策略编辑卡 (2026-10-08): with the card open, "止损改成 20 跳" / "每次开两手" go to its 补一句 as one UI
    command (never the order / quick routes — voiceOrderConfirm=false would confirm an order card at once);
    an order naming its contract, interface words and a waiting card's 确认 keep their own routes."""
    from trader_runtime.strategy_editor import EDITOR_ASK_LINE

    assert route_utterance("止损改成20跳", pending=False, editor_open=True) == "editor_ask"
    assert route_utterance("每次开两手", pending=False, editor_open=True) == "editor_ask"
    assert route_utterance("加一个MACD金叉", pending=False, editor_open=True) == "editor_ask"
    assert route_utterance("每次开两手", pending=False, editor_open=False) != "editor_ask"
    assert route_utterance("螺纹买两手", pending=False, editor_open=True, product_names=["螺纹钢"]) == "delegate"
    assert route_utterance("打开设置", pending=False, editor_open=True) == "delegate"
    assert route_utterance("确认", pending=True, editor_open=True) == "confirm"
    for text in ("最长持仓20根后把所有仓位都关掉", "持仓满20根后把所有仓位都关掉"):
        assert route_utterance(text, pending=False, editor_open=True) == "editor_ask", text
    for text in ("在仓位面板里把所有持仓都关掉", "在仓位的面板里把所有持仓都关掉"):
        assert route_utterance(text, pending=False, editor_open=True) == "delegate", text
    app, gateway, service = await _voice_app(tmp_path, "voice-editor.sqlite3")
    state = CallState(window_id=WINDOW)
    sent: list[dict] = []
    original = service._call

    async def call(operation, **kwargs):
        if operation == "POST /api/app/ui/commands":
            sent.append(kwargs["body"]["changes"])
            return {"data": {"status": "applied", "revision": len(sent)}}
        return await original(operation, **kwargs)
    service._call = call  # type: ignore[method-assign]
    result = await service.run_tool("editor_ask", {"utterance": "止损改成 20 跳"}, state)
    assert sent == [{"strategyEditor": {"ask": "止损改成 20 跳"}}]
    assert result["ok"] and result["say"] == EDITOR_ASK_LINE
    hold_rule = "最长持仓20根后把所有仓位都关掉"
    result = await service.run_tool("editor_ask", {"utterance": hold_rule}, state)
    assert result["ok"] and result["say"] == EDITOR_ASK_LINE
    assert sent[-1] == {"strategyEditor": {"ask": hold_rule}}
    assert gateway.submit_count == 0
    app.state.store.close()


# -- 09-28: 提前判停 — a sentence the Runtime answers itself is routed from Doubao's partial ---------

@pytest.mark.parametrize("late_tool_first", [False, True])
async def test_a_spoken_confirm_is_routed_from_the_partial_and_doubaos_late_final_is_dropped(tmp_path, monkeypatch, late_tool_first):
    """Doubao shows "确认。" in its partials at once but sends the completed seconds later (after the
    Runtime spoke the read-back). The partial held unchanged EARLY_FINAL_SECONDS routes the confirm; the
    late completed of the same words and Doubao's own reply to it (a confirm tool call, words) are
    dropped — one order, one "已确认", no second voice. Both orders of the Runtime's "已确认" read-out and
    Doubao's late tool call: the read-out is never dropped, and its response.done (no id) ending the mute
    early does not let the late words through (09-29, flaky under load before)."""
    app, gateway, service = await _voice_app(tmp_path, "early-confirm.sqlite3")
    driven: list[bool] = []
    finished = asyncio.Event()

    async def wait_until(predicate, timeout=10.0):
        for _ in range(int(timeout * 20)):
            if predicate():
                return True
            await asyncio.sleep(0.05)
        return False

    def events() -> list[str]:
        return [e["event"] for e in service_timeline(service)]

    async def read_out(emit, response_id):
        await emit({"type": "response.output_audio.started", "tts_type": "chat_tts_text", "response_id": response_id})
        await emit({"type": "response.output_audio.delta", "delta": "QUJD", "response_id": response_id})
        await emit({"type": "response.output_audio.done", "response_id": response_id})
        await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})

    async def deferred_read_out(emit, response_id):
        assert await wait_until(lambda: "lateToolDropped" in events())
        await read_out(emit, response_id)

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            if driven:
                return
            driven.append(True)
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "买入一手黄金"})
            await wait_until(lambda: any(p["type"] == "speech_text_buffer.commit" and str(p["text"]).endswith("确认吗？")
                                         for p in upstream_sent))
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            for partial in ("确认", "确认。", "确认", "确认。", "确认"):
                await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": partial})
                await asyncio.sleep(0.05)
            assert await wait_until(lambda: gateway.submit_count == 1, timeout=3.0)  # before any completed
            if not late_tool_first:
                assert await wait_until(lambda: "audioDone" in events()[events().index("asrEarlyFinal"):])
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "确认。"})
            await emit({"type": "response.function_call_arguments.done", "items": [{"call_id": "late1", "name": "confirm", "arguments": "{}"}]})
            await asyncio.sleep(0.2)
            await emit({"type": "response.output_text.delta", "delta": "好的，已经帮你确认了。", "response_id": "r-late"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
            finished.set()
        elif kind == "speech_text_buffer.commit":
            response_id = f"r{len(upstream_sent)}"
            if late_tool_first and "已确认" in str(payload["text"]):
                asyncio.create_task(deferred_read_out(emit, response_id))
            else:
                await read_out(emit, response_id)
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    # The late reply's response.done ends the mute: once, or twice when the read-out's done came in between.
    await run(service, until=lambda: finished.is_set() and events().count("modelReplyCanceled") == 1 + late_tool_first,
              timeout=20)

    texts = [e["delta"] for e in client_out if e.get("type") == "text"]
    assert gateway.submit_count == 1
    assert sum("已确认" in text for text in texts) == 1 and "好的，已经帮你确认了。" not in texts
    assert sum(e.get("type") == "audio" for e in client_out) == 2  # the read-back and "已确认" both played
    timeline = client_out[-1]["timeline"]
    early = next(e for e in timeline if e["event"] == "asrEarlyFinal")
    assert early["route"] == "confirm" and early["heldSeconds"] >= 0.6
    order = [e["event"] for e in timeline]
    assert order.index("asrEarlyFinal") < order.index("asrFinalLate") < order.index("lateToolDropped")
    tool_answer = next(p for p in upstream_sent if p["type"] == "conversation.item.create" and p["items"][0].get("call_id") == "late1")
    assert json.loads(tool_answer["items"][0]["content"][0]["text"]) == {"ok": True, "say": ""}
    app.state.store.close()


def service_timeline(service):
    return [event for state in service.calls.values() for event in state.timeline]


async def test_a_quick_question_still_waits_for_doubaos_final_and_words_added_later_are_answered(tmp_path, monkeypatch):
    """Only sentences the Runtime answers itself go early: "沪金多少钱？" is Doubao's own quick turn and
    waits for its completed. A delegated sentence answered early whose completed carries more words
    routes just the added words (merged into the unanswered half)."""
    import trader_runtime.voice_realtime as module
    # The real windows are 3.0 s (continuation) and 0.6 s (early-final wait); the sleeps below stay
    # past/inside them by the same proportions at 1/6 scale.
    monkeypatch.setattr(module, "EARLY_FINAL_SECONDS", 0.1)
    monkeypatch.setattr(module, "CONTINUATION_SECONDS", 0.5)
    app, _, service = await _voice_app(tmp_path, "early-quick.sqlite3")
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    delegated: list[str] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            await asyncio.sleep(0.5)
            return {"data": {"runStatus": "completed", "reply": "库存如下。", "spoken": "库存如下。", "sessionId": "s-c"}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]
    driven: list[bool] = []

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            if driven:
                return
            driven.append(True)
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "沪金多少钱？"})
            await asyncio.sleep(0.2)
            assert not any(e["event"] == "asrEarlyFinal" for e in service_timeline(service))
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "沪金多少钱？"})
            await asyncio.sleep(0.65)  # past the patched continuation window: the next sentence is its own
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "帮我查一下。"})
            await asyncio.sleep(0.15)
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "帮我查一下螺纹的库存"})
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "帮我查一下螺纹的库存。"})
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started", "tts_type": "chat_tts_text"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any(e.get("delta") == "库存如下。" for e in client_out if e.get("type") == "text"), timeout=20)

    timeline = client_out[-1]["timeline"]
    assert [e["route"] for e in timeline if e["event"] == "asrEarlyFinal"] == ["delegate"]
    assert next(e for e in timeline if e["event"] == "asrFinalLate")["tail"] == "螺纹的库存。"
    assert delegated[-1] == "帮我查一下螺纹的库存。"  # the half and the added words, answered once
    assert any(e["event"] == "utteranceMerged" for e in timeline)
    app.state.store.close()


# -- 10-08: the second sentence of a call (desktop web, after the Runtime read an answer out) ----------

async def _readout(emit):
    await emit({"type": "response.output_audio.started", "tts_type": "chat_tts_text"})
    await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
    await emit({"type": "response.output_audio.done"})
    await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})


async def test_an_answered_sentence_doubao_kept_open_is_not_asked_again_however_long_the_answer_took(tmp_path, monkeypatch):
    """Reproduced against the live service (10-08): "帮我看看黄金的走势。" was answered from its partial;
    Doubao never completed that segment, and after the 10 s answer the next sentence came back as
    "帮我看看黄金的走势。现在螺纹钢多少钱？" — the expired window re-asked the answered question. The
    answered words are stripped from the partials and the completed however late; only the new
    sentence is routed and shown."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "EARLY_FINAL_SECONDS", 0.1)
    monkeypatch.setattr(module, "LATE_FINAL_WINDOW_SECONDS", 0.3)  # the answer outlasts the window
    app, _, service = await _voice_app(tmp_path, "second-sentence.sqlite3")
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    delegated: list[str] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            return {"data": {"runStatus": "completed", "reply": "金价偏强。", "spoken": "金价偏强。", "sessionId": "s-c"}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]
    driven: list[bool] = []
    done = asyncio.Event()

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            if driven:
                return
            driven.append(True)
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "帮我看看黄金的走势。"})
            for _ in range(60):  # answered from the partial; Doubao never completes the segment
                await asyncio.sleep(0.05)
                if delegated:
                    break
            await asyncio.sleep(0.5)  # the read-out takes longer than the window
            for partial in ("帮我看看黄金的走势。现在", "帮我看看黄金的走势。现在螺纹钢多少钱"):
                await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": partial})
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "帮我看看黄金的走势。现在螺纹钢多少钱？"})
            await asyncio.sleep(0.3)
            done.set()
        elif kind == "speech_text_buffer.commit":
            await _readout(emit)
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    _, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=done.is_set, timeout=10)

    timeline = client_out[-1]["timeline"]
    assert [e["text"] for e in timeline if e["event"] == "asrFinal"] == ["帮我看看黄金的走势。", "现在螺纹钢多少钱？"]
    assert delegated == ["帮我看看黄金的走势。"]  # asked once; the new sentence is Doubao's quick turn
    partials = [e["text"] for e in client_out if e.get("type") == "transcript" and not e["final"]]
    assert partials[-2:] == ["现在", "现在螺纹钢多少钱"]
    app.state.store.close()


async def test_a_sentence_doubao_never_finalises_after_the_forced_commit_is_answered_from_its_partial(tmp_path, monkeypatch):
    """Production calls 10-08: after a read-out the user's next sentence got partials and a forced
    commit, then no completed until the user hung up (1.4–6.5 s). STALL_FINAL_SECONDS after the forced
    commit the last partial is the sentence; Doubao would not answer what it never finalised, so even
    small talk goes to the agent. Doubao's completed of the same words, arriving afterwards, is dropped."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "STALL_FINAL_SECONDS", 0.2)
    app, _, service = await _voice_app(tmp_path, "stall-final.sqlite3")
    preferences = app.state.store.user_preferences("local-user")
    fast = preferences.model_copy(update={"voice": preferences.voice.model_copy(update={"endpointStallSeconds": 0.2})})
    monkeypatch.setattr(app.state.store, "user_preferences", lambda user_id: fast)
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    delegated: list[str] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            return {"data": {"runStatus": "completed", "reply": "在的。", "spoken": "在的。", "sessionId": "s-c"}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]
    driven: list[bool] = []
    done = asyncio.Event()

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            if driven:
                return
            driven.append(True)
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "你在吗"})
            for _ in range(60):
                await asyncio.sleep(0.05)
                if delegated:
                    break
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "你在吗？"})
            await asyncio.sleep(0.3)
            done.set()
        elif kind == "speech_text_buffer.commit":
            await _readout(emit)
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=done.is_set, timeout=10)

    timeline = client_out[-1]["timeline"]
    order = [e["event"] for e in timeline]
    assert order.index("asrForcedCommit") < order.index("asrStallFinal") < order.index("asrFinal")
    assert [e["text"] for e in timeline if e["event"] == "asrFinal"] == ["你在吗"]
    assert next(e for e in timeline if e["event"] == "routed")["action"] == "delegate"
    assert delegated == ["你在吗"] and "asrFinalLate" in order  # the late completed is not asked again
    assert any(e.get("type") == "text" and e.get("delta") == "在的。" for e in client_out)
    app.state.store.close()


def test_numeral_equivalence_does_not_blur_values_the_grammar_does_not_cover() -> None:
    """10-09 review: the numeral comparison must not invent a match the words do not support. A decimal
    keeps its point (0.1 must not compare as 01 = 一), digit-by-digit Chinese without a unit (“一二”)
    is refused instead of collapsing to its last digit by the amount parser; the same decimal in both
    sentences and one spoken digit still strip. 二十 = 20 (the production rewrite) is the tests below."""
    import trader_runtime.voice_realtime as module
    bare = module._bare
    assert module._answered_prefix(bare("止损改成 20 跳。打开画线工具栏。"), bare("止损改成二十跳")) == 7
    assert module._answered_prefix(bare("止损改成 0.1 倍 ATR。打开画线工具栏。"), bare("止损改成一倍ATR")) is None
    assert module._answered_prefix(bare("止损改成 1.5 跳。打开画线工具栏。"), bare("止损改成十五跳")) is None
    assert module._answered_prefix(bare("止损改成一二跳。打开画线工具栏。"), bare("止损改成2跳")) is None
    assert module._answered_prefix(bare("止损改成二零跳。打开画线工具栏。"), bare("止损改成20跳")) is None  # digit-by-digit: refused, not 20
    assert module._answered_prefix(bare("止损改成 1.5 跳。打开画线工具栏。"), bare("止损改成1.5跳")) == 8  # the same decimal
    assert module._answered_prefix(bare("止损改成 1.50 跳。打开画线工具栏。"), bare("止损改成1.5跳")) == 9  # the same decimal by value
    assert module._answered_prefix(bare("止损改成 1.05 倍 ATR。打开画线工具栏。"), bare("止损改成1.5倍ATR")) is None  # 1.05's 05 is not 1.5's 5: one token, not two runs
    assert module._answered_prefix(bare("止损改成 1.5 跳。打开画线工具栏。"), bare("止损改成一")) is None  # an answered integer is not the head of a new decimal
    assert module._answered_prefix(bare("止损改成一二十跳。打开画线工具栏。"), bare("止损改成20跳")) is None  # not a number the grammar reads
    assert module._answered_prefix(bare("改成1跳打开画线工具栏。"), bare("改成一跳")) == 4  # one spoken digit
    assert module._answered_prefix(bare("止损改成2万打开画线工具栏"), bare("止损改成二")) is None
    assert module._answered_prefix(bare("止损改成20万打开画线工具栏"), bare("止损改成二十")) is None
    assert module._answered_prefix(bare("止损改成一点五跳"), bare("止损改成1")) is None
    assert module._answered_prefix(bare("止损改成二十点五跳"), bare("止损改成20")) is None
    assert module._answered_prefix(bare("止损改成二十，点开画线工具栏"), bare("止损改成20")) == 6
    # 10-10 review: a separator before 点+numeral keeps 点 the click verb (the transcript goes in whole;
    # without the boundary 二十点一下 read as the number 二十点一 and the answered words would not strip)
    assert module._answered_prefix("止损改成二十，点一下画线工具栏", bare("止损改成20")) == 6
    assert module._answered_prefix("止损改成二十。点一下画线工具栏", bare("止损改成二十")) == 6
    assert module._answered_prefix("止损改成二十 点一下画线工具栏", bare("止损改成二十")) == 6  # a space is the same boundary
    assert module._answered_prefix("止损改成二十、点一下画线工具栏", bare("止损改成二十")) == 6
    assert module._after_bare("止损改成二十，点一下画线工具栏", 6) == "点一下画线工具栏"
    # 10-10 review: the answered record keeps the transcript's separators, so the same completed after
    # the answered “止损改成二十，点一下画线工具栏” is the same words — a dedup, not the numeral
    # correction 二十 → 二十点一 that would route the answered instruction a second time.
    assert module._prefix_match("止损改成二十，点一下画线工具栏。", "止损改成二十，点一下画线工具栏") == (14, False)
    # digit grouping between ASCII digits is not a word boundary: the number stays one token, however
    # the two passes spelled it — the separator around Chinese numerals still breaks words.
    assert module._answered_prefix("止损改成1,000跳。打开画线工具栏。", "止损改成1000跳") == 9
    assert module._answered_prefix("止损改成1000跳。打开画线工具栏。", "止损改成 1,000 跳") == 9
    assert module._answered_prefix("止损改成 10 000 跳。打开画线工具栏。", "止损改成10000跳") == 10
    assert module._answered_prefix("止损改成1，000跳。打开画线工具栏。", "止损改成1000跳") is None  # a fullwidth comma still breaks
    assert module._after_bare("止损改成 1.5 跳。打开画线工具栏。", 8) == "打开画线工具栏。"  # the dot counts with its digits


async def test_a_late_completed_that_rewrites_the_answered_numeral_still_strips_it(tmp_path, monkeypatch):
    """Production 10-09 (voice-continuation-repro): “止损改成二十跳” was answered from its partial by the
    stall final; the next sentence “打开画线工具栏” came with partials alternating between the new words and
    “止损改成 20 跳。打开画线工具栏。” — Doubao's second pass had rewritten 二十 as 20 — and the completed carried
    the executed sentence again, so the toolbar command was misrouted as another strategy edit. The
    answered prefix matches by numeral value; only the new sentence routes."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "STALL_FINAL_SECONDS", 0.2)
    assert module._answered_prefix("止损改成20跳打开画线工具栏", "止损改成二十跳") == 7  # 二十 = 20: the same words
    assert module._answered_prefix("止损改成30跳打开画线工具栏", "止损改成二十跳") is None  # a different value is a new sentence
    assert module._answered_prefix("打开画线工具栏", "止损改成二十跳") is None
    app, _, service = await _voice_app(tmp_path, "numeral-late-final.sqlite3")
    preferences = app.state.store.user_preferences("local-user")
    fast = preferences.model_copy(update={"voice": preferences.voice.model_copy(update={"endpointStallSeconds": 0.2})})
    monkeypatch.setattr(app.state.store, "user_preferences", lambda user_id: fast)
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    delegated: list[str] = []
    commands: list[dict] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            return {"data": {"runStatus": "completed", "reply": "改好了。", "spoken": "改好了。", "sessionId": "s-c"}}
        if operation == "POST /api/app/ui/commands":
            commands.append(body["changes"])
            window["revision"] += 1
            return {"data": {"status": "applied", "observedRevision": window["revision"], "state": {}}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]
    driven: list[bool] = []
    done = asyncio.Event()

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            if driven:
                return
            driven.append(True)
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "止损改成二十跳"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "止损改成二十跳。"})
            for _ in range(60):  # no completed: the stall final answers from the partial
                await asyncio.sleep(0.05)
                if delegated:
                    break
            await asyncio.sleep(0.5)  # the user pauses; the completion still lands inside the real 10 s window
            for partial in ("打开", "打开画线工具栏", "止损改成 20 跳，打开画线工具栏。", "打开画线工具栏",
                            "止损改成 20 跳。打开画线工具栏。"):
                await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": partial})
                await asyncio.sleep(0.05)
            await emit({"type": "conversation.item.input_audio_transcription.completed",
                        "text": "止损改成 20 跳。打开画线工具栏。"})
            await asyncio.sleep(0.5)
            done.set()
        elif kind == "speech_text_buffer.commit":
            await _readout(emit)
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    _, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=done.is_set, timeout=15)

    timeline = client_out[-1]["timeline"]
    assert [e["text"] for e in timeline if e["event"] == "asrFinal"] == ["止损改成二十跳。", "打开画线工具栏。"]
    assert delegated == ["止损改成二十跳。"]  # the executed sentence is not asked again
    assert next(e for e in timeline if e["event"] == "asrFinalLate")["tail"] == "打开画线工具栏。"
    assert commands and commands[-1] == {"disclosure": {"name": "chart-drawing-toolbar", "open": True}}
    partials = [e["text"] for e in client_out if e.get("type") == "transcript" and not e["final"]]
    assert all("止损" not in p for p in partials[2:])  # even the re-sent prefix is not shown again
    app.state.store.close()


async def test_a_late_completed_with_the_editor_open_strips_the_ask_and_only_the_toolbar_routes(tmp_path, monkeypatch):
    """The production shape of the 10-09 repro: the 策略编辑卡 was open, so the answered “止损改成二十跳”
    went to its 补一句 (editor_ask). The rewritten late completed must strip that ask exactly once
    more — “打开画线工具栏” routes as the toolbar command, never as a second editor ask."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "STALL_FINAL_SECONDS", 0.2)
    app, _, service = await _voice_app(tmp_path, "numeral-editor.sqlite3")
    preferences = app.state.store.user_preferences("local-user")
    fast = preferences.model_copy(update={"voice": preferences.voice.model_copy(update={"endpointStallSeconds": 0.2})})
    monkeypatch.setattr(app.state.store, "user_preferences", lambda user_id: fast)
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {"strategyEditor": {"open": True}}}
    delegated: list[str] = []
    commands: list[dict] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            return {"data": {"runStatus": "completed", "reply": "改好了。", "spoken": "改好了。", "sessionId": "s-c"}}
        if operation == "POST /api/app/ui/commands":
            commands.append(body["changes"])
            window["revision"] += 1
            return {"data": {"status": "applied", "observedRevision": window["revision"], "state": window["state"]}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]
    driven: list[bool] = []
    done = asyncio.Event()

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            if driven:
                return
            driven.append(True)
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "止损改成二十跳"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "止损改成二十跳。"})
            for _ in range(60):  # no completed: the stall final answers from the partial
                await asyncio.sleep(0.05)
                if commands:
                    break
            await asyncio.sleep(0.5)  # the pause stays inside the real 10 s late-final window
            for partial in ("打开", "打开画线工具栏", "止损改成 20 跳，打开画线工具栏。", "打开画线工具栏",
                            "止损改成 20 跳。打开画线工具栏。"):
                await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": partial})
                await asyncio.sleep(0.05)
            await emit({"type": "conversation.item.input_audio_transcription.completed",
                        "text": "止损改成 20 跳。打开画线工具栏。"})
            await asyncio.sleep(0.5)
            done.set()
        elif kind == "speech_text_buffer.commit":
            await _readout(emit)
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    _, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=done.is_set, timeout=15)

    timeline = client_out[-1]["timeline"]
    assert [e["text"] for e in timeline if e["event"] == "asrFinal"] == ["止损改成二十跳。", "打开画线工具栏。"]
    assert [change for change in commands if "strategyEditor" in change] == [{"strategyEditor": {"ask": "止损改成二十跳。"}}]
    assert commands[-1] == {"disclosure": {"name": "chart-drawing-toolbar", "open": True}}
    assert delegated == []  # nothing needed the chat agent
    partials = [e["text"] for e in client_out if e.get("type") == "transcript" and not e["final"]]
    assert all("止损" not in p for p in partials[2:])  # the re-sent prefix is not shown again
    app.state.store.close()


@pytest.mark.parametrize("answered, completed", [
    ("止损改成二十跳", "止损改成 30 跳。打开画线工具栏。"),
    ("止损改成一倍ATR", "止损改成 0.1 倍 ATR。打开画线工具栏。"),
    ("止损改成十五跳", "止损改成 1.5 跳。打开画线工具栏。"),
    ("止损改成2跳", "止损改成一二跳。打开画线工具栏。"),
    ("止损改成一", "止损改成 1.5 跳。打开画线工具栏。"),
    ("止损改成1", "止损改成 1.05 倍 ATR。打开画线工具栏。"),
    ("止损改成1", "止损改成一点五跳。打开画线工具栏。"),
    ("止损改成20", "止损改成二十点五跳。打开画线工具栏。"),
], ids=["thirty", "decimal-point", "one-point-five", "digit-by-digit", "answered-digit-new-decimal", "answered-integer-new-decimal", "chinese-decimal-one", "chinese-decimal-twenty"])
async def test_a_late_completed_with_a_different_number_is_not_stripped(tmp_path, monkeypatch, answered, completed):
    """The numeral match is by value, not shape: after `answered` was answered, a completed that changes
    the number is a new instruction — it must reach the agent whole, not be trimmed into the toolbar
    command. 0.1 is not 一 (the decimal point survives), 1.5 is not 十五, digit-by-digit “一二” is
    refused rather than read as 2, and an answered integer (一 or 1) is not the head of a new decimal
    (10-09 review: all of these used to strip the sentence away). The completed lands inside the real
    10 s late-final window (10-10 review: the old fixture shrank the window to 0.3 s and slept past it,
    so the similar-duplicate branch — the one that swallowed the correction in production — never ran)."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "STALL_FINAL_SECONDS", 0.2)
    app, _, service = await _voice_app(tmp_path, "numeral-change.sqlite3")
    preferences = app.state.store.user_preferences("local-user")
    fast = preferences.model_copy(update={"voice": preferences.voice.model_copy(update={"endpointStallSeconds": 0.2})})
    monkeypatch.setattr(app.state.store, "user_preferences", lambda user_id: fast)
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    delegated: list[str] = []
    commands: list[dict] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            return {"data": {"runStatus": "completed", "reply": "改好了。", "spoken": "改好了。", "sessionId": "s-c"}}
        if operation == "POST /api/app/ui/commands":
            commands.append(body["changes"])
            return {"data": {"status": "applied", "observedRevision": window["revision"] + 1, "state": {}}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]
    driven: list[bool] = []
    done = asyncio.Event()

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            if driven:
                return
            driven.append(True)
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": answered})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": answered + "。"})
            for _ in range(60):
                await asyncio.sleep(0.05)
                if delegated:
                    break
            await asyncio.sleep(0.5)  # well inside the real 10 s late-final window
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": completed})
            await asyncio.sleep(0.05)
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": completed})
            await asyncio.sleep(0.5)
            done.set()
        elif kind == "speech_text_buffer.commit":
            await _readout(emit)
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    _, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=done.is_set, timeout=15)

    timeline = client_out[-1]["timeline"]
    assert [e["text"] for e in timeline if e["event"] == "asrFinal"] == [answered + "。", completed]
    assert delegated == [answered + "。", completed]  # the whole completed reaches the agent
    assert not any("chart-drawing-toolbar" in str(change) for change in commands)  # not trimmed into the toolbar command
    app.state.store.close()


@pytest.mark.parametrize("answered, completed, tail", [
    ("止损改成二十", "止损改成二十，点一下画线工具栏。", "点一下画线工具栏。"),
    ("止损改成20", "止损改成 20，点一下画线工具栏。", "点一下画线工具栏。"),
    ("止损改成二十", "止损改成二十 点一下画线工具栏。", "点一下画线工具栏。"),  # a space is the same boundary
    ("止损改成二十", "止损改成二十。点开画线工具栏。", "点开画线工具栏。"),
    ("止损改成1000跳", "止损改成1,000跳。点一下画线工具栏。", "点一下画线工具栏。"),  # grouping comma inside the number
    ("止损改成1,000跳", "止损改成1000跳。点一下画线工具栏。", "点一下画线工具栏。"),  # the answered side had the grouping
], ids=["comma", "comma-arabic", "space", "period", "grouping-comma", "grouping-reverse"])
async def test_a_late_completed_with_a_click_after_the_number_strips_only_the_answered_words(
        tmp_path, monkeypatch, answered, completed, tail):
    """10-10 review (P2): the completed “止损改成二十，点一下画线工具栏” carries the answered instruction plus
    a new UI action. Without the separator's word boundary the numeral scanner read 二十点一下 as the
    number 二十点一, the answered words did not strip, and the late final re-routed the old instruction
    (or dropped the click). With the boundary kept, only the click sentence routes — the completed
    arrives inside the real 10 s late-final window, like production. A comma or space between two
    ASCII digits is digit grouping (“止损改成 1,000 跳”), not a boundary: the rewritten number still
    strips and only the appended sentence routes."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "STALL_FINAL_SECONDS", 0.2)
    app, _, service = await _voice_app(tmp_path, "numeral-click-boundary.sqlite3")
    preferences = app.state.store.user_preferences("local-user")
    fast = preferences.model_copy(update={"voice": preferences.voice.model_copy(update={"endpointStallSeconds": 0.2})})
    monkeypatch.setattr(app.state.store, "user_preferences", lambda user_id: fast)
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    delegated: list[str] = []
    commands: list[dict] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            return {"data": {"runStatus": "completed", "reply": "改好了。", "spoken": "改好了。", "sessionId": "s-c"}}
        if operation == "POST /api/app/ui/commands":
            commands.append(body["changes"])
            return {"data": {"status": "applied", "observedRevision": window["revision"] + 1, "state": {}}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]
    driven: list[bool] = []
    done = asyncio.Event()

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            if driven:
                return
            driven.append(True)
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": answered})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": answered + "。"})
            for _ in range(60):  # no completed: the stall final answers from the partial
                await asyncio.sleep(0.05)
                if delegated:
                    break
            await asyncio.sleep(0.5)  # inside the real 10 s late-final window
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": completed})
            await asyncio.sleep(0.05)
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": completed})
            await asyncio.sleep(0.5)
            done.set()
        elif kind == "speech_text_buffer.commit":
            await _readout(emit)
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    _, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=done.is_set, timeout=15)

    timeline = client_out[-1]["timeline"]
    assert [e["text"] for e in timeline if e["event"] == "asrFinal"] == [answered + "。", tail]
    assert delegated == [answered + "。", tail]  # the answered instruction is not asked again; the click is new
    assert next(e for e in timeline if e["event"] == "asrFinalLate")["tail"] == tail
    app.state.store.close()


@pytest.mark.parametrize("completed", [
    "止损改成二十，点一下画线工具栏。",
    "止损改成 20，点一下画线工具栏。",
], ids=["identical", "numeral-rewritten"])
async def test_a_completed_repeating_the_answered_click_sentence_is_dropped_whole(
        tmp_path, monkeypatch, completed):
    """10-10 review (P2): the stall final routed the whole “止损改成二十，点一下画线工具栏。” and Doubao's
    completed repeated it — verbatim, or with the second pass rewriting 二十 as 20. The answered record
    keeps the transcript's separators, so the repeat matches by numeral value and dedupes inside the
    real 10 s late-final window; the bare-only record lost the comma, read 二十点一 as one number,
    reported a correction and routed the answered instruction (click included) a second time."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "STALL_FINAL_SECONDS", 0.2)
    app, _, service = await _voice_app(tmp_path, "numeral-identical-repeat.sqlite3")
    preferences = app.state.store.user_preferences("local-user")
    fast = preferences.model_copy(update={"voice": preferences.voice.model_copy(update={"endpointStallSeconds": 0.2})})
    monkeypatch.setattr(app.state.store, "user_preferences", lambda user_id: fast)
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    delegated: list[str] = []

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            return {"data": {"runStatus": "completed", "reply": "改好了。", "spoken": "改好了。", "sessionId": "s-c"}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]
    driven: list[bool] = []
    done = asyncio.Event()

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            if driven:
                return
            driven.append(True)
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "止损改成二十，点一下画线工具栏"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "止损改成二十，点一下画线工具栏。"})
            for _ in range(60):  # no completed: the stall final answers from the partial
                await asyncio.sleep(0.05)
                if delegated:
                    break
            await asyncio.sleep(0.5)  # inside the real 10 s late-final window
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": completed})
            await asyncio.sleep(0.05)
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": completed})
            await asyncio.sleep(0.5)
            done.set()
        elif kind == "speech_text_buffer.commit":
            await _readout(emit)
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    _, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=done.is_set, timeout=15)

    timeline = client_out[-1]["timeline"]
    assert [e["text"] for e in timeline if e["event"] == "asrFinal"] == ["止损改成二十，点一下画线工具栏。"]
    assert delegated == ["止损改成二十，点一下画线工具栏。"]  # the whole sentence executed exactly once
    late = [e for e in timeline if e["event"] == "asrFinalLate"]
    assert late and "tail" not in late[-1]  # the repeat deduped, not trimmed and not re-routed
    app.state.store.close()


@pytest.mark.parametrize("continuation_window", [3.0, 0.1])
async def test_a_corrected_completed_replaces_the_still_waiting_early_round_instead_of_merging(tmp_path, monkeypatch, continuation_window):
    """10-10 review (P1): the stall final routed “止损改成1” and its delegate was still waiting (no
    answer, no audio) when the corrected completed “止损改成一点五跳” landed inside the real 10 s
    late-final window. The correction replaces that round — cancelled like a merge, never glued onto
    the words it corrects — and the agent receives the corrected sentence whole (the earlier tests
    let CHAT_SEND answer aloud first, which masked this merge path)."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "STALL_FINAL_SECONDS", 0.2)
    monkeypatch.setattr(module, "CONTINUATION_SECONDS", continuation_window)
    app, _, service = await _voice_app(tmp_path, "numeral-correction-replace.sqlite3")
    session = app.state.broker.create_session("local", "native", "local-user")
    cancelled: list[str] = []
    callbacks: list[Any] = []

    async def fake_http(method, path, body=None, *, account_id=""):
        cancelled.append(path)
        return 200, {}

    def fake_subscribe(session_id, on_sentence, utterance, *, on_run_id=None):
        if on_run_id is not None:
            callbacks.append(on_run_id)
        return lambda: None

    service._http = fake_http  # type: ignore[method-assign]
    service._subscribe_turn_text = fake_subscribe  # type: ignore[method-assign]
    preferences = app.state.store.user_preferences("local-user")
    fast = preferences.model_copy(update={"voice": preferences.voice.model_copy(update={"endpointStallSeconds": 0.2})})
    monkeypatch.setattr(app.state.store, "user_preferences", lambda user_id: fast)
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {}}
    delegated: list[str] = []
    release = asyncio.Event()

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        if operation == CHAT_SEND:
            delegated.append(body["text"])
            await release.wait()  # the round is still waiting when the correction lands
            return {"data": {"runStatus": "completed", "reply": "改好了。", "spoken": "改好了。", "sessionId": "s-c"}}
        return {"data": {}}
    service._call = fake_call  # type: ignore[method-assign]
    driven: list[bool] = []
    done = asyncio.Event()

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            if driven:
                return
            driven.append(True)
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "止损改成1"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "止损改成1。"})
            for _ in range(60):  # no completed: the stall final answers from the partial
                await asyncio.sleep(0.05)
                if delegated:
                    break
            callbacks[0]("run-correction-1")
            await asyncio.sleep(0.5)  # inside the real 10 s late-final window, before or after continuation
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "止损改成一点五跳"})
            await asyncio.sleep(0.05)
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "止损改成一点五跳。"})
            await asyncio.sleep(0.3)
            release.set()  # the corrected round's answer may come back now
            await asyncio.sleep(0.5)
            done.set()
        elif kind == "speech_text_buffer.commit":
            await _readout(emit)
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    _, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=done.is_set, timeout=15)

    timeline = client_out[-1]["timeline"]
    events = [e["event"] for e in timeline]
    assert [e["text"] for e in timeline if e["event"] == "asrFinal"] == ["止损改成1。", "止损改成一点五跳。"]
    assert delegated == ["止损改成1。", "止损改成一点五跳。"]  # the correction whole, never the merged 拼串
    assert "utteranceCorrected" in events and "utteranceMerged" not in events
    assert cancelled == ["/api/runs/run-correction-1/cancel"]
    superseded = [e["payload"] for _, e in app.state.store.list_events(session.session_id) if e["name"] == "message.superseded"]
    assert superseded == [{"turnId": "run-correction-1-user", "supersededBy": "止损改成一点五跳。"}]
    app.state.store.close()


async def test_a_wordless_start_does_not_cut_the_answer_still_playing_and_the_first_words_do(tmp_path, monkeypatch):
    """10-08: Doubao sends a wordless `transcription.started` when a reply starts or is cancelled; as a
    barge-in it dropped the answer the client was still playing. Only recognised words interrupt
    (as push-to-talk counts speech) — a real "等一下" over the answer still does at once."""
    app, _, service = await _voice_app(tmp_path, "barge-in.sqlite3")
    driven: list[bool] = []
    done = asyncio.Event()
    marks: dict[str, int] = {}

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            if driven:
                return
            driven.append(True)
            # An answer of ~2 s audio delivered at once: the client is still playing it afterwards.
            await emit({"type": "response.output_audio.started", "tts_type": "default", "response_id": "r1"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD" * 64000, "response_id": "r1"})
            await emit({"type": "response.output_audio.done", "response_id": "r1"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
            await emit({"type": "conversation.item.input_audio_transcription.started"})  # the ghost
            await asyncio.sleep(0.2)
            marks["ghost"] = len(client_out)
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "等一下"})
            await asyncio.sleep(0.2)
            done.set()
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    _, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=done.is_set, timeout=10)

    before, after = client_out[:marks["ghost"]], client_out[marks["ghost"]:]
    assert {"type": "interrupt"} not in before
    assert {"type": "interrupt"} in after
    assert after.index({"type": "interrupt"}) < next(i for i, e in enumerate(after) if e.get("type") == "transcript")
    app.state.store.close()


async def test_a_redial_is_not_refused_while_the_old_call_waits_for_doubaos_close(tmp_path, monkeypatch):
    """10-08 production: hang up, call again → 403 for ~5 s ("语音通道连接失败"), because the account's one
    call slot was held until Doubao acknowledged session.close. The slot and the voice seconds end when
    the person leaves; the old session's close finishes in the background (no microphone behind it),
    and the seconds are booked once."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "SESSION_CLOSE_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(VoiceRealtimeService, "local_mode", property(lambda self: False))
    app, _, service = await _voice_app(tmp_path, "redial.sqlite3")
    created = asyncio.Event()

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
            created.set()
        # session.close is never acknowledged: the old call waits the full SESSION_CLOSE_WAIT_SECONDS.
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    slots_while_closing: list[int] = []

    async def redial_check():
        for _ in range(200):
            await asyncio.sleep(0.01)
            if any(p["type"] == "session.close" for p in upstream_sent):
                slots_while_closing.append(service.user_calls("local-user"))
                return
    checker = asyncio.create_task(redial_check())
    today = datetime.now(UTC).date().isoformat()
    before = app.state.store.voice_seconds("local-user", today)
    await run(service, until=created.is_set, timeout=10)
    await checker
    assert slots_while_closing == [0]  # a new call by this account would be accepted right now
    assert client_out[-1]["type"] == "closed"
    assert app.state.store.voice_seconds("local-user", today) - before == int(client_out[-1]["durationSeconds"])
    app.state.store.close()


# -- 引导复盘 (2026-09-29) -------------------------------------------------------------------------

REVIEW_PICKS = {"scope": "day", "unnoted": 3, "total": 4, "picks": [
    {"id": "t1:t2", "question": "螺纹这笔做多，亏了 2,300 元，拿了 40 分钟。当时是怎么想的？"},
    {"id": "t3:t4", "question": "豆粕这笔做空，赚了 800 元。那天调研看偏多，你反着做，当时是怎么考虑的？"},
    {"id": "t5:t6", "question": "沪金这笔做多，亏了 300 元。当时为什么没走？"}]}


def _fake_review_calls(monkeypatch, service, picks=REVIEW_PICKS, *, save_ok=True):
    calls: list[tuple[str, dict | None, dict | None]] = []
    original = service._call

    async def fake(operation, *, path=None, query=None, body=None, replay=None):
        if operation == "GET /api/account/review-picks":
            calls.append((operation, query, None))
            return {"ok": True, "data": picks}
        if operation == "PUT /api/account/trade-notes/{round_id}":
            calls.append((operation, path, body))
            return {"ok": save_ok, "data": {} if save_ok else {"detail": "没有这笔平仓记录"}}
        return await original(operation, path=path, query=query, body=body, replay=replay)
    monkeypatch.setattr(service, "_call", fake)
    return calls


async def test_the_voice_review_asks_records_follows_up_once_skips_and_ends(tmp_path, monkeypatch):
    """The Runtime asks the picks one by one: an answer is recorded as the trade's 补充 (via voice); a
    very short one gets one follow-up and the two are kept together; 跳过 moves on without a note; the
    last one ends the review. Quotes, interface commands and orders keep their own paths meanwhile."""
    app, _, service = await _voice_app(tmp_path, "review-steps.sqlite3")
    calls = _fake_review_calls(monkeypatch, service)
    service.product_names = ["螺纹钢", "沪金"]  # the catalog's names, which the call reads before routing
    state = CallState(window_id=WINDOW)
    assert service.review_route(state, "复盘一下这周", "delegate") == "review_start"
    opening = await service.review_step(state, "review_start", "复盘一下这周")
    assert calls[0] == ("GET /api/account/review-picks", {"scope": "week"}, None)
    assert opening == "好，这周有 3 笔值得聊聊，一笔一笔来。螺纹这笔做多，亏了 2,300 元，拿了 40 分钟。当时是怎么想的？"
    # Inside the review: free words answer, 跳过 / 不聊了 steer, a quote or an order keeps its path.
    assert service.review_route(state, "追高了。", "chat") == "review_answer"
    assert service.review_route(state, "跳过", "chat") == "review_skip"
    assert service.review_route(state, "先到这里吧", "delegate") == "review_stop"
    assert service.review_route(state, "沪金多少钱", "quick") == "quick"
    assert service.review_route(state, "开多一手螺纹", "delegate") == "delegate"
    assert service.review_route(state, "开多一手螺纹，止损3100", "delegate") == "delegate"  # an order, not the answer
    assert service.review_route(state, "撤掉刚才的委托", "delegate") == "delegate"  # a cancellation keeps its route
    assert service.review_route(state, "确认", "confirm") == "confirm"
    assert service.review_route(state, "为什么螺纹今天跌了？", "delegate") == "delegate"   # a question back, not an answer
    assert service.review_route(state, "现在还能追吗", "delegate") == "delegate"
    follow = await service.review_step(state, "review_answer", "追高了。")
    assert follow.startswith("能多说一句吗") and calls[-1] == (
        "PUT /api/account/trade-notes/{round_id}", {"round_id": "t1:t2"}, {"text": "追高了", "via": "voice"})
    second = await service.review_step(state, "review_answer", "看到放量就进了，没等回踩")
    assert calls[-1][2] == {"text": "追高了；看到放量就进了，没等回踩", "via": "voice"}
    assert second == "记下了。下一笔，豆粕这笔做空，赚了 800 元。那天调研看偏多，你反着做，当时是怎么考虑的？"
    # "嗯" is no answer: one nudge, then it skips (nothing is saved).
    assert (await service.review_step(state, "review_answer", "嗯。")).startswith("可以说说当时怎么想的")
    saves = len(calls)
    skipped = await service.review_step(state, "review_answer", "嗯")
    assert len(calls) == saves and skipped.startswith("好，跳过。下一笔，沪金")
    await service.review_step(state, "review_stop", "不聊了")
    await service.review_step(state, "review_start", "复盘一下这周")
    state.review["index"] = 1
    skipped = await service.review_step(state, "review_skip", "跳过")
    assert skipped.startswith("好，跳过。下一笔，沪金") and calls[-1][0] == "GET /api/account/review-picks"   # skipping saves nothing
    last = await service.review_step(state, "review_answer", "舍不得止损，想等它回来")
    assert last == "记下了。就这些了，补充都记在复盘页对应的交易里。" and state.review is None
    assert service.review_route(state, "追高了", "chat") == "chat"   # the review is over
    # 不聊了 ends it at once; a failed save says so and moves on.
    await service.review_step(state, "review_start", "复盘一下")
    assert (await service.review_step(state, "review_stop", "不聊了")).startswith("好，先聊到这儿") and state.review is None
    _fake_review_calls(monkeypatch, service, save_ok=False)
    await service.review_step(state, "review_start", "复盘一下")
    assert (await service.review_step(state, "review_answer", "当时看错了支撑位")).startswith("这句没记上")
    app.state.store.close()


async def test_the_voice_review_says_why_there_is_nothing_to_ask_and_offers_itself_once_after_the_close(tmp_path, monkeypatch):
    app, _, service = await _voice_app(tmp_path, "review-offer.sqlite3")
    state = CallState(window_id=WINDOW)
    for picks, line in (({"picks": [], "total": 0, "unnoted": 0}, "今天还没有平仓的交易，没什么可复盘的。"),
                        ({"picks": [], "total": 2, "unnoted": 0}, "今天的交易都补过想法了，没有要再聊的。"),
                        ({"picks": [], "total": 2, "unnoted": 2}, "今天的交易没有特别需要聊的。想聊哪一笔，可以在复盘页选中它补一句。")):
        _fake_review_calls(monkeypatch, service, picks)
        assert await service.review_step(state, "review_start", "复盘一下") == line and state.review is None
    _fake_review_calls(monkeypatch, service)
    at = lambda hhmm: datetime.fromisoformat(f"2026-09-29T{hhmm}:00+08:00")  # noqa: E731
    assert await service.review_offer_line(state, now=at("14:30")) is None          # the day session is on
    assert await service.review_offer_line(state, now=at("21:10")) is None          # the night session is on
    assert await service.review_offer_line(state, now=at("15:20")) == "今天有 3 笔交易值得聊聊，要复盘一下吗？"
    assert state.review_offer is True
    assert service.review_route(state, "好啊", "chat") == "review_start"
    assert service.review_route(state, "下次吧", "chat") == "review_decline"
    assert service.review_route(state, "螺纹多少钱", "quick") == "quick"
    assert await service.review_offer_line(CallState(window_id=WINDOW), now=at("16:00")) is None   # once a day
    app.state.store.close()


async def test_the_review_button_opens_the_call_with_the_first_question_and_the_answer_is_recorded(tmp_path, monkeypatch):
    """复盘页“语音复盘”: the call's hello carries intent=review; the Runtime speaks the opening line itself,
    the user's answer is recorded once they stop talking, and the next question follows."""
    app, _, service = await _voice_app(tmp_path, "review-call.sqlite3")
    calls = _fake_review_calls(monkeypatch, service, {**REVIEW_PICKS, "picks": REVIEW_PICKS["picks"][:2]})
    driven: list[bool] = []

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            if driven:
                return
            driven.append(True)
            for _ in range(100):
                if any(p["type"] == "speech_text_buffer.commit" and "螺纹" in str(p["text"]) for p in upstream_sent):
                    break
                await asyncio.sleep(0.05)
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "看到放量就追进去了，没等回踩。"})
            # Doubao answers the sentence itself; the Runtime mutes it and speaks the next question.
            await emit({"type": "response.output_text.delta", "delta": "听起来你很冲动呢", "response_id": "r-doubao"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started", "tts_type": "chat_tts_text", "response_id": f"r{len(upstream_sent)}"})
            await emit({"type": "response.output_audio.done", "response_id": f"r{len(upstream_sent)}"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "intent": "review"})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any("豆粕" in str(e.get("delta")) for e in client_out if e.get("type") == "text"), timeout=20)

    spoken = [str(p["text"]) for p in upstream_sent if p["type"] == "speech_text_buffer.commit"]
    assert spoken[0].startswith("好，今天有 2 笔值得聊聊") and spoken[1].startswith("记下了。下一笔，豆粕")
    assert ("PUT /api/account/trade-notes/{round_id}", {"round_id": "t1:t2"},
            {"text": "看到放量就追进去了，没等回踩", "via": "voice"}) in calls
    texts = [e["delta"] for e in client_out if e.get("type") == "text"]
    assert "听起来你很冲动呢" not in texts
    events = [e["event"] for e in client_out[-1]["timeline"]]
    assert "reviewOpened" in events and "reviewStep" in events
    app.state.store.close()


async def test_a_foreign_or_expired_chat_session_is_never_adopted_by_the_call(tmp_path):
    """#523 R1a：客户端 hello 只带会话 id——别人的会话、或自那之后过期/删除的，都不被认领：
    不读它的对话、不把它记成本通话的会话、更不往里写；通话回落到用户自己的最近会话。"""
    app, _, service = await _voice_app(tmp_path, "hello-own-session.sqlite3")
    broker = app.state.broker
    foreign = broker.create_session("local", "native", "someone-else").session_id
    own = broker.create_session("local", "native", "local-user").session_id
    for session_id, prefix, question, answer in ((foreign, "fx", "别人的问题", "别人的回答"),
                                                 (own, "own1", "自己的问题", "自己的回答")):
        await broker.publish(session_id, kind="message", name="message.completed",
                             payload={"turnId": prefix + "-user", "role": "user", "text": question})
        await broker.publish(session_id, kind="message", name="message.completed",
                             payload={"turnId": prefix, "role": "assistant", "text": answer})

    # hello 带别人的会话：不认领——上下文读的是自己的会话，foreign 绝不会成为本通话的会话。
    state = CallState(window_id=WINDOW, chat_session_id=foreign)
    assert [item["text"] for item in service.dialog_context(state)] == ["自己的问题", "自己的回答"]
    assert state.chat_session_id == own and broker.get_session(foreign, "local-user") is None

    # 客户端手里的 id 过期了（离线期间被回收）：不写它，回落到自己的最近会话。
    newer = broker.create_session("local", "native", "local-user").session_id
    await broker.publish(newer, kind="message", name="message.completed",
                         payload={"turnId": "new1-user", "role": "user", "text": "最近的问题"})
    await broker.publish(newer, kind="message", name="message.completed",
                         payload={"turnId": "new1", "role": "assistant", "text": "最近的回答"})
    with app.state.store.lock, app.state.store.connection:
        app.state.store.connection.execute(
            "UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (own,))
    state = CallState(window_id=WINDOW, chat_session_id=own)
    context = service.dialog_context(state)
    assert state.chat_session_id == newer and [item["text"] for item in context] == ["最近的问题", "最近的回答"]
    app.state.store.close()


async def test_quick_account_and_positions_read_the_chat_sessions_account(tmp_path, monkeypatch):
    """#523 R1b：权益/持仓快问读通话所续聊天会话绑定的账户（多账户时跟界面切换的那个），
    不是用户默认账户。"""
    from test_account_registry import BETA_ACCOUNT, DEFAULT_ACCOUNT, write_accounts_file
    from test_account_reminders import ReminderProvider
    from test_simulation_orders import USER

    monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(write_accounts_file(
        tmp_path / "accounts.json", [
            {"id": DEFAULT_ACCOUNT, "displayName": "账户甲", "dataMode": "fake"},
            {"id": BETA_ACCOUNT, "displayName": "账户乙", "dataMode": "fake"},
        ])))
    app = create_app(tmp_path / "quick-accounts.sqlite3", local_token="test-local-token",
                     agent_mode="fake", data_mode="fake",
                     account_providers={DEFAULT_ACCOUNT: ReminderProvider(), BETA_ACCOUNT: ReminderProvider()})
    try:
        for account in (DEFAULT_ACCOUNT, BETA_ACCOUNT):
            app.state.store.grant_account(account_id=account, user_id=USER, role="owner", granted_by=USER)
        beta = app.state.accounts.get(BETA_ACCOUNT).provider
        beta.summary_overrides = {"equity": 777_777}
        beta.position_items = [{"symbol": "SHFE.au2612", "direction": "short", "volume": 5,
                                "unrealizedPnl": -310, "todayVolume": 0, "historicalVolume": 5,
                                "frozenToday": 0, "frozenHistorical": 0}]
        beta_session = app.state.broker.create_session("local", "native", USER, trading_account_id=BETA_ACCOUNT)
        default_session = app.state.broker.create_session("local", "native", USER)
        service = VoiceRealtimeService(app, key_file=tmp_path / "missing-key")

        state = CallState(window_id=WINDOW, chat_session_id=beta_session.session_id, user_id=USER)
        account = await service.run_tool("get_account", {}, state)
        assert account["权益"] == "777,777 元"          # 读的是会话绑定的账户乙
        positions = await service.run_tool("get_positions", {}, state)
        assert "沪金2612 空 5 手" in positions["持仓"] and "多 2 手" not in positions["持仓"]

        plain = CallState(window_id=WINDOW, chat_session_id=default_session.session_id, user_id=USER)
        assert (await service.run_tool("get_account", {}, plain))["权益"] == "1,026,580 元"
    finally:
        app.state.store.close()


async def test_replay_tagged_announcements_stay_in_their_replay_call(tmp_path, monkeypatch):
    """#521 R2：回放里触发的提醒带着回放 id——只有绑到同一回放的通话念它；实盘通话只念没打标的
    （实盘自己的）条目，两边互不越界。"""
    import trader_runtime.voice_realtime as voice_module
    app, _, service = await _voice_app(tmp_path, "replay-announce.sqlite3")
    service.announce("live-event", "实盘的提醒", user_id="local-user")
    service.announce("replay-event", "回放的提醒", user_id="local-user", replay_id="a" * 32)
    items = service._pending_announcements["local-user"]
    assert items[0] == {"kind": "live-event", "text": "实盘的提醒"}
    assert items[1] == {"kind": "replay-event", "text": "回放的提醒", "replayId": "a" * 32}

    # 一路实盘通话：只念实盘条目，回放条目始终不出声。
    monkeypatch.setattr(voice_module, "ANNOUNCE_POLL_SECONDS", 0.05)

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})

    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "sessionId": ""})})
    await run(service, until=lambda: any(
        message.get("type") == "announce" and "实盘的提醒" in str(message.get("text")) for message in client_out))
    await asyncio.sleep(0.3)   # 几个轮询周期过去：回放条目不会插进来
    assert not any("回放的提醒" in str(message.get("text")) for message in client_out)
    app.state.store.close()


async def test_a_day_voice_review_opens_with_the_daily_summary_and_the_review_page(tmp_path, monkeypatch):
    """今日复盘 (2026-10-06): “复盘一下 / 今天复盘” first reads the latest review's summary (and opens 复盘 on
    that day in the caller's window), then asks the picks as before; a week review does not."""
    app, _, service = await _voice_app(tmp_path, "review-summary.sqlite3")
    calls = _fake_review_calls(monkeypatch, service)
    inner = service._call
    today = datetime.now(UTC).astimezone(CHINA).date().isoformat()
    sent: list[dict] = []

    async def fake(operation, *, path=None, query=None, body=None, replay=None):
        if operation == "GET /api/account/daily-review":
            return {"ok": True, "data": {"review": {"tradingDay": today, "summary": {"text": "今天三笔里两笔在计划内。"}}}}
        if operation == "POST /api/app/ui/commands":
            sent.append(body["changes"])
            return {"ok": True, "data": {"status": "applied"}}
        return await inner(operation, path=path, query=query, body=body, replay=replay)
    monkeypatch.setattr(service, "_call", fake)

    async def window(state):
        return {"windowId": WINDOW, "revision": 3}
    monkeypatch.setattr(service, "_window", window)
    state = CallState(window_id=WINDOW)
    assert service.review_route(state, "今天复盘", "delegate") == "review_start"
    opening = await service.review_step(state, "review_start", "今天复盘")
    assert opening.startswith("先说今天的复盘：今天三笔里两笔在计划内。好，今天有 3 笔值得聊聊")
    assert sent == [{"reviewDay": today}] and calls[0] == ("GET /api/account/review-picks", {"scope": "day"}, None)
    week = await service.review_step(CallState(window_id=WINDOW), "review_start", "复盘一下这周")
    assert week.startswith("好，这周有 3 笔")
    app.state.store.close()


# --- #523 R4 (observation only): the provider-side diagnostics -------------------------------------
# upstreamSent / providerEvent / audioPath carry ids, enums and counts only — never the spoken
# text, the base64 audio or any raw payload object.
_DIAG_MARK_FIELDS = {
    "upstreamSent": {"msg", "turn"},
    "providerEvent": {"msg", "rid", "nid", "status", "tts", "turn", "mute", "commit", "audio"},
    "audioPath": {"recvFrames", "recvBytes", "dropFrames", "dropBytes", "fwdFrames", "fwdBytes"},
}
# The uplink microphone fields on the same audioPath line (10-14): counts and levels only.
_MIC_FIELDS = {"micFrames", "micBytes", "micFirstAt", "micMutedFrames", "micRmsAvg", "micRmsPeak", "silentRatio"}
_DIAG_MARK_ALLOWED = {**_DIAG_MARK_FIELDS, "audioPath": _DIAG_MARK_FIELDS["audioPath"] | _MIC_FIELDS}


def _assert_diag_marks_clean(timeline, forbidden):
    for entry in timeline:
        if entry["event"] not in _DIAG_MARK_FIELDS:
            continue
        assert set(entry) - {"at", "event"} <= _DIAG_MARK_ALLOWED[entry["event"]], entry
        assert all(isinstance(v, (str, int, float, bool)) or v is None for v in entry.values()), entry
        dumped = json.dumps(entry, ensure_ascii=False)
        assert not any(value in dumped for value in forbidden), entry


async def _intent_call(tmp_path, monkeypatch, on_send):
    """The "打开设置" intent turn against a scripted upstream (the 09-18 test's shape), with the
    window command receipt faked; the fake call path itself is the shared `_scripted_call`.
    Returns (upstream_sent, client_out, inbound, run); the run wrapper closes this helper's own
    store when the call ends."""
    app, _, service = await _voice_app(tmp_path)
    window = {"windowId": WINDOW, "online": True, "revision": 4, "state": {"settingsOpen": False}}

    async def fake_call(operation, *, path=None, query=None, body=None):
        if operation == "GET /api/app/ui/windows":
            return {"data": {"windows": [window]}}
        assert operation == "POST /api/app/ui/commands"
        window["revision"] += 1
        return {"data": {"status": "applied", "observedRevision": window["revision"], "state": {}}}
    service._call = fake_call  # type: ignore[method-assign]

    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})

    async def run_and_close_store(*, until, timeout=15):
        try:
            await run(service, until=until, timeout=timeout)
        finally:
            app.state.store.close()
    return upstream_sent, client_out, inbound, run_and_close_store


async def test_provider_trace_commit_sent_upstream_but_answered_with_no_audio(tmp_path, monkeypatch):
    """#523 的线上形状（“切到十五分钟”）：commit 真的发出去了（upstreamSent 在 speakCommit 之后），
    provider 却一个音频帧都没回（audioPath recv 0）——从 timeline 就能区分“没发”和“本地丢弃”。"""
    import trader_runtime.voice_realtime as module
    states = []
    original_state = module.CallState

    def capture_state(*args, **kwargs):
        state = original_state(*args, **kwargs)
        states.append(state)
        return state

    monkeypatch.setattr(module, "CallState", capture_state)

    async def on_send(payload, put):
        kind = payload["type"]
        if kind == "session.create":
            await put({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await put({"type": "conversation.item.input_audio_transcription.completed", "text": "打开设置。"})
        elif kind == "speech_text_buffer.commit":
            # The provider ends the committed turn silently: no started, no delta, no done — the
            # failure being hunted; only response.done ever comes back, with its nested id/status
            # sent as objects carrying text: they must never be flattened into the timeline.
            await put({"type": "response.done", "response": {"id": {"text": "SECRET-ID"},
                                                            "status": {"text": "SECRET-STATUS"},
                                                            "usage": {"total_tokens": 2}}})
            # A provider cancellation advances the real call's turn while send is still awaiting
            # completion. The completed-send mark must retain its initiating turn.
            await put({"type": "response.canceled"})
            async with asyncio.timeout(1):
                while states[0].turn_serial == 1:
                    await asyncio.sleep(0)
            assert states[0].turn_serial == 2
        elif kind == "session.close":
            await put({"type": "session.closed"})
    upstream_sent, client_out, _, run = await _intent_call(tmp_path, monkeypatch, on_send)
    await run(until=lambda: any(e.get("type") == "text" and e.get("delta") == "设置打开了" for e in client_out))

    assert [p["text"] for p in upstream_sent if p["type"] == "speech_text_buffer.commit"] == ["设置打开了"]
    assert not any(e.get("type") == "audio" for e in client_out)
    timeline = client_out[-1]["timeline"]
    names = [e["event"] for e in timeline]
    committed = next(e for e in timeline if e["event"] == "speakCommit")
    assert committed["tool"] == "intent" and "chars" in committed
    sent = [e for e in timeline if e["event"] == "upstreamSent" and e.get("msg") == "speech_text_buffer.commit"]
    assert len(sent) == 1 and names.index("speakCommit") < names.index("upstreamSent")
    assert names.index("canceled") < names.index("upstreamSent")
    assert sent[0]["turn"] == 1  # initiating turn, although cancellation advanced it during send
    path = next(e for e in timeline if e["event"] == "audioPath")
    assert {k: path[k] for k in _DIAG_MARK_FIELDS["audioPath"]} == {"recvFrames": 0, "recvBytes": 0, "dropFrames": 0,
                                                                    "dropBytes": 0, "fwdFrames": 0, "fwdBytes": 0}
    assert sum(1 for e in timeline if e["event"] == "providerEvent" and e.get("msg") == "response.done") == 1
    poisoned = next(e for e in timeline if e["event"] == "providerEvent" and e.get("msg") == "response.done")
    assert poisoned["nid"] is None and poisoned["status"] is None and poisoned["rid"] is None
    assert "routedResponseWait" in names  # the silent turn ended only by the short mute-wait timeout
    assert any(e["event"] == "providerEvent" and e.get("msg") == "session.closed" for e in timeline)
    _assert_diag_marks_clean(timeline, ["设置打开了", "打开设置", "AAAA", "QUJD", "SECRET-ID", "SECRET-STATUS"])


async def test_provider_trace_counts_muted_and_cancelled_audio_as_dropped_not_missing(tmp_path, monkeypatch):
    """分支二：provider 的 filler 音频被 mute/canceled 过滤。过滤前 providerEvent 仍在（started 带
    mute=True），audioPath 把收到/丢弃/转发的帧分开记账——recv 与 drop 对得上，不是“没收到”。"""
    import trader_runtime.voice_realtime as module
    # The scripted filler starts 0.15 s after the final: the mute wait must outlive it (as in the
    # 09-18 test), or the Runtime speaks first and never cancels Doubao's reply.
    monkeypatch.setattr(module, "ROUTED_RESPONSE_WAIT_SECONDS", 2.0)

    async def on_send(payload, put):
        kind = payload["type"]
        if kind == "session.create":
            await put({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await put({"type": "conversation.item.input_audio_transcription.completed", "text": "打开设置。"})
            await asyncio.sleep(0.15)
            await put({"type": "response.output_text.delta", "delta": "好"})
            await put({"type": "response.output_audio.started"})
            await put({"type": "response.output_audio.delta", "delta": "AAAA"})
        elif kind == "response.cancel":
            await put({"type": "response.canceled"})
            # A malformed frame (a non-string delta: the frame counts, its bytes are 0) and a
            # padded one ("AA==" really decodes to 1 byte, not the old ×3//4 estimate's 3).
            await put({"type": "response.output_audio.delta", "delta": {"oops": True}})
            await put({"type": "response.output_audio.delta", "delta": "AA=="})
            await put({"type": "response.output_audio.done"})
            await put({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
        elif kind == "speech_text_buffer.commit":
            await put({"type": "response.output_audio.started"})
            await put({"type": "response.output_audio.delta", "delta": "QUJD"})
            await put({"type": "response.output_audio.done"})
            await put({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await put({"type": "session.closed"})
    upstream_sent, client_out, _, run = await _intent_call(tmp_path, monkeypatch, on_send)
    await run(until=lambda: any(e.get("type") == "text" and e.get("delta") == "设置打开了" for e in client_out))

    kinds = [p["type"] for p in upstream_sent]
    assert kinds.index("response.cancel") < kinds.index("speech_text_buffer.commit")
    assert not any(e.get("type") == "audio" and e.get("data") == "AAAA" for e in client_out)
    assert any(e.get("type") == "audio" and e.get("data") == "QUJD" for e in client_out)
    timeline = client_out[-1]["timeline"]
    names = [e["event"] for e in timeline]
    assert names.index("modelReplyMuted") < names.index("upstreamSent")  # the cancel send completed after the mute decision
    assert next(e for e in timeline if e["event"] == "upstreamSent" and e.get("msg") == "response.cancel")["turn"] == 1
    filler_started = [e for e in timeline if e["event"] == "providerEvent" and e.get("msg") == "response.output_audio.started"]
    assert len(filler_started) == 2 and filler_started[0]["mute"] is True and filler_started[1]["mute"] is False
    assert sum(1 for e in timeline if e["event"] == "audioStart") == 1  # only the commit reply's audio started
    path = next(e for e in timeline if e["event"] == "audioPath")
    # Real decoded bytes: AAAA=3, the malformed frame=0, AA===1 (dropped), QUJD=3 (forwarded).
    assert {k: path[k] for k in _DIAG_MARK_FIELDS["audioPath"]} == {"recvFrames": 4, "recvBytes": 7, "dropFrames": 3,
                                                                    "dropBytes": 4, "fwdFrames": 1, "fwdBytes": 3}
    _assert_diag_marks_clean(timeline, ["设置打开了", "打开设置", "好", "AA==", "oops"])


async def test_provider_trace_normal_audio_forwarded_with_ids_and_enums(tmp_path, monkeypatch):
    """分支三：正常链路。recv==fwd、drop 为 0；providerEvent 带 response_id / 嵌套 response.id /
    tts_type（限长，缺省 None），不改变转发行为。"""
    app, _, service = await _voice_app(tmp_path, "provider-forward.sqlite3")

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "沪金多少钱？"})
            await asyncio.sleep(0.1)
            await emit({"type": "response.function_call_arguments.done",
                        "items": [{"call_id": "c1", "name": "get_quote", "arguments": json.dumps({"query": "沪金"})}]})
        elif kind == "conversation.item.create" and payload["items"][0].get("role") == "tool":
            await emit({"type": "response.output_audio.started", "response_id": "r1", "tts_type": "chat_tts_text"})
            await emit({"type": "response.output_audio.delta", "delta": "AA==", "response_id": "r1"})  # 1 real byte
            await emit({"type": "response.output_audio.done", "response_id": "r1"})
            await emit({"type": "response.done", "response": {"id": "r1", "status": "completed", "usage": {"total_tokens": 3}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any(e.get("type") == "usage" for e in client_out))

    assert not any(p["type"] == "response.cancel" for p in upstream_sent)
    assert any(e.get("type") == "audio" and e.get("data") == "AA==" for e in client_out)
    timeline = client_out[-1]["timeline"]
    started = next(e for e in timeline if e["event"] == "providerEvent" and e.get("msg") == "response.output_audio.started")
    assert started["rid"] == "r1" and started["tts"] == "chat_tts_text" and started["nid"] is None and started["mute"] is False
    done = next(e for e in timeline if e["event"] == "providerEvent" and e.get("msg") == "response.done")
    assert done["nid"] == "r1" and done["rid"] is None
    assert done["status"] == "completed"  # the formal status lives on the nested response.status
    path = next(e for e in timeline if e["event"] == "audioPath")
    assert {k: path[k] for k in _DIAG_MARK_FIELDS["audioPath"]} == {"recvFrames": 1, "recvBytes": 1, "dropFrames": 0,
                                                                    "dropBytes": 0, "fwdFrames": 1, "fwdBytes": 1}
    _assert_diag_marks_clean(timeline, ["沪金多少钱", "QUJD", "AA=="])


def _tone(amplitude: int, samples: int = 320) -> bytes:
    """A 20 ms PCM16 square-ish wave at the given amplitude (RMS == amplitude)."""
    return b"".join(int(amplitude if i % 2 == 0 else -amplitude).to_bytes(2, "little", signed=True) for i in range(samples))


def test_mic_stats_counts_frames_levels_and_keeps_muted_frames_out_of_the_levels():
    from trader_runtime.voice_realtime import MIC_LEVEL_EVERY, MicStats
    stats = MicStats(started=100.0)
    assert stats.summary() == {"micFrames": 0, "micBytes": 0, "micFirstAt": None, "micMutedFrames": 0,
                               "micRmsAvg": None, "micRmsPeak": None, "silentRatio": None}
    # Ten sampled frames: six of digital silence, four of speech-level sound.
    for index in range(10 * MIC_LEVEL_EVERY):
        sampled = index // MIC_LEVEL_EVERY
        stats.add(b"\x00" * 640 if sampled < 6 else _tone(2000 if sampled < 9 else 4000), 101.25 + index * 0.02)
    stats.muted = True
    stats.add(_tone(8000), 103.0)  # muted: counted apart, never in the levels
    stats.muted = False
    summary = stats.summary()
    assert summary["micFrames"] == 10 * MIC_LEVEL_EVERY + 1
    assert summary["micBytes"] == 640 * summary["micFrames"]
    assert summary["micFirstAt"] == 1.25
    assert summary["micMutedFrames"] == 1
    assert summary["micRmsPeak"] == 4000
    assert summary["micRmsAvg"] == round((3 * 2000 + 4000) / 10)
    assert summary["silentRatio"] == 0.6
    # The base64 form counts its real decoded size; a broken payload is a 0-byte frame.
    stats.add_base64(base64.b64encode(_tone(1000)).decode(), 104.0)
    stats.add_base64("not base64!", 104.1)
    assert stats.summary()["micBytes"] == 640 * (summary["micFrames"] + 1)


async def test_audio_path_carries_the_uplink_microphone_counts_for_a_silent_caller(tmp_path, monkeypatch):
    """10-14 Windows 网页通话豆包没听到声音：audioPath 同一行带上行统计——收到了帧但全是数字静音
    （silentRatio 1.0）与根本没发帧（micFrames 0）从 timeline 就能分开；静音期间的帧单独计数。"""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "MIC_LEVEL_EVERY", 1)
    app, _, service = await _voice_app(tmp_path, "mic-uplink.sqlite3")

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif payload["type"] == "session.close":
            await emit({"type": "session.closed"})
    upstream_sent, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    for _ in range(3):
        await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "mute"})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "unmute"})})
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "audio", "data": base64.b64encode(b"\x00" * 640).decode()})})
    try:
        await run(service, timeout=1, until=lambda: sum(p["type"] == "input_audio_buffer.append" for p in upstream_sent) == 5)
    finally:
        app.state.store.close()
    path = next(e for e in client_out[-1]["timeline"] if e["event"] == "audioPath")
    assert {k: path[k] for k in _MIC_FIELDS - {"micFirstAt"}} == {
        "micFrames": 5, "micBytes": 3200, "micMutedFrames": 1, "micRmsAvg": 0, "micRmsPeak": 0, "silentRatio": 1.0}
    assert isinstance(path["micFirstAt"], float) and path["micFirstAt"] >= 0
    _assert_diag_marks_clean(client_out[-1]["timeline"], ["AAAA"])


async def test_pending_symbol_asr_final_reaches_card_owner_and_retires_the_old_confirmation(tmp_path, monkeypatch):
    app, gateway, service = await _voice_app(tmp_path, "pending-symbol-ws.sqlite3")
    prepared_state = CallState(window_id=WINDOW)
    await service.run_tool("delegate", {"utterance": "买入一手黄金"}, prepared_state)
    old_id = prepared_state.pending["proposals"][0]["proposalId"]
    calls = []
    original = service._call

    async def observe(operation, **kwargs):
        calls.append(operation)
        return await original(operation, **kwargs)
    monkeypatch.setattr(service, "_call", observe)

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif payload["type"] == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "换成螺纹"})
        elif payload["type"] == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.done"})
        elif payload["type"] == "session.close":
            await emit({"type": "session.closed"})
    _, output, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW,
                                                                       "sessionId": prepared_state.chat_session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    try:
        await run(service, timeout=1, until=lambda: any(e.get("type") == "tool_result" and e.get("name") == "delegate" for e in output))
        assert calls.count(CHAT_SEND) == 1 and "POST /api/app/ui/commands" not in calls
        assert any(e["event"] == "routed" and e["action"] == "delegate" for e in output[-1]["timeline"])
        assert app.state.store.get(old_id)["status"] == "rejected"
        assert (await service.run_tool("confirm", {}, CallState(window_id=WINDOW,
                          chat_session_id=prepared_state.chat_session_id)))["error"] == "nothing_pending"
        assert gateway.submit_count == 0
    finally:
        app.state.store.close()


@pytest.mark.parametrize("case", ["stale", "confirmed", "wrong_account"])
async def test_pending_symbol_reply_stops_before_the_model_when_old_card_rejection_is_unconfirmed(tmp_path, monkeypatch, case):
    app, gateway, service = await _voice_app(tmp_path, "pending-symbol-reject.sqlite3")
    state = CallState(window_id=WINDOW)
    await service.run_tool("delegate", {"utterance": "买入一手黄金"}, state)
    old_id = state.pending["proposals"][0]["proposalId"]
    calls = []
    original = service._call

    async def observe(operation, **kwargs):
        calls.append(operation)
        return await original(operation, **kwargs)
    original_http = service._http
    async def failed_reject(method, path, body=None, **kwargs):
        if path.endswith("/reject"):
            if case == "confirmed":
                concurrent = CallState(window_id=WINDOW, chat_session_id=state.chat_session_id)
                assert (await service.run_tool("confirm", {}, concurrent))["ok"] is True
            return 409, {"code": "stale_proposal_version"}
        code, record = await original_http(method, path, body, **kwargs)
        if case == "wrong_account":
            record = {**record, "tradingAccountId": "another-account"}
        return code, record
    monkeypatch.setattr(service, "_call", observe)
    monkeypatch.setattr(service, "_http", failed_reject)
    try:
        result = await service.run_tool("delegate", {"utterance": "换成螺纹"}, state)
        assert result["ok"] is False and result["error"] == "pending_reject_unconfirmed"
        assert "尚未处理" in result["say"] and CHAT_SEND not in calls
        expected_status = "candidate_recorded" if case == "confirmed" else "approval_required"
        assert app.state.store.get(old_id)["status"] == expected_status
        assert gateway.submit_count == int(case == "confirmed")  # only the concurrent authorized confirm
    finally:
        app.state.store.close()


@pytest.mark.parametrize("rejects", [True, False])
async def test_a_cancel_with_an_interface_tail_retires_the_card_before_delegating(tmp_path, monkeypatch, rejects):
    """#756: “取消这张卡，切到日线” is not fixed-table navigation — the old card is formally rejected
    before the delegate; when the server does not show it rejected, the turn stops there."""
    app, gateway, service = await _voice_app(tmp_path, "mixed-cancel.sqlite3")
    state = CallState(window_id=WINDOW)
    await service.run_tool("delegate", {"utterance": "买入一手黄金"}, state)
    old_id = state.pending["proposals"][0]["proposalId"]
    calls = []
    original, original_http = service._call, service._http

    async def observe(operation, **kwargs):
        calls.append(operation)
        return await original(operation, **kwargs)

    async def http(method, path, body=None, **kwargs):
        if path.endswith("/reject") and not rejects:
            return 409, {"code": "stale_proposal_version"}
        return await original_http(method, path, body, **kwargs)
    monkeypatch.setattr(service, "_call", observe)
    monkeypatch.setattr(service, "_http", http)
    try:
        result = await service.run_tool("delegate", {"utterance": "取消这张卡，切到日线"}, state)
        record = app.state.store.get(old_id)
        if rejects:
            assert record["status"] == "rejected" and record["approvalUsedAt"]
            assert CHAT_SEND in calls and result["ok"] is True
        else:
            assert result["ok"] is False and result["error"] == "pending_reject_unconfirmed"
            assert "旧卡还未确认作废" in result["say"] and CHAT_SEND not in calls
            assert record["status"] == "approval_required"
        assert gateway.submit_count == 0
    finally:
        app.state.store.close()


async def test_voice_reads_the_usd_ledger_beside_the_rmb_one(tmp_path, monkeypatch):
    """本站模拟两本账（所有者 2026-10-21）：语音问持仓、权益时美元子账户（外盘）一起报，金额分开不相加。"""
    app, _, service = await _voice_app(tmp_path, "quick-two-ledgers.sqlite3")
    state = CallState(window_id=WINDOW)
    now = {"dataStatus": "normal", "ageSeconds": 0.0, "updatedAt": "刚刚"}
    replies = {
        ("/api/account/summary", "paper:u1"): {**now, "accountName": "本站模拟账户", "equity": 1_000_000, "todayPnl": -300,
                                               "floatProfit": -300, "marginRatio": 2.0, "positionCount": 1},
        ("/api/account/summary", "paperusd:u1"): {**now, "equity": 100_031, "todayPnl": 127},
        ("/api/positions", "paper:u1"): {"items": [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 2,
                                                   "unrealizedPnl": -300}]},
        ("/api/positions", "paperusd:u1"): {"items": [{"symbol": "CME.GCEZ26", "instrumentName": "COMEX黄金2612",
                                                      "direction": "long", "volume": 1, "unrealizedPnl": 160}]},
    }

    async def http(method, path, body=None, *, account_id=None):
        return 200, replies[(path, account_id)]

    monkeypatch.setattr(service, "_http", http)
    monkeypatch.setattr(service, "_chat_account_id", lambda _state: "paper:u1")
    monkeypatch.setattr(app.state.accounts, "foreign_account_of",
                        lambda account: "paperusd:u1" if account == "paper:u1" else None, raising=False)
    positions = await service.run_tool("get_positions", {}, state)
    assert "COMEX黄金2612（外盘，美元） 多 1 手" in positions["持仓"]
    assert positions["合计浮动盈亏"] == "-300 元" and positions["外盘浮动盈亏"] == "+160 美元"
    account = await service.run_tool("get_account", {}, state)
    assert account["美元账（外盘）"] == "权益 100,031 美元，今日 +127 美元"
    await app.state.shared_market.close()
    app.state.store.close()

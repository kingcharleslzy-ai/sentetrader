"""External agents can open a chat, send a message and read the turn back; the embedded model cannot."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from trader_runtime.agent_api import CHAT_OPERATIONS, CHAT_SEND, AppCall
from trader_runtime.main import create_app

ORIGIN = "http://127.0.0.1:5173"
TOKEN = "chat-test-token"


@pytest.fixture
async def runtime(tmp_path: Path):
    app = create_app(tmp_path / "runtime.sqlite3", local_token=TOKEN, agent_mode="fake", cors_origins={ORIGIN})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     headers={"Origin": ORIGIN}) as client:
            login = await client.post("/api/auth/local", headers={"Authorization": f"Bearer {TOKEN}"})
            assert login.status_code == 200
            yield app, client


async def test_catalog_lists_the_conversation_operations(runtime) -> None:
    _, client = runtime
    manifest = (await client.get("/api/agent/capabilities")).json()
    by_operation = {item["operation"]: item for item in manifest["operations"]}
    for operation in CHAT_OPERATIONS:
        assert operation in by_operation, operation
    assert by_operation[CHAT_SEND]["name"] == "chat_send" and by_operation[CHAT_SEND]["readOnly"] is False
    detail = (await client.get("/api/agent/capabilities", params={"operation": CHAT_SEND})).json()
    assert detail["bodySchema"]["required"] == ["text"]
    # The event stream and session deletion stay outside the catalog.
    assert "GET /api/events" not in by_operation and "DELETE /api/sessions/{session_id}" not in by_operation


async def test_chat_send_creates_a_session_and_returns_the_turn_summary(runtime) -> None:
    _, client = runtime
    first = await client.post("/api/agent/call", json={"operation": CHAT_SEND, "body": {"text": "看看账户"}})
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["runStatus"] == "completed" and data["timedOut"] is False
    assert data["reply"] and not data["reply"].startswith("正在处理")
    assert data["eventCount"] >= 2 and data["toolSteps"] == []  # a plain answer runs no task steps
    session_id = data["sessionId"]
    # A second message in the same session reuses it and is answered too.
    second = await client.post("/api/agent/call", json={"operation": CHAT_SEND, "body": {"text": "行情如何", "sessionId": session_id}})
    assert second.status_code == 200 and second.json()["data"]["sessionId"] == session_id
    listed = (await client.get("/api/chat-sessions")).json()["items"]
    assert any(item["sessionId"] == session_id for item in listed)


async def test_chat_send_rejects_an_unknown_session_and_empty_text(runtime) -> None:
    _, client = runtime
    unknown = await client.post("/api/agent/call", json={"operation": CHAT_SEND, "body": {"text": "x", "sessionId": "nope"}})
    assert unknown.status_code == 404
    empty = await client.post("/api/agent/call", json={"operation": CHAT_SEND, "body": {"text": "   "}})
    assert empty.status_code == 422


async def test_chat_send_refuses_over_long_text_before_creating_a_session(runtime) -> None:
    """#529-A2：chat:send 的字数上限与消息路由一致（4000 字），并在新建会话之前就拒绝——
    超长文本不再先留下一个空会话再吃 422。"""
    app, client = runtime
    schema = (await client.get("/api/agent/capabilities", params={"operation": CHAT_SEND})).json()["bodySchema"]
    assert schema["properties"]["text"]["maxLength"] == 4000
    over_long = await client.post("/api/agent/call", json={"operation": CHAT_SEND, "body": {"text": "长" * 4001}})
    assert over_long.status_code == 422
    assert (await client.get("/api/chat-sessions")).json()["items"] == []  # 没有为它建过会话
    assert app.state.store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


async def test_chat_send_refuses_a_non_numeric_timeout_with_422(runtime) -> None:
    """#529-A2a：timeoutSeconds 不是数字时 422，而不是 float() 抛出 500；也不先建会话。"""
    app, client = runtime
    for bad in ("abc", True, [5], "nan", "inf"):
        refused = await client.post("/api/agent/call", json={
            "operation": CHAT_SEND, "body": {"text": "看看账户", "timeoutSeconds": bad}})
        assert refused.status_code == 422, bad
    assert app.state.store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


async def test_the_embedded_model_cannot_drive_a_conversation(runtime) -> None:
    app, client = runtime
    bridge = app.state.app_capabilities
    session = app.state.broker.create_session("local", "native", "local-user")
    headers = {"authorization": f"Bearer {TOKEN}"}
    for operation, body in [
        (CHAT_SEND, {"text": "再问一句"}),
        ("POST /api/sessions/{session_id}/messages", {"text": "再问一句"}),
        ("POST /api/sessions", {"mode": "local"}),
        ("PUT /api/models/preference", {"model": "deepseek-flash"}),
    ]:
        call = AppCall(operation=operation, body=body,
                       path={"session_id": session.session_id} if "{session_id}" in operation else {})
        with pytest.raises(HTTPException) as raised:
            await bridge.call(call, user="local-user", headers=headers, session_id=session.session_id)
        assert raised.value.status_code == 409 and raised.value.detail == "chat_recursion_forbidden"
    # Proposal cards come from the agent's own tools; the routes are refused inside a turn so a card can
    # never be created outside the conversation (the 1.6.0 acceptance saw the model do that for 全部平仓).
    from trader_runtime.agent_api import PROPOSAL_ROUTES
    for operation in sorted(PROPOSAL_ROUTES):
        with pytest.raises(HTTPException) as raised:
            await bridge.call(AppCall(operation=operation, body={}), user="local-user", headers=headers, session_id=session.session_id)
        assert raised.value.status_code == 409 and str(raised.value.detail).startswith("proposal_route_forbidden_use_tool")
    # The same operations work for an external caller (no originating chat session).
    external = await client.post("/api/agent/call", json={
        "operation": "POST /api/sessions/{session_id}/messages", "path": {"session_id": session.session_id}, "body": {"text": "外部一句"}})
    assert external.status_code == 200 and external.json()["data"]["status"] == "accepted"


async def test_models_are_readable_and_switchable_through_the_catalog(runtime) -> None:
    _, client = runtime
    models = await client.post("/api/agent/call", json={"operation": "GET /api/models"})
    assert models.status_code == 200 and "models" in models.json()["data"]
    missing = await client.post("/api/agent/call", json={"operation": "PUT /api/models/preference", "body": {"model": "no-such-model"}})
    assert missing.json()["status"] == 404


async def test_chat_send_reports_the_task_steps_of_a_working_turn(runtime) -> None:
    app, client = runtime
    # The fake agent stages its backtest progress over ~2s of sleeps; the assertions below
    # only read the finished steps/model, not the pacing (test_api.py does the same).
    app.state.adapter.backtest_progress_delay = 0
    result = await client.post("/api/agent/call", json={"operation": CHAT_SEND, "body": {"text": "修改策略并回测"}})
    assert result.status_code == 200, result.text
    steps = result.json()["data"]["toolSteps"]
    assert steps and all(step["name"] and step["status"] == "done" for step in steps)
    assert all("tool" in step for step in steps) and "model" in result.json()["data"]



async def test_chat_send_returns_only_this_runs_turn_amid_late_reminders_and_other_runs(runtime, monkeypatch) -> None:
    """#523 R3a：一轮 chat:send 期间落进同会话的别的东西——收盘提醒（另一路投递的 assistant 消息）、
    别的一轮的任务完成与答案、带别轮 turnId 的警告——都不算这一轮的答复：回复、工具步骤、模型与
    警告只取本次 run 的 turn；适配器没写任务标记的轮次维持原有的整个窗口收集（前面的用例即回退形态）。"""
    app, client = runtime
    created = await client.post("/api/sessions", params={"chat": "true"}, json={"mode": "local"})
    assert created.status_code == 201, created.text
    session_id = created.json()["sessionId"]
    broker = app.state.broker

    async def scripted(target_session, text, run_id, *, user_id=None, model_slug=None):
        # 本次 run 自己的标记（native 循环的形状）：task 事件带 taskId 与模型 turn 的 turnId。
        await broker.publish(target_session, kind="progress", name="task.started",
                             payload={"taskId": run_id, "turnId": "turn-own", "title": "回答中", "subtitle": "",
                                      "progress": 10, "status": "running", "steps": []})
        await broker.publish(target_session, kind="message", name="message.completed",
                             payload={"turnId": "turn-own", "role": "assistant", "text": "本问题的答案"})
        await broker.publish(target_session, kind="progress", name="task.completed",
                             payload={"taskId": run_id, "turnId": "turn-own", "title": "回答中", "subtitle": "",
                                      "progress": 100, "status": "completed",
                                      "steps": [{"id": "s1", "label": "回测一手参数", "tool": "backtest",
                                                 "status": "done", "detail": "收益 12%"}],
                                      "model": "glm-test"})
        # 同一会话里后来落进的：提醒（无 taskId）、别的一轮的任务完成与答案、别轮的警告。
        await broker.publish(target_session, kind="message", name="message.completed",
                             payload={"turnId": "reminder-market_close_day-acct-2026-10-04", "role": "assistant",
                                      "text": "收盘提醒：4 分钟后日盘收盘"})
        await broker.publish(target_session, kind="progress", name="task.completed",
                             payload={"taskId": "run-other", "turnId": "turn-other", "title": "别的任务",
                                      "subtitle": "", "progress": 100, "status": "completed",
                                      "steps": [{"id": "x1", "label": "别人的步骤", "tool": "backtest",
                                                 "status": "done", "detail": ""}],
                                      "model": "other-model"})
        await broker.publish(target_session, kind="message", name="message.completed",
                             payload={"turnId": "turn-other", "role": "assistant", "text": "别的问题的答案"})
        await broker.publish(target_session, kind="warning", name="warning",
                             payload={"turnId": "turn-other", "title": "别人的警告", "message": "本轮未读取实时数据",
                                      "severity": "warning", "deterministic": True})

    monkeypatch.setattr(app.state.adapter, "handle_message", scripted)
    result = await client.post("/api/agent/call",
                               json={"operation": CHAT_SEND, "body": {"text": "看看账户", "sessionId": session_id}})
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["runStatus"] == "completed" and data["reply"] == "本问题的答案"
    assert data["toolSteps"] == [{"name": "回测一手参数", "tool": "backtest", "status": "done", "detail": "收益 12%"}]
    assert data["model"] == "glm-test" and data["warnings"] == [] and data["pendingProposals"] == []

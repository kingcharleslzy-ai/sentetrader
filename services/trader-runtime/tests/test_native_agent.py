"""Behavior contract of the Runtime's native model loop (OpenAI Responses wire).

Every test drives the public HTTP surface through `create_app` with the fake
Responses server, so the assertions describe what a client observes: event
order, gating, tool cards, cancellation and failure scoping.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fixtures.card_tools import legacy_card_args
from fixtures.fake_responses_server import CORE_TOOLS, FakeResponsesServer
from trader_runtime.main import create_app
from trader_runtime.model_provider import KIMI_MODEL
from trader_runtime.strategy_explanations import ModelSummary
from trader_runtime.turn_router import MARKET_QUERY_TOOLS, UI_FALLBACK_TOOLS
from trader_runtime.workspace_policy import WorkspacePolicy

TOKEN = "test-native-token"
ORIGIN = "http://127.0.0.1:5173"
DEEPSEEK_KEY = "sk-fixture-deepseek-not-real"
ZHIPU_KEY = "sk-fixture-zhipu-not-real"
KIMI_KEY = "sk-fixture-kimi-not-real"


@pytest.fixture
def responses_server() -> FakeResponsesServer:
    return FakeResponsesServer(expected_keys={"deepseek-flash": DEEPSEEK_KEY, "glm-5.3-flash": ZHIPU_KEY, KIMI_MODEL: KIMI_KEY})


@pytest.fixture(autouse=True)
def _fast_research_deadlines(monkeypatch):
    """A market query also asks the research desk for its judgment card; with the fake provider
    the sources never answer and every wait runs to its bound. Bounds only — shorten them so a
    turn does not wait real 4–8 s seconds for a card the test is not about."""
    import trader_runtime.native_agent as native_agent_module
    import trader_runtime.research_desk as research_desk_module
    monkeypatch.setattr(research_desk_module, "SOURCE_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(native_agent_module.NativeAgentAdapter, "JUDGMENT_POLL_SECONDS", 0.05)


@pytest.fixture
async def native_application(tmp_path: Path, responses_server: FakeResponsesServer, monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", ZHIPU_KEY)
    monkeypatch.setenv("MOONSHOT_API_KEY", KIMI_KEY)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    application = create_app(
        tmp_path / "native.sqlite3",
        local_token=TOKEN,
        agent_mode="native",
        agent_provider="deepseek",
        deepseek_api_key=DEEPSEEK_KEY,
        native_transport=responses_server.transport(),
        engineering_root=tmp_path / "ai-trader-project",
    )
    yield application
    await application.state.runs.shutdown()
    await application.state.adapter.shutdown()
    await application.state.broker.shutdown()
    application.state.store.close()


@pytest.fixture
async def native_client(native_application: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=native_application), base_url="http://test",
        headers={"origin": ORIGIN},
    ) as client:
        response = await client.post(
            "/api/auth/local", headers={"authorization": f"Bearer {TOKEN}", "origin": ORIGIN}
        )
        assert response.status_code == 200
        yield client


async def create_session(client: AsyncClient) -> str:
    response = await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})
    assert response.status_code == 201
    return response.json()["sessionId"]


async def start_run(client: AsyncClient, session_id: str, text: str) -> str:
    response = await client.post(f"/api/sessions/{session_id}/messages", json={"text": text})
    assert response.status_code == 202
    return response.json()["runId"]


async def start_context_run(client: AsyncClient, session_id: str, text: str) -> str:
    response = await client.post(
        f"/api/sessions/{session_id}/messages",
        json={
            "text": text,
            "workspaceContext": {
                "view": "trade", "symbol": "SHFE.au2612", "durationSeconds": 300,
                "selectedPrice": 823.5, "quoteDatetime": "2026-09-02 09:31:02.000000",
                "capturedAt": "2026-09-02T01:31:02Z", "ageSeconds": 0.4, "dataStatus": "normal",
                "marketSessionStatus": "trading", "lastPrice": 823.5, "bidPrice1": 823.0,
                "askPrice1": 824.0, "priceTick": 0.5, "positions": [], "activeOrderCount": 0,
            },
        },
    )
    assert response.status_code == 202
    return response.json()["runId"]


async def wait_for_run(application: FastAPI, run_id: str, expected: set[str] | None = None) -> dict:
    terminal = expected or {"completed", "failed", "cancelled"}
    for _ in range(400):
        run = application.state.store.get_run(run_id)
        if run["status"] in terminal:
            return run
        await asyncio.sleep(0.01)
    session_id = application.state.store.get_run(run_id)["sessionId"]
    events = [event["name"] for _, event in application.state.store.list_events(session_id)]
    raise AssertionError(f"run {run_id} did not reach {terminal}; events={events}")


async def wait_for_event(application: FastAPI, session_id: str, name: str, **match: object) -> dict:
    # A run reaching a terminal status in the store does not mean the turn task
    # has published its closing events yet; wait for the event itself.
    for _ in range(400):
        for event in events_of(application, session_id):
            if event["name"] == name and all(event["payload"].get(key) == value for key, value in match.items()):
                return event
        await asyncio.sleep(0.01)
    events = [event["name"] for event in events_of(application, session_id)]
    raise AssertionError(f"event {name} matching {match} did not land; events={events}")


def events_of(application: FastAPI, session_id: str) -> list[dict]:
    return [event for _, event in application.state.store.list_events(session_id)]


def assistant_answers(application: FastAPI, session_id: str) -> list[str]:
    return [
        event["payload"]["text"] for event in events_of(application, session_id)
        if event["name"] == "message.completed" and event["payload"].get("role") == "assistant"
    ]


# --- request contract -------------------------------------------------------

async def test_turn_publishes_the_ui_event_sequence_and_sends_the_tool_contract(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "只做只读策略分析")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    names = [event["name"] for event in events_of(native_application, session_id)]
    assert {"message.delta", "message.completed", "task.started", "task.completed", "agent.result"} <= set(names)
    assert names.index("task.completed") < len(names) - 1 - names[::-1].index("message.completed")
    request = responses_server.requests[-1]
    assert request["model"] == "deepseek-flash"
    assert request["stream"] is True
    assert "authenticated product APIs" in request["instructions"]
    assert CORE_TOOLS <= {tool["name"] for tool in request["tools"]}
    assert all(tool["type"] == "function" and "parameters" in tool for tool in request["tools"])
    assert "previous_response_id" not in request
    assert native_application.state.adapter.workspace.resolved_root.name == "ai-trader-project"


async def test_configured_provider_is_authenticated_without_any_login(
    native_client: AsyncClient,
):
    account = await native_client.get("/api/agent/account")
    assert account.status_code == 200
    assert account.json() == {
        "available": True, "authenticated": True, "loginRequired": False,
        "configurationRequired": False, "credentialConfigured": True,
        "provider": "deepseek", "providerName": "DeepSeek", "model": "deepseek-flash",
        "authMode": None, "planType": None,
    }
    # There is no login flow any more: the route itself is gone.
    login = await native_client.post("/api/agent/login")
    assert login.status_code == 404


async def test_missing_api_key_fails_closed_with_a_configuration_error(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    server = FakeResponsesServer()
    application = create_app(
        tmp_path / "missing.sqlite3", local_token=TOKEN, agent_mode="native",
        agent_provider="deepseek", native_transport=server.transport(),
        engineering_root=tmp_path / "ai-trader-project",
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test",
                               headers={"origin": ORIGIN}) as client:
            await client.post("/api/auth/local", headers={"authorization": f"Bearer {TOKEN}", "origin": ORIGIN})
            account = await client.get("/api/agent/account")
            assert account.json()["configurationRequired"] is True
            assert account.json()["authenticated"] is False
            session_id = await create_session(client)
            run_id = await start_run(client, session_id, "没有 Key 时不得进入模型")
            assert (await wait_for_run(application, run_id))["status"] == "failed"
            errors = [event for event in events_of(application, session_id) if event["name"] == "error"]
            assert errors[-1]["payload"]["code"] == "deepseek_api_key_required"
            assert server.requests == []
    finally:
        await application.state.runs.shutdown()
        await application.state.adapter.shutdown()
        await application.state.broker.shutdown()
        application.state.store.close()


async def test_user_bubble_hides_the_structured_workspace_context(
    native_client: AsyncClient, native_application: FastAPI,
):
    session_id = await create_session(native_client)
    question = "当前这张图怎么看？"
    run_id = await start_context_run(native_client, session_id, question)
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    user_messages = [
        event["payload"]["text"] for event in events_of(native_application, session_id)
        if event["name"] == "message.completed" and event["payload"].get("role") == "user"
    ]
    assert user_messages[-1] == question
    assert "workspaceContext" not in user_messages[-1]


# --- freshness gate ---------------------------------------------------------

async def test_freshness_gate_labels_an_unverified_answer_instead_of_dropping_it(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    run_id = await start_context_run(native_client, session_id, "强制遗漏首次搜索，现在怎么看？")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert len(responses_server.requests) == 1
    answers = assistant_answers(native_application, session_id)
    assert answers[-1].startswith("这条未搜索答案不应显示。")
    events = events_of(native_application, session_id)
    warning_index = next(i for i, event in enumerate(events)
                         if event["name"] == "warning" and event["payload"]["title"] == "本轮未读取实时数据")
    answer_index = next(i for i, event in enumerate(events)
                        if event["name"] == "message.completed" and event["payload"].get("text") == answers[-1])
    assert warning_index < answer_index
    assert events[warning_index]["payload"]["turnId"] == events[answer_index]["payload"]["turnId"]


async def test_reading_the_manual_is_not_evidence_that_the_prices_were_checked(
    native_client: AsyncClient, native_application: FastAPI,
):
    """外审 09-19 A6: any business tool call used to clear the freshness footnote, so answering from
    memory after `app_capabilities` looked verified. Only a successful live-fact read counts now."""
    session_id = await create_session(native_client)
    run_id = await start_context_run(native_client, session_id, "只读操作手册后断言行情，现在怎么看？")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    warnings = [event["payload"] for event in events_of(native_application, session_id) if event["name"] == "warning"]
    assert [item["title"] for item in warnings] == ["本轮未读取实时数据"]


async def test_a_failed_market_read_is_not_evidence_either(
    native_client: AsyncClient, native_application: FastAPI,
):
    """The old rule marked the turn verified before dispatch, so a failed snapshot counted as well."""
    session_id = await create_session(native_client)
    run_id = await start_context_run(native_client, session_id, "行情读取失败后断言行情，现在怎么看？")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    warnings = [event["payload"] for event in events_of(native_application, session_id) if event["name"] == "warning"]
    assert "本轮未读取实时数据" in [item["title"] for item in warnings]


async def test_freshness_gate_stays_silent_when_the_answer_states_no_live_facts(
    native_client: AsyncClient, native_application: FastAPI,
):
    session_id = await create_session(native_client)
    run_id = await start_context_run(native_client, session_id, "强制遗漏首次搜索，只聊天气，现在怎么看？")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert not [event for event in events_of(native_application, session_id) if event["name"] == "warning"]


def test_asserts_live_facts_needs_a_market_word_and_a_number():
    from trader_runtime.native_agent import asserts_live_facts
    assert asserts_live_facts("螺纹最新价 3126 元，昨天的消息说要涨。")
    assert asserts_live_facts("9月17日的新闻：进口纱报价回落。")
    assert not asserts_live_facts("今天天气不错，适合出门走走。")
    assert not asserts_live_facts("成交量放大说明关注度在提高。")  # no number
    assert not asserts_live_facts(None)


async def test_gated_turn_streams_progress_and_only_holds_the_assistant_text(
    native_client: AsyncClient, native_application: FastAPI,
):
    session_id = await create_session(native_client)
    run_id = await start_context_run(native_client, session_id, "强制遗漏首次搜索，现在怎么看？")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    events = events_of(native_application, session_id)
    tasks = [event["payload"] for event in events
             if event["kind"] == "progress" and event["payload"].get("taskId") == run_id]
    assert tasks and tasks[0]["status"] == "running" and tasks[0]["title"] == "正在核验最新信息"
    assert tasks[-1]["status"] == "completed" and tasks[-1]["startedAt"]
    assert not any(event["kind"] == "progress" and event["payload"].get("taskId") != run_id for event in events)
    assert not any(event["name"] == "message.delta" and "这条未搜索答案不应显示" in event["payload"].get("text", "")
                   for event in events)
    completed_index = next(i for i, event in enumerate(events)
                           if event["kind"] == "progress" and event["payload"].get("taskId") == run_id
                           and event["payload"]["status"] == "completed")
    answer_index = next(i for i, event in enumerate(events)
                        if event["name"] == "message.completed" and "这条未搜索答案不应显示" in event["payload"].get("text", ""))
    assert completed_index < answer_index


async def test_gated_turn_keeps_streaming_tool_cards_before_the_verified_answer(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    run_id = await start_context_run(native_client, session_id, "强制遗漏首次搜索，现在读取Trader数据")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    events = events_of(native_application, session_id)
    account_index = next(i for i, event in enumerate(events) if event["name"] == "card.account")
    answer_index = next(i for i, event in enumerate(events)
                        if event["name"] == "message.completed" and event["payload"].get("role") == "assistant"
                        and "Trader只读工具返回" in event["payload"]["text"])
    assert account_index < answer_index
    # A live business-tool read verifies freshness: no warning card, no replay.
    assert not any(event["name"] == "warning" for event in events)
    assert len(responses_server.requests) == 2


async def test_meta_question_bypasses_the_freshness_gate(
    native_client: AsyncClient, native_application: FastAPI,
):
    session_id = await create_session(native_client)
    run_id = await start_context_run(native_client, session_id, "你现在是什么模型？")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    answers = assistant_answers(native_application, session_id)
    assert answers and answers[-1] == "已完成只读分析。"
    assert not any(event["name"] == "warning" for event in events_of(native_application, session_id))


async def test_direct_stop_reaches_the_model_without_waiting_for_search(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
):
    adapter = native_application.state.adapter
    stop = AsyncMock(wraps=adapter.trader_tools.autonomy.deactivate)
    monkeypatch.setattr(adapter.trader_tools.autonomy, "deactivate", stop)
    session_id = await create_session(native_client)
    run_id = await start_context_run(native_client, session_id, "现在急停")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    stop.assert_awaited_once()
    assert stop.call_args.kwargs["emergency"] is True
    assert len(responses_server.requests) == 2


async def test_web_search_tool_verifies_a_gated_turn(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
):
    search = AsyncMock(return_value={"query": "螺纹钢 最新消息", "results": [], "searchedAt": "2026-09-12T00:00:00Z"})
    monkeypatch.setattr(native_application.state.adapter.trader_tools.web_search, "search", search)
    session_id = await create_session(native_client)
    run_id = await start_context_run(native_client, session_id, "联网搜索后回答，现在怎么看？")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    search.assert_awaited_once()
    assert assistant_answers(native_application, session_id)[-1] == "已联网核验后的回答。"
    assert not any(event["name"] == "warning" for event in events_of(native_application, session_id))


async def test_unavailable_market_context_replaces_the_model_with_three_short_sentences(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    run_id = await start_context_run(native_client, session_id, "行情不可用短答，现在怎么看？")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    answers = assistant_answers(native_application, session_id)
    assert not any("等待几十秒" in answer for answer in answers)
    assert answers[-1] == (
        "结论：实时行情还没就绪，暂时不判断这个合约的方向。\n\n"
        "为什么：缺少最新价格和K线，新闻不能代替盘面。\n\n"
        "现在怎么做：数据恢复前先观望。"
    )
    # The loop stops after the failed context read instead of paying for another model round.
    assert len(responses_server.requests) == 1


# --- tools ------------------------------------------------------------------

async def test_read_only_trader_tool_is_validated_and_publishes_its_card(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "请读取Trader数据")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    events = events_of(native_application, session_id)
    assert any(event["name"] == "card.account" and event["payload"]["dataStatus"] == "simulated" for event in events)
    assert any("Trader只读工具返回" in answer for answer in assistant_answers(native_application, session_id))
    follow_up = responses_server.requests[-1]["input"]
    call = next(item for item in follow_up if item.get("type") == "function_call")
    output = next(item for item in follow_up if item.get("type") == "function_call_output")
    assert call["name"] == "get_account_summary" and output["call_id"] == call["call_id"]
    # The provider's reasoning item travels back ahead of the call it produced.
    kinds = [item.get("type") for item in follow_up if item.get("type")]
    assert kinds == ["reasoning", "function_call", "function_call_output"]
    assert next(item for item in follow_up if item.get("type") == "reasoning")["reasoning_text"].startswith("thinking-")
    completed = next(event for event in events if event["name"] == "task.completed")
    task_steps = [step["label"] for step in completed["payload"]["steps"]]
    assert "读取账户" in task_steps
    # Agents confirm the model and the raw tool from the events themselves, not from Runtime logs.
    assert next(step["tool"] for step in completed["payload"]["steps"] if step["label"] == "读取账户") == "get_account_summary"
    assert "tool" not in next(step for step in completed["payload"]["steps"] if step["id"] == "understand")
    assert completed["payload"]["model"] == native_application.state.adapter.model
    result = next(event for event in events if event["name"] == "agent.result")
    assert result["payload"]["model"] == native_application.state.adapter.model
    assert not hasattr(native_application.state.adapter.trader_tools, "insert_order")
    assert not hasattr(native_application.state.adapter.trader_tools, "cancel_order")


async def test_an_unexpected_tool_exception_is_reported_to_the_model_not_fatal(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
):
    async def broken(**_kwargs):
        raise RuntimeError("sidecar exploded")

    monkeypatch.setattr(native_application.state.adapter.trader_tools, "call_async", broken)
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "请读取Trader数据")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    events = events_of(native_application, session_id)
    assert any(event["name"] == "warning" and event["payload"]["title"] == "工具执行失败" for event in events)
    assert "sidecar exploded" not in repr(events)
    output = next(item for item in responses_server.requests[-1]["input"] if item.get("type") == "function_call_output")
    assert json.loads(output["output"])["errorCode"] == "tool_failed"
    assert assistant_answers(native_application, session_id)[-1] == "Trader只读工具明确返回数据不可用。"


async def test_a_tool_whose_skill_was_switched_off_mid_turn_refuses_execution(
    native_application: FastAPI, monkeypatch,
):
    """529-A1: the tool list a turn already carries must not outlive the skill's
    availability. Switching the owning skill off between listing and execution
    is refused with the registry's unified line and the tool never runs; a core
    tool no skill owns keeps executing."""
    from trader_runtime.native_agent import _ToolCall, _Turn
    from trader_runtime.store import utc_now

    adapter = native_application.state.adapter
    session = native_application.state.broker.create_session("local", "native", "local-user")
    turn = _Turn(session_id=session.session_id, run_id="run-skill-off", turn_id="turn-1",
                 mode="local", gated=False,
                 task={"turnId": "turn-1", "taskId": "run-skill-off", "title": "AI 正在处理",
                       "subtitle": "", "status": "running", "startedAt": utc_now(),
                       "summary": "", "steps": [], "progress": 0})
    executed: list[str] = []

    async def fake_call_async(**kwargs):
        executed.append(str(kwargs["tool"]))
        return {"success": True, "contentItems": [{"type": "text", "text": f"{kwargs['tool']} ok"}]}, None

    monkeypatch.setattr(adapter.trader_tools, "call_async", fake_call_async)

    # While web-research is available the tool runs; the switch then goes off.
    first = await adapter._execute_tool(
        turn, _ToolCall(item_id="i1", call_id="c1", name="web_search",
                        arguments=json.dumps({"query": "螺纹钢库存"})), "local-user")
    assert executed == ["web_search"] and "web_search ok" in first

    native_application.state.store.set_skill_enabled("web-research", False)
    refused = await adapter._execute_tool(
        turn, _ToolCall(item_id="i2", call_id="c2", name="web_search",
                        arguments=json.dumps({"query": "螺纹钢库存"})), "local-user")
    assert executed == ["web_search"]  # the refused call never reached the tool
    assert "联网研究" in refused and "停用" in refused

    # An unowned core tool is never skill-refused.
    core = await adapter._execute_tool(
        turn, _ToolCall(item_id="i3", call_id="c3", name="get_account_summary", arguments="{}"),
        "local-user")
    assert executed == ["web_search", "get_account_summary"] and "get_account_summary ok" in core


async def test_parallel_tool_calls_are_executed_in_order_and_answered_together(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "两个并行工具")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert assistant_answers(native_application, session_id)[-1] == "并行工具已返回：get_account_summary+get_positions。"
    follow_up = responses_server.requests[-1]["input"]
    kinds = [item.get("type") for item in follow_up if item.get("type") in {"function_call", "function_call_output"}]
    assert kinds == ["function_call", "function_call", "function_call_output", "function_call_output"]
    assert len(responses_server.requests) == 2


async def test_advice_is_built_from_one_verified_futures_context(
    native_client: AsyncClient, native_application: FastAPI,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "请给我完整期货建议")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    answer = assistant_answers(native_application, session_id)[-1]
    assert all(section in answer for section in ("结论：", "为什么：", "风险：", "现在怎么做："))
    assert "实盘功能未开启" in answer
    assert len(answer) <= 220
    for internal_term in ("analysisReady", "proposalReady", "liveTrading", "approval_required", "dataStatus",
                          "TqSdk", "TqKq", "Runtime", "Sidecar"):
        assert internal_term not in answer


async def test_candidate_is_created_in_one_round_with_its_readiness_gate_and_no_order_is_sent(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """Round reduction (2026-09-19): an order request is one model round — the card tool (propose_order) runs
    the readiness gate get_futures_decision_context used to supply, and the card carries the result."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "请生成交易候选：沪金空一手")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    events = events_of(native_application, session_id)
    proposals = [event for event in events if event["name"] == "proposal.trade"]
    assert len(proposals) == 1
    card = proposals[0]["payload"]
    assert card["status"] == "approval_required"
    assert card["environment"] == "simulation"
    assert card["symbol"] == "SHFE.AU2612"
    assert card["readiness"]["passed"] is True and "就绪检查已通过" in card["readiness"]["summary"]
    assert card["riskCheck"]["reason"].startswith("就绪检查已通过")
    completed = [event for event in events if event["name"] == "task.completed"][-1]
    assert [step["tool"] for step in completed["payload"]["steps"] if step.get("tool")] == ["propose_order"]
    assert len(responses_server.requests) == 2  # one tool round + the answer
    assert native_application.state.simulation_orders.list(user_id="local-user") == []
    assert any("尚未发送任何委托" in answer for answer in assistant_answers(native_application, session_id))


async def test_candidate_gate_not_ready_makes_no_card_and_explains_in_chinese(
    native_client: AsyncClient, native_application: FastAPI,
):
    """The same fail-closed outcome as when get_futures_decision_context reported proposalReady=false:
    no card, the missing item named with the userFacing label, no internal codes in the answer."""
    tools = native_application.state.adapter.trader_tools
    original = tools.runtime_status
    tools.runtime_status = lambda mode: {**original(mode), "accountStatus": "stale"}
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "请生成交易候选：沪金空一手")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    events = events_of(native_application, session_id)
    assert not [event for event in events if event["name"] == "proposal.trade"]
    answer = assistant_answers(native_application, session_id)[-1]
    assert "账户数据不是最新状态" in answer
    assert "proposal_gate_not_ready" not in answer and "account_not_fresh" not in answer


async def test_candidate_with_market_unavailable_fails_closed_like_the_context_tool(
    native_client: AsyncClient, native_application: FastAPI,
):
    tools = native_application.state.adapter.trader_tools
    original = tools.runtime_status
    tools.runtime_status = lambda mode: {**original(mode), "marketDataStatus": "unavailable"}
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "请生成交易候选：沪金空一手")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert not [event for event in events_of(native_application, session_id) if event["name"] == "proposal.trade"]
    assert "实时行情还没就绪" in assistant_answers(native_application, session_id)[-1]


# --- on-demand screen snapshot ------------------------------------------------------


def _window_pulse(window_id: str, text: str) -> dict:
    from trader_runtime.ui_control import ChartOptions
    state = dict(symbol="SHFE.au2612", duration=300, view="trade", panel="order", settingsOpen=False, theme="dark",
                 dockSide="right", dockWidth=400, replay=False, settingsPage="account", fontScale="standard",
                 density="standard", chart={key: True for key in ChartOptions.model_fields},
                 chartSizes=dict(main=58, kd=19, macd=23), strategy=None, highlights=[], chartAnnotations=[], chartFocus=None)
    observation = {"capturedAt": "2026-09-19T01:00:00Z", "viewport": {"width": 1280, "height": 720}, "text": text,
                   "truncated": False, "regions": [], "controls": [], "context": {"highlightTargets": ["rail:order", "positions-bar"]}}
    return {"windowId": window_id, "revision": 0, "state": state, "observation": observation}


async def _register_window(client: AsyncClient, text: str = "交易台 沪金2612 最新价 823.5 持仓 多 2 手 浮盈 2670") -> str:
    from uuid import uuid4
    window_id = str(uuid4())
    response = await client.post("/api/ui/pulse", json=_window_pulse(window_id, text))
    assert response.status_code == 200, response.text
    return window_id


def _model_saw(responses_server: FakeResponsesServer, fragment: str) -> bool:
    # The latest conversation-turn request (it carries tools): a side-channel structured call — e.g.
    # the research desk's judgment an opinion turn may start in the background (判断卡) — can land after it.
    body = [request for request in responses_server.requests if request.get("tools")][-1]
    return any(fragment in str(item.get("content") or "") for item in body.get("input") or [] if isinstance(item, dict))


async def test_a_turn_that_points_at_the_screen_gets_the_window_snapshot_attached(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“这个数 / 屏幕上…”: Runtime attaches the conversation window's snapshot to the turn instead of
    asking the model to spend a round on app_call; the instruction tells it not to read again."""
    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "屏幕上这个数是什么意思", "uiWindowId": window_id})
    assert response.status_code == 202
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    assert _model_saw(responses_server, "页面快照（本对话窗口现在显示的内容")
    assert _model_saw(responses_server, "浮盈 2670") and _model_saw(responses_server, "不要再用 app_call 读窗口快照")
    assert _model_saw(responses_server, "焦点 view=trade；symbol=SHFE.au2612")
    # The user bubble stays the plain question.
    user_bubbles = [event["payload"]["text"] for event in events_of(native_application, session_id)
                    if event["name"] == "message.completed" and event["payload"].get("role") == "user"]
    assert user_bubbles == ["屏幕上这个数是什么意思"]


async def test_a_turn_that_does_not_point_at_the_screen_gets_no_snapshot(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "沪金现在多少钱", "uiWindowId": window_id})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    assert not _model_saw(responses_server, "页面快照（本对话窗口")
    assert not _model_saw(responses_server, "先读本对话窗口的页面快照")  # the model is no longer told to read on its own


async def test_auto_read_screen_off_keeps_the_snapshot_out(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    window_id = await _register_window(native_client)
    assert (await native_client.put("/api/preferences", json={"chat": {"autoReadScreen": False}})).status_code == 200
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "屏幕上这个数是什么意思", "uiWindowId": window_id})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    assert not _model_saw(responses_server, "页面快照（本对话窗口")
    assert _model_saw(responses_server, "不必先读页面快照")  # the account's own preference line stays


async def test_a_turn_carries_the_memory_block_until_the_account_turns_it_off(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """私人记忆 3a: an active memory rides every model turn as reference-only context, right after
    the account-settings line; chat.memory off removes it entirely (and refuses new chat memories)."""
    assert (await native_client.post("/api/memory", json={
        "layer": "profile", "text": "只做螺纹，别的品种先问一句", "via": "settings"})).status_code == 201
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages", json={"text": "螺纹怎么看"})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    assert _model_saw(responses_server, "关于这位用户的记忆（只作参考，不构成任何授权")
    assert _model_saw(responses_server, "只做螺纹，别的品种先问一句")

    assert (await native_client.put("/api/preferences", json={"chat": {"memory": False}})).status_code == 200
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages", json={"text": "螺纹怎么看"})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    assert not _model_saw(responses_server, "关于这位用户的记忆")


async def test_backtest_tool_cancellation_releases_the_lease_and_the_fifo(
    native_client: AsyncClient, native_application: FastAPI, monkeypatch,
):
    repo_root = Path(__file__).resolve().parents[3]
    shutil.copytree(repo_root / "workspace",
                    native_application.state.adapter.workspace.resolved_root / "workspace", dirs_exist_ok=True)
    entered = threading.Event()

    def blocking_execute(*, lease, **_kwargs):
        entered.set()
        while True:
            lease.check()
            time.sleep(0.01)

    monkeypatch.setattr(native_application.state.backtest_service, "_execute", blocking_execute)
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "执行可取消回测")
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=3)
    assert native_application.state.backtest_coordinator.snapshot()["jobs"] == 1
    cancelled = await native_client.post(f"/api/runs/{run_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert (await wait_for_run(native_application, run_id, {"cancelled"}))["status"] == "cancelled"
    assert native_application.state.backtest_coordinator.snapshot()["jobs"] == 0
    with pytest.raises(KeyError):
        native_application.state.store.latest_backtest_run(session_id=session_id, user_id="local-user")
    next_run = await start_run(native_client, session_id, "取消回测后继续只读任务")
    assert (await wait_for_run(native_application, next_run))["status"] == "completed"


# --- streaming, cancellation, failures -------------------------------------

async def test_streaming_deltas_are_coalesced_before_persistence(
    native_client: AsyncClient, native_application: FastAPI,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "生成大量流式输出")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    events = events_of(native_application, session_id)
    assert len([event for event in events if event["name"] == "message.delta"]) <= 2
    assert any(event["name"] == "message.completed" and "合并完成" in event["payload"]["text"] for event in events)


async def test_reasoning_summary_streams_into_the_task_card_never_into_the_answer(
    native_client: AsyncClient, native_application: FastAPI,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "带推理摘要")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    events = events_of(native_application, session_id)
    summaries = [event["payload"]["summary"] for event in events if event["kind"] == "progress"]
    assert "先核对账户，再看盘面。" in summaries[-1]
    assert assistant_answers(native_application, session_id)[-1] == "推理摘要之后的回答。"


async def test_cancel_closes_the_stream_and_releases_the_fifo(
    native_client: AsyncClient, native_application: FastAPI,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "永不返回")
    await asyncio.sleep(0.1)
    cancelled = await native_client.post(f"/api/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert (await wait_for_run(native_application, run_id, {"cancelled"}))["status"] == "cancelled"
    completed = (await wait_for_event(native_application, session_id, "task.completed", taskId=run_id))["payload"]
    assert completed["status"] == "cancelled"
    # The streamed placeholder turn is closed with a final message, so no client keeps it "answering"
    # (1.6.5 web report: 新对话 / 历史对话 stayed disabled after 停止).
    turn_id = completed["turnId"]
    await wait_for_event(native_application, session_id, "message.completed", turnId=turn_id)
    events = events_of(native_application, session_id)
    deltas = [e for e in events if e["name"] == "message.delta" and e["payload"].get("turnId") == turn_id]
    finals = [e for e in events if e["name"] == "message.completed" and e["payload"].get("turnId") == turn_id]
    assert deltas and finals and finals[-1]["payload"]["text"] == "已停止。"
    next_run = await start_run(native_client, session_id, "取消后继续")
    assert (await wait_for_run(native_application, next_run))["status"] == "completed"


async def test_a_cancelled_question_is_not_replayed_to_the_model(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "永不返回")
    await asyncio.sleep(0.1)
    await native_client.post(f"/api/runs/{run_id}/cancel")
    assert (await wait_for_run(native_application, run_id, {"cancelled"}))["status"] == "cancelled"
    next_run = await start_run(native_client, session_id, "取消后继续")
    assert (await wait_for_run(native_application, next_run))["status"] == "completed"
    texts = [item.get("content") for item in responses_server.requests[-1]["input"] if item.get("role") == "user"]
    assert len(texts) == 1 and texts[0].endswith("取消后继续") and "永不返回" not in texts[0]  # the turn's clock line rides in front
    # The same holds after a restart that rebuilds history from persisted events.
    native_application.state.adapter.forget_history()
    third = await start_run(native_client, session_id, "第二轮引用上一轮")
    assert (await wait_for_run(native_application, third))["status"] == "completed"
    roles = [item.get("role") for item in responses_server.requests[-1]["input"] if isinstance(item, dict)]
    assert roles == ["user", "assistant", "user"]


async def test_turn_timeout_fails_the_run_and_keeps_the_adapter_usable(
    native_client: AsyncClient, native_application: FastAPI,
):
    native_application.state.adapter.turn_timeout_seconds = 0.05
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "永不返回终态")
    assert (await wait_for_run(native_application, run_id))["status"] == "failed"
    events = events_of(native_application, session_id)
    task_events = [event for event in events if event["name"] == "task.completed" and event["payload"]["taskId"] == run_id]
    assert task_events[-1]["payload"]["status"] == "failed"
    assert any(event["name"] == "error" and event["payload"].get("runId") == run_id
               and event["payload"]["code"] == "agent_turn_timeout" and event["payload"]["scope"] == "task"
               for event in events)
    native_application.state.adapter.turn_timeout_seconds = 30
    next_run = await start_run(native_client, session_id, "超时后继续")
    assert (await wait_for_run(native_application, next_run))["status"] == "completed"


class _TimeoutOnlyAfterTheToolStarts:
    """`asyncio.timeout` double for the drain test below: the turn's deadline passes when the
    blocking tool announces it has started, not on a wall-clock budget. A fixed budget races the
    turn's setup (history, first model round, tool dispatch, backtest preamble) under parallel
    load — CI 2026-10-08 saw it expire before `_execute` was entered, the run then failed
    correctly while the tool had never run. Arming on entry keeps the timeout landing on a
    running tool in every run, whatever the machine's load."""

    def __init__(self, started: asyncio.Event) -> None:
        self._started = started
        self._task: asyncio.Task | None = None
        self._watcher: asyncio.Task | None = None
        self._fired = False

    async def __aenter__(self) -> _TimeoutOnlyAfterTheToolStarts:
        self._task = asyncio.current_task()

        async def fire() -> None:
            await self._started.wait()
            self._fired = True
            if self._task is not None:
                self._task.cancel()

        self._watcher = asyncio.create_task(fire())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
            self._watcher = None
        # Same conversion the stdlib performs: a cancellation this double caused leaves the
        # timeout handler in native_agent as a TimeoutError, exactly like a real deadline.
        if (self._fired and exc_type is asyncio.CancelledError
                and self._task is not None and self._task.cancelling() > 0):
            self._task.uncancel()
            raise TimeoutError from exc
        return False


async def test_turn_timeout_drains_a_running_tool_before_the_run_closes(
    native_client: AsyncClient, native_application: FastAPI, monkeypatch,
):
    repo_root = Path(__file__).resolve().parents[3]
    shutil.copytree(repo_root / "workspace",
                    native_application.state.adapter.workspace.resolved_root / "workspace", dirs_exist_ok=True)
    started, finished = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def blocking_execute(*, lease, **_kwargs):
        loop.call_soon_threadsafe(started.set)
        try:
            while True:
                lease.check()
                time.sleep(0.01)
        finally:
            finished.set()

    monkeypatch.setattr(native_application.state.backtest_service, "_execute", blocking_execute)
    native_application.state.adapter.turn_timeout_seconds = 0.3
    real_timeout = asyncio.timeout

    def gated_timeout(delay: float):
        # Only the turn's own budget is replaced; every other asyncio.timeout user
        # (research desk, side channels, guests) keeps the real implementation.
        if delay != native_application.state.adapter.turn_timeout_seconds:
            return real_timeout(delay)
        return _TimeoutOnlyAfterTheToolStarts(started)

    monkeypatch.setattr(asyncio, "timeout", gated_timeout)
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "执行可取消回测")
    run = await wait_for_run(native_application, run_id)
    assert run["status"] == "failed"
    assert finished.is_set()
    assert native_application.state.backtest_coordinator.snapshot()["jobs"] == 0
    assert native_application.state.adapter._tool_tasks == {}
    errors = [event for event in events_of(native_application, session_id) if event["name"] == "error"]
    assert errors[-1]["payload"]["code"] == "agent_turn_timeout"


async def test_http_error_from_the_model_fails_the_task_not_the_runtime(
    native_client: AsyncClient, native_application: FastAPI,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "服务端错误")
    assert (await wait_for_run(native_application, run_id))["status"] == "failed"
    events = events_of(native_application, session_id)
    errors = [event for event in events if event["name"] == "error"]
    assert errors[-1]["payload"]["code"] == "model_request_failed"
    assert errors[-1]["payload"]["scope"] == "task"
    assert "exploded" not in errors[-1]["payload"]["message"]
    assert native_application.state.adapter.runtime_status("local")["agentConnected"] is True
    next_run = await start_run(native_client, session_id, "失败后继续")
    assert (await wait_for_run(native_application, next_run))["status"] == "completed"


async def test_interrupted_stream_fails_the_run_and_unblocks_the_fifo(
    native_client: AsyncClient, native_application: FastAPI,
):
    session_id = await create_session(native_client)
    first = await start_run(native_client, session_id, "流中断")
    second = await start_run(native_client, session_id, "第二个排队任务继续执行")
    assert (await wait_for_run(native_application, first))["status"] == "failed"
    assert (await wait_for_run(native_application, second))["status"] == "completed"
    events = events_of(native_application, session_id)
    assert any(event["name"] == "task.completed" and event["payload"]["taskId"] == first
               and event["payload"]["status"] == "failed" for event in events)
    assert any(event["name"] == "error" and event["payload"].get("runId") == first
               and event["payload"]["code"] == "model_stream_interrupted" for event in events)


async def test_truncated_output_is_shown_with_a_warning(
    native_client: AsyncClient, native_application: FastAPI,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "超长输出")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    events = events_of(native_application, session_id)
    assert assistant_answers(native_application, session_id)[-1].startswith("这段回答被模型输出上限截断")
    assert any(event["name"] == "warning" and "截断" in event["payload"]["message"] for event in events)


# --- history, usage, models --------------------------------------------------

async def test_conversation_history_is_carried_into_the_next_request(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    first = await start_run(native_client, session_id, "第一轮问题")
    assert (await wait_for_run(native_application, first))["status"] == "completed"
    second = await start_run(native_client, session_id, "第二轮引用上一轮")
    assert (await wait_for_run(native_application, second))["status"] == "completed"
    assert assistant_answers(native_application, session_id)[-1] == "上一轮我说过：已完成只读分析。"
    roles = [item.get("role") for item in responses_server.requests[-1]["input"] if isinstance(item, dict)]
    assert roles == ["user", "assistant", "user"]


async def test_workspace_context_and_tool_items_are_sent_only_with_their_own_turn(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    first = await start_context_run(native_client, session_id, "请读取Trader数据")
    assert (await wait_for_run(native_application, first))["status"] == "completed"
    first_request = responses_server.requests[0]["input"]
    assert "quoteDatetime" in json.dumps(first_request[-1], ensure_ascii=False)
    second = await start_run(native_client, session_id, "第二轮引用上一轮")
    assert (await wait_for_run(native_application, second))["status"] == "completed"
    second_request = responses_server.requests[-1]["input"]
    # The first turn's snapshot and routing instructions are gone from history;
    # its tool traffic survives exactly one more turn (a follow-up may refer to it).
    assert "quoteDatetime" not in json.dumps(second_request, ensure_ascii=False)
    assert "AI_TRADER_FUTURES_CONTEXT" not in json.dumps(second_request, ensure_ascii=False)
    assert second_request[0] == {"role": "user", "content": "请读取Trader数据"}
    assert [item.get("type") for item in second_request if item.get("type")] == ["reasoning", "function_call", "function_call_output"]
    third = await start_run(native_client, session_id, "第三轮")
    assert (await wait_for_run(native_application, third))["status"] == "completed"
    third_request = responses_server.requests[-1]["input"]
    assert [item.get("type") for item in third_request if item.get("type")] == []
    assert [item.get("role") for item in third_request] == ["user", "assistant", "user", "assistant", "user"]


async def test_history_is_rebuilt_from_persisted_events_after_a_restart(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    first = await start_run(native_client, session_id, "第一轮问题")
    assert (await wait_for_run(native_application, first))["status"] == "completed"
    native_application.state.adapter.forget_history()
    second = await start_run(native_client, session_id, "第二轮引用上一轮")
    assert (await wait_for_run(native_application, second))["status"] == "completed"
    assert assistant_answers(native_application, session_id)[-1] == "上一轮我说过：已完成只读分析。"


async def test_token_usage_is_recorded_from_the_response_usage(
    native_client: AsyncClient, native_application: FastAPI,
):
    from datetime import UTC, datetime
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "只做只读策略分析")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    day = datetime.now(UTC).date().isoformat()
    assert native_application.state.store.token_usage("local-user", day) == 160


async def test_each_turn_records_its_requests_and_token_split(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """Token 计量 (2026-10-14): one agent_turn_usage row per turn — the number of model requests and
    the input / output / cache-hit / reasoning tokens summed over them, as the provider reported."""
    session_id = await create_session(native_client)
    before = len(responses_server.requests)
    run_id = await start_run(native_client, session_id, "只做只读策略分析")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    await wait_for_event(native_application, session_id, "agent.result")
    sent = len(responses_server.requests) - before
    rows = native_application.state.store.turn_usage(user_id="local-user")
    assert [row["run_id"] for row in rows] == [run_id]
    row = rows[0]
    tool_rounds = sent - 1  # every round before the answer called a tool
    assert row["requests"] == sent >= 1
    assert row["input_tokens"] == 100 * tool_rounds + 120
    assert row["output_tokens"] == 20 * tool_rounds + 40
    assert row["cached_tokens"] == 64 * tool_rounds + 96
    assert row["reasoning_tokens"] == 5 * tool_rounds
    assert row["status"] == "completed" and row["route"] == "general" and row["session_id"] == session_id
    assert row["first_text_ms"] is not None and row["elapsed_ms"] >= row["first_text_ms"]


async def test_a_failed_answer_publish_corrects_the_usage_status(
    native_client: AsyncClient, native_application: FastAPI, monkeypatch,
):
    """#866: the usage row is written before the answer is published; a publish that raises must not
    leave "completed" in the table."""
    adapter = native_application.state.adapter

    async def broken(*args, **kwargs):
        raise RuntimeError("publish failed")

    monkeypatch.setattr(adapter, "_publish_answer", broken)
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "只做只读策略分析")
    await wait_for_run(native_application, run_id)
    rows = native_application.state.store.turn_usage(user_id="local-user")
    assert [(row["run_id"], row["status"]) for row in rows] == [(run_id, "failed")]


async def test_missing_usage_falls_back_to_a_character_estimate(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    server = FakeResponsesServer(include_usage=False)
    application = create_app(
        tmp_path / "usage.sqlite3", local_token=TOKEN, agent_mode="native", agent_provider="deepseek",
        deepseek_api_key=DEEPSEEK_KEY, native_transport=server.transport(),
        engineering_root=tmp_path / "ai-trader-project",
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test",
                               headers={"origin": ORIGIN}) as client:
            await client.post("/api/auth/local", headers={"authorization": f"Bearer {TOKEN}", "origin": ORIGIN})
            session_id = await create_session(client)
            run_id = await start_run(client, session_id, "只做只读策略分析")
            assert (await wait_for_run(application, run_id))["status"] == "completed"
            from datetime import UTC, datetime
            assert application.state.store.token_usage("local-user", datetime.now(UTC).date().isoformat()) >= 1
    finally:
        await application.state.runs.shutdown()
        await application.state.adapter.shutdown()
        await application.state.broker.shutdown()
        application.state.store.close()


async def test_each_model_uses_its_own_endpoint_and_key(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    models = await native_client.get("/api/models")
    assert {entry["slug"] for entry in models.json()["models"]} == {"deepseek-flash", "glm-5.3-flash", KIMI_MODEL}
    switched = await native_client.put("/api/models/preference", json={"model": "glm-5.3-flash"})
    assert switched.status_code == 200 and switched.json()["current"] == "glm-5.3-flash"
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "只做只读策略分析")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert responses_server.requests[-1]["model"] == "glm-5.3-flash"
    assert responses_server.authorizations[-1] == f"Bearer {ZHIPU_KEY}"
    clients = native_application.state.adapter.model_clients
    assert str(clients["glm-5.3-flash"].base_url).startswith("https://open.bigmodel.cn/api/v1")
    assert str(clients["deepseek-flash"].base_url).startswith("https://api.deepseek.com")


async def test_kimi_member_selection_and_structured_output_use_its_own_key(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    adapter = native_application.state.adapter
    assert str(adapter.model_clients[KIMI_MODEL].base_url).startswith("https://api.kimi.com/coding/v1")
    catalog = (await native_client.get("/api/models")).json()
    entry = next(entry for entry in catalog["models"] if entry["slug"] == KIMI_MODEL)
    assert entry["displayName"] == "Kimi K3.1 Pro" and entry["memberOnly"] and entry["selectable"]
    chosen = await native_client.put("/api/models/preference", json={"model": KIMI_MODEL})
    assert chosen.status_code == 200 and chosen.json()["current"] == KIMI_MODEL
    before = len(responses_server.requests)
    result = await adapter.complete_structured(
        instructions="只返回摘要", input_text="{}", schema=ModelSummary, timeout_seconds=5, model=KIMI_MODEL,
        max_output_tokens=6000, light_reasoning=True)
    assert result.value.summary
    sent = responses_server.requests[before]
    assert sent["model"] == "k3.1-pro" and sent["reasoning"]["effort"] == "low"
    assert responses_server.authorizations[before] == f"Bearer {KIMI_KEY}"


async def test_legacy_model_preference_maps_on_read_without_rewriting(
    native_client: AsyncClient, native_application: FastAPI,
):
    store = native_application.state.store
    store.set_model_pref("local-user", "gpt-6-astra")
    assert (await native_client.get("/api/models")).json()["current"] == KIMI_MODEL
    assert store.connection.execute("SELECT model_slug FROM user_model_prefs WHERE user_id = ?",
                                    ("local-user",)).fetchone()[0] == "gpt-6-astra"
    assert native_application.state.adapter._endpoint_for("gpt-6-astra").slug == KIMI_MODEL


async def test_a_stored_model_preference_without_a_key_fails_instead_of_switching_models(
    native_application: FastAPI,
):
    from trader_runtime.native_agent import NativeAgentError
    adapter = native_application.state.adapter
    adapter.endpoints.pop(KIMI_MODEL)
    with pytest.raises(NativeAgentError, match="model_unavailable"):
        adapter._endpoint_for("gpt-6-astra")
    assert adapter._endpoint_for(None).slug == "deepseek-flash"
    payload = adapter.error_payload(NativeAgentError("model_unavailable"), "run")
    assert "重新选择模型" in payload["message"] and payload["scope"] == "task"


async def test_api_keys_never_leak_into_events_or_logs(
    native_client: AsyncClient, native_application: FastAPI, caplog,
):
    caplog.set_level(logging.INFO)
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "请读取Trader数据")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    serialized = repr(events_of(native_application, session_id))
    assert DEEPSEEK_KEY not in serialized and ZHIPU_KEY not in serialized
    assert DEEPSEEK_KEY not in caplog.text and ZHIPU_KEY not in caplog.text
    for endpoint in native_application.state.adapter.endpoints.values():
        assert endpoint.api_key not in repr(endpoint) and endpoint.api_key not in str(endpoint)
    assert any("agent_turn run_id=" in record.message and "model=deepseek-flash" in record.message
               and "status=" in record.message for record in caplog.records)


async def test_resuming_a_persisted_session_publishes_runtime_status(
    native_client: AsyncClient, native_application: FastAPI,
):
    session_id = await create_session(native_client)
    response = await native_client.get(f"/api/sessions/{session_id}")
    assert response.status_code == 200
    statuses = [event for event in events_of(native_application, session_id) if event["name"] == "runtime.status"]
    assert statuses[-1]["payload"]["connected"] is True
    assert statuses[-1]["payload"]["adapter"] == "native-agent"


async def test_structured_completion_has_no_tools_and_returns_the_schema(
    native_application: FastAPI, responses_server: FakeResponsesServer,
):
    adapter = native_application.state.adapter
    result = await adapter.complete_structured(
        instructions="只返回摘要", input_text="{\"event\": 1}", schema=ModelSummary, timeout_seconds=5,
    )
    assert result.value.summary == "成交来自策略信号，价格与记录一致。"
    request = responses_server.requests[-1]
    assert request.get("tools") in (None, [])
    assert request["text"]["format"]["type"] == "json_schema"


async def test_structured_completion_counts_tokens_against_the_adapters_user(
    native_application: FastAPI, responses_server: FakeResponsesServer,
):
    from datetime import UTC, datetime
    adapter = native_application.state.adapter
    store = native_application.state.store
    day = datetime.now(UTC).date().isoformat()
    # A shared (unowned) adapter reports the tokens but records nothing.
    result = await adapter.complete_structured(
        instructions="只返回摘要", input_text="{}", schema=ModelSummary, timeout_seconds=5)
    assert result.tokens == 160 and store.token_usage("background-user", day) == 0
    # On the shared adapter (owner-password mode) the caller names the user.
    await adapter.complete_structured(
        instructions="只返回摘要", input_text="{}", schema=ModelSummary, timeout_seconds=5, usage_user="caller-user")
    assert store.token_usage("caller-user", day) == 160
    # A per-user adapter (the cloud router sets usage_user) records them for that user.
    adapter.usage_user = "background-user"
    try:
        await adapter.complete_structured(
            instructions="只返回摘要", input_text="{}", schema=ModelSummary, timeout_seconds=5)
    finally:
        adapter.usage_user = None
    assert store.token_usage("background-user", day) == 160


async def test_structured_completion_refuses_a_tool_outside_its_allowlist(
    native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
):
    from trader_runtime.market_briefs import NewsCandidate
    from trader_runtime.native_agent import NativeAgentError
    adapter = native_application.state.adapter
    dispatched = AsyncMock()
    monkeypatch.setattr(adapter.trader_tools, "call_async", dispatched)
    with pytest.raises(NativeAgentError, match="unexpected_tool"):
        await adapter.complete_structured(
            instructions="越权调用 只找新闻", input_text="{}", schema=NewsCandidate,
            timeout_seconds=5, tools={"web_search"},
        )
    dispatched.assert_not_awaited()
    assert len(responses_server.requests) == 1


async def test_briefs_retrieval_uses_the_shared_search_entry(
    native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
):
    """Brief retrieval uses the shared search function, without a provider-specific engine."""
    from trader_runtime.market_briefs import NO_NEWS, collect_news
    adapter = native_application.state.adapter
    engines: list[str | None] = []

    class _StubSearch:
        async def search(self, query, *, count=5, engine=None):
            engines.append(engine)
            return {"query": query, "results": [], "searchedAt": "2026-10-08T04:00:00+00:00"}

    monkeypatch.setattr(adapter.trader_tools, "web_search", _StubSearch())
    result = await collect_news(adapter, "SHFE.rb2610", "螺纹钢")
    # The fixture's NewsCandidate scenario searches once, then answers empty excerpt — an honest blank
    # (the one outcome that withdraws a stored story).
    assert result == NO_NEWS
    assert engines == [None]


async def test_an_empty_answer_after_a_failed_search_is_inconclusive(
    native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
):
    """#788：检索本身失败（工具输出 error）时模型交回的空结论不是“确实没有新闻”，不撤下已核实的简报。"""
    from trader_runtime.market_briefs import collect_news
    from trader_runtime.web_search import WebSearchError
    adapter = native_application.state.adapter

    class _BrokenSearch:
        async def search(self, query, *, count=5, engine=None):
            raise WebSearchError("web_search_timeout")

    monkeypatch.setattr(adapter.trader_tools, "web_search", _BrokenSearch())
    assert await collect_news(adapter, "SHFE.rb2610", "螺纹钢") is None


async def test_structured_completion_corrects_an_invalid_reply_once_then_gives_up(
    native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """保护：结构化输出没通过校验时，把校验问题交给模型改一次（10-08 一句话建策略常因一个越界字段整份作废）；
    改了还不对才报 model_output_invalid。"""
    from trader_runtime.native_agent import NativeAgentError
    adapter = native_application.state.adapter
    before = len(responses_server.requests)
    result = await adapter.complete_structured(
        instructions="先交错 只返回摘要", input_text="{}", schema=ModelSummary, timeout_seconds=5)
    assert result.value.summary == "成交来自策略信号，价格与记录一致。"
    sent = responses_server.requests[before:]
    assert len(sent) == 2 and "没有通过校验" in json.dumps(sent[1]["input"][-1], ensure_ascii=False)
    with pytest.raises(NativeAgentError, match="model_output_invalid"):
        await adapter.complete_structured(
            instructions="一直交错 只返回摘要", input_text="{}", schema=ModelSummary, timeout_seconds=5)
    assert len(responses_server.requests) == before + 4


async def test_structured_completion_runs_light_and_reports_a_cut_off_reply_as_empty(
    native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """保护（10-12 线上研讨）：DeepSeek 的思考也算在 max_output_tokens 里，1500 被推理吃满，正文为空，
    两次都空却报成 model_output_invalid。填表式调用（light_reasoning）在 DeepSeek 上以不思考（effort
    none）运行并带上上限；仍被截断或为空时报 model_output_empty，与格式不合法分开。"""
    from trader_runtime.native_agent import NativeAgentError
    adapter = native_application.state.adapter
    before = len(responses_server.requests)
    result = await adapter.complete_structured(
        instructions="只返回摘要", input_text="{}", schema=ModelSummary, timeout_seconds=5,
        max_output_tokens=6000, light_reasoning=True)
    assert result.value.summary
    sent = responses_server.requests[before]
    assert sent["reasoning"]["effort"] == "none" and sent["max_output_tokens"] == 6000
    with pytest.raises(NativeAgentError, match="model_output_empty"):
        await adapter.complete_structured(
            instructions="推理吃满额度 只返回摘要", input_text="{}", schema=ModelSummary, timeout_seconds=5,
            max_output_tokens=1500)
    # Without light_reasoning the deployment's own effort applies (unchanged for other callers).
    assert responses_server.requests[-1]["reasoning"]["effort"] != "none"


async def test_a_rate_limit_inside_the_stream_is_reported_as_a_rate_limit(
    native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """GLM 的 429 在流式请求里是 HTTP 200 加一条 response.failed（error.code rate_limit_exceeded）：
    要报 model_rate_limited（研讨记录写“限流”），不是笼统的 model_request_failed。"""
    from trader_runtime.native_agent import NativeAgentError
    with pytest.raises(NativeAgentError, match="^model_rate_limited$"):
        await native_application.state.adapter.complete_structured(
            instructions="只返回摘要", input_text="流内限流", schema=ModelSummary, timeout_seconds=5, model="glm-5.3-flash")


def test_kimis_overload_error_event_is_a_rate_limit():
    """Kimi 过载（10-24）：流里是一条 type=error 的事件，错误本身挂在 event.error 上（code rate_limit_exceeded，
    “The engine is currently overloaded”）——要报 model_rate_limited，不是笼统的请求失败或断流。"""
    from types import SimpleNamespace

    from trader_runtime.native_agent import NativeAgentAdapter
    event = SimpleNamespace(type="error", error={"type": "rate_limit_exceeded", "code": "rate_limit_exceeded",
                                                "message": "The engine is currently overloaded, please try again later"})
    assert NativeAgentAdapter._stream_failure(event) == "model_rate_limited"


def test_structured_replies_are_parsed_leniently():
    from trader_runtime.native_agent import extract_json_object
    assert extract_json_object('```json\n{"summary": "a"}\n```') == '{"summary": "a"}'
    assert extract_json_object('结论如下：{"summary": "a"} 完') == '{"summary": "a"}'
    assert extract_json_object('没有 JSON') == '没有 JSON'


def test_local_default_mode_is_native_and_makes_no_request_at_startup(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AI_TRADER_AGENT_MODE", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    application = create_app(tmp_path / "default.sqlite3", local_token=TOKEN, engineering_root=tmp_path / "project")
    assert application.state.agent_mode == "native"
    assert application.state.adapter.adapter_id == "native-agent"
    application.state.store.close()


def test_developer_instructions_keep_the_trading_and_presentation_contract(tmp_path: Path):
    workspace = WorkspacePolicy(tmp_path / "project")
    workspace.prepare()
    instructions = workspace.developer_instructions()
    for fragment in (
        "Never patch source files, install dependencies, execute arbitrary commands",
        "authenticated product APIs", "Never retry an UNKNOWN order",
        "get_futures_decision_context", "结论, 为什么, 风险, 现在怎么做",
        "never expose tool names, JSON keys, enum values",
        "Separate observed facts, reasoned judgment, and unknowns",
        # Card and reply brevity (owner, 2026-09-18): no boilerplate, no headings, tables only for ≥2 rows,
        # at most one sentence beside a card.
        "never open with 我已为你", "no headings", "at least two rows of data",
        "at most one short sentence that does not repeat the card's fields",
        # Chart questions take the deterministic tools; annotations only on request, once per turn.
        "must not call get_futures_decision_context", "Call annotate_chart only when the user asks",
        "once per turn with every annotation",
    ):
        assert fragment in instructions


# --- voice turns: same session, spoken summary, fast path ----------------------------


async def test_a_voice_turn_joins_the_session_and_carries_a_spoken_summary(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    # A typed turn first, then a spoken one: both land in the same conversation.
    typed = await start_run(native_client, session_id, "只做只读策略分析")
    assert (await wait_for_run(native_application, typed))["status"] == "completed"
    response = await native_client.post(f"/api/sessions/{session_id}/messages", json={"text": "请生成交易候选：沪金空一手", "origin": "voice"})
    assert response.status_code == 202
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    events = events_of(native_application, session_id)
    users = [e["payload"] for e in events if e["name"] == "message.completed" and e["payload"]["role"] == "user"]
    assert "origin" not in users[0] and users[1]["origin"] == "voice"
    answers = [e["payload"] for e in events if e["name"] == "message.completed" and e["payload"].get("role") == "assistant" and e["payload"].get("turnId")]
    assert "origin" not in answers[0]
    # A short answer is read as written (markdown stripped), with no second model request.
    assert answers[-1]["origin"] == "voice" and answers[-1]["spoken"] == "模拟交易方案已生成，正在等待你确认；尚未发送任何委托"
    assert all("改成中文口语" not in str(request.get("instructions") or "") for request in responses_server.requests)
    # A long answer is not condensed any more (V1.1): the voice reads the first paragraph as written; the
    # details after the blank line stay in the conversation. No second model request.
    requests_before = len(responses_server.requests)
    long_turn = await native_client.post(f"/api/sessions/{session_id}/messages", json={"text": "长回答：你还有什么能力", "origin": "voice"})
    assert (await wait_for_run(native_application, long_turn.json()["runId"]))["status"] == "completed"
    long_answer = [e["payload"] for e in events_of(native_application, session_id) if e["name"] == "message.completed" and e["payload"].get("role") == "assistant" and e["payload"].get("turnId")][-1]
    assert long_answer["spoken"] == "结论：能力很多，先说三条。第一，看行情。第二，管策略。第三，下模拟单"
    assert "第1项" in long_answer["text"] and len(responses_server.requests) == requests_before + 1
    # Every voice turn runs on the low reasoning tier; the voice-turn instruction asks for a spoken first paragraph.
    assert responses_server.requests[-1]["reasoning"]["effort"] == "minimal"
    assert any("第一段用口语直接回答" in json.dumps(request["input"], ensure_ascii=False) for request in responses_server.requests[1:])
    assert [e for e in events if e["name"] == "proposal.trade"][0]["payload"]["status"] == "approval_required"


async def test_a_voice_fast_query_skips_the_gate_and_runs_read_only_with_minimal_reasoning(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages", json={"text": "现在螺纹钢价格多少", "origin": "voice"})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    turn_request = responses_server.requests[0]
    assert turn_request["reasoning"]["effort"] == "minimal"
    names = {tool["name"] for tool in turn_request["tools"]}
    assert CORE_TOOLS <= names and not names & {"execute_simulation_trade", "propose_order", "cancel_simulation_order", "start_strategy_monitoring"}
    assert "本轮问题可能与当前行情或新闻相关" not in json.dumps(turn_request["input"], ensure_ascii=False)
    events = events_of(native_application, session_id)
    assert not [e for e in events if e["name"] == "warning"]
    # The same question typed is routed as a plain market query: the narrowed read tools (no card, no
    # web), minimal reasoning and temperature 0 — same tools every run.
    typed = await start_run(native_client, session_id, "现在螺纹钢价格多少")
    assert (await wait_for_run(native_application, typed))["status"] == "completed"
    typed_request = responses_server.requests[-1]
    assert typed_request["reasoning"]["effort"] == "minimal" and typed_request["temperature"] == 0.0
    assert {tool["name"] for tool in typed_request["tools"]} == set(MARKET_QUERY_TOOLS)


async def test_a_sure_order_spoken_is_carded_without_the_voice_fast_path(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“持仓都平了” spoken: 持仓 is a fast-path word, but the sentence is a sure 全平 (09-20 eval order-24: the fast
    flag once let the turn browse app_call / get_positions instead). 意图重构第 3 步: the planner makes the batch card
    with no model round, and the spoken line is the card's own words."""
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages", json={"text": "持仓都平了", "origin": "voice"})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    assert responses_server.requests == []
    events = events_of(native_application, session_id)
    cards = [event["payload"] for event in events if event["name"] == "proposal.close_batch"]
    assert len(cards) == 1 and cards[0]["status"] == "approval_required"
    answer = [event["payload"] for event in events if event["name"] == "message.completed"
              and event["payload"].get("role") == "assistant"][-1]
    assert answer["origin"] == "voice" and answer["spoken"] == answer["text"] and answer["text"].endswith("确认吗？")
    assert answer["turnId"] == cards[0]["turnId"]  # the card and its line are one turn (chat:send collects both)


async def test_the_voice_reads_a_bounded_first_paragraph_and_a_follow_up_carries_its_instruction(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages", json={"text": "口语失败也要回答", "origin": "voice"})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    answers = [e["payload"] for e in events_of(native_application, session_id)
               if e["name"] == "message.completed" and e["payload"].get("role") == "assistant" and e["payload"].get("turnId")]
    # A first paragraph longer than the guard is cut at a sentence end, never mid-number.
    assert answers[-1]["text"].startswith("口语失败：已完成只读分析。") and answers[-1]["origin"] == "voice"
    assert answers[-1]["spoken"].startswith("口语失败：已完成只读分析。") and 200 < len(answers[-1]["spoken"]) <= 400
    # The user spoke again while that answer was being read: the next turn says so to the model.
    follow = await native_client.post(f"/api/sessions/{session_id}/messages", json={"text": "那 MACD 呢", "origin": "voice", "followUp": True})
    assert (await wait_for_run(native_application, follow.json()["runId"]))["status"] == "completed"
    assert "优先回答这句" in json.dumps(responses_server.requests[-1]["input"], ensure_ascii=False)
    users = [e["payload"] for e in events_of(native_application, session_id) if e["name"] == "message.completed" and e["payload"].get("role") == "user"]
    assert users[-1]["text"] == "那 MACD 呢"


async def test_turn_text_listeners_see_every_delta_before_the_coalescer(
    native_client: AsyncClient, native_application: FastAPI,
):
    session_id = await create_session(native_client)
    seen: list[dict] = []
    unsubscribe = native_application.state.adapter.subscribe_turn_text(session_id, seen.append)
    response = await native_client.post(f"/api/sessions/{session_id}/messages", json={"text": "现在螺纹钢价格多少", "origin": "voice"})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    unsubscribe()
    assert seen[0]["role"] == "user" and seen[0]["text"] == "现在螺纹钢价格多少" and seen[0]["final"] is True
    answers = [item for item in seen if item["role"] == "assistant"]
    assert answers and answers[-1]["final"] is True and answers[-1]["text"]
    assert len({item["turnId"] for item in answers}) == 1 and all(item["text"] for item in answers)
    assert not [item for item in answers[:-1] if item["final"]]
    # Unsubscribed: a later turn reaches nobody.
    again = await native_client.post(f"/api/sessions/{session_id}/messages", json={"text": "现在螺纹钢价格多少", "origin": "voice"})
    assert (await wait_for_run(native_application, again.json()["runId"]))["status"] == "completed"
    assert seen[-1]["final"] is True and len({item["turnId"] for item in seen if item["role"] == "assistant"}) == 1


async def test_the_next_turn_learns_that_the_last_proposal_card_was_abandoned(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """History keeps only question and answer; the model read its own "等你确认" and refused to make a new
    card after the user (or the voice timeout) rejected the old one (1.6.0 acceptance, 2026-09-18)."""
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages", json={"text": "请生成交易候选：沪金空一手", "origin": "voice"})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    card = [e for e in events_of(native_application, session_id) if e["name"] == "proposal.trade"][0]["payload"]
    # Neither pending nor rejected cards are secrets to the next turn.
    second = await start_run(native_client, session_id, "再来一次")
    assert (await wait_for_run(native_application, second))["status"] == "completed"
    request_text = json.dumps(responses_server.requests[-1]["input"], ensure_ascii=False)
    assert "仍在等待用户确认" in request_text and "等待确认" in request_text
    rejected = await native_client.post(f"/api/proposals/{card['proposalId']}/reject",
                                        json={"proposalVersion": card["version"], "idempotencyKey": "abandon-1"})
    assert rejected.status_code == 200
    third = await start_run(native_client, session_id, "再出一张同样的")
    assert (await wait_for_run(native_application, third))["status"] == "completed"
    request_text = json.dumps(responses_server.requests[-1]["input"], ensure_ascii=False)
    assert "都已结束" in request_text and "已放弃或超时" in request_text and "必须重新调用工具生成新方案卡" in request_text
    # The instruction names the one card tool and forbids creating cards through app_call.
    assert "方案卡只能由 propose_order 产生" in json.dumps(responses_server.requests[0]["input"], ensure_ascii=False)



# --- history compaction (外审 09-19 A7) -------------------------------------------------------------


def _tool_turns(count: int, *, filler: int = 20_000) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for index in range(count):
        items.extend([
            {"role": "user", "content": f"第 {index} 个问题 " + "话" * filler},
            {"type": "function_call", "call_id": f"call-{index}", "name": "get_market_snapshot", "arguments": "{}"},
            {"type": "function_call_output", "call_id": f"call-{index}", "output": "{}"},
            {"role": "assistant", "content": f"第 {index} 个回答"},
        ])
    return items


async def test_compaction_moves_whole_turns_instead_of_splitting_a_tool_call(
    native_application: FastAPI, monkeypatch,
):
    """The audit's reproduction: item-by-item trimming stopped between a function_call and its output,
    leaving a lone function_call_output the wire rejects. Compaction now moves whole turn groups."""
    adapter = native_application.state.adapter
    session_id = "compaction-session"
    adapter._history[session_id] = _tool_turns(4)

    async def summary(endpoint, *, instructions, input_text, **_):
        return "四轮问答的摘要"

    monkeypatch.setattr(adapter, "_complete_text", summary)
    await adapter._compact_history(session_id, next(iter(adapter.endpoints.values())))

    kept = adapter._history[session_id]
    assert kept[0] == {"role": "user", "content": "（此前对话摘要）四轮问答的摘要"}
    assert len(kept) < len(_tool_turns(4))  # something was actually compacted
    calls = [item["call_id"] for item in kept if item.get("type") == "function_call"]
    outputs = [item["call_id"] for item in kept if item.get("type") == "function_call_output"]
    assert calls == outputs and calls  # never a lone output, never a lone call


async def test_a_failed_summary_leaves_the_history_untouched(native_application: FastAPI, monkeypatch):
    """The old code popped the turns first and lost them when the summary call failed."""
    adapter = native_application.state.adapter
    session_id = "compaction-failure"
    original = _tool_turns(4)
    adapter._history[session_id] = list(original)

    async def failing(endpoint, *, instructions, input_text, **_):
        raise RuntimeError("summary_unavailable")

    monkeypatch.setattr(adapter, "_complete_text", failing)
    await adapter._compact_history(session_id, next(iter(adapter.endpoints.values())))
    assert adapter._history[session_id] == original


def test_an_unpaired_earlier_turn_is_dropped_whole_before_the_request(native_application: FastAPI):
    """Last line of defence: whatever produced it, a turn whose call and output do not pair up never
    reaches the model — and the turn being answered is never the one dropped."""
    from trader_runtime.native_agent import NativeAgentAdapter
    items = _tool_turns(2, filler=1)
    del items[2]  # the first turn's function_call_output
    paired = NativeAgentAdapter._paired_tool_items(items)
    assert [item.get("call_id") for item in paired if item.get("type") == "function_call"] == ["call-1"]
    assert [item.get("call_id") for item in paired if item.get("type") == "function_call_output"] == ["call-1"]
    assert paired[0]["content"].startswith("第 1 个问题")
    # A current turn whose output has not come back yet is kept: it holds the question being answered.
    in_flight = _tool_turns(1, filler=1)[:-2]
    assert NativeAgentAdapter._paired_tool_items(in_flight) == in_flight


async def test_an_external_app_call_tool_card_reaches_the_conversation(native_client: AsyncClient, native_application: FastAPI):
    """`tool:propose_order` through app_call (the external AI's card tool, 意图重构第 3 步): the card is published to
    the caller's session like a turn's card. The three retired card tools are no capability any more."""
    created = await native_client.post("/api/agent/call", json={"operation": "tool:propose_order", "body": {
        "act": "open", "symbol": "SHFE.au2612", "dir": "short", "vol": 1, "price": "queue", "reason": "外部 agent 生成候选"}})
    assert created.status_code == 200, created.text
    proposal_id = created.json()["data"]["data"]["proposalId"]
    session_id = next(iter(native_application.state.app_capabilities.sessions.values()))
    published = [event for _, event in native_application.state.store.list_events(session_id) if event["name"] == "proposal.trade"]
    assert published and published[-1]["payload"]["proposalId"] == proposal_id
    retired = await native_client.post("/api/agent/call", json={"operation": "tool:create_trade_proposal", "body": {
        "symbol": "SHFE.au2612", "side": "sell_open", "volume": 1, "reason": "旧工具"}})
    assert retired.status_code == 404


async def test_an_opening_order_without_a_product_takes_the_windows_contract_without_a_model(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“820挂一手多” names no product: the order planner takes the asking window's contract (even with
    autoReadScreen off) and the user's own price — no model round, nothing of the screen read to anyone."""
    window_id = await _register_window(native_client)
    assert (await native_client.put("/api/preferences", json={"chat": {"autoReadScreen": False}})).status_code == 200
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "820挂一手多", "uiWindowId": window_id})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    assert responses_server.requests == []
    cards = [event["payload"] for event in events_of(native_application, session_id) if event["name"] == "proposal.trade"]
    assert len(cards) == 1
    assert cards[0]["symbol"].upper() == "SHFE.AU2612" and cards[0]["side"] == "buy_open" and cards[0]["volume"] == 1
    assert cards[0]["limitPrice"] == 820
    assert assistant_answers(native_application, session_id)[-1] == "沪金2612，买开 1 手，限价 820，确认吗？"


async def test_a_fixed_table_interface_request_runs_without_a_model_turn(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“把设置打开” typed with the window online: the deterministic router executes the UI command
    itself through the same path as the voice fast intent — no model request at all, one assistant
    line, the window command applied with its receipt."""
    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    requests_before = len(responses_server.requests)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "把设置打开", "uiWindowId": window_id})
    assert response.status_code == 202
    run_id = response.json()["runId"]

    # A live client dispatches the queued command and receipts it applied (the real window's loop).
    pulse = _window_pulse(window_id, "交易台")
    applied: list[dict] = []

    async def play() -> None:
        while True:
            result = (await native_client.post("/api/ui/pulse", json=pulse)).json()
            command = result.get("command")
            if command:
                pulse["revision"] += 1
                pulse["state"] = {**pulse["state"], **command["changes"]}
                pulse["receipt"] = {"commandId": command["commandId"], "status": "applied"}
                await native_client.post("/api/ui/pulse", json=pulse)
                pulse.pop("receipt")
                applied.append(command["changes"])
            await asyncio.sleep(0.01)

    player = asyncio.create_task(play())
    try:
        assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    finally:
        player.cancel()
        await asyncio.gather(player, return_exceptions=True)
    assert responses_server.requests[requests_before:] == []  # the turn never reached the model
    assert applied == [{"settingsOpen": True}]
    assert assistant_answers(native_application, session_id)[-1] == "设置打开了"


async def test_a_sure_typed_order_is_carded_by_the_planner_without_a_model_round(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“搞一手螺纹多的” typed (意图重构第 3 步): the order-intent parser is sure of it, so the Runtime's planner makes
    the card — contract from the catalog, the opening gate, the default price — with no model request at all, and
    the turn's line says the card's own numbers."""
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages", json={"text": "搞一手螺纹多的"})
    assert response.status_code == 202
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    assert responses_server.requests == []
    cards = [event["payload"] for event in events_of(native_application, session_id)
             if event["name"] == "proposal.trade"]
    assert cards and cards[-1]["status"] == "approval_required"
    assert cards[-1]["symbol"].upper() == "SHFE.RB2610" and cards[-1]["side"] == "buy_open" and cards[-1]["volume"] == 1
    assert cards[-1]["readiness"]["passed"] is True and cards[-1]["intentSource"] == "parser"
    answer = assistant_answers(native_application, session_id)[-1]
    assert answer.startswith("螺纹钢2610，买开 1 手，") and answer.endswith("确认吗？")
    assert native_application.state.simulation_orders.list(user_id="local-user") == []
    assert native_application.state.store.get(cards[-1]["proposalId"])["payload"]["reason"] == "打字：搞一手螺纹多的"


async def test_a_typed_sentence_of_two_orders_cards_each_from_its_own_clause_in_order(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """所有者 2026-10-16（一句两单按顺序出两张卡、各自确认）: “开多一手螺纹，然后开空两手沪金” is split by the parser
    and each clause read on its own — the 螺纹 card buys one, the 沪金 card sells two (never a slot from the other
    clause); both published in order, each its own card to click, no model round; marked `parser_legs`. (Bare
    “买一手螺纹，然后卖两手沪金” is refused here — legs_unsettled: the test account holds the opposite side of one of
    them, 开新仓还是平仓 — one order at a time.)"""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "开多一手螺纹，然后开空两手沪金")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert responses_server.requests == []
    cards = [event["payload"] for event in events_of(native_application, session_id) if event["name"] == "proposal.trade"]
    assert [(card["symbol"].upper()[:7], card["side"], card["volume"], card["status"], card["intentSource"]) for card in cards] == [
        ("SHFE.RB", "buy_open", 1, "approval_required", "parser_legs"),
        ("SHFE.AU", "sell_open", 2, "approval_required", "parser_legs")]
    answer = assistant_answers(native_application, session_id)[-1]
    assert answer.startswith("一共 2 张卡，各自确认。第 1 张：螺纹钢") and "买开 1 手" in answer
    assert answer.index("第 2 张：") < answer.index("卖开 2 手")
    assert native_application.state.simulation_orders.list(user_id="local-user") == []
    bare = await create_session(native_client)
    run_id = await start_run(native_client, bare, "买一手螺纹，然后卖两手沪金")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert responses_server.requests == []
    assert not [event for event in events_of(native_application, bare) if event["name"].startswith("proposal.")]
    assert assistant_answers(native_application, bare)[-1].endswith("这句话里有几单，请一单一单说。")


async def test_the_direct_path_takes_the_default_size_and_keeps_the_words_origin(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """#952 Codex: an opening that names no size takes 设置 → 交易 → 默认手数 (when set above 1, as the trading AI's
    preference line says), and a spoken order's reason says 语音, not 打字."""
    native_application.state.store.update_preferences("local-user", {"trade": {"defaultVolume": 4}})
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "螺纹开多", "origin": "voice"})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    assert responses_server.requests == []
    cards = [event["payload"] for event in events_of(native_application, session_id) if event["name"] == "proposal.trade"]
    assert len(cards) == 1 and cards[0]["volume"] == 4 and cards[0]["intentSource"] == "parser"
    assert native_application.state.store.get(cards[0]["proposalId"])["payload"]["reason"] == "语音：螺纹开多"


async def test_an_order_missing_its_direction_asks_through_the_structured_question_card(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“来一手螺纹” (no direction): the planner asks — the "需要确认的信息" card whose quick answers are whole orders
    the parser reads (so a tap goes straight to its card) — and the turn ends on the question; no proposal, no model
    round, never a guessed 买开."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "来一手螺纹")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert responses_server.requests == []
    events = events_of(native_application, session_id)
    questions = [event["payload"] for event in events if event["name"] == "card.order_question"]
    assert len(questions) == 1 and questions[0]["kind"] == "ask" and questions[0]["missing"] == ["dir"]
    assert [option["label"] for option in questions[0]["options"]] == ["买开（做多）", "卖开（做空）"]
    assert [option["prompt"] for option in questions[0]["options"]] == ["买开1手螺纹钢2610", "卖开1手螺纹钢2610"]
    assert questions[0]["title"] == "需要确认：方向"
    answer = [event["payload"] for event in events if event["name"] == "message.completed"
              and event["payload"]["role"] == "assistant"][-1]
    # The question card and the answer share the turn, and the answer is the question itself.
    assert answer["turnId"] == questions[0]["turnId"] and answer["text"] == "开多还是开空？"
    assert not [event for event in events if event["name"].startswith("proposal.")]


@pytest.mark.parametrize("text", ["该不该买两手螺纹", "我只是想认真问问你的看法，现在行情波动这么大而且风险也不小，我是不是应该买一手螺纹钢多单呢"])
async def test_a_no_card_turn_runs_no_order_write_even_after_activating_a_skill(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
    text: str,
):
    """#952 Codex: “该不该买两手螺纹” is advice (no_card), and so is a 45-character question past the parser's length
    limit (its manner is read before the length). With 自主执行 on (the default), activating order-ops still offers
    no execution: every round withholds the order writes (turn_router.ORDER_WRITE_TOOLS) and the dispatch refuses
    them — directly or through app_call — so nothing is ordered and no card is made."""
    def scenario(text, outputs, body):
        if not outputs:
            return responses_server._tool_stream([("activate_skill", {"name": "order-ops"})])
        if len(outputs) == 1:
            return responses_server._tool_stream([
                ("execute_simulation_trade", {"symbol": "SHFE.rb2610", "side": "buy_open", "volume": 2,
                                              "priceIntent": "aggressive", "reason": "该不该买两手螺纹"}),
                ("app_call", {"requestJson": json.dumps({"operation": "tool:execute_simulation_trade", "body": {
                    "symbol": "SHFE.rb2610", "side": "buy_open", "volume": 2, "priceIntent": "aggressive",
                    "reason": "该不该买两手螺纹"}})}),
                ("set_simulation_stop_loss", {"symbol": "SHFE.rb2610", "stopLossPrice": 3000}),
            ])
        return responses_server._text_stream("要不要买看你的计划，我这轮不下单。")
    monkeypatch.setattr(responses_server, "_scenario", scenario)
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, text)
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    later = [{tool.get("name") for tool in request["tools"]} for request in responses_server.requests[1:]]
    assert later and not any(names & {"execute_simulation_trade", "propose_order", "set_simulation_stop_loss"}
                             for names in later)
    refused = FakeResponsesServer._tool_outputs(responses_server.requests[-1])[-3:]
    assert [output["payload"].get("errorCode") for output in refused] == ["tool_not_offered"] * 3
    assert native_application.state.simulation_orders.list(user_id="local-user") == []
    assert not [event for event in events_of(native_application, session_id) if event["name"].startswith("proposal.")]


@pytest.mark.parametrize("text", ["螺纹给我追两张多单", "帮我下个螺纹多单两张", "820挂一手", "你自己下单买一手", "查一下委托状态"])
async def test_a_users_turn_never_fills_an_order_directly(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
    text: str,
):
    """Owner 2026-10-16: in a turn the user's own message caused, the AI never fills an order directly — with 自主执行
    on (the default) and order-ops mounted, execute_simulation_trade is not offered and the dispatch refuses it,
    directly or through app_call, whatever the words; the order is a propose_order card the user confirms. (Strategy
    auto orders — AI-initiated — still fill directly: test_strategy_signal_action.)"""
    def scenario(text_, outputs, body):
        if not outputs:
            return responses_server._tool_stream([("activate_skill", {"name": "order-ops"})])
        if len(outputs) == 1:
            return responses_server._tool_stream([
                ("execute_simulation_trade", {"symbol": "SHFE.rb2610", "side": "buy_open", "volume": 1,
                                              "priceIntent": "aggressive", "reason": text}),
                ("app_call", {"requestJson": json.dumps({"operation": "tool:execute_simulation_trade", "body": {
                    "symbol": "SHFE.rb2610", "side": "buy_open", "volume": 1, "priceIntent": "aggressive",
                    "reason": text}})}),
            ])
        return responses_server._text_stream("这句我只能出卡让你确认，不能直接下单。")
    monkeypatch.setattr(responses_server, "_scenario", scenario)
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, text)
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    later = [{tool.get("name") for tool in request["tools"]} for request in responses_server.requests[1:]]
    assert later and all("execute_simulation_trade" not in names for names in later)
    refused = FakeResponsesServer._tool_outputs(responses_server.requests[-1])[-2:]
    assert [output["payload"].get("errorCode") for output in refused] == ["tool_not_offered"] * 2
    assert native_application.state.simulation_orders.list(user_id="local-user") == []


@pytest.mark.parametrize(("text", "asked"), [
    ("820挂一手", False), ("介绍一下这个策略", False), ("这个策略怎么样", False), ("我在看这个策略", False),
    ("启动这个策略吗", False), ("别启动策略", False), ("确认", False),  # a bare 确认 with no restatement of the strategy
    ("把螺纹策略上线，自动下单", True), ("启动螺纹策略", True), ("开始监控螺纹", True),
])
async def test_a_users_turn_starts_an_auto_strategy_only_when_the_user_asks_for_it(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
    text: str, asked: bool,
):
    """#953 Codex, #932 复核: start_strategy_monitoring with signalAction auto (or left out) may order by itself, so a
    user's turn runs it only when the words command its start (the parser's manner: no question, introduction or
    judgement) — never slipped into “820挂一手” or “介绍一下这个策略”. The same start is caught however it is spelled:
    the tool, app_call's `tool:start_strategy_monitoring`, or the HTTP route. An explicit start still starts at once: no
    new authorization words."""
    def scenario(text_, outputs, body):
        if not outputs:
            return responses_server._tool_stream([
                ("start_strategy_monitoring", {"strategyId": "strategy-x", "signalAction": "auto"}),
                ("app_call", {"requestJson": json.dumps({"operation": "tool:start_strategy_monitoring",
                                                         "body": {"strategyId": "strategy-x"}})}),
                ("app_call", {"requestJson": json.dumps({"operation": "POST /api/strategies/{strategy_id}/monitor/start",
                                                         "path": {"strategy_id": "strategy-x"},
                                                         "body": {"signalAction": "auto"}})}),
            ])
        return responses_server._text_stream("好的。")
    monkeypatch.setattr(responses_server, "_scenario", scenario)
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, text)
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    codes = [output["payload"].get("errorCode") for output in FakeResponsesServer._tool_outputs(responses_server.requests[-1])]
    assert len(codes) == 3
    if asked:  # reached the start itself (strategy-x does not exist: the tool's own refusal), not a gate
        assert not {"strategy_start_not_asked", "tool_not_offered", "ui_only_tool", "ui_scope_write"} & set(codes), codes
    else:  # a question or a negation is a no-card turn: the start is not even offered there
        assert set(codes) <= {"strategy_start_not_asked", "tool_not_offered"}


def test_a_bare_confirmation_starts_only_the_strategy_the_ais_last_answer_named():
    """#932 复核: 确认 / 好 is a start only as the answer to the AI's restatement of that very strategy; a start command
    naming another of the user's strategies does not start this one."""
    from types import SimpleNamespace

    from trader_runtime.native_agent import NativeAgentAdapter, _Turn
    drafts = {"s-rb": "螺纹均线", "s-au": "沪金突破"}
    store = SimpleNamespace(
        get_session=lambda session_id: {"userId": "u"},
        get_strategy_draft=lambda strategy_id, user_id: {"name": drafts[strategy_id]},
        list_strategy_draft_heads=lambda user_id, limit: [{"strategyId": key, "name": name} for key, name in drafts.items()])
    agent = NativeAgentAdapter.__new__(NativeAgentAdapter)
    agent.store = store
    agent._history = {"s": [{"role": "user", "content": "螺纹均线能上线吗"},
                            {"role": "assistant", "content": "螺纹均线已验证，要用自动下单上线螺纹均线吗？"},
                            {"role": "user", "content": "确认"}]}

    def asks(text, strategy_id, mark=2):
        turn = _Turn(session_id="s", run_id="r", turn_id="t", mode="local", gated=False, task={}, user_text=text,
                     history_mark=mark)
        return agent._asks_for_strategy(turn, strategy_id)
    assert asks("确认", "s-rb") and asks("好的", "s-rb")
    assert not asks("确认", "s-au")  # the answer restated another strategy
    assert not asks("确认", "s-rb", mark=0)  # nothing restated before it
    assert asks("启动沪金突破", "s-au") and asks("启动这个策略", "s-au")
    assert not asks("启动螺纹均线策略", "s-au")  # names another strategy
    for words in ("介绍一下螺纹均线", "螺纹均线怎么样", "启动螺纹均线吗", "别启动螺纹均线", "不要上线螺纹均线", "螺纹均线已经启动了"):
        assert not asks(words, "s-rb"), words
    assert asks("把螺纹均线改成自动下单", "s-rb") and asks("帮我把螺纹均线跑起来", "s-rb")
    # A22: an answer that only described the strategy is no start request — a bare 好 to it starts nothing.
    agent._history["s"][1]["content"] = "螺纹均线近 60 个交易日胜率 52%，回撤 8%。"
    assert not asks("好", "s-rb") and not asks("确认", "s-rb")
    agent._history["s"][1]["content"] = "螺纹均线已验证。要启动它吗？"  # 它 may point at several: the user says it
    assert not asks("好", "s-rb")
    agent._history["s"][1]["content"] = "要启用螺纹均线吗？"  # every start verb the parser accepts
    assert asks("确认", "s-rb")
    agent._history["s"][1]["content"] = "螺纹均线表现平稳。要启动沪金突破吗？"  # the question is about the other one
    assert not asks("好", "s-rb") and asks("好", "s-au")
    agent._history["s"][1]["content"] = "螺纹均线还没有启动，要查看详情吗？"  # a status, then another question
    assert not asks("好", "s-rb")
    agent._history["s"][1]["content"] = "螺纹均线表现平稳，要启动沪金突破吗？"  # the question names the other one
    assert not asks("好", "s-rb") and asks("好", "s-au")
    agent._history["s"][1]["content"] = "想看看螺纹均线需要满足什么条件才能启动吗？"  # discusses starting, asks nothing
    assert not asks("好", "s-rb")
    agent._history["s"][1]["content"] = "螺纹均线回测通过。要不要启动螺纹均线？"  # 要不要 is a question, not 不要
    assert asks("好", "s-rb")
    agent._history["s"][1]["content"] = "螺纹均线和刚才那张策略都通过了，要启动吗？"  # names neither in the question
    assert not asks("好", "s-rb")
    agent._history["s"][1]["content"] = "要用螺纹均线演示如何启动吗？"  # a demonstration, not a start
    assert not asks("好", "s-rb")
    agent._history["s"][1]["content"] = "要启动螺纹均线吗？算了，先不要启动。"  # withdrawn later in the answer
    assert not asks("好", "s-rb")
    agent._history["s"][1]["content"] = "要启动 **螺纹均线** 吗？"  # Markdown and spaces
    assert asks("好", "s-rb")
    drafts["s-auto"] = "自动下单"  # a name that is also a start verb
    agent._history["s"][1]["content"] = "要自动下单吗？"
    assert not asks("好", "s-auto")
    drafts["s-star"] = "*"  # a name made only of Markdown marks
    agent._history["s"][1]["content"] = "**要启动吗？**"
    assert not asks("好", "s-star")
    drafts["s-u"] = "RB_均线"  # a name with an underscore
    agent._history["s"][1]["content"] = "要启动RB_均线吗？"
    assert asks("好", "s-u")
    drafts["s-rb2"] = "螺纹均线 (2)"  # a copy's name contains the original's
    agent._history["s"][1]["content"] = "要启动螺纹均线 (2)吗？"
    assert asks("好", "s-rb2")


async def test_a_parsed_order_goes_to_the_planner_and_is_never_executed(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“买螺纹” with 自主执行 on: the parser reads it, the planner asks the size — no model round, no order."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "买螺纹")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert responses_server.requests == []
    questions = [event["payload"] for event in events_of(native_application, session_id)
                 if event["name"] == "card.order_question"]
    assert len(questions) == 1 and questions[0]["missing"] == ["vol"]
    assert native_application.state.simulation_orders.list(user_id="local-user") == []


async def test_the_order_round_must_end_in_a_card_a_refusal_or_a_question(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
):
    """#952 Codex: the trading AI's order round (“开多一手螺纹，止损3100”) is card-required — tool_choice required
    until propose_order comes back; prose is retried once, then the Runtime's own 没能生成方案卡 card ends the turn,
    never the model's words claiming a card."""
    def scenario(text, outputs, body):
        return responses_server._text_stream("好的，已经给你下好了。")
    monkeypatch.setattr(responses_server, "_scenario", scenario)
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "开多一手螺纹，止损3100")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert len(responses_server.requests) == 2  # the round and its one retry
    assert all(request.get("tool_choice") == "required" for request in responses_server.requests)
    events = events_of(native_application, session_id)
    questions = [event["payload"] for event in events if event["name"] == "card.order_question"]
    assert len(questions) == 1 and questions[0]["kind"] == "no_card"
    assert assistant_answers(native_application, session_id)[-1] == "这次没能生成方案卡，可以换个说法再说一次，或打开下单台自己下。"
    assert not [event for event in events if event["name"].startswith("proposal.")]


async def test_the_trading_ais_several_orders_in_one_sentence_make_no_card(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """#952 Codex: the model's legs cannot be bound clause by clause (买一手螺纹，然后卖两手沪金 read with the sizes
    swapped passes every whole-sentence check), so propose_order refuses any sentence of several orders — like the
    fast parser — and the user says them one at a time."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "一句两单：螺纹钢多一手，沪金空一手")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert not [event for event in events_of(native_application, session_id) if event["name"].startswith("proposal.")]
    output = FakeResponsesServer._tool_outputs(responses_server.requests[-1])[0]["payload"]
    assert output["errorCode"] == "legs_entangled" and "一单一单说" in str(output["data"]["userFacing"])


async def test_a_card_the_users_words_do_not_ground_becomes_a_question(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """意图重构第 3 步（槽位出处）: the model's propose_order names a contract, a side and a size the user never said
    (“请生成交易候选”): no card — the planner's question card offers the candidate contracts instead."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "请生成交易候选")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    events = events_of(native_application, session_id)
    assert not [event for event in events if event["name"].startswith("proposal.")]
    questions = [event["payload"] for event in events if event["name"] == "card.order_question"]
    assert len(questions) == 1 and questions[0]["missing"] == ["product"]
    assert assistant_answers(native_application, session_id)[-1] == "说的是哪个合约？"


async def test_a_question_while_a_card_waits_makes_no_new_card_but_keeps_the_cards_own_tools(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """#952 Codex: with a card waiting, “螺纹买两手吗” is still a question — propose_order is withheld (and refused if
    called), while the waiting card's reject tool stays offered."""
    session_id = await create_session(native_client)
    native_application.state.proposals.create_trade(
        session_id, "waiting-card", symbol="SHFE.au2612", side="buy_open", volume=1,
        price_intent="passive", reason="等确认")
    run_id = await start_run(native_client, session_id, "螺纹买两手吗")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    offered = {tool.get("name") for tool in responses_server.requests[0]["tools"]}
    assert "propose_order" not in offered and "reject_proposal" in offered
    cards = [event for event in events_of(native_application, session_id) if event["name"].startswith("proposal.")]
    assert cards == []  # the waiting card was made outside the turn's events; no new one was published
    assert len(native_application.state.store.recent_proposals(session_id, limit=5)) == 1


async def test_a_card_tool_a_question_turn_does_not_offer_is_refused_before_it_runs(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """#929 (10-15 线上) → 意图重构第 3 步: “螺纹买两手吗” is a question — the turn answers and withholds the card tool.
    A model that calls propose_order anyway is refused before anything runs (no card), and answers instead."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "螺纹买两手吗")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    offered = {tool.get("name") for tool in responses_server.requests[0]["tools"]}
    assert "propose_order" not in offered
    refused = [item for item in responses_server.requests[1]["input"] if item.get("type") == "function_call_output"]
    assert json.loads(refused[-1]["output"])["errorCode"] == "tool_not_offered"
    events = events_of(native_application, session_id)
    assert not [event for event in events if event["name"].startswith("proposal.")]
    steps = [event["payload"] for event in events if event["name"] == "task.completed"][-1]["steps"]
    assert [step.get("errorCode") for step in steps if step.get("tool") == "propose_order"] == ["tool_not_offered"]
    assert assistant_answers(native_application, session_id)[-1] == "这是问句，我先不出卡。"


async def test_the_question_card_settles_when_its_answer_arrives(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """10-08 线上：点了追问卡的“买开（做多）”后卡仍显示“待你回答”、按钮还能再点。The reply that
    arrives after a 需要确认 card — an option's prompt or any typed words — republishes the card
    under the same cardId as answered (with the chosen option's label), and only that once: a later
    message settles nothing."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "来一手螺纹")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    events = events_of(native_application, session_id)
    asked = [event["payload"] for event in events if event["name"] == "card.order_question"]
    assert len(asked) == 1 and asked[0]["kind"] == "ask"
    # The option button's prompt arrives as the user's next message: the card is settled before
    # the model's proposal, and the proposal it was waiting for lands after it.
    answer_run = await start_run(native_client, session_id, "买开1手螺纹钢2610")
    assert (await wait_for_run(native_application, answer_run))["status"] == "completed"
    events = events_of(native_application, session_id)
    settled = [(index, event["payload"]) for index, event in enumerate(events)
               if event["name"] == "card.order_question"]
    assert [payload["cardId"] for _, payload in settled] == [asked[0]["cardId"]] * 2
    first, second = settled[0][1], settled[-1][1]
    assert second["answered"] is True and second["answer"] == "买开（做多）" and second["answeredAt"]
    assert second["question"] == first["question"] and second["options"] == first["options"]
    proposals = [index for index, event in enumerate(events) if event["name"] == "proposal.trade"]
    assert proposals and settled[-1][0] < proposals[0]
    # A later, unrelated message settles nothing: still exactly one answered republish.
    later_run = await start_run(native_client, session_id, "今天沪金怎么看")
    assert (await wait_for_run(native_application, later_run))["status"] == "completed"
    events = events_of(native_application, session_id)
    question_events = [event["payload"] for event in events if event["name"] == "card.order_question"]
    assert len(question_events) == 2
    assert len([payload for payload in question_events if payload.get("answered")]) == 1


async def test_a_pure_interface_turn_leaves_the_question_card_open(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """#753 review: “把设置打开” while a 需要确认 card waits is no answer to it — the card keeps its choices;
    the next real answer still settles it."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "来一手螺纹")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    window_id = await _register_window(native_client)
    pulse = _window_pulse(window_id, "交易台")

    async def play() -> None:
        while True:
            command = (await native_client.post("/api/ui/pulse", json=pulse)).json().get("command")
            if command:
                pulse["revision"] += 1
                pulse["state"] = {**pulse["state"], **command["changes"]}
                pulse["receipt"] = {"commandId": command["commandId"], "status": "applied"}
                await native_client.post("/api/ui/pulse", json=pulse)
                pulse.pop("receipt")
            await asyncio.sleep(0.01)

    player = asyncio.create_task(play())
    try:
        response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                            json={"text": "把设置打开", "uiWindowId": window_id})
        assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    finally:
        player.cancel()
        await asyncio.gather(player, return_exceptions=True)
    assert assistant_answers(native_application, session_id)[-1] == "设置打开了"
    questions = [event["payload"] for event in events_of(native_application, session_id) if event["name"] == "card.order_question"]
    assert len(questions) == 1 and not questions[0].get("answered")
    answer_run = await start_run(native_client, session_id, "买开1手螺纹钢2610")
    assert (await wait_for_run(native_application, answer_run))["status"] == "completed"
    questions = [event["payload"] for event in events_of(native_application, session_id) if event["name"] == "card.order_question"]
    assert questions[-1]["answered"] is True


async def test_a_typed_answer_that_matches_no_option_settles_the_card_without_a_label(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """The user answers in their own words instead of a button: the card is still settled (answered
    without a label — the reply itself is right below in the conversation), so its buttons stop
    offering an already-consumed answer."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "来一手螺纹")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    typed_run = await start_run(native_client, session_id, "螺纹买一手")
    assert (await wait_for_run(native_application, typed_run))["status"] == "completed"
    settled = [event["payload"] for event in events_of(native_application, session_id)
               if event["name"] == "card.order_question"]
    assert len(settled) == 2 and settled[-1]["answered"] is True and settled[-1]["answer"] is None


async def test_an_opinion_question_gets_the_judgment_card_first_and_its_ready_update_in_place(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“螺纹怎么看”: the Runtime attaches the research desk's judgment card before the model answers —
    生成中 while today's judgment is still being made, then the ready judgment under the same cardId
    (stance, confidence, facts with their quote and source, invalidation, 打开调研). The model still
    answers as a general turn, told not to repeat the card."""
    from types import SimpleNamespace

    reports = [
        {"productName": "螺纹钢", "mainSymbol": "SHFE.rb2610", "judgment": {"status": "pending"}},
        {"productName": "螺纹钢", "mainSymbol": "SHFE.rb2610", "judgment": {
            "status": "ready", "today": True, "stance": "偏多", "confidence": "中", "headline": "库存下降",
            "generatedAt": "2026-10-04T00:46:00+00:00", "researchDay": "2026-10-04",
            "points": [{"text": "社会库存连降", "quote": "库存 520 万吨", "basis": ["F.stock"]}],
            "risks": [{"text": "需求回落", "quote": "表观消费下滑", "basis": ["F.demand"]}],
            "basis": {"F.stock": {"source": "钢联", "asOf": "2026-10-03"},
                      "F.demand": {"source": "钢联", "asOf": "2026-10-02"}}}},
    ]
    seen: list[str] = []

    class Desk:
        generator_for = None

        async def report(self, symbol, *, generator=None, force=False, user_id=None):
            seen.append(symbol)
            return reports[0] if len(seen) == 1 else reports[1]

    adapter = native_application.state.adapter
    adapter.trader_tools.research_brief = SimpleNamespace(desk=Desk())
    adapter.JUDGMENT_POLL_SECONDS = 0.01
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "螺纹怎么看")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    for _ in range(200):
        cards = [event["payload"] for event in events_of(native_application, session_id)
                 if event["name"] == "card.research_judgment"]
        if len(cards) >= 2:
            break
        await asyncio.sleep(0.02)
    assert seen[0] == "SHFE.rb2610"
    assert [card["status"] for card in cards] == ["pending", "ready"]
    assert cards[0]["cardId"] == cards[1]["cardId"] and cards[0]["turnId"] == cards[1]["turnId"]
    ready = cards[1]
    assert ready["stance"] == "偏多" and ready["confidence"] == "中"
    assert ready["facts"] == [{"text": "社会库存连降", "quote": "库存 520 万吨", "source": "钢联", "asOf": "2026-10-03"}]
    assert ready["invalidation"][0]["text"] == "需求回落"
    assert ready["actions"] == [{"kind": "ui", "label": "打开调研", "changes": {"news": {"tab": "desk"}}}]
    events = events_of(native_application, session_id)
    answer = [event["payload"] for event in events if event["name"] == "message.completed"
              and event["payload"]["role"] == "assistant"][-1]
    assert answer["turnId"] == ready["turnId"]  # the card sits on the answer's own turn
    # The 生成中 card went out before the model's answer of that turn.
    names = [event["name"] for event in events if event["payload"].get("turnId") == ready["turnId"]]
    assert names.index("card.research_judgment") < names.index("message.completed")
    assert _model_saw(responses_server, "系统已在回答上方附上该品种当日的调研判断卡")


async def test_a_refused_card_ends_the_turn_with_its_user_facing_line(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“平掉沪金三手” typed: the fixture's au2612 position holds 2 lots (all today's), so the planner refuses
    (close_volume_exceeds_position) and its line is the answer — no model round, no card, and the auto-decided
    bucket never surfaces 平今/平昨 as a demand."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "平掉沪金三手")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert responses_server.requests == []
    answers = assistant_answers(native_application, session_id)
    assert answers[-1] == "当前没有足够的沪金2612持仓可平 3 手（今仓可平 2 手、昨仓可平 0 手），平不了。"
    assert not [event for event in events_of(native_application, session_id)
                if event["name"].startswith("proposal.")]
    assert native_application.state.simulation_orders.list(user_id="local-user") == []


async def test_a_close_all_over_a_closed_market_ends_the_turn_with_each_skipped_contract(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“全部平仓” typed while the market is closed (#721, 2026-10-11 线上): every position is skipped (no fresh
    quote), the planner refuses with each contract and its Chinese reason, and that is the answer — no model
    round, no DSML markup, no card."""
    provider = native_application.state.simulation_orders.data_provider
    live_snapshot = provider.market_snapshot
    provider.market_snapshot = lambda turn_id, symbol: {**live_snapshot(turn_id, symbol), "marketSessionStatus": "closed"}
    try:
        session_id = await create_session(native_client)
        run_id = await start_run(native_client, session_id, "全部平仓")
        assert (await wait_for_run(native_application, run_id))["status"] == "completed"
        assert responses_server.requests == []
        answers = assistant_answers(native_application, session_id)
        assert answers[-1] == "沪金2612：当前不在交易时段；SHFE.ag2612：当前不在交易时段；热轧卷板2610：当前不在交易时段。"
        assert not [event for event in events_of(native_application, session_id)
                    if event["name"].startswith("proposal.")]
        assert native_application.state.simulation_orders.list(user_id="local-user") == []
    finally:
        provider.market_snapshot = live_snapshot


async def test_a_close_all_with_no_positions_at_all_says_so(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """The same 全平 with nothing to close is refused with「没有可平的持仓」, never a bare code (#721)."""
    provider = native_application.state.simulation_orders.data_provider
    provider.positions = lambda: []
    try:
        session_id = await create_session(native_client)
        run_id = await start_run(native_client, session_id, "全部平仓")
        assert (await wait_for_run(native_application, run_id))["status"] == "completed"
        assert responses_server.requests == []
        assert assistant_answers(native_application, session_id)[-1] == "没有可平的持仓。"
    finally:
        del provider.positions  # back to the class's fixture positions


async def test_a_general_turn_retrying_the_same_card_refusal_ends_on_the_refusal_line(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """AI 审计 2026-10-13：没走固定路线的回合里，模型对“没有可平的持仓”连调三次全平工具，最后说成
    “取不到持仓数据”。同一拒绝第二次出现就以拒绝原话收尾；拒绝说明不再是“数据未就绪”；
    失败步骤在任务事件里带 errorCode，事后可查。"""
    provider = native_application.state.simulation_orders.data_provider
    provider.positions = lambda: []
    try:
        session_id = await create_session(native_client)
        run_id = await start_run(native_client, session_id, "重复拒绝场景：全部平仓")
        assert (await wait_for_run(native_application, run_id))["status"] == "completed"
        assert len(responses_server.requests) == 2  # the second identical refusal ends the turn
        assert assistant_answers(native_application, session_id)[-1] == "没有可平的持仓"
        outputs = FakeResponsesServer._tool_outputs(responses_server.requests[-1])
        assert outputs[0]["payload"]["errorCode"] == "no_position_to_close"
        assert "数据" not in outputs[0]["payload"]["warning"] or "不要改说成数据取不到" in outputs[0]["payload"]["warning"]
        completed = [event for event in events_of(native_application, session_id) if event["name"] == "task.completed"]
        steps = [step for step in completed[-1]["payload"]["steps"] if step.get("tool") == "propose_order"]
        assert [step.get("errorCode") for step in steps] == ["no_position_to_close", "no_position_to_close"]
    finally:
        del provider.positions


@pytest.mark.parametrize("text,expected", [
    ("收尾轮吐标记", "没有生成卡片，这次没办成，稍后再试一次。"),
    ("收尾轮正常收尾", "行情读了很多轮，还没凑齐结论，稍后再问一次。"),
])
async def test_the_exhausted_closing_round_publishes_prose_never_raw_tool_call_markup(
    native_client: AsyncClient, native_application: FastAPI, text: str, expected: str,
):
    """Rounds exhausted → one closing round with tool_choice="none". DeepSeek answers that round by
    writing its tool calls into the text as DSML markup (#721): the published answer is the fixed
    honest line, never the markup. An ordinary prose closing passes through unchanged."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, text)
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    answers = assistant_answers(native_application, session_id)
    assert answers[-1] == expected
    assert "DSML" not in answers[-1] and "<tool_call" not in answers[-1]


@pytest.mark.parametrize("text", ["请生成交易候选", "交易AI平掉沪金三手"])
async def test_a_turn_ended_by_a_question_or_a_refusal_streams_its_task_completed_last(
    native_client: AsyncClient, native_application: FastAPI, text: str,
):
    """2026-10-04 线上：问题卡（规划器追问）或出卡被拒收尾的回合，工具步骤刚发的 task.progress 还在
    0.15 s 合并窗口里，task.completed 紧跟着到。旧逻辑把这条旧快照作瞬时事件补发，而 SSE 每批先发持久
    事件、后发瞬时事件，客户端于是先收到 completed、再收到 running 的旧快照，进度行一直停在
    “等待模型响应… 停止”，刷新后才对。这里按客户端实际读的 SSE 流断言：该任务最后一条事件是 completed。"""
    broker = native_application.state.broker
    session_id = await create_session(native_client)
    stop = asyncio.Event()

    class Reader:
        async def is_disconnected(self) -> bool:
            return stop.is_set()

    async def allow() -> None:
        return None

    received: list[dict] = []
    subscribed = asyncio.Event()

    async def on_subscribed() -> None:
        subscribed.set()

    async def read() -> None:
        async for frame in broker.stream(session_id, Reader(), authorize=allow, on_subscribed=on_subscribed):
            if not frame.startswith("event: heartbeat"):
                received.append(json.loads(frame.split("data: ", 1)[1]))

    reader = asyncio.create_task(read())
    await subscribed.wait()
    run_id = await start_run(native_client, session_id, text)
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    await wait_for_event(native_application, session_id, "task.completed", taskId=run_id)
    await asyncio.sleep(0.4)  # past the coalescing window: a late snapshot would have landed by now
    stop.set()
    await asyncio.wait_for(reader, timeout=3)
    task_events = [event for event in received
                   if event["name"].startswith("task.") and event["payload"].get("taskId") == run_id]
    assert task_events and task_events[-1]["name"] == "task.completed"
    assert task_events[-1]["payload"]["status"] == "completed"
    assert all(step["status"] != "active" for step in task_events[-1]["payload"]["steps"])


async def test_a_close_without_that_position_ends_the_turn_honestly(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“把螺纹那一手平了”: the fixture holds no rb position, so the honest “当前没有…持仓” comes before any SHFE
    今/昨 bucket complaint (the 09-20 bench showed the reverse order hiding it) — the planner's line, no model."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "把螺纹那一手平了")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert responses_server.requests == []
    answers = assistant_answers(native_application, session_id)
    assert answers[-1] == "当前没有螺纹钢2610的持仓，平不了。"
    assert not [event for event in events_of(native_application, session_id)
                if event["name"].startswith("proposal.")]
    assert native_application.state.simulation_orders.list(user_id="local-user") == []


async def test_a_search_with_no_result_ends_the_turn_without_looping(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“来一手豆粕”: the product is not in the catalog, so the parser is not sure and the trading AI searches. The
    model would repeat search_instruments until the rounds run out and end “未完成。”; the Runtime ends the turn
    with the honest line after the second empty search — no card."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "来一手豆粕")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert len(responses_server.requests) == 2
    answers = assistant_answers(native_application, session_id)
    assert answers[-1] == "没找到叫“豆粕”的合约，请说品种名或代码"
    assert not [event for event in events_of(native_application, session_id)
                if event["name"].startswith("proposal.")]
    assert native_application.state.simulation_orders.list(user_id="local-user") == []


# --- fuzzy P2: Runtime-attached lines, honest cutoffs and forced closes ------------------


class _StubPositionsTools:
    """Just enough of TraderReadOnlyTools for the Runtime-attached position line."""

    def __init__(self, rows: list[dict]) -> None:
        class _Provider:
            def positions(self) -> list[dict]:
                return rows
        self.provider = _Provider()


class _StubCatalogTools:
    """Just enough of TraderReadOnlyTools for the product pre-resolution line."""

    def __init__(self, items: list[dict]) -> None:
        class _Provider:
            def instrument_catalog(self) -> dict:
                return {"items": items}
        self.provider = _Provider()


async def test_a_pointing_close_sentence_takes_the_windows_contract_with_read_screen_off(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“把这个平一半” with chat.autoReadScreen off: no window snapshot is read to anyone; the planner takes the
    window's contract and the account's own position (沪金多 2 手 → 平一半 = 1 手), no model round."""
    window_id = await _register_window(native_client)
    assert (await native_client.put("/api/preferences", json={"chat": {"autoReadScreen": False}})).status_code == 200
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "把这个平一半", "uiWindowId": window_id})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    assert responses_server.requests == []
    cards = [event["payload"] for event in events_of(native_application, session_id) if event["name"] == "proposal.trade"]
    assert len(cards) == 1 and cards[0]["side"] == "sell_close" and cards[0]["volume"] == 1
    assert cards[0]["symbol"].upper() == "SHFE.AU2612"


async def test_the_product_hint_resolves_one_product_alias_and_nothing_ambiguous(
    native_application: FastAPI,
):
    """The pre-resolution line names the contract for exactly one catalog product (prefix or alias);
    zero or several products leave the sentence to the model's own search."""
    adapter = native_application.state.adapter
    items = [
        {"symbol": "SHFE.rb2610", "instrumentId": "rb2610", "instrumentName": "螺纹钢2610", "isMain": True},
        {"symbol": "SHFE.au2612", "instrumentId": "au2612", "instrumentName": "沪金2612", "isMain": True},
    ]
    tools = _StubCatalogTools(items)
    assert await adapter._product_hint_line("现在螺纹多少钱", tools) == (
        "用户说的品种：螺纹 = SHFE.rb2610（螺纹钢2610，主力），直接用这个合约，不用 search_instruments。\n")
    assert await adapter._product_hint_line("金子现在多少钱", tools) == (
        "用户说的品种：金子 = SHFE.au2612（沪金2612，主力），直接用这个合约，不用 search_instruments。\n")
    assert await adapter._product_hint_line("棉花最近咋样", tools) == ""
    assert await adapter._product_hint_line("螺纹和沪金多少钱", tools) == ""
    # An answer-card turn (回答卡 token 收敛, 2026-10-14) resolves every product it names, so a compare reads at once;
    # a named product the catalog cannot resolve leaves the search to the model.
    assert await adapter._product_hint_line("螺纹跟沪金今天哪个强", tools, several=True) == (
        "用户说的品种：螺纹 = SHFE.rb2610（螺纹钢2610，主力）；沪金 = SHFE.au2612（沪金2612，主力），"
        "直接用这些合约，不用 search_instruments。\n")
    assert await adapter._product_hint_line("螺纹跟沪金哪个强", _StubCatalogTools(items[:1]), several=True) == ""

    # One spoken word naming two products ("菜籽" → 菜籽油 / 菜籽粕) is ambiguous, never a three-contract compare (#886 review).
    async def names() -> list[str]:
        return ["菜籽油", "菜籽粕", "沪金"]
    adapter._product_names = names
    rapeseed = [{"symbol": "CZCE.OI701", "instrumentId": "OI701", "instrumentName": "菜籽油701", "isMain": True},
                {"symbol": "CZCE.RM701", "instrumentId": "RM701", "instrumentName": "菜籽粕701", "isMain": True}, items[1]]
    assert await adapter._product_hint_line("菜籽和沪金哪个强", _StubCatalogTools(rapeseed), several=True) == ""


async def test_a_market_query_route_ends_at_the_first_empty_search(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“这个怎么样” typed: the market_query route's bare reference searches nothing; the first empty
    result is already the honest end of the turn — one model request, no card, no looping."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "这个怎么样")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert len(responses_server.requests) == 1
    assert assistant_answers(native_application, session_id)[-1] == "没找到叫“这个”的合约，请说品种名或代码"
    assert not [event for event in events_of(native_application, session_id)
                if event["name"].startswith("proposal.")]


async def test_a_general_route_tolerates_one_empty_search_then_closes_honestly(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“棉花最近咋样” typed (general route): the first empty search is tolerated — the model may
    rephrase — and the second consecutive one ends the turn, naming the retried query word."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "棉花最近咋样")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert len(responses_server.requests) == 2  # the first empty search did not end the turn
    assert assistant_answers(native_application, session_id)[-1] == "没找到叫“棉花期货”的合约，请说品种名或代码"


async def test_round_exhaustion_gets_a_forced_no_tools_closing_round(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
    monkeypatch,
):
    """A turn that only ever calls tools exhausts its rounds: one more request forbids tool calls
    with tool_choice="none" while the tools stay declared (the history's function_call items need
    them — dropping tools alone is a 400 on Responses-compatible providers), and that round's
    one-sentence close is the published answer — not “未完成。”."""
    from trader_runtime import native_agent as native_agent_module
    monkeypatch.setattr(native_agent_module, "MAX_TOOL_ROUNDS", 1)
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "一直查工具不回答")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert len(responses_server.requests) == 2  # one tool round, then the forced close
    assert responses_server.requests[-1]["tool_choice"] == "none"
    assert responses_server.requests[-1]["tools"]  # declared, just not callable
    assert assistant_answers(native_application, session_id)[-1].startswith("查了几轮行情但没凑齐依据")


async def test_a_ui_round_without_a_write_escalates_to_the_general_turn(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“把界面切到图表那一页”: interface wording the fixed table misses. The UI round only reads the
    capabilities and attempts no write, so the same run re-runs it as a general turn (full tools),
    whose clarification is the answer — never a fixed line (#748)."""
    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "把界面切到图表那一页", "uiWindowId": window_id})
    assert response.status_code == 202
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    assert len(responses_server.requests) == 4
    assert all({tool["name"] for tool in body["tools"]} == UI_FALLBACK_TOOLS
               for body in responses_server.requests[:3])
    escalated = responses_server.requests[3]
    assert {tool["name"] for tool in escalated["tools"]} > UI_FALLBACK_TOOLS and "tool_choice" not in escalated
    # The UI round's tool items left the history: the general turn starts from the question.
    assert not any(item.get("type") in {"function_call", "function_call_output"} for item in escalated["input"])
    assert assistant_answers(native_application, session_id)[-1] == "你说的图表那一页是主图还是复盘页？"
    # The general turn took the UI round's reads over: they leave the task card (#779 P3).
    final = [e for e in events_of(native_application, session_id) if e["name"] == "task.completed"][-1]["payload"]
    assert [step["id"] for step in final["steps"]] == ["understand"]


async def test_an_escalated_ui_round_leaves_no_failed_step_on_the_task_card(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
):
    """#779 P3 (10-12 线上): the UI round's refused reads stayed on the task card as two 失败 steps after
    the general turn answered, so the user thought the turn had failed. Escalating drops them."""
    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    missing = "00000000-0000-0000-0000-000000000000"

    def scenario(text, outputs, body):
        if {tool["name"] for tool in body.get("tools") or []} != UI_FALLBACK_TOOLS:
            return responses_server._text_stream("你说的图表那一页是主图还是复盘页？")
        if len(outputs) < 2:
            # A read the window route refuses (unknown window): a failed step, no write attempted.
            return responses_server._tool_stream([("app_call", {"requestJson": json.dumps({
                "operation": "GET /api/app/ui/windows/{window_id}", "path": {"window_id": missing}})})])
        return responses_server._text_stream("没找到。")

    monkeypatch.setattr(responses_server, "_scenario", scenario)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "把界面切到图表那一页", "uiWindowId": window_id})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    # The UI round's two reads were refused (failed steps then), and no write was attempted.
    ui_outputs = [output for body in responses_server.requests[1:3]
                  for output in responses_server._tool_outputs(body)]
    assert ui_outputs and all(output["payload"]["data"]["ok"] is False for output in ui_outputs)
    final = [e for e in events_of(native_application, session_id) if e["name"] == "task.completed"][-1]["payload"]
    assert final["status"] == "completed"
    assert not any(step["status"] == "failed" for step in final["steps"]), final["steps"]
    assert assistant_answers(native_application, session_id)[-1] == "你说的图表那一页是主图还是复盘页？"


async def test_an_escalated_ui_round_reads_and_answers_but_runs_no_business_write(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
):
    """#523 P1: "打开持仓清单" whose UI round attempted no write went on as the general turn with the full
    toolbox, so one off-script stop_simulation_execution / app_call write from the model ran for real. The
    escalated turn may still read and answer; every write outside the interface scope is refused unrun."""
    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    stop_body = json.dumps({"operation": "POST /api/simulation/execution/stop", "body": {"emergency": True}})

    def scenario(text, outputs, body):
        if {tool["name"] for tool in body.get("tools") or []} == UI_FALLBACK_TOOLS:
            return responses_server._text_stream("好的。")  # no interface write: escalates
        if not outputs:
            return responses_server._tool_stream([
                ("stop_simulation_execution", {"emergency": True, "scope": "opens"}),
                ("app_call", {"requestJson": stop_body}),
                ("propose_order", legacy_card_args("create_close_batch_proposal", {})),
                ("get_positions", {}),
            ])
        return responses_server._text_stream("持仓清单里现在有沪金多单。")

    monkeypatch.setattr(responses_server, "_scenario", scenario)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "打开持仓清单那个东西", "uiWindowId": window_id})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    outputs = responses_server._tool_outputs(responses_server.requests[-1])
    codes = [output["payload"].get("errorCode") for output in outputs]
    assert codes[:3] == ["ui_scope_write"] * 3, outputs
    assert outputs[3]["payload"]["data"]["positions"], outputs[3]  # the read still runs
    assert not [event for event in events_of(native_application, session_id) if event["name"].startswith("proposal.")]
    assert assistant_answers(native_application, session_id)[-1] == "持仓清单里现在有沪金多单。"


async def test_an_escalated_ui_round_still_changes_the_users_own_settings(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
):
    """Owner 2026-10-19 (语音“打开语音免确认”说了三遍没改成): a settings sentence worded like an interface command
    (“打开…”“关闭…”) goes the UI round, finds no interface write and escalates; the escalation still refuses orders and
    switches (#523) but runs update_preferences — the user's own setting on the user's word (owner 2026-10-13)."""
    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    store = native_application.state.store

    def scenario(text, outputs, body):
        if {tool["name"] for tool in body.get("tools") or []} == UI_FALLBACK_TOOLS:
            return responses_server._text_stream("好的。")  # no interface write: escalates
        if not outputs:
            return responses_server._tool_stream([
                ("update_preferences", {"changes": {"trade": {"voiceOrderConfirm": False}}}),
                ("stop_simulation_execution", {"emergency": True, "scope": "opens"}),
            ])
        return responses_server._text_stream("语音下单免口头确认已打开。")

    monkeypatch.setattr(responses_server, "_scenario", scenario)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "打开语音免确认那个开关", "uiWindowId": window_id})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    assert {tool["name"] for tool in responses_server.requests[0].get("tools") or []} == UI_FALLBACK_TOOLS  # the UI round
    outputs = responses_server._tool_outputs(responses_server.requests[-1])
    codes = [output["payload"].get("errorCode") for output in outputs]
    assert codes[0] != "ui_scope_write" and codes[1] == "ui_scope_write", outputs  # the setting runs, the switch not
    assert store.get_preferences("local-user")["preferences"]["trade"]["voiceOrderConfirm"] is False


async def test_an_escalated_navigation_sentence_never_switches_a_trading_control(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
):
    """PR #1004 review: “打开持仓清单那个东西” escalated with a model-written patch turning 语音免确认 or AI 自主执行 —
    the sentence names neither, so the patch is refused unrun; a plain setting the model changes still runs."""
    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    store = native_application.state.store
    before = store.get_preferences("local-user")["preferences"]["trade"]

    def scenario(text, outputs, body):
        if {tool["name"] for tool in body.get("tools") or []} == UI_FALLBACK_TOOLS:
            return responses_server._text_stream("好的。")
        if not outputs:
            return responses_server._tool_stream([
                ("update_preferences", {"changes": {"trade": {"voiceOrderConfirm": False}}}),
                ("update_preferences", {"changes": {"trade": {"aiAutonomy": False}}}),
            ])
        return responses_server._text_stream("持仓清单已打开。")

    monkeypatch.setattr(responses_server, "_scenario", scenario)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "打开持仓清单那个东西", "uiWindowId": window_id})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    codes = [output["payload"].get("errorCode") for output in responses_server._tool_outputs(responses_server.requests[-1])]
    assert codes == ["ui_scope_write", "ui_scope_write"], codes
    assert store.get_preferences("local-user")["preferences"]["trade"] == before


async def test_an_interface_round_cannot_change_trading_controls_through_a_ui_command(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
):
    """#932 复核 (P1, #523 payload seam): the UI round's operation allowlist admits POST /api/app/ui/commands, and its
    `changes` could turn AI 自主执行 or 语音下单免口头确认 off, or a running strategy to 自动下单, on a sentence that only
    asked to open a panel. Those are trading controls: a UI command carrying one is refused before it is queued (nothing
    reaches the window), and the account's preferences stay as they were."""
    from uuid import uuid4

    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    store = native_application.state.store
    before = store.get_preferences("local-user")["preferences"]["trade"]

    def command(changes):
        return ("app_call", {"requestJson": json.dumps({
            "operation": "POST /api/app/ui/commands", "body": {
                "commandId": str(uuid4()), "windowId": window_id, "expectedRevision": 0, "changes": changes}})})

    def scenario(text, outputs, body):
        if not outputs:
            return responses_server._tool_stream([
                command({"preference": {"key": "trade.aiAutonomy", "value": False}}),
                command({"preference": {"key": "trade.voiceOrderConfirm", "value": False}}),
                command({"strategyInstance": {"strategyId": "strategy-x", "signalAction": "auto"}}),
            ])
        return responses_server._text_stream("好的。")

    monkeypatch.setattr(responses_server, "_scenario", scenario)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "打开持仓清单那个东西", "uiWindowId": window_id})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    outputs = responses_server._tool_outputs(responses_server.requests[1])
    assert len(outputs) == 3
    assert all(output["payload"]["data"]["uiCommandAttempted"] is False for output in outputs), outputs
    window = (await native_client.get(f"/api/app/ui/windows/{window_id}")).json()
    assert window["revision"] == 0
    assert store.get_preferences("local-user")["preferences"]["trade"] == before


@pytest.mark.parametrize("case", ["mixed", "plain", "stale", "foreign"])
async def test_a_typed_cancel_retires_the_waiting_card_through_the_card_route(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
    monkeypatch, case: str,
):
    """#756 typed (10-12 线上): “取消这张卡，切到日线” switched the chart but left the card confirmable —
    the card's 取消 route was not in the catalog, so the typed turn could only send the user to the
    button. Now `reject_proposal` takes the button's own route and Owner: the card ends rejected with
    approvalUsedAt, and the interface part runs on its receipt. A wrong version or another user's card
    is refused by the Owner and the card keeps waiting. There is no confirming tool."""
    from uuid import uuid4

    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    store = native_application.state.store
    card = native_application.state.proposals.create_trade(
        session_id, "typed-cancel", symbol="SHFE.au2612", side="buy_open", volume=1,
        price_intent="passive", reason="typed cancel")
    proposal_id = card["proposalId"]
    version = store.get(proposal_id)["version"]
    if case == "foreign":
        with store.lock, store.connection:
            store.connection.execute("UPDATE proposals SET user_id = ? WHERE id = ?", ("someone-else", proposal_id))
    pulse = _window_pulse(window_id, "交易台")
    commands: list[dict] = []
    seen: list[dict] = []

    def scenario(text, outputs, body):
        names = {tool["name"] for tool in body.get("tools") or []}
        assert "reject_proposal" in names and not any("confirm" in name for name in names)
        if not outputs:
            # The status line hands the model the waiting card's id and version.
            assert f"proposalId={proposal_id} proposalVersion={version}" in text
            return responses_server._tool_stream([("reject_proposal", {
                "proposalId": proposal_id, "proposalVersion": version + (1 if case == "stale" else 0)})])
        seen.append(outputs[-1]["payload"])
        if case == "mixed" and len(outputs) == 1:
            return responses_server._tool_stream([("app_call", {"requestJson": json.dumps({
                "operation": "POST /api/app/ui/commands", "query": {"waitMs": 3000},
                "body": {"commandId": str(uuid4()), "windowId": window_id, "expectedRevision": 0,
                         "changes": {"duration": 86400}}})})])
        return responses_server._text_stream("好的。")

    monkeypatch.setattr(responses_server, "_scenario", scenario)

    async def play():
        while True:
            result = (await native_client.post("/api/ui/pulse", json=pulse)).json()
            command = result.get("command")
            if command:
                commands.append(command["changes"])
                pulse["revision"] += 1
                pulse["state"] = {**pulse["state"], **command["changes"]}
                pulse["receipt"] = {"commandId": command["commandId"], "status": "applied"}
                await native_client.post("/api/ui/pulse", json=pulse)
                pulse.pop("receipt")
            await asyncio.sleep(0.01)

    player = asyncio.create_task(play())
    try:
        text = "取消这张卡，切到日线" if case == "mixed" else "算了"
        response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                            json={"text": text, "uiWindowId": window_id})
        assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    finally:
        player.cancel()
    record = store.get(proposal_id)
    if case in {"mixed", "plain"}:
        assert seen[0]["data"]["status"] == "rejected" and seen[0]["errorCode"] is None
        assert record["status"] == "rejected" and record["approvalUsedAt"] is not None
        audits = store.connection.execute(
            "SELECT action FROM proposal_audit WHERE proposal_id = ?", (proposal_id,)).fetchall()
        assert [row["action"] for row in audits][-1:] == ["reject"]
        result = [e["payload"] for e in events_of(native_application, session_id) if e["name"] == "proposal.result"]
        assert result and result[-1]["proposalId"] == proposal_id and result[-1]["status"] == "rejected"
        # The rejected card can no longer be confirmed — not by a click, not by the voice "确认".
        confirm = await native_client.post(f"/api/proposals/{proposal_id}/confirm", json={
            "approvalId": record["approvalId"], "proposalVersion": record["version"], "idempotencyKey": "after-typed-cancel"})
        assert confirm.status_code == 409 and confirm.json()["detail"]["code"] == "invalid_state_transition"
        assert store.get(proposal_id)["status"] == "rejected"
        if case == "mixed":
            assert commands == [{"duration": 86400}]
            assert seen[-1]["data"]["data"]["status"] == "applied"
        else:
            assert commands == []
    else:
        expected = "stale_proposal_version" if case == "stale" else "proposal_not_found"
        assert seen[0]["errorCode"] == expected and seen[0]["warning"]
        assert record["status"] == "approval_required" and record["approvalUsedAt"] is None
        assert commands == []


@pytest.mark.parametrize("text", ["取消这张卡，切到日线", "确认并切到日线", "改成限价3500，切到五分钟"])
async def test_a_card_answer_with_an_interface_tail_keeps_the_full_toolbox(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, text: str,
):
    """#756 typed: with a card waiting, a sentence that answers it and adds an interface tail is a
    general turn — the card tools stay offered, nothing forces a UI tool, no UI-only refusal."""
    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    native_application.state.proposals.create_trade(
        session_id, "waiting-card", symbol="SHFE.au2612", side="buy_open", volume=1,
        price_intent="passive", reason="mixed reply")
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": text, "uiWindowId": window_id})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    first = responses_server.requests[0]
    assert {"propose_order", "app_call", "reject_proposal"} <= {tool["name"] for tool in first["tools"]}
    # Rejecting is the only card answer a tool gives: confirming stays the user's own click / spoken 确认.
    assert not any("confirm" in tool["name"] for tool in first["tools"])
    assert "tool_choice" not in first


ESCALATED_ANSWERS = {"no_tool": "账户风险度不高，主要敞口在沪金。", "clarify": "你说的清单是持仓清单还是委托清单？"}


def test_editor_still_asking_reads_only_an_applied_receipt():
    # 11-02: a 策略编辑卡 still designing from a sentence gets the "this step is done" note; nothing else does.
    from trader_runtime.trader_tools import TraderReadOnlyTools

    def receipt(status, ask):
        return {"ok": True, "data": {"status": status, "state": {"strategyEditor": {"open": True, "ask": ask}}}}
    assert TraderReadOnlyTools._editor_still_asking(receipt("applied", {"text": "x", "status": "running"}))
    assert not TraderReadOnlyTools._editor_still_asking(receipt("applied", {"text": "x", "status": "done"}))
    assert not TraderReadOnlyTools._editor_still_asking(receipt("rejected", {"text": "x", "status": "running"}))
    assert not TraderReadOnlyTools._editor_still_asking({"ok": False, "status": 422, "data": {"detail": []}})


@pytest.mark.parametrize("case", ["no_tool", "clarify", "positions", "two_steps", "second_rejected", "pending", "highlight", "malformed_app", "invalid_highlight", "overclaim", "empty_final", "three_applied", "schema_body", "second_pending", "pending_card", "blocked_business", "stale_retry", "bare_changes"])
async def test_ui_owner_uses_real_step_receipts_without_retrying_unknown_writes(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
    monkeypatch, case: str,
):
    from uuid import uuid4

    from trader_runtime.ui_control import describe_ui_change

    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    commands: list[dict] = []
    receipts: list[dict] = []
    pulse = _window_pulse(window_id, "交易台")
    proposal_id = None
    executed = []
    execution_before = (await native_client.get("/api/ai-execution")).json() if case == "blocked_business" else None
    if case == "blocked_business":
        from trader_runtime.trader_tools import TraderReadOnlyTools
        original_call = TraderReadOnlyTools.call_async
        async def observed_call(self, *, tool, arguments, **kwargs):
            executed.append((tool, arguments))
            return await original_call(self, tool=tool, arguments=arguments, **kwargs)
        monkeypatch.setattr(TraderReadOnlyTools, "call_async", observed_call)
    service = state = waiting = None
    if case == "pending_card":
        from trader_runtime.voice_realtime import CallState, VoiceRealtimeService
        card = native_application.state.proposals.create_trade(
            session_id, "before-navigation", symbol="SHFE.au2612", side="buy_open", volume=1,
            price_intent="passive", reason="offline pending navigation")
        proposal_id = card["proposalId"]
        service = VoiceRealtimeService(native_application)
        state = CallState(window_id=window_id, chat_session_id=session_id)
        waiting = service._resolve_pending(state)
        assert waiting is not None
    wanted = [{"panel": "positions"}]
    if case in {"highlight", "invalid_highlight"}:
        wanted = [{"highlight": {"target": "positions-bar", "label": "持仓栏"}}]
    if case in {"two_steps", "second_rejected", "three_applied", "second_pending"}:
        wanted.append({"positionsTab": "orders"})
    if case == "three_applied":
        wanted.append({"positionsTab": "fills"})

    def scenario(text, outputs, body):
        if {tool["name"] for tool in body.get("tools") or []} != UI_FALLBACK_TOOLS:
            # The escalated general turn (#759/#748): full tools, a fresh history, its own answer.
            assert case in ESCALATED_ANSWERS and not outputs and commands == []
            return responses_server._text_stream(ESCALATED_ANSWERS[case])
        if case == "clarify":
            # #748: read the capabilities, then ask instead of writing.
            return responses_server._tool_stream([("app_capabilities", {})]) if not outputs else (
                responses_server._text_stream("你说的清单是哪一个？"))
        if case == "stale_retry":
            # #759 B / AI 审计 2026-10-13: a stale revision is refused before queueing (nothing ran), and
            # the Runtime itself sends the command once more against the window's latest revision.
            if not outputs:
                return responses_server._tool_stream([("app_call", {"requestJson": json.dumps({
                    "operation": "POST /api/app/ui/commands", "query": {"waitMs": 3000},
                    "body": {"commandId": str(uuid4()), "windowId": window_id,
                             "expectedRevision": 7, "changes": wanted[0]}})})])
            data = outputs[0]["payload"]["data"]
            assert data["ok"] is True and data["data"]["status"] == "applied"
            assert data["revisionRetry"]["from"] == 7 and data["revisionRetry"]["to"] == 0
            return responses_server._text_stream("持仓清单打开了。")
        if case == "bare_changes":
            # 2026-11-02: the model writes only the field; the Runtime adds commandId, the turn's window,
            # its revision and waitMs, and wraps the field into `changes` — applied on the first send.
            if not outputs:
                return responses_server._tool_stream([("app_call", {"requestJson": json.dumps({
                    "operation": "POST /api/app/ui/commands", "body": wanted[0]})})])
            data = outputs[0]["payload"]["data"]
            assert data["ok"] is True and data["data"]["status"] == "applied" and "revisionRetry" not in data
            return responses_server._text_stream("持仓清单打开了。")
        if case == "blocked_business" and not outputs:
            return responses_server._tool_stream([
                ("app_call", {"requestJson": json.dumps({"operation": "POST /api/ai-execution/start",
                    "body": {"sessionId": session_id, "symbols": ["SHFE.au2612"], "permissions": ["open"]}})}),
                ("app_call", {"requestJson": json.dumps({"operation": "POST /api/ai-execution/stop", "body": {}})}),
                ("app_call", {"requestJson": json.dumps({"operation": "POST /api/simulation/orders/{client_order_id}/cancel",
                    "path": {"client_order_id": "not-a-real-order"}})}),
                ("propose_order", legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1})),
                ("cancel_simulation_order", {"clientOrderId": "not-a-real-order"}),
                ("ask_order_details", {"question": "买还是卖？", "options": []}),
                ("activate_skill", {"name": "trade-orders"}),
            ])
        if case == "blocked_business" and len(outputs) == 7:
            assert all(o["payload"].get("success") is False for o in outputs)
            assert executed == []
        if not outputs and case == "malformed_app":
            return responses_server._tool_stream([("app_call", {"requestJson": "{broken-json"})])
        if not outputs and case == "invalid_highlight":
            return responses_server._tool_stream([("highlight_ui", {"label": "missing target"})])
        if not outputs and case == "schema_body":
            return responses_server._tool_stream([("app_call", {"requestJson": json.dumps({
                "operation": "POST /api/app/ui/commands", "body": {"windowId": window_id, "commands": [{"type": "panel", "panel": "positions"}]},
            })})])
        if case == "schema_body" and len(outputs) == 1:
            data = outputs[0]["payload"]["data"]
            assert data["status"] == 422 and data["uiCommandAttempted"] is False and commands == []
            assert "changes：缺少" in data["hint"] and "commands：不认识这个字段" in data["hint"]
        corrected = case in {"malformed_app", "invalid_highlight", "schema_body"}
        step = len(outputs) - int(corrected) - (7 if case == "blocked_business" else 0)
        if case == "no_tool" or step >= len(wanted):
            if case in {"overclaim", "empty_final"}:
                return responses_server._text_stream("全部操作都完成了。" if case == "overclaim" else "")
            if case in {"highlight", "invalid_highlight"}:
                return responses_server._text_stream("持仓栏标出来了。")
            return responses_server._text_stream("持仓清单和分页都打开了。" if case != "positions" else "持仓清单打开了。")
        if case in {"highlight", "invalid_highlight"}:
            return responses_server._tool_stream([("highlight_ui", {**wanted[0]["highlight"], "uiWindowId": window_id})])
        calls = [("app_call", {"requestJson": json.dumps({
            "operation": "POST /api/app/ui/commands", "query": {"waitMs": 0 if case == "pending" or (case == "second_pending" and step == 1) else 3000},
            "body": {"commandId": str(uuid4()), "windowId": window_id,
                     "expectedRevision": step, "changes": wanted[step]},
        })})]
        if case == "blocked_business":
            calls.insert(0, ("app_call", {"requestJson": json.dumps({"operation": "GET /api/app/ui/windows/{window_id}",
                                                                     "path": {"window_id": window_id}})}))
        return responses_server._tool_stream(calls)

    monkeypatch.setattr(responses_server, "_scenario", scenario)

    async def play():
        while True:
            result = (await native_client.post("/api/ui/pulse", json=pulse)).json()
            command = result.get("command")
            if command:
                if case == "second_pending" and commands:
                    await asyncio.sleep(0.01)
                    continue
                commands.append(command["changes"])
                rejected = case == "second_rejected" and len(commands) == 2
                if not rejected:  # a window that refuses a command changes nothing, so its revision stays
                    pulse["revision"] += 1
                    if "highlight" in command["changes"]:
                        pulse["state"]["highlights"] = [command["changes"]["highlight"]]
                    else:
                        pulse["state"] = {**pulse["state"], **command["changes"]}
                pulse["receipt"] = {"commandId": command["commandId"], "status": "rejected" if rejected else "applied"}
                await native_client.post("/api/ui/pulse", json=pulse)
                if case == "pending_card":
                    receipt = await native_client.get(f"/api/app/ui/commands/{command['commandId']}")
                    assert receipt.status_code == 200
                    receipts.append(receipt.json())
                pulse.pop("receipt")
            await asyncio.sleep(0.01)

    player = None if case == "pending" else asyncio.create_task(play())
    try:
        text = "看看持仓栏在哪里" if case in {"highlight", "invalid_highlight"} else (
            "打开持仓清单切到委托页切到成交页" if case == "three_applied" else (
                "打开持仓清单切到委托页" if len(wanted) == 2 or case in {"overclaim", "empty_final"} else (
                    "帮我看看我的模拟盘持仓风险" if case == "no_tool" else (
                        "取消这张卡，打开所有持仓清单" if case == "pending_card" else "打开所有持仓清单"))))
        if case == "pending_card":
            # #756: a card answer with an interface tail is not pure navigation — the spoken turn
            # formally rejects the waiting card first, then the interface part runs on its receipts.
            result = await service.run_tool("delegate", {"utterance": text}, state)
            assert result["ok"] is True and result["pendingConfirmation"] is False
            assert result["sessionId"] == session_id, (result, responses_server.requests)
            record = native_application.state.store.get(proposal_id)
            assert record["status"] == "rejected" and record["approvalUsedAt"] is not None
            assert state.pending is None
            assert (await service.run_tool("confirm", {}, state))["error"] == "nothing_pending"
        else:
            response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                                json={"text": text, "uiWindowId": window_id, "origin": "voice"})
            assert response.status_code == 202
            assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    finally:
        if player:
            player.cancel()
            await asyncio.gather(player, return_exceptions=True)
    answer = assistant_answers(native_application, session_id)[-1]
    if case == "blocked_business":
        assert [tool for tool, _ in executed] == ["app_call", "app_call"]
        assert [json.loads(args["requestJson"])["operation"] for _, args in executed] == [
            "GET /api/app/ui/windows/{window_id}", "POST /api/app/ui/commands"]
        assert (await native_client.get("/api/ai-execution")).json() == execution_before
        assert native_application.state.store.pending_proposals(session_id) == []
        assert not any(e["name"] == "card.order_question" for e in events_of(native_application, session_id))
    if case in ESCALATED_ANSWERS:
        # #759 A / #748: the UI round attempted no write — its prose is dropped and the same run
        # answers as a general turn (full tools) instead of a fixed line.
        assert answer == ESCALATED_ANSWERS[case]
        assert commands == [] and len(responses_server.requests) == (2 if case == "no_tool" else 3)
        assert {"get_positions", "get_account_summary"} <= {tool["name"] for tool in responses_server.requests[-1]["tools"]}
        assert not any("你说的清单是哪一个" in str(e["payload"]) for e in events_of(native_application, session_id))
    elif case == "pending":
        assert "尚未确认完成" in answer and "重复提交" in answer
        assert len(responses_server.requests) == 1
    elif case == "second_pending":
        assert "已改好：打开持仓面板" in answer and "尚未确认完成" in answer
        assert commands == wanted[:1] and len(responses_server.requests) == 2
        assert pulse["state"]["panel"] == "positions" and pulse["state"].get("positionsTab") != "orders"
    elif case == "second_rejected":
        # The refused step is named (10-13 live: “打开复盘看看昨天的” on an account with no review that day).
        assert answer.startswith("已改好：打开持仓面板。「持仓栏切到「委托」页签」没有生效")
        assert len(responses_server.requests) == 2 and commands == wanted
        assert pulse["state"]["panel"] == "positions" and pulse["state"].get("positionsTab") != "orders"
    else:
        if case in {"highlight", "invalid_highlight"}:
            assert len(commands) == 1 and len(pulse["state"]["highlights"]) == 1
            assert all(pulse["state"]["highlights"][0][key] == value for key, value in wanted[0]["highlight"].items())
        else:
            assert commands == wanted
            assert pulse["state"]["panel"] == "positions"
        if case in {"stale_retry", "bare_changes"}:
            assert answer == "已改好：打开持仓面板。"
        if case == "two_steps":
            assert pulse["state"]["positionsTab"] == "orders"
        if case in {"overclaim", "empty_final", "three_applied"}:
            assert answer == "已改好：" + "；".join(describe_ui_change(change) for change in wanted) + "。"
            assert not any("全部操作都完成了" in str(e["payload"]) for e in events_of(native_application, session_id))
        if case in {"overclaim", "empty_final"}:
            assert pulse["state"].get("positionsTab") != "orders"
        if case == "three_applied":
            assert pulse["state"]["positionsTab"] == "fills"
        assert len(responses_server.requests) == len(wanted) + 1 + int(case in {"malformed_app", "invalid_highlight", "schema_body", "blocked_business"})
    if case == "pending_card":
        assert len(receipts) == 1 and receipts[0]["status"] == "applied"
        assert receipts[0]["windowId"] == window_id and receipts[0]["state"]["panel"] == "positions"
    ui_requests = responses_server.requests[:-1] if case in ESCALATED_ANSWERS else responses_server.requests
    assert responses_server.requests[0]["tool_choice"] == "required"
    assert all("tool_choice" not in body for body in responses_server.requests[1:])
    assert all({tool["name"] for tool in body["tools"]} == UI_FALLBACK_TOOLS for body in ui_requests)
    # No card is made; only the voice-retired old card is re-published (countdown cleared, then rejected).
    assert all(e["payload"].get("proposalId") == proposal_id for e in events_of(native_application, session_id)
               if e["name"].startswith("proposal."))


async def test_a_three_step_interface_request_finishes_in_one_round_and_says_each_step(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch,
):
    """AI 审计 2026-10-13：“看看今天成交，只看AI下的”只做了第一步就回“已有 1 步界面操作确认生效”。
    界面回合的消息里带好窗口 id 与 revision（不必先读窗口），三条命令同一轮依次发出——后两条的
    revision 已经过期，Runtime 按最新版本各重发一次；回复逐条说改了什么、在哪。"""
    import re
    from uuid import uuid4

    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    pulse = _window_pulse(window_id, "交易台")
    wanted = [{"panel": "positions"}, {"positionsTab": "fills"}, {"fillsSource": "ai"}]
    commands: list[dict] = []

    def scenario(text, outputs, body):
        if outputs:
            assert all(item["payload"]["data"]["data"]["status"] == "applied" for item in outputs)
            assert [bool(item["payload"]["data"].get("revisionRetry")) for item in outputs] == [False, True, True]
            return responses_server._text_stream("都弄好了。")
        found = re.search(r"windowId=([0-9a-f-]+)，revision=(\d+)；当前交易日 (\d{4}-\d{2}-\d{2})，上一个交易日 (\d{4}-\d{2}-\d{2})", text)
        assert found and found.group(1) == window_id and found.group(3) > found.group(4)
        # Relative words ("字大一点") act on the current appearance, which the line carries too.
        assert "外观：theme=dark；fontScale=standard；density=standard。" in text
        return responses_server._tool_stream([("app_call", {"requestJson": json.dumps({
            "operation": "POST /api/app/ui/commands", "query": {"waitMs": 3000},
            "body": {"commandId": str(uuid4()), "windowId": window_id, "expectedRevision": int(found.group(2)),
                     "changes": change}})}) for change in wanted])

    monkeypatch.setattr(responses_server, "_scenario", scenario)

    async def play():
        while True:
            command = (await native_client.post("/api/ui/pulse", json=pulse)).json().get("command")
            if command:
                commands.append(command["changes"])
                pulse["revision"] += 1
                pulse["state"] = {**pulse["state"], **command["changes"]}
                pulse["receipt"] = {"commandId": command["commandId"], "status": "applied"}
                await native_client.post("/api/ui/pulse", json=pulse)
                pulse.pop("receipt")
            await asyncio.sleep(0.01)

    player = asyncio.create_task(play())
    try:
        response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                            json={"text": "看看今天成交只看AI下的", "uiWindowId": window_id})
        assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    finally:
        player.cancel()
        await asyncio.gather(player, return_exceptions=True)
    assert commands == wanted and len(responses_server.requests) == 2
    assert assistant_answers(native_application, session_id)[-1] == (
        "已改好：打开持仓面板；持仓栏切到「成交」页签；成交只看AI 下的。")


@pytest.mark.parametrize("case", ["typed", "voice_clarify", "voice_replace"])
async def test_pending_symbol_reply_keeps_card_owner_and_never_confirms_the_old_contract(
    tmp_path, responses_server, monkeypatch, case,
):
    import httpx

    from test_simulation_orders import Gateway, NormalProvider
    from trader_runtime.voice_realtime import CallState, VoiceRealtimeService

    gateway, provider = Gateway(), NormalProvider()
    reached, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def model(request):
        from trader_runtime.message_context import PENDING_CARD_REPLY
        assert PENDING_CARD_REPLY.get() is None  # queued worker uses the encoded fact, not ambient state
        reached.set()
        await release.wait()
        return await responses_server.handle(request)

    app = create_app(tmp_path / "symbol-reply.sqlite3", runtime_state_root=tmp_path,
                     local_token=TOKEN, agent_mode="native", agent_provider="deepseek",
                     deepseek_api_key=DEEPSEEK_KEY, native_transport=httpx.MockTransport(model),
                     data_provider=provider, simulation_gateway=gateway)
    service = VoiceRealtimeService(app, key_file=tmp_path / "missing-key")
    original = app.state.app_capabilities.call

    async def observe(args, **kwargs):
        if args.operation == "POST /api/app/ui/commands":
            writes.append(args.body)
            reached.set()
        return await original(args, **kwargs)
    monkeypatch.setattr(app.state.app_capabilities, "call", observe)

    def scenario(text, outputs, body):
        if case == "voice_replace" and not outputs:
            return responses_server._tool_stream([("propose_order", legacy_card_args("create_trade_proposal", {
                "symbol": "SHFE.rb2610", "side": "buy_open", "volume": 1,
                "priceIntent": "passive", "reason": "替换原卡，等待新卡确认"}))])
        return responses_server._text_stream("生成了新的螺纹卡，等待确认。" if case == "voice_replace"
                                             else ("旧卡已作废。要把合约改为螺纹吗？" if case == "voice_clarify"
                                                   else "要修改交易卡的合约，还是只切换图表？原交易卡没有修改。"))
    monkeypatch.setattr(responses_server, "_scenario", scenario)
    task = None
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test",
                               headers={"origin": ORIGIN}) as client:
            await client.post("/api/auth/local", headers={"authorization": f"Bearer {TOKEN}"})
            session_id = await create_session(client)
            window_id = await _register_window(client)
            old = app.state.proposals.create_trade(session_id, "old-card", symbol="SHFE.au2612",
                side="buy_open", volume=1, price_intent="passive", reason="old awaiting card")
            state = CallState(window_id=window_id, chat_session_id=session_id)
            service._resolve_pending(state)
            if case == "typed":
                response = await client.post(f"/api/sessions/{session_id}/messages",
                    json={"text": "换成螺纹", "uiWindowId": window_id})
                run_id = response.json()["runId"]
            else:
                provider.canonical_symbol = "SHFE.rb2610" if case == "voice_replace" else "SHFE.au2612"
                task = asyncio.create_task(service.run_tool("delegate", {"utterance": "换成螺纹"}, state))
            # The turn reaching the model (or writing the UI) is awaited as an event, not under a 1 s wall-clock bound
            # that a cold import or a loaded machine overran (#917: it failed on main as often as on the branch).
            # The turn ending first is the failure; pytest-timeout guards a hang.
            async def turn_ended() -> None:
                while app.state.store.get_run(run_id)["status"] in {"queued", "running"}:
                    await asyncio.sleep(0.01)
            arrived = asyncio.ensure_future(reached.wait())
            ended = task if task is not None else asyncio.ensure_future(turn_ended())
            await asyncio.wait({arrived, ended}, return_when=asyncio.FIRST_COMPLETED)
            arrived.cancel()
            if ended is not task:
                ended.cancel()
            assert reached.is_set(), "the turn ended without reaching the model"
            assert writes == []  # a candidate card edit must never take the fixed chart path
            if case != "typed":
                assert app.state.store.get(old["proposalId"])["status"] == "rejected"
                assert (await service.run_tool("confirm", {}, state))["error"] == "nothing_pending"
                assert gateway.submit_count == 0  # while the model is still waiting
            release.set()
            if task:
                result = await task
                assert result["ok"] is True
            else:
                assert (await wait_for_run(app, run_id))["status"] == "completed"
            assert writes == [] and len(responses_server.requests) >= 1
            assert {tool["name"] for tool in responses_server.requests[0]["tools"]} > UI_FALLBACK_TOOLS
            if case == "voice_replace":
                new = state.pending["proposals"][0]
                assert new["proposalId"] != old["proposalId"]
                record = app.state.store.get(new["proposalId"])
                assert record["payload"]["symbol"] == "SHFE.rb2610" and record["version"] == new["version"]
                assert gateway.submit_count == 0
                assert (await service.run_tool("confirm", {}, state))["ok"] is True
                assert gateway.submit_count == 1 and gateway.request["symbol"] == "SHFE.rb2610"
                assert app.state.store.get(new["proposalId"])["status"] == "candidate_recorded"
                assert (await service.run_tool("confirm", {}, state))["error"] == "nothing_pending"
                assert gateway.submit_count == 1
            elif case == "voice_clarify":
                assert "旧卡已作废" in result["content"] and not result["pendingConfirmation"]
                assert (await service.run_tool("confirm", {}, state))["error"] == "nothing_pending"
                assert gateway.submit_count == 0
                from trader_runtime.message_context import PENDING_CARD_REPLY, decode_instruction
                assert PENDING_CARD_REPLY.get() is None
                # A later turn, a different session and a different identity cannot inherit the handoff.
                other = await create_session(client)
                for target, context in ((session_id, None), (other, ("local-user", session_id)),
                                        (session_id, ("other-user", session_id))):
                    token = PENDING_CARD_REPLY.set(context)
                    try:
                        accepted = await client.post(f"/api/sessions/{target}/messages",
                            json={"text": "你好", "origin": "voice"})
                    finally:
                        PENDING_CARD_REPLY.reset(token)
                    next_run = accepted.json()["runId"]
                    assert (await wait_for_run(app, next_run))["status"] == "completed"
                    assert decode_instruction(app.state.store.get_run(next_run)["instruction"]).pending_card_reply is False
            else:
                assert "原交易卡没有修改" in assistant_answers(app, session_id)[-1]
                assert app.state.store.get(old["proposalId"])["status"] == "approval_required"
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await app.state.runs.shutdown()
        await app.state.adapter.shutdown()
        await app.state.broker.shutdown()
        app.state.store.close()


@pytest.mark.parametrize("entry", ["typed", "delegate", "asr_final"])
async def test_explicit_contract_chart_navigation_keeps_the_pending_card(
    native_client, native_application, responses_server, monkeypatch, entry,
):
    from test_voice_realtime import _scripted_call
    from trader_runtime.voice_realtime import CallState, VoiceRealtimeService

    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    old = native_application.state.proposals.create_trade(session_id, "chart-navigation",
        symbol="SHFE.au2612", side="buy_open", volume=1, price_intent="passive", reason="offline waiting card")
    service = VoiceRealtimeService(native_application)
    state = CallState(window_id=window_id, chat_session_id=session_id)
    waiting = service._resolve_pending(state)
    before = (await native_client.get(f"/api/proposals/{old['proposalId']}")).json()
    pulse = _window_pulse(window_id, "交易台")
    commands, receipts = [], []
    monkeypatch.setattr(responses_server, "_scenario", lambda *args: responses_server._text_stream("请说清楚要做什么。"))

    async def play():
        while True:
            command = (await native_client.post("/api/ui/pulse", json=pulse)).json().get("command")
            if command:
                commands.append(command["changes"])
                pulse["revision"] += 1
                pulse["state"] = {**pulse["state"], **command["changes"]}
                pulse["receipt"] = {"commandId": command["commandId"], "status": "applied"}
                assert (await native_client.post("/api/ui/pulse", json=pulse)).status_code == 200
                receipt = await native_client.get(f"/api/app/ui/commands/{command['commandId']}")
                assert receipt.status_code == 200
                receipts.append(receipt.json())
                pulse.pop("receipt")
            await asyncio.sleep(0.01)

    player = asyncio.create_task(play())
    try:
        text = "切到螺纹的图" if entry == "asr_final" else "看螺纹图"
        if entry == "typed":
            accepted = await native_client.post(f"/api/sessions/{session_id}/messages",
                                                json={"text": text, "uiWindowId": window_id})
            assert accepted.status_code == 202
            assert (await wait_for_run(native_application, accepted.json()["runId"]))["status"] == "completed"
        elif entry == "delegate":
            assert (await service.run_tool("delegate", {"utterance": text}, state))["ok"] is True
            assert state.pending is waiting
        else:
            async def on_send(payload, emit):
                if payload["type"] == "session.create":
                    await emit({"type": "session.created", "session": {"id": "s1"}})
                elif payload["type"] == "input_audio_buffer.append":
                    await emit({"type": "conversation.item.input_audio_transcription.completed", "text": text})
                elif payload["type"] == "session.close":
                    await emit({"type": "session.closed"})
            _, output, inbound, run = _scripted_call(monkeypatch, on_send)
            await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": window_id,
                                                                                   "sessionId": session_id})})
            await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
            await run(service, timeout=1, until=lambda: any(e.get("type") == "tool_result" for e in output))
            assert any(e.get("name") == "intent" and e.get("ok") for e in output)
        assert commands == [{"symbol": "SHFE.rb2610"}]
        assert len(receipts) == 1 and receipts[0]["status"] == "applied"
        assert receipts[0]["windowId"] == window_id and receipts[0]["state"]["symbol"] == "SHFE.rb2610"
        after = (await native_client.get(f"/api/proposals/{old['proposalId']}")).json()
        assert after["status"] == "approval_required" and after["approvalUsedAt"] is None
        assert all(after[key] == before[key] for key in ("version", "approvalId", "approvalExpiresAt"))
        assert responses_server.requests == []  # no business model or proposal tools on explicit navigation
    finally:
        player.cancel()
        await asyncio.gather(player, return_exceptions=True)


# --- 思考不进正文、现状只按本轮读回（2026-10-13 夜盘 P1）----------------------------------------------

_THINKING_WORDS = ("Wait", "用户此前", "不要赘述", "保持简短", "重新组织", "<think>", "我再想想")


async def test_thinking_written_into_the_answer_never_reaches_the_screen_the_voice_or_the_history(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """22:27 线上：“3050 那张单撤了吧”的正文里出现了 Wait — 用户此前… 重新组织：…。思考通道本身是分开的，
    是模型把自言自语写进了正文：流式不外发标记之后的内容，收尾时禁工具重写一轮，重写干净就用重写。"""
    session_id = await create_session(native_client)
    seen: list[dict] = []
    adapter = native_application.state.adapter
    unsubscribe = adapter.subscribe_turn_text(session_id, seen.append)
    published: list[tuple[str, bool]] = []
    original_publish = adapter._publish

    async def spy(session, kind, name, payload, *, notify=True):
        if name == "message.delta":
            published.append((str(payload.get("text")), notify))
        await original_publish(session, kind, name, payload, notify=notify)

    adapter._publish = spy
    try:
        run_id = await start_run(native_client, session_id, "思考外泄：3050 那张单撤了吧")
        assert (await wait_for_run(native_application, run_id))["status"] == "completed"
        await wait_for_event(native_application, session_id, "message.completed", role="assistant")
    finally:
        unsubscribe()
        adapter._publish = original_publish
    assert assistant_answers(native_application, session_id)[-1] == "3050 那张螺纹多单已撤销，没成交。"
    # Every streamed update (what the screen and the voice listener read) and every persisted event.
    streamed = [item["text"] for item in seen if item["role"] == "assistant"]
    persisted = [json.dumps(event["payload"], ensure_ascii=False) for event in events_of(native_application, session_id)]
    assert streamed and not any(word in text for text in streamed + persisted for word in _THINKING_WORDS)
    # The screen drops the suspect draft for the placeholder once thinking shows (the voice listener only
    # stops receiving the draft); the rewrite then streams and completes.
    from trader_runtime.native_agent import THINKING_PLACEHOLDER
    assert (THINKING_PLACEHOLDER, False) in published
    assert all(text != THINKING_PLACEHOLDER for text in streamed)
    # One rewrite round, tools off; the leaked draft is not part of its input.
    rewrites = [request for request in responses_server.requests
                if "把思考过程写进了正文" in json.dumps(request.get("input"), ensure_ascii=False)]
    assert len(rewrites) == 1 and rewrites[0]["tool_choice"] == "none"
    assert "Wait" not in json.dumps(rewrites[0]["input"], ensure_ascii=False)
    # The history the next turn sees holds the clean answer only.
    run_id = await start_run(native_client, session_id, "第二轮引用")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    await wait_for_event(native_application, session_id, "message.completed",
                         text="上一轮我说过：3050 那张螺纹多单已撤销，没成交。")


async def test_a_rewrite_that_still_thinks_aloud_ends_in_the_fixed_line_and_think_blocks_drop_silently(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    from trader_runtime.native_agent import THINKING_FALLBACK_LINE
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "思考外泄两次：3050 那张单撤了吧")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    await wait_for_event(native_application, session_id, "message.completed", text=THINKING_FALLBACK_LINE)
    before = len(responses_server.requests)
    run_id = await start_run(native_client, session_id, "思考外泄think块")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    await wait_for_event(native_application, session_id, "message.completed", text="3050 那张单已撤销。")
    assert len(responses_server.requests) == before + 1  # a closed <think> block needs no rewrite
    persisted = [json.dumps(event["payload"], ensure_ascii=False) for event in events_of(native_application, session_id)]
    assert not any(word in text for text in persisted for word in _THINKING_WORDS)


def test_ordinary_answers_are_not_mistaken_for_thinking() -> None:
    from trader_runtime.native_agent import visible_answer
    for answer in ("螺纹、热卷等等都在跌。", "受邀用户首次登录要填邀请码。", "止损 3076 已撤销，现在没有生效的保护。",
                   "结论：偏空。\n\n现在怎么做：反弹到 3100 不过再空。", "如果用户之前设置了止损，平仓后会自动撤销。",
                   "回复偏好是保持简短，所以只给结论。", "Actually, your order is still working.",
                   "结论：偏多。Let me know 价位再说。"):
        assert visible_answer(answer) == (answer, False)
    assert visible_answer("已撤销。Wait — 用户此前说过") == ("已撤销。", True)
    assert visible_answer("已撤销。用户此前设过止损，不要赘述。") == ("已撤销。", True)
    assert visible_answer("Order cancelled. Wait, I should recheck") == ("Order cancelled.", True)
    assert visible_answer("<think>只想了没写</think>") == ("", True)  # nothing answered: a leak, never "任务已完成"


async def test_a_sentence_about_positions_orders_or_protections_gets_the_account_read_now(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """22:31 线上：撤止盈后 AI 说“只剩 3076 那条止损还生效”，那条 22:30:54 已触发。说到持仓/挂单/保护的回合，
    Runtime 附上现场读的现状（持仓、挂着的委托、仍生效的保护），模型讲剩余只照它，不照之前说过的。"""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "螺纹的止盈撤掉，不要了")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert _model_saw(responses_server, "账户现状（系统本轮现场读取")
    assert _model_saw(responses_server, "仍生效的保护：无")
    assert _model_saw(responses_server, "不按之前对话里说过的")
    assert _model_saw(responses_server, "北京时间 ")
    for sentence in ("那张单还在吗", "我空仓了吗"):
        run_id = await start_run(native_client, session_id, sentence)
        assert (await wait_for_run(native_application, run_id))["status"] == "completed"
        assert _model_saw(responses_server, "账户现状（系统本轮现场读取")
    run_id = await start_run(native_client, session_id, "只做只读策略分析")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    assert not _model_saw(responses_server, "账户现状（系统本轮现场读取")


# --- #872 写后读回失败：此前的账户读取不再背书现状 ----------------------------------------

_ACCOUNT_QUESTION = "撤掉螺纹的止盈，再报一下现在的持仓"  # a question form (“…还剩几手？”) is a no-card turn: no protection write (#952)
_ACCOUNT_ANSWER = "止盈已撤，当前持仓还是 2 手。"


def _account_read(volume: int) -> dict:
    return {"available": True, "asOf": "2026-10-14T13:30:00+00:00", "asOfLocal": "21:30:00",
            "positions": [{"symbol": "SHFE.rb2610", "direction": "long", "volume": volume}],
            "workingOrders": [], "protections": []}


_ACCOUNT_UNREADABLE = {"available": False, "asOf": "2026-10-14T13:30:05+00:00", "asOfLocal": "21:30:05",
                       "rule": "持仓/挂单/保护的现状这次读不到：不要陈述剩余持仓、挂单或保护，需要时请用户看持仓面板。"}


async def _account_turn(native_client: AsyncClient, native_application: FastAPI, monkeypatch, caplog,
                        text: str, reads: list[dict], origin: str | None = None) -> tuple[str, dict, list[str]]:
    """One turn with the account reads scripted in order (the opening state line, then each write's
    re-read): returns the session, the turn's closing log record and the stop ids actually cancelled."""
    tools = native_application.state.adapter.trader_tools
    queue = list(reads)
    cancelled: list[str] = []

    async def account_state(user_id, *, mode):
        return queue.pop(0)

    def cancel_stop(*, user_id, stop_id):
        cancelled.append(stop_id)
        return {"stopId": stop_id, "symbol": "SHFE.rb2610", "positionDirection": "long", "positionBucket": "today",
                "volume": 2, "remainingVolume": 2, "triggerPrice": None, "takeProfitPrice": 3078.0,
                "source": "manual", "status": "CANCELLED"}

    monkeypatch.setattr(tools, "account_state", account_state)
    monkeypatch.setattr(tools.autonomy, "cancel_stop", cancel_stop)
    caplog.set_level(logging.INFO, logger="uvicorn.error.agent")
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": text, **({"origin": origin} if origin else {})})
    assert response.status_code == 202
    run_id = response.json()["runId"]
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    await wait_for_event(native_application, session_id, "message.completed", role="assistant")
    assert queue == []  # every scripted read was consumed: no extra or missing account read
    line = next(record.getMessage() for record in caplog.records
                if record.getMessage().startswith(f"agent_turn run_id={run_id} "))
    closing = {"evidence": json.loads(line.split(" evidence=", 1)[1].split(" account_reads=", 1)[0]),
               "account_reads": json.loads(line.split(" account_reads=", 1)[1])}
    return session_id, closing, cancelled


def _turn_view(native_application: FastAPI, session_id: str) -> tuple[list[str], list[str], str]:
    events = events_of(native_application, session_id)
    deltas = [str(event["payload"].get("text") or "") for event in events if event["name"] == "message.delta"]
    names = [(event["name"], event["payload"].get("title") or event["payload"].get("role")) for event in events]
    warnings = [title for name, title in names if name == "warning"]
    answer = assistant_answers(native_application, session_id)[-1]
    # The account footnote, when there is one, lands before the answer.
    if warnings:
        assert names.index(("warning", warnings[0])) < len(names) - 1 - names[::-1].index(("message.completed", "assistant"))
    return deltas, warnings, answer


async def test_a_write_whose_re_read_fails_retires_the_earlier_account_read(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch, caplog,
):
    """#872 t0–t3：开头读到持仓 2 手（t0）→ 撤止盈成功（t1）→ 写后重读失败 accountAfter.available=false（t2）→
    模型仍写“当前持仓还是 2 手”（t3）。撤销本身照常算成功、只撤一次；但开头那份不再背书现状：后续请求里换成“操作后
    没读到、开头那份已过时”，正文不流式放出，答案前有确定的“账户现状未能确认”脚注；证据历史仍留开头那次。"""
    session_id, closing, cancelled = await _account_turn(
        native_client, native_application, monkeypatch, caplog, "读回场景：" + _ACCOUNT_QUESTION,
        [_account_read(2), _ACCOUNT_UNREADABLE])
    assert cancelled == ["stop-take-a"]
    conversation = [request for request in responses_server.requests if request.get("tools")]
    first, follow_up = (json.dumps(request["input"], ensure_ascii=False) for request in conversation[-2:])
    assert "账户现状（系统本轮现场读取" in first and "SHFE.rb2610" in first
    assert "账户现状（系统本轮现场读取" not in follow_up
    assert "操作后没能重新读到账户" in follow_up and "也不要说没有" in follow_up
    deltas, warnings, answer = _turn_view(native_application, session_id)
    assert not any("止盈已撤" in delta for delta in deltas)
    assert warnings == ["账户现状未能确认"]
    footnote = next(event["payload"] for event in events_of(native_application, session_id) if event["name"] == "warning")
    assert "操作已执行" in footnote["message"] and footnote["deterministic"] is True
    assert answer == _ACCOUNT_ANSWER  # the text is not filtered by words; the footnote says what is unknown
    assert [record["tool"] for record in closing["evidence"]] == ["account_state"]  # history kept for the audit
    assert [(read["source"], read["available"]) for read in closing["account_reads"]] == [
        ("account_state", True), ("cancel_simulation_stop_loss", False)]
    # The write itself is reported done, not as a failed cancel.
    steps = next(event["payload"]["steps"] for event in reversed(events_of(native_application, session_id))
                 if event["name"] in {"task.completed", "task.progress"} and event["payload"].get("steps"))
    assert any(step.get("tool") == "cancel_simulation_stop_loss" and step.get("status") == "done" for step in steps)


async def test_a_second_write_whose_re_read_fails_retires_the_first_good_re_read(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch, caplog,
):
    """#872 同根序列：开头读成功 → 写 A 读回成功 → 写 B 成功但读回失败：A 的读回不背书 B 之后的现状。"""
    session_id, closing, cancelled = await _account_turn(
        native_client, native_application, monkeypatch, caplog, "读回场景〔两写〕：" + _ACCOUNT_QUESTION,
        [_account_read(2), _account_read(2), _ACCOUNT_UNREADABLE])
    assert cancelled == ["stop-take-a", "stop-take-b"]
    assert _model_saw(responses_server, "操作后没能重新读到账户")
    assert not _model_saw(responses_server, "账户现状（系统本轮现场读取")
    deltas, warnings, _ = _turn_view(native_application, session_id)
    assert warnings == ["账户现状未能确认"] and not any("止盈已撤" in delta for delta in deltas)
    assert [record["tool"] for record in closing["evidence"]] == ["account_state", "cancel_simulation_stop_loss"]
    assert [read["available"] for read in closing["account_reads"]] == [True, True, False]


async def test_good_re_reads_carry_the_account_and_an_unreadable_start_is_restored_by_a_later_read(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch, caplog,
):
    """#872 正向对照：①两次写后都读回成功——照常流式、无脚注，开头那份换成“以最近一次读回为准”（日志仍留两次读回）；
    ②开头读失败、写后读回成功——以后者为准，“读不到”那行不再留在请求里；③写后读失败、下一次写后读回成功——恢复；
    之后分别读持仓与委托不恢复（两半不是同一切面）。"""
    session_id, closing, _ = await _account_turn(
        native_client, native_application, monkeypatch, caplog, "读回场景〔两写〕：" + _ACCOUNT_QUESTION,
        [_account_read(2), _account_read(2), _account_read(1)])
    deltas, warnings, answer = _turn_view(native_application, session_id)
    assert warnings == [] and any("止盈已撤" in delta for delta in deltas), deltas
    assert _model_saw(responses_server, "以本轮最近一次写操作工具结果里的 accountAfter 为准")
    assert not _model_saw(responses_server, "账户现状（系统本轮现场读取")
    assert [record["tool"] for record in closing["evidence"]] == [
        "account_state", "cancel_simulation_stop_loss", "cancel_simulation_stop_loss"]

    session_id, closing, _ = await _account_turn(
        native_client, native_application, monkeypatch, caplog, "读回场景：" + _ACCOUNT_QUESTION,
        [_ACCOUNT_UNREADABLE, _account_read(2)])
    _, warnings, _ = _turn_view(native_application, session_id)
    assert warnings == []
    assert _model_saw(responses_server, "以本轮最近一次写操作工具结果里的 accountAfter 为准")
    assert not _model_saw(responses_server, "账户现状本轮读不到")

    # A failed re-read is restored only by a later whole-account snapshot (the next write's accountAfter)…
    session_id, closing, _ = await _account_turn(
        native_client, native_application, monkeypatch, caplog, "读回场景〔两写〕：" + _ACCOUNT_QUESTION,
        [_account_read(2), _ACCOUNT_UNREADABLE, _account_read(1)])
    _, warnings, _ = _turn_view(native_application, session_id)
    assert warnings == []
    assert [read["available"] for read in closing["account_reads"]] == [True, False, True]
    assert _model_saw(responses_server, "以本轮最近一次写操作工具结果里的 accountAfter 为准")
    # …never by get_positions + get_account_activity: two halves, not one snapshot (#900 review).
    session_id, closing, _ = await _account_turn(
        native_client, native_application, monkeypatch, caplog, "读回场景〔补读〕：" + _ACCOUNT_QUESTION,
        [_account_read(2), _ACCOUNT_UNREADABLE])
    _, warnings, _ = _turn_view(native_application, session_id)
    assert warnings == ["账户现状未能确认"]
    assert [read["source"] for read in closing["account_reads"]] == ["account_state", "cancel_simulation_stop_loss"]
    assert _model_saw(responses_server, "操作后没能重新读到账户")


async def test_an_unreadable_account_keeps_the_market_evidence_and_says_the_account_is_unconfirmed(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch, caplog,
):
    """#872 混合：本轮读了行情也撤了止盈、写后读回失败——行情证据保留（不出“本轮未读取实时数据”），同时明确账户现状
    未确认；一次行情读取遮不住账户读回失败。"""
    session_id, closing, _ = await _account_turn(
        native_client, native_application, monkeypatch, caplog, "读回场景〔报价〕：" + _ACCOUNT_QUESTION,
        [_account_read(2), _ACCOUNT_UNREADABLE])
    _, warnings, _ = _turn_view(native_application, session_id)
    assert warnings == ["账户现状未能确认"]
    assert "get_market_snapshot" in [record["tool"] for record in closing["evidence"]]


async def test_a_voice_turn_says_the_account_is_unconfirmed_before_the_answer(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch, caplog,
):
    """#900 审查：语音听不到提示卡——念的那段（第一段与 spoken）以同一句开头，不只发 warning 事件。"""
    session_id, _, _ = await _account_turn(
        native_client, native_application, monkeypatch, caplog, "读回场景：" + _ACCOUNT_QUESTION,
        [_account_read(2), _ACCOUNT_UNREADABLE], origin="voice")
    answer = [event["payload"] for event in events_of(native_application, session_id)
              if event["name"] == "message.completed" and event["payload"].get("role") == "assistant"][-1]
    assert answer["text"].startswith("账户现状没能重新读到") and answer["spoken"].startswith("账户现状没能重新读到")


async def test_the_account_note_comes_before_the_market_fallback_too(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer, monkeypatch, caplog,
):
    """#900 审查：同一回合写后读回失败、行情上下文又不可用——行情兜底答案之前照样有账户提示，语音也念。"""
    session_id, _, _ = await _account_turn(
        native_client, native_application, monkeypatch, caplog, "读回场景〔行情坏〕：" + _ACCOUNT_QUESTION,
        [_account_read(2), _ACCOUNT_UNREADABLE], origin="voice")
    _, warnings, _ = _turn_view(native_application, session_id)
    answer = [event["payload"] for event in events_of(native_application, session_id)
              if event["name"] == "message.completed" and event["payload"].get("role") == "assistant"][-1]
    assert warnings == ["账户现状未能确认"] and "实时行情还没就绪" in answer["text"]
    assert answer["spoken"].startswith("账户现状没能重新读到")


def test_a_write_through_the_capability_catalog_counts_as_the_same_write() -> None:
    """#900 审查：内置 AI 经 app_call 的 tool:cancel_simulation_stop_loss 做同一写操作——结果是原生工具的回报套在
    app_call 回报里（agent_api 的 {ok, status, operation, data} 外壳）；它的 accountAfter 与直接调用一样进末次账户读取。"""
    from trader_runtime.native_agent import NativeAgentAdapter
    from trader_runtime.trader_tools import TraderReadOnlyTools, TraderToolResult

    def wrap(tool: str, data: dict, ok: bool = True) -> dict:
        return TraderReadOnlyTools._response(None, TraderToolResult(tool=tool, data=data), ok)  # type: ignore[arg-type]

    def through_catalog(available: bool | None, *, ok: bool = True, operation: str = "tool:cancel_simulation_stop_loss"):
        inner = wrap("cancel_simulation_stop_loss", {"status": "CANCELLED", **(
            {"accountAfter": {"available": available}} if available is not None else {})})
        result = {"ok": ok, "status": 200 if ok else 409, "operation": operation, "environment": "simulation",
                  "data": json.loads(inner["contentItems"][0]["text"])}
        arguments = {"requestJson": json.dumps({"operation": operation, "body": {"stopId": "stop-take-a"}})}
        return arguments, wrap("app_call", result, ok)

    read = NativeAgentAdapter._account_write_read
    assert read("app_call", *through_catalog(False)) is False
    assert read("app_call", *through_catalog(True)) is True
    assert read("app_call", *through_catalog(None)) is False  # no re-read at all: not a current account
    assert read("app_call", *through_catalog(False, ok=False)) is None  # the write did not run
    assert read("app_call", *through_catalog(False, operation="tool:get_positions")) is None  # not a write
    assert read("app_call", {"requestJson": "not json"}, wrap("app_call", {})) is None
    direct = wrap("cancel_simulation_stop_loss", {"accountAfter": {"available": False}})
    assert read("cancel_simulation_stop_loss", {}, direct) is False


# --- 回答卡片化 (2026-10-14) ------------------------------------------------------

async def test_an_analysis_turn_gets_the_answer_card_with_only_figures_it_read_back(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "回答卡场景 沪金走势咋样")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    await wait_for_event(native_application, session_id, "agent.result")
    # The answer card was offered on this analysis question, and asked for in the turn's own text.
    first = next(request for request in responses_server.requests if "回答卡场景" in json.dumps(request["input"], ensure_ascii=False))
    assert "answer_card" in {tool["name"] for tool in first["tools"]}
    assert "answer_card 出回答卡" in json.dumps(first["input"], ensure_ascii=False)
    events = events_of(native_application, session_id)
    answer = next(event for event in events if event["name"] == "card.answer")["payload"]
    # 去重: a compare card shows its two columns only (the price cell its column shows goes, an unknown field key is
    # dropped), a line with a number of its own is dropped, and a reason keeps the cause without the price shown.
    assert answer["figures"] == []
    assert len(answer["compare"]["left"]["lines"]) == 1 and answer["compare"]["left"]["lines"][0].startswith("站在最新价 ")
    assert answer["reasons"] == ["多头排列（5 分钟线 MA5 高于 MA20）", "回踩有支撑"]
    assert answer["symbol"] == "SHFE.au2612" and [a["label"] for a in answer["actions"]] == ["看图表", "问 AI 怎么办"]
    assert answer["basis"]["sources"][:2] == ["行情快照 au2612", "走势分析 au2612"]
    # The reads became their own cards (layer one), one per kind — and the answer card folds both into its 依据.
    names = [event["name"] for event in events]
    assert names.count("card.market") == 1
    market = next(event for event in events if event["name"] == "card.market")["payload"]
    analysis = next(event for event in events if event["name"] == "card.data")["payload"]
    assert analysis["kind"] == "analysis" and analysis["cardId"].startswith("data-analysis-")
    assert answer["folded"] == [market["cardId"], analysis["cardId"]]
    # Cells were dropped, yet the card (its conclusion backed) closes the turn: no correction round (token 收敛).
    turn_requests = [request for request in responses_server.requests if "回答卡场景" in json.dumps(request["input"], ensure_ascii=False)]
    assert len(turn_requests) == 2
    assert assistant_answers(native_application, session_id)[-1] == "短线偏强，回踩不破可以继续看多。"
    row = native_application.state.store.turn_usage(user_id="local-user")[0]
    assert row["requests"] == 2 and row["card_count"] == 1 and row["numeric_text"] == 0  # the folded cards do not count


async def test_an_answer_card_whose_conclusion_was_not_read_goes_back_to_the_model(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "结论无出处场景 沪金走势咋样")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    await wait_for_event(native_application, session_id, "agent.result")
    turn_requests = [request for request in responses_server.requests if "结论无出处场景" in json.dumps(request["input"], ensure_ascii=False)]
    assert len(turn_requests) == 3
    # 接受链 (#754): a conclusion with a number nothing read is no card at all — the model is told and answers in text.
    events = events_of(native_application, session_id)
    assert "card.answer" not in [event["name"] for event in events]
    outputs = [item["output"] for item in turn_requests[-1]["input"] if item.get("type") == "function_call_output"]
    assert "里有数字" in outputs[-1]
    # The model's correction then trims to two sentences under the quote card.
    assert assistant_answers(native_application, session_id)[-1] == "沪金短线偏强。回踩不破可以继续看多。"


async def test_a_card_the_broker_refuses_is_not_counted_as_shown(native_application: FastAPI, monkeypatch):
    """#878 review: a card enters the turn's shown cards only after the broker validated and stored it."""
    from types import SimpleNamespace

    adapter = native_application.state.adapter
    monkeypatch.setitem(adapter._turns, "run-refused", SimpleNamespace(turn_id="turn-refused", routine=None))

    async def refuse(*_args, **_kwargs):
        raise ValueError("invalid_event")

    monkeypatch.setattr(adapter.broker, "publish", refuse)
    with pytest.raises(ValueError):
        await adapter._publish("s", "artifact", "card.data", {"turnId": "turn-refused", "cardId": "data-x"})
    assert "turn-refused" not in adapter._turn_card_ids


def test_partial_reads_keep_the_text_after_an_answer_card_and_unsettled_reads_are_read_again() -> None:
    from types import SimpleNamespace

    from trader_runtime.answer_cards import TurnCards
    from trader_runtime.native_agent import NativeAgentAdapter

    cards = TurnCards("t")
    cards.data_card("get_market_board", {}, {"rows": [{"instrumentId": f"x{i}", "changePercent": 0.1} for i in range(15)]},
                    account_word="")
    card, _, _ = cards.answer_card({"title": "板块", "conclusion": "普涨。"})
    cards.accept(card)
    turn = SimpleNamespace(cards=cards, routine=None, focus=None, route=None)
    shown = {"card.data|data-quotes-t", "card.answer|answer-t"}
    assert NativeAgentAdapter._read_cards_only(turn, shown) is False  # 3 of 15 rows are on no card (#754)
    cards.partial = 0
    assert NativeAgentAdapter._read_cards_only(turn, shown) is True

    settled = NativeAgentAdapter._read_settled
    pending = json.dumps({"data": {"events": [], "screen": {"pending": 3}}})
    assert not settled("get_news_flash", {"product": "SHFE.au"}, pending)
    assert not settled("get_foreign_markets", {}, json.dumps({"data": {"status": "unavailable", "items": []}}))
    assert settled("get_foreign_markets", {}, json.dumps({"data": {"status": "ok", "items": []}}))
    assert not settled("get_chart_bars", {}, "{}")  # the window's chart: another one after a switch
    assert settled("get_chart_bars", {"symbol": "SHFE.au2612", "durationSeconds": 300}, "{}")


async def test_a_reversed_reading_on_the_answer_card_goes_back_to_the_model(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """10-14 19:05: 卡上写“缩量”而依据的量比 > 1。结构化后读数是键，断言与读数表不符的结论整卡退回。"""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "定性核对场景 沪金走势咋样")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    await wait_for_event(native_application, session_id, "agent.result")
    answer = next(event for event in events_of(native_application, session_id) if event["name"] == "card.answer")["payload"]
    analysis = next(event for event in events_of(native_application, session_id) if event["name"] == "card.data")["payload"]
    ratio = next(f["value"] for f in analysis["figures"] if f["label"] == "量比")
    right = "放量" if float(ratio) > 1 else "缩量"
    # The reversed reading refused the first card; the second renders the reading from the 读数表.
    assert answer["conclusion"] == f"沪金{right}（5 分钟线 量比 {float(ratio):.2f}）"
    assert answer["reasons"] == ["资金回流"]  # the reason with the reversed key went
    assert answer["risk"].startswith("若继续")  # the risk line may name a reading word
    assert assistant_answers(native_application, session_id)[-1] == answer["conclusion"]
    # The model was told the tools' labels and the 对昨结算 rule up front (the answer_card definition, 10-17).
    first = next(request for request in responses_server.requests if "定性核对场景" in json.dumps(request["input"], ensure_ascii=False))
    card_tool = next(tool for tool in first["tools"] if tool["name"] == "answer_card")
    assert "dayMove" in card_tool["description"] and "对昨结算" in card_tool["description"]


async def test_a_turn_is_told_the_beijing_time_and_that_tool_times_are_utc(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """#932 P2: 00:25 was told as “约 16:25” — the model read a tool's UTC capturedAt as Beijing time."""
    from datetime import UTC, datetime, timedelta, timezone
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "报时场景 沪金多少钱")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    first = next(request for request in responses_server.requests if "报时场景" in json.dumps(request["input"], ensure_ascii=False))
    text = json.dumps(first["input"], ensure_ascii=False)
    beijing = datetime.now(UTC).astimezone(timezone(timedelta(hours=8)))
    assert f"现在是北京时间 {beijing:%Y-%m-%d}" in text and "+00:00 或 Z 的时间是 UTC" in text


async def test_the_same_read_twice_in_a_turn_is_not_read_again(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "同轮重读场景 沪金多少钱")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    await wait_for_event(native_application, session_id, "agent.result")
    outputs = [item["output"] for item in responses_server.requests[-1]["input"] if item.get("type") == "function_call_output"]
    assert len(outputs) == 2 and '"price"' in outputs[0] and "本轮已经读过" in outputs[1] and '"price"' not in outputs[1]
    events = events_of(native_application, session_id)
    assert [event["name"] for event in events].count("card.market") == 1
    steps = next(event for event in reversed(events) if event["name"] == "task.completed")["payload"]["steps"]
    assert [step["detail"] for step in steps if step.get("tool") == "get_market_snapshot"] == ["已完成", "本轮已读过"]


async def test_a_clean_answer_card_closes_the_turn_without_another_model_request(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    before = len(responses_server.requests)
    run_id = await start_run(native_client, session_id, "收尾出卡场景 沪金走势咋样")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    await wait_for_event(native_application, session_id, "agent.result")
    turn_requests = [request for request in responses_server.requests[before:] if not request.get("text")]
    calls = [item["name"] for request in turn_requests for item in request["input"] if item.get("type") == "function_call"]
    # read round → card round → done: no request carries the answer_card call back to the model.
    assert "answer_card" not in calls and len(turn_requests) == 2
    assert assistant_answers(native_application, session_id)[-1] == "沪金短线偏强。"
    row = native_application.state.store.turn_usage(user_id="local-user")[0]
    assert row["requests"] == 2 and row["first_text_ms"] is not None
    assert row["card_count"] == 1 and row["numeric_text"] == 0  # the answer card (the quote card is in its 依据)


async def test_a_read_becomes_a_data_card_and_quotes_of_one_turn_share_one_card(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "持仓卡场景")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    await wait_for_event(native_application, session_id, "agent.result")
    card = next(event for event in events_of(native_application, session_id) if event["name"] == "card.data")["payload"]
    assert card["kind"] == "positions" and card["rows"][0]["primary"] == "au2612 多 2 手"
    # The positions card carries the facts: the answer keeps its conclusion.
    assert assistant_answers(native_application, session_id)[-1] == "你有三个持仓，整体浮盈。沪金 2 手浮盈 2670 元。"
    # No answer card offered on a positions question: the request carries no extra definition.
    assert all("answer_card" not in {tool["name"] for tool in request.get("tools") or []}
               for request in responses_server.requests)

    session_id = await create_session(native_client)  # a fresh turn: the last turn's tool outputs ride along
    run_id = await start_run(native_client, session_id, "两个合约报价")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    await wait_for_event(native_application, session_id, "agent.result", taskId=run_id)
    quotes = [event["payload"] for event in events_of(native_application, session_id) if event["name"] == "card.market"]
    assert len({quote["cardId"] for quote in quotes}) == 1
    assert quotes[-1]["symbol"] == "SHFE.AU2612" and [peer["symbol"] for peer in quotes[-1]["peers"]] == ["CFFEX.IF2609"]


async def test_a_prose_answer_with_figures_and_no_card_is_counted(
    native_client: AsyncClient, native_application: FastAPI, caplog,
):
    session_id = await create_session(native_client)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        run_id = await start_run(native_client, session_id, "纯文字报数")
        assert (await wait_for_run(native_application, run_id))["status"] == "completed"
        await wait_for_event(native_application, session_id, "agent.result")
    assert any("agent_card_gap" in record.getMessage() and run_id in record.getMessage() for record in caplog.records)
    row = native_application.state.store.turn_usage(user_id="local-user")[0]
    assert row["numeric_text"] == 1 and row["card_count"] == 0
    response = await native_client.get("/api/app/answer-cards/coverage", params={"days": 1})
    assert response.status_code == 200
    stats = response.json()
    assert stats["numericTurns"] == 1 and stats["numericWithoutCard"] == 1 and stats["cardRate"] == 0
    assert stats["recentGaps"][0]["runId"] == run_id and stats["byRoute"][0]["withoutCard"] == 1

"""Typed-turn token slimming (2026-10-14): what each model request carries.

A general turn used to resend every tool definition (~21k tokens), an 8k system prompt, a 34k
capability catalog once read, and UI rounds the whole UiCommand schema (~16k). These tests pin the
slimmer contract through the public HTTP surface with the fake Responses server: deferred tools mount
by skill and unmount when idle, the embedded catalog is a compact index with details on request, UI
rounds carry the compact command index, and a delegated voice turn sends a short context.
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from httpx import AsyncClient

from fixtures.fake_responses_server import FakeResponsesServer
from test_native_agent import (  # noqa: F401  (fixtures)
    _fast_research_deadlines,
    _register_window,
    create_session,
    native_application,
    native_client,
    responses_server,
    start_run,
    wait_for_run,
)
from trader_runtime.native_agent import SKILL_MOUNT_IDLE_TURNS, VOICE_HISTORY_TURNS, wire_schema
from trader_runtime.turn_router import UI_FALLBACK_TOOLS


def _tool_names(request: dict) -> set[str]:
    return {tool["name"] for tool in request.get("tools") or []}


async def test_deferred_tools_mount_by_the_sentences_words_and_unmount_after_idle_turns(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """A general turn carries only the tools not deferred; a sentence using a skill's mount-words mounts
    that skill (its tools and its manual) for the turn without an activate_skill round, and the skill
    leaves the requests again once SKILL_MOUNT_IDLE_TURNS turns pass without it."""
    session_id = await create_session(native_client)

    async def turn(text: str) -> dict:
        run_id = await start_run(native_client, session_id, text)
        assert (await wait_for_run(native_application, run_id))["status"] == "completed"
        return responses_server.requests[-1]

    plain = await turn("只做只读策略分析")
    assert "create_price_alert" not in _tool_names(plain) and "get_positions" in _tool_names(plain)
    assert '<skill_content name="price-alerts">' not in plain["instructions"]
    assert "*- price-alerts:" in plain["instructions"]  # marked: it has tools to mount
    mounted = await turn("螺纹到了3150帮我提醒一下")
    assert {"create_price_alert", "list_price_alerts", "cancel_price_alert"} <= _tool_names(mounted)
    assert '<skill_content name="price-alerts">' in mounted["instructions"]
    assert "*- price-alerts:" not in mounted["instructions"]
    kept = [await turn("随便聊聊") for _ in range(SKILL_MOUNT_IDLE_TURNS)]
    assert all("create_price_alert" in _tool_names(request) for request in kept)
    gone = await turn("随便聊聊")
    assert "create_price_alert" not in _tool_names(gone)
    assert '<skill_content name="price-alerts">' not in gone["instructions"]


async def test_activate_skill_mounts_its_tools_for_the_next_round_of_the_same_turn(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "挂上那个技能")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    first, second = responses_server.requests[-2:]
    assert "create_price_alert" not in _tool_names(first)
    assert "create_price_alert" in _tool_names(second)
    assert '<skill_content name="price-alerts">' in second["instructions"]
    output = next(item["output"] for item in second["input"] if item.get("type") == "function_call_output")
    # The manual rides the instructions; the tool output only says what was mounted (no second copy).
    assert "已挂上" in output and "create_price_alert" in output and "<skill_content" not in output


async def test_calling_any_tool_of_a_skill_mounts_its_manual_for_the_next_round(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """The detailed rules moved out of the standing prompt live in the skills' manuals: a turn that uses
    a skill's ordinary (not deferred) tool carries that skill's manual from its next round on."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "两个并行工具")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    first, second = responses_server.requests[-2:]
    assert '<skill_content name="review-summary">' not in first["instructions"]
    assert '<skill_content name="review-summary">' in second["instructions"]
    assert "get_trade_journal" in _tool_names(second)  # its deferred tools came along


async def test_the_embedded_catalog_is_a_compact_index_with_details_on_request(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """app_capabilities without an operation used to return ~34k tokens (every operation, every tool's
    description again, the external-agent guide and the whole UiCommand schema). The embedded AI gets the
    operations grouped by skill and one line per interface field; details come per operation or field."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "只读操作手册后断言行情")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    output = next(item["output"] for item in responses_server.requests[-1]["input"]
                  if item.get("type") == "function_call_output")
    catalog = json.loads(output)["data"]
    assert "guide" not in catalog and "price-alerts" in catalog["operations"]
    # Tool-only skills and deferred tools stay discoverable by name.
    assert "tool:get_market_board" in catalog["operations"]["market-lookup"]
    assert "tool:annotate_chart" in catalog["operations"]["chart-annotations"]
    assert "panel=ai|" in catalog["uiControl"]["index"] and "$defs" not in output
    caps = native_application.state.adapter.trader_tools.app_capabilities
    full = caps.manifest(None, user="local-user")  # the external agents' catalog is unchanged
    assert "guide" in full and "commandSchema" in full["uiControl"]
    assert len(output) * 4 < len(json.dumps(full, ensure_ascii=False))
    panel = caps.manifest("ui:panel", user="local-user")
    assert panel["field"] == "panel" and any("panel 打开右侧栏面板" in note for note in panel["notes"])
    disclosure = caps.manifest("ui:disclosure", user="local-user")
    assert disclosure["schema"]["anyOf"][0]["$ref"] == "#/$defs/DisclosureChange" and "DisclosureChange" in disclosure["$defs"]
    by_skill = caps.manifest(None, user="local-user", compact=True, query="chart-annotations")
    assert [item["operation"] for item in by_skill["tools"]] == ["tool:annotate_chart"]
    assert caps.manifest("tool:annotate_chart", user="local-user")["bodySchema"]
    alerts = caps.manifest(None, user="local-user", compact=True, query="alerts")
    assert alerts["operations"] and all("alert" in item["operation"] for item in alerts["operations"])


async def test_a_ui_round_carries_the_compact_command_index_not_the_whole_schema(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "把界面切到图表那一页", "uiWindowId": window_id})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    ui_round = responses_server.requests[0]
    assert _tool_names(ui_round) == UI_FALLBACK_TOOLS
    assert "本轮界面命令索引" in ui_round["instructions"]
    assert "positionsTab=positions|orders|fills" in ui_round["instructions"]
    assert '"$defs"' not in ui_round["instructions"] and "commandSchema" not in ui_round["instructions"]


def test_wire_schema_drops_generated_titles_but_keeps_a_property_named_title():
    schema = {"title": "Args", "type": "object", "properties": {
        "title": {"title": "Title", "type": "string", "description": "卡片标题"},
        "symbol": {"title": "Symbol", "type": "string"}},
        "$defs": {"Inner": {"title": "Inner", "type": "object", "properties": {"x": {"title": "X", "type": "integer"}}}}}
    assert wire_schema(schema) == {"type": "object", "properties": {
        "title": {"type": "string", "description": "卡片标题"}, "symbol": {"type": "string"}},
        "$defs": {"Inner": {"type": "object", "properties": {"x": {"type": "integer"}}}}}


async def test_a_delegated_voice_turn_sends_only_the_last_few_turns_and_the_positions(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """语音委派短上下文: a voice turn carries the last VOICE_HISTORY_TURNS earlier turns (the front desk's
    own answers appended by append_voice_turn among them) and the account's positions line; a typed
    turn in the same session still carries the whole conversation."""
    session_id = await create_session(native_client)
    for index in range(VOICE_HISTORY_TURNS + 2):
        run_id = await start_run(native_client, session_id, f"第{index}个只读问题")
        assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    native_application.state.adapter.append_voice_turn(session_id, "前台问过的一句", "前台答过的一句")
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "语音里再问一句", "origin": "voice"})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    users = [item["content"] for item in responses_server.requests[-1]["input"] if item.get("role") == "user"]
    assert len(users) == VOICE_HISTORY_TURNS + 1
    assert users[-2] == "前台问过的一句" and not any("第0个" in text for text in users)
    assert "账户现状（系统本轮现场读取" in users[-1] and "持仓：" in users[-1]
    typed = await start_run(native_client, session_id, "打字再问一句")
    assert (await wait_for_run(native_application, typed))["status"] == "completed"
    typed_users = [item for item in responses_server.requests[-1]["input"] if item.get("role") == "user"]
    assert len(typed_users) > VOICE_HISTORY_TURNS + 2


async def test_a_manual_read_without_a_topic_returns_its_table_of_contents(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """GET /api/app/help without a topic is the whole manual (~300k characters); the embedded AI gets the
    table of contents and reads one topic in full (the route itself is unchanged for the help card)."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "读使用说明目录")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    outputs = [item["output"] for item in responses_server.requests[-1]["input"]
               if item.get("type") == "function_call_output"]
    outline, topic = (json.loads(output)["data"]["data"] for output in outputs)
    assert "next" in outline and "agent" in outline["topics"] and "paragraphs" not in outline["topics"]["agent"]
    assert len(outputs[0]) < 20_000
    assert topic["topics"]["agent"]["paragraphs"]
    full = await native_client.get("/api/app/help")
    assert len(full.text) > 100_000 and "paragraphs" in full.json()["topics"]["agent"]
    # The embedded lookup of the route says so; the external catalog's definition is unchanged.
    caps = native_application.state.adapter.trader_tools.app_capabilities
    assert "目录" in caps.manifest("GET /api/app/help", user="local-user", compact=True)["embeddedResponse"]
    assert "embeddedResponse" not in caps.manifest("GET /api/app/help", user="local-user")


async def test_a_manual_search_returns_the_sentences_around_the_word(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“手续费在哪改” after the outline: query.search finds 设置 → 高级交易设置 → 费率 (10-14 实测 the model
    answered “没有这个设置” from the outline alone; the prompt now requires the check)."""
    session_id = await create_session(native_client)
    run_id = await start_run(native_client, session_id, "搜使用说明")
    assert (await wait_for_run(native_application, run_id))["status"] == "completed"
    output = next(item["output"] for item in responses_server.requests[-1]["input"]
                  if item.get("type") == "function_call_output")
    found = json.loads(output)["data"]["data"]
    assert found["search"] == "手续费" and len({match["topic"] for match in found["matches"]}) == len(found["matches"]) and any(
        match["topic"] == "settings" and "高级交易设置" in match["text"] for match in found["matches"])
    assert "query.search" in responses_server.requests[-1]["instructions"]


async def test_a_finished_turns_tool_outputs_ride_the_next_turn_cut_short(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    from trader_runtime.native_agent import CARRIED_TOOL_OUTPUT_CHARS, CARRIED_TOOL_OUTPUT_CUT
    session_id = await create_session(native_client)
    first = await start_run(native_client, session_id, "读策略计划定义")
    assert (await wait_for_run(native_application, first))["status"] == "completed"
    within = next(item["output"] for item in responses_server.requests[-1]["input"]
                  if item.get("type") == "function_call_output")
    assert len(within) > CARRIED_TOOL_OUTPUT_CHARS  # the turn itself reads it whole
    second = await start_run(native_client, session_id, "随便聊聊")
    assert (await wait_for_run(native_application, second))["status"] == "completed"
    carried = next(item["output"] for item in responses_server.requests[-1]["input"]
                   if item.get("type") == "function_call_output")
    assert carried == within[:CARRIED_TOOL_OUTPUT_CHARS] + CARRIED_TOOL_OUTPUT_CUT


async def test_a_sentence_naming_a_mounted_skill_skips_the_ui_only_round(
    native_client: AsyncClient, native_application: FastAPI, responses_server: FakeResponsesServer,
):
    """“回放不看了，退出” looks like interface wording, but it names the replay skill (control_replay):
    the general turn with that skill's tools answers it, not the UI-only round guessing a command."""
    window_id = await _register_window(native_client)
    session_id = await create_session(native_client)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "回放不看了，退出", "uiWindowId": window_id})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    first = responses_server.requests[0]
    assert _tool_names(first) != UI_FALLBACK_TOOLS and "control_replay" in _tool_names(first)
    assert "本轮界面命令索引" not in first["instructions"]
    # Interface wording that names no skill still takes the UI round.
    session_id = await create_session(native_client)
    before = len(responses_server.requests)
    response = await native_client.post(f"/api/sessions/{session_id}/messages",
                                        json={"text": "把界面切到图表那一页", "uiWindowId": window_id})
    assert (await wait_for_run(native_application, response.json()["runId"]))["status"] == "completed"
    assert _tool_names(responses_server.requests[before]) == UI_FALLBACK_TOOLS

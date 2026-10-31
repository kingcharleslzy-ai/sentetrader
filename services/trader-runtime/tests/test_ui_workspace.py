from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from test_strategy_workbench import plan
from trader_runtime.main import create_app
from trader_runtime.store import RuntimeStore, utc_now
from trader_runtime.strategy_scope import allows_symbol, execution_strategy
from trader_runtime.ui_workspace import UiWorkspace


@pytest.mark.asyncio
async def test_design_survives_view_changes_and_is_idempotent_and_user_scoped(tmp_path, monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()
    calls = []
    async def design(adapter, description, constraints, *, on_progress, **kwargs):
        calls.append(description); on_progress(stage="模型正在分析", summary="正在核对用户的周期选择")
        started.set(); await finish.wait(); return plan().model_dump()
    monkeypatch.setattr("trader_runtime.ui_workspace.design_plan", design)
    workspace = UiWorkspace(tmp_path)
    body = {"id": uuid4().hex, "description": "原始想法", "constraints": {}}
    job = workspace.start("one", body, object())
    await started.wait()
    assert workspace.start("one", body, object())["id"] == job["id"]
    assert workspace.get("one", job["id"])["summary"]
    with pytest.raises(HTTPException) as denied:
        workspace.get("two", job["id"])
    assert denied.value.status_code == 404
    finish.set(); await asyncio.gather(*list(workspace.tasks.values()))
    restored = UiWorkspace(tmp_path)
    assert restored.scope("one") == workspace.scope("one")
    assert restored.scope("two") != workspace.scope("one")
    assert restored.get("one", job["id"])["result"]["description"] == plan().description
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancel_before_execution_and_restart_do_not_resubmit(tmp_path, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("cancelled request must not start the model")
    monkeypatch.setattr("trader_runtime.ui_workspace.design_plan", unexpected)
    workspace = UiWorkspace(tmp_path)
    body = {"id": uuid4().hex, "description": "待分析", "constraints": {}}
    workspace.start("one", body, object())
    assert (await workspace.cancel("one", body["id"]))["status"] == "cancelled"
    workspace.persist("one", dict(id=uuid4().hex, status="running", description="interrupted", startedAt=utc_now()))
    path = next(p for p in workspace.root.glob("*.json") if 'interrupted' in p.read_text())
    import json
    identifier = json.loads(path.read_text())["id"]
    recovered = UiWorkspace(tmp_path).get("one", identifier)
    assert recovered["status"] == "cancelled" and "重启" in recovered["stage"]
    assert not workspace.tasks


@pytest.mark.asyncio
async def test_chat_restore_ignores_url_port_and_keeps_user_history(tmp_path):
    app = create_app(tmp_path/"db.sqlite", runtime_state_root=tmp_path, agent_mode="fake", local_token="test-replay-token")
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://127.0.0.1:49100", headers=headers) as c:
            first = (await c.post("/api/sessions/restore", json={"mode":"local","agentId":"native"})).json()
            scope = (await c.get("/api/workspace")).json()["scope"]
        async with AsyncClient(transport=ASGITransport(app), base_url="http://127.0.0.1:49200", headers=headers) as c:
            second = (await c.post("/api/sessions/restore", json={"mode":"local","agentId":"native"})).json()
            assert second["sessionId"] == first["sessionId"]
            assert second["expiresAt"].startswith("9999")
            assert (await c.get("/api/workspace")).json()["scope"] == scope
            assert app.state.store.recent_chat_session(user_id="someone-else", mode="local") is None


def test_final_messages_survive_transient_event_retention(tmp_path):
    store = RuntimeStore(tmp_path/"db.sqlite", max_events_per_session=2)
    session = store.create_session(user_id="one", mode="local", agent_id="codex")["sessionId"]
    for index in range(5):
        store.append_event(session, {"id":str(uuid4()),"schemaVersion":1,"sessionId":session,"kind":"message","name":"message.completed","timestamp":utc_now(),"payload":{"turnId":str(index),"role":"user","text":f"message {index}"}})
    for index in range(5):
        store.append_event(session, {"id":str(uuid4()),"schemaVersion":1,"sessionId":session,"kind":"message","name":"message.delta","timestamp":utc_now(),"payload":{"turnId":"stream","role":"assistant","text":str(index)}})
    events = [event for _, event in store.list_events(session)]
    assert len([e for e in events if e["name"] == "message.completed"]) == 5
    assert len([e for e in events if e["name"] == "message.delta"]) == 2
    # #932 P2: an order card is kept, so the result that settles it is kept too — 77 old cards read 等待确认
    # again after their proposal.result was trimmed.
    store.append_event(session, {"id":str(uuid4()),"schemaVersion":1,"sessionId":session,"kind":"result","name":"proposal.result","timestamp":utc_now(),
                                 "payload":{"proposalId":"p1","status":"rejected","auditId":"a1","message":"超时","reason":"expired"}})
    for index in range(5):
        store.append_event(session, {"id":str(uuid4()),"schemaVersion":1,"sessionId":session,"kind":"message","name":"message.delta","timestamp":utc_now(),"payload":{"turnId":"stream","role":"assistant","text":str(index)}})
    assert [e["payload"]["proposalId"] for _, e in store.list_events(session) if e["name"] == "proposal.result"] == ["p1"]
    store.close()


@pytest.mark.asyncio
async def test_unbound_library_scope_and_concrete_execution_are_separate(tmp_path):
    app = create_app(tmp_path/"db.sqlite", runtime_state_root=tmp_path, agent_mode="fake", local_token="test-replay-token")
    headers = {"Origin":"http://127.0.0.1:5173","Authorization":"Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as c:
            saved = (await c.post('/api/strategy-plans',json={"plan":plan().model_dump()})).json()
            assert saved['symbol'] == '*' and saved['definition']['instrumentScope']['mode'] == 'any'
            assert allows_symbol(saved, 'SHFE.rb2610')
            kwargs = dict(store=app.state.store, broker=app.state.broker, user='local-user')
            execution = execution_strategy(saved, 'SHFE.au2612', **kwargs)
            assert execution['strategyId'] == execution_strategy(saved, 'SHFE.au2612', **kwargs)['strategyId']
            assert execution['symbol'] == 'SHFE.au2612'
            changed = (await c.post(f"/api/strategy-plans/{saved['strategyId']}/scope",json={"mode":"selected","symbols":["SHFE.rb","DCE.i"]})).json()
            assert allows_symbol(changed,'SHFE.rb2610') and allows_symbol(changed,'DCE.i2701')
            assert not allows_symbol(changed,'SHFE.au2612')
            with pytest.raises(ValueError): execution_strategy(changed,'SHFE.au2612',**kwargs)
            unchanged = app.state.store.get_strategy_draft(execution['strategyId'],user_id='local-user')
            assert unchanged['symbol'] == 'SHFE.au2612' and unchanged['definitionDigest'] == execution['definitionDigest']
            library = (await c.get('/api/strategy-plans')).json()
            assert len(library['items']) == 1 and len(library['executions']) == 1

@pytest.mark.asyncio
async def test_summary_events_are_forwarded_but_raw_reasoning_is_not(tmp_path):
    from pathlib import Path

    from fixtures.fake_responses_server import FakeResponsesServer
    from trader_runtime.broker import EventBroker
    from trader_runtime.native_agent import ModelEndpoint, NativeAgentAdapter
    from trader_runtime.store import RuntimeStore
    from trader_runtime.strategy_explanations import ModelSummary
    server = FakeResponsesServer(reasoning_summary=True)
    store = RuntimeStore(tmp_path / "s.sqlite3")
    adapter = NativeAgentAdapter(
        store=store, broker=EventBroker(store), workspace_root=Path(tmp_path) / "ws", provider="deepseek",
        endpoints={"deepseek-flash": ModelEndpoint("deepseek-flash", "deepseek", "https://fake.test/", "sk-x")},
        http_transport=server.transport())
    updates = []
    try:
        await adapter.complete_structured(instructions="带推理摘要 只返回摘要", input_text="{}", schema=ModelSummary,
                                          timeout_seconds=5, on_progress=lambda **kw: updates.append(kw))
    finally:
        await adapter.shutdown()
        store.close()
    assert updates[-1]["summary"] == "先核对账户，再看盘面。"
    assert "raw private" not in str(updates)


def test_structured_questions_are_not_executable_without_answers():
    p = plan()
    from trader_runtime.strategy_rules import Clarification
    p.clarifications = [Clarification(id="period",question="哪个周期？",recommended=0,options=[{"label":"5分钟","description":"盘中"},{"label":"日线","description":"趋势"}])]
    assert not p.questions
    with pytest.raises(ValueError,match="待澄清"):
        p.check_executable()


def test_restore_prefers_recent_user_conversation_over_background_status(tmp_path):
    store = RuntimeStore(tmp_path/"db.sqlite")
    older = store.create_session(user_id="one", mode="local", agent_id="codex")["sessionId"]
    newer = store.create_session(user_id="one", mode="local", agent_id="codex")["sessionId"]
    first = store.create_run(session_id=older, user_id="one", instruction="old")
    second = store.create_run(session_id=newer, user_id="one", instruction="new")
    with store.connection:
        store.connection.execute("UPDATE agent_runs SET created_at = '2026-09-04T01:00:00+00:00' WHERE id = ?", (first["runId"],))
        store.connection.execute("UPDATE agent_runs SET created_at = '2026-09-05T01:00:00+00:00' WHERE id = ?", (second["runId"],))
        store.connection.execute("UPDATE sessions SET updated_at = '2026-09-06T01:00:00+00:00' WHERE id = ?", (older,))
    assert store.recent_chat_session(user_id="one", mode="local")["sessionId"] == newer
    # #523: an expired session is no conversation to restore or for a voice call to fall back on.
    with store.connection:
        store.connection.execute("UPDATE sessions SET expires_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (newer,))
    assert store.recent_chat_session(user_id="one", mode="local")["sessionId"] == older
    assert [row["sessionId"] for row in store.chat_sessions_by_recency(user_id="one")] == [older]
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [(TimeoutError(), "model_timeout"), (RuntimeError("deepseek_api_key_required"), "model_auth_required")])
async def test_design_failure_survives_restart_with_cause_and_stage(tmp_path, monkeypatch, failure, code):
    async def broken(*args, on_progress, **kwargs):
        on_progress(stage="等待模型生成策略")
        raise failure
    monkeypatch.setattr("trader_runtime.ui_workspace.design_plan", broken)
    workspace = UiWorkspace(tmp_path)
    job = workspace.start("one", {"description": "原文必须保留", "constraints": {}}, object())
    await asyncio.gather(*workspace.tasks.values())
    result = UiWorkspace(tmp_path).get("one", job["id"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == code
    assert result["error"]["phase"] == "等待模型生成策略"
    assert result["description"] == "原文必须保留"


@pytest.mark.asyncio
async def test_more_than_two_clarifications_can_finish_without_losing_answers(tmp_path, monkeypatch):
    captured = []
    async def design(adapter, description, constraints, **kwargs):
        captured.append(constraints)
        return plan().model_dump()
    monkeypatch.setattr("trader_runtime.ui_workspace.design_plan", design)
    constraints = {"clarificationRound": 3, "clarificationAnswers": [{"question": "使用什么周期？", "answer": "日线"}]}
    workspace = UiWorkspace(tmp_path)
    result = workspace.start("one", {"description": "原始交易条件", "constraints": constraints}, object())
    await asyncio.gather(*workspace.tasks.values())
    assert workspace.get("one", result["id"])["status"] == "completed"
    assert captured == [constraints]


@pytest.mark.asyncio
async def test_model_auth_error_finishes_design_and_does_not_expose_secrets():
    import httpx

    from trader_runtime.native_agent import NativeAgentError
    from trader_runtime.strategy_design_errors import design_failure
    # A 401 whose body quotes the header never reaches the user-facing failure.
    upstream = httpx.Response(401, json={"error": {"message": "Unauthorized Authorization: Bearer private-value"}},
                              request=httpx.Request("POST", "https://fake.test/responses"))
    error = NativeAgentError("model_auth_failed")
    error.__cause__ = RuntimeError(upstream.text)
    failure = design_failure(error, "等待模型生成策略")
    assert failure["code"] == "model_auth_required"
    assert "private-value" not in str(failure)


@pytest.mark.asyncio
async def test_new_chat_keeps_previous_history_and_is_user_scoped(tmp_path):
    app = create_app(tmp_path/"db.sqlite", runtime_state_root=tmp_path, agent_mode="fake", local_token="test-replay-token")
    headers = {"Origin":"http://127.0.0.1:5173", "Authorization":"Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://127.0.0.1:49100", headers=headers) as client:
            body = {"mode":"local", "agentId":"native"}
            old = (await client.post("/api/sessions/restore", json=body)).json()["sessionId"]
            new = (await client.post("/api/sessions?chat=true", json=body)).json()["sessionId"]
            assert old != new
            history = (await client.get("/api/chat-sessions")).json()["items"]
            assert {item["sessionId"] for item in history} == {old, new}
            assert (await client.get(f"/api/sessions/{old}")).status_code == 200
            assert (await client.get(f"/api/sessions/{new}")).json()["expiresAt"].startswith("9999")
            assert app.state.store.list_chat_sessions(user_id="another-user", mode="local") == []
            assert (await client.get("/api/chat-sessions?mode=cloud")).json()["items"] == []


def test_ui_changes_accept_the_positions_bar_and_still_the_legacy_panel_value() -> None:
    from trader_runtime.ui_control import UiChanges

    assert UiChanges(positionsBar="collapsed").positionsBar == "collapsed"
    assert UiChanges(panel="positions").panel == "positions"
    import pytest as _pytest
    with _pytest.raises(ValueError):
        UiChanges(positionsBar="hidden")  # type: ignore[arg-type]


def test_legacy_positions_panel_command_matches_an_expanded_bar() -> None:
    from trader_runtime.ui_control import changes_match

    assert changes_match({"panel": None, "positionsBar": "expanded"}, {"panel": "positions"})
    assert changes_match({"panel": None}, {"panel": "positions"})  # a client without the bar field
    assert not changes_match({"panel": None, "positionsBar": "collapsed"}, {"panel": "positions"})
    assert changes_match({"positionsBar": "collapsed"}, {"positionsBar": "collapsed"})


def test_removed_depth_panel_command_is_still_accepted_and_opens_the_order_panel() -> None:
    """2026-10-07: the 价格阶梯 merged into the 下单面板's 五档; `panel: depth` from older agents,
    skills or saved tours is normalized to order, so it is forwarded and judged as the 下单面板."""
    from trader_runtime.ui_control import TourFocus, UiChanges, changes_match

    changes = UiChanges.model_validate({"panel": "depth"}).model_dump(exclude_unset=True)
    assert changes == {"panel": "order"}
    assert changes_match({"panel": "order"}, changes) and not changes_match({"panel": "ai"}, changes)
    assert TourFocus.model_validate({"panel": "depth"}).panel == "order"


def test_news_flash_tab_filter_and_calendar_fold_are_commands() -> None:
    from pydantic import ValidationError

    from trader_runtime.ui_control import DisclosureChange, UiChanges, changes_match

    # 快讯页签 (2026-10-03): tab flash + flashFilter, receipt read from state.news; 展开全部 is a disclosure.
    change = UiChanges.model_validate({"news": {"tab": "flash", "flashFilter": "macro"}})
    assert change.news is not None and change.news.flashFilter == "macro"
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"news": {"flashFilter": "today"}})
    assert DisclosureChange(name="flash-calendar", open=True).name == "flash-calendar"
    # 新闻 AI 筛选 (2026-10-07): the sort switch and the two dropped folds.
    assert UiChanges.model_validate({"news": {"flashSort": "impact"}}).news.flashSort == "impact"  # type: ignore[union-attr]
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"news": {"flashSort": "hot"}})
    assert {DisclosureChange(name=name, open=True).name for name in ("flash-dropped", "news-dropped")} == {"flash-dropped", "news-dropped"}
    # 第六批 (2026-10-07): the 次要消息 folds take no id; a merged event's reports are named by the event id.
    assert DisclosureChange(name="flash-minor", open=True).id is None and DisclosureChange(name="news-minor", open=False).name == "news-minor"
    assert DisclosureChange(name="flash-event", id="e1a2b3c4d5e6", open=True).id == "e1a2b3c4d5e6"
    # 翻译原文折叠 (2026-10-11): named by the item / report / phone event id.
    assert DisclosureChange(name="flash-orig", id="f1e2d3c4b5a6", open=True).id == "f1e2d3c4b5a6"
    assert DisclosureChange(name="news-orig", open=True).id is None
    with pytest.raises(ValidationError):
        DisclosureChange(name="flash-minor", id="e1", open=True)
    assert changes_match({"panel": "news", "news": {"tab": "flash", "flashSort": "impact"}}, {"news": {"flashSort": "impact"}})
    state = {"panel": "news", "news": {"tab": "flash", "flashFilter": "watch"}}
    assert changes_match(state, {"news": {"flashFilter": "watch"}})
    assert not changes_match(state, {"news": {"flashFilter": "macro"}})
    assert not changes_match({"panel": None, "news": {"tab": "flash"}}, {"news": {"tab": "flash"}})


def test_research_desk_rejudge_source_jump_and_folds_are_commands() -> None:
    from pydantic import ValidationError

    from trader_runtime.ui_control import DisclosureChange, UiChanges, changes_match

    # 调研页改版 (2026-10-07 第七批): 按最新消息重新判断 and the 资料来源 jump are news commands; the four folds are disclosures.
    rejudge = UiChanges.model_validate({"news": {"deskRejudge": True}}).model_dump(exclude_unset=True)
    source = UiChanges.model_validate({"news": {"deskSource": "M.price"}}).model_dump(exclude_unset=True)
    for wrong in ({"deskRejudge": False}, {"deskSource": "price"}, {"deskSource": "M.price; drop"}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate({"news": wrong})
    assert {DisclosureChange(name=name, open=True).name for name in ("desk-fundamental", "desk-macro", "desk-reports", "desk-sources")} == {
        "desk-fundamental", "desk-macro", "desk-reports", "desk-sources"}
    with pytest.raises(ValidationError):
        DisclosureChange(name="desk-sources", id="M.price", open=True)
    desk = {"panel": "news", "news": {"tab": "desk", "desk": {"rejudge": None, "highlight": "M.price"}}}
    # Applied once the page has sent the refresh (the receipt is read when the page acknowledges the command;
    # the Runtime's answer — started / limited with its reason — follows in rejudge.message).
    assert not changes_match(desk, rejudge)
    desk["news"]["desk"]["rejudge"] = {"status": "sending", "message": "正在按最新消息重新判断…"}
    assert changes_match(desk, rejudge) and changes_match(desk, source)
    assert not changes_match(desk, {"news": {"deskSource": "D.spot_basis"}})
    assert not changes_match({**desk, "news": {**desk["news"], "tab": "flash"}}, source)


def test_settings_page_legacy_values_map_onto_the_split_pages() -> None:
    from trader_runtime.ui_control import UiChanges, changes_match, settings_page

    # 设置拆页 (2026-09-30): fees now lands on the second-level page, not 交易.
    assert settings_page("fees") == "trade-advanced" and settings_page("simulation") == "trade"
    assert settings_page("memory-advanced") == "memory-advanced" and settings_page("ai") == "ai"
    assert changes_match({"settingsPage": "trade-advanced"}, {"settingsPage": "fees"})
    assert changes_match({"settingsPage": "trade"}, {"settingsPage": "simulation"})
    assert not changes_match({"settingsPage": "trade"}, {"settingsPage": "fees"})
    feedback = UiChanges.model_validate({"settingsPage": "feedback"}).model_dump(exclude_unset=True)
    assert changes_match({"settingsOpen": True, "settingsPage": "feedback"}, feedback)
    assert not changes_match({"settingsOpen": True, "settingsPage": "about"}, feedback)


def test_admin_commands_are_rejected_but_older_heartbeats_still_report_the_console() -> None:
    from pydantic import ValidationError

    from trader_runtime.ui_control import ChartOptions, DisclosureChange, UiChanges, UiState

    # 后台管理 is its own page (qilinshuzhi.com/trader/admin, 2026-09-30): the trading
    # workspace window no longer hosts the console, so the commands that opened it are gone.
    for gone in ({"view": "admin"}, {"adminSection": "users"}, {"settingsPage": "admin"}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate(gone)
    # 1.6.12 and earlier desktop clients still heartbeat view/settingsPage "admin" and the
    # open adminSection; a report is never a request, so the state keeps accepting them.
    base = dict(symbol=None, duration=60, view="trade", panel=None, settingsOpen=False, theme="dark",
                dockSide="right", dockWidth=400, replay=False, settingsPage="account", fontScale="standard",
                density="standard", chart={k: True for k in ChartOptions.model_fields},
                chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    legacy = UiState.model_validate(base | {"view": "admin", "settingsPage": "admin", "adminSection": "skills"})
    assert (legacy.view, legacy.settingsPage, legacy.adminSection) == ("admin", "admin", "skills")
    # 复盘 (2026-10-03): the review reports its 账户 tiles and the 权益曲线 summary beside the selection.
    review = UiState.model_validate(base | {"view": "review", "review": {
        "month": "2026-09", "day": "2026-09-30", "filter": "", "selectedTradeId": None, "selectedNote": None,
        "tiles": [{"label": "本月平仓", "value": "—", "note": "本月还没有平仓"}],
        "equityCurve": {"from": "2026-09-14", "to": "2026-09-30", "days": 13, "high": 1033977, "low": 1002400},
        "unlinkedNotes": 2}}).review
    assert review is not None and review.tiles[0]["note"] == "本月还没有平仓" and review.equityCurve["days"] == 13
    assert review.unlinkedNotes == 2 and DisclosureChange(name="unlinked-notes", open=True).name == "unlinked-notes"


def test_phone_layout_tab_is_a_command_reported_only_by_phone_windows() -> None:
    from pydantic import ValidationError

    from trader_runtime.ui_control import ChartOptions, UiChanges, UiState, changes_match

    # 手机版 (2026-10-04): the bottom tab is one command; a desktop window reports neither field.
    assert UiChanges.model_validate({"mobileTab": "notify"}).mobileTab == "notify"
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"mobileTab": "orders"})
    base = dict(symbol=None, duration=60, view="trade", panel=None, settingsOpen=False, theme="dark",
                dockSide="right", dockWidth=340, replay=False, settingsPage="account", fontScale="standard",
                density="standard", chart={k: True for k in ChartOptions.model_fields},
                chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    phone = UiState.model_validate(base | {"panel": "ai", "mobileLayout": True, "mobileTab": "chat"})
    assert (phone.mobileLayout, phone.mobileTab) == (True, "chat")
    assert UiState.model_validate(base).mobileTab is None
    state = phone.model_dump(exclude_none=True)
    assert changes_match(state, {"mobileTab": "chat"}) and changes_match(state, {"panel": "ai"})
    assert not changes_match(state, {"mobileTab": "research"})
    assert not changes_match(UiState.model_validate(base).model_dump(exclude_none=True), {"mobileTab": "ai"})


def test_mobile_market_is_one_key_per_command_and_judged_by_the_account_document() -> None:
    """手机看盘 (2026-10-09): `mobileMarket` changes one thing at a time; the receipt needs the 看盘 tab and the
    account document's ui.mobileMarket (a visitor reports none and keeps its own receipt); the 副图设置 sheet is a
    disclosure. A sub pane counts only once it is in chart.subPaneList."""
    from pydantic import ValidationError

    from trader_runtime.ui_control import ChartOptions, UiChanges, UiState, changes_match
    from trader_runtime.user_preferences import UserPreferences

    for good in ({"view": "quotes"}, {"board": "CZCE"}, {"columns": ["last", "bid", "ask"]}, {"layout": "six"},
                 {"cell": {"index": 5, "symbol": None}}, {"cell": {"index": 0, "duration": 900}}, {"subPane": "cci"},
                 {"subPanes": True}):
        assert UiChanges.model_validate({"mobileMarket": good}).mobileMarket is not None, good
    for bad in ({"view": "quotes", "board": "main"}, {}, {"board": "LME"}, {"columns": ["last", "last"]},
                {"columns": ["last", "bid", "ask", "volume", "change"]}, {"cell": {"index": 6, "duration": 300}},
                {"cell": {"index": 0}}, {"subPane": "boll"}, {"view": None}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate({"mobileMarket": bad})
    base = dict(symbol="SHFE.rb2701", duration=300, view="trade", panel=None, settingsOpen=False, theme="dark",
                dockSide="right", dockWidth=340, replay=False, settingsPage="account", fontScale="standard",
                density="standard", chart={k: True for k in ChartOptions.model_fields},
                chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    document = UserPreferences().model_dump()
    document["chart"]["subPaneList"] = ["kd", "cci"]
    document["ui"]["mobileMarket"].update(view="multi", layout="four",
                                          cells=[{"symbol": "SHFE.rb2701", "duration": 900}] + [{"symbol": None, "duration": 300}] * 5)
    state = UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "market", "preferences": document,
                                           "disclosures": {"mobile-subpanes": False}}).model_dump()
    assert changes_match(state, {"mobileMarket": {"view": "multi"}})
    assert changes_match(state, {"mobileMarket": {"layout": "four"}}) and not changes_match(state, {"mobileMarket": {"layout": "six"}})
    assert changes_match(state, {"mobileMarket": {"cell": {"index": 0, "symbol": "shfe.RB2701", "duration": 900}}})
    assert changes_match(state, {"mobileMarket": {"cell": {"index": 1, "symbol": None}}})
    assert not changes_match(state, {"mobileMarket": {"cell": {"index": 1, "duration": 3600}}})
    # A 报价 change is not applied while the document still shows 多图; the sheet is judged by its disclosure.
    assert not changes_match(state, {"mobileMarket": {"board": "watchlist"}})
    assert changes_match(state, {"mobileMarket": {"subPanes": False}}) and not changes_match(state, {"mobileMarket": {"subPanes": True}})
    single = {**document, "ui": {**document["ui"], "mobileMarket": {**document["ui"]["mobileMarket"], "view": "single", "subPane": "cci"}}}
    shown = UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "market", "preferences": single}).model_dump()
    assert changes_match(shown, {"mobileMarket": {"subPane": "cci"}})
    stray = {**single, "chart": {**single["chart"], "subPaneList": ["kd"]}}
    assert not changes_match(UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "market", "preferences": stray}).model_dump(),
                             {"mobileMarket": {"subPane": "cci"}})
    # Another tab is never applied; a window without the document (a visitor) keeps its own receipt.
    assert not changes_match({**state, "mobileTab": "ai"}, {"mobileMarket": {"view": "multi"}})
    visitor = UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "market"}).model_dump()
    assert changes_match(visitor, {"mobileMarket": {"view": "quotes"}})


def test_phone_pages_and_notify_filter_are_commands_the_phone_window_reports() -> None:
    from pydantic import ValidationError

    from trader_runtime.ui_control import ChartOptions, UiChanges, UiState, changes_match

    # 手机版第二期: a full-screen page (null closes it) and the 通知 filter, each one command.
    assert UiChanges.model_validate({"mobilePage": "order"}).mobilePage == "order"
    assert UiChanges.model_validate({"mobilePage": None}).model_fields_set == {"mobilePage"}
    assert UiChanges.model_validate({"mobileNotifyFilter": "fill"}).mobileNotifyFilter == "fill"
    for bad in ({"mobilePage": "strategy"}, {"mobileNotifyFilter": "risk"}, {"mobileNotifyFilter": None}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate(bad)
    base = dict(symbol=None, duration=60, view="trade", panel=None, settingsOpen=False, theme="dark",
                dockSide="right", dockWidth=340, replay=False, settingsPage="account", fontScale="standard",
                density="standard", chart={k: True for k in ChartOptions.model_fields},
                chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    phone = UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "market", "mobilePage": "order",
                                           "mobileNotifyFilter": "all", "mobileUnread": 3})
    state = phone.model_dump()
    assert changes_match(state, {"mobilePage": "order"}) and not changes_match(state, {"mobilePage": None})
    closed = UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "market", "mobilePage": None}).model_dump()
    assert changes_match(closed, {"mobilePage": None}) and changes_match(state, {"mobileNotifyFilter": "all"})
    with pytest.raises(ValidationError):
        UiState.model_validate(base | {"mobileUnread": -1})
    # AI 简报订阅 (2026-10-05): the 我的 → AI 简报推送 page and 通知 narrowed to one product (null = all).
    assert UiChanges.model_validate({"mobilePage": "briefs"}).mobilePage == "briefs"
    assert UiChanges.model_validate({"mobileNotifyProduct": "SHFE.rb"}).mobileNotifyProduct == "SHFE.rb"
    assert UiChanges.model_validate({"mobileNotifyProduct": None}).model_fields_set == {"mobileNotifyProduct"}
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"mobileNotifyProduct": "SHFE.rb2601"})
    narrowed = UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "notify", "mobileNotifyFilter": "brief",
                                              "mobileNotifyProduct": "SHFE.rb"}).model_dump()
    assert changes_match(narrowed, {"mobileNotifyProduct": "SHFE.rb"}) and not changes_match(narrowed, {"mobileNotifyProduct": None})
    assert changes_match(phone.model_dump(), {"mobileNotifyProduct": None})
    # 安装到桌面 (2026-10-06): the install guide page and the environment it reports while open.
    assert UiChanges.model_validate({"mobilePage": "install"}).mobilePage == "install"
    guide = UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "me", "mobilePage": "install",
                                           "installEnvironment": "in-app-ios"}).model_dump()
    assert changes_match(guide, {"mobilePage": "install"})
    with pytest.raises(ValidationError):
        UiState.model_validate(base | {"installEnvironment": "netscape"})
    # 锁屏提醒 (2026-10-06): the push page and what it reports while open.
    push = UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "me", "mobilePage": "push",
                                          "pushSupport": "needs-install", "pushSubscribed": False}).model_dump()
    assert changes_match(push, {"mobilePage": "push"})
    with pytest.raises(ValidationError):
        UiState.model_validate(base | {"pushSupport": "maybe"})


def test_reminder_center_is_one_command_on_either_layout_and_the_old_reminder_filter_opens_its_page() -> None:
    """统一提醒 (#932, 2026-10-15): `reminderCenter` opens the bell's popover (computer) or the 提醒 page
    (phone) and picks its filter; the window reports {open, filter, unread, today, earlier}. The timeline's
    retired 提醒 filter is applied once the phone shows the 提醒 page."""
    from pydantic import ValidationError

    from trader_runtime.ui_control import ChartOptions, UiChanges, UiState, changes_match, describe_ui_change

    assert UiChanges.model_validate({"reminderCenter": {"open": True, "filter": "unread"}}).reminderCenter.filter == "unread"
    for bad in ({"reminderCenter": {"open": True, "filter": "today"}}, {"reminderCenter": None},
                {"reminderCenter": {"open": True, "earlier": True}}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate(bad)
    base = dict(symbol=None, duration=60, view="trade", panel=None, settingsOpen=False, theme="dark",
                dockSide="right", dockWidth=340, replay=False, settingsPage="account", fontScale="standard",
                density="standard", chart={k: True for k in ChartOptions.model_fields},
                chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    center = {"open": True, "filter": "unread", "unread": 6, "today": 13, "earlier": False}
    shown = UiState.model_validate(base | {"reminderCenter": center}).model_dump()
    assert changes_match(shown, {"reminderCenter": {"open": True}})
    assert changes_match(shown, {"reminderCenter": {"filter": "unread"}})
    assert not changes_match(shown, {"reminderCenter": {"open": False}})
    assert not changes_match(UiState.model_validate(base).model_dump(), {"reminderCenter": {"open": True}})
    with pytest.raises(ValidationError):
        UiState.model_validate(base | {"reminderCenter": center | {"unread": -1}})
    page = UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "ai", "mobilePage": "reminders",
                                          "mobileNotifyFilter": "all"}).model_dump()
    assert changes_match(page, {"mobilePage": "reminders"}) and changes_match(page, {"mobileNotifyFilter": "reminder"})
    home = UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "ai", "mobileNotifyFilter": "all"}).model_dump()
    assert not changes_match(home, {"mobileNotifyFilter": "reminder"})
    assert describe_ui_change({"reminderCenter": {"open": True, "filter": "unread"}}) == "打开提醒中心，只看未读"
    assert describe_ui_change({"reminderCenter": {"open": False}}) == "关闭提醒中心"


def test_phone_new_structure_strategy_tab_lab_page_and_the_retired_notify_tab() -> None:
    """手机版新结构 (2026-10-06): 策略 is a tab, 回测结果页 a page (`mobileLab` opens one comparison and
    the window reports it with the variant chosen), and the retired 通知 tab still takes its command —
    a newer client opens AI 首页 for it and reports ai, an older one notify; both are applied."""
    from pydantic import ValidationError

    from trader_runtime.ui_control import ChartOptions, UiChanges, UiState, changes_match

    base = dict(symbol=None, duration=60, view="trade", panel=None, settingsOpen=False, theme="dark",
                dockSide="right", dockWidth=340, replay=False, settingsPage="account", fontScale="standard",
                density="standard", chart={k: True for k in ChartOptions.model_fields},
                chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    assert UiChanges.model_validate({"mobileTab": "strategy"}).mobileTab == "strategy"
    newer = UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "ai", "mobileNotifyFilter": "all"}).model_dump()
    older = UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "notify"}).model_dump()
    assert changes_match(newer, {"mobileTab": "notify"}) and changes_match(older, {"mobileTab": "notify"})
    assert changes_match(older, {"mobileTab": "ai"}) and not changes_match(newer, {"mobileTab": "strategy"})
    assert UiChanges.model_validate({"mobilePage": "lab"}).mobilePage == "lab"
    assert UiChanges.model_validate({"mobileLab": {"labId": "6f1c"}}).mobileLab.labId == "6f1c"
    for bad in ({"mobileLab": {"labId": "../x"}}, {"mobileLab": None}, {"mobileLab": {"labId": "a", "variantId": "v1"}}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate(bad)
    lab = UiState.model_validate(base | {"mobileLayout": True, "mobileTab": "strategy", "mobilePage": "lab",
                                         "mobileLab": {"labId": "6f1c", "chosen": "v2"}}).model_dump()
    assert changes_match(lab, {"mobilePage": "lab"}) and changes_match(lab, {"mobileLab": {"labId": "6f1c"}})
    assert not changes_match(lab, {"mobileLab": {"labId": "other"}})


def test_surface_commands_match_the_state_they_change_and_old_clients_are_not_contradicted():
    from pydantic import ValidationError

    from trader_runtime.ui_control import UiChanges, changes_match
    state = {"pinnedSymbols": ["SHFE.rb2701"], "review": {"month": "2026-09", "day": "2026-09-10", "filter": "", "selectedTradeId": "t1"},
             "dialog": "account-detail", "strategyDetails": {"rules": True}, "monitorCards": {"research": "collapsed"},
             "selectedPosition": {"symbol": "SHFE.rb2610", "direction": "short"}, "aiPage": "chat"}
    assert changes_match(state, {"pinnedSymbols": {"add": "shfe.rb2701"}})
    assert not changes_match(state, {"pinnedSymbols": {"remove": "SHFE.rb2701"}})
    assert changes_match(state, {"reviewExpand": {"day": "2026-09-10"}})
    assert not changes_match(state, {"reviewExpand": {"month": "2026-08"}})
    assert changes_match(state, {"reviewFilter": ""})
    # 本站模拟合成一个账户 (2026-10-21): a client before the switch reports no currency and is not contradicted.
    assert changes_match(state, {"reviewCurrency": "USD"})
    assert changes_match({"review": {**state["review"], "currency": "USD"}}, {"reviewCurrency": "USD"})
    assert not changes_match({"review": {**state["review"], "currency": "CNY"}}, {"reviewCurrency": "USD"})
    assert changes_match(state, {"reviewSelect": "t1"})
    assert not changes_match(state, {"reviewSelect": None})
    # 今日复盘: a client before the block reports no dailyReview and is not contradicted; one with it must show the day.
    assert changes_match(state, {"reviewDay": "2026-10-09"})
    shown = {**state, "review": {**state["review"], "dailyReview": {"tradingDay": "2026-10-09", "legs": 4, "planned": 3}}}
    assert changes_match(shown, {"reviewDay": "2026-10-09"}) and not changes_match(shown, {"reviewDay": "2026-10-08"})
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"reviewDay": "10-09"})
    assert changes_match(state, {"dialog": {"name": "account-detail", "open": True}})
    assert changes_match(state, {"dialog": {"name": "fee-hint", "open": False}})
    assert not changes_match(state, {"dialog": {"name": "account-detail", "open": False}})
    assert changes_match(state, {"strategyDetails": {"section": "rules", "open": True}})
    assert changes_match(state, {"monitorCard": {"name": "research", "expanded": False}})
    assert changes_match(state, {"selectPosition": {"symbol": "shfe.rb2610", "direction": "short"}})
    assert not changes_match(state, {"selectPosition": None})
    # 1.6.6: an older client still reports aiPage, a 1.6.6 client answers with panel + aiFilter.
    assert changes_match(state, {"aiPage": "chat"})
    new = {"panel": "ai", "aiFilter": "all"}
    assert changes_match(new, {"aiPage": "chat"})
    assert not changes_match(new, {"aiPage": "monitor"})
    assert changes_match({"panel": "ai", "aiFilter": "trading"}, {"aiPage": "monitor"})
    assert changes_match({"panel": "ai", "aiFilter": "all"}, {"aiFilter": "all"})
    # 2026-09-25: aiFilter is retired — a newer client reports no filter and only opens the 对话面板.
    assert changes_match({"panel": "ai", "aiFilter": None}, {"aiFilter": "trading"})
    assert not changes_match({"panel": "strategy", "aiFilter": None}, {"aiFilter": "trading"})
    assert changes_match({"panel": "ai", "aiFilter": None}, {"aiPage": "monitor"})
    # 成交: positionsTab fills, with trades (平仓记录) as its alias on either side.
    assert changes_match({"positionsTab": "fills"}, {"positionsTab": "trades"})
    assert changes_match({"positionsTab": "trades"}, {"positionsTab": "fills"})
    assert not changes_match({"positionsTab": "orders"}, {"positionsTab": "fills"})
    # The strategy panel's tab and the 成交 来源 filter are plain state fields.
    assert changes_match({"strategyTab": "running", "panel": "strategy"}, {"strategyTab": "running"})
    assert not changes_match({"strategyTab": None}, {"strategyTab": "detail"})  # a client too old to have tabs
    # 第八批 (2026-10-08): the retired 实验室 tab opens the 结果页 — a newer client reports detail, an older lab.
    assert changes_match({"strategyTab": "detail"}, {"strategyTab": "lab"})
    assert changes_match({"strategyTab": "lab"}, {"strategyTab": "lab"})
    assert not changes_match({"strategyTab": "library"}, {"strategyTab": "lab"})
    assert changes_match({"strategyTab": "detail", "strategyLab": {"status": "none"}}, {"strategyLab": {"open": True}})
    # 策略编辑卡 (2026-10-08) replaced the ask box and the guide card: their commands are refused with the
    # command that does the job now; the old state fields stay accepted (1.7.2 clients still report them).
    for retired in ({"strategyIdea": {"text": "突破就追", "send": True}}, {"strategyGuide": {"open": True}},
                    {"strategyTemplate": {"templateId": "double_ma", "run": True}}):
        with pytest.raises(ValidationError, match="strategyEditor"):
            UiChanges.model_validate(retired)
    assert UiChanges.model_validate({"strategyTemplate": {"restore": True}}).strategyTemplate
    # The phone's 策略 tabs.
    assert changes_match({"mobileStrategyTab": "mine"}, {"mobileStrategyTab": "mine"})
    assert not changes_match({"mobileStrategyTab": "running"}, {"mobileStrategyTab": "mine"})
    assert UiChanges.model_validate({"disclosure": {"name": "strategy-ask-ai", "open": True}}).disclosure
    assert changes_match({"fillsSource": "stop"}, {"fillsSource": "stop"})
    assert UiChanges.model_validate({"strategyTab": "library"}).strategyTab == "library"
    assert UiChanges.model_validate({"disclosure": {"name": "strategy-run-log", "open": True, "id": "run-1"}}).disclosure
    assert UiChanges.model_validate({"disclosure": {"name": "strategy-new", "open": True}}).disclosure
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"strategyTab": "fees"})
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"fillsSource": "broker"})
    # panel: positions opens the 持仓面板; a 1.4.8–1.6.5 client that expanded the bottom bar still counts.
    assert changes_match({"panel": "positions", "positionsBar": "collapsed"}, {"panel": "positions"})
    assert changes_match({"panel": None, "positionsBar": "expanded"}, {"panel": "positions"})
    assert not changes_match({"panel": None, "positionsBar": "collapsed"}, {"panel": "positions"})
    # positions-drawer stays as the panel's alias, for both client generations.
    assert changes_match({"panel": "positions"}, {"dialog": {"name": "positions-drawer", "open": True}})
    assert changes_match({"dialog": "positions-drawer"}, {"dialog": {"name": "positions-drawer", "open": True}})
    assert changes_match({"panel": "ai", "dialog": None}, {"dialog": {"name": "positions-drawer", "open": False}})
    assert not changes_match({"panel": "positions"}, {"dialog": {"name": "positions-drawer", "open": False}})
    # settingsPage: the split pages (2026-09-30); a 1.6.5 name in a command or an old client's state maps.
    assert changes_match({"settingsPage": "trade-advanced"}, {"settingsPage": "fees"})
    assert changes_match({"settingsPage": "trade"}, {"settingsPage": "simulation"})
    assert changes_match({"settingsPage": "appearance"}, {"settingsPage": "ui"})  # old client, new command
    assert not changes_match({"settingsPage": "chart"}, {"settingsPage": "ui"})
    assert UiChanges.model_validate({"settingsPage": "data"}).settingsPage == "data"
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"settingsPage": "privacy"})
    # chatSession: a known id must be the shown one; a new session's id is only known to the client.
    assert changes_match({**state, "chatSessionId": "s-2"}, {"chatSession": {"sessionId": "s-2"}})
    assert not changes_match({**state, "chatSessionId": "s-1"}, {"chatSession": {"sessionId": "s-2"}})
    assert changes_match({**state, "chatSessionId": "s-3"}, {"chatSession": {"new": True}})
    assert UiChanges.model_validate({"chatSession": {"new": True}}).chatSession.new
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"chatSession": {"sessionId": "s-1", "new": True}})
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"chatSession": {}})
    # The review page not mounted yet: not applied. A client that never reports the field: not contradicted.
    assert not changes_match({"review": None}, {"reviewExpand": {"day": "2026-09-10"}})
    old = {"panel": None}
    for changes in ({"reviewExpand": {"day": "2026-09-10"}}, {"dialog": {"name": "help", "open": True}}, {"pinnedSymbols": {"add": "SHFE.rb2701"}},
                    {"strategyDetails": {"section": "rules", "open": True}}, {"monitorCard": {"name": "research", "expanded": True}},
                    {"selectPosition": None}):
        assert changes_match(old, changes)


def test_board_and_contract_product_commands_match_and_old_clients_are_not_contradicted():
    from pydantic import ValidationError

    from trader_runtime.ui_control import ChartOptions, UiChanges, UiState, changes_match
    # One field per command; the board's filter and sort and the drawer's product each validate.
    assert UiChanges.model_validate({"view": "board"}).view == "board"
    assert UiChanges.model_validate({"boardExchange": "SHFE"}).boardExchange == "SHFE"
    assert UiChanges.model_validate({"boardExchange": "all"}).boardExchange == "all"
    assert UiChanges.model_validate({"boardExchange": "watchlist"}).boardExchange == "watchlist"  # 自选筛选
    assert UiChanges.model_validate({"boardSort": {"key": "volume", "order": "asc"}}).boardSort.order == "asc"
    assert UiChanges.model_validate({"contractProduct": "SHFE.rb"}).contractProduct == "SHFE.rb"
    assert UiChanges.model_validate({"contractProduct": None}).contractProduct is None
    for changes in ({"view": "board", "boardExchange": "SHFE"}, {"boardExchange": "NYSE"},
                    {"boardSort": {"key": "last", "order": "desc"}}, {"contractProduct": "rb"}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate(changes)
    # Judged by the state the board view and the drawer report; a client before the
    # board (or one not on it) reports no field and its receipt stands.
    board = {"view": "board", "boardExchange": "all", "boardSort": {"key": "changePercent", "order": "desc"}}
    assert changes_match(board, {"boardExchange": "all"})
    assert not changes_match(board, {"boardExchange": "DCE"})
    # 自选 (2026-10-02): the count rides with the exchange while the board is on screen.
    assert changes_match({**board, "boardExchange": "watchlist", "boardWatchlistCount": 2}, {"boardExchange": "watchlist"})
    assert changes_match(board, {"boardSort": {"key": "changePercent", "order": "desc"}})
    assert not changes_match(board, {"boardSort": {"key": "volume", "order": "desc"}})
    assert changes_match({**board, "boardSort": None}, {"boardSort": {"key": "openInterest", "order": "asc"}})
    assert changes_match({"view": "trade"}, {"boardExchange": "SHFE"})  # the board is not mounted
    drawer = {"dialog": "contract-drawer", "contractProduct": "SHFE.rb"}
    assert changes_match(drawer, {"contractProduct": "SHFE.rb"})
    assert not changes_match(drawer, {"contractProduct": "DCE.i"})
    assert changes_match({"dialog": "contract-drawer", "contractProduct": None}, {"contractProduct": None})
    assert changes_match({"dialog": None}, {"contractProduct": "SHFE.rb"})  # an older drawer
    # 合约抽屉“重试” (2026-10-11): only true; applied once a new read started, not when nothing can be read.
    assert UiChanges.model_validate({"contractCatalogRetry": True}).contractCatalogRetry is True
    for bad in ({"contractCatalogRetry": False}, {"contractCatalogRetry": None}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate(bad)
    retrying = {"dialog": "contract-drawer", "contractProduct": None}
    assert changes_match({**retrying, "contractCatalog": {"status": "loading", "canRetry": False}}, {"contractCatalogRetry": True})
    assert not changes_match({**retrying, "contractCatalog": {"status": "offline", "canRetry": False}}, {"contractCatalogRetry": True})
    assert changes_match(retrying, {"contractCatalogRetry": True})  # an older drawer without the report
    # The heartbeat keeps validating without the new report-only fields (older clients omit them).
    state = dict(symbol="SHFE.rb2610", duration=60, view="board", panel=None, settingsOpen=False,
                 theme="dark", dockSide="right", dockWidth=400, replay=False, settingsPage="account",
                 fontScale="standard", density="standard",
                 chart={k: True for k in ChartOptions.model_fields},
                 chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    assert UiState(**state).boardExchange is None and UiState(**state).boardSort is None
    assert UiState(**state).boardWatchlistCount is None
    assert UiState(**state, boardExchange="all",
                  boardSort={"key": "volume", "order": "asc"}, contractProduct="SHFE.rb",
                  boardWatchlistCount=3).boardWatchlistCount == 3


def test_chartview_command_and_the_30m_weekly_periods_match_the_reported_state():
    from pydantic import ValidationError

    from trader_runtime.ui_control import ChartOptions, UiChanges, UiState, changes_match

    # 30 分 and 周线 joined the period whitelist (2026-10-02); anything between the steps still refuses.
    assert UiChanges(duration=1800).duration == 1800 and UiChanges(duration=604800).duration == 604800
    with pytest.raises(ValidationError):
        UiChanges(duration=1200)
    # 分时 is the chart's second form over the same 60 s serial, not a period.
    assert UiChanges(chartView="timeshare").chartView == "timeshare" and UiChanges(chartView="kline").chartView == "kline"
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"chartView": "tick"})
    state = dict(symbol="SHFE.rb2610", duration=60, view="trade", panel=None, settingsOpen=False,
                 theme="dark", dockSide="right", dockWidth=400, replay=False, settingsPage="account",
                 fontScale="standard", density="standard", chart={k: True for k in ChartOptions.model_fields},
                 chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    # An older client omits the field; a new one reports which form the chart is in.
    assert UiState(**state).chartView is None
    assert UiState(**state, chartView="timeshare").chartView == "timeshare"
    assert changes_match({**state, "chartView": "timeshare"}, {"chartView": "timeshare"})
    assert not changes_match({**state, "chartView": "kline"}, {"chartView": "timeshare"})
    assert not changes_match(state, {"chartView": "timeshare"})  # a report without the field never matches
    # 分时 pins the data period to 60 s; a period command lands on kline with that period.
    assert changes_match({**state, "chartView": "timeshare", "duration": 60}, {"duration": 60})
    assert changes_match({**state, "chartView": "kline", "duration": 604800}, {"duration": 604800})
    assert not changes_match({**state, "chartView": "kline", "duration": 604800}, {"duration": 1800})


def test_disclosure_command_validates_names_and_card_ids_and_matches_reported_state():
    import pytest as _pytest

    from trader_runtime.ui_control import UiChanges, changes_match

    assert UiChanges(disclosure={"name": "protection-status", "open": True}).disclosure.name == "protection-status"
    assert UiChanges(disclosure={"name": "tool-card", "open": False, "id": "card-7"}).disclosure.id == "card-7"
    with _pytest.raises(ValueError):
        UiChanges(disclosure={"name": "protection-status", "open": True, "id": "x"})  # id is for card disclosures only
    with _pytest.raises(ValueError):
        UiChanges(disclosure={"name": "unknown", "open": True})  # type: ignore[typeddict-item]
    state = {"disclosures": {"protection-status": True, "tool-card": False, "tool-card:card-7": True}}
    assert changes_match(state, {"disclosure": {"name": "protection-status", "open": True}})
    assert not changes_match(state, {"disclosure": {"name": "protection-status", "open": False}})
    assert changes_match(state, {"disclosure": {"name": "tool-card", "open": False}})
    assert changes_match(state, {"disclosure": {"name": "tool-card", "open": True, "id": "card-7"}})
    assert not changes_match(state, {"disclosure": {"name": "tool-card", "open": True, "id": "card-8"}})
    assert not changes_match(state, {"disclosure": {"name": "replay-fees", "open": True}})  # not mounted
    assert changes_match({"panel": None}, {"disclosure": {"name": "replay-fees", "open": True}})  # old client
    # 合约信息卡 (2026-10-02)：合约头部的 ⓘ 弹层，普通折叠开关（不在卡内，没有 id）。
    assert UiChanges(disclosure={"name": "contract-info", "open": True}).disclosure.name == "contract-info"
    assert changes_match({"disclosures": {"contract-info": False}},
                         {"disclosure": {"name": "contract-info", "open": False}})
    assert not changes_match({"disclosures": {"contract-info": False}},
                             {"disclosure": {"name": "contract-info", "open": True}})
    with _pytest.raises(ValueError):
        UiChanges(disclosure={"name": "contract-info", "open": True, "id": "x"})
    for opened in (True, False):
        change = UiChanges(disclosure={"name": "feedback-diagnostics", "open": opened}).model_dump(exclude_unset=True)
        assert changes_match({"disclosures": {"feedback-diagnostics": opened}}, change)
        assert not changes_match({"disclosures": {"feedback-diagnostics": not opened}}, change)
    with _pytest.raises(ValueError):
        UiChanges(disclosure={"name": "feedback-diagnostics", "open": True, "id": "x"})


def test_drawing_surfaces_report_only_open_entries_and_the_old_popover_name_means_the_toolbar() -> None:
    """画线第二版 (2026-10-09): a drawing's style bar / 精确设置, a tool, a group and a right-click menu
    are named by id and reported only while open — a missing entry is closed. The first version's
    chart-drawing-tools still works and is judged by the toolbar the new client reports."""
    import pytest as _pytest

    from trader_runtime.ui_control import UiChanges, changes_match

    for name, entry in (("chart-drawing-style", "d1"), ("chart-drawing-settings", "d1"), ("chart-drawing-tool", "fib"),
                        ("chart-drawing-group", "shapes"), ("chart-context-menu", "pane:macd/swap"),
                        ("mobile-drawing-style-row", "width")):
        change = UiChanges(disclosure={"name": name, "open": True, "id": entry}).model_dump(exclude_unset=True)
        assert changes_match({"disclosures": {name: True, f"{name}:{entry}": True}}, change)
        assert not changes_match({"disclosures": {}}, change)
        closed = UiChanges(disclosure={"name": name, "open": False, "id": entry}).model_dump(exclude_unset=True)
        assert changes_match({"disclosures": {}}, closed)
        assert not changes_match({"disclosures": {f"{name}:{entry}": True}}, closed)
    for name in ("chart-drawing-toolbar", "chart-drawing-magnet", "chart-drawing-lock-all", "chart-drawings-hidden",
                 "chart-drawing-clear"):
        assert changes_match({"disclosures": {name: True}}, {"disclosure": {"name": name, "open": True}})
        with _pytest.raises(ValueError):
            UiChanges(disclosure={"name": name, "open": True, "id": "x"})
    old = {"disclosure": {"name": "chart-drawing-tools", "open": True}}
    assert changes_match({"disclosures": {"chart-drawing-toolbar": True}}, old)        # 画线第二版 client
    assert not changes_match({"disclosures": {"chart-drawing-toolbar": False}}, old)
    assert changes_match({"disclosures": {"chart-drawing-tools": True}}, old)          # first-version client
    # AI 审计 2026-10-13: the designer's old 查看完整规则 name is judged by the 高级 fold the client reports.
    full_rules = {"disclosure": {"name": "strategy-full-rules", "open": True}}
    assert changes_match({"disclosures": {"strategy-advanced": True}}, full_rules)
    assert not changes_match({"disclosures": {"strategy-advanced": False}}, full_rules)
    assert changes_match({"disclosures": {"strategy-full-rules": True}}, full_rules)  # older client
    # 策略的“手续费” opens 设置 → 高级交易设置 while the panel stays on 我的策略.
    fees = UiChanges.model_validate({"strategy": {"tab": "fees"}}).model_dump(exclude_unset=True)
    assert changes_match({"strategy": {"tab": "library"}, "settingsOpen": True, "settingsPage": "trade-advanced"}, fees)
    assert not changes_match({"strategy": {"tab": "library"}, "settingsOpen": False, "settingsPage": "account"}, fees)


def test_memory_and_routines_are_in_the_observed_state_while_their_page_shows() -> None:
    """AI 审计 2026-10-13：覆盖表与 SKILL 说“我的流程”“记忆”在快照里可见，但窗口状态没有这两项。
    设置 → AI 与记忆显示时，窗口回报 memory（开没开、各层几条）与 routines（每个流程与步骤）。"""
    from pydantic import ValidationError

    from trader_runtime.ui_control import ChartOptions, UiState

    base = dict(symbol=None, duration=60, view="trade", panel=None, settingsOpen=True, theme="dark",
                dockSide="right", dockWidth=340, replay=False, settingsPage="memory", fontScale="standard",
                density="standard", chart={k: True for k in ChartOptions.model_fields},
                chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    state = UiState.model_validate(base | {
        "memory": {"enabled": True, "counts": {"profile": 2, "rule": 1, "lesson": 0}},
        "routines": {"count": 1, "items": [{"id": "rt-1", "name": "老规矩", "steps": ["看行情：螺纹", "出卡：买一手"],
                                            "lastRunAt": None}]},
    }).model_dump()
    assert state["memory"]["counts"]["profile"] == 2 and state["routines"]["items"][0]["name"] == "老规矩"
    assert UiState.model_validate(base).memory is None  # another page: not reported
    for bad in ({"memory": {"enabled": True, "counts": {"secret": 1}}},
                {"routines": {"count": 1, "items": [{"id": "", "name": "x"}]}}):
        with pytest.raises(ValidationError):
            UiState.model_validate(base | bad)


def test_account_page_plan_fold_and_live_trading_switch_commands() -> None:
    """设置 → 账户 (2026-10-08): 档位 is a fold; 实盘 has no futures company behind it, so turning
    the switch on is refused with the reason and the snapshot reports it off with that reason."""
    import pytest as _pytest
    from pydantic import ValidationError

    from trader_runtime.ui_control import LIVE_TRADING_UNAVAILABLE, LiveTradingState, UiChanges, changes_match
    for opened in (True, False):
        change = UiChanges(disclosure={"name": "plan-quota", "open": opened}).model_dump(exclude_unset=True)
        assert changes_match({"disclosures": {"plan-quota": opened}}, change)
    with _pytest.raises(ValidationError, match=LIVE_TRADING_UNAVAILABLE):
        UiChanges(disclosure={"name": "live-trading", "open": True})
    off = UiChanges(disclosure={"name": "live-trading", "open": False}).model_dump(exclude_unset=True)
    assert changes_match({"disclosures": {"live-trading": False}}, off)
    assert LiveTradingState(state="off", disabledReason=LIVE_TRADING_UNAVAILABLE).state == "off"
    with _pytest.raises(ValidationError):
        LiveTradingState(state="on", disabledReason="")  # type: ignore[arg-type]


def test_proposal_confirm_dialog_can_only_be_closed_by_command():
    import pytest as _pytest

    from trader_runtime.ui_control import UiChanges, changes_match

    assert UiChanges(dialog={"name": "proposal-confirm", "open": False}).dialog.open is False
    with _pytest.raises(ValueError):
        UiChanges(dialog={"name": "proposal-confirm", "open": True})
    assert changes_match({"dialog": None}, {"dialog": {"name": "proposal-confirm", "open": False}})
    assert not changes_match({"dialog": "proposal-confirm"}, {"dialog": {"name": "proposal-confirm", "open": False}})


def test_trading_account_command_names_a_directory_id_and_old_clients_are_not_contradicted():
    from trader_runtime.ui_control import ChartOptions, UiChanges, UiState, changes_match

    # One command field at a time; the id names an account from GET /api/accounts.
    assert UiChanges(tradingAccount="tqkq-sim:abc123def456").tradingAccount == "tqkq-sim:abc123def456"
    with pytest.raises(ValueError):
        UiChanges.model_validate({"tradingAccount": "tqkq-sim:abc123def456", "panel": "ai"})
    with pytest.raises(ValueError):
        UiChanges.model_validate({"tradingAccount": ""})
    # UiState reports the session's account; a client before the switcher reports none.
    state = dict(symbol="SHFE.rb2610", duration=60, view="trade", panel=None, settingsOpen=False,
        theme="dark", dockSide="right", dockWidth=400, replay=False, settingsPage="account",
        fontScale="standard", density="standard", chart={k: True for k in ChartOptions.model_fields},
        chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    assert UiState(**state).tradingAccount is None
    assert UiState(**state, tradingAccount="tqkq-sim:default").tradingAccount == "tqkq-sim:default"
    # A new client's receipt must name the account it switched to; an old client's receipt stands.
    assert changes_match({"tradingAccount": "tqkq-sim:abc123def456"}, {"tradingAccount": "tqkq-sim:abc123def456"})
    assert not changes_match({"tradingAccount": "tqkq-sim:default"}, {"tradingAccount": "tqkq-sim:abc123def456"})
    assert changes_match({"panel": None}, {"tradingAccount": "tqkq-sim:abc123def456"})
    assert changes_match({"tradingAccount": None}, {"tradingAccount": "tqkq-sim:abc123def456"})


def test_strategy_editor_command_and_its_receipt():
    """策略编辑卡 (2026-10-08): the command's shape, and the receipt judged by the card the window reports —
    the same table as the client's useUiControl.matches (a mismatch either way locks or flips receipts)."""
    from pydantic import ValidationError

    from trader_runtime.ui_control import ChartOptions, UiChanges, UiState, changes_match

    assert UiChanges.model_validate({"strategyEditor": {"new": True, "start": "template:double_ma", "ask": "只做上午"}})
    for bad in ({"strategyEditor": {}}, {"strategyEditor": {"close": True, "new": True}},
                {"strategyEditor": {"new": True, "edit": "s1"}}, {"strategyEditor": {"start": "template:../x"}},
                {"strategyEditor": {"answers": {}}}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate(bad)
    card = {"open": True, "mode": "new", "strategyId": None, "start": "habits",
            "prefill": {"eligible": True}, "ask": {"text": "止损改成 20 跳", "status": "running"}}
    state = {"strategyEditor": card}
    assert changes_match(state, {"strategyEditor": {"new": True}})
    assert changes_match(state, {"strategyEditor": {"start": "habits"}})
    assert not changes_match(state, {"strategyEditor": {"start": "blank"}})
    assert changes_match(state, {"strategyEditor": {"ask": "止损改成 20 跳"}})
    assert not changes_match(state, {"strategyEditor": {"ask": "只做多"}})
    assert not changes_match(state, {"strategyEditor": {"edit": "s1"}})
    assert not changes_match(state, {"strategyEditor": {"close": True}})
    assert not changes_match(state, {"strategyEditor": {"save": True}})
    assert changes_match({"strategyEditor": {**card, "saving": True}}, {"strategyEditor": {"save": True}})
    # 根据我的交易生成策略 with too few trades: the card opens blank and says why — still applied.
    blank = {"strategyEditor": {"open": True, "start": "blank", "prefill": {"eligible": False, "reason": "不到 5 笔"}}}
    assert changes_match(blank, {"strategyEditor": {"new": True, "start": "habits"}})
    assert changes_match({"strategyEditor": {"open": True, "strategyId": "s1", "start": "blank"}},
                         {"strategyEditor": {"edit": "s1"}})
    assert not changes_match({"strategyEditor": {"open": True, "strategyId": "s1"}}, {"strategyEditor": {"new": True}})
    assert changes_match({"strategyEditor": {"open": False}}, {"strategyEditor": {"close": True}})
    # 条件提醒 (第 6 批): the window state carries 我的提醒's list (the readback first found it dropped by the model).
    from trader_runtime.ui_control import UiState
    assert "myReminders" in UiState.model_fields
    # 新建提醒 (第 6 批): applied only when the card opened in 提醒模式; reminder goes with new.
    assert changes_match({"strategyEditor": {"open": True, "reminder": True}}, {"strategyEditor": {"new": True, "reminder": True}})
    assert not changes_match({"strategyEditor": {"open": True}}, {"strategyEditor": {"new": True, "reminder": True}})
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"strategyEditor": {"reminder": True}})
    # A save closes the card at once; the closed snapshot keeps the saved strategy as the proof.
    assert changes_match({"strategyEditor": {"open": False, "savedStrategyId": "s9"}}, {"strategyEditor": {"save": True}})
    # Still loading the prefill / template: new / start are not applied yet.
    assert not changes_match({"strategyEditor": {**card, "loading": "正在读你的成交…"}}, {"strategyEditor": {"new": True}})
    # The retired 策略设计窗's dialog name opens the card (no dialog is reported): judged by the card;
    # a client before the card still reports the dialog itself.
    designer = {"dialog": {"name": "strategy-designer", "open": True}}
    assert changes_match({"dialog": None, "strategyEditor": {"open": True}}, designer)
    assert not changes_match({"dialog": None, "strategyEditor": {"open": False}}, designer)
    assert changes_match({"dialog": None, "strategyEditor": {"open": False}}, {"dialog": {"name": "strategy-designer", "open": False}})
    assert changes_match({"dialog": "strategy-designer"}, designer)
    assert not changes_match({"dialog": None}, designer)
    # 第二版 (2026-10-08): 流程图定位 by the card's own focus; a card before it reports none (the client's receipt stands).
    assert UiChanges.model_validate({"strategyEditor": {"focus": "stop"}})
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"strategyEditor": {"focus": "nowhere"}})
    assert changes_match({"strategyEditor": {**card, "focus": "stop"}}, {"strategyEditor": {"focus": "stop"}})
    assert not changes_match({"strategyEditor": {**card, "focus": None}}, {"strategyEditor": {"focus": "stop"}})
    assert changes_match(state, {"strategyEditor": {"focus": "stop"}})
    # The picker and every "?" are per-id disclosures; all of them are reported (not only the last 20).
    for name, entry in (("strategy-editor-picker", "entryLong:1:left"), ("help-tip", "strategy-editor:section-entry")):
        assert UiChanges.model_validate({"disclosure": {"name": name, "id": entry, "open": True}})
        assert changes_match({"disclosures": {f"{name}:{entry}": True}}, {"disclosure": {"name": name, "id": entry, "open": True}})
    assert UiChanges.model_validate({"disclosure": {"name": "strategy-editor-sizing-more", "open": True}})
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"disclosure": {"name": "strategy-editor-sizing-more", "id": "x", "open": True}})
    # A 1.7.2 client still reports the retired guide card and ask box in its heartbeat: accepted.
    base = dict(symbol=None, duration=60, view="trade", panel=None, settingsOpen=False, theme="dark",
                dockSide="right", dockWidth=340, replay=False, settingsPage="account", fontScale="standard",
                density="standard", chart={k: True for k in ChartOptions.model_fields},
                chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    UiState.model_validate(base | {"strategyGuide": {"open": False, "stage": "none"}, "strategyIdea": {"stage": "idle"},
                                   "strategyEditor": card})



def test_quick_volume_is_a_local_ui_setting_and_receipt_matches():
    from pydantic import ValidationError

    from trader_runtime.ui_control import UiChanges, changes_match

    for lots in (1, 2, 5):
        command = UiChanges(quickVolume=lots).model_dump(exclude_unset=True)
        assert changes_match({"quickVolume": lots}, command)
        assert not changes_match({"quickVolume": 3}, command)
    with pytest.raises(ValidationError):
        UiChanges(quickVolume=3)

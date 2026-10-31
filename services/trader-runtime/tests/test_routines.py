"""我的流程 (2026-10-06): the 流程卡 is only a draft until the user's own 保存; the limits and the
refusal of anything that would make a routine an order path; the exact-name trigger; and the run —
step by step over each step's own tools, cards only (autonomy on or off), stopping at a failed step."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fixtures.fake_responses_server import FakeResponsesServer, sse
from test_ai_annotations import HEADERS, with_tools
from trader_runtime.main import create_app
from trader_runtime.routines import (
    GONE_LINE,
    NEVER_IN_ROUTINE,
    STEP_TOOLS,
    advisory_step,
    call_name,
    step_instruction,
    step_manner,
    step_operations,
    step_outcome,
    step_tools,
    step_view,
)
from trader_runtime.testing.fake_gateway import FakeSimulationGateway
from trader_runtime.voice_intents import routine_request

USER = "local-user"
STEPS = [{"kind": "read", "text": "读自选品种的今日判断"}, {"kind": "card", "text": "按约定出开仓卡"},
         {"kind": "read", "text": "看一下账户"}]


def _app(tmp_path: Path):
    return create_app(tmp_path / "routines.sqlite3", runtime_state_root=tmp_path, agent_mode="fake",
                      local_token="annotation-test-token")


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS)


async def _say(app, text: str) -> str:
    session = app.state.broker.create_session("local", "fake", USER)
    await app.state.broker.publish(session.session_id, kind="message", name="message.completed",
                                   payload={"turnId": "t-user", "role": "user", "text": text})
    return session.session_id


async def _ai(client: AsyncClient, operation: str, body: dict | None = None, path: dict | None = None) -> dict:
    payload: dict[str, Any] = {"operation": operation, **({"body": body} if body is not None else {}),
                               **({"path": path} if path else {})}
    return (await client.post("/api/agent/call", json=payload)).json()


async def _saved(client: AsyncClient, name: str = "老规矩", steps: list | None = None) -> dict:
    """A routine the user saved from a 流程卡 (draft through the route, the user's 保存)."""
    draft = await client.post("/api/routines/drafts", json={"name": name, "steps": steps or STEPS})
    assert draft.status_code == 201, draft.text
    saved = await client.post("/api/routines", json={"draftId": draft.json()["draft"]["id"]})
    assert saved.status_code == 201, saved.text
    return saved.json()


# -- 流程卡: the AI drafts, only the user's 保存 writes -------------------------------------------------

async def test_the_ai_only_drafts_a_card_and_the_users_save_writes_it(tmp_path):
    app = _app(tmp_path)
    with_tools(app)
    async with app.router.lifespan_context(app), _client(app) as client:
        session_id = await _say(app, "以后我说「老规矩」，就帮我看判断、出开仓卡、看账户")
        proposed = await _ai(client, "tool:propose_routine", {"name": "老规矩", "steps": STEPS})
        assert proposed["ok"], proposed
        draft = proposed["data"]["data"]["draft"]
        assert proposed["data"]["data"]["status"] == "draft"
        # A draft is not a routine; the catalog's save route refuses the AI (and so does PATCH).
        assert (await client.get("/api/routines")).json()["items"] == []
        refused = await _ai(client, "POST /api/routines", {"draftId": draft["id"]})
        assert refused["status"] == 403 and "保存" in refused["data"]["detail"]
        assert (await client.get("/api/routines")).json()["items"] == []
        # The card went to the conversation the tool ran in, with its types and the card-step note.
        cards = [event["payload"] for _, event in app.state.store.list_events(app.state.app_capabilities.sessions[USER])
                 if event["name"] == "card.routine"]
        assert cards and cards[-1]["status"] == "draft" and [step["kindLabel"] for step in cards[-1]["steps"]] == ["读", "出卡", "读"]
        assert "下单仍要你点确认" in cards[-1]["note"]
        # The user's own 保存 writes it once and republishes the card as saved; a second 保存 is 409.
        saved = await client.post("/api/routines", json={"draftId": draft["id"]})
        assert saved.status_code == 201 and saved.json()["name"] == "老规矩" and saved.json()["stepCount"] == 3
        again = await client.post("/api/routines", json={"draftId": draft["id"]})
        assert again.status_code == 409
        republished = [event["payload"] for _, event in app.state.store.list_events(app.state.app_capabilities.sessions[USER])
                       if event["name"] == "card.routine"]
        assert republished[-1]["status"] == "saved" and republished[-1]["cardId"] == cards[-1]["cardId"]
        # Same name again: the draft is a change to the saved routine, not a second one.
        change = await _ai(client, "tool:propose_routine", {"name": "老规矩", "steps": STEPS[:2]})
        assert change["data"]["data"]["draft"]["routineId"] == saved.json()["id"]
        assert session_id  # the user's sentence lives in its own session


async def test_limits_unique_names_patch_and_delete(tmp_path):
    app = _app(tmp_path)
    async with app.router.lifespan_context(app), _client(app) as client:
        for index in range(5):
            await _saved(client, name=f"流程{index}", steps=STEPS[:1])
        sixth = await client.post("/api/routines/drafts", json={"name": "第六个", "steps": STEPS[:1]})
        assert sixth.status_code == 422 and "最多 5 个" in sixth.json()["detail"]
        assert (await client.post("/api/routines", json={"name": "第六个", "steps": STEPS[:1]})).status_code == 422
        eleven = await client.post("/api/routines/drafts", json={"name": "流程0", "steps": [STEPS[0]] * 11})
        assert eleven.status_code == 422 and "最多 10 步" in eleven.json()["detail"]
        too_long = await client.post("/api/routines/drafts", json={"name": "一二三四五六七八九十一二三", "steps": STEPS[:1]})
        assert too_long.status_code == 422
        routines = (await client.get("/api/routines")).json()["items"]
        first, second = routines[0], routines[1]
        # Rename and step edits are the user's; a taken name is 409; the AI may not patch.
        assert (await client.patch(f"/api/routines/{first['id']}", json={"name": second["name"]})).status_code == 409
        renamed = await client.patch(f"/api/routines/{first['id']}", json={"name": "收盘检查", "steps": STEPS})
        assert renamed.status_code == 200 and renamed.json()["name"] == "收盘检查" and renamed.json()["stepCount"] == 3
        assert (await _ai(client, "PATCH /api/routines/{routine_id}", {"name": "别的"}, {"routine_id": first["id"]}))["status"] == 403
        assert (await client.delete(f"/api/routines/{second['id']}")).status_code == 200
        assert (await client.delete(f"/api/routines/{second['id']}")).status_code == 404
        assert len((await client.get("/api/routines")).json()["items"]) == 4


@pytest.mark.parametrize("steps, said", [
    ([{"kind": "card", "text": "直接下单不用问我"}], "以后我说老规矩就出卡"),
    ([{"kind": "read", "text": "把自主执行打开"}], "以后我说老规矩就打开自主执行"),
    ([{"kind": "read", "text": "看判断"}], "老规矩里直接下单，不用问我"),  # clean steps, the user's own words ask for it
])
async def test_authorization_wording_is_refused_without_a_save(tmp_path, steps, said):
    app = _app(tmp_path)
    with_tools(app)
    async with app.router.lifespan_context(app), _client(app) as client:
        await _say(app, said)
        proposed = await _ai(client, "tool:propose_routine", {"name": "老规矩", "steps": steps})
        assert proposed["ok"] and proposed["data"]["data"]["status"] == "refused"
        assert "免确认下单" in proposed["data"]["data"]["refusal"]
        draft_id = proposed["data"]["data"]["draft"]["id"]
        saved = await client.post("/api/routines", json={"draftId": draft_id})
        assert saved.status_code == 422 and (await client.get("/api/routines")).json()["items"] == []
    # The user's own edit is held to the same rule.
    app2 = _app(tmp_path / "second")
    async with app2.router.lifespan_context(app2), _client(app2) as client:
        routine = await _saved(client)
        refused = await client.patch(f"/api/routines/{routine['id']}", json={"steps": [{"kind": "card", "text": "自动下单"}]})
        assert refused.status_code == 422


def test_only_the_exact_name_or_a_run_verb_triggers():
    names = {"老规矩": "a", "收盘检查": "b"}
    assert routine_request("老规矩", names) == "老规矩"
    assert routine_request("老规矩。", names) == "老规矩"
    assert routine_request("跑一下老规矩", names) == "老规矩"
    assert routine_request("运行「收盘检查」", names) == "收盘检查"
    for text in ("老规矩是什么意思", "老规矩去掉回测那步", "按老规矩办？我想想", "跑一下", "看看螺纹", "规矩"):
        assert routine_request(text, names) is None, text
    assert routine_request("老规矩", {}) is None


def test_no_step_type_ever_carries_an_order_or_settings_tool():
    for kind, tools in STEP_TOOLS.items():
        assert not tools & NEVER_IN_ROUTINE - {"app_capabilities"}, kind
        assert "execute_simulation_trade" not in tools and "update_preferences" not in tools


async def test_guests_cannot_reach_routines(tmp_path):
    app = create_app(tmp_path / "guest.db", agent_mode="fake", data_mode="fake", local_token="private-token",
                     cors_origins={"http://localhost"})
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost",
                               headers={"origin": "http://localhost"}) as guest:
            await guest.post("/api/auth/guest")
            assert (await guest.get("/api/routines")).status_code in (401, 403)
            assert (await guest.post("/api/agent/call", json={"operation": "GET /api/routines"})).status_code == 403
            manifest = (await guest.get("/api/agent/capabilities")).json()
            assert not any("/api/routines" in item["operation"] for item in manifest["operations"])
    finally:
        await app.state.guests.close()
        app.state.store.close()


# -- running: the native loop, scripted model ------------------------------------------------------

class ScriptedResponses(FakeResponsesServer):
    """Answers by the step instruction in the latest user item; records what each request offered."""

    def __init__(self) -> None:
        super().__init__(expected_keys={})
        self.offered: list[tuple[str, set[str]]] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        text = self._latest_user_text(body)
        names = {str(tool.get("name")) for tool in body.get("tools") or []}
        self.offered.append((text, names))
        items = body.get("input") or []
        last_user = max((index for index, item in enumerate(items) if isinstance(item, dict) and item.get("role") == "user"), default=-1)
        outputs = [json.loads(str(item.get("output") or "{}")) for item in items[last_user + 1:]
                   if isinstance(item, dict) and item.get("type") == "function_call_output"]
        events = self.step(text, outputs)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"".join(sse(e) for e in events))

    def step(self, text: str, outputs: list[dict]) -> list[dict]:
        if "流程「" not in text:
            return self._text_stream("好的。")
        if "坏掉" in text:
            return self._text_stream("失败：数据源没有回应")
        if "下单捣乱" in text and not outputs:
            return self._tool_stream([("execute_simulation_trade", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1})])
        if "下单捣乱" in text:
            return self._text_stream("没下成")
        if "超档" in text:
            # 先读订阅、再加一个加不进去的品种；收尾只说没能添加（没有“失败：”前缀）。
            if not outputs:
                return self._tool_stream([("app_call", {"requestJson": json.dumps({"operation": "GET /api/app/brief-subscriptions"})})])
            if len(outputs) == 1:
                return self._tool_stream([("app_call", {"requestJson": json.dumps({
                    "operation": "PUT /api/app/brief-subscriptions", "body": {"products": ["ZZZ.nope"]}})})])
            return self._text_stream("订阅已到上限，没能添加")
        if "（提醒" in text and not outputs:
            # A model that would set the alert whenever it is offered the tool.
            if "create_price_alert" in self.offered[-1][1]:
                return self._tool_stream([("create_price_alert", {"symbol": "SHFE.au2612", "price": 825.0})])
            return self._text_stream("有必要：3127 上方设一个突破提醒，要设就说一声")
        if "（出卡" in text and not outputs:
            return self._tool_stream([("propose_order", {
                "act": "open", "symbol": "SHFE.au2612", "dir": "short", "vol": 1, "price": "queue", "reason": "流程出卡"})])
        if "（出卡" in text:
            return self._text_stream("出了 1 张开仓卡，等你确认")
        if not outputs:
            return self._tool_stream([("get_account_summary", {})])
        return self._text_stream("读完账户，权益正常")


TOKEN = "routine-native-token"
ORIGIN = "http://127.0.0.1:5173"


async def _native(tmp_path: Path, monkeypatch, *, gateway: bool):
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-fixture-zhipu-not-real")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    server = ScriptedResponses()
    app = create_app(tmp_path / "native.sqlite3", local_token=TOKEN, agent_mode="native", agent_provider="deepseek",
                     deepseek_api_key="sk-fixture-deepseek-not-real", native_transport=server.transport(),
                     engineering_root=tmp_path / "project", **({"simulation_gateway": FakeSimulationGateway()} if gateway else {}))
    return app, server


async def _native_client(app: FastAPI) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"origin": ORIGIN})
    assert (await client.post("/api/auth/local", headers={"authorization": f"Bearer {TOKEN}", "origin": ORIGIN})).status_code == 200
    return client


async def _close(app: FastAPI) -> None:
    await app.state.runs.shutdown()
    await app.state.adapter.shutdown()
    await app.state.broker.shutdown()
    app.state.store.close()


async def _run(app: FastAPI, client: AsyncClient, session_id: str, text: str, routine_id: str | None = None) -> dict:
    response = await client.post(f"/api/sessions/{session_id}/messages",
                                 json={"text": text, **({"routineId": routine_id} if routine_id else {})})
    assert response.status_code == 202, response.text
    return await _wait(app, response.json()["runId"])


async def _wait(app: FastAPI, run_id: str) -> dict:
    for _ in range(400):
        run = app.state.store.get_run(run_id)
        if run["status"] in {"completed", "failed", "cancelled"}:
            await asyncio.sleep(0.02)
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


def _events(app: FastAPI, session_id: str, name: str) -> list[dict]:
    return [event["payload"] for _, event in app.state.store.list_events(session_id) if event["name"] == name]


async def _session(client: AsyncClient) -> str:
    return (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]


async def test_a_run_works_each_step_over_its_own_tools_and_the_card_step_waits_for_the_user(tmp_path, monkeypatch):
    app, server = await _native(tmp_path, monkeypatch, gateway=False)
    try:
        client = await _native_client(app)
        routine = await _saved(client)
        session_id = await _session(client)
        assert (await _run(app, client, session_id, "老规矩"))["status"] == "completed"
        cards = _events(app, session_id, "card.routine_run")
        final = cards[-1]
        assert final["status"] == "completed" and final["confirmCount"] == 1
        assert [step["status"] for step in final["steps"]] == ["done", "confirm", "done"]
        # The card ticks the steps off in order: each step goes active before it is done.
        sequence = [[step["status"] for step in card["steps"]] for card in cards]
        assert sequence[1] == ["active", "waiting", "waiting"] and ["done", "active", "waiting"] in sequence
        assert ["done", "confirm", "active"] in sequence
        proposals = _events(app, session_id, "proposal.trade")
        assert len(proposals) == 1 and proposals[0]["status"] == "approval_required"
        assert app.state.simulation_orders.list(user_id=USER) == []
        # Every request offered only that step's tools (plus ones earlier steps already used).
        step_requests = [(text, names) for text, names in server.offered if "流程「" in text]
        assert step_requests
        for text, names in step_requests:
            kind = "card" if "（出卡" in text else "read"
            assert names <= STEP_TOOLS[kind] | STEP_TOOLS["read"] | STEP_TOOLS["card"], names
            assert not names & NEVER_IN_ROUTINE
        card_step = [names for text, names in step_requests if "（出卡" in text][0]
        assert "propose_order" in card_step and "get_futures_decision_context" not in card_step
        answer = [m for m in _events(app, session_id, "message.completed") if m.get("role") == "assistant"][-1]
        assert answer["text"] == "「老规矩」跑完了：3 件事，1 步出了卡等你确认。"
        assert (await client.get("/api/routines")).json()["items"][0]["lastRunAt"] is not None
        assert routine["id"] == final["routineId"]
        await client.aclose()
    finally:
        await _close(app)


async def test_autonomy_on_still_offers_no_direct_order_tool_and_a_refused_step_stops_the_run(tmp_path, monkeypatch):
    app, server = await _native(tmp_path, monkeypatch, gateway=True)
    try:
        client = await _native_client(app)
        await _saved(client, steps=[{"kind": "read", "text": "看账户"}, {"kind": "card", "text": "下单捣乱"},
                                    {"kind": "read", "text": "再看账户"}])
        session_id = await _session(client)
        # Autonomy is on in this app, yet a turn the user's message caused is never offered the direct order tool
        # (owner 2026-10-16: its orders are cards; AI 自主执行 is for what the AI or a strategy starts itself) — even
        # with the skill that carries it mounted by its words …
        assert (await _run(app, client, session_id, "查一下委托状态"))["status"] == "completed"
        assert "cancel_simulation_order" in server.offered[-1][1] and "execute_simulation_trade" not in server.offered[-1][1]
        # … nor is a routine run, and the model's attempt anyway is refused at execution.
        assert (await _run(app, client, session_id, "跑一下老规矩"))["status"] == "completed"
        step_requests = [names for text, names in server.offered if "流程「" in text]
        assert step_requests and all("execute_simulation_trade" not in names for names in step_requests)
        final = _events(app, session_id, "card.routine_run")[-1]
        assert [step["status"] for step in final["steps"]] == ["done", "failed", "skipped"] and final["status"] == "stopped"
        assert not any("第 3/3 步" in text for text, _ in server.offered)  # the step after the failure never ran
        assert app.state.simulation_orders.list(user_id=USER) == []
        answer = [m for m in _events(app, session_id, "message.completed") if m.get("role") == "assistant"][-1]
        assert answer["text"].startswith("「老规矩」停在第 2 步") and "后面 1 步没有做" in answer["text"]
        await client.aclose()
    finally:
        await _close(app)


async def test_a_failed_step_stops_there_and_cards_already_made_stay(tmp_path, monkeypatch):
    app, server = await _native(tmp_path, monkeypatch, gateway=False)
    try:
        client = await _native_client(app)
        await _saved(client, steps=[{"kind": "card", "text": "出开仓卡"}, {"kind": "read", "text": "坏掉的数据"},
                                    {"kind": "read", "text": "看账户"}])
        session_id = await _session(client)
        assert (await _run(app, client, session_id, "老规矩"))["status"] == "completed"
        final = _events(app, session_id, "card.routine_run")[-1]
        assert [step["status"] for step in final["steps"]] == ["confirm", "failed", "skipped"]
        assert final["steps"][1]["result"] == "数据源没有回应"
        assert not any("第 3/3 步" in text for text, _ in server.offered)
        assert _events(app, session_id, "proposal.trade")[0]["status"] == "approval_required"
        answer = [m for m in _events(app, session_id, "message.completed") if m.get("role") == "assistant"][-1]
        assert "已出的卡照常可以确认" in answer["text"]
        await client.aclose()
    finally:
        await _close(app)


def test_a_step_worded_as_see_if_needed_only_advises():
    """保护：步骤原话说“看看有没有必要 / 要不要”时，流程只给建议，不擅自设提醒、画线或改订阅（10-06 实测）。"""
    every = set().union(*STEP_TOOLS.values())
    tentative = {"kind": "alert", "text": "看看有没有必要设个到价提醒"}
    plain = {"kind": "alert", "text": "在 3127 设上方提醒"}
    assert advisory_step(tentative) and not advisory_step(plain)
    assert "create_price_alert" not in step_tools("alert", every, advisory=True)
    assert "create_price_alert" in step_tools("alert", every)
    assert advisory_step({"kind": "draw", "text": "需要的话画一下支撑"})
    assert "create_chart_drawing" not in step_tools("draw", every, advisory=True)
    assert step_operations("watch", advisory=advisory_step({"kind": "watch", "text": "要不要盯一下螺纹"})) == {
        "GET /api/app/brief-subscriptions"}
    assert not advisory_step({"kind": "card", "text": "看看要不要出平仓卡"})  # a card waits for 确认 anyway
    instruction = step_instruction({"name": "老规矩", "steps": [tentative]}, 0)
    assert "「看看有没有必要设个到价提醒」" in instruction and "要设就说一声" in instruction and "不要设" in instruction


async def test_an_advisory_alert_step_sets_nothing_and_reports_its_advice(tmp_path, monkeypatch):
    app, server = await _native(tmp_path, monkeypatch, gateway=False)
    try:
        client = await _native_client(app)
        await _saved(client, steps=[{"kind": "read", "text": "看账户"}, {"kind": "alert", "text": "看看有没有必要设个到价提醒"}])
        session_id = await _session(client)
        assert (await _run(app, client, session_id, "老规矩"))["status"] == "completed"
        alert_requests = [names for text, names in server.offered if "（提醒" in text]
        assert alert_requests and all("create_price_alert" not in names for names in alert_requests)
        final = _events(app, session_id, "card.routine_run")[-1]
        assert [step["status"] for step in final["steps"]] == ["done", "done"]
        assert final["steps"][1]["result"] == "有必要：3127 上方设一个突破提醒，要设就说一声"
        alerts = await client.get("/api/market/alerts")
        assert alerts.status_code == 200 and alerts.json()["active"] == []
        await client.aclose()
    finally:
        await _close(app)


@pytest.mark.parametrize(("kind", "text", "manner"), [
    ("alert", "看看要不要给螺纹设个到价提醒", "advisory"),
    ("alert", "看看有没有必要设个到价提醒", "advisory"),
    ("draw", "需要的话画一下支撑", "advisory"),
    ("watch", "是否需要盯一下螺纹", "advisory"),
    ("alert", "看看要不要给螺纹设个到价提醒，需要就设一个", "conditional"),
    ("alert", "看看有没有必要设提醒，有必要就设", "conditional"),
    ("alert", "该设就设个夜盘提醒", "conditional"),
    ("draw", "需要的话就画一下支撑", "conditional"),
    ("watch", "螺纹合适就加进盯异动", "conditional"),
    ("alert", "在 3127 设上方提醒", None),
    ("draw", "画一下前高", None),
    ("card", "看看要不要出平仓卡，需要就出", None),  # a card waits for 确认 anyway
    ("read", "看看要不要读一下判断", None),
])
def test_the_users_words_decide_whether_a_step_advises_acts_if_needed_or_just_acts(kind, text, manner):
    """保护：步骤只说“看看要不要”才只给建议；带“需要就设”这类执行从句是需要时执行，卡上如实标注（10-06 实测卡上说会设、实际不设）。"""
    step = {"kind": kind, "text": text}
    assert step_manner(step) == manner
    assert advisory_step(step) == (manner == "advisory")
    assert step_view(step).get("manner") == manner
    every = set().union(*STEP_TOOLS.values())
    if manner == "conditional":
        assert set(STEP_TOOLS[kind]) - NEVER_IN_ROUTINE <= step_tools(kind, every, advisory=advisory_step(step))
        instruction = step_instruction({"name": "老规矩", "steps": [step]}, 0)
        assert f"「{text}」" in instruction and "有必要就用这一步的工具做好" in instruction and "不要设" not in instruction


async def test_a_watch_step_whose_add_fails_after_a_good_read_stops_the_run(tmp_path, monkeypatch):
    """保护：一步先读成功、要做的写入失败时停在这一步，后面不再跑（Codex #522 10-06）。"""
    app, server = await _native(tmp_path, monkeypatch, gateway=False)
    try:
        client = await _native_client(app)
        await _saved(client, steps=[{"kind": "watch", "text": "超档也盯一下橡胶异动"}, {"kind": "read", "text": "看账户"}])
        session_id = await _session(client)
        assert (await _run(app, client, session_id, "老规矩"))["status"] == "completed"
        final = _events(app, session_id, "card.routine_run")[-1]
        assert [step["status"] for step in final["steps"]] == ["failed", "skipped"] and final["status"] == "stopped"
        assert final["steps"][0]["result"] == "订阅已到上限，没能添加"
        assert not any("第 2/2 步" in text for text, _ in server.offered)
        await client.aclose()
    finally:
        await _close(app)


def test_a_steps_own_action_decides_it_not_the_read_before_it():
    read, put = ("GET /api/app/brief-subscriptions", True, False), "PUT /api/app/brief-subscriptions"
    assert step_outcome("watch", "", [read, (put, False, False)]) == ("failed", "这一步要做的事没做成")
    assert step_outcome("alert", "没设上", [("get_market_snapshot", True, False), ("create_price_alert", False, False)])[0] == "failed"
    assert step_outcome("card", "没出成", [("get_positions", True, False), ("propose_order", False, False)])[0] == "failed"
    # A retry that worked, a refused off-step call, or a plain read step stay as they were.
    assert step_outcome("watch", "加上了", [read, (put, False, False), (put, True, False)]) == ("done", "加上了")
    assert step_outcome("alert", "只给了建议", [("routine_refused", False, False), ("get_market_snapshot", True, False)])[0] == "done"
    assert step_outcome("read", "读完了", [("get_positions", False, False), ("get_positions", True, False)])[0] == "done"
    assert call_name("app_call", {"requestJson": json.dumps({"operation": put})}) == put
    assert call_name("app_call", {"requestJson": "坏"}) == "app_call"


def test_a_judged_step_that_needed_nothing_is_done_but_a_plain_one_is_not():
    assert step_outcome("alert", "现在离关键位还远，不用设", [], judged=True) == ("done", "现在离关键位还远，不用设")
    assert step_outcome("alert", "设好了", [])[0] == "failed"


def test_the_drafting_prompt_keeps_the_users_words_and_adds_no_promise_to_act():
    """保护：拟卡时保留用户原话、不追加“需要就设”，回复按卡上的标注说（10-06 实测）。"""
    from trader_runtime.message_context import ROUTINE_DEFINE_INSTRUCTION
    from trader_runtime.trader_tools import TOOL_ARGUMENT_MODELS
    assert "保留用户原话" in ROUTINE_DEFINE_INSTRUCTION and "不要自己追加“需要就设" in ROUTINE_DEFINE_INSTRUCTION
    text_field = TOOL_ARGUMENT_MODELS["propose_routine"].model_json_schema()["$defs"]["RoutineStepArguments"]["properties"]["text"]
    assert "保留用户原话" in text_field["description"] and "需要就设" in text_field["description"]


async def test_an_act_if_needed_alert_step_is_offered_the_alert_tool_and_sets_it(tmp_path, monkeypatch):
    app, server = await _native(tmp_path, monkeypatch, gateway=False)
    try:
        client = await _native_client(app)
        await _saved(client, steps=[{"kind": "read", "text": "看账户"},
                                    {"kind": "alert", "text": "看看要不要给螺纹设个到价提醒，需要就设一个"}])
        session_id = await _session(client)
        assert (await _run(app, client, session_id, "老规矩"))["status"] == "completed"
        alert_requests = [names for text, names in server.offered if "（提醒" in text]
        assert alert_requests and "create_price_alert" in alert_requests[0]
        final = _events(app, session_id, "card.routine_run")[-1]
        assert final["steps"][1]["manner"] == "conditional" and [step["status"] for step in final["steps"]] == ["done", "done"]
        alerts = await client.get("/api/market/alerts")
        assert alerts.status_code == 200 and len(alerts.json()["active"]) == 1
        await client.aclose()
    finally:
        await _close(app)


async def test_the_next_turn_knows_the_card_was_saved_renamed_or_deleted(tmp_path, monkeypatch):
    """保护：用户在流程卡上点了保存（或在设置里改名、删除）后，下一轮模型知道，不再说“还要点保存”（10-06 实测）。"""
    app, server = await _native(tmp_path, monkeypatch, gateway=False)
    try:
        client = await _native_client(app)
        session_id = await _session(client)

        def latest_context() -> str:
            items = server.requests[-1].get("input") or []
            return next(str(item.get("content")) for item in reversed(items) if isinstance(item, dict) and item.get("role") == "user")
        draft = (await client.post("/api/routines/drafts", json={"name": "老规矩", "steps": STEPS, "sessionId": session_id})).json()["draft"]
        await _run(app, client, session_id, "你好")
        assert "「老规矩」还没保存" in latest_context()
        saved = (await client.post("/api/routines", json={"draftId": draft["id"]})).json()
        await _run(app, client, session_id, "存好了吗")
        assert "「老规矩」已保存" in latest_context() and "用户现在保存的流程：「老规矩」" in latest_context()
        assert (await client.patch(f"/api/routines/{saved['id']}", json={"name": "早盘"})).status_code == 200
        await _run(app, client, session_id, "现在叫什么")
        assert "后来在设置里改名或删除了" in latest_context() and "用户现在保存的流程：「早盘」" in latest_context()
        assert (await client.delete(f"/api/routines/{saved['id']}")).status_code == 200
        await _run(app, client, session_id, "还在吗")
        assert "用户现在保存的流程：没有" in latest_context()
        await client.aclose()
    finally:
        await _close(app)


async def test_a_question_about_the_routine_does_not_run_it(tmp_path, monkeypatch):
    app, server = await _native(tmp_path, monkeypatch, gateway=False)
    try:
        client = await _native_client(app)
        await _saved(client)
        session_id = await _session(client)
        assert (await _run(app, client, session_id, "老规矩是什么意思"))["status"] == "completed"
        assert _events(app, session_id, "card.routine_run") == []
        assert not any("流程「" in text for text, _ in server.offered)
        # A definition sentence is a general turn carrying the 流程卡 instruction and the draft tool.
        assert (await _run(app, client, session_id, "以后我说「收盘检查」，就帮我看持仓、设夜盘提醒"))["status"] == "completed"
        text, names = server.offered[-1]
        assert "propose_routine" in names and "建一个“我的流程”" in json.dumps(server.requests[-1]["input"], ensure_ascii=False)
        await client.aclose()
    finally:
        await _close(app)


async def test_a_picked_run_runs_that_routine_by_id_even_when_another_name_matches_the_text(tmp_path, monkeypatch):
    """保护 #522：A「收盘检查」与 B「跑一下收盘检查」并存时，设置里的运行（消息带 routineId）和
    POST /api/routines/{A}/run 都只跑 A；排队时 A 被删则明说已不在，不转投 B、不当普通聊天。"""
    app, server = await _native(tmp_path, monkeypatch, gateway=False)
    try:
        client = await _native_client(app)
        a = await _saved(client, "收盘检查", [{"kind": "read", "text": "看账户"}])
        b = await _saved(client, "跑一下收盘检查", [{"kind": "read", "text": "看账户"}, {"kind": "read", "text": "再看账户"}])
        session_id = await _session(client)

        def last_run() -> dict[str, str | None]:
            return {item["id"]: item["lastRunAt"] for item in app.state.store.list_routines(USER)}

        # 设置里的运行: the chat shows “跑一下收盘检查”, the run is A (whose name the text does not match first).
        assert (await _run(app, client, session_id, "跑一下收盘检查", a["id"]))["status"] == "completed"
        card = _events(app, session_id, "card.routine_run")[-1]
        assert card["routineId"] == a["id"] and len(card["steps"]) == 1
        assert last_run()[a["id"]] is not None and last_run()[b["id"]] is None
        user = [m for m in _events(app, session_id, "message.completed") if m.get("role") == "user"][-1]
        assert user["text"] == "跑一下收盘检查"
        # POST /api/routines/{A}/run: the same — A, never B.
        response = await client.post(f"/api/routines/{a['id']}/run", json={"sessionId": session_id})
        assert response.status_code == 202, response.text
        assert (await _wait(app, response.json()["runId"]))["status"] == "completed"
        assert _events(app, session_id, "card.routine_run")[-1]["routineId"] == a["id"]
        assert last_run()[b["id"]] is None
        # Typing the sentence keeps the name rule: the whole sentence is B's name, so B runs.
        assert (await _run(app, client, session_id, "跑一下收盘检查"))["status"] == "completed"
        assert _events(app, session_id, "card.routine_run")[-1]["routineId"] == b["id"]
        # A deleted while its run waited: a plain answer, no run of B, no model turn.
        runs, asked = len(_events(app, session_id, "card.routine_run")), len(server.offered)
        assert (await client.delete(f"/api/routines/{a['id']}")).status_code == 200
        assert (await _run(app, client, session_id, "跑一下收盘检查", a["id"]))["status"] == "completed"
        assert len(_events(app, session_id, "card.routine_run")) == runs and len(server.offered) == asked
        answer = [m for m in _events(app, session_id, "message.completed") if m.get("role") == "assistant"][-1]
        assert answer["text"] == GONE_LINE
        await client.aclose()
    finally:
        await _close(app)

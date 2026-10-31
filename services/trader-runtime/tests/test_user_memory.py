"""私人记忆 3a (2026-09-29): three layers per user, the audit trail with 撤销, the
原话核对 for chat-written memories, the authorization interception, the resident
turn block and the voice profile — plus the remember / forget / recall_memory tools
through the same catalog the UI uses."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from test_ai_annotations import HEADERS, with_tools
from trader_runtime import user_memory
from trader_runtime.main import create_app
from trader_runtime.store import RuntimeStore
from trader_runtime.user_memory import TURN_BUDGET, VOICE_BUDGET

USER = "local-user"


def _app(tmp_path: Path, name="memory.sqlite3"):
    return create_app(tmp_path / name, runtime_state_root=tmp_path, agent_mode="fake",
                      local_token="annotation-test-token")


async def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS)


async def _say(app, text: str) -> None:
    """One stored user turn: what the via=chat 原话核对 compares a quote against."""
    session = app.state.broker.create_session("local", "fake", USER)
    await app.state.broker.publish(session.session_id, kind="message", name="message.completed",
                                   payload={"turnId": "t-user", "role": "user", "text": text})


async def _add(client: AsyncClient, layer="profile", text="我只做螺纹和铁矿", **extra) -> httpx.Response:
    return await client.post("/api/memory", json={"layer": layer, "text": text, **extra})


async def test_add_update_invalidate_undo_and_the_change_trail(tmp_path):
    app = _app(tmp_path)
    async with app.router.lifespan_context(app), await _client(app) as client:
        created = await _add(client, text="我只做螺纹和铁矿")
        assert created.status_code == 201, created.text
        item = created.json()
        assert item["layerLabel"] == "画像" and item["status"] == "active" and item["source"] == "user"
        assert item["evidence"]["via"] == "settings" and item["evidence"]["quote"] is None
        memory_id = item["id"]

        updated = await client.put(f"/api/memory/{memory_id}", json={"text": "我只做螺纹"})
        assert updated.status_code == 200 and updated.json()["text"] == "我只做螺纹"

        deleted = await client.delete(f"/api/memory/{memory_id}")
        assert deleted.json() == {"id": memory_id, "status": "invalid"}
        listed = (await client.get("/api/memory", params={"includeInvalid": True})).json()
        assert listed["enabled"] is True and len(listed["items"]) == 1
        assert listed["items"][0]["status"] == "invalid" and listed["items"][0]["invalidReason"] == "用户删除"
        assert (await client.get("/api/memory")).json()["items"] == []  # invalid is hidden by default
        # The audit trail: add → update → invalidate, newest first.
        changes = listed["changes"]
        assert [change["op"] for change in changes] == ["invalidate", "update", "add"]
        assert changes[0]["before"]["status"] == "active" and changes[0]["after"]["status"] == "invalid"

        # 撤销 invalidate → active again; 撤销 update → back to the previous text; 撤销 add → invalid.
        restored = await client.post(f"/api/memory/changes/{changes[0]['id']}/undo")
        assert restored.status_code == 200 and restored.json()["status"] == "active"
        # 撤销那条“恢复”本身 → 再失效（reason 撤销恢复）；这条失效也能再撤回恢复。
        restore_change = (await client.get("/api/memory", params={"includeInvalid": True})).json()["changes"][0]
        assert restore_change["op"] == "restore"
        re_invalid = await client.post(f"/api/memory/changes/{restore_change['id']}/undo")
        assert re_invalid.status_code == 200 and re_invalid.json()["status"] == "invalid"
        assert re_invalid.json()["invalidReason"] == "撤销恢复"
        re_invalid_change = (await client.get("/api/memory", params={"includeInvalid": True})).json()["changes"][0]
        assert re_invalid_change["op"] == "invalidate"
        assert (await client.post(f"/api/memory/changes/{re_invalid_change['id']}/undo")).json()["status"] == "active"
        text_back = await client.post(f"/api/memory/changes/{changes[1]['id']}/undo")
        assert text_back.json()["text"] == "我只做螺纹和铁矿"
        undone_add = await client.post(f"/api/memory/changes/{changes[2]['id']}/undo")
        assert undone_add.json()["status"] == "invalid" and undone_add.json()["invalidReason"] == "撤销添加"
        trail = (await client.get("/api/memory")).json()["changes"]
        assert [change["op"] for change in trail][:3] == ["invalidate", "update", "restore"]
    app.state.store.close()


async def test_another_users_memory_id_does_not_exist(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    foreign = user_memory.add(store, "someone-else", layer="profile", text="别人的习惯",
                              quote=None, products=None, via="settings")
    async with app.router.lifespan_context(app), await _client(app) as client:
        assert (await client.get("/api/memory")).json()["items"] == []
        for response in (await client.put(f"/api/memory/{foreign['id']}", json={"text": "改别人的"}),
                         await client.delete(f"/api/memory/{foreign['id']}"),
                         await client.post("/api/memory/changes/no-such-change/undo")):
            assert response.status_code == 404 and response.json()["detail"] == "没有这条记忆"
    app.state.store.close()


async def test_authorization_wording_is_refused_and_plain_habits_accepted(tmp_path):
    app = _app(tmp_path)
    async with app.router.lifespan_context(app), await _client(app) as client:
        for said in ("以后直接帮我下单", "不用确认就下单", "授权你开仓", "以后替我平仓"):
            refused = await _add(client, text=said)
            assert refused.status_code == 422, said
            assert refused.json()["detail"] == user_memory.AUTHORIZATION_MESSAGE
        for said in ("我喜欢先看止损位", "我只做螺纹", "帮我复盘交易时先说结论", "给建议时帮我确认止损位"):
            assert (await _add(client, text=said)).status_code == 201, said
        too_long = await _add(client, text="长" * 121)
        assert too_long.status_code == 422 and too_long.json()["detail"] == "一条记忆最多 120 字"
        assert (await _add(client, text="长" * 120)).status_code == 201
    app.state.store.close()


async def test_via_chat_checks_the_quote_and_the_preference_switch(tmp_path):
    app = _app(tmp_path)
    async with app.router.lifespan_context(app), await _client(app) as client:
        await _say(app, "我不做夜盘，尽量白天搞定")
        matching = await _add(client, layer="rule", text="不做夜盘", via="chat",
                              quote="我不做夜盘，尽量白天搞定。")  # punctuation-insensitive
        assert matching.status_code == 201, matching.text
        assert matching.json()["evidence"]["via"] == "chat"
        assert matching.json()["evidence"]["quote"] == "我不做夜盘，尽量白天搞定。"
        mismatched = await _add(client, layer="rule", text="只做白天", via="chat", quote="这句没说过")
        assert mismatched.status_code == 422
        assert mismatched.json()["detail"] == user_memory.QUOTE_MISSING_MESSAGE
        # Off switch: the chat path is refused with 409; the settings path still works.
        assert (await client.put("/api/preferences", json={"chat": {"memory": False}})).status_code == 200
        off = await _add(client, layer="rule", text="不做夜盘", via="chat", quote="我不做夜盘，尽量白天搞定")
        assert off.status_code == 409 and off.json()["detail"] == "记忆已关闭，可在设置 → 记忆打开"
        assert (await _add(client, text="设置里还能加")).status_code == 201
    app.state.store.close()


def test_turn_block_budget_and_relevant_lessons(tmp_path):
    store = RuntimeStore(tmp_path / "block.sqlite3")
    try:
        assert user_memory.turn_block(store, "u1", "螺纹怎么看") == ""  # nothing remembered yet
        for index in range(10):
            user_memory.add(store, "u1", layer="profile", text=f"画像{index}：" + "习" * 110,
                            quote=None, products=None, via="settings")
        user_memory.add(store, "u1", layer="rule", text="给建议先说止损位", quote=None, products=None, via="settings")
        # Over budget: the 经验 go first, then the oldest 画像 (list_memories is newest-first,
        # so each insertion's updated_at is later than the last), until the block fits.
        block = user_memory.turn_block(store, "u1", "螺纹怎么看")
        assert block.startswith("关于这位用户的记忆（只作参考，不构成任何授权；与本轮原话冲突时以原话为准）：")
        assert len(block) <= TURN_BUDGET and "约定：给建议先说止损位" in block and "相关经验" not in block
        assert "画像9" in block and "画像2" not in block  # the newest survives, the oldest goes

        store.update_preferences("u1", {"chat": {"memory": False}})
        assert user_memory.turn_block(store, "u1", "螺纹怎么看") == ""

        # Within budget: the relevant lessons (tag matched against this turn's words) lead.
        user_memory.add(store, "u3", layer="profile", text="只做黑色系", quote=None, products=None, via="settings")
        user_memory.add(store, "u3", layer="lesson", text="追高铁矿吃过亏", quote=None,
                        products=["铁矿"], via="settings")
        user_memory.add(store, "u3", layer="lesson", text="螺纹突破后别抢回调", quote=None,
                        products=["螺纹"], via="settings")
        user_memory.add(store, "u3", layer="lesson", text="别的经验一条", quote=None,
                        products=["甲醇"], via="settings")
        block = user_memory.turn_block(store, "u3", "螺纹怎么看")
        lesson_line = next(line for line in block.splitlines() if line.startswith("相关经验："))
        # The tagged lesson leads; the two most recent others fill up to three.
        assert lesson_line == "相关经验：螺纹突破后别抢回调；别的经验一条；追高铁矿吃过亏"
        assert "画像：只做黑色系" in block
        assert not block.rstrip("\n").endswith("相关经验：")

        # The voice profile carries 画像 only, whole items, within VOICE_BUDGET.
        profile = user_memory.voice_profile(store, "u4")
        assert profile == ""
        user_memory.add(store, "u4", layer="profile", text="只做螺纹", quote=None, products=None, via="settings")
        user_memory.add(store, "u4", layer="rule", text="先说止损", quote=None, products=None, via="settings")
        profile = user_memory.voice_profile(store, "u4")
        assert profile == "关于用户：只做螺纹" and len(profile) <= VOICE_BUDGET
    finally:
        store.close()


async def test_the_remember_forget_recall_tools_go_through_the_catalog(tmp_path):
    app = _app(tmp_path)
    with_tools(app)
    async with app.router.lifespan_context(app), await _client(app) as client:
        await _say(app, "记住我只做螺纹，别的先不碰")
        remembered = await client.post("/api/agent/call", json={"operation": "tool:remember", "body": {
            "layer": "profile", "text": "只做螺纹", "quote": "记住我只做螺纹，别的先不碰"}})
        payload = remembered.json()
        assert payload["ok"], payload
        memory_id = payload["data"]["data"]["id"]

        recalled = await client.post("/api/agent/call", json={"operation": "tool:recall_memory", "body": {
            "layer": "rule"}})
        assert recalled.json()["ok"] and recalled.json()["data"]["data"]["items"] == []

        listed = await client.post("/api/agent/call", json={"operation": "tool:recall_memory", "body": {}})
        assert [item["id"] for item in listed.json()["data"]["data"]["items"]] == [memory_id]

        forgotten = await client.post("/api/agent/call", json={"operation": "tool:forget", "body": {
            "memoryId": memory_id, "reason": "记错了"}})
        assert forgotten.json()["ok"]
        remaining = (await client.get("/api/memory")).json()
        assert remaining["items"] == []
        assert [change["op"] for change in remaining["changes"]] == ["invalidate", "add"]
        # The forget tool deletes via=chat, so the trail shows who did it: AI 删除, not 你删除.
        assert remaining["changes"][1]["actor"] == "ai" and remaining["changes"][0]["actor"] == "ai"

        # The tool surfaces the route's own refusals (原话对不上 → 422 detail as the warning).
        mismatched = await client.post("/api/agent/call", json={"operation": "tool:remember", "body": {
            "layer": "profile", "text": "只做铁矿", "quote": "这句没说过"}})
        assert not mismatched.json()["ok"]
        assert user_memory.QUOTE_MISSING_MESSAGE in mismatched.json()["data"]["warning"]
    app.state.store.close()


@pytest.mark.parametrize("said", ["以后直接帮我下单", "不用确认就下单", "授权你开仓", "以后替我平仓"])
def test_the_authorization_pattern(said):
    assert user_memory.AUTHORIZATION_RE.search(said)


@pytest.mark.parametrize("said", ["我喜欢先看止损位", "我只做螺纹", "帮我复盘交易时先说结论", "给建议时帮我确认止损位"])
def test_plain_habits_do_not_match(said):
    assert not user_memory.AUTHORIZATION_RE.search(said)


async def test_an_ai_call_through_the_catalog_writes_memory_as_chat_origin(tmp_path):
    """#524-1: a request forwarded by /api/agent/call is not the user's own settings page — it writes
    with chat rules whatever via the body claims (quote check + 记忆已关闭 switch), and the audit
    says ai for add/update/delete/undo. The user's own requests keep their via and their actor."""
    app = _app(tmp_path, "ai-origin.sqlite3")
    with_tools(app)
    async with app.router.lifespan_context(app), await _client(app) as client:
        await _say(app, "我只做螺纹和铁矿")

        async def ai(operation: str, body=None, path=None) -> dict:
            payload = {"operation": operation}
            if body is not None:
                payload["body"] = body
            if path is not None:
                payload["path"] = path
            return (await client.post("/api/agent/call", json=payload)).json()

        # body 里冒充 via=settings 也没用：非人工请求一律按对话来源处理 → 没带原话，拒绝。
        bare = await ai("POST /api/memory", {"layer": "profile", "text": "只做螺纹", "via": "settings"})
        assert not bare["ok"] and bare["status"] == 422 and "原话" in bare["data"]["detail"]
        mismatched = await ai("POST /api/memory", {"layer": "profile", "text": "只做螺纹", "quote": "这句没说过"})
        assert not mismatched["ok"] and "原话" in mismatched["data"]["detail"]
        quoted = await ai("POST /api/memory", {"layer": "profile", "text": "只做螺纹", "quote": "我只做螺纹和铁矿"})
        assert quoted["ok"], quoted
        item = quoted["data"]
        assert item["evidence"]["via"] == "chat" and item["evidence"]["quote"] == "我只做螺纹和铁矿"
        memory_id = item["id"]

        # 设置路径绕不开、对话路径关得住：AI 写记忆同样受“记忆已关闭”约束。
        assert (await client.put("/api/preferences", json={"chat": {"memory": False}})).status_code == 200
        off = await ai("POST /api/memory", {"layer": "profile", "text": "还想再记一条", "quote": "我只做螺纹和铁矿"})
        assert not off["ok"] and "记忆已关闭" in off["data"]["detail"]
        assert (await client.put("/api/preferences", json={"chat": {"memory": True}})).status_code == 200

        # AI 改记忆走同一个门（#524-A）：没带原话 422；原话核得上才改，依据换成这句原话。
        unquoted = await ai("PUT /api/memory/{memory_id}", {"text": "只做螺纹，别的先不碰"}, {"memory_id": memory_id})
        assert not unquoted["ok"] and unquoted["status"] == 422 and "原话" in unquoted["data"]["detail"]
        edited = await ai("PUT /api/memory/{memory_id}", {"text": "只做螺纹，别的先不碰", "quote": "我只做螺纹和铁矿"},
                          {"memory_id": memory_id})
        assert edited["ok"] and edited["data"]["text"] == "只做螺纹，别的先不碰"
        assert edited["data"]["evidence"]["via"] == "chat" and edited["data"]["evidence"]["quote"] == "我只做螺纹和铁矿"
        deleted = await ai("DELETE /api/memory/{memory_id}", None, {"memory_id": memory_id})
        assert deleted["ok"] and deleted["data"]["status"] == "invalid"
        trail = (await client.get("/api/memory", params={"includeInvalid": True})).json()["changes"]
        assert [change["op"] for change in trail] == ["invalidate", "update", "add"]
        assert {change["actor"] for change in trail} == {"ai"}  # 不是用户手工删改
        restored = await ai("POST /api/memory/changes/{change_id}/undo", None, {"change_id": trail[0]["id"]})
        assert restored["ok"] and restored["data"]["status"] == "active"

        # 用户自己的设置页路径照旧：via=settings、审计 actor=user。
        mine = await _add(client, text="设置里加的")
        assert mine.status_code == 201 and mine.json()["evidence"]["via"] == "settings"
        last = (await client.get("/api/memory")).json()["changes"][0]
        assert last["op"] == "add" and last["actor"] == "user"

        # 记忆关闭后：AI 新增、修改、撤销（会把记忆带回来）都 409 且零写入；AI“忘掉”照常可以；
        # 人在设置页仍可新增、修改、删除、撤销（关闭只管 AI 记，不锁人工管理）。
        assert (await client.put("/api/preferences", json={"chat": {"memory": False}})).status_code == 200
        audits = len((await client.get("/api/memory", params={"includeInvalid": True})).json()["changes"])
        target = mine.json()["id"]
        blocked = [
            await ai("POST /api/memory", {"layer": "profile", "text": "关了还想记", "quote": "我只做螺纹和铁矿"}),
            await ai("PUT /api/memory/{memory_id}", {"text": "关了还想改", "quote": "我只做螺纹和铁矿"},
                     {"memory_id": target}),
            await ai("POST /api/memory/changes/{change_id}/undo", None, {"change_id": last["id"]}),
        ]
        assert [(item["ok"], item["status"]) for item in blocked] == [(False, 409)] * 3
        after = (await client.get("/api/memory", params={"includeInvalid": True})).json()
        assert len(after["changes"]) == audits
        assert next(item for item in after["items"] if item["id"] == target)["text"] == "设置里加的"
        human_edit = await client.put(f"/api/memory/{target}", json={"text": "人工改的"})
        assert human_edit.status_code == 200 and human_edit.json()["text"] == "人工改的"
        human_add = await _add(client, text="关了也能手动加")
        assert human_add.status_code == 201
        forgot = await ai("DELETE /api/memory/{memory_id}", None, {"memory_id": target})
        assert forgot["ok"] and forgot["data"]["status"] == "invalid"
        trail = (await client.get("/api/memory", params={"includeInvalid": True})).json()["changes"]
        assert [(change["op"], change["actor"]) for change in trail[:3]] == [
            ("invalidate", "ai"), ("add", "user"), ("update", "user")]
        assert (await client.post(f"/api/memory/changes/{trail[0]['id']}/undo")).status_code == 200
    app.state.store.close()


async def test_user_texts_are_filtered_in_sql_so_assistant_chatter_cannot_push_them_out(tmp_path):
    """#524-8: the 原话核对 and the consolidation window filter role=user in SQL; a hundred-plus
    assistant replies after the user's words must not push them out of the LIMIT."""
    app = _app(tmp_path, "sql-filter.sqlite3")
    store = app.state.store
    async with app.router.lifespan_context(app):
        session = app.state.broker.create_session("local", "fake", USER)
        await app.state.broker.publish(session.session_id, kind="message", name="message.completed",
                                       payload={"turnId": "t-user", "role": "user", "text": "我只做螺纹和铁矿"})
        for index in range(201):
            await app.state.broker.publish(session.session_id, kind="message", name="message.completed",
                                           payload={"turnId": f"a{index}", "role": "assistant", "text": "回复"})
        assert store.recent_user_texts(USER) == ["我只做螺纹和铁矿"]
        start = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        end = datetime.now(UTC).isoformat()
        assert [item["text"] for item in store.user_texts_between(USER, start, end)] == ["我只做螺纹和铁矿"]
    app.state.store.close()


def test_undo_restores_the_evidence_and_tags_too(tmp_path):
    """#524-6: 撤销一条 update 改回的不只是文字——依据（evidence）和品种标签一起放回去。"""
    store = RuntimeStore(tmp_path / "undo-full.sqlite3")
    try:
        memory = user_memory.add(store, USER, layer="lesson", text="追高铁矿吃过亏", quote=None,
                                 products=["铁矿"], via="settings")
        original = {"text": memory["text"], "evidence": memory["evidence"], "tags": memory["tags"]}
        edited = user_memory.update(store, USER, memory["id"], "改成自己的话", actor="user")
        assert edited["text"] == "改成自己的话" and edited["tags"] == ["铁矿"]
        change = store.list_memory_changes(USER)[0]   # 最新在前：这条是刚才的 update
        undone = user_memory.undo(store, USER, change["id"])
        assert undone["text"] == original["text"]
        assert undone["evidence"] == original["evidence"] and undone["tags"] == original["tags"]

        # 统计条目同理：用户改出的 userEdited 撤销后回到整理写下的依据与标签。
        from trader_runtime.memory_consolidation import apply_stats
        apply_stats(store, USER, [{"key": "stat:holding", "layer": "lesson", "n": 6, "wins": 5,
                                   "roundIds": ["t1"], "tags": ["螺纹"],
                                   "text": "多为日内短线（持仓中位 30 分钟）"}])
        item = next(memory for memory in store.list_memories(USER) if memory["source"] == "stats")
        user_memory.update(store, USER, item["id"], "日内为主", actor="user")
        edit_change = next(change for change in store.list_memory_changes(USER)
                           if change["op"] == "update" and change["memoryId"] == item["id"])
        back = user_memory.undo(store, USER, edit_change["id"])
        assert back["text"] == "多为日内短线（持仓中位 30 分钟）"
        assert back["evidence"]["statKey"] == "stat:holding" and "userEdited" not in back["evidence"]
        assert back["tags"] == ["螺纹"]
    finally:
        store.close()


def test_turn_block_ranks_lessons_by_shared_words_not_only_tags(tmp_path):
    """BM25 over characters and character pairs (text_relevance, from Vibe-Trading): an untagged lesson about
    this turn's subject leads even when it is the oldest; one shared filler character (的, 看) is no match."""
    store = RuntimeStore(tmp_path / "rank.sqlite3")
    try:
        user_memory.add(store, "u6", layer="lesson", text="夜盘开盘半小时别追单", quote=None, products=None, via="settings")
        for text in ("甲醇的仓位别太重", "多看日线再下手", "铁矿止损要设宽一点"):
            user_memory.add(store, "u6", layer="lesson", text=text, quote=None, products=None, via="settings")
        block = user_memory.turn_block(store, "u6", "今晚夜盘想追一下")
        lesson_line = next(line for line in block.splitlines() if line.startswith("相关经验："))
        assert lesson_line == "相关经验：夜盘开盘半小时别追单；铁矿止损要设宽一点；多看日线再下手"
    finally:
        store.close()


def test_text_relevance_tokens_keep_one_character_products():
    from trader_runtime import text_relevance
    assert text_relevance.tokens("螺纹钢") == ["螺", "纹", "钢", "螺纹", "纹钢"]
    assert "铜" in text_relevance.tokens("铜怎么看") and "看" not in text_relevance.tokens("铜怎么看")
    assert text_relevance.tokens("RB2701 日线") == ["rb2701", "日", "线", "日线"]
    assert text_relevance.rank("铜", ["沪铜别隔夜", "白银看日线"])[1] == 0.0


def test_turn_block_trims_rules_when_they_alone_overflow_the_budget(tmp_path):
    """#524-10: 约定也进 600 字预算——画像裁完还不够时，最旧的约定跟着让位。"""
    store = RuntimeStore(tmp_path / "rules-budget.sqlite3")
    try:
        for index in range(8):
            user_memory.add(store, "u5", layer="rule", text=f"约定{index}：" + "则" * 110,
                            quote=None, products=None, via="settings")
        block = user_memory.turn_block(store, "u5", "螺纹怎么看")
        assert len(block) <= TURN_BUDGET
        assert "约定7" in block and "约定0" not in block   # 最新的留下，最旧的让位
        assert "画像：" not in block                        # 没有画像时也轮不到全量约定
    finally:
        store.close()

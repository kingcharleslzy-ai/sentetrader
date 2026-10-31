"""Late consolidation and undo must preserve the person's newer memory decision."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from test_user_memory import USER, _app, _client
from trader_runtime.memory_consolidation import MemoryOp, extract_from_words
from trader_runtime.store import RuntimeStore


@pytest.mark.parametrize("decision", ["edit", "aba", "delete_restore"])
@pytest.mark.parametrize("operation", ["update", "invalidate"])
async def test_late_extraction_keeps_the_newer_http_decision(tmp_path, monkeypatch, decision, operation):
    # A frozen timestamp makes A -> B -> A distinguishable only by a real revision.
    monkeypatch.setattr("trader_runtime.store.memory.utc_now", lambda: "2026-10-11T08:00:00+00:00")
    app = _app(tmp_path)
    store = app.state.store
    started, release = asyncio.Event(), asyncio.Event()

    class WaitingModel:
        async def complete_structured(self, **_):
            started.set()
            await release.wait()
            return SimpleNamespace(tokens=1, value=SimpleNamespace(ops=[MemoryOp(
                op=operation, memoryId=memory_id, layer="profile", text="模型旧轮黄金" if operation == "update" else None,
                quote="我只做螺纹")]))

    try:
        async with await _client(app) as client:
            original = (await client.post("/api/memory", json={"layer": "profile", "text": "旧偏好螺纹"})).json()
            memory_id = original["id"]
            task = asyncio.create_task(extract_from_words(WaitingModel(), store, USER, ["我只做螺纹"], []))
            await started.wait()
            if decision == "delete_restore":
                assert (await client.delete(f"/api/memory/{memory_id}")).status_code == 200
                change_id = store.connection.execute("SELECT id FROM user_memory_changes ORDER BY rowid DESC LIMIT 1").fetchone()[0]
                change = next(row for row in (await client.get("/api/memory", params={"includeInvalid": True})).json()["changes"] if row["id"] == change_id)
                assert (await client.post(f"/api/memory/changes/{change['id']}/undo")).status_code == 200
            else:
                assert (await client.put(f"/api/memory/{memory_id}", json={"text": "用户最新纠正铁矿"})).status_code == 200
                if decision == "aba":
                    assert (await client.put(f"/api/memory/{memory_id}", json={"text": original["text"]})).status_code == 200
            expected = store.get_memory(memory_id, user_id=USER)
            audit_count = store.connection.execute("SELECT COUNT(*) FROM user_memory_changes").fetchone()[0]
            release.set()
            result = await task
            assert result["applied"] == 0
            assert result["rejected"] == {"目标记忆已更新": 1}
            assert store.get_memory(memory_id, user_id=USER) == expected
            assert store.connection.execute("SELECT COUNT(*) FROM user_memory_changes").fetchone()[0] == audit_count
        cold = RuntimeStore(tmp_path / "memory.sqlite3")
        try:
            assert cold.get_memory(memory_id, user_id=USER) == expected
        finally:
            cold.close()
    finally:
        store.close()


async def test_replayed_http_undo_cannot_replace_a_later_edit(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    try:
        async with await _client(app) as client:
            memory_id = (await client.post("/api/memory", json={"layer": "profile", "text": "原文字"})).json()["id"]
            await client.put(f"/api/memory/{memory_id}", json={"text": "第二版"})
            change_id = (await client.get("/api/memory")).json()["changes"][0]["id"]
            assert (await client.post(f"/api/memory/changes/{change_id}/undo")).status_code == 200
            await client.put(f"/api/memory/{memory_id}", json={"text": "用户后来的决定"})
            audit_count = store.connection.execute("SELECT COUNT(*) FROM user_memory_changes").fetchone()[0]
            response = await client.post(f"/api/memory/changes/{change_id}/undo")
            assert response.status_code == 409
            assert (await client.get("/api/memory")).json()["items"][0]["text"] == "用户后来的决定"
            assert store.connection.execute("SELECT COUNT(*) FROM user_memory_changes").fetchone()[0] == audit_count
    finally:
        store.close()


async def test_same_model_batch_advances_only_its_visible_target(tmp_path, monkeypatch):
    from trader_runtime import user_memory
    from trader_runtime.memory_consolidation import MemoryOps
    store = RuntimeStore(tmp_path / "batch.sqlite3")
    try:
        first = user_memory.add(store, USER, layer="profile", text="模型可见", quote=None, products=None, via="settings")
        hidden = user_memory.add(store, USER, layer="profile", text="未进材料", quote=None, products=None, via="settings")
        # Existing prompt limit: show exactly the newest one, not a second authorizing list.
        monkeypatch.setattr("trader_runtime.memory_consolidation.MAX_CONTEXT_MEMORIES", 1)
        visible = store.list_memories(USER)[0]
        unseen = first if visible["id"] == hidden["id"] else hidden
        class BatchModel:
            async def complete_structured(self, **_):
                return SimpleNamespace(tokens=1, value=MemoryOps(ops=[
                    MemoryOp(op="update", memoryId=visible["id"], layer="profile", text="先更新", quote="我只做螺纹"),
                    MemoryOp(op="update", memoryId=visible["id"], layer="profile", text="再次更新", quote="我只做螺纹"),
                    MemoryOp(op="update", memoryId=unseen["id"], layer="profile", text="不能借ID更新", quote="我只做螺纹"),
                    MemoryOp(op="invalidate", memoryId=visible["id"], layer="profile", quote="我只做螺纹"),
                ]))
        result = await extract_from_words(BatchModel(), store, USER, ["我只做螺纹"], [])
        assert result["applied"] == 3 and result["rejected"] == {"目标记忆未提供给本轮提炼": 1}
        final = store.get_memory(visible["id"], user_id=USER)
        assert final["text"] == "再次更新" and final["status"] == "invalid"
        assert store.get_memory(unseen["id"], user_id=USER) == unseen
    finally:
        store.close()


def test_writes_without_actor_and_outer_transaction_still_advance_versions(tmp_path):
    from trader_runtime.store.memory import MemoryConflict
    store = RuntimeStore(tmp_path / "outer.sqlite3")
    other = RuntimeStore(tmp_path / "outer.sqlite3")
    try:
        original = store.insert_memory(USER, "profile", "原文", "user", {}, [])
        with pytest.raises(RuntimeError, match="abort outer"):
            with store.transaction() as connection:
                edited = store.update_memory_text_and_evidence(original["id"], user_id=USER, text="事务内更改",
                                                                evidence={}, actor="user", connection=connection)
                assert edited["revision"] != original["revision"]
                assert other.get_memory(original["id"], user_id=USER) == original  # no hidden commit
                raise RuntimeError("abort outer")
        assert other.get_memory(original["id"], user_id=USER) == original
        assert store.list_memory_changes(USER) == []
        modified = store.update_memory_text(original["id"], user_id=USER, text="无审计调用仍有版本")
        revisions = {original["revision"], modified["revision"]}
        modified = store.update_memory_text_and_evidence(original["id"], user_id=USER, text=modified["text"], evidence={"n": 2})
        revisions.add(modified["revision"])
        modified = store.set_memory_status(original["id"], user_id=USER, status="invalid", reason="无审计失效")
        revisions.add(modified["revision"])
        modified = store.set_memory_status(original["id"], user_id=USER, status="active", reason=None)
        revisions.add(modified["revision"])
        assert len(revisions) == 5
        with pytest.raises(MemoryConflict):
            store.set_memory_status(original["id"], user_id=USER, status="invalid", reason="旧候选",
                                    actor="consolidation", expected_revision=original["revision"])
        assert other.get_memory(original["id"], user_id=USER) == modified
        with store.transaction() as connection:
            inserted = store.insert_memory(USER, "lesson", "外层新增", "user", {}, [], actor="user", connection=connection)
            invalid = store.set_memory_status(inserted["id"], user_id=USER, status="invalid", reason="外层失效",
                                               actor="user", connection=connection)
            assert invalid["revision"] != inserted["revision"]
            assert other.get_memory(inserted["id"], user_id=USER) is None
        assert other.get_memory(inserted["id"], user_id=USER) == invalid
    finally:
        other.close()
        store.close()


def _make_legacy(store):
    import json
    # Synthetic pre-upgrade facts and audit snapshots; production migration does
    # not rewrite them or infer the owner's past decisions.
    with store.connection:
        store.connection.execute("UPDATE user_memories SET revision='', state_revision=''")
        for row in store.connection.execute("SELECT id,before_json,after_json FROM user_memory_changes").fetchall():
            values = []
            for field in ("before_json", "after_json"):
                value = json.loads(row[field]) if row[field] else None
                if value:
                    value.pop("revision", None)
                    value.pop("stateRevision", None)
                values.append(json.dumps(value) if value is not None else None)
            store.connection.execute("UPDATE user_memory_changes SET before_json=?, after_json=? WHERE id=?", (*values, row["id"]))


@pytest.mark.parametrize("legacy", [False, True])
def test_continuous_undo_restore_and_retry_survive_cold_reopen(tmp_path, legacy):
    from trader_runtime import user_memory
    store = RuntimeStore(tmp_path / "undo.sqlite3")
    try:
        item = user_memory.add(store, USER, layer="lesson", text="原经验", quote=None, products=["螺纹"], via="settings")
        add = store.list_memory_changes(USER)[0]
        user_memory.update(store, USER, item["id"], "修正经验")
        edit = store.list_memory_changes(USER)[0]
        user_memory.invalidate(store, USER, item["id"], "用户删除")
        deleted = store.list_memory_changes(USER)[0]
        if legacy:
            _make_legacy(store)
            with store.connection:
                store.connection.execute("ALTER TABLE user_memories DROP COLUMN revision")
                store.connection.execute("ALTER TABLE user_memories DROP COLUMN state_revision")
                store.connection.execute("ALTER TABLE user_memory_changes DROP COLUMN undo_of")
            old_fact = tuple(store.connection.execute("SELECT * FROM user_memories").fetchone())
            old_audit = [tuple(row) for row in store.connection.execute("SELECT * FROM user_memory_changes ORDER BY rowid")]
            store.close()
            store = RuntimeStore(tmp_path / "undo.sqlite3")
            assert tuple(store.connection.execute("SELECT * FROM user_memories").fetchone())[:-2] == old_fact
            assert [tuple(row)[:-1] for row in store.connection.execute("SELECT * FROM user_memory_changes ORDER BY rowid")] == old_audit
        restored = user_memory.undo(store, USER, deleted["id"])
        assert restored["status"] == "active" and restored["text"] == "修正经验"
        restore = store.list_memory_changes(USER)[0]
        assert user_memory.undo(store, USER, restore["id"])["status"] == "invalid"
        invalidate = store.list_memory_changes(USER)[0]
        assert user_memory.undo(store, USER, invalidate["id"])["status"] == "active"
        original = user_memory.undo(store, USER, edit["id"])
        assert original["text"] == "原经验" and original["tags"] == ["螺纹"]
        audit_count = len(store.list_memory_changes(USER))
        store.close()
        store = RuntimeStore(tmp_path / "undo.sqlite3")
        assert user_memory.undo(store, USER, edit["id"]) == original
        assert len(store.list_memory_changes(USER)) == audit_count
        removed = user_memory.undo(store, USER, add["id"])
        assert removed["status"] == "invalid" and removed["invalidReason"] == "撤销添加"
    finally:
        store.close()


@pytest.mark.parametrize("broken", [False, True])
def test_legacy_undo_requires_the_audit_cause_not_equal_time_or_text(tmp_path, monkeypatch, broken):
    from trader_runtime import user_memory
    from trader_runtime.store.memory import MemoryConflict
    monkeypatch.setattr("trader_runtime.store.memory.utc_now", lambda: "2026-10-11T08:00:00+00:00")
    store = RuntimeStore(tmp_path / "legacy.sqlite3")
    try:
        item = user_memory.add(store, USER, layer="profile", text="A", quote=None, products=None, via="settings")
        user_memory.update(store, USER, item["id"], "B")
        edit_b = store.list_memory_changes(USER)[0]
        user_memory.update(store, USER, item["id"], "A")
        user_memory.update(store, USER, item["id"], "B")
        latest = store.list_memory_changes(USER)[0]
        _make_legacy(store)
        if broken:
            with store.connection:
                store.connection.execute("UPDATE user_memories SET evidence_json='{}', text='未审计更改'")
        before = store.get_memory(item["id"], user_id=USER)
        audit_count = len(store.list_memory_changes(USER))
        with pytest.raises(MemoryConflict):
            user_memory.undo(store, USER, latest["id"] if broken else edit_b["id"])
        assert store.get_memory(item["id"], user_id=USER) == before
        assert len(store.list_memory_changes(USER)) == audit_count
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["update", "undo"])
def test_audit_failure_rolls_back_the_fact_version_and_undo_marker(tmp_path, operation):
    import sqlite3

    from trader_runtime import user_memory
    store = RuntimeStore(tmp_path / "fault.sqlite3")
    try:
        item = user_memory.add(store, USER, layer="profile", text="原文", quote=None, products=None, via="settings")
        user_memory.update(store, USER, item["id"], "新文")
        target = store.list_memory_changes(USER)[0]
        before = store.get_memory(item["id"], user_id=USER)
        audit_count = len(store.list_memory_changes(USER))
        with store.connection:
            store.connection.execute("CREATE TRIGGER fail_memory_audit BEFORE INSERT ON user_memory_changes BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")
        with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
            if operation == "update":
                store.update_memory_text_and_evidence(item["id"], user_id=USER, text="候选", evidence={},
                                                     actor="consolidation", expected_revision=before["revision"])
            else:
                user_memory.undo(store, USER, target["id"])
        assert store.get_memory(item["id"], user_id=USER) == before
        assert len(store.list_memory_changes(USER)) == audit_count
        assert store.connection.execute("SELECT count(*) FROM user_memory_changes WHERE undo_of IS NOT NULL").fetchone()[0] == 0
        with store.connection:
            store.connection.execute("DROP TRIGGER fail_memory_audit")
        if operation == "undo":
            assert user_memory.undo(store, USER, target["id"])["text"] == "原文"
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["update", "undo"])
def test_two_connections_share_the_memory_write_and_undo_fence(tmp_path, operation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from trader_runtime import user_memory
    from trader_runtime.store.memory import MemoryConflict
    first, second = RuntimeStore(tmp_path / "race.sqlite3"), RuntimeStore(tmp_path / "race.sqlite3")
    try:
        item = user_memory.add(first, USER, layer="profile", text="原文", quote=None, products=None, via="settings")
        edited = user_memory.update(first, USER, item["id"], "第二版")
        change_id = first.list_memory_changes(USER)[0]["id"]
        barrier = Barrier(2)
        def execute(store):
            barrier.wait(timeout=30)
            try:
                if operation == "undo":
                    return user_memory.undo(store, USER, change_id)
                return store.update_memory_text_and_evidence(item["id"], user_id=USER, text="候选版本", evidence={},
                                                            actor="consolidation", expected_revision=edited["revision"])
            except MemoryConflict:
                return None
        audit_count = len(first.list_memory_changes(USER))
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(execute, store) for store in (first, second)]
            results = [job.result(timeout=30) for job in jobs]
        assert len(first.list_memory_changes(USER)) == audit_count + 1
        if operation == "undo":
            assert results[0] == results[1] and results[0]["text"] == "原文"
        else:
            assert sum(result is not None for result in results) == 1
            assert first.get_memory(item["id"], user_id=USER)["text"] == "候选版本"
    finally:
        second.close()
        first.close()


def _old_runtime_edit(store, memory_id, text):
    """Pre-#760 MemoryStore's UPDATE + audit shape, with the new DB columns retained.

    Both old snapshots omit revision/stateRevision; old SQL never assigns the new
    columns. This is the rollback writer contract, not a new API bypass in product.
    """
    import json
    from uuid import uuid4
    keys = ("id", "userId", "layer", "text", "source", "evidence", "tags", "status",
            "createdAt", "updatedAt", "invalidAt", "invalidReason")
    with store.connection:
        before = {key: value for key, value in store.get_memory(memory_id, user_id=USER).items() if key in keys}
        # Keep the same timestamp too: only a causal audit identity can detect ABA.
        store.connection.execute("UPDATE user_memories SET text=?, updated_at=? WHERE id=? AND user_id=?", (text, before["updatedAt"], memory_id, USER))
        after = {**before, "text": text}
        change_id = uuid4().hex
        store.connection.execute("INSERT INTO user_memory_changes (id,user_id,memory_id,op,before_json,after_json,actor,at) VALUES (?,?,?,'update',?,?,'user',?)",
                                 (change_id, USER, memory_id, json.dumps(before), json.dumps(after), before["updatedAt"]))
        return change_id


@pytest.mark.parametrize("aba", [False, True])
async def test_old_runtime_writer_does_not_leave_a_valid_new_undo_token(tmp_path, aba):
    app = _app(tmp_path)
    store = app.state.store
    try:
        async with await _client(app) as client:
            item = (await client.post("/api/memory", json={"layer": "profile", "text": "原文"})).json()
            await client.put(f"/api/memory/{item['id']}", json={"text": "新版待撤"})
            change_id = store.list_memory_changes(USER)[0]["id"]
        _old_runtime_edit(store, item["id"], "旧Runtime期间后改")
        if aba:
            _old_runtime_edit(store, item["id"], "新版待撤")
        store.close()
        app = _app(tmp_path)
        store = app.state.store
        current = store.get_memory(item["id"], user_id=USER)
        count = len(store.list_memory_changes(USER))
        async with await _client(app) as client:
            response = await client.post(f"/api/memory/changes/{change_id}/undo")
            assert response.status_code == 409
        assert store.get_memory(item["id"], user_id=USER) == current
        assert len(store.list_memory_changes(USER)) == count
    finally:
        store.close()


async def test_old_runtime_audit_during_model_wait_invalidates_the_observed_version(tmp_path):
    from trader_runtime import user_memory
    from trader_runtime.memory_consolidation import MemoryOps
    store = RuntimeStore(tmp_path / "rollback-model.sqlite3")
    try:
        item = user_memory.add(store, USER, layer="profile", text="A", quote=None, products=None, via="settings")
        class RollbackModel:
            async def complete_structured(self, **_):
                _old_runtime_edit(store, item["id"], "B")
                _old_runtime_edit(store, item["id"], "A")
                return SimpleNamespace(tokens=1, value=MemoryOps(ops=[MemoryOp(
                    op="update", memoryId=item["id"], layer="profile", text="迟到模型", quote="我只做螺纹")]))
        result = await extract_from_words(RollbackModel(), store, USER, ["我只做螺纹"], [])
        assert result["applied"] == 0 and result["rejected"] == {"目标记忆已更新": 1}
        assert store.get_memory(item["id"], user_id=USER)["text"] == "A"
        assert len(store.list_memory_changes(USER)) == 3  # add + two old writes, no model side effect
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["undo_chain", "fresh_candidate"])
def test_old_runtime_current_fact_can_be_explicitly_undone_or_newly_observed(tmp_path, operation):
    from trader_runtime import user_memory
    store = RuntimeStore(tmp_path / "rollback-current.sqlite3")
    try:
        item = user_memory.add(store, USER, layer="profile", text="原文", quote=None, products=None, via="settings")
        user_memory.update(store, USER, item["id"], "新版待撤")
        new_change = store.list_memory_changes(USER)[0]["id"]
        old_change = _old_runtime_edit(store, item["id"], "旧版后改")
        store.close()
        store = RuntimeStore(tmp_path / "rollback-current.sqlite3")
        if operation == "undo_chain":
            assert user_memory.undo(store, USER, old_change)["text"] == "新版待撤"
            assert user_memory.undo(store, USER, new_change)["text"] == "原文"
        else:
            current = store.get_memory(item["id"], user_id=USER)
            result = store.update_memory_text_and_evidence(item["id"], user_id=USER, text="本轮已读后改",
                                                          evidence={}, actor="consolidation", expected_revision=current["revision"])
            assert result["text"] == "本轮已读后改"
            assert store.get_memory(item["id"], user_id=USER) == result
    finally:
        store.close()

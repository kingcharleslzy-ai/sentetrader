"""#524 采纳前复核：统计与原话提炼共用一个短写事务，在里面重读记忆开关、本次整理认领和
材料来源。材料删掉/改掉、开关关闭、整理取消或被接手以后，迟到的结果一条都不写；失败的
整理记 error、可以重试，失败那天的材料由下一次补上。全部是合成用户、合成原话与真实临时 SQLite。"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from test_memory_consolidation import CHINA, USER, FakeAdapter, RecordingAdapter, _app, _runs, _stat
from trader_runtime import user_memory
from trader_runtime.memory_consolidation import MemoryConsolidator, MemoryOp, extract_from_words
from trader_runtime.store import RuntimeStore

ACCOUNT = "tqkq-sim:default"


async def _said(app, text: str) -> str:
    """一条真实存下的用户原话（message.completed, role=user），返回它所在的会话 ID。"""
    session = app.state.broker.create_session("local", "fake", USER)
    await app.state.broker.publish(session.session_id, kind="message", name="message.completed",
                                   payload={"turnId": "t-user", "role": "user", "text": text})
    return session.session_id


def _window(store: RuntimeStore) -> tuple[list[dict], list[dict]]:
    start, end = (datetime.now(UTC) - timedelta(hours=1)).isoformat(), (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    return store.user_texts_between(USER, start, end), store.notes_between(USER, start, end)


def _audits(store: RuntimeStore) -> int:
    return store.connection.execute("SELECT COUNT(*) FROM user_memory_changes").fetchone()[0]


@pytest.mark.parametrize("change", ["kept", "session_deleted", "note_deleted", "note_edited"])
async def test_material_removed_while_the_model_runs_is_not_adopted(tmp_path, change):
    app = _app(tmp_path, f"material-{change}.sqlite3")
    store = app.state.store
    try:
        session_id = await _said(app, "我只做螺纹和铁矿")
        store.set_trade_note("r-1", "亏损单拿太久了，以后到止损就走", via="typed", user_id=USER,
                             trading_account_id=ACCOUNT)
        texts, notes = _window(store)
        assert [item["text"] for item in texts] == ["我只做螺纹和铁矿"] and len(notes) == 1

        def during():
            if change == "session_deleted":
                assert store.delete_session(session_id)
            elif change == "note_deleted":
                assert store.delete_trade_note("r-1", trading_account_id=ACCOUNT)
            elif change == "note_edited":
                store.set_trade_note("r-1", "我改主意了，这条补充不算", via="typed", user_id=USER,
                                     trading_account_id=ACCOUNT)
        quote = "我只做螺纹和铁矿" if change == "session_deleted" or change == "kept" else "以后到止损就走"
        adapter = RecordingAdapter([MemoryOp(op="add", layer="profile", text="只做螺纹和铁矿", quote=quote)],
                                   during=during)
        result = await extract_from_words(adapter, store, USER, texts, notes)
        if change == "kept":
            assert result["applied"] == 1 and _audits(store) == 1
            return
        assert result["applied"] == 0 and result["rejected"] == {"原话已删除或已改": 1}
        assert store.list_memories(USER, include_invalid=True) == [] and _audits(store) == 0
    finally:
        store.close()


async def test_a_deletion_suppresses_the_old_material_but_not_new_words(tmp_path):
    """删除抑制按来源版本：删掉以后，同一批旧原话重放不再加回；用户之后亲口再说一遍是新的决定。"""
    app = _app(tmp_path, "suppress.sqlite3")
    store = app.state.store
    add = [MemoryOp(op="add", layer="rule", text="以后只做日内", quote="以后只做日内不过夜")]
    try:
        await _said(app, "以后只做日内不过夜")
        texts, notes = _window(store)
        assert (await extract_from_words(FakeAdapter(add), store, USER, texts, notes))["applied"] == 1
        memory = store.list_memories(USER)[0]
        user_memory.invalidate(store, USER, memory["id"], "用户删除")

        replay = await extract_from_words(FakeAdapter(add), store, USER, *_window(store))
        assert replay["applied"] == 0 and replay["rejected"] == {"重复": 1}
        assert store.list_memories(USER) == []

        await _said(app, "以后只做日内不过夜")   # 删除之后又亲口说了一遍
        again = await extract_from_words(FakeAdapter(add), store, USER, *_window(store))
        assert again["applied"] == 1
        assert [item["text"] for item in store.list_memories(USER)] == ["以后只做日内"]
    finally:
        store.close()


class _BlockedStats:
    """让统计计算停在工作线程里，测试在这段时间里关开关或取消整理；返回真实的统计条目。"""

    def __init__(self, consolidator: MemoryConsolidator, *, block_first_only: bool = True):
        self.entered, self.release, self.finished = threading.Event(), threading.Event(), threading.Event()
        self.calls = 0
        self.block_first_only = block_first_only
        consolidator._compute_stats = self.compute
        original = consolidator._stats_for

        def stats_for(*args):
            try:
                return original(*args)
            finally:
                self.finished.set()
        consolidator._stats_for = stats_for

    def compute(self, user_id, now):
        self.calls += 1
        if self.calls == 1 or not self.block_first_only:
            self.entered.set()
            assert self.release.wait(5)
        return [_stat("stat:source:manual", "近 90 天手动单 6 笔，赢 5 笔（胜率 83%）")], "net"


@pytest.mark.parametrize("change", ["normal", "switch_off", "cancel"])
async def test_stats_thread_writes_nothing_after_the_switch_turns_off_or_the_run_is_cancelled(tmp_path, change):
    app = _app(tmp_path, f"stats-{change}.sqlite3")
    store = app.state.store
    consolidator: MemoryConsolidator = app.state.memory_consolidator
    consolidator._adapter_for = lambda user_id: FakeAdapter([])
    blocked = _BlockedStats(consolidator)
    day = datetime.now(UTC).astimezone(CHINA).date().isoformat()
    await _said(app, "随便聊聊")
    try:
        task = asyncio.create_task(consolidator.run())
        assert await asyncio.to_thread(blocked.entered.wait, 5)
        assert store.list_memories(USER, include_invalid=True) == []
        if change == "normal":
            blocked.release.set()
            await task
            assert [item["source"] for item in store.list_memories(USER)] == ["stats"]
            assert _runs(store) == {(USER, day): "done"}
            return
        if change == "switch_off":
            store.update_preferences(USER, {"chat": {"memory": False}})
            blocked.release.set()
            await task
            assert _runs(store) == {(USER, day): "done"}
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert _runs(store) == {(USER, day): "error"}   # 取消一返回，旧线程就没有采纳资格了
            blocked.release.set()
            assert await asyncio.to_thread(blocked.finished.wait, 5)
        assert store.list_memories(USER, include_invalid=True) == [] and _audits(store) == 0
        if change == "cancel":
            # 取消后用户手动再整理：新的一次正常写入。
            await consolidator.run_manual(USER)
            assert len(store.list_memories(USER)) == 1 and _runs(store) == {(USER, day): "done"}
        cold = RuntimeStore(tmp_path / f"stats-{change}.sqlite3")
        try:
            assert len(cold.list_memories(USER, include_invalid=True)) == (1 if change == "cancel" else 0)
        finally:
            cold.close()
    finally:
        blocked.release.set()
        store.close()


async def test_an_old_attempt_cannot_adopt_or_finish_after_a_takeover(tmp_path):
    """手动整理接手当天认领后，被接手的那一次（迟到的统计线程）既不能采纳，也不能把状态写回。"""
    app = _app(tmp_path, "takeover.sqlite3")
    store = app.state.store
    consolidator: MemoryConsolidator = app.state.memory_consolidator
    consolidator._adapter_for = lambda user_id: FakeAdapter([])
    day = datetime.now(UTC).astimezone(CHINA).date().isoformat()
    window = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    try:
        old = store.claim_memory_run(USER, day, window_start=window)
        new = store.claim_memory_run(USER, day, window_start=window, force=True)
        assert old and new and old["attempt"] != new["attempt"]
        assert not store.memory_run_current(USER, day, old["attempt"])
        consolidator._compute_stats = lambda user_id, now: (
            [_stat("stat:source:manual", "近 90 天手动单 6 笔，赢 5 笔（胜率 83%）")], "net")
        late = consolidator._stats_for(USER, datetime.now(UTC), {"day": day, "attempt": old["attempt"]})
        assert late == {"skipped": "cancelled"} and store.list_memories(USER) == []
        assert store.record_memory_run(USER, day, "done", {}, attempt=old["attempt"]) is False
        assert _runs(store) == {(USER, day): "running"}
        assert store.record_memory_run(USER, day, "done", {}, attempt=new["attempt"]) is True
        # 重开已完成的一天（手动整理）只用本次窗口，不把已整理过的更早材料再送一遍。
        later = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        reopened = store.claim_memory_run(USER, day, window_start=later, force=True)
        assert reopened and reopened["windowStart"] == later
        assert store.record_memory_run(USER, day, "done", {}, attempt=reopened["attempt"]) is True
        # 卡住的 running 也算进同一天的重试上限；force 不受限。
        store.connection.execute("UPDATE user_memory_runs SET status = 'running', at = ?, attempts = 3"
                                 " WHERE user_id = ? AND day = ?",
                                 ((datetime.now(UTC) - timedelta(hours=2)).isoformat(), USER, day))
        store.connection.commit()
        assert store.claim_memory_run(USER, day, window_start=window) is None
        stuck = store.connection.execute("SELECT attempt FROM user_memory_runs WHERE day = ?", (day,)).fetchone()[0]
        # 第二天认领时，前一天还挂着的 running 作废：它晚回来不能和新的一天同时采纳。
        tomorrow = store.claim_memory_run(USER, "2099-01-01", window_start=window)
        assert tomorrow is not None and not store.memory_run_current(USER, day, stuck)
        assert _runs(store)[(USER, day)] == "error"
        # 另一个连接（另一个进程）同一天：已认领的排他；新的一天只有先到的那个认领成功。
        other = RuntimeStore(tmp_path / "takeover.sqlite3")
        try:
            assert other.claim_memory_run(USER, "2099-01-01", window_start=window) is None
            assert store.claim_memory_run(USER, "2099-01-02", window_start=window) is not None
            assert other.claim_memory_run(USER, "2099-01-02", window_start=window) is None
        finally:
            other.close()
    finally:
        store.close()


async def test_manual_failures_record_error_and_errors_retry_a_bounded_number_of_times(tmp_path):
    app = _app(tmp_path, "manual-error.sqlite3")
    store = app.state.store
    consolidator: MemoryConsolidator = app.state.memory_consolidator
    day = datetime.now(UTC).astimezone(CHINA).date().isoformat()

    class Down:
        calls = 0

        async def complete_structured(self, **_):
            Down.calls += 1
            raise RuntimeError("model down")
    consolidator._adapter_for = lambda user_id: Down()
    await _said(app, "我只做螺纹和铁矿")
    try:
        detail = await consolidator.run_manual(USER)
        assert detail["words"] == {"error": "RuntimeError"} and _runs(store) == {(USER, day): "error"}

        def broken(user_id, now):
            raise RuntimeError("stats down")
        original = consolidator._compute_stats
        consolidator._compute_stats = broken
        with pytest.raises(RuntimeError):
            await consolidator.run_manual(USER)
        assert _runs(store) == {(USER, day): "error"}
        consolidator._compute_stats = original

        # 每日整理对 error 重试，但同一天有上限（这一行已认领 2 次，再重试 1 次就到 3 次）。
        Down.calls = 0
        await consolidator.run()
        await consolidator.run()
        assert Down.calls == 1 and _runs(store) == {(USER, day): "error"}
        # 手动“现在整理一次”不受这个上限；模型恢复后记 done。
        consolidator._adapter_for = lambda user_id: FakeAdapter([])
        await consolidator.run_manual(USER)
        assert _runs(store) == {(USER, day): "done"}
    finally:
        store.close()


async def test_a_failed_days_material_is_carried_into_the_next_run(tmp_path):
    """失败那天只有一条原话：第二天它已滑出 24 小时窗口，仍然由下一次整理补上；补上之后不再回看。"""
    app = _app(tmp_path, "carry.sqlite3")
    store = app.state.store
    consolidator: MemoryConsolidator = app.state.memory_consolidator

    class Down:
        async def complete_structured(self, **_):
            raise RuntimeError("model down")
    consolidator._adapter_for = lambda user_id: Down()
    await _said(app, "我只做螺纹和铁矿")
    now = datetime.now(UTC)
    try:
        await consolidator.run(now)
        later = RecordingAdapter([])
        consolidator._adapter_for = lambda user_id: later
        await consolidator.run(now + timedelta(hours=30))
        assert later.calls == 1 and later.prompts[0]["userMessages"] == ["我只做螺纹和铁矿"]
        next_day = (now + timedelta(hours=30)).astimezone(CHINA).date().isoformat()
        assert _runs(store)[(USER, next_day)] == "done"
        await consolidator.run(now + timedelta(hours=54))
        assert later.calls == 1
    finally:
        store.close()


def test_deletion_order_uses_the_note_time_conservatively():
    """补充写入时间到毫秒：删除后再改写的补充确定在删除之后；与删除分不清先后（旧补充只到秒、
    同一秒）时按删除之前处理——宁可少记，不把删掉的记忆加回来。"""
    from trader_runtime.memory_consolidation import _newer_than_deletions
    deleted = [datetime(2026, 10, 12, 8, 0, 5, 700000, tzinfo=UTC)]
    assert _newer_than_deletions({"at": "2026-10-12T08:00:05.701+00:00"}, deleted)
    assert not _newer_than_deletions({"at": "2026-10-12T08:00:05.699+00:00"}, deleted)
    assert not _newer_than_deletions({"at": "2026-10-12T08:00:05+00:00"}, deleted)   # 旧补充，同一秒
    assert _newer_than_deletions({"at": "2026-10-12T08:00:06+00:00"}, deleted)
    assert not _newer_than_deletions({"at": None}, deleted)


async def test_a_note_rewritten_after_a_deletion_is_a_new_decision(tmp_path):
    app = _app(tmp_path, "note-order.sqlite3")
    store = app.state.store
    add = [MemoryOp(op="add", layer="lesson", text="到止损就走", quote="以后到止损就走")]
    try:
        store.set_trade_note("r-1", "亏损单拿太久了，以后到止损就走", via="typed", user_id=USER,
                             trading_account_id=ACCOUNT)
        assert "." in _window(store)[1][0]["at"]   # 毫秒精度
        assert (await extract_from_words(FakeAdapter(add), store, USER, *_window(store)))["applied"] == 1
        user_memory.invalidate(store, USER, store.list_memories(USER)[0]["id"], "用户删除")
        assert (await extract_from_words(FakeAdapter(add), store, USER, *_window(store)))["rejected"] == {"重复": 1}
        time.sleep(0.002)   # 补充时间到毫秒：与删除同一毫秒分不清先后，按删除之前处理（见上一用例）
        store.set_trade_note("r-1", "想清楚了：以后到止损就走", via="typed", user_id=USER, trading_account_id=ACCOUNT)
        assert (await extract_from_words(FakeAdapter(add), store, USER, *_window(store)))["applied"] == 1
    finally:
        store.close()


async def test_only_material_sent_to_the_model_can_back_a_candidate(tmp_path):
    """没进 prompt 的旧补充（只送最新 20 条）不能替已删掉的那条补充作原话依据。"""
    app = _app(tmp_path, "unseen.sqlite3")
    store = app.state.store
    try:
        base = datetime.now(UTC) - timedelta(minutes=30)
        store.connection.execute(
            "INSERT INTO trade_notes (trading_account_id, round_id, text, via, user_id, updated_at)"
            " VALUES (?, 'old', '老补充：以后到止损就走', 'typed', ?, ?)", (ACCOUNT, USER, base.isoformat()))
        for index in range(20):
            store.connection.execute(
                "INSERT INTO trade_notes (trading_account_id, round_id, text, via, user_id, updated_at)"
                " VALUES (?, ?, ?, 'typed', ?, ?)",
                (ACCOUNT, f"r{index}", "以后到止损就走" if index == 19 else f"补充{index}", USER,
                 (base + timedelta(minutes=1 + index)).isoformat()))
        store.connection.commit()

        def during():
            assert store.delete_trade_note("r19", trading_account_id=ACCOUNT)
        adapter = RecordingAdapter([MemoryOp(op="add", layer="lesson", text="到止损就走", quote="以后到止损就走")],
                                   during=during)
        texts, notes = _window(store)
        assert len(notes) == 21
        result = await extract_from_words(adapter, store, USER, texts, notes)
        assert "老补充：以后到止损就走" not in [note["text"] for note in adapter.prompts[0]["tradeNotes"]]
        assert result["applied"] == 0 and result["rejected"] == {"原话已删除或已改": 1}
    finally:
        store.close()

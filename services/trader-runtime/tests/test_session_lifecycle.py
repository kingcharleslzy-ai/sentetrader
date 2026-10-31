from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.main import create_app

TOKEN = "phase13-token"
ORIGIN = "http://127.0.0.1:5173"


async def test_session_delete_releases_runtime_resources_but_retains_proposal_audit(tmp_path: Path) -> None:
    application = create_app(tmp_path / "runtime.sqlite3", local_token=TOKEN, agent_mode="fake")
    session = application.state.broker.create_session("local", "native", "local-user")
    proposal = application.state.proposals.create_trade(
        session.session_id, "turn-audit", symbol="AU2612", side="buy_open",
        volume=1, price_intent="passive", reason="audit retention test",
    )
    audit_id = application.state.store.audit(proposal["proposalId"], "created", {"retained": True})
    run = application.state.runs.enqueue(
        session_id=session.session_id, user_id="local-user", instruction="回测最近三年"
    )
    await asyncio.sleep(0.02)

    assert await application.state.lifecycle.release(session.session_id) is True
    with pytest.raises(KeyError):
        application.state.store.get_session(session.session_id)
    assert session.session_id not in application.state.runs.workers
    assert session.session_id not in application.state.runs.queues
    assert session.session_id not in application.state.broker.conditions
    assert application.state.store.get(proposal["proposalId"])["id"] == proposal["proposalId"]
    assert application.state.store.connection.execute(
        "SELECT id FROM proposal_audit WHERE id = ?", (audit_id,)
    ).fetchone()["id"] == audit_id
    with pytest.raises(KeyError):
        application.state.store.get_run(str(run["runId"]))
    application.state.store.close()


async def test_runtime_api_exposes_independent_status_and_delete_session(tmp_path: Path) -> None:
    application = create_app(tmp_path / "api.sqlite3", local_token=TOKEN, agent_mode="fake")
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test",
        headers={"Origin": ORIGIN},
    ) as client:
        login = await client.post("/api/auth/local", headers={"Authorization": f"Bearer {TOKEN}"})
        assert login.status_code == 200
        status = (await client.get("/api/runtime/status")).json()
        assert status["runtimeConnected"] is True
        assert status["agentConnected"] is True
        assert status["marketDataStatus"] == "simulated"
        assert status["accountStatus"] == "simulated"
        assert status["tradingGatewayStatus"] == "disabled"
        created = await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})
        session_id = created.json()["sessionId"]
        deleted = await client.delete(f"/api/sessions/{session_id}")
        assert deleted.json()["status"] == "deleted"
        assert (await client.get(f"/api/sessions/{session_id}")).status_code == 404
    application.state.store.close()


async def test_a_message_landing_mid_release_keeps_the_retained_chat_session(tmp_path: Path) -> None:
    """#515 F-c：保留期清扫的删除带二次核对——释放要等待（清运行、等适配器），期间落进来的
    用户消息把会话从删除手里留下；没有新消息的旧会话照常清掉。"""
    application = create_app(tmp_path / "mid-release.sqlite3", local_token=TOKEN, agent_mode="fake")
    store = application.state.store
    broker = application.state.broker
    stale = broker.create_session("local", "native", "local-user")
    store.retain_chat_session(stale.session_id, user_id="local-user")
    busy = broker.create_session("local", "native", "local-user")
    store.retain_chat_session(busy.session_id, user_id="local-user")
    store.update_preferences("local-user", {"data": {"sessionRetentionDays": 30}})
    with store.lock, store.connection:
        # 两个会话都“40 天没动静”：清扫的一轮会把它们都列为过期。
        backdated = "2026-01-01T00:00:00+00:00"
        store.connection.execute(
            "UPDATE sessions SET created_at = ? WHERE id IN (?, ?)",
            (backdated, stale.session_id, busy.session_id))
    original_release = broker.release_session

    async def release_with_a_message_landing(session_id: str) -> None:
        if session_id == busy.session_id:
            # 释放等待期间一条用户消息落了进来（真实入口：对话的消息事件）。
            await broker.publish(session_id, kind="message", name="message.completed", payload={
                "turnId": "turn-mid-release", "role": "user", "text": "还在用"})
        await original_release(session_id)

    broker.release_session = release_with_a_message_landing  # type: ignore[method-assign]
    cleaned = await application.state.lifecycle.cleanup_expired()

    assert cleaned == 1  # 只有真正过期的那个被删了
    with pytest.raises(KeyError):
        store.get_session(stale.session_id, user_id="local-user")
    # 新消息的会话留下：它现在的最后活跃时间是“现在”，不再满足任何保留期口径。
    assert store.get_session(busy.session_id, user_id="local-user")["sessionId"] == busy.session_id
    application.state.store.close()


async def test_a_run_enqueued_inside_the_release_window_answers_409(tmp_path: Path) -> None:
    """#515 F-d：释放的整个窗口（清运行到删行）里 enqueue 直接 409 session_releasing——
    不再接受一个马上会被级联删除踩掉的运行。"""
    application = create_app(tmp_path / "release-409.sqlite3", local_token=TOKEN, agent_mode="fake")
    broker = application.state.broker
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test",
        headers={"Origin": ORIGIN},
    ) as client:
        login = await client.post("/api/auth/local", headers={"Authorization": f"Bearer {TOKEN}"})
        assert login.status_code == 200
        session_id = (await client.post(
            "/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
        mid_release: list[int] = []
        original_release = broker.release_session

        async def release_with_a_late_send(session_id: str) -> None:
            # 消息正好落在释放窗口里（运行已开始清理、行还没删）。
            mid_release.append((await client.post(
                f"/api/sessions/{session_id}/messages", json={"text": "落在释放窗口里"})).status_code)
            await original_release(session_id)

        broker.release_session = release_with_a_late_send  # type: ignore[method-assign]
        deleted = await client.delete(f"/api/sessions/{session_id}")
        assert deleted.json()["status"] == "deleted"
        assert mid_release == [409]
    application.state.store.close()


def test_listed_order_cards_name_their_proposals_account(tmp_path: Path) -> None:
    """#1005: a card's order status is its own account's; the listed card carries that account from the
    proposal record, including a card published before the field existed."""
    from trader_runtime.store import RuntimeStore

    store = RuntimeStore(tmp_path / "cards.sqlite3")
    session = store.create_session(user_id="local-user", mode="local", agent_id="native",
                                   trading_account_id="paper:a")
    store.create(proposal_id="card-b", session_id=session["sessionId"], proposal_type="trade",
                 status="candidate_recorded", payload={"proposalId": "card-b"}, trading_account_id="tqkq-sim:b")

    def event(event_id: str, name: str, payload: dict[str, object]) -> dict[str, object]:
        return {"schemaVersion": "1", "id": event_id, "kind": "approval", "name": name,
                "timestamp": "2026-10-19T00:00:00+00:00", "payload": payload}

    store.append_event(session["sessionId"], event("e1", "proposal.trade", {"proposalId": "card-b", "turnId": "t"}))
    store.append_event(session["sessionId"], event("e2", "proposal.trade", {"proposalId": "no-record"}))
    store.append_event(session["sessionId"], event("e3", "message.completed", {"proposalId": "card-b"}))
    payloads = [item["payload"] for _, item in store.list_events(session["sessionId"])]
    assert payloads[0]["accountId"] == "tqkq-sim:b"   # the proposal's account, not the session's
    assert "accountId" not in payloads[1]              # no proposal record: unchanged
    assert "accountId" not in payloads[2]              # not an order card
    store.close()

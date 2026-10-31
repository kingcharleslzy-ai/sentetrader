"""确认卡超时（2026-09-28 网页实测）：确认窗口（10 分钟）过了的卡在数据库里仍是“等待确认”，网页卡片还能点、
点了静默 409，AI 也以为卡还在、不肯重出。现在：迟到的确认说明原因且不下单；每 30 秒的清理把这类卡按“取消”
同样作废并发 proposal.result；AI 判断“有没有待确认的卡”只算窗口内的。另：开了“语音下单免口头确认”时，通话里
的偏好说明让模型说“按你的设置直接下单”而不是问“确认吗”。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from test_simulation_orders import Gateway, NormalProvider, application, confirm, proposal, quote_now
from trader_runtime.lifecycle import SessionLifecycleManager
from trader_runtime.proposals import APPROVAL_EXPIRED_TEXT, ProposalConflict, awaiting_confirmation
from trader_runtime.user_preferences import UserPreferences, preference_instruction


def _expire(app, item) -> None:
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with app.state.store.transaction() as connection:
        connection.execute("UPDATE proposals SET approval_expires_at = ? WHERE id = ?", (past, item["proposalId"]))


async def test_a_late_confirmation_says_why_and_sends_nothing(tmp_path: Path) -> None:
    gateway, provider = Gateway(), NormalProvider()
    app = application(tmp_path / "late.sqlite3", gateway, provider)
    item = proposal(app)
    provider.quote_datetime = quote_now()
    _expire(app, item)
    with pytest.raises(ProposalConflict) as refused:
        await confirm(app, item)
    assert refused.value.code == "approval_expired" and str(refused.value) == APPROVAL_EXPIRED_TEXT
    assert gateway.submit_count == 0
    app.state.store.close()


async def test_the_sweep_withdraws_expired_cards_like_a_cancel(tmp_path: Path) -> None:
    app = application(tmp_path / "sweep.sqlite3", Gateway())
    store = app.state.store
    fresh, stale = proposal(app, turn="fresh"), proposal(app, turn="stale")
    _expire(app, stale)
    manager = SessionLifecycleManager(store=store, broker=app.state.broker, runs=None, adapter=None)
    assert await manager.expire_approvals() == 1
    withdrawn = store.get(stale["proposalId"])
    assert withdrawn["status"] == "rejected" and withdrawn["payload"]["expired"] is True
    assert withdrawn["approvalUsedAt"] is None                      # nobody confirmed or cancelled it
    assert store.get(fresh["proposalId"])["status"] == "approval_required"
    results = [event for _, event in store.list_events(withdrawn["sessionId"]) if event.get("name") == "proposal.result"]
    assert results and results[-1]["payload"] == {"proposalId": stale["proposalId"], "status": "rejected",
                                                  "auditId": withdrawn["payload"]["auditId"], "message": APPROVAL_EXPIRED_TEXT,
                                                  "reason": "expired"}  # #932 P2: the card says 已过期
    assert await manager.expire_approvals() == 0                    # a second sweep finds nothing
    store.close()


def test_only_cards_inside_their_window_are_awaiting_confirmation():
    now = datetime(2026, 9, 28, 2, 30, tzinfo=UTC)
    card = {"status": "approval_required", "approvalExpiresAt": "2026-09-28T02:31:00+00:00"}
    assert awaiting_confirmation(card, now)
    assert not awaiting_confirmation({**card, "approvalExpiresAt": "2026-09-28T02:29:59+00:00"}, now)
    assert not awaiting_confirmation({**card, "status": "rejected"}, now)
    assert not awaiting_confirmation({**card, "approvalExpiresAt": None}, now)


def test_a_call_with_no_spoken_confirmation_tells_the_model_not_to_ask():
    free = UserPreferences.model_validate({"trade": {"voiceOrderConfirm": False}})
    assert "不要问“确认吗”" in preference_instruction(free, origin="voice")
    assert "确认吗" not in preference_instruction(free)                      # typed chat still confirms on the card
    assert preference_instruction(UserPreferences(), origin="voice") == ""    # the default asks, as before


async def test_old_cards_settle_from_one_read_of_their_records(tmp_path: Path) -> None:
    """历史卡的结局: the page reads every old waiting card's record in one request (GET /api/proposals?ids=…);
    only the caller's own cards come back."""
    from httpx import ASGITransport, AsyncClient

    app = application(tmp_path / "batch.sqlite3", Gateway())
    first, second = proposal(app, turn="one"), proposal(app, turn="two")
    with app.state.store.transaction() as connection:
        connection.execute("UPDATE proposals SET user_id = 'someone-else' WHERE id = ?", (second["proposalId"],))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"Origin": "http://127.0.0.1:5173"}) as http:
            assert (await http.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
            answer = await http.get("/api/proposals", params={"ids": [first["proposalId"], second["proposalId"], "missing"]})
            assert answer.status_code == 200
            assert [(item["id"], item["status"]) for item in answer.json()["items"]] == [(first["proposalId"], "approval_required")]
            assert (await http.get("/api/proposals", params={"ids": [f"id-{n}" for n in range(101)]})).status_code == 422
    finally:
        app.state.store.close()

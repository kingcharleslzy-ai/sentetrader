"""CNY conversations can read their USD history without changing execution scope."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from test_foreign_paper import MAIN, foreign_app
from test_native_agent import (
    _fast_research_deadlines,  # noqa: F401 -- fixture
    _model_saw,
    _window_pulse,
    create_session,
    native_application,  # noqa: F401 -- fixture
    native_client,  # noqa: F401 -- fixture
    responses_server,  # noqa: F401 -- fixture
    wait_for_run,
)
from test_paper_accounts import close_paper_app, register_user
from test_trade_review import fill, with_tools


async def test_review_currency_uses_the_declared_ledger_and_existing_permissions(tmp_path, monkeypatch):
    app, _ = foreign_app(tmp_path, monkeypatch, "review-ledgers.sqlite3")
    friend = other = None
    try:
        friend, user = await register_user(app)
        other, _ = await register_user(app, username="other-friend")
        store = app.state.store
        parent = store.find_user_paper_account(user_id=user)["id"]
        usd = app.state.paper_accounts.foreign_account_of(parent)
        day = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%d")
        store.upsert_account_trades([fill("a-open", MAIN, "BUY", "OPEN", 4000, day, 10),
                                     fill("a-close", MAIN, "SELL", "CLOSE", 4001, day, 11)],
                                    {MAIN: 100}, trading_account_id=usd)
        store.upsert_account_trades([fill("z-open", MAIN, "BUY", "OPEN", 4000, day, 10),
                                     fill("z-close", MAIN, "SELL", "CLOSE", 4001, day, 11)],
                                    {MAIN: 100}, trading_account_id=usd)
        # Today's activity is empty; historical closed trades are still present.
        activity = await friend.get("/api/account/activity", headers={"X-AI-Trader-Account": usd})
        assert activity.status_code == 200 and activity.json()["trades"] == []
        default = await friend.get("/api/account/pnl")
        assert default.status_code == 200 and default.json()["currency"] == "CNY"
        historical = await friend.get("/api/account/pnl", params={"currency": "USD"})
        assert historical.status_code == 200
        assert historical.json()["currency"] == "USD"
        assert [t["id"] for t in historical.json()["trades"]] == ["a-open:a-close", "z-open:z-close"]
        explicit = await friend.get("/api/account/pnl", headers={"X-AI-Trader-Account": usd})
        assert explicit.json() == historical.json()
        # Direct HTTP, app_call and the embedded tool share the same historical owner.
        with_tools(app)
        for body in [{"operation": "GET /api/account/journal", "query": {"currency": "USD", "days": 366, "limit": 1}},
                     {"operation": "tool:get_trade_journal", "body": {"currency": "USD", "days": 366, "limit": 1}}]:
            response = (await friend.post("/api/agent/call", json=body)).json()
            assert response["ok"]
            data = response["data"]
            if "data" in data:
                data = data["data"]
            assert data["currency"] == "USD" and data["days"] == 366
            assert [t["id"] for t in data["trades"]] == ["a-open:a-close"]
        recent = (await friend.get("/api/account/journal", params={"currency": "USD", "days": 1})).json()
        assert recent["trades"] == [] and recent["days"] == 1 and recent["currency"] == "USD"
        assert (await other.get("/api/account/pnl", params={"currency": "USD"})).json()["trades"] == []
        assert store.get_user_default_account(user_id=user) == parent
        # No grant to this exact child means no read, even though the parent still exists.
        with store.lock, store.connection:
            store.connection.execute("DELETE FROM account_grants WHERE user_id=? AND account_id=?", (user, usd))
        assert (await friend.get("/api/account/pnl", params={"currency": "USD"})).status_code == 403
        assert (await friend.get("/api/account/journal", params={"currency": "USD"})).status_code == 403
        assert (await friend.get("/api/account/pnl", params={"currency": "EUR"})).status_code == 422
    finally:
        if friend:
            await friend.aclose()
        if other:
            await other.aclose()
        await close_paper_app(app)


async def test_voice_review_context_follows_the_window_without_keyword_routing(
    native_client, native_application, responses_server,
):
    session = await create_session(native_client)
    window = str(uuid4())
    pulse = _window_pulse(window, "复盘 美元 两笔历史记录")
    pulse["state"].update(view="review", review={"currency": "USD", "currencies": ["CNY", "USD"],
        "month": "2026-10", "day": "2026-10-22", "filter": "CME.MGCZ26", "selectedTradeId": None})
    assert (await native_client.post("/api/ui/pulse", json=pulse)).status_code == 200
    accepted = await native_client.post(f"/api/sessions/{session}/messages", json={
        "text": "选择微型美黄金的第一笔平仓记录", "origin": "voice", "uiWindowId": window})
    await wait_for_run(native_application, accepted.json()["runId"])
    assert _model_saw(responses_server, '"currency": "USD"')
    assert _model_saw(responses_server, '"filter": "CME.MGCZ26"')
    assert _model_saw(responses_server, "activity只含当日成交")
    assert _model_saw(responses_server, "days=366")
    # The next window state supersedes the prior ledger, rather than becoming stale chat history.
    pulse["revision"] = 1
    pulse["state"]["review"]["currency"] = "CNY"
    assert (await native_client.post("/api/ui/pulse", json=pulse)).status_code == 200
    accepted = await native_client.post(f"/api/sessions/{session}/messages", json={
        "text": "看看这笔历史记录", "origin": "voice", "uiWindowId": window})
    await wait_for_run(native_application, accepted.json()["runId"])
    assert _model_saw(responses_server, '"currency": "CNY"')

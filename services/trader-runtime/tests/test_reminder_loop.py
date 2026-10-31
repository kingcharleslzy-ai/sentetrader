"""Protects the Runtime-side 收盘前 / 风险度 reminders with no client open: snapshot → judgment → dedup row → message, card and lock-screen push, once per day across rounds and restarts, and nothing from a stale snapshot."""
from __future__ import annotations

from pathlib import Path

from test_account_reminders import CLOSE_15, TUE, ReminderProvider, at, login, reminder_cards, reminder_notices
from test_simulation_orders import Gateway, application
from test_web_push import APPLE, body, configure
from trader_runtime import account_reminders


async def test_the_loop_reminds_and_pushes_once_without_any_client_read(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 14, 56))
    path = tmp_path / "loop.sqlite3"
    provider = ReminderProvider()
    provider.schedules.update({"SHFE.au2612": CLOSE_15, "SHFE.rb2610": CLOSE_15})
    # A cached snapshot first (#458): nothing is judged, nothing is recorded.
    provider.summary_overrides = {"riskRatio": 86.0, "margin": 430_000, "equity": 500_000, "available": 70_000,
                                  "isCached": True}
    app = application(path, Gateway(), provider)
    sender = configure(app)
    http = await login(app)
    session_id = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
    # 交割提醒 rides the same loop (#932); this test is about 收盘前 / 风险度.
    assert (await http.put("/api/preferences", json={"notify": {"marketCloseReminder": True,
                                                                "delivery": False}})).status_code == 200
    assert (await http.put("/api/app/push/subscription", json=body("phone-0001", APPLE))).status_code == 200
    loop = app.state.account_reminder_loop

    assert await loop.run_once() == 1
    await app.state.push.drain()
    assert reminder_notices(app, session_id) == [] and sender.sent == []

    # Fresh again: both reminders, each with its card and one push that links to that card.
    provider.summary_overrides = {**provider.summary_overrides, "isCached": False}
    await loop.run_once()
    await app.state.push.drain()
    close = reminder_notices(app, session_id, "reminder-market_close_day")
    risk = reminder_notices(app, session_id, "reminder-risk_ratio")
    assert len(close) == 1 and len(risk) == 1
    close_turn = close[0]["payload"]["turnId"]
    assert reminder_cards(app, session_id, close_turn)[0]["kind"] == "market_close"
    pushes = {payload["kind"]: payload for _, payload in sender.sent}
    assert set(pushes) == {"market_close", "risk"} and len(sender.sent) == 2
    assert pushes["market_close"]["url"] == f"?open=card:reminder-{close_turn}"
    assert pushes["market_close"]["title"] == "收盘前持仓" and "4 分钟后日盘收盘" in pushes["market_close"]["body"]

    # The next round (and a client read through the other entry) sends nothing new.
    await loop.run_once()
    assert (await http.get("/api/positions")).status_code == 200
    await app.state.push.drain()
    assert len(reminder_notices(app, session_id)) == 2 and len(sender.sent) == 2
    await http.aclose()
    app.state.store.close()

    # A restart on the same database does not remind again.
    restarted = application(path, Gateway(), provider)
    sender = configure(restarted)
    await restarted.state.account_reminder_loop.run_once()
    await restarted.state.push.drain()
    assert len(reminder_notices(restarted, session_id)) == 2 and sender.sent == []
    restarted.state.store.close()

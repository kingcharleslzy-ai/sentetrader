"""Protects 盘中异动 on the Runtime: a held contract is watched even when nobody reads its chart (its channel is held, released after the position closes, never opened without spare capacity), a brief product only when already served; one message + card + desktop event per movement for every account owner, a push only to subscribed devices, at most one per contract per 15 minutes, and no order button."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_account_reminders import ReminderProvider, assert_no_order_action, login, reminder_cards, reminder_notices
from test_simulation_orders import Gateway, application
from test_web_push import APPLE, body, configure

START = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)   # 10:00 Beijing


def moving_workbench(symbol: str, end: datetime) -> dict:
    """25 closed one-minute bars ending at `end`: 20 quiet ones, then 5 rising 3 ticks each on 3× volume."""
    first = int((end - timedelta(minutes=25)).timestamp()) * 1_000_000_000
    bars = []
    for index in range(25):
        base = 3000 + max(0, index - 19) * 3
        bars.append({"datetime": first + index * 60_000_000_000, "open": base, "high": base + 3 if index >= 20 else base + 1,
                     "low": base, "close": base + 3 if index >= 20 else base, "volume": 300 if index >= 20 else 100})
    return {"dataStatus": "normal", "ageSeconds": 1, "marketSessionStatus": "trading", "bars": bars,
            "quote": {"symbol": symbol, "name": "螺纹钢2610", "priceTick": 1}}


class Channel:
    def __init__(self, symbol):
        self.symbol, self.packet, self.error = symbol, None, None


class Shared:
    """The shared market as the watch sees it: served series, channels it opens and releases, free slots."""

    def __init__(self):
        self.served: dict[str, dict] = {}
        self.feeds: dict[str, dict] = {}     # what an opened channel would carry
        self.opened: list[str] = []
        self.released: list[str] = []
        self.free = 40

    def served_workbench(self, symbol, duration):
        assert duration == 60
        return self.served.get(symbol)

    def free_market_slots(self):
        return self.free

    async def acquire_market(self, symbol, duration):
        assert duration == 60
        self.opened.append(symbol)
        self.free -= 1
        channel = Channel(symbol)
        channel.packet = {"value": {"workbench": self.feeds.get(symbol) or {"dataStatus": "normal", "bars": [], "quote": {}}}}
        return channel

    def release_market(self, channel):
        self.released.append(channel.symbol)
        self.free += 1


async def test_held_contracts_are_watched_without_a_reader_and_brief_products_only_when_served(tmp_path: Path, monkeypatch):
    provider = ReminderProvider()
    app = application(tmp_path / "movement.sqlite3", Gateway(), provider)
    sender = configure(app)
    shared = Shared()
    app.state.shared_market = shared
    http = await login(app)
    session_id = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
    app.state.broker.subscribers[session_id] = 1

    async def subscribed(user):
        return ["DCE.i2701"]   # an AI 简报 product nobody is reading right now
    monkeypatch.setattr(app.state.market_briefs, "subscribed_main_symbols", subscribed)
    clock = {"now": START}
    watch = app.state.movement_watch
    watch.now = lambda: clock["now"]

    # 盘中异动 is off by default (owner 2026-10-14): nothing is watched until the user turns it on.
    assert (await watch.run_once())["users"] == 0 and shared.opened == []
    assert (await http.put("/api/preferences", json={"notify": {"movement": True}})).status_code == 200

    # No lock-screen device yet: the conversation card and the desktop event still go out. rb2610 is held
    # (short 1) and nobody reads its chart — the watch opens and holds its channel; i2701 is not served.
    shared.feeds["SHFE.rb2610"] = moving_workbench("SHFE.rb2610", START)
    counts = await watch.run_once()
    await app.state.push.drain()
    assert sorted(shared.opened) == ["SHFE.au2612", "SHFE.rb2610"] and shared.released == []
    assert counts == {"users": 1, "judged": 2, "unserved": 1, "capacity": 0, "fired": 1}
    notices = reminder_notices(app, session_id, "movement-")
    assert len(notices) == 1 and notices[0]["payload"]["text"].startswith("盘中异动：螺纹钢2610 近5分钟上涨15跳")
    turn = notices[0]["payload"]["turnId"]
    card = reminder_cards(app, session_id, turn)[0]
    assert card["kind"] == "market_movement" and card["title"] == "盘中异动 · 螺纹钢2610"
    figures = {item["label"]: item["value"] for item in card["figures"]}
    assert figures["5 分钟"] == "+15 跳" and figures["量能"] == "3.0 倍" and figures["持仓"] == "1 手空单"
    assert [action["label"] for action in card["actions"]] == ["平一半", "问 AI 怎么办"]
    assert_no_order_action(card)
    feed = [event for _, event in app.state.broker.transient_events[session_id] if event.get("name") == "feed.reminder"]
    assert feed[-1]["payload"]["kind"] == "market_movement"
    assert sender.sent == []   # no device has lock-screen push

    # Same movement next minute: once only; the held channels are not opened again.
    assert (await http.put("/api/app/push/subscription", json=body("phone-0001", APPLE))).status_code == 200
    clock["now"] = START + timedelta(minutes=1)
    assert (await watch.run_once())["fired"] == 0 and len(shared.opened) == 2
    # A new 5-minute bucket 6 minutes later is still inside the quarter hour; past it, a movement reminds again
    # — now also on the subscribed device, linking to its card.
    for minutes, fired in ((6, 0), (16, 1)):
        clock["now"] = START + timedelta(minutes=minutes)
        watch._channels["SHFE.RB2610"].packet = {"value": {"workbench": moving_workbench("SHFE.rb2610", clock["now"])}}
        assert (await watch.run_once())["fired"] == fired
    await app.state.push.drain()
    assert len(reminder_notices(app, session_id, "movement-")) == 2
    second = reminder_notices(app, session_id, "movement-")[-1]["payload"]["turnId"]
    assert [(payload["kind"], payload["url"]) for _, payload in sender.sent] == [("movement", f"?open=card:reminder-{second}")]
    assert "你有 1 手空单，浮亏 310 元。" in sender.sent[0][1]["body"]

    # The rb position closes: its channel is released; i2701 starts being served and is judged.
    provider.position_items = [item for item in provider.position_items if item["symbol"] != "SHFE.rb2610"]
    shared.served["DCE.i2701"] = {"dataStatus": "normal", "bars": [], "quote": {}}
    counts = await watch.run_once()
    assert shared.released == ["SHFE.rb2610"] and counts["unserved"] == 0 and counts["judged"] == 2

    # 盘中异动 off: nothing is watched for this user, and every held channel goes back.
    assert (await http.put("/api/preferences", json={"notify": {"movement": False}})).status_code == 200
    assert (await watch.run_once())["users"] == 0 and sorted(shared.released) == ["SHFE.au2612", "SHFE.rb2610"]
    await http.aclose()
    app.state.store.close()


async def test_no_new_channel_without_spare_capacity(tmp_path: Path):
    app = application(tmp_path / "movement-full.sqlite3", Gateway(), ReminderProvider())
    http = await login(app)
    assert (await http.put("/api/preferences", json={"notify": {"movement": True}})).status_code == 200
    shared = Shared()
    shared.free = 8          # at the reserve: the public market keeps these for readers
    app.state.shared_market = shared
    watch = app.state.movement_watch
    counts = await watch.run_once()
    assert shared.opened == [] and counts["capacity"] == 2 and counts["fired"] == 0
    shared.free = 9
    await watch.run_once()
    assert len(shared.opened) == 1 and shared.opened[0] in {"SHFE.au2612", "SHFE.rb2610"}
    await watch.stop()
    assert shared.released == shared.opened
    await http.aclose()
    app.state.store.close()

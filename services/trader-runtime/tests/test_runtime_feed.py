"""The push feed replaces client polling: changed snapshots only, throttled, per session focus."""
import asyncio
import json
import time

import httpx
import pytest

from test_stream_revocation import ORIGIN, Reader, serving
from trader_runtime import runtime_feed as feed_module
from trader_runtime.main import create_app
from trader_runtime.runtime_feed import FeedReadError, RuntimeFeed

TOKEN = "feed-test-token"


class FakeBroker:
    def __init__(self, subscribed):
        self.subscribed = set(subscribed)
        self.published = []
        self.generations = {}

    def subscribed_session_ids(self):
        return tuple(self.subscribed)

    def stream_generation(self, session_id):
        return self.generations.get(session_id, 1)

    def reattach(self, session_id):
        self.generations[session_id] = self.stream_generation(session_id) + 1

    async def publish_transient(self, session_id, *, kind, name, payload):
        self.published.append((session_id, kind, name, payload))


def make_feed(broker, *, account=None, market=None, activity=None, execution=None, raw_activity=None, **options):
    users = {"s1": "alice", "s2": "bob"}
    return RuntimeFeed(
        broker=broker, session_user=options.pop("session_user", users.get),
        account_activity=raw_activity or (lambda account_id: asyncio.sleep(0, {"orders": [], "trades": []})),
        account_overview=account or (lambda account_id, user, raw: asyncio.sleep(0, {"account": {"user": user, "equity": 1}, "positions": [], "activity": raw, "risk": {}})),
        market_workbench=market or (lambda account_id, symbol, duration: asyncio.sleep(0, {"schemaVersion": 1, "durationSeconds": duration, "quote": {"symbol": symbol}, "bars": [], "liveTrading": "disabled"})),
        activity=activity or (lambda account_id, user, symbol, raw: asyncio.sleep(0, {"environment": "simulation", "symbol": symbol, "orders": raw["orders"], "fills": [], "strategies": []})),
        execution=execution or (lambda account_id, user: asyncio.sleep(0, {"stops": [], "simulationOnly": True, "liveTrading": "disabled", "user": user})),
        **options,
    )


async def test_tick_pushes_account_and_execution_to_attached_sessions_only_when_changed():
    broker = FakeBroker({"s1"})
    feed = make_feed(broker)
    assert await feed.tick() == 2
    names = [(item[0], item[2]) for item in broker.published]
    assert names == [("s1", "feed.account"), ("s1", "feed.execution")]
    # Same payload again: nothing is sent.
    assert await feed.tick() == 0
    # A session without an SSE reader receives nothing.
    feed.set_focus("s2", symbol="SHFE.rb2610", duration=60)
    assert await feed.tick() == 0


async def test_focus_adds_market_and_activity_and_is_reused_across_sessions(monkeypatch):
    broker = FakeBroker({"s1", "s2"})
    calls = []
    async def market(account_id, symbol, duration):
        calls.append((symbol, duration))
        return {"schemaVersion": 1, "durationSeconds": duration, "quote": {"symbol": symbol, "last": len(calls)}, "bars": [], "liveTrading": "disabled"}
    feed = make_feed(broker, market=market)
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=300)
    feed.set_focus("s2", symbol="SHFE.rb2610", duration=300)
    await feed.tick()
    markets = [item for item in broker.published if item[2] == "feed.market"]
    assert {item[0] for item in markets} == {"s1", "s2"}
    assert calls == [("SHFE.rb2610", 300)]  # one read serves both sessions
    # Within the market throttle a changed quote is not re-read.
    assert await feed.tick() == 0
    assert calls == [("SHFE.rb2610", 300)]
    clock = time.monotonic()
    monkeypatch.setattr(feed_module, "monotonic", lambda: clock + 2)
    await feed.tick()
    assert len(calls) == 2
    assert len([item for item in broker.published if item[2] == "feed.market"]) == 4


async def test_snapshot_gated_reads_happen_only_after_a_new_snapshot(monkeypatch):
    broker = FakeBroker({"s1"})
    calls = []
    reads = []
    async def market(account_id, symbol, duration):
        calls.append(symbol)
        return {"schemaVersion": 1, "durationSeconds": duration, "quote": {"symbol": symbol, "last": len(calls)}, "bars": [], "liveTrading": "disabled"}
    async def raw_activity(account_id):
        reads.append(1)
        return {"orders": [], "trades": []}
    feed = make_feed(broker, market=market, raw_activity=raw_activity, snapshot_gated=True)
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=60)
    await feed.tick()
    clock = time.monotonic()
    monkeypatch.setattr(feed_module, "monotonic", lambda: clock + 2)
    await feed.tick()
    assert len(calls) == 1 and len(reads) == 1  # throttle elapsed, but the provider has nothing new
    feed.snapshot_arrived()
    await feed.tick()
    assert len(calls) == 2 and len(reads) == 2
    # A focus change is served without waiting for a snapshot.
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=300)
    await feed.tick()
    assert len(calls) == 3


async def test_the_snapshot_gate_expires_so_a_silent_provider_is_re_judged(monkeypatch):
    # The gate may skip reads the provider cannot have changed, but it must never
    # hold a channel silent forever: a provider that stopped refreshing is itself
    # the staleness the client has to learn about.
    broker = FakeBroker({"s1"})
    calls = []
    async def market(account_id, symbol, duration):
        calls.append(symbol)
        if len(calls) > 1:
            raise FeedReadError("market_quote_not_ready")
        return {"schemaVersion": 1, "durationSeconds": duration, "quote": {"symbol": symbol}, "bars": [], "liveTrading": "disabled"}
    feed = make_feed(broker, market=market, snapshot_gated=True)
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=60)
    await feed.tick()
    clock = time.monotonic()
    monkeypatch.setattr(feed_module, "monotonic", lambda: clock + 2)
    await feed.tick()
    assert len(calls) == 1  # still inside the refresh window: the gate holds
    monkeypatch.setattr(feed_module, "monotonic", lambda: clock + 6)
    await feed.tick()
    assert len(calls) == 2
    status = [item for item in broker.published if item[2] == "feed.status"]
    assert status[-1][3] == {"channel": "market", "status": "unavailable", "reason": "market_quote_not_ready"}


async def test_account_activity_is_read_once_per_tick_for_account_and_activity():
    broker = FakeBroker({"s1", "s2"})
    reads = []
    async def raw_activity(account_id):
        reads.append(1)
        return {"orders": [{"id": len(reads)}], "trades": []}
    feed = make_feed(broker, raw_activity=raw_activity)
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=60)
    feed.set_focus("s2", symbol="SHFE.ag2612", duration=60)
    await feed.tick()
    assert len(reads) == 1
    accounts = [item for item in broker.published if item[2] == "feed.account"]
    activities = [item for item in broker.published if item[2] == "feed.activity"]
    assert len(accounts) == 2 and len(activities) == 2
    assert all(item[3]["activity"]["orders"] == [{"id": 1}] for item in accounts)
    assert all(item[3]["orders"] == [{"id": 1}] for item in activities)


async def test_an_order_change_pushes_activity_before_the_next_snapshot(monkeypatch):
    broker = FakeBroker({"s1"})
    orders = []
    async def raw_activity(account_id):
        return {"orders": list(orders), "trades": []}
    feed = make_feed(broker, raw_activity=raw_activity, snapshot_gated=True)
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=60)
    await feed.tick()
    clock = time.monotonic()
    monkeypatch.setattr(feed_module, "monotonic", lambda: clock + 2)
    orders.append({"id": "o1"})
    assert await feed.tick() == 0  # no snapshot yet: the activity is not even re-read
    feed.orders_changed()          # what SimulationOrderService.on_change calls
    assert await feed.tick() >= 1
    activities = [item for item in broker.published if item[2] == "feed.activity"]
    assert activities[-1][3]["orders"] == [{"id": "o1"}]


async def test_a_reattached_reader_gets_every_payload_again(monkeypatch):
    broker = FakeBroker({"s1"})
    feed = make_feed(broker)
    assert await feed.tick() == 2
    clock = time.monotonic()
    monkeypatch.setattr(feed_module, "monotonic", lambda: clock + 2)
    assert await feed.tick() == 0  # nothing changed
    broker.reattach("s1")  # the desktop re-opened its SSE stream and lost its feed state
    assert await feed.tick() == 2
    assert [item[2] for item in broker.published] == ["feed.account", "feed.execution"] * 2


async def test_timestamp_only_changes_are_resent_at_the_refresh_interval(monkeypatch):
    broker = FakeBroker({"s1"})
    reads = []
    async def account(account_id, user, raw):
        reads.append(1)
        return {"account": {"equity": 1, "ageSeconds": len(reads), "capturedAt": f"t{len(reads)}"}, "positions": [], "activity": raw, "risk": {}}
    feed = make_feed(broker, account=account)
    await feed.tick()
    clock = time.monotonic()
    monkeypatch.setattr(feed_module, "monotonic", lambda: clock + 2)
    await feed.tick()
    assert len(reads) == 2
    assert len([item for item in broker.published if item[2] == "feed.account"]) == 1  # only the age moved
    monkeypatch.setattr(feed_module, "monotonic", lambda: clock + 6)
    await feed.tick()
    assert len([item for item in broker.published if item[2] == "feed.account"]) == 2  # refreshed after 5 s


async def test_a_failed_read_reports_unavailable_on_its_own_channel(monkeypatch):
    # Silence used to leave the client holding the last good payload as if it were
    # current, while the other channels kept the whole feed looking healthy.
    broker = FakeBroker({"s1"})
    down = [True]
    async def broken(account_id, user, raw):
        if down[0]:
            raise FeedReadError("account_unavailable")
        return {"account": {"user": user, "equity": 1}, "positions": [], "activity": raw, "risk": {}}
    feed = make_feed(broker, account=broken)
    assert await feed.tick() == 2
    assert [(item[2], item[3].get("channel")) for item in broker.published] == [
        ("feed.status", "account"), ("feed.execution", None)]
    assert broker.published[0][3] == {"channel": "account", "status": "unavailable", "reason": "account_unavailable"}
    # Repeating the same failure is not a new event; the execution channel is unaffected.
    assert await feed.tick() == 0
    # Recovery publishes the snapshot again on the same channel.
    clock = time.monotonic()
    monkeypatch.setattr(feed_module, "monotonic", lambda: clock + 2)
    down[0] = False
    assert await feed.tick() == 1
    assert broker.published[-1][2] == "feed.account"


async def test_an_unnamed_read_failure_still_reports_the_channel():
    broker = FakeBroker({"s1"})
    async def broken(account_id, user, raw):
        raise ValueError("some internal detail")
    feed = make_feed(broker, account=broken)
    assert await feed.tick() == 2
    assert broker.published[0][3] == {"channel": "account", "status": "unavailable", "reason": "read_unavailable"}


async def test_one_failed_account_activity_read_reports_both_channels_it_feeds():
    broker = FakeBroker({"s1"})
    reads = []
    async def raw_activity(account_id):
        reads.append(1)
        raise FeedReadError("account_activity_unavailable")
    feed = make_feed(broker, raw_activity=raw_activity)
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=60)
    await feed.tick()
    assert len(reads) == 1  # the expensive read is not repeated for the second channel
    reported = {item[3]["channel"] for item in broker.published if item[2] == "feed.status"}
    assert reported == {"account", "activity"}
    assert {item[2] for item in broker.published} == {"feed.status", "feed.execution", "feed.market"}


def test_only_a_code_shaped_reason_reaches_the_client():
    """A provider exception's text can carry paths, symbols or credentials. Only the
    condition code travels on the feed; anything else becomes the channel's own code."""
    from trader_runtime.wiring.feed import _read_error
    assert _read_error(RuntimeError("market_quote_not_ready"), "market_unavailable").reason == "market_quote_not_ready"
    assert _read_error(KeyError("sidecar_reconnecting"), "market_unavailable").reason == "sidecar_reconnecting"
    for prose in ("connection refused to 10.0.0.2:8765 (token=abc)", "行情读取失败", "Market Quote Not Ready", ""):
        assert _read_error(RuntimeError(prose), "market_unavailable").reason == "market_unavailable"
    assert _read_error(RuntimeError(), "account_unavailable").reason == "account_unavailable"
    assert _read_error(RuntimeError(object()), "account_unavailable").reason == "account_unavailable"


def test_focus_rejects_unknown_durations_and_sessions():
    feed = make_feed(FakeBroker(set()))
    with pytest.raises(ValueError):
        feed.set_focus("s1", symbol="SHFE.rb2610", duration=45)
    with pytest.raises(KeyError):
        feed.set_focus("nobody", symbol="SHFE.rb2610", duration=60)


async def until(condition, limit=5.0):
    """Wait for an event instead of sleeping a fixed time: returns as soon as `condition()` holds."""
    async with asyncio.timeout(limit):
        while not condition():
            await asyncio.sleep(0.005)


async def test_sse_readers_receive_feed_events_and_ui_nudges(tmp_path):
    app = create_app(tmp_path / "feed.sqlite3", local_token=TOKEN, agent_mode="fake", data_mode="fake",
                     engineering_root=tmp_path / "project")
    async with serving(app) as base, httpx.AsyncClient(base_url=base, headers={"Origin": ORIGIN}, trust_env=False) as client:
        assert (await client.post("/api/auth/local", headers={"authorization": f"Bearer {TOKEN}"})).status_code == 200
        client.headers["authorization"] = f"Bearer {TOKEN}"
        session = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
        reader = Reader(client, session)
        task = asyncio.create_task(reader.run())
        try:
            await asyncio.wait_for(reader.opened.wait(), 2)
            await until(lambda: session in app.state.broker.subscribed_session_ids())
            feed = app.state.runtime_feed
            focus = await client.put(f"/api/sessions/{session}/feed", json={"symbol": "SHFE.au2612", "durationSeconds": 300})
            assert focus.status_code == 200 and focus.json()["symbol"] == "SHFE.au2612"
            assert (await client.put(f"/api/sessions/{session}/feed", json={"symbol": "SHFE.au2612", "durationSeconds": 45})).status_code == 422
            await feed.tick()
            wanted = {"feed.account", "feed.execution", "feed.market", "feed.activity"}
            await until(lambda: wanted <= {item["name"] for item in reader.messages})
            names ={item["name"] for item in reader.messages}
            assert {"feed.account", "feed.execution", "feed.market", "feed.activity"} <= names
            market = next(item for item in reader.messages if item["name"] == "feed.market")
            assert market["payload"]["durationSeconds"] == 300 and market["payload"]["quote"]["symbol"].upper() == "SHFE.AU2612"
            account = next(item for item in reader.messages if item["name"] == "feed.account")
            assert set(account["payload"]) >= {"account", "positions", "activity", "risk"}
            # A queued UI command nudges every attached session of the user.
            await feed.publish_ui_nudge("local-user", "command")
            await until(lambda: any(item["name"] == "feed.ui" for item in reader.messages))
            assert any(item["name"] == "feed.ui" and item["payload"]["reason"] == "command" for item in reader.messages)
            # Feed events are transient: none of them is persisted as chat history.
            persisted = [event["name"] for _, event in app.state.store.list_events(session)]
            assert not any(name.startswith("feed.") for name in persisted)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await app.state.runtime_feed.stop()
            await app.state.shared_market.close()
            await app.state.runs.shutdown(); await app.state.adapter.shutdown(); await app.state.broker.shutdown()


async def test_a_client_that_applies_touches_gets_stamps_instead_of_the_unchanged_frame(monkeypatch):
    """feed.touch (2026-09-28): the 5 s refresh of an unchanged frame carries only its capture stamps
    to a session that asked for it; a changed frame, a reattached reader and older clients get full frames."""
    from trader_runtime.contracts import validate_event
    broker = FakeBroker({"s1", "s2"})
    reads = []
    async def account(account_id, user, raw):
        reads.append(1)
        return {"account": {"equity": 1, "ageSeconds": len(reads), "capturedAt": f"t{len(reads)}"}, "positions": [], "activity": raw, "risk": {}}
    feed = make_feed(broker, account=account)
    feed.set_focus("s1", symbol=None, duration=60, feed_touch=True)            # s2: an older client
    await feed.tick()
    clock = time.monotonic()
    monkeypatch.setattr(feed_module, "monotonic", lambda: clock + 6)
    await feed.tick()
    touches = [item for item in broker.published if item[0] == "s1" and item[2] == "feed.touch"]
    assert [item[3]["name"] for item in touches] == ["feed.account", "feed.execution"]
    stamps = touches[0][3]["stamps"]
    assert [path for path, _ in stamps] == [["account", "ageSeconds"], ["account", "capturedAt"]]
    assert touches[1][3]["stamps"] == []                                      # execution has no stamps
    assert [item[2] for item in broker.published if item[0] == "s2"].count("feed.account") == 2
    for item in touches:
        validate_event({"schemaVersion": 1, "id": "x", "sessionId": "s1", "kind": "result", "name": "feed.touch",
                        "timestamp": "2026-09-28T00:00:00Z", "payload": item[3]})
    # Business content changed: the full frame, never a touch.
    async def richer(account_id, user, raw):
        return {"account": {"equity": 2, "ageSeconds": 0, "capturedAt": "t9"}, "positions": [], "activity": raw, "risk": {}}
    feed._account_overview = richer
    monkeypatch.setattr(feed_module, "monotonic", lambda: clock + 8)
    before = len(broker.published)
    await feed.tick()
    assert ("s1", "feed.account") in [(item[0], item[2]) for item in broker.published[before:]]
    # A reattached reader starts empty: full frames again.
    broker.reattach("s1")
    monkeypatch.setattr(feed_module, "monotonic", lambda: clock + 20)
    before = len(broker.published)
    await feed.tick()
    assert {item[2] for item in broker.published[before:] if item[0] == "s1"} == {"feed.account", "feed.execution"}


def test_feed_payloads_validate_against_the_event_contract():
    from trader_runtime.contracts import validate_event
    validate_event({"schemaVersion": 1, "id": "x", "sessionId": "s", "kind": "result", "name": "feed.execution",
                    "timestamp": "2026-09-12T00:00:00Z", "payload": {"stops": [], "simulationOnly": True, "liveTrading": "disabled", "authorization": None}})
    validate_event({"schemaVersion": 1, "id": "x", "sessionId": "s", "kind": "result", "name": "feed.status",
                    "timestamp": "2026-09-12T00:00:00Z",
                    "payload": {"channel": "market", "status": "unavailable", "reason": "market_quote_not_ready"}})
    with pytest.raises(ValueError):
        validate_event({"schemaVersion": 1, "id": "x", "sessionId": "s", "kind": "result", "name": "feed.market",
                        "timestamp": "2026-09-12T00:00:00Z", "payload": {"quote": {}}})
    with pytest.raises(ValueError):
        validate_event({"schemaVersion": 1, "id": "x", "sessionId": "s", "kind": "result", "name": "feed.status",
                        "timestamp": "2026-09-12T00:00:00Z", "payload": {"channel": "market", "status": "ok"}})
    assert json.dumps({"ok": True})


class PushMarket:
    def __init__(self):
        from types import SimpleNamespace
        self.channel = SimpleNamespace(packet=None, changed=asyncio.Event(), error=None, refs=0)
        self.fail = False
        self.version = 0
        self.wait_first = asyncio.Event()
        self.wait_first.set()

    async def acquire_market(self, symbol, duration):
        if self.fail:
            raise RuntimeError("source unavailable")
        self.channel.refs += 1
        try:
            await self.wait_first.wait()
            return self.channel
        except BaseException:
            self.channel.refs -= 1
            raise

    def release_market(self, channel):
        channel.refs -= 1

    def push(self, symbol="SHFE.rb2610", price=1):
        self.version += 1
        self.channel.packet = {"id": str(self.version), "value": {"workbench": {
            "schemaVersion": 1, "durationSeconds": 60,
            "quote": {"symbol": symbol, "lastPrice": price}, "bars": [], "liveTrading": "disabled",
        }}}
        event, self.channel.changed = self.channel.changed, asyncio.Event()
        event.set()


async def until(predicate):
    async with asyncio.timeout(1.5):
        while not predicate():
            await asyncio.sleep(0.001)


async def test_shared_market_ignores_slow_private_reads_and_snapshot_gate():
    broker = FakeBroker({"s1", "s2"})
    source = PushMarket()
    private = asyncio.Event()
    async def slow(account):
        await private.wait()
        return {"orders": [], "trades": []}
    async def forbidden(*args):
        raise AssertionError("private market callback must never run")
    feed = make_feed(broker, raw_activity=slow, market=forbidden,
                     shared_market=lambda: source, snapshot_gated=True,
                     session_account=lambda session: session)
    for session in ("s1", "s2"):
        feed.set_focus(session, symbol="SHFE.rb2610", duration=60)
    await feed.start()
    try:
        await until(lambda: source.channel.refs == 2)
        for price in range(1, 6):
            start = time.monotonic()
            source.push(price=price)
            await until(lambda: len([row for row in broker.published if row[2] == "feed.market"]) == price * 2)
            assert time.monotonic() - start < .25
        assert not private.is_set()
    finally:
        await feed.stop()
    assert source.channel.refs == 0


async def test_shared_focus_disconnect_reconnect_account_switch_and_cancel_first_frame():
    broker = FakeBroker({"s1"})
    source = PushMarket()
    source.wait_first.clear()
    feed = make_feed(broker, shared_market=lambda: source)
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=60)
    await until(lambda: source.channel.refs == 1)
    feed.set_focus("s1", symbol="SHFE.au2612", duration=300)
    await asyncio.sleep(.01)
    assert source.channel.refs == 1
    source.push(symbol="SHFE.au2612")
    source.wait_first.set()
    await until(lambda: any(row[2] == "feed.market" for row in broker.published))
    assert all(row[3]["quote"]["symbol"] == "SHFE.au2612" for row in broker.published if row[2] == "feed.market")
    feed.session_account_changed("s1", "new-account")
    await asyncio.sleep(.01)
    assert source.channel.refs == 1
    broker.subscribed.clear()
    broker.on_subscribers_changed("s1")
    await until(lambda: source.channel.refs == 0)
    before = len(broker.published)
    broker.subscribed.add("s1")
    broker.reattach("s1")
    broker.on_subscribers_changed("s1")
    await until(lambda: len(broker.published) > before)
    feed.forget_session("s1")
    await until(lambda: source.channel.refs == 0)
    await feed.stop()


async def test_shared_first_failure_reports_market_status_then_recovers():
    broker = FakeBroker({"s1"})
    source = PushMarket()
    source.fail = True
    feed = make_feed(broker, shared_market=lambda: source)
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=60)
    try:
        await until(lambda: bool(broker.published))
        assert broker.published[-1][2:] == ("feed.status", {
            "channel": "market", "status": "unavailable", "reason": "market_unavailable"})
        source.fail = False
        source.push()
        await until(lambda: any(row[2] == "feed.market" for row in broker.published))
        source.channel.error = RuntimeError("disconnected")
        source.push(price=2)
        await until(lambda: broker.published[-1][2] == "feed.status")
        source.channel.error = None
        source.push(price=3)
        await until(lambda: broker.published[-1][2] == "feed.market")
    finally:
        await feed.stop()
    assert source.channel.refs == 0


async def test_market_burst_keeps_latest_without_evicting_other_events(tmp_path):
    from trader_runtime.broker import EventBroker
    from trader_runtime.store import RuntimeStore
    store = RuntimeStore(tmp_path / "latest-market.sqlite3")
    broker = EventBroker(store)
    session = broker.create_session("local", "native", "alice").session_id
    class Connected:
        async def is_disconnected(self):
            return False
    async def authorize():
        pass
    async def subscribed():
        await broker.publish_transient(session, kind="result", name="feed.ui", payload={"reason": "command"})
    stream = broker.stream(session, Connected(), authorize=authorize, on_subscribed=subscribed)
    try:
        assert 'feed.ui' in await anext(stream)
        await broker.publish(session, kind="message", name="message.completed", payload={
            "turnId": "test", "role": "assistant", "agentId": "native", "text": "reliable"})
        await broker.publish_transient(session, kind="result", name="feed.ui", payload={"reason": "keep"})
        source = PushMarket()
        for price in range(1000):
            source.push(price=price)
            await broker.publish_transient(session, kind="result", name="feed.market", payload=source.channel.packet["value"]["workbench"])
        assert len(broker.transient_events[session]) == 2
        assert 'reliable' in await anext(stream)
        assert 'keep' in await anext(stream)
        assert '999' in await anext(stream)
        # A newly attached reader's resnapshot remains visible to both streams.
        async def resnapshot():
            source.push(price=1000)
            await broker.publish_transient(session, kind="result", name="feed.market",
                                           payload=source.channel.packet["value"]["workbench"])
        second = broker.stream(session, Connected(), authorize=authorize, on_subscribed=resnapshot)
        try:
            assert 'reliable' in await anext(second)
            assert '1000' in await anext(second)
            assert '1000' in await anext(stream)
            assert broker.stream_generation(session) == 2
        finally:
            await second.aclose()
        assert broker.subscribers[session] == 1
        assert broker.latest_market[session][1]["payload"]["quote"]["lastPrice"] == 1000
    finally:
        await stream.aclose()
        store.close()
    assert session not in broker.latest_market


async def test_shared_source_burst_reaches_authorized_sse_while_private_read_waits(tmp_path):
    app = create_app(tmp_path / "push-sse.sqlite3", local_token=TOKEN, agent_mode="fake", data_mode="fake",
                     engineering_root=tmp_path / "project")
    source = PushMarket()
    app.state.shared_market = source
    private = asyncio.Event()
    async def slow(account):
        await private.wait()
        return {"orders": [], "trades": []}
    app.state.runtime_feed._account_activity = slow
    async with serving(app) as base, httpx.AsyncClient(base_url=base, headers={"Origin": ORIGIN}, trust_env=False) as client:
        assert (await client.post("/api/auth/local", headers={"authorization": f"Bearer {TOKEN}"})).status_code == 200
        client.headers["authorization"] = f"Bearer {TOKEN}"
        session = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
        reader = Reader(client, session)
        task = asyncio.create_task(reader.run())
        await app.state.runtime_feed.start()
        try:
            await asyncio.wait_for(reader.opened.wait(), 2)
            response = await client.put(f"/api/sessions/{session}/feed", json={"symbol": "SHFE.au2612", "durationSeconds": 60})
            assert response.status_code == 200
            await until(lambda: source.channel.refs == 1)
            for price in range(1, 5):
                start = time.monotonic()
                source.push(symbol="SHFE.au2612", price=price)
                await until(lambda: any(row.get("name") == "feed.market" and row["payload"]["quote"]["lastPrice"] == price for row in reader.messages))
                assert time.monotonic() - start < .25
            assert not private.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await app.state.runtime_feed.stop()
            await app.state.runs.shutdown()
            await app.state.adapter.shutdown()
            await app.state.broker.shutdown()
        assert source.channel.refs == 0


async def test_shared_packet_revision_controls_notifications():
    broker = FakeBroker({"s1"})
    source = PushMarket()
    source.push()
    feed = make_feed(broker, shared_market=lambda: source)
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=60)
    try:
        await until(lambda: len(broker.published) == 1)
        # Object replacement with the same source revision is not a new market frame.
        source.channel.packet = {**source.channel.packet}
        event, source.channel.changed = source.channel.changed, asyncio.Event()
        event.set()
        await asyncio.sleep(.02)
        assert len(broker.published) == 1
        source.push(price=2)
        await until(lambda: len(broker.published) == 2)
        assert broker.published[-1][3]["quote"]["lastPrice"] == 2
    finally:
        await feed.stop()


async def test_old_focus_acquisition_error_cannot_overwrite_new_focus_status():
    broker = FakeBroker({"s1"})
    source = PushMarket()
    source.push(symbol="SHFE.au2612")
    entered = asyncio.Event()
    acquire = source.acquire_market
    async def delayed(symbol, duration):
        if symbol == "SHFE.rb2610":
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("old source failed while closing") from None
        return await acquire(symbol, duration)
    source.acquire_market = delayed
    feed = make_feed(broker, shared_market=lambda: source)
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=60)
    try:
        await entered.wait()
        feed.set_focus("s1", symbol="SHFE.au2612", duration=60)
        await until(lambda: bool(broker.published))
        assert all(row[2] == "feed.market" and row[3]["quote"]["symbol"] == "SHFE.au2612" for row in broker.published)
    finally:
        await feed.stop()


async def test_market_failure_removes_queued_healthy_snapshot_for_slow_reader(tmp_path):
    from trader_runtime.broker import EventBroker
    from trader_runtime.store import RuntimeStore
    store = RuntimeStore(tmp_path / "market-failure.sqlite3")
    broker = EventBroker(store)
    session = broker.create_session("local", "native", "alice").session_id
    class Connected:
        async def is_disconnected(self):
            return False
    async def authorize():
        pass
    async def subscribed():
        await broker.publish_transient(session, kind="result", name="feed.ui", payload={"reason": "open"})
    stream = broker.stream(session, Connected(), authorize=authorize, on_subscribed=subscribed)
    try:
        await anext(stream)
        source = PushMarket()
        source.push()
        await broker.publish_transient(session, kind="result", name="feed.market", payload=source.channel.packet["value"]["workbench"])
        await broker.publish_transient(session, kind="result", name="feed.status", payload={"channel": "market", "status": "unavailable", "reason": "market_unavailable"})
        assert broker.latest_market[session][1]["name"] == "feed.status"
        assert 'feed.status' in await anext(stream)
        source.push(price=3)
        await broker.publish_transient(session, kind="result", name="feed.market", payload=source.channel.packet["value"]["workbench"])
        frame = await anext(stream)
        assert 'feed.market' in frame and '"lastPrice":3' in frame
        # A focus replacement discards an unread error from the old source too.
        await broker.publish_transient(session, kind="result", name="feed.status", payload={"channel": "market", "status": "unavailable", "reason": "market_unavailable"})
        broker.clear_market(session)
        source.push(symbol="SHFE.au2612", price=4)
        await broker.publish_transient(session, kind="result", name="feed.market", payload=source.channel.packet["value"]["workbench"])
        frame = await anext(stream)
        assert 'feed.status' not in frame and 'SHFE.au2612' in frame
    finally:
        await stream.aclose()
        store.close()


async def test_members_that_ask_for_patches_get_changes_not_the_whole_chart():
    """2026-09-27: every member received the full ~41 KB workbench on every market change.
    A session that asked for patches gets only what changed since the frame it holds; an
    older client (no marketPatches) keeps getting full frames; any gap falls back to full."""
    from trader_runtime.contracts import validate_event
    broker = FakeBroker({"s1", "s2"})
    source = PushMarket()
    feed = make_feed(broker, shared_market=lambda: source)

    def push(version, previous, price):
        source.version = version
        workbench = {"schemaVersion": 1, "durationSeconds": 60, "capturedAt": f"t{version}",
                     "quote": {"symbol": "SHFE.rb2610", "lastPrice": price}, "bars": [], "liveTrading": "disabled"}
        source.channel.packet = {"id": str(version), "previous": previous, "value": {"workbench": workbench},
                                 "patch": {"workbench": {"quote": workbench["quote"]}} if previous else None}
        event, source.channel.changed = source.channel.changed, asyncio.Event()
        event.set()

    def last(session):
        return [row for row in broker.published if row[0] == session][-1]

    push(1, None, 100)
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=60, market_patches=True)
    feed.set_focus("s2", symbol="SHFE.rb2610", duration=60)
    try:
        await until(lambda: len(broker.published) == 2)
        assert {row[2] for row in broker.published} == {"feed.market"}
        assert all(row[3]["revision"] == "1" for row in broker.published)
        push(2, "1", 101)
        await until(lambda: len(broker.published) == 4)
        patch = last("s1")
        assert patch[2] == "feed.market-patch"
        assert patch[3] == {"revision": "2", "base": "1", "durationSeconds": 60,
                            "changes": {"quote": {"symbol": "SHFE.rb2610", "lastPrice": 101}, "capturedAt": "t2"}}
        validate_event({"schemaVersion": 1, "id": "x", "sessionId": "s1", "kind": "result", "name": "feed.market-patch",
                        "timestamp": "2026-09-27T00:00:00Z", "payload": patch[3]})
        assert last("s2")[2] == "feed.market" and last("s2")[3]["quote"]["lastPrice"] == 101
        # The client does not hold packet 3's base: a full frame, never a patch on the wrong base.
        push(4, "3", 103)
        await until(lambda: len(broker.published) == 6)
        assert last("s1")[2] == "feed.market" and last("s1")[3]["revision"] == "4"
        push(5, "4", 104)
        await until(lambda: len(broker.published) == 8)
        assert last("s1")[2] == "feed.market-patch"
        # A reattached reader starts empty: full frame again.
        broker.reattach("s1")
        broker.on_subscribers_changed("s1")
        await until(lambda: last("s1")[2] == "feed.market" and last("s1")[3]["revision"] == "5")
    finally:
        await feed.stop()


async def test_streaming_sessions_are_not_re_read_from_the_store_on_every_wake(monkeypatch):
    """2026-10-03 load test: every site-hosted paper account's snapshot wakes a tick, and each tick
    and market watcher re-read every streaming session from the store — 100 accounts × 100
    sessions, the Runtime's top cost. One read per session a second; an ended session still stops."""
    broker = FakeBroker({"s1", "s2"})
    source = PushMarket()
    users = {"s1": "alice", "s2": "bob"}
    reads = []

    def lookup(session_id):
        reads.append(session_id)
        return users.get(session_id)
    feed = make_feed(broker, session_user=lookup, shared_market=lambda: source)
    for session in ("s1", "s2"):
        feed.set_focus(session, symbol="SHFE.rb2610", duration=60)
    await until(lambda: source.channel.refs == 2)
    try:
        reads.clear()
        for price in range(1, 21):
            await feed.tick()
            source.push(price=price)
            await asyncio.sleep(0)
        await until(lambda: len([row for row in broker.published if row[2] == "feed.market"]) >= 2)
        assert len(reads) <= 2
        # The session expired in the store: noticed once the reuse window is over.
        users.pop("s1")
        later = time.monotonic() + 2
        monkeypatch.setattr(feed_module, "monotonic", lambda: later)
        await feed.tick()
        await until(lambda: source.channel.refs == 1)
        # Forgetting a session drops its cached answer at once.
        reads.clear()
        feed.forget_session("s2")
        feed.set_focus("s2", symbol="SHFE.rb2610", duration=60)
        assert reads and set(reads) == {"s2"}
    finally:
        await feed.stop()


async def test_transient_ring_overflow_bumps_a_lagging_reader_once_for_a_full_resend(tmp_path):
    """#515 F-f：非行情瞬事环（256 格）真溢出时，落在读者身后的帧是静默丢的。滞后的读者得到
    一次代际提升（推送侧清空 last_sent 后全量重发），每个读者只提一次；环上还在的帧照常送达；
    没有溢出不提；读者全走后溢出记录清掉。"""
    from trader_runtime.broker import EventBroker
    from trader_runtime.store import RuntimeStore
    store = RuntimeStore(tmp_path / "ring-overflow.sqlite3")
    broker = EventBroker(store)
    session = broker.create_session("local", "native", "alice").session_id

    class Connected:
        async def is_disconnected(self):
            return False

    async def authorize():
        pass

    async def marker():
        await broker.publish_transient(session, kind="result", name="feed.ui", payload={"reason": "attach"})

    async def burst(count, first):
        for index in range(first, first + count):
            await broker.publish_transient(session, kind="result", name="feed.ui",
                                           payload={"reason": f"n{index}"})

    stream = broker.stream(session, Connected(), authorize=authorize, on_subscribed=marker)
    try:
        assert "attach" in await anext(stream)
        generation = broker.stream_generation(session)

        await burst(10, 0)
        for _ in range(10):
            await anext(stream)                 # 跟上：没有溢出，没有代际提升
        assert broker.stream_generation(session) == generation
        assert session not in broker.transient_evicted

        await burst(300, 10)                    # 环还差 45 帧才满 256：这批把头 55 帧挤出去了
        assert broker.transient_evicted[session] == 55
        lines = []
        for _ in range(300):
            lines.append(await anext(stream))   # 幸存帧从最老一帧起照常整批送达
            if "n309" in lines[-1]:
                break
        assert broker.stream_generation(session) == generation + 1   # 恢复读取的那一次提升
        assert "n54" in lines[0] and "n309" in lines[-1] and len(lines) == 256

        await burst(5, 310)                     # 又一批（继续溢出）：同一读者不再提第二次
        assert "n310" in await anext(stream)
        assert broker.stream_generation(session) == generation + 1
    finally:
        await stream.aclose()
        store.close()
    assert session not in broker.transient_events
    assert session not in broker.transient_sequences
    assert session not in broker.transient_evicted


async def test_a_resend_pushed_off_the_ring_again_is_asked_for_again(tmp_path):
    """#515：滞后读者拿到一次代际提升后仍没追上，推送侧发出的恢复全量帧又被后续突发挤出环——
    原来同一落后段只提一次，读者此后只收 touch，旧值被刷成“新鲜”。现在提升之后发出的帧再被挤掉，
    就再提一次；只丢了提升之前的帧不重复提。"""
    from trader_runtime.broker import EventBroker
    from trader_runtime.store import RuntimeStore
    store = RuntimeStore(tmp_path / "ring-resend.sqlite3")
    broker = EventBroker(store)
    session = broker.create_session("local", "native", "alice").session_id

    class Connected:
        async def is_disconnected(self):
            return False

    async def authorize():
        pass

    async def burst(count, first):
        for index in range(first, first + count):
            await broker.publish_transient(session, kind="result", name="feed.ui",
                                           payload={"reason": f"n{index}"})

    stream = broker.stream(session, Connected(), authorize=authorize, on_subscribed=lambda: burst(1, 0))
    try:
        assert "n0" in await anext(stream)
        generation = broker.stream_generation(session)
        await burst(300, 1)                     # 读者落后，环挤掉了它没读到的帧
        assert "n45" in await anext(stream)
        assert broker.stream_generation(session) == generation + 1
        # 读者逐帧读这一批时，推送侧发出恢复全量帧，又一阵突发把它挤出环。
        await broker.publish_transient(session, kind="result", name="feed.ui", payload={"reason": "full"})
        await burst(300, 301)
        for _ in range(255):
            await anext(stream)                 # 读完上一批（它在环里时取出的）
        assert broker.stream_generation(session) == generation + 1
        await anext(stream)                     # 下一批：恢复帧已丢，读者还在环头之后
        assert broker.stream_generation(session) == generation + 2
    finally:
        await stream.aclose()
        store.close()


async def test_shutting_the_broker_down_ends_open_readers_before_the_store_closes(tmp_path):
    """#530：回放关闭时子 app 先 broker.shutdown 再 store.close；已打开的读者醒来读已关闭的库，
    抛 sqlite3.ProgrammingError。现在 shutdown 唤醒并结束每个读者，结束后不再碰库。"""
    from trader_runtime.broker import EventBroker
    from trader_runtime.store import RuntimeStore
    store = RuntimeStore(tmp_path / "shutdown.sqlite3")
    broker = EventBroker(store)
    session = broker.create_session("local", "native", "alice").session_id

    class Connected:
        async def is_disconnected(self):
            return False

    async def authorize():
        pass

    async def first():
        await broker.publish_transient(session, kind="result", name="feed.ui", payload={"reason": "first"})

    stream = broker.stream(session, Connected(), authorize=authorize, on_subscribed=first)
    assert "first" in await anext(stream)
    waiting = asyncio.ensure_future(anext(stream))   # 读者停在等待里
    await asyncio.sleep(0)
    await broker.shutdown()
    store.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, 2)
    assert session not in broker.subscribers


async def test_a_tick_discards_a_round_whose_read_crossed_a_switch_or_rebuild():
    """#515 F-e：读是 await 的，读到一半会话换了账户、或状态对象被重建——旧账户的数字不能盖上
    “本会话最新”的章发出去。这一轮整轮丢弃（不写 read_version、不发布），下一轮按现状重读。"""
    broker = FakeBroker({"s1", "s2"})
    read_users = []
    mutated = set()

    async def overview(account_id, user, raw):
        read_users.append(user)
        if user not in mutated:
            mutated.add(user)
            if user == "alice":
                feed.session_account_changed("s1", "acct-b")   # s1 的读期间：会话换了账户
            else:
                feed._sessions.pop("s2", None)                 # s2 的读期间：状态对象被重建
        return {"account": {"user": user, "equity": 1}, "positions": [], "activity": raw, "risk": {}}

    feed = make_feed(broker, account=overview)
    assert await feed.tick() == 0
    assert broker.published == []
    assert set(read_users) == {"alice", "bob"}
    assert feed._sessions["s1"].account_id == "acct-b"
    assert feed._sessions["s1"].read_version == {} and feed._sessions["s1"].last_sent == {}
    # 下一轮按现状重读：两个会话各自照常发布。
    assert await feed.tick() == 4
    assert sorted((item[0], item[2]) for item in broker.published) == [
        ("s1", "feed.account"), ("s1", "feed.execution"), ("s2", "feed.account"), ("s2", "feed.execution")]


async def test_publish_only_stamps_last_sent_when_the_account_survived_the_send():
    """#515 F-e 的发送半边：发送本身也是 await 的，发着发着换了账户时帧照发（不能吞），但不能再
    记成本会话已发的最新一帧——否则下一轮同摘要就跳过，新账户的数字永远发不出去。"""
    class SwitchingBroker(FakeBroker):
        def __init__(self):
            super().__init__(set())
            self.feed = None
            self.switched = False

        async def publish_transient(self, session_id, *, kind, name, payload):
            if name == "feed.account" and not self.switched:
                self.switched = True
                self.feed.session_account_changed(session_id, "acct-b")
            self.published.append((session_id, kind, name, payload))

    broker = SwitchingBroker()
    feed = make_feed(broker)
    broker.feed = feed
    state = feed._state("s1")
    payload = {"account": {"user": "alice", "equity": 1}, "positions": [], "activity": {}, "risk": {}}
    assert await feed._publish("s1", state, "feed.account", payload, digest="A") == 1
    assert broker.published[0][2] == "feed.account"
    assert state.last_sent == {}
    # 账户稳定时照常记账：同一摘要下一轮就不重发。
    assert await feed._publish("s1", state, "feed.execution", {"user": "alice"}) == 1
    assert set(state.last_sent) == {"feed.execution"}
    assert await feed._publish("s1", state, "feed.execution", {"user": "alice"}) == 0

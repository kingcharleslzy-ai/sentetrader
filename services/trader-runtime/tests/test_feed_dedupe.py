"""扩容① (2026-10-13): what many sessions receive identically is built once, the SSE loop checks its caller
once a second instead of per event, and hot-path JSON goes through orjson with the stdlib's output."""
import asyncio
import json
import math
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_runtime_feed import PushMarket, make_feed, until
from trader_runtime import fast_json
from trader_runtime import shared_market as shared_module
from trader_runtime.broker import EventBroker
from trader_runtime.contracts import validate_event
from trader_runtime.shared_market import MarketPacket, SharedMarketService, bars_digest
from trader_runtime.store import RuntimeStore

FIXTURES = Path(__file__).resolve().parents[3] / "packages" / "ui-events" / "fixtures" / "events.v1.json"


class Connected:
    async def is_disconnected(self):
        return False


def counting_broker(tmp_path, sessions):
    broker = EventBroker(RuntimeStore(tmp_path / "dedupe.sqlite3"))
    for session in sessions:
        broker.subscribers[session] = 1  # an attached reader, as stream() counts it
    prepared = []
    original = broker.prepare_frame

    def prepare_frame(name, payload):
        prepared.append(name)
        return original(name, payload)

    broker.prepare_frame = prepare_frame
    return broker, prepared


def frame_text(broker, session, name):
    """The `data:` text the session's stream would send for its latest `name` event."""
    if name in {"feed.market", "feed.market-patch"}:
        _, event = broker.latest_market[session]
        prepared = broker.market_frames[session]
    else:
        _, event = [item for item in broker.transient_events[session] if item[1]["name"] == name][-1]
        prepared = event.prepared
    assert prepared is not None and prepared.payload is event["payload"]
    return prepared.encoded


def bar(index):
    return {"datetime": 1_791_800_000_000_000_000 + index * 60_000_000_000, "open": 1.0 + index, "high": 2.0 + index,
            "low": 0.5 + index, "close": 1.5 + index, "volume": 10 + index, "openInterest": 100, "tradingDay": "2026-10-13"}


def push(source, version, previous, price, bars):
    source.version = version
    workbench = {"schemaVersion": 1, "durationSeconds": 60, "capturedAt": f"t{version}",
                 "quote": {"symbol": "SHFE.rb2610", "lastPrice": price}, "bars": bars, "liveTrading": "disabled"}
    source.channel.packet = MarketPacket(id=str(version), previous=previous, value={"workbench": workbench},
                                         patch={"workbench": {"quote": workbench["quote"]}} if previous else None)
    event, source.channel.changed = source.channel.changed, asyncio.Event()
    event.set()


async def test_one_market_patch_per_revision_for_every_session_on_the_chart(tmp_path):
    sessions = ["s1", "s2", "s3"]
    broker, prepared = counting_broker(tmp_path, sessions)
    source = PushMarket()
    feed = make_feed(broker, shared_market=lambda: source, session_user=lambda session: "alice")
    push(source, 1, None, 100, [bar(0), bar(1)])
    for session in sessions:
        feed.set_focus(session, symbol="SHFE.rb2610", duration=60, market_patches=True)
    try:
        await until(lambda: all(broker.latest_market.get(s, (0, {}))[1].get("name") == "feed.market" for s in sessions))
        assert prepared.count("feed.market") == 1  # one member frame per packet, not one per session
        prepared.clear()
        push(source, 2, "1", 101, [bar(0), bar(1)])
        await until(lambda: all(broker.latest_market[s][1]["name"] == "feed.market-patch" for s in sessions))
        assert prepared == ["feed.market-patch"]  # built, validated and encoded once
        texts = {frame_text(broker, s, "feed.market-patch") for s in sessions}
        assert len(texts) == 1
        assert json.loads(texts.pop()) == {"revision": "2", "base": "1", "durationSeconds": 60, "changes": {
            "quote": {"symbol": "SHFE.rb2610", "lastPrice": 101}, "capturedAt": "t2"}}
        assert len({id(broker.market_frames[s]) for s in sessions}) == 1
    finally:
        await feed.stop()
        broker.store.close()


async def test_account_activity_and_execution_frames_are_digested_and_encoded_once_per_tick(tmp_path, monkeypatch):
    sessions = ["s1", "s2", "s3", "s4"]
    broker, prepared = counting_broker(tmp_path, sessions)
    digests = []
    original_digest = __import__("trader_runtime.runtime_feed", fromlist=["_digest"])._digest

    def digest(payload):
        digests.append(payload)
        return original_digest(payload)

    monkeypatch.setattr("trader_runtime.runtime_feed._digest", digest)
    feed = make_feed(broker, session_user=lambda session: "alice")
    for session in sessions:
        feed.set_focus(session, symbol="SHFE.rb2610", duration=60, feed_touch=True)
    assert await feed.tick() == 16  # account, execution, activity and (no shared market here) the chart, to four sessions
    assert sorted(prepared) == ["feed.account", "feed.activity", "feed.execution", "feed.market"]
    assert len(digests) == 4
    for name in ("feed.account", "feed.activity", "feed.execution", "feed.market"):
        assert len({frame_text(broker, s, name) for s in sessions}) == 1
    # Unchanged within the refresh window: nothing is read into a frame again, nothing is sent.
    assert await feed.tick() == 0


async def test_a_focus_with_since_gets_only_the_newer_bars_and_the_window_proof(tmp_path):
    broker, prepared = counting_broker(tmp_path, ["s1", "s2", "s3"])
    source = PushMarket()
    feed = make_feed(broker, shared_market=lambda: source, session_user=lambda session: "alice")
    bars = [bar(i) for i in range(10)]
    push(source, 1, None, 100, bars)
    held = bars[7]["datetime"]
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=60, market_patches=True, since=held)
    feed.set_focus("s2", symbol="SHFE.rb2610", duration=60, market_patches=True, since=held)
    feed.set_focus("s3", symbol="SHFE.rb2610", duration=60, market_patches=True)  # holds nothing
    try:
        await until(lambda: all(s in broker.latest_market for s in ("s1", "s2", "s3")))
        first = json.loads(frame_text(broker, "s1", "feed.market"))
        assert [item["datetime"] for item in first["bars"]] == [bars[8]["datetime"], bars[9]["datetime"]]
        assert first["barsAfter"] == held and first["revision"] == "1"
        assert first["barsWindow"] == {"first": bars[0]["datetime"], "count": 10, "digest": bars_digest(bars[:8])}
        assert broker.market_frames["s1"] is broker.market_frames["s2"]  # one answer per packet and since
        whole = json.loads(frame_text(broker, "s3", "feed.market"))
        assert len(whole["bars"]) == 10 and "barsAfter" not in whole
        assert prepared.count("feed.market") == 2
        # #850: the hint answered the first frame only — a restarted watcher (a reattached reader)
        # sends the whole window, which that reader may need.
        assert feed.focus("s2").since is None
        broker.latest_market.pop("s2")
        feed._restart_market("s2")
        await until(lambda: "s2" in broker.latest_market)
        again = json.loads(frame_text(broker, "s2", "feed.market"))
        assert len(again["bars"]) == 10 and "barsAfter" not in again
        validate_event({"schemaVersion": 1, "id": "x", "sessionId": "s1", "kind": "result", "name": "feed.market",
                        "timestamp": "2026-10-13T00:00:00Z", "payload": first})
        # #850: the proof is part of the contract — half of it, or a malformed window, does not validate.
        for broken in ({k: v for k, v in first.items() if k != "barsWindow"},
                       {**first, "barsWindow": {**first["barsWindow"], "count": 0}},
                       {**first, "barsWindow": {"first": bars[0]["datetime"], "count": 10}}):
            with pytest.raises(ValueError, match="invalid_event_payload"):
                validate_event({"schemaVersion": 1, "id": "x", "sessionId": "s1", "kind": "result",
                                "name": "feed.market", "timestamp": "2026-10-13T00:00:00Z", "payload": broken})
        # The next change is an ordinary patch on that revision.
        push(source, 2, "1", 101, bars)
        await until(lambda: broker.latest_market["s1"][1]["name"] == "feed.market-patch")
        assert json.loads(frame_text(broker, "s1", "feed.market-patch"))["base"] == "1"
        # A since the window no longer holds answers the whole window.
        feed.set_focus("s1", symbol="SHFE.rb2610", duration=60, market_patches=True, since=1)
        await until(lambda: broker.latest_market.get("s1", (0, {"name": None}))[1]["name"] == "feed.market"
                    and broker.latest_market["s1"][1]["payload"].get("revision") == "2")
        assert "barsAfter" not in json.loads(frame_text(broker, "s1", "feed.market"))
    finally:
        await feed.stop()
        broker.store.close()


def test_since_answers_per_packet_are_capped():

    from trader_runtime.runtime_feed import SINCE_ANSWERS_PER_PACKET, RuntimeFeed
    bars = [bar(i) for i in range(SINCE_ANSWERS_PER_PACKET + 10)]
    payload = {"bars": bars, "revision": "1"}
    frame = SimpleNamespace(payload=payload)
    packet: dict = {}
    prepared = []

    def prepare(answer):
        prepared.append(answer)
        return SimpleNamespace(payload=answer)

    for item in bars[:-1]:
        RuntimeFeed._since_frame(packet, frame, item["datetime"], prepare)
    assert len(packet["member_since"]) == SINCE_ANSWERS_PER_PACKET  # a burst of distinct values stays bounded
    # A since outside the window answers the shared frame and is not cached.
    assert RuntimeFeed._since_frame(packet, frame, 1, prepare) is frame
    assert 1 not in packet["member_since"]
    # A cached value is answered from the cache.
    count = len(prepared)
    RuntimeFeed._since_frame(packet, frame, bars[0]["datetime"], prepare)
    assert len(prepared) == count


async def test_an_unencodable_frame_still_fails_alone_not_the_whole_tick(tmp_path):
    from decimal import Decimal
    broker, prepared = counting_broker(tmp_path, ["s1", "s2"])
    feed = make_feed(broker, session_user=lambda session: "alice",
                     execution=lambda account_id, user: asyncio.sleep(0, {"stops": [], "simulationOnly": True,
                                                                          "liveTrading": "disabled", "cap": Decimal("1.5")}))
    assert await feed.tick() == 4  # account and execution to both sessions
    assert prepared.count("feed.account") == 1
    # The execution frame goes out unprepared, so the stream drops just that one (as before).
    assert all(not getattr(item[1], "prepared", None) for s in ("s1", "s2")
               for item in broker.transient_events[s] if item[1]["name"] == "feed.execution")
    broker.store.close()


def test_a_published_packet_is_encoded_for_guests_only_when_a_guest_reads_it(monkeypatch):
    encodes = []
    original = shared_module.event
    monkeypatch.setattr(shared_module, "event", lambda name, value: encodes.append(name) or original(name, value))
    service = SimpleNamespace(clock=lambda: 0.0)
    channel = SimpleNamespace(packet=None, notify=lambda: None)
    current = {"quote": {"symbol": "SHFE.rb2610", "lastPrice": 1}, "workbench": {"quote": {}, "bars": [bar(0)]}}
    SharedMarketService.publish_market(service, channel, current)
    changed = {"quote": {"symbol": "SHFE.rb2610", "lastPrice": 2}, "workbench": {"quote": {}, "bars": [bar(0)]}}
    SharedMarketService.publish_market(service, channel, changed)
    assert encodes == []  # members never read these texts
    packet = channel.packet
    assert packet["update"].startswith("event: update\n")
    assert packet["update"] is packet["update"]
    assert encodes == ["update"]
    assert json.loads(packet["snapshot"].split("data: ", 1)[1]) == changed
    assert encodes == ["update", "snapshot"]
    assert "member_frame" not in packet and packet.get("member_frame") is None


async def test_the_stream_checks_its_caller_once_per_second_and_reads_the_events_table_only_on_delivery(tmp_path):
    broker = EventBroker(RuntimeStore(tmp_path / "recheck.sqlite3"))
    session = broker.create_session("local", "native", "alice").session_id
    checks = []
    allowed = [True]

    async def authorize():
        checks.append(time.monotonic())
        if not allowed[0]:
            from fastapi import HTTPException
            raise HTTPException(401, "session_auth_revoked")

    reads = []
    original = broker.store.list_events
    broker.store.list_events = lambda *args, **kwargs: reads.append(1) or original(*args, **kwargs)
    stream = broker.stream(session, Connected(), authorize=authorize)
    frames = []

    async def read():
        async for frame in stream:
            frames.append(frame)

    reader = asyncio.create_task(read())
    try:
        await until(lambda: broker.subscribers.get(session) == 1)
        for index in range(300):
            await broker.publish_transient(session, kind="result", name="feed.ui", payload={"reason": f"r{index}"})
            if index % 30 == 0:
                await asyncio.sleep(0.01)
        await until(lambda: sum('"feed.ui"' in frame for frame in frames) == 300)
        assert len(checks) <= 2  # was one per event: 300+
        assert len(reads) <= 3  # nothing persisted: the table is not read per wake
        await broker.publish(session, kind="message", name="message.completed",
                             payload={"turnId": "t", "role": "assistant", "text": "stored"})
        await until(lambda: any("stored" in frame for frame in frames))  # a delivery is read at once
        # Revoked: the stream ends within the recheck second, also while events keep coming.
        allowed[0] = False
        revoked = time.monotonic()
        while not reader.done() and time.monotonic() - revoked < 3:
            await broker.publish_transient(session, kind="result", name="feed.ui", payload={"reason": "busy"})
            await asyncio.sleep(0.05)
        assert reader.done()
        assert time.monotonic() - revoked <= broker.recheck_seconds + 0.3
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        broker.store.close()


async def test_an_idle_revoked_stream_ends_within_the_recheck_delay(tmp_path):
    broker = EventBroker(RuntimeStore(tmp_path / "idle.sqlite3"))
    session = broker.create_session("local", "native", "alice").session_id
    allowed = [True]

    async def authorize():
        if not allowed[0]:
            from fastapi import HTTPException
            raise HTTPException(403, "session_owner_mismatch")

    frames = []

    async def read():
        async for frame in broker.stream(session, Connected(), authorize=authorize):
            frames.append(frame)

    reader = asyncio.create_task(read())
    try:
        await asyncio.sleep(0.5)
        allowed[0] = False
        revoked = time.monotonic()
        await asyncio.wait_for(reader, broker.recheck_seconds + 1)
        assert time.monotonic() - revoked <= broker.recheck_seconds + 0.3
        assert all(frame.startswith("event: heartbeat") for frame in frames)
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        broker.store.close()


def test_fast_json_writes_what_the_stdlib_wrote():
    def stdlib(value, **options):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), **options)

    events = json.loads(FIXTURES.read_text(encoding="utf-8"))
    assert events
    for event in events:
        assert fast_json.dumps(event) == stdlib(event)  # the shared contract fixtures, byte for byte
        assert fast_json.loads(fast_json.dumps(event).encode()) == event
    sample = {"文字": "螺纹钢 rb2610", "z": 1, "a": [0.1 + 0.2, 3087.5, -0.0, 1e16, 1791800000000000000, None, True],
              "nested": {"b": 1, "a": 2}}
    assert fast_json.dumps(sample) == stdlib(sample)  # key order, Chinese, floats
    assert fast_json.dumps(sample, sort_keys=True) == stdlib(sample, sort_keys=True)
    # What orjson refuses falls back to the stdlib's exact text.
    for value in ({1: "a", "b": 2}, {True: 1, None: 2}, {"big": 2**70}):
        assert fast_json.dumps(value) == stdlib(value)
    # What the stdlib refuses stays refused (the stream drops that one transient), unless `default` says how.
    for value in ({"price": __import__("decimal").Decimal("1.5")}, {"s": {1, 2}}):
        with pytest.raises(TypeError):
            fast_json.dumps(value)
    assert fast_json.dumps({"d": __import__("datetime").date(2026, 10, 13)}, sort_keys=True, default=str) == '{"d":"2026-10-13"}'
    # Same value after parsing even where the text differs: exponents, and NaN (now valid JSON).
    assert json.loads(fast_json.dumps({"x": 1e-7})) == {"x": 1e-7}
    assert fast_json.dumps({"x": math.nan}) == '{"x":null}'
    # Decoding: orjson first, the stdlib for what it refuses (NaN tokens, a BOM).
    assert fast_json.loads(b'{"a":"\xe4\xb8\xad","b":[1,2.5]}') == {"a": "中", "b": [1, 2.5]}
    assert math.isnan(fast_json.loads(b'{"a":NaN}')["a"])
    with pytest.raises(ValueError):
        fast_json.loads(b"not json")

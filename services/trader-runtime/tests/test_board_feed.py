"""报价总表 push and "only what is seen" (2026-10-12): one board read and one diff per round shared by
every window, each row filter built once, nothing to a hidden page, full frames on return, and the
private channels never paused."""
import asyncio
import time

import httpx

from test_runtime_feed import FakeBroker, PushMarket, make_feed, until
from test_stream_revocation import ORIGIN, Reader, serving
from trader_runtime import runtime_feed as feed_module
from trader_runtime.contracts import validate_event
from trader_runtime.main import create_app
from trader_runtime.runtime_feed import BoardWant

TOKEN = "board-feed-token"


def row(symbol, exchange, product, last, **extra):
    return {"symbol": symbol, "exchangeId": exchange, "productId": product, "last": last, "volume": 1,
            "quoteTime": "21:00:00", **extra}


class Board:
    """The shared market's board() as the feed sees it: one dict per read, counted."""

    def __init__(self):
        self.rows = [row("SHFE.au2612", "SHFE", "au", 800.0), row("SHFE.rb2601", "SHFE", "rb", 3100.0),
                     row("DCE.i2601", "DCE", "i", 780.0)]
        self.reads = 0
        self.fail: str | None = None
        self.stamp = 0

    async def board(self):
        self.reads += 1
        if self.fail is not None:
            from fastapi import HTTPException
            raise HTTPException(503, self.fail)
        self.stamp += 1
        return {"schemaVersion": 1, "capturedAt": f"2026-10-12T13:00:{self.stamp:02d}Z", "dataStatus": "normal",
                "revision": f"r{self.stamp}", "truncated": False, "rows": [dict(item) for item in self.rows]}

    def set(self, symbol, **fields):
        for item in self.rows:
            if item["symbol"] == symbol:
                item.update(fields)


def sent(broker, session=None, name=None):
    return [item for item in broker.published
            if (session is None or item[0] == session) and (name is None or item[2] == name)]


def contract_ok(name, payload):
    validate_event({"schemaVersion": 1, "id": "x", "sessionId": "s", "kind": "result", "name": name,
                    "timestamp": "2026-10-12T00:00:00Z", "payload": payload})


async def test_first_frame_is_full_then_only_changed_fields_one_read_for_all_windows():
    broker = FakeBroker({"s1", "s2"})
    board = Board()
    feed = make_feed(broker, shared_market=lambda: board)
    everything = BoardWant.of(everything=True, key="all")
    feed.set_view("s1", visible=True, board=everything)
    feed.set_view("s2", visible=True, board=everything)
    assert await feed.board_round() == 2
    assert board.reads == 1  # one read serves every window
    full = sent(broker, name="feed.board")
    assert len(full) == 2 and full[0][3] is full[1][3]  # one frame built for both
    assert [item["symbol"] for item in full[0][3]["rows"]] == ["SHFE.au2612", "SHFE.rb2601", "DCE.i2601"]
    assert full[0][3]["key"] == "all" and full[0][3]["dataStatus"] == "normal"
    contract_ok("feed.board", full[0][3])
    revision = full[0][3]["feedRevision"]

    # Same business content (only stamps moved): nothing is sent inside the refresh interval.
    assert await feed.board_round() == 0
    board.set("SHFE.rb2601", last=3101.0, volume=7)
    assert await feed.board_round() == 2
    patches = sent(broker, name="feed.board-patch")
    assert len(patches) == 2 and patches[0][3] is patches[1][3]
    patch = patches[0][3]
    assert patch["base"] == revision and patch["feedRevision"] != revision
    assert patch["rows"] == {"SHFE.rb2601": {"last": 3101.0, "volume": 7}}
    assert patch["meta"]["capturedAt"].endswith(":03Z")  # the stamps ride on every patch
    contract_ok("feed.board-patch", patch)


async def test_a_filtered_window_gets_only_its_rows_in_full_and_patch():
    broker = FakeBroker({"s1", "s2"})
    board = Board()
    feed = make_feed(broker, shared_market=lambda: board)
    feed.set_view("s1", visible=True, board=BoardWant.of(exchanges=["dce"], key="x:dce"))
    feed.set_view("s2", visible=True, board=BoardWant.of(products=["SHFE.au"], symbols=[], key="p:au"))
    await feed.board_round()
    s1 = sent(broker, "s1", "feed.board")[-1][3]
    s2 = sent(broker, "s2", "feed.board")[-1][3]
    assert [item["symbol"] for item in s1["rows"]] == ["DCE.i2601"] and s1["key"] == "x:dce"
    assert [item["symbol"] for item in s2["rows"]] == ["SHFE.au2612"]
    board.set("SHFE.rb2601", last=1.0)
    board.set("DCE.i2601", last=2.0)
    await feed.board_round()
    assert sent(broker, "s1", "feed.board-patch")[-1][3]["rows"] == {"DCE.i2601": {"last": 2.0}}
    # A change outside a window's rows still moves its revision on (an empty patch keeps the chain).
    assert sent(broker, "s2", "feed.board-patch")[-1][3]["rows"] == {}


async def test_rows_added_or_reordered_send_full_frames_and_a_failure_reports_then_recovers():
    broker = FakeBroker({"s1"})
    board = Board()
    feed = make_feed(broker, shared_market=lambda: board)
    feed.set_view("s1", visible=True, board=BoardWant.of(everything=True))
    await feed.board_round()
    board.rows.append(row("CFFEX.IF2612", "CFFEX", "IF", 3900.0))
    await feed.board_round()
    assert [item[2] for item in broker.published] == ["feed.board", "feed.board"]
    board.fail = "guest_market_source_unavailable"
    await feed.board_round()
    assert broker.published[-1][2:] == ("feed.status", {"channel": "board", "status": "unavailable",
                                                         "reason": "guest_market_source_unavailable"})
    contract_ok("feed.status", broker.published[-1][3])
    assert await feed.board_round() == 0  # the same unavailability is reported once
    board.fail = None
    await feed.board_round()
    assert broker.published[-1][2] == "feed.board"  # recovery starts from a full frame


async def test_a_quiet_board_is_refreshed_and_a_reattached_reader_starts_full(monkeypatch):
    broker = FakeBroker({"s1"})
    board = Board()
    feed = make_feed(broker, shared_market=lambda: board)
    feed.set_view("s1", visible=True, board=BoardWant.of(everything=True))
    await feed.board_round()
    clock = time.monotonic()
    monkeypatch.setattr(feed_module, "monotonic", lambda: clock + feed_module.REFRESH_SECONDS + 1)
    await feed.board_round()
    refresh = broker.published[-1][3]
    assert broker.published[-1][2] == "feed.board-patch" and refresh["base"] == refresh["feedRevision"]
    assert refresh["rows"] == {} and "capturedAt" in refresh["meta"]
    broker.reattach("s1")
    await feed.board_round()
    assert broker.published[-1][2] == "feed.board"


async def test_a_resent_view_is_a_resync_and_no_board_goes_to_windows_without_it():
    broker = FakeBroker({"s1", "s2"})
    board = Board()
    feed = make_feed(broker, shared_market=lambda: board)
    feed.set_view("s1", visible=True, board=BoardWant.of(everything=True))
    feed.set_view("s2", visible=True, board=None)
    await feed.board_round()
    feed.set_view("s1", visible=True, board=BoardWant.of(everything=True))
    await feed.board_round()
    assert [item[2] for item in sent(broker, "s1")] == ["feed.board", "feed.board"]
    assert not sent(broker, "s2")


async def test_a_hidden_page_pauses_board_and_chart_but_keeps_private_channels():
    broker = FakeBroker({"s1"})
    board = Board()
    source = PushMarket()
    both = _Both(board, source)
    feed = make_feed(broker, shared_market=lambda: both)
    feed.set_focus("s1", symbol="SHFE.rb2610", duration=60, market_patches=True)
    feed.set_view("s1", visible=True, board=BoardWant.of(everything=True))
    await until(lambda: source.channel.refs == 1)
    source.push(price=1)
    await until(lambda: bool(sent(broker, "s1", "feed.market")))
    await feed.board_round()

    feed.set_view("s1", visible=False, board=BoardWant.of(everything=True))
    await until(lambda: source.channel.refs == 0)  # the chart's shared series is let go
    before = len(broker.published)
    board.set("SHFE.au2612", last=801.0)
    source.push(price=2)
    assert await feed.board_round() == 0
    await asyncio.sleep(0.02)
    assert not [item for item in broker.published[before:] if item[2] in {"feed.market", "feed.market-patch", "feed.board", "feed.board-patch"}]
    # Account and execution (orders, fills, protection state) keep flowing to the hidden page.
    await feed.tick()
    assert {item[2] for item in broker.published[before:]} >= {"feed.account", "feed.execution"}

    feed.set_view("s1", visible=True, board=BoardWant.of(everything=True))
    await until(lambda: source.channel.refs == 1)
    await until(lambda: len(sent(broker, "s1", "feed.market")) == 2)  # a full chart, not a patch
    assert sent(broker, "s1", "feed.market")[-1][3]["quote"]["lastPrice"] == 2
    await feed.board_round()
    last = sent(broker, "s1", "feed.board")[-1][3]
    assert next(item for item in last["rows"] if item["symbol"] == "SHFE.au2612")["last"] == 801.0
    await feed.stop()


class _Both:
    """The shared market with both the chart channel and the board."""

    def __init__(self, board, market):
        self._board, self._market = board, market

    async def board(self):
        return await self._board.board()

    async def acquire_market(self, symbol, duration):
        return await self._market.acquire_market(symbol, duration)

    def release_market(self, channel):
        self._market.release_market(channel)


async def test_view_route_streams_the_board_once_encoded_and_guards_its_input(tmp_path):
    app = create_app(tmp_path / "board.sqlite3", local_token=TOKEN, agent_mode="fake", data_mode="fake",
                     engineering_root=tmp_path / "project")
    async with serving(app) as base, httpx.AsyncClient(base_url=base, headers={"Origin": ORIGIN}, trust_env=False) as client:
        assert (await client.post("/api/auth/local", headers={"authorization": f"Bearer {TOKEN}"})).status_code == 200
        client.headers["authorization"] = f"Bearer {TOKEN}"
        session = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
        reader = Reader(client, session)
        task = asyncio.create_task(reader.run())
        try:
            await asyncio.wait_for(reader.opened.wait(), 2)
            view = await client.put(f"/api/sessions/{session}/feed/view",
                                    json={"visible": True, "board": {"all": True, "key": "all"}})
            assert view.status_code == 200 and view.json() == {"sessionId": session, "visible": True, "board": True}
            assert await app.state.runtime_feed.board_round() == 1  # the test server runs no feed loop
            await until_async(lambda: any(item.get("name") == "feed.board" for item in reader.messages))
            frame = next(item for item in reader.messages if item.get("name") == "feed.board")["payload"]
            assert frame["key"] == "all" and frame["rows"] and {"symbol", "last", "exchangeId"} <= set(frame["rows"][0])
            bad = await client.put(f"/api/sessions/{session}/feed/view", json={"board": {"exchanges": ["SHFE;drop"]}})
            assert bad.status_code == 422
            missing = await client.put("/api/sessions/nope/feed/view", json={"visible": False})
            assert missing.status_code == 404
            hidden = await client.put(f"/api/sessions/{session}/feed/view", json={"visible": False, "board": {"all": True}})
            assert hidden.json()["visible"] is False
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await app.state.runtime_feed.stop()
            await app.state.shared_market.close()
            await app.state.runs.shutdown(); await app.state.adapter.shutdown(); await app.state.broker.shutdown()


async def until_async(predicate, seconds=3.0):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.02)

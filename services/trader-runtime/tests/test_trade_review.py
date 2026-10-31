"""Account-wide trade review over persisted fills, and the active order filter."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient

from test_ai_annotations import with_tools
from test_simulation_orders import USER, Gateway, application, confirm, proposal
from test_strategy_protection import Provider
from trader_runtime.store import RuntimeStore

AU, RB = "SHFE.au2612", "SHFE.rb2701"


def at(day: str, hour: int) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00:00+08:00").timestamp() * 1e9)


def fill(trade_id, symbol, direction, offset, price, day, hour, volume=1):
    return {"tradeId": trade_id, "clientOrderId": f"order-{trade_id}", "symbol": symbol, "direction": direction,
            "offset": offset, "price": price, "volume": volume, "tradeDateTime": at(day, hour), "tradingDay": day}


async def login(http: AsyncClient) -> AsyncClient:
    response = await http.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})
    assert response.status_code == 200
    return http


def client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://127.0.0.1:5173"})


async def test_review_pairs_yesterday_open_with_today_close_across_symbols(tmp_path):
    provider = Provider()
    app = application(tmp_path / "review.sqlite3", Gateway(), provider)
    now_local = datetime.now(UTC).astimezone()
    today = now_local.strftime("%Y-%m-%d")
    yesterday = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        async with client(app) as http:
            await login(http)
            # Day one: the sidecar only reports that day's fills. Reading the review persists them.
            provider.activity["trades"] = [fill("au-open", AU, "BUY", "OPEN", 820.0, yesterday, 10),
                                           fill("rb-open", RB, "SELL", "OPEN", 3200.0, yesterday, 11)]
            first = (await http.get("/api/trade-review")).json()
            assert first["trades"] == [] and first["unmatched"] == []
            assert {row[0] for row in app.state.store.connection.execute("SELECT trade_id FROM account_trades")} == {"au-open", "rb-open"}
            # Day two: yesterday's fills are gone from the provider; the store still knows them.
            provider.activity["trades"] = [fill("au-close", AU, "SELL", "CLOSE", 821.0, today, 10),
                                           fill("rb-close", RB, "BUY", "CLOSE", 3190.0, today, 14)]
            review = (await http.get("/api/trade-review")).json()
            assert [(t["symbol"], t["direction"], t["openPrice"], t["closePrice"]) for t in review["trades"]] == [
                (AU, "long", 820.0, 821.0), (RB, "short", 3200.0, 3190.0)]
            assert [t["gross"] for t in review["trades"]] == [1000.0, 10000.0]
            assert review["unmatched"] == []
            # No custom fee schedule: gross only, never a guessed net.
            assert all(t["fees"] is None and t["net"] is None for t in review["trades"])
            assert review["estimatedNet"] is None
            # A repeated read does not duplicate the round trips.
            again = (await http.get("/api/trade-review")).json()
            assert [t["id"] for t in again["trades"]] == [t["id"] for t in review["trades"]]
            # The symbol filter keeps the old single-contract contract.
            only = (await http.get("/api/trade-review", params={"symbol": RB})).json()
            assert [t["symbol"] for t in only["trades"]] == [RB]
            # The window keeps whole round trips by close time; the open may be older than the window.
            windowed = (await http.get("/api/trade-review", params={"days": 1})).json()
            assert len(windowed["trades"]) == 2
            # The provider stops serving RB (expired): its stored multiple keeps the round trip.
            original = provider.market_snapshot
            def without_rb(turn, symbol):
                if symbol == RB:
                    raise KeyError(symbol)
                return original(turn, symbol)
            provider.market_snapshot = without_rb
            still = (await http.get("/api/trade-review")).json()
            assert [t["symbol"] for t in still["trades"]] == [AU, RB] and "skippedSymbols" not in still
            # A fill whose multiple was never known is reported, not fatal.
            provider.activity["trades"] = [fill("cu-open", "SHFE.cu2612", "BUY", "OPEN", 70000.0, today, 9),
                                           fill("cu-close", "SHFE.cu2612", "SELL", "CLOSETODAY", 70010.0, today, 10)]
            provider.market_snapshot = lambda turn, symbol: (_ for _ in ()).throw(KeyError(symbol)) if symbol != AU else original(turn, symbol)
            partial = (await http.get("/api/trade-review")).json()
            assert [t["symbol"] for t in partial["trades"]] == [AU, RB]
            assert partial["skippedSymbols"] == ["SHFE.cu2612"]
            assert (await http.get("/api/trade-review", params={"symbol": "SHFE.cu2612"})).status_code == 409
    finally:
        app.state.store.close()


def order(trade_id, client_order_id, direction, offset, price, day, hour):
    return {**fill(trade_id, RB, direction, offset, price, day, hour), "clientOrderId": client_order_id}


def seed_order_sources(app) -> None:
    """The AI's own order and a deterministic stop (proposal automationSource), and a strategy
    execution attempt whose order the strategy placed."""
    store = app.state.store
    session = app.state.broker.create_session("local", "native", USER)
    now = datetime.now(UTC).isoformat()
    for proposal_id, client_order_id, extra in (
            ("p-ai", "cli-ai", {"automationSource": "ai_autonomous", "reason": "突破前高，自主开多"}),
            ("p-stop", "cli-stop", {"automationSource": "deterministic_stop", "reason": "止损触发"}),
            # A card the conversation's AI proposed and the user confirmed: 「AI 建议」 with its reason.
            ("p-card", "cli-card", {"origin": "ai", "reason": "回踩均线企稳，建议试多 1 手"}),
            # The ticket's card (REST route, no origin): 手动 even though it carries a reason.
            ("p-manual", "cli-manual", {"reason": "用户在三键交易台明确授权一张手工模拟委托。"})):
        store.create(proposal_id=proposal_id, session_id=session.session_id, proposal_type="trade", status="executed",
                     payload={"symbol": RB, **extra}, user_id=USER)
        store.connection.execute("""INSERT INTO tqkq_sim_order_links (id, user_id, proposal_id, client_order_id, request_json,
            submit_phase, cancel_phase, created_at, updated_at) VALUES (?, ?, ?, ?, '{}', 'CONFIRMED', 'NONE', ?, ?)""",
            (f"link-{proposal_id}", USER, proposal_id, client_order_id, now, now))
    store.save_strategy_draft(strategy_id="run-1", user_id=USER, source_session_id=session.session_id, name="双均线",
                              symbol=RB, version=1, status="monitoring", definition={"template": "combined"}, definition_digest="d-1")
    store.connection.execute("""INSERT INTO strategy_intent_snapshots (id, strategy_id, user_id, strategy_digest,
        signal_bar_time, context_digest, payload_json, created_at) VALUES ('intent-1', 'run-1', ?, 'd-1', '1', 'c-1', '{}', ?)""", (USER, now))
    for attempt_id, bar, client_order_id, action in (("a-open", "1", "cli-strategy-open", "open_long"),
                                                      ("a-close", "2", "cli-strategy-close", "close_long")):
        store.connection.execute("""INSERT INTO strategy_execution_attempts (id, strategy_id, user_id, intent_id, signal_bar_time,
            trading_day, action, volume, status, client_order_id, created_at, updated_at)
            VALUES (?, 'run-1', ?, 'intent-1', ?, '2026-09-24', ?, 1, 'SUBMITTED', ?, ?, ?)""",
            (attempt_id, USER, bar, action, client_order_id, now, now))
    store.connection.commit()


async def test_review_labels_each_round_trip_with_its_source(tmp_path):
    provider = Provider()
    app = application(tmp_path / "sources.sqlite3", Gateway(), provider)
    today = datetime.now(UTC).astimezone().strftime("%Y-%m-%d")
    try:
        seed_order_sources(app)
        provider.activity["trades"] = [
            # AI opened, the user closed by hand: the opener decides.
            order("t1", "cli-ai", "BUY", "OPEN", 3100.0, today, 9), order("t2", "cli-manual", "SELL", "CLOSETODAY", 3110.0, today, 9),
            # A strategy's position closed by its stop-loss: 止损.
            order("t3", "cli-strategy-open", "BUY", "OPEN", 3100.0, today, 10), order("t4", "cli-stop", "SELL", "CLOSETODAY", 3090.0, today, 10),
            # The strategy opened and closed: 策略 with its name.
            order("t5", "cli-strategy-open", "BUY", "OPEN", 3100.0, today, 11), order("t6", "cli-strategy-close", "SELL", "CLOSETODAY", 3105.0, today, 11),
            # The ticket (or an order outside the app): 手动.
            order("t7", "outside", "SELL", "OPEN", 3100.0, today, 12), order("t8", "", "BUY", "CLOSETODAY", 3095.0, today, 12),
            # The AI's card, confirmed, closed on the ticket: AI 建议.
            order("t9", "cli-card", "BUY", "OPEN", 3100.0, today, 13), order("t10", "cli-manual", "SELL", "CLOSETODAY", 3102.0, today, 14),
        ]
        async with client(app) as http:
            await login(http)
            review = (await http.get("/api/trade-review")).json()
        sources = [(t["source"], t.get("strategyName"), t.get("reason")) for t in review["trades"]]
        # A protective close carries its trigger line as the reason (今日复盘 2026-10-06 quotes it).
        assert sources == [("ai", None, "突破前高，自主开多"), ("stop", None, "止损触发"), ("strategy", "双均线", None),
                           ("manual", None, None), ("card", None, "回踩均线企稳，建议试多 1 手")]
        # Only the fields were added: pairing and P&L are what they were.
        assert [t["gross"] for t in review["trades"]] == [10000.0, -10000.0, 5000.0, 5000.0, 2000.0]
        assert [t["holdingMinutes"] for t in review["trades"]] == [0, 0, 0, 0, 60]
        assert all(t["note"] is None and t["deskView"] is None for t in review["trades"])
    finally:
        app.state.store.close()


async def test_journal_carries_reason_note_and_the_desk_view_at_the_open(tmp_path):
    """交易日志: the journal lists round trips newest first with 来源, reason, the user's 补充 and what the
    research desk said about the product when it was opened; a note is kept per account round trip,
    replaced, deleted, and refused for a round trip the account never had."""
    provider = Provider()
    app = application(tmp_path / "journal.sqlite3", Gateway(), provider)
    today = datetime.now(UTC).astimezone().strftime("%Y-%m-%d")
    try:
        seed_order_sources(app)
        provider.activity["trades"] = [
            order("t1", "cli-card", "BUY", "OPEN", 3100.0, today, 9), order("t2", "cli-manual", "SELL", "CLOSETODAY", 3090.0, today, 10),
            order("t3", "outside", "SELL", "OPEN", 3100.0, today, 11), order("t4", "", "BUY", "CLOSETODAY", 3095.0, today, 11),
        ]
        # The desk's judgment made before the 09:00 open, same research day (08:30 onwards); one made after it
        # (10:30) is not what the user saw when opening.
        research = app.state.research.connection
        for generated, stance in ((f"{today}T08:45:00+08:00", "偏多"), (f"{today}T10:30:00+08:00", "偏空")):
            research.execute("INSERT INTO research_desk_judgment_history VALUES (?, ?, ?, ?)", (
                "SHFE.rb", generated, today, json.dumps({"value": {"stance": stance, "confidence": "中", "headline": f"螺纹{stance}"},
                                                         "generatedAt": generated, "researchDay": today})))
        research.commit()
        async with client(app) as http:
            await login(http)
            journal = (await http.get("/api/account/journal")).json()
            assert journal["total"] == 2
            first, second = journal["trades"]
            assert (first["sourceLabel"], first["reason"], first["note"]) == ("手动", None, None)
            assert (second["sourceLabel"], second["reason"], second["pnlBasis"]) == ("AI 建议", "回踩均线企稳，建议试多 1 手", "gross")
            assert second["holdingMinutes"] == 60 and second["openTime"] == f"{today}T09:00+08:00"
            assert second["deskView"]["stance"] == "偏多" and second["deskView"]["headline"] == "螺纹偏多"
            assert first["deskView"]["stance"] == "偏空"   # opened 11:00, after the 10:30 judgment
            saved = await http.put(f"/api/account/trade-notes/{second['id']}", json={"text": "  看错了支撑  ", "via": "voice"})
            assert saved.status_code == 200 and saved.json()["note"]["text"] == "看错了支撑"
            await http.put(f"/api/account/trade-notes/{second['id']}", json={"text": "支撑没守住就该走"})
            entry = next(t for t in (await http.get("/api/account/journal")).json()["trades"] if t["id"] == second["id"])
            assert entry["note"] == "支撑没守住就该走"
            review = (await http.get("/api/account/pnl")).json()
            assert next(t for t in review["trades"] if t["id"] == second["id"])["note"]["via"] == "typed"
            assert (await http.put("/api/account/trade-notes/nope:nope", json={"text": "x"})).status_code == 404
            assert (await http.put(f"/api/account/trade-notes/{second['id']}", json={"text": "   "})).status_code == 422
            assert (await http.delete(f"/api/account/trade-notes/{second['id']}")).json()["deleted"] is True
            assert all(t["note"] is None for t in (await http.get("/api/account/journal")).json()["trades"])
            # The AI reads and writes through the same routes: its note is marked as written in chat.
            with_tools(app)
            read = (await http.post("/api/agent/call", json={"operation": "tool:get_trade_journal", "body": {"days": 3, "limit": 1}})).json()
            assert read["ok"] and [t["id"] for t in read["data"]["data"]["trades"]] == [first["id"]]
            written = (await http.post("/api/agent/call", json={"operation": "tool:set_trade_note",
                                                                "body": {"roundId": second["id"], "text": "追高了"}})).json()
            assert written["ok"] and written["data"]["data"]["note"]["via"] == "chat"
            refused = (await http.post("/api/agent/call", json={"operation": "tool:set_trade_note",
                                                                "body": {"roundId": "nope:nope", "text": "x"}})).json()
            assert not refused["ok"] and "没有这笔平仓记录" in refused["data"]["warning"]
            # 引导复盘: the biggest loss without a 补充 is picked, named by the catalog, with its question.
            await http.delete(f"/api/account/trade-notes/{second['id']}")
            picks = (await http.get("/api/account/review-picks")).json()
            assert [p["id"] for p in picks["picks"]] == [second["id"]] and picks["total"] == 2
            assert picks["picks"][0]["question"] == "rb2701这笔做多，亏了 10,000 元，拿了 1 小时。当时是怎么想的？"
            tool = (await http.post("/api/agent/call", json={"operation": "tool:get_review_picks", "body": {"scope": "week"}})).json()
            assert tool["ok"] and tool["data"]["data"]["scope"] == "week"
            assert (await http.get("/api/account/review-picks", params={"scope": "month"})).status_code == 422
            # 习惯对照 reads the same tagged rounds; one 计划内 trade (the confirmed AI card) is not enough to compare.
            habits = (await http.get("/api/account/habits")).json()
            assert (habits["status"], habits["planned"], habits["needed"], habits["days"]) == ("insufficient", 1, 5, 90)
            assert habits["basis"] == "gross" and habits["currency"] == "CNY" and len(habits["rules"]) == 4
            called = (await http.post("/api/agent/call", json={"operation": "GET /api/account/habits"})).json()
            assert called["ok"] and called["data"]["status"] == "insufficient", called
    finally:
        app.state.store.close()


async def test_active_filter_drops_finished_orders(tmp_path):
    gateway = Gateway()
    app = application(tmp_path / "active.sqlite3", gateway)
    try:
        result = await confirm(app, proposal(app))
        async with client(app) as http:
            await login(http)
            active = (await http.get("/api/simulation/orders", params={"active": "true"})).json()["items"]
            assert [item["clientOrderId"] for item in active] == [result["clientOrderId"]]
            gateway.query_report = gateway.report(result["simulationOrder"]["request"], status="FINISHED", volume_left=0)
            await app.state.simulation_orders.recover_once(user_id=USER, attempts=1, interval=0)
            everything = (await http.get("/api/simulation/orders")).json()["items"]
            assert [item["displayStatus"] for item in everything] == ["已成交"]
            assert (await http.get("/api/simulation/orders", params={"active": "true"})).json()["items"] == []
    finally:
        app.state.store.close()


async def test_ledger_run_persists_the_fills_it_reads(tmp_path):
    provider = Provider()
    provider.activity["trades"] = [fill("feed-open", AU, "BUY", "OPEN", 820.0, provider.day, 10)]
    app = application(tmp_path / "feed-trades.sqlite3", Gateway(), provider)
    try:
        assert await app.state.account_ledger.run_once() == {"fills": 1, "backfilled": 0, "daily": 1}
        rows = app.state.store.list_account_trades()
        assert [(row["tradeId"], row["tradingDay"], row["volumeMultiple"]) for row in rows] == [("feed-open", provider.day, 1000.0)]
        # Unchanged fills are not rewritten; fixture data is never persisted.
        app.state.store.upsert_account_trades = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rewritten"))
        assert await app.state.account_ledger.run_once() == {"fills": 0, "backfilled": 0, "daily": 1}
        provider.activity = {**provider.activity, "dataStatus": "simulated",
                             "trades": [fill("fixture", AU, "BUY", "OPEN", 1.0, provider.day, 11)]}
        await app.state.account_ledger.run_once()
        assert [row["tradeId"] for row in app.state.store.list_account_trades()] == ["feed-open"]
    finally:
        app.state.store.close()


async def test_review_reads_a_contracts_multiple_from_the_provider_once(tmp_path):
    provider = Provider()
    quotes: list[str] = []
    snapshot = provider.market_snapshot

    def counted(turn_id, symbol):
        quotes.append(symbol)
        return snapshot(turn_id, symbol)
    provider.market_snapshot = counted
    app = application(tmp_path / "multiple-once.sqlite3", Gateway(), provider)
    today = datetime.now(UTC).astimezone().strftime("%Y-%m-%d")
    provider.activity["trades"] = [fill("au-open", AU, "BUY", "OPEN", 820.0, today, 10),
                                   fill("au-close", AU, "SELL", "CLOSETODAY", 821.0, today, 11)]
    try:
        async with client(app) as http:
            await login(http)
            first = (await http.get("/api/trade-review")).json()
            for _ in range(3):
                assert (await http.get("/api/trade-review")).json()["trades"] == first["trades"]
            await app.state.account_ledger.run_once()
        assert [t["gross"] for t in first["trades"]] == [1000.0]
        assert quotes == [AU]
    finally:
        app.state.store.close()


async def test_finished_order_without_fill_report_is_backfilled_until_the_real_fill_arrives(tmp_path):
    provider = Provider()
    gateway = Gateway()
    app = application(tmp_path / "backfill.sqlite3", gateway, provider)
    # The backfilled open is stamped with when the order was placed and gets that moment's *trading* day (from 18:00
    # Beijing that is the next day); the fills below must sit on the same trading day for CLOSETODAY to pair.
    from trader_runtime.trading_calendar import trading_day_at
    today = trading_day_at(datetime.now(UTC))
    try:
        result = await confirm(app, proposal(app, volume=3))
        request = result["simulationOrder"]["request"]
        client_order_id = result["clientOrderId"]
        # Partially filled: the exchange took two of three lots before the order finished.
        gateway.query_report = gateway.report(request, status="FINISHED", volume_left=1)
        await app.state.simulation_orders.recover_once(user_id=USER, attempts=1, interval=0)
        rows = app.state.store.list_account_trades()
        assert [(r["tradeId"], r["clientOrderId"], r["symbol"], r["direction"], r["offset"], r["price"], r["volume"],
                 r["source"], r["approximate"]) for r in rows] == [
            (f"link:{client_order_id}", client_order_id, AU, "BUY", "OPEN", request["limitPrice"], 2, "order_backfill", True)]
        assert rows[0]["tradingDay"] and rows[0]["volumeMultiple"] == 1000.0
        # A repeated backfill writes nothing: the existing row of any source covers the link.
        from trader_runtime.order_backfill import backfill_order_fills
        assert backfill_order_fills(app.state.store, provider) == 0
        assert len(app.state.store.list_account_trades()) == 1
        async with client(app) as http:
            await login(http)
            # The close comes from the sidecar as a real fill: the round trip is approximate on the open leg.
            # The backfilled open is stamped with its placing time (now): the close must come after it whatever the wall clock says.
            provider.activity["trades"] = [{**fill("au-close", AU, "SELL", "CLOSETODAY", request["limitPrice"] + 2, today, 14, volume=2),
                                            "tradeDateTime": int((datetime.now(UTC) + timedelta(hours=1)).timestamp() * 1_000_000_000)}]
            review = (await http.get("/api/trade-review")).json()
            assert [(t["direction"], t["volume"], t["openPrice"], t["closePrice"], t["gross"], t["approximate"])
                    for t in review["trades"]] == [("long", 2, request["limitPrice"], request["limitPrice"] + 2, 4000.0, True)]
            # The real fill for the open order arrives (a better price): it replaces the synthesized one.
            # Also relative to the wall clock: the real open must precede the close above whatever the hour is.
            real_open = {**fill("au-open", AU, "BUY", "OPEN", request["limitPrice"] - 1, today, 10, volume=2),
                         "tradeDateTime": int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1_000_000_000),
                         "clientOrderId": client_order_id}
            provider.activity["trades"] = [real_open, provider.activity["trades"][0]]
            review = (await http.get("/api/trade-review")).json()
            assert [(t["openPrice"], t["gross"], t["approximate"]) for t in review["trades"]] == [(request["limitPrice"] - 1, 6000.0, False)]
            assert [(r["tradeId"], r["source"]) for r in app.state.store.list_account_trades()] == [("au-open", "activity"), ("au-close", "activity")]
            assert backfill_order_fills(app.state.store, provider) == 0
            assert len(app.state.store.list_account_trades()) == 2
    finally:
        app.state.store.close()


async def test_a_note_follows_its_round_trip_when_the_real_fill_replaces_the_backfill(tmp_path):
    """#502: the user's 补充 on a round trip whose open was backfilled stays with that trade once the real
    fill replaces the backfill (one fill for the order); an order that turns out to have several fills
    keeps the 补充 where it was instead of copying it onto each."""
    provider = Provider()
    gateway = Gateway()
    path = tmp_path / "note-follows.sqlite3"
    app = application(path, gateway, provider)
    try:
        async def backfilled_round(volume: int, key: str, close_id: str):
            result = await confirm(app, proposal(app, volume=volume, turn=f"turn-{key}"), f"confirm-key-{key}")
            request, client_order_id = result["simulationOrder"]["request"], result["clientOrderId"]
            gateway.query_report = gateway.report(request, status="FINISHED", volume_left=0)
            await app.state.simulation_orders.recover_once(user_id=USER, attempts=1, interval=0)
            row = next(r for r in app.state.store.list_account_trades() if r["tradeId"] == f"link:{client_order_id}")
            close = {**fill(close_id, AU, "SELL", "CLOSETODAY", request["limitPrice"] - 1, row["tradingDay"], 14, volume=volume),
                     "tradeDateTime": row["tradeDateTime"] + 3_600_000_000_000}
            return request, client_order_id, row, close

        request, client_order_id, row, close = await backfilled_round(2, "one", "au-close")
        provider.activity["trades"] = [close]
        async with client(app) as http:
            await login(http)
            [entry] = (await http.get("/api/account/journal")).json()["trades"]
            assert entry["id"] == f"link:{client_order_id}:au-close"
            saved = await http.put(f"/api/account/trade-notes/{entry['id']}", json={"text": "追高了", "via": "voice"})
            assert saved.status_code == 200
            assert (await http.get("/api/account/review-picks", params={"scope": "week"})).json()["unnoted"] == 0
            # The real fill for the open order arrives, same price, volume and time, and replaces the backfill.
            real_open = {**fill("au-open", AU, "BUY", "OPEN", request["limitPrice"], row["tradingDay"], 9, volume=2),
                         "tradeDateTime": row["tradeDateTime"], "clientOrderId": client_order_id}
            provider.activity["trades"] = [real_open, close]
            for _ in range(2):   # repeated reads stay put
                [entry] = (await http.get("/api/account/journal")).json()["trades"]
                assert (entry["id"], entry["note"]) == ("au-open:au-close", "追高了")
                assert (await http.get("/api/account/review-picks", params={"scope": "week"})).json()["unnoted"] == 0
            [trade] = (await http.get("/api/account/pnl")).json()["trades"]
            assert (trade["id"], trade["note"]["text"], trade["note"]["via"]) == ("au-open:au-close", "追高了", "voice")
            # A card still holding the old id finds no such round trip; the current id updates it.
            stale = await http.put(f"/api/account/trade-notes/link:{client_order_id}:au-close", json={"text": "x"})
            assert stale.status_code == 404
            assert (await http.put("/api/account/trade-notes/au-open:au-close", json={"text": "追高，止损太远"})).status_code == 200

            # An order whose two lots come back as two real fills: the 补充 is not copied onto both.
            request, split_order, row, close = await backfilled_round(2, "two", "au-close-2")
            provider.activity["trades"] = [real_open, provider.activity["trades"][1], close]
            [entry] = [t for t in (await http.get("/api/account/journal")).json()["trades"] if t["id"].endswith(":au-close-2")]
            await http.put(f"/api/account/trade-notes/{entry['id']}", json={"text": "拆单前的补充"})
            halves = [{**fill(f"au-open-{n}", AU, "BUY", "OPEN", request["limitPrice"], row["tradingDay"], 9),
                       "tradeDateTime": row["tradeDateTime"] + n, "clientOrderId": split_order} for n in (1, 2)]
            provider.activity["trades"] = [real_open, provider.activity["trades"][1], *halves, close]
            split = [t for t in (await http.get("/api/account/journal")).json()["trades"] if t["id"].endswith(":au-close-2")]
            assert sorted(t["id"] for t in split) == ["au-open-1:au-close-2", "au-open-2:au-close-2"]
            assert all(t["note"] is None for t in split)
            # The review lists that 补充 as unlinked (#514): its backfilled fill is gone and the
            # split real fills are not provably the same trade, so it stays put, read-only.
            review = (await http.get("/api/trade-review")).json()
            assert [(n["roundId"], n["text"], n["via"]) for n in review["unlinkedNotes"]] == [
                (f"link:{split_order}:au-close-2", "拆单前的补充", "typed")]
            # The 复盘 page reads the same list from /api/account/pnl.
            assert (await http.get("/api/account/pnl")).json()["unlinkedNotes"] == review["unlinkedNotes"]
            # The 交易日志 (and the AI/CLI reading it) sees the same list and the note's scope (#514).
            journal = (await http.get("/api/account/journal")).json()
            assert journal["unlinkedNotes"] == review["unlinkedNotes"]
            assert all(t["noteScopeChanged"] is False for t in journal["trades"])
        app.state.store.close()
        store = RuntimeStore(path)
        try:   # after a reopen the 补充 are where they were, nothing copied or lost
            assert {key: note["text"] for key, note in store.list_trade_notes().items()} == {
                "au-open:au-close": "追高，止损太远", f"link:{split_order}:au-close-2": "拆单前的补充"}
        finally:
            store.close()
    finally:
        app.state.store.close()


def test_notes_move_only_when_the_replacement_provably_keeps_the_trades(tmp_path):
    """#514-1: a backfill replaced by real fills takes its 补充 along only where the replacement is
    provably the same trade — one real fill of the same volume, and every FIFO round trip the
    backfill took part in reappears under the real id with the same other side and volume. Split
    fills (one batch or one per pass) and re-ordered FIFO keep each 补充 on its old key and listed
    as unlinkedNotes; nothing is guessed onto a round trip that never happened."""
    day = "2026-10-02"
    minute = 60 * 10**9

    def real(trade_id, client_order_id, direction, offset, price, when, volume):
        return {"tradeId": trade_id, "clientOrderId": client_order_id, "symbol": RB, "direction": direction,
                "offset": offset, "price": price, "volume": volume, "tradeDateTime": when,
                "tradingDay": day, "volumeMultiple": 10.0}

    def backfilled(client_order_id, direction, offset, price, when, volume):
        return {"tradeId": f"link:{client_order_id}", "clientOrderId": client_order_id, "symbol": RB,
                "direction": direction, "offset": offset, "price": price, "volume": volume,
                "tradeDateTime": when, "tradingDay": day, "volumeMultiple": 10.0}

    path = tmp_path / "provable.sqlite3"
    store = RuntimeStore(path)
    try:
        # 双腿同批：开、平都补录过，两条真实成交同一批到达 → 两侧都能证明，补充整体迁到真实回合。
        store.backfill_account_trades([backfilled("o-bf", "BUY", "OPEN", 3100.0, at(day, 9), 2),
                                       backfilled("c-bf", "SELL", "CLOSETODAY", 3110.0, at(day, 10), 2)])
        store.set_trade_note("link:o-bf:link:c-bf", "双边补录的补充", via="typed", user_id="owner")
        store.upsert_account_trades([real("r-open", "o-bf", "BUY", "OPEN", 3100.0, at(day, 9), 2),
                                     real("r-close", "c-bf", "SELL", "CLOSETODAY", 3110.0, at(day, 10), 2)], {RB: 10.0})
        assert {key: note["text"] for key, note in store.list_trade_notes().items()} == {
            "r-open:r-close": "双边补录的补充"}

        # 分批两笔：补录 2 手，真实成交 1 手先到、另一手下一批才到 → 证明不了，补充留在原处。
        store.backfill_account_trades([backfilled("o-split", "BUY", "OPEN", 3100.0, at(day, 11), 2)])
        store.upsert_account_trades([real("c2", "", "SELL", "CLOSETODAY", 3110.0, at(day, 12), 2)], {RB: 10.0})
        store.set_trade_note("link:o-split:c2", "拆着回补的补充", via="voice", user_id="owner")
        store.upsert_account_trades([real("r-half-a", "o-split", "BUY", "OPEN", 3100.0, at(day, 11), 1)], {RB: 10.0})
        store.upsert_account_trades([real("r-half-b", "o-split", "BUY", "OPEN", 3100.0, at(day, 11), 1)], {RB: 10.0})
        assert {key: note["text"] for key, note in store.list_trade_notes().items()} == {
            "r-open:r-close": "双边补录的补充", "link:o-split:c2": "拆着回补的补充"}
        assert [note["roundId"] for note in store.list_unlinked_trade_notes()] == ["link:o-split:c2"]

        # FIFO 重组（i514 §2B）：补录 2+1 手、两笔平仓；真实回补交换了两笔开的先后，回合跟着
        # 重组。旧实现按字符串改名会把补充挂到“看起来像”的回合上；这里三条全部留在原处。
        store.backfill_account_trades([backfilled("oa", "BUY", "OPEN", 3100.0, at(day, 9) + minute, 2),
                                       backfilled("ob", "BUY", "OPEN", 3100.0, at(day, 9) + 2 * minute, 1)])
        store.upsert_account_trades([real("c1", "", "SELL", "CLOSETODAY", 3110.0, at(day, 10), 1),
                                     real("c3", "", "SELL", "CLOSETODAY", 3110.0, at(day, 10) + 5 * minute, 2)], {RB: 10.0})
        for key, text in (("link:oa:c1", "先出一手"), ("link:oa:c3", "再出两手"), ("link:ob:c3", "后一笔的")):
            store.set_trade_note(key, text, via="typed", user_id="owner")
        store.upsert_account_trades([real("real-a", "oa", "BUY", "OPEN", 3100.0, at(day, 9) - 30 * minute, 2),
                                     real("real-b", "ob", "BUY", "OPEN", 3100.0, at(day, 9) - 60 * minute, 1)], {RB: 10.0})
        assert {key: note["text"] for key, note in store.list_trade_notes().items()} == {
            "r-open:r-close": "双边补录的补充", "link:o-split:c2": "拆着回补的补充",
            "link:oa:c1": "先出一手", "link:oa:c3": "再出两手", "link:ob:c3": "后一笔的"}
        assert sorted(note["roundId"] for note in store.list_unlinked_trade_notes()) == [
            "link:o-split:c2", "link:oa:c1", "link:oa:c3", "link:ob:c3"]
    finally:
        store.close()
    for _ in range(2):   # a reopen has no startup migration to finish the job (#514-1): 原地不动
        store = RuntimeStore(path)
        try:
            assert len(store.list_trade_notes()) == 5
            assert len(store.list_unlinked_trade_notes()) == 4
        finally:
            store.close()


def test_legacy_orphaned_notes_stay_put_and_are_listed_after_a_reopen(tmp_path):
    """#502/#514 history: 补充 written under a backfilled fill's id that a real fill has since
    replaced (the pre-fix rows) are not migrated on boot — the backfill row is gone, so equivalence
    can no longer be proven. They keep their text/via/user/time where they are, and the review
    lists them as unlinkedNotes; another account's notes are untouched."""
    path = tmp_path / "orphans.sqlite3"
    store = RuntimeStore(path)
    other = "tqkq-sim:other"
    planted = {"link:AITone:real-close": "补录开仓的补充", "open-b:link:AITthree": "平仓那边换了",
               "link:AITtwo:real-close": "拆单", "link:AITfour:real-close": "旧的",
               "taken-open:real-close": "新的"}
    try:
        store.upsert_account_trades([
            {**fill("real-open", RB, "BUY", "OPEN", 3100.0, "2026-10-02", 9), "clientOrderId": "AITone"},
            {**fill("split-a", RB, "BUY", "OPEN", 3100.0, "2026-10-02", 9), "clientOrderId": "AITtwo"},
            {**fill("split-b", RB, "BUY", "OPEN", 3100.0, "2026-10-02", 9), "clientOrderId": "AITtwo"},
            {**fill("real-close", RB, "SELL", "CLOSETODAY", 3090.0, "2026-10-02", 10), "clientOrderId": "AITthree"},
            {**fill("taken-open", RB, "BUY", "OPEN", 3100.0, "2026-10-02", 9), "clientOrderId": "AITfour"},
        ])
        for key, text in planted.items():
            store.set_trade_note(key, text, via="voice", user_id="owner")
        store.set_trade_note("link:AITone:real-close", "别的账户", via="typed", user_id="guest", trading_account_id=other)
    finally:
        store.close()
    for _ in range(2):   # nothing moves on boot, however many times it reopens
        store = RuntimeStore(path)
        try:
            assert {key: note["text"] for key, note in store.list_trade_notes().items()} == planted
            # Every link: side's backfill row is gone → unlinked; the plain key is a live round trip.
            assert sorted(note["roundId"] for note in store.list_unlinked_trade_notes()) == [
                "link:AITfour:real-close", "link:AITone:real-close", "link:AITtwo:real-close",
                "open-b:link:AITthree"]
            assert list(store.list_trade_notes(trading_account_id=other)) == ["link:AITone:real-close"]
        finally:
            store.close()


async def test_reverse_order_recovery_still_pairs_the_backfilled_round_trip(tmp_path):
    """#503: recovery queries the newest order first, so the close's report is saved before the open's.
    The backfilled fills take the order's own time (the report's insert time, else when the order was
    placed), not the moment the report was read back: the round trip still pairs, and a row written
    the old way is corrected on the next pass."""
    from test_simulation_orders import NormalProvider, close_proposal
    from trader_runtime.order_backfill import backfill_order_fills
    provider = Provider()
    provider.position_items = NormalProvider().position_items
    gateway = Gateway()
    app = application(tmp_path / "reverse.sqlite3", gateway, provider)
    day = (datetime.now(UTC) - timedelta(days=2)).astimezone().strftime("%Y-%m-%d")
    try:
        gateway.submit_error = True
        opened = (await confirm(app, proposal(app), "confirm-open-0001"))["simulationOrder"]
        closed = (await confirm(app, close_proposal(app), "confirm-close-0001"))["simulationOrder"]
        assert (opened["displayStatus"], closed["displayStatus"]) == ("结果未知", "结果未知")
        for order, hour in ((opened, 9), (closed, 10)):
            placed = datetime.fromtimestamp(at(day, hour) / 1e9, UTC).isoformat()
            app.state.store.connection.execute(
                "UPDATE tqkq_sim_order_links SET created_at = ?, updated_at = ? WHERE client_order_id = ?",
                (placed, placed, order["clientOrderId"]))
        app.state.store.connection.commit()
        reports = {order["clientOrderId"]: gateway.report(order["request"], status="FINISHED", volume_left=0)
                   for order in (opened, closed)}
        queried: list[str] = []

        async def query_order(client_order_id):
            queried.append(client_order_id)
            return reports[client_order_id]
        gateway.query_order = query_order
        gateway.submit_error = False
        await app.state.simulation_orders.recover_once(user_id=USER, attempts=1, interval=0)
        assert queried == [closed["clientOrderId"], opened["clientOrderId"]]
        rows = {r["tradeId"]: r for r in app.state.store.list_account_trades()}
        open_row, close_row = rows[f"link:{opened['clientOrderId']}"], rows[f"link:{closed['clientOrderId']}"]
        assert (open_row["tradeDateTime"], close_row["tradeDateTime"]) == (at(day, 9), at(day, 10))
        expected = round((closed["request"]["limitPrice"] - opened["request"]["limitPrice"]) * 1000, 2)
        async with client(app) as http:
            await login(http)
            review = (await http.get("/api/trade-review", params={"days": 30})).json()
            assert [(t["volume"], t["gross"], t["approximate"]) for t in review["trades"]] == [(1, expected, True)]
            assert review["unmatched"] == []
            pnl = (await http.get("/api/account/pnl")).json()
            assert (pnl["tradeCount"], pnl["realizedNet"]) == (1, expected)
            # A row stamped the old way (the read-back moment) is re-derived from its order on the next pass.
            app.state.store.connection.execute(
                "UPDATE account_trades SET trade_date_time = ?, trading_day = ? WHERE trade_id = ?",
                (at(day, 23), "2099-01-01", open_row["tradeId"]))
            app.state.store.connection.commit()
            assert backfill_order_fills(app.state.store, provider) == 1
            assert backfill_order_fills(app.state.store, provider) == 0
            fixed = {r["tradeId"]: r for r in app.state.store.list_account_trades()}[open_row["tradeId"]]
            assert (fixed["tradeDateTime"], fixed["tradingDay"]) == (at(day, 9), open_row["tradingDay"])
            assert len((await http.get("/api/trade-review", params={"days": 30})).json()["trades"]) == 1
    finally:
        app.state.store.close()


def test_backfilled_fill_time_prefers_the_report_insert_time_over_the_read_back_moment():
    from trader_runtime.order_backfill import synthesize_fills
    request = {"clientOrderId": "AITx", "symbol": AU, "direction": "BUY", "offset": "OPEN", "limitPrice": 820.0}
    link = {"submitPhase": "CONFIRMED", "request": request, "createdAt": "2026-10-02T01:00:00+00:00",
            "lastSyncedAt": "2026-10-03T08:00:00+00:00",
            "report": {"order": {"status": "FINISHED", "volume": 1, "volumeLeft": 0}}}
    [placed] = synthesize_fills([link], lambda _: 1000.0)
    assert (placed["tradeDateTime"], placed["tradingDay"]) == (at("2026-10-02", 9), "2026-10-02")
    # A report with the broker's insert time (night session, 21:05 Friday: the next trading day is Monday).
    night = int(datetime.fromisoformat("2026-10-09T21:05:00+08:00").timestamp() * 1e9)
    link["report"]["order"]["insertDateTime"] = night
    [inserted] = synthesize_fills([link], lambda _: 1000.0)
    assert (inserted["tradeDateTime"], inserted["tradingDay"]) == (night, "2026-10-12")


async def test_account_pnl_groups_realized_by_month_and_day(tmp_path):
    provider = Provider()
    app = application(tmp_path / "pnl.sqlite3", Gateway(), provider)
    # Local calendar first: near midnight the UTC month boundary and the local one differ.
    now_local = datetime.now(UTC).astimezone()
    today = now_local.strftime("%Y-%m-%d")
    last_month = (now_local.replace(day=1) - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        async with client(app) as http:
            await login(http)
            provider.activity["trades"] = [fill("au-open", AU, "BUY", "OPEN", 820.0, last_month, 10),
                                           fill("au-close", AU, "SELL", "CLOSETODAY", 819.0, last_month, 14),
                                           fill("rb-open", RB, "SELL", "OPEN", 3099.0, today, 9, volume=3),
                                           fill("rb-close", RB, "BUY", "CLOSETODAY", 3045.0, today, 10, volume=3)]
            pnl = (await http.get("/api/account/pnl")).json()
            equity = provider.account_summary("")["equity"]
            assert pnl["initialBalance"] == 1_000_000 and pnl["totalPnl"] == round(equity - 1_000_000, 2)
            assert pnl["today"] == provider.account_summary("")["todayNetPnl"]
            assert pnl["accountName"] == provider.account_summary("")["accountName"]
            # No account day rows yet: the cumulative commission is unknown, not zero.
            assert pnl["cumulativeCommission"] is None and pnl["commissionSince"] is None
            app.state.store.upsert_account_daily("2026-09-09", {"equity": 1_000_100, "commission": 12.5})
            app.state.store.upsert_account_daily(today, {"equity": 1_000_000, "commission": 7.5})
            pnl = (await http.get("/api/account/pnl")).json()
            assert pnl["cumulativeCommission"] == 20.0 and pnl["commissionSince"] == "2026-09-09"
            # 权益曲线: every stored day, oldest first, also days without closes; the day's P&L
            # falls back to the equity move when the row has none (the first day has no witness).
            assert [(p["day"], p["equity"], p["pnl"]) for p in pnl["equityCurve"]] == [
                ("2026-09-09", 1_000_100.0, None), (today, 1_000_000.0, -100.0)]
            assert [(m["month"], m["realizedNet"], m["tradeCount"], m["winRate"]) for m in pnl["months"]] == [
                (today[:7], 162000.0, 1, 1.0), (last_month[:7], -1000.0, 1, 0.0)]
            assert pnl["thisMonth"] == 162000.0 and pnl["winRate"] == 0.5 and pnl["tradeCount"] == 2
            assert pnl["thisMonthTradeCount"] == 1
            assert [(d["day"], d["realizedNet"]) for d in pnl["days"]] == [(today, 162000.0), (last_month, -1000.0)]
            assert [(t["symbol"], t["direction"], t["volume"], t["openPrice"], t["closePrice"], t["approximate"], t["tradingDay"])
                    for t in pnl["trades"]] == [(RB, "short", 3, 3099.0, 3045.0, False, today), (AU, "long", 1, 820.0, 819.0, False, last_month)]
            # The starting balance is an account preference.
            from test_account_preferences import PREFERENCES
            saved = await http.put("/api/app/preferences", json={"revision": 0, "preferences": {**PREFERENCES, "initialBalance": 900_000}})
            assert saved.status_code == 200, saved.text
            pnl = (await http.get("/api/account/pnl", params={"months": 1})).json()
            assert pnl["initialBalance"] == 900_000 and pnl["totalPnl"] == round(equity - 900_000, 2)
            assert [m["month"] for m in pnl["months"]] == [today[:7]]
    finally:
        app.state.store.close()


async def test_account_pnl_falls_back_to_the_settled_row_and_equity_delta(tmp_path):
    """After the 15:15 settlement the realtime summary reports a zeroed todayNetPnl;
    the day's stored row (and the equity move when even that is gone) still shows the result."""
    provider = Provider()
    app = application(tmp_path / "pnl-settled.sqlite3", Gateway(), provider)
    now_local = datetime.now(UTC).astimezone()
    today = now_local.strftime("%Y-%m-%d")
    yesterday = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")
    realtime = provider.account_summary
    provider.day = today
    provider.account_summary = lambda turn_id: {**realtime(turn_id), "equity": 1035207.0,
                                                 "todayPnl": 0.0, "todayNetPnl": 0.0,
                                                 "closeProfit": 0.0, "positionProfit": 0.0, "commission": 0.0}
    try:
        async with client(app) as http:
            await login(http)
            provider.activity["trades"] = [fill("au-open", AU, "BUY", "OPEN", 820.0, today, 10),
                                           fill("au-close", AU, "SELL", "CLOSETODAY", 818.5, today, 14)]
            # Yesterday's row carries its own result; today's row keeps the pre-settlement figures.
            app.state.store.upsert_account_daily(yesterday, {"equity": 1036929.0, "staticBalance": 1036929.0,
                                                             "closeProfit": -1500.0, "commission": 222.0,
                                                             "todayNetPnl": -1722.0})
            app.state.store.upsert_account_daily(today, {"equity": 1035207.0, "staticBalance": 1035207.0,
                                                          "closeProfit": -1500.0, "commission": 222.0,
                                                          "todayNetPnl": -1722.0})
            pnl = (await http.get("/api/account/pnl")).json()
            assert pnl["today"] == -1722.0
            assert pnl["equity"] == 1035207.0
            # The day's row comes from the settled figures; its equityDelta is the move against
            # the previous stored trading day (yesterday's row, which has no round trips).
            assert [(d["day"], d["equity"], d["equityDelta"]) for d in pnl["days"]] == [
                (today, 1035207.0, -1722.0)]
            assert [(p["day"], p["pnl"]) for p in pnl["equityCurve"]] == [(yesterday, -1722.0), (today, -1722.0)]
            # A day whose stored row was flattened to zero by the old overwrite (fabricated here
            # with a direct write, since the fixed upsert refuses to zero a booked day): the
            # equity difference against the previous trading day is the only remaining witness.
            app.state.store.connection.execute(
                "UPDATE account_daily SET close_profit = 0, position_profit = 0, commission = 0,"
                " today_net_pnl = 0 WHERE trading_day IN (?, ?)", (today, yesterday))
            pnl = (await http.get("/api/account/pnl")).json()
            assert pnl["today"] == 0.0  # nothing recoverable for the headline
            assert [(d["day"], d["equityDelta"]) for d in pnl["days"]] == [(today, -1722.0)]
            assert [(p["day"], p["pnl"]) for p in pnl["equityCurve"]] == [(yesterday, 0.0), (today, -1722.0)]
    finally:
        app.state.store.close()


async def test_ledger_run_keeps_one_account_row_per_trading_day(tmp_path):
    provider = Provider()
    app = application(tmp_path / "daily.sqlite3", Gateway(), provider)
    try:
        assert await app.state.account_ledger.run_once() == {"fills": 0, "backfilled": 0, "daily": 1}
        rows = app.state.store.list_account_daily()
        summary = provider.account_summary("")
        assert [(r["tradingDay"], r["equity"], r["todayNetPnl"]) for r in rows] == [(provider.day, summary["equity"], summary["todayNetPnl"])]
        await app.state.account_ledger.run_once()
        assert len(app.state.store.list_account_daily()) == 1
    finally:
        app.state.store.close()


def test_partial_real_fills_shrink_the_backfilled_row_until_covered(tmp_path):
    """#514-10: TqKq 的部分成交一笔一笔到。第一笔到达时补录行按剩余手数留在账上，
    不再凭第一笔就整行消失；全部成交到达后才删除，中间任何时刻读到的都是那几手。"""
    day = "2026-10-02"
    store = RuntimeStore(tmp_path / "partial.sqlite3")

    def real(trade_id, volume):
        return {"tradeId": trade_id, "clientOrderId": "o-part", "symbol": RB, "direction": "BUY",
                "offset": "OPEN", "price": 3100.0, "volume": volume, "tradeDateTime": at(day, 9),
                "tradingDay": day, "volumeMultiple": 10.0}

    try:
        store.backfill_account_trades([
            {"clientOrderId": "o-part", "symbol": RB, "direction": "BUY", "offset": "OPEN",
             "price": 3100.0, "volume": 3, "tradeDateTime": at(day, 9), "tradingDay": day,
             "volumeMultiple": 10.0}])
        store.upsert_account_trades([real("r-1", 1)], {RB: 10.0})
        assert sorted((row["tradeId"], row["volume"]) for row in store.list_account_trades()) == [
            ("link:o-part", 2), ("r-1", 1)]   # 剩余两手仍以补录行在账
        store.upsert_account_trades([real("r-1", 1)], {RB: 10.0})   # 同一笔成交再读一次不再扣
        store.upsert_account_trades([real("r-2", 1)], {RB: 10.0})
        assert sorted((row["tradeId"], row["volume"]) for row in store.list_account_trades()) == [
            ("link:o-part", 1), ("r-1", 1), ("r-2", 1)]
        store.upsert_account_trades([real("r-1", 1), real("r-3", 1)], {RB: 10.0})
        assert sorted((row["tradeId"], row["volume"]) for row in store.list_account_trades()) == [
            ("r-1", 1), ("r-2", 1), ("r-3", 1)]
    finally:
        store.close()


def test_trade_note_compares_its_written_volume_with_the_round(tmp_path):
    """#514-2: 补充写入时记下回合手数；同一回合 ID 的手数后来变了（迟到的更早开仓
    重排 FIFO），读回时标“范围已变”，原文照读、不挪动。"""
    from trader_runtime.strategy_workbench import journal_fields
    store = RuntimeStore(tmp_path / "note-volume.sqlite3")
    try:
        store.set_trade_note("open:close", "这两手拿得住", via="typed", user_id="owner", volume_at_write=2)
        assert store.list_trade_notes()["open:close"]["volumeAtWrite"] == 2
        rounds = [{"id": "open:close", "symbol": RB, "direction": "long", "volume": 1,
                   "openTime": at("2026-10-02", 9), "closeTime": at("2026-10-02", 10)}]
        journal_fields(rounds, store.list_trade_notes())
        assert rounds[0]["note"]["scopeChanged"] is True and rounds[0]["note"]["text"] == "这两手拿得住"
        journal_fields(rounds, {"open:close": {**store.list_trade_notes()["open:close"], "volumeAtWrite": 1}})
        assert "scopeChanged" not in rounds[0]["note"]
    finally:
        store.close()


async def test_put_trade_note_stamps_the_round_volume(tmp_path):
    provider = Provider()
    app = application(tmp_path / "note-stamp.sqlite3", Gateway(), provider)
    today = datetime.now(UTC).astimezone().strftime("%Y-%m-%d")
    try:
        async with client(app) as http:
            await login(http)
            provider.activity["trades"] = [fill("au-open", AU, "BUY", "OPEN", 820.0, today, 10, volume=2),
                                           fill("au-close", AU, "SELL", "CLOSETODAY", 819.0, today, 14, volume=2)]
            [trade] = (await http.get("/api/account/pnl")).json()["trades"]
            saved = await http.put(f"/api/account/trade-notes/{trade['id']}",
                                   json={"text": "两手追高", "via": "typed"})
            assert saved.status_code == 200, saved.text
            assert saved.json()["note"]["volumeAtWrite"] == 2
            note = app.state.store.list_trade_notes()[trade["id"]]
            assert note["volumeAtWrite"] == 2
            # 手数没变：读回不标“范围已变”。
            [entry] = (await http.get("/api/account/pnl")).json()["trades"]
            assert entry["note"]["text"] == "两手追高" and "scopeChanged" not in entry["note"]
    finally:
        app.state.store.close()


def test_daily_pnl_columns_move_together_or_not_at_all(tmp_path):
    """#514-8: 四个盈亏字段是一份快照，要么整体替换、要么整体保留；缺一项的读数
    不再把自己的字段拼到另一时刻读到的值上。"""
    store = RuntimeStore(tmp_path / "daily-whole.sqlite3")
    try:
        store.upsert_account_daily("2026-10-02", {"equity": 100.0, "closeProfit": 100.0,
                                                  "positionProfit": 0.0, "commission": 10.0, "todayNetPnl": 90.0})
        store.upsert_account_daily("2026-10-02", {"equity": 200.0, "closeProfit": 200.0,
                                                  "commission": 20.0, "todayNetPnl": 180.0})  # positionProfit 缺
        [row] = store.list_account_daily()
        assert (row["closeProfit"], row["positionProfit"], row["commission"], row["todayNetPnl"]) == (
            100.0, 0.0, 10.0, 90.0)
        assert row["equity"] == 200.0   # 权益照常前进
        store.upsert_account_daily("2026-10-02", {"equity": 200.0, "closeProfit": 200.0,
                                                  "positionProfit": 5.0, "commission": 20.0, "todayNetPnl": 185.0})
        [row] = store.list_account_daily()
        assert (row["closeProfit"], row["positionProfit"], row["commission"], row["todayNetPnl"]) == (
            200.0, 5.0, 20.0, 185.0)
        store.upsert_account_daily("2026-10-02", {"equity": 200.0, "closeProfit": 0.0,
                                                  "positionProfit": 0.0, "commission": 0.0, "todayNetPnl": 0.0})
        [row] = store.list_account_daily()
        assert row["todayNetPnl"] == 185.0   # 结算清零不抹掉当日已记的结果
    finally:
        store.close()


def test_account_pnl_marks_estimated_curve_points_and_carries_unmatched():
    """#514-9/#514-11: 权益差兜底出来的曲线点标 pnlEstimated；未配平的平仓手数随
    盈亏响应一起给出，读的人分得清“没配平”和“全配平”。"""
    from trader_runtime.account_pnl import account_pnl
    day, next_day = "2026-10-02", "2026-10-05"

    def round_(ident, close_day, gross):
        return {"id": ident, "symbol": RB, "direction": "long", "volume": 1, "openTime": at(day, 9),
                "closeTime": at(close_day, 10), "openPrice": 3100.0, "closePrice": 3110.0,
                "gross": gross, "net": gross, "fees": 0.0, "tradingDay": close_day}

    def daily(day_, equity, **pnl):
        return {"tradingDay": day_, "equity": equity, "staticBalance": None, "closeProfit": None,
                "positionProfit": None, "commission": None, "todayNetPnl": None, "capturedAt": "", **pnl}

    review = {"trades": [round_("t1", day, 100.0), round_("t2", next_day, -50.0)],
              "unmatched": [{"tradeId": "c-late", "volume": 1}], "costSource": "estimate"}
    rows = [daily("2026-09-30", 1_000_000.0),
            daily(day, 1_000_100.0, closeProfit=100.0, positionProfit=0.0, commission=10.0, todayNetPnl=90.0),
            daily(next_day, 1_000_050.0)]
    result = account_pnl(review, daily=rows, initial_balance=1_000_000.0, months=3)
    assert [(p["day"], p["pnl"], p.get("pnlEstimated", False)) for p in result["equityCurve"]] == [
        ("2026-09-30", None, False),   # 没有前一日权益：无从估算
        (day, 90.0, False),            # 账面值
        (next_day, -50.0, True)]       # 权益差兜底：标估算
    assert result["unmatched"] == [{"tradeId": "c-late", "volume": 1}]


def test_backfilled_trading_day_snaps_holidays_via_the_exchange_calendar():
    """#514-12: 补录推断交易日带上交易所日历（国庆 10-01 起休市、10-08 开市），
    节假日顺延到下一个已知交易日；日历范围之外退回周末规则，不跨缺口硬跳。"""
    from trader_runtime.exchange_calendar import calendar_trading_days
    from trader_runtime.trading_calendar import trading_day_at as trading_day_for
    days = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"]

    def moment(text):
        return datetime.fromisoformat(text)

    assert trading_day_for(moment("2026-09-30T14:00:00+08:00"), days) == "2026-09-30"
    assert trading_day_for(moment("2026-10-01T10:00:00+08:00"), days) == "2026-10-08"
    assert trading_day_for(moment("2026-10-07T22:30:00+08:00"), days) == "2026-10-08"   # 夜盘跨日
    assert trading_day_for(moment("2026-10-10T10:00:00+08:00"), days) == "2026-10-12"   # 范围外：周末规则
    assert trading_day_for(moment("2026-09-25T10:00:00+08:00"), days) == "2026-09-25"   # 范围前：不跳到首日
    assert trading_day_for(moment("2026-10-01T10:00:00+08:00")) == "2026-10-01"         # 没有日历：老规则

    class CalendarProvider:
        def market_calendar(self):
            return {"tradingDay": "2026-10-09", "previousTradingDay": "2026-10-08",
                    "recentTradingDays": ["2026-09-30", "2026-10-08"]}

    class BrokenProvider:
        def market_calendar(self):
            raise RuntimeError("sidecar_market_calendar_unavailable")

    assert calendar_trading_days(CalendarProvider()) == ["2026-09-30", "2026-10-08", "2026-10-09"]
    assert calendar_trading_days(BrokenProvider()) == []


def test_journal_entry_carries_the_notes_scope() -> None:
    """#514: the journal reads the note's volumeAtWrite / scopeChanged like the review and the P&L do."""
    from trader_runtime.routes.account import journal_entry
    base = {"id": "r1", "symbol": "SHFE.au2612", "direction": "long", "volume": 1, "openTime": 1_000_000_000,
            "closeTime": 2_000_000_000, "openPrice": 1.0, "closePrice": 2.0, "gross": 1.0}
    changed = journal_entry({**base, "note": {"text": "追高了", "volumeAtWrite": 2, "scopeChanged": True}})
    assert (changed["note"], changed["noteVolumeAtWrite"], changed["noteScopeChanged"]) == ("追高了", 2, True)
    plain = journal_entry({**base, "note": {"text": "ok", "volumeAtWrite": 1}})
    assert plain["noteScopeChanged"] is False
    none = journal_entry({**base, "note": None})
    assert (none["note"], none["noteVolumeAtWrite"], none["noteScopeChanged"]) == (None, None, False)

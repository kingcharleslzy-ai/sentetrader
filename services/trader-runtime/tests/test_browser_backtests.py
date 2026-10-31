"""规则回测本机优先 (2026-10-12): the server's half of a backtest the browser computes.

The inputs route hands out exactly the setup the server's own run replays (the same inputs digest),
``run_rule`` over them gives the server's report, the result route wraps it with the server's
record of the request (never the browser's), refuses a stale engine or a ticket it no longer holds,
and the server's rerun over the same inputs is cross-checked against the browser's numbers.
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from test_strategy_backtest import cond, plan
from trader_runtime.browser_backtests import BrowserChecks, BrowserTickets, Ticket, inputs_digest
from trader_runtime.main import create_app
from trader_runtime.strategy_backtest import setup_from_inputs, setup_inputs
from trader_runtime.strategy_lab import engine_bundle
from trader_runtime.strategy_lab_engine import run_rule

HEADERS = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer bt-token"}
CROSS = plan(entry=[cond("金叉", "ma", "cross_up", {"indicator": "ma", "period": 20}, tf=300, period=5)],
             exit_=[cond("死叉", "ma", "cross_down", {"indicator": "ma", "period": 20}, tf=300, period=5)]).model_dump()


def app_for(tmp_path):
    return create_app(tmp_path / "browser.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="bt-token")


async def fetch_inputs(client, **body):
    response = await client.post("/api/strategy-backtests/inputs", json=body, headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_browser_result_is_the_servers_result_over_the_same_inputs(tmp_path):
    app = app_for(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            saved = (await client.post("/api/strategy-plans", json={"plan": CROSS})).json()
            # The raw route speaks gzip (a few MB of K-lines on the wire).
            raw = await client.post("/api/strategy-backtests/inputs", headers={"Accept-Encoding": "gzip"},
                                    json={"strategyId": saved["strategyId"], "symbol": "SHFE.au2612", "slippageTicks": 1})
            assert raw.headers["content-encoding"] == "gzip"
            served = await fetch_inputs(client, strategyId=saved["strategyId"], symbol="SHFE.au2612", slippageTicks=1)
            assert served["engineDigest"] == engine_bundle()["digest"]
            assert served["inputDigest"] == inputs_digest(served["inputs"])
            assert served["inputs"]["slippageTicks"] == 1 and served["inputs"]["symbol"] == "SHFE.au2612"
            report = json.loads(json.dumps(run_rule(served["inputs"])))
            posted = await client.post("/api/strategy-backtests/browser-result", json={
                "inputDigest": served["inputDigest"], "ticketId": served["ticketId"], "engineDigest": served["engineDigest"],
                "result": {**report, "strategyId": "forged", "source": "server", "symbol": "SHFE.rb2601"}, "runMs": 812})
            assert posted.status_code == 200, posted.text
            browser = posted.json()
            # The server's record of the request wins over anything the browser sent.
            assert browser["source"] == "browser" and browser["strategyId"] == saved["strategyId"]
            assert browser["symbol"] == "SHFE.au2612" and browser["params"]["slippageTicks"] == 1
            assert browser["inputDigest"] == served["inputDigest"] and browser["timings"]["runMs"] == 812
            assert browser["verdict"]["facts"] is not None and browser["backtestId"]
            stored = (await client.get("/api/strategy-backtests", params={"strategyId": saved["strategyId"]})).json()
            assert stored["items"][0]["backtestId"] == browser["backtestId"]
            # One result per ticket: handing it in again (a retry after a lost reply) returns the same stored
            # result — no second backtestId, no second row.
            again = await client.post("/api/strategy-backtests/browser-result", json={
                "inputDigest": served["inputDigest"], "ticketId": served["ticketId"],
                "engineDigest": served["engineDigest"], "result": report})
            assert again.status_code == 200 and again.json() == browser
            assert len((await client.get("/api/strategy-backtests",
                                         params={"strategyId": saved["strategyId"]})).json()["items"]) == 1
            # The server's rerun (保存后的权威复核) reads the same inputs, gives the same numbers and says so.
            server = (await client.post(f"/api/strategies/{saved['strategyId']}/backtest",
                                        json={"symbol": "SHFE.au2612", "slippageTicks": 1, "fallbackReason": "rerun"})).json()
            assert server["source"] == "server" and server["fallbackReason"] == "rerun"
            assert server["inputDigest"] == served["inputDigest"]
            assert server["summary"] == browser["summary"] and server["trades"] == browser["trades"]
            assert server["browserCheck"] == {"match": True}


@pytest.mark.asyncio
async def test_preview_inputs_stay_unsaved_and_a_stale_engine_or_unknown_inputs_are_refused(tmp_path):
    app = app_for(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            served = await fetch_inputs(client, plan=CROSS, symbol="SHFE.au2612", tradingDays=60, preview=True)
            report = run_rule(served["inputs"])
            stale = await client.post("/api/strategy-backtests/browser-result", json={
                "inputDigest": served["inputDigest"], "ticketId": served["ticketId"], "engineDigest": "0" * 16,
                "result": report})
            assert stale.status_code == 409 and stale.json()["detail"] == "engine_changed"
            unknown = await client.post("/api/strategy-backtests/browser-result", json={
                "inputDigest": served["inputDigest"], "ticketId": "f" * 16, "engineDigest": served["engineDigest"],
                "result": report})
            assert unknown.status_code == 409 and unknown.json()["detail"] == "inputs_expired"
            wrong_digest = await client.post("/api/strategy-backtests/browser-result", json={
                "inputDigest": "f" * 16, "ticketId": served["ticketId"], "engineDigest": served["engineDigest"],
                "result": report})
            assert wrong_digest.status_code == 409 and wrong_digest.json()["detail"] == "inputs_expired"
            broken = await client.post("/api/strategy-backtests/browser-result", json={
                "inputDigest": served["inputDigest"], "ticketId": served["ticketId"],
                "engineDigest": served["engineDigest"], "result": {"summary": {}}})
            assert broken.status_code == 422
            done = (await client.post("/api/strategy-backtests/browser-result", json={
                "inputDigest": served["inputDigest"], "ticketId": served["ticketId"],
                "engineDigest": served["engineDigest"], "result": report})).json()
            assert done["source"] == "browser" and done["planSummary"] and done["params"]["tradingDays"] == 60
            assert (await client.get("/api/strategy-backtests")).json()["items"] == []
            # Neither a plan nor a strategy: nothing to read.
            assert (await client.post("/api/strategy-backtests/inputs", json={"symbol": "SHFE.au2612"})).status_code == 422
            # Another user's strategy is as missing as an unknown one.
            assert (await client.post("/api/strategy-backtests/inputs",
                                      json={"strategyId": "nope", "symbol": "SHFE.au2612"})).status_code == 404


def test_inputs_round_trip_and_tickets_have_their_own_ids_and_are_bounded():
    from test_backtest_template_parity import parity_history, variants
    setup = variants(parity_history())["double_ma"]
    inputs = json.loads(json.dumps(setup_inputs(setup)))
    assert setup_from_inputs(inputs) == setup
    tickets = BrowserTickets(kept=2)
    issued = [Ticket(user="u", digest="same", plan={}, options={}, symbol="s", identity={"n": index},
                     persist=True, preview=False, fee_source="custom", load_ms=0) for index in range(3)]
    for ticket in issued:
        tickets.issue(ticket)
    # Same user, same inputs digest: three tickets, three ids — none overwrites another's metadata.
    assert len({ticket.id for ticket in issued}) == 3
    assert tickets.get("u", issued[0].id) is None and tickets.get("other", issued[1].id) is None
    assert tickets.get("u", issued[1].id).identity == {"n": 1} and tickets.get("u", issued[2].id).identity == {"n": 2}
    assert tickets.get("u", issued[1].id) is not None          # reading does not consume it
    checks = BrowserChecks()
    checks.remember("u", "d", {"netProfit": 1})
    assert checks.check("u", {"inputDigest": "d", "summary": {"netProfit": 2}}) == {"match": False, "browserSummary": {"netProfit": 1}}
    assert checks.check("u", {"inputDigest": "x", "summary": {}}) is None


@pytest.mark.asyncio
async def test_two_requests_over_the_same_inputs_keep_their_own_metadata(tmp_path):
    """#510：同用户同输入的两次请求各有票据，结果存到各自的策略标识下，互不覆盖。"""
    app = app_for(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            first = await fetch_inputs(client, plan=CROSS, symbol="SHFE.au2612", strategyId="s-first")
            second = await fetch_inputs(client, plan=CROSS, symbol="SHFE.au2612", strategyId="s-second")
            assert first["inputDigest"] == second["inputDigest"] and first["ticketId"] != second["ticketId"]
            report = json.loads(json.dumps(run_rule(first["inputs"])))
            results = {}
            for name, served in (("second", second), ("first", first)):
                posted = await client.post("/api/strategy-backtests/browser-result", json={
                    "inputDigest": served["inputDigest"], "ticketId": served["ticketId"],
                    "engineDigest": served["engineDigest"], "result": report})
                assert posted.status_code == 200, posted.text
                results[name] = posted.json()
            assert results["first"]["strategyId"] == "s-first" and results["second"]["strategyId"] == "s-second"
            assert results["first"]["backtestId"] != results["second"]["backtestId"]


@pytest.mark.asyncio
async def test_a_concurrent_second_hand_in_of_one_ticket_gets_the_first_result(tmp_path):
    """#510：回包丢了的重试可能与第一次同时到达：同一张票据只存一次，两次拿到同一个 backtestId。"""
    import asyncio

    app = app_for(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            served = await fetch_inputs(client, plan=CROSS, symbol="SHFE.au2612", strategyId="s-once")
            report = json.loads(json.dumps(run_rule(served["inputs"])))
            body = {"inputDigest": served["inputDigest"], "ticketId": served["ticketId"],
                    "engineDigest": served["engineDigest"], "result": report}
            one, two = await asyncio.gather(client.post("/api/strategy-backtests/browser-result", json=body),
                                            client.post("/api/strategy-backtests/browser-result", json=body))
            assert one.status_code == two.status_code == 200
            assert one.json()["backtestId"] == two.json()["backtestId"]
            stored = (await client.get("/api/strategy-backtests", params={"strategyId": "s-once"})).json()["items"]
            assert [item["backtestId"] for item in stored] == [one.json()["backtestId"]]


@pytest.mark.asyncio
async def test_the_inputs_route_takes_a_backtest_queue_slot(tmp_path):
    """#510：读历史的 inputs 与服务端回测同一并发上限；没有空位排队超时是 429，不绕过队列。"""
    app = app_for(tmp_path)
    async with app.router.lifespan_context(app):
        queue = app.state.strategy_backtest_queue
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            queue.global_limit, queue.wait_seconds = 0, 0.05
            refused = await client.post("/api/strategy-backtests/inputs", json={"plan": CROSS, "symbol": "SHFE.au2612"})
            assert refused.status_code == 429
            queue.global_limit, queue.wait_seconds = 2, 5
            assert (await client.post("/api/strategy-backtests/inputs",
                                      json={"plan": CROSS, "symbol": "SHFE.au2612"})).status_code == 200
            assert queue.limits()["running"] == 0 and queue.limits()["queued"] == 0


@pytest.mark.asyncio
async def test_compare_on_the_browser_waits_for_the_page_and_records_why_the_cloud_ran_it(tmp_path):
    app = app_for(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            saved = (await client.post("/api/strategy-plans", json={"plan": CROSS})).json()
            view = (await client.post(f"/api/strategy-plans/{saved['strategyId']}/compare",
                                      json={"symbol": "SHFE.au2612", "runOn": "browser"})).json()
            assert view["status"] == "ready" and view["source"] is None and view["fallbackReason"] is None
            lab = await client.get(f"/api/strategy-labs/{view['labId']}/inputs", headers={"Accept-Encoding": "gzip"})
            assert lab.status_code == 200
            started = (await client.post(f"/api/strategy-labs/{view['labId']}/run-server",
                                         json={"fallbackReason": "hidden"})).json()
            assert started["status"] == "running" and started["request"]["fallbackReason"] == "hidden"
            assert (await client.get(f"/api/strategy-plans/{saved['strategyId']}/compare")).json()["fallbackReason"] == "hidden"


@pytest.mark.asyncio
async def test_the_rollback_switch_sends_every_interface_backtest_to_the_server(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_TRADER_BROWSER_BACKTESTS", "0")
    app = app_for(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            refused = await client.post("/api/strategy-backtests/inputs", json={"plan": CROSS, "symbol": "SHFE.au2612"})
            assert refused.status_code == 503 and refused.json()["detail"] == "browser_backtests_disabled"
            saved = (await client.post("/api/strategy-plans", json={"plan": CROSS})).json()
            view = (await client.post(f"/api/strategy-plans/{saved['strategyId']}/compare",
                                      json={"symbol": "SHFE.au2612", "runOn": "browser"})).json()
            assert view["status"] == "running"


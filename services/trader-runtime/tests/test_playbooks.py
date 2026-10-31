"""打法本 (2026-10-25): the user's setups at the HTTP route (create / list / update / delete, the per-user cap, a
name used twice, a strategy link that is not the user's), a card tagged with a setup carrying it into the trade
review's order sources (a deleted setup names none), an unknown setup refused at the proposal route, and the
routes owned by the strategy skill in the capability catalog."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient

from test_price_alerts import AlertProvider, login
from test_simulation_orders import USER, Gateway, application
from trader_runtime.routes.account import PLAYBOOK_MAX
from trader_runtime.skill_registry import route_owner_map
from trader_runtime.strategy_workbench import order_sources

RULES = {"entryRules": "突破开盘半小时高点，量能 1.5 倍以上", "exitRules": "5 分钟线收在区间下四分之一就走", "stopRules": "半小时低点"}


async def test_create_list_update_delete_and_each_refusal(tmp_path: Path) -> None:
    app = application(tmp_path / "playbooks.sqlite3", Gateway(), AlertProvider())
    http = await login(app)

    created = await http.post("/api/playbooks", json={"name": " 开盘突破 ", **RULES})
    assert created.status_code == 201, created.text
    playbook = created.json()["playbook"]
    assert playbook["name"] == "开盘突破" and playbook["entryRules"] == RULES["entryRules"] and playbook["strategyId"] is None

    assert (await http.post("/api/playbooks", json={"name": "开盘突破"})).status_code == 409   # a name used twice
    assert (await http.post("/api/playbooks", json={"name": "  "})).status_code == 422          # a name is required
    assert (await http.post("/api/playbooks", json={"name": "x", "wat": 1})).status_code == 422  # extra=forbid
    linked = await http.post("/api/playbooks", json={"name": "均线回踩", "strategyId": "not-mine"})
    assert linked.status_code == 422 and "策略" in linked.json()["detail"]

    listed = (await http.get("/api/playbooks")).json()
    assert [item["name"] for item in listed["playbooks"]] == ["开盘突破"] and listed["max"] == PLAYBOOK_MAX

    updated = await http.put(f"/api/playbooks/{playbook['id']}", json={"name": "开盘区间突破", **RULES, "stopRules": "区间低点"})
    assert updated.status_code == 200 and updated.json()["playbook"]["stopRules"] == "区间低点"
    assert (await http.put("/api/playbooks/missing", json={"name": "x"})).status_code == 404

    assert (await http.delete(f"/api/playbooks/{playbook['id']}")).status_code == 200
    assert (await http.delete(f"/api/playbooks/{playbook['id']}")).status_code == 404
    assert (await http.get("/api/playbooks")).json()["playbooks"] == []

    for index in range(PLAYBOOK_MAX):
        assert (await http.post("/api/playbooks", json={"name": f"打法{index}"})).status_code == 201
    full = await http.post("/api/playbooks", json={"name": "再来一个"})
    assert full.status_code == 409 and f"最多 {PLAYBOOK_MAX}" in full.json()["detail"]


def link(store: Any, session_id: str, proposal_id: str, client_order_id: str, extra: dict[str, Any]) -> None:
    now = datetime.now(UTC).isoformat()
    store.create(proposal_id=proposal_id, session_id=session_id, proposal_type="trade", status="executed",
                 payload={"symbol": "SHFE.rb2701", **extra}, user_id=USER)
    store.connection.execute("""INSERT INTO tqkq_sim_order_links (id, user_id, proposal_id, client_order_id, request_json,
        submit_phase, cancel_phase, created_at, updated_at) VALUES (?, ?, ?, ?, '{}', 'CONFIRMED', 'NONE', ?, ?)""",
        (f"link-{proposal_id}", USER, proposal_id, client_order_id, now, now))
    store.connection.commit()


async def test_a_tagged_card_carries_its_setup_into_the_order_sources(tmp_path: Path) -> None:
    app = application(tmp_path / "playbook-sources.sqlite3", Gateway(), AlertProvider())
    store = app.state.store
    session = app.state.broker.create_session("local", "native", USER)
    breakout = store.create_playbook(user_id=USER, fields={"name": "开盘突破", **RULES}, max_count=PLAYBOOK_MAX)
    pullback = store.create_playbook(user_id=USER, fields={"name": "均线回踩", "entryRules": "", "exitRules": "", "stopRules": ""},
                                     max_count=PLAYBOOK_MAX)
    link(store, session.session_id, "p-ticket", "cli-ticket", {"reason": "下单票"})
    link(store, session.session_id, "p-card", "cli-card", {"origin": "ai", "reason": "回踩均线企稳"})
    link(store, session.session_id, "p-plain", "cli-plain", {"reason": "下单票"})
    link(store, session.session_id, "p-gone", "cli-gone", {"reason": "下单票"})
    store.tag_proposal_playbook("p-ticket", breakout["id"], user_id=USER)
    store.tag_proposal_playbook("p-card", pullback["id"], user_id=USER)
    gone = store.create_playbook(user_id=USER, fields={"name": "删掉的", "entryRules": "", "exitRules": "", "stopRules": ""},
                                 max_count=PLAYBOOK_MAX)
    store.tag_proposal_playbook("p-gone", gone["id"], user_id=USER)
    store.delete_playbook(gone["id"], user_id=USER)

    sources = order_sources(store, USER)
    assert sources["cli-ticket"] == {"source": "manual", "playbookId": breakout["id"], "playbookName": "开盘突破"}
    assert sources["cli-card"] == {"source": "card", "reason": "回踩均线企稳", "playbookId": pullback["id"], "playbookName": "均线回踩"}
    assert "cli-plain" not in sources   # an untagged ticket order still reads as 手动 by its absence
    assert "cli-gone" not in sources    # a deleted setup names none


async def test_the_proposal_route_refuses_a_setup_that_is_not_the_users(tmp_path: Path) -> None:
    app = application(tmp_path / "playbook-route.sqlite3", Gateway(), AlertProvider())
    http = await login(app)
    session = app.state.broker.create_session("local", "native", USER)
    refused = await http.post("/api/simulation/proposals", json={
        "sessionId": session.session_id, "symbol": "SHFE.rb2701", "side": "buy_open", "volume": 1,
        "reason": "下单票", "playbookId": "someone-elses"})
    assert refused.status_code == 422 and refused.json()["detail"] == "playbook_not_found"


async def test_a_ticket_order_placed_under_a_setup_tags_its_card(tmp_path: Path) -> None:
    app = application(tmp_path / "playbook-ticket.sqlite3", Gateway())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        session = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
        playbook = (await client.post("/api/playbooks", json={"name": "开盘突破", **RULES})).json()["playbook"]
        placed = await client.post("/api/simulation/proposals", json={
            "sessionId": session["sessionId"], "symbol": "SHFE.au2612", "side": "buy_open", "volume": 1,
            "priceIntent": "passive", "reason": "下单票", "playbookId": playbook["id"]})
        assert placed.status_code == 201, placed.text
        proposal_id = placed.json()["proposalId"]
    row = app.state.store.connection.execute(
        "SELECT playbook_id, user_id FROM proposal_playbooks WHERE proposal_id = ?", (proposal_id,)).fetchone()
    assert row is not None and row["playbook_id"] == playbook["id"] and row["user_id"] == USER
    app.state.store.close()


def test_the_routes_belong_to_their_skills() -> None:
    owners = route_owner_map()
    for route in ("GET /api/playbooks", "POST /api/playbooks", "PUT /api/playbooks/{playbook_id}", "DELETE /api/playbooks/{playbook_id}"):
        assert owners[route] == "strategy-management"
    for route in ("PUT /api/account/trade-playbooks/{round_id}", "DELETE /api/account/trade-playbooks/{round_id}"):
        assert owners[route] == "review-summary"


def beijing_ns(day: str, clock: str) -> int:
    return int(datetime.fromisoformat(f"{day}T{clock}:00+08:00").timestamp() * 1e9)


def round_trip(index: int, *, open_at: tuple[str, str], day: str, net: float, playbook: str | None = None,
               symbol: str = "SHFE.rb2701") -> dict[str, Any]:
    trade: dict[str, Any] = {"id": f"r{index}", "symbol": symbol, "openTime": beijing_ns(*open_at),
                             "closeTime": beijing_ns(*open_at) + 600_000_000_000, "tradingDay": day, "net": net, "gross": net,
                             "volume": 1, "direction": "long", "holdingMinutes": 10}
    if playbook:
        trade["playbookName"] = playbook
    return trade


def test_the_breakdown_groups_by_setup_trading_block_and_weekday() -> None:
    from trader_runtime.memory_stats import review_breakdown

    rounds = [
        round_trip(1, open_at=("2026-10-23", "09:05"), day="2026-10-23", net=300, playbook="开盘突破"),
        round_trip(2, open_at=("2026-10-23", "09:40"), day="2026-10-23", net=-100, playbook="开盘突破"),
        round_trip(3, open_at=("2026-10-23", "13:45"), day="2026-10-23", net=-200),
        # A Thursday-night open belongs to Friday's trading day.
        round_trip(4, open_at=("2026-10-22", "21:30"), day="2026-10-23", net=50, playbook="均线回踩"),
        round_trip(5, open_at=("2026-10-20", "23:30"), day="2026-10-21", net=80),
        # A foreign (CME) month has no domestic trading block, but still counts for its weekday.
        round_trip(6, open_at=("2026-10-21", "10:40"), day="2026-10-21", net=-20, symbol="CME.GCEZ26"),
    ]
    result = review_breakdown(rounds)
    assert [(row["label"], row["trades"], row["wins"], row["net"]) for row in result["byPlaybook"]] == [
        ("开盘突破", 2, 1, 200), ("均线回踩", 1, 1, 50), ("未标笔记", 3, 1, -140)]
    assert [(row["label"], row["trades"]) for row in result["byTimeBlock"]] == [
        ("夜盘 21–22 点", 1), ("夜盘 23 点后", 1), ("9:00–10:15", 2), ("13:30–15:00", 1)]
    assert [(row["label"], row["trades"], row["winRate"]) for row in result["byWeekday"]] == [("周三", 2, 50), ("周五", 4, 50)]


def test_a_setup_or_block_well_off_the_overall_win_rate_becomes_a_lesson() -> None:
    from trader_runtime.memory_stats import trade_stats

    # Six afternoon 均线回踩 trades all lose, ten 9:00 trades all win: both groups clear the same ≥5-trade,
    # far-from-overall bar as every other lesson; the winning block is a lesson too, the small groups are not.
    rounds = [round_trip(i, open_at=("2026-10-23", "13:45"), day="2026-10-23", net=-100, playbook="均线回踩") for i in range(6)]
    rounds += [round_trip(10 + i, open_at=("2026-10-23", "09:05"), day="2026-10-23", net=200) for i in range(10)]
    keys = {entry["key"]: entry["text"] for entry in trade_stats(rounds, names={}, now_ns=beijing_ns("2026-10-24", "12:00"))}
    assert "stat:playbook:均线回踩" in keys and "胜率 0%" in keys["stat:playbook:均线回踩"]
    assert "stat:block:13:30–15:00" in keys and "stat:block:9:00–10:15" in keys
    assert not any(key.startswith("stat:weekday:") for key in keys)  # every trade is a Friday: no gap to the overall


async def test_a_closed_round_trip_is_tagged_afterwards_and_the_breakdown_follows(tmp_path: Path) -> None:
    from test_strategy_protection import Provider
    from test_trade_review import fill

    provider = Provider()
    app = application(tmp_path / "playbook-tags.sqlite3", Gateway(), provider)
    today = datetime.now(UTC).astimezone().strftime("%Y-%m-%d")
    provider.activity["trades"] = [fill("rb-open", "SHFE.rb2701", "BUY", "OPEN", 3200.0, today, 9),
                                   fill("rb-close", "SHFE.rb2701", "SELL", "CLOSETODAY", 3210.0, today, 10)]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"Origin": "http://127.0.0.1:5173"}) as http:
            assert (await http.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
            review = (await http.get("/api/trade-review")).json()
            round_id = review["trades"][0]["id"]
            assert "playbookName" not in review["trades"][0]
            assert review["breakdown"]["byPlaybook"] == [
                {"label": "未标笔记", "trades": 1, "wins": 1, "winRate": 100, "net": 10000.0, "average": 10000.0}]
            playbook = (await http.post("/api/playbooks", json={"name": "开盘突破", **RULES})).json()["playbook"]

            assert (await http.put(f"/api/account/trade-playbooks/{round_id}", json={"playbookId": "nope"})).status_code == 422
            assert (await http.put("/api/account/trade-playbooks/missing", json={"playbookId": playbook["id"]})).status_code == 404
            tagged = await http.put(f"/api/account/trade-playbooks/{round_id}", json={"playbookId": playbook["id"]})
            assert tagged.status_code == 200 and tagged.json()["playbookName"] == "开盘突破"
            review = (await http.get("/api/trade-review")).json()
            assert review["trades"][0]["playbookName"] == "开盘突破"
            assert [row["label"] for row in review["breakdown"]["byPlaybook"]] == ["开盘突破"]

            assert (await http.delete(f"/api/account/trade-playbooks/{round_id}")).json()["cleared"] is True
            assert "playbookName" not in (await http.get("/api/trade-review")).json()["trades"][0]
    finally:
        app.state.store.close()

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from trader_runtime.broker import EventBroker
from trader_runtime.data_providers import SidecarReadOnlyDataProvider, fake_account_summary, fake_market_workbench
from trader_runtime.main import create_app
from trader_runtime.order_boundaries import SimulationOrderError, quote_is_fresh
from trader_runtime.replay import ReplayProvider, copy_replay_configuration, replay_day_refusal
from trader_runtime.sidecar_http import SidecarHttpError
from trader_runtime.store import RuntimeStore

IDENTIFIER = "a" * 32
SYMBOL = "SHFE.au2612"


class HistoricalSource(SidecarReadOnlyDataProvider):
    """Synthetic sidecar boundary fixture; never used by the application."""
    def __init__(self):
        super().__init__(base_url="http://127.0.0.1:9999", token="fixture-token-" * 3, symbols=[SYMBOL])
        clock = "2026-09-03T13:00:00+00:00"
        workbenches = {str(duration): fake_market_workbench(SYMBOL, duration)
                      for duration in (60, 180, 300, 900, 3600, 86400)}
        for chart in workbenches.values():
            chart["quote"].update(symbol=SYMBOL, quoteDatetime=clock, priceTick=.02, volumeMultiple=1000,
                                  bidPrice1=823.48, askPrice1=823.52, lastPrice=823.5)
            # 仿 sidecar 的回放源不带交易时段（fake_market_workbench 现在带）：历史分钟数据
            # 没有“今天几点收盘”，拿不到时段就拒绝收盘前平仓的用例仍要成立。
            chart["quote"].pop("tradingSchedule", None)
        self.data = {
            "replayId": IDENTIFIER, "accountType": "TqSim", "liveTrading": "disabled", "status": "ready",
            "symbol": SYMBOL, "tradingDay": "2026-09-04", "clock": clock, "precision": "minute-derived-quotes",
            "stepInFlight": None,
            "account": {**fake_account_summary(""), "equity": 1_000_000, "tradingDay": "2026-09-04", "todayNetPnl": 0},
            "market": {**workbenches["60"]["quote"], "price": 823.5, "lowerLimit": None, "upperLimit": None},
            "positions": [], "orders": [], "trades": [], "workbenches": workbenches,
            "instrument": {"symbol": SYMBOL, "exchangeId": "SHFE", "instrumentId": "au2612", "productId": "au",
                           "instrumentName": "沪金2612", "priceTick": .02, "volumeMultiple": 1000, "isMain": True},
        }
        self.submits = []
        self.reports = {}
        self.creates = []
        self.failure = False
        self.closes = []  # identifiers the sidecar was told to close
        self.gone = False  # the sidecar no longer knows this replay
        self.http_error = None  # an HTTP status the sidecar answers replay requests with
        # 交易日历 (2026-10-03): the calendar the create route consults before forwarding.
        self.calendar = {"tradingDay": "2026-09-07", "previousTradingDay": "2026-09-04",
                         "recentTradingDays": ["2026-09-04", "2026-09-03", "2026-09-02", "2026-09-01",
                                               "2026-08-31", "2026-08-28", "2026-08-27", "2026-08-26",
                                               "2026-08-25", "2026-08-24"]}

    async def start(self):
        pass

    async def stop(self):
        pass

    def account_summary(self, turn_id):
        return {**fake_account_summary(turn_id), "equity": 3_000_000}

    def _request(self, method, path, payload=None, **kwargs):
        # The live app's startup ledger prefetches the activity feed, like the real sidecar.
        if path == "/v1/activity":
            assert method == "GET"
            return {"schemaVersion": 1, "orders": [], "trades": []}
        if path == "/v1/public-market/calendar":
            assert method == "GET"
            return self.calendar
        assert path == "/v1/replays" or path.startswith(f"/v1/replays/{IDENTIFIER}/")
        if self.failure:
            raise TimeoutError("synthetic transport outage")
        if self.http_error is not None and path != "/v1/replays":
            raise SidecarHttpError("sidecar_http_error", status=self.http_error,
                                  payload={"error": "invalid_replay_request" if self.http_error == 422 else "unavailable"})
        action = path.rsplit("/", 1)[-1]
        if path == "/v1/replays" and method == "POST":
            refusal = getattr(self, "create_refusal", None)
            if refusal is not None:
                raise SidecarHttpError("sidecar_http_error", status=422, payload=refusal)
            self.creates.append(payload)
        if action == "close":
            self.closes.append(IDENTIFIER)
            return {"status": "closed"}
        if self.gone:
            # The real sidecar answers an unknown replay id with 422.
            raise SidecarHttpError("sidecar_http_error", status=422, payload={"error": "invalid_replay_request"})
        if action == "submit":
            self.submits.append(payload)
            self.reports[payload["clientOrderId"]] = {"ordersInitialized": True, "found": True,
                "order": {**payload, "volumeLeft": payload["volume"], "status": "ALIVE", "isOnline": True}}
            return {"ordersInitialized": True, "found": False}
        if action == "query":
            return copy.deepcopy(self.reports[payload["clientOrderId"]])
        # #932 B3: like the sidecar, an advance step id runs once (`executed` lists the ids that
        # stepped). advance_failure: "answer-lost" (stepped, the answer lost), "not-arrived" (lost
        # before the sidecar took it — what a state read overtaking the advance sees),
        # "part-way" (the sidecar answers incomplete); `legacy` drops step ids altogether.
        if getattr(self, "legacy", False):
            self.data.pop("stepInFlight", None)
        if action == "advance":
            step_id = (payload or {}).get("stepId")
            results = self.__dict__.setdefault("results", {})
            executed = self.__dict__.setdefault("executed", [])
            failure, self.advance_failure = getattr(self, "advance_failure", None), None
            if failure == "not-arrived":
                raise TimeoutError("synthetic: the advance never reached the sidecar")
            if not getattr(self, "legacy", False) and step_id in results:
                if results[step_id] is not None:
                    raise results[step_id]
                return copy.deepcopy(self.data)
            if getattr(self, "running", None) == step_id:
                self.running, self.data["stepInFlight"] = None, None  # the adopted step finishes
            if failure == "part-way":
                self.data.update(status="incomplete", error="replay_advance_incomplete")
                results[step_id] = SidecarHttpError("sidecar_http_error", status=409,
                                                    payload={"error": "replay_command_failed"})
                raise results[step_id]
            executed.append(step_id)
            results[step_id] = None
            self.data.update(status="ready")
            self.data.pop("error", None)
            self.data["clock"] = (datetime.fromisoformat(self.data["clock"]) + timedelta(minutes=1)).isoformat()
            self.data["market"]["quoteDatetime"] = self.data["clock"]
            if failure == "answer-lost":
                raise TimeoutError("synthetic lost answer")
            if failure == "command-unknown":  # the actor's 5 s answer window ran out
                raise SidecarHttpError("sidecar_http_error", status=409, payload={"error": "replay_command_unknown"})
            if failure == "server-error":  # anything not on the allowlist is no answer either
                raise SidecarHttpError("sidecar_http_error", status=502, payload={})
        return copy.deepcopy(self.data)

    async def _request_async(self, method, path, payload=None, **kwargs):
        return self._request(method, path, payload, **kwargs)


async def test_replay_refuses_a_non_trading_day_before_the_sidecar_is_called(tmp_path: Path):
    """回放默认日期与休市拒绝 (2026-10-03): the create route consults the sidecar's
    TqSdk calendar before forwarding — a holiday answers 422 in Chinese naming the
    nearest trading day, and the sidecar is never asked for the replay."""
    source = HistoricalSource()
    source.calendar = {"tradingDay": "2026-10-05", "previousTradingDay": "2026-09-30",
                       "recentTradingDays": ["2026-09-30", "2026-09-29", "2026-09-28", "2026-09-25",
                                             "2026-09-24", "2026-09-23", "2026-09-22", "2026-09-21",
                                             "2026-09-18", "2026-09-17"]}
    app = create_app(tmp_path / "holiday.sqlite3", runtime_state_root=tmp_path, data_mode="tqkq-sidecar", data_provider=source,
                     agent_mode="fake", local_token="test-replay-token")
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            holiday = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-10-01", "initialBalance": 1_000_000})
            assert holiday.status_code == 422, holiday.text
            assert holiday.json()["detail"] == "10 月 1 日休市，没有行情；最近的交易日是 9 月 30 日"
            weekend = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-26", "initialBalance": 1_000_000})
            assert weekend.status_code == 422 and "9 月 25 日" in weekend.json()["detail"]
            unfinished = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-10-05", "initialBalance": 1_000_000})
            assert unfinished.status_code == 422 and "还没有走完" in unfinished.json()["detail"]
            assert source.creates == []  # refused before the forward, every time
            # A trading day the calendar certifies passes through to the sidecar.
            ok = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-29", "initialBalance": 1_000_000})
            assert ok.status_code == 201, ok.text
            assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "close"})).status_code == 200
            # A weekday older than the calendar's certified window is the sidecar's own
            # call (deep history), never a calendar refusal.
            deep = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})
            assert deep.status_code == 201, deep.text
            assert [create["tradingDay"] for create in source.creates] == ["2026-09-29", "2026-09-04"]


def test_the_current_trading_day_is_refused_only_until_the_calendar_says_it_closed():
    """#859 Codex P2 / #754：sidecar 北京 15:30 后放行当天，Runtime 却按日历拒到 18:00。现在 Runtime 读日历的
    latestClosedTradingDay（sidecar 同一条 15:30 规则）；旧 sidecar 不送这个字段时照旧拒到换日。"""
    calendar = {"tradingDay": "2026-10-13", "previousTradingDay": "2026-10-12", "recentTradingDays": ["2026-10-12"]}
    assert "还没有走完" in replay_day_refusal("2026-10-13", calendar)
    assert "还没有走完" in replay_day_refusal("2026-10-13", {**calendar, "latestClosedTradingDay": "2026-10-12"})
    assert replay_day_refusal("2026-10-13", {**calendar, "latestClosedTradingDay": "2026-10-13"}) is None
    assert replay_day_refusal("2026-10-12", {**calendar, "latestClosedTradingDay": "2026-10-13"}) is None


async def test_the_sidecars_unfinished_day_refusal_reaches_the_user_in_plain_chinese(tmp_path: Path):
    """10-13 夜盘验收：默认交易日 10-13 被 sidecar 以“还没收盘”拒绝，Runtime 却包成 409
    replay_start_failed_check_credentials_date，页面只说“无法确认回放创建完成”。sidecar 点名的这条拒绝改为
    422 中文：哪天、为什么、可以选哪天；其他失败仍是原来的 409。"""
    source = HistoricalSource()
    # 21:42 on 10-13: the night session already belongs to 10-14, so 10-13 is the latest ended day.
    source.calendar = {"tradingDay": "2026-10-14", "previousTradingDay": "2026-10-13",
                       "recentTradingDays": ["2026-10-13", "2026-10-12", "2026-10-09"]}
    source.create_refusal = {"error": "invalid_replay_request", "reason": "replay_requires_completed_day"}
    app = create_app(tmp_path / "unfinished.sqlite3", runtime_state_root=tmp_path, data_mode="tqkq-sidecar", data_provider=source,
                     agent_mode="fake", local_token="test-replay-token")
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            refused = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-10-13", "initialBalance": 1_000_000})
            assert refused.status_code == 422, refused.text
            assert refused.json()["detail"] == "10 月 13 日 的行情还没收盘（白盘 15:00 结束后才能回放），可以选更早的交易日"
            source.create_refusal = {"error": "invalid_replay_request"}  # any other refusal: unchanged
            other = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-10-13", "initialBalance": 1_000_000})
            assert other.status_code == 409 and other.json()["detail"] == "replay_start_failed_check_credentials_date"
            assert source.creates == []


async def test_replay_http_orders_and_accounts_are_isolated(tmp_path: Path, monkeypatch):
    # An enabled live environment must not override the injected replay gateway.
    monkeypatch.setenv("AI_TRADER_SIMULATION_TRADING", "1")
    source = HistoricalSource()
    app = create_app(tmp_path / "live.sqlite3", runtime_state_root=tmp_path, data_mode="tqkq-sidecar", data_provider=source,
                     agent_mode="fake", local_token="test-replay-token")
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})
            assert created.status_code == 201, created.text
            prefix = created.json()["runtimePath"]
            # Let the child consume a state through its normal authenticated source.
            await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "step"})
            assert (await client.get("/api/account/summary")).json()["equity"] == 3_000_000
            assert (await client.get(prefix + "/api/account/summary")).json()["equity"] == 1_000_000
            session = (await client.post(prefix + "/api/sessions", json={"mode": "local"})).json()
            proposal = await client.post(prefix + "/api/simulation/proposals", json={
                "sessionId": session["sessionId"], "symbol": SYMBOL, "side": "buy_open", "volume": 1,
                "priceIntent": "aggressive", "reason": "replay isolation test",
            })
            assert proposal.status_code == 201, proposal.text
            item = proposal.json()
            confirmation = {"approvalId": item["approvalId"], "proposalVersion": item["version"], "idempotencyKey": "replay-confirm-0001"}
            result = await client.post(prefix + f"/api/proposals/{item['proposalId']}/confirm", json=confirmation)
            assert result.status_code == 200, result.text
            assert len(source.submits) == 1
            assert app.state.simulation_orders.list(user_id="local-user") == []
            repeated = await client.post(prefix + f"/api/proposals/{item['proposalId']}/confirm", json=confirmation)
            assert repeated.status_code == 200
            assert len(source.submits) == 1
            assert (await client.post(f"/api/proposals/{item['proposalId']}/confirm", json=confirmation)).status_code == 404
            account = await client.get(prefix + "/api/agent/account")
            assert account.status_code == 200 and account.json()["authMode"] == "fake"
            assert (await client.post(prefix + "/api/agent/login")).status_code == 403
            assert (await client.get(prefix + "/api/account/summary", headers={"Authorization": "Bearer wrong"})).status_code == 401
            assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "close"})).status_code == 200
            assert (await client.get(prefix + "/api/account/summary")).status_code == 404
            assert (await client.get("/api/account/summary")).json()["equity"] == 3_000_000


def build_slot(path: Path, source, **kwargs):
    return create_app(path, runtime_state_root=path.parent, data_mode="tqkq-sidecar", data_provider=source,
                      agent_mode="fake", local_token="test-replay-token", **kwargs)


async def test_shutdown_detaches_the_replay_and_the_next_slot_adopts_it_paused(tmp_path: Path):
    # A blue-green slot switch: slot A's lifespan exit must not close the sidecar
    # replay, and slot B must adopt it (paused) from the durable record and the
    # child database — never two processes advancing one replay.
    source = HistoricalSource()
    slot_a = build_slot(tmp_path / "live-a.sqlite3", source)
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    old_context = None
    async with slot_a.router.lifespan_context(slot_a):
        async with AsyncClient(transport=ASGITransport(slot_a), base_url="http://runtime", headers=headers) as client:
            created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})
            assert created.status_code == 201, created.text
            played = await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "play"})
            assert played.json()["playing"] is True
        old_context = slot_a.state.replay_router.contexts[IDENTIFIER]
    # Lifespan exit = detach: the sidecar replay stays, the loop is stopped and
    # joined, and the old slot can no longer drive the replay.
    assert source.closes == []
    assert old_context.task.done() and not old_context.playing and old_context.closed
    with pytest.raises(HTTPException):
        old_context.require_open("local-user")
    assert (tmp_path / "replays" / IDENTIFIER / "replay.json").exists()

    slot_b = build_slot(tmp_path / "live-b.sqlite3", source)
    async with slot_b.router.lifespan_context(slot_b):
        router = slot_b.state.replay_router
        with pytest.raises(HTTPException):
            await router.resolve(IDENTIFIER, "another-user")  # membership isolation survives adoption
        async with AsyncClient(transport=ASGITransport(slot_b), base_url="http://runtime", headers=headers) as client:
            listed = (await client.get("/api/replays")).json()["items"]
            assert [item["replayId"] for item in listed] == [IDENTIFIER]
            assert listed[0]["status"] == "loading"  # listed before adoption, the client finds it again
            detail = await client.get(f"/api/replays/{IDENTIFIER}")
            assert detail.status_code == 200, detail.text
            adopted = detail.json()
            assert adopted["status"] == "ready" and adopted["playing"] is False  # paused takeover
            summary = await client.get(f"/api/replays/{IDENTIFIER}/runtime/api/account/summary")
            assert summary.status_code == 200 and summary.json()["equity"] == 1_000_000
            # Only the new slot advances; the adopted replay plays on from paused.
            before = source.data["clock"]
            resumed = await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "step"})
            assert resumed.json()["playing"] is False
            assert source.data["clock"] > before


async def test_a_step_stopped_part_way_pauses_trading_until_the_minute_is_finished(tmp_path: Path):
    """#932 B3：sidecar 单步中途失败置 incomplete——回放暂停、读数不可交易（不再拿旧快照当 ready），
    单步会先走完这一分钟，恢复 ready 后才继续查单、保护和策略。"""
    source = HistoricalSource()
    app = build_slot(tmp_path / "incomplete.sqlite3", source)
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})
            assert created.status_code == 201, created.text
            prefix = created.json()["runtimePath"]
            source.advance_failure = "part-way"
            failed = await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "step"})
            assert failed.status_code == 503 and failed.json()["detail"] == "replay_step_unconfirmed"
            context = app.state.replay_router.contexts[IDENTIFIER]
            # Taken from the sidecar before the failure is answered, not at the loop's next poll.
            assert context.provider.snapshot["status"] == "incomplete"
            detail = (await client.get(f"/api/replays/{IDENTIFIER}")).json()
            assert detail["status"] == "incomplete" and detail["playing"] is False
            assert "只推进了一部分" in detail["notice"]
            assert context.provider.runtime_status()["simulationTrading"] is False
            assert (await client.get(prefix + "/api/account/summary")).status_code != 200
            # 平仓并停盘 reads the snapshot: refused until the minute is finished, never left ending.
            refused = await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "finish"})
            assert refused.status_code == 409 and refused.json()["detail"] == "replay_snapshot_unavailable"
            assert context.ending is False
            before = source.data["clock"]
            resumed = await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "step"})
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["status"] == "ready" and resumed.json()["notice"] is None
            assert source.data["clock"] > before
            assert (await client.get(prefix + "/api/account/summary")).json()["equity"] == 1_000_000


def _counting(context):
    """Record the replay clock each time the post-step strategy evaluation runs."""
    evaluated = []
    monitor = context.child.state.strategy_monitor
    original = monitor.run_once

    async def counted(*args, **kwargs):
        evaluated.append(context.provider.snapshot["clock"])
        return await original(*args, **kwargs)

    monitor.run_once = counted
    return evaluated


async def _create(client):
    created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})
    assert created.status_code == 201, created.text
    return created.json()["runtimePath"]


@pytest.mark.parametrize("failure", ["answer-lost", "not-arrived", "command-unknown", "server-error"])
async def test_a_lost_advance_is_resent_with_its_id_and_steps_only_once(tmp_path: Path, failure):
    """#932 B3 幂等单步：答复丢失（answer-lost）或单步还没到 sidecar、状态读先到（not-arrived）都不按状态猜，
    快照 unconfirmed 不可交易；后台用同一个步编号重发，sidecar 同一编号只执行一次——拿到结果后照常
    查单/保护/策略/提醒，这一分钟不跳过也不多走；迟到的原请求再来也不再走一步。"""
    source = HistoricalSource()
    app = build_slot(tmp_path / f"{failure}.sqlite3", source)
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            await _create(client)
            context = app.state.replay_router.contexts[IDENTIFIER]
            assert context.provider.step_ids_supported is True  # known from the creation answer
            before = context.provider.snapshot["clock"]
            evaluated = _counting(context)
            source.advance_failure = failure
            stepped = await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "step"})
            assert stepped.status_code == 503 and stepped.json()["detail"] == "replay_step_unconfirmed"
            step_id = context.provider.pending_step
            assert step_id is not None and context.provider.snapshot["status"] == "unconfirmed"
            assert context.provider.runtime_status()["simulationTrading"] is False
            # A state read never settles it, even one that shows ready.
            await context.provider.command("state")
            assert context.provider.snapshot["status"] == "unconfirmed" and evaluated == []
            detail = (await client.get(f"/api/replays/{IDENTIFIER}")).json()
            assert detail["status"] == "unconfirmed" and "正在重试" in detail["notice"]
            async with context.lock:  # what the background loop does next
                await context._resend_pending_step()
            snapshot = context.provider.snapshot
            assert context.provider.pending_step is None and snapshot["status"] == "ready"
            assert snapshot["clock"] > before and evaluated == [snapshot["clock"]]
            assert source.executed == [step_id]
            assert (await client.get(f"/api/replays/{IDENTIFIER}")).json()["notice"] is None
            # The original request arriving late is the same step: answered, never stepped again.
            source._request("POST", f"/v1/replays/{IDENTIFIER}/advance", {"stepId": step_id})
            assert source.executed == [step_id]


@pytest.mark.parametrize("trouble", ["bad-answer", "post-step-fails"])
async def test_a_step_is_settled_only_after_its_answer_and_post_step_work_succeed(tmp_path: Path, trouble):
    """#932 B3：重发拿到的答复校验不过（身份不符），或步后处理抛错，这一步都不算确认——仍 pending、
    unconfirmed；下一次同号重发通过后才清 pending、恢复 ready，步后处理跑完一次，单步只执行一次。"""
    source = HistoricalSource()
    app = build_slot(tmp_path / f"{trouble}.sqlite3", source)
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            await _create(client)
            context = app.state.replay_router.contexts[IDENTIFIER]
            evaluated = _counting(context)
            source.advance_failure = "answer-lost"
            assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "step"})).status_code == 503
            step_id = context.provider.pending_step
            if trouble == "bad-answer":
                source.data["replayId"] = "b" * 32
            else:
                monitor = context.child.state.strategy_monitor
                working = monitor.run_once

                async def failing(*args, **kwargs):
                    monitor.run_once = working
                    raise RuntimeError("synthetic post-step failure")

                monitor.run_once = failing
            async with context.lock:
                await context._resend_pending_step()
            assert context.provider.pending_step == step_id
            assert context.provider.snapshot["status"] == "unconfirmed"
            assert context.provider.runtime_status()["simulationTrading"] is False
            source.data["replayId"] = IDENTIFIER
            context.resend_at = 0.0
            async with context.lock:
                await context._resend_pending_step()
            assert context.provider.pending_step is None and context.provider.snapshot["status"] == "ready"
            assert source.executed == [step_id] and len(evaluated) == 1


async def test_an_older_sidecar_leaves_a_lost_advance_unconfirmed(tmp_path: Path):
    """#932 B3：旧 sidecar 不认步编号，不重发；保持 unconfirmed（不因读到 ready 清掉），提示退出回放重开。"""
    source = HistoricalSource()
    source.legacy = True
    app = build_slot(tmp_path / "legacy.sqlite3", source)
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            await _create(client)
            context = app.state.replay_router.contexts[IDENTIFIER]
            await context.provider.command("state")
            source.advance_failure = "answer-lost"
            assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "step"})).status_code == 503
            async with context.lock:
                await context._resend_pending_step()
                await context.provider.command("state")
            assert len(source.executed) == 1  # nothing resent
            assert context.provider.pending_step is not None and context.provider.snapshot["status"] == "unconfirmed"
            assert context.provider.runtime_status()["simulationTrading"] is False
            assert "退出回放" in (await client.get(f"/api/replays/{IDENTIFIER}")).json()["notice"]
            refused = await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "finish"})
            assert refused.status_code == 409 and context.ending is False


async def test_an_adopting_slot_resends_the_step_the_sidecar_still_runs(tmp_path: Path):
    """#932 B3：换槽时 sidecar 状态里的 stepInFlight 是唯一记录——新槽把它记为 pending、快照 unconfirmed，
    用同一编号重发等到结果，再在新槽跑这一分钟的查单/保护/策略/提醒，只走一步。"""
    source = HistoricalSource()
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    slot_a = build_slot(tmp_path / "step-a.sqlite3", source)
    async with slot_a.router.lifespan_context(slot_a):
        async with AsyncClient(transport=ASGITransport(slot_a), base_url="http://runtime", headers=headers) as client:
            await _create(client)
    # The old slot's advance is still queued/running in the sidecar when the new slot adopts.
    source.running = source.data["stepInFlight"] = "a" * 32
    before = source.data["clock"]
    slot_b = build_slot(tmp_path / "step-b.sqlite3", source)
    async with slot_b.router.lifespan_context(slot_b):
        async with AsyncClient(transport=ASGITransport(slot_b), base_url="http://runtime", headers=headers) as client:
            assert (await client.get(f"/api/replays/{IDENTIFIER}")).status_code == 200
            context = slot_b.state.replay_router.contexts[IDENTIFIER]
            evaluated = _counting(context)
            assert context.provider.pending_step == "a" * 32
            assert context.provider.snapshot["status"] == "unconfirmed"
            assert context.provider.runtime_status()["simulationTrading"] is False
            async with context.lock:
                await context._resend_pending_step()
            snapshot = context.provider.snapshot
            assert context.provider.pending_step is None and snapshot["status"] == "ready"
            assert snapshot["clock"] > before and evaluated == [snapshot["clock"]]
            assert source.executed == ["a" * 32]


async def test_a_finished_replay_stays_ended_across_an_adoption(tmp_path: Path):
    """R2：停盘终态不只在内存里——finish 落在 review.json，接管的下一个 slot 恢复 ended
    （加仓保持暂停、播放/单步被拒），已停盘的回放不会因部署换手重新开放。"""
    source = HistoricalSource()
    slot_a = build_slot(tmp_path / "end-a.sqlite3", source)
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with slot_a.router.lifespan_context(slot_a):
        async with AsyncClient(transport=ASGITransport(slot_a), base_url="http://runtime", headers=headers) as client:
            created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})
            assert created.status_code == 201, created.text
            finished = await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "finish"})
            assert finished.status_code == 200, finished.text
            assert finished.json()["ended"] is True and finished.json()["playing"] is False
    assert (tmp_path / "replays" / IDENTIFIER / "review.json").exists()

    slot_b = build_slot(tmp_path / "end-b.sqlite3", source)
    async with slot_b.router.lifespan_context(slot_b):
        async with AsyncClient(transport=ASGITransport(slot_b), base_url="http://runtime", headers=headers) as client:
            listed = (await client.get("/api/replays")).json()["items"]
            assert [item["replayId"] for item in listed] == [IDENTIFIER]
            assert listed[0]["ended"] is True and listed[0]["entriesPaused"] is True
            detail = await client.get(f"/api/replays/{IDENTIFIER}")
            assert detail.status_code == 200, detail.text
            adopted = detail.json()
            assert adopted["ended"] is True and adopted["entriesPaused"] is True and adopted["playing"] is False
            before = source.data["clock"]
            assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "step"})).status_code == 409
            assert source.data["clock"] == before


async def test_a_probe_error_keeps_the_record_an_explicit_gone_deletes_it(tmp_path: Path):
    """R3：probe 收到 5xx 只说明边车暂时没答上来——接管记录保留（新建也仍按一个回放拒绝）；
    只有 422/404（边车明确不认识这个回放）才删记录。"""
    source = HistoricalSource()
    slot_a = build_slot(tmp_path / "err-a.sqlite3", source)
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with slot_a.router.lifespan_context(slot_a):
        async with AsyncClient(transport=ASGITransport(slot_a), base_url="http://runtime", headers=headers) as client:
            assert (await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})).status_code == 201
    source.http_error = 503  # the sidecar is up but failing: adoption is undecided, not dead
    slot_b = build_slot(tmp_path / "err-b.sqlite3", source)
    async with slot_b.router.lifespan_context(slot_b):
        async with AsyncClient(transport=ASGITransport(slot_b), base_url="http://runtime", headers=headers) as client:
            assert (await client.get(f"/api/replays/{IDENTIFIER}")).status_code == 404
            assert (tmp_path / "replays" / IDENTIFIER / "replay.json").exists()  # the record survived
            assert (await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-03",
                                                            "initialBalance": 1_000_000})).status_code == 409
            source.http_error = 422  # the sidecar no longer has it: the record dies with it
            assert (await client.get(f"/api/replays/{IDENTIFIER}")).status_code == 404
            assert not (tmp_path / "replays" / IDENTIFIER / "replay.json").exists()
            created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-03", "initialBalance": 1_000_000})
            assert created.status_code == 201, created.text


async def test_adoption_refuses_a_replay_the_sidecar_no_longer_has(tmp_path: Path):
    source = HistoricalSource()
    slot_a = build_slot(tmp_path / "live-a.sqlite3", source)
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with slot_a.router.lifespan_context(slot_a):
        async with AsyncClient(transport=ASGITransport(slot_a), base_url="http://runtime", headers=headers) as client:
            assert (await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})).status_code == 201
    source.gone = True  # the sidecar dropped it (replaced by another replay, restarted)
    slot_b = build_slot(tmp_path / "live-b.sqlite3", source)
    async with slot_b.router.lifespan_context(slot_b):
        async with AsyncClient(transport=ASGITransport(slot_b), base_url="http://runtime", headers=headers) as client:
            assert (await client.get(f"/api/replays/{IDENTIFIER}")).status_code == 404
            assert not (tmp_path / "replays" / IDENTIFIER / "replay.json").exists()  # the record died with it
            # The one-replay rule counts adoptable replays only while they exist.
            source.gone = False
            created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})
            assert created.status_code == 201, created.text


async def test_adoptable_replay_still_blocks_a_second_creation(tmp_path: Path):
    source = HistoricalSource()
    slot_a = build_slot(tmp_path / "live-a.sqlite3", source)
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with slot_a.router.lifespan_context(slot_a):
        async with AsyncClient(transport=ASGITransport(slot_a), base_url="http://runtime", headers=headers) as client:
            assert (await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})).status_code == 201
    slot_b = build_slot(tmp_path / "live-b.sqlite3", source)
    async with slot_b.router.lifespan_context(slot_b):
        async with AsyncClient(transport=ASGITransport(slot_b), base_url="http://runtime", headers=headers) as client:
            assert (await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-03", "initialBalance": 1_000_000})).status_code == 409


async def test_explicit_close_ends_the_sidecar_replay_and_the_record(tmp_path: Path):
    source = HistoricalSource()
    app = build_slot(tmp_path / "live.sqlite3", source)
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            assert (await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})).status_code == 201
            assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "close"})).status_code == 200
            assert source.closes == [IDENTIFIER]  # the user's close still closes the sidecar replay
            assert not (tmp_path / "replays" / IDENTIFIER / "replay.json").exists()
            assert (await client.get(f"/api/replays/{IDENTIFIER}")).status_code == 404
            assert (await client.get("/api/replays")).json()["items"] == []
            # With the record gone, the same process may create the next replay.
            assert (await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})).status_code == 201


async def test_custom_fees_wait_for_historical_snapshot():
    source = HistoricalSource()
    ready = copy.deepcopy(source.data)
    source.data = {key: ready[key] for key in ("replayId", "accountType", "symbol", "tradingDay")}
    source.data["status"] = "loading"
    provider = ReplayProvider(source, IDENTIFIER)
    provider.fee_schedule = {"source": "custom", "default": {"mode": "per_lot", "open": 3,
        "closeToday": 3, "closeYesterday": 3}, "overrides": []}
    statuses = []
    provider.subscribe_status(statuses.append)
    for _ in range(2):
        assert (await provider.command("state"))["status"] == "loading"
        assert statuses[-1]["marketDataStatus"] == "connecting"
    source.data = ready
    assert (await provider.command("state"))["status"] == "ready"
    assert provider.account_summary("")["equity"] == 1_000_000
    assert statuses[-1]["marketDataStatus"] == "normal"


async def test_replay_account_keeps_source_amounts_and_one_ratio_after_custom_fees():
    """#478：回放账户保留来源账本的保证金和可用资金；自定义手续费只按差额调整一次权益、可用和
    盈亏，保证金不动，两种比例都从调整后的同一版快照算。"""
    source = HistoricalSource()
    base = {key: value for key, value in source.data["account"].items()
            if key not in ("margin", "available", "riskRatio", "marginRatio")}
    # 挂单冻结 200：来源可用 300，不是 equity − margin。
    source.data["account"] = {**base, "equity": 1000, "commission": 0, "todayPnl": 0, "todayNetPnl": 0,
                              "margin": 500, "available": 300, "marginRatio": 50}
    provider = ReplayProvider(source, IDENTIFIER)
    await provider.command("state")
    account = provider.account_summary("")
    assert (account["margin"], account["available"], account["riskRatio"]) == (500, 300, 50)

    source.data["account"].update(available=500)
    source.data["trades"] = [{"tradeId": "t1", "clientOrderId": "AITreplay0001", "symbol": SYMBOL,
                              "direction": "BUY", "offset": "OPEN", "volume": 1, "price": 823.5,
                              "tradeDateTime": 1, "tradingDay": "2026-09-04"}]
    provider.fee_schedule = {"source": "custom", "default": {"mode": "per_lot", "open": 100,
        "closeToday": 100, "closeYesterday": 100}, "overrides": []}
    for _ in range(2):  # 重复读同一来源快照不累计扣费
        await provider.command("state")
        account = provider.account_summary("")
        assert (account["equity"], account["commission"]) == (900, 100)
        assert (account["margin"], account["available"]) == (500, 400)
        assert account["riskRatio"] == account["marginRatio"] == pytest.approx(500 / 900 * 100)

    # 旧 sidecar 只给比例：保证金按来源比例还原，可用资金退回 equity − margin（看不到挂单冻结）。
    provider.fee_schedule = None
    source.data["account"] = {**base, "equity": 1000, "commission": 0, "todayPnl": 0, "marginRatio": 50}
    await provider.command("state")
    account = provider.account_summary("")
    assert (account["margin"], account["available"], account["riskRatio"]) == (500, 500, 50)


async def test_replay_clock_does_not_weaken_live_freshness_or_transport_age():
    source = HistoricalSource()
    provider = ReplayProvider(source, IDENTIFIER)
    await provider.command("state")
    market = provider.market_snapshot("", SYMBOL)
    quote_is_fresh(market, allow_fixture=False, now=provider.clock())
    with pytest.raises(SimulationOrderError, match="quote_stale"):
        quote_is_fresh({**market, "replayId": IDENTIFIER}, allow_fixture=False)
    with pytest.raises(SimulationOrderError, match="quote_stale"):
        quote_is_fresh(market, allow_fixture=False, now=provider.clock() + timedelta(seconds=16))
    provider.received_at -= 16
    with pytest.raises(RuntimeError, match="replay_snapshot_unavailable"):
        provider.market_snapshot("", SYMBOL)


async def test_replay_cannot_read_other_contract_or_continue_after_finish():
    source = HistoricalSource()
    provider = ReplayProvider(source, IDENTIFIER)
    await provider.command("state")
    with pytest.raises(KeyError):
        provider.market_snapshot("", "SHFE.rb2610")
    source.data["status"] = "finished"
    await provider.command("state")
    assert provider.runtime_status()["simulationTrading"] is False
    with pytest.raises(SimulationOrderError, match="market_session_closed"):
        quote_is_fresh(provider.market_snapshot("", SYMBOL), allow_fixture=False, now=provider.clock())


async def test_backfilling_period_answers_recognizably_while_the_display_period_serves():
    source = HistoricalSource()
    source.data = copy.deepcopy(source.data)
    source.data["workbenches"]["300"] = {**source.data["workbenches"]["300"], "bars": [], "loading": True}
    provider = ReplayProvider(source, IDENTIFIER)
    await provider.command("state")
    assert provider.market_workbench(SYMBOL, 60)["bars"]
    with pytest.raises(RuntimeError, match="market_series_not_ready"):
        provider.market_workbench(SYMBOL, 300)
    # The period arriving clears the loading mark; history merging keeps its bars.
    source.data["workbenches"]["300"] = copy.deepcopy(HistoricalSource().data["workbenches"]["300"])
    await provider.command("state")
    assert provider.market_workbench(SYMBOL, 300)["bars"]


async def test_replay_create_passes_the_display_period_to_the_sidecar(tmp_path):
    source = HistoricalSource()
    app = create_app(tmp_path / "live.sqlite3", runtime_state_root=tmp_path, data_mode="tqkq-sidecar", data_provider=source,
                     agent_mode="fake", local_token="test-replay-token")
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04",
                                                              "initialBalance": 1_000_000, "defaultDurationSeconds": 900})
            assert created.status_code == 201, created.text
    assert source.creates == [{"symbol": SYMBOL, "tradingDay": "2026-09-04",
                               "initialBalance": 1_000_000, "defaultDurationSeconds": 900}]


async def test_replay_accepts_the_30_minute_and_weekly_display_periods(tmp_path):
    """R4：图表停在 30 分钟或周线时开始回放不再 422——这两个显示周期与共享行情一致，
    原样转发给 sidecar。"""
    clock = "2026-09-03T13:00:00+00:00"
    for duration in (1800, 604800):
        source = HistoricalSource()
        chart = fake_market_workbench(SYMBOL, duration)
        chart["quote"].update(symbol=SYMBOL, quoteDatetime=clock, priceTick=.02, volumeMultiple=1000,
                              bidPrice1=823.48, askPrice1=823.52, lastPrice=823.5)
        chart["quote"].pop("tradingSchedule", None)
        source.data["workbenches"][str(duration)] = chart
        app = create_app(tmp_path / f"live-{duration}.sqlite3", runtime_state_root=tmp_path / f"rt-{duration}",
                         data_mode="tqkq-sidecar", data_provider=source, agent_mode="fake",
                         local_token="test-replay-token")
        headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
                created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04",
                                                                  "initialBalance": 1_000_000,
                                                                  "defaultDurationSeconds": duration})
                assert created.status_code == 201, created.text
        assert source.creates[-1]["defaultDurationSeconds"] == duration


async def test_historical_stop_uses_common_service_and_closes_only_once(tmp_path):
    source = HistoricalSource()
    source.data["positions"] = [{"symbol": SYMBOL, "direction": "long", "volume": 1,
                                 "todayVolume": 1, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    provider = ReplayProvider(source, IDENTIFIER)
    await provider.command("state")
    app = create_app(tmp_path / "replay.sqlite3", runtime_state_root=tmp_path, agent_mode="fake",
                     data_provider=provider, simulation_gateway=provider, _replay_provider=provider)
    try:
        session = app.state.broker.create_session("local", "native", "local-user")
        stop = await app.state.autonomy.create_stop(user_id="local-user", session_id=session.session_id,
            symbol=SYMBOL, position_direction="long", position_bucket="today", volume=1, trigger_price=823)
        assert stop["status"] == "ARMED"
        source.data["market"].update(price=822.98, bidPrice1=822.96, askPrice1=823)
        await provider.command("state")
        await app.state.autonomy.run_once()
        await app.state.autonomy.run_once()
        assert len(source.submits) == 1
        assert source.submits[0]["direction"] == "SELL"
        assert source.submits[0]["offset"] == "CLOSETODAY"
        assert source.submits[0]["volume"] == 1
    finally:
        app.state.store.close()


def test_replay_copies_risk_and_validated_strategies_but_not_active_state(tmp_path):
    source, target = RuntimeStore(tmp_path / "source.sqlite3"), RuntimeStore(tmp_path / "replay.sqlite3")
    try:
        rules = {"allowedSymbols": ["AU2612"], "maxLossPerTrade": "1000", "maxDailyLoss": "5000",
                 "noNewPositionsAfter": "02:00", "timezone": "Asia/Shanghai"}
        with source.transaction() as connection:
            source.apply_risk_policy(user_id="local-user", expected_version=0, proposal_id="original-risk",
                                     rules=rules, rules_digest="a" * 64, connection=connection)
        for identifier, validation in (("validated", "passed"), ("unvalidated", "incomplete")):
            source.save_strategy_draft(strategy_id=identifier, user_id="local-user", source_session_id="original",
                name=identifier, symbol=SYMBOL, version=1, status="monitoring", definition={}, definition_digest="b" * 64)
            source.save_strategy_validation_report(report_id=identifier, strategy_id=identifier, user_id="local-user",
                strategy_digest="b" * 64, status=validation, checks=[], evidence={})
        child = SimpleNamespace(state=SimpleNamespace(store=target, broker=EventBroker(target)))
        copy_replay_configuration(source, child, "local-user", SYMBOL)
        assert target.active_risk_policy(user_id="local-user")["rules"] == rules
        copied = target.list_strategy_drafts(user_id="local-user")
        assert [(item["strategyId"], item["status"]) for item in copied] == [("validated", "paused")]
        assert source.get_strategy_draft("validated", user_id="local-user")["status"] == "monitoring"
        assert target.list_monitoring_strategies() == []
    finally:
        source.close()
        target.close()


async def test_a_cloud_user_owns_their_replay_account(tmp_path):
    """回放是用户自己的独立账户：云端用户（非 local-token）也要有 owner 授权，否则挂保护、
    开仓卡顺带止损止盈都以 account_not_granted 被拒（10-02 线上回放实测）。重复建子应用不重复授权。"""
    source = HistoricalSource()
    app = build_slot(tmp_path / "live.sqlite3", source)
    provider = ReplayProvider(source, IDENTIFIER)
    await provider.command("state")
    child = app.state.replay_router.create_child(tmp_path / "child", provider, "cloud-user-7")
    again = app.state.replay_router.create_child(tmp_path / "child", provider, "cloud-user-7", configure=False)
    try:
        account = child.state.accounts.default.account_id
        assert child.state.store.account_role(account_id=account, user_id="cloud-user-7") == "owner"
        assert again.state.store.account_role(account_id=account, user_id="cloud-user-7") == "owner"
    finally:
        child.state.store.close()
        again.state.store.close()
        app.state.store.close()

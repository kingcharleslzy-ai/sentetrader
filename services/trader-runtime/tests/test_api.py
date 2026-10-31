import asyncio
import hashlib
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from trader_runtime import risk as risk_module
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.fake_agent import parse_fake_risk_rule
from trader_runtime.main import create_app

TOKEN = "test-local-token"
ORIGIN = "http://127.0.0.1:5173"


@pytest.fixture
def application(tmp_path: Path):
    application = create_app(tmp_path / "test.sqlite3", local_token=TOKEN, agent_mode="fake")
    yield application
    application.state.store.close()


@pytest.fixture
async def client(application: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test",
        headers={"Origin": ORIGIN},
    ) as test_client:
        response = await test_client.post("/api/auth/local", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200
        yield test_client


async def create_session(client: AsyncClient) -> str:
    response = await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})
    assert response.status_code == 201
    return response.json()["sessionId"]


def mutation(version: int, key: str | None = None) -> dict[str, object]:
    return {"proposalVersion": version, "idempotencyKey": key or str(uuid4())}


def create_trade(application: FastAPI, session_id: str, turn_id: str = "turn-test"):
    return application.state.proposals.create_trade(
        session_id,
        turn_id,
        symbol="AU2612",
        side="buy_open",
        volume=1,
        price_intent="passive",
        reason="test candidate",
    )


def test_fake_risk_rule_parser_preserves_user_values_or_rejects() -> None:
    assert parse_fake_risk_rule(
        "黄金每笔最多亏五百元，每天亏到两千元，下午三点以后不再开仓"
    ) == {
        "allowed_symbols": ["AU2612"], "max_loss_per_trade": "500",
        "max_daily_loss": "2000", "no_new_positions_after": "15:00",
    }
    assert parse_fake_risk_rule(
        "黄金和螺纹钢都可以交易，每笔最多亏1000元，每日亏到3000元，22:00以后不再开仓"
    ) == {
        "allowed_symbols": ["AU2612", "RB2610"], "max_loss_per_trade": "1000",
        "max_daily_loss": "3000", "no_new_positions_after": "22:00",
    }
    assert parse_fake_risk_rule(
        "黄金每笔最多亏1000.50元，每天亏到3000.25元，中午一点以后不再开仓"
    ) == {
        "allowed_symbols": ["AU2612"], "max_loss_per_trade": "1000.50",
        "max_daily_loss": "3000.25", "no_new_positions_after": "13:00",
    }
    assert parse_fake_risk_rule(
        "黄金每笔最多亏1000.500元，每天亏到3000元，下午三点以后不再开仓"
    ) is None
    assert parse_fake_risk_rule("黄金每笔少亏一点，晚上早点停") is None


async def test_session_agent_id_defaults_to_native_and_rejects_the_removed_codex_alias(client: AsyncClient) -> None:
    """The pre-1.5.0 "codex" alias is gone: unknown agent ids are 422, the default is native."""
    created = await client.post("/api/sessions", json={"mode": "local"})
    assert created.status_code == 201 and created.json()["agentId"] == "native"
    assert (await client.post("/api/sessions", json={"mode": "local", "agentId": "codex"})).status_code == 422
    assert (await client.post("/api/sessions/restore", json={"mode": "local", "agentId": "codex"})).status_code == 422
    assert (await client.post("/api/sessions", json={"mode": "local", "agentId": "other"})).status_code == 422


async def test_runtime_requires_local_auth_and_rejects_untrusted_origins(application: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as raw:
        assert (await raw.get("/api/runtime/status")).status_code == 401
        rejected = await raw.post(
            "/api/auth/local", headers={"Origin": "https://evil.example", "Authorization": f"Bearer {TOKEN}"}
        )
        assert rejected.status_code == 403


async def test_packaged_tauri_origin_can_exchange_the_local_token(application: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as raw:
        response = await raw.post(
            "/api/auth/local",
            headers={"Origin": "tauri://localhost", "Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 200

        # WebKit may reject cross-site localhost cookies from tauri://. The
        # one-time IPC token therefore remains a valid in-memory bearer for
        # every protected local route, including the SSE request.
        raw.cookies.clear()
        session = await raw.post(
            "/api/sessions",
            headers={"Origin": "tauri://localhost", "Authorization": f"Bearer {TOKEN}"},
            json={"mode": "local", "agentId": "native"},
        )
        assert session.status_code == 201


async def test_runtime_is_explicitly_fake_and_live_trading_is_disabled(client: AsyncClient):
    response = await client.get("/api/runtime/status")
    assert response.status_code == 200
    assert response.json()["adapter"] == "fake-agent"
    assert response.json()["accountMode"] == "fake"
    assert response.json()["liveTrading"] is False


async def test_instrument_catalog_is_read_only_searchable_and_main_filterable(
    client: AsyncClient,
):
    response = await client.get(
        "/api/instruments", params={"exchange": "shfe", "mainOnly": "true", "search": "沪金"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["schemaVersion"] == 1
    assert payload["dataStatus"] == "simulated"
    assert payload["count"] == 1
    assert payload["items"][0] == {
        "symbol": "SHFE.au2612", "exchangeId": "SHFE", "instrumentId": "au2612",
        "instrumentName": "沪金2612", "productId": "au", "priceTick": 0.02,
        "volumeMultiple": 1000.0, "deliveryYear": 2026, "deliveryMonth": 12,
        "expireRestDays": 90, "hasNight": True, "isMain": True, "openLimit": 0,
        # 交割月提醒 (2026-10-02)：目录条目带 deliveryNotice（12 月交割还远，这里为 None）。
        "deliveryNotice": None,
        # 收盘提醒 (2026-10-03)：时段从目录读（快照同款规整，HH:MM），不占 K 线订阅。
        "tradingSchedule": {"day": [["09:00", "10:15"], ["10:30", "11:30"], ["13:30", "15:00"]],
                            "night": [["21:00", "02:30"]]},
    }

    by_product = await client.get("/api/instruments", params={"product": "i"})
    assert by_product.status_code == 200
    assert [item["symbol"] for item in by_product.json()["items"]] == ["DCE.i2609"]


async def test_market_workbench_returns_detached_quote_and_kline_snapshots(
    client: AsyncClient,
):
    response = await client.get(
        "/api/markets/DCE.i2609/workbench", params={"duration": 300},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["schemaVersion"] == 1
    assert payload["durationSeconds"] == 300
    assert payload["quote"]["symbol"] == "DCE.I2609"
    assert len(payload["bars"]) == 80
    assert payload["bars"][-1]["datetime"] > payload["bars"][0]["datetime"]
    assert payload["dataStatus"] == "simulated"
    assert payload["liveTrading"] == "disabled"

    invalid = await client.get(
        "/api/markets/DCE.i2609/workbench", params={"duration": 7},
    )
    assert invalid.status_code == 422


async def test_market_ticket_creates_only_a_confirmable_simulation_candidate(
    client: AsyncClient, application: FastAPI,
):
    session_id = await create_session(client)

    response = await client.post("/api/simulation/proposals", json={
        "sessionId": session_id,
        "symbol": "SHFE.au2612",
        "side": "buy_open",
        "volume": 1,
        "priceIntent": "passive",
        "reason": "用户从行情工作台创建模拟开仓候选。",
    })

    assert response.status_code == 201
    proposal = response.json()
    assert proposal["symbol"] == "SHFE.AU2612"
    assert proposal["side"] == "buy_open"
    assert proposal["volume"] == 1
    assert proposal["status"] == "approval_required"
    assert proposal["executionEligibleAtCreation"] is False
    assert application.state.simulation_orders.list(user_id="local-user") == []
    events = [event for _, event in application.state.store.list_events(session_id)]
    assert [event["name"] for event in events[-2:]] == [
        "message.completed", "proposal.trade",
    ]
    assert events[-1]["payload"]["proposalId"] == proposal["proposalId"]


async def test_market_ticket_accepts_an_explicit_chart_price_and_rejects_off_tick(
    client: AsyncClient,
):
    session_id = await create_session(client)
    request = {
        "sessionId": session_id,
        "symbol": "SHFE.au2612",
        "side": "buy_open",
        "volume": 1,
        "priceIntent": "passive",
        "reason": "用户在图表上明确选择模拟限价。",
    }

    accepted = await client.post(
        "/api/simulation/proposals", json={**request, "limitPrice": 821.5},
    )
    assert accepted.status_code == 201
    assert accepted.json()["limitPrice"] == 821.5

    rejected = await client.post(
        "/api/simulation/proposals", json={**request, "limitPrice": 821.3},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {"code": "limit_price_not_on_tick"}


async def test_market_ticket_allows_any_contract_without_an_explicit_policy(
    client: AsyncClient, application: FastAPI,
):
    session_id = await create_session(client)

    response = await client.post("/api/simulation/proposals", json={
        "sessionId": session_id,
        "symbol": "DCE.i2609",
        "side": "sell_open",
        "volume": 1,
        "priceIntent": "passive",
        "reason": "用户从行情工作台创建模拟开仓候选。",
    })

    assert response.status_code == 201
    proposal = response.json()
    assert proposal["symbol"] == "DCE.I2609"
    assert proposal["status"] == "approval_required"
    assert proposal["riskCheck"]["checks"]["symbolAllowed"] is True
    assert application.state.simulation_orders.list(user_id="local-user") == []


async def test_account_activity_is_read_only_and_initialized(client: AsyncClient):
    response = await client.get("/api/account/activity")

    assert response.status_code == 200
    assert response.json() == {
        "schemaVersion": 1,
        "capturedAt": response.json()["capturedAt"],
        "ageSeconds": 0,
        "dataStatus": "simulated",
        "ordersInitialized": True,
        "orders": [],
        "trades": [],
        "liveTrading": "disabled",
    }


async def test_session_bootstrap_is_persisted_and_resumable(client: AsyncClient, application: FastAPI):
    session_id = await create_session(client)
    event_names = [event["name"] for _, event in application.state.store.list_events(session_id)]
    assert "card.account" in event_names
    assert "card.strategy" in event_names
    assert event_names.index("message.completed") < event_names.index("card.account")
    resumed = await client.get(f"/api/sessions/{session_id}")
    assert resumed.status_code == 200
    assert resumed.json()["sessionId"] == session_id


async def test_trade_state_machine_requires_versioned_approval_and_is_idempotent(
    client: AsyncClient, application: FastAPI
):
    session_id = await create_session(client)
    proposal = create_trade(application, session_id)
    proposal_id = proposal["proposalId"]
    assert proposal["riskCheck"]["passed"] is True

    key = str(uuid4())
    confirmation = {"approvalId": proposal["approvalId"], **mutation(1, key)}
    confirmed = await client.post(f"/api/proposals/{proposal_id}/confirm", json=confirmation)
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "candidate_recorded"
    assert confirmed.json()["environment"] == "simulation"

    repeated = await client.post(f"/api/proposals/{proposal_id}/confirm", json=confirmation)
    assert repeated.status_code == 200
    assert repeated.json()["auditId"] == confirmed.json()["auditId"]
    terminal_retry = await client.post(
        f"/api/proposals/{proposal_id}/confirm",
        json={"approvalId": proposal["approvalId"], **mutation(1)},
    )
    assert terminal_retry.status_code == 409
    assert terminal_retry.json()["detail"]["code"] == "invalid_state_transition"


async def test_stale_version_and_reused_idempotency_key_are_rejected(client: AsyncClient, application: FastAPI):
    session_id = await create_session(client)
    proposal = create_trade(application, session_id)
    key = str(uuid4())
    rejected = await client.post(
        f"/api/proposals/{proposal['proposalId']}/reject", json=mutation(1, key)
    )
    assert rejected.status_code == 200
    conflict = await client.post(
        f"/api/proposals/{proposal['proposalId']}/confirm",
        json={"approvalId": proposal["approvalId"], **mutation(1, key)},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_key_conflict"
    second = create_trade(application, session_id, "turn-second")
    stale = await client.post(
        f"/api/proposals/{second['proposalId']}/reject", json=mutation(2)
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_proposal_version"


async def test_trusted_risk_recheck_ignores_agent_supplied_limits(
    client: AsyncClient, application: FastAPI, monkeypatch: pytest.MonkeyPatch,
):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 29, 10, 0, tzinfo=tz)

    monkeypatch.setattr(risk_module, "datetime", FixedDateTime)
    session_id = await create_session(client)
    rule = application.state.proposals.create_risk_rule(
        session_id, "turn-risk", allowed_symbols=["AU2612"],
        max_loss_per_trade="2000", max_daily_loss="3000", no_new_positions_after="23:59",
    )
    assert (await client.post(
        f"/api/proposals/{rule['proposalId']}/confirm",
        json={"approvalId": rule["approvalId"], **mutation(1)},
    )).status_code == 200
    proposal = create_trade(application, session_id)
    record = application.state.store.get(proposal["proposalId"])
    payload = record["payload"]
    payload["stopPrice"] = 819
    payload["riskCheck"] = {"passed": True, "limit": 999999, "estimatedLoss": 1,
                            "contractMultiplier": 1, "priceTick": "1", "reason": "agent says safe"}
    application.state.store.update(
        proposal["proposalId"], status="approval_required", payload=payload, version=1,
        approval_id=record["approvalId"], approval_expires_at=record["approvalExpiresAt"],
    )
    response = await client.post(
        f"/api/proposals/{proposal['proposalId']}/confirm",
        json={"approvalId": proposal["approvalId"], **mutation(1)},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["riskCheck"]["limit"] == 2000
    assert response.json()["detail"]["riskCheck"]["estimatedLoss"] == 4000


async def test_risk_rule_proposal_applies_a_versioned_runtime_policy(
    client: AsyncClient, application: FastAPI, monkeypatch: pytest.MonkeyPatch,
):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 29, 10, 0, tzinfo=tz)

    monkeypatch.setattr(risk_module, "datetime", FixedDateTime)
    session_id = await create_session(client)
    proposal = application.state.proposals.create_risk_rule(
        session_id, "turn-risk", allowed_symbols=["AU2612", "RB2610"],
        max_loss_per_trade="1000", max_daily_loss="3000",
        no_new_positions_after="22:00",
    )
    assert application.state.store.active_risk_policy(user_id="local-user") is None

    confirmed = await client.post(
        f"/api/proposals/{proposal['proposalId']}/confirm",
        json={"approvalId": proposal["approvalId"], **mutation(1)},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "risk_rules_applied"
    active = application.state.store.active_risk_policy(user_id="local-user")
    assert active is not None
    assert active["version"] == 1
    assert active["rules"]["maxLossPerTrade"] == "1000.00"
    assert active["rules"]["allowedSymbols"] == ["AU2612", "RB2610"]
    summary = await client.get("/api/risk/summary")
    assert summary.status_code == 200
    assert summary.json() == {
        "configured": True,
        "policyVersion": 1,
        "dataStatus": "simulated",
        "maxLossPerTrade": 1000.0,
        "maxDailyLoss": 3000.0,
        "noNewPositionsAfter": "22:00",
        "allowedSymbols": ["AU2612", "RB2610"],
        "status": "normal",
        "dailyLossUsed": 0.0,
        "dailyLossRemaining": 3000.0,
        "dailyLossPercent": 0.0,
        "openingAllowedNow": True,
        "message": "当前仍在已确认的风险预算内。",
    }

    trade = create_trade(application, session_id, "turn-trade")
    assert trade["riskCheck"]["limit"] == 1000
    assert trade["riskCheck"]["passed"] is True


async def test_concurrent_risk_rule_candidates_do_not_overwrite_each_other(
    client: AsyncClient, application: FastAPI,
):
    session_id = await create_session(client)
    first = application.state.proposals.create_risk_rule(
        session_id, "turn-one", allowed_symbols=["AU2612"],
        max_loss_per_trade="1000", max_daily_loss="3000",
        no_new_positions_after="22:00",
    )
    second = application.state.proposals.create_risk_rule(
        session_id, "turn-two", allowed_symbols=["AU2612"],
        max_loss_per_trade="800", max_daily_loss="2500",
        no_new_positions_after="21:30",
    )
    assert (await client.post(
        f"/api/proposals/{first['proposalId']}/confirm",
        json={"approvalId": first["approvalId"], **mutation(1)},
    )).status_code == 200
    conflict = await client.post(
        f"/api/proposals/{second['proposalId']}/confirm",
        json={"approvalId": second["approvalId"], **mutation(1)},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "risk_policy_version_conflict"


async def test_daily_loss_rule_rejects_unavailable_or_cached_account_snapshots(
    client: AsyncClient, application: FastAPI, monkeypatch,
):
    session_id = await create_session(client)
    proposal = application.state.proposals.create_risk_rule(
        session_id, "turn-risk", allowed_symbols=["AU2612"],
        max_loss_per_trade="1000", max_daily_loss="3000",
        no_new_positions_after="23:59",
    )
    assert (await client.post(
        f"/api/proposals/{proposal['proposalId']}/confirm",
        json={"approvalId": proposal["approvalId"], **mutation(1)},
    )).status_code == 200

    monkeypatch.setattr(
        application.state.proposals.data_provider,
        "account_summary",
        lambda _turn_id: {
            "dataStatus": "unavailable", "isCached": True, "todayNetPnl": 0,
        },
    )
    trade = create_trade(application, session_id, "turn-trade")
    assert trade["riskCheck"]["passed"] is False
    assert trade["riskCheck"]["checks"]["dailyLoss"] is False
    summary = await client.get("/api/risk/summary")
    assert summary.status_code == 200
    assert summary.json()["status"] == "unavailable"
    assert summary.json()["dailyLossUsed"] is None


async def test_runs_are_serialized_per_session_and_can_be_cancelled(client: AsyncClient, application: FastAPI):
    session_id = await create_session(client)
    first = (await client.post(
        f"/api/sessions/{session_id}/messages", json={"text": "回测最近三年"}
    )).json()["runId"]
    second = (await client.post(
        f"/api/sessions/{session_id}/messages", json={"text": "再回测一次"}
    )).json()["runId"]
    await asyncio.sleep(0.08)
    assert application.state.store.get_run(first)["status"] == "running"
    assert application.state.store.get_run(second)["status"] == "queued"
    cancelled = await client.post(f"/api/runs/{first}/cancel")
    assert cancelled.status_code == 200
    assert (await client.post(f"/api/runs/{second}/cancel")).status_code == 200
    await asyncio.sleep(0.05)
    assert application.state.store.get_run(first)["status"] == "cancelled"
    assert application.state.store.get_run(second)["status"] == "cancelled"


async def test_fake_backtest_cancellation_stops_thread_and_does_not_persist_result(
    client: AsyncClient, application: FastAPI, monkeypatch,
):
    session_id = await create_session(client)
    application.state.adapter.backtest_progress_delay = 0
    entered = threading.Event()

    def blocking_execute(*, lease, **_kwargs):
        entered.set()
        while True:
            lease.check()
            time.sleep(0.01)

    monkeypatch.setattr(application.state.backtest_service, "_execute", blocking_execute)
    run_id = (await client.post(
        f"/api/sessions/{session_id}/messages", json={"text": "回测并等待取消"}
    )).json()["runId"]
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=3)
    task_events = [
        event for _, event in application.state.store.list_events(session_id)
        if event["payload"].get("taskId") == run_id
    ]
    assert task_events[-1]["payload"]["status"] == "running"
    assert task_events[-1]["payload"]["progress"] == 78
    assert not any(event["name"] == "task.completed" for event in task_events)
    assert application.state.adapter.tasks[run_id]["status"] == "running"
    cancelled = await client.post(f"/api/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    for _ in range(100):
        if application.state.backtest_coordinator.snapshot()["jobs"] == 0:
            break
        await asyncio.sleep(0.01)
    assert application.state.backtest_coordinator.snapshot()["jobs"] == 0
    assert application.state.store.get_run(run_id)["status"] == "cancelled"
    with pytest.raises(KeyError):
        application.state.store.latest_backtest_run(
            session_id=session_id, user_id="local-user"
        )


async def test_fake_backtest_failure_never_publishes_successful_completion(
    client: AsyncClient, application: FastAPI, monkeypatch,
):
    session_id = await create_session(client)
    application.state.adapter.backtest_progress_delay = 0

    def fail_execute(**_kwargs):
        raise RuntimeError("fixture_worker_failed")

    monkeypatch.setattr(application.state.backtest_service, "_execute", fail_execute)
    run_id = (await client.post(
        f"/api/sessions/{session_id}/messages", json={"text": "回测失败终态"}
    )).json()["runId"]
    for _ in range(200):
        if application.state.store.get_run(run_id)["status"] == "failed":
            break
        await asyncio.sleep(0.01)
    assert application.state.store.get_run(run_id)["status"] == "failed"
    terminal = [
        event for _, event in application.state.store.list_events(session_id)
        if event["name"] == "task.completed"
        and event["payload"].get("taskId") == run_id
    ]
    assert [event["payload"]["status"] for event in terminal] == ["failed"]
    with pytest.raises(KeyError):
        application.state.store.latest_backtest_run(
            session_id=session_id, user_id="local-user"
        )


async def test_fake_backtest_completes_only_after_result_is_persisted_and_published(
    client: AsyncClient, application: FastAPI,
):
    session_id = await create_session(client)
    application.state.adapter.backtest_progress_delay = 0
    run_id = (await client.post(
        f"/api/sessions/{session_id}/messages", json={"text": "回测成功终态"}
    )).json()["runId"]
    for _ in range(300):
        if application.state.store.get_run(run_id)["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert application.state.store.get_run(run_id)["status"] == "completed"
    events = [event for _, event in application.state.store.list_events(session_id)]
    card_index = next(
        index for index, event in enumerate(events)
        if event["name"] == "card.backtest" and event["payload"].get("taskId") == run_id
    )
    completed_index = next(
        index for index, event in enumerate(events)
        if event["name"] == "task.completed"
        and event["payload"].get("taskId") == run_id
        and event["payload"]["status"] == "completed"
    )
    assert card_index < completed_index
    backtest_id = events[card_index]["payload"]["backtestId"]
    assert application.state.store.get_backtest_run(
        backtest_id, user_id="local-user"
    )["status"] == "completed"


class HistoricalBacktestProvider(FakeReadOnlyDataProvider):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[dict[str, object]] = []

    def historical_backtest(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(dict(request))
        bars = [
            {
                "datetime": 1_770_000_000_000_000_000 + index,
                "open": 3500 + index,
                "high": 3502 + index,
                "low": 3498 + index,
                "close": 3501 + index,
                "volume": 100 + index,
                "openInterest": 8000 + index,
            }
            for index in range(30)
        ]
        digest = hashlib.sha256(json.dumps(
            bars, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        return {
            "engineVersion": "tqsdk-3.10.2-tqsim-tqbacktest-test",
            "engineDigest": "a" * 64,
            "dataSource": (
                f"tqsdk:{request['symbol']}:{request['startDate']}:"
                f"{request['endDate']}:{request['durationSeconds']}"
            ),
            "dataDigest": digest,
            "dataPointCount": len(bars),
            "contractMultiplier": 10.0,
            "priceTick": 1.0,
            "fillMode": "ACTIVE",
            "commissionModel": "tqsdk_default",
            "bars": bars,
            "baseline": {
                "returnPercent": 1.0, "maxDrawdownPercent": 0.8,
                "signalCount": 4, "winRatePercent": 50.0, "tradeCount": 4,
                "commission": 8.0, "curve": [1_000_000, 1_010_000],
            },
            "candidate": {
                "returnPercent": 1.5, "maxDrawdownPercent": 0.7,
                "signalCount": 6, "winRatePercent": 66.67, "tradeCount": 6,
                "commission": 12.0, "curve": [1_000_000, 1_015_000],
            },
            "executionEnabled": False,
            "orderCapabilities": [],
            "accountType": "TqSim",
        }


async def test_tqsdk_backtest_endpoint_publishes_the_completed_research_card(tmp_path: Path):
    provider = HistoricalBacktestProvider()
    application = create_app(
        tmp_path / "tqsdk-api.sqlite3",
        local_token=TOKEN,
        agent_mode="fake",
        data_provider=provider,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
            headers={"Origin": ORIGIN},
        ) as test_client:
            assert (await test_client.post(
                "/api/auth/local", headers={"Authorization": f"Bearer {TOKEN}"}
            )).status_code == 200
            session_id = await create_session(test_client)
            response = await test_client.post("/api/backtests/tqsdk", json={
                "sessionId": session_id,
                "requestId": "tqsdk-api-request",
                "symbol": "SHFE.rb2610",
                "startDate": "2026-06-01",
                "endDate": "2026-08-30",
                "durationSeconds": 3600,
                "baselineThreshold": "1.00",
                "candidateThreshold": "0.90",
                "initialBalance": 1_000_000,
            })
        assert response.status_code == 201, response.text
        response_card = response.json()
        assert response_card["sourceKind"] == "tqsdk_history"
        assert response_card["taskId"] == "tqsdk-api-request"
        assert response_card["turnId"]
        assert provider.requests == [{
            "symbol": "SHFE.rb2610",
            "startDate": "2026-06-01",
            "endDate": "2026-08-30",
            "durationSeconds": 3600,
            "baselineThreshold": "1.00",
            "candidateThreshold": "0.90",
            "initialBalance": 1_000_000.0,
        }]
        events = [event for _, event in application.state.store.list_events(session_id)]
        assert [event["name"] for event in events[-2:]] == [
            "message.completed", "card.backtest"
        ]
        assert events[-1]["payload"]["taskId"] == "tqsdk-api-request"
        assert "deploymentEligible" not in events[-1]["payload"]
        assert events[-1]["payload"]["turnId"] == events[-2]["payload"]["turnId"]
        assert response_card == events[-1]["payload"]
    finally:
        application.state.store.close()


async def test_cancelled_run_cannot_be_overwritten_by_late_completion(application: FastAPI):
    session = application.state.broker.create_session("local", "native", "local-user")
    run = application.state.store.create_run(
        session_id=session.session_id, user_id="local-user", instruction="race"
    )
    application.state.store.update_run(run["runId"], status="running", expected_statuses={"queued"})
    application.state.store.update_run(
        run["runId"], status="cancelled", error="cancelled_by_user", expected_statuses={"running"}
    )
    late = application.state.store.update_run(
        run["runId"], status="completed", result={"late": True}, expected_statuses={"running"}
    )
    assert late["status"] == "cancelled"


async def test_idle_run_worker_releases_session_memory(client: AsyncClient, application: FastAPI):
    application.state.runs.idle_worker_seconds = 0.02
    session_id = await create_session(client)
    response = await client.post(
        f"/api/sessions/{session_id}/messages", json={"text": "查看账户"}
    )
    run_id = response.json()["runId"]
    for _ in range(100):
        if application.state.store.get_run(run_id)["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.04)
    assert session_id not in application.state.runs.workers
    assert session_id not in application.state.runs.queues


async def test_event_retention_is_bounded(tmp_path: Path):
    application = create_app(tmp_path / "retention.sqlite3", local_token=TOKEN, agent_mode="fake")
    application.state.store.max_events_per_session = 3
    session = application.state.broker.create_session("local", "native", "local-user")
    for index in range(5):
        await application.state.broker.publish(
            session.session_id, kind="error", name="error",
            payload={"code": f"e{index}", "message": "bounded"},
        )
    events = application.state.store.list_events(session.session_id)
    assert len(events) == 3
    assert events[0][1]["payload"]["code"] == "e2"
    application.state.store.close()


def test_restart_recovers_incomplete_runs_and_appends_an_error_event(tmp_path: Path):
    database = tmp_path / "restart.sqlite3"
    first = create_app(database, local_token=TOKEN, agent_mode="fake")
    session = first.state.broker.create_session("local", "native", "local-user")
    run = first.state.store.create_run(
        session_id=session.session_id, user_id="local-user", instruction="unfinished"
    )
    first.state.store.update_run(run["runId"], status="running")
    first.state.store.close()

    restarted = create_app(database, local_token=TOKEN, agent_mode="fake")
    assert restarted.state.store.get_run(run["runId"])["status"] == "failed"
    recovery = [event for _, event in restarted.state.store.list_events(session.session_id)]
    assert recovery[-1]["payload"]["code"] == "runtime_restarted"
    restarted.state.store.close()


def _acquire_fresh_lease(store, instance_id: str) -> None:
    """Stand in for a live slot's heartbeat: the lease row is fresh, so a takeover
    must refuse and construction must treat the holder as alive."""
    from datetime import UTC, timedelta

    from trader_runtime.store import utc_now

    stale_before = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    assert store.try_acquire_lease(
        instance_id=instance_id, host="blue-green-host", pid=111, build_sha=None,
        now=utc_now(), stale_before=stale_before)


def test_a_live_lease_holder_keeps_its_runs_through_a_standby_boot(tmp_path: Path):
    """525-A: during a blue-green deploy the serving slot still holds the lease
    with a fresh heartbeat while the standby constructs. The standby's boot must
    not mark the serving slot's runs failed — they are still executing there."""
    database = tmp_path / "bluegreen.sqlite3"
    serving = create_app(database, local_token=TOKEN, agent_mode="fake")
    session = serving.state.broker.create_session("local", "native", "local-user")
    running = serving.state.store.create_run(
        session_id=session.session_id, user_id="local-user", instruction="in flight")
    serving.state.store.update_run(running["runId"], status="running")
    queued = serving.state.store.create_run(
        session_id=session.session_id, user_id="local-user", instruction="next in line")
    _acquire_fresh_lease(serving.state.store, "serving-slot")

    standby = create_app(database, local_token=TOKEN, agent_mode="fake")
    assert standby.state.store.get_run(running["runId"])["status"] == "running"
    assert standby.state.store.get_run(queued["runId"])["status"] == "queued"
    # No restart notice was written into the serving slot's session either.
    assert all(event["payload"].get("code") != "runtime_restarted"
               for _, event in standby.state.store.list_events(session.session_id))
    standby.state.store.close()
    serving.state.store.close()


async def test_the_lease_holder_recovers_leftover_runs_once_on_acquisition(tmp_path: Path):
    """525-A: a standby that could not recover at construction (the lease was
    live) recovers the leftover runs when it takes the lease over from a crashed
    holder — once per process, with the same restart notice a restart writes."""
    from datetime import UTC, timedelta

    database = tmp_path / "takeover.sqlite3"
    crashed = create_app(database, local_token=TOKEN, agent_mode="fake")
    session = crashed.state.broker.create_session("local", "native", "local-user")
    leftover = crashed.state.store.create_run(
        session_id=session.session_id, user_id="local-user", instruction="from the crashed leader")
    crashed.state.store.update_run(leftover["runId"], status="running")
    _acquire_fresh_lease(crashed.state.store, "old-leader")
    crashed.state.store.close()

    successor = create_app(database, local_token=TOKEN, agent_mode="fake")
    # Construction saw a live holder and stood by: nothing was recovered yet.
    assert successor.state.store.get_run(leftover["runId"])["status"] == "running"

    # The old leader's heartbeat goes stale; the successor's lifespan takes the
    # lease over, and the holder recovers the leftover runs.
    stale_before = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    with successor.state.store.lock, successor.state.store.connection:
        successor.state.store.connection.execute(
            "UPDATE runtime_lease SET heartbeat_at = ? WHERE id = 1", (stale_before,))
    async with successor.router.lifespan_context(successor):
        recovered = successor.state.store.get_run(leftover["runId"])
        assert recovered["status"] == "failed" and recovered["error"] == "runtime_restarted"
        events = [event for _, event in successor.state.store.list_events(session.session_id)]
        assert events[-1]["payload"]["code"] == "runtime_restarted"
        assert len([event for event in events
                    if event["payload"].get("code") == "runtime_restarted"]) == 1
        # Once per process: a later re-acquisition must not recover again.
        assert successor.state.ctx.services.runs_recovered is True
    successor.state.store.close()


async def test_a_lease_takeover_keeps_the_runs_the_new_slot_already_accepted(tmp_path: Path):
    """525-A: nginx serves the new slot before the old one stops, so the new slot accepts runs while
    it still stands by. Taking the lease over ends only the previous holder's leftovers, never the
    new slot's own queued/running work."""
    from datetime import UTC, timedelta

    database = tmp_path / "own-runs.sqlite3"
    old = create_app(database, local_token=TOKEN, agent_mode="fake")
    session = old.state.broker.create_session("local", "native", "local-user")
    leftover = old.state.store.create_run(
        session_id=session.session_id, user_id="local-user", instruction="old slot's run")
    old.state.store.update_run(leftover["runId"], status="running")
    _acquire_fresh_lease(old.state.store, "old-slot")
    old.state.store.close()

    new = create_app(database, local_token=TOKEN, agent_mode="fake")
    own = new.state.store.create_run(
        session_id=session.session_id, user_id="local-user", instruction="sent to the new slot")
    new.state.runs.accepted.add(own["runId"])  # accepted while standing by (enqueue records it)
    stale_before = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    with new.state.store.lock, new.state.store.connection:
        new.state.store.connection.execute(
            "UPDATE runtime_lease SET heartbeat_at = ? WHERE id = 1", (stale_before,))
    async with new.router.lifespan_context(new):
        assert new.state.store.get_run(leftover["runId"])["error"] == "runtime_restarted"
        assert new.state.store.get_run(own["runId"])["status"] == "queued"
    new.state.store.close()


async def test_run_manager_shutdown_marks_its_own_runs_restart_terminated(tmp_path: Path):
    """525-A: a clean slot exit ends the runs only this RunManager accepted —
    the running one and the queued one behind it — as terminated by the restart,
    never as a user cancellation, and tells the session once per run."""
    from trader_runtime.broker import EventBroker
    from trader_runtime.runs import RunManager
    from trader_runtime.store import RuntimeStore

    class _HangingAdapter:
        async def handle_message(self, session_id, text, run_id, *, user_id=None, model_slug=None):
            await asyncio.Event().wait()

    store = RuntimeStore(tmp_path / "run-manager.sqlite3")
    broker = EventBroker(store)
    await broker.start()
    session = broker.create_session("local", "native", "local-user")
    runs = RunManager(store, broker, _HangingAdapter())
    running = runs.enqueue(session_id=session.session_id, user_id="local-user", instruction="in flight")
    for _ in range(200):
        if store.get_run(running["runId"])["status"] == "running":
            break
        await asyncio.sleep(0.01)
    queued = runs.enqueue(session_id=session.session_id, user_id="local-user", instruction="next in line")

    await runs.shutdown()
    for run_id in (running["runId"], queued["runId"]):
        run = store.get_run(run_id)
        assert run["status"] == "failed" and run["error"] == "runtime_restarted", run
    events = [event for _, event in store.list_events(session.session_id)]
    assert [event["payload"].get("code") for event in events].count("runtime_restarted") == 2
    await broker.shutdown()
    store.close()


async def test_transactional_outbox_replays_after_a_runtime_gap(tmp_path: Path):
    application = create_app(tmp_path / "outbox.sqlite3", local_token=TOKEN, agent_mode="fake")
    session = application.state.broker.create_session("local", "native", "local-user")
    with application.state.store.transaction() as connection:
        application.state.broker.stage(
            session.session_id, kind="error", name="error",
            payload={"code": "durable", "message": "outbox"}, connection=connection,
        )
    assert application.state.store.list_events(session.session_id) == []
    await application.state.broker.flush()
    events = application.state.store.list_events(session.session_id)
    assert events[-1][1]["payload"]["code"] == "durable"
    with application.state.store.connection:
        application.state.store.connection.execute(
            "INSERT INTO outbox_events VALUES (?, ?, ?, ?, ?)",
            ("legacy-delivered", session.session_id, "{}", "2026-08-25T00:00:00+00:00",
             "2026-08-25T00:00:01+00:00"),
        )
    await application.state.broker.flush()
    assert application.state.store.connection.execute(
        "SELECT COUNT(*) AS total FROM outbox_events"
    ).fetchone()["total"] == 0
    application.state.store.close()


async def test_a_late_event_for_a_deleted_session_does_not_block_other_sessions(tmp_path: Path, caplog):
    """#499: a producer that still holds a released session's id must not leave an orphan outbox
    row that fails every later delivery, and orphans an older version left are dropped at start."""
    database = tmp_path / "orphan-outbox.sqlite3"
    application = create_app(database, local_token=TOKEN, agent_mode="fake")
    broker, store = application.state.broker, application.state.store
    gone = broker.create_session("local", "native", "local-user").session_id
    healthy = broker.create_session("local", "native", "other-user").session_id
    await application.state.lifecycle.release(gone)
    await broker.publish(gone, kind="message", name="message.completed",
                         payload={"turnId": "late", "role": "assistant", "text": "迟到的语音结果"})
    for index in range(3):
        await broker.publish(healthy, kind="message", name="message.completed",
                             payload={"turnId": f"ok-{index}", "role": "assistant", "text": f"第{index}条"})
    texts = [event["payload"]["text"] for _, event in store.list_events(healthy) if event["payload"].get("turnId", "").startswith("ok-")]
    assert texts == ["第0条", "第1条", "第2条"]
    assert store.connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 0
    # An orphan row an older version committed (beside a valid one) is dropped, logged, at the next start.
    with store.connection:
        store.connection.execute("INSERT INTO outbox_events VALUES (?, ?, ?, ?, NULL)",
                                 ("orphan", gone, json.dumps({"id": "orphan", "sessionId": gone}), "2026-10-01T00:00:00+00:00"))
    broker.stage(healthy, kind="message", name="message.completed", payload={"turnId": "queued", "role": "assistant", "text": "排队的"})
    store.close()
    restarted = create_app(database, local_token=TOKEN, agent_mode="fake")
    with caplog.at_level("WARNING"):
        await restarted.state.broker.start()
    await restarted.state.broker.shutdown()
    store = restarted.state.store
    assert [event["payload"]["text"] for _, event in store.list_events(healthy)].count("排队的") == 1
    assert store.connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 0
    assert any("outbox_orphan_dropped" in record.getMessage() and gone in record.getMessage() for record in caplog.records)
    store.close()


async def test_running_diff_artifacts_replace_snapshots_and_keep_only_the_final(tmp_path: Path):
    application = create_app(tmp_path / "diff-artifacts.sqlite3", local_token=TOKEN, agent_mode="fake")
    session = application.state.broker.create_session("local", "native", "local-user")
    for index in range(12):
        await application.state.broker.publish(
            session.session_id, kind="diff", name="agent.diff",
            payload={
                "turnId": "turn-diff", "taskId": "task-diff", "itemId": "item-diff",
                "status": "running",
                "changes": [{"path": "apps/client/src/App.tsx", "kind": "update", "diff": f"+{index}"}],
            },
        )
    await application.state.broker.publish(
        session.session_id, kind="diff", name="agent.diff",
        payload={
            "turnId": "turn-diff", "taskId": "task-diff", "itemId": "item-diff",
            "status": "completed",
            "changes": [{"path": "apps/client/src/App.tsx", "kind": "update", "diff": "+final"}],
        },
    )
    rows = application.state.store.connection.execute(
        "SELECT payload_json FROM artifacts WHERE session_id = ? AND artifact_type = 'agent.diff'",
        (session.session_id,),
    ).fetchall()
    assert len(rows) == 1
    assert '"status": "completed"' in rows[0]["payload_json"]
    assert "+final" in rows[0]["payload_json"]
    application.state.store.close()


def test_session_ttl_does_not_delete_proposal_audit(tmp_path: Path):
    application = create_app(tmp_path / "audit-retention.sqlite3", local_token=TOKEN, agent_mode="fake")
    session = application.state.broker.create_session("local", "native", "local-user")
    proposal = create_trade(application, session.session_id, "turn-audit")
    audit_id = application.state.store.audit(proposal["proposalId"], "created", {"status": "retained"})
    with application.state.store.lock, application.state.store.connection:
        application.state.store.connection.execute(
            "UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (session.session_id,),
        )
    assert application.state.store.cleanup_expired_sessions() == 1
    assert application.state.store.get(proposal["proposalId"])["id"] == proposal["proposalId"]
    row = application.state.store.connection.execute(
        "SELECT id FROM proposal_audit WHERE id = ?", (audit_id,)
    ).fetchone()
    assert row["id"] == audit_id
    application.state.store.close()

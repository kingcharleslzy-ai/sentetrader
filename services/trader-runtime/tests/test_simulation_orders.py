from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from fixtures.card_tools import legacy_card_args
from trader_runtime import risk as risk_module
from trader_runtime.autonomy import AutonomyError
from trader_runtime.data_providers import (
    FakeReadOnlyDataProvider,
    fake_account_summary,
    fake_market_snapshot,
)
from trader_runtime.main import create_app
from trader_runtime.risk import RiskPolicy
from trader_runtime.sidecar_http import SidecarHttpError
from trader_runtime.simulation_orders import SimulationOrderError
from trader_runtime.trader_tools import TraderReadOnlyTools
from trader_runtime.voice_realtime import _order_outcome_line

USER = "local-user"
SHANGHAI = timezone(timedelta(hours=8))


def quote_now() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S.%f")


class NormalProvider(FakeReadOnlyDataProvider):
    def __init__(self) -> None:
        self.age_seconds = 0.0
        self.price_tick = 0.5
        self.bid = 823.0
        self.ask: float | None = 824.0
        self.last = 824.0
        self.quote_datetime = quote_now()
        self.market_session_status = "trading"
        self.canonical_symbol = "SHFE.au2612"
        self.position_items = [
            {
                "symbol": "SHFE.au2612", "direction": "long", "volume": 2,
                "todayVolume": 1, "historicalVolume": 1,
                "frozenToday": 0, "frozenHistorical": 0,
            },
            {
                "symbol": "SHFE.au2612", "direction": "short", "volume": 2,
                "todayVolume": 1, "historicalVolume": 1,
                "frozenToday": 0, "frozenHistorical": 0,
            },
        ]

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        return {
            **fake_account_summary(turn_id),
            "dataStatus": "normal",
            "isCached": False,
        }

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        return {
            **fake_market_snapshot(turn_id, symbol),
            "symbol": self.canonical_symbol,
            "dataStatus": "normal",
            "isCached": False,
            "ageSeconds": self.age_seconds,
            "quoteDatetime": self.quote_datetime,
            "marketSessionStatus": self.market_session_status,
            "priceTick": self.price_tick,
            "bidPrice1": self.bid,
            "askPrice1": self.ask,
            "price": self.last,
        }

    def positions(self) -> list[dict[str, Any]]:
        # The AI tools validate positions as the Position contract (exchangeId / instrumentId /
        # unrealizedPnl); the order tests only set the volume buckets.
        return [{"exchangeId": str(item["symbol"]).split(".", 1)[0], "instrumentId": str(item["symbol"]).split(".", 1)[-1],
                 "unrealizedPnl": 0, **item} for item in self.position_items]

    def account_activity(self) -> dict[str, Any]:
        return {**super().account_activity(), "dataStatus": "normal", "isCached": False}


class Gateway:
    def __init__(self) -> None:
        self.submit_count = 0
        self.query_count = 0
        self.cancel_count = 0
        self.submit_error = False
        self.query_error = False
        self.cancel_error = False
        self.submit_report: dict[str, Any] | None = None
        self.query_report: dict[str, Any] | None = None
        self.query_reports: list[dict[str, Any]] = []
        self.cancel_report: dict[str, Any] | None = None
        self.request: dict[str, Any] | None = None

    @staticmethod
    def report(
        request: dict[str, Any],
        *,
        status: str = "ALIVE",
        volume_left: int | None = None,
        initialized: bool = True,
        found: bool = True,
        **overrides: Any,
    ) -> dict[str, Any]:
        if not found:
            return {"ordersInitialized": initialized, "found": False}
        order = {
            "clientOrderId": request["clientOrderId"],
            "exchangeOrderId": "fixture-order",
            "symbol": request["symbol"],
            "direction": request["direction"],
            "offset": request["offset"],
            "priceType": "LIMIT",
            "volume": request["volume"],
            "volumeLeft": request["volume"] if volume_left is None else volume_left,
            "limitPrice": request["limitPrice"],
            "status": status,
            "isOnline": status == "ALIVE",
            "isDead": status == "FINISHED",
            "isError": False,
        }
        order.update(overrides)
        return {"ordersInitialized": initialized, "found": True, "order": order}

    async def submit_order_once(self, request: dict[str, Any]) -> dict[str, Any]:
        self.submit_count += 1
        self.request = dict(request)
        if self.submit_error:
            raise TimeoutError("response lost")
        return self.submit_report or self.report(request)

    async def query_order(self, client_order_id: str) -> dict[str, Any]:
        self.query_count += 1
        if self.query_error:
            raise TimeoutError("query lost")
        if self.query_reports:
            return self.query_reports.pop(0)
        if self.query_report is not None:
            return self.query_report
        if self.request is None:
            return {"ordersInitialized": True, "found": False}
        assert client_order_id == self.request["clientOrderId"]
        return self.report(self.request)

    async def cancel_order_once(
        self, client_order_id: str, command_id: str
    ) -> dict[str, Any]:
        self.cancel_count += 1
        assert command_id == f"cancel:{client_order_id}"
        if self.cancel_error:
            raise TimeoutError("cancel response lost")
        assert self.request is not None
        return self.cancel_report or self.report(self.request, status="FINISHED")


class HangingGateway(Gateway):
    def __init__(self) -> None:
        super().__init__()
        self.submit_started = asyncio.Event()
        self.submit_release = asyncio.Event()

    async def submit_order_once(self, request: dict[str, Any]) -> dict[str, Any]:
        self.submit_count += 1
        self.request = dict(request)
        self.submit_started.set()
        await self.submit_release.wait()
        return self.report(request)


def application(path: Path, gateway: Gateway, provider: NormalProvider | None = None):
    return create_app(
        path,
        local_token="test-local-token",
        agent_mode="fake",
        data_provider=provider or NormalProvider(),
        simulation_gateway=gateway,
    )


def proposal(app, *, turn: str = "turn-order", volume: int = 1) -> dict[str, Any]:
    session = app.state.broker.create_session("local", "native", USER)
    return app.state.proposals.create_trade(
        session.session_id,
        turn,
        symbol="SHFE.au2612",
        side="buy_open",
        volume=volume,
        price_intent="passive",
        reason="black-box order test",
    )


def close_proposal(
    app,
    *,
    side: str = "sell_close",
    position_bucket: str = "today",
    volume: int = 1,
    symbol: str = "SHFE.au2612",
    turn: str = "turn-close",
) -> dict[str, Any]:
    session = app.state.broker.create_session("local", "native", USER)
    return app.state.proposals.create_trade(
        session.session_id,
        turn,
        symbol=symbol,
        side=side,
        volume=volume,
        price_intent="passive",
        position_bucket=position_bucket,
        reason="manual close black-box test",
    )


async def confirm(
    app, item: dict[str, Any], key: str = "confirm-key-0001"
) -> dict[str, Any]:
    return await app.state.proposals.confirm(
        item["proposalId"],
        user_id=USER,
        approval_id=item["approvalId"],
        proposal_version=item["version"],
        idempotency_key=key,
    )


async def list_orders(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as client:
        response = await client.post(
            "/api/auth/local", headers={"Authorization": "Bearer test-local-token"},
        )
        assert response.status_code == 200
        return await client.get("/api/simulation/orders")


async def test_manual_close_is_one_explicit_bucket_and_one_submit(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    provider = NormalProvider()
    app = application(tmp_path / "close-today.sqlite3", gateway, provider)
    item = close_proposal(app)

    assert item["side"] == "sell_close"
    assert item["offset"] == "CLOSETODAY"
    assert item["positionBucket"] == "today"
    assert item["riskCheck"]["checks"]["reduceOnly"] is True
    assert gateway.submit_count == 0

    result = await confirm(app, item, "confirm-close-today")
    assert gateway.submit_count == 1
    assert result["simulationOrder"]["request"] == {
        "commandId": f"submit:{result['clientOrderId']}",
        "clientOrderId": result["clientOrderId"],
        "symbol": "SHFE.au2612",
        "direction": "SELL",
        "offset": "CLOSETODAY",
        "volume": 1,
        "priceType": "LIMIT",
        "limitPrice": 824.0,
    }
    app.state.store.close()


async def test_explicit_chart_limit_is_preserved_through_confirm_and_submit(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "chart-limit.sqlite3", gateway, NormalProvider())
    session = app.state.broker.create_session("local", "native", USER)
    item = app.state.proposals.create_trade(
        session.session_id,
        "turn-chart-limit",
        symbol="SHFE.au2612",
        side="buy_open",
        volume=1,
        price_intent="passive",
        limit_price=821.5,
        reason="explicit chart price",
    )

    assert item["limitPrice"] == 821.5
    result = await confirm(app, item, "confirm-chart-limit")
    assert gateway.submit_count == 1
    assert result["simulationOrder"]["request"]["limitPrice"] == 821.5
    app.state.store.close()


def test_explicit_chart_limit_must_match_tick(tmp_path: Path) -> None:
    app = application(
        tmp_path / "chart-limit-off-tick.sqlite3", Gateway(), NormalProvider(),
    )
    session = app.state.broker.create_session("local", "native", USER)

    with pytest.raises(SimulationOrderError, match="limit_price_not_on_tick"):
        app.state.proposals.create_trade(
            session.session_id,
            "turn-chart-limit-off-tick",
            symbol="SHFE.au2612",
            side="buy_open",
            volume=1,
            price_intent="passive",
            limit_price=821.3,
            reason="off tick chart price",
        )
    app.state.store.close()


async def test_manual_close_yesterday_and_short_direction_are_explicit(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "close-history.sqlite3", gateway, NormalProvider())
    item = close_proposal(
        app, side="buy_close", position_bucket="historical", turn="short-history",
    )
    assert item["offset"] == "CLOSE"
    result = await confirm(app, item, "confirm-short-history")
    assert result["simulationOrder"]["request"]["direction"] == "BUY"
    assert result["simulationOrder"]["request"]["offset"] == "CLOSE"
    assert result["simulationOrder"]["request"]["limitPrice"] == 823.0
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_other_exchange_uses_one_close_without_today_split(
    tmp_path: Path,
) -> None:
    provider = NormalProvider()
    provider.canonical_symbol = "DCE.i2609"
    provider.position_items = [{
        "symbol": "DCE.i2609", "direction": "long", "volume": 3,
        "todayVolume": 1, "historicalVolume": 2,
        "frozenToday": 0, "frozenHistorical": 0,
    }]
    gateway = Gateway()
    app = application(tmp_path / "close-dce.sqlite3", gateway, provider)
    item = close_proposal(
        app, symbol="DCE.i2609", position_bucket="all", volume=2, turn="dce-close",
    )
    assert item["offset"] == "CLOSE"
    result = await confirm(app, item, "confirm-dce-close")
    assert result["simulationOrder"]["request"]["volume"] == 2
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_close_rechecks_frozen_quantity_before_submit(
    tmp_path: Path,
) -> None:
    provider = NormalProvider()
    gateway = Gateway()
    app = application(tmp_path / "close-frozen.sqlite3", gateway, provider)
    item = close_proposal(app)
    provider.position_items[0]["frozenToday"] = 1

    with pytest.raises(SimulationOrderError, match="close_volume_unavailable"):
        await confirm(app, item, "confirm-close-frozen")
    assert gateway.submit_count == 0
    assert app.state.simulation_orders.list(user_id=USER) == []
    app.state.store.close()


async def test_close_unknown_only_queries_original_order_and_never_resubmits(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    gateway.submit_error = True
    app = application(tmp_path / "close-unknown.sqlite3", gateway, NormalProvider())
    item = close_proposal(app)
    first = await confirm(app, item, "confirm-close-unknown")
    order = first["simulationOrder"]
    assert order["displayStatus"] == "结果未知"
    assert gateway.submit_count == 1

    gateway.submit_error = False
    gateway.query_report = Gateway.report(order["request"])
    recovered = await app.state.simulation_orders.query(
        order["clientOrderId"], user_id=USER,
    )
    assert recovered["displayStatus"] == "已受理"
    assert recovered["request"]["offset"] == "CLOSETODAY"
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_close_offset_identity_mismatch_stops_progress(tmp_path: Path) -> None:
    gateway = Gateway()

    async def mismatched(request: dict[str, Any]) -> dict[str, Any]:
        gateway.submit_count += 1
        gateway.request = dict(request)
        return Gateway.report(request, offset="OPEN")

    gateway.submit_order_once = mismatched  # type: ignore[method-assign]
    app = application(tmp_path / "close-identity.sqlite3", gateway, NormalProvider())
    result = await confirm(app, close_proposal(app), "confirm-close-identity")
    assert result["simulationOrder"]["displayStatus"] == "身份异常"
    assert result["simulationOrder"]["remoteStatus"] == "IDENTITY_MISMATCH"
    assert gateway.submit_count == 1
    app.state.store.close()


def test_shfe_close_never_auto_splits_across_today_and_history(tmp_path: Path) -> None:
    app = application(tmp_path / "close-no-split.sqlite3", Gateway(), NormalProvider())
    with pytest.raises(SimulationOrderError, match="close_volume_unavailable"):
        close_proposal(app, volume=2, position_bucket="today")
    app.state.store.close()


def prepare_only(app, item: dict[str, Any]) -> dict[str, Any]:
    with app.state.store.transaction() as connection:
        return app.state.simulation_orders.prepare(
            proposal_id=item["proposalId"],
            user_id=USER,
            payload=item,
            connection=connection,
        )


async def test_confirmation_is_required_and_duplicate_confirmation_submits_once(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    provider = NormalProvider()
    app = application(tmp_path / "runtime.sqlite3", gateway, provider)
    item = proposal(app)
    assert gateway.submit_count == 0

    provider.quote_datetime = quote_now()
    first = await confirm(app, item)
    second = await confirm(app, item)
    assert gateway.submit_count == 1
    assert first["clientOrderId"] == second["clientOrderId"]
    assert first["simulationOrder"]["displayStatus"] == "已受理"
    assert app.state.store.get(item["proposalId"])["payload"]["quoteDatetime"] == provider.quote_datetime
    app.state.store.close()


async def test_market_ticket_http_flow_never_submits_before_explicit_confirmation(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "market-ticket.sqlite3", gateway)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as client:
        assert (await client.post(
            "/api/auth/local", headers={"Authorization": "Bearer test-local-token"},
        )).status_code == 200
        session = (await client.post(
            "/api/sessions", json={"mode": "local", "agentId": "native"},
        )).json()
        candidate_response = await client.post("/api/simulation/proposals", json={
            "sessionId": session["sessionId"],
            "symbol": "SHFE.au2612",
            "side": "buy_open",
            "volume": 1,
            "priceIntent": "passive",
            "reason": "用户从行情工作台创建模拟开仓候选。",
        })
        assert candidate_response.status_code == 201
        candidate = candidate_response.json()
        assert gateway.submit_count == 0
        assert app.state.simulation_orders.list(user_id=USER) == []

        app.state.data_provider.quote_datetime = quote_now()
        confirmed = await client.post(
            f"/api/proposals/{candidate['proposalId']}/confirm",
            json={
                "approvalId": candidate["approvalId"],
                "proposalVersion": candidate["version"],
                "idempotencyKey": "market-ticket-confirm-0001",
            },
        )
        assert confirmed.status_code == 200
        assert gateway.submit_count == 1
        assert confirmed.json()["simulationOrder"]["displayStatus"] == "已受理"
    app.state.store.close()


async def test_ai_execution_http_session_is_bounded_and_owner_can_emergency_stop(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "ai-http.sqlite3", gateway)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as client:
        assert (await client.post(
            "/api/auth/local", headers={"Authorization": "Bearer test-local-token"},
        )).status_code == 200
        session = (await client.post(
            "/api/sessions", json={"mode": "local", "agentId": "native"},
        )).json()
        started = await client.post("/api/ai-execution/start", json={
            "sessionId": session["sessionId"],
            "symbols": ["SHFE.au2612"],
            "permissions": ["open", "cancel", "close", "stop"],
            "maxOrderVolume": 1,
            "maxOrders": 10,
            "durationMinutes": 30,
        })
        assert started.status_code == 200
        assert started.json()["status"] == "ACTIVE"
        assert started.json()["allowedSymbols"] == ["SHFE.au2612"]
        assert started.json()["remainingOrders"] == 10
        assert started.json()["environment"] == "TqKq simulation"
        assert started.json()["liveTrading"] == "disabled"
        app.state.store.save_strategy_draft(
            strategy_id="strategy-emergency-stop",
            user_id=USER,
            source_session_id=session["sessionId"],
            name="急停联动策略",
            symbol="SHFE.au2612",
            version=1,
            status="monitoring",
            definition={"timeframeSeconds": 300, "riskLimits": {}},
            definition_digest="a" * 64,
        )
        # 急停只停当前账户（所有者 2026-10-04）：同一用户另一个账户上的策略照常监控。
        app.state.store.save_strategy_draft(
            strategy_id="strategy-other-account",
            user_id=USER,
            source_session_id=session["sessionId"],
            name="另一账户的策略",
            symbol="SHFE.au2612",
            version=1,
            status="monitoring",
            definition={"timeframeSeconds": 300, "riskLimits": {}},
            definition_digest="b" * 64,
            trading_account_id="paper:other-account",
        )
        snapshot = await client.get("/api/ai-execution")
        assert snapshot.status_code == 200
        assert snapshot.json()["authorization"]["authorizationId"] == started.json()["authorizationId"]
        stopped = await client.post(
            "/api/ai-execution/stop", json={"emergency": True},
        )
        assert stopped.status_code == 200
        assert stopped.json()["authorization"]["status"] == "EMERGENCY_STOP"
        assert stopped.json()["cancelledOrders"] == []
        assert stopped.json()["pausedStrategyIds"] == ["strategy-emergency-stop"]
        assert stopped.json()["liveTrading"] == "disabled"
        assert app.state.store.get_strategy_draft(
            "strategy-emergency-stop", user_id=USER
        )["status"] == "paused"
        assert app.state.store.get_strategy_draft(
            "strategy-other-account", user_id=USER
        )["status"] == "monitoring"
    assert gateway.submit_count == 0
    app.state.store.close()


async def test_record_only_proposal_cannot_become_executable_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "record-only.sqlite3"
    initial = create_app(
        database, local_token="test-local-token", agent_mode="fake",
        data_provider=NormalProvider(), simulation_gateway=None,
    )
    item = proposal(initial)
    assert item["executionEligibleAtCreation"] is False
    initial.state.store.close()

    gateway = Gateway()
    restarted = application(database, gateway)
    with pytest.raises(SimulationOrderError, match="proposal_not_execution_eligible"):
        await confirm(restarted, item)
    assert gateway.submit_count == 0
    restarted.state.store.close()


async def test_prepared_survives_restart_and_needs_manual_continue(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    gateway = Gateway()
    first = application(database, gateway)
    link = prepare_only(first, proposal(first))
    first.state.store.close()

    provider = NormalProvider()
    restarted = application(database, gateway, provider)
    current = restarted.state.simulation_orders.get(link["clientOrderId"], user_id=USER)
    assert current["submitPhase"] == "PREPARED"
    assert current["displayStatus"] == "待发送"
    assert gateway.submit_count == 0
    provider.quote_datetime = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with pytest.raises(SimulationOrderError, match="quote_stale"):
        await restarted.state.simulation_orders.dispatch(link["clientOrderId"], user_id=USER)
    assert gateway.submit_count == 0
    provider.quote_datetime = quote_now()
    current_risk = restarted.state.simulation_orders.risk_check
    restarted.state.simulation_orders.risk_check = lambda payload, user_id: {"passed": False}
    with pytest.raises(SimulationOrderError, match="risk_check_failed"):
        await restarted.state.simulation_orders.dispatch(link["clientOrderId"], user_id=USER)
    assert gateway.submit_count == 0
    assert restarted.state.simulation_orders.get(link["clientOrderId"], user_id=USER)["submitPhase"] == "PREPARED"
    restarted.state.simulation_orders.risk_check = current_risk
    sent = await restarted.state.simulation_orders.dispatch(
        link["clientOrderId"], user_id=USER
    )
    assert sent["displayStatus"] == "已受理"
    assert gateway.submit_count == 1
    restarted.state.store.close()


async def test_prepared_order_stays_unsent_after_midnight_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "midnight-cutoff.sqlite3"
    gateway = Gateway()
    initial = application(database, gateway)
    link = prepare_only(initial, proposal(initial))
    initial.state.store.close()

    restarted = application(database, gateway)
    restarted.state.simulation_orders.policies.get = lambda **_kwargs: RiskPolicy(
        max_loss_per_trade=Decimal("2000"),
        no_new_positions_after="22:00",
    )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = cls(2026, 8, 30, 0, 1, tzinfo=SHANGHAI)
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(risk_module, "datetime", FixedDateTime)

    with pytest.raises(SimulationOrderError, match="risk_check_failed"):
        await restarted.state.simulation_orders.dispatch(
            link["clientOrderId"], user_id=USER,
        )
    assert gateway.submit_count == 0
    assert restarted.state.simulation_orders.get(
        link["clientOrderId"], user_id=USER,
    )["submitPhase"] == "PREPARED"
    restarted.state.store.close()


async def test_not_dispatched_report_preserves_manual_submit_and_cancel_choices(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    gateway.submit_report = {
        "ordersInitialized": False, "found": False,
        "commandStatus": "NOT_DISPATCHED",
    }
    app = application(tmp_path / "not-ready.sqlite3", gateway)
    prepared = await confirm(app, proposal(app))
    assert prepared["simulationOrder"]["submitPhase"] == "PREPARED"

    gateway.submit_report = None
    accepted = await app.state.simulation_orders.dispatch(
        prepared["clientOrderId"], user_id=USER,
    )
    assert accepted["displayStatus"] == "已受理"
    gateway.cancel_report = {
        "ordersInitialized": False, "found": False,
        "commandStatus": "NOT_DISPATCHED",
    }
    still_alive = await app.state.simulation_orders.cancel(
        prepared["clientOrderId"], user_id=USER,
    )
    assert still_alive["cancelPhase"] == "NONE"
    assert still_alive["displayStatus"] == "已受理"
    app.state.store.close()


async def test_paper_settlement_pending_rejection_stays_unsent_with_a_readable_reason(
    tmp_path: Path,
) -> None:
    """本站模拟引擎等结算价时的 409 拒收（2026-10-08）：命令没有进账本，委托是
    如实的“未发送”并带原因——不是“结果未知”，也不是网关中断；语音按 code 念
    出可读的中文原因。"""
    gateway = Gateway()

    async def refused(request: dict[str, Any]) -> dict[str, Any]:
        gateway.submit_count += 1
        gateway.request = dict(request)
        raise SidecarHttpError(
            "sidecar_http_error", status=409,
            payload={"error": "paper_settlement_pending"})

    gateway.submit_order_once = refused  # type: ignore[method-assign]
    app = application(tmp_path / "settlement-pending.sqlite3", gateway)
    confirmed = await confirm(app, proposal(app))
    order = confirmed["simulationOrder"]
    assert order["submitPhase"] == "PREPARED"
    assert order["displayStatus"] == "待发送"
    assert order["lastReadError"] == "paper_settlement_pending"
    spoken = _order_outcome_line({
        "symbol": order["request"]["symbol"], "displayStatus": order["displayStatus"],
        "error": order["lastReadError"],
    })
    assert "等交易所公布结算价" in spoken
    app.state.store.close()


async def test_a_timed_out_submit_the_gateway_never_sent_returns_to_unsent(
    tmp_path: Path,
) -> None:
    """2026-10-12 审查：下单调用超时（网关 4 秒没答）先记结果未知；回查时网关明确说这单
    排队过期、从未发出（NOT_DISPATCHED），委托回到“未发送”并带原因，不停在结果未知；
    再点继续照常重发。只是“查不到”（NOT_FOUND）的仍是结果未知。"""
    gateway = Gateway()
    gateway.submit_error = True  # the call times out: UNKNOWN
    app = application(tmp_path / "expired.sqlite3", gateway)
    confirmed = await confirm(app, proposal(app))
    client_order_id = confirmed["clientOrderId"]
    assert confirmed["simulationOrder"]["submitPhase"] == "UNKNOWN"
    # A bare "not found" proves nothing: still unknown.
    gateway.query_report = {"ordersInitialized": True, "found": False}
    still_unknown = await app.state.simulation_orders.query(client_order_id, user_id=USER)
    assert still_unknown["submitPhase"] == "UNKNOWN" and still_unknown["displayStatus"] == "结果未知"
    # Nor does a gateway that cannot look yet (orders not initialized).
    gateway.query_report = {"ordersInitialized": False, "found": False, "commandStatus": "NOT_DISPATCHED"}
    assert (await app.state.simulation_orders.query(client_order_id, user_id=USER))["submitPhase"] == "UNKNOWN"
    gateway.query_report = {"ordersInitialized": True, "found": False,
                            "commandStatus": "NOT_DISPATCHED", "errorCode": "gateway_command_expired"}
    unsent = await app.state.simulation_orders.query(client_order_id, user_id=USER)
    assert unsent["submitPhase"] == "PREPARED" and unsent["displayStatus"] == "待发送"
    assert unsent["lastReadError"] == "gateway_command_expired"
    # 继续: sent again through the normal checks.
    gateway.submit_error, gateway.query_report = False, None
    sent = await app.state.simulation_orders.dispatch(client_order_id, user_id=USER)
    assert sent["displayStatus"] == "已受理" and gateway.submit_count == 2
    app.state.store.close()


async def test_a_submit_refused_at_authentication_is_unsent_not_unknown(
    tmp_path: Path,
) -> None:
    """号池子进程换了人：旧 token 在鉴权处就被拒（401），命令没进任何账户——如实“未发送”。"""
    gateway = Gateway()

    async def refused(request: dict[str, Any]) -> dict[str, Any]:
        gateway.submit_count += 1
        raise SidecarHttpError("sidecar_http_error", status=401, payload={"error": "sidecar_auth_required"})

    gateway.submit_order_once = refused  # type: ignore[method-assign]
    app = application(tmp_path / "auth-refused.sqlite3", gateway)
    confirmed = await confirm(app, proposal(app))
    order = confirmed["simulationOrder"]
    assert order["submitPhase"] == "PREPARED" and order["lastReadError"] == "sidecar_auth_rejected"
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_call_started_becomes_unknown_and_restart_only_queries(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    gateway = Gateway()
    first = application(database, gateway)
    link = prepare_only(first, proposal(first))
    request = link["request"]
    with first.state.store.lock, first.state.store.connection:
        first.state.store.connection.execute(
            "UPDATE tqkq_sim_order_links SET submit_phase = 'CALL_STARTED' WHERE id = ?",
            (link["id"],),
        )
    first.state.store.close()

    query_gateway = Gateway()
    query_gateway.request = request
    query_gateway.query_report = query_gateway.report(request)
    restarted = application(database, query_gateway)
    # Construction (a standby slot too) leaves the link alone; the process
    # taking the lease over recovers it before any loop runs.
    assert restarted.state.simulation_orders.get(
        link["clientOrderId"], user_id=USER)["submitPhase"] == "CALL_STARTED"
    await restarted.state.instance_lease.start()
    await restarted.state.instance_lease.stop()
    assert (
        restarted.state.simulation_orders.get(
            link["clientOrderId"],
            user_id=USER,
        )["submitPhase"]
        == "UNKNOWN"
    )
    await restarted.state.simulation_orders.recover_once(user_id=USER, attempts=1, interval=0)
    recovered = restarted.state.simulation_orders.get(
        link["clientOrderId"], user_id=USER
    )
    assert recovered["displayStatus"] == "已受理"
    assert query_gateway.submit_count == 0
    assert query_gateway.query_count == 1
    restarted.state.store.close()


async def test_lost_submit_response_never_resubmits_and_not_found_is_not_retryable(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    gateway.submit_error = True
    app = application(tmp_path / "runtime.sqlite3", gateway)
    result = await confirm(app, proposal(app))
    client_order_id = result["clientOrderId"]
    assert result["simulationOrder"]["submitPhase"] == "UNKNOWN"
    assert result["simulationOrder"]["displayStatus"] == "结果未知"

    gateway.query_report = {"ordersInitialized": False, "found": False}
    before_initialization = await app.state.simulation_orders.query(
        client_order_id, user_id=USER
    )
    assert before_initialization["remoteStatus"] is None
    gateway.query_report = {"ordersInitialized": True, "found": False}
    not_found = await app.state.simulation_orders.query(client_order_id, user_id=USER)
    assert not_found["remoteStatus"] == "NOT_FOUND"
    assert gateway.submit_count == 1
    assert gateway.query_count == 2
    with pytest.raises(SimulationOrderError, match="order_not_prepared"):
        await app.state.simulation_orders.dispatch(client_order_id, user_id=USER)
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_restart_queries_unknown_order_after_history_initializes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    first_gateway = Gateway()
    first_gateway.submit_error = True
    first = application(database, first_gateway)
    result = await confirm(first, proposal(first))
    request = result["simulationOrder"]["request"]
    first.state.store.close()

    gateway = Gateway()
    gateway.request = request
    gateway.query_report = gateway.report(request)
    restarted = application(database, gateway)
    await restarted.state.simulation_orders.recover_once(user_id=USER, attempts=1, interval=0)
    recovered = restarted.state.simulation_orders.get(
        request["clientOrderId"], user_id=USER
    )
    assert recovered["displayStatus"] == "已受理"
    assert gateway.query_count == 1
    assert gateway.submit_count == 0
    restarted.state.store.close()


def _ai_proposal(app, **kwargs: Any) -> dict[str, Any]:
    item = proposal(app, **kwargs)
    with app.state.store.connection:
        app.state.store.connection.execute(
            "UPDATE proposals SET payload_json = json_set(payload_json, '$.automationSource', 'ai_autonomous')")
    return item


def _ai_activity_status(app, request: dict[str, Any], order: dict[str, Any],
                        fills: int = 0) -> str:
    from trader_runtime.agent_activity import activity_snapshot

    trades = ([{"clientOrderId": request["clientOrderId"], "volume": fills, "tradeDateTime": 1}]
              if fills else [])
    snapshot = activity_snapshot(app, USER, "", {"orders": [order], "trades": trades})
    return snapshot["orders"][0]["displayStatus"]


async def test_ai_activity_uses_the_order_service_status_instead_of_recomputing(
    tmp_path: Path,
) -> None:
    # #483：本站模拟账户拒单、提交回包丢失时，AI 动态不能把 FINISHED 无成交说成"已撤销"。
    gateway = Gateway()
    gateway.submit_error = True
    app = application(tmp_path / "runtime.sqlite3", gateway)
    result = await confirm(app, _ai_proposal(app))
    request = result["simulationOrder"]["request"]
    rejected = gateway.report(request, status="FINISHED", isError=True)
    assert _ai_activity_status(app, request, rejected["order"]) == "结果未知"
    gateway.query_report = rejected
    await app.state.simulation_orders.query(request["clientOrderId"], user_id=USER)
    assert _ai_activity_status(app, request, rejected["order"]) == "已拒绝"
    app.state.store.close()

    # 本地已确认在途时，活动帧经同一投影推进：撤单、成交；终态之后旧帧不回退。
    gateway = Gateway()
    app = application(tmp_path / "live.sqlite3", gateway)
    result = await confirm(app, _ai_proposal(app, volume=2))
    request = result["simulationOrder"]["request"]
    assert result["simulationOrder"]["displayStatus"] == "已受理"
    cancelled = gateway.report(request, status="FINISHED")["order"]
    assert _ai_activity_status(app, request, cancelled) == "已撤销"
    partial = gateway.report(request, status="ALIVE", volume_left=1)["order"]
    assert _ai_activity_status(app, request, partial, fills=1) == "部分成交"
    filled = gateway.report(request, status="FINISHED", volume_left=0)["order"]
    assert _ai_activity_status(app, request, filled, fills=2) == "已成交"
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(request["clientOrderId"], user_id=USER)
    alive = gateway.report(request)["order"]
    assert _ai_activity_status(app, request, alive) == "已成交"
    app.state.store.close()


async def test_a_partially_filled_order_ending_on_an_error_frame_is_partial_not_rejected(
        tmp_path: Path) -> None:
    """#518：部分成交后以错误帧收尾的委托，已成交的那几手是事实——显示“部分成交后已结束”
    （#932：已不在场上，不是还挂着的“部分成交”），不被错误标志整笔说成“已拒绝”；零成交的
    错误帧仍是“已拒绝”。"""
    gateway = Gateway()
    app = application(tmp_path / "partial-error.sqlite3", gateway)
    result = await confirm(app, _ai_proposal(app, volume=2))
    request = result["simulationOrder"]["request"]
    errored = gateway.report(request, status="FINISHED", isError=True, volume_left=1)
    assert _ai_activity_status(app, request, errored["order"], fills=1) == "部分成交后已结束"
    rejected = gateway.report(request, status="FINISHED", isError=True, volume_left=2)
    assert _ai_activity_status(app, request, rejected["order"]) == "已拒绝"
    app.state.store.close()


async def test_cancel_response_loss_is_sticky_and_fill_wins_cancel_race(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "lost.sqlite3", gateway)
    result = await confirm(app, proposal(app))
    client_order_id = result["clientOrderId"]
    gateway.cancel_error = True
    lost = await app.state.simulation_orders.cancel(client_order_id, user_id=USER)
    assert lost["cancelPhase"] == "UNKNOWN"
    assert lost["displayStatus"] == "结果未知"
    with pytest.raises(SimulationOrderError, match="order_not_cancelable"):
        await app.state.simulation_orders.cancel(client_order_id, user_id=USER)
    assert gateway.cancel_count == 1
    app.state.store.close()

    race_gateway = Gateway()
    race_app = application(tmp_path / "race.sqlite3", race_gateway)
    race_result = await confirm(race_app, proposal(race_app, turn="turn-race"))
    race_gateway.cancel_report = race_gateway.report(
        race_result["simulationOrder"]["request"],
        status="FINISHED",
        volume_left=0,
    )
    filled = await race_app.state.simulation_orders.cancel(
        race_result["clientOrderId"], user_id=USER
    )
    assert filled["displayStatus"] == "已成交"
    assert filled["cancelPhase"] == "CONFIRMED"
    race_app.state.store.close()

    partial_gateway = Gateway()
    partial_app = application(tmp_path / "partial.sqlite3", partial_gateway)
    partial_result = await confirm(
        partial_app, proposal(partial_app, turn="turn-partial", volume=2)
    )
    partial_gateway.cancel_report = partial_gateway.report(
        partial_result["simulationOrder"]["request"], status="FINISHED", volume_left=1
    )
    partial = await partial_app.state.simulation_orders.cancel(
        partial_result["clientOrderId"], user_id=USER
    )
    assert partial["displayStatus"] == "部分成交后已结束"  # #932：撤了剩余，不再挂着
    assert partial["filledVolume"] == 1
    partial_app.state.store.close()


async def test_order_gone_after_its_trading_day_expires_instead_of_staying_unknown(tmp_path: Path) -> None:
    # 09-24 production: two m2701 limit orders last read ALIVE after 09-23's close, then dropped
    # from the broker's list at settlement; a cancel the next morning left them "结果未知" for good.
    gateway = Gateway()
    app = application(tmp_path / "expired.sqlite3", gateway)
    orders = app.state.simulation_orders
    ids = [(await confirm(app, proposal(app, turn=f"turn-exp-{i}", volume=2), key=f"confirm-exp-{i}"))["clientOrderId"]
           for i in range(3)]
    gone = {"ordersInitialized": True, "found": False}
    gateway.query_report = gone
    same_day = await orders.query(ids[0], user_id=USER)
    assert same_day["displayStatus"] == "已受理"  # found=false within the day stays a stale read
    assert same_day["lastReadError"] == "stale_order_report"

    connection = app.state.store.connection
    with connection:
        # The order's own day (2026-10-12: its insert time, here when it was placed) is what ended.
        connection.execute("UPDATE tqkq_sim_order_links SET last_synced_at = ?, created_at = ?",
                           ((datetime.now(UTC) - timedelta(days=4)).isoformat(),) * 2)
        connection.execute("UPDATE tqkq_sim_order_links SET cancel_phase = 'UNKNOWN' WHERE client_order_id = ?", (ids[1],))
        connection.execute("""INSERT INTO account_trades (trading_account_id, trade_id, client_order_id, symbol, direction,
            offset, price, volume, trade_date_time, trading_day, captured_at)
            VALUES (?, 'trade-exp', ?, 'SHFE.au2612', 'BUY', 'OPEN', 824.0, 1, 1000, '2026-09-23', '2026-09-23T07:00:00+00:00')""",
                           (app.state.store.default_trading_account_id, ids[2]))
    expired = await orders.query(ids[0], user_id=USER)
    assert expired["displayStatus"] == "已撤销"
    assert expired["report"]["order"]["expired"] is True
    cancel_unknown = await orders.query(ids[1], user_id=USER)
    assert (cancel_unknown["displayStatus"], cancel_unknown["cancelPhase"]) == ("已撤销", "CONFIRMED")
    partly_filled = await orders.query(ids[2], user_id=USER)
    assert (partly_filled["displayStatus"], partly_filled["filledVolume"]) == ("部分成交后已结束", 1)
    assert gateway.submit_count == 3 and gateway.cancel_count == 0
    app.state.store.close()


def _calendar(start: str, end: str, holiday: tuple[str, str]) -> list[str]:
    days, day = [], datetime.fromisoformat(start).date()
    while day <= datetime.fromisoformat(end).date():
        if day.weekday() < 5 and not holiday[0] <= day.isoformat() <= holiday[1]:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


class CalendarProvider(NormalProvider):
    """The sidecar's calendar summary with its window of trading days; None = unreadable."""

    def __init__(self, days: list[str] | None) -> None:
        super().__init__()
        self.days = days

    def market_calendar(self) -> dict[str, Any]:
        if self.days is None:
            raise RuntimeError("sidecar_market_calendar_unavailable")
        return {"tradingDay": self.days[-1], "previousTradingDay": self.days[-2],
                "recentTradingDays": self.days[-11:-1][::-1], "tradingDays": self.days}


@pytest.mark.parametrize(("holiday", "entered", "now", "ended"), (
    # 国庆（示意：10-01～10-07 休市、10-08 开市）
    (("2026-10-01", "2026-10-07"), "2026-09-30 10:00", "2026-10-08 10:00", True),    # 节前白天的单：节后首日已失效
    (("2026-10-01", "2026-10-07"), "2026-09-30 19:00", "2026-10-08 10:00", False),   # 节前晚上挂的单属于节后首日
    (("2026-10-01", "2026-10-07"), "2026-09-30 19:00", "2026-10-08 18:30", True),    # 节后首日 18:00 换日后才失效
    (("2026-10-01", "2026-10-07"), "2026-10-09 21:30", "2026-10-10 10:00", False),   # 周五夜盘的单属于周一
    (("2026-10-01", "2026-10-07"), "2026-10-13 19:30", "2026-10-14 10:00", False),   # 18–21 点挂的单归下一交易日
    # 春节（示意：2027-02-08～02-12 休市、02-15 开市）
    (("2027-02-08", "2027-02-12"), "2027-02-05 19:00", "2027-02-15 10:00", False),
    (("2027-02-08", "2027-02-12"), "2027-02-05 14:00", "2027-02-15 10:00", True),
))
async def test_order_expiry_follows_the_exchange_calendar(
    tmp_path: Path, holiday: tuple[str, str], entered: str, now: str, ended: bool,
) -> None:
    """2026-10-12：一张限价单只活它挂进去的那个交易日（按它自己的委托时间，不按最后一次回报）；
    双方都走同一个交易日口径和交易所日历，节后首日、周五夜盘与 18–21 点都按同一规则判。"""
    from trader_runtime import exchange_calendar

    exchange_calendar._cache.clear()
    year = holiday[0][:4]
    provider = CalendarProvider(_calendar(f"{year}-01-01", f"{year}-12-31", holiday))
    app = application(tmp_path / "calendar.sqlite3", Gateway(), provider)
    orders = app.state.simulation_orders
    local = datetime.fromisoformat(now).replace(tzinfo=SHANGHAI)
    orders.trading_clock = lambda: local
    link = {"createdAt": datetime.fromisoformat(entered).replace(tzinfo=SHANGHAI).isoformat(), "report": None}
    from trader_runtime.exchange_calendar import calendar_trading_days
    assert orders._trading_day_ended(link, calendar_trading_days(provider)) is ended
    app.state.store.close()


async def test_order_gone_without_a_calendar_expires_by_the_weekend_rule(tmp_path: Path) -> None:
    """日历读不到（或回放这类没有日历的 provider，#790 review）：按周末规则判，快期不再列出的过期单照常
    记为已撤销/部分成交，不永远停在 stale_order_report。"""
    from trader_runtime import exchange_calendar

    exchange_calendar._cache.clear()
    provider = CalendarProvider(None)
    gateway = Gateway()
    app = application(tmp_path / "no-calendar.sqlite3", gateway, provider)
    orders = app.state.simulation_orders
    order_id = (await confirm(app, proposal(app, turn="turn-no-cal")))["clientOrderId"]
    gateway.query_report = {"ordersInitialized": True, "found": False}
    with app.state.store.connection as connection:
        connection.execute("UPDATE tqkq_sim_order_links SET last_synced_at = ?, created_at = ?",
                           ((datetime.now(UTC) - timedelta(days=4)).isoformat(),) * 2)
    expired = await orders.query(order_id, user_id=USER)
    assert expired["report"]["order"]["expired"] is True and expired["lastReadError"] != "stale_order_report"
    app.state.store.close()


@pytest.mark.parametrize(("field", "value"), (("direction", "SELL"), ("limitPrice", 822.5)))
async def test_gateway_identity_mismatch_stops_projection(
    tmp_path: Path, field: str, value: str | float,
) -> None:
    gateway = Gateway()
    original_submit = gateway.submit_order_once

    async def mismatched(request: dict[str, Any]) -> dict[str, Any]:
        report = await original_submit(request)
        report["order"][field] = value
        return report

    gateway.submit_order_once = mismatched  # type: ignore[method-assign]
    app = application(tmp_path / "runtime.sqlite3", gateway)
    result = await confirm(app, proposal(app))
    order = result["simulationOrder"]
    assert order["remoteStatus"] == "IDENTITY_MISMATCH"
    assert order["displayStatus"] == "身份异常"
    assert order["submitPhase"] == "UNKNOWN"
    app.state.store.close()


@pytest.mark.parametrize(("field", "value", "anomaly"), [
    ("direction", "SELL", "IDENTITY_MISMATCH"),
    ("volumeLeft", 3, "REPORT_INVALID"),
])
async def test_cancel_anomaly_settles_call_without_overwriting_confirmed_fills(
    tmp_path: Path, field: str, value: str | int, anomaly: str,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "cancel-anomaly.sqlite3", gateway)
    result = await confirm(app, proposal(app, volume=2))
    identifier = result["clientOrderId"]
    request = result["simulationOrder"]["request"]
    gateway.query_report = gateway.report(request, volume_left=1)
    previous = await app.state.simulation_orders.query(identifier, user_id=USER)
    gateway.cancel_report = gateway.report(request, status="FINISHED", volume_left=0)
    gateway.cancel_report["order"][field] = value
    order = await app.state.simulation_orders.cancel(identifier, user_id=USER)
    assert order["submitPhase"] == "CONFIRMED"
    assert order["cancelPhase"] == "UNKNOWN"
    assert order["remoteStatus"] == anomaly
    assert order["filledVolume"] == previous["filledVolume"] == 1
    assert order["report"] == previous["report"]
    assert (await app.state.simulation_orders.query(identifier, user_id=USER))["remoteStatus"] == anomaly
    with pytest.raises(SimulationOrderError, match="order_not_cancelable"):
        await app.state.simulation_orders.cancel(identifier, user_id=USER)
    assert gateway.submit_count == gateway.cancel_count == 1
    app.state.store.close()


async def test_user_query_is_written_to_proposal_audit(tmp_path: Path) -> None:
    gateway = Gateway()
    app = application(tmp_path / "query-audit.sqlite3", gateway)
    result = await confirm(app, proposal(app))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as client:
        assert (await client.post(
            "/api/auth/local", headers={"Authorization": "Bearer test-local-token"},
        )).status_code == 200
        response = await client.post(
            f"/api/simulation/orders/{result['clientOrderId']}/query"
        )
    assert response.status_code == 200
    actions = app.state.store.connection.execute(
        "SELECT action FROM proposal_audit WHERE proposal_id = ?",
        (result["proposalId"],),
    ).fetchall()
    assert "simulation-query" in {row["action"] for row in actions}
    app.state.store.close()


async def test_first_order_list_returns_recovered_cancellation(tmp_path: Path) -> None:
    database = tmp_path / "cancelled-on-start.sqlite3"
    first = application(database, Gateway())
    result = await confirm(first, proposal(first))
    request = result["simulationOrder"]["request"]
    assert result["simulationOrder"]["displayStatus"] == "已受理"
    assert result["simulationOrder"]["request"]["symbol"] == "SHFE.au2612"
    first.state.store.close()

    gateway = Gateway()
    gateway.request = request
    gateway.query_report = gateway.report(
        request, status="FINISHED", volume_left=request["volume"]
    )
    restarted = application(database, gateway)
    response = await list_orders(restarted)
    assert response.status_code == 200
    assert response.json()["items"][0]["displayStatus"] == "已撤销"
    assert gateway.query_count == 1
    assert gateway.submit_count == 0
    restarted.state.store.close()


async def test_first_order_list_returns_recovered_fill(tmp_path: Path) -> None:
    database = tmp_path / "filled-on-start.sqlite3"
    first = application(database, Gateway())
    result = await confirm(first, proposal(first))
    request = result["simulationOrder"]["request"]
    assert result["simulationOrder"]["displayStatus"] == "已受理"
    first.state.store.close()

    gateway = Gateway()
    gateway.request = request
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=0)
    restarted = application(database, gateway)
    response = await list_orders(restarted)
    assert response.status_code == 200
    assert response.json()["items"][0]["displayStatus"] == "已成交"
    assert gateway.query_count == 1
    assert gateway.submit_count == 0
    restarted.state.store.close()


async def test_order_list_waits_for_remote_history_initialization(
    tmp_path: Path, monkeypatch,
) -> None:
    # The route's recovery cadence is a module constant (production 1 s): the
    # wait itself — three queries until ordersInitialized — is the assertion,
    # not the pacing.
    import trader_runtime.routes.execution as execution_routes
    monkeypatch.setattr(execution_routes, "ORDER_RECOVERY_INTERVAL_SECONDS", 0.01)
    database = tmp_path / "history-on-start.sqlite3"
    first = application(database, Gateway())
    result = await confirm(first, proposal(first))
    request = result["simulationOrder"]["request"]
    first.state.store.close()

    gateway = Gateway()
    gateway.request = request
    gateway.query_reports = [
        {"ordersInitialized": False, "found": False},
        {"ordersInitialized": False, "found": False},
        gateway.report(request, status="FINISHED", volume_left=request["volume"]),
    ]
    restarted = application(database, gateway)
    response = await asyncio.wait_for(list_orders(restarted), timeout=5)
    assert response.status_code == 200
    assert response.json()["items"][0]["displayStatus"] == "已撤销"
    assert gateway.query_count == 3
    assert gateway.submit_count == 0
    restarted.state.store.close()


async def test_live_order_poll_is_one_query_and_never_waits_for_terminal_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live-poll.sqlite3"
    first = application(database, Gateway())
    result = await confirm(first, proposal(first))
    request = result["simulationOrder"]["request"]
    first.state.store.close()

    gateway = Gateway()
    gateway.request = request
    gateway.query_reports = [
        {"ordersInitialized": False, "found": False},
        gateway.report(request, status="FINISHED", volume_left=0),
    ]
    restarted = application(database, gateway)
    async with AsyncClient(
        transport=ASGITransport(app=restarted), base_url="http://test",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as client:
        assert (await client.post(
            "/api/auth/local", headers={"Authorization": "Bearer test-local-token"},
        )).status_code == 200
        response = await asyncio.wait_for(
            client.get("/api/simulation/orders?recoveryWait=false"), timeout=1
        )
    assert response.status_code == 200
    assert gateway.query_count == 1
    assert gateway.submit_count == 0
    restarted.state.store.close()


async def test_order_list_without_pending_recovery_returns_without_gateway_query(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "empty-on-start.sqlite3", gateway)
    response = await asyncio.wait_for(list_orders(app), timeout=1)
    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert gateway.query_count == 0
    assert gateway.submit_count == 0
    app.state.store.close()


async def test_final_confirmation_rejects_stale_quote_and_non_tick_price(
    tmp_path: Path,
) -> None:
    stale_provider = NormalProvider()
    stale_app = application(tmp_path / "stale.sqlite3", Gateway(), stale_provider)
    stale = proposal(stale_app)
    stale_provider.age_seconds = 16.0
    with pytest.raises(SimulationOrderError, match="quote_stale"):
        await confirm(stale_app, stale)
    stale_app.state.store.close()

    frozen_provider = NormalProvider()
    frozen_provider.quote_datetime = (
        datetime.now(SHANGHAI) - timedelta(seconds=16)
    ).strftime("%Y-%m-%d %H:%M:%S.%f")
    frozen_app = application(tmp_path / "frozen.sqlite3", Gateway(), frozen_provider)
    with pytest.raises(SimulationOrderError, match="quote_stale"):
        proposal(frozen_app)
    frozen_app.state.store.close()

    tick_provider = NormalProvider()
    tick_provider.price_tick = 0.1
    tick_provider.bid = 823.1
    tick_app = application(tmp_path / "tick.sqlite3", Gateway(), tick_provider)
    off_tick = proposal(tick_app)
    tick_provider.price_tick = 0.5
    with pytest.raises(SimulationOrderError, match="limit_price_not_on_tick"):
        await confirm(tick_app, off_tick)
    tick_app.state.store.close()


def test_closed_market_is_reported_before_quote_staleness(tmp_path: Path) -> None:
    provider = NormalProvider()
    provider.market_session_status = "closed"
    provider.age_seconds = 3_600.0
    provider.quote_datetime = (
        datetime.now(SHANGHAI) - timedelta(hours=1)
    ).strftime("%Y-%m-%d %H:%M:%S.%f")
    gateway = Gateway()
    app = application(tmp_path / "closed-market.sqlite3", gateway, provider)

    with pytest.raises(SimulationOrderError, match="market_session_closed"):
        proposal(app)

    assert gateway.submit_count == 0
    app.state.store.close()


def test_subsecond_future_quote_is_accepted_but_large_clock_skew_fails_closed(
    tmp_path: Path,
) -> None:
    near_provider = NormalProvider()
    near_provider.quote_datetime = (
        datetime.now(SHANGHAI) + timedelta(milliseconds=500)
    ).strftime("%Y-%m-%d %H:%M:%S.%f")
    near_app = application(tmp_path / "near-future.sqlite3", Gateway(), near_provider)
    assert proposal(near_app)["status"] == "approval_required"
    near_app.state.store.close()

    far_provider = NormalProvider()
    far_provider.quote_datetime = (
        datetime.now(SHANGHAI) + timedelta(seconds=5)
    ).strftime("%Y-%m-%d %H:%M:%S.%f")
    far_app = application(tmp_path / "far-future.sqlite3", Gateway(), far_provider)
    with pytest.raises(SimulationOrderError, match="quote_stale"):
        proposal(far_app)
    far_app.state.store.close()


def test_one_sided_book_only_rejects_the_missing_price_intent(tmp_path: Path) -> None:
    provider = NormalProvider()
    provider.ask = None
    app = application(tmp_path / "one-sided.sqlite3", Gateway(), provider)
    session = app.state.broker.create_session("local", "native", USER)
    passive = app.state.proposals.create_trade(
        session.session_id, "passive", symbol="SHFE.au2612", side="buy_open",
        volume=1, price_intent="passive", reason="use available bid",
    )
    assert passive["limitPrice"] == provider.bid
    with pytest.raises(SimulationOrderError, match="limit_price_unavailable"):
        app.state.proposals.create_trade(
            session.session_id, "aggressive", symbol="SHFE.au2612", side="buy_open",
            volume=1, price_intent="aggressive", reason="ask unavailable",
        )
    app.state.store.close()


def _tool_status(app):
    """The full runtime status the AI tools see in the app (create_trade_proposal validates it as
    RuntimeStatus for its readiness gate)."""
    return lambda mode: {
        "mode": mode, "adapter": "native-agent", "runtimeConnected": True, "agentConnected": True,
        "connected": True, "version": "1.6.5", **app.state.data_provider.runtime_status(),
    }


def test_autonomy_adds_only_execution_and_stop_state_beside_thin_order_link(
    tmp_path: Path,
) -> None:
    app = application(tmp_path / "runtime.sqlite3", Gateway())
    names = {
        row["name"]
        for row in app.state.store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        if "execution" in row["name"] or "order" in row["name"]
    }
    assert names == {
        "tqkq_sim_order_links",
        "ai_execution_authorizations",
        "ai_execution_controls",
        "ai_stop_orders",
        "strategy_execution_attempts",
        "strategy_execution_legs",
    }
    columns = {
        row["name"]
        for row in app.state.store.connection.execute(
            "PRAGMA table_info(tqkq_sim_order_links)"
        )
    }
    assert {
        "id",
        "user_id",
        "proposal_id",
        "client_order_id",
        "request_json",
        "submit_phase",
        "cancel_phase",
        "last_remote_status",
        "last_report_json",
        "last_synced_at",
        "created_at",
        "updated_at",
    } <= columns
    app.state.store.close()


async def test_bounded_ai_session_executes_without_per_order_confirmation(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "ai-auto.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    authorization = app.state.autonomy.activate(
        user_id=USER,
        session_id=session.session_id,
        symbols=["SHFE.au2612"],
        permissions=["open", "cancel", "close", "stop"],
        max_order_volume=1,
        max_orders=1,
        duration_minutes=30,
    )

    result = await app.state.autonomy.execute_trade(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        side="buy_open",
        volume=1,
        price_intent="passive",
        reason="AI 在已授权模拟会话中自主开仓",
    )

    assert authorization["status"] == "ACTIVE"
    assert gateway.submit_count == 1
    assert result["simulationOrder"]["displayStatus"] == "已受理"
    assert result["simulationOrder"]["request"]["symbol"] == "SHFE.au2612"
    proposal_record = app.state.store.get(result["proposalId"])
    assert proposal_record["status"] == "candidate_recorded"
    assert proposal_record["payload"]["authorizationMode"] == "delegated_session"
    assert proposal_record["payload"]["authorizationId"] == authorization["authorizationId"]
    with pytest.raises(AutonomyError, match="authorization_order_limit_reached"):
        await app.state.autonomy.execute_trade(
            user_id=USER,
            session_id=session.session_id,
            symbol="SHFE.au2612",
            side="sell_open",
            volume=1,
            price_intent="passive",
            reason="超过会话委托次数",
        )
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_unsent_ai_attempt_releases_bounded_authorization_quota(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    gateway.submit_report = {
        "ordersInitialized": False,
        "found": False,
        "commandStatus": "NOT_DISPATCHED",
    }
    app = application(tmp_path / "ai-unsent-quota.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    app.state.autonomy.activate(
        user_id=USER,
        session_id=session.session_id,
        symbols=["SHFE.au2612"],
        permissions=["open"],
        max_order_volume=1,
        max_orders=1,
        duration_minutes=30,
    )

    unsent = await app.state.autonomy.execute_trade(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        side="buy_open",
        volume=1,
        price_intent="passive",
        reason="网关明确未发送不消耗额度",
    )
    assert unsent["simulationOrder"]["submitPhase"] == "PREPARED"
    assert app.state.autonomy.current(user_id=USER)["usedOrders"] == 0

    gateway.submit_report = None
    sent = await app.state.autonomy.execute_trade(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        side="sell_open",
        volume=1,
        price_intent="passive",
        reason="未发送后仍可使用同一有限额度",
    )
    assert sent["simulationOrder"]["displayStatus"] == "已受理"
    assert app.state.autonomy.current(user_id=USER)["usedOrders"] == 1
    app.state.store.close()


async def test_runtime_restart_refreshes_internal_lease_without_disarming_stop(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ai-restart-boundary.sqlite3"
    gateway = Gateway()
    initial = application(database, gateway)
    session = initial.state.broker.create_session("local", "native", USER)
    authorization = initial.state.autonomy.activate(
        user_id=USER,
        session_id=session.session_id,
        symbols=["SHFE.au2612"],
        permissions=["open", "cancel", "close", "stop"],
        max_order_volume=1,
        max_orders=10,
        duration_minutes=30,
    )
    with initial.state.store.connection:
        initial.state.store.connection.execute(
            """INSERT INTO ai_stop_orders
            (id, user_id, authorization_id, session_id, symbol, position_direction,
             position_bucket, volume, trigger_price, price_intent, status,
             close_proposal_id, close_client_order_id, triggered_at, last_error,
             created_at, updated_at)
            VALUES ('stop-restart', ?, ?, ?, 'SHFE.au2612', 'long', 'today', 1,
                    '700.00', 'aggressive', 'ARMED', NULL, NULL, NULL, NULL, ?, ?)""",
            (USER, authorization["authorizationId"], session.session_id, quote_now(), quote_now()),
        )
    initial.state.store.close()

    restarted = application(database, gateway)
    # The lease holder recovers at takeover, before any loop runs.
    await restarted.state.instance_lease.start()
    await restarted.state.instance_lease.stop()

    assert restarted.state.autonomy.current(user_id=USER)["status"] == "EXPIRED"
    assert restarted.state.autonomy.list_stops(user_id=USER)[0]["status"] == "ARMED"
    await restarted.state.autonomy.execute_trade(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        side="buy_open",
        volume=1,
        price_intent="passive",
        reason="重启后自动建立新的模拟执行租约",
    )
    assert restarted.state.autonomy.current(user_id=USER)["status"] == "ACTIVE"
    assert gateway.submit_count == 1
    restarted.state.store.close()


async def test_explicit_emergency_stop_is_not_bypassed_by_automatic_lease(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "ai-emergency-boundary.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    await app.state.autonomy.execute_trade(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        side="buy_open",
        volume=1,
        price_intent="passive",
        reason="先建立默认模拟执行租约",
    )
    await app.state.autonomy.deactivate(user_id=USER, emergency=True)

    with pytest.raises(AutonomyError, match="ai_execution_stopped"):
        await app.state.autonomy.execute_trade(
            user_id=USER,
            session_id=session.session_id,
            symbol="SHFE.au2612",
            side="sell_open",
            volume=1,
            price_intent="passive",
            reason="急停后不得自动恢复",
        )
    resumed = app.state.autonomy.resume_runtime_access(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
    )
    assert resumed["status"] == "ACTIVE"
    assert resumed["maxOrderVolume"] == 2_147_483_647
    assert resumed["maxOrders"] == 2_147_483_647
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_ai_can_cancel_its_existing_simulation_order_once(tmp_path: Path) -> None:
    gateway = Gateway()
    app = application(tmp_path / "ai-cancel.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    app.state.autonomy.activate(
        user_id=USER,
        session_id=session.session_id,
        symbols=["SHFE.au2612"],
        permissions=["open", "cancel"],
        max_order_volume=1,
        max_orders=2,
        duration_minutes=30,
    )
    opened = await app.state.autonomy.execute_trade(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        side="buy_open",
        volume=1,
        price_intent="passive",
        reason="测试自主撤单",
    )
    cancelled = await app.state.autonomy.cancel_order(
        user_id=USER, client_order_id=opened["clientOrderId"]
    )
    assert cancelled["displayStatus"] == "已撤销"
    assert gateway.submit_count == 1
    assert gateway.cancel_count == 1
    app.state.store.close()


async def test_ai_emergency_stop_revokes_session_cancels_orders_and_disarms_stops(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "ai-emergency.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    app.state.autonomy.activate(
        user_id=USER,
        session_id=session.session_id,
        symbols=["SHFE.au2612"],
        permissions=["open", "cancel", "stop"],
        max_order_volume=1,
        max_orders=2,
        duration_minutes=30,
    )
    await app.state.autonomy.execute_trade(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        side="buy_open",
        volume=1,
        price_intent="passive",
        reason="验证急停撤销 AI 委托",
    )
    stop = await app.state.autonomy.create_stop(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        position_direction="long",
        position_bucket="today",
        volume=1,
        trigger_price=823.5,
    )

    result = await app.state.autonomy.deactivate(user_id=USER, emergency=True)

    assert result["authorization"]["status"] == "EMERGENCY_STOP"
    assert len(result["cancelledOrders"]) == 1
    assert result["cancelledOrders"][0]["displayStatus"] == "已撤销"
    assert gateway.cancel_count == 1
    cancelled_stop = next(
        item for item in app.state.autonomy.list_stops(user_id=USER)
        if item["stopId"] == stop["stopId"]
    )
    assert cancelled_stop["status"] == "CANCELLED"
    with pytest.raises(AutonomyError, match="ai_execution_stopped"):
        await app.state.autonomy.execute_trade(
            user_id=USER,
            session_id=session.session_id,
            symbol="SHFE.au2612",
            side="sell_open",
            volume=1,
            price_intent="passive",
            reason="急停后不得下单",
        )
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_emergency_stop_does_not_wait_for_inflight_gateway_and_cancels_late_accept(
    tmp_path: Path,
) -> None:
    gateway = HangingGateway()
    app = application(tmp_path / "ai-inflight-emergency.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    app.state.autonomy.activate(
        user_id=USER,
        session_id=session.session_id,
        symbols=["SHFE.au2612"],
        permissions=["open", "cancel"],
        max_order_volume=1,
        max_orders=1,
        duration_minutes=30,
    )
    submit_task = asyncio.create_task(app.state.autonomy.execute_trade(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        side="buy_open",
        volume=1,
        price_intent="passive",
        reason="验证急停不等待网关",
    ))
    await asyncio.wait_for(gateway.submit_started.wait(), timeout=0.5)

    emergency = await asyncio.wait_for(
        app.state.autonomy.deactivate(user_id=USER, emergency=True), timeout=0.5
    )

    assert emergency["authorization"]["status"] == "EMERGENCY_STOP"
    assert not submit_task.done()
    gateway.submit_release.set()
    with pytest.raises(AutonomyError, match="ai_execution_stopped"):
        await submit_task
    assert gateway.submit_count == 1
    assert gateway.cancel_count == 1
    assert app.state.simulation_orders.list(user_id=USER)[0]["displayStatus"] == "已撤销"
    app.state.store.close()


async def test_emergency_stop_fences_waiting_stop_creation(tmp_path: Path) -> None:
    gateway = Gateway()
    app = application(tmp_path / "ai-stop-emergency-race.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    app.state.autonomy.activate(
        user_id=USER,
        session_id=session.session_id,
        symbols=["SHFE.au2612"],
        permissions=["stop"],
        max_order_volume=1,
        max_orders=1,
        duration_minutes=30,
    )

    await app.state.autonomy._action_lock.acquire()
    create_task = asyncio.create_task(app.state.autonomy.create_stop(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        position_direction="long",
        position_bucket="today",
        volume=1,
        trigger_price=823.5,
    ))
    await asyncio.sleep(0)
    assert not create_task.done()
    emergency_task = asyncio.create_task(
        app.state.autonomy.deactivate(user_id=USER, emergency=True)
    )
    emergency = await asyncio.wait_for(emergency_task, timeout=0.5)
    assert emergency["authorization"]["status"] == "EMERGENCY_STOP"
    app.state.autonomy._action_lock.release()

    with pytest.raises(AutonomyError, match="ai_execution_stopped"):
        await create_task
    assert app.state.autonomy.list_stops(user_id=USER) == []
    assert gateway.submit_count == 0
    app.state.store.close()


async def test_owner_can_remove_armed_stop_after_normal_session_expiry(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "ai-stop-cancel.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    app.state.autonomy.activate(
        user_id=USER,
        session_id=session.session_id,
        symbols=["SHFE.au2612"],
        permissions=["stop"],
        max_order_volume=1,
        max_orders=1,
        duration_minutes=30,
    )
    stop = await app.state.autonomy.create_stop(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        position_direction="long",
        position_bucket="today",
        volume=1,
        trigger_price=823.5,
    )
    await app.state.autonomy.deactivate(user_id=USER, emergency=False)

    cancelled = app.state.autonomy.cancel_stop(
        user_id=USER, stop_id=stop["stopId"]
    )

    assert cancelled["status"] == "CANCELLED"
    assert gateway.submit_count == 0
    app.state.store.close()


async def test_deterministic_stop_triggers_one_reduce_only_close(tmp_path: Path) -> None:
    gateway = Gateway()
    provider = NormalProvider()
    app = application(tmp_path / "ai-stop.sqlite3", gateway, provider)
    session = app.state.broker.create_session("local", "native", USER)
    app.state.autonomy.activate(
        user_id=USER,
        session_id=session.session_id,
        symbols=["SHFE.au2612"],
        permissions=["stop", "close"],
        max_order_volume=1,
        max_orders=1,
        duration_minutes=30,
    )
    stop = await app.state.autonomy.create_stop(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        position_direction="long",
        position_bucket="today",
        volume=1,
        trigger_price=823.5,
    )
    assert stop["status"] == "ARMED"
    assert gateway.submit_count == 0

    provider.last = 823.0
    provider.quote_datetime = quote_now()
    row = app.state.store.connection.execute(
        "SELECT * FROM ai_stop_orders WHERE id = ?", (stop["stopId"],)
    ).fetchone()
    assert row is not None
    await app.state.autonomy._check_stop(row)

    triggered = app.state.autonomy.list_stops(user_id=USER)[0]
    assert triggered["status"] == "SUBMITTED", triggered["lastError"]
    assert triggered["closeClientOrderId"]
    assert gateway.submit_count == 1
    assert gateway.request is not None
    assert gateway.request["direction"] == "SELL"
    assert gateway.request["offset"] == "CLOSETODAY"

    gateway.query_report = gateway.report(
        gateway.request, status="FINISHED", volume_left=0
    )
    submitted_row = app.state.store.connection.execute(
        "SELECT * FROM ai_stop_orders WHERE id = ?", (stop["stopId"],)
    ).fetchone()
    assert submitted_row is not None
    await app.state.autonomy._reconcile_stop(submitted_row)
    completed = app.state.autonomy.list_stops(user_id=USER)[0]
    assert completed["status"] == "EXECUTED"
    assert gateway.query_count == 1

    await app.state.autonomy._check_stop(row)
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_unknown_stop_close_keeps_identity_and_only_queries_original_order(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    gateway.submit_error = True
    provider = NormalProvider()
    app = application(tmp_path / "ai-stop-unknown.sqlite3", gateway, provider)
    session = app.state.broker.create_session("local", "native", USER)
    app.state.autonomy.activate(
        user_id=USER,
        session_id=session.session_id,
        symbols=["SHFE.au2612"],
        permissions=["stop"],
        max_order_volume=1,
        max_orders=1,
        duration_minutes=30,
    )
    stop = await app.state.autonomy.create_stop(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        position_direction="long",
        position_bucket="today",
        volume=1,
        trigger_price=823.5,
    )
    provider.last = 823.0
    provider.quote_datetime = quote_now()
    armed = app.state.store.connection.execute(
        "SELECT * FROM ai_stop_orders WHERE id = ?", (stop["stopId"],)
    ).fetchone()
    assert armed is not None
    await app.state.autonomy._check_stop(armed)
    unknown = app.state.autonomy.list_stops(user_id=USER)[0]
    assert unknown["status"] == "UNKNOWN"
    assert unknown["closeClientOrderId"]
    assert gateway.submit_count == 1

    gateway.submit_error = False
    gateway.query_report = gateway.report(gateway.request)
    unknown_row = app.state.store.connection.execute(
        "SELECT * FROM ai_stop_orders WHERE id = ?", (stop["stopId"],)
    ).fetchone()
    assert unknown_row is not None
    await app.state.autonomy._reconcile_stop(unknown_row)
    recovered = app.state.autonomy.list_stops(user_id=USER)[0]
    assert recovered["status"] == "SUBMITTED"
    assert gateway.query_count == 1
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_ai_unknown_submit_is_queried_and_never_resubmitted(tmp_path: Path) -> None:
    gateway = Gateway()
    gateway.submit_error = True
    app = application(tmp_path / "ai-unknown.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    app.state.autonomy.activate(
        user_id=USER,
        session_id=session.session_id,
        symbols=["SHFE.au2612"],
        permissions=["open"],
        max_order_volume=1,
        max_orders=1,
        duration_minutes=30,
    )
    result = await app.state.autonomy.execute_trade(
        user_id=USER,
        session_id=session.session_id,
        symbol="SHFE.au2612",
        side="buy_open",
        volume=1,
        price_intent="passive",
        reason="验证 UNKNOWN 不重发",
    )
    assert result["simulationOrder"]["displayStatus"] == "结果未知"
    assert gateway.submit_count == 1

    gateway.submit_error = False
    gateway.query_report = Gateway.report(result["simulationOrder"]["request"])
    queried = await app.state.autonomy.query_order(
        user_id=USER, client_order_id=result["clientOrderId"]
    )
    assert queried["displayStatus"] == "已受理"
    assert gateway.query_count == 1
    assert gateway.submit_count == 1
    app.state.store.close()


async def test_agent_execution_tools_are_visible_only_with_runtime_autonomy(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "ai-tools.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(
        app.state.data_provider,
        _tool_status(app),
        proposals=app.state.proposals,
        autonomy=app.state.autonomy,
    )
    tools.app_capabilities = app.state.app_capabilities
    definitions = {item["name"] for item in tools.definitions()}
    assert {
        "resume_simulation_execution",
        "stop_simulation_execution",
        "execute_simulation_trade",
        "query_simulation_order",
        "cancel_simulation_order",
        "set_simulation_stop_loss",
    } <= definitions

    executed, _ = await tools.call_async(
        tool="execute_simulation_trade",
        arguments={
            "symbol": "SHFE.au2612",
            "side": "buy_open",
            "volume": 1,
            "priceIntent": "passive",
            "reason": "模拟盘直接执行，不询问逐单授权",
        },
        turn_id="turn-blocked",
        mode="local",
        session_id=session.session_id,
        user_id=USER,
    )
    assert executed["success"] is True
    assert gateway.submit_count == 1
    lease = app.state.autonomy.current(user_id=USER)
    assert lease is not None
    assert lease["maxOrderVolume"] == 2_147_483_647
    assert lease["maxOrders"] == 2_147_483_647
    assert lease["expiresAt"] == "9999-12-31T23:59:59+00:00"

    stopped, _ = await tools.call_async(
        tool="stop_simulation_execution",
        arguments={"emergency": True},
        turn_id="turn-stop",
        mode="local",
        session_id=session.session_id,
        user_id=USER,
    )
    assert stopped["success"] is True
    assert app.state.autonomy.current(user_id=USER)["status"] == "EMERGENCY_STOP"

    blocked, _ = await tools.call_async(
        tool="execute_simulation_trade",
        arguments={
            "symbol": "SHFE.au2612",
            "side": "sell_open",
            "volume": 1,
            "priceIntent": "passive",
            "reason": "急停后不能从普通交易请求自动恢复",
        },
        turn_id="turn-still-stopped",
        mode="local",
        session_id=session.session_id,
        user_id=USER,
    )
    assert blocked["success"] is False

    resumed, _ = await tools.call_async(
        tool="resume_simulation_execution",
        arguments={"symbol": "SHFE.au2612"},
        turn_id="turn-resume",
        mode="local",
        session_id=session.session_id,
        user_id=USER,
    )
    assert resumed["success"] is True
    assert app.state.autonomy.current(user_id=USER)["status"] == "ACTIVE"
    assert gateway.submit_count == 1
    app.state.store.close()


def test_codex_tool_can_only_create_candidate_not_order_commands(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "runtime.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(
        app.state.data_provider,
        _tool_status(app),
        proposals=app.state.proposals,
    )
    definitions = {item["name"]: item for item in tools.definitions()}
    assert "propose_order" in definitions
    assert not {"create_trade_proposal", "create_close_batch_proposal", "create_target_position_proposal"} & definitions.keys()
    assert (
        not {"submit_order", "query_order", "cancel_order", "close_position"}
        & definitions.keys()
    )
    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {
            "symbol": "SHFE.au2612",
            "side": "sell_open",
            "volume": 1,
            "priceIntent": "passive",
            "reason": "candidate only",
        }),
        turn_id="turn-tool",
        mode="local",
        session_id=session.session_id,
    )
    assert result["success"] is True
    assert event is not None and event[0] == "proposal.trade"
    assert gateway.submit_count == 0
    app.state.store.close()


def test_codex_tool_can_create_user_confirmed_close_candidate_without_sending(
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    app = application(tmp_path / "runtime-close-tool.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(
        app.state.data_provider,
        _tool_status(app),
        proposals=app.state.proposals,
    )

    definitions = {item["name"]: item for item in tools.definitions()}
    schema = definitions["propose_order"]["inputSchema"]
    assert set(schema["properties"]["act"]["anyOf"][0]["enum"]) == {"open", "close", "close_all", "target", "reverse"}
    assert schema["properties"]["bucket"]["anyOf"][0]["enum"] == ["today", "yesterday", "auto"]
    assert schema["properties"]["dir"]["anyOf"][0]["enum"] == ["long", "short"]

    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {
            "symbol": "SHFE.au2612",
            "side": "sell_close",
            "positionBucket": "today",
            "volume": 1,
            "priceIntent": "aggressive",
            "reason": "用户询问平今候选，仍需本人确认",
        }),
        turn_id="turn-close-tool",
        mode="local",
        session_id=session.session_id,
    )

    assert result["success"] is True
    assert event is not None and event[0] == "proposal.trade"
    assert event[1]["side"] == "sell_close"
    assert event[1]["offset"] == "CLOSETODAY"
    assert event[1]["positionBucket"] == "today"
    assert event[1]["status"] == "approval_required"
    assert gateway.submit_count == 0
    app.state.store.close()


def test_default_simulation_new_contract_has_no_hidden_size_budget_or_synthetic_stop(tmp_path):
    provider = NormalProvider()
    provider.canonical_symbol = "SHFE.rb2701"
    gateway = Gateway()
    app = application(tmp_path / "unlimited.sqlite", gateway, provider)
    session = app.state.broker.create_session("local", "native", USER)
    candidate = app.state.simulation_orders.build_candidate(
        user_id=USER, symbol="SHFE.rb2701", side="buy_open", volume=10000,
        price_intent="aggressive", reason="isolated candidate only",
    )
    assert candidate["riskCheck"]["passed"]
    assert candidate["volume"] == 10000
    assert "stopPrice" not in candidate
    assert not candidate["riskCheck"]["checks"]["lossEstimateAvailable"]
    authorization = app.state.autonomy.ensure_runtime_access(
        user_id=USER, session_id=session.session_id, symbol="SHFE.rb2701",
        permission="open", volume=10000,
    )
    assert "SHFE.rb2701" in authorization["allowedSymbols"]
    assert gateway.submit_count == 0


# --- batch close: one card, one confirmation, every order sent and reported ----------------------


async def test_close_batch_lists_every_position_bucket_and_one_confirmation_sends_them_all(tmp_path: Path) -> None:
    gateway = Gateway()
    provider = NormalProvider()
    app = application(tmp_path / "close-batch.sqlite3", gateway, provider)
    session = app.state.broker.create_session("local", "native", USER)
    # long 2 (today 1 + historical 1) and short 2 (today 1 + historical 1) on SHFE: four orders.
    card = app.state.proposals.create_close_batch(session.session_id, "turn-batch", reason="用户说全部平仓")
    assert card["status"] == "approval_required" and card["totalOrders"] == 4 and card["totalVolume"] == 4
    assert [(o["side"], o["offset"], o["positionBucket"], o["volume"]) for o in card["orders"]] == [
        ("sell_close", "CLOSETODAY", "today", 1), ("sell_close", "CLOSE", "historical", 1),
        ("buy_close", "CLOSETODAY", "today", 1), ("buy_close", "CLOSE", "historical", 1)]
    assert all(o["priceIntent"] == "aggressive" for o in card["orders"]) and gateway.submit_count == 0
    # Filters: one direction, or named symbols (exchange-less spelling accepted); nothing to close → conflict.
    longs = app.state.proposals.create_close_batch(session.session_id, "turn-long", reason="平多", direction="long")
    assert [o["side"] for o in longs["orders"]] == ["sell_close", "sell_close"]
    named = app.state.proposals.create_close_batch(session.session_id, "turn-named", reason="平沪金", symbols=["au2612"])
    assert named["totalOrders"] == 4
    from trader_runtime.proposals import ProposalConflict
    with pytest.raises(ProposalConflict) as missing:
        app.state.proposals.create_close_batch(session.session_id, "turn-none", reason="平螺纹", symbols=["rb2610"])
    assert missing.value.code == "no_position_to_close"
    # One confirmation: every order is prepared, then sent one by one and each reports back.
    result = await confirm(app, card, "confirm-batch")
    assert gateway.submit_count == 4 and result["status"] == "candidate_recorded"
    assert [o["displayStatus"] for o in result["simulationOrders"]] == ["已受理"] * 4
    assert len({o["clientOrderId"] for o in result["simulationOrders"]}) == 4
    assert app.state.store.get(card["proposalId"])["status"] == "candidate_recorded"
    listed = (await list_orders(app)).json()["items"]
    assert len(listed) == 4
    # The other cards are still open and can be rejected like any card.
    await app.state.proposals.reject(longs["proposalId"], user_id=USER, proposal_version=1, idempotency_key="reject-long")
    assert app.state.store.get(longs["proposalId"])["status"] == "rejected"
    app.state.store.close()


async def test_close_batch_over_a_closed_market_refuses_with_each_skipped_contract(tmp_path: Path) -> None:
    """#721: 休市时全部平仓——每个持仓都因行情不新鲜被跳过，拒绝必须带上每个合约和 builder 的
    原因码（工具层据此说中文），而不是一个光秃秃的 close_batch_no_order_available。"""
    provider = NormalProvider()
    provider.market_session_status = "closed"
    app = application(tmp_path / "close-batch-closed.sqlite3", Gateway(), provider)
    session = app.state.broker.create_session("local", "native", USER)
    from trader_runtime.proposals import ProposalConflict
    with pytest.raises(ProposalConflict) as refused:
        app.state.proposals.create_close_batch(session.session_id, "turn-closed", reason="休市时全部平仓")
    assert refused.value.code == "close_batch_no_order_available"
    assert str(refused.value) == "close_batch_no_order_available"  # str() keeps the code: logs/HTTP unchanged
    # au2612 long 2 (今 1 + 昨 1) and short 2 (今 1 + 昨 1): four legs, each skipped for the same reason.
    assert [(item["symbol"], item["reason"]) for item in refused.value.skipped] == [
        ("SHFE.au2612", "market_session_closed")] * 4
    app.state.store.close()


async def test_close_batch_with_nothing_to_close_refuses_plainly(tmp_path: Path) -> None:
    """没有持仓时同一拒绝说「没有可平的持仓」的来源：skipped 为空、code 仍是 no_position_to_close。"""
    provider = NormalProvider()
    provider.position_items = []
    app = application(tmp_path / "close-batch-empty.sqlite3", Gateway(), provider)
    session = app.state.broker.create_session("local", "native", USER)
    from trader_runtime.proposals import ProposalConflict
    with pytest.raises(ProposalConflict) as refused:
        app.state.proposals.create_close_batch(session.session_id, "turn-empty", reason="全部平仓")
    assert refused.value.code == "no_position_to_close"
    assert refused.value.skipped == []
    app.state.store.close()


async def test_close_batch_route_and_agent_tool_publish_the_card(tmp_path: Path) -> None:
    gateway = Gateway()
    app = application(tmp_path / "close-batch-route.sqlite3", gateway, NormalProvider())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        session_id = (await client.post("/api/sessions?chat=true", json={"mode": "local"})).json()["sessionId"]
        created = await client.post("/api/simulation/close-batch-proposals", json={"sessionId": session_id, "direction": "short", "reason": "平掉所有空单"})
        assert created.status_code == 201 and created.json()["totalOrders"] == 2
        empty = await client.post("/api/simulation/close-batch-proposals", json={"sessionId": session_id, "symbols": ["rb2610"], "reason": "平螺纹"})
        assert empty.status_code == 409
        events = [event for _, event in app.state.store.list_events(session_id)]
        assert [e["name"] for e in events if e["name"].startswith("proposal.")] == ["proposal.close_batch"]
        catalog = (await client.get("/api/agent/capabilities")).json()
        assert "POST /api/simulation/close-batch-proposals" in {item["operation"] for item in catalog["operations"]}
    tools = app.state.adapter.trader_tools if hasattr(app.state.adapter, "trader_tools") else None
    if tools is not None:
        assert "propose_order" in {d["name"] for d in tools.definitions()}
    assert gateway.submit_count == 0
    app.state.store.close()


def test_every_display_status_is_classified_and_none_defaults_to_sent() -> None:
    """The batch roll-up classifies by display status, so a status nobody classified must
    never fall into "已发送" — that claim is the whole point of the fix. This enumerates
    what `order_display_status` can actually return and fails when a new one appears."""
    import inspect
    import re

    from trader_runtime.simulation_orders import (
        NOT_SENT_STATUSES,
        SENT_STATUSES,
        UNKNOWN_STATUSES,
        close_batch_execution,
        order_display_status,
    )
    source = inspect.getsource(order_display_status)
    # Every Chinese literal in that function is a display status (the others are remote
    # status codes and dict keys), including the ones returned from a conditional.
    returned = {literal for literal in re.findall(r'"([^"]+)"', source)
                if re.search(r"[一-鿿]", literal)}
    assert returned == {"待发送", "已放弃", "结果未知", "身份异常", "回报异常",
                        "已受理", "部分成交", "部分成交后已结束", "已成交", "已拒绝", "已撤销"}
    classified = NOT_SENT_STATUSES | SENT_STATUSES | UNKNOWN_STATUSES
    assert returned <= classified and not (NOT_SENT_STATUSES & SENT_STATUSES)
    assert not (UNKNOWN_STATUSES & (NOT_SENT_STATUSES | SENT_STATUSES))
    # Semantics: PREPARED/abandoned never reached the gateway; a rejection or a cancellation
    # did, so both count as sent; an identity/report anomaly is exactly "unknown".
    assert NOT_SENT_STATUSES == {"待发送", "已放弃"}
    assert {"已拒绝", "已撤销"} <= SENT_STATUSES
    assert {"身份异常", "回报异常"} <= UNKNOWN_STATUSES
    counts = {status: close_batch_execution([{"displayStatus": status}]) for status in returned}
    for status in returned:
        bucket = "notSent" if status in NOT_SENT_STATUSES else "sent" if status in SENT_STATUSES else "unknown"
        assert counts[status][bucket] == 1, status
    # An unlisted status (a future display value) is unknown, never a send receipt.
    invented = close_batch_execution([{"displayStatus": "某种新状态"}, {}])
    assert invented["unknown"] == 2 and invented["sent"] == 0


def _child_ids(app, card: dict[str, Any]) -> list[str]:
    return [str(order["clientOrderId"]) for order in app.state.store.get(card["proposalId"])["payload"]["orders"]]


async def test_close_batch_reports_what_each_order_became_not_that_it_was_confirmed(tmp_path: Path) -> None:
    """A confirmation interrupted between sends must never be summarised as "sent"."""
    gateway = Gateway()
    app = application(tmp_path / "close-batch-interrupted.sqlite3", gateway, NormalProvider())
    session = app.state.broker.create_session("local", "native", USER)
    card = app.state.proposals.create_close_batch(session.session_id, "turn-batch", reason="全部平仓")
    assert card["totalOrders"] == 4
    orders = app.state.simulation_orders
    real_dispatch = orders.dispatch
    calls: list[str] = []

    async def interrupted(client_order_id: str, *, user_id: str):
        calls.append(client_order_id)
        if len(calls) == 2:
            raise KeyboardInterrupt("runtime restarted mid-confirmation")
        return await real_dispatch(client_order_id, user_id=user_id)

    orders.dispatch = interrupted  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        await confirm(app, card, "confirm-interrupted")
    orders.dispatch = real_dispatch  # type: ignore[method-assign]
    # The approval is recorded, one order went out and the rest were never sent.
    record = app.state.store.get(card["proposalId"])
    assert record["status"] == "candidate_recorded" and gateway.submit_count == 1
    ids = _child_ids(app, card)
    assert len(ids) == 4
    view = app.state.proposals.close_batch_view(record["payload"], user_id=USER)
    assert [item["displayStatus"] for item in view["simulationOrders"]] == ["已受理", "待发送", "待发送", "待发送"]
    assert view["execution"] == {"total": 4, "sent": 1, "notSent": 3, "unknown": 0, "filled": 0,
                                 "text": "已确认，1 笔已发送、3 笔未发送"}
    # What the model is told matches those facts, not the parent's approval status.
    assert app.state.proposals.close_batch_state(record) == "已确认，1 笔已发送、3 笔未发送"
    # Retrying the same confirmation reads the current facts back; nothing is re-sent.
    retried = await confirm(app, card, "confirm-interrupted")
    assert gateway.submit_count == 1
    assert retried["execution"]["sent"] == 1 and retried["execution"]["notSent"] == 3
    assert [item["clientOrderId"] for item in retried["simulationOrders"]] == ids
    # An unsent child is finished through the ordinary single-order path; no new order appears.
    assert (await orders.dispatch(ids[2], user_id=USER))["displayStatus"] == "已受理"
    assert orders.abandon(ids[3], user_id=USER)["displayStatus"] == "已放弃"
    assert gateway.submit_count == 2 and len(orders.list(user_id=USER)) == 4
    assert app.state.proposals.close_batch_state(record) == "已确认，2 笔已发送、2 笔未发送"
    app.state.store.close()


async def test_close_batch_marks_a_refused_send_as_not_sent_with_its_reason(tmp_path: Path) -> None:
    gateway = Gateway()
    app = application(tmp_path / "close-batch-refused.sqlite3", gateway, NormalProvider())
    session = app.state.broker.create_session("local", "native", USER)
    card = app.state.proposals.create_close_batch(session.session_id, "turn-batch", reason="全部平仓")
    orders = app.state.simulation_orders
    real_dispatch = orders.dispatch
    refused: list[str] = []

    async def refuse_second(client_order_id: str, *, user_id: str):
        refused.append(client_order_id)
        if len(refused) == 2:
            raise SimulationOrderError("close_offset_mismatch")
        return await real_dispatch(client_order_id, user_id=user_id)

    orders.dispatch = refuse_second  # type: ignore[method-assign]
    result = await confirm(app, card, "confirm-refused")
    orders.dispatch = real_dispatch  # type: ignore[method-assign]
    facts = result["simulationOrders"]
    # Refused before the gateway call: not sent, with the reason — never "结果未知".
    assert facts[1]["displayStatus"] == "待发送" and facts[1]["submitPhase"] == "PREPARED"
    assert facts[1]["lastReadError"] == "close_offset_mismatch"
    assert result["execution"]["notSent"] == 1 and result["execution"]["unknown"] == 0
    assert result["execution"]["text"] == "已确认，3 笔已发送、1 笔未发送"
    assert gateway.submit_count == 3
    app.state.store.close()


async def test_close_batch_keeps_an_unknown_send_unknown_and_never_resends_it(tmp_path: Path) -> None:
    gateway = Gateway()
    gateway.submit_error = True  # every send loses its response
    app = application(tmp_path / "close-batch-unknown.sqlite3", gateway, NormalProvider())
    session = app.state.broker.create_session("local", "native", USER)
    card = app.state.proposals.create_close_batch(session.session_id, "turn-batch", reason="全部平仓")
    result = await confirm(app, card, "confirm-unknown")
    assert [item["displayStatus"] for item in result["simulationOrders"]] == ["结果未知"] * 4
    assert result["execution"] == {"total": 4, "sent": 0, "notSent": 0, "unknown": 4, "filled": 0,
                                   "text": "已确认，4 笔结果未知"}
    assert gateway.submit_count == 4
    # The same confirmation again reads the facts back and sends nothing.
    again = await confirm(app, card, "confirm-unknown")
    assert gateway.submit_count == 4 and again["execution"]["unknown"] == 4
    # Nor does an unknown order accept a fresh dispatch of its own.
    with pytest.raises(SimulationOrderError):
        await app.state.simulation_orders.dispatch(_child_ids(app, card)[0], user_id=USER)
    assert gateway.submit_count == 4
    app.state.store.close()


async def test_close_batch_republishes_its_card_with_the_child_order_ids(tmp_path: Path) -> None:
    gateway = Gateway()
    app = application(tmp_path / "close-batch-card.sqlite3", gateway, NormalProvider())
    session = app.state.broker.create_session("local", "native", USER)
    card = app.state.proposals.create_close_batch(session.session_id, "turn-batch", reason="全部平仓")
    assert not any(order.get("clientOrderId") for order in card["orders"])
    await confirm(app, card, "confirm-card")
    published = [event for _, event in app.state.store.list_events(session.session_id)
                 if event["name"] == "proposal.close_batch"]
    # The card is published again once its children exist, so the conversation can show
    # each order's real state — including after a restart replays these events.
    assert len(published) == 1
    assert published[-1]["payload"]["status"] == "candidate_recorded"
    assert all(order.get("clientOrderId") for order in published[-1]["payload"]["orders"])
    app.state.store.close()


async def test_close_batch_failure_leaves_nothing_behind_when_a_finished_link_awaits_backfill(
    tmp_path: Path,
) -> None:
    """A confirmation that fails partway must roll back every child order — even when
    the change notification would synthesize a backfill fill, which historically
    committed the caller's transaction halfway through the batch."""
    gateway = Gateway()
    app = application(tmp_path / "close-batch-atomic.sqlite3", gateway, NormalProvider())
    orders = app.state.simulation_orders
    store = app.state.store
    session = app.state.broker.create_session("local", "native", USER)
    # A finished, confirmed link with no fill row: the next change notification
    # synthesizes its fill, which is what used to commit the confirm transaction.
    seed_proposal = proposal(app, turn="turn-seed")
    now = datetime.now(UTC).isoformat()
    seed_request = {
        "commandId": "submit:AITseed0000001", "clientOrderId": "AITseed0000001",
        "symbol": "SHFE.au2612", "direction": "BUY", "offset": "OPEN",
        "volume": 1, "priceType": "LIMIT", "limitPrice": 824.0,
    }
    with store.lock, store.connection:
        store.connection.execute(
            """INSERT INTO tqkq_sim_order_links
            (id, user_id, proposal_id, client_order_id, request_json, submit_phase,
             cancel_phase, last_remote_status, last_report_json, last_synced_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'CONFIRMED', 'NONE', 'FINISHED', ?, ?, ?, ?)""",
            (str(uuid4()), USER, seed_proposal["proposalId"], "AITseed0000001",
             json.dumps(seed_request),
             json.dumps({"order": {"status": "FINISHED", "volume": 1, "volumeLeft": 0}}),
             now, now, now),
        )
    card = app.state.proposals.create_close_batch(session.session_id, "turn-batch", reason="全部平仓")
    assert card["totalOrders"] == 4
    real_prepare = orders.prepare
    prepared: list[str] = []

    def fail_second(*, proposal_id: str, user_id: str, payload: dict[str, Any], connection):
        prepared.append(proposal_id)
        if len(prepared) == 2:
            raise SimulationOrderError("close_offset_mismatch")
        return real_prepare(proposal_id=proposal_id, user_id=user_id, payload=payload, connection=connection)

    orders.prepare = fail_second  # type: ignore[method-assign]
    with pytest.raises(SimulationOrderError):
        await confirm(app, card, "confirm-atomicity")
    orders.prepare = real_prepare  # type: ignore[method-assign]
    # Nothing is half-sent: no child order link, no child proposal, no backfilled
    # fill and the card is still waiting for the user's confirmation.
    links = store.connection.execute("SELECT client_order_id FROM tqkq_sim_order_links").fetchall()
    assert [row[0] for row in links] == ["AITseed0000001"]
    children = store.connection.execute(
        "SELECT COUNT(*) FROM proposals WHERE json_extract(payload_json, '$.parentProposalId') = ?",
        (card["proposalId"],),
    ).fetchone()[0]
    assert children == 0
    assert store.get(card["proposalId"])["status"] == "approval_required"
    assert store.connection.execute(
        "SELECT COUNT(*) FROM account_trades WHERE trade_id = 'link:AITseed0000001'"
    ).fetchone()[0] == 0
    # The same card confirms cleanly afterwards; the notification now fires only
    # after the commit, so the seeded link finally gets its synthesized fill.
    in_transaction_when_notified: list[bool] = []
    unowned_when_notified: list[bool] = []
    real_on_change = orders.on_change

    def on_change_outside_transaction():
        in_transaction_when_notified.append(store.connection.in_transaction)
        unowned_when_notified.append(store.connection.owner_thread is None)
        assert real_on_change is not None
        real_on_change()

    orders.on_change = on_change_outside_transaction
    result = await confirm(app, card, "confirm-atomicity-retry")
    orders.on_change = real_on_change
    assert gateway.submit_count == 4 and result["status"] == "candidate_recorded"
    assert store.connection.execute("SELECT COUNT(*) FROM tqkq_sim_order_links").fetchone()[0] == 5
    assert store.connection.execute(
        "SELECT COUNT(*) FROM account_trades WHERE trade_id = 'link:AITseed0000001'"
    ).fetchone()[0] == 1
    # Every notification fired after the commit: no open transaction, no owner.
    assert in_transaction_when_notified and not any(in_transaction_when_notified)
    assert unowned_when_notified and all(unowned_when_notified)
    app.state.store.close()


# --- one-round candidate: the readiness gate inside create_trade_proposal (2026-09-19) ---------------


def _tool_text(result: dict) -> dict:
    return json.loads(result["contentItems"][0]["text"])


def test_opening_candidate_carries_the_readiness_gate_result_on_the_card(tmp_path: Path) -> None:
    app = application(tmp_path / "runtime-gate-ok.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(app.state.data_provider, _tool_status(app), proposals=app.state.proposals)
    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "priceIntent": "aggressive", "reason": "开多"}),
        turn_id="turn-gate-ok", mode="local", session_id=session.session_id,
    )
    payload = _tool_text(result)
    assert result["success"] is True and event is not None and event[0] == "proposal.trade"
    assert payload["data"]["readiness"] == event[1]["readiness"]
    assert event[1]["readiness"]["passed"] is True and event[1]["readiness"]["issues"] == []
    assert "行情、K线、合约、账户、持仓与委托数据均为最新" in event[1]["readiness"]["summary"]
    assert "涨跌停范围内" in event[1]["readiness"]["summary"]
    # The card's 详情 shows the gate through its 风控 line; the risk verdict itself is unchanged.
    assert event[1]["riskCheck"]["reason"].startswith("就绪检查已通过")
    assert event[1]["riskCheck"]["reason"].endswith("模拟盘未设置账户交易限制。")
    assert payload["data"]["riskCheck"] == {"passed": True, "reason": event[1]["riskCheck"]["reason"]}
    app.state.store.close()


def test_opening_candidate_is_refused_with_chinese_labels_when_the_gate_is_not_ready(tmp_path: Path) -> None:
    """Same outcome as get_futures_decision_context reporting proposalReady=false: no card, the labels
    of the missing items, nothing stored."""
    app = application(tmp_path / "runtime-gate-stale.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    status = _tool_status(app)
    tools = TraderReadOnlyTools(app.state.data_provider, lambda mode: {**status(mode), "accountStatus": "stale"},
                                proposals=app.state.proposals)
    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "priceIntent": "aggressive", "reason": "开多"}),
        turn_id="turn-gate-stale", mode="local", session_id=session.session_id,
    )
    payload = _tool_text(result)
    assert result["success"] is False and event is None
    assert payload["errorCode"] == "proposal_gate_not_ready"
    assert payload["warning"] == "关键事实或风控门禁未就绪，只能解释缺失项，不能给出交易候选。"
    assert payload["data"]["proposalReadinessReasons"] == ["account_not_fresh"]
    assert payload["data"]["userFacing"]["candidateStatus"] == "暂时不能生成交易方案"
    assert payload["data"]["userFacing"]["candidateIssues"] == ["账户数据不是最新状态"]
    context, _ = tools.call(tool="get_futures_decision_context", arguments={"symbol": "SHFE.au2612"},
                            turn_id="turn-gate-stale-ctx", mode="local")
    assert _tool_text(context)["data"]["userFacing"]["candidateIssues"] == payload["data"]["userFacing"]["candidateIssues"]
    assert app.state.store.recent_proposals(session.session_id, limit=5) == []
    app.state.store.close()


def test_opening_candidate_with_market_unavailable_uses_the_context_tool_error_code(tmp_path: Path) -> None:
    app = application(tmp_path / "runtime-gate-market.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    status = _tool_status(app)
    tools = TraderReadOnlyTools(app.state.data_provider, lambda mode: {**status(mode), "marketDataStatus": "unavailable"},
                                proposals=app.state.proposals)
    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "sell_open", "volume": 1, "priceIntent": "passive", "reason": "开空"}),
        turn_id="turn-gate-market", mode="local", session_id=session.session_id,
    )
    assert result["success"] is False and event is None
    assert _tool_text(result)["errorCode"] == "marketDataStatus_unavailable"
    app.state.store.close()


def test_price_outside_the_limits_is_refused_in_the_users_words(tmp_path: Path, monkeypatch) -> None:
    """The candidate builder's price-band check (a quote pinned at the limit) after the gate passed:
    no card, told as the label the card's 详情 would otherwise show."""
    app = application(tmp_path / "runtime-gate-limits.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(app.state.data_provider, _tool_status(app), proposals=app.state.proposals)

    def outside(price, tick, market):
        raise SimulationOrderError("limit_price_outside_limits")

    monkeypatch.setattr(app.state.simulation_orders, "_validate_price", outside)
    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "priceIntent": "aggressive", "reason": "开多"}),
        turn_id="turn-gate-limits", mode="local", session_id=session.session_id,
    )
    payload = _tool_text(result)
    assert result["success"] is False and event is None
    assert payload["errorCode"] == "limit_price_outside_limits"
    assert payload["data"]["userFacing"]["candidateIssues"] == ["委托价超出当前涨跌停范围"]
    app.state.store.close()


def test_closing_candidate_keeps_its_own_position_checks_instead_of_the_bar_gate(tmp_path: Path) -> None:
    """A close is not blocked by the opening gate's bar/catalog freshness; its readiness line names the
    close checks, and a missing position is told in the user's words."""
    app = application(tmp_path / "runtime-gate-close.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    status = _tool_status(app)
    tools = TraderReadOnlyTools(app.state.data_provider, lambda mode: {**status(mode), "riskStatus": "unknown"},
                                proposals=app.state.proposals)
    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "sell_close", "positionBucket": "today", "volume": 1,
                   "priceIntent": "aggressive", "reason": "平今"}),
        turn_id="turn-gate-close", mode="local", session_id=session.session_id,
    )
    assert result["success"] is True and event is not None
    assert event[1]["readiness"]["summary"].startswith("就绪检查已通过：账户与持仓为最新")
    missing, none_event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "sell_close", "positionBucket": "today", "volume": 5,
                   "priceIntent": "aggressive", "reason": "平今五手"}),
        turn_id="turn-gate-close-missing", mode="local", session_id=session.session_id,
    )
    assert missing["success"] is False and none_event is None
    assert _tool_text(missing)["errorCode"] == "close_volume_unavailable"
    assert _tool_text(missing)["data"]["userFacing"]["candidateIssues"] == ["可平手数不足（含冻结手数）"]
    app.state.store.close()


def test_a_close_without_a_bucket_is_decided_from_the_position_it_acts_on(tmp_path: Path, monkeypatch) -> None:
    """09-20 第二轮：上期所/能源中心的平仓没说平今还是平昨时，Runtime 按持仓替用户判定——只有今仓→
    平今、只有昨仓→平昨、够昨仓→平昨、不够昨仓但够今仓→平今、跨今昨→一张 close_batch 卡（两腿
    合计等于要平的手数，冻结先扣）、超过总持仓→中文拒绝不建卡；明确说了今昨则不替用户改判。"""
    app = application(tmp_path / "runtime-close-bucket.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(app.state.data_provider, _tool_status(app), proposals=app.state.proposals)

    def hold(positions):
        monkeypatch.setattr(app.state.data_provider, "positions", lambda: positions)

    def close(symbol, volume, turn):
        return tools.call(
            tool="propose_order",
            arguments=legacy_card_args("create_trade_proposal", {"symbol": symbol, "side": "sell_close", "volume": volume,
                       "priceIntent": "aggressive", "reason": "平仓"}),
            turn_id=turn, mode="local", session_id=session.session_id,
        )

    def au(today, historical, frozen_today=0, frozen_historical=0):
        return [{"symbol": "SHFE.au2612", "exchangeId": "SHFE", "instrumentId": "au2612", "direction": "long",
                 "volume": today + historical, "unrealizedPnl": 2670, "todayVolume": today,
                 "historicalVolume": historical, "frozenToday": frozen_today, "frozenHistorical": frozen_historical}]

    hold(au(today=2, historical=0))
    result, event = close("SHFE.au2612", 2, "turn-bucket-only-today")
    assert result["success"] is True and event[0] == "proposal.trade" and event[1]["positionBucket"] == "today"
    hold(au(today=0, historical=2))
    result, event = close("SHFE.au2612", 2, "turn-bucket-only-historical")
    assert result["success"] is True and event[1]["positionBucket"] == "historical"
    hold(au(today=2, historical=1))
    result, event = close("SHFE.au2612", 1, "turn-bucket-fits-historical")
    assert result["success"] is True and event[1]["positionBucket"] == "historical"
    result, event = close("SHFE.au2612", 2, "turn-bucket-needs-today")
    assert result["success"] is True and event[1]["positionBucket"] == "today"
    hold(au(today=2, historical=1, frozen_today=1))
    result, event = close("SHFE.au2612", 2, "turn-bucket-spans-both")
    assert result["success"] is True and event[0] == "proposal.close_batch"
    assert {(order["positionBucket"], order["volume"]) for order in event[1]["orders"]} == {("today", 1), ("historical", 1)}
    assert event[1]["totalVolume"] == 2
    hold(au(today=2, historical=1))
    result, event = close("SHFE.au2612", 4, "turn-bucket-exceeds")
    assert result["success"] is False and event is None
    payload = _tool_text(result)
    assert payload["errorCode"] == "close_volume_exceeds_position"
    assert payload["data"]["userFacing"]["candidateIssues"] == [
        "当前没有足够的沪金2612（SHFE.au2612）持仓可平 4 手（今仓可平 2 手、昨仓可平 1 手），平不了"]
    # 用户明确说了今昨：不替用户改判（自动判定会选平昨，这里保持 today）。
    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "sell_close", "positionBucket": "today", "volume": 1,
                   "priceIntent": "aggressive", "reason": "平今"}),
        turn_id="turn-bucket-explicit", mode="local", session_id=session.session_id,
    )
    assert result["success"] is True and event[1]["positionBucket"] == "today"
    app.state.store.close()


def test_a_close_without_a_bucket_follows_the_saved_preference_like_the_estimate(
    tmp_path: Path, monkeypatch,
) -> None:
    """#480：估算和 AI 平仓候选共用一份今昨分配，都按“设置 → 交易 → 平今平昨”取量。"""
    app = application(tmp_path / "runtime-close-preference.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(app.state.data_provider, _tool_status(app), proposals=app.state.proposals)
    app.state.proposals.fee_settings.save(USER, {
        "source": "custom",
        "default": {"mode": "per_lot", "open": 0.0, "closeYesterday": 1.0, "closeToday": 100.0},
    })

    def hold(today, historical):
        monkeypatch.setattr(app.state.data_provider, "positions", lambda: [{
            "symbol": "SHFE.au2612", "exchangeId": "SHFE", "instrumentId": "au2612", "direction": "long",
            "volume": today + historical, "unrealizedPnl": 0, "todayVolume": today,
            "historicalVolume": historical, "frozenToday": 0, "frozenHistorical": 0, "margin": 80_000.0}])

    def close(volume, turn, **extra):
        return tools.call(
            tool="propose_order",
            arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "sell_close", "volume": volume,
                       "priceIntent": "aggressive", "reason": "平仓", **extra}),
            turn_id=turn, mode="local", session_id=session.session_id, user_id=USER,
        )

    hold(today=1, historical=1)
    app.state.store.update_preferences(USER, {"trade": {"closePreference": "today"}})
    estimate = app.state.proposals.order_estimate(
        user_id=USER, symbol="SHFE.au2612", side="sell", offset="close", volume=1,
        provider=app.state.data_provider)
    assert estimate["feeBreakdown"]["today"]["volume"] == 1
    result, event = close(1, "turn-pref-today")
    assert result["success"] is True and event[1]["positionBucket"] == "today"
    # 显式桶不被偏好改写。
    result, event = close(1, "turn-pref-explicit", positionBucket="historical")
    assert result["success"] is True and event[1]["positionBucket"] == "historical"
    # 平昨优先、跨两侧：昨仓先取满，余量平今——批量卡与估算同一份分配。
    hold(today=3, historical=2)
    app.state.store.update_preferences(USER, {"trade": {"closePreference": "historical"}})
    estimate = app.state.proposals.order_estimate(
        user_id=USER, symbol="SHFE.au2612", side="sell", offset="close", volume=4,
        provider=app.state.data_provider)
    result, event = close(4, "turn-pref-historical-span")
    assert result["success"] is True and event[0] == "proposal.close_batch"
    legs = {order["positionBucket"]: order["volume"] for order in event[1]["orders"]}
    assert legs == {"historical": 2, "today": 2}
    assert {name: part["volume"] for name, part in estimate["feeBreakdown"].items()} == legs
    app.state.store.close()


def test_a_failed_risk_check_keeps_its_own_reason_without_the_readiness_prefix(tmp_path: Path, monkeypatch) -> None:
    """Review of #229: "就绪检查已通过；<失败原因>" on a risk-blocked card reads as a contradiction."""
    app = application(tmp_path / "runtime-gate-risk-blocked.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(app.state.data_provider, _tool_status(app), proposals=app.state.proposals)
    original = app.state.simulation_orders.risk_check

    def blocked(payload, user_id):
        return {**original(payload, user_id), "passed": False, "reason": "预计最大亏损超过单笔风险上限 ¥500。"}

    monkeypatch.setattr(app.state.simulation_orders, "risk_check", blocked)
    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "priceIntent": "aggressive", "reason": "开多"}),
        turn_id="turn-gate-risk-blocked", mode="local", session_id=session.session_id,
    )
    assert result["success"] is True and event is not None
    assert event[1]["status"] == "risk_blocked"
    assert event[1]["riskCheck"] == {**event[1]["riskCheck"], "passed": False, "reason": "预计最大亏损超过单笔风险上限 ¥500。"}
    assert event[1]["readiness"]["passed"] is True  # the gate itself passed; the risk rule did not
    app.state.store.close()


# --- AI 自主执行 switch (外审 09-19 A5) --------------------------------------------------------------


async def test_ai_autonomy_switch_off_refuses_execution_and_builds_no_lease(tmp_path: Path) -> None:
    """设置 → 交易 → AI 自主执行 off: the Runtime refuses the AI's own execution tool and the lease it
    would silently create. Proposal cards — what the user confirms — are untouched, and turning the
    switch back on restores the old behavior."""
    gateway = Gateway()
    app = application(tmp_path / "ai-autonomy.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(app.state.data_provider, _tool_status(app),
                                proposals=app.state.proposals, autonomy=app.state.autonomy)
    tools.app_capabilities = app.state.app_capabilities
    app.state.store.update_preferences(USER, {"trade": {"aiAutonomy": False}})

    arguments = {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1,
                 "priceIntent": "passive", "reason": "AI 自主执行已关闭时不应成交"}
    refused, _ = await tools.call_async(tool="execute_simulation_trade", arguments=arguments,
                                        turn_id="turn-autonomy-off", mode="local",
                                        session_id=session.session_id, user_id=USER)
    payload = _tool_text(refused)
    assert refused["success"] is False and payload["errorCode"] == "ai_autonomy_disabled"
    assert "只能生成待确认方案" in payload["data"]["userFacing"]
    assert gateway.submit_count == 0
    assert app.state.autonomy.current(user_id=USER) is None
    # The AI's stop tool leases the gateway the same way, and is refused the same way.
    stop, _ = await tools.call_async(
        tool="set_simulation_stop_loss",
        arguments={"symbol": "SHFE.au2612", "positionDirection": "long", "positionBucket": "today",
                   "volume": 1, "triggerPrice": 800},
        turn_id="turn-autonomy-off-stop", mode="local", session_id=session.session_id, user_id=USER)
    assert stop["success"] is False and _tool_text(stop)["errorCode"] == "ai_autonomy_disabled"
    assert app.state.autonomy.current(user_id=USER) is None

    card, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "limitPrice": 820, "reason": "出卡不受影响"}),
        turn_id="turn-autonomy-off-card", mode="local", session_id=session.session_id)
    assert card["success"] is True and event is not None and event[1]["status"] == "approval_required"

    app.state.store.update_preferences(USER, {"trade": {"aiAutonomy": True}})
    executed, _ = await tools.call_async(tool="execute_simulation_trade",
                                         arguments={**arguments, "reason": "打开后恢复原行为"},
                                         turn_id="turn-autonomy-on", mode="local",
                                         session_id=session.session_id, user_id=USER)
    assert executed["success"] is True and gateway.submit_count == 1
    assert app.state.autonomy.current(user_id=USER)["status"] == "ACTIVE"
    app.state.store.close()


async def test_an_execution_error_with_its_own_chinese_sentence_reaches_the_model(
        tmp_path: Path, monkeypatch) -> None:
    """#528：错误自带中文原句（AutonomyError.message——类上注明“供用户读的说明面”）时，
    工具结果把原句带给模型，不只剩一个代码加通用套话。"""
    app = application(tmp_path / "tool-message.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(app.state.data_provider, _tool_status(app),
                                proposals=app.state.proposals, autonomy=app.state.autonomy)
    tools.app_capabilities = app.state.app_capabilities

    async def refused(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AutonomyError("strategy_signal_not_auto", "执行方式已改为出卡确认，这单没有发出。")

    monkeypatch.setattr(app.state.autonomy, "execute_trade", refused)
    result, _ = await tools.call_async(
        tool="execute_simulation_trade",
        arguments={"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1,
                   "priceIntent": "passive", "reason": "改口径"},
        turn_id="turn-tool-message", mode="local", session_id=session.session_id, user_id=USER)
    payload = _tool_text(result)
    assert result["success"] is False and payload["errorCode"] == "strategy_signal_not_auto"
    assert "执行方式已改为出卡确认" in str(payload.get("warning"))
    app.state.store.close()


async def test_turning_the_switch_off_ends_the_running_lease_through_the_preferences_route(tmp_path: Path) -> None:
    """Every writer (client, preference command, update_preferences tool) goes through PUT
    /api/preferences, so an ACTIVE lease is stopped there instead of staying reported as running."""
    gateway = Gateway()
    app = application(tmp_path / "ai-autonomy-lease.sqlite3", gateway)
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(app.state.data_provider, _tool_status(app),
                                proposals=app.state.proposals, autonomy=app.state.autonomy)
    tools.app_capabilities = app.state.app_capabilities
    executed, _ = await tools.call_async(
        tool="execute_simulation_trade",
        arguments={"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "priceIntent": "passive", "reason": "建租约"},
        turn_id="turn-lease", mode="local", session_id=session.session_id, user_id=USER)
    assert executed["success"] is True
    assert app.state.autonomy.current(user_id=USER)["status"] == "ACTIVE"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        document = await client.put("/api/preferences", json={"trade": {"aiAutonomy": False}})
        assert document.status_code == 200, document.text
        assert document.json()["preferences"]["trade"]["aiAutonomy"] is False
    assert app.state.autonomy.current(user_id=USER)["status"] == "STOPPED"
    app.state.store.close()


# --- the user's own price (外审 09-19 A3) ------------------------------------------------------------


def test_the_users_own_price_is_the_cards_price_instead_of_the_book(tmp_path: Path) -> None:
    """"820挂一手多" used to become the book's 824: the tool had no limitPrice, so Runtime priced the
    card off the quote. With limitPrice the card, the tool's answer and the user's sentence agree."""
    app = application(tmp_path / "runtime-limit-price.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(app.state.data_provider, _tool_status(app), proposals=app.state.proposals)
    result, event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "limitPrice": 820, "reason": "820挂一手多"}),
        turn_id="turn-limit-price", mode="local", session_id=session.session_id,
    )
    assert result["success"] is True and event is not None
    assert event[1]["limitPrice"] == 820.0
    data = _tool_text(result)["data"]
    assert (data["limitPrice"], data["side"], data["volume"], data["symbol"]) == (820.0, "buy_open", 1, "SHFE.au2612")
    # Without a price the book still decides: an aggressive buy takes the ask.
    book, book_event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "priceIntent": "aggressive", "reason": "来一手多"}),
        turn_id="turn-book-price", mode="local", session_id=session.session_id,
    )
    assert book["success"] is True and book_event is not None and book_event[1]["limitPrice"] == 824.0
    app.state.store.close()


def test_a_card_without_any_price_takes_the_runtime_default_instead_of_refusing(tmp_path: Path) -> None:
    """第三轮（09-21）：模型没说价格不再拒绝出卡。平仓默认对手价；开仓用账号的默认价格方式
    （trade.defaultPriceIntent，读不到账号时挂单）。用户说了价格（limitPrice）仍原样上卡，两个都
    传时以 limitPrice 为准。"""
    app = application(tmp_path / "runtime-default-price.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(app.state.data_provider, _tool_status(app), proposals=app.state.proposals)
    # 平仓不传价：对手价默认（sell_close aggressive 打买一价；fixture 的 au2612 多头今 1 昨 1，
    # 平 1 手自动判平昨）。
    close, close_event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "sell_close", "volume": 1, "reason": "平一半"}),
        turn_id="turn-default-close", mode="local", session_id=session.session_id,
    )
    assert close["success"] is True and close_event is not None
    assert close_event[1]["priceIntent"] == "aggressive" and close_event[1]["limitPrice"] == 823.0
    assert close_event[1]["positionBucket"] == "historical"
    # 开仓不传价：账号默认价格方式（账号默认 aggressive → 卖一价）；读不到账号（没有 user_id）时挂单。
    opened, opened_event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "reason": "来一手"}),
        turn_id="turn-default-open", mode="local", session_id=session.session_id, user_id=USER,
    )
    assert opened["success"] is True and opened_event is not None
    assert opened_event[1]["priceIntent"] == "aggressive" and opened_event[1]["limitPrice"] == 824.0
    anonymous, anonymous_event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "reason": "来一手"}),
        turn_id="turn-default-open-anonymous", mode="local", session_id=session.session_id,
    )
    assert anonymous_event is not None and anonymous_event[1]["priceIntent"] == "passive"
    # 偏好改成挂单后，开仓默认跟着改（preference_instruction 之外，Runtime 自己也执行同一默认）。
    app.state.store.update_preferences(USER, {"trade": {"defaultPriceIntent": "passive"}})
    passive, passive_event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "reason": "来一手"}),
        turn_id="turn-default-open-passive", mode="local", session_id=session.session_id, user_id=USER,
    )
    assert passive_event is not None and passive_event[1]["priceIntent"] == "passive"
    assert passive_event[1]["limitPrice"] == 823.0
    # 两个都传仍以 limitPrice 为准（原行为保留）。
    both, both_event = tools.call(
        tool="propose_order",
        arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "limitPrice": 820,
                   "priceIntent": "aggressive", "reason": "两个都给"}),
        turn_id="turn-both-prices", mode="local", session_id=session.session_id,
    )
    assert both["success"] is True and both_event is not None and both_event[1]["limitPrice"] == 820.0
    app.state.store.close()


def test_a_close_for_a_position_the_account_does_not_hold_is_said_before_the_bucket_ask(
    tmp_path: Path,
) -> None:
    """第三轮（09-21）：平仓先说有没有持仓。SHFE.rb2610 不在持仓里时，诚实拒绝必须先于“必须指明
    平今平昨”（缺省今昨时 builder 的 close_bucket_required 原本抢先）；其他交易所同样先查持仓。"""
    app = application(tmp_path / "runtime-close-missing.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(app.state.data_provider, _tool_status(app), proposals=app.state.proposals)
    for symbol in ("SHFE.rb2610", "DCE.i2609"):
        result, event = tools.call(
            tool="propose_order",
            arguments=legacy_card_args("create_trade_proposal", {"symbol": symbol, "side": "sell_close", "volume": 1, "priceIntent": "aggressive",
                       "reason": "平掉没有的持仓"}),
            turn_id=f"turn-close-missing-{symbol}", mode="local", session_id=session.session_id,
        )
        payload = _tool_text(result)
        assert result["success"] is False and event is None
        assert payload["errorCode"] == "close_position_not_found"
        display = {"SHFE.rb2610": "螺纹钢2610（SHFE.rb2610）", "DCE.i2609": "铁矿石2609（DCE.i2609）"}[symbol]
        assert payload["data"]["userFacing"]["candidateIssues"] == [f"当前没有{display}的持仓，平不了"]
    assert app.state.store.recent_proposals(session.session_id, limit=5) == []
    app.state.store.close()


def test_an_unusable_price_is_refused_in_chinese_instead_of_being_moved(tmp_path: Path) -> None:
    """Outside the band or off the tick: no card and a Chinese reason. The price is never adjusted."""
    app = application(tmp_path / "runtime-limit-price-bad.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    tools = TraderReadOnlyTools(app.state.data_provider, _tool_status(app), proposals=app.state.proposals)
    for price, issue, code in ((1200, "委托价超出当前涨跌停范围", "limit_price_outside_limits"),
                               (820.3, "委托价不是最小变动价位的整数倍", "limit_price_not_on_tick")):
        result, event = tools.call(
            tool="propose_order",
            arguments=legacy_card_args("create_trade_proposal", {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1, "limitPrice": price,
                       "reason": "指定价开多"}),
            turn_id=f"turn-bad-price-{price}", mode="local", session_id=session.session_id,
        )
        payload = _tool_text(result)
        assert result["success"] is False and event is None
        assert payload["errorCode"] == code
        assert payload["data"]["userFacing"]["candidateIssues"] == [issue]
    assert app.state.store.recent_proposals(session.session_id, limit=5) == []
    app.state.store.close()


async def test_a_submitted_order_logs_where_its_time_went(tmp_path: Path, caplog) -> None:
    """下单全程打点 (2026-09-27): one log line per order splits checks / gateway / the sidecar's share."""
    import logging

    class TimedGateway(Gateway):
        async def submit_order_once(self, request):
            report = await super().submit_order_once(request)
            return {**report, "timing": {"queuedMs": 3, "exchangeMs": 140}}

    gateway = TimedGateway()
    provider = NormalProvider()
    app = application(tmp_path / "runtime.sqlite3", gateway, provider)
    item = proposal(app)
    provider.quote_datetime = quote_now()
    # uvicorn's own logging config stops propagation at "uvicorn" once another test applied it.
    logger = logging.getLogger("uvicorn.error.orders")
    logger.addHandler(caplog.handler)
    level = logger.level
    logger.setLevel(logging.INFO)
    try:
        result = await confirm(app, item)
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(level)
    assert result["simulationOrder"]["displayStatus"] == "已受理"
    line = next(record.getMessage() for record in caplog.records if record.getMessage().startswith("order_timing"))
    assert f"client_order_id={result['clientOrderId']}" in line
    assert "sidecar_queued_ms=3 sidecar_exchange_ms=140 status=ALIVE" in line
    assert "checks_ms=" in line and "gateway_ms=" in line
    app.state.store.close()


# -- 执行链共用判断 (2026-10-04 #518): “还在挂”只看阶段，不看中文投影 ------------------------------


def test_order_is_working_reads_the_order_phases_not_the_display_words() -> None:
    """共用判断 #518: CONFIRMED + ALIVE 且撤单没有已确认/未知 —— 中文“部分成交”既可能是
    还挂着也可能是已结束，只有阶段能回答“这单还在不在场上”。"""
    from trader_runtime.simulation_orders import order_is_working

    def order(**fields: Any) -> dict[str, Any]:
        return {"submitPhase": "CONFIRMED", "remoteStatus": "ALIVE", "cancelPhase": "NONE", **fields}

    assert order_is_working(order()) is True
    assert order_is_working(order(remoteStatus="FINISHED")) is False   # 结束（含部分成交后结束）
    assert order_is_working(order(cancelPhase="CONFIRMED")) is False   # 撤单已确认
    assert order_is_working(order(cancelPhase="UNKNOWN")) is False     # 结果未知：只查询原单
    assert order_is_working(order(submitPhase="PREPARED")) is False    # 还没发出去


async def test_an_ended_partial_close_is_not_cancelable_and_reads_no_unknown(tmp_path: Path) -> None:
    """部分成交后已结束的单不是“在挂”：撤单直接回不可撤，不向网关发起撤、也不落“结果未知”。"""
    gateway = Gateway()
    provider = NormalProvider()
    provider.position_items = [  # 今仓 2 手：平 2 手，成交 1 手后单子结束
        {"symbol": "SHFE.au2612", "direction": "long", "volume": 2, "todayVolume": 2,
         "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    app = application(tmp_path / "ended-partial.sqlite3", gateway, provider)
    item = close_proposal(app, volume=2)
    provider.quote_datetime = quote_now()
    order_id = (await confirm(app, item, "ended-partial-confirm"))["clientOrderId"]
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=1)
    order = await app.state.simulation_orders.query(order_id, user_id=USER)
    assert order["displayStatus"] == "部分成交后已结束"  # #932：投影说清已结束，不再与“还挂着”同字
    with pytest.raises(SimulationOrderError) as raised:
        await app.state.simulation_orders.cancel(order_id, user_id=USER)
    assert str(raised.value) == "order_not_cancelable"
    assert gateway.cancel_count == 0  # 没向网关发起撤单
    after = await app.state.simulation_orders.query(order_id, user_id=USER)
    assert after["cancelPhase"] == "NONE"  # 也没有“结果未知”
    app.state.store.close()


async def test_a_failed_single_trade_dispatch_keeps_the_reason_on_the_prepared_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单笔确认的派发抛错 (#518-D)：先 note_dispatch_error 再抛，这单留 PREPARED + 原因，
    不是空白的“未发送”；由用户自己的继续/放弃接手，绝不被重发。"""
    gateway = Gateway()
    provider = NormalProvider()
    provider.position_items = [  # 今仓 1 手：出卡与 prepare 都平得起
        {"symbol": "SHFE.au2612", "direction": "long", "volume": 1, "todayVolume": 1,
         "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    app = application(tmp_path / "dispatch-error.sqlite3", gateway, provider)
    item = close_proposal(app, volume=1)
    provider.quote_datetime = quote_now()

    async def refusing_dispatch(client_order_id: str, *, user_id: str) -> dict[str, Any]:
        # 派发期的确定性拒绝（网关之前，如发送时持仓复核不过）：单留 PREPARED
        raise SimulationOrderError("close_volume_exceeds_position")

    monkeypatch.setattr(app.state.proposals.simulation_orders, "dispatch", refusing_dispatch)
    with pytest.raises(SimulationOrderError):
        await confirm(app, item, "dispatch-error-confirm")
    assert gateway.submit_count == 0  # 根本没到网关
    row = app.state.store.connection.execute(
        "SELECT submit_phase, last_read_error FROM tqkq_sim_order_links WHERE user_id = ?",
        (USER,)).fetchone()
    assert row["submit_phase"] == "PREPARED"
    assert row["last_read_error"] == "close_volume_exceeds_position"  # 原因留在这单上
    app.state.store.close()


def test_a_timed_ai_lease_that_ran_out_does_not_block_the_users_own_stop(tmp_path: Path) -> None:
    """#1206: a 5-minute AI lease long expired kept refusing the user's own stop (ai_execution_expired);
    the user's action now starts the default unlimited lease, the AI's own still reads the expiry."""
    provider = NormalProvider()
    provider.canonical_symbol = "SHFE.rb2701"
    app = application(tmp_path / "expired-lease.sqlite3", Gateway(), provider)
    session = app.state.broker.create_session("local", "native", USER)
    autonomy = app.state.autonomy
    autonomy.activate(user_id=USER, session_id=session.session_id, symbols=["SHFE.rb2701"],
                      permissions=["open", "close", "cancel", "stop"], max_order_volume=1, max_orders=1,
                      duration_minutes=5)
    past = (datetime.now(UTC) - timedelta(days=9)).isoformat()
    with autonomy.store.connection:
        autonomy.store.connection.execute(
            "UPDATE ai_execution_authorizations SET status = 'EXPIRED', expires_at = ? WHERE user_id = ?", (past, USER))
    lease = autonomy.ensure_runtime_access(user_id=USER, session_id=session.session_id, symbol="SHFE.rb2701",
                                           permission="stop", volume=1)
    assert lease["status"] == "ACTIVE" and lease["expiresAt"].startswith("9999")
    with autonomy.store.connection:
        autonomy.store.connection.execute(
            "UPDATE ai_execution_authorizations SET status = 'EXPIRED', expires_at = ? WHERE user_id = ?", (past, USER))
    with pytest.raises(AutonomyError):
        autonomy.ensure_runtime_access(user_id=USER, session_id=session.session_id, symbol="SHFE.rb2701",
                                       permission="stop", volume=1, ai_initiated=True)

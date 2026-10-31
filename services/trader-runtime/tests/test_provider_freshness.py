from __future__ import annotations

import asyncio
import json
import queue
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.data_providers import (
    FakeReadOnlyDataProvider,
    OfficialTqKqSession,
    TqKqReadOnlyProvider,
    TqKqSnapshot,
)
from trader_runtime.main import create_app
from trader_runtime.trader_tools import TraderReadOnlyTools

TOKEN = "phase13-review-token"
ORIGIN = "http://127.0.0.1:5173"


def snapshot(
    *, close_profit: float = 900, position_profit: float = 300,
    commission: float = 100, captured_age: float = 0,
) -> TqKqSnapshot:
    return TqKqSnapshot(
        account={"balance": 500_000, "staticBalance": 498_000, "available": 450_000,
                 "floatProfit": 1200, "positionProfit": position_profit,
                 "closeProfit": close_profit, "commission": commission, "margin": 50_000},
        positions=[{"symbol": "SHFE.au2612", "exchangeId": "SHFE",
                    "instrumentId": "au2612", "direction": "long", "volume": 1,
                    "unrealizedPnl": 1200}],
        markets={"SHFE.au2612": {"symbol": "SHFE.au2612", "name": "沪金2612",
                                  "price": 824.2, "changePercent": 0.72,
                                  "quoteDatetime": "2026-08-26 15:00:00.000000"}},
        captured_at_monotonic=time.monotonic() - captured_age,
        captured_at_utc=(datetime.now(UTC) - timedelta(seconds=captured_age)).isoformat(),
    )


def official_session(*, wait_result: bool, valid: bool = True) -> OfficialTqKqSession:
    session = object.__new__(OfficialTqKqSession)
    session.api = SimpleNamespace(wait_update=lambda **_: wait_result, close=lambda: None)
    session.account = SimpleNamespace(
        balance=500_000, static_balance=498_000, available=450_000,
        float_profit=1200, position_profit=300, close_profit=900,
        commission=100, margin=50_000,
    )
    session.position_entities = {
        "SHFE.au2612": SimpleNamespace(pos_long=2, pos_short=0, float_profit_long=1200),
        "DCE.i2609": SimpleNamespace(pos_long=0, pos_short=3, float_profit_short=-450),
    }
    quote_price = 824.2 if valid else float("nan")
    session.quotes = {
        "SHFE.au2612": SimpleNamespace(
            last_price=quote_price, pre_close=820, pre_settlement=819,
            datetime="2026-08-26 15:00:00", instrument_name="沪金2612",
            bid_price1=824.0, ask_price1=824.4, price_tick=0.02,
            volume_multiple=1000, upper_limit=900, lower_limit=700,
        )
    }
    return session


def test_official_tqkq_wait_update_timeout_and_incomplete_initial_entity_are_not_fresh() -> None:
    assert official_session(wait_result=False).read(0.01) is None
    assert official_session(wait_result=True, valid=False).read(0.01) is None


def test_official_tqkq_uses_full_dictionary_symbols_and_pos_long_short() -> None:
    result = official_session(wait_result=True).read(0.01)
    assert result is not None
    assert {(position["symbol"], position["exchangeId"], position["instrumentId"],
             position["direction"], position["volume"]) for position in result.positions} == {
        ("SHFE.au2612", "SHFE", "au2612", "long", 2),
        ("DCE.i2609", "DCE", "i2609", "short", 3),
    }
    assert result.captured_at_utc.endswith("+00:00")


@pytest.mark.parametrize(("close_profit", "position_profit", "commission", "expected"), [
    (900, 300, 100, 1100),
    (-600, -200, 50, -850),
    (0, 100, 150, -50),
])
def test_account_today_net_pnl_uses_close_plus_position_minus_commission(
    close_profit: float, position_profit: float, commission: float, expected: float,
) -> None:
    provider = TqKqReadOnlyProvider(username="fixture", password="fixture")
    provider._snapshot = snapshot(close_profit=close_profit, position_profit=position_profit,
                                  commission=commission)
    provider._state = "normal"
    account = provider.account_summary("turn-pnl")
    assert account["todayPnl"] == expected
    assert account["todayNetPnl"] == expected
    assert account["floatProfit"] == 1200
    assert account["positionProfit"] == position_profit
    assert account["closeProfit"] == close_profit
    assert account["commission"] == commission


class ControlledSession:
    def __init__(self) -> None:
        self.values: queue.Queue[TqKqSnapshot | Exception] = queue.Queue()
        self.closed = False
        self.read_timeout_cap = 0.01

    def read(self, timeout_seconds: float) -> TqKqSnapshot | None:
        try:
            value = self.values.get(timeout=min(timeout_seconds, self.read_timeout_cap))
        except queue.Empty:
            return None
        if isinstance(value, Exception):
            raise value
        return value

    def close(self) -> None:
        self.closed = True


async def _wait_for_status(application: Any, session_id: str, expected: str) -> None:
    for _ in range(300):
        statuses = [
            event["payload"]["marketDataStatus"]
            for _, event in application.state.store.list_events(session_id)
            if event["name"] == "runtime.status"
        ]
        if statuses and statuses[-1] == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"did not receive runtime.status={expected}")


async def test_provider_transitions_publish_sse_state_once_and_recover(tmp_path: Path) -> None:
    controlled = ControlledSession()

    def factory(_: str, __: str, ___: Sequence[str]) -> ControlledSession:
        return controlled

    provider = TqKqReadOnlyProvider(
        username="fixture", password="fixture", session_factory=factory,
        stale_after_seconds=0.04,
    )
    application = create_app(
        tmp_path / "status.sqlite3",
        local_token=TOKEN, agent_mode="fake", data_provider=provider,
    )
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test",
            headers={"Origin": ORIGIN},
        ) as client:
            await client.post("/api/auth/local", headers={"Authorization": f"Bearer {TOKEN}"})
            created = await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})
            session_id = created.json()["sessionId"]
            # Provider transitions are broadcast only to sessions with an attached SSE reader.
            # Bootstrap/resume still publish a fresh status for dormant sessions on demand.
            application.state.broker.subscribers[session_id] = 1
            await _wait_for_status(application, session_id, "connecting")
            controlled.values.put(snapshot())
            await _wait_for_status(application, session_id, "normal")
            controlled.values.put(snapshot())
            await asyncio.sleep(0.02)
            await _wait_for_status(application, session_id, "stale")
            # Give the event loop a deterministic window to observe recovery.
            # A 10 ms empty-read timeout can otherwise race a 40 ms stale threshold
            # on a loaded Windows runner and create an unrelated second stale event.
            controlled.read_timeout_cap = 1.0
            controlled.values.put(snapshot())
            await _wait_for_status(application, session_id, "normal")
            controlled.values.put(RuntimeError("fixture_disconnect"))
            await _wait_for_status(application, session_id, "unavailable")
            statuses = [
                event["payload"]["marketDataStatus"]
                for _, event in application.state.store.list_events(session_id)
                if event["name"] == "runtime.status"
            ]
            assert statuses.count("stale") == 1
            assert statuses[-4:] == ["normal", "stale", "normal", "unavailable"]
            application.state.broker.subscribers.pop(session_id, None)


async def test_fake_adapter_never_invents_profit_for_unavailable_tqkq_provider(
    tmp_path: Path,
) -> None:
    provider = TqKqReadOnlyProvider(username=None, password=None)
    application = create_app(
        tmp_path / "fake-tqkq.sqlite3", local_token=TOKEN,
        agent_mode="fake", data_provider=provider,
    )
    session = application.state.broker.create_session("local", "native", "local-user")
    await application.state.adapter.bootstrap(session.session_id)
    await application.state.adapter.handle_message(session.session_id, "查询账户盈亏", "run-account")
    messages = [
        event["payload"]["text"]
        for _, event in application.state.store.list_events(session.session_id)
        if event["name"] == "message.completed" and event["payload"]["role"] == "assistant"
    ]
    assert any("当前不可用" in message for message in messages)
    assert not any("黄金多头贡献" in message or "盈利主要来自" in message for message in messages)
    cards = [
        event["payload"] for _, event in application.state.store.list_events(session.session_id)
        if event["name"] == "card.account"
    ]
    assert cards[-1]["dataStatus"] == "unavailable"
    application.state.store.close()


def test_trader_tools_fail_closed_when_unavailable_and_label_stale_data() -> None:
    provider = TqKqReadOnlyProvider(username="fixture", password="fixture")
    tools = TraderReadOnlyTools(provider, lambda mode: {
        "mode": mode, "adapter": "native-agent", "accountMode": "tqkq-readonly",
        "runtimeConnected": True, "agentConnected": True, "connected": True,
        "marketDataStatus": provider.runtime_status()["marketDataStatus"],
        "accountStatus": provider.runtime_status()["accountStatus"],
        "tradingGatewayStatus": provider.runtime_status()["tradingGatewayStatus"],
        "riskStatus": provider.runtime_status()["riskStatus"],
        "dataErrorCode": provider.runtime_status()["dataErrorCode"], "version": "0.4.0",
    })
    definitions = {definition["name"]: definition for definition in tools.definitions()}
    assert "TqSdk/TqKq 当前期货行情源" in definitions["get_market_snapshot"]["description"]
    assert "TqKq 模拟账户" in definitions["get_account_summary"]["description"]
    unavailable, _ = tools.call(tool="get_account_summary", arguments={}, turn_id="turn", mode="local")
    assert unavailable["success"] is False
    provider._snapshot = snapshot(captured_age=60)
    provider._state = "normal"
    provider._stale_after_seconds = 1
    stale, card = tools.call(tool="get_account_summary", arguments={}, turn_id="turn", mode="local")
    assert stale["success"] is True
    assert "陈旧" in stale["contentItems"][0]["text"]
    assert card is not None and card[1]["dataStatus"] == "stale"


def test_all_trader_read_tools_have_validated_results_and_bounded_catalog_search() -> None:
    provider = FakeReadOnlyDataProvider()

    def status(mode: str) -> dict[str, Any]:
        return {"mode": mode, "adapter": "native-agent", "runtimeConnected": True,
                "agentConnected": True, "connected": True, "version": "0.4.0",
                **provider.runtime_status()}

    tools = TraderReadOnlyTools(provider, status)
    assert {definition["name"] for definition in tools.definitions()} == tools.NAMES
    assert not tools.NAMES.intersection({
        "submit_order", "query_order", "cancel_order", "close_position",
        "insert_order", "start_strategy", "stop_loss",
    })
    arguments = {
        "get_runtime_data_status": {}, "get_account_summary": {}, "get_positions": {},
        "get_account_activity": {},
        "search_instruments": {"search": "沪金", "exchange": "SHFE", "limit": 1},
        "get_settlement_prices": {"symbols": ["SHFE.au2612"], "days": 2},
        "get_main_contract_history": {"symbol": "SHFE.au", "days": 5},
        "get_symbol_ranking": {"symbol": "SHFE.au2612", "rankingType": "VOLUME", "days": 1},
        "get_market_board": {},
        "get_market_snapshot": {"symbol": "SHFE.au2612"},
        "get_market_analysis": {
            "symbol": "SHFE.au2612", "durationSeconds": 300, "lookbackBars": 40,
        },
        "get_futures_decision_context": {
            "symbol": "SHFE.au2612", "durationSeconds": 300, "lookbackBars": 40,
        },
    }
    responses: dict[str, dict[str, Any]] = {}
    for name in sorted(tools.NAMES):
        response, _ = tools.call(
            tool=name, arguments=arguments[name], turn_id="turn-tools", mode="local"
        )
        assert response["success"] is True
        assert '"readOnly":true' in response["contentItems"][0]["text"]
        responses[name] = json.loads(response["contentItems"][0]["text"])
    assert responses["search_instruments"]["data"]["items"] == [{
        "symbol": "SHFE.au2612", "exchangeId": "SHFE", "instrumentId": "au2612",
        "instrumentName": "沪金2612", "productId": "au", "priceTick": 0.02,
        "volumeMultiple": 1000.0, "deliveryYear": 2026, "deliveryMonth": 12,
        "expireRestDays": 90, "hasNight": True, "isMain": True, "openLimit": 0,
    }]
    assert responses["get_account_activity"]["data"]["liveTrading"] == "disabled"
    assert responses["get_market_analysis"]["data"]["method"] == (
        "deterministic_ohlcv_oi_summary_v1"
    )
    assert responses["get_futures_decision_context"]["data"]["proposalReady"] is True
    invalid, _ = tools.call(
        tool="get_market_snapshot", arguments={"symbol": "AU; rm -rf"},
        turn_id="turn-tools", mode="local",
    )
    assert invalid["success"] is False
    invalid_search, _ = tools.call(
        tool="search_instruments", arguments={"exchange": "SHFE;DROP"},
        turn_id="turn-tools", mode="local",
    )
    assert invalid_search["success"] is False


def test_tqkq_credentials_and_reload_are_blocked_until_isolated_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_TRADER_RELOAD_ENABLED", "1")
    with pytest.raises(RuntimeError, match="tqkq_readonly_reload_forbidden"):
        create_app(tmp_path / "reload.sqlite3", data_mode="tqkq-readonly", agent_mode="fake")
    monkeypatch.delenv("AI_TRADER_RELOAD_ENABLED")
    monkeypatch.setenv("AI_TRADER_TQKQ_USERNAME", "must-not-enter-runtime")
    with pytest.raises(RuntimeError, match="tqkq_credentials_require_isolated_sidecar"):
        create_app(tmp_path / "credential.sqlite3", data_mode="tqkq-readonly", agent_mode="fake")

from __future__ import annotations

import ast
import asyncio
import math
import time
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

from trader_runtime.broker import EventBroker
from trader_runtime.data_providers import (
    OfficialTqKqSession,
    SidecarReadOnlyDataProvider,
    TqKqReadOnlyProvider,
    TqKqSnapshot,
    _initialization_progress_bucket,
)
from trader_runtime.store import RuntimeStore

TOKEN = "phase13-token"
ORIGIN = "http://127.0.0.1:5173"


def test_authenticated_tqsdk_call_disables_sensitive_debug_logs() -> None:
    source = Path(__file__).resolve().parents[1] / "src/trader_runtime/data_providers.py"
    calls = [
        node
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "TqApi"
    ]

    assert len(calls) == 1
    keywords = {item.arg: item.value for item in calls[0].keywords if item.arg}
    assert isinstance(keywords.get("debug"), ast.Constant)
    assert keywords["debug"].value is False
    assert isinstance(keywords.get("disable_print"), ast.Constant)
    assert keywords["disable_print"].value is True


def test_in_process_provider_preserves_one_sided_book() -> None:
    session = object.__new__(OfficialTqKqSession)
    session.api = SimpleNamespace(wait_update=lambda deadline: True)
    session.account = SimpleNamespace(
        balance=1, static_balance=1, available=1, float_profit=0,
        position_profit=0, close_profit=0, commission=0, margin=0,
    )
    session.position_entities = {}
    session.quotes = {"SHFE.au2612": SimpleNamespace(
        last_price=1000, bid_price1=999.5, ask_price1=math.nan,
        price_tick=0.5, volume_multiple=1000, upper_limit=1000,
        lower_limit=600, datetime="2026-08-29 09:00:00",
        pre_close=900, pre_settlement=900, instrument_name="沪金2612",
    )}

    snapshot = session.read(0.1)

    assert snapshot is not None
    assert snapshot.markets["SHFE.au2612"]["bidPrice1"] == 999.5
    assert snapshot.markets["SHFE.au2612"]["askPrice1"] is None


def test_sidecar_provider_preserves_execution_quote_metadata() -> None:
    provider = SidecarReadOnlyDataProvider(
        base_url="http://127.0.0.1:1", token="x" * 32,
        symbols=("SHFE.au2612",),
    )
    provider._status["marketDataStatus"] = "normal"
    provider._snapshot_received_monotonic = time.monotonic()
    provider._markets["SHFE.au2612"] = {
        "symbol": "SHFE.au2612", "name": "沪金", "price": 824.2,
        "quoteDatetime": "2026-08-29 09:00:00", "bidPrice1": 824,
        "askPrice1": 824.4, "priceTick": 0.02, "volumeMultiple": 1000,
        "upperLimit": 900, "lowerLimit": 700,
    }
    market = provider.market_snapshot("turn", "SHFE.au2612")
    assert {key: market[key] for key in (
        "quoteDatetime", "bidPrice1", "askPrice1", "priceTick",
        "volumeMultiple", "upperLimit", "lowerLimit",
    )} == {key: provider._markets["SHFE.au2612"][key] for key in (
        "quoteDatetime", "bidPrice1", "askPrice1", "priceTick",
        "volumeMultiple", "upperLimit", "lowerLimit",
    )}


def test_connection_progress_is_bucketed_and_provider_broadcasts_target_live_sse_only(
    tmp_path: Path,
) -> None:
    assert _initialization_progress_bucket(0.4) == 0.0
    assert _initialization_progress_bucket(19.9) == 10.0
    assert _initialization_progress_bucket(float("nan")) is None

    broker = EventBroker(RuntimeStore(tmp_path / "runtime.sqlite3"))
    connected = broker.create_session("local", "native", "local-user")
    dormant = broker.create_session("local", "native", "local-user")
    broker.subscribers[connected.session_id] = 1
    broker.subscribers[dormant.session_id] = 0
    assert broker.subscribed_session_ids() == (connected.session_id,)


class FixtureTqKqSession:
    def __init__(self, username: str, password: str, symbols: Sequence[str]) -> None:
        assert username == "fixture-user"
        assert password == "fixture-password"
        self.symbols = symbols
        self.closed = False

    def read(self, timeout_seconds: float) -> TqKqSnapshot:
        time.sleep(min(timeout_seconds, 0.005))
        return TqKqSnapshot(
            account={"balance": 500_000, "staticBalance": 498_000, "available": 450_000,
                     "floatProfit": 1200, "positionProfit": 300, "closeProfit": 900,
                     "commission": 100, "margin": 50_000},
            positions=[{"symbol": "SHFE.au2612", "direction": "long", "volume": 1,
                        "unrealizedPnl": 1500}],
            markets={"SHFE.au2612": {"symbol": "SHFE.au2612", "name": "沪金2612",
                                      "price": 824.2, "changePercent": 0.72}},
            captured_at_monotonic=time.monotonic(),
            captured_at_utc="2026-08-26T00:00:00+00:00",
        )

    def close(self) -> None:
        self.closed = True


class StaleFixtureTqKqSession(FixtureTqKqSession):
    def read(self, timeout_seconds: float) -> TqKqSnapshot:
        snapshot = super().read(timeout_seconds)
        return TqKqSnapshot(
            snapshot.account, snapshot.positions, snapshot.markets,
            captured_at_monotonic=time.monotonic() - 60,
            captured_at_utc="2026-08-25T23:59:00+00:00",
        )


async def test_tqkq_provider_is_read_only_and_separates_component_status() -> None:
    provider = TqKqReadOnlyProvider(
        username="fixture-user", password="fixture-password",
        symbols=("SHFE.au2612",), session_factory=FixtureTqKqSession,
    )
    await provider.start()
    for _ in range(100):
        if provider.runtime_status()["marketDataStatus"] == "normal":
            break
        await asyncio.sleep(0.005)
    assert provider.runtime_status() == {
        "accountMode": "tqkq-readonly", "marketDataStatus": "normal",
        "accountStatus": "normal", "tradingGatewayStatus": "read_only",
        "riskStatus": "normal", "dataErrorCode": None,
    }
    account = provider.account_summary("turn")
    assert account["accountName"] == "TqKq 模拟账户"
    assert account["todayNetPnl"] == 1100
    assert account["todayPnl"] == 1100
    assert account["staticBalance"] == 498_000
    assert provider.positions()[0]["volume"] == 1
    assert provider.market_snapshot("turn", "AU2612")["price"] == 824.2
    assert provider._username is None and provider._password is None
    assert not hasattr(provider, "insert_order")
    assert not hasattr(provider, "cancel_order")
    await provider.stop()


async def test_missing_tqkq_credentials_fail_closed_without_starting_a_thread() -> None:
    provider = TqKqReadOnlyProvider(username=None, password=None)
    await provider.start()
    status = provider.runtime_status()
    assert status["marketDataStatus"] == "unavailable"
    assert status["tradingGatewayStatus"] == "unavailable"
    assert status["riskStatus"] == "unknown"
    assert status["dataErrorCode"] == "tqkq_credentials_missing"
    await provider.stop()


async def test_stale_tqkq_snapshot_never_claims_that_risk_is_normal() -> None:
    provider = TqKqReadOnlyProvider(
        username="fixture-user", password="fixture-password",
        symbols=("SHFE.au2612",), session_factory=StaleFixtureTqKqSession,
        stale_after_seconds=1,
    )
    await provider.start()
    for _ in range(100):
        if provider.runtime_status()["marketDataStatus"] == "stale":
            break
        await asyncio.sleep(0.005)
    status = provider.runtime_status()
    assert status["marketDataStatus"] == "stale"
    assert status["accountStatus"] == "stale"
    assert status["riskStatus"] == "unknown"
    await provider.stop()

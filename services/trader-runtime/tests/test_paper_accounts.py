"""本站模拟账户（paper，2026-10-03）：注册即开户、引擎撮合、重启与解绑回落。

撮合引擎用一只 httpx.MockTransport 替身：路径前缀 /accounts/{id}/v1/*、按账
户各自的账本（委托/持仓/成交）、越对手价全额成交否则挂住的撮合规则、撤单保留
已成交量——与 A1 引擎合同同形。控制面（POST /accounts 建户）用 FakePaperClient
替身。默认账户的行情源用 RecordingMarketSource（sidecar 形状的假 provider），
验证两条口径：共享行情读委托给默认 provider；盘前校验行情与撮合同走引擎。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import threading
import time
from contextlib import aclosing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest

import test_account_pool
import trader_runtime.account_pool as account_pool_module
import trader_runtime.paper_accounts as paper_accounts_module
import trader_runtime.wiring.services as wiring_services
from test_account_pool import POOL_URL, SIDECAR_TOKEN, TOKEN, FakePoolClient, NamedFakeProvider, bind_account_id
from test_cloud_accounts import ORIGIN, PASSWORD, PASSWORD_HASH, SESSION_SECRET
from test_stream_revocation import ORIGIN as LOCAL_ORIGIN
from trader_runtime.account_ledger import AccountLedger
from trader_runtime.agent_api import AppCall
from trader_runtime.data_providers import (
    FakeReadOnlyDataProvider,
    SidecarReadOnlyDataProvider,
    fake_market_snapshot,
)
from trader_runtime.main import create_app
from trader_runtime.paper_accounts import PAPER_INITIAL_BALANCE, PaperAccountManager, PaperEngineError
from trader_runtime.routes.account import bind_tqkq_account, store_tqkq_pool_account
from trader_runtime.store import RuntimeStore
from trader_runtime.store.trading_accounts import market_source_owner_id
from trader_runtime.tqkq_retirement import retire_tqkq_trading
from trader_runtime.wiring.accounts import ACCOUNT_HEADER

ENGINE_URL = "http://paper-engine.test"
TRADING_DAY = "2026-10-03"
CHINA = timezone(timedelta(hours=8))
SYMBOL = "SHFE.au2612"


class FakePaperEngine:
    """The site matching engine as one MockTransport handler: per-account books
    (orders / positions / trades) and the sidecar's response shapes under
    /accounts/{id}/v1/*. The crossing rule is A1's contract: a BUY limit at or
    above askPrice1 (a SELL at or below bidPrice1) fills fully at the limit,
    anything else rests; a cancel finishes the order keeping what filled."""

    def __init__(self) -> None:
        self.creates: list[tuple[str, float]] = []
        self.resets: list[str] = []
        self.fail_create = False
        self.paths: list[tuple[str, str]] = []
        self.books: dict[str, dict[str, Any]] = {}
        # Every order request the engine received verbatim (feeRate 等 Runtime 带来的字段)。
        self.submits: list[dict[str, Any]] = []
        # Shared-poll switches: an engine from before the batch route, a batch
        # that cannot be reached, accounts the engine cannot read.
        self.batch_route = True
        self.batch_down = False
        self.unreadable: set[str] = set()

    def book(self, account_id: str) -> dict[str, Any]:
        return self.books.setdefault(account_id, {"orders": {}, "positions": [], "trades": []})

    def create(self, account_id: str, initial_balance: float) -> dict[str, Any]:
        if self.fail_create:
            raise PaperEngineError("paper_engine_unavailable")
        self.creates.append((account_id, initial_balance))
        self.book(account_id)
        return {"accountId": account_id, "initialBalance": initial_balance}

    # -- responses -----------------------------------------------------------

    def _quote(self, symbol: str) -> dict[str, Any]:
        # 盘前校验行情与撮合同源：买卖一夹着最新价，时间每请求现取（15 秒新鲜度）。
        now = datetime.now(CHINA)
        return {
            **fake_market_snapshot("", SYMBOL),
            "symbol": symbol,
            "dataStatus": "normal",
            "isCached": False,
            "marketSessionStatus": "trading",
            "quoteDatetime": now.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "capturedAt": now.isoformat(),
            "ageSeconds": 0.0,
            "price": 824.0,
            "bidPrice1": 823.0,
            "askPrice1": 824.0,
        }

    def _account(self, book: dict[str, Any]) -> dict[str, Any]:
        return {
            "tradingDay": TRADING_DAY,
            "capturedAt": datetime.now(CHINA).isoformat(),
            "equity": PAPER_INITIAL_BALANCE,
            "staticBalance": PAPER_INITIAL_BALANCE,
            "available": PAPER_INITIAL_BALANCE,
            "margin": 0.0, "todayPnl": 0.0, "todayNetPnl": 0.0, "floatProfit": 0.0,
            "positionProfit": 0.0, "closeProfit": 0.0, "commission": 0.0,
            "marginRatio": 0.0, "riskRatio": 0.0,
            "positionCount": len(book["positions"]),
        }

    @staticmethod
    def _report(book: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
        del book  # the report is the order's own state; kept for shape parity
        return {"ordersInitialized": True, "found": True, "order": {
            "clientOrderId": order["clientOrderId"],
            "exchangeOrderId": order["exchangeOrderId"],
            "symbol": order["symbol"], "direction": order["direction"],
            "offset": order["offset"], "priceType": "LIMIT",
            "volume": order["volume"], "volumeLeft": order["volumeLeft"],
            "limitPrice": order["limitPrice"], "status": order["status"],
            "isOnline": order["status"] == "ALIVE", "isDead": order["status"] == "FINISHED",
            "isError": False,
        }}

    def _activity(self, book: dict[str, Any]) -> dict[str, Any]:
        return {
            "schemaVersion": 1, "capturedAt": datetime.now(CHINA).isoformat(),
            "ageSeconds": 0.0, "dataStatus": "normal", "isCached": False,
            "ordersInitialized": True,
            "orders": [self._report(book, order)["order"] for order in book["orders"].values()],
            "trades": [dict(trade) for trade in book["trades"]],
            "liveTrading": "disabled",  # the real engine's activity carries it (sidecar paper.py)
        }

    def _submit(self, book: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        self.submits.append(dict(request))
        quote = self._quote(str(request["symbol"]))
        crossing = (
            request["direction"] == "BUY" and float(request["limitPrice"]) >= float(quote["askPrice1"])
            or request["direction"] == "SELL" and float(request["limitPrice"]) <= float(quote["bidPrice1"])
        )
        order = {
            "clientOrderId": str(request["clientOrderId"]),
            "exchangeOrderId": f"ENG{len(book['orders']) + 1:06d}",
            "symbol": str(request["symbol"]),
            "direction": str(request["direction"]),
            "offset": str(request["offset"]),
            "volume": int(request["volume"]),
            "limitPrice": float(request["limitPrice"]),
            "status": "FINISHED" if crossing else "ALIVE",
            "volumeLeft": 0 if crossing else int(request["volume"]),
        }
        book["orders"][order["clientOrderId"]] = order
        if crossing:
            self._fill(book, order)
        return self._report(book, order)

    def _fill(self, book: dict[str, Any], order: dict[str, Any]) -> None:
        volume = order["volume"] - order["volumeLeft"]
        side = "long" if (order["direction"] == "BUY") == (order["offset"] == "OPEN") else "short"
        position = next((item for item in book["positions"]
                         if item["symbol"] == order["symbol"] and item["direction"] == side), None)
        if order["offset"] == "OPEN":
            if position is None:
                book["positions"].append({
                    "symbol": order["symbol"], "direction": side, "volume": volume,
                    "todayVolume": volume, "historicalVolume": 0,
                    "frozenToday": 0, "frozenHistorical": 0,
                })
            else:
                position["volume"] += volume
                position["todayVolume"] += volume
        elif position is not None:
            position["volume"] -= volume
            position["todayVolume"] -= min(position["todayVolume"], volume)
            if position["volume"] <= 0:
                book["positions"].remove(position)
        book["trades"].append({
            "symbol": order["symbol"],
            "tradeId": f"engine-{len(book['trades']) + 1:06d}",
            "tradeDateTime": time.time_ns(),
            "clientOrderId": order["clientOrderId"],
            "direction": order["direction"], "offset": order["offset"],
            "price": order["limitPrice"], "volume": volume,
            "tradingDay": TRADING_DAY, "volumeMultiple": 1000.0,
        })

    @staticmethod
    def _status() -> dict[str, Any]:
        return {
            "dataStatus": "normal", "accountStatus": "normal",
            "marketDataStatus": "normal", "ageSeconds": 0.0,
            "dataConnectionStage": "ready",
        }

    def _snapshots(self, request: httpx2.Request) -> httpx2.Response:
        if not self.batch_route:
            return httpx2.Response(404, json={"error": "paper_route_not_found"})
        if self.batch_down:
            raise httpx2.ConnectError("paper engine down", request=request)
        accounts: dict[str, Any] = {}
        for account_id in json.loads(request.content)["accountIds"]:
            if account_id in self.unreadable or account_id not in self.books:
                accounts[account_id] = {"error": "account_not_found", "httpStatus": 404}
                continue
            book = self.book(account_id)
            accounts[account_id] = {
                "status": self._status(), "account": self._account(book),
                "positions": [dict(item) for item in book["positions"]],
            }
        return httpx2.Response(200, json={"schemaVersion": 1, "accounts": accounts})

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST" and request.url.path == "/v1/snapshots":
            self.paths.append((request.method, request.url.path))
            return self._snapshots(request)
        match = re.fullmatch(r"/accounts/([^/]+)/v1/(.+)", request.url.path)
        if match is None:
            return httpx2.Response(404, json={"error": "engine_path_unknown"})
        account_id, tail = match.groups()
        self.paths.append((request.method, request.url.path))
        book = self.book(account_id)
        if request.method == "GET":
            if tail == "status":
                return httpx2.Response(200, json=self._status())
            if tail == "account":
                return httpx2.Response(200, json=self._account(book))
            if tail == "positions":
                return httpx2.Response(200, json=[dict(item) for item in book["positions"]])
            if tail == "activity":
                return httpx2.Response(200, json=self._activity(book))
            if tail == "snapshot":  # the engine's combined account cut (sidecar paper.py `combined`)
                return httpx2.Response(200, json={
                    "schemaVersion": 1, "capturedAt": datetime.now(CHINA).isoformat(), "ageSeconds": 0.0,
                    "dataStatus": "normal", "status": self._status(), "account": self._account(book),
                    "positions": [dict(item) for item in book["positions"]], "activity": self._activity(book)})
            if tail.startswith("market/") and tail.endswith("/quote"):
                return httpx2.Response(200, json=self._quote(
                    tail.removeprefix("market/").removesuffix("/quote")))
            if tail.startswith("simulation/orders/"):
                order = book["orders"].get(tail.removeprefix("simulation/orders/"))
                if order is None:
                    return httpx2.Response(200, json={"ordersInitialized": True, "found": False})
                return httpx2.Response(200, json=self._report(book, order))
        elif request.method == "POST" and tail == "simulation/orders/submit":
            return httpx2.Response(200, json=self._submit(book, json.loads(request.content or b"{}")))
        elif request.method == "POST" and tail.endswith("/cancel"):
            order = book["orders"].get(
                tail.removeprefix("simulation/orders/").removesuffix("/cancel"))
            if order is None:
                return httpx2.Response(200, json={"ordersInitialized": True, "found": False})
            order["status"] = "FINISHED"  # 已成交量留在 volumeLeft 的差里
            return httpx2.Response(200, json=self._report(book, order))
        return httpx2.Response(404, json={"error": "engine_path_unknown"})


class FakePaperClient:
    """The engine control-plane double: provisioning answered from the fake
    engine, never over HTTP (base_url/token still feed the provider factory)."""

    base_url = ENGINE_URL
    token = SIDECAR_TOKEN

    def __init__(self, engine: FakePaperEngine) -> None:
        self._engine = engine

    async def create_account(self, account_id: str, initial_balance: float) -> dict[str, Any]:
        return self._engine.create(account_id, initial_balance)

    async def health(self) -> dict[str, Any]:  # 行情账号接力 (test_paper_market_sources covers it)
        return {"marketSource": {"revision": "fake"}}

    async def set_market_sources(self, sources: list[dict[str, Any]]) -> dict[str, Any]:
        return {"revision": "fake"}

    async def reset(self, account_id: str) -> dict[str, Any]:
        self._engine.resets.append(account_id)
        return {"accountId": account_id, "status": "reset", "balance": PAPER_INITIAL_BALANCE,
                "initBalance": PAPER_INITIAL_BALANCE, "cancelledOrders": 0, "clearedPositions": []}


class MockPaperProvider(paper_accounts_module.PaperProvider):
    """A PaperProvider whose HTTP channel answers from the fake engine. The
    SidecarHttp clients built in __init__ point at an address nothing ever
    contacts; replacing both leaves nothing to close beyond what stop() does."""

    engine: FakePaperEngine  # bound per app below

    def __init__(
        self, *, account_id: str, base_url: str, token: str, market_source: Any,
        poller: paper_accounts_module.PaperSnapshotPoller,
    ) -> None:
        super().__init__(account_id=account_id, base_url=base_url, token=token,
                         market_source=market_source, poller=poller)
        transport = httpx2.MockTransport(self.engine.handler)
        self._http.sync = httpx2.Client(base_url=self._base_url, transport=transport)
        self._http.async_client = httpx2.AsyncClient(base_url=self._base_url, transport=transport)


class RecordingMarketSource(SidecarReadOnlyDataProvider):
    """The deployment's default provider double: sidecar-shaped (so the
    simulation-trading guard accepts it), every market read served by the fake
    provider and recorded in `calls` — the shared-market-source oracle."""

    def __init__(self) -> None:
        super().__init__(base_url="http://127.0.0.1:43123", token=SIDECAR_TOKEN, symbols=())
        self.calls: list[str] = []
        self._market = FakeReadOnlyDataProvider()

    def runtime_status(self) -> dict[str, Any]:
        # The deployment's market feed is up: the opening gate every card passes (2026-10-16 规划器) reads it.
        return {**super().runtime_status(), "marketDataStatus": "normal"}

    def instrument_catalog(self) -> dict[str, Any]:
        self.calls.append("instrument_catalog")
        return self._market.instrument_catalog()

    def market_board(self) -> dict[str, Any]:
        self.calls.append("market_board")
        return self._market.market_board()

    def market_quotes(self, symbols: Any) -> dict[str, Any]:
        self.calls.append("market_quotes")
        return self._market.market_quotes(symbols)

    def market_calendar(self) -> dict[str, Any]:
        self.calls.append("market_calendar")
        return self._market.market_calendar()

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        self.calls.append("market_workbench")
        return self._market.market_workbench(symbol, duration_seconds)

    def market_history_nowait(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        self.calls.append("market_history_nowait")
        return self._market.market_history_nowait(symbol, duration_seconds, length)

    def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        self.calls.append("market_history")
        return self._market.market_history(symbol, duration_seconds, length)

    def symbol_settlements(self, symbols: Any, days: int) -> dict[str, Any]:
        self.calls.append("symbol_settlements")
        return self._market.symbol_settlements(symbols, days)

    def main_contract_history(self, symbol: str, days: int) -> dict[str, Any]:
        self.calls.append("main_contract_history")
        return self._market.main_contract_history(symbol, days)

    def symbol_ranking(self, symbol: str, ranking_type: str, days: int) -> dict[str, Any]:
        self.calls.append("symbol_ranking")
        return self._market.symbol_ranking(symbol, ranking_type, days)

    def historical_backtest(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("historical_backtest")
        return self._market.historical_backtest(request)


def _bind_engine(monkeypatch: pytest.MonkeyPatch, engine: FakePaperEngine) -> None:
    class BoundPaperProvider(MockPaperProvider):
        pass

    BoundPaperProvider.engine = engine
    monkeypatch.setattr(paper_accounts_module, "PaperProvider", BoundPaperProvider)


def _bind_poller(manager: PaperAccountManager, engine: FakePaperEngine) -> None:
    """The shared poller's batch reads answered by the same fake engine."""
    manager.poller._http.sync = httpx2.Client(
        base_url=ENGINE_URL, transport=httpx2.MockTransport(engine.handler))


def paper_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: str, agent_mode: str = "fake"):
    """An owner-password app with the paper engine wired: registration on, the
    fake engine behind both the control plane and the per-account provider, and
    the deployment default as a recording sidecar-shaped market source."""
    monkeypatch.setenv("AI_TRADER_PAPER_ENGINE_URL", ENGINE_URL)
    monkeypatch.setenv("AI_TRADER_READONLY_SIDECAR_TOKEN", SIDECAR_TOKEN)
    monkeypatch.setenv("AI_TRADER_SIMULATION_TRADING", "1")
    # The ledger's idle bounds only: the app reads the constants at creation, and
    # a test that waits a pass must not wait a real 5 s tick.
    monkeypatch.setattr(wiring_services, "LEDGER_MIN_INTERVAL", 0.05)
    monkeypatch.setattr(wiring_services, "LEDGER_TICK_SECONDS", 0.2)
    engine = FakePaperEngine()
    _bind_engine(monkeypatch, engine)
    application = create_app(
        tmp_path / database, agent_mode=agent_mode, auth_mode="owner-password",
        owner_username="charles", owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET, cors_origins={ORIGIN},
        data_mode="tqkq-sidecar", data_provider=RecordingMarketSource(),
        agent_provider="deepseek", registration_enabled=True,
        deepseek_api_key="sk-test-deepseek-key-000001",
    )
    manager = application.state.paper_accounts
    assert manager is not None
    manager.client = FakePaperClient(engine)
    _bind_poller(manager, engine)
    return application, engine


def unbind_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: str):
    """A local-token app with both on-demand managers wired: the pool (fake
    control-plane client, fake per-account provider) and the paper engine with
    its real assembly path — the 快期 trading retirement (#1205) runs against both."""
    key_file = tmp_path / f"{database}.key"
    key_file.write_bytes(base64.b64encode(os.urandom(32)))
    monkeypatch.setenv("AI_TRADER_CREDENTIAL_KEY_FILE", str(key_file))
    monkeypatch.setenv("AI_TRADER_TQKQ_POOL_URL", POOL_URL)
    monkeypatch.setenv("AI_TRADER_READONLY_SIDECAR_TOKEN", SIDECAR_TOKEN)
    monkeypatch.setenv("AI_TRADER_PAPER_ENGINE_URL", ENGINE_URL)
    engine = FakePaperEngine()
    _bind_engine(monkeypatch, engine)
    application = create_app(tmp_path / database, local_token=TOKEN, agent_mode="fake", data_mode="fake")
    pool = application.state.account_pool
    assert pool is not None
    pool.client = FakePoolClient()
    paper = application.state.paper_accounts
    assert paper is not None
    paper.client = FakePaperClient(engine)
    _bind_poller(paper, engine)
    real_build = account_pool_module.build_account_services

    def recording_build(spec, **kwargs):
        return real_build(spec, injected=NamedFakeProvider(spec.id), **kwargs)

    monkeypatch.setattr(account_pool_module, "build_account_services", recording_build)
    return application, engine


async def close_paper_app(application) -> None:
    manager = application.state.paper_accounts
    if manager is not None:
        await manager.shutdown()
    application.state.store.close()


async def register_user(application, username: str = "friend") -> tuple[httpx.AsyncClient, str]:
    """One invited registration through the real routes: the owner issues the
    code, the newcomer registers — and is provisioned — in the same call."""
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 headers={"Origin": ORIGIN}) as owner:
        login = await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})
        assert login.status_code == 200
        created = await owner.post("/api/auth/owner/invite-codes")
        assert created.status_code == 200
        code = created.json()["inviteCode"]
    client = httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Origin": ORIGIN})
    registered = await client.post("/api/auth/owner/register", json={
        "username": username, "password": "a-solid-password-123", "inviteCode": code})
    assert registered.status_code == 200
    return client, str(registered.json()["userId"])


async def open_session(client: httpx.AsyncClient) -> str:
    session = await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})
    assert session.status_code == 201
    return str(session.json()["sessionId"])


async def confirmed_order(
    client: httpx.AsyncClient, session_id: str, *, price_intent: str, key: str,
    protection: dict[str, float] | None = None, side: str = "buy_open",
    position_bucket: str | None = None, volume: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One trade card confirmed through the real HTTP routes: the card, then the
    dispatched simulation order (the confirm response's `simulationOrder`)."""
    body: dict[str, Any] = {
        "sessionId": session_id, "symbol": SYMBOL, "side": side, "volume": volume,
        "priceIntent": price_intent, "reason": "本站模拟链路测试",
        **({"positionBucket": position_bucket} if position_bucket else {}),
    }
    if protection is not None:
        body["protection"] = protection
    candidate = await client.post("/api/simulation/proposals", json=body)
    assert candidate.status_code == 201, candidate.text[:300]
    card = candidate.json()
    confirmed = await client.post(f"/api/proposals/{card['proposalId']}/confirm", json={
        "approvalId": card["approvalId"], "proposalVersion": card["version"],
        "idempotencyKey": key})
    assert confirmed.status_code == 200, confirmed.text[:300]
    return card, confirmed.json()["simulationOrder"]


async def wait_paper_positions(application, account_id: str, *, count: int):
    """Wait for the account's polled snapshot to carry `count` positions — the
    close-order precheck reads the poll, which may lag the engine's fill."""
    services = application.state.accounts.get(account_id)
    assert services is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline:
        if len(services.provider.positions()) >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"paper_positions_not_ready account={account_id}")


async def wait_paper_ready(application, account_id: str):
    """Wait for the account's provider poll to see the engine (status normal,
    gateway simulating) — assembly itself is synchronous, its data is not."""
    services = application.state.accounts.get(account_id)
    assert services is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline:
        status = services.provider.runtime_status()
        if (status.get("accountStatus") == "normal"
                and status.get("tradingGatewayStatus") == "simulation"):
            return services
        await asyncio.sleep(0.05)
    raise AssertionError(f"paper_account_not_ready account={account_id}")


async def test_registration_provisions_one_paper_account_per_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, engine = paper_app(tmp_path, monkeypatch, "register.sqlite3")
    try:
        store = application.state.store
        manager = application.state.paper_accounts
        friend, friend_id = await register_user(application)
        try:
            # 注册页读的是匿名 status：部署开了本站模拟盘，注册文案要如实说。
            anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                          base_url="http://test", headers={"Origin": ORIGIN})
            try:
                assert (await anonymous.get("/api/auth/owner/status")).json()["paperConfigured"] is True
            finally:
                await anonymous.aclose()
            assert (await friend.get("/api/auth/owner/status")).json()["bindings"]["paperConfigured"] is True
            record = store.find_user_paper_account(user_id=friend_id)
            assert record is not None
            paper_id = str(record["id"])
            assert paper_id.startswith("paper:")
            assert store.get_user_default_account(user_id=friend_id) == paper_id
            assert [c for c in engine.creates if c[0] == paper_id] == [(paper_id, PAPER_INITIAL_BALANCE)]  # the inviting admin opens one too
            # 幂等：再次补开户拿到同一份账户，不向引擎重复建户。
            assert str((await manager.ensure_paper_account(friend_id))["id"]) == paper_id
            assert [c for c in engine.creates if c[0] == paper_id] == [(paper_id, PAPER_INITIAL_BALANCE)]
            # 新会话落在自己的本站模拟账户上；目录行说明它是谁、由谁执行。
            session_id = await open_session(friend)
            assert store.get_session(session_id)["tradingAccountId"] == paper_id
            directory = (await friend.get("/api/accounts")).json()
            assert directory["current"] == paper_id
            entry = next(item for item in directory["accounts"] if item["id"] == paper_id)
            assert entry["providerKind"] == "paper"
            assert entry["displayName"] == "本站模拟"
            assert entry["executionOwner"] is True
            assert entry["poolStatus"] == "connected"
        finally:
            await friend.aclose()
        # 第二个用户：另一份账户，目录里只见自己的。
        other, other_id = await register_user(application, "other")
        try:
            other_record = store.find_user_paper_account(user_id=other_id)
            assert other_record is not None and str(other_record["id"]) != paper_id
            directory = (await other.get("/api/accounts")).json()
            assert {item["id"] for item in directory["accounts"]
                    if item["providerKind"] == "paper"} == {str(other_record["id"])}
        finally:
            await other.aclose()
    finally:
        await close_paper_app(application)


async def test_paper_session_trades_through_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, engine = paper_app(tmp_path, monkeypatch, "trade.sqlite3")
    try:
        store = application.state.store
        friend, friend_id = await register_user(application)
        try:
            session_id = await open_session(friend)
            account_id = str(store.get_session(session_id)["tradingAccountId"])
            services = await wait_paper_ready(application, account_id)
            assert account_id.startswith("paper:")
            # 状态口径：本站模拟、引擎网关在撮合、模拟交易开着。
            status = (await friend.get("/api/runtime/status")).json()
            assert status["accountMode"] == "paper"
            assert status["accountStatus"] == "normal"
            assert status["tradingGatewayStatus"] == "simulation"
            assert status["simulationTrading"] is True
            assert status["dataSource"] == "paper"
            assert status["accountLabel"] == "本站模拟账户"
            assert status["account"]["id"] == account_id
            # 对手价开多 → 越卖一，引擎全额成交；卡与委托都标本站模拟。
            card, filled = await confirmed_order(
                friend, session_id, price_intent="aggressive", key="paper-fill",
                protection={"stopLossPrice": 812.0})
            assert card["accountLabel"] == "本站模拟账户"
            assert filled["displayStatus"] == "已成交"
            assert filled["accountLabel"] == "本站模拟账户"
            book = engine.book(account_id)
            assert [(item["direction"], item["volume"]) for item in book["positions"]] == [("long", 1)]
            # 成交驱动的账本与保护：同步一轮，止损按卡上的固定价挂起。
            assert services.autonomy is not None
            assert services.autonomy.sync_strategy_protection() == {}
            stops = store.connection.execute(
                "SELECT user_id, symbol, position_direction, volume, trigger_price, status"
                " FROM ai_stop_orders WHERE trading_account_id = ?", (account_id,)).fetchall()
            assert [(row["user_id"], row["symbol"], row["position_direction"], row["volume"], row["status"])
                    for row in stops] == [(friend_id, SYMBOL, "long", 1, "ARMED")]
            assert float(stops[0]["trigger_price"]) == 812.0
            assert services.account_ledger is not None
            await services.account_ledger.run_once()
            days = store.list_account_daily(trading_account_id=account_id)
            assert [row["tradingDay"] for row in days] == [TRADING_DAY]
            assert days[0]["equity"] == PAPER_INITIAL_BALANCE
            assert [trade["volume"] for trade in
                    store.list_account_trades(symbol=SYMBOL, trading_account_id=account_id)] == [1]
            # 挂单价不越对手价 → 挂住；撤单 → 已撤销；两个委托都在列表里。
            _, resting = await confirmed_order(friend, session_id, price_intent="passive", key="paper-rest")
            assert resting["displayStatus"] == "已受理"
            cancelled = await friend.post(f"/api/simulation/orders/{resting['clientOrderId']}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["displayStatus"] == "已撤销"
            listed = (await friend.get("/api/simulation/orders?recoveryWait=false")).json()["items"]
            assert {item["displayStatus"] for item in listed} == {"已成交", "已撤销"}
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


async def test_paper_orders_carry_the_effective_fee_rate(tmp_path, monkeypatch):
    """本站模拟盘的扣费费率随单走（2026-10-08）：未自定义费率时带交易所标准（种的缓存），
    自定义了带自定义的那条（override 命中合约用 override）。"""
    from conftest import per_lot_entry, seed_exchange_fees

    seed_exchange_fees(tmp_path, {"SHFE.au2612": per_lot_entry("SHFE", "au2612", "au", 1000, 5, 6, 7)})
    application, engine = paper_app(tmp_path, monkeypatch, "feerate.sqlite3")
    try:
        friend, _ = await register_user(application)
        try:
            session_id = await open_session(friend)
            account_id = str(application.state.store.get_session(session_id)["tradingAccountId"])
            await wait_paper_ready(application, account_id)
            await confirmed_order(friend, session_id, price_intent="aggressive", key="paper-feerate")
            [request] = engine.submits
            assert request["feeRate"] == {"mode": "per_lot", "open": 5.0, "closeYesterday": 6.0,
                                          "closeToday": 7.0, "openPerLotExtra": 0.0,
                                          "closeYesterdayPerLotExtra": 0.0, "closeTodayPerLotExtra": 0.0}

            # 自定义费率整套替换：override 精确命中 au2612，就用 override 那条。
            custom = await friend.post("/api/trading-costs", json={
                "source": "custom", "default": {"mode": "per_lot", "open": 1, "closeYesterday": 1,
                                                "closeToday": 1},
                "overrides": [{"symbol": "SHFE.au2612", "mode": "per_lot", "open": 2,
                               "closeYesterday": 3, "closeToday": 4}]})
            assert custom.status_code == 200, custom.text[:200]
            await confirmed_order(friend, session_id, price_intent="aggressive", key="paper-feerate-2")
            assert engine.submits[-1]["feeRate"]["open"] == 2
            assert engine.submits[-1]["feeRate"]["closeToday"] == 4
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


async def test_paper_session_status_describes_the_paper_account_not_the_deployment_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-10-08: a new user with only the 本站模拟账户 read “快期模拟账户” in 设置 → 账户 and in
    the greeting — the native adapter's bootstrap / resume runtime.status described the deployment
    default provider. Every session status now names the session's own paper account."""
    application, _engine = paper_app(tmp_path, monkeypatch, "native-status.sqlite3", agent_mode="native")
    try:
        store = application.state.store
        friend, _friend_id = await register_user(application)
        try:
            session_id = await open_session(friend)
            account_id = str(store.get_session(session_id)["tradingAccountId"])
            assert account_id.startswith("paper:")
            await wait_paper_ready(application, account_id)
            assert (await friend.get(f"/api/sessions/{session_id}")).status_code == 200
        finally:
            await friend.aclose()
        events = [event for _, event in store.list_events(session_id)]
        statuses = [event["payload"] for event in events if event["name"] == "runtime.status"]
        assert len(statuses) >= 2  # bootstrap, then the resume's refresh
        for status in statuses:
            assert status["accountMode"] == "paper"
            assert status["dataSource"] == "paper"
            assert status["accountLabel"] == "本站模拟账户"
            assert status["capabilitySummary"].startswith("本站模拟账户 · ")
            assert status["liveTrading"] is False
            assert status["account"]["id"] == account_id
        assert statuses[-1]["capabilitySummary"] == "本站模拟账户 · 模拟执行可用 · 实盘关闭"
        greeting = next(event["payload"]["text"] for event in events
                        if event["name"] == "message.completed" and "已连接" in event["payload"]["text"])
        assert "本站模拟账户" in greeting and "快期" not in greeting
    finally:
        await close_paper_app(application)


async def test_idle_paper_accounts_are_detached_busy_and_present_ones_stay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scale (2026-10-03): every assembled paper account polls the engine, so an
    account nobody uses must not stay assembled forever. Idle = no granted user
    heartbeat past idle_seconds and nothing working for it (strategy, armed stop,
    in-flight order); detaching keeps the engine book, a request re-assembles it."""
    application, engine = paper_app(tmp_path, monkeypatch, "idle.sqlite3")
    try:
        store = application.state.store
        manager = application.state.paper_accounts
        presence = application.state.presence
        assert manager.presence is presence
        friend, friend_id = await register_user(application)
        try:
            session_id = await open_session(friend)
            busy_id = str(store.get_session(session_id)["tradingAccountId"])
            await confirmed_order(friend, session_id, price_intent="passive", key="idle-busy")
            await wait_paper_ready(application, busy_id)
        finally:
            await friend.aclose()
        other, other_id = await register_user(application, "other")
        try:
            await open_session(other)
        finally:
            await other.aclose()
        idle_id = str(store.find_user_paper_account(user_id=other_id)["id"])
        assert application.state.accounts.get(idle_id) is not None
        # The resting order (accepted, ALIVE at the engine) keeps busy_id busy.
        # Both users just showed up: nothing is detached.
        presence.touch(friend_id)
        presence.touch(other_id)
        assert await manager.scan_idle() == []
        # Everyone gone past idle_seconds: only the account with nothing working detaches.
        manager.idle_seconds = 0.0
        assert await manager.scan_idle() == [idle_id]
        assert application.state.accounts.get(idle_id) is None
        assert application.state.accounts.get(busy_id) is not None
        assert manager.account_state(idle_id) == "offline"
        assert idle_id in engine.books  # the engine keeps the book
        # The user comes back: the next request re-assembles the same account.
        again = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                  base_url="http://test", headers={"Origin": ORIGIN},
                                  cookies=other.cookies)
        try:
            manager.idle_seconds = 3600.0
            await open_session(again)
            assert application.state.accounts.get(idle_id) is not None
        finally:
            await again.aclose()
    finally:
        await close_paper_app(application)


async def wait_account_status(application, account_id: str, expected: str) -> None:
    services = application.state.accounts.get(account_id)
    assert services is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while services.provider.runtime_status().get("accountStatus") != expected:
        assert loop.time() < deadline, (account_id, services.provider.runtime_status())
        await asyncio.sleep(0.05)


async def test_paper_accounts_share_one_batched_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scale (2026-10-03, second round): assembled paper accounts no longer run
    a poll thread each; one shared poller reads them all with one batch request
    a second. Each account keeps its own status and degradation: an account the
    engine cannot read degrades alone, an unreachable engine degrades all, an
    engine without the batch route is read account by account, and a detached
    account leaves the poll."""
    application, engine = paper_app(tmp_path, monkeypatch, "shared-poll.sqlite3")
    # The poller's 1 s tick is its deployment pace, not what this test checks: shrink it so
    # each status transition costs one fast cycle instead of a real second.
    application.state.paper_accounts.poller.interval_seconds = 0.05
    try:
        store = application.state.store
        manager = application.state.paper_accounts
        ids: list[str] = []
        for name in ("friend", "other"):
            client, user_id = await register_user(application, name)
            try:
                await open_session(client)
            finally:
                await client.aclose()
            ids.append(str(store.find_user_paper_account(user_id=user_id)["id"]))
        first, second = ids
        for account_id in ids:
            await wait_paper_ready(application, account_id)
        poller_thread = manager.poller._thread
        assert poller_thread is not None and poller_thread.is_alive()
        assert not any(thread.name == "readonly-sidecar-client" for thread in threading.enumerate())
        assert ("POST", "/v1/snapshots") in engine.paths
        assert not any(path.endswith("/v1/status") for _, path in engine.paths)

        # One account the engine cannot read: it degrades, the other stays live.
        engine.unreadable.add(first)
        await wait_account_status(application, first, "unavailable")
        assert application.state.accounts.get(first).provider.runtime_status()["dataErrorCode"] \
            == "readonly_sidecar_sidecarhttperror"
        engine.books[second]["positions"].append({
            "symbol": SYMBOL, "direction": "long", "volume": 1, "todayVolume": 1,
            "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0})
        second_provider = application.state.accounts.get(second).provider
        for _ in range(100):
            if second_provider.positions():
                break
            await asyncio.sleep(0.05)
        assert second_provider.positions()[0]["symbol"] == SYMBOL
        assert second_provider.runtime_status()["accountStatus"] == "normal"
        engine.unreadable.clear()
        await wait_account_status(application, first, "normal")

        # A subscriber of one account raising does not stop the shared thread.
        first_provider = application.state.accounts.get(first).provider

        def broken() -> None:
            raise RuntimeError("subscriber_bug")

        unsubscribe = first_provider.subscribe_snapshot(broken)
        try:
            engine.books[second]["positions"].clear()
            for _ in range(100):
                if not second_provider.positions():
                    break
                await asyncio.sleep(0.05)
            assert second_provider.positions() == []
            assert poller_thread.is_alive()
        finally:
            unsubscribe()

        # The engine unreachable: every paper account degrades, then recovers.
        engine.batch_down = True
        for account_id in ids:
            await wait_account_status(application, account_id, "unavailable")
        engine.batch_down = False
        for account_id in ids:
            await wait_account_status(application, account_id, "normal")

        # An engine from before the batch route (the Runtime switches first during
        # a release): each account is read on its own and stays live.
        engine.batch_route = False
        engine.paths.clear()
        for account_id in ids:
            for _ in range(100):
                if any(path.endswith(f"{account_id}/v1/status") for _, path in engine.paths):
                    break
                await asyncio.sleep(0.05)
            status = application.state.accounts.get(account_id).provider.runtime_status()
            assert status["accountStatus"] == "normal", status
            assert any(path.endswith(f"{account_id}/v1/status") for _, path in engine.paths)

        # A detached account leaves the shared poll.
        engine.batch_route = True
        assert await manager.detach(first, reason="test")
        engine.paths.clear()
        for _ in range(100):
            if ("POST", "/v1/snapshots") in engine.paths:
                break
            await asyncio.sleep(0.05)
        assert ("POST", "/v1/snapshots") in engine.paths
        assert manager.poller._providers.keys() == {application.state.accounts.get(second).provider}
        # The last account gone: the poll thread ends with it.
        assert await manager.detach(second, reason="test")
        poller_thread.join(2.0)
        assert not poller_thread.is_alive() and manager.poller._thread is None
    finally:
        await close_paper_app(application)


async def test_another_user_cannot_reach_a_paper_account_by_any_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolation (2026-10-03, before opening registration): the engine trusts its
    caller, so the Runtime is the only gate. User B naming user A's paper account
    — account header on every read and write route, a session switched onto it,
    A's order ids on the order actions — is refused every time, and A's book in
    the engine stays exactly as it was."""
    application, engine = paper_app(tmp_path, monkeypatch, "isolation.sqlite3")
    try:
        store = application.state.store
        friend, friend_id = await register_user(application)
        try:
            friend_session = await open_session(friend)
            victim = str(store.get_session(friend_session)["tradingAccountId"])
            _, resting = await confirmed_order(friend, friend_session, price_intent="passive", key="isolation-a")
            await wait_paper_ready(application, victim)
        finally:
            await friend.aclose()
        book_before = json.dumps(engine.book(victim), sort_keys=True, default=str)
        intruder, intruder_id = await register_user(application, "intruder")
        try:
            own = str(store.find_user_paper_account(user_id=intruder_id)["id"])
            assert own != victim
            header = {"X-AI-Trader-Account": victim}
            for path in ("/api/account/summary", "/api/positions", "/api/account/activity",
                         "/api/account/pnl", "/api/account/journal", "/api/simulation/orders",
                         "/api/runtime/status"):
                response = await intruder.get(path, headers=header)
                assert response.status_code in (403, 404), (path, response.status_code, response.text[:200])
            # A's order ids, through B's own context and through the header.
            for headers in ({}, header):
                for action in ("cancel", "query", "abandon"):
                    response = await intruder.post(
                        f"/api/simulation/orders/{resting['clientOrderId']}/{action}", headers=headers)
                    assert response.status_code in (403, 404, 409), (action, headers, response.status_code)
            # A card naming A's account (the header an external CLI sends with --account): a named account
            # is the caller's intent (#947 review), so one B holds no grant on is refused outright — no card on
            # A's book, and none silently moved to B's own.
            session_id = await open_session(intruder)
            response = await intruder.post("/api/simulation/proposals", headers=header, json={
                "sessionId": session_id, "symbol": SYMBOL, "side": "buy_open", "volume": 1,
                "priceIntent": "passive", "reason": "越权测试"})
            assert response.status_code in (403, 404), response.status_code
            assert not store.connection.execute(
                "SELECT 1 FROM proposals WHERE session_id = ?", (session_id,)).fetchall()
            # B cannot use another user's session either.
            response = await intruder.post("/api/simulation/proposals", json={
                "sessionId": friend_session, "symbol": SYMBOL, "side": "buy_open", "volume": 1,
                "priceIntent": "passive", "reason": "越权测试"})
            assert response.status_code in (403, 404), response.status_code
            # Switching B's own session onto A's account.
            response = await intruder.put(f"/api/sessions/{session_id}/account", json={"accountId": victim})
            assert response.status_code in (403, 404), response.status_code
            assert str(store.get_session(session_id)["tradingAccountId"]) == own
            # The directory never lists A's account to B.
            listed = {item["id"] for item in (await intruder.get("/api/accounts")).json()["accounts"]}
            assert victim not in listed and own in listed
            # B trading on B's own account leaves A's book untouched.
            await confirmed_order(intruder, session_id, price_intent="passive", key="isolation-b")
        finally:
            await intruder.aclose()
        assert json.dumps(engine.book(victim), sort_keys=True, default=str) == book_before
        assert engine.book(victim)["orders"][resting["clientOrderId"]]["status"] == "ALIVE"
    finally:
        await close_paper_app(application)


async def test_a_card_route_acts_on_the_account_the_caller_names_over_the_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#947 review: an external CLI/MCP names the account (`--account paper:…` → the account header) while the
    conversation it passes is on the default (快期) account. The named account is the caller's intent: the card
    is made — and confirmed — on the paper account, and says it is not the conversation's account."""
    application, engine = paper_app(tmp_path, monkeypatch, "card-named-account.sqlite3")
    try:
        store = application.state.store
        default_id = application.state.accounts.default.account_id
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Origin": ORIGIN}) as owner:
            assert (await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
            paper_id = (await owner.post("/api/accounts/open-paper")).json()["accountId"]
            # The owner chose the main account as default (#1205: without one, their paper account comes first).
            owner_id = str((await owner.get("/api/auth/owner/status")).json()["userId"])
            store.set_user_default_account(user_id=owner_id, account_id=default_id)
            # The first read naming the account assembles it (on demand), then its poll sees the engine.
            await owner.get("/api/account/summary", headers={"X-AI-Trader-Account": paper_id})
            await wait_paper_ready(application, paper_id)
            session_id = await open_session(owner)
            assert str(store.get_session(session_id)["tradingAccountId"]) == default_id
            response = await owner.post("/api/simulation/proposals", headers={"X-AI-Trader-Account": paper_id}, json={
                "sessionId": session_id, "symbol": SYMBOL, "side": "buy_open", "volume": 1,
                "priceIntent": "aggressive", "reason": "外部 CLI 指定本站账户"})
            assert response.status_code == 201, response.text[:300]
            card = response.json()
            assert card["accountLabel"] == "本站模拟账户"
            assert card["accountOverride"]["accountId"] == paper_id
            assert card["accountOverride"]["sessionAccountId"] == default_id
            assert str(store.get(card["proposalId"])["tradingAccountId"]) == paper_id
            said = [event["payload"]["text"] for _, event in store.list_events(session_id)
                    if event["name"] == "message.completed"]
            assert any(f"按指定账户 {paper_id}" in text for text in said)
            confirmed = await owner.post(f"/api/proposals/{card['proposalId']}/confirm", json={
                "approvalId": card["approvalId"], "proposalVersion": card["version"],
                "idempotencyKey": "named-account-card"})
            assert confirmed.status_code == 200, confirmed.text[:300]
            assert confirmed.json()["simulationOrder"]["clientOrderId"] in engine.book(paper_id)["orders"]
    finally:
        await close_paper_app(application)


async def test_a_card_stays_on_the_account_it_was_planned_on_when_the_conversation_switches_mid_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#932 A6（同账户 TOCTOU）：the card route picked the conversation's paper account (its provider, its reads); the
    conversation is switched to the default account while the plan is being made (here: on the planner's catalog read,
    between choosing the account and writing the card). The card stays on the account it was planned on — never the
    other account's book built from the first one's data — and says it is not the conversation's account now."""
    application, engine = paper_app(tmp_path, monkeypatch, "card-frozen-account.sqlite3")
    try:
        store = application.state.store
        default_id = application.state.accounts.default.account_id
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Origin": ORIGIN}) as owner:
            assert (await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
            paper_id = (await owner.post("/api/accounts/open-paper")).json()["accountId"]
            await owner.get("/api/account/summary", headers={"X-AI-Trader-Account": paper_id})
            await wait_paper_ready(application, paper_id)
            session_id = await open_session(owner)
            assert (await owner.put(f"/api/sessions/{session_id}/account",
                                    json={"accountId": paper_id})).status_code == 200
            user_id = str(store.get_session(session_id)["userId"])
            provider = application.state.accounts.get(paper_id).provider
            original, switched = provider.instrument_catalog, []

            def catalog_then_switch() -> dict[str, Any]:
                if not switched:  # the conversation moves to the default account mid-plan, once
                    store.set_session_trading_account(session_id, default_id, user_id=user_id)
                    switched.append(True)
                return original()
            provider.instrument_catalog = catalog_then_switch
            response = await owner.post("/api/simulation/proposals", json={
                "sessionId": session_id, "symbol": SYMBOL, "side": "buy_open", "volume": 1,
                "priceIntent": "aggressive", "reason": "规划途中会话改绑"})
            assert switched and str(store.get_session(session_id)["tradingAccountId"]) == default_id
            assert response.status_code == 201, response.text[:300]
            card = response.json()
            assert str(store.get(card["proposalId"])["tradingAccountId"]) == paper_id
            assert card["accountLabel"] == "本站模拟账户"
            assert card["accountOverride"]["accountId"] == paper_id
            assert card["accountOverride"]["sessionAccountId"] == default_id
            assert engine.book(paper_id)["orders"] == {} or all(
                order.get("status") != "ALIVE" for order in engine.book(paper_id)["orders"].values())  # a card only
    finally:
        await close_paper_app(application)


async def test_owner_login_lands_on_the_own_paper_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快期只管行情 (owner 2026-10-31): the administrator's login opens their 本站模拟 account and makes
    it the default, like a 快期 login; the static 快期 main account is a market source and leaves the
    account directory unless a session still sits on it. Opening again returns the same account."""
    application, engine = paper_app(tmp_path, monkeypatch, "open-paper.sqlite3")
    try:
        store = application.state.store
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Origin": ORIGIN}) as owner:
            assert (await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
            owner_id = str((await owner.get("/api/auth/owner/status")).json()["userId"])
            paper_id = store.get_user_default_account(user_id=owner_id)
            assert paper_id is not None and paper_id.startswith("paper:")
            session_id = await open_session(owner)
            assert str(store.get_session(session_id)["tradingAccountId"]) == paper_id
            main_id = application.state.accounts.default.account_id
            listed = {item["id"] for item in (await owner.get("/api/accounts")).json()["accounts"]}
            assert paper_id in listed and main_id not in listed
            # A session still on the main account (an old conversation) lists it as where it is.
            store.set_user_default_account(user_id=owner_id, account_id=main_id)
            session_id = await open_session(owner)
            assert str(store.get_session(session_id)["tradingAccountId"]) == main_id
            directory = (await owner.get("/api/accounts", headers={ACCOUNT_HEADER: main_id})).json()
            assert directory["current"] == main_id and main_id in {item["id"] for item in directory["accounts"]}
            again = await owner.post("/api/accounts/open-paper")
            assert again.json()["accountId"] == paper_id
            assert engine.creates == [(paper_id, PAPER_INITIAL_BALANCE)]
            # Logging in again puts the default back on 本站模拟.
            assert (await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
            assert store.get_user_default_account(user_id=owner_id) == paper_id
        anonymous = httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Origin": ORIGIN})
        try:
            assert (await anonymous.post("/api/accounts/open-paper")).status_code in (401, 403)
        finally:
            await anonymous.aclose()
    finally:
        await close_paper_app(application)


async def test_app_call_acts_on_the_account_the_caller_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-10-09 主动测试：外部 agent 经 /api/agent/call 带本站账户头说“买一手螺纹”，新建的对话
    却绑到默认（快期）账户，卡在快期成交——入口只转发了登录凭证，账户头被丢掉。账户头要随调用
    转发到业务接口与 chat:send 新建的会话；不带头时仍落默认账户。重启后空闲的本站账户在该账户
    会话里第一句对话前先装配，不再整轮报“当前未装配”。"""
    application, _ = paper_app(tmp_path, monkeypatch, "app-call-account.sqlite3")
    try:
        store = application.state.store
        default_id = application.state.accounts.default.account_id
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Origin": ORIGIN}) as owner:
            assert (await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
            paper_id = (await owner.post("/api/accounts/open-paper")).json()["accountId"]
            # The owner chose the main account as default (#1205: without one, their paper account comes first).
            owner_id = str((await owner.get("/api/auth/owner/status")).json()["userId"])
            store.set_user_default_account(user_id=owner_id, account_id=default_id)
            header = {"X-AI-Trader-Account": paper_id}

            async def call(body, headers=None):
                response = await owner.post("/api/agent/call", json=body, headers=headers or {})
                assert response.status_code == 200, response.text[:300]
                return response.json()

            read = await call({"operation": "GET /api/accounts"}, header)
            assert read["data"]["current"] == paper_id
            assert (await call({"operation": "GET /api/accounts"}))["data"]["current"] == default_id

            named = (await call({"operation": "chat:send", "body": {"text": "你好", "timeoutSeconds": 5}}, header))["data"]
            assert str(store.get_session(named["sessionId"])["tradingAccountId"]) == paper_id
            assert named["accountId"] == paper_id
            plain = (await call({"operation": "chat:send", "body": {"text": "你好", "timeoutSeconds": 5}}))["data"]
            assert str(store.get_session(plain["sessionId"])["tradingAccountId"]) == default_id
            assert plain["accountId"] == default_id

            manager = application.state.paper_accounts
            await manager.shutdown()
            await manager.start_background()
            assert application.state.accounts.get(paper_id) is None
            sent = await owner.post(f"/api/sessions/{named['sessionId']}/messages", json={"text": "你好"})
            assert sent.status_code == 202, sent.text[:200]
            assert application.state.accounts.get(paper_id) is not None
    finally:
        await close_paper_app(application)


async def test_nested_tool_reads_the_session_account_not_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#674：外部 agent 带账户头调 tool:get_trade_journal，工具会话绑到了该账户，但工具内部再经
    AppCapabilities.call 转发 HTTP 时只带回了登录凭证，业务请求重新解析到默认账户。统一转发边界
    从当次会话补回账户头。用同一用户可读的两个纸账户做正控：A（owner 自己的，设为默认）与
    B（另一成员注册即开、授权 owner 的），各有一笔不同的完整往返——直接 HTTP、agent HTTP、
    嵌套 tool 各读各的账户；显式非空头覆盖会话；空/空白头与 resolve_account 同义（未指定）；
    无头跟默认；重绑 A→B→A、改默认都不动已绑会话；嵌套 set_trade_note 只写会话账户；
    账户撤权后拒绝，不静默回落默认。"""
    application, _ = paper_app(tmp_path, monkeypatch, "nested-tool-account.sqlite3", agent_mode="native")
    try:
        store = application.state.store
        deployment_default = application.state.accounts.default.account_id
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Origin": ORIGIN}) as owner:
            assert (await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
            user = str((await owner.get("/api/auth/owner/status")).json()["userId"])
            # A：owner 自己开通的本站模拟账户（生产 bind 用的同一 store 写法把它设为默认，
            # 让无头调用落在一个可读账户上，而不是拿“读不到”当正控）。
            account_a = (await owner.post("/api/accounts/open-paper")).json()["accountId"]
            store.set_user_default_account(user_id=user, account_id=account_a)
            # B：另一成员注册即开的本站模拟账户，授权 owner —— 同用户两个可读账户、两份不同账本。
            friend, friend_id = await register_user(application)
            try:
                account_b = str(store.find_user_paper_account(user_id=friend_id)["id"])
            finally:
                await friend.aclose()
            store.grant_account(account_id=account_b, user_id=user, role="operator", granted_by=friend_id)
            # 各账户一笔完整往返（买卖都主动成交）。引擎单号按账户各自计数、两账户的 round id
            # 相同，用手数区分账本内容（A 一手、B 两手），读取归属按 (id, volume) 断言。
            trips: dict[str, tuple[str, int]] = {}
            for account, volume in ((account_a, 1), (account_b, 2)):
                bound = await owner.post("/api/sessions", json={"mode": "local"},
                                         headers={"X-AI-Trader-Account": account})
                assert bound.status_code == 201, bound.text[:300]
                trade_session = bound.json()["sessionId"]
                await confirmed_order(owner, trade_session, price_intent="aggressive",
                                      key=f"nested-{account[-6:]}-open", volume=volume)
                await wait_paper_positions(application, account, count=1)
                await confirmed_order(owner, trade_session, price_intent="aggressive",
                                      key=f"nested-{account[-6:]}-close", side="sell_close",
                                      position_bucket="today", volume=volume)
                direct = await owner.get("/api/account/journal", headers={"X-AI-Trader-Account": account})
                assert direct.status_code == 200, direct.text[:300]
                [trip] = direct.json()["trades"]
                trips[account] = (trip["id"], trip["volume"])
            assert trips[account_a][1] != trips[account_b][1]

            def rounds(entries: list[dict[str, Any]]) -> list[tuple[str, int]]:
                return [(t["id"], t["volume"]) for t in entries]

            # agent HTTP 与嵌套 tool：带 B 头读 B（#672 修了第一条，这里补上嵌套 tool），带 A 头读 A。
            async def nested_journal(account: str) -> dict[str, Any]:
                response = await owner.post("/api/agent/call",
                    json={"operation": "tool:get_trade_journal", "body": {"days": 30}},
                    headers={"X-AI-Trader-Account": account})
                assert response.status_code == 200, response.text[:300]
                return response.json()

            on_b = await nested_journal(account_b)
            assert on_b["ok"], on_b
            assert rounds(on_b["data"]["data"]["trades"]) == [trips[account_b]]
            picks = (await owner.post("/api/agent/call",
                json={"operation": "tool:get_review_picks", "body": {"scope": "week"}},
                headers={"X-AI-Trader-Account": account_b})).json()
            assert picks["ok"] and picks["data"]["data"]["total"] == 1
            on_a = await nested_journal(account_a)
            assert on_a["ok"] and rounds(on_a["data"]["data"]["trades"]) == [trips[account_a]]
            # 无头调用跟默认账户 A（正向：读出 A 的那笔）。
            headless = (await owner.post("/api/agent/call",
                json={"operation": "tool:get_trade_journal", "body": {"days": 30}})).json()
            assert headless["ok"], headless
            assert rounds(headless["data"]["data"]["trades"]) == [trips[account_a]]
            # 内置 AI 的嵌套读取跟随会话自己的账户；重绑 A→B→A 每次重读。
            bridge = application.state.app_capabilities
            adapter = application.state.adapter
            if hasattr(adapter, "adapter_for_user"):
                adapter = adapter.adapter_for_user(user)
            tools = adapter.trader_tools
            plain = await owner.post("/api/sessions", json={"mode": "local"})
            chat_id = plain.json()["sessionId"]
            assert str(store.get_session(chat_id)["tradingAccountId"]) == account_a
            bridge.bind_session(chat_id, user, dict(owner.build_request("GET", "/").headers))

            async def chat_journal() -> dict[str, Any]:
                envelope, _ = await tools.call_async(tool="get_trade_journal", arguments={"days": 30},
                    turn_id="nested-account", mode="local", session_id=chat_id, user_id=user)
                return {"success": envelope["success"],
                        **json.loads(envelope["contentItems"][0]["text"])}

            default_read = await chat_journal()
            assert default_read["success"] and rounds(default_read["data"]["trades"]) == [trips[account_a]]
            assert (await owner.put(f"/api/sessions/{chat_id}/account", json={"accountId": account_b})).status_code == 200
            bound_b = await chat_journal()
            assert bound_b["success"] and rounds(bound_b["data"]["trades"]) == [trips[account_b]]
            assert (await owner.put(f"/api/sessions/{chat_id}/account", json={"accountId": account_a})).status_code == 200
            back_on_a = await chat_journal()
            assert back_on_a["success"] and rounds(back_on_a["data"]["trades"]) == [trips[account_a]]
            # 会话绑 B 时经真实 bridge.call 验证转发边界的判定：空/空白账户头＝未指定
            # （与 resolve_account 同义）→ 仍读 B；非空显式 A 覆盖会话 → 读 A。
            assert (await owner.put(f"/api/sessions/{chat_id}/account", json={"accountId": account_b})).status_code == 200
            login_headers = dict(owner.build_request("GET", "/api/agent/capabilities").headers)
            for blank in ("", "   "):
                forwarded = await bridge.call(AppCall(operation="GET /api/account/journal"), user=user,
                    headers={**login_headers, "x-ai-trader-account": blank}, session_id=chat_id)
                assert forwarded["ok"], forwarded
                assert rounds(forwarded["data"]["trades"]) == [trips[account_b]]
            explicit = await bridge.call(AppCall(operation="GET /api/account/journal"), user=user,
                headers={**login_headers, "x-ai-trader-account": account_a}, session_id=chat_id)
            assert explicit["ok"] and rounds(explicit["data"]["trades"]) == [trips[account_a]]
            # 改用户的默认账户（换回部署默认）不影响已绑 B 的会话：嵌套读取仍读 B。
            store.set_user_default_account(user_id=user, account_id=deployment_default)
            still_b = await chat_journal()
            assert still_b["success"] and rounds(still_b["data"]["trades"]) == [trips[account_b]]
            # 嵌套写入口 set_trade_note 只落在会话绑定的 B 上；A 的同一字段不变。
            note_envelope, _ = await tools.call_async(tool="set_trade_note",
                arguments={"roundId": trips[account_b][0], "text": "这手是按计划试仓，拿到目标位再平。"},
                turn_id="nested-account", mode="local", session_id=chat_id, user_id=user)
            assert note_envelope["success"], note_envelope
            after_write = await chat_journal()
            assert rounds(after_write["data"]["trades"]) == [trips[account_b]]
            assert after_write["data"]["trades"][0]["note"] == "这手是按计划试仓，拿到目标位再平。"
            journal_a = await owner.get("/api/account/journal", headers={"X-AI-Trader-Account": account_a})
            assert journal_a.status_code == 200
            [a_trip] = journal_a.json()["trades"]
            assert (a_trip["id"], a_trip["volume"]) == trips[account_a] and a_trip["note"] is None
            # 会话仍在 B 上时撤权：嵌套读取拒绝（403 account_not_granted），外层带 B 头同样拒绝。
            store.delete_account_grants(account_id=account_b)
            revoked = await chat_journal()
            assert not revoked["success"] and "account_not_granted" in str(revoked.get("warning"))
            refused = await owner.post("/api/agent/call",
                json={"operation": "tool:get_trade_journal", "body": {"days": 30}},
                headers={"X-AI-Trader-Account": account_b})
            assert refused.status_code == 403, refused.text[:200]
        await application.state.adapter.shutdown()
    finally:
        await close_paper_app(application)


async def test_restart_reassembles_busy_accounts_and_keeps_engine_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, engine = paper_app(tmp_path, monkeypatch, "restart.sqlite3")
    try:
        store = application.state.store
        manager = application.state.paper_accounts
        friend, _ = await register_user(application)
        try:
            session_id = await open_session(friend)
            busy_id = str(store.get_session(session_id)["tradingAccountId"])
            _, resting = await confirmed_order(friend, session_id, price_intent="passive", key="paper-busy")
            await wait_paper_ready(application, busy_id)
        finally:
            await friend.aclose()
        other, other_id = await register_user(application, "other")
        try:
            await open_session(other)  # 装配一次，此后按需
        finally:
            await other.aclose()
        idle_record = store.find_user_paper_account(user_id=other_id)
        assert idle_record is not None
        idle_id = str(idle_record["id"])
        # 重启（进程内等价物：全部卸下，再走开机扫描）。已受理、引擎里仍挂着
        # 的委托把账户标成 busy；空闲账户不被开机装配。
        await manager.shutdown()
        assert application.state.accounts.get(busy_id) is None
        assert application.state.accounts.get(idle_id) is None
        assert manager.account_state(busy_id) == "offline"
        await manager.start_background()
        assert manager.account_state(busy_id) == "connected"
        assert application.state.accounts.get(idle_id) is None
        # 引擎侧账本不受 Runtime 重启影响：挂单还在。
        assert engine.book(busy_id)["orders"][resting["clientOrderId"]]["status"] == "ALIVE"
    finally:
        await close_paper_app(application)


async def test_legacy_user_without_accounts_is_backfilled_on_first_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, engine = paper_app(tmp_path, monkeypatch, "backfill.sqlite3")
    try:
        store = application.state.store
        engine.fail_create = True  # 注册时引擎失联：注册不能被挡下
        friend, friend_id = await register_user(application)
        try:
            assert (await friend.get("/api/auth/owner/status")).json()["bindings"]["paperConfigured"] is True
            assert store.find_user_paper_account(user_id=friend_id) is None
            engine.fail_create = False
            # 老用户（或引擎恢复后）第一次请求：403 补开户路径接上。
            session_id = await open_session(friend)
            account_id = str(store.get_session(session_id)["tradingAccountId"])
            assert account_id.startswith("paper:")
            assert [c for c in engine.creates if c[0] == account_id] == [(account_id, PAPER_INITIAL_BALANCE)]
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


async def test_tqkq_retirement_fully_unbinds_a_member_account_and_moves_its_sessions_to_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快期模拟停用交易 (#1205)：启动一步把不是行情源的成员快期账户整套解绑（停子进程、删口令、删授权、
    停用账户行），默认账户与停在上面的会话都挪到本站模拟；会话先全部改绑、推送再 best-effort（526-C2）——
    一条推送失败不挡其余。第二次启动什么也不做。"""
    application, _engine = unbind_app(tmp_path, monkeypatch, "retire-plain.sqlite3")
    try:
        store = application.state.store
        pool = application.state.account_pool
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test",
            headers={"Origin": LOCAL_ORIGIN, "Authorization": f"Bearer {TOKEN}"},
        ) as client:
            assert (await client.post("/api/auth/local")).status_code == 200
            paper_id = str((await application.state.paper_accounts.ensure_paper_account("local-user"))["id"])
            await bind_tqkq_account(store, pool, user_id="local-user", username=test_account_pool.USERNAME,
                                    password=test_account_pool.PASSWORD, kq_username=test_account_pool.USERNAME)
            tqkq_id = bind_account_id()
            first, second = await open_session(client), await open_session(client)
            assert store.get_session(first)["tradingAccountId"] == tqkq_id
            assert store.get_session(second)["tradingAccountId"] == tqkq_id
            real_publish = application.state.broker.publish
            pushes: list[str] = []

            async def flaky_publish(session_id, **kwargs):
                pushes.append(session_id)
                if len(pushes) == 1:
                    raise RuntimeError("push broken")
                return await real_publish(session_id, **kwargs)

            monkeypatch.setattr(application.state.broker, "publish", flaky_publish)
            assert await retire_tqkq_trading(application.state.ctx, market_sources=[]) == [tqkq_id]
            assert store.load_tqkq_credentials(user_id="local-user") is None
            assert store.list_account_grantees(account_id=tqkq_id) == []
            assert store.get_trading_account(tqkq_id)["status"] == "disabled"
            assert application.state.accounts.get(tqkq_id) is None and pool.client.stops == [tqkq_id]
            assert store.get_user_default_account(user_id="local-user") == paper_id
            assert store.get_session(first)["tradingAccountId"] == paper_id
            assert store.get_session(second)["tradingAccountId"] == paper_id
            assert len(pushes) == 2  # 失败的那条只记日志，后面的会话照发
            # 改绑后推给会话的 runtime.status 说的是本站模拟账户（说明字段随合并重算）。
            rebound = [event["payload"] for _, event in store.list_events(second)
                       if event["name"] == "runtime.status"][-1]
            assert rebound["account"]["id"] == paper_id and rebound["accountLabel"] == "本站模拟账户"
            directory = (await client.get("/api/accounts")).json()
            assert tqkq_id not in {item["id"] for item in directory["accounts"]}
            # 旧客户端的绑定/解绑只得到 410。
            assert (await client.post("/api/accounts/unbind-tqkq")).status_code == 410
            assert (await client.post("/api/accounts/bind-tqkq", json={"username": "x", "password": "y"})).status_code == 410
            assert await retire_tqkq_trading(application.state.ctx, market_sources=[]) == []
            assert pool.client.stops == [tqkq_id]
    finally:
        await close_paper_app(application)


async def test_tqkq_retirement_keeps_a_market_source_and_skips_a_busy_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1205：行情源里的成员快期账户留着账户行、托管口令和子进程（行情接力还用它登录），只删交易授权，
    默认账户挪到持有人自己的本站模拟（没有就开一个）；有未了结止损的账户这次跳过、原样不动；
    行情专用（market-source:）账户从不碰。第二次启动只剩那个忙的账户仍被跳过。"""
    application, _engine = unbind_app(tmp_path, monkeypatch, "retire-source.sqlite3")
    store = application.state.store
    pool = application.state.account_pool
    source_id = store_tqkq_pool_account(store, pool, owner_user_id="member-a", username="kq-source", password="pw-a")
    busy_id = store_tqkq_pool_account(store, pool, owner_user_id="member-b", username="kq-busy", password="pw-b")
    market_only = store_tqkq_pool_account(store, pool, owner_user_id=market_source_owner_id("kq-data"),
                                          username="kq-data", password="pw-c")
    store.set_user_default_account(user_id="member-a", account_id=source_id)
    _execution_rows(store, busy_id)
    sources = [source_id, busy_id, market_only]
    store.set_market_sources(sources)
    # The boot runs the step (the lifespan's exit closes the store, so the checks run inside it).
    async with application.router.lifespan_context(application):
        assert store.get_trading_account(source_id)["status"] == "active"
        assert store.load_tqkq_credentials(user_id="member-a")["account_id"] == source_id
        assert store.list_account_grantees(account_id=source_id) == []
        member_paper = store.find_user_paper_account(user_id="member-a")
        assert member_paper is not None
        assert store.get_user_default_account(user_id="member-a") == str(member_paper["id"])
        assert pool.client.stops == []  # the market relay keeps its child
        assert store.list_account_grantees(account_id=busy_id) == ["member-b"]
        assert store.get_trading_account(busy_id)["status"] == "active"
        assert store.list_account_grantees(account_id=market_only) == [market_source_owner_id("kq-data")]
        assert await retire_tqkq_trading(application.state.ctx, market_sources=sources) == []
        assert store.list_account_grantees(account_id=busy_id) == ["member-b"]


async def test_the_tqkq_main_account_places_no_orders_beside_the_paper_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1205：配置了本站模拟盘时静态快期主账号（tqkq-sidecar）不再有下单网关——行情照旧，委托回
    simulation_gateway_disabled；本站模拟账户照常下单。没有本站模拟盘的部署照旧（test_owner_auth 的 tqkqMode）。"""
    application, _engine = paper_app(tmp_path, monkeypatch, "tqkq-no-gateway.sqlite3")
    try:
        assert application.state.accounts.default.simulation_orders.enabled is False
        friend, _ = await register_user(application)
        try:
            session_id = await open_session(friend)
            account_id = str(application.state.store.get_session(session_id)["tradingAccountId"])
            assert (await wait_paper_ready(application, account_id)).simulation_orders.enabled is True
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


async def test_paper_market_reads_delegate_to_the_default_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, engine = paper_app(tmp_path, monkeypatch, "delegate.sqlite3")
    try:
        friend, _ = await register_user(application)
        try:
            session_id = await open_session(friend)
            account_id = str(application.state.store.get_session(session_id)["tradingAccountId"])
            provider = (await wait_paper_ready(application, account_id)).provider
            market = application.state.data_provider
            assert market is not None
            before = len(market.calls)
            # 共享行情读：走默认 provider（一套订阅、一个行情源）。
            provider.instrument_catalog()
            provider.market_board()
            provider.market_quotes((SYMBOL,))
            provider.market_calendar()
            provider.market_workbench(SYMBOL, 60)
            provider.symbol_settlements((SYMBOL,), 3)
            assert set(market.calls[before:]) == {
                "instrument_catalog", "market_board", "market_quotes",
                "market_calendar", "market_workbench", "symbol_settlements"}
            engine_paths = {path for _, path in engine.paths}
            assert not any(part in path for path in engine_paths
                           for part in ("/board", "/calendar", "/workbench", "/settlements", "/instruments"))
            # 盘前校验行情与撮合同源：报价读引擎，不落到默认 provider。
            snapshot = provider.market_snapshot("", SYMBOL)
            assert snapshot["price"] == 824.0
            assert "market_snapshot" not in market.calls
            assert any(path.endswith(f"/market/{SYMBOL}/quote")
                       for _, path in engine.paths)
        finally:
            await friend.aclose()
    finally:
        await close_paper_app(application)


async def test_without_engine_url_registration_and_sessions_behave_as_before(
    tmp_path: Path,
) -> None:
    application = create_app(
        tmp_path / "flag-off.sqlite3", agent_mode="fake", auth_mode="owner-password",
        owner_username="charles", owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET, cors_origins={ORIGIN},
        data_mode="tqkq-sidecar", data_provider=FakeReadOnlyDataProvider(),
        agent_provider="deepseek", registration_enabled=True,
        deepseek_api_key="sk-test-deepseek-key-000001",
    )
    try:
        assert application.state.paper_accounts is None
        friend, friend_id = await register_user(application)
        try:
            # 未配置引擎：注册照常成功、如实告知没有本站模拟；未绑定成员的
            # 第一个请求仍是之前的 403。
            status = (await friend.get("/api/auth/owner/status")).json()
            assert status["bindings"]["paperConfigured"] is False
            assert application.state.store.find_user_paper_account(user_id=friend_id) is None
            refused = await friend.post("/api/sessions", json={"mode": "local", "agentId": "native"})
            assert refused.status_code == 403
            assert refused.json()["detail"] == "tqkq_account_required"
        finally:
            await friend.aclose()
        # 引擎下线前留下的 paper 行：拒绝（503），而不是落到别人的账上。
        second, second_id = await register_user(application, "second")
        try:
            store = application.state.store
            store.ensure_trading_account(
                "paper:orphan000001", execution_owner_user_id=second_id,
                provider_kind="paper", display_name="本站模拟",
                sidecar_url=None, sidecar_token_file=None,
                grants=((second_id, "owner"),))
            store.set_user_default_account(user_id=second_id, account_id="paper:orphan000001")
            refused = await second.post("/api/sessions", json={"mode": "local", "agentId": "native"})
            assert refused.status_code == 503
            assert refused.json()["detail"] == "paper_engine_unavailable"
        finally:
            await second.aclose()
    finally:
        application.state.store.close()


async def test_admin_disabling_a_user_releases_their_paper_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, engine = paper_app(tmp_path, monkeypatch, "disable.sqlite3")
    try:
        store = application.state.store
        manager = application.state.paper_accounts
        friend, friend_id = await register_user(application)
        try:
            session_id = await open_session(friend)
            account_id = str(store.get_session(session_id)["tradingAccountId"])
            await wait_paper_ready(application, account_id)
            assert manager.account_state(account_id) == "connected"
        finally:
            await friend.aclose()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                     base_url="http://test", headers={"Origin": ORIGIN}) as owner:
            login = await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})
            assert login.status_code == 200
            disabled = await owner.put("/api/admin/users/friend", json={"disabled": True})
            assert disabled.status_code == 200
        # 立即卸下；行与引擎账本保留（审计），下次登录在门口就被拒绝。
        assert application.state.accounts.get(account_id) is None
        assert manager.account_state(account_id) == "offline"
        assert str(store.find_user_paper_account(user_id=friend_id)["id"]) == account_id
    finally:
        await close_paper_app(application)


async def test_owner_alone_resets_a_paper_account_with_the_id_typed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重置本站模拟账户 (#1204, owner 2026-10-30): owner-only admin route; the body must repeat the id, only a
    `paper:` account qualifies, and the engine's answer comes back as is."""
    application, engine = paper_app(tmp_path, monkeypatch, "reset.sqlite3")
    try:
        friend, friend_id = await register_user(application)
        account_id = str(application.state.store.find_user_paper_account(user_id=friend_id)["id"])
        path = f"/api/admin/paper-accounts/{account_id}/reset"
        try:
            # A member never reaches it (human-only admin route: hidden, 404).
            assert (await friend.post(path, json={"confirmAccountId": account_id})).status_code == 404
        finally:
            await friend.aclose()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                     base_url="http://test", headers={"Origin": ORIGIN}) as owner:
            assert (await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
            mismatch = await owner.post(path, json={"confirmAccountId": "paper:other"})
            assert mismatch.status_code == 409 and mismatch.json()["detail"]["code"] == "confirm_mismatch"
            unknown = await owner.post("/api/admin/paper-accounts/paper:nope/reset", json={"confirmAccountId": "paper:nope"})
            assert unknown.status_code == 404
            assert engine.resets == []
            reset = await owner.post(path, json={"confirmAccountId": account_id})
        assert reset.status_code == 200 and reset.json()["status"] == "reset"
        assert reset.json()["balance"] == PAPER_INITIAL_BALANCE and engine.resets == [account_id]
    finally:
        await close_paper_app(application)


async def test_engine_client_sends_the_engine_create_contract() -> None:
    """Production 10-03: the Runtime sent `initialBalance`, the engine (A1 contract)
    accepts exactly {accountId, initBalance} and answered 422 — every registration
    and 开通 failed with 503. Pin the request the client actually puts on the wire."""
    seen: list[tuple[str, dict[str, Any], str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content), request.headers.get("Authorization", "")))
        return httpx.Response(200, json={"accountId": "paper:wire0000001", "initBalance": 1_000_000.0})

    real_client = httpx.AsyncClient
    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    paper_accounts_module.httpx.AsyncClient = patched  # type: ignore[assignment]
    try:
        client = paper_accounts_module.PaperEngineClient(ENGINE_URL, SIDECAR_TOKEN)
        await client.create_account("paper:wire0000001", PAPER_INITIAL_BALANCE)
        await client.reset("paper:wire0000001")  # #1204: the reset repeats the id as its confirmation
    finally:
        paper_accounts_module.httpx.AsyncClient = real_client  # type: ignore[assignment]
    assert seen == [("/accounts", {"accountId": "paper:wire0000001", "initBalance": PAPER_INITIAL_BALANCE},
                     f"Bearer {SIDECAR_TOKEN}"),
                    ("/accounts/paper:wire0000001/reset", {"confirmAccountId": "paper:wire0000001"},
                     f"Bearer {SIDECAR_TOKEN}")]


async def test_accepted_resting_order_keeps_the_account_busy_until_it_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#474: an order the engine accepted and still holds (CONFIRMED + ALIVE) is
    working for the account — with everyone away the idle scan keeps it, a
    restart re-assembles it; an unsettled cancel counts too. Once the order
    finishes unwatched, the next scan asks the engine, sees it FINISHED and
    reclaims the account; a restart no longer assembles it."""
    application, engine = paper_app(tmp_path, monkeypatch, "busy-resting.sqlite3")
    try:
        store = application.state.store
        manager = application.state.paper_accounts
        friend, _ = await register_user(application)
        try:
            session_id = await open_session(friend)
            account_id = str(store.get_session(session_id)["tradingAccountId"])
            _, resting = await confirmed_order(friend, session_id, price_intent="passive", key="busy-resting")
            await wait_paper_ready(application, account_id)
        finally:
            await friend.aclose()

        def link() -> tuple[str, str, str]:
            row = store.connection.execute(
                "SELECT submit_phase, cancel_phase, last_remote_status FROM tqkq_sim_order_links"
                " WHERE client_order_id = ?", (resting["clientOrderId"],)).fetchone()
            return row["submit_phase"], row["cancel_phase"], row["last_remote_status"]

        assert link() == ("CONFIRMED", "NONE", "ALIVE")
        assert account_id in store.list_busy_paper_accounts()
        manager.idle_seconds = 0.0  # everyone counts as away
        assert await manager.scan_idle() == []
        assert application.state.accounts.get(account_id) is not None
        await manager.shutdown()
        assert application.state.accounts.get(account_id) is None
        await manager.start_background()
        assert manager.account_state(account_id) == "connected"
        # A cancel whose outcome is not settled keeps the account busy as well.
        with store.lock, store.connection:
            store.connection.execute(
                "UPDATE tqkq_sim_order_links SET last_remote_status = 'FINISHED', cancel_phase = 'UNKNOWN'"
                " WHERE client_order_id = ?", (resting["clientOrderId"],))
        assert account_id in store.list_busy_paper_accounts()
        with store.lock, store.connection:
            store.connection.execute(
                "UPDATE tqkq_sim_order_links SET last_remote_status = 'ALIVE', cancel_phase = 'NONE'"
                " WHERE client_order_id = ?", (resting["clientOrderId"],))
        # The order finishes at the engine while nobody watches.
        engine.book(account_id)["orders"][resting["clientOrderId"]]["status"] = "FINISHED"
        assert await manager.scan_idle() == [account_id]
        assert link() == ("CONFIRMED", "NONE", "FINISHED")
        assert account_id not in store.list_busy_paper_accounts()
        await manager.shutdown()
        await manager.start_background()
        assert application.state.accounts.get(account_id) is None
    finally:
        await close_paper_app(application)


async def test_concurrent_provisioning_across_connections_claims_one_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#475: two managers on two SQLite connections provisioning the same user at
    once agree on one account; a retry after an engine failure resumes the id it
    claimed; another user provisioning at the same time gets their own."""
    application, engine = paper_app(tmp_path, monkeypatch, "claims.sqlite3")
    second_store = RuntimeStore(tmp_path / "claims.sqlite3")
    try:
        store = application.state.store
        manager = application.state.paper_accounts
        second = PaperAccountManager(
            client=FakePaperClient(engine), registry=application.state.accounts, store=second_store,
            services=manager.services, settings=manager.settings, runtime_feed=manager.runtime_feed,
            status_for_session=lambda _: {},
        )
        attempts: list[str] = []
        arrived = asyncio.Event()

        class RacingClient(FakePaperClient):
            async def create_account(self, account_id: str, initial_balance: float) -> dict[str, Any]:
                attempts.append(account_id)
                if len(attempts) >= 2:
                    arrived.set()
                # Both calls are inside the engine before either returns.
                await asyncio.wait_for(arrived.wait(), 5.0)
                return await super().create_account(account_id, initial_balance)

        manager.client = RacingClient(engine)
        second.client = RacingClient(engine)
        first, other = await asyncio.gather(
            manager.ensure_paper_account("racer", make_default=False),
            second.ensure_paper_account("racer", make_default=False),
        )
        assert first["id"] == other["id"]
        assert attempts == [first["id"], first["id"]]  # the engine's create is idempotent per id
        owned = store.connection.execute(
            "SELECT id FROM trading_accounts WHERE provider_kind = 'paper' AND execution_owner_user_id = ?",
            ("racer",)).fetchall()
        assert [row["id"] for row in owned] == [first["id"]]
        assert [grant["account_id"] for grant in store.list_account_grants(user_id="racer")] == [first["id"]]
        assert store.get_user_default_account(user_id="racer") is None
        # Different users racing stay apart.
        attempts.clear()
        arrived.clear()
        mine, theirs = await asyncio.gather(
            manager.ensure_paper_account("left"), second.ensure_paper_account("right"))
        assert mine["id"] != theirs["id"]
        assert store.get_user_default_account(user_id="left") == mine["id"]
        assert store.get_user_default_account(user_id="right") == theirs["id"]
        # An engine failure leaves the claim; the retry resumes the same id.
        manager.client = FakePaperClient(engine)
        second.client = FakePaperClient(engine)
        claimed: list[str] = []
        real_create = engine.create

        def recording_create(account_id: str, initial_balance: float) -> dict[str, Any]:
            claimed.append(account_id)
            return real_create(account_id, initial_balance)

        monkeypatch.setattr(engine, "create", recording_create)
        engine.fail_create = True
        with pytest.raises(PaperEngineError):
            await manager.ensure_paper_account("retry")
        assert store.find_user_paper_account(user_id="retry") is None
        engine.fail_create = False
        retried = await second.ensure_paper_account("retry")
        assert claimed == [retried["id"], retried["id"]]
    finally:
        second_store.close()
        await close_paper_app(application)


async def test_paper_accounts_from_before_claims_keep_their_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#475 migration: a database written before claims existed claims each
    user's earliest active paper account on the next start; a duplicate a race
    left behind keeps its row and is never handed out."""
    application, engine = paper_app(tmp_path, monkeypatch, "legacy-claims.sqlite3")
    try:
        store = application.state.store
        manager = application.state.paper_accounts
        original = str((await manager.ensure_paper_account("legacy"))["id"])
        store.ensure_trading_account(
            "paper:duplicate000", execution_owner_user_id="legacy", provider_kind="paper",
            display_name="本站模拟", sidecar_url=None, grants=(("legacy", "owner"),))
        with store.lock, store.connection:
            store.connection.execute("DROP TABLE paper_account_claims")
    finally:
        await close_paper_app(application)
    reopened, engine = paper_app(tmp_path, monkeypatch, "legacy-claims.sqlite3")
    try:
        store = reopened.state.store
        assert str(store.find_user_paper_account(user_id="legacy")["id"]) == original
        assert str((await reopened.state.paper_accounts.ensure_paper_account("legacy"))["id"]) == original
        assert engine.creates == []
        assert store.get_trading_account("paper:duplicate000") is not None
    finally:
        await close_paper_app(reopened)


def _execution_rows(store, account_id: str) -> None:
    now = datetime.now(UTC)
    with store.lock, store.connection:
        store.connection.execute(
            """INSERT INTO ai_execution_authorizations
            (id, user_id, session_id, status, symbols_json, permissions_json, max_order_volume,
             max_orders, expires_at, created_at, updated_at, trading_account_id)
            VALUES (?, 'someone', 'session', 'ACTIVE', '[]', '[]', 1, 1, ?, ?, ?, ?)""",
            (f"auth-{account_id}", (now + timedelta(days=1)).isoformat(), now.isoformat(),
             now.isoformat(), account_id))
        store.connection.execute(
            """INSERT INTO ai_stop_orders
            (id, user_id, authorization_id, session_id, symbol, position_direction, position_bucket,
             volume, trigger_price, price_intent, status, close_client_order_id,
             created_at, updated_at, trading_account_id)
            VALUES (?, 'someone', ?, 'session', ?, 'long', 'all', 1, '800', 'last', 'TRIGGERING',
                    'paper-close', ?, ?, ?)""",
            (f"stop-{account_id}", f"auth-{account_id}", SYMBOL, now.isoformat(), now.isoformat(), account_id))


def _execution_state(store, account_id: str) -> tuple[str, str]:
    authorization = store.connection.execute(
        "SELECT status FROM ai_execution_authorizations WHERE id = ?", (f"auth-{account_id}",)).fetchone()
    stop = store.connection.execute(
        "SELECT status FROM ai_stop_orders WHERE id = ?", (f"stop-{account_id}",)).fetchone()
    return authorization["status"], stop["status"]


async def test_assembling_an_account_recovers_only_that_account_on_the_loop_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#481: assembling account B leaves running account A's AI lease and its
    in-flight stop trigger alone; a standby assembly (no loops) writes nothing;
    the restart recovery of A runs when A itself is assembled on the loop side,
    and touches nothing of B."""
    application, _engine = paper_app(tmp_path, monkeypatch, "scoped-recovery.sqlite3")
    try:
        store = application.state.store
        manager = application.state.paper_accounts
        account_a = str((await manager.ensure_paper_account("alice"))["id"])
        account_b = str((await manager.ensure_paper_account("bob"))["id"])
        await manager.ensure(account_a, reason="test")
        _execution_rows(store, account_a)
        await manager.ensure(account_b, reason="test")
        assert _execution_state(store, account_a) == ("ACTIVE", "TRIGGERING")
        _execution_rows(store, account_b)
        # A blue-green standby assembling A read-only never writes recovery.
        await manager.detach(account_a, reason="test")
        manager.may_run_loops = lambda: False
        await manager.ensure(account_a, reason="test")
        assert _execution_state(store, account_a) == ("ACTIVE", "TRIGGERING")
        # The loop side assembling A recovers A only.
        await manager.detach(account_a, reason="test")
        manager.may_run_loops = lambda: True
        await manager.ensure(account_a, reason="test")
        assert _execution_state(store, account_a) == ("EXPIRED", "UNKNOWN")
        assert store.connection.execute(
            "SELECT last_error FROM ai_stop_orders WHERE id = ?", (f"stop-{account_a}",),
        ).fetchone()["last_error"] == "runtime_restarted_during_trigger"
        assert _execution_state(store, account_b) == ("ACTIVE", "TRIGGERING")
    finally:
        await close_paper_app(application)


async def test_cancelled_paper_assembly_leaves_nothing_and_the_retry_starts_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#482: a caller cancelled while the assembly waits on the first ledger read
    leaves no half-built services registered — subscriptions and the provider
    are undone — and the next ensure assembles afresh with every loop running."""
    application, _engine = paper_app(tmp_path, monkeypatch, "cancelled.sqlite3")
    try:
        manager = application.state.paper_accounts
        account_id = str((await manager.ensure_paper_account("carol"))["id"])
        entered = asyncio.Event()
        real_run_once = AccountLedger.run_once
        calls: list[AccountLedger] = []

        async def first_read_hangs(self: AccountLedger) -> dict[str, int]:
            calls.append(self)
            if len(calls) == 1:
                entered.set()
                await asyncio.Event().wait()
            return await real_run_once(self)

        monkeypatch.setattr(AccountLedger, "run_once", first_read_hangs)
        assembling = asyncio.create_task(manager.ensure(account_id, reason="test"))
        await asyncio.wait_for(entered.wait(), 5.0)
        half = application.state.accounts.get(account_id)
        assert half is not None and manager.account_state(account_id) == "connecting"
        assembling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await assembling
        assert application.state.accounts.get(account_id) is None
        assert manager.account_state(account_id) == "offline"
        assert half.simulation_orders.on_change is None
        assert half.provider._http.async_client.is_closed
        services = await manager.ensure(account_id, reason="retry")
        assert services is not half
        assert manager.account_state(account_id) == "connected"
        assert services.account_ledger is not None and services.account_ledger._task is not None
        assert services.autonomy._monitor_task is not None
        assert services.strategy_monitor is not None and services.strategy_monitor._task is not None
        assert services.simulation_orders.on_change is not None
    finally:
        await close_paper_app(application)


async def test_paper_positions_http_degrades_but_execution_stays_closed(tmp_path, monkeypatch):
    application, engine = paper_app(tmp_path, monkeypatch, "positions-outage.sqlite3")
    try:
        client, user_id = await register_user(application)
        async with aclosing(client):
            await open_session(client)
            account_id = str(application.state.store.find_user_paper_account(user_id=user_id)["id"])
            await wait_paper_ready(application, account_id)
            provider = application.state.accounts.get(account_id).provider
            await asyncio.to_thread(application.state.paper_accounts.poller.remove, provider)
            row = {"symbol": SYMBOL, "direction": "long", "volume": 2, "openPrice": 820.0,
                   "lastPrice": 821.0, "unrealizedPnl": 2000.0}
            state = {"/v1/status": {"dataStatus": "unavailable"},
                     "/v1/account": {"dataStatus": "unavailable", "equity": 1002000.0},
                     "/v1/positions": [row]}
            observed = []
            provider.subscribe_snapshot(lambda: observed.append(provider.runtime_status()["accountStatus"]))
            provider._poll_once(state.__getitem__)
            assert observed == ["unavailable"]
            pushed = await application.state.runtime_feed._account_overview(
                account_id, user_id, engine._activity(engine.book(account_id)))
            assert pushed["positions"] == [row]
            assert pushed["positionsStatus"]["dataStatus"] == "unavailable"
            response = await client.get("/api/positions")
            assert response.status_code == 200
            assert response.json()["items"] == [row]
            assert response.json()["dataStatus"] == "unavailable"
            assert response.json()["snapshotAvailable"] is True
            assert response.json()["isCached"] is True
            assert (await client.get("/api/account/summary")).json()["dataStatus"] == "unavailable"
            with pytest.raises(RuntimeError, match="snapshot_unavailable"):
                provider.positions()
            # An engine account error retains only the last known facts, labelled unavailable.
            provider._poll_once(paper_accounts_module.PaperSnapshotPoller._slice(
                {account_id: {"error": "market_source_unavailable", "httpStatus": 503}}, account_id, None))
            failed = await client.get("/api/positions")
            assert failed.status_code == 200 and failed.json()["items"] == [row]
            assert failed.json()["dataStatus"] == "unavailable"
            # Recovery is the original normal projection, with no lost holdings.
            state["/v1/status"] = {"dataStatus": "normal"}
            state["/v1/account"]["dataStatus"] = "normal"
            provider._poll_once(state.__getitem__)
            normal = (await client.get("/api/positions")).json()
            assert normal["items"] == [row] and normal["dataStatus"] == "normal"
            assert normal["isCached"] is False and provider.positions() == [row]
    finally:
        await close_paper_app(application)


async def test_paper_first_positions_waits_then_times_out_without_claiming_empty(tmp_path, monkeypatch):
    application, engine = paper_app(tmp_path, monkeypatch, "positions-first.sqlite3")
    try:
        client, user_id = await register_user(application)
        async with aclosing(client):
            await open_session(client)
            account_id = str(application.state.store.find_user_paper_account(user_id=user_id)["id"])
            await wait_paper_ready(application, account_id)
            provider = application.state.accounts.get(account_id).provider
            await asyncio.to_thread(application.state.paper_accounts.poller.remove, provider)
            with provider._lock:
                provider._positions = None
            provider._first_snapshot.clear()
            original = provider.positions_view
            async def short_wait():
                return await original(wait_seconds=0.03)
            monkeypatch.setattr(provider, "positions_view", short_wait)
            timed_out = await client.get("/api/positions")
            assert timed_out.status_code == 200
            assert timed_out.json()["snapshotAvailable"] is False
            assert timed_out.json()["dataStatus"] == "unavailable" and timed_out.json()["items"] == []
            # A saturated executor must not turn the 2.5 s bound into unbounded queue time.
            async def queued_worker(*args, **kwargs):
                await asyncio.Event().wait()
            with monkeypatch.context() as patch:
                patch.setattr(asyncio, "to_thread", queued_worker)
                queued = await asyncio.wait_for(original(wait_seconds=0.03), 0.5)
                assert queued["snapshotAvailable"] is False
            started = asyncio.Event()
            async def tracked_wait():
                started.set()
                return await original()
            monkeypatch.setattr(provider, "positions_view", tracked_wait)
            pending = asyncio.create_task(client.get("/api/positions"))
            await asyncio.wait_for(started.wait(), 1.0)
            assert not pending.done()
            state = {"/v1/status": {"dataStatus": "normal"},
                     "/v1/account": {"dataStatus": "normal"},
                     "/v1/positions": [{"symbol": SYMBOL, "volume": 1}]}
            provider._poll_once(state.__getitem__)
            arrived = await asyncio.wait_for(pending, 1.0)
            assert arrived.status_code == 200 and arrived.json()["snapshotAvailable"] is True
            assert arrived.json()["items"][0]["volume"] == 1
    finally:
        await close_paper_app(application)


async def test_tqkq_positions_http_still_returns_503_without_snapshot(tmp_path):
    provider = SidecarReadOnlyDataProvider(base_url="http://127.0.0.1:43123", token=SIDECAR_TOKEN, symbols=())
    application = create_app(tmp_path / "tqkq-positions.sqlite3", local_token=TOKEN,
                             agent_mode="fake", data_mode="tqkq-sidecar", data_provider=provider)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test",
                                    headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            response = await client.get("/api/positions")
            assert response.status_code == 503
            assert response.json()["detail"] == "readonly_sidecar_snapshot_unavailable"
    finally:
        await provider._http.close()
        application.state.store.close()

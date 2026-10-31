from __future__ import annotations

import asyncio
import json
from urllib.error import URLError

import pytest

from trader_runtime.data_providers import SidecarReadOnlyDataProvider
from trader_runtime.trader_tools import TraderReadOnlyTools


@pytest.mark.asyncio
async def test_sidecar_provider_copies_only_read_snapshots(monkeypatch: pytest.MonkeyPatch):
    provider = SidecarReadOnlyDataProvider(
        base_url="http://127.0.0.1:45123",
        token="runtime-sidecar-token-with-at-least-32-characters",
        symbols=("SHFE.au2612",),
        poll_interval_seconds=0.01,
    )
    responses = {
        "/v1/status": {
            "dataStatus": "normal", "accountStatus": "normal",
            "marketDataStatus": "normal", "marketSessionStatus": "closed",
            "dataErrorCode": None,
            "readOnly": True, "liveTrading": "disabled",
        },
        "/v1/account": {
            "accountName": "TqKq 模拟账户", "capturedAt": "2026-08-26T08:00:00+00:00",
            "ageSeconds": 0.2, "dataStatus": "normal", "equity": 100000,
            "todayPnl": 120, "todayNetPnl": 120, "floatProfit": 100,
            "positionProfit": 150, "closeProfit": 0, "commission": 30,
            "marginRatio": 10, "positionCount": 1,
            # 本站模拟引擎挂起中的到期结算（#527）：Runtime 原样透传给界面与 AI。
            "expirySettlements": [{"symbol": "SHFE.au2610", "finalDay": "2026-10-15", "status": "estimated",
                                   "statusLabel": "暂估，待核", "since": "2026-10-19",
                                   "estimate": {"price": 822.0, "kind": "last", "day": "2026-10-15"}}],
        },
        "/v1/positions": [{
            "symbol": "SHFE.au2612", "exchangeId": "SHFE", "instrumentId": "au2612",
            "direction": "long", "volume": 1, "lastPrice": 823.5, "unrealizedPnl": 100,
        }],
        "/v1/activity": {
            "schemaVersion": 1, "capturedAt": "2026-08-26T08:00:00+00:00",
            "ageSeconds": 0.2, "dataStatus": "normal", "ordersInitialized": True,
            "orders": [{"clientOrderId": "AIT-order", "symbol": "SHFE.au2612",
                        "status": "ALIVE", "volume": 1, "volumeLeft": 1}],
            "trades": [], "liveTrading": "disabled",
        },
        "/v1/instruments": {
            "schemaVersion": 1, "capturedAt": "2026-08-26T08:00:00+00:00",
            "dataStatus": "normal", "isCached": False,
            "items": [{"symbol": "SHFE.au2612", "exchangeId": "SHFE",
                       "instrumentId": "au2612", "instrumentName": "沪金2612",
                       "productId": "au", "expireRestDays": 90, "isMain": True}],
        },
        "/v1/market/SHFE.au2612": {
            "symbol": "SHFE.au2612", "name": "沪金", "price": 823.5,
            "changePercent": 0.2, "capturedAt": "2026-08-26T08:00:00+00:00",
            "ageSeconds": 0.2, "dataStatus": "normal",
            "marketSessionStatus": "closed",
        },
        "/v1/market/DCE.i2609/quote": {
            "symbol": "DCE.i2609", "name": "铁矿石2609", "price": 815.0,
            "changePercent": -0.4, "capturedAt": "2026-08-26T08:00:00+00:00",
            "quoteDatetime": "2026-08-26 16:00:00.000000",
            "ageSeconds": 0.1, "dataStatus": "normal", "priceTick": 0.5,
            "volumeMultiple": 100, "upperLimit": 900, "lowerLimit": 700,
        },
        "/v1/market/SHFE.au2612/workbench?duration=300": {
            "schemaVersion": 1,
            "durationSeconds": 300,
            "capturedAt": "2026-08-26T08:00:00+00:00",
            "ageSeconds": 0,
            "dataStatus": "normal",
            "isCached": False,
            "liveTrading": "disabled",
            "quote": {"symbol": "SHFE.au2612", "lastPrice": 823.5},
            "bars": [
                {
                    "datetime": 1_788_192_000_000_000_000 + index * 300_000_000_000,
                    "open": 820 + index * 0.1,
                    "high": 821 + index * 0.1,
                    "low": 819 + index * 0.1,
                    "close": 820.5 + index * 0.1,
                    "volume": 10 + index,
                    "openInterest": 100 + index,
                }
                for index in range(40)
            ],
        },
    }
    monkeypatch.setattr(provider, "_fetch", lambda path: responses[path])

    await provider.start()
    try:
        for _ in range(100):
            if provider.runtime_status()["accountStatus"] == "normal":
                break
            await asyncio.sleep(0.01)
        status = provider.runtime_status()
        assert status == {
            "accountMode": "tqkq-readonly",
            "marketDataStatus": "normal",
            "marketSessionStatus": "closed",
            "accountStatus": "normal",
            "tradingGatewayStatus": "read_only",
            "riskStatus": "normal",
            "dataErrorCode": None,
            "dataConnectionStage": None,
            "dataInitializationSeconds": None,
            "dataStartupTimings": None,
            "dataStartupTotalSeconds": None,
        }
        assert "token" not in str(status).lower()
        summary = provider.account_summary("turn-1")
        assert summary["equity"] == 100000
        assert [(item["symbol"], item["statusLabel"], item["estimate"]["price"])
                for item in summary["expirySettlements"]] == [("SHFE.au2610", "暂估，待核", 822.0)]
        assert provider.positions()[0]["symbol"] == "SHFE.au2612"
        assert provider.account_activity()["orders"][0]["clientOrderId"] == "AIT-order"
        assert provider.market_snapshot("turn-1", "au2612")["price"] == 823.5
        iron = provider.market_snapshot("turn-2", "DCE.i2609")
        assert iron["price"] == 815.0
        assert iron["priceTick"] == 0.5
        assert iron["trend"] == "down"
        assert provider.instrument_catalog()["items"][0]["isMain"] is True
        workbench = provider.market_workbench("SHFE.au2612", 300)
        assert workbench["quote"]["lastPrice"] == 823.5
        assert workbench["bars"][0]["close"] == 820.5
        tools = TraderReadOnlyTools(
            provider,
            lambda mode: {
                "mode": mode, "adapter": "native-agent",
                "runtimeConnected": True, "agentConnected": True,
                "connected": True, "version": "1.2.5",
                **provider.runtime_status(),
            },
        )
        account_response, _ = tools.call(tool="get_account_summary", arguments={}, turn_id="turn-account", mode="local")
        account_data = json.loads(account_response["contentItems"][0]["text"])["data"]
        assert account_data["expirySettlements"][0]["status"] == "estimated"
        positions_response, _ = tools.call(tool="get_positions", arguments={}, turn_id="turn-positions", mode="local")
        positions_data = json.loads(positions_response["contentItems"][0]["text"])["data"]
        assert positions_data["positions"][0]["lastPrice"] == 823.5  # #803：持仓自己的现价
        decision_response, decision_event = tools.call(
            tool="get_futures_decision_context",
            arguments={"symbol": "SHFE.au2612", "durationSeconds": 300},
            turn_id="turn-decision", mode="local",
        )
        decision = json.loads(decision_response["contentItems"][0]["text"])["data"]
        assert decision_response["success"] is True
        assert decision["analysisReady"] is True
        assert decision["proposalReady"] is True
        assert decision["activity"]["activeOrderCount"] == 1
        assert decision["liveTrading"] == "disabled"
        assert decision_event is None
        assert not hasattr(provider, "insert_order")
        assert not hasattr(provider, "cancel_order")
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_fixture_sidecar_is_reported_as_fake_data(monkeypatch: pytest.MonkeyPatch):
    provider = SidecarReadOnlyDataProvider(
        base_url="http://127.0.0.1:45123",
        token="runtime-sidecar-token-with-at-least-32-characters",
        symbols=("SHFE.au2612",),
        mode="fake",
        poll_interval_seconds=0.01,
    )
    responses = {
        "/v1/status": {"dataStatus": "simulated", "dataErrorCode": None},
        "/v1/account": {"accountName": "Sidecar 合成只读快照", "equity": 0},
        "/v1/positions": [],
        "/v1/market/SHFE.au2612": {
            "symbol": "SHFE.au2612", "name": "沪金（假数据）", "price": 800,
            "changePercent": 0, "dataStatus": "simulated",
        },
    }
    monkeypatch.setattr(provider, "_fetch", lambda path: responses[path])
    await provider.start()
    try:
        for _ in range(100):
            if provider.runtime_status()["accountStatus"] == "simulated":
                break
            await asyncio.sleep(0.01)
        assert provider.runtime_status()["accountMode"] == "fake"
        assert provider.runtime_status()["tradingGatewayStatus"] == "disabled"
        assert "合成假数据" in provider.account_summary("turn-1")["insight"]
    finally:
        await provider.stop()


def test_sidecar_provider_rejects_non_loopback_endpoint():
    with pytest.raises(ValueError, match="sidecar_must_use_localhost"):
        SidecarReadOnlyDataProvider(
            base_url="https://example.com",
            token="runtime-sidecar-token-with-at-least-32-characters",
            symbols=("SHFE.au2612",),
        )


@pytest.mark.asyncio
async def test_sidecar_disconnect_marks_cached_snapshot_unavailable(monkeypatch: pytest.MonkeyPatch):
    provider = SidecarReadOnlyDataProvider(
        base_url="http://127.0.0.1:45123",
        token="runtime-sidecar-token-with-at-least-32-characters",
        symbols=("SHFE.au2612",),
        poll_interval_seconds=0.01,
    )
    disconnected = False
    responses = {
        "/v1/status": {"dataStatus": "normal", "dataErrorCode": None, "ageSeconds": 0.1},
        "/v1/account": {
            "accountName": "TqKq 模拟账户", "capturedAt": "2026-08-26T08:00:00+00:00",
            "ageSeconds": 0.1, "equity": 100000, "todayPnl": 10, "todayNetPnl": 10,
            "floatProfit": 10, "positionProfit": 10, "closeProfit": 0, "commission": 0,
            "marginRatio": 1, "positionCount": 1,
        },
        "/v1/positions": [{
            "symbol": "SHFE.au2612", "exchangeId": "SHFE", "instrumentId": "au2612",
            "direction": "long", "volume": 1, "unrealizedPnl": 10,
        }],
        "/v1/market/SHFE.au2612": {
            "symbol": "SHFE.au2612", "name": "沪金", "price": 823.5,
            "changePercent": 0.2, "capturedAt": "2026-08-26T08:00:00+00:00",
            "ageSeconds": 0.1,
        },
    }

    def fetch(path: str):
        if disconnected:
            raise URLError("sidecar stopped")
        return responses[path]

    monkeypatch.setattr(provider, "_fetch", fetch)
    await provider.start()
    try:
        for _ in range(100):
            if provider.runtime_status()["accountStatus"] == "normal":
                break
            await asyncio.sleep(0.01)
        first_age = provider.account_summary("turn")["ageSeconds"]
        disconnected = True
        for _ in range(100):
            if provider.runtime_status()["accountStatus"] == "unavailable":
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.04)
        status = provider.runtime_status()
        cached = provider.account_summary("turn")
        assert status["accountStatus"] == "unavailable"
        assert status["marketDataStatus"] == "unavailable"
        assert status["riskStatus"] == "unknown"
        assert cached["status"] == "warning"
        assert cached["dataStatus"] == "unavailable"
        assert cached["isCached"] is True
        assert cached["ageSeconds"] > first_age
        with pytest.raises(RuntimeError, match="readonly_sidecar_snapshot_unavailable"):
            provider.positions()
        with pytest.raises(RuntimeError, match="readonly_sidecar_snapshot_unavailable"):
            provider.market_snapshot("turn", "SHFE.au2612")
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_account_snapshot_reads_one_combined_sidecar_fact(monkeypatch: pytest.MonkeyPatch):
    """同源快照 (#700 review): inside provider_read_snapshot(account=True) the trading day /
    positions / activity all come from the ONE combined read — never the poll cache beside
    another request's facts. Outside the context the ordinary reads apply unchanged."""
    from trader_runtime.data_providers import provider_read_snapshot

    provider = SidecarReadOnlyDataProvider(
        base_url="http://127.0.0.1:45123",
        token="runtime-sidecar-token-with-at-least-32-characters",
        symbols=(),
    )
    combined = {
        "schemaVersion": 1,
        "account": {"tradingDay": "2026-09-01", "dataStatus": "normal", "ageSeconds": 0.0,
                    "equity": 100_000, "capturedAt": "2026-09-01T08:00:00+00:00"},
        "positions": [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 2}],
        "activity": {"schemaVersion": 1, "capturedAt": "2026-09-01T08:00:00+00:00",
                     "ageSeconds": 0.0, "dataStatus": "normal", "ordersInitialized": True,
                     "orders": [], "trades": []},
    }

    async def request(method, path, payload=None, *, timeout=None):
        assert str(path).casefold() == "/v1/snapshot"     # 只预取 combined 一条
        return combined

    monkeypatch.setattr(provider, "_request_async", request)
    async with provider_read_snapshot(provider, refresh=True, account=True):
        assert provider.account_summary("turn")["tradingDay"] == "2026-09-01"
        assert [item["symbol"] for item in provider.positions()] == ["SHFE.rb2701"]
        assert provider.account_activity()["ordersInitialized"] is True
    with pytest.raises(RuntimeError, match="readonly_sidecar_snapshot_unavailable"):
        provider.positions()          # 上下文外没有 combined: 普通读法照旧，不混读


@pytest.mark.asyncio
async def test_an_old_sidecar_without_the_combined_read_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """旧服务无 /v1/snapshot (#700 review): the combined prefetch fails — every account read
    inside the snapshot refuses instead of silently pairing a fresh activity with the poll
    cache's positions (the stop-close guard turns this into an explicit card refusal)."""
    from trader_runtime.data_providers import provider_read_snapshot

    provider = SidecarReadOnlyDataProvider(
        base_url="http://127.0.0.1:45123",
        token="runtime-sidecar-token-with-at-least-32-characters",
        symbols=(),
    )

    async def request(method, path, payload=None, *, timeout=None):
        raise RuntimeError("sidecar_route_not_found")     # 旧 sidecar 没有 combined 读

    monkeypatch.setattr(provider, "_request_async", request)
    async with provider_read_snapshot(provider, refresh=True, account=True):
        for read in (provider.positions, provider.account_activity,
                     lambda: provider.account_summary("")):
            with pytest.raises(RuntimeError, match="sidecar_account_snapshot_unavailable"):
                read()


@pytest.mark.asyncio
async def test_nested_snapshot_reads_keep_the_combined_account_context(monkeypatch: pytest.MonkeyPatch):
    """嵌套读 (#700 审查): a read opened INSIDE the account context — a new symbol's
    quote, even with refresh=True — keeps the combined cut. The poll cache's 99-lot
    position must never bleed back in mid-guard, and another provider's context never
    serves this provider's account facts."""
    from trader_runtime.data_providers import provider_read_snapshot

    def build(volume: int, day: str, fill: str | None):
        return SidecarReadOnlyDataProvider(
            base_url="http://127.0.0.1:45123",
            token="runtime-sidecar-token-with-at-least-32-characters",
            symbols=(),
        ), {
            "schemaVersion": 1,
            "account": {"tradingDay": day, "dataStatus": "normal", "ageSeconds": 0.0},
            "positions": [{"symbol": "SHFE.rb2701", "direction": "long", "volume": volume}],
            "activity": {"schemaVersion": 1, "ageSeconds": 0.0, "dataStatus": "normal",
                         "ordersInitialized": True, "orders": [],
                         "trades": ([] if fill is None else
                                    [{"tradeId": fill, "symbol": "SHFE.rb2701", "volume": 1}])},
        }

    provider, combined = build(2, "2026-10-09", "fill-new")
    # 轮询缓存里是一份旧事实：99 手、旧交易日、没有新成交。
    with provider._lock:
        provider._account = {"tradingDay": "2000-01-01", "equity": 12345.0,
                             "capturedAt": "2000-01-01T00:00:00+00:00"}
        provider._positions = [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 99}]
        provider._status["accountStatus"] = "normal"
    requested: list[str] = []

    async def request(method, path, payload=None, *, timeout=None):
        requested.append(str(path))
        if str(path).casefold() == "/v1/snapshot":
            return combined
        if "/quote" in str(path):
            return {"symbol": "SHFE.au2612", "price": 820.0, "dataStatus": "normal",
                    "quoteDatetime": "2026-10-09 09:31:00.000000"}
        raise AssertionError(f"unexpected prefetch: {path}")

    monkeypatch.setattr(provider, "_request_async", request)
    async with provider_read_snapshot(provider, refresh=True, account=True):
        assert [item["volume"] for item in provider.positions()] == [2]
        async with provider_read_snapshot(provider, symbols=("SHFE.au2612",), refresh=True):
            # 显式刷新同时更新账户切面，不退回轮询缓存或只刷新 activity。
            assert [item["volume"] for item in provider.positions()] == [2]
            assert provider.account_activity()["trades"][0]["tradeId"] == "fill-new"
            assert provider.account_summary("")["tradingDay"] == "2026-10-09"
        assert [item["volume"] for item in provider.positions()] == [2]
        assert requested.count("/v1/snapshot") == 2
        assert any("/quote" in path for path in requested)
    assert [item["volume"] for item in provider.positions()] == [99]   # 上下文外: 轮询缓存照旧

    other, other_combined = build(1, "2026-11-02", None)

    async def other_request(method, path, payload=None, *, timeout=None):
        assert str(path).casefold() == "/v1/snapshot"
        return other_combined

    monkeypatch.setattr(other, "_request_async", other_request)
    async with provider_read_snapshot(other, refresh=True, account=True):
        # 另一个 provider 的上下文不串：各自的账户事实各走各的。
        assert [item["volume"] for item in other.positions()] == [1]
        assert [item["volume"] for item in provider.positions()] == [99]


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [False, True])
@pytest.mark.parametrize("refresh", [False, True])
async def test_nested_snapshot_refreshes_existing_market_and_activity(monkeypatch, account, refresh):
    """Sending after signal evaluation must observe new prices and fills when refresh is requested."""
    from trader_runtime.data_providers import provider_read_snapshot

    provider = SidecarReadOnlyDataProvider(
        base_url="http://127.0.0.1:45123", token="test-snapshot-token-with-at-least-32-characters", symbols=())
    provider._status["marketDataStatus"] = "normal"
    revision = 1

    async def request(method, path, payload=None, *, timeout=None):
        activity = {"schemaVersion": 1, "ageSeconds": 0, "dataStatus": "normal",
                    "ordersInitialized": True, "orders": [], "trades": [{"tradeId": str(revision)}]}
        if path == "/v1/activity":
            return activity
        if path == "/v1/snapshot":
            return {"schemaVersion": 1, "activity": activity,
                    "account": {"tradingDay": "2026-10-09", "dataStatus": "normal", "ageSeconds": 0},
                    "positions": [{"volume": revision}]}
        return {"symbol": "SHFE.au2612", "price": 820 + revision,
                "dataStatus": "normal", "ageSeconds": 0}

    monkeypatch.setattr(provider, "_request_async", request)
    async with provider_read_snapshot(provider, symbols=("SHFE.au2612",), account=account):
        revision = 2
        async with provider_read_snapshot(provider, symbols=("SHFE.au2612",), refresh=refresh):
            expected = 2 if refresh else 1
            assert provider.market_snapshot("", "SHFE.au2612")["price"] == 820 + expected
            assert provider.account_activity()["trades"][0]["tradeId"] == str(expected)
            if account:
                assert provider.positions() == [{"volume": expected}]
        assert provider.market_snapshot("", "SHFE.au2612")["price"] == 821
        assert provider.account_activity()["trades"][0]["tradeId"] == "1"


@pytest.mark.asyncio
async def test_real_fixture_sidecar_combined_overrides_a_stale_poll_cache(tmp_path):
    """真实适配 (#700 审查): against the real fixture sidecar over HTTP, the combined
    read inside provider_read_snapshot overrides a stale poll cache (old account /
    99-lot positions / old day); an old sidecar without the route fails the prefetch
    and the account reads refuse — zero fallback to that stale cache."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    from trader_runtime.data_providers import provider_read_snapshot
    from trader_runtime.sidecar_http import SidecarHttpError

    sidecar_root = Path(__file__).resolve().parents[2] / "tqkq-sidecar"
    token = "runtime-sidecar-token-with-at-least-32-characters"
    process = subprocess.Popen(
        [sys.executable, "-m", "tqkq_readonly_sidecar.main", "--config-stdin"],
        cwd=sidecar_root, env={**os.environ, "PYTHONPATH": str(sidecar_root / "src")},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(json.dumps({
            "token": token, "port": 0, "parentPid": os.getpid(),
            "instanceId": "runtime-sidecar-test-instance-with-at-least-32-chars",
            "symbols": ["SHFE.au2612"], "mode": "fixture",
        }) + "\n")
        process.stdin.flush()
        ready = json.loads(process.stdout.readline())
        assert ready["status"] == "ready", ready
        port = int(ready["port"])

        provider = SidecarReadOnlyDataProvider(
            base_url=f"http://127.0.0.1:{port}", token=token, symbols=("SHFE.au2612",),
        )
        # 一份与 sidecar 现值冲突的旧缓存：旧账户、99 手、旧交易日。
        with provider._lock:
            provider._account = {"tradingDay": "2000-01-01", "equity": 12345.0,
                                 "capturedAt": "2000-01-01T00:00:00+00:00"}
            provider._positions = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 99}]
            provider._status["accountStatus"] = "normal"

        async with provider_read_snapshot(provider, refresh=True, account=True):
            assert provider.positions() == []                  # combined 的新事实覆盖旧缓存
            summary = provider.account_summary("")
            assert summary["tradingDay"] != "2000-01-01"
            assert provider.account_activity()["schemaVersion"] == 1

        real_request = provider._request_async

        async def without_combined(method, path, payload=None, *, timeout=None):
            if str(path).casefold() == "/v1/snapshot":
                raise SidecarHttpError("404 route not found")  # 旧 sidecar 没有这个路由
            return await real_request(method, path, payload, timeout=timeout)

        provider._request_async = without_combined            # noqa: B010 — 受控传输层注入
        async with provider_read_snapshot(provider, refresh=True, account=True):
            for read in (provider.positions, provider.account_activity,
                         lambda: provider.account_summary("")):
                with pytest.raises(RuntimeError, match="sidecar_account_snapshot_unavailable"):
                    read()                                     # 零回退: 不落回 99 手旧缓存
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.asyncio
@pytest.mark.timeout(120)   # hang guard only: a real tqsdk engine process boots here, seconds on a loaded runner
async def test_paper_provider_reads_one_combined_fact_over_real_http(tmp_path):
    """Paper 端集成 (#700 审查): the Runtime's real PaperProvider against the real
    engine over HTTP — account A's provider sees only A's facts (its filled position,
    its trade, that day) from ONE combined /v1/snapshot read, and account B's only
    B's (empty) — different accounts never mix. The engine (tqsdk SimTrade) runs in
    a subprocess under the sidecar project's own python: the runtime suite's
    environment deliberately has no tqsdk, mirroring the CI's two venvs."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    from trader_runtime.data_providers import provider_read_snapshot
    from trader_runtime.paper_accounts import PaperProvider

    sidecar_root = Path(__file__).resolve().parents[2] / "tqkq-sidecar"
    token = "paper-test-token-with-at-least-32-characters"
    account_a, account_b = "paper:0000000000a1", "paper:0000000000b2"
    script = tmp_path / "engine.py"
    script.write_text(
        "import json, sys, threading\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from datetime import datetime\n"
        "from tqkq_readonly_sidecar.paper import PaperEngine, PaperServer, VirtualClock\n"
        "from test_paper_book import TRADING_DATES, au_quote, submit_request\n"
        "engine = PaperEngine(db_path=sys.argv[3],\n"
        "                     clock=VirtualClock(datetime.fromisoformat('2026-11-02T09:00:00+08:00')),\n"
        "                     trading_dates=TRADING_DATES)\n"
        "engine.open()\n"
        "engine.start()\n"
        "engine.create_account(sys.argv[5])\n"
        "engine.create_account(sys.argv[6])\n"
        "server = PaperServer(('127.0.0.1', 0), sys.argv[4], engine)\n"
        "threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05},\n"
        "                daemon=True).start()\n"
        "engine.feed_quote(au_quote('09:31:00.000000', 820.0))\n"
        "assert engine.drain(timeout=5.0)\n"
        "report = engine.submit(sys.argv[5], submit_request('order-a'))\n"
        "assert report['order']['status'] == 'FINISHED', report\n"
        "print(json.dumps({'port': int(server.server_address[1])}), flush=True)\n"
        "sys.stdin.readline()          # 稳住引擎直到测试收尾\n"
        "server.shutdown()\n"
        "server.server_close()\n"
        "engine.close()\n",
        encoding="utf-8",
    )
    # 引擎要 tqsdk：runtime 套件的环境故意不带——按项目自己的 python 找（CI 是 sidecar
    # 项目的 .venv，本机是共享 hash 环境经 AI_TRADER_SIDECAR_PYTHON 指定）。
    candidates = [os.environ.get("AI_TRADER_SIDECAR_PYTHON"),
                  str(sidecar_root / ".venv" / "bin" / "python"), sys.executable]
    # find_spec, not `import tqsdk`: the probe must not pay for a second full tqsdk import (seconds on a
    # loaded runner) — only the engine subprocess below imports it.
    has_tqsdk = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('tqsdk') else 1)"
    engine_python = next((candidate for candidate in candidates
                          if candidate and Path(candidate).exists()
                          and subprocess.run([candidate, "-c", has_tqsdk],
                                             capture_output=True).returncode == 0), None)
    if engine_python is None:
        pytest.skip("no python with tqsdk available for the paper engine subprocess")
    process = subprocess.Popen(
        [engine_python, str(script), str(sidecar_root / "src"), str(sidecar_root / "tests"),
         str(tmp_path / "paper.sqlite3"), token, account_a, account_b],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        ready_line = process.stdout.readline()
        assert ready_line, f"the paper engine subprocess exited before it was ready: {process.wait(timeout=5)}"
        port = int(json.loads(ready_line)["port"])

        def provider_for(account_id: str) -> PaperProvider:
            return PaperProvider(
                account_id=account_id, base_url=f"http://127.0.0.1:{port}/accounts/{account_id}",
                token=token, market_source=object(), poller=object(),
            )

        first, second = provider_for(account_a), provider_for(account_b)
        async with provider_read_snapshot(first, refresh=True, account=True):
            assert [(item["symbol"], item["direction"], item["volume"])
                    for item in first.positions()] == [("SHFE.au2612", "long", 1)]
            assert [trade["clientOrderId"] for trade in first.account_activity()["trades"]] \
                == ["order-a"]
            assert first.account_summary("")["tradingDay"] == "2026-11-02"
        async with provider_read_snapshot(second, refresh=True, account=True):
            assert second.positions() == []                   # 不同账户不串
            assert second.account_activity()["trades"] == []
    finally:
        process.stdin.close()
        process.wait(timeout=5)

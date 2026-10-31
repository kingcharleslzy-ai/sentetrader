from __future__ import annotations

import ast
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd
import pytest

import tqkq_readonly_sidecar.tqsdk_backtest as tqsdk_backtest
from tqkq_readonly_sidecar.main import (
    FixtureSession,
    OfficialTqKqSession,
    ReadOnlyServer,
    Snapshot,
    SnapshotService,
    build_instrument_catalog,
    calendar_summary,
    consume_credentials,
    detach_quote,
    market_data_status,
    per_lot_amount,
    process_is_alive,
    quote_age_seconds,
    trading_day_from_calendar,
    trading_day_from_dates,
    trading_schedule_status,
)

PROJECT = Path(__file__).resolve().parents[1]
TOKEN = "sidecar-test-token-with-at-least-32-characters"
INSTANCE_ID = "sidecar-test-instance-with-at-least-32-characters"


def official_calendar(start, end):
    dates = pd.date_range(start, end)
    return {
        "date": dates,
        "trading": [value.weekday() < 5 for value in dates],
    }


def test_authenticated_tqsdk_calls_disable_sensitive_debug_logs():
    sources = [
        PROJECT / "src/tqkq_readonly_sidecar/main.py",
        PROJECT / "src/tqkq_readonly_sidecar/tqsdk_backtest.py",
        PROJECT / "src/tqkq_readonly_sidecar/history.py",
    ]
    calls = [
        node
        for source in sources
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "TqApi"
    ]

    assert len(calls) == 4  # main.py: TqKq trading session and TqSim 行情专用 session
    for call in calls:
        keywords = {item.arg: item.value for item in call.keywords if item.arg}
        assert isinstance(keywords.get("debug"), ast.Constant)
        assert keywords["debug"].value is False
        assert isinstance(keywords.get("disable_print"), ast.Constant)
        assert keywords["disable_print"].value is True


def test_historical_backtest_engine_digest_is_package_safe(monkeypatch):
    monkeypatch.setattr(
        tqsdk_backtest, "__file__", "/missing/tqsdk_backtest.py",
    )

    first = tqsdk_backtest._engine_digest()

    assert len(first) == 64
    assert first == tqsdk_backtest._engine_digest()


def test_historical_backtest_signal_uses_the_completed_bar():
    klines = pd.DataFrame({
        "datetime": [1_777_500_000_000_000_000 + index for index in range(21)],
        "open": [100.0 + index for index in range(21)],
        "high": [101.0 + index for index in range(21)],
        "low": [99.0 + index for index in range(21)],
        "close": [100.0 + index for index in range(21)],
        "volume": [100.0] * 19 + [200.0, 0.0],
        "close_oi": [8_000.0 + index for index in range(21)],
    })
    klines.loc[20, ["open", "high", "low", "close"]] = 1.0

    def moving_average(frame, period):
        return pd.DataFrame({"ma": frame["close"].rolling(period).mean()})

    completed, ready = tqsdk_backtest._completed_signal(
        klines, 1.0, moving_average,
    )

    assert completed["datetime"] == int(klines.iloc[-2]["datetime"])
    assert completed["volume"] == 200
    assert ready is True


def test_history_artifact_request_reuses_one_tqsdk_replay(monkeypatch):
    calls = []

    def run_variant(**kwargs):
        calls.append(kwargs)
        return ({
            "returnPercent": 0.0,
            "maxDrawdownPercent": 0.0,
            "signalCount": 0,
            "winRatePercent": 0.0,
            "tradeCount": 0,
            "commission": 0.0,
            "contractMultiplier": 10.0,
            "priceTick": 1.0,
            "curve": [1_000_000.0],
        }, [{
            "datetime": 1_700_000_000_000_000_000,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10,
            "openInterest": 100,
        }])

    monkeypatch.setattr(tqsdk_backtest, "_run_variant", run_variant)

    result = tqsdk_backtest.run_tqsdk_backtest({
        "symbol": "SHFE.rb2610",
        "startDate": "2026-08-01",
        "endDate": "2026-08-02",
        "durationSeconds": 300,
        "baselineThreshold": 1.0,
        "candidateThreshold": 1.0,
        "initialBalance": 1_000_000.0,
    }, "user", "password")

    assert len(calls) == 1
    assert result["baseline"] == result["candidate"]


def test_historical_backtest_survives_live_snapshot_failure(monkeypatch):
    def fail_session(*_args):
        raise RuntimeError("snapshot_actor_failed")

    observed: dict[str, object] = {}

    def run_backtest(request, username, password):
        observed.update({"username": username, "password": password})
        return SnapshotService._fixture_backtest(request)

    monkeypatch.setattr(tqsdk_backtest, "run_tqsdk_backtest", run_backtest)
    service = SnapshotService(
        username="history-user", password="history-password",
        symbols=["SHFE.au2612"], fixture=False,
        session_factory=fail_session,
    )
    server = ReadOnlyServer(("127.0.0.1", 0), TOKEN, service)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    service.start()
    server_thread.start()
    try:
        deadline = time.monotonic() + 1
        while (
            service.status()["dataStatus"] != "unavailable"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        with request(
            int(server.server_address[1]), "/v1/backtests/tqsdk",
            method="POST", payload={
                "symbol": "SHFE.au2612",
                "startDate": "2026-06-01",
                "endDate": "2026-08-30",
                "durationSeconds": 3600,
                "baselineThreshold": 1.0,
                "candidateThreshold": 0.9,
                "initialBalance": 1_000_000.0,
            },
        ) as response:
            result = json.load(response)
        assert service.status()["dataStatus"] == "unavailable"
        assert result["accountType"] == "TqSim"
        assert observed == {
            "username": "history-user", "password": "history-password",
        }
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)
        service.stop()


def request(
    port: int, path: str, *, token: str | None = TOKEN, method: str = "GET",
    payload: dict | None = None,
):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = json.dumps(payload).encode() if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    return urlopen(Request(
        f"http://127.0.0.1:{port}{path}", headers=headers, method=method, data=data,
    ), timeout=5)


def start_fixture():
    process = subprocess.Popen(
        [sys.executable, "-m", "tqkq_readonly_sidecar.main", "--config-stdin"],
        cwd=PROJECT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps({
        "token": TOKEN,
        "port": 0,
        "parentPid": os.getpid(),
        "instanceId": INSTANCE_ID,
        "symbols": ["SHFE.au2612", "SHFE.rb2610"],
        "mode": "fixture",
    }) + "\n")
    process.stdin.flush()
    assert process.stdout is not None
    ready = json.loads(process.stdout.readline())
    assert ready["status"] == "ready"
    assert ready["instanceId"] == INSTANCE_ID
    assert ready["liveTrading"] == "disabled"
    port = int(ready["port"])
    assert port > 0
    for _ in range(30):
        try:
            with request(port, "/healthz") as response:
                if json.load(response)["hasSnapshot"]:
                    break
        except OSError:
            pass
        time.sleep(0.05)
    return process, port


def stop(process: subprocess.Popen[str]) -> None:
    process.terminate()
    process.wait(timeout=5)


def test_fixture_sidecar_exposes_only_authenticated_read_data():
    process, port = start_fixture()
    try:
        with request(port, "/v1/status") as response:
            status = json.load(response)
        assert status["readOnly"] is True
        assert status["readOnlyData"] is True
        assert status["simulationTrading"] is False
        assert status["liveTrading"] == "disabled"
        assert status["capabilities"] == [
            "account.read", "positions.read", "orders.read", "trades.read",
            "market.read", "market.series.read", "instruments.read", "backtest.tqsdk",
            "settlements.read", "cont_history.read", "rankings.read",
        ]
        assert status["instrumentCatalogStatus"] == "ready"

        with request(port, "/v1/account") as response:
            account = json.load(response)
        assert account["accountName"] == "Sidecar 合成只读快照"
        assert account["equity"] == 0
        assert account["staticBalance"] == 0

        with request(port, "/v1/activity") as response:
            activity = json.load(response)
        assert activity["ordersInitialized"] is True

        with request(
            port,
            "/v1/backtests/tqsdk",
            method="POST",
            payload={
                "symbol": "SHFE.rb2610",
                "startDate": "2026-08-01",
                "endDate": "2026-08-28",
                "durationSeconds": 3600,
                "baselineThreshold": "1.00",
                "candidateThreshold": "0.90",
                "initialBalance": 1_000_000,
            },
        ) as response:
            backtest = json.load(response)
        assert backtest["accountType"] == "TqSim"
        assert backtest["executionEnabled"] is False
        assert backtest["orderCapabilities"] == []
        assert backtest["dataPointCount"] == 30
        assert backtest["candidate"]["returnPercent"] == 1.8
        assert activity["orders"] == []
        assert activity["trades"] == []
        assert activity["liveTrading"] == "disabled"

        with request(port, "/v1/market/SHFE.au2612") as response:
            assert json.load(response)["symbol"] == "SHFE.au2612"

        with request(port, "/v1/instruments") as response:
            catalog = json.load(response)
        with request(port, "/v1/instruments") as response:
            catalog_again = json.load(response)
        assert catalog["schemaVersion"] == 1
        assert catalog["dataStatus"] == "simulated"
        assert catalog_again["capturedAt"] == catalog["capturedAt"]
        assert {item["productId"] for item in catalog["items"]} >= {"au", "rb", "i", "IF"}

        with request(port, "/v1/market/DCE.i2609/workbench?duration=300") as response:
            workbench = json.load(response)
        assert workbench["schemaVersion"] == 1
        assert workbench["quote"]["symbol"] == "DCE.i2609"
        assert workbench["durationSeconds"] == 300
        assert len(workbench["bars"]) == 80
        assert workbench["dataStatus"] == "simulated"
        assert workbench["liveTrading"] == "disabled"

        with request(port, "/v1/market/DCE.i2609/history?duration=60&length=500") as response:
            history = json.load(response)
        assert history["schemaVersion"] == 1 and len(history["bars"]) == 500 and history["priceTick"] == 0.5
        assert history["liveTrading"] == "disabled"

        with request(port, "/v1/market/DCE.i2609/quote") as response:
            dynamic_quote = json.load(response)
        assert dynamic_quote["symbol"] == "DCE.i2609"
        assert dynamic_quote["priceTick"] == 0.5
        assert dynamic_quote["dataStatus"] == "simulated"
        assert dynamic_quote["tradingStatus"] == "CONTINOUS"
        assert dynamic_quote["tradingStatusLabel"] == "连续交易"
        assert dynamic_quote["openLimit"] == 0
        # 五档盘口整档透出，价格按 priceTick 逐档外推、挂量递减。
        assert dynamic_quote["bidPrice5"] == 3500.0 - 0.5 * 5
        assert dynamic_quote["askPrice5"] == 3500.0 + 0.5 * 5
        assert [dynamic_quote[f"bidVolume{level}"] for level in range(1, 6)] == [12, 10, 8, 6, 4]
        assert [dynamic_quote[f"askVolume{level}"] for level in range(1, 6)] == [9, 7, 5, 3, 2]
        assert workbench["quote"]["bidPrice5"] is not None
        assert workbench["quote"]["askVolume5"] == 2

        with request(port, "/v1/settlements?symbols=SHFE.rb2610&days=3") as response:
            settlements = json.load(response)
        assert settlements["schemaVersion"] == 1
        assert settlements["days"] == 3
        assert len(settlements["items"]) == 3
        row = settlements["items"][-1]
        assert row["symbol"] == "SHFE.rb2610"
        assert row["settlement"] > 0
        assert settlements["asOf"] == max(item["tradingDay"] for item in settlements["items"])

        with request(port, "/v1/cont-history?symbols=KQ.m@SHFE.rb&days=20") as response:
            cont_history = json.load(response)
        assert cont_history["schemaVersion"] == 1
        rows = cont_history["symbols"]["KQ.m@SHFE.rb"]
        assert len(rows) == 20
        assert all(item["mainSymbol"].startswith("SHFE.rb") for item in rows)
        assert len(cont_history["rollDates"]["KQ.m@SHFE.rb"]) == 1

        with request(port, "/v1/rankings?symbol=SHFE.rb2610&type=VOLUME&days=2") as response:
            rankings = json.load(response)
        assert rankings["schemaVersion"] == 1
        assert rankings["symbol"] == "SHFE.rb2610"
        assert rankings["rankingType"] == "VOLUME"
        assert len(rankings["rows"]) == 40  # 2 trading days × top-20 brokers
        assert rankings["rows"][0]["rank"] == 1
        assert rankings["rows"][0]["broker"]
        assert rankings["asOf"] == max(item["tradingDay"] for item in rankings["rows"])

        for path in (
            "/v1/settlements?symbols=KQ.m@SHFE.rb&days=1",
            "/v1/settlements?symbols=SHFE.rb2610&days=0",
            "/v1/settlements?symbols=SHFE.rb2610&days=31",
            "/v1/settlements?symbols=",
            "/v1/cont-history?symbols=SHFE.rb2610&days=5",
            "/v1/cont-history?symbols=KQ.m@SHFE.rb&days=201",
            "/v1/rankings?symbol=SHFE.rb2610&type=OPEN&days=1",
            "/v1/rankings?symbol=SHFE.rb2610&type=VOLUME&days=6",
            "/v1/rankings?symbol=",
        ):
            try:
                request(port, path)
                raise AssertionError(f"invalid query unexpectedly succeeded: {path}")
            except HTTPError as error:
                assert error.code == 422, path

        for path in (
            "/v1/settlements?symbols=SHFE.rb9999&days=1",
            "/v1/rankings?symbol=SHFE.rb9999&type=VOLUME&days=1",
        ):
            try:
                request(port, path)
                raise AssertionError(f"unknown symbol unexpectedly succeeded: {path}")
            except HTTPError as error:
                assert error.code == 404, path

        with request(port, "/v1/instruments") as response:
            catalog = json.load(response)
        assert all(item["openLimit"] == 0 for item in catalog["items"])

        try:
            request(port, "/v1/market/DCE.i2609/workbench?duration=7")
            raise AssertionError("unsupported K-line duration unexpectedly succeeded")
        except HTTPError as error:
            assert error.code == 422
        for bad in ("duration=7&length=10", "duration=60&length=10001", "duration=60&length=x"):
            try:
                request(port, f"/v1/market/DCE.i2609/history?{bad}")
                raise AssertionError(f"invalid history request unexpectedly succeeded: {bad}")
            except HTTPError as error:
                assert error.code == 422

        for path in ("/v1/account", "/v1/activity"):
            try:
                request(port, path, token=None)
                raise AssertionError("unauthenticated request unexpectedly succeeded")
            except HTTPError as error:
                assert error.code == 401

        for path in ("/v1/orders", "/v1/cancel", "/v1/liquidate"):
            try:
                request(port, path, method="POST")
                raise AssertionError(f"write route unexpectedly succeeded: {path}")
            except HTTPError as error:
                assert error.code == 405

    finally:
        stop(process)


def test_explicit_simulation_service_exposes_submit_query_and_cancel_once():
    service = SnapshotService(
        username=None,
        password=None,
        symbols=["SHFE.au2612"],
        fixture=True,
        trading_enabled=True,
    )
    server = ReadOnlyServer(("127.0.0.1", 0), TOKEN, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    service.start()
    thread.start()
    port = int(server.server_address[1])
    try:
        deadline = time.monotonic() + 2
        while not service.status()["hasSnapshot"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.status()["simulationTrading"] is True
        submission = {
            "commandId": "submit:fixture-order-1",
            "clientOrderId": "fixture-order-1",
            "symbol": "SHFE.au2612",
            "direction": "BUY",
            "offset": "OPEN",
            "volume": 1,
            "priceType": "LIMIT",
            "limitPrice": 823.0,
        }
        for invalid in (
            {**submission, "offset": "INVALID"},
            {**submission, "limitPrice": 823.11},
            {**submission, "unexpected": True},
        ):
            try:
                request(
                    port, "/v1/simulation/orders/submit", method="POST", payload=invalid
                )
                raise AssertionError("invalid simulation order unexpectedly accepted")
            except HTTPError as error:
                assert error.code == 422
        for method in ("PUT", "PATCH", "DELETE"):
            try:
                request(
                    port, "/v1/simulation/orders/submit", method=method,
                )
                raise AssertionError(f"unsupported method accepted: {method}")
            except HTTPError as error:
                assert error.code == 405
        with request(
            port, "/v1/simulation/orders/submit", method="POST", payload=submission
        ) as response:
            accepted = json.load(response)
        assert accepted["order"]["status"] == "ALIVE"
        assert accepted["order"]["volumeLeft"] == 1
        with request(
            port, "/v1/simulation/orders/submit", method="POST", payload=submission
        ) as response:
            assert json.load(response) == accepted
        with request(port, "/v1/simulation/orders/fixture-order-1") as response:
            assert json.load(response)["order"]["clientOrderId"] == "fixture-order-1"

        close_submission = {
            **submission,
            "commandId": "submit:fixture-close-1",
            "clientOrderId": "fixture-close-1",
            "direction": "SELL",
            "offset": "CLOSETODAY",
        }
        with request(
            port, "/v1/simulation/orders/submit", method="POST",
            payload=close_submission,
        ) as response:
            close_report = json.load(response)
        assert close_report["order"]["direction"] == "SELL"
        assert close_report["order"]["offset"] == "CLOSETODAY"

        dynamic_submission = {
            **submission,
            "commandId": "submit:fixture-iron-order-1",
            "clientOrderId": "fixture-iron-order-1",
            "symbol": "dce.i2609",
            "limitPrice": 3499.5,
        }
        with request(
            port, "/v1/simulation/orders/submit", method="POST",
            payload=dynamic_submission,
        ) as response:
            dynamic_accepted = json.load(response)
        assert dynamic_accepted["order"]["symbol"] == "DCE.i2609"
        assert dynamic_accepted["order"]["status"] == "ALIVE"
        try:
            request(
                port, "/v1/simulation/orders/submit", method="POST",
                payload={
                    **dynamic_submission,
                    "commandId": "submit:fixture-bad-close-1",
                    "clientOrderId": "fixture-bad-close-1",
                    "offset": "CLOSETODAY",
                },
            )
            raise AssertionError("DCE CLOSETODAY unexpectedly accepted")
        except HTTPError as error:
            assert error.code == 422
        with request(
            port,
            "/v1/simulation/orders/fixture-order-1/cancel",
            method="POST",
            payload={"commandId": "cancel:fixture-order-1"},
        ) as response:
            assert json.load(response)["order"]["status"] == "FINISHED"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        service.stop()
        server.server_close()


def test_config_is_received_over_stdin_not_process_arguments():
    source = (PROJECT / "src/tqkq_readonly_sidecar/main.py").read_text(encoding="utf-8")
    assert 'add_argument("--username"' not in source
    assert 'add_argument("--password"' not in source
    assert 'add_argument("--token"' not in source
    assert "sys.stdin.readline" in source


def test_parent_watchdog_can_distinguish_the_desktop_parent_from_a_missing_pid():
    assert process_is_alive(os.getpid()) is True
    assert process_is_alive(2_147_483_647) is False


def test_credentials_are_removed_from_long_lived_config_after_consumption():
    config = {"username": "demo", "password": "secret", "token": TOKEN}
    assert consume_credentials(config) == ("demo", "secret")
    assert "username" not in config
    assert "password" not in config


def test_official_catalog_uses_discovery_metadata_without_trading_calls():
    day = [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]]

    class Frame:
        def to_dict(self, *, orient: str):
            assert orient == "records"
            return [
                {"instrument_id": "SHFE.au2702", "instrument_name": "沪金2702",
                 "exchange_id": "SHFE", "product_id": "au", "expired": float("nan"),
                 "price_tick": 0.02, "volume_multiple": 1000,
                 "delivery_year": 2027, "delivery_month": 2, "expire_rest_days": 150,
                 "trading_time_day": day,
                 "trading_time_night": ["21:00:00-02:30:00"]},   # 连写也规整
                {"instrument_id": "SHFE.au2612", "instrument_name": "沪金2612",
                 "exchange_id": "SHFE", "product_id": "au", "expired": False,
                 "price_tick": 0.02, "volume_multiple": 1000,
                 "delivery_year": 2026, "delivery_month": 12, "expire_rest_days": 90,
                 "trading_time_day": day,
                 "trading_time_night": [["21:00:00", "02:30:00"]]},
                {"instrument_id": "CFFEX.IF2609", "instrument_name": "沪深300股指2609",
                 "exchange_id": "CFFEX", "product_id": "IF", "expired": False,
                 "price_tick": 0.2, "volume_multiple": 300,
                 "delivery_year": 2026, "delivery_month": 9, "expire_rest_days": 20,
                 "trading_time_day": [["09:30:00", "11:30:00"], ["13:00:00", "15:00:00"]],
                 "trading_time_night": None},                    # 无夜盘：None 半场 → []
                {"instrument_id": "SHFE.cu2611", "instrument_name": "沪铜2611",
                 "exchange_id": "SHFE", "product_id": "cu", "expired": False,
                 "price_tick": 10, "volume_multiple": 5,
                 "delivery_year": 2026, "delivery_month": 11, "expire_rest_days": 60,
                 "trading_time_day": "09:00-15:00",              # 不可用形状 → None
                 "trading_time_night": []},
            ]

    class Api:
        calls: list[tuple] = []

        def query_quotes(self, **kwargs):
            self.calls.append(("query_quotes", kwargs))
            return ["SHFE.au2702", "CFFEX.IF2609", "SHFE.au2612", "SHFE.cu2611"]

        def query_cont_quotes(self):
            self.calls.append(("query_cont_quotes",))
            return ["SHFE.au2612", "CFFEX.IF2609"]

        def query_symbol_info(self, symbols):
            self.calls.append(("query_symbol_info", tuple(symbols)))
            return Frame()

    api = Api()
    catalog = build_instrument_catalog(api)

    assert [item["symbol"] for item in catalog] == [
        "CFFEX.IF2609", "SHFE.au2612", "SHFE.au2702", "SHFE.cu2611",
    ]
    assert catalog[0]["hasNight"] is False
    assert catalog[1]["hasNight"] is True
    assert catalog[1]["isMain"] is True
    assert catalog[2]["isMain"] is False
    # 目录自带时段（收盘提醒读目录，不占 K 线订阅）：成对与连写都规整，缺半场为 []，
    # 形状不可用为 None。
    assert catalog[0]["tradingSchedule"] == {
        "day": [["09:30:00", "11:30:00"], ["13:00:00", "15:00:00"]], "night": []}
    assert catalog[1]["tradingSchedule"] == {"day": day, "night": [["21:00:00", "02:30:00"]]}
    assert catalog[2]["tradingSchedule"] == {"day": day, "night": [["21:00:00", "02:30:00"]]}
    assert catalog[3]["tradingSchedule"] is None
    assert api.calls == [
        ("query_quotes", {"ins_class": "FUTURE", "expired": False}),
        ("query_cont_quotes",),
        ("query_symbol_info", ("CFFEX.IF2609", "SHFE.au2612", "SHFE.au2702", "SHFE.cu2611")),
    ]


def test_official_market_workbench_detaches_quote_and_ready_kline_data():
    calls: list[tuple] = []
    serial = pd.DataFrame([
        {
            "datetime": 1_788_192_000_000_000_000,
            "open": 811.0,
            "high": 814.0,
            "low": 810.0,
            "close": 813.0,
            "volume": 25,
            "open_oi": 1000,
            "close_oi": 1008,
        },
        {
            "datetime": 1_788_192_300_000_000_000,
            "open": 813.0,
            "high": 816.0,
            "low": 812.0,
            "close": 815.0,
            "volume": 30,
            "open_oi": 1008,
            "close_oi": 1012,
        },
    ])
    quote = SimpleNamespace(
        instrument_name="铁矿石2609",
        datetime="2026-08-31 12:00:00.000000",
        last_price=815.0,
        pre_close=810.0,
        bid_price1=814.5,
        bid_volume1=12,
        ask_price1=815.0,
        ask_volume1=9,
        open=811.0,
        highest=816.0,
        lowest=808.0,
        volume=1200,
        open_interest=1012,
        price_tick=0.5,
        volume_multiple=100,
        upper_limit=900.0,
        lower_limit=700.0,
    )

    class Api:
        get_trading_calendar = staticmethod(official_calendar)

        def get_kline_serial(self, symbol, duration, *, data_length):
            calls.append(("get_kline_serial", symbol, duration, data_length))
            return serial

        def get_quote(self, symbol):
            calls.append(("get_quote", symbol))
            return quote

        def is_serial_ready(self, value):
            return value is serial

        def wait_update(self, *, deadline):
            raise AssertionError(f"ready serial should not wait: {deadline}")

    session = object.__new__(OfficialTqKqSession)
    session.api = Api()
    session._catalog_symbols = {"DCE.I2609": "DCE.i2609"}
    session._kline_serials = {}
    session.quotes = {}
    session._market_session_status = lambda _symbol: "closed"

    result = session.market_workbench("dce.i2609", 300)

    assert result["quote"]["symbol"] == "DCE.i2609"
    assert result["quote"]["lastPrice"] == 815.0
    assert result["quote"]["marketSessionStatus"] == "closed"
    assert result["bars"][-1] == {
        "datetime": 1_788_192_300_000_000_000,
        "open": 813.0,
        "high": 816.0,
        "low": 812.0,
        "close": 815.0,
        "volume": 30.0,
        "openInterest": 1012.0,
        "tradingDay": "2026-09-01",
    }
    assert calls == [
        ("get_kline_serial", "DCE.i2609", 300, 256),
        ("get_quote", "DCE.i2609"),
    ]
    assert not hasattr(Api, "insert_order")


def test_official_workbench_takes_the_new_periods_and_lengths_the_minute_serial():
    """30 分与周线 (2026-10-02) 直传 TqSdk；1 分钟 serial 加长到 600 根装下一整个交易日（分时图共用），
    其余周期仍 256 根。昨结价随报价透传，分时图的虚线与涨跌幅基线用它。"""
    calls: list[tuple] = []
    serial = pd.DataFrame([
        {"datetime": 1_788_192_000_000_000_000, "open": 811.0, "high": 814.0, "low": 810.0, "close": 813.0,
         "volume": 25, "open_oi": 1000, "close_oi": 1008},
        {"datetime": 1_788_192_300_000_000_000, "open": 813.0, "high": 816.0, "low": 812.0, "close": 815.0,
         "volume": 30, "open_oi": 1008, "close_oi": 1012},
    ])
    quote = SimpleNamespace(
        instrument_name="铁矿石2609", datetime="2026-08-31 12:00:00.000000", last_price=815.0, pre_close=810.0,
        pre_settlement=808.5, bid_price1=814.5, bid_volume1=12, ask_price1=815.0, ask_volume1=9,
        open=811.0, highest=816.0, lowest=808.0, volume=1200, open_interest=1012,
        price_tick=0.5, volume_multiple=100, upper_limit=900.0, lower_limit=700.0,
    )

    class Api:
        get_trading_calendar = staticmethod(official_calendar)

        def get_kline_serial(self, symbol, duration, *, data_length):
            calls.append(("get_kline_serial", symbol, duration, data_length))
            return serial

        def get_quote(self, symbol):
            return quote

        def is_serial_ready(self, value):
            return value is serial

        def wait_update(self, *, deadline):
            raise AssertionError(f"ready serial should not wait: {deadline}")

    session = object.__new__(OfficialTqKqSession)
    session.api = Api()
    session._catalog_symbols = {"DCE.I2609": "DCE.i2609"}
    session._kline_serials = {}
    session.quotes = {}
    session._market_session_status = lambda _symbol: "closed"

    for duration in (60, 1800, 604800):
        result = session.market_workbench("dce.i2609", duration)
        assert result["durationSeconds"] == duration
        assert result["quote"]["preSettlement"] == 808.5
    assert [(symbol, duration, data_length) for _, symbol, duration, data_length in calls] == [
        ("DCE.i2609", 60, 600), ("DCE.i2609", 1800, 256), ("DCE.i2609", 604800, 256)]


class HistoryApi:
    """A data-only TqApi stand-in for the history actor: counts subscriptions, can be told to fail."""

    instances: list = []

    def __init__(self, credentials, *, ready=True, fail=False):
        self.credentials, self.ready, self.fail = credentials, ready, fail
        self.subscriptions: list[tuple] = []
        self.closed = False
        HistoryApi.instances.append(self)

    get_trading_calendar = staticmethod(official_calendar)

    def get_kline_serial(self, symbol, duration, *, data_length):
        if self.fail:
            raise ConnectionError("history transport lost")
        self.subscriptions.append((symbol, duration, data_length))
        return pd.DataFrame([{"datetime": 1_788_192_000_000_000_000 + i * 60_000_000_000, "open": 800.0 + i,
                              "high": 801.0 + i, "low": 799.0 + i, "close": 800.5 + i, "volume": 10,
                              "open_oi": 1, "close_oi": 1} for i in range(5)])

    def get_quote(self, symbol):
        return SimpleNamespace(price_tick=0.5, volume_multiple=100, trading_time=None)

    def is_serial_ready(self, value):
        return self.ready

    def wait_update(self, *, deadline):
        return None

    def close(self):
        self.closed = True


def history_service(**actor):
    from tqkq_readonly_sidecar.history import HistoryActor
    HistoryApi.instances = []
    service = SnapshotService(username="user", password="secret", symbols=["DCE.i2609"])
    factory = actor.pop("factory", lambda credentials: HistoryApi(credentials))
    service.history = HistoryActor(lambda: service._backtest_credentials, api_factory=factory, **actor)
    service._instruments = [{"symbol": "DCE.i2609"}, {"symbol": "SHFE.rb2610"}, {"symbol": "SHFE.au2612"}]
    return service


def test_history_runs_on_its_own_data_only_session_never_on_the_live_one():
    """规则回测取数：独立 history actor，按上限重建；实盘会话的命令队列与订阅计数完全不动。"""
    service = history_service(max_series=2)
    live_series = {("DCE.i2609", 300): object()}
    service._session = SimpleNamespace(_kline_serials=live_series)
    try:
        for symbol in ("dce.i2609", "SHFE.rb2610", "SHFE.au2612"):
            for duration in (60, 300):
                result = service.market_history(symbol, duration, 3)
                assert [bar["open"] for bar in result["bars"]] == [802.0, 803.0, 804.0]
                assert result["priceTick"] == 0.5 and result["bars"][0]["tradingDay"]
        # Nothing reached the live actor's queue and its subscriptions are untouched.
        assert service._commands.empty() and list(live_series) == [("DCE.i2609", 300)]
        # Six (symbol, period) pairs through a cap of two: rebuilt every two, earlier APIs closed.
        assert len(HistoryApi.instances) == 3 and all(api.closed for api in HistoryApi.instances[:2])
        assert HistoryApi.instances[0].subscriptions == [("DCE.i2609", 60, 10_000), ("DCE.i2609", 300, 10_000)]
        assert all(api.credentials == ("user", "secret") for api in HistoryApi.instances)
        # The same pair again reuses the open serial: no new subscription.
        service.market_history("SHFE.au2612", 300, 3)
        assert len(HistoryApi.instances[-1].subscriptions) == 2 and len(HistoryApi.instances) == 3
        with pytest.raises(KeyError):
            service.market_history("SHFE.xx2612", 60, 3)
    finally:
        service.history.close()
    assert HistoryApi.instances[-1].closed


def test_history_waits_for_the_download_then_recycles_when_idle():
    # The first API's serial is still downloading; a reopened one has it at once.
    service = history_service(factory=lambda credentials: HistoryApi(credentials, ready=bool(HistoryApi.instances)),
                              ready_wait_seconds=0.05, idle_seconds=0.2)
    try:
        with pytest.raises(RuntimeError, match="market_series_not_ready"):
            service.market_history("DCE.i2609", 60, 3)
        HistoryApi.instances[0].ready = True
        assert len(service.market_history("DCE.i2609", 60, 3)["bars"]) == 3
        assert HistoryApi.instances[0].subscriptions == [("DCE.i2609", 60, 10_000)]   # subscribed once
        deadline = time.monotonic() + 5
        while not HistoryApi.instances[0].closed and time.monotonic() < deadline:
            time.sleep(0.05)
        assert HistoryApi.instances[0].closed   # idle: closed, reopened on the next request
        service.market_history("DCE.i2609", 60, 3)
        assert len(HistoryApi.instances) == 2
    finally:
        service.history.close()


def test_history_repeat_read_merges_new_sdk_bar_before_snapshot():
    class UpdatingHistoryApi(HistoryApi):
        def __init__(self, credentials):
            super().__init__(credentials)
            self.pending = False
            self.serial = None
            self.quote = None

        def get_kline_serial(self, symbol, duration, *, data_length):
            self.serial = super().get_kline_serial(symbol, duration, data_length=data_length)
            return self.serial

        def get_quote(self, symbol):
            self.quote = super().get_quote(symbol)
            return self.quote

        def wait_update(self, *, deadline):
            if self.pending:
                self.serial.loc[len(self.serial)] = {
                    **self.serial.iloc[-1].to_dict(),
                    "datetime": int(self.serial.iloc[-1]["datetime"]) + 60_000_000_000,
                    "close": 902.0,
                }
                self.quote.price_tick = 1.0
                self.pending = False
                return True
            return False

    service = history_service(factory=UpdatingHistoryApi, idle_seconds=0.3)
    server = ReadOnlyServer(("127.0.0.1", 0), TOKEN, service)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.server_address[1]
    try:
        with request(port, "/v1/market/DCE.i2609/history?duration=60&length=2") as response:
            first = json.load(response)
        api = HistoryApi.instances[0]
        api.pending = True
        deadline = time.monotonic() + 1.0
        while api.pending and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not api.pending  # idle actor advances the SDK even without a read request
        with request(port, "/v1/market/DCE.i2609/history?duration=60&length=2") as response:
            second = json.load(response)
        assert second["bars"][-1]["close"] == 902.0
        assert second["priceTick"] == 1.0 and first["priceTick"] == 0.5
        assert second["bars"][-1]["datetime"] > first["bars"][-1]["datetime"]
        assert len(api.subscriptions) == 1
        started = time.monotonic()
        with request(port, "/v1/market/DCE.i2609/history?duration=60&length=2") as response:
            assert json.load(response)["bars"] == second["bars"]
        assert time.monotonic() - started < 1.0  # ready but quiet: never waits for an 8s download
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)
        service.history.close()
    assert api.closed and service.history._thread is None


def test_a_failing_history_session_fails_only_the_request_and_is_rebuilt():
    attempts = []

    def factory(credentials):
        attempts.append(credentials)
        return HistoryApi(credentials, fail=len(attempts) == 1)

    service = history_service(factory=factory)
    live = ("status", "dataStatus", "marketDataStatus", "dataErrorCode", "marketSeriesCount", "dynamicQuoteCount")
    status = {key: service.status()[key] for key in live}
    try:
        with pytest.raises(RuntimeError, match="history_session_failed"):
            service.market_history("DCE.i2609", 60, 3)
        assert HistoryApi.instances[0].closed and service._commands.empty()
        assert {key: service.status()[key] for key in live} == status   # live data untouched
        assert len(service.market_history("DCE.i2609", 60, 3)["bars"]) == 3   # fresh API on the next request
    finally:
        service.history.close()
    missing = history_service()
    missing._backtest_credentials = ("", "")
    try:
        with pytest.raises(RuntimeError, match="tqkq_credentials_missing"):
            missing.market_history("DCE.i2609", 60, 3)
    finally:
        missing.history.close()


def test_official_dynamic_quote_is_isolated_from_required_snapshot_quotes():
    calls: list[tuple] = []
    quote = SimpleNamespace(
        instrument_name="铁矿石2609",
        datetime="2026-08-31 12:00:00.000000",
        last_price=815.0,
        pre_close=810.0,
        bid_price1=814.5,
        bid_volume1=12,
        ask_price1=815.0,
        ask_volume1=9,
        bid_price2=814.0,
        bid_volume2=10,
        ask_price2=815.5,
        ask_volume2=7,
        # 3–5 档 TqSdk 没给或是 NaN：detach_quote 归一成 None，不外推。
        bid_price3=math.nan,
        ask_price5=math.nan,
        open=811.0,
        highest=816.0,
        lowest=808.0,
        volume=1200,
        open_interest=1012,
        price_tick=0.5,
        volume_multiple=100,
        upper_limit=900.0,
        lower_limit=700.0,
    )

    class Api:
        def get_quote(self, symbol):
            calls.append(("get_quote", symbol))
            return quote

        def wait_update(self, *, deadline):
            raise AssertionError(f"ready quote should not wait: {deadline}")

    session = object.__new__(OfficialTqKqSession)
    session.api = Api()
    session._catalog_symbols = {"DCE.I2609": "DCE.i2609"}
    session.quotes = {"SHFE.au2612": object()}
    session._dynamic_quotes = {}

    result = session.market_quote("dce.i2609")

    assert result["bidPrice2"] == 814.0 and result["bidVolume2"] == 10
    assert result["askPrice2"] == 815.5 and result["askVolume2"] == 7
    assert result["bidPrice3"] is None and result["bidVolume5"] is None
    assert result["askPrice3"] is None and result["askPrice5"] is None

    assert result["symbol"] == "DCE.i2609"
    assert set(session.quotes) == {"SHFE.au2612"}
    assert set(session._dynamic_quotes) == {"DCE.i2609"}
    assert calls == [("get_quote", "DCE.i2609")]


def test_localhost_bind_does_not_depend_on_reverse_dns():
    service = SnapshotService(
        username=None,
        password=None,
        symbols=["SHFE.au2612"],
        fixture=True,
    )
    with patch("socket.getfqdn", side_effect=AssertionError("DNS must not be used")):
        server = ReadOnlyServer(("127.0.0.1", 0), TOKEN, service)
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert int(server.server_address[1]) > 0
        assert server.server_name == "127.0.0.1"
    finally:
        server.server_close()


def test_official_snapshot_waits_until_every_displayed_account_field_is_ready():
    stages: list[str] = []
    session = object.__new__(OfficialTqKqSession)
    session._on_stage = stages.append
    session.api = SimpleNamespace(wait_update=lambda deadline: True)
    session.account = SimpleNamespace(
        balance=1_000_000.0,
        static_balance=math.nan,
        available=math.nan,
        float_profit=math.nan,
        position_profit=math.nan,
        close_profit=math.nan,
        commission=math.nan,
        margin=math.nan,
    )
    session.position_entities = {}
    session._trading_statuses = {
        "SHFE.au2612": SimpleNamespace(trade_status="NOTRADING"),
    }
    session.quotes = {
        "SHFE.au2612": SimpleNamespace(
            last_price=823.5,
            datetime="2026-08-28 09:00:00.000000",
            pre_close=820.0,
            pre_settlement=819.0,
            instrument_name="沪金2612",
            bid_price1=823.0,
            ask_price1=824.0,
            price_tick=0.5,
            volume_multiple=1000,
            upper_limit=1000.0,
            lower_limit=600.0,
        )
    }

    assert session.read(1.0) is None
    assert stages[-1] == "waiting_account"


def test_official_snapshot_uses_complete_account_values_without_fallbacks():
    stages: list[str] = []
    session = object.__new__(OfficialTqKqSession)
    # Snapshot-only fixture: the background catalog task is already scheduled.
    session._catalog_task = object()
    session._on_stage = stages.append
    session.api = SimpleNamespace(
        wait_update=lambda deadline: False,
        get_trading_calendar=official_calendar,
    )
    session._initial_snapshot_pending = True
    session.orders_initialized = False
    session.orders = {"history-order": object()}
    session._observed_order_ids = set()
    session.account = SimpleNamespace(
        balance=1_000_000.0,
        static_balance=990_000.0,
        available=800_000.0,
        float_profit=1_000.0,
        position_profit=1_200.0,
        close_profit=300.0,
        commission=50.0,
        margin=200_000.0,
    )
    session.position_entities = {}
    session._trading_statuses = {
        "SHFE.au2612": SimpleNamespace(trade_status="NOTRADING"),
    }
    session.quotes = {
        "SHFE.au2612": SimpleNamespace(
            last_price=823.5,
            datetime="2026-08-28 09:00:00.000000",
            pre_close=820.0,
            pre_settlement=819.0,
            instrument_name="沪金2612",
            bid_price1=823.0,
            ask_price1=824.0,
            price_tick=0.5,
            volume_multiple=1000,
            upper_limit=1000.0,
            lower_limit=600.0,
        )
    }

    snapshot = session.read(0.05)

    assert snapshot is not None
    assert snapshot.account == {
        "balance": 1_000_000.0,
        "staticBalance": 990_000.0,
        "available": 800_000.0,
        "floatProfit": 1_000.0,
        "positionProfit": 1_200.0,
        "closeProfit": 300.0,
        "commission": 50.0,
        "margin": 200_000.0,
    }
    assert session.orders_initialized is True
    assert session._observed_order_ids == {"history-order"}
    assert stages[-1] == "ready"
    assert session._initial_snapshot_pending is False
    assert snapshot.markets["SHFE.au2612"]["marketSessionStatus"] == "closed"
    unchanged = session.read(0.05)
    assert unchanged is not None
    assert unchanged.account == snapshot.account
    assert stages[-1] == "ready"


def test_official_snapshot_distinguishes_network_disconnect_from_no_business_change():
    stages: list[str] = []
    notifications = iter([
        [{"code": 2019112911, "conn_id": "md"}],
        [{"code": 2019112902, "conn_id": "md"}],
    ])
    session = object.__new__(OfficialTqKqSession)
    # Snapshot-only fixture: the background catalog task is already scheduled.
    session._catalog_task = object()
    session._on_stage = stages.append
    session._notifier = SimpleNamespace(get_notifies=lambda: next(notifications))
    session._disconnected_connections = set()
    session.api = SimpleNamespace(
        wait_update=lambda deadline: False,
        get_trading_calendar=official_calendar,
    )
    session._initial_snapshot_pending = False
    session.orders_initialized = True
    session._observed_order_ids = set()
    session.orders = {}
    session.trades = {}
    session.account = SimpleNamespace(
        balance=1_000_000.0, static_balance=1_000_000.0, available=1_000_000.0,
        float_profit=0.0, position_profit=0.0, close_profit=0.0,
        commission=0.0, margin=0.0,
    )
    session.position_entities = {}
    session.quotes = {
        "SHFE.au2612": SimpleNamespace(
            last_price=823.5, datetime="2026-08-28 09:00:00.000000",
            pre_close=820.0, pre_settlement=819.0, instrument_name="沪金2612",
            bid_price1=823.0, ask_price1=824.0, price_tick=0.5,
            volume_multiple=1000, upper_limit=1000.0, lower_limit=600.0,
        )
    }

    assert session.read(0.25) is None
    assert stages[-1] == "reconnecting"
    assert session.read(0.25) is not None
    assert stages[-1] == "ready"


def test_account_summary_reports_the_settled_static_balance():
    """After the 15:15 settlement the day's P&L fields come back zeroed while
    static_balance still holds the settlement base; the summary must carry it so the
    Runtime can persist it with the daily row."""
    service = SnapshotService(username=None, password=None, symbols=["SHFE.au2612"], fixture=True)
    service._status = "normal"
    service._connection_stage = "ready"
    service._snapshot = Snapshot(
        account={"balance": 1035207.0, "staticBalance": 1036930.0, "available": 1035207.0,
                 "floatProfit": 0.0, "positionProfit": 0.0, "closeProfit": 0.0,
                 "commission": 0.0, "margin": 0.0},
        positions=[], orders=[], trades=[], markets={},
        trading_day="2026-09-18", captured_at="2026-09-18T07:00:00+00:00",
    )
    service._captured_monotonic = time.monotonic()

    account = service.account()

    assert account["staticBalance"] == 1036930.0
    assert account["equity"] == 1035207.0
    assert account["todayNetPnl"] == 0.0


def test_account_summary_reports_available_margin_and_risk_ratio():
    """The three money figures a beginner actually asks about: remaining usable
    funds, occupied margin and the risk ratio (margin ÷ equity, percent)."""
    service = SnapshotService(username=None, password=None, symbols=["SHFE.au2612"], fixture=True)
    service._status = "normal"
    service._connection_stage = "ready"
    service._snapshot = Snapshot(
        account={"balance": 1_000_000.0, "staticBalance": 990_000.0, "available": 800_000.0,
                 "floatProfit": 1_000.0, "positionProfit": 1_200.0, "closeProfit": 300.0,
                 "commission": 50.0, "margin": 200_000.0},
        positions=[], orders=[], trades=[], markets={},
        trading_day="2026-09-18", captured_at="2026-09-18T07:00:00+00:00",
    )
    service._captured_monotonic = time.monotonic()

    account = service.account()

    assert account["available"] == 800_000.0
    assert account["margin"] == 200_000.0
    assert account["riskRatio"] == 20.0
    assert account["marginRatio"] == 20.0  # 老客户端仍读同口径旧字段


class _EntityQuote(dict):
    """A Quote-shaped double: tqsdk entities are dicts whose keys are also readable
    as attributes; the contract service's per-lot margin/commission ride only the
    dict channel (they are not Quote class attributes)."""

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


def _quote_entity(margin=82_350.0, commission=24.7, **overrides) -> _EntityQuote:
    fields = {
        "last_price": 823.5, "datetime": "2026-08-28 09:00:00.000000",
        "pre_close": 820.0, "pre_settlement": 819.0, "instrument_name": "沪金2612",
        "bid_price1": 823.0, "ask_price1": 824.0, "price_tick": 0.5,
        "volume_multiple": 1000, "upper_limit": 1000.0, "lower_limit": 600.0,
        "margin": margin, "commission": commission,
    }
    fields.update(overrides)
    return _EntityQuote(fields)


def test_per_lot_amount_reads_the_entity_channel_and_narrows_bad_values():
    quote = _quote_entity()
    assert per_lot_amount(quote, "margin") == 82_350.0
    assert per_lot_amount(quote, "commission") == 24.7
    # NaN / ≤0 / 缺失 / 无 dict 通道（SimpleNamespace 式对象）都归一成 None。
    assert per_lot_amount(_quote_entity(margin=math.nan), "margin") is None
    assert per_lot_amount(_quote_entity(margin=-1.0), "margin") is None
    assert per_lot_amount(_quote_entity(margin=0.0), "margin") is None
    assert per_lot_amount(_quote_entity(commission=None), "commission") is None
    assert per_lot_amount(SimpleNamespace(last_price=823.5), "margin") is None


def test_detach_quote_carries_per_lot_margin_and_commission():
    quote = detach_quote("SHFE.au2612", _quote_entity())
    assert quote["marginPerLot"] == 82_350.0
    assert quote["commissionPerLot"] == 24.7
    narrowed = detach_quote("SHFE.au2612", _quote_entity(margin=math.nan, commission=math.nan))
    assert narrowed["marginPerLot"] is None
    assert narrowed["commissionPerLot"] is None


def test_detach_quote_change_is_against_pre_settlement_like_the_board():
    """#751 B / #806: 沪金 10-12 收盘 905.52, 昨收 905.52 → the old 昨收 basis read 0.00% while the board
    read +0.71% (昨结 899.10). The single quote now uses 昨结算 too; 昨收 only when 昨结算 is missing."""
    quote = detach_quote("SHFE.au2612", _quote_entity(last_price=905.52, pre_close=905.52, pre_settlement=899.10))
    assert round(quote["changePercent"], 2) == 0.71
    no_settlement = detach_quote("SHFE.au2612", _quote_entity(last_price=905.52, pre_close=900.0, pre_settlement=math.nan))
    assert round(no_settlement["changePercent"], 2) == 0.61


def test_snapshot_market_carries_per_lot_margin_and_commission():
    """The polled snapshot path (`/v1/market/{symbol}`) must carry the same
    per-lot figures the display quote does — the Runtime reads this one."""
    session = object.__new__(OfficialTqKqSession)
    session._catalog_task = object()
    session._on_stage = lambda _stage: None
    session.api = SimpleNamespace(
        wait_update=lambda deadline: False,
        get_trading_calendar=official_calendar,
    )
    session._initial_snapshot_pending = False
    session.orders_initialized = True
    session._observed_order_ids = set()
    session.orders = {}
    session.account = SimpleNamespace(
        balance=1_000_000.0, static_balance=990_000.0, available=800_000.0,
        float_profit=1_000.0, position_profit=1_200.0, close_profit=300.0,
        commission=50.0, margin=200_000.0,
    )
    session.position_entities = {}
    session._trading_statuses = {"SHFE.au2612": SimpleNamespace(trade_status="NOTRADING")}
    session.quotes = {"SHFE.au2612": _quote_entity()}

    snapshot = session.read(1.0)

    assert snapshot is not None
    market = snapshot.markets["SHFE.au2612"]
    assert market["marginPerLot"] == 82_350.0
    assert market["commissionPerLot"] == 24.7


def test_closed_market_keeps_latest_quote_current_but_active_old_quote_is_stale():
    assert market_data_status(
        "normal", "closed", "2026-08-28 11:30:00.000000", 15,
    )[0] == "normal"
    assert market_data_status(
        "normal", "trading", "2026-08-28 11:30:00.000000", 15,
    )[0] == "stale"

    service = SnapshotService(
        username=None, password=None, symbols=["SHFE.au2612"], fixture=True,
    )
    service._status = "normal"
    service._connection_stage = "ready"
    service._snapshot = Snapshot(
        account={"balance": 0.0}, positions=[], orders=[], trades=[],
        markets={
            "SHFE.au2612": {
                "symbol": "SHFE.au2612", "name": "沪金2612", "price": 823.5,
                "quoteDatetime": "2026-08-28 11:30:00.000000",
                "marketSessionStatus": "closed",
            }
        },
        trading_day="2026-08-28",
        captured_at="2026-08-28T03:30:00+00:00",
    )
    service._captured_monotonic = time.monotonic()

    status = service.status()
    assert status["accountStatus"] == "normal"
    assert status["marketDataStatus"] == "normal"
    assert status["marketSessionStatus"] == "closed"
    market = service.market("SHFE.au2612")
    assert market["dataStatus"] == "normal"
    assert market["marketSessionStatus"] == "closed"


def _feed_service(markets: dict[str, dict]) -> SnapshotService:
    service = SnapshotService(username=None, password=None, symbols=list(markets), fixture=True)
    service._status = "normal"
    service._connection_stage = "ready"
    service._snapshot = Snapshot(
        account={"balance": 0.0}, positions=[], orders=[], trades=[],
        markets=markets, trading_day="2026-10-08",
        captured_at="2026-10-08T02:00:00+00:00",
    )
    service._captured_monotonic = time.monotonic()
    return service


def _quote_time(seconds_ago: float) -> str:
    china = timezone(timedelta(hours=8))
    moment = datetime.now(china) - timedelta(seconds=seconds_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def _market_item(symbol: str, session: str, seconds_ago: float) -> dict:
    return {
        "symbol": symbol, "name": symbol, "price": 3000.0,
        "quoteDatetime": _quote_time(seconds_ago),
        "marketSessionStatus": session,
    }


def test_quiet_contract_does_not_flag_whole_feed_stale():
    # 2026-10-08 production: near-delivery SHFE.rb2610 sat unchanged for minutes
    # mid-session while au2612/rb2701 ticked every few seconds; the any-stale
    # aggregation showed 行情过期 on every screen.
    service = _feed_service({
        "SHFE.au2612": _market_item("SHFE.au2612", "trading", 3),
        "SHFE.rb2610": _market_item("SHFE.rb2610", "trading", 150),
        "SHFE.rb2701": _market_item("SHFE.rb2701", "trading", 3),
    })
    status = service.status()
    assert status["marketDataStatus"] == "normal"
    assert status["marketSessionStatus"] == "trading"
    # Per-symbol staleness stays visible and still gates that contract's orders.
    assert service.market("SHFE.rb2610")["dataStatus"] == "stale"
    assert service.market("SHFE.au2612")["dataStatus"] == "normal"


def test_feed_stale_when_every_trading_contract_stops_ticking():
    service = _feed_service({
        "SHFE.au2612": _market_item("SHFE.au2612", "trading", 90),
        "SHFE.rb2610": _market_item("SHFE.rb2610", "trading", 150),
        # A closed session with its last quote does not rescue the feed verdict.
        "KQ.m@SHFE.rr": _market_item("KQ.m@SHFE.rr", "closed", 3600),
    })
    assert service.status()["marketDataStatus"] == "stale"


def test_unknown_session_quiet_contract_alone_does_not_flag_feed_stale():
    service = _feed_service({
        "SHFE.au2612": _market_item("SHFE.au2612", "trading", 3),
        "GFEX.si2612": _market_item("GFEX.si2612", "unknown", 3600),
    })
    assert service.status()["marketDataStatus"] == "normal"


def test_only_unknown_session_contracts_cannot_judge_the_feed():
    service = _feed_service({
        "GFEX.si2612": _market_item("GFEX.si2612", "unknown", 3600),
    })
    assert service.status()["marketDataStatus"] == "normal"


def test_night_session_uses_next_official_trading_day_across_weekend():
    trading_dates = {"2026-09-04", "2026-09-07", "2026-09-08"}

    assert trading_day_from_calendar(
        datetime(2026, 9, 4, 14, 30, tzinfo=timezone(timedelta(hours=8))),
        trading_dates,
    ) == "2026-09-04"
    assert trading_day_from_calendar(
        datetime(2026, 9, 4, 21, 0, tzinfo=timezone(timedelta(hours=8))),
        trading_dates,
    ) == "2026-09-07"


def test_trading_day_from_dates_answers_what_the_per_bar_scan_answered():
    """The bisect lookup is the hot-path replacement for the scan-and-membership
    wording it inherited; both must agree on every hour and calendar edge."""
    days = [date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 7),
            date(2026, 12, 31), date(2027, 1, 4)]
    trading_dates = {value.isoformat() for value in days}
    for hour in (0, 9, 17, 18, 23):
        for current in [date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 5),
                        date(2026, 9, 6), date(2026, 9, 7), date(2026, 12, 31),
                        date(2027, 1, 2), date(2027, 1, 4)]:
            moment = datetime(current.year, current.month, current.day, hour,
                              tzinfo=timezone(timedelta(hours=8)))
            if hour < 18 and current in days:
                expected = current.isoformat()
            else:
                later = [value for value in days if value > current]
                if not later:
                    with pytest.raises(RuntimeError, match="trading_calendar_unavailable"):
                        trading_day_from_dates(moment, sorted(days))
                    continue
                expected = later[0].isoformat()
            assert trading_day_from_dates(moment, sorted(days)) == expected
            assert trading_day_from_calendar(moment, trading_dates) == expected


def test_annotate_trading_days_marks_rollover_and_weekend():
    fake = SimpleNamespace(_trading_dates=lambda start, end: {"2026-10-09", "2026-10-12"})
    cn = timezone(timedelta(hours=8))
    stamps = [datetime(2026, 10, 9, 15, 0, tzinfo=cn),
              datetime(2026, 10, 9, 20, 30, tzinfo=cn),
              datetime(2026, 10, 10, 10, 0, tzinfo=cn)]
    bars = [{"datetime": int(moment.timestamp() * 1e9), "close": 960} for moment in stamps]
    annotated = OfficialTqKqSession._annotate_trading_days(fake, bars)
    assert [bar["tradingDay"] for bar in annotated] == ["2026-10-09", "2026-10-12", "2026-10-12"]


def test_annotate_trading_days_parses_the_calendar_once_per_render():
    """Repro of the 2026-10-09 pool incident: the market-data worker re-renders
    every subscribed series on each market update, and the old per-bar
    trading_day_from_calendar re-ran strptime over the whole calendar for every
    bar — 600 bars × 90 dates pinned a full core. One parse per render with a
    bisect per bar stays orders of magnitude under that budget."""
    calendar = set()
    candidate = date(2026, 7, 1)
    while len(calendar) < 90:
        if candidate.weekday() < 5:
            calendar.add(candidate.isoformat())
        candidate += timedelta(days=1)
    fake = SimpleNamespace(_trading_dates=lambda start, end: set(calendar))
    bars = [{"datetime": int((datetime(2026, 10, 9, 9, 0, tzinfo=timezone(timedelta(hours=8)))
                             + timedelta(minutes=offset)).timestamp() * 1e9),
             "close": 960}
            for offset in range(600)]
    began = time.perf_counter()
    annotated = OfficialTqKqSession._annotate_trading_days(fake, bars)
    elapsed = time.perf_counter() - began
    assert len(annotated) == 600
    assert {bar["tradingDay"] for bar in annotated} == {"2026-10-09", "2026-10-12"}
    # The per-bar strptime storm needed seconds for this render; the budget
    # leaves two orders of margin against the fix while still failing on it.
    assert elapsed < 0.5


def test_subsecond_future_quote_is_clamped_but_large_clock_skew_fails_closed():
    near_future = (datetime.now(UTC) + timedelta(milliseconds=500)).isoformat()
    far_future = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()

    assert quote_age_seconds(near_future) == 0.0
    assert market_data_status("normal", "trading", near_future, 15)[0] == "normal"
    assert quote_age_seconds(far_future) < -1.0
    assert market_data_status("normal", "trading", far_future, 15)[0] == "stale"


def test_public_quote_schedule_distinguishes_lunch_and_cross_midnight_sessions():
    schedule = SimpleNamespace(
        day=[["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"],
             ["13:30:00", "15:00:00"]],
        night=[["21:00:00", "26:30:00"]],
    )
    china = timezone(timedelta(hours=8))

    assert trading_schedule_status(
        schedule, datetime(2026, 8, 28, 9, 30, tzinfo=china),
    ) == "trading"
    assert trading_schedule_status(
        schedule, datetime(2026, 8, 28, 11, 35, tzinfo=china),
    ) == "closed"
    assert trading_schedule_status(
        schedule, datetime(2026, 8, 29, 1, 30, tzinfo=china),
    ) == "trading"
    assert trading_schedule_status(
        schedule, datetime(2026, 8, 31, 1, 30, tzinfo=china),
    ) == "closed"
    assert trading_schedule_status(SimpleNamespace(day=[], night=[])) == "unknown"


def national_day_dates() -> set[str]:
    """Weekdays 2026-09-01 … 2026-10-31 minus the 10-01 … 10-07 holiday (示意数据)."""
    days, day = set(), date(2026, 9, 1)
    while day <= date(2026, 10, 31):
        if day.weekday() < 5 and not date(2026, 10, 1) <= day <= date(2026, 10, 7):
            days.add(day.isoformat())
        day += timedelta(days=1)
    return days


def test_the_schedule_fallback_knows_holidays_from_the_exchange_calendar():
    """#516：没有官方交易状态时按星期+时刻回退，国庆日盘时段被判 trading，旧报价标“行情待更新”。
    有交易日历时节假日日盘休市；节前最后一晚、节后首日前一晚都没有夜盘；日历外仍按星期。"""
    schedule = SimpleNamespace(day=[["09:00:00", "10:15:00"], ["13:30:00", "15:00:00"]],
                               night=[["21:00:00", "23:00:00"]])
    china = timezone(timedelta(hours=8))
    dates = national_day_dates()

    def status(*moment: int, calendar: set[str] | None = dates) -> str:
        return trading_schedule_status(schedule, datetime(*moment, tzinfo=china), calendar)

    assert status(2026, 10, 5, 9, 30) == "closed"                      # a holiday weekday
    assert status(2026, 10, 5, 9, 30, calendar=None) == "trading"      # the weekday rule cannot see it
    assert status(2026, 9, 30, 21, 30) == "closed"                     # no night session before the holiday
    assert status(2026, 9, 30, 9, 30) == "trading"
    assert status(2026, 10, 8, 9, 30) == "trading"
    assert status(2026, 10, 8, 21, 30) == "trading"
    assert status(2026, 10, 9, 21, 30) == "trading"                    # Friday night belongs to Monday
    assert status(2026, 11, 3, 9, 30) == "trading"                     # past the calendar: the weekday rule
    assert status(2026, 10, 10, 9, 30) == "closed"                     # a Saturday: closed, not unknown


def test_the_calendar_summary_names_the_day_that_closed_at_1530():
    """#859 / #754：回放“已结束交易日”一条规则——交易日当天 15:30 之后它本身就是最近已收盘的交易日（sidecar
    回放同一时刻放行），18:00 换日后是前一交易日；Runtime 与客户端都读这个字段。"""
    china = timezone(timedelta(hours=8))
    dates = national_day_dates()
    for moment, closed in ((datetime(2026, 10, 13, 15, 29, tzinfo=china), "2026-10-12"),
                           (datetime(2026, 10, 13, 15, 30, tzinfo=china), "2026-10-13"),
                           (datetime(2026, 10, 13, 21, 0, tzinfo=china), "2026-10-13"),
                           (datetime(2026, 10, 3, 16, 0, tzinfo=china), "2026-09-30")):   # a holiday afternoon
        summary = calendar_summary(moment, dates)
        assert summary["latestClosedTradingDay"] == closed, moment


def activity_session():
    session = object.__new__(OfficialTqKqSession)
    # Snapshot-only fixture: the background catalog task is already scheduled.
    session._catalog_task = object()
    session._on_stage = lambda _: None
    session.api = SimpleNamespace(
        wait_update=lambda deadline: True,
        get_trading_calendar=official_calendar,
    )
    session.orders_initialized = True
    session._observed_order_ids = set()
    session.account = SimpleNamespace(
        balance=1_000_000.0, static_balance=1_000_000.0, available=900_000.0,
        float_profit=120.0, position_profit=120.0, close_profit=0.0,
        commission=2.0, margin=100_000.0,
    )
    session.position_entities = {
        "SHFE.au2612": SimpleNamespace(
            pos_long=2, pos_long_today=1, pos_long_his=1,
            volume_long_frozen_today=0, volume_long_frozen_his=1,
            open_price_long=820.0, margin_long=100_000.0, float_profit_long=120.0,
            pos_short=0,
        )
    }
    session.orders = {
        "AIT-order": SimpleNamespace(
            order_id="AIT-order", exchange_order_id="EX-1", exchange_id="SHFE",
            instrument_id="au2612", direction="BUY", offset="OPEN",
            volume_orign=2, volume_left=1, limit_price=820.0, price_type="LIMIT",
            status="ALIVE", insert_date_time=1_788_192_100_000_000_000,
            last_msg="报单成功",
        )
    }
    session.trades = {
        "trade-1": SimpleNamespace(
            trade_id="trade-1", order_id="AIT-order", exchange_trade_id="ET-1",
            exchange_id="SHFE", instrument_id="au2612", direction="BUY",
            offset="OPEN", price=820.0, volume=1,
            trade_date_time=1_788_192_200_000_000_000,
        )
    }
    session.quotes = {
        "SHFE.au2612": SimpleNamespace(
            last_price=821.0, datetime="2026-08-31 09:00:00.000000",
            pre_close=819.0, pre_settlement=819.0, instrument_name="沪金2612",
            bid_price1=820.98, ask_price1=821.0, price_tick=0.02,
            volume_multiple=1000, upper_limit=900.0, lower_limit=700.0,
        )
    }

    return session


def test_official_snapshot_detaches_positions_orders_and_trades():
    session = activity_session()
    snapshot = session.read(1.0)

    assert snapshot is not None
    assert snapshot.positions[0]["todayVolume"] == 1
    assert snapshot.positions[0]["historicalVolume"] == 1
    assert snapshot.positions[0]["frozenHistorical"] == 1
    assert snapshot.orders[0]["filledVolume"] == 1
    assert snapshot.orders[0]["symbol"] == "SHFE.au2612"
    assert snapshot.trades[0]["clientOrderId"] == "AIT-order"
    assert snapshot.trades[0]["tradeId"] == "trade-1"


def test_official_snapshot_preserves_a_legitimate_one_sided_book():
    session = object.__new__(OfficialTqKqSession)
    # Snapshot-only fixture: the background catalog task is already scheduled.
    session._catalog_task = object()
    session._on_stage = lambda _: None
    session.api = SimpleNamespace(
        wait_update=lambda deadline: True,
        get_trading_calendar=official_calendar,
    )
    session.orders_initialized = True
    session.account = SimpleNamespace(
        balance=1_000_000.0, static_balance=990_000.0, available=800_000.0,
        float_profit=0.0, position_profit=0.0, close_profit=0.0,
        commission=0.0, margin=0.0,
    )
    session.position_entities = {}
    session.quotes = {
        "SHFE.au2612": SimpleNamespace(
            last_price=1000.0, datetime="2026-08-29 09:00:00.000000",
            pre_close=900.0, pre_settlement=900.0, instrument_name="沪金2612",
            bid_price1=999.5, ask_price1=math.nan, price_tick=0.5,
            volume_multiple=1000, upper_limit=1000.0, lower_limit=600.0,
        )
    }

    snapshot = session.read(1.0)

    assert snapshot is not None
    assert snapshot.markets["SHFE.au2612"]["bidPrice1"] == 999.5
    assert snapshot.markets["SHFE.au2612"]["askPrice1"] is None


def test_local_prefilled_alive_order_is_not_server_confirmation():
    order = SimpleNamespace(
        order_id="local-order",
        exchange_order_id="",
        exchange_id="SHFE",
        instrument_id="au2612",
        direction="BUY",
        offset="OPEN",
        volume_orign=1,
        volume_left=1,
        limit_price=823.0,
        price_type="LIMIT",
        status="ALIVE",
        is_online=True,
        is_dead=False,
        is_error=False,
        last_msg="",
    )
    assert OfficialTqKqSession._order_report(order) == {
        "ordersInitialized": True,
        "found": False,
    }


def test_official_submit_forwards_the_explicit_close_offset_once():
    captured: dict[str, object] = {}
    order = object()
    session = object.__new__(OfficialTqKqSession)

    def insert_order(**kwargs):
        captured.update(kwargs)
        return order

    session.api = SimpleNamespace(insert_order=insert_order)
    session._wait_for_order = lambda actual: {  # type: ignore[method-assign]
        "ordersInitialized": True, "found": actual is order,
    }
    report = session.submit_order_once({
        "clientOrderId": "AIT-close-once",
        "symbol": "SHFE.au2612",
        "direction": "SELL",
        "offset": "CLOSETODAY",
        "volume": 1,
        "limitPrice": 824.0,
    })

    assert report["found"] is True
    assert captured == {
        "symbol": "SHFE.au2612",
        "direction": "SELL",
        "offset": "CLOSETODAY",
        "volume": 1,
        "limit_price": 824.0,
        "order_id": "AIT-close-once",
    }


def test_order_commands_are_not_consumed_before_history_initializes():
    service = SnapshotService(
        username=None, password=None, symbols=["SHFE.au2612"],
        fixture=True, trading_enabled=True,
    )
    result = service.cancel_order_once("fixture-order", "cancel:fixture-order")
    assert result == {
        "ordersInitialized": False, "found": False,
        "commandStatus": "NOT_DISPATCHED",
    }
    assert service._commands.empty()
    assert service._cancelled_order_ids == set()


def test_initial_snapshot_timeout_fails_closed_with_a_specific_stage():
    class NeverReadySession:
        def __init__(self, stage):
            self.stage = stage

        def read(self, timeout_seconds: float):
            del timeout_seconds
            self.stage("waiting_market")
            return None

        def close(self):
            return None

    service = SnapshotService(
        username="configured",
        password="configured",
        symbols=["SHFE.au2612"],
        initial_snapshot_timeout_seconds=0.02,
        session_factory=lambda username, password, symbols, stage: NeverReadySession(stage),
    )
    service.start()
    try:
        deadline = time.monotonic() + 1
        status = service.status()
        while status["dataStatus"] != "unavailable" and time.monotonic() < deadline:
            time.sleep(0.01)
            status = service.status()
        assert status["dataStatus"] == "unavailable"
        assert status["dataErrorCode"] == "tqkq_initial_snapshot_timeout"
        assert status["dataConnectionStage"] == "failed"
        assert status["hasSnapshot"] is False
    finally:
        service.stop()


@pytest.mark.parametrize("clock,day", [
    ("2026-08-21T14:00:00+08:00", "2026-08-21"),
    ("2026-08-21T21:05:00+08:00", "2026-08-24"),
    ("2026-08-22T01:05:00+08:00", "2026-08-24"),
])
def test_fill_day_uses_trade_time_when_snapshot_is_later(clock, day):
    session = activity_session()
    session.trades["trade-1"].trade_date_time = int(datetime.fromisoformat(clock).timestamp() * 1e9)
    snapshot = session.read(1)
    assert snapshot.trades[0]["tradingDay"] == day
    assert snapshot.trading_day != day


def test_unavailable_fill_calendar_does_not_fabricate_snapshot_day():
    session = activity_session()
    original = session._trading_day
    def bounded_calendar(current):
        if current.year < 2026:
            raise RuntimeError("trading_calendar_unavailable")
        return original(current)
    session._trading_day = bounded_calendar
    session.trades["trade-1"].trade_date_time = 1
    snapshot = session.read(1)
    assert snapshot is not None
    assert snapshot.trades[0]["tradingDay"] is None


@pytest.mark.parametrize("field,value", [("last_price", math.nan), ("datetime", ""), ("upper_limit", math.nan)])
def test_incomplete_quote_does_not_freeze_account_or_activity(field, value):
    session = activity_session()
    symbol = "SHFE.au2612"
    session._market_session_status = lambda _: "closed"
    setattr(session.quotes[symbol], field, value)
    snapshot = session.read(0.01)
    assert snapshot is not None
    assert snapshot.account["balance"] == 1_000_000
    assert snapshot.orders[0]["clientOrderId"] == "AIT-order"
    assert snapshot.trades[0]["tradeId"] == "trade-1"
    assert snapshot.markets[symbol]["price"] is None
    assert field in snapshot.markets[symbol]["missingFields"]
    service = SnapshotService(username=None, password=None, symbols=[symbol], fixture=True)
    service._snapshot, service._status = snapshot, "normal"
    service._captured_monotonic = time.monotonic()
    status = service.status()
    assert status["accountStatus"] == "normal"
    assert status["marketDataStatus"] == "stale"
    assert status["marketSessionStatus"] == "closed"
    assert status["marketErrors"][symbol]["code"] == "market_quote_not_ready"
    assert service.market(symbol)["dataStatus"] == "stale"
    session.account.balance += 100
    assert session.read(0.01).account["balance"] == 1_000_100


def test_retained_workbench_quote_never_enters_order_quote_path():
    session = activity_session()
    symbol = "SHFE.au2612"
    session._catalog_symbols = {symbol.upper(): symbol}
    session._kline_serials = {(symbol, 60): object()}
    session.api.is_serial_ready = lambda _: True
    session._market_session_status = lambda _: "closed"
    bars = [{"datetime": 1_788_192_300_000_000_000, "close": 821.0}]
    with patch("tqkq_readonly_sidecar.main.detach_klines", return_value=bars):
        first = session.market_workbench(symbol, 60)
        session.quotes[symbol].last_price = math.nan
        retained = session.market_workbench(symbol, 60)
        assert retained["quote"]["lastPrice"] == first["quote"]["lastPrice"]
        assert retained["quote"]["quoteDatetime"] == first["quote"]["quoteDatetime"]
        assert retained["quote"]["dataStatus"] == "stale"
        service = SnapshotService(username=None, password=None, symbols=[symbol], fixture=True)
        service._snapshot, service._status = session.read(0.01), "normal"
        service._captured_monotonic = time.monotonic()
        service._session = session  # a serving session; without one a request is refused as sidecar_reconnecting
        service._commands = SimpleNamespace(put=lambda command: command.response.put(retained))
        wire = service.market_workbench(symbol, 60)
        assert wire["dataStatus"] == "stale" and wire["isCached"] is True
        assert wire["marketSessionStatus"] == "closed"
        with pytest.raises(RuntimeError, match="market_quote_not_ready"):
            session.market_quote(symbol)
        session.quotes[symbol].last_price = 822.0
        recovered = session.market_workbench(symbol, 60)
        assert recovered["quote"]["lastPrice"] == 822.0
        assert "dataErrorCode" not in recovered["quote"]
        # Never seen a live quote (no night session before Monday's open): the chart
        # still comes back, priced by its last bar and flagged pre-open; the order
        # quote path still refuses.
        session._display_quotes.clear()
        session.quotes[symbol].last_price = math.nan
        pre_open = session.market_workbench(symbol, 60)
        assert [bar["close"] for bar in pre_open["bars"]] == [821.0]
        assert pre_open["quote"]["lastPrice"] == 821.0
        assert pre_open["quote"]["dataStatus"] == "stale"
        assert pre_open["quote"]["dataErrorCode"] == "market_quote_pre_open"
        assert pre_open["quote"]["quoteDatetime"] == "2026-09-01 00:05:00.000000"
        assert pre_open["quote"]["bidPrice1"] is None and pre_open["quote"]["upperLimit"] is None
        with pytest.raises(RuntimeError, match="market_quote_not_ready"):
            session.market_quote(symbol)
        # The session is on but every bar of today has zero volume: listed, broadcast,
        # never traded today. Same display quote, its own code; a traded bar or a
        # closed session keeps pre-open.
        session._market_session_status = lambda _: "trading"
        quiet = [{"datetime": 1_788_192_300_000_000_000, "close": 821.0, "volume": 0.0, "tradingDay": "2026-09-01"},
                 {"datetime": 1_788_192_360_000_000_000, "close": 821.0, "volume": 0.0, "tradingDay": "2026-09-01"}]
        with patch("tqkq_readonly_sidecar.main.detach_klines", return_value=quiet):
            no_trade = session.market_workbench(symbol, 60)
        assert no_trade["quote"]["dataErrorCode"] == "market_quote_no_trade" and no_trade["quote"]["lastPrice"] == 821.0
        with patch("tqkq_readonly_sidecar.main.detach_klines", return_value=[*quiet[:1], {**quiet[1], "volume": 3.0}]):
            assert session.market_workbench(symbol, 60)["quote"]["dataErrorCode"] == "market_quote_pre_open"
        session._market_session_status = lambda _: "closed"
        with patch("tqkq_readonly_sidecar.main.detach_klines", return_value=quiet):
            assert session.market_workbench(symbol, 60)["quote"]["dataErrorCode"] == "market_quote_pre_open"


def test_subscription_caps_answer_limit_but_keep_the_session_alive():
    session = activity_session()
    configured = "SHFE.au2612"
    others = [f"DCE.x{index}2701" for index in range(60)]
    session._catalog_symbols = {s.upper(): s for s in [configured, *others]}
    session._kline_serials, session._dynamic_quotes = {}, {}
    session.api.get_quote = lambda symbol: SimpleNamespace(last_price=1.0, datetime="2026-09-11 14:59:59.000000", instrument_name=symbol,
        price_tick=1.0, volume_multiple=10.0, pre_close=1.0, trading_time=None)
    session.api.get_kline_serial = lambda symbol, duration, data_length: object()
    session.api.is_serial_ready = lambda _: True
    session._market_session_status = lambda _: "closed"
    session._ensure_trading_status = lambda _: None
    bars = [{"datetime": 1_788_192_300_000_000_000, "close": 821.0}]
    with patch("tqkq_readonly_sidecar.main.detach_klines", return_value=bars), patch("tqkq_readonly_sidecar.main.MAX_MARKET_SERIES", 4):
        for symbol in [configured, *others[:3]]:
            session.market_workbench(symbol, 60)
        assert session.subscriptions_exhausted() is True
        with pytest.raises(RuntimeError, match="market_subscription_limit"):
            session.market_workbench(others[3], 60)
        # Nothing was dropped: the session still serves everything it already holds, quotes included.
        assert session.market_workbench(others[0], 60)["quote"]["symbol"] == others[0]
        assert session.market_quote(configured)["symbol"] == configured
        assert session.subscription_counts() == {"marketSeriesCount": 4, "dynamicQuoteCount": 3}


class StallingSession:
    """A session whose read() blocks for good once told to, like TqSdk wedged in a reconnect."""
    built = []
    fail_next = 0          # constructor failures still to raise (TqKq maintenance, transient login errors)
    slow_start = 0.0       # seconds the constructor takes (a slow cold start)

    def __init__(self, username, password, symbols, on_stage):
        if StallingSession.fail_next > 0:
            StallingSession.fail_next -= 1
            StallingSession.built.append(None)
            raise TimeoutError("tqkq login timed out")
        if StallingSession.slow_start:
            on_stage("waiting_account")
            time.sleep(StallingSession.slow_start)
        self.symbols, self.on_stage = symbols, on_stage
        self.stall = threading.Event()
        self.wedged = threading.Event()  # set once read() is actually stuck
        self.closed = threading.Event()
        self.generation = len(StallingSession.built) + 1
        StallingSession.built.append(self)
        self._fixture = FixtureSession(symbols)

    def read(self, timeout):
        if self.stall.is_set():
            self.wedged.set()
            self.closed.wait(timeout=30)
            return None
        snapshot = self._fixture.read(timeout)
        if snapshot is not None and getattr(self, "closed_market", False):
            for item in snapshot.markets.values():
                item["marketSessionStatus"] = "closed"
        return snapshot

    def instrument_catalog(self):
        return self._fixture.instrument_catalog()

    def market_workbench(self, symbol, duration):
        return self._fixture.market_workbench(symbol, duration)

    def market_quote(self, symbol):
        return self._fixture.market_quote(symbol)

    def subscription_counts(self):
        return {"marketSeriesCount": 0, "dynamicQuoteCount": 0}

    def subscriptions_exhausted(self):
        return getattr(self, "exhausted", False)

    def close(self):
        self.closed.set()


@pytest.fixture(autouse=True)
def fast_supervisor(monkeypatch):
    """The supervisor checks its worker once a second in production; tests check every 50 ms."""
    monkeypatch.setattr("tqkq_readonly_sidecar.main.SESSION_SUPERVISOR_POLL_SECONDS", 0.05)


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_watchdog_replaces_a_wedged_session_after_repeated_command_timeouts():
    StallingSession.built = []
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"], session_factory=StallingSession)
    with patch("tqkq_readonly_sidecar.main.SESSION_STALL_TIMEOUTS", 3):
        service.start()
        try:
            assert _wait_until(lambda: service.status()["hasSnapshot"])
            first = StallingSession.built[0]
            first.stall.set()
            # The actor no longer answers: three timed-out market requests in a row.
            with patch("tqkq_readonly_sidecar.main.queue.Queue.get", side_effect=queue.Empty):
                for _ in range(3):
                    with pytest.raises(RuntimeError, match="market_request_timeout"):
                        service.market_workbench("SHFE.au2612", 60)
            assert _wait_until(lambda: len(StallingSession.built) == 2)
            assert service.status()["sessionRebuilds"][-1]["reason"] == "market_session_stalled"
            assert _wait_until(lambda: first.closed.is_set())
            second = StallingSession.built[1]
            assert _wait_until(lambda: service._session is second and service.status()["dataConnectionStage"] == "ready")
            assert service.market_workbench("SHFE.au2612", 60)["quote"]["symbol"] == "SHFE.au2612"
            assert service.status()["dataStatus"] == "normal"
        finally:
            service.stop()


def test_rebuild_retries_with_backoff_until_the_session_comes_back():
    StallingSession.built, StallingSession.fail_next = [], 0
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"], session_factory=StallingSession)
    with patch("tqkq_readonly_sidecar.main.SESSION_STALL_TIMEOUTS", 1), patch("tqkq_readonly_sidecar.main.SESSION_RETRY_BACKOFF_SECONDS", (0.1, 0.1, 0.1)):
        service.start()
        try:
            assert _wait_until(lambda: service.status()["hasSnapshot"])
            first = StallingSession.built[0]
            first.stall.set()
            StallingSession.fail_next = 2  # the next two logins fail, the third works
            with patch("tqkq_readonly_sidecar.main.queue.Queue.get", side_effect=queue.Empty):
                with pytest.raises(RuntimeError):
                    service.market_workbench("SHFE.au2612", 60)
            assert _wait_until(lambda: len([s for s in StallingSession.built if s is not None]) == 2, timeout=10)
            recovered = [s for s in StallingSession.built if s is not None][1]
            assert _wait_until(lambda: service._session is recovered and service.status()["dataConnectionStage"] == "ready", timeout=10)
            reasons = [item["reason"] for item in service.status()["sessionRebuilds"]]
            assert reasons[0] == "market_session_stalled" and len(reasons) == 3
            assert all(reason.startswith("session_retry:tqkq_connection_timeout") for reason in reasons[1:])
            assert service.status()["dataStatus"] == "normal"
        finally:
            service.stop()


def test_credential_rejection_stops_retrying():
    StallingSession.built, StallingSession.fail_next = [], 0
    def rejecting(username, password, symbols, on_stage):
        raise RuntimeError("用户权限认证失败")
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"], session_factory=rejecting)
    with patch("tqkq_readonly_sidecar.main.SESSION_RETRY_BACKOFF_SECONDS", (0.05,)):
        service.start()
        try:
            assert _wait_until(lambda: service.status()["dataConnectionStage"] == "failed")
            assert _wait_until(lambda: not service._thread.is_alive(), timeout=5)
            assert service.status()["dataErrorCode"] == "tqkq_authentication_failed"
            assert service.status()["sessionRebuilds"] == []
        finally:
            service.stop()


def test_a_slow_rebuild_is_not_mistaken_for_a_second_stall():
    StallingSession.built, StallingSession.fail_next = [], 0
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"], session_factory=StallingSession)
    with patch("tqkq_readonly_sidecar.main.SESSION_STALL_TIMEOUTS", 1), patch("tqkq_readonly_sidecar.main.SESSION_STALL_SECONDS", 0.5), \
            patch("tqkq_readonly_sidecar.main.SESSION_FIRST_SNAPSHOT_SECONDS", 30.0):
        service.start()
        try:
            assert _wait_until(lambda: service.status()["hasSnapshot"])
            StallingSession.built[0].stall.set()
            StallingSession.slow_start = 0.8  # the rebuilt session takes longer than the stall window to come up
            with patch("tqkq_readonly_sidecar.main.queue.Queue.get", side_effect=queue.Empty):
                with pytest.raises(RuntimeError):
                    service.market_workbench("SHFE.au2612", 60)
            assert _wait_until(lambda: len(StallingSession.built) == 2, timeout=10)
            assert _wait_until(lambda: service.status()["dataConnectionStage"] == "ready" and service._session is StallingSession.built[1], timeout=10)
            time.sleep(0.6)  # past the 0.5 s stall window, a dozen supervisor checks
            assert len(StallingSession.built) == 2
            assert [item["reason"] for item in service.status()["sessionRebuilds"]] == ["market_session_stalled"]
        finally:
            StallingSession.slow_start = 0.0
            service.stop()


def test_idle_closed_market_at_the_subscription_cap_rebuilds_the_session():
    StallingSession.built = []
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"], session_factory=StallingSession)
    with patch("tqkq_readonly_sidecar.main.SESSION_IDLE_RESET_SECONDS", 0.2):
        service.start()
        try:
            assert _wait_until(lambda: service.status()["hasSnapshot"])
            first = StallingSession.built[0]
            first.exhausted, first.closed_market = True, True
            # Market closed, cap reached, nobody asking for a while: the session is rebuilt with zero subscriptions.
            assert _wait_until(lambda: len(StallingSession.built) == 2)
            assert service.status()["sessionRebuilds"][-1]["reason"] == "market_subscription_reset"
            assert _wait_until(lambda: service._session is StallingSession.built[1] and service.status()["dataConnectionStage"] == "ready")
        finally:
            service.stop()


def test_one_contract_without_a_quote_does_not_stall_the_feed():
    quoted = {"symbol": "SHFE.rb2610", "price": 3044.0, "marketSessionStatus": "closed", "dataStatus": "normal",
              "quoteDatetime": "2026-09-11 22:59:59.500000"}
    missing = {"symbol": "CZCE.PK611", "price": None, "quoteDatetime": "", "marketSessionStatus": "closed",
               "dataStatus": "stale", "dataErrorCode": "market_quote_not_ready", "missingFields": ["last_price", "datetime"]}
    service = SnapshotService(username=None, password=None, symbols=["SHFE.rb2610", "CZCE.PK611"], fixture=True)
    snapshot = Snapshot({}, [], [], [], {"SHFE.rb2610": quoted, "CZCE.PK611": missing}, "2026-09-14", "2026-09-13T08:00:00+00:00")
    service._snapshot, service._status, service._captured_monotonic = snapshot, "normal", time.monotonic()
    service._connection_stage, service._error_code = "ready", None
    status = service.status()
    assert status["marketDataStatus"] == "normal" and status["dataConnectionStage"] == "ready" and status["dataErrorCode"] is None
    assert status["marketErrors"] == {"CZCE.PK611": {"code": "market_quote_not_ready", "missingFields": ["last_price", "datetime"]}}
    assert status["marketSeriesCount"] == 0 and status["dynamicQuoteCount"] == 0


def test_connection_stage_can_leave_ready_when_account_becomes_incomplete():
    service = SnapshotService(username=None, password=None, symbols=["SHFE.au2612"], fixture=True)
    service._status = "normal"
    service._connection_stage = "ready"
    service._set_stage("waiting_account")
    assert service.status()["dataConnectionStage"] == "waiting_account"


def test_historical_initialization_still_waits_for_its_quote_clock():
    session = activity_session()
    session.historical = True
    session.quotes["SHFE.au2612"].datetime = ""
    session.snapshot_time = lambda: (_ for _ in ()).throw(AssertionError("clock is not ready"))
    assert session.read(0.01) is None


def test_timeouts_during_a_rebuild_do_not_count_against_the_next_session():
    """The 09-14 storm: feed requests kept timing out while the replacement session was still
    logging in, the count was already ≥3 when it came up, and it was replaced at once — 41 times."""
    StallingSession.built, StallingSession.fail_next = [], 0
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"], session_factory=StallingSession)
    with patch("tqkq_readonly_sidecar.main.SESSION_STALL_TIMEOUTS", 3), patch("tqkq_readonly_sidecar.main.SESSION_STALL_BACKOFF_SECONDS", (0.05,)):
        service.start()
        try:
            assert _wait_until(lambda: service.status()["hasSnapshot"])
            first = StallingSession.built[0]
            first.stall.set()
            StallingSession.slow_start = 0.3  # the replacement takes a while to log in
            with patch("tqkq_readonly_sidecar.main.queue.Queue.get", side_effect=queue.Empty):
                for _ in range(3):
                    with pytest.raises(RuntimeError, match="market_request_timeout"):
                        service.market_workbench("SHFE.au2612", 60)
            assert _wait_until(lambda: service._session is None, timeout=5)
            # While the session is being built, requests are refused at once and count nothing.
            for _ in range(6):
                with pytest.raises(RuntimeError, match="sidecar_reconnecting"):
                    service.market_workbench("SHFE.au2612", 60)
            with patch("tqkq_readonly_sidecar.main.queue.Queue.get", side_effect=queue.Empty):
                service._note_command_timeout()  # a straggler that timed out with no session
            assert service._command_timeouts == 0
            assert _wait_until(lambda: len(StallingSession.built) == 2 and service._session is StallingSession.built[1], timeout=10)
            time.sleep(0.3)  # six supervisor checks
            assert len(StallingSession.built) == 2, "the replacement must not be replaced for timeouts it never saw"
            assert [item["reason"] for item in service.status()["sessionRebuilds"]] == ["market_session_stalled"]
            assert service.market_workbench("SHFE.au2612", 60)["quote"]["symbol"] == "SHFE.au2612"
        finally:
            StallingSession.slow_start = 0.0
            service.stop()


def test_rebuild_answers_queued_commands_with_reconnecting():
    StallingSession.built, StallingSession.fail_next = [], 0
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"], session_factory=StallingSession)
    with patch("tqkq_readonly_sidecar.main.SESSION_STALL_TIMEOUTS", 1):
        service.start()
        try:
            assert _wait_until(lambda: service.status()["hasSnapshot"])
            first = StallingSession.built[0]
            first.stall.set()
            # The actor takes a queued command at once, so queue only once it is stuck in read().
            assert first.wedged.wait(timeout=2)
            # Two requests queued behind the wedged actor: their callers time out …
            results: list[dict] = []
            def request():
                try:
                    results.append(service.market_workbench("SHFE.au2612", 60))
                except RuntimeError as error:
                    results.append({"error": str(error)})
            callers = [threading.Thread(target=request) for _ in range(2)]
            for caller in callers:
                caller.start()
            time.sleep(0.3)
            queued = service._commands.qsize()
            assert queued == 2
            # … and the rebuild drains the queue, answering each with sidecar_reconnecting instead of
            # leaving them for the next session.
            service._begin_rebuild("market_session_stalled", service._generation)
            assert service._commands.qsize() == 0
            for caller in callers:
                caller.join(timeout=6)
            assert len(results) == 2
            assert [item.get("error") for item in results] == ["sidecar_reconnecting"] * 2, results
        finally:
            service.stop()


def test_repeated_stalls_back_off_after_the_first_immediate_rebuild():
    StallingSession.built, StallingSession.fail_next = [], 0
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"], session_factory=StallingSession)
    # The supervisor polls every 50 ms here, so the first (immediate) rebuild lands well within
    # the 1 s backoff step and the backed-off second one only after it.
    with patch("tqkq_readonly_sidecar.main.SESSION_STALL_TIMEOUTS", 1), patch("tqkq_readonly_sidecar.main.SESSION_STALL_BACKOFF_SECONDS", (1.0,)), \
            patch("tqkq_readonly_sidecar.main.SESSION_STALL_STREAK_RESET_SECONDS", 30.0):
        service.start()
        try:
            def stall_current():
                session = service._session
                session.stall.set()
                with patch("tqkq_readonly_sidecar.main.queue.Queue.get", side_effect=queue.Empty):
                    with pytest.raises(RuntimeError):
                        service.market_workbench("SHFE.au2612", 60)
            assert _wait_until(lambda: service.status()["hasSnapshot"])
            started = time.monotonic()
            stall_current()
            # First stall: replaced immediately (well under the 1 s backoff).
            assert _wait_until(lambda: len(StallingSession.built) == 2 and service._session is StallingSession.built[1], timeout=5)
            first_rebuild = time.monotonic() - started
            assert first_rebuild < 1.0
            # Second stall right after: the supervisor waits the first backoff step before rebuilding.
            started = time.monotonic()
            stall_current()
            assert _wait_until(lambda: len(StallingSession.built) == 3, timeout=8)
            assert time.monotonic() - started >= 1.0
            reasons = [item["reason"] for item in service.status()["sessionRebuilds"]]
            assert reasons == ["market_session_stalled", "market_session_stalled"]
        finally:
            service.stop()


def test_a_rebuild_writes_the_actor_traceback_to_stderr(capsys):
    StallingSession.built, StallingSession.fail_next = [], 0
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"], session_factory=StallingSession)
    with patch("tqkq_readonly_sidecar.main.SESSION_STALL_TIMEOUTS", 1):
        service.start()
        try:
            assert _wait_until(lambda: service.status()["hasSnapshot"])
            StallingSession.built[0].stall.set()
            with patch("tqkq_readonly_sidecar.main.queue.Queue.get", side_effect=queue.Empty):
                with pytest.raises(RuntimeError):
                    service.market_workbench("SHFE.au2612", 60)
            assert _wait_until(lambda: len(StallingSession.built) == 2, timeout=5)
        finally:
            service.stop()
    err = capsys.readouterr().err
    assert "rebuild reason=market_session_stalled thread=tqkq-readonly-session-1" in err


def test_timeouts_before_the_first_snapshot_do_not_count_as_a_stall():
    """A freshly logged-in session answers slowly while its initial data arrives; that is not a stall."""
    StallingSession.built, StallingSession.fail_next = [], 0
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"], session_factory=StallingSession)
    with patch("tqkq_readonly_sidecar.main.SESSION_STALL_TIMEOUTS", 1), patch("tqkq_readonly_sidecar.main.SESSION_FIRST_SNAPSHOT_SECONDS", 30.0):
        service.start()
        try:
            assert _wait_until(lambda: bool(StallingSession.built) and service._session is StallingSession.built[0])
            # Pretend the session has not delivered a snapshot of its own yet: the previous session's capture is older.
            with service._lock:
                service._captured_monotonic = service._session_started_monotonic - 1.0
            for _ in range(3):
                service._note_command_timeout()
            assert service._command_timeouts == 0
            time.sleep(1.2)
            assert len(StallingSession.built) == 1 and service.status()["sessionRebuilds"] == []
            # Once the session has captured, a timeout counts again.
            assert _wait_until(lambda: service._captured_monotonic is not None and service._captured_monotonic >= service._session_started_monotonic)
            service._note_command_timeout()
            assert service._command_timeouts == 1
        finally:
            service.stop()


def test_a_worker_that_exits_on_its_own_logs_the_exception_and_names_it_in_the_retry_reason(capsys):
    class DyingSession(StallingSession):
        def read(self, timeout):
            if getattr(self, "die", False):
                raise ValueError("websocket closed by peer")
            return super().read(timeout)
    StallingSession.built, StallingSession.fail_next = [], 0
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"], session_factory=DyingSession)
    with patch("tqkq_readonly_sidecar.main.SESSION_RETRY_BACKOFF_SECONDS", (0.1, 0.1)):
        service.start()
        try:
            assert _wait_until(lambda: service.status()["hasSnapshot"])
            StallingSession.built[0].die = True
            assert _wait_until(lambda: len(StallingSession.built) == 2 and service._session is StallingSession.built[1], timeout=10)
            reasons = [item["reason"] for item in service.status()["sessionRebuilds"]]
            assert reasons == ["session_retry:tqkq_sidecar_valueerror:ValueError"]
        finally:
            service.stop()
    err = capsys.readouterr().err
    assert "worker exit generation=1 error=ValueError: websocket closed by peer" in err
    assert "Traceback (most recent call last)" in err and 'raise ValueError("websocket closed by peer")' in err


def test_a_queued_order_is_taken_at_once_and_reports_where_its_time_went() -> None:
    # 2026-09-27 下单全程打点: the actor used to sleep 0.1 s after every read before looking
    # for commands, so each order waited up to that long on top of the read.
    service = SnapshotService(username=None, password=None, symbols=["SHFE.au2612"],
                              fixture=True, trading_enabled=True)
    service.start()
    try:
        deadline = time.monotonic() + 2
        while service.status()["dataStatus"] != "simulated" and time.monotonic() < deadline:
            time.sleep(0.01)
        waits = []
        for index in range(8):
            time.sleep(0.013 * index)  # land at different points of the actor's loop
            report = service.submit_order_once({
                "commandId": f"timing-{index}", "clientOrderId": f"timing-{index}", "symbol": "SHFE.au2612",
                "direction": "BUY", "offset": "OPEN", "volume": 1, "limitPrice": 500.0,
            })
            assert report["found"] is True
            assert set(report["timing"]) == {"queuedMs", "exchangeMs"}
            waits.append(report["timing"]["queuedMs"])
        assert max(waits) < 60, waits
    finally:
        service.stop()


def test_a_quiet_market_read_stops_waiting_once_a_command_is_queued() -> None:
    # 2026-09-27 production (market closed): a quote read through the actor took p50 143 ms,
    # max 255 ms, because read() sat in one 0.25 s wait_update before looking at the queue.
    class QuietApi:
        def __init__(self) -> None:
            self.calls = 0
            self.update_at: float | None = None

        def wait_update(self, deadline: float) -> bool:
            self.calls += 1
            while time.time() < deadline:
                if self.update_at is not None and time.time() >= self.update_at:
                    return True
                time.sleep(0.002)
            return False

    session = object.__new__(OfficialTqKqSession)
    session.api = QuietApi()
    session._catalog_task = object()
    session.snapshot = lambda: None
    # Nothing queued: the whole timeout is still waited (no busier snapshot cadence).
    started = time.monotonic()
    session.read(0.25)
    assert 0.24 <= time.monotonic() - started < 0.4
    assert session.api.calls >= 4
    # A command queued mid-wait ends it within one short slice.
    queued_at = time.monotonic() + 0.06
    session.commands_waiting = lambda: time.monotonic() >= queued_at
    started = time.monotonic()
    session.read(0.25)
    assert time.monotonic() - started < 0.13
    # A market update still ends it at once.
    session.commands_waiting = None
    session.api.update_at = time.time() + 0.02
    started = time.monotonic()
    session.read(0.25)
    assert time.monotonic() - started < 0.06


def test_identical_worker_exits_without_a_snapshot_re_exec_the_container_sidecar(monkeypatch):
    """2026-09-29: every rebuilt session died with "Event loop is closed" until the container was recreated."""
    class WedgedSession(StallingSession):
        def read(self, timeout):
            if getattr(self, "die", False) or len(StallingSession.built) > 1:
                raise RuntimeError("Event loop is closed")
            return super().read(timeout)
    StallingSession.built, StallingSession.fail_next = [], 0
    monkeypatch.delenv("AI_TRADER_SIDECAR_REEXEC_AT", raising=False)
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"], session_factory=WedgedSession,
                              reexec_on_wedge=True)
    reexecs = threading.Event()
    service.reexec = reexecs.set
    with patch("tqkq_readonly_sidecar.main.SESSION_RETRY_BACKOFF_SECONDS", (0.1, 0.1, 0.1)):
        service.start()
        try:
            assert _wait_until(lambda: service.status()["hasSnapshot"])
            StallingSession.built[0].die = True
            # The first exit follows a session that served: one retry. The rebuilt one dies the same way
            # before any snapshot: the process starts over.
            assert reexecs.wait(10)
            assert len(StallingSession.built) == 2
        finally:
            service.stop()


def test_re_exec_is_off_for_pool_children_and_guarded_by_its_interval(monkeypatch):
    from tqkq_readonly_sidecar.main import REEXEC_MIN_INTERVAL_SECONDS, reexec_allowed
    class AlwaysDying(StallingSession):
        def read(self, timeout):
            raise RuntimeError("Event loop is closed")
    StallingSession.built, StallingSession.fail_next = [], 0
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"], session_factory=AlwaysDying)
    called = threading.Event()
    service.reexec = called.set
    with patch("tqkq_readonly_sidecar.main.SESSION_RETRY_BACKOFF_SECONDS", (0.05,) * 5):
        service.start()
        try:
            assert _wait_until(lambda: len(StallingSession.built) >= 3, timeout=10)
            assert not called.is_set()  # a pool child keeps the in-process retries only
        finally:
            service.stop()
    monkeypatch.setenv("AI_TRADER_SIDECAR_REEXEC_AT", "1000")
    assert not reexec_allowed(now=1000 + REEXEC_MIN_INTERVAL_SECONDS - 1)
    assert reexec_allowed(now=1000 + REEXEC_MIN_INTERVAL_SECONDS)


def test_combined_snapshot_route_serves_one_fact_per_read():
    """/v1/snapshot (#700 review): status / account / positions / activity off ONE snapshot —
    the same projections the four single reads answer with, so a Runtime reader never pairs
    one request's activity with another request's positions."""
    process, port = start_fixture()
    try:
        with request(port, "/v1/snapshot") as response:
            combined = json.load(response)
        assert combined["schemaVersion"] == 1
        assert combined["dataStatus"] in {"normal", "simulated"}   # fixture 模式自报 simulated
        assert set(combined["status"]) == {"dataStatus", "accountStatus", "marketDataStatus",
                                           "ordersInitialized"}
        with request(port, "/v1/account") as response:
            account = json.load(response)
        with request(port, "/v1/positions") as response:
            positions = json.load(response)
        with request(port, "/v1/activity") as response:
            activity = json.load(response)
        volatile = ("ageSeconds", "capturedAt")

        def settled(value):
            return {key: item for key, item in value.items() if key not in volatile}

        assert settled(combined["account"]) == settled(account)
        assert combined["positions"] == positions
        assert settled(combined["activity"]) == settled(activity)
        assert combined["activity"]["schemaVersion"] == 1
    finally:
        stop(process)


def _seeded_service(snapshot: Snapshot) -> SnapshotService:
    """A service whose actor has published exactly the given cut (no thread started)."""
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"])
    with service._lock:
        service._snapshot = snapshot
        service._captured_monotonic = time.monotonic()
        service._status = "normal"
        service._error_code = None
        service._connection_stage = "ready"
        service._orders_initialized = False
    return service


@pytest.mark.parametrize("reader", ["activity", "combined"])
def test_a_publish_landing_mid_read_never_mixes_two_cuts(reader):
    """#700 review 受控交错：读侧一旦在拿到 status 之后另读一次派生状态（旧 activity 的
    第二次 status()，或 status 与 snapshot 两次锁边界），actor 的一次发布就能落在中间——
    旧成交配新 ordersInitialized。修复后 activity()/combined() 的全部事实取自同一个锁边界
    内的一次 status 调用：把发布注入到“第二次 status 调用”上，第一次读仍是完整的旧切面，
    之后的下一次读才见完整新切面。"""
    def cut(trades, day):
        return Snapshot(
            account={"balance": 100_000.0, "staticBalance": 100_000.0, "available": 100_000.0,
                     "floatProfit": 0.0, "positionProfit": 0.0, "closeProfit": 0.0,
                     "commission": 0.0, "margin": 0.0},
            positions=[], orders=[],
            trades=[{"tradeId": f"trade-{day}", "clientOrderId": "order-1",
                     "symbol": "SHFE.au2612", "direction": "BUY", "offset": "OPEN",
                     "price": 820.0, "volume": 1}] if trades else [],
            markets={}, trading_day=day, captured_at=f"{day}T09:31:00+08:00",
        )

    service = _seeded_service(cut(trades=False, day="2026-11-02"))
    real_status = service.status
    calls = {"count": 0}

    def interleaving_status():
        calls["count"] += 1
        if calls["count"] == 2:                    # 下一次 status 调用前 actor 发布了新切面
            with service._lock:
                service._snapshot = cut(trades=True, day="2026-11-03")
                service._captured_monotonic = time.monotonic()
                service._orders_initialized = True
        return real_status()

    service.status = interleaving_status
    try:
        first = service.activity() if reader == "activity" else service.combined()
        activity = first if reader == "activity" else first["activity"]
        assert activity["trades"] == [] and activity["ordersInitialized"] is False
        if reader == "combined":
            assert first["account"]["tradingDay"] == "2026-11-02"
            assert first["status"]["ordersInitialized"] is False
        second = service.activity() if reader == "activity" else service.combined()
        activity = second if reader == "activity" else second["activity"]
        assert [trade["tradeId"] for trade in activity["trades"]] == ["trade-2026-11-03"]
        assert activity["ordersInitialized"] is True
    finally:
        service.status = real_status


def test_daily_maintenance_failure_has_its_own_code():
    # 2026-10-21 19:00: TqSdk adds "每日 19:00-19:30 为日常运维时间" to a connection failure in 快期's
    # daily maintenance window; the client says 例行维护 instead of 网络中断.
    from tqkq_readonly_sidecar.main import tqkq_error_code
    error = Exception("与 wss://free-api.shinnytech.com/t/nfmd/front/mobile 的连接失败，每日 19:00-19:30 为日常运维时间，请稍后再试")
    assert tqkq_error_code(error) == "tqkq_daily_maintenance"
    assert tqkq_error_code(Exception("boom")) == "tqkq_sidecar_exception"

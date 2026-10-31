"""paper 引擎服务层：HTTP 契约、持久化与恢复、文件锁、多账户隔离、
RecordedQuoteSource 回放，以及与现有 sidecar 假数据模式的逐字段契约对照。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from types import SimpleNamespace

import pytest

from test_paper_book import ACCOUNT, TOKEN, TRADING_DATES, au_quote, submit_request
from tqkq_readonly_sidecar.main import (
    OfficialTqKqSession,
    ReadOnlyServer,
    SnapshotService,
)
from tqkq_readonly_sidecar.paper import (
    PaperEngine,
    PaperServer,
    RecordedQuoteSource,
    TqQuoteSource,
    UnknownSymbolError,
    VirtualClock,
)
from tqkq_readonly_sidecar.paper_book import AttrView

SECOND = "paper:0000000000b2"


@pytest.fixture()
def service(tmp_path):
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"), clock=clock,
                         trading_dates=TRADING_DATES)
    engine.open()
    engine.start()
    engine.create_account(ACCOUNT)
    yield engine
    engine.close()


def feed(engine: PaperEngine, quote: dict) -> None:
    engine.feed_quote(quote)
    assert engine.drain(timeout=5.0)


# -- 恢复与 NOT_DISPATCHED -------------------------------------------------


def test_restart_recovers_state_and_gates_commands_until_first_quote(tmp_path):
    db = str(tmp_path / "paper.sqlite3")
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    first = PaperEngine(db_path=db, clock=clock, trading_dates=TRADING_DATES)
    first.open()
    first.start()
    first.create_account(ACCOUNT)
    feed(first, au_quote("09:31:00.000000", 820.0))
    first.submit(ACCOUNT, submit_request("order-recover", volume=2))
    before = first.account(ACCOUNT)
    first.close()

    restarted = PaperEngine(
        db_path=db,
        clock=VirtualClock(datetime.fromisoformat("2026-11-02T10:00:00+08:00")),
        trading_dates=TRADING_DATES,
    )
    restarted.open()
    restarted.start()
    # 首批行情之前：命令 NOT_DISPATCHED、行情未初始化，但引擎已加载完毕，
    # 账目可读、读接口不再依赖行情新鲜度（审查 #465 第 4 条）。
    gate = restarted.submit(ACCOUNT, submit_request("order-gated"))
    assert gate == {"ordersInitialized": False, "found": False, "commandStatus": "NOT_DISPATCHED"}
    assert restarted.activity(ACCOUNT)["ordersInitialized"] is True
    after = restarted.account(ACCOUNT)
    assert after["equity"] == before["equity"]
    assert after["positionCount"] == 1
    assert [item["volume"] for item in restarted.positions(ACCOUNT)] == [2]
    # 恢复后已结束委托仍查得到（归档）。
    archived = restarted.query_order(ACCOUNT, "order-recover")
    assert archived["found"] is True
    assert archived["order"]["status"] == "FINISHED"
    # 首批行情到达后恢复可交易；NOT_DISPATCHED 未被缓存，同一 clientOrderId 可重试。
    feed(restarted, au_quote("10:31:00.000000", 821.0))
    assert restarted.activity(ACCOUNT)["ordersInitialized"] is True
    closed = restarted.submit(ACCOUNT, submit_request(
        "order-gated", direction="SELL", offset="CLOSETODAY", volume=2, limitPrice=820.98))
    assert closed["order"]["status"] == "FINISHED"
    restarted.close()


def test_settlement_trade_log_persisted_per_day(service):
    feed(service, au_quote("09:31:00.000000", 820.0))
    service.submit(ACCOUNT, submit_request("order-log", volume=1))
    feed(service, au_quote("2026-11-03 09:31:00.000000", 821.0, settlement=821.0))
    with sqlite3.connect(service.db_path) as connection:
        rows = connection.execute(
            "SELECT trading_day FROM paper_trade_log WHERE account_id = ?", (ACCOUNT,)
        ).fetchall()
    assert [row[0] for row in rows] == ["2026-11-02"]


# -- 审查 #465：新合约先记租约再等首包 --------------------------------------


def test_market_quote_leases_symbol_and_waits_for_first_packet(tmp_path):
    # 从未订阅过的合约：第一次读取就要触发订阅（wanted_symbols），首包未到时
    # 报明确的 market_quote_not_ready，而不是让合约永远进不了订阅表。
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"), clock=clock,
                         trading_dates=TRADING_DATES, quote_wait_seconds=0.5)
    engine.open()
    engine.start()
    engine.create_account(ACCOUNT)
    try:
        with pytest.raises(RuntimeError, match="market_quote_not_ready"):
            engine.market_quote(ACCOUNT, "SHFE.rb2610")
        assert "SHFE.rb2610" in engine.wanted_symbols()
        feed(engine, au_quote("09:31:00.000000", 820.0, symbol="SHFE.rb2610"))
        quote = engine.market_quote(ACCOUNT, "SHFE.rb2610")
        assert quote["lastPrice"] == 820.0
    finally:
        engine.close()


def test_a_lapsed_subscription_waits_for_a_fresh_quote_instead_of_answering_the_old_one(tmp_path):
    # 2026-10-21：订阅断了（租约过期、没有持仓挂单）的合约，缓存里留着断订阅前的旧报价；隔天第一次读取
    # 若直接答它，Runtime 判“行情不是最新”挡回第一单。现在重新订阅后在交易时段内等新的一笔再答。
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:31:00+08:00"))
    engine = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"), clock=clock,
                         trading_dates=TRADING_DATES, quote_wait_seconds=2.0)
    engine.open()
    engine.start()
    engine.create_account(ACCOUNT)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0, symbol="SHFE.rb2610"))
        assert engine.market_quote(ACCOUNT, "SHFE.rb2610")["dataStatus"] == "normal"
        clock.advance(datetime.fromisoformat("2026-11-02T09:55:00+08:00"))   # lease (600 s) long gone
        assert "SHFE.rb2610" not in engine.wanted_symbols()
        threading.Timer(0.2, lambda: feed(engine, au_quote("09:55:00.000000", 826.0, symbol="SHFE.rb2610"))).start()
        quote = engine.market_quote(ACCOUNT, "SHFE.rb2610")
        assert quote["lastPrice"] == 826.0 and quote["dataStatus"] == "normal"
        # 一直等不到新的一笔：照旧答旧报价并标旧（Runtime 如实说行情不是最新），不报错。
        clock.advance(datetime.fromisoformat("2026-11-02T10:10:00+08:00"))
        engine.quote_wait_seconds = 0.2
        assert engine.market_quote(ACCOUNT, "SHFE.rb2610")["dataStatus"] == "stale"
    finally:
        engine.close()


def test_second_engine_on_the_same_database_refuses_to_start(tmp_path, service):
    other = PaperEngine(db_path=service.db_path,
                        clock=VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00")),
                        trading_dates=TRADING_DATES)
    with pytest.raises(RuntimeError, match="paper_engine_lock_held"):
        other.open()


def test_accounts_are_isolated(service):
    service.create_account(SECOND)
    feed(service, au_quote("09:31:00.000000", 820.0))
    service.submit(ACCOUNT, submit_request("order-isolated"))
    second = service.submit(SECOND, submit_request("order-isolated-b", volume=1,
                                                   limitPrice=820.02))
    assert second["order"]["status"] == "FINISHED"
    first_positions = {item["volume"] for item in service.positions(ACCOUNT)}
    second_positions = {item["volume"] for item in service.positions(SECOND)}
    assert first_positions == {1} and second_positions == {1}
    assert service.account(ACCOUNT)["equity"] == service.account(SECOND)["equity"]
    assert {item["clientOrderId"] for item in service.activity(ACCOUNT)["orders"]} == {
        "order-isolated"}
    assert {item["clientOrderId"] for item in service.activity(SECOND)["orders"]} == {
        "order-isolated-b"}


# -- TqQuoteSource ------------------------------------------------------------


def test_tq_quote_source_feeds_the_first_snapshot_of_a_closed_market(service, monkeypatch):
    """Production 10-03 (holiday): synchronous get_quote returns with the snapshot
    already in, so it never shows up as a later is_changing — and a closed market
    never changes. Every quote read answered 503 market_quote_not_ready. A new
    subscription's snapshot is fed at once."""
    snapshot = au_quote("2026-09-30 14:59:59.999500", 820.0)
    subscribed: list[str] = []

    class ClosedMarketApi:
        def get_quote(self, symbol):
            subscribed.append(symbol)
            fields = {key: value for key, value in snapshot.items() if key != "trading_time"}
            return SimpleNamespace(**{**fields, "trading_time": None})
        def wait_update(self, deadline=None):
            time.sleep(0.02)
            return False
        def is_changing(self, _obj):
            return False  # the market is closed: nothing ever changes
        def close(self):
            pass

    source = TqQuoteSource(lambda: ("user", "pass"))
    monkeypatch.setattr(source, "_connect", lambda symbols: (ClosedMarketApi(), {}))
    with service._state_lock:
        service._leases["SHFE.au2612"] = service._clock()  # a reader asked for it
    source.start(service)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "SHFE.au2612" not in service._quotes:
            time.sleep(0.05)
    finally:
        source.stop()
    assert subscribed == ["SHFE.au2612"]
    assert service._quotes["SHFE.au2612"]["last_price"] == 820.0


def test_tq_quote_source_detach_keeps_product_id_for_margin_grouping():
    # 527-2：官方 SimTrade 的同品种单边大边保证金分支读 quote 的 product_id
    # （tqsdk 3.10.2 trade_future._adjust_account_by_position：缺失时退化为
    # other_margin 两边相加）。_detach 是生产行情进内核的唯一通道，必须保留
    # 这个字段；引擎侧的分组效果见 test_paper_book 的锁仓用例。
    source = TqQuoteSource(lambda: ("user", "pass"))
    detached = source._detach("SHFE.au2612", AttrView(au_quote("09:31:00.000000", 820.0)))
    assert detached["product_id"] == "au"


@pytest.mark.parametrize("expire_seconds,expired", [(1_800_000_000.0, False), (1.0, True)])
def test_tq_quote_source_reads_the_expiry_in_seconds(service, expire_seconds, expired):
    """10-08: TqSdk's Quote.expire_datetime is seconds; passed through raw, rb2701 read as expired
    in 1970 and the production engine rejected every order with 合约已到期或即将到期."""
    source = TqQuoteSource(lambda: ("user", "pass"))
    feed(service, source._detach("SHFE.au2612", AttrView({**au_quote("09:31:00.000000", 820.0),
                                                          "expire_datetime": expire_seconds})))
    order = service.submit(ACCOUNT, submit_request("order-expiry"))["order"]
    assert ("合约已到期或即将到期" in str(order.get("lastMsg"))) is expired


# -- RecordedQuoteSource ---------------------------------------------------


def test_recorded_quote_source_replays_jsonl(tmp_path, service):
    lines = [
        json.dumps(au_quote("09:31:00.000000", 820.0)),
        json.dumps(au_quote("09:32:00.000000", 820.5)),
        json.dumps(au_quote("09:33:00.000000", 821.0)),
    ]
    path = tmp_path / "quotes.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    source = RecordedQuoteSource(path=str(path))
    assert len(source.quotes) == 3
    for quote in source.quotes:
        service.feed_quote(quote)
        assert service.drain(timeout=5.0)
    assert service.market_quote(ACCOUNT, "SHFE.au2612")["lastPrice"] == 821.0


def test_recorded_quote_source_auto_feed_keeps_the_clock_on_the_recording(service):
    lines = [json.dumps(au_quote("09:31:00.000000", 820.0))]
    source = RecordedQuoteSource(lines=lines)
    source.start(service, interval=0.01)
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                if service.market_quote(ACCOUNT, "SHFE.au2612")["lastPrice"] == 820.0:
                    break
            except (KeyError, RuntimeError):
                pass  # 首笔行情尚未被 actor 处理
            time.sleep(0.01)
        assert service.market_quote(ACCOUNT, "SHFE.au2612")["lastPrice"] == 820.0
    finally:
        source.stop()


# -- HTTP 契约 ---------------------------------------------------------------


@pytest.fixture()
def http_service(tmp_path):
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"), clock=clock,
                         trading_dates=TRADING_DATES)
    engine.open()
    engine.start()
    server = PaperServer(("127.0.0.1", 0), TOKEN, engine)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield engine, int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        engine.close()


def request(port: int, path: str, *, token: str | None = TOKEN, method: str = "GET",
            payload: dict | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = json.dumps(payload).encode() if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    return urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers, method=method, data=data,
    ), timeout=10)


def test_http_contract_routes_and_auth(http_service):
    engine, port = http_service
    try:
        request(port, "/healthz", token="wrong-token-x")
        raise AssertionError("unauthenticated request unexpectedly succeeded")
    except urllib.error.HTTPError as error:
        assert error.code == 401

    with request(port, "/healthz") as response:
        health = json.load(response)
    assert health["service"] == "ai-trader-paper-engine"

    try:
        request(port, f"/accounts/{ACCOUNT}/v1/account")
        raise AssertionError("unknown account unexpectedly served")
    except urllib.error.HTTPError as error:
        assert error.code == 404

    with request(port, "/accounts", method="POST",
                 payload={"accountId": ACCOUNT}) as response:
        created = json.load(response)
    assert created["accountId"] == ACCOUNT
    assert created["initBalance"] == 1_000_000.0
    with request(port, "/accounts", method="POST",
                 payload={"accountId": ACCOUNT}) as response:
        assert json.load(response) == created  # 幂等
    with request(port, "/accounts", method="POST",
                 payload={"accountId": SECOND, "initBalance": 500_000.0}) as response:
        assert json.load(response)["initBalance"] == 500_000.0
    with request(port, f"/accounts/{ACCOUNT}") as response:
        assert json.load(response)["accountId"] == ACCOUNT

    quote = au_quote("09:31:00.000000", 820.0)
    engine.feed_quote(quote)
    assert engine.drain(timeout=5.0)
    with request(port, f"/accounts/{ACCOUNT}/v1/market/SHFE.au2612/quote") as response:
        market = json.load(response)
    assert market["symbol"] == "SHFE.au2612"
    assert market["price"] == 820.0
    assert market["dataStatus"] in {"normal", "stale"}
    # Production 10-03 integration: the order estimate reads these two per-lot
    # figures and the Runtime refuses a closed-market order up front by the
    # session status — both were missing ("unknown" / null) before.
    assert market["marginPerLot"] == 80_000.0 and market["commissionPerLot"] == 20.0
    assert market["marketSessionStatus"] == "trading"  # 2026-11-02 09:31 is in the day session

    with request(port, f"/accounts/{ACCOUNT}/v1/simulation/orders/submit", method="POST",
                 payload=submit_request("order-http")) as response:
        accepted = json.load(response)
    assert accepted["order"]["status"] == "FINISHED"
    with request(port, f"/accounts/{ACCOUNT}/v1/simulation/orders/order-http") as response:
        assert json.load(response)["order"]["clientOrderId"] == "order-http"
    # 已成交的委托不可撤（409）；挂单可撤（200）。
    try:
        request(port, f"/accounts/{ACCOUNT}/v1/simulation/orders/order-http/cancel",
                method="POST", payload={"commandId": "cmd-cancel-http-0"})
        raise AssertionError("cancel of a filled order unexpectedly succeeded")
    except urllib.error.HTTPError as error:
        assert error.code == 409
    with request(port, f"/accounts/{ACCOUNT}/v1/simulation/orders/submit", method="POST",
                 payload=submit_request("order-http-rest", limitPrice=815.0)) as response:
        assert json.load(response)["order"]["status"] == "ALIVE"
    with request(port, f"/accounts/{ACCOUNT}/v1/simulation/orders/order-http-rest/cancel",
                 method="POST", payload={"commandId": "cmd-cancel-http-1"}) as response:
        cancelled = json.load(response)
    assert cancelled["order"]["status"] == "FINISHED"
    assert cancelled["order"]["isError"] is False

    with request(port, f"/accounts/{ACCOUNT}/v1/status") as response:
        status = json.load(response)
    assert status["dataStatus"] in {"normal", "stale", "unavailable"}
    assert status["tradingDay"] == "2026-11-02"
    with request(port, f"/accounts/{ACCOUNT}/v1/account") as response:
        account = json.load(response)
    assert account["tradingDay"] == "2026-11-02"
    with request(port, f"/accounts/{ACCOUNT}/v1/positions") as response:
        assert json.load(response)[0]["symbol"] == "SHFE.au2612"
    with request(port, f"/accounts/{ACCOUNT}/v1/activity") as response:
        activity = json.load(response)
    assert activity["schemaVersion"] == 1
    assert activity["ordersInitialized"] is True
    assert activity["liveTrading"] == "disabled"
    assert activity["trades"][0]["tradingDay"] == "2026-11-02"
    assert activity["trades"][0]["tradeId"]

    for payload in (
        {"accountId": ""},
        {"accountId": "x"},
        {"accountId": ACCOUNT, "initBalance": "rich"},
    ):
        try:
            request(port, "/accounts", method="POST", payload=payload)
            raise AssertionError("invalid account payload unexpectedly accepted")
        except urllib.error.HTTPError as error:
            assert error.code == 422
    try:
        request(port, f"/accounts/{ACCOUNT}/v1/simulation/orders/submit", method="POST",
                payload={**submit_request("order-http-bad"), "offset": "INVALID"})
        raise AssertionError("invalid order unexpectedly accepted")
    except urllib.error.HTTPError as error:
        assert error.code == 422


def test_snapshots_batch_matches_the_single_account_reads(http_service):
    """The Runtime's shared poll (2026-10-03): one POST returns every account's
    status/account/positions in the single-read shapes; an unknown account
    carries its own 404 and does not fail the others."""
    engine, port = http_service
    for account_id in (ACCOUNT, SECOND):
        with request(port, "/accounts", method="POST", payload={"accountId": account_id}):
            pass
    engine.feed_quote(au_quote("09:31:00.000000", 820.0))
    assert engine.drain(timeout=5.0)
    with request(port, f"/accounts/{ACCOUNT}/v1/simulation/orders/submit", method="POST",
                 payload=submit_request("order-batch")) as response:
        assert json.load(response)["order"]["status"] == "FINISHED"
    missing = "paper:000000000000"
    with request(port, "/v1/snapshots", method="POST",
                 payload={"accountIds": [ACCOUNT, missing, SECOND]}) as response:
        batch = json.load(response)
    assert batch["schemaVersion"] == 1
    assert batch["accounts"][missing] == {"error": "account_not_found", "httpStatus": 404}
    for account_id in (ACCOUNT, SECOND):
        entry = batch["accounts"][account_id]
        for part in ("status", "account", "positions"):
            with request(port, f"/accounts/{account_id}/v1/{part}") as response:
                single = json.load(response)
            if part == "account":  # stamped at read time
                assert entry[part].pop("capturedAt") and single.pop("capturedAt")
            assert entry[part] == single, (account_id, part)
    assert batch["accounts"][ACCOUNT]["positions"][0]["symbol"] == "SHFE.au2612"
    assert batch["accounts"][SECOND]["positions"] == []

    for payload in ({}, {"accountIds": "x"}, {"accountIds": ["x"]},
                    {"accountIds": [ACCOUNT], "extra": 1}):
        try:
            request(port, "/v1/snapshots", method="POST", payload=payload)
            raise AssertionError("invalid snapshots request unexpectedly accepted")
        except urllib.error.HTTPError as error:
            assert error.code == 422
    try:
        request(port, "/v1/snapshots", method="POST", token="wrong-token-x",
                payload={"accountIds": [ACCOUNT]})
        raise AssertionError("unauthenticated snapshots unexpectedly served")
    except urllib.error.HTTPError as error:
        assert error.code == 401


# -- 与现有 sidecar 假数据模式的契约对照 ------------------------------------


def test_contract_shapes_cover_the_sidecar_fixture_fields(tmp_path):
    """引擎输出不得比 sidecar 假数据模式少字段：逐键集合对比（允许引擎多字段）。"""
    sidecar = SnapshotService(username=None, password=None,
                              symbols=["SHFE.au2612"], fixture=True, trading_enabled=True)
    server = ReadOnlyServer(("127.0.0.1", 0), TOKEN, sidecar)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    sidecar.start()
    thread.start()
    port = int(server.server_address[1])
    try:
        deadline = time.monotonic() + 5
        while not sidecar.status()["hasSnapshot"] and time.monotonic() < deadline:
            time.sleep(0.01)
        submission = submit_request("fixture-parity-1")
        with request(port, "/v1/simulation/orders/submit", method="POST",
                     payload=submission) as response:
            expected_report = json.load(response)
        with request(port, "/v1/account") as response:
            expected_account = json.load(response)
        with request(port, "/v1/activity") as response:
            expected_activity = json.load(response)
        assert expected_activity["orders"], "fixture sidecar exposes no activity order"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        sidecar.stop()

    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"), clock=clock,
                         trading_dates=TRADING_DATES)
    engine.open()
    engine.start()
    engine.create_account(ACCOUNT)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-parity-1"))
        assert set(expected_account) <= set(engine.account(ACCOUNT))
        activity = engine.activity(ACCOUNT)
        for key in set(expected_activity) - {"orders", "trades"}:
            assert key in activity
        assert set(expected_activity["orders"][0]) <= set(activity["orders"][0])
        report = engine.query_order(ACCOUNT, "order-parity-1")
        assert set(expected_report["order"]) <= set(report["order"])

        # positions / trades：sidecar 假数据模式不产出，用同一份生产转换函数对齐键集合。
        position_source = SimpleNamespace(
            pos_long=1, pos_short=0, pos_long_today=1, pos_long_his=0,
            pos_short_today=0, pos_short_his=0,
            volume_long_frozen_today=0, volume_long_frozen_his=0,
            volume_short_frozen_today=0, volume_short_frozen_his=0,
            open_price_long=820.02, open_price_short=float("nan"),
            margin_long=80_000.0, margin_short=0.0,
            float_profit_long=-40.0, float_profit_short=0.0,
        )
        expected_position = OfficialTqKqSession._activity_position("SHFE.au2612", position_source)
        assert set(expected_position[0]) <= set(engine.positions(ACCOUNT)[0])
        trade_source = SimpleNamespace(
            trade_id="t1", order_id="o1", exchange_trade_id="t1",
            exchange_id="SHFE", instrument_id="au2612",
            direction="BUY", offset="OPEN", price=820.02, volume=1,
            trade_date_time=1,
        )
        expected_trade = OfficialTqKqSession._activity_trade(trade_source)
        assert set(expected_trade) <= set(activity["trades"][0])
    finally:
        engine.close()


# -- 审查 527：故障停机、提交原子性与日历续期 --------------------------------


def build_service(db_path):
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = PaperEngine(db_path=str(db_path), clock=clock, trading_dates=TRADING_DATES)
    engine.open()
    engine.start()
    engine.create_account(ACCOUNT)
    return engine


def test_actor_quote_failure_fails_closed_instead_of_silent_death(service, monkeypatch):
    """527-3：行情处理链路抛异常不得无声杀死 actor 还照报 normal——引擎标记
    故障停机：新命令立即报 paper_engine_failed（不等 4 秒超时），读接口
    unavailable，行情不再入队、drain 不再挂等。"""
    feed(service, au_quote("09:31:00.000000", 820.0))
    resting = service.submit(ACCOUNT, submit_request("order-before-fatal", limitPrice=815.0))
    assert resting["order"]["status"] == "ALIVE"  # 挂单让账户进入合约→账户索引

    def boom(quote):
        raise RuntimeError("probe_quote_boom")

    monkeypatch.setattr(service._accounts[ACCOUNT].book, "apply_quote", boom)
    service.feed_quote(au_quote("09:41:00.000000", 821.0))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not service._failed():
        time.sleep(0.01)
    assert service._failed()

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="paper_engine_failed"):
        service.submit(ACCOUNT, submit_request("order-after-fatal"))
    assert time.monotonic() - started < 1.0  # 立即失败，不等 COMMAND_TIMEOUT
    with pytest.raises(RuntimeError, match="paper_engine_failed"):
        service.cancel_order(ACCOUNT, "order-before-fatal", "cmd-after-fatal")
    assert service.health()["status"] == "unavailable"
    assert service.health()["dataStatus"] == "unavailable"
    assert service.status(ACCOUNT)["dataStatus"] == "unavailable"
    assert service.account(ACCOUNT)["dataStatus"] == "unavailable"

    # 行情不再入队；已入队的一次性排空，drain 立即满足。
    posted = service._posted
    service.feed_quote(au_quote("09:51:00.000000", 822.0))
    assert service._posted == posted
    assert service._quote_queue.qsize() == 0
    assert service.drain(timeout=1.0) is True


def test_stopping_engine_answers_every_command_without_the_timeout(tmp_path, monkeypatch):
    """#527/#510：停机开始后不再接纳、也不再执行命令；正在执行的照常答完，队列里还没
    执行的与停机后才到的都当场答 paper_engine_stopping（没执行），不让调用方等 4 秒
    超时、也不留“执行了却报失败”的答复。"""
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"), clock=clock,
                         trading_dates=TRADING_DATES)
    engine.open()
    engine.start()
    engine.create_account(ACCOUNT)
    feed(engine, au_quote("09:31:00.000000", 820.0))
    inside, release = threading.Event(), threading.Event()
    original = engine._handle_submit

    def slow(account_id, command):
        if command.client_order_id == "order-slow":
            inside.set()
            release.wait(5)
        return original(account_id, command)

    monkeypatch.setattr(engine, "_handle_submit", slow)
    outcomes: dict[str, object] = {}

    def call(coid: str) -> None:
        # 直接入队（engine.submit 入队前要拿状态锁，而执行中的命令正持有它）。
        request = submit_request(coid, limitPrice=815.0)
        try:
            outcomes[coid] = engine._command(
                "submit", account_id=ACCOUNT, command_id=request["commandId"],
                client_order_id=coid, request=request)["order"]["status"]
        except RuntimeError as error:
            outcomes[coid] = str(error)

    slow_thread = threading.Thread(target=call, args=("order-slow",))
    slow_thread.start()
    assert inside.wait(5)
    queued_thread = threading.Thread(target=call, args=("order-queued",))
    queued_thread.start()
    deadline = time.monotonic() + 5
    while engine._commands.qsize() < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert engine._commands.qsize() == 1
    closer = threading.Thread(target=engine.close)
    closer.start()
    while not engine._stop.is_set():
        time.sleep(0.01)
    started = time.monotonic()
    release.set()
    for thread in (slow_thread, queued_thread, closer):
        thread.join(5)
    assert time.monotonic() - started < 1.0
    assert outcomes == {"order-slow": "ALIVE", "order-queued": "paper_engine_stopping"}
    with pytest.raises(RuntimeError, match="paper_engine_stopping"):
        engine.submit(ACCOUNT, submit_request("order-late"))


def test_stopping_answers_queued_commands_while_the_running_one_stays_blocked(tmp_path, monkeypatch):
    """#527 waiter（#913 复核 P2）：actor 卡在正在执行的命令上不放，停机一开始就给队列里
    确定没执行的命令答 paper_engine_stopping——不等慢命令结束、也不等到命令超时；正在
    执行的那条之后按实际结果答复，不伪报未执行。"""
    monkeypatch.setattr("tqkq_readonly_sidecar.paper.COMMAND_TIMEOUT_SECONDS", 3.0)
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"), clock=clock,
                         trading_dates=TRADING_DATES)
    engine.open()
    engine.start()
    engine.create_account(ACCOUNT)
    feed(engine, au_quote("09:31:00.000000", 820.0))
    inside, release = threading.Event(), threading.Event()
    original = engine._handle_submit

    def slow(account_id, command):
        if command.client_order_id == "order-slow":
            inside.set()
            release.wait(10)
        return original(account_id, command)

    monkeypatch.setattr(engine, "_handle_submit", slow)
    outcomes: dict[str, object] = {}

    def call(coid: str) -> None:
        request = submit_request(coid, limitPrice=815.0)
        try:
            outcomes[coid] = engine._command(
                "submit", account_id=ACCOUNT, command_id=request["commandId"],
                client_order_id=coid, request=request)["order"]["status"]
        except RuntimeError as error:
            outcomes[coid] = str(error)

    slow_thread = threading.Thread(target=call, args=("order-slow",))
    slow_thread.start()
    assert inside.wait(5)
    queued = [threading.Thread(target=call, args=(f"order-queued-{index}",)) for index in range(2)]
    for thread in queued:
        thread.start()
    deadline = time.monotonic() + 5
    while engine._commands.qsize() < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert engine._commands.qsize() == 2
    closer = threading.Thread(target=engine.close, kwargs={"join_seconds": 5.0})
    started = time.monotonic()
    closer.start()
    for thread in queued:
        thread.join(max(0.0, started + 1.0 - time.monotonic()))
    # 慢命令仍卡着：排队的两条已答“停机中、没执行”，用时远小于命令超时。
    assert slow_thread.is_alive()
    assert time.monotonic() - started < 1.5
    assert outcomes == {"order-queued-0": "paper_engine_stopping", "order-queued-1": "paper_engine_stopping"}
    release.set()
    for thread in (slow_thread, closer):
        thread.join(5)
    assert outcomes["order-slow"] == "ALIVE"
    assert {order["clientOrderId"] for order in engine_orders(tmp_path)} == {"order-slow"}


def engine_orders(tmp_path) -> list[dict]:
    reopened = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"),
                           clock=VirtualClock(datetime.fromisoformat("2026-11-02T09:40:00+08:00")),
                           trading_dates=TRADING_DATES)
    reopened.open()
    try:
        return reopened.activity(ACCOUNT)["orders"]
    finally:
        reopened.close()


def test_command_put_after_the_actor_exited_is_answered_at_once(service, monkeypatch):
    """入队与 actor 退出赛跑（入队前检查时还在、入队时已退出且排空过）：_command 自己
    排空，答 paper_engine_stopping，不等超时。"""
    real_put = service._commands.put

    def put_after_exit(command):
        service._stop.set()
        service._thread.join(5)
        service._stop.clear()   # 模拟检查时还没停：只剩 actor 已退出这一个事实
        real_put(command)

    monkeypatch.setattr(service._commands, "put", put_after_exit)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="paper_engine_stopping"):
        service.submit(ACCOUNT, submit_request("order-race"))
    assert time.monotonic() - started < 1.0
    service._stop.set()


def test_persist_failure_rebuilds_account_and_retry_executes_once(tmp_path):
    """527-1：paper_orders 写失败 → submit 报错、账户从最后已提交状态重建
    （未提交的内存订单不得被查询/重试当作已确认事实），恢复写入后同一命令
    重试只撮合一次。"""
    db = str(tmp_path / "paper.sqlite3")
    engine = build_service(db)
    probe = sqlite3.connect(db)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        probe.execute(
            "CREATE TRIGGER block_paper_orders BEFORE INSERT ON paper_orders "
            "BEGIN SELECT RAISE(ABORT, 'probe_order_abort'); END")
        probe.commit()
        with pytest.raises(sqlite3.IntegrityError, match="probe_order_abort"):
            engine.submit(ACCOUNT, submit_request("order-aborted"))
        # 内存回到最后已提交状态：订单查不到、没有持仓、账目与库一致。
        assert engine.query_order(ACCOUNT, "order-aborted") == {
            "ordersInitialized": True, "found": False}
        assert engine.account(ACCOUNT)["positionCount"] == 0
        assert engine.account(ACCOUNT)["equity"] == 1_000_000.0
        assert probe.execute("SELECT count(*) FROM paper_orders").fetchone()[0] == 0
        assert probe.execute("SELECT count(*) FROM paper_commands").fetchone()[0] == 0
        state = PaperEngine._committed_state(probe, ACCOUNT)[0]
        assert state["_positions"] == {}

        probe.execute("DROP TRIGGER block_paper_orders")
        probe.commit()
        retried = engine.submit(ACCOUNT, submit_request("order-aborted"))
        assert retried["order"]["filledVolume"] == 1
        assert [item["volume"] for item in engine.positions(ACCOUNT)] == [1]
        assert probe.execute(
            "SELECT count(*) FROM paper_commands WHERE command_id='cmd-order-aborted'"
        ).fetchone()[0] == 1
    finally:
        probe.close()
        engine.close()


def test_command_row_is_atomic_with_the_account_snapshot(tmp_path):
    """527-1：命令关联行与账本快照/终态委托同一事务——命令写不进去时快照必须
    一起回滚，否则重启后订单存在而命令丢失，重放会二次撮合。"""
    db = str(tmp_path / "paper.sqlite3")
    engine = build_service(db)
    probe = sqlite3.connect(db)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        probe.execute(
            "CREATE TRIGGER block_paper_commands BEFORE INSERT ON paper_commands "
            "BEGIN SELECT RAISE(ABORT, 'probe_command_abort'); END")
        probe.commit()
        with pytest.raises(sqlite3.IntegrityError, match="probe_command_abort"):
            engine.submit(ACCOUNT, submit_request("order-cmd-abort"))
        assert engine.account(ACCOUNT)["positionCount"] == 0  # 快照同样回滚
        assert engine.query_order(ACCOUNT, "order-cmd-abort")["found"] is False
        assert probe.execute("SELECT count(*) FROM paper_orders").fetchone()[0] == 0
        assert probe.execute("SELECT count(*) FROM paper_trades").fetchone()[0] == 0  # 成交行同样回滚
        state = PaperEngine._committed_state(probe, ACCOUNT)[0]
        assert state["_positions"] == {}
    finally:
        probe.close()
        engine.close()


# -- #527 任务二：落盘失败且重建失败后的假确认隔离 ----------------------------


def _break_restore_entry(engine: PaperEngine, monkeypatch) -> None:
    def broken_restore(*args, **kwargs):
        raise RuntimeError("probe_reload_boom")

    monkeypatch.setattr(engine, "_restore_entry", broken_restore)


def test_query_does_not_confirm_uncommitted_memory_when_reload_also_fails(
        tmp_path, monkeypatch):
    """#527 任务二：已提交 ALIVE 挂单被真实报价成交，落盘失败且账户重建也失败
    ——引擎故障停机隔离不确定内存，query 与同命令幂等重试不能再把未提交订单当
    已确认事实；独立 SQLite 与冷重启都只有已提交的 ALIVE/filled0。"""
    db = str(tmp_path / "paper.sqlite3")
    engine = build_service(db)
    probe = sqlite3.connect(db)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        resting = engine.submit(ACCOUNT, submit_request("order-fake-fill", limitPrice=815.0))
        assert resting["order"]["status"] == "ALIVE"

        probe.execute(
            "CREATE TRIGGER block_paper_orders BEFORE INSERT ON paper_orders "
            "BEGIN SELECT RAISE(ABORT, 'probe_order_abort'); END")
        probe.commit()
        _break_restore_entry(engine, monkeypatch)
        # 真实报价穿过限价：成交进内存、落盘失败、重建也失败。
        feed(engine, au_quote("09:41:00.000000", 810.0))
        assert engine._failed()  # 不确定内存被故障停机隔离，不能照常服务
        # 所有正式读入口经 _require_account（锁内共用检查）显式失败，不能成功
        # 确认不确定内存（#527 返修：不再是 query 一处的锁外守卫）。
        with pytest.raises(RuntimeError, match="paper_engine_failed"):
            engine.query_order(ACCOUNT, "order-fake-fill")
        for reader in (engine.account_info, engine.status, engine.account,
                       engine.positions, engine.activity, engine.combined):
            with pytest.raises(RuntimeError, match="paper_engine_failed"):
                reader(ACCOUNT)
        with pytest.raises(RuntimeError, match="paper_engine_failed"):
            engine.market_quote(ACCOUNT, "SHFE.au2612")
        assert engine.snapshots([ACCOUNT])["accounts"][ACCOUNT] == {
            "error": "paper_engine_failed", "httpStatus": 503}
        # 同账号新命令/幂等重试立即失败，不消费未提交内存。
        with pytest.raises(RuntimeError, match="paper_engine_failed"):
            engine.submit(ACCOUNT, submit_request("order-fake-fill", limitPrice=815.0))
        with pytest.raises(RuntimeError, match="paper_engine_failed"):
            engine.cancel_order(ACCOUNT, "order-fake-fill", "cmd-cancel-fake-fill")
        # 独立连接：账本仍是已提交的 ALIVE、0 成交、无终态委托行。
        state = PaperEngine._committed_state(probe, ACCOUNT)[0]
        order = state["_orders"]["SHFE.au2612"]["order-fake-fill"]
        assert order["status"] == "ALIVE"
        assert order["volume_left"] == order["volume_orign"] == 1
        assert probe.execute("SELECT count(*) FROM paper_orders").fetchone()[0] == 0
    finally:
        probe.close()
        engine.close()

    # 冷重启：从最后已提交状态恢复，委托仍是 ALIVE、0 成交。
    restarted = build_service(db)
    try:
        report = restarted.query_order(ACCOUNT, "order-fake-fill")
        assert report["order"]["status"] == "ALIVE"
        assert report["order"]["filledVolume"] == 0
        assert restarted.account(ACCOUNT)["positionCount"] == 0
    finally:
        restarted.close()


def test_submit_failure_with_reload_failure_cannot_fake_confirm_on_retry(
        tmp_path, monkeypatch):
    """#527 任务二（submit 侧）：提交 SQL 失败 + 重建失败后，同一命令的幂等重试
    不得把未提交内存订单当已确认结果返回（不落命令行），查询同样不可成功。"""
    db = str(tmp_path / "paper.sqlite3")
    engine = build_service(db)
    probe = sqlite3.connect(db)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        probe.execute(
            "CREATE TRIGGER block_paper_orders BEFORE INSERT ON paper_orders "
            "BEGIN SELECT RAISE(ABORT, 'probe_order_abort'); END")
        probe.commit()
        _break_restore_entry(engine, monkeypatch)
        with pytest.raises(sqlite3.IntegrityError, match="probe_order_abort"):
            engine.submit(ACCOUNT, submit_request("order-abort-retry"))
        assert engine._failed()
        # 幂等重试不能消费未提交内存假确认，命令行也不落库。
        with pytest.raises(RuntimeError, match="paper_engine_failed"):
            engine.submit(ACCOUNT, submit_request("order-abort-retry"))
        with pytest.raises(RuntimeError, match="paper_engine_failed"):
            engine.query_order(ACCOUNT, "order-abort-retry")
        assert probe.execute("SELECT count(*) FROM paper_commands").fetchone()[0] == 0
        assert probe.execute("SELECT count(*) FROM paper_orders").fetchone()[0] == 0
    finally:
        probe.close()
        engine.close()

    restarted = build_service(db)
    try:
        assert restarted.query_order(ACCOUNT, "order-abort-retry") == {
            "ordersInitialized": True, "found": False}
    finally:
        restarted.close()


def test_uncertain_flag_set_under_the_read_lock_cannot_leak_out(tmp_path):
    """#527 任务二返修：隔离检查收敛在 _state_lock 内的 _require_account。
    读者确已进入等待 _state_lock 的阶段（acquire 已发出、内锁仍被 actor 持有）
    之后，actor 才在锁内标记不确定账户再放行——读者拿到锁的第一个动作就是
    锁内隔离检查，必须 paper_engine_failed。若检查在锁外（旧 query_order
    守卫），读者在标记写入前就已越过检查点、随后照常返回，本用例红。"""
    class SignallingLock:
        """代理 RLock（仅本测试替换 engine._state_lock，不改产品锁）：任何
        线程真正 acquire 内锁之前先发 attempted 事件。"""

        def __init__(self, inner) -> None:
            self._inner = inner
            self.attempted = threading.Event()

        def acquire(self, *args, **kwargs):
            self.attempted.set()
            return self._inner.acquire(*args, **kwargs)

        def release(self) -> None:
            self._inner.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc_info) -> None:
            self._inner.release()

    engine = build_service(str(tmp_path / "paper.sqlite3"))
    original_lock = engine._state_lock
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine._state_lock = SignallingLock(original_lock)
        outcome: dict[str, str] = {}

        def reader() -> None:
            try:
                engine.query_order(ACCOUNT, "order-race")
                outcome["query"] = "ok"
            except RuntimeError as error:
                outcome["query"] = str(error)

        # actor 先持锁：读者启动，等它的 acquire 确已发出（此刻它已越过旧
        # 实现里锁外的检查点、正阻塞在内锁上），actor 才在锁内标记。
        with engine._state_lock:
            engine._state_lock.attempted.clear()  # 主线程自己的 acquire 也发过
            thread = threading.Thread(target=reader)
            thread.start()
            assert engine._state_lock.attempted.wait(timeout=5.0)
            engine._uncertain_accounts.add(ACCOUNT)
        thread.join(timeout=5.0)
        assert outcome == {"query": "paper_engine_failed"}
    finally:
        engine._state_lock = original_lock
        engine.close()


# -- 审查 527-10：交易日历续期 ------------------------------------------------


def _fixed_paper_monotonic(monkeypatch, start: float) -> SimpleNamespace:
    """给 paper 模块换一个固定 monotonic（#708）：测试不依赖宿主开机时长，
    paper.time 其余函数照旧。"""
    holder = SimpleNamespace(now=start)
    monkeypatch.setattr(
        "tqkq_readonly_sidecar.paper.time",
        SimpleNamespace(monotonic=lambda: holder.now, time=time.time, sleep=time.sleep))
    return holder


def _fixed_paper_datetime(monkeypatch, iso_day: str) -> None:
    """把 paper 模块的 datetime.now 冻结到指定北京日期（#708 审查评论）：
    覆盖前沿判定不随真实墙钟漂移，年底（前沿相对不足）也不误红。局部子类
    只换 now，datetime 其余方法与类型行为不变。"""
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime.fromisoformat(f"{iso_day}T12:00:00+08:00")
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr("tqkq_readonly_sidecar.paper.datetime", FrozenDateTime)


def test_tq_quote_source_renews_trading_calendar_before_it_runs_out(service, monkeypatch):
    """527-10：日历只在建连时取一次（−14～+31 天）；行情会话长期不断线时，
    剩余可用日期不足一周要在会话内续期——从未尝试过就立即发起（#708），
    即使 monotonic 绝对值很低（刚开机）也不能当成冷却中。"""
    holder = _fixed_paper_monotonic(monkeypatch, 2539.0)  # QA 机开机 42 分钟的实测值
    _fixed_paper_datetime(monkeypatch, "2026-10-01")
    fresh_frame = {
        "date": [datetime(2026, 10, 1), datetime(2026, 10, 2),
                 datetime(2026, 11, 30), datetime(2026, 12, 1)],
        "trading": [True, True, True, True],
    }

    class RenewingApi:
        def __init__(self) -> None:
            self.calendar_calls = 0

        def get_quote(self, symbol):
            fields = {key: value for key, value in au_quote(
                "2026-09-30 14:59:59.999500", 820.0).items() if key != "trading_time"}
            return SimpleNamespace(**fields, trading_time=None)

        def get_trading_calendar(self, start_dt, end_dt):
            self.calendar_calls += 1
            return fresh_frame

        def wait_update(self, deadline=None):
            source.stop()  # 跑完一轮（含日历续期检查）就收线，不真实 sleep
            return False

        def is_changing(self, _obj):
            return False

        def close(self):
            pass

    api = RenewingApi()
    source = TqQuoteSource(lambda: ("user", "pass"))
    monkeypatch.setattr(source, "_connect", lambda symbols: (api, {}))
    source._calendar = {"2026-10-01", "2026-10-02"}  # 只剩今天，前沿不足一周
    with service._state_lock:
        service._leases["SHFE.au2612"] = service._clock()  # 订阅集非空才会跑会话
    source._run(service)  # 同步跑正式 _run 入口：一轮建连 + 一轮续期检查后收线
    assert "2026-12-01" in source.trading_dates()
    assert api.calendar_calls == 1
    holder.now = 2539.0 + 3600.0 + 1.0  # 越过一小时冷却后仍不发起：命中的是覆盖充足分支
    assert source._calendar_refresh_due() is False


def test_calendar_refresh_failure_keeps_the_old_calendar_and_rate_limits(monkeypatch):
    holder = _fixed_paper_monotonic(monkeypatch, 0.0)  # 刚开机：monotonic 从 0 起
    _fixed_paper_datetime(monkeypatch, "2026-10-01")  # 覆盖判定不随真实墙钟漂移
    source = TqQuoteSource(lambda: ("user", "pass"))
    source._calendar = {"2026-10-01", "2026-10-02"}  # 前沿不足一周

    class BrokenCalendarApi:
        def get_trading_calendar(self, start_dt, end_dt):
            raise RuntimeError("probe_calendar_down")

    assert source._calendar_refresh_due() is True  # 从未尝试：首次立即发起（#708）
    source._refresh_calendar(BrokenCalendarApi())  # 失败不抛出
    assert source.trading_dates() == {"2026-10-01", "2026-10-02"}  # 旧日历保留
    holder.now = 3599.0
    assert source._calendar_refresh_due() is False  # 冷却期内不再发起
    holder.now = 3600.0
    assert source._calendar_refresh_due() is True  # 整 3600 秒后可再试
    source._calendar = {"2026-12-31"}  # 覆盖充足同样不发起
    holder.now = 7200.0
    assert source._calendar_refresh_due() is False


# -- 2026-10-08：结算价主动查询 ------------------------------------------------


def test_tq_quote_source_queries_settlements_for_a_deferred_account(service, monkeypatch):
    """18:00 换日后结算价未公布：行情源对 pending 合约 query_symbol_settlement，
    结果一到就喂给引擎结算（这是 18:00–夜盘开盘之间没有行情时的主通道）；
    结算完成后没有 pending 合约就不再查询。"""
    class SettlementApi:
        def __init__(self) -> None:
            self.calls = 0

        def get_quote(self, symbol):
            # 比引擎缓存（18:31）旧的快照：只验证它不会倒灌，不参与结算。
            fields = {key: value for key, value in au_quote(
                "2026-09-30 14:59:59.999500", 820.0).items() if key != "trading_time"}
            return SimpleNamespace(**fields, trading_time=None)

        def get_trading_calendar(self, start_dt, end_dt):
            return {"date": [], "trading": []}

        def query_symbol_settlement(self, symbols, days):
            self.calls += 1
            assert symbols == ["SHFE.au2612"]
            return SimpleNamespace(to_dict=lambda orient: [
                {"symbol": "SHFE.au2612", "datetime": "2026-11-02", "settlement": 825.0},
            ])

        def wait_update(self, deadline=None):
            time.sleep(0.02)
            return False

        def is_changing(self, _obj):
            return False

        def close(self):
            pass

    api = SettlementApi()
    source = TqQuoteSource(lambda: ("user", "pass"))
    monkeypatch.setattr(source, "_connect", lambda symbols: (api, {}))
    source._calendar = {"2026-12-31"}  # 日历覆盖充足，绕过续期分支
    source._settlement_attempt_at = 0.0
    feed(service, au_quote("09:31:00.000000", 820.0))
    service.submit(ACCOUNT, submit_request("order-settle-query"))
    feed(service, au_quote("18:31:00.000000", 820.5))
    assert service.pending_settlement_symbols() == {"SHFE.au2612"}
    source.start(service)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and service.settlement_count(ACCOUNT) == 0:
            time.sleep(0.05)
        assert service.settlement_count(ACCOUNT) == 1
        assert service.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-03"
        assert service.pending_settlement_symbols() == set()
        assert api.calls == 1
    finally:
        source.stop()


def test_settlement_rows_map_exchange_local_codes_back_to_requested_symbols(capsys):
    """#527 PR-3（PR #647 review r4219045042）：官方文档写 query_symbol_settlement 返回
    的 symbol 是“交易所内合约代码”（rb2701）。按请求的完整代码映射回去；服务端已带
    前缀的行原样认；映射不唯一（不同交易所同名合约同时在查）或不在请求里的行丢弃
    并记日志，不猜归属。"""
    requested = ["SHFE.rb2701", "CFFEX.IF2612", "SHFE.xx2612", "DCE.xx2612"]
    rows = [
        {"symbol": "rb2701", "datetime": "20261102", "settlement": 3080.0},
        {"symbol": "CFFEX.IF2612", "datetime": "2026-11-02", "settlement": 4000.2},
        {"symbol": "xx2612", "datetime": "20261102", "settlement": 1.0},
        {"symbol": "ag2612", "datetime": "20261102", "settlement": 2.0},
        {"symbol": "DCE.rb2701", "datetime": "20261102", "settlement": 3.0},
        {"symbol": "rb2701", "datetime": "20261102", "settlement": float("nan")},
    ]
    items = TqQuoteSource.settlement_items(requested, rows)
    assert items == [
        {"symbol": "SHFE.rb2701", "tradingDay": "2026-11-02", "settlement": 3080.0},
        {"symbol": "CFFEX.IF2612", "tradingDay": "2026-11-02", "settlement": 4000.2},
    ]
    logged = capsys.readouterr().err
    assert "xx2612(ambiguous)" in logged
    assert "ag2612(unrequested)" in logged and "DCE.rb2701(unrequested)" in logged


def test_unprefixed_settlement_rows_settle_night_and_day_only_holdings(service):
    """同一账户混持夜盘品种（SHFE.au2612）与无夜盘品种（CFFEX.IF2612）：18:31 换日后
    两个合约都等结算价；主动查询按官方形状只回交易所内代码，映射后两边都拿到
    11-02 的正式结算价，账户结算一次。原实现把 au2612/IF2612 原样喂引擎，永远
    对不上完整代码，整夜 paper_settlement_pending。"""
    index = au_quote("09:31:00.000000", 4000.0, symbol="CFFEX.IF2612",
                     margin=150_000.0, commission=30.0)
    index.update({"product_id": "IF", "volume_multiple": 300.0, "price_tick": 0.2,
                  "upper_limit": 4400.0, "lower_limit": 3600.0, "instrument_name": "沪深300指数2612",
                  "ask_price1": 4000.2, "bid_price1": 3999.8,
                  "trading_time": {"day": [["09:30:00", "11:30:00"], ["13:00:00", "15:00:00"]],
                                   "night": []}})
    feed(service, au_quote("09:31:00.000000", 820.0))
    feed(service, index)
    service.submit(ACCOUNT, submit_request("order-mix-au"))
    service.submit(ACCOUNT, submit_request(
        "order-mix-if", symbol="CFFEX.IF2612", limitPrice=4000.2))
    assert {item["symbol"] for item in service.positions(ACCOUNT)} == {"SHFE.au2612", "CFFEX.IF2612"}
    feed(service, au_quote("18:31:00.000000", 820.5))
    assert service.pending_settlement_symbols() == {"SHFE.au2612", "CFFEX.IF2612"}

    class SettlementApi:
        def query_symbol_settlement(self, symbols, days):
            assert symbols == ["CFFEX.IF2612", "SHFE.au2612"]
            return SimpleNamespace(to_dict=lambda orient: [
                {"symbol": "IF2612", "datetime": "20261102", "settlement": 4010.0},
                {"symbol": "au2612", "datetime": "20261102", "settlement": 825.0},
            ])

    TqQuoteSource(lambda: ("user", "pass"))._query_settlements(service, SettlementApi())
    assert service.drain(timeout=5.0)
    assert service.settlement_count(ACCOUNT) == 1
    assert service.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-03"
    assert service.pending_settlement_symbols() == set()
    # 结算后持仓价按各自正式结算价重置：au 825 × 1000、IF 4010 × 300 的浮盈已结转。
    assert service.account(ACCOUNT)["staticBalance"] == pytest.approx(
        1_000_000.0 - 20.0 - 30.0 + (825.0 - 820.02) * 1000 + (4010.0 - 4000.2) * 300)


def test_submit_validates_the_fee_rate_and_charges_it(http_service):
    """feeRate 严格校验（2026-10-08）：mode 只认 per_lot/notional、数值有限且 ≥0、多余键
    一律 422，不悄悄丢字段；合法费率随单生效——按费率计佣金，不是 quote 的每手手续费。"""
    engine, port = http_service
    with request(port, "/accounts", method="POST", payload={"accountId": ACCOUNT}) as response:
        assert json.load(response)["accountId"] == ACCOUNT
    # 缺字段的提交仍然 422（feeRate 只是新增可选键，没放松其余合同）。
    missing_volume = {key: value for key, value in submit_request("order-rate-missing").items()
                      if key != "volume"}
    try:
        request(port, f"/accounts/{ACCOUNT}/v1/simulation/orders/submit", method="POST",
                payload=missing_volume)
        raise AssertionError("submit missing a required field unexpectedly accepted")
    except urllib.error.HTTPError as error:
        assert error.code == 422
    for bad in (
        {"mode": "weird"},
        {"mode": "per_lot", "open": -1},
        {"mode": "per_lot", "open": float("nan")},
        {"mode": "per_lot", "surprise": 1},
        {"mode": "notional", "closeToday": "cheap"},
    ):
        try:
            request(port, f"/accounts/{ACCOUNT}/v1/simulation/orders/submit", method="POST",
                    payload=submit_request("order-rate-bad", feeRate=bad))
            raise AssertionError("invalid feeRate unexpectedly accepted")
        except urllib.error.HTTPError as error:
            assert error.code == 422

    engine.feed_quote(au_quote("09:31:00.000000", 820.0))
    assert engine.drain(timeout=5.0)
    rate = {"mode": "per_lot", "open": 5.0, "closeYesterday": 5.0, "closeToday": 5.0}
    with request(port, f"/accounts/{ACCOUNT}/v1/simulation/orders/submit", method="POST",
                 payload=submit_request("order-rate-good", feeRate=rate)) as response:
        accepted = json.load(response)
    assert accepted["order"]["isError"] is False
    with request(port, f"/accounts/{ACCOUNT}/v1/account") as response:
        account = json.load(response)
    assert account["commission"] == 5.0  # quote 每手是 20：按 feeRate 计，不是 20


def test_combined_snapshot_route_serves_one_fact_per_read(http_service):
    """/accounts/{id}/v1/snapshot (#700 review): status / account / positions / activity off
    ONE lock-held engine cut — the same shapes the four single reads answer with, still
    consistent after a fill, so a Runtime reader never pairs one request's activity with
    another request's positions."""
    engine, port = http_service
    with request(port, "/accounts", method="POST", payload={"accountId": ACCOUNT}) as response:
        assert json.load(response)["accountId"] == ACCOUNT
    engine.feed_quote(au_quote("09:31:00.000000", 820.0))
    assert engine.drain(timeout=5.0)
    with request(port, f"/accounts/{ACCOUNT}/v1/simulation/orders/submit", method="POST",
                 payload=submit_request("order-combined")) as response:
        assert json.load(response)["order"]["status"] == "FINISHED"

    def combined() -> dict:
        with request(port, f"/accounts/{ACCOUNT}/v1/snapshot") as response:
            return json.load(response)

    first = combined()
    assert first["schemaVersion"] == 1
    volatile = ("ageSeconds", "capturedAt")

    def settled(value):
        if not isinstance(value, dict):
            return value
        return {key: item for key, item in value.items() if key not in volatile}

    for part, path in (("status", "v1/status"), ("account", "v1/account"),
                       ("positions", "v1/positions"), ("activity", "v1/activity")):
        with request(port, f"/accounts/{ACCOUNT}/{path}") as response:
            single = json.load(response)
        assert settled(first[part]) == settled(single), part
    assert first["positions"] and first["positions"][0]["symbol"] == "SHFE.au2612"
    assert [trade["tradeId"] for trade in first["activity"]["trades"]] == \
        [trade["tradeId"] for trade in combined()["activity"]["trades"]]


# -- #527 B2：合约资料表批量刷新 --------------------------------------------------


class ContractServiceApi:
    """TqSdk 3.10.2 合约服务形状：query_symbol_info 的表格不带每手保证金/手续费，
    原始应答（future 片段带 margin/commission）留在 api._data["symbols"]，用 SDK
    自己的 _symbols_to_quotes 解析。"""

    def __init__(self) -> None:
        from tqsdk.utils_symbols import _symbols_to_quotes
        self._convert = _symbols_to_quotes
        self._data = {"symbols": {"PYSDK_api_probe": {"result": {"multi_symbol_info": [
            {"instrument_id": "SHFE.rb2701", "class": "FUTURE", "exchange_id": "SHFE",
             "product_id": "rb", "margin": 3_800.0, "commission": 0.31},
        ]}}}}

    def _symbols_to_quotes(self, symbols, keys=None):
        return self._convert(symbols, keys)

    def query_quotes(self, ins_class=None, expired=None):
        assert (ins_class, expired) == ("FUTURE", False)
        return ["SHFE.rb2701"]

    def query_symbol_info(self, symbols):
        assert symbols == ["SHFE.rb2701"]
        return SimpleNamespace(to_dict=lambda orient: [{
            "instrument_id": "SHFE.rb2701", "ins_class": "FUTURE", "product_id": "rb",
            "volume_multiple": 10.0, "price_tick": 1.0, "expire_datetime": 1_800_000_000.0,
            "delivery_year": 2027, "delivery_month": 1,
        }])


def _contract(engine: PaperEngine, symbol: str) -> dict:
    with engine._state_lock:
        return dict(engine._contracts.get(symbol) or {})


def test_contract_refresh_fills_the_table_from_the_official_contract_service(service):
    source = TqQuoteSource(lambda: ("user", "pass"))
    source._refresh_contracts(service, ContractServiceApi())
    assert service.drain(timeout=5.0)
    row = _contract(service, "SHFE.rb2701")
    assert (row["volume_multiple"], row["price_tick"], row["delivery_month"]) == (10.0, 1.0, 1)
    assert row["expire_datetime"] == 1_800_000_000.0 * 1e9  # 秒 → 纳秒
    assert (row["margin"], row["commission"]) == (3_800.0, 0.31)
    assert row["static_source"] == row["fee_source"] == "contract_service"
    assert service.health()["contracts"]["refreshError"] is None


def test_contract_refresh_failure_keeps_the_old_table(service):
    source = TqQuoteSource(lambda: ("user", "pass"))
    source._refresh_contracts(service, ContractServiceApi())
    assert service.drain(timeout=5.0)

    class BrokenApi:
        def query_quotes(self, ins_class=None, expired=None):
            raise RuntimeError("probe_contract_service_down")

    source._refresh_contracts(service, BrokenApi())
    assert service.drain(timeout=5.0)
    assert _contract(service, "SHFE.rb2701")["margin"] == 3_800.0  # 旧表照用
    assert "probe_contract_service_down" in service.health()["contracts"]["refreshError"]
    # 资料表补齐的合约照常下单（价格仍来自实时行情）。
    feed(service, {**au_quote("09:31:00.000000", 3071.0, symbol="SHFE.rb2701"),
                   "margin": float("nan"), "commission": float("nan"), "volume_multiple": 0.0,
                   "product_id": "", "price_tick": 1.0, "upper_limit": 3300.0, "lower_limit": 2800.0})
    report = service.submit(ACCOUNT, submit_request("order-rb-table", symbol="SHFE.rb2701",
                                                    limitPrice=3072.0))
    assert report["order"]["isError"] is False


def test_contract_refresh_runs_at_start_and_once_each_evening(monkeypatch):
    holder = _fixed_paper_monotonic(monkeypatch, 100.0)
    source = TqQuoteSource(lambda: ("user", "pass"))
    assert source._contract_refresh_due() is True  # 启动：从未刷新过
    holder.now = 200.0
    assert source._contract_refresh_due() is False  # 失败/进行中：一小时冷却
    holder.now = 100.0 + 3600.0
    assert source._contract_refresh_due() is True
    source._contract_refreshed_at = datetime.fromisoformat("2026-11-02T10:00:00+08:00")
    holder.now = 100.0 + 7200.0 + 1
    _fixed_paper_datetime(monkeypatch, "2026-11-02")  # 当天 12:00：还没到 20:00
    assert source._contract_refresh_due() is False

    class Evening(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-11-02T20:05:00+08:00")

    monkeypatch.setattr("tqkq_readonly_sidecar.paper.datetime", Evening)
    assert source._contract_refresh_due() is True  # 结算后、夜盘前刷新一次
    source._contract_refreshed_at = datetime.fromisoformat("2026-11-02T20:06:00+08:00")
    holder.now += 3600.0 + 1
    assert source._contract_refresh_due() is False  # 当晚已刷新


def test_contract_table_write_failure_fails_the_refresh_and_retries(service, monkeypatch):
    # #777 review：批次写表失败（磁盘满/只读）整批回滚，健康报刷新失败，行情源
    # 一小时后重试而不是等到当晚；实时行情回写失败仍不致命。
    holder = _fixed_paper_monotonic(monkeypatch, 100.0)
    _fixed_paper_datetime(monkeypatch, "2026-11-02")
    with service._state_lock:
        service._db.execute(
            "CREATE TRIGGER probe_full BEFORE INSERT ON paper_contracts "
            "BEGIN SELECT RAISE(ABORT, 'probe_disk_full'); END")
    source = TqQuoteSource(lambda: ("user", "pass"))
    assert source._contract_refresh_due(service) is True
    source._refresh_contracts(service, ContractServiceApi())
    assert service.drain(timeout=5.0)
    contracts = service.health()["contracts"]
    assert "probe_disk_full" in contracts["refreshError"] and contracts["refreshedAt"] is None
    assert _contract(service, "SHFE.rb2701") == {}
    holder.now = 100.0 + 3600.0 + 1
    assert source._contract_refresh_due(service) is True
    feed(service, au_quote("09:31:00.000000", 820.0))
    report = service.submit(ACCOUNT, submit_request("order-table-readonly", limitPrice=820.02))
    assert report["order"]["isError"] is False


# -- 合约名入口（2026-10-13 事故） -------------------------------------------------


def _leased(engine: PaperEngine) -> set[str]:
    with engine._state_lock:
        return set(engine._leases)


def test_quote_reads_spell_the_contract_the_exchange_way_before_the_lease(tmp_path):
    """10-13 production: a quote read for SHFE.RB2701 (upper-cased by the Runtime) went into the
    lease as given and broke the market subscription. With no contract table yet, the exchange's
    own case rule spells it; a name that is no futures contract is refused before the lease."""
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"), clock=clock,
                         trading_dates=TRADING_DATES, quote_wait_seconds=0.05)
    engine.open()
    engine.start()
    engine.create_account(ACCOUNT)
    try:
        for asked, spelled in (("SHFE.RB2701", "SHFE.rb2701"), ("czce.sr701", "CZCE.SR701"),
                               ("cffex.if2612", "CFFEX.IF2612"), ("gfex.LC2701", "GFEX.lc2701")):
            with pytest.raises(RuntimeError, match="market_quote_not_ready"):
                engine.market_quote(ACCOUNT, asked)
            assert spelled in engine.wanted_symbols()
        for bad in ("NYMEX.CL2612", "rb2701", "SHFE.rb", "SHFE.rb2701C3000", ""):
            with pytest.raises(UnknownSymbolError):
                engine.market_quote(ACCOUNT, bad)
        assert _leased(engine) == {"SHFE.rb2701", "CZCE.SR701", "CFFEX.IF2612", "GFEX.lc2701"}
        # An order names the contract through the same entry: the book only ever holds SHFE.au2612.
        feed(engine, au_quote("09:31:00.000000", 820.0))
        order = engine.submit(ACCOUNT, submit_request("order-upper", symbol="shfe.AU2612"))["order"]
        assert order["status"] == "FINISHED" and order["symbol"] == "SHFE.au2612"
        assert "SHFE.AU2612" not in engine.wanted_symbols()
    finally:
        engine.close()


def test_a_refreshed_contract_table_refuses_unknown_contracts_over_http(http_service):
    """Once the contract service has filled the table (every unexpired future), a name outside it
    is no contract: 404 symbol_not_found (the read-only sidecar's answer), nothing leased; a known
    contract in any case is read under the table's spelling."""
    engine, port = http_service
    engine.create_account(ACCOUNT)
    source = TqQuoteSource(lambda: ("user", "pass"))
    source._refresh_contracts(engine, ContractServiceApi())
    assert engine.drain(timeout=5.0)
    for name in ("SHFE.RB2799", "SHFE.rb2799", "NYMEX.CL2612"):
        try:
            request(port, f"/accounts/{ACCOUNT}/v1/market/{name}/quote")
            raise AssertionError(f"{name} unexpectedly served")
        except urllib.error.HTTPError as error:
            assert error.code == 404 and json.load(error) == {"error": "symbol_not_found"}
    assert _leased(engine) == set()
    # A well-formed name outside the table may be a contract listed after the refresh: the source
    # is asked once to refresh the table early (at most hourly, like a failed refresh's retry).
    assert engine.take_contract_miss() is True and engine.take_contract_miss() is False
    source._contract_attempt_at = time.monotonic() - source.CONTRACT_RETRY_SECONDS - 1
    try:
        request(port, f"/accounts/{ACCOUNT}/v1/market/SHFE.rb2711/quote")
    except urllib.error.HTTPError as error:
        assert error.code == 404
    assert source._contract_refresh_due(engine) is True and source._contract_refresh_due(engine) is False
    feed(engine, au_quote("09:31:00.000000", 3_500.0, symbol="SHFE.rb2701"))
    with request(port, f"/accounts/{ACCOUNT}/v1/market/SHFE.RB2701/quote") as response:
        assert json.load(response)["symbol"] == "SHFE.rb2701"
    assert _leased(engine) == {"SHFE.rb2701"}
    with request(port, "/healthz") as response:
        assert json.load(response)["rejectedSymbols"] == []


def _asks_the_source(engine: PaperEngine, name: str, spelled: str) -> None:
    """表外、形状对的名字没有被拒：按交易所规则写好进租约，等行情源订阅判断。"""
    with pytest.raises(RuntimeError, match="market_quote_not_ready"):
        engine.market_quote(ACCOUNT, name)
    assert spelled in _leased(engine)


def test_only_a_fresh_refresh_in_this_process_makes_the_table_a_complete_catalog(tmp_path):
    """#842 Codex P2：整表刷新过一次不等于永远完整。重启后库里的旧表、超过一天没刷新
    成功的表都不能把新挂牌的合约答成不存在（否则最多等一小时提前刷新）；合约服务列出
    但资料取不全的合约也算存在。形状不对的名字照样当场拒绝。"""
    db = str(tmp_path / "paper.sqlite3")
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = PaperEngine(db_path=db, clock=clock, trading_dates=TRADING_DATES,
                         quote_wait_seconds=0.05)
    engine.open()
    engine.start()
    engine.create_account(ACCOUNT)
    try:
        TqQuoteSource(lambda: ("user", "pass"))._refresh_contracts(engine, ContractServiceApi())
        assert engine.drain(timeout=5.0)
        with pytest.raises(UnknownSymbolError):
            engine.market_quote(ACCOUNT, "SHFE.rb2711")
        # 列出了、资料没进表（query_symbol_info 缺这一行）的合约：存在。
        engine.feed_contracts([], listed=["SHFE.rb2701", "SHFE.rb2711"])
        assert engine.drain(timeout=5.0)
        _asks_the_source(engine, "SHFE.RB2711", "SHFE.rb2711")
        # 刷新超过一天没再成功：旧表不算完整目录。
        clock.advance(datetime.fromisoformat("2026-11-03T10:01:00+08:00"))
        _asks_the_source(engine, "shfe.rb2712", "SHFE.rb2712")
        with pytest.raises(UnknownSymbolError):
            engine.market_quote(ACCOUNT, "NYMEX.CL2612")
    finally:
        engine.close()
    restarted = PaperEngine(db_path=db, clock=VirtualClock(
        datetime.fromisoformat("2026-11-02T09:30:00+08:00")), trading_dates=TRADING_DATES,
        quote_wait_seconds=0.05)
    restarted.open()
    restarted.start()
    try:
        # 库里的表是上一个进程刷新的：本进程还没刷新前，表外的新合约交给行情源判断。
        assert _contract(restarted, "SHFE.rb2701")["static_source"] == "contract_service"
        _asks_the_source(restarted, "SHFE.rb2711", "SHFE.rb2711")
    finally:
        restarted.close()


# -- 强平 (#1204) ---------------------------------------------------------------


def test_an_account_at_full_risk_is_force_closed_largest_margin_first_before_it_goes_negative(service):
    """所有者 2026-10-30：风险度（保证金 ÷ 权益）到 100% 就强平，不等权益变负——先撤挂单，再对价平掉刚好够把
    风险度压回 100% 以下的手数；强平单号带前缀、成交带 forcedLiquidation，下一笔行情不再重复强平。"""
    feed(service, au_quote("09:31:00.000000", 820.0))
    rate = {"mode": "per_lot", "open": 5.0, "closeYesterday": 6.0, "closeToday": 7.0}
    filled = service.submit(ACCOUNT, submit_request("open-12", volume=12, feeRate=rate))
    assert filled["order"]["status"] == "FINISHED" and service.positions(ACCOUNT)[0]["volume"] == 12
    resting = service.submit(ACCOUNT, submit_request("resting-sell", direction="SELL", offset="CLOSETODAY", limitPrice=850.0))
    assert resting["order"]["status"] == "ALIVE"
    feed(service, au_quote("09:32:00.000000", 812.0))   # 权益 100 万 − 12 × 8,000 ≈ 90.4 万 < 保证金 96 万
    activity = service.activity(ACCOUNT)
    forced = [order for order in activity["orders"] if order["clientOrderId"].startswith("paper-force-")]
    assert len(forced) == 1 and forced[0]["offset"] == "CLOSETODAY" and forced[0]["direction"] == "SELL"
    assert {order["clientOrderId"]: order["status"] for order in activity["orders"]}["resting-sell"] == "FINISHED"
    trades = [trade for trade in activity["trades"] if trade.get("forcedLiquidation")]
    assert sum(int(trade["volume"]) for trade in trades) == 1   # 超出 ≈ 5.6 万 < 一手 8 万保证金：平 1 手就够
    assert service.positions(ACCOUNT)[0]["volume"] == 11
    account = service.account(ACCOUNT)
    assert account["margin"] < account["equity"]
    # #1210: the forced close pays the fee rate its position was opened with (平今 7), not the quote's 20 per lot.
    assert account["commission"] == 12 * 5.0 + 7.0
    feed(service, au_quote("09:33:00.000000", 812.0))
    assert len([o for o in service.activity(ACCOUNT)["orders"] if o["clientOrderId"].startswith("paper-force-")]) == 1


def test_reset_cancels_orders_clears_positions_restores_the_initial_balance_and_survives_a_restart(tmp_path):
    """账户重置 (#1204，所有者 2026-10-30 定 1b1e 恢复 100 万)：确认号对不上拒绝；撤挂单、清持仓、权益回初始，
    今天的成交留着，重置作为一条 reset 命令行落库，重启后仍是重置后的账。"""
    db = str(tmp_path / "paper.sqlite3")
    first = PaperEngine(db_path=db, clock=VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00")),
                        trading_dates=TRADING_DATES)
    first.open()
    first.start()
    first.create_account(ACCOUNT)
    feed(first, au_quote("09:31:00.000000", 820.0))
    first.submit(ACCOUNT, submit_request("open-3", volume=3))
    first.submit(ACCOUNT, submit_request("resting", direction="SELL", offset="CLOSETODAY", limitPrice=850.0))
    feed(first, au_quote("09:32:00.000000", 815.0))
    with pytest.raises(RuntimeError, match="confirm_mismatch"):
        first.reset_account(ACCOUNT, SECOND)
    result = first.reset_account(ACCOUNT, ACCOUNT)
    assert result["status"] == "reset" and result["cancelledOrders"] == 1 and result["balance"] == 1_000_000.0
    assert result["clearedPositions"] == [{"symbol": "SHFE.au2612", "direction": "long", "volume": 3}]
    assert first.positions(ACCOUNT) == [] and first.account(ACCOUNT)["equity"] == 1_000_000.0
    assert {trade["volume"] for trade in first.activity(ACCOUNT)["trades"]} == {3}   # 今天的成交还在
    first.close()
    kinds = [row[0] for row in sqlite3.connect(db).execute(
        "SELECT kind FROM paper_commands WHERE account_id=? ORDER BY rowid", (ACCOUNT,))]
    assert kinds[-1] == "reset"
    restarted = PaperEngine(db_path=db, clock=VirtualClock(datetime.fromisoformat("2026-11-02T10:00:00+08:00")),
                            trading_dates=TRADING_DATES)
    restarted.open()
    restarted.start()
    assert restarted.positions(ACCOUNT) == [] and restarted.account(ACCOUNT)["equity"] == 1_000_000.0
    restarted.close()


def test_reset_route_needs_the_matching_confirmation(http_service):
    engine, port = http_service
    engine.create_account(ACCOUNT)
    path = f"/accounts/{ACCOUNT}/reset"
    with pytest.raises(urllib.error.HTTPError) as mismatch:
        request(port, path, method="POST", payload={"confirmAccountId": SECOND})
    assert mismatch.value.code == 409 and json.loads(mismatch.value.read())["error"] == "confirm_mismatch"
    with pytest.raises(urllib.error.HTTPError) as unknown:
        request(port, f"/accounts/{SECOND}/reset", method="POST", payload={"confirmAccountId": SECOND})
    assert unknown.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as unauthorized:
        request(port, path, token=None, method="POST", payload={"confirmAccountId": ACCOUNT})
    assert unauthorized.value.code == 401
    body = json.loads(request(port, path, method="POST", payload={"confirmAccountId": ACCOUNT}).read())
    assert body["status"] == "reset" and body["balance"] == 1_000_000.0

"""新浪备用行情 (2026-10-07): protects that Sina stands in only when every TqKq source is
down — never during a closed market on a connected source — that everything it serves is
marked `backup`, that it switches back by itself, and that no order, stop, alert or
strategy gate ever accepts a backup price. No network: Sina answers from canned text."""
from __future__ import annotations

import asyncio
import copy
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from test_market_source_relay import FIRST, SECOND, RelaySource, relay  # noqa: F401 — the fixture
from test_movement_alerts import START, moving_workbench
from test_order_protection import Provider, _direct_stop, _fresh, _long_items
from test_simulation_orders import USER, Gateway, _tool_status, _tool_text, application
from test_strategy_workbench import SYMBOL, bars, plan
from trader_runtime.host_throttle import HostThrottle
from trader_runtime.market_briefs import market_movement
from trader_runtime.order_boundaries import SimulationOrderError, quote_is_fresh
from trader_runtime.price_alerts import rows_to_quotes
from trader_runtime.shared_market import SOURCE_SIGNAL_SECONDS
from trader_runtime.sina_market import (
    BACKUP_NOTICE,
    BACKUP_SOURCE,
    SinaBackup,
    aggregate_bars,
    board_row,
    from_sina_code,
    parse_daily_bars,
    parse_minute_bars,
    parse_quotes,
    three_minute_bucket,
    to_sina_code,
    week_bucket,
)
from trader_runtime.strategy_rules import StrategyRuleError, evaluate_plan
from trader_runtime.trader_tools import TraderReadOnlyTools

NOW = datetime(2026, 9, 30, 7, 0, 5, tzinfo=UTC)  # 15:00:05 Beijing, just after the close
# Live answers captured 2026-10-07 (the holiday keeps them at the 09-30 close).
AU = ('var hq_str_nf_AU2612="黄金2612,150000,901.500,911.260,900.260,910.580,910.300,910.580,910.580,905.040,'
      '897.860,1,3,225591.000,184360,沪,黄金,2026-09-30,1,,,,,,,,,905.051,910.200,1,910.120,1,910.000,3,'
      '909.860,2,910.900,4,910.940,4,911.000,4,911.020,2";')
RB = ('var hq_str_nf_RB2610="螺纹钢2610,150000,3080.000,3090.000,3070.000,3085.000,3084.000,3086.000,3085.000,'
      '3080.000,3075.000,407,60,1592266.000,638932,沪,螺纹钢,2026-09-30,1,,,,,,,,,3080.824,0.000,0,0.000,0,'
      '0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0";')
AP = ('var hq_str_nf_AP2701="苹果2701,150000,6952.000,6952.000,6765.000,6854.000,6850.000,6856.000,6854.000,'
      '6834.000,6956.000,13,4,142083.000,141807,郑,苹果,2026-09-30,1,,,,,,,,,6834.000,0.000,0,0.000,0,0.000,0,'
      '0.000,0,0.000,0,0.000,0,0.000,0,0.000,0";')
IF = ('var hq_str_nf_IF2610="4349.600,4364.200,4334.000,4353.000,19842,86279044.400,49879.000,4353.000,0.000,'
      '4775.200,3907.200,0.000,0.000,4334.800,4341.200,51603.000,4352.800,1,0.000,0,0.000,0,0.000,0,0.000,0,'
      '4354.000,1,0.000,0,0.000,0,0.000,0,0.000,0,2026-09-30,15:00:00,400,0,,,,,,,,,4348.304,沪深300指数期货2610";')
EMPTY = 'var hq_str_nf_ZZ2612="";'
MINUTES = ('/*<script>location.href=\'//sina.com\';</script>*/\nvar t=([{"d":"2026-09-30 14:55:00","o":"910.540",'
           '"h":"910.760","l":"910.360","c":"910.420","v":"455","p":"225703"},{"d":"2026-09-30 15:00:00",'
           '"o":"910.420","h":"910.800","l":"910.400","c":"910.580","v":"83","p":"225591"}]);')
DAILY = ('var t=([{"d":"2026-09-28","o":"918.940","h":"918.980","l":"903.940","c":"904.660","v":"150699",'
         '"p":"226497","s":"912.060"},{"d":"2026-09-29","o":"901.040","h":"903.420","l":"892.620","c":"898.780",'
         '"v":"237443","p":"225820","s":"897.860"},{"d":"2026-09-30","o":"901.500","h":"911.260","l":"900.260",'
         '"c":"910.580","v":"184360","p":"225591","s":"905.040"}]);')


class FakeSina:
    """Sina's three endpoints from canned text; `down` makes every request fail."""

    def __init__(self, *, fresh: bool = False) -> None:
        self.urls: list[str] = []
        self.down = False
        self.fresh = fresh  # stamp quotes with the current Beijing time instead of the 09-30 close

    async def fetch(self, url: str) -> bytes:
        self.urls.append(url)
        if self.down:
            raise OSError("sina unreachable")
        if "hq.sinajs.cn" in url:
            known = {"AU2612": AU, "RB2610": RB, "AP2701": AP, "IF2610": IF}
            if self.fresh:
                beijing = datetime.now(UTC) + timedelta(hours=8)
                known["AU2612"] = AU.replace("150000", beijing.strftime("%H%M%S")).replace(
                    "2026-09-30", beijing.strftime("%Y-%m-%d"))
            codes = url.split("list=", 1)[1].split(",")
            return "\n".join(known.get(code.removeprefix("nf_"), f'var hq_str_{code}="";')
                             for code in codes).encode("gbk")
        return (DAILY if "getDailyKLine" in url else MINUTES).encode()


def install(service, sina: FakeSina) -> None:
    service.backup = SinaBackup(sina.fetch, throttle=HostThrottle(0, (0, 0)),
                                now=(lambda: datetime.now(UTC)) if sina.fresh else (lambda: NOW))
    service.backup_interval = 0.02


def fail_all(relay_ns, reason: str = "pool_down") -> None:
    for source in relay_ns.sources.values():
        source.fail_reason = reason


def report_lost(relay_ns, lost: dict[str, bool]) -> None:
    """Each source answers HTTP fine but, while `lost[source]`, says its TqKq connection is
    lost (the sidecar's stale packets: public_market_disconnected on the workbench)."""
    for account_id, source in relay_ns.sources.items():
        read = source.read

        async def reporting(path, read=read, account_id=account_id):
            result = await read(path)
            if not lost[account_id]:
                return result
            if "/workbench" in path:
                return {**result, "dataStatus": "stale", "dataErrorCode": "public_market_disconnected"}
            return {**result, "dataStatus": "stale"}
        source.read = reporting


async def until(predicate, seconds: float = 3.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


# -- parsing and symbols ------------------------------------------------------------------

def test_quote_lines_parse_commodity_depth_czce_cffex_and_skip_empty_codes() -> None:
    quotes = parse_quotes("\n".join([AU, AP, IF, EMPTY]), cffex_codes={"IF2610"}, now=NOW)
    assert set(quotes) == {"AU2612", "AP2701", "IF2610"}  # an unknown contract answers ""
    au = quotes["AU2612"]
    assert (au["lastPrice"], au["preSettlement"], au["open"], au["highest"], au["lowest"]) == (910.58, 897.86, 901.5, 911.26, 900.26)
    assert (au["bidPrice1"], au["askPrice1"], au["volume"], au["openInterest"]) == (910.3, 910.58, 184360, 225591)
    assert (au["bidPrice5"], au["askPrice5"]) == (909.86, 911.02)  # 上期所五档
    assert au["quoteDatetime"] == "2026-09-30T15:00:00+08:00"
    assert quotes["AP2701"]["lastPrice"] == 6854 and "bidPrice2" not in quotes["AP2701"]  # 郑商所无五档
    cffex = quotes["IF2610"]
    assert (cffex["lastPrice"], cffex["preSettlement"], cffex["upperLimit"], cffex["lowerLimit"]) == (4353, 4341.2, 4775.2, 3907.2)
    assert (cffex["bidPrice1"], cffex["askPrice1"], cffex["name"]) == (4352.8, 4354, "沪深300指数期货2610")


def test_backup_board_row_withholds_a_last_outside_the_day_range(caplog: pytest.LogCaptureFixture) -> None:
    """#806: the backup board keeps the sidecar's guard — a 最新价 outside 最高/最低 is not shown, once logged."""
    item = {"exchangeId": "SHFE", "productId": "au", "instrumentId": "au2612", "instrumentName": "沪金2612"}
    raw = {"lastPrice": 985.52, "preSettlement": 899.10, "highest": 913.0, "lowest": 903.72,
           "quoteDatetime": "2026-10-12 15:00:00"}
    with caplog.at_level("WARNING", logger="trader_runtime.sina_market"):
        row = board_row("SHFE.au2612", raw, item)
        board_row("SHFE.au2612", raw, item)
    assert row["status"] == "market_quote_unavailable" and row["last"] is None and row["change"] is None
    assert [record.getMessage() for record in caplog.records].count(
        "sina_board_quote_inconsistent symbol=SHFE.au2612 last=985.52 high=913.0 low=903.72") == 1
    good = board_row("SHFE.au2612", {**raw, "lastPrice": 905.52}, item)
    assert (good["status"], good["last"], good["change"], good["changePercent"]) == ("normal", 905.52, 6.42, 0.71)
    # #823: an inverted day range (最高 below 最低) is an impossible quote as well, never a normal row.
    inverted = board_row("SHFE.au2612", {**raw, "lastPrice": 905.52, "highest": 903.72, "lowest": 913.0}, item)
    assert inverted["status"] == "market_quote_unavailable" and inverted["last"] is None


def test_symbols_map_to_sina_and_back_with_the_czce_decade_inferred() -> None:
    cases = {"SHFE.au2612": "AU2612", "INE.sc2611": "SC2611", "DCE.m2701": "M2701", "GFEX.si2701": "SI2701",
             "CZCE.AP701": "AP2701", "CZCE.SR609": "SR2609", "CFFEX.IF2610": "IF2610"}
    for symbol, code in cases.items():
        assert to_sina_code(symbol, 2026) == code
        assert from_sina_code(code, symbol.split(".")[0]) == symbol
    assert to_sina_code("CZCE.AP001", 2029) == "AP3001"  # 2029 年的 0 是 2030
    assert to_sina_code("KQ.m@SHFE.au", 2026) is None and to_sina_code("SHFE.au", 2026) is None


def test_bars_restamp_to_the_start_and_aggregate_three_minutes_and_weeks() -> None:
    minutes = parse_minute_bars(MINUTES, 5)
    assert [datetime.fromtimestamp(b["datetime"] / 1e9, UTC).strftime("%H:%M") for b in minutes] == ["06:50", "06:55"]
    assert minutes[-1]["close"] == 910.58 and minutes[-1]["openInterest"] == 225591
    daily = parse_daily_bars(DAILY)
    assert [b["tradingDay"] for b in daily] == ["2026-09-28", "2026-09-29", "2026-09-30"]
    assert datetime.fromtimestamp(daily[0]["datetime"] / 1e9, UTC).hour == 16  # 00:00 Beijing
    weekly = aggregate_bars(daily, week_bucket)
    assert len(weekly) == 1 and (weekly[0]["open"], weekly[0]["high"], weekly[0]["low"], weekly[0]["close"]) == (918.94, 918.98, 892.62, 910.58)
    assert weekly[0]["volume"] == 150699 + 237443 + 184360 and "tradingDay" not in weekly[0]
    three = aggregate_bars(parse_minute_bars(MINUTES, 1), three_minute_bucket)
    assert len(three) == 2  # 14:54 and 14:59 fall in different 3-minute buckets
    assert parse_minute_bars("var t=(null);", 1) == [] and parse_daily_bars("") == []


# -- the switch ---------------------------------------------------------------------------

def test_backup_reads_share_requests_and_a_failure_is_not_repeated_inside_the_retry() -> None:
    """#976 第 7 步：新浪备用走统一采集——同时到期的合约合成一次报价请求、还新鲜的不再请求；新浪失败时
    不拿旧价当新价（照旧答不可用），重试间隔内不再请求；没人看的合约过一段时间从存储里清掉。"""
    from trader_runtime.sina_market import IDLE_SECONDS, QUOTE_TTL, SinaUnavailable

    async def scenario() -> None:
        sina = FakeSina()
        clock = [1000.0]
        backup = SinaBackup(sina.fetch, throttle=HostThrottle(0, (0, 0)), clock=lambda: clock[0], now=lambda: NOW)
        first, second = await asyncio.gather(backup.quotes(["SHFE.au2612", "SHFE.rb2610"]),
                                             backup.quotes(["SHFE.rb2610", "CZCE.AP701"]))
        quote_urls = [url for url in sina.urls if "hq.sinajs.cn" in url]
        assert len(quote_urls) == 2 and "nf_RB2610" not in quote_urls[1]  # rb joined the first request
        assert set(first) == {"SHFE.au2612", "SHFE.rb2610"} and set(second) == {"SHFE.rb2610", "CZCE.AP701"}
        await backup.quotes(["SHFE.au2612"])
        assert len([url for url in sina.urls if "hq.sinajs.cn" in url]) == 2  # still fresh: no request

        clock[0] += QUOTE_TTL + 0.1
        sina.down = True
        with pytest.raises(SinaUnavailable, match="sina_quote_unavailable"):
            await backup.quotes(["SHFE.au2612"])  # an old Sina price is never served as fresh
        asked = len(sina.urls)
        with pytest.raises(SinaUnavailable):
            await backup.quotes(["SHFE.au2612"])
        assert len(sina.urls) == asked  # inside the retry interval: no new request
        with pytest.raises(SinaUnavailable, match="sina_bars_unavailable"):
            await backup.bars("SHFE.au2612", 300)
        sina.down = False
        clock[0] += QUOTE_TTL + 0.1
        assert (await backup.quotes(["SHFE.au2612"]))["SHFE.au2612"]["lastPrice"] == 910.58

        clock[0] += IDLE_SECONDS + 1
        await backup.quotes(["SHFE.au2612"])  # rb and ap were not read for IDLE_SECONDS: they leave the store
        assert backup.kept.get("quote:SHFE.rb2610") is None and backup.kept.get("quote:CZCE.AP701") is None
        assert backup.kept.get("quote:SHFE.au2612") is not None
        await backup.close()

    asyncio.run(scenario())


def test_a_cancelled_backup_read_still_leaves_its_contracts_evictable() -> None:
    from trader_runtime.sina_market import IDLE_SECONDS

    async def scenario() -> None:
        sina, release = FakeSina(), asyncio.Event()
        clock = [1000.0]

        async def slow(url: str) -> bytes:
            await release.wait()
            return await sina.fetch(url)

        backup = SinaBackup(slow, throttle=HostThrottle(0, (0, 0)), clock=lambda: clock[0], now=lambda: NOW)
        reader = asyncio.create_task(backup.quotes(["SHFE.rb2610"]))
        await asyncio.sleep(0.01)
        reader.cancel()  # the reader gives up; the request it started still finishes and is stored
        release.set()
        await asyncio.sleep(0.01)
        assert backup.kept.get("quote:SHFE.rb2610") is not None
        clock[0] += IDLE_SECONDS + 1
        await backup.quotes(["SHFE.au2612"])
        assert backup.kept.get("quote:SHFE.rb2610") is None  # unread for IDLE_SECONDS: evicted
        await backup.close()

    asyncio.run(scenario())


async def test_every_source_down_serves_board_quotes_and_charts_from_sina_then_switches_back(relay, monkeypatch) -> None:
    service, sina = relay.service, FakeSina()
    install(service, sina)
    changes: list[bool] = []
    service.backup_listeners.append(lambda: changes.append(service.backup_active))
    fail_all(relay)

    board = await service.board()
    assert (board["dataStatus"], board["source"]) == ("backup", BACKUP_SOURCE)
    au = next(row for row in board["rows"] if row["symbol"] == "SHFE.au2612")
    assert (au["last"], au["preSettlement"], au["changePercent"], au["status"]) == (910.58, 897.86, 1.42, "normal")
    quotes = await service.quotes(["SHFE.au2612"])
    assert quotes["dataStatus"] == "backup" and quotes["rows"][0]["last"] == 910.58

    channel = await service.acquire_market("SHFE.au2612", 300)
    try:
        workbench = channel.packet["value"]["workbench"]
        assert (workbench["dataStatus"], workbench["source"], workbench["quote"]["dataStatus"]) == ("backup", BACKUP_SOURCE, "backup")
        assert workbench["quote"]["lastPrice"] == 910.58 and workbench["quote"]["marketSessionStatus"] == "closed"
        assert len(workbench["bars"]) == 2 and workbench["liveTrading"] == "disabled"
        snapshot = await service.snapshot("SHFE.au2612")
        assert snapshot["dataStatus"] == "backup" and snapshot["summary"] == BACKUP_NOTICE
        assert service.backup_active and changes == [True]
        # A contract Sina does not carry fails on its own, as before; the market stays on backup.
        with pytest.raises(HTTPException) as missing:
            await service.acquire_market("CFFEX.IF2609", 300)
        assert missing.value.detail == "guest_market_source_unavailable"
        assert service.backup_active and changes == [True]

        # runtime.status reads `backup` wherever a client sees it, and only over a non-normal status.
        provider = relay.app.state.data_provider
        original = provider.runtime_status
        monkeypatch.setattr(provider, "runtime_status", lambda: {**original(), "marketDataStatus": "unavailable"})
        response = await relay.owner.get("/api/runtime/status", params={"mode": "cloud"})
        status = response.json()
        assert response.status_code == 200, status
        assert (status["marketDataStatus"], status["marketDataSource"]) == ("backup", BACKUP_SOURCE)
        assert service.present_status({"marketDataStatus": "normal"}) == {"marketDataStatus": "normal"}
        session = relay.app.state.broker.create_session("cloud", "native", "alice")
        published = await relay.app.state.broker.publish(
            session.session_id, kind="result", name="runtime.status",
            payload={**(await relay.owner.get("/api/runtime/status", params={"mode": "cloud"})).json(),
                     "marketDataStatus": "unavailable"})
        assert published["payload"]["marketDataStatus"] == "backup"
        transient = await relay.app.state.broker.publish_transient(  # the stream's first status on connect
            session.session_id, kind="result", name="runtime.status", payload=published["payload"] | {"marketDataStatus": "unavailable"})
        assert transient["payload"]["marketDataStatus"] == "backup"

        # Any source back: the next read is TqKq again, by itself.
        for source in relay.sources.values():
            source.fail_reason = None
        offset = 120.0
        monkeypatch.setattr(service, "clock", lambda: time.time() + offset)  # past every backoff
        service.cache.clear()
        board = await service.board()
        assert board["dataStatus"] != "backup" and "source" not in board
        await until(lambda: channel.packet["value"]["workbench"]["dataStatus"] == "normal")
        assert not service.backup_active and changes == [True, False]
        assert service.present_status({"marketDataStatus": "unavailable"}) == {"marketDataStatus": "unavailable"}
    finally:
        service.release_market(channel)


async def test_a_connected_source_in_a_closed_market_never_switches(relay) -> None:
    """休市：快期连着但没有新数据——照旧读快期，新浪一次都不请求。"""
    service, sina = relay.service, FakeSina()
    install(service, sina)
    for source in relay.sources.values():
        read = source.read

        async def closed(path, read=read):
            result = await read(path)
            if "/workbench" in path:
                result = {**result, "marketSessionStatus": "closed",
                          "quote": {**result["quote"], "marketSessionStatus": "closed", "dataErrorCode": "market_quote_stale"}}
            return result
        source.read = closed
    board = await service.board()
    channel = await service.acquire_market("SHFE.au2612", 300)
    try:
        await asyncio.sleep(0.1)
        assert board["dataStatus"] != "backup" and channel.packet["value"]["workbench"]["dataStatus"] == "normal"
        assert sina.urls == [] and not service.backup_active
    finally:
        service.release_market(channel)


async def test_a_runtime_restarted_during_the_outage_names_contracts_from_the_saved_catalog(relay, monkeypatch) -> None:
    service, sina = relay.service, FakeSina()
    install(service, sina)
    await service.instruments()  # a healthy read leaves the catalog on disk
    assert service._catalog_file is not None and service._catalog_file.exists()
    service._last_catalog = None  # as after a restart
    service.cache.clear()

    def unreadable():
        raise RuntimeError("sidecar down")
    monkeypatch.setattr(relay.app.state.data_provider, "instrument_catalog", unreadable)
    fail_all(relay)
    board = await service.board()
    assert board["dataStatus"] == "backup" and any(row["symbol"] == "SHFE.au2612" for row in board["rows"])


async def test_sources_reporting_a_lost_tqkq_connection_count_as_down_only_when_all_do(relay) -> None:
    service, sina = relay.service, FakeSina()
    install(service, sina)
    lost = {FIRST: True, SECOND: False}
    report_lost(relay, lost)

    board = await service.board()  # the first source is lost: the second, healthy, serves the read
    assert board["dataStatus"] not in {"stale", "backup"} and sina.urls == []
    assert relay.sources[SECOND].calls["/v1/public-market/board"] == 1
    lost[SECOND] = True
    service.source_states[SECOND].observed_at = 0  # its healthy reading has aged out
    service.cache.clear()
    board = await service.board()
    assert board["dataStatus"] == "backup" and sina.urls


async def test_when_sina_fails_too_the_original_unavailable_answer_stands(relay) -> None:
    service, sina = relay.service, FakeSina()
    install(service, sina)
    sina.down = True
    fail_all(relay)
    with pytest.raises(HTTPException) as board:
        await service.board()
    assert (board.value.status_code, board.value.detail) == (503, "guest_market_source_unavailable")
    with pytest.raises(HTTPException) as chart:
        await service.acquire_market("SHFE.au2612", 300)
    assert chart.value.detail == "guest_market_source_unavailable"
    assert sina.urls and not service.backup_active


async def test_the_ai_tools_say_backup_every_time(relay) -> None:
    service, sina = relay.service, FakeSina()
    install(service, sina)
    fail_all(relay)
    tools = TraderReadOnlyTools(relay.app.state.data_provider, lambda mode: {})
    tools.app_capabilities = SimpleNamespace(app=relay.app)
    for tool, arguments in (("get_market_snapshot", {"symbol": "SHFE.au2612"}),
                            ("get_market_board", {})):
        result, _ = await tools.shared_market_tool(tool, arguments, turn_id="t-backup", user_id="alice")
        payload = _tool_text(result)
        assert payload["warning"] == BACKUP_NOTICE, (tool, payload)


# -- nothing executes on a backup price ----------------------------------------------------

def test_order_stop_and_breakeven_freshness_gate_refuses_backup() -> None:
    market = {"dataStatus": "backup", "isCached": False, "ageSeconds": 1,
              "quoteDatetime": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "marketSessionStatus": "trading"}
    quote_is_fresh({**market, "dataStatus": "normal"}, allow_fixture=False)
    with pytest.raises(SimulationOrderError, match="quote_not_current"):
        quote_is_fresh(market, allow_fixture=True)


@pytest.mark.parametrize("tool", ["propose_order", "get_futures_decision_context"])
def test_trade_proposal_and_decision_context_refuse_backup(tmp_path: Path, tool: str) -> None:
    app = application(tmp_path / f"backup-{tool}.sqlite3", Gateway())
    session = app.state.broker.create_session("local", "native", USER)
    status = _tool_status(app)
    tools = TraderReadOnlyTools(app.state.data_provider, lambda mode: {**status(mode), "marketDataStatus": "backup"},
                                proposals=app.state.proposals)
    arguments = {"symbol": "SHFE.au2612"} if tool != "propose_order" else {
        "act": "open", "symbol": "SHFE.au2612", "dir": "long", "vol": 1, "price": "queue", "reason": "开多"}
    result, event = tools.call(tool=tool, arguments=arguments, turn_id="turn-backup", mode="local",
                               session_id=session.session_id)
    assert result["success"] is False and event is None
    assert _tool_text(result)["errorCode"] == "marketDataStatus_unavailable"
    assert app.state.store.recent_proposals(session.session_id, limit=5) == []
    app.state.store.close()


class BackupQuoteProvider(Provider):
    def __init__(self) -> None:
        super().__init__()
        self.market_status = "normal"

    def market_snapshot(self, turn_id: str, symbol: str) -> dict:
        return {**super().market_snapshot(turn_id, symbol), "dataStatus": self.market_status}


async def test_a_direct_stop_never_fires_on_a_backup_quote(tmp_path: Path) -> None:
    app = application(tmp_path / "backup-stop.sqlite3", Gateway(), BackupQuoteProvider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    await _direct_stop(app, session.session_id, volume=2)
    try:
        provider.market_status = "backup"
        _fresh(provider, 820.0)  # crossed the 822.5 stop, but only on a backup quote
        await app.state.autonomy.run_once()
        assert gateway.submit_count == 0
        provider.market_status = "normal"
        _fresh(provider, 820.0)
        await app.state.autonomy.run_once()
        assert gateway.submit_count == 1
    finally:
        app.state.store.close()


async def test_price_alerts_do_not_trigger_on_the_backup_board(relay) -> None:
    service, sina = relay.service, FakeSina(fresh=True)
    install(service, sina)
    fail_all(relay)
    store = relay.app.state.store
    store.create_price_alert(user_id="alice", symbol="SHFE.au2612", name="沪金2612", direction="above",
                             price=Decimal("900"), note="", via="ui", session_id=None, max_active=20)
    await relay.app.state.price_alerts._tick()
    listed = store.list_price_alerts(user_id="alice")
    assert len(listed["active"]) == 1 and not listed["triggered"]
    # The board did come from Sina, crossed (910.58 > 900) and fresh: only `backup` held it back.
    read = rows_to_quotes(await service.board())["SHFE.AU2612"]
    assert read["dataStatus"] == "backup" and read["lastPrice"] == 910.58
    quote_is_fresh({**read, "dataStatus": "normal"}, allow_fixture=False)


def test_movement_watch_ignores_a_backup_series() -> None:
    workbench = moving_workbench("SHFE.rb2610", START)
    assert market_movement(workbench, START) is not None
    assert market_movement({**workbench, "dataStatus": "backup"}, START) is None


def test_strategy_rules_refuse_a_backup_series() -> None:
    backup = {"bars": bars([824] * 200, 60), "dataStatus": "backup", "ageSeconds": 1, "isCached": False}

    class BackupMarket:
        def market_workbench(self, symbol, timeframe):
            return copy.deepcopy(backup)

    with pytest.raises(StrategyRuleError) as raised:
        evaluate_plan(BackupMarket(), {"plan": plan().model_dump()}, SYMBOL)
    assert raised.value.code == "strategy_market_not_fresh"


async def test_only_the_later_source_recovering_takes_board_quotes_and_charts_back_from_sina(relay, monkeypatch) -> None:
    """A/B 正常 → 都成功返回断流 → 新浪 → 只恢复 B：总表、报价、既有与新图表都由 B 服务，
    通道只迁移一次，备用标记清除；之后 A 恢复也不抢回 B 上的通道。"""
    service, sina = relay.service, FakeSina()
    install(service, sina)
    lost = {FIRST: False, SECOND: False}
    report_lost(relay, lost)
    offset = [0.0]
    monkeypatch.setattr(service, "clock", lambda: time.time() + offset[0])
    states = service.source_states
    held = await service.acquire_market("SHFE.au2612", 300)
    fresh: list = []
    try:
        assert held.source == FIRST and (states[FIRST].channels, states[SECOND].channels) == (1, 0)

        lost[FIRST] = lost[SECOND] = True
        for account_id in (SECOND, FIRST):  # both sources' last reads said so (as the probe records)
            states[account_id].observed_at, states[account_id].connection_lost = service.clock(), True
        service.cache.clear()
        assert (await service.board())["dataStatus"] == "backup"
        assert (await service.quotes(["SHFE.au2612"]))["dataStatus"] == "backup"
        await until(lambda: held.packet["value"]["workbench"]["dataStatus"] == "backup")
        assert service.backup_active and held.source == FIRST  # nowhere connected to go

        lost[SECOND] = False
        offset[0] += SOURCE_SIGNAL_SECONDS + 1  # B's lost reading ages out; A keeps reporting lost
        service.cache.clear()
        board = await service.board()
        assert board["dataStatus"] not in {"stale", "backup"} and "source" not in board
        quotes = await service.quotes(["SHFE.au2612"])
        assert quotes["dataStatus"] not in {"stale", "backup"} and quotes["rows"][0]["symbol"] == "SHFE.au2612"
        await until(lambda: held.packet["value"]["workbench"]["dataStatus"] == "normal")
        assert held.source == SECOND and (states[FIRST].channels, states[SECOND].channels) == (0, 1)
        assert not service.backup_active
        fresh.append(await service.acquire_market("SHFE.rb2610", 300))
        assert fresh[0].source == SECOND and fresh[0].packet["value"]["workbench"]["dataStatus"] == "normal"

        lost[FIRST] = False
        offset[0] += SOURCE_SIGNAL_SECONDS + 1
        b_reads = relay.sources[SECOND].workbench_reads()
        await until(lambda: relay.sources[SECOND].workbench_reads() > b_reads + 2)  # producers keep going
        assert (held.source, fresh[0].source) == (SECOND, SECOND)  # a recovered A reclaims nothing
        assert (states[FIRST].channels, states[SECOND].channels) == (0, 2) and not service.backup_active
    finally:
        for channel in [held, *fresh]:
            service.release_market(channel)


@pytest.mark.parametrize("refusal", ["market_subscription_limit", "market_series_not_ready"])
async def test_leaving_the_backup_for_a_stale_tqkq_packet_says_stale_until_a_healthy_read(relay, monkeypatch, refusal) -> None:
    """全断 → 新浪 → B 连上但满额 / 冷启动、实际给的是 A 的旧快期包：退出备用，状态如实为 stale
    （不是 backup，也不是 normal），不再请求新浪；B 真正健康地读到后才回到 provider 自己的状态。"""
    service, sina = relay.service, FakeSina()
    install(service, sina)
    lost = {FIRST: True, SECOND: True}
    report_lost(relay, lost)
    states = {account_id: service._source_state(account_id) for account_id in (FIRST, SECOND)}
    for state in states.values():  # both sources' last reads said their TqKq connection is lost
        state.observed_at, state.connection_lost = service.clock(), True
    changes: list[str | None] = []
    service.backup_listeners.append(lambda: changes.append(service.served_status))
    provider = relay.app.state.data_provider
    original = provider.runtime_status
    own = {"marketDataStatus": "unavailable"}  # the account's own provider while TqKq is down
    monkeypatch.setattr(provider, "runtime_status", lambda: {**original(), **own})

    async def presented() -> dict:
        response = await relay.owner.get("/api/runtime/status", params={"mode": "cloud"})
        assert response.status_code == 200, response.text
        return response.json()

    assert (await service.board())["dataStatus"] == "backup" and service.backup_active
    assert (await presented())["marketDataStatus"] == "backup"

    lost[SECOND] = False
    relay.sources[SECOND].fail_reason = refusal  # connected, but full or warming up: it refuses the read
    states[SECOND].observed_at = 0
    sina_requests = len(sina.urls)
    service.cache.clear()
    board = await service.board()
    assert board["dataStatus"] == "stale" and "source" not in board  # A's stale TqKq packet is what is served
    assert service.served_status == "stale" and not service.backup_active and len(sina.urls) == sina_requests
    status = await presented()
    assert status["marketDataStatus"] == "stale" and status.get("marketDataSource") != BACKUP_SOURCE
    assert service.present_status({"marketDataStatus": "normal"}) == {"marketDataStatus": "normal"}
    service.cache.clear()
    assert (await service.board())["dataStatus"] == "stale" and service.served_status == "stale"  # still not normal

    relay.sources[SECOND].fail_reason = None  # B takes the read for real
    states[SECOND].full_until = 0
    own["marketDataStatus"] = "normal"
    service.cache.clear()
    board = await service.board()
    assert board["dataStatus"] not in {"stale", "backup"} and service.served_status is None
    assert (await presented())["marketDataStatus"] == "normal"
    assert changes == ["backup", "stale", None]


async def test_a_full_connected_source_takes_no_chart_without_a_slot_and_is_not_all_down(relay) -> None:
    """B 满：A 断流时图表不无槽读 B，也不把满额当全断（不切新浪），状态不报备用。"""
    service, sina = relay.service, FakeSina()
    install(service, sina)
    service.market_capacity = 1
    lost = {FIRST: False, SECOND: False}
    report_lost(relay, lost)
    held = await service.acquire_market("SHFE.au2612", 300)
    other = await service.acquire_market("SHFE.rb2610", 300)
    try:
        assert (held.source, other.source) == (FIRST, SECOND)
        lost[FIRST] = True
        a_reads, b_reads = relay.sources[FIRST].workbench_reads(), relay.sources[SECOND].workbench_reads()
        await until(lambda: relay.sources[FIRST].workbench_reads() > a_reads + 2
                    and relay.sources[SECOND].workbench_reads() > b_reads + 2)
        assert service.source_states[FIRST].connection_lost and held.source == FIRST and other.source == SECOND
        assert (service.source_states[FIRST].channels, service.source_states[SECOND].channels) == (1, 1)
        assert not any("SHFE.au2612" in path for path in relay.sources[SECOND].calls)  # no slotless read on B
        assert sina.urls == [] and not service.backup_active
    finally:
        service.release_market(held)
        service.release_market(other)

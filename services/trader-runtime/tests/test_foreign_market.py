"""外盘统一入口（foreign_market）：新浪行解析、源顺序与整行取自一个源、快期哨兵为空、失败回退与旧值、源配置。"""
from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from trader_runtime import foreign_market
from trader_runtime.foreign_market import (
    DEFAULT_SOURCES,
    ForeignMarketService,
    KqdForeignSource,
    Mt5ForeignSource,
    SinaForeignSource,
    build_sources,
    configured_sources,
    parse_sina,
)
from trader_runtime.instruments import FOREIGN_BY_KEY, FOREIGN_PRODUCTS, foreign_for_domestic
from trader_runtime.last_good import LastGood

SINA = (
    'var hq_str_hf_GC="4184.855,,4186.500,4187.100,4220.400,4167.500,04:59:56,4170.100,4192.100,0,1,1,2026-10-17,纽约黄金,0";\n'
    'var hq_str_hf_FCPO="4678.440,,4676.000,4677.000,4695.000,4577.000,17:59:59,4619.000,4621.000,92636,13,3,2026-10-16,马棕油,28561";\n'
    'var hq_str_hf_CL="0,,0,0,0,0,00:00:00,0,0,0,0,0,2026-10-17,纽约原油,0";\n'
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_parse_sina_reads_the_row_fields_and_skips_a_row_without_a_price():
    rows = parse_sina(SINA, FOREIGN_PRODUCTS)
    gold = rows["COMEX.GC"]
    assert gold["last"] == 4184.855 and gold["preSettlement"] == 4170.1 and gold["open"] == 4192.1
    assert gold["bidPrice1"] == 4186.5 and gold["askPrice1"] == 4187.1 and gold["high"] == 4220.4 and gold["low"] == 4167.5
    assert gold["changePercent"] == round((4184.855 - 4170.1) / 4170.1 * 100, 2)
    assert gold["quoteTime"] == "2026-10-17T04:59:56+08:00" and gold["source"] == "sina" and gold["currency"] == "USD"
    assert gold["openInterest"] is None and rows["BMD.FCPO"]["openInterest"] == 92636
    assert "NYMEX.CL" not in rows  # a zero price is the source's “nothing”, never a quote


def test_each_row_comes_whole_from_the_first_source_that_has_it():
    first = _Fake("mt5", {"COMEX.GC": _quote("COMEX.GC", "mt5", 1.0)})
    second = _Fake("sina", {"COMEX.GC": _quote("COMEX.GC", "sina", 2.0), "NYMEX.CL": _quote("NYMEX.CL", "sina", 3.0)})
    service = ForeignMarketService(LastGood(), [first, second])
    rows = {row["key"]: row for row in service.snapshot()["rows"]}
    assert rows["COMEX.GC"]["source"] == "mt5" and rows["COMEX.GC"]["last"] == 1.0
    assert rows["NYMEX.CL"]["source"] == "sina"
    assert [item.key for item in second.asked[0]] == [item.key for item in FOREIGN_PRODUCTS if item.key != "COMEX.GC"]


def test_a_failing_source_falls_through_and_a_total_failure_keeps_the_last_rows_stale():
    clock = _Clock()
    broken = _Fake("mt5", error=ConnectionError("down"))
    sina = _Fake("sina", {"COMEX.GC": _quote("COMEX.GC", "sina", 2.0)})
    service = ForeignMarketService(LastGood(clock=clock), [broken, sina])
    good = service.snapshot()
    assert good["status"] == "ok" and good["failedSources"] == ["mt5"] and good["rows"][0]["source"] == "sina"
    sina.rows, sina.error = {}, TimeoutError("down")
    clock.now += 6  # the first read was partial (mt5 failed): read again after the short retry
    stale = service.snapshot()
    assert stale["status"] == "stale" and stale["rows"] == good["rows"] and stale["error"] == "foreign_sources_failed:mt5,sina"


def test_the_kqd_source_leaves_its_sentinels_empty_and_reads_through_the_shared_quote_channel():
    asked: list[list[str]] = []

    async def reader(symbols: list[str]) -> dict[str, Any]:
        asked.append(symbols)
        return {"dataStatus": "normal", "rows": [
            {"symbol": "KQD.m@COMEX.GC", "last": 4186.7, "preSettlement": None, "high": 4220.4, "low": 4167.5,
             "volume": 135074, "openInterest": 0, "bidPrice1": -1, "askPrice1": -1, "quoteTime": "2026-10-18 04:14:34"},
            {"symbol": "KQD.m@NYMEX.CL", "errorCode": "market_subscription_limit"},
        ]}

    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        source = KqdForeignSource()
        service = ForeignMarketService(LastGood(), [source])
        service.bind(loop, reader)
        rows = {row["key"]: row for row in service.snapshot()["rows"]}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(5)
        loop.close()
    gold = rows["COMEX.GC"]
    assert gold["source"] == "kqd" and gold["last"] == 4186.7 and gold["volume"] == 135074
    assert gold["preSettlement"] is None and gold["change"] is None and gold["changePercent"] is None
    assert gold["bidPrice1"] is None and gold["askPrice1"] is None and gold["openInterest"] is None
    assert list(rows) == ["COMEX.GC"]
    assert sum(len(batch) for batch in asked) == sum(1 for item in FOREIGN_PRODUCTS if item.kqd)
    assert max(len(batch) for batch in asked) <= 20


def test_an_unbound_kqd_source_fails_like_an_unreachable_one():
    service = ForeignMarketService(LastGood(), [KqdForeignSource()])
    assert service.snapshot()["error"] == "foreign_sources_failed:kqd"


@pytest.mark.parametrize(("value", "expected"), [
    ("", DEFAULT_SOURCES), ("mt5, sina,kqd", ("mt5", "sina", "kqd")), ("kqd,kqd,bogus", ("kqd",)), ("bogus", DEFAULT_SOURCES)])
def test_configured_sources_keep_known_names_in_order(value, expected):
    assert configured_sources(value) == expected


def test_the_catalog_keys_are_unique_and_domestic_links_match_the_research_mapping():
    assert len(FOREIGN_BY_KEY) == len(FOREIGN_PRODUCTS)
    assert [item.key for item in foreign_for_domestic("cu")] == ["LMEOTC.CA"]
    assert [item.key for item in foreign_for_domestic("sc")] == ["NYMEX.CL", "ICE.BRN"]
    assert foreign_for_domestic("rb") == ()
    assert all(item.kqd is None or item.kqd.startswith("KQD.m@") for item in FOREIGN_PRODUCTS)


def _quote(key: str, source: str, last: float) -> dict[str, Any]:
    return {"key": key, "name": FOREIGN_BY_KEY[key].name, "last": last, "source": source}


class _Fake:
    def __init__(self, name: str, rows: dict[str, dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.name, self.rows, self.error = name, rows or {}, error
        self.asked: list[list[Any]] = []

    def quotes(self, products: list[Any]) -> dict[str, dict[str, Any]]:
        self.asked.append(products)
        if self.error is not None:
            raise self.error
        return {key: row for key, row in self.rows.items() if key in {item.key for item in products}}


def test_sina_source_asks_once_for_every_code():
    urls: list[str] = []
    source = SinaForeignSource(fetch_text=lambda url: urls.append(url) or SINA)
    rows = source.quotes(list(FOREIGN_PRODUCTS))
    assert len(urls) == 1 and "hf_GC" in urls[0] and "hf_FCPO" in urls[0] and set(rows) == {"COMEX.GC", "BMD.FCPO"}


def test_sina_copper_is_converted_from_cents_to_the_catalog_unit_and_rows_carry_their_precision():
    text = 'var hq_str_hf_HG="656.029,,656.300,656.450,664.900,654.650,04:59:56,655.650,655.900,0,3,2,2026-10-17,美铜,0";'
    copper = parse_sina(text, FOREIGN_PRODUCTS)["COMEX.HG"]
    assert copper["unit"] == "美元/磅" and copper["last"] == 6.56029 and copper["preSettlement"] == 6.5565
    assert copper["high"] == 6.649 and copper["decimals"] == 4
    assert parse_sina(SINA, FOREIGN_PRODUCTS)["COMEX.GC"]["decimals"] == 1


def test_a_failed_source_keeps_its_products_last_rows_stale_instead_of_dropping_them():
    clock = _Clock()
    sina = _Fake("sina", {"COMEX.GC": _quote("COMEX.GC", "sina", 2.0), "ICE.BRN": _quote("ICE.BRN", "sina", 74.8)})
    kqd = _Fake("kqd", {"COMEX.GC": _quote("COMEX.GC", "kqd", 2.1)})
    service = ForeignMarketService(LastGood(clock=clock), [sina, kqd])
    assert {row["key"] for row in service.snapshot()["rows"]} == {"COMEX.GC", "ICE.BRN"}
    sina.error = ConnectionError("down")
    clock.now += 6  # the first read was partial too (products neither source answered): the 5 s retry
    after = service.snapshot()
    rows = {row["key"]: row for row in after["rows"]}
    assert rows["COMEX.GC"]["source"] == "kqd" and "stale" not in rows["COMEX.GC"]
    assert rows["ICE.BRN"]["last"] == 74.8 and rows["ICE.BRN"]["stale"] is True  # Brent only Sina carries: kept, marked
    assert (after["partial"]["failed"], after["partial"]["carried"]) == (["sina"], ["ICE.BRN"])
    sina.error = None
    clock.now += 6  # a partial read is read again after the short retry
    assert all("stale" not in row for row in service.snapshot()["rows"])


def test_the_board_preference_accepts_the_foreign_filter():
    from trader_runtime.user_preferences import MarketBoardPreferences

    assert MarketBoardPreferences(exchange="FOREIGN").exchange == "FOREIGN"


def test_a_source_that_answers_without_a_product_keeps_that_products_last_row_stale():
    clock = _Clock()
    sina = _Fake("sina", {"COMEX.GC": _quote("COMEX.GC", "sina", 2.0), "ICE.BRN": _quote("ICE.BRN", "sina", 74.8)})
    service = ForeignMarketService(LastGood(clock=clock), [sina])
    service.snapshot()
    del sina.rows["ICE.BRN"]  # HTTP 200, Brent's row missing (or a zero price): not a source failure
    clock.now += 6  # the first read was partial too (Sina products it left out): the 5 s retry
    after = service.snapshot()
    rows = {row["key"]: row for row in after["rows"]}
    assert rows["ICE.BRN"]["stale"] is True and (after["partial"]["failed"], after["partial"]["carried"]) == ([], ["ICE.BRN"])


# --- MT5 只读行情桥源 -------------------------------------------------------------------------------

DAY = 86400.0
MT5_NOW = 1_792_300_000.0  # 2026-10-18（周日）


def _iso(days: float) -> str:
    return datetime.fromtimestamp(MT5_NOW + days * DAY, UTC).isoformat()


MT5_SYMBOLS = {"symbols": [
    {"symbol": "GCEV26", "expirationTime": _iso(10)}, {"symbol": "GCEZ26", "expirationTime": _iso(72)},
    {"symbol": "GCEG27", "expirationTime": _iso(129)}, {"symbol": "CLEX26", "expirationTime": _iso(2)},
    {"symbol": "CLEZ26", "expirationTime": _iso(33)}, {"symbol": "SICZ26", "expirationTime": _iso(72)},
    {"symbol": "GCEQ26X", "expirationTime": _iso(40)},
]}


def _mt5_quote(symbol: str, last: float | None, *, volume: float = 0.0, held: float = 0.0, **extra: Any) -> dict[str, Any]:
    return {"symbol": symbol, "bid": last and last - 0.3, "ask": last and last + 0.3, "last": last, "lastVolume": 1.0,
            "timeMsc": 1_792_184_399_830, "sessionVolume": volume, "sessionOpen": 4176.6, "sessionClose": 4176.9,
            "sessionHigh": 4220.4, "sessionLow": 4167.5, "openInterest": held, "settlement": 4181.1, **extra}


class _Bridge:
    def __init__(self, quotes: dict[str, dict[str, Any]]) -> None:
        self.quotes, self.paths = quotes, []

    def __call__(self, path: str) -> Any:
        self.paths.append(path)
        if path.startswith("/v1/symbols"):
            return MT5_SYMBOLS
        wanted = path.split("symbols=", 1)[1].split(",")
        return {"quotes": [self.quotes.get(name) or {"symbol": name, "error": "no_quote"} for name in wanted]}


def _mt5(bridge: _Bridge, now: float = MT5_NOW) -> Mt5ForeignSource:
    return Mt5ForeignSource(get_json=bridge, clock=lambda: now)


def test_mt5_rows_come_whole_from_the_main_month_by_activity():
    bridge = _Bridge({
        "GCEV26": _mt5_quote("GCEV26", 4177.0, volume=900),
        "GCEZ26": _mt5_quote("GCEZ26", 4186.7, volume=150_000, held=0, bid=4186.4, ask=4186.7),
        "GCEG27": _mt5_quote("GCEG27", 4216.0, volume=3000),
        "CLEX26": _mt5_quote("CLEX26", 101.84, volume=400_000, settlement=101.41),  # 2 days left: never main
        "CLEZ26": _mt5_quote("CLEZ26", 99.04, volume=120_000, held=300_000, settlement=99.02),
    })
    rows = _mt5(bridge).quotes(list(FOREIGN_PRODUCTS))
    assert set(rows) == {"COMEX.GC", "NYMEX.CL"}
    gold = rows["COMEX.GC"]
    assert gold["contract"] == "GCEZ26" and gold["source"] == "mt5" and gold["last"] == 4186.7
    assert gold["preSettlement"] == 4181.1 and gold["change"] == round(4186.7 - 4181.1, 6)
    assert (gold["bidPrice1"], gold["askPrice1"]) == (4186.4, 4186.7)
    assert (gold["open"], gold["high"], gold["low"]) == (4176.6, 4220.4, 4167.5)
    assert gold["volume"] == 150_000 and gold["openInterest"] is None  # AMP's 0 open interest is “none”
    assert gold["quoteTime"] == "2026-10-17T04:59:59+08:00" and gold["unit"] == "美元/盎司"
    crude = rows["NYMEX.CL"]
    assert crude["contract"] == "CLEZ26" and crude["preSettlement"] == 99.02 and crude["openInterest"] == 300_000
    # one contract-list read and one batched quote read; SICZ26 / GCEQ26X are not catalog months
    assert bridge.paths[0] == "/v1/symbols?path=Exchange-Futures" and len(bridge.paths) == 2
    assert "CLEX26" not in bridge.paths[1] and "SICZ26" not in bridge.paths[1] and "GCEQ26X" not in bridge.paths[1]
    _mt5_reuse = _mt5(bridge)
    _mt5_reuse.quotes(list(FOREIGN_PRODUCTS))
    _mt5_reuse.quotes(list(FOREIGN_PRODUCTS))
    assert sum(path.startswith("/v1/symbols") for path in bridge.paths) == 2  # the list is cached per source


def test_mt5_keeps_a_negative_price_and_treats_only_zero_as_missing():
    bridge = _Bridge({"CLEZ26": _mt5_quote("CLEZ26", -37.63, volume=120_000, settlement=-12.5, bid=0.0)})
    crude = _mt5(bridge).quotes(list(FOREIGN_PRODUCTS))["NYMEX.CL"]
    assert (crude["last"], crude["preSettlement"], crude["bidPrice1"]) == (-37.63, -12.5, None)


def test_mt5_without_activity_keeps_the_last_main_month_or_takes_the_first_month_a_month_out():
    quiet = {name: _mt5_quote(name, 4180.0 + index) for index, name in enumerate(("GCEV26", "GCEZ26", "GCEG27"))}
    fresh = _mt5(_Bridge(quiet))
    assert fresh.quotes(list(FOREIGN_PRODUCTS))["COMEX.GC"]["contract"] == "GCEZ26"  # GCEV26 has only 10 days left
    bridge = _Bridge({**quiet, "GCEG27": _mt5_quote("GCEG27", 4216.0, held=500_000)})
    source = _mt5(bridge)
    assert source.quotes(list(FOREIGN_PRODUCTS))["COMEX.GC"]["contract"] == "GCEG27"
    bridge.quotes = {**quiet, "GCEZ26": _mt5_quote("GCEZ26", 4186.7, volume=20)}  # a thin open says nothing yet
    assert source.quotes(list(FOREIGN_PRODUCTS))["COMEX.GC"]["contract"] == "GCEG27"


def test_mt5_main_month_without_a_price_leaves_the_product_to_the_next_source():
    bridge = _Bridge({"GCEV26": _mt5_quote("GCEV26", 4177.0), "GCEZ26": _mt5_quote("GCEZ26", None)})
    kqd = _Fake("kqd", {"COMEX.GC": _quote("COMEX.GC", "kqd", 4186.0)})
    service = ForeignMarketService(LastGood(), [_mt5(bridge), kqd])
    rows = {row["key"]: row for row in service.snapshot()["rows"]}
    assert rows["COMEX.GC"]["source"] == "kqd"  # not GCEV26's price: a row is never another month's


def test_mt5_after_sina_only_fills_what_sina_lacks():
    bridge = _Bridge({"GCEZ26": _mt5_quote("GCEZ26", 4186.7, volume=150_000),
                      "CLEZ26": _mt5_quote("CLEZ26", 99.04, volume=120_000)})
    sina = _Fake("sina", {"COMEX.GC": _quote("COMEX.GC", "sina", 4184.9)})
    service = ForeignMarketService(LastGood(), [sina, _mt5(bridge), _Fake("kqd")])
    payload = service.snapshot()
    rows = {row["key"]: row for row in payload["rows"]}
    assert rows["COMEX.GC"]["source"] == "sina" and rows["NYMEX.CL"]["source"] == "mt5"
    assert payload["sources"] == ["sina", "mt5", "kqd"]
    assert "GCEZ26" not in bridge.paths[-1]  # gold was not even asked of MT5


def test_mt5_bridge_failures_fall_through_without_the_token(monkeypatch, tmp_path, caplog):
    secret = "s3cret-bridge-token-" + "x" * 24
    token_file = tmp_path / "mt5-bridge-token"
    token_file.write_text(secret + "\n")
    monkeypatch.setenv("AI_TRADER_MT5_BRIDGE_URL", "http://172.17.0.1:11210/")
    monkeypatch.delenv("AI_TRADER_MT5_BRIDGE_TOKEN", raising=False)
    monkeypatch.setenv("AI_TRADER_MT5_BRIDGE_TOKEN_FILE", str(token_file))
    seen: list[httpx.Request] = []
    answer = {"status": 503}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if answer["status"] == 0:
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(answer["status"], json={"status": "unavailable", "reason": "terminal_disconnected"})

    real_client = httpx.Client
    monkeypatch.setattr(foreign_market.httpx, "Client",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    errors = []
    for status in (503, 0):
        answer["status"] = status
        with pytest.raises(Exception) as caught:
            Mt5ForeignSource().quotes(list(FOREIGN_PRODUCTS))
        errors.append(str(caught.value))
    assert errors == ["mt5_bridge_http_503", "mt5_bridge_unreachable:ConnectTimeout"]
    assert str(seen[0].url) == "http://172.17.0.1:11210/v1/symbols?path=Exchange-Futures"
    assert seen[0].headers["Authorization"] == f"Bearer {secret}"

    caplog.set_level("DEBUG")
    sina = _Fake("sina", {"COMEX.GC": _quote("COMEX.GC", "sina", 2.0)})
    service = ForeignMarketService(LastGood(), [_Fake("sina-down", error=ConnectionError("x")), Mt5ForeignSource(), sina])
    payload = service.snapshot()
    assert payload["rows"][0]["source"] == "sina" and "mt5" in payload["failedSources"]
    assert secret not in caplog.text and all(secret not in text for text in errors)

    monkeypatch.delenv("AI_TRADER_MT5_BRIDGE_TOKEN_FILE")
    with pytest.raises(RuntimeError, match="mt5_bridge_not_configured"):
        Mt5ForeignSource().quotes(list(FOREIGN_PRODUCTS))
    assert [source.name for source in build_sources(("sina", "mt5", "kqd"))] == ["sina", "mt5", "kqd"]


def test_a_zero_or_negative_previous_settlement_gives_a_change_but_no_percentage():
    """#932: MT5 can settle energy below zero; a percentage of a negative base would flip its sign."""
    bridge = _Bridge({"CLEZ26": _mt5_quote("CLEZ26", -37.63, volume=120_000, settlement=-12.5)})
    crude = _mt5(bridge).quotes(list(FOREIGN_PRODUCTS))["NYMEX.CL"]
    assert crude["change"] == round(-37.63 - -12.5, 6) and crude["changePercent"] is None
    positive = _mt5(_Bridge({"CLEZ26": _mt5_quote("CLEZ26", 99.04, volume=120_000, settlement=99.02)}))
    assert positive.quotes(list(FOREIGN_PRODUCTS))["NYMEX.CL"]["changePercent"] == round((99.04 - 99.02) / 99.02 * 100, 2)


def test_an_empty_mt5_contract_list_is_retried_soon_and_never_replaces_a_good_one():
    """#932: an HTTP 200 with no catalog month must not be cached for half an hour as “no contracts”."""
    listing: dict[str, Any] = {"symbols": []}
    quotes = {"GCEZ26": _mt5_quote("GCEZ26", 4186.7, volume=150_000)}
    paths: list[str] = []

    def bridge(path: str) -> Any:
        paths.append(path)
        if path.startswith("/v1/symbols"):
            return listing
        wanted = path.split("symbols=", 1)[1].split(",")
        return {"quotes": [quotes.get(name) or {"symbol": name, "error": "no_quote"} for name in wanted]}

    def listed() -> int:
        return sum(path.startswith("/v1/symbols") for path in paths)

    now = [MT5_NOW]
    source = Mt5ForeignSource(get_json=bridge, clock=lambda: now[0])
    for _ in range(2):  # cold and empty: the source fails (the next source serves), asked once inside the retry
        with pytest.raises(RuntimeError, match="mt5_contracts_empty"):
            source.quotes(list(FOREIGN_PRODUCTS))
    assert listed() == 1
    now[0] += foreign_market.MT5_CONTRACTS_RETRY_SECONDS
    listing["symbols"] = MT5_SYMBOLS["symbols"]
    assert source.quotes(list(FOREIGN_PRODUCTS))["COMEX.GC"]["contract"] == "GCEZ26"  # a minute later, not 30
    now[0] += foreign_market.MT5_CONTRACTS_TTL_SECONDS
    listing["symbols"] = []  # the half-hourly refresh comes back empty: the good table stays in use
    assert source.quotes(list(FOREIGN_PRODUCTS))["COMEX.GC"]["contract"] == "GCEZ26"
    asked = listed()
    source.quotes(list(FOREIGN_PRODUCTS))
    assert listed() == asked  # the empty refresh is retried after a minute, not on every read


def test_a_cold_start_without_a_related_row_reads_unavailable_not_an_empty_ok():
    """#932: only gold answered and nothing was kept: crude oil's readers say unavailable; a partly covered
    product names what is missing; a product with no related foreign market is a plain empty answer."""
    sina = _Fake("sina", {"COMEX.GC": _quote("COMEX.GC", "sina", 2.0), "NYMEX.CL": _quote("NYMEX.CL", "sina", 70.0)})
    board = ForeignMarketService(LastGood(), [sina]).snapshot()
    assert board["status"] == "ok" and "ICE.BRN" in board["partial"]["missing"]
    assert board["partial"]["failed"] == [] and board["partial"]["carried"] == []
    cold = ForeignMarketService(LastGood(), [_Fake("sina", {"COMEX.GC": _quote("COMEX.GC", "sina", 2.0)})])
    crude = cold.for_product("sc")
    assert crude["status"] == "unavailable" and crude["rows"] == [] and crude["missing"] == ["NYMEX.CL", "ICE.BRN"]
    assert crude["error"] == "foreign_product_unavailable"
    gold = cold.for_product("au")
    assert gold["status"] == "ok" and "missing" not in gold
    steel = cold.for_product("rb")
    assert steel["status"] == "ok" and steel["rows"] == [] and "missing" not in steel
    partly = ForeignMarketService(LastGood(), [_Fake("sina", {"NYMEX.CL": _quote("NYMEX.CL", "sina", 70.0)})])
    oil = partly.for_product("sc")
    assert oil["status"] == "ok" and [row["key"] for row in oil["rows"]] == ["NYMEX.CL"] and oil["missing"] == ["ICE.BRN"]
    # Only what a configured source carries is expected (Codex r4307848103): MT5 alone has no Brent mapping.
    mt5_only = ForeignMarketService(LastGood(), [_Fake("mt5", {"NYMEX.CL": _quote("NYMEX.CL", "mt5", 70.0)})])
    crude_mt5 = mt5_only.for_product("sc")
    assert crude_mt5["status"] == "ok" and "missing" not in crude_mt5
    assert "ICE.BRN" not in mt5_only.snapshot()["partial"]["missing"]
    # Every source answered nothing on a cold start: the all-markets view still names what is missing (r4307878763).
    empty = ForeignMarketService(LastGood(), [_Fake("sina")]).for_product(None)
    assert empty["status"] == "unavailable" and "COMEX.GC" in empty["missing"] and "NYMEX.CL" in empty["missing"]


# -- 外盘图表（所有者 2026-10-19 方案 A）------------------------------------------------------------

class _ChartBridge(_Bridge):
    """The bridge with K-lines: each requested bar is one minute (or the frame's length) apart, prices rising."""

    def __call__(self, path: str) -> Any:
        if path.startswith("/v1/bars"):
            self.paths.append(path)
            query = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&"))
            step = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}[query["timeframe"]]
            start = 1_792_180_800  # a minute boundary, 3-minute and 30-minute aligned
            return {"bars": [{"time": start + i * step, "open": 4180 + i, "high": 4181 + i, "low": 4179 + i,
                              "close": 4180.5 + i, "tickVolume": 5, "volume": 2} for i in range(int(query["count"]))]}
        return super().__call__(path)


def _chart_service(order: tuple[str, ...], bridge: _ChartBridge) -> ForeignMarketService:
    from trader_runtime.foreign_market import KqdForeignSource
    sources = {"mt5": _mt5(bridge), "kqd": KqdForeignSource(), "sina": SinaForeignSource()}
    return ForeignMarketService(LastGood(), [sources[name] for name in order],
                                now=lambda: datetime.fromtimestamp(1_792_184_400, UTC))


def test_a_foreign_chart_at_a_minute_period_comes_whole_from_the_mt5_main_month():
    bridge = _ChartBridge({"GCEZ26": _mt5_quote("GCEZ26", 4186.7, volume=150_000)})
    service = _chart_service(("mt5", "sina", "kqd"), bridge)
    item = {"symbol": "KQD.m@COMEX.GC", "instrumentName": "COMEX黄金主连", "priceTick": 0.1, "volumeMultiple": 100}
    packet = service.chart("KQD.m@COMEX.GC", 180, item)
    assert packet is not None and packet["source"] == "MT5 · GCEZ26" and packet["quote"]["source"] == "MT5 · GCEZ26"
    assert packet["dataStatus"] == "normal" and packet["durationSeconds"] == 180 and packet["revision"].startswith("mt5:")
    quote = packet["quote"]
    assert (quote["lastPrice"], quote["preSettlement"], quote["priceTick"]) == (4186.7, 4181.1, 0.1)
    assert quote["marketSessionStatus"] == "trading"  # the main month quoted 0.2 s before “now”
    bars = packet["bars"]
    bars_path = next(path for path in bridge.paths if path.startswith("/v1/bars"))
    assert "symbol=GCEZ26" in bars_path and "timeframe=M1" in bars_path and "count=768" in bars_path
    assert len(bars) == 256 and all(int(bar["datetime"]) % (180 * 1_000_000_000) == 0 for bar in bars)
    first = bars[0]  # three 1-minute bars: first open, highest high, lowest low, last close, summed volume
    assert first["high"] - first["open"] == 3 and first["volume"] == 6
    # 外盘三期：每根线带 CME 交易日，行情时段是 CME 的（北京时间跨午夜，dayRule cme）
    from trader_runtime.trading_calendar import cme_trading_day
    assert all(bar["tradingDay"] == cme_trading_day(int(bar["datetime"])) for bar in bars)
    assert quote["tradingSchedule"]["dayRule"] == "cme"


def test_a_foreign_chart_stays_on_kqd_for_weekly_bars_a_kqd_first_order_or_an_unknown_symbol():
    bridge = _ChartBridge({"GCEZ26": _mt5_quote("GCEZ26", 4186.7, volume=150_000)})
    assert _chart_service(("mt5", "kqd"), bridge).chart("KQD.m@COMEX.GC", 604800) is None  # 周线：快期主连
    assert _chart_service(("kqd", "mt5"), bridge).chart("KQD.m@COMEX.GC", 300) is None  # 快期排在前
    assert _chart_service(("mt5", "kqd"), bridge).chart("KQD.m@CBOT.XX", 300) is None  # 不在外盘目录
    assert not any(path.startswith("/v1/bars") for path in bridge.paths)


def test_the_shared_market_reads_a_foreign_chart_from_mt5_and_falls_back_to_kqd_for_a_while():
    """The chart path: a 快期外盘主连 workbench read at an MT5 period goes to ForeignMarketService.chart; an MT5 failure
    leaves that symbol on 快期 for foreign_backoff seconds (no flipping between sources); other reads never ask."""
    from types import SimpleNamespace

    from trader_runtime.shared_market import SharedMarketService, foreign_listed

    calls: list[tuple[str, int]] = []
    now = [100.0]

    def chart(symbol: str, duration: int, item: Any) -> Any:
        calls.append((symbol, duration))
        if duration == 900:
            raise ConnectionError("bridge down")
        return {"source": "MT5 · GCEZ26", "item": item}

    market = SimpleNamespace(foreign_chart=chart, clock=lambda: now[0], foreign_backoff=60.0, _foreign_down={},
                             _last_catalog={"items": [{"symbol": "KQD.m@COMEX.GC", "priceTick": 0.1}]})
    read = lambda path: asyncio.run(SharedMarketService._foreign_read(market, path))  # noqa: E731
    got = read("/v1/public-market/KQD.m@COMEX.GC/workbench?duration=300&after=x&waitMs=10000")
    assert got["source"] == "MT5 · GCEZ26" and got["item"]["priceTick"] == 0.1
    assert read("/v1/public-market/SHFE.rb2701/workbench?duration=300") is None  # domestic: never asked
    assert read("/v1/public-market/quotes?symbols=KQD.m@COMEX.GC") is None
    assert read("/v1/public-market/KQD.m@COMEX.GC/workbench?duration=900") is None  # MT5 failed → 快期
    assert read("/v1/public-market/KQD.m@COMEX.GC/workbench?duration=300") is None  # still backing off
    now[0] += 61
    assert read("/v1/public-market/KQD.m@COMEX.GC/workbench?duration=300") is not None
    assert calls == [("KQD.m@COMEX.GC", 300), ("KQD.m@COMEX.GC", 900), ("KQD.m@COMEX.GC", 300)]
    # 外盘只认一份目录: 快期's other 海外主连 leave the catalog; every domestic contract stays
    assert foreign_listed({"exchangeId": "KQD", "symbol": "KQD.m@COMEX.GC"})
    assert not foreign_listed({"exchangeId": "KQD", "symbol": "KQD.m@CFE.VX"})
    assert foreign_listed({"exchangeId": "SHFE", "symbol": "SHFE.rb2701"})


def test_the_feed_focus_takes_a_foreign_chart_symbol_but_order_requests_keep_their_format():
    """外盘图表 (2026-10-19): the chart's push focus is a 快期外盘主连 symbol with “@”; it used to answer 422."""
    import pydantic

    from trader_runtime.routes.requests import FeedFocusRequest
    assert FeedFocusRequest(symbol="KQD.m@COMEX.GC").symbol == "KQD.m@COMEX.GC"
    with pytest.raises(pydantic.ValidationError):
        FeedFocusRequest(symbol="KQD.m@COMEX.GC/../x")


def test_a_foreign_chart_symbol_rides_every_chat_context_field():
    """外盘图表 (2026-10-19 回读): with the chart on KQD.m@COMEX.GC every chat message answered 422 (its window context,
    recent actions and the suggestions request carried the symbol); the push focus too. One shared pattern now."""
    from trader_runtime.chat_suggestions import SuggestionsRequest
    from trader_runtime.routes.requests import FeedFocusRequest, MessageRequest

    symbol = "KQD.m@COMEX.GC"
    assert FeedFocusRequest(symbol=symbol).symbol == symbol
    assert SuggestionsRequest(symbol=symbol, view="trade", trigger="symbol").symbol == symbol
    request = MessageRequest.model_validate({"text": "黄金怎么看", "workspaceContext": {
        "view": "watch", "symbol": symbol, "durationSeconds": 300, "quoteDatetime": "2026-10-19T16:30:00+08:00",
        "capturedAt": "2026-10-19T16:30:01+08:00", "ageSeconds": 1, "dataStatus": "normal"}})
    assert request.workspaceContext is not None and request.workspaceContext.symbol == symbol


def test_a_foreign_month_history_is_that_contracts_own_mt5_bars_for_backtests():
    """外盘三期（方案 A）：回测读外盘月份合约自己的 MT5 K 线（不是主力、不是快期主连），带 CME 交易日与合约规格；
    周线返回 None，调用方照旧读快期主连。"""
    from trader_runtime.trading_calendar import cme_trading_day
    bridge = _ChartBridge({"GCEZ26": _mt5_quote("GCEZ26", 4186.7, volume=150_000)})
    service = _chart_service(("mt5", "sina", "kqd"), bridge)
    history = service.history("CME.GCEG27", 300, 100)
    assert history is not None and history["source"] == "MT5 · GCEG27"
    assert any("symbol=GCEG27" in path and "timeframe=M5" in path for path in bridge.paths)
    assert history["tradingSchedule"]["dayRule"] == "cme"
    assert history["bars"] and all(bar["tradingDay"] == cme_trading_day(int(bar["datetime"])) for bar in history["bars"])
    assert service.history("CME.GCEZ26", 604800, 100) is None
    assert service.history("SHFE.au2612", 300, 100) is None


def test_a_foreign_month_live_workbench_is_that_contracts_own_mt5_quote_and_bars():
    """外盘三期（方案 A）：策略实时判定读的也是这个月份合约自己的 MT5 报价与 K 线，与回测、撮合同源。"""
    bridge = _ChartBridge({"GCEZ26": _mt5_quote("GCEZ26", 4186.7, volume=150_000),
                           "GCEG27": _mt5_quote("GCEG27", 4201.3, volume=20_000)})
    service = _chart_service(("mt5", "sina", "kqd"), bridge)
    packet = service.workbench("CME.GCEG27", 300)
    assert packet is not None and packet["source"] == "MT5 · GCEG27"
    assert packet["quote"]["lastPrice"] == 4201.3 and packet["quote"]["symbol"] == "CME.GCEG27"
    assert packet["bars"] and any("symbol=GCEG27" in path for path in bridge.paths)
    assert service.workbench("CME.GCEZ26", 604800) is None


def _hourly_bridge(quote_ms: int, hours: list[datetime]) -> _Bridge:
    class Bridge(_Bridge):
        def __call__(self, path: str) -> Any:
            if path.startswith("/v1/bars"):
                self.paths.append(path)
                count = int(path.rsplit("count=", 1)[1])
                return {"bars": [{"time": int(hour.timestamp()), "open": 4180 + i, "high": 4181 + i, "low": 4179 + i,
                                  "close": 4180.5 + i, "tickVolume": 5, "volume": 2}
                                 for i, hour in enumerate(hours)][-count:]}
            return super().__call__(path)
    return Bridge({"GCEZ26": _mt5_quote("GCEZ26", 4186.7, volume=150_000, timeMsc=quote_ms)})


def test_a_foreign_daily_chart_is_the_mt5_month_by_cme_trading_day():
    """外盘尾项 (2026-10-21): 快期外盘主连的日线按国内日历切（北京 18:00 换日、国内长假并成一根），日线改由 MT5
    主力月份的小时线按 CME 交易日合成——感恩节周四的盘并进周五；整段小时线半小时读一次，其间只读最后 48 根。"""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from trader_runtime.foreign_market import MT5_HOURLY_REFRESH_SECONDS
    start = datetime(2026, 11, 24, 18, tzinfo=ZoneInfo("America/New_York"))  # Tuesday 18:00 ET: Wednesday's day
    hours = [start + timedelta(hours=step) for step in range(66)]  # to Friday 11:00 ET
    bridge = _hourly_bridge(int(hours[-1].timestamp() * 1000) + 600_000, hours)
    clock = [MT5_NOW]
    service = ForeignMarketService(LastGood(), [Mt5ForeignSource(get_json=bridge, clock=lambda: clock[0])],
                                   now=lambda: datetime.fromtimestamp(MT5_NOW, UTC))
    packet = service.chart("KQD.m@COMEX.GC", 86400, {"priceTick": 0.1})
    assert packet is not None and packet["source"] == "MT5 · GCEZ26"
    bars = packet["bars"]
    assert [bar["tradingDay"] for bar in bars] == ["2026-11-25", "2026-11-27"]
    wednesday, friday = bars
    assert (wednesday["open"], wednesday["close"], wednesday["volume"]) == (4180, 4180.5 + 23, 48)  # 18:00–17:00
    assert friday["open"] == 4180 + 24 and friday["close"] == 4180.5 + 65  # Wednesday 18:00 ET to Friday
    assert wednesday["datetime"] == int(datetime(2026, 11, 25, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()) * 1_000_000_000
    counts = lambda: [path.rsplit("count=", 1)[1] for path in bridge.paths if path.startswith("/v1/bars")]  # noqa: E731
    service.chart("KQD.m@COMEX.GC", 86400, {})
    assert counts() == ["10000", "48"]
    clock[0] += MT5_HOURLY_REFRESH_SECONDS
    service.chart("KQD.m@COMEX.GC", 86400, {})
    assert counts() == ["10000", "48", "10000"]


def test_mt5_bars_still_syncing_leave_the_chart_on_kqd_for_that_read():
    """外盘尾项 (2026-10-21): the first read of a month nobody read for a while answers the terminal's old local
    history (the last bar hours back while the quote is current); that read is 快期's, the next one MT5's."""
    from datetime import timedelta
    start = datetime(2026, 10, 21, 13, tzinfo=UTC)
    hours = [start + timedelta(minutes=5 * step) for step in range(40)]  # last bar 16:15 UTC
    behind = _hourly_bridge(int((hours[-1] + timedelta(hours=5)).timestamp() * 1000), hours)
    service = _chart_service(("mt5", "kqd"), behind)
    assert service.chart("KQD.m@COMEX.GC", 300) is None
    assert service.workbench("CME.GCEZ26", 300) is None
    current = _hourly_bridge(int((hours[-1] + timedelta(minutes=3)).timestamp() * 1000), hours)
    assert _chart_service(("mt5", "kqd"), current).chart("KQD.m@COMEX.GC", 300) is not None


def test_mt5_quotes_are_read_in_batches_the_bridge_accepts():
    """10-20 外盘扩到 MT5 的 CME 美元品种后，候选月份超过行情桥一次 50 个的上限：分批读，每个品种照常挑主力。"""
    from trader_runtime.foreign_market import MT5_QUOTE_BATCH
    tradable = [item for item in FOREIGN_PRODUCTS if item.mt5]
    months = ("Z26", "G27", "H27")
    symbols = {"symbols": [{"symbol": f"{item.mt5}{month}", "expirationTime": _iso(72 + 30 * index)}
                           for item in tradable for index, month in enumerate(months)]}

    class Bridge(_Bridge):
        def __call__(self, path: str) -> Any:
            if path.startswith("/v1/symbols"):
                self.paths.append(path)
                return symbols
            assert len(path.split("symbols=", 1)[1].split(",")) <= MT5_QUOTE_BATCH
            return super().__call__(path)

    bridge = Bridge({f"{item.mt5}Z26": _mt5_quote(f"{item.mt5}Z26", 100.0, volume=10_000) for item in tradable})
    rows = _mt5(bridge).quotes(tradable)
    assert len(tradable) * len(months) > MT5_QUOTE_BATCH
    assert {row["contract"] for row in rows.values()} == {f"{item.mt5}Z26" for item in tradable}
    assert sum(path.startswith("/v1/quotes") for path in bridge.paths) >= 2

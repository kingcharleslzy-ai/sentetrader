"""AKShare research data: recorded samples only, no network."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.main import create_app
from trader_runtime.research_data import (
    DATASETS,
    AkshareSources,
    ResearchDataService,
    last_trading_date,
    normalise_shfe_receipt_html,
    split_symbol,
)

FIXTURES = Path(__file__).parent / "fixtures" / "akshare"


class Rows(list):
    """The slice of the DataFrame surface the service uses; CI has no pandas."""

    def to_dict(self, orient: str = "records") -> list[dict]:
        assert orient == "records"
        return list(self)


def frame(name: str) -> Rows:
    return Rows(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def frames(name: str) -> dict[str, Rows]:
    return {key: Rows(rows) for key, rows in json.loads((FIXTURES / name).read_text(encoding="utf-8")).items()}


class RecordedAkshare:
    """The AKShare surface the service touches, answered from recorded samples; `failing` names raise."""

    def __init__(self, failing: set[str] | None = None, slow: set[str] | None = None, calendar: list[str] | None = None) -> None:
        self.failing, self.slow, self.calls, self.dates = failing or set(), slow or set(), [], []
        self.calendar = calendar

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if name in self.slow:
            time.sleep(3)
        if name in getattr(self, "hung", set()):
            threading.Event().wait(5)  # a site that never answers
        if name in self.failing:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    def get_shfe_rank_table(self, date, vars_list):
        self._guard("get_shfe_rank_table"); self.dates.append(date); return frames("shfe_rank_rb.json")


    def shfe_receipt_page(self, url):
        """SHFE's own daily receipt page (not an AKShare call): `AkshareSources.http_get`."""
        self._guard("shfe_receipt_page"); self.dates.append(url)
        return (FIXTURES / "shfe_receipt_20260924.html").read_text(encoding="utf-8")

    def get_rank_table_czce(self, date):
        self._guard("get_rank_table_czce"); return frames("czce_rank_ap.json")

    def futures_warehouse_receipt_czce(self, date):
        self._guard("futures_warehouse_receipt_czce"); return frames("czce_receipt_sr.json")

    def futures_spot_price(self, date):
        self._guard("futures_spot_price"); return frame("spot_price.json")

    def futures_inventory_99(self, symbol):
        self._guard("futures_inventory_99"); raise ValueError("Expecting value")

    def futures_inventory_em(self, symbol):
        self._guard("futures_inventory_em"); return frame("inventory_em_rb.json")

    def get_roll_yield_bar(self, type_method, var, date):
        self._guard("get_roll_yield_bar"); return frame("roll_yield_rb.json")

    def futures_zh_realtime(self, symbol):
        self._guard("futures_zh_realtime"); self.dates.append(symbol); return frame("sina_realtime_m.json")

    def futures_dce_position_rank(self, date, vars_list):
        self._guard("futures_dce_position_rank"); raise AssertionError("DCE is behind a bot check; never called")

    def futures_warehouse_receipt_dce(self, date):
        self._guard("futures_warehouse_receipt_dce"); raise AssertionError("DCE is behind a bot check; never called")

    def futures_comm_info(self, symbol):
        self._guard("futures_comm_info"); return frame("comm_info.json")


def service(tmp_path, recorded: RecordedAkshare, clock=None) -> ResearchDataService:
    connection = sqlite3.connect(tmp_path / "research.sqlite3", check_same_thread=False)
    sources = AkshareSources()
    sources._module = recorded
    sources.http_get = recorded.shfe_receipt_page
    # The exchange calendar comes from the 快期 calendar in production (wiring/services.py); here the recording's.
    sources.calendar = (lambda: recorded.calendar) if recorded.calendar is not None else None
    kwargs = {"clock": clock} if clock else {}
    return ResearchDataService(connection=connection, lock=threading.Lock(), sources=sources, **kwargs)


def test_symbols_split_into_exchange_product_and_contract():
    assert split_symbol("SHFE.rb2610") == ("SHFE", "rb", "rb2610")
    assert split_symbol("CZCE.AP701") == ("CZCE", "AP", "AP701")
    with pytest.raises(ValueError):
        split_symbol("rb2610")


def test_summary_normalises_every_dataset_and_keeps_failures_local(tmp_path):
    recorded = RecordedAkshare(failing={"shfe_receipt_page"})
    svc = service(tmp_path, recorded)
    try:
        summary = svc.summary("SHFE.rb2610")
        items = summary["datasets"]
        assert set(items) == set(DATASETS)
        rank = items["position_rank"]
        assert rank["status"] == "ok" and rank["data"]["contract"].upper() == "RB2610"
        assert len(rank["data"]["long"]) <= 20 and rank["data"]["netLong"] == rank["data"]["longTotal"] - rank["data"]["shortTotal"]
        assert items["warehouse_receipt"]["status"] == "unavailable"
        assert items["warehouse_receipt"]["error"].startswith("research_source_error:")
        basis = items["spot_basis"]["data"]
        assert basis["dominantContract"].lower().startswith("rb") and isinstance(basis["dominantBasis"], float)
        inventory = items["inventory"]
        assert inventory["status"] == "ok" and inventory["source"].startswith("东方财富") and inventory["data"]["trend"] in {"up", "down", "flat"}
        term = items["term_structure"]["data"]
        assert term["shape"] in {"contango", "backwardation", "flat"} and len(term["contracts"]) >= 2
        fees = items["fees_margin"]["data"]
        assert fees["contract"] == "rb2610" and fees["marginRateLong"] is not None and "不改写" in fees["note"]
        assert all(item["asOf"] for name, item in items.items() if item["status"] == "ok")
    finally:
        svc.close()


def test_shfe_receipts_come_from_the_exchange_daily_page():
    """SHFE retired the JSON AKShare read (404 since September 2026): the HTML report's product
    section — 仓库 and 厂库 tables together — gives the total, the change and the busy warehouses."""
    html = (FIXTURES / "shfe_receipt_20260924.html").read_text(encoding="utf-8")
    rebar = normalise_shfe_receipt_html(html, "rb")
    assert (rebar["total"], rebar["change"]) == (85813, 12396)
    assert rebar["warehouses"][0] == {"warehouse": "惠龙港", "quantity": 62420, "change": 12396}
    assert all(item["quantity"] or item["change"] for item in rebar["warehouses"])
    copper = normalise_shfe_receipt_html(html, "cu")
    assert (copper["total"], copper["change"]) == (16620, -2468)
    assert not any("计" in item["warehouse"] for item in copper["warehouses"])  # 合计 / 完税商品总计 are not warehouses
    for product in ("hc", "xx"):  # a section missing from the page, a product SHFE does not list
        with pytest.raises(LookupError):
            normalise_shfe_receipt_html(html, product)


def test_exchange_holidays_are_skipped_by_the_trading_calendar(tmp_path):
    """2026-09-25 (中秋) was a weekday without trading: before 17:00 on Monday 09-28 the last
    published day is Thursday 09-24, and the whole 国庆 week reads 09-29."""
    calendar = ["2026-09-24", "2026-09-28", "2026-09-29", "2026-10-08"]
    days = {day.replace("-", "") for day in calendar}
    monday_morning = datetime(2026, 9, 28, 3, 0, tzinfo=UTC)  # 11:00 Beijing
    assert last_trading_date(monday_morning, days) == "20260924"
    assert last_trading_date(datetime(2026, 9, 28, 10, 0, tzinfo=UTC), days) == "20260928"  # 18:00 Beijing
    assert last_trading_date(datetime(2026, 10, 5, 3, 0, tzinfo=UTC), days) == "20260929"
    assert last_trading_date(monday_morning) == "20260925"  # without a calendar only weekends are skipped
    # A day before the calendar's span skips weekends only, never runs past it.
    assert last_trading_date(datetime(2026, 10, 7, 3, 0, tzinfo=UTC), {"20261008", "20261009"}) == "20261006"
    recorded = RecordedAkshare(calendar=calendar)
    svc = service(tmp_path, recorded, clock=lambda: monday_morning)
    try:
        assert svc.dataset("position_rank", "SHFE.rb2610")["asOf"] == "20260924"
        assert svc.dataset("warehouse_receipt", "SHFE.rb2610")["data"]["total"] == 85813
        assert recorded.dates == ["20260924", "https://www.shfe.com.cn/data/tradedata/future/stockdata/dailystock_20260924/ZH/all.html"]
        # The calendar is the wired exchange calendar: AKShare's trade-date reader is never called.
        svc.dataset("spot_basis", "SHFE.rb2610")
        assert "tool_trade_date_hist_sina" not in recorded.calls
    finally:
        svc.close()


def test_dce_items_come_from_sources_outside_its_bot_check(tmp_path):
    """DCE answers its data endpoints with a bot-verification page (HTTP 412): the term structure
    comes from Sina's contract list, the receipts from Eastmoney's registered totals, and the member
    rank — which has no other free source — says why it is missing without touching DCE."""
    recorded = RecordedAkshare()
    svc = service(tmp_path, recorded)
    try:
        items = svc.summary("DCE.m2701")["datasets"]
        term = items["term_structure"]
        assert term["status"] == "ok" and term["source"].endswith("新浪）") and len(term["asOf"]) == 8
        assert all(row["contract"][1:].isdigit() for row in term["data"]["contracts"])  # the continuous M0 is not a month
        months = [row["contract"] for row in term["data"]["contracts"]]
        assert months == sorted(months) and term["data"]["nearContract"] == months[0]
        receipt = items["warehouse_receipt"]
        assert receipt["status"] == "ok" and "东方财富" in receipt["source"] and receipt["data"]["warehouses"] == []
        assert receipt["data"]["total"] == items["inventory"]["data"]["latest"]["value"]
        assert items["position_rank"]["error"] == "research_source_blocked"
        assert "豆粕" in recorded.dates
        assert not {"futures_dce_position_rank", "futures_warehouse_receipt_dce", "get_roll_yield_bar"} & set(recorded.calls)
    finally:
        svc.close()


def test_cache_serves_within_ttl_and_a_failed_refresh_returns_the_last_value_as_stale(tmp_path):
    now = [datetime(2026, 9, 11, 3, 0, tzinfo=UTC)]  # 11:00 Beijing, intraday
    recorded = RecordedAkshare()
    svc = service(tmp_path, recorded, clock=lambda: now[0])
    try:
        first = svc.dataset("spot_basis", "SHFE.rb2610")
        assert first["status"] == "ok"
        assert svc.dataset("spot_basis", "SHFE.rb2701")["status"] == "ok"
        assert recorded.calls.count("futures_spot_price") == 1  # same product, fresh cache
        # A later refresh whose source fails keeps the value, marked stale, with the error attached.
        now[0] += timedelta(hours=2)  # 13:00 Beijing: past the intraday hour, same trading day
        recorded.failing.add("futures_spot_price")
        stale = svc.dataset("spot_basis", "SHFE.rb2610")
        assert stale["status"] == "stale" and stale["data"] == first["data"] and stale["error"].startswith("research_source_error")
        # Inside the failure back-off nothing is fetched again; a restart over the same database still has it.
        assert svc.dataset("spot_basis", "SHFE.rb2610")["status"] == "stale"
        assert recorded.calls.count("futures_spot_price") == 2
        restarted = ResearchDataService(connection=svc.connection, lock=threading.Lock(), sources=svc.sources,
                                        clock=lambda: now[0])
        try:
            again = restarted.dataset("spot_basis", "SHFE.rb2610")
            assert again["status"] == "stale" and again["data"] == first["data"]
        finally:
            restarted.close()
    finally:
        svc.close()


def test_missing_dependency_and_unknown_products_are_reported_not_raised(tmp_path):
    svc = service(tmp_path, RecordedAkshare())
    try:
        assert svc.dataset("position_rank", "CZCE.PK611")["error"] == "research_product_not_in_source"
        assert svc.dataset("position_rank", "KQD.CBOT.ZC")["error"] == "research_exchange_unsupported"
    finally:
        svc.close()
    missing = ResearchDataService(connection=sqlite3.connect(tmp_path / "m.sqlite3", check_same_thread=False), lock=threading.Lock())
    missing.sources._module = None
    import builtins
    real_import = builtins.__import__
    def no_akshare(name, *args, **kwargs):
        if name == "akshare":
            raise ImportError("no akshare")
        return real_import(name, *args, **kwargs)
    builtins.__import__ = no_akshare
    try:
        item = missing.dataset("inventory", "SHFE.rb2610")
        assert item["status"] == "unavailable" and item["error"] == "research_dependency_missing"
    finally:
        builtins.__import__ = real_import
        missing.close()


def test_a_hung_source_does_not_hold_the_pool(tmp_path, monkeypatch):
    monkeypatch.setattr("trader_runtime.research_data.FETCH_TIMEOUT_SECONDS", 0.5)
    recorded = RecordedAkshare()
    recorded.hung = {"futures_comm_info"}
    svc = service(tmp_path, recorded)
    try:
        started = time.monotonic()
        summary = svc.summary("SHFE.rb2610")
        assert time.monotonic() - started < 3
        assert summary["datasets"]["fees_margin"]["error"] == "research_fetch_timeout"
        assert [summary["datasets"][name]["status"] for name in ("position_rank", "spot_basis", "inventory", "term_structure")] == ["ok"] * 4
        # A second summary is served from cache for the good items and times out again only on the hung one, promptly.
        started = time.monotonic()
        again = svc.summary("SHFE.rb2610")
        assert time.monotonic() - started < 3 and again["datasets"]["spot_basis"]["status"] == "ok"
        assert recorded.calls.count("futures_spot_price") == 1
    finally:
        svc.close()


def test_slow_source_times_out_alone(tmp_path, monkeypatch):
    monkeypatch.setattr("trader_runtime.research_data.FETCH_TIMEOUT_SECONDS", 0.5)
    svc = service(tmp_path, RecordedAkshare(slow={"futures_comm_info"}))
    try:
        summary = svc.summary("SHFE.rb2610")
        assert summary["datasets"]["fees_margin"]["error"] == "research_fetch_timeout"
        assert summary["datasets"]["spot_basis"]["status"] == "ok"
    finally:
        svc.close()


async def test_routes_are_read_only_and_in_the_capability_catalog(tmp_path, monkeypatch):
    app = create_app(tmp_path / "runtime.sqlite3", local_token="research-test-token-with-32-chars!!", agent_mode="fake", data_mode="fake")
    app.state.research.sources._module = recorded = RecordedAkshare()
    app.state.research.sources.http_get = recorded.shfe_receipt_page
    async def news_brief(self, symbol, topic):
        return {"symbol": symbol, "topic": topic, "status": "ok", "results": []}
    monkeypatch.setattr("trader_runtime.research_brief.ResearchBriefService.brief", news_brief)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://127.0.0.1:5173"}) as client:
            assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer research-test-token-with-32-chars!!"})).status_code == 200
            news = await client.get("/api/research/news", params={"symbol": "SHFE.rb2610"})
            assert news.status_code == 200 and news.json()["topic"] == "news"
            summary = await client.get("/api/research/summary", params={"symbol": "SHFE.rb2610"})
            assert summary.status_code == 200 and set(summary.json()["datasets"]) == set(DATASETS)
            one = await client.get("/api/research/term_structure", params={"symbol": "SHFE.rb2610"})
            assert one.status_code == 200 and one.json()["data"]["shape"] in {"contango", "backwardation", "flat"}
            assert (await client.get("/api/research/nope", params={"symbol": "SHFE.rb2610"})).status_code == 404
            assert (await client.get("/api/research/summary", params={"symbol": "rb"})).status_code == 422
            catalog = (await client.get("/api/agent/capabilities")).json()
            operations = {item["operation"]: item for item in catalog["operations"]}
            assert operations["GET /api/research/news"]["readOnly"] is True
            assert operations["GET /api/research/summary"]["readOnly"] is True
            assert operations["GET /api/research/{dataset}"]["readOnly"] is True
    finally:
        app.state.research.close()
        app.state.store.close()


class TwoMonths(RecordedAkshare):
    """SHFE's rank table and the fee table both carry rb2610 and rb2701 in one answer."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        super().__init__()
        self.gate = gate

    def get_shfe_rank_table(self, date, vars_list):
        self._guard("get_shfe_rank_table")
        if self.gate is not None:
            self.gate.wait(5)
        rows = frames("shfe_rank_rb.json")["rb2610"]
        later = Rows({**row, "symbol": "RB2701", "long_open_interest": (row["long_open_interest"] or 0) * 2} for row in rows)
        return {"rb2610": rows, "rb2701": later}


def test_contract_datasets_keep_each_month_apart_and_share_one_source_read(tmp_path):
    """#487: fees and the member rank are per contract — one month never answers for another,
    in memory, after a restart, after a miss, or when both months are read at once — while the
    source's whole table is still read once and the product-level datasets stay shared."""
    gate = threading.Event()
    recorded = TwoMonths(gate)
    svc = service(tmp_path, recorded)
    try:
        results: dict[str, dict] = {}
        readers = [threading.Thread(target=lambda month=month: results.__setitem__(month, svc.dataset("position_rank", f"SHFE.{month}")))
                   for month in ("rb2610", "rb2701")]
        for reader in readers:
            reader.start()
        time.sleep(0.2)
        gate.set()
        for reader in readers:
            reader.join(5)
        assert results["rb2610"]["data"]["contract"] == "RB2610" and results["rb2701"]["data"]["contract"] == "RB2701"
        assert results["rb2701"]["data"]["longTotal"] == 2 * results["rb2610"]["data"]["longTotal"]
        assert results["rb2701"]["data"]["scope"] == "contract"
        assert recorded.calls.count("get_shfe_rank_table") == 1  # the table is read once for both months
        # A month the source does not list fails alone; the listed month after it is read normally.
        assert svc.dataset("fees_margin", "SHFE.rb9999")["error"] == "research_product_not_in_source"
        assert svc.dataset("fees_margin", "SHFE.rb2701")["data"]["contract"] == "rb2701"
        assert svc.dataset("fees_margin", "SHFE.rb2610")["data"]["contract"] == "rb2610"
        assert recorded.calls.count("futures_comm_info") == 1
        # Product-level datasets still answer every month from one entry.
        svc.dataset("spot_basis", "SHFE.rb2610")
        svc.dataset("spot_basis", "SHFE.rb2701")
        assert recorded.calls.count("futures_spot_price") == 1
    finally:
        svc.close()
    restarted = service(tmp_path, TwoMonths())
    try:
        assert restarted.dataset("fees_margin", "SHFE.rb2701")["data"]["contract"] == "rb2701"
        assert restarted.dataset("position_rank", "SHFE.rb2701")["data"]["contract"] == "RB2701"
        # Both served from the per-contract rows on disk (the trading calendar aside).
        assert [call for call in restarted.sources._module.calls if call != "tool_trade_date_hist_sina"] == []
    finally:
        restarted.close()


class GfexAkshare(RecordedAkshare):
    """AKShare 1.18.94's get_roll_yield_bar on GFEX: 工业硅/碳酸锂/多晶硅 are in its product → exchange map and
    read the exchange's daily table; 铂/钯 are not (symbol_market gives None → AttributeError on .upper())."""

    broken: frozenset[str] = frozenset()

    def get_roll_yield_bar(self, type_method, var, date):
        self._guard(f"get_roll_yield_bar:{var}")
        if var in {"PT", "PD"} or var in self.broken:
            raise AttributeError("'NoneType' object has no attribute 'upper'")
        return Rows([{"symbol": f"{var}2601", "settle": 9000, "close": 9010, "open_interest": 1, "volume": 1},
                     {"symbol": f"{var}2605", "settle": 9100, "close": 9110, "open_interest": 1, "volume": 1}])


def test_gfex_term_structure_reads_mapped_products_and_reports_unmapped_ones_as_not_in_source(tmp_path, caplog):
    """#932: only 铂/钯 lack AKShare's mapping; the whole-exchange gate also cut off 工业硅/碳酸锂/多晶硅. The map is
    checked before the call, so an error inside a mapped product's read stays a source error (Codex r4307878771)."""
    recorded = GfexAkshare()
    svc = service(tmp_path, recorded)
    svc.sources.roll_yield_products = lambda: frozenset({"SI", "LC", "PS", "RB"})  # AKShare's own map (cons)
    try:
        for symbol in ("GFEX.si2601", "GFEX.lc2601"):
            payload = svc.dataset("term_structure", symbol)
            assert payload["status"] == "ok" and payload["data"]["shape"] == "contango", symbol
        with caplog.at_level("WARNING"):
            for symbol in ("GFEX.pt2601", "GFEX.pd2601"):
                payload = svc.dataset("term_structure", symbol)
                assert payload["status"] == "unavailable" and payload["error"] == "research_product_not_in_source"
            assert "research_fetch_failed" not in caplog.text  # the source lacking a product is quiet
            recorded.broken = frozenset({"PS"})
            broken = svc.dataset("term_structure", "GFEX.ps2601")
            assert broken["error"] == "research_source_error:AttributeError" and "research_fetch_failed" in caplog.text
        assert "get_roll_yield_bar:PT" not in recorded.calls and "get_roll_yield_bar:PD" not in recorded.calls
    finally:
        svc.close()


def test_a_failure_back_off_ends_when_the_trading_day_turns(tmp_path):
    """#932: a failure just before 17:00 must not hold the new trading day's data back for the 10-minute back-off."""
    now = [datetime(2026, 9, 11, 7, 50, tzinfo=UTC)]  # 15:50 Beijing (a Friday): trading day 9-10
    recorded = RecordedAkshare()
    svc = service(tmp_path, recorded, clock=lambda: now[0])
    try:
        assert svc.dataset("spot_basis", "SHFE.rb2610")["status"] == "ok"
        now[0] += timedelta(minutes=65)  # 16:55: past the intraday hour, the refresh fails → back-off to 17:05
        recorded.failing.add("futures_spot_price")
        assert svc.dataset("spot_basis", "SHFE.rb2610")["status"] == "stale"
        now[0] += timedelta(minutes=3)  # 16:58: same trading day, still backing off
        assert svc.dataset("spot_basis", "SHFE.rb2610")["status"] == "stale"
        assert recorded.calls.count("futures_spot_price") == 2
        recorded.failing.clear()
        now[0] += timedelta(minutes=3)  # 17:01: trading day 9-11, inside the old back-off
        fresh = svc.dataset("spot_basis", "SHFE.rb2610")
        assert fresh["status"] == "ok" and recorded.calls.count("futures_spot_price") == 3
    finally:
        svc.close()


def test_upgrading_again_after_a_rollback_takes_the_newer_legacy_values(tmp_path):
    """#932: the old version kept writing research_cache after a rollback; the second upgrade must not keep the
    collection store's older value (INSERT OR IGNORE did), and an older legacy row never replaces a newer one."""
    connection = sqlite3.connect(tmp_path / "research.sqlite3", check_same_thread=False)
    day = last_trading_date(datetime.now(UTC))
    first = ResearchDataService(connection=connection, lock=threading.Lock(), sources=AkshareSources())
    first.close()

    def legacy(spot: float, fetched_at: float) -> None:
        payload = {"dataset": "spot_basis", "status": "ok", "asOf": day, "source": "生意社", "data": {"spotPrice": spot}}
        with connection:
            connection.execute("INSERT INTO research_cache VALUES ('spot_basis', 'rb', ?, ?, ?) ON CONFLICT(dataset, scope)"
                               " DO UPDATE SET fetched_at = excluded.fetched_at, payload_json = excluded.payload_json",
                               (day, fetched_at, json.dumps(payload, ensure_ascii=False)))

    def stored() -> tuple[float, float]:
        row = connection.execute("SELECT fetched_at, payload_json FROM research_live_cache WHERE key = ?",
                                 ("research:spot_basis:rb",)).fetchone()
        return row[0], json.loads(row[1])["data"]["spotPrice"]

    legacy(3100.0, 1000.0)
    ResearchDataService(connection=connection, lock=threading.Lock(), sources=AkshareSources()).close()  # upgrade
    assert stored() == (1000.0, 3100.0)
    legacy(3200.0, 2000.0)  # rolled back: the old version refreshed its own table
    ResearchDataService(connection=connection, lock=threading.Lock(), sources=AkshareSources()).close()  # upgrade again
    assert stored() == (2000.0, 3200.0)
    with connection:  # the new version refreshed the value since; the old row (older) must not replace it
        connection.execute("UPDATE research_live_cache SET fetched_at = 3000, payload_json = json_set(payload_json,"
                           " '$.data.spotPrice', 3300.0) WHERE key = 'research:spot_basis:rb'")
    ResearchDataService(connection=connection, lock=threading.Lock(), sources=AkshareSources()).close()
    assert stored() == (3000.0, 3300.0)


def test_an_old_product_keyed_cache_is_never_read_as_a_contract(tmp_path):
    """#487: rows written before the per-contract key do not say which month they were; the upgrade
    drops them for the contract datasets and keeps the product-level ones."""
    connection = sqlite3.connect(tmp_path / "research.sqlite3")
    day = last_trading_date(datetime.now(UTC))
    old = {"dataset": "fees_margin", "status": "ok", "asOf": day, "source": "九期网", "data": {"contract": "rb2610", "marginPerLot": 111}}
    basis = {"dataset": "spot_basis", "status": "ok", "asOf": day, "source": "生意社", "data": {"spotPrice": 3165.0}}
    with connection:
        connection.execute("CREATE TABLE research_cache (dataset TEXT NOT NULL, product TEXT NOT NULL, trading_day TEXT NOT NULL,"
                           " fetched_at REAL NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (dataset, product))")
        for payload in (old, basis):
            connection.execute("INSERT INTO research_cache VALUES (?, 'rb', ?, ?, ?)",
                               (payload["dataset"], day, time.time(), json.dumps(payload, ensure_ascii=False)))
    connection.close()
    recorded = RecordedAkshare()
    svc = service(tmp_path, recorded)
    try:
        assert svc.dataset("fees_margin", "SHFE.rb2701")["data"]["contract"] == "rb2701"
        assert svc.dataset("spot_basis", "SHFE.rb2701")["data"] == {"spotPrice": 3165.0}
        assert [call for call in recorded.calls if call != "tool_trade_date_hist_sina"] == ["futures_comm_info"]
    finally:
        svc.close()


def test_after_the_settlement_a_value_fetched_after_17_lasts_the_day_and_a_new_trading_day_refetches(tmp_path):
    now = [datetime(2026, 9, 11, 9, 30, tzinfo=UTC)]  # 17:30 Beijing: the day session's data is out
    recorded = RecordedAkshare()
    svc = service(tmp_path, recorded, clock=lambda: now[0])
    try:
        assert svc.dataset("spot_basis", "SHFE.rb2610")["status"] == "ok"
        now[0] += timedelta(hours=3)  # 20:30, same date: still that settled value
        svc.dataset("spot_basis", "SHFE.rb2610")
        assert recorded.calls.count("futures_spot_price") == 1
        now[0] += timedelta(days=3)  # 9-14 20:30 Beijing: a later trading day
        svc.dataset("spot_basis", "SHFE.rb2610")
        assert recorded.calls.count("futures_spot_price") == 2
    finally:
        svc.close()

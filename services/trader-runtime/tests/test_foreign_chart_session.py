"""外盘盘点 D (2026-10-21): a tradable 外盘主连 chart's snapshot carries CME's hours and status and, on the daily /
weekly charts drawn from 快期外盘主连, MT5's main-month quote — the client reads it, it keeps no CME clock of its own."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from test_owner_auth import ORIGIN, PASSWORD_HASH, SESSION_SECRET
from trader_runtime.foreign_market import cme_chart
from trader_runtime.main import create_app
from trader_runtime.shared_market import trading_schedule
from trader_runtime.trading_calendar import NS, cme_session, cme_trading_day

SYMBOL = "KQD.m@CME.NQ"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def ns(text: str) -> int:
    return int(at(text).timestamp() * NS)


def test_cme_session_says_open_daily_break_weekend_and_full_day_closures_with_the_next_open():
    assert cme_session(ns("2026-10-21T17:19:00+08:00")) == {"status": "trading"}
    assert cme_session(ns("2026-10-21T05:30:00+08:00")) == {
        "status": "closed", "closedFor": "break", "reopensAt": "2026-10-21T06:00+08:00"}
    # US winter time: the break and the open move an hour later in Beijing
    assert cme_session(ns("2026-12-02T06:30:00+08:00"))["reopensAt"] == "2026-12-02T07:00+08:00"
    assert cme_session(ns("2026-12-02T07:00:00+08:00"))["status"] == "trading"
    # 美东周五 17:30 → 周日 18:00 (Beijing Monday 06:00)
    assert cme_session(ns("2026-10-24T05:30:00+08:00")) == {
        "status": "closed", "closedFor": "weekend", "reopensAt": "2026-10-26T06:00+08:00"}
    assert cme_session(ns("2026-10-25T12:00:00+08:00"))["closedFor"] == "weekend"
    assert cme_session(ns("2026-10-26T06:30:00+08:00"))["status"] == "trading"
    # Christmas (CME_CLOSED_DAYS): shut from the 24th's close (美东) until Sunday 27th 18:00 (winter: Beijing 07:00)
    christmas = cme_session(ns("2026-12-25T12:00:00+08:00"))
    assert christmas == {"status": "closed", "closedFor": "holiday", "reopensAt": "2026-12-28T07:00+08:00"}
    assert cme_session(ns("2026-12-25T07:30:00+08:00"))["closedFor"] == "holiday"  # 美东 24 日 18:30：平日该开了
    # the trading-day rule itself is unchanged, the closures included
    assert cme_trading_day(ns("2026-12-25T07:30:00+08:00")) == "2026-12-28"


def _kqd_daily() -> dict:
    """The 快期外盘主连 daily workbench as the producer builds it: 00:00–23:59, a ~10-minute-old quote, “closed”."""
    return {"schemaVersion": 1, "dataStatus": "normal", "isCached": False, "ageSeconds": 612.0,
            "durationSeconds": 86400, "marketSessionStatus": "closed", "liveTrading": "disabled",
            "quote": {"symbol": SYMBOL, "name": "迷你纳指主连", "lastPrice": 25010.0, "price": 25010.0,
                      "quoteDatetime": "2026-10-21 17:08:00", "ageSeconds": 612.0, "marketSessionStatus": "closed",
                      "priceTick": 0.25, "volumeMultiple": 20.0, "dataStatus": "normal",
                      "tradingSchedule": trading_schedule({"day": [["00:00:00", "23:59:00"]], "night": []})},
            "bars": [{"datetime": 1, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0, "openInterest": 0.0}]}


def _mt5_row(last: float, quote_time: str) -> dict:
    return {"key": "CME.NQ", "source": "mt5", "contract": "ENQZ26", "last": last, "preSettlement": 24900.0,
            "change": last - 24900.0, "changePercent": round((last - 24900.0) / 24900.0 * 100, 2), "open": 24950.0,
            "high": 25100.0, "low": 24800.0, "bidPrice1": last - 0.25, "askPrice1": last + 0.25, "volume": 1200.0,
            "openInterest": None, "quoteTime": quote_time}


def test_a_daily_foreign_chart_takes_cme_hours_and_status_and_the_mt5_main_month_quote():
    now = at("2026-10-21T17:18:01+08:00")
    got = cme_chart(_kqd_daily(), _mt5_row(25040.5, "2026-10-21T17:18:00+08:00"), now)
    quote = got["quote"]
    assert (got["marketSessionStatus"], quote["marketSessionStatus"]) == ("trading", "trading")
    assert quote["tradingSchedule"] == {"day": [["06:00", "29:00"]], "night": [], "dayRule": "cme", "cmeHours": "equity"}
    assert (quote["lastPrice"], quote["bidPrice1"], quote["source"]) == (25040.5, 25040.25, "MT5 · ENQZ26")
    assert quote["quoteDatetime"] == "2026-10-21T17:18:00+08:00" and got["ageSeconds"] == quote["ageSeconds"] == 1.0
    assert "sessionClosedFor" not in quote
    assert got["bars"] == _kqd_daily()["bars"] and got.get("source") is None  # the K-lines stay 快期主连's
    # at the daily break: closed, why, and when it opens again — the same quote
    pause = cme_chart(_kqd_daily(), _mt5_row(25040.5, "2026-10-22T05:00:00+08:00"), at("2026-10-22T05:10:00+08:00"))
    assert pause["marketSessionStatus"] == "closed"
    assert (pause["quote"]["sessionClosedFor"], pause["quote"]["sessionReopensAt"]) == ("break", "2026-10-22T06:00+08:00")


def test_without_mt5_the_daily_chart_keeps_the_kqd_quote_and_its_age_but_still_cme_status():
    got = cme_chart(_kqd_daily(), None, at("2026-10-21T17:18:01+08:00"))
    assert got["quote"]["lastPrice"] == 25010.0 and got["ageSeconds"] == 612.0 and "source" not in got["quote"]
    assert got["marketSessionStatus"] == "trading" and got["quote"]["tradingSchedule"]["dayRule"] == "cme"


def test_an_mt5_minute_quote_quiet_for_over_half_an_hour_reads_closed_inside_cme_hours():
    packet = {**_kqd_daily(), "source": "MT5 · ENQZ26", "durationSeconds": 300,
              "quote": {**_kqd_daily()["quote"], "source": "MT5 · ENQZ26", "ageSeconds": 1.0,
                        "quoteDatetime": "2026-10-21T16:38:00+08:00"}}  # built 40 minutes ago, re-applied now
    got = cme_chart(packet, None, at("2026-10-21T17:18:01+08:00"))
    assert got["marketSessionStatus"] == "closed" and "sessionClosedFor" not in got["quote"]
    assert got["quote"]["lastPrice"] == 25010.0  # an MT5 chart's own quote is never replaced


def test_the_quote_schedule_keeps_the_cme_day_rule_across_the_boundary():
    assert trading_schedule({"day": [["06:00:00", "29:00:00"]], "night": [], "dayRule": "cme", "cmeHours": "grains"}) == {
        "day": [["06:00", "29:00"]], "night": [], "dayRule": "cme", "cmeHours": "grains"}
    assert "dayRule" not in trading_schedule({"day": [["09:00:00", "10:15:00"]], "night": []})


class _KqdDailySource:
    """The sidecar's public workbench for one 快期外盘主连 daily chart: a revision that never moves (快期's foreign
    quote is slow) answered after a short long poll."""

    def __init__(self, provider) -> None:
        self.provider = provider
        self.waits: list[str] = []

    async def read(self, path: str):
        if path == "/v1/public-market/instruments":
            return {"catalogStatus": "ready", "dataStatus": "normal", "items": [
                {"symbol": SYMBOL, "exchangeId": "KQD", "instrumentId": "m@CME.NQ", "instrumentName": "迷你纳指主连",
                 "productId": "NQ", "priceTick": 0.25, "volumeMultiple": 20,
                 "tradingSchedule": {"day": [["00:00:00", "23:59:00"]], "night": []}}]}
        if path == "/v1/public-market/calendar":
            return self.provider.market_calendar()
        query = parse_qs(urlsplit(path).query)
        if "after" in query:
            self.waits.append(query["waitMs"][0])
            await asyncio.sleep(.01)
        body = _kqd_daily()
        return {**body, "revision": "kqd-1", "bars": body["bars"],
                "quote": {**body["quote"], "tradingSchedule": {"day": [["00:00:00", "23:59:00"]], "night": []}}}


@pytest.fixture
async def market(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_TRADER_SHARED_MARKET_ACCOUNT_IDS", "tqkq-sim:source0")
    app = create_app(tmp_path / "cme.db", runtime_state_root=tmp_path / "state", agent_mode="native",
                     data_mode="fake", auth_mode="owner-password", owner_username="owner",
                     owner_password_hash=PASSWORD_HASH, owner_session_secret=SESSION_SECRET, cors_origins={ORIGIN})
    service = app.state.shared_market
    source = _KqdDailySource(app.state.data_provider)

    async def market_source(account_id):
        return 19001

    async def read(port, path):
        return await source.read(path)

    service.ctx.account_pool = SimpleNamespace(market_source=market_source, client=SimpleNamespace(market=read))
    app.state.store.ensure_trading_account("tqkq-sim:source0", execution_owner_user_id="cme-owner",
                                           provider_kind="tqkq-pool", display_name="source0", sidecar_url=None)
    yield SimpleNamespace(service=service, source=source)
    await service.close()
    await app.state.adapter.shutdown()
    app.state.store.close()


async def until(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


async def test_the_shared_market_daily_foreign_chart_follows_mt5_and_cme_without_a_kqd_change(market):
    service = market.service
    clock = [at("2026-10-21T17:18:01+08:00").timestamp()]
    service.clock = lambda: clock[0]
    row = [_mt5_row(25040.5, "2026-10-21T17:18:00+08:00")]
    service.foreign_quote = lambda symbol: row[0] if symbol == SYMBOL else None
    channel = await service.acquire_market(SYMBOL, 86400)
    quote = channel.packet["value"]["quote"]
    assert quote["lastPrice"] == 25040.5 and quote["source"] == "MT5 · ENQZ26"
    assert quote["marketSessionStatus"] == "trading" and quote["tradingSchedule"]["dayRule"] == "cme"
    assert channel.packet["value"]["workbench"]["quote"] == quote
    # MT5 moves while 快期's revision does not: the next round republishes the new quote
    row[0] = _mt5_row(25055.0, "2026-10-21T17:18:02+08:00")
    clock[0] += 2
    await until(lambda: channel.packet["value"]["quote"]["lastPrice"] == 25055.0)
    assert market.source.waits and set(market.source.waits) == {str(service.foreign_wait_ms)}
    # the clock reaches CME's daily break: the same view reads closed with the reason
    clock[0] = at("2026-10-22T05:10:00+08:00").timestamp()
    await until(lambda: channel.packet["value"]["workbench"]["marketSessionStatus"] == "closed")
    assert channel.packet["value"]["quote"]["sessionClosedFor"] == "break"
    service.release_market(channel)

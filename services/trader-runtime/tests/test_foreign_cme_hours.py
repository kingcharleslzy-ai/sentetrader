"""外盘 F2 (2026-10-21): each 外盘 product trades its own CME Globex hours (CME Group contract specs) — CBOT grains in
two periods (芝加哥 19:00–次日 07:45, 08:30–13:20), lean hogs in one day period (08:30–13:05), the rest 23 hours.
The chart snapshot, the strategy's session position and the 收盘提醒 all read them; Beijing hours follow each
trading day's own US daylight-saving offset. A 外盘 minute chart drawn from 快期外盘主连 (MT5 down) re-dates its bars
by the CME trading day."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from trader_runtime.account_reminders import closing_window, normalize_schedule
from trader_runtime.foreign_market import chart_packet, cme_chart
from trader_runtime.instruments import cme_hours_of
from trader_runtime.trading_calendar import NS, cme_schedule, cme_session, session_position, sessions_of_day

CHICAGO = ZoneInfo("America/Chicago")
SOYBEANS = "KQD.m@CBOT.ZS"


def chicago(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=CHICAGO).astimezone(UTC)


def ns(moment: datetime) -> int:
    return int(moment.timestamp() * NS)


def beijing(moment_ns: int) -> str:
    return datetime.fromtimestamp(moment_ns / NS, UTC).astimezone(ZoneInfo("Asia/Shanghai")).isoformat(timespec="minutes")


def test_the_catalog_names_each_products_cme_hours():
    assert {cme_hours_of(code) for code in ("KQD.m@CBOT.ZS", "KQD.m@CBOT.ZM", "KQD.m@CBOT.ZL", "KQD.m@CBOT.ZC",
                                             "KQD.m@CBOT.ZW", "CME.ZSEF27", "CME.ZWAZ26")} == {"grains"}
    assert cme_hours_of("CME.HEZ26") == "livestock"
    assert {cme_hours_of(code) for code in ("KQD.m@COMEX.GC", "KQD.m@NYMEX.CL", "CME.GCEZ26")} == {"globex"}


def test_soybeans_shut_from_7_45_to_8_30_and_from_13_20_to_19_00_chicago_time():
    grains = cme_hours_of(SOYBEANS)
    wednesday = "2026-10-21"
    assert cme_session(ns(chicago(f"{wednesday}T07:44")), grains) == {"status": "trading"}
    assert cme_session(ns(chicago(f"{wednesday}T07:50")), grains) == {
        "status": "closed", "closedFor": "intraday", "reopensAt": "2026-10-21T21:30+08:00"}
    assert cme_session(ns(chicago(f"{wednesday}T08:30")), grains) == {"status": "trading"}
    assert cme_session(ns(chicago(f"{wednesday}T13:19")), grains) == {"status": "trading"}
    assert cme_session(ns(chicago(f"{wednesday}T13:25")), grains) == {
        "status": "closed", "closedFor": "break", "reopensAt": "2026-10-22T08:00+08:00"}  # 芝加哥 19:00
    assert cme_session(ns(chicago(f"{wednesday}T18:59")), grains)["status"] == "closed"
    assert cme_session(ns(chicago(f"{wednesday}T19:00")), grains) == {"status": "trading"}
    # the 23-hour products trade through both pauses
    assert cme_session(ns(chicago(f"{wednesday}T07:50")))["status"] == "trading"
    assert cme_session(ns(chicago(f"{wednesday}T15:00")))["status"] == "trading"
    # Friday's 13:20 close to Sunday's 19:00 open is the weekend
    assert cme_session(ns(chicago("2026-10-23T14:00")), grains) == {
        "status": "closed", "closedFor": "weekend", "reopensAt": "2026-10-26T08:00+08:00"}
    # US winter time (after 2026-11-01): the same Chicago hours are an hour later in Beijing
    assert cme_session(ns(chicago("2026-11-04T07:50")), grains) == {
        "status": "closed", "closedFor": "intraday", "reopensAt": "2026-11-04T22:30+08:00"}


def test_grain_sessions_follow_each_trading_days_own_daylight_saving_offset():
    summer = cme_schedule(datetime(2026, 10, 21, 12, tzinfo=UTC), "grains")
    assert summer == {"day": [["08:00:00", "20:45:00"], ["21:30:00", "26:20:00"]], "night": [],
                      "dayRule": "cme", "cmeHours": "grains"}
    friday = sessions_of_day("2026-10-30", summer)           # US summer time: opens Thursday 19:00 CDT
    monday = sessions_of_day("2026-11-02", summer)           # after the switch: opens Sunday 19:00 CST
    assert [(beijing(item.start_ns), beijing(item.end_ns)) for item in friday] == [
        ("2026-10-30T08:00+08:00", "2026-10-30T20:45+08:00"), ("2026-10-30T21:30+08:00", "2026-10-31T02:20+08:00")]
    assert [(beijing(item.start_ns), beijing(item.end_ns)) for item in monday] == [
        ("2026-11-02T09:00+08:00", "2026-11-02T21:45+08:00"), ("2026-11-02T22:30+08:00", "2026-11-03T03:20+08:00")]


def test_lean_hogs_trade_only_8_30_to_13_05_chicago_on_weekdays():
    hogs = cme_hours_of("CME.HEZ26")
    assert cme_session(ns(chicago("2026-10-21T12:00")), hogs) == {"status": "trading"}
    assert cme_session(ns(chicago("2026-10-21T13:10")), hogs) == {
        "status": "closed", "closedFor": "break", "reopensAt": "2026-10-22T21:30+08:00"}
    assert cme_session(ns(chicago("2026-10-22T03:00")), hogs)["closedFor"] == "break"  # Globex 23 hours would trade
    assert cme_session(ns(chicago("2026-10-24T10:00")), hogs) == {
        "status": "closed", "closedFor": "weekend", "reopensAt": "2026-10-26T21:30+08:00"}
    assert cme_session(ns(chicago("2026-10-26T08:00")), hogs)["closedFor"] == "weekend"  # Monday before the open


def test_strategies_and_the_close_reminder_read_the_grain_hours():
    schedule = cme_schedule(hours="grains")
    paused = session_position(ns(chicago("2026-10-21T07:50")), schedule)
    assert (paused.trading_day, paused.in_session) == ("2026-10-21", False)
    late = session_position(ns(chicago("2026-10-21T13:00")), schedule)
    assert late.in_session and round(late.minutes_to_day_end) == 20
    # 收盘提醒: 5 minutes before the day's last close (13:20), never before the 07:45 pause
    reminder = normalize_schedule(schedule)
    assert closing_window(reminder, ns(chicago("2026-10-21T13:17"))) == ("day", "2026-10-21", 3)
    assert closing_window(reminder, ns(chicago("2026-10-21T07:42"))) is None


def _kqd_workbench(symbol: str, duration: int, bars: list[dict]) -> dict:
    return {"schemaVersion": 1, "dataStatus": "normal", "isCached": False, "ageSeconds": 1.0, "durationSeconds": duration,
            "marketSessionStatus": "trading", "liveTrading": "disabled",
            "quote": {"symbol": symbol, "name": "美豆主连", "lastPrice": 1050.0, "price": 1050.0,
                      "quoteDatetime": "2026-10-21 20:49:59", "ageSeconds": 1.0, "marketSessionStatus": "trading",
                      "dataStatus": "normal", "tradingSchedule": {"day": [["00:00", "23:59"]], "night": []}},
            "bars": bars}


def test_a_soybean_chart_snapshot_reads_closed_in_the_morning_pause_with_both_periods():
    now = chicago("2026-10-21T07:50")
    got = cme_chart(_kqd_workbench(SOYBEANS, 86400, []), None, now, symbol=SOYBEANS, duration=86400)
    quote = got["quote"]
    assert got["marketSessionStatus"] == quote["marketSessionStatus"] == "closed"
    assert (quote["sessionClosedFor"], quote["sessionReopensAt"]) == ("intraday", "2026-10-21T21:30+08:00")
    assert quote["tradingSchedule"] == {"day": [["08:00", "20:45"], ["21:30", "26:20"]], "night": [],
                                        "dayRule": "cme", "cmeHours": "grains"}


def test_a_lean_hog_month_snapshot_reads_the_weekend():
    row = {"key": "CME.HE", "source": "mt5", "contract": "HEZ26", "name": "CME瘦肉猪", "last": 80.5,
           "preSettlement": 80.0, "changePercent": 0.6, "quoteTime": "2026-10-24T02:04:59+08:00"}
    packet = chart_packet("CME.HEZ26", 300, row, [], {}, chicago("2026-10-24T10:00"))
    assert packet["marketSessionStatus"] == "closed" and packet["quote"]["sessionClosedFor"] == "weekend"
    assert packet["quote"]["tradingSchedule"]["cmeHours"] == "livestock"


def test_a_minute_chart_drawn_from_kqd_takes_cme_trading_days_its_daily_chart_keeps_its_own():
    # 美东 2026-10-21 08:00 (北京 20:00): CME day 10-21; the 快期 annotation (北京 18:00 换日) says 10-22.
    stamp = ns(datetime(2026, 10, 21, 12, tzinfo=UTC))
    bars = [{"datetime": stamp, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
             "openInterest": 0.0, "tradingDay": "2026-10-22"}]
    now = chicago("2026-10-21T09:00")
    minute = cme_chart(_kqd_workbench(SOYBEANS, 60, bars), None, now, symbol=SOYBEANS, duration=60)
    assert [bar["tradingDay"] for bar in minute["bars"]] == ["2026-10-21"]
    daily = cme_chart(_kqd_workbench(SOYBEANS, 86400, bars), None, now, symbol=SOYBEANS, duration=86400)
    assert daily["bars"] == bars
    # an MT5-drawn chart already carries CME days and is left as it is
    mt5 = cme_chart({**_kqd_workbench(SOYBEANS, 60, bars), "source": "MT5 · ZSEF27"}, None, now, symbol=SOYBEANS)
    assert mt5["bars"] == bars

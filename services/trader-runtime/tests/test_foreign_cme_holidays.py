"""外盘尾项 (2026-10-21): CME holidays and early closes from CME Group's holiday trading hours — a US holiday's
abbreviated Globex session belongs to the next trade date and halts at its group's time (stock indexes and
Treasuries 12:00 CT, metals and energy 13:30, FX 16:00), grains and hogs do not trade on it, Thanksgiving Friday and
Christmas Eve close early. The chart status, the strategy's session position and the CME trading day all read it."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from trader_runtime.instruments import cme_hours_of
from trader_runtime.trading_calendar import NS, cme_session, cme_trading_day, session_position

CHICAGO = ZoneInfo("America/Chicago")


def at(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=CHICAGO).astimezone(UTC).timestamp() * NS)


def closed(reopens: str) -> dict[str, str]:
    return {"status": "closed", "closedFor": "holiday", "reopensAt": reopens}


def test_the_catalog_names_each_products_holiday_group():
    assert {cme_hours_of(code) for code in ("KQD.m@CME.NQ", "CME.ENQZ26", "KQD.m@CBOT.YM")} == {"equity"}
    assert {cme_hours_of(code) for code in ("KQD.m@CBOT.ZN", "CME.TYAZ26")} == {"rates"}
    assert {cme_hours_of(code) for code in ("KQD.m@CME.6E", "CME.EU6Z26")} == {"fx"}
    assert {cme_hours_of(code) for code in ("KQD.m@COMEX.GC", "KQD.m@NYMEX.CL")} == {"globex"}


def test_thanksgiving_thursday_trades_for_friday_and_halts_by_group():
    # Wednesday 17:00 CT opens Friday's trade date: Thursday has none
    assert cme_trading_day(at("2026-11-25T17:30")) == cme_trading_day(at("2026-11-26T10:00")) == "2026-11-27"
    assert cme_session(at("2026-11-26T10:00"), "equity") == {"status": "trading"}
    assert cme_session(at("2026-11-26T12:30"), "equity") == closed("2026-11-27T07:00+08:00")  # 17:00 CT reopen
    assert cme_session(at("2026-11-26T12:30"), "globex") == {"status": "trading"}  # metals/energy until 13:30
    assert cme_session(at("2026-11-26T13:40"), "globex") == closed("2026-11-27T07:00+08:00")
    assert cme_session(at("2026-11-26T15:00"), "fx") == {"status": "trading"}  # fx until 16:00
    assert cme_session(at("2026-11-26T10:00"), "grains") == closed("2026-11-27T22:30+08:00")  # Friday 08:30 CT
    # Friday closes early: stock indexes 12:15 CT, metals 13:45, grains and hogs 12:05
    assert cme_session(at("2026-11-27T12:10"), "equity") == {"status": "trading"}
    assert cme_session(at("2026-11-27T12:20"), "equity") == closed("2026-11-30T07:00+08:00")
    assert cme_session(at("2026-11-27T13:40"), "globex") == {"status": "trading"}
    assert cme_session(at("2026-11-27T12:10"), "grains") == closed("2026-11-30T09:00+08:00")  # Sunday 19:00 CT
    assert cme_session(at("2026-11-27T12:10"), "livestock") == closed("2026-11-30T22:30+08:00")


def test_a_monday_holiday_belongs_to_tuesday_and_christmas_eve_closes_early():
    # MLK 2027: Sunday 17:00 CT opens Tuesday's trade date, Monday halts at 12:00 CT and reopens 17:00 CT
    assert cme_trading_day(at("2027-01-18T09:00")) == "2027-01-19"
    assert cme_session(at("2027-01-18T09:00"), "equity") == {"status": "trading"}
    assert cme_session(at("2027-01-18T12:30"), "rates")["closedFor"] == "holiday"
    assert cme_session(at("2027-01-18T09:00"), "grains") == closed("2027-01-19T09:00+08:00")  # Monday 19:00 CT
    position = session_position(at("2027-01-18T09:00"), {"dayRule": "cme", "cmeHours": "equity"})
    assert position.trading_day == "2027-01-19" and position.in_session
    # Christmas Eve 2026: 12:15 CT (stock indexes) / 12:45 CT (metals, energy, fx), then shut to Sunday
    assert cme_session(at("2026-12-24T12:30"), "equity") == closed("2026-12-28T07:00+08:00")
    assert cme_session(at("2026-12-24T12:30"), "fx") == {"status": "trading"}
    assert cme_session(at("2026-12-25T09:00"), "fx") == closed("2026-12-28T07:00+08:00")


def test_an_ordinary_day_keeps_its_regular_hours():
    assert cme_session(at("2026-10-21T12:30"), "equity") == {"status": "trading"}
    assert cme_session(at("2026-10-21T16:30"), "equity") == {
        "status": "closed", "closedFor": "break", "reopensAt": "2026-10-22T06:00+08:00"}

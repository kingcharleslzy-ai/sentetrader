"""CME Globex hours for the foreign paper engine (外盘撮合引擎), by product kind and trade date.

The same table as the Runtime's ``trading_calendar.CME_CLOSED_DAYS`` / ``CME_HOURS`` / ``CME_HOLIDAY_HOURS``
(source: CME Group contract specs and holiday trading hours, read 2026-10-21): the engine cannot import the
Runtime, so this module keeps a copy with no dependencies, and a Runtime test loads it by path and checks that the
two agree (``tests/test_cme_hours_sync.py``) — change both together.

Periods are minutes after 00:00 New York time of the trade date D (negative = the days before), wall clock:
Chicago is always New York − 1 hour, both on US daylight saving. A time outside every period of its trade date is
outside the session: no new order is taken and resting orders do not fill (prices still update).
"""
from __future__ import annotations

Periods = tuple[tuple[int, int], ...]

# No CME trade date (full closures); a holiday's own Globex session belongs to the next trade date.
CME_CLOSED_DAYS = frozenset({
    "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24", "2027-12-31",
})

_GLOBEX_DAY: Periods = ((-6 * 60, 17 * 60),)
CME_HOURS: dict[str, Periods] = {
    "globex": _GLOBEX_DAY,
    "equity": _GLOBEX_DAY,
    "rates": _GLOBEX_DAY,
    "fx": _GLOBEX_DAY,
    "grains": ((-4 * 60, 8 * 60 + 45), (9 * 60 + 30, 14 * 60 + 20)),
    "livestock": ((9 * 60 + 30, 14 * 60 + 5),),
}
DEFAULT_KIND = "globex"

_AFTER_MONDAY_HOLIDAY: dict[str, Periods] = {"equity": ((-1800, -660), (-360, 1020)), "rates": ((-1800, -660), (-360, 1020)),
                                             "globex": ((-1800, -570), (-360, 1020)), "fx": ((-1800, -420), (-360, 1020))}
_THANKSGIVING_FRIDAY: dict[str, Periods] = {"equity": ((-1800, -660), (-360, 795)), "rates": ((-1800, -660), (-360, 795)),
                                            "globex": ((-1800, -570), (-360, 885)), "fx": ((-1800, -420), (-360, 885)),
                                            "grains": ((570, 785),), "livestock": ((570, 785),)}
CME_HOLIDAY_HOURS: dict[str, dict[str, Periods]] = {
    "2026-09-08": _AFTER_MONDAY_HOLIDAY,
    "2026-11-27": _THANKSGIVING_FRIDAY,
    "2026-12-24": {"equity": ((-360, 795),), "rates": ((-360, 795),), "globex": ((-360, 825),), "fx": ((-360, 825),),
                   "grains": ((-240, 525), (570, 785)), "livestock": ((570, 785),)},
    "2027-01-19": _AFTER_MONDAY_HOLIDAY,
    "2027-02-16": _AFTER_MONDAY_HOLIDAY,
    "2027-06-01": _AFTER_MONDAY_HOLIDAY,
    "2027-06-21": dict.fromkeys(("equity", "rates", "globex", "fx"), ((-4680, -3540), (-360, 1020))),
    "2027-07-06": {**_AFTER_MONDAY_HOLIDAY, "rates": _AFTER_MONDAY_HOLIDAY["globex"], "grains": ((570, 860),)},
    "2027-09-07": _AFTER_MONDAY_HOLIDAY,
    "2027-11-26": _THANKSGIVING_FRIDAY,
}

# MT5 (AMP) root → hours kind; a root not listed trades the regular 23-hour Globex day.
ROOT_KINDS: dict[str, str] = {
    **dict.fromkeys(("ZCE", "ZLE", "ZME", "ZSE", "ZWA"), "grains"),
    "HE": "livestock",
    **dict.fromkeys(("ENQ", "EP", "M2K", "MES", "MNQ", "MYM", "RTY", "YM"), "equity"),
    **dict.fromkeys(("BP6", "CA6", "DA6", "EU6", "JY6", "NE6", "SF6"), "fx"),
    **dict.fromkeys(("FVA", "TUA", "TYA", "USA"), "rates"),
}


def kind_of(root: str | None) -> str:
    return ROOT_KINDS.get(root or "", DEFAULT_KIND)


def periods(kind: str, trade_date: str) -> Periods:
    """The periods of ``kind`` on ``trade_date``: its holiday hours that day, else its regular hours."""
    regular = CME_HOURS.get(kind, CME_HOURS[DEFAULT_KIND])
    return CME_HOLIDAY_HOURS.get(trade_date, {}).get(kind, regular)

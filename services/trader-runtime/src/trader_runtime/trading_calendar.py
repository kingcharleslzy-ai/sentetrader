"""Exchange trading-day and session arithmetic for strategy building blocks (pure functions).

Everything here is deterministic: no wall clock, no database, no provider. Times are K-line
style epoch nanoseconds; sessions are read in China Standard Time (UTC+8), the time zone of
TqSdk's ``Quote.trading_time`` schedule (``{"day": [["09:00:00", "10:15:00"], ...],
"night": [["21:00:00", "25:00:00"]]}``, night ends past 24:00 mean the next calendar day).

Trading-day rule, identical to TqSdk's own ``_get_trading_day_from_timestamp``: a time at or
after 18:00 (``TRADING_DAY_TURN_HOUR``) belongs to the next day, and a Saturday / Sunday rolls on
to Monday — so Friday's night session belongs to Monday. Holidays are not known from the
schedule: when the caller passes the trading days it knows (the exchange-calendar ``tradingDay``
annotation the sidecar puts on every bar, the days of the daily K-line series, or the sidecar's
calendar summary via ``summary_trading_days``), a naive day inside that span snaps forward to
the first known trading day. A moment outside the known span keeps the weekend rule instead of
jumping a gap. A bar's own ``tradingDay`` annotation always wins.

This is the Runtime's one trading-day rule (2026-10-12): order expiry, backfilled fills, the
direct-stop overnight bucket, delivery reminders and the strategy engine all read it.

Daily anchors (昨高 / 昨低 / 昨收 / 今开 / 前 N 日) are always read from the 86400 series,
which TqSdk aligns by trading day (the night session belongs to the next trading day), never
by cutting minute bars at calendar midnight.
"""
from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

CST = timezone(timedelta(hours=8))
NS = 1_000_000_000
# The one cut for "which trading day does this moment belong to" (2026-10-12). 18:00 is TqSdk's own
# rule, the sidecar's (fill ``tradingDay`` annotations, the account summary's ``tradingDay``, the
# calendar summary's bucket) and so the strategy protections' overnight bucket. The day session
# closes by 15:15 and the night session opens at 21:00 (call auction 20:55), so nothing trades
# between 18:00 and 21:00: an order entered then waits for the night session, which belongs to the
# next trading day. The Runtime's order backfill used to cut at 20:00 with no recorded reason,
# which put 18:00–20:00 pre-night orders on the ended day and disagreed with the real fills.
TRADING_DAY_TURN_HOUR = 18


def local_time(timestamp_ns: int) -> datetime:
    return datetime.fromtimestamp(int(timestamp_ns) / NS, tz=UTC).astimezone(CST)


def _next_weekday(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def naive_trading_day(timestamp_ns: int) -> str:
    """TqSdk's rule without a calendar: ≥18:00 → next day, weekend → Monday."""
    current = local_time(timestamp_ns)
    day = current.date() + timedelta(days=1) if current.hour >= TRADING_DAY_TURN_HOUR else current.date()
    return _next_weekday(day).isoformat()


def trading_day_of(timestamp_ns: int, trading_days: Sequence[str] | None = None) -> str:
    """The trading day a moment belongs to. With known trading days (sorted ISO strings), a day
    inside their span that is not one of them (a holiday) snaps forward to the next known trading
    day; a day outside the span keeps the weekend rule (no jumping across a gap the calendar does
    not cover)."""
    candidate = naive_trading_day(timestamp_ns)
    if trading_days and trading_days[0] <= candidate:
        index = bisect_left(trading_days, candidate)
        if index < len(trading_days):
            return trading_days[index]
    return candidate


def trading_day_at(moment: str | datetime, trading_days: Sequence[str] | None = None) -> str:
    """``trading_day_of`` for an aware datetime or ISO text (a naive one is read as UTC, the way the
    store writes its timestamps)."""
    when = moment if isinstance(moment, datetime) else datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return trading_day_of(int(when.timestamp() * NS), trading_days)


def summary_trading_days(summary: Any) -> list[str]:
    """The known trading days (sorted ISO) in the sidecar's calendar summary
    (``/v1/public-market/calendar``): its ``tradingDays`` window (past and coming days, when the
    sidecar sends it), the recent ended days and the current one. Every listed span is complete —
    the sidecar lists every trading day inside it — so a weekday missing inside the span is a
    holiday. Anything unreadable gives an empty list (the caller's no-calendar fallback)."""
    if not isinstance(summary, dict):
        return []
    days: set[str] = set()
    for key in ("tradingDays", "recentTradingDays"):
        value = summary.get(key)
        if isinstance(value, list):
            days.update(str(item) for item in value)
    for key in ("tradingDay", "previousTradingDay"):
        if summary.get(key):
            days.add(str(summary[key]))
    return sorted(day for day in days if _iso_day(day))


def _iso_day(text: str) -> bool:
    try:
        return date.fromisoformat(text).isoformat() == text
    except ValueError:
        return False


def daily_bar_day(bar: dict[str, Any]) -> str:
    """The trading day of one 86400 K-line: its annotation, else the CST date of its datetime
    (TqSdk stamps a daily bar at 00:00 CST of its trading day)."""
    annotated = bar.get("tradingDay")
    if isinstance(annotated, str) and annotated:
        return annotated
    return local_time(int(bar["datetime"])).date().isoformat()


def bar_trading_day(bar: dict[str, Any], trading_days: Sequence[str] | None = None) -> str:
    annotated = bar.get("tradingDay")
    if isinstance(annotated, str) and annotated:
        return annotated
    return trading_day_of(int(bar["datetime"]), trading_days)


def daily_index(daily_bars: Sequence[dict[str, Any]] | None) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Sorted trading days and the daily bar of each, from a 86400 series (the last, still
    forming day included — callers only read its open)."""
    table: dict[str, dict[str, Any]] = {}
    for bar in daily_bars or []:
        table[daily_bar_day(bar)] = bar
    return sorted(table), table


def previous_trading_day(day: str, trading_days: Sequence[str] | None = None) -> str:
    """The trading day before ``day``: the calendar's when ``day`` lies inside its span, else the
    weekday rule — as in ``trading_day_of``, a calendar that ends early (a refresh failed and the
    old one was kept) must not anchor a later day, and its night session, on its own last day."""
    if trading_days and trading_days[0] < day <= trading_days[-1]:
        return trading_days[bisect_left(trading_days, day) - 1]
    current = date.fromisoformat(day) - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current.isoformat()


# -- CME (外盘三期, 2026-10-20) ----------------------------------------------------------------------
# A foreign product's trading day is CME Globex's: a moment at or after 18:00 New York time belongs to the
# next day, Saturday/Sunday roll on to Monday and the days with no CME trade date skip ahead (the same list as the
# foreign paper engine's ``cme_hours`` module, with these hours too — change both together; tests/test_cme_hours_sync.py
# checks they agree). Its schedule, in this
# module's Beijing-time format, is the product's regular CME Globex periods (``CME_HOURS``), marked
# ``"dayRule": "cme"`` so ``session_position`` reads the CME trading day instead of the domestic 18:00-Beijing
# rule, and ``"cmeHours"`` naming which periods — ``sessions_of_day`` builds each trading day's sessions from New
# York time (its own daylight-saving offset) and that day's holiday hours (``CME_HOLIDAY_HOURS``). Bars carry their
# CME ``tradingDay``.
NEW_YORK = ZoneInfo("America/New_York")
# No CME trade date (外盘尾项 2026-10-21, CME Group holiday calendar, see ``CME_HOLIDAY_HOURS``): full closures
# (Christmas, New Year, Good Friday) and the US holidays whose abbreviated Globex session belongs to the next
# trade date (Labor Day, Thanksgiving, MLK, Presidents', Memorial, Juneteenth, Independence Day).
CME_CLOSED_DAYS = frozenset({
    "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24", "2027-12-31",
})

# CME Globex hours by kind (外盘 F2, 2026-10-21): minutes after 00:00 New York time of the trading day D (negative =
# the days before). Chicago (CT) is always New York (ET) − 1 hour — both follow US daylight saving. Source: the
# CME Group contract-specs pages (cmegroup.com/markets/…/<product>.contractSpecs.html), read 2026-10-21:
# * globex (metals, energy), equity (stock indexes), rates (Treasuries), fx — the same regular hours, e.g. the
#   10-Year T-Note “Sunday - Friday 6:00 p.m. - 5:00 p.m. ET”: one period with a 60-minute daily break; the four
#   kinds differ only on holidays.
# * grains — CBOT soybeans, soybean meal, soybean oil, corn and Chicago SRW wheat (ZS ZM ZL ZC ZW): “Sunday -
#   Friday 7:00 p.m. - 7:45 a.m. CT and Monday - Friday 8:30 a.m. - 1:20 p.m. CT” — trading day D opens D−1 19:00 CT,
#   pauses 07:45–08:30 CT and closes 13:20 CT.
# * livestock — CME lean hogs (HE): “Monday - Friday: 8:30 a.m. - 1:05 p.m. CT”, day hours only.
CmePeriods = tuple[tuple[int, int], ...]
_GLOBEX_DAY: CmePeriods = ((-6 * 60, 17 * 60),)
CME_HOURS: dict[str, CmePeriods] = {
    "globex": _GLOBEX_DAY,
    "equity": _GLOBEX_DAY,
    "rates": _GLOBEX_DAY,
    "fx": _GLOBEX_DAY,
    "grains": ((-4 * 60, 8 * 60 + 45), (9 * 60 + 30, 14 * 60 + 20)),
    "livestock": ((9 * 60 + 30, 14 * 60 + 5),),
}
DEFAULT_CME_HOURS = "globex"

# Trade dates whose hours differ from ``CME_HOURS`` (same minutes format; a kind not listed keeps its regular
# hours). Source: CME Group's holiday trading hours (cmegroup.com/trading-hours.html, the per-product Globex
# schedule it serves for each listed holiday, read 2026-10-21 for every product we trade; CME finalises them about
# two weeks before each holiday, so re-read then). After a Monday holiday (Sunday 17:00 CT opens the Tuesday trade
# date) the Globex kinds halt on the holiday at 12:00 CT (equity, rates), 13:30 CT (metals, energy) or 16:00 CT
# (fx) and reopen 17:00 CT; grains and hogs do not trade on the holiday. Thanksgiving's Thursday session belongs to
# Friday, which closes early; Christmas Eve closes early.
_AFTER_MONDAY_HOLIDAY: dict[str, CmePeriods] = {"equity": ((-1800, -660), (-360, 1020)), "rates": ((-1800, -660), (-360, 1020)),
                         "globex": ((-1800, -570), (-360, 1020)), "fx": ((-1800, -420), (-360, 1020))}
_THANKSGIVING_FRIDAY: dict[str, CmePeriods] = {"equity": ((-1800, -660), (-360, 795)), "rates": ((-1800, -660), (-360, 795)),
                        "globex": ((-1800, -570), (-360, 885)), "fx": ((-1800, -420), (-360, 885)),
                        "grains": ((570, 785),), "livestock": ((570, 785),)}
CME_HOLIDAY_HOURS: dict[str, dict[str, CmePeriods]] = {
    "2026-09-08": _AFTER_MONDAY_HOLIDAY,
    "2026-11-27": _THANKSGIVING_FRIDAY,
    "2026-12-24": {"equity": ((-360, 795),), "rates": ((-360, 795),), "globex": ((-360, 825),), "fx": ((-360, 825),),
                   "grains": ((-240, 525), (570, 785)), "livestock": ((570, 785),)},
    "2027-01-19": _AFTER_MONDAY_HOLIDAY,
    "2027-02-16": _AFTER_MONDAY_HOLIDAY,
    "2027-06-01": _AFTER_MONDAY_HOLIDAY,
    # Juneteenth (Friday): Thursday 17:00 CT – Friday 12:00 CT belongs to Monday's trade date.
    "2027-06-21": dict.fromkeys(("equity", "rates", "globex", "fx"), ((-4680, -3540), (-360, 1020))),
    # Independence Day (Monday): rates halt with metals and energy; grains skip Monday evening.
    "2027-07-06": {**_AFTER_MONDAY_HOLIDAY, "rates": _AFTER_MONDAY_HOLIDAY["globex"], "grains": ((570, 860),)},
    "2027-09-07": _AFTER_MONDAY_HOLIDAY,
    "2027-11-26": _THANKSGIVING_FRIDAY,
}


def cme_hours_kind(schedule: dict[str, Any] | None) -> str:
    """Which ``CME_HOURS`` a CME schedule names (``cmeHours``; absent or unknown is the 23-hour Globex day)."""
    kind = schedule.get("cmeHours") if isinstance(schedule, dict) else None
    return kind if isinstance(kind, str) and kind in CME_HOURS else DEFAULT_CME_HOURS


def cme_rule(schedule: dict[str, Any] | None) -> dict[str, Any]:
    """The CME marks a normalized schedule keeps (``dayRule`` and ``cmeHours``), empty for any other schedule —
    the one place the schedule normalizers (chart quote, 收盘提醒) copy them from."""
    if not isinstance(schedule, dict) or schedule.get("dayRule") != "cme":
        return {}
    return {"dayRule": "cme", "cmeHours": cme_hours_kind(schedule)}


def cme_trading_day(timestamp_ns: int, closed_days: frozenset[str] = CME_CLOSED_DAYS) -> str:
    current = datetime.fromtimestamp(int(timestamp_ns) / NS, tz=UTC).astimezone(NEW_YORK)
    day = current.date() + timedelta(days=1) if current.hour >= TRADING_DAY_TURN_HOUR else current.date()
    while day.weekday() >= 5 or day.isoformat() in closed_days:
        day += timedelta(days=1)
    return day.isoformat()


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def cme_schedule(at: datetime | None = None, hours: str = DEFAULT_CME_HOURS) -> dict[str, Any]:
    """A product's regular CME Globex hours (``CME_HOURS[hours]``) in the Beijing-time schedule format at the US
    offset of ``at``: the 23-hour day of trading day D opens D 06:00 Beijing in US summer time (D 07:00 in winter)
    and closes past midnight (29:00 / 30:00); CBOT grains run D 08:00–20:45 and 21:30–26:20 in summer time.
    Holiday hours are in the sessions (``sessions_of_day``), not here."""
    moment = (at or datetime.now(UTC)).astimezone(NEW_YORK)
    shift = (8 - int((moment.utcoffset() or timedelta()).total_seconds() // 3600)) * 60  # New York → Beijing
    kind = hours if hours in CME_HOURS else DEFAULT_CME_HOURS
    periods = [[_clock(start + shift), _clock(end + shift)] for start, end in CME_HOURS[kind]]
    return {"day": periods, "night": [], "dayRule": "cme", "cmeHours": kind}


@lru_cache(maxsize=4096)
def cme_sessions(trading_day: str, hours: str = DEFAULT_CME_HOURS, holidays: bool = True) -> tuple[Session, ...]:
    """One CME trade date's sessions for a kind of hours, from New York wall-clock time (each moment's own US
    offset): its holiday hours (``CME_HOLIDAY_HOURS``) when ``holidays``, else the regular ``CME_HOURS``."""
    kind = hours if hours in CME_HOURS else DEFAULT_CME_HOURS
    periods = (CME_HOLIDAY_HOURS.get(trading_day, {}) if holidays else {}).get(kind, CME_HOURS[kind])
    midnight = datetime.combine(date.fromisoformat(trading_day), datetime.min.time(), tzinfo=NEW_YORK)

    def at(minutes: int) -> int:
        wall = (midnight.replace(tzinfo=None) + timedelta(minutes=minutes)).replace(tzinfo=NEW_YORK)
        return int(wall.timestamp()) * NS
    return tuple(Session(at(start), at(end), False) for start, end in periods)


def _next_cme_day(day: str, closed_days: frozenset[str]) -> str:
    current = date.fromisoformat(day) + timedelta(days=1)
    while current.weekday() >= 5 or current.isoformat() in closed_days:
        current += timedelta(days=1)
    return current.isoformat()


def _cme_next_session(timestamp_ns: int, hours: str, holidays: bool) -> tuple[Session, bool]:
    """The first CME session of ``hours`` that has not ended at ``timestamp_ns`` (inside a session: that one), and
    whether it is its trading day's first — on the CME holiday calendar, or the regular one when not ``holidays``."""
    closed_days = CME_CLOSED_DAYS if holidays else frozenset()
    day = cme_trading_day(timestamp_ns, closed_days)
    for _ in range(14):
        for index, item in enumerate(cme_sessions(day, hours, holidays)):
            if item.end_ns >= timestamp_ns:
                return item, index == 0
        day = _next_cme_day(day, closed_days)  # after the day's last close
    raise ValueError("no CME session ahead")


def cme_session(timestamp_ns: int, hours: str = DEFAULT_CME_HOURS) -> dict[str, Any]:
    """CME Globex open or shut at one moment for a product's hours (外盘图表快照与策略共用这一条规则，外盘盘点 D
    2026-10-21；按品种时段 F2；节假日时段 2026-10-21 尾项）：``status`` trading / closed；closed 时 ``closedFor`` 是
    intraday（交易日内两段之间，CBOT 谷物芝加哥 07:45–08:30）、break（两个交易日之间的每日停盘：23 小时品种美东
    17:00–18:00，谷物芝加哥 13:20–19:00，瘦肉猪 13:05–次日 08:30）、weekend（周五收盘到周一的交易日开盘）或
    holiday（CME 节假日：全天休市、提前收盘或节日当天的停盘，``CME_CLOSED_DAYS`` / ``CME_HOLIDAY_HOURS``），
    ``reopensAt`` 是下一次开盘的北京时间。"""
    kind = hours if hours in CME_HOURS else DEFAULT_CME_HOURS
    opens, first = _cme_next_session(timestamp_ns, kind, True)
    if opens.start_ns <= timestamp_ns:
        return {"status": "trading"}
    regular, _ = _cme_next_session(timestamp_ns, kind, False)
    if regular.start_ns != opens.start_ns:
        reason = "holiday"
    elif not first:
        reason = "intraday"
    else:
        reason = "weekend" if date.fromisoformat(cme_trading_day(opens.start_ns)).weekday() == 0 else "break"
    reopens = datetime.fromtimestamp(opens.start_ns / NS, tz=UTC).astimezone(CST).isoformat(timespec="minutes")
    return {"status": "closed", "closedFor": reason, "reopensAt": reopens}


# -- sessions -----------------------------------------------------------------------------------

def _seconds(text: str) -> int:
    hours, minutes, seconds = (int(part) for part in str(text).split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _parse_periods(day_periods: Any, night_periods: Any) -> tuple[list[tuple[int, int]], list[tuple[int, int]]] | None:
    try:
        day = [(_seconds(start), _seconds(end)) for start, end in day_periods or []]
        night = [(_seconds(start), _seconds(end)) for start, end in night_periods or []]
    except (TypeError, ValueError):
        return None
    return (day, night) if day or night else None


Periods = tuple[tuple[str, ...], ...]
ScheduleKey = tuple[Periods, Periods]


def _schedule_key(schedule: dict[str, Any]) -> ScheduleKey | None:
    """The schedule's periods as nested tuples when it has TqSdk's shape (lists of string periods),
    so its parse can be remembered; None for any other shape (parsed every time)."""
    key = []
    for part in ("day", "night"):
        periods = schedule.get(part) or []
        if not isinstance(periods, list | tuple) or not all(
                isinstance(period, list | tuple) and all(isinstance(item, str) for item in period) for period in periods):
            return None
        key.append(tuple(tuple(period) for period in periods))
    return key[0], key[1]


@lru_cache(maxsize=64)
def _parsed(key: ScheduleKey) -> tuple[list[tuple[int, int]], list[tuple[int, int]]] | None:
    return _parse_periods(key[0], key[1])


def parse_schedule(schedule: dict[str, Any] | None) -> tuple[list[tuple[int, int]], list[tuple[int, int]]] | None:
    """(day periods, night periods) in seconds after local midnight; None when unusable. Parsed once
    per distinct schedule (a copy is returned)."""
    if not isinstance(schedule, dict):
        return None
    key = _schedule_key(schedule)
    if key is None:
        return _parse_periods(schedule.get("day"), schedule.get("night"))
    parsed = _parsed(key)
    return None if parsed is None else (list(parsed[0]), list(parsed[1]))


@dataclass(frozen=True)
class Session:
    start_ns: int
    end_ns: int
    night: bool


def sessions_of_day(trading_day: str, schedule: dict[str, Any] | None,
                    trading_days: Sequence[str] | None = None) -> list[Session]:
    """The absolute trading sessions of one trading day, in time order: the night periods on the
    previous trading day's evening, then the day periods on the trading day itself."""
    if not isinstance(schedule, dict):
        return []
    if schedule.get("dayRule") == "cme":
        # CME 时段按美东时间排（随美国夏令时，跨夏令时切换的历史也对，外盘三期），节假日按 CME 节假日时段表。
        return list(cme_sessions(trading_day, cme_hours_kind(schedule)))
    key = _schedule_key(schedule)
    parsed = parse_schedule(schedule) if key is None else _parsed(key)
    if parsed is None:
        return []
    evening = previous_trading_day(trading_day, trading_days)
    if key is None:
        return _sessions(trading_day, evening, parsed)
    return list(_remembered_sessions(trading_day, evening, key))   # built once per day and schedule


def _sessions(trading_day: str, evening: str,
              parsed: tuple[list[tuple[int, int]], list[tuple[int, int]]] | None) -> list[Session]:
    if parsed is None:
        return []
    day_periods, night_periods = parsed
    midnight = int(datetime.combine(date.fromisoformat(trading_day), datetime.min.time(), tzinfo=CST).timestamp())
    evening_midnight = int(datetime.combine(date.fromisoformat(evening), datetime.min.time(), tzinfo=CST).timestamp())
    result = [Session((evening_midnight + start) * NS, (evening_midnight + end) * NS, True) for start, end in night_periods]
    result += [Session((midnight + start) * NS, (midnight + end) * NS, False) for start, end in day_periods]
    return sorted(result, key=lambda item: item.start_ns)


@lru_cache(maxsize=4096)
def _remembered_sessions(trading_day: str, evening: str, key: ScheduleKey) -> tuple[Session, ...]:
    return tuple(_sessions(trading_day, evening, _parsed(key)))


@dataclass(frozen=True)
class SessionPosition:
    trading_day: str
    in_session: bool
    night: bool
    minutes_to_session_end: float | None
    minutes_to_day_end: float | None
    session_end_ns: int | None
    day_end_ns: int | None


def session_position(timestamp_ns: int, schedule: dict[str, Any] | None,
                     trading_days: Sequence[str] | None = None) -> SessionPosition:
    """Where a decision time sits in its trading day: inside a session (start ≤ t ≤ end, so a bar
    closing exactly on the bell still counts), whether that is the night session, minutes left
    in this session and minutes left until the trading day's last session closes. Without a
    usable schedule only the trading day is known (the rest is None / False)."""
    if isinstance(schedule, dict) and schedule.get("dayRule") == "cme":
        day = cme_trading_day(timestamp_ns)
    else:
        day = trading_day_of(timestamp_ns, trading_days)
    sessions = sessions_of_day(day, schedule, trading_days)
    if not sessions:
        return SessionPosition(day, False, False, None, None, None, None)
    day_end = sessions[-1].end_ns
    current = next((item for item in sessions if item.start_ns <= timestamp_ns <= item.end_ns), None)
    if current is None:
        return SessionPosition(day, False, False, None, (day_end - timestamp_ns) / NS / 60, None, day_end)
    return SessionPosition(day, True, current.night, (current.end_ns - timestamp_ns) / NS / 60,
                           (day_end - timestamp_ns) / NS / 60, current.end_ns, day_end)


def is_night(timestamp_ns: int, schedule: dict[str, Any] | None = None,
             trading_days: Sequence[str] | None = None) -> bool:
    """Night session: by the schedule when it is usable, else 18:00–06:00 local. CME's one 23-hour session has
    no night part of its own, so a foreign product's night is the same 18:00–06:00 Beijing hours (“夜盘不开新仓”
    keeps meaning the Beijing night, 外盘三期)."""
    position = session_position(timestamp_ns, schedule, trading_days)
    if position.in_session and not (isinstance(schedule, dict) and schedule.get("dayRule") == "cme"):
        return position.night
    hour = local_time(timestamp_ns).hour
    return hour >= TRADING_DAY_TURN_HOUR or hour < 6


def minutes_of_day(timestamp_ns: int) -> int:
    current = local_time(timestamp_ns)
    return current.hour * 60 + current.minute


def first_bars_of_day(bars: Sequence[dict[str, Any]], index: int, count: int, *,
                      schedule: dict[str, Any] | None = None,
                      trading_days: Sequence[str] | None = None) -> list[dict[str, Any]] | None:
    """The first ``count`` bars of the trading day that bar ``index`` belongs to (the opening
    range), or None while that range is not complete at ``index`` or the series may not reach
    back to the day's first bar (it must show the previous day, or start exactly on the first
    session's bell)."""
    day = bar_trading_day(bars[index], trading_days)
    start = index
    while start > 0 and bar_trading_day(bars[start - 1], trading_days) == day:
        start -= 1
    if start == 0:
        sessions = sessions_of_day(day, schedule, trading_days)
        if not sessions or int(bars[0]["datetime"]) != sessions[0].start_ns:
            return None
    if index - start + 1 < count:
        return None
    return list(bars[start:start + count])

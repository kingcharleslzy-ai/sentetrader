"""一个交易日口径 (2026-10-12)：18:00 换日、夜盘归下一交易日、节假日来自交易所日历，过期单、
补录成交、直接保护的隔夜滚桶与交割提醒共用同一份日历来源；日历读不到时各自的回退。

日历都是示意数据：国庆按 2026-10-01～10-07 休市、10-08 开市；春节按 2027-02-08～02-12 休市；
2028 春节按 01-25～02-01 休市（交割月前一个月月末落在假期里）。"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from trader_runtime import exchange_calendar
from trader_runtime.autonomy import direct_protection_bucket
from trader_runtime.data_providers import SidecarReadOnlyDataProvider, provider_read_snapshot
from trader_runtime.delivery_rules import delivery_notice, trading_today
from trader_runtime.exchange_calendar import calendar_trading_days
from trader_runtime.order_backfill import synthesize_fills
from trader_runtime.research_desk import debate_sessions
from trader_runtime.trading_calendar import (
    TRADING_DAY_TURN_HOUR,
    previous_trading_day,
    sessions_of_day,
    summary_trading_days,
    trading_day_at,
)

CST = timezone(timedelta(hours=8))


def calendar(start: str, end: str, holidays: tuple[tuple[str, str], ...] = ()) -> list[str]:
    """Weekdays from start to end, minus the holiday ranges (inclusive)."""
    off = set()
    for first, last in holidays:
        day = date.fromisoformat(first)
        while day <= date.fromisoformat(last):
            off.add(day)
            day += timedelta(days=1)
    days, day = [], date.fromisoformat(start)
    while day <= date.fromisoformat(end):
        if day.weekday() < 5 and day not in off:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


NATIONAL_DAY = calendar("2026-09-01", "2026-11-30", (("2026-10-01", "2026-10-07"),))
SPRING_2027 = calendar("2027-01-04", "2027-03-31", (("2027-02-08", "2027-02-12"),))
SPRING_2028 = calendar("2028-01-03", "2028-02-29", (("2028-01-25", "2028-02-01"),))


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=CST)


@pytest.fixture(autouse=True)
def fresh_calendar_cache():
    exchange_calendar._cache.clear()
    yield
    exchange_calendar._cache.clear()


# -- the one rule ------------------------------------------------------------------------------

def test_the_turn_is_eighteen_hundred_and_night_sessions_belong_to_the_next_trading_day():
    assert TRADING_DAY_TURN_HOUR == 18
    assert trading_day_at(at("2026-09-29 17:59"), NATIONAL_DAY) == "2026-09-29"
    # 18:00–21:00 nothing trades; an order entered then waits for the night session → next day.
    assert trading_day_at(at("2026-09-29 18:00"), NATIONAL_DAY) == "2026-09-30"
    assert trading_day_at(at("2026-09-29 19:30"), NATIONAL_DAY) == "2026-09-30"
    assert trading_day_at(at("2026-09-29 20:30"), NATIONAL_DAY) == "2026-09-30"
    # Friday night → Monday, with or without a calendar.
    assert trading_day_at(at("2026-10-09 21:30"), NATIONAL_DAY) == "2026-10-12"
    assert trading_day_at(at("2026-10-09 21:30")) == "2026-10-12"
    assert trading_day_at(at("2026-10-10 01:30"), NATIONAL_DAY) == "2026-10-12"


def test_holidays_snap_to_the_first_trading_day_after_them():
    assert trading_day_at(at("2026-09-30 14:00"), NATIONAL_DAY) == "2026-09-30"
    assert trading_day_at(at("2026-09-30 19:00"), NATIONAL_DAY) == "2026-10-08"   # 节前最后一晚
    assert trading_day_at(at("2026-10-03 10:00"), NATIONAL_DAY) == "2026-10-08"
    assert trading_day_at(at("2026-10-07 22:00"), NATIONAL_DAY) == "2026-10-08"
    assert trading_day_at(at("2027-02-05 19:00"), SPRING_2027) == "2027-02-15"
    assert trading_day_at(at("2027-02-10 10:00"), SPRING_2027) == "2027-02-15"
    # Without a calendar the weekend rule alone cannot see the holiday.
    assert trading_day_at(at("2026-10-03 10:00")) == "2026-10-05"


def test_the_summary_reader_unions_the_window_and_the_recent_days():
    summary = {"tradingDay": "2026-10-08", "previousTradingDay": "2026-09-30",
               "recentTradingDays": ["2026-09-30", "2026-09-29"],
               "tradingDays": ["2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"]}
    assert summary_trading_days(summary) == ["2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"]
    # An older sidecar sends no window: the recent days and the current one still count.
    assert summary_trading_days({"tradingDay": "2026-10-08", "recentTradingDays": ["2026-09-30"]}) == [
        "2026-09-30", "2026-10-08"]
    assert summary_trading_days({"recentTradingDays": ["bad", "2026-09-30"]}) == ["2026-09-30"]
    assert summary_trading_days(None) == []


def test_the_shared_reader_caches_and_reports_an_unreadable_calendar_as_empty():
    class Provider:
        calls = 0

        def market_calendar(self):
            self.calls += 1
            return {"tradingDay": "2026-10-08", "previousTradingDay": "2026-09-30",
                    "recentTradingDays": ["2026-09-30"], "tradingDays": NATIONAL_DAY}

    class Broken:
        def market_calendar(self):
            raise RuntimeError("sidecar_market_calendar_unavailable")

    provider = Provider()
    assert calendar_trading_days(provider) == NATIONAL_DAY
    assert calendar_trading_days(provider) == NATIONAL_DAY and provider.calls == 1
    assert calendar_trading_days(Broken()) == []
    assert calendar_trading_days(object()) == []   # a provider without a calendar


# -- one reliable reader (#754) ------------------------------------------------------------------

SUMMARY = {"tradingDay": "2026-10-08", "previousTradingDay": "2026-09-30",
           "recentTradingDays": ["2026-09-30"], "tradingDays": NATIONAL_DAY}


def sidecar(monkeypatch, read):
    """A sidecar provider whose calendar is `read()` (a sync HTTP read) and whose snapshot prefetch answers."""
    provider = SidecarReadOnlyDataProvider(base_url="http://127.0.0.1:1", token="calendar-test-token-" * 2,
                                           symbols=("SHFE.rb2701",))

    def request(method, path, payload=None, *, timeout=None):
        assert path == "/v1/public-market/calendar"
        return read()

    async def request_async(method, path, payload=None, *, timeout=None):
        return {"schemaVersion": 1, "ordersInitialized": True, "orders": [], "trades": []}
    monkeypatch.setattr(provider, "_request", request)
    monkeypatch.setattr(provider, "_request_async", request_async)
    return provider


async def test_a_calendar_read_inside_a_snapshot_is_the_real_calendar_and_outside_readers_keep_it(monkeypatch):
    """#790 残留：快照内读日历曾因未预取得空列表，并把空列表按 provider 缓存 10 秒，快照外的过期单 / 补录 /
    交割提醒也跟着拿到空日历。现在账户快照进门时旁路开读日历（不等它），读进自己的缓存，快照内外都是真日历，只读一次。"""
    reads = []
    provider = sidecar(monkeypatch, lambda: reads.append(1) or SUMMARY)
    async with provider_read_snapshot(provider):
        assert await exchange_calendar.trading_days(provider) == NATIONAL_DAY   # joins the read the snapshot started
        assert calendar_trading_days(provider) == NATIONAL_DAY
    assert calendar_trading_days(provider) == NATIONAL_DAY
    assert await exchange_calendar.trading_days(provider) == NATIONAL_DAY
    assert len(reads) == 1


async def test_on_the_event_loop_a_cold_calendar_read_never_waits_and_a_failure_never_empties_it(monkeypatch):
    release, reads = threading.Event(), []

    def read():
        reads.append(1)
        assert release.wait(5)
        if len(reads) > 1:
            raise RuntimeError("sidecar_market_calendar_unavailable")
        return SUMMARY
    provider = sidecar(monkeypatch, read)
    started = time.monotonic()
    assert calendar_trading_days(provider) == []      # nothing known yet: the background read is started
    assert time.monotonic() - started < 1             # …and the loop was not held for it
    release.set()
    assert await exchange_calendar.trading_days(provider) == NATIONAL_DAY   # joins the same read
    assert len(reads) == 1
    # Expired, and the next read fails: the last good calendar stands in, never an empty one.
    key = id(provider)
    exchange_calendar._cache[key] = replace(exchange_calendar._cache[key], fresh_until=0.0)
    assert await exchange_calendar.trading_days(provider) == NATIONAL_DAY
    assert len(reads) == 2
    assert await asyncio.to_thread(calendar_trading_days, provider) == NATIONAL_DAY   # within the retry wait


def test_a_stamped_direct_stop_never_reads_the_calendar():
    """#790 残留：直接保护每轮滚桶都急着读日历；行上有账户交易日时根本用不到。"""
    def unread():
        raise AssertionError("the calendar was read for a stamped row")
    stamped = {"position_bucket": "today", "symbol": "SHFE.rb2701", "account_trading_day": "2026-09-30",
               "created_at": at("2026-09-30 14:00").isoformat()}
    assert direct_protection_bucket(stamped, today="2026-10-08", trading_days=unread) == "historical"
    legacy = {**stamped, "account_trading_day": None}
    assert direct_protection_bucket(legacy, today="2026-10-08", trading_days=lambda: NATIONAL_DAY) == "historical"


# -- a calendar shorter than the day (#809) -----------------------------------------------------

def test_a_day_past_the_calendar_keeps_the_weekday_rule_for_its_previous_day_and_night_session():
    """日历刷新失败保留旧值、比目标日短：前一交易日曾被锚到日历最后一天、夜盘排到那天晚上。"""
    short = [day for day in NATIONAL_DAY if day <= "2026-10-09"]
    assert previous_trading_day("2026-10-13", short) == "2026-10-12"
    assert previous_trading_day("2026-10-12", short) == "2026-10-09"
    assert previous_trading_day("2026-10-08", short) == "2026-09-30"      # inside the span: the holiday is known
    assert previous_trading_day("2026-08-31", short) == "2026-08-28"      # before the span: weekday rule
    schedule = {"day": [["09:00:00", "15:00:00"]], "night": [["21:00:00", "23:00:00"]]}
    night = sessions_of_day("2026-10-13", schedule, short)[0]
    assert night.night and datetime.fromtimestamp(night.start_ns / 1e9, CST) == at("2026-10-12 21:00")
    # The debate's sessions (#809) read the same rule: a night session past the span, none after the holiday.
    assert [item.night for item in debate_sessions("2026-10-13", schedule, short)] == [True, False]
    assert [item.night for item in debate_sessions("2026-10-08", schedule, short)] == [False]


# -- backfilled fills ---------------------------------------------------------------------------

def link(order_id: str, created: str) -> dict:
    return {"clientOrderId": order_id, "submitPhase": "CONFIRMED", "createdAt": at(created).isoformat(),
            "request": {"clientOrderId": order_id, "symbol": "SHFE.rb2701", "direction": "BUY",
                        "offset": "OPEN", "limitPrice": 3100.0},
            "report": {"order": {"status": "FINISHED", "volume": 1, "volumeLeft": 0}}}


def test_backfilled_fill_days_follow_the_calendar_and_the_eighteen_hundred_turn():
    links = [link("before-holiday", "2026-09-30 14:00"), link("evening-before", "2026-09-30 19:00"),
             link("friday-night", "2026-10-09 21:30"), link("tuesday-evening", "2026-10-13 19:30")]
    days = {fill["clientOrderId"]: fill["tradingDay"] for fill in synthesize_fills(links, lambda _: 10.0, NATIONAL_DAY)}
    assert days == {"before-holiday": "2026-09-30", "evening-before": "2026-10-08",
                    "friday-night": "2026-10-12", "tuesday-evening": "2026-10-14"}
    # Calendar unreadable: the weekend rule stands in (the next pass with a calendar re-derives the row).
    fallback = {fill["clientOrderId"]: fill["tradingDay"] for fill in synthesize_fills(links, lambda _: 10.0, [])}
    assert fallback["evening-before"] == "2026-10-01" and fallback["friday-night"] == "2026-10-12"


# -- direct-stop overnight bucket ---------------------------------------------------------------

def test_direct_stop_today_bucket_rolls_on_the_first_trading_day_after_the_holiday():
    row = {"position_bucket": "today", "symbol": "SHFE.rb2701", "created_at": at("2026-09-30 14:00").isoformat()}
    assert direct_protection_bucket(row, today="2026-09-30", trading_days=NATIONAL_DAY) == "today"
    holiday_today = trading_day_at(at("2026-10-03 10:00"), NATIONAL_DAY)
    assert direct_protection_bucket(row, today=holiday_today, trading_days=NATIONAL_DAY) == "historical"
    night = {**row, "created_at": at("2026-10-09 22:00").isoformat()}   # Friday night belongs to Monday
    assert direct_protection_bucket(night, today="2026-10-12", trading_days=NATIONAL_DAY) == "today"


def test_released_providers_leave_the_calendar_cache():
    """#790 review：池账户回收再装配、回放结束都会换新的 provider；过期的旧条目随下一次写入清掉，
    不把已释放的 provider 一直挂在缓存里。"""
    class Released:
        def market_calendar(self):
            return {"tradingDay": "2026-10-12", "previousTradingDay": "2026-10-09", "recentTradingDays": []}

    old = Released()
    calendar_trading_days(old)
    key = id(old)
    exchange_calendar._cache[key] = replace(exchange_calendar._cache[key], fresh_until=0.0, kept_until=0.0)  # 已过期
    calendar_trading_days(Released())
    assert key not in exchange_calendar._cache and len(exchange_calendar._cache) == 1


# -- delivery reminders -------------------------------------------------------------------------

IF2610 = {"symbol": "CFFEX.IF2610", "exchangeId": "CFFEX", "instrumentId": "IF2610",
          "productId": "IF", "deliveryYear": 2026, "deliveryMonth": 10}
RB2802 = {"symbol": "SHFE.rb2802", "exchangeId": "SHFE", "instrumentId": "rb2802",
          "productId": "rb", "deliveryYear": 2028, "deliveryMonth": 2}


def test_delivery_days_left_skip_the_national_day_holiday():
    # IF2610 到期日 10-16（第三个周五）。节前 09-28：工作日估算 15 天不提醒；扣掉国庆 5 个工作日是 10 天。
    assert delivery_notice(IF2610, date(2026, 9, 28)) is None
    notice = delivery_notice(IF2610, date(2026, 9, 28), NATIONAL_DAY)
    assert (notice["tradingDaysLeft"], notice["level"], notice["estimated"]) == (10, "warning", False)
    after = delivery_notice(IF2610, date(2026, 10, 8), NATIONAL_DAY)   # 节后首日
    assert (after["tradingDaysLeft"], after["lastHoldDay"], after["estimated"]) == (7, "2026-10-16", False)


def test_delivery_deadline_moves_back_when_the_month_end_is_a_holiday():
    # 2028-02 交割：前一个月最后交易日。示意日历里 01-25～02-01 春节休市 → 01-24（周一）。
    notice = delivery_notice(RB2802, date(2028, 1, 17), SPRING_2028)
    assert notice["lastHoldDay"] == "2028-01-24" and notice["tradingDaysLeft"] == 6
    assert notice["level"] == "warning" and notice["estimated"] is False
    assert "1 月 24 日" in notice["text"]
    # 工作日估算看不到假期：死线算成 01-31、11 个工作日，干脆不提醒。
    assert delivery_notice(RB2802, date(2028, 1, 17)) is None
    # 假期里已过最后可持仓日：已进交割月口径。
    inside = delivery_notice(RB2802, date(2028, 1, 27), SPRING_2028)
    assert inside["tradingDaysLeft"] == 0 and inside["level"] == "danger"


def test_cash_settled_expiry_on_a_holiday_rolls_to_the_next_trading_day():
    holiday_friday = [day for day in NATIONAL_DAY if day != "2026-10-16"]
    notice = delivery_notice(IF2610, date(2026, 10, 12), holiday_friday)
    assert notice["lastHoldDay"] == "2026-10-19" and notice["estimated"] is False


def test_delivery_without_a_covering_calendar_is_a_flagged_weekday_estimate():
    # 日历读不到：按工作日、标 estimated。
    unknown = delivery_notice(IF2610, date(2026, 10, 8))
    assert unknown["tradingDaysLeft"] == 7 and unknown["estimated"] is True
    # 只到今天的日历（旧 sidecar 没送后面的交易日）：后段按工作日，仍标估算。
    partial = [day for day in NATIONAL_DAY if day <= "2026-10-08"]
    notice = delivery_notice(IF2610, date(2026, 10, 8), partial)
    assert notice["tradingDaysLeft"] == 7 and notice["estimated"] is True


def test_trading_today_uses_the_calendar():
    holiday_noon = at("2026-10-03 12:00").timestamp()
    assert trading_today(holiday_noon, NATIONAL_DAY) == date(2026, 10, 8)
    assert trading_today(holiday_noon) == date(2026, 10, 5)


def test_a_foreign_product_reads_the_cme_trading_day_and_hours():
    """外盘三期：外盘品种按 CME 分日（美东 18:00 换日，周末顺延到周一，全天休市日跳过），时段在北京时间里
    跨午夜（美国夏令时 06:00–次日 05:00，冬令时 07:00–次日 06:00）。"""
    from datetime import datetime, timedelta, timezone

    from trader_runtime.trading_calendar import cme_schedule, cme_trading_day, session_position
    cst = timezone(timedelta(hours=8))

    def at(*parts):
        moment = datetime(*parts, tzinfo=cst)
        return int(moment.timestamp() * 1e9), cme_schedule(moment)

    assert cme_schedule(datetime(2026, 10, 20, tzinfo=cst))["day"] == [["06:00:00", "29:00:00"]]
    assert cme_schedule(datetime(2026, 11, 10, tzinfo=cst))["day"] == [["07:00:00", "30:00:00"]]
    ns, schedule = at(2026, 10, 21, 2, 0)   # Tue 14:00 New York
    position = session_position(ns, schedule)
    assert position.trading_day == "2026-10-20" and position.in_session and round(position.minutes_to_day_end) == 180
    ns, schedule = at(2026, 10, 21, 5, 30)  # the daily break
    assert not session_position(ns, schedule).in_session
    ns, _ = at(2026, 10, 24, 7, 0)          # Friday evening New York: closed till Sunday 18:00, Monday's day
    assert cme_trading_day(ns) == "2026-10-26"
    ns, _ = at(2026, 12, 25, 8, 0)          # Thursday 19:00 New York, Christmas Eve: the next day is Monday
    assert cme_trading_day(ns) == "2026-12-28"
    # A series carries one schedule (the request's day), yet each trading day keeps its own US offset: a winter
    # bar judged with the summer schedule still closes at 06:00 Beijing (17:00 New York), not 05:00.
    ns, _ = at(2026, 11, 11, 5, 30)         # Tue 16:30 New York, after the clocks went back
    winter = session_position(ns, cme_schedule(datetime(2026, 10, 20, tzinfo=cst)))
    assert winter.trading_day == "2026-11-10" and winter.in_session and round(winter.minutes_to_day_end) == 30
    # “夜盘不开新仓” on a foreign strategy keeps meaning the Beijing night inside CME's one long session.
    from trader_runtime.trading_calendar import is_night
    assert is_night(*at(2026, 10, 20, 22, 0)) and not is_night(*at(2026, 10, 20, 14, 0))

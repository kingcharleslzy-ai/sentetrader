from __future__ import annotations

import argparse
import asyncio
import bisect
import faulthandler
import hashlib
import hmac
import json
import math
import os
import queue
import re
import signal
import socket
import sys
import threading
import time
import traceback
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from datetime import time as dt_time
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

# 30 分 (1800) 与周线 (604800) joined 2026-10-02: TqSdk takes arbitrary seconds at the
# minute level and multiples of a day above it (up to 28 days), so both fit directly.
ALLOWED_KLINE_DURATIONS = frozenset({60, 180, 300, 900, 1800, 3600, 86400, 604800})
# Supports the accepted MA200 + 20-bar event window and a forming bar.
KLINE_DATA_LENGTH = 256
# 分时图 (2026-10-02) needs one full trading day of 1-minute bars (a gold night+day
# day runs ~555 minutes); the timeshare view shares the regular 1-minute chart's
# (symbol, 60) serial, only longer — no extra subscription.
TIMESHARE_DATA_LENGTH = 600


def kline_data_length(duration_seconds: int) -> int:
    """The per-serial bar count: one trading day at 1 minute, otherwise 256."""
    return TIMESHARE_DATA_LENGTH if duration_seconds == 60 else KLINE_DATA_LENGTH
# 规则回测 (2026-09-25): one read-only history request, TqSdk's per-serial maximum. Served by the
# separate history actor (history.py), never by the live session: it does not touch MAX_MARKET_SERIES.
HISTORY_DATA_LENGTH = 10_000
# Hard caps for one TqApi session. TqSdk never unsubscribes a chart or quote, so
# dropping our own reference frees nothing on the wire; past the cap the session
# answers market_subscription_limit and stays alive. The service rebuilds the
# session (subscriptions back to zero) when the market is closed and idle.
MAX_MARKET_SERIES = 48
MAX_DYNAMIC_QUOTES = 96
# 报价总表: one get_quote per main contract, no kline serials — its own cap,
# independent of MAX_MARKET_SERIES. Overflow is truncated by catalog order and
# flagged (`truncated`) in the response.
MAX_BOARD_QUOTES = 160
# 只订报价的补充通道 (2026-10-03): 非主力合约盯价 etc. rides the board's quote-only
# storage through per-read leases — never a kline serial, so it never takes one
# of the 48 public series slots. Overflow answers that symbol with an errorCode.
MAX_EXTRA_QUOTES = 40
# Service watchdog: no new snapshot for this long, or this many command timeouts
# in a row, means the actor thread is wedged inside TqSdk and gets replaced.
SESSION_STALL_SECONDS = 180.0
SESSION_STALL_TIMEOUTS = 3
SESSION_IDLE_RESET_SECONDS = 60.0
# A rebuilt session that never produces its first snapshot is stalled too.
SESSION_FIRST_SNAPSHOT_SECONDS = 600.0
# A worker that dies (TqKq maintenance, transient login failure) is retried with backoff;
# only a credential / permission rejection stops the retries.
SESSION_RETRY_BACKOFF_SECONDS = (30.0, 60.0, 120.0, 240.0, 600.0)
# A second stall right after a rebuild waits before the next one (the first rebuild is immediate);
# a session that served for this long resets the streak.
SESSION_STALL_BACKOFF_SECONDS = SESSION_RETRY_BACKOFF_SECONDS[:3]
SESSION_STALL_STREAK_RESET_SECONDS = 60.0
# How often the supervisor checks its session worker (tests shorten it).
SESSION_SUPERVISOR_POLL_SECONDS = 1.0
# A worker that dies twice in a row with the same error and no snapshot in between is not coming back
# in this process (2026-09-29: "Event loop is closed" on every rebuilt session until the container was
# recreated). The container's sidecar then re-executes itself in place — same container, so the
# Runtime and pool that share its network namespace keep their network — at most once per interval.
REEXEC_AFTER_SAME_EXITS = 2
REEXEC_MIN_INTERVAL_SECONDS = 600.0
REEXEC_ENV = "AI_TRADER_SIDECAR_REEXEC_AT"
FATAL_SESSION_CODES = frozenset({"tqkq_authentication_failed", "tqkq_permission_denied", "tqkq_credentials_missing"})
# How long an order call waits for the actor. A submit still queued when the caller has given up
# (gateway_command_timeout → the Runtime records UNKNOWN and asks by clientOrderId) is not sent
# late at a price nobody is watching any more: it is answered "not dispatched" instead.
ORDER_COMMAND_WAIT_SECONDS = 4.0
# The full contract catalogue of a live session: after a failed load it is fetched again on the
# same session with this backoff (the last step repeats) instead of never until the next rebuild.
CATALOG_RETRY_BACKOFF_SECONDS = (15.0, 30.0, 60.0, 120.0, 300.0)
BACKTEST_DURATIONS = frozenset({60, 180, 300, 900, 3600, 86400})
BACKTEST_MAX_DAYS = {60: 7, 180: 14, 300: 31, 900: 93, 3600: 366, 86400: 366}
ACTUAL_FUTURES_SYMBOL = re.compile(
    r"^(?:CFFEX|CZCE|DCE|GFEX|INE|SHFE)\.[A-Za-z]{1,3}\d{3,4}$"
)
CONTINUOUS_FUTURES_SYMBOL = re.compile(
    r"^KQ\.m@(?:CFFEX|CZCE|DCE|GFEX|INE|SHFE)\.[A-Za-z]{1,3}$"
)
# Quote.trading_status raw values (tqsdk TradingStatus.trade_status) to the label
# the UI and the readiness copy use. Unknown values pass through untranslated.
TRADING_STATUS_LABELS = {
    "AUCTIONORDERING": "集合竞价",
    "AUCTIONTRADE": "集合竞价撮合",
    "CONTINOUS": "连续交易",
    "NOTRADING": "非交易时段",
}
MAX_FUTURE_QUOTE_SKEW_SECONDS = 1.0
CHINA_TIMEZONE = timezone(timedelta(hours=8))


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def trading_day_from_dates(current: datetime, sorted_days: Sequence[date],
                           tz: tzinfo = CHINA_TIMEZONE) -> str:
    """The trading day ``current`` belongs to, as a bisect lookup in an already
    parsed, sorted calendar. Per-bar callers (`_annotate_trading_days` renders
    every bar of every subscribed series on each market update) must go through
    this entry: re-running strptime over the whole calendar per bar is what
    pinned a full core in the pool session (2026-10-09)."""
    # The day flips at 18:00 local time: Beijing for the domestic exchanges, New York for the CME
    # Globex day the foreign paper engine keeps (its trading day runs 18:00–17:00 ET).
    local = current.astimezone(tz)
    current_date = local.date()
    index = bisect.bisect_right(sorted_days, current_date)
    if local.hour < 18 and index > 0 and sorted_days[index - 1] == current_date:
        return current_date.isoformat()
    if index < len(sorted_days):
        return sorted_days[index].isoformat()
    raise RuntimeError("trading_calendar_unavailable")


def trading_day_from_calendar(current: datetime, trading_dates: set[str],
                              tz: tzinfo = CHINA_TIMEZONE) -> str:
    return trading_day_from_dates(
        current, sorted(date.fromisoformat(value) for value in trading_dates), tz,
    )


def fixture_trading_day(current: datetime) -> str:
    local = current.astimezone(CHINA_TIMEZONE)
    candidate = local.date()
    if local.hour >= 18:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.isoformat()


# A trading day ends with its day session on that calendar date (15:00, 15:15 for the bond futures);
# its night session ran the evening before. From this moment on its date the day counts as ended: the
# replay accepts it (`replay.validate_request`) and the calendar summary names it
# `latestClosedTradingDay`, the one rule the Runtime and the client read (#859 / #754).
TRADING_DAY_CLOSED_AT = dt_time(15, 30)


def calendar_bucket(current: datetime) -> tuple[date, bool, bool]:
    """The calendar summary's cache key: the natural day plus its two boundaries,
    15:30 (the trading day has closed) and 18:00 (the current trading day flips to
    the next one)."""
    local = current.astimezone(CHINA_TIMEZONE)
    return local.date(), local.time() >= TRADING_DAY_CLOSED_AT, local.hour >= 18


def calendar_summary(current: datetime, trading_dates: set[str] | None,
                     recent_days: int = 10) -> dict[str, Any]:
    """The replay default and the 休市 refusal (2026-10-03): the trading day
    ``current`` belongs to, the latest trading day whose sessions have all
    closed before it, and those days newest first. ``latestClosedTradingDay``
    is the latest day that may be replayed: the current trading day itself once
    its day session closed (``TRADING_DAY_CLOSED_AT`` on its date, until the
    18:00 turn), else the previous one. With a calendar,
    ``tradingDays`` also lists every trading day of the read window, oldest
    first and coming days included (2026-10-12: the Runtime's order expiry,
    backfill and delivery reminders count trading days across holidays from
    it). Without a calendar (fixture mode) only the weekend rule is known and
    ``tradingDays`` is left out."""
    trading_day = (trading_day_from_calendar(current, trading_dates)
                   if trading_dates is not None else fixture_trading_day(current))
    if trading_dates is not None:
        ended = sorted(value for value in trading_dates if value < trading_day)
    else:
        ended = []
        candidate = datetime.strptime(trading_day, "%Y-%m-%d").date() - timedelta(days=1)
        while len(ended) < recent_days:
            if candidate.weekday() < 5:
                ended.append(candidate.isoformat())
            candidate -= timedelta(days=1)
        ended.reverse()
    if not ended:
        raise RuntimeError("trading_calendar_unavailable")
    local = current.astimezone(CHINA_TIMEZONE)
    closed_today = (local.date().isoformat() == trading_day
                    and local.time() >= TRADING_DAY_CLOSED_AT)
    summary: dict[str, Any] = {
        "tradingDay": trading_day,
        "previousTradingDay": ended[-1],
        "recentTradingDays": ended[-recent_days:][::-1],
        "latestClosedTradingDay": trading_day if closed_today else ended[-1],
    }
    if trading_dates is not None:
        summary["tradingDays"] = sorted(trading_dates)
    return summary


def quote_age_seconds(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return float("inf")
    try:
        quoted_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if quoted_at.tzinfo is None:
        quoted_at = quoted_at.replace(tzinfo=timezone(timedelta(hours=8)))
    age = (datetime.now(UTC) - quoted_at.astimezone(UTC)).total_seconds()
    return max(0.0, age) if age >= -MAX_FUTURE_QUOTE_SKEW_SECONDS else age


def market_session_status(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"AUCTIONORDERING", "CONTINOUS"}:
        return "trading"
    if raw == "NOTRADING":
        return "closed"
    return "unknown"


def trading_schedule_status(trading_time: Any, now: datetime | None = None,
                            trading_dates: Collection[str] | None = None) -> str:
    """Infer an exchange session from the public Quote.trading_time schedule.

    The schedule is a fallback when the account cannot use get_trading_status();
    an ordinary lunch break is never confused with a network disconnect. With the
    exchange calendar (``trading_dates``, ISO days of a complete span) a holiday is
    closed too: no day session on a day the calendar does not list, and no night
    session on an evening whose next trading day is not the next weekday (the
    exchanges hold none before a holiday — #516). Outside the calendar's span, or
    without one, the weekday rule stands in.
    """
    day_periods = getattr(trading_time, "day", None)
    night_periods = getattr(trading_time, "night", None)
    if not isinstance(day_periods, (list, tuple)):
        day_periods = []
    if not isinstance(night_periods, (list, tuple)):
        night_periods = []
    if not day_periods and not night_periods:
        return "unknown"

    china = timezone(timedelta(hours=8))
    current = now or datetime.now(china)
    current = current.replace(tzinfo=china) if current.tzinfo is None else current.astimezone(china)
    seconds = current.hour * 3600 + current.minute * 60 + current.second

    def in_periods(periods: Sequence[Any], value: int) -> tuple[bool, bool]:
        valid = False
        for period in periods:
            if not isinstance(period, (list, tuple)) or len(period) != 2:
                continue
            try:
                start = sum(
                    unit * scale
                    for unit, scale in zip(
                        (int(part) for part in str(period[0]).split(":")),
                        (3600, 60, 1), strict=True,
                    )
                )
                end = sum(
                    unit * scale
                    for unit, scale in zip(
                        (int(part) for part in str(period[1]).split(":")),
                        (3600, 60, 1), strict=True,
                    )
                )
            except (TypeError, ValueError):
                continue
            valid = True
            if start <= value < end:
                return True, True
        return False, valid

    known = frozenset(trading_dates or ())
    span = (min(known), max(known)) if known else None

    def trading(day: date) -> bool:
        text = day.isoformat()
        if span is not None and span[0] <= text <= span[1]:
            return text in known
        return day.weekday() < 5

    def night_session(evening: date) -> bool:
        if not trading(evening):
            return False
        weekday = evening + timedelta(days=1)
        while weekday.weekday() >= 5:
            weekday += timedelta(days=1)
        # Every weekday between this evening and its night session's trading day trades: no holiday.
        return trading(weekday)

    valid_schedule = False
    matched, valid = in_periods(day_periods, seconds)
    if matched and trading(current.date()):
        return "trading"
    valid_schedule = valid_schedule or valid

    night_seconds: int | None = None
    if seconds >= 18 * 3600 and night_session(current.date()):
        night_seconds = seconds
    elif seconds < 6 * 3600 and night_session(current.date() - timedelta(days=1)):
        night_seconds = seconds + 24 * 3600
    if night_seconds is not None:
        matched, valid = in_periods(night_periods, night_seconds)
        if matched:
            return "trading"
        valid_schedule = valid_schedule or valid
    elif night_periods:
        # Validate the schedule even while the current weekday/time cannot be
        # part of a night session.
        _, valid = in_periods(night_periods, seconds)
        valid_schedule = valid_schedule or valid

    return "closed" if valid_schedule else "unknown"


def market_data_status(
    connection_status: str, session_status: str, quote_datetime: Any,
    stale_after_seconds: float,
) -> tuple[str, float]:
    quote_age = quote_age_seconds(quote_datetime)
    if connection_status not in {"normal", "simulated"}:
        return connection_status, quote_age
    if connection_status == "simulated" or session_status == "closed":
        return connection_status, quote_age
    if not math.isfinite(quote_age) or quote_age < 0 or quote_age > stale_after_seconds:
        return "stale", quote_age
    return connection_status, quote_age


def process_is_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def start_parent_watchdog(parent_pid: int) -> None:
    def watch() -> None:
        while process_is_alive(parent_pid):
            time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=watch, name="desktop-parent-watchdog", daemon=True).start()


def finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def change_basis(quote: Any) -> float | None:
    """涨跌的基准 (#751 B / #806, 2026-10-13): 昨结算 — the domestic futures convention and the
    报价总表's own — so the header, the AI card and the board agree on one contract. 昨收 only
    when a quote carries no 昨结算 (it used to come first, and the two can differ in sign)."""
    previous = finite_number(getattr(quote, "pre_settlement", None))
    if not previous:
        previous = finite_number(getattr(quote, "pre_close", None))
    return previous or None


def last_outside_day_range(last: float | None, high: float | None, low: float | None) -> bool:
    """A last price above the day's 最高 or below its 最低 cannot be a real trade of that day (#806):
    the reader must not show it. Unknown bounds (盘前, a source without them) are no evidence; an
    inverted day range (最高 below 最低) is itself an impossible quote and is withheld too (#823)."""
    if last is None or high is None or low is None:
        return False
    if high < low:
        return True
    tolerance = 1e-6 * max(1.0, abs(last))
    return last > high + tolerance or last < low - tolerance


def number(value: Any, default: float = 0.0) -> float:
    result = finite_number(value)
    return default if result is None else result


def per_lot_amount(quote: Any, key: str) -> float | None:
    """每手保证金/手续费：合约服务写进 quote 实体的内部字段（不是 Quote 类
    属性，只能防御性读取）。非有限正数或缺失归一成 None，消费者按估算缺失处理。"""
    raw = quote.get(key) if hasattr(quote, "get") else None
    result = finite_number(raw)
    return result if result is not None and result > 0 else None


def symbol_parts(symbol: str) -> tuple[str, str]:
    exchange, separator, instrument = symbol.partition(".")
    return (exchange, instrument) if separator else ("", exchange)


def trading_day_string(value: Any) -> str:
    """Normalize a tqsdk settlement/ranking/cont-calendar date key to YYYY-MM-DD."""
    raw = str(value or "").strip()
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", raw):
        return raw[:10]
    return raw


# query_symbol_ranking value columns by ranking_type: (value, change, rank).
RANKING_COLUMNS = {
    "VOLUME": ("volume", "volume_change", "volume_ranking"),
    "LONG": ("long_oi", "long_change", "long_ranking"),
    "SHORT": ("short_oi", "short_change", "short_ranking"),
}


def _has_night_session(value: Any) -> bool:
    if value is None:
        return False
    if hasattr(value, "dropna") and hasattr(value, "tolist"):
        value = value.dropna().tolist()
    if isinstance(value, (list, tuple, set)):
        return any(str(item).strip() for item in value)
    return bool(str(value).strip())


def catalog_trading_schedule(day_value: Any, night_value: Any) -> dict[str, Any] | None:
    """目录条目的 tradingSchedule：query_symbol_info 的 trading_time_day/night 就是
    Quote.trading_time 的两个半场（成对 "HH:MM:SS"），照 detach_trading_schedule 的
    规整复制；拿不到或形状不可用给 None——收盘提醒读目录就够了，不为它占 K 线订阅。"""
    result: dict[str, Any] = {}
    for name, value in (("day", day_value), ("night", night_value)):
        if hasattr(value, "dropna") and hasattr(value, "tolist"):
            value = value.dropna().tolist()
        periods: list[list[str]] = []
        if isinstance(value, (list, tuple)):
            for period in value:
                # 成对列表是 TqSdk 的原样；"21:00:00-02:30:00" 连写也接受。
                pair = str(period).split("-") if isinstance(period, str) else list(period)
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    return None
                values = [str(item) for item in pair]
                if not all(re.fullmatch(r"\d{2}:[0-5]\d:[0-5]\d", item) for item in values):
                    return None
                periods.append(values)
        elif value is not None:
            return None
        result[name] = periods
    return result if (result["day"] or result["night"]) else None


def build_instrument_catalog(api: Any) -> list[dict[str, Any]]:
    """Build a detached catalog using only official TqSdk discovery APIs."""
    symbols = sorted({
        str(symbol) for symbol in api.query_quotes(ins_class="FUTURE", expired=False)
        if str(symbol)
    })
    if not symbols:
        raise RuntimeError("instrument_catalog_empty")
    main_symbols = {str(symbol) for symbol in api.query_cont_quotes()}
    records = api.query_symbol_info(symbols).to_dict(orient="records")
    return instrument_catalog_from_records(records, main_symbols)


def instrument_catalog_from_records(records: Sequence[dict[str, Any]], main_symbols: set[str]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for row in records:
        symbol = str(row.get("instrument_id") or "")
        exchange_id = str(row.get("exchange_id") or "")
        product_id = str(row.get("product_id") or "")
        # query_quotes(..., expired=False) is the authoritative filter.  Some
        # query_symbol_info payloads omit `expired` or expose it as NaN, whose
        # Python truth value is True and would incorrectly discard the symbol.
        if not symbol or not exchange_id or not product_id:
            continue
        catalog.append({
            "symbol": symbol,
            "exchangeId": exchange_id,
            "instrumentId": symbol_parts(symbol)[1],
            "instrumentName": str(row.get("instrument_name") or symbol),
            "productId": product_id,
            "priceTick": finite_number(row.get("price_tick")),
            "volumeMultiple": finite_number(row.get("volume_multiple")),
            "deliveryYear": int(number(row.get("delivery_year"))),
            "deliveryMonth": int(number(row.get("delivery_month"))),
            "expireRestDays": int(number(row.get("expire_rest_days"))),
            "hasNight": _has_night_session(row.get("trading_time_night")),
            "tradingSchedule": catalog_trading_schedule(
                row.get("trading_time_day"), row.get("trading_time_night")),
            "isMain": symbol in main_symbols,
            # 日内开仓限额（交易所限仓）；0 或缺失 = 无限制。
            "openLimit": int(number(row.get("open_limit"))),
        })
    if not catalog:
        raise RuntimeError("instrument_catalog_metadata_empty")
    return sorted(catalog, key=lambda item: (
        item["exchangeId"], item["productId"], item["deliveryYear"],
        item["deliveryMonth"], item["symbol"],
    ))


def detach_quote(symbol: str, quote: Any) -> dict[str, Any]:
    last = finite_number(getattr(quote, "last_price", None))
    quote_time = str(getattr(quote, "datetime", "") or "").strip()
    if last is None or not quote_time:
        raise RuntimeError("market_quote_not_ready")
    previous = change_basis(quote)
    change = ((last - previous) / previous * 100) if previous else 0.0
    return {
        "symbol": symbol,
        "name": str(getattr(quote, "instrument_name", symbol) or symbol),
        "quoteDatetime": quote_time,
        "lastPrice": last,
        "changePercent": change,
        "bidPrice1": finite_number(getattr(quote, "bid_price1", None)),
        "bidVolume1": finite_number(getattr(quote, "bid_volume1", None)),
        "askPrice1": finite_number(getattr(quote, "ask_price1", None)),
        "askVolume1": finite_number(getattr(quote, "ask_volume1", None)),
        # 五档盘口：模拟盘常只填一档，缺的档位是 NaN，finite_number 归一成 None。
        "bidPrice2": finite_number(getattr(quote, "bid_price2", None)),
        "bidVolume2": finite_number(getattr(quote, "bid_volume2", None)),
        "askPrice2": finite_number(getattr(quote, "ask_price2", None)),
        "askVolume2": finite_number(getattr(quote, "ask_volume2", None)),
        "bidPrice3": finite_number(getattr(quote, "bid_price3", None)),
        "bidVolume3": finite_number(getattr(quote, "bid_volume3", None)),
        "askPrice3": finite_number(getattr(quote, "ask_price3", None)),
        "askVolume3": finite_number(getattr(quote, "ask_volume3", None)),
        "bidPrice4": finite_number(getattr(quote, "bid_price4", None)),
        "bidVolume4": finite_number(getattr(quote, "bid_volume4", None)),
        "askPrice4": finite_number(getattr(quote, "ask_price4", None)),
        "askVolume4": finite_number(getattr(quote, "ask_volume4", None)),
        "bidPrice5": finite_number(getattr(quote, "bid_price5", None)),
        "bidVolume5": finite_number(getattr(quote, "bid_volume5", None)),
        "askPrice5": finite_number(getattr(quote, "ask_price5", None)),
        "askVolume5": finite_number(getattr(quote, "ask_volume5", None)),
        "open": finite_number(getattr(quote, "open", None)),
        "highest": finite_number(getattr(quote, "highest", None)),
        "lowest": finite_number(getattr(quote, "lowest", None)),
        # 昨结价 (2026-10-02): the 分时图's reference line and its change-% baseline.
        "preSettlement": finite_number(getattr(quote, "pre_settlement", None)),
        "volume": finite_number(getattr(quote, "volume", None)),
        "openInterest": finite_number(getattr(quote, "open_interest", None)),
        "priceTick": finite_number(getattr(quote, "price_tick", None)),
        "volumeMultiple": finite_number(getattr(quote, "volume_multiple", None)),
        "upperLimit": finite_number(getattr(quote, "upper_limit", None)),
        "lowerLimit": finite_number(getattr(quote, "lower_limit", None)),
        "openLimit": int(number(getattr(quote, "open_limit", 0))),
        "tradingSchedule": detach_trading_schedule(getattr(quote, "trading_time", None)),
        # 每手保证金与手续费（参考估算用，实际以成交后为准）。
        "marginPerLot": per_lot_amount(quote, "margin"),
        "commissionPerLot": per_lot_amount(quote, "commission"),
    }


def no_trade_today(bars: list[dict[str, Any]]) -> bool:
    """Every bar of the latest trading day carries zero volume: the contract is
    listed and broadcast, but nobody has traded it today."""
    if not bars:
        return False
    today = bars[-1].get("tradingDay")
    todays = [bar for bar in bars if bar.get("tradingDay") == today]
    return bool(todays) and all(not finite_number(bar.get("volume")) for bar in todays)


def pre_open_quote(symbol: str, quote: Any, last_bar: dict[str, Any], *, code: str = "market_quote_pre_open") -> dict[str, Any]:
    """Display-only quote for a contract whose live quote is not there yet: the
    contract's static fields plus the last kline bar as price and time. Marked
    stale with its own code so consumers can tell it from a lost feed
    (`market_quote_pre_open` before the open, `market_quote_no_trade` when the
    session is on but the contract has not traded today)."""
    return {
        "symbol": symbol,
        "name": str(getattr(quote, "instrument_name", symbol) or symbol),
        "quoteDatetime": datetime.fromtimestamp(int(last_bar["datetime"]) / 1e9, tz=UTC)
            .astimezone(CHINA_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S.%f"),
        "lastPrice": float(last_bar["close"]),
        "changePercent": 0.0,
        "bidPrice1": None, "bidVolume1": None, "askPrice1": None, "askVolume1": None,
        "bidPrice2": None, "bidVolume2": None, "askPrice2": None, "askVolume2": None,
        "bidPrice3": None, "bidVolume3": None, "askPrice3": None, "askVolume3": None,
        "bidPrice4": None, "bidVolume4": None, "askPrice4": None, "askVolume4": None,
        "bidPrice5": None, "bidVolume5": None, "askPrice5": None, "askVolume5": None,
        "open": None, "highest": None, "lowest": None,
        # 昨结价仍是交易所静态字段，开盘前也有；分时图的虚线不因无行情而消失。
        "preSettlement": finite_number(getattr(quote, "pre_settlement", None)),
        "volume": None, "openInterest": None,
        "priceTick": finite_number(getattr(quote, "price_tick", None)),
        "volumeMultiple": finite_number(getattr(quote, "volume_multiple", None)),
        "upperLimit": None, "lowerLimit": None,
        "openLimit": int(number(getattr(quote, "open_limit", 0))),
        "tradingSchedule": detach_trading_schedule(getattr(quote, "trading_time", None)),
        "marginPerLot": per_lot_amount(quote, "margin"),
        "commissionPerLot": per_lot_amount(quote, "commission"),
        "dataStatus": "stale",
        "dataErrorCode": code,
    }


_BOARD_MONTH_SUFFIX = re.compile(r"\d{3,4}$")


def board_product_name(instrument_name: str) -> str:
    """The 品种名 a board row groups by: the instrument name without its
    trailing delivery-month digits (螺纹钢2610 → 螺纹钢, 沪深300股指2609 →
    沪深300股指). A name that is nothing but digits is kept as is."""
    product = _BOARD_MONTH_SUFFIX.sub("", str(instrument_name or "").strip())
    return product or str(instrument_name or "").strip()


def detach_trading_schedule(trading_time: Any) -> dict[str, Any] | None:
    """Copy public contract hours for display only; never infer execution permission."""
    if trading_time is None:
        return None
    result: dict[str, Any] = {}
    for name in ("day", "night"):
        periods = getattr(trading_time, name, None)
        if not isinstance(periods, (list, tuple)):
            return None
        copied = []
        for period in periods:
            if not isinstance(period, (list, tuple)) or len(period) != 2:
                return None
            values = [str(value) for value in period]
            if not all(re.fullmatch(r"\d{2}:[0-5]\d:[0-5]\d", value) for value in values):
                return None
            copied.append(values)
        result[name] = copied
    return result if result["day"] or result["night"] else None


def repaired_prices(open_: float, high: float, low: float, close: float) -> tuple[float, float, float, float]:
    """快期外盘主连（KQD.m@…）的日线偶有最低价或最高价写成 -1（缺数占位，2026-10-21 迷你纳指 20 根里 3 根），
    当真价会算出上百个百分点的振幅、图上拖出长影线。一根 K 线的其余三个价都为正、只有它是负数时按缺数补：
    最低价取开收较低者、最高价取开收较高者。真的负价（2020 年 WTI 那样整根为负）原样保留。"""
    if low < 0 < min(open_, high, close):
        low = min(open_, close)
    if high < 0 < min(open_, low, close):
        high = max(open_, close)
    return open_, high, low, close


def detach_klines(serial: Any, limit: int = KLINE_DATA_LENGTH) -> list[dict[str, float | int]]:
    bars: list[dict[str, float | int]] = []
    for row in serial.itertuples(index=False):
        timestamp = int(number(getattr(row, "datetime", 0)))
        values = {
            "open": finite_number(getattr(row, "open", None)),
            "high": finite_number(getattr(row, "high", None)),
            "low": finite_number(getattr(row, "low", None)),
            "close": finite_number(getattr(row, "close", None)),
        }
        if timestamp <= 0 or any(value is None for value in values.values()):
            continue
        prices = repaired_prices(*(float(values[key]) for key in ("open", "high", "low", "close")))  # type: ignore[arg-type]
        bars.append({
            "datetime": timestamp,
            **dict(zip(("open", "high", "low", "close"), prices, strict=True)),
            "volume": number(getattr(row, "volume", 0)),
            "openInterest": number(getattr(row, "close_oi", 0)),
        })
    return bars[-limit:]


def fixture_history_bars(base: float, duration_seconds: int, length: int, tick: float) -> list[dict[str, Any]]:
    """Deterministic waves for the fixture's history: trends both ways so rule backtests trade."""
    end = 1_788_192_000_000_000_000 + 80 * duration_seconds * 1_000_000_000
    bars: list[dict[str, Any]] = []
    previous = base
    for index in range(length):
        timestamp = end - (length - index) * duration_seconds * 1_000_000_000
        center = base * (1 + 0.02 * math.sin(index / 37) + 0.008 * math.sin(index / 7.3))
        close = round(center / tick) * tick
        high = max(previous, close) + tick * (1 + index % 3)
        low = min(previous, close) - tick * (1 + index % 2)
        bars.append({
            "datetime": timestamp, "open": previous, "high": high, "low": low, "close": close,
            "volume": 100 + index % 50, "openInterest": 8_000 + index % 400,
            "tradingDay": fixture_trading_day(datetime.fromtimestamp(timestamp / 1_000_000_000, tz=UTC)),
        })
        previous = close
    return bars


def fixture_instrument_catalog() -> list[dict[str, Any]]:
    # 时段与 fake 行情快照同一份：商品夜盘 21:00–02:30（跨零点写法），股指只有日盘。
    day = [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]]
    night = [["21:00:00", "02:30:00"]]
    stock_day = [["09:30:00", "11:30:00"], ["13:00:00", "15:00:00"]]
    return [
        {"symbol": "CFFEX.IF2609", "exchangeId": "CFFEX", "instrumentId": "IF2609",
         "instrumentName": "沪深300股指2609", "productId": "IF", "priceTick": 0.2,
         "volumeMultiple": 300.0, "deliveryYear": 2026, "deliveryMonth": 9,
         "expireRestDays": 20, "hasNight": False, "isMain": True, "openLimit": 0,
         "tradingSchedule": {"day": stock_day, "night": []}},
        {"symbol": "DCE.i2609", "exchangeId": "DCE", "instrumentId": "i2609",
         "instrumentName": "铁矿石2609", "productId": "i", "priceTick": 0.5,
         "volumeMultiple": 100.0, "deliveryYear": 2026, "deliveryMonth": 9,
         "expireRestDays": 12, "hasNight": True, "isMain": True, "openLimit": 0,
         "tradingSchedule": {"day": day, "night": night}},
        {"symbol": "SHFE.au2612", "exchangeId": "SHFE", "instrumentId": "au2612",
         "instrumentName": "沪金2612", "productId": "au", "priceTick": 0.02,
         "volumeMultiple": 1000.0, "deliveryYear": 2026, "deliveryMonth": 12,
         "expireRestDays": 90, "hasNight": True, "isMain": True, "openLimit": 0,
         "tradingSchedule": {"day": day, "night": night}},
        {"symbol": "SHFE.rb2610", "exchangeId": "SHFE", "instrumentId": "rb2610",
         "instrumentName": "螺纹钢2610", "productId": "rb", "priceTick": 1.0,
         "volumeMultiple": 10.0, "deliveryYear": 2026, "deliveryMonth": 10,
         "expireRestDays": 35, "hasNight": True, "isMain": True, "openLimit": 0,
         "tradingSchedule": {"day": day, "night": night}},
    ]


def tqkq_error_code(error: Exception) -> str:
    message = str(error)
    if message.startswith("tqsdk_backtest_"):
        return message
    if "用户权限认证失败" in message or "authentication" in message.lower():
        return "tqkq_authentication_failed"
    if "账户不支持" in message or "需要购买" in message:
        return "tqkq_permission_denied"
    # 快期每日 19:00–19:30 例行维护（北京时间）：TqSdk 在这段时间里连不上时在原因后面加这句话（按时钟判断）。
    if "日常运维时间" in message:
        return "tqkq_daily_maintenance"
    if "timeout" in message.lower() or "timed out" in message.lower():
        return "tqkq_connection_timeout"
    return f"tqkq_sidecar_{type(error).__name__.lower()}"


@dataclass(frozen=True)
class Snapshot:
    account: dict[str, float]
    positions: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    markets: dict[str, dict[str, Any]]
    trading_day: str
    captured_at: str


class SnapshotSession(Protocol):
    def read(self, timeout_seconds: float) -> Snapshot | None: ...

    def instrument_catalog(self) -> list[dict[str, Any]]: ...
    def market_quote(self, symbol: str) -> dict[str, Any]: ...
    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]: ...
    def query_settlements(self, symbols: Sequence[str], days: int) -> dict[str, Any]: ...
    def query_cont_history(self, symbols: Sequence[str], days: int) -> dict[str, Any]: ...
    def query_ranking(self, symbol: str, ranking_type: str, days: int) -> dict[str, Any]: ...

    def submit_order_once(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def query_order(self, client_order_id: str) -> dict[str, Any]: ...
    def cancel_order_once(self, client_order_id: str) -> dict[str, Any]: ...

    def close(self) -> None: ...


class OfficialTqKqSession:
    """The only class in the sidecar that owns a TqApi object."""

    def __init__(
        self, username: str, password: str, symbols: Sequence[str],
        on_stage: Callable[[str], None] | None = None, *, market_only: bool = False,
    ) -> None:
        self._on_stage = on_stage or (lambda _: None)
        self._on_stage("loading_sdk")
        from tqsdk import TqApi, TqAuth, TqKq, TqNotify, TqSim  # type: ignore[import-not-found]

        from .sdk_trim import trim_expired_contract_table

        self._on_stage("authenticating")
        # 行情专用 (2026-10-13): a market-source login opens the data-only TqApi the TqSdk skill
        # gives for data access — the quote/contract (md) and trading-status connections, no TqKq
        # trading session (no td connection, no remote account/orders). Its TqSim account is local
        # and never served (SnapshotService.market_only refuses the account routes).
        self.market_only = market_only
        if market_only:
            self.api = TqApi(TqSim(), auth=TqAuth(username, password), debug=False, disable_print=True)
        else:
            # The desktop release preflight checks for this exact TqKq construction.
            self.api = TqApi(TqKq(), auth=TqAuth(username, password),
                             debug=False, disable_print=True)
        trim_expired_contract_table(self.api)
        self._notifier = TqNotify(self.api)
        self._disconnected_connections: set[str] = set()
        self._on_stage("subscribing")
        self.account = self.api.get_account()
        self.position_entities = self.api.get_position()
        self.orders = self.api.get_order()
        self.trades = self.api.get_trade()
        self.quotes = {symbol: self.api.get_quote(symbol) for symbol in symbols}
        self._trading_statuses: dict[str, Any] = {}
        self._trading_status_attempted: set[str] = set()
        for symbol in symbols:
            self._ensure_trading_status(symbol)
        # Only the configured quotes are required for the first screen. Full
        # discovery runs cooperatively on the same TqApi owner after a snapshot.
        self.catalog_pending = True
        self._catalog_task = None
        self._instrument_catalog: list[dict[str, Any]] | None = None
        self._instrument_catalog_error: str | None = None
        self._catalog_symbols = {
            symbol.upper(): symbol for symbol in self.quotes
        }
        self._dynamic_quotes: dict[str, Any] = {}
        self._kline_serials: dict[tuple[str, int], Any] = {}
        self._initial_snapshot_pending = True
        self.orders_initialized, self._observed_order_ids = False, set()
        self._on_stage("waiting_snapshot")

    def _trading_dates(self, start: datetime, end: datetime) -> set[str]:
        calendar = self.api.get_trading_calendar(start.date(), end.date())
        return {
            value.date().isoformat()
            for value, trading in zip(calendar["date"], calendar["trading"], strict=True)
            if bool(trading)
        }

    def _trading_day(self, current: datetime) -> str:
        local = current.astimezone(CHINA_TIMEZONE)
        return trading_day_from_calendar(
            local,
            self._trading_dates(local - timedelta(days=7), local + timedelta(days=21)),
        )

    def _annotate_trading_days(
        self, bars: list[dict[str, float | int]],
    ) -> list[dict[str, Any]]:
        if not bars:
            return []
        local_times = [
            datetime.fromtimestamp(int(bar["datetime"]) / 1_000_000_000, tz=UTC)
            .astimezone(CHINA_TIMEZONE)
            for bar in bars
        ]
        trading_dates = self._trading_dates(
            min(local_times) - timedelta(days=7), max(local_times) + timedelta(days=21)
        )
        # Parse the calendar once per render, then bisect per bar — see
        # trading_day_from_dates for why the per-bar path must stay parse-free.
        sorted_days = sorted(date.fromisoformat(value) for value in trading_dates)
        return [
            {**bar, "tradingDay": trading_day_from_dates(local, sorted_days)}
            for bar, local in zip(bars, local_times, strict=True)
        ]

    @staticmethod
    def _critical_connection(connection_id: str) -> bool:
        return connection_id == "md" or re.fullmatch(r"td_\d+", connection_id) is not None

    def _connections_healthy(self) -> bool:
        notifier = getattr(self, "_notifier", None)
        disconnected = getattr(self, "_disconnected_connections", set())
        if notifier is None:
            return not disconnected
        for notification in notifier.get_notifies():
            connection_id = str(notification.get("conn_id") or "")
            if not self._critical_connection(connection_id):
                continue
            code = int(number(notification.get("code"), -1))
            if code in {2019112910, 2019112911}:
                disconnected.add(connection_id)
            elif code in {2019112901, 2019112902}:
                disconnected.discard(connection_id)
        self._disconnected_connections = disconnected
        return not disconnected

    def _market_session_status(self, symbol: str) -> str:
        status = getattr(self, "_trading_statuses", {}).get(symbol)
        official = market_session_status(getattr(status, "trade_status", None))
        if official != "unknown":
            return official
        quote = getattr(self, "quotes", {}).get(symbol)
        if quote is None:
            quote = getattr(self, "_dynamic_quotes", {}).get(symbol)
        return trading_schedule_status(getattr(quote, "trading_time", None),
                                       trading_dates=self._session_trading_dates())

    def _session_trading_dates(self) -> frozenset[str] | None:
        """The exchange calendar around today for the schedule fallback's holidays (#516), read once per
        natural day — the same window as the public calendar summary, so the market worker's calendar
        cache serves both. None (the weekday rule) while the calendar cannot be read."""
        now = datetime.now(CHINA_TIMEZONE)
        cached = getattr(self, "_session_calendar", None)
        if cached is not None and cached[0] == now.date():
            return cached[1]
        try:
            dates = frozenset(self._trading_dates(now - timedelta(days=60), now + timedelta(days=45)))
        except Exception:  # noqa: BLE001 - no calendar: the weekday rule stands in, read again next call
            return None
        if not dates:
            return None
        self._session_calendar = (now.date(), dates)
        return dates

    def _trading_status_fields(self, symbol: str) -> dict[str, str]:
        """The raw exchange trading status and its Chinese label, when the
        account has the trading-status permission for this contract."""
        status = getattr(self, "_trading_statuses", {}).get(symbol)
        raw = str(getattr(status, "trade_status", "") or "").strip().upper()
        if not raw or raw == "NAN":
            return {}
        return {
            "tradingStatus": raw,
            "tradingStatusLabel": TRADING_STATUS_LABELS.get(raw, raw),
        }

    def _ensure_trading_status(self, symbol: str) -> None:
        attempted = getattr(self, "_trading_status_attempted", set())
        if symbol in attempted:
            return
        attempted.add(symbol)
        self._trading_status_attempted = attempted
        try:
            statuses = getattr(self, "_trading_statuses", {})
            statuses[symbol] = self.api.get_trading_status(symbol)
            self._trading_statuses = statuses
        except Exception:
            # Trading-status permission is optional. Without it, quote age
            # remains fail-closed and we never infer an exchange session.
            return

    def instrument_catalog(self) -> list[dict[str, Any]]:
        if self._instrument_catalog is None:
            # These are current authenticated quote fields, not a cached account
            # or guessed contract specification. Discovery later replaces them.
            rows = [{**dict(quote), "instrument_id": symbol}
                    for symbol, quote in self.quotes.items()]
            return instrument_catalog_from_records(rows, set())
        return [dict(item) for item in self._instrument_catalog]

    async def _load_instrument_catalog(self) -> None:
        try:
            symbols = sorted({str(symbol) for symbol in await asyncio.wait_for(
                self.api.query_quotes(ins_class="FUTURE", expired=False), timeout=30,
            ) if str(symbol)})
            if not symbols:
                raise RuntimeError("instrument_catalog_empty")
            main_symbols = set(await asyncio.wait_for(self.api.query_cont_quotes(), timeout=30))
            frame = await asyncio.wait_for(self.api.query_symbol_info(symbols), timeout=30)
            catalog = instrument_catalog_from_records(frame.to_dict(orient="records"), main_symbols)
            self._instrument_catalog = catalog
            self._catalog_symbols.update({item["symbol"].upper(): item["symbol"] for item in catalog})
            self._instrument_catalog_error = None
            self.catalog_pending = False
        except Exception:
            # Only the configured contracts are known until a retry lands; the session stays
            # pending (the service keeps polling it) and read() schedules the next attempt.
            failures = getattr(self, "_catalog_failures", 0)
            self._catalog_failures = failures + 1
            self._catalog_retry_at = time.monotonic() + CATALOG_RETRY_BACKOFF_SECONDS[
                min(failures, len(CATALOG_RETRY_BACKOFF_SECONDS) - 1)]
            self._instrument_catalog_error = "tqkq_instrument_catalog_unavailable"
        finally:
            self._catalog_task_done = True

    def _catalog_due(self) -> bool:
        """Whether read() should (re)start the full catalogue load: never started, or the last
        attempt failed and its backoff has passed. A loaded catalogue is never fetched again."""
        if self._catalog_task is None:
            return True
        if getattr(self, "_instrument_catalog", None) is not None or not getattr(self, "_catalog_task_done", False):
            return False
        return time.monotonic() >= getattr(self, "_catalog_retry_at", float("inf"))

    def _resolve_symbol(self, symbol: str) -> str:
        """The catalogue spelling of a requested contract. Before the full catalogue has loaded
        only the configured contracts are known: anything else is "catalogue not ready", not a
        contract that does not exist."""
        actual_symbol = self._catalog_symbols.get(str(symbol).upper())
        if actual_symbol is not None:
            return actual_symbol
        # Only a live session tracks its full load (the shared public-market worker reuses the
        # renderer with its own catalogue and keeps answering not-found there).
        if hasattr(self, "_instrument_catalog") and self._instrument_catalog is None:
            raise RuntimeError("instrument_catalog_not_ready")
        raise KeyError(symbol)

    def _quote_for_symbol(self, symbol: str) -> Any:
        quote = self.quotes.get(symbol)
        if quote is not None:
            return quote
        dynamic_quotes = getattr(self, "_dynamic_quotes", None)
        if dynamic_quotes is None:
            dynamic_quotes = self._dynamic_quotes = {}
        quote = dynamic_quotes.get(symbol)
        if quote is None:
            if len(dynamic_quotes) >= MAX_DYNAMIC_QUOTES:
                raise RuntimeError("market_quote_subscription_limit")
            quote = self.api.get_quote(symbol)
            dynamic_quotes[symbol] = quote
        self._ensure_trading_status(symbol)
        return quote

    def subscription_counts(self) -> dict[str, int]:
        return {"marketSeriesCount": len(getattr(self, "_kline_serials", {})),
                "dynamicQuoteCount": len(getattr(self, "_dynamic_quotes", {}))}

    def subscriptions_exhausted(self) -> bool:
        return len(getattr(self, "_kline_serials", {})) >= MAX_MARKET_SERIES

    def market_quote(self, symbol: str) -> dict[str, Any]:
        actual_symbol = self._resolve_symbol(symbol)
        quote = self._quote_for_symbol(actual_symbol)
        try:
            result = detach_quote(actual_symbol, quote)
        except RuntimeError:
            self.api.wait_update(deadline=time.time() + 0.5)
            result = detach_quote(actual_symbol, quote)
        return {
            **result,
            "marketSessionStatus": self._market_session_status(actual_symbol),
            **self._trading_status_fields(actual_symbol),
        }

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        actual_symbol = self._resolve_symbol(symbol)
        key = (actual_symbol, duration_seconds)
        if key not in self._kline_serials:
            if len(self._kline_serials) >= MAX_MARKET_SERIES:
                raise RuntimeError("market_subscription_limit")
            self._kline_serials[key] = self.api.get_kline_serial(
                actual_symbol,
                duration_seconds,
                data_length=kline_data_length(duration_seconds),
            )
        quote = self._quote_for_symbol(actual_symbol)
        serial = self._kline_serials[key]
        if not self.api.is_serial_ready(serial):
            self.api.wait_update(deadline=time.time() + 0.5)
        if not self.api.is_serial_ready(serial):
            raise RuntimeError("market_series_not_ready")
        bars = self._annotate_trading_days(
            detach_klines(serial, limit=kline_data_length(duration_seconds)))
        if not bars:
            raise RuntimeError("market_series_empty")
        return {
            "schemaVersion": 1,
            "durationSeconds": duration_seconds,
            "quote": self._workbench_quote(actual_symbol, quote, bars),
            "bars": bars,
        }

    def _workbench_quote(self, actual_symbol: str, quote: Any, bars: list[dict[str, Any]]) -> dict[str, Any]:
        """The display quote of a workbench: shared by this session's workbench and the public
        market worker's incremental renderer (public_market.py), so both answer the same fields."""
        # Only the display workbench may retain the last complete quote.
        # market_quote(), used by order checks, always requires current fields.
        retained = getattr(self, "_display_quotes", {})
        try:
            display_quote = detach_quote(actual_symbol, quote)
            retained[actual_symbol] = dict(display_quote)
            self._display_quotes = retained
        except RuntimeError:
            if actual_symbol in retained:
                display_quote = {**retained[actual_symbol], "dataStatus": "stale",
                                 "dataErrorCode": "market_quote_not_ready"}
            else:
                # No live quote yet (a contract without a night session between the
                # Friday day-switch and Monday's open): the chart is still real. The
                # display quote takes the last bar's close and time and says so;
                # market_quote() for order checks still refuses to invent a price.
                session_on = self._market_session_status(actual_symbol) == "trading"
                display_quote = pre_open_quote(
                    actual_symbol, quote, bars[-1],
                    code="market_quote_no_trade" if session_on and no_trade_today(bars) else "market_quote_pre_open")
        return {
            **display_quote,
            "marketSessionStatus": self._market_session_status(actual_symbol),
            **self._trading_status_fields(actual_symbol),
        }

    def query_settlements(self, symbols: Sequence[str], days: int) -> dict[str, Any]:
        """Daily settlement prices via api.query_symbol_settlement (blocks on the
        actor thread until the download finishes, tqsdk's own 30s deadline)."""
        resolved: list[str] = []
        for symbol in symbols:
            actual_symbol = self._resolve_symbol(symbol)
            if actual_symbol not in resolved:
                resolved.append(actual_symbol)
        frame = self.api.query_symbol_settlement(resolved, days)
        items: list[dict[str, Any]] = []
        latest = ""
        for row in frame.to_dict(orient="records"):
            trading_day = trading_day_string(row.get("datetime"))
            settlement = finite_number(row.get("settlement"))
            if settlement is None:
                continue
            latest = max(latest, trading_day)
            items.append({
                "symbol": str(row.get("symbol") or ""),
                "tradingDay": trading_day,
                "settlement": settlement,
            })
        items.sort(key=lambda item: (item["symbol"], item["tradingDay"]))
        # asOf: the newest trading day a settlement was already published for
        # (a day's settlement appears only after ~19:00 that evening).
        return {"schemaVersion": 1, "days": days, "asOf": latest or None, "items": items}

    def query_cont_history(self, symbols: Sequence[str], days: int) -> dict[str, Any]:
        """Which actual contract the continuous symbol tracked each day
        (api.query_his_cont_quotes, a synchronous HTTP calendar download)."""
        requested: list[str] = []
        for symbol in symbols:
            value = str(symbol)
            if not CONTINUOUS_FUTURES_SYMBOL.fullmatch(value):
                raise KeyError(symbol)
            if value not in requested:
                requested.append(value)
        frame = self.api.query_his_cont_quotes(requested, days)
        result: dict[str, Any] = {
            "schemaVersion": 1, "days": days, "symbols": {}, "rollDates": {},
        }
        for row in frame.to_dict(orient="records"):
            date = trading_day_string(row.get("date"))
            for cont in requested:
                main_symbol = str(row.get(cont) or "").strip()
                if not main_symbol:
                    continue
                result["symbols"].setdefault(cont, []).append(
                    {"date": date, "mainSymbol": main_symbol}
                )
        for cont, rows in result["symbols"].items():
            result["rollDates"][cont] = [
                rows[index]["date"]
                for index in range(1, len(rows))
                if rows[index]["mainSymbol"] != rows[index - 1]["mainSymbol"]
            ]
        return result

    def query_ranking(self, symbol: str, ranking_type: str, days: int) -> dict[str, Any]:
        """Per-broker volume/open-interest ranking via api.query_symbol_ranking
        (blocks on the actor thread like query_settlements)."""
        actual_symbol = self._resolve_symbol(symbol)
        value_column, change_column, rank_column = RANKING_COLUMNS[ranking_type]
        frame = self.api.query_symbol_ranking(actual_symbol, ranking_type, days)
        rows: list[dict[str, Any]] = []
        latest = ""
        for row in frame.to_dict(orient="records"):
            trading_day = trading_day_string(row.get("datetime"))
            value = finite_number(row.get(value_column))
            if value is None:
                continue
            latest = max(latest, trading_day)
            rows.append({
                "tradingDay": trading_day,
                "broker": str(row.get("broker") or ""),
                "value": value,
                "change": finite_number(row.get(change_column)),
                "rank": int(number(row.get(rank_column))),
            })
        rows.sort(key=lambda item: (item["tradingDay"], item["rank"]))
        return {
            "schemaVersion": 1,
            "symbol": actual_symbol,
            "rankingType": ranking_type,
            "days": days,
            "asOf": latest or None,
            "rows": rows,
        }

    # Set by the actor: True while a command (an order, a quote read) is queued for it.
    commands_waiting: Callable[[], bool] | None = None
    COMMAND_CHECK_SECONDS = 0.05

    def read(self, timeout_seconds: float) -> Snapshot | None:
        # A false return only means no business field changed before the
        # deadline. It is not a connection failure (common during a market
        # break or when account values stay unchanged).
        # The wait runs in short slices so a queued command is not held behind a quiet
        # market for the whole timeout: 2026-09-27 production, market closed, a quote read
        # through this actor took p50 143 ms / max 255 ms while the HTTP hop took 1.4 ms.
        deadline = time.time() + timeout_seconds
        while True:
            if self.api.wait_update(deadline=min(deadline, time.time() + self.COMMAND_CHECK_SECONDS)):
                break
            if time.time() >= deadline or (self.commands_waiting is not None and self.commands_waiting()):
                break
        snapshot = self.snapshot()
        if snapshot is not None and self._catalog_due():
            self._catalog_task_done = False
            self.catalog_pending = True
            self._catalog_task = self.api.create_task(self._load_instrument_catalog())
        return snapshot

    def snapshot_time(self) -> datetime:
        return datetime.now(UTC)

    def snapshot(self) -> Snapshot | None:
        if not self._connections_healthy():
            self._on_stage("reconnecting")
            return None
        required_account = {
            "balance": finite_number(getattr(self.account, "balance", None)),
            "staticBalance": finite_number(getattr(self.account, "static_balance", None)),
            "available": finite_number(getattr(self.account, "available", None)),
            "floatProfit": finite_number(getattr(self.account, "float_profit", None)),
            "positionProfit": finite_number(getattr(self.account, "position_profit", None)),
            "closeProfit": finite_number(getattr(self.account, "close_profit", None)),
            "commission": finite_number(getattr(self.account, "commission", None)),
            "margin": finite_number(getattr(self.account, "margin", None)),
        }
        if any(value is None for value in required_account.values()):
            self._on_stage("waiting_account")
            return None
        account = {key: float(value) for key, value in required_account.items()}

        markets: dict[str, dict[str, Any]] = {}
        for symbol, quote in self.quotes.items():
            last = finite_number(getattr(quote, "last_price", None))
            bid = finite_number(getattr(quote, "bid_price1", None))
            ask = finite_number(getattr(quote, "ask_price1", None))
            tick = finite_number(getattr(quote, "price_tick", None))
            multiplier = finite_number(getattr(quote, "volume_multiple", None))
            upper = finite_number(getattr(quote, "upper_limit", None))
            lower = finite_number(getattr(quote, "lower_limit", None))
            quote_time = str(getattr(quote, "datetime", "") or "").strip()
            if (
                any(
                    value is None
                    for value in ((last, tick, multiplier) if getattr(self, "historical", False)
                                  else (last, tick, multiplier, upper, lower))
                )
                or not quote_time
            ):
                if getattr(self, "historical", False):
                    # Historical time itself comes from this quote.
                    return None
                # A missing quote must not freeze fresh account/order reports.
                # Never synthesize a price or retain an executable old book.
                markets[symbol] = {
                    "symbol": symbol, "name": str(getattr(quote, "instrument_name", symbol)),
                    "price": None, "quoteDatetime": quote_time,
                    "marketSessionStatus": self._market_session_status(symbol),
                    **self._trading_status_fields(symbol),
                    "dataStatus": "stale", "dataErrorCode": "market_quote_not_ready",
                    "missingFields": [name for name, value in {
                        "last_price": last, "price_tick": tick, "volume_multiple": multiplier,
                        "upper_limit": upper, "lower_limit": lower,
                        "datetime": quote_time or None,
                    }.items() if value is None],
                }
                continue
            previous = change_basis(quote)
            change = ((last - previous) / previous * 100) if previous else 0.0
            markets[symbol] = {
                "symbol": symbol,
                "name": str(getattr(quote, "instrument_name", symbol)),
                "price": last,
                "bidPrice1": bid,
                "askPrice1": ask,
                "priceTick": tick,
                "volumeMultiple": multiplier,
                "upperLimit": upper,
                "lowerLimit": lower,
                "changePercent": change,
                "quoteDatetime": quote_time,
                "marginPerLot": per_lot_amount(quote, "margin"),
                "commissionPerLot": per_lot_amount(quote, "commission"),
                "marketSessionStatus": self._market_session_status(symbol),
                **self._trading_status_fields(symbol),
            }
            retained = getattr(self, "_display_quotes", {})
            retained[symbol] = detach_quote(symbol, quote)
            self._display_quotes = retained

        positions: list[dict[str, Any]] = []
        entries = self.position_entities.items() if hasattr(self.position_entities, "items") else ()
        for full_symbol, position in entries:
            positions.extend(self._activity_position(str(full_symbol), position))
        if not self.orders_initialized:
            self._observed_order_ids.update(str(key) for key in self.orders)
            self.orders_initialized = True
        orders = [
            self._activity_order(order)
            for order in getattr(self, "orders", {}).values()
            if str(getattr(order, "order_id", "") or "")
        ]
        orders.sort(key=lambda item: int(item["insertDateTime"]), reverse=True)
        trades = [
            self._activity_trade(trade)
            for trade in getattr(self, "trades", {}).values()
            if str(getattr(trade, "trade_id", "") or "")
        ]
        # Trade timestamps, not the current account snapshot, determine the
        # trading day. This also covers Friday night and holiday sessions.
        fill_days = {}
        for trade in trades:
            timestamp = trade["tradeDateTime"]
            try:
                if timestamp <= 0:
                    raise ValueError("missing_trade_timestamp")
                traded_at = datetime.fromtimestamp(timestamp / 1e9, tz=UTC).astimezone(CHINA_TIMEZONE)
                period = (traded_at.date(), traded_at.hour >= 18)
                if period not in fill_days:
                    fill_days[period] = self._trading_day(traded_at)
                trade["tradingDay"] = fill_days[period]
            except (ValueError, OverflowError, OSError, RuntimeError):
                # Preserve the fill for diagnosis; consumers must fail closed
                # for this contract rather than substituting today's date.
                trade["tradingDay"] = None
        trades.sort(key=lambda item: int(item["tradeDateTime"]), reverse=True)
        self._initial_snapshot_pending = False
        self._on_stage("waiting_market" if any(m.get("dataErrorCode") for m in markets.values()) else "ready")
        captured = self.snapshot_time()
        return Snapshot(
            account, positions, orders, trades, markets,
            self._trading_day(captured), captured.isoformat()
        )

    @staticmethod
    def _activity_position(full_symbol: str, position: Any) -> list[dict[str, Any]]:
        """One row per non-zero direction of a TqSdk position entity; shared with the
        paper engine (paper_book.py), which feeds attribute views over SimTrade dicts."""
        symbol = str(full_symbol)
        exchange_id, instrument_id = symbol_parts(symbol)
        rows: list[dict[str, Any]] = []
        for direction, volume_name, profit_name in (
            ("long", "pos_long", "float_profit_long"),
            ("short", "pos_short", "float_profit_short"),
        ):
            volume = int(number(getattr(position, volume_name, 0)))
            if volume:
                rows.append({
                    "symbol": symbol,
                    "exchangeId": exchange_id,
                    "instrumentId": instrument_id,
                    "direction": direction,
                    "volume": volume,
                    "todayVolume": int(number(getattr(position, f"pos_{direction}_today", 0))),
                    "historicalVolume": int(number(getattr(position, f"pos_{direction}_his", 0))),
                    "frozenToday": int(number(getattr(position, f"volume_{direction}_frozen_today", 0))),
                    "frozenHistorical": int(number(getattr(position, f"volume_{direction}_frozen_his", 0))),
                    "openPrice": finite_number(getattr(position, f"open_price_{direction}", None)),
                    # 持仓自己的最新价（#803）：浮盈按它算，非当前图表合约的行也能显示现价。
                    "lastPrice": finite_number(getattr(position, "last_price", None)),
                    "margin": finite_number(getattr(position, f"margin_{direction}", None)),
                    "unrealizedPnl": number(getattr(position, profit_name, 0)),
                })
        return rows

    @staticmethod
    def _activity_order(order: Any) -> dict[str, Any]:
        exchange_id = str(getattr(order, "exchange_id", "") or "")
        instrument_id = str(getattr(order, "instrument_id", "") or "")
        original = int(number(getattr(order, "volume_orign", 0)))
        left = int(number(getattr(order, "volume_left", 0)))
        return {
            "clientOrderId": str(getattr(order, "order_id", "") or ""),
            "exchangeOrderId": str(getattr(order, "exchange_order_id", "") or ""),
            "symbol": f"{exchange_id}.{instrument_id}" if exchange_id and instrument_id else "",
            "direction": str(getattr(order, "direction", "") or ""),
            "offset": str(getattr(order, "offset", "") or ""),
            "volume": original,
            "volumeLeft": left,
            "filledVolume": max(0, original - left),
            "limitPrice": finite_number(getattr(order, "limit_price", None)),
            "priceType": str(getattr(order, "price_type", "") or ""),
            "status": str(getattr(order, "status", "") or "UNKNOWN"),
            "insertDateTime": int(number(getattr(order, "insert_date_time", 0))),
            "lastMsg": str(getattr(order, "last_msg", "") or ""),
        }

    @staticmethod
    def _activity_trade(trade: Any) -> dict[str, Any]:
        exchange_id = str(getattr(trade, "exchange_id", "") or "")
        instrument_id = str(getattr(trade, "instrument_id", "") or "")
        return {
            "tradeId": str(getattr(trade, "trade_id", "") or ""),
            "clientOrderId": str(getattr(trade, "order_id", "") or ""),
            "exchangeTradeId": str(getattr(trade, "exchange_trade_id", "") or ""),
            "symbol": f"{exchange_id}.{instrument_id}" if exchange_id and instrument_id else "",
            "direction": str(getattr(trade, "direction", "") or ""),
            "offset": str(getattr(trade, "offset", "") or ""),
            "price": finite_number(getattr(trade, "price", None)),
            "volume": int(number(getattr(trade, "volume", 0))),
            "tradeDateTime": int(number(getattr(trade, "trade_date_time", 0))),
            # 本站模拟盘的逐笔佣金 (2026-10-09)：账本已按费率算在每笔成交上，序列化带出去，
            # 策略净值归属用实际值、不再只剩费率估算。
            "commission": finite_number(getattr(trade, "commission", None)),
        }

    @staticmethod
    def _order_report(order: Any, observed: bool = False) -> dict[str, Any]:
        order_id = str(getattr(order, "order_id", "") or "")
        exchange_id = str(getattr(order, "exchange_id", "") or "")
        instrument_id = str(getattr(order, "instrument_id", "") or "")
        original = int(number(getattr(order, "volume_orign", 0)))
        left = int(number(getattr(order, "volume_left", 0)))
        evidence = any(
            (
                observed,
                bool(str(getattr(order, "exchange_order_id", "") or "")),
                original > 0 and left < original,
            )
        )
        if not order_id or not evidence:
            return {"ordersInitialized": True, "found": False}
        return {
            "ordersInitialized": True,
            "found": True,
            "order": {
                "clientOrderId": order_id,
                "exchangeOrderId": str(getattr(order, "exchange_order_id", "") or ""),
                "symbol": f"{exchange_id}.{instrument_id}"
                if exchange_id and instrument_id
                else "",
                "direction": str(getattr(order, "direction", "") or ""),
                "offset": str(getattr(order, "offset", "") or ""),
                "volume": original,
                "volumeLeft": left,
                "limitPrice": finite_number(getattr(order, "limit_price", None)),
                "priceType": str(getattr(order, "price_type", "") or ""),
                "status": str(getattr(order, "status", "") or "UNKNOWN"),
                "isOnline": getattr(order, "is_online", None) is True,
                "isDead": getattr(order, "is_dead", None) is True,
                "isError": getattr(order, "is_error", None) is True,
                "lastMsg": str(getattr(order, "last_msg", "") or ""),
            },
        }

    def _wait_for_order(
        self, order: Any, timeout_seconds: float = 2.0
    ) -> dict[str, Any]:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.api.wait_update(deadline=min(deadline, time.time() + 0.25)) is True and self.api.is_changing(order):
                self._observed_order_ids.add(str(getattr(order, "order_id", "")))
            report = self._order_report(order, str(getattr(order, "order_id", "")) in self._observed_order_ids)
            if report["found"]:
                return report
        return self._order_report(order, str(getattr(order, "order_id", "")) in self._observed_order_ids)

    def submit_order_once(self, request: dict[str, Any]) -> dict[str, Any]:
        order = self.api.insert_order(
            symbol=request["symbol"],
            direction=request["direction"],
            offset=request["offset"],
            volume=request["volume"],
            limit_price=request["limitPrice"],
            order_id=request["clientOrderId"],
        )
        return self._wait_for_order(order)

    def query_order(self, client_order_id: str) -> dict[str, Any]:
        order = self.api.get_order(client_order_id)
        return self._wait_for_order(order, 0.2)

    def cancel_order_once(self, client_order_id: str) -> dict[str, Any]:
        order = self.api.get_order(client_order_id)
        current = self._order_report(order, client_order_id in self._observed_order_ids)
        if not current["found"]:
            raise RuntimeError("order_not_confirmed")
        remote = current["order"]
        if remote["status"] != "ALIVE" or int(remote["volumeLeft"]) <= 0:
            raise RuntimeError("order_not_cancelable")
        self.api.cancel_order(order)
        report = self._wait_for_order(order)
        if report.get("order", {}).get("status") != "FINISHED":
            report["commandStatus"] = "UNKNOWN"
        return report

    def close(self) -> None:
        self.api.close()


class FixtureSession:
    """Deterministic process-boundary fixture; it never represents a real account."""

    def __init__(self, symbols: Sequence[str]) -> None:
        self.symbols = tuple(symbols)
        self.orders_initialized = True
        self.orders: dict[str, dict[str, Any]] = {}

    def instrument_catalog(self) -> list[dict[str, Any]]:
        return fixture_instrument_catalog()

    def market_quote(self, symbol: str) -> dict[str, Any]:
        item = next(
            (entry for entry in fixture_instrument_catalog()
             if entry["symbol"].upper() == symbol.upper()),
            None,
        )
        if item is None:
            raise KeyError(symbol)
        base = 820.0 if item["productId"] == "au" else 3500.0
        tick = item["priceTick"]
        bid_volumes, ask_volumes = (12, 10, 8, 6, 4), (9, 7, 5, 3, 2)
        quote = {
            "symbol": item["symbol"], "name": item["instrumentName"],
            "quoteDatetime": utc_now(), "lastPrice": base,
            "changePercent": 0.2, "bidPrice1": base - tick,
            "bidVolume1": 12, "askPrice1": base + tick,
            "askVolume1": 9, "open": base - tick * 4,
            "highest": base + item["priceTick"] * 10,
            "lowest": base - item["priceTick"] * 10,
            "volume": 24_680, "openInterest": 8_000,
            "priceTick": item["priceTick"], "volumeMultiple": item["volumeMultiple"],
            "upperLimit": base * 1.1, "lowerLimit": base * 0.9,
            "openLimit": item.get("openLimit", 0),
            "marginPerLot": round(base * item["volumeMultiple"] * 0.1, 2),
            "commissionPerLot": round(base * item["volumeMultiple"] * 3e-5, 2),
            "tradingStatus": "CONTINOUS", "tradingStatusLabel": "连续交易",
        }
        for level in range(2, 6):
            quote[f"bidPrice{level}"] = base - tick * level
            quote[f"bidVolume{level}"] = bid_volumes[level - 1]
            quote[f"askPrice{level}"] = base + tick * level
            quote[f"askVolume{level}"] = ask_volumes[level - 1]
        return quote

    def market_board_rows(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Fixture 报价总表 rows: one deterministic row per main contract, the
        same field shape the live board worker renders."""
        catalog = fixture_instrument_catalog()
        percents = {"au": 0.85, "rb": -0.42, "i": 1.1, "IF": -0.25}
        volumes = {"au": 12_340, "rb": 98_700, "i": 45_600, "IF": 7_890}
        interests = {"au": 8_000, "rb": 2_450_000, "i": 765_000, "IF": 234_000}
        rows: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            item = next(
                (entry for entry in catalog
                 if entry["symbol"].upper() == symbol.upper()),
                None,
            )
            if item is None:
                continue
            quote = self.market_quote(item["symbol"])
            last = float(quote["lastPrice"])
            pre = round(last / (1 + percents.get(item["productId"], 0.3) / 100), 2)
            change = round(last - pre, 2)
            rows[item["symbol"]] = {
                "symbol": item["symbol"],
                "exchangeId": item["exchangeId"],
                "productId": item["productId"],
                "instrumentId": item["instrumentId"],
                "instrumentName": item["instrumentName"],
                "productName": board_product_name(item["instrumentName"]),
                "last": last,
                "preSettlement": pre,
                "change": change,
                "changePercent": round(change / pre * 100, 2),
                "volume": volumes.get(item["productId"], 24_680),
                "openInterest": interests.get(item["productId"], 8_000),
                "openInterestChange": -240,
                "high": quote["highest"],
                "low": quote["lowest"],
                "upperLimit": quote["upperLimit"],
                "lowerLimit": quote["lowerLimit"],
                "bidPrice1": quote.get("bidPrice1"),
                "askPrice1": quote.get("askPrice1"),
                "quoteTime": quote["quoteDatetime"],
                "status": "normal",
            }
        return rows

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        item = next(
            (entry for entry in fixture_instrument_catalog()
             if entry["symbol"].upper() == symbol.upper()),
            None,
        )
        if item is None:
            raise KeyError(symbol)
        base = 820.0 if item["productId"] == "au" else 3500.0
        start = 1_788_192_000_000_000_000
        bars = []
        for index in range(80):
            opening = base + index * 0.4
            bars.append({
                "datetime": start + index * duration_seconds * 1_000_000_000,
                "open": opening,
                "high": opening + 1.2,
                "low": opening - 0.8,
                "close": opening + 0.6,
                "volume": 120 + index,
                "openInterest": 8_000 + index * 3,
                "tradingDay": fixture_trading_day(
                    datetime.fromtimestamp(
                        (start + index * duration_seconds * 1_000_000_000) / 1_000_000_000,
                        tz=UTC,
                    )
                ),
            })
        last = bars[-1]["close"]
        tick = item["priceTick"]
        bid_volumes, ask_volumes = (12, 10, 8, 6, 4), (9, 7, 5, 3, 2)
        quote = {
            "symbol": item["symbol"],
            "name": item["instrumentName"],
            "quoteDatetime": utc_now(),
            "lastPrice": last,
            "changePercent": 0.68,
            "bidPrice1": last - tick,
            "bidVolume1": 12,
            "askPrice1": last + tick,
            "askVolume1": 9,
            "open": bars[0]["open"],
            "highest": max(bar["high"] for bar in bars),
            "lowest": min(bar["low"] for bar in bars),
            "volume": 24_680,
            "openInterest": bars[-1]["openInterest"],
            "priceTick": tick,
            "volumeMultiple": item["volumeMultiple"],
            "upperLimit": last * 1.1,
            "lowerLimit": last * 0.9,
            "openLimit": item.get("openLimit", 0),
            "tradingStatus": "CONTINOUS",
            "tradingStatusLabel": "连续交易",
        }
        for level in range(2, 6):
            quote[f"bidPrice{level}"] = last - tick * level
            quote[f"bidVolume{level}"] = bid_volumes[level - 1]
            quote[f"askPrice{level}"] = last + tick * level
            quote[f"askVolume{level}"] = ask_volumes[level - 1]
        return {
            "schemaVersion": 1,
            "durationSeconds": duration_seconds,
            "quote": quote,
            "bars": bars,
        }

    @staticmethod
    def _fixture_trading_days(count: int) -> list[str]:
        """The most recent `count` fixture trading days, newest first."""
        days: list[str] = []
        candidate = datetime.now(UTC).astimezone(CHINA_TIMEZONE)
        while len(days) < count:
            iso = fixture_trading_day(candidate.replace(hour=12))
            if iso not in days:
                days.append(iso)
            candidate -= timedelta(days=1)
        return days

    def query_settlements(self, symbols: Sequence[str], days: int) -> dict[str, Any]:
        catalog = fixture_instrument_catalog()
        days_list = self._fixture_trading_days(days)
        items: list[dict[str, Any]] = []
        for symbol in symbols:
            item = next(
                (entry for entry in catalog
                 if entry["symbol"].upper() == symbol.upper()),
                None,
            )
            if item is None:
                raise KeyError(symbol)
            base = 820.0 if item["productId"] == "au" else 3500.0
            for offset, trading_day in enumerate(days_list):
                items.append({
                    "symbol": item["symbol"],
                    "tradingDay": trading_day,
                    "settlement": round(base - offset * 1.5, 2),
                })
        items.sort(key=lambda entry: (entry["symbol"], entry["tradingDay"]))
        return {"schemaVersion": 1, "days": days, "asOf": max(days_list), "items": items}

    def query_cont_history(self, symbols: Sequence[str], days: int) -> dict[str, Any]:
        days_list = self._fixture_trading_days(days)
        result: dict[str, Any] = {"schemaVersion": 1, "days": days, "symbols": {}, "rollDates": {}}
        for symbol in symbols:
            value = str(symbol)
            if not CONTINUOUS_FUTURES_SYMBOL.fullmatch(value):
                raise KeyError(symbol)
            exchange, product = value.removeprefix("KQ.m@").split(".", 1)
            # A synthetic roll two thirds back: the older contract led until then.
            previous = days_list[max(days - 1 - (2 * days // 3), 0)]
            rows = [
                {
                    "date": day,
                    "mainSymbol": (
                        f"{exchange}.{product}2510" if day < previous
                        else f"{exchange}.{product}2610"
                    ),
                }
                for day in reversed(days_list)
            ]
            result["symbols"][value] = rows
            result["rollDates"][value] = [
                rows[index]["date"]
                for index in range(1, len(rows))
                if rows[index]["mainSymbol"] != rows[index - 1]["mainSymbol"]
            ]
        return result

    def query_ranking(self, symbol: str, ranking_type: str, days: int) -> dict[str, Any]:
        item = next(
            (entry for entry in fixture_instrument_catalog()
             if entry["symbol"].upper() == symbol.upper()),
            None,
        )
        if item is None:
            raise KeyError(symbol)
        if ranking_type not in RANKING_COLUMNS:
            raise KeyError(ranking_type)
        brokers = ("东证期货", "国泰君安", "银河期货", "永安期货", "中信期货",
                   "海通期货", "广发期货", "申银万国", "华泰期货", "光大期货",
                   "方正中期", "国投安信", "招商期货", "浙商期货", "中粮期货",
                   "南华期货", "五矿期货", "建信期货", "瑞达期货", "兴业期货")
        days_list = self._fixture_trading_days(days)
        rows: list[dict[str, Any]] = []
        for trading_day in days_list:
            for index, broker in enumerate(brokers):
                rows.append({
                    "tradingDay": trading_day,
                    "broker": broker,
                    "value": float(24_000 - index * 950),
                    "change": float((index % 5 - 2) * 310),
                    "rank": index + 1,
                })
        rows.sort(key=lambda entry: (entry["tradingDay"], entry["rank"]))
        return {
            "schemaVersion": 1,
            "symbol": item["symbol"],
            "rankingType": ranking_type,
            "days": days,
            "asOf": max(days_list),
            "rows": rows,
        }

    def read(self, timeout_seconds: float) -> Snapshot:
        del timeout_seconds
        markets = {
            symbol: {
                "symbol": symbol,
                "name": "只读 Sidecar 合成行情",
                "price": 823.5 if "au" in symbol.lower() else 3512.0,
                "bidPrice1": 823.0 if "au" in symbol.lower() else 3511.0,
                "askPrice1": 824.0 if "au" in symbol.lower() else 3512.0,
                "priceTick": 0.5 if "au" in symbol.lower() else 1.0,
                "volumeMultiple": 1000.0 if "au" in symbol.lower() else 10.0,
                "upperLimit": 1000.0 if "au" in symbol.lower() else 4000.0,
                "lowerLimit": 600.0 if "au" in symbol.lower() else 3000.0,
                "changePercent": 0.0,
                "quoteDatetime": utc_now(),
                "marginPerLot": 82_350.0 if "au" in symbol.lower() else 351.2,
                "commissionPerLot": 24.71 if "au" in symbol.lower() else 1.05,
                "tradingStatus": "CONTINOUS",
                "tradingStatusLabel": "连续交易",
            }
            for symbol in self.symbols
        }
        return Snapshot(
            account={
                "balance": 0.0,
                "staticBalance": 0.0,
                "available": 0.0,
                "floatProfit": 0.0,
                "positionProfit": 0.0,
                "closeProfit": 0.0,
                "commission": 0.0,
                "margin": 0.0,
            },
            positions=[],
            orders=[dict(item) for item in self.orders.values()],
            trades=[],
            markets=markets,
            trading_day=fixture_trading_day(datetime.now(UTC)),
            captured_at=utc_now(),
        )

    def close(self) -> None:
        return None

    def submit_order_once(self, request: dict[str, Any]) -> dict[str, Any]:
        order = {
            "clientOrderId": request["clientOrderId"],
            "exchangeOrderId": "FIXTURE-ORDER",
            "symbol": request["symbol"],
            "direction": request["direction"],
            "offset": request["offset"],
            "volume": request["volume"],
            "volumeLeft": request["volume"],
            "filledVolume": 0,
            "limitPrice": request["limitPrice"],
            "priceType": "LIMIT",
            "status": "ALIVE",
            "insertDateTime": time.time_ns(),
            "isOnline": True,
            "isDead": False,
            "isError": False,
            "lastMsg": "fixture alive",
        }
        self.orders[request["clientOrderId"]] = order
        return {"ordersInitialized": True, "found": True, "order": dict(order)}

    def query_order(self, client_order_id: str) -> dict[str, Any]:
        order = self.orders.get(client_order_id)
        return {
            "ordersInitialized": True,
            "found": order is not None,
            **({"order": dict(order)} if order else {}),
        }

    def cancel_order_once(self, client_order_id: str) -> dict[str, Any]:
        order = self.orders.get(client_order_id)
        if order is None or order["status"] != "ALIVE" or order["volumeLeft"] <= 0:
            raise RuntimeError("order_not_cancelable")
        order.update(
            {
                "status": "FINISHED",
                "isOnline": False,
                "isDead": True,
                "lastMsg": "fixture canceled",
            }
        )
        return {"ordersInitialized": True, "found": True, "order": dict(order)}


@dataclass
class ActorCommand:
    kind: str
    command_id: str
    client_order_id: str
    request: dict[str, Any]
    response: queue.Queue[dict[str, Any]]
    # When the command was queued; a submit reports how long it waited for the actor.
    queued_at: float = field(default_factory=time.monotonic)


class SnapshotService:
    def __init__(
        self, *, username: str | None, password: str | None, symbols: Sequence[str],
        fixture: bool = False, stale_after_seconds: float = 15.0,
        initial_snapshot_timeout_seconds: float = 180.0,
        trading_enabled: bool = False,
        market_only: bool = False,
        reexec_on_wedge: bool = False,
        session_factory: Callable[
            [str, str, Sequence[str], Callable[[str], None]], SnapshotSession
        ] | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self.symbols = tuple(symbols)
        self.fixture = fixture
        # 行情专用 (2026-10-13): a market-source pool child. Its session is data-only (no TqKq
        # trading session), it never trades, and it serves no account, positions or orders —
        # only status, market and contract reads.
        self.market_only = market_only
        self.trading_enabled = trading_enabled and not market_only
        self.stale_after_seconds = stale_after_seconds
        self.initial_snapshot_timeout_seconds = initial_snapshot_timeout_seconds
        self.session_factory = session_factory or partial(OfficialTqKqSession, market_only=market_only)
        # Only the container entry reads its credentials from files and can start over by re-exec;
        # pool children get theirs on stdin once, so they keep the in-process retries only.
        self.reexec_on_wedge = reexec_on_wedge
        self.reexec: Callable[[], None] = reexec_process
        self._snapshot: Snapshot | None = None
        self._captured_monotonic: float | None = None
        self._status = "connecting" if fixture or (username and password) else "unavailable"
        self._error_code = None if self._status == "connecting" else "tqkq_credentials_missing"
        self._connection_stage = "starting" if self._status == "connecting" else "failed"
        self._started_monotonic = time.monotonic()
        self._stage_started_monotonic = self._started_monotonic
        self._startup_durations: dict[str, float] = {}
        self._startup_total_seconds: float | None = None
        # Reentrant so a read can hold ONE boundary and re-read the derived status
        # (#700 review): the snapshot and its status/age/ordersInitialized leave the
        # service as a single cut, never one publish apart.
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._session: Any = None
        self._thread: threading.Thread | None = None
        # Session generation: a replaced worker sees a newer generation and exits.
        self._generation = 0
        self._worker: threading.Thread | None = None
        self._command_timeouts = 0
        self._last_market_request = time.monotonic()
        self._session_started_monotonic = time.monotonic()
        self._session_rebuilds: list[dict[str, Any]] = []
        # Why the last worker ended on its own (exception class + message); the retry reason carries the class.
        self._worker_exit: dict[str, str] | None = None
        self._commands: queue.Queue[ActorCommand] = queue.Queue()
        self._command_results: dict[str, dict[str, Any]] = {}
        self._submitted_order_ids: set[str] = set()
        # Submits answered without ever reaching TqSdk (expired in the queue, dropped by a session
        # rebuild), by clientOrderId: the Runtime's follow-up query gets this definite answer
        # instead of a bare "not found" it must keep treating as unknown.
        self._undispatched_orders: dict[str, dict[str, Any]] = {}
        self._cancelled_order_ids: set[str] = set()
        self._backtest_lock = threading.Lock()
        self._backtest_credentials = (username or "", password or "")
        from .replay import ReplayManager
        self.replays = ReplayManager(lambda: self._backtest_credentials)
        # 规则回测的历史取数：独立 data-only TqApi，与实盘会话隔离（history.py）。
        from .history import HistoryActor
        self.history = HistoryActor(lambda: self._backtest_credentials)
        from .public_market import PublicMarketService
        self.public_market = PublicMarketService(
            lambda: self._backtest_credentials, fixture=fixture,
            stale_after_seconds=stale_after_seconds,
        )
        self._orders_initialized = False
        self._instruments: list[dict[str, Any]] | None = None
        self._instrument_catalog_captured_at: str | None = None
        self._instrument_catalog_error: str | None = None
        self._instrument_catalog_pending = True

    def start(self) -> None:
        if self._status == "unavailable":
            return
        self._thread = threading.Thread(target=self._run, name="tqkq-readonly-source", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.replays.close()
        self.history.close()
        self.public_market.close()
        if self._thread:
            self._thread.join(timeout=5)
        self._backtest_credentials = ("", "")

    def _run(self) -> None:
        """Supervisor: runs one session worker at a time and replaces it when it
        stalls (TqSdk wedged inside a reconnect or a chart download) or when the
        market is closed and the subscription cap is reached with nobody asking."""
        with self._lock:
            username, password = self._username or "", self._password or ""
            self._username = self._password = None
        credentials = (username, password)
        failures = 0
        same_exits, last_exit = 0, None
        stalls = 0  # consecutive stall rebuilds without a session that served long enough
        while not self._stop.is_set():
            with self._lock:
                self._generation += 1
                generation = self._generation
                commands = self._commands
                self._command_timeouts = 0
                self._session_started_monotonic = time.monotonic()
            worker = threading.Thread(target=self._serve, args=(credentials, generation, commands),
                                      name=f"tqkq-readonly-session-{generation}", daemon=True)
            self._worker = worker
            worker.start()
            reason = None
            while not self._stop.is_set():
                self._stop.wait(SESSION_SUPERVISOR_POLL_SECONDS)
                if not worker.is_alive():
                    break
                reason = self._rebuild_reason()
                if reason:
                    break
                with self._lock:
                    serving_for = time.monotonic() - self._session_started_monotonic if self._session is not None else 0.0
                if stalls and serving_for >= SESSION_STALL_STREAK_RESET_SECONDS:
                    stalls = 0
            if self._stop.is_set():
                break
            if reason is None:
                # The worker ended on its own: a fatal credential problem stops here, anything
                # else (TqKq maintenance, a transient login failure) is retried with backoff.
                with self._lock:
                    code = self._error_code
                    had_snapshot = self._snapshot is not None
                    exit_type = (self._worker_exit or {}).get("type")
                    signature = (exit_type, (self._worker_exit or {}).get("message"))
                    served = self._captured_monotonic is not None and self._captured_monotonic >= self._session_started_monotonic
                same_exits = 1 if served or signature != last_exit else same_exits + 1
                last_exit = signature
                if self.reexec_on_wedge and same_exits >= REEXEC_AFTER_SAME_EXITS and reexec_allowed():
                    try:
                        sys.stderr.write(f"tqkq-readonly-session re-exec after {same_exits} identical worker exits "
                                         f"({signature[0]}: {signature[1]})\n")
                        sys.stderr.flush()
                    except Exception:
                        pass
                    self.reexec()
                    return
                if code in FATAL_SESSION_CODES or not had_snapshot and failures >= len(SESSION_RETRY_BACKOFF_SECONDS):
                    break
                delay = SESSION_RETRY_BACKOFF_SECONDS[min(failures, len(SESSION_RETRY_BACKOFF_SECONDS) - 1)]
                failures += 1
                reason = f"session_retry:{code or 'unknown'}" + (f":{exit_type}" if exit_type else "")
                self._begin_rebuild(reason, generation, keep_error=True)
                self._stop.wait(delay)
                continue
            failures = 0
            self._dump_actor_traceback(worker, reason)
            self._begin_rebuild(reason, generation)
            old_session = self._session
            self._session = None
            threading.Thread(target=self._close_quietly, args=(old_session,), name="tqkq-readonly-close", daemon=True).start()
            # The first rebuild is immediate; a stall that repeats right after a rebuild
            # backs off, so a wedged upstream cannot turn into a login storm.
            if stalls:
                self._stop.wait(SESSION_STALL_BACKOFF_SECONDS[min(stalls - 1, len(SESSION_STALL_BACKOFF_SECONDS) - 1)])
            stalls += 1

    @staticmethod
    def _dump_actor_traceback(worker: threading.Thread, reason: str) -> None:
        """Where the actor is stuck, on stderr, before it is replaced (tqsdk prints nothing itself)."""
        try:
            sys.stderr.write(f"tqkq-readonly-session rebuild reason={reason} thread={worker.name}\n")
            if worker.ident is not None:
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            sys.stderr.flush()
        except Exception:
            pass

    def _serve(self, credentials: tuple[str, str], generation: int, commands: queue.Queue[ActorCommand]) -> None:
        # `commands` is this worker's own queue: a rebuild hands the next worker a fresh one, so a replaced
        # worker that wakes up (close() ends its stuck read) cannot take a request meant for its successor.
        session: SnapshotSession | None = None
        try:
            session = FixtureSession(self.symbols) if self.fixture else self.session_factory(
                credentials[0], credentials[1], self.symbols, self._set_stage
            )
            with self._lock:
                if self._generation != generation:
                    return
                self._session = session
                if isinstance(session, OfficialTqKqSession):
                    session.commands_waiting = lambda: not commands.empty()
                # Timeouts that hit while this session was being built are not its stalls.
                self._command_timeouts = 0
                self._session_started_monotonic = time.monotonic()
                self._orders_initialized = bool(
                    getattr(session, "orders_initialized", False)
                )
            while not self._stop.is_set() and self._generation == generation:
                try:
                    command = commands.get_nowait()
                except queue.Empty:
                    command = None
                if command is not None:
                    self._handle_command(session, command)
                snapshot = session.read(0.25)
                if self._generation != generation:
                    break
                if snapshot is not None:
                    with self._lock:
                        if self._startup_total_seconds is None:
                            now = time.monotonic()
                            self._record_stage_duration(now)
                            self._startup_total_seconds = round(now - self._started_monotonic, 3)
                        self._snapshot = snapshot
                        self._captured_monotonic = time.monotonic()
                        self._status = "simulated" if self.fixture else "normal"
                        # A single contract without a quote (no night session before Monday's
                        # open) keeps its own dataErrorCode; only every quote missing stalls the feed.
                        missing_quotes = bool(snapshot.markets) and all(m.get("dataErrorCode") for m in snapshot.markets.values())
                        self._error_code = "market_quote_not_ready" if missing_quotes else None
                        self._connection_stage = "waiting_market" if missing_quotes else "ready"
                        self._orders_initialized = bool(getattr(session, "orders_initialized", False))
                    if self._instrument_catalog_pending:
                        try:
                            instruments = session.instrument_catalog()
                            catalog_error = getattr(session, "_instrument_catalog_error", None)
                        except Exception:
                            instruments = None
                            catalog_error = "tqkq_instrument_catalog_unavailable"
                        with self._lock:
                            if instruments:
                                self._instruments = instruments
                                self._instrument_catalog_captured_at = utc_now()
                            self._instrument_catalog_error = catalog_error
                            self._instrument_catalog_pending = bool(getattr(session, "catalog_pending", False))
                elif self._age_seconds() > self.stale_after_seconds:
                    with self._lock:
                        self._status = "stale"
                # The idle pause ends as soon as a command (an order) is queued: sleeping it out
                # added up to 0.1 s to every order on top of the read above (2026-09-27).
                try:
                    command = commands.get(timeout=0.1)
                except queue.Empty:
                    command = None
                if command is not None:
                    if self._stop.is_set() or self._generation != generation:
                        self._answer_unhandled(command, "sidecar_reconnecting")
                        break
                    self._handle_command(session, command)
        except Exception as error:
            if self._generation != generation:
                return
            # The worker is ending on its own: say exactly why, with the stack, before the supervisor retries.
            detail = {"type": type(error).__name__, "message": str(error)[:300]}
            try:
                sys.stderr.write(f"tqkq-readonly-session worker exit generation={generation} "
                                 f"error={detail['type']}: {detail['message']}\n{traceback.format_exc()}")
                sys.stderr.flush()
            except Exception:
                pass
            with self._lock:
                self._worker_exit = detail
                if self._startup_total_seconds is None:
                    self._record_stage_duration(time.monotonic())
                self._status = "unavailable"
                self._error_code = tqkq_error_code(error)
                self._connection_stage = "failed"
                if self._instruments is None:
                    self._instrument_catalog_error = tqkq_error_code(error)
        finally:
            if session and self._generation == generation:
                self._close_quietly(session)

    @staticmethod
    def _close_quietly(session: Any) -> None:
        if session is None:
            return
        try:
            session.close()
        except Exception:
            pass

    def _rebuild_reason(self) -> str | None:
        """Why the current session should be replaced, or None."""
        with self._lock:
            session = self._session
            captured = self._captured_monotonic
            started = self._session_started_monotonic
            timeouts = self._command_timeouts
            idle_for = time.monotonic() - self._last_market_request
            snapshot = self._snapshot
        if session is None:
            return None
        now = time.monotonic()
        if timeouts >= SESSION_STALL_TIMEOUTS:
            return "market_session_stalled"
        # Only this session's own snapshots count; a snapshot left by the previous
        # session must not make a slow cold start look stalled.
        if captured is not None and captured >= started and now - captured > SESSION_STALL_SECONDS:
            return "market_session_stalled"
        if (captured is None or captured < started) and now - started > SESSION_FIRST_SNAPSHOT_SECONDS:
            return "market_session_stalled"
        exhausted = getattr(session, "subscriptions_exhausted", None)
        if callable(exhausted) and exhausted() and idle_for > SESSION_IDLE_RESET_SECONDS and snapshot is not None:
            sessions = {str(item.get("marketSessionStatus") or "unknown") for item in snapshot.markets.values()}
            if sessions and sessions <= {"closed"}:
                return "market_subscription_reset"
        return None

    def _begin_rebuild(self, reason: str, generation: int, *, keep_error: bool = False) -> None:
        # Commands queued for the session being replaced are answered now instead of
        # being served by the next session as if they were fresh; the next session gets its own queue.
        with self._lock:
            commands, self._commands = self._commands, queue.Queue()
        while True:
            try:
                command = commands.get_nowait()
            except queue.Empty:
                break
            self._answer_unhandled(command, "sidecar_reconnecting")
        try:
            sys.stderr.write(f"tqkq-readonly-session rebuild reason={reason} generation={generation}\n")
            sys.stderr.flush()
        except Exception:
            pass
        with self._lock:
            self._session_rebuilds.append({"reason": reason, "generation": generation, "at": utc_now()})
            self._session_rebuilds = self._session_rebuilds[-20:]
            self._command_timeouts = 0
            if not keep_error:
                self._error_code = reason
            self._connection_stage = "reconnecting"
            if self._snapshot is not None:
                self._status = "stale"
            # Startup timings are per session so the client's cold-start phases show again.
            self._started_monotonic = time.monotonic()
            self._stage_started_monotonic = self._started_monotonic
            self._startup_durations = {}
            self._startup_total_seconds = None
            # A catalogue the last session only got partly (load failed) is taken again from the next one.
            self._instrument_catalog_pending = self._instruments is None or self._instrument_catalog_error is not None

    def _require_session(self) -> None:
        """A market request while the session is being rebuilt is answered at once, not after a timeout."""
        with self._lock:
            session = self._session
        if session is None:
            raise RuntimeError("sidecar_reconnecting")

    def _note_command_timeout(self) -> None:
        with self._lock:
            # A timeout counts against a session only once it has delivered its first snapshot: a
            # session that is still receiving its initial data answers slowly, not stalled.
            # SESSION_FIRST_SNAPSHOT_SECONDS still bounds a session that never gets there.
            started = self._session_started_monotonic
            captured = self._captured_monotonic
            if self._session is not None and captured is not None and captured >= started:
                self._command_timeouts += 1

    def _note_command_ok(self) -> None:
        with self._lock:
            self._command_timeouts = 0

    def _not_dispatched(self, command: ActorCommand, code: str) -> dict[str, Any]:
        """A submit/cancel that never reached TqSdk, said as such. A submit is also
        remembered by its clientOrderId, so the Runtime's query for an order whose
        call it gave up on learns that nothing was sent (and may send it again from
        "not sent") instead of reading a bare "not found" as still unknown."""
        result = {"ordersInitialized": True, "found": False,
                  "commandStatus": "NOT_DISPATCHED", "errorCode": code}
        if command.kind == "submit":
            with self._lock:
                if command.client_order_id not in self._submitted_order_ids:
                    self._undispatched_orders[command.client_order_id] = dict(result)
        return result

    def _answer_unhandled(self, command: ActorCommand, code: str) -> None:
        """Answer a queued command the actor will never run (session rebuild, stop)."""
        result = (self._not_dispatched(command, code) if command.kind in {"submit", "cancel"}
                  else {"errorCode": code})
        try:
            command.response.put_nowait(result)
        except queue.Full:
            pass

    def _handle_command(self, session: SnapshotSession, command: ActorCommand) -> None:
        if command.kind == "quote":
            try:
                result = {"quote": session.market_quote(str(command.request["symbol"]))}
            except KeyError:
                result = {"errorCode": "symbol_not_found"}
            except Exception as error:
                result = {"errorCode": str(error) or type(error).__name__}
            command.response.put(result)
            return
        if command.kind == "market":
            try:
                result = session.market_workbench(
                    str(command.request["symbol"]),
                    int(command.request["durationSeconds"]),
                )
            except KeyError:
                result = {"errorCode": "symbol_not_found"}
            except Exception as error:
                result = {"errorCode": str(error) or type(error).__name__}
            command.response.put(result)
            return
        if command.kind in {"settlement", "cont_history", "ranking"}:
            try:
                if command.kind == "settlement":
                    result = session.query_settlements(
                        [str(item) for item in command.request["symbols"]],
                        int(command.request["days"]),
                    )
                elif command.kind == "cont_history":
                    result = session.query_cont_history(
                        [str(item) for item in command.request["symbols"]],
                        int(command.request["days"]),
                    )
                else:
                    result = session.query_ranking(
                        str(command.request["symbol"]),
                        str(command.request["rankingType"]),
                        int(command.request["days"]),
                    )
            except KeyError:
                result = {"errorCode": "symbol_not_found"}
            except Exception as error:
                result = {"errorCode": str(error) or type(error).__name__}
            command.response.put(result)
            return
        cached = (
            self._command_results.get(command.command_id)
            if command.kind != "query"
            else None
        )
        if cached is not None:
            command.response.put(dict(cached))
            return
        if (command.kind == "submit" and command.client_order_id not in self._submitted_order_ids
                and time.monotonic() - command.queued_at >= ORDER_COMMAND_WAIT_SECONDS):
            # The caller stopped waiting (gateway_command_timeout) before the actor got here:
            # never send it now, minutes late at its old price. Not cached by commandId — the
            # Runtime's commandId is per order, and "not sent" must stay sendable from 未发送.
            command.response.put(self._not_dispatched(command, "gateway_command_expired"))
            return
        if command.kind == "query":
            with self._lock:
                undispatched = (self._undispatched_orders.get(command.client_order_id)
                                if command.client_order_id not in self._submitted_order_ids else None)
            if undispatched is not None:
                command.response.put(dict(undispatched))
                return
        try:
            if command.kind == "submit":
                if command.client_order_id in self._submitted_order_ids:
                    result = session.query_order(command.client_order_id)
                else:
                    with self._lock:
                        self._submitted_order_ids.add(command.client_order_id)
                        self._undispatched_orders.pop(command.client_order_id, None)
                    started = time.monotonic()
                    result = session.submit_order_once(command.request)
                    # Where an order's time goes: waiting for this actor, then insert → the
                    # exchange's acknowledgement. The Runtime logs it with its own share.
                    result = {**result, "timing": {
                        "queuedMs": round((started - command.queued_at) * 1000),
                        "exchangeMs": round((time.monotonic() - started) * 1000),
                    }}
            elif command.kind == "query":
                result = session.query_order(command.client_order_id)
            elif command.kind == "cancel":
                if command.client_order_id in self._cancelled_order_ids:
                    result = session.query_order(command.client_order_id)
                else:
                    self._cancelled_order_ids.add(command.client_order_id)
                    result = session.cancel_order_once(command.client_order_id)
            else:
                raise RuntimeError("unknown_actor_command")
        except Exception as error:
            result = {
                "ordersInitialized": self._orders_initialized,
                "found": False,
                "commandStatus": "UNKNOWN",
                "errorCode": f"gateway_{type(error).__name__.lower()}",
            }
        if command.kind != "query":
            self._command_results[command.command_id] = dict(result)
        command.response.put(result)

    def _handle_backtest_request(
        self, request: dict[str, Any], response: queue.Queue[dict[str, Any]],
    ) -> None:
        try:
            if self.fixture:
                result = self._fixture_backtest(request)
            else:
                from .tqsdk_backtest import run_tqsdk_backtest

                username, password = self._backtest_credentials
                if not username or not password:
                    raise RuntimeError("tqsdk_backtest_credentials_missing")
                result = run_tqsdk_backtest(request, username, password)
        except Exception as error:
            result = {"errorCode": tqkq_error_code(error)}
        finally:
            self._backtest_lock.release()
        response.put(result)

    @staticmethod
    def _fixture_backtest(request: dict[str, Any]) -> dict[str, Any]:
        bars = [
            {
                "datetime": (
                    timestamp := 1_770_000_000_000_000_000
                    + index * 3_600_000_000_000
                ),
                "open": 3500.0 + index,
                "high": 3502.0 + index,
                "low": 3498.0 + index,
                "close": 3501.0 + index,
                "volume": 100 + index,
                "openInterest": 8_000 + index,
                "tradingDay": fixture_trading_day(
                    datetime.fromtimestamp(timestamp / 1_000_000_000, tz=UTC)
                ),
            }
            for index in range(30)
        ]
        digest = hashlib.sha256(json.dumps(
            bars, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        baseline = {
            "returnPercent": 1.2, "maxDrawdownPercent": 0.6,
            "signalCount": 4, "winRatePercent": 50.0, "tradeCount": 4,
            "commission": 8.0, "curve": [1_000_000.0, 1_005_000.0, 1_012_000.0],
        }
        candidate = {
            "returnPercent": 1.8, "maxDrawdownPercent": 0.5,
            "signalCount": 6, "winRatePercent": 66.67, "tradeCount": 6,
            "commission": 12.0, "curve": [1_000_000.0, 1_008_000.0, 1_018_000.0],
        }
        return {
            "engineVersion": "tqsdk-3.10.2-tqsim-tqbacktest-fixture",
            "engineDigest": hashlib.sha256(b"tqsdk-backtest-fixture-v1").hexdigest(),
            "dataSource": (
                f"fixture:{request['symbol']}:{request['startDate']}:"
                f"{request['endDate']}:{request['durationSeconds']}"
            ),
            "dataDigest": digest,
            "dataPointCount": len(bars),
            "contractMultiplier": 10.0,
            "priceTick": 1.0,
            "fillMode": "ACTIVE",
            "commissionModel": "tqsdk_default",
            "bars": bars,
            "baseline": baseline,
            "candidate": candidate,
            "executionEnabled": False,
            "orderCapabilities": [],
            "accountType": "TqSim",
        }

    def _actor_call(
        self,
        *,
        kind: str,
        command_id: str,
        client_order_id: str,
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.trading_enabled:
            raise RuntimeError("simulation_trading_disabled")
        with self._lock:
            initialized = self._orders_initialized
        if not initialized:
            return {"ordersInitialized": False, "found": False, "commandStatus": "NOT_DISPATCHED"}
        response: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._commands.put(
            ActorCommand(
                kind,
                command_id,
                client_order_id,
                request or {},
                response,
            )
        )
        try:
            return response.get(timeout=ORDER_COMMAND_WAIT_SECONDS)
        except queue.Empty as error:
            raise RuntimeError("gateway_command_timeout") from error

    def submit_order_once(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._actor_call(
            kind="submit",
            command_id=str(request["commandId"]),
            client_order_id=str(request["clientOrderId"]),
            request=request,
        )

    def query_order(self, client_order_id: str) -> dict[str, Any]:
        return self._actor_call(
            kind="query",
            command_id=f"query:{client_order_id}:{time.monotonic_ns()}",
            client_order_id=client_order_id,
        )

    def cancel_order_once(
        self, client_order_id: str, command_id: str
    ) -> dict[str, Any]:
        return self._actor_call(
            kind="cancel",
            command_id=command_id,
            client_order_id=client_order_id,
        )

    def historical_backtest(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._backtest_lock.acquire(blocking=False):
            raise RuntimeError("tqsdk_backtest_busy")
        response: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        worker = threading.Thread(
            target=self._handle_backtest_request,
            args=(request, response),
            name="tqsdk-backtest",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            self._backtest_lock.release()
            raise
        try:
            result = response.get(timeout=125.0)
        except queue.Empty as error:
            raise RuntimeError("tqsdk_backtest_timeout") from error
        if result.get("errorCode"):
            raise RuntimeError(str(result["errorCode"]))
        return result

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        if duration_seconds not in ALLOWED_KLINE_DURATIONS:
            raise ValueError("invalid_market_duration")
        status = self.status()
        if status["dataStatus"] in {"connecting", "unavailable"}:
            raise RuntimeError(
                str(status.get("dataErrorCode") or "sidecar_snapshot_unavailable")
            )
        self._require_session()
        response: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._commands.put(ActorCommand(
            "market",
            f"market:{time.monotonic_ns()}",
            "",
            {"symbol": symbol, "durationSeconds": duration_seconds},
            response,
        ))
        with self._lock:
            self._last_market_request = time.monotonic()
        try:
            result = response.get(timeout=5.0)
        except queue.Empty as error:
            self._note_command_timeout()
            raise RuntimeError("market_request_timeout") from error
        self._note_command_ok()
        error_code = result.get("errorCode")
        if error_code == "symbol_not_found":
            raise KeyError(symbol)
        if error_code:
            raise RuntimeError(str(error_code))
        current_status = self.status()
        quote = result["quote"]
        session_status = str(quote.get("marketSessionStatus") or "unknown")
        quote_status, quote_age = market_data_status(
            str(quote.get("dataStatus") or current_status["dataStatus"])
            if current_status["dataStatus"] in {"normal", "simulated"} else str(current_status["dataStatus"]),
            session_status,
            quote.get("quoteDatetime"),
            self.stale_after_seconds,
        )
        return {
            **result,
            "capturedAt": utc_now(),
            "ageSeconds": quote_age,
            "dataStatus": quote_status,
            "marketSessionStatus": session_status,
            "isCached": quote_status == "stale",
            "liveTrading": "disabled",
        }

    def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        """规则回测的只读历史 K 线（不下单、不改账户），由独立的 history actor（history.py，自己的
        data-only TqApi 与订阅上限）取数，从不进实盘会话的命令队列、不占实盘的 MAX_MARKET_SERIES；
        下载中答 market_series_not_ready，由调用方重试。"""
        if duration_seconds not in ALLOWED_KLINE_DURATIONS:
            raise ValueError("invalid_market_duration")
        if not 1 <= length <= HISTORY_DATA_LENGTH:
            raise ValueError("invalid_history_length")
        catalog = fixture_instrument_catalog() if self.fixture else (self._instruments or [])
        known = {str(item["symbol"]).upper(): str(item["symbol"]) for item in catalog}
        if known and symbol.upper() not in known:
            with self._lock:
                partial = not self.fixture and self._instrument_catalog_error is not None
            if partial:
                # Only the configured contracts are known until the full catalogue loads.
                raise RuntimeError("instrument_catalog_not_ready")
            raise KeyError(symbol)
        actual = known.get(symbol.upper(), symbol)
        if self.fixture:
            item = next(entry for entry in catalog if entry["symbol"] == actual)
            base = 820.0 if item["productId"] == "au" else 3500.0
            result: dict[str, Any] = {
                "schemaVersion": 1, "symbol": actual, "durationSeconds": duration_seconds,
                "priceTick": item["priceTick"], "volumeMultiple": item["volumeMultiple"], "tradingSchedule": None,
                "bars": fixture_history_bars(base, duration_seconds, length, item["priceTick"])}
        else:
            result = self.history.request(actual, duration_seconds, length)
        error_code = result.get("errorCode")
        if error_code == "symbol_not_found":
            raise KeyError(symbol)
        if error_code:
            raise RuntimeError(str(error_code))
        return {**result, "capturedAt": utc_now(), "liveTrading": "disabled"}

    def market_quote(self, symbol: str) -> dict[str, Any]:
        status = self.status()
        if status["dataStatus"] in {"connecting", "unavailable"}:
            raise RuntimeError(
                str(status.get("dataErrorCode") or "sidecar_snapshot_unavailable")
            )
        self._require_session()
        response: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._commands.put(ActorCommand(
            "quote", f"quote:{time.monotonic_ns()}", "", {"symbol": symbol}, response,
        ))
        try:
            result = response.get(timeout=3.0)
        except queue.Empty as error:
            self._note_command_timeout()
            raise RuntimeError("market_quote_timeout") from error
        self._note_command_ok()
        error_code = result.get("errorCode")
        if error_code == "symbol_not_found":
            raise KeyError(symbol)
        if error_code:
            raise RuntimeError(str(error_code))
        quote = result["quote"]
        current_status = self.status()
        session_status = str(quote.get("marketSessionStatus") or "unknown")
        quote_status, quote_age = market_data_status(
            str(current_status["dataStatus"]),
            session_status,
            quote.get("quoteDatetime"),
            self.stale_after_seconds,
        )
        return {
            **quote,
            "price": quote["lastPrice"],
            "capturedAt": utc_now(),
            "ageSeconds": quote_age,
            "dataStatus": quote_status,
            "marketSessionStatus": session_status,
            "isCached": quote_status == "stale",
            "liveTrading": "disabled",
        }

    def _query_command(
        self, kind: str, request: dict[str, Any], *, timeout_seconds: float,
        timeout_code: str,
    ) -> dict[str, Any]:
        """One settlement / cont-history / ranking query on the actor thread.
        These tqsdk queries block until the download lands (their own 30s
        deadline), so the HTTP wait is longer than a quote's."""
        status = self.status()
        if status["dataStatus"] in {"connecting", "unavailable"}:
            raise RuntimeError(
                str(status.get("dataErrorCode") or "sidecar_snapshot_unavailable")
            )
        self._require_session()
        response: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._commands.put(ActorCommand(
            kind, f"{kind}:{time.monotonic_ns()}", "", request, response,
        ))
        try:
            result = response.get(timeout=timeout_seconds)
        except queue.Empty as error:
            self._note_command_timeout()
            raise RuntimeError(timeout_code) from error
        self._note_command_ok()
        error_code = result.get("errorCode")
        if error_code == "symbol_not_found":
            raise KeyError(request.get("symbol", ""))
        if error_code:
            raise RuntimeError(str(error_code))
        return result

    def symbol_settlements(self, symbols: Sequence[str], days: int) -> dict[str, Any]:
        return self._query_command(
            "settlement", {"symbols": [str(item) for item in symbols], "days": days},
            timeout_seconds=10.0, timeout_code="settlement_query_timeout",
        )

    def cont_history(self, symbols: Sequence[str], days: int) -> dict[str, Any]:
        return self._query_command(
            "cont_history", {"symbols": [str(item) for item in symbols], "days": days},
            timeout_seconds=10.0, timeout_code="cont_history_query_timeout",
        )

    def symbol_ranking(self, symbol: str, ranking_type: str, days: int) -> dict[str, Any]:
        return self._query_command(
            "ranking", {"symbol": symbol, "rankingType": ranking_type, "days": days},
            timeout_seconds=10.0, timeout_code="ranking_query_timeout",
        )

    def _age_seconds(self) -> float:
        with self._lock:
            captured = self._captured_monotonic
        return 0.0 if captured is None else max(0.0, time.monotonic() - captured)

    def _set_stage(self, stage: str) -> None:
        with self._lock:
            if stage in {"reconnecting", "waiting_account", "waiting_market", "ready"} or (
                self._snapshot is None and self._status == "connecting"
            ):
                if stage != self._connection_stage and self._startup_total_seconds is None:
                    self._record_stage_duration(time.monotonic())
                self._connection_stage = stage

    def _record_stage_duration(self, now: float) -> None:
        stage = self._connection_stage
        self._startup_durations[stage] = round(
            self._startup_durations.get(stage, 0.0) + now - self._stage_started_monotonic, 3,
        )
        self._stage_started_monotonic = now

    def status(self) -> dict[str, Any]:
        age = self._age_seconds()
        initializing_for = max(0.0, time.monotonic() - self._started_monotonic)
        with self._lock:
            status, error_code = self._status, self._error_code
            stage, snapshot = self._connection_stage, self._snapshot
            has_snapshot = snapshot is not None
            orders_initialized = self._orders_initialized
            catalog_ready = self._instruments is not None
            catalog_error = self._instrument_catalog_error
            catalog_pending = self._instrument_catalog_pending
            startup_durations = dict(self._startup_durations)
            startup_total_seconds = self._startup_total_seconds
            counts = getattr(self._session, "subscription_counts", None) if getattr(self, "_session", None) is not None else None
            session_rebuilds = list(self._session_rebuilds)
        subscription_counts = counts() if callable(counts) else {"marketSeriesCount": 0, "dynamicQuoteCount": 0}
        if has_snapshot and status == "normal" and age > self.stale_after_seconds:
            status = "stale"
        if (
            not has_snapshot
            and status == "connecting"
            and initializing_for > self.initial_snapshot_timeout_seconds
        ):
            status = "unavailable"
            error_code = "tqkq_initial_snapshot_timeout"
            stage = "failed"
        account_status = status
        market_status = status
        session_status = "unknown"
        if snapshot is not None and status in {"normal", "simulated"}:
            sessions = {
                str(item.get("marketSessionStatus") or "unknown")
                for item in snapshot.markets.values()
            }
            if sessions == {"closed"}:
                session_status = "closed"
            elif "trading" in sessions:
                session_status = "trading"
            # Contracts that have a quote decide the feed's freshness; a contract with
            # no quote at all (pre-open) is reported per symbol in marketErrors.
            quoted = [item for item in snapshot.markets.values() if item.get("price") is not None]
            if not quoted and snapshot.markets:
                # Nothing has ever quoted: the feed has delivered no market data yet.
                market_status = "stale"
            else:
                # The feed is down only when every contract in a trading session has
                # stopped ticking. One quiet contract — illiquid or near delivery, book
                # unchanged for minutes mid-session (2026-10-08: SHFE.rb2610 froze for
                # 2+ minutes while au2612/rb2701 kept ticking) — is not an outage; its
                # staleness stays visible per symbol and still gates its own orders.
                trading = [
                    item for item in quoted
                    if str(item.get("marketSessionStatus") or "unknown") == "trading"
                ]
                fresh = any(
                    market_data_status(
                        str(item.get("dataStatus") or status),
                        "trading",
                        item.get("quoteDatetime"),
                        self.stale_after_seconds,
                    )[0] in {"normal", "simulated"}
                    for item in trading
                )
                if trading and not fresh:
                    market_status = "stale"
        capabilities = ([] if self.market_only else [
            "account.read", "positions.read", "orders.read", "trades.read",
        ]) + ["market.read", "market.series.read"]
        capabilities.append("instruments.read")
        capabilities.append("backtest.tqsdk")
        capabilities.extend([
            "settlements.read", "cont_history.read", "rankings.read",
        ])
        if self.trading_enabled:
            capabilities.extend(
                [
                    "simulation_order.submit",
                    "simulation_order.query",
                    "simulation_order.cancel",
                ]
            )
        return {
            "schemaVersion": 1,
            "service": "ai-trader-tqkq-readonly-sidecar",
            "status": status,
            "dataStatus": status,
            "accountStatus": account_status,
            "marketDataStatus": market_status,
            "marketSessionStatus": session_status,
            "marketErrors": {symbol: {"code": item["dataErrorCode"],
                                      "missingFields": item.get("missingFields", [])}
                             for symbol, item in (snapshot.markets.items() if snapshot else [])
                             if item.get("dataErrorCode")},
            "dataErrorCode": error_code,
            "dataConnectionStage": stage,
            **subscription_counts,
            "marketSeriesLimit": MAX_MARKET_SERIES,
            "sessionRebuilds": session_rebuilds,
            "dataInitializationSeconds": round(initializing_for, 3) if not has_snapshot else 0.0,
            "dataStartupTimings": startup_durations,
            "dataStartupTotalSeconds": startup_total_seconds,
            "hasSnapshot": has_snapshot,
            "ageSeconds": round(age, 3),
            "readOnly": not self.trading_enabled,
            "readOnlyData": True,
            "simulationTrading": self.trading_enabled,
            "marketOnly": self.market_only,
            "ordersInitialized": orders_initialized,
            # partial = only the configured contracts are known (the full load failed and is
            # being retried); other contracts answer instrument_catalog_not_ready meanwhile.
            "instrumentCatalogStatus": (
                "partial" if catalog_ready and catalog_error
                else "loading" if catalog_pending
                else "ready" if catalog_ready else "unavailable"
            ),
            "instrumentCatalogErrorCode": catalog_error,
            "liveTrading": "disabled",
            "capabilities": capabilities,
        }

    def _current(self) -> tuple[Snapshot, str, float, dict[str, Any]]:
        """One coherent cut: the snapshot AND the status/age judged at the same instant.
        The actor publishes `_snapshot` / `_captured_monotonic` / `_status` /
        `_orders_initialized` together under this lock, so taking it once across both
        reads is what makes them inseparable — reading the status first and the snapshot
        after (the old shape) let a publish land between them: old activity paired with
        new ordersInitialized (#700 review)."""
        with self._lock:
            status = self.status()          # 先判状态再取快照：同一边界内后取的事实不会更旧
            snapshot = self._snapshot
        if snapshot is None or status["dataStatus"] in {"connecting", "unavailable"}:
            raise RuntimeError(str(status.get("dataErrorCode") or "sidecar_snapshot_unavailable"))
        return snapshot, str(status["dataStatus"]), float(status["ageSeconds"]), status

    def _account_projection(self, snapshot: Snapshot, data_status: str, age: float) -> dict[str, Any]:
        """The /v1/account shape off one given snapshot — shared by the single read and the
        combined read (#700 review), so the two can never drift apart."""
        account = snapshot.account
        equity = number(account.get("balance"))
        position_profit = number(account.get("positionProfit"))
        close_profit = number(account.get("closeProfit"))
        commission = number(account.get("commission"))
        margin = number(account.get("margin"))
        return {
            "accountName": "Sidecar 合成只读快照" if self.fixture else "TqKq 模拟账户",
            "capturedAt": snapshot.captured_at,
            "tradingDay": snapshot.trading_day,
            "ageSeconds": age,
            "dataStatus": data_status,
            "equity": equity,
            # The settlement base: it survives the 15:15 settlement that zeroes the day's
            # P&L fields, so the Runtime can persist it with the daily row.
            "staticBalance": number(account.get("staticBalance")),
            "todayPnl": close_profit + position_profit - commission,
            "todayNetPnl": close_profit + position_profit - commission,
            "floatProfit": number(account.get("floatProfit")),
            "positionProfit": position_profit,
            "closeProfit": close_profit,
            "commission": commission,
            # 剩余可用资金与保证金占用（元）：新手最直接的两笔“钱”。
            "available": number(account.get("available")),
            "margin": margin,
            # 风险度＝保证金占用 ÷ 权益（百分数，与 marginRatio 同口径；越高越接近被强平）。
            "riskRatio": (margin / equity * 100) if equity > 0 else 0.0,
            "marginRatio": (margin / equity * 100) if equity > 0 else 0.0,
            "positionCount": len(snapshot.positions),
        }

    def account(self) -> dict[str, Any]:
        snapshot, data_status, age, _ = self._current()
        return self._account_projection(snapshot, data_status, age)

    def positions(self) -> list[dict[str, Any]]:
        snapshot, _, _, _ = self._current()
        return [dict(item) for item in snapshot.positions]

    def _activity_projection(self, snapshot: Snapshot, data_status: str, age: float,
                             orders_initialized: bool) -> dict[str, Any]:
        """The /v1/activity shape off one given snapshot — shared by the single read and the
        combined read (#700 review)."""
        return {
            "schemaVersion": 1,
            "capturedAt": snapshot.captured_at,
            "ageSeconds": age,
            "dataStatus": data_status,
            "ordersInitialized": orders_initialized,
            "orders": [dict(item) for item in snapshot.orders],
            "trades": [dict(item) for item in snapshot.trades],
            "liveTrading": "disabled",
        }

    def activity(self) -> dict[str, Any]:
        snapshot, data_status, age, status = self._current()
        return self._activity_projection(snapshot, data_status, age,
                                         bool(status["ordersInitialized"]))

    def combined(self) -> dict[str, Any]:
        """One trading fact per read (#700 review): status / account / positions / activity
        projected off ONE snapshot taken under one lock boundary (see `_current`), so a
        Runtime reader (the stop-close chain) can never pair one request's activity with
        another request's positions — nor one cut's activity with the next cut's
        ordersInitialized. The shapes are the same projections the four single reads
        answer with."""
        snapshot, data_status, age, status = self._current()
        return {
            "schemaVersion": 1,
            "capturedAt": snapshot.captured_at,
            "ageSeconds": age,
            "dataStatus": data_status,
            "status": {key: status[key] for key in
                       ("dataStatus", "accountStatus", "marketDataStatus", "ordersInitialized")},
            "account": self._account_projection(snapshot, data_status, age),
            "positions": [dict(item) for item in snapshot.positions],
            "activity": self._activity_projection(snapshot, data_status, age,
                                                  bool(status["ordersInitialized"])),
        }

    def instrument_catalog(self) -> dict[str, Any]:
        with self._lock:
            items = None if self._instruments is None else [dict(item) for item in self._instruments]
            captured_at = self._instrument_catalog_captured_at
            error_code = self._instrument_catalog_error
        if items is None:
            raise RuntimeError(error_code or "instrument_catalog_unavailable")
        status = self.status()
        return {
            "schemaVersion": 1,
            "capturedAt": captured_at,
            "dataStatus": status["dataStatus"],
            "isCached": status["dataStatus"] in {"stale", "unavailable"},
            "catalogStatus": status["instrumentCatalogStatus"],
            "items": items,
        }

    def market(self, symbol: str) -> dict[str, Any]:
        snapshot, data_status, age, _ = self._current()
        requested = symbol.upper()
        result = next((value for key, value in snapshot.markets.items()
                       if key.upper() == requested or key.split(".")[-1].upper() == requested), None)
        if result is None:
            raise KeyError(symbol)
        session_status = str(result.get("marketSessionStatus") or "unknown")
        quote_status, quote_age = market_data_status(
            str(result.get("dataStatus") or data_status) if data_status in {"normal", "simulated"} else data_status,
            session_status,
            result.get("quoteDatetime"),
            self.stale_after_seconds,
        )
        return {
            **result,
            "capturedAt": snapshot.captured_at,
            "ageSeconds": quote_age if math.isfinite(quote_age) else age,
            "dataStatus": quote_status,
            "marketSessionStatus": session_status,
            "isCached": quote_status == "stale",
        }


def _query_int(query: dict[str, list[str]], name: str, *, default: int) -> int | None:
    """A positive-integer query parameter, or None when malformed."""
    raw = str(query.get(name, [str(default)])[0]).strip()
    try:
        return int(raw)
    except ValueError:
        return None


class ReaderGone(Exception):
    """A parked public-market long poll's reader closed its connection (the Runtime retired the
    channel or gave up): the request ends without answering into the closed socket."""


class ReadOnlyHandler(BaseHTTPRequestHandler):
    server: ReadOnlyServer
    # Keep-alive (号池长轮询, 2026-10-14): the Runtime's httpx2 pools reuse a connection instead of
    # opening one — and this server a thread — per read. Every answer carries Content-Length
    # (_json_body). A write request closes its connection after the answer: an early refusal may
    # leave its body unread. An idle kept-alive connection ends when the client drops it (httpx2
    # after 5 s) or after `timeout`, so the client, never this server, closes an idle connection.
    protocol_version = "HTTP/1.1"
    timeout = 60

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            if self.protocol_version != "HTTP/1.1":
                raise
            # A client dropped (reset) a kept-alive connection between requests — a restarting
            # Runtime does it to every idle one. Nothing was in flight: no traceback.

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the sidecar quiet: request paths can contain symbols but never secrets.
        del format, args

    def _json(self, status: int, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        self._json_body(status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())

    def _json_body(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection and self.protocol_version == "HTTP/1.1":
            self.send_header("Connection", "close")  # the client must not reuse this connection
        self.end_headers()
        self.wfile.write(body)

    def _reader_gone(self) -> bool:
        """The client closed its end: readable with nothing to read. A reader never sends another
        request on a connection while its long poll is parked, so readable means closed."""
        connection = self.connection
        try:
            # With a socket timeout set (`timeout`), Python waits for readability before a recv —
            # MSG_DONTWAIT alone would park here: peek in non-blocking mode, then restore it.
            timeout = connection.gettimeout()
            connection.setblocking(False)
            try:
                return connection.recv(1, socket.MSG_PEEK) == b""
            finally:
                connection.settimeout(timeout)
        except (BlockingIOError, InterruptedError):
            return False  # nothing to read: still connected
        except (OSError, ValueError):
            return True

    def _authorized(self, *, public: bool = False) -> bool:
        """This process's own token opens everything. A pool child also accepts
        the pool's shared token, but only on the public market routes (data that
        belongs to no account): its account, positions and orders answer to the
        token of this one spawn alone (2026-10-12 port-reuse review)."""
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not supplied:
            return False
        if hmac.compare_digest(supplied, self.server.token):
            return True
        public_token = getattr(self.server, "public_token", None)
        return public and bool(public_token) and hmac.compare_digest(supplied, public_token)

    def _body(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid_content_length") from error
        if size <= 0 or size > 16 * 1024:
            raise ValueError("invalid_request_size")
        payload = json.loads(self.rfile.read(size))
        if not isinstance(payload, dict):
            raise ValueError("invalid_json_object")
        return payload

    @staticmethod
    def _identifier(value: Any, *, command: bool = False) -> str:
        result = str(value) if isinstance(value, str) else ""
        pattern = r"[A-Za-z0-9:_-]{8,100}" if command else r"[A-Za-z0-9_-]{8,64}"
        if re.fullmatch(pattern, result) is None:
            raise ValueError("invalid_identifier")
        return result

    def _submit_request(self) -> dict[str, Any]:
        payload = self._body()
        required = {
            "commandId",
            "clientOrderId",
            "symbol",
            "direction",
            "offset",
            "volume",
            "priceType",
            "limitPrice",
        }
        if set(payload) != required:
            raise ValueError("invalid_submit_fields")
        command_id = self._identifier(payload["commandId"], command=True)
        client_order_id = self._identifier(payload["clientOrderId"])
        if payload["direction"] not in {"BUY", "SELL"}:
            raise ValueError("invalid_order_direction")
        if payload["offset"] not in {"OPEN", "CLOSE", "CLOSETODAY"}:
            raise ValueError("invalid_order_offset")
        if payload["priceType"] != "LIMIT":
            raise ValueError("limit_order_required")
        if (
            isinstance(payload["volume"], bool)
            or not isinstance(payload["volume"], int)
            or payload["volume"] <= 0
        ):
            raise ValueError("invalid_volume")
        price = finite_number(payload["limitPrice"])
        if price is None or price <= 0:
            raise ValueError("invalid_limit_price")
        market = self.server.service.market_quote(str(payload["symbol"]))
        if market.get("marketSessionStatus") == "closed":
            raise ValueError("market_session_closed")
        exchange = str(market.get("symbol") or "").split(".", 1)[0].upper()
        if payload["offset"] == "CLOSETODAY" and exchange not in {"SHFE", "INE"}:
            raise ValueError("close_today_exchange_required")
        if (
            market.get("dataStatus") not in {"normal", "simulated"}
            or float(market.get("ageSeconds", float("inf")))
            > self.server.service.stale_after_seconds
        ):
            raise ValueError("market_snapshot_stale")
        tick, lower, upper = (
            finite_number(market.get(name))
            for name in (
                "priceTick",
                "lowerLimit",
                "upperLimit",
            )
        )
        if tick is None or lower is None or upper is None or tick <= 0:
            raise ValueError("market_metadata_unavailable")
        steps = price / tick
        if abs(steps - round(steps)) > 1e-8 or not lower <= price <= upper:
            raise ValueError("invalid_limit_price")
        return {
            **payload,
            "commandId": command_id,
            "clientOrderId": client_order_id,
            "symbol": str(market["symbol"]),
            "limitPrice": price,
        }

    def _backtest_request(self) -> dict[str, Any]:
        payload = self._body()
        required = {
            "symbol", "startDate", "endDate", "durationSeconds",
            "baselineThreshold", "candidateThreshold", "initialBalance",
        }
        if set(payload) != required:
            raise ValueError("invalid_backtest_fields")
        raw_symbol = str(payload["symbol"] or "")
        exchange, separator, instrument = raw_symbol.partition(".")
        symbol_hint = f"{exchange.upper()}.{instrument}" if separator else raw_symbol
        requested = symbol_hint.upper()
        if not ACTUAL_FUTURES_SYMBOL.fullmatch(symbol_hint):
            raise ValueError("invalid_backtest_symbol")
        try:
            catalog = self.server.service.instrument_catalog()["items"]
        except RuntimeError:
            catalog = None
        symbol = symbol_hint if catalog is None else next(
            (str(item["symbol"]) for item in catalog
             if str(item.get("symbol") or "").upper() == requested),
            None,
        )
        if symbol is None:
            raise ValueError("invalid_backtest_symbol")
        try:
            start = datetime.strptime(str(payload["startDate"]), "%Y-%m-%d").date()
            end = datetime.strptime(str(payload["endDate"]), "%Y-%m-%d").date()
        except ValueError as error:
            raise ValueError("invalid_backtest_date") from error
        duration = payload["durationSeconds"]
        if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or duration not in BACKTEST_DURATIONS
        ):
            raise ValueError("invalid_backtest_duration")
        days = (end - start).days + 1
        today = datetime.now(timezone(timedelta(hours=8))).date()
        if days < 2 or days > BACKTEST_MAX_DAYS[int(duration)] or end >= today:
            raise ValueError("invalid_backtest_range")
        thresholds = []
        for name in ("baselineThreshold", "candidateThreshold"):
            value = finite_number(payload[name])
            if value is None or not 0.1 <= value <= 5.0:
                raise ValueError("invalid_backtest_threshold")
            thresholds.append(value)
        balance = finite_number(payload["initialBalance"])
        if balance is None or not 100_000 <= balance <= 100_000_000:
            raise ValueError("invalid_backtest_balance")
        return {
            "symbol": symbol,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "durationSeconds": int(duration),
            "baselineThreshold": thresholds[0],
            "candidateThreshold": thresholds[1],
            "initialBalance": balance,
        }

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._authorized(public=path.startswith("/v1/public-market/")):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "sidecar_auth_required"})
            return
        try:
            if path == "/healthz" or path == "/v1/status":
                self._json(HTTPStatus.OK, self.server.service.status())
            elif self.server.service.market_only and path in MARKET_ONLY_REFUSED_PATHS:
                # A market-source child has no trading account to show (its session is data-only).
                self._json(HTTPStatus.CONFLICT, {"error": "market_only_session"})
            elif path == "/v1/account":
                self._json(HTTPStatus.OK, self.server.service.account())
            elif path == "/v1/positions":
                self._json(HTTPStatus.OK, self.server.service.positions())
            elif path == "/v1/activity":
                self._json(HTTPStatus.OK, self.server.service.activity())
            elif path == "/v1/snapshot":
                self._json(HTTPStatus.OK, self.server.service.combined())
            elif path == "/v1/public-market/instruments":
                self._json(HTTPStatus.OK, self.server.service.public_market.instruments())
            elif path == "/v1/public-market/board":
                self._json(HTTPStatus.OK, self.server.service.public_market.board())
            elif path == "/v1/public-market/quotes":
                query = parse_qs(parsed.query)
                symbols = [
                    item.strip() for item in str(query.get("symbols", [""])[0]).split(",")
                    if item.strip()
                ]
                if not symbols or len(symbols) > 20 or any(len(item) > 40 for item in symbols):
                    self._json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": "invalid_quote_symbols"},
                    )
                    return
                self._json(
                    HTTPStatus.OK, self.server.service.public_market.quotes(symbols)
                )
            elif path == "/v1/public-market/calendar":
                self._json(HTTPStatus.OK, self.server.service.public_market.calendar())
            elif path.startswith("/v1/public-market/") and path.endswith("/workbench"):
                query = parse_qs(parsed.query)
                duration = _query_int(query, "duration", default=300)
                wait_ms = _query_int(query, "waitMs", default=0)
                if duration not in ALLOWED_KLINE_DURATIONS or wait_ms is None or wait_ms < 0:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_market_duration"})
                    return
                symbol = unquote(path.removeprefix("/v1/public-market/").removesuffix("/workbench"))
                # Encoded once per view by the service; `barsVersion` (扩容 ②) asks for a delta.
                self._json_body(HTTPStatus.OK, self.server.service.public_market.workbench_body(
                    symbol, duration, after=query.get("after", [None])[0], wait_ms=wait_ms,
                    bars_version=query.get("barsVersion", [None])[0], reader_gone=self._reader_gone,
                ))
            elif path == "/v1/instruments":
                self._json(HTTPStatus.OK, self.server.service.instrument_catalog())
            elif path == "/v1/settlements":
                query = parse_qs(parsed.query)
                symbols = [
                    item.strip() for item in str(query.get("symbols", [""])[0]).split(",")
                    if item.strip()
                ]
                if not symbols or len(symbols) > 10 or any(
                    ACTUAL_FUTURES_SYMBOL.fullmatch(item) is None for item in symbols
                ):
                    self._json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": "invalid_settlement_symbols"},
                    )
                    return
                days = _query_int(query, "days", default=1)
                if days is None or not 1 <= days <= 30:
                    self._json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": "invalid_settlement_days"},
                    )
                    return
                self._json(
                    HTTPStatus.OK, self.server.service.symbol_settlements(symbols, days)
                )
            elif path == "/v1/cont-history":
                query = parse_qs(parsed.query)
                symbols = [
                    item.strip() for item in str(query.get("symbols", [""])[0]).split(",")
                    if item.strip()
                ]
                if not symbols or len(symbols) > 10 or any(
                    CONTINUOUS_FUTURES_SYMBOL.fullmatch(item) is None for item in symbols
                ):
                    self._json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": "invalid_cont_history_symbols"},
                    )
                    return
                days = _query_int(query, "days", default=20)
                if days is None or not 1 <= days <= 200:
                    self._json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": "invalid_cont_history_days"},
                    )
                    return
                self._json(HTTPStatus.OK, self.server.service.cont_history(symbols, days))
            elif path == "/v1/rankings":
                query = parse_qs(parsed.query)
                symbol = str(query.get("symbol", [""])[0]).strip()
                if ACTUAL_FUTURES_SYMBOL.fullmatch(symbol) is None:
                    self._json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": "invalid_ranking_symbol"},
                    )
                    return
                ranking_type = str(query.get("type", ["VOLUME"])[0]).strip().upper()
                if ranking_type not in RANKING_COLUMNS:
                    self._json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": "invalid_ranking_type"},
                    )
                    return
                days = _query_int(query, "days", default=1)
                if days is None or not 1 <= days <= 5:
                    self._json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": "invalid_ranking_days"},
                    )
                    return
                self._json(
                    HTTPStatus.OK,
                    self.server.service.symbol_ranking(symbol, ranking_type, days),
                )
            elif path.startswith("/v1/market/") and path.endswith("/workbench"):
                raw_duration = parse_qs(parsed.query).get("duration", ["300"])[0]
                try:
                    duration = int(raw_duration)
                except ValueError:
                    self._json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": "invalid_market_duration"},
                    )
                    return
                symbol = unquote(path.removeprefix("/v1/market/").removesuffix("/workbench"))
                self._json(
                    HTTPStatus.OK,
                    self.server.service.market_workbench(symbol, duration),
                )
            elif path.startswith("/v1/market/") and path.endswith("/history"):
                query = parse_qs(parsed.query)
                duration = _query_int(query, "duration", default=300)
                length = _query_int(query, "length", default=HISTORY_DATA_LENGTH)
                if duration not in ALLOWED_KLINE_DURATIONS or length is None or not 1 <= length <= HISTORY_DATA_LENGTH:
                    self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "invalid_market_history"})
                    return
                symbol = unquote(path.removeprefix("/v1/market/").removesuffix("/history"))
                self._json(HTTPStatus.OK, self.server.service.market_history(symbol, duration, length))
            elif path.startswith("/v1/market/") and path.endswith("/quote"):
                symbol = unquote(
                    path.removeprefix("/v1/market/").removesuffix("/quote")
                )
                self._json(HTTPStatus.OK, self.server.service.market_quote(symbol))
            elif path.startswith("/v1/market/"):
                self._json(HTTPStatus.OK, self.server.service.market(unquote(path.removeprefix("/v1/market/"))))
            elif path.startswith("/v1/simulation/orders/") and not path.endswith(
                "/cancel"
            ):
                if not self.server.service.trading_enabled:
                    self._json(
                        HTTPStatus.NOT_FOUND, {"error": "simulation_route_not_found"}
                    )
                    return
                client_order_id = self._identifier(
                    unquote(path.removeprefix("/v1/simulation/orders/"))
                )
                self._json(
                    HTTPStatus.OK, self.server.service.query_order(client_order_id)
                )
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "readonly_route_not_found"})
        except ReaderGone:
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            # The reader left while its answer was being written (a retired long poll, a timed-out
            # read): the same as ReaderGone, without a traceback per answer into a closed socket.
            self.close_connection = True
        except KeyError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "symbol_not_found"})
        except ValueError:
            self._json(
                HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "invalid_order_identifier"}
            )
        except RuntimeError as error:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})

    def do_POST(self) -> None:  # noqa: N802
        self.close_connection = True  # see protocol_version: a refused body may be left unread
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "sidecar_auth_required"})
            return
        path = urlparse(self.path).path
        if path.startswith("/v1/replays"):
            try:
                if path == "/v1/replays":
                    result = self.server.service.replays.create(self._body())
                else:
                    parts = path.strip("/").split("/")
                    if len(parts) != 4 or parts[3] not in {"state", "advance", "submit", "query", "cancel", "close"}:
                        raise ValueError("invalid_replay_route")
                    actor = self.server.service.replays.get(parts[2])
                    action = parts[3]
                    # Drain declared bodies even for state/close. Closing a
                    # socket with unread request data can truncate a large reply.
                    body = (self._body() if action not in {"state", "close"}
                            or self.headers.get("Content-Length") not in {None, "0"} else {})
                    if action == "state":
                        result = actor.state()
                    elif action == "close":
                        actor.close()
                        result = {"status": "closed"}
                    else:
                        if action in {"query", "cancel"}:
                            self._identifier(body.get("clientOrderId"))
                        result = actor.call(action, body)
                self._json(HTTPStatus.OK, result)
            except (ValueError, TypeError, KeyError) as error:
                # The one refusal the user can act on (pick an ended day) names itself; every other
                # malformed request stays the generic code.
                reason = str(error) if isinstance(error, ValueError) and str(error) == "replay_requires_completed_day" else None
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY,
                           {"error": "invalid_replay_request", **({"reason": reason} if reason else {})})
            except RuntimeError as error:
                self._json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        if path == "/v1/backtests/tqsdk":
            try:
                self._json(
                    HTTPStatus.OK,
                    self.server.service.historical_backtest(self._backtest_request()),
                )
            except (json.JSONDecodeError, ValueError):
                self._json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {"error": "invalid_tqsdk_backtest_request"},
                )
            except RuntimeError as error:
                self._json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        if not self.server.service.trading_enabled:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_sidecar"})
            return
        try:
            if path == "/v1/simulation/orders/submit":
                self._json(
                    HTTPStatus.OK,
                    self.server.service.submit_order_once(self._submit_request()),
                )
                return
            prefix, suffix = "/v1/simulation/orders/", "/cancel"
            if path.startswith(prefix) and path.endswith(suffix):
                client_order_id = self._identifier(
                    unquote(path[len(prefix) : -len(suffix)])
                )
                payload = self._body()
                if set(payload) != {"commandId"}:
                    raise ValueError("invalid_cancel_fields")
                command_id = self._identifier(payload["commandId"], command=True)
                self._json(
                    HTTPStatus.OK,
                    self.server.service.cancel_order_once(client_order_id, command_id),
                )
                return
            self._json(
                HTTPStatus.METHOD_NOT_ALLOWED, {"error": "simulation_route_not_allowed"}
            )
        except (json.JSONDecodeError, ValueError):
            self._json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"error": "invalid_simulation_order_request"},
            )
        except RuntimeError as error:
            self._json(HTTPStatus.CONFLICT, {"error": str(error)})

    def _unsupported_write(self) -> None:
        self.close_connection = True
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})

    do_PUT = _unsupported_write
    do_PATCH = _unsupported_write
    do_DELETE = _unsupported_write


class ReadOnlyServer(ThreadingHTTPServer):
    daemon_threads = True
    # listen backlog (同 PaperServer #663): the default 5 refused connections when the Runtime opened
    # many at once (10-12 容量测试: 100 users over 64 long polls plus short reads per source).
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], token: str, service: SnapshotService,
                 *, public_token: str | None = None) -> None:
        super().__init__(address, ReadOnlyHandler)
        self.token = token
        self.public_token = public_token
        self.service = service

    def server_bind(self) -> None:
        # HTTPServer.server_bind performs reverse DNS through socket.getfqdn().
        # This localhost-only sidecar neither needs nor trusts DNS for identity,
        # and a slow resolver must not outlive Tauri's readiness timeout.
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


# Account reads a market-only child refuses (it has no TqKq trading session behind them).
MARKET_ONLY_REFUSED_PATHS = frozenset({"/v1/account", "/v1/positions", "/v1/activity", "/v1/snapshot"})


def read_config() -> dict[str, Any]:
    line = sys.stdin.readline(64 * 1024)
    if not line:
        raise RuntimeError("sidecar_config_missing")
    config = json.loads(line)
    required = {"token", "port", "symbols", "instanceId", "parentPid"}
    if not isinstance(config, dict) or not required.issubset(config):
        raise RuntimeError("sidecar_config_invalid")
    if not isinstance(config["token"], str) or len(config["token"]) < 32:
        raise RuntimeError("sidecar_token_invalid")
    public_token = config.get("publicToken")
    if public_token is not None and (not isinstance(public_token, str) or len(public_token) < 32
                                     or public_token == config["token"]):
        raise RuntimeError("sidecar_token_invalid")
    if not isinstance(config["port"], int) or not 0 <= config["port"] <= 65535:
        raise RuntimeError("sidecar_port_invalid")
    if not isinstance(config["instanceId"], str) or len(config["instanceId"]) < 32:
        raise RuntimeError("sidecar_instance_invalid")
    if not isinstance(config["parentPid"], int) or config["parentPid"] in {1} or config["parentPid"] < 0:
        raise RuntimeError("sidecar_parent_invalid")
    if not isinstance(config["symbols"], list) or not config["symbols"]:
        raise RuntimeError("sidecar_symbols_invalid")
    if config.get("mode") not in {"fixture", "tqkq-readonly", "tqkq-simulation"}:
        raise RuntimeError("sidecar_mode_invalid")
    if not isinstance(config.get("marketOnly", False), bool):
        raise RuntimeError("sidecar_market_only_invalid")
    return config


def reexec_allowed(now: float | None = None) -> bool:
    try:
        last = float(os.environ.get(REEXEC_ENV, "0") or 0)
    except ValueError:
        last = 0.0
    return (now if now is not None else time.time()) - last >= REEXEC_MIN_INTERVAL_SECONDS


def reexec_process() -> None:
    """Start this process over in place (all TqApi sessions, threads and loops from scratch); the
    environment carries the time so a persistent upstream failure cannot loop re-execs."""
    os.environ[REEXEC_ENV] = str(time.time())
    os.execv(sys.executable, sys.orig_argv)


def consume_credentials(config: dict[str, Any]) -> tuple[str | None, str | None]:
    username = config.pop("username", None)
    password = config.pop("password", None)
    return (
        username if isinstance(username, str) and username else None,
        password if isinstance(password, str) and password else None,
    )


def serve(config: dict[str, Any]) -> int:
    parent_pid = int(config["parentPid"])
    if parent_pid > 1:
        start_parent_watchdog(parent_pid)
    username, password = consume_credentials(config)
    service = SnapshotService(
        username=username,
        password=password,
        symbols=[str(item) for item in config["symbols"]],
        fixture=config.get("mode") == "fixture",
        trading_enabled=config.get("mode") == "tqkq-simulation",
        market_only=bool(config.get("marketOnly", False)),
        stale_after_seconds=float(config.get("staleAfterSeconds", 15.0)),
        initial_snapshot_timeout_seconds=float(config.get("initialSnapshotTimeoutSeconds", 180.0)),
        reexec_on_wedge=bool(config.get("reexecOnWedge", False)),
    )
    del username, password
    server = ReadOnlyServer(("127.0.0.1", int(config["port"])), str(config["token"]), service,
                            public_token=config.get("publicToken"))
    actual_port = int(server.server_address[1])
    stop = threading.Event()

    def shutdown(*_: Any) -> None:
        if not stop.is_set():
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    service.start()
    # Readiness contains no token or credential and is consumed by the native parent.
    print(json.dumps({
        "status": "ready", "port": actual_port,
        "instanceId": config["instanceId"], "liveTrading": "disabled",
    }), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        service.stop()
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Trader TqKq read-only sidecar")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--config-stdin", action="store_true")
    mode.add_argument("--diagnose-startup", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.diagnose_startup:
            from .startup_diagnostics import profile_sdk_import
            print(json.dumps(profile_sdk_import(), ensure_ascii=False), flush=True)
            return 0
        return serve(read_config())
    except Exception as error:
        print(json.dumps({"status": "failed", "error": type(error).__name__}), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    # `python -m tqkq_readonly_sidecar.main` (the pool's account children) loads this file as
    # __main__, while public_market imports tqkq_readonly_sidecar.main: two copies of every class.
    # A ReaderGone raised by public_market then escaped do_GET's `except ReaderGone` (the __main__
    # copy) and printed a traceback per retired long poll (2026-10-13). Run the canonical module.
    from tqkq_readonly_sidecar.main import main as canonical_main

    raise SystemExit(canonical_main())

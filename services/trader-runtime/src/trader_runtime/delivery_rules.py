"""交割月与到期提醒的纯函数（2026-10-02）。

个人账户不能持仓进交割月：期货公司会在交割月第一个交易日前强平。股指期货
（CFFEX 的 IF/IH/IC/IM）是现金交割，个人账户可以持到最后交易日，到期自动结算。

`delivery_notice(item, today, trading_days)` 读目录里的一个合约、今天的交易日与交易所日历
已知的交易日，返回 ``{kind, level, lastHoldDay, tradingDaysLeft, estimated, text}`` 或 None：

- 股指现金交割 → ``kind="expiry"``，lastHoldDay＝交割月第三个周五（最后交易日，遇节假日顺延
  到下一交易日）。
- 其他商品/国债 → ``kind="delivery_month"``，lastHoldDay＝交割月前一个月的最后交易日
  （个人账户最后能持仓的日子，收盘前须平仓）。
- 已进交割月 → danger、tradingDaysLeft 0；剩余交易日（含今天）≤3 → danger；
  ≤10 → warning；更远不提醒。合约过期或日期缺失不提醒。

交易日（2026-10-12）：与过期单、补录成交同一份交易所日历（``exchange_calendar``，sidecar 的
TqSdk 日历，节假日已扣）。日历覆盖不到的那段（日历读不到、或 sidecar 还没送来后面的交易日）
按周一到周五计，``estimated`` 为 True，界面与 AI 说“按工作日估算”——长假前后实际天数会更少。
只提醒，不代替平仓，也不构成任何下单限制。

结果缓存（2026-10-13 第二轮省 CPU）：共享目录每 2 秒给全部合约（约 900 个）各算一次提醒，
`_trading_days_left` 按天循环到死线（远月一年多），夜盘 60 读者档占 Runtime 一成。现在一份日历
（同一组交易日）只建一次 `_Calendar`（``functools.lru_cache``），提醒按 (现金交割?, 交割年月,
今天) 记在这份日历上：跨日换“今天”就是新键，日历更新（sidecar 送来新交易日）就是新日历、新记录。
同一键的结果与不缓存时逐字节相同；每次返回一份副本，调用方改了也不影响下一位。
"""
from __future__ import annotations

import functools
import time
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from .trading_calendar import CME_CLOSED_DAYS, NS, trading_day_of

# 中金所股指期货：现金交割，个人账户可持到最后交易日（交割月第三个周五）。
CASH_SETTLED_PRODUCTS = {"IF", "IH", "IC", "IM"}
WARNING_TRADING_DAYS = 10
DANGER_TRADING_DAYS = 3
# 一份日历上记多少条提醒结果（键 = 现金交割?, 交割年月, 今天）；全站约几百个交割年月，满了整份清掉重来。
MEMO_LIMIT = 4096
_MISSING: Any = object()


def trading_today(now: float | None = None, trading_days: Sequence[str] | None = None) -> date:
    """今天的交易日（≥18:00 算下一交易日，周末与日历里的节假日顺延，与 trading_calendar 同一口径）。"""
    return date.fromisoformat(trading_day_of(int((time.time() if now is None else now) * NS), trading_days))


class _Calendar:
    """交易所日历已知的交易日（完整区间：区间内没列出的工作日就是节假日）；区间外按工作日。"""

    def __init__(self, trading_days: Sequence[str] | None) -> None:
        days = sorted(str(day) for day in trading_days or [])
        self.known = frozenset(days)
        self.first = date.fromisoformat(days[0]) if days else None
        self.last = date.fromisoformat(days[-1]) if days else None
        self.memo: dict[tuple[bool, int, int, date], dict[str, Any] | None] = {}

    def covers(self, day: date) -> bool:
        return self.first is not None and self.last is not None and self.first <= day <= self.last

    def is_trading(self, day: date) -> bool:
        return day.isoformat() in self.known if self.covers(day) else day.weekday() < 5


@functools.lru_cache(maxsize=8)
def _calendar(trading_days: tuple[str, ...]) -> _Calendar:
    """同一组交易日共用一份日历（与其上的提醒记录）；日历一变就是另一份。"""
    return _Calendar(trading_days)


def _month_shift(year: int, month: int, offset: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + offset
    return total // 12, total % 12 + 1


def _last_trading_day(year: int, month: int, calendar: _Calendar) -> tuple[date, bool]:
    """该月自己的最后一个交易日（月末逢周末/节假日向前挪），与它是否按工作日估算。"""
    day = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    estimated = not calendar.covers(day)
    while not calendar.is_trading(day):
        day -= timedelta(days=1)
    return day, estimated or not calendar.covers(day)


def _third_friday(year: int, month: int, calendar: _Calendar) -> tuple[date, bool]:
    """交割月第三个周五（中金所最后交易日，遇节假日顺延到下一交易日），与它是否按工作日估算。"""
    day = date(year, month, 1)
    day = day + timedelta(days=(4 - day.weekday()) % 7) + timedelta(days=14)
    estimated = not calendar.covers(day)
    while not calendar.is_trading(day):
        day += timedelta(days=1)
    return day, estimated or not calendar.covers(day)


def _trading_days_left(today: date, deadline: date, calendar: _Calendar) -> tuple[int, bool]:
    """today 到 deadline（含两端）之间的交易日数（deadline 早于 today 时为 0），与其中是否有
    日历覆盖不到、按工作日估算的日子。"""
    if deadline < today:
        return 0, False
    count, estimated, day = 0, False, today
    while day <= deadline:
        estimated = estimated or not calendar.covers(day)
        count += calendar.is_trading(day)
        day += timedelta(days=1)
    return count, estimated


def _delivery_period(item: Mapping[str, Any], today: date) -> tuple[int, int] | None:
    """合约的交割年月：目录字段优先（主连/回放数据里常为 0），缺失时从合约代码尾部解析。"""
    year = item.get("deliveryYear")
    month = item.get("deliveryMonth")
    if isinstance(year, int) and isinstance(month, int) and 2000 < year < 2100 and 1 <= month <= 12:
        return year, month
    code = str(item.get("instrumentId") or item.get("symbol") or "")
    digits = ""
    for char in reversed(code):
        if not char.isdigit():
            break
        digits = char + digits
    if len(digits) == 4:  # YYMM，如 rb2610 → 2026-10
        year, month = 2000 + int(digits[:2]), int(digits[2:])
    elif len(digits) == 3:  # YMM（郑商所），如 CF610 → 2026-10：年份只有个位，取离今天最近的那个十年
        year, month = today.year - today.year % 10 + int(digits[0]), int(digits[1:])
        if year < today.year - 1:
            year += 10
    else:
        return None
    if 1 <= month <= 12:
        return year, month
    return None


def delivery_notice(item: Mapping[str, Any], today: date,
                    trading_days: Sequence[str] | None = None) -> dict[str, Any] | None:
    """目录合约的交割/到期提醒；不到提醒窗口或日期不可知时为 None。``trading_days`` 是交易所日历
    已知的交易日（``exchange_calendar``）；缺失或覆盖不到的部分按工作日估算（``estimated``）。"""
    period = _delivery_period(item, today)
    if period is None:
        return None
    year, month = period
    days = trading_days if isinstance(trading_days, tuple) else tuple(trading_days or ())
    calendar = _calendar(days)
    exchange = str(item.get("exchangeId") or "").upper()
    product = str(item.get("productId") or "").upper()
    cash_settled = exchange == "CFFEX" and product in CASH_SETTLED_PRODUCTS
    key = (cash_settled, year, month, today)
    notice = calendar.memo.get(key, _MISSING)
    if notice is _MISSING:
        notice = _notice(cash_settled, year, month, today, calendar)
        if len(calendar.memo) >= MEMO_LIMIT:
            calendar.memo.clear()
        calendar.memo[key] = notice
    return dict(notice) if notice is not None else None


def _notice(cash_settled: bool, year: int, month: int, today: date,
            calendar: _Calendar) -> dict[str, Any] | None:
    if cash_settled:
        deadline, deadline_estimated = _third_friday(year, month, calendar)  # 最后交易日，到期现金交割
        if today > deadline:
            return None  # 已到期
        days, estimated = _trading_days_left(today, deadline, calendar)
        if days > WARNING_TRADING_DAYS:
            return None
        return {"kind": "expiry", "level": "danger" if days <= DANGER_TRADING_DAYS else "warning",
                "lastHoldDay": deadline.isoformat(), "tradingDaysLeft": days,
                "estimated": estimated or deadline_estimated,
                "text": f"{deadline.month} 月 {deadline.day} 日到期，现金交割"}
    # 交割月前一个月最后交易日
    deadline, deadline_estimated = _last_trading_day(*_month_shift(year, month, -1), calendar)
    if today > date(*_month_shift(year, month, 1), 1) - timedelta(days=1):
        return None  # 交割月都已过完：合约已了结
    if today > deadline:
        return {"kind": "delivery_month", "level": "danger", "lastHoldDay": deadline.isoformat(),
                "tradingDaysLeft": 0, "estimated": deadline_estimated,
                "text": "已进入交割月，个人账户不能持仓，应尽快平仓"}
    days, estimated = _trading_days_left(today, deadline, calendar)
    if days > WARNING_TRADING_DAYS:
        return None
    return {"kind": "delivery_month", "level": "danger" if days <= DANGER_TRADING_DAYS else "warning",
            "lastHoldDay": deadline.isoformat(), "tradingDaysLeft": days,
            "estimated": estimated or deadline_estimated,
            "text": (f"个人账户须在 {deadline.month} 月 {deadline.day} 日收盘前平仓，"
                     f"之后进入交割月会被强平")}


def foreign_expiry_notice(expire_day: date, today: date) -> dict[str, Any] | None:
    """美元子账户外盘月份合约的到期提醒（外盘代码统一，2026-10-21）：最后交易日（MT5 合约表的到期时间，美东日期）
    前 10 个 CME 交易日内 warning、3 个以内 danger；到期后外盘撮合引擎按结算价了结。CME 交易日 = 周一到周五去掉
    全天休市日（trading_calendar.CME_CLOSED_DAYS）。已过期为 None。"""
    if today > expire_day:
        return None
    days, day = 0, today
    while day <= expire_day:
        days += day.weekday() < 5 and day.isoformat() not in CME_CLOSED_DAYS
        day += timedelta(days=1)
    if days > WARNING_TRADING_DAYS:
        return None
    return {"kind": "expiry", "level": "danger" if days <= DANGER_TRADING_DAYS else "warning",
            "lastHoldDay": expire_day.isoformat(), "tradingDaysLeft": days, "estimated": False,
            "text": f"{expire_day.month} 月 {expire_day.day} 日到期（最后交易日），到期按结算价了结；要继续持有请先换月"}


def contract_payload(quote: Mapping[str, Any]) -> dict[str, Any]:
    """合约信息卡与 AI 回执里的“一手 / 资金 / 时间”数据（缺数据为 None，界面显示 —）。"""
    def finite(key: str) -> float | None:
        value = quote.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    return {"multiplier": finite("volumeMultiple"), "priceTick": finite("priceTick"),
            "marginPerLot": finite("marginPerLot"), "commissionPerLot": finite("commissionPerLot"),
            "tradingSchedule": quote.get("tradingSchedule") if isinstance(quote.get("tradingSchedule"), dict) else None}

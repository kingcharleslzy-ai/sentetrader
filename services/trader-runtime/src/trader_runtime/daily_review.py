"""今日复盘 (2026-10-06, owner-approved batch 3): after the day session the Runtime tells the user
about the day — every order of the trading day as one 笔 with its time, action, reason and tag,
the closes' realized P&L and a two-to-three sentence summary. 交易验尸 (2026-10-25) adds each 笔's checks
against the user's own plan and a question to answer into its 补充 (`trade_autopsy`), and on the week's last
trading day 本周最该改的一件事 in place of the daily 习惯 observation.

Tags are the owner's rule, decided from the fill's source and the reminders' times, never by a
model (`tag_for`, in this priority): ① a strategy signal, a stop-loss / take-profit or conditional
order, or the AI's own autonomous order → 计划内; ② otherwise, when a Sente reminder about the same
product (盘中异动, 到价, 收盘前持仓, 风险度 — 风险度 covers the whole account) came within the 15
minutes before the order was placed, the user placed it themselves → 提醒后 (a card the AI wrote at
their word or from a reminder card's 平一半 included); ③ otherwise an AI card the user confirmed →
计划内; ④ everything else the user placed by hand → 临时. The night session belongs to the next
trading day (the provider's own tradingDay on every fill); figures are the account's fills, the
pairing's realized P&L and the account's own day P&L.

The summary is one bounded model call on the user's own model and quota (the tier's model, the
daily token cap, usage booked to the user — as the chat suggestions do), fed only the day's facts,
the reminders of the time, the cards' reasons, the checks and the user's 约定; it also judges the entry of a
笔 whose setup is written only in words. What comes back is checked like the research desk's judgment: every
number must appear in the facts; no forecast and no trading advice. Anything that fails — or no model at all —
falls back to the deterministic sentence (“今天 N 笔里 M 笔在计划内”) and leaves that entry unjudged. The
week's lesson is the program's (the check that came up most) and reaches the memory only when the user says 记住.

Generated at 15:15 Beijing time for every owner of an assembled account that has fills that day
(the loop polls once a minute and runs only on the instance-lease holder). A USD sub-account
(``paperusd:``, 外盘本站模拟) trades on CME's day instead: its review of a CME trading day comes at 17:15
New York time that day, after CME's 17:00 close (05:15 / 06:15 Beijing the next morning, by US daylight
saving) — never at the Beijing 15:15 mid-session. Its amounts are in 美元 and its fees the foreign
engine's per-lot USD fee (``account_trade_review``). Before the model is called
the run claims the review (a push-notice claim row; a claim older than GENERATION_CLAIM_SECONDS is
taken over), so it is generated and saved once, and the delivery sends exactly that saved text: a
saved 15:15 review that was not delivered yet (a handoff, a failed delivery) is delivered as saved,
never regenerated. "No fills" is only concluded from current account activity (fresh, orders
initialized) and is never final for the day: a later round looks again. The review's `pushed_at`
makes the delivery once per user × account × trading day across restarts and blue-green slots;
重新生成 recomputes and overwrites, never pushes.
Delivery follows 通知哪些事 · 收盘复盘 (notify.dailyReview): a message + card in the latest
conversation, the feed.reminder event, the lock-screen push. A live voice call is not interrupted.
It never trades and never advises a trade."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from . import trade_autopsy, user_memory
from .conversation_cards import reminder_card, ui_action
from .instruments import account_currency, is_foreign_paper, money_unit
from .review_tags import NS, TAG_LABELS, TAG_RULES, Reminder, product_key, tag_for
from .trading_calendar import CME_CLOSED_DAYS, NEW_YORK, cme_trading_day

log = logging.getLogger("uvicorn.error.daily_review")

CHINA = ZoneInfo("Asia/Shanghai")
GENERATE_AT = clock_time(15, 15)
# The USD sub-account's review (外盘盘点 3A, 2026-10-21): CME closes at 17:00 New York; a quarter past it,
# as the domestic 15:15 follows the 15:00 close.
CME_GENERATE_AT = clock_time(17, 15)
VERSION = 1
LOOP_SECONDS = 60.0
MODEL_TIMEOUT_SECONDS = 30
HISTORY_REVIEWS = 5
# 结算后更正 looks back this many reviews per account (a weekend or a holiday between a day and its settled row).
SETTLE_LOOKBACK = 3
MODEL_LEGS = 40
MANUAL_MIN_SECONDS = 30
# A generation claim older than this belongs to a process that died mid-call: the next round takes it over.
GENERATION_CLAIM_SECONDS = 10 * 60
REMINDER_LABELS = {"movement": "盘中异动", "price_alert": "到价提醒", "market_close": "收盘前持仓提醒", "risk": "风险度提醒"}
SOURCE_LABELS = {"manual": "手动", "card": "AI 出卡", "ai": "AI 自主", "stop": "止损止盈", "liquidation": "强平"}
OPEN_SIDES = {"BUY": "买开", "SELL": "卖开"}
CLOSE_SIDES = {"SELL": "平多", "BUY": "平空"}
NO_REASON = "没有记下理由"

NUMBER = re.compile(r"\d+(?:\.\d+)?")
CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_COUNT = re.compile(r"([零一二两三四五六七八九十百千]+)(?=笔|手|次|个|单|元|块|点|跳|天|分钟)")
# No trading advice and no forecast in a review (the prompt says so; this is the backstop).
ADVICE = re.compile(r"(建议|应该|应当|可以考虑|可考虑|不妨|最好|下次|明天|接下来|之后可以)[^。；，]{0,10}"
                    r"(买|卖|开仓|平仓|加仓|减仓|做多|做空|止损|止盈|持有|离场|进场|入场)")
FORECAST = re.compile(r"预计|预期|有望|将会|目标价|会涨|会跌|能赚|还会")

INSTRUCTIONS = """你在为一位期货交易者写今天的收盘复盘（交易验尸）。输入是这个交易日按委托聚合的成交 legs（每笔有 id、时间、动作、标签 计划内/提醒后/临时、理由、平仓盈亏、程序查出的问题 checks，开仓笔可能带他的交易笔记 playbook）、当时的 Sente 提醒 reminders、他给 AI 定的约定 rules，以及当天的统计 stats。
summary：两三句中文、不超过 120 个字，说几笔在计划内、哪笔不在计划里（提醒后或临时）以及当时的理由、checks 里查出的问题（提前离场、连亏后再开、亏过止损等）、有没有违背他的约定；只用输入里的事实，数字必须原样出现在输入里，不要自己计算新的数字（不要算合计、比例、百分比）。写给人看：不要出现输入里的英文字段名（planned、wins、stats、checks 等）和成交编号（L1、L2），提到某笔就说时间和动作，如“13:44 买开 2 手螺纹”。
不写收益预测，不给任何交易建议：不说以后、明天、下次应该怎么买卖、开平仓、加减仓或设止损止盈。
entryFits（可选）：只对带 playbook 的开仓笔，按交易笔记的入场规则 entryRules 判断这笔入场合不合规则：leg 是这笔的 id，fits 是 true/false，why 一句话（不超过 40 个字）引用这笔的理由、时间或提醒说明依据，数字只能用输入里的；看不出来就不要写这笔。"""


# -- reading the account -----------------------------------------------------------------------------

def _ns(value: Any) -> int | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * NS)


def load_reminders(store: Any, *, user_id: str, account_id: str) -> list[Reminder]:
    """Every reminder the tag rule may cite: 收盘前 / 风险度 of this account (reminder_notices), the user's
    盘中异动 (the push_notices ledger) and triggered 到价提醒 (price_alerts — quotes have no account)."""
    found: list[Reminder] = []
    with store.lock:
        notices = store.connection.execute(
            "SELECT kind, text, created_at, symbols FROM reminder_notices WHERE user_id = ? AND account_id = ?",
            (user_id, account_id)).fetchall()
        movements = store.connection.execute(
            "SELECT key, created_at, text FROM push_notices WHERE user_id = ? AND key LIKE 'market:%'",
            (user_id,)).fetchall()
        alerts = store.connection.execute(
            "SELECT symbol, name, direction, price, triggered_at FROM price_alerts"
            " WHERE user_id = ? AND status = 'triggered' AND triggered_at IS NOT NULL", (user_id,)).fetchall()
    for row in notices:
        at = _ns(row["created_at"])
        if at is None:
            continue
        kind = "risk" if row["kind"] == "risk_ratio" else "market_close"
        symbols = [item for item in str(row["symbols"] or "").split(",") if item]
        products = frozenset(product_key(item) for item in symbols) if kind == "market_close" and symbols else None
        found.append(Reminder(at, kind, str(row["text"] or REMINDER_LABELS[kind]), products))
    for row in movements:
        at = _ns(row["created_at"])
        parts = str(row["key"]).split(":")
        if at is None or len(parts) < 2:
            continue
        found.append(Reminder(at, "movement", str(row["text"] or REMINDER_LABELS["movement"]),
                              frozenset({product_key(parts[1])})))
    for row in alerts:
        at = _ns(row["triggered_at"])
        if at is None:
            continue
        verb = "涨到" if row["direction"] == "above" else "跌到"
        found.append(Reminder(at, "price_alert", f"到价提醒：{row['name'] or row['symbol']} {verb} {readable_prices(str(row['price']))}",
                              frozenset({product_key(row["symbol"])})))
    return sorted(found, key=lambda item: item.at_ns)


def order_placed_times(store: Any, user_id: str) -> dict[str, int]:
    """clientOrderId → when the app sent it (the order link's creation); an order placed outside the app
    has none and is judged at its first fill."""
    with store.lock:
        rows = store.connection.execute(
            "SELECT client_order_id, created_at FROM tqkq_sim_order_links WHERE user_id = ?", (user_id,)).fetchall()
    placed = {}
    for row in rows:
        at = _ns(row["created_at"])
        if at is not None:
            placed[str(row["client_order_id"])] = at
    return placed


def _clock(ns: int) -> str:
    return datetime.fromtimestamp(ns / NS, UTC).astimezone(CHINA).strftime("%H:%M")


def price_text(value: float) -> str:
    return f"{value:,.0f}" if value == int(value) else f"{value:,.4f}".rstrip("0").rstrip(".")


def money_text(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:+,.0f}" if round(value) == value else f"{value:+,.2f}"


def due_review_day(account_id: str, now: datetime) -> str | None:
    """The trading day whose review is due on this account at `now`, or None before its time: a domestic
    account's is today's (Beijing) from 15:15; a USD sub-account's is the CME trading day that closed at
    17:00 New York, from 17:15 New York (its own date — the evening after 18:00 already trades the next
    CME day, but the review is still that day's)."""
    if is_foreign_paper(account_id):
        local = now.astimezone(NEW_YORK)
        return local.date().isoformat() if local.time() >= CME_GENERATE_AT else None
    local = now.astimezone(CHINA)
    return local.date().isoformat() if local.time() >= GENERATE_AT else None


def current_trading_day(account_id: str, now: datetime) -> str:
    """The account's trading day at `now` as the review names it: the CME trading day for a USD sub-account
    (18:00 New York rolls it), the Beijing date for the rest (重新生成 without a day, the current-read check)."""
    if is_foreign_paper(account_id):
        return cme_trading_day(int(now.timestamp() * NS))
    return now.astimezone(CHINA).date().isoformat()


def generate_at_text(account_id: str, now: datetime) -> str:
    """When the account's review is generated, in Beijing time: 15:15, or for a USD sub-account 17:15 New York
    as Beijing reads it today (05:15 in US summer time, 06:15 in winter)."""
    if not is_foreign_paper(account_id):
        return GENERATE_AT.strftime("%H:%M")
    local = now.astimezone(NEW_YORK)
    moment = datetime.combine(local.date(), CME_GENERATE_AT, tzinfo=NEW_YORK)
    return moment.astimezone(CHINA).strftime("%H:%M")


def review_money(value: float | None, account_id: str) -> str | None:
    """The review's amount as the card and the push show it: unchanged (no unit) on a domestic account,
    “+90 美元” on a USD sub-account."""
    text = money_text(value)
    currency = account_currency(account_id)
    return f"{text} {money_unit(currency)}" if text is not None and currency != "CNY" else text


# A price inside a reason (“保护性止盈触发：3188”): four or more integer digits that are not part of a
# contract code (RB2701), a clock or a date.
_PRICE_IN_TEXT = re.compile(r"(?<![A-Za-z\d.,:/-])\d{4,}(?:\.\d+)?(?![\d:/-])")


def readable_prices(text: str) -> str:
    """Prices in a shown line with thousands separators, as every other figure on the page (3,188). The
    checks read “3,188” and “3188” as the same number, so nothing a summary may cite changes."""
    return _PRICE_IN_TEXT.sub(lambda match: price_text(float(match.group(0))), text)


def _reason(info: dict[str, Any], tag: str, reminder: Reminder | None) -> str:
    parts = []
    if tag == "remind" and reminder is not None:
        parts.append(f"{_clock(reminder.at_ns)} {REMINDER_LABELS.get(reminder.kind, '提醒')}后")
    source = str(info.get("source") or "manual")
    reason = str(info.get("reason") or "").strip()
    if source == "strategy":
        parts.append(f"策略「{info.get('strategyName') or '策略'}」的信号")
    elif reason:
        parts.append(readable_prices(reason[:80]))
    return " · ".join(parts) or NO_REASON


def build_legs(fills: list[dict[str, Any]], *, trading_day: str, sources: dict[str, dict[str, Any]],
               placed: dict[str, int], reminders: list[Reminder], rounds: list[dict[str, Any]],
               names: dict[str, str]) -> list[dict[str, Any]]:
    """The trading day's fills as 笔: one per order (fills of one clientOrderId together — partial fills
    are one 笔 at their volume-weighted price), in order of the first fill, each tagged and explained.
    A close carries the realized P&L of the round trips its fills closed (net when the fee schedule
    gives it, else gross)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    for fill in sorted(fills, key=lambda item: (int(item["tradeDateTime"]), str(item["tradeId"]))):
        if fill.get("tradingDay") != trading_day or str(fill["tradeId"]) in seen:
            continue
        seen.add(str(fill["tradeId"]))
        key = str(fill.get("clientOrderId") or "") or f"trade:{fill['tradeId']}"
        groups.setdefault(key, []).append(fill)
    legs: list[dict[str, Any]] = []
    for index, (key, group) in enumerate(groups.items(), start=1):
        first = group[0]
        symbol, direction, offset = str(first["symbol"]), str(first.get("direction") or "").upper(), str(first.get("offset") or "")
        volume = sum(int(item["volume"]) for item in group)
        price = sum(float(item["price"]) * int(item["volume"]) for item in group) / volume if volume else float(first["price"])
        at = int(first["tradeDateTime"])
        order_id = str(first.get("clientOrderId") or "")
        info = sources.get(order_id) or {"source": "manual"}
        source = str(info.get("source") or "manual")
        placed_at = min(placed.get(order_id, at), at)
        tag, reminder = tag_for(source=source, placed_ns=placed_at, symbol=symbol, reminders=reminders)
        trade_ids = [str(item["tradeId"]) for item in group]
        closing = offset != "OPEN"
        closed = [item for item in rounds if str(item.get("closeTradeId") or "") in trade_ids] if closing else []
        pnl = round(sum(float(item["net"] if item.get("net") is not None else item["gross"]) for item in closed), 2) if closed else None
        side = (CLOSE_SIDES if closing else OPEN_SIDES).get(direction, "成交")
        name = names.get(symbol.upper(), "")
        code = symbol.split(".", 1)[-1].upper()
        legs.append({
            "id": f"L{index}", "orderId": key, "tradeIds": trade_ids, "symbol": symbol, "name": name,
            "time": datetime.fromtimestamp(at / NS, UTC).astimezone(CHINA).isoformat(timespec="minutes"),
            "clock": _clock(at), "side": side, "volume": volume, "price": round(price, 4),
            "action": " ".join(part for part in (side, name, code) if part) + f" × {volume} @ {price_text(round(price, 4))}",
            "source": source, "sourceLabel": str(info.get("strategyName") or SOURCE_LABELS.get(source, source)),
            "tag": tag, "tagLabel": TAG_LABELS[tag], "reason": _reason(info, tag, reminder),
            "reminder": ({"kind": reminder.kind, "text": reminder.text[:160], "clock": _clock(reminder.at_ns)}
                         if reminder is not None else None),
            "pnl": pnl, "pnlBasis": (None if not closed else "net" if all(item.get("net") is not None for item in closed) else "gross"),
            "approximate": any(bool(item.get("approximate")) for item in group),
        })
    return legs


def leg_stats(legs: list[dict[str, Any]]) -> dict[str, int]:
    closes = [leg for leg in legs if leg["pnl"] is not None]
    return {"legs": len(legs), "planned": sum(leg["tag"] == "plan" for leg in legs),
            "remind": sum(leg["tag"] == "remind" for leg in legs), "adhoc": sum(leg["tag"] == "adhoc" for leg in legs),
            "closes": len(closes), "wins": sum(leg["pnl"] > 0 for leg in closes)}


def template_summary(stats: dict[str, int]) -> str:
    """The deterministic sentence (no model, or a model answer that did not pass the checks)."""
    text = f"今天 {stats['legs']} 笔里 {stats['planned']} 笔在计划内"
    if stats["remind"]:
        text += f"，{stats['remind']} 笔是提醒后下的"
    if stats["adhoc"]:
        text += f"，{stats['adhoc']} 笔临时"
    return text + "。"


def annotate_round_tags(store: Any, *, user_id: str, account_id: str | None, rounds: list[dict[str, Any]],
                        sources: dict[str, dict[str, Any]] | None = None) -> None:
    """平仓记录 (2026-10-06): each round trip's 开 and 平 legs with the same tags as the 今日复盘, in place."""
    if not rounds:
        return
    if sources is None:
        from .strategy_workbench import order_sources
        sources = order_sources(store, user_id)
    reminders = load_reminders(store, user_id=user_id, account_id=account_id or store.default_trading_account_id)
    placed = order_placed_times(store, user_id)
    for trade in rounds:
        for side, order_key, time_key in (("open", "openOrderId", "openTime"), ("close", "closeOrderId", "closeTime")):
            order_id = str(trade.get(order_key) or "")
            at = int(trade[time_key])
            info = sources.get(order_id) or {"source": "manual"}
            tag, _ = tag_for(source=str(info.get("source") or "manual"), placed_ns=min(placed.get(order_id, at), at),
                             symbol=str(trade["symbol"]), reminders=reminders)
            trade[f"{side}Tag"] = tag


# -- checking the model's answer ----------------------------------------------------------------------

def _cn_number(text: str) -> int | None:
    if all(char in CN_DIGITS for char in text):
        return int("".join(str(CN_DIGITS[char]) for char in text)) if len(text) > 1 else CN_DIGITS[text]
    total, current = 0, 0
    for char in text:
        if char in CN_DIGITS:
            current = CN_DIGITS[char]
        elif char in "十百千":
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            current = 0
        else:
            return None
    return total + current


def numbers_in(text: str) -> set[float]:
    """Every number a sentence states: digits (thousands separators ignored) and Chinese counts before a
    measure word (“三笔”“两手”)."""
    values = {float(value) for value in NUMBER.findall(str(text).replace(",", ""))}
    for match in CN_COUNT.finditer(str(text)):
        value = _cn_number(match.group(1))
        if value is not None:
            values.add(float(value))
    return values


def fact_numbers(facts: Any) -> set[float]:
    """The numbers the model was given (any value anywhere in the facts, read as text)."""
    return numbers_in(json.dumps(facts, ensure_ascii=False))


def summary_problem(text: str, allowed: set[float]) -> str | None:
    if not text or len(text) > 200:
        return "length"
    if ADVICE.search(text):
        return "advice"
    if FORECAST.search(text):
        return "forecast"
    if not numbers_in(text) <= allowed:
        return "number_mismatch"
    return None


class EntryFitDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")
    leg: str
    fits: bool
    why: str


class ReviewDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str
    entryFits: list[EntryFitDraft] = []


# -- the service -----------------------------------------------------------------------------------

def _day_pnl(store: Any, account: dict[str, Any] | None, *, account_id: str, trading_day: str,
             next_day: str | None = None) -> tuple[float | None, float | None, str]:
    """(当日净盈亏, 手续费, 口径) of the trading day, on the settlement basis (所有者 2026-10-22，国内外一起): once the
    next trading day's row exists, its static balance is this day's settled close, so the day's net P&L is
    ``static(next day) − static(this day)`` — closes against the previous settlement, positions marked at this day's
    settlement price, fees out (口径 ``settlement``). Before the settlement it is the last read of the day (the account
    snapshot's equity against its static balance, positions at that moment's price; 口径 ``live``)."""
    from .store.trades import carries_day_pnl

    def money(value: Any) -> float | None:
        # 10-08: the raw ledger float (-204.0507999999454) reached the model and the 复盘 verbatim.
        return round(float(value), 2) if value is not None else None
    rows = store.list_account_daily(since_day=trading_day, trading_account_id=account_id)
    row = next((item for item in rows if item["tradingDay"] == trading_day), None)
    following = next((item for item in rows if next_day and item["tradingDay"] == next_day), None)
    if (row is not None and following is not None and row.get("staticBalance") is not None
            and following.get("staticBalance") is not None):
        return (money(float(following["staticBalance"]) - float(row["staticBalance"])), money(row.get("commission")),
                "settlement")
    if row is not None and row.get("todayNetPnl") is not None:
        return money(row["todayNetPnl"]), money(row.get("commission")), "live"
    if account and account.get("tradingDay") == trading_day and carries_day_pnl(account):
        try:
            return money(account["todayNetPnl"]), money(account.get("commission")), "live"
        except (KeyError, TypeError, ValueError):
            pass
    return None, None, "live"


def next_cme_trade_day(day: str) -> str:
    """The CME trade date after ``day`` (weekends and the days with no CME trade date skipped)."""
    current = date.fromisoformat(day) + timedelta(days=1)
    while current.weekday() >= 5 or current.isoformat() in CME_CLOSED_DAYS:
        current += timedelta(days=1)
    return current.isoformat()


class DailyReviewService:
    def __init__(self, app: Any, *, now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 adapter_for: Callable[[str], Any] | None = None, interval: float = LOOP_SECONDS) -> None:
        self.app = app
        self.now = now
        self.interval = interval
        self._adapter_for = adapter_for
        self._task: asyncio.Task[None] | None = None
        self._checked: set[tuple[str, str, str]] = set()
        self._settled: set[tuple[str, str, str]] = set()  # reviews rebuilt on the settled basis this process
        self._manual_at: dict[str, float] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    # -- inputs

    def _adapter(self, user_id: str) -> Any:
        if self._adapter_for is not None:
            return self._adapter_for(user_id)
        adapter: Any = getattr(self.app.state, "adapter", None)
        return adapter.adapter_for_user(user_id) if hasattr(adapter, "adapter_for_user") else adapter

    def _model(self, user_id: str) -> tuple[Any, str | None] | None:
        """The user's adapter and the model their tier allows, or None (no model, AI off, quota used up)."""
        from .plans import daily_token_limit, default_allowed_model, plan_for_user
        adapter = self._adapter(user_id)
        if adapter is None or not hasattr(adapter, "complete_structured") or not getattr(adapter, "agent_authenticated", False):
            return None
        store = self.app.state.store
        plan = plan_for_user(store, user_id)
        limit = daily_token_limit(plan, env_limit=int(os.getenv("AI_TRADER_DAILY_TOKEN_LIMIT", "200000000")))
        if limit is not None and store.token_usage(user_id, datetime.now(UTC).date().isoformat()) >= limit:
            return None
        ctx = getattr(self.app.state, "ctx", None)
        configured = set(getattr(getattr(ctx, "agent", None), "native_endpoints", None) or {})
        model = default_allowed_model(plan, configured, store.get_model_pref(user_id)) if configured else None
        if configured and model is None:
            return None
        return adapter, model

    def _names(self, provider: Any, symbols: set[str]) -> dict[str, str]:
        try:
            items = provider.instrument_catalog().get("items") or []
        except Exception:  # noqa: BLE001 — names are cosmetic; the code stays
            return {}
        return {str(item["symbol"]).upper(): re.sub(r"\d+$", "", str(item.get("instrumentName") or "")).strip()
                for item in items if str(item.get("symbol") or "").upper() in symbols}

    def _rules(self, user_id: str) -> list[str]:
        store = self.app.state.store
        try:
            if not store.user_preferences(user_id).chat.memory:
                return []
            return [item["text"] for item in store.list_memories(user_id)
                    if item["status"] == "active" and item["layer"] == "rule"][:12]
        except Exception:  # noqa: BLE001
            return []

    async def _strategy_fits(self, user_id: str, services: Any, legs: list[dict[str, Any]],
                             playbooks: dict[str, dict[str, Any]], placed: dict[str, int]) -> None:
        """合不合入场规则 for the opening 笔 under a setup linked to a strategy: that strategy's entry rule on the
        bars before the order was placed (`placed`; an order sent outside the app is judged at its first fill — a
        deleted strategy or a moment the bars cannot say leaves the 笔 unjudged)."""
        store = self.app.state.store
        for leg in legs:
            playbook = playbooks.get(leg.get("playbookId") or "")
            if leg["side"] not in OPEN_SIDES.values() or not playbook or not playbook.get("strategyId"):
                continue
            try:
                strategy = await asyncio.to_thread(store.get_strategy_draft, playbook["strategyId"], user_id=user_id)
            except KeyError:
                continue
            long = leg["side"] == OPEN_SIDES["BUY"]
            filled = int(datetime.fromisoformat(leg["time"]).timestamp() * NS)
            at = min(placed.get(leg["orderId"], filled), filled)
            fits = await asyncio.to_thread(trade_autopsy.strategy_entry_fit, services.provider, strategy["definition"],
                                           leg["symbol"], at_ns=at, long=long)
            if fits is None:
                continue
            side = "做多" if long else "做空"
            trade_autopsy.add_entry_fit(leg, fits=fits, basis="strategy", text=(
                f"入场时策略「{strategy['name']}」给出了{side}信号" if fits
                else f"入场时交易笔记「{playbook['name']}」关联的策略「{strategy['name']}」没有{side}信号"))

    # -- building

    async def day_fills(self, services: Any, trading_day: str) -> list[dict[str, Any]]:
        """The account's fills of the trading day. Empty only when that is a fact: for the current day the account
        activity must be current (fresh, orders initialized) — a connecting or stale read that shows no trades
        raises RuntimeError("daily_review_activity_not_current") instead of passing for a day without fills."""
        from .strategy_intents import _current_fact
        from .trading_costs import account_fills
        activity = await asyncio.to_thread(services.provider.account_activity)
        today = [item for item in activity.get("trades") or [] if item.get("tradeId")]
        fills = [item for item in await asyncio.to_thread(account_fills, self.app.state.store, today, None,
                                                           services.account_id)
                 if item.get("tradingDay") == trading_day]
        if (not fills and trading_day >= current_trading_day(services.account_id, self.now())
                and not _current_fact(activity, require_initialized=True)):
            raise RuntimeError("daily_review_activity_not_current")
        return fills

    async def build(self, user_id: str, services: Any, trading_day: str, *, reason: str) -> dict[str, Any] | None:
        """The trading day's review for the user on this account, or None when it has no fills."""
        from .strategy_workbench import account_trade_review, order_sources
        app, store = self.app, self.app.state.store
        ctx = getattr(app.state, "ctx", None)
        desk = getattr(getattr(ctx, "services", None), "research_desk", None)
        account_id = services.account_id
        fills = await self.day_fills(services, trading_day)
        if not fills:
            return None
        review = await account_trade_review(store, services.provider, app.state.fee_settings, user_id, symbol=None,
                                            days=30, trading_account_id=account_id, desk=desk)
        sources = await asyncio.to_thread(order_sources, store, user_id)
        reminders = await asyncio.to_thread(load_reminders, store, user_id=user_id, account_id=account_id)
        placed = await asyncio.to_thread(order_placed_times, store, user_id)
        names = await asyncio.to_thread(self._names, services.provider, {str(item["symbol"]).upper() for item in fills})
        legs = build_legs(fills, trading_day=trading_day, sources=sources, placed=placed, reminders=reminders,
                          rounds=review["trades"], names=names)
        playbooks = {item["id"]: item for item in await asyncio.to_thread(store.list_playbooks, user_id=user_id)}
        stops = await asyncio.to_thread(trade_autopsy.load_stops, store, user_id=user_id, account_id=account_id)
        trade_autopsy.check_legs(legs, rounds=review["trades"], sources=sources, stops=stops, keeps_playbooks=bool(playbooks))
        await self._strategy_fits(user_id, services, legs, playbooks, placed)
        next_day = await self._next_trading_day(services, trading_day)
        pnl, commission, basis = await asyncio.to_thread(_day_pnl, store, review.get("account"), account_id=account_id,
                                                         trading_day=trading_day, next_day=next_day)
        history = [(item["tradingDay"], leg) for item in store.recent_daily_reviews(
            user_id=user_id, account_id=account_id, before=trading_day, limit=HISTORY_REVIEWS)
            for leg in (item["payload"].get("legs") or [])]
        stats = leg_stats(legs)
        summary = {"text": template_summary(stats), "source": "template"}
        model_info: dict[str, Any] = {}
        # A setup written only in words: the model reads its entry rule against the 笔 (checked like the summary).
        worded = {leg["id"]: playbooks[leg["playbookId"]] for leg in legs if leg.get("playbookId") in playbooks
                  and "entryFit" not in leg and not playbooks[leg["playbookId"]].get("strategyId")
                  and playbooks[leg["playbookId"]].get("entryRules")}
        drafted = await self._draft(user_id, trading_day=trading_day, legs=legs, stats=stats, pnl=pnl,
                                    commission=commission, reminders=reminders, worded=worded,
                                    currency=account_currency(account_id))
        if drafted is not None:
            value, model_info, facts = drafted
            allowed = fact_numbers(facts)
            problem = summary_problem(value.summary.strip(), allowed)
            if problem is None:
                summary = {"text": value.summary.strip(), "source": "model"}
            else:
                log.info("daily_review_summary_dropped user=%s reason=%s", user_id, problem)
            by_id = {leg["id"]: leg for leg in legs}
            for fit in value.entryFits:
                why = fit.why.strip()
                if (fit.leg in worded and "entryFit" not in by_id[fit.leg] and len(why) <= 60
                        and summary_problem(why, allowed) is None):
                    trade_autopsy.add_entry_fit(by_id[fit.leg], fits=fit.fits, basis="model", text=why)
        # 本周最该改的一件事 (2026-10-25) replaces the daily 习惯 observation: on the week's last trading day only.
        observation = None
        lesson = trade_autopsy.week_lesson(trading_day, legs, history) if trade_autopsy.is_week_end(trading_day, next_day) else None
        if lesson is not None:
            observation = {**lesson, "kind": "weekly", "key": user_memory._normalized(lesson["text"]), "status": "pending",
                           # 这一版的修订号：每次生成都换，记住按它绑定用户看到的那一版 (#524)。
                           "revision": uuid4().hex[:16]}
        summary["basis"] = (f"依据这 {stats['legs']} 笔成交、当时的提醒和你的约定；数字都可在下面的成交里核对"
                            if summary["source"] == "model" else "按成交来源和提醒时间统计，没有用模型")
        generated = self.now().astimezone(UTC).isoformat(timespec="seconds")
        return {"version": VERSION, "tradingDay": trading_day, "accountId": account_id, "generatedAt": generated,
                "reason": reason, "stats": stats, "dayPnl": pnl, "dayPnlBasis": basis, "commission": commission, "legs": legs,
                "summary": summary, "observation": observation, "rules": list(TAG_RULES), **model_info}

    async def _draft(self, user_id: str, *, trading_day: str, legs: list[dict[str, Any]], stats: dict[str, int],
                     pnl: float | None, commission: float | None, reminders: list[Reminder],
                     worded: dict[str, dict[str, Any]], currency: str = "CNY") -> tuple[ReviewDraft, dict[str, Any], dict[str, Any]] | None:
        picked = await asyncio.to_thread(self._model, user_id)
        if picked is None:
            return None
        adapter, model = picked
        start = min(int(datetime.fromisoformat(leg["time"]).timestamp() * NS) for leg in legs) - 3600 * NS
        facts = {
            "tradingDay": trading_day, "stats": stats, "dayNetPnl": pnl, "commission": commission,
            "legs": [{**{key: leg[key] for key in ("id", "clock", "action", "tagLabel", "reason", "sourceLabel", "pnl")},
                      "checks": [item["text"] for item in leg.get("checks") or []],
                      **({"playbook": {"name": worded[leg["id"]]["name"], "entryRules": worded[leg["id"]]["entryRules"]}}
                         if leg["id"] in worded else {})}
                     for leg in legs[:MODEL_LEGS]],
            "reminders": [{"clock": _clock(item.at_ns), "kind": REMINDER_LABELS.get(item.kind, item.kind), "text": item.text[:160]}
                          for item in reminders if item.at_ns >= start][-20:],
            "rules": await asyncio.to_thread(self._rules, user_id),
        }
        instructions = INSTRUCTIONS
        if currency != "CNY":
            # 美元子账户（外盘）：金额都是美元，交易日是 CME 的（美东 18:00 换日），没有国内的日盘夜盘。
            unit = money_unit(currency)
            facts["currency"] = currency
            instructions += (f"\n这是外盘{unit}账户：输入里的金额（pnl、dayNetPnl、commission）都是{unit}，"
                             f"提到金额写“{unit}”，不要写“元”；交易日按 CME（美东 18:00 换日），不说日盘、夜盘。")
        try:
            result = await adapter.complete_structured(
                instructions=instructions, input_text=json.dumps(facts, ensure_ascii=False), schema=ReviewDraft,
                timeout_seconds=MODEL_TIMEOUT_SECONDS, model=model, usage_user=user_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — no model answer: the deterministic sentence
            log.warning("daily_review_model_failed user=%s error=%s", user_id, type(error).__name__)
            return None
        return result.value, {"model": model or getattr(adapter, "model", None), "tokens": int(getattr(result, "tokens", 0) or 0)}, facts

    async def _next_trading_day(self, services: Any, trading_day: str) -> str | None:
        """The trading day after ``trading_day`` on this account: the CME trade date for a USD sub-account, the
        exchange calendar's for the rest (None while the calendar cannot say — the day then stays on the live basis)."""
        if is_foreign_paper(services.account_id):
            return next_cme_trade_day(trading_day)
        from .exchange_calendar import trading_days
        try:
            days = await trading_days(services.provider)
        except Exception:  # noqa: BLE001 - no calendar now: the settled figure waits for the next round
            return None
        later = [day for day in days if day > trading_day]
        return min(later) if later and days and days[0] <= trading_day else None

    async def settle_recent(self, user_id: str, services: Any) -> int:
        """结算后更正（所有者 2026-10-22）：the review is pushed before the day settles (15:15 against the evening
        settlement; 05:15 against the 06:00 CME roll), so its 当日盈亏 is first the last read of the day. Once the next
        trading day's row carries the settled balance, the recent reviews still on the live basis are rebuilt quietly
        (no second push) with the settled figure. Returns how many were rebuilt."""
        store, account_id = self.app.state.store, services.account_id
        rebuilt = 0
        for item in store.recent_daily_reviews(user_id=user_id, account_id=account_id, before="9999-12-31",
                                               limit=SETTLE_LOOKBACK):
            day, payload = str(item["tradingDay"]), item["payload"] or {}
            key = (user_id, account_id, day)
            if key in self._settled or payload.get("dayPnlBasis") == "settlement":
                continue
            next_day = await self._next_trading_day(services, day)
            _, _, basis = await asyncio.to_thread(_day_pnl, store, None, account_id=account_id, trading_day=day,
                                                  next_day=next_day)
            if basis != "settlement":
                continue
            try:
                await self.generate(user_id, services, day, reason="settled")
            except (KeyError, ValueError, RuntimeError) as error:
                log.debug("daily_review_settle_skipped account=%s day=%s error=%s", account_id, day, error)
                continue
            self._settled.add(key)
            rebuilt += 1
        return rebuilt

    def _lock(self, user_id: str, account_id: str) -> asyncio.Lock:
        return self._locks.setdefault((user_id, account_id), asyncio.Lock())

    async def generate(self, user_id: str, services: Any, trading_day: str, *, reason: str) -> dict[str, Any] | None:
        """Build and store (overwrite) the day's review; None when the day has no fills."""
        async with self._lock(user_id, services.account_id):
            payload = await self.build(user_id, services, trading_day, reason=reason)
            if payload is not None:
                previous = self.app.state.store.get_daily_review(user_id=user_id, account_id=services.account_id,
                                                                 trading_day=trading_day)
                kept = ((previous or {}).get("payload") or {}).get("observation")
                fresh = payload["observation"]
                if kept and kept.get("status") != "pending" and (fresh is None or fresh.get("key") == kept.get("key")):
                    payload["observation"] = kept   # an answered observation stays answered on this day
                self.app.state.store.save_daily_review(user_id=user_id, account_id=services.account_id,
                                                       trading_day=trading_day, payload=payload, version=VERSION)
            return payload

    # -- the 15:15 run (17:15 New York for a USD sub-account)

    async def run_once(self) -> int:
        """One round: every owner of every assembled account, once per trading day after its review time
        (`due_review_day`: 15:15 Beijing; 17:15 New York for a USD sub-account). Returns how many reviews
        were delivered."""
        now = self.now()
        store = self.app.state.store
        delivered = 0
        for services in list(self.app.state.accounts.all()):
            for user_id in store.account_owner_ids(services.account_id):
                await self.settle_recent(user_id, services)
            day = due_review_day(services.account_id, now)
            if day is None:
                continue
            for user_id in store.account_owner_ids(services.account_id):
                key = (user_id, services.account_id, day)
                if key in self._checked:
                    continue
                payload = await self._auto_review(user_id, services, day)
                if payload is None:
                    continue   # no fills yet, an account still connecting, or another process generating
                self._checked.add(key)
                if await self.deliver(user_id, services.account_id, payload):
                    delivered += 1
        return delivered

    async def _auto_review(self, user_id: str, services: Any, day: str) -> dict[str, Any] | None:
        """The review the 15:15 run delivers, or None (nothing to deliver this round; tried again next round)."""
        store, account_id = self.app.state.store, services.account_id
        existing = store.get_daily_review(user_id=user_id, account_id=account_id, trading_day=day)
        if existing is not None and existing["pushedAt"]:
            self._checked.add((user_id, account_id, day))
            return None
        if existing is not None and (existing["payload"] or {}).get("reason") == "auto":
            # Generated by the 15:15 run (this process or the other slot) but not delivered yet: deliver that saved
            # text, so what is archived and what is pushed stay one.
            return existing["payload"]
        try:
            if not await self.day_fills(services, day):
                return None   # a current read with no fills: looked at again next round, never final for the day
        except (KeyError, ValueError, RuntimeError) as error:
            log.debug("daily_review_skipped account=%s error=%s", account_id, error)
            return None   # an account still connecting (or activity not current) is tried again next round
        stale_before = datetime.fromtimestamp(time.time() - GENERATION_CLAIM_SECONDS, UTC).isoformat()
        if not store.claim_daily_review_generation(user_id=user_id, account_id=account_id, trading_day=day,
                                                   stale_before=stale_before):
            return None   # another process is generating it; the next round delivers what it saved
        try:
            payload = await self.generate(user_id, services, day, reason="auto")
        except (KeyError, ValueError, RuntimeError) as error:
            store.release_daily_review_generation(user_id=user_id, account_id=account_id, trading_day=day)
            log.debug("daily_review_skipped account=%s error=%s", account_id, error)
            return None
        except BaseException:
            store.release_daily_review_generation(user_id=user_id, account_id=account_id, trading_day=day)
            raise
        if payload is None:
            store.release_daily_review_generation(user_id=user_id, account_id=account_id, trading_day=day)
        return payload

    async def deliver(self, user_id: str, account_id: str, payload: dict[str, Any]) -> bool:
        """The once-only delivery through the one reminder entry (#932): the review's pushed_at (the claim
        both builds read) commits with the 提醒中心 row and the message; then the feed event and the push.
        With 收盘复盘 off nothing is claimed — turned on later the same day, the next round delivers it."""
        from .reminders import ReminderSpec, deliver
        from .web_push import review_message
        app, store = self.app, self.app.state.store
        day = payload["tradingDay"]
        stats, summary = payload["stats"], payload["summary"]["text"]
        foreign = is_foreign_paper(account_id)
        title = f"{'外盘' if foreign else '今日'}复盘 · {day[5:]}"
        pnl = review_money(payload.get("dayPnl"), account_id)
        turn_id = f"daily-review-{account_id}-{day}"
        at = self.now().astimezone(UTC).isoformat()
        card = reminder_card(turn_id=turn_id, kind="daily_review", title=title, summary=summary, at=at,
                             figures=[("当日净盈亏", pnl), ("成交", f"{stats['legs']} 笔"),
                                      ("按计划", f"{stats['planned']} / {stats['legs']}")],
                             actions=[ui_action("看复盘", {"reviewDay": day})])
        card.update({"tradingDay": day, "pnl": pnl, "tags": [
            {"tag": tag, "label": TAG_LABELS[tag], "count": stats[name]}
            for tag, name in (("plan", "planned"), ("remind", "remind"), ("adhoc", "adhoc")) if stats[name]]})
        text = f"{'外盘复盘（美元账户，' if foreign else '今日复盘（'}{day[5:]}）：{summary}"

        def claim(connection: Any) -> bool:
            return bool(store.claim_daily_review_push(user_id=user_id, account_id=account_id, trading_day=day,
                                                      connection=connection))

        # A live voice call is not interrupted: the review waits on the page and in the conversation.
        result = await deliver(app, ReminderSpec(
            kind="daily_review", user_id=user_id, account_id=account_id, dedupe_key=f"{account_id}:{day}",
            trading_day=day, at=at, title="外盘复盘好了" if foreign else "今天的复盘好了", body=summary, message=text, turn_id=turn_id,
            cards=[("card.reminder", card)], target={"type": "review", "tradingDay": day},
            push=review_message(trading_day=day, legs=stats["legs"], planned=stats["planned"], pnl=pnl,
                                account_id=account_id),
            legacy=claim))
        if result.reminder is None:
            # Off, already delivered by the other slot, or a failed write: look again next round (an
            # already delivered review is recognised by its pushed_at and dropped there).
            self._checked.discard((user_id, account_id, day))
            return False
        return True

    def allow_manual(self, user_id: str) -> bool:
        now = time.monotonic()
        if now - self._manual_at.get(user_id, -MANUAL_MIN_SECONDS) < MANUAL_MIN_SECONDS:
            return False
        self._manual_at[user_id] = now
        return True

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 — one bad round never ends the loop
                log.warning("daily_review_loop_failed", exc_info=True)

    def start(self) -> None:
        """The 15:15 loop: only the instance-lease holder runs it (wiring/producers.py)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


# -- routes ------------------------------------------------------------------------------------------

DAY = r"^\d{4}-\d{2}-\d{2}$"


class RegenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tradingDay: str | None = Field(default=None, pattern=DAY, description="交易日；省略为今天（北京时间）")


class ObservationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tradingDay: str = Field(pattern=DAY, description="这份复盘的交易日")
    verdict: Literal["remember", "dismiss"] = Field(description="remember 记住（写进经验记忆）/ dismiss 不对（以后不再提）")
    quote: str | None = Field(default=None, max_length=200,
                              description="AI 代用户记住时必填：用户亲口说“记住这条”的原话（服务端核对话记录）")
    observationKey: str = Field(min_length=1, max_length=400, description="回答的那条观察的 key（GET 返回的 observation.key）")
    revision: str = Field(min_length=1, max_length=400,
                          description="回答的那一版观察的 revision（GET 返回的 observation.revision；旧复盘没有时填 key）")


def _observation_revision(observation: dict[str, Any]) -> str:
    """The version a 记住/不对 must name: the generated revision, or (reviews written before it) the key."""
    return str(observation.get("revision") or observation.get("key") or "")


def _stale(observation: dict[str, Any] | None, body: ObservationAnswer) -> bool:
    return (not observation or str(observation.get("key") or "") != body.observationKey
            or _observation_revision(observation) != body.revision)


OBSERVATION_STALE = {"code": "observation_stale", "message": "这条观察已更新，请看新的一条"}


def register(application: FastAPI, ctx: Any) -> None:
    from .skill_registry import human_request
    store = ctx.services.store
    current_user = ctx.current_user
    service = DailyReviewService(application)
    application.state.daily_reviews = service

    def view(record: dict[str, Any] | None, days: list[str], account_id: str) -> dict[str, Any]:
        return {"review": record["payload"] if record else None, "days": days,
                "generatedAt": record["generatedAt"] if record else None,
                "generateAt": generate_at_text(account_id, service.now()), "currency": account_currency(account_id),
                "rules": list(TAG_RULES)}

    @application.get("/api/account/daily-review")
    async def get_daily_review(request: Request, tradingDay: str | None = Query(default=None, pattern=DAY)) -> dict[str, Any]:
        """今日复盘（交易验尸）：这个账户某个交易日（省略＝最近一份）的复盘——每笔成交（时间、动作、来源、理由、标签
        计划内 / 提醒后 / 临时、平仓盈亏），每笔程序查出的问题 checks（code：early_exit 提前离场、after_losses 连亏后再开、
        untagged 没标交易笔记、stop_beyond 亏过止损、no_stop 没设止损、entry_miss 不合入场规则）、开仓笔的交易笔记 playbookName
        与入场判断 entryFit（basis strategy 按关联策略的规则算 / model 由模型按打法文字判断）、这笔所属的平仓记录
        roundId 和要回答的一句 question（回答写进 PUT /api/account/trade-notes/{roundId} 的补充）；四格统计、总结和
        标签规则原话；一周最后一个交易日的 observation 是“本周最该改的一件事”（kind weekly、code、count、status
        pending / remembered / dismissed），其他日子为 null；`days` 是有复盘的交易日（新到旧）。每个交易日 15:15 有成交才生成，没有时 review 为 null。
        美元子账户（外盘，账户头 paperusd:）按 CME 交易日：美东 17:15（收盘后一刻钟，北京时间次日 05:15 / 冬令时 06:15）
        生成那个 CME 交易日的复盘，金额是美元（currency USD），`generateAt` 是这个时点的北京时间。只读。"""
        user_id = await current_user(request)
        account = (await ctx.account_context(request, user_id)).account
        record = store.get_daily_review(user_id=user_id, account_id=account.account_id, trading_day=tradingDay)
        return view(record, store.daily_review_days(user_id=user_id, account_id=account.account_id), account.account_id)

    @application.post("/api/account/daily-review/regenerate")
    async def regenerate_daily_review(request: Request, body: RegenerateBody) -> dict[str, Any]:
        """重新生成：按现在的成交重算这个交易日（省略＝今天；美元子账户是当前的 CME 交易日）的复盘并覆盖，不推送。同一用户 30 秒一次（429）；这天没有
        成交 409 no_fills。总结用你的模型和额度，额度用完或模型不可用时写确定性的一句。"""
        user_id = await current_user(request)
        account = (await ctx.account_context(request, user_id)).account
        if not service.allow_manual(user_id):
            raise HTTPException(429, "刚生成过，30 秒后再试")
        try:
            day = body.tradingDay or current_trading_day(account.account_id, service.now())
            payload = await service.generate(user_id, account, day, reason="manual")
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(409, "成交或合约参数尚不可用") from error
        if payload is None:
            raise HTTPException(409, {"code": "no_fills", "message": "这个交易日没有成交，不生成复盘"})
        record = store.get_daily_review(user_id=user_id, account_id=account.account_id, trading_day=payload["tradingDay"])
        return view(record, store.daily_review_days(user_id=user_id, account_id=account.account_id), account.account_id)

    @application.post("/api/account/daily-review/observation")
    async def answer_observation(request: Request, body: ObservationAnswer) -> dict[str, Any]:
        """复盘里的“本周最该改的一件事”（一周最后一个交易日的 observation）：remember 记住——写进私人记忆的经验层
        （来源记这份复盘的交易日与成交 id），像授权的话 422 不记；dismiss 不对——记下否决。必须带回答的那一版观察的 observationKey 与 revision
        （GET 返回的 observation.key / observation.revision）：观察已重新生成、不是这一版时 409 observation_stale
        （重读当前复盘再答）。AI 代用户记住只在用户明确说“记住这条”时，并且必须带 quote（用户原话，服务端核对，
        对不上 422；记忆关闭 409）。观察已答过 409。记忆与审计、否决记录、复盘状态在同一事务里写，要么都写要么都不写。"""
        user_id = await current_user(request)
        account = (await ctx.account_context(request, user_id)).account
        record = store.get_daily_review(user_id=user_id, account_id=account.account_id, trading_day=body.tradingDay)
        observation = ((record or {}).get("payload") or {}).get("observation")
        if not observation:
            raise HTTPException(404, "这份复盘没有“本周最该改的一件事”")
        if _stale(observation, body):
            raise HTTPException(409, OBSERVATION_STALE)
        if observation.get("status") != "pending":
            raise HTTPException(409, "这条观察已经答过了")
        human = human_request(request)
        text = str(observation["text"])
        if body.verdict == "remember":
            if not human:
                if not user_memory._memory_enabled(store, user_id):
                    raise HTTPException(409, "记忆已关闭，可在设置 → 记忆打开")
                if not body.quote or not body.quote.strip():
                    raise HTTPException(422, "AI 代你记住时必须带你说“记住这条”的原话 quote")
                try:
                    user_memory.verify_quote(store, user_id, body.quote)
                except user_memory.MemoryRefused as error:
                    raise HTTPException(422, str(error)) from error
            try:
                text = user_memory.clean_text(text)
            except user_memory.MemoryRefused as error:
                raise HTTPException(422, str(error)) from error
        verdict = "remembered" if body.verdict == "remember" else "dismissed"
        with store.transaction() as db:
            # 同一短事务里重读：仍是用户看到的那一版、仍待回答，才一并写记忆与审计、否决记录和复盘状态。
            record = store.get_daily_review(user_id=user_id, account_id=account.account_id, trading_day=body.tradingDay)
            current = ((record or {}).get("payload") or {}).get("observation")
            if record is None or _stale(current, body):
                raise HTTPException(409, OBSERVATION_STALE)
            assert current is not None
            if current.get("status") != "pending":
                raise HTTPException(409, "这条观察已经答过了")
            memory_id = None
            if body.verdict == "remember":
                existing = next((item for item in store.list_memories(user_id) if item["status"] == "active"
                                 and user_memory._normalized(item["text"]) == user_memory._normalized(text)), None)
                if existing is not None:
                    memory_id = existing["id"]
                else:
                    memory = store.insert_memory(user_id, "lesson", text, "user", {
                        "via": "daily_review", "tradingDay": body.tradingDay, "accountId": account.account_id,
                        "tradeIds": current.get("tradeIds") or [], "legs": current.get("legs") or [],
                        "quote": body.quote if not human else None, "at": user_memory.beijing_now()}, [],
                        actor="user" if human else "ai", connection=db)
                    memory_id = memory["id"]
            store.record_observation_verdict(user_id=user_id, key=str(current.get("key") or user_memory._normalized(current["text"])),
                                             verdict=verdict, text=str(current["text"]), account_id=account.account_id,
                                             trading_day=body.tradingDay, connection=db)
            payload = {**record["payload"], "observation": {**current, "status": verdict,
                                                            **({"memoryId": memory_id} if memory_id else {})}}
            store.save_daily_review(user_id=user_id, account_id=account.account_id, trading_day=body.tradingDay,
                                    payload=payload, version=int(record["version"]), connection=db)
        return {"tradingDay": body.tradingDay, "observation": payload["observation"], "memoryId": memory_id}

__all__ = ["DailyReviewService", "Reminder", "TAG_LABELS", "TAG_RULES", "annotate_round_tags", "build_legs",
           "current_trading_day", "due_review_day", "load_reminders", "product_key", "register", "tag_for",
           "template_summary"]

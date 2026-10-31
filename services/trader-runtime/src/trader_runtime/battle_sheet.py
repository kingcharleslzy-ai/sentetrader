"""今日作战表 (owner 2026-10-25, 侧栏重排第 5 批): the 调研 panel's first page — one row per 自选品种, ranked.

Each row is computed by the program, never by a model: the key levels from the product's main contract
bars (昨高 / 昨低, the 开盘区间 = the trading day's first 15 minutes, the day's 均价), the news strength and
the newest 快讯 events from the 调研 page's screened events, and a priority from how strong the news is
and how close the price sits to a key level. No direction is shown (owner decision: the sheet names levels
and setups, not 偏多 / 偏空). The product part is shared and reads no account; the user's part — which
products (their 自选) and which of their 打法 may show up near those levels — is put together per request.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .instruments import FOREIGN_BY_KEY
from .trading_calendar import bar_trading_day, daily_bar_day, trading_day_of

log = logging.getLogger("uvicorn.error.battle_sheet")

SHEET_LIMIT = 12  # the desk pre-generates at most 12 products a day; the sheet ranks as many 自选
ROW_TTL_SECONDS = 60
INTRADAY_SECONDS = 300
INTRADAY_BARS = 240  # 20 trading hours of 5-minute bars: always reaches back past the current day's open
DAILY_BARS = 10
OPENING_BARS = 3  # 开盘区间 = the trading day's first three 5-minute bars
NEAR_PERCENT = 0.3
CLOSE_PERCENT = 1.0
NEWS_SCORE = {"强": 2, "中": 1, "无": 0}
NEWS_SHOWN = 2

# 打法本 names / entry rules that fit a scenario: the scenario is in play when one of its levels is within
# CLOSE_PERCENT of the price (or, for 消息, when the product has news today).
SCENARIO_WORDS = {
    "prev": ("昨高", "昨低", "前高", "前低", "昨日", "昨天"),
    "opening": ("开盘区间", "开盘", "区间突破", "ORB", "orb"),
    "vwap": ("均价", "VWAP", "vwap", "分时均"),
    "news": ("消息", "新闻", "数据", "事件", "公告"),
}
ANY_LEVEL_WORDS = ("突破", "回踩", "假突破", "反抽", "支撑", "压力")
LEVEL_SCENARIO = {"prev_high": "prev", "prev_low": "prev", "opening_high": "opening", "opening_low": "opening", "vwap": "vwap"}


def key_levels(daily: list[dict[str, Any]], intraday: list[dict[str, Any]], current_day: str,
               trading_days: list[str]) -> list[dict[str, Any]]:
    """昨高 / 昨低 from the last daily bar before the current trading day; 开盘区间 and 均价 from the current
    day's 5-minute bars (夜盘归下一交易日) — only when the series shows the bar before the day's first one, so
    the day is known to be complete from its open. Before the day opens there is no 开盘区间 or 均价 yet."""
    levels: list[dict[str, Any]] = []
    previous = [bar for bar in daily if _number(bar.get("high")) and daily_bar_day(bar) < current_day]
    if previous:
        bar = max(previous, key=daily_bar_day)
        levels += [{"key": "prev_high", "label": "昨高", "price": float(bar["high"])},
                   {"key": "prev_low", "label": "昨低", "price": float(bar["low"])}]
    days = [bar_trading_day(bar, trading_days or None) for bar in intraday]
    start = next((index for index, day in enumerate(days) if day == current_day), None)
    if start:  # 0 = the series starts inside the day: its open may be missing
        today = intraday[start:]
        if len(today) >= OPENING_BARS:
            opening = today[:OPENING_BARS]
            levels += [{"key": "opening_high", "label": "开盘区间高", "price": max(float(bar["high"]) for bar in opening)},
                       {"key": "opening_low", "label": "开盘区间低", "price": min(float(bar["low"]) for bar in opening)}]
        volume = sum(float(bar.get("volume") or 0) for bar in today)
        if volume > 0:
            weighted = sum((float(bar["high"]) + float(bar["low"]) + float(bar["close"])) / 3 * float(bar.get("volume") or 0)
                           for bar in today)
            levels.append({"key": "vwap", "label": "均价", "price": weighted / volume})
    return levels


def with_distances(levels: list[dict[str, Any]], price: float | None) -> list[dict[str, Any]]:
    if not price:
        return levels
    return [{**level, "distancePercent": round(abs(price - level["price"]) / price * 100, 2)} for level in levels]


def news_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    """强: a 重要 event in the last 24 hours; 中: only 其他 ones (or keyword-only, unscreened); 无: none. The
    shown items are the newest 重要 first, then the rest — what 调研 · 快讯 already lists, no direction."""
    major = [event for event in events if event.get("tier") == "major"]
    other = [event for event in events if event.get("tier") in {"other", "unscreened"}]
    strength = "强" if major else "中" if other else "无"
    items = [{"id": event.get("id"), "title": event.get("eventLabel") or event.get("title") or str(event.get("text") or "")[:40],
              "at": event.get("publishedAt"), "reports": int(event.get("reports") or 1)} for event in (major + other)[:NEWS_SHOWN]]
    return {"strength": strength, "items": items, "major": len(major), "other": len(other),
            "minor": sum(event.get("tier") == "minor" for event in events)}


def priority_of(strength: str, distance: float | None) -> tuple[str, int]:
    """高 / 中 / 低 from two points each: the news (强 2, 中 1) and the nearest key level (≤0.3% 2, ≤1% 1)."""
    score = NEWS_SCORE.get(strength, 0) + (0 if distance is None else 2 if distance <= NEAR_PERCENT else 1 if distance <= CLOSE_PERCENT else 0)
    return ("高" if score >= 3 else "中" if score == 2 else "低"), score


def playbooks_for(playbooks: list[dict[str, Any]], levels: list[dict[str, Any]], strength: str) -> list[dict[str, Any]]:
    """The user's 打法 whose name or entry rules mention a scenario in play: a level within CLOSE_PERCENT
    (昨高低 / 开盘区间 / 均价, or a generic 突破 / 回踩 word for any of them), or 消息 when there is news."""
    close = [level for level in levels if level.get("distancePercent") is not None and level["distancePercent"] <= CLOSE_PERCENT]
    close.sort(key=lambda level: level["distancePercent"])
    matched: list[dict[str, Any]] = []
    for playbook in playbooks:
        text = f"{playbook.get('name') or ''} {playbook.get('entryRules') or ''}"
        why = next((f"离{level['label']} {level['distancePercent']}%" for level in close
                    if any(word in text for word in SCENARIO_WORDS[LEVEL_SCENARIO[level["key"]]])), None)
        # A generic 突破 / 回踩 fits any close level, unless the setup names its own level (昨低反抽 waits for 昨低).
        named = any(word in text for scenario in ("prev", "opening", "vwap") for word in SCENARIO_WORDS[scenario])
        if why is None and close and not named and any(word in text for word in ANY_LEVEL_WORDS):
            why = f"离{close[0]['label']} {close[0]['distancePercent']}%"
        if why is None and strength != "无" and any(word in text for word in SCENARIO_WORDS["news"]):
            why = f"消息{strength}"
        if why is not None:
            matched.append({"id": playbook.get("id"), "name": playbook.get("name"), "why": why})
    return matched[:3]


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and value > 0 else None


@dataclass
class BattleSheet:
    """The shared per-product rows (cached a minute) and the per-user sheet built from them."""
    desk: Any  # ResearchDeskService: the provider, the catalog's main contract, the trading days, the 快讯 events
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    _rows: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict, repr=False)

    async def product_row(self, key: str) -> dict[str, Any]:
        cached = self._rows.get(key)
        if cached is not None and self.clock() - cached[0] < ROW_TTL_SECONDS:
            return cached[1]
        row = await self._compute(key)
        self._rows[key] = (self.clock(), row)
        return row

    async def _compute(self, key: str) -> dict[str, Any]:
        exchange, _, product = key.partition(".")
        row: dict[str, Any] = {"product": key, "name": product, "symbol": None, "price": None, "levels": [], "nearest": None,
                               "news": {"strength": "无", "items": [], "major": 0, "other": 0, "minor": 0}}
        extra = getattr(self.desk, "extra", None)
        if extra is not None:
            try:
                name, events = await asyncio.to_thread(extra.product_events, product.lower())
                row["name"], row["news"] = name or product, news_of(events)
            except Exception:  # noqa: BLE001 — no events: the row still ranks by its levels
                log.warning("battle_sheet_events_failed product=%s", key, exc_info=True)
        symbol = await self.desk.main_symbol(product)
        if not symbol or not symbol.upper().startswith(exchange.upper() + "."):
            row["error"] = "合约目录里没有这个品种的主力合约"
        else:
            row["symbol"] = symbol
            provider = self.desk.provider()
            try:
                daily, intraday, snapshot = await asyncio.gather(
                    asyncio.to_thread(provider.market_history, symbol, 86400, DAILY_BARS),
                    asyncio.to_thread(provider.market_history, symbol, INTRADAY_SECONDS, INTRADAY_BARS),
                    asyncio.to_thread(provider.market_snapshot, "", symbol))
                days = self.desk.trading_days()
                current = trading_day_of(int(self.now().timestamp() * 1e9), days or None)
                row["price"] = _number(snapshot.get("price"))
                row["levels"] = with_distances(key_levels([bar for bar in daily.get("bars") or [] if isinstance(bar, dict)],
                                                          [bar for bar in intraday.get("bars") or [] if isinstance(bar, dict)],
                                                          current, days), row["price"])
            except Exception as error:  # noqa: BLE001 — the sidecar's history session is down: say so on the row
                log.warning("battle_sheet_market_failed symbol=%s error=%s", symbol, error)
                row["error"] = "行情暂时读不到，关键价位稍后再算"
        measured = [level for level in row["levels"] if level.get("distancePercent") is not None]
        nearest = min(measured, key=lambda level: level["distancePercent"]) if measured else None
        row["nearest"] = {"label": nearest["label"], "distancePercent": nearest["distancePercent"]} if nearest else None
        row["priority"], row["score"] = priority_of(row["news"]["strength"], nearest["distancePercent"] if nearest else None)
        return row

    async def sheet(self, products: list[str], playbooks: list[dict[str, Any]]) -> dict[str, Any]:
        domestic = [key for key in products if key not in FOREIGN_BY_KEY]
        shown = domestic[:SHEET_LIMIT]
        rows = await asyncio.gather(*(self.product_row(key) for key in shown))
        ranked = sorted(rows, key=lambda row: (-row["score"], row["nearest"]["distancePercent"] if row["nearest"] else 999.0,
                                               shown.index(row["product"])))
        return {"generatedAt": self.now().isoformat(timespec="seconds"),
                "rows": [{**row, "playbooks": playbooks_for(playbooks, row["levels"], row["news"]["strength"])} for row in ranked],
                "foreign": [key for key in products if key in FOREIGN_BY_KEY], "over": max(0, len(domestic) - SHEET_LIMIT),
                "playbookCount": len(playbooks)}

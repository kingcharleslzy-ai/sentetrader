from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, Literal, Protocol
from urllib.parse import quote

from . import exchange_calendar
from .instruments import account_currency
from .sidecar_http import SidecarHttp, SidecarHttpError
from .trading_calendar import naive_trading_day

StatusSubscriber = Callable[[dict[str, Any]], None]
Unsubscribe = Callable[[], None]
CHINA_TIMEZONE = timezone(timedelta(hours=8))

# Detached HTTP snapshots are prefetched before taking business action locks.
# Transactions, authorization checks and dispatch stay on the Runtime task.
_read_snapshot: ContextVar[Any] = ContextVar("sidecar_read_snapshot", default=None)


@asynccontextmanager
async def provider_read_snapshot(provider, *, symbols=(), timeframes=(), activity=True, refresh=False,
                                 account=False):
    if not isinstance(provider, SidecarReadOnlyDataProvider):
        # Replay and in-memory providers already expose non-blocking snapshots.
        yield
        return
    if account:
        # 同一份采集事实 (#700 review): the whole account context (trading day / positions /
        # activity) is one combined sidecar read instead of a fresh activity paired with the
        # poll cache's positions. A sidecar without the combined read fails the prefetch and
        # the caller refuses — never judged across two snapshots.
        paths = {"/v1/snapshot"}
    else:
        paths = {"/v1/activity"} if activity else set()
    for symbol in symbols:
        symbol = symbol.strip()
        prefix = f"/v1/market/{quote(symbol, safe='.')}"
        paths.add(f"{prefix}/quote")
        paths.update(f"{prefix}/workbench?duration={duration}" for duration in timeframes)
    current = _read_snapshot.get()
    held: dict[str, Any] = {}
    if current and current[0] is provider:
        # Preserve unrequested facts. Explicit refresh still re-fetches requested
        # market data; account activity refreshes the entire combined cut together.
        held = dict(current[1])
        if "/v1/snapshot" in held and "/v1/activity" in paths:
            paths.remove("/v1/activity")
            paths.add("/v1/snapshot")
        if not refresh:
            paths = {path for path in paths if path.casefold() not in held}

    async def fetch(path):
        try:
            value = await provider._request_async("GET", path)
        except Exception as error:
            value = error
        return path.casefold(), (value, time.monotonic())

    # The exchange calendar is not part of the cut: an account snapshot (its readers judge order expiry,
    # backfilled fills and the direct-stop bucket) starts its read beside the cut, into its own cache, so
    # they find it in memory instead of failing as not prefetched (#754). The snapshot never waits for it;
    # market-only snapshots leave it alone.
    if account or activity:
        exchange_calendar.read_soon(provider)
    values = {**held, **dict(await asyncio.gather(*(fetch(path) for path in sorted(paths))))}
    token = _read_snapshot.set((provider, values))
    try:
        yield
    finally:
        _read_snapshot.reset(token)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _fixture_trading_day(timestamp_ns: int) -> str:
    return naive_trading_day(timestamp_ns)  # the Runtime's one rule, weekend-only (fixture data)


def _age_label(age_seconds: float) -> str:
    seconds = max(0, int(age_seconds))
    if seconds < 5:
        return "刚刚"
    if seconds < 60:
        return f"{seconds} 秒前"
    return f"{seconds // 60} 分钟前"


def _initialization_progress_bucket(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return float(math.floor(seconds / 10) * 10)


def fake_account_summary(turn_id: str) -> dict[str, Any]:
    return {
        "turnId": turn_id, "accountName": "假数据模拟账户", "updatedAt": "14:26",
        "capturedAt": "2026-08-25T14:26:00+08:00", "ageSeconds": 0,
        "dataStatus": "simulated", "equity": 1_026_580, "todayPnl": 3_420,
        "tradingDay": "2026-08-25",
        "todayNetPnl": 3_420, "floatProfit": 3_420, "positionProfit": 3_420,
        "closeProfit": 0, "commission": 0, "marginRatio": 18.6,
        "available": 835_570, "margin": 191_010, "riskRatio": 18.6,
        "positionCount": 3, "riskBudgetUsed": 31, "status": "normal",
        "insight": "黄金多头贡献约 78% 的今日盈利；螺纹钢策略尚未形成完整信号。",
        "equityCurve": [18, 18.4, 20.2, 19.7, 22.8, 24.1, 23.7, 25.9],
    }


def fake_market_snapshot(turn_id: str, symbol: str = "AU2612") -> dict[str, Any]:
    snapshot = {
        "turnId": turn_id, "symbol": symbol.upper(),
        "name": "沪金" if symbol.upper().startswith("AU") else symbol.upper(),
        "price": 823.5, "changePercent": 0.68, "updatedAt": "2 秒前",
        "capturedAt": "2026-08-25T14:26:00+08:00", "ageSeconds": 2,
        "dataStatus": "simulated", "trend": "up",
        "quoteDatetime": datetime.now(UTC).isoformat(),
        "bidPrice1": 823.0, "bidVolume1": 12,
        "askPrice1": 824.0, "askVolume1": 9,
        "priceTick": 0.5, "volumeMultiple": 1000.0,
        "upperLimit": 1000.0, "lowerLimit": 600.0,
        # 合约信息卡的时间块 (2026-10-02)：TqSdk trading_time 形状（夜盘跨零点时结束时间更小）。
        "tradingSchedule": {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
                            "night": [["21:00:00", "02:30:00"]]},
        "openLimit": 0,
        "marginPerLot": 82_350.0, "commissionPerLot": 24.71,
        "tradingStatus": "CONTINOUS", "tradingStatusLabel": "连续交易",
        "summary": "短线保持上行，但当前只展示用户询问所需的价格与方向信息。",
        "priceCurve": [815.2, 816.4, 817.1, 816.8, 819.3, 820.1, 821.9, 823.5],
    }
    # 五档盘口：按 priceTick 逐档外推，挂量逐档递减。
    bid_volumes, ask_volumes = (12, 10, 8, 6, 4), (9, 7, 5, 3, 2)
    for level in range(2, 6):
        snapshot[f"bidPrice{level}"] = snapshot["bidPrice1"] - snapshot["priceTick"] * (level - 1)
        snapshot[f"bidVolume{level}"] = bid_volumes[level - 1]
        snapshot[f"askPrice{level}"] = snapshot["askPrice1"] + snapshot["priceTick"] * (level - 1)
        snapshot[f"askVolume{level}"] = ask_volumes[level - 1]
    return snapshot


def fake_instrument_catalog() -> dict[str, Any]:
    # 时段与 fake 行情快照同一份（收盘提醒读目录条目的 tradingSchedule，不占 K 线订阅）：
    # 商品夜盘 21:00–02:30（跨零点写法），股指只有日盘。
    day = [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]]
    night = [["21:00:00", "02:30:00"]]
    stock_day = [["09:30:00", "11:30:00"], ["13:00:00", "15:00:00"]]
    return {
        "schemaVersion": 1,
        "capturedAt": _utc_now(),
        "dataStatus": "simulated",
        "isCached": False,
        "items": [
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
            # 近月股指（现金交割）：到期提醒的样例，与 9 月已到期的主力 IF2609 区分。
            {"symbol": "CFFEX.IF2610", "exchangeId": "CFFEX", "instrumentId": "IF2610",
             "instrumentName": "沪深300股指2610", "productId": "IF", "priceTick": 0.2,
             "volumeMultiple": 300.0, "deliveryYear": 2026, "deliveryMonth": 10,
             "expireRestDays": 10, "hasNight": False, "isMain": False, "openLimit": 0,
             "tradingSchedule": {"day": stock_day, "night": []}},
            # 交割月提醒的样例持仓合约（2026-10 交割）：与 rb2610 分开——测试体系默认 rb2610 没有持仓。
            {"symbol": "SHFE.hc2610", "exchangeId": "SHFE", "instrumentId": "hc2610",
             "instrumentName": "热轧卷板2610", "productId": "hc", "priceTick": 1.0,
             "volumeMultiple": 10.0, "deliveryYear": 2026, "deliveryMonth": 10,
             "expireRestDays": 35, "hasNight": True, "isMain": False, "openLimit": 0,
             "tradingSchedule": {"day": day, "night": night}},
        ],
    }


def fake_market_workbench(symbol: str, duration_seconds: int) -> dict[str, Any]:
    market = fake_market_snapshot("", symbol)
    start = 1_788_192_000_000_000_000
    bars = []
    for index in range(80):
        opening = 810 + index * 0.15
        timestamp = start + index * duration_seconds * 1_000_000_000
        bars.append({
            "datetime": timestamp,
            "open": opening,
            "high": opening + 1.2,
            "low": opening - 0.8,
            "close": opening + 0.6,
            "volume": 100 + index,
            "openInterest": 8_000 + index * 2,
            "tradingDay": _fixture_trading_day(timestamp),
        })
    return {
        "schemaVersion": 1,
        "durationSeconds": duration_seconds,
        "capturedAt": _utc_now(),
        "ageSeconds": 0,
        "dataStatus": "simulated",
        "isCached": False,
        "liveTrading": "disabled",
        "quote": {
            "symbol": market["symbol"],
            "name": market["name"],
            "quoteDatetime": market["quoteDatetime"],
            "lastPrice": bars[-1]["close"],
            "changePercent": market["changePercent"],
            # 昨结价 (2026-10-02): the 分时图's dashed reference; the board row already carried it.
            "preSettlement": round(bars[-1]["close"] / (1 + market["changePercent"] / 100), 2),
            "bidPrice1": market["bidPrice1"],
            "bidVolume1": 12,
            "askPrice1": market["askPrice1"],
            "askVolume1": 9,
            "open": bars[0]["open"],
            "highest": max(bar["high"] for bar in bars),
            "lowest": min(bar["low"] for bar in bars),
            "volume": 24_680,
            "openInterest": bars[-1]["openInterest"],
            "priceTick": market["priceTick"],
            "volumeMultiple": market["volumeMultiple"],
            "upperLimit": market["upperLimit"],
            "lowerLimit": market["lowerLimit"],
            "marginPerLot": market["marginPerLot"],
            "commissionPerLot": market["commissionPerLot"],
            "tradingSchedule": market["tradingSchedule"],
            **{key: market[key] for key in market
               if key.startswith(("bidPrice", "bidVolume", "askPrice", "askVolume")) and key[-1] in "2345"},
        },
        "bars": bars,
    }


def _fake_board_row(item: dict[str, Any], *, percent: float = 0.3,
                    volume: int = 24_680, open_interest: int = 8_000) -> dict[str, Any]:
    """One board-shaped quote row for the fake data mode — the same field shape
    the sidecar's quote-only channel renders (报价总表 and 只订报价的 extras)."""
    month_suffix = re.compile(r"\d{3,4}$")
    last = 820.0 if item["productId"] == "au" else 3500.0
    pre = round(last / (1 + percent / 100), 2)
    change = round(last - pre, 2)
    return {
        "symbol": item["symbol"], "exchangeId": item["exchangeId"],
        "productId": item["productId"], "instrumentId": item["instrumentId"],
        "instrumentName": item["instrumentName"],
        "productName": month_suffix.sub("", item["instrumentName"]),
        "last": last, "preSettlement": pre, "change": change,
        "changePercent": round(change / pre * 100, 2),
        "volume": volume, "openInterest": open_interest,
        "openInterestChange": -240,
        "high": last + 12.0, "low": last - 12.0,
        "upperLimit": round(last * 1.1, 2), "lowerLimit": round(last * 0.9, 2),
        "bidPrice1": last - 1.0, "askPrice1": last + 1.0,
        "quoteTime": _utc_now(), "status": "normal",
    }


def fake_market_board() -> dict[str, Any]:
    """报价总表 for the fake data mode: the same row shape the sidecar board renders."""
    percents = {"au": 0.85, "rb": -0.42, "i": 1.1, "IF": -0.25}
    volumes = {"au": 12_340, "rb": 98_700, "i": 45_600, "IF": 7_890}
    interests = {"au": 8_000, "rb": 2_450_000, "i": 765_000, "IF": 234_000}
    rows = [
        _fake_board_row(item, percent=percents.get(item["productId"], 0.3),
                        volume=volumes.get(item["productId"], 24_680),
                        open_interest=interests.get(item["productId"], 8_000))
        for item in fake_instrument_catalog()["items"] if item.get("isMain") is True
    ]
    return {"schemaVersion": 1, "capturedAt": _utc_now(), "dataStatus": "simulated",
            "revision": f"fake:{time.time_ns()}", "truncated": False, "rows": rows}


def fake_market_quotes(symbols: Sequence[str]) -> dict[str, Any]:
    """只订报价的 extras (2026-10-03) for the fake data mode: quote-only rows for
    any catalog contract — a non-main included — through the same row builder as
    the board; an unknown symbol answers that symbol alone."""
    catalog = {item["symbol"].upper(): item for item in fake_instrument_catalog()["items"]}
    rows = []
    for symbol in symbols:
        text = str(symbol).strip()
        item = catalog.get(text.upper())
        if item is None:
            rows.append({"symbol": text, "errorCode": "symbol_not_found"})
        else:
            rows.append(_fake_board_row(item))
    return {"schemaVersion": 1, "capturedAt": _utc_now(), "dataStatus": "simulated", "rows": rows}


def fake_market_calendar() -> dict[str, Any]:
    """交易日历 (2026-10-03) for the fake data mode: the weekend rule, the same
    summary shape the sidecar's TqSdk calendar carries."""
    candidate = date.fromisoformat(naive_trading_day(time.time_ns()))
    recent: list[str] = []
    day = candidate - timedelta(days=1)
    while len(recent) < 10:
        if day.weekday() < 5:
            recent.append(day.isoformat())
        day -= timedelta(days=1)
    return {"tradingDay": candidate.isoformat(), "previousTradingDay": recent[0],
            "recentTradingDays": recent}


# 规则回测: how long the Runtime waits for the sidecar to download a fresh history serial, one
# request's own timeout (the history actor waits up to 8 s per request), and the result cache.
HISTORY_READY_SECONDS = 40.0
HISTORY_REQUEST_SECONDS = 20.0
HISTORY_MAX_BARS = 10_000
HISTORY_CACHE_SECONDS = 300.0
# 2026-10-13 内存调查: one 10 000-bar entry is about 5 MiB of Python objects, so 32 entries could pin
# ~160 MiB per provider; 12 still holds every (合约, 周期) a few strategies and monitors read at once.
HISTORY_CACHE_SIZE = 12
# An expired entry keeps answering market_history_nowait (the live monitor's 当日最高 / 最低 only needs
# bars before the live window) while one background read refreshes it; an entry nobody refreshed for
# this much longer is deleted outright instead of being kept until the size bound pushes it out.
HISTORY_CACHE_STALE_GRACE_SECONDS = HISTORY_CACHE_SECONDS


class HistoryCache:
    """(合约, 周期) → the latest full-depth history for HISTORY_CACHE_SECONDS; a request for fewer
    bars is served from it (the most recent `length`). Bounded; the oldest entry goes first. An entry
    older than ttl + stale_grace is deleted on the next access and by a one-shot background sweep
    timer (armed while anything is cached), so an idle Runtime does not keep old series alive."""

    def __init__(self, *, ttl: float = HISTORY_CACHE_SECONDS, size: int = HISTORY_CACHE_SIZE,
                 stale_grace: float = HISTORY_CACHE_STALE_GRACE_SECONDS,
                 clock: Callable[[], float] = time.monotonic, sweep_timer: bool = True) -> None:
        self.ttl, self.size, self.stale_grace, self.clock = ttl, size, stale_grace, clock
        self._items: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._sweep_timer = sweep_timer
        self._timer: threading.Timer | None = None

    def _sweep_locked(self) -> None:
        horizon = self.clock() - self.ttl - self.stale_grace
        for key in [key for key, (stored, _) in self._items.items() if stored < horizon]:
            del self._items[key]

    def _arm_locked(self) -> None:
        if not self._sweep_timer or self._timer is not None or not self._items:
            return
        oldest = min(stored for stored, _ in self._items.values())
        delay = max(1.0, oldest + self.ttl + self.stale_grace - self.clock())
        timer = threading.Timer(delay, self._timer_fired)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _timer_fired(self) -> None:
        with self._lock:
            self._timer = None
            self._sweep_locked()
            self._arm_locked()

    def close(self) -> None:
        """Drop everything and cancel the sweep timer (its callback would otherwise keep this cache
        and its series alive until it fires). A later put arms a new one."""
        with self._lock:
            timer, self._timer = self._timer, None
            self._items.clear()
        if timer is not None:
            timer.cancel()

    def sweep(self) -> None:
        """Delete every entry past ttl + stale_grace now."""
        with self._lock:
            self._sweep_locked()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def get(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any] | None:
        result, fresh = self.peek(symbol, duration_seconds, length)
        return result if fresh else None

    def peek(self, symbol: str, duration_seconds: int, length: int) -> tuple[dict[str, Any] | None, bool]:
        """(the kept history even when expired, whether it is still fresh)."""
        with self._lock:
            self._sweep_locked()
            entry = self._items.get((symbol.upper(), duration_seconds))
            if entry is None:
                return None, False
            fresh = self.clock() - entry[0] <= self.ttl
            result = entry[1]
        return {**result, "bars": result["bars"][-length:]}, fresh

    def put(self, symbol: str, duration_seconds: int, result: dict[str, Any]) -> None:
        with self._lock:
            self._sweep_locked()
            self._items[(symbol.upper(), duration_seconds)] = (self.clock(), result)
            while len(self._items) > self.size:
                self._items.pop(min(self._items, key=lambda key: self._items[key][0]))
            self._arm_locked()


def fake_market_history(symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
    """Deterministic waves ending where the fake chart ends, so a rule backtest trades both ways."""
    market = fake_market_snapshot("", symbol)
    tick = float(market["priceTick"])
    end = 1_788_192_000_000_000_000 + 80 * duration_seconds * 1_000_000_000
    count = max(1, min(int(length), HISTORY_MAX_BARS))
    bars, previous = [], 820.0
    for index in range(count):
        timestamp = end - (count - index) * duration_seconds * 1_000_000_000
        center = 820.0 * (1 + 0.02 * math.sin(index / 37) + 0.008 * math.sin(index / 7.3))
        close = round(center / tick) * tick
        bars.append({"datetime": timestamp, "open": previous,
                     "high": max(previous, close) + tick * (1 + index % 3),
                     "low": min(previous, close) - tick * (1 + index % 2), "close": close,
                     "volume": 100 + index % 50, "openInterest": 8_000 + index % 400,
                     "tradingDay": _fixture_trading_day(timestamp)})
        previous = close
    return {"schemaVersion": 1, "symbol": market["symbol"], "durationSeconds": duration_seconds,
            "priceTick": tick, "volumeMultiple": float(market["volumeMultiple"]), "tradingSchedule": None,
            "dataStatus": "simulated", "bars": bars}


class ReadOnlyDataProvider(Protocol):
    mode: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def subscribe_status(self, subscriber: StatusSubscriber) -> Unsubscribe: ...
    def runtime_status(self) -> dict[str, Any]: ...
    def account_summary(self, turn_id: str) -> dict[str, Any]: ...
    def positions(self) -> list[dict[str, Any]]: ...
    def account_activity(self) -> dict[str, Any]: ...
    def instrument_catalog(self) -> dict[str, Any]: ...
    def market_board(self) -> dict[str, Any]: ...
    def market_quotes(self, symbols: Sequence[str]) -> dict[str, Any]: ...
    def market_calendar(self) -> dict[str, Any]: ...
    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]: ...
    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]: ...


class FakeReadOnlyDataProvider:
    """Deterministic Phase 1 fixture. It has no gateway or order capability."""

    mode = "fake"
    symbols = ("AU2612",)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def subscribe_status(self, subscriber: StatusSubscriber) -> Unsubscribe:
        del subscriber
        return lambda: None

    def runtime_status(self) -> dict[str, Any]:
        return {"accountMode": self.mode, "marketDataStatus": "simulated",
                "accountStatus": "simulated", "tradingGatewayStatus": "disabled",
                "riskStatus": "normal"}

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        return fake_account_summary(turn_id)

    def positions(self) -> list[dict[str, Any]]:
        return [
            {"symbol": "SHFE.au2612", "exchangeId": "SHFE", "instrumentId": "au2612",
             "direction": "long", "volume": 2, "unrealizedPnl": 2670,
             # Today/historical split the way TqKq reports it: a SHFE batch close needs it to build orders.
             "todayVolume": 2, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0,
             # 每手持仓保证金（元），平仓释放保证金的估算用。
             "margin": 164_700.0},
            {"symbol": "SHFE.ag2612", "exchangeId": "SHFE", "instrumentId": "ag2612",
             "direction": "long", "volume": 1, "unrealizedPnl": 750,
             "todayVolume": 0, "historicalVolume": 1, "frozenToday": 0, "frozenHistorical": 0,
             "margin": 12_150.0},
            # 交割月提醒的样例持仓（2026-10 前后：个人账户已不能持仓进 10 月交割月）。
            {"symbol": "SHFE.hc2610", "exchangeId": "SHFE", "instrumentId": "hc2610",
             "direction": "short", "volume": 1, "unrealizedPnl": -310,
             "todayVolume": 0, "historicalVolume": 1, "frozenToday": 0, "frozenHistorical": 0,
             "margin": 3_850.0},
        ]

    def account_activity(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1, "capturedAt": _utc_now(), "ageSeconds": 0,
            "dataStatus": "simulated", "ordersInitialized": True,
            "orders": [], "trades": [], "liveTrading": "disabled",
        }

    def instrument_catalog(self) -> dict[str, Any]:
        return fake_instrument_catalog()

    def market_board(self) -> dict[str, Any]:
        return fake_market_board()

    def market_quotes(self, symbols: Sequence[str]) -> dict[str, Any]:
        return fake_market_quotes(symbols)

    def market_calendar(self) -> dict[str, Any]:
        return fake_market_calendar()

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        return fake_market_snapshot(turn_id, symbol)

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        return fake_market_workbench(symbol, duration_seconds)

    def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        return fake_market_history(symbol, duration_seconds, length)

    def market_history_nowait(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        return fake_market_history(symbol, duration_seconds, length)

    # The three sidecar-only TqSdk queries, synthesized deterministically so the
    # fake data mode exercises the same tool surfaces end to end.
    def symbol_settlements(self, symbols: Sequence[str], days: int) -> dict[str, Any]:
        catalog = fake_instrument_catalog()["items"]
        known = {str(item["symbol"]).upper(): item for item in catalog}
        items: list[dict[str, Any]] = []
        for symbol in symbols:
            item = known.get(str(symbol).upper())
            if item is None:
                raise KeyError(symbol)
            base = 820.0 if item["productId"] == "au" else 3500.0
            for offset in range(days):
                items.append({
                    "symbol": item["symbol"],
                    "tradingDay": (datetime.now(UTC).astimezone(CHINA_TIMEZONE).date()
                                   - timedelta(days=offset)).isoformat(),
                    "settlement": round(base - offset * 1.5, 2),
                })
        return {"schemaVersion": 1, "days": days, "asOf": items[0]["tradingDay"], "items": items}

    def main_contract_history(self, symbol: str, days: int) -> dict[str, Any]:
        cont = str(symbol).removeprefix("KQ.m@")
        exchange, _, product = cont.partition(".")
        rows = [
            {
                "date": (datetime.now(UTC).astimezone(CHINA_TIMEZONE).date()
                         - timedelta(days=days - 1 - index)).isoformat(),
                "mainSymbol": (
                    f"{exchange}.{product}2510" if index < days - 5
                    else f"{exchange}.{product}2610"
                ),
            }
            for index in range(days)
        ]
        return {
            "schemaVersion": 1, "days": days, "contSymbol": f"KQ.m@{exchange}.{product}",
            "rows": rows,
            "rollDates": [
                rows[index]["date"] for index in range(1, len(rows))
                if rows[index]["mainSymbol"] != rows[index - 1]["mainSymbol"]
            ],
        }

    def symbol_ranking(self, symbol: str, ranking_type: str, days: int) -> dict[str, Any]:
        catalog = fake_instrument_catalog()["items"]
        if str(symbol).upper() not in {
            str(item["symbol"]).upper() for item in catalog
        }:
            raise KeyError(symbol)
        brokers = ("东证期货", "国泰君安", "银河期货", "永安期货", "中信期货",
                   "海通期货", "广发期货", "申银万国", "华泰期货", "光大期货")
        rows = [
            {
                "tradingDay": (datetime.now(UTC).astimezone(CHINA_TIMEZONE).date()
                               - timedelta(days=offset)).isoformat(),
                "broker": broker,
                "value": float(24_000 - index * 950),
                "change": float((index % 5 - 2) * 310),
                "rank": index + 1,
            }
            for offset in range(days)
            for index, broker in enumerate(brokers)
        ]
        return {
            "schemaVersion": 1, "symbol": str(symbol), "rankingType": ranking_type,
            "days": days, "asOf": rows[0]["tradingDay"], "rows": rows,
        }


@dataclass(frozen=True)
class TqKqSnapshot:
    account: dict[str, Any]
    positions: list[dict[str, Any]]
    markets: dict[str, dict[str, Any]]
    captured_at_monotonic: float
    captured_at_utc: str
    trading_day: str | None = None


class TqKqSession(Protocol):
    """Narrow snapshot-source boundary; Phase 1.4 IPC client implements this protocol."""

    def read(self, timeout_seconds: float) -> TqKqSnapshot | None: ...
    def close(self) -> None: ...


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _number(value: Any, default: float = 0.0) -> float:
    result = _finite_number(value)
    return default if result is None else result


def _symbol_parts(symbol: str) -> tuple[str, str]:
    exchange, separator, instrument = symbol.partition(".")
    return (exchange, instrument) if separator else ("", exchange)


class OfficialTqKqSession:
    """Owns the TqApi object in the provider thread and exposes read snapshots only."""

    def __init__(self, username: str, password: str, symbols: Sequence[str]) -> None:
        from tqsdk import TqApi, TqAuth, TqKq  # type: ignore[import-not-found]

        self.api = TqApi(TqKq(), auth=TqAuth(username, password),
                         debug=False, disable_print=True)
        self.account = self.api.get_account()
        self.position_entities = self.api.get_position()
        self.quotes = {symbol: self.api.get_quote(symbol) for symbol in symbols}

    def read(self, timeout_seconds: float) -> TqKqSnapshot | None:
        # TqSdk returns False when the deadline expires without business data.
        if self.api.wait_update(deadline=time.time() + timeout_seconds) is not True:
            return None
        fields = {
            "balance": getattr(self.account, "balance", None),
            "staticBalance": getattr(self.account, "static_balance", None),
            "available": getattr(self.account, "available", None),
            "floatProfit": getattr(self.account, "float_profit", None),
            "positionProfit": getattr(self.account, "position_profit", None),
            "closeProfit": getattr(self.account, "close_profit", None),
            "commission": getattr(self.account, "commission", None),
            "margin": getattr(self.account, "margin", None),
        }
        account: dict[str, float] = {}
        for name, value in fields.items():
            number = _finite_number(value)
            if number is None:
                return None
            account[name] = number

        markets: dict[str, dict[str, Any]] = {}
        for symbol, quote_state in self.quotes.items():
            last = _finite_number(getattr(quote_state, "last_price", None))
            bid = _finite_number(getattr(quote_state, "bid_price1", None))
            ask = _finite_number(getattr(quote_state, "ask_price1", None))
            tick = _finite_number(getattr(quote_state, "price_tick", None))
            multiplier = _finite_number(getattr(quote_state, "volume_multiple", None))
            upper = _finite_number(getattr(quote_state, "upper_limit", None))
            lower = _finite_number(getattr(quote_state, "lower_limit", None))
            quote_time = str(getattr(quote_state, "datetime", "") or "").strip()
            if (
                any(
                    value is None
                    for value in (last, tick, multiplier, upper, lower)
                )
                or not quote_time
            ):
                return None
            # 涨跌对昨结算 (#751 B, 2026-10-13), as the sidecar and the 报价总表; 昨收 only without one.
            previous = _finite_number(getattr(quote_state, "pre_settlement", None)) or _finite_number(
                getattr(quote_state, "pre_close", None))
            change = ((last - previous) / previous * 100) if previous else 0.0
            markets[symbol] = {"symbol": symbol,
                               "name": str(getattr(quote_state, "instrument_name", symbol)),
                               "price": last, "changePercent": change,
                               "bidPrice1": bid, "askPrice1": ask,
                               "priceTick": tick, "volumeMultiple": multiplier,
                               "upperLimit": upper, "lowerLimit": lower,
                               "quoteDatetime": quote_time}

        positions: list[dict[str, Any]] = []
        entries = self.position_entities.items() if hasattr(self.position_entities, "items") else ()
        for full_symbol, position in entries:
            symbol = str(full_symbol)
            exchange_id, instrument_id = _symbol_parts(symbol)
            pos_long = int(_number(getattr(position, "pos_long", 0)))
            pos_short = int(_number(getattr(position, "pos_short", 0)))
            if pos_long:
                positions.append({"symbol": symbol, "exchangeId": exchange_id,
                                  "instrumentId": instrument_id, "direction": "long",
                                  "volume": pos_long,
                                  "unrealizedPnl": _number(getattr(position, "float_profit_long", 0))})
            if pos_short:
                positions.append({"symbol": symbol, "exchangeId": exchange_id,
                                  "instrumentId": instrument_id, "direction": "short",
                                  "volume": pos_short,
                                  "unrealizedPnl": _number(getattr(position, "float_profit_short", 0))})
        return TqKqSnapshot(account, positions, markets, time.monotonic(), _utc_now())

    def close(self) -> None:
        self.api.close()


class TqKqReadOnlyProvider:
    """TqKq account/position/quote reader. No order API is exposed by this type."""

    mode = "tqkq-readonly"

    def __init__(
        self, *, username: str | None, password: str | None,
        symbols: Sequence[str] = ("SHFE.au2612", "SHFE.rb2610"),
        session_factory: Callable[[str, str, Sequence[str]], TqKqSession] = OfficialTqKqSession,
        stale_after_seconds: float = 15.0,
    ) -> None:
        self._username, self._password = username, password
        self._symbols, self._session_factory = tuple(symbols), session_factory
        self.symbols = self._symbols
        self._stale_after_seconds = stale_after_seconds
        self._snapshot: TqKqSnapshot | None = None
        self._state = "connecting" if username and password else "unavailable"
        self._error_code = None if username and password else "tqkq_credentials_missing"
        self._lock = threading.Lock()
        self._subscribers: set[StatusSubscriber] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def subscribe_status(self, subscriber: StatusSubscriber) -> Unsubscribe:
        with self._lock:
            self._subscribers.add(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.discard(subscriber)
        return unsubscribe

    def _set_state(self, state: str, error_code: str | None = None) -> None:
        with self._lock:
            if self._state == state and self._error_code == error_code:
                return
            self._state, self._error_code = state, error_code
            subscribers = tuple(self._subscribers)
        status = self._status_without_refresh()
        for subscriber in subscribers:
            try:
                subscriber(status)
            except Exception:
                continue

    async def start(self) -> None:
        if not self._username or not self._password:
            self._set_state("unavailable", "tqkq_credentials_missing")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._set_state("connecting")
        self._thread = threading.Thread(target=self._run, name="ai-trader-tqkq-reader", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 5.0)
        self._thread = None

    def _run(self) -> None:
        session: TqKqSession | None = None
        try:
            with self._lock:
                username, password = self._username or "", self._password or ""
                # The source hands credentials to the dedicated reader once; cached provider/tool
                # objects retain only detached snapshots after initialization.
                self._username = self._password = None
            session = self._session_factory(username, password, self._symbols)
            while not self._stop.is_set():
                snapshot = session.read(1.0)
                if snapshot is None:
                    self._refresh_stale_state()
                    continue
                with self._lock:
                    self._snapshot = snapshot
                self._set_state("normal")
        except Exception as error:
            self._set_state("unavailable", f"tqkq_reader_{type(error).__name__.lower()}")
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def _refresh_stale_state(self) -> None:
        with self._lock:
            state, snapshot = self._state, self._snapshot
        if snapshot is None or state == "unavailable":
            return
        age = time.monotonic() - snapshot.captured_at_monotonic
        self._set_state("stale" if age > self._stale_after_seconds else "normal")

    def _effective_state(self) -> str:
        self._refresh_stale_state()
        with self._lock:
            return self._state

    def _status_without_refresh(self) -> dict[str, Any]:
        with self._lock:
            state, error_code = self._state, self._error_code
        return {"accountMode": self.mode, "marketDataStatus": state,
                "accountStatus": state if state in {"normal", "stale", "connecting"} else "unavailable",
                "tradingGatewayStatus": "read_only" if state in {"normal", "stale"} else "unavailable",
                "riskStatus": "normal" if state == "normal" else "unknown",
                "dataErrorCode": error_code}

    def runtime_status(self) -> dict[str, Any]:
        self._refresh_stale_state()
        return self._status_without_refresh()

    def _current(self) -> tuple[TqKqSnapshot, str, float]:
        state = self._effective_state()
        with self._lock:
            snapshot, error_code = self._snapshot, self._error_code
        if snapshot is None or state in {"connecting", "unavailable"}:
            raise RuntimeError(error_code or "tqkq_snapshot_unavailable")
        return snapshot, state, max(0.0, time.monotonic() - snapshot.captured_at_monotonic)

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        try:
            snapshot, data_status, age = self._current()
        except RuntimeError:
            return {"turnId": turn_id, "accountName": "TqKq 只读账户",
                    "updatedAt": "尚无有效快照", "capturedAt": _utc_now(), "ageSeconds": 0,
                    "dataStatus": "unavailable", "equity": 0, "todayPnl": 0,
                    "todayNetPnl": 0, "floatProfit": 0, "positionProfit": 0,
                    "closeProfit": 0, "commission": 0, "marginRatio": 0,
                    "positionCount": 0, "riskBudgetUsed": 0, "status": "warning",
                    "insight": "TqKq 尚无有效只读快照；这些零值是不可用占位，不是账户数据。",
                    "equityCurve": []}
        account = snapshot.account
        equity = _number(account.get("balance"))
        float_profit = _number(account.get("floatProfit"))
        position_profit = _number(account.get("positionProfit"))
        close_profit = _number(account.get("closeProfit"))
        commission = _number(account.get("commission"))
        today_net_pnl = close_profit + position_profit - commission
        margin = _number(account.get("margin"))
        stale = data_status == "stale"
        return {"turnId": turn_id, "accountName": "TqKq 模拟账户",
                "updatedAt": _age_label(age), "capturedAt": snapshot.captured_at_utc,
                "ageSeconds": round(age, 3), "dataStatus": data_status,
                "equity": equity, "staticBalance": _number(account.get("staticBalance")),
                "todayPnl": today_net_pnl, "todayNetPnl": today_net_pnl,
                "tradingDay": snapshot.trading_day,
                "floatProfit": float_profit, "positionProfit": position_profit,
                "closeProfit": close_profit, "commission": commission,
                "marginRatio": (margin / equity * 100) if equity > 0 else 0,
                "positionCount": len(snapshot.positions), "riskBudgetUsed": 0,
                "status": "warning" if stale else "normal",
                "insight": (f"只读快照已陈旧（{_age_label(age)}）；不得据此确认交易。" if stale
                            else "只读展示 TqKq 账户快照；Runtime 未开放任何委托或撤单能力。"),
                "equityCurve": [equity]}

    def positions(self) -> list[dict[str, Any]]:
        snapshot, _, _ = self._current()
        return [dict(position) for position in snapshot.positions]

    def account_activity(self) -> dict[str, Any]:
        raise RuntimeError("account_activity_requires_sidecar")

    def instrument_catalog(self) -> dict[str, Any]:
        snapshot, data_status, _ = self._current()
        items = []
        for symbol in self._symbols:
            market = snapshot.markets.get(symbol, {})
            exchange_id, instrument_id = _symbol_parts(symbol)
            product_id = "".join(character for character in instrument_id if character.isalpha())
            items.append({
                "symbol": symbol, "exchangeId": exchange_id, "instrumentId": instrument_id,
                "instrumentName": str(market.get("name") or symbol), "productId": product_id,
                "priceTick": market.get("priceTick"),
                "volumeMultiple": market.get("volumeMultiple"),
                "deliveryYear": 0, "deliveryMonth": 0, "expireRestDays": 0,
                "hasNight": False, "isMain": False,
            })
        return {"schemaVersion": 1, "capturedAt": snapshot.captured_at_utc,
                "dataStatus": data_status, "isCached": data_status == "stale", "items": items}

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        snapshot, data_status, age = self._current()
        requested = symbol.upper()
        market = next((value for key, value in snapshot.markets.items()
                       if key.upper() == requested or key.split(".")[-1].upper() == requested), None)
        if market is None:
            raise KeyError(symbol)
        change, price = _number(market.get("changePercent")), _number(market.get("price"))
        stale = data_status == "stale"
        return {"turnId": turn_id, "symbol": str(market["symbol"]), "name": str(market["name"]),
                "price": price, "changePercent": change, "updatedAt": _age_label(age),
                "capturedAt": snapshot.captured_at_utc, "ageSeconds": round(age, 3),
                "dataStatus": data_status,
            "quoteDatetime": market.get("quoteDatetime"),
            "bidPrice1": market.get("bidPrice1"),
            "askPrice1": market.get("askPrice1"),
            "priceTick": market.get("priceTick"),
            "volumeMultiple": market.get("volumeMultiple"),
            "upperLimit": market.get("upperLimit"),
            "lowerLimit": market.get("lowerLimit"),
                "trend": "up" if change > 0 else "down" if change < 0 else "flat",
                "summary": (f"TqKq 只读行情已陈旧（{_age_label(age)}），不得作为交易确认依据。"
                            if stale else "来自 TqKq 的只读行情快照。"),
                "priceCurve": [price]}

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        del symbol, duration_seconds
        raise RuntimeError("market_series_requires_sidecar")

    def market_board(self) -> dict[str, Any]:
        raise RuntimeError("market_board_requires_sidecar")

    def market_quotes(self, symbols: Sequence[str]) -> dict[str, Any]:
        del symbols
        raise RuntimeError("market_quotes_requires_sidecar")

    def market_calendar(self) -> dict[str, Any]:
        raise RuntimeError("market_calendar_requires_sidecar")

    def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        del symbol, duration_seconds, length
        raise RuntimeError("market_series_requires_sidecar")

    # Settlement / main-contract history / ranking need the sidecar's TqApi actor.
    def symbol_settlements(self, symbols: Sequence[str], days: int) -> dict[str, Any]:
        del symbols, days
        raise RuntimeError("settlements_require_sidecar")

    def main_contract_history(self, symbol: str, days: int) -> dict[str, Any]:
        del symbol, days
        raise RuntimeError("cont_history_requires_sidecar")

    def symbol_ranking(self, symbol: str, ranking_type: str, days: int) -> dict[str, Any]:
        del symbol, ranking_type, days
        raise RuntimeError("rankings_require_sidecar")


MARKET_WORKBENCH_CONTRACT_CODES = frozenset({
    "market_series_empty", "market_series_not_ready", "market_quote_not_ready", "market_subscription_limit",
})


class SidecarReadOnlyDataProvider:
    """Owns the authenticated localhost channel to the isolated TqKq process."""

    mode = "tqkq-readonly"

    def _paper_name(self) -> str:
        return "本站模拟 · 美元" if account_currency(getattr(self, "account_id", None)) == "USD" else "本站模拟账户"

    def __init__(
        self, *, base_url: str, token: str, symbols: Sequence[str],
        mode: Literal["fake", "tqkq-readonly", "paper"] = "tqkq-readonly",
        poll_interval_seconds: float = 0.5,
    ) -> None:
        # Paper accounts reach the site-hosted matching engine, which the pool
        # deploys beside the runtime — not necessarily on a 127.0.0.1 address.
        if mode != "paper" and not base_url.startswith("http://127.0.0.1:"):
            raise ValueError("sidecar_must_use_localhost")
        if len(token) < 32:
            raise ValueError("sidecar_token_invalid")
        self.mode = mode
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._http = SidecarHttp(self._base_url, token)
        self._symbols = tuple(symbols)
        self.symbols = self._symbols
        self._history_cache = HistoryCache()
        self._history_fetching: set[tuple[str, int]] = set()
        self._history_fetching_lock = threading.Lock()
        self._poll_interval = poll_interval_seconds
        self._status: dict[str, Any] = {
            "accountMode": self.mode,
            "marketDataStatus": "connecting",
            "accountStatus": "connecting",
            "tradingGatewayStatus": "unavailable",
            "riskStatus": "unknown",
            "dataErrorCode": None,
            "dataConnectionStage": "starting",
            "dataInitializationSeconds": 0.0,
        }
        self._account: dict[str, Any] | None = None
        self._positions: list[dict[str, Any]] | None = None
        self._markets: dict[str, dict[str, Any]] = {}
        self._snapshot_received_monotonic: float | None = None
        self._snapshot_remote_age = 0.0
        self._lock = threading.Lock()
        self._subscribers: set[StatusSubscriber] = set()
        self._snapshot_subscribers: set[Callable[[], None]] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # The last instrument catalog read whole (instrument_catalog): what every reader gets, marked cached, when
        # one read fails (#932 F4).
        self._last_catalog: dict[str, Any] | None = None

    def subscribe_status(self, subscriber: StatusSubscriber) -> Unsubscribe:
        with self._lock:
            self._subscribers.add(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.discard(subscriber)
        return unsubscribe

    def subscribe_snapshot(self, subscriber: Callable[[], None]) -> Unsubscribe:
        """Called from the poll thread after every refreshed account/market snapshot."""
        with self._lock:
            self._snapshot_subscribers.add(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                self._snapshot_subscribers.discard(subscriber)
        return unsubscribe

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
        *, timeout: float | None = None,
    ) -> Any:
        if self._stop.is_set():
            raise SidecarHttpError("sidecar_stopped")
        return self._http.request(method, path, payload,
            timeout=timeout if timeout is not None else 4.0 if method == "POST" else 2.0)

    async def _request_async(self, method, path, payload=None, *, timeout=None):
        if self._stop.is_set():
            raise SidecarHttpError("sidecar_stopped")
        return await self._http.request_async(method, path, payload,
            timeout=timeout if timeout is not None else 4.0 if method == "POST" else 2.0)

    def _fetch(self, path: str) -> Any:
        current = _read_snapshot.get()
        if current and current[0] is self:
            path = path.casefold()
            if path not in current[1]:
                raise RuntimeError("sidecar_read_not_prefetched")
            value, received = current[1][path]
            if isinstance(value, Exception):
                raise value
            if isinstance(value, dict) and "ageSeconds" in value:
                return {**value, "ageSeconds": float(value["ageSeconds"]) + time.monotonic() - received}
            return value
        return self._request("GET", path)

    def _combined_snapshot(self) -> dict[str, Any] | None:
        """The task-local combined account read (#700 review) — trading day / positions /
        activity off ONE sidecar snapshot — or None when this context did not prefetch one
        (every other reader keeps its ordinary path). A failed or malformed prefetch raises
        here so the caller refuses instead of mixing two snapshots' facts."""
        current = _read_snapshot.get()
        if not (current and current[0] is self):
            return None
        entry = current[1].get("/v1/snapshot")
        if entry is None:
            return None
        value, received = current[1]["/v1/snapshot"]
        if isinstance(value, Exception):
            # An old sidecar without the combined read (404), a timeout, any transport
            # failure: the reader refuses — fail closed, never mixed across two snapshots.
            raise RuntimeError("sidecar_account_snapshot_unavailable") from value
        if not isinstance(value, dict) or value.get("schemaVersion") != 1:
            raise RuntimeError("invalid_sidecar_snapshot")
        elapsed = time.monotonic() - received

        def aged(part: Any) -> Any:
            if isinstance(part, dict) and "ageSeconds" in part:
                return {**part, "ageSeconds": float(part["ageSeconds"]) + elapsed}
            return part

        return {"status": value.get("status"), "account": aged(value.get("account")),
                "positions": value.get("positions"), "activity": aged(value.get("activity"))}

    async def _order_request(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await self._request_async(method, path, payload)
        if not isinstance(result, dict):
            raise ValueError("invalid_sidecar_order_report")
        return result

    async def submit_order_once(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._order_request("POST", "/v1/simulation/orders/submit", request)

    async def query_order(self, client_order_id: str) -> dict[str, Any]:
        return await self._order_request(
            "GET", f"/v1/simulation/orders/{quote(client_order_id, safe='')}"
        )

    async def cancel_order_once(
        self, client_order_id: str, command_id: str,
    ) -> dict[str, Any]:
        path = f"/v1/simulation/orders/{quote(client_order_id, safe='')}/cancel"
        return await self._order_request("POST", path, {"commandId": command_id})

    # The settlement / cont-history / ranking queries go straight to the sidecar
    # (never prefetched): they are one-shot reads outside the snapshot refresh.
    def _query_sidecar(self, path: str, *, unavailable_code: str, timeout: float = 12.0) -> dict[str, Any]:
        try:
            result = self._request("GET", path, timeout=timeout)
        except SidecarHttpError as error:
            if error.status == 404:
                raise KeyError(path) from error
            if error.status is not None:
                raise RuntimeError(str(error.payload.get("error") or unavailable_code)) from error
            raise RuntimeError(unavailable_code) from error
        except (TimeoutError, OSError) as error:
            raise RuntimeError(unavailable_code) from error
        if not isinstance(result, dict) or result.get("schemaVersion") != 1:
            raise RuntimeError(f"invalid_{unavailable_code}")
        return result

    def market_history_nowait(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        """The cached history, never waiting: 当日最高 / 最低 in the live monitor, which must not hold
        the event loop while the history session downloads. An expired entry is still answered (it only
        has to reach back before the live window) while one background read refreshes it; with nothing
        kept one background read is started and ``market_series_not_ready`` raised — a later poll of
        the same bar finds it."""
        cached, fresh = self._history_cache.peek(symbol, duration_seconds, length)
        if fresh and cached is not None:
            return cached
        key = (symbol.upper(), duration_seconds)
        with self._history_fetching_lock:
            start = key not in self._history_fetching
            self._history_fetching.add(key)
        if start:
            def fetch() -> None:
                try:
                    self.market_history(symbol, duration_seconds, length)
                except (KeyError, RuntimeError, ValueError):
                    logging.getLogger(__name__).info("session_history_prefetch_failed symbol=%s duration=%s",
                                                     symbol, duration_seconds)
                finally:
                    with self._history_fetching_lock:
                        self._history_fetching.discard(key)
            threading.Thread(target=fetch, name="session-history", daemon=True).start()
        if cached is not None:
            return cached
        raise RuntimeError("market_series_not_ready")

    def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        """规则回测的只读历史 K 线（最多 1 万根），由 sidecar 独立的 history 会话提供（不占实盘订阅）。
        Always fetched at the full depth and cached per (合约, 周期) for HISTORY_CACHE_SECONDS, so the
        same strategy backtested again does not ask the sidecar twice. The first request of a pair
        is answered market_series_not_ready while TqSdk downloads: retried here briefly."""
        cache = self._history_cache
        cached = cache.get(symbol, duration_seconds, length)
        if cached is not None:
            return cached
        path = f"/v1/market/{quote(symbol, safe='.')}/history?duration={duration_seconds}&length={HISTORY_MAX_BARS}"
        deadline = time.monotonic() + HISTORY_READY_SECONDS
        while True:
            try:
                result = self._query_sidecar(path, unavailable_code="sidecar_market_history_unavailable",
                                             timeout=HISTORY_REQUEST_SECONDS)
                break
            except KeyError as error:
                raise KeyError(symbol) from error
            except RuntimeError as error:
                if str(error) != "market_series_not_ready" or time.monotonic() >= deadline:
                    raise
                time.sleep(1.0)
        if not isinstance(result.get("bars"), list):
            raise RuntimeError("invalid_sidecar_market_history")
        cache.put(symbol, duration_seconds, result)
        return cache.get(symbol, duration_seconds, length) or result

    def symbol_settlements(self, symbols: Sequence[str], days: int) -> dict[str, Any]:
        joined = ",".join(quote(str(item), safe=".") for item in symbols)
        return self._query_sidecar(
            f"/v1/settlements?symbols={joined}&days={days}",
            unavailable_code="sidecar_settlements_unavailable",
        )

    def main_contract_history(self, symbol: str, days: int) -> dict[str, Any]:
        cont = quote(str(symbol), safe="@.")
        result = self._query_sidecar(
            f"/v1/cont-history?symbols={cont}&days={days}",
            unavailable_code="sidecar_cont_history_unavailable",
        )
        rows = result.get("symbols", {}).get(str(symbol))
        if not isinstance(rows, list):
            raise RuntimeError("invalid_sidecar_cont_history")
        roll_dates = result.get("rollDates", {}).get(str(symbol), [])
        return {
            "schemaVersion": 1, "days": days, "contSymbol": str(symbol),
            "rows": rows, "rollDates": roll_dates if isinstance(roll_dates, list) else [],
        }

    def symbol_ranking(self, symbol: str, ranking_type: str, days: int) -> dict[str, Any]:
        return self._query_sidecar(
            f"/v1/rankings?symbol={quote(str(symbol), safe='.')}"
            f"&type={quote(str(ranking_type), safe='')}&days={days}",
            unavailable_code="sidecar_ranking_unavailable",
        )

    def historical_backtest(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._request(
                "POST", "/v1/backtests/tqsdk", request, timeout=130.0,
            )
        except SidecarHttpError as error:
            if error.status is not None:
                raise RuntimeError(str(error.payload.get("error") or "tqsdk_backtest_failed")) from error
            raise RuntimeError("tqsdk_backtest_unavailable") from error
        except (TimeoutError, OSError) as error:
            raise RuntimeError("tqsdk_backtest_unavailable") from error
        if (
            not isinstance(result, dict)
            or result.get("accountType") != "TqSim"
            or result.get("executionEnabled") is not False
            or result.get("orderCapabilities") != []
            or not isinstance(result.get("baseline"), dict)
            or not isinstance(result.get("candidate"), dict)
            or not isinstance(result.get("bars"), list)
            or not isinstance(result.get("dataDigest"), str)
        ):
            raise RuntimeError("invalid_tqsdk_backtest_result")
        return result

    def _publish_status(self, next_status: dict[str, Any]) -> None:
        with self._lock:
            if self._status == next_status:
                return
            self._status = next_status
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber(dict(next_status))
            except Exception:
                continue

    async def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="readonly-sidecar-client", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 5.0)
        self._thread = None
        # Remove the only Runtime-held copy of the IPC token after shutdown.
        await self._http.close()
        self._token = ""
        # A stopped (detached) account's cached series and its sweep timer go now, not ten minutes later.
        self._history_cache.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._poll_once(self._fetch)
            self._stop.wait(self._poll_interval)

    def _poll_once(self, fetch: Callable[[str], Any], *, read_unavailable: bool = False) -> None:
        """One status/account/positions refresh through `fetch` (this provider's
        own reads, or a paper account's slice of the shared batch read)."""
        try:
            sidecar_status = fetch("/v1/status")
            data_status = str(sidecar_status.get("dataStatus") or "unavailable")
            if data_status not in {"simulated", "connecting", "normal", "stale", "unavailable"}:
                raise ValueError("invalid_sidecar_status")
            account_status = str(sidecar_status.get("accountStatus") or data_status)
            market_status = str(sidecar_status.get("marketDataStatus") or data_status)
            valid_statuses = {"simulated", "connecting", "normal", "stale", "unavailable"}
            if account_status not in valid_statuses or market_status not in valid_statuses:
                raise ValueError("invalid_sidecar_component_status")
            gateway = (
                "disabled"
                if self.mode == "fake"
                else "simulation" if self.mode == "paper" and account_status in {"simulated", "normal", "stale"}
                else "read_only" if account_status in {"simulated", "normal", "stale"}
                else "unavailable"
            )
            next_status = {
                "accountMode": self.mode,
                "marketDataStatus": market_status,
                "accountStatus": account_status,
                "tradingGatewayStatus": gateway,
                "riskStatus": "normal" if (
                    account_status in {"simulated", "normal"}
                    and market_status in {"simulated", "normal"}
                ) else "unknown",
                "dataErrorCode": sidecar_status.get("dataErrorCode"),
                "dataConnectionStage": sidecar_status.get("dataConnectionStage"),
                "dataStartupTimings": sidecar_status.get("dataStartupTimings"),
                "dataStartupTotalSeconds": sidecar_status.get("dataStartupTotalSeconds"),
                # Connection stage changes are published immediately. The elapsed
                # counter is bucketed so it cannot create an SQLite/SSE event every
                # 500 ms for every open session while TqSdk cold-starts.
                "dataInitializationSeconds": _initialization_progress_bucket(
                    sidecar_status.get("dataInitializationSeconds")
                ),
            }
            market_session = sidecar_status.get("marketSessionStatus")
            if market_session in {"trading", "closed", "unknown"}:
                next_status["marketSessionStatus"] = market_session
            if data_status in {"simulated", "normal", "stale"} or read_unavailable:
                account = fetch("/v1/account")
                positions = fetch("/v1/positions")
                markets = {
                    symbol: fetch(f"/v1/market/{quote(symbol, safe='.')}")
                    for symbol in self._symbols
                }
                if not isinstance(account, dict) or not isinstance(positions, list):
                    raise ValueError("invalid_sidecar_snapshot")
                if read_unavailable and account_status in {"connecting", "unavailable"}:
                    # Paper can read its ledger without quotes. Publish the
                    # unavailable gateway before notifying snapshot consumers.
                    self._publish_status(next_status)
                with self._lock:
                    self._account = account
                    self._positions = positions
                    self._markets = markets
                    self._snapshot_received_monotonic = time.monotonic()
                    self._snapshot_remote_age = max(
                        _number(sidecar_status.get("ageSeconds")),
                        _number(account.get("ageSeconds")),
                    )
                    snapshot_subscribers = tuple(self._snapshot_subscribers)
                for subscriber in snapshot_subscribers:
                    subscriber()
            self._publish_status(next_status)
        except (TimeoutError, OSError, ValueError) as error:
            self._publish_status({
                "accountMode": self.mode,
                "marketDataStatus": "unavailable",
                "accountStatus": "unavailable",
                "tradingGatewayStatus": "unavailable",
                "riskStatus": "unknown",
                "dataErrorCode": f"readonly_sidecar_{type(error).__name__.lower()}",
                "dataConnectionStage": "failed",
                "dataInitializationSeconds": None,
            })

    def runtime_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _local_snapshot_age(self) -> float:
        with self._lock:
            received = self._snapshot_received_monotonic
            remote_age = self._snapshot_remote_age
        if received is None:
            return 0.0
        return max(0.0, remote_age + time.monotonic() - received)

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        combined = self._combined_snapshot()
        if combined is not None:
            # 同一份采集事实 (#700 review): inside an account snapshot the day comes from
            # that one combined read, never the poll cache beside another read's activity.
            account = combined["account"]
            if not isinstance(account, dict):
                raise RuntimeError("invalid_sidecar_snapshot")
            data_status = str(account.get("dataStatus") or "unavailable")
            return {**account, "turnId": turn_id, "dataStatus": data_status,
                    "isCached": data_status in {"connecting", "unavailable", "stale"}}
        with self._lock:
            account = dict(self._account) if self._account is not None else None
            data_status = str(self._status["accountStatus"])
        if account is None:
            return {
                "turnId": turn_id,
                "accountName": self._paper_name() if self.mode == "paper" else "TqKq 隔离只读账户",
                "currency": account_currency(getattr(self, "account_id", None)),
                "updatedAt": "尚无有效快照",
                "capturedAt": _utc_now(),
                "ageSeconds": 0,
                "dataStatus": "unavailable",
                "equity": 0,
                "todayPnl": 0,
                "todayNetPnl": 0,
                "floatProfit": 0,
                "positionProfit": 0,
                "closeProfit": 0,
                "commission": 0,
                "marginRatio": 0,
                "available": 0,
                "margin": 0,
                "riskRatio": 0,
                "positionCount": 0,
                "riskBudgetUsed": 0,
                "status": "warning",
                "insight": "本站模拟撮合引擎尚无有效快照；零值不是账户事实。" if self.mode == "paper"
                else "隔离 Sidecar 尚无有效快照；零值不是账户事实。",
                "equityCurve": [],
            }
        age = self._local_snapshot_age()
        unavailable = data_status in {"connecting", "unavailable"}
        return {
            "turnId": turn_id,
            # 本站模拟账户的名字是产品口径，不透传引擎内部命名。
            "accountName": self._paper_name() if self.mode == "paper"
            else str(account.get("accountName") or "TqKq 隔离只读账户"),
            # 外盘二期：美元子账户的金额是美元，账户卡据此换币种。
            "currency": account_currency(getattr(self, "account_id", None)),
            "updatedAt": _age_label(age),
            "capturedAt": str(account.get("capturedAt") or _utc_now()),
            "tradingDay": account.get("tradingDay"),
            "ageSeconds": age,
            "dataStatus": data_status,
            "isCached": unavailable,
            "equity": _number(account.get("equity")),
            "staticBalance": _number(account.get("staticBalance")) if account.get("staticBalance") is not None else None,
            "todayPnl": _number(account.get("todayPnl")),
            "todayNetPnl": _number(account.get("todayNetPnl")),
            "floatProfit": _number(account.get("floatProfit")),
            "positionProfit": _number(account.get("positionProfit")),
            "closeProfit": _number(account.get("closeProfit")),
            "commission": _number(account.get("commission")),
            "marginRatio": max(0.0, _number(account.get("marginRatio"))),
            # 剩余可用资金与风险度（百分数）；旧版 sidecar 未上报时按 0 透出，与其它金额字段同口径。
            "available": _number(account.get("available")),
            "margin": _number(account.get("margin")),
            "riskRatio": max(0.0, _number(account.get("riskRatio"))),
            "positionCount": max(0, int(_number(account.get("positionCount")))),
            # 本站模拟引擎挂起中的到期结算（#527 最终结算日协议）：待最后交易日结算价 / 暂估待核，
            # 原样透传给界面与 AI；快期账户没有这一项。
            "expirySettlements": [dict(item) for item in account.get("expirySettlements") or []
                                  if isinstance(item, dict)],
            "riskBudgetUsed": 0,
            "status": "warning" if data_status in {"stale", "connecting", "unavailable"} else "normal",
            "insight": (
                "隔离 Sidecar 已失联；以下仅为缓存快照，不是当前账户事实。"
                if unavailable and self.mode != "paper"
                else "本站模拟撮合引擎已失联；以下仅为缓存快照，不是当前账户事实。"
                if unavailable
                else
                "隔离 Sidecar 快照已陈旧，不得据此确认交易。"
                if data_status == "stale" and self.mode != "paper"
                else "本站模拟快照已陈旧，不得据此确认交易。"
                if data_status == "stale"
                else "合成假数据来自隔离 Sidecar，不代表真实账户。"
                if self.mode == "fake"
                else "账户快照来自本站模拟撮合引擎；委托在本站撮合，实盘永久关闭。"
                if self.mode == "paper"
                else "账户快照来自隔离 Sidecar；模拟执行权限单独管理，实盘永久关闭。"
            ),
            "equityCurve": [_number(account.get("equity"))],
        }

    def positions(self) -> list[dict[str, Any]]:
        combined = self._combined_snapshot()
        if combined is not None:  # the same combined read the day / activity came from
            positions = combined["positions"]
            if not isinstance(positions, list):
                raise RuntimeError("invalid_sidecar_snapshot")
            return [dict(item) for item in positions]
        with self._lock:
            data_status = str(self._status["accountStatus"])
            if self._positions is None or data_status in {"connecting", "unavailable"}:
                raise RuntimeError("readonly_sidecar_snapshot_unavailable")
            return [dict(item) for item in self._positions]

    def account_activity(self) -> dict[str, Any]:
        combined = self._combined_snapshot()
        if combined is not None:
            result = combined["activity"]
        else:
            try:
                result = self._fetch("/v1/activity")
            except SidecarHttpError as error:
                raise RuntimeError("sidecar_account_activity_unavailable") from error
        if (
            not isinstance(result, dict)
            or result.get("schemaVersion") != 1
            or not isinstance(result.get("orders"), list)
            or not isinstance(result.get("trades"), list)
        ):
            raise RuntimeError("invalid_sidecar_account_activity")
        return result

    def instrument_catalog(self) -> dict[str, Any]:
        """The sidecar's instrument catalog — the one read every Runtime reader of it shares (the order planner, the
        router's product names, the decision context, the routes). A read that fails (a timeout, a malformed answer,
        a catalog still loading) serves the last catalog read whole, marked `isCached` — the catalog changes once a
        trading day, so the last one still names the contracts, and freshness checks see the mark (#932 F4: one
        failed read used to become "no catalog", and "卖一手白银" went on as the raw word and came back
        没找到叫“白银”的合约). Only a provider that has never read one raises."""
        try:
            result = self._fetch("/v1/instruments")
            if (
                not isinstance(result, dict)
                or result.get("schemaVersion") != 1
                or not isinstance(result.get("items"), list)
            ):
                raise RuntimeError("invalid_sidecar_instrument_catalog")
        except (OSError, RuntimeError, ValueError) as error:  # SidecarHttpError is an OSError
            last = self._last_catalog
            if last is None:
                if isinstance(error, RuntimeError):
                    raise
                raise RuntimeError("sidecar_instrument_catalog_unavailable") from error
            logging.getLogger(__name__).info("instrument_catalog_read_failed reason=%s served=last", error)
            return {**last, "isCached": True}
        if result["items"]:
            self._last_catalog = result
        elif self._last_catalog is not None:
            return {**self._last_catalog, "isCached": True}  # an empty answer (still loading) is not a catalog
        return result

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        requested = symbol.upper()
        with self._lock:
            market = next((dict(value) for key, value in self._markets.items()
                           if key.upper() == requested or key.split(".")[-1].upper() == requested), None)
            data_status = str(self._status["marketDataStatus"])
        if data_status in {"connecting", "unavailable"}:
            raise RuntimeError("readonly_sidecar_snapshot_unavailable")
        current = _read_snapshot.get()
        if current and current[0] is self:
            market = None  # Use the fresh, task-local quote, including its age.
        if market is None:
            try:
                market = self._fetch(f"/v1/market/{quote(symbol, safe='.')}/quote")
            except SidecarHttpError as error:
                if error.status == 404:
                    raise KeyError(symbol) from error
                raise RuntimeError("sidecar_market_quote_unavailable") from error
            except (TimeoutError, OSError) as error:
                raise RuntimeError("sidecar_market_quote_unavailable") from error
            if not isinstance(market, dict) or not market.get("symbol"):
                raise RuntimeError("invalid_sidecar_market_quote")
            data_status = str(market.get("dataStatus") or data_status)
        # Missing contract quotes do not invalidate the account, nor become zero prices.
        if market.get("price") is None:
            raise RuntimeError("sidecar_market_quote_unavailable")
        if data_status in {"normal", "simulated"}:
            data_status = str(market.get("dataStatus") or data_status)
        change = _number(market.get("changePercent"))
        price = _number(market.get("price"))
        age = (
            max(0.0, _number(market.get("ageSeconds")))
            if market.get("ageSeconds") is not None
            else self._local_snapshot_age()
        )
        return {
            "turnId": turn_id,
            "symbol": str(market.get("symbol") or symbol),
            "name": str(market.get("name") or symbol),
            "price": price,
            "changePercent": change,
            "updatedAt": _age_label(age),
            "capturedAt": str(market.get("capturedAt") or _utc_now()),
            "ageSeconds": age,
            "dataStatus": data_status,
            **{key: market.get(key) for key in (
                "quoteDatetime", "priceTick",
                "volumeMultiple", "upperLimit", "lowerLimit", "marketSessionStatus",
                "tradingStatus", "tradingStatusLabel", "openLimit",
                # 每手保证金/手续费（交易所标准参考，缺数据时为 None）。
                "marginPerLot", "commissionPerLot",
                # 五档盘口：整档透传，缺档由 sidecar 归一成 None。
                "bidPrice1", "bidVolume1", "askPrice1", "askVolume1",
                "bidPrice2", "bidVolume2", "askPrice2", "askVolume2",
                "bidPrice3", "bidVolume3", "askPrice3", "askVolume3",
                "bidPrice4", "bidVolume4", "askPrice4", "askVolume4",
                "bidPrice5", "bidVolume5", "askPrice5", "askVolume5",
            )},
            "trend": "up" if change > 0 else "down" if change < 0 else "flat",
            "summary": (
                "隔离 Sidecar 行情已陈旧，不得作为交易确认依据。"
                if data_status == "stale"
                else "来自隔离 Sidecar 的合成假行情。"
                if self.mode == "fake"
                else "来自本站模拟账户的行情快照，撮合与行情同源。"
                if self.mode == "paper"
                else "来自隔离 Sidecar 的只读行情快照。"
            ),
            "priceCurve": [price],
        }

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        try:
            result = self._fetch(
                f"/v1/market/{quote(symbol, safe='.')}/workbench?duration={duration_seconds}"
            )
        except SidecarHttpError as error:
            if error.status == 404:
                raise KeyError(symbol) from error
            code = error.payload.get("error", "sidecar_transport_unavailable")
            # Diagnostics keep the upstream code, never its arbitrary payload.
            safe_code = code if isinstance(code, str) and len(code) <= 100 and code.replace("_", "").isascii() and code.replace("_", "").isalnum() else "sidecar_response_error"
            logging.getLogger("uvicorn.error.agent").warning(
                "market_workbench_unavailable status=%s code=%s", error.status, safe_code)
            # A per-contract condition is worth telling the client apart from a lost sidecar:
            # a contract with no trades since listing, no quote yet, or the subscription limit.
            if safe_code in MARKET_WORKBENCH_CONTRACT_CODES:
                raise RuntimeError(safe_code) from error
            raise RuntimeError("sidecar_market_workbench_unavailable") from error
        if (
            not isinstance(result, dict)
            or result.get("schemaVersion") != 1
            or not isinstance(result.get("quote"), dict)
            or not isinstance(result.get("bars"), list)
        ):
            raise RuntimeError("invalid_sidecar_market_workbench")
        return result

    def market_board(self) -> dict[str, Any]:
        """报价总表: the isolated sidecar's quote-only main-contract table."""
        try:
            result = self._fetch("/v1/public-market/board")
        except SidecarHttpError as error:
            raise RuntimeError("sidecar_market_board_unavailable") from error
        if (
            not isinstance(result, dict)
            or result.get("schemaVersion") != 1
            or not isinstance(result.get("rows"), list)
        ):
            raise RuntimeError("invalid_sidecar_market_board")
        return result

    def market_quotes(self, symbols: Sequence[str]) -> dict[str, Any]:
        """只订报价的 extras 通道 (2026-10-03): quote-only rows for any catalog
        contract — a non-main included — never a kline serial. An unknown symbol
        or a capped request answers that symbol with an errorCode row."""
        path = "/v1/public-market/quotes?symbols=" + ",".join(
            str(symbol).strip() for symbol in symbols if str(symbol).strip())
        try:
            result = self._fetch(path)
        except SidecarHttpError as error:
            raise RuntimeError("sidecar_market_quotes_unavailable") from error
        if (
            not isinstance(result, dict)
            or result.get("schemaVersion") != 1
            or not isinstance(result.get("rows"), list)
        ):
            raise RuntimeError("invalid_sidecar_market_quotes")
        return result

    def market_calendar(self) -> dict[str, Any]:
        """交易日历摘要 (2026-10-03): the replay's default trading day and the
        休市 refusal, straight from the sidecar's TqSdk calendar."""
        try:
            result = self._fetch("/v1/public-market/calendar")
        except SidecarHttpError as error:
            raise RuntimeError("sidecar_market_calendar_unavailable") from error
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("tradingDay"), str)
            or not isinstance(result.get("previousTradingDay"), str)
            or not isinstance(result.get("recentTradingDays"), list)
        ):
            raise RuntimeError("invalid_sidecar_market_calendar")
        return result

    async def public_market(self, path: str) -> dict[str, Any]:
        """公共行情接力 (2026-10-04): one raw /v1/public-market read for the relay —
        the main sidecar account serving as a public source goes through its own
        provider instead of the account pool. The sidecar's error code (limit,
        cold series) is preserved for the relay's classification; an unknown or
        non-public path is refused rather than proxied."""
        if not isinstance(path, str) or not path.startswith("/v1/public-market/") or ".." in path:
            raise RuntimeError("public_market_path_required")
        try:
            # Async with the pool client's 15 s: a workbench long-poll waits up to 10 s, which
            # neither the 2 s GET default nor a borrowed worker thread per channel can carry. A long
            # poll (`waitMs=`) rides SidecarHttp's own long-poll pool, apart from the short reads.
            result = await self._request_async("GET", path, timeout=15.0)
        except SidecarHttpError as error:
            code = error.payload.get("error", "sidecar_public_market_unavailable")
            # Diagnostics keep the upstream code, never its arbitrary payload.
            safe_code = code if isinstance(code, str) and len(code) <= 100 and code.replace("_", "").isascii() and code.replace("_", "").isalnum() else "sidecar_response_error"
            raise RuntimeError(safe_code) from error
        if not isinstance(result, dict):
            raise RuntimeError("invalid_sidecar_public_market")
        return result

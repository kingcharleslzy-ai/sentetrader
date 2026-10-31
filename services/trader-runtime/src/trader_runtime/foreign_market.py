"""外盘行情的唯一入口（2026-10-18，#976 第 2 步）：报价页“外盘”分组、AI 的外盘查询与调研“外盘联动”
都读这里，不再各自去取。

品种与各源代码见 ``instruments.FOREIGN_PRODUCTS``。数据源按 ``AI_TRADER_FOREIGN_SOURCES``（逗号分隔，
默认 ``sina,kqd``；可选 ``sina`` 新浪外盘、``mt5`` 4090 上的 MT5 只读行情桥、``kqd`` 快期外盘主连）的顺序排；
每个品种整行取自排在最前、有有效最新价的那一个源，行上写明 ``source``，不同源的字段从不拼在一行里（快期主连
没有昨结算，它那一行的涨跌就是空的，不拿别的源补）。MT5 一行整行来自该品种的主力月份合约（``contract``）。
所有者 2026-10-19 定：MT5 排在最前（生产 ``mt5,sina,kqd``，此前 10-18 起为 ``sina,mt5,kqd``）；顺序只由 ``.env.production`` 决定，代码默认值不变。

一次读取取全部品种，经 ``collection.Collector``（独立车道 ``foreign``）同一时刻只取一次、几秒内共用；
源失败时保留上次好值标 stale。外盘行情只供看盘、调研与 AI 参考，不进下单、止损、提醒与成交核验。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Coroutine, Iterable
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Protocol

import httpx

from .collection import Collector
from .host_throttle import GENTLE, HostThrottle
from .instruments import (
    FOREIGN_PRODUCTS,
    FOREIGN_TRADE_EXCHANGE,
    ForeignProduct,
    cme_hours_of,
    foreign_for_domestic,
    foreign_product_of,
    foreign_tradable,
    foreign_trade_name,
    foreign_trade_symbol,
)
from .last_good import LastGood
from .secret_values import secret_value
from .sina_market import aggregate_bars
from .tls import shared_ssl_context
from .trading_calendar import CST, NEW_YORK, cme_rule, cme_schedule, cme_session, cme_trading_day

log = logging.getLogger(__name__)

ENV_SOURCES = "AI_TRADER_FOREIGN_SOURCES"
DEFAULT_SOURCES = ("sina", "kqd")
QUOTE_TTL_SECONDS = 3.0
RETRY_SECONDS = 5.0  # a failed or partial read (a partial one still serves rows) is read again this soon
WAIT_SECONDS = 10.0
SOURCE_TIMEOUT_SECONDS = 8.0
UNAVAILABLE = "foreign_market_unavailable"
SINA_URL = "https://hq.sinajs.cn/list="
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
SOURCE_LABELS = {"sina": "新浪外盘", "kqd": "快期外盘主连", "mt5": "MT5（AMP）"}
# 外盘图表（所有者 2026-10-19 定方案 A）：分钟到小时线由 MT5 主力月份合约画（整张图一个源），MT5 没有的周期
# 由它的更细周期合成；周线和 MT5 不通时整张图仍是快期主连（共享行情照旧）。日线（外盘尾项 2026-10-21）也由 MT5
# 画：快期外盘主连的日线按国内交易日历切（北京 18:00 换日，国内长假几天并成一根），MT5 的 D1 按 UTC 自然日切，
# 都不是 CME 的交易日——日线由该月份的小时线按 CME 交易日（``cme_trading_day``，含节假日）合成。
DAILY = 86400
CHART_TIMEFRAMES = {60: ("M1", 1), 180: ("M1", 3), 300: ("M5", 1), 900: ("M15", 1), 1800: ("M15", 2), 3600: ("H1", 1),
                    DAILY: ("H1", 24)}
CHART_BARS = {60: 600}  # 分时 needs a whole session; other periods carry what the sidecar does
CHART_DEFAULT_BARS = 256
MT5_MAX_BARS = 10_000  # the bridge's per-request cap (mt5_bridge.terminal.MAX_BARS)
CHART_SOURCE_PREFIX = "MT5"
CHART_SESSION_QUOTE_SECONDS = 30 * 60  # a main-month quote older than this reads as a closed market
# A month's whole hourly history (the daily chart's source, at most the bridge's cap) is read again this often;
# between, each read takes only its last MT5_HOURLY_TAIL hours.
MT5_HOURLY_REFRESH_SECONDS = 1800.0
MT5_HOURLY_TAIL = 48
NS = 1_000_000_000
NOTE = "外盘为海外交易所的参考行情，交易单位与国内合约不同，不能直接比价；只供看盘与调研，不作下单依据。"


class ForeignSource(Protocol):
    name: str

    def quotes(self, products: list[ForeignProduct]) -> dict[str, dict[str, Any]]:
        """One read of every product this source carries: key → row (only rows with a valid last price)."""
        ...


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _price(value: Any) -> float | None:
    """A price field: the sources' sentinels (0 / negative = 没有) are absent, never a price."""
    number = _number(value)
    return number if number is not None and number > 0 else None


def _row(product: ForeignProduct, source: str, *, last: float, pre_settlement: float | None = None,
         open_: float | None = None, high: float | None = None, low: float | None = None, bid: float | None = None,
         ask: float | None = None, volume: float | None = None, open_interest: float | None = None,
         quote_time: str | None = None) -> dict[str, Any]:
    change = round(last - pre_settlement, 6) if pre_settlement else None
    return {
        "key": product.key, "name": product.name, "exchange": product.exchange, "currency": product.currency,
        "unit": product.unit, "last": last, "preSettlement": pre_settlement, "change": change,
        # A percentage of a negative base (MT5 can settle energy below zero) would flip the sign: the change stays,
        # the percentage is empty (#932). A zero is every source's “no settlement” (MT5 reports a missing price
        # as 0.0), so it never reaches here as a base.
        "changePercent": round(change / pre_settlement * 100, 2) if change is not None and pre_settlement is not None and pre_settlement > 0 else None,
        "open": open_, "high": high, "low": low, "bidPrice1": bid, "askPrice1": ask,
        "volume": volume, "openInterest": open_interest, "quoteTime": quote_time, "source": source,
        "decimals": product.decimals, "domestic": list(product.domestic),
        "chartSymbol": product.kqd,  # the chart opened from this row (报价页外盘行可点，所有者 2026-10-19)
    }


def parse_sina(text: str, products: Iterable[ForeignProduct]) -> dict[str, dict[str, Any]]:
    """``var hq_str_hf_GC="last,,bid,ask,high,low,HH:MM:SS,pre_settle,open,oi,…,date,name,…";`` per code.
    Fields as AKShare ``futures_foreign_commodity_realtime`` reads them; the time is Beijing time. Prices are
    converted to the catalog's unit (``sina_scale``: Sina quotes COMEX copper in cents per pound)."""
    by_code = {item.sina: item for item in products if item.sina}
    rows: dict[str, dict[str, Any]] = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("var hq_str_hf_") or "=" not in line:
            continue
        code = line[len("var hq_str_hf_"):line.index("=")]
        product = by_code.get(code)
        fields = line.split("=", 1)[1].strip().strip('"').split(",")
        if product is None or len(fields) < 13:
            continue
        scale = product.sina_scale

        def price(index: int, scale: float = scale) -> float | None:
            value = _price(fields[index])
            return round(value * scale, 6) if value is not None else None

        last = price(0)
        if last is None:
            continue
        quote_time = None
        try:
            quote_time = datetime.strptime(f"{fields[12]} {fields[6]}", "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=CST).isoformat()
        except ValueError:
            pass
        held = _number(fields[9])
        rows[product.key] = _row(product, "sina", last=last, pre_settlement=price(7), open_=price(8), high=price(4),
                                 low=price(5), bid=price(2), ask=price(3), open_interest=held if held else None,
                                 quote_time=quote_time)
    return rows


@dataclass
class SinaForeignSource:
    name: str = "sina"
    throttle: HostThrottle = GENTLE
    fetch_text: Callable[[str], str] | None = None

    def _get(self, url: str) -> str:
        if self.fetch_text is not None:
            return self.fetch_text(url)
        with self.throttle.turn("hq.sinajs.cn"), httpx.Client(timeout=SOURCE_TIMEOUT_SECONDS, headers=SINA_HEADERS,
                                                               verify=shared_ssl_context()) as client:
            response = client.get(url)
        response.raise_for_status()
        return response.content.decode("gbk", errors="replace")

    def quotes(self, products: list[ForeignProduct]) -> dict[str, dict[str, Any]]:
        codes = [item.sina for item in products if item.sina]
        return parse_sina(self._get(SINA_URL + ",".join(f"hf_{code}" for code in codes)), products) if codes else {}


@dataclass
class KqdForeignSource:
    """快期合约目录里的外盘主连，经共享行情的补充报价通道读（每次至多 20 个，见 shared_market.quotes）。
    快期这些主连没有昨结算、买一卖一给的是无效哨兵、持仓量恒为 0：这几项如实为空。"""

    quotes_reader: Callable[[list[str]], Coroutine[Any, Any, dict[str, Any]]] | None = None
    loop: asyncio.AbstractEventLoop | None = None
    name: str = "kqd"

    def quotes(self, products: list[ForeignProduct]) -> dict[str, dict[str, Any]]:
        if self.quotes_reader is None or self.loop is None or self.loop.is_closed():
            raise RuntimeError("foreign_kqd_not_bound")
        by_symbol = {item.kqd: item for item in products if item.kqd}
        symbols = list(by_symbol)
        rows: dict[str, dict[str, Any]] = {}
        for start in range(0, len(symbols), 20):
            future = asyncio.run_coroutine_threadsafe(self.quotes_reader(symbols[start:start + 20]), self.loop)
            try:
                snapshot = future.result(timeout=SOURCE_TIMEOUT_SECONDS)
            except FutureTimeout:
                future.cancel()
                raise
            if snapshot.get("dataStatus") != "normal":
                continue  # backup / unavailable: Sina's domestic backup carries no foreign rows anyway
            for item in snapshot.get("rows") or []:
                product = by_symbol.get(str(item.get("symbol")))
                last = _price(item.get("last")) if isinstance(item, dict) else None
                if product is None or last is None:
                    continue
                volume = _number(item.get("volume"))
                rows[product.key] = _row(product, "kqd", last=last, pre_settlement=_price(item.get("preSettlement")),
                                         high=_price(item.get("high")), low=_price(item.get("low")),
                                         bid=_price(item.get("bidPrice1")), ask=_price(item.get("askPrice1")),
                                         volume=volume if volume else None, quote_time=item.get("quoteTime") or None)
        return rows


MT5_URL_ENV = "AI_TRADER_MT5_BRIDGE_URL"
MT5_TOKEN_ENV = "AI_TRADER_MT5_BRIDGE_TOKEN"  # or AI_TRADER_MT5_BRIDGE_TOKEN_FILE (secret_values)
MT5_TIMEOUT_SECONDS = 4.0
MT5_CONTRACTS_TTL_SECONDS = 1800.0  # the contract list (names, expiries) changes only on listings
MT5_CONTRACTS_RETRY_SECONDS = 60.0  # a list without any catalog month (empty 200) is asked again this soon
MT5_QUOTE_BATCH = 50          # the bridge's /v1/quotes answers at most this many symbols per request
MT5_CANDIDATES = 3            # nearest live months per product that are quoted to pick the main one
MT5_EXPIRY_GUARD_DAYS = 3     # a month in its last 3 days is never the main one
MT5_QUIET_ROLL_DAYS = 30      # without activity figures: the nearest month with more than 30 days left
MT5_MIN_ACTIVE_VOLUME = 50.0  # a session volume below this (just after the open) says nothing yet
MT5_MONTHS = "FGHJKMNQUVXZ"


def _epoch(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp() if value else None
    except ValueError:
        return None


def _mt5_price(value: Any) -> float | None:
    """The bridge sends MT5's missing price (0.0) as null and a real negative price (energy futures can
    trade below zero) as it is: only zero is “none” here, unlike the other sources' sentinels."""
    price = _number(value)
    return round(price, 6) if price else None


@dataclass
class Mt5ForeignSource:
    """MT5 只读行情桥（``services/mt5-bridge``，4090 上挂所有者已登录的 AMP 模拟终端，经反向隧道到生产机）。

    每个有 ``mt5`` 根代码的品种取主力月份合约，整行（价、买卖、高低开、成交量、持仓量、结算价）都来自它：
    候选是根代码 + 月份字母 + 两位年份、未到期且离到期超过 ``MT5_EXPIRY_GUARD_DAYS`` 天的最近
    ``MT5_CANDIDATES`` 个月份；按持仓量、其次当日成交量（不足 ``MT5_MIN_ACTIVE_VOLUME`` 视为还没成交）选最大者，
    并记住它；周末与开盘前这些都是 0 时沿用本进程上次选出的月份，没有就取离到期超过
    ``MT5_QUIET_ROLL_DAYS`` 天的最近月份（再没有取最远的候选）。昨结算用 MT5 的结算价
    （``session_price_settlement``），时间是 MT5 逐笔时间（交易服务器时钟≈UTC）换成北京时间。

    合约表半小时取一次，报价每次一个批量请求；桥不可用、超时（4 秒）或未配置都抛异常，交给统一入口换下一个源。
    令牌只放请求头，不进日志与异常文本。"""

    name: str = "mt5"
    base_url: str | None = None
    token: str | None = None
    get_json: Callable[[str], Any] | None = None  # tests: path → decoded JSON
    clock: Callable[[], float] = time.time
    _contracts: dict[str, list[tuple[str, float | None]]] = field(default_factory=dict, init=False, repr=False)
    _contracts_at: float | None = field(default=None, init=False, repr=False)
    _contracts_retry_at: float = field(default=0.0, init=False, repr=False)
    _main: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    # Each listed month's (tick size, multiplier): the 外盘本站模拟 trade catalog's contract facts (二期).
    _specs: dict[str, tuple[float | None, float | None]] = field(default_factory=dict, init=False, repr=False)
    # Each month's hourly bars by start (ns) and when the whole history was last read: the daily chart's source.
    _hourly: dict[str, tuple[float, dict[int, dict[str, Any]]]] = field(default_factory=dict, init=False, repr=False)
    _hourly_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _get(self, path: str) -> Any:
        if self.get_json is not None:
            return self.get_json(path)
        url = (self.base_url or os.getenv(MT5_URL_ENV) or "").strip().rstrip("/")
        token = self.token if self.token is not None else secret_value(MT5_TOKEN_ENV)
        if not url or not token:
            raise RuntimeError("mt5_bridge_not_configured")
        try:
            with httpx.Client(timeout=MT5_TIMEOUT_SECONDS) as client:
                response = client.get(url + path, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as error:
            raise ConnectionError(f"mt5_bridge_unreachable:{type(error).__name__}") from None
        if response.status_code != 200:
            raise RuntimeError(f"mt5_bridge_http_{response.status_code}")
        return response.json()

    def _contract_table(self) -> dict[str, list[tuple[str, float | None]]]:
        """Every catalog root's listed months with their expiry (epoch seconds), read every half hour. A list
        without any catalog month (an empty 200 while the terminal reloads) never replaces a good table or
        counts as read: it is asked again after ``MT5_CONTRACTS_RETRY_SECONDS``, and with no good table yet
        the source fails (the next source serves) instead of quietly carrying nothing for half an hour (#932)."""
        now = self.clock()
        stale = self._contracts_at is None or now - self._contracts_at >= MT5_CONTRACTS_TTL_SECONDS
        if stale and now >= self._contracts_retry_at:
            roots = {item.mt5 for item in FOREIGN_PRODUCTS if item.mt5}
            pattern = re.compile(rf"^({'|'.join(re.escape(root) for root in sorted(roots))})[{MT5_MONTHS}]\d{{2}}$")
            table: dict[str, list[tuple[str, float | None]]] = {root: [] for root in roots}
            specs: dict[str, tuple[float | None, float | None]] = {}
            for item in self._get("/v1/symbols?path=Exchange-Futures").get("symbols") or []:
                match = pattern.match(str(item.get("symbol") or ""))
                if match:
                    table[match.group(1)].append((item["symbol"], _epoch(item.get("expirationTime"))))
                    specs[item["symbol"]] = (_number(item.get("tickSize")), _number(item.get("multiplier")))
            if any(table.values()):
                self._contracts, self._contracts_at, self._specs = table, now, specs
            else:
                self._contracts_retry_at = now + MT5_CONTRACTS_RETRY_SECONDS
                log.info("mt5_contracts_empty kept=%s", bool(self._contracts))
        if not any(self._contracts.values()):
            raise RuntimeError("mt5_contracts_empty")
        return self._contracts

    def _pick(self, root: str, months: list[tuple[str, float | None]], quotes: dict[str, dict[str, Any]],
              now: float) -> str:
        def activity(symbol: str) -> tuple[float, float]:
            quote = quotes.get(symbol) or {}
            volume = _number(quote.get("sessionVolume")) or 0.0
            return (_number(quote.get("openInterest")) or 0.0, volume if volume >= MT5_MIN_ACTIVE_VOLUME else 0.0)

        names = [symbol for symbol, _ in months]
        leader = max(names, key=activity)
        if any(activity(leader)):
            self._main[root] = leader
            return leader
        if self._main.get(root) in names:
            return self._main[root]
        quiet = [symbol for symbol, expiry in months if expiry is None or expiry - now > MT5_QUIET_ROLL_DAYS * 86400]
        return quiet[0] if quiet else names[-1]

    def quotes(self, products: list[ForeignProduct]) -> dict[str, dict[str, Any]]:
        by_root = {item.mt5: item for item in products if item.mt5}
        if not by_root:
            return {}
        now = self.clock()
        table = self._contract_table()
        candidates: dict[str, list[tuple[str, float | None]]] = {}
        for root in by_root:
            live = sorted((month for month in table.get(root, [])
                           if month[1] is None or month[1] - now > MT5_EXPIRY_GUARD_DAYS * 86400),
                          key=lambda month: month[1] if month[1] is not None else math.inf)
            if live:
                candidates[root] = live[:MT5_CANDIDATES]
        symbols = [symbol for months in candidates.values() for symbol, _ in months]
        if not symbols:
            return {}
        # 行情桥一次最多报 MT5_QUOTE_BATCH 个合约（外盘品种多了以后候选月份超过它）：分批读，合在一起挑主力。
        quotes: dict[str, dict[str, Any]] = {}
        for start in range(0, len(symbols), MT5_QUOTE_BATCH):
            answer = self._get("/v1/quotes?symbols=" + ",".join(symbols[start:start + MT5_QUOTE_BATCH]))
            quotes.update({str(item.get("symbol")): item for item in answer.get("quotes") or []
                           if isinstance(item, dict) and not item.get("error")})
        rows: dict[str, dict[str, Any]] = {}
        for root, months in candidates.items():
            contract = self._pick(root, months, quotes, now)
            row = _quote_row(by_root[root], contract, quotes.get(contract))
            if row is None:
                continue  # no price on the main month: this product goes to the next source, never another month
            rows[by_root[root].key] = row
        return rows

    def month_chart(self, product: ForeignProduct, month: str, duration: int) -> dict[str, Any] | None:
        """One month contract's own quote row and K-lines (外盘三期：策略实时判定读它下单的那个月份，与撮合同一份
        价格)；该月份没有报价或不是 MT5 周期时 None。MT5 failures raise."""
        if duration not in CHART_TIMEFRAMES:
            return None
        answer = self._get("/v1/quotes?symbols=" + month)
        quote = next((item for item in answer.get("quotes") or []
                      if isinstance(item, dict) and str(item.get("symbol")) == month and not item.get("error")), None)
        row = _quote_row(product, month, quote)
        if row is None:
            return None
        bars = self.bars(month, duration, CHART_BARS.get(duration, CHART_DEFAULT_BARS))
        return {"row": row, "bars": bars} if bars and not _syncing(bars, row, duration) else None


    def chart(self, product: ForeignProduct, duration: int) -> dict[str, Any] | None:
        """The main-month contract's quote row and K-lines for one chart period (``CHART_TIMEFRAMES``): the same
        contract the 外盘 row shows, every bar from it. None when the product has no MT5 root, the period is not
        an MT5 one, or the main month has no price (the chart then stays on 快期主连)."""
        spec = CHART_TIMEFRAMES.get(duration)
        if spec is None or not product.mt5:
            return None
        row = self.quotes([product]).get(product.key)
        if row is None:
            return None
        limit = CHART_BARS.get(duration, CHART_DEFAULT_BARS)
        bars = self.bars(str(row["contract"]), duration, limit)
        return {"row": row, "bars": bars} if bars and not _syncing(bars, row, duration) else None

    def bars(self, contract: str, duration: int, limit: int) -> list[dict[str, Any]]:
        """One MT5 month contract's last ``limit`` K-lines of a chart period (``CHART_TIMEFRAMES``; 3 分 from 1 分,
        30 分 from 15 分), each with its CME ``tradingDay`` (美东 18:00 换日 — the strategy rules split days by it).
        The chart and the 外盘三期 backtest read the same bars. MT5 failures raise."""
        frame, factor = CHART_TIMEFRAMES[duration]
        if duration == DAILY:
            daily = aggregate_bars(self._hourly_bars(contract), lambda bar: _bar_cme_day(int(bar["datetime"])))
            return [{**bar, "datetime": _cst_midnight(day), "tradingDay": day}
                    for bar in daily[-limit:] for day in (_bar_cme_day(int(bar["datetime"])),)]
        bars = self._read_bars(contract, frame, min(MT5_MAX_BARS, limit * factor))
        if factor > 1:  # buckets on the period's own boundaries
            bars = aggregate_bars(bars, lambda bar: int(bar["datetime"]) // (duration * NS))
        days: list[dict[str, Any]] = [{**bar, "tradingDay": cme_trading_day(int(bar["datetime"] or 0))} for bar in bars]
        return days[-limit:]

    def _read_bars(self, contract: str, frame: str, count: int) -> list[dict[str, Any]]:
        answer = self._get(f"/v1/bars?symbol={contract}&timeframe={frame}&count={count}")
        bars = []
        for item in answer.get("bars") or []:
            prices = [_mt5_price(item.get(name)) for name in ("open", "high", "low", "close")]
            stamp = _number(item.get("time"))
            if stamp is None or any(price is None for price in prices):
                continue
            volume = _number(item.get("volume")) or _number(item.get("tickVolume")) or 0.0
            bars.append({"datetime": int(stamp) * NS, "open": prices[0], "high": prices[1], "low": prices[2],
                         "close": prices[3], "volume": volume, "openInterest": 0.0})
        return bars

    def _hourly_bars(self, contract: str) -> list[dict[str, Any]]:
        """A month's hourly bars, oldest first: the whole history (up to the bridge's cap) every
        ``MT5_HOURLY_REFRESH_SECONDS``, else the held one with its last ``MT5_HOURLY_TAIL`` hours read again (a tail
        that does not reach the held bars reads the whole history instead)."""
        now = self.clock()
        with self._hourly_lock:
            held = self._hourly.get(contract)
        if held is not None and now - held[0] < MT5_HOURLY_REFRESH_SECONDS:
            tail = self._read_bars(contract, "H1", MT5_HOURLY_TAIL)
            if tail and held[1] and tail[0]["datetime"] <= max(held[1]):
                table = {**held[1], **{bar["datetime"]: bar for bar in tail}}
                with self._hourly_lock:
                    self._hourly[contract] = (held[0], table)
                return [table[key] for key in sorted(table)]
        table = {bar["datetime"]: bar for bar in self._read_bars(contract, "H1", MT5_MAX_BARS)}
        with self._hourly_lock:
            self._hourly[contract] = (now, table)
        return [table[key] for key in sorted(table)]


def _quote_row(product: ForeignProduct, contract: str, quote: dict[str, Any] | None) -> dict[str, Any] | None:
    """An MT5 quote of one month contract as an 外盘 row (``contract`` names the month); None without a price."""
    last = _mt5_price(quote.get("last")) if quote else None
    if quote is None or last is None:
        return None
    stamp = _number(quote.get("timeMsc"))
    volume, held = _number(quote.get("sessionVolume")), _number(quote.get("openInterest"))
    return {**_row(
        product, "mt5", last=last, pre_settlement=_mt5_price(quote.get("settlement")),
        open_=_mt5_price(quote.get("sessionOpen")), high=_mt5_price(quote.get("sessionHigh")),
        low=_mt5_price(quote.get("sessionLow")), bid=_mt5_price(quote.get("bid")),
        ask=_mt5_price(quote.get("ask")), volume=volume or None, open_interest=held or None,
        quote_time=datetime.fromtimestamp(stamp / 1000, UTC).astimezone(CST).isoformat(timespec="seconds")
        if stamp else None), "contract": contract}


def _cst_midnight(day: str) -> int:
    """A daily bar's stamp: 00:00 Beijing of its trading day, as TqSdk stamps one (``daily_bar_day``)."""
    return int(datetime.fromisoformat(day).replace(tzinfo=CST).timestamp()) * NS


def _syncing(bars: list[dict[str, Any]], row: dict[str, Any], duration: int) -> bool:
    """Whether a month's MT5 bars are still catching up (外盘尾项 2026-10-21): the bridge answers from the
    terminal's local history and MT5 only then syncs it with the server, so the first read of a month nobody read
    for a while can end hours back while its quote is current. Its last bar starting more than one period plus
    ``CHART_SESSION_QUOTE_SECONDS`` before the quote (a daily chart: on a CME trading day before that of the quote
    less that margin) is such a read — the chart stays on 快期 that round and the next one reads MT5 again."""
    quote = _epoch(row.get("quoteTime"))
    if quote is None:
        return False
    if duration == DAILY:
        behind = _bar_cme_day(int(quote - CHART_SESSION_QUOTE_SECONDS) * NS) > str(bars[-1].get("tradingDay"))
    else:
        behind = quote - int(bars[-1]["datetime"]) / NS > duration + CHART_SESSION_QUOTE_SECONDS
    if behind:
        log.info("mt5_bars_syncing contract=%s duration=%s", row.get("contract"), duration)
    return behind


def _quote_age(row: dict[str, Any], now: datetime) -> float | None:
    quote_time = row.get("quoteTime")
    try:
        stamp = datetime.fromisoformat(str(quote_time)) if quote_time else None
    except ValueError:
        return None
    if stamp is None:
        return None
    return max(0.0, (now - (stamp if stamp.tzinfo else stamp.replace(tzinfo=CST))).total_seconds())


def _mt5_quote_fields(row: dict[str, Any], age: float | None) -> dict[str, Any]:
    """An MT5 month row as the chart quote's price and freshness fields (the rest of the quote is the chart's)."""
    last, change = row["last"], row.get("changePercent")
    return {
        "price": last, "lastPrice": last, "preSettlement": row.get("preSettlement"),
        "changePercent": change if change is not None else 0.0,
        "trend": "up" if (change or 0) > 0 else "down" if (change or 0) < 0 else "flat",
        "open": row.get("open"), "highest": row.get("high"), "lowest": row.get("low"),
        "bidPrice1": row.get("bidPrice1"), "askPrice1": row.get("askPrice1"), "volume": row.get("volume"),
        "openInterest": row.get("openInterest"), "quoteDatetime": row.get("quoteTime"),
        "ageSeconds": round(age or 0.0, 3), "dataStatus": "normal", "source": f"{CHART_SOURCE_PREFIX} · {row['contract']}",
    }


def cme_chart_session(now: datetime, quote_age: float | None, hours: str = "globex") -> dict[str, Any]:
    """外盘（CME）图表快照的交易时段与状态（外盘盘点 D 2026-10-21，客户端只读它）：``tradingSchedule`` 是这个品种的
    CME 时段（``trading_calendar.cme_schedule``，``hours`` 是 ``ForeignProduct.hours``，带 ``dayRule: "cme"`` 与
    ``cmeHours``），``marketSessionStatus`` 按它判定（盘中休息、每日停盘、周末、``CME_CLOSED_DAYS`` 全天休市，见
    ``trading_calendar.cme_session``）；时段内 MT5 月份报价已超过 ``CHART_SESSION_QUOTE_SECONDS`` 也算休市（CME
    临时休市、提前收盘这些没写进日历的）。休市时带 ``sessionClosedFor``（intraday / break / weekend / holiday）与
    ``sessionReopensAt``。"""
    session = cme_session(int(now.timestamp() * NS), hours)
    status = session["status"]
    if status == "trading" and quote_age is not None and quote_age > CHART_SESSION_QUOTE_SECONDS:
        status = "closed"
    fields: dict[str, Any] = {"marketSessionStatus": status, "tradingSchedule": cme_schedule(now, hours)}
    if session["status"] == "closed":
        fields.update(sessionClosedFor=session["closedFor"], sessionReopensAt=session["reopensAt"])
    return fields


@lru_cache(maxsize=16384)
def _bar_cme_day(stamp_ns: int) -> str:
    return cme_trading_day(stamp_ns)


def cme_chart(workbench: dict[str, Any], row: dict[str, Any] | None, now: datetime, *,
              symbol: str | None = None, duration: int | None = None) -> dict[str, Any]:
    """一张可下单外盘主连图表（``KQD.m@CME.NQ``）的快照按 CME 口径（外盘盘点 D 2026-10-21）：共享行情的生产者对
    每个周期、每条读取（工作台、推送、行情快照）都经这里。分钟到小时线整张来自 MT5 主力月份（``chart_packet``）；
    周线（MT5 不通时还有日线）的 K 线仍是快期外盘主连，但报价与新鲜度换成 ``row``——同一主力月份的 MT5 实时报价（外盘报价行，
    与外盘撮合、策略日线同一份价格；快期外盘主连约 10 分钟才更新一次）。``row`` 为 None（MT5 不通）时报价照旧是
    快期主连的，新鲜度如实按它算。时段与状态见 ``cme_chart_session``（按 ``symbol`` 这个品种的时段）。MT5 不通、
    分钟到小时线退回快期外盘主连时，K 线自带的是国内换日规则的 ``tradingDay``：这里改成 CME 交易日（美东 18:00
    换日，与 MT5 K 线同一规则，F2 2026-10-21）；快期的日线、周线 K 线按交易日一根，照旧（国内日历，见
    ``CHART_TIMEFRAMES``）。"""
    quote = dict(workbench.get("quote") or {})
    meta: dict[str, Any] = {}
    mt5_drawn = str(workbench.get("source") or "").startswith(CHART_SOURCE_PREFIX)
    if mt5_drawn:
        age = _quote_age({"quoteTime": quote.get("quoteDatetime")}, now)  # its own MT5 quote, aged to now
    elif row is not None:
        age = _quote_age(row, now)
        quote.update(_mt5_quote_fields(row, age))
        meta = {"ageSeconds": quote["ageSeconds"], "dataStatus": "normal", "isCached": False}
    else:
        age = None
    session = cme_chart_session(now, age, cme_hours_of(str(symbol or quote.get("symbol") or "")))
    schedule = session["tradingSchedule"]
    quote.update(session, tradingSchedule={"day": [[start[:5], end[:5]] for start, end in schedule["day"]],
                                           "night": [], **cme_rule(schedule)})  # HH:MM as every chart quote
    if "sessionClosedFor" not in session:
        quote.pop("sessionClosedFor", None)
        quote.pop("sessionReopensAt", None)
    period = duration if duration is not None else workbench.get("durationSeconds")
    if not mt5_drawn and isinstance(period, int | float) and 0 < period < 86400 and workbench.get("bars"):
        meta["bars"] = [{**bar, "tradingDay": _bar_cme_day(int(bar["datetime"]))} if bar.get("datetime") else bar
                        for bar in workbench["bars"]]
    return {**workbench, **meta, "marketSessionStatus": session["marketSessionStatus"], "quote": quote}


def chart_packet(symbol: str, duration: int, row: dict[str, Any], bars: list[dict[str, Any]],
                 item: dict[str, Any], now: datetime) -> dict[str, Any]:
    """An MT5 chart read in the shape the shared market's sidecar answers a workbench read with (quote + bars),
    so the producer, the chart and the snapshot need nothing of their own. ``source`` names the contract
    (“MT5 · GCEZ26”); the contract facts (tick, multiplier) are the 快期 catalog's for the 主连 symbol, the hours
    and status CME's for this product (``cme_chart_session``)."""
    last, quote_time = row["last"], row.get("quoteTime")
    age = _quote_age(row, now)
    session = cme_chart_session(now, age, cme_hours_of(symbol))
    status = session["marketSessionStatus"]
    source = f"{CHART_SOURCE_PREFIX} · {row['contract']}"
    quote = {
        "symbol": symbol, "name": item.get("instrumentName") or row["name"], **_mt5_quote_fields(row, age),
        "priceTick": item.get("priceTick"), "volumeMultiple": item.get("volumeMultiple"),
        "capturedAt": now.isoformat(), **session,
    }
    digest = hashlib.sha1(json.dumps([last, quote_time, len(bars), bars[-1:]], sort_keys=True, default=str).encode())
    return {"schemaVersion": 1, "capturedAt": now.isoformat(), "dataStatus": "normal", "isCached": False,
            "source": source, "liveTrading": "disabled", "ageSeconds": quote["ageSeconds"], "durationSeconds": duration,
            "marketSessionStatus": status, "revision": "mt5:" + digest.hexdigest()[:16], "quote": quote, "bars": bars}


def configured_sources(value: str | None = None) -> tuple[str, ...]:
    raw = os.getenv(ENV_SOURCES, "") if value is None else value
    names = tuple(dict.fromkeys(name.strip().lower() for name in raw.split(",") if name.strip()))
    return tuple(name for name in names if name in SOURCE_LABELS) or DEFAULT_SOURCES


@dataclass
class ForeignMarketService:
    kept: LastGood
    sources: list[ForeignSource]
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    _collect: Collector = field(init=False, repr=False)
    # One atomic sample from the normal source read, never the fallback/last-good board.
    mt5_health: tuple[datetime, bool] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._collect = Collector(self.kept, lane="foreign", workers=1, error_prefix="foreign_source_error")

    def bind(self, loop: asyncio.AbstractEventLoop, quotes_reader: Callable[[list[str]], Coroutine[Any, Any, dict[str, Any]]]) -> None:
        """Give the 快期 source the running loop and the shared market's quote reader (lifespan, once)."""
        for source in self.sources:
            if isinstance(source, KqdForeignSource):
                source.loop, source.quotes_reader = loop, quotes_reader

    def close(self) -> None:
        self._collect.close()

    def _fetch(self) -> dict[str, Any]:
        products = list(FOREIGN_PRODUCTS)
        found: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        for source in self.sources:
            wanted = [item for item in products if item.key not in found]
            if not wanted:
                break
            observes_mt5 = source.name == "mt5" and any(item.mt5 for item in wanted)
            try:
                rows = source.quotes(wanted)
            except Exception as error:  # noqa: BLE001 — one source failing leaves the next one
                log.info("foreign_source_failed source=%s error=%s", source.name, type(error).__name__)
                failed.append(source.name)
                if observes_mt5:
                    self.mt5_health = (self.now(), False)
                continue
            if observes_mt5:
                self.mt5_health = (self.now(), bool(rows))
            for item in wanted:
                if item.key in rows:
                    found[item.key] = rows[item.key]
        if not found:
            return {"status": "unavailable", "error": f"foreign_sources_failed:{','.join(failed)}" if failed else UNAVAILABLE}
        # A product a source failed on, or simply left out (an HTTP 200 without its row, a zero price), keeps its
        # last good row, marked stale: the answer is partial (kept, read again after the short retry), never a
        # fresh "ok" missing products.
        previous = {row["key"]: row for row in (self.kept.value("board") or {}).get("rows") or []}
        carried: list[str] = []
        for item in products:
            if item.key not in found and item.key in previous:
                found[item.key] = {**previous[item.key], "stale": True}
                carried.append(item.key)
        # A product some configured source carries but no source answered and nothing was kept (a cold start):
        # missing, so the answer is partial and the product's readers say unavailable, not "no data" (#932).
        missing = [item.key for item in products if item.key not in found and self._carried_by_a_source(item)]
        payload: dict[str, Any] = {
            "status": "ok", "fetchedAt": self.now().isoformat(timespec="seconds"),
            "sources": [source.name for source in self.sources],
            "rows": [found[item.key] for item in products if item.key in found],
        }
        if failed:
            payload["failedSources"] = failed
        if failed or carried or missing:
            payload["partial"] = {"failed": failed, "carried": carried, "missing": missing}
        return payload

    def chart(self, symbol: str, duration: int, item: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """A chart read for a 快期外盘主连 symbol (``KQD.m@COMEX.GC``) of a catalog product, from MT5, when MT5 comes
        before 快期 in the source order and the period is an MT5 one; None otherwise (the shared market then reads
        快期 as before — one source for the whole chart). Sync: call from a worker thread. MT5 failures raise."""
        product = next((p for p in FOREIGN_PRODUCTS if p.kqd and p.kqd.upper() == symbol.upper()), None)
        names = [source.name for source in self.sources]
        mt5 = next((source for source in self.sources if isinstance(source, Mt5ForeignSource)), None)
        if product is None or mt5 is None or duration not in CHART_TIMEFRAMES:
            return None
        if "kqd" in names and names.index("kqd") < names.index("mt5"):
            return None
        got = mt5.chart(product, duration)
        return None if got is None else chart_packet(symbol, duration, got["row"], got["bars"], item or {}, self.now())

    def chart_quote(self, symbol: str) -> dict[str, Any] | None:
        """外盘主连图表（``KQD.m@CME.NQ``）快期画的周线（MT5 不通时的日线）的报价：该品种外盘报价行此刻的 MT5 主力月份报价（与分钟图、外盘
        撮合同一个月份，经 ``snapshot`` 几秒内共用一次读取）；MT5 不是这一行的源、行是沿用的旧值或没有这一行时 None。
        Sync: worker thread."""
        product = next((p for p in FOREIGN_PRODUCTS if p.kqd and p.kqd.upper() == str(symbol).upper()), None)
        if product is None or not any(isinstance(source, Mt5ForeignSource) for source in self.sources):
            return None
        row = next((row for row in self.snapshot().get("rows") or [] if row.get("key") == product.key), None)
        return row if row is not None and row.get("source") == "mt5" and not row.get("stale") and row.get("contract") else None

    def history(self, contract_symbol: str, duration: int, length: int) -> dict[str, Any] | None:
        """外盘三期（方案 A）：一个外盘月份合约（``CME.GCEZ26``）分钟到小时周期的历史，形状同行情服务的 history
        读取（bars + 合约的最小变动价位、乘数、交易时段），回测与策略读同一份 MT5 数据（日线由小时线按 CME 交易日合成）。周线与非 MT5 周期为
        None（调用方照旧读快期主连）。Sync: worker thread. MT5 failures raise."""
        mt5 = next((source for source in self.sources if isinstance(source, Mt5ForeignSource)), None)
        product = foreign_product_of(contract_symbol)
        if mt5 is None or product is None or duration not in CHART_TIMEFRAMES:
            return None
        month = contract_symbol.partition(".")[2].upper()
        mt5._contract_table()
        tick, multiplier = mt5._specs.get(month, (None, None))
        bars = mt5.bars(month, duration, length)
        return {"symbol": contract_symbol, "durationSeconds": duration, "bars": bars, "priceTick": tick,
                "volumeMultiple": multiplier, "tradingSchedule": cme_schedule(self.now(), product.hours),
                "dataStatus": "normal",
                "source": f"{CHART_SOURCE_PREFIX} · {month}"}

    def workbench(self, contract_symbol: str, duration: int) -> dict[str, Any] | None:
        """外盘三期（方案 A）：策略实时判定读的行情（报价 + K 线，形状同行情服务的 workbench），取自这个月份合约
        自己的 MT5 数据，与回测、撮合同一份价格。周线与非 MT5 周期为 None（调用方照旧读快期主连）。
        Sync: worker thread. MT5 failures raise."""
        mt5 = next((source for source in self.sources if isinstance(source, Mt5ForeignSource)), None)
        product = foreign_product_of(contract_symbol)
        if mt5 is None or product is None or duration not in CHART_TIMEFRAMES:
            return None
        month = contract_symbol.partition(".")[2].upper()
        got = mt5.month_chart(product, month, duration)
        if got is None:
            return None
        mt5._contract_table()
        tick, multiplier = mt5._specs.get(month, (None, None))
        item = {"instrumentName": foreign_trade_name(contract_symbol), "priceTick": tick, "volumeMultiple": multiplier}
        return chart_packet(contract_symbol, duration, got["row"], got["bars"], item, self.now())

    def trade_catalog(self) -> dict[str, Any]:
        """外盘本站模拟（二期）的可下单合约目录：带 MT5 根代码的品种、未到期的全部月份合约，代码是外盘撮合
        引擎的写法（``CME.GCEZ26``），``isMain`` 是外盘报价行此刻用的主力月份（同一个选法）。MT5 不可用时
        目录为空、``catalogStatus`` unavailable——外盘撮合也暂停，不拿别的源顶替。Sync: worker thread."""
        mt5 = next((source for source in self.sources if isinstance(source, Mt5ForeignSource)), None)
        if mt5 is None:
            return {"items": [], "catalogStatus": "unavailable", "dataStatus": "unavailable",
                    "dataErrorCode": "mt5_source_not_configured"}
        try:
            table = mt5._contract_table()
        except Exception as error:  # noqa: BLE001 — the bridge down is an empty catalog, said so
            log.info("foreign_trade_catalog_unavailable error=%s", type(error).__name__)
            return {"items": [], "catalogStatus": "unavailable", "dataStatus": "unavailable",
                    "dataErrorCode": "mt5_contracts_unavailable"}
        mains = {row["key"]: row.get("contract") for row in self.snapshot().get("rows") or []
                 if row.get("source") == "mt5" and not row.get("stale")}
        now = mt5.clock()
        items: list[dict[str, Any]] = []
        for product in FOREIGN_PRODUCTS:
            if not foreign_tradable(product) or product.mt5 is None:
                continue
            months = sorted(table.get(product.mt5, []), key=lambda month: month[1] if month[1] is not None else math.inf)
            for name, expiry in months:
                if expiry is not None and expiry <= now:
                    continue
                year, month = 2000 + int(name[-2:]), MT5_MONTHS.index(name[-3]) + 1
                tick, multiplier = mt5._specs.get(name, (None, None))
                if expiry is None or not tick or not multiplier:
                    continue  # 外盘引擎不收资料不全的月份（paper_foreign.contract_record），目录也不列
                items.append({
                    "symbol": foreign_trade_symbol(name), "exchangeId": FOREIGN_TRADE_EXCHANGE, "instrumentId": name,
                    "instrumentName": foreign_trade_name(foreign_trade_symbol(name)), "productId": product.mt5,
                    "productName": product.name, "foreignKey": product.key, "currency": product.currency,
                    "unit": product.unit, "priceTick": tick, "volumeMultiple": multiplier,
                    "deliveryYear": year, "deliveryMonth": month,
                    "expireRestDays": int((expiry - now) // 86400) if expiry is not None else None,
                    # 最后交易日（美东日期）：美元子账户外盘仓的到期提醒按它算（外盘代码统一，2026-10-21）。
                    "expireDate": datetime.fromtimestamp(expiry, UTC).astimezone(NEW_YORK).date().isoformat(),
                    "isMain": mains.get(product.key) == name, "hasNight": True, "chartSymbol": product.kqd,
                })
        return {"items": items, "catalogStatus": "normal", "dataStatus": "normal", "source": "mt5"}

    def month_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """外盘月份合约（``CME.GCEZ26``）各自的 MT5 报价行（到价提醒盯价用，与撮合同一份价格）：代码（大写）→ 外盘行
        （last、quoteTime、contract…）；没有报价的月份不在结果里。MT5 未配置或不通时抛异常。Sync: worker thread."""
        mt5 = next((source for source in self.sources if isinstance(source, Mt5ForeignSource)), None)
        if mt5 is None:
            raise RuntimeError("mt5_source_not_configured")
        wanted = {str(symbol).upper().partition(".")[2]: (str(symbol).upper(), product) for symbol in symbols
                  if (product := foreign_product_of(str(symbol))) is not None and str(symbol).upper().startswith("CME.")}
        months = sorted(wanted)
        rows: dict[str, dict[str, Any]] = {}
        for start in range(0, len(months), MT5_QUOTE_BATCH):
            answer = mt5._get("/v1/quotes?symbols=" + ",".join(months[start:start + MT5_QUOTE_BATCH]))
            for item in answer.get("quotes") or []:
                month = str(item.get("symbol") or "") if isinstance(item, dict) and not item.get("error") else ""
                if month in wanted and (row := _quote_row(wanted[month][1], month, item)) is not None:
                    rows[wanted[month][0]] = row
        return rows

    def _carried_by_a_source(self, product: ForeignProduct) -> bool:
        return any(getattr(product, source.name, None) for source in self.sources)

    def snapshot(self) -> dict[str, Any]:
        """Every foreign product's row (sync; call from a worker thread)."""
        payload = self._collect.read("board", self._fetch, ttl=QUOTE_TTL_SECONDS, retry=RETRY_SECONDS,
                                     wait=WAIT_SECONDS, unavailable=UNAVAILABLE)
        rows = payload.get("rows") or []
        return {**payload, "rows": rows, "sourceLabels": SOURCE_LABELS, "note": NOTE}

    async def board(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.snapshot)

    def for_product(self, product: str | None) -> dict[str, Any]:
        """The research / AI view: every row, or a domestic product id's related ones (none for most products).
        ``product`` is the lower-case product id (research_live.product_key). The status is the product's own
        (#932): related products none of which has a row → unavailable; some without a row → ``missing`` lists
        them; a product with no related foreign market is a plain empty answer."""
        payload = self.snapshot()
        if not product:
            if payload.get("status") not in {"ok", "stale"}:
                # Nothing at all (a cold start with every source down or quoting nothing): every product a
                # configured source carries is missing, named for the all-markets reader too (#932).
                return {**payload, "missing": [item.key for item in FOREIGN_PRODUCTS if self._carried_by_a_source(item)]}
            return payload
        related = foreign_for_domestic(product)
        wanted = {item.key for item in related}
        rows = [row for row in payload["rows"] if row["key"] in wanted]
        present = {row["key"] for row in rows}
        # Only what a configured source can carry is expected (as in _fetch): with ``AI_TRADER_FOREIGN_SOURCES``
        # leaving a provider out, its products are not “missing”, they are simply not offered.
        expected = [item.key for item in related if self._carried_by_a_source(item)]
        missing = [key for key in expected if key not in present]
        payload = {**payload, "rows": rows}
        if expected and not rows:
            return {**payload, "status": "unavailable", "missing": missing,
                    "error": payload.get("error") or "foreign_product_unavailable"}
        if missing:
            payload["missing"] = missing
        return payload


def build_sources(names: tuple[str, ...] | None = None) -> list[ForeignSource]:
    made: dict[str, Callable[[], ForeignSource]] = {"sina": SinaForeignSource, "mt5": Mt5ForeignSource,
                                                    "kqd": KqdForeignSource}
    return [made[name]() for name in (names or configured_sources()) if name in made]

"""Futures research data that the TqKq free feed does not carry, fetched from
AKShare's free scrapers (exchange sites, Sina, Eastmoney): member position
ranks, warehouse receipts, spot basis, inventory, term structure and the
fee / margin reference table.

Every dataset fails on its own. Sources are scrapers with uneven reliability,
so a failure keeps the last good value (marked stale) and is never allowed to
touch the market or execution paths: fetches run in a small thread pool with
a per-dataset timeout, the akshare import is deferred, and a missing package
only turns every dataset into `research_dependency_missing`.
"""
from __future__ import annotations

import functools
import importlib
import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

import httpx

from .collection import Collector, Request
from .host_throttle import EASTMONEY
from .instruments import ForeignProduct, foreign_product_of
from .last_good import TABLE as LAST_GOOD_TABLE
from .last_good import Kept, LastGood

log = logging.getLogger("uvicorn.error.research")

CHINA = timezone(timedelta(hours=8))
# The Eastmoney data center host: AKShare's futures_inventory_em reads it on the Eastmoney lane.
EASTMONEY_DATACENTER_HOST = "datacenter-web.eastmoney.com"
DATASETS = ("position_rank", "warehouse_receipt", "spot_basis", "inventory", "term_structure", "fees_margin")
# Extracted per contract (one month's member rank, one month's fees); the rest are per product.
CONTRACT_DATASETS = frozenset({"position_rank", "fees_margin"})
# 九期网's fee table (futures_comm_info) answers in 7–25 s; the other sources in under 2 s.
FETCH_TIMEOUT_SECONDS = 30.0
INTRADAY_TTL_SECONDS = 60 * 60
# A failed or stale entry is retried sooner than a good one.
FAILURE_TTL_SECONDS = 10 * 60
# A source table that covers every month (the rank, the fee table) serves all months asked within this.
SOURCE_TABLE_TTL_SECONDS = 10 * 60
# After the day session's settlement data is published, one fetch per trading day is enough.
SETTLED_HOUR = 17
RECENT_POINTS = 30


# ---------------------------------------------------------------- symbols

def split_symbol(symbol: str) -> tuple[str, str, str]:
    """`SHFE.rb2610` → (exchange `SHFE`, product `rb`, contract `rb2610`)."""
    exchange, _, contract = symbol.partition(".")
    if not exchange or not contract:
        raise ValueError("research_symbol_invalid")
    product = contract.rstrip("0123456789")
    if not product:
        raise ValueError("research_symbol_invalid")
    return exchange.upper(), product, contract


FOREIGN_DATA_NOTICE = "外盘品种没有国内交易所的持仓排名、仓单、基差、库存这些研究数据；外盘看调研里的行情与对应国内品种、快讯和资讯。"
# 外盘品种的研究（2026-10-21 所有者：打开迷你纳指，调研、资讯都不可用）：研究台、资讯、研究数据与研讨都经
# research_target 认外盘代码（快期外盘主连 KQD.m@CME.NQ 或美元子账户月份 CME.ENQZ26），交易所写 FOREIGN、
# 品种写外盘品种键（CME.NQ）；国内代码照旧 split_symbol。
FOREIGN_RESEARCH = "FOREIGN"


def research_target(symbol: str) -> tuple[str, str, ForeignProduct | None]:
    """The product a research request is about: (exchange, product, the foreign product or None).
    `KQD.m@CME.NQ` / `CME.ENQZ26` → ("FOREIGN", "CME.NQ", 迷你纳指100); `SHFE.rb2610` → ("SHFE", "rb", None)."""
    item = foreign_product_of(symbol)
    if item is not None:
        return FOREIGN_RESEARCH, item.key, item
    exchange, product, _ = split_symbol(symbol)
    return exchange, product, None


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if not value or value in {"-", "—"}:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _records(frame: Any) -> list[dict[str, Any]]:
    """A DataFrame (or rows already built as dicts) as plain dicts; anything else is no data."""
    to_dict = getattr(frame, "to_dict", None)
    if to_dict is None:
        return [dict(row) for row in frame if isinstance(row, dict)] if isinstance(frame, list) else []
    try:
        rows = to_dict(orient="records")
    except TypeError:
        return []
    return [dict(row) for row in rows]


def _pick_frame(table: Any, *keys: str) -> Any:
    """AKShare rank/receipt calls return {contract or product: DataFrame}; find ours case-insensitively."""
    if not isinstance(table, dict):
        return None
    wanted = [key.upper() for key in keys if key]
    for name, frame in table.items():
        if str(name).upper() in wanted:
            return frame
    return None


# ---------------------------------------------------------------- normalisers
# Each takes the raw AKShare result(s) and returns the JSON the API serves.

def normalise_position_rank(table: Any, product: str, contract: str) -> dict[str, Any]:
    """The contract's own table when the exchange ranks per contract; CZCE ranks per product, and
    `scope` says which one the figures are."""
    frame = _pick_frame(table, contract)
    scope = "contract"
    if frame is None:
        frame, scope = _pick_frame(table, product), "product"
    if frame is None:
        raise LookupError("research_product_not_in_source")
    longs: list[dict[str, Any]] = []
    shorts: list[dict[str, Any]] = []
    for row in _records(frame):
        rank = int(_number(row.get("rank")) or 0)
        if _text(row.get("long_party_name")):
            longs.append({"rank": rank, "member": _text(row.get("long_party_name")),
                          "openInterest": _number(row.get("long_open_interest")), "change": _number(row.get("long_open_interest_chg"))})
        if _text(row.get("short_party_name")):
            shorts.append({"rank": rank, "member": _text(row.get("short_party_name")),
                           "openInterest": _number(row.get("short_open_interest")), "change": _number(row.get("short_open_interest_chg"))})
    if not longs and not shorts:
        raise LookupError("research_product_not_in_source")
    def total(items: list[dict[str, Any]], key: str) -> float:
        return float(sum(item[key] or 0.0 for item in items))
    long_total, short_total = total(longs, "openInterest"), total(shorts, "openInterest")
    long_change, short_change = total(longs, "change"), total(shorts, "change")
    named = _text(next((row.get("symbol") for row in _records(frame)), contract)) or contract
    return {"contract": named, "scope": scope, "long": longs[:20], "short": shorts[:20],
            "longTotal": long_total, "shortTotal": short_total, "netLong": long_total - short_total,
            "longChange": long_change, "shortChange": short_change}


def normalise_warehouse_receipt(result: Any, product: str) -> dict[str, Any]:
    rows: list[dict[str, Any]]
    if isinstance(result, dict):
        frame = _pick_frame(result, product)
        if frame is None:
            raise LookupError("research_product_not_in_source")
        rows = _records(frame)
    else:
        rows = [row for row in _records(result)
                if _text(row.get("品种") or row.get("variety") or row.get("symbol")).upper().startswith(product.upper())]
        if not rows:
            raise LookupError("research_product_not_in_source")
    total = change = 0.0
    warehouses = []
    for row in rows:
        quantity = _number(row.get("仓单数量") or row.get("receipt") or row.get("今日仓单量") or row.get("仓单量"))
        delta = _number(row.get("当日增减") or row.get("receipt_chg") or row.get("增减"))
        name = _text(row.get("仓库简称") or row.get("仓库") or row.get("warehouse") or row.get("品牌"))
        if quantity is None:
            continue
        total += quantity
        change += delta or 0
        if name and name not in {"总计", "合计"}:
            warehouses.append({"warehouse": name, "quantity": quantity, "change": delta})
    return {"total": total, "change": change, "warehouses": warehouses[:20]}


def normalise_spot_basis(frame: Any, product: str) -> dict[str, Any]:
    row = next((row for row in _records(frame) if _text(row.get("symbol")).upper() == product.upper()), None)
    if row is None:
        raise LookupError("research_product_not_in_source")
    return {"spotPrice": _number(row.get("spot_price")),
            "dominantContract": _text(row.get("dominant_contract")), "dominantPrice": _number(row.get("dominant_contract_price")),
            "dominantBasis": _number(row.get("dom_basis")), "dominantBasisRate": _number(row.get("dom_basis_rate")),
            "nearContract": _text(row.get("near_contract")), "nearPrice": _number(row.get("near_contract_price")),
            "nearBasis": _number(row.get("near_basis")), "nearBasisRate": _number(row.get("near_basis_rate"))}


def normalise_inventory(frame: Any) -> dict[str, Any]:
    points: list[tuple[str, float, float | None]] = []
    for row in _records(frame):
        date = _text(row.get("日期") or row.get("date"))
        value = _number(row.get("库存") or row.get("value") or row.get("inventory"))
        if date and value is not None:
            points.append((date[:10], value, _number(row.get("增减") or row.get("change"))))
    if not points:
        raise LookupError("research_product_not_in_source")
    points.sort(key=lambda item: item[0])
    recent = [{"date": date, "value": value, "change": change} for date, value, change in points[-RECENT_POINTS:]]
    first, last = points[-RECENT_POINTS:][0][1], points[-1][1]
    trend = "up" if last > first * 1.02 else "down" if last < first * 0.98 else "flat"
    return {"latest": recent[-1], "trend": trend, "points": recent}


def normalise_term_structure(frame: Any, product: str) -> dict[str, Any]:
    settles: list[tuple[str, float, dict[str, Any]]] = []
    for row in _records(frame):
        name = _text(row.get("symbol"))
        settle = _number(row.get("settle")) or _number(row.get("close"))
        if name.upper().startswith(product.upper()) and settle:
            settles.append((name, settle, {"contract": name, "settle": settle, "close": _number(row.get("close")),
                                           "openInterest": _number(row.get("open_interest")), "volume": _number(row.get("volume"))}))
    if len(settles) < 2:
        raise LookupError("research_product_not_in_source")
    settles.sort(key=lambda item: item[0])
    contracts = [item[2] for item in settles]
    first, last = settles[0][1], settles[-1][1]
    shape = "contango" if last > first * 1.002 else "backwardation" if last < first * 0.998 else "flat"
    return {"shape": shape, "nearContract": contracts[0]["contract"], "farContract": contracts[-1]["contract"],
            "spread": round(last - first, 4), "contracts": contracts}


def normalise_fees_margin(frame: Any, contract: str) -> dict[str, Any]:
    row = next((row for row in _records(frame) if _text(row.get("合约代码")).upper() == contract.upper()), None)
    if row is None:
        raise LookupError("research_product_not_in_source")
    return {"contract": _text(row.get("合约代码")), "name": _text(row.get("合约名称")), "exchange": _text(row.get("交易所名称")),
            "marginRateLong": _number(row.get("保证金-买开")), "marginRateShort": _number(row.get("保证金-卖开")),
            "marginPerLot": _number(row.get("保证金-每手")),
            "openFee": _text(row.get("手续费标准-开仓-元")), "closeFee": _text(row.get("手续费标准-平昨-元")),
            "closeTodayFee": _text(row.get("手续费标准-平今-元")), "feePerLot": _number(row.get("手续费")),
            "feesUpdatedAt": _text(row.get("手续费更新时间")),
            "note": "参考值，来自第三方汇总，不改写本机手续费设置。"}


# ---------------------------------------------------------------- sources

@dataclass
class Fetched:
    data: dict[str, Any]
    source: str
    as_of: str


class AkshareSources:
    """The AKShare calls, one per dataset. Names follow the AKShare docs exactly."""

    def __init__(self) -> None:
        self._module: Any | None = None
        # SHFE's daily receipt page; tests swap in recorded HTML.
        self.http_get: Callable[[str], str] = _http_get
        # The exchange calendar (ISO trading days), wired in by the services assembly; None in a bare test.
        self.calendar: Callable[[], Iterable[str]] | None = None
        self._tables: dict[tuple[str, ...], tuple[float, Any]] = {}
        self._table_locks: dict[tuple[str, ...], threading.Lock] = {}
        self._tables_lock = threading.Lock()

    def _table(self, key: tuple[str, ...], day: str, load: Callable[[], Any]) -> Any:
        """A source answering with every month at once is read once for all the months asked within
        a few minutes: the second month waits for the first month's read instead of repeating it."""
        with self._tables_lock:
            lock = self._table_locks.setdefault(key, threading.Lock())
        with lock:
            cached = self._tables.get((*key, day))
            if cached is not None and time.time() - cached[0] < SOURCE_TABLE_TTL_SECONDS:
                return cached[1]
            value = load()
            with self._tables_lock:
                self._tables = {name: entry for name, entry in self._tables.items()
                                if time.time() - entry[0] < SOURCE_TABLE_TTL_SECONDS}
                self._tables[(*key, day)] = (time.time(), value)
            return value

    def module(self) -> Any:
        if self._module is None:
            try:
                import akshare  # deferred: heavy, optional
            except ImportError as error:
                raise RuntimeError("research_dependency_missing") from error
            self._module = akshare
        return self._module

    def trading_days(self) -> frozenset[str]:
        """The exchange calendar as YYYYMMDD strings: the Runtime's one calendar source (2026-10-18, #976 step 6) —
        the 快期 calendar the trading paths read (exchange_calendar.calendar_trading_days), wired in as
        `calendar` (ISO days). Empty without one; the weekday rule then stands in."""
        try:
            days = self.calendar() if self.calendar is not None else ()
        except Exception as error:  # noqa: BLE001 - the weekday rule stands in
            log.warning("research_calendar_failed error=%s", type(error).__name__)
            return frozenset()
        return frozenset(str(day).replace("-", "") for day in days
                         if len(str(day).replace("-", "")) == 8 and str(day).replace("-", "").isdigit())

    def _last_trading_date(self, now: datetime) -> str:
        return last_trading_date(now, self.trading_days())

    def position_rank(self, exchange: str, product: str, contract: str, now: datetime) -> Fetched:
        if exchange == "DCE":
            raise LookupError("research_source_blocked")  # see DCE_SINA_NAMES
        ak, date = self.module(), self._last_trading_date(now)
        upper = product.upper()
        if exchange == "SHFE" or exchange == "INE":
            table = self._table(("rank", exchange, upper), date, lambda: ak.get_shfe_rank_table(date=date, vars_list=[upper]))
            source = "上期所会员成交持仓排名（AKShare get_shfe_rank_table）"
        elif exchange == "CZCE":
            table = self._table(("rank", exchange), date, lambda: ak.get_rank_table_czce(date=date))
            source = "郑商所会员持仓排名（AKShare get_rank_table_czce）"
        elif exchange == "CFFEX":
            table = self._table(("rank", exchange, upper), date, lambda: ak.get_cffex_rank_table(date=date, vars_list=[upper]))
            source = "中金所会员持仓排名（AKShare get_cffex_rank_table）"
        elif exchange == "GFEX":
            table = self._table(("rank", exchange, upper), date, lambda: ak.futures_gfex_position_rank(date=date, vars_list=[upper]))
            source = "广期所会员持仓排名（AKShare futures_gfex_position_rank）"
        else:
            raise LookupError("research_exchange_unsupported")
        return Fetched(normalise_position_rank(table, product, contract), source, date)

    def warehouse_receipt(self, exchange: str, product: str, contract: str, now: datetime) -> Fetched:
        ak, date = self.module(), self._last_trading_date(now)
        if exchange == "SHFE" or exchange == "INE":
            html = self.http_get(SHFE_RECEIPT_URL.format(date=date))
            return Fetched(normalise_shfe_receipt_html(html, product), "上期所仓单日报（交易所官网）", date)
        if exchange == "DCE":
            with EASTMONEY.turn(EASTMONEY_DATACENTER_HOST):
                totals = normalise_receipt_totals(ak.futures_inventory_em(symbol=product))
            return Fetched(totals, "大商所注册仓单总量（AKShare futures_inventory_em，东方财富；无分仓库明细）", totals["date"])
        if exchange == "CZCE":
            result, source = ak.futures_warehouse_receipt_czce(date=date), "郑商所仓单日报（AKShare futures_warehouse_receipt_czce）"
        elif exchange == "GFEX":
            result, source = ak.futures_gfex_warehouse_receipt(date=date), "广期所仓单日报（AKShare futures_gfex_warehouse_receipt）"
        else:
            raise LookupError("research_exchange_unsupported")
        return Fetched(normalise_warehouse_receipt(result, product), source, date)

    def spot_basis(self, exchange: str, product: str, contract: str, now: datetime) -> Fetched:
        ak, date = self.module(), self._last_trading_date(now)
        frame = ak.futures_spot_price(date=date)
        return Fetched(normalise_spot_basis(frame, product), "现货价格与基差（AKShare futures_spot_price，生意社）", date)

    def inventory(self, exchange: str, product: str, contract: str, now: datetime) -> Fetched:
        ak = self.module()
        try:
            frame, source = ak.futures_inventory_99(symbol=product), "99 期货网库存（AKShare futures_inventory_99）"
            data = normalise_inventory(frame)
        except Exception:  # noqa: BLE001 - the fallback source is the point
            with EASTMONEY.turn(EASTMONEY_DATACENTER_HOST):
                frame, source = ak.futures_inventory_em(symbol=product), "东方财富库存（AKShare futures_inventory_em）"
            data = normalise_inventory(frame)
        return Fetched(data, source, data["latest"]["date"])

    def roll_yield_products(self) -> frozenset[str] | None:
        """The products AKShare's get_roll_yield_bar can place on an exchange (its own map), None when unknown."""
        try:
            cons = importlib.import_module("akshare.futures.cons")
        except ImportError:
            return None
        table = getattr(cons, "market_exchange_symbols", None)
        if not isinstance(table, dict):
            return None
        return frozenset(str(item).upper() for items in table.values() for item in items) or None

    def term_structure(self, exchange: str, product: str, contract: str, now: datetime) -> Fetched:
        ak, date = self.module(), self._last_trading_date(now)
        if exchange == "DCE":
            name = DCE_SINA_NAMES.get(product.lower())
            if name is None:
                raise LookupError("research_product_not_in_source")
            rows, trade_date = sina_term_rows(ak.futures_zh_realtime(symbol=name), product)
            return Fetched(normalise_term_structure(rows, product), "各月合约结算价（AKShare futures_zh_realtime，新浪）",
                           (trade_date or date).replace("-", ""))
        # get_roll_yield_bar reads the exchange's daily table through AKShare's product → exchange map; a product
        # missing there (广期所铂/钯 in 1.18.94) is not in this source — checked first, so any other error from the
        # call stays a source error (#932). 工业硅/碳酸锂/多晶硅 are mapped and read normally.
        mapped = self.roll_yield_products()
        if mapped is not None and product.upper() not in mapped:
            raise LookupError("research_product_not_in_source")
        frame = ak.get_roll_yield_bar(type_method="symbol", var=product.upper(), date=date)
        return Fetched(normalise_term_structure(frame, product), "各月合约结算价（AKShare get_roll_yield_bar）", date)

    def fees_margin(self, exchange: str, product: str, contract: str, now: datetime) -> Fetched:
        ak = self.module()
        frame = self._table(("fees",), now.astimezone(CHINA).strftime("%Y%m%d"), lambda: ak.futures_comm_info(symbol="所有"))
        data = normalise_fees_margin(frame, contract)
        return Fetched(data, "保证金与手续费参考（AKShare futures_comm_info，九期网）", data.get("feesUpdatedAt") or now.astimezone(CHINA).strftime("%Y-%m-%d"))


# SHFE / INE warehouse receipts. The exchange retired the JSON file AKShare reads
# (dailydata/<date>dailystock.dat answers 404 since September 2026); the daily report is now an
# HTML page with one table per product section: 螺纹钢(仓库), 螺纹钢(厂库), 铜(BC)...
SHFE_RECEIPT_URL = "https://www.shfe.com.cn/data/tradedata/future/stockdata/dailystock_{date}/ZH/all.html"
SHFE_RECEIPT_SECTIONS = {
    "cu": "铜", "al": "铝", "zn": "锌", "pb": "铅", "ni": "镍", "sn": "锡", "ao": "氧化铝", "ad": "铸造铝合金",
    "ru": "天然橡胶", "br": "丁二烯橡胶", "sp": "纸浆", "op": "胶版印刷纸", "fu": "燃料油", "bu": "石油沥青",
    "au": "黄金", "ag": "白银", "rb": "螺纹钢", "wr": "线材", "hc": "热轧卷板", "ss": "不锈钢",
    "sc": "中质含硫原油", "lu": "低硫燃料油", "nr": "20号胶", "bc": "铜(BC)",
}


class _ShfeReceiptRows(HTMLParser):
    """Collects (section, cells) for every data row; a section starts at its special_row_type row."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, list[str]]] = []
        self._section = ""
        self._cells: list[str] | None = None
        self._text: list[str] | None = None
        self._special = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._cells, self._special = [], "special_row_type" in (dict(attrs).get("class") or "")
        elif tag in {"td", "th"} and self._cells is not None:
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._text is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cells is not None and self._text is not None:
            self._cells.append("".join(self._text).strip())
            self._text = None
        elif tag == "tr" and self._cells is not None:
            if self._special and self._cells:
                self._section = self._cells[0]
            elif self._section and self._cells:
                self.rows.append((self._section, self._cells))
            self._cells = None


def _section_product(section: str) -> str:
    """螺纹钢(仓库) / 螺纹钢(厂库) → 螺纹钢; 铜(BC) keeps its suffix (it is 国际铜, a product of its own)."""
    for suffix in ("(仓库)", "(厂库)"):
        if section.endswith(suffix):
            return section[: -len(suffix)]
    return section


def normalise_shfe_receipt_html(html: str, product: str) -> dict[str, Any]:
    name = SHFE_RECEIPT_SECTIONS.get(product.lower())
    if name is None:
        raise LookupError("research_product_not_in_source")
    collector = _ShfeReceiptRows()
    collector.feed(html)
    rows = [cells for section, cells in collector.rows if _section_product(section) == name]
    if not rows:
        raise LookupError("research_product_not_in_source")
    total = change = 0.0
    totals_seen = False
    warehouses: list[tuple[str, float, float]] = []
    for cells in rows:
        quantity, delta = (_number(cell) for cell in cells[-2:]) if len(cells) >= 3 else (None, None)
        # Region subtotals (合计) and the 保税 / 完税 subtotals are not warehouses; 总计 is the section total.
        if quantity is None or delta is None or cells[0] in {"地区", "合计"} or cells[-3].endswith("商品总计"):
            continue
        if cells[0] == "总计":
            total, change, totals_seen = total + quantity, change + delta, True
            continue
        warehouses.append((cells[-3], quantity, delta))
    if not totals_seen:
        total = sum(item[1] for item in warehouses)
        change = sum(item[2] for item in warehouses)
    # The busiest warehouses first; the empty ones say nothing.
    busy = [{"warehouse": name, "quantity": quantity, "change": delta}
            for name, quantity, delta in sorted(warehouses, key=lambda item: -item[1]) if quantity or delta]
    return {"total": total, "change": change, "warehouses": busy[:20]}


def _http_get(url: str) -> str:
    response = httpx.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                                                  " (KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
    if response.status_code == 404:
        raise LookupError("research_product_not_in_source")  # no report for that day
    response.raise_for_status()
    return response.text


# DCE put its data endpoints (dcereport/publicweb/dailystat/*) behind a bot-verification page in
# 2026 (HTTP 412 with a JavaScript challenge): AKShare's rank, receipt and daily-bar calls all fail
# there. The rank has no other free source and is reported as blocked; the term structure comes
# from Sina's contract list and the receipts from Eastmoney's registered-receipt totals.
DCE_SINA_NAMES = {
    "m": "豆粕", "a": "豆一", "b": "豆二", "y": "豆油", "p": "棕榈", "c": "玉米", "cs": "玉米淀粉",
    "i": "铁矿石", "j": "焦炭", "jm": "焦煤", "l": "塑料", "v": "PVC", "pp": "PP", "eg": "乙二醇",
    "eb": "苯乙烯", "pg": "液化石油气", "jd": "鸡蛋", "lh": "生猪", "fb": "纤维板", "bb": "胶合板",
    "rr": "粳米", "lg": "原木", "bz": "纯苯",
}


def sina_term_rows(frame: Any, product: str) -> tuple[list[dict[str, Any]], str | None]:
    """Sina's contracts of one product as the term-structure rows: dated contracts only (not the
    continuous M0), settle = today's settlement once published, else the previous one."""
    rows, trade_date = [], None
    for row in _records(frame):
        name = _text(row.get("symbol")).upper()
        letters = name.rstrip("0123456789")
        if letters != product.upper() or len(name) - len(letters) < 3:
            continue
        settle = _number(row.get("settlement")) or _number(row.get("presettlement"))
        rows.append({"symbol": name, "settle": settle, "close": _number(row.get("trade")) or _number(row.get("close")),
                     "open_interest": _number(row.get("position")), "volume": _number(row.get("volume"))})
        trade_date = trade_date or _text(row.get("tradedate")) or None
    return rows, trade_date


def normalise_receipt_totals(frame: Any) -> dict[str, Any]:
    """Registered-receipt totals (Eastmoney's 期货库存 is the exchange receipt count) without a
    per-warehouse list: the latest total and its change."""
    inventory = normalise_inventory(frame)
    latest = inventory["latest"]
    return {"total": latest["value"], "change": latest["change"] or 0.0, "warehouses": [], "date": latest["date"]}


def last_trading_date(now: datetime, trading_days: Iterable[str] | None = None) -> str:
    """The latest trading day whose day-session data is published (after 17:00 Beijing, today counts).
    `trading_days` (YYYYMMDD) is the exchange calendar (the 快期 summary spans 60 days back to 45 ahead); a day
    outside its span and every day without one skip weekends only."""
    local = now.astimezone(CHINA)
    day = local.date()
    if local.hour < SETTLED_HOUR:
        day -= timedelta(days=1)
    calendar = set(trading_days or ())
    first, last = (min(calendar), max(calendar)) if calendar else ("", "")
    for _ in range(40):
        key = day.strftime("%Y%m%d")
        if (key in calendar) if first <= key <= last else day.weekday() < 5:
            return key
        day -= timedelta(days=1)
    return key


# ---------------------------------------------------------------- service

# The research datasets' last good values live in the shared collection store (research_live_cache) under
# this prefix since 2026-10-18 (#976 step 4); the older research_cache table is read once at start-up and
# left as it is, so a rollback to an earlier build still finds its own rows.
CACHE_PREFIX = "research:"


@dataclass
class ResearchDataService:
    """The six research datasets, read through the shared collector (collection.Collector): one fetch per
    key at a time, the last good value kept (stale on failure), fresh by trading day. `sources` is swapped for
    recorded samples in tests."""

    connection: sqlite3.Connection
    lock: threading.Lock | threading.RLock
    sources: Any = field(default_factory=AkshareSources)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    workers: int = 8

    def __post_init__(self) -> None:
        with self.lock, self.connection:
            # `scope` is the contract for CONTRACT_DATASETS and the product for the rest.
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS research_cache (dataset TEXT NOT NULL, scope TEXT NOT NULL,"
                " trading_day TEXT NOT NULL, fetched_at REAL NOT NULL, payload_json TEXT NOT NULL,"
                " PRIMARY KEY (dataset, scope))"
            )
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(research_cache)")}
            if "product" in columns:
                # Before 2026-10-03 every row was keyed by product, so a contract dataset's row does not
                # say which month it was: those are dropped, the product-level rows carry over.
                self.connection.execute("ALTER TABLE research_cache RENAME COLUMN product TO scope")
                self.connection.execute(
                    f"DELETE FROM research_cache WHERE dataset IN ({', '.join('?' for _ in CONTRACT_DATASETS)})",
                    tuple(sorted(CONTRACT_DATASETS)))
        # Both clocks follow the service clock, so a value's fetch time and "now" are read on one timeline.
        self._kept = LastGood(self.connection, self.lock, prefix=CACHE_PREFIX,
                              clock=lambda: self.clock().timestamp(), wall=lambda: self.clock().timestamp())
        self._import_legacy_rows()
        self._collect = Collector(self._kept, lane="research", workers=self.workers,
                                  error_prefix="research_source_error", timeout_error="research_fetch_timeout")

    def _import_legacy_rows(self) -> None:
        """Good rows of the older research_cache table, where the collection store has none or an older one: after
        a rollback the old version kept writing research_cache, so upgrading again takes its newer values (#932)."""
        with self.lock, self.connection:
            rows = self.connection.execute(
                "SELECT dataset, scope, trading_day, fetched_at, payload_json FROM research_cache").fetchall()
            for dataset, scope, trading_day, fetched_at, payload_json in rows:
                try:
                    payload = json.loads(payload_json)
                except ValueError:
                    continue
                if not isinstance(payload, dict) or payload.get("status") != "ok":
                    continue
                self.connection.execute(
                    f"INSERT INTO {LAST_GOOD_TABLE} (key, fetched_at, payload_json) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET fetched_at = excluded.fetched_at, payload_json = excluded.payload_json"
                    f" WHERE excluded.fetched_at > {LAST_GOOD_TABLE}.fetched_at",
                    (f"{CACHE_PREFIX}{dataset}:{scope}", float(fetched_at),
                     json.dumps({**payload, "tradingDay": trading_day}, ensure_ascii=False)))

    def close(self) -> None:
        self._collect.close()

    # -- public

    def dataset(self, name: str, symbol: str) -> dict[str, Any]:
        if name not in DATASETS:
            raise KeyError(name)
        return self.summary(symbol, (name,))["datasets"][name]

    def summary(self, symbol: str, names: Iterable[str] = DATASETS) -> dict[str, Any]:
        wanted = [name for name in names if name in DATASETS]
        exchange, product, foreign = research_target(symbol)
        if foreign is not None:
            # 这些数据集（持仓排名、仓单、基差、库存…）都是国内交易所的，外盘没有：明说，不当成故障。
            return {"symbol": symbol, "exchange": exchange, "product": product, "contract": symbol,
                    "generatedAt": self.clock().isoformat(), "foreign": True,
                    "datasets": {name: {"dataset": name, "status": "unavailable", "error": "research_foreign_not_applicable"}
                                 for name in wanted},
                    "notice": FOREIGN_DATA_NOTICE}
        exchange, product, contract = split_symbol(symbol)
        now = self.clock()
        requests = [Request(self._key(name, product, contract),
                            functools.partial(self._fetch, name, exchange, product, contract, now), "research_cache_missing")
                    for name in wanted]
        # epoch: a failure's 10-minute back-off holds within its trading day; at 17:00 the day turns and the
        # new day's data is fetched at once instead of after the back-off (#932).
        answers = self._collect.read_many(requests, ttl=INTRADAY_TTL_SECONDS, retry=FAILURE_TTL_SECONDS,
                                          wait=FETCH_TIMEOUT_SECONDS + 1, valid_for=self._valid_for,
                                          epoch=self._trading_day(now))
        items = {name: self._shaped(name, answers[request.key]) for name, request in zip(wanted, requests, strict=True)}
        return {"symbol": symbol, "exchange": exchange, "product": product, "contract": contract,
                "generatedAt": now.isoformat(), "datasets": items,
                "notice": "研究数据来自 AKShare 免费爬虫源（交易所官网、新浪、东方财富），部分数据集可能缺失或延迟；缺失不是故障。"}

    # -- cache

    def _trading_day(self, now: datetime) -> str:
        calendar = getattr(self.sources, "trading_days", None)
        return last_trading_date(now, calendar() if calendar else None)

    def _valid_for(self, kept: Kept) -> float:
        """How long a stored value stays fresh from its fetch: not past its trading day; once the day session's
        settlement data is out (after 17:00), a value fetched after 17:00 the same day lasts the day; otherwise
        an hour."""
        now = self.clock()
        if not isinstance(kept.value, dict) or kept.value.get("tradingDay") != self._trading_day(now):
            return 0.0
        local = now.astimezone(CHINA)
        fetched_local = datetime.fromtimestamp(kept.saved_at, tz=CHINA)
        if local.hour >= SETTLED_HOUR and fetched_local.date() == local.date() and fetched_local.hour >= SETTLED_HOUR:
            return float("inf")
        return INTRADAY_TTL_SECONDS

    @staticmethod
    def _key(name: str, product: str, contract: str) -> str:
        """The stored value and the in-flight fetch are both per this key."""
        return f"{name}:{(contract if name in CONTRACT_DATASETS else product).lower()}"

    @staticmethod
    def _shaped(name: str, answer: dict[str, Any]) -> dict[str, Any]:
        """The collector's answer in the dataset shape every reader expects (an unavailable one included)."""
        if answer.get("status") == "unavailable":
            return {**ResearchDataService._unavailable(name, str(answer.get("error") or "research_cache_missing"))}
        return {key: value for key, value in answer.items() if key != "tradingDay"}

    # -- fetch

    def _fetch(self, name: str, exchange: str, product: str, contract: str, now: datetime) -> dict[str, Any]:
        """One read of one dataset. The scraper runs on its own throwaway thread: AKShare's requests often carry
        no timeout, so a hung site must not hold a collector worker — the thread is abandoned, not joined."""
        fetcher = getattr(self.sources, name)
        started = time.monotonic()
        single = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"research-{name}")
        try:
            fetched: Fetched = single.submit(fetcher, exchange, product, contract, now).result(timeout=FETCH_TIMEOUT_SECONDS)
        except FutureTimeout:
            return self._unavailable(name, "research_fetch_timeout")
        except LookupError as error:
            return self._unavailable(name, str(error) or "research_product_not_in_source")
        except RuntimeError as error:
            return self._unavailable(name, str(error))
        except Exception as error:  # noqa: BLE001 - scraper sources fail in every conceivable way
            log.warning("research_fetch_failed dataset=%s product=%s error=%s", name, product, type(error).__name__)
            return self._unavailable(name, f"research_source_error:{type(error).__name__}")
        finally:
            single.shutdown(wait=False, cancel_futures=True)
        return {"dataset": name, "status": "ok", "asOf": fetched.as_of, "source": fetched.source,
                "fetchedAt": now.isoformat(), "elapsedSeconds": round(time.monotonic() - started, 2), "data": fetched.data,
                "tradingDay": self._trading_day(now)}

    @staticmethod
    def _unavailable(name: str, error: str) -> dict[str, Any]:
        return {"dataset": name, "status": "unavailable", "asOf": None, "source": None, "error": error, "data": None}

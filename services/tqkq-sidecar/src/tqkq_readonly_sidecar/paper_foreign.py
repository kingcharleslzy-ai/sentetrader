"""外盘本站模拟（外盘二期，2026-10-19 所有者定）：同一个撮合引擎的第二个实例，只撮合外盘。

所有者口径（10-18 方案、10-19 恢复开发）：MT5 只取行情，外盘委托一笔不经过 MT5，全部在本站
撮合引擎里成交；每个本站模拟账户另有一个美元子账户（单独一本账，本实例的数据库）；合约是
具体月份（MT5 的 ``GCEZ26``），不用主连。

与国内引擎的差别全部在这里配置，引擎与账本本身不分叉：

* 时间：引擎本地时区是美东（``NEW_YORK``），交易日 18:00 换日，正是 CME Globex 的交易日
  （周日 18:00 开周一的交易日）；各品种按自己的 CME 时段（``cme_hours``：23 小时、谷物两段、瘦肉猪日间，
  节假日的半天盘与停盘），时段外不收新单、挂单不撮合。
* 日历：周一到周五去掉 CME 全天休市日（``cme_hours.CME_CLOSED_DAYS``，节日当天的半天盘归下一个交易日）。
* 行情与合约资料：``Mt5PaperSource`` 轮询 4090 上的 MT5 只读行情桥（与 Runtime 外盘行情同一个
  桥）。合约名是 ``CME.<MT5 合约名>``（AMP 把 CME 集团各交易所都放在 ``Exchange-Futures\\CME``
  下）；乘数 = tickValue / tickSize、保证金按交易所隔夜标准（AMP 公开保证金表，取不到才用 MT5 的 marginInitial），手续费每手每边固定
  ``COMMISSION_PER_LOT`` 美元（所有者 10-18 同意固定费率）。CME 金属、能源没有涨跌停，
  合约资料里的涨跌停取参考价 ±``PRICE_BAND``，只为挡住明显打错的价格。
* 成交：理想模式（按对手方最优价）。MT5 只有一档盘口，第一版不做排队模型。
* 结算（2026-10-20 所有者定）：优先用交易所正式结算价——MT5 的 session_price_settlement 在 CME 公布
  后一小时内更新（10-20 实测：金银北京 01:30 公布、01:40 已更新，原油美豆 02:40 前更新），比美东 18:00
  换日早得多。本交易日盘中看到这个字段从开盘时的值变了，变后的值就是当日正式结算价；没看到变化
  （MT5 没更新、引擎中途重启说不清）就退回收盘价近似：结算日美东 17:00 前最后一根 1 分钟线的收盘价。
  到期合约的最后交易日同一取法。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .cme_hours import CME_CLOSED_DAYS, DEFAULT_KIND, kind_of
from .main import CHINA_TIMEZONE, trading_day_from_calendar

NEW_YORK = ZoneInfo("America/New_York")
# 交易时段（2026-10-21）：行情的 ``trading_time`` 只写品种的时段种类（``{"cmeHours": 种类}``），引擎按当时的交易日
# 从 ``cme_hours`` 取那一天的时段——平日是 CME Globex 23 小时（谷物两段、瘦肉猪日间），节假日按 CME 公布的半天盘与
# 停盘（股指、美债、金属能源、外汇各自的提前收盘与 17:00 重开）。``cme_hours`` 与 Runtime 的 trading_calendar 同一份，
# 有测试核对。时段外的价格照样更新，不收新单，挂单原样保留不成交。
CME_SCHEDULE: dict[str, Any] = {"cmeHours": DEFAULT_KIND}


def product_schedule(root: str | None) -> dict[str, Any]:
    """一个外盘品种（MT5 根代码）的交易时段（新的一份）。"""
    return {"cmeHours": kind_of(root)}


CALENDAR_PAST_DAYS = 30
CALENDAR_AHEAD_DAYS = 60

FOREIGN_EXCHANGE = "CME"
COMMISSION_PER_LOT = 2.5
PRICE_BAND = 0.5
FOREIGN_INITIAL_BALANCE = 100_000.0
# 最近一笔逐笔在这么久之内，行情时间记读取时刻；更久没跳的如实记最后一笔的时间（见 quote_record）。
TICK_FRESH_MS = 60_000
MT5_MONTHS = "FGHJKMNQUVXZ"
_MONTH_SYMBOL = re.compile(rf"^([A-Z0-9]+?)([{MT5_MONTHS}])(\d{{2}})$")


def cme_trading_dates(now: datetime | None = None) -> set[str]:
    """``now`` 前后的 CME 交易日（周一到周五去掉全天休市日）。"""
    base = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK).date()
    days = (base + timedelta(days=offset) for offset in range(-CALENDAR_PAST_DAYS, CALENDAR_AHEAD_DAYS + 1))
    return {day.isoformat() for day in days if day.weekday() < 5 and day.isoformat() not in CME_CLOSED_DAYS}


def engine_symbol(mt5_name: str) -> str:
    return f"{FOREIGN_EXCHANGE}.{mt5_name}"


def mt5_name(symbol: str) -> str:
    return str(symbol).split(".", 1)[-1]


def delivery(mt5_symbol: str) -> tuple[str, int, int] | None:
    """MT5 月份合约名 → (根代码, 交割年, 交割月)；不是 根代码+月份字母+两位年份 的为 None。"""
    match = _MONTH_SYMBOL.match(mt5_symbol)
    if match is None:
        return None
    return match.group(1), 2000 + int(match.group(3)), MT5_MONTHS.index(match.group(2)) + 1


def _iso_ns(value: Any) -> float | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp() * 1e9


def _price(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# 外盘保证金按交易所隔夜标准（所有者 2026-10-21 定）：MT5（AMP）合约资料里的 marginInitial 是 AMP 的日内保证金
# （迷你标普 400 美元），持仓过夜要的是交易所标准（约 2.9 万美元）。CME 官网拦截脚本访问；AMP 公开的保证金表
# 逐品种列出交易所维持保证金（“*Maintenance”）与日内保证金，且“CQG Symbol”列就是 AMP 的 MT5 根代码（EP、GCE…）。
EXCHANGE_MARGINS_URL = os.environ.get("AI_TRADER_FOREIGN_MARGINS_URL", "https://ampfutures.com/trading-info/margins")
EXCHANGE_MARGINS_MIN_ROWS = 20         # 一份能用的表至少这么多行；少了当页面改版或读残，不替换上一份
EXCHANGE_MARGINS_REFRESH_SECONDS = 12 * 3600.0
EXCHANGE_MARGINS_RETRY_SECONDS = 3600.0
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_MONEY = re.compile(r"^\$([\d,]+(?:\.\d+)?)$")


def parse_exchange_margins(page: str) -> dict[str, float]:
    """AMP 保证金表 → {MT5 根代码: 交易所维持保证金（美元/手）}。表头是 名称、交易所代码、CQG 代码、交易所、
    *Maintenance、Day Trading；只收 CQG 代码非空、维持保证金是金额的行。"""
    import html as _html
    margins: dict[str, float] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", cell))).strip() for cell in _CELL.findall(row)]
        if len(cells) < 6:
            continue
        money = _MONEY.match(cells[4])
        root = cells[2].upper()
        if money and root and re.fullmatch(r"[A-Z0-9]+", root):
            value = float(money.group(1).replace(",", ""))
            if value > 0:
                margins[root] = value
    return margins


class ExchangeMargins:
    """交易所隔夜保证金表：每 12 小时读一次 AMP 公开的保证金表，读到的存成 JSON（重启照用）；读失败或读残留着上一份，
    一小时后再试；一份都没有时为空，合约资料退回 MT5 的日内保证金（与改之前一样）。"""

    def __init__(self, path: str | None, *, fetch: Callable[[], str] | None = None,
                 url: str = EXCHANGE_MARGINS_URL) -> None:
        self.path = path
        self.url = url
        self._fetch = fetch
        self.margins: dict[str, float] = {}
        self.fetched_at: float | None = None
        self._retry_at = 0.0
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    saved = json.load(handle)
                self.margins = {str(key): float(value) for key, value in (saved.get("margins") or {}).items()}
                self.fetched_at = float(saved.get("fetchedAt") or 0.0) or None
            except (OSError, ValueError, TypeError) as error:
                _log(f"exchange_margins_unreadable path={path} error={type(error).__name__}")

    def _download(self) -> str:
        if self._fetch is not None:
            return self._fetch()
        request = urllib.request.Request(self.url, headers={"User-Agent": "Mozilla/5.0 (ai-trader paper engine)"})
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 — fixed https URL
            return response.read().decode("utf-8", errors="ignore")

    def refresh_if_due(self, now: float) -> bool:
        """到期就读一次；表有变化返回 True（调用方据此重算合约资料的保证金）。"""
        due = self.fetched_at is None or now - self.fetched_at >= EXCHANGE_MARGINS_REFRESH_SECONDS
        if not due or now < self._retry_at:
            return False
        try:
            fresh = parse_exchange_margins(self._download())
        except Exception as error:  # noqa: BLE001 — a failed read keeps the last table
            _log(f"exchange_margins_fetch_failed error={type(error).__name__}")
            self._retry_at = now + EXCHANGE_MARGINS_RETRY_SECONDS
            return False
        if len(fresh) < EXCHANGE_MARGINS_MIN_ROWS:
            _log(f"exchange_margins_too_few rows={len(fresh)}")
            self._retry_at = now + EXCHANGE_MARGINS_RETRY_SECONDS
            return False
        changed = fresh != self.margins
        self.margins, self.fetched_at = fresh, now
        if self.path:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump({"fetchedAt": now, "source": self.url, "margins": fresh}, handle, ensure_ascii=False)
                os.replace(tmp, self.path)
            except OSError as error:
                _log(f"exchange_margins_unsaved error={type(error).__name__}")
        return changed


def contract_record(row: dict[str, Any], commission: float = COMMISSION_PER_LOT,
                    overnight: dict[str, float] | None = None) -> dict[str, Any] | None:
    """行情桥 /v1/symbols 的一行 → 引擎合约资料（字段同 paper_contracts）。不是月份期货、
    缺乘数/最小变动价位/到期时间的为 None。保证金取交易所隔夜标准（``overnight``，按根代码），
    表里没有这个品种才退回 MT5 的 marginInitial（AMP 日内保证金）。"""
    name = str(row.get("symbol") or "")
    parsed = delivery(name)
    multiple, tick = _price(row.get("multiplier")), _price(row.get("tickSize"))
    expire = _iso_ns(row.get("expirationTime"))
    if parsed is None or not multiple or not tick or expire is None:
        return None
    root, year, month = parsed
    margin = (overnight or {}).get(root) or _price(row.get("marginInitial"))
    return {
        "symbol": engine_symbol(name), "ins_class": "FUTURE", "product_id": root,
        "volume_multiple": multiple, "price_tick": tick, "expire_datetime": expire,
        "delivery_year": year, "delivery_month": month,
        "margin": margin if margin and margin > 0 else float("nan"),
        "commission": float(commission),
        "instrument_name": str(row.get("description") or name),
    }


def quote_record(row: dict[str, Any], contract: dict[str, Any] | None,
                 as_of_ms: int | None = None) -> dict[str, Any] | None:
    """行情桥 /v1/quotes 的一条 → 引擎行情。MT5 只在报价变化时出逐笔：最近一笔在 ``TICK_FRESH_MS`` 之内时，
    行情时间记这次读取的时刻（桥时钟 ``as_of_ms``，终端连着时此刻的买卖价就是读到的这组——与国内快照同一
    含义）；更久没跳（休市中的品种，如瘦肉猪只在 CME 日间交易）就记最后一笔的时间，行情如实是旧的，
    下单前的新鲜度检查会拦住。从没出过逐笔（``timeMsc`` 为空）的不算行情。时间换成北京时间字符串（引擎与
    Runtime 读行情时间都按北京时间，交易日的归属由引擎按美东时区判断）。结算价字段一律留空：交易所结算价
    不进撮合引擎（见模块说明）。"""
    name = str(row.get("symbol") or "")
    tick_ms = row.get("timeMsc")
    if not name or not tick_ms:
        return None
    stamp = int(tick_ms)
    if as_of_ms and 0 <= int(as_of_ms) - stamp <= TICK_FRESH_MS:
        stamp = int(as_of_ms)
    moment = datetime.fromtimestamp(stamp / 1000, tz=UTC).astimezone(CHINA_TIMEZONE)
    bid, ask, last = _price(row.get("bid")), _price(row.get("ask")), _price(row.get("last"))
    if last is None and bid is not None and ask is not None:
        last = (bid + ask) / 2
    if last is None:
        return None
    nan = float("nan")
    reference = _price(row.get("settlement")) or last
    band = abs(reference) * PRICE_BAND
    quote: dict[str, Any] = {
        "symbol": engine_symbol(name),
        "datetime": moment.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "last_price": last,
        "ask_price1": ask if ask is not None else nan,
        "bid_price1": bid if bid is not None else nan,
        "ask_volume1": 0.0, "bid_volume1": 0.0,
        "ins_class": "FUTURE",
        "trading_time": product_schedule((contract or {}).get("product_id") or (delivery(name) or (None,))[0]),
        "upper_limit": reference + band if band else nan,
        "lower_limit": reference - band if band else nan,
        "settlement": nan, "pre_settlement": nan, "pre_close": nan,
        "open": _price(row.get("sessionOpen")) or nan,
        "highest": _price(row.get("sessionHigh")) or nan,
        "lowest": _price(row.get("sessionLow")) or nan,
        "volume": float(row.get("sessionVolume") or 0),
        "open_interest": float(row.get("openInterest") or 0),
    }
    if contract:
        tick = contract.get("price_tick")
        if tick and band:
            # 涨跌停对齐到最小变动价位，否则合法价位也可能落在带外的半跳上。
            quote["upper_limit"] = math.floor(quote["upper_limit"] / tick) * tick
            quote["lower_limit"] = math.ceil(quote["lower_limit"] / tick) * tick
        for key in ("product_id", "volume_multiple", "price_tick", "expire_datetime", "delivery_year",
                    "delivery_month", "margin", "commission", "instrument_name"):
            if key in contract:
                quote[key] = contract[key]
    return quote


def close_before(bars: list[dict[str, Any]], day: str) -> float | None:
    """结算日 ``day``（CME 交易日）的收盘价近似：美东 17:00 前、本交易日开盘（前一自然日
    18:00）之后最后一根 1 分钟线的收盘价。MT5 的 K 线时间是开盘时刻（交易服务器≈UTC）。"""
    end = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=NEW_YORK) + timedelta(hours=17)
    start = end - timedelta(hours=23)
    best: tuple[int, float] | None = None
    for bar in bars:
        try:
            opened = int(bar["time"])
        except (KeyError, TypeError, ValueError):
            continue
        close = _price(bar.get("close"))
        if close is None or not start.timestamp() <= opened < end.timestamp():
            continue
        if best is None or opened > best[0]:
            best = (opened, close)
    return best[1] if best else None


class BridgeError(RuntimeError):
    pass


class Mt5PaperSource:
    """外盘撮合行情源：轮询 MT5 只读行情桥，喂给引擎行情、合约资料和收盘价近似结算价。

    订阅集 = 引擎 wanted_symbols()（持仓、挂单、最近读过报价的合约），每秒一批（桥一次最多 50 个）。
    合约资料启动时取一次，之后每 ``CONTRACT_REFRESH_SECONDS`` 一次（失败一分钟后再试）。有账户等
    结算价时每 ``SETTLEMENT_RETRY_SECONDS`` 按 1 分钟线补一次。桥不通时引擎照常在 60 秒后把行情
    状态降为 unavailable；外盘不切别的源撮合（所有者 10-18：MT5 断了只看行情、暂停外盘撮合）。
    """

    POLL_SECONDS = 1.0
    RETRY_SECONDS = 5.0
    # 与 Runtime 外盘合约表同一节奏（半小时）：Runtime 目录里刚挂牌的月份，引擎这边不会还当不存在。
    CONTRACT_REFRESH_SECONDS = 1800.0
    CONTRACT_RETRY_SECONDS = 60.0
    SETTLEMENT_RETRY_SECONDS = 60.0
    BATCH = 50
    BAR_LIMIT = 10_000
    TIMEOUT_SECONDS = 6.0

    def __init__(self, base_url: str, token: str, *,
                 get_json: Callable[[str], Any] | None = None,
                 clock: Callable[[], float] = time.time,
                 commission: float = COMMISSION_PER_LOT,
                 margins: ExchangeMargins | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.margins = margins
        self.token = token
        self._get_json = get_json
        self._clock = clock
        self.commission = float(commission)
        self._contracts: dict[str, dict[str, Any]] = {}
        self._contracts_at: float | None = None
        self._contracts_retry_at = 0.0
        self._settlement_at = 0.0
        self._fed: dict[str, tuple[Any, ...]] = {}
        # 交易所结算价（session_price_settlement）的盘中观察：(合约, 交易日) → 当日第一次看到的值，与盘中
        # 变成的新值（= 当日正式结算价）。只记本进程看到的；重启后当日说不清就退回收盘价近似。
        self._settle_first: dict[tuple[str, str], float] = {}
        self._settle_official: dict[tuple[str, str], float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- 桥 ------------------------------------------------------------------------------
    def _get(self, path: str) -> Any:
        if self._get_json is not None:
            return self._get_json(path)
        request = urllib.request.Request(self.base_url + path, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=self.TIMEOUT_SECONDS) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise BridgeError(f"mt5_bridge_http_{error.code}") from None
        except (OSError, ValueError) as error:
            raise BridgeError(f"mt5_bridge_unreachable:{type(error).__name__}") from None

    # -- 生命周期 ------------------------------------------------------------------------
    def start(self, engine: Any) -> None:
        self._thread = threading.Thread(target=self._run, args=(engine,), name="paper-mt5-quotes", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self, engine: Any) -> None:
        while not self._stop.is_set():
            try:
                self.step(engine)
                pause = self.POLL_SECONDS
            except BridgeError as error:
                engine.mark_source_reconnecting()
                _log(f"paper mt5 source retry: {error}")
                pause = self.RETRY_SECONDS
            except Exception as error:  # 一次轮询失败不让线程退出
                engine.mark_source_reconnecting()
                _log(f"paper mt5 source error: {type(error).__name__}: {str(error)[:200]}")
                pause = self.RETRY_SECONDS
            self._stop.wait(pause)

    # -- 一轮 ----------------------------------------------------------------------------
    def step(self, engine: Any) -> None:
        """一轮轮询（测试直接调）：合约资料到期就刷新，再取订阅集的报价，再补等着的结算价。"""
        now = self._clock()
        if self.margins is not None and self.margins.refresh_if_due(now):
            self._contracts_at = None   # 保证金表变了：合约资料马上按新表重算一遍
        if self._contracts_at is None or now - self._contracts_at >= self.CONTRACT_REFRESH_SECONDS:
            if now >= self._contracts_retry_at:
                self._refresh_contracts(engine, now)
        self._feed_quotes(engine)
        if now - self._settlement_at >= self.SETTLEMENT_RETRY_SECONDS:
            self._settlement_at = now
            self._feed_settlements(engine)

    def _refresh_contracts(self, engine: Any, now: float) -> None:
        try:
            rows = self._get("/v1/symbols?path=Exchange-Futures").get("symbols") or []
        except BridgeError as error:
            engine.note_contract_refresh_failed(str(error))
            self._contracts_retry_at = now + self.CONTRACT_RETRY_SECONDS
            raise
        overnight = self.margins.margins if self.margins is not None else None
        records = [record for record in (contract_record(row, self.commission, overnight) for row in rows) if record]
        if not records:
            # 终端刚起、还没列出合约：不当成“目录为空”，一分钟后再取。
            self._contracts_retry_at = now + self.CONTRACT_RETRY_SECONDS
            return
        self._contracts = {record["symbol"]: record for record in records}
        self._contracts_at = now
        engine.feed_contracts(
            [{key: value for key, value in record.items() if key != "instrument_name"} for record in records],
            listed=list(self._contracts))

    def _feed_quotes(self, engine: Any) -> None:
        wanted = sorted(engine.wanted_symbols())
        if self._contracts_at is not None:
            unknown = [symbol for symbol in wanted if symbol not in self._contracts]
            if unknown:
                engine.reject_symbols(unknown, "mt5_symbol_not_listed")
                wanted = [symbol for symbol in wanted if symbol in self._contracts]
        if not wanted:
            engine.mark_source_connected()
            return
        for index in range(0, len(wanted), self.BATCH):
            names = ",".join(mt5_name(symbol) for symbol in wanted[index:index + self.BATCH])
            payload = self._get(f"/v1/quotes?symbols={urllib.parse.quote(names, safe=',')}")
            engine.mark_source_connected()
            as_of = payload.get("bridgeTimeMs")
            day = self._trading_day()
            for row in payload.get("quotes") or []:
                symbol = engine_symbol(str(row.get("symbol") or ""))
                self._watch_settlement(symbol, day, _price(row.get("settlement")))
                quote = quote_record(row, self._contracts.get(symbol), as_of if isinstance(as_of, int) else None)
                if quote is None:
                    continue
                key = (quote["datetime"], quote["last_price"], quote["bid_price1"], quote["ask_price1"])
                if self._fed.get(symbol) == key:
                    continue
                self._fed[symbol] = key
                engine.feed_quote(quote)

    def _trading_day(self) -> str | None:
        """此刻的 CME 交易日（美东 18:00 换日）；日历算不出时为 None（不记结算价观察）。"""
        now = datetime.fromtimestamp(self._clock(), tz=NEW_YORK)
        try:
            return trading_day_from_calendar(now, cme_trading_dates(now), NEW_YORK)
        except RuntimeError:
            return None

    def _watch_settlement(self, symbol: str, day: str | None, value: float | None) -> None:
        """盘中记 MT5 结算价字段：交易日内第一次看到的值是上一交易日的结算价；之后变成的值就是本交易日的
        正式结算价（CME 当天公布、MT5 随即更新）。只留最近几天。"""
        # A missing price comes as null (the bridge maps MT5's 0.0 to null); a real negative energy settlement stays.
        if day is None or value is None or value == 0:
            return
        key = (symbol, day)
        first = self._settle_first.setdefault(key, value)
        if value != first:
            self._settle_official[key] = value
        if len(self._settle_first) > 400:
            for old in sorted({d for _, d in self._settle_first})[:-5]:
                for table in (self._settle_first, self._settle_official):
                    for stale in [k for k in table if k[1] == old]:
                        table.pop(stale, None)

    def _settlement(self, symbol: str, day: str) -> float | None:
        """结算日 ``day`` 的结算价：盘中看到的交易所正式结算价，没有就退回收盘价近似。"""
        official = self._settle_official.get((symbol, day))
        if official is not None:
            return official
        return self._close(symbol, day)

    def _close(self, symbol: str, day: str) -> float | None:
        end = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=NEW_YORK) + timedelta(hours=17)
        minutes = int(max(0.0, self._clock() - end.timestamp()) // 60) + 23 * 60 + 5
        count = max(1, min(self.BAR_LIMIT, minutes))
        payload = self._get(f"/v1/bars?symbol={urllib.parse.quote(mt5_name(symbol))}&timeframe=M1&count={count}&start=0")
        return close_before(payload.get("bars") or [], day)

    def _feed_settlements(self, engine: Any) -> None:
        """等结算价的持仓合约按目标日收盘价近似补上；到期合约按最后交易日补（查不到也报 checked，
        引擎据此在挂起满一个交易日后暂估）。单个合约取线失败只跳过它，下一轮再试。"""
        pending = sorted(engine.pending_settlement_symbols())
        if pending:
            target = engine.settlement_target_day()
            if target:
                items = []
                for symbol in pending:
                    try:
                        price = self._settlement(symbol, target)
                    except BridgeError as error:
                        _log(f"paper mt5 settlement bars failed ({symbol}): {error}")
                        continue
                    if price is not None:
                        items.append({"symbol": symbol, "tradingDay": target, "settlement": price})
                if items:
                    engine.feed_settlements(items)
        finals = engine.pending_final_settlements()
        if finals:
            items, checked = [], {}
            for symbol, day in sorted(finals.items()):
                try:
                    price = self._settlement(symbol, day)
                except BridgeError as error:
                    _log(f"paper mt5 final settlement bars failed ({symbol}): {error}")
                    continue
                checked[symbol] = day
                if price is not None:
                    items.append({"symbol": symbol, "tradingDay": day, "settlement": price})
            engine.feed_settlements(items, checked=checked)


def _log(message: str) -> None:
    try:
        import sys

        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def bridge_settings() -> tuple[str, str] | None:
    """MT5 行情桥地址与令牌（与 Runtime 外盘行情同一组环境变量）；没配齐为 None。"""
    url = (os.environ.get("AI_TRADER_MT5_BRIDGE_URL") or "").strip()
    token = ""
    path = os.environ.get("AI_TRADER_MT5_BRIDGE_TOKEN_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError:
            token = ""
    token = token or (os.environ.get("AI_TRADER_MT5_BRIDGE_TOKEN") or "").strip()
    return (url, token) if url and token else None

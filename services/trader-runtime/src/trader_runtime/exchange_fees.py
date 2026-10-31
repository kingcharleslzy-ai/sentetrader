"""交易所标准手续费（所有者 2026-10-08 决定：不自定义费率就按交易所标准）。

来源是 openctp 的每日汇总页（http://openctp.cn/fees.html，只能 http，单页全部合约，
标题带日期）。openctp 的数字 = 交易所标准 + 它合作经纪商的固定加收（费率 +0.000001、
每手 +0.01 元），读进来先减掉加收还原成交易所标准。

* 读路径（下单估算 / paper 扣费 / 回测）永不联网：只读进程内缓存 → 缓存文件，
  文件 fetchedDay 不是今天就照用但标 stale；都没有返回 None（调用方各自退回兜底口径）。
* 联网抓取只在后台：``refresh`` 抓一次并原子写 ``state_root/exchange-fees.json``
  （超时 15 秒封顶），``start_refresher`` 起守护线程——启动即刷新，之后每 30 分钟
  检查当天（北京时间）是否已成功抓过，没有就重试；手上一份表都没有（冷启动且从没抓成功）
  时每 2 分钟重试，不让下单估算干等 30 分钟。测试环境用
  ``AI_TRADER_EXCHANGE_FEES_DISABLED`` 不起线程，直接种缓存文件或测解析函数。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .host_throttle import GENTLE
from .last_good import read_json, write_json_atomic
from .research_data import CHINA

log = logging.getLogger("uvicorn.error.research")

OPENCTP_FEES_URL = "http://openctp.cn/fees.html"
OPENCTP_HOST = "openctp.cn"
FETCH_TIMEOUT_SECONDS = 15.0
# openctp 合作经纪商的固定加收：费率按成交额比例、每手按固定元数。
BROKER_RATIO_ADDON = 0.000001
BROKER_PER_LOT_ADDON = 0.01
CACHE_NAME = "exchange-fees.json"
DISABLED_ENV = "AI_TRADER_EXCHANGE_FEES_DISABLED"
# 后台刷新节奏：当天（北京时间）没抓成功就每隔这么久重试一次。
REFRESH_INTERVAL_SECONDS = 30 * 60
# 冷启动：内存与缓存文件都没有任何一份表时的重试间隔。
COLD_RETRY_SECONDS = 2 * 60

_lock = threading.Lock()
# 进程内缓存：(root 路径字符串) → 已归一的表；读路径不再碰盘或联网。
_tables: dict[str, dict[str, Any]] = {}
# 每个 root 的刷新线程与其停止开关（start_refresher 幂等）。
_refreshers: dict[str, tuple[threading.Thread, threading.Event]] = {}
_refresher_lock = threading.Lock()


def _network_allowed() -> bool:
    return not os.environ.get(DISABLED_ENV)


def _today() -> str:
    return datetime.now(CHINA).strftime("%Y-%m-%d")


# -- 解析（纯函数，测试用录制的行） -------------------------------------------------


class _FeesPageParser(HTMLParser):
    """表格页最小解析：<title> 的日期与每个 <tr> 的 <td> 文本列表。"""

    def __init__(self) -> None:
        super().__init__()
        self.title: list[str] = []
        self.rows: list[list[str]] = []
        self._in_title = False
        self._in_cell = False
        self._row: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ARG002
        if tag == "tr":
            self._row = []
        elif tag == "title":
            self._in_title = True
        elif tag in {"td", "th"} and self._row is not None:
            self._in_cell = True
            self._row.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._row is not None:
            if any(cell.strip() for cell in self._row):
                self.rows.append(self._row)
            self._row = None
        elif tag == "title":
            self._in_title = False
        elif tag in {"td", "th"}:
            self._in_cell = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        elif self._in_cell and self._row is not None:
            self._row[-1] += data


def _clean_ratio(raw: str) -> float:
    """减掉经纪商费率加收并去掉浮点噪声；不为负。"""
    return max(0.0, round(float(raw) - BROKER_RATIO_ADDON, 9))


def _clean_per_lot(raw: str) -> float:
    return max(0.0, round(float(raw) - BROKER_PER_LOT_ADDON, 3))


def _exchange_rate(open_pair: tuple[float, float], close_pair: tuple[float, float],
                   close_today_pair: tuple[float, float]) -> dict[str, Any]:
    """三栏（开/平/平今）各自的 (费率, 每手) → 一条 FeeRate 形状的费率。

    任一栏费率 >0 就整条按 notional 表达（万分之数 = 费率 × 10000），每手部分进
    对应的 perLotExtra；三栏都是纯每手时才用 per_lot。同一栏两者都 >0 时两部分都算。
    """
    columns = (open_pair, close_pair, close_today_pair)
    mode = "notional" if any(ratio > 0 for ratio, _ in columns) else "per_lot"
    rate: dict[str, Any] = {"mode": mode}
    for key, (ratio, per_lot) in zip(("open", "closeYesterday", "closeToday"), columns):
        if mode == "notional":
            rate[key] = round(ratio * 10000, 9)
            rate[f"{key}PerLotExtra"] = per_lot
        else:
            rate[key] = per_lot
            rate[f"{key}PerLotExtra"] = 0.0
    return rate


def parse_fees_page(html: str) -> tuple[str | None, dict[str, dict[str, Any]]]:
    """openctp fees.html → (页面日期 asOf, 合约 → 条目)。条目：exchange / contract /
    product / multiplier / rate（FeeRate 形状）。解析不出任何合约时合约表为空。"""
    parser = _FeesPageParser()
    parser.feed(html)
    # The live page's <title> has no date (10-08): it is in the heading
    # “期货交易手续费与保证金 - 20261008”; the title is tried first for an older layout.
    match = (re.search(r"(\d{4})(\d{2})(\d{2})", "".join(parser.title))
             or re.search(r"手续费与保证金\s*-\s*(\d{4})(\d{2})(\d{2})", html))
    as_of = "-".join(match.groups()) if match else None
    contracts: dict[str, dict[str, Any]] = {}
    for row in parser.rows:
        cells = [cell.strip() for cell in row]
        if len(cells) < 13:
            continue
        try:
            multiplier = float(cells[5])
            columns = tuple(
                (_clean_ratio(cells[index]), _clean_per_lot(cells[index + 1]))
                for index in (7, 9, 11))
        except ValueError:
            continue
        exchange, contract, product = cells[0].upper(), cells[1].upper(), cells[3].upper()
        if not re.fullmatch(r"[A-Z]{1,3}\d+", contract):
            continue
        contracts[f"{exchange}.{contract}"] = {
            "exchange": exchange, "contract": contract, "product": product,
            "multiplier": multiplier,
            "rate": _exchange_rate(*columns),
        }
    return as_of, contracts


# -- 抓取与缓存 ---------------------------------------------------------------------


def _fetch_html() -> str:
    import httpx

    with GENTLE.turn(OPENCTP_HOST):
        with httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = client.get(OPENCTP_FEES_URL)
            response.raise_for_status()
            return response.text


def _cache_path(root: Path) -> Path:
    return root / CACHE_NAME


def _key(root: Path) -> str:
    """进程内缓存与刷新线程的键：同一个 root 无论相对路径还是软链接都落到同一项。"""
    return str(Path(root).resolve())


def _read_cache(root: Path) -> dict[str, Any] | None:
    table = read_json(_cache_path(root))
    return table if isinstance(table, dict) and isinstance(table.get("contracts"), dict) and table["contracts"] else None


def _write_cache(root: Path, table: dict[str, Any]) -> None:
    write_json_atomic(_cache_path(root), table)


def _table(root: Path | None) -> dict[str, Any] | None:
    """读路径（永不联网）：进程内缓存 → 缓存文件。fetchedDay 不是今天的表照用但标
    stale（后台线程会尽快换新）；两边都没有返回 None。"""
    if root is None:
        return None
    resolved = Path(root)
    key = _key(resolved)
    today = _today()
    with _lock:
        cached = _tables.get(key)
        if cached is not None:
            if cached.get("fetchedDay") == today:
                return cached
            # 进程跨过北京时间零点还没换新：能用的旧表标 stale 再用。
            if not cached.get("stale"):
                cached = {**cached, "stale": True}
                _tables[key] = cached
            return cached
        loaded = _read_cache(resolved)
        if loaded is None:
            return None
        if loaded.get("fetchedDay") != today:
            loaded = {**loaded, "stale": True}
        _tables[key] = loaded
        return loaded


# -- 后台刷新 -------------------------------------------------------------------------


def refresh(root: Path) -> bool:
    """联网抓一次、解析、原子写缓存文件并更新进程内缓存；成功 True。网络请求在锁外，
    只在更新内存缓存时短暂持锁。失败保留旧缓存并标 stale（只降级非当天的表）。"""
    resolved = Path(root)
    key = _key(resolved)
    try:
        as_of, contracts = parse_fees_page(_fetch_html())
    except Exception:  # noqa: BLE001 — 网络失败走缓存降级，不让后台线程死掉
        contracts, as_of = {}, None
    if not (contracts and as_of):
        today = _today()
        with _lock:
            cached = _tables.get(key)
            if cached is None:
                loaded = _read_cache(resolved)
                if loaded is not None:
                    cached = loaded
                    _tables[key] = cached
            if cached is not None and cached.get("fetchedDay") != today and not cached.get("stale"):
                _tables[key] = {**cached, "stale": True}
        return False
    fresh = {"asOf": as_of, "fetchedDay": _today(), "contracts": contracts, "stale": False}
    try:
        _write_cache(resolved, fresh)
    except OSError:
        pass
    with _lock:
        _tables[key] = fresh
    return True


def _needs_fetch(root: Path) -> bool:
    """当天（北京时间）还没有一份成功抓到的表（内存或缓存文件任一算数）。"""
    today = _today()
    with _lock:
        cached = _tables.get(_key(root))
    if cached is not None and cached.get("fetchedDay") == today and not cached.get("stale"):
        return False
    loaded = _read_cache(root)
    return not (loaded is not None and loaded.get("fetchedDay") == today
                and not loaded.get("stale"))


def _has_table(root: Path) -> bool:
    with _lock:
        if _key(root) in _tables:
            return True
    return _read_cache(root) is not None


def _refresher_loop(root: Path, stop: threading.Event) -> None:
    while True:
        if _needs_fetch(root):
            refresh(root)
        if stop.wait(REFRESH_INTERVAL_SECONDS if _has_table(root) else COLD_RETRY_SECONDS):
            break


def start_refresher(root: Path) -> None:
    """每个 root 起一次后台刷新线程（幂等）；启动即刷新一次，之后每 30 分钟补抓当天
    没成功的。``AI_TRADER_EXCHANGE_FEES_DISABLED`` 时不起线程（测试环境）。"""
    if not _network_allowed():
        return
    resolved = Path(root)
    key = _key(resolved)
    with _refresher_lock:
        entry = _refreshers.get(key)
        if entry is not None and entry[0].is_alive():
            return
        stop = threading.Event()
        thread = threading.Thread(target=_refresher_loop, args=(resolved, stop),
                                  name=f"exchange-fees-refresh:{resolved.name}", daemon=True)
        _refreshers[key] = (thread, stop)
        thread.start()


def stop_refresher(root: Path | None = None, wait: float = 0.0) -> None:
    """停掉刷新线程；不给 root 停全部。``wait`` > 0 时等正在抓取的线程结束（最多这么多秒，
    失租移交用：旧持有者释放租约后不再写缓存文件）；等不到记日志照常返回。线程是 daemon。"""
    with _refresher_lock:
        keys = list(_refreshers) if root is None else [
            key for key in _refreshers if key == _key(root)]
        entries = [_refreshers.pop(key) for key in keys]
    for _thread, stop in entries:
        stop.set()
    if wait > 0:
        deadline = time.monotonic() + wait
        for thread, _stop in entries:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                log.warning("exchange_fees_refresher_still_running thread=%s", thread.name)


# -- 对外入口 -----------------------------------------------------------------------


def _entry_for(contracts: dict[str, dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    key = symbol.strip().upper()
    if key in contracts:
        return contracts[key]
    # 合约不在表里（未上市/已摘牌）：同一交易所内同品种任一合约，品种费率相同；
    # 同字母品种在别的交易所费率不同，不跨所借用。没写交易所前缀的代码不知道属于
    # 哪家，才在全部表项里找。
    parts = key.split(".")
    exchange = parts[0] if len(parts) == 2 else None
    product = re.sub(r"\d+$", "", parts[-1]).casefold()
    if not product:
        return None
    matches = sorted(name for name in contracts
                     if re.sub(r"\d+$", "", name.split(".")[-1]).casefold() == product
                     and (exchange is None or name.split(".")[0] == exchange))
    return contracts[matches[0]] if matches else None


def exchange_fee_schedule(symbol: str, *, root: Path | None = None) -> dict[str, Any] | None:
    """`symbol` 的交易所标准费率表（与 reference_fee_schedule 同形，source=exchange）；
    openctp 拿不到（未配置根 / 抓取失败且无缓存）返回 None，调用方退回自己的兜底。"""
    table = _table(root)
    if table is None:
        return None
    entry = _entry_for(table["contracts"], symbol)
    if entry is None:
        return None
    return {
        "source": "exchange", "default": entry["rate"], "overrides": [],
        "reference": {"source": "交易所标准（openctp 汇总）", "asOf": table.get("asOf"),
                      "contract": entry["contract"], "stale": bool(table.get("stale"))},
    }

"""本站模拟盘撮合引擎（2026-10-03）：一个进程服务所有模拟账户。

背景：逐用户快期账号方案废弃（每个账号一个 ~300–500MB 的 TqSdk 进程撑不起
50–100 用户）。本引擎用一份 SQLite + 单线程 actor 管理全部账户，每个账户一份
``PaperBook``（paper_book.py，内核是 TqSdk 官方 SimTrade，纯计算不联网）。

* 所有 SimTrade 调用都发生在 actor 线程；HTTP 线程通过命令队列调用。
* 撮合用的行情来自独立 data-only TqApi（``TqQuoteSource``），与 sidecar 实盘
  会话隔离；离线测试用 ``RecordedQuoteSource`` 回放 JSONL。
* 持久化：每个账户一次操作（submit / cancel / fill / settlement）一个事务，
  命令关联行并入同一事务，commit 成功才清 pending、登记内存命令映射；落盘
  失败时该账户从最后已提交状态重建后再报错，未提交的内存订单不作为已确认
  事实（审查 527-1）。行情驱动的浮动盈亏不落盘，重启后由行情重算。
  增量落盘（2026-10-12 压测：每条命令重写整本快照，单户当日 8000 笔时每次写
  2.2 MB，全站吞吐被拖垮）：每个事务只写小体量当前状态（paper_account_heads：
  资金、持仓、活单、行情，覆盖写一行）+ 本次新增的成交行（paper_trades，按
  账户内序号追加）+ 终态委托行（paper_orders，带账本日）；整本快照
  paper_accounts.state_json 降为检查点，只在日结、正常停机与空闲定期时写，
  格式不变（旧版本回滚可直接读）。恢复 = 当前状态行 + 序号起点之后的成交行；
  检查点比当前状态新（旧库首次启动或回滚后旧版本写过）时先把检查点转成
  当前状态行（一次事务，失败则引擎不启动，原快照列不动）。
* 故障停机（审查 527-3）：actor 处理行情链路抛出不可恢复异常时整体停机——
  新命令立即返回 paper_engine_failed（不等超时），读接口报 unavailable，
  行情不再入队；不吞掉异常假装正常。
* 结算（2026-10-08 起，所有者决定与真实期货账户同口径；目标日与来源约束
  2026-10-11 收敛，#527）：每日按交易所正式结算价结算，且只认目标日的价——
  目标日 = 新账本日按日历对应的上一个有效交易日；更旧日期的价格不能提前
  推进结算。来源优先级：行情源主动查询的 query_symbol_settlement（带所属
  交易日，最可靠）＞ 新交易日交易时段内行情携带的 settlement/pre_settlement
  （即上一交易日结算价）；同日低优先级不得覆盖高优先级，反向到达的高优先级
  升级。收盘价兜底是 PR647 的已发布设计，所有者 2026-10-11 亲自选择改为只等
  交易所正式结算价：价格未公布就继续 pending（含夜盘首委托前）、暂停新委托，
  绝不拿 last_price 或旧报价补位。
  18:00 换日时结算价未公布的账户推迟结算（账本日保持 previous_day），每条
  行情与定时主动查询都会重试；期间新委托被拒（paper_settlement_pending），
  保证委托晚于结算入账、不被结算撤掉。
* 到期合约的最终结算日（#527，2026-10-15 所有者决定）= 最后交易日（合约资料到期
  时间 expire_datetime 所在日）：普通目标日恰是最后交易日时与日常结算同一口径（等
  该日正式价、整户推迟），按原 Book 到期语义强平。跨停机越过到期日（普通目标日已
  晚于最后交易日）时只挂起这一个合约——账户其余合约照常结算、照常下单（该合约
  已到期，它的委托本来就被拒）；行情源按日期补查（query_symbol_settlement 带
  start_dt=最后交易日、days=1），查到就按原到期语义强平；挂起满一个交易日、当天
  补查仍不到就按最后可得价格暂估强平，日结日志与成交标“暂估，待核”，之后查到
  正式价补差一笔进当日平仓盈亏并标“已核”。挂起记录在 paper_expiry_settlements，
  与账本同一事务推进状态，重复恢复不会重复强平或补差。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import queue
import re
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from socketserver import TCPServer
from typing import Any
from urllib.parse import unquote, urlparse

from .main import (
    CHINA_TIMEZONE,
    ActorCommand,
    ReadOnlyHandler,
    detach_quote,
    detach_trading_schedule,
    finite_number,
    number,
    tqkq_error_code,
    trading_day_from_calendar,
    trading_day_string,
    utc_now,
)
from .paper_book import (
    CLOSE_YESTERDAY_ONLY_EXCHANGES,
    CONTRACT_METADATA,
    FEE_METADATA,
    FORCE_ORDER_PREFIX,
    AttrView,
    PaperBook,
    fee_rate_leg,
    in_trading_session,
    quote_moment,
    resolve_contract_metadata,
    valid_contract_field,
)
from .paper_fill import IDEAL, FillModel
from .paper_sources import MarketSourceRelay, SourceAccount, parse_sources

PAPER_SERVICE = "ai-trader-paper-engine"
DEFAULT_PAPER_PORT = 19011
DEFAULT_PAPER_DB = "/data/paper.sqlite3"
# 行情读取的订阅租约：market_quote 读过的合约 10 分钟内保持在行情源订阅集里。
QUOTE_LEASE_SECONDS = 600.0
COMMAND_TIMEOUT_SECONDS = 4.0
# 结算价主动查询的间隔（2026-10-08）：18:00 换日后有持仓账户还没结算时，行情源
# 每 5 分钟 query_symbol_settlement 一次，交易所约 19:00 公布，公布即结算。
SETTLEMENT_QUERY_SECONDS = 300.0
# close 等 actor 退出的上限；超时说明 actor 卡住（审查 527-4），数据库与 flock
# 不再关闭，交给进程退出回收。
CLOSE_JOIN_SECONDS = 5.0
# 合约资料表（#527 B2）：每手保证金/手续费超过这么多个交易日没被实时行情或合约
# 服务确认过，就在下单结果与健康里标“旧值”；静态字段（乘数、到期等）不受影响。
CONTRACT_FEE_STALE_TRADING_DAYS = 3
# 到期合约不再写进资料表；已在表里的行到期满这么久才清理（到期结算还要读它）。
CONTRACT_EXPIRED_GRACE_SECONDS = 30 * 86400
# 检查点（整本快照）的定期合并：有新提交的账户距上次检查点满这么久，actor 每
# CHECKPOINT_SCAN_SECONDS 检查一次、一次只合并一个账户（写一次整本，耗时随当日
# 成交笔数增长，所以限频）。只为回滚时旧版本读到的快照足够新；恢复不依赖它。
CHECKPOINT_INTERVAL_SECONDS = 600.0
CHECKPOINT_SCAN_SECONDS = 30.0
# 合约名（2026-10-13 事故）：外部送来的合约名先在这里规范成交易所写法再进租约/下单。
# 合约资料表有就用表里的写法；本进程没有足够新的合约服务整表刷新时按交易所大小写规则：
# 上期/大商/能源/广期品种小写，郑商/中金大写。别的交易所、别的形状一律不认。
# 合约服务的整表刷新只在这么久之内算“完整目录”（#842 Codex P2）：表外名字直接答不存在。
# 每天 20:00 刷新一次，留一小时余量；更旧（刷新连续失败）或本进程还没刷新过（重启后
# 库里的表可能是几天前的）时，表外形状对的名字交给行情源订阅判断，新挂牌合约不被拒。
CONTRACT_CATALOG_FRESH_SECONDS = 25 * 3600.0
SYMBOL_PATTERN = re.compile(r"([A-Za-z]+)\.([A-Za-z]+)([0-9]+)")
LOWER_PRODUCT_EXCHANGES = frozenset({"SHFE", "DCE", "INE", "GFEX"})
UPPER_PRODUCT_EXCHANGES = frozenset({"CZCE", "CFFEX"})
# 行情源订阅时合约服务答"合约不存在"的合约：这么久之内不再订阅、读报价直接答
# symbol_not_found；过期后若仍有人要（持仓/挂单/新的读取）再试一次。
REJECTED_SYMBOL_SECONDS = 1800.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_accounts (
    id TEXT PRIMARY KEY,
    init_balance REAL NOT NULL,
    book_trading_day TEXT NOT NULL,
    state_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    checkpoint_of INTEGER
);
CREATE TABLE IF NOT EXISTS paper_commands (
    account_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    kind TEXT,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (account_id, command_id)
);
CREATE TABLE IF NOT EXISTS paper_orders (
    account_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    final_json TEXT NOT NULL,
    book_day TEXT,
    PRIMARY KEY (account_id, client_order_id)
);
CREATE TABLE IF NOT EXISTS paper_account_heads (
    account_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    trades_from_seq INTEGER NOT NULL,
    book_trading_day TEXT NOT NULL,
    head_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_trades (
    account_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    book_day TEXT NOT NULL,
    trading_day TEXT,
    trade_json TEXT NOT NULL,
    PRIMARY KEY (account_id, seq)
);
CREATE TABLE IF NOT EXISTS paper_trade_log (
    account_id TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    log_json TEXT NOT NULL,
    PRIMARY KEY (account_id, trading_day)
);
CREATE TABLE IF NOT EXISTS paper_contracts (
    symbol TEXT PRIMARY KEY,
    ins_class TEXT,
    product_id TEXT,
    volume_multiple REAL,
    price_tick REAL,
    expire_datetime REAL,
    delivery_year INTEGER,
    delivery_month INTEGER,
    margin REAL,
    commission REAL,
    static_source TEXT,
    static_updated_at TEXT,
    fee_source TEXT,
    fee_updated_at TEXT
);
CREATE TABLE IF NOT EXISTS paper_expiry_settlements (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    final_day TEXT NOT NULL,
    status TEXT NOT NULL,
    since_day TEXT NOT NULL,
    record_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol, final_day)
);
"""
# 挂起的到期结算（#527 最终结算日协议，2026-10-15 所有者决定）：跨停机越过到期日、
# 最后交易日结算价还没补查到的持仓，账户其余合约照常结算，只这一个合约挂起。
# paper_expiry_settlements 每户每合约一行，随账户事务提交（与账本同一事务）：
#   pending   持仓未平，等最后交易日的正式结算价（query_symbol_settlement 按日期补查）；
#   estimated 挂起满一个交易日、当天补查仍不到：按最后可得价格暂估强平，待核；
#   settled   按正式结算价强平（挂起后才查到）；
#   adjusted  暂估之后查到正式价：补差一笔进当日平仓盈亏/手续费，已核。
# 旧版本不认识这张表、照常开库：pending 的账户在旧版本里回到整户等待（旧行为），
# estimated 的补差等回到新版本再做。
EXPIRY_OPEN_STATUSES = frozenset({"pending", "estimated"})
EXPIRY_STATUSES = EXPIRY_OPEN_STATUSES | {"settled", "adjusted"}
EXPIRY_STATUS_LABELS = {
    "pending": "待最后交易日结算价",
    "estimated": "暂估，待核",
    "settled": "已按最后交易日结算价结算",
    "adjusted": "已核，已补差",
}


def _log_line(message: str) -> None:
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except Exception:
        pass


class VirtualClock:
    """可推进的时钟：回放行情把时间定在录制时刻，成交与判断全部确定。"""

    def __init__(self, start: datetime) -> None:
        self._moment = start.astimezone(CHINA_TIMEZONE)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._moment

    def advance(self, moment: datetime) -> None:
        with self._lock:
            if moment > self._moment:
                self._moment = moment


# 迁移时判不出类型的历史命令行（#527 D4）：只允许按原单只读查询，任何可能
# 产生副作用的请求（新下单、撤活单）都拒绝，行本身不再改写。
COMMAND_KIND_LEGACY = "legacy"


def infer_legacy_command_kind(account_id: str, command_id: str, request_json: str) -> str:
    """按已存的原始请求判定无 kind 历史命令行的类型（#527 D4）。

    两种请求在 `_command` 里的形状不同：提交是 HTTP 校验后的完整下单字段
    （commandId/clientOrderId/symbol…）加 `_account_id`；撤单入口的 request
    固定为空，落库后恰好是 `{"_account_id": 账户}`。只认这两种与本行
    account/command 自洽的形状，其他（坏 JSON、账户或 commandId 对不上、
    字段缺失）一律 legacy。生产只读统计复用这一个函数。"""
    try:
        request = json.loads(request_json)
    except (TypeError, ValueError):
        return COMMAND_KIND_LEGACY
    if not isinstance(request, dict) or request.get("_account_id") != account_id:
        return COMMAND_KIND_LEGACY
    if set(request) == {"_account_id"}:
        return "cancel"
    if (request.get("commandId") == command_id
            and isinstance(request.get("clientOrderId"), str) and request["clientOrderId"]
            and isinstance(request.get("symbol"), str) and request["symbol"]):
        return "submit"
    return COMMAND_KIND_LEGACY


def _expire_day(expire_ns: float, tz: tzinfo = CHINA_TIMEZONE) -> str:
    """合约到期时间（TqSdk expire_datetime，最后交易日收盘时刻）所在的引擎本地日期（国内为北京）=
    最后交易日 = 最终结算日（#527，2026-10-15 所有者决定）。"""
    return datetime.fromtimestamp(expire_ns / 1e9, tz=UTC).astimezone(tz).date().isoformat()


def _weekend_calendar(now: datetime, tz: tzinfo = CHINA_TIMEZONE) -> set[str]:
    """没有交易日历时的兜底：前后 30 天的周一到周五。节假日识别不了，
    交易时段判断由合约时段 + 无行情自然兜底。"""
    base = now.astimezone(tz).date()
    days = [(base + timedelta(days=offset)) for offset in range(-30, 31)]
    return {day.isoformat() for day in days if day.weekday() < 5}


class UnknownSymbolError(LookupError):
    """合约名不是一个存在的合约（形状不对、交易所不认、资料表里没有、或合约服务
    答过不存在）：HTTP 404 symbol_not_found，与只读 sidecar 同一个错误码。不进租约、不订阅。"""


def rule_symbol(symbol: str) -> str | None:
    """按交易所大小写规则写出的合约名（交易所大写；上期/大商/能源/广期品种小写，
    郑商/中金品种大写）；形状不对或不是这六个交易所时为 None。"""
    match = SYMBOL_PATTERN.fullmatch(str(symbol or "").strip())
    if match is None:
        return None
    exchange, product, digits = match.group(1).upper(), match.group(2), match.group(3)
    if exchange in LOWER_PRODUCT_EXCHANGES:
        return f"{exchange}.{product.lower()}{digits}"
    if exchange in UPPER_PRODUCT_EXCHANGES:
        return f"{exchange}.{product.upper()}{digits}"
    return None


_NONEXISTENT_NAMES = re.compile(r"non-existent instruments?\s*:?\s*([^\]\n]+)")
_NONEXISTENT_CODES = re.compile(r"代码\s*(\[[^\]]*\]|\S+)\s*不存在")
_SYMBOL_TOKEN = re.compile(r"[A-Za-z]+\.[A-Za-z0-9]+")


def nonexistent_instruments(error: BaseException) -> list[str]:
    """合约服务答"合约不存在"时报错里点名的合约（TqSdk 3.10 symbols.py 的
    `查询合约服务报错 ... contains non-existent instrument: X`，或 api 的
    `代码 [...] 不存在`）；其他错误（断线、认证、超时）为空。这类错误只说明一个
    合约名是坏的，不是行情源故障。"""
    message = str(error)
    found: list[str] = []
    for pattern in (_NONEXISTENT_NAMES, _NONEXISTENT_CODES):
        for chunk in pattern.findall(message):
            for name in _SYMBOL_TOKEN.findall(chunk):
                if name not in found:
                    found.append(name)
    return found


class PaperMigrationError(RuntimeError):
    """启动迁移失败（检查点 → 当前状态行）：引擎不启动，原快照列未被改写。"""


def _checkpoint_newer(checkpoint_revision: Any, checkpoint_of: Any, has_head: bool) -> bool:
    """检查点（paper_accounts.state_json）是否比当前状态行（paper_account_heads）新。
    本版本写检查点（开户、日结、停机、定期、迁移）时把 checkpoint_of 置为同一个
    revision；旧版本不认识这一列，它开户留空、每次落盘 revision+1 而不动这一列。
    所以：没有当前状态行、checkpoint_of 为空或与 revision 不等 = 旧库，或回滚后
    旧版本写过这个账户——以检查点为准。不比时间戳（时钟回拨不影响判断）。"""
    return (not has_head or checkpoint_of is None
            or int(checkpoint_revision or 0) != int(checkpoint_of))


def checkpoint_lag(db: sqlite3.Connection) -> list[str]:
    """检查点没追平的账户（回滚前与停机后的同一个判据）：每户须 revision = 当前状态行
    revision = checkpoint_of，旧版本读 state_json 才是最后已提交状态。没有当前状态行的
    账户（迁移没跑）同样算没追平。"""
    return [str(account_id) for (account_id,) in db.execute(
        "SELECT a.id FROM paper_accounts a LEFT JOIN paper_account_heads h ON h.account_id = a.id "
        "WHERE h.revision IS NULL OR a.revision != h.revision "
        "OR a.checkpoint_of IS NULL OR a.checkpoint_of != a.revision ORDER BY a.id").fetchall()]


@dataclass
class _PaperAccount:
    """actor 线程内的一份账户状态；archived_orders 是当日/上一交易日已结束委托。"""

    book: PaperBook
    archived_orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    settlements: int = 0
    # 本轮命令/行情处理中新增的已结束委托，随下一次事务一起写盘。
    pending_orders: list[dict[str, Any]] = field(default_factory=list)
    # 已提交的落盘版本（每个账户事务 +1）与最近检查点对应的版本。
    revision: int = 0
    checkpoint_revision: int = 0
    checkpoint_at: float = 0.0
    # 当日成交行：book._sim._trades[i] 存在 paper_trades 的 seq = trades_from_seq + i；
    # 前 persisted_trades 笔已提交。只在提交成功后更新。
    trades_from_seq: int = 0
    persisted_trades: int = 0
    # 挂起中的到期结算（pending / estimated）：合约 → paper_expiry_settlements 的记录。
    # 只在提交成功后更新。
    expiry: dict[str, dict[str, Any]] = field(default_factory=dict)


class PaperEngine:
    """多账户单线程 actor。所有 SimTrade 调用、SQLite 写入都在 actor 线程；
    读接口与 create_account 只拿状态锁做快照。"""

    def __init__(
        self, *, db_path: str,
        clock: Callable[[], datetime] | None = None,
        trading_dates: Callable[[], set[str]] | set[str] | None = None,
        stale_after_seconds: float = 15.0,
        quote_wait_seconds: float = 5.0,
        fill_model: FillModel | None = None,
        timezone: tzinfo = CHINA_TIMEZONE,
    ) -> None:
        self.db_path = str(db_path)
        # 交易日按引擎本地时间 18:00 换日（trading_day_from_dates）：国内引擎是北京时间；外盘
        # 引擎（paper_foreign.py，2026-10-19）是美东时间，正好是 CME Globex 的交易日口径。
        self._tz = timezone
        # 成交模型（2026-10-13，paper_fill.py）：全引擎一份，每个账本共用。不传 = 理想
        # 模式（SimTrade 原行为，测试与工具的历史口径）；生产入口 serve() 按环境变量
        # 构造，默认真实模式。
        self.fill_model = fill_model
        # 有委托还没到达撮合的账户：行情静止时按缓存盘口兜底到达（_sweep_arrivals）。
        self._arrival_accounts: set[str] = set()
        self._clock = clock or (lambda: datetime.now(timezone))
        self._trading_dates = trading_dates
        self.stale_after_seconds = float(stale_after_seconds)
        self.quote_wait_seconds = float(quote_wait_seconds)
        self._state_lock = threading.RLock()
        self._drain_cond = threading.Condition()
        self._posted = 0
        self._processed = 0
        self._quote_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._commands: queue.Queue[ActorCommand] = queue.Queue()
        self._accounts: dict[str, _PaperAccount] = {}
        # commandId → (kind, client_order_id)（审查 527-8）：同一 commandId 绑定
        # 它首次的命令类型，不跨 submit/cancel 复用。
        self._command_orders: dict[tuple[str, str], tuple[str, str]] = {}
        self._quotes: dict[str, dict[str, Any]] = {}
        # 合约 → 重启前账本已知的最新行情时间（#527 B2e）：_load 从各账本快照
        # 里的行情取最大值，只作防倒退水位，不进 _quotes——历史快照不是实时
        # 行情，不能让健康状态、ordersInitialized 或行情读接口把它当最新价。
        self._quote_watermarks: dict[str, datetime] = {}
        # 合约资料表 paper_contracts 的内存镜像（#527 B2）：合约 → 字段 + 来源/更新
        # 时间。actor 线程写（实时行情回写、批量刷新），读接口在 _state_lock 下读。
        # 行情缓存存的是按资料表补齐后的行情，_quote_fills 记哪些字段是资料表补的。
        self._contracts: dict[str, dict[str, Any]] = {}
        # 资料表的大小写不敏感索引（大写 → 表里写法）；合约服务最近一次整表刷新列出的
        # 全部合约名（含资料不全、没进表的）与刷新时刻：足够新时它就是全部未到期期货，
        # 表外的名字是不存在的合约（_symbol_spelling，#842）。只认本进程收到的刷新。
        self._contract_index: dict[str, str] = {}
        self._catalog_names: dict[str, str] = {}
        self._catalog_at: datetime | None = None
        self._contract_miss = False
        # 行情源订阅时合约服务答"不存在"的合约 → 时间（REJECTED_SYMBOL_SECONDS 内不订阅）。
        self._rejected_symbols: dict[str, datetime] = {}
        self._quote_fills: dict[str, set[str]] = {}
        self._contract_lock = threading.Lock()
        self._contract_inbox: list[tuple[list[str], list[dict[str, Any]]]] = []
        self._contract_refreshed_at: str | None = None
        self._contract_refresh_error: str | None = None
        # 结算价按合约记 (所属交易日, 来源, 价格)：来源 "query"（主动查询，最
        # 可靠）或 "quote"（交易时段内行情的 settlement/pre_settlement，归属上一
        # 交易日）。结算只精确匹配目标日的记录（#527）：更旧日期不提前推进、
        # 跨日残留不复用（审查 #465 第 5 条）、同日低优先级不覆盖高优先级。
        self._settlement_records: dict[str, tuple[str, str, float]] = {}
        # 主动查询到的结算价先落这里，actor 线程合并进 _settlement_records；
        # 一次 feed_settlements 是一个批次，drain 计数按批结对（两次 feed 之间
        # 没被排空时 _posted/_processed 也不会错位，等 drain 的调用方不挂起）。
        self._settlement_lock = threading.Lock()
        self._settlement_inbox: list[tuple[list[dict[str, Any]], dict[str, str]]] = []
        # (账户, 交易日) → 已日志过的推迟/兜底事件，避免每条行情刷屏。
        self._settlement_logs: set[tuple[str, str]] = set()
        # 到期合约的最后交易日结算价（#527 最终结算日协议）：合约 → (最后交易日, 来源,
        # 价格)。主动查询按日期补查的结果里，日期恰是该合约最后交易日的才记；普通
        # _settlement_records 只留每合约最新一天，跨停机后可能已被更新的日期覆盖。
        self._final_records: dict[str, tuple[str, str, float]] = {}
        # 合约 → 最近一次按日期补查最后交易日结算价（无论查到与否）时的交易日：暂估
        # 只在当天确实又补查过一次之后才做（重启后的第一次结算不抢在补查之前暂估）。
        self._final_checked: dict[str, str] = {}
        # 有挂起到期结算的账户（只为每条行情不必遍历全部账户）。
        self._expiry_accounts: set[str] = set()
        # 落盘失败且重建也失败的账户（#527 任务二）：内存停在未提交状态且回不到
        # 一致，读入口经 _require_account（锁内共用检查）一律拒绝，不得把它的
        # 内存当已确认事实。
        self._uncertain_accounts: set[str] = set()
        self._force_sequence = 0  # 强平委托号序号（#1204），与时间戳一起保证同一秒内不重号
        self._leases: dict[str, datetime] = {}
        # 合约 → 关心它的账户（有持仓或有挂单）：行情只分发给相关账户（审查
        # #465 第 1 条）。索引只增不减（撤仓后残留的多推一次行情无害），结算后重建。
        self._symbol_accounts: dict[str, set[str]] = {}
        self._engine_day: str | None = None
        self._source_connected_at: datetime | None = None
        self._source_reconnecting_at: datetime | None = None
        self._last_quote_moment: datetime | None = None
        self._opened = False
        self._stop = threading.Event()
        # actor 线程已退出、不会再执行命令（_run 收尾时置位，先于排空队列）：之后入队
        # 的命令由 _command 自己排空答复，不必等超时。
        self._actor_exited = threading.Event()
        # actor 故障停机原因（527-3）：None 表示健康；置位后命令立即失败、
        # 读接口 unavailable、行情不再入队。
        self._fatal: str | None = None
        self._thread: threading.Thread | None = None
        self._db: sqlite3.Connection | None = None
        # 读连接池（WAL 读快照）：activity 的当日行在撮合锁外按索引读。
        self._readers: queue.SimpleQueue[sqlite3.Connection] = queue.SimpleQueue()
        self._lock_fd: int | None = None
        self.checkpoint_interval_seconds = CHECKPOINT_INTERVAL_SECONDS
        self._checkpoint_scan_at = 0.0

    # -- 生命周期 ---------------------------------------------------------------

    def open(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        lock_fd = os.open(f"{self.db_path}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(lock_fd)
            raise RuntimeError("paper_engine_lock_held") from error
        self._lock_fd = lock_fd
        try:
            self._db = sqlite3.connect(self.db_path, check_same_thread=False)
            # WAL：activity 在撮合锁外读当日行时，读事务不挡 actor 提交（2026-10-12）。
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._migrate_commands_kind()
            self._migrate_incremental()
            self._load()
            self._load_contracts()
        except BaseException:
            # 迁移或恢复失败（fail-closed）：引擎不启动，连接与文件锁当场释放。
            if self._db is not None:
                self._db.close()
                self._db = None
            os.close(lock_fd)
            self._lock_fd = None
            raise
        self._opened = True

    def _migrate_commands_kind(self) -> None:
        """增量迁移（审查 527-8 / #527 D4）：老库的 paper_commands 补 kind 列，
        再按已存 request_json 一次性回填空 kind（submit / cancel / legacy）。
        回填只写 kind，不改写原始 request_json；之后不再有空 kind 行，重放
        不能由当前请求认领历史行的类型。只动空 kind 行，重复启动无变化。"""
        assert self._db is not None
        with self._db:
            columns = {str(row[1]) for row in self._db.execute(
                "PRAGMA table_info(paper_commands)")}
            if "kind" not in columns:
                self._db.execute("ALTER TABLE paper_commands ADD COLUMN kind TEXT")
            rows = self._db.execute(
                "SELECT account_id, command_id, request_json FROM paper_commands "
                "WHERE kind IS NULL OR kind=''").fetchall()
            for account_id, command_id, request_json in rows:
                self._db.execute(
                    "UPDATE paper_commands SET kind=? WHERE account_id=? AND command_id=?",
                    (infer_legacy_command_kind(str(account_id), str(command_id), request_json),
                     account_id, command_id))

    def _migrate_incremental(self) -> None:
        """增量落盘迁移（2026-10-12）：检查点比当前状态行新的账户（旧库首次启动、
        或回滚后旧版本又写过快照），把整本快照拆成当前状态行 + 当日成交行；
        paper_orders 补 book_day 列并按 _bookDay 回填。全部账户一个事务：任何
        一户解析失败整体回滚、抛 PaperMigrationError（引擎不启动），原快照列
        state_json 始终不改写。已迁移的库再启动没有变化（幂等）。"""
        assert self._db is not None
        current = "schema"
        try:
            columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(paper_orders)")}
            if "book_day" not in columns:
                self._db.execute("ALTER TABLE paper_orders ADD COLUMN book_day TEXT")
            columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(paper_accounts)")}
            if "checkpoint_of" not in columns:
                self._db.execute("ALTER TABLE paper_accounts ADD COLUMN checkpoint_of INTEGER")
            self._db.execute("CREATE INDEX IF NOT EXISTS paper_orders_by_day "
                             "ON paper_orders (account_id, book_day)")
            with self._db:
                for account_id, client_order_id, final_json in self._db.execute(
                        "SELECT account_id, client_order_id, final_json FROM paper_orders "
                        "WHERE book_day IS NULL").fetchall():
                    current = f"order {account_id}/{client_order_id}"
                    record = json.loads(final_json)
                    self._db.execute(
                        "UPDATE paper_orders SET book_day=? WHERE account_id=? AND client_order_id=?",
                        (str(record.get("_bookDay") or ""), account_id, client_order_id))
                heads = {str(row[0]) for row in self._db.execute(
                    "SELECT account_id FROM paper_account_heads")}
                for account_id, revision, checkpoint_of, updated_at in self._db.execute(
                        "SELECT id, revision, checkpoint_of, updated_at FROM paper_accounts").fetchall():
                    if not _checkpoint_newer(revision, checkpoint_of, str(account_id) in heads):
                        continue
                    current = f"account {account_id}"
                    (state_json,) = self._db.execute(
                        "SELECT state_json FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
                    state = json.loads(state_json)
                    if not isinstance(state, dict):
                        raise ValueError("snapshot is not an object")
                    missing = [key for key in ("_account", "_positions", "_orders", "_trades",
                                               "quotes", "book_trading_day") if key not in state]
                    if missing:
                        raise ValueError(f"snapshot missing {', '.join(missing)}")
                    trades = state.pop("_trades")
                    if not isinstance(trades, list) or not all(isinstance(t, dict) for t in trades):
                        raise ValueError("snapshot _trades is not a list of trades")
                    book_day = str(state["book_trading_day"])
                    self._db.execute("DELETE FROM paper_trades WHERE account_id=?", (account_id,))
                    self._db.executemany(
                        "INSERT INTO paper_trades (account_id, seq, book_day, trading_day, trade_json) "
                        "VALUES (?,?,?,?,?)",
                        [(account_id, seq, book_day, self._stored_trade_day(trade), json.dumps(trade))
                         for seq, trade in enumerate(trades)])
                    self._db.execute(
                        "INSERT OR REPLACE INTO paper_account_heads "
                        "(account_id, revision, trades_from_seq, book_trading_day, head_json, updated_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (account_id, int(revision or 0), 0, book_day, json.dumps(state),
                         str(updated_at or "")))
                    # 只登记“检查点 = 这一版”，state_json 本身不改写。
                    self._db.execute("UPDATE paper_accounts SET checkpoint_of=revision WHERE id=?",
                                     (account_id,))
        except Exception as error:
            raise PaperMigrationError(
                f"paper_migration_failed: {current}: {type(error).__name__}: {str(error)[:200]}"
            ) from error

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="paper-engine-actor", daemon=True)
        self._thread.start()

    def close(self, *, join_seconds: float = CLOSE_JOIN_SECONDS) -> bool:
        """停机：停止接纳命令、等 actor 退出、合并检查点。返回检查点是否追平（每户
        revision = 当前状态行 = checkpoint_of）；False 时旧版本读到的快照可能落后，
        serve() 据此非 0 退出，回滚前须先 --checkpoint-db（#527/#510）。"""
        self._stop.set()
        # 停止接纳的同一刻答复队列里的命令（#527 waiter，#913 复核）：actor 看到停机后
        # 不再领取，留在队列里的都确定没执行，不必等正在执行的慢命令结束或等到命令超时。
        # 已被 actor 领取的照常按实际结果答复；队列线程安全，每条只被一方取走、答一次。
        self._fail_pending_commands()
        if self._thread:
            self._thread.join(timeout=join_seconds)
            if self._thread.is_alive():
                # join 超时：旧 actor 可能还在写库（审查 527-4）。数据库连接与
                # flock 不关闭，交给进程退出回收——关掉句柄会让还活着的 actor
                # 写进已关闭的连接。检查点也就没合并：报没追平。
                _log_line("paper engine close: actor still alive; db and lock left open, "
                          "checkpoints not merged")
                return False
        caught_up = True
        if self._db is not None:
            # 正常停机把检查点合并到最新已提交状态：回滚到旧版本时它读到的快照
            # 就是停机那一刻（读的是库里已提交行，不看内存，故障停机也安全）。
            caught_up = self._checkpoint_all()
            self._db.close()
            self._db = None
        while True:
            try:
                self._readers.get_nowait().close()
            except queue.Empty:
                break
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        return caught_up

    def _load(self) -> None:
        assert self._db is not None
        for (account_id,) in self._db.execute("SELECT id FROM paper_accounts").fetchall():
            self._accounts[str(account_id)] = self._restore_entry(str(account_id))
        for account_id in self._accounts:
            self._index_rebuild(account_id)
            if self._accounts[account_id].book.pending_arrival():
                self._arrival_accounts.add(account_id)
            if self._accounts[account_id].expiry:
                self._expiry_accounts.add(account_id)
        self._seed_quote_watermarks()
        for account_id, command_id, client_order_id, kind in self._db.execute(
            "SELECT account_id, command_id, client_order_id, kind FROM paper_commands"
        ).fetchall():
            # 值是 (kind, client_order_id)；open 先跑迁移，这里 kind 都已回填。
            self._command_orders[(str(account_id), str(command_id))] = (
                str(kind), str(client_order_id))

    def _seed_quote_watermarks(self) -> None:
        """重启后的防倒退水位（#527 B2e）：每个合约取所有账本快照里已知行情时间
        的最大值。进程内缓存 _quotes 重启后是空的，没有它第一条迟到的旧行情会
        把恢复的账本价格拉回去并驱动撮合。"""
        for entry in self._accounts.values():
            for symbol in entry.book.quote_symbols():
                quote = entry.book.latest_quote(symbol)
                moment = quote_moment(quote) if quote is not None else None
                if moment is None:
                    continue
                known = self._quote_watermarks.get(symbol)
                if known is None or moment > known:
                    self._quote_watermarks[symbol] = moment

    def _archive_keep_days(self) -> set[str] | None:
        """内存归档只留当前与上一交易日（审查 #465 第 6 条）：更早的委托与成交
        留在 paper_orders / paper_trade_log 表里，query_order 落库回读。真实
        日历未就绪时没有日期概念，先全量读入，日历就绪后的第一次结算再裁剪。"""
        if not self._calendar_ready():
            return None
        try:
            today = trading_day_from_calendar(self._clock(), self._calendar(), self._tz)
            return {today, self._previous_trading_date(today)}
        except RuntimeError:
            return None

    @staticmethod
    def _committed_state(db: sqlite3.Connection, account_id: str) -> tuple[dict[str, Any], int, int]:
        """库里一个账户最后已提交的整本账：当前状态行 + 序号起点之后的成交行。
        返回 (state, revision, trades_from_seq)。恢复与检查点合并共用这一处。
        当前状态行缺失或比检查点旧说明迁移没跑（不应发生），拒绝而不是读旧值。"""
        row = db.execute(
            "SELECT a.revision, a.checkpoint_of, h.revision, h.trades_from_seq, h.head_json "
            "FROM paper_accounts a LEFT JOIN paper_account_heads h ON h.account_id = a.id "
            "WHERE a.id=?", (account_id,)).fetchone()
        if row is None:
            raise KeyError(account_id)
        cp_revision, checkpoint_of, revision, from_seq, head_json = row
        if _checkpoint_newer(cp_revision, checkpoint_of, head_json is not None):
            raise RuntimeError("paper_state_unmigrated")
        state = json.loads(head_json)
        state["_trades"] = [json.loads(trade_json) for (trade_json,) in db.execute(
            "SELECT trade_json FROM paper_trades WHERE account_id=? AND seq>=? ORDER BY seq",
            (account_id, int(from_seq)))]
        return state, int(revision), int(from_seq)

    def _restore_entry(self, account_id: str) -> _PaperAccount:
        """从库里的最后已提交状态构建一份账户内存态：_load 启动加载与落盘失败
        后的重建（527-1）共用这一条路径；恢复后照旧跑账本自检（#527 F3/#777）。"""
        assert self._db is not None
        (init_balance, cp_revision) = self._db.execute(
            "SELECT init_balance, revision FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        state, revision, from_seq = self._committed_state(self._db, account_id)
        book = PaperBook(
            account_id=account_id, init_balance=float(init_balance),
            clock=self._clock, trading_dates=self._calendar, fill_model=self.fill_model,
            timezone=self._tz,
        )
        book.restore_state(state, trades_from_seq=from_seq)
        if not book.sanity():
            raise RuntimeError("paper_state_corrupt")
        if book.lots_dropped:
            # 回滚后的旧版本写过这个账户（#932 B2）：明细按桶成本重建，记一笔原因。
            _log_line(f"paper lots rebuilt from bucket cost ({account_id}): "
                      "last writer did not keep FIFO lots")
        entry = _PaperAccount(
            book=book, revision=revision, checkpoint_revision=int(cp_revision or 0),
            checkpoint_at=time.monotonic(), trades_from_seq=from_seq,
            persisted_trades=len(state["_trades"]))
        keep = self._archive_keep_days()
        query = "SELECT client_order_id, final_json FROM paper_orders WHERE account_id=?"
        params: list[Any] = [account_id]
        if keep is not None:
            # 内存只留当前与上一交易日（审查 #465 第 6 条）；没有账本日的旧行照收。
            query += f" AND (book_day IN ({', '.join('?' for _ in keep)}) OR book_day='')"
            params.extend(sorted(keep))
        for client_order_id, final_json in self._db.execute(query, params).fetchall():
            entry.archived_orders[str(client_order_id)] = json.loads(final_json)
        total = self._db.execute(
            "SELECT count(*) FROM paper_trade_log WHERE account_id=?", (account_id,)
        ).fetchone()
        entry.settlements = int(total[0]) if total else 0
        entry.expiry = self._open_expiry_rows(self._db, account_id)
        return entry

    @staticmethod
    def _open_expiry_rows(db: sqlite3.Connection, account_id: str) -> dict[str, dict[str, Any]]:
        """一个账户挂起中的到期结算（pending / estimated）。记录解析不了或状态不认识就
        拒绝恢复（fail-closed，与账本自检同一处），不把半份记录当成已结或未结。"""
        rows: dict[str, dict[str, Any]] = {}
        for symbol, final_day, status, since_day, record_json in db.execute(
                "SELECT symbol, final_day, status, since_day, record_json "
                "FROM paper_expiry_settlements WHERE account_id=?", (account_id,)).fetchall():
            if status not in EXPIRY_STATUSES:
                raise RuntimeError("paper_expiry_record_corrupt")
            if status not in EXPIRY_OPEN_STATUSES:
                continue
            record = json.loads(record_json)
            if (not isinstance(record, dict) or record.get("status") != status
                    or record.get("finalDay") != final_day or record.get("since") != since_day):
                raise RuntimeError("paper_expiry_record_corrupt")
            rows[str(symbol)] = record
        return rows

    def _reload_account(self, account_id: str) -> None:
        """把一个账户的内存态（Book、归档、成交、索引）重建为 SQLite 里最后已
        提交的状态；落盘失败后由持久化路径调用（527-1），未提交的内存订单从此
        不再被查询/重试当作已确认事实。"""
        assert self._db is not None
        exists = self._db.execute(
            "SELECT 1 FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        if exists is None:
            self._accounts.pop(account_id, None)
            self._expiry_accounts.discard(account_id)
            self._index_rebuild(account_id)
            return
        self._accounts[account_id] = self._restore_entry(account_id)
        if self._accounts[account_id].expiry:
            self._expiry_accounts.add(account_id)
        else:
            self._expiry_accounts.discard(account_id)
        self._index_rebuild(account_id)
        if self._accounts[account_id].book.pending_arrival():
            self._arrival_accounts.add(account_id)

    def _reload_account_after_persist_failure(self, account_id: str) -> None:
        """落盘失败后的兜底重建；重建自身再失败时把引擎标记故障停机，并在
        _state_lock 下把该账户记为状态不确定（#527 任务二）：内存与已提交账本
        不一致且回不到一致状态，读入口经 _require_account 的共用检查一律拒绝
        （HTTP 503），命令/幂等重试立即失败，隔离给进程重启回到最后已提交状态。
        一般故障停机（527-3）不隔离读，账户读仍按 unavailable 报状态、数据可查
        （527-6 停车用例的合同）；重建成功的正控路径不受影响。"""
        try:
            self._reload_account(account_id)
        except Exception as error:
            with self._state_lock:
                self._uncertain_accounts.add(account_id)
            self._mark_fatal(error)

    # -- 时间与日历 ---------------------------------------------------------------

    def _calendar(self) -> set[str]:
        if callable(self._trading_dates):
            return set(self._trading_dates())
        if self._trading_dates:
            return set(self._trading_dates)
        return _weekend_calendar(self._clock(), self._tz)

    def _calendar_ready(self) -> bool:
        """真实交易日历是否已加载。周末兜底规则不是真实日历：在它生效期间既不
        触发跨日结算，也不用兜底日期给新账户定账本日（审查 #465 第 2 条）。"""
        if self._trading_dates is None:
            return False
        return bool(self._trading_dates()) if callable(self._trading_dates) else bool(self._trading_dates)

    def _previous_trading_date(self, trading_day: str) -> str:
        dates = sorted(self._calendar())
        earlier = [value for value in dates if value < trading_day]
        return earlier[-1] if earlier else trading_day

    # -- 合约→账户索引 ------------------------------------------------------------

    def _index_add(self, account_id: str, symbol: str) -> None:
        if symbol:
            self._symbol_accounts.setdefault(symbol, set()).add(account_id)

    def _index_rebuild(self, account_id: str) -> None:
        for accounts in self._symbol_accounts.values():
            accounts.discard(account_id)
        entry = self._accounts.get(account_id)
        if entry is not None:
            for symbol in entry.book.needed_symbols():
                self._symbol_accounts.setdefault(symbol, set()).add(account_id)

    # -- 行情源状态 -----------------------------------------------------------------

    def mark_source_connected(self) -> None:
        with self._state_lock:
            self._source_connected_at = self._clock()
            self._source_reconnecting_at = None

    def mark_source_reconnecting(self) -> None:
        with self._state_lock:
            if self._source_reconnecting_at is None:
                self._source_reconnecting_at = self._clock()

    def _note_source_activity(self) -> None:
        """收到任何一笔行情即视为行情源在线；正在重连时同时结束重连窗口。"""
        if self._source_connected_at is None:
            self._source_connected_at = self._clock()
        self._source_reconnecting_at = None

    def _source_state(self) -> str:
        if self._failed():
            return "unavailable"
        if self._source_reconnecting_at is not None:
            if (self._clock() - self._source_reconnecting_at).total_seconds() > 60:
                return "unavailable"
            return "normal"
        return "normal" if self._source_connected_at is not None else "unavailable"

    def _current_day(self, entry: _PaperAccount | None = None) -> str:
        try:
            return trading_day_from_calendar(self._clock(), self._calendar(), self._tz)
        except RuntimeError:
            if entry is not None:
                return entry.book.book_trading_day
            days = [item.book.book_trading_day for item in self._accounts.values()]
            return max(days) if days else ""

    # -- 行情 --------------------------------------------------------------------

    def feed_quote(self, quote: dict[str, Any]) -> None:
        if self._failed():
            return  # 引擎已故障停机：行情丢弃，队列不再增长（527-3）。
        with self._drain_cond:
            self._posted += 1
        self._quote_queue.put(dict(quote))

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._drain_cond:
            while self._processed < self._posted:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._drain_cond.wait(remaining)
            return True

    def _subscribed(self, symbol: str) -> bool:
        """这个合约此刻在行情源的订阅集里（持仓/挂单需要，或租约未过期）；调用方持状态锁。"""
        leased = self._leases.get(symbol)
        if leased is not None and (self._clock() - leased).total_seconds() < QUOTE_LEASE_SECONDS:
            return True
        return any(symbol in entry.book.needed_symbols() for entry in self._accounts.values())

    def _quote_age(self, quote: dict[str, Any]) -> float:
        """一笔缓存行情的年龄（秒）：行情时间到现在；没有时间戳的按无穷旧。"""
        moment = quote_moment(quote)
        return math.inf if moment is None else max(0.0, (self._clock() - moment).total_seconds())

    def wanted_symbols(self) -> set[str]:
        """行情源应订阅的合约：有持仓/挂单的 + 最近被读行情的。"""
        now = self._clock()
        with self._state_lock:
            wanted: set[str] = set()
            for entry in self._accounts.values():
                wanted |= entry.book.needed_symbols()
            wanted |= {
                symbol for symbol, at in self._leases.items()
                if (now - at).total_seconds() < QUOTE_LEASE_SECONDS
            }
            # 合约服务答过不存在的合约不进订阅集（持仓/挂单里的也一样，过期后再试）。
            return {symbol for symbol in wanted if symbol and not self._rejected(symbol, now)}

    def _stale_quote(self, symbol: str, moment: datetime | None) -> bool:
        """乱序迟到的旧行情（审查 527-12）：同一合约比缓存里最新一笔还旧，或比
        重启前账本已知的最新行情还旧（#527 B2e 水位）。任一笔没有可解析时间
        （quote_moment 为 None）的没有新旧可言，照旧处理。"""
        if not symbol or moment is None:
            return False
        watermark = self._quote_watermarks.get(symbol)
        if watermark is not None and moment < watermark:
            return True
        cached = self._quotes.get(symbol)
        if cached is None:
            return False
        cached_moment = quote_moment(cached)
        return cached_moment is not None and moment < cached_moment

    def _apply_quote(self, quote: dict[str, Any]) -> None:
        symbol = str(quote.get("symbol") or "")
        moment = quote_moment(quote)
        if moment is not None and isinstance(self._clock, VirtualClock):
            self._clock.advance(moment)
        with self._state_lock:
            self._note_source_activity()
            # 迟到的旧行情整笔丢弃（审查 527-12）：缓存不倒退、结算价不回写、
            # 也不入账本驱动撮合。
            in_session = False
            if not self._stale_quote(symbol, moment):
                if symbol:
                    # 合约资料唯一取值处（#527 B2）：实时有效值回写资料表，缺的字段
                    # 由资料表补；缓存、账本、行情读接口看到的都是补齐后的行情。
                    self._remember_contract(quote)
                    quote, filled = self._with_contract(quote)
                    self._quotes[symbol] = quote
                    self._quote_fills[symbol] = filled
                    if moment is not None:
                        self._last_quote_moment = moment
                    # 交易时段内的行情才记录结算价（settlement/pre_settlement 在
                    # TqSdk 语义里是上一交易日的结算价）；18:00–夜盘开盘之间这段
                    # 死区里 settlement 字段还是更早一天的值，记了就会张冠李戴。
                    in_session = self._record_settlement_fields(symbol, quote, moment)
                    if self._expiry_accounts:
                        self._track_expiry_basis(symbol, quote)
                # 行情只推给有持仓或有挂单的账户（合约→账户索引），其余账户的账本
                # 不需要它；下单前再把引擎缓存的最新行情注入目标账本（审查 #465 第 1 条）。
                related = [account_id for account_id in self._symbol_accounts.get(symbol, ())]  # noqa: SIM118
                for account_id in related:
                    # 先入账最新价（不撮合）：随后结算里的到期判断要读它。
                    self._accounts[account_id].book.inject_quote(quote)
                self._advance_engine_day()
                for account_id in related:
                    entry = self._accounts[account_id]
                    # 新交易日的撮合开始前必须先完成结算（2026-10-08）：还欠结算的
                    # 账户收交易时段行情只更新价格，不撮合。
                    if (in_session and entry.book.book_trading_day
                            and entry.book.book_trading_day < str(self._engine_day)):
                        self._log_settlement_event(account_id, str(self._engine_day),
                                                   "paper settlement deferred: no price yet")
                        continue
                    for final in entry.book.apply_quote(quote):
                        self._archive_order(entry, final)
                    # 真实模式的部分成交、委托到达与排上队不产生终态委托，同样要落盘。
                    if entry.book.take_match_dirty() or entry.pending_orders:
                        self._persist_account(account_id)
                    self._force_close_if_due(account_id, entry)
                self._sweep_arrivals()
        with self._drain_cond:
            self._processed += 1
            self._drain_cond.notify_all()

    def _force_close_if_due(self, account_id: str, entry: _PaperAccount) -> None:
        """强平 (#1204, 所有者 2026-10-30)：像期货公司一样，风险度（保证金 ÷ 权益）到 100% 就强平，不等权益变负。
        先撤该账户全部挂单（释放冻结），再按对价（跌停/涨停价限价，即时成交）平仓，保证金占用大的先平，平到
        预计风险度低于 100% 为止；权益已不为正时全平。强平委托号带 FORCE_ORDER_PREFIX，成交照常进账本、
        委托与成交记录。上一轮强平单还在途、合约不在交易时段或行情过期时这轮不动，下一笔行情再看。
        只在 actor 线程、状态锁内调用。"""
        if account_id in self._uncertain_accounts:
            return
        book = entry.book
        account = book.account_fields()
        balance, margin = float(account.get("balance") or 0.0), float(account.get("margin") or 0.0)
        if margin <= 0 or (balance > 0 and margin < balance):
            return
        live = book.live_orders()
        if any(str(order.get("order_id") or "").startswith(FORCE_ORDER_PREFIX) for order in live):
            return
        for order in live:
            if str(order.get("status")) == "ALIVE":
                self._archive_order(entry, book.cancel(str(order["order_id"])))
        legs: list[tuple[float, str, str, int, int]] = []  # (每手保证金, 合约, 方向, 今仓, 昨仓)
        for symbol, position in book._sim._positions.items():
            for side in ("long", "short"):
                today = int(position.get(f"volume_{side}_today") or 0)
                his = int(position.get(f"volume_{side}_his") or 0)
                if today + his > 0:
                    per_lot = float(position.get(f"margin_{side}") or 0.0) / (today + his)
                    legs.append((per_lot, symbol, side, today, his))
        legs.sort(key=lambda leg: leg[0] * (leg[3] + leg[4]), reverse=True)
        excess = math.inf if balance <= 0 else margin - balance
        sent = 0
        for per_lot, symbol, side, today, his in legs:
            if excess < 0:
                break
            quote = self._quotes.get(symbol)
            if quote is None or self._quote_age(quote) > self.stale_after_seconds or not book.is_in_trading_time(quote):
                continue
            selling = side == "long"
            price = finite_number(quote.get("lower_limit" if selling else "upper_limit"))
            if price is None or price <= 0:
                price = finite_number(quote.get("bid_price1" if selling else "ask_price1"))
            if price is None or price <= 0:
                continue
            lots = today + his if math.isinf(excess) or per_lot <= 0 else min(today + his, math.floor(excess / per_lot) + 1)
            split = symbol.split(".", 1)[0].upper() in CLOSE_YESTERDAY_ONLY_EXCHANGES
            parts = [("CLOSE", min(lots, his)), ("CLOSETODAY", lots - min(lots, his))] if split else [("CLOSE", lots)]
            # #1210: the forced close charges the fee rate its position was opened with — the same source
            # the expiry close reads — not the quote's per-lot commission. Read once: a filled leg may clear it.
            rate = (book._sim._positions.get(symbol) or {}).get(f"fee_rate_{side}")
            fee = {"feeRate": dict(rate)} if isinstance(rate, dict) else {}
            for offset, volume in parts:
                if volume <= 0:
                    continue
                self._force_sequence += 1
                coid = f"{FORCE_ORDER_PREFIX}{account_id.split(':')[-1][:12]}-{int(time.time())}-{self._force_sequence}"
                quote_now, _ = self._resolved_quote(symbol)
                book.inject_quote(quote_now)
                _, final = book.submit({"symbol": symbol, "clientOrderId": coid, "direction": "SELL" if selling else "BUY",
                                        "offset": offset, "volume": volume, "limitPrice": price, **fee})
                if str(final.get("status")) == "FINISHED":
                    self._archive_order(entry, final)
                if book.pending_arrival():
                    self._arrival_accounts.add(account_id)
                sent += 1
            excess -= per_lot * lots
        if sent or entry.pending_orders:
            book.take_match_dirty()
            self._persist_account(account_id)
            _log_line(f"paper force close: account={account_id} balance={balance:.2f} margin={margin:.2f} orders={sent}")

    def _sweep_arrivals(self) -> None:
        """真实成交模型的行情静止兜底（2026-10-13）：委托到达时刻 + 等待上限已过、
        它的合约还没有新行情时，按缓存最新盘口到达。只在 actor 线程、状态锁内调用；
        在每笔行情之后（回放与测试的确定顺序）和真实时钟的空闲轮询里跑。还欠结算
        的账户不撮合（与行情撮合同一条件）。"""
        if not self._arrival_accounts:
            return
        for account_id in sorted(self._arrival_accounts):
            entry = self._accounts.get(account_id)
            if entry is None or account_id in self._uncertain_accounts:
                self._arrival_accounts.discard(account_id)
                continue
            if (self._engine_day and entry.book.book_trading_day
                    and entry.book.book_trading_day < str(self._engine_day)):
                continue
            for final in entry.book.sweep_arrivals(self.stale_after_seconds):
                self._archive_order(entry, final)
            if entry.book.take_match_dirty() or entry.pending_orders:
                self._persist_account(account_id)
            if not entry.book.pending_arrival():
                self._arrival_accounts.discard(account_id)

    def _record_settlement_fields(self, symbol: str, quote: dict[str, Any],
                                  moment: datetime | None) -> bool:
        """交易时段内的行情把 settlement / pre_settlement 记为上一交易日的结算价。
        返回这笔行情是否在交易时段内（撮合与结算推迟的判断用同一个结果）。"""
        if moment is None or not self._calendar_ready():
            return False
        if not in_trading_session(self._clock(), quote.get("trading_time"), self._calendar(), self._tz):
            return False
        try:
            quote_day = trading_day_from_calendar(moment, self._calendar(), self._tz)
        except RuntimeError:
            return True
        belongs = self._previous_trading_date(quote_day)
        for source_field in ("settlement", "pre_settlement"):
            value = finite_number(quote.get(source_field))
            if value is None:
                continue
            self._merge_settlement_record(symbol, belongs, "quote", value)
        return True

    # 来源优先级（PR647 合同）：主动查询 > 交易时段行情携带的结算字段。
    _SETTLEMENT_SOURCE_PRIORITY = {"query": 2, "quote": 1}

    def _merge_settlement_record(self, symbol: str, day: str, source: str,
                                 price: float) -> None:
        """合并一条结算价事实（#527）：更早日期不回写；同日只有不低于现有优先级
        的来源才覆盖（低优先级不得覆盖 query，反向到达的 query 升级；同来源同日
        后到的修订值覆盖先到）。"""
        existing = self._settlement_records.get(symbol)
        if existing is not None:
            old_day, old_source, _ = existing
            if day < old_day:
                return
            if day == old_day and self._SETTLEMENT_SOURCE_PRIORITY[source] < \
                    self._SETTLEMENT_SOURCE_PRIORITY[old_source]:
                return
        self._settlement_records[symbol] = (day, source, price)

    def _advance_engine_day(self) -> None:
        """跨日结算只在引擎层判断：真实日历就绪后，交易日前进一格，账本日落后的
        账户尝试结算（无持仓的不需要结算价，直接日结清当日字段）。目标日的正式
        结算价没到齐的账户推迟，等下一条行情或主动查询结果再试。随后处理挂起的
        到期结算（_resolve_expiries）：查到最后交易日结算价就强平，挂起满一个交易日
        当天仍补查不到就暂估强平，暂估之后查到正式价就补差。"""
        if not self._calendar_ready():
            return
        try:
            day = trading_day_from_calendar(self._clock(), self._calendar(), self._tz)
        except RuntimeError:
            return
        if self._engine_day is None or day > self._engine_day:
            self._engine_day = day
        self._settle_due_accounts(day)
        for account_id in sorted(self._expiry_accounts):
            self._resolve_expiries(account_id)

    def _settle_due_accounts(self, day: str) -> None:
        for account_id, entry in list(self._accounts.items()):
            book_day = entry.book.book_trading_day
            if not book_day:
                # 日历未就绪期间开的账户：账本日此刻才定，直接落在当前交易日。
                entry.book.book_trading_day = day
                self._persist_account(account_id)
            elif book_day < day:
                # 无持仓的账户同样走正式结算：空价格集不等行情，当日损益/费用归零、
                # 日报落上一交易日、GFD 挂单跨日失效（生产 2026-10-09：空仓账户
                # 只推进日期导致当日字段保留、日报缺失）。
                self._try_settle_account(account_id, entry, day)

    @staticmethod
    def _held_symbols(entry: _PaperAccount) -> set[str]:
        return {
            symbol for symbol, position in entry.book._sim._positions.items()
            if position.get("volume_long") or position.get("volume_short")
        }

    def _expired_final_day(self, entry: _PaperAccount, symbol: str) -> str | None:
        """已到期持仓合约的最终结算日 = 最后交易日（2026-10-15 所有者决定）：合约资料
        的到期时间（TqSdk expire_datetime，最后交易日收盘）所在的北京日期。未到期或
        没有到期时间返回 None。"""
        quote = self._quotes.get(symbol) or entry.book.latest_quote(symbol) or {}
        expire_ns = finite_number(quote.get("expire_datetime"))
        if expire_ns is None or expire_ns > int(self._clock().timestamp() * 1e9):
            return None
        return _expire_day(expire_ns, self._tz)

    def _final_price(self, symbol: str, final_day: str) -> tuple[float, str] | None:
        """到期合约最后交易日的正式结算价 (价格, 来源)：按日期补查的结果优先，其次是
        普通结算价记录里恰好是那一天的。"""
        for record in (self._final_records.get(symbol), self._settlement_records.get(symbol)):
            if record is not None and record[0] == final_day:
                return record[2], record[1]
        return None

    def _settlement_plan(self, entry: _PaperAccount, new_day: str
                         ) -> tuple[dict[str, tuple[float, str]], dict[str, str]] | None:
        """本次结算的价格方案：(持仓合约 → (结算价, "settlement"), 要挂起的到期合约 →
        最后交易日)；None = 整户推迟。

        未到期合约只认目标日（新账本日的上一有效交易日）的交易所正式结算价（#527，
        所有者 2026-10-11“日结只等正式结算价”，整户口径不变）：更旧日期不提前推进，
        任何一个没到就整户推迟，绝不用收盘价或旧报价补位。已到期合约按最后交易日
        的正式结算价走原到期强平语义（2026-10-15 所有者决定）：最后交易日恰是目标日
        时与日常结算同一口径（等该日价、整户推迟）；早于目标日（跨停机越过到期日）
        时只挂起这一个合约——本次结算不给它价、不强平，账户其余合约照常结算，强平
        交给 _resolve_expiries；晚于目标日（到期当天收盘后重启、账本还在前一天）时
        整户推迟到换日后按同日口径处理。已挂起的合约同样不给价。"""
        prices: dict[str, tuple[float, str]] = {}
        deferred: dict[str, str] = {}
        target = self._previous_trading_date(new_day)
        for symbol in self._held_symbols(entry):
            if symbol in entry.expiry:
                continue
            final_day = self._expired_final_day(entry, symbol)
            if final_day is None:
                record = self._settlement_records.get(symbol)
                if record is None or record[0] != target:
                    return None
                prices[symbol] = (record[2], "settlement")
            elif final_day < target:
                deferred[symbol] = final_day
            elif final_day > target:
                return None
            else:
                final = self._final_price(symbol, final_day)
                if final is None:
                    return None
                prices[symbol] = (final[0], "settlement")
        return prices, deferred

    def _try_settle_account(self, account_id: str, entry: _PaperAccount,
                            new_day: str) -> bool:
        plan = self._settlement_plan(entry, new_day)
        if plan is None:
            return False
        prices, deferred = plan
        self._settle_account(account_id, entry, new_day, prices, deferred)
        self._index_rebuild(account_id)
        # 挂起的合约若最后交易日结算价已在手，结算之后当场强平（与原到期语义同一结果：
        # 标记到持仓均价结算，再在新交易日按结算价平）。
        self._resolve_expiries(account_id)
        return True

    def _log_settlement_event(self, account_id: str, day: str, message: str) -> None:
        if (account_id, day) in self._settlement_logs:
            return
        self._settlement_logs.add((account_id, day))
        try:
            sys.stderr.write(f"paper {account_id} {day}: {message}\n")
            sys.stderr.flush()
        except Exception:
            pass

    def _settle_account(self, account_id: str, entry: _PaperAccount, new_day: str,
                        prices: dict[str, tuple[float, str]],
                        deferred: dict[str, str] | None = None) -> None:
        # 新挂起的到期合约：先记下最后可得价格（结算会把账本里的行情标记到持仓均价）。
        new_rows = {symbol: self._new_expiry_row(entry, symbol, final_day, new_day)
                    for symbol, final_day in sorted((deferred or {}).items())}
        for symbol in new_rows:
            self._log_settlement_event(account_id, f"{new_day} {symbol}",
                                       "paper expiry settlement deferred: final-day price not found")
        result = entry.book.settle(new_day, prices)
        previous_day = str(result["previous_day"])
        for final in result["day_orders"]:
            entry.archived_orders[str(final["order_id"])] = {**final, "_bookDay": previous_day}
            entry.pending_orders.append({**final, "_bookDay": previous_day})
        for final in result["expiry_orders"]:
            entry.archived_orders[str(final["order_id"])] = {**final, "_bookDay": new_day}
            entry.pending_orders.append({**final, "_bookDay": new_day})
        entry.settlements += 1
        # 内存归档裁剪到当前与上一交易日；更早的数据仍在 SQLite（审查 #465 第 6 条）。
        keep = {new_day, self._previous_trading_date(new_day)}
        entry.archived_orders = {
            coid: record for coid, record in entry.archived_orders.items()
            if str(record.get("_bookDay")) in keep
        }
        expiry_log = self._expiry_log(account_id, entry, previous_day, new_rows)
        log_json = json.dumps({
            **result["trade_log"],
            "settlementSources": result["settlement_sources"],
            **({"expiryFeeSources": result["expiry_fee_sources"]}
               if result.get("expiry_fee_sources") else {}),
            **({"expirySettlements": expiry_log} if expiry_log else {}),
        })
        self._persist_account(account_id, trade_day=(previous_day, log_json), expiry=new_rows)

    # -- 挂起的到期结算（#527 最终结算日协议） --------------------------------------

    def _quote_basis(self, quote: dict[str, Any] | None) -> dict[str, Any] | None:
        """一笔真实行情作为暂估依据：{price, day, at}——最新价、所属交易日（日历覆盖不到时
        退回北京日期）与行情时间（ISO；新旧按它比，不按价格）。"""
        if not quote:
            return None
        price = finite_number(quote.get("last_price"))
        moment = quote_moment(quote)
        if price is None or price <= 0 or moment is None:
            return None
        try:
            day = trading_day_from_calendar(moment, self._calendar(), self._tz)
        except RuntimeError:
            day = moment.astimezone(self._tz).date().isoformat()
        return {"price": price, "day": day, "at": moment.isoformat()}

    @staticmethod
    def _basis_order(basis: dict[str, Any]) -> tuple[str, int, float]:
        """真实行情依据的新旧次序：先交易日，再行情时间。早于本规则写下的挂起记录只有日期
        （精度不足，不伪造时间），同一天里排在任何带时间的依据之前。"""
        at = basis.get("at")
        if isinstance(at, str):
            try:
                return str(basis["day"]), 1, datetime.fromisoformat(at).timestamp()
            except ValueError:
                pass
        return str(basis["day"]), 0, 0.0

    def _newer_basis(self, current: Any, candidate: dict[str, Any] | None) -> bool:
        if candidate is None:
            return False
        if not isinstance(current, dict) or finite_number(current.get("price")) is None:
            return True
        return self._basis_order(candidate) > self._basis_order(current)

    def _track_expiry_basis(self, symbol: str, quote: dict[str, Any]) -> None:
        """挂起中的到期合约收到更新的真实行情：单调更新挂起记录里的暂估依据，与账本同一
        事务落盘（#527 暂估选价，#928 复核）。引擎行情缓存重启即丢，账本里的行情又会被
        结算标记到持仓均价，都不能当暂估依据的来源。actor 线程、状态锁内、入账之前调用。"""
        basis = self._quote_basis(quote)
        if basis is None:
            return
        for account_id in sorted(self._expiry_accounts):
            entry = self._accounts.get(account_id)
            if entry is None or account_id in self._uncertain_accounts:
                continue
            row = entry.expiry.get(symbol)
            if row is None or row["status"] != "pending" or not self._newer_basis(row.get("lastPrice"), basis):
                continue
            self._persist_account(account_id, expiry={symbol: {**row, "lastPrice": basis}})

    def _new_expiry_row(self, entry: _PaperAccount, symbol: str, final_day: str,
                        since: str) -> dict[str, Any]:
        position = entry.book._sim._positions.get(symbol) or {}
        last: dict[str, Any] | None = None
        # 引擎行情缓存是真实行情；账本里的行情这时还没被本次结算标记。取较新的一笔。
        for quote in (self._quotes.get(symbol), entry.book.latest_quote(symbol)):
            found = self._quote_basis(quote)
            if self._newer_basis(last, found):
                last = found
        return {
            "symbol": symbol, "finalDay": final_day, "status": "pending", "since": since,
            "volumeLong": int(position.get("volume_long") or 0),
            "volumeShort": int(position.get("volume_short") or 0),
            "lastPrice": last,
            "events": [{"day": since, "kind": "deferred"}],
        }

    def _estimate_basis(self, symbol: str, row: dict[str, Any]) -> dict[str, Any] | None:
        """暂估价 = 最后可得价格：最后交易日及以前最近一天的正式结算价与挂起记录里的最新
        真实价（挂起时记下、挂起中随行情单调更新并落盘）二者中较新的——先比日期，同一天
        结算价优先，真实行情之间按行情时间；价格高低不参与比较。账本里的行情已被结算
        标记到持仓均价，不是市场价，不用。"""
        candidates: list[tuple[tuple[str, int, int, float], dict[str, Any]]] = []
        record = self._settlement_records.get(symbol)
        if record is not None and record[0] <= row["finalDay"]:
            candidates.append(((record[0], 1, 0, 0.0),
                               {"price": record[2], "kind": "settlement", "day": record[0]}))
        last = row.get("lastPrice")
        if isinstance(last, dict) and finite_number(last.get("price")) is not None:
            day, precise, at = self._basis_order(last)
            candidates.append(((day, 0, precise, at), {
                "price": float(last["price"]), "kind": "last", "day": day,
                **({"at": last["at"]} if precise else {})}))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def _estimate_due(self, symbol: str, row: dict[str, Any], book_day: str) -> bool:
        """挂起满一个交易日（账本日已过挂起当天）且今天又按日期补查过一次仍没有。"""
        day = self._engine_day
        return (day is not None and book_day == day and book_day > str(row["since"])
                and self._final_checked.get(symbol) == day)

    def _resolve_expiries(self, account_id: str) -> None:
        """处理一个账户挂起的到期结算（actor 线程、状态锁内）。每次变化与账本同一
        事务提交，状态随之推进（pending → settled / estimated → adjusted）：重复恢复、
        重复调用都只按库里的状态走，不会再平一次或再补一次差。"""
        entry = self._accounts.get(account_id)
        if entry is None or not entry.expiry or account_id in self._uncertain_accounts:
            return
        book_day = entry.book.book_trading_day
        changed: dict[str, dict[str, Any]] = {}
        for symbol, row in sorted(entry.expiry.items()):
            official = self._final_price(symbol, str(row["finalDay"]))
            if row["status"] == "pending" and official is not None:
                changed[symbol] = self._close_pending_expiry(
                    entry, row, official[0], "settled", {"source": official[1]})
            elif row["status"] == "pending" and self._estimate_due(symbol, row, book_day):
                basis = self._estimate_basis(symbol, row)
                if basis is None:
                    self._log_settlement_event(account_id, f"{book_day} {symbol}",
                                               "paper expiry estimate skipped: no price at all")
                    continue
                changed[symbol] = self._close_pending_expiry(
                    entry, row, float(basis["price"]), "estimated", basis)
            elif row["status"] == "estimated" and official is not None:
                changed[symbol] = self._adjust_estimated_expiry(entry, row, official)
        if changed:
            self._persist_account(account_id, expiry=changed)
            self._index_rebuild(account_id)

    def _close_pending_expiry(self, entry: _PaperAccount, row: dict[str, Any], price: float,
                              status: str, detail: dict[str, Any]) -> dict[str, Any]:
        symbol, book_day = str(row["symbol"]), entry.book.book_trading_day
        label = {"symbol": symbol, "finalDay": row["finalDay"],
                 "source": "estimated" if status == "estimated" else "settlement",
                 "price": price}
        closed = entry.book.close_expired(symbol, price, label)
        for final in closed["orders"]:
            self._archive_order(entry, final)
        key = "estimate" if status == "estimated" else "official"
        return {
            **row, "status": status,
            key: {"price": price, "day": book_day, **detail},
            "closeOrders": [str(final["order_id"]) for final in closed["orders"]],
            "legs": closed["legs"],
            "feeSources": closed["fee_sources"],
            "events": [*row.get("events", []), {"day": book_day, "kind": status, "price": price}],
        }

    def _adjust_estimated_expiry(self, entry: _PaperAccount, row: dict[str, Any],
                                 official: tuple[float, str]) -> dict[str, Any]:
        """暂估强平之后查到正式价：按每条腿 (正式价 − 暂估价) × 手数 × 乘数 补平仓盈亏
        （空头取反），带费率的腿按同一栏（到期强平是平昨）补手续费差；行情每手手续费
        与价格无关，不补。差额进当前账本日。"""
        price, source = official
        close_profit = 0.0
        commission = 0.0
        for leg in row.get("legs", []):
            volume = int(leg.get("volume") or 0)
            multiplier = finite_number(leg.get("multiplier")) or 0.0
            sign = 1.0 if leg.get("side") == "long" else -1.0
            close_profit += sign * (price - float(leg["price"])) * volume * multiplier
            rate = leg.get("feeRate")
            if isinstance(rate, dict):
                commission += fee_rate_leg(rate, "closeYesterday", price, multiplier, volume) \
                    - float(leg.get("commission") or 0.0)
        close_profit, commission = round(close_profit, 2), round(commission, 2)
        entry.book.adjust_close(close_profit, commission)
        book_day = entry.book.book_trading_day
        return {
            **row, "status": "adjusted",
            "official": {"price": price, "day": book_day, "source": source},
            "adjustment": {"closeProfit": close_profit, "commission": commission, "day": book_day},
            "events": [*row.get("events", []),
                       {"day": book_day, "kind": "adjusted", "price": price,
                        "closeProfit": close_profit, "commission": commission}],
        }

    @staticmethod
    def _expiry_view(record: dict[str, Any], events: list[dict[str, Any]] | None = None
                     ) -> dict[str, Any]:
        status = str(record.get("status"))
        view: dict[str, Any] = {
            "symbol": record.get("symbol"), "finalDay": record.get("finalDay"),
            "status": status, "statusLabel": EXPIRY_STATUS_LABELS.get(status, status),
            "since": record.get("since"),
        }
        for key in ("estimate", "official", "adjustment"):
            if isinstance(record.get(key), dict):
                view[key] = dict(record[key])
        if events is not None:
            view["events"] = events
        return view

    def _expiry_log(self, account_id: str, entry: _PaperAccount, day: str,
                    new_rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """日结日志里的到期挂起记录：结算后仍挂起的（含本次新挂起的）与当天有变化的，
        各带当天的事件。暂估的写明“暂估，待核”，补差后的写明“已核”。"""
        assert self._db is not None
        records = {str(row[0]): json.loads(row[1]) for row in self._db.execute(
            "SELECT symbol, record_json FROM paper_expiry_settlements WHERE account_id=?",
            (account_id,)).fetchall()}
        records.update(entry.expiry)
        records.update(new_rows)
        log: list[dict[str, Any]] = []
        for symbol in sorted(records):
            record = records[symbol]
            events = [event for event in record.get("events", []) if event.get("day") == day]
            if events or record.get("status") in EXPIRY_OPEN_STATUSES:
                log.append(self._expiry_view(record, events))
        return log

    def pending_final_settlements(self) -> dict[str, str]:
        """挂起的到期合约 → 最后交易日：行情源按日期补查 query_symbol_settlement。
        暂估过、还没核的也在内。"""
        with self._state_lock:
            wanted: dict[str, str] = {}
            for account_id in self._expiry_accounts:
                for symbol, row in self._accounts[account_id].expiry.items():
                    if self._final_price(symbol, str(row["finalDay"])) is None:
                        wanted[symbol] = str(row["finalDay"])
            return wanted

    def _known_final_day(self, symbol: str) -> str | None:
        for account_id in self._expiry_accounts:
            row = self._accounts[account_id].expiry.get(symbol)
            if row is not None:
                return str(row["finalDay"])
        source = self._quotes.get(symbol) or self._contracts.get(symbol) or {}
        expire_ns = finite_number(source.get("expire_datetime"))
        return _expire_day(expire_ns, self._tz) if expire_ns is not None else None

    # -- 持久化 --------------------------------------------------------------------

    def _archive_order(self, entry: _PaperAccount, final: dict[str, Any]) -> None:
        record = {**final, "_bookDay": entry.book.book_trading_day}
        entry.archived_orders[str(final["order_id"])] = record
        entry.pending_orders.append(record)

    def _write_command_row(self, account_id: str, command_id: str, kind: str,
                           client_order_id: str, request: dict[str, Any],
                           result: dict[str, Any]) -> None:
        """命令行只写一次（审查 527-8）：首次提交的 request_json 不因重放被覆盖；
        commandId 绑定命令类型，跨类型复用直接拒绝；迁移标 legacy 的历史行
        与任何类型都不一致，也拒绝（#527 D4）。调用方须在 `with self._db` 事务内。"""
        assert self._db is not None
        row = self._db.execute(
            "SELECT kind FROM paper_commands WHERE account_id=? AND command_id=?",
            (account_id, command_id)).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO paper_commands "
                "(account_id, command_id, client_order_id, kind, request_json, result_json) "
                "VALUES (?,?,?,?,?,?)",
                (account_id, command_id, client_order_id, kind,
                 json.dumps(request), json.dumps(result)),
            )
        elif row[0] != kind:
            raise RuntimeError("command_kind_mismatch")

    def _stored_trade_day(self, trade: dict[str, Any]) -> str | None:
        """成交行里存的交易日：真实日历就绪时落盘当刻算好（activity 读时不再逐笔
        查日历）；未就绪存空，读时再算，与原来读时现算同一口径。"""
        return self._trade_day(trade) if self._calendar_ready() else None

    def _persist_account(self, account_id: str, *, trade_day: tuple[str, str] | None = None,
                         command: tuple[str, str, str, dict[str, Any], dict[str, Any]] | None = None,
                         expiry: dict[str, dict[str, Any]] | None = None) -> None:
        """当前状态行、本次新增成交行、终态委托、日结日志、到期挂起记录与命令关联行写在同一个
        SQLite 事务里；commit 成功才清 pending、推进成交序号、登记内存命令映射
        （527-1）。落盘失败时数据库回滚，内存由 `_reload_account` 回到最后已提交
        状态，再把错误抛给调用方——Runtime 进 UNKNOWN 后查询得到 found=false，
        账本两侧保持一致。

        写入量与当日已有成交笔数无关（2026-10-12）：当前状态不含当日成交，成交
        只追加本次新增的行。日结（trade_day）时 SimTrade 已把当日成交清零（它们
        全部已在之前的事务里落行，日志 log_json 另存一份）：新账本日的成交序号
        接着往后排，旧行删除，同一事务合并一次检查点（此刻当日成交最少）。"""
        assert self._db is not None
        entry = self._accounts[account_id]
        book = entry.book
        settled = trade_day is not None
        from_seq = entry.trades_from_seq + (entry.persisted_trades if settled else 0)
        written = 0 if settled else entry.persisted_trades
        new_trades = book.trades_since(written)
        revision = entry.revision + 1
        head_json = json.dumps(book.export_state(include_trades=False, trades_from_seq=from_seq))
        try:
            with self._db:
                self._db.execute(
                    "INSERT OR REPLACE INTO paper_account_heads "
                    "(account_id, revision, trades_from_seq, book_trading_day, head_json, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (account_id, revision, from_seq, book.book_trading_day, head_json, utc_now()),
                )
                if new_trades:
                    self._db.executemany(
                        "INSERT INTO paper_trades (account_id, seq, book_day, trading_day, trade_json) "
                        "VALUES (?,?,?,?,?)",
                        [(account_id, from_seq + written + index, book.book_trading_day,
                          self._stored_trade_day(trade), json.dumps(trade))
                         for index, trade in enumerate(new_trades)],
                    )
                for record in entry.pending_orders:
                    self._db.execute(
                        "INSERT OR REPLACE INTO paper_orders "
                        "(account_id, client_order_id, final_json, book_day) VALUES (?,?,?,?)",
                        (account_id, str(record["order_id"]), json.dumps(record),
                         str(record.get("_bookDay") or "")),
                    )
                if trade_day is not None:
                    self._db.execute(
                        "INSERT OR REPLACE INTO paper_trade_log (account_id, trading_day, log_json) "
                        "VALUES (?,?,?)",
                        (account_id, trade_day[0], trade_day[1]),
                    )
                    self._db.execute(
                        "DELETE FROM paper_trades WHERE account_id=? AND seq<?", (account_id, from_seq))
                if command is not None:
                    command_id, kind, client_order_id, request, result = command
                    self._write_command_row(account_id, command_id, kind,
                                            client_order_id, request, result)
                for symbol, record in (expiry or {}).items():
                    self._db.execute(
                        "INSERT OR REPLACE INTO paper_expiry_settlements "
                        "(account_id, symbol, final_day, status, since_day, record_json, updated_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (account_id, symbol, str(record["finalDay"]), str(record["status"]),
                         str(record["since"]), json.dumps(record), utc_now()),
                    )
                if settled:
                    self._write_checkpoint(account_id)
        except Exception:
            # 重建失败时由 _reload_account_after_persist_failure 标记故障停机（#527
            # 任务二）；这里照旧把原始持久化错误抛给调用方。
            self._reload_account_after_persist_failure(account_id)
            raise
        # 提交成功之后才动内存：pending 清空、成交序号推进、命令映射登记。
        entry.pending_orders.clear()
        entry.revision = revision
        entry.trades_from_seq = from_seq
        entry.persisted_trades = written + len(new_trades)
        if settled:
            entry.checkpoint_revision = revision
            entry.checkpoint_at = time.monotonic()
        if command is not None:
            self._command_orders[(account_id, command[0])] = (command[1], command[2])
        for symbol, record in (expiry or {}).items():
            if record["status"] in EXPIRY_OPEN_STATUSES:
                entry.expiry[symbol] = record
            else:
                entry.expiry.pop(symbol, None)
        if entry.expiry:
            self._expiry_accounts.add(account_id)
        else:
            self._expiry_accounts.discard(account_id)

    # -- 检查点 --------------------------------------------------------------------

    def _write_checkpoint(self, account_id: str) -> None:
        """把库里最后已提交的整本账（当前状态行 + 当日成交行）合并写进
        paper_accounts.state_json，revision / checkpoint_of 置为当前状态行的版本。
        只读库、不看内存；调用方负责事务。格式与旧版本整本快照相同。"""
        assert self._db is not None
        state, revision, _ = self._committed_state(self._db, account_id)
        self._db.execute(
            "UPDATE paper_accounts SET state_json=?, book_trading_day=?, revision=?, "
            "checkpoint_of=?, updated_at=? WHERE id=?",
            (json.dumps(state), str(state.get("book_trading_day") or ""), revision, revision,
             utc_now(), account_id),
        )

    def _checkpoint_all(self) -> bool:
        """停机时合并所有检查点没追平的账户（一个事务），再按 checkpoint_lag 核对。
        返回是否全部追平。失败不影响本版本恢复（已提交的当前状态行与成交行仍完整），
        但旧版本会读到落后快照：如实报给调用方，不只写日志。"""
        assert self._db is not None
        try:
            with self._db:
                for account_id in checkpoint_lag(self._db):
                    self._write_checkpoint(account_id)
            lagging = checkpoint_lag(self._db)
        except Exception as error:
            _log_line(f"paper checkpoint on close failed: {type(error).__name__}: {str(error)[:200]}")
            return False
        if lagging:
            _log_line(f"paper checkpoint on close not caught up: {', '.join(lagging)[:400]}")
            return False
        return True

    def _maybe_checkpoint(self) -> None:
        """定期合并（actor 线程）：每 CHECKPOINT_SCAN_SECONDS 看一次，挑一个检查点
        落后最久且已超过间隔的账户合并。失败只记日志、下个间隔再试。"""
        now = time.monotonic()
        if now < self._checkpoint_scan_at:
            return
        self._checkpoint_scan_at = now + CHECKPOINT_SCAN_SECONDS
        with self._state_lock:
            due = [(entry.checkpoint_at, account_id) for account_id, entry in self._accounts.items()
                   if entry.revision > entry.checkpoint_revision
                   and account_id not in self._uncertain_accounts
                   and now - entry.checkpoint_at >= self.checkpoint_interval_seconds]
            if not due or self._db is None:
                return
            _, account_id = min(due)
            entry = self._accounts[account_id]
            entry.checkpoint_at = now
            try:
                with self._db:
                    self._write_checkpoint(account_id)
            except Exception as error:
                _log_line(f"paper checkpoint failed ({account_id}): "
                          f"{type(error).__name__}: {str(error)[:200]}")
                return
            entry.checkpoint_revision = entry.revision

    def _record_command(self, account_id: str, command_id: str, kind: str,
                        client_order_id: str, request: dict[str, Any],
                        result: dict[str, Any]) -> None:
        """重放路径只补命令行：SQL 提交成功后才登记内存映射（527-1）。"""
        assert self._db is not None
        with self._db:
            self._write_command_row(account_id, command_id, kind,
                                    client_order_id, request, result)
        self._command_orders[(account_id, command_id)] = (kind, client_order_id)

    # -- actor ----------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._actor_loop()
        finally:
            # 先宣告退出、再排空：此后入队的命令由 _command 自己排空（见 _command），
            # 两边谁先拿到都只答一次，队列里不会留下没人答复的命令。
            self._actor_exited.set()
            self._fail_pending_commands()

    def _actor_loop(self) -> None:
        while not self._stop.is_set():
            # 命令优先于行情：下单不该等在行情后面。
            while not self._stop.is_set():
                # 停机开始后不再执行队列里的命令：留给退出收尾答 paper_engine_stopping。
                try:
                    command = self._commands.get_nowait()
                except queue.Empty:
                    break
                self._dispatch(command)
                if self._failed():
                    # 命令链路也能标记故障停机（#527 任务二：落盘且重建都失败）：
                    # actor 立即停止，队列里的命令由停机收尾（_run）统一答复。
                    return
            try:
                self._drain_settlements()
            except Exception as error:
                self._mark_fatal(error)
                break
            self._drain_contracts()
            self._maybe_checkpoint()
            try:
                quote = self._quote_queue.get(timeout=0.05)
            except queue.Empty:
                if self._arrival_accounts and not isinstance(self._clock, VirtualClock):
                    # 真实时钟下行情静止也要让到点的委托到达；虚拟时钟只随行情推进，
                    # 兜底只在每笔行情之后跑，回放结果与轮询时机无关。
                    try:
                        with self._state_lock:
                            self._sweep_arrivals()
                    except Exception as error:
                        self._mark_fatal(error)
                        break
                continue
            try:
                self._apply_quote(quote)
            except Exception as error:
                # 行情/结算/落盘异常会把账本留在半修改状态：不能吞掉继续假装
                # 正常，引擎整体标记故障停机（527-3），由进程重启回到已提交状态。
                self._mark_fatal(error)
                break

    def _drain_settlements(self) -> None:
        """把主动查询到的结算价合并进记录并重试推迟的结算；只在 actor 线程跑。
        排空按批次结账：进来几个批次就补几个 _processed，两次 feed 之间没有被
        排空也不会丢计数。按日期补查到期合约最后交易日的结果（#527 最终结算日协议）
        另记进 _final_records，补查过（无论查到与否）的合约记下今天，暂估据此判断。"""
        with self._settlement_lock:
            batches, self._settlement_inbox = self._settlement_inbox, []
        if not batches:
            return
        try:
            with self._state_lock:
                for items, checked in batches:
                    for item in items:
                        symbol = str(item.get("symbol") or "")
                        day = str(item.get("tradingDay") or "")
                        price = finite_number(item.get("settlement"))
                        if not symbol or not day or price is None:
                            continue
                        self._merge_settlement_record(symbol, day, "query", price)
                        if self._known_final_day(symbol) == day:
                            self._final_records[symbol] = (day, "query", price)
                    if checked and self._calendar_ready():
                        try:
                            today = trading_day_from_calendar(self._clock(), self._calendar(), self._tz)
                        except RuntimeError:
                            today = None
                        if today is not None:
                            for symbol in checked:
                                self._final_checked[str(symbol)] = today
                self._advance_engine_day()
        finally:
            with self._drain_cond:
                self._processed += len(batches)
                self._drain_cond.notify_all()

    def feed_settlements(self, items: list[dict[str, Any]], *,
                         checked: dict[str, str] | None = None) -> None:
        """行情源主动查到的结算价（{symbol, tradingDay, settlement}）从这里进引擎，
        actor 合并后立刻重试推迟的结算。一次调用是一个批次。checked：这一批是按日期
        补查到期合约最后交易日结算价的（合约 → 最后交易日），没查到也要报，暂估要
        知道今天确实补查过。"""
        if self._failed() or not (items or checked):
            return
        with self._drain_cond:
            self._posted += 1
        with self._settlement_lock:
            self._settlement_inbox.append(([dict(item) for item in items], dict(checked or {})))

    # -- 合约资料表（#527 B2） ------------------------------------------------------

    def _load_contracts(self) -> None:
        """开库时读资料表进内存镜像；表里还没有的合约用各账本快照里的行情补一次
        （来源 snapshot，旧库升级后不丢已知资料）。已到期的合约不补。"""
        assert self._db is not None
        columns = ("symbol",) + CONTRACT_METADATA + (
            "static_source", "static_updated_at", "fee_source", "fee_updated_at")
        for row in self._db.execute(f"SELECT {', '.join(columns)} FROM paper_contracts").fetchall():
            record = dict(zip(columns, row, strict=True))
            self._contracts[str(record.pop("symbol"))] = {
                key: value for key, value in record.items() if value is not None}
        for entry in self._accounts.values():
            for symbol in entry.book.quote_symbols():
                if symbol in self._contracts:
                    continue
                quote = entry.book.latest_quote(symbol) or {}
                moment = quote_moment(quote)
                self._remember_contract(quote, source="snapshot",
                                        at=moment.isoformat() if moment else None)
        self._reindex_contracts()

    def _reindex_contracts(self) -> None:
        self._contract_index = {symbol.upper(): symbol for symbol in self._contracts}

    def _catalog_complete(self) -> bool:
        """合约服务的整表刷新是否足够新、可以当完整目录（#842）：本进程收到过，且在
        CONTRACT_CATALOG_FRESH_SECONDS 之内。库里的旧表、连续刷新失败后的旧表都不算。"""
        at = self._catalog_at
        return at is not None and (self._clock() - at).total_seconds() <= CONTRACT_CATALOG_FRESH_SECONDS

    def _symbol_spelling(self, symbol: str) -> str:
        """外部送来的合约名（读报价、下单）的唯一入口，调用方持 _state_lock：规范成
        交易所写法，不存在的合约抛 UnknownSymbolError——不进租约、不订阅。

        依据依次是：资料表与最近一次整表刷新列出的名字（合约服务的写法）、已收到行情的
        合约、各账本持仓/挂单的合约（到期后表里清掉了也认），都大小写不敏感；本进程的
        整表刷新足够新（_catalog_complete）时，表外的名字就是不存在的合约。没有足够新的
        刷新（新库、刚重启、刷新连续失败）时按交易所大小写规则写，真不存在的由行情源
        订阅时剔除（reject_symbols）——新挂牌的合约不会被一张旧表拒掉（#842）。"""
        text = str(symbol or "").strip()
        folded = text.upper()
        known = self._contract_index.get(folded) or self._catalog_names.get(folded)
        if known is None:
            known = next((name for name in self._quotes if name.upper() == folded), None)
        if known is None:
            known = next((name for entry in self._accounts.values()
                          for name in entry.book.needed_symbols() if name.upper() == folded), None)
        if known is None and not self._catalog_complete():
            known = rule_symbol(text)
        elif known is None and rule_symbol(text) is not None:
            # 形状对、表里没有：可能是表刷新之后才挂牌的新合约。照样答不存在，但请行情源
            # 提前刷新资料表（至多每小时一次），挂牌了的下一次读就认得。
            self._contract_miss = True
        if known is None or self._rejected(known):
            raise UnknownSymbolError(text)
        return known

    def take_contract_miss(self) -> bool:
        """有人读过表外的合约名（形状对）之后的第一次询问为 True：行情源据此提前刷新资料表。"""
        with self._state_lock:
            missed, self._contract_miss = self._contract_miss, False
            return missed

    def _rejected(self, symbol: str, now: datetime | None = None) -> bool:
        at = self._rejected_symbols.get(symbol)
        if at is None:
            return False
        if ((now or self._clock()) - at).total_seconds() < REJECTED_SYMBOL_SECONDS:
            return True
        self._rejected_symbols.pop(symbol, None)
        return False

    def reject_symbols(self, symbols: list[str], reason: str) -> list[str]:
        """行情源订阅时合约服务答"不存在"的合约：撤掉它的租约、REJECTED_SYMBOL_SECONDS
        内不再进订阅集，等它首包的读取立即答 symbol_not_found。其余合约与行情源不受影响。"""
        now = self._clock()
        dropped: list[str] = []
        with self._state_lock:
            for symbol in symbols:
                name = str(symbol or "").strip()
                if not name:
                    continue
                self._leases.pop(name, None)
                self._rejected_symbols[name] = now
                dropped.append(name)
        if dropped:
            _log_line(f"paper quote source dropped non-existent contract(s) {', '.join(dropped)} "
                      f"for {int(REJECTED_SYMBOL_SECONDS)}s; other contracts unaffected: {reason[:200]}")
        return dropped

    def _with_contract(self, quote: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
        return resolve_contract_metadata(quote, self._contracts.get(str(quote.get("symbol") or "")))

    def _resolved_quote(self, symbol: str) -> tuple[dict[str, Any], set[str]]:
        """缓存行情按当前资料表重补（#777 review）：缓存里由资料表补的字段先去掉
        再补，表刷新后不沿用缓存时的旧值；实时值仍优先。"""
        fills = self._quote_fills.get(symbol, set())
        base = {key: value for key, value in self._quotes[symbol].items() if key not in fills}
        return self._with_contract(base)

    def _contract_expired(self, symbol: str, fields: dict[str, Any], *, grace: float = 0.0) -> bool:
        expire = finite_number(fields.get("expire_datetime"))
        if expire is None:
            expire = finite_number((self._contracts.get(symbol) or {}).get("expire_datetime"))
        return expire is not None and expire <= (self._clock().timestamp() - grace) * 1e9

    def _remember_contract(self, quote: dict[str, Any], *, source: str = "quote",
                           at: str | None = None) -> None:
        """实时行情（或快照）里的有效资料回写资料表。值没变时同一自然日只确认
        一次（更新时间），不是每笔行情写一次库。到期合约不进表。"""
        symbol = str(quote.get("symbol") or "")
        if not symbol or str(quote.get("ins_class") or "") != "FUTURE":
            return
        fields = {name: quote[name] for name in CONTRACT_METADATA
                  if valid_contract_field(name, quote.get(name))}
        if not fields or self._contract_expired(symbol, fields):
            return
        record = self._contract_record(symbol, fields, source, at)
        if record is None:
            return
        try:
            self._write_contracts({symbol: record})
        except sqlite3.Error as error:
            # 实时行情回写只是兜底来源：写失败不影响撮合与账本，记日志下次再写。
            _log_line(f"paper contract table write failed: {type(error).__name__}: {str(error)[:200]}")

    def _contract_record(self, symbol: str, fields: dict[str, Any], source: str,
                         at: str | None = None) -> dict[str, Any] | None:
        """合并后的资料表行；没有变化也不需要确认时返回 None。每手保证金/手续费
        只到一项时值照写，但组的来源/确认时间不前移（#777 review）：缺的那项没被
        确认，不能借这次确认显得新鲜。"""
        now = self._clock()
        stamp = at or now.isoformat()
        today = now.date().isoformat()
        current = self._contracts.get(symbol, {})
        record = {**current, **fields}
        touched = False
        for group, names in (("static", tuple(n for n in CONTRACT_METADATA if n not in FEE_METADATA)),
                             ("fee", FEE_METADATA)):
            present = [name for name in names if name in fields]
            if not present:
                continue
            changed = any(current.get(name) != fields[name] for name in present)
            touched = touched or changed
            if group == "fee" and len(present) < len(names):
                continue
            confirmed_today = str(current.get(f"{group}_updated_at") or "")[:10] >= today
            if changed or (not confirmed_today and at is None):
                record[f"{group}_source"] = source
                record[f"{group}_updated_at"] = stamp
                touched = True
        return record if touched else None

    def _write_contracts(self, records: dict[str, dict[str, Any]],
                         prune: list[str] | None = None) -> None:
        """资料表写入（一个事务）与内存镜像更新。与账户写入共用 _state_lock 串行化
        （#777 review）：同一连接上并发的两个事务会互相回滚。写失败抛给调用方。"""
        assert self._db is not None
        columns = ("symbol",) + CONTRACT_METADATA + (
            "static_source", "static_updated_at", "fee_source", "fee_updated_at")
        with self._state_lock:
            with self._db:
                if records:
                    self._db.executemany(
                        f"INSERT OR REPLACE INTO paper_contracts ({', '.join(columns)}) "
                        f"VALUES ({', '.join('?' for _ in columns)})",
                        [(symbol, *(record.get(name) for name in columns[1:]))
                         for symbol, record in records.items()],
                    )
                if prune:
                    self._db.executemany("DELETE FROM paper_contracts WHERE symbol=?",
                                         [(symbol,) for symbol in prune])
            self._contracts.update(records)
            for symbol in prune or ():
                self._contracts.pop(symbol, None)
            self._reindex_contracts()

    def feed_contracts(self, records: list[dict[str, Any]], listed: list[str] | None = None) -> None:
        """行情源批量刷新到的全部未到期期货合约资料（字段同资料表）从这里进引擎；
        actor 写表。一次调用是一个批次。listed 是合约服务这次列出的全部合约名（资料
        取不全的也算存在，#842）；不给时按 records 的合约名。"""
        if self._failed():
            return
        names = [str(name) for name in (listed if listed is not None
                                        else [record.get("symbol") for record in records]) if name]
        with self._drain_cond:
            self._posted += 1
        with self._contract_lock:
            self._contract_inbox.append((names, [dict(record) for record in records]))

    def note_contract_refresh_failed(self, message: str) -> None:
        """批量刷新失败：旧表照用，健康里带上原因。"""
        with self._state_lock:
            self._contract_refresh_error = str(message)[:200]

    def contract_refresh_failed(self) -> bool:
        """最近一次批量刷新（取数或写表）是否失败：行情源据此按失败安排重试。"""
        with self._state_lock:
            return self._contract_refresh_error is not None

    def _drain_contracts(self) -> None:
        with self._contract_lock:
            batches, self._contract_inbox = self._contract_inbox, []
        if not batches:
            return
        try:
            for listed, records in batches:
                with self._state_lock:
                    merged: dict[str, dict[str, Any]] = {}
                    for record in records:
                        symbol = str(record.get("symbol") or "")
                        fields = {name: record[name] for name in CONTRACT_METADATA
                                  if valid_contract_field(name, record.get(name))}
                        if (not symbol or str(fields.get("ins_class") or "FUTURE") != "FUTURE"
                                or not fields or self._contract_expired(symbol, fields)):
                            continue
                        row = self._contract_record(symbol, fields, "contract_service")
                        if row is not None:
                            merged[symbol] = row
                    expired = [symbol for symbol, record in self._contracts.items()
                               if symbol not in merged and self._contract_expired(
                                   symbol, record, grace=CONTRACT_EXPIRED_GRACE_SECONDS)]
                    # 一个批次一个事务：写失败整批回滚、内存不动，健康报刷新失败，
                    # 行情源据此按失败重试（#777 review）。
                    self._write_contracts(merged, expired)
                    now = self._clock()
                    self._contract_refreshed_at = now.isoformat()
                    self._contract_refresh_error = None
                    # 写表成功才算一次完整目录（#842）：写失败的批次不刷新目录时刻。
                    self._catalog_names = {name.upper(): name for name in listed}
                    self._catalog_at = now
        except Exception as error:
            message = f"{type(error).__name__}: {str(error)[:200]}"
            _log_line(f"paper contract refresh merge failed: {message}")
            with self._state_lock:
                self._contract_refresh_error = message
        finally:
            with self._drain_cond:
                self._processed += len(batches)
                self._drain_cond.notify_all()

    def _contract_fee_cutoff(self) -> str:
        """早于这一天确认过的每手保证金/手续费算旧值：N 个交易日前（日历未就绪时
        按 7 个自然日）。"""
        today = self._clock().date()
        if self._calendar_ready():
            dates = sorted(day for day in self._calendar() if day <= today.isoformat())
            if len(dates) > CONTRACT_FEE_STALE_TRADING_DAYS:
                return dates[-(CONTRACT_FEE_STALE_TRADING_DAYS + 1)]
            return dates[0] if dates else today.isoformat()
        return (today - timedelta(days=7)).isoformat()

    @staticmethod
    def _contract_fee_stale(record: dict[str, Any], cutoff: str) -> bool:
        updated = str(record.get("fee_updated_at") or "")[:10]
        return not updated or updated < cutoff

    def _contract_source(self, symbol: str, filled: set[str]) -> dict[str, Any] | None:
        """下单结果的标注：保证金/手续费来自资料表（不是这笔实时行情），以及是否旧值。
        随委托存进账本（PaperBook.submit），回执、查询与幂等重放都带 contractSource。"""
        if not (filled & set(FEE_METADATA)):
            return None
        record = self._contracts.get(symbol, {})
        return {
            "fees": "contractTable",
            "feeSource": record.get("fee_source"),
            "feeUpdatedAt": record.get("fee_updated_at"),
            "feeStale": self._contract_fee_stale(record, self._contract_fee_cutoff()),
        }

    def _contract_health(self) -> dict[str, Any]:
        cutoff = self._contract_fee_cutoff()
        return {
            "rows": len(self._contracts),
            "refreshedAt": self._contract_refreshed_at,
            "refreshError": self._contract_refresh_error,
            "staleFeeRows": sum(1 for record in self._contracts.values()
                                if self._contract_fee_stale(record, cutoff)),
        }

    def pending_settlement_symbols(self) -> set[str]:
        """账本日落后仍有持仓的账户所持有的合约：行情源该为它们主动查结算价。"""
        with self._state_lock:
            if not self._calendar_ready():
                return set()
            try:
                day = trading_day_from_calendar(self._clock(), self._calendar(), self._tz)
            except RuntimeError:
                return set()
            symbols: set[str] = set()
            for entry in self._accounts.values():
                if entry.book.book_trading_day and entry.book.book_trading_day < day:
                    symbols |= self._held_symbols(entry)
            return symbols

    def settlement_target_day(self) -> str | None:
        """欠结算的账户此刻要的结算价所属日 = 当前交易日的上一交易日（与 _settlement_plan
        同一口径）；日历未就绪为 None。没有主动查询接口的行情源（外盘 MT5）据此按日取价。"""
        with self._state_lock:
            if not self._calendar_ready():
                return None
            try:
                day = trading_day_from_calendar(self._clock(), self._calendar(), self._tz)
            except RuntimeError:
                return None
            return self._previous_trading_date(day)

    def _mark_fatal(self, error: BaseException) -> None:
        """记录故障并停机（527-3）：排空行情队列、叫醒 drain 等待方，命令与
        读接口从此立即失败。"""
        message = f"{type(error).__name__}: {str(error)[:200]}"
        with self._state_lock:
            if self._fatal is None:
                self._fatal = message
        while True:
            try:
                self._quote_queue.get_nowait()
            except queue.Empty:
                break
        with self._drain_cond:
            self._processed = self._posted
            self._drain_cond.notify_all()
        try:
            sys.stderr.write(f"paper engine fatal (actor stopped): {message}\n")
            sys.stderr.flush()
        except Exception:
            pass

    def _fail_pending_commands(self) -> None:
        """actor 退出后给还留在队列里的命令一个明确结局（都没执行过），等待方不必等
        超时：故障停机答 paper_engine_failed，正常停机答 paper_engine_stopping。"""
        reason = "paper_engine_failed" if self._fatal is not None else "paper_engine_stopping"
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                command.response.put_nowait(("err", RuntimeError(reason)))
            except Exception:
                pass

    def _dispatch(self, command: ActorCommand) -> None:
        try:
            with self._state_lock:
                if command.kind == "submit":
                    result = self._handle_submit(
                        str(command.request.get("_account_id")), command)
                elif command.kind == "reset":
                    result = self._handle_reset(str(command.request.get("_account_id")), command)
                else:
                    result = self._handle_cancel(
                        str(command.request.get("_account_id")), command)
            command.response.put(("ok", result))
        except Exception as error:
            command.response.put(("err", error))

    def _require_account(self, account_id: str) -> _PaperAccount:
        """读入口的共用责任入口（须在 _state_lock 内调用，#527 任务二返修）：落盘
        且重建都失败的账户内存可能停在未提交状态，任何确认读（query_order /
        account_info / status / account / positions / activity / combined /
        snapshots / market_quote）都不得把它的内存当已确认事实。检查收敛在锁内
        的这一处：读取在锁上排队期间被标记也不会漏出，query_order 不再另设锁外
        守卫。一般故障停机（527-3）没有恢复失败就不隔离读（527-6 停车只读合同）。"""
        entry = self._accounts.get(account_id)
        if entry is None:
            raise KeyError(account_id)
        if account_id in self._uncertain_accounts:
            raise RuntimeError("paper_engine_failed")
        return entry

    def _settle_before_command(self, account_id: str, entry: _PaperAccount) -> None:
        """账本日落后先尝试结算；目标日的正式结算价未到时拒绝命令（2026-10-11
        所有者合同：不再按撮合已开始用收盘价兜底），委托晚于结算入账，不会被
        这次结算的 GFD 撤单波及。"""
        day = self._current_day(entry)
        book_day = entry.book.book_trading_day
        if not book_day or book_day >= day:
            return
        self._settle_due_accounts(day)
        if entry.book.book_trading_day < day:
            self._log_settlement_event(
                account_id, day, "paper submit rejected: settlement price not ready")
            raise RuntimeError("paper_settlement_pending")

    def _handle_submit(self, account_id: str, command: ActorCommand) -> dict[str, Any]:
        entry = self._require_account(account_id)
        request = command.request
        command_id, coid = str(command.command_id), str(command.client_order_id)
        # commandId 绑定命令类型（审查 527-8）：换一种命令复用同一 commandId 是
        # 调用方错误，直接拒绝；判不出类型的历史行只读回原单（#527 D4）。
        known = self._command_orders.get((account_id, command_id))
        if known is not None and known[0] == COMMAND_KIND_LEGACY:
            return self._legacy_command_report(account_id, entry, known[1], cancel=False)
        if known is not None and known[0] != "submit":
            raise RuntimeError("command_kind_mismatch")
        # 同一 command（换手重放）或同一 clientOrderId（重复提交）都返回该委托的
        # 当前状态——重放读到的是最新状态，不是提交时刻的快照。
        for candidate in ((known[1] if known is not None else None), coid):
            if not candidate:
                continue
            existing = self._known_order(account_id, entry, candidate)
            if existing is not None:
                report = entry.book.order_report(existing)
                self._record_command(account_id, command_id, "submit", candidate, request, report)
                return report
        # 账本日落后先结算再收新委托（2026-10-08）：settle 会撤掉昨日 GFD 挂单，
        # 委托必须晚于结算进账本，不能被这次结算撤掉。目标日正式结算价还没到就拒绝，
        # 调用方稍后重试（不再有收盘价兜底，2026-10-11）；跨停机越过到期日的合约只挂起
        # 它自己，不拦账户其余合约的委托（#527 最终结算日协议）。
        self._settle_before_command(account_id, entry)
        if str(request["symbol"]) not in self._quotes:
            return {"ordersInitialized": False, "found": False, "commandStatus": "NOT_DISPATCHED"}
        # 该账户此前不关心这个合约（索引外）：把引擎缓存的最新行情注入账本供
        # 下单校验读价，不触发撮合；成交或挂单后进入索引，后续行情照常分发。
        symbol = str(request["symbol"])
        # 缓存后资料表可能刚刷新：注入前按当前资料表重补（实时值仍优先）。
        quote, filled = self._resolved_quote(symbol)
        entry.book.inject_quote(quote)
        report, final = entry.book.submit(
            request, contract_source=self._contract_source(symbol, filled))
        if str(final.get("status")) == "FINISHED":
            self._archive_order(entry, final)
        entry.book.take_match_dirty()  # 下单本身就落盘
        if entry.book.pending_arrival():
            self._arrival_accounts.add(account_id)
        self._index_add(account_id, str(request["symbol"]))
        # 账本快照、终态委托与命令行同一事务提交（527-1）：落盘失败时账户内存
        # 回到最后已提交状态并报错。
        self._persist_account(account_id, command=(command_id, "submit", coid, request, report))
        return report

    def _known_order(self, account_id: str, entry: _PaperAccount,
                     client_order_id: str) -> dict[str, Any] | None:
        """在挂单、内存归档和 paper_orders 里找这笔委托：内存只留两天，更早的已结束
        委托从库里回读（审查 #465 第 6 条）；提交去重与查询共用这一处（#473）。"""
        found = entry.book.live_order(client_order_id) or entry.archived_orders.get(client_order_id)
        if found is not None:
            return found
        assert self._db is not None
        row = self._db.execute(
            "SELECT final_json FROM paper_orders WHERE account_id=? AND client_order_id=?",
            (account_id, client_order_id),
        ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def _legacy_command_report(self, account_id: str, entry: _PaperAccount,
                               target: str, *, cancel: bool) -> dict[str, Any]:
        """legacy 命令行的重放（#527 D4）：只按行里记的原单只读返回当前状态，
        不认领类型、不写命令行。原单不存在（重放会变成新下单）或撤单重放遇到
        活单（会撤掉它）都是副作用，拒绝。"""
        if cancel and entry.book.live_order(target) is not None:
            raise RuntimeError("command_kind_mismatch")
        existing = self._known_order(account_id, entry, target)
        if existing is None:
            raise RuntimeError("command_kind_mismatch")
        return entry.book.order_report(existing)

    def _handle_cancel(self, account_id: str, command: ActorCommand) -> dict[str, Any]:
        entry = self._require_account(account_id)
        command_id, coid = str(command.command_id), str(command.client_order_id)
        # commandId 绑定命令类型（审查 527-8）：提交命令的 commandId 不能拿来撤单；
        # 判不出类型的历史行不能撤活单，只读回原单（#527 D4）。
        known = self._command_orders.get((account_id, command_id))
        if known is not None and known[0] == COMMAND_KIND_LEGACY:
            return self._legacy_command_report(account_id, entry, known[1], cancel=True)
        if known is not None and known[0] != "cancel":
            raise RuntimeError("command_kind_mismatch")
        target = (known[1] if known is not None else None) or coid
        live = entry.book.live_order(target)
        if live is None:
            # 同一撤单命令重放：幂等返回当前（已结束）状态；归档内存裁剪后仍从
            # paper_orders 回读（527-9，与 submit/query 共用 _known_order）。
            # 新命令撤已结束/未知委托则拒绝。
            archived = self._known_order(account_id, entry, target)
            if known is not None and archived is not None:
                report = entry.book.order_report(archived)
                self._record_command(account_id, command_id, "cancel", target,
                                     command.request, report)
                return report
            raise RuntimeError("order_not_cancelable")
        symbol = f"{live.get('exchange_id')}.{live.get('instrument_id')}"
        if symbol not in self._quotes:
            return {"ordersInitialized": False, "found": False, "commandStatus": "NOT_DISPATCHED"}
        final = entry.book.cancel(target)
        self._archive_order(entry, final)
        report = entry.book.order_report(final)
        self._persist_account(account_id, command=(command_id, "cancel", target,
                                                   command.request, report))
        return report

    def _handle_reset(self, account_id: str, command: ActorCommand) -> dict[str, Any]:
        """账户重置 (#1204)：先撤全部挂单，再清持仓、资金回初始；重置记录作为一条 reset 命令行与账本快照
        同一事务提交（审计可查），账户编号与归属不变。"""
        entry = self._require_account(account_id)
        cancelled = 0
        for order in entry.book.live_orders():
            if str(order.get("status")) == "ALIVE":
                self._archive_order(entry, entry.book.cancel(str(order["order_id"])))
                cancelled += 1
        cleared = entry.book.reset()
        entry.book.take_match_dirty()
        self._arrival_accounts.discard(account_id)
        self._index_rebuild(account_id)
        account = entry.book.account_fields()
        result = {"accountId": account_id, "status": "reset", "balance": float(account["balance"]),
                  "initBalance": entry.book.init_balance, "cancelledOrders": cancelled,
                  "clearedPositions": cleared, "resetAt": utc_now()}
        self._persist_account(account_id, command=(command.command_id, "reset", "", command.request, result))
        _log_line(f"paper account reset: account={account_id} cancelled={cancelled} cleared={cleared}")
        return result

    def reset_account(self, account_id: str, confirm_account_id: str) -> dict[str, Any]:
        """账户重置的唯一入口（所有者用，经 Runtime 管理接口）：确认号必须与账户号一致。"""
        if confirm_account_id != account_id:
            raise RuntimeError("confirm_mismatch")
        with self._state_lock:
            self._require_account(account_id)
        return self._command("reset", account_id=account_id, command_id=f"reset-{int(time.time() * 1000)}",
                             client_order_id="", request={"confirmAccountId": confirm_account_id})

    def _command(self, kind: str, *, account_id: str, command_id: str,
                 client_order_id: str, request: dict[str, Any]) -> dict[str, Any]:
        if self._fatal is None and self._stop.is_set():
            # 停机开始后不再接纳命令（#527/#510）：确定答复“没有执行”，不入队。
            raise RuntimeError("paper_engine_stopping")
        if self._failed():
            # actor 已故障停机：立即失败，不让调用方等 4 秒超时（527-3）。
            raise RuntimeError("paper_engine_failed")
        response: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        self._commands.put(ActorCommand(
            kind, command_id, client_order_id, {**request, "_account_id": account_id}, response,
        ))
        if self._actor_exited.is_set() or self._stop.is_set():
            # 入队时 actor 已经退出（停机或故障），或停机已开始（close 的排空可能早于这次
            # 入队）：自己排空——队列里剩下的都没被 actor 领取过，各答一次确定结局。否则
            # 等 actor：它要么执行这条命令，要么在退出收尾里答复（_run 先置位再排空）。
            # 不在这里直接报错：命令可能已被 actor 领取执行，答复以 actor 给的为准。
            self._fail_pending_commands()
        try:
            status, payload = response.get(timeout=COMMAND_TIMEOUT_SECONDS)
        except queue.Empty as error:
            raise RuntimeError("paper_command_timeout") from error
        if status == "err":
            raise payload
        return payload

    # -- 账户 --------------------------------------------------------------------

    def create_account(self, account_id: str, init_balance: float = 1_000_000.0) -> dict[str, Any]:
        with self._state_lock:
            if account_id in self._accounts:
                return self.account_info(account_id)
            book = PaperBook(
                account_id=account_id, init_balance=float(init_balance),
                clock=self._clock, trading_dates=self._calendar, fill_model=self.fill_model,
                timezone=self._tz,
            )
            assert self._db is not None
            stamp = utc_now()
            with self._db:
                # 开户即一份检查点（版本 0）+ 同版本的当前状态行，没有成交行。
                self._db.execute(
                    "INSERT OR REPLACE INTO paper_accounts "
                    "(id, init_balance, book_trading_day, state_json, revision, updated_at, checkpoint_of) "
                    "VALUES (?,?,?,?,0,?,0)",
                    (account_id, float(init_balance), book.book_trading_day,
                     json.dumps(book.export_state()), stamp),
                )
                self._db.execute(
                    "INSERT OR REPLACE INTO paper_account_heads "
                    "(account_id, revision, trades_from_seq, book_trading_day, head_json, updated_at) "
                    "VALUES (?,0,0,?,?,?)",
                    (account_id, book.book_trading_day,
                     json.dumps(book.export_state(include_trades=False)), stamp),
                )
                self._db.execute("DELETE FROM paper_trades WHERE account_id=?", (account_id,))
            # 提交成功后才放进内存（527-1 的同一口径）：写库失败时账户不生效。
            self._accounts[account_id] = _PaperAccount(book=book, checkpoint_at=time.monotonic())
            return self.account_info(account_id)

    def account_info(self, account_id: str) -> dict[str, Any]:
        with self._state_lock:
            entry = self._require_account(account_id)
            return {
                "accountId": account_id,
                "initBalance": entry.book.init_balance,
                "bookTradingDay": entry.book.book_trading_day,
                # 真实的落盘版本（审查 527-11）：每个已提交的账户事务 +1。
                "revision": entry.revision,
            }

    # -- 命令入口 -----------------------------------------------------------------

    def submit(self, account_id: str, request: dict[str, Any]) -> dict[str, Any]:
        with self._state_lock:
            self._require_account(account_id)
            # 合约名与读报价同一个入口规范写法，账本里只出现交易所写法；认不出的
            # 原样交给 actor，没有它的行情照旧 NOT_DISPATCHED，不进账本、不订阅。
            try:
                request = {**request, "symbol": self._symbol_spelling(str(request.get("symbol") or ""))}
            except UnknownSymbolError:
                pass
        # 平今只对上期所/能源开放——与 sidecar 同一道门，直接调用引擎也过这里。
        symbol = str(request.get("symbol") or "")
        if (str(request.get("offset")) == "CLOSETODAY"
                and symbol.split(".", 1)[0].upper() not in {"SHFE", "INE"}):
            raise ValueError("close_today_exchange_required")
        return self._command(
            "submit", account_id=account_id,
            command_id=str(request["commandId"]), client_order_id=str(request["clientOrderId"]),
            request=request,
        )

    def cancel_order(self, account_id: str, client_order_id: str, command_id: str) -> dict[str, Any]:
        self._require_account_readonly(account_id)
        return self._command(
            "cancel", account_id=account_id,
            command_id=command_id, client_order_id=client_order_id, request={},
        )

    def query_order(self, account_id: str, client_order_id: str) -> dict[str, Any]:
        # 不确定账户的隔离检查在 _state_lock 内的 _require_account（共用责任
        # 入口），HTTP 503、冷重启回读为准（#527 任务二返修）。
        with self._state_lock:
            entry = self._require_account(account_id)
            known = self._known_order(account_id, entry, client_order_id)
            if known is not None:
                return entry.book.order_report(known)
            return {"ordersInitialized": True, "found": False}

    def _require_account_readonly(self, account_id: str) -> None:
        with self._state_lock:
            self._require_account(account_id)

    # -- 读接口 ---------------------------------------------------------------------

    def _age_seconds(self) -> float:
        if self._last_quote_moment is None:
            return float("inf")
        return max(0.0, (self._clock() - self._last_quote_moment).total_seconds())

    def _failed(self) -> bool:
        """引擎是否已故障停机（527-3）：actor 线程死亡，或行情处理链路报过
        致命错误。置位后命令立即失败、读接口 unavailable、行情不再入队。"""
        if self._fatal is not None:
            return True
        thread = self._thread
        return thread is not None and not thread.is_alive()

    def _data_status(self) -> str:
        if self._failed():
            return "unavailable"
        if not self._quotes:
            return "unavailable"
        return "normal" if self._age_seconds() <= self.stale_after_seconds else "stale"

    def health(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "schemaVersion": 1,
                "service": PAPER_SERVICE,
                "status": self._data_status(),
                "dataStatus": self._data_status(),
                "tradingDay": self._current_day(),
                "ordersInitialized": bool(self._quotes),
                "accountCount": len(self._accounts),
                "liveTrading": "disabled",
                "contracts": self._contract_health(),
                "fillModel": (self.fill_model or IDEAL).describe(),
            }

    def rejected_symbols(self) -> list[str]:
        """行情源答过不存在、眼下不订阅的合约（/healthz 带出，排查用）。"""
        with self._state_lock:
            return sorted(symbol for symbol in list(self._rejected_symbols) if self._rejected(symbol))

    def status(self, account_id: str) -> dict[str, Any]:
        with self._state_lock:
            entry = self._require_account(account_id)
            return {
                **self.health(),
                "tradingDay": self._current_day(entry),
                # 账户数据新鲜度不跟随行情（审查 #465 第 4 条）：引擎加载完毕、
                # 行情源在线（或 60 秒重连窗口内）即 normal，age 是快照生成年龄。
                "ageSeconds": 0.0,
                "dataStatus": self._source_state(),
                "ordersInitialized": self._opened,
                "bookTradingDay": entry.book.book_trading_day,
                "simulationTrading": True,
                "readOnly": False,
            }

    def account(self, account_id: str) -> dict[str, Any]:
        with self._state_lock:
            entry = self._require_account(account_id)
            fields = entry.book.account_fields()
            equity = number(fields.get("balance"))
            position_profit = number(fields.get("position_profit"))
            close_profit = number(fields.get("close_profit"))
            commission = number(fields.get("commission"))
            margin = number(fields.get("margin"))
            return {
                "accountName": "本站模拟盘",
                "capturedAt": utc_now(),
                "tradingDay": entry.book.book_trading_day,
                "ageSeconds": 0.0,
                "dataStatus": self._source_state(),
                "equity": equity,
                "staticBalance": number(fields.get("static_balance")),
                "todayPnl": close_profit + position_profit - commission,
                "todayNetPnl": close_profit + position_profit - commission,
                "floatProfit": number(fields.get("float_profit")),
                "positionProfit": position_profit,
                "closeProfit": close_profit,
                "commission": commission,
                "available": number(fields.get("available")),
                "margin": margin,
                "riskRatio": (margin / equity * 100) if equity > 0 else 0.0,
                "marginRatio": (margin / equity * 100) if equity > 0 else 0.0,
                "positionCount": len(entry.book.positions()),
                # 挂起中的到期结算（#527 最终结算日协议）：待最后交易日结算价 / 暂估待核。
                "expirySettlements": [self._expiry_view(entry.expiry[symbol])
                                      for symbol in sorted(entry.expiry)],
            }

    def positions(self, account_id: str) -> list[dict[str, Any]]:
        with self._state_lock:
            entry = self._require_account(account_id)
            return entry.book.positions()

    def snapshots(self, account_ids: list[str]) -> dict[str, Any]:
        """The Runtime's shared poll: each account's status/account/positions,
        the same shapes as the three single-account reads. An account that cannot
        be read carries its own error; the others are unaffected."""
        accounts: dict[str, Any] = {}
        for account_id in account_ids:
            try:
                accounts[account_id] = {
                    "status": self.status(account_id),
                    "account": self.account(account_id),
                    "positions": self.positions(account_id),
                }
            except KeyError:
                accounts[account_id] = {"error": "account_not_found", "httpStatus": 404}
            except RuntimeError as error:
                accounts[account_id] = {"error": str(error), "httpStatus": 503}
        return {"schemaVersion": 1, "accounts": accounts}

    def _trade_day(self, trade: dict[str, Any]) -> str | None:
        timestamp = int(number(trade.get("trade_date_time")))
        if timestamp <= 0:
            return None
        moment = datetime.fromtimestamp(timestamp / 1e9, tz=UTC).astimezone(CHINA_TIMEZONE)
        try:
            return trading_day_from_calendar(moment, self._calendar(), self._tz)
        except RuntimeError:
            return None

    def combined(self, account_id: str) -> dict[str, Any]:
        """One trading fact per read (#700 review): status / account / positions / activity
        read under ONE lock hold from the same engine state, so a Runtime reader (the
        stop-close chain) never pairs one request's activity with another request's
        positions. The shapes are the same projections the four single reads answer with
        (the RLock makes the nested reads one consistent cut). The day's order and trade
        rows are then read outside the lock from the database snapshot pinned inside it."""
        with self._state_lock:
            head = {
                "schemaVersion": 1,
                "capturedAt": utc_now(),
                "ageSeconds": 0.0,
                "dataStatus": self._source_state(),
                "status": self.status(account_id),
                "account": self.account(account_id),
                "positions": self.positions(account_id),
            }
            cut = self._activity_cut(account_id)
        return {**head, "activity": self._activity_rows(cut)}

    def activity(self, account_id: str) -> dict[str, Any]:
        return self._activity_rows(self._activity_cut(account_id))

    def _reader(self) -> sqlite3.Connection:
        try:
            return self._readers.get_nowait()
        except queue.Empty:
            reader = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
            reader.execute("PRAGMA query_only=1")
            return reader

    def _activity_cut(self, account_id: str) -> dict[str, Any]:
        """activity 在撮合锁内只做常数量的事（2026-10-12 压测：锁内遍历当日 8000 笔
        要 72 ms，期间撮合排队）：取活单、账本日与当日成交序号起点，并在锁内开一个
        读事务、用第一条读定下 WAL 读快照——锁内内存与已提交行是同一份事实
        （actor 只在持锁时改账本并在释放前提交），之后锁外按索引读当日行仍是这一刻。"""
        with self._state_lock:
            entry = self._require_account(account_id)
            reader = self._reader()
            try:
                reader.execute("BEGIN")
                reader.execute("SELECT revision FROM paper_account_heads WHERE account_id=?",
                               (account_id,)).fetchone()
            except Exception:
                reader.close()
                raise
            return {
                "account_id": account_id,
                "day": entry.book.book_trading_day,
                "from_seq": entry.trades_from_seq,
                "live": [PaperBook.activity_order(order) for order in entry.book.live_orders()],
                "reader": reader,
                "data_status": self._source_state(),
                "opened": self._opened,
            }

    def _activity_rows(self, cut: dict[str, Any]) -> dict[str, Any]:
        reader: sqlite3.Connection = cut["reader"]
        account_id, day = cut["account_id"], cut["day"]
        try:
            order_rows = reader.execute(
                "SELECT final_json FROM paper_orders WHERE account_id=? AND book_day=?",
                (account_id, day)).fetchall()
            log_row = reader.execute(
                "SELECT log_json FROM paper_trade_log WHERE account_id=? AND trading_day=?",
                (account_id, day)).fetchone()
            trade_rows = reader.execute(
                "SELECT trading_day, trade_json FROM paper_trades WHERE account_id=? AND seq>=? "
                "ORDER BY seq", (account_id, cut["from_seq"])).fetchall()
            expiry_status = {(str(symbol), str(final_day)): str(status)
                             for symbol, final_day, status in reader.execute(
                                 "SELECT symbol, final_day, status FROM paper_expiry_settlements "
                                 "WHERE account_id=?", (account_id,)).fetchall()}
            reader.execute("COMMIT")
        except Exception:
            reader.close()
            raise
        self._readers.put(reader)
        orders = list(cut["live"])
        orders.extend(PaperBook.activity_order(json.loads(final_json)) for (final_json,) in order_rows)
        orders.sort(key=lambda item: int(item["insertDateTime"]), reverse=True)
        logged = json.loads(log_row[0]).get("trades", []) if log_row is not None else []
        trades = [
            {**PaperBook.trade_record(dict(trade)), "tradingDay": day,
             **({"reason": trade["reason"]} if trade.get("reason") else {})}
            for trade in logged
        ]
        for trading_day, trade_json in trade_rows:
            trade = json.loads(trade_json)
            trades.append({
                **PaperBook.trade_record(trade),
                "tradingDay": trading_day if trading_day is not None else self._trade_day(trade),
                **({"reason": trade["reason"]} if trade.get("reason") else {})})
        for trade in trades:
            # 挂起后强平的到期成交带上它那条到期记录的当前状态：暂估的成交在补差后显示“已核”。
            label = trade.get("expirySettlement")
            if isinstance(label, dict):
                status = expiry_status.get((str(label.get("symbol")), str(label.get("finalDay"))))
                if status is not None:
                    trade["expirySettlement"] = {
                        **label, "status": status,
                        "statusLabel": EXPIRY_STATUS_LABELS.get(status, status)}
        trades.sort(key=lambda item: int(item["tradeDateTime"]), reverse=True)
        return {
            "schemaVersion": 1,
            "capturedAt": utc_now(),
            "ageSeconds": 0.0,
            "dataStatus": cut["data_status"],
            "ordersInitialized": cut["opened"],
            "orders": orders,
            "trades": trades,
            "liveTrading": "disabled",
        }

    def market_quote(self, account_id: str, symbol: str) -> dict[str, Any]:
        # 先记租约再查行情：从未订阅过的合约第一次读取就进入 wanted_symbols，
        # 订阅不会被"查不到行情"挡在门外（审查 #465 第 3 条）。租约之前先把合约名
        # 规范成交易所写法、不存在的直接 symbol_not_found（2026-10-13：一个大写的
        # SHFE.RB2701 进了租约，行情源整条连接反复重连、全站报价不可用）。
        with self._state_lock:
            entry = self._require_account(account_id)
            symbol = self._symbol_spelling(symbol)
            # 订阅早已断了（租约过期、也没有持仓挂单）的合约，缓存里留着的是断订阅前的旧报价：
            # 这次读取重新订阅后像首次一样等新的一笔，不把旧报价当答案（2026-10-21：微型黄金
            # 隔天第一单因此被 Runtime 判“行情不是最新”挡回，隔几秒重下才成）。
            lapsed = not self._subscribed(symbol)
            self._leases[symbol] = self._clock()
            quote = self._quotes.get(symbol)
            old_age = self._quote_age(quote) if quote is not None and lapsed else None
            # 休市时等不来新的一笔：只在交易时段内等。
            renew = (old_age is not None and old_age > self.stale_after_seconds
                     and quote is not None and entry.book.is_in_trading_time(quote))
        if quote is None or renew:
            # 等行情源首包（不持状态锁，不挡 actor），超时报明确的错误码；等待中
            # 行情源答了"合约不存在"就立即报 symbol_not_found。断订阅的旧报价等不到新的就照旧答它（标旧）。
            deadline = time.monotonic() + self.quote_wait_seconds
            waiting = True
            while waiting and time.monotonic() < deadline:
                time.sleep(0.02)
                with self._state_lock:
                    if self._rejected(symbol):
                        raise UnknownSymbolError(symbol)
                    self._leases[symbol] = self._clock()
                    current = self._quotes.get(symbol)
                    if current is not None and (not renew or self._quote_age(current) < (old_age or 0.0) - 1.0):
                        quote, waiting = current, False
            if quote is None:
                raise RuntimeError("market_quote_not_ready")
        with self._state_lock:
            quote, _ = (self._resolved_quote(symbol) if symbol in self._quotes
                        else self._with_contract(quote))
            entry = self._require_account(account_id)
            result = detach_quote(symbol, AttrView(dict(quote)))
            moment = quote_moment(quote)
            age = self._age_seconds() if moment is None else max(
                0.0, (self._clock() - moment).total_seconds())
            self._leases[symbol] = self._clock()
            status = "normal" if age <= self.stale_after_seconds else "stale"
            return {
                **result,
                "price": result["lastPrice"],
                "capturedAt": utc_now(),
                "ageSeconds": age,
                "dataStatus": status,
                # The same calendar-aware session judgement the matcher uses (holidays
                # included), so the Runtime refuses a closed-market order up front
                # instead of the engine rejecting it after the fact.
                "marketSessionStatus": "trading" if entry.book.is_in_trading_time(quote) else "closed",
                # detach_quote reads these through `.get`, which the dict view lacks:
                # the per-lot figures the order estimate needs come from the raw quote.
                "marginPerLot": _per_lot(quote.get("margin")),
                "commissionPerLot": _per_lot(quote.get("commission")),
                "isCached": status == "stale",
                "liveTrading": "disabled",
            }

    def settlement_count(self, account_id: str) -> int:
        with self._state_lock:
            return self._require_account(account_id).settlements


class RecordedQuoteSource:
    """离线回放：JSONL 每行一份行情 dict，start 后按序喂给引擎（一遍即止）。"""

    def __init__(self, path: str | None = None, lines: list[str] | None = None) -> None:
        if lines is None:
            with open(str(path), encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        self.quotes = [json.loads(line) for line in lines or [] if line.strip()]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, engine: PaperEngine, interval: float = 0.05) -> None:
        def run() -> None:
            for quote in self.quotes:
                if self._stop.is_set():
                    break
                engine.feed_quote(quote)
                self._stop.wait(interval)

        self._thread = threading.Thread(target=run, name="paper-recorded-quotes", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


def contract_service_fees(api: Any, symbols: set[str]) -> dict[str, dict[str, Any]]:
    """每手保证金/手续费：TqSdk 3.10.2 合约服务的期货片段带 margin/commission
    （ins_schema.future_frag），get_quote 正是从这里拿到它们（symbols.TqSymbols）；
    query_symbol_info 的应答留在 api._data["symbols"]，但它的 DataFrame 不带这两列。
    按 SDK 自己的 _symbols_to_quotes 从缓存的应答里取；私有结构变了就返回空，
    改由实时行情回写补齐。"""
    fees: dict[str, dict[str, Any]] = {}
    try:
        cached = api._data.get("symbols", {}) or {}
        for result in list(cached.values()):
            if not isinstance(result, dict) or not result.get("result"):
                continue
            for symbol, quote in api._symbols_to_quotes(result, keys={"margin", "commission"}).items():
                if symbol in symbols and quote:
                    fees[symbol] = dict(quote)
    except Exception:
        return {}
    return fees


def contract_records_from_symbol_info(rows: list[dict[str, Any]],
                                      fees: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """query_symbol_info 行 → 资料表字段（到期时间秒 → 纳秒，与 Book 的比较单位一致）。"""
    records: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("instrument_id") or "")
        if not symbol:
            continue
        expire = finite_number(row.get("expire_datetime"))
        record = {
            "symbol": symbol,
            "ins_class": str(row.get("ins_class") or ""),
            "product_id": str(row.get("product_id") or ""),
            "volume_multiple": finite_number(row.get("volume_multiple")),
            "price_tick": finite_number(row.get("price_tick")),
            "expire_datetime": expire * 1e9 if expire is not None else None,
            "delivery_year": int(number(row.get("delivery_year"))),
            "delivery_month": int(number(row.get("delivery_month"))),
        }
        fee = fees.get(symbol) or {}
        record["margin"] = finite_number(fee.get("margin"))
        record["commission"] = finite_number(fee.get("commission"))
        records.append(record)
    return records


class TqQuoteSource:
    """行情源：独立 data-only TqApi（不进实盘会话的命令队列），只订 quote。

    订阅集 = 引擎 wanted_symbols()；累计订阅超过上限就重建会话（先订必要的
    合约再切换），订阅从零重新累计——TqSdk 不退订，只能换会话（同 #456 的做法）。
    交易日历建连时取一次（今天 −14 ～ +31 天）；行情会话长期不断线时，剩余可用
    交易日不足一周就在会话内续期，失败保留旧日历、限流后重试（审查 527-10）。
    结算价主动查询（2026-10-08）：引擎有账户等结算价时，每 5 分钟
    query_symbol_settlement 一次喂给引擎，交易所公布（约 19:00）即结算。
    行情账号接力（2026-10-12，paper_sources.py）：连哪个快期账号由 `relay` 决定，
    同一时刻只连一个；认证失败立即换、连接失败/断线连续 MAX_FAILURES 次换。
    """

    MAX_PAPER_QUOTES = 200
    # 日历剩余可用日期不足一周就续期；失败后至少间隔一小时再试（527-10）。
    CALENDAR_REFRESH_AHEAD_DAYS = 7
    CALENDAR_RETRY_SECONDS = 3600.0
    # 结算价主动查询（2026-10-08）：有账户等结算价时每 5 分钟查一次，交易所
    # 约 19:00 公布，公布即由引擎结算；查询带 8 个交易日便于跨多日停机取最新。
    SETTLEMENT_QUERY_DAYS = 8
    # 合约资料表批量刷新（#527 B2）：启动一次，之后每天 20:00（结算价约 19:00 公布
    # 之后、夜盘之前）一次；失败保留旧表，一小时后再试。
    CONTRACT_REFRESH_HOUR = 20
    CONTRACT_RETRY_SECONDS = 3600.0

    # 同一账号两次连接尝试之间的等待（原来的固定 5 秒重连间隔）。
    RETRY_SECONDS = 5.0
    # 换了账号后第一次连接前的等待：不必再等满 RETRY_SECONDS。
    SWITCH_SECONDS = 0.5

    def __init__(self, credentials: Callable[[], tuple[str, str]], *,
                 relay: MarketSourceRelay | None = None,
                 api_factory: Callable[[str, str], Any] | None = None) -> None:
        # credentials：引擎自己挂载的账号（secrets 文件）；Runtime 下发清单之前只用它。
        self.relay = relay or MarketSourceRelay(credentials)
        self._api_factory = api_factory or self._official_api
        self._account: SourceAccount | None = None  # 当前会话用的账号
        self._calendar: set[str] = set()
        # None=从未尝试：0.0 会被当成时间点，刚开机（monotonic 绝对值低）时
        # 首次续期被误判冷却中（#708）。
        self._calendar_attempt_at: float | None = None
        self._settlement_attempt_at = 0.0
        self._contract_attempt_at: float | None = None
        self._contract_refreshed_at: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def trading_dates(self) -> set[str]:
        return set(self._calendar)

    def start(self, engine: PaperEngine) -> None:
        self._thread = threading.Thread(
            target=self._run, args=(engine,), name="paper-tq-quotes", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.relay.wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _pause(self, seconds: float) -> None:
        """等一会儿再连；清单更新或停止会提前唤醒。"""
        if self._stop.is_set():
            return
        self.relay.wake.wait(max(0.0, seconds))
        self.relay.wake.clear()

    def _fetch_calendar(self, api: Any) -> None:
        today = datetime.now(CHINA_TIMEZONE).date()
        frame = api.get_trading_calendar(today - timedelta(days=14), today + timedelta(days=31))
        self._calendar = {
            value.date().isoformat()
            for value, trading in zip(frame["date"], frame["trading"], strict=True)
            if bool(trading)
        }

    def _calendar_refresh_due(self) -> bool:
        """日历前沿是否已进入续期窗口（527-10）。检查与占坑合一：同一时间只
        放行一次续期尝试，失败也要等冷却结束。从未尝试（None）不占冷却、立即
        放行，不看 monotonic 绝对值（#708）。"""
        now = time.monotonic()
        horizon = (datetime.now(CHINA_TIMEZONE).date()
                   + timedelta(days=self.CALENDAR_REFRESH_AHEAD_DAYS)).isoformat()
        if self._calendar and max(self._calendar) >= horizon:
            return False
        last = self._calendar_attempt_at
        if last is not None and now - last < self.CALENDAR_RETRY_SECONDS:
            return False
        self._calendar_attempt_at = now
        return True

    def _refresh_calendar(self, api: Any) -> None:
        try:
            self._fetch_calendar(api)
        except Exception as error:
            # 保留旧日历继续用（宁可按旧日历拒新仓，也不伪造交易日），稍后重试。
            try:
                sys.stderr.write(f"paper trading calendar refresh failed (keeping old): "
                                 f"{type(error).__name__}: {str(error)[:200]}\n")
                sys.stderr.flush()
            except Exception:
                pass

    def _settlement_query_due(self) -> bool:
        """结算价主动查询的检查与占坑合一：同一时间只放行一次查询尝试，失败也
        要等冷却结束（引擎侧账户等结算价时才发起）。"""
        if time.monotonic() - self._settlement_attempt_at < SETTLEMENT_QUERY_SECONDS:
            return False
        self._settlement_attempt_at = time.monotonic()
        return True

    def _query_settlements(self, engine: PaperEngine, api: Any) -> None:
        """为等结算价的持仓合约主动查 query_symbol_settlement。失败只记日志等
        下个周期，绝不落进行情会话的重连路径——查询失败不代表连接断了。"""
        symbols = sorted(engine.pending_settlement_symbols())
        if not symbols:
            return
        try:
            frame = api.query_symbol_settlement(symbols, self.SETTLEMENT_QUERY_DAYS)
            records = frame.to_dict(orient="records")
        except Exception as error:
            try:
                sys.stderr.write(f"paper settlement query failed (will retry): "
                                 f"{type(error).__name__}: {str(error)[:200]}\n")
                sys.stderr.flush()
            except Exception:
                pass
            return
        items = self.settlement_items(symbols, records)
        if items:
            engine.feed_settlements(items)

    def _query_final_settlements(self, engine: PaperEngine, api: Any) -> None:
        """挂起的到期合约按日期补查最后交易日的结算价（#527 最终结算日协议）：
        query_symbol_settlement(symbols, days=1, start_dt=最后交易日) 只返回那一天
        （TqSdk 3.10.2 docstring：指定 start_dt 时返回从该日起已产生的 days 个交易日）。
        同一最后交易日的合约一次查。查到没查到都报给引擎（checked）——暂估要知道
        今天确实补查过；查询失败同样算“补查不到”，只记日志，不进重连路径。"""
        wanted = engine.pending_final_settlements()
        if not wanted:
            return
        by_day: dict[str, list[str]] = {}
        for symbol, day in wanted.items():
            by_day.setdefault(day, []).append(symbol)
        for day, symbols in sorted(by_day.items()):
            symbols.sort()
            try:
                frame = api.query_symbol_settlement(symbols, days=1, start_dt=date.fromisoformat(day))
                records = frame.to_dict(orient="records")
            except Exception as error:
                records = []
                try:
                    sys.stderr.write(f"paper final settlement query failed ({day}): "
                                     f"{type(error).__name__}: {str(error)[:200]}\n")
                    sys.stderr.flush()
                except Exception:
                    pass
            items = [item for item in self.settlement_items(symbols, records)
                     if item["tradingDay"] == day]
            engine.feed_settlements(items, checked={symbol: day for symbol in symbols})

    @staticmethod
    def settlement_items(requested: list[str],
                         records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """查询返回行 → 引擎的 {symbol, tradingDay, settlement}，symbol 映射回请求的
        完整代码（#527 PR-3）。官方文档写明返回的 symbol 列是“交易所内合约代码”
        （``rb2701``），引擎按完整代码（``SHFE.rb2701``）取价——原样喂进去永远
        对不上，整夜 pending。按请求建“交易所内代码→完整代码”映射；服务端已带
        交易所前缀的行（等于某个请求代码）直接用。映射不唯一（不同交易所同名
        合约同时在查）或不在请求里的行丢弃并记日志，不猜归属。"""
        requested_set = set(requested)
        local: dict[str, list[str]] = {}
        for full in requested:
            local.setdefault(full.split(".", 1)[-1], []).append(full)
        items: list[dict[str, Any]] = []
        dropped: list[str] = []
        for row in records:
            day = trading_day_string(row.get("datetime"))
            price = finite_number(row.get("settlement"))
            raw = str(row.get("symbol") or "").strip()
            if not day or price is None or not raw:
                continue
            if raw in requested_set:
                symbol = raw
            else:
                candidates = local.get(raw, []) if "." not in raw else []
                if len(candidates) != 1:
                    dropped.append(f"{raw}({'ambiguous' if candidates else 'unrequested'})")
                    continue
                symbol = candidates[0]
            items.append({"symbol": symbol, "tradingDay": day, "settlement": price})
        if dropped:
            try:
                sys.stderr.write("paper settlement rows dropped (symbol not mappable): "
                                 f"{', '.join(sorted(set(dropped)))[:400]}\n")
                sys.stderr.flush()
            except Exception:
                pass
        return items

    def _contract_refresh_due(self, engine: PaperEngine | None = None) -> bool:
        """合约资料表刷新的检查与占坑合一：从未成功 → 立即；成功过 → 当天 20:00
        之后还没刷新过才刷新；有人读过表外的合约名 → 提前一次。距上次发起不足一小时
        不再发起。交给引擎的批次写表失败也算失败（#777 review）。"""
        now_mono = time.monotonic()
        last = self._contract_attempt_at
        if last is not None and now_mono - last < self.CONTRACT_RETRY_SECONDS:
            return False
        if engine is not None and engine.take_contract_miss():
            # 读过表外的合约名：可能是新挂牌的合约，提前刷新一次（与失败重试同一个间隔）。
            self._contract_attempt_at = now_mono
            return True
        if (self._contract_refreshed_at is not None and engine is not None
                and engine.contract_refresh_failed()):
            self._contract_refreshed_at = None
        if self._contract_refreshed_at is not None:
            now = datetime.now(CHINA_TIMEZONE)
            evening = now.replace(hour=self.CONTRACT_REFRESH_HOUR, minute=0, second=0, microsecond=0)
            if now < evening or self._contract_refreshed_at >= evening:
                return False
        self._contract_attempt_at = now_mono
        return True

    def _refresh_contracts(self, engine: PaperEngine, api: Any) -> None:
        """全部未到期期货合约的资料：query_quotes 列合约 + 一次 query_symbol_info 批量
        取静态字段（与只读 sidecar 的合约目录同一组官方接口）；每手保证金/手续费
        query_symbol_info 的表格不带，从同一次合约服务应答里取（取不到就只靠实时
        行情回写）。失败只记日志与健康说明，旧表照用，不影响交易。"""
        try:
            symbols = sorted({str(symbol) for symbol in api.query_quotes(
                ins_class="FUTURE", expired=False) if str(symbol)})
            if not symbols:
                raise RuntimeError("contract_list_empty")
            rows = api.query_symbol_info(symbols).to_dict(orient="records")
            records = contract_records_from_symbol_info(rows, contract_service_fees(api, set(symbols)))
            if not records:
                raise RuntimeError("contract_metadata_empty")
        except Exception as error:
            message = f"{type(error).__name__}: {str(error)[:200]}"
            _log_line(f"paper contract refresh failed (keeping old table): {message}")
            engine.note_contract_refresh_failed(message)
            return
        engine.feed_contracts(records, listed=symbols)
        self._contract_refreshed_at = datetime.now(CHINA_TIMEZONE)

    @staticmethod
    def _official_api(username: str, password: str) -> Any:
        from tqsdk import TqApi, TqAuth  # 惰性导入，与 main.py 一致

        from .sdk_trim import trim_expired_contract_table
        api = TqApi(auth=TqAuth(username, password), debug=False, disable_print=True)
        trim_expired_contract_table(api)  # 期货行全留，结算价/合约资料表不受影响
        return api

    def _connect(self, symbols: list[str]):
        account = self._account or self.relay.pick()
        if account is None:
            raise RuntimeError("paper_market_source_unavailable")
        username, password = self.relay.credentials(account)
        if not username or not password:
            raise RuntimeError("tqkq_credentials_missing")
        api = self._api_factory(username, password)
        try:
            quotes = {symbol: api.get_quote(symbol) for symbol in symbols}
            self._fetch_calendar(api)
        except Exception:
            self._close_quietly(api)  # 换账号重连时不留半开的会话
            raise
        return api, quotes

    @staticmethod
    def _close_quietly(api: Any) -> None:
        try:
            api.close()
        except Exception:
            pass

    def _detach(self, symbol: str, quote: Any) -> dict[str, Any]:
        schedule = detach_trading_schedule(getattr(quote, "trading_time", None)) or {
            "day": [], "night": []}

        def value(name: str, default: float = 0.0) -> float:
            return float(getattr(quote, name, default))

        return {
            "symbol": symbol,
            "datetime": str(getattr(quote, "datetime", "") or ""),
            "last_price": value("last_price", float("nan")),
            "ask_price1": value("ask_price1", float("nan")),
            "bid_price1": value("bid_price1", float("nan")),
            "ask_volume1": value("ask_volume1"),
            "bid_volume1": value("bid_volume1"),
            "ins_class": str(getattr(quote, "ins_class", "") or ""),
            # 品种代码：官方 SimTrade 按品种对同品种多空取大边保证金
            # （trade_future._adjust_account_by_position）；丢掉这个字段会让
            # 锁仓保证金退化成两边相加（审查 527-2）。
            "product_id": str(getattr(quote, "product_id", "") or ""),
            "volume_multiple": value("volume_multiple"),
            "price_tick": value("price_tick"),
            "upper_limit": value("upper_limit", float("nan")),
            "lower_limit": value("lower_limit", float("nan")),
            "margin": value("margin", float("nan")),
            "commission": value("commission", float("nan")),
            "settlement": value("settlement", float("nan")),
            "pre_settlement": value("pre_settlement", float("nan")),
            "pre_close": value("pre_close", float("nan")),
            "trading_time": {"day": schedule["day"], "night": schedule["night"]},
            # TqSdk gives the expiry in seconds; the book compares nanoseconds. Read raw, every
            # contract looked long expired and production rejected every order (10-08).
            "expire_datetime": value("expire_datetime", float("nan")) * 1e9,
            "delivery_year": int(number(getattr(quote, "delivery_year", 0))),
            "delivery_month": int(number(getattr(quote, "delivery_month", 0))),
            "instrument_name": str(getattr(quote, "instrument_name", symbol) or symbol),
            "open": value("open", float("nan")),
            "highest": value("highest", float("nan")),
            "lowest": value("lowest", float("nan")),
            "volume": value("volume"),
            "open_interest": value("open_interest"),
        }

    def _feed_initial(self, engine: PaperEngine, quotes: dict[str, Any]) -> None:
        """Synchronous get_quote returns once the first snapshot has arrived, so that
        snapshot never shows up as a later `is_changing` — a closed market (holiday,
        night) would then never reach the engine (production 10-03: every quote read
        503 market_quote_not_ready). Feed what each new subscription already holds."""
        for symbol, quote in quotes.items():
            if str(getattr(quote, "datetime", "") or "").strip():
                engine.feed_quote(self._detach(symbol, quote))

    @staticmethod
    def _error_code(error: BaseException) -> str:
        message = str(error)
        if message in {"tqkq_credentials_missing", "paper_market_source_unavailable"}:
            return message
        return tqkq_error_code(error) if isinstance(error, Exception) else type(error).__name__

    def _run(self, engine: PaperEngine) -> None:
        api, quotes = None, {}
        cumulative: set[str] = set()
        connecting = False
        connected_at = 0.0
        while not self._stop.is_set():
            try:
                if api is None:
                    # 先消费变更再选账号：选完之后到连上之前清单若又变了，变更标记
                    # 留着，连上后的下一轮就会核对这个账号还在不在。
                    self.relay.take_change()
                    account = self.relay.pick()
                    if account is None:
                        # 清单里的账号全部在冷却（或清单为空）：没有行情连接，引擎照旧在
                        # 重连 60 秒后把账户数据降级为 unavailable；最早一个账号冷却结束再试。
                        engine.mark_source_reconnecting()
                        self._pause(min(self.relay.next_ready_in(), self.RETRY_SECONDS) or self.SWITCH_SECONDS)
                        continue
                    self._account = account
                    connecting = True
                    api, quotes = self._connect(sorted(engine.wanted_symbols()))
                    cumulative = set(quotes)
                    self._feed_initial(engine, quotes)
                    connecting = False
                    connected_at = time.monotonic()
                    self.relay.connected(account)
                    # 连接成功即结束重连窗口；断线进入 except 时记起点，引擎据此
                    # 只在 60 秒后把账户数据降级为 unavailable（审查 #465 第 7 条）。
                    engine.mark_source_connected()
                    continue
                if self.relay.take_change() and not self.relay.still_current(self._account):
                    # 当前账号被移出清单（或凭据变了）：关掉这条连接，按新清单重连。
                    leaving = self._account.account_id if self._account is not None else "-"
                    _log_line(f"paper market source leaving {leaving}: removed from the source list")
                    self._close_quietly(api)
                    api, quotes = None, {}
                    cumulative = set()
                    self._account = None
                    engine.mark_source_reconnecting()
                    continue
                wanted = engine.wanted_symbols()
                missing = [symbol for symbol in sorted(wanted) if symbol not in quotes]
                if missing:
                    if len(cumulative | set(missing)) <= self.MAX_PAPER_QUOTES:
                        for symbol in missing:
                            quotes[symbol] = api.get_quote(symbol)
                        cumulative |= set(missing)
                        self._feed_initial(engine, {symbol: quotes[symbol] for symbol in missing})
                    else:
                        # 超过上限：新建会话先订必要合约，就绪后切换、旧会话关闭。
                        new_api, new_quotes = self._connect(sorted(wanted))
                        old = api
                        api, quotes = new_api, new_quotes
                        cumulative = set(wanted)
                        self._feed_initial(engine, new_quotes)
                        threading.Thread(target=self._close_quietly, args=(old,),
                                         name="paper-quote-recycle", daemon=True).start()
                if self._calendar_refresh_due():
                    self._refresh_calendar(api)
                if self._settlement_query_due():
                    self._query_settlements(engine, api)
                    self._query_final_settlements(engine, api)
                if self._contract_refresh_due(engine):
                    self._refresh_contracts(engine, api)
                api.wait_update(deadline=time.time() + 0.5)
                if self._stop.is_set():
                    break
                for symbol, quote in list(quotes.items()):
                    if api.is_changing(quote):
                        engine.feed_quote(self._detach(symbol, quote))
            except Exception as error:
                account = self._account
                # 只认这条连接正要订阅的合约：报错点名的合约不在订阅集里时（报错格式变了、
                # 别的原因），按原来的连接故障处理，不在同一账号上空转重连。
                unknown = [name for name in nonexistent_instruments(error) if name in engine.wanted_symbols()]
                if unknown:
                    # 一个坏合约名不是行情源故障（2026-10-13）：合约服务答"不存在"时，TqSdk
                    # 的合约查询任务随之退出、这条会话不能再用，只能重连——但还是这个账号，
                    # 不记失败、不切账号；坏合约交给引擎剔出订阅集，其余合约重连后照常订阅。
                    engine.reject_symbols(unknown, self.relay.redact(f"{type(error).__name__}: {error}", account))
                    if api is not None:
                        self._close_quietly(api)
                    api, quotes = None, {}
                    cumulative = set()
                    connecting = False
                    self._account = None
                    engine.mark_source_reconnecting()
                    self._pause(self.SWITCH_SECONDS)
                    continue
                code = self._error_code(error)
                switching = False
                if account is not None:
                    switching = self.relay.failed(
                        account, code, connecting=connecting,
                        session_seconds=None if connecting else time.monotonic() - connected_at)
                detail = self.relay.redact(f"{type(error).__name__}: {str(error)[:200]}", account)
                name = account.account_id if account is not None else "-"
                _log_line(f"paper quote source retry ({name}, {code}"
                          f"{', switching account' if switching else ''}): {detail}")
                if api is not None:
                    self._close_quietly(api)
                api, quotes = None, {}
                cumulative = set()
                connecting = False
                self._account = None
                engine.mark_source_reconnecting()
                self._pause(self.SWITCH_SECONDS if switching else self.RETRY_SECONDS)
        if api is not None:
            self._close_quietly(api)


def _per_lot(value: Any) -> float | None:
    result = finite_number(value)
    return result if result is not None and result > 0 else None


_FEE_RATE_KEYS = ("open", "closeToday", "closeYesterday",
                  "openPerLotExtra", "closeTodayPerLotExtra", "closeYesterdayPerLotExtra")


def _valid_fee_rate(value: Any) -> dict[str, Any]:
    """feeRate 字段严格校验：只认 Runtime 费率模型的键，mode 只能 per_lot/notional，
    数值有限且 ≥0；越界一律 422，不悄悄丢字段。"""
    if not isinstance(value, dict) or value.get("mode") not in {"per_lot", "notional"}:
        raise ValueError("invalid_fee_rate")
    if set(value) - {"mode", *_FEE_RATE_KEYS}:
        raise ValueError("invalid_fee_rate")
    rate: dict[str, Any] = {"mode": str(value["mode"])}
    for key in _FEE_RATE_KEYS:
        number_value = finite_number(value.get(key, 0))
        if number_value is None or number_value < 0:
            raise ValueError("invalid_fee_rate")
        rate[key] = number_value
    return rate


class PaperHandler(ReadOnlyHandler):
    # The paper engine keeps one connection per request (keep-alive is the market sidecar's only).
    protocol_version = "HTTP/1.0"
    timeout = None

    # Account ids are the Runtime's `paper:<12 hex>` (2026-10-03): unlike order and
    # command ids they carry a colon, so they get their own pattern.
    @staticmethod
    def _account_identifier(value: Any) -> str:
        result = str(value) if isinstance(value, str) else ""
        if re.fullmatch(r"[A-Za-z0-9:_-]{8,64}", result) is None:
            raise ValueError("invalid_identifier")
        return result

    """与 sidecar 同一套鉴权与报文约定，路径前缀换成 /accounts/{id}。"""

    server: PaperServer

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "sidecar_auth_required"})
            return
        path = urlparse(self.path).path
        engine = self.server.engine
        try:
            if path == "/healthz":
                relay = self.server.market_sources
                health = {**engine.health(), "rejectedSymbols": engine.rejected_symbols()}
                if relay is not None:
                    health["marketSource"] = relay.status()
                self._json(HTTPStatus.OK, health)
                return
            parts = path.strip("/").split("/", 2)
            if len(parts) < 2 or parts[0] != "accounts":
                self._json(HTTPStatus.NOT_FOUND, {"error": "paper_route_not_found"})
                return
            account_id = self._account_identifier(unquote(parts[1]))
            rest = parts[2] if len(parts) > 2 else ""
            if not rest:
                self._json(HTTPStatus.OK, engine.account_info(account_id))
            elif rest == "v1/status":
                self._json(HTTPStatus.OK, engine.status(account_id))
            elif rest == "v1/account":
                self._json(HTTPStatus.OK, engine.account(account_id))
            elif rest == "v1/positions":
                self._json(HTTPStatus.OK, engine.positions(account_id))
            elif rest == "v1/activity":
                self._json(HTTPStatus.OK, engine.activity(account_id))
            elif rest == "v1/snapshot":
                self._json(HTTPStatus.OK, engine.combined(account_id))
            elif rest.startswith("v1/market/") and rest.endswith("/quote"):
                engine.account_info(account_id)  # 未知账户先 404，再谈合约
                symbol = unquote(rest[len("v1/market/"):-len("/quote")])
                # 等待首包超时由 engine 抛 RuntimeError("market_quote_not_ready")，
                # 外层统一映射 503，调用方重试即得（审查 #465 第 3 条）。
                self._json(HTTPStatus.OK, engine.market_quote(account_id, symbol))
            elif rest.startswith("v1/simulation/orders/") and not rest.endswith("/cancel"):
                client_order_id = self._identifier(
                    unquote(rest[len("v1/simulation/orders/"):]))
                self._json(HTTPStatus.OK, engine.query_order(account_id, client_order_id))
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "paper_route_not_found"})
        except UnknownSymbolError:
            # 与只读 sidecar 同一个答复：Runtime 的报价读取把它当"没有这个合约"。
            self._json(HTTPStatus.NOT_FOUND, {"error": "symbol_not_found"})
        except KeyError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "account_not_found"})
        except ValueError:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "invalid_paper_request"})
        except RuntimeError as error:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "sidecar_auth_required"})
            return
        path = urlparse(self.path).path
        engine = self.server.engine
        try:
            if path == "/accounts":
                self._json(HTTPStatus.OK, self._create_request())
                return
            if path == "/v1/snapshots":
                self._json(HTTPStatus.OK, engine.snapshots(self._snapshots_request()))
                return
            if path == "/v1/market-sources":
                # 行情账号清单（2026-10-12）：Runtime 经同一 Bearer 鉴权下发；口令只在
                # 内存，答复只有指纹与状态。回放模式没有快期连接，404。
                relay = self.server.market_sources
                if relay is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "paper_market_sources_unsupported"})
                    return
                revision = relay.configure(parse_sources(self._body()))
                self._json(HTTPStatus.OK, {"revision": revision, "marketSource": relay.status()})
                return
            prefix = "/accounts/"
            if not path.startswith(prefix):
                self._json(HTTPStatus.NOT_FOUND, {"error": "paper_route_not_found"})
                return
            account_id, _, rest = path[len(prefix):].partition("/")
            account_id = self._account_identifier(unquote(account_id))
            if rest == "reset":
                payload = self._body()
                if set(payload) != {"confirmAccountId"}:
                    raise ValueError("invalid_reset_fields")
                self._json(HTTPStatus.OK, engine.reset_account(account_id, str(payload["confirmAccountId"])))
                return
            if rest == "v1/simulation/orders/submit":
                self._json(HTTPStatus.OK, engine.submit(account_id, self._submit_request()))
                return
            marker = "v1/simulation/orders/"
            if rest.startswith(marker) and rest.endswith("/cancel"):
                client_order_id = self._identifier(
                    unquote(rest[len(marker):-len("/cancel")]))
                payload = self._body()
                if set(payload) != {"commandId"}:
                    raise ValueError("invalid_cancel_fields")
                command_id = self._identifier(payload["commandId"], command=True)
                self._json(HTTPStatus.OK, engine.cancel_order(
                    account_id, client_order_id, command_id))
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "paper_route_not_found"})
        except KeyError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "account_not_found"})
        except (json.JSONDecodeError, ValueError):
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "invalid_paper_request"})
        except RuntimeError as error:
            self._json(HTTPStatus.CONFLICT, {"error": str(error)})

    def _create_request(self) -> dict[str, Any]:
        payload = self._body()
        if set(payload) - {"accountId", "initBalance"}:
            raise ValueError("invalid_account_fields")
        account_id = self._account_identifier(payload.get("accountId"))
        balance = 1_000_000.0
        if "initBalance" in payload:
            value = finite_number(payload["initBalance"])
            if value is None or not 100_000 <= value <= 100_000_000:
                raise ValueError("invalid_init_balance")
            balance = value
        return self.server.engine.create_account(account_id, balance)

    def _snapshots_request(self) -> list[str]:
        payload = self._body()
        account_ids = payload.get("accountIds")
        if set(payload) != {"accountIds"} or not isinstance(account_ids, list) or len(account_ids) > 500:
            raise ValueError("invalid_snapshots_request")
        return [self._account_identifier(value) for value in account_ids]

    def _submit_request(self) -> dict[str, Any]:
        payload = self._body()
        required = {
            "commandId", "clientOrderId", "symbol", "direction", "offset",
            "volume", "priceType", "limitPrice",
        }
        # 只允许 required 或 required + feeRate：缺字段、多字段都 422（feeRate 是
        # 2026-10-08 新增的可选键，其余合同不变）。
        if set(payload) not in (required, required | {"feeRate"}):
            raise ValueError("invalid_submit_fields")
        command_id = self._identifier(payload["commandId"], command=True)
        client_order_id = self._identifier(payload["clientOrderId"])
        if payload["direction"] not in {"BUY", "SELL"}:
            raise ValueError("invalid_order_direction")
        if payload["offset"] not in {"OPEN", "CLOSE", "CLOSETODAY"}:
            raise ValueError("invalid_order_offset")
        if payload["priceType"] != "LIMIT":
            raise ValueError("limit_order_required")
        if (isinstance(payload["volume"], bool)
                or not isinstance(payload["volume"], int)
                or payload["volume"] <= 0):
            raise ValueError("invalid_volume")
        price = finite_number(payload["limitPrice"])
        if price is None or price <= 0:
            raise ValueError("invalid_limit_price")
        # 行情事实校验（涨跌停/价位/时段/资金）由引擎在撮合内核前完成，
        # 拒绝以错单形式返回，而不是 HTTP 4xx。
        result = {
            **payload,
            "commandId": command_id,
            "clientOrderId": client_order_id,
            "symbol": str(payload["symbol"]),
            "limitPrice": price,
        }
        if "feeRate" in payload:
            # 本站模拟盘的扣费费率（Runtime 按交易所标准/自定义解析后随单带来）。
            result["feeRate"] = _valid_fee_rate(payload["feeRate"])
        return result


class PaperServer(ThreadingHTTPServer):
    daemon_threads = True
    # listen backlog: the default 5 dropped connections when 100 accounts submitted at once
    # (test_paper_load http-100, 10-09: 106 connect timeouts, commands never reached the engine).
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], token: str, engine: PaperEngine,
                 market_sources: MarketSourceRelay | None = None) -> None:
        super().__init__(address, PaperHandler)
        self.token = token
        self.engine = engine
        self.market_sources = market_sources

    def server_bind(self) -> None:
        # 同 ReadOnlyServer：本地服务不做反向 DNS。
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


def _secret_value(file_env: str, plain_env: str) -> str | None:
    path = os.environ.get(file_env)
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read().strip()
            if content:
                return content
        except OSError:
            pass
    value = os.environ.get(plain_env)
    return value or None


def _paper_token() -> str:
    return (
        _secret_value("AI_TRADER_PAPER_TOKEN_FILE", "AI_TRADER_PAPER_TOKEN")
        or _secret_value("AI_TRADER_READONLY_SIDECAR_TOKEN_FILE", "AI_TRADER_READONLY_SIDECAR_TOKEN")
        or ""
    )


def _tqkq_credentials() -> tuple[str, str]:
    username = _secret_value("AI_TRADER_TQKQ_USERNAME_FILE", "AI_TRADER_TQKQ_USERNAME")
    password = _secret_value("AI_TRADER_TQKQ_PASSWORD_FILE", "AI_TRADER_TQKQ_PASSWORD")
    return username or "", password or ""


def _start_foreign(token: str) -> tuple[PaperEngine, Any, PaperServer] | None:
    """外盘本站模拟（paper_foreign.py，2026-10-19）：同一进程里的第二个引擎实例，自己的数据库与端口。
    只在配了 AI_TRADER_PAPER_FOREIGN_PORT 且 MT5 行情桥地址、令牌齐全时启动；起不来只记日志，
    国内引擎照常服务。外盘实例故障停机时只它自己答不可用（不触发整进程退出重启）。"""
    port_text = (os.environ.get("AI_TRADER_PAPER_FOREIGN_PORT") or "").strip()
    if not port_text:
        return None
    from .paper_foreign import NEW_YORK, ExchangeMargins, Mt5PaperSource, bridge_settings, cme_trading_dates

    bridge = bridge_settings()
    if bridge is None:
        _log_line("paper foreign engine not started: mt5_bridge_not_configured")
        return None
    engine: PaperEngine | None = None
    source: Any = None
    try:
        port = int(port_text)
        db_path = os.environ.get("AI_TRADER_PAPER_FOREIGN_DB", "/data/paper-foreign.sqlite3")
        engine = PaperEngine(
            db_path=db_path,
            clock=lambda: datetime.now(NEW_YORK), trading_dates=cme_trading_dates,
            stale_after_seconds=float(os.environ.get("AI_TRADER_PAPER_STALE_SECONDS", "15")),
            timezone=NEW_YORK,
        )
        engine.open()
        engine.start()
        # 交易所隔夜保证金表（10-21）：存在外盘库旁边，重启照用。
        margins = ExchangeMargins(os.path.join(os.path.dirname(db_path) or ".", "foreign-margins.json"))
        source = Mt5PaperSource(*bridge, margins=margins)
        source.start(engine)
        server = PaperServer(("127.0.0.1", port), token, engine, None)
    except Exception as error:
        # 端口写错、被占或库打不开：只外盘不起，已起的部分收掉，国内引擎照常服务。
        _log_line(f"paper foreign engine not started: {type(error).__name__}: {str(error)[:200]}")
        if source is not None:
            source.stop()
        if engine is not None and engine._opened:
            engine.close()
        return None
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                     name="paper-foreign-http", daemon=True).start()
    print(json.dumps({"status": "ready", "port": int(server.server_address[1]),
                      "service": PAPER_SERVICE, "market": "foreign", "liveTrading": "disabled"}), flush=True)
    return engine, source, server


def serve() -> int:
    port = int(os.environ.get("AI_TRADER_PAPER_PORT", str(DEFAULT_PAPER_PORT)))
    db_path = os.environ.get("AI_TRADER_PAPER_DB", DEFAULT_PAPER_DB)
    token = _paper_token()
    if not token or len(token) < 32:
        print(json.dumps({"status": "failed", "error": "paper_token_missing"}),
              file=sys.stderr, flush=True)
        return 2
    clock: Callable[[], datetime]
    source: RecordedQuoteSource | TqQuoteSource
    trading_dates: Callable[[], set[str]] | set[str] | None
    market_sources: MarketSourceRelay | None
    recorded = os.environ.get("AI_TRADER_PAPER_RECORDED")
    if recorded:
        clock = VirtualClock(datetime.now(CHINA_TIMEZONE))
        source = RecordedQuoteSource(path=recorded)
        trading_dates = None
        market_sources = None
    else:
        clock = lambda: datetime.now(CHINA_TIMEZONE)  # noqa: E731
        tq_source = TqQuoteSource(_tqkq_credentials)
        source = tq_source
        market_sources = tq_source.relay
        trading_dates = tq_source.trading_dates
    engine = PaperEngine(
        db_path=db_path, clock=clock, trading_dates=trading_dates,
        stale_after_seconds=float(os.environ.get("AI_TRADER_PAPER_STALE_SECONDS", "15")),
        # 成交模型（2026-10-13）：默认真实模式；AI_TRADER_PAPER_FILL_MODE=ideal 退回理想成交。
        fill_model=FillModel.from_env(os.environ),
    )
    engine.open()
    engine.start()
    source.start(engine)
    server = PaperServer(("127.0.0.1", port), token, engine, market_sources)
    foreign = _start_foreign(token)
    stop = threading.Event()

    def shutdown(*_: Any) -> None:
        if not stop.is_set():
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    def exit_on_fatal() -> None:
        # A fatal actor (527-3) leaves the engine refusing every command; exiting lets the
        # container restart policy bring it back from the last committed SQLite state instead
        # of staying unavailable until someone restarts it by hand. The grace lets in-flight
        # requests get their paper_engine_failed answer first.
        while not stop.wait(1.0):
            if engine._failed():
                time.sleep(2.0)
                failed.set()
                shutdown()
                return

    failed = threading.Event()
    threading.Thread(target=exit_on_fatal, name="paper-fatal-watch", daemon=True).start()
    print(json.dumps({
        "status": "ready", "port": int(server.server_address[1]),
        "service": PAPER_SERVICE, "liveTrading": "disabled",
    }), flush=True)
    caught_up = False
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        source.stop()
        caught_up = engine.close()
        server.server_close()
        if foreign is not None:
            foreign_engine, foreign_source, foreign_server = foreign
            foreign_server.shutdown()
            foreign_source.stop()
            caught_up = foreign_engine.close() and caught_up
            foreign_server.server_close()
    if failed.is_set():
        return 3
    if not caught_up:
        # 停机没能把检查点追平（actor 未在时限内退出、合并失败或核对不过）：旧版本读
        # state_json 会读到落后的账本。非 0 退出让部署/运维看得见，回滚前先跑
        # --checkpoint-db（#527/#510）。
        print(json.dumps({"status": "failed", "error": "paper_checkpoint_not_caught_up"}),
              file=sys.stderr, flush=True)
        return 4
    return 0


def _normalized_export(book: PaperBook) -> str:
    # NaN（如 SimTrade 恒为 NaN 的 ctp_balance）按 JSON 文本比较才相等。
    return json.dumps(book.export_state(), sort_keys=True)


def check_db(db_path: str) -> dict[str, Any]:
    """部署前预演（只在生产库的副本上跑，副本会被写）：按新版本开库——迁移、
    恢复、自检——逐户与原整本快照按旧路径恢复的结果逐项比对；正常停机写检查点
    后再开一次，验证迁移幂等、重启一致。挂起的到期结算记录随开库解析（坏记录
    开库即失败），pending 的合约须仍有持仓；旧库没有这张表时开库建空表。引擎正在
    用的库有文件锁，打不开。"""
    originals: dict[str, tuple[float, str]] = {}
    adopted: set[str] = set()
    probe = sqlite3.connect(db_path)
    try:
        tables = {str(row[0]) for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {str(row[1]) for row in probe.execute("PRAGMA table_info(paper_accounts)")}
        heads = ({str(row[0]) for row in probe.execute("SELECT account_id FROM paper_account_heads")}
                 if "paper_account_heads" in tables else set())
        marker = "checkpoint_of" if "checkpoint_of" in columns else "NULL"
        for account_id, init_balance, state_json, revision, checkpoint_of in probe.execute(
                f"SELECT id, init_balance, state_json, revision, {marker} FROM paper_accounts").fetchall():
            originals[str(account_id)] = (float(init_balance), str(state_json))
            if _checkpoint_newer(revision, checkpoint_of, str(account_id) in heads):
                adopted.add(str(account_id))  # 这次开库会从整本快照迁移，快照就是对照基准
    finally:
        probe.close()
    mismatches: list[str] = []
    exports: dict[str, str] = {}
    engine = PaperEngine(db_path=db_path)
    engine.open()
    try:
        for account_id, (init_balance, state_json) in originals.items():
            restored = engine._accounts[account_id].book
            exports[account_id] = _normalized_export(restored)
            if account_id not in adopted:
                continue  # 已迁移的账户：快照列只是检查点，不是对照基准
            legacy = PaperBook(account_id=account_id, init_balance=init_balance,
                               clock=engine._clock, trading_dates=engine._calendar)
            legacy.restore_state(json.loads(state_json))
            if _normalized_export(legacy) != exports[account_id]:
                mismatches.append(f"{account_id}: migrated state differs from the snapshot")
        assert engine._db is not None
        trades = engine._db.execute("SELECT count(*) FROM paper_trades").fetchone()[0]
        # 挂起的到期结算（#527 最终结算日协议）：记录已随开库解析（坏记录开库即失败）；
        # pending 的合约账本里必须还有持仓，否则挂起记录与账本对不上。
        expiry_open = 0
        for account_id, entry in engine._accounts.items():
            held = engine._held_symbols(entry)
            for symbol, record in entry.expiry.items():
                expiry_open += 1
                if record["status"] == "pending" and symbol not in held:
                    mismatches.append(f"{account_id}: pending expiry {symbol} has no position")
    finally:
        engine.close()
    again = PaperEngine(db_path=db_path)
    again.open()
    try:
        for account_id, exported in exports.items():
            if _normalized_export(again._accounts[account_id].book) != exported:
                mismatches.append(f"{account_id}: second open differs")
        assert again._db is not None
        # 第一次正常停机写的检查点（第二次开库还没写任何东西）：没追平就是停机路径
        # 有问题，回滚后的旧版本会读到落后快照——判不一致，不只报个数。
        lagging = checkpoint_lag(again._db)
        checkpoints_current = len(originals) - len(lagging)
        mismatches.extend(f"{account_id}: checkpoint not caught up after a clean close"
                          for account_id in lagging)
    finally:
        again.close()
    return {
        "status": "ok" if not mismatches else "mismatch",
        "accounts": len(originals),
        "migratedFromSnapshot": len(adopted),
        "tradeRows": int(trades),
        "checkpointsCurrent": int(checkpoints_current),
        "expirySettlementsOpen": expiry_open,
        "mismatches": mismatches,
    }


def checkpoint_db(db_path: str) -> dict[str, Any]:
    """回滚前合并检查点（引擎须已停；deploy-release.sh 回滚起旧引擎前的门）：按新版本
    开库再正常关闭，然后另开连接逐户核对 revision = 当前状态行 = checkpoint_of。
    全部追平才 ok，旧版本读 state_json 就是最后已提交状态；否则 not_caught_up，
    回滚应停下而不是起旧引擎。"""
    engine = PaperEngine(db_path=db_path)
    engine.open()
    accounts = len(engine._accounts)
    caught_up = engine.close()
    probe = sqlite3.connect(db_path)
    try:
        lagging = checkpoint_lag(probe)
    finally:
        probe.close()
    ok = caught_up and not lagging
    return {"status": "ok" if ok else "not_caught_up", "accounts": accounts, "lagging": lagging}


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Trader 本站模拟盘撮合引擎")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--check-db", metavar="PATH",
                        help="在库副本上预演迁移+恢复+自检并逐户比对（会写这份副本）")
    parser.add_argument("--checkpoint-db", metavar="PATH",
                        help="引擎停止后合并检查点，供回滚到旧版本前使用")
    arguments = parser.parse_args()
    if not (arguments.serve or arguments.check_db or arguments.checkpoint_db):
        parser.print_usage()
        return 2
    try:
        if arguments.check_db:
            report = check_db(arguments.check_db)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            return 0 if report["status"] == "ok" else 1
        if arguments.checkpoint_db:
            report = checkpoint_db(arguments.checkpoint_db)
            print(json.dumps(report), flush=True)
            return 0 if report["status"] == "ok" else 1
        return serve()
    except PaperMigrationError as error:
        # 迁移失败 fail-closed：引擎不启动，原快照列未改写；原因写清楚便于处理。
        print(json.dumps({"status": "failed", "error": "paper_migration_failed",
                          "detail": str(error)}, ensure_ascii=False), file=sys.stderr, flush=True)
        return 2
    except Exception as error:
        print(json.dumps({"status": "failed", "error": type(error).__name__}),
              file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

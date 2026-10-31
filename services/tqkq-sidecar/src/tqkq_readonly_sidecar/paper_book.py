"""本站模拟盘的单账户账本（2026-10-03，所有者决定放弃逐用户快期账号后引入）。

记账核心直接复用 TqSdk 官方本地模拟盘的 ``SimTrade``（与 TqSim/回测同一份纯计算，
不联网）。本模块只做这些事：

* 注入两个钩子，修掉 SimTrade 自带的时间判断缺陷（行情静止时放行盘后委托、不认节假日）：
  ``get_trade_timestamp`` 用注入的时钟，``is_in_trading_time`` 用时钟 + 交易日历 + 合约时段；
* 下单前置校验（NaN 保证金、期权、涨跌停、最小变动价位、交割月、到期）——SimTrade 遇到
  NaN 保证金会让资金全变 NaN，必须先在门口拒单；
* 下单瞬间即可成交的限价单按对手方最优价撮合（SimTrade 原生一律按自身限价，与交易所
  连续竞价"新到可成交单按对手方申报价成交"口径不符）——这是“理想模式”；
* “真实模式”（2026-10-13，生产默认，模型在 paper_fill.py）：关掉 SimTrade 自己的撮合，
  委托经时间差到达后按一档盘口量吃单（超量按冲击模型逐档变差）、剩余按排队模型被动
  成交；每笔（含部分）成交拆成子单交给 SimTrade 的整笔成交记账，账目口径不变；
* 非交易时段行情只更新价格不撮合、结算前按结算价标记并自动平掉到期持仓；
* 把 SimTrade 的 dict 状态导出/导入为可 JSON 往返的整份账本。

字段形状转换复用 main.py 的生产转换函数（``_activity_position/_activity_order/
_activity_trade/_order_report``），本模块的 *View 只补 SimTrade dict 没有的计算字段。
"""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime, timedelta, tzinfo
from datetime import time as clock_time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .cme_hours import periods as cme_periods
from .main import (
    CHINA_TIMEZONE,
    OfficialTqKqSession,
    finite_number,
    number,
    trading_day_from_calendar,
)
from .paper_fill import (
    IDEAL,
    MODE_IDEAL,
    FillModel,
    decision_price,
    join_queue,
    queue_step,
    taker_plan,
)

# 撤单类 last_msg 片段：FINISHED 且未成交但属于正常撤单，不算错单。
_CANCEL_MARKS = ("已撤单", "撤销")

# 这些交易所的 CLOSE 在 SimTrade 里只平昨仓（CLOSETODAY 单独平今）。
CLOSE_YESTERDAY_ONLY_EXCHANGES = frozenset({"SHFE", "INE"})

# 中金所股指期货现金交割，自然人客户可正常持有、交易交割月合约（含当月合约）；
# 其余期货品种实物交割——商品期货自然人不得持仓进入交割月（大商所/郑商所/广期所
# 交割办法，上期所/能源中心个别品种除外但同样不得交割），中金所国债期货自然人须
# 提前申报国债托管账户才能持仓进入交割月（本站模拟不建模申报）。这些品种维持
# 交割月禁开；品种按交易所 + 合约目录的 product_id 判断，不写死合约代码。
CFFEX_CASH_SETTLED_PRODUCTS = frozenset({"IF", "IH", "IC", "IM"})


def fee_rate_leg(rate: dict[str, Any], key: str, price: float, multiplier: float,
                 volume: int) -> float:
    """一条费率栏（open / closeToday / closeYesterday）的手续费：notional 是万分之数 ×
    成交价 × 合约乘数 × 手数 / 10000，加每手加收 × 手数，保留 2 位——与 Runtime 的
    trade_fee 同公式（sidecar 不能 import runtime，测试里两边对拍）。"""
    amount = Decimal(str(rate.get(key) or 0)) * Decimal(str(volume))
    if str(rate.get("mode")) == "notional":
        amount = amount * Decimal(str(price)) * Decimal(str(multiplier)) / Decimal(10000)
    amount += Decimal(str(rate.get(f"{key}PerLotExtra") or 0)) * Decimal(str(volume))
    return float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

# 快照版本（审查 527-11）：restore 拒绝比自己新的版本，旧一版/无版本（老快照）照收。
# 2：有部分成交的活单（volume_left < volume_orign，剩余按影子单记账，2026-10-13 真实
# 成交模型）。只有这种快照写 2，其余仍写 1：回滚到不认识部分成交的旧版本时，带部分
# 成交活单的账户拒绝恢复（引擎停机报错），而不是按原始手数撤单/结算出一本糊涂账；
# 没有这种活单的账户旧版本照常读（审查 P1，2026-10-13）。
BASE_STATE_VERSION = 1
STATE_VERSION = 2
# 同一笔行情的已耗盘口量随快照落盘（#834）：新增键，不改版本号。82a441df 与更早的
# 读者不认识它、照常恢复，只是回到修复前“重启丢预算”的行为，账目不受影响。
CONSUMED_KEY = "fill_consumed"
# 持仓明细的写者证明（#932 B2，2026-10-19）：快照顶层记“写这份明细时账户的持久成交
# 序号”{"from": 当日第一笔成交的序号（paper_trades 的 trades_from_seq）, "seq": from +
# 当日成交笔数}——跨日单调、只随成交增长。恢复时 seq 与 from + 快照里的当日成交笔数
# 不等、from 与库里已提交的起点不等（或缺这个键）就丢弃全部明细，下一笔成交按桶成本
# 惰性重建；带着 from，检查点整本快照单独恢复也能自证。所有旧版本的 export_state 都按固定键重建顶层 dict，不会原样保留它：
# 回滚到明细之前的版本再升级回来，旧写者写过这个账户就一定缺键——哪怕它在桶内
# 平了又开、让手数与桶成本恰好复原（ABA）。写在持仓 dict 里的标记会被旧版本原样
# 带回来，所以不放那里。不改版本号：旧版本读到不认识的顶层键照常恢复。
LOTS_TRADE_SEQ_KEY = "open_lots_trade_seq"
# 委托 dict 上的资料表来源标注（#777 review，回执 contractSource）：委托里多一个键，
# 不改版本号——SimTrade 与旧版本都按名取字段，不认识的键原样保留，只是旧版本的回执
# 不带 contractSource。
CONTRACT_SOURCE_KEY = "paper_contract_source"
# 强平 (#1204, 2026-10-30): the engine's own close orders when an account's risk ratio reaches 100%; Runtime reads the
# prefix as the order's 来源 (强平).
FORCE_ORDER_PREFIX = "paper-force-"


class _AllUsed(dict):
    """来源未知的已耗量（#834 修复前的快照没有记录）：按这笔行情的盘口全部用尽算，
    等可证明的新行情（时间变了）再重新供应——宁可少成交，也不凭同一笔恢复的行情
    把停机前已吃掉的量再吃一遍。"""

    def get(self, key: Any, default: Any = None) -> float:
        return math.inf

# 合约资料合同（#527 B2）：官方 SimTrade 按每手保证金 × 手数、乘数直接算持仓与账户，
# 行情源偶发缺值（_detach 缺字段给 NaN/0）若原样合并进内核，NaN 会污染持仓/账户
# 保证金并落盘，重启时整个引擎 paper_state_corrupt。取值顺序只有一处——引擎行情
# 入口按合约用 resolve_contract_metadata：实时行情的有效值优先（并回写引擎 SQLite
# 的合约资料表 paper_contracts）→ 资料表 → 都没有就保持缺失，下单由 pre_check 拒绝。
# 撮合内核必需的三项：缺任何一项有效值的合约不交给内核，只更新价格（apply_quote 守卫）。
KERNEL_METADATA = ("margin", "commission", "volume_multiple")
# 每手保证金/手续费（随交易所调整，资料表单独记来源与更新时间）。
FEE_METADATA = ("margin", "commission")
# 合约资料表承担的全部字段。
CONTRACT_METADATA = (
    "ins_class", "product_id", "volume_multiple", "price_tick", "expire_datetime",
    "delivery_year", "delivery_month", "margin", "commission",
)


def valid_contract_field(name: str, value: Any) -> bool:
    """资料字段是否有效：非有限、≤0（手续费 <0）、空串、交割月不在 1–12 都算缺失。"""
    if name in ("product_id", "ins_class"):
        return isinstance(value, str) and bool(value.strip())
    parsed = finite_number(value)
    if parsed is None:
        return False
    if name == "delivery_month":
        return 1 <= parsed <= 12
    return parsed >= 0 if name == "commission" else parsed > 0


def resolve_contract_metadata(quote: dict[str, Any],
                              record: dict[str, Any] | None) -> tuple[dict[str, Any], set[str]]:
    """实时行情 + 合约资料表记录 → (逐项取有效值的行情, 由资料表补上的字段)。
    价格类字段不动；实时值有效时永远用实时值。"""
    resolved = dict(quote)
    filled: set[str] = set()
    if record:
        for name in CONTRACT_METADATA:
            if (not valid_contract_field(name, resolved.get(name))
                    and valid_contract_field(name, record.get(name))):
                resolved[name] = record[name]
                filled.add(name)
    return resolved, filled


def kernel_metadata_ready(quote: dict[str, Any]) -> bool:
    return all(valid_contract_field(name, quote.get(name)) for name in KERNEL_METADATA)


# 恢复自检（#527 F3）覆盖的 SimTrade 数值字段：都由有限输入算出，健康账本恒为有限数。
# ctp_balance / ctp_available 官方恒为 NaN，future_margin 是空仓时可为 NaN 的辅助量，不查。
_SANE_ACCOUNT_FIELDS = (
    "balance", "available", "margin", "static_balance", "pre_balance", "float_profit",
    "position_profit", "close_profit", "commission", "frozen_margin", "frozen_commission",
)
_SANE_POSITION_FIELDS = (
    "margin", "margin_long", "margin_short", "float_profit", "float_profit_long",
    "float_profit_short", "position_profit", "position_profit_long", "position_profit_short",
    "open_cost_long", "open_cost_short", "position_cost_long", "position_cost_short",
)


class AttrView:
    """dict 的属性视图：getattr(view, "last_price") 读 dict 键，extras 覆盖别名。"""

    __slots__ = ("_data", "_extra")

    def __init__(self, data: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_extra", extra or {})

    def __getattr__(self, name: str) -> Any:
        extra = object.__getattribute__(self, "_extra")
        if name in extra:
            return extra[name]
        data = object.__getattribute__(self, "_data")
        try:
            return data[name]
        except KeyError as error:
            raise AttributeError(name) from error


def position_view(position: dict[str, Any]) -> AttrView:
    """SimTrade 持仓 dict → snapshot() 消费的 TqSdk 形状：pos_* 是昨+今的合计字段。"""
    return AttrView(position, {
        "pos_long": position.get("volume_long", 0),
        "pos_short": position.get("volume_short", 0),
        "pos_long_today": position.get("volume_long_today", 0),
        "pos_long_his": position.get("volume_long_his", 0),
        "pos_short_today": position.get("volume_short_today", 0),
        "pos_short_his": position.get("volume_short_his", 0),
    })


def order_view(order: dict[str, Any]) -> AttrView:
    """SimTrade 委托 dict → _order_report 消费的形状：补 is_online/is_dead/is_error。"""
    finished = str(order.get("status")) == "FINISHED"
    untouched = int(order.get("volume_left") or 0) == int(order.get("volume_orign") or 0)
    message = str(order.get("last_msg") or "")
    cancelled = any(mark in message for mark in _CANCEL_MARKS)
    return AttrView(order, {
        "is_online": str(order.get("status")) == "ALIVE",
        "is_dead": finished,
        "is_error": bool(finished and untouched and not cancelled),
    })


def _period_seconds(value: Any) -> int | None:
    parts = str(value or "").split(":")
    if len(parts) > 3:
        return None
    try:
        units = [int(part) for part in parts] + [0] * (3 - len(parts))
    except ValueError:
        return None
    return units[0] * 3600 + units[1] * 60 + units[2]


def quote_moment(quote: dict[str, Any]) -> datetime | None:
    """行情时间（交易所本地时间）→ 带时区 datetime；缺失或无法解析返回 None。"""
    raw = str(quote.get("datetime") or "").strip()
    if not raw:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=CHINA_TIMEZONE)
        except ValueError:
            continue
    return None


def in_trading_session(now: datetime, schedule: Any, trading_dates: set[str],
                       tz: tzinfo = CHINA_TIMEZONE) -> bool:
    """墙上（或注入）时钟 + 交易日历 + 合约时段是否在交易时段内；引擎与账本共用
    同一判断（结算价记录、兜底截止时刻都按它定）。节假日、非交易时段一律 False。

    夜盘归属下一交易日：交易日 T 的夜盘物理上从上一交易日 T-1 的 21:00 开始，
    跨零点的时段结束时间落在 T 的自然日上——与 tqsdk._get_trading_timestamp 的
    锚定方式一致，但日历用真实交易日（认节假日）而不是"18 点换日 + 周末顺延"。

    外盘（paper_foreign.py）：时间按引擎本地时区 ``tz``（美东）读；时段表是 ``{"cmeHours": 种类}`` 时按
    ``cme_hours`` 取该交易日的时段（含节假日）；带 ``"nightAnchor": "eve"`` 的旧式时段表夜盘锚定交易日的
    前一个自然日。
    """
    schedule = schedule if isinstance(schedule, dict) else {}
    try:
        trading_day = trading_day_from_calendar(now, trading_dates, tz)
    except RuntimeError:
        return False  # 日历不可用：宁可拒单，不放行盘后委托
    now = now.astimezone(tz)
    kind = schedule.get("cmeHours")
    if isinstance(kind, str):
        # 外盘（2026-10-21）：CME 各品种当天的时段，单位是距交易日 0 点（引擎时区）的分钟，可以跨好几个自然日
        # （周一假日的交易日从周日 18:00 开始）。
        minutes = ((now.date() - date.fromisoformat(trading_day)).days * 1440
                   + now.hour * 60 + now.minute + now.second / 60)
        return any(start <= minutes < end for start, end in cme_periods(kind, trading_day))
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    if now.date().isoformat() == trading_day:
        for period in schedule.get("day") or []:
            bounds = [_period_seconds(part) for part in period]
            if len(bounds) == 2 and None not in bounds and bounds[0] <= seconds < bounds[1]:
                return True
    if schedule.get("nightAnchor") == "eve":
        previous: str | None = (date.fromisoformat(trading_day) - timedelta(days=1)).isoformat()
    else:
        previous = _previous_trading_date_of(trading_day, trading_dates)
    if previous is None:
        return False
    anchor = datetime.strptime(previous, "%Y-%m-%d").date()
    for period in schedule.get("night") or []:
        bounds = [_period_seconds(part) for part in period]
        if len(bounds) != 2 or None in bounds:
            continue
        start = datetime.combine(anchor, clock_time(), tzinfo=tz) + timedelta(seconds=bounds[0])
        end = datetime.combine(anchor, clock_time(), tzinfo=tz) + timedelta(
            seconds=bounds[1] + (86400 if bounds[1] <= bounds[0] else 0))
        if start <= now < end:
            return True
    return False


def _previous_trading_date_of(trading_day: str, trading_dates: set[str]) -> str | None:
    earlier = [value for value in trading_dates if value < trading_day]
    return max(earlier) if earlier else None


class PaperBook:
    """一个模拟账户 = 一份 SimTrade 状态。所有方法只在引擎的单线程 actor 上调用。"""

    def __init__(self, *, account_id: str, init_balance: float, clock: Callable[[], datetime],
                 trading_dates: Callable[[], set[str]] | set[str],
                 fill_model: FillModel | None = None, timezone: tzinfo = CHINA_TIMEZONE) -> None:
        from tqsdk.tradeable.sim.trade_future import SimTrade  # 惰性导入，与 main.py 一致

        self.account_id = account_id
        self._tz = timezone  # 交易日与时段按引擎本地时间（国内北京，外盘美东）
        self.init_balance = float(init_balance)
        self._clock = clock
        self._trading_dates = trading_dates if callable(trading_dates) else (lambda: set(trading_dates))
        self._force_trading_time = False
        # 成交模型（2026-10-13，paper_fill.py）：不传 = 理想模式（SimTrade 原行为）；
        # 生产入口 serve() 默认真实模式。真实模式下 SimTrade 自己的撮合关闭，由本账本
        # 的外层撮合决定每一笔成交，再拆成子单交给 SimTrade 的整笔成交记账。
        self.fill_model = fill_model or IDEAL
        self._native_match = not self.fill_model.realistic
        # 理想模式的成交在下单当下发生 = 吃单；之后由行情触发 = 挂单被动成交。
        self._native_liquidity = "maker"
        # 同一笔行情里本账户已吃掉的盘口量：合约 → (行情时间, {价格: 手数})。随快照
        # 落盘、恢复（#834）：与成交、活单在同一事务提交，重启后同一笔行情不再供应。
        self._consumed: dict[str, tuple[str, dict[float, float]]] = {}
        # 外层撮合改过状态（到达、排队、成交）且尚未落盘：引擎据此决定是否写一次。
        self._match_dirty = False
        # 最近一次恢复是否因写者证明不成立丢弃了持仓明细（#932 B2，引擎记日志用）。
        self.lots_dropped = False
        # 真实日历未就绪时账本日留空：不用周末兜底规则定日，日历就绪后由引擎
        # 推进到当前交易日（审查 #465 第 2 条）。
        try:
            book_day = trading_day_from_calendar(clock(), self._trading_dates(), timezone)
        except RuntimeError:
            book_day = ""
        self.book_trading_day = book_day
        self._sim = SimTrade(
            "sim", account_id=account_id, init_balance=self.init_balance,
            get_trade_timestamp=lambda: int(clock().timestamp() * 1e9),
            is_in_trading_time=self._sim_is_in_trading_time,
        )
        # 带费率的单按 feeRate 计佣金（2026-10-08：本站模拟盘按交易所标准/自定义费率扣费，
        # 不再用行情源每手手续费）。费率随 SimTrade 的 order dict 走，快照导出/导入自然
        # 带着它；不带 feeRate 的单保持 SimTrade 原逻辑。
        original_generate_trade = self._sim._generate_trade

        def _generate_trade_with_fee_rate(order: dict[str, Any], quote: dict[str, Any],
                                          price: float) -> dict[str, Any]:
            trade = original_generate_trade(order, quote, price)
            rate = order.get("feeRate")
            if isinstance(rate, dict):
                trade["commission"] = self._fee_rate_commission(rate, order, quote, price)
            if isinstance(order.get("paper_fill"), dict):
                # SimTrade 自己撮合的成交（理想模式）也带对账字段；真实模式的拆分成交
                # 由 _fill_part 覆盖成完整口径。
                trade["fill"] = self._fill_audit(order, quote, float(price),
                                                 int(order["volume_left"]),
                                                 liquidity=self._native_liquidity, impact_ticks=0)
            return trade

        self._sim._generate_trade = _generate_trade_with_fee_rate

        # 昨/今两桶成本（开仓成本 2026-10-09 #662；持仓成本与平仓盈亏 2026-10-12）：
        # SimTrade 平仓一律按全持仓混合均价扣减 open_cost/position_cost，并按混合
        # 持仓价算平仓盈亏——昨仓成本混进今仓开仓均价（10-09 rb2701 剩 1 手今仓
        # 显示昨结价 3080），平今的平仓盈亏也被昨仓结算价拖走（10-12 夜盘 au 平今
        # 实赚 +100 显示 −2630，剩余昨仓持仓价漂到 903.985 而非结算价 907.78）。
        # 这里在每笔成交后按实际被平的那桶（上期所/能源中心按 CLOSE/CLOSETODAY
        # 指令，其他交易所与 SimTrade 实际扣减一致先昨后今）扣减，桶内按持仓明细
        # 先开先平（2026-10-17 #932，与期货公司 CTP 逐笔持仓明细同口径）：开仓价＝
        # 剩余明细开仓价的加权均价；平仓盈亏按交易所逐日盯市口径（平今对被平那几笔
        # 的开仓价、平昨对昨结算价）；持仓盈亏＝各桶相对各自成本。平仓盈亏与持仓
        # 盈亏只互相挪差额，权益、可用资金不动。浮盈派生量随新开仓成本同步重算（#666）。
        original_on_order_traded = self._sim._on_order_traded

        def _on_order_traded_with_cost_buckets(order, trade, symbol, position,
                                               quote, underlying_quote):
            side = (("long" if str(order["direction"]) == "BUY" else "short")
                    if str(order["offset"]) == "OPEN"
                    else ("short" if str(order["direction"]) == "BUY" else "long"))
            pre_today = int(position.get(f"volume_{side}_today") or 0)
            pre_his = int(position.get(f"volume_{side}_his") or 0)
            multiple = float(quote["volume_multiple"])
            # 惰性初始化必须在原始记账之前：老账本只有成交前的桶成本或混合成本，
            # 按当时的昨/今手数建明细才不把本笔成交错摊进桶里。
            self._open_lots(position, side, "today", pre_today, multiple)
            self._open_lots(position, side, "his", pre_his, multiple)
            self._init_position_cost_buckets(position, side, pre_today, pre_his)
            pre_position_cost = float(position.get(f"position_cost_{side}") or 0.0)
            original_on_order_traded(order, trade, symbol, position, quote, underlying_quote)
            removed_today = self._rebook_open_lots(position, side, float(trade["price"]),
                                                   pre_today, pre_his, multiple)
            self._rebucket_position_cost(position, quote, side, float(trade["price"]),
                                         pre_today, pre_his, pre_position_cost, removed_today)
            self._remember_open_fee_rate(position, side, order)

        # 部分成交（2026-10-13）：SimTrade 只会整笔成交，它的成交/撤单记账一律按
        # volume_orign 扣冻结与持仓。真实模式把部分成交拆成子单记账后，母单保留
        # 原始 volume_orign（报告里的委托手数），volume_left 是剩余；之后无论是
        # SimTrade 自己整笔成交剩余（理想模式重启接手）还是撤单/日终撤销，都按
        # “手数 = 剩余”的影子单记账，再把冻结与剩余写回母单。
        def _on_order_traded_partial_safe(order, trade, symbol, position, quote, underlying_quote):
            shadow = self._remainder_shadow(order)
            if shadow is None:
                return _on_order_traded_with_cost_buckets(order, trade, symbol, position,
                                                          quote, underlying_quote)
            _on_order_traded_with_cost_buckets(shadow, trade, symbol, position, quote, underlying_quote)
            self._copy_back(order, shadow)

        original_on_order_failed = self._sim._on_order_failed

        def _on_order_failed_partial_safe(symbol, order):
            shadow = self._remainder_shadow(order)
            if shadow is None:
                return original_on_order_failed(symbol, order)
            original_on_order_failed(symbol, shadow)
            self._copy_back(order, shadow)

        self._sim._on_order_traded = _on_order_traded_partial_safe
        self._sim._on_order_failed = _on_order_failed_partial_safe

        # SimTrade 自己的撮合（下单即撮合、每笔行情撮合）只在理想模式与到期强平时运行。
        original_match_order = self._sim._match_order

        def _match_order_gated(order, symbol, position, quote, underlying_quote=None):
            if self._native_match:
                original_match_order(order, symbol, position, quote, underlying_quote)

        self._sim._match_order = _match_order_gated

    @staticmethod
    def _remainder_shadow(order: dict[str, Any]) -> dict[str, Any] | None:
        left = int(order.get("volume_left") or 0)
        if left == int(order.get("volume_orign") or 0):
            return None
        return {**order, "volume_orign": left}

    @staticmethod
    def _copy_back(order: dict[str, Any], shadow: dict[str, Any]) -> None:
        for key in ("volume_left", "frozen_margin", "frozen_premium"):
            order[key] = shadow[key]

    @staticmethod
    def _remember_open_fee_rate(position: dict[str, Any], side: str,
                                order: dict[str, Any]) -> None:
        """开仓成交把这张单的 feeRate 记到持仓该方向（#643/#527 PR-3）：到期强平
        没有用户请求可带费率，按最近一笔开仓的费率口径收平仓费，而不是行情源
        每手手续费。开仓单不带 feeRate（行情手续费口径）时清掉记录，保持同一
        口径；该方向平到 0 也清掉，免得旧费率留给下一次开仓。随持仓 dict 进快照。"""
        key = f"fee_rate_{side}"
        if str(order["offset"]) == "OPEN":
            rate = order.get("feeRate")
            if isinstance(rate, dict):
                position[key] = dict(rate)
            else:
                position.pop(key, None)
        elif not (int(position.get(f"volume_{side}_today") or 0)
                  + int(position.get(f"volume_{side}_his") or 0)):
            position.pop(key, None)

    def _fee_rate_commission(self, rate: dict[str, Any], order: dict[str, Any],
                             quote: dict[str, Any], price: float) -> float:
        """订单的 feeRate → 这笔成交的佣金。开仓 open；上期所/能源中心 CLOSE=平昨、
        CLOSETODAY=平今；其他交易所的 CLOSE 按 SimTrade 的真实平仓顺序（先昨后期，
        与 _on_insert_order 的冻结分配同一分支）拆成昨/今两段分别计费。"""
        volume = int(order["volume_left"])
        multiplier = float(quote["volume_multiple"])
        offset = str(order["offset"])
        exchange = str(order["exchange_id"])
        if offset == "OPEN":
            return fee_rate_leg(rate, "open", price, multiplier, volume)
        if offset == "CLOSETODAY":
            return fee_rate_leg(rate, "closeToday", price, multiplier, volume)
        if exchange in CLOSE_YESTERDAY_ONLY_EXCHANGES:
            return fee_rate_leg(rate, "closeYesterday", price, multiplier, volume)
        position = self._sim._positions.get(f"{exchange}.{order['instrument_id']}") or {}
        side = "short" if str(order["direction"]) == "BUY" else "long"
        frozen_his = int(position.get(f"volume_{side}_frozen_his") or 0)
        his = min(volume, frozen_his)
        total = fee_rate_leg(rate, "closeYesterday", price, multiplier, his)
        if volume > his:
            total += fee_rate_leg(rate, "closeToday", price, multiplier, volume - his)
        return total

    # -- 开仓成本：按笔持仓明细，桶内先开先平 ------------------------------------
    #
    # 每个方向的今仓 / 昨仓各一份明细 open_lots_{side}_{today|his} = [[开仓价, 手数], …]，
    # 先开的在前（2026-10-17 #932）。开仓成本、开仓均价都从明细推导，这是唯一的
    # 责任入口；open_cost_{side}_{today|his} 只是明细之和的镜像，随明细一起写——
    # 回滚到明细之前的版本时旧代码仍按它读出正确的桶成本。

    @staticmethod
    def _lots_cost(lots: list[list[float]], multiple: float) -> float:
        return sum(price * volume for price, volume in lots) * multiple

    @staticmethod
    def _valid_lots(raw: Any, volume: int, cost: float | None,
                    multiple: float) -> list[list[float]] | None:
        """明细与桶对得上（手数之和＝桶手数，有镜像成本时成本之和＝镜像）才用；
        结构坏、对不上（旧版本改过桶成本而没改明细）返回 None 由调用方重建。"""
        if not isinstance(raw, list):
            return None
        lots: list[list[float]] = []
        for item in raw:
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                return None
            price, lot_volume = finite_number(item[0]), finite_number(item[1])
            if price is None or lot_volume is None or lot_volume <= 0 \
                    or lot_volume != int(lot_volume):
                return None
            lots.append([price, int(lot_volume)])
        if sum(lot_volume for _, lot_volume in lots) != volume:
            return None
        if cost is not None and not math.isclose(PaperBook._lots_cost(lots, multiple), cost,
                                                 rel_tol=1e-9, abs_tol=1e-6):
            return None
        return lots

    @staticmethod
    def _store_lots(position: dict[str, Any], side: str, bucket: str,
                    lots: list[list[float]], multiple: float) -> None:
        # 每次写新列表、不就地改：快照导出/恢复只浅拷贝持仓 dict。
        position[f"open_lots_{side}_{bucket}"] = [[price, volume] for price, volume in lots]
        position[f"open_cost_{side}_{bucket}"] = PaperBook._lots_cost(lots, multiple)

    def _open_lots(self, position: dict[str, Any], side: str, bucket: str,
                   volume: int, multiple: float) -> list[list[float]]:
        """读一桶（today / his）的持仓明细。明细缺失或与桶对不上时按桶重建成一条
        （价＝桶均价、量＝桶手数）：有桶成本键（2026-10-09 起的快照）取它，更老的
        账本把 SimTrade 的混合 open_cost 按昨今手数比例摊开。重建不改开仓均价，
        之后每笔成交按明细先开先平演进；不回改历史成交。"""
        cost = finite_number(position.get(f"open_cost_{side}_{bucket}"))
        lots = self._valid_lots(position.get(f"open_lots_{side}_{bucket}"), volume, cost, multiple)
        if lots is None:
            if cost is None:
                total_volume = int(position.get(f"volume_{side}") or 0)
                total_cost = float(position.get(f"open_cost_{side}") or 0.0)
                cost = total_cost * volume / total_volume if total_volume else 0.0
            lots = [[cost / volume / multiple, volume]] if volume > 0 else []
            self._store_lots(position, side, bucket, lots, multiple)
        return lots

    @staticmethod
    def _take_first_lots(lots: list[list[float]],
                         volume: int) -> tuple[list[list[float]], float]:
        """先开先平扣 volume 手：返回剩余明细与被平那几笔的 Σ开仓价×手数。"""
        remaining: list[list[float]] = []
        taken = 0.0
        for price, lot_volume in lots:
            used = min(volume, lot_volume)
            taken += price * used
            volume -= used
            if lot_volume > used:
                remaining.append([price, lot_volume - used])
        return remaining, taken

    def _rebook_open_lots(self, position: dict[str, Any], side: str, trade_price: float,
                          pre_today: int, pre_his: int, multiple: float) -> float:
        """一笔成交后改明细并回写 SimTrade 的 open_cost/open_price（展示、浮盈、保本
        “移到成本”与合并卡都读它）。开仓按成交价追加到今仓明细末尾；平仓从实际
        被平的那桶按先开先平扣。返回平掉的今仓开仓成本（持仓成本今仓桶同额扣减）。"""
        post_today = int(position.get(f"volume_{side}_today") or 0)
        post_his = int(position.get(f"volume_{side}_his") or 0)
        buckets = {"today": (pre_today, post_today), "his": (pre_his, post_his)}
        removed_today = 0.0
        total = 0.0
        for bucket, (pre, post) in buckets.items():
            lots = self._open_lots(position, side, bucket, pre, multiple)
            if post > pre:
                # 昨仓桶只在防御路径增加（结算迁移不产生成交）；同价相邻合并成一条。
                if lots and lots[-1][0] == trade_price:
                    lots = [*lots[:-1], [trade_price, lots[-1][1] + post - pre]]
                else:
                    lots = [*lots, [trade_price, post - pre]]
            elif post < pre:
                lots, taken = self._take_first_lots(lots, pre - post)
                if bucket == "today":
                    removed_today = taken * multiple
            self._store_lots(position, side, bucket, lots, multiple)
            total += position[f"open_cost_{side}_{bucket}"]
        volume = post_today + post_his
        position[f"open_cost_{side}"] = total
        position[f"open_price_{side}"] = total / volume / multiple if volume else float("nan")
        self._sync_side_float_profit(position, side, multiple)
        return removed_today

    def _sync_side_float_profit(self, position: dict[str, Any], side: str,
                                multiple: float) -> None:
        """按剩余开仓成本口径重算该方向浮盈（#666）。SimTrade 平仓时按旧混合
        均价比例扣减 float_profit_{side}，后续行情只按 position.last_price 的
        价格差额增量，开仓价被分桶重写后浮盈永远不会自己跟上。这里用当时的
        估值价（position.last_price）× 剩余 open_cost 一次性置准，账户
        float_profit 同步差额——它不进 balance/available，权益口径不动。
        之后行情仍以 last_price 为基准加差额，不会重复计入或漏计。"""
        volume = int(position.get(f"volume_{side}") or 0)
        if volume:
            last = finite_number(position.get("last_price"))
            if last is None:
                return  # 估值价缺失：不动增量基准，等下一笔行情/成交再同步
            value = last * volume * multiple - float(position.get(f"open_cost_{side}") or 0.0)
            if side == "short":
                value = -value
        else:
            value = 0.0
        previous = float(position.get(f"float_profit_{side}") or 0.0)
        if value == previous:
            return
        position[f"float_profit_{side}"] = value
        position["float_profit"] = (float(position.get("float_profit_long") or 0.0)
                                    + float(position.get("float_profit_short") or 0.0))
        self._sim._account["float_profit"] += value - previous

    # -- 持仓成本分桶与平仓盈亏 ------------------------------------------------

    @staticmethod
    def _init_position_cost_buckets(position: dict[str, Any], side: str,
                                    today_volume: int, his_volume: int) -> None:
        """持仓成本两桶（position_cost_{side}_today / _his）的惰性初始化，在成交的
        原始记账之前做。今仓的持仓成本恒等于今仓开仓成本（都按成交价入桶、按同一
        份明细先开先平扣减），昨仓桶取 SimTrade 持仓成本的其余部分——两桶之和等于现有
        position_cost，初始化不改持仓价。结算后（今仓为 0）昨仓桶正好是结算价 ×
        手数；本次修复上线前已被混合扣减带偏的昨仓成本留在昨仓桶里，下一次结算
        重置到结算价后自然收敛，不回改历史。"""
        today_key, his_key = f"position_cost_{side}_today", f"position_cost_{side}_his"
        if today_key in position and his_key in position:
            return
        total = float(position.get(f"position_cost_{side}") or 0.0)
        if not his_volume:
            today = total
        elif not today_volume:
            today = 0.0
        else:
            today = float(position.get(f"open_cost_{side}_today") or 0.0)
        position[today_key] = today
        position[his_key] = total - today

    def _rebucket_position_cost(self, position: dict[str, Any], quote: dict[str, Any],
                                side: str, trade_price: float, pre_today: int, pre_his: int,
                                pre_position_cost: float, removed_today: float) -> None:
        """一笔成交后重记持仓成本并改正平仓盈亏（交易所逐日盯市口径）。

        开仓按成交价入今仓桶；平仓从实际被平的桶（按成交前后昨/今手数判定，
        与 SimTrade 的扣减一致）扣减：今仓扣被平那几笔明细的开仓成本
        （removed_today，先开先平），昨仓每手都是昨结算价，按手数比例扣与先开
        先平同值。SimTrade 已按混合持仓价记了
        平仓盈亏、按比例扣了持仓盈亏：两者的偏差是同一个数 delta——平仓盈亏
        加 delta、持仓盈亏减 delta，权益与可用资金不变。该方向平到 0 时混合与
        分桶扣减的是同一份总成本，delta 恒为 0，不引入浮点残差。"""
        multiple = float(quote["volume_multiple"])
        post_today = int(position.get(f"volume_{side}_today") or 0)
        post_his = int(position.get(f"volume_{side}_his") or 0)
        today_key, his_key = f"position_cost_{side}_today", f"position_cost_{side}_his"
        today_cost = float(position[today_key])
        his_cost = float(position[his_key])
        removed = 0.0
        closed = False
        if post_today > pre_today:
            today_cost += trade_price * (post_today - pre_today) * multiple
        elif post_today < pre_today and pre_today:
            closed = True
            taken = today_cost if post_today == 0 else removed_today
            today_cost -= taken
            removed += taken
        if post_his > pre_his:
            his_cost += trade_price * (post_his - pre_his) * multiple  # 防御：正常路径不发生
        elif post_his < pre_his and pre_his:
            closed = True
            taken = his_cost if post_his == 0 else his_cost / pre_his * (pre_his - post_his)
            his_cost -= taken
            removed += taken
        if post_today == 0:
            today_cost = 0.0
        if post_his == 0:
            his_cost = 0.0
        position[today_key] = today_cost
        position[his_key] = his_cost
        volume = post_today + post_his
        mixed_cost = float(position.get(f"position_cost_{side}") or 0.0)
        position[f"position_cost_{side}"] = today_cost + his_cost
        position[f"position_price_{side}"] = \
            (today_cost + his_cost) / volume / multiple if volume else float("nan")
        if not closed or volume == 0:
            return
        mixed_removed = pre_position_cost - mixed_cost
        delta = mixed_removed - removed if side == "long" else removed - mixed_removed
        if delta == 0.0:
            return
        account = self._sim._account
        account["close_profit"] += delta
        account["position_profit"] -= delta
        position[f"position_profit_{side}"] = \
            float(position.get(f"position_profit_{side}") or 0.0) - delta
        position["position_profit"] = (float(position.get("position_profit_long") or 0.0)
                                       + float(position.get("position_profit_short") or 0.0))

    def _carry_open_cost_buckets(self) -> None:
        """结算今仓转昨仓：今仓明细按原顺序接到昨仓明细之后（开仓价不变，先开先平的
        次序保留），在到期强平之前做，强平的扣减落在昨仓明细上。明细与桶成本对不上
        （或缺）时按合并后的桶成本重建成一条；没有分桶键的老持仓不动，留给下次成交
        惰性初始化。持仓成本两桶直接丢掉：SimTrade.settle 已把整份持仓成本重置为
        结算价 × 手数（全是昨仓），下一笔成交按“今仓 0、昨仓＝全部”惰性重建。"""
        for symbol, position in self._sim._positions.items():
            multiple = finite_number((self._sim._quotes.get(symbol) or {}).get("volume_multiple"))
            for side in ("long", "short"):
                today_key, his_key = f"open_cost_{side}_today", f"open_cost_{side}_his"
                today_lots = position.pop(f"open_lots_{side}_today", None)
                his_lots = position.pop(f"open_lots_{side}_his", None)
                if today_key in position or his_key in position:
                    merged = (float(position.pop(today_key, 0.0))
                              + float(position.get(his_key, 0.0)))
                    position[his_key] = merged
                    volume = int(position.get(f"volume_{side}") or 0)
                    if multiple is not None and multiple > 0:
                        joined = ([*his_lots, *today_lots]
                                  if isinstance(his_lots, list) and isinstance(today_lots, list)
                                  else None)
                        lots = self._valid_lots(joined, volume, merged, multiple)
                        if lots is None:
                            lots = [[merged / volume / multiple, volume]] if volume > 0 else []
                        self._store_lots(position, side, "his", lots, multiple)
                position.pop(f"position_cost_{side}_today", None)
                position.pop(f"position_cost_{side}_his", None)

    # -- 时间判断 ------------------------------------------------------------

    def trading_day(self) -> str:
        try:
            return trading_day_from_calendar(self._clock(), self._trading_dates(), self._tz)
        except RuntimeError:
            return self.book_trading_day

    def is_in_trading_time(self, quote: dict[str, Any]) -> bool:
        """墙上（或注入）时钟 + 交易日历 + 合约时段。节假日、非交易时段一律 False。

        判断本体是模块级 ``in_trading_session``：引擎记录结算价、判断收盘兜底的
        截止时刻，与账本撮合用同一份逻辑。
        """
        if self._force_trading_time:
            return True
        schedule = quote.get("trading_time") if isinstance(quote, dict) else None
        return in_trading_session(self._clock(), schedule, self._trading_dates(), self._tz)

    def _sim_is_in_trading_time(self, quote: dict[str, Any]) -> bool:
        return self.is_in_trading_time(quote)

    # -- 行情 ----------------------------------------------------------------

    def apply_quote(self, quote: dict[str, Any]) -> list[dict[str, Any]]:
        """行情推进：交易时段内撮合并更新账目；非交易时段只更新价格，挂单原样保留。
        跨日结算在引擎层先行（结算会撤掉昨日 GFD 单），本方法不再感知日边界。
        非期货（期权/组合）不进撮合内核：SimTrade 会断言标的行情，而本引擎在门口
        拒单不会持有它们，行情只入缓存供校验读价。

        合约资料已由引擎行情入口按“实时有效值 → 资料表”补齐（#527 B2）；仍缺
        保证金/手续费/乘数的合约只更新价格、不交给内核，下单由 pre_check 明确拒绝。"""
        symbol = str(quote["symbol"])
        payload = dict(quote)
        pack = {"quotes": {symbol: payload}}
        future = str(payload.get("ins_class") or "") == "FUTURE"
        if future and not kernel_metadata_ready(payload):
            self._sim._quotes[symbol] = payload
            return []
        if future and self.is_in_trading_time(quote):
            _, events = self._sim.update_quotes(symbol, pack)
            if not self._native_match:
                self._match_live(symbol)
                _, more = self._sim._return_results()
                events = list(events) + list(more)
            return self._finished(events)
        live = self._sim._orders.get(symbol)
        parked = dict(live) if live else None
        try:
            if parked is not None:
                self._sim._orders[symbol] = {}
            if future:
                _, events = self._sim.update_quotes(symbol, pack)
            else:
                self._sim._quotes[symbol] = payload
                events = []
        finally:
            # 恢复放 finally（审查 527-6）：update_quotes 抛异常也不能把挂单丢在门外。
            if parked is not None:
                if parked:
                    self._sim._orders[symbol] = parked
                else:
                    self._sim._orders.pop(symbol, None)
        return self._finished(events)

    @staticmethod
    def _finished(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(event) for event in events if str(event.get("status")) == "FINISHED"]

    def inject_quote(self, quote: dict[str, Any]) -> None:
        """把引擎缓存的最新行情放进账本，供下单校验读价；不撮合、不更新账目。
        用于索引外的账户首次下单一瞬间（审查 #465 第 1 条）。"""
        self._sim._quotes[str(quote["symbol"])] = dict(quote)

    # -- 下单 / 撤单 ----------------------------------------------------------

    def pre_check(self, request: dict[str, Any]) -> str | None:
        """SimTrade 门口的引擎自有校验；返回拒单理由，None 表示放行。"""
        symbol = str(request["symbol"])
        quote = self._sim._quotes.get(symbol)
        if quote is None or not quote.get("datetime"):
            return "未收到指定合约行情，暂不能下单"
        if str(quote.get("ins_class") or "") != "FUTURE":
            return "不支持的合约类型，本站模拟盘目前只支持期货（期权、组合已拒单）"
        expire_ns = finite_number(quote.get("expire_datetime"))
        if expire_ns is None:
            return "合约到期时间暂无来源（实时行情与合约资料表都没有），拒绝下单"
        if expire_ns <= int(self._clock().timestamp() * 1e9):
            return "合约已到期或即将到期，拒绝下单"
        if not all(valid_contract_field(name, quote.get(name)) for name in FEE_METADATA):
            return "合约保证金或手续费暂无来源（实时行情与合约资料表都没有有效值），拒绝下单"
        multiple = finite_number(quote.get("volume_multiple"))
        if multiple is None or multiple <= 0:
            return "合约乘数无效（缺失或非有限数），拒绝下单"
        tick = finite_number(quote.get("price_tick"))
        upper = finite_number(quote.get("upper_limit"))
        lower = finite_number(quote.get("lower_limit"))
        price = finite_number(request.get("limitPrice"))
        if tick is None or tick <= 0 or upper is None or lower is None or price is None:
            return "合约涨跌停或最小变动价位信息无效，拒绝下单"
        steps = price / tick
        if abs(steps - round(steps)) > 1e-8:
            return "委托价格不在最小变动价位上"
        if not lower <= price <= upper:
            return "委托价格超出涨跌停范围"
        if str(request.get("offset")) == "OPEN":
            try:
                current = trading_day_from_calendar(self._clock(), self._trading_dates(), self._tz)
            except RuntimeError:
                return "交易日历不可用，暂不能开新仓"
            year = int(finite_number(quote.get("delivery_year")) or 0)
            month = int(finite_number(quote.get("delivery_month")) or 0)
            exchange_id = str(quote.get("exchange_id") or symbol.split(".", 1)[0])
            product_id = str(quote.get("product_id") or "").strip()
            if not product_id:
                product_id = symbol.split(".", 1)[-1].rstrip("0123456789")
            cash_settled = exchange_id == "CFFEX" and product_id in CFFEX_CASH_SETTLED_PRODUCTS
            if not cash_settled and not (year > 0 and 1 <= month <= 12):
                # 缺交割年月就判断不了是否已进交割月（#527 B2c）：宁可拒开仓。
                return "合约交割年月信息缺失，暂不能开新仓（平仓不受限）"
            if (not cash_settled
                    and (year, month) <= (int(current[:4]), int(current[5:7]))):
                return "个人账户不得进入交割月开仓，请移仓换月（平仓不受限）"
        return None

    def _counterparty_fill_price(self, symbol: str, pack: dict[str, Any]) -> float | None:
        """下单瞬间即可成交的限价单的对手方最优价（交易所连续竞价口径）。

        连续竞价按价格优先、时间优先撮合，新到的可成交限价单以对手方最优申报价
        成交：买单按卖一、卖单按买一，不劣于自身限价（10-08 双账本对照里快期模拟
        按 892.74、本站按自身限价 892.72，少 1 跳）。TqSdk SimTrade 原生对限价单一律
        按自身限价成交（trade_base.match_order），本站在门口改写撮合价；只在对手价
        严格更优时改写，等于对手价时原生行为已一致。挂单等待后续行情的被动成交、
        涨跌停无对手盘、到期强平（标记价即对手价）都不受影响。多档盘口按对手一档价
        整单一次成交，与 SimTrade 的全成口径一致。
        """
        quote = self._sim._quotes.get(symbol) or {}
        price = finite_number(pack.get("limit_price"))
        if price is None:
            return None
        if str(pack.get("direction")) == "BUY":
            counter = finite_number(quote.get("ask_price1"))
            hit = counter is not None and price >= counter
        else:
            counter = finite_number(quote.get("bid_price1"))
            hit = counter is not None and price <= counter
        return counter if hit and counter != price else None

    def submit(self, request: dict[str, Any], *,
               contract_source: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """下单。返回 (HTTP 报告, 最终委托状态)；校验不过时合成 FINISHED 错单，不进 SimTrade。
        contract_source：引擎给的“保证金/手续费来自资料表”标注，随委托 dict 进快照与终态
        委托行（#777 review），查询与幂等重放重建的回执里同样带 contractSource。"""
        extra = {CONTRACT_SOURCE_KEY: dict(contract_source)} if contract_source else {}
        symbol = str(request["symbol"])
        exchange_id, instrument_id = symbol.split(".", 1)
        order_id = str(request["clientOrderId"])
        volume = int(request["volume"])
        price = float(request["limitPrice"])
        rejection = self.pre_check(request)
        if rejection is not None:
            final = {
                "user_id": self.account_id, "order_id": order_id, "exchange_order_id": order_id,
                "exchange_id": exchange_id, "instrument_id": instrument_id,
                "direction": str(request["direction"]), "offset": str(request["offset"]),
                "volume_orign": volume, "volume_left": volume, "price_type": "LIMIT",
                "limit_price": price, "frozen_margin": 0.0, "frozen_premium": 0.0,
                "last_msg": rejection, "status": "FINISHED",
                "insert_date_time": int(self._clock().timestamp() * 1e9), **extra,
            }
            return self.order_report(final), final
        pack = {
            "aid": "insert_order", "order_id": order_id, "user_id": self.account_id,
            "exchange_id": exchange_id, "instrument_id": instrument_id,
            "direction": str(request["direction"]), "offset": str(request["offset"]),
            "price_type": "LIMIT", "limit_price": price, "volume": volume, **extra,
        }
        if request.get("feeRate") is not None:
            # Runtime 带来的生效费率（交易所标准或自定义）；整份请求已持久化在
            # paper_commands.request_json，费率随 order dict 进快照，重启重建同费。
            pack["feeRate"] = request["feeRate"]
        # 撮合状态随 order dict 进快照（2026-10-13）：下单时刻的参照价、到达时刻、排队。
        pack["paper_fill"] = self._decision_state(symbol, str(request["direction"]))
        if not self._native_match:
            return self._submit_realistic(symbol, order_id, pack)
        improved = self._counterparty_fill_price(symbol, pack)
        if improved is not None:
            pack = {**pack, "limit_price": improved}
        self._native_liquidity = "taker"
        try:
            _, events = self._sim.insert_order(symbol, pack)
        finally:
            self._native_liquidity = "maker"
        if improved is not None:
            # 委托记录仍显示用户限价；成交价是改善后的对手价（在 trades 里）。
            for event in events:
                if str(event.get("order_id")) == order_id:
                    event["limit_price"] = price
            for orders in self._sim._orders.values():
                live = orders.get(order_id)
                if live is not None:
                    live["limit_price"] = price
        final = dict(events[-1]) if events else dict(self.live_order(order_id) or {})
        return self.order_report(final), final

    # -- 真实模式的外层撮合（2026-10-13，模型见 paper_fill.py） -----------------

    def _now_ns(self) -> int:
        return int(self._clock().timestamp() * 1e9)

    def _decision_state(self, symbol: str, direction: str) -> dict[str, Any]:
        """下单时刻的撮合状态：参照价（对手一档价）与到达时刻。理想模式到达即下单时刻。"""
        quote = self._sim._quotes.get(symbol) or {}
        now = self._now_ns()
        price = decision_price(direction, quote)
        realistic = not self._native_match
        return {
            "model": self.fill_model.mode,
            "decisionAt": now,
            "decisionPrice": price,
            "arrivalAt": now + (self.fill_model.latency_ns if realistic else 0),
            "arrived": not realistic,
            "arrivedAt": None if realistic else now,
            "arrivalPrice": None if realistic else price,
            "queueAhead": None,
            "queueAheadAtJoin": None,
            "lastVolume": None,
        }

    def _submit_realistic(self, symbol: str, order_id: str,
                          pack: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """真实模式下单：SimTrade 照常做门口校验与冻结（资金不足、平仓手数不足当场
        拒单），但不撮合；委托在下单时刻 + 时间差后才“到达”。时间差为 0 时当场按
        当前盘口到达——与 _match_live 同一道守卫：当前这笔行情的已耗量未知（#834 前的
        快照恢复的那笔）时不按它到达，留到下一笔新行情按吃单到达（#837 Codex P2）。"""
        _, events = self._sim.insert_order(symbol, pack)
        live = self._sim._orders.get(symbol, {}).get(order_id)
        if live is not None and self.fill_model.latency_ns == 0:
            current = self._sim._quotes[symbol]
            if not self._budget_unknown(symbol, current):
                self._arrive(live, symbol, current)
        _, more = self._sim._return_results()
        events = list(events) + list(more)
        live = self._sim._orders.get(symbol, {}).get(order_id)
        if live is not None:
            final = dict(live)
        else:
            final = next((dict(event) for event in reversed(events)
                          if str(event.get("order_id")) == order_id), {})
        return self.order_report(final), final

    def _consumed_at(self, symbol: str, quote: dict[str, Any]) -> dict[float, float]:
        """本账户在这一笔行情上已吃掉的盘口量；新行情（时间变了）重新计。"""
        stamp = str(quote.get("datetime") or "")
        current = self._consumed.get(symbol)
        if current is None or current[0] != stamp:
            current = (stamp, {})
            self._consumed[symbol] = current
        return current[1]

    def _budget_unknown(self, symbol: str, quote: dict[str, Any]) -> bool:
        """这笔行情的已耗量来自 #834 之前的快照、无从得知：这笔行情上整笔不撮合——
        委托不按它到达（到达了也吃不到量，只会被当成挂单）、已到达的挂单也不排队或
        穿价成交，等下一笔新行情再撮合。"""
        current = self._consumed.get(symbol)
        return (current is not None and isinstance(current[1], _AllUsed)
                and current[0] == str(quote.get("datetime") or ""))

    def _match_live(self, symbol: str, *, sweep_only: bool = False) -> None:
        """一笔行情上的外层撮合：未到达的委托到了到达时刻就按这笔行情到达（吃单 →
        剩余排队）；已到达的挂单按排队模型被动成交。sweep_only=True 是行情静止时的
        兜底：只让超过等待上限的委托按缓存盘口到达，不推进排队。"""
        orders = self._sim._orders.get(symbol)
        if not orders:
            return
        quote = self._sim._quotes.get(symbol)
        if quote is None:
            return
        if self._budget_unknown(symbol, quote):
            # 已耗量未知的恢复行情（#834 前的快照）：整笔不撮合——不到达、不排队，也不走
            # 穿价分支把剩余全部成交（审查 P1，2026-10-13）；等行情时间变了再按新行情撮合。
            return
        now = self._now_ns()
        for order_id in list(orders):
            order = orders.get(order_id)
            if order is None or str(order.get("status")) != "ALIVE":
                continue
            state = order.get("paper_fill")
            if not isinstance(state, dict) or state.get("model") == MODE_IDEAL:
                # 理想模式下的挂单（重启改成真实模式后接手）：视为已到达、尚未排上。
                state = {**self._decision_state(symbol, str(order["direction"])),
                         "arrived": True, "arrivedAt": now}
                order["paper_fill"] = state
                self._match_dirty = True
            if not state.get("arrived"):
                due = int(state["arrivalAt"]) + (self.fill_model.arrival_wait_ns if sweep_only else 0)
                if now >= due:
                    self._arrive(order, symbol, quote)
                continue
            if not sweep_only:
                self._queue_fill(order, symbol, quote)

    def sweep_arrivals(self, max_quote_age_seconds: float = 15.0) -> list[dict[str, Any]]:
        """行情静止兜底（引擎在别的合约行情或空闲时调用）：到达时刻 + 等待上限已过、
        仍没有新行情的委托，按缓存里的最新盘口到达。只在交易时段内，且缓存行情不旧于
        max_quote_age_seconds（引擎的行情过期阈值）：重启后或行情源断流时缓存的是几分钟
        前的盘口，不能拿它成交，委托留到下一笔新行情再到达（审查 P1，2026-10-13）。"""
        now = self._clock()
        for symbol in list(self._sim._orders):
            quote = self._sim._quotes.get(symbol)
            if quote is None or not self.is_in_trading_time(quote):
                continue
            moment = quote_moment(quote)
            if moment is None or (now - moment).total_seconds() > max_quote_age_seconds:
                continue
            if str(quote.get("ins_class") or "") != "FUTURE" or not kernel_metadata_ready(quote):
                continue
            self._match_live(symbol, sweep_only=True)
        _, events = self._sim._return_results()
        return self._finished(events)

    def pending_arrival(self) -> bool:
        """有还没到达撮合的委托（引擎据此决定要不要做静止兜底）。"""
        return any(
            isinstance(order.get("paper_fill"), dict) and not order["paper_fill"].get("arrived")
            for orders in self._sim._orders.values() for order in orders.values())

    def take_match_dirty(self) -> bool:
        dirty, self._match_dirty = self._match_dirty, False
        return dirty

    def _tick_of(self, quote: dict[str, Any]) -> float:
        tick = finite_number(quote.get("price_tick"))
        return tick if tick is not None and tick > 0 else 0.0

    def _arrive(self, order: dict[str, Any], symbol: str, quote: dict[str, Any]) -> None:
        """委托到达：对手价在限价以内的部分按盘口量与冲击模型吃单，剩余按限价排队。"""
        state = order["paper_fill"]
        direction = str(order["direction"])
        limit = float(order["limit_price"])
        tick = self._tick_of(quote)
        state["arrived"] = True
        state["arrivedAt"] = self._now_ns()
        state["arrivalPrice"] = decision_price(direction, quote)
        self._match_dirty = True
        if tick <= 0:
            return
        consumed = self._consumed_at(symbol, quote)
        plan = taker_plan(direction, limit, int(order["volume_left"]), quote, tick,
                          self.fill_model.impact_ticks, consumed)
        for price, volume, impact in plan:
            consumed[price] = consumed.get(price, 0.0) + volume
            self._fill_part(order, symbol, quote, price, volume, liquidity="taker", impact_ticks=impact)
            if str(order.get("status")) != "ALIVE":
                return
        ahead = join_queue(direction, limit, quote, tick)
        state["queueAhead"] = ahead
        state["queueAheadAtJoin"] = ahead
        state["lastVolume"] = finite_number(quote.get("volume"))

    def _queue_fill(self, order: dict[str, Any], symbol: str, quote: dict[str, Any]) -> None:
        state = order["paper_fill"]
        tick = self._tick_of(quote)
        if tick <= 0:
            return
        limit = float(order["limit_price"])
        consumed = self._consumed_at(symbol, quote)
        had_queue = state.get("queueAhead") is not None
        step = queue_step(str(order["direction"]), limit, int(order["volume_left"]),
                          state.get("queueAhead"), state.get("lastVolume"), quote, tick,
                          self.fill_model.queue_share,
                          consumed_at_limit=consumed.get(limit, 0.0))
        state["queueAhead"] = step.queue_ahead
        state["lastVolume"] = step.last_volume
        if not had_queue and step.queue_ahead is not None:
            # 原来看不到本价位，此刻排上：记下排队尾时的队前量（落盘一次）。
            state["queueAheadAtJoin"] = step.queue_ahead
            self._match_dirty = True
        if step.fill > 0:
            if not step.crossed:
                consumed[limit] = consumed.get(limit, 0.0) + step.fill
            self._fill_part(order, symbol, quote, limit, step.fill, liquidity="maker", impact_ticks=0)

    def _fill_part(self, order: dict[str, Any], symbol: str, quote: dict[str, Any], price: float,
                   volume: int, *, liquidity: str, impact_ticks: int) -> None:
        """从母单切出 volume 手按 price 成交：子单（同 order_id、手数 = volume、分到
        按比例的冻结保证金）走 SimTrade 自己的整笔成交记账——持仓、保证金、手续费
        （含 feeRate 与昨/今分段）、平仓盈亏、昨/今成本分桶全部是原路径；母单剩余
        与冻结同步扣减，全部成交时按 SimTrade 的终态（全部成交、移出活单）收尾。
        平仓单的持仓冻结在母单下单时已按 SimTrade 规则占好，子单成交按同一份冻结扣减。
        成交编号沿用 SimTrade 的“委托号|成交前剩余手数”，每笔唯一。"""
        sim = self._sim
        order_id = str(order["order_id"])
        left = int(order["volume_left"])
        volume = min(int(volume), left)
        if volume <= 0:
            return
        frozen = float(order.get("frozen_margin") or 0.0)
        share = frozen if volume == left else frozen * volume / left
        child = {**order, "volume_orign": volume, "volume_left": volume,
                 "frozen_margin": share, "frozen_premium": 0.0}
        position = sim._ensure_position(symbol, quote, None)
        trade = sim._generate_trade(child, quote, price)
        trade["trade_id"] = trade["exchange_trade_id"] = f"{order_id}|{left}"
        trade["fill"] = self._fill_audit(order, quote, price, volume,
                                         liquidity=liquidity, impact_ticks=impact_ticks)
        sim._trades.append(trade)
        if math.isnan(position["last_price"]):
            position["last_price"] = price
        sim._on_order_traded(child, trade, symbol, position, quote, None)
        order["volume_left"] = left - volume
        order["frozen_margin"] = 0.0 if volume == left else frozen - share
        self._match_dirty = True
        if order["volume_left"] == 0:
            order["status"] = "FINISHED"
            order["last_msg"] = "全部成交"
            sim._orders_events.append(order)
            del sim._orders[symbol][order_id]
        else:
            order["last_msg"] = "部分成交"

    def _fill_audit(self, order: dict[str, Any], quote: dict[str, Any], price: float,
                    volume: int, *, liquidity: str, impact_ticks: int) -> dict[str, Any]:
        """每笔成交的对账/校准字段（随成交行落盘、进日结日志与 activity）：决策时价格
        （下单时对手一档价）、到达时价格、成交价、相对决策价的不利滑点跳数（买：成交价
        − 决策价；卖反向；负数 = 价格改善）、冲击跳数、排队等待时长（到达 → 成交）、
        进队时队前量、是否部分成交。"""
        state = order.get("paper_fill") or {}
        tick = self._tick_of(quote)
        decision = finite_number(state.get("decisionPrice"))
        slippage = None
        if decision is not None and tick > 0:
            raw = (price - decision) if str(order["direction"]) == "BUY" else (decision - price)
            slippage = round(raw / tick, 4)
        arrived_at = state.get("arrivedAt")
        now = self._now_ns()
        wait_ms = round((now - int(arrived_at)) / 1e6, 3) if arrived_at is not None else None
        return {
            "model": str(state.get("model") or self.fill_model.mode),
            "liquidity": liquidity,
            "decisionAt": state.get("decisionAt"),
            "decisionPrice": decision,
            "arrivalAt": arrived_at,
            "arrivalPrice": finite_number(state.get("arrivalPrice")),
            "fillPrice": float(price),
            "slippageTicks": slippage,
            "impactTicks": int(impact_ticks),
            "queueAheadAtJoin": state.get("queueAheadAtJoin"),
            "queueWaitMs": wait_ms,
            "partial": int(volume) < int(order.get("volume_orign") or 0),
        }

    def cancel(self, client_order_id: str) -> dict[str, Any]:
        for symbol, orders in self._sim._orders.items():
            order = orders.get(client_order_id)
            if order is not None:
                if str(order.get("status")) != "ALIVE":
                    break
                _, events = self._sim.cancel_order(symbol, {"order_id": client_order_id})
                return dict(events[-1]) if events else dict(order)
        raise RuntimeError("order_not_cancelable")

    def reset(self) -> list[dict[str, Any]]:
        """账户重置 (#1204, 所有者 2026-10-30)：持仓清空、资金回到开户时的初始资金；今天的成交原样留着
        （历史成交与复盘不动）。调用方先撤掉挂单。返回清掉的持仓（合约、方向、手数）。"""
        from tqsdk.tradeable.sim.trade_future import SimTrade  # 惰性导入，与 __init__ 一致

        cleared = [{"symbol": row["symbol"], "direction": row["direction"], "volume": int(row["volume"])}
                   for row in self.positions() if int(row.get("volume") or 0) > 0]
        fresh = SimTrade("sim", account_id=self.account_id, init_balance=self.init_balance,
                         get_trade_timestamp=lambda: int(self._clock().timestamp() * 1e9),
                         is_in_trading_time=self._sim_is_in_trading_time)
        # 原地替换内容：SimTrade 内部别处可能持有这几个 dict 的引用。
        self._sim._account.clear()
        self._sim._account.update(fresh._account)
        self._sim._positions.clear()
        self._sim._orders.clear()
        self._match_dirty = True
        return cleared

    def live_order(self, client_order_id: str) -> dict[str, Any] | None:
        for orders in self._sim._orders.values():
            order = orders.get(client_order_id)
            if order is not None:
                return dict(order)
        return None

    # -- 结算与到期 ------------------------------------------------------------

    def settle(self, new_day: str, settlement_prices: dict[str, tuple[float, str]]) -> dict[str, Any]:
        """跨日结算，按序执行：

        1. 停车挂单，持仓合约标记到结算价（未到期）或持仓均价（已到期，结转不产生盈亏）；
        2. 恢复挂单后交给 SimTrade.settle——GFD 挂单由它原生撤销、今仓转昨仓、当日字段清零；
        3. 结算之后按结算价强平已到期持仓：成交落在新交易日的 close_profit/commission 里，
           平仓盈亏相对持仓均价（而不是被结算重置过的价），与交易所到期处理口径一致。

        settlement_prices: 引擎选好的 合约 → (价格, 来源标签)。来源是 "settlement"
        （目标日的交易所正式结算价，主动查询或新交易日行情携带的 pre_settlement，
        2026-10-11 起不再有收盘价兜底）；引擎保证每个未到期持仓合约都有目标日的价，
        账本不自行回落到最新价（2026-10-08 起，与真实期货账户同口径）。

        已到期而引擎没给价的持仓（#527 最终结算日协议，2026-10-15：跨停机越过到期日、
        最后交易日结算价还没补查到）只把这一个合约挂起：标记到持仓均价（本次结算不
        确认它的任何盈亏）、不强平，来源记 "expiry-pending"；账户其余合约照常结算。
        之后由引擎按最后交易日结算价（或一个交易日后按暂估价）调 close_expired 强平。
        """
        previous_day = self.book_trading_day
        parked: dict[str, dict[str, dict[str, Any]]] = {}
        for symbol, orders in list(self._sim._orders.items()):
            if orders:
                parked[symbol] = dict(orders)
                self._sim._orders[symbol] = {}
        now_ns = int(self._clock().timestamp() * 1e9)
        sources: dict[str, str] = {}
        expired: set[str] = set()
        try:
            for symbol, position in list(self._sim._positions.items()):
                if not (position.get("volume_long") or position.get("volume_short")):
                    continue
                quote = self._sim._quotes.get(symbol)
                if quote is None or not quote.get("datetime"):
                    continue
                price, source = settlement_prices.get(symbol, (None, ""))
                expire_ns = finite_number(quote.get("expire_datetime"))
                if expire_ns is not None and expire_ns <= now_ns:
                    # 到期合约：结转价 = 持仓均价。结算把昨仓价重置为 position.last_price，
                    # 这里提前把行情标记到持仓均价，重置后 position_price 不变，强平的
                    # 平仓盈亏恰好是从开仓到结算价的全部损益，且不与已计入权益的浮盈重复。
                    direction = "long" if position.get("volume_long") else "short"
                    carry = finite_number(position.get(f"position_price_{direction}"))
                    if carry is None:
                        sources[symbol] = "unavailable"
                        continue
                    if settlement_prices.get(symbol) is None and price is None:
                        # 到期但没有最后交易日结算价（审查 527-5 / #527 最终结算日协议）：
                        # 不拿持仓成本冒充强平价、不强平；标记到持仓均价，本次结算不确认
                        # 它的盈亏（结算后持仓价不变），留给拿到价格的 close_expired。
                        sources[symbol] = "expiry-pending"
                        self._mark_quote(symbol, quote, carry)
                        continue
                    expired.add(symbol)
                    price, source = carry, "expiry-carry"
                if price is None:
                    sources[symbol] = "unavailable"
                    continue
                sources[symbol] = source
                self._mark_quote(symbol, quote, price)
        finally:
            # 恢复放 finally（审查 527-6）：标记阶段抛异常也不能把挂单丢在门外。
            for symbol, orders in parked.items():
                self._sim._orders[symbol] = orders
        _, _, trade_log = self._sim.settle()
        self._carry_open_cost_buckets()
        expiry_fee_sources: dict[str, str] = {}
        expiry_orders = self._close_expired(expired, settlement_prices, expiry_fee_sources)
        self.book_trading_day = new_day
        return {
            "previous_day": previous_day,
            "trade_log": trade_log,
            "settlement_sources": sources,
            "expiry_fee_sources": expiry_fee_sources,
            "day_orders": [dict(order) for orders in parked.values() for order in orders.values()],
            "expiry_orders": expiry_orders,
        }

    def _mark_quote(self, symbol: str, quote: dict[str, Any], price: float) -> None:
        marked = dict(quote)
        marked.update({"last_price": price, "ask_price1": price, "bid_price1": price})
        self._sim.update_quotes(symbol, {"quotes": {symbol: marked}})

    def close_expired(self, symbol: str, price: float,
                      label: dict[str, Any]) -> dict[str, Any]:
        """挂起的到期持仓按给定价当场强平（#527 最终结算日协议）：与日结里的到期强平
        同一条路径（同一费率口径、reason=expiry），成交另带 expirySettlement 标签
        （正式价 / 暂估价及最后交易日）。返回结束的强平单、费率口径与每条腿的
        事实（方向、手数、成交价、实收手续费、乘数、费率），暂估之后补差按它算。"""
        position = self._sim._positions.get(symbol) or {}
        rates = {side: position.get(f"fee_rate_{side}") for side in ("long", "short")}
        quote = self._sim._quotes.get(symbol) or {}
        multiplier = finite_number(quote.get("volume_multiple"))
        seen = len(self._sim._trades)
        fee_sources: dict[str, str] = {}
        orders = self._close_expired({symbol}, {symbol: (price, "settlement")}, fee_sources,
                                     label=label)
        legs = []
        for trade in self._sim._trades[seen:]:
            if trade.get("reason") != "expiry":
                continue
            side = "long" if str(trade.get("direction")) == "SELL" else "short"
            rate = rates.get(side)
            legs.append({
                "side": side, "volume": int(trade.get("volume") or 0),
                "price": float(trade.get("price") or price),
                "commission": float(trade.get("commission") or 0.0),
                "multiplier": multiplier,
                **({"feeRate": dict(rate)} if isinstance(rate, dict) else {}),
            })
        return {"orders": orders, "fee_sources": fee_sources, "legs": legs}

    def adjust_close(self, close_profit: float, commission: float) -> None:
        """暂估到期强平的补差（#527 最终结算日协议）：持仓已经平掉，差额只进当日
        平仓盈亏与手续费，权益与可用资金随之变动——与一笔成交对账户的影响同一
        公式（SimTrade 自己的 _adjust_account_by_trade）。"""
        self._sim._adjust_account_by_trade(commission=commission, close_profit=close_profit)

    def _close_expired(self, expired: set[str],
                       settlement_prices: dict[str, tuple[float, str]],
                       fee_sources: dict[str, str], *,
                       label: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """结算之后按结算价强平到期持仓。挂单已被 settle 撤销、冻结已清零、昨今仓
        已合并为昨仓，直接 CLOSE 全量；强平不受交易时段限制，成交 reason=expiry。

        平仓费（#643/#527 PR-3）：强平单带上开仓时记到持仓的 feeRate，与用户自己
        平仓同一费率公式；fee_sources 记每条腿的口径（"open-fee-rate"）。旧快照
        开仓时还没有这条记录、或开仓单本就不带费率的，沿用 SimTrade 的行情每手
        手续费并记 "quote-commission"——不回算历史、不猜费率。"""
        finished: list[dict[str, Any]] = []
        for symbol in sorted(expired):
            position = self._sim._positions.get(symbol) or {}
            volume_long = int(position.get("volume_long") or 0)
            volume_short = int(position.get("volume_short") or 0)
            if not (volume_long or volume_short):
                continue
            quote = self._sim._quotes.get(symbol)
            if quote is None or not quote.get("datetime"):
                continue
            price, _ = settlement_prices.get(symbol, (None, ""))
            if price is None:
                continue
            self._mark_quote(symbol, quote, price)
            exchange_id = str(position.get("exchange_id") or symbol.split(".", 1)[0])
            instrument_id = str(position.get("instrument_id") or symbol.split(".", 1)[-1])
            legs: list[tuple[str, int]] = []
            if volume_long:
                legs.append(("SELL", volume_long))
            if volume_short:
                legs.append(("BUY", volume_short))
            for direction, volume in legs:
                order_id = f"expiry-{instrument_id.lower()}-{len(finished) + 1}"
                pack = {
                    "aid": "insert_order", "order_id": order_id, "user_id": self.account_id,
                    "exchange_id": exchange_id, "instrument_id": instrument_id,
                    "direction": direction, "offset": "CLOSE",
                    "price_type": "LIMIT", "limit_price": price, "volume": volume,
                }
                side = "long" if direction == "SELL" else "short"
                rate = position.get(f"fee_rate_{side}")
                if isinstance(rate, dict):
                    pack["feeRate"] = dict(rate)
                fee_sources[f"{symbol}:{side}"] = (
                    "open-fee-rate" if isinstance(rate, dict) else "quote-commission")
                # 强平按结算价当场整笔成交（交易所到期处理，不排队、不受成交模型影响）：
                # 临时打开 SimTrade 自己的撮合。
                self._force_trading_time = True
                native, self._native_match = self._native_match, True
                try:
                    _, events = self._sim.insert_order(symbol, pack)
                finally:
                    self._force_trading_time = False
                    self._native_match = native
                for trade in self._sim._trades:
                    if str(trade.get("order_id")) == order_id:
                        trade["reason"] = "expiry"
                        if label is not None:
                            trade["expirySettlement"] = dict(label)
                if events:
                    finished.append(dict(events[-1]))
        return finished

    # -- 形状转换（复用 main.py 的生产函数） -----------------------------------

    def account_fields(self) -> dict[str, Any]:
        return dict(self._sim._account)

    def positions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for symbol, position in self._sim._positions.items():
            rows.extend(OfficialTqKqSession._activity_position(symbol, position_view(position)))
        return rows

    def live_orders(self) -> list[dict[str, Any]]:
        return [dict(order) for orders in self._sim._orders.values() for order in orders.values()]

    def order_report(self, order: dict[str, Any]) -> dict[str, Any]:
        report = OfficialTqKqSession._order_report(order_view(order), observed=True)
        if report.get("found"):
            # sidecar 实盘报告没有的补充字段：已成交手数与本地报单时间。
            original = int(number(order.get("volume_orign") or 0))
            report["order"]["filledVolume"] = max(0, original - int(number(order.get("volume_left") or 0)))
            report["order"]["insertDateTime"] = int(number(order.get("insert_date_time") or 0))
            if isinstance(order.get(CONTRACT_SOURCE_KEY), dict):
                report["order"]["contractSource"] = dict(order[CONTRACT_SOURCE_KEY])
        return report

    @staticmethod
    def activity_order(order: dict[str, Any]) -> dict[str, Any]:
        view = order_view(order)
        return {
            **OfficialTqKqSession._activity_order(view),
            # sidecar 假数据模式的委托行带这三个状态位，引擎补齐同名字段。
            "isOnline": view.is_online,
            "isDead": view.is_dead,
            "isError": view.is_error,
        }

    @staticmethod
    def trade_record(trade: dict[str, Any]) -> dict[str, Any]:
        record = OfficialTqKqSession._activity_trade(AttrView(trade))
        if isinstance(trade.get("fill"), dict):
            # 成交模型的对账/校准字段（2026-10-13）；上线前的历史成交没有。
            record["fill"] = dict(trade["fill"])
        if str(trade.get("order_id") or "").startswith(FORCE_ORDER_PREFIX):
            record["forcedLiquidation"] = True
        if isinstance(trade.get("expirySettlement"), dict):
            # 挂起后强平的到期成交（#527 最终结算日协议）：按最后交易日正式价还是暂估价。
            record["expirySettlement"] = dict(trade["expirySettlement"])
        return record

    def trades(self) -> list[dict[str, Any]]:
        return [self.trade_record(dict(trade)) for trade in self._sim._trades]

    def raw_trades(self) -> list[dict[str, Any]]:
        """当日原始成交 dict（trade_date_time 等蛇形键），供引擎推交易日。"""
        return [dict(trade) for trade in self._sim._trades]

    def needed_symbols(self) -> set[str]:
        needed = set(self._sim._positions.keys())
        for symbol, orders in self._sim._orders.items():
            if orders:
                needed.add(symbol)
        return needed

    def latest_quote(self, symbol: str) -> dict[str, Any] | None:
        quote = self._sim._quotes.get(symbol)
        return dict(quote) if quote is not None else None

    def quote_symbols(self) -> list[str]:
        return list(self._sim._quotes)

    # -- 导出 / 导入 -----------------------------------------------------------

    def export_state(self, *, include_trades: bool = True,
                     trades_from_seq: int = 0) -> dict[str, Any]:
        """整份账本。include_trades=False 是引擎每次落盘写的小体量当前状态（资金、
        持仓、活单、行情）；当日成交随成交逐行另存，不随每条命令重写（2026-10-12）。
        trades_from_seq 是当日第一笔成交的持久序号（引擎的 paper_trades 起点），
        与当日成交笔数相加就是明细写者证明（LOTS_TRADE_SEQ_KEY）。"""
        sim = self._sim
        partial_live = any(
            int(order.get("volume_left") or 0) < int(order.get("volume_orign") or 0)
            for orders in sim._orders.values() for order in orders.values())
        state: dict[str, Any] = {
            "version": STATE_VERSION if partial_live else BASE_STATE_VERSION,
            "init_balance": self.init_balance,
            "book_trading_day": self.book_trading_day,
            "_account": dict(sim._account),
            "_positions": {symbol: dict(position) for symbol, position in sim._positions.items()},
            "_orders": {
                symbol: {order_id: dict(order) for order_id, order in orders.items()}
                for symbol, orders in sim._orders.items() if orders
            },
        }
        if include_trades:
            state["_trades"] = [dict(trade) for trade in sim._trades]
        state["quotes"] = {symbol: dict(quote) for symbol, quote in sim._quotes.items()}
        state["_max_datetime"] = str(sim._max_datetime)
        state[CONSUMED_KEY] = self._export_consumed()
        state[LOTS_TRADE_SEQ_KEY] = {"from": int(trades_from_seq),
                                     "seq": int(trades_from_seq) + len(sim._trades)}
        return state

    def _export_consumed(self) -> dict[str, dict[str, Any]]:
        """已耗盘口量（#834）：只留行情时间仍是账本最新行情的合约——更早的那笔已被
        新行情取代，引擎防倒退水位也不会再放它进来。总是写这个键（可以为空），
        恢复时据“有没有这个键”区分新旧快照。"""
        exported: dict[str, dict[str, Any]] = {}
        for symbol, (stamp, used) in self._consumed.items():
            quote = self._sim._quotes.get(symbol)
            if not stamp or quote is None or str(quote.get("datetime") or "") != stamp:
                continue
            if isinstance(used, _AllUsed):
                exported[symbol] = {"datetime": stamp, "exhausted": True}
            elif used:
                exported[symbol] = {"datetime": stamp,
                                    "levels": [[price, volume] for price, volume in sorted(used.items())]}
        return exported

    def _restore_consumed(self, state: dict[str, Any]) -> None:
        """恢复已耗盘口量（#834）。修复前的快照没有这个键：每个合约按恢复的那笔
        行情盘口全部用尽算（_AllUsed），新行情到来前不吃单、不按对价/队列补量。"""
        self._consumed = {}
        raw = state.get(CONSUMED_KEY)
        if not isinstance(raw, dict):
            for symbol, quote in self._sim._quotes.items():
                stamp = str(quote.get("datetime") or "")
                if stamp:
                    self._consumed[symbol] = (stamp, _AllUsed())
            return
        for symbol, item in raw.items():
            if not isinstance(item, dict) or not item.get("datetime"):
                continue
            stamp = str(item["datetime"])
            if item.get("exhausted"):
                self._consumed[str(symbol)] = (stamp, _AllUsed())
                continue
            levels: dict[float, float] = {}
            for price, volume in item.get("levels") or []:
                levels[float(price)] = float(volume)
            self._consumed[str(symbol)] = (stamp, levels)

    def trades_since(self, start: int) -> list[dict[str, Any]]:
        """当日成交里第 start 笔之后的（引擎按此逐行追加落盘）。"""
        return [dict(trade) for trade in self._sim._trades[start:]]

    def restore_state(self, state: dict[str, Any], *, trades_from_seq: int | None = None) -> None:
        """trades_from_seq：库里已提交的当日成交序号起点（引擎恢复时给）；单独恢复一份
        整本快照（检查点、对照）时不给，明细写者证明只按快照自身核对。"""
        # 快照版本（审查 527-11）：老快照没有版本号按 0 收；比自己新的版本说明
        # 这份代码读不懂它，拒绝恢复而不是解出一本糊涂账。
        if int(state.get("version") or 0) > STATE_VERSION:
            raise RuntimeError("paper_state_version_too_new")
        sim = self._sim
        sim._account = dict(state["_account"])
        sim._positions = {symbol: dict(position) for symbol, position in state["_positions"].items()}
        sim._orders = {
            symbol: {order_id: dict(order) for order_id, order in orders.items()}
            for symbol, orders in state["_orders"].items() if orders
        }
        sim._trades = [dict(trade) for trade in state["_trades"]]
        sim._quotes = {symbol: dict(quote) for symbol, quote in state["quotes"].items()}
        sim._max_datetime = str(state.get("_max_datetime") or "")
        # 排队用的累计成交量基准不跨重启（2026-10-13）：停机期间的成交量不属于任何
        # 一笔行情间隔，按它推进队列会把停机期间的全部成交算进本价位。下一笔行情
        # 重新定基准；落盘之后、停机之前推进的那点队列进度丢失（偏保守）。
        for orders in sim._orders.values():
            for order in orders.values():
                match = order.get("paper_fill")
                if isinstance(match, dict):
                    match["lastVolume"] = None
        self._restore_consumed(state)
        self._drop_unproven_lots(state, trades_from_seq, len(sim._trades))
        self.book_trading_day = str(state["book_trading_day"])
        self.init_balance = float(state.get("init_balance", self.init_balance))
        # 带桶快照：按保存的桶成本、数量和估值价校验/修正派生浮盈（#666 修复前
        # 落盘的快照可能带着失真的 float_profit，重启后增量基准纠不回来）。
        # 无桶的旧快照保持原混合成本口径，不猜历史分摊。
        for symbol, position in sim._positions.items():
            if not any(f"open_cost_{side}_{bucket}" in position
                       for side in ("long", "short") for bucket in ("today", "his")):
                continue
            multiple = finite_number((sim._quotes.get(symbol) or {}).get("volume_multiple"))
            if multiple is None or multiple <= 0:
                continue
            for side in ("long", "short"):
                self._sync_side_float_profit(position, side, multiple)

    def _drop_unproven_lots(self, state: dict[str, Any], trades_from_seq: int | None,
                            trades: int) -> None:
        """明细写者证明（#932 B2）：快照的成交序号戳与已提交的持久成交序号不等或缺失，
        说明最后写这本账的不是维护明细的写者（回滚后的旧版本），或之后又有成交没
        改明细——明细可能落后于桶成本，哪怕手数与成本恰好对上。丢弃全部明细，下一笔
        成交或结算翻桶按桶成本重建成一条（_open_lots）；开仓均价、桶成本、成交与
        资金都不动。证明成立（本版本正常重启）时原样保留，先开先平次序不丢。"""
        stamp = state.get(LOTS_TRADE_SEQ_KEY)
        start = stamp.get("from") if isinstance(stamp, dict) else None
        seq = stamp.get("seq") if isinstance(stamp, dict) else None
        if (type(start) is int and type(seq) is int and seq == start + trades
                and (trades_from_seq is None or start == trades_from_seq)):
            self.lots_dropped = False
            return
        dropped = False
        for position in self._sim._positions.values():
            for side in ("long", "short"):
                for bucket in ("today", "his"):
                    dropped |= position.pop(f"open_lots_{side}_{bucket}", None) is not None
        self.lots_dropped = dropped

    def sanity(self) -> bool:
        """恢复后的账本自检：关键字段缺失或被 NaN 污染时拒绝上线。账户资金字段、
        每个持仓的保证金/盈亏/成本（#527 F3）都须为有限数，持仓手数非负且冻结
        不超过持仓，有持仓一侧的持仓均价有限。今/昨分桶（#777 review）：每桶手数与
        冻结都是有限非负数、冻结不超过本桶，两桶之和等于合计——SimTrade 的平今/平昨
        校验与冻结只看分桶，合计对而分桶坏的账本会拒错单或放错单。"""
        if not math.isfinite(self.init_balance):
            return False
        for key in _SANE_ACCOUNT_FIELDS:
            if finite_number(self._sim._account.get(key)) is None:
                return False
        for position in self._sim._positions.values():
            for key in _SANE_POSITION_FIELDS:
                if finite_number(position.get(key)) is None:
                    return False
            for side in ("long", "short"):
                volume = finite_number(position.get(f"volume_{side}"))
                frozen = finite_number(position.get(f"volume_{side}_frozen", 0))
                if volume is None or volume < 0 or frozen is None or not 0 <= frozen <= volume:
                    return False
                if volume > 0 and finite_number(position.get(f"position_price_{side}")) is None:
                    return False
                today = finite_number(position.get(f"volume_{side}_today"))
                his = finite_number(position.get(f"volume_{side}_his"))
                frozen_today = finite_number(position.get(f"volume_{side}_frozen_today", 0))
                frozen_his = finite_number(position.get(f"volume_{side}_frozen_his", 0))
                if today is None or his is None or frozen_today is None or frozen_his is None:
                    return False
                if not (0 <= frozen_today <= today and 0 <= frozen_his <= his):
                    return False
                if today + his != volume or frozen_today + frozen_his != frozen:
                    return False
        return True

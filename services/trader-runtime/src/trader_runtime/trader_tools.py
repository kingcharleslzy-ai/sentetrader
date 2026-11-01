from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .answer_readings import analysis_labels, day_move_label
from .autonomy import AutonomousSimulationService, AutonomyError
from .backtests import BacktestService
from .contracts import AccountSummary, MarketSnapshot, RuntimeStatus
from .conversation_cards import (
    execution_card,
    protection_state,
    stop_execution_card,
    track_execution_card,
    track_execution_stop,
)
from .data_providers import ReadOnlyDataProvider, provider_read_snapshot
from .delivery_rules import contract_payload, delivery_notice, trading_today
from .exchange_calendar import calendar_trading_days
from .indicators import atr as atr_series
from .indicators import boll as boll_series
from .indicators import cci as cci_series
from .indicators import rsi as rsi_series
from .instruments import (
    CONTEXT_SYMBOL_PATTERN,
    SYMBOL_PATTERN,
    ForeignProduct,
    foreign_named,
    foreign_product_of,
    foreign_spoken,
    foreign_tradable,
    foreign_trade_name,
    foreign_trade_symbol,
    is_foreign_paper,
)
from .operations import owned_operation
from .order_intent import OrderIntent, Price, card_skills
from .order_planner import (
    CATALOG_UNREADABLE,
    AccountSnapshot,
    Ask,
    Card,
    Cards,
    Outcome,
    PlanContext,
    Refuse,
    ask_choices,
    catalog_names,
    ground,
    needs_account,
    prefs_for,
    product_of,
    read_account_snapshot,
    read_account_snapshot_now,
    resolve_words,
)
from .order_planner import plan as plan_order
from .proposals import ProposalConflict, ProposalService
from .research_brief import ResearchBriefService
from .research_live import ResearchLiveService
from .simulation_orders import (
    SimulationOrderError,
)
from .sina_market import BACKUP_NOTICE, BACKUP_STATUS
from .strategy_rules import ProtectionTrailing
from .strategy_validation import validate_strategy_history
from .web_search import WebSearchClient, WebSearchError

log = logging.getLogger(__name__)

class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarketArguments(ToolArguments):
    symbol: str = Field(min_length=1, max_length=80, pattern=CONTEXT_SYMBOL_PATTERN)


class MarketAnalysisArguments(MarketArguments):
    durationSeconds: Literal[60, 180, 300, 900, 1800, 3600, 86400, 604800] = 300
    lookbackBars: int = Field(default=40, ge=20, le=120)


class InstrumentSearchArguments(ToolArguments):
    search: str = Field(default="", max_length=40)
    exchange: str = Field(default="", max_length=10, pattern=r"^[A-Za-z]*$")
    mainOnly: bool = False
    limit: int = Field(default=12, ge=1, le=20)


class BoardArguments(ToolArguments):
    sort: Literal["changePercent", "volume", "openInterest", "openInterestChange"] = Field(
        default="changePercent",
        description="排序字段：changePercent 涨跌幅 / volume 成交量 / openInterest 持仓量 / openInterestChange 增仓")
    order: Literal["desc", "asc"] = Field(default="desc", description="desc 从大到小 / asc 从小到大")
    exchange: str = Field(default="", max_length=10, pattern=r"^[A-Za-z]*$",
                          description="只看某个交易所，如 SHFE；留空看全部")
    limit: int = Field(default=10, ge=1, le=30, description="返回行数上限")


class WebSearchArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=128)
    count: int = Field(default=5, ge=1, le=10)


class ResearchBriefArguments(ToolArguments):
    symbol: str = Field(min_length=3, max_length=80, pattern=CONTEXT_SYMBOL_PATTERN)
    topic: Literal["position_rank", "warehouse_receipt", "spot_basis", "inventory",
                   "term_structure", "fees_margin", "news", "desk"]


class NewsFlashArguments(ToolArguments):
    product: str | None = Field(default=None, max_length=24, description="只看某品种，如 SHFE.rb 或 rb；省略看全部")
    hours: int = Field(default=24, ge=1, le=720, description="往前多少小时；最多 72，officialOnly 时最多 720（官方条目保留 30 天）")
    limit: int = Field(default=20, ge=1, le=50, description="返回条数上限")
    macroOnly: bool = Field(default=False, description="true 只看宏观类快讯（美联储、CPI、央行等）")
    officialOnly: bool = Field(default=False, description="true 只看官方源条目（五家期货交易所公告、统计局/发改委等部委发布），不按 product 筛选，可往前查 30 天")


class ForeignMarketsArguments(ToolArguments):
    product: str | None = Field(default=None, max_length=24, description="只看某品种对应的外盘，如 SHFE.sc；省略看全部")


class EconomicCalendarArguments(ToolArguments):
    days: int = Field(default=7, ge=1, le=7, description="未来多少天（最多 7 天，即后台实际抓取的范围）")
    region: Literal["中国", "美国", "欧元区"] | None = Field(default=None, description="只看某地区；省略看全部")
    product: str | None = Field(default=None, max_length=24, description="只看与某品种有关的数据，如 SHFE.au 或 au；省略看全部")


class TradeJournalArguments(ToolArguments):
    days: int = Field(default=7, ge=1, le=366, description="最近多少天平仓的交易")
    currency: Literal["CNY", "USD"] | None = Field(default=None, description="复盘账本币种；省略沿用会话账户，USD读本账户的美元子账，不切换交易账户")
    symbol: str | None = Field(default=None, max_length=40, pattern=CONTEXT_SYMBOL_PATTERN, description="只看某个合约，如 SHFE.rb2701")
    limit: int = Field(default=20, ge=1, le=50)


class ReviewPicksArguments(ToolArguments):
    scope: Literal["day", "week"] = Field(default="day", description="day 最近一个有平仓的交易日，week 最近七天")


class DailyReviewArguments(ToolArguments):
    tradingDay: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="交易日 YYYY-MM-DD；省略：读取为最近一份，重新生成为今天")


class ReviewObservationArguments(ToolArguments):
    tradingDay: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="这份复盘的交易日")
    verdict: Literal["remember", "dismiss"] = Field(description="remember 记住 / dismiss 不对（以后不再提）")
    quote: str | None = Field(default=None, max_length=200, description="记住时必填：用户亲口说“记住这条”之类的原话，一字不差")
    observationKey: str = Field(min_length=1, max_length=400, description="get_daily_review 返回的 observation.key")
    revision: str = Field(min_length=1, max_length=400,
                          description="get_daily_review 返回的 observation.revision（没有时填 key）；观察已更新会被拒，重读后再答")


class TradeNoteArguments(ToolArguments):
    roundId: str = Field(min_length=3, max_length=120, description="get_trade_journal 返回的交易 id")
    text: str = Field(min_length=1, max_length=500, description="用户自己的话，照原意记，不要替他总结或评价")


class PriceAlertCreateArguments(ToolArguments):
    symbol: str = Field(min_length=1, max_length=60,
                        description="合约或品种：SHFE.rb2610 / rb2610 / 螺纹钢（品种名自动取当前主力合约）")
    price: float = Field(gt=0, description="目标价；必须落在该合约的最小变动价位上")
    direction: Literal["above", "below"] | None = Field(
        default=None, description="above 涨到 / below 跌到；省略时 Runtime 按最新价自动判定")
    note: str = Field(default="", max_length=60, description="可选备注，一句话")


class PriceAlertIdArguments(ToolArguments):
    alertId: str = Field(min_length=1, max_length=40, description="list_price_alerts 返回的提醒 id")


# 作废待确认卡 (#756) is the card's own 取消: like the card routes it belongs to no skill, so no skill
# switch can take away a waiting card's way out (the card priority, test_skill_registry checklist 8).
CARD_ACTION_TOOLS = frozenset({"reject_proposal"})


class RejectProposalArguments(ToolArguments):
    proposalId: str = Field(min_length=1, max_length=80, description="系统说明里那张待确认卡的 proposalId，照抄")
    proposalVersion: int = Field(ge=1, description="同一张卡的 version，照抄；卡已更新时会被拒绝")


DRAWING_KIND_ARGUMENT = Literal["hline", "trendline", "zone", "ray", "extended", "hray", "vline", "channel", "polyline",
                                "rect_zone", "fib", "percent", "gann", "rect", "ellipse", "triangle", "text", "arrow",
                                "price_label", "measure"]
_DRAWING_POINTS_HELP = ("坐标：hline {price}；trendline {t1,p1,t2,p2}；zone {low,high}（这三种也可写 anchors）；"
                        "其余都写 {anchors:[{t,p},…]}（t 秒级 bar 时间，p 价格）：ray/extended/rect_zone/fib/percent/gann/rect/ellipse/arrow/measure 两个点，"
                        "hray/text/price_label 一个点，vline 一个点只要 t，channel/triangle 三个点（通道第三点定宽度），polyline 2–20 个点；"
                        "fib/percent 可加 levels（比例，1 = 100%，默认 fib 0/0.236/0.382/0.5/0.618/1）")
_DRAWING_STYLE_HELP = ("可选样式：color（user/amber/red/green/blue/ink 或 #rrggbb）、width 1–4、dash solid/dashed/dotted、"
                       "extendLeft/extendRight 向左/右延长、showPrice 价格标签、locked 锁定、hidden 隐藏、"
                       "allPeriods 在所有周期显示（false 时合并后的样式必须有有效 period，否则返回 422；"
                       "period 为画线时的周期秒数，0 = 分时）")


class ChartDrawingCreateArguments(ToolArguments):
    symbol: str = Field(min_length=1, max_length=60,
                        description="合约或品种：SHFE.rb2610 / rb2610 / 螺纹钢（品种名自动取当前主力合约）")
    kind: DRAWING_KIND_ARGUMENT = Field(
        description="hline 水平线 / trendline 趋势线（线段）/ ray 射线 / extended 直线 / hray 水平射线 / vline 垂直线 / "
                    "channel 平行通道 / polyline 折线 / zone 价格区间 / rect_zone 矩形区间 / fib 黄金分割 / percent 百分比线 / "
                    "gann 江恩八线 / rect 矩形 / ellipse 圆或椭圆 / triangle 三角形 / text 文字 / arrow 箭头 / price_label 价格标签 / measure 测量")
    points: dict[str, Any] = Field(description=_DRAWING_POINTS_HELP)
    label: str = Field(default="", max_length=60, description="可选短标签 ≤12 字；text 文字的内容写这里（≤60 字）")
    style: dict[str, Any] | None = Field(default=None, description=_DRAWING_STYLE_HELP)


class ChartDrawingUpdateArguments(ToolArguments):
    drawingId: str = Field(min_length=1, max_length=40, description="list_chart_drawings 返回的线 id")
    points: dict[str, Any] | None = Field(default=None, description="移动这条线：新的坐标，写法同 create_chart_drawing（不能改类型）；锁定的线先解锁")
    label: str | None = Field(default=None, max_length=60, description="新标签（空串清除）")
    style: dict[str, Any] | None = Field(default=None, description="只写要改的样式键（null 恢复默认）。" + _DRAWING_STYLE_HELP)


class ChartDrawingIdArguments(ToolArguments):
    drawingId: str = Field(min_length=1, max_length=40, description="list_chart_drawings 返回的线 id")


class ChartDrawingListArguments(ToolArguments):
    symbol: str | None = Field(default=None, max_length=60, description="可选：只看某合约的线；省略列出全部合约")


class ChartDrawingClearArguments(ToolArguments):
    symbol: str = Field(min_length=1, max_length=60, description="要清空画线的合约（实际合约代码或品种名，品种名取当前主力）")


class SettlementPricesArguments(ToolArguments):
    symbols: list[str] = Field(min_length=1, max_length=10,
                               description="实际交割合约代码列表，如 [\"SHFE.rb2610\"]；不要传主连")
    days: int = Field(default=1, ge=1, le=30, description="交易日数；当日结算价 19:00 后才产生")


class MainContractHistoryArguments(ToolArguments):
    symbol: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_.@-]+$",
                        description="品种或主连代码：rb / SHFE.rb / KQ.m@SHFE.rb 均可，Runtime 统一成主连")
    days: int = Field(default=20, ge=1, le=200, description="最近交易日数，默认 20")


class SymbolRankingArguments(ToolArguments):
    symbol: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    rankingType: Literal["VOLUME", "LONG", "SHORT"] = Field(
        default="VOLUME", description="VOLUME 成交量排名 / LONG 多头持仓 / SHORT 空头持仓")
    days: int = Field(default=1, ge=1, le=5, description="天数；每日返回前 20 席位")


class BacktestArguments(ToolArguments):
    strategyPath: str = Field(default="workspace/strategies/rb_trend.py", min_length=1, max_length=500)
    dataPath: str = Field(default="workspace/backtests/fixtures/rb_monthly_v1.csv", min_length=1, max_length=500)
    baselineThreshold: str = Field(default="1.00", pattern=r"^\d+(?:\.\d+)?$")
    candidateThreshold: str = Field(default="0.90", pattern=r"^\d+(?:\.\d+)?$")


class TqSdkBacktestArguments(ToolArguments):
    symbol: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    startDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    endDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    durationSeconds: Literal[60, 180, 300, 900, 3600, 86400] = 3600
    baselineThreshold: str = Field(default="1.00", pattern=r"^\d+(?:\.\d+)?$")
    candidateThreshold: str = Field(default="0.90", pattern=r"^\d+(?:\.\d+)?$")
    initialBalance: float = Field(default=1_000_000, ge=100_000, le=100_000_000)


class StrategyDraftArguments(ToolArguments):
    """“我的策略”的一条（#932 F5）：与策略编辑卡同一个保存入口 POST /api/strategy-plans。"""
    plan: dict[str, Any] = Field(
        description="完整策略定义 StrategyPlan（与策略编辑卡保存的同一份；字段以 app_capabilities 里 "
                    "POST /api/strategy-plans 的 schema 为准，修改时在 get_strategy_draft 读到的 definition.plan 上改）。"
                    "只写用户说了的规则与参数，没说的不加。")
    instrumentScope: dict[str, Any] | None = Field(
        default=None,
        description='适用范围：只用于某些品种或合约时 {"mode":"selected","symbols":["SHFE.ag2612"]}，全部品种 {"mode":"any"}；'
                    '省略时新建为全部品种，修改（带 baseStrategyId）沿用原策略的范围')
    baseStrategyId: str | None = Field(
        default=None, min_length=8, max_length=100,
        description="修改已有策略时填它的 strategyId（策略库版本或运行实例都行）：存为该策略的新版本，"
                    "原版本和正在运行的实例都不变。新建时省略。")


class StrategyIdArguments(ToolArguments):
    strategyId: str = Field(min_length=8, max_length=100)


class StrategyStartArguments(StrategyIdArguments):
    signalAction: Literal["alert", "confirm", "auto"] | None = Field(
        default=None,
        description="执行方式：alert 只提醒 / confirm 出卡确认（用户确认后才下单）/ auto 自动下单（需 AI 自主执行开着）。"
                    "省略保持实例原设置；提醒与确认卡发到本对话。")
    resetDrawdown: bool = Field(
        default=False,
        description="重置回撤并恢复：停机中的一轮回撤在平仓、撤单与成交回报都对齐确认完成后删掉旧轮、"
                    "开新一轮（新基准新高点）。前提不满足会被拒绝并说明原因。普通启动（省略/false）"
                    "绝不重置——停机中的一轮保持停止，不会开新仓。")


class StrategyValidationArguments(StrategyIdArguments):
    startDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    splitDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    endDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    slippageTicks: float = Field(default=1.0, ge=0, le=10)
    commissionTicksPerSide: float = Field(default=0.5, ge=0, le=10)
    initialBalance: float = Field(default=1_000_000, ge=100_000, le=100_000_000)


class StrategyListArguments(ToolArguments):
    limit: int = Field(default=10, ge=1, le=20)


class ExplainStrategySignalArguments(ToolArguments):
    strategyId: str | None = Field(default=None, max_length=100,
                                   description="省略 = 当前合约上监控中的策略（多张时返回候选让用户选）")
    symbol: str | None = Field(default=None, max_length=80, pattern=CONTEXT_SYMBOL_PATTERN,
                               description="省略 = 对话窗口当前合约")
    chartTimeframeSeconds: Literal[60, 180, 300, 900, 3600, 86400] | None = Field(
        default=None, description="当前图表周期；跟随图表周期的策略（如“图表周期均线交叉”）必须知道它，省略 = 对话窗口当前周期")
    uiWindowId: str | None = Field(default=None, max_length=40)


class TradeProtectionArguments(ToolArguments):
    """开仓顺带挂的保护价（至少一个）：确认成交后按实际成交量自动挂上，撤单/未成交不挂。"""
    stopLossPrice: float | None = Field(default=None, gt=0, description="止损价：亏到这个价自动平仓")
    takeProfitPrice: float | None = Field(default=None, gt=0, description="止盈价：赚到这个价自动平仓")
    trailing: ProtectionTrailing | None = Field(default=None, description="用户要追踪止损时填：止损按最新价只紧不松地跟（要有止损价）")
    breakevenAtR: bool | None = Field(default=None, description="用户说浮盈 1R 后移到成本时为 true（要有止损价）")


class RemainingProtectionArguments(ToolArguments):
    """合并卡第二步（只跟平仓卡带）：平仓后剩余仓位的止损/止盈改到哪。给价＝改到这个价；给 null＝撤掉这一项；
    不给＝按各桶可证明的原价保留；同桶异价或部分仓位无保护不能确定时拒绝出卡。剩余仓位原来的止损止盈会被替换，不会叠两套。"""
    stopLoss: float | None = Field(default=None, gt=0, description="剩余仓位的新止损价；null＝撤掉止损；不传＝按各桶可证明原价保留；有歧义会拒绝出卡")
    takeProfit: float | None = Field(default=None, gt=0, description="剩余仓位的新止盈价；null＝撤掉止盈；不传＝按各桶可证明原价保留；有歧义会拒绝出卡")


class OrderConditionArguments(ToolArguments):
    """用户说的条件（“跌到 3000 就买”“收盘前平掉”）：有条件的单不出即时卡（到价的可建价格提醒）。"""
    type: Literal["price", "time"]
    op: Literal["<=", ">=", "<", ">", "reach"] | None = Field(default=None, description="价格条件：跌到 <=、跌破 <、涨到 >=、突破 >、做到 reach")
    value: float | None = Field(default=None, gt=0, description="价格条件的价位")
    text: str | None = Field(default=None, max_length=40, description="时间条件的原话（收盘前、明天开盘）")


class OrderLegArguments(ToolArguments):
    """一个下单意图（docs/ORDER_INTENT_V2.md §2.1）：只写用户说了的，没说的留空，Runtime 的下单规划器补齐或追问。"""
    act: Literal["open", "close", "close_all", "target", "reverse"] = Field(
        description="open 开仓 / close 平掉一部分（带手数或比例）/ close_all 平掉整个持仓（全部、某品种、某一边）/ "
                    "target 调到目标净持仓 / reverse 反手")
    symbol: str | None = Field(
        default=None, min_length=1, max_length=40,
        description="合约：用户说了月份就写合约代码（如 SHFE.rb2701）或原话（螺纹2701）；只说了品种写品种名（螺纹）——平仓、调仓按持有的月份；"
                    "没说就不填（用当前窗口的合约）")
    symbols: list[str] | None = Field(
        default=None, max_length=20,
        description="只给 close_all：要平的几个品种或合约（“把螺纹和沪金都平了”）；不填 = 全部持仓")
    dir: Literal["long", "short"] | None = Field(
        default=None, description="多/空，不是买/卖：开多、平多单 = long；开空、平空单 = short。用户没说、只能从持仓猜时不填")
    vol: int | None = Field(default=None, gt=0, le=10000, description="手数（open / close）")
    target: int | None = Field(default=None, ge=-10000, le=10000, description="target：目标净持仓，多为正、空为负、清零为 0")
    frac: float | None = Field(default=None, gt=0, lt=1, description="close：按持仓比例平（平一半 = 0.5），Runtime 向下取整至少 1 手")
    price: Literal["counter", "queue", "last"] | None = Field(
        default=None, description="价格方式：counter 对价/对手价/市价、queue 挂单价/排队价（挂买一卖一）、last 最新价/现价；没说不填")
    limitPrice: float | None = Field(default=None, gt=0, description="用户自己说的价（“820 挂一手多”= 820），照原价出卡")
    bucket: Literal["today", "yesterday", "auto"] | None = Field(default=None, description="平今 today / 平昨 yesterday；没说不填")
    cond: OrderConditionArguments | None = Field(default=None, description="用户说了条件（到价、到时间）才填")
    protection: TradeProtectionArguments | None = Field(default=None, description="开仓顺带的止损/止盈价")
    remainingProtection: RemainingProtectionArguments | None = Field(
        default=None, description="平仓后剩余仓位的止损/止盈改到哪（“平一半，剩下的止损挪到成本”）")


class ProposeOrderArguments(OrderLegArguments):
    """propose_order：交易 AI 唯一的出卡工具，一次一单。一句里有几单（“买两手螺纹然后买一手沪金”）时只填 legs：Runtime
    不出卡，请用户一单一单说（#952：每一单的槽位绑不到它自己的分句上）。"""
    act: Literal["open", "close", "close_all", "target", "reverse"] | None = Field(  # type: ignore[assignment]
        default=None, description="一句一单时必填：open / close / close_all / target / reverse（见 legs 里的同名字段）")
    legs: list[OrderLegArguments] | None = Field(default=None, min_length=2, max_length=4,
                                                 description="一句里有几单时填（不出卡，Runtime 请用户一单一单说）；一单时不填")
    reason: str = Field(min_length=1, max_length=1000, description="一句话写用户的原意")

    @model_validator(mode="after")
    def _one_order_or_legs(self) -> ProposeOrderArguments:
        own = self.model_dump(exclude={"legs", "reason"}, exclude_none=True)
        if self.legs and own:
            raise ValueError("legs 与本层下单字段只能二选一")
        if not self.legs and self.act is None:
            raise ValueError("act 必填（或用 legs 写一句多单）")
        return self


class OrderEstimateArguments(ToolArguments):
    """下单前的“钱”估算（只读）：开仓保证金/手续费/可开手数，平仓释放保证金与今昨拆分的手续费。"""
    symbol: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    side: Literal["buy", "sell"] = Field(description="方向：buy 买入（开多或平空）、sell 卖出（开空或平多）")
    offset: Literal["open", "close"] = Field(default="open", description="open 开仓 / close 平仓")
    volume: int = Field(default=1, gt=0, le=1000, description="手数")
    price: float | None = Field(default=None, gt=0, description="参考价；省略用最新价")
    positionBucket: Literal["today", "historical", "all"] | None = Field(
        default=None,
        description="平仓只算哪一侧：today 今仓 / historical 昨仓 / all 按账户平今平昨偏好拆（上期所、能源中心）；其他交易所指令不分今昨，费用按先昨后今拆；开仓忽略")


class AutonomousTradeArguments(ToolArguments):
    symbol: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    side: Literal["buy_open", "sell_open", "buy_close", "sell_close"]
    volume: int = Field(gt=0)
    priceIntent: Literal["passive", "aggressive", "last"]
    positionBucket: Literal["today", "historical", "all"] | None = None
    reason: str = Field(min_length=1, max_length=1000)


class SimulationOrderArguments(ToolArguments):
    clientOrderId: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class StopLossArguments(ToolArguments):
    symbol: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    positionDirection: Literal["long", "short"]
    positionBucket: Literal["today", "historical", "all"]
    volume: int = Field(gt=0)
    # 止损 / 止盈至少一个（“给我的螺纹多单设个止损 3100”只传 triggerPrice；“只挂止盈 3200”只传
    # takeProfitPrice）。方向错了 Runtime 用中文原因拒绝（多仓 止损<现价<止盈，空仓相反）。
    triggerPrice: float | None = Field(default=None, gt=0)
    takeProfitPrice: float | None = Field(default=None, gt=0)
    priceIntent: Literal["passive", "aggressive", "last"] = "aggressive"


class StopLossIdArguments(ToolArguments):
    stopId: str = Field(min_length=8, max_length=100)


class ResumeSimulationArguments(ToolArguments):
    symbol: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")


class StopSimulationArguments(ToolArguments):
    emergency: bool = False
    scope: Literal["all", "opens"] = "all"


class CapabilityArguments(ToolArguments):
    operation: str | None = Field(default=None,
        description="省略时查目录；否则复制目录中的 operation 原文（含 GET/POST 和 {路径参数}），不要替换占位符或传实际 URL；"
                    "界面命令某字段的完整定义用 ui:<字段>。")
    query: str | None = Field(default=None, max_length=40,
        description="只列与这个关键词匹配的操作、工具和界面字段（如 alerts、strategy、disclosure）")


class AppCallArguments(ToolArguments):
    requestJson: str = Field(min_length=1,
        description='JSON 编码的字符串，不是对象。内容字段为 operation、path、query、body、replayId。'
        '例如 {"operation":"GET /api/app/ui/windows/{window_id}","path":{"window_id":"窗口UUID"}}；'
        'operation 保留占位符，实际值放在 path 的同名键中。')


# AI annotation layer (step one): read the chart the user sees, point at a surface, draw an opinion.
# Every tool acts on the conversation's own window (`uiWindowId` from the message, #193) unless one is named.
class ChartBarsArguments(ToolArguments):
    uiWindowId: str | None = Field(default=None, max_length=40, description="要读的窗口；省略 = 本次对话的窗口")
    symbol: str | None = Field(default=None, max_length=80, pattern=CONTEXT_SYMBOL_PATTERN, description="省略 = 窗口当前合约")
    durationSeconds: Literal[60, 180, 300, 900, 1800, 3600, 86400, 604800] | None = Field(default=None, description="省略 = 窗口当前周期")
    range: Literal["visible", "recent"] = Field(default="visible", description="visible = 图上可见区间（无窗口时退回 recent）；recent = 最近 limit 根")
    limit: int = Field(default=120, ge=20, le=300, description="最多返回的 K 线根数")


class HighlightUiArguments(ToolArguments):
    uiWindowId: str | None = Field(default=None, max_length=40)
    target: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$",
                               description="界面目标 id（快照 context.highlightTargets 里的值，如 rail:order、positions-bar、chart.main）")
    label: str | None = Field(default=None, max_length=40, description="气泡里的一句话，≤40 字")
    style: Literal["pulse", "outline"] = "pulse"
    durationMs: int = Field(default=6000, ge=0, le=15000, description="1000–15000；0 = 清除该目标")
    clear: bool = Field(default=False, description="true = 清除全部高亮")


class AnnotateChartArguments(ToolArguments):
    uiWindowId: str | None = Field(default=None, max_length=40)
    op: Literal["set", "add", "clear"] = Field(description="set 替换全部、add 追加（同 id 覆盖）、clear 清空")
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=6,
        description="标注项（同一屏最多 6 条）：{id, kind: point|hline|zone|trendline|vline|circle|arrow|text, pane?: main|kd|macd|rsi|oi, role?: buy|sell|close|note（point 用）, "
                    "price?, price2?, time?, time2?, label?(≤8 字短标签，如“空 15420”), tone?}；多空入场/止损/止盈/平仓一律用 point（精确到 time+price），"
                    "压力/支撑才用 hline 或 zone；副图 pane 只支持 hline/vline/point/text，price 为该副图数值（K/D、DIF/DEA/柱、RSI 0–100、oi 持仓量手数）；"
                    "pane 只用窗口当前打开的副图，没打开的副图不显示该标注（回执会注明）；time 用 get_chart_bars 返回的 time（秒）。"
                    "图上显示编号 ①②③ + 短标签，解释写在回复正文里按编号讲。")
    symbol: str | None = Field(default=None, max_length=40, pattern=SYMBOL_PATTERN, description="省略 = 窗口当前合约")
    focus: dict[str, int] | None = Field(default=None, description="画完后把图滚到 {from, to}（秒）；省略不滚动")


class StartTourArguments(ToolArguments):
    uiWindowId: str | None = Field(default=None, max_length=40)
    steps: list[dict[str, Any]] = Field(min_length=1, max_length=20,
        description="引导步骤（1–20）：{target: 界面目标 id（快照 context.highlightTargets）, label: ≤40 字气泡说明, say?: ≤80 字这一步要说的话, "
                    "focus?: {view|panel|strategyTab|positionsBar|positionsTab|ticketMode|settingsPage|settingsOpen|dialog|disclosure} 先打开目标所在表面}")


class UpdatePreferencesArguments(ToolArguments):
    changes: dict[str, Any] = Field(min_length=1,
        description="部分更新：只含要改的分类和键，如 {\"voice\": {\"speed\": \"fast\"}} 或 {\"trade\": {\"defaultPriceIntent\": \"passive\"}}；"
                    "分类 voice/chart/trade/chat/notify/data/ui/marketBoard（报价总表：columns 列、sort 排序、exchange 交易所筛选），"
                    "键与取值范围先用 get_preferences 读当前值。")


# 私人记忆 (3a, 2026-09-29): the user's own words only. quote must be the sentence the user
# just said (checked against recent turns server-side); anything that reads as authorization
# is refused — memory never replaces the settings or the confirmation flow.
MemoryLayer = Literal["profile", "rule", "lesson"]


class RememberArguments(ToolArguments):
    layer: MemoryLayer = Field(description="profile 画像（常做的品种、日内/波段、仓位习惯、回答偏好、明确限制）；"
                                           "rule 约定（用户要求 AI 怎么做事，如“给建议先说止损位”）；lesson 经验（交易经验教训）")
    text: str = Field(min_length=1, max_length=120,
                      description="记下的内容，≤120 字，照用户的原意一句话，不添加你的评价")
    quote: str = Field(min_length=2, max_length=200,
                       description="用户这句原话（必须来自本轮或最近的用户消息，服务端会核对；对不上会被拒绝）")
    products: list[str] | None = Field(default=None, max_length=5,
                                       description="相关品种的中文名，如 [\"螺纹\",\"铁矿\"]，每个 1–6 字；经验条目用它在相关话题里优先带上")


class ForgetArguments(ToolArguments):
    memoryId: str = Field(min_length=1, max_length=64, description="recall_memory 返回的记忆 id")
    reason: str | None = Field(default=None, max_length=120, description="用户说为什么不对，一句话")


class RecallArguments(ToolArguments):
    layer: MemoryLayer | None = Field(default=None, description="只看某一层：profile / rule / lesson")
    includeInvalid: bool = Field(default=False, description="true 时连已删除（失效）的一起列出")


# 我的流程 (2026-10-06): the AI drafts a 流程卡 (the user's 保存 writes it), lists, and runs on request.
RoutineStepKind = Literal["read", "draw", "backtest", "margin", "card", "alert", "watch", "review"]


class RoutineStepArguments(ToolArguments):
    kind: RoutineStepKind = Field(description="read 读今日判断/调研/简报/持仓账户；draw 画线；backtest 回测；margin 算保证金与可开手数；"
                                              "card 出开仓/平仓卡（只出卡）；alert 到价提醒；watch 盯异动；review 复盘")
    text: str = Field(min_length=1, max_length=60, description="这一步做什么，尽量保留用户原话一句（≤60 字）；不要追加用户没说的“需要就设 / 有必要就设”")


class ProposeRoutineArguments(ToolArguments):
    name: str = Field(min_length=1, max_length=12, description="流程名，用户起的名字（≤12 字），如“老规矩”")
    steps: list[RoutineStepArguments] = Field(min_length=1, max_length=12, description="按用户说的顺序，最多 10 步")
    routineId: str | None = Field(default=None, max_length=40,
                                  description="改已有流程时填它的 id（list_routines）；写改完后的完整步骤")


class RunRoutineArguments(ToolArguments):
    routineId: str = Field(min_length=1, max_length=40, description="list_routines 返回的流程 id")
    quote: str = Field(min_length=2, max_length=200, description="用户要求运行的原话（必须含流程名，服务端核对）")


class StrategyBacktestArguments(ToolArguments):
    strategyId: str | None = Field(default=None, max_length=100, description="已保存的策略（策略库或运行实例 id）")
    plan: dict[str, Any] | None = Field(default=None, description="不保存直接回测的完整策略定义（改后版本）；与 strategyId 二选一")
    symbol: str | None = Field(default=None, max_length=40, pattern=SYMBOL_PATTERN,
                               description="回测合约；策略库版本与 plan 必填")
    timeframe: Literal[60, 180, 300, 900, 3600, 86400] | None = Field(default=None, description="跟随图表周期的策略用哪个周期")
    tradingDays: int | None = Field(default=None, ge=1, le=2000, description="最近 N 个交易日；省略按周期取最近可取的历史")
    slippageTicks: int = Field(default=0, ge=0, le=5)
    initialBalance: float | None = Field(default=None, gt=0, le=1e12)


TemplateCategory = Literal["trend_breakout", "ma_trend", "channel_reversion", "reversal", "intraday_range"]
TemplateTimeframe = Literal[60, 180, 300, 900, 3600, 86400]


class StrategyTemplateListArguments(ToolArguments):
    category: TemplateCategory | None = Field(
        default=None, description="只列一类：trend_breakout 趋势突破 / ma_trend 均线趋势 / channel_reversion 通道回归 / reversal 反转 / intraday_range 日内区间")


class StrategyTemplatePreviewArguments(ToolArguments):
    templateId: str = Field(min_length=1, max_length=60, description="list_strategy_templates 的 id")
    symbol: str = Field(max_length=40, pattern=SYMBOL_PATTERN, description="回测合约，例如 SHFE.rb2610")
    params: dict[str, float | None] = Field(default_factory=dict, description="要改的参数（键见模板 params）；省略取默认，可不填的参数（optional）给 null 即不设")
    timeframe: TemplateTimeframe | None = Field(default=None, description="执行周期；省略用推荐周期")
    tradingDays: int | None = Field(default=None, ge=1, le=2000, description="最近 N 个交易日；省略按周期取可取的历史")
    slippageTicks: int = Field(default=0, ge=0, le=5)


class StrategyTemplateCreateArguments(ToolArguments):
    templateId: str = Field(min_length=1, max_length=60, description="list_strategy_templates 的 id")
    params: dict[str, float | None] = Field(default_factory=dict, description="要改的参数；省略取默认，可不填的参数（optional）给 null 即不设")
    name: str | None = Field(default=None, min_length=1, max_length=80,
                             description="策略名称；省略为“模板名 · 合约中文名 · 周期”，重名自动加“(2)”")
    chartSymbol: str | None = Field(default=None, max_length=40, pattern=SYMBOL_PATTERN,
                                    description="默认名里的合约（用户正在看的合约）；只用于命名")
    symbol: str | None = Field(default=None, max_length=40, description="只用于这个品种或合约（适用范围），省略为全部品种")
    timeframe: TemplateTimeframe | None = Field(default=None, description="执行周期；省略用推荐周期")


class StrategyLabVariantArguments(ToolArguments):
    """策略实验室的一组变体：要么 templateId（+params），要么完整 plan。"""
    label: str | None = Field(default=None, min_length=1, max_length=60, description="这组的名字；省略为“模板名 · 参数摘要”或策略名")
    templateId: str | None = Field(default=None, min_length=1, max_length=60, description="来源模板（list_strategy_templates 的 id）")
    params: dict[str, float | str | None] | None = Field(default=None, description="模板参数（键见模板 params）；省略取默认")
    timeframe: TemplateTimeframe | None = Field(default=None, description="执行周期；省略用模板推荐周期")
    plan: dict[str, Any] | None = Field(default=None, description="完整策略定义（不用模板时给这个）")


class StrategyLabCreateArguments(ToolArguments):
    symbol: str = Field(max_length=40, pattern=SYMBOL_PATTERN, description="对比的合约，例如 SHFE.rb2610")
    variants: list[StrategyLabVariantArguments] = Field(min_length=1, max_length=12,
        description="要对比的几组（同一合约上最多 12 组）：几组模板参数，或改前 / 改后的 plan")
    tradingDays: int | None = Field(default=None, ge=1, le=2000, description="只回测最近 N 个交易日；省略按周期取可取的历史")
    slippageTicks: int = Field(default=0, ge=0, le=5, description="开平各算几跳滑点")
    uiWindowId: str | None = Field(default=None, max_length=80, description="旧字段，接受但不再使用：比较一律在云端算")


class StrategyLabGetArguments(ToolArguments):
    labId: str = Field(min_length=1, max_length=40, description="create_strategy_lab 返回的 labId")
    waitSeconds: int = Field(default=60, ge=0, le=90, description="最多等多久出结果（每 2 秒查一次；超时返回当前进度）")


class StrategyLabChooseArguments(ToolArguments):
    labId: str = Field(min_length=1, max_length=40, description="create_strategy_lab 返回的 labId")
    variantId: str = Field(min_length=1, max_length=40, description="get_strategy_lab 表里想留下的那组")
    name: str | None = Field(default=None, min_length=1, max_length=80, description="策略名称；省略用这组的名字")


class ReplayControlArguments(ToolArguments):
    action: Literal["play", "pause", "step", "speed", "pause_entries", "resume_entries", "finish", "close"] = Field(
        description="控制用户当前的历史回放：play 播放/继续、pause 暂停、step 单步走一根、speed 调速（必带 speed）、"
                    "pause_entries/resume_entries 暂停/恢复历史信号注入、finish 收尾结算、close 关闭回放")
    speed: Literal[1, 2, 5, 10] | None = Field(default=None, description="speed 动作的每秒步数（一步一根 1 分钟线），只允许 1/2/5/10")


def _leg_intent(leg: OrderLegArguments, reason: str) -> OrderIntent:
    cond = leg.cond.model_dump(exclude_none=True) if leg.cond is not None else None
    return OrderIntent(
        act=leg.act, stance="conditional" if cond else "order", symbol=leg.symbol,
        symbols=tuple(leg.symbols or ()), dir=leg.dir, vol=leg.vol, target=leg.target, frac=leg.frac,
        price=Price(mode=leg.price, limit=leg.limitPrice), bucket=leg.bucket or "auto", cond=cond, reason=reason,
        protection=leg.protection.model_dump(exclude_none=True) if leg.protection is not None else None,
        remaining_protection=(leg.remainingProtection.model_dump(exclude_unset=True)
                              if leg.remainingProtection is not None else None))


def argument_problems(error: ValidationError) -> str:
    """A tool's rejected arguments as one Chinese line: each missing or invalid field by name, and a validator's own
    sentence (“act 必填…”) as written — so the model fixes the call instead of guessing."""
    parts: list[str] = []
    for problem in error.errors()[:6]:
        field = ".".join(str(part) for part in problem.get("loc") or ()) or "参数"
        message = str(problem.get("msg") or "").removeprefix("Value error, ")
        parts.append(f"缺少 {field}" if problem.get("type") == "missing" else f"{field} 不合法：{message}")
    return "参数不对，没有执行：" + "；".join(dict.fromkeys(parts)) + "。按工具说明补齐或改正后再调一次。"


def proposed_intent(args: ProposeOrderArguments) -> OrderIntent:
    """propose_order's validated arguments as the OrderIntent the planner takes (one order, or its legs in order)."""
    if args.legs:
        legs = tuple(_leg_intent(leg, args.reason) for leg in args.legs)
        return OrderIntent(act=legs[0].act, legs=legs, reason=args.reason)
    assert args.act is not None  # the model's validator: one order, or legs
    return _leg_intent(args, args.reason)


def card_skill_block(store: Any, user_id: str | None, intent: OrderIntent) -> str | None:
    """The refusal line when a skill the intent's card acts need is unavailable to this user (order_intent.card_skills)."""
    from .skill_registry import skills_block_line
    return skills_block_line(store, user_id, card_skills(intent))


ONE_ORDER_AT_A_TIME_LINE = "这句话里有几单，请一单一单说，每单各自出卡确认"
# One tool call that made several cards (今仓全平几个合约): its `card` slot carries them all under this name; each is
# published as its own card (`card_events`), never as this envelope.
MULTI_CARD_EVENT = "proposal.cards"


def card_events(card: tuple[str, dict[str, Any]] | None) -> list[tuple[str, dict[str, Any]]]:
    """The cards one tool result publishes, in order: none, the one card, or each card of a multi-card result."""
    if card is None:
        return []
    if card[0] == MULTI_CARD_EVENT:
        return [(str(event), payload) for event, payload in card[1].get("cards") or []]
    return [card]


TOOL_ARGUMENT_MODELS: dict[str, type[ToolArguments]] = {
    "get_preferences": ToolArguments,
    "get_trade_journal": TradeJournalArguments,
    "get_review_picks": ReviewPicksArguments,
    "set_trade_note": TradeNoteArguments,
    "get_daily_review": DailyReviewArguments,
    "regenerate_daily_review": DailyReviewArguments,
    "answer_review_observation": ReviewObservationArguments,
    "create_price_alert": PriceAlertCreateArguments,
    "list_price_alerts": ToolArguments,
    "cancel_price_alert": PriceAlertIdArguments,
    "reject_proposal": RejectProposalArguments,
    "list_chart_drawings": ChartDrawingListArguments,
    "create_chart_drawing": ChartDrawingCreateArguments,
    "update_chart_drawing": ChartDrawingUpdateArguments,
    "delete_chart_drawing": ChartDrawingIdArguments,
    "clear_chart_drawings": ChartDrawingClearArguments,
    "update_preferences": UpdatePreferencesArguments,
    "remember": RememberArguments,
    "forget": ForgetArguments,
    "recall_memory": RecallArguments,
    "propose_routine": ProposeRoutineArguments,
    "list_routines": ToolArguments,
    "run_routine": RunRoutineArguments,
    "start_tour": StartTourArguments,
    "get_chart_bars": ChartBarsArguments,
    "highlight_ui": HighlightUiArguments,
    "annotate_chart": AnnotateChartArguments,
    "get_runtime_data_status": ToolArguments,
    "get_account_summary": ToolArguments,
    "get_positions": ToolArguments,
    "get_account_activity": ToolArguments,
    "search_instruments": InstrumentSearchArguments,
    "get_market_board": BoardArguments,
    "web_search": WebSearchArguments,
    "get_research_brief": ResearchBriefArguments,
    "get_news_flash": NewsFlashArguments,
    "get_foreign_markets": ForeignMarketsArguments,
    "get_economic_calendar": EconomicCalendarArguments,
    "get_interest_rates": ToolArguments,
    "get_settlement_prices": SettlementPricesArguments,
    "get_main_contract_history": MainContractHistoryArguments,
    "get_symbol_ranking": SymbolRankingArguments,
    "get_market_snapshot": MarketArguments,
    "get_market_analysis": MarketAnalysisArguments,
    "get_futures_decision_context": MarketAnalysisArguments,
    "run_reproducible_backtest": BacktestArguments,
    "run_tqsdk_historical_backtest": TqSdkBacktestArguments,
    "propose_order": ProposeOrderArguments,
    "estimate_order": OrderEstimateArguments,
    "create_strategy_draft": StrategyDraftArguments,
    "list_strategy_drafts": StrategyListArguments,
    "run_strategy_validation": StrategyValidationArguments,
    "get_ai_execution_status": ToolArguments,
    "resume_simulation_execution": ResumeSimulationArguments,
    "stop_simulation_execution": StopSimulationArguments,
    "execute_simulation_trade": AutonomousTradeArguments,
    "query_simulation_order": SimulationOrderArguments,
    "cancel_simulation_order": SimulationOrderArguments,
    "set_simulation_stop_loss": StopLossArguments,
    "cancel_simulation_stop_loss": StopLossIdArguments,
    "app_capabilities": CapabilityArguments,
    "app_call": AppCallArguments,
    "control_replay": ReplayControlArguments,
    "run_strategy_backtest": StrategyBacktestArguments,
    "list_strategy_templates": StrategyTemplateListArguments,
    "preview_strategy_template": StrategyTemplatePreviewArguments,
    "create_strategy_from_template": StrategyTemplateCreateArguments,
    "create_strategy_lab": StrategyLabCreateArguments,
    "get_strategy_lab": StrategyLabGetArguments,
    "choose_strategy_lab_variant": StrategyLabChooseArguments,
    "get_strategy_draft": StrategyIdArguments,
    "evaluate_strategy_draft": StrategyIdArguments,
    "explain_strategy_signal": ExplainStrategySignalArguments,
    "prepare_strategy_validation": StrategyIdArguments,
    "preview_strategy_intent": StrategyIdArguments,
    "get_strategy_monitor_status": StrategyIdArguments,
    "start_strategy_monitoring": StrategyStartArguments,
    "pause_strategy_monitoring": StrategyIdArguments,
}
# The catalog route each of these tools calls — the one app_call names for the same action. Declared once: the
# tool's handler below builds its AppCall from it, and agent_actions (AI 指令栏 "AI 做了 N 件事" / 撤销) reads it, so a
# dedicated tool and app_call land on the same per-route wording and undo.
TOOL_OPERATIONS: dict[str, str] = {
    "create_price_alert": "POST /api/market/alerts",
    "list_price_alerts": "GET /api/market/alerts",
    "cancel_price_alert": "DELETE /api/market/alerts/{alert_id}",
    "list_chart_drawings": "GET /api/market/drawings",
    "create_chart_drawing": "POST /api/market/drawings",
    "update_chart_drawing": "PUT /api/market/drawings/{drawing_id}",
    "delete_chart_drawing": "DELETE /api/market/drawings/{drawing_id}",
    "clear_chart_drawings": "DELETE /api/market/drawings",
    "create_strategy_draft": "POST /api/strategy-plans",
    "list_strategy_templates": "GET /api/strategy-templates",
    "preview_strategy_template": "POST /api/strategy-templates/{template_id}/preview",
    "create_strategy_from_template": "POST /api/strategy-templates/{template_id}/instantiate",
}

BOARD_ROW_VIEW_FIELDS = (
    "symbol", "exchangeId", "instrumentId", "instrumentName", "productName",
    "last", "change", "changePercent", "volume", "openInterest",
    "openInterestChange", "status",
)


def _board_payload(board: dict[str, Any], args: BoardArguments) -> dict[str, Any]:
    """Condensed 报价总表 for the AI: filtered by exchange, sorted, capped rows."""
    wanted = args.exchange.strip().upper()
    rows = [row for row in board.get("rows", []) if isinstance(row, dict)
            and (not wanted or str(row.get("exchangeId", "")).upper() == wanted)]

    def numeric(row: dict[str, Any]) -> float | None:
        value = row.get(args.sort)
        return float(value) if isinstance(value, (int, float)) else None

    rows.sort(key=lambda row: (
        numeric(row) is None,
        -(numeric(row) or 0.0) if args.order == "desc" else (numeric(row) or 0.0),
    ))
    return {
        "dataStatus": board.get("dataStatus"), "capturedAt": board.get("capturedAt"),
        "revision": board.get("revision"), "truncated": board.get("truncated"),
        "sort": args.sort, "order": args.order, "exchange": wanted or None,
        "rows": [{key: row.get(key) for key in BOARD_ROW_VIEW_FIELDS} for row in rows[:args.limit]],
    }


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str = Field(min_length=1)
    exchangeId: str
    instrumentId: str = Field(min_length=1)
    direction: Literal["long", "short"]
    volume: int = Field(gt=0)
    todayVolume: int = Field(default=0, ge=0)
    historicalVolume: int = Field(default=0, ge=0)
    frozenToday: int = Field(default=0, ge=0)
    frozenHistorical: int = Field(default=0, ge=0)
    openPrice: float | None = None
    # 持仓自己的最新价（#803，sidecar 与本站引擎同一字段）；旧版 sidecar 没有。
    lastPrice: float | None = None
    margin: float | None = None
    unrealizedPnl: float


# 外盘仓合并显示 (2026-10-21)：这两个读数在人民币本站模拟账户上带美元子账户那一半（`foreignAccount`）。
# 本站模拟两本账（2026-10-21）：账户摘要（权益、可用、保证金、今日）也带美元那一本。
FOREIGN_HALF_TOOLS = frozenset({"get_positions", "get_account_activity", "get_account_summary"})
FOREIGN_HALF_RULE = ("foreignAccount 是同一用户的美元子账户（外盘单都在这里成交），金额是美元：本站模拟是一个账户两本账，"
                     "报权益、持仓、委托、成交时两边都要报，外盘那部分写明美元，不要和人民币相加。")


class PositionsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataStatus: Literal["simulated", "normal", "stale"]
    positions: list[Position]


class TraderToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    readOnly: bool = True
    executionEnabled: bool = False
    data: dict[str, Any] | list[dict[str, Any]] | None = None
    warning: str | None = None
    errorCode: str | None = None


def _finite(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non_finite_market_value")
    return result


def _lab_table(lab: dict[str, Any]) -> dict[str, Any]:
    """get_strategy_lab 的简表：每组的样本内 / 样本外统计、衰减与标记，不带原始 plan 与逐笔。"""
    ranking = lab.get("ranking") or {}
    ranked = {str(item.get("variantId")): item for item in ranking.get("items") or []}

    def segment(side: dict[str, Any] | None) -> dict[str, Any] | None:
        return {key: side.get(key) for key in ("trades", "winRate", "net", "maxDrawdown")} if side else None

    rows: list[dict[str, Any]] = []
    for item in lab.get("results") or []:
        entry: dict[str, Any] = {"variantId": item.get("variantId"), "label": item.get("label", "")}
        if item.get("error"):
            entry["error"] = item["error"]
        else:
            entry["inSample"] = segment(item.get("inSample"))
            entry["outSample"] = segment(item.get("outSample"))
        rank = ranked.get(str(item.get("variantId"))) or {}
        entry.update({key: rank.get(key) for key in ("isRank", "oosRank", "decay", "flags")})
        rows.append(entry)
    # One fee schedule per lab: the first variant's 手续费口径 speaks for all of them.
    fee = next((item["feeEstimate"] for item in lab.get("results") or [] if item.get("feeEstimate")), None)
    return {"labId": lab["id"], "symbol": lab["symbol"], "status": lab["status"], "where": lab.get("source"),
            "tried": ranking.get("tried"), "best": ranking.get("best"), "noProfit": bool(ranking.get("noProfit")), "rows": rows,
            **({"feeEstimate": fee} if fee else {}),
            **({"chosen": lab["chosen"]} if lab.get("chosen") else {})}


_READINESS_REASON_LABELS = {
    "market_not_fresh": "当前行情不是最新状态",
    "analysis_not_fresh": "用于判断的K线数据不是最新状态",
    "instrument_catalog_not_fresh": "合约目录不是最新状态",
    "instrument_not_in_catalog": "没有在当前合约目录中找到该合约",
    "instrument_expiry_unknown": "无法确认合约到期日",
    "instrument_expired": "该合约已经到期",
    "account_not_fresh": "账户数据不是最新状态",
    "activity_not_fresh": "委托和成交数据不是最新状态",
    "orders_not_initialized": "委托数据尚未读取完成",
    "risk_status_not_normal": "风险检查当前未通过",
    "simulation_gateway_disabled": "模拟交易通道未开启",
    "simulation_gateway_unavailable": "模拟交易通道暂时不可用",
}


def _readiness_issue_labels(reasons: list[str]) -> list[str]:
    return [_READINESS_REASON_LABELS.get(reason, "有一项必要信息尚未就绪") for reason in reasons]


# 账户金额的口径说明（#794）：随 get_account_summary 一起给模型，报数直接用这些权威字段，
# 不从成交、交易日志加总，也不用“今日 − 浮动”反推（浮动是相对开仓价，持仓盈亏相对昨结算价）。
ACCOUNT_PNL_FIELDS = {
    "closeProfit": "今日平仓盈亏（元，账户口径、逐日盯市：平今对开仓价、平昨对昨结算价；不含手续费）",
    "positionProfit": "今日持仓盈亏（元，逐日盯市：今仓对开仓价、昨仓对昨结算价）",
    "floatProfit": "浮动盈亏（元，持仓相对开仓均价，持仓面板“浮动盈亏”）",
    "commission": "今日手续费（元，当日全部成交含开仓）",
    "todayNetPnl": "今日净盈亏（元）= closeProfit + positionProfit − commission，即页头“今日”",
}
ACCOUNT_PNL_RULE = (
    "用户问“今天平仓盈亏/已实现/平仓赚了多少”“今天手续费”“今天盈亏”“浮动多少”时，直接报 pnlFields 对应字段的数并说明口径"
    "（平仓盈亏不含手续费；扣费后要说“平仓盈亏 − 手续费”并给两个数）；不要从成交、get_trade_journal 逐笔加总，"
    "也不要用 todayNetPnl − floatProfit 推算。get_trade_journal 是按原开平价配对的完整交易，昨天开今天平的单子与账户"
    "平仓盈亏（对昨结算价）口径不同，只用来讲单笔交易。"
)
# 保护单状态（#794 P3）：effective=false 的已不在，讨论前先看这里。
PROTECTION_RULE = (
    "protections 是本账户的止损/止盈保护单：只有 effective=true 的仍挂着、会按价触发；effective=false（已撤销/已触发/"
    "已平/结果未知）的已经不在了。用户提到某个保护价时先在这里核对：不在 effective 列表里就直接说明它已经不在了"
    "（附 statusLabel/statusDetail），不要当作仍有效的止损讨论或建议改价；要重新保护请用户另设。"
)
INACTIVE_PROTECTION_LIMIT = 10
# 持仓/挂单/保护的现状（2026-10-13 夜盘 P1）：撤保护后 AI 凭上一轮的话说“只剩 3076 那条止损还生效”，
# 而它 22:30:54 已触发平仓。撤单、挂/撤保护的回报附上写完之后现场重读的这一份，回合里提到持仓/挂单/保护时
# 也附同一份（native_agent），模型陈述现状只能照它或本轮读工具。
ACCOUNT_STATE_WRITE_TOOLS = frozenset({"cancel_simulation_order", "set_simulation_stop_loss", "cancel_simulation_stop_loss",
                                       "stop_simulation_execution"})
ACCOUNT_STATE_RULE = (
    "这是系统在 asOf（北京时间 asOfLocal）现场读到的整个账户的持仓（positions）、仍挂着的委托（workingOrders）和"
    "仍生效的保护（protections，完整列出，列表里没有的保护就是已经不在了）。向用户说剩余持仓、挂单或保护时只按这里和"
    "本轮工具结果，不按之前对话里说过的；刚撤的那张委托以撤单工具的结果为准。"
)
# Account data that is not a live read: the post-write state fails closed instead of being shown as current.
ACCOUNT_STATE_NOT_LIVE = frozenset({"connecting", "unavailable", "backup", "stale"})
FOREIGN_LEDGER_RULE = ("ledger=USD 的行在同一用户的美元子账户（外盘本站模拟，金额是美元）：本站模拟是一个账户两本账，"
                       "报持仓、挂单、保护时两边都说，外盘那部分写明美元，不和人民币相加；平外盘仓直接点名那个外盘合约。")
FOREIGN_LEDGER_UNREADABLE_RULE = "美元子账户（外盘）这次读不到：不要说外盘没有持仓或挂单，只说外盘部分暂时读不到。"
ACCOUNT_STATE_UNAVAILABLE_RULE = "持仓/挂单/保护的现状这次读不到：不要陈述剩余持仓、挂单或保护，需要时请用户看持仓面板。"

DECISION_GATE_WARNING = "关键事实或风控门禁未就绪，只能解释缺失项，不能给出交易候选。"
# A card refusal that states a fact about the account / contract / price (没有可平的持仓, 合约不存在,
# 超出可平量…): the AI audit (2026-10-13) found the gate warning above on "没有可平的持仓" made the model
# retry three times and tell the user the account data was unreadable.
def _foreign_month(symbol: str | None) -> str | None:
    """一个点名了月份的外盘合约（``CME.GCEZ26`` 或裸的 ``GCEZ26``）在外盘引擎里的写法；快期外盘主连、品种词为 None。"""
    text = str(symbol or "").strip()
    if not text or text.upper().startswith("KQD."):
        return None
    candidate = text if "." in text else foreign_trade_symbol(text.upper())
    return candidate.upper() if foreign_product_of(candidate) is not None else None


def foreign_main_symbol(provider: Any, product: ForeignProduct) -> str | None:
    """美元子账户目录里这个外盘品种此刻的主力月份（``CME.GCEZ26``）；目录读不到或没有主力时为 None。"""
    try:
        items = provider.instrument_catalog().get("items") or []
    except Exception:
        return None
    return next((str(item["symbol"]) for item in items
                 if isinstance(item, dict) and item.get("foreignKey") == product.key and item.get("isMain")), None)


# 外盘二期：外盘单只在美元子账户撮合；它此刻装配不上（外盘引擎或 MT5 不通、还没开好）时如实说。
FOREIGN_ACCOUNT_UNAVAILABLE = "外盘在本站模拟的美元子账户下单，美元子账户现在连不上，外盘暂时不能下单。"
FOREIGN_NO_MAIN_LINE = "外盘这个品种现在取不到主力月份合约（MT5 行情不通），暂时不能下单。"
CARD_REFUSAL_WARNING = ("没有出卡：原因就是 userFacing.candidateIssues，按原话告诉用户；"
                        "这是账户、合约或价格的当前事实，不要改说成数据取不到，也不要用同样的参数重试。")
# 设置 → 交易 → AI 自主执行 is off for this account (user_preferences.trade.aiAutonomy).
AI_AUTONOMY_DISABLED_MESSAGE = (
    "AI 自主执行已关闭，只能生成待确认方案：改用 propose_order 出卡。"
)
# 4.3 执行归属与角色：不是负责人就出卡，viewer 只读。
NOT_EXECUTION_OWNER_MESSAGE = (
    "这个账户的 AI 自主执行由执行负责人管理，你不是负责人，只能生成待确认方案卡："
    "改用 propose_order 出卡。"
)
ACCOUNT_READ_ONLY_MESSAGE = "你对这个账户只有查看权限，不能下单、改单、挂止损或急停。"
# get_strategy_lab's wait cadence (a knob so tests can wait fast; production 2 s).
STRATEGY_LAB_POLL_SECONDS = 2.0
# Context-level failures: the same three codes for get_futures_decision_context and for the gate inside
# propose_order, so the agent loop treats both as "market not ready" (native_agent).
DECISION_CONTEXT_UNAVAILABLE = frozenset({
    "marketDataStatus_unavailable", "market_symbol_not_found", "invalid_or_unavailable_decision_context",
})



def _user_facing_decision_summary(
    *, analysis_reasons: list[str], proposal_reasons: list[str], gateway_reasons: list[str]
) -> dict[str, Any]:
    """Give the model stable Chinese copy instead of making it translate protocol enums."""
    return {
        "analysisStatus": "当前信息足够分析" if not analysis_reasons else "当前信息不足，暂时不能可靠分析",
        "candidateStatus": (
            "可以生成待确认方案" if not proposal_reasons else "暂时不能生成交易方案"
        ),
        "simulationStatus": (
            "模拟交易通道已就绪" if not gateway_reasons else "模拟交易通道当前不可用"
        ),
        "analysisIssues": _readiness_issue_labels(analysis_reasons),
        "candidateIssues": _readiness_issue_labels(proposal_reasons),
        "simulationIssues": _readiness_issue_labels(gateway_reasons),
        "safetyBoundary": "实盘功能未开启",
    }


def _market_analysis(workbench: dict[str, Any], *, lookback: int) -> dict[str, Any]:
    if (
        workbench.get("schemaVersion") != 1
        or workbench.get("liveTrading") != "disabled"
        # backup (Sina while TqKq is down) is analysed for research; the decision context
        # still marks it analysis_not_fresh and no proposal can follow from it.
        or workbench.get("dataStatus") not in {"simulated", "normal", "stale", "backup"}
        or not isinstance(workbench.get("quote"), dict)
        or not isinstance(workbench.get("bars"), list)
    ):
        raise ValueError("invalid_market_workbench")
    bars = [item for item in workbench["bars"] if isinstance(item, dict)][-lookback:]
    if len(bars) < 20:
        raise ValueError("insufficient_market_bars")
    closes = [_finite(item["close"]) for item in bars]
    highs = [_finite(item["high"]) for item in bars]
    lows = [_finite(item["low"]) for item in bars]
    volumes = [_finite(item["volume"]) for item in bars]
    interests = [_finite(item["openInterest"]) for item in bars]
    short_window = min(5, len(closes))
    long_window = min(20, len(closes))
    sma_short = sum(closes[-short_window:]) / short_window
    sma_long = sum(closes[-long_window:]) / long_window
    period_return = (closes[-1] / closes[0] - 1) * 100 if closes[0] else 0.0
    range_percent = ((max(highs) - min(lows)) / closes[0] * 100) if closes[0] else 0.0
    average_volume = sum(volumes) / len(volumes)
    volume_ratio = volumes[-1] / average_volume if average_volume else None
    tolerance = max(abs(closes[-1]) * 0.0001, 1e-9)
    trend = (
        "up"
        if sma_short > sma_long + tolerance and period_return > 0
        else "down"
        if sma_short < sma_long - tolerance and period_return < 0
        else "range"
    )
    quote = workbench["quote"]
    # BOLL(20,2)/ATR(14)/RSI(14)/CCI(14) latest values, the same definitions
    # tqsdk.ta uses (pinned by a parity test); the readings stay qualitative.
    bands = boll_series(closes, 20, 2)
    boll_latest = {name: round(values[-1], 6) if values[-1] is not None else None
                   for name, values in bands.items()}
    atr_values = [value for value in atr_series(highs, lows, closes, 14) if value is not None]
    atr_latest = atr_values[-1] if atr_values else None
    atr_baseline = (sum(atr_values) / len(atr_values)) if atr_values else None
    rsi_latest = rsi_series(closes, 14)[-1]
    cci_latest = cci_series(highs, lows, closes, 14)[-1]
    if boll_latest["top"] is not None and closes[-1] > boll_latest["top"]:
        boll_reading = "价格在布林上轨之上"
    elif boll_latest["bottom"] is not None and closes[-1] < boll_latest["bottom"]:
        boll_reading = "价格在布林下轨之下"
    else:
        boll_reading = "价格在布林带内运行"
    if atr_latest is not None and atr_baseline:
        atr_reading = (
            "波动较近期放大" if atr_latest > atr_baseline * 1.2
            else "波动较近期收敛" if atr_latest < atr_baseline * 0.8
            else "波动与近期相当"
        )
    else:
        atr_reading = "样本不足"
    rsi_reading = (
        "超买区" if rsi_latest is not None and rsi_latest > 70
        else "超卖区" if rsi_latest is not None and rsi_latest < 30
        else "中性区间" if rsi_latest is not None else "样本不足"
    )
    cci_reading = (
        "偏强" if cci_latest is not None and cci_latest > 100
        else "偏弱" if cci_latest is not None and cci_latest < -100
        else "中性" if cci_latest is not None else "样本不足"
    )
    return {
        "symbol": str(quote.get("symbol") or ""),
        "durationSeconds": int(workbench.get("durationSeconds") or 0),
        "capturedAt": workbench.get("capturedAt"),
        "ageSeconds": _finite(workbench.get("ageSeconds", 0)),
        "dataStatus": workbench.get("dataStatus"),
        "liveTrading": "disabled",
        "barCount": len(bars),
        "windowStart": bars[0].get("datetime"),
        "windowEnd": bars[-1].get("datetime"),
        "lastClose": closes[-1],
        "periodReturnPercent": round(period_return, 4),
        "rangePercent": round(range_percent, 4),
        "sma5": round(sma_short, 6),
        "sma20": round(sma_long, 6),
        "averageVolume": round(average_volume, 4),
        "latestVolume": volumes[-1],
        "volumeRatio": round(volume_ratio, 4) if volume_ratio is not None else None,
        "openInterestChange": round(interests[-1] - interests[0], 4),
        "averageBarRange": round(
            sum(high - low for high, low in zip(highs, lows, strict=True)) / len(bars), 6
        ),
        "indicators": {
            "boll": {**boll_latest, "reading": boll_reading},
            "atr": {"value": round(atr_latest, 6) if atr_latest is not None else None,
                    "reading": atr_reading},
            "rsi": {"value": round(rsi_latest, 4) if rsi_latest is not None else None,
                    "reading": rsi_reading},
            "cci": {"value": round(cci_latest, 6) if cci_latest is not None else None,
                    "reading": cci_reading},
        },
        "trend": trend,
        # 定性标签 (回答卡核对同一口径): the model quotes these instead of judging 放量 / 增仓 / 涨跌 itself.
        "labels": analysis_labels(volume_ratio=volume_ratio, open_interest_change=interests[-1] - interests[0],
                                  period_return=period_return, trend=trend, rsi_reading=rsi_reading),
        "method": "deterministic_ohlcv_oi_summary_v1",
    }


_MA_PERIODS = (5, 10, 25, 60, 200)

_MAIN_CONT_SYMBOL = re.compile(r"^KQ\.m@[A-Za-z]+\.[A-Za-z0-9]+$")


def _opened_today(activity: dict[str, Any], symbol: str, trading_day: str | None) -> int:
    """This account's opened volume for the contract today (offset=OPEN fills of
    the current trading day). The activity feed is the user's own account, so the
    sum is already per-account."""
    total = 0
    for trade in activity.get("trades") or []:
        if not isinstance(trade, dict):
            continue
        if str(trade.get("symbol") or "").upper() != symbol.upper():
            continue
        if str(trade.get("offset") or "").upper() != "OPEN":
            continue
        day = trade.get("tradingDay")
        if day is None:
            timestamp = trade.get("tradeDateTime")
            if not timestamp:
                continue
            day = datetime.fromtimestamp(
                int(timestamp) / 1e9, tz=timezone(timedelta(hours=8))
            ).date().isoformat()
        if trading_day is not None and str(day) != str(trading_day):
            continue
        total += int(trade.get("volume") or 0)
    return total


def _chart_bars(provider: ReadOnlyDataProvider, args: ChartBarsArguments, window: dict[str, Any] | None, *, workbench=None) -> dict[str, Any]:
    """The bars the user's chart shows, in the chart's own units (time in epoch seconds), with the
    moving averages the chart has switched on and the deterministic summary `get_market_analysis` uses."""
    state = (window or {}).get("state") or {}
    symbol = args.symbol or state.get("symbol")
    duration = args.durationSeconds or state.get("duration")
    if not symbol or not duration:
        raise ValueError("窗口没有合约图表，请指定 symbol 与 durationSeconds")
    if workbench is None:
        workbench = provider.market_workbench(str(symbol), int(duration))
    if workbench.get("schemaVersion") != 1 or not isinstance(workbench.get("bars"), list):
        raise ValueError("invalid_market_workbench")
    every = [item for item in workbench["bars"] if isinstance(item, dict)]
    closes = [_finite(item["close"]) for item in every]
    chart = state.get("chart") or {}
    # 图表指标可配置 (2026-10-02): the account's chart.maLines names the periods the user's chart
    # draws; a client before that (or a guest window) still reports the ma5…ma200 switches.
    preferences = state.get("preferences")
    configured = preferences.get("chart", {}).get("maLines") if isinstance(preferences, dict) else None
    if isinstance(configured, list) and configured:
        periods = sorted({int(line["period"]) for line in configured
                          if isinstance(line, dict) and isinstance(line.get("period"), int) and line.get("enabled", True)})
    else:
        periods = [p for p in _MA_PERIODS if chart.get(f"ma{p}", True)]
    averages: dict[int, list[float | None]] = {}
    for period in periods:
        values: list[float | None] = []
        total = 0.0
        for index, close in enumerate(closes):
            total += close
            if index >= period:
                total -= closes[index - period]
            values.append(round(total / period, 4) if index >= period - 1 else None)
        averages[period] = values
    context = ((window or {}).get("observation") or {}).get("context") or {}
    viewport = context.get("chartViewport") or {}
    time_range = viewport.get("timeRange") if isinstance(viewport, dict) else None
    source = "recent"
    selected = list(range(len(every)))
    if args.range == "visible" and isinstance(time_range, dict) and time_range.get("from") is not None and time_range.get("to") is not None:
        low, high = float(time_range["from"]), float(time_range["to"])
        visible = [i for i, item in enumerate(every) if low <= int(item["datetime"]) // 1_000_000_000 <= high]
        if visible:
            selected, source = visible, "visible"
    selected = selected[-args.limit:]
    from zoneinfo import ZoneInfo
    shanghai = ZoneInfo("Asia/Shanghai")
    bars = []
    for index in selected:
        item = every[index]
        seconds = int(item["datetime"]) // 1_000_000_000
        bars.append({
            "time": seconds,
            "datetime": datetime.fromtimestamp(seconds, tz=shanghai).strftime("%Y-%m-%d %H:%M"),
            "open": _finite(item["open"]), "high": _finite(item["high"]), "low": _finite(item["low"]), "close": _finite(item["close"]),
            "volume": _finite(item.get("volume", 0)), "openInterest": _finite(item.get("openInterest", 0)),
            "ma": {str(period): averages[period][index] for period in periods},
        })
    try:
        analysis = _market_analysis(workbench, lookback=max(20, min(120, len(every))))
    except ValueError:
        analysis = None
    quote = workbench.get("quote") or {}
    return {
        "symbol": str(symbol), "durationSeconds": int(duration), "dataStatus": workbench.get("dataStatus"),
        "capturedAt": workbench.get("capturedAt"), "priceTick": quote.get("priceTick"), "lastPrice": quote.get("lastPrice"),
        "range": {"source": source, "from": bars[0]["time"] if bars else None, "to": bars[-1]["time"] if bars else None,
                  "loadedFrom": int(every[0]["datetime"]) // 1_000_000_000 if every else None,
                  "loadedTo": int(every[-1]["datetime"]) // 1_000_000_000 if every else None},
        "timeUnit": "epoch_seconds", "barCount": len(bars), "bars": bars,
        "movingAverages": {str(period): averages[period][-1] if averages[period] else None for period in periods},
        "analysis": analysis,
        "windowId": (window or {}).get("windowId"),
    }


def _shown_panes(state: dict[str, Any]) -> set[str]:
    """The panes the window's chart currently draws (main plus its sub panes): the account's
    chart.subPaneList when the window reports preferences, else the legacy kd/macd switches."""
    panes = {"main"}
    preferences = state.get("preferences")
    configured = preferences.get("chart", {}).get("subPaneList") if isinstance(preferences, dict) else None
    if isinstance(configured, list) and configured:
        for item in configured:
            name = item.get("type") if isinstance(item, dict) else item
            if name in {"kd", "macd", "rsi", "oi", "cci", "volume"}:
                panes.add(str(name))
    else:
        chart = state.get("chart") or {}
        if chart.get("kd", True):
            panes.add("kd")
        if chart.get("macd", True):
            panes.add("macd")
    return panes


# M2+registry (2026-09-24): which native tools a user may see is owned by the
# skill registry (`skill_registry.hidden_tool_names`) — a tool belongs to
# exactly one SKILL.md, and a user without that skill (globally disabled,
# tier-withheld or owner-only) never sees it, whether the turn reaches it
# through the general loop, a routed subset or the capability catalog
# (`app_call`).


class TraderReadOnlyTools:
    """Validated data, proposal and bounded delegated-simulation tools."""

    NAMES = {
        "get_runtime_data_status", "get_account_summary", "get_positions",
        "get_account_activity", "search_instruments", "get_market_snapshot",
        "get_market_analysis", "get_futures_decision_context",
    }

    def _simulation_mode_allowed(self, mode: str) -> bool:
        return mode == "local" or bool(self.autonomy and self.autonomy.supports_mode(mode))

    def __init__(
        self, provider: ReadOnlyDataProvider,
        runtime_status: Callable[[str], dict[str, Any]],
        backtests: BacktestService | None = None,
        proposals: ProposalService | None = None,
        autonomy: AutonomousSimulationService | None = None,
        web_search: WebSearchClient | None = None,
        research_brief: ResearchBriefService | None = None,
        research_live: ResearchLiveService | None = None,
    ) -> None:
        self.provider = provider
        self.runtime_status = runtime_status
        self.backtests = backtests
        self.proposals = proposals
        self.autonomy = autonomy
        self.web_search = web_search
        self.research_brief = research_brief
        self.research_live = research_live
        self.app_capabilities = None
        self.replay_context = None
        # The trading account this set's provider reads (set by whoever binds it to one: the native adapter, the card
        # routes); None = not bound, the cards follow the conversation's account as before.
        self.account_id: str | None = None
        self.NAMES = set(type(self).NAMES)
        if web_search is not None:
            self.NAMES.add("web_search")
        if research_brief is not None:
            self.NAMES.add("get_research_brief")
        if research_live is not None:
            self.NAMES.update({"get_news_flash", "get_foreign_markets", "get_economic_calendar", "get_interest_rates"})
        if backtests is not None:
            self.NAMES.add("run_reproducible_backtest")
            if callable(getattr(provider, "historical_backtest", None)):
                self.NAMES.add("run_tqsdk_historical_backtest")
        # The three TqSdk A-tier reads: present only when the provider serves them
        # (the sidecar and the fake fixture do; the in-process TqKq provider does not).
        if callable(getattr(provider, "symbol_settlements", None)):
            self.NAMES.add("get_settlement_prices")
        if callable(getattr(provider, "main_contract_history", None)):
            self.NAMES.add("get_main_contract_history")
        if callable(getattr(provider, "symbol_ranking", None)):
            self.NAMES.add("get_symbol_ranking")
        if callable(getattr(provider, "market_board", None)):
            self.NAMES.add("get_market_board")
        if proposals is not None:
            self.NAMES.update({
                "propose_order", "estimate_order",
                "get_strategy_draft", "list_strategy_drafts",
                "evaluate_strategy_draft", "explain_strategy_signal", "prepare_strategy_validation",
                "preview_strategy_intent", "get_strategy_monitor_status",
            })
            if (
                backtests is not None
                and callable(getattr(provider, "historical_backtest", None))
            ):
                self.NAMES.add("run_strategy_validation")
        if autonomy is not None and autonomy.enabled:
            self.NAMES.update({
                "get_ai_execution_status",
                "resume_simulation_execution",
                "stop_simulation_execution",
                "execute_simulation_trade",
                "query_simulation_order",
                "cancel_simulation_order",
                "set_simulation_stop_loss",
                "cancel_simulation_stop_loss",
                "start_strategy_monitoring",
                "pause_strategy_monitoring",
            })

    # 按技能延迟挂载 (2026-10-14 token 瘦身): a general (or voice fast) turn does not carry these until their
    # owning skill is mounted for the session — activate_skill, the sentence's own words (the skill's
    # mount-words) or an earlier call; native_agent unmounts a skill two turns after its last use. Every
    # definition carries the flag from here (`deferLoading`); routed turns keep their fixed subsets.
    DEFERRED_TOOL_NAMES = frozenset({
        "get_settlement_prices", "get_main_contract_history", "get_symbol_ranking", "get_market_board",
        "get_foreign_markets", "get_economic_calendar", "get_interest_rates",
        "run_reproducible_backtest", "run_tqsdk_historical_backtest", "create_strategy_draft", "list_strategy_drafts",
        "get_strategy_draft", "evaluate_strategy_draft", "explain_strategy_signal", "prepare_strategy_validation",
        "run_strategy_validation", "preview_strategy_intent", "get_strategy_monitor_status",
        "start_strategy_monitoring", "pause_strategy_monitoring", "run_strategy_backtest", "list_strategy_templates",
        "preview_strategy_template", "create_strategy_from_template", "create_strategy_lab", "get_strategy_lab",
        "choose_strategy_lab_variant", "estimate_order", "remember", "forget", "recall_memory",
        "propose_routine", "list_routines", "run_routine", "get_trade_journal", "get_review_picks",
        "get_daily_review", "regenerate_daily_review", "answer_review_observation", "set_trade_note",
        "create_price_alert", "list_price_alerts", "cancel_price_alert", "list_chart_drawings",
        "create_chart_drawing", "update_chart_drawing", "delete_chart_drawing", "clear_chart_drawings",
        "start_tour", "control_replay", "execute_simulation_trade", "query_simulation_order",
        "get_ai_execution_status", "resume_simulation_execution",
    })

    # The voice fast path (a spoken "how much is rebar now?") runs with these tools only: reads that
    # answer directly, plus search and the capability catalog for the odd read-only app call (app_call can
    # also write, so a fast turn's app_call is held to read-only operations where it runs: native_agent).
    READ_ONLY_TOOL_NAMES = frozenset({
        "get_account_summary", "get_market_snapshot", "get_market_analysis", "get_positions", "get_account_activity",
        "get_futures_decision_context", "get_runtime_data_status", "search_instruments", "query_simulation_order",
        "get_ai_execution_status", "get_strategy_monitor_status", "list_strategy_drafts", "get_strategy_draft",
        "app_capabilities", "app_call", "web_search", "get_chart_bars", "get_preferences",
        "get_research_brief", "get_settlement_prices", "get_main_contract_history", "get_symbol_ranking",
        "get_market_board", "get_trade_journal", "get_review_picks", "get_daily_review", "recall_memory", "list_routines", "estimate_order",
        "get_news_flash", "get_foreign_markets", "get_economic_calendar", "get_interest_rates",
    })
    # UI-only tools next to app_call: authenticated like it, never behind the operation lock.
    UI_TOOL_NAMES = frozenset({"get_chart_bars", "highlight_ui", "annotate_chart", "start_tour"})
    # The account's preferences (GET/PUT /api/preferences through the catalog): same authentication, no lock.
    # 规则回测 goes through its catalog route the same way: a read-only computation, no operation lock.
    # 策略模板库 (2026-09-25): list / preview (a backtest) / create through the template routes, likewise.
    # 保存“我的策略” (create_strategy_draft, #932 F5) is the editor's own save route, POST /api/strategy-plans, likewise.
    TEMPLATE_TOOL_NAMES = frozenset({"list_strategy_templates", "preview_strategy_template", "create_strategy_from_template",
                                     "create_strategy_draft"})
    # 策略实验室 (2026-09-30): open a comparison, wait for its table, save the winner — the lab routes, no lock.
    LAB_TOOL_NAMES = frozenset({"create_strategy_lab", "get_strategy_lab", "choose_strategy_lab_variant"})
    # 交易日志 (2026-09-29): read the journal, write the user's 补充 — the review page's routes, no lock.
    JOURNAL_TOOL_NAMES = frozenset({"get_trade_journal", "get_review_picks", "set_trade_note",
                                    # 今日复盘 (2026-10-06): read, regenerate, answer the 习惯 observation.
                                    "get_daily_review", "regenerate_daily_review", "answer_review_observation"})
    # 私人记忆 (3a, 2026-09-29): remember / forget / recall through /api/memory, likewise no lock.
    MEMORY_TOOL_NAMES = frozenset({"remember", "forget", "recall_memory"})
    # 我的流程 (2026-10-06): 流程卡 / list / run through /api/routines, likewise no lock.
    ROUTINE_TOOL_NAMES = frozenset({"propose_routine", "list_routines", "run_routine"})
    # 到价提醒 (2026-10-02): set / list / cancel through /api/market/alerts — an alert only ever
    # reminds, so nothing here takes the operation lock either.
    PRICE_ALERT_TOOL_NAMES = frozenset({"create_price_alert", "list_price_alerts", "cancel_price_alert"})
    # The one card tool (意图重构第 3 步): the model fills an OrderIntent, the order planner (order_planner.plan) makes
    # the cards, asks, or refuses.
    ORDER_CARD_TOOLS = frozenset({"propose_order"})
    # 用户画线 (2026-10-02; 画线第二版 2026-10-09 adds change and clear): list / create / update /
    # delete / clear through /api/market/drawings — a drawing only ever marks the chart, so nothing
    # here takes the operation lock either.
    CHART_DRAWING_TOOL_NAMES = frozenset({"list_chart_drawings", "create_chart_drawing", "update_chart_drawing",
                                          "delete_chart_drawing", "clear_chart_drawings"})
    PREFERENCE_TOOL_NAMES = (frozenset({"get_preferences", "update_preferences", "run_strategy_backtest"})
                             | TEMPLATE_TOOL_NAMES | LAB_TOOL_NAMES | JOURNAL_TOOL_NAMES | MEMORY_TOOL_NAMES
                             | ROUTINE_TOOL_NAMES)

    def read_only_names(self) -> set[str]:
        return {definition["name"] for definition in self.definitions() if definition["name"] in self.READ_ONLY_TOOL_NAMES}

    def _account_word(self) -> str:
        """The account word tool descriptions name: 本站模拟 on a paper-backed
        deployment, 快期模拟 otherwise (the shared default provider)."""
        return "本站模拟" if self.provider.mode == "paper" else "快期模拟"

    def _market_is_synthetic(self) -> bool:
        """行情是不是合成假数据：只有假数据提供方（mode fake）是。快期只读（tqkq-readonly，含回放）
        与本站模拟（paper，撮合引擎读真实快期行情）都是真实行情——账户模拟不等于行情假（#822）。"""
        return self.provider.mode == "fake"

    def definitions(self) -> list[dict[str, Any]]:
        account_description = (
            "读取经 Runtime 校验的 TqKq 模拟账户摘要与快照时效。只读、不能下单；数据陈旧时明确标记。"
            if self.provider.mode == "tqkq-readonly"
            else "读取本站模拟撮合账户摘要与快照时效。只读、不能下单；数据陈旧时明确标记。"
            if self.provider.mode == "paper"
            else "读取合成假账户摘要与快照时效。只读；数据陈旧时明确标记。"
        ) + ("今天平仓盈亏、持仓盈亏、浮动盈亏、手续费、今日净盈亏都用这里返回的 pnlFields 原值报数并说明口径，"
             "不要从成交或交易日志加总，也不要用“今日 − 浮动”推算。")
        market_description = (
            "读取指定合约经 Runtime 校验的只读行情快照与时效。价格来自 TqSdk/TqKq 当前期货行情源；"
            "账户仍为模拟账户，委托与撤单能力关闭。contract 带合约乘数/每跳/每手保证金手续费/交易时段，"
            "deliveryNotice 带个人账户交割月最后可持仓日与剩余交易日——“一手多少钱”“什么时候要平仓”用它答。"
            "contract.commissionPerLot 与下单票、estimate_order 同一个估算（元/手，开仓一手按现价），commissionSource 标来源。"
            if not self._market_is_synthetic()
            else "读取指定合约的合成假行情快照与时效；不得描述为真实行情。"
            "contract 带合约乘数/每跳/每手保证金手续费/交易时段，deliveryNotice 带交割月最后可持仓日与剩余交易日。"
            "contract.commissionPerLot 与 estimate_order 同一个估算（元/手）。"
        )
        definitions = [
            {"type": "function", "name": "get_runtime_data_status",
             "description": "读取 Runtime、Agent、行情、账户、交易网关和风险的独立状态。只读。",
             "inputSchema": TOOL_ARGUMENT_MODELS["get_runtime_data_status"].model_json_schema()},
            {"type": "function", "name": "get_account_summary",
             "description": account_description,
             "inputSchema": TOOL_ARGUMENT_MODELS["get_account_summary"].model_json_schema()},
            {"type": "function", "name": "get_positions",
             "description": "读取完整交易所合约代码、方向、数量和浮动盈亏。只读。",
             "inputSchema": TOOL_ARGUMENT_MODELS["get_positions"].model_json_schema()},
            {"type": "function", "name": "get_account_activity",
             "description": (f"读取{self._account_word()}账户当前委托和成交事实、初始化状态与快照时间，"
                             "以及止损/止盈保护单的当前状态 protections（effective=true 才是仍挂着的）。"
                             "用户提到某个止损/保护价时先读它核对，已失效的直接说已经不在了。只读，不能撤单。"),
             "inputSchema": TOOL_ARGUMENT_MODELS["get_account_activity"].model_json_schema()},
            {"type": "function", "name": "search_instruments",
             "description": (
                 "从官方 TqSdk 合约目录搜索当前未到期的实际交割合约和主力映射。"
                 "返回最多 20 项；主连代码只用于发现，交易候选必须使用实际交割合约。"
             ),
             "inputSchema": TOOL_ARGUMENT_MODELS["search_instruments"].model_json_schema()},
            {"type": "function", "name": "get_market_snapshot",
             "description": market_description,
             "inputSchema": TOOL_ARGUMENT_MODELS["get_market_snapshot"].model_json_schema()},
            {"type": "function", "name": "get_market_analysis",
             "description": (
                 "读取指定实际交割合约的最近 K 线，并由 Runtime 确定性计算涨跌、区间、"
                 "5/20 均价、量比、持仓量变化和趋势。只读；这是行情事实摘要，不是收益预测。"
                 "labels 是 Runtime 判定的放量缩量、增减仓、区间涨跌、趋势、RSI 定性，回答照抄（回答卡上写成对应的定性键）；区间涨跌不是当日涨跌，当日涨跌看行情快照 dayMove（对昨结算）。"
                 "解释走势、指标（KD/MACD/均线）、形态这类“讲图”问题用它（配合 get_chart_bars），秒级返回；不要为此调 get_futures_decision_context。"
             ),
             "inputSchema": TOOL_ARGUMENT_MODELS["get_market_analysis"].model_json_schema()},
            {"type": "function", "name": "get_futures_decision_context",
             "description": (
                 "只在回答方向判断或风险评估前调用（汇总账户与持仓，耗时较长）；单纯解释走势或指标用 get_market_analysis；用户要开仓/平仓时不要先调它——propose_order 自带同一套就绪检查，直接出卡。"
                 "Runtime 一次性汇总并校验运行状态、合约、当前行情、确定性 K 线摘要、"
                 "账户、持仓和活动委托，并附带可直接用于回答用户的中文就绪说明。只读，"
                 "不会创建候选或提交委托；任一关键事实陈旧或缺失时失败关闭。"
             ),
             "inputSchema": TOOL_ARGUMENT_MODELS["get_futures_decision_context"].model_json_schema()},
        ]
        if self.web_search is not None:
            definitions.append({
                "type": "function", "name": "web_search",
                "description": (
                    "联网搜索实时信息（新闻、政策、宏观事件、当前日期等），由 Runtime 执行真实搜索并返回"
                    "标题、链接、摘要与发布时间。需要核验时效性行情判断或最新消息时调用；不是行情工具，"
                    "价格与持仓必须用 get_futures_decision_context 等正式行情工具。"
                ),
                "inputSchema": TOOL_ARGUMENT_MODELS["web_search"].model_json_schema(),
            })
        if self.research_brief is not None:
            definitions.append({
                "type": "function", "name": "get_research_brief",
                "description": (
                    "期货研究信息统一入口（只读）。topic：position_rank 持仓排名（主力席位）、warehouse_re"
                    "ceipt 仓单、spot_basis 现货与基差、inventory（实为交易所注册仓单；真实库存看 desk 的"
                    "上期所周报）、term_structure 期限结构、fees_margin 保证金与手续费参考——问这几项用对应主"
                    "题，不要联网；news 消息面与为什么涨跌（第三方报道）；desk 调研汇总：主力走势、关联品种、基本面与产业数据、"
                    "相关宏观、期货公司观点，事实带 id/来源/日期，tiles 是关键数据格，news 是新闻面一行，另附共享 AI "
                    "判断（points/risks 带 direction，conclusion 一句结论，change 比上一研究日，"
                    "stale 表示之后有新消息、可能过时；judgment.status 非 ready 时只有事实）。综合分析、问怎"
                    "么看先读 desk，复述时事实与判断分开，报道说明是媒体说法。实时价格、涨跌、成交量、持仓量用行情工具。各条带 so"
                    "urce/status/asOf：stale 是上次成功的旧值，unavailable 只说暂缺（爬虫源缺失是常态）"
                    "，不用记忆补数。"
                ),
                "inputSchema": TOOL_ARGUMENT_MODELS["get_research_brief"].model_json_schema(),
            })
        if self.research_live is not None:
            definitions.append({
                "type": "function", "name": "get_news_flash",
                "description": (
                    "读取最近的市场快讯（财联社、东方财富、新浪、金十、华尔街见闻、交易所公告、统计局/发改委等二十余源，跨源去重，新→旧"
                    "，只读）。tags 里“据:新华社”一类是原始来源。product 传品种（SHFE.rb 或 rb）只看与它相关的"
                    "：模型逐条判断怎么影响该品种，同一件事合成 events（eventLabel、publishedAt、title、"
                    "ai{event, direction 偏多/偏空/中性/分歧, strength 弱/中/强, reason, t"
                    "ransmission}、tier major 重要/other 其他/minor 次要、reports 几家报道、"
                    "items 原文与链接）；brief 是今日新闻要点（net、take、points、watch，status re"
                    "ady/stale/writing/none），tonight 是今晚相关数据；screen.pending 是还在"
                    "筛的条数，screen.mode=keyword 表示未经 AI 筛选。officialOnly=true 只看官方"
                    "源（hours 最多 720），macroOnly=true 只看宏观，hours 默认 24。问最近快讯、某品种新"
                    "闻用本工具，不要联网。快讯是第三方信息、未经核实，复述时说明是快讯；ai 与 brief 是对新闻的解读，不是交易建"
                    "议；status 非 ok 时只说暂缺，不用记忆补数。"
                ),
                "inputSchema": TOOL_ARGUMENT_MODELS["get_news_flash"].model_json_schema(),
            })
            definitions.append({
                "type": "function", "name": "get_foreign_markets",
                "description": (
                    "读取外盘参考行情（伦铜等 LME 金属、COMEX 金银铜、WTI/布伦特原油、美豆、美棉、马棕油等主连，只读，与报价页“外盘”同一来源）。"
                    "product 传国内品种（如 SHFE.sc）只看它对应的外盘；省略看全部。问“外盘怎么样”“伦铜夜里涨了吗”时用本工具。"
                    "每条的 source 是它取自的数据源，unit 是报价单位；changePercent 为空表示该源没有昨结算价，不要自己推算。"
                    "外盘交易单位与国内合约不同，不能直接比价；没有对应外盘的国内品种返回空；status=stale 是上次取到的价格。"
                ),
                "inputSchema": TOOL_ARGUMENT_MODELS["get_foreign_markets"].model_json_schema(),
            })
            definitions.append({
                "type": "function", "name": "get_economic_calendar",
                "description": (
                    "读取经济数据与事件日历（未来几天，中国/美国/欧元区，重要性≥中，只读，后台 6 小时缓存）。"
                    "每条带日期时间、地区、预期与前值。product 传品种（SHFE.au 或 au）只看与它有关的数据（贵金属看美联储/美国通胀就业/美债美元，"
                    "能源化工看 EIA/API/OPEC，农产品看 USDA/天气，黑色与有色看中国 PMI/地产/基建，股指国债看中国宏观与央行），otherCount 是其余条数。"
                    "问“这周有什么重要数据”“非农什么时候出”时用本工具；"
                    "status 为 loading 是后台还在拉取，请稍后再读。"
                ),
                "inputSchema": TOOL_ARGUMENT_MODELS["get_economic_calendar"].model_json_schema(),
            })
            definitions.append({
                "type": "function", "name": "get_interest_rates",
                "description": (
                    "读取关键利率（只读）：中国 10 年期国债、美国 10 年期国债、美债 10Y-2Y 利差、"
                    "SHIBOR 隔夜与 1 周、LPR 1 年与 5 年，每项带最新有值的日期。"
                    "问“美债收益率多少”“SHIBOR 多少”“LPR 是多少”时用本工具，不要联网。"
                ),
                "inputSchema": TOOL_ARGUMENT_MODELS["get_interest_rates"].model_json_schema(),
            })
        if "get_settlement_prices" in self.NAMES:
            definitions.append({
                "type": "function", "name": "get_settlement_prices",
                "description": (
                    "读取合约每日结算价（官方 TqSdk query_symbol_settlement，只读）。"
                    "当日结算价约 19:00 后产生，之前返回最近已产生的交易日并在 asOf 标明。"
                    "问“结算价多少/上一交易日结算价/近几天结算价”时用本工具；主连不支持，请先换成实际交割合约。"
                ),
                "inputSchema": TOOL_ARGUMENT_MODELS["get_settlement_prices"].model_json_schema(),
            })
        if "get_main_contract_history" in self.NAMES:
            definitions.append({
                "type": "function", "name": "get_main_contract_history",
                "description": (
                    "读取主连合约近 n 个交易日每天对应的实际主力合约，并标出换月日期"
                    "（官方 TqSdk query_his_cont_quotes，只读）。问“主力什么时候换月/过去一个月主力是哪份合约”时用；"
                    "symbol 传品种（rb）或主连（KQ.m@SHFE.rb）均可。"
                ),
                "inputSchema": TOOL_ARGUMENT_MODELS["get_main_contract_history"].model_json_schema(),
            })
        if "get_symbol_ranking" in self.NAMES:
            definitions.append({
                "type": "function", "name": "get_symbol_ranking",
                "description": (
                    "读取合约成交/持仓排名前 20 席位（龙虎榜：期货公司、本期值、增减），"
                    "官方 TqSdk query_symbol_ranking，只读。rankingType：VOLUME 成交量 / LONG 多头持仓 / SHORT 空头持仓。"
                    "问“主力席位/龙虎榜/某某期货公司在什么位置”时用本工具，不要联网搜。"
                ),
                "inputSchema": TOOL_ARGUMENT_MODELS["get_symbol_ranking"].model_json_schema(),
            })
        if "get_market_board" in self.NAMES:
            definitions.append({
                "type": "function", "name": "get_market_board",
                "description": (
                    "读取报价总表：全部品种主力合约的最新价、涨跌幅（对昨结算）、成交量、持仓量和增仓"
                    "（每品种一行，只读）。可按涨跌幅/成交量/持仓量/增仓排序、按交易所筛选。"
                    "问“今天哪些品种涨得最多”“持仓量最大的几个”时用本工具；要看某个合约的 K 线或细节用 get_market_analysis。"
                ),
                "inputSchema": TOOL_ARGUMENT_MODELS["get_market_board"].model_json_schema(),
            })
        if self.backtests is not None:
            definitions.append({
                "type": "function", "name": "run_reproducible_backtest",
                "description": "从不可变策略与数据快照运行确定性回测并生成可审计制品；不连接交易网关。",
                "inputSchema": TOOL_ARGUMENT_MODELS["run_reproducible_backtest"].model_json_schema(),
            })
        if "run_tqsdk_historical_backtest" in self.NAMES:
            definitions.append({
                "type": "function",
                "name": "run_tqsdk_historical_backtest",
                "description": (
                    "使用官方 TqSdk 3.10.2 的 TqBacktest 历史行情与独立 TqSim 本地账户，"
                    "比较量价趋势策略的两个成交量阈值。不会访问 TqKq 委托账户、不会部署策略。"
                ),
                "inputSchema": TOOL_ARGUMENT_MODELS["run_tqsdk_historical_backtest"].model_json_schema(),
            })
        if self.proposals is not None:
            definitions.extend([
                {
                    "type": "function", "name": "list_strategy_drafts",
                    "description": "列出当前用户最近保存的策略草案和回测状态。只读。",
                    "inputSchema": TOOL_ARGUMENT_MODELS["list_strategy_drafts"].model_json_schema(),
                },
                {
                    "type": "function", "name": "get_strategy_draft",
                    "description": "读取当前用户的一张策略草案、参数和风险边界。只读。",
                    "inputSchema": TOOL_ARGUMENT_MODELS["get_strategy_draft"].model_json_schema(),
                },
                {
                    "type": "function", "name": "evaluate_strategy_draft",
                    "description": (
                        "用当前已完成K线确定性检查一张已保存策略草案的条件，返回买入、卖出或等待信号。"
                        "只读，不回测、不启动策略、不创建候选、不提交委托。"
                    ),
                    "inputSchema": TOOL_ARGUMENT_MODELS["evaluate_strategy_draft"].model_json_schema(),
                },
                {
                    "type": "function", "name": "explain_strategy_signal",
                    "description": (
                        "用户问“按我的策略现在该怎么做”“策略怎么看”“现在能开多吗”时用它：按当前数据"
                        "（回放中按回放时钟）解读一张策略的信号——结论（开多/开空/平多/平空/观望）、"
                        "每条条件的满足情况与差距、参考入场价和策略止损止盈，并把入场点、止损止盈线、"
                        "关键条件位画到图上（①②③+短标签，回复正文按编号讲解）。只读：不下单、不出方案卡；"
                        "用户说“那就下”时才用 propose_order 出卡。未验证的草案也能解读，结果标“未验证”。"
                        "多张监控中策略时返回候选，先问用户用哪一张。"
                    ),
                    "inputSchema": TOOL_ARGUMENT_MODELS["explain_strategy_signal"].model_json_schema(),
                },
                {
                    "type": "function", "name": "prepare_strategy_validation",
                    "description": (
                        "为一张已保存策略草案建立正式验证报告并进入验证中。只完成基础结构检查，"
                        "未来数据偏差、样本外、成本、保护器和信号数量仍必须由后续历史回放提供证据。"
                        "不会把策略标记为可运行，不会创建委托。"
                    ),
                    "inputSchema": TOOL_ARGUMENT_MODELS["prepare_strategy_validation"].model_json_schema(),
                },
                {
                    "type": "function", "name": "preview_strategy_intent",
                    "description": (
                        "为已验证通过的策略生成一次只读意图快照：已完成K线信号、当前仓位目标和"
                        "确定性风险裁剪。只保存审计快照，不启动循环、不创建委托。"
                    ),
                    "inputSchema": TOOL_ARGUMENT_MODELS["preview_strategy_intent"].model_json_schema(),
                },
                {
                    "type": "function", "name": "get_strategy_monitor_status",
                    "description": ("读取策略监控状态、保护器状态和最近模拟执行尝试，以及策略的预算占用与"
                                   "回撤停机读数（funds / drawdown，与 HTTP 的 monitor 载荷同一份；"
                                   "drawdown.canResume 与恢复前提同源）。只读。"),
                    "inputSchema": TOOL_ARGUMENT_MODELS["get_strategy_monitor_status"].model_json_schema(),
                },
            ])
            if "run_strategy_validation" in self.NAMES:
                definitions.append({
                    "type": "function", "name": "run_strategy_validation",
                    "description": (
                        "用官方 TqSdk 历史K线对已保存策略做逐根回放，分离样本内与样本外，"
                        "并计入滑点、费用、止损、止盈、冷却和每日开仓上限。只有全部检查通过时"
                        "才标记为验证通过但未启动；绝不创建委托。"
                    ),
                    "inputSchema": TOOL_ARGUMENT_MODELS["run_strategy_validation"].model_json_schema(),
                })
            definitions.append({
                "type": "function", "name": "propose_order",
                "description": (
                    "唯一的出卡工具：把用户这句下单的话填成下单意图，Runtime 的下单规划器读取整账户、算合约月份、开平与方向、手数"
                    "（差额、平一半）、平今平昨、价格并做就绪检查，出待用户本人确认的卡（一张或按顺序多张），或返回要追问的一句、"
                    "不出卡的中文原因（userFacing）。只写用户说了的：没说的方向、手数、合约留空，Runtime 会追问，不要猜，也不要先读持仓、"
                    "行情或 K 线来算参数。只是问、转述已做的事、条件单（到价/到时间才下）、或用户自己收回时不调用。"
                    "撤单、改保护用各自的工具；本工具不下单、不撤单。返回里的合约、方向、手数、价格就是卡上的数，回答按它们复述。"
                ),
                "inputSchema": TOOL_ARGUMENT_MODELS["propose_order"].model_json_schema(),
            })
            definitions.append({
                "type": "function", "name": "estimate_order",
                "description": (
                    "下单前问“钱”的只读估算：开一手某合约大约要多少保证金和手续费、可用资金最多能开几手；"
                    "平仓大约释放多少保证金。回答“开一手螺纹要多少保证金”“我最多还能开几手”“平掉这手能腾出多少钱”"
                    "时用本工具，不要自己拿保证金率心算。commissionPerLot（每手开仓手续费）与 fee（本次手数合计）就是下单票上的数，"
                    "units 给出各字段单位，commissionSource 是 market_source 时说明是行情源参考值；"
                    "返回 basis 说明是交易所标准估算、实际以成交后为准；"
                    "缺数据的字段为 null 并带 missing 原因，如实说明，不编数。只读：不出卡、不下单、不拦截委托。"
                ),
                "inputSchema": TOOL_ARGUMENT_MODELS["estimate_order"].model_json_schema(),
            })
        if self.autonomy is not None and self.autonomy.enabled:
            definitions.extend([
                {
                    "type": "function", "name": "start_strategy_monitoring",
                    "description": (
                        "启动一张已正式验证通过的策略进行后台模拟监控（监控中再调用＝修改执行方式）。只读取已完成K线；"
                        "signalAction 决定信号怎么处理：alert 只提醒、confirm 出确认卡由用户确认后下单、auto 通过保护器时"
                        "直接创建模拟委托（AI 自主执行关闭时按 confirm 处理）。resetDrawdown=true 时重置回撤停机并开新一轮"
                        "（前提不满足会被拒绝并说明原因；普通启动绝不重置，停机中的一轮保持停止不开新仓）。"
                        "提醒与确认卡发到本对话；实盘始终关闭。"
                    ),
                    "inputSchema": TOOL_ARGUMENT_MODELS["start_strategy_monitoring"].model_json_schema(),
                },
                {
                    "type": "function", "name": "pause_strategy_monitoring",
                    "description": "立即暂停策略产生新的模拟执行意图；既有委托仍按原订单身份回读。",
                    "inputSchema": TOOL_ARGUMENT_MODELS["pause_strategy_monitoring"].model_json_schema(),
                },
                {
                    "type": "function", "name": "get_ai_execution_status",
                    "description": (
                        "读取当前模拟自动执行状态和可配置限制。只读；模拟动作需要时由 Runtime"
                        "自动建立执行租约，不向用户请求逐单授权。"
                    ),
                    "inputSchema": TOOL_ARGUMENT_MODELS["get_ai_execution_status"].model_json_schema(),
                },
                {
                    "type": "function", "name": "resume_simulation_execution",
                    "description": (
                        "仅在用户明确说恢复或继续 AI 模拟执行时，解除此前的普通停止或急停状态。"
                        "恢复本身不下单、不重启已暂停策略，也不恢复急停时取消的止损或委托。"
                    ),
                    "inputSchema": TOOL_ARGUMENT_MODELS["resume_simulation_execution"].model_json_schema(),
                },
                {
                    "type": "function", "name": "stop_simulation_execution",
                    "description": (
                        "仅在用户要现在停住 AI 的模拟执行（“停下”“先别动了”“急停”）时调用。普通停止拦 AI 一切新动作"
                        "并立即暂停所有策略监控，已挂止损止盈仍执行；scope=opens 只停新开仓、不暂停策略；急停另撤 AI 委托并"
                        "作废止损。用户说的是以后的做法（“以后别让 AI 自己下单”“都先问我”“下单前先给我看”）不是停止："
                        "用 update_preferences 把 trade.aiAutonomy 设为 false（之后只出待确认卡，策略监控照常），不调本工具。"
                    ),
                    "inputSchema": TOOL_ARGUMENT_MODELS["stop_simulation_execution"].model_json_schema(),
                },
                {
                    "type": "function", "name": "execute_simulation_trade",
                    "description": (
                        "AI 自主执行专用：用户明确要 AI 自己交易/自动执行/按策略执行时才用。用户本人说的“买一手/空两手/平掉/全平”"
                        "一律用 propose_order 出卡让用户确认，不用本工具。"
                        "提交一张 TqKq 模拟限价开仓或减少持仓委托；默认没有平台手数或次数上限。"
                        "Runtime 重新读取行情、账户、持仓、冻结量和策略里用户写的预算，"
                        "生成唯一 clientOrderId，并最多调用一次 insert_order。结果 UNKNOWN 时绝不重发。"
                    ),
                    "inputSchema": TOOL_ARGUMENT_MODELS["execute_simulation_trade"].model_json_schema(),
                },
                {
                    "type": "function", "name": "query_simulation_order",
                    "description": "查询本人一张既有 TqKq 模拟委托的交易所事实；不会重新提交。",
                    "inputSchema": TOOL_ARGUMENT_MODELS["query_simulation_order"].model_json_schema(),
                },
                {
                    "type": "function", "name": "cancel_simulation_order",
                    "description": (
                        "在活动授权范围内撤销本人一张仍可撤的 TqKq 模拟委托。"
                        "只调用一次 cancel_order，结果未知时仅查询原委托。"
                    ),
                    "inputSchema": TOOL_ARGUMENT_MODELS["cancel_simulation_order"].model_json_schema(),
                },
                {
                    "type": "function", "name": "set_simulation_stop_loss",
                    "description": (
                        "为本人当前 TqKq 模拟持仓设置一个持久化、只减少持仓的确定性止损/止盈"
                        "（“给我的螺纹多单设个止损 3100”只传 triggerPrice；“止盈 3200”只传 takeProfitPrice，"
                        "两个至少一个）。方向要成立：多仓 止损<现价<止盈，空仓相反，反了会收到中文原因。"
                        "触发后只创建一张平仓委托，不追价、不撤单重报。"
                    ),
                    "inputSchema": TOOL_ARGUMENT_MODELS["set_simulation_stop_loss"].model_json_schema(),
                },
                {
                    "type": "function", "name": "cancel_simulation_stop_loss",
                    "description": "取消本人一条尚未触发的模拟盘止损。",
                    "inputSchema": TOOL_ARGUMENT_MODELS["cancel_simulation_stop_loss"].model_json_schema(),
                },
            ])
        if self.app_capabilities is not None:
            definitions.extend([
                {"type": "function", "name": "app_capabilities",
                 "description": "读取本软件的开放能力目录（按技能分组的操作与界面命令字段索引）。再复制 operation 原文查完整参数，"
                                "或用 query 只看相关的几项；不要把 operation 写成实际URL，也不要查询本工具自身。已读过的定义在本次连接内复用。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["app_capabilities"].model_json_schema()},
                {"type": "function", "name": "app_call",
                 "description": "调用已发现的业务接口。requestJson 必须是 JSON 编码的字符串；operation 保留 {占位符}，路径值放 path，查询参数放 query，请求放 body。界面命令按目录 uiControl.index 的字段写，拿不准某字段时用 app_capabilities 的 operation=ui:<字段>；多项外观用 changes.presentation 一次提交。query.waitMs=3000 等待真实回执，applied.state 已确认所改字段，observedRevision 可连续使用；需要读取新内容或布局时再读页面。其他操作先查询定义，不猜参数，不越过核心权限。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["app_call"].model_json_schema()},
                {"type": "function", "name": "get_chart_bars",
                 "description": "读取用户图表上的 K 线：当前合约与周期（或指定）、可见区间或最近 N 根 OHLCV、已开启均线的逐根数值和确定性走势摘要。"
                                "返回的 time 是秒级 bar 时间戳，annotate_chart 的 time/time2 和 chartFocus 直接用它。讲走势前先读这一份。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["get_chart_bars"].model_json_schema()},
                {"type": "function", "name": "highlight_ui",
                 "description": "在用户界面上指给用户看：给一个界面目标（按钮、面板、区域）加脉冲/描边高亮和一句气泡说明（最多同时 3 个，超时或用户点击即消失）。"
                                "target 用窗口快照 context.highlightTargets 里的 id；只用于界面元素，价位、区间、进场点用 annotate_chart 画在图上；不触发任何交易。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["highlight_ui"].model_json_schema()},
                {"type": "function", "name": "annotate_chart",
                 "description": "在用户图表上画 AI 标注：主图或当前打开的副图（kd/macd/rsi/持仓量，跟随用户的副图设置）；多空入场、止损、止盈、平仓用 point（精确到一个点，role 决定三角/方块/圆点），"
                                "压力/支撑用 hline 或 zone，趋势 trendline，时刻 vline，副图背离/超买用 point/hline/text。同一屏最多 6 条，每条 ≤8 字短标签；"
                                "图上只显示编号 ①②③ + 短标签，解释写在回复正文里按编号讲。讲完不要自动清除；用户可右键“清空图上标记”。可选 focus 把图滚到指定时间段。画的是观点，不是委托。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["annotate_chart"].model_json_schema()},
                {"type": "function", "name": "get_preferences",
                 "description": "读取本账号的偏好设置（语音：音色/语速/判停/确认窗口/播报；图表：默认周期/K线样式/涨跌色/均线与副图指标（chart.maLines/boll/subPaneList）/AI 标注；"
                                "交易：默认手数/默认价格意图/平今平昨/语音免确认/闪电下单；AI 对话：回答风格/先读屏；通知；数据保留天数）。"
                                "用户问“我的设置是什么/现在语速是多少”时用它。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["get_preferences"].model_json_schema()},
                {"type": "function", "name": "update_preferences",
                 "description": "修改本账号偏好（部分更新，返回全量），不用先确认。口语到键：说话快/慢→voice.speed fast/slow；回答短/详细→chat.replyStyle concise/detailed；以后默认两手→trade.defaultVolume 2；直接成交/别挂单→trade.defaultPriceIntent aggressive，挂单等→passive；别念简讯→notify.desktop.briefs false（AI 简讯一个开关管全部渠道）；以后别让 AI/策略自己下单、都先问我→trade.aiAutonomy false（自动下单的策略改为出卡，不停策略监控；对话里说的单本来就只出卡），让策略自动下单→true；语音下单不用说确认→trade.voiceOrderConfirm false，要确认→true；两个方向用户说了就改。成交播报→notify.desktop.fills true（成交一个开关管全部渠道；voice.announceFills 已退役不生效）；换男声/女声→voice.voice 取 GET /api/voice/realtime/voices 的 id（男声小天 zh_male_xiaotian_jupiter_bigtts）；判停/确认时限→voice.endpointStallSeconds / confirmWindowSeconds。保本提醒门槛→notify.breakEvenRule：mode multiple（multiple 0.5–5 倍风险）或 amount（amount 元，整数）；外盘美元子账户的美元门槛 amountUsd（整数美元，只在 amount 时用；null＝外盘按 multiple，用户没说就别设）。图表指标先 get_preferences 读当前值再改：chart.maLines 整组替换如 [{period:5},{period:10},{period:20}]（最多 8 条、周期 2–500、不重复，关一条用 enabled:false）；布林 chart.boll.enabled/period/width；副图 chart.subPaneList 整组替换如 [\"rsi\"]（kd/macd/rsi/oi/cci/volume，最多 3 个）；chart.rsiParams.period、chart.cciParams.period；SAR chart.sar.enabled/step(0.01–0.1)/max(0.1–0.5)。越界整体拒绝并说明原因。主题、字号、布局不在这里，用界面命令。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["update_preferences"].model_json_schema()},
                {"type": "function", "name": "remember",
                 "description": "把用户亲口说的事记进他的私人记忆（画像/约定/经验三层）。只在用户明确说“记住…”“以后…”“别再…”"
                                "或清楚陈述自己的交易习惯、限制、对 AI 的要求时用；quote 必须原样填用户这句话（服务端会核对话记录，对不上拒绝）。"
                                "约定只记用户要求 AI 怎么做事；“以后我说『X』就帮我做这几步”是我的流程，用 propose_routine，不是记忆；任何要你自动下单、免确认、替他交易的话都不要记——那不是记忆，"
                                "是设置：用 update_preferences 改 trade.aiAutonomy / trade.voiceOrderConfirm。记忆只作参考，永远不构成授权。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["remember"].model_json_schema()},
                {"type": "function", "name": "forget",
                 "description": "删掉一条记忆（用户说“忘掉…”“那条不对”时）：先用 recall_memory 找到那条的 id 再删。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["forget"].model_json_schema()},
                {"type": "function", "name": "recall_memory",
                 "description": "读取用户的私人记忆（画像/约定/经验）。用户问“你记得我什么”“我之前说过什么”时用；"
                                "需要更多他的历史经验和习惯时也可以读。只读。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["recall_memory"].model_json_schema()},
                {"type": "function", "name": "propose_routine",
                 "description": "我的流程：把用户说的一串做法（“以后我说『老规矩』就帮我……”）按顺序拆成步骤，出一张流程卡发到对话。"
                                "只出卡、不保存：用户点卡上的“保存”才存，不要说已经存好；改已有流程（“老规矩去掉回测那步”）带 routineId 写改完的完整步骤。"
                                "最多 10 步、每人最多 5 个流程（超了如实转告）。名称或步骤含免确认下单、自动下单、改风险设置、自主执行开关时卡会被拒绝"
                                "（没有保存按钮），照实转告原因，不要替用户改写。不要在这一轮执行这些步骤。"
                                "步骤文本保留用户原话，不得自行追加“需要就设 / 有必要就设”这类执行承诺：提醒、画线、盯异动步骤只说“看看要不要 / 有没有必要”时运行时只给建议、不会设，"
                                "原话带“需要就设”这类从句时才是需要时执行；返回的 steps[].manner 就是卡上的标注，回复按它说，不要把只给建议说成会设。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["propose_routine"].model_json_schema()},
                {"type": "function", "name": "list_routines",
                 "description": "列出用户的“我的流程”（名称、每一步的类型和内容、上次运行时间）。用户问“我有哪些流程”“老规矩是哪几步”时用；只读，不运行。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["list_routines"].model_json_schema()},
                {"type": "function", "name": "run_routine",
                 "description": "运行用户的一个流程（排在本轮之后，在本对话里按步骤做完，要下单的步骤只出卡）。只在用户明确要求运行时用，"
                                "quote 填用户这句原话（须含流程名）；用户整句就是流程名或“跑一下X”时 Runtime 已经直接运行，不用调用。不要自己启动流程。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["run_routine"].model_json_schema()},
                {"type": "function", "name": "get_trade_journal",
                 "description": "读取交易日志：最近平仓的完整交易（开平价、持仓分钟、盈亏及口径），每笔的来源（手动 / AI 建议 / AI 自主 / 策略名 / 止损）、"
                                "理由（AI 建议卡或 AI 自主单的理由，策略单是策略名）、用户的补充 note（noteScopeChanged 为 true 表示补充写在别的手数上，转述要说明），以及开仓那天调研员对该品种的判断 deskView；"
                                "响应另带 unlinkedNotes（找不到对应交易的补充）。"
                                "用户问“我这手做得合理吗”“最近亏在哪”“复盘一下”时先读它；事实照它讲，你的评价明确说是你的判断。"
                                "复盘美元历史时传 currency=USD；按界面的历史范围传 days（账户复盘为366天），不要拿只含当日成交的activity空表声称历史为空。"
                                "它按原开平价逐笔配对，不是账户“今天平仓盈亏”：问今天平仓/手续费/今日盈亏用 get_account_summary 的 pnlFields，不要把这里的逐笔相加。只读。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["get_trade_journal"].model_json_schema()},
                {"type": "function", "name": "get_review_picks",
                 "description": "引导复盘：按固定规则挑出最值得聊的最多 3 笔已平仓交易（亏得最多的、和当天调研判断方向相反的、比自己平时拿盈利单久得多的亏损单；"
                                "已写过补充的跳过），每笔带一句要问的话 question。用户说“复盘一下 / 复盘这周”时用它：一次只问一笔，用 question 的问法，"
                                "他回答后用 set_trade_note 照原意记下，再问下一笔；他说跳过就下一笔，说不聊了就停。没有 picks 时如实说没有需要聊的。只读。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["get_review_picks"].model_json_schema()},
                {"type": "function", "name": "get_daily_review",
                 "description": "读取今日复盘（每个交易日 15:15 有成交才生成；外盘美元子账户在美东 17:15 CME 收盘后生成、金额是美元）：每笔成交的时间、动作、来源、理由、标签（计划内 / 提醒后 / 临时，"
                                "按成交来源和提醒时间判定）、平仓盈亏，每笔程序查出的问题 checks（提前离场、连亏后再开、没标交易笔记、亏过止损、没设止损、不合入场规则）和要问他的一句 question（他回答后用 set_trade_note 记到这笔的 roundId），四格统计、两三句总结；一周最后一个交易日还有“本周最该改的一件事” observation（status pending 待用户答）。"
                                "用户问“今天复盘”“今天怎么样”时先读它，照它讲，不自己算数字、不自己加问题。只读。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["get_daily_review"].model_json_schema()},
                {"type": "function", "name": "regenerate_daily_review",
                 "description": "按现在的成交重新生成某个交易日（默认今天）的复盘并覆盖（收盘后又有成交、用户说“重新复盘今天”时用）；不推送，不下单。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["regenerate_daily_review"].model_json_schema()},
                {"type": "function", "name": "answer_review_observation",
                 "description": "回答复盘里的“本周最该改的一件事”：remember 记住（写进经验记忆）只在用户明确说“记住这条”之类时用，quote 必须原样填他这句话；"
                                "dismiss 不对在用户说“不对 / 不是这样”时用。不要替用户决定。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["answer_review_observation"].model_json_schema()},
                {"type": "function", "name": "set_trade_note",
                 "description": "把用户对某笔交易说的话记成这笔的补充（复盘补写，覆盖原来的一条）。只在用户自己说了当时的想法、理由或教训时用，"
                                "照他的原意记一句，不要替他编理由或写你的评价。roundId 用 get_trade_journal 返回的 id。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["set_trade_note"].model_json_schema()},
                {"type": "function", "name": "create_price_alert",
                 "description": "设一条到价提醒：合约涨到/跌到目标价时提醒用户（对话消息＋桌面通知＋通话口播，只提醒不下单）。"
                                "用户说“螺纹涨到 3150 提醒我”“跌破 3100 告诉我”时用；价格必须在该合约的最小变动价位上，"
                                "方向可省（按最新价自动判定）。返回已设内容与现价；拒绝时把中文原因原话转告。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["create_price_alert"].model_json_schema()},
                {"type": "function", "name": "list_price_alerts",
                 "description": "列出用户当前生效的到价提醒和最近已触发的（每条带合约、方向、价格、备注）。用户问“我有哪些提醒”“设了几个提醒”时用。只读。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["list_price_alerts"].model_json_schema()},
                {"type": "function", "name": "cancel_price_alert",
                 "description": "删掉一条生效中的到价提醒。用户说“删掉螺纹的提醒”“那条不要了”时先 list_price_alerts 找到 id 再删。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["cancel_price_alert"].model_json_schema()},
                {"type": "function", "name": "list_chart_drawings",
                 "description": "列出用户自己画在图上的线和图形（趋势线、水平线、通道、区间、黄金分割、矩形、文字等 20 种，按合约存、跨设备同步，与 AI 标注是两层）。"
                                "每条带 id、样式和一句话回执；价位类的线带现价相对位置（如“现价 3112，在 3100 上方 12”），黄金分割/百分比/江恩带各档价位。"
                                "用户问“我画的线被突破了吗”“我在这个合约标过哪些价位”“黄金分割 61.8 在哪”时先读它。只读。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["list_chart_drawings"].model_json_schema()},
                {"type": "function", "name": "create_chart_drawing",
                 "description": "替用户在图上画一条线或图形（存服务端、跨设备同步），可带颜色/粗细/线型/延长等样式。"
                                "只在用户明确要画时用（“帮我在 3150 画一条线”“把 3100 到 3130 标成区间”“从这波低点到高点画个黄金分割”）；时间拿不准先 get_chart_bars。"
                                "自己讲解行情时的临时标注仍用 annotate_chart，不要混。返回画好的线与回执；拒绝时把中文原因原话转告。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["create_chart_drawing"].model_json_schema()},
                {"type": "function", "name": "update_chart_drawing",
                 "description": "改用户画的一条线：换颜色/粗细/线型、延长、锁定/解锁、隐藏/显示、只在本周期显示，或把它移到新坐标、改标签。"
                                "先 list_chart_drawings 拿 id；style 只写要改的键。锁定的线要移动先解锁（style.locked=false 与 points 同一次可以）。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["update_chart_drawing"].model_json_schema()},
                {"type": "function", "name": "delete_chart_drawing",
                 "description": "删掉用户画的一条线。用户说“把我画的 3100 那条线删了”“那条趋势线不要了”时先 list_chart_drawings 找到 id 再删。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["delete_chart_drawing"].model_json_schema()},
                {"type": "function", "name": "clear_chart_drawings",
                 "description": "清空用户在一个合约上画的全部线（只删这个合约的，别的合约不动；不影响 AI 标注）。只在用户明确说“删掉所有画线/全部清空”时用。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["clear_chart_drawings"].model_json_schema()},
                {"type": "function", "name": "start_tour",
                 "description": "带用户分步看软件：按顺序打开表面并高亮目标（每步一句气泡说明，可带 say 供语音逐步播报），底部出现“第 n/N 步”指示器，"
                                "用户点上一步/下一步/结束或说“下一步”“结束”；用户说“带我看看/教我用这个软件/怎么下单”时用它生成课程（默认七步课程见 GET /api/app/help 的 tour 主题，可按问题裁剪）。"
                                "只是指引，不触发交易。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["start_tour"].model_json_schema()},
                {"type": "function", "name": "run_strategy_backtest",
                 "description": "用用户自己的规则回测历史（与上线运行同一套判定：收盘确认、下一根开盘成交，计手续费）。"
                                "strategyId 回测已保存的策略；plan 回测未保存的改后版本。修改策略时各跑一次，用“改前 → 改后”对比。只读，不下单。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["run_strategy_backtest"].model_json_schema()},
                {"type": "function", "name": "list_strategy_templates",
                 "description": "列出经典策略模板（分类、一句话场景、不带术语的大白话 plainSummary、新手/进阶、日内/会隔夜、来源、推荐周期、"
                                "可调参数的默认值与范围及“是什么 / 调大 / 调小”说明 help）。给用户推荐起点时先用它，向新手介绍时用 plainSummary 和 help 的说法。只读。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["list_strategy_templates"].model_json_schema()},
                {"type": "function", "name": "preview_strategy_template",
                 "description": "按参数实例化一个模板（不保存）并在合约上规则回测，结果卡发到对话；返回实例化的策略与回测摘要。"
                                "参数越界时如实转告原因。只读，不下单。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["preview_strategy_template"].model_json_schema()},
                {"type": "function", "name": "create_strategy_draft",
                 "description": "把用户的策略存成一条“我的策略”草稿：完整 StrategyPlan，走策略编辑卡同一个保存接口（POST /api/strategy-plans），"
                                "存好就出现在策略面板“我的策略”里，能看、能改、能删。修改已有策略带 baseStrategyId，存为它的新版本，"
                                "原版本与运行中的实例不变。只保存：不回测、不上线、不下单；上线由用户决定。返回 strategyId。"
                                "从经典模板开始用 create_strategy_from_template。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["create_strategy_draft"].model_json_schema()},
                {"type": "function", "name": "create_strategy_from_template",
                 "description": "用户同意后，按模板与参数存成一条“我的策略”草稿（与自己设计的策略完全相同，上线仍由用户决定）。返回 strategyId。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["create_strategy_from_template"].model_json_schema()},
                {"type": "function", "name": "create_strategy_lab",
                 "description": "开一次策略实验室：同一合约并行对比最多 12 组变体（模板参数各档，或改前 / 改后的 plan），"
                                "前 70% 历史挑、后 30% 做样本外检验，在云端算。返回 labId，用 get_strategy_lab 等结果。只读历史，不下单。"
                                "只比一条已存策略的关键参数时，用结果页同一接口 POST /api/strategy-plans/{id}/compare 更省事。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["create_strategy_lab"].model_json_schema()},
                {"type": "function", "name": "get_strategy_lab",
                 "description": "读策略实验室的对比结果（create_strategy_lab 的 labId）：每组的样本内 / 样本外笔数、胜率、净利、回撤、"
                                "衰减与标记（best 样本内最佳 / overfit 过拟合风险 / negative_oos 样本外亏损 / too_few 样本太少）。"
                                "noProfit 为 true 时各组在样本内都没赚钱、没有推荐的一组，如实告诉用户，不要硬挑一组推荐。"
                                "没算完时最多等 waitSeconds 秒。只读。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["get_strategy_lab"].model_json_schema()},
                {"type": "function", "name": "choose_strategy_lab_variant",
                 "description": "用户同意后，把实验室里留下的那组存为“我的策略”：服务端先用规则回测同一口径重跑一遍权威回测再保存"
                                "（本机算的数字只用于对比）。返回 strategyId 与回测摘要；上线仍由用户决定。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["choose_strategy_lab_variant"].model_json_schema()},
                {"type": "function", "name": "control_replay",
                 "description": "控制用户当前的历史回放（没有回放时报 replay_not_found）：play 播放/继续、pause 暂停、step 单步（一步一根完整的 1 分钟线）、"
                                "speed 调每秒步数 1/2/5/10、pause_entries/resume_entries 暂停/恢复历史信号注入、finish 收尾结算、close 关闭。"
                                "走回放窗口同一条控制接口；回放不能按时间跳，只能播放、暂停、单步和调这四档速度。",
                 "inputSchema": TOOL_ARGUMENT_MODELS["control_replay"].model_json_schema()},
            ])
            if self.proposals is not None:
                definitions.append({
                    "type": "function", "name": "reject_proposal",
                    "description": ("作废一张还在等用户确认的方案卡（下单卡、批量平仓卡、目标持仓卡、价差卡），等于用户点卡上的“取消”："
                                    "走卡片按钮同一个接口，卡变为已作废、不下单。只在用户明确说取消/算了/不要了/放弃这张卡时用，"
                                    "proposalId 与 proposalVersion 照抄系统说明里的待确认卡；返回 status=rejected 才算作废成功，"
                                    "被拒时按原因照实说明。没有确认下单的工具：确认只能用户自己点卡上的“确认”或在语音里说“确认”。"),
                    "inputSchema": TOOL_ARGUMENT_MODELS["reject_proposal"].model_json_schema()})
        for definition in definitions:
            definition["deferLoading"] = definition["name"] in self.DEFERRED_TOOL_NAMES
        return definitions

    # -- account preferences ---------------------------------------------------

    async def _preference_tool(self, tool: str, arguments: Any, *, user_id: str, session_id: str | None):
        """GET/PUT /api/preferences through the capability bridge: the same route the UI and the CLI use."""
        from .agent_api import AppCall
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            args = TOOL_ARGUMENT_MODELS[tool].model_validate(parsed)
        except (ValidationError, ValueError, TypeError):
            return self._unavailable(tool, "invalid_tool_arguments"), None
        read_only = tool == "get_preferences"
        call = AppCall(operation="GET /api/preferences") if read_only else AppCall(operation="PUT /api/preferences", body=args.changes)
        result = await self.app_capabilities.call(call, user=user_id, session_id=session_id)
        if result.get("ok"):
            return self._response(TraderToolResult(tool=tool, readOnly=read_only, executionEnabled=False, data=result["data"]), True), None
        detail = str((result.get("data") or {}).get("detail") or "设置读写失败")
        return self._response(TraderToolResult(tool=tool, readOnly=read_only, warning=detail), False), None

    async def _journal_tool(self, tool: str, arguments: Any, *, user_id: str, session_id: str | None):
        """交易日志 through the catalog routes the review page and the CLI use: GET /api/account/journal and
        PUT /api/account/trade-notes/{round_id} (via "chat": the AI wrote the user's words down)."""
        from .agent_api import AppCall
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            args = TOOL_ARGUMENT_MODELS[tool].model_validate(parsed)
        except (ValidationError, ValueError, TypeError):
            return self._unavailable(tool, "invalid_tool_arguments"), None
        read_only = tool in {"get_trade_journal", "get_review_picks", "get_daily_review"}
        if tool == "get_daily_review":
            assert isinstance(args, DailyReviewArguments)
            call = AppCall(operation="GET /api/account/daily-review", query={"tradingDay": args.tradingDay} if args.tradingDay else {})
        elif tool == "regenerate_daily_review":
            assert isinstance(args, DailyReviewArguments)
            call = AppCall(operation="POST /api/account/daily-review/regenerate", body={"tradingDay": args.tradingDay} if args.tradingDay else {})
        elif isinstance(args, ReviewObservationArguments):
            call = AppCall(operation="POST /api/account/daily-review/observation",
                           body={"tradingDay": args.tradingDay, "verdict": args.verdict, "observationKey": args.observationKey,
                                 "revision": args.revision, **({"quote": args.quote} if args.quote else {})})
        elif isinstance(args, ReviewPicksArguments):
            call = AppCall(operation="GET /api/account/review-picks", query={"scope": args.scope})
        elif isinstance(args, TradeJournalArguments):
            call = AppCall(operation="GET /api/account/journal",
                           query={"days": args.days, "limit": args.limit, **({"symbol": args.symbol} if args.symbol else {}),
                                  **({"currency": args.currency} if args.currency else {})})
        else:
            assert isinstance(args, TradeNoteArguments)
            call = AppCall(operation="PUT /api/account/trade-notes/{round_id}", path={"round_id": args.roundId},
                           body={"text": args.text, "via": "chat"})
        result = await self.app_capabilities.call(call, user=user_id, session_id=session_id)
        if result.get("ok"):
            return self._response(TraderToolResult(tool=tool, readOnly=read_only, executionEnabled=False, data=result["data"]), True), None
        detail = str((result.get("data") or {}).get("detail") or ("交易日志暂不可用" if read_only else "补充没有记下"))
        return self._response(TraderToolResult(tool=tool, readOnly=read_only, warning=detail), False), None

    async def _memory_tool(self, tool: str, arguments: Any, *, user_id: str, session_id: str | None):
        """私人记忆 through the catalog routes the settings page and the CLI use: POST /api/memory
        (via "chat": the AI wrote the user's own words down), DELETE /api/memory/{memory_id},
        GET /api/memory."""
        from .agent_api import AppCall
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            args = TOOL_ARGUMENT_MODELS[tool].model_validate(parsed)
        except (ValidationError, ValueError, TypeError):
            return self._unavailable(tool, "invalid_tool_arguments"), None
        read_only = tool == "recall_memory"
        if tool == "remember":
            body: dict[str, Any] = {"layer": args.layer, "text": args.text, "quote": args.quote, "via": "chat"}
            if args.products:
                body["products"] = args.products
            call = AppCall(operation="POST /api/memory", body=body)
        elif tool == "forget":
            query: dict[str, Any] = {"via": "chat"}
            if args.reason:
                query["reason"] = args.reason
            call = AppCall(operation="DELETE /api/memory/{memory_id}", path={"memory_id": args.memoryId},
                           query=query)
        else:
            assert isinstance(args, RecallArguments)
            call = AppCall(operation="GET /api/memory", query={"includeInvalid": str(args.includeInvalid)})
        result = await self.app_capabilities.call(call, user=user_id, session_id=session_id)
        if not result.get("ok"):
            detail = str((result.get("data") or {}).get("detail") or "记忆读写失败")
            return self._response(TraderToolResult(tool=tool, readOnly=read_only, warning=detail), False), None
        data = result["data"]
        if tool == "recall_memory" and args.layer:
            data = {**data, "items": [item for item in data.get("items", []) if item.get("layer") == args.layer]}
        return self._response(TraderToolResult(tool=tool, readOnly=read_only, executionEnabled=False, data=data), True), None

    async def _routine_tool(self, tool: str, arguments: Any, *, user_id: str, session_id: str | None, turn_id: str):
        """我的流程 through the catalog routes the settings page and the CLI use: POST /api/routines/drafts
        (the 流程卡 — published into this conversation under this turn by the route itself; the user's 保存
        writes the routine), GET /api/routines, POST /api/routines/{routine_id}/run (with the user's words)."""
        from .agent_api import AppCall
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            args = TOOL_ARGUMENT_MODELS[tool].model_validate(parsed)
        except (ValidationError, ValueError, TypeError):
            return self._unavailable(tool, "invalid_tool_arguments"), None
        read_only = tool == "list_routines"
        if isinstance(args, ProposeRoutineArguments):
            body: dict[str, Any] = {"name": args.name, "steps": [step.model_dump() for step in args.steps],
                                    **({"routineId": args.routineId} if args.routineId else {}),
                                    **({"sessionId": session_id, "turnId": turn_id} if session_id else {})}
            call = AppCall(operation="POST /api/routines/drafts", body=body)
        elif isinstance(args, RunRoutineArguments):
            if not session_id:
                return self._response(TraderToolResult(tool=tool, readOnly=False, warning="没有对话可运行流程"), False), None
            call = AppCall(operation="POST /api/routines/{routine_id}/run", path={"routine_id": args.routineId},
                           body={"sessionId": session_id, "quote": args.quote})
        else:
            call = AppCall(operation="GET /api/routines")
        result = await self.app_capabilities.call(call, user=user_id, session_id=session_id)
        if not result.get("ok"):
            detail = str((result.get("data") or {}).get("detail") or "流程暂不可用")
            return self._response(TraderToolResult(tool=tool, readOnly=read_only, warning=detail), False), None
        data = result["data"]
        if tool == "propose_routine":
            card = data.get("card") or {}
            from .routines import MANNER_WORDS
            data = {"draft": data.get("draft"), "status": card.get("status"), "refusal": card.get("refusal"),
                    "steps": [{"kind": step.get("kind"), "text": step.get("text"),
                               "manner": MANNER_WORDS.get(str(step.get("manner")), "照做")} for step in card.get("steps") or []],
                    "shown": "流程卡已发到对话：用户点“保存”才存" if card.get("status") == "draft"
                    else "流程卡被拒绝，没有保存按钮；把 refusal 原话转告用户"}
        elif tool == "run_routine":
            data = {**data, "note": "已排在本轮之后运行；本轮简短告诉用户马上开始，不要自己做这些步骤"}
        return self._response(TraderToolResult(tool=tool, readOnly=read_only, executionEnabled=False, data=data), True), None

    # The card button's refusals, said plainly (proposal_error codes of POST /api/proposals/{id}/reject).
    REJECT_REFUSALS = {
        "invalid_state_transition": "这张卡已经结束（已确认、已作废或已超时），没有可作废的卡。",
        "stale_proposal_version": "这张卡已经更新过，版本对不上，没有作废；请按最新的卡再说一次。",
        "proposal_not_found": "没有找到这张卡，没有作废任何卡。",
    }

    async def _reject_proposal_tool(self, arguments: Any, *, user_id: str, session_id: str | None):
        """作废待确认卡 (2026-10-12, #756): the same POST /api/proposals/{id}/reject the card's 取消 button
        and the spoken "算了" use, through the catalog bridge — one Owner (ProposalService.reject: owner,
        status and version checks, audit, approvalUsedAt, the proposal.result event). Only rejecting:
        there is no confirm tool; a card is confirmed by the user's own click or spoken "确认"."""
        from uuid import uuid4

        from .agent_api import AppCall
        tool = "reject_proposal"
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            args = RejectProposalArguments.model_validate(parsed)
        except (ValidationError, ValueError, TypeError):
            return self._unavailable(tool, "invalid_tool_arguments"), None
        call = AppCall(operation="POST /api/proposals/{proposal_id}/reject", path={"proposal_id": args.proposalId},
                       body={"proposalVersion": args.proposalVersion, "idempotencyKey": f"agent-reject-{uuid4().hex}"})
        result = await self.app_capabilities.call(call, user=user_id, session_id=session_id)
        data = result.get("data") or {}
        if result.get("ok"):
            return self._response(TraderToolResult(tool=tool, readOnly=False, data={
                "proposalId": args.proposalId, "status": data.get("status"), "auditId": data.get("auditId"),
                "message": "卡已作废，没有下单。"}), True), None
        detail = data.get("detail")
        code = str((detail.get("code") if isinstance(detail, dict) else detail) or "proposal_operation_failed")
        return self._unavailable(tool, code, message=self.REJECT_REFUSALS.get(code, f"卡没有作废（{code}）。")), None

    async def _price_alert_tool(self, tool: str, arguments: Any, *, user_id: str, session_id: str | None):
        """到价提醒 through the catalog routes the chart button and the CLI use: GET/POST
        /api/market/alerts and DELETE /api/market/alerts/{alert_id}. A spoken 品种名 resolves to
        the current main contract here; the tick grid, direction inference and the per-user cap
        are the route's — its Chinese refusals reach the user as-is."""
        from .agent_api import AppCall
        from .price_alerts import alert_line
        from .voice_intents import resolve_symbol
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            args = TOOL_ARGUMENT_MODELS[tool].model_validate(parsed)
        except (ValidationError, ValueError, TypeError):
            return self._unavailable(tool, "invalid_tool_arguments"), None
        read_only = tool == "list_price_alerts"
        if tool == "create_price_alert":
            assert isinstance(args, PriceAlertCreateArguments)
            symbol = args.symbol.strip()
            if self.shared_market is not None:
                try:
                    catalog = await self.shared_market.instruments()
                    resolved = resolve_symbol(symbol, catalog.get("items", []))
                    if resolved is not None:
                        symbol = str(resolved["symbol"])
                except Exception:
                    pass  # an unreadable catalog now just leaves the symbol for the route to judge
            body: dict[str, Any] = {"symbol": symbol, "price": args.price, "via": "chat"}
            if args.direction is not None:
                body["direction"] = args.direction
            if args.note:
                body["note"] = args.note
            if session_id:
                body["sessionId"] = session_id
            call = AppCall(operation=TOOL_OPERATIONS[tool], body=body)
        elif tool == "cancel_price_alert":
            assert isinstance(args, PriceAlertIdArguments)
            call = AppCall(operation=TOOL_OPERATIONS[tool], path={"alert_id": args.alertId})
        else:
            call = AppCall(operation=TOOL_OPERATIONS[tool])
        result = await self.app_capabilities.call(call, user=user_id, session_id=session_id)
        if not result.get("ok"):
            detail = str((result.get("data") or {}).get("detail") or "提醒没有设置成功")
            return self._response(TraderToolResult(tool=tool, readOnly=read_only, warning=detail), False), None
        data = result["data"]
        if tool == "create_price_alert":
            alert = dict(data.get("alert") or {})
            if alert and self.shared_market is not None:
                try:
                    quote = await self.shared_market.snapshot(str(alert.get("symbol") or ""), turn_id="pricealert-tool")
                    alert["lastPrice"] = quote.get("lastPrice", quote.get("price"))
                except Exception:
                    pass  # the receipt still stands; it just misses the live price
            data = {**data, "alert": alert, "summary": alert_line(alert, last_price=alert.get("lastPrice"))}
        elif tool == "list_price_alerts":
            data = {**data, "lines": [alert_line(alert) for alert in data.get("active", [])]}
        return self._response(TraderToolResult(tool=tool, readOnly=read_only, executionEnabled=False, data=data), True), None

    async def _chart_drawing_tool(self, tool: str, arguments: Any, *, user_id: str, session_id: str | None):
        """用户画线 through the catalog routes the chart toolbar uses: GET/POST /api/market/drawings
        and DELETE /api/market/drawings/{drawing_id}. A spoken 品种名 resolves to the current main
        contract here; the shape rules and the per-contract cap are the route's — its Chinese
        refusals reach the user as-is."""
        from .agent_api import AppCall
        from .chart_drawings import drawing_line
        from .voice_intents import resolve_symbol
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            args = TOOL_ARGUMENT_MODELS[tool].model_validate(parsed)
        except (ValidationError, ValueError, TypeError):
            return self._unavailable(tool, "invalid_tool_arguments"), None
        read_only = tool == "list_chart_drawings"

        async def contract(spoken: str) -> str:
            symbol = spoken.strip()
            if self.shared_market is not None:
                try:
                    catalog = await self.shared_market.instruments()
                    resolved = resolve_symbol(symbol, catalog.get("items", []))
                    if resolved is not None:
                        symbol = str(resolved["symbol"])
                except Exception:
                    pass  # an unreadable catalog now just leaves the symbol for the route to judge
            return symbol

        if tool == "create_chart_drawing":
            assert isinstance(args, ChartDrawingCreateArguments)
            body: dict[str, Any] = {"symbol": await contract(args.symbol), "kind": args.kind, "points": args.points}
            if args.label:
                body["label"] = args.label
            if args.style:
                body["style"] = args.style
            call = AppCall(operation=TOOL_OPERATIONS[tool], body=body)
        elif tool == "update_chart_drawing":
            assert isinstance(args, ChartDrawingUpdateArguments)
            patch = {key: value for key, value in (("points", args.points), ("label", args.label), ("style", args.style))
                     if value is not None}
            if not patch:
                return self._response(TraderToolResult(tool=tool, readOnly=False, warning="没有要改的内容：给 points、label 或 style"), False), None
            call = AppCall(operation=TOOL_OPERATIONS[tool], path={"drawing_id": args.drawingId}, body=patch)
        elif tool == "clear_chart_drawings":
            assert isinstance(args, ChartDrawingClearArguments)
            call = AppCall(operation=TOOL_OPERATIONS[tool], query={"symbol": await contract(args.symbol)})
        elif tool == "delete_chart_drawing":
            assert isinstance(args, ChartDrawingIdArguments)
            call = AppCall(operation=TOOL_OPERATIONS[tool], path={"drawing_id": args.drawingId})
        elif isinstance(args, ChartDrawingListArguments) and args.symbol:
            call = AppCall(operation=TOOL_OPERATIONS[tool], query={"symbol": args.symbol.strip()})
        else:
            call = AppCall(operation=TOOL_OPERATIONS[tool])
        result = await self.app_capabilities.call(call, user=user_id, session_id=session_id)
        if not result.get("ok"):
            detail = str((result.get("data") or {}).get("detail") or "这条线没有画上")
            return self._response(TraderToolResult(tool=tool, readOnly=read_only, warning=detail), False), None
        data = result["data"]
        if tool == "clear_chart_drawings":
            cleared = int(data.get("cleared") or 0)
            data = {**data, "summary": f"清空了这个合约的 {cleared} 条画线" if cleared else "这个合约没有画过线"}
        elif tool in {"create_chart_drawing", "update_chart_drawing", "delete_chart_drawing"}:
            drawing = dict(data.get("drawing") or {})
            if drawing and self.shared_market is not None:
                try:
                    quote = await self.shared_market.snapshot(str(drawing.get("symbol") or ""), turn_id="drawings-tool")
                    drawing["lastPrice"] = quote.get("lastPrice", quote.get("price"))
                except Exception:
                    pass  # the receipt still stands; it just misses the live price
            data = {**data, "drawing": drawing,
                    "summary": drawing_line(drawing, last_price=drawing.get("lastPrice"))}
        else:
            # The relative-position lines need one quote per symbol; a read that fails (or a
            # list spanning more symbols than the cap below) just leaves that line without 现价.
            drawings = [dict(item) for item in data.get("drawings", [])]
            quotes: dict[str, Any] = {}
            if self.shared_market is not None:
                for symbol in sorted({str(item.get("symbol")) for item in drawings})[:6]:
                    try:
                        quote = await self.shared_market.snapshot(symbol, turn_id="drawings-tool")
                        quotes[symbol] = quote.get("lastPrice", quote.get("price"))
                    except Exception:
                        pass
            data = {**data, "lines": [drawing_line(item, last_price=quotes.get(str(item.get("symbol"))))
                                      for item in drawings]}
        return self._response(TraderToolResult(tool=tool, readOnly=read_only, executionEnabled=False, data=data), True), None

    async def _strategy_backtest_tool(self, arguments: Any, *, user_id: str, session_id: str | None,
                                      turn_id: str, task_id: str | None):
        """规则回测 through the catalog routes (POST /api/strategies/{id}/backtest or /api/strategy-backtests),
        the same ones the detail page and the CLI use. The result card lands on this turn; the model gets
        the figures to compare before / after."""
        from .agent_api import AppCall
        from .strategy_backtest import card_payload
        tool = "run_strategy_backtest"
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            args = StrategyBacktestArguments.model_validate(parsed)
        except (ValidationError, ValueError, TypeError):
            return self._unavailable(tool, "invalid_tool_arguments"), None
        if (args.strategyId is None) == (args.plan is None):
            return self._response(TraderToolResult(tool=tool, warning="strategyId 与 plan 二选一"), False), None
        options = {key: value for key, value in args.model_dump(exclude={"strategyId", "plan"}).items() if value is not None}
        call = (AppCall(operation="POST /api/strategies/{strategy_id}/backtest", path={"strategy_id": args.strategyId},
                        body=options) if args.strategyId is not None
                else AppCall(operation="POST /api/strategy-backtests", body={**options, "plan": args.plan}))
        result = await self.app_capabilities.call(call, user=user_id, session_id=session_id)
        if not result.get("ok"):
            detail = str((result.get("data") or {}).get("detail") or "回测未完成")
            return self._response(TraderToolResult(tool=tool, warning=detail), False), None
        data = result["data"]
        brief = {key: data.get(key) for key in ("backtestId", "strategyName", "symbol", "timeframeSeconds", "summary",
                                                "exitReasons", "coverage", "notes", "params")}
        brief["lastTrades"] = (data.get("trades") or [])[-5:]
        if data.get("checkLine") and not data.get("headline"):
            brief["health"] = {"headline": data["checkLine"], "advice": []}   # 回测与体检：通过
        if data.get("headline"):
            # 策略体检: where the entry conditions get stuck and structural fixes, counted in entry bars.
            brief["health"] = {"headline": data["headline"], "advice": [item["text"] for item in data.get("advice") or []]}
        response = TraderToolResult(tool=tool, readOnly=True, executionEnabled=False, data=brief,
                                    warning="回测只读取历史行情计算，不下单；历史表现不代表未来。")
        return self._response(response, True), ("card.strategy_backtest",
                                                card_payload(data, turn_id=turn_id, task_id=task_id or data["backtestId"]))

    async def _strategy_template_tool(self, tool: str, arguments: Any, *, user_id: str, session_id: str | None,
                                      turn_id: str, task_id: str | None):
        """策略模板库 through the catalog routes the gallery and the CLI use: GET /api/strategy-templates,
        POST …/{id}/preview (a rule backtest; its card lands on this turn), POST …/{id}/instantiate; and 保存“我的策略”
        (create_strategy_draft) through the editor's own save route POST /api/strategy-plans (#932 F5)."""
        from .agent_api import AppCall
        from .strategy_backtest import card_payload
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            args = TOOL_ARGUMENT_MODELS[tool].model_validate(parsed)
        except (ValidationError, ValueError, TypeError):
            return self._unavailable(tool, "invalid_tool_arguments"), None
        options = args.model_dump(exclude={"templateId", "category"}, exclude_none=True)
        if tool == "create_strategy_draft":
            call = AppCall(operation=TOOL_OPERATIONS[tool], body=options)
        elif tool == "list_strategy_templates":
            call = AppCall(operation=TOOL_OPERATIONS[tool])
        else:
            template_id = str(args.templateId)  # type: ignore[attr-defined]
            call = AppCall(operation=TOOL_OPERATIONS[tool], path={"template_id": template_id}, body=options)
        result = await self.app_capabilities.call(call, user=user_id, session_id=session_id)
        writes = tool in {"create_strategy_from_template", "create_strategy_draft"}
        if not result.get("ok"):
            # A plan the schema rejects says which fields (the route's 422 list), so the next call can fix them.
            detail = str((result.get("data") or {}).get("detail") or "模板操作未完成")[:800]
            return self._response(TraderToolResult(tool=tool, readOnly=not writes, warning=detail), False), None
        data = result["data"]
        if tool == "list_strategy_templates":
            category = getattr(args, "category", None)
            items = [{key: item[key] for key in ("id", "name", "categoryLabel", "scenario", "plainSummary", "level", "holding",
                                                 "recommendedTimeframe", "source")}
                     | {"params": [{key: p[key] for key in ("key", "label", "default", "min", "max", "step", "unit", "help")}
                                   for p in item["params"]]}
                     for item in data["items"] if category is None or item["category"] == category]
            return self._response(TraderToolResult(tool=tool, data={"items": items}), True), None
        if writes:
            brief = {key: data.get(key) for key in ("strategyId", "name", "version", "status")}
            brief["templateSource"] = (data.get("definition") or {}).get("templateSource")
            brief["reminderOnly"] = bool(((data.get("definition") or {}).get("plan") or {}).get("reminderOnly"))
            return self._response(TraderToolResult(tool=tool, readOnly=False, data=brief,
                                                   warning="已存为策略草稿，未上线；上线由用户在策略面板决定。"), True), None
        backtest = data["backtest"]
        brief = {"templateId": data["templateId"], "params": data["params"], "planName": data["plan"]["name"],
                 "backtest": {key: backtest.get(key) for key in ("backtestId", "symbol", "timeframeSeconds", "summary",
                                                                 "exitReasons", "coverage", "notes", "feeEstimate")}}
        response = TraderToolResult(tool=tool, data=brief, warning="回测只读取历史行情计算，不下单；历史表现不代表未来。")
        return self._response(response, True), ("card.strategy_backtest",
                                                card_payload(backtest, turn_id=turn_id, task_id=task_id or backtest["backtestId"]))

    async def _strategy_lab_tool(self, tool: str, arguments: Any, *, user_id: str, session_id: str | None,
                                 turn_id: str, task_id: str | None):
        """策略实验室 (2026-09-30) through the lab routes the 实验室 tab and the CLI use:
        POST /api/strategy-labs, GET …/{lab_id} (polled while the caller waits), POST …/run-server
        (no window to compute in) and POST …/choose."""
        from fastapi import HTTPException

        from .agent_api import AppCall
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            args = TOOL_ARGUMENT_MODELS[tool].model_validate(parsed)
        except (ValidationError, ValueError, TypeError):
            return self._unavailable(tool, "invalid_tool_arguments"), None

        async def lab_call(operation: str, *, path: dict[str, str] | None = None, body: dict | None = None) -> dict[str, Any]:
            result = await self.app_capabilities.call(AppCall(operation=operation, path=path or {}, body=body),
                                                      user=user_id, session_id=session_id)
            if not result.get("ok"):
                raise HTTPException(int(result.get("status") or 400),
                                    str((result.get("data") or {}).get("detail") or "实验室操作未完成"))
            return result["data"]

        async def run_in_cloud(lab_id: str) -> None:
            await lab_call("POST /api/strategy-labs/{lab_id}/run-server", path={"lab_id": lab_id}, body={})

        try:
            if tool == "create_strategy_lab":
                body = {"symbol": args.symbol, "slippageTicks": args.slippageTicks,
                        "variants": [variant.model_dump(exclude_none=True) for variant in args.variants],
                        **({"tradingDays": args.tradingDays} if args.tradingDays else {})}
                lab = await lab_call("POST /api/strategy-labs", body=body)
                # 2026-10-08: the client no longer computes labs in the browser (the 实验室 tab became the
                # 结果页's 试几组参数比一比): every comparison runs in the cloud; uiWindowId is accepted and ignored.
                where = "server"
                await run_in_cloud(lab["id"])
                return self._response(TraderToolResult(
                    tool=tool, readOnly=True,
                    data={"labId": lab["id"], "tried": len(lab["variants"]), "where": where,
                          "variants": [{"id": item["id"], "label": item["label"]} for item in lab["variants"]]},
                    warning="对比只读取历史行情计算，不下单；历史表现不代表未来。"), True), None
            if tool == "get_strategy_lab":
                loop = asyncio.get_running_loop()
                deadline = loop.time() + args.waitSeconds
                while True:
                    lab = await lab_call("GET /api/strategy-labs/{lab_id}", path={"lab_id": args.labId})
                    if lab["status"] in {"done", "error"} or loop.time() >= deadline:
                        break
                    await asyncio.sleep(STRATEGY_LAB_POLL_SECONDS)
                return self._response(TraderToolResult(
                    tool=tool, readOnly=True, data=_lab_table(lab),
                    warning="对比只读取历史行情计算，不下单；历史表现不代表未来。" if lab["status"] == "done" else None), True), None
            picked = await lab_call("POST /api/strategy-labs/{lab_id}/choose", path={"lab_id": args.labId},
                                    body={"variantId": args.variantId, **({"name": args.name} if args.name else {})})
            return self._response(TraderToolResult(
                tool=tool, readOnly=False,
                data={key: picked.get(key) for key in ("strategyId", "backtest", "serverSummary")},
                warning="已存为“我的策略”，未上线；上线由用户在策略面板决定。"), True), None
        except HTTPException as error:
            return self._response(TraderToolResult(tool=tool, readOnly=tool != "choose_strategy_lab_variant",
                                                   warning=str(error.detail)), False), None

    async def _control_replay_tool(self, arguments: Any, *, user_id: str, session_id: str | None):
        """POST /api/replays/{identifier}/control over the MAIN runtime's capability bridge — the same
        route the replay window and the CLI use, no separate engine path. On a replay child the main
        bridge is reached with the child's own authenticated headers (both share one auth)."""
        from .agent_api import AppCall
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            args = TOOL_ARGUMENT_MODELS["control_replay"].model_validate(parsed)
        except (ValidationError, ValueError, TypeError):
            return self._unavailable("control_replay", "invalid_tool_arguments"), None
        if args.action == "speed" and args.speed is None:
            return self._unavailable("control_replay", "speed_required"), None
        body: dict[str, Any] = {"action": args.action}
        if args.speed is not None:
            body["speed"] = args.speed
        context = self.replay_context
        if context is not None:
            bridge = context.parent.state.app_capabilities if context.parent is not None else None
            identifier = context.provider.identifier
        else:
            app = self.app_capabilities.app
            router = getattr(app.state, "replay_router", None)
            context = next((item for item in router.contexts.values() if item.user_id == user_id), None) if router is not None else None
            bridge, identifier = (self.app_capabilities, context.provider.identifier) if context is not None else (None, None)
        if bridge is None or identifier is None:
            return self._unavailable("control_replay", "replay_not_found"), None
        operation = AppCall(operation="POST /api/replays/{identifier}/control", path={"identifier": identifier}, body=body)
        if bridge is self.app_capabilities:
            result = await bridge.call(operation, user=user_id, session_id=session_id)
        else:
            identity = self.app_capabilities.capture_identity(user_id, session_id=session_id)
            result = await bridge.call(operation, user=user_id, headers=identity.headers)
        if result.get("ok"):
            return self._response(TraderToolResult(tool="control_replay", readOnly=False, executionEnabled=False,
                                                   data=result["data"]), True), None
        detail = str((result.get("data") or {}).get("detail") or "回放控制失败")
        return self._response(TraderToolResult(tool="control_replay", warning=detail), False), None

    # -- AI annotation layer -------------------------------------------------

    async def _ui_window(self, window_id: str | None, *, user_id: str, session_id: str | None, task_id: str | None, headers=None) -> dict[str, Any]:
        """The window a UI tool acts on: the one named, else the conversation's own (#193: never a guess)."""
        from .agent_api import AppCall
        if not window_id and task_id and self.proposals is not None:
            from .message_context import decode_instruction
            run = self.proposals.store.get_run(task_id, user_id=user_id)
            window_id = decode_instruction(run["instruction"]).ui_window_id if run else None
        from .ui_control import not_enqueued
        # No command is sent before the window is read: each refusal here provably ran nothing.
        if not window_id:
            raise not_enqueued(409, "no_window: 本次对话没有绑定软件窗口，请传 uiWindowId（GET /api/app/ui/windows）")
        result = await self.app_capabilities.call(AppCall(operation="GET /api/app/ui/windows/{window_id}", path={"window_id": window_id}),
                                                  user=user_id, session_id=session_id, **({"headers": headers} if headers is not None else {}))
        if not result.get("ok"):
            raise not_enqueued(int(result.get("status") or 409), str((result.get("data") or {}).get("detail") or "窗口不可用"))
        window = result["data"]
        if not window.get("online"):
            raise not_enqueued(409, "目标窗口离线")
        return window

    async def _ui_revision_retry(self, request: Any, result: dict[str, Any], *, user_id: str,
                                 session_id: str | None) -> dict[str, Any]:
        """A model's UI command refused only because the window's revision moved on (the user's own
        message bumps it while the model thinks — 46% of first UI commands in the 2026-10-13 audit):
        nothing was queued, and every UiChanges value is absolute, so it is sent once more against the
        window's latest revision under a new commandId. Any other outcome is returned as it was."""
        from uuid import uuid4

        from .agent_api import AppCall
        from .turn_router import ui_command_moved_on
        body = request.body if isinstance(request.body, dict) else {}
        detail = str((result.get("data") or {}).get("detail") or "") if isinstance(result.get("data"), dict) else ""
        if not ui_command_moved_on(result) or not body.get("windowId"):
            return result
        try:
            window = await self._ui_window(str(body["windowId"]), user_id=user_id, session_id=session_id, task_id=None)
        except Exception:
            return result
        retry = {**body, "commandId": str(uuid4()), "expectedRevision": window["revision"]}
        retried = await self.app_capabilities.call(AppCall(operation=request.operation, query=dict(request.query), body=retry),
                                                   user=user_id, session_id=session_id)
        retried["revisionRetry"] = {"from": body.get("expectedRevision"), "to": window["revision"],
                                    "firstOutcome": detail or "rejected"}
        return retried

    async def _ui_command(self, window: dict[str, Any], changes: dict[str, Any], *, user_id: str, session_id: str | None) -> dict[str, Any]:
        """One UI command with a real receipt; a revision race re-reads the window once."""
        from uuid import uuid4

        from fastapi import HTTPException

        from .agent_api import AppCall
        for attempt in range(2):
            body = {"commandId": str(uuid4()), "windowId": window["windowId"], "expectedRevision": window["revision"], "changes": changes}
            result = await self.app_capabilities.call(AppCall(operation="POST /api/app/ui/commands", query={"waitMs": 3000}, body=body),
                                                      user=user_id, session_id=session_id)
            if result.get("ok"):
                receipt = result["data"]
                if receipt.get("status") == "rejected" and attempt == 0 and receipt.get("state") is None:
                    # Rejected before the client saw it: the window moved on.
                    window = await self._ui_window(window["windowId"], user_id=user_id, session_id=session_id, task_id=None)
                    continue
                return receipt
            detail = str((result.get("data") or {}).get("detail") or "")
            if result.get("status") == 409 and "界面已变化" in detail and attempt == 0:
                window = await self._ui_window(window["windowId"], user_id=user_id, session_id=session_id, task_id=None)
                continue
            if result.get("uiCommandAttempted") is False:
                from .ui_control import not_enqueued
                raise not_enqueued(int(result.get("status") or 409), detail or "界面命令失败")
            raise HTTPException(int(result.get("status") or 409), detail or "界面命令失败")
        raise HTTPException(409, "界面命令未能确认")

    async def _annotation_tool(self, tool: str, arguments: Any, *, user_id: str, session_id: str | None, task_id: str | None, headers=None):
        from fastapi import HTTPException
        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments)
            args = TOOL_ARGUMENT_MODELS[tool].model_validate(parsed)
            if tool == "highlight_ui":
                from .ui_control import HighlightChange
                HighlightChange.model_validate(args.model_dump(exclude={"uiWindowId"}, exclude_none=True))
        except (ValidationError, ValueError, TypeError):
            if tool == "highlight_ui":
                return self._response(TraderToolResult(tool=tool, errorCode="invalid_tool_arguments",
                    warning="界面参数无效，尚未发送命令", data={"uiCommandAttempted": False}), False), None
            return self._unavailable(tool, "invalid_tool_arguments"), None
        try:
            if tool == "get_chart_bars":
                bars_args = ChartBarsArguments.model_validate(args.model_dump())
                window: dict[str, Any] | None = None
                if bars_args.uiWindowId or not (bars_args.symbol and bars_args.durationSeconds):
                    try:
                        window = await self._ui_window(bars_args.uiWindowId, user_id=user_id, session_id=session_id, task_id=task_id, headers=headers)
                    except HTTPException:
                        if not (bars_args.symbol and bars_args.durationSeconds):
                            raise
                workbench = None
                if self.shared_market is not None:
                    state = (window or {}).get("state") or {}
                    symbol = bars_args.symbol or state.get("symbol")
                    duration = bars_args.durationSeconds or state.get("duration")
                    if not symbol or not duration:
                        raise ValueError("窗口没有合约图表，请指定 symbol 与 durationSeconds")
                    workbench = (await self.shared_market.market(str(symbol), int(duration)))["workbench"]
                data = _chart_bars(self.provider, bars_args, window, workbench=workbench)
                warning = BACKUP_NOTICE if (workbench or {}).get("dataStatus") == BACKUP_STATUS else None
                return self._response(TraderToolResult(tool=tool, readOnly=True, data=data, warning=warning), True), None
            window = await self._ui_window(getattr(args, "uiWindowId", None), user_id=user_id, session_id=session_id, task_id=task_id)
            if tool == "start_tour":
                tour_args = StartTourArguments.model_validate(args.model_dump())
                receipt = await self._ui_command(window, {"tour": {"op": "start", "steps": tour_args.steps}}, user_id=user_id, session_id=session_id)
                data = {"status": receipt.get("status"), "tour": (receipt.get("state") or {}).get("tour"), "windowId": window["windowId"]}
                if receipt.get("status") == "rejected":
                    data["hint"] = "第一步的 target 不在界面上或 focus 无法打开：先读窗口快照 context.highlightTargets"
                return self._response(TraderToolResult(tool=tool, readOnly=False, executionEnabled=False, data=data),
                                      receipt.get("status") == "applied"), None
            if tool == "highlight_ui":
                highlight = args.model_dump(exclude={"uiWindowId"}, exclude_none=True)
                receipt = await self._ui_command(window, {"highlight": highlight}, user_id=user_id, session_id=session_id)
                data = {"status": receipt.get("status"), "highlights": (receipt.get("state") or {}).get("highlights"),
                        "windowId": window["windowId"]}
                if receipt.get("status") == "rejected":
                    data["hint"] = "目标不在界面上：先读窗口快照 context.highlightTargets 选一个存在的 id"
                return self._response(TraderToolResult(tool=tool, readOnly=False, executionEnabled=False, data=data),
                                      receipt.get("status") == "applied"), None
            annotate = AnnotateChartArguments.model_validate(args.model_dump())
            changes = {"chartAnnotations": {"op": annotate.op, "items": annotate.items, **({"symbol": annotate.symbol} if annotate.symbol else {})}}
            receipt = await self._ui_command(window, changes, user_id=user_id, session_id=session_id)
            data = {"status": receipt.get("status"), "chartAnnotations": (receipt.get("state") or {}).get("chartAnnotations"),
                    "windowId": window["windowId"]}
            ok = receipt.get("status") == "applied"
            if receipt.get("status") == "rejected":
                data["say"] = "图上一屏最多 6 条标注（含已画的），请精简后重画：多空点位用 point，压力支撑各留一条，解释写进正文。"
            if ok:
                # 图表指标可配置 (2026-10-02): an item aimed at a pane the user removed is stored
                # but not drawn; the receipt says which ones so the answer can mention it.
                hidden = sorted({
                    str(item.get("pane") or "main") for item in annotate.items if isinstance(item, dict)
                } - _shown_panes(window.get("state") or {}))
                if hidden:
                    data["notDrawnPanes"] = hidden
                    data["paneNote"] = ("这些副图当前没有打开：" + "、".join(hidden)
                                        + "，对应标注不会显示；改画到已打开的副图，或先让用户打开该副图。")
            if ok and annotate.focus:
                window = {**window, "revision": receipt.get("observedRevision") or window["revision"]}
                focus_receipt = await self._ui_command(window, {"chartFocus": annotate.focus}, user_id=user_id, session_id=session_id)
                data["focus"] = {"status": focus_receipt.get("status"), "chartFocus": (focus_receipt.get("state") or {}).get("chartFocus")}
            return self._response(TraderToolResult(tool=tool, readOnly=False, executionEnabled=False, data=data), ok), None
        except HTTPException as error:
            from .ui_control import UI_COMMAND_ATTEMPTED_HEADER
            # Refused before any command was queued (#759): the same mark app_call carries.
            data = ({"uiCommandAttempted": False}
                    if (error.headers or {}).get(UI_COMMAND_ATTEMPTED_HEADER) == "false" else None)
            return self._response(TraderToolResult(tool=tool, readOnly=tool == "get_chart_bars", warning=str(error.detail),
                                                   data=data), False), None
        except (ValueError, TypeError, KeyError) as error:
            return self._response(TraderToolResult(tool=tool, readOnly=tool == "get_chart_bars", warning=f"参数或行情无效：{error}"), False), None

    async def _explain_signal_tool(
        self, arguments: Any, *, turn_id: str, mode: str,
        session_id: str | None, user_id: str | None, task_id: str | None,
    ):
        """explain_strategy_signal: evaluate the strategy on the current data (the replay clock in a
        replay), then draw the numbered annotations on the conversation's own chart window — the same
        chartAnnotations command annotate_chart uses. Read-only: no proposal, no order; a missing or
        offline window only skips the drawing, never the explanation."""
        from fastapi import HTTPException
        if self.proposals is None or user_id is None or not self._simulation_mode_allowed(mode):
            return self._unavailable("explain_strategy_signal", "strategy_service_unavailable"), None
        parsed = arguments if isinstance(arguments, dict) else {}
        try:
            args = ExplainStrategySignalArguments.model_validate(parsed)
        except (ValidationError, ValueError, TypeError):
            return self._unavailable("explain_strategy_signal", "invalid_tool_arguments"), None
        window: dict[str, Any] | None = None
        try:
            window = await self._ui_window(args.uiWindowId, user_id=user_id, session_id=session_id, task_id=task_id)
        except HTTPException:
            window = None  # no bound window (CLI) or offline: explain anyway, just without drawing
        symbol, timeframe = args.symbol, args.chartTimeframeSeconds
        if window is not None:
            state = window.get("state") or {}
            window_symbol = state.get("symbol")
            symbol = symbol or (window_symbol if str(window_symbol or "") not in {"", "*"} else None)
            duration = state.get("duration")
            timeframe = timeframe or (duration if duration in (60, 180, 300, 900, 3600, 86400) else None)
        try:
            explanation = await asyncio.to_thread(
                self.proposals.explain_strategy_signal,
                user_id=user_id, turn_id=turn_id, strategy_id=args.strategyId,
                symbol=symbol, chart_timeframe_seconds=timeframe,
            )
        except KeyError:
            return self._unavailable("explain_strategy_signal", "strategy_not_found"), None
        except ProposalConflict as error:
            return self._response(TraderToolResult(
                tool="explain_strategy_signal", readOnly=True,
                warning=str(error) or error.code,
            ), False), None
        except (RuntimeError, TypeError, ValueError) as error:
            return self._unavailable("explain_strategy_signal", str(error) or "strategy_signal_unavailable"), None
        if explanation.get("requiresSelection"):
            return self._response(TraderToolResult(
                tool="explain_strategy_signal", readOnly=True, data=explanation,
                warning="多张监控中的策略：把候选念给用户，先问用哪一张。",
            ), True), None
        annotations = list(explanation.get("annotations") or [])
        drawn = False
        if window is not None and annotations:
            try:
                receipt = await self._ui_command(
                    window,
                    {"chartAnnotations": {"op": "set", "items": annotations, "symbol": explanation["symbol"]}},
                    user_id=user_id, session_id=session_id,
                )
                drawn = receipt.get("status") == "applied"
            except HTTPException:
                drawn = False
        if drawn:
            warning = "解读只读；编号标注已画到图上，回复正文按编号讲解；没有创建候选、授权或委托。"
        elif annotations:
            warning = "解读只读；本次没有在线窗口，标注没有画上，请在回复里按编号讲清楚；没有创建候选、授权或委托。"
        else:
            warning = "解读只读；没有创建候选、授权或委托。"
        response = TraderToolResult(
            tool="explain_strategy_signal", readOnly=True,
            data={**explanation, "annotationsDrawn": drawn,
                  "windowId": (window or {}).get("windowId")},
            warning=warning,
        )
        return self._response(response, True), None

    async def call_async(
        self, *, tool: str, arguments: Any, turn_id: str, mode: str,
        session_id: str | None = None, user_id: str | None = None,
        task_id: str | None = None, host_identity=None, chart: str | None = None, said: str | None = None,
        card_reply: bool = False,
    ) -> tuple[dict[str, Any], tuple[str, dict[str, Any]] | None]:
        """`chart`: the contract the asking window shows — propose_order's intent naming no contract means it.
        `said`: the user's own words this turn — every key slot of propose_order's intent must rest on them
        (order_planner.ground); None (an external caller's catalog call) states its slots itself. `card_reply`: a
        spoken reply to the waiting card the voice front retired before passing the words on (#756)."""
        from fastapi import HTTPException
        if self.app_capabilities is None:
            return self._unavailable(tool, "app_capabilities_unavailable"), None

        async def invoke():
            return await self._call_async(tool=tool, arguments=arguments, turn_id=turn_id,
                mode=mode, session_id=session_id, user_id=user_id, task_id=task_id, chart=chart, said=said,
                card_reply=card_reply)

        try:
            identity = self.app_capabilities.capture_identity(user_id, session_id=session_id)

            async def authorize():
                if host_identity is not None:
                    await host_identity.verify()
                await identity.verify()

            # Authenticate every interactive tool in both environments. The
            # envelope delegates operation ownership/locking to its selected call.
            # control_replay must stay out of the replay child's envelope lock: the
            # control route it calls acquires that same lock on the child.
            if (tool in {"app_capabilities", "app_call", "control_replay"}
                    or tool in self.UI_TOOL_NAMES or tool in self.PREFERENCE_TOOL_NAMES
                    or tool in self.PRICE_ALERT_TOOL_NAMES
                    or tool in self.CHART_DRAWING_TOOL_NAMES
                    or tool == "reject_proposal"):
                await authorize()
                return await invoke()
            if self.replay_context is not None:
                return await self.replay_context.call_tool(user_id, invoke, authorize=authorize)
            return await owned_operation(invoke, authorize=authorize)
        except HTTPException as error:
            return self._unavailable(tool, str(error.detail)), None

    def _attributed_trades(self, user_id: str | None, trades: list[Any]) -> list[Any]:
        """成交归属 (#932)：策略、AI 自主与止损下的单，成交上带 `sourceLabel`（策略 · 名称 / AI 自主 / 止损），
        与复盘“来源”同一入口（order_sources）；用户自己的单与读不到来源的不带。"""
        if self.autonomy is None or user_id is None or not trades:
            return trades
        from .strategy_workbench import order_sources, source_label
        try:
            sources = order_sources(self.autonomy.store, user_id)
        except Exception:
            return trades
        attributed = []
        for trade in trades:
            label = source_label(sources.get(str(trade.get("clientOrderId") or ""))) if isinstance(trade, dict) else None
            attributed.append({**trade, "sourceLabel": label} if label else trade)
        return attributed

    def _protections(self, user_id: str | None) -> dict[str, Any]:
        """本账户保护单的当前状态（#794 P3）：有效的全列，已失效的只留最近几条并标明为什么不在了。"""
        if self.autonomy is None or user_id is None:
            return {}
        try:
            stops = self.autonomy.list_stops(user_id=user_id)
        except Exception:
            return {"protections": None, "protectionRule": "保护单暂时读不到，不要假定某个保护价仍然有效。"}
        keys = ("stopId", "symbol", "positionDirection", "positionBucket", "volume", "remainingVolume",
                "triggerPrice", "takeProfitPrice", "source", "strategyId", "status", "updatedAt")
        rows = [{**{key: stop.get(key) for key in keys}, **protection_state(stop)} for stop in stops]
        effective = [row for row in rows if row["effective"]]
        inactive = [row for row in rows if not row["effective"]][:INACTIVE_PROTECTION_LIMIT]
        return {"protections": effective + inactive, "protectionRule": PROTECTION_RULE}

    async def account_state(self, user_id: str | None, *, mode: str) -> dict[str, Any]:
        """整个账户的持仓、仍挂着的委托、仍生效的保护，现场重读的一份（2026-10-13 夜盘 P1）。撤单/挂撤保护的
        回报（accountAfter）与回合上下文（native_agent）共用这一入口。持仓与委托取自同一次组合读取
        （account=True，不拿轮询缓存的持仓配新的委托），读取放在线程里不卡事件循环；行情账户状态陈旧、缓存或
        读不到都按读不到处理：available=false，并告诉模型不要陈述现状。各列表完整，不截断。"""
        now = datetime.now(UTC)
        as_of = now.isoformat()
        as_of_local = now.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
        try:
            positions, orders, stops = await self._account_rows(user_id, mode=mode)
        except Exception as error:
            log.info("account_state_unavailable reason=%s", error)
            return {"asOf": as_of, "asOfLocal": as_of_local, "available": False, "rule": ACCOUNT_STATE_UNAVAILABLE_RULE}
        # 本站模拟两本账（2026-10-21）：美元子账户（外盘）的持仓、委托、保护接在后面，每行 ledger=USD——
        # 语音“平仓”、问持仓不会因为外盘仓在子账户里就说“没有持仓”。子账户读不到时如实写明。
        foreign: dict[str, Any] | None = None
        companion = await self._foreign_tools(user_id) if self._has_foreign_half() else None
        if self._has_foreign_half():
            if companion is None:
                foreign = {"status": "unavailable", "rule": FOREIGN_LEDGER_UNREADABLE_RULE}
            else:
                try:
                    usd_positions, usd_orders, usd_stops = await companion._account_rows(user_id, mode=mode)
                except Exception as error:
                    log.info("account_state_foreign_unavailable reason=%s", error)
                    foreign = {"status": "unavailable", "rule": FOREIGN_LEDGER_UNREADABLE_RULE}
                else:
                    tag = {"ledger": "USD"}
                    positions += [{**row, **tag} for row in usd_positions if isinstance(row, dict)]
                    orders += [{**row, **tag} for row in usd_orders if isinstance(row, dict)]
                    stops += [{**row, **tag} for row in usd_stops if isinstance(row, dict)]
                    foreign = {"status": "ok", "accountId": companion.account_id, "rule": FOREIGN_LEDGER_RULE}
        position_keys = ("symbol", "direction", "volume", "todayVolume", "historicalVolume", "openPrice", "ledger")
        order_keys = ("symbol", "direction", "offset", "volume", "volumeLeft", "limitPrice", "clientOrderId", "ledger")
        stop_keys = ("stopId", "symbol", "positionDirection", "positionBucket", "volume", "remainingVolume",
                     "triggerPrice", "takeProfitPrice", "source", "ledger")

        def left(row: dict[str, Any]) -> int:
            try:
                return int(row.get("volumeLeft", row.get("volume")) or 0)
            except (TypeError, ValueError):
                return 0

        return {
            "asOf": as_of, "asOfLocal": as_of_local, "available": True,
            "positions": [{key: row.get(key) for key in position_keys if row.get(key) is not None}
                          for row in positions if isinstance(row, dict)],
            "workingOrders": [{key: row.get(key) for key in order_keys if row.get(key) is not None}
                              for row in orders if isinstance(row, dict) and row.get("status") == "ALIVE" and left(row) > 0],
            "protections": [{key: stop.get(key) for key in stop_keys if stop.get(key) is not None}
                            for stop in stops if protection_state(stop)["effective"]],
            "rule": ACCOUNT_STATE_RULE,
            **({"foreignAccount": foreign} if foreign is not None else {}),
        }

    async def _ledger_autonomy(self, user_id: str, *, symbol: str | None = None, client_order_id: str | None = None,
                               stop_id: str | None = None) -> Any:
        """本站模拟两本账（2026-10-21）：一个动作落在它自己那本账的执行服务上——外盘合约、美元子账户里的委托或保护
        走子账户，其余（以及没有子账户、子账户此刻装配不上时）照旧本账户。AI 看得到外盘委托就撤得掉。"""
        if not self._has_foreign_half():
            return self.autonomy
        if symbol is not None:
            product = foreign_product_of(str(symbol)) or foreign_named(str(symbol))
            foreign = product is not None and foreign_tradable(product)
        elif client_order_id is not None:
            try:
                self.autonomy.simulation_orders.get(str(client_order_id), user_id=user_id)
                foreign = False
            except KeyError:
                foreign = True  # not this ledger's order: the USD ledger's, or unknown there too (its own refusal)
        elif stop_id is not None:
            row = self.autonomy.store.connection.execute(
                "SELECT trading_account_id FROM ai_stop_orders WHERE id = ? AND user_id = ?", (stop_id, user_id)).fetchone()
            foreign = row is not None and str(row[0]) != str(self.account_id)
        else:
            foreign = False
        if not foreign:
            return self.autonomy
        tools = await self._foreign_tools(user_id)
        return tools.autonomy if tools is not None and tools.autonomy is not None else self.autonomy

    def _has_foreign_half(self) -> bool:
        """This is a user's RMB 本站模拟 account that has a USD sub-account (外盘) behind it."""
        registry = getattr(self.proposals, "accounts", None)
        foreign_of = getattr(registry, "foreign_account_of", None)
        return (str(self.account_id or "").startswith("paper:") and foreign_of is not None
                and foreign_of(str(self.account_id)) is not None)

    async def _account_rows(self, user_id: str | None, *, mode: str) -> tuple[list[Any], list[Any], list[Any]]:
        """This account's positions, orders and protections, read now in one cut (raises when not current)."""
        status = str((self.runtime_status(mode) or {}).get("accountStatus") or "unavailable")
        if status in ACCOUNT_STATE_NOT_LIVE:
            raise RuntimeError(f"accountStatus_{status}")
        async with provider_read_snapshot(self.provider, refresh=True, account=True):
            positions = list(self.provider.positions() or [])
            activity = self.provider.account_activity()
        orders = activity.get("orders") if isinstance(activity, dict) else None
        if (not isinstance(orders, list) or activity.get("isCached")
                or str(activity.get("dataStatus") or "") in ACCOUNT_STATE_NOT_LIVE):
            raise RuntimeError("account_activity_not_current")
        stops = (await asyncio.to_thread(self.autonomy.list_stops, user_id=user_id)
                 if self.autonomy is not None and user_id else [])
        return positions, list(orders), list(stops)

    def _with_order_fee(self, market: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        """行情快照的每手手续费换成下单票同一估算入口的值（#770）：行情源的 commissionPerLot
        对按成交额收费的品种只有约 1/乘数（螺纹 0.31 对下单票 3.1），模型照它答就差 10 倍。
        contract 带 commissionSource（custom / exchange_standard / market_source）与单位；
        拿不到费率服务时保留行情源值并标 market_source。"""
        contract = dict(market.get("contract") or {})
        fee: dict[str, Any] | None = None
        if self.proposals is not None and user_id is not None:
            try:
                fee = self.proposals.contract_fee(user_id=user_id, market=market)
            except Exception:
                fee = None
        if fee is None:
            raw = contract.get("commissionPerLot", market.get("commissionPerLot"))
            fee = {"commissionPerLot": raw,
                   "commissionSource": "market_source" if raw is not None else None,
                   "commissionUnit": "元/手（开仓一手，按现价）"}
        contract.update(fee)
        per_lot = fee["commissionPerLot"]
        move = day_move_label(market)
        return {**market, "contract": contract, **({"dayMove": move} if move else {}),
                "commissionPerLot": per_lot if isinstance(per_lot, (int, float)) and per_lot > 0 else None}

    SHARED_MARKET_TOOLS = frozenset({"search_instruments", "get_market_snapshot", "get_market_analysis", "get_chart_bars", "get_market_board"})

    @property
    def shared_market(self):
        if self.replay_context is not None:
            return None
        app = getattr(self.app_capabilities, "app", None)
        return getattr(getattr(app, "state", None), "shared_market", None)

    async def shared_market_tool(self, tool, arguments, *, turn_id, user_id, session_id=None, task_id=None, headers=None):
        from fastapi import HTTPException
        try:
            if tool == "get_chart_bars":
                return await self._annotation_tool(tool, arguments, user_id=user_id, session_id=session_id, task_id=task_id, headers=headers)
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            args = TOOL_ARGUMENT_MODELS[tool].model_validate(parsed)
            if tool == "search_instruments":
                catalog = await self.shared_market.instruments()
                needle, exchange = args.search.strip().lower(), args.exchange.strip().upper()
                items = [item for item in catalog["items"]
                         if (not exchange or str(item.get("exchangeId", "")).upper() == exchange)
                         and (not args.mainOnly or item.get("isMain") is True)
                         and (not needle or needle in " ".join(str(item.get(key, "")) for key in
                             ("symbol", "instrumentId", "instrumentName", "productId")).lower())][:args.limit]
                data = {"dataStatus": catalog.get("dataStatus"), "capturedAt": catalog.get("capturedAt"), "items": items}
            elif tool in {"get_market_snapshot", "get_market_analysis"} and _foreign_month(args.symbol):
                # 外盘月份合约（CME.GCEZ26）不在快期目录里：读它自己的 MT5 行情（与美元子账户撮合同一份价格）。
                data = await self._foreign_month_market(tool, args, user_id=user_id, turn_id=turn_id)
            elif tool == "get_market_analysis":
                workbench = (await self.shared_market.market(args.symbol, args.durationSeconds))["workbench"]
                data = _market_analysis(workbench, lookback=args.lookbackBars)
            elif tool == "get_market_board":
                data = _board_payload(await self.shared_market.board(),
                                      BoardArguments.model_validate(args.model_dump()))
            else:
                data = self._with_order_fee(await self.shared_market.snapshot(args.symbol, turn_id), user_id)
            # 新浪备用: the model is told, every time, that these are watch-only backup numbers.
            warning = BACKUP_NOTICE if data.get("dataStatus") == BACKUP_STATUS else None
            return self._response(TraderToolResult(tool=tool, readOnly=True, data=data, warning=warning), True), (("card.market", data) if tool == "get_market_snapshot" else None)
        except (HTTPException, ValueError, TypeError, RuntimeError) as error:
            return self._unavailable(tool, str(error.detail) if isinstance(error, HTTPException) else "invalid_or_unavailable_provider_snapshot"), None

    async def _foreign_month_market(self, tool: str, args: Any, *, user_id: str | None, turn_id: str) -> dict[str, Any]:
        """一个外盘月份合约的行情读数（外盘代码统一，2026-10-21）：分钟到小时读 MT5 这个月份本身；日线读同品种的快期
        外盘主连 K 线。MT5 读不到时抛 HTTPException（中文原因），不拿别的源顶替。"""
        from fastapi import HTTPException

        from .data_providers import _age_label
        symbol = str(_foreign_month(args.symbol))
        product = foreign_product_of(symbol)
        app = getattr(self.app_capabilities, "app", None)
        foreign = getattr(getattr(getattr(getattr(app, "state", None), "ctx", None), "services", None), "foreign_market", None)
        if product is None or foreign is None:
            raise HTTPException(503, "外盘行情服务还没就绪，稍后再试")
        duration = getattr(args, "durationSeconds", 60) if tool == "get_market_analysis" else 60
        try:
            workbench = await asyncio.to_thread(foreign.workbench, symbol, duration)
        except Exception as error:  # noqa: BLE001 — the bridge down is said so, never another source's price
            raise HTTPException(503, f"现在读不到 {foreign_trade_name(symbol) or symbol} 的 MT5 行情（{type(error).__name__}），稍后再试") from None
        if workbench is None and tool == "get_market_analysis" and product.kqd:
            # 日线及以上：K 线用同品种的快期外盘主连（与图表、策略同一口径）。
            workbench = (await self.shared_market.market(product.kqd, duration))["workbench"]
        if workbench is None:
            raise HTTPException(503, f"{foreign_trade_name(symbol) or symbol} 此刻没有 MT5 报价（可能休市或还没成交），稍后再试")
        if tool == "get_market_analysis":
            return _market_analysis(workbench, lookback=args.lookbackBars)
        quote = dict(workbench.get("quote") or {})
        price = quote.get("lastPrice", quote.get("price"))
        age = float(workbench.get("ageSeconds") or 0.0)
        return {**quote, "turnId": turn_id, "symbol": symbol, "price": price,
                "name": foreign_trade_name(symbol) or quote.get("name") or symbol, "updatedAt": _age_label(age),
                "ageSeconds": round(age, 3), "currency": product.currency, "unit": product.unit,
                "summary": f"MT5（AMP）{symbol.partition('.')[2]} 月份合约的只读行情，单位{product.unit}，"
                           "与美元子账户撮合同一份价格。", "priceCurve": [price]}

    def _foreign_product(self, *words: Any, chart: str | None = None) -> ForeignProduct | None:
        """外盘二期：这一单说的是可下单的外盘品种（名字、别名、代码；什么合约都没说时看窗口的图表）→ 该品种；
        否则 None（照常在本账户规划）。美元子账户自己的工具也答（只用来把快期外盘主连代码换成主力月份）。"""
        registry = getattr(self.proposals, "accounts", None)
        if not is_foreign_paper(self.account_id) and getattr(registry, "foreign_account", None) is None:
            return None
        named = [str(word) for word in words if word]
        product = foreign_named(*named)
        if product is None and not any(isinstance(word, str) and word.strip() for word in words[1:]) and chart:
            product = foreign_product_of(chart)
        return product if product is not None and foreign_tradable(product) else None

    async def _foreign_tools(self, user_id: str | None) -> TraderReadOnlyTools | None:
        """The user's USD sub-account's card tools (assembled on demand), or None when it cannot be had now."""
        registry = getattr(self.proposals, "accounts", None)
        resolver = getattr(registry, "foreign_account", None)
        if registry is None or resolver is None or not user_id or self.proposals is None:
            return None
        foreign_id = await resolver(user_id)
        account = registry.get(foreign_id) if foreign_id else None
        if account is None:
            return None
        from .wiring.accounts import merge_paper_account_status
        base, store, foreign = self.runtime_status, self.proposals.store, account.account_id

        def status(mode: str) -> dict[str, Any]:
            return merge_paper_account_status(base(mode), store=store, accounts=registry, account_id=foreign)

        tools = TraderReadOnlyTools(account.provider, status, proposals=self.proposals, autonomy=account.autonomy)
        tools.app_capabilities = self.app_capabilities
        tools.replay_context = self.replay_context
        tools.account_id = foreign
        return tools

    async def _with_foreign_half(self, tool: str, response: dict[str, Any], *, arguments: Any, turn_id: str,
                                 mode: str, session_id: str | None, user_id: str | None) -> dict[str, Any]:
        """外盘仓合并显示 (2026-10-21)：人民币本站模拟账户的持仓 / 委托成交读数带上它的美元子账户那一半
        （`foreignAccount`：编号、美元、同一工具在子账户上的读数），与持仓面板同一口径——模型不会因为外盘仓在
        子账户里就说“没有持仓”。子账户此刻读不到写明 unavailable；其余账户原样返回。"""
        if not self._has_foreign_half() or not response.get("success"):
            return response
        foreign_of = self.proposals.accounts.foreign_account_of
        try:
            payload = json.loads(response["contentItems"][0]["text"])
        except (KeyError, IndexError, TypeError, ValueError):
            return response
        from .paper_accounts import PAPER_FOREIGN_DISPLAY_NAME
        half: dict[str, Any] = {"accountId": foreign_of(str(self.account_id)), "displayName": PAPER_FOREIGN_DISPLAY_NAME,
                                "currency": "USD", "rule": FOREIGN_HALF_RULE}
        tools = await self._foreign_tools(user_id)
        if tools is None:
            half["status"] = "unavailable"
        else:
            foreign_response, _ = await asyncio.to_thread(
                tools.call, tool=tool, arguments=arguments, turn_id=turn_id, mode=mode,
                session_id=session_id, user_id=user_id)
            try:
                foreign_payload = json.loads(foreign_response["contentItems"][0]["text"])
            except (KeyError, IndexError, TypeError, ValueError):
                foreign_payload = {}
            if foreign_response.get("success") and isinstance(foreign_payload.get("data"), dict):
                # The read's own fields (an account summary carries its own `status`) under the half's status.
                half.update({**foreign_payload["data"], "status": "ok"})
            else:
                half["status"] = "unavailable"
                half["errorCode"] = foreign_payload.get("errorCode")
        if isinstance(payload.get("data"), dict):
            payload["data"]["foreignAccount"] = half
        return {**response, "contentItems": [{"type": "inputText", "text": json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"))}]}

    async def _assemble_strategy_account(self, arguments: Any, user_id: str | None) -> None:
        """外盘三期：AI 启动的策略挂在美元子账户上而它闲置时被卸下了——先按需装配（与网页请求同一个
        ``foreign_account`` 入口），之后同步的启动分支才找得到它的执行器。其余情况什么都不做。"""
        registry = getattr(self.proposals, "accounts", None)
        resolver = getattr(registry, "foreign_account", None)
        strategy_id = arguments.get("strategyId") if isinstance(arguments, dict) else None
        if registry is None or resolver is None or not user_id or not strategy_id or self.proposals is None:
            return
        try:
            bound = str(self.proposals.store.get_strategy_draft(str(strategy_id), user_id=user_id)
                        .get("tradingAccountId") or "")
        except KeyError:
            return
        if is_foreign_paper(bound) and registry.get(bound) is None:
            await resolver(user_id)

    def _foreign_unavailable(self, tool: str) -> tuple[dict[str, Any], None]:
        return self._response(self._trade_refusal(tool, "foreign_account_unavailable", [FOREIGN_ACCOUNT_UNAVAILABLE]),
                              False), None

    @staticmethod
    def _foreign_main(tools: TraderReadOnlyTools, product: ForeignProduct) -> str | None:
        return foreign_main_symbol(tools.provider, product)

    async def call_order_card(
        self, *, tool: str, arguments: Any, turn_id: str, mode: str,
        session_id: str | None = None, user_id: str | None = None, task_id: str | None = None,
        chart: str | None = None, said: str | None = None, card_reply: bool = False,
    ) -> tuple[dict[str, Any], tuple[str, dict[str, Any]] | None]:
        """propose_order 的异步入口：平仓、全平、目标持仓、反手、一句多单先做一次整账户读取（持仓与委托出自同一份
        组合切面，#942），再在线程里规划出卡。快照只罩这两次读取：出卡要读的行情、K 线、合约目录走各自的正常读取
        路径。只开仓不读账户。

        外盘二期：说的是外盘品种（或什么都没说、窗口开着外盘图表）时，整单交给用户的美元子账户规划出卡——外盘
        只在那里撮合；卡上注明下到美元子账户。说法里的外盘别名换成目录里的品种名，图表换成该品种的主力月份。"""
        raw = arguments if isinstance(arguments, dict) else {}
        product = self._foreign_product(said, raw.get("symbol"), *(raw.get("symbols") or []), chart=chart)
        if product is not None and is_foreign_paper(self.account_id):
            # 已在美元子账户：窗口开着外盘图表（快期外盘主连代码）时，它的合约是该品种的主力月份。
            if chart and str(chart).upper().startswith("KQD."):
                chart = await asyncio.to_thread(foreign_main_symbol, self.provider, product) or chart
            if isinstance(raw.get("symbol"), str) and raw["symbol"].upper().startswith("KQD."):
                main = await asyncio.to_thread(foreign_main_symbol, self.provider, product)
                arguments = {**raw, "symbol": main or product.name}
            product = None
        if product is not None:
            tools = await self._foreign_tools(user_id)
            if tools is None:
                return self._foreign_unavailable(tool)
            main = await asyncio.to_thread(self._foreign_main, tools, product)
            moved = dict(raw)
            if isinstance(moved.get("symbol"), str):
                moved["symbol"] = (main or product.name) if foreign_product_of(moved["symbol"]) and str(
                    moved["symbol"]).upper().startswith("KQD.") else foreign_spoken(moved["symbol"], product)
            if isinstance(moved.get("symbols"), list):
                moved["symbols"] = [foreign_spoken(str(word), product) for word in moved["symbols"]]
            return await tools.call_order_card(
                tool=tool, arguments=moved, turn_id=turn_id, mode=mode, session_id=session_id, user_id=user_id,
                task_id=task_id, chart=main if chart and foreign_product_of(chart) else None,
                said=foreign_spoken(said, product) if said is not None else None, card_reply=card_reply)
        try:
            intent = proposed_intent(ProposeOrderArguments.model_validate(arguments if isinstance(arguments, dict) else {}))
        except ValidationError:
            intent = None
        if intent is not None:
            route, tools = await self._ledger_route(intent, user_id=user_id)
            if route == "usd" and tools is not None:
                return await tools.call_order_card(
                    tool=tool, arguments=arguments, turn_id=turn_id, mode=mode, session_id=session_id,
                    user_id=user_id, task_id=task_id, chart=None, said=said, card_reply=card_reply)
            if route == "both" and tools is not None and session_id is not None:
                outcome = await self._plan_both(intent, tools, mode=mode, session_id=session_id, turn_id=turn_id,
                                                user_id=user_id, origin="ai", intent_source="model")
                return self.outcome_response(tool, outcome, intent=intent)
        reads = intent is not None and (needs_account(intent) or said is not None)  # grounding reads the positions too
        snapshot = await read_account_snapshot(self.provider) if reads else None
        return await asyncio.to_thread(
            self.call,
            tool=tool, arguments=arguments, turn_id=turn_id, mode=mode,
            session_id=session_id, user_id=user_id, task_id=task_id, account_cut=snapshot, chart=chart, said=said,
            card_reply=card_reply,
        )

    async def plan_order(
        self, intent: OrderIntent, *, mode: str, session_id: str, turn_id: str, user_id: str | None,
        origin: str | None = None, target_position: dict[str, Any] | None = None,
        gate: Literal["decision", "order_safety"] = "decision", account_id: str | None = None,
        intent_source: Literal["parser", "model"] | None = None, ledger_route: bool = True,
    ) -> Outcome:
        """下单意图 → 规划器的异步入口（HTTP 出卡路由、目标持仓接力、语音前台）：需要账户的意图先读一次整账户
        切面，规划与写卡在线程里做，不卡事件循环。`gate` 见 PlanContext；`account_id` 是调用方显式指定、已核过
        授权且可写的账户（这套工具也须是那个账户的）。"""
        product = None if intent.legs else self._foreign_product(intent.product, intent.symbol)
        # 一句多单全是外盘（“买一手COMEX黄金，再买一手COMEX白银”）：整句交给美元子账户，每单各换成它的月份合约；
        # 中外混在一句的照旧在本账户规划（整句不出卡，请一单一单说）。
        foreign_legs = [self._foreign_product(leg.product, leg.symbol) for leg in intent.legs]
        if foreign_legs and all(foreign_legs) and not is_foreign_paper(self.account_id):
            tools = await self._foreign_tools(user_id)
            if tools is None:
                return Refuse("foreign_account_unavailable", (FOREIGN_ACCOUNT_UNAVAILABLE,))
            legs = [await self._foreign_month_intent(leg, tools, leg_product)
                    for leg, leg_product in zip(intent.legs, foreign_legs, strict=True)]
            if any(leg is None for leg in legs):
                return Refuse("instrument_not_found", (FOREIGN_NO_MAIN_LINE,))
            return await tools.plan_order(replace(intent, legs=tuple(legs)), mode=mode, session_id=session_id,
                                          turn_id=turn_id, user_id=user_id, origin=origin,
                                          target_position=target_position, gate=gate, intent_source=intent_source)
        if product is not None and is_foreign_paper(self.account_id):
            # 这套就是美元子账户的工具：不转账户，只把快期外盘主连代码换成主力月份。网页每个请求都带当前账户头，
            # 人民币账户的账户头不算“点名下到这里”——外盘单只在美元子账户撮合，照样转过去。
            if str(intent.symbol or "").upper().startswith("KQD."):
                main = await asyncio.to_thread(foreign_main_symbol, self.provider, product)
                if main is not None:
                    intent = replace(intent, symbol=main, product=None if intent.act == "open" else product_of(main))
            product = None
        if product is not None:
            # 外盘二期：解析器路径（打字快速出卡、语音前台）同样把外盘单交给美元子账户（见 call_order_card）。
            tools = await self._foreign_tools(user_id)
            if tools is None:
                return Refuse("foreign_account_unavailable", (FOREIGN_ACCOUNT_UNAVAILABLE,))
            moved = await self._foreign_month_intent(intent, tools, product)
            if moved is None:
                return Refuse("instrument_not_found", (FOREIGN_NO_MAIN_LINE,))
            return await tools.plan_order(moved, mode=mode, session_id=session_id, turn_id=turn_id, user_id=user_id,
                                          origin=origin, target_position=target_position, gate=gate,
                                          intent_source=intent_source)
        if ledger_route:
            route, tools = await self._ledger_route(intent, user_id=user_id)
            if route == "usd" and tools is not None:
                return await tools.plan_order(intent, mode=mode, session_id=session_id, turn_id=turn_id, user_id=user_id,
                                              origin=origin, target_position=target_position, gate=gate,
                                              intent_source=intent_source)
            if route == "both" and tools is not None:
                return await self._plan_both(intent, tools, mode=mode, session_id=session_id, turn_id=turn_id,
                                             user_id=user_id, origin=origin, intent_source=intent_source)
        snapshot = await read_account_snapshot(self.provider) if needs_account(intent) else None
        context = replace(self.plan_context(mode=mode, session_id=session_id, turn_id=turn_id, user_id=user_id,
                                            origin=origin, target_position=target_position),
                          gate=gate, account_id=account_id, intent_source=intent_source)
        store = self.proposals.store if self.proposals is not None else None
        return await asyncio.to_thread(
            lambda: plan_order(intent, snapshot, prefs_for(store, user_id), context))

    async def _foreign_month_intent(self, intent: OrderIntent, tools: TraderReadOnlyTools,
                                    product: ForeignProduct) -> OrderIntent | None:
        """一单外盘意图在美元子账户里的合约：点名的月份（CME.GCEG27 / GCEG27）原样保留；快期外盘主连或只说品种才用
        主力月份。主力月份取不到时为 None。"""
        month = _foreign_month(intent.symbol)
        main = month or await asyncio.to_thread(self._foreign_main, tools, product)
        if main is None:
            return None
        return replace(intent, symbol=main, product=None if intent.act == "open" or month else product_of(main))

    async def _ledger_route(self, intent: OrderIntent, *, user_id: str | None
                            ) -> tuple[Literal["own", "usd", "both"], TraderReadOnlyTools | None]:
        """本站模拟两本账（所有者 2026-10-21）：一句什么合约都没点名的平仓落在哪本账——“全平”两本账都有仓就两本
        都平（每本一张卡，互相关联、一起确认），只有美元账有仓就去美元账；“平仓”人民币账一手都没有而美元账有，
        去美元账（看着螺纹说“平仓”，外盘的仓不再答“没有持仓”）。点名了合约或品种、外盘品种（前面已转走）、
        一句多单、没有美元子账户：都留在本账户。"""
        if (user_id is None or intent.legs or intent.act not in {"close", "close_all"} or not self._has_foreign_half()
                or intent.product or intent.symbol or intent.symbols):
            return "own", None
        tools = await self._foreign_tools(user_id)
        if tools is None:
            return "own", None
        own, usd = await read_account_snapshot(self.provider), await read_account_snapshot(tools.provider)
        own_held = own.readable and bool(own.positions)
        usd_held = usd.readable and bool(usd.positions)
        if usd_held and not own_held and own.readable:
            return "usd", tools
        if intent.act == "close_all" and own_held and usd_held:
            return "both", tools
        return "own", None

    async def _plan_both(self, intent: OrderIntent, tools: TraderReadOnlyTools, *, mode: str, session_id: str,
                         turn_id: str, user_id: str | None, origin: str | None,
                         intent_source: Literal["parser", "model"] | None) -> Outcome:
        """全平两本账：每本账照常各出一张平仓卡（各自的账户、各自的确认链），再互相关联——卡上分开列，确认或放弃
        一张时另一张跟着（routes/execution.py）。一本账出不了卡（如外盘休市）时只出另一本，并说明那本为什么没出。"""
        own = await self.plan_order(intent, mode=mode, session_id=session_id, turn_id=turn_id, user_id=user_id,
                                    origin=origin, intent_source=intent_source, ledger_route=False)
        usd = await tools.plan_order(intent, mode=mode, session_id=session_id, turn_id=turn_id, user_id=user_id,
                                     origin=origin, intent_source=intent_source)
        if not isinstance(own, Cards) or not isinstance(usd, Cards):
            kept, lost, word = (own, usd, "外盘美元账") if isinstance(own, Cards) else (usd, own, "人民币账")
            if not isinstance(kept, Cards):
                return own
            reason = "；".join(lost.issues) if isinstance(lost, Refuse) and lost.issues else "这会儿出不了卡"
            return replace(kept, notes=(*kept.notes, f"{word}的仓没出平仓卡：{reason}"))
        cards = (*own.cards, *usd.cards)
        ids = [str(card.proposal.get("proposalId")) for card in cards if card.proposal.get("proposalId")]
        if self.proposals is not None and len(ids) > 1:
            self.proposals.store.link_proposals(ids)
            cards = tuple(replace(card, proposal={**card.proposal, "linkedProposalIds": [
                other for other in ids if other != card.proposal.get("proposalId")]}) for card in cards)
        return replace(own, cards=cards, notes=(*own.notes, *usd.notes))

    async def account_snapshot(self) -> AccountSnapshot:
        """The account cut an entry resolves a sentence's scope against before planning (语音前台)."""
        return await read_account_snapshot(self.provider)

    async def _call_async(
        self, *, tool: str, arguments: Any, turn_id: str, mode: str,
        session_id: str | None = None, user_id: str | None = None,
        task_id: str | None = None, chart: str | None = None, said: str | None = None, card_reply: bool = False,
    ) -> tuple[dict[str, Any], tuple[str, dict[str, Any]] | None]:
        if tool == "start_strategy_monitoring":
            await self._assemble_strategy_account(arguments, user_id)
        if tool in self.SHARED_MARKET_TOOLS and self.shared_market is not None:
            return await self.shared_market_tool(tool, arguments, turn_id=turn_id, user_id=user_id,
                                                 session_id=session_id, task_id=task_id)
        if tool in self.UI_TOOL_NAMES:
            if self.app_capabilities is None or not user_id:
                return self._unavailable(tool, "app_capabilities_unavailable"), None
            return await self._annotation_tool(tool, arguments, user_id=user_id, session_id=session_id, task_id=task_id)
        if tool in self.PRICE_ALERT_TOOL_NAMES:
            if self.app_capabilities is None or not user_id:
                return self._unavailable(tool, "app_capabilities_unavailable"), None
            return await self._price_alert_tool(tool, arguments, user_id=user_id, session_id=session_id)
        if tool in self.CHART_DRAWING_TOOL_NAMES:
            if self.app_capabilities is None or not user_id:
                return self._unavailable(tool, "app_capabilities_unavailable"), None
            return await self._chart_drawing_tool(tool, arguments, user_id=user_id, session_id=session_id)
        if tool in self.PREFERENCE_TOOL_NAMES:
            if self.app_capabilities is None or not user_id:
                return self._unavailable(tool, "app_capabilities_unavailable"), None
            if tool == "run_strategy_backtest":
                return await self._strategy_backtest_tool(arguments, user_id=user_id, session_id=session_id,
                                                          turn_id=turn_id, task_id=task_id)
            if tool in self.TEMPLATE_TOOL_NAMES:
                return await self._strategy_template_tool(tool, arguments, user_id=user_id, session_id=session_id,
                                                          turn_id=turn_id, task_id=task_id)
            if tool in self.LAB_TOOL_NAMES:
                return await self._strategy_lab_tool(tool, arguments, user_id=user_id, session_id=session_id,
                                                     turn_id=turn_id, task_id=task_id)
            if tool in self.JOURNAL_TOOL_NAMES:
                return await self._journal_tool(tool, arguments, user_id=user_id, session_id=session_id)
            if tool in self.MEMORY_TOOL_NAMES:
                return await self._memory_tool(tool, arguments, user_id=user_id, session_id=session_id)
            if tool in self.ROUTINE_TOOL_NAMES:
                return await self._routine_tool(tool, arguments, user_id=user_id, session_id=session_id, turn_id=turn_id)
            return await self._preference_tool(tool, arguments, user_id=user_id, session_id=session_id)
        if tool == "reject_proposal":
            if self.app_capabilities is None or not user_id:
                return self._unavailable(tool, "app_capabilities_unavailable"), None
            return await self._reject_proposal_tool(arguments, user_id=user_id, session_id=session_id)
        if tool == "control_replay":
            if self.app_capabilities is None or not user_id:
                return self._unavailable(tool, "app_capabilities_unavailable"), None
            return await self._control_replay_tool(arguments, user_id=user_id, session_id=session_id)
        if tool in {"app_capabilities", "app_call"}:
            if self.app_capabilities is None or not user_id:
                return self._unavailable(tool, "app_capabilities_unavailable"), None
            from fastapi import HTTPException

            from .agent_api import AppCall, operation_is_read_only
            api_call_started = False
            try:
                parsed = arguments if isinstance(arguments, dict) else json.loads(arguments)
                parsed = TOOL_ARGUMENT_MODELS[tool].model_validate(parsed).model_dump()
                if tool == "app_capabilities":
                    # The embedded AI reads the compact directory (2026-10-14 token 瘦身); one operation's
                    # full definition is unchanged.
                    result = self.app_capabilities.manifest(parsed.get("operation"), user=user_id, compact=True,
                                                            query=parsed.get("query"))
                    read_only, ok = True, True
                else:
                    request = AppCall.model_validate_json(parsed["requestJson"])
                    if request.operation == "GET /api/app/ui/windows" and not request.query and task_id and self.proposals:
                        from .message_context import decode_instruction
                        run = self.proposals.store.get_run(task_id, user_id=user_id)
                        window = decode_instruction(run["instruction"]).ui_window_id
                        if window:
                            request.query = {"windowId": window}
                    api_call_started = True
                    result = await self.app_capabilities.call(request, user=user_id, session_id=session_id)
                    if request.operation == "POST /api/app/ui/commands":
                        result = await self._ui_revision_retry(request, result, user_id=user_id, session_id=session_id)
                    read_only = operation_is_read_only(request.operation)
                    ok = result["ok"]
                    if not ok:
                        # Why the call failed, as a code the task event and the log carry (audit 10-13).
                        detail = str((result.get("data") or {}).get("detail") or "") if isinstance(result.get("data"), dict) else ""
                        code = "ui_revision_conflict" if "界面已变化" in detail else f"app_call_http_{result.get('status')}"
                        response = self._response(TraderToolResult(tool=tool, readOnly=read_only,
                            executionEnabled=not read_only, data=result, errorCode=code), False)
                        return response, None
                if (tool == "app_call" and request.operation == "GET /api/app/help"
                        and not (request.query or {}).get("topic")):
                    # The embedded AI reads the manual's table of contents, then one topic (the whole
                    # manual is ~145k tokens and rode every later round of the turn, 2026-10-14 瘦身);
                    # query.search returns the manual's sentences around a word instead.
                    from .app_help import help_outline, help_search
                    term = str((request.query or {}).get("search") or "").strip()
                    result = {**result, "data": help_search(term) if term else help_outline()}
                from .ui_captures import separate_capture_image
                result, captured = separate_capture_image(result)
                response = self._response(TraderToolResult(tool=tool, readOnly=read_only,
                    executionEnabled=not read_only, data=result), ok)
                if captured is not None:
                    response["contentItems"].append({"type": "inputImage", "imageUrl": f"data:{captured.mimeType};base64,{captured.data}"})
                return response, None
            except (ValueError, TypeError, KeyError, HTTPException) as error:
                message = error.detail if isinstance(error, HTTPException) else "接口参数无效，请先读取能力定义"
                data = {"uiCommandAttempted": False} if tool == "app_call" and not api_call_started else {}
                return self._response(TraderToolResult(tool=tool, warning=str(message), data=data), False), None
        autonomous_tools = {
            "get_ai_execution_status", "resume_simulation_execution",
            "stop_simulation_execution", "execute_simulation_trade",
            "query_simulation_order", "cancel_simulation_order",
            "set_simulation_stop_loss", "cancel_simulation_stop_loss",
        }
        if tool == "web_search":
            parsed = arguments if isinstance(arguments, dict) else {}
            try:
                args = WebSearchArguments.model_validate(parsed)
            except ValidationError:
                return self._unavailable(tool, "invalid_tool_arguments"), None
            if self.web_search is None:
                return self._unavailable(tool, "web_search_unconfigured"), None
            if self.replay_context is not None:
                return self._unavailable(tool, "web_search_disabled_in_replay"), None
            try:
                data = await self.web_search.search(args.query, count=args.count)
            except WebSearchError as error:
                return self._unavailable(tool, str(error)), None
            return self._response(TraderToolResult(tool=tool, readOnly=True, data=data), True), None
        if tool == "get_research_brief":
            parsed = arguments if isinstance(arguments, dict) else {}
            try:
                args = ResearchBriefArguments.model_validate(parsed)
            except ValidationError:
                return self._unavailable(tool, "invalid_tool_arguments"), None
            if self.research_brief is None:
                return self._unavailable(tool, "research_brief_unavailable"), None
            if self.replay_context is not None:
                return self._unavailable(tool, "research_brief_disabled_in_replay"), None
            try:
                data = await self.research_brief.brief(args.symbol, args.topic)
            except ValueError as error:
                return self._unavailable(tool, str(error) or "research_symbol_invalid"), None
            except RuntimeError as error:
                return self._unavailable(tool, str(error) or "research_brief_failed"), None
            return self._response(TraderToolResult(tool=tool, readOnly=True, data=data), True), None
        if tool in {"get_news_flash", "get_foreign_markets", "get_economic_calendar", "get_interest_rates"}:
            parsed = arguments if isinstance(arguments, dict) else {}
            try:
                args = TOOL_ARGUMENT_MODELS[tool].model_validate(parsed)
            except ValidationError:
                return self._unavailable(tool, "invalid_tool_arguments"), None
            if self.research_live is None:
                return self._unavailable(tool, "research_live_unavailable"), None
            if self.replay_context is not None:
                return self._unavailable(tool, "research_live_disabled_in_replay"), None
            try:
                data = await asyncio.to_thread(self._research_live_call, tool, args)
            except RuntimeError as error:
                return self._unavailable(tool, str(error) or "research_live_failed"), None
            return self._response(TraderToolResult(tool=tool, readOnly=True, data=data), True), None
        if tool == "explain_strategy_signal":
            return await self._explain_signal_tool(
                arguments, turn_id=turn_id, mode=mode,
                session_id=session_id, user_id=user_id, task_id=task_id,
            )
        if tool in self.ORDER_CARD_TOOLS:
            return await self.call_order_card(
                tool=tool, arguments=arguments, turn_id=turn_id, mode=mode,
                session_id=session_id, user_id=user_id, task_id=task_id, chart=chart, said=said, card_reply=card_reply)
        if tool not in autonomous_tools:
            response, card = await asyncio.to_thread(
                self.call,
                tool=tool, arguments=arguments, turn_id=turn_id, mode=mode,
                session_id=session_id, user_id=user_id, task_id=task_id,
            )
            if tool in FOREIGN_HALF_TOOLS:
                response = await self._with_foreign_half(tool, response, arguments=arguments, turn_id=turn_id,
                                                         mode=mode, session_id=session_id, user_id=user_id)
            return response, card
        if self.autonomy is None or session_id is None or user_id is None or not self._simulation_mode_allowed(mode):
            return self._unavailable(tool, "ai_execution_unavailable"), None
        parsed = arguments if isinstance(arguments, dict) else {}
        try:
            if tool == "get_ai_execution_status":
                ToolArguments.model_validate(parsed)
                result: Any = self.autonomy.current(user_id=user_id)
            elif tool == "resume_simulation_execution":
                args = ResumeSimulationArguments.model_validate(parsed)
                result = self.autonomy.resume_runtime_access(
                    user_id=user_id, session_id=session_id, symbol=args.symbol, ai_initiated=True,
                )
            elif tool == "stop_simulation_execution":
                args = StopSimulationArguments.model_validate(parsed)
                # 同一入口（execution_stop）：人民币本站模拟账户连同它的美元子账户一并停。
                from .execution_stop import stop_execution
                result = await stop_execution(autonomy=self.autonomy, proposals=self.proposals,
                                              accounts=getattr(self.proposals, "accounts", None),
                                              user_id=user_id, emergency=args.emergency, scope=args.scope)
            elif tool == "execute_simulation_trade":
                args = AutonomousTradeArguments.model_validate(parsed)
                autonomy = await self._ledger_autonomy(user_id, symbol=args.symbol)
                result = await autonomy.execute_trade(
                    user_id=user_id, session_id=session_id,
                    symbol=args.symbol, side=args.side, volume=args.volume,
                    price_intent=args.priceIntent, position_bucket=args.positionBucket,
                    reason=args.reason, ai_initiated=True,
                )
            elif tool == "query_simulation_order":
                args = SimulationOrderArguments.model_validate(parsed)
                autonomy = await self._ledger_autonomy(user_id, client_order_id=args.clientOrderId)
                result = await autonomy.query_order(
                    user_id=user_id, client_order_id=args.clientOrderId
                )
            elif tool == "cancel_simulation_order":
                args = SimulationOrderArguments.model_validate(parsed)
                autonomy = await self._ledger_autonomy(user_id, client_order_id=args.clientOrderId)
                result = await autonomy.cancel_order(
                    user_id=user_id, client_order_id=args.clientOrderId
                )
            elif tool == "set_simulation_stop_loss":
                args = StopLossArguments.model_validate(parsed)
                autonomy = await self._ledger_autonomy(user_id, symbol=args.symbol)
                result = await autonomy.create_stop(
                    user_id=user_id, session_id=session_id, symbol=args.symbol,
                    position_direction=args.positionDirection,
                    position_bucket=args.positionBucket, volume=args.volume,
                    trigger_price=args.triggerPrice, take_profit_price=args.takeProfitPrice,
                    price_intent=args.priceIntent,
                    ai_initiated=True,
                )
            else:
                args = StopLossIdArguments.model_validate(parsed)
                autonomy = await self._ledger_autonomy(user_id, stop_id=args.stopId)
                result = autonomy.cancel_stop(
                    user_id=user_id, stop_id=args.stopId
                )
        except ValidationError:
            return self._unavailable(tool, "invalid_tool_arguments"), None
        except (AutonomyError, SimulationOrderError, KeyError, RuntimeError, ValueError) as error:
            code = str(error) if str(error) else "ai_execution_failed"
            # These fences are the account's own rules, not faults: say what remains
            # possible so the model produces a card instead of retrying the tool.
            fence_message = {
                "ai_autonomy_disabled": AI_AUTONOMY_DISABLED_MESSAGE,
                "not_execution_owner": NOT_EXECUTION_OWNER_MESSAGE,
                "account_read_only": ACCOUNT_READ_ONLY_MESSAGE,
            }.get(code)
            if fence_message is not None:
                return self._response(TraderToolResult(
                    tool=tool, readOnly=False, errorCode=code,
                    data={"userFacing": fence_message},
                    warning=fence_message,
                ), False), None
            return self._unavailable(tool, code, message=getattr(error, "message", None)), None
        after = (await self.account_state(user_id, mode=mode)
                 if tool in ACCOUNT_STATE_WRITE_TOOLS and isinstance(result, dict) else None)
        # The card (and the stop tracker it registers, which assumes the card is already out) only after
        # the awaited read above, so a stop that triggers meanwhile cannot have its terminal update land
        # before the original "已挂保护" card.
        card = self._execution_card(tool, parsed, result, turn_id=turn_id, session_id=session_id, user_id=user_id)
        if after is not None:
            # What is left after this write, read now (2026-10-13 P1): the model reports the remaining
            # position / orders / protections from this, never from what an earlier turn said.
            # The whole account, not just this contract: "撤掉这张，还剩哪些挂单" asks about every symbol.
            result = {**result, "accountAfter": after}
        response = TraderToolResult(
            tool=tool,
            readOnly=tool in {"get_ai_execution_status", "query_simulation_order"},
            executionEnabled=tool not in {"get_ai_execution_status", "query_simulation_order"},
            data=result,
            warning=(
                "仅作用于 TqKq 模拟账户；liveTrading=disabled。"
                if tool != "get_ai_execution_status" else None
            ),
        )
        return self._response(response, True), card

    def _execution_card(self, tool: str, arguments: dict[str, Any], result: Any, *, turn_id: str,
                        session_id: str, user_id: str) -> tuple[str, dict[str, Any]] | None:
        """执行回报卡 (2026-10-04): the order the AI just sent (its account read-back) or the stop it
        armed, as a card in the turn's conversation; the order watch re-reads a sent order and
        republishes the card as it fills, is cancelled or stays unknown — an armed stop's card is
        followed the same way, from the stop's own row (trigger, cancel, shrink, unknown)."""
        if not isinstance(result, dict):
            return None
        name = None
        if tool == "execute_simulation_trade" and isinstance(result.get("simulationOrder"), dict):
            order = result["simulationOrder"]
            symbol = str((order.get("request") or {}).get("symbol") or "")
            try:
                name = self.provider.market_snapshot(turn_id, symbol).get("name") if symbol else None
            except Exception:
                name = None
            payload = execution_card(turn_id=turn_id, source="ai", source_label="AI 自主", order=order,
                                     reason=str(arguments.get("reason") or ""), name=name)
            track_execution_card(getattr(self.autonomy, "broker", None), session_id, payload,
                                 simulation_orders=getattr(self.autonomy, "simulation_orders", None), user_id=user_id)
            return "card.execution", payload
        if tool == "set_simulation_stop_loss" and result.get("stopId"):
            try:
                name = self.provider.market_snapshot(turn_id, str(result.get("symbol") or "")).get("name")
            except Exception:
                name = None
            payload = stop_execution_card(turn_id=turn_id, stop=result, name=name)
            track_execution_stop(getattr(self.autonomy, "broker", None), session_id, payload,
                                 stop_id=str(result["stopId"]), user_id=user_id)
            return "card.execution", payload
        return None

    def _decision_context(
        self, context_args: MarketAnalysisArguments, *, status_payload: dict[str, Any], turn_id: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """The verified futures context (get_futures_decision_context) or an error code. Also the
        readiness gate of every opening card (order_planner.opening_gate), so an order request is one model round."""
        market_status = str(status_payload.get("marketDataStatus") or "unavailable")
        account_status = str(status_payload.get("accountStatus") or "unavailable")
        # backup = TqKq is down and only Sina's watch-only quotes exist: same refusal.
        if market_status in {"connecting", "unavailable", "backup"}:
            return None, "marketDataStatus_unavailable"
        try:
            runtime = RuntimeStatus.model_validate(status_payload).model_dump()
            market = MarketSnapshot.model_validate(
                self.provider.market_snapshot(turn_id, context_args.symbol)
            ).model_dump()
            analysis = _market_analysis(
                self.provider.market_workbench(
                    context_args.symbol, context_args.durationSeconds
                ),
                lookback=context_args.lookbackBars,
            )
            catalog = self.provider.instrument_catalog()
            if (
                not isinstance(catalog, dict)
                or not isinstance(catalog.get("items"), list)
            ):
                raise RuntimeError("invalid_decision_context")
            if account_status in {"connecting", "unavailable"}:
                account = None
                positions: dict[str, Any] = {
                    "dataStatus": account_status, "positions": [],
                }
                activity = {
                    "capturedAt": None, "ageSeconds": None,
                    "dataStatus": account_status, "ordersInitialized": False,
                    "orders": [], "trades": [], "liveTrading": "disabled",
                }
            else:
                account = AccountSummary.model_validate(
                    self.provider.account_summary(turn_id)
                ).model_dump()
                positions = PositionsResult.model_validate({
                    "dataStatus": account_status,
                    "positions": self.provider.positions(),
                }).model_dump()
                activity = self.provider.account_activity()
                if (
                    not isinstance(activity, dict)
                    or activity.get("liveTrading") != "disabled"
                    or not isinstance(activity.get("ordersInitialized"), bool)
                    or not isinstance(activity.get("orders"), list)
                    or not isinstance(activity.get("trades"), list)
                ):
                    raise RuntimeError("invalid_decision_context")
        except KeyError:
            return None, "market_symbol_not_found"
        except (RuntimeError, ValidationError, TypeError, ValueError):
            return None, "invalid_or_unavailable_decision_context"

        instrument = next((
            item for item in catalog["items"]
            if isinstance(item, dict)
            and str(item.get("symbol") or "").upper() == context_args.symbol.upper()
        ), None)
        analysis_reasons: list[str] = []
        fresh_states = {"normal", "simulated"}
        if market_status not in fresh_states or market.get("dataStatus") not in fresh_states:
            analysis_reasons.append("market_not_fresh")
        if analysis.get("dataStatus") not in fresh_states:
            analysis_reasons.append("analysis_not_fresh")
        if catalog.get("dataStatus") not in fresh_states or catalog.get("isCached") is True:
            analysis_reasons.append("instrument_catalog_not_fresh")
        if instrument is None:
            analysis_reasons.append("instrument_not_in_catalog")
        else:
            try:
                expire_days = int(instrument.get("expireRestDays"))
            except (TypeError, ValueError):
                analysis_reasons.append("instrument_expiry_unknown")
            else:
                if expire_days <= 0:
                    analysis_reasons.append("instrument_expired")
        proposal_reasons = list(analysis_reasons)
        if (
            account_status not in fresh_states
            or not isinstance(account, dict)
            or account.get("dataStatus") not in fresh_states
        ):
            proposal_reasons.append("account_not_fresh")
        if activity.get("dataStatus") not in fresh_states:
            proposal_reasons.append("activity_not_fresh")
        if activity.get("ordersInitialized") is not True:
            proposal_reasons.append("orders_not_initialized")
        if runtime.get("riskStatus") != "normal":
            proposal_reasons.append("risk_status_not_normal")
        gateway_reasons = list(proposal_reasons)
        if runtime.get("simulationTrading") is not True:
            gateway_reasons.append("simulation_gateway_disabled")
        if runtime.get("tradingGatewayStatus") == "unavailable":
            gateway_reasons.append("simulation_gateway_unavailable")
        active_orders = [
            item for item in activity["orders"]
            if isinstance(item, dict)
            and str(item.get("status") or item.get("remoteStatus") or "").upper()
            in {"ALIVE", "PENDING", "UNKNOWN"}
        ]
        # The exchange's intraday open-position cap (0/absent = unlimited) and how
        # much this account already opened today; the proposal gate enforces it.
        try:
            open_limit = int(market.get("openLimit") or 0)
        except (TypeError, ValueError):
            open_limit = 0
        opened_today = 0
        if open_limit > 0:
            trading_day = None
            if isinstance(account, dict):
                trading_day = str(account.get("tradingDay") or "") or None
            opened_today = _opened_today(activity, context_args.symbol, trading_day)
        context = {
            "schemaVersion": 1,
            "source": "runtime_verified_futures_context_v1",
            "symbol": context_args.symbol,
            "durationSeconds": context_args.durationSeconds,
            "lookbackBars": context_args.lookbackBars,
            "analysisReady": not analysis_reasons,
            "proposalReady": not proposal_reasons,
            "simulationGatewayReady": not gateway_reasons,
            "analysisReadinessReasons": analysis_reasons,
            "proposalReadinessReasons": proposal_reasons,
            "simulationGatewayReadinessReasons": gateway_reasons,
            "userFacing": _user_facing_decision_summary(
                analysis_reasons=analysis_reasons,
                proposal_reasons=proposal_reasons,
                gateway_reasons=gateway_reasons,
            ),
            "candidateOnlyDefault": True,
            "liveTrading": "disabled",
            "runtime": runtime,
            "instrument": instrument,
            "market": market,
            "openLimit": {
                "limit": open_limit,
                "openedToday": opened_today if open_limit else None,
                "remaining": max(open_limit - opened_today, 0) if open_limit else None,
            },
            "analysis": analysis,
            "account": account,
            "positions": positions,
            "activity": {
                "capturedAt": activity.get("capturedAt"),
                "ageSeconds": activity.get("ageSeconds"),
                "dataStatus": activity.get("dataStatus"),
                "ordersInitialized": activity.get("ordersInitialized") is True,
                "activeOrderCount": len(active_orders),
                "activeOrders": active_orders[:20],
                "recentTrades": [
                    item for item in activity["trades"] if isinstance(item, dict)
                ][:20],
                "liveTrading": "disabled",
            },
        }
        return context, None

    def _response(self, result: TraderToolResult, success: bool) -> dict[str, Any]:
        return {"contentItems": [{"type": "inputText", "text": json.dumps(
            result.model_dump(), ensure_ascii=False, separators=(",", ":")
        )}], "success": success}

    def _unavailable(self, tool: str, code: str, *, message: str | None = None) -> dict[str, Any]:
        # 528-F5: 带中文句子的错误（AutonomyError.message 等）把原句带给模型，不只剩代码。
        return self._response(TraderToolResult(
            tool=tool, errorCode=code,
            warning=message or "工具调用失败；按 errorCode 说明原因，不要一律说成数据不可用；不得把占位值解释为账户或行情事实。",
        ), False)

    def _strategy_monitor_for(self, strategy: dict[str, Any]) -> Any | None:
        """The strategy's own account's monitor, reached through this session's autonomy's
        back-reference (2026-10-10 #692). A strategy bound to another account — or an account
        this boot no longer assembles — gets None: this session's tools must not read or
        start another account's book."""
        if self.autonomy is None:
            return None
        if str(strategy.get("tradingAccountId") or "") != self.autonomy.account_id:
            return None
        return self.autonomy.strategy_monitor

    def _trade_refusal(self, tool: str, code: str, issues: list[str]) -> TraderToolResult:
        """A card-level refusal (no card): the errorCode plus the user's-words line. A one_round
        route publishes this line as the turn's answer instead of asking the model again."""
        return TraderToolResult(
            tool=tool, readOnly=False, errorCode=code,
            data={"proposalReady": False, "proposalReadinessReasons": [code],
                  "userFacing": {"candidateStatus": "暂时不能生成交易方案",
                                 "candidateIssues": issues,
                                 "safetyBoundary": "实盘功能未开启"}},
            warning=CARD_REFUSAL_WARNING,
        )

    def _resolve_main_cont(self, raw: str) -> str:
        """KQ.m@SHFE.rb from any of: KQ.m@SHFE.rb / SHFE.rb / rb (the bare product
        only when the catalog shows it on exactly one exchange)."""
        value = raw.strip()
        if _MAIN_CONT_SYMBOL.fullmatch(value):
            return value
        exchange, separator, product = value.partition(".")
        if separator:
            return f"KQ.m@{exchange.upper()}.{product}"
        try:
            items = self.provider.instrument_catalog().get("items") or []
        except Exception as error:
            raise ValueError("main_contract_exchange_unknown") from error
        exchanges = {
            str(item.get("exchangeId") or "").upper()
            for item in items
            if isinstance(item, dict)
            and str(item.get("productId") or "").lower() == value.lower()
        }
        if len(exchanges) != 1:
            raise ValueError(
                "main_contract_product_ambiguous" if exchanges
                else "main_contract_product_not_found")
        return f"KQ.m@{exchanges.pop()}.{value}"

    @staticmethod
    def _close_batch_response(tool: str, proposal: dict[str, Any]) -> TraderToolResult:
        return TraderToolResult(
            tool=tool, readOnly=False,
            data={"proposalId": proposal["proposalId"], "status": proposal["status"], "totalOrders": proposal["totalOrders"],
                  "orders": [{k: o.get(k) for k in ("symbol", "name", "side", "volume", "positionBucket", "limitPrice")} for o in proposal["orders"]],
                  "skipped": proposal.get("skipped") or [],
                  **({"remainingProtection": proposal["remainingProtection"]}
                     if proposal.get("remainingProtection") else {})},
            warning="仅创建批量平仓候选；用户确认一次后 Runtime 逐张发送模拟委托并逐张回报。",
        )

    def plan_context(
        self, *, mode: str, session_id: str, turn_id: str, user_id: str | None, origin: str | None = None,
        target_position: dict[str, Any] | None = None, status_payload: dict[str, Any] | None = None,
    ) -> PlanContext:
        """This account's planning environment: its provider, the proposal service and the opening gate's
        verified context (`_decision_context`, the same reads get_futures_decision_context answers with)."""
        def decision_context(symbol: str) -> tuple[dict[str, Any] | None, str | None]:
            status = status_payload if status_payload is not None else self.runtime_status(mode)
            return self._decision_context(MarketAnalysisArguments(symbol=symbol), status_payload=status,
                                          turn_id=turn_id)
        # 冻结账户（#932 A6）：这套工具读的就是这个账户的持仓与行情，计划与卡都落在它上面——规划途中会话改绑别的账户，
        # 卡不跟着漂过去（落在原账户、卡上注明与对话账户不同）。没有多账户装配时（单账户本机）照旧跟会话。
        frozen = self.account_id if getattr(self.proposals, "accounts", None) is not None else None
        return PlanContext(provider=self.provider, proposals=self.proposals, session_id=session_id,
                           turn_id=turn_id, user_id=user_id, decision_context=decision_context,
                           origin=origin, target_position=target_position, resolved_account_id=frozen)

    def outcome_response(
        self, tool: str, outcome: Outcome, *, intent: OrderIntent | None = None,
    ) -> tuple[dict[str, Any], tuple[str, dict[str, Any]] | None]:
        """The planner's answer as the card tool's result: the card (its own numbers, so the sentence the user hears
        and the card they see agree) — several cards of one sentence each with their numbers, published one by one
        (`card_events`) — or the question to ask (with quick answers built from `intent`), or the refusal in the
        user's words, or the gate's missing facts."""
        if isinstance(outcome, Ask):
            result = self._trade_refusal(tool, f"order_{outcome.missing}_missing", [outcome.question])
            names = self._catalog_names()
            result.data = {**result.data, "ask": {
                "missing": outcome.missing, "question": outcome.question,
                "options": ask_choices(intent, outcome, lambda symbol: names.get(symbol.lower()) or symbol)
                if intent is not None else []}}
            return self._response(result, False), None
        if isinstance(outcome, Refuse):
            if outcome.form == "unavailable":
                return self._unavailable(tool, outcome.code), None
            if outcome.form == "unchanged":
                # Already at the wanted net (dual holdings count as their net): no card, said honestly.
                return self._response(TraderToolResult(
                    tool=tool, readOnly=False,
                    data={**dict(outcome.data or {}), "cardIssued": False, "userFacing": outcome.facing()},
                ), True), None
            return self._response(TraderToolResult(
                tool=tool, readOnly=False, errorCode=outcome.code,
                data={"proposalReady": False, "proposalReadinessReasons": outcome.readiness_reasons(),
                      "userFacing": outcome.facing(),
                      # 条件单：到价的可以先建价格提醒（create_price_alert），到价后再出卡。
                      **({"suggest": outcome.data["suggest"]} if (outcome.data or {}).get("suggest") else {})},
                warning=DECISION_GATE_WARNING if outcome.form == "gate" else CARD_REFUSAL_WARNING,
            ), False), None
        annotations = {**({"targetPosition": outcome.target_position} if outcome.target_position is not None else {}),
                       **dict(outcome.extra or {}),
                       **({"otherLegs": list(outcome.notes)} if outcome.notes else {})}
        if len(outcome.cards) > 1:
            # 今仓全平多个合约（几张卡）：每张卡各自确认（所有者 2026-10-16），按顺序出。
            response = TraderToolResult(
                tool=tool, readOnly=False,
                data={"totalCards": len(outcome.cards), "cards": [self._card_data(card) for card in outcome.cards],
                      **annotations},
                warning="仅创建候选；每张卡都要用户本人逐张确认后才可能发送模拟委托。")
            return self._response(response, True), (MULTI_CARD_EVENT, {
                "cards": [(card.event, card.proposal) for card in outcome.cards]})
        card = outcome.cards[0]
        if card.event == "proposal.close_batch":
            response = self._close_batch_response(tool, card.proposal)
            response.data = {**response.data, **annotations}
            return self._response(response, True), (card.event, card.proposal)
        response = TraderToolResult(
            tool=tool, readOnly=False,
            # 目标持仓 first legs repeat the annotation (第几步/共几步) so the spoken answer matches the card;
            # superseded-plan notes ride along the same way.
            data={**self._card_data(card), **annotations},
            warning="仅创建候选；必须由用户在客户端明确确认后才可能发送模拟委托。",
        )
        return self._response(response, True), (card.event, card.proposal)

    def _card_data(self, card: Card) -> dict[str, Any]:
        """One card's own numbers for the tool result (a batch card lists its orders)."""
        proposal = card.proposal
        if card.event == "proposal.close_batch":
            return {"event": card.event, **self._close_batch_response("propose_order", proposal).data}
        return {
            "proposalId": proposal["proposalId"],
            "status": proposal["status"],
            # The card's own numbers, so the sentence the user hears and the card they see carry the
            # same price, side, size and contract.
            "symbol": proposal["symbol"],
            "side": proposal["side"],
            "volume": proposal["volume"],
            "limitPrice": proposal["limitPrice"],
            "readiness": proposal["readiness"],
            "riskCheck": {"passed": proposal["riskCheck"]["passed"], "reason": proposal["riskCheck"]["reason"]},
            # The protection the card carries, when the user set one (开仓顺带保护).
            **({"protection": proposal["protection"]} if proposal.get("protection") else {}),
            # 合并卡: step ② as the card shows it (剩余仓位的止损/止盈改到哪、原来是多少).
            **({"remainingProtection": proposal["remainingProtection"]}
               if proposal.get("remainingProtection") else {}),
        }

    def _correction_card(self, session_id: str, said: str, products: tuple[str, ...], *,
                         card_reply: bool = False) -> list[dict[str, Any]]:
        """The one waiting card's orders (symbol / side / volume / price) when this sentence explicitly corrects it (the parser
        reads 改单 / 改口: “改成三手”“不对，是卖”); none otherwise — every other slot must be in the words. A spoken
        `card_reply` corrects the card the voice front just retired (#756), the session's latest."""
        from .order_intent_parse import ParseContext, Uncertain, parse
        from .proposals import awaiting_confirmation
        try:
            records = self.proposals.store.recent_proposals(session_id, limit=5) if self.proposals is not None else []
        except Exception:
            return []
        waiting = [record for record in records if awaiting_confirmation(record)]
        if not waiting and card_reply and records and records[0].get("status") == "rejected":
            waiting = records[:1]
        reading = parse(said, ParseContext(products=products, pending_card=True))
        corrects = (isinstance(reading, OrderIntent) and (reading.act == "amend" or reading.fixed)
                    or isinstance(reading, Uncertain) and reading.gap in {"amend", "correction"})
        if len(waiting) != 1 or not corrects:
            return []
        out: list[dict[str, Any]] = []
        for record in waiting:
            payload = record.get("payload") or {}
            for order in payload.get("orders") or [payload]:
                if isinstance(order, dict) and order.get("symbol"):
                    out.append({key: order.get(key) for key in ("symbol", "side", "volume", "priceIntent", "limitPrice")})
        return out

    def _catalog_names(self) -> dict[str, str]:
        """symbol (lower case) → catalog instrumentName, for lines the user reads; empty when the catalog is unreadable."""
        try:
            return catalog_names(self.provider.instrument_catalog().get("items") or [])
        except Exception:
            return {}

    def _research_live_call(self, tool: str, args: ToolArguments) -> dict[str, Any]:
        """The four live-intel reads (flash / foreign / calendar / rates), served off the shared
        caches by name so the async dispatch can offload the whole call to a worker thread."""
        live = self.research_live
        assert live is not None
        if tool == "get_news_flash" and isinstance(args, NewsFlashArguments):
            feed = live.flash_feed(args.product, "macro" if args.macroOnly else None, args.hours, args.limit,
                                   official=args.officialOnly)
            if "events" in feed:
                # The events carry the same items merged: the model reads them (major first) instead of both lists.
                rank = {"major": 0, "other": 1, "minor": 2, "unscreened": 3}
                events = sorted(feed["events"], key=lambda event: rank.get(event["tier"], 4))[:args.limit]
                feed = {key: value for key, value in feed.items() if key != "items"}
                feed["events"] = [{**event, "items": [{key: item[key] for key in ("source", "publishedAt", "link")}
                                                      for item in event["items"]]} for event in events]
            return feed
        if tool == "get_foreign_markets" and isinstance(args, ForeignMarketsArguments):
            return live.foreign_markets(args.product)
        if tool == "get_economic_calendar" and isinstance(args, EconomicCalendarArguments):
            return live.calendar_feed(args.days, args.region, args.product)
        return live.rates()

    def call(
        self, *, tool: str, arguments: Any, turn_id: str, mode: str,
        session_id: str | None = None, user_id: str | None = None,
        task_id: str | None = None,
        account_cut: AccountSnapshot | None = None, chart: str | None = None, said: str | None = None,
        card_reply: bool = False,
    ) -> tuple[dict[str, Any], tuple[str, dict[str, Any]] | None]:
        if tool not in self.NAMES:
            return self._unavailable(tool, "unknown_trader_tool"), None
        if tool == "get_research_brief":
            # Async-only (search and thread-offloaded datasets); the sync path never serves it.
            return self._unavailable(tool, "research_brief_async_required"), None
        if tool in {"get_news_flash", "get_foreign_markets", "get_economic_calendar", "get_interest_rates"}:
            # Async-only (thread-offloaded reads; a first foreign read can take seconds).
            return self._unavailable(tool, "research_live_async_required"), None
        if tool == "explain_strategy_signal":
            # Async-only (draws on the conversation window via the async UI bridge).
            return self._unavailable(tool, "explain_signal_async_required"), None
        try:
            parsed_arguments = arguments if isinstance(arguments, dict) else {}
            args = TOOL_ARGUMENT_MODELS[tool].model_validate(parsed_arguments)
        except ValidationError as error:
            # Which field is missing or wrong, in words the model can act on (#932: propose_order only said the code).
            return self._unavailable(tool, "invalid_tool_arguments", message=argument_problems(error)), None

        status_payload = self.runtime_status(mode)
        if tool in {"get_strategy_draft", "list_strategy_drafts"}:
            if self.proposals is None or user_id is None:
                return self._unavailable(tool, "strategy_service_unavailable"), None
            try:
                if tool == "get_strategy_draft":
                    strategy_id_args = StrategyIdArguments.model_validate(args.model_dump())
                    result_data: Any = self.proposals.store.get_strategy_draft(
                        strategy_id_args.strategyId, user_id=user_id
                    )
                    result_data["latestValidationReport"] = self.proposals.store.latest_strategy_validation_report(
                        strategy_id_args.strategyId, user_id=user_id
                    )
                else:
                    list_args = StrategyListArguments.model_validate(args.model_dump())
                    result_data = self.proposals.store.list_strategy_drafts(
                        user_id=user_id, limit=list_args.limit
                    )
            except KeyError:
                return self._unavailable(tool, "strategy_not_found"), None
            return self._response(TraderToolResult(tool=tool, data=result_data), True), None
        if tool == "evaluate_strategy_draft":
            if self.proposals is None or user_id is None:
                return self._unavailable(tool, "strategy_service_unavailable"), None
            strategy_id_args = StrategyIdArguments.model_validate(args.model_dump())
            try:
                evaluation = self.proposals.evaluate_strategy_draft(
                    strategy_id=strategy_id_args.strategyId,
                    user_id=user_id,
                    turn_id=turn_id,
                )
            except (KeyError, ProposalConflict, RuntimeError, TypeError, ValueError):
                return self._unavailable(tool, "strategy_evaluation_unavailable"), None
            response = TraderToolResult(
                tool=tool,
                data={
                    "strategyId": evaluation["strategyId"],
                    "signal": evaluation["signal"],
                    "evaluatedBarTime": evaluation["evaluatedBarTime"],
                    "dataStatus": evaluation["dataStatus"],
                },
                warning="信号预览只读；没有创建候选、授权或委托。",
            )
            return self._response(response, True), ("card.strategy", evaluation)
        if tool == "prepare_strategy_validation":
            if self.proposals is None or user_id is None or not self._simulation_mode_allowed(mode):
                return self._unavailable(tool, "strategy_service_unavailable"), None
            strategy_id_args = StrategyIdArguments.model_validate(args.model_dump())
            try:
                validation = self.proposals.prepare_strategy_validation(
                    strategy_id=strategy_id_args.strategyId,
                    user_id=user_id,
                    turn_id=turn_id,
                )
            except (KeyError, ProposalConflict, RuntimeError, TypeError, ValueError):
                return self._unavailable(tool, "strategy_validation_unavailable"), None
            response = TraderToolResult(
                tool=tool,
                readOnly=False,
                data={
                    "strategyId": validation["strategyId"],
                    "status": validation["status"],
                    "validationReportId": validation["validationReportId"],
                    "validationStatus": validation["validationStatus"],
                },
                warning="策略仅进入验证中；尚未通过回测，不允许启动或交易。",
            )
            return self._response(response, True), ("card.strategy", validation)
        if tool == "preview_strategy_intent":
            if self.proposals is None or user_id is None or not self._simulation_mode_allowed(mode):
                return self._unavailable(tool, "strategy_intent_unavailable"), None
            strategy_id_args = StrategyIdArguments.model_validate(args.model_dump())
            try:
                intent = self.proposals.preview_strategy_intent(
                    strategy_id=strategy_id_args.strategyId,
                    user_id=user_id,
                    turn_id=turn_id,
                )
            except (KeyError, ProposalConflict, RuntimeError, TypeError, ValueError):
                return self._unavailable(tool, "strategy_intent_unavailable"), None
            adjusted = intent["riskAdjustedIntent"]
            response = TraderToolResult(
                tool=tool,
                readOnly=False,
                data={
                    "strategyId": intent["strategyId"],
                    "intentId": intent["intentId"],
                    "signal": intent["signalSnapshot"]["signal"],
                    "action": adjusted["action"],
                    "volume": adjusted["volume"],
                    "riskStatus": adjusted["status"],
                    "executionEligible": False,
                },
                warning="只保存意图快照；未启动策略，也没有创建委托。",
            )
            return self._response(response, True), ("card.strategy", intent)
        if tool == "get_strategy_monitor_status":
            if self.proposals is None or user_id is None:
                return self._unavailable(tool, "strategy_service_unavailable"), None
            strategy_id_args = StrategyIdArguments.model_validate(args.model_dump())
            try:
                strategy = self.proposals.store.get_strategy_draft(
                    strategy_id_args.strategyId, user_id=user_id
                )
            except KeyError:
                return self._unavailable(tool, "strategy_not_found"), None
            state = self.proposals.store.strategy_monitor_state(
                strategy_id=strategy_id_args.strategyId, user_id=user_id
            )
            attempts = self.proposals.store.list_strategy_execution_attempts(
                strategy_id=strategy_id_args.strategyId, user_id=user_id, limit=20
            )
            # 预算 / 回撤读数与 HTTP 的 monitor 载荷同一份 (2026-10-10 #692)：策略挂在别的账户上
            # 或本账户监控未装配时不给读数（不能拿这个账户的行情去算另一个账户的占用）。
            funds = drawdown = None
            monitor = self._strategy_monitor_for(strategy)
            if monitor is not None:
                funds, drawdown = monitor.funds_and_drawdown(strategy)
            response = TraderToolResult(
                tool=tool,
                data={
                    "strategyId": strategy["strategyId"],
                    "status": strategy["status"],
                    "monitorState": state,
                    "recentAttempts": attempts,
                    "funds": funds,
                    "drawdown": drawdown,
                    "executionMode": "simulation_only",
                    "liveTrading": "disabled",
                },
            )
            return self._response(response, True), None
        if tool in {"start_strategy_monitoring", "pause_strategy_monitoring"}:
            if (
                self.proposals is None or self.autonomy is None
                or not self.autonomy.enabled or user_id is None or not self._simulation_mode_allowed(mode)
            ):
                return self._unavailable(tool, "strategy_monitor_unavailable"), None
            strategy_id_args = StrategyIdArguments.model_validate({"strategyId": args.model_dump()["strategyId"]})
            signal_action = args.model_dump().get("signalAction") if tool == "start_strategy_monitoring" else None
            reset_drawdown = bool(args.model_dump().get("resetDrawdown")) if tool == "start_strategy_monitoring" else False
            target = "monitoring" if tool == "start_strategy_monitoring" else "paused"
            if signal_action == "auto" and hasattr(self.autonomy, "autonomy_enabled") \
                    and not self.autonomy.autonomy_enabled(user_id):
                return self._unavailable(tool, "ai_autonomy_disabled"), None
            try:
                current = self.proposals.store.get_strategy_draft(strategy_id_args.strategyId, user_id=user_id)
                if target == "monitoring":
                    # 启动 / 显式重置回撤走与 HTTP monitor/start 同一个共用入口 (2026-10-10 #692)：
                    # 重置前提核对、排期核对、状态迁移与“成功后才删停机状态”完全一致，工具不再
                    # 复制一套迁移，普通 start 也不会把停机中的一轮假报成已恢复。
                    autonomy = self.autonomy
                    bound = str(current.get("tradingAccountId") or "")
                    registry = getattr(self.proposals, "accounts", None)
                    if bound != autonomy.account_id and is_foreign_paper(bound) and registry is not None:
                        # 外盘三期：外盘策略挂在用户的美元子账户上，人民币账户的对话也能启动它（同一个用户）。
                        foreign = registry.get(bound)
                        if foreign is None or getattr(foreign, "autonomy", None) is None:
                            return self._unavailable(tool, "foreign_account_unavailable",
                                                     message=FOREIGN_ACCOUNT_UNAVAILABLE), None
                        autonomy = foreign.autonomy
                    if bound != autonomy.account_id:
                        return self._unavailable(
                            tool, "strategy_account_not_bound",
                            message="这张策略挂在另一个交易账户上；本会话的工具只能操作本账户的策略。"), None
                    monitor = autonomy.strategy_monitor
                    if monitor is None or not monitor.enabled:
                        return self._unavailable(tool, "strategy_monitor_unavailable"), None
                    strategy = monitor.start_monitoring(
                        current, signal_action=signal_action, session_id=session_id,
                        reset_drawdown=reset_drawdown)
                else:
                    strategy = (self.proposals.transition_strategy_status(
                        strategy_id=strategy_id_args.strategyId, user_id=user_id, target="paused"))
            except KeyError:
                return self._unavailable(tool, "strategy_not_found"), None
            except ProposalConflict as error:
                # errorCode 是机器可读的码（与 HTTP 的 detail.code 同一个），中文句子进 warning。
                return self._unavailable(tool, error.code, message=str(error)), None
            drawdown_state = (self.proposals.store.strategy_drawdown_state(
                strategy_id=strategy["strategyId"], user_id=user_id)
                if target == "monitoring" else None)
            stopped_round = bool(
                drawdown_state is not None
                and str(drawdown_state["status"]) in {"stopped", "unavailable", "liquidating"})
            if target == "monitoring":
                warning = {"alert": "已开始监听已完成K线；执行方式为只提醒：信号来了只在对话里提醒，不出卡、不下单。",
                           "confirm": "已开始监听已完成K线；执行方式为出卡确认：信号来了出一张确认卡，用户确认后才下单。"}.get(
                    str(strategy.get("signalAction")),
                    "已开始监听已完成K线；执行方式为自动下单：保护器通过后直接提交模拟委托（AI 自主执行关闭时改为出卡确认）。")
                if drawdown_state is not None and str(drawdown_state["status"]) == "liquidating":
                    # 平仓中的一轮 (2026-10-10 复审)：如实说仍在平仓、新开仓锁定——不声称已恢复；
                    # 平仓与撤单全部完成后策略停在 stopped，届时才谈得上显式 resetDrawdown。
                    warning += " 注意：上一轮回撤停机仍在平仓处理中，新开仓保持锁定；平仓与撤单完成后策略会停在停止状态，届时需显式 resetDrawdown=true 才能开新一轮。"
                elif stopped_round:
                    # 普通 start 绝不重置 (2026-10-09)：停机中的一轮保持停止——不开新仓、不重新
                    # 评估；要开新一轮必须显式 resetDrawdown=true 且满足恢复前提。如实说，不假报已恢复。
                    warning += " 注意：上一轮回撤停机仍保持停止，本轮不会开新仓；要开新一轮需显式 resetDrawdown=true（需已平仓且委托与回报对齐）。"
                elif reset_drawdown:
                    warning += " 已重置回撤，开启新一轮（新基准、新高点）。"
            else:
                warning = "已停止产生新的策略意图；既有模拟委托仍按原订单身份回读。"
            response = TraderToolResult(
                tool=tool,
                readOnly=False,
                executionEnabled=target == "monitoring",
                data={
                    "strategyId": strategy["strategyId"],
                    "status": strategy["status"],
                    "signalAction": strategy.get("signalAction"),
                    "drawdownStatus": (str(drawdown_state["status"]) if drawdown_state is not None else None),
                    "executionMode": "simulation_only",
                    "liveTrading": "disabled",
                },
                warning=warning,
            )
            return self._response(response, True), None
        if tool == "run_strategy_validation":
            if (
                self.proposals is None or self.backtests is None or session_id is None
                or user_id is None or task_id is None or not self._simulation_mode_allowed(mode)
            ):
                return self._unavailable(tool, "strategy_validation_unavailable"), None
            runner = getattr(self.provider, "historical_backtest", None)
            if not callable(runner):
                return self._unavailable(tool, "tqsdk_backtest_unavailable"), None
            validation_args = StrategyValidationArguments.model_validate(args.model_dump())
            try:
                start = datetime.fromisoformat(validation_args.startDate).date()
                split = datetime.fromisoformat(validation_args.splitDate).date()
                end = datetime.fromisoformat(validation_args.endDate).date()
                if not start < split < end:
                    raise ValueError("strategy_validation_date_order_invalid")
                prepared = self.proposals.prepare_strategy_validation(
                    strategy_id=validation_args.strategyId,
                    user_id=user_id,
                    turn_id=turn_id,
                )
                strategy = self.proposals.store.get_strategy_draft(
                    validation_args.strategyId, user_id=user_id
                )
                comparison = self.backtests.run_tqsdk(
                    session_id=session_id,
                    user_id=user_id,
                    symbol=strategy["symbol"],
                    start_date=validation_args.startDate,
                    end_date=validation_args.endDate,
                    duration_seconds=int(strategy["definition"]["timeframeSeconds"]),
                    baseline_threshold="1.00",
                    candidate_threshold="1.00",
                    initial_balance=validation_args.initialBalance,
                    runner=runner,
                    request_id=task_id,
                )
                history = self.backtests.tqsdk_history_artifact(
                    backtest_id=comparison["backtestId"], user_id=user_id
                )
                split_timestamp = int(datetime.combine(
                    split, datetime.min.time(), tzinfo=timezone(timedelta(hours=8))
                ).timestamp() * 1_000_000_000)
                replay = validate_strategy_history(
                    definition=strategy["definition"],
                    raw_bars=history["bars"],
                    split_timestamp=split_timestamp,
                    price_tick=history["priceTick"],
                    contract_multiplier=history["contractMultiplier"],
                    data_source=history["dataSource"],
                    data_digest=history["dataDigest"],
                    slippage_ticks=validation_args.slippageTicks,
                    commission_ticks_per_side=validation_args.commissionTicksPerSide,
                    initial_balance=validation_args.initialBalance,
                )
                result = self.proposals.complete_strategy_validation(
                    strategy_id=validation_args.strategyId,
                    report_id=prepared["validationReportId"],
                    user_id=user_id,
                    turn_id=turn_id,
                    result=replay,
                    source_backtest_id=comparison["backtestId"],
                )
            except (KeyError, ProposalConflict, RuntimeError, TypeError, ValueError):
                return self._unavailable(tool, "strategy_validation_failed"), None
            response = TraderToolResult(
                tool=tool,
                readOnly=False,
                data={
                    "strategyId": result["strategyId"],
                    "status": result["status"],
                    "validationReportId": result["validationReportId"],
                    "validationStatus": result["validationStatus"],
                    "sourceBacktestId": comparison["backtestId"],
                    "latestValidationReport": self.proposals.store.get_strategy_validation_report(
                        result["validationReportId"], user_id=user_id
                    ),
                },
                warning="验证结果不会启动策略；没有创建委托。",
            )
            return self._response(response, True), ("card.strategy", result)
        if tool in self.ORDER_CARD_TOOLS:
            # propose_order → 下单意图 → 规划器（docs/ORDER_INTENT_V2.md 第 3 步）：合约（目录解析）、开平、今昨、价格、
            # 开仓就绪门禁、挂单检查都在 order_planner 里算一次，与打字快速路径、HTTP 路由、目标持仓接力、语音前台同一份。
            if self.proposals is None or session_id is None or not self._simulation_mode_allowed(mode):
                return self._unavailable(tool, "proposal_service_unavailable"), None
            intent = proposed_intent(ProposeOrderArguments.model_validate(args.model_dump(exclude_unset=True)))
            if intent.legs:
                # 一句多单：模型路径一律不出（与快速解析器一致，#952 Codex）——每一单的槽位绑不到它自己的分句上。
                return self.outcome_response(tool, Refuse("legs_entangled", (ONE_ORDER_AT_A_TIME_LINE,)))
            blocked = card_skill_block(self.proposals.store, user_id, intent)
            if blocked is not None:
                return self._response(self._trade_refusal(tool, "skill_unavailable", [blocked]), False), None
            try:
                items = [item for item in self.provider.instrument_catalog().get("items") or [] if isinstance(item, dict)]
            except Exception:
                # Never read (the provider keeps the last catalog through a failed read): said as it is, not read as
                # "no catalog" — the words would go on as said and come back 没找到叫“白银”的合约 (#932 F4).
                return self.outcome_response(tool, CATALOG_UNREADABLE)
            snapshot = account_cut
            if snapshot is None and (needs_account(intent) or said is not None):
                snapshot = read_account_snapshot_now(self.provider)
            if said is not None:
                # 槽位必须有出处：the model's reading keeps only what the words (or the context) vouch for.
                from .turn_router import catalog_product_names
                products = tuple(catalog_product_names(items) or ())
                grounded = ground(intent, said, products=products, chart=chart,
                                  positions=snapshot.positions if snapshot is not None and snapshot.readable else None,
                                  cards=self._correction_card(session_id, said, products, card_reply=card_reply),
                                  items=items)
                if isinstance(grounded, Ask):
                    return self.outcome_response(tool, grounded, intent=intent)
                intent = grounded
            resolved = resolve_words(intent, items, chart)
            if isinstance(resolved, Refuse):
                return self.outcome_response(tool, resolved)
            outcome = plan_order(resolved, snapshot, prefs_for(self.proposals.store, user_id), replace(self.plan_context(
                mode=mode, session_id=session_id, turn_id=turn_id, user_id=user_id, origin="ai",
                status_payload=status_payload), intent_source="model"))
            return self.outcome_response(tool, outcome, intent=resolved)
        if tool == "run_reproducible_backtest":
            if self.backtests is None or session_id is None or user_id is None or task_id is None:
                return self._unavailable(tool, "backtest_service_unavailable"), None
            backtest_args = BacktestArguments.model_validate(args.model_dump())
            try:
                comparison = self.backtests.run(
                    session_id=session_id,
                    user_id=user_id,
                    strategy_path=backtest_args.strategyPath,
                    data_path=backtest_args.dataPath,
                    baseline_threshold=backtest_args.baselineThreshold,
                    candidate_threshold=backtest_args.candidateThreshold,
                    request_id=task_id,
                )
            except (ValueError, RuntimeError):
                return self._unavailable(tool, "backtest_failed"), None
            payload = {**comparison, "turnId": turn_id, "taskId": task_id}
            response = TraderToolResult(
                tool=tool,
                readOnly=False,
                data={
                    "backtestId": comparison["backtestId"],
                    "artifactDigest": comparison["artifactDigest"],
                    "conclusion": comparison["conclusion"],
                    "reproducible": True,
                },
                warning="回测只生成研究制品，不会部署策略或提交订单。",
            )
            return self._response(response, True), ("card.backtest", payload)
        if tool == "run_tqsdk_historical_backtest":
            if self.backtests is None or session_id is None or user_id is None or task_id is None:
                return self._unavailable(tool, "backtest_service_unavailable"), None
            runner = getattr(self.provider, "historical_backtest", None)
            if not callable(runner):
                return self._unavailable(tool, "tqsdk_backtest_unavailable"), None
            historical = TqSdkBacktestArguments.model_validate(args.model_dump())
            try:
                comparison = self.backtests.run_tqsdk(
                    session_id=session_id,
                    user_id=user_id,
                    symbol=historical.symbol,
                    start_date=historical.startDate,
                    end_date=historical.endDate,
                    duration_seconds=historical.durationSeconds,
                    baseline_threshold=historical.baselineThreshold,
                    candidate_threshold=historical.candidateThreshold,
                    initial_balance=historical.initialBalance,
                    runner=runner,
                    request_id=task_id,
                )
            except (ValueError, RuntimeError):
                return self._unavailable(tool, "tqsdk_backtest_failed"), None
            payload = {**comparison, "turnId": turn_id, "taskId": task_id}
            response = TraderToolResult(
                tool=tool,
                readOnly=False,
                data={
                    "backtestId": comparison["backtestId"],
                    "artifactDigest": comparison["artifactDigest"],
                    "conclusion": comparison["conclusion"],
                    "dataPointCount": comparison["dataPointCount"],
                },
                warning="回测只使用独立 TqSim 历史账户，不会触碰已登录 TqKq 委托。",
            )
            return self._response(response, True), ("card.backtest", payload)
        if tool == "get_runtime_data_status":
            try:
                data = RuntimeStatus.model_validate(status_payload).model_dump()
            except ValidationError:
                return self._unavailable(tool, "invalid_runtime_status"), None
            return self._response(TraderToolResult(tool=tool, data=data), True), None

        if tool == "get_futures_decision_context":
            context, error = self._decision_context(
                MarketAnalysisArguments.model_validate(args.model_dump()),
                status_payload=status_payload, turn_id=turn_id,
            )
            if context is None:
                return self._unavailable(tool, str(error)), None
            warning = None if context["proposalReady"] else DECISION_GATE_WARNING
            return self._response(TraderToolResult(
                tool=tool, data=context, warning=warning
            ), True), None

        component = (
            "accountStatus"
            if tool in {"get_account_summary", "get_positions", "get_account_activity"}
            else "marketDataStatus"
        )
        data_status = str(status_payload.get(component) or "unavailable")
        if data_status in {"connecting", "unavailable", "backup"}:
            return self._unavailable(tool, f"{component}_unavailable"), None
        warning = "只读快照已陈旧，不得作为交易确认依据。" if data_status == "stale" else None
        try:
            if tool == "get_account_summary":
                account = AccountSummary.model_validate(
                    self.provider.account_summary(turn_id)
                ).model_dump()
                # 报数用的权威字段与口径一起给模型（#794），卡片仍是原摘要。
                guided = {**account,
                          "pnlFields": {key: {"value": account[key], "meaning": meaning}
                                        for key, meaning in ACCOUNT_PNL_FIELDS.items()},
                          "pnlRule": ACCOUNT_PNL_RULE}
                result = TraderToolResult(tool=tool, data=guided, warning=warning)
                return self._response(result, True), ("card.account", account)
            if tool == "estimate_order":
                if self.proposals is None or user_id is None:
                    return self._unavailable(tool, "estimate_service_unavailable"), None
                estimate_args = OrderEstimateArguments.model_validate(args.model_dump())
                try:
                    estimate = self.proposals.order_estimate(
                        user_id=user_id, symbol=estimate_args.symbol, side=estimate_args.side,
                        offset=estimate_args.offset, volume=estimate_args.volume,
                        price=estimate_args.price, provider=self.provider,
                        position_bucket=estimate_args.positionBucket,
                    )
                except (KeyError, RuntimeError, ValueError):
                    return self._unavailable(tool, "estimate_unavailable"), None
                return self._response(TraderToolResult(tool=tool, readOnly=True, data=estimate), True), None
            if tool == "get_positions":
                positions = PositionsResult.model_validate({
                    "dataStatus": data_status, "positions": self.provider.positions()
                }).model_dump()
                return self._response(TraderToolResult(
                    tool=tool, data=positions, warning=warning
                ), True), None
            if tool == "get_account_activity":
                activity = self.provider.account_activity()
                if (
                    not isinstance(activity, dict)
                    or activity.get("liveTrading") != "disabled"
                    or not isinstance(activity.get("ordersInitialized"), bool)
                    or not isinstance(activity.get("orders"), list)
                    or not isinstance(activity.get("trades"), list)
                ):
                    raise RuntimeError("invalid_account_activity")
                return self._response(TraderToolResult(
                    tool=tool, data={**activity, "trades": self._attributed_trades(user_id, activity["trades"]),
                                     **self._protections(user_id)}, warning=warning
                ), True), None
            if tool == "search_instruments":
                search_args = InstrumentSearchArguments.model_validate(args.model_dump())
                catalog = self.provider.instrument_catalog()
                items = catalog.get("items") if isinstance(catalog, dict) else None
                if not isinstance(items, list):
                    raise RuntimeError("invalid_instrument_catalog")
                needle = search_args.search.strip().lower()
                exchange = search_args.exchange.strip().upper()
                matches = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if exchange and str(item.get("exchangeId", "")).upper() != exchange:
                        continue
                    if search_args.mainOnly and item.get("isMain") is not True:
                        continue
                    searchable = " ".join(str(item.get(key, "")) for key in (
                        "symbol", "instrumentId", "instrumentName", "productId",
                    )).lower()
                    if needle and needle not in searchable:
                        continue
                    matches.append({
                        key: item.get(key) for key in (
                            "symbol", "exchangeId", "instrumentId", "instrumentName",
                            "productId", "priceTick", "volumeMultiple", "deliveryYear",
                            "deliveryMonth", "expireRestDays", "hasNight", "isMain",
                            "openLimit",
                        )
                    })
                    if len(matches) >= search_args.limit:
                        break
                return self._response(TraderToolResult(
                    tool=tool,
                    data={
                        "dataStatus": str(catalog.get("dataStatus") or data_status),
                        "capturedAt": catalog.get("capturedAt"),
                        "items": matches,
                    },
                    warning=warning,
                ), True), None
            if tool == "get_settlement_prices":
                settlement_args = SettlementPricesArguments.model_validate(args.model_dump())
                runner = getattr(self.provider, "symbol_settlements", None)
                if not callable(runner):
                    return self._unavailable(tool, "settlements_unavailable"), None
                try:
                    data = runner(settlement_args.symbols, settlement_args.days)
                except KeyError:
                    return self._unavailable(tool, "market_symbol_not_found"), None
                except RuntimeError as error:
                    return self._unavailable(tool, str(error) or "settlements_unavailable"), None
                return self._response(TraderToolResult(tool=tool, data=data), True), None
            if tool == "get_main_contract_history":
                history_args = MainContractHistoryArguments.model_validate(args.model_dump())
                runner = getattr(self.provider, "main_contract_history", None)
                if not callable(runner):
                    return self._unavailable(tool, "cont_history_unavailable"), None
                try:
                    cont = self._resolve_main_cont(history_args.symbol)
                    data = runner(cont, history_args.days)
                except KeyError:
                    return self._unavailable(tool, "market_symbol_not_found"), None
                except (RuntimeError, ValueError) as error:
                    return self._unavailable(tool, str(error) or "cont_history_unavailable"), None
                return self._response(TraderToolResult(tool=tool, data=data), True), None
            if tool == "get_symbol_ranking":
                ranking_args = SymbolRankingArguments.model_validate(args.model_dump())
                runner = getattr(self.provider, "symbol_ranking", None)
                if not callable(runner):
                    return self._unavailable(tool, "rankings_unavailable"), None
                try:
                    data = runner(ranking_args.symbol, ranking_args.rankingType, ranking_args.days)
                except KeyError:
                    return self._unavailable(tool, "market_symbol_not_found"), None
                except RuntimeError as error:
                    return self._unavailable(tool, str(error) or "rankings_unavailable"), None
                return self._response(TraderToolResult(tool=tool, data=data), True), None
            if tool == "get_market_board":
                board_args = BoardArguments.model_validate(args.model_dump())
                runner = getattr(self.provider, "market_board", None)
                if not callable(runner):
                    return self._unavailable(tool, "market_board_unavailable"), None
                try:
                    data = _board_payload(runner(), board_args)
                except RuntimeError as error:
                    return self._unavailable(tool, str(error) or "market_board_unavailable"), None
                return self._response(TraderToolResult(tool=tool, data=data), True), None
            if tool == "get_market_analysis":
                analysis_args = MarketAnalysisArguments.model_validate(args.model_dump())
                workbench = self.provider.market_workbench(
                    analysis_args.symbol, analysis_args.durationSeconds
                )
                analysis = _market_analysis(workbench, lookback=analysis_args.lookbackBars)
                return self._response(TraderToolResult(
                    tool=tool, data=analysis, warning=warning
                ), True), None
            market_args = MarketArguments.model_validate(args.model_dump())
            market = MarketSnapshot.model_validate(
                self.provider.market_snapshot(turn_id, market_args.symbol)
            ).model_dump()
            # 合约信息卡与交割提醒 (2026-10-02)：同一份补全，同步提供方（fake/回放）走目录补。
            market["contract"] = contract_payload(market)
            market = self._with_order_fee(market, user_id)
            try:
                items = self.provider.instrument_catalog().get("items") or []
            except Exception:
                items = []
            item = next((entry for entry in items
                         if str(entry.get("symbol", "")).upper() == str(market.get("symbol", "")).upper()), None)
            days = calendar_trading_days(self.provider)
            market["deliveryNotice"] = (delivery_notice(item, trading_today(trading_days=days), days)
                                        if item is not None else None)
            # The exchange's intraday open-position limit, when the contract has
            # one, together with this account's opened volume today.
            try:
                open_limit = int(market.get("openLimit") or 0)
            except (TypeError, ValueError):
                open_limit = 0
            if open_limit > 0:
                try:
                    trading_day = str(
                        (self.provider.account_summary(turn_id) or {}).get("tradingDay") or ""
                    ) or None
                except Exception:
                    trading_day = None
                try:
                    opened = _opened_today(
                        self.provider.account_activity(), market["symbol"], trading_day)
                except Exception:
                    opened = None
                market["openLimit"] = open_limit
                market["openedToday"] = opened
                market["openRemaining"] = None if opened is None else max(open_limit - opened, 0)
            result = TraderToolResult(tool=tool, data=market, warning=warning)
            return self._response(result, True), ("card.market", market)
        except KeyError:
            return self._unavailable(tool, "market_symbol_not_found"), None
        except (RuntimeError, ValidationError, TypeError, ValueError):
            return self._unavailable(tool, "invalid_or_unavailable_provider_snapshot"), None


def account_card_tools(
    app: Any, user_id: str, account: Any, status: Callable[[str], dict[str, Any]],
) -> TraderReadOnlyTools:
    """One account's card tools for an entry outside the trading AI's own turn (HTTP 出卡路由、语音前台、目标持仓
    接力): the very set the AI's turn on that account uses (the native adapter builds one per account, so the
    opening gate reads the same status), else — an adapter without them — the same construction with `status`."""
    adapter: Any = getattr(app.state, "adapter", None)
    target: Any = adapter.adapter_for_user(user_id) if hasattr(adapter, "adapter_for_user") else adapter
    if callable(getattr(target, "tools_for_account", None)):
        return target.tools_for_account(account.account_id)
    tools = TraderReadOnlyTools(account.provider, status, proposals=getattr(app.state, "proposals", None),
                                autonomy=getattr(account, "autonomy", None))
    tools.account_id = account.account_id
    return tools

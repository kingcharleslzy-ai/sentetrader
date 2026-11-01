"""Per-user preferences the Runtime persists and its consumers read by key (设置重做第 2 步).

One SQLite row per user: the full JSON document plus a version that grows on every write. `PUT
/api/preferences` is a partial update (only the keys sent change) and always returns the whole
document, so a client, the embedded agent and an external agent see the same values. Presentation
layout (`/api/app/preferences`) stays separate: it is device geometry with its own revision.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator, model_validator

from .instruments import PRODUCT_KEY_PATTERN, SYMBOL_PATTERN

PREFERENCES_VERSION = 1


class VoicePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Doubao voice id (GET /api/voice/realtime/voices); null = the Runtime default.
    voice: str | None = Field(default=None, max_length=80)
    speed: Literal["slow", "normal", "fast"] = "normal"
    # ASR endpoint watchdog — a safety net only (09-21): Doubao's own dynamic endpointing
    # (asr.extra end_smooth_window_ms) decides when a sentence ends; this forces the endpoint when the
    # transcript has stalled this long without a final (voice_realtime). It must stay above the
    # server's window or it would re-cut mid-sentence pauses.
    endpointStallSeconds: float = Field(default=3.0, ge=2.0, le=5.0)

    @field_validator("endpointStallSeconds", mode="before")
    @classmethod
    def _lift_old_stall(cls, value: Any) -> Any:
        """Accounts saved with the old 0.8–3.0 range must not 422 on read: lift anything under 2.0."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        return 2.0 if number < 2.0 else value
    # How long a read-back waits for the spoken "确认" after it finished playing.
    confirmWindowSeconds: int = Field(default=15, ge=5, le=60, strict=True)
    # Retired (#932): read so an older document still validates, decide nothing. 成交/撤单 are spoken by
    # notify.desktop.fills / cancels, AI 简讯 by notify.desktop.briefs (one switch per reminder kind).
    announceFills: bool = True
    announceBriefs: bool = True
    # The agent may draw on the chart during a call when the user asks it to explain a move.
    autoAnnotate: bool = True
    # Push-to-talk (按住说话): the transcript is sent as the user's message on release, or (false)
    # put into the composer to edit first. Read by the client only.
    pttSendOnRelease: bool = True


class KdParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    k: int = Field(default=9, ge=2, le=100, strict=True)
    d: int = Field(default=3, ge=1, le=50, strict=True)
    smooth: int = Field(default=3, ge=1, le=50, strict=True)


class MacdParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fast: int = Field(default=12, ge=2, le=100, strict=True)
    slow: int = Field(default=26, ge=3, le=200, strict=True)
    signal: int = Field(default=9, ge=1, le=100, strict=True)


class SubPanes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kd: bool = True
    macd: bool = True


# 图表指标可配置 (2026-10-02): the MA lines over the price pane, one Bollinger overlay, and the
# sub-pane list (order = display order). `subPanes` stays as the 1.6.12-era kd/macd switches: a
# document without `subPaneList` derives it from `subPanes` (kd first), and a write that carries
# `subPaneList` syncs `subPanes` back so an installed 1.6.12 desktop still shows the same switches.
# 手机看盘第二批 (2026-10-09): cci and volume (成交量 as its own pane) joined; the phone shows one of
# the chosen panes at a time and the computer all of them — one list for both.
SUBPANE_TYPES = Literal["kd", "macd", "rsi", "oi", "cci", "volume"]
SUBPANES_MAX = 3
MA_LINES_MAX = 8
DEFAULT_MA_PERIODS = (5, 10, 25, 60, 200)


def _sub_pane_name(value: Any) -> Any:
    """A pane is its name ("kd"); {"type": "kd"} (the shape an older note suggested) parses the same."""
    if isinstance(value, dict) and set(value) == {"type"}:
        return value["type"]
    return value


def _default_sub_pane_list() -> list[SUBPANE_TYPES]:
    """["kd", "macd"]: the panes the fixed-era chart always showed."""
    return ["kd", "macd"]


class MaLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period: int = Field(ge=2, le=500, strict=True)
    enabled: bool = True


class BollPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    period: int = Field(default=20, ge=5, le=100, strict=True)
    # Standard-deviation width of the bands, e.g. 2 or 2.5.
    width: float = Field(default=2.0, ge=1.0, le=4.0)


class RsiParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period: int = Field(default=14, ge=2, le=100, strict=True)


DRAWING_KIND = Literal["hline", "trendline", "zone", "ray", "extended", "hray", "vline", "channel", "polyline",
                       "rect_zone", "fib", "percent", "gann", "rect", "ellipse", "triangle", "text", "arrow",
                       "price_label", "measure"]


class DrawingStyleDefault(BaseModel):
    """画线第二版 (2026-10-09): one tool's remembered style (精确设置 → 存为这类线的默认样式). The keys
    mirror a drawing's own style (chart_drawings.parse_style); absent keys keep the tool's own look."""
    model_config = ConfigDict(extra="forbid")
    color: str | None = Field(default=None, pattern=r"^(user|amber|red|green|blue|ink|#[0-9a-fA-F]{6})$")
    width: int | None = Field(default=None, ge=1, le=4, strict=True)
    dash: Literal["solid", "dashed", "dotted"] | None = None
    extendLeft: bool | None = None
    extendRight: bool | None = None
    showPrice: bool | None = None
    allPeriods: bool | None = None


class CciParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period: int = Field(default=14, ge=2, le=100, strict=True)


class SarPreferences(BaseModel):
    """抛物线 SAR 叠加 (2026-10-09): 加速因子从 step 起、每创新高/新低加 step，封顶 max."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    step: float = Field(default=0.02, ge=0.01, le=0.1)
    max: float = Field(default=0.2, ge=0.1, le=0.5)


class ChartPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    defaultDuration: Literal[60, 180, 300, 900, 1800, 3600, 86400, 604800] = 300
    # 分时图 (2026-10-02): which chart the workbench opens on. "timeshare" also pins the
    # data period to 60 s client-side; `defaultDuration` stays what the K 线 view opens on.
    defaultView: Literal["kline", "timeshare"] = "kline"
    candleStyle: Literal["candle", "ohlc"] = "candle"
    # cn: 红涨绿跌; intl: 绿涨红跌. Chart series and market direction colors follow this together.
    upDownColors: Literal["cn", "intl"] = "cn"
    kdParams: KdParams = Field(default_factory=KdParams)
    macdParams: MacdParams = Field(default_factory=MacdParams)
    # 副图列表 (0–3 个，类型不重复): kd / macd / rsi / oi（持仓量）/ cci / volume（成交量），顺序即显示顺序. 参数仍各存各的
    # （rsiParams 等）；条目也接受 {"type": "kd"} 写法，存储与回读始终是名字字符串.
    subPaneList: list[Annotated[SUBPANE_TYPES, BeforeValidator(_sub_pane_name)]] = Field(
        default_factory=_default_sub_pane_list, max_length=SUBPANES_MAX)
    # 1.6.12 及更早的 kd/macd 开关：旧文档推导 subPaneList，新写入同步回来.
    subPanes: SubPanes = Field(default_factory=SubPanes)
    # 主图均线（1–8 条，周期不重复），默认 5/10/25/60/200 全开（与固定时代一致）.
    maLines: list[MaLine] = Field(
        default_factory=lambda: [MaLine(period=period) for period in DEFAULT_MA_PERIODS],
        min_length=1, max_length=MA_LINES_MAX)
    # 布林带叠加（中轨 = MA(period)，上下轨 = 中轨 ± width × 标准差）.
    boll: BollPreferences = Field(default_factory=BollPreferences)
    sar: SarPreferences = Field(default_factory=SarPreferences)
    # 手机副图设置的“均线”总开关 (2026-10-09): the periods that were on when it switched every line off, so
    # switching it back on restores them; emptied once restored. The computer's per-line switches ignore it.
    maMuted: list[int] = Field(default_factory=list, max_length=MA_LINES_MAX)
    rsiParams: RsiParams = Field(default_factory=RsiParams)
    cciParams: CciParams = Field(default_factory=CciParams)
    aiAnnotationsVisible: bool = True
    # Minutes after which the client clears AI drawings; 0 = keep until cleared by hand.
    aiAnnotationsAutoClearMinutes: int = Field(default=0, ge=0, le=1440, strict=True)
    # 画线第二版 (2026-10-09): each tool's default style (written by 精确设置's 存为这类线的默认样式),
    # 磁吸 (snap a click to the bar's open/high/low/close), and the toolbar's 全部锁定 / 全部隐藏 —
    # view switches over every drawing, not written into the drawings themselves.
    drawingDefaults: dict[DRAWING_KIND, DrawingStyleDefault] = Field(default_factory=dict)
    drawingMagnet: bool = False
    drawingsLocked: bool = False
    drawingsHidden: bool = False

    @model_validator(mode="after")
    def unique_and_synced(self):
        periods = [line.period for line in self.maLines]
        if len(set(periods)) != len(periods):
            raise ValueError("均线周期不能重复")
        if len(set(self.subPaneList)) != len(self.subPaneList):
            raise ValueError("副图指标不能重复")
        if "subPaneList" in self.model_fields_set:
            self.subPanes.kd = "kd" in self.subPaneList
            self.subPanes.macd = "macd" in self.subPaneList
        else:
            # An older document (or an older client's patch): the kd/macd switches decide the panes.
            self.subPaneList = [pane for pane, on in (("kd", self.subPanes.kd), ("macd", self.subPanes.macd)) if on]
        return self


class TradePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    defaultVolume: int = Field(default=1, ge=1, le=100, strict=True)
    # What the agent uses when the user names no price: aggressive = 对手价, passive = 挂单.
    defaultPriceIntent: Literal["aggressive", "passive"] = "aggressive"
    closePreference: Literal["today", "historical", "auto"] = "auto"
    # Simulation only: whether a spoken order still waits for the spoken "确认". Live trading is not
    # a Runtime capability, so this never reaches a real account.
    voiceOrderConfirm: bool = True
    # 闪电下单: a ladder click builds the candidate directly; the confirmation still applies.
    quickOrder: bool = False
    # Whether the AI may send simulation orders by itself (its own execution tool, the runtime
    # execution lease it needs, and strategy monitoring's automatic execution). Off = the AI can only
    # produce proposal cards, which the user confirms as before; enforced in the Runtime (autonomy.py),
    # not by prompt wording.
    aiAutonomy: bool = True


class ChatPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # concise is the standing reply contract (developer_instructions); detailed opens it up on request.
    replyStyle: Literal["concise", "detailed"] = "concise"
    # Read the window snapshot first when the user points at the screen ("这个", "屏幕上").
    autoReadScreen: bool = True
    # 让 AI 记住我 (记忆层 3a, 2026-09-29): off = chat and voice carry no memory block
    # and a chat "记住…" is refused (409) instead of written.
    memory: bool = True
    sendKey: Literal["enter", "ctrlEnter"] = "enter"


class DesktopNotifications(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 成交与撤单 (2026-10-06): one switch in the settings writes both; the switch shows `fills`.
    fills: bool = True
    cancels: bool = True
    # Legacy (2026-10-06): 急停 always notifies — on the desktop and on the lock screen — and the
    # switch is gone. The key stays so an older client's document still validates; nobody reads it.
    emergencyStop: bool = True
    # AI 简讯: since #932 the one switch of a new news brief's reminder (提醒中心, desktop, push, voice);
    # 盘中异动 has its own `movement` switch.
    briefs: bool = False
    # 到价提醒 (2026-10-02): since #932 the one switch of a triggered alert's reminder — the
    # conversation message, the desktop notification, the spoken line and the push.
    priceAlerts: bool = True


class BreakEvenRule(BaseModel):
    """保本提醒的阈值 (2026-10-06): 浮盈达到“成本到止损的距离”的 `multiple` 倍时问（默认 1 倍风险），
    或 mode=amount 时按该持仓浮动盈亏达到 `amount` 元。外盘美元子账户的持仓在 mode=amount 时按自己的
    美元门槛 `amountUsd`（所有者 2026-10-21）；不设（null）就按 `multiple` 判断——美元盈亏从不比人民币门槛。"""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["multiple", "amount"] = "multiple"
    multiple: float = Field(default=1.0, ge=0.5, le=5.0)
    amount: int = Field(default=1000, ge=1, le=10_000_000, strict=True)
    amountUsd: int | None = Field(default=None, ge=1, le=10_000_000, strict=True)


class NotifyPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    desktop: DesktopNotifications = Field(default_factory=DesktopNotifications)
    sound: bool = True
    # 收盘前提醒 (2026-10-03): 日盘/夜盘最后一段结束前 5 分钟还有持仓时提醒一次，只提醒。
    marketCloseReminder: bool = False
    # 风险度提醒 (2026-10-03): 账户直报的风险度到 80% 时每个交易日提醒一次，只提醒——
    # 不平仓、不拒单、不加任何限制（模拟账户无平台风险规则的所有者决定保持不变）。
    riskReminder: bool = True
    # 监控中的策略信号变化（观望 → 开多/开空/平仓）主动提醒：图上标注 + 轻提示，语音通话中
    # 播一句。关闭只停主动提醒，不影响主动问策略信号。2026-10-06 起也是“通知哪些事 · 策略信号”：
    # 上线策略出信号（只提醒的消息、确认卡、自动下的模拟单）的锁屏推送同受它管。
    strategySignalAlerts: bool = True
    # 通知哪些事 (2026-10-06) — one set of switches for the desktop notifications and the lock
    # screen: 开盘前判断 (each trading day after the 08:45 pre-generation, one push per user) and
    # 盘中异动 (held contracts and brief products moving fast on volume: a message + card, the
    # desktop notification and the push; one per product per 15 minutes). 盘中异动 is off by default since
    # 2026-10-14 (owner: too frequent); the user turns it on in 通知哪些事. Stored documents keep their own value.
    morningDesk: bool = True
    movement: bool = False
    # 收盘复盘 (2026-10-06): each trading day at 15:15 (a USD sub-account: 17:15 New York, after CME's close),
    # when the account has fills that day, the 今日复盘
    # arrives as a message + card in the latest conversation, the desktop notification and the push.
    # Off stops the delivery only; the review itself is still written for the 复盘 page.
    dailyReview: bool = True
    # 保本提醒 (2026-10-06): 挂了止损、止损还劣于成本的持仓，浮盈达到阈值（breakEvenRule，默认 1 倍
    # 风险）时每笔持仓每个交易日问一次要不要把止损移到成本——对话消息 + 提醒卡、桌面通知、通话口播、锁屏推送。
    # Off stops the question entirely; nothing is ever moved without the user's own 移到成本.
    breakEven: bool = True
    breakEvenRule: BreakEvenRule = Field(default_factory=BreakEvenRule)
    # 统一提醒 (#932, 2026-10-15): one switch per reminder kind gates every channel — the conversation,
    # the desktop notification, the spoken line and the push (reminder_kinds.KINDS). These five kinds had
    # no switch before; the other kinds keep the keys above.
    stopTriggered: bool = True        # 止损止盈触发
    protectionCancelled: bool = True  # 保护单撤销（原仓平完自动撤）
    orderUnfilled: bool = True        # 挂单未成交（15 秒、60 秒）与目标持仓下一步
    delivery: bool = True             # 交割/到期
    researchDebate: bool = True       # 多 AI 研讨完成
    # 锁屏上 · 显示金额和手数: off → every push body is a short line without figures (螺纹有新成交).
    lockScreenAmounts: bool = True


class DataPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Chat sessions idle longer than this are released by the lifecycle sweep.
    sessionRetentionDays: int = Field(default=365, ge=7, le=365, strict=True)


# The quick-skill bar over the chat composer: skill ids the user pinned. Only
# skills that actually offer a quick action qualify, and the bar stays small.
QUICK_SKILLS_MAX = 8
# 默认不置顶（owner 2026-10-05）：输入框上方改由 AI 建议问题占用，快捷技能栏只在
# 用户自己钉选后出现。apps/client/src/userPreferences.ts 的 DEFAULT_QUICK_SKILLS 是
# 这份默认的镜像，两边一起改。
DEFAULT_QUICK_SKILLS: list[str] = []


# 手机看盘 (2026-10-09): the 看盘 tab's top switch 报价 / 多图 / 单图 (it reopens on the last one), the
# 报价 list's 板块 and value columns (合约 is always first), the 多图 layout and its cells, and the sub
# pane the phone's single chart shows (one of chart.subPaneList; null = the first). A cell's symbol null
# fills itself (the current contract, then 自选, then the busiest main contracts).
MOBILE_MARKET_VIEWS = Literal["quotes", "multi", "single"]
MOBILE_QUOTE_BOARDS = Literal["watchlist", "main", "SHFE", "DCE", "CZCE", "CFFEX", "GFEX", "INE", "FOREIGN"]
MOBILE_QUOTE_COLUMNS = Literal["last", "changePercent", "change", "openInterest", "bid", "ask", "volume", "openInterestChange"]
MOBILE_QUOTE_COLUMNS_MAX = 4
MOBILE_MULTI_LAYOUTS = Literal["four", "periods", "six"]
MOBILE_CHART_DURATIONS = Literal[60, 180, 300, 900, 1800, 3600, 86400, 604800]
MOBILE_MULTI_CELLS = 6


class MobileChartCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str | None = Field(default=None, pattern=SYMBOL_PATTERN, max_length=40)
    duration: MOBILE_CHART_DURATIONS = 300


def _default_mobile_cells() -> list[MobileChartCell]:
    return [MobileChartCell() for _ in range(MOBILE_MULTI_CELLS)]


def _default_mobile_columns() -> list[MOBILE_QUOTE_COLUMNS]:
    return ["last", "changePercent", "openInterest"]


def _default_mobile_periods() -> list[MOBILE_CHART_DURATIONS]:
    return [300, 900, 3600, 86400]


class MobileMarketPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view: MOBILE_MARKET_VIEWS = "single"
    board: MOBILE_QUOTE_BOARDS = "watchlist"
    columns: list[MOBILE_QUOTE_COLUMNS] = Field(default_factory=_default_mobile_columns,
                                                min_length=1, max_length=MOBILE_QUOTE_COLUMNS_MAX)
    layout: MOBILE_MULTI_LAYOUTS = "four"
    # 4 合约 uses the first four, 6 图 all six; 同合约 4 周期 shows the current contract at `periods`.
    cells: list[MobileChartCell] = Field(default_factory=_default_mobile_cells, max_length=MOBILE_MULTI_CELLS)
    periods: list[MOBILE_CHART_DURATIONS] = Field(default_factory=_default_mobile_periods, min_length=4, max_length=4)
    subPane: SUBPANE_TYPES | None = None

    @model_validator(mode="after")
    def unique_columns(self):
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("报价列不能重复")
        return self


class UiPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quickSkills: list[str] = Field(default_factory=lambda: list(DEFAULT_QUICK_SKILLS),
                                   max_length=QUICK_SKILLS_MAX)
    # 设置 → 界面 → AI 形象 (2026-09-28): the floating AI figure and the chat avatar.
    # kline = K 线音柱, eyes = 灵动双眼, pixel = 像素小牛.
    aiAvatar: Literal["kline", "eyes", "pixel"] = "kline"
    # 设置 → 界面 → 布局 (2026-09-30): hide the collapsed positions bar when the account
    # has no positions and no working orders (the chart takes the space).
    autoHidePositionsBar: bool = True
    # 手机看盘 (2026-10-09): 报价 / 多图 / 单图 and each one's own settings, remembered per account.
    mobileMarket: MobileMarketPreferences = Field(default_factory=MobileMarketPreferences)

    @field_validator("quickSkills")
    @classmethod
    def quick_skills(cls, value: list[str]) -> list[str]:
        from .skill_registry import skill_by_id
        for skill_id in value:
            skill = skill_by_id(skill_id)
            if skill is None:
                raise ValueError(f"未知的技能：{skill_id}")
            if not skill.quick_action:
                raise ValueError(f"技能 {skill_id} 没有快捷操作")
        if len(set(value)) != len(value):
            raise ValueError("quickSkills 不能重复")
        return value


# 报价总表列可自定义 (2026-10-02): which columns the board shows (order = display order; 品种/合约
# are fixed first columns and never in the list), which one it sorts by and which exchange it filters
# to — all remembered per account so the board opens as it was left.
BOARD_COLUMN_TYPES = Literal[
    "last", "change", "changePercent", "volume", "openInterest", "openInterestChange",
    "high", "low", "preSettlement", "upperLimit", "lowerLimit", "quoteTime",
]
BOARD_COLUMNS_MAX = 12
DEFAULT_BOARD_COLUMNS: tuple[BOARD_COLUMN_TYPES, ...] = ("last", "change", "changePercent", "volume",
                                                         "openInterest", "openInterestChange", "high", "low")
BOARD_SORT_KEYS = ("changePercent", "volume", "openInterest", "openInterestChange")
# "watchlist" = the board's 自选 filter (only the products the user starred); added 2026-10-02.
BOARD_EXCHANGE_CODES = ("all", "watchlist", "SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX")


def _default_board_columns() -> list[BOARD_COLUMN_TYPES]:
    """The eight columns the fixed-era board always showed."""
    return list(DEFAULT_BOARD_COLUMNS)


class BoardSortPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Literal["changePercent", "volume", "openInterest", "openInterestChange"] = "changePercent"
    order: Literal["desc", "asc"] = "desc"


class MarketBoardPreferences(BaseModel):
    """报价总表: the column list (1–12, unique), the sort and the exchange filter. The UI and the
    AI write the same fields (`update_preferences`); `boardExchange`/`boardSort` UI commands apply
    through the window and the client writes the value back here."""
    model_config = ConfigDict(extra="forbid")
    columns: list[BOARD_COLUMN_TYPES] = Field(default_factory=_default_board_columns,
                                              min_length=1, max_length=BOARD_COLUMNS_MAX)
    sort: BoardSortPreferences = Field(default_factory=BoardSortPreferences)
    exchange: Literal["all", "watchlist", "SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX", "FOREIGN"] = "all"

    @model_validator(mode="after")
    def unique_columns(self):
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("报价表列不能重复")
        return self


# 自选品种 (2026-10-02): the products the user starred (报价总表行 / 合约抽屉 / 合约头部 write the same
# list). One entry per product — "<exchangeId>.<productId>" like "SHFE.rb" / "CZCE.CF", case as the
# catalog spells it; a foreign product is its 品种目录键 "COMEX.GC" (instruments.PRODUCT_KEY_PATTERN, 2026-10-21) — so the board's 自选 filter follows the main contract across roll-overs. The
# catalog itself is not read here: a starred product must survive a catalog read failing.
WATCHLIST_MAX = 50
_WATCHLIST_PRODUCT = re.compile(PRODUCT_KEY_PATTERN)


def _watchlist_products(value: list[str]) -> list[str]:
    """Format check plus order-preserving dedupe; a star toggle writes the whole list, so the cap
    is what keeps it bounded (50), not the dedupe."""
    for item in value:
        if not _WATCHLIST_PRODUCT.fullmatch(item):
            raise ValueError(f"自选品种格式应为 交易所.品种（如 SHFE.rb，外盘如 COMEX.GC）：{item}")
    return list(dict.fromkeys(value))


class WatchlistPreferences(BaseModel):
    """自选品种: the starred products, newest last. Read by the board's 自选 filter, the drawer and
    the contract head; the UI toggles and the AI's update_preferences write the same whole list."""
    model_config = ConfigDict(extra="forbid")
    products: list[str] = Field(default_factory=list, max_length=WATCHLIST_MAX)

    @field_validator("products", mode="before")
    @classmethod
    def _normalize(cls, value: list[str]) -> list[str]:
        return _watchlist_products(value)


# AI 简报订阅品种 (2026-10-05): the products briefs cover — the same "<exchangeId>.<productId>"
# keys as the watchlist. `products` doubles as the user-set marker: null (the default) = the user
# never chose, so reads derive the default (自选前 N，无自选取当前主力所在品种); a list, empty
# included, is the user's own choice and survives tier changes untouched. The tier cap applies at
# the /api/app/brief-subscriptions boundary, not here.
class BriefsPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    products: list[str] | None = Field(default=None, max_length=WATCHLIST_MAX)

    @field_validator("products", mode="before")
    @classmethod
    def _normalize(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _watchlist_products(value)


class UserPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice: VoicePreferences = Field(default_factory=VoicePreferences)
    chart: ChartPreferences = Field(default_factory=ChartPreferences)
    trade: TradePreferences = Field(default_factory=TradePreferences)
    chat: ChatPreferences = Field(default_factory=ChatPreferences)
    notify: NotifyPreferences = Field(default_factory=NotifyPreferences)
    data: DataPreferences = Field(default_factory=DataPreferences)
    ui: UiPreferences = Field(default_factory=UiPreferences)
    marketBoard: MarketBoardPreferences = Field(default_factory=MarketBoardPreferences)
    watchlist: WatchlistPreferences = Field(default_factory=WatchlistPreferences)
    briefs: BriefsPreferences = Field(default_factory=BriefsPreferences)


class PreferencesDocument(BaseModel):
    version: int
    preferences: UserPreferences


# Every category is optional in an update; inside a category only the keys sent change.
CATEGORIES = tuple(UserPreferences.model_fields)


def _known_model(annotation: Any) -> type[BaseModel] | None:
    """The one pydantic model an annotation carries, if it carries exactly one."""
    if get_origin(annotation) is Annotated:
        return _known_model(get_args(annotation)[0])
    if get_origin(annotation) is Union:
        models = [arg for arg in get_args(annotation)
                  if isinstance(arg, type) and issubclass(arg, BaseModel)]
        return models[0] if len(models) == 1 else None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def without_retired_skills(document: dict[str, Any]) -> dict[str, Any]:
    """A stored ui.quickSkills may pin a skill that has since been retired (strategy-guided-design,
    2026-10-08). Reading drops such pins so the rest of the document still validates — the validator
    refuses unknown ids, and one stale pin must never reset every other preference to defaults."""
    ui = document.get("ui")
    if not isinstance(ui, dict) or not isinstance(ui.get("quickSkills"), list):
        return document
    pins = ui["quickSkills"]
    from .skill_registry import skill_by_id
    kept = [pin for pin in pins if isinstance(pin, str) and (skill := skill_by_id(pin)) is not None and skill.quick_action]
    if len(kept) == len(pins):
        return document
    return {**document, "ui": {**ui, "quickSkills": kept}}


def split_known_fields(model: type[BaseModel], document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """522-F: split a stored document into the fields `model` still knows and the
    rest. A Runtime one step behind must read what it knows — an explicit off
    included — instead of falling back to whole-document defaults, and keep the
    stranger fields verbatim for the write-back."""
    known: dict[str, Any] = {}
    unknown: dict[str, Any] = {}
    for key, value in document.items():
        field = model.model_fields.get(key)
        if field is None:
            unknown[key] = value
            continue
        cleaned, stranger = _split_known_value(field.annotation, value)
        known[key] = cleaned
        if stranger:
            unknown[key] = stranger
    return known, unknown


def _split_known_value(annotation: Any, value: Any) -> tuple[Any, Any]:
    if isinstance(value, dict):
        nested = _known_model(annotation)
        return split_known_fields(nested, value) if nested is not None else (value, {})
    if isinstance(value, list):
        item = annotation
        if get_origin(item) is Annotated:
            item = get_args(item)[0]
        nested = _known_model(get_args(item)[0]) if get_origin(item) is list and get_args(item) else None
        if nested is None:
            return value, {}
        cleaned_items: list[Any] = []
        stranger_items: list[dict[str, Any] | None] = []
        for entry in value:
            if isinstance(entry, dict):
                cleaned, stranger = split_known_fields(nested, entry)
                cleaned_items.append(cleaned)
                stranger_items.append(stranger or None)
            else:
                cleaned_items.append(entry)
                stranger_items.append(None)
        return cleaned_items, stranger_items if any(stranger_items) else {}
    return value, {}


def restore_unknown_fields(document: dict[str, Any], unknown: dict[str, Any]) -> dict[str, Any]:
    """Merge the stranger fields back into the known document verbatim: dicts
    merge recursively, lists pair up index by index (the same items, cleaned)."""
    merged = dict(document)
    for key, value in unknown.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = restore_unknown_fields(current, value)
        elif isinstance(value, list) and isinstance(current, list):
            merged[key] = [
                restore_unknown_fields(item, stranger)
                if isinstance(stranger, dict) and isinstance(item, dict) else item
                for item, stranger in zip(current, value)
            ]
        else:
            merged[key] = value
    return merged


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def merge_preferences(current: UserPreferences, patch: dict[str, Any]) -> UserPreferences:
    """`patch` is a partial document ({"voice": {"speed": "fast"}}); unknown keys and bad values are
    rejected as a whole, so a write never lands half-applied."""
    if not isinstance(patch, dict) or not patch:
        raise HTTPException(422, "preferences 需要是至少含一个分类的对象")
    unknown = sorted(set(patch) - set(CATEGORIES))
    if unknown:
        raise HTTPException(422, f"未知的设置分类：{', '.join(unknown)}")
    merged = _deep_merge(current.model_dump(), patch)
    # A 1.6.12 client toggles kd/macd through the old `subPanes` switches: its patch carries no
    # `subPaneList`, so the stored list must not survive to outvote it — drop it and let
    # ChartPreferences derive the list from the patched switches.
    chart_patch = patch.get("chart")
    if isinstance(chart_patch, dict) and "subPanes" in chart_patch and "subPaneList" not in chart_patch:
        merged.get("chart", {}).pop("subPaneList", None)
    try:
        return UserPreferences.model_validate(merged)
    except ValidationError as error:
        first = error.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        raise HTTPException(422, f"设置值无效：{location}: {first.get('msg')}") from None


def preference_change_to_patch(key: str, value: Any) -> dict[str, Any]:
    """The `preference: {key, value}` UI command names one leaf by dotted path (voice.speed,
    notify.desktop.fills); the same partial document as a PUT."""
    parts = key.split(".")
    patch: Any = value
    for part in reversed(parts):
        patch = {part: patch}
    return patch


def preference_value(preferences: dict[str, Any], key: str) -> Any:
    node: Any = preferences
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def preference_instruction(preferences: UserPreferences, *, origin: str | None = None) -> str:
    """One line of Chinese the agent reads at the start of every turn; empty when every consumer
    setting is at its default so unchanged accounts see the prompt they had before."""
    trade, chat, voice = preferences.trade, preferences.chat, preferences.voice
    parts: list[str] = []
    if trade.defaultPriceIntent != "aggressive":
        parts.append("用户没说价格时开仓按挂单价（Runtime 已按这个默认定价，propose_order 不用填 price）")
    if trade.defaultVolume != 1:
        parts.append(f"用户没说手数时默认 {trade.defaultVolume} 手")
    if not trade.aiAutonomy:
        parts.append("本账号关闭了 AI 自主执行：策略的自动下单改为出卡确认，AI 也不能替用户挂止损"
                     "（对话里用户说的单本来就只用 propose_order 出方案卡）")
    if trade.closePreference != "auto":
        parts.append("平仓优先平今仓" if trade.closePreference == "today" else "平仓优先平昨仓")
    if chat.replyStyle == "detailed":
        parts.append("回答可以展开：结论之后给出依据、细节和表格，不必等用户追问")
    if not chat.autoReadScreen:
        parts.append("用户提到界面时不必先读页面快照，直接按已知信息回答")
    if origin == "voice" and not trade.voiceOrderConfirm:
        parts.append("本账号开了“语音下单免口头确认”（仅模拟）：这通电话里出的下单 / 平仓方案卡会按设置立即确认，"
                     "回复说“按你的设置直接下单”，不要问“确认吗”")
    if origin == "voice" and not voice.autoAnnotate:
        parts.append("通话中不要调 annotate_chart 或 highlight_ui 画图/高亮，用话说清楚即可")
    if not parts:
        return ""
    return "本账号设置（优先于下文的通用默认）：" + "；".join(parts) + "。\n"


def register_user_preferences(app, current_user) -> None:
    store = app.state.store

    @app.get("/api/preferences", response_model=PreferencesDocument)
    async def read_user_preferences(request: Request):
        """本账号的全部偏好设置（语音/图表/交易/AI 对话/通知/数据），含版本号。"""
        return store.get_preferences(await current_user(request))

    @app.put("/api/preferences", response_model=PreferencesDocument)
    async def update_user_preferences(request: Request, patch: dict[str, Any],
                                      version: int | None = Query(default=None, ge=0, description="上次读到的 version；不一致时 409，省略则无条件合并")):
        """部分更新：只传要改的分类和键（如 {"voice": {"speed": "fast"}}），返回更新后的全量设置与新版本号。
        未知键或越界值整体拒绝（422）；query 带 version 时与当前版本不一致返回 409（另一台设备已改）。"""
        user = await current_user(request)
        voice = getattr(app.state, "voice_realtime", None)
        wanted = (patch.get("voice") or {}).get("voice") if isinstance(patch.get("voice"), dict) else None
        if wanted is not None and voice is not None and wanted not in voice.voice_ids():
            raise HTTPException(404, "voice_unavailable")
        document = store.update_preferences(user, patch, expected_version=version)
        # Every writer (client, `preference` UI command, update_preferences tool, CLI) comes through
        # here, so this is where turning AI 自主执行 off ends the running execution lease.
        autonomy = getattr(app.state, "autonomy", None)
        if autonomy is not None and isinstance(patch.get("trade"), dict) and "aiAutonomy" in patch["trade"]:
            autonomy.apply_autonomy_preference(user, enabled=bool(document["preferences"]["trade"]["aiAutonomy"]))
        return document

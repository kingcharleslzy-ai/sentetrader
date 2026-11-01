"""Typed UI commands with per-user/window ownership and rendered-state receipts.

Only the UI heartbeat is a consumer. Product agents can enqueue and inspect
commands, never acknowledge their own commands through the capability bridge.
"""
import asyncio
import logging
import re
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .app_help import HELP_GROUPS, resolve_topic
from .instruments import PRODUCT_KEY_PATTERN, SYMBOL_PATTERN, foreign_display_name
from .ui_captures import ONLINE_SECONDS, CaptureReceipt, CaptureRequest, register_ui_captures
from .ui_surfaces import surface_aliases, surface_ids, surface_labels, surfaces_with
from .user_preferences import (
    CATEGORIES,
    MOBILE_CHART_DURATIONS,
    MOBILE_MARKET_VIEWS,
    MOBILE_MULTI_LAYOUTS,
    MOBILE_QUOTE_BOARDS,
    MOBILE_QUOTE_COLUMNS,
    MOBILE_QUOTE_COLUMNS_MAX,
    QUICK_SKILLS_MAX,
    SUBPANE_TYPES,
    UserPreferences,
    merge_preferences,
    preference_change_to_patch,
    preference_value,
)

# A command refused before it was queued never reached a window (#759): the refusal says so in
# this response header, and the app_call bridge turns it into `uiCommandAttempted: False` — the
# same "nothing was sent" mark its own schema check gives. A caller may then re-read the window and
# retry, or say plainly that nothing ran; a queued command with no receipt stays unknown.
UI_COMMAND_ATTEMPTED_HEADER = "X-UI-Command-Attempted"


def not_enqueued(status: int, detail: str) -> HTTPException:
    """A refusal of `POST /api/app/ui/commands` that provably queued nothing."""
    return HTTPException(status, detail, headers={UI_COMMAND_ATTEMPTED_HEADER: "false"})


# Agent-facing UI operations: commands plus the existing window/receipt/capture observations.
# Runtime heartbeat/acknowledgment routes are intentionally not agent operations.
UI_AGENT_OPERATIONS = frozenset({
    "GET /api/app/ui/windows", "GET /api/app/ui/windows/{window_id}",
    "POST /api/app/ui/commands", "GET /api/app/ui/commands/{command_id}",
    "POST /api/app/ui/windows/{window_id}/captures", "GET /api/app/ui/captures/{request_id}",
})


class ChartOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kd: bool
    macd: bool
    volume: bool
    crosshair: bool
    lastPriceLine: bool
    ma5: bool
    ma10: bool
    ma25: bool
    ma60: bool
    ma200: bool


class ChartOptionChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["kd", "macd", "volume", "crosshair", "lastPriceLine", "ma5", "ma10", "ma25", "ma60", "ma200"]
    visible: bool


class ChartSizes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    main: float = Field(ge=34, le=76)
    kd: float = Field(ge=12, le=36)
    macd: float = Field(ge=12, le=36)


class StrategyItem(BaseModel):
    strategyId: str = Field(max_length=200)
    name: str = Field(max_length=1000)
    version: int


class StrategyUiState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selectedStrategyId: str = Field(max_length=200)
    tab: Literal["library", "fees"]
    designerOpen: bool
    items: list[StrategyItem] = Field(max_length=200)


class StrategyUiChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selectedStrategyId: str | None = Field(default=None, min_length=1, max_length=200)
    tab: Literal["library", "fees"] | None = None
    designerOpen: bool | None = None

    @model_validator(mode="after")
    def single_change(self):
        if len(self.model_fields_set) != 1 or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("一次只修改一个策略导航字段")
        return self


class UiObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capturedAt: str = Field(max_length=40)
    viewport: dict[str, int]
    activeDialog: str | None = Field(default=None, max_length=200)
    text: str = Field(max_length=16000)
    truncated: bool
    regions: list[dict] = Field(max_length=30)
    controls: list[dict] = Field(max_length=150)
    context: dict


class UiDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deviceId: str = Field(min_length=8, max_length=100)
    platform: Literal["macos", "windows", "linux", "web"]
    version: str = Field(max_length=80)
    # Client build sha (the X-AI-Trader-Client header's `+sha` part); older clients omit it.
    build: str | None = Field(default=None, max_length=40)
    nativeCapture: bool = False


class PresentationChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme: Literal["dark", "light", "system"] | None = None
    fontScale: Literal["standard", "large", "extra-large"] | None = None
    density: Literal["compact", "standard", "comfortable"] | None = None
    dockSide: Literal["left", "right"] | None = None
    dockWidth: int | None = Field(default=None, ge=280, le=1600, strict=True)
    chartSizes: ChartSizes | None = None
    # 恢复默认布局: dock right, default width, positions bar collapsed (the settings button).
    reset: bool | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("组合设置至少包含一个有效的外观或布局字段")
        return self


# Interface surface ids (dialogs, disclosures, pages, panels, tabs) and their old aliases come from the one table
# in ui_surfaces.json (see ui_surfaces.py); what each one is lives there as its desc.
DIALOG_NAMES = Literal[surface_ids("dialogs")]
# Dialogs that only a user action may open: they carry a trading intent.
CLOSE_ONLY_DIALOGS = surfaces_with("dialogs", "closeOnly")
STRATEGY_DETAIL_SECTIONS = Literal["rules", "scope", "executions", "idea"]
# Round twelve: `<details>` sections and boolean toggles without a command of their own.
# Settings categories (设置重做 step 1, 1.6.6; split again 2026-09-30): the pages plus the
# 1.6.5 pages an older client still reports or an older command still names;
# `settings_page()` maps the old ones onto the new. The *-advanced pages are the
# second-level detail pages (no nav entry; their parent page links to them).
# The 用户管理 page ("admin") left with the 后台管理 split (2026-09-30): only the
# heartbeat still accepts it, commands reject it (see UiState.settingsPage).
SETTINGS_PAGE = Literal[surface_ids("settingsPages")]
SETTINGS_PAGE_STATE_ONLY = Literal[surface_ids("settingsPages", state=True)]
LEGACY_SETTINGS_PAGES = surface_aliases("settingsPages")


def settings_page(value):
    return LEGACY_SETTINGS_PAGES.get(value, value)


# The 价格阶梯 side panel ("depth") was removed 2026-10-07; its 五档 live in the 下单面板. Commands
# and tour steps still accept `panel: depth` (older agents, skills and saved tours) and normalize it
# to "order", so the forwarded command and its receipt check are the 下单面板's. Older desktop
# clients that still have the panel may report it in their heartbeat (UiState keeps the value).
LEGACY_PANELS = surface_aliases("panels")
PANEL = Literal[surface_ids("panels")]


def legacy_panel(value):
    return LEGACY_PANELS.get(value, value)


DISCLOSURE_NAMES = Literal[surface_ids("disclosures")]
# 实盘 is not connected to any futures company: the 设置 → 账户 switch and its command say so.
LIVE_TRADING_UNAVAILABLE = "暂无期货公司接入，实盘暂不可开启"
# Disclosures inside cards: the command applies to the most recent card unless `id` names one.
CARD_DISCLOSURES = surfaces_with("disclosures", "card")
# Disclosures with one entry per drawing / tool / menu: the client reports only the open ones, so a missing
# `name:id` is closed.
SPARSE_DISCLOSURES = surfaces_with("disclosures", "sparse")
# Old disclosure names a command may still use, and the surface the client opens (and reports) for them.
DISCLOSURE_ALIASES = surface_aliases("disclosures")
# 持仓面板 · 成交 (2026-09-25): positionsTab `fills` replaced `trades` (平仓记录); the old value is an alias.
POSITIONS_TAB = Literal[surface_ids("positionsTabs")]
POSITIONS_TAB_ALIASES = surface_aliases("positionsTabs")
# 成交 list's 来源 filter and the 策略面板's three tabs.
FILLS_SOURCE = Literal["all", "manual", "card", "ai", "strategy", "stop", "liquidation"]
# 策略页改版第八批 (2026-10-08): two tabs — library 我的策略 (the 新建策略 button and one card per strategy)
# and running 正在运行 — plus detail, the selected strategy's 结果页 (opened from a card). The
# retired 实验室 tab's lab is still accepted: it opens the 结果页 at its 试几组参数比一比 (reported detail).
STRATEGY_TAB = Literal[surface_ids("strategyTabs")]
STRATEGY_TAB_ALIASES = surface_aliases("strategyTabs")
# 手机版 策略 (2026-10-08): its two tabs — running 正在运行 / mine 我的策略.
MOBILE_STRATEGY_TAB = Literal[surface_ids("mobileStrategyTabs")]
# 手机版 (2026-10-04): the phone layout's bottom tabs — ai = AI 首页 (home), chat = AI 对话 (the
# conversation, the same state as `panel: ai`), research 调研 / market 看盘 / strategy 策略 / me 我的.
# 2026-10-06 (手机版新结构): 通知 merged into AI 首页's timeline and its tab became 策略; the old value
# notify is still accepted and opens AI 首页 (its filter kept) — a newer client reports mobileTab ai.
MOBILE_TAB = Literal[surface_ids("mobileTabs")]
MOBILE_TAB_ALIASES = surface_aliases("mobileTabs")
# 手机版第二期 (2026-10-04): the full-screen pages over the tabs — order 下单页, positions 持仓与委托,
# review 复盘摘要, and the 我的 rows that open an existing surface in its phone form: accounts = 设置 →
# 账户, memory = 设置 → 记忆, appearance = 设置 → 界面, help = 使用说明卡. 2026-10-05: briefs = 我的 →
# AI 简报推送 (the push switch and the subscribed products). 2026-10-06: install = 安装到桌面引导
# (我的 → 设置 → 添加到手机桌面; it also opens by itself on the phone web page); push = 锁屏提醒
# (我的 → 通知设置 → 锁屏提醒: this device's switch, 通知哪些事, 锁屏上, the devices that have it).
# 2026-10-06 (手机版新结构): lab = 回测结果页 (策略 → 最近的回测, the latest comparison; `mobileLab` opens one).
# 2026-10-15 (统一提醒 #932): reminders = 提醒 (the 提醒中心 the AI 首页 bell opens; `reminderCenter` opens it
# on either layout); push keeps this device's switch and the devices — the kinds moved to 设置 → 提醒.
MOBILE_PAGE = Literal[surface_ids("mobilePages")]
# 锁屏提醒 (2026-10-06): what this browser can do about lock-screen push, reported while mobilePage is push —
# supported (a home-screen iPhone, Safari, Edge, Firefox), needs-install (iPhone in a Safari tab or an app)
# or unsupported (Android, Chrome and the browsers on Google's push service, which is unreachable here).
PUSH_SUPPORT = Literal["supported", "needs-install", "unsupported"]


class LiveTradingState(BaseModel):
    """设置 → 账户 → 实盘 (2026-10-08), while that page shows: always off, and why it cannot be turned on."""
    model_config = ConfigDict(extra="forbid")
    state: Literal["off"]
    disabledReason: str = Field(max_length=80)
class MemoryPanelState(BaseModel):
    """设置 → AI 与记忆 (desktop page or the phone's 记忆 page), while it shows (AI 审计 2026-10-13): whether
    memory is on and how many entries each layer holds; the entries themselves read GET /api/memory."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    counts: dict[Literal["profile", "rule", "lesson"], int]


class RoutineSummaryState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(max_length=40)
    steps: list[str] = Field(default_factory=list, max_length=20)
    lastRunAt: str | None = Field(default=None, max_length=40)


class RoutinesPanelState(BaseModel):
    """我的流程 (设置 → AI 与记忆), while it shows (AI 审计 2026-10-13): the saved routines in order, each
    with its steps as the list shows them; GET /api/routines carries the full definitions."""
    model_config = ConfigDict(extra="forbid")
    count: int = Field(ge=0)
    items: list[RoutineSummaryState] = Field(default_factory=list, max_length=20)


# The browser environment the install guide teaches (reported while mobilePage is install).
INSTALL_ENVIRONMENT = Literal["installed", "in-app-ios", "in-app-android", "ios-safari-26", "ios-safari-legacy", "ios-chrome-edge",
                              "ios-other", "android-prompt", "android-vendor", "desktop"]
# AI 首页时间线的筛选（原通知页）：全部 / 成交回报 / AI 简报（今日判断与盘中简报）。2026-10-15: 提醒 left the
# timeline for the 提醒中心; the old value reminder is still accepted and opens the 提醒 page.
MOBILE_NOTIFY_FILTER = Literal["all", "reminder", "fill", "brief"]
# 提醒中心 (统一提醒 #932, 2026-10-15): which reminders it lists — unread only or all of the day.
REMINDER_FILTER = Literal["unread", "all"]


class ReminderCenterChange(BaseModel):
    """提醒中心: open or close it (the computer's bell popover, the phone's 提醒 page) and pick its filter;
    a filter alone also opens it. `more` = 继续加载当天提醒 (#932 R5): the next batch of a day past its first
    500 reminders, while the snapshot's moreToday says there is one (opens the center too)."""
    model_config = ConfigDict(extra="forbid")
    open: bool | None = None
    filter: REMINDER_FILTER | None = None
    more: Literal[True] | None = None


class ReminderCenterState(BaseModel):
    """提醒中心, reported while the window has one (a member's connected window): open, its filter, the
    user's unread count, how many reminders of the current trading day are read so far, whether more of them
    are still to read past the page limit (moreToday: 继续加载当天提醒), the last `more` command's outcome (moreAsked =
    its number, moreDone = the number whose read finished, moreError = why it loaded nothing) and whether 查看更早
    was read."""
    model_config = ConfigDict(extra="forbid")
    open: bool
    filter: REMINDER_FILTER
    unread: int = Field(ge=0)
    today: int = Field(ge=0)
    moreToday: bool = False
    moreAsked: int = Field(default=0, ge=0)
    moreDone: int = Field(default=0, ge=0)
    moreError: str = Field(default="", max_length=200)
    earlier: bool = False


class MobileLabChange(BaseModel):
    """手机版回测结果页 (2026-10-06): the comparison shown (`GET /api/strategy-labs/{labId}`)."""
    model_config = ConfigDict(extra="forbid")
    labId: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class MobileLabState(BaseModel):
    """The 回测结果页's comparison and the variant 选用 of it (null until one is chosen)."""
    model_config = ConfigDict(extra="forbid")
    labId: str = Field(max_length=64)
    chosen: str | None = Field(default=None, max_length=40)


class MobileChartCellChange(BaseModel):
    """手机看盘 · 多图 (2026-10-09): one cell of the current layout — its contract (null = 自动补位) and/or
    period. In 同合约 4 周期 the contract is the chart's own (a symbol there switches the current contract)."""
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0, le=5, strict=True)
    symbol: str | None = Field(default=None, pattern=SYMBOL_PATTERN, max_length=40)
    duration: MOBILE_CHART_DURATIONS | None = None

    @model_validator(mode="after")
    def something(self):
        if "symbol" not in self.model_fields_set and self.duration is None:
            raise ValueError("cell 需要 symbol 或 duration")
        return self


class MobileMarketChange(BaseModel):
    """手机看盘 (2026-10-09), phone layout only; one key per command, each opens the 看盘 tab first.
    view 报价 / 多图 / 单图; board and columns land on 报价, layout and cell on 多图, subPane and subPanes
    (the 副图设置 sheet) on 单图. The values are account preferences (ui.mobileMarket, chart.subPaneList)
    reported in state.preferences; the sheet is disclosures.mobile-subpanes."""
    model_config = ConfigDict(extra="forbid")
    view: MOBILE_MARKET_VIEWS | None = None
    board: MOBILE_QUOTE_BOARDS | None = None
    columns: list[MOBILE_QUOTE_COLUMNS] | None = Field(default=None, min_length=1, max_length=MOBILE_QUOTE_COLUMNS_MAX)
    layout: MOBILE_MULTI_LAYOUTS | None = None
    cell: MobileChartCellChange | None = None
    # Show this pane under the single chart; one not chosen yet takes the shown one's place (or is added).
    subPane: SUBPANE_TYPES | None = None
    subPanes: bool | None = None

    @model_validator(mode="after")
    def one_key(self):
        given = [key for key in self.model_fields_set if getattr(self, key) is not None]
        if len(given) != 1 or len(self.model_fields_set) != 1:
            raise ValueError("mobileMarket 一次只改一项：view / board / columns / layout / cell / subPane / subPanes")
        if self.columns is not None and len(set(self.columns)) != len(self.columns):
            raise ValueError("报价列不能重复")
        return self


class DialogChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: DIALOG_NAMES
    open: bool
    # `help` only: the manual topic to show.
    topic: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def openable(self):
        if self.open and self.name in CLOSE_ONLY_DIALOGS:
            raise ValueError("该确认框只能由用户操作打开；命令只能关闭它")
        if self.topic is not None and self.name != "help":
            raise ValueError("topic 只用于 help")
        return self


class HelpFaqChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0, le=19, strict=True, description="常见问题的序号（从 0 数）")
    open: bool


class HelpChange(BaseModel):
    """使用说明卡 (2026-09-25 两层重做): the card opens first when it is closed. One command may combine
    keys, applied in order: topic / group → query → detail → faq."""
    model_config = ConfigDict(extra="forbid")
    topic: str | None = Field(default=None, max_length=80, description="打开该主题（旧 id 按别名解析）")
    group: str | None = Field(default=None, max_length=40,
                              description="打开该分组的第一个主题；developer 同时展开导航底部的“开发者参考”")
    query: str | None = Field(default=None, max_length=80,
                              description="搜索词（匹配标题、summary、steps、faq、detail；空串清除；命中 detail 时自动展开完整说明）")
    detail: bool | None = Field(default=None, description="展开 / 收起当前主题的“完整说明”")
    faq: HelpFaqChange | None = Field(default=None, description="展开 / 收起当前主题的一个常见问题")
    retry: Literal[True] | None = Field(
        default=None, description="说明读取失败后重新读取（卡上的“重试”；快照 state.help.canRetry 为 true 时可用，"
                                  "已读到时 rejected；卡没开时打开并读取）")

    @model_validator(mode="after")
    def known(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("help 至少包含 topic、group、query、detail、faq、retry 之一")
        if self.topic is not None and resolve_topic(self.topic) is None:
            raise ValueError("没有这个帮助主题（主题 id 见 GET /api/app/help）")
        if self.group is not None and self.group not in {group["id"] for group in HELP_GROUPS}:
            raise ValueError("没有这个帮助分组（分组见 GET /api/app/help 的 groups）")
        return self


class DisclosureChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: DISCLOSURE_NAMES
    open: bool
    # Card disclosures only: the conversation card id; omitted means the most recent card.
    id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def card_only_id(self):
        if self.name == "live-trading" and self.open:
            raise ValueError(LIVE_TRADING_UNAVAILABLE)
        if self.id is not None and self.name not in CARD_DISCLOSURES:
            raise ValueError("id 只用于卡内与对象上的折叠（progress-steps、advice-sources、tool-card、diff-card、card-details、"
                             "strategy-run-log、strategy-signal-action、strategy-stop-confirm、strategy-actions、strategy-delete-confirm、ai-menu、"
                             "strategy-backtest-trades、strategy-param-help、strategy-template-details、chart-pane-editor、"
                             "position-protection、routine-delete、flash-event、news-event、flash-orig、news-orig、strategy-editor-section、strategy-editor-condition、"
                             "strategy-editor-picker、help-tip、chart-drawing-group、chart-drawing-tool、chart-drawing-style、"
                             "chart-drawing-settings、chart-context-menu、mobile-chart-cell、mobile-drawing-style-row）")
        return self


class ChatSessionChange(BaseModel):
    """Switch the AI dock to a chat session: `sessionId` from `chatHistory`, or `new: true` for a fresh one."""
    model_config = ConfigDict(extra="forbid")
    sessionId: str | None = Field(default=None, min_length=1, max_length=200)
    new: bool = False

    @model_validator(mode="after")
    def one_target(self):
        if bool(self.sessionId) == self.new:
            raise ValueError("chatSession 需要 sessionId 或 new: true 之一")
        return self


class ChatHistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sessionId: str = Field(min_length=1, max_length=200)
    createdAt: str = Field(min_length=1, max_length=80)


class PinnedSymbolsChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add: str | None = Field(default=None, pattern=SYMBOL_PATTERN, max_length=40)
    remove: str | None = Field(default=None, pattern=SYMBOL_PATTERN, max_length=40)

    @model_validator(mode="after")
    def single(self):
        if (self.add is None) == (self.remove is None):
            raise ValueError("pinnedSymbols 一次只能 add 或 remove 一个合约")
        return self


class ReviewExpandChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    day: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def single(self):
        if (self.month is None) == (self.day is None):
            raise ValueError("reviewExpand 一次展开一个月或一个交易日")
        return self


class StrategyDetailsChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: STRATEGY_DETAIL_SECTIONS
    open: bool


SIGNAL_ACTION = Literal["alert", "confirm", "auto"]


class StrategyLaunchChange(BaseModel):
    """The open 上线 dialog's fields (open it with `dialog: strategy-launch`); submitting stays a click."""
    model_config = ConfigDict(extra="forbid")
    symbol: str | None = Field(default=None, pattern=SYMBOL_PATTERN, max_length=40)
    signalAction: SIGNAL_ACTION | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("strategyLaunch 至少包含 symbol 或 signalAction")
        return self


# #932 复核 (P1, owner 2026-10-16): whether the AI may order by itself (AI 自主执行), whether a spoken order still waits for
# the spoken 确认 (语音下单免口头确认) and a running strategy's 执行方式 are trading controls, not interface state. They
# change only through their own interfaces — the preference document (update_preferences / PUT /api/preferences) and the
# strategy's monitor (start_strategy_monitoring / POST …/monitor/start) — which the AI uses when the user asks for it.
# A UI command carrying one is refused before it is queued (the client does not apply one either), so an interface
# request can never change them on the way.
TRADE_CONTROL_KEYS = frozenset({"aiAutonomy", "voiceOrderConfirm"})  # under `trade`
TRADE_CONTROL_REFUSED = ("AI 自主执行与语音下单免口头确认不经界面命令修改：用户要改时用 update_preferences"
                         "（PUT /api/preferences）改 trade.aiAutonomy / trade.voiceOrderConfirm")
SIGNAL_ACTION_REFUSED = ("运行中策略的执行方式不经界面命令修改：用户要改时用 start_strategy_monitoring"
                         "（POST /api/strategies/{strategy_id}/monitor/start 的 signalAction）")


class StrategyInstanceChange(BaseModel):
    """Retired (#932 复核): a running card's 执行方式 is a trading control. The field stays in the schema for older
    agents and is refused with the interface that changes it (SIGNAL_ACTION_REFUSED)."""
    model_config = ConfigDict(extra="forbid")
    strategyId: str = Field(min_length=1, max_length=200)
    signalAction: SIGNAL_ACTION


class StrategyBacktestChange(BaseModel):
    """规则回测 (2026-09-25) on the selected strategy's 详情: the parameter row, 重跑, 在图上标出买卖点
    and the folded 逐笔清单. run=true runs the same POST …/backtest the 回测 button sends."""
    model_config = ConfigDict(extra="forbid")
    tradingDays: int | None = Field(default=None, ge=1, le=2000, description="区间：最近 N 个交易日")
    slippageTicks: int | None = Field(default=None, ge=0, le=5, description="滑点跳数")
    run: Literal[True] | None = Field(default=None, description="按当前参数回测（与“回测”按钮同一接口）")
    mark: bool | None = Field(default=None, description="true 把开平仓点画到图上（先切到回测的合约与周期），false 清除")
    trades: bool | None = Field(default=None, description="展开 / 收起逐笔清单")
    applyAdvice: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,40}$",
        description="策略体检的“按这个改”：按回测结果里这条建议（advice id）改好，存为新版本并自动重跑回测")

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("strategyBacktest 至少包含 tradingDays、slippageTicks、run、mark、trades、applyAdvice 之一")
        return self


class StrategyTemplateChange(BaseModel):
    """策略模板库 on the 结果页: restore 让来自模板的策略恢复模板默认参数（另存为新版本）。
    The gallery, the parameter page and the template chips were retired 2026-10-08: a template is now the
    策略编辑卡's 起点 (`strategyEditor: {new: true, start: "template:<id>"}`). Their keys stay in the schema
    for older agents and are refused with that pointer."""
    model_config = ConfigDict(extra="forbid")
    open: bool | None = Field(default=None, description="已下线：模板画廊并入策略编辑卡的“从模板”")
    templateId: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$", max_length=60,
                                   description="已下线：用 strategyEditor {new: true, start: template:<id>}")
    params: dict[str, float | None] | None = Field(default=None, description="已下线")
    timeframe: Literal[60, 180, 300, 900, 3600, 86400] | None = Field(default=None, description="已下线")
    run: Literal[True] | None = Field(default=None, description="已下线")
    save: Literal[True] | None = Field(default=None, description="已下线")
    restore: Literal[True] | None = Field(default=None, description="结果页：来自模板的策略恢复模板默认参数（另存新版本）")

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("strategyTemplate 只支持 restore")
        if self.model_fields_set != {"restore"}:
            raise ValueError(TEMPLATE_RETIRED)
        return self


# 2026-10-08: the 策略编辑卡 replaced the AI 引导设计卡 (strategyGuide), 我的策略's ask box (strategyIdea) and
# the template gallery. The old commands stay in the UiChanges schema for older agents and are refused
# with the command that does the job now.
TEMPLATE_RETIRED = ("模板画廊已并入策略编辑卡：用 strategyEditor {new: true, start: template:<模板id>} 从模板新建，"
                    "strategyTemplate 只剩 restore")
GUIDE_RETIRED = ("AI 引导设计已下线：新建或修改策略用策略编辑卡——strategyEditor {new: true} 新建（有成交时按习惯预填），"
                 "{ask: 一句话} 让 AI 改表单")
IDEA_RETIRED = "我的策略的输入框已换成策略编辑卡：用 strategyEditor {new: true, ask: 一句话}"
EDITOR_START = r"^(blank|habits|template:[a-z][a-z0-9_]{0,59})$"


class StrategyEditorChoice(BaseModel):
    """编辑卡上点选的几项 (改版第一步 2026-10-31)：提醒的响几次、到期、整张自选表；策略的“信号来了怎么办”。"""
    model_config = ConfigDict(extra="forbid")
    repeat: Literal["once", "rearm", "bar"] | None = Field(default=None,
        description="提醒·响几次：once 只响一次（响过自动停）/ rearm 条件再满足再响 / bar 每根 K 线最多一次")
    until: Literal["today", "week", "forever"] | None = Field(default=None,
        description="提醒·到期：today 今天收盘 / week 本周 / forever 一直（到期自动停并说一声）")
    watchlist: bool | None = Field(default=None,
        description="提醒·盯哪个品种：true 整张自选表（开始提醒时按每个自选品种的主力合约各上线一个，分别判断）/ false 只盯卡上的合约")
    signalAction: Literal["alert", "confirm", "auto"] | None = Field(default=None,
        description="策略·信号来了怎么办：上线运行对话框默认选中的执行方式（alert 只提醒 / confirm 出卡确认 / auto 自动下单，"
                    "auto 要 设置 → 交易 → AI 自主执行 开着）；上线仍由用户点")


class StrategyEditorChange(BaseModel):
    """策略编辑卡 (2026-10-08): 新建或修改策略都打开这张卡（策略面板里）。One command may combine keys, applied in
    order: close → open / new / edit → start → answers → ask → focus → save. The card only designs: saving stores a
    draft (a new version when editing), 上线运行 stays the user's click on the existing launch dialog."""
    model_config = ConfigDict(extra="forbid")
    open: Literal[True] | None = Field(default=None, description="打开编辑卡：有没存的草稿就接着改，没有就新建（同 new）")
    new: Literal[True] | None = Field(default=None,
        description="新建：起点默认“按我的习惯”（近 20 个交易日已平仓成交至少 5 笔时），不够就空白；合约默认当前图表合约")
    reminder: Literal[True] | None = Field(default=None,
        description="和 new 一起用：新建提醒（提醒模式，2026-10-25 第 6 批）——只填条件和提醒时段，不填手数和止损；保存为只提醒的策略"
                    "（plan.reminderOnly），列在提醒面板“我的提醒”、不出现在“我的策略”，“开始提醒”按只提醒上线（不出卡、不下单）")
    edit: str | None = Field(default=None, min_length=1, max_length=200,
        description="修改我的策略里的这条（strategyId）：卡片改不了的规则原样保留，存的时候存为新版本，原版本保留")
    start: str | None = Field(default=None, pattern=EDITOR_START,
        description="起点（替换表单内容）：blank 空白 / habits 按我的习惯（成交不够时打开空白，快照 prefill 写明原因）/ "
                    "template:<模板id> 从模板（模板 id 见 GET /api/strategy-templates）")
    answers: dict[str, str] | None = Field(default=None,
        description="回答补一句后 AI 追问的问题（快照 questions）：问题 id → 回答，答完带着答案重新生成")
    ask: str | None = Field(default=None, min_length=1, max_length=500,
        description="补一句：表单空时按这句生成整份计划，已有内容时只改相关字段（POST /api/strategy-plans/design-jobs，"
                    "带 previousPlan）；改到的字段高亮，不直接保存")
    focus: Literal["basics", "entry", "exit", "sizing", "trigger", "entryLong", "entryShort", "action", "stop", "take", "trailing",
                   "breakeven", "scaleOut", "timeTakeProfit", "maxHold", "closeBeforeEnd", "exitRules", "limits"] | None = Field(default=None,
        description="流程图定位：把表单滚到这一块并短暂高亮（同点流程图的方块）——四段 basics / entry / exit / sizing，或流程图方块 "
                    "trigger 触发、entryLong / entryShort 开多 / 开空条件、action 开仓手数、stop 止损、take 止盈、trailing 追踪、breakeven 保本、"
                    "scaleOut 分批止盈、timeTakeProfit 按时间止盈、maxHold 最长持仓、closeBeforeEnd 收盘前平仓、exitRules 按条件平仓、limits 冷却与每天次数；"
                    "在“更多”里的会先展开；手机上展开对应的段（快照 state.strategyEditor.focus）")
    save: Literal[True] | None = Field(default=None,
        description="存为草稿（开仓条件和止损填好才能存；修改已有策略时存为新版本），存好后打开它的结果页并自动回测")
    close: Literal[True] | None = Field(default=None, description="关闭编辑卡，放弃没存的改动")
    choose: StrategyEditorChoice | None = Field(default=None,
        description="点选卡上的几项（不经 AI，改了要存）：提醒模式的 repeat / until / watchlist，策略的 signalAction；"
                    "快照 state.strategyEditor.fields.reminder 与 fields.signalAction")

    @model_validator(mode="after")
    def valid(self):
        keys = self.model_fields_set
        if not keys or any(getattr(self, key) is None for key in keys):
            raise ValueError("strategyEditor 至少包含 open、new、edit、start、answers、ask、focus、choose、save、close 之一")
        if self.choose is not None and not self.choose.model_fields_set:
            raise ValueError("choose 至少包含 repeat、until、watchlist、signalAction 之一")
        if self.close and len(keys) > 1:
            raise ValueError("close 不和其他字段一起用")
        if sum(key in keys for key in ("open", "new", "edit")) > 1:
            raise ValueError("open、new、edit 一次只用一个")
        if self.reminder and not self.new:
            raise ValueError("reminder 和 new 一起用：新建提醒")
        if self.answers is not None and (not self.answers or len(self.answers) > 3 or any(
                not 1 <= len(key) <= 80 or not 1 <= len(value.strip()) <= 2000 for key, value in self.answers.items())):
            raise ValueError("answers 是 1–3 个问题 id → 回答")
        return self


class StrategyLabChange(BaseModel):
    """试几组参数比一比 (策略页改版第八批, 2026-10-08; 原“实验室”页签已并入结果页): 选中策略最关键的一个
    参数跑当前值和上下各一组，前 70% 历史用来挑、后 30% 单独检验；界面默认在本机浏览器算（本机优先，
    2026-10-12），算不了自动转云端。open 打开选中策略的结果页并停在比较；run 开始比较；labId 打开那次
    比较所属的策略（旧比较没有所属策略时只回报那次比较）；choose 把某组存为我的策略（服务端先重跑权威
    回测，name 可选名字）。useServer 是旧字段，接受但不再区分。"""
    model_config = ConfigDict(extra="forbid")
    open: bool | None = Field(default=None, description="打开选中策略的结果页，停在“试几组参数比一比”")
    labId: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,40}$", description="打开这次比较所属策略的结果页")
    run: Literal[True] | None = Field(default=None, description="对选中策略开始比较（POST /api/strategy-plans/{id}/compare）")
    useServer: bool | None = Field(default=None, description="旧字段：接受但不再区分（界面默认本机算，算不了转云端）")
    choose: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,40}$", description="把这组变体存为我的策略（variantId，服务端先重跑权威回测）")
    name: str | None = Field(default=None, min_length=1, max_length=60, description="存为我的策略时的名字（配合 choose）")

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("strategyLab 至少包含 open、labId、run、useServer、choose、name 之一")
        return self


class MonitorCardChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["research"]
    expanded: bool


class PositionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=SYMBOL_PATTERN, max_length=40)
    direction: Literal["long", "short"]


# AI annotation layer, step one (2026-09-18): the agent points at a surface (`highlight`) and draws
# its view on the main chart (`chartAnnotations`, `chartFocus`). Drawing is opinion, never an order.
HIGHLIGHT_MAX = 3
# v2 (user feedback on 1.6.5: "too crowded"): six drawings per contract at most, numbered ①②③ on the
# chart with a short label; the explanation lives in the reply, referenced by number.
CHART_ANNOTATIONS_MAX = 6
ANNOTATION_LABEL_MAX = 8
ANNOTATION_KINDS = Literal["point", "hline", "zone", "trendline", "vline", "circle", "arrow", "text"]
ANNOTATION_TONES = Literal["info", "warn", "danger", "good"]
# 图表指标可配置 (2026-10-02): the panes follow the account's chart.subPaneList; rsi (0–100) and
# oi (持仓量, 手) joined kd/macd; cci on 2026-10-09 (the 成交量 pane takes no drawings). A pane the user
# switched off is not drawn.
ANNOTATION_PANES = Literal["main", "kd", "macd", "rsi", "oi", "cci"]
ANNOTATION_ROLES = Literal["buy", "sell", "close", "note"]
# Indicator panes draw only what is meaningful on an oscillator: levels, moments, points, words.
SUBPANE_KINDS = {"hline", "vline", "point", "text"}


class HighlightChange(BaseModel):
    """Pulse or outline a surface with a small label bubble; `clear: true` removes every highlight."""
    model_config = ConfigDict(extra="forbid")
    target: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$",
                               description="表面/控件稳定 id：data-agent-target、data-testid 或 data-region 的值（快照 context.highlightTargets 列出可用值）")
    label: str | None = Field(default=None, max_length=40)
    style: Literal["pulse", "outline"] = "pulse"
    durationMs: int = Field(default=6000, ge=0, le=15000, strict=True, description="1000–15000；0 = 清除该目标")
    clear: bool = False

    @model_validator(mode="after")
    def target_or_clear(self):
        if self.clear == bool(self.target):
            raise ValueError("highlight 需要 target 或 clear: true 之一")
        if self.target and 0 < self.durationMs < 1000:
            raise ValueError("durationMs 取 1000–15000，0 表示清除该目标")
        return self


class HighlightState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(max_length=80)
    label: str | None = Field(default=None, max_length=40)
    style: Literal["pulse", "outline"]
    until: str = Field(max_length=40)


class ChartAnnotationItem(BaseModel):
    """One drawing on a chart pane. Times are bar timestamps in epoch seconds (the chart's own unit,
    `chartViewport.timeRange` in the snapshot); `price` is the pane's value unit (contract price on
    main, K/D on kd, DIF/DEA/柱 on macd, 0–100 on rsi, 手 on oi). `point` is the precise
    entry/exit/close marker (role picks the glyph and tone); hline/zone are for levels and areas
    only. Labels are ≤8 chars: the chart shows the item's number ①②③ plus this label, the
    explanation goes in the reply."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    kind: ANNOTATION_KINDS
    pane: ANNOTATION_PANES = "main"
    role: ANNOTATION_ROLES | None = Field(default=None, description="point 用：buy 上三角、sell 下三角、close 方块、note 圆点")
    price: float | None = None
    price2: float | None = None
    time: int | None = Field(default=None, ge=0, strict=True)
    time2: int | None = Field(default=None, ge=0, strict=True)
    label: str | None = Field(default=None, max_length=ANNOTATION_LABEL_MAX, description="≤8 字短标签，如“空 15420”；解释写在回复里按编号讲")
    tone: ANNOTATION_TONES | None = Field(default=None, description="省略时 point 按 role 取色（buy=good、sell=danger、close=warn、note=info），其余 info")

    @model_validator(mode="after")
    def coordinates(self):
        need = {"point": ("price", "time"), "hline": ("price",), "zone": ("price", "price2"),
                "trendline": ("price", "time", "price2", "time2"), "vline": ("time",), "circle": ("price", "time"),
                "arrow": ("price", "time"), "text": ("price", "time")}[self.kind]
        missing = [key for key in need if getattr(self, key) is None]
        if missing:
            raise ValueError(f"{self.kind} 需要 {', '.join(need)}")
        if self.kind == "text" and not self.label:
            raise ValueError("text 需要 label")
        if self.pane != "main" and self.kind not in SUBPANE_KINDS:
            raise ValueError(f"{self.pane} 副图只支持 hline、vline、point、text")
        if self.role is not None and self.kind != "point":
            raise ValueError("role 只用于 point")
        if self.pane == "main" and any(value is not None and value <= 0 for value in (self.price, self.price2)):
            raise ValueError("主图 price 必须大于 0")
        return self


class ChartAnnotationsChange(BaseModel):
    """`set` replaces, `add` appends (same id replaces), `clear` removes the AI's drawings on one contract
    (default: the window's current contract). At most 6 items per contract — one screen stays readable."""
    model_config = ConfigDict(extra="forbid")
    op: Literal["set", "add", "clear"]
    items: list[ChartAnnotationItem] = Field(default_factory=list, max_length=CHART_ANNOTATIONS_MAX)
    symbol: str | None = Field(default=None, pattern=SYMBOL_PATTERN, max_length=40)

    @model_validator(mode="after")
    def items_by_op(self):
        if self.op == "clear" and self.items:
            raise ValueError("clear 不带 items")
        if self.op != "clear" and not self.items:
            raise ValueError(f"{self.op} 需要至少一项 items")
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("items 的 id 重复")
        return self


TOUR_MAX_STEPS = 20


def _array_branches_last(schema: dict) -> None:
    """Pydantic's smart union emits the `list[str]` branch of `value` before `null`, which would
    read against the compat baseline as a type change at anyOf[4]. A union is unordered on the
    wire; re-emit it with the baseline's order intact so the list is a purely appended branch."""
    branches = schema.get("anyOf")
    if branches:
        schema["anyOf"] = [branch for branch in branches if branch.get("type") != "array"] \
            + [branch for branch in branches if branch.get("type") == "array"]


class PreferenceChange(BaseModel):
    """Set one account preference from the settings UI: the client writes it through PUT /api/preferences
    and reports the document back in `preferences`. Agents normally use update_preferences directly.
    `ui.quickSkills` is the one list-valued key (the quick-skill bar's pinned skill ids)."""
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z]+(\.[A-Za-z]+){1,2}$", max_length=60,
                     description="点分路径，如 voice.speed、trade.defaultVolume、notify.desktop.fills、ui.quickSkills")
    value: str | int | float | bool | None | list[str] = Field(default=None, json_schema_extra=_array_branches_last)

    @field_validator("value")
    @classmethod
    def quick_skill_pins(cls, pins: "str | int | float | bool | None | list[str]"):
        # max_length on the Field itself cannot span a union (it would TypeError on the scalar
        # branches), so the quick-skill cap rides this validator; the registry rules and the
        # duplicate check live in UiPreferences and run again in known_leaf's merge below.
        if isinstance(pins, list) and len(pins) > QUICK_SKILLS_MAX:
            raise ValueError(f"quickSkills 最多 {QUICK_SKILLS_MAX} 个")
        return pins

    @model_validator(mode="after")
    def known_leaf(self):
        # The same validation as a PUT: an unknown key or an out-of-range value is rejected here, not
        # after the client tried to save it.
        if self.key.split(".")[0] not in CATEGORIES:
            raise ValueError("未知的设置键")
        patch = preference_change_to_patch(self.key, self.value)
        trade = patch.get("trade")
        if isinstance(trade, dict) and TRADE_CONTROL_KEYS & set(trade):
            raise ValueError(TRADE_CONTROL_REFUSED)
        try:
            merge_preferences(UserPreferences(), patch)
        except HTTPException as error:
            raise ValueError(str(error.detail)) from None
        return self


class RemindersNavigation(BaseModel):
    """提醒面板（电脑右侧栏）/ 提醒页（手机）的页签（2026-10-25）：mine 我的提醒（到价、画线、条件提醒一张表）/ general 通用提醒（原设置 → 提醒）/ history 提醒记录（原提醒中心）。"""
    model_config = ConfigDict(extra="forbid")
    tab: Literal[surface_ids("reminderTabs")] | None = None


class NewsNavigation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 调研面板（2026-10-25 前叫新闻面板）的页签（改版第一步 2026-10-31 起页签只有三个）：sheet 今天（今日作战表，第一页；
    # 游客没有）/ desk 品种 / flash 快讯；news 相关报道（原资讯）、data 研究数据、debate 辩论是“品种”下面的子页，
    # 从品种页底部“更多”一行打开，顶上“‹ 品种”返回。
    tab: Literal["sheet", "desk", "debate", "flash", "news", "data"] | None = None
    # 快讯页签的筛选（2026-10-03）：当前品种 / 持仓与自选 / 宏观 / 全部；只给筛选不给 tab 时客户端顺带切到快讯页。
    flashFilter: Literal["product", "watch", "macro", "all"] | None = None
    # 快讯排序（2026-10-07）：time 按时间（默认，最新在上）/ impact 按影响（AI 判为强、直接讲本品种的在前）；
    # 只给排序也切到快讯页。
    flashSort: Literal["time", "impact"] | None = None
    pinnedSymbol: str | None = Field(default=None, pattern=SYMBOL_PATTERN, max_length=40)
    # 调研页（2026-10-07 第七批）：deskRejudge 等同点“按最新消息重新判断”（走 POST /api/research/desk/refresh，
    # 一小时一次与当日次数的限制不变；回执看快照 state.news.desk.rejudge 的 status / message）；deskSource
    # 展开“资料来源”并定位、高亮一条事实（事实 id，如 M.price；快照 state.news.desk.highlight）。两者都切到调研页。
    deskRejudge: Literal[True] | None = None
    deskSource: str | None = Field(default=None, pattern=r"^[A-Z]\.[A-Za-z0-9_]{1,40}$", max_length=42)


class ResearchDebateChange(BaseModel):
    """多 AI 研讨 (2026-10-11; its own page 2026-10-24): the 辩论 page — the right rail's 辩论 on a computer (half
    the window, `panel: debate`), a full-screen page over the 调研 tab on a phone. One field per command: `start` =
    press 研讨 on a 快讯 event (`start: <事件 id>`, events[].id), 让 AI 们辩一轮 on 今日判断 (`start: "desk"`) or the
    newest 重要 event of the product being read (`start: "event"`) — an event already debated today, or an
    unchanged 今日议题, opens its result instead of running again; `open` shows one debate by id; `rerun` is
    再辩一轮 on the open one (a new run); `close` closes the page. Starting runs POST /api/research/debates (four
    analysts' reports written at once, then four spoken turns one after another, then the judge — about two to three
    minutes, the day's cap applies; reports and turns stream into the page; a finished report's card folds with
    `disclosure: debate-analyst-<technical|supply|flow|news>`); 写进今日判断 is POST /api/research/debates/{id}/adopt."""
    model_config = ConfigDict(extra="forbid")
    start: str | None = Field(default=None, pattern=r"^(desk|event|[A-Za-z0-9:_.-]{1,80})$",
                              description="desk = 今日议题；event = 当前品种最新一条重要快讯；其余为快讯事件 id（events[].id）")
    open: str | None = Field(default=None, pattern=r"^db[0-9a-f]{12}$", description="打开这次研讨（GET /api/research/debates 的 id）")
    rerun: Literal[True] | None = Field(default=None, description="再辩一轮：对打开的这次研讨重新跑（新一次，计入当日次数）")
    close: Literal[True] | None = Field(default=None, description="返回：收起研讨子视图")

    @model_validator(mode="after")
    def one_field(self):
        if len(self.model_fields_set) != 1 or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("researchDebate 只能包含 start、open、rerun、close 其中一个")
        return self


class TourFocus(BaseModel):
    """What a tour step opens before it highlights: a subset of UiChanges, applied in order."""
    model_config = ConfigDict(extra="forbid")
    view: Literal["trade", "review"] | None = None
    panel: PANEL | None = None
    aiPage: Literal["monitor", "chat"] | None = None
    aiFilter: Literal["all", "chat", "trading"] | None = None
    positionsBar: Literal["expanded", "collapsed"] | None = None
    positionsTab: POSITIONS_TAB | None = None
    strategyTab: STRATEGY_TAB | None = None
    news: NewsNavigation | None = None
    ticketMode: Literal["open", "close", "target"] | None = None
    # Legacy "admin" only reaches here as a heartbeat echo: a tour an older Runtime
    # started could focus the old 用户管理 page. Commands cannot target it any more.
    settingsPage: SETTINGS_PAGE | SETTINGS_PAGE_STATE_ONLY | None = None
    settingsOpen: bool | None = None
    dialog: DialogChange | None = None
    disclosure: DisclosureChange | None = None

    @field_validator("panel")
    @classmethod
    def current_panel(cls, value):
        return legacy_panel(value)


class TourStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$", description="高亮目标 id（同 highlight.target）")
    label: str = Field(min_length=1, max_length=40, description="气泡里的一句话")
    say: str | None = Field(default=None, max_length=80, description="这一步要念/要说的话（语音回合逐步播报）")
    focus: TourFocus | None = Field(default=None, description="先把目标所在表面打开（view/panel/dialog…），再高亮")


class TourChange(BaseModel):
    """A guided tour: `start` with 1–20 steps, then `next` / `prev` / `stop`. Each step opens its surface,
    highlights its target and shows the step indicator (第 n/N 步 · 上一步 / 下一步 / 结束). A click anywhere
    only closes the bubble; only `stop` (or 结束, or `next` past the last step) ends the tour."""
    model_config = ConfigDict(extra="forbid")
    op: Literal["start", "next", "prev", "stop"]
    steps: list[TourStep] = Field(default_factory=list, max_length=TOUR_MAX_STEPS)

    @model_validator(mode="after")
    def steps_by_op(self):
        if self.op == "start" and not self.steps:
            raise ValueError("start 需要 1–20 个 steps")
        if self.op != "start" and self.steps:
            raise ValueError("只有 start 带 steps")
        return self


class TourState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool
    step: int = Field(ge=0)
    total: int = Field(ge=0)
    current: TourStep | None = None


class ChartFocusChange(BaseModel):
    """Scroll the chart to a time span (epoch seconds, bar timestamps); `null` returns to the live view."""
    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True, serialize_by_alias=True)
    from_: int = Field(alias="from", ge=0, strict=True)
    to: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def ordered(self):
        if self.to <= self.from_:
            raise ValueError("chartFocus 的 to 必须大于 from")
        return self


class ReviewUiState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str | None = None
    day: str | None = None
    filter: str = ""
    selectedTradeId: str | None = None
    # Reports only (older clients omit them; limits are sanity caps, never a reason to refuse a
    # heartbeat): the selected trade's 补充, the 账户 block's four tiles as shown
    # ({label, value, note}), the 权益曲线 summary (from/to/days/startEquity/endEquity/high/low/principal),
    # and how many 未关联的补充 the page lists.
    selectedNote: str | None = Field(default=None, max_length=2000)
    tiles: list[dict[str, Any]] | None = Field(default=None, max_length=8)
    equityCurve: dict[str, Any] | None = None
    unlinkedNotes: int | None = Field(default=None, ge=0)
    # 今日复盘 (2026-10-06), reports only: the trading day the block shows, its 笔 count, how many are
    # 计划内, and the observation's status (pending / remembered / dismissed, null without one);
    # null while the account has no review yet.
    dailyReview: dict[str, Any] | None = None
    # 习惯对照 (2026-10-25), reports only: status (ok / insufficient), 实际 and 只做计划内 counts and P&L, the gap,
    # and the picks' trade ids (select one with `review: {select}`); null until the block has read its data.
    habits: dict[str, Any] | None = None
    # 本站模拟合成一个账户 (2026-10-21), reports only: which ledger the panel shows and which it can show
    # (["CNY", "USD"] with a 外盘 USD sub-account, ["CNY"] otherwise).
    currency: Literal["CNY", "USD"] | None = None
    currencies: list[Literal["CNY", "USD"]] | None = Field(default=None, max_length=2)


# The 后台管理 console's sections. The console left the trading workspace for the
# standalone qilinshuzhi.com/trader/admin page (2026-09-30), so no command names them
# any more; 1.6.12 and earlier desktop clients still report the open section in
# their heartbeat — 桌面升级后可删. `skills` is the registry matrix (2026-09-24).
ADMIN_SECTION = Literal["overview", "users", "plans", "skills", "invites", "accounts"]


RemoteKey = Literal["up", "down", "left", "right", "ok", "back", "home", "menu", "tv", "volumeUp", "volumeDown", "voice", "power"]
RemoteAction = Literal["none", "ptt", "cancel", "chat", "settings", "scrollUp", "scrollDown", "focusPrevious", "focusNext", "activate"]


class RemoteSettingsChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool | None = None
    bindings: dict[RemoteKey, RemoteAction] | None = None
    resetBindings: bool | None = None
    diagnostic: bool | None = None
    disconnect: bool | None = None

    @model_validator(mode="after")
    def voice_only_ptt(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("遥控器操作不能为空或包含 null")
        if self.diagnostic is True and (self.enabled is False or self.disconnect is True):
            raise ValueError("开始诊断不能同时停用或断开遥控器")
        if self.disconnect is True and self.enabled is True:
            raise ValueError("断开不能同时启用遥控器")
        if self.resetBindings is True and "bindings" in self.model_fields_set:
            raise ValueError("恢复默认不能同时修改按键")
        if any(action == "ptt" and key != "voice" for key, action in (self.bindings or {}).items()):
            raise ValueError("按住说话仅用于遥控器语音键")
        return self


class UiState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str | None = Field(default=None, max_length=40)
    duration: Literal[60, 180, 300, 900, 1800, 3600, 86400, 604800]
    # 分时图 (2026-10-02): which chart the workbench shows; "timeshare" reads the same 60 s
    # serial. A client before the field reports none — the K 线 is the default.
    chartView: Literal["kline", "timeshare"] | None = None
    # view "admin" is the 后台管理 console 1.6.12 and earlier desktop clients still
    # report (the console moved to /trader/admin, 2026-09-30); 桌面升级后可删.
    view: Literal[surface_ids("views", state=None)]
    # "depth" (the 价格阶梯, removed 2026-10-07) is only reported by older desktop clients that still have it.
    panel: PANEL | None
    settingsOpen: bool
    theme: Literal["dark", "light", "system"]
    dockSide: Literal["left", "right"]
    dockWidth: int = Field(ge=280, le=1600)
    replay: bool
    # settingsPage "admin" (用户管理) is still reported by 1.6.12 and earlier desktop
    # clients (后台管理 moved to /trader/admin, 2026-09-30); commands reject it. 桌面升级后可删.
    settingsPage: SETTINGS_PAGE | SETTINGS_PAGE_STATE_ONLY
    fontScale: Literal["standard", "large", "extra-large"]
    density: Literal["compact", "standard", "comfortable"]
    chart: ChartOptions
    chartSizes: ChartSizes
    strategy: StrategyUiState | None
    # Positions/orders bar under the chart (1.4.8+); older clients do not report it.
    positionsBar: Literal["expanded", "collapsed"] | None = None
    # 底栏自动隐藏 (2026-09-30), reports only (older clients omit it): true when the collapsed
    # bar is not rendered because the account has no positions and no working orders.
    positionsBarAutoHidden: bool | None = None
    # Round eleven (AI operability contract); older clients omit all of these.
    pinnedSymbols: list[str] | None = Field(default=None, max_length=8)
    # 1.6.6 右侧栏重设计 v2: the AI dock lost its inner page; 1.6.5 and older clients still report it.
    aiPage: Literal["monitor", "chat"] | None = None
    # The 对话面板 flow filter (1.6.6–1.6.11); retired 2026-09-25 (the panel holds the conversation
    # only), so newer clients omit it.
    aiFilter: Literal["all", "chat", "trading"] | None = None
    aiActivityCard: Literal["open", "closed"] | None = None
    positionsTab: POSITIONS_TAB | None = None
    # 2026-09-25: the 成交 list's 来源 filter and the 策略面板's tab; older clients omit them.
    fillsSource: FILLS_SOURCE | None = None
    strategyTab: STRATEGY_TAB | None = None
    news: dict[str, Any] | None = None
    # 提醒面板 / 提醒页（2026-10-25）开着时回报页签 {tab: mine | general | history}。
    reminders: dict[str, Any] | None = None
    # 我的提醒's 条件提醒（第 6 批）while it shows (the price alerts are GET /api/market/alerts): {status: loading|ready|error, items: [{id, name, status:
    #  monitoring|paused|idle, symbol, runId}], error}.
    myReminders: dict[str, Any] | None = None
    # 多 AI 研讨 (2026-10-11): the 辩论 page while it shows — id, status, the running step, who is speaking, the
    # topic, the verdict's stance and confidence, whether 写进今日判断 is possible / done, the last request.
    researchDebate: dict[str, Any] | None = None
    ordersHistory: bool | None = None
    ticketMode: Literal["open", "close", "target"] | None = None
    quickOrderConfirm: bool | None = None
    remoteSettings: dict[str, Any] | None = None
    quickVolume: Literal[1, 2, 5] | None = None
    review: ReviewUiState | None = None
    # 1.6.3: the chat session the dock shows and, while 历史对话 is open, the list it offers.
    chatSessionId: str | None = Field(default=None, max_length=200)
    chatHistory: list[ChatHistoryItem] | None = Field(default=None, max_length=200)
    selectedPosition: PositionSelection | None = None
    # Stable English name of the open dialog (the `dialog` command's enum); `activeDialog` in the
    # observation keeps the visible title.
    dialog: str | None = Field(default=None, max_length=40)
    # Section open states only (rules / scope / executions / idea): agents read every value as a boolean.
    strategyDetails: dict[str, bool] | None = None
    # 说人话 (2026-09-25), reports only (older clients omit it): the one sentence 详情 shows above the five
    # sections for the strategy it shows; null when 详情 shows none.
    strategySummary: str | None = Field(default=None, max_length=2000)
    monitorCards: dict[str, Literal["expanded", "collapsed"]] | None = None
    # Round twelve: `<details>`/toggle states by disclosure name; card disclosures also as `name:id`.
    disclosures: dict[str, bool] | None = None
    # The candidate price the chart / ladder / right-click picked (the ticket's draft price); null = none.
    selectedPrice: float | None = None
    ticketPrice: float | None = None
    ticketPriceMode: Literal["limit", "opponent", "last"] | None = None
    # 清空图上标记 (2026-09-30), reports only: the current contract's 成交标记清除时间 (ISO;
    # fills at or before it are not drawn, new fills still appear). null/absent = never cleared.
    marksCleared: str | None = Field(default=None, max_length=40)
    # AI annotation layer (1.6.4+): active highlights, the current contract's AI drawings, the focused span.
    # The state is a report of what the window shows, not a request: it must never be refused by
    # the command schema. A client still holding drawings made under an earlier contract (v1 labels
    # up to 40 chars, up to 20 items) would otherwise fail every heartbeat, drop out of the window
    # list and become uncommandable (2026-09-18 production incident). Limits here are only sanity caps.
    highlights: list[dict[str, Any]] | None = Field(default=None, max_length=20)
    chartAnnotations: list[dict[str, Any]] | None = Field(default=None, max_length=50)
    chartFocus: dict[str, int] | None = None
    # Guided tour (step two): active flag, 1-based step, total, and the current step.
    tour: TourState | None = None
    # 1.6.6: the account preference document the client holds (GET /api/preferences), by category.
    preferences: dict[str, Any] | None = None
    # 4.4b: the account the attached session acts on (runtime.status.account.id); a client
    # before the switcher reports no account field.
    tradingAccount: str | None = Field(default=None, max_length=120)
    # The 后台管理 console's open section, still reported by 1.6.12 and earlier
    # desktop clients (后台管理 moved to /trader/admin, 2026-09-30); null when it is
    # closed or the window is not the owner's. 桌面升级后可删.
    adminSection: ADMIN_SECTION | None = None
    # 侧栏重设计第三期 (2026-09-25), reports only (older clients omit them): the 上线 dialog
    # {open, strategyId, symbol, symbolLocked, signalAction, autoBlocked, error} and the 运行中
    # instances [{strategyId, name, symbol, status, signalAction, effectiveSignalAction}].
    strategyLaunch: dict[str, Any] | None = None
    strategyInstances: list[dict[str, Any]] | None = Field(default=None, max_length=50)
    # 规则回测 (2026-09-25), reports only (older clients omit it): the 详情 result card
    # {strategyId, running, params: {tradingDays, slippageTicks}, symbol, timeframeSeconds,
    #  summary: {netProfit, maxDrawdown, winRate, payoffRatio, trades} | null, error, marked, tradesOpen,
    #  health: {headline, advice: [{id, text}]} | null, applying, appliedAdvice} (策略体检, 2026-09-26).
    strategyBacktest: dict[str, Any] | None = None
    # 策略模板库 (2026-09-25), reports only (older clients omit it): {open, stage: gallery|params|null,
    #  templateId, templateName, params, timeframe, invalid: {key: 原因}, running, summary, error, savedStrategyId,
    #  detail: {strategyId, templateId, templateName, params, defaults} | null (详情里来自模板的策略)}.
    strategyTemplate: dict[str, Any] | None = None
    # AI 引导设计卡, retired 2026-10-08 (the 策略编辑卡 replaced it): 1.7.2 and older clients still report
    # it, so the field stays accepted; nothing reads it.
    strategyGuide: dict[str, Any] | None = None
    # 策略编辑卡 (2026-10-08), reports only (older clients omit it): {open, mode: new|edit, strategyId (the one
    #  being edited), start: blank|habits|template:<id>, name, fields: {symbol, timeframe, direction, entryLong /
    #  entryShort: {join, conditions: [label], hits: [n], together}, stop: {mode, value}, take: {mode, value},
    #  trailing, closeBeforeEnd, lots, maxPosition, maxOrdersPerDay, entryWindows, allowNightEntries, pyramiding,
    #  more: {...}}, aiFields: [field], changed: [field], prefill: {eligible, reason, count, summary} | null,
    #  advanced: [label] (read-only rules kept as they are), missing: [what still blocks backtest / save],
    #  backtest: {status, trades, winRate, netProfit, maxDrawdown, previous} | null, canSave, ask: {text, status} | null,
    #  questions: [{id, question, options}], saving, savedStrategyId, error}.
    strategyEditor: dict[str, Any] | None = None
    # 使用说明卡 (2026-09-25), reports only (older clients omit it): {open, topic, group, query, matches:
    #  [topic ids], detailOpen, faqOpen: [indices], developerOpen, loadStatus: loading|ready|error|offline, loadError, canRetry}.
    help: dict[str, Any] | None = None
    # 试几组参数比一比 (2026-10-08; was 策略实验室), reports only (older clients omit it): {labId, strategyId,
    #  status: none|running|done|error, where: server, paramLabel, best, line, rows: [{variantId, label, current,
    #  netProfit, maxDrawdown, winRate, flags}]}.
    strategyLab: dict[str, Any] | None = None
    # 我的策略的输入框, retired the same day it shipped (2026-10-08, the 策略编辑卡 replaced it): kept accepted for
    # clients that still report it; nothing reads it.
    strategyIdea: dict[str, Any] | None = None
    # 手机版 策略 (2026-10-08), while mobileTab is strategy: its tab (running 正在运行 / mine 我的策略).
    mobileStrategyTab: MOBILE_STRATEGY_TAB | None = None
    # 报价总表 (2026-10-01), reports only (older clients omit them): the exchange filter
    # ("all", "watchlist" or an exchange code) and the column sort {key, order} the board view shows.
    boardExchange: str | None = Field(default=None, max_length=10)
    boardSort: dict[str, Any] | None = None
    # 自选品种 (2026-10-02): how many products the account's watchlist holds (the list itself is
    # in `preferences.watchlist.products`); reported with the board fields, while the view is on.
    boardWatchlistCount: int | None = Field(default=None, ge=0, le=50, strict=True)
    # 合约抽屉两级列表 (2026-10-01), reports only: the product whose months are expanded
    # ("<exchangeId>.<productId>"); null when every product is collapsed.
    contractProduct: str | None = Field(default=None, max_length=40)
    # 合约抽屉的目录状态 (2026-10-11), reports only while the drawer is open (older clients omit it):
    # {status: loading|error|ready|offline|empty|expired, canRetry} — canRetry = the drawer shows 重试
    # (contractCatalogRetry); offline = the app service is not connected (a guest is never offline).
    contractCatalog: dict[str, Any] | None = None
    # 用户画线 (2026-10-02), reports only (older clients omit it): {symbol, count, tool} — the
    # current contract's saved drawings and the active tool. 画线第二版 (2026-10-09) adds (same field,
    # no new top-level key): toolbar (expanded | collapsed), group (the open tool list or null), selected
    # (drawing id or null), settings (the drawing whose 精确设置 is open), magnet / locked / hidden, the
    # open right-click menu (contextMenu: main | drawing:<id> | pane:<type>, with "/<submenu>"), and items
    # (≤50: id, kind, label, locked, hidden) so a command can name a drawing.
    chartDrawings: dict[str, Any] | None = None
    # 手机版 (2026-10-04), reported only while the window shows the phone layout (a narrow touch
    # screen): `mobileLayout` true and the bottom tab shown; a desktop-layout window omits both.
    mobileLayout: bool | None = None
    mobileTab: MOBILE_TAB | None = None
    # 手机版第二期: the full-screen page shown over the tabs (null = none), the AI 首页 timeline's filter
    # (原通知页) and how many items the user has not read (read marks live in the browser only).
    mobilePage: MOBILE_PAGE | None = None
    mobileNotifyFilter: MOBILE_NOTIFY_FILTER | None = None
    mobileUnread: int | None = Field(default=None, ge=0)
    # AI 简报订阅 (2026-10-05): the product the timeline's AI 简报 rows are narrowed to (absent = all).
    mobileNotifyProduct: str | None = Field(default=None, max_length=40)
    # 回测结果页 (2026-10-06), while mobilePage is lab: its comparison and the chosen variant.
    mobileLab: MobileLabState | None = None
    # 安装到桌面 (2026-10-06): which browser the install guide is teaching, while mobilePage is install.
    installEnvironment: INSTALL_ENVIRONMENT | None = None
    # 锁屏提醒 (2026-10-06), while mobilePage is push: whether this browser can take lock-screen push and
    # whether this device has it on (its subscription is on the Runtime).
    pushSupport: PUSH_SUPPORT | None = None
    # 设置 → 账户 → 实盘 (2026-10-08), while settingsPage account shows: {state: off, disabledReason}.
    liveTrading: LiveTradingState | None = None
    # 提醒中心 (2026-10-15), reported while the window has one: {open, filter, unread, today, earlier}.
    reminderCenter: ReminderCenterState | None = None
    # 设置 → AI 与记忆: memory and 我的流程, while that page shows (AI 审计 2026-10-13).
    memory: MemoryPanelState | None = None
    routines: RoutinesPanelState | None = None
    pushSubscribed: bool | None = None


class BoardSortChange(BaseModel):
    """报价总表: which column the board sorts by and in which direction."""
    model_config = ConfigDict(extra="forbid")
    key: Literal["changePercent", "volume", "openInterest", "openInterestChange"]
    order: Literal["desc", "asc"]


class UiChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "minProperties": 1, "maxProperties": 1,
        "description": "remoteSettings 操作本机遥控器：enabled 布尔、bindings 按键到动作对象、resetBindings 布尔、diagnostic 布尔、disconnect 布尔；按住说话仅语音键，连接必须用户点击选择硬件；诊断只在测试与诊断页开始，离开自动停止，不保存或上传录音。quickVolume 设置本机快捷手数 1 / 2 / 5（快照 state.quickVolume），不下单；快捷买多、卖空、平仓、平今、平一半、撤本合约挂单、反手仅人工操作，AI 使用候选卡。每条命令仅含一个字段。多项外观或布局放在 presentation 中一次提交；settingsPage 自动打开设置（account 账户 / ai AI 与对话 / memory 记忆（画像与“我的流程”：列表 GET /api/routines，删的二次确认是 disclosure: routine-delete，id=流程 id）/ voice 语音 / chart 图表 / trade 交易 / notify 提醒（每类提醒一个开关，管对话、桌面通知、语音与锁屏推送，开关是 preference 命令；急停总是提醒）/ ui 界面 / shortcuts 快捷键 / data 数据与隐私 / about 关于，二级页 trade-advanced 高级交易设置 / voice-advanced 判停与确认时限 / remote-keys 遥控器按键设置 / remote-diagnostics 遥控器测试与诊断 / chart-advanced 指标参数与均线 / memory-advanced 约定、经验与最近变动 / feedback 问题与建议（关于下的二级页，游客也可打开；填写、复制和打开邮件草稿由用户操作，草稿不代表发送）；旧值 connection→account、appearance→ui、simulation→trade、fees→trade-advanced 仍接受）；后台管理是独立网页 qilinshuzhi.com/trader/admin，不在交易工作区窗口里，没有对应界面命令；"
                       "dialog 打开/关闭对话框与抽屉（quick-order-confirm、abandon-order、protection-reprice-confirm（图上拖线改价的确认卡）、proposal-confirm（只在 1.8.0 桌面客户端，网页无此框）只能关闭；"
                       "update-dialog 是新版本更新窗，仅开关窗口，下载安装仍是用户受保护操作；change-password 是设置 → 账户的修改密码框（先 settingsPage: account），只开关，密码由用户本人输入和提交，快照不含输入内容）；"
                       "disclosure 展开/收起折叠段与开关（设置 → 账户：档位的每日额度与今日用量是 plan-quota；实盘开关是 live-trading——暂无期货公司接入，open: true 被拒并说明原因，快照 state.liveTrading＝{state: off, disabledReason}）；reviewSelect、selectPosition 传 null 表示取消选择；"
                       "reviewDay 打开复盘（电脑是复盘页、手机是复盘摘要页）并把“今日复盘”块切到那个交易日（只能是 GET /api/account/daily-review 的 days 里的一天，否则 rejected；快照 state.review.dailyReview）；“今日复盘”块的“怎么算的”折叠是 disclosure: review-how；“习惯对照”块（实际 vs 只做计划内，GET /api/account/habits）的“怎么算的”折叠是 disclosure: habits-how，快照 state.review.habits（status、actual、planned、gap、picks 的成交 id，选中一笔用 review select）；重新生成、记住“本周最该改的一件事”是业务接口 POST /api/account/daily-review/regenerate 与 /observation；reviewCurrency CNY / USD 把账户复盘切到人民币账或外盘美元账（本站模拟合成一个账户：外盘合约在美元子账户撮合记账，顶栏权益、成交与账户明细都两边一起显示；只有有美元子账户时能切 USD，快照 state.review.currency / currencies）；"
                       "highlight 高亮一个界面目标并配说明气泡，chartAnnotations 在主图画 AI 标注（set/add/clear），"
                       "chartFocus 把图表滚到某时间段（null 回到最新）；selectedPrice 设置候选价并打开下单台、填入手动价格且切到限价（只填价，不改方向、不下单；快照 ticketPrice / ticketPriceMode），null 只清除候选价标记（图上左键选价已移除，五档和此命令共用入口）；"
                       "panel 打开右侧栏面板（ai 对话 / strategy 策略 / news 调研（2026-10-25 前叫新闻；页签 sheet 今天（今日作战表，第一页：自选品种按优先级一行一个，关键价位、消息、可能用上的交易笔记，快照 state.news.sheet 回报 status / rows / high / mid / low / top；GET /api/research/sheet）/ desk 品种 / flash 快讯；news 相关报道（原资讯）、debate 辩论是“品种”下的子页（2026-10-31 改版第一步）；data 研究数据不再列在品种页“更多”里（基本面已含其内容），仍可直接用 tab 打开）/ debate 辩论：打开调研面板的辩论页签（原右侧栏单独的辩论页），按 panel news、news.tab debate 回报 / reminders 提醒（2026-10-25 新增；页签用 reminders {tab: mine 我的提醒 | general 通用提醒（原设置 → 提醒）| history 提醒记录（原提醒中心）}）/ order 下单：五档 + 下单票 + 本合约持仓 / positions 持仓，null 收起）；旧值 depth（原价格阶梯，已并入下单面板）仍接受，按 order 打开并按 order 回报；"
                       "chartMarks:\"clear\" 清空当前合约图上标记（AI 标注、回测买卖点一并清除，并把成交标记的清除时间记为现在，此前的成交标记不再显示、新成交仍显示）；"
                       "画线（2026-10-09 第二版，K 线与分时）：disclosure chart-drawing-toolbar 显示/收起画线工具栏（平时隐藏，浮在图表左侧；选了工具会自己出来，回十字光标就收起；手机单图上是 ✎ 底部面板；旧名 chart-drawing-tools 同义），"
                       "chart-drawing-group {id: lines|levels|fib|shapes|notes|measure} 显示工具栏并展开一组工具，chart-drawing-tool {id: 工具名如 trendline/hline/fib} open 开始画（在图上点）、false 回十字光标，"
                       "chart-drawing-magnet / chart-drawing-lock-all / chart-drawings-hidden 是磁吸、全部锁定、全部隐藏三个开关，chart-drawing-clear 打开“全部删除”的二次确认（这些也在主图右键“画线 ▸”里），"
                       "chart-drawing-style {id: 线 id} 选中一条线并出浮动样式条（手机单图上是图下的条，mobile-drawing-style-row {id: color|width|dash} 展开其中一排颜色 / 粗细 / 线型，要先选中线），chart-drawing-settings {id: 线 id} 打开精确设置，"
                       "chart-context-menu {id: main | drawing:<线 id> | pane:<kd|macd|rsi|oi>，可加 /draw、/period、/indicator、/add-pane、/style、/swap 打开子菜单} 打开三种右键菜单（只服务图表，不含下单）；"
                       "画、改、删本身走 /api/market/drawings（快照 state.chartDrawings：工具栏、选中、各开关、打开的菜单与本合约的线 items）；"
                       "presentation.reset 恢复默认布局；"
                       "tour 启动/推进/结束分步引导（start 带 steps，每步先 focus 再 highlight）；"
                       "preference 从设置页改一项账号偏好（key 点分路径 + value；agent 直接用 update_preferences 更省事；"
                       "trade.aiAutonomy 与 trade.voiceOrderConfirm 不经界面命令改，只用 update_preferences）；"
                       "tradingAccount 把当前会话换到目录里的另一个账户（PUT /api/sessions/{id}/account，观察见 runtime.status.account 与 UiState.tradingAccount）；"
                       "strategyTab 打开策略面板并切到 library 我的策略（策略面板只有我的策略 / 正在运行两页；旧值 playbooks＝原打法页，2026-10-31 起打法改名交易笔记、搬到复盘页，按 playbooks 打开复盘页并按 view: review 回报，新建 / 改的表单是 disclosure: playbook-editor，增删改走 /api/playbooks）/ library 我的策略（“新建策略”按钮 + 每个策略一张卡）/ running 正在运行 / detail 选中策略的结果页（从卡片打开；旧值 lab＝原实验室，打开结果页的“试几组参数比一比”，回报 detail）；positionsTab 的 fills 是持仓面板与底栏的「成交」页（旧值 trades 仍接受），fillsSource 按来源筛选成交（all 全部 / manual 手动 / card AI 建议（对话里 AI 出卡、你确认的）/ ai AI 自主 / strategy 策略 / stop 止损）；"
                       "aiFilter 已退役（对话面板只放对话），仍接受但只打开对话面板；"
                       "strategyLaunch 填上线对话框的合约与执行方式（先 dialog: strategy-launch 打开；验证并上线由用户点）；"
                       "strategyInstance 已退役：运行中实例的执行方式（alert / confirm / auto）只用 start_strategy_monitoring 改，快照 state.strategyInstances 照常；"
                       "strategy-actions / strategy-delete-confirm 开关已有策略的更多操作 / 删除确认，id=library:<策略id> 或 replay:<策略id>，不执行删除；strategyEditor 操作策略编辑卡（新建或修改策略都用它，电脑放在策略面板里，手机布局是盖住底部标签的整屏页（disclosure strategy-editor-section {id: basics|entry|exit|sizing|flow} 切四段与策略流程、strategy-editor-condition {id: 组:第几条，如 entryLong:1} 开合某条条件的底部面板，strategy-editor-trades 展开预填依据）；电脑上打开时策略面板约占窗口四分之三，右栏是策略流程图与回测；disclosure strategy-editor-picker {id: 组:第几条:left|right，如 entryLong:1:left} 打开某条条件一侧的指标选择器（全部指标分类可搜索，手机打开该条的底部面板并停在那一侧），strategy-editor-sizing-more 展开④的更多，help-tip {id: 说明的 key} 打开一个“?”说明；只设计不下单）：new:true 新建（起点默认按我的习惯：近 20 个交易日已平仓成交至少 5 笔时程序按成交预填合约、方向、止损跳数、收盘前平仓、开仓时段、每次手数，带 AI 标记与依据，不够就空白；合约默认当前图表合约），edit:\"<strategyId>\" 修改已有策略（卡片表达不了的规则原样保留，存为新版本、原版本保留），open:true 打开（有没存的草稿接着改），start 换起点（blank / habits / template:<模板id>），ask:\"一句话\" 补一句（表单空时生成整份计划，已有内容时只改相关字段，改到的字段高亮、不直接保存），answers {问题 id: 回答} 回答 AI 追问，focus 把表单定位到流程图的某一块（同点方块：basics/entry/exit/sizing 或 trigger/entryLong/entryShort/action/stop/take/trailing/breakeven/scaleOut/timeTakeProfit/maxHold/closeBeforeEnd/exitRules/limits），choose 点选卡上的几项（提醒模式 repeat once 只响一次 / rearm 条件再满足再响 / bar 每根 K 线最多一次、until today / week / forever 到期、watchlist 整张自选表；策略 signalAction＝信号来了怎么办，上线对话框默认选中的执行方式，auto 要 AI 自主执行开着；快照 fields.reminder / fields.signalAction），save:true 存为草稿（开仓条件和止损填好才能存，存好电脑打开结果页、手机回到我的策略，都自动回测），close:true 关闭并放弃没存的改动；上线运行仍由用户在卡里点（打开上线对话框）。改动后约 1.5 秒自动用已收盘 K 线回测最近 60 个交易日（默认在本机浏览器算，算不了自动转云端；快照 state.strategyEditor：各字段、aiFields、prefill、backtest 四格、flow 流程图结构（触发、开多 / 开空条件组与每条满足次数、开仓、各平仓方式与回测里实际触发次数、冷却与每天次数；次数只取当前表单的预览回测，拿不到不写）、focus、canSave、missing、questions；backtest.source browser/server 与 fallbackReason、engine 本机引擎状态只供排障）；"
                       "strategyBacktest 操作选中策略结果页的回测（界面只有这一种回测：规则回测，默认最近 6 个月、扣手续费；默认在本机浏览器算，算不了自动转云端，存为策略后的那次由服务端重跑）：tradingDays / slippageTicks 改参数，run:true 按当前参数回测，"
                       "mark 把开平仓点画到图上或清除，trades 展开逐笔清单，applyAdvice:\"<建议 id>\" 按回测体检的这条建议改好、存为新版本并重跑回测（快照 state.strategyBacktest，verdict 是“AI 怎么看”：程序算的事实句与模型写的那句；source browser/server 是这次在哪算的、fallbackReason 是为何转云端、browserCheck 是服务端按同一份输入复核本机结果是否一致、engine 是本机引擎状态，只供排障，界面不显示）；"
                       "strategyTemplate {restore:true} 让结果页里来自模板的策略恢复模板默认（快照 state.strategyTemplate）；模板画廊已并入策略编辑卡的“从模板”（strategyEditor start template:<id>）。"
                       "strategyLab 操作结果页的“试几组参数比一比”（原实验室页签已去掉）：open 打开选中策略的结果页并停在比较，run:true 对它最关键的一个参数跑当前值和上下各一组（默认本机算、算不了转云端，前 70% 挑、后 30% 单独检验），"
                       "labId 打开那次比较所属策略的结果页，choose:\"<变体 id>\" 把这组存为我的策略、服务端先重跑权威回测；useServer 旧字段照收（快照 state.strategyLab：每组赚 / 最多亏 / 胜率、当前组、line＝“后 X 个月单独检验：…”、where＝browser/server、fallbackReason）。"
                       "停止并平仓：运行中卡的“停止并平仓”＝ POST /api/strategies/{id}/monitor/stop {closePositions:true}——停止后这个策略还有持仓就在对话里出平仓卡等用户确认，从不直接下单（自动下单的实例也一样）。"
                       "help 操作使用说明卡（没开时先打开）：topic 打开主题（旧 id 仍可用），group 打开分组的第一个主题，query 搜索，"
                       "detail 展开 / 收起“完整说明”，faq:{index,open} 展开 / 收起一个常见问题，retry:true 在说明读取失败后重新读取（快照 state.help，含 loadStatus loading/ready/error/offline、loadError、canRetry）；dialog: help 仍可开关卡片。"
                       "view 切到 board 打开报价总表（全部品种主力合约一张表）；boardExchange 换总表的交易所筛选（all 或 watchlist 自选＝只看星标品种，FOREIGN 外盘＝海外主连报价，点一行打开它的图表（同合约选择的海外主连），快照 state.boardExchange、state.boardWatchlistCount 是自选数），"
                       "boardSort 换总表排序 {key, order}（快照 state.boardSort）——筛选与排序由账号偏好 marketBoard 记住，自选品种清单是偏好 watchlist.products（“交易所.品种”如 SHFE.rb，增删先读再整段写，经 preference 或 update_preferences），"
                       "列显示哪些用 disclosure: board-columns 打开“列”浮层（快照 disclosures.board-columns），增删调序经 preference: marketBoard.columns 或 update_preferences；"
                       "contractProduct 展开合约抽屉里某个品种的月份列表（“<交易所>.<品种>”如 SHFE.rb，海外主连用外盘品种键如 COMEX.GC，需抽屉已打开，null 收起全部，快照 state.contractProduct）；contractCatalogRetry: true 等价抽屉里的“重试”，目录读取失败或读到空目录后重新读取（需抽屉已打开、快照 state.contractCatalog.canRetry 为 true；正在读取、已读到、应用服务未连接或游客会话失效时 rejected（命令回执只有状态，原因读快照 state.contractCatalog.status））。"
                       "duration 切 K 线周期（60/180/300/900/1800/3600/86400/604800 秒 = 1/3/5/15/30 分、1 小时、日线、周线），任何周期都会回到 K 线视图；"
                       "chartView 切图表形态：timeshare 打开分时图（当交易日分钟收盘折线＋当日均价＋昨结虚线，数据周期钉在 60 秒，无 MA/副图指标）、kline 回 K 线（快照 state.chartView）。"
                       "mobileTab 切手机版底部标签（只对手机布局的窗口有效，电脑布局 rejected）：ai AI 首页（今天概况、待确认置顶、带筛选的时间线）、chat AI 对话（与 panel: ai 同一状态）、research 调研、market 看盘、strategy 策略（正在运行 / 我的策略两个分页 + 底部“跟 AI 说……”输入框；分页用 mobileStrategyTab；我的策略顶上“新建策略”、每条“编辑 / 回测 / 上线…”，新建与编辑用 strategyEditor）、me 我的；旧值 notify（原通知标签，已并进 AI 首页）仍接受，打开 AI 首页并保持当前筛选，快照回报 mobileTab ai（快照 state.mobileLayout、state.mobileTab；手机布局下打开侧边面板的命令 rejected；没登录的游客窗口也是手机布局，默认 market，只能看盘和调研，ai / strategy / me 显示“登录后使用”页，chat 不可用）。"
                       "mobilePage 打开手机版的全屏子页（只对手机布局有效）：order 下单页（当前合约，确认后才下单）、positions 持仓与委托、review 复盘摘要、briefs AI 简报推送（推送开关与订阅品种，增删调序走 PUT /api/app/brief-subscriptions）、install 安装到桌面引导（按浏览器给步骤、安卓一键安装按钮或微信等 App 内的换浏览器遮罩；快照 state.installEnvironment 是识别出的环境 installed / in-app-ios / in-app-android / ios-safari-26 / ios-safari-legacy / ios-chrome-edge / ios-other / android-prompt / android-vendor / desktop；安装本身由用户在浏览器里完成）、reminders 提醒（提醒中心的手机页，同 reminderCenter: {open: true}）、push 锁屏提醒（这台设备的开关与“发一条试试”、已开锁屏提醒的设备；提醒哪些事与锁屏上显示金额和手数在 settingsPage notify；快照 state.pushSupport＝supported / needs-install（iPhone 要先放到主屏幕）/ unsupported（安卓与 Chrome 走谷歌推送，国内收不到），state.pushSubscribed＝这台设备是否已开；打开必须用户在页面上点，AI 只能用 GET /api/app/push 查、POST /api/app/push/test 发一条试试）、lab 回测结果页（策略 → 最近的回测里最新一次；几组参数的净值、样本内 / 样本外与过拟合风险，选用一组走 POST /api/strategy-labs/{labId}/choose，“上线运行”与电脑同一校验、执行方式固定出卡确认；要看指定的一次用 mobileLab）、accounts 设置·账户（交易账户切换）、memory 设置·记忆（AI 与记忆，含我的流程）、appearance 设置·界面（外观与字号）、help 使用说明卡，null 关闭（快照 state.mobilePage）；"
                       "手机布局下 positionsTab / fillsSource / ordersHistory 落到持仓与委托页、ticketMode / selectPosition 落到下单页、reviewExpand / reviewFilter / reviewSelect / reviewCurrency 落到复盘摘要页；看盘的合约选择是 dialog: contract-drawer，周期与合约用 duration / chartView / symbol。"
                       "mobileNotifyFilter 切 AI 首页时间线的筛选（原通知页）并打开 AI 标签、滚到时间线：all 全部、fill 成交、brief AI 简报；旧值 reminder 打开提醒页（快照 state.mobileNotifyFilter；state.mobileUnread 是提醒中心的未读条数，与 state.reminderCenter.unread 相同）。"
                       "reminders {tab} 打开提醒面板（电脑右侧栏“提醒”）/ 提醒页（手机）并切到该页签：mine 我的提醒（一张表：到某个价、碰到我画的线（到价提醒，增删走 /api/market/alerts，画线的带 drawingId）和按条件（条件提醒：只提醒的策略，快照 state.myReminders {status, items: [{id, name, status monitoring/paused/idle（任一合约在盯即 monitoring）, symbol（在跑的各合约）, runs [{runId, status}]}]}，暂停 / 删除作用于每个实例，改用 strategyEditor {edit: id}，开停删走策略接口 monitor/start {signalAction: alert}、monitor/pause、DELETE /api/strategy-plans/{id}）；“新建提醒”= disclosure: reminder-alert-new（到某个价的表单，当前合约），旁边“按条件…”= strategyEditor {new: true, reminder: true}）、general 通用提醒（每类提醒的开关、桌面通知、锁屏推送；原设置 → 提醒，旧命令 settingsPage: notify 也到这里）、history 提醒记录；快照 state.reminders {tab}（面板或页面开着时）。reminderCenter {open?, filter?, more?} 打开/关闭提醒记录——电脑是提醒面板的“提醒记录”页签（K 线工具条铃铛直达），手机是提醒页的同一页签（同一份数据）：filter unread 只看未读、all 当天全部（只给 filter 也会打开）；more: true＝“继续加载当天提醒”，当天一次最多读 500 条，快照 state.reminderCenter.moreToday 为 true 时读下一批（同时打开提醒中心）：下一批读到才回 applied（moreDone 追上 moreAsked 且 moreError 为空），没有更多、刚刷新过要再点或读取失败回 rejected，原因在 state.reminderCenter.moreError；内容是当前交易日的提醒按日盘上午 / 下午 / 夜盘分组倒序，“查看更早”与已读走业务接口 GET /api/reminders?scope=earlier、POST /api/reminders/read（快照 state.reminderCenter {open, filter, unread, today, moreToday, moreAsked, moreDone, moreError, earlier}，today 是已读出的当天条数）；点一条提醒去它的卡（对话）、委托、复盘、调研或新闻；保本提醒的“移到成本 / 不用”是 POST /api/ai-execution/breakeven/{id}/{move|decline}。"
                       "mobileNotifyProduct 在 AI 首页时间线的 AI 简报下只看一个品种（“<交易所>.<品种>”如 SHFE.rb，须是订阅品种（推送范围内）或时间线里出现的品种，否则 rejected；null 看全部品种；同时切到 AI 简报筛选与 AI 标签，快照 state.mobileNotifyProduct）。"
                       "mobileLab {labId} 打开手机版回测结果页看指定的一次比较（labId 来自 GET /api/strategy-labs；快照 state.mobilePage lab、state.mobileLab {labId, chosen}——chosen 是已选用的变体，未选为 null）。"
                       "mobileStrategyTab 切手机版策略页的分页（running 正在运行：暂停 / 继续 / 停止并平仓；mine 我的策略：顶上“新建策略”，每个策略一张卡带“编辑 / 回测 / 上线…”，一条没有时是三种开始方式与常用模板），先切到策略标签（快照 state.mobileStrategyTab）。"
                       "mobileMarket 改手机版看盘（只对手机布局有效，先切到看盘标签；一次一项）：view quotes 报价列表 / multi 多图 / single 单图；board 报价板块 watchlist 自选 / main 主力 / SHFE 上期所 / DCE 大商所 / CZCE 郑商所 / CFFEX 中金所 / GFEX 广期所 / INE 能源中心 / FOREIGN 外盘（点一行打开它的单图；切到报价）；columns 报价的数值列 1–4 个（last 最新 / changePercent 涨跌幅 / change 涨跌 / openInterest 持仓量 / bid 买价 / ask 卖价 / volume 成交量 / openInterestChange 日增仓）；layout 多图布局 four 4 合约 / periods 同合约 4 周期 / six 6 图（切到多图）；cell {index, symbol?, duration?} 改多图当前布局的一格（symbol null＝自动补位；同合约 4 周期里 symbol 切当前合约）；subPane 单图下面显示哪个副图（kd / macd / rsi / oi 持仓量 / cci / volume 成交量，未选的替换正在显示的那个）；subPanes true/false 开关副图设置面板（选 0–3 个副图、主图指标 均线 / 布林 / SAR、各指标参数，快照 disclosures.mobile-subpanes）。都记在账号偏好 ui.mobileMarket 与 chart.subPaneList，快照 state.preferences。"
                       "researchDebate 操作“多 AI 辩论”页（电脑是调研面板“品种”下的辩论子页，同 panel: debate；手机是调研页上的整屏子页；一次一项）：start 发起或打开辩论——start: \"desk\" 等于今日判断旁的“让 AI 们辩一轮”（今日议题），start: \"event\" 辩当前品种最新一条重要快讯，start: <事件 id>（快讯接口 events[].id）等于快讯行的“研讨”；同一事件同一品种当天只辩一次、今日议题资料没变时直接打开已有结果；open: <研讨 id> 打开一次辩论（也是“以前的辩论”里的一格）；rerun: true 是结果页的“再辩一轮”（新跑一次，计入当日次数）；close: true 收起辩论页。辩论本身走 POST /api/research/debates（技术面、供需基本面、资金持仓、新闻宏观四位分析师先同时写报告，再多方开场、空方回应、多方反驳、空方反驳，一句一句流式出现，最后裁判匿名裁决，约 2 到 3 分钟；写完的报告卡片用 disclosure: debate-analyst-technical / supply / flow / news 展开收起），写进今日判断走 POST /api/research/debates/{id}/adopt，停止走 POST /api/research/debates/{id}/cancel（快照 state.researchDebate：id、status queued/running/done/review 需复核/failed/cancelled、step（collect / analysts / turn1…turn4 / judge）、analysts（四份报告写完几份，没写成也算）、lesson（上次复盘）、speaking（正在说的一方）、topic、stance、confidence、canAdopt、adopted、request）。游客不能发起。",
    })
    symbol: str | None = Field(default=None, pattern=SYMBOL_PATTERN, max_length=40)
    duration: Literal[60, 180, 300, 900, 1800, 3600, 86400, 604800] | None = None
    # 分时图 (2026-10-02): "timeshare" opens the 分时图 (数据周期钉在 60 秒), "kline" returns to the
    # K 线; a plain `duration` pick also returns to the K 线 view.
    chartView: Literal["kline", "timeshare"] | None = None
    view: Literal[surface_ids("views")] | None = None
    panel: PANEL | None = None
    settingsOpen: bool | None = None
    theme: Literal["dark", "light", "system"] | None = None
    dockSide: Literal["left", "right"] | None = None
    dockWidth: int | None = Field(default=None, ge=280, le=1600, strict=True)
    # The positions/orders bar under the chart; `panel: positions` is the older way to expand it.
    positionsBar: Literal["expanded", "collapsed"] | None = None

    settingsPage: SETTINGS_PAGE | None = None
    fontScale: Literal["standard", "large", "extra-large"] | None = None
    density: Literal["compact", "standard", "comfortable"] | None = None
    chartOption: ChartOptionChange | None = None
    chartSizes: ChartSizes | None = None
    strategy: StrategyUiChange | None = None
    presentation: PresentationChanges | None = None
    # Round eleven: every user-operable surface is commandable (AI operability contract).
    pinnedSymbols: PinnedSymbolsChange | None = None
    # 1.6.6: kept for older agents — both values open the 对话面板.
    aiPage: Literal["monitor", "chat"] | None = None
    # Retired 2026-09-25 (the 对话面板 has no filter any more): still accepted, opens the 对话面板.
    aiFilter: Literal["all", "chat", "trading"] | None = None
    aiActivityCard: Literal["open", "closed"] | None = None
    # fills = 成交 (trades is its old name); fillsSource filters the 成交 list by 来源.
    positionsTab: POSITIONS_TAB | None = None
    fillsSource: FILLS_SOURCE | None = None
    # 策略面板 tab: library 我的策略 / running 正在运行 / detail 选中策略的结果页 (lab: the old 实验室, = detail).
    strategyTab: STRATEGY_TAB | None = None
    news: NewsNavigation | None = None
    reminders: RemindersNavigation | None = None
    ordersHistory: bool | None = None
    ticketMode: Literal["open", "close", "target"] | None = None
    quickOrderConfirm: bool | None = None
    remoteSettings: RemoteSettingsChange | None = None
    quickVolume: Literal[1, 2, 5] | None = None
    chatSession: ChatSessionChange | None = None
    reviewExpand: ReviewExpandChange | None = None
    reviewFilter: str | None = Field(default=None, pattern=r"^([A-Za-z]+\.[A-Za-z0-9]+)?$", max_length=40)
    reviewSelect: str | None = Field(default=None, max_length=200)
    # 今日复盘 (2026-10-06): open 复盘 (the phone's 复盘摘要) on that trading day's review.
    reviewDay: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    # 本站模拟合成一个账户 (2026-10-21): 账户复盘 shows the RMB or the 外盘 USD ledger.
    reviewCurrency: Literal["CNY", "USD"] | None = None
    dialog: DialogChange | None = None
    strategyDetails: StrategyDetailsChange | None = None
    monitorCard: MonitorCardChange | None = None
    # 侧栏重设计第三期 (2026-09-25): the 上线 dialog's fields and a running card's 执行方式.
    strategyLaunch: StrategyLaunchChange | None = None
    strategyInstance: StrategyInstanceChange | None = None
    strategyBacktest: StrategyBacktestChange | None = None
    strategyTemplate: StrategyTemplateChange | None = None
    # Retired 2026-10-08 (the 策略编辑卡 replaced the guide card and the ask box): still in the schema for
    # older agents, refused with the strategyEditor command that does the job now.
    strategyGuide: dict[str, Any] | None = None
    # 试几组参数比一比 (2026-10-08; the retired 实验室 tab's command): the 结果页's comparison.
    strategyLab: StrategyLabChange | None = None
    strategyIdea: dict[str, Any] | None = None
    # 策略编辑卡 (2026-10-08): new / edit / 起点 / 补一句 / 存为草稿 / close.
    strategyEditor: StrategyEditorChange | None = None
    # 使用说明卡: topic / group / search / 完整说明 / 常见问题 (the card opens first).
    help: HelpChange | None = None
    # 报价总表 (2026-10-01): the exchange filter and the column sort of the board view;
    # "watchlist" (2026-10-02) shows only the account's starred products.
    boardExchange: Literal["all", "watchlist", "SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX", "FOREIGN"] | None = None
    boardSort: BoardSortChange | None = None
    # 合约抽屉两级列表 (2026-10-01): which product's months are expanded; null collapses all.
    contractProduct: str | None = Field(default=None, pattern=PRODUCT_KEY_PATTERN, max_length=40)
    # 合约抽屉的“重试” (2026-10-11): re-read the catalog after a failed or empty read; needs the drawer
    # open and state.contractCatalog.canRetry (rejected while loading, already read or not connected).
    contractCatalogRetry: Literal[True] | None = None
    # 多 AI 研讨 (2026-10-11): start / open / 再辩一轮 / close on the 辩论 page (desktop rail page, phone 调研 sub-page).
    researchDebate: ResearchDebateChange | None = None
    selectPosition: PositionSelection | None = None
    disclosure: DisclosureChange | None = None
    # Candidate price (what the order panel's 五档 sets — chart-click pricing is gone, and since
    # 画线第二版 the chart's right-click menu no longer fills a draft price);
    # null = 撤销选价.
    selectedPrice: float | None = Field(default=None, gt=0)
    # 清空图上标记 (2026-09-30): the chart right-click's 清空图上标记 — clears the AI
    # annotations and backtest marks and retires the current contract's trade markers.
    chartMarks: Literal["clear"] | None = None
    # AI annotation layer: point at a surface, draw on the chart, scroll the chart.
    highlight: HighlightChange | None = None
    chartAnnotations: ChartAnnotationsChange | None = None
    chartFocus: ChartFocusChange | None = None
    tour: TourChange | None = None
    preference: PreferenceChange | None = None
    # 4.4b: switch the session to another account from the directory; the state follows via
    # the runtime.status the switch publishes, so the applied receipt lands on a later pulse.
    tradingAccount: str | None = Field(default=None, min_length=1, max_length=120)
    # 手机版 (2026-10-04): the phone layout's bottom tab; a desktop-layout window rejects it.
    mobileTab: MOBILE_TAB | None = None
    # 手机版第二期: a full-screen page over the tabs (null closes it) and the AI 首页 timeline's filter.
    mobilePage: MOBILE_PAGE | None = None
    mobileNotifyFilter: MOBILE_NOTIFY_FILTER | None = None
    # AI 简报订阅 (2026-10-05): narrow the timeline's AI 简报 to one product (null = every product).
    mobileNotifyProduct: str | None = Field(default=None, pattern=r"^[A-Za-z]+\.[A-Za-z]{1,4}$", max_length=40)
    # 回测结果页 (2026-10-06): open one comparison of 策略 → 最近的回测 (mobilePage lab opens the latest).
    mobileLab: MobileLabChange | None = None
    # 手机版 策略 (2026-10-08): its two tabs; opens the 策略 tab first.
    mobileStrategyTab: MOBILE_STRATEGY_TAB | None = None
    # 手机看盘 (2026-10-09): 报价 / 多图 / 单图 and their settings; opens the 看盘 tab first.
    mobileMarket: MobileMarketChange | None = None
    # 提醒中心 (2026-10-15): the bell's popover on a computer, the 提醒 page on a phone.
    reminderCenter: ReminderCenterChange | None = None

    @field_validator("remoteSettings")
    @classmethod
    def remote_settings_not_null(cls, value):
        if value is None:
            raise ValueError("遥控器操作不能为 null")
        return value

    @field_validator("panel")
    @classmethod
    def current_panel(cls, value):
        # 2026-10-07: `depth` (the removed 价格阶梯) opens the 下单面板; see LEGACY_PANELS.
        return legacy_panel(value)

    @field_validator("strategyGuide", "strategyIdea", "strategyInstance")
    @classmethod
    def retired_strategy_commands(cls, value, info):
        if value is not None:
            raise ValueError({"strategyGuide": GUIDE_RETIRED, "strategyIdea": IDEA_RETIRED,
                              "strategyInstance": SIGNAL_ACTION_REFUSED}[info.field_name])
        return value

    @model_validator(mode="after")
    def single_change(self):
        fields = self.model_fields_set
        nullable = {"panel", "reviewSelect", "selectPosition", "chartFocus", "selectedPrice", "contractProduct", "mobilePage", "mobileNotifyProduct"}
        if len(fields) != 1 or any(getattr(self, key) is None and key not in nullable for key in fields):
            raise ValueError("一次命令只修改一个界面字段；仅 panel、reviewSelect、selectPosition、chartFocus、selectedPrice、contractProduct、mobilePage、mobileNotifyProduct 可设为 null")
        return self


class UiCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commandId: UUID
    windowId: UUID
    expectedRevision: int = Field(ge=0, strict=True)
    changes: UiChanges


class UiReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commandId: UUID
    status: Literal["applied", "rejected", "unknown"]


class UiPulse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    windowId: UUID
    revision: int = Field(ge=0, strict=True)
    state: UiState
    receipt: UiReceipt | None = None
    observation: UiObservation
    closing: bool = False
    device: UiDevice | None = None
    captureReceipt: CaptureReceipt | None = None
    # 锁屏提醒 (2026-10-06): document.visibilityState === "visible" when the heartbeat left (a change
    # sends one at once). A device with an online, visible window gets no lock-screen push — the page
    # shows the event itself. An older client omits it and counts as not visible.
    visible: bool | None = None


class UiCommandResult(UiCommand):
    status: Literal["queued", "dispatched", "applied", "rejected", "expired", "unknown"]
    state: UiState | None
    observedRevision: int | None = None


class UiWindowSnapshot(BaseModel):
    windowId: UUID
    revision: int
    online: bool
    state: UiState
    device: UiDevice | None = None


class UiWindowDetail(UiWindowSnapshot):
    observation: UiObservation


class UiWindowsSnapshot(BaseModel):
    windows: list[UiWindowSnapshot]


class UiPulseResult(BaseModel):
    command: UiCommandResult | None
    capture: CaptureRequest | None = None


@dataclass
class Window:
    user: str
    identity: object
    pulse: UiPulse
    seen: float


@dataclass
class Command:
    user: str
    identity: object
    request: UiCommand
    created: float
    status: str = "queued"
    state: dict | None = None
    observed_revision: int | None = None
    # When the window reported it ran the command (an "applied" receipt): the Runtime then judges it.
    claimed: float | None = None

    def result(self):
        return {**self.request.model_dump(mode="json", exclude_unset=True),
                "status": self.status, "state": self.state,
                "observedRevision": self.observed_revision}


# What an applied command changed, in the user's words (AI audit 2026-10-13): a multi-step interface
# request is answered with each applied step and where it is, not "已有 N 步界面操作确认生效".
_VIEW_WORDS = surface_labels("views")
_PANEL_WORDS = surface_labels("panels")
_THEME_WORDS = {"dark": "深色", "light": "浅色", "system": "跟随系统"}
_FONT_WORDS = {"standard": "标准", "large": "大", "extra-large": "特大"}
_DENSITY_WORDS = {"compact": "紧凑", "standard": "标准", "comfortable": "宽松"}
_POSITIONS_TAB_WORDS = surface_labels("positionsTabs")
_FILLS_SOURCE_WORDS = {"all": "全部", "manual": "手动下的", "card": "确认卡下的", "ai": "AI 下的",
                       "strategy": "策略下的", "stop": "止损止盈触发的", "liquidation": "本站模拟盘强平的"}
_STRATEGY_TAB_WORDS = surface_labels("strategyTabs")
_REMINDER_TAB_WORDS = surface_labels("reminderTabs")
_SETTINGS_PAGE_WORDS = surface_labels("settingsPages")
_DURATION_WORDS = {60: "1 分钟", 180: "3 分钟", 300: "5 分钟", 900: "15 分钟", 1800: "30 分钟", 3600: "1 小时",
                   86400: "日线", 604800: "周线"}
VOICE_CALL_TAP_HINT = "要是没接通或听不到声音，点一下右下角的 AI 球（浏览器要你亲手点一下才放开麦克风和声音）"


def describe_ui_change(changes: dict[str, Any]) -> str:
    """One applied command's change as a short Chinese phrase (what changed, and where)."""
    if not changes:
        return "界面没有变化"
    key, value = next(iter(changes.items()))
    if key == "view":
        return f"切到{_VIEW_WORDS.get(value, value)}"
    if key == "panel":
        return "收起侧边面板" if value is None else f"打开{_PANEL_WORDS.get(value, value)}"
    if key == "positionsBar":
        return "展开持仓栏" if value == "expanded" else "收起持仓栏"
    if key == "settingsOpen":
        return "打开设置" if value else "关闭设置"
    if key == "settingsPage":
        # 2026-10-25: 设置 → 提醒 is the 提醒面板's 通用提醒 tab now.
        return "打开提醒面板的通用提醒" if value == "notify" else f"设置切到「{_SETTINGS_PAGE_WORDS.get(value, value)}」页"
    if key == "reminders":
        return f"提醒面板切到「{_REMINDER_TAB_WORDS.get((value or {}).get('tab'), '提醒')}」"
    if key == "theme":
        return f"主题换成{_THEME_WORDS.get(value, value)}"
    if key == "fontScale":
        return f"字号调成{_FONT_WORDS.get(value, value)}"
    if key == "density":
        return f"界面密度调成{_DENSITY_WORDS.get(value, value)}"
    if key == "positionsTab":
        return f"持仓栏切到「{_POSITIONS_TAB_WORDS.get(value, value)}」页签"
    if key == "fillsSource":
        return f"成交只看{_FILLS_SOURCE_WORDS.get(value, value)}" if value != "all" else "成交显示全部来源"
    if key == "strategyTab":
        if value == "playbooks":
            return "打开复盘页的交易笔记"
        return f"策略面板切到「{_STRATEGY_TAB_WORDS.get(value, value)}」"
    if key == "reviewDay":
        return f"复盘切到 {value} 这一天"
    if key == "reviewCurrency":
        return "复盘切到美元（外盘）" if value == "USD" else "复盘切到人民币"
    if key == "symbol":
        return f"合约切到 {foreign_display_name(str(value)) or value}"
    if key == "duration":
        return f"周期切到{_DURATION_WORDS.get(value, value)}"
    if key == "chartView":
        return "切到分时图" if value == "timeshare" else "切回 K 线"
    if key == "dockSide":
        return f"侧边栏放到{'左' if value == 'left' else '右'}边"
    if key == "highlight":
        return "在界面上高亮了要看的位置"
    if key == "reminderCenter" and isinstance(value, dict):
        if value.get("open") is False:
            return "关闭提醒中心"
        if value.get("more"):
            return "继续加载当天提醒"
        return "打开提醒中心" + ("，只看未读" if value.get("filter") == "unread" else "，看全部" if value.get("filter") == "all" else "")
    if key in {"disclosure", "dialog"} and isinstance(value, dict):
        name, opened = str(value.get("name") or ""), bool(value.get("open"))
        if key == "disclosure" and name == "voice-call":
            return f"开始接通语音通话（{VOICE_CALL_TAP_HINT}）" if opened else "挂断语音通话"
        return f"{'打开' if opened else '关闭'}「{name}」{'对话框' if key == 'dialog' else ''}"
    return f"界面 {key} 已改为 {value}"



# A window pulses every 30 s while idle once the SSE feed can nudge it, and at
# once when a command, capture or receipt is pending: ONLINE_SECONDS lives in ui_captures.


# How long a command the window ran may take to show in its state before the receipt is unknown.
RECEIPT_SECONDS = 7


def refused(state, changes) -> bool:
    """A command whose work finished without doing it: rejected at once, its reason in the snapshot — today only
    reminderCenter {more: true} whose read loaded nothing (state.reminderCenter.moreError, #932 R5)."""
    wanted = changes.get("reminderCenter")
    center = state.get("reminderCenter")
    return bool(isinstance(wanted, dict) and wanted.get("more") and isinstance(center, dict)
                and int(center.get("moreAsked") or 0) > 0 and center.get("moreDone") == center.get("moreAsked")
                and center.get("moreError"))


# Round-eleven surface commands are judged by the state they change. A client too old to report a
# field is not contradicted: a missing key counts as matched.
_SURFACE_COMMANDS = {"pinnedSymbols", "reviewExpand", "reviewFilter", "reviewSelect", "reviewDay", "reviewCurrency", "dialog",
                     "strategyDetails", "monitorCard", "selectPosition", "disclosure",
                     "highlight", "chartAnnotations", "chartMarks", "chartFocus", "tour", "preference",
                     "tradingAccount", "strategyBacktest", "strategyTemplate",
                     "strategyEditor", "strategyLab", "help", "news", "researchDebate",
                     "boardExchange", "boardSort", "contractProduct", "contractCatalogRetry"}


def _template_matches(card, value) -> bool:
    """策略模板库: judged by the gallery / parameter page / 详情 the client reports."""
    if card is None:
        return True   # a client before the template library: the client's receipt stands
    params = card.get("params") or {}
    detail = card.get("detail") or {}
    if (value.get("run") or value.get("save")) and card.get("savedStrategyId"):
        return True   # 2026-10-08: run (save) consumes the parameter page; the saved strategy is the proof
    return ((value.get("open") is None or bool(card.get("open")) == value["open"])
            and (value.get("templateId") is None or card.get("templateId") == value["templateId"])
            and all(params.get(k) == v for k, v in (value.get("params") or {}).items())
            and (value.get("timeframe") is None or card.get("timeframe") == value["timeframe"])
            # 2026-10-08: run (save is its old name) saves the strategy and opens its 结果页.
            and (not value.get("run") or bool(card.get("running") or card.get("summary") or card.get("error")
                                               or card.get("savedStrategyId")))
            and (not value.get("save") or bool(card.get("running") or card.get("savedStrategyId") or card.get("error")))
            and (not value.get("restore") or bool(detail.get("defaults"))))


def _editor_matches(card, value) -> bool:
    """策略编辑卡: close by the card being closed; open / new / edit / start by the open card (edit by the
    strategy it edits, start by its 起点 — habits also counts when the client reported that the trades are
    too few to prefill, which opens it blank); answers / ask once the card took the sentence (its own
    result is the design job's); save by the saving flag, the saved strategy or the error."""
    if card is None:
        return True   # a client before the card: the client's receipt stands
    if value.get("close"):
        return not card.get("open")
    if value.get("save") and card.get("savedStrategyId"):
        return True   # a save closes the card; the closed snapshot keeps the saved strategy
    if not card.get("open"):
        return False
    if card.get("loading") and any(value.get(key) is not None for key in ("open", "new", "edit", "start")):
        return False   # the prefill / template is still loading into the form
    if value.get("edit") is not None and card.get("strategyId") != value["edit"]:
        return False
    if value.get("new") and value.get("start") is None and card.get("strategyId"):
        return False
    if value.get("reminder") and not card.get("reminder"):
        return False   # 新建提醒: the card opened in 提醒模式, not an ordinary new strategy
    start = value.get("start")
    prefill = card.get("prefill") if isinstance(card.get("prefill"), dict) else {}
    if start is not None and card.get("start") != start and not (start == "habits" and prefill.get("eligible") is False):
        return False
    ask = card.get("ask") if isinstance(card.get("ask"), dict) else {}
    if value.get("ask") is not None and ask.get("text") != value["ask"]:
        return False
    if value.get("answers") is not None and not ask.get("status"):
        return False
    if value.get("focus") is not None and "focus" in card and card.get("focus") != value["focus"]:
        return False   # a card before 第二版 reports no focus: the client's receipt stands
    choose = value.get("choose") if isinstance(value.get("choose"), dict) else {}
    fields = card.get("fields") if isinstance(card.get("fields"), dict) else {}
    shown = {**(fields.get("reminder") or {}), "signalAction": fields.get("signalAction")}
    if any(shown.get(key) != choice for key, choice in choose.items() if choice is not None):
        return False   # 点选: the card shows every chosen value
    if value.get("save"):
        return bool(card.get("saving") or card.get("savedStrategyId") or card.get("error"))
    return True


def _lab_matches(state, value) -> bool:
    """试几组参数比一比: open by the 结果页 it lives on (a newer client reports detail, an older one lab),
    labId by the current comparison, run by the status the client reports; choose's own proof is the
    business call's result, so the comparison only has to know the variant."""
    if value.get("open") and state.get("strategyTab") not in {None, "lab", "detail"}:
        return False
    card = state.get("strategyLab")
    if card is None:
        return True   # a client before the lab: the client's receipt stands
    if value.get("labId") is not None and card.get("labId") != value["labId"]:
        return False
    if value.get("run"):
        return card.get("status") in {"running", "done", "error"}
    if value.get("choose") is not None:
        return any(str(row.get("variantId")) == value["choose"] for row in card.get("rows") or [] if isinstance(row, dict))
    return True


def _help_matches(card, value) -> bool:
    """使用说明卡: judged by the topic, search, 完整说明 and 常见问题 the card reports."""
    if card is None:
        return True   # a client before the two-layer card: the client's receipt stands
    topic = resolve_topic(value["topic"]) if value.get("topic") is not None else None
    faq = value.get("faq")
    return (bool(card.get("open"))
            and (topic is None or card.get("topic") == topic)
            and (value.get("group") is None or card.get("group") == value["group"])
            and (value.get("query") is None or (card.get("query") or "") == value["query"])
            and (value.get("detail") is None or bool(card.get("detailOpen")) == value["detail"])
            and (faq is None or (faq["index"] in (card.get("faqOpen") or [])) == faq["open"])
            # retry: a new read started — loadStatus moves on to loading / ready (or error again if it failed
            # once more); offline means nothing could be read. Older cards without loadStatus keep their receipt.
            and (not value.get("retry") or card.get("loadStatus") != "offline"))


def _debate_matches(card, value) -> bool:
    """多 AI 研讨: close by the sub-view being gone; open by the id it shows; start / rerun once the view took
    the request (its own result arrives later and is the debate's: request.status sending / started /
    reused / refused with the reason)."""
    if value.get("close"):
        return not (card or {}).get("open")
    if not isinstance(card, dict) or not card.get("open"):
        return False
    if value.get("open") is not None:
        return card.get("id") == value["open"]
    request = card.get("request") if isinstance(card.get("request"), dict) else {}
    if value.get("rerun"):
        return request.get("kind") == "rerun" and bool(request.get("status"))
    wanted = value.get("start")
    return request.get("kind") == "start" and bool(request.get("status")) and (
        wanted in ("desk", "event") or request.get("eventId") == wanted)


def _surface_matches(state, key, value) -> bool:
    review = state.get("review") or {}
    if key == "researchDebate":
        return _debate_matches(state.get("researchDebate"), value)
    if key == "news":
        news = state.get("news") or {}
        desk = news.get("desk") or {}

        def shown(name: str, wanted: Any) -> bool:
            if name == "deskRejudge":  # the page sent the refresh (sending); its answer follows in rejudge.message
                rejudge = desk.get("rejudge")
                return news.get("tab") == "desk" and isinstance(rejudge, dict) and bool(rejudge.get("status"))
            if name == "deskSource":
                return news.get("tab") == "desk" and desk.get("highlight") == wanted
            return news.get(name) == wanted
        return state.get("panel") == "news" and all(shown(name, wanted) for name, wanted in value.items())
    if key == "pinnedSymbols":
        if state.get("pinnedSymbols") is None:
            return True
        pinned = {str(item).upper() for item in state["pinnedSymbols"]}
        add, remove = value.get("add"), value.get("remove")
        return add.upper() in pinned if add else str(remove).upper() not in pinned
    if key == "reviewExpand":
        if state.get("review") is None:
            return "review" not in state
        month, day = value.get("month"), value.get("day")
        return review.get("month") == month if month else review.get("day") == day
    if key == "reviewFilter":
        return "review" not in state or review.get("filter") == value
    if key == "reviewSelect":
        return "review" not in state or review.get("selectedTradeId") == value
    if key == "reviewCurrency":
        return "review" not in state or "currency" not in review or review.get("currency") == value
    if key == "reviewDay":
        # 今日复盘: the block reports the day it shows; a client before the block reports no field.
        shown = review.get("dailyReview") if isinstance(review, dict) else None
        return "review" not in state or "dailyReview" not in review or (shown or {}).get("tradingDay") == value
    if key == "dialog":
        if value["name"] == "positions-drawer":
            # 1.6.6: the 持仓抽屉 became the 持仓 side panel; the dialog name stays as its alias.
            # A 1.6.5 client still reports the drawer itself under `dialog`.
            showing = state.get("panel") == "positions" or state.get("dialog") == "positions-drawer"
            return showing if value["open"] else not showing
        if value["name"] == "strategy-designer" and isinstance(state.get("strategyEditor"), dict):
            # 2026-10-08: the retired 策略设计窗's name opens / closes the 策略编辑卡, which is no dialog —
            # judged by the card.
            return bool(state["strategyEditor"].get("open")) == value["open"]
        if "dialog" not in state:
            return True
        return state.get("dialog") == value["name"] if value["open"] else state.get("dialog") != value["name"]
    if key == "strategyBacktest":
        card = state.get("strategyBacktest")
        if card is None:
            return True   # a client before 规则回测, or no strategy on 详情: the client's receipt stands
        params = card.get("params") or {}
        return ((value.get("tradingDays") is None or params.get("tradingDays") == value["tradingDays"])
                and (value.get("slippageTicks") is None or params.get("slippageTicks") == value["slippageTicks"])
                and (value.get("mark") is None or card.get("marked") == value["mark"])
                and (value.get("trades") is None or card.get("tradesOpen") == value["trades"])
                and (not value.get("run") or bool(card.get("running") or card.get("summary")))
                and (not value.get("applyAdvice") or card.get("applying") is True
                     or card.get("appliedAdvice") == value["applyAdvice"]))
    if key == "strategyTemplate":
        return _template_matches(state.get("strategyTemplate"), value)
    if key == "strategyEditor":
        return _editor_matches(state.get("strategyEditor"), value)
    if key == "strategyLab":
        return _lab_matches(state, value)
    if key == "help":
        return _help_matches(state.get("help"), value)
    if key == "boardExchange":
        # 报价总表: judged by the exchange filter the board view reports; a client
        # before the board (or one not on it) reports no field, and its receipt stands.
        return state.get("boardExchange") is None or state.get("boardExchange") == value
    if key == "boardSort":
        sort = state.get("boardSort")
        if sort is None:
            return True
        return sort.get("key") == value["key"] and sort.get("order") == value["order"]
    if key == "contractProduct":
        # The drawer's expanded product; the client rejects the command when the
        # drawer is closed, so here only the reported product has to agree.
        if "contractProduct" not in state:
            return True
        return state.get("contractProduct") == value
    if key == "contractCatalogRetry":
        # A new read started (loading, or already answered); the client refuses it when there is nothing
        # to retry, so only a drawer reporting it can no longer read (offline) contradicts it.
        catalog = state.get("contractCatalog")
        return catalog is None or catalog.get("status") != "offline"
    if key == "strategyDetails":
        sections = state.get("strategyDetails")
        return sections is None or sections.get(value["section"]) == value["open"]
    if key == "monitorCard":
        cards = state.get("monitorCards")
        return cards is None or cards.get(value["name"]) == ("expanded" if value["expanded"] else "collapsed")
    if key == "disclosure":
        disclosures = state.get("disclosures")
        if disclosures is None:
            return True
        entry = f"{value['name']}:{value['id']}" if value.get("id") else value["name"]
        if value["name"] in SPARSE_DISCLOSURES:
            return disclosures.get(entry, False) == value["open"]
        alias = DISCLOSURE_ALIASES.get(value["name"])
        if alias is not None:
            # An old name (the first 画线 popover, the designer's 查看完整规则): the client opens the new
            # surface and reports that name; an older client still reports the old one (AI 审计 2026-10-13).
            current = f"{alias}:{value['id']}" if value.get("id") else alias
            return disclosures.get(current, disclosures.get(entry)) == value["open"]
        return disclosures.get(entry) == value["open"]
    if key == "selectPosition":
        if "selectedPosition" not in state:
            return True
        selected = state.get("selectedPosition")
        if value is None:
            return selected is None
        return bool(selected and str(selected.get("symbol", "")).upper() == value["symbol"].upper()
                    and selected.get("direction") == value["direction"])
    if key == "highlight":
        highlights = state.get("highlights")
        if highlights is None:
            return True
        targets = {item.get("target") for item in highlights}
        if value.get("clear"):
            return not targets
        if value.get("durationMs") == 0:
            return value["target"] not in targets
        return value["target"] in targets
    if key == "chartAnnotations":
        items = state.get("chartAnnotations")
        if items is None or (value.get("symbol") and state.get("symbol") != value["symbol"]):
            # Drawings on another contract are not in this snapshot; the client's receipt stands.
            return True
        ids = {item.get("id") for item in items}
        if value["op"] == "clear":
            return not ids
        wanted = {item["id"] for item in value.get("items", [])}
        return wanted <= ids and (value["op"] == "add" or ids <= wanted)
    if key == "chartMarks":
        # 清空图上标记: judged by the cleared-at stamp the client reports (absent = a client
        # before the feature; the client's receipt stands).
        return "marksCleared" not in state or bool(state.get("marksCleared"))
    if key == "tour":
        tour = state.get("tour")
        if tour is None:
            return "tour" not in state  # an older client reports no tour at all; a newer one always reports it
        if value["op"] == "start":
            return bool(tour.get("active")) and tour.get("step") == 1 and tour.get("total") == len(value.get("steps") or [])
        if value["op"] == "stop":
            return not tour.get("active")
        return True  # next / prev: the client's receipt stands (past the last step the tour ends)
    if key == "preference":
        preferences = state.get("preferences")
        if preferences is None:
            return True  # a client before the settings rows reports no document; its receipt stands
        return preference_value(preferences, value["key"]) == value["value"]
    if key == "tradingAccount":
        if state.get("tradingAccount") is None:
            return True  # a client before the switcher reports no account; its receipt stands
        return state.get("tradingAccount") == value
    if key == "chartFocus":
        if "chartFocus" not in state:
            return True
        focus = state.get("chartFocus")
        if value is None:
            return focus is None
        return bool(focus) and focus.get("from") == value["from"] and focus.get("to") == value["to"]
    return False


def _mobile_market_matches(state, value) -> bool:
    """手机看盘: the 看盘 tab shows, and the account document the window reports holds the value (a
    window without the document — a guest — is judged by its own receipt)."""
    if state.get("mobileTab") != "market":
        return False
    if "subPanes" in value:
        disclosures = state.get("disclosures")
        return disclosures is None or bool(disclosures.get("mobile-subpanes")) == value["subPanes"]
    preferences = state.get("preferences")
    market = preference_value(preferences, "ui.mobileMarket") if isinstance(preferences, dict) else None
    if not isinstance(market, dict):
        return True
    views = {"board": "quotes", "columns": "quotes", "layout": "multi", "cell": "multi", "subPane": "single"}
    key = next(iter(value))
    if market.get("view") != views.get(key, value.get("view")):
        return False
    if key in ("board", "columns", "layout", "subPane") and market.get(key) != value[key]:
        return False
    if key == "subPane":
        panes = preference_value(preferences, "chart.subPaneList") or []
        return value["subPane"] in panes
    if key == "cell":
        cell = value["cell"]
        index = cell["index"]
        if market.get("layout") == "periods":
            periods = market.get("periods") or []
            if cell.get("duration") is not None and (index >= len(periods) or periods[index] != cell["duration"]):
                return False
            return not cell.get("symbol") or str(state.get("symbol") or "").upper() == cell["symbol"].upper()
        cells = market.get("cells") or []
        current = cells[index] if index < len(cells) and isinstance(cells[index], dict) else {}
        if "symbol" in cell and str(current.get("symbol") or "").upper() != str(cell.get("symbol") or "").upper():
            return False
        return cell.get("duration") is None or current.get("duration") == cell["duration"]
    return True


def changes_match(state, changes):
    for key, value in changes.items():
        if key == "remoteSettings":
            remote = state.get("remoteSettings")
            if not isinstance(remote, dict):
                return False
            for flag in ("enabled", "diagnostic"):
                if flag in value and remote.get(flag) != value[flag]:
                    return False
            if value.get("resetBindings") and remote.get("bindingsDefault") is not True:
                return False
            if value.get("diagnostic") and remote.get("phase") != "ready":
                return False
            if value.get("disconnect") and remote.get("phase") != "idle":
                return False
            if any((remote.get("bindings") or {}).get(k) != v for k, v in (value.get("bindings") or {}).items()):
                return False
        elif key in _SURFACE_COMMANDS:
            if not _surface_matches(state, key, value):
                return False
        elif key == "panel" and value == "debate":
            # 2026-10-25: the 辩论 page is the 调研面板's 辩论 tab; a client from before reports its own panel.
            news = state.get("news") if isinstance(state.get("news"), dict) else {}
            if state.get("panel") != "debate" and not (state.get("panel") == "news" and news.get("tab") == "debate"):
                return False
        elif key == "panel" and value == "positions":
            # 1.6.6: 持仓 is a side panel again. 1.4.8–1.6.5 mapped this command onto the
            # bottom bar, so a client that reports the bar expanded still counts as applied.
            if state.get("panel") != "positions" and state.get("positionsBar") not in (None, "expanded"):
                return False
        elif key == "aiPage":
            # 1.6.6: the AI dock lost its inner page; both old values open the 对话面板
            # (a 1.6.6–1.6.11 client also filters the flow to 策略与交易 for monitor).
            # Older clients still report aiPage.
            if state.get("aiPage") == value:
                continue
            if state.get("panel") != "ai":
                return False
            if value == "monitor" and state.get("aiFilter") not in (None, "trading"):
                return False
        elif key == "aiFilter":
            # Retired 2026-09-25: a newer client only opens the 对话面板 and reports no filter;
            # a 1.6.6–1.6.11 client still applies it.
            if state.get("aiFilter") is None:
                if state.get("panel") != "ai":
                    return False
            elif state.get("aiFilter") != value:
                return False
        elif key == "mobileTab":
            # 2026-10-06: the retired 通知 tab is AI 首页 now; an older client still reports notify.
            if MOBILE_TAB_ALIASES.get(state.get("mobileTab"), state.get("mobileTab")) != MOBILE_TAB_ALIASES.get(value, value):
                return False
        elif key == "strategyTab" and value == "playbooks":
            # 2026-10-31: 交易笔记 is on the 复盘 page; a client from before still reports its 打法 tab.
            if state.get("view") != "review" and state.get("strategyTab") != "playbooks":
                return False
        elif key == "strategyTab":
            # 2026-10-08: the retired 实验室 tab opens the 结果页; an older client still reports lab.
            if STRATEGY_TAB_ALIASES.get(state.get("strategyTab"), state.get("strategyTab")) != STRATEGY_TAB_ALIASES.get(value, value):
                return False
        elif key == "reminderCenter" and isinstance(value, dict) and value.get("more"):
            # 继续加载当天提醒 (#932 R5): applied once this command's read landed — moreDone caught up with moreAsked and
            # no moreError (a client before those fields reports none: its open center stands).
            center = state.get("reminderCenter")
            if not isinstance(center, dict) or center.get("open") is not True:
                return False
            if not changes_match(center, {name: wanted for name, wanted in value.items() if name != "more"}):
                return False
            if "moreAsked" in center and not (
                    int(center.get("moreAsked") or 0) > 0 and center.get("moreDone") == center.get("moreAsked")
                    and not center.get("moreError")):
                return False
        elif key == "mobileNotifyFilter" and value == "reminder":
            # 2026-10-15: 提醒 left the timeline; the old filter opens the 提醒 page (the 提醒中心).
            if state.get("mobilePage") != "reminders":
                return False
        elif key == "mobileStrategyTab":
            if state.get("mobileStrategyTab") != value:
                return False
        elif key == "mobileMarket":
            if not _mobile_market_matches(state, value):
                return False
        elif key == "positionsTab":
            wanted = POSITIONS_TAB_ALIASES.get(value, value)
            if POSITIONS_TAB_ALIASES.get(state.get("positionsTab"), state.get("positionsTab")) != wanted:
                return False
        elif key == "presentation":
            if not changes_match(state, {k: v for k, v in value.items() if k != "reset"}):
                return False
            # 恢复默认布局: the layout fields the client resets (older clients report no positionsBar).
            if value.get("reset") and (state.get("dockSide") != "right" or state.get("positionsBar") not in (None, "collapsed")):
                return False
        elif key == "chartOption":
            if state["chart"][value["name"]] != value["visible"]:
                return False
        elif key == "settingsPage" and value == "notify":
            # 2026-10-25: 设置 → 提醒 is the 提醒 panel's / page's 通用提醒 tab; a client from before reports its own page.
            reminders = state.get("reminders") if isinstance(state.get("reminders"), dict) else {}
            if settings_page(state.get("settingsPage")) != "notify" and reminders.get("tab") != "general":
                return False
        elif key == "reminders":
            reminders = state.get("reminders") if isinstance(state.get("reminders"), dict) else {}
            if value.get("tab") and reminders.get("tab") != value["tab"]:
                return False
        elif key == "settingsPage":
            if settings_page(state.get("settingsPage")) != settings_page(value):
                return False
        elif key == "strategy" and value.get("tab") == "fees":
            # 策略的“手续费” opens 设置 → 高级交易设置; the strategy panel itself stays on 我的策略
            # (AI 审计 2026-10-13: judged against the panel's own tab, it never matched).
            if not (state.get("settingsOpen") and settings_page(state.get("settingsPage")) == "trade-advanced"):
                return False
        elif key == "chatSession":
            # A new session's id is only known to the client; its applied receipt stands.
            if value.get("sessionId") and state.get("chatSessionId") != value["sessionId"]:
                return False
        elif isinstance(value, dict):
            if not isinstance(state.get(key), dict) or not changes_match(state[key], value):
                return False
        elif key == "selectedPrice":
            if state.get(key) != value or (value is not None and (
                state.get("ticketPrice") != value or state.get("ticketPriceMode") != "limit"
            )):
                return False
        elif state.get(key) != value:
            return False
    return True


# 界面命令紧凑索引 (2026-10-14 token 瘦身): the embedded AI's interface rounds used to carry the whole
# UiCommand JSON Schema (~16k tokens) in their instructions and the catalog. The index is the same
# schema read mechanically — every changes field with its values or keys — and one field's full
# definition plus the description sentences that name it come from `ui_field_detail` on request
# (app_capabilities operation="ui:<field>"). Both derive from UiChanges: no second hand-written table.
UI_COMMAND_OPERATION = "POST /api/app/ui/commands"


def _ui_resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """A property schema without its nullable wrapper and with a top-level $ref resolved."""
    if "anyOf" in schema:
        branches = [branch for branch in schema["anyOf"] if branch.get("type") != "null"]
        if len(branches) == 1:
            schema = branches[0]
    ref = str(schema.get("$ref") or "")
    if ref.startswith("#/$defs/"):
        return defs.get(ref.removeprefix("#/$defs/"), schema)
    return schema


def _ui_value(schema: dict[str, Any], defs: dict[str, Any], *, depth: int = 0) -> str:
    schema = _ui_resolve(schema, defs)
    if "enum" in schema:
        values = [("null" if value is None else str(value)) for value in schema["enum"]]
        return "|".join(values)
    if "const" in schema:
        return str(schema["const"]).lower() if isinstance(schema["const"], bool) else str(schema["const"])
    kind = schema.get("type")
    if kind == "object" and isinstance(schema.get("properties"), dict):
        if depth >= 1:
            return "{" + ",".join(schema["properties"]) + "}"
        required = set(schema.get("required") or ())
        parts = []
        for key, child in schema["properties"].items():
            value = _ui_value(child, defs, depth=depth + 1)
            mark = "" if key in required else "?"
            hint = str(_ui_resolve(child, defs).get("description") or child.get("description") or "")
            if value == "str" and hint:
                value += "「" + hint.split("。", 1)[0][:60] + "」"
            parts.append(f"{key}{mark}:{value}")
        return "{" + ",".join(parts) + "}"
    if kind == "array":
        return "[" + _ui_value(schema.get("items") or {}, defs, depth=depth + 1) + "]"
    if kind == "boolean":
        return "bool"
    if kind == "integer":
        low, high = schema.get("minimum"), schema.get("maximum")
        return f"int {low}-{high}" if low is not None and high is not None else "int"
    if kind == "number":
        return "number"
    if kind == "string":
        return "str"
    return "object"


def _ui_schema() -> tuple[dict[str, Any], dict[str, Any]]:
    schema = UiCommand.model_json_schema()
    defs = schema.get("$defs", {})
    return defs["UiChanges"], defs


def ui_command_index() -> str:
    """Every UiChanges field with its values (enums inline) or keys, on one compact text."""
    changes, defs = _ui_schema()
    fields = [f"{name}={_ui_value(prop, defs)}" for name, prop in changes["properties"].items()]
    return (f"{UI_COMMAND_OPERATION} body={{commandId:新UUID,windowId,expectedRevision,changes:{{只放一个字段}}}}"
            " query={waitMs:3000}；applied 才算生效，回执 state 已核对所改字段、observedRevision 可接着用；"
            "queued/dispatched 按原 commandId 查询，不重发。字段（? 为可省略键）：" + "；".join(fields)
            + "。某字段的完整定义与说明：app_capabilities operation=\"ui:<字段>\"。")


def ui_field_detail(field: str) -> dict[str, Any] | None:
    """One UiChanges field: its schema (with the definitions it references) and the sentences of the
    UiChanges description that name it."""
    changes, defs = _ui_schema()
    prop = changes["properties"].get(field)
    if prop is None:
        return None
    used: dict[str, Any] = {}

    def include(value: Any) -> None:
        if isinstance(value, dict):
            name = str(value.get("$ref") or "").removeprefix("#/$defs/")
            if name in defs and name not in used:
                used[name] = defs[name]
                include(defs[name])
            for child in value.values():
                include(child)
        elif isinstance(value, list):
            for child in value:
                include(child)

    include(prop)
    description = str(changes.get("description") or "")
    notes = [part.strip() for part in re.split(r"(?<=[；。])", description)
             if re.search(rf"(?<![A-Za-z]){re.escape(field)}(?![A-Za-z])", part)]
    return {"field": field, "schema": prop, **({"$defs": used} if used else {}), "notes": notes}


def register_ui_control(app, current_user):
    lock = asyncio.Lock()
    windows: dict[UUID, Window] = {}
    commands: dict[tuple[str, UUID], Command] = {}

    def log_result(command):
        logging.getLogger("uvicorn.error.agent").info(
            "agent_ui command_id=%s window_id=%s status=%s elapsed_ms=%.1f",
            command.request.commandId, command.request.windowId, command.status,
            (monotonic() - command.created) * 1000,
        )

    def judge(command, pulse, *, final=False):
        """回执只在这里判 (owner 2026-11-01): a command the window says it ran is applied once the state it reports
        shows the change (changes_match), rejected when its work finished without doing it, and — `final`, after
        RECEIPT_SECONDS — unknown otherwise. A state change pulses at once, so the judgement follows it."""
        changes = command.request.changes.model_dump(exclude_unset=True)
        state = pulse.state.model_dump()
        if refused(state, changes):
            command.status = "rejected"
        elif changes_match(state, changes):
            command.status = "applied"
        elif final:
            command.status = "unknown"
        else:
            return
        command.state, command.observed_revision = state, pulse.revision
        log_result(command)

    def prune():
        now = monotonic()
        for key, command in list(commands.items()):
            if command.status == "dispatched" and command.claimed is not None and now - command.claimed > RECEIPT_SECONDS:
                window = windows.get(command.request.windowId)
                if window is not None:
                    judge(command, window.pulse, final=True)
                else:
                    command.status = "unknown"
                    log_result(command)
            if now - command.created > 15 and command.status in {"queued", "dispatched"}:
                command.status = "expired" if command.status == "queued" else "unknown"
                log_result(command)
            if now - command.created > 300:
                del commands[key]
        for key, window in list(windows.items()):
            if now - window.seen > 6 * ONLINE_SECONDS:
                del windows[key]

    async def identity(request):
        user = await current_user(request)
        inherited = request.scope.get("ai_trader.call_identity")
        if inherited is not None:
            await inherited.verify()
            if inherited.user != user:
                raise HTTPException(403, "操作身份与当前用户不一致")
            return user, inherited
        headers = {key: request.headers[key] for key in ("authorization", "cookie") if key in request.headers}
        return user, app.state.app_capabilities.capture_identity(user, headers=headers)

    async def owned_window(window_id, user):
        window = windows.get(window_id)
        if window is None or window.user != user:
            raise HTTPException(404, "目标窗口不存在")
        if window.pulse.closing:
            raise HTTPException(410, "目标窗口已关闭")
        try:
            await window.identity.verify()
        except HTTPException:
            raise HTTPException(410, "窗口登录身份已失效") from None
        return window

    accept_capture, dispatch_capture = register_ui_captures(app, identity, owned_window, lock, commands)

    def visible_devices(user: str) -> set[str]:
        """锁屏推送's one rule about windows: the devices on which this user has an online window
        whose page is in front. A read of the registry without awaiting, so it needs no lock."""
        now = monotonic()
        return {window.pulse.device.deviceId for window in windows.values()
                if window.user == user and not window.pulse.closing and window.pulse.visible is True
                and window.pulse.device is not None and now - window.seen <= ONLINE_SECONDS}
    app.state.ui_visible_devices = visible_devices

    async def verify_ui_window(identifier, user):
        async with lock:
            window = await owned_window(UUID(identifier), user)
            if monotonic() - window.seen > ONLINE_SECONDS:
                raise HTTPException(409, "对话窗口已离线，请重新连接")
    app.state.verify_ui_window = verify_ui_window

    # Kept outside /api/app: agents cannot forge a browser heartbeat or receipt.
    @app.post("/api/ui/pulse", response_model=UiPulseResult, response_model_exclude_unset=True)
    async def pulse(request: Request, body: UiPulse):
        from .guest import guest_request
        async with lock:
            user, caller = await identity(request)
            # M1a: the UI heartbeat is the primary "this user is online" signal
            # for on-demand account reclamation.
            app.state.presence.touch(user)
            prune()
            window = windows.get(body.windowId)
            if body.closing:
                if window is not None:
                    if window.user != user:
                        raise HTTPException(404, "目标窗口不存在")
                    await window.identity.verify()
                # Retain the closed id so a heartbeat arriving after pagehide
                # cannot register the departed document again.
                windows[body.windowId] = Window(user, caller, body, monotonic())
                for command in commands.values():
                    if command.user == user and command.request.windowId == body.windowId:
                        if command.status == "queued":
                            command.status = "expired"
                        elif command.status == "dispatched":
                            command.status = "unknown"
                return {"command": None}
            if window is None:
                if sum(w.user == user and not w.pulse.closing for w in windows.values()) >= 8:
                    # Reloads get a fresh consumer id. Retained offline snapshots
                    # must not occupy every slot until their 60-second expiry.
                    offline = [(key, w) for key, w in windows.items()
                               if w.user == user and not w.pulse.closing and monotonic() - w.seen > ONLINE_SECONDS]
                    if not offline:
                        raise HTTPException(429, "已连接窗口过多")
                    retired, _ = min(offline, key=lambda item: item[1].seen)
                    del windows[retired]
                    for command in commands.values():
                        if command.user == user and command.request.windowId == retired:
                            if command.status == "queued":
                                command.status = "expired"
                            elif command.status == "dispatched":
                                command.status = "unknown"
                previous_device = None
                window = Window(user, caller, body, monotonic())
                windows[body.windowId] = window
            else:
                await owned_window(body.windowId, user)
                if body.revision < window.pulse.revision or (body.revision == window.pulse.revision and body.state != window.pulse.state):
                    raise HTTPException(409, "窗口状态版本冲突")
                previous_device = window.pulse.device.deviceId if window.pulse.device else None
                window.pulse, window.seen = body, monotonic()
            # 锁屏提醒 (#521): a device pushes only to whoever is signed in on it. The first heartbeat
            # of a window (a new login gets a new window) retires other users' subscriptions here.
            if body.device is not None and body.device.deviceId != previous_device and not guest_request(request):
                app.state.store.retire_device_push_subscriptions(device_id=body.device.deviceId, keep_user_id=user)
            if body.receipt:
                command = commands.get((user, body.receipt.commandId))
                if command and command.request.windowId == body.windowId and command.status in {"dispatched", "unknown"}:
                    try:
                        await command.identity.verify()
                        authorized = True
                    except HTTPException:
                        authorized = False
                    if authorized and body.receipt.status == "applied":
                        # The window ran it; judge() decides whether it took effect, on this and later heartbeats.
                        command.status, command.claimed = "dispatched", monotonic()
                    elif authorized and body.receipt.status == "rejected" and body.revision != command.request.expectedRevision:
                        # The window had moved on before it ran it (state settling late, 2026-11-01 readback): the
                        # same outcome as a refusal at dispatch — rejected with no state, which callers re-send.
                        command.status = "rejected"
                        log_result(command)
                    else:
                        command.status = body.receipt.status if authorized else "unknown"
                        command.state = body.state.model_dump()
                        command.observed_revision = body.revision
                        log_result(command)
            for command in list(commands.values()):
                if command.user == user and command.request.windowId == body.windowId and command.status == "dispatched" \
                        and command.claimed is not None:
                    judge(command, body)
            await accept_capture(body, user)
            for command in commands.values():
                if command.user != user or command.request.windowId != body.windowId or command.status != "queued":
                    continue
                try:
                    await command.identity.verify()
                except HTTPException:
                    command.status = "rejected"
                    continue
                if command.request.expectedRevision != body.revision:
                    command.status = "rejected"
                    continue
                command.status = "dispatched"
                return {"command": command.result()}
            return {"command": None, "capture": await dispatch_capture(body, user)}

    @app.get("/api/app/ui/windows", response_model=UiWindowsSnapshot)
    async def list_windows(request: Request, deviceId: str | None = Query(default=None, max_length=100),
                           windowId: UUID | None = None):
        async with lock:
            user = await current_user(request)
            prune()
            result = []
            for identifier, window in list(windows.items()):
                if window.user != user or window.pulse.closing:
                    continue
                if windowId is not None and identifier != windowId:
                    continue
                if deviceId not in {None, "*"} and (window.pulse.device is None or window.pulse.device.deviceId != deviceId):
                    continue
                try:
                    await window.identity.verify()
                except HTTPException:
                    continue
                result.append({"windowId": str(identifier), "revision": window.pulse.revision,
                               "online": monotonic() - window.seen <= ONLINE_SECONDS, "state": window.pulse.state.model_dump(), "device": window.pulse.device})
            return {"windows": result}

    @app.get("/api/app/ui/windows/{window_id}", response_model=UiWindowDetail)
    async def window_detail(request: Request, window_id: UUID):
        async with lock:
            user = await current_user(request)
            prune()
            window = await owned_window(window_id, user)
            return {"windowId": window_id, "revision": window.pulse.revision,
                    "online": monotonic() - window.seen <= ONLINE_SECONDS, "state": window.pulse.state,
                    "observation": window.pulse.observation, "device": window.pulse.device}

    @app.post("/api/app/ui/commands", response_model=UiCommandResult, response_model_exclude_unset=True)
    async def enqueue(request: Request, body: UiCommand, waitMs: int = Query(default=0, ge=0, le=5000)):
        async with lock:
            user, caller = await identity(request)
            prune()
            key = (user, body.commandId)
            if key in commands:
                command = commands[key]
                if command.request != body:
                    raise not_enqueued(409, "命令编号已用于不同参数")
            else:
                try:
                    window = await owned_window(body.windowId, user)
                except HTTPException as error:
                    raise not_enqueued(error.status_code, str(error.detail)) from None
                if monotonic() - window.seen > ONLINE_SECONDS:
                    raise not_enqueued(409, "目标窗口离线，请回读窗口列表")
                if body.expectedRevision != window.pulse.revision:
                    raise not_enqueued(409, "界面已变化，请重新读取状态")
                if any(c.user == user and c.request.windowId == body.windowId and c.status in {"queued", "dispatched", "unknown"}
                       for c in commands.values()):
                    raise not_enqueued(409, "已有未确认的界面命令，请按原编号查询")
                if sum(c.user == user for c in commands.values()) >= 256:
                    raise not_enqueued(429, "界面命令过多，请稍后再试")
                command = Command(user, caller, body, monotonic())
                commands[key] = command
        feed = getattr(app.state, "runtime_feed", None)
        if feed is not None:
            await feed.publish_ui_nudge(user, "command")
        # Release the UI lock while the native consumer renders and acknowledges.
        # Cancellation/timeout only ends this wait; it never re-enqueues a command.
        deadline = asyncio.get_running_loop().time() + waitMs / 1000
        while True:
            async with lock:
                await caller.verify()
                prune()
                remaining = deadline - asyncio.get_running_loop().time()
                if command.status not in {"queued", "dispatched"} or remaining <= 0:
                    return command.result()
            await asyncio.sleep(min(.05, remaining))

    @app.get("/api/app/ui/commands/{command_id}", response_model=UiCommandResult, response_model_exclude_unset=True)
    async def command_status(request: Request, command_id: UUID):
        async with lock:
            user = await current_user(request)
            prune()
            command = commands.get((user, command_id))
            if command is None:
                raise HTTPException(404, "命令不存在或已超过五分钟保留期，请回读窗口状态")
            return command.result()

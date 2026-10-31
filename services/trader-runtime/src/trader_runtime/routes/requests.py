"""Request bodies of the HTTP API; the response shapes stay plain dicts."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ..instruments import CONTEXT_SYMBOL_PATTERN
from ..message_context import FuturesWorkspaceContext
from ..strategy_rules import ProtectionTrailing


class SessionRequest(BaseModel):
    mode: Literal["local", "cloud"] = "local"
    agentId: Literal["native"] = "native"


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    workspaceContext: FuturesWorkspaceContext | None = None
    uiWindowId: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    # "voice": delegated by the real-time voice call; the agent adds a spoken summary to its answer.
    # "ptt" (09-28): held words from 按住说话 — also answered with a spoken summary (the client reads it
    # out), but no call is running: the call-only rules (spoken 确认, auto-confirm) do not apply.
    origin: Literal["voice", "ptt"] | None = None
    # Voice: the user spoke this while the previous answer was still being read out.
    followUp: bool | None = None
    # 我的流程 (#522): the routine the user picked (设置里的“运行”, POST /api/routines/{id}/run). `text`
    # stays the chat's “跑一下<名称>”; the run takes this routine by id, never by the name in the text.
    routineId: str | None = Field(default=None, min_length=1, max_length=80)


class FeedFocusRequest(BaseModel):
    symbol: str | None = Field(default=None, min_length=1, max_length=80, pattern=CONTEXT_SYMBOL_PATTERN)
    durationSeconds: Literal[60, 180, 300, 900, 1800, 3600, 86400, 604800] = 60
    # The client applies feed.market-patch (2026-09-27). Older clients never send it and
    # keep receiving full feed.market frames only.
    marketPatches: bool = False
    # The client applies feed.touch (2026-09-28): an unchanged account / activity / execution frame is
    # refreshed by its capture stamps alone. Older clients keep the full frame every 5 s.
    feedTouch: bool = False
    # The last closed bar (its `datetime`) the client holds for this chart (2026-10-13, #816): the
    # first chart frame then carries only the bars after it plus `barsAfter` / `barsWindow`, as
    # GET /api/markets/{symbol}/workbench?since= does. Omitted: the whole window, as before.
    since: int | None = Field(default=None, ge=0)


_Code = Annotated[str, Field(min_length=1, max_length=80, pattern=CONTEXT_SYMBOL_PATTERN)]


class FeedBoardWantRequest(BaseModel):
    """The board rows a window shows (2026-10-12): `all`, or rows of these exchanges, products
    ("au" / "SHFE.au") and contracts. `key` is echoed in every board frame."""
    model_config = ConfigDict(extra="forbid")
    all: bool = False
    exchanges: list[_Code] = Field(default_factory=list, max_length=16)
    products: list[_Code] = Field(default_factory=list, max_length=200)
    symbols: list[_Code] = Field(default_factory=list, max_length=200)
    key: str = Field(default="", max_length=80, pattern=r"^[A-Za-z0-9_:.-]*$")


class FeedViewRequest(BaseModel):
    """What the window shows (2026-10-12): page visibility (Page Visibility API) and the board rows
    on screen (None = no board). A hidden page gets no chart market or board push; account,
    activity, execution and every event (orders, alerts, reminders, AI results) keep flowing."""
    model_config = ConfigDict(extra="forbid")
    visible: bool = True
    board: FeedBoardWantRequest | None = None


class SessionAccountRequest(BaseModel):
    """Switch the account a session acts on; the caller needs a grant on it. `expectedGeneration` (#932 follow-up):
    the session's accountGeneration this switch was decided on — a newer switch landed first means 409
    account_switch_superseded and nothing changes; without it (an older client) the switch applies as before."""
    accountId: str = Field(min_length=1, max_length=120)
    expectedGeneration: int | None = Field(default=None, ge=0)


class OwnerLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class OwnerLogoutRequest(BaseModel):
    """锁屏提醒 (#521): the signing-out browser's device id (the UI heartbeat's), so this device's
    lock-screen push for the user stops with the session. Optional: an older client sends no body."""
    deviceId: str | None = Field(default=None, min_length=8, max_length=100)


class OwnerPasswordChangeRequest(BaseModel):
    currentPassword: str = Field(min_length=1, max_length=256)
    newPassword: str = Field(min_length=1, max_length=256)


class AdminPasswordResetRequest(BaseModel):
    newPassword: str = Field(min_length=1, max_length=256)


class OwnerRegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)
    inviteCode: str = Field(min_length=1, max_length=200)


class TqkqLoginRequest(BaseModel):
    """M1b: log in with the user's own TqKq account (phone / e-mail / username
    and its password); the invite code is only needed on the first login."""
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=400)
    inviteCode: str = Field(default="", max_length=200)


class ModelPreferenceRequest(BaseModel):
    model: str = Field(min_length=1, max_length=80)


class AdminUserUpdateRequest(BaseModel):
    """M2: the owner moves a cloud user onto any existing tier or flips their
    disable flag; both fields optional so one call may change only one."""
    plan: str | None = None
    disabled: bool | None = None


class InviteCodeRequest(BaseModel):
    """An owner-generated invite code: how many days it stays valid (1–90,
    ignored when `neverExpires`), the tier the first login through it lands on
    (empty = the default tier), an optional custom code (4–32 chars of
    letters/digits/-/_ , case-sensitive; blank = a random one), and whether it
    admits one use or unlimited uses. Body-less legacy callers keep the old
    shape: random one-shot code, 14 days, default tier."""
    daysValid: int = Field(default=14, ge=1, le=90)
    plan: str | None = Field(default=None, max_length=80)
    code: str | None = Field(default=None, max_length=32)
    uses: Literal["once", "unlimited"] = "once"
    neverExpires: bool = False


class AdminPlanCreateRequest(BaseModel):
    """M2: a new tier — its display name, and the existing tier to copy."""
    label: str = Field(min_length=1, max_length=40)
    copyFrom: str | None = Field(default=None, max_length=80)


class AdminPlanUpdateRequest(BaseModel):
    """M2: one tier's partial update. `models` is the exact allowed model set
    (null = every configured model); the numeric fields are per-day caps
    (null = unlimited). `skills` is a partial map (registry skill id → on/off):
    ids present flip to their value, ids absent keep the tier's current state.
    `features` is the 1.6.x spelling of the same edit — camel keys, each
    translated through the legacy feature→skills map."""
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, min_length=1, max_length=40)
    models: list[str] | None = None
    dailyTokens: int | None = Field(default=None, ge=0, le=1_000_000_000)
    voiceSecondsPerDay: int | None = Field(default=None, ge=0, le=10_000_000)
    marketHistoryTradingDays: int | None = Field(default=None, ge=1, le=5, strict=True)
    # AI 简报订阅品种上限 (2026-10-05); null = 不限.
    briefProducts: int | None = Field(default=None, ge=0, le=50)
    skills: dict[str, bool] | None = None
    features: dict[str, bool] | None = None
    isDefault: bool | None = None


class AdminSkillsUpdateRequest(BaseModel):
    """Several skills' global switches in one call — a partial map, ids absent
    keep their state. Owner-only surface; the switch binds everyone."""
    model_config = ConfigDict(extra="forbid")
    skills: dict[str, bool]


class AdminSkillUpdateRequest(BaseModel):
    """One skill's global switch."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool


MARKET_SOURCES_MAX = 16  # the longest ordered source list the admin can save


class AdminMarketSourcesRequest(BaseModel):
    """公共行情多账号接力 (2026-10-04): the whole ordered source list in one PUT —
    the position in the list is the fill order. Empty is a deliberate stop (the
    fake/ refusal path); duplicates are refused. Only existing active
    `tqkq-pool` / `tqkq-sidecar` accounts qualify, checked by the route."""
    model_config = ConfigDict(extra="forbid")
    accountIds: list[str] = Field(max_length=MARKET_SOURCES_MAX)


class AdminMarketSourceAccountRequest(BaseModel):
    """行情专用快期账户 (2026-10-12): the TqKq login the owner types into 后台「行情源」.
    No length constraints here — a schema refusal would echo the typed value in the
    422 body; the route checks the lengths and answers with a code only. SecretStr
    keeps the password out of every repr and log line."""
    model_config = ConfigDict(extra="forbid")
    username: str
    password: SecretStr


class AdminPaperResetRequest(BaseModel):
    """重置本站模拟账户 (#1204): the account id typed again, so a reset is never one stray call."""
    model_config = ConfigDict(extra="forbid")
    confirmAccountId: str = Field(max_length=200)


class BacktestRequest(BaseModel):
    sessionId: str
    instruction: str = "把成交量阈值改成 90%，回测最近三年。"


class ReproducibleBacktestRequest(BaseModel):
    sessionId: str
    requestId: str | None = Field(default=None, min_length=8, max_length=200)
    strategyPath: str = Field(default="workspace/strategies/rb_trend.py", min_length=1, max_length=500)
    dataPath: str = Field(default="workspace/backtests/fixtures/rb_monthly_v1.csv", min_length=1, max_length=500)
    baselineThreshold: str = Field(default="1.00", pattern=r"^\d+(?:\.\d+)?$")
    candidateThreshold: str = Field(default="0.90", pattern=r"^\d+(?:\.\d+)?$")


class TqSdkBacktestRequest(BaseModel):
    sessionId: str = Field(min_length=1, max_length=200)
    requestId: str | None = Field(default=None, min_length=8, max_length=200)
    symbol: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    startDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    endDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    durationSeconds: Literal[300, 900, 3600, 86400] = 3600
    baselineThreshold: str = Field(default="1.00", pattern=r"^\d+(?:\.\d+)?$")
    candidateThreshold: str = Field(default="0.90", pattern=r"^\d+(?:\.\d+)?$")
    initialBalance: float = Field(default=1_000_000, ge=100_000, le=100_000_000)


class StrategyMonitorStartRequest(BaseModel):
    """启动 (or, while monitoring, change) an instance's 执行方式 — alert 只提醒 / confirm 出卡确认 /
    auto 自动下单 (auto needs 设置 → 交易 → AI 自主执行 on) — and the chat its reminders and cards
    go to. Omitted fields keep the instance's stored values (a new instance: auto, latest chat).
    resetDrawdown (回撤停机, 2026-10-09): only true starts a fresh drawdown round (new baseline and
    peak, once the triggered round's close/cancel confirmations are done); a plain start resumes
    the same round — baseline and peak stay, and a still-breached limit re-triggers at once."""
    model_config = ConfigDict(extra="forbid")
    signalAction: Literal["alert", "confirm", "auto"] | None = None
    sessionId: str | None = Field(default=None, min_length=1, max_length=200)
    resetDrawdown: bool = False


class StrategyMonitorStopRequest(BaseModel):
    """停止一个实例。closePositions（停止并平仓，2026-10-08）：停止后若这个策略还有持仓，在它的对话里
    出平仓卡等用户确认——从不直接下单，自动下单的实例也一样；sessionId 指定出卡的对话（须是本人的）。"""
    model_config = ConfigDict(extra="forbid")
    closePositions: bool = False
    sessionId: str | None = Field(default=None, min_length=1, max_length=200)


class ConfirmRequest(BaseModel):
    approvalId: str = Field(min_length=1)
    proposalVersion: int = Field(ge=1)
    idempotencyKey: str = Field(min_length=8, max_length=200)


class MutationRequest(BaseModel):
    proposalVersion: int = Field(ge=1)
    idempotencyKey: str = Field(min_length=8, max_length=200)


class RemainingProtectionRequest(BaseModel):
    """合并卡第二步：剩余仓位的止损/止盈。键给了价＝改到这个价，给 null＝撤掉这一项，不给＝原样。"""
    model_config = ConfigDict(extra="forbid")
    stopLoss: float | None = Field(default=None, gt=0)
    takeProfit: float | None = Field(default=None, gt=0)


class TradeProposalRequest(BaseModel):
    sessionId: str = Field(min_length=1, max_length=200)
    # 外盘图表的下单票送快期外盘主连代码（KQD.m@COMEX.GC）：规划器把它换成美元子账户的主力月份（外盘二期）。
    symbol: str = Field(min_length=3, max_length=80, pattern=CONTEXT_SYMBOL_PATTERN)
    side: Literal["buy_open", "sell_open", "buy_close", "sell_close"]
    # 上期所/能源中心平仓不给（或给 all）= 按“平今平昨”偏好自动分配，跨今昨时出一张批量平仓卡（2026-10-16 规划器）。
    positionBucket: Literal["today", "historical", "all"] | None = None
    volume: int = Field(ge=1)
    # 不给 = 统一默认：平仓对价，开仓按账号的默认价格方式（2026-10-16 起；此前默认 passive）。
    priceIntent: Literal["passive", "aggressive", "last"] | None = None
    limitPrice: float | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=500)
    # 开仓顺带保护（可只填其一）；平仓请求带了会被拒绝。
    protection: TradeProtectionRequest | None = None
    # 合并卡 (第四批)：平仓后剩余仓位的止损/止盈（给价=改到这个价，null=撤掉，不传=不动）；只有平仓带。
    remainingProtection: RemainingProtectionRequest | None = None
    # 打法本 (2026-10-25)：这一单按哪个打法下（GET /api/playbooks 的 id）；记在卡上，复盘里这笔开平就带上这个打法。
    playbookId: str | None = Field(default=None, min_length=1, max_length=64)


class CloseBatchProposalRequest(BaseModel):
    """Close every open position (or the named symbols / one direction) behind one confirmation."""
    sessionId: str = Field(min_length=1, max_length=200)
    symbols: list[str] | None = Field(default=None, max_length=50)
    direction: Literal["long", "short"] | None = None
    reason: str = Field(min_length=1, max_length=500)


class TargetPositionProposalRequest(BaseModel):
    """One wanted net position; the Runtime plans the difference and issues only the first leg's
    confirmation card (unchanged honestly says so without a card)."""
    sessionId: str = Field(min_length=1, max_length=200)
    symbol: str = Field(min_length=3, max_length=80, pattern=CONTEXT_SYMBOL_PATTERN)
    targetNet: int = Field(ge=-1000, le=1000)
    priceIntent: Literal["passive", "aggressive", "last"] | None = None
    reason: str = Field(min_length=1, max_length=500)


class VoicePreferenceRequest(BaseModel):
    voice: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_]+$")


ExecutionPermission = Literal["open", "cancel", "close", "stop"]
ALL_EXECUTION_PERMISSIONS: list[ExecutionPermission] = ["open", "cancel", "close", "stop"]


class AiExecutionStartRequest(BaseModel):
    sessionId: str = Field(min_length=1, max_length=200)
    symbols: list[str] = Field(min_length=1, max_length=20)
    permissions: list[ExecutionPermission] = Field(
        default_factory=lambda: list(ALL_EXECUTION_PERMISSIONS),
        min_length=1,
        max_length=4,
    )
    maxOrderVolume: int | None = Field(default=None, ge=1)
    maxOrders: int | None = Field(default=None, ge=1)
    durationMinutes: int | None = Field(default=None, ge=5)


class AiExecutionResumeRequest(BaseModel):
    sessionId: str = Field(min_length=1, max_length=200)
    symbol: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.@-]+$")


class AiExecutionStopRequest(BaseModel):
    emergency: bool = False
    scope: Literal["all", "opens"] = "all"


class AiExecutionLimitsRequest(BaseModel):
    revision: int = Field(ge=0)
    maxOrderVolume: int | None = Field(default=None, ge=1, le=2147483647)
    maxOrders: int | None = Field(default=None, ge=1, le=2147483647)


class TradeProtectionRequest(BaseModel):
    """开仓时顺带挂的保护价（至少一个）：确认成交后按实际成交量自动挂上，撤单/未成交不挂。trailing 让止损按最新价
    只收紧地追踪；breakevenAtR 浮盈到 1R（开仓价到止损的距离）后把止损移到开仓价。两者都要有止损价。"""
    stopLossPrice: float | None = Field(default=None, gt=0)
    takeProfitPrice: float | None = Field(default=None, gt=0)
    trailing: ProtectionTrailing | None = None
    breakevenAtR: bool | None = None


class AiStopLossRequest(BaseModel):
    sessionId: str = Field(min_length=1, max_length=200)
    symbol: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    positionDirection: Literal["long", "short"]
    positionBucket: Literal["today", "historical", "all"]
    volume: int = Field(ge=1)
    # 止损 / 止盈至少一个；方向错了 Runtime 以中文原因拒绝（多仓 止损<现价<止盈）。
    triggerPrice: float | None = Field(default=None, gt=0)
    takeProfitPrice: float | None = Field(default=None, gt=0)
    priceIntent: Literal["passive", "aggressive", "last"] = "aggressive"
    # 追踪止损 / 浮盈 1R 后移到成本（2026-10-25）：同 TradeProtectionRequest，从这笔持仓的开仓均价算起。
    trailing: ProtectionTrailing | None = None
    breakevenAtR: bool | None = None


class AiStopRepriceRequest(BaseModel):
    """图上拖线改价：一条保护的止损或止盈改到新价位；previous 是拖动起点（被别处改过就拒绝）。"""
    level: Literal["stop", "take"]
    previous: float = Field(gt=0)
    price: float = Field(gt=0)

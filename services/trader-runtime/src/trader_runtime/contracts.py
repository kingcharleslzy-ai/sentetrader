from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEMA_VERSION = 1
KINDS = {"message", "progress", "tool_call", "artifact", "chart", "diff", "approval", "result", "warning", "error"}


class Payload(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)


class SessionReady(Payload):
    mode: Literal["local", "cloud"]
    agentId: Literal["native", "claude", "kimi", "dsh", "workbuddy"]


class RuntimeStatus(Payload):
    mode: Literal["local", "cloud"]
    adapter: Literal["fake-agent", "native-agent", "historical-replay"]
    accountMode: Literal["fake", "tqkq-readonly", "paper"]
    runtimeConnected: bool
    agentConnected: bool
    # backup (2026-10-07): TqKq is down and the shared market shows Sina quotes —
    # for watching only; every order/stop/alert gate treats it as unavailable.
    marketDataStatus: Literal["simulated", "connecting", "normal", "stale", "unavailable", "backup"]
    marketDataSource: str | None = None
    marketSessionStatus: Literal["trading", "closed", "unknown"] | None = None
    accountStatus: Literal["simulated", "connecting", "normal", "stale", "unavailable"]
    tradingGatewayStatus: Literal["disabled", "read_only", "simulation", "unavailable"]
    simulationTrading: bool = False
    riskStatus: Literal["normal", "warning", "unknown"]
    dataErrorCode: str | None = None
    dataConnectionStage: Literal[
        "starting", "loading_sdk", "authenticating", "subscribing", "discovering_instruments", "waiting_snapshot",
        "waiting_account", "waiting_market", "reconnecting", "ready", "failed",
    ] | None = None
    dataInitializationSeconds: float | None = Field(default=None, ge=0)
    dataStartupTimings: dict[str, float] | None = None
    dataStartupTotalSeconds: float | None = Field(default=None, ge=0)
    agentNotice: str | None = None
    connected: bool
    version: str = Field(min_length=1)


class Message(Payload):
    turnId: str = Field(min_length=1)
    role: Literal["user", "assistant", "system"]
    text: str
    agentId: Literal["native", "claude", "kimi", "dsh", "workbuddy"] | None = None
    # A streamed answer that did not finish: the turn is closed with this marker so the client
    # unblocks the conversation and history does not treat the stub as an answer.
    ended: Literal["cancelled", "failed"] | None = None


class MessageSuperseded(Payload):
    """续句合并：被并走的半句 user 消息由这个事件标记，客户端隐藏该轮（数据不删）。"""
    turnId: str = Field(min_length=1)
    supersededBy: str = Field(min_length=1)


class TaskStep(Payload):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    status: Literal["waiting", "active", "done", "failed"]
    detail: str


class AgentTask(Payload):
    turnId: str = Field(min_length=1)
    taskId: str = Field(min_length=1)
    title: str = Field(min_length=1)
    subtitle: str
    progress: int = Field(ge=0, le=100)
    status: Literal["running", "completed", "failed", "cancelled"]
    steps: list[TaskStep]


class AccountSummary(Payload):
    turnId: str = Field(min_length=1)
    accountName: str = Field(min_length=1)
    updatedAt: str = Field(min_length=1)
    capturedAt: str = Field(min_length=1)
    ageSeconds: float = Field(ge=0)
    dataStatus: Literal["simulated", "normal", "stale", "unavailable"]
    isCached: bool = False
    equity: float
    todayPnl: float
    todayNetPnl: float
    floatProfit: float
    positionProfit: float
    closeProfit: float
    commission: float
    marginRatio: float = Field(ge=0)
    # 剩余可用资金与保证金占用（元）、风险度＝保证金占用 ÷ 权益（百分数）。
    available: float = 0
    margin: float = 0
    riskRatio: float = Field(default=0, ge=0)
    positionCount: int = Field(ge=0)
    # 本站模拟挂起中的到期结算（#527）：每项 symbol / finalDay / status / statusLabel / since，
    # 暂估、正式价与补差各带 estimate / official / adjustment；快期账户为空。
    expirySettlements: list[dict[str, Any]] = Field(default_factory=list)
    riskBudgetUsed: float = Field(ge=0)
    status: Literal["normal", "warning"]
    insight: str
    equityCurve: list[float]
    # 回答卡片化 (2026-10-14): the turn's one account card; a second read replaces it in place.
    cardId: str | None = None


class MarketPeer(Payload):
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    price: float
    changePercent: float
    # stale / backup when this contract's own read was not current (the main quote's badge covers only itself).
    dataStatus: Literal["stale", "backup"] | None = None


class MarketSnapshot(Payload):
    turnId: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    price: float
    changePercent: float
    updatedAt: str = Field(min_length=1)
    capturedAt: str = Field(min_length=1)
    ageSeconds: float = Field(ge=0)
    # backup: Sina's quote while TqKq is down (sina_market) — shown, never traded on.
    dataStatus: Literal["simulated", "normal", "stale", "backup"]
    source: str | None = None
    trend: Literal["up", "down", "flat"]
    summary: str
    priceCurve: list[float]
    # 每手保证金/手续费（元，交易所标准参考；缺数据时为 None）。
    marginPerLot: float | None = Field(default=None, gt=0)
    commissionPerLot: float | None = Field(default=None, gt=0)
    # 合约信息卡 (2026-10-02)：交易时段 {"day": [["09:00","10:15"],…], "night": […]}（缺为 None）、
    # 一手/资金摘要与交割提醒（deliveryNotice 无提醒时为 None）。共享行情源直接带；同步
    # 提供方（fake/回放）的字段由 trader_tools 读目录补上。
    tradingSchedule: dict | None = None
    contract: dict | None = None
    deliveryNotice: dict | None = None
    # 回答卡片化 (2026-10-14): the turn's one quote card; other contracts read in the same turn ride as peers.
    cardId: str | None = None
    peers: list[MarketPeer] | None = Field(default=None, max_length=6)


class StrategyCondition(Payload):
    label: str = Field(min_length=1)
    status: Literal["passed", "waiting", "failed"]
    detail: str


class StrategyRiskLimits(Payload):
    maxPosition: int = Field(ge=1)
    maxOrdersPerDay: int = Field(ge=1)
    stopLossTicks: int = Field(ge=1, le=5000)
    takeProfitTicks: int | None = Field(ge=1, le=10000)
    cooldownBars: int = Field(ge=0, le=100)


class StrategySignalSnapshot(Payload):
    signal: Literal["buy", "sell", "wait"]
    barTime: str = Field(min_length=1)
    barState: Literal["completed"]
    dataStatus: Literal["normal", "simulated"]
    definitionDigest: str = Field(pattern=r"^[0-9a-f]{64}$")


class StrategyPositionIntent(Payload):
    action: Literal[
        "hold", "open_long", "open_short", "close_long", "close_short",
        "reduce_long", "reduce_short",
    ]
    requestedVolume: int = Field(ge=0)
    currentLong: int = Field(ge=0)
    currentShort: int = Field(ge=0)
    targetLong: int = Field(ge=0)
    targetShort: int = Field(ge=0)
    reason: str


class StrategyIntentCheck(Payload):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    passed: bool


class StrategyRiskAdjustedIntent(Payload):
    status: Literal["no_action", "allowed", "blocked", "reduce_only"]
    action: Literal[
        "hold", "open_long", "open_short", "close_long", "close_short",
        "reduce_long", "reduce_short",
    ]
    volume: int = Field(ge=0)
    stopPrice: float | None = None
    takeProfitPrice: float | None = None
    checks: list[StrategyIntentCheck]
    riskCheck: dict[str, Any] | None = None
    reason: str
    executionEligible: Literal[False]
    liveTrading: Literal["disabled"]


class StrategyExplanation(Payload):
    turnId: str = Field(min_length=1)
    strategyId: str = Field(min_length=1)
    name: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    version: str = Field(min_length=1)
    status: Literal[
        "draft", "validating", "ready", "monitoring", "waiting", "running", "paused", "archived"
    ]
    summary: str
    conditions: list[StrategyCondition]
    template: Literal["ma_cross", "breakout", "kd_cross", "macd_cross", "combined"] | None = None
    timeframeSeconds: Literal[60, 180, 300, 900, 3600, 86400] | None = None
    direction: Literal["long", "short", "both"] | None = None
    riskLimits: StrategyRiskLimits | None = None
    definitionDigest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    backtestStatus: Literal["not_run", "passed", "failed"] | None = None
    executionMode: Literal["simulation_only"] | None = None
    createdAt: str | None = None
    validationReportId: str | None = None
    validationStatus: Literal["incomplete", "passed", "failed"] | None = None
    intentId: str | None = None
    signalSnapshot: StrategySignalSnapshot | None = None
    positionIntent: StrategyPositionIntent | None = None
    riskAdjustedIntent: StrategyRiskAdjustedIntent | None = None


class BacktestMetric(Payload):
    label: str = Field(min_length=1)
    before: str
    after: str
    tone: Literal["positive", "warning", "neutral"]


class BacktestCandidateParameters(Payload):
    baselineThreshold: str = Field(pattern=r"^\d+(?:\.\d+)?$")
    candidateThreshold: str = Field(pattern=r"^\d+(?:\.\d+)?$")


class BacktestComparison(Payload):
    turnId: str = Field(min_length=1)
    taskId: str = Field(min_length=1)
    backtestId: str = Field(min_length=1)
    artifactDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategyDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    engineDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategyReleaseId: str = Field(min_length=1)
    candidateVariantId: str = Field(pattern=r"^sha256-[0-9a-f]{64}$")
    candidateConfigDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidateParameters: BacktestCandidateParameters
    engineVersion: str = Field(min_length=1)
    dataSource: str = Field(min_length=1)
    generatedAt: str = Field(min_length=1)
    reproducible: Literal[True]
    sourceKind: Literal["synthetic_fixture", "tqsdk_history"] = "synthetic_fixture"
    dataPointCount: int | None = Field(default=None, ge=1)
    strategyName: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    oldVersion: str = Field(min_length=1)
    newVersion: str = Field(min_length=1)
    period: str = Field(min_length=1)
    metrics: list[BacktestMetric]
    oldCurve: list[float]
    newCurve: list[float]
    conclusion: str
    recommendation: str


class StrategyBacktestResult(Payload):
    """规则回测结果卡（策略积木·第一期 B）：用户自己的规则按收盘确认、下一根开盘成交回放。"""
    turnId: str = Field(min_length=1)
    taskId: str = Field(min_length=1)
    backtestId: str = Field(min_length=1)
    kind: Literal["rule"]
    strategyName: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    timeframeSeconds: int = Field(gt=0)
    summary: dict[str, Any]
    exitReasons: list[dict[str, Any]]
    equityCurve: list[dict[str, Any]] = Field(max_length=400)
    trades: list[dict[str, Any]] = Field(max_length=200)
    tradesTotal: int = Field(ge=0)
    coverage: dict[str, Any]
    assumptions: list[str]


class RiskCheck(Payload):
    passed: bool
    limit: float = Field(ge=0)
    estimatedLoss: float = Field(ge=0)
    reason: str
    contractMultiplier: float = Field(gt=0)
    priceTick: str = Field(pattern=r"^\d+(?:\.\d+)?$")
    policyVersion: int = Field(default=0, ge=0)
    checks: dict[str, bool] = Field(default_factory=dict)


class TradeProposal(Payload):
    turnId: str = Field(min_length=1)
    proposalId: str = Field(min_length=1)
    version: int = Field(ge=1)
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    side: Literal["buy_open", "sell_open", "buy_close", "sell_close"]
    offset: Literal["OPEN", "CLOSE", "CLOSETODAY"] = "OPEN"
    positionBucket: Literal["today", "historical", "all"] | None = None
    volume: int = Field(gt=0)
    limitPrice: float = Field(gt=0)
    stopPrice: float | None = Field(default=None, gt=0)
    contractMultiplier: float = Field(gt=0)
    updatedAt: str = Field(min_length=1)
    environment: Literal["simulation"]
    status: Literal["draft", "risk_blocked", "approval_required", "candidate_recorded", "rejected"]
    riskCheck: RiskCheck
    approvalId: str | None = None
    auditId: str | None = None


class CloseBatchOrder(Payload):
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    side: Literal["buy_close", "sell_close"]
    offset: Literal["CLOSE", "CLOSETODAY"]
    positionBucket: Literal["today", "historical", "all"]
    volume: int = Field(gt=0)
    limitPrice: float = Field(gt=0)
    contractMultiplier: float = Field(gt=0)
    riskCheck: RiskCheck
    clientOrderId: str | None = None


class CloseBatchProposal(Payload):
    """One card closing every listed position bucket; one confirmation sends them all, one by one."""
    turnId: str = Field(min_length=1)
    proposalId: str = Field(min_length=1)
    version: int = Field(ge=1)
    environment: Literal["simulation"]
    reason: str
    orders: list[CloseBatchOrder] = Field(min_length=1)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    priceIntent: Literal["aggressive", "passive", "last"] | None = None
    userLimitPrice: bool = False
    totalOrders: int = Field(ge=1)
    totalVolume: int = Field(ge=1)
    riskCheck: dict[str, Any]
    updatedAt: str = Field(min_length=1)
    status: Literal["draft", "risk_blocked", "approval_required", "candidate_recorded", "rejected"]
    approvalId: str | None = None
    auditId: str | None = None
    voiceConfirmBy: str | None = None


class SpreadLegOrder(Payload):
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    side: Literal["buy_open", "sell_open", "buy_close", "sell_close"]
    offset: Literal["OPEN", "CLOSE", "CLOSETODAY"]
    positionBucket: Literal["today", "historical", "all"] | None = None
    volume: int = Field(gt=0)
    limitPrice: float = Field(gt=0)
    contractMultiplier: float = Field(gt=0)
    riskCheck: RiskCheck
    clientOrderId: str | None = None


class SpreadProposal(Payload):
    """One card for both legs of one spread action; one confirmation dispatches the two legs
    together at the opponent's price and the card reports each leg's real result."""
    turnId: str = Field(min_length=1)
    proposalId: str = Field(min_length=1)
    version: int = Field(ge=1)
    environment: Literal["simulation"]
    reason: str
    spread: dict[str, Any]
    spreadWords: str = Field(min_length=1)
    legsWords: str = Field(min_length=1)
    action: Literal["open_long", "open_short", "close_long", "close_short"]
    units: int = Field(ge=1)
    currentSpread: float | None = None
    entrySpread: float | None = None
    exitReason: Literal["spread_stop", "spread_take"] | None = None
    orders: list[SpreadLegOrder] = Field(min_length=1)
    totalOrders: int = Field(ge=1)
    totalVolume: int = Field(ge=1)
    riskCheck: dict[str, Any]
    updatedAt: str = Field(min_length=1)
    status: Literal["draft", "risk_blocked", "approval_required", "candidate_recorded", "rejected"]
    approvalId: str | None = None
    auditId: str | None = None
    voiceConfirmBy: str | None = None
    strategySource: dict[str, Any] | None = None


class RiskRuleInstrument(Payload):
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)


class RiskRuleProposal(Payload):
    turnId: str = Field(min_length=1)
    proposalId: str = Field(min_length=1)
    version: int = Field(ge=1)
    basePolicyVersion: int = Field(ge=0)
    policyVersion: int = Field(ge=1)
    allowedInstruments: list[RiskRuleInstrument] = Field(min_length=1)
    maxLossPerTrade: str = Field(pattern=r"^\d+(?:\.\d{1,2})?$")
    maxDailyLoss: str = Field(pattern=r"^\d+(?:\.\d{1,2})?$")
    noNewPositionsAfter: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: Literal["Asia/Shanghai"]
    rulesDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    deterministic: Literal[True]
    executionEnabled: Literal[False]
    status: Literal["approval_required", "risk_rules_applied", "rejected"]
    approvalId: str = Field(min_length=1)
    policyId: str | None = None
    auditId: str | None = None


class ProposalApproval(Payload):
    proposalId: str = Field(min_length=1)
    approvalId: str = Field(min_length=1)
    proposalVersion: int = Field(ge=1)
    proposalType: Literal["trade", "risk_rule", "cancel"]


class ProposalResult(Payload):
    proposalId: str = Field(min_length=1)
    status: Literal["candidate_recorded", "risk_rules_applied", "rejected"]
    auditId: str = Field(min_length=1)
    message: str
    # Why a card closed without the user's 放弃 (#932 P2): its confirm window ran out, or a newer plan replaced it.
    reason: Literal["expired", "superseded"] | None = None


class RiskWarning(Payload):
    turnId: str = Field(min_length=1)
    title: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: Literal["warning", "danger"]
    deterministic: bool


class ErrorPayload(Payload):
    turnId: str | None = None
    runId: str | None = None
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    scope: Literal["connection", "agent", "task", "validation", "trading"] = "task"


class AgentToolActivity(Payload):
    turnId: str = Field(min_length=1)
    taskId: str = Field(min_length=1)
    itemId: str = Field(min_length=1)
    toolType: Literal["command", "function"]
    status: Literal["running", "completed", "failed"]
    command: str | None = None
    cwd: str | None = None
    exitCode: int | None = None
    outputSummary: str | None = None


class AgentFileChange(Payload):
    path: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    diff: str


class AgentDiff(Payload):
    turnId: str = Field(min_length=1)
    taskId: str = Field(min_length=1)
    itemId: str = Field(min_length=1)
    status: Literal["running", "completed", "failed"]
    changes: list[AgentFileChange]


class AgentResult(Payload):
    turnId: str = Field(min_length=1)
    taskId: str = Field(min_length=1)
    threadId: str = Field(min_length=1)
    status: Literal["completed", "failed", "cancelled"]


class FeedPayload(Payload):
    """Feed payloads mirror a REST response verbatim; only their anchors are pinned."""


class FeedForeignAccount(FeedPayload):
    accountId: str = Field(min_length=1)
    account: dict[str, Any]
    positions: list[dict[str, Any]]
    positionsStatus: dict[str, Any] | None = None
    orders: list[dict[str, Any]]


class FeedAccount(FeedPayload):
    account: dict[str, Any]
    positions: list[dict[str, Any]]
    activity: dict[str, Any]
    risk: dict[str, Any]
    foreign: FeedForeignAccount | None = None


class BarsWindow(Payload):
    """The window a `since` answer proves (#816): its first bar, its bar count and the digest of the
    bars up to and including `barsAfter` — the client merges its held bars only when all three match."""
    first: int
    count: int = Field(ge=1)
    digest: str = Field(min_length=1)


class FeedMarket(FeedPayload):
    schemaVersion: Literal[1]
    durationSeconds: Literal[60, 180, 300, 900, 1800, 3600, 86400, 604800]
    quote: dict[str, Any]
    bars: list[dict[str, Any]]
    liveTrading: Literal["disabled"]
    # A focus `since` answer (#816, contract #850): only the bars after `barsAfter`, with the window proof.
    barsAfter: int | None = None
    barsWindow: BarsWindow | None = None

    @model_validator(mode="after")
    def _since_answer_is_whole(self) -> FeedMarket:
        if (self.barsAfter is None) != (self.barsWindow is None):
            raise ValueError("barsAfter and barsWindow come together")
        return self


class FeedMarketPatch(Payload):
    """The shared market changed since the full feed.market `base` this session holds:
    changed scalars (the full quote when it changed) and `barsPatch` {bars, trimBefore}.
    Sent only to sessions that asked for patches (PUT .../feed marketPatches); a client
    whose revision is not `base` asks for a full frame again instead of applying it."""
    revision: str = Field(min_length=1)
    base: str = Field(min_length=1)
    durationSeconds: Literal[60, 180, 300, 900, 1800, 3600, 86400, 604800]
    changes: dict[str, Any]


class FeedBoard(FeedPayload):
    """报价总表 pushed (2026-10-12): the board rows the session's window shows (PUT .../feed/view
    `board`), the same row shape GET /api/market/board serves, at board revision `feedRevision`.
    `key` echoes the client's name for its row filter, so it knows the push covers what it asked."""
    feedRevision: str = Field(min_length=1)
    key: str
    rows: list[dict[str, Any]]


class FeedBoardPatch(Payload):
    """What changed on the board since revision `base` the session holds in full: changed fields by
    symbol (`rows`, only rows its filter shows) and changed meta, always with the capture stamps.
    `base == feedRevision` is a refresh with nothing changed. A client holding another revision
    sends its view again for a full feed.board instead of applying it."""
    feedRevision: str = Field(min_length=1)
    base: str = Field(min_length=1)
    key: str
    meta: dict[str, Any]
    rows: dict[str, dict[str, Any]]


class FeedTouch(Payload):
    """The channel's last full frame is still current: only its capture stamps moved (2026-09-28).
    `stamps` = [[path, value], ...] for every capturedAt / ageSeconds in that frame. Sent instead of
    the unchanged frame to sessions that asked for it (PUT .../feed feedTouch)."""
    name: Literal["feed.account", "feed.activity", "feed.execution"]
    stamps: list[list[Any]]


class FeedActivity(FeedPayload):
    environment: Literal["replay", "simulation"]
    symbol: str
    orders: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    strategies: list[dict[str, Any]]


class FeedExecution(FeedPayload):
    stops: list[dict[str, Any]]
    simulationOnly: Literal[True]
    liveTrading: Literal["disabled"]


class FeedOrder(FeedPayload):
    """A watched simulation order's read-back changed (2026-10-08): the session whose card was
    confirmed hears the new fact, so a confirmed card's rows and the orders screen follow the
    order live. The payload is the same view /api/simulation-orders serves, pushed once per
    change by the order watch — never a re-send of the order itself (TQKQ_SIM_THIN_V1)."""
    clientOrderId: str = Field(min_length=1)
    accountId: str = Field(min_length=1)
    displayStatus: str = Field(min_length=1)


class FeedUiNudge(Payload):
    reason: str = Field(min_length=1)


class FeedDebate(Payload):
    """多 AI 研讨 (2026-10-24): one turn's (turn, side) or one analyst's report (analyst) text so far while the model
    writes it — the whole text each time, so a dropped frame costs nothing; when it ends a feed.ui nudge
    (research-debate) brings its checked version."""
    id: str = Field(min_length=1)
    turn: int | None = Field(default=None, ge=1, le=4)
    side: Literal["bull", "bear"] | None = None
    analyst: Literal["technical", "supply", "flow", "news"] | None = None
    text: str


class FeedStatus(Payload):
    """One feed channel cannot be read right now. Sent on the channel that failed so
    the client falls back to polling that reader alone, with the provider's reason."""
    channel: Literal["account", "market", "activity", "execution", "board"]
    status: Literal["unavailable"]
    reason: str = Field(min_length=1)


class FeedStrategySignal(FeedPayload):
    """A monitoring strategy's signal changed (观望 → 开/平): the same read
    explain_strategy_signal produces, pushed so the client can draw the numbered
    annotations and show the spoken line without being asked."""
    strategyId: str = Field(min_length=1)
    name: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    action: Literal["open_long", "open_short", "close_long", "close_short"]
    actionLabel: str = Field(min_length=1)
    volume: int = Field(ge=0)
    barTime: int | None
    evaluatedBarTime: str | None
    validated: Literal[True]
    annotations: list[dict[str, Any]]
    spoken: str = Field(min_length=1)
    executionMode: Literal["simulation_only"]


class FeedPriceAlert(FeedPayload):
    """A price alert crossed (2026-10-02): every subscribed window of the user's shows the
    desktop notification and sound, and drops the alert's chart line and list row."""
    id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    direction: Literal["above", "below"]
    price: str = Field(min_length=1)
    lastPrice: float | None
    note: str
    text: str = Field(min_length=1)
    triggeredAt: str | None


class FeedReminder(FeedPayload):
    """统一提醒 (#932)：Runtime 已按该类提醒自己的开关把关，提醒落库后该用户每个订阅窗口一条——
    客户端重读提醒中心（GET /api/reminders）并按 title/text 弹桌面通知与界内提示条；type=update
    是已有提醒的状态变化（平仓单已成交、保本已移），不弹通知。"""
    kind: str = Field(min_length=1)
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    at: str = Field(min_length=1)
    # The reminders row (提醒中心) and whether this is a new reminder or a changed one.
    id: str | None = None
    type: Literal["new", "update"] | None = None
    # The user's unread reminders of the current trading day after this one (the bell's badge).
    unread: int | None = Field(default=None, ge=0)
    # #457: the account a reminder belongs to (absent for an account-less kind).
    accountId: str | None = None
    accountName: str | None = None
    # 多 AI 研讨 (2026-10-11): kind research_debate names the debate that ended.
    debateId: str | None = None


class CardAction(Payload):
    """A button on a structured card (2026-10-04): `ask` sends `prompt` as the user's own next
    message (the normal conversation, no shortcut past it); `ui` applies `changes` — an existing
    UI command — in the window that shows the card. No action ever places or cancels an order.
    `breakeven` (保本提醒, 2026-10-06) answers that card's question through its own business route
    (POST /api/ai-execution/breakeven/{breakevenId}/move | decline) — the user's own click, never an order."""
    kind: Literal["ask", "ui", "breakeven"]
    label: str = Field(min_length=1, max_length=40)
    prompt: str | None = Field(default=None, max_length=200)
    changes: dict[str, Any] | None = None
    breakevenId: str | None = None
    choice: Literal["move", "decline"] | None = None


class OrderQuestionOption(Payload):
    label: str = Field(min_length=1, max_length=40)
    prompt: str = Field(min_length=1, max_length=200)


class OrderQuestionCard(Payload):
    """下单类回合的结构化结局之一 (2026-10-04)：`ask` = the order planner asks for what the order is missing (意图重构
    第 3 步：打字快速路径或 propose_order 返回的追问，选项是补全后的整句); `no_card` = 仍会在强制下单回合模型两次
    只回文字时发出（重试一次仍无出卡调用，Runtime 说明没能生成方案卡）。"""
    turnId: str = Field(min_length=1)
    cardId: str = Field(min_length=1)
    kind: Literal["ask", "no_card"]
    title: str = Field(min_length=1, max_length=60)
    question: str = Field(min_length=1, max_length=300)
    missing: list[str] = Field(default_factory=list, max_length=6)
    options: list[OrderQuestionOption] = Field(default_factory=list, max_length=4)
    reason: str | None = None
    userText: str | None = None
    actions: list[CardAction] = Field(default_factory=list, max_length=4)
    createdAt: str = Field(min_length=1)
    # The reply that answered an `ask` card (10-08): republished under the same cardId with
    # `answered` — the option's label when the reply was that option's prompt.
    answered: bool = False
    answeredAt: str | None = None
    answer: str | None = Field(default=None, max_length=40)


class JudgmentLine(Payload):
    text: str = Field(min_length=1)
    quote: str = ""
    source: str = ""
    asOf: str | None = None


class ResearchJudgmentCard(Payload):
    """判断卡 (2026-10-04)：the research desk's shared judgment of the product for today, attached
    by the Runtime to an opinion question before the model answers. `pending` is republished
    under the same cardId once the judgment is ready (or `unavailable` when it cannot be made)."""
    turnId: str = Field(min_length=1)
    cardId: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    productName: str = Field(min_length=1)
    status: Literal["pending", "ready", "unavailable"]
    stance: Literal["偏多", "偏空", "震荡", "不明"] | None = None
    confidence: Literal["低", "中", "高"] | None = None
    headline: str | None = None
    facts: list[JudgmentLine] = Field(default_factory=list, max_length=4)
    invalidation: list[JudgmentLine] = Field(default_factory=list, max_length=3)
    generatedAt: str | None = None
    researchDay: str | None = None
    note: str | None = None
    actions: list[CardAction] = Field(default_factory=list, max_length=4)


class CardFigure(Payload):
    label: str = Field(min_length=1, max_length=20)
    value: str = Field(min_length=1, max_length=60)
    tone: Literal["positive", "warning", "danger"] | None = None


class ReminderCard(Payload):
    """提醒卡 (2026-10-04)：到价、风险度、收盘前、交割、止损止盈触发、挂单未成交 — one card shape,
    `kind` picks icon and title. It rides with the reminder's message.completed under the same
    turnId (voice and older clients keep reading that text). Its actions only ask the AI or open
    a panel; a reminder card never carries an order button."""
    turnId: str = Field(min_length=1)
    cardId: str = Field(min_length=1)
    kind: Literal["price_alert", "risk_ratio", "market_close", "delivery", "protection", "order_unfilled",
                  "market_movement", "daily_review", "breakeven"]
    title: str = Field(min_length=1, max_length=40)
    summary: str = Field(min_length=1, max_length=300)
    figures: list[CardFigure] = Field(default_factory=list, max_length=8)
    symbol: str | None = None
    at: str = Field(min_length=1)
    actions: list[CardAction] = Field(default_factory=list, max_length=4)
    # 保本提醒 (kind breakeven): the one step the card proposes ({title, detail}) and the question's state
    # ({id, state: pending | moved | declined | failed, result}); republished under the same cardId.
    step: dict[str, Any] | None = None
    breakeven: dict[str, Any] | None = None
    note: str | None = None


class AnswerFigure(Payload):
    """回答卡 / 数据卡的一格 (2026-10-14)。`dir` 是市场方向（涨跌色偏好生效）。"""
    label: str = Field(min_length=1, max_length=12)
    value: str = Field(min_length=1, max_length=24)
    unit: str | None = Field(default=None, max_length=8)
    dir: Literal["up", "down"] | None = None


class AnswerSide(Payload):
    title: str = Field(min_length=1, max_length=20)
    lines: list[str] = Field(default_factory=list, max_length=4)


class AnswerCompare(Payload):
    left: AnswerSide
    right: AnswerSide


class AnswerBasis(Payload):
    """依据（默认折叠）：读取时间与这一轮读了什么——Runtime 按本回合的工具调用写，不由模型写。"""
    at: str = Field(min_length=1)
    sources: list[str] = Field(default_factory=list, max_length=8)


class AnswerCard(Payload):
    """回答卡 (2026-10-14)：分析、对比、“咋样 / 怎么看”一类问题的综合回答做成一张卡。模型经 answer_card
    给标题与嵌字段键的结论、数字格、对比、理由与风险；卡上读数由 Runtime 按本回合读数表渲染（键查不到的去掉），
    依据与按钮（看图表 / 问 AI 怎么办）由 Runtime 写。同一回合再调一次 = 同 cardId 原地替换。"""
    turnId: str = Field(min_length=1)
    cardId: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=24)
    conclusion: str = Field(min_length=1, max_length=80)
    figures: list[AnswerFigure] = Field(default_factory=list, max_length=6)
    compare: AnswerCompare | None = None
    reasons: list[str] = Field(default_factory=list, max_length=3)
    risk: str | None = Field(default=None, max_length=80)
    symbol: str | None = None
    basis: AnswerBasis
    actions: list[CardAction] = Field(default_factory=list, max_length=2)
    # A stale / backup read the answer rests on (数据陈旧 / 备用行情), shown on the card face.
    warning: str | None = Field(default=None, max_length=24)
    # 去重 (2026-10-14 跟进): cardIds of this turn's read cards (行情 / 账户 / 数据卡) shown before the answer card;
    # the client shows them inside the answer card's 依据 instead of beside it.
    folded: list[str] = Field(default_factory=list, max_length=12)


class DataCardRow(Payload):
    primary: str = Field(min_length=1, max_length=80)
    secondary: str | None = Field(default=None, max_length=120)
    value: str | None = Field(default=None, max_length=24)
    dir: Literal["up", "down"] | None = None
    tag: str | None = Field(default=None, max_length=12)


class DataCard(Payload):
    """数据卡 (2026-10-14)：只读工具的结构化结果直接成卡——持仓、挂单与保护、快讯、走势。由 Runtime 按工具
    结果确定性生成，不靠模型选择；同一回合同类读取合并到同一 cardId。报价总表为 quotes。"""
    turnId: str = Field(min_length=1)
    cardId: str = Field(min_length=1)
    kind: Literal["positions", "orders", "news", "analysis", "quotes"]
    title: str = Field(min_length=1, max_length=40)
    figures: list[AnswerFigure] = Field(default_factory=list, max_length=6)
    rows: list[DataCardRow] = Field(default_factory=list, max_length=12)
    note: str | None = Field(default=None, max_length=160)
    empty: str | None = Field(default=None, max_length=60)
    # A stale / backup read (数据陈旧 / 备用行情): shown on the card face, not only in the folded basis.
    warning: str | None = Field(default=None, max_length=12)
    symbol: str | None = None
    basis: AnswerBasis
    actions: list[CardAction] = Field(default_factory=list, max_length=2)


class ExecutionCard(Payload):
    """执行回报卡 (2026-10-04)：an order the AI (自主执行) or a strategy (自动下单) sent, or a stop
    the AI armed. `status` is read back from the account (the simulation order's own state);
    `unknown` is shown as unknown, never as success. Later fills / cancels republish the card
    under the same cardId."""
    turnId: str = Field(min_length=1)
    cardId: str = Field(min_length=1)
    source: Literal["ai", "strategy"]
    sourceLabel: str = Field(min_length=1, max_length=80)
    orderKind: Literal["order", "stop"]
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    sideLabel: str = Field(min_length=1, max_length=20)
    volume: int = Field(ge=0)
    price: str | None = None
    reason: str = ""
    status: Literal["submitted", "partial", "filled", "unfilled", "rejected", "unknown", "armed", "failed"]
    statusLabel: str = Field(min_length=1, max_length=20)
    filledVolume: int | None = Field(default=None, ge=0)
    clientOrderId: str | None = None
    detail: str | None = None
    updatedAt: str = Field(min_length=1)


RoutineStepKind = Literal["read", "draw", "backtest", "margin", "card", "alert", "watch", "review"]


class RoutineStepView(Payload):
    kind: RoutineStepKind
    kindLabel: str = Field(min_length=1, max_length=8)
    text: str = Field(min_length=1, max_length=60)


class RoutineCard(Payload):
    """流程卡 (我的流程, 2026-10-06): the steps the AI drafted from the user's words, each with its type.
    `draft` shows 保存 / 改一下 — only the user's 保存 (POST /api/routines {draftId}) writes the routine;
    `saved` is republished under the same cardId after it; `refused` (免确认下单、自动下单、改风险设置、
    自主执行开关) has no 保存 and says why."""
    turnId: str = Field(min_length=1)
    cardId: str = Field(min_length=1)
    draftId: str = Field(min_length=1)
    routineId: str | None = None
    mode: Literal["create", "update"]
    name: str = Field(min_length=1, max_length=12)
    steps: list[RoutineStepView] = Field(min_length=1, max_length=10)
    status: Literal["draft", "saved", "refused"]
    refusal: str | None = None
    savedAt: str | None = None
    note: str | None = None


class RoutineRunStep(RoutineStepView):
    status: Literal["waiting", "active", "done", "confirm", "failed", "skipped"]
    result: str | None = Field(default=None, max_length=120)


class RoutineRunCard(Payload):
    """进度卡 (我的流程, 2026-10-06): a routine running as one turn — every step's state (进行中 / 完成 /
    需你确认 / 失败 / 没做) and one result line, republished under the same cardId as it moves on. A
    `confirm` step made a card the user still has to confirm; a failed step stops the run."""
    turnId: str = Field(min_length=1)
    cardId: str = Field(min_length=1)
    routineId: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=12)
    status: Literal["running", "completed", "stopped", "cancelled"]
    steps: list[RoutineRunStep] = Field(min_length=1, max_length=10)
    startedAt: str = Field(min_length=1)
    finishedAt: str | None = None
    elapsedSeconds: float = Field(ge=0)
    confirmCount: int = Field(ge=0)


EVENT_MODELS: dict[str, tuple[str, type[Payload]]] = {
    "session.ready": ("result", SessionReady),
    "runtime.status": ("result", RuntimeStatus),
    "message.delta": ("message", Message),
    "message.completed": ("message", Message),
    "message.superseded": ("message", MessageSuperseded),
    "task.started": ("progress", AgentTask),
    "task.progress": ("progress", AgentTask),
    "task.completed": ("progress", AgentTask),
    "card.account": ("artifact", AccountSummary),
    "card.market": ("artifact", MarketSnapshot),
    "card.strategy": ("artifact", StrategyExplanation),
    "card.backtest": ("chart", BacktestComparison),
    "card.strategy_backtest": ("chart", StrategyBacktestResult),
    "card.order_question": ("artifact", OrderQuestionCard),
    "card.research_judgment": ("artifact", ResearchJudgmentCard),
    "card.reminder": ("artifact", ReminderCard),
    "card.execution": ("artifact", ExecutionCard),
    "card.answer": ("artifact", AnswerCard),
    "card.data": ("artifact", DataCard),
    "card.routine": ("artifact", RoutineCard),
    "card.routine_run": ("artifact", RoutineRunCard),
    "proposal.trade": ("approval", TradeProposal),
    "proposal.close_batch": ("approval", CloseBatchProposal),
    "proposal.spread": ("approval", SpreadProposal),
    "proposal.risk_rule": ("approval", RiskRuleProposal),
    "trade.approval.required": ("approval", ProposalApproval),
    "risk_rule.approval.required": ("approval", ProposalApproval),
    "proposal.result": ("result", ProposalResult),
    "agent.tool.started": ("tool_call", AgentToolActivity),
    "agent.tool.progress": ("tool_call", AgentToolActivity),
    "agent.tool.completed": ("tool_call", AgentToolActivity),
    "agent.diff": ("diff", AgentDiff),
    "agent.result": ("result", AgentResult),
    "feed.account": ("result", FeedAccount),
    "feed.market": ("result", FeedMarket),
    "feed.market-patch": ("result", FeedMarketPatch),
    "feed.touch": ("result", FeedTouch),
    "feed.board": ("result", FeedBoard),
    "feed.board-patch": ("result", FeedBoardPatch),
    "feed.activity": ("result", FeedActivity),
    "feed.execution": ("result", FeedExecution),
    "feed.order": ("result", FeedOrder),
    "feed.ui": ("result", FeedUiNudge),
    "feed.debate": ("result", FeedDebate),
    "feed.status": ("result", FeedStatus),
    "feed.strategy-signal": ("result", FeedStrategySignal),
    "feed.price-alert": ("result", FeedPriceAlert),
    "feed.reminder": ("result", FeedReminder),
    "warning": ("warning", RiskWarning),
    "error": ("error", ErrorPayload),
}


def validate_event(event: dict[str, Any]) -> None:
    if event.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("unsupported_schema_version")
    if not isinstance(event.get("id"), str) or not event["id"]:
        raise ValueError("invalid_event_id")
    if not isinstance(event.get("sessionId"), str) or not event["sessionId"]:
        raise ValueError("invalid_event_session")
    if not isinstance(event.get("timestamp"), str) or not event["timestamp"]:
        raise ValueError("invalid_event_timestamp")
    name = event.get("name")
    definition = EVENT_MODELS.get(name)
    if definition is None:
        raise ValueError("unknown_event_name")
    expected_kind, model = definition
    if event.get("kind") != expected_kind or event.get("kind") not in KINDS:
        raise ValueError("invalid_event_kind")
    try:
        model.model_validate(event.get("payload"))
    except ValidationError as error:
        raise ValueError(f"invalid_event_payload:{name}") from error

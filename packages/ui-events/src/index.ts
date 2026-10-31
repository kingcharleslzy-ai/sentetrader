import { z } from "zod";

export type UiEventKind =
  | "message"
  | "progress"
  | "tool_call"
  | "artifact"
  | "chart"
  | "diff"
  | "approval"
  | "result"
  | "warning"
  | "error";

export type RuntimeMode = "local" | "cloud";
export type AgentId = "native" | "claude" | "kimi" | "dsh" | "workbuddy";
export type TurnRole = "user" | "assistant" | "system";

export interface UiEventEnvelope<Name extends string, Payload> {
  schemaVersion: 1;
  id: string;
  sessionId: string;
  kind: UiEventKind;
  name: Name;
  timestamp: string;
  payload: Payload;
}

export interface MessagePayload {
  turnId: string;
  role: TurnRole;
  text: string;
  agentId?: AgentId;
  /** "voice": the turn came in through the real-time call (the user spoke; the answer was read out). */
  origin?: "voice";
  /** Voice turns only: the 1–3 sentence summary the call read out; null when the summary failed. */
  spoken?: string | null;
  /** A streamed answer that did not finish ("cancelled"/"failed"); the client unblocks the turn. */
  ended?: "cancelled" | "failed";
  /** 对象级 AI (2026-09-25): a user turn asked from an object's "AI ▾" menu; shown as "@ label".
   *  strategy_edit: the strategy editor's 跟 AI 说, which the Runtime has sent since 2026-10-08 (#932 F2). */
  focus?: { kind: "strategy" | "strategy_instance" | "position" | "order" | "strategy_edit"; label: string };
}

/** 续句合并：被并走的半句 user 消息标记为 superseded，客户端隐藏该轮（数据不删）。 */
export interface MessageSupersededPayload {
  turnId: string;
  supersededBy: string;
}

export interface AccountSummary {
  turnId: string;
  accountName: string;
  /** 外盘二期：美元子账户为 USD；缺省按人民币。 */
  currency?: "CNY" | "USD";
  updatedAt: string;
  capturedAt?: string;
  ageSeconds?: number;
  dataStatus?: "simulated" | "normal" | "stale" | "unavailable";
  isCached?: boolean;
  equity: number;
  todayPnl: number;
  todayNetPnl?: number;
  floatProfit?: number;
  positionProfit?: number;
  closeProfit?: number;
  commission?: number;
  marginRatio: number;
  positionCount: number;
  /** 本站模拟挂起中的到期结算（#527）：待最后交易日结算价 / 暂估待核；快期账户为空。 */
  expirySettlements?: Array<{ symbol: string; finalDay: string; status: string; statusLabel: string;
    estimate?: { price: number } }>;
  riskBudgetUsed: number;
  status: "normal" | "warning";
  insight: string;
  equityCurve: number[];
  /** 回答卡片化 (2026-10-14): the turn's one account card — a second read in the turn replaces it in place. */
  cardId?: string;
}

export interface MarketSnapshot {
  turnId: string;
  symbol: string;
  name: string;
  price: number;
  changePercent: number;
  updatedAt: string;
  capturedAt?: string;
  ageSeconds?: number;
  /** `backup`: Sina's quote while every TqKq source is down — watch-only. */
  dataStatus?: "simulated" | "normal" | "stale" | "backup";
  /** 新浪（备用） on a backup quote. */
  source?: string;
  marketSessionStatus?: "trading" | "closed" | "unknown";
  trend: "up" | "down" | "flat";
  summary: string;
  priceCurve: number[];
  /** 回答卡片化 (2026-10-14): the turn's one quote card; further contracts read in the same turn ride as `peers`. */
  cardId?: string;
  peers?: MarketPeer[];
}

export interface MarketPeer { symbol: string; name: string; price: number; changePercent: number; dataStatus?: "stale" | "backup" | null }

export interface StrategyCondition {
  label: string;
  status: "passed" | "waiting" | "failed";
  detail: string;
}

export interface StrategyExplanation {
  turnId: string;
  strategyId: string;
  name: string;
  symbol: string;
  version: string;
  status: "draft" | "validating" | "ready" | "monitoring" | "waiting" | "running" | "paused" | "archived";
  summary: string;
  conditions: StrategyCondition[];
  template?: "ma_cross" | "breakout" | "kd_cross" | "macd_cross" | "combined";
  timeframeSeconds?: 60 | 180 | 300 | 900 | 3600 | 86400;
  direction?: "long" | "short" | "both";
  riskLimits?: {
    maxPosition: number;
    maxOrdersPerDay: number;
    stopLossTicks: number;
    takeProfitTicks: number | null;
    cooldownBars: number;
  };
  definitionDigest?: string;
  /** 建议调整参数 (2026-09-25): a draft saved from another strategy, and 旧值 → 新值 per parameter. */
  revisionOf?: { strategyId: string; name: string; version: string };
  changes?: { field: string; label: string; from: unknown; to: unknown }[];
  backtestStatus?: "not_run" | "passed" | "failed";
  executionMode?: "simulation_only";
  createdAt?: string;
  validationReportId?: string;
  validationStatus?: "incomplete" | "passed" | "failed";
  intentId?: string;
  signalSnapshot?: {
    signal: "buy" | "sell" | "wait";
    barTime: string;
    barState: "completed";
    dataStatus: "normal" | "simulated";
    definitionDigest: string;
    /** 加仓 (2026-09-28): this open adds to the held same-direction position. */
    add?: boolean;
    /** The time-exit / 分批止盈 reason this close is one firing of (once per position). */
    exitReason?: string;
  };
  positionIntent?: {
    action: "hold" | "open_long" | "open_short" | "close_long" | "close_short" | "reduce_long" | "reduce_short";
    requestedVolume: number;
    currentLong: number;
    currentShort: number;
    targetLong: number;
    targetShort: number;
    reason: string;
  };
  riskAdjustedIntent?: {
    status: "no_action" | "allowed" | "blocked" | "reduce_only";
    action: "hold" | "open_long" | "open_short" | "close_long" | "close_short" | "reduce_long" | "reduce_short";
    volume: number;
    stopPrice: number | null;
    takeProfitPrice: number | null;
    checks: Array<{ id: string; label: string; passed: boolean }>;
    riskCheck: Record<string, unknown> | null;
    reason: string;
    executionEligible: false;
    liveTrading: "disabled";
  };
}

export interface TaskStep {
  id: string;
  label: string;
  /** Raw tool name behind a tool step (web_search, get_account_summary …); absent on non-tool steps. */
  tool?: string;
  status: "waiting" | "active" | "done" | "failed";
  detail: string;
  /** A failed tool step's errorCode (no_position_to_close, tool_failed …), for tracing a failure later. */
  errorCode?: string;
}

export interface AgentTask {
  stage?: string;
  summary?: string;
  startedAt?: string;
  finishedAt?: string;
  turnId: string;
  taskId: string;
  title: string;
  subtitle: string;
  progress: number;
  status: "running" | "completed" | "failed" | "cancelled";
  steps: TaskStep[];
  /** Model endpoint slug the turn runs on (native agent); older Runtimes omit it. */
  model?: string | null;
}

export interface BacktestMetric {
  label: string;
  before: string;
  after: string;
  tone: "positive" | "warning" | "neutral";
}

export interface BacktestComparison {
  turnId: string;
  taskId: string;
  backtestId: string;
  artifactDigest: string;
  strategyDigest: string;
  dataDigest: string;
  engineDigest: string;
  strategyReleaseId: string;
  candidateVariantId: string;
  candidateConfigDigest: string;
  candidateParameters: {
    baselineThreshold: string;
    candidateThreshold: string;
  };
  engineVersion: string;
  dataSource: string;
  generatedAt: string;
  reproducible: true;
  sourceKind?: "synthetic_fixture" | "tqsdk_history";
  dataPointCount?: number;
  strategyName: string;
  symbol: string;
  oldVersion: string;
  newVersion: string;
  period: string;
  metrics: BacktestMetric[];
  oldCurve: number[];
  newCurve: number[];
  conclusion: string;
  recommendation: string;
}

/** 规则回测 (策略积木·第一期 B): one trade of the user's own rules over history. One strategy
 * position may be several fills (加仓) and close in several rows (分批止盈): `adds` is the 加仓
 * count at this close, `partial` marks a row that closed only part of the position. */
export interface StrategyBacktestTrade {
  id: number; direction: "long" | "short"; volume: number;
  openTime: number; openPrice: number; closeTime: number; closePrice: number;
  exitReason: string; exitLabel: string; barsHeld: number; gross: number; fees: number; net: number;
  adds?: number; partial?: boolean;
  /** 价差 (2026-09-29): each leg's own fill — prices, volumes, gross and fees per leg; the row's own
   * openPrice / closePrice are spread values and volume counts spread units. */
  legs?: StrategyBacktestLegTrade[];
}

/** 价差回测的一腿: 方向 is buy / sell on that contract, gross and fees use the leg's own multiplier. */
export interface StrategyBacktestLegTrade {
  symbol: string; direction: "buy" | "sell"; volume: number;
  openTime: number; openPrice: number; closeTime: number; closePrice: number;
  gross: number; fees: number;
}

export interface StrategyHealthGroup {
  judgedBars: number; allPass: number; relaxedCrossPass: number | null;
  conditions: { label: string; alone: number; without: number; cross: boolean }[];
}
export interface StrategyHealthAdvice {
  id: string; kind: "drop" | "relaxCross"; groups: ("entryLong" | "entryShort")[];
  index?: number; label?: string; withinBars?: number; passes: Partial<Record<"entryLong" | "entryShort", number>>; text: string;
}

/** 规则回测 result: POST /api/strategies/{id}/backtest, /api/strategy-backtests, and the `card.strategy_backtest` card. */
export interface StrategyBacktestResult {
  backtestId: string; kind: "rule"; createdAt?: string;
  strategyId?: string | null; strategyName: string; version?: number | null;
  symbol: string; timeframeSeconds: number;
  params?: { tradingDays: number | null; slippageTicks: number; initialBalance: number };
  summary: { netProfit: number; returnPercent: number | null; maxDrawdown: number; maxDrawdownPercent: number;
    winRate: number | null; payoffRatio: number | null; trades: number; averageBarsHeld: number | null; fees: number; blockedOpens?: number };
  exitReasons: { reason: string; label: string; count: number; net: number }[];
  equityCurve: { time: number; equity: number }[];
  trades: StrategyBacktestTrade[]; tradesTotal?: number; tradesTruncated?: boolean;
  coverage: { firstBarTime: number; lastBarTime: number; bars: number; tradingDays: number;
    firstTradingDay: string | null; lastTradingDay: string | null; requestedTradingDays: number | null; limited: boolean };
  notes?: string[]; assumptions: string[]; feeSource?: string;
  /** How the fees were counted (2026-09-25): custom rates, the third-party reference, or none (warning). */
  feeEstimate?: { source: "custom" | "reference" | "none"; warning: boolean; text: string };
  /** A template preview's backtest (策略模板库). */
  templateId?: string;
  /** 价差 (2026-09-29): the two legs this plan trades (the row's `symbol` is leg A) and the spread's
   * own close per judged bar for the 价差小图; absent on single-contract plans. */
  spread?: { kind: "difference" | "ratio"; legs: { symbol: string; lots: number; weight: number;
    priceTick: number; volumeMultiple: number }[] };
  spreadCurve?: { time: number; value: number }[];
  /** 策略体检 (2026-09-26): per entry group, how often each condition held (occurrences, not bars). */
  diagnosis?: Partial<Record<"entryLong" | "entryShort", StrategyHealthGroup>>;
  /** Structural suggestions when the plan opens nothing (≤ 2), counted in entries, never by profit. */
  advice?: StrategyHealthAdvice[];
  /** One line: how often the entry groups held and where they are stuck; null when healthy. */
  headline?: string | null;
  /** 回测与体检 (2026-09-26): the verdict every run carries — the headline when stuck, else the pass line. */
  checkLine?: string | null;
  timings?: { loadMs: number; runMs: number };
  /** AI 怎么看 (2026-10-08): the facts the Runtime computed from this result (each sentence with the
   * numbers it quotes) and, once asked for, the model's plain sentence (source model) — or the facts
   * themselves when the model was unavailable or used a number the facts do not hold (source facts). */
  verdict?: StrategyBacktestVerdict;
  /** 本机优先 (2026-10-12): where it was computed — the user's browser (Pyodide) or the Runtime — and,
   *  for a server run the interface could have computed, why it did not; diagnostic only, not shown.
   *  `browserCheck` is the server's rerun compared with the browser's numbers over the same inputs. */
  source?: "browser" | "server";
  fallbackReason?: string;
  inputDigest?: string;
  engineDigest?: string;
  browserCheck?: { match: boolean; browserSummary?: Record<string, unknown> };
}

export interface StrategyBacktestVerdict {
  facts: { id: string; text: string; tone: "info" | "warning"; numbers: string[] }[];
  text: string | null; source: "model" | "facts" | null;
}

export interface StrategyBacktestCard extends StrategyBacktestResult {
  turnId: string;
  taskId: string;
}

export interface RiskCheck {
  passed: boolean;
  limit: number;
  estimatedLoss: number;
  reason: string;
  contractMultiplier: number;
  priceTick: string;
  policyVersion?: number;
  checks?: Record<string, boolean>;
}

export interface TargetPositionPlan {
  symbol: string;
  currentNet: number;
  targetNet: number;
  longVolume: number;
  shortVolume: number;
  legs: Array<{ action: "open" | "close"; side: string; volume: number }>;
  summary: string;
  crossesZero: boolean;
  unchanged: boolean;
  /** The price mode the user stated, used by every step of the plan (#941); null = per-side defaults. */
  priceIntent?: "passive" | "aggressive" | "last" | null;
  /** Present when the card is one step of a multi-leg plan (a reverse target). */
  planId?: string;
  step?: number;
  totalSteps?: number;
  note?: string;
}

/** 下单前的“钱”估算 (2026-10-02): read-only, display-only — what one lot's margin and fee
 * roughly are, how many lots the available funds still open, and (closing) the margin a
 * matching position would release. Missing facts are null with `missing` reason codes; the
 * estimate never gates or blocks the order. */
export interface OrderEstimate {
  schemaVersion: 1;
  symbol: string;
  side: "buy" | "sell";
  offset: "open" | "close";
  volume: number;
  price: number | null;
  marginPerLot: number | null;
  /** 开仓一手的实际生效手续费 (2026-10-13 #770)：与 `fee` 同一估算入口，来源见 commissionSource。 */
  commissionPerLot: number | null;
  commissionSource?: "custom" | "exchange_standard" | "market_source" | null;
  /** 各金额字段的单位说明（给 AI 复述用）。 */
  units?: Record<string, string>;
  margin: number | null;
  fee: number | null;
  /** 平仓手续费的今昨拆分 (2026-10-03)：只有上期所/能源中心这类分今昨的交易所给，其余为 null。 */
  feeBreakdown?: { today: { volume: number; fee: number }; historical: { volume: number; fee: number } } | null;
  available: number | null;
  maxOpenVolume: number | null;
  insufficient: boolean;
  releasedMargin: number | null;
  basis: string;
  missing: string[];
  dataStatus?: string;
}

export interface TradeProposal {
  turnId: string;
  proposalId: string;
  version: number;
  symbol: string;
  name: string;
  side: "buy_open" | "sell_open" | "buy_close" | "sell_close";
  offset?: "OPEN" | "CLOSE" | "CLOSETODAY";
  positionBucket?: "today" | "historical" | "all";
  volume: number;
  limitPrice: number;
  stopPrice?: number;
  contractMultiplier: number;
  updatedAt: string;
  environment: "simulation";
  status: "draft" | "risk_blocked" | "approval_required" | "candidate_recorded" | "rejected";
  /** Client-side, from the proposal.result that closed the card: its confirm window ran out or a newer plan replaced it. */
  closedReason?: ProposalClosedReason;
  /** Which simulation account the card trades: 本站模拟账户 (site engine) or 快期模拟账户;
   * absent on cards from before paper accounts existed. */
  accountLabel?: string;
  /** The trading account the card's proposal belongs to (#1005), from the Runtime's proposal record:
   * the card reads its order status from this account. Absent from Runtimes before the field. */
  accountId?: string;
  /** The card account's money (外盘二期): USD on the 本站模拟美元子账户, CNY otherwise; absent = CNY. */
  currency?: "CNY" | "USD";
  riskCheck: RiskCheck;
  executionEligibleAtCreation?: boolean;
  approvalId?: string;
  auditId?: string;
  /** Set while the real-time call waits for a spoken "确认": ISO time when the card is abandoned. */
  voiceConfirmBy?: string | null;
  /** The 目标持仓 plan this card is one step of. */
  targetPosition?: TargetPositionPlan;
  /** The “钱”估算 an opening card carries (保证金约 / 手续费约 / 可开手数): display-only. */
  estimate?: OrderEstimate;
  /** 出卡确认 (2026-09-25): a monitored strategy's signal as this card; the fill reads as the strategy's.
   * `add` marks a 加仓 open (2026-09-28): the card confirms an add, not a fresh entry. */
  strategySource?: { strategyId: string; name: string; attemptId?: string; barTime?: string; add?: boolean };
  /** A strategy card the Runtime refused to send (the user's stop / emergency stop): why, in Chinese. */
  confirmBlocked?: { code: string; reason: string; at?: string };
  /** 下单顺带保护 (2026-10-02): the user's own stop / take riding this open card — either key may be
   * absent (at least one is set); the fill arms them at the actual filled volume. */
  protection?: { stopLossPrice?: string; takeProfitPrice?: string };
  /** 合并卡 (2026-10-06): a close card's second step — the remaining position's stop / take. */
  remainingProtection?: RemainingProtection;
}

/** One level of a merged card's second step: `set` to a price, `remove`, or `keep` what stands now. */
export interface RemainingProtectionLevel { action: "set" | "remove" | "keep"; price?: string | null; atCost?: boolean }

/** One position bucket's own stop / take (#528): today / historical lots may carry different prices. */
export interface ProtectionBucketLevels {
  positionBucket: "today" | "historical" | "all";
  volume: number;
  stopLoss: string | null;
  takeProfit: string | null;
}

/** 合并卡 (2026-10-06): ① close the card's lots, ② once the close filled, the remaining position's
 *  user-set stop / take are replaced by these (never two sets). `status` follows the second step:
 *  pending (card) → awaiting_close (confirmed) → done / failed / skipped, republished under the card. */
export interface RemainingProtection {
  direction: "long" | "short";
  remainingVolume: number;
  stopLoss: RemainingProtectionLevel;
  takeProfit: RemainingProtectionLevel;
  /** Scalar only when every bucket agrees; otherwise null and `perBucketOriginal` holds each bucket's
   *  price at card time (pre-close lots, never the remaining split). */
  original: { stopLoss?: string | null; takeProfit?: string | null };
  perBucketOriginal?: ProtectionBucketLevels[] | null;
  cost?: string | null;
  status: "pending" | "awaiting_close" | "done" | "failed" | "skipped";
  reason?: string | null;
  result?: { remainingVolume?: number; stopLoss?: string | null; takeProfit?: string | null;
    previousStopLoss?: string | null; previousTakeProfit?: string | null; filled?: number; reason?: string;
    /** What this run actually left on each remaining bucket, and what it replaced there. */
    perBucketProtection?: (ProtectionBucketLevels & { previousStopLoss: string | null; previousTakeProfit: string | null })[] | null } | null;
  actions?: CardAction[];
  /** The position also carries a strategy's own protection, which this card leaves alone (two stops). */
  strategyNote?: string | null;
}

export interface CloseBatchOrder {
  symbol: string;
  name: string;
  side: "buy_close" | "sell_close";
  offset: "CLOSE" | "CLOSETODAY";
  positionBucket: "today" | "historical" | "all";
  volume: number;
  limitPrice: number;
  contractMultiplier: number;
  riskCheck: RiskCheck;
  clientOrderId?: string | null;
}

/** One card closing every listed position bucket; one confirmation sends them all, one by one. */
export interface CloseBatchProposal {
  /** 本站模拟两本账的全平 (2026-10-21): the other ledger's close card of the same sentence — confirming or
   *  rejecting either one does the same to the other on the Runtime. */
  linkedProposalIds?: string[];
  turnId: string;
  proposalId: string;
  version: number;
  environment: "simulation";
  reason: string;
  orders: CloseBatchOrder[];
  skipped: Record<string, unknown>[];
  /** How every order's price was set; absent on cards from before the field (read as 对手价). */
  priceIntent?: "aggressive" | "passive" | "last" | null;
  /** The prices are the user's own limit price. */
  userLimitPrice?: boolean;
  totalOrders: number;
  totalVolume: number;
  riskCheck: Record<string, unknown>;
  updatedAt: string;
  status: "draft" | "risk_blocked" | "approval_required" | "candidate_recorded" | "rejected";
  /** Client-side, from the proposal.result that closed the card: its confirm window ran out or a newer plan replaced it. */
  closedReason?: ProposalClosedReason;
  /** Which simulation account the card trades: 本站模拟账户 (site engine) or 快期模拟账户. */
  accountLabel?: string;
  /** The trading account the card's proposal belongs to (#1005), from the Runtime's proposal record:
   * the card reads its order status from this account. Absent from Runtimes before the field. */
  accountId?: string;
  approvalId?: string | null;
  auditId?: string | null;
  voiceConfirmBy?: string | null;
  /** The 目标持仓 plan this card is one step of. */
  targetPosition?: TargetPositionPlan;
  /** 合并卡 on a 今+昨 split close of one position (2026-10-06). */
  remainingProtection?: RemainingProtection;
}

export interface SpreadLegOrder {
  symbol: string;
  name: string;
  side: "buy_open" | "sell_open" | "buy_close" | "sell_close";
  offset: "OPEN" | "CLOSE" | "CLOSETODAY";
  positionBucket?: "today" | "historical" | "all" | null;
  volume: number;
  limitPrice: number;
  contractMultiplier: number;
  riskCheck: RiskCheck;
  clientOrderId?: string | null;
}

/** 价差 (2026-09-29): one card for both legs of one spread action; one confirmation dispatches
 * the two legs together at the opponent's price, and the confirmed card reports each leg's
 * real order (`simulationOrders` / `execution`, the same 口径 as close_batch). */
export interface SpreadProposal {
  turnId: string;
  proposalId: string;
  version: number;
  environment: "simulation";
  reason: string;
  spread: Record<string, unknown>;
  spreadWords: string;
  legsWords: string;
  action: "open_long" | "open_short" | "close_long" | "close_short";
  units: number;
  currentSpread?: number | null;
  entrySpread?: number | null;
  exitReason?: "spread_stop" | "spread_take" | null;
  orders: SpreadLegOrder[];
  totalOrders: number;
  totalVolume: number;
  riskCheck: Record<string, unknown>;
  updatedAt: string;
  status: "draft" | "risk_blocked" | "approval_required" | "candidate_recorded" | "rejected";
  /** Client-side, from the proposal.result that closed the card: its confirm window ran out or a newer plan replaced it. */
  closedReason?: ProposalClosedReason;
  /** Which simulation account the card trades: 本站模拟账户 (site engine) or 快期模拟账户. */
  accountLabel?: string;
  /** The trading account the card's proposal belongs to (#1005), from the Runtime's proposal record:
   * the card reads its order status from this account. Absent from Runtimes before the field. */
  accountId?: string;
  approvalId?: string | null;
  auditId?: string | null;
  voiceConfirmBy?: string | null;
  strategySource?: { strategyId: string; name: string; attemptId?: string; barTime?: string } | null;
  /** Present on the confirmed card's republish: each leg's live order and the combined readout. */
  simulationOrders?: unknown[];
  execution?: Record<string, unknown>;
  /** The settled attempt's conclusion line (成交完成 / 已补单成交 / 已回退并暂停 / …), present once
   * the executor's follow finishes — on its final push and on every idempotent replay alike. */
  executionConclusion?: { code: string; text: string } | null;
  /** Why a confirming click ordered nothing (e.g. the strategy paused while the card waited). */
  confirmBlocked?: { code: string; reason: string; at?: string };
}

export interface RiskRuleProposal {
  turnId: string;
  proposalId: string;
  version: number;
  basePolicyVersion: number;
  policyVersion: number;
  allowedInstruments: Array<{ symbol: string; name: string }>;
  maxLossPerTrade: string;
  maxDailyLoss: string;
  noNewPositionsAfter: string;
  timezone: "Asia/Shanghai";
  rulesDigest: string;
  deterministic: true;
  executionEnabled: false;
  status: "approval_required" | "risk_rules_applied" | "rejected";
  approvalId: string;
  policyId?: string;
  auditId?: string;
}

export interface RiskWarning {
  turnId: string;
  title: string;
  message: string;
  severity: "warning" | "danger";
  deterministic: boolean;
}

export interface ApprovalRequired {
  proposalId: string;
  approvalId: string;
  proposalVersion: number;
  proposalType: "trade" | "risk_rule" | "cancel";
}

/** Why a card closed without the user's 放弃 (#932 P2): 已过期 (the confirm window ran out) / 已作废 (replaced). */
export type ProposalClosedReason = "expired" | "superseded";

export interface ProposalResult {
  proposalId: string;
  status: "candidate_recorded" | "risk_rules_applied" | "rejected";
  auditId: string;
  message: string;
  reason?: ProposalClosedReason | null;
}

/** The account a status snapshot acts on (4.4a): how it is shown, the caller's
 *  role on it and the execution-owner fence. Omitted when the account row is gone.
 *  A pool-managed account (M1a/M1b) also states where its child is — `poolStatus`
 *  and the `status` shorthand (connecting / queued / connected / offline). */
export interface RuntimeAccountStatus {
  id: string;
  displayName: string;
  role: string;
  environment: string;
  executionOwner: boolean;
  poolStatus?: "connecting" | "queued" | "connected" | "offline";
  status?: "connecting" | "queued" | "connected" | "offline";
  /** 外盘二期：美元子账户为 USD。 */
  currency?: "CNY" | "USD";
  /** 外盘仓合并显示 (2026-10-21)：人民币本站模拟账户已开好的美元子账户编号（paperusd:同号），持仓面板并进它的外盘仓。 */
  foreignAccountId?: string;
}

export interface RuntimeStatus {
  mode: RuntimeMode;
  adapter: "fake-agent" | "native-agent" | "historical-replay";
  accountMode: "fake" | "tqkq-readonly" | "paper";
  runtimeConnected: boolean;
  agentConnected: boolean;
  /** `backup` (2026-10-07): TqKq is down and the shared market serves Sina's watch-only quotes. */
  marketDataStatus: "simulated" | "connecting" | "normal" | "stale" | "unavailable" | "backup";
  /** Where the market data comes from while it is `backup` (新浪（备用）). */
  marketDataSource?: string;
  marketSessionStatus?: "trading" | "closed" | "unknown";
  accountStatus: "simulated" | "connecting" | "normal" | "stale" | "unavailable";
  tradingGatewayStatus: "disabled" | "read_only" | "simulation" | "unavailable";
  simulationTrading?: boolean;
  dataSource?: "historical" | "tqkq" | "paper" | "demo";
  executionMode?: "simulation" | "read_only";
  accountLabel?: string;
  executionLabel?: string;
  capabilitySummary?: string;
  riskStatus: "normal" | "warning" | "unknown";
  dataErrorCode?: string | null;
  dataConnectionStage?: "starting" | "loading_sdk" | "authenticating" | "subscribing" | "discovering_instruments" | "waiting_snapshot" | "waiting_account" | "waiting_market" | "reconnecting" | "ready" | "failed" | null;
  dataInitializationSeconds?: number | null;
  dataStartupTimings?: Record<string, number> | null;
  dataStartupTotalSeconds?: number | null;
  agentNotice?: string | null;
  /** Deprecated compatibility field; use agentConnected. */
  connected: boolean;
  version: string;
  account?: RuntimeAccountStatus;
  /** How many times this session was moved to another account on this Runtime (#932 follow-up): the number the
   *  next PUT /api/sessions/{id}/account names as expectedGeneration. Absent from an older Runtime. */
  accountGeneration?: number;
}

export interface AgentToolActivity {
  turnId: string;
  taskId: string;
  itemId: string;
  toolType: "command" | "function";
  status: "running" | "completed" | "failed";
  command?: string;
  cwd?: string;
  exitCode?: number | null;
  outputSummary?: string;
}

export interface AgentFileChange {
  path: string;
  kind: string;
  diff: string;
}

export interface AgentDiffArtifact {
  turnId: string;
  taskId: string;
  itemId: string;
  status: "running" | "completed" | "failed";
  changes: AgentFileChange[];
}

export interface RuntimeError {
  turnId?: string | null;
  runId?: string | null;
  code: string;
  message: string;
  scope: "connection" | "agent" | "task" | "validation" | "trading";
}

export interface AgentResult {
  turnId: string;
  taskId: string;
  threadId: string;
  status: "completed" | "failed" | "cancelled";
  /** Model endpoint slug the turn ran on; older Runtimes omit it. */
  model?: string | null;
}

export interface FeedAccount {
  account: Record<string, unknown>;
  positions: Array<Record<string, unknown>>;
  activity: Record<string, unknown>;
  risk: Record<string, unknown>;
  [key: string]: unknown;
}

/** The window a focus `since` answer proves: its first bar, its bar count and the digest of the bars up to `barsAfter`. */
export interface BarsWindow {
  first: number;
  count: number;
  digest: string;
}

export interface FeedMarket {
  schemaVersion: 1;
  durationSeconds: 60 | 180 | 300 | 900 | 1800 | 3600 | 86400 | 604800;
  quote: Record<string, unknown>;
  bars: Array<Record<string, unknown>>;
  liveTrading: "disabled";
  /** A focus `since` answer: only the bars after this one, with `barsWindow` (always together). */
  barsAfter?: number;
  barsWindow?: BarsWindow;
  [key: string]: unknown;
}

export interface FeedActivity {
  environment: "replay" | "simulation";
  symbol: string;
  orders: Array<Record<string, unknown>>;
  fills: Array<Record<string, unknown>>;
  strategies: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface FeedExecution {
  stops: Array<Record<string, unknown>>;
  simulationOnly: true;
  liveTrading: "disabled";
  [key: string]: unknown;
}

export interface FeedUiNudge {
  reason: string;
}

/** 多 AI 研讨 (2026-10-24): one turn's (turn, side) or one analyst's report (analyst) text so far while it is
 *  written (the whole text each time). */
export interface FeedDebate {
  id: string;
  turn?: number;
  side?: "bull" | "bear";
  analyst?: "technical" | "supply" | "flow" | "news";
  text: string;
}

/** One feed channel cannot be read right now; that reader falls back to its own poll. */
/** What changed in the shared market since the full frame `base` (feed.market `revision`).
 *  Sent only to sessions that asked for patches; `changes` holds changed scalars, the full
 *  quote when it changed, and `barsPatch` {bars, trimBefore}. */
export interface FeedMarketPatch {
  revision: string;
  base: string;
  durationSeconds: 60 | 180 | 300 | 900 | 1800 | 3600 | 86400 | 604800;
  changes: Record<string, unknown>;
}

/** 报价总表 pushed (2026-10-12): the rows the window asked for (PUT .../feed/view `board`), the same
 *  row shape GET /api/market/board serves, at board revision `feedRevision`; `key` echoes the client's
 *  name for its row filter. */
export interface FeedBoard {
  feedRevision: string;
  key: string;
  rows: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

/** What changed on the board since revision `base`: changed fields by symbol and changed meta (always
 *  with the capture stamps). `base === feedRevision` is a refresh with nothing changed. */
export interface FeedBoardPatch {
  feedRevision: string;
  base: string;
  key: string;
  meta: Record<string, unknown>;
  rows: Record<string, Record<string, unknown>>;
}

/** An unchanged account / activity / execution frame is still current: only its capture stamps
 *  moved. `stamps` = [[path, value], ...] into the last full frame of that channel. Sent only to
 *  sessions that asked for it (feed focus `feedTouch`). */
export interface FeedTouch {
  name: "feed.account" | "feed.activity" | "feed.execution";
  stamps: [Array<string | number>, unknown][];
}

export interface FeedStatus {
  channel: "account" | "market" | "activity" | "execution" | "board";
  status: "unavailable";
  reason: string;
}

/** A monitoring strategy's signal changed: the same read explain_strategy_signal produces,
 * pushed so the client can draw the numbered annotations and show the spoken line. */
export interface FeedStrategySignal {
  strategyId: string;
  name: string;
  symbol: string;
  action: "open_long" | "open_short" | "close_long" | "close_short";
  actionLabel: string;
  volume: number;
  barTime: number | null;
  evaluatedBarTime: string | null;
  validated: true;
  annotations: Array<Record<string, unknown>>;
  spoken: string;
  executionMode: "simulation_only";
}

/** 到价提醒 (2026-10-02): one alert crossed. Every subscribed window of the user's gets one —
 *  the client announces it and drops the alert's chart line and list row. */
export interface FeedPriceAlert {
  id: string;
  symbol: string;
  name: string;
  direction: "above" | "below";
  price: string;
  lastPrice: number | null;
  note: string;
  text: string;
  triggeredAt: string | null;
}

/** 统一提醒 (#932): the Runtime gated it on the kind's own switch and recorded it (GET /api/reminders);
 *  every subscribed window of the user re-reads the 提醒中心 and shows the desktop notification and the
 *  light in-app notice — except `type: "update"`, a known reminder's changed state (no notification). */
export interface FeedReminder {
  kind: string;
  title: string;
  text: string;
  at: string;
  id?: string;
  type?: "new" | "update";
  unread?: number;
  accountId?: string;
  accountName?: string;
  debateId?: string;
}

/** A watched simulation order's read-back changed (2026-10-08): pushed to the session whose card
 *  was confirmed, so a confirmed card's per-order rows and the orders screen follow the order
 *  live. The payload is the same simulation-order view the list endpoint serves (the client's
 *  SimulationOrder), carried in full — only the anchors are pinned here. */
export interface FeedOrder {
  clientOrderId: string;
  accountId: string;
  displayStatus: string;
  [field: string]: unknown;
}


/** A structured card's button (2026-10-04): `ask` sends `prompt` as the user's next message;
 *  `ui` applies `changes` (an existing UI command) in this window. Never an order. */
export type CardAction =
  | { kind: "ask"; label: string; prompt: string; changes?: null }
  | { kind: "ui"; label: string; changes: Record<string, unknown>; prompt?: null }
  /** 保本提醒 (2026-10-06): answers the card's own question through its business route — never an order. */
  | { kind: "breakeven"; label: string; breakevenId: string; choice: "move" | "decline"; prompt?: null; changes?: null };

/** 下单类回合的结构化结局 (2026-10-04): `ask` = the order planner asks for what the order is missing (意图重构
 *  第 3 步: the typed fast path or propose_order; options send a whole order as the user's reply); `no_card` = 仍会
 *  在强制下单回合模型两次只回文字时发出. */
export interface OrderQuestionCard {
  turnId: string;
  cardId: string;
  kind: "ask" | "no_card";
  title: string;
  question: string;
  missing?: string[];
  options?: { label: string; prompt: string }[];
  reason?: string | null;
  userText?: string | null;
  actions?: CardAction[];
  createdAt: string;
  /** The reply that answered this card (republished under the same cardId): buttons stop offering
   *  an answer the conversation already consumed; `answer` is the chosen option's label. */
  answered?: boolean;
  answeredAt?: string | null;
  answer?: string | null;
}

export interface JudgmentLine { text: string; quote?: string; source?: string; asOf?: string | null }

/** 判断卡 (2026-10-04): the research desk's judgment of the product today, attached before the
 *  model answers an opinion question; `pending` is replaced under the same cardId when ready. */
export interface ResearchJudgmentCard {
  turnId: string;
  cardId: string;
  symbol: string;
  productName: string;
  status: "pending" | "ready" | "unavailable";
  stance?: "偏多" | "偏空" | "震荡" | "不明" | null;
  confidence?: "低" | "中" | "高" | null;
  headline?: string | null;
  facts?: JudgmentLine[];
  invalidation?: JudgmentLine[];
  generatedAt?: string | null;
  researchDay?: string | null;
  note?: string | null;
  actions?: CardAction[];
}

export interface CardFigure { label: string; value: string; tone?: "positive" | "warning" | "danger" | null }

/** 回答卡片化 (2026-10-14): one number cell of an answer / data card. `dir` is market direction (涨跌色
 *  preference applies: up = 涨色, down = 跌色); the Runtime only lets through figures traced back to this turn's reads. */
export interface AnswerFigure { label: string; value: string; unit?: string | null; dir?: "up" | "down" | null }
export interface AnswerSide { title: string; lines: string[] }
/** 依据 (folded): when the facts were read and from which reads — built by the Runtime, never the model. */
export interface AnswerBasis { at: string; sources: string[] }

/** 回答卡 `card.answer` (2026-10-14): an analysis / compare / "咋样" answer as one card — title, one-line
 *  conclusion, ≤6 figures, optional two-sided compare, ≤3 reasons, one risk line, folded basis, ≤2 actions
 *  (看图表 / 问 AI 怎么办). Same cardId again = replaced in place. */
export interface AnswerCard {
  turnId: string;
  cardId: string;
  title: string;
  conclusion: string;
  figures?: AnswerFigure[];
  compare?: { left: AnswerSide; right: AnswerSide } | null;
  reasons?: string[];
  risk?: string | null;
  symbol?: string | null;
  basis: AnswerBasis;
  actions?: CardAction[];
  /** A stale / backup read the answer rests on, shown on the card face. */
  warning?: string | null;
  /** 去重 (2026-10-14 跟进): cardIds of this turn's read cards (行情 / 账户 / 数据卡) shown before the answer
   *  card. They render inside the answer card's 依据 instead of standing beside it. */
  folded?: string[];
}

export interface DataCardRow { primary: string; secondary?: string | null; value?: string | null; dir?: "up" | "down" | null; tag?: string | null }

/** 数据卡 `card.data` (2026-10-14): a read tool's structured result shown as a card — 持仓 (positions),
 *  挂单与保护 (orders), 快讯 / 消息面 (news), 走势 (analysis), 报价总表 (quotes). Built by the Runtime from the tool result; several reads
 *  of one kind in a turn merge into the same cardId. */
export interface DataCard {
  turnId: string;
  cardId: string;
  kind: "positions" | "orders" | "news" | "analysis" | "quotes";
  title: string;
  figures?: AnswerFigure[];
  rows?: DataCardRow[];
  note?: string | null;
  empty?: string | null;
  /** A stale / backup read (数据陈旧 / 备用行情), shown on the card face. */
  warning?: string | null;
  symbol?: string | null;
  basis: AnswerBasis;
  actions?: CardAction[];
}

/** 提醒卡 (2026-10-04): one shape for 到价/风险度/收盘前/交割/止损止盈触发/挂单未成交; rides with the
 *  reminder's message under the same turnId. Actions ask the AI or open a panel — never an order. */
export interface ReminderCard {
  turnId: string;
  cardId: string;
  kind: "price_alert" | "risk_ratio" | "market_close" | "delivery" | "protection" | "order_unfilled" | "market_movement" | "daily_review" | "breakeven";
  title: string;
  summary: string;
  figures?: CardFigure[];
  symbol?: string | null;
  at: string;
  actions?: CardAction[];
  /** 今日复盘卡 (kind daily_review, 2026-10-06): the trading day, the day's net P&L as shown and the
   *  tag counts (计划内 3 / 提醒后 1); other kinds omit them. */
  tradingDay?: string;
  pnl?: string | null;
  tags?: { tag: "plan" | "remind" | "adhoc"; label: string; count: number }[];
  /** 保本提醒 (kind breakeven, 2026-10-06): the one step it proposes and the question's state. */
  step?: { title: string; detail?: string | null } | null;
  breakeven?: { id: string; state: "pending" | "moved" | "declined" | "failed"; cost: string; direction: "long" | "short"; result?: string | null } | null;
  /** 保本提醒: “策略「X」的保护不受影响…” when the position also has a strategy's own stop. */
  note?: string | null;
}

/** 执行回报卡 (2026-10-04): what the AI (自主执行) or a strategy (自动下单) sent, read back from the
 *  account; later fills/cancels republish under the same cardId. `unknown` is never success. */
export interface ExecutionCard {
  turnId: string;
  cardId: string;
  source: "ai" | "strategy";
  sourceLabel: string;
  orderKind: "order" | "stop";
  symbol: string;
  name: string;
  sideLabel: string;
  volume: number;
  price?: string | null;
  reason?: string;
  status: "submitted" | "partial" | "filled" | "unfilled" | "rejected" | "unknown" | "armed" | "failed";
  statusLabel: string;
  filledVolume?: number | null;
  clientOrderId?: string | null;
  detail?: string | null;
  updatedAt: string;
}

export type RoutineStepKind = "read" | "draw" | "backtest" | "margin" | "card" | "alert" | "watch" | "review";
/** `manner` (提醒、画线、盯异动 only, by the user's words): advisory 只给建议 (“看看要不要设提醒”: no
 *  writing tools), conditional 需要时执行 (“…需要就设”: judged, then may write); absent = just does it. */
export type RoutineStepManner = "advisory" | "conditional";
export interface RoutineStepView { kind: RoutineStepKind; kindLabel: string; text: string; manner?: RoutineStepManner | null }

/** 流程卡 (我的流程, 2026-10-06): the steps the AI drafted, each with its type. `draft` shows 保存 /
 *  改一下 — only the user's 保存 writes the routine; `saved` replaces it under the same cardId; `refused`
 *  (免确认下单、自动下单、改风险设置、自主执行开关) has no 保存 and says why. */
export interface RoutineCard {
  turnId: string;
  cardId: string;
  draftId: string;
  routineId?: string | null;
  mode: "create" | "update";
  name: string;
  steps: RoutineStepView[];
  status: "draft" | "saved" | "refused";
  refusal?: string | null;
  savedAt?: string | null;
  note?: string | null;
}

export interface RoutineRunStep extends RoutineStepView {
  status: "waiting" | "active" | "done" | "confirm" | "failed" | "skipped";
  result?: string | null;
}

/** 进度卡 (我的流程, 2026-10-06): a running routine, each step's state and one result line, republished
 *  under the same cardId. `confirm` = the step made a card the user still has to confirm. */
export interface RoutineRunCard {
  turnId: string;
  cardId: string;
  routineId: string;
  name: string;
  status: "running" | "completed" | "stopped" | "cancelled";
  steps: RoutineRunStep[];
  startedAt: string;
  finishedAt?: string | null;
  elapsedSeconds: number;
  confirmCount: number;
}

export type UiEvent =
  | UiEventEnvelope<"session.ready", { mode: RuntimeMode; agentId: AgentId }>
  | UiEventEnvelope<"message.delta", MessagePayload>
  | UiEventEnvelope<"message.completed", MessagePayload>
  | UiEventEnvelope<"message.superseded", MessageSupersededPayload>
  | UiEventEnvelope<"task.started", AgentTask>
  | UiEventEnvelope<"task.progress", AgentTask>
  | UiEventEnvelope<"task.completed", AgentTask>
  | UiEventEnvelope<"card.account", AccountSummary>
  | UiEventEnvelope<"card.market", MarketSnapshot>
  | UiEventEnvelope<"card.strategy", StrategyExplanation>
  | UiEventEnvelope<"card.backtest", BacktestComparison>
  | UiEventEnvelope<"card.strategy_backtest", StrategyBacktestCard>
  | UiEventEnvelope<"card.order_question", OrderQuestionCard>
  | UiEventEnvelope<"card.research_judgment", ResearchJudgmentCard>
  | UiEventEnvelope<"card.reminder", ReminderCard>
  | UiEventEnvelope<"card.execution", ExecutionCard>
  | UiEventEnvelope<"card.answer", AnswerCard>
  | UiEventEnvelope<"card.data", DataCard>
  | UiEventEnvelope<"card.routine", RoutineCard>
  | UiEventEnvelope<"card.routine_run", RoutineRunCard>
  | UiEventEnvelope<"proposal.trade", TradeProposal>
  | UiEventEnvelope<"proposal.close_batch", CloseBatchProposal>
  | UiEventEnvelope<"proposal.spread", SpreadProposal>
  | UiEventEnvelope<"proposal.risk_rule", RiskRuleProposal>
  | UiEventEnvelope<"trade.approval.required", ApprovalRequired>
  | UiEventEnvelope<"risk_rule.approval.required", ApprovalRequired>
  | UiEventEnvelope<"proposal.result", ProposalResult>
  | UiEventEnvelope<"runtime.status", RuntimeStatus>
  | UiEventEnvelope<"agent.tool.started", AgentToolActivity>
  | UiEventEnvelope<"agent.tool.progress", AgentToolActivity>
  | UiEventEnvelope<"agent.tool.completed", AgentToolActivity>
  | UiEventEnvelope<"agent.diff", AgentDiffArtifact>
  | UiEventEnvelope<"agent.result", AgentResult>
  | UiEventEnvelope<"feed.account", FeedAccount>
  | UiEventEnvelope<"feed.market", FeedMarket>
  | UiEventEnvelope<"feed.market-patch", FeedMarketPatch>
  | UiEventEnvelope<"feed.touch", FeedTouch>
  | UiEventEnvelope<"feed.board", FeedBoard>
  | UiEventEnvelope<"feed.board-patch", FeedBoardPatch>
  | UiEventEnvelope<"feed.activity", FeedActivity>
  | UiEventEnvelope<"feed.execution", FeedExecution>
  | UiEventEnvelope<"feed.ui", FeedUiNudge>
  | UiEventEnvelope<"feed.debate", FeedDebate>
  | UiEventEnvelope<"feed.status", FeedStatus>
  | UiEventEnvelope<"feed.strategy-signal", FeedStrategySignal>
  | UiEventEnvelope<"feed.price-alert", FeedPriceAlert>
  | UiEventEnvelope<"feed.reminder", FeedReminder>
  | UiEventEnvelope<"feed.order", FeedOrder>
  | UiEventEnvelope<"warning", RiskWarning>
  | UiEventEnvelope<"error", RuntimeError>;

const finiteNumber = z.number().finite();
const messageSchema = z.object({
  turnId: z.string().min(1), role: z.enum(["user", "assistant", "system"]), text: z.string(),
  agentId: z.enum(["native", "claude", "kimi", "dsh", "workbuddy"]).optional(),
  origin: z.literal("voice").optional(), spoken: z.string().nullable().optional(),
  ended: z.enum(["cancelled", "failed"]).optional(),
  focus: z.object({ kind: z.enum(["strategy", "strategy_instance", "position", "order", "strategy_edit"]), label: z.string().min(1).max(80) }).optional()
});
const messageSupersededSchema = z.object({
  turnId: z.string().min(1), supersededBy: z.string().min(1)
});
const taskStepSchema = z.object({
  id: z.string().min(1), label: z.string().min(1), tool: z.string().min(1).optional(),
  status: z.enum(["waiting", "active", "done", "failed"]), detail: z.string(),
  errorCode: z.string().min(1).optional()
});
const taskSchema = z.object({
  stage: z.string().optional(), summary: z.string().optional(),
  startedAt: z.string().optional(), finishedAt: z.string().optional(),
  turnId: z.string().min(1), taskId: z.string().min(1), title: z.string().min(1), subtitle: z.string(),
  progress: z.number().int().min(0).max(100), status: z.enum(["running", "completed", "failed", "cancelled"]),
  steps: z.array(taskStepSchema), model: z.string().nullable().optional()
});
const accountSchema = z.object({
  turnId: z.string().min(1), accountName: z.string().min(1), currency: z.enum(["CNY", "USD"]).optional(), updatedAt: z.string().min(1),
  capturedAt: z.string().min(1).optional(), ageSeconds: finiteNumber.nonnegative().optional(),
  dataStatus: z.enum(["simulated", "normal", "stale", "unavailable"]).optional(),
  isCached: z.boolean().optional(),
  equity: finiteNumber, todayPnl: finiteNumber, marginRatio: finiteNumber.nonnegative(),
  todayNetPnl: finiteNumber.optional(), floatProfit: finiteNumber.optional(),
  positionProfit: finiteNumber.optional(), closeProfit: finiteNumber.optional(),
  commission: finiteNumber.optional(),
  positionCount: z.number().int().nonnegative(), riskBudgetUsed: finiteNumber.nonnegative(),
  expirySettlements: z.array(z.object({ symbol: z.string(), finalDay: z.string(), status: z.string(), statusLabel: z.string(),
    estimate: z.object({ price: finiteNumber }).passthrough().optional() }).passthrough()).optional(),
  status: z.enum(["normal", "warning"]), insight: z.string(), equityCurve: z.array(finiteNumber),
  cardId: z.string().min(1).optional()
});
const marketSchema = z.object({
  turnId: z.string().min(1), symbol: z.string().min(1), name: z.string().min(1), price: finiteNumber,
  changePercent: finiteNumber, updatedAt: z.string().min(1), trend: z.enum(["up", "down", "flat"]),
  capturedAt: z.string().min(1).optional(), ageSeconds: finiteNumber.nonnegative().optional(),
  dataStatus: z.enum(["simulated", "normal", "stale", "backup"]).optional(), source: z.string().optional(),
  marketSessionStatus: z.enum(["trading", "closed", "unknown"]).optional(),
  summary: z.string(), priceCurve: z.array(finiteNumber),
  cardId: z.string().min(1).optional(),
  peers: z.array(z.object({ symbol: z.string().min(1), name: z.string().min(1), price: finiteNumber, changePercent: finiteNumber,
    dataStatus: z.enum(["stale", "backup"]).nullable().optional() })).max(6).optional()
});
const strategySchema = z.object({
  turnId: z.string().min(1), strategyId: z.string().min(1), name: z.string().min(1),
  symbol: z.string().min(1), version: z.string().min(1), status: z.enum(["draft", "validating", "ready", "monitoring", "waiting", "running", "paused", "archived"]),
  summary: z.string(), conditions: z.array(z.object({
    label: z.string().min(1), status: z.enum(["passed", "waiting", "failed"]), detail: z.string()
  })),
  template: z.enum(["ma_cross", "breakout", "kd_cross", "macd_cross", "combined"]).optional(),
  timeframeSeconds: z.union([z.literal(60), z.literal(180), z.literal(300), z.literal(900), z.literal(3600), z.literal(86400)]).optional(),
  direction: z.enum(["long", "short", "both"]).optional(),
  riskLimits: z.object({
    maxPosition: z.number().int().min(1),
    maxOrdersPerDay: z.number().int().min(1),
    stopLossTicks: z.number().int().min(1).max(5000),
    takeProfitTicks: z.number().int().min(1).max(10000).nullable(),
    cooldownBars: z.number().int().min(0).max(100)
  }).optional(),
  definitionDigest: z.string().regex(/^[0-9a-f]{64}$/).optional(),
  revisionOf: z.object({ strategyId: z.string().min(1), name: z.string().min(1), version: z.string().min(1) }).optional(),
  changes: z.array(z.object({ field: z.string().min(1), label: z.string().min(1), from: z.unknown(), to: z.unknown() })).max(40).optional(),
  backtestStatus: z.enum(["not_run", "passed", "failed"]).optional(),
  executionMode: z.literal("simulation_only").optional(),
  createdAt: z.string().min(1).optional(),
  validationReportId: z.string().min(1).optional(),
  validationStatus: z.enum(["incomplete", "passed", "failed"]).optional(),
  intentId: z.string().min(1).optional(),
  signalSnapshot: z.object({
    signal: z.enum(["buy", "sell", "wait"]),
    barTime: z.string().min(1),
    barState: z.literal("completed"),
    dataStatus: z.enum(["normal", "simulated"]),
    definitionDigest: z.string().regex(/^[0-9a-f]{64}$/),
    add: z.boolean().optional(),
    exitReason: z.string().optional()
  }).optional(),
  positionIntent: z.object({
    action: z.enum(["hold", "open_long", "open_short", "close_long", "close_short", "reduce_long", "reduce_short"]),
    requestedVolume: z.number().int().nonnegative(),
    currentLong: z.number().int().nonnegative(),
    currentShort: z.number().int().nonnegative(),
    targetLong: z.number().int().nonnegative(),
    targetShort: z.number().int().nonnegative(),
    reason: z.string()
  }).optional(),
  riskAdjustedIntent: z.object({
    status: z.enum(["no_action", "allowed", "blocked", "reduce_only"]),
    action: z.enum(["hold", "open_long", "open_short", "close_long", "close_short", "reduce_long", "reduce_short"]),
    volume: z.number().int().nonnegative(),
    stopPrice: finiteNumber.nullable(),
    takeProfitPrice: finiteNumber.nullable(),
    checks: z.array(z.object({
      id: z.string().min(1), label: z.string().min(1), passed: z.boolean()
    })),
    riskCheck: z.record(z.string(), z.unknown()).nullable(),
    reason: z.string(),
    executionEligible: z.literal(false),
    liveTrading: z.literal("disabled")
  }).optional()
});
const backtestSchema = z.object({
  turnId: z.string().min(1), taskId: z.string().min(1), backtestId: z.string().min(1),
  artifactDigest: z.string().regex(/^[0-9a-f]{64}$/), strategyDigest: z.string().regex(/^[0-9a-f]{64}$/),
  dataDigest: z.string().regex(/^[0-9a-f]{64}$/), engineDigest: z.string().regex(/^[0-9a-f]{64}$/),
  strategyReleaseId: z.string().min(1),
  candidateVariantId: z.string().regex(/^sha256-[0-9a-f]{64}$/),
  candidateConfigDigest: z.string().regex(/^[0-9a-f]{64}$/),
  candidateParameters: z.object({
    baselineThreshold: z.string().regex(/^\d+(?:\.\d+)?$/),
    candidateThreshold: z.string().regex(/^\d+(?:\.\d+)?$/)
  }),
  engineVersion: z.string().min(1), dataSource: z.string().min(1), generatedAt: z.string().min(1),
  reproducible: z.literal(true),
  sourceKind: z.enum(["synthetic_fixture", "tqsdk_history"]).optional(),
  dataPointCount: z.number().int().positive().optional(),
  strategyName: z.string().min(1),
  symbol: z.string().min(1), oldVersion: z.string().min(1), newVersion: z.string().min(1),
  period: z.string().min(1), metrics: z.array(z.object({
    label: z.string().min(1), before: z.string(), after: z.string(), tone: z.enum(["positive", "warning", "neutral"])
  })), oldCurve: z.array(finiteNumber), newCurve: z.array(finiteNumber), conclusion: z.string(), recommendation: z.string()
});
const strategyBacktestSchema = z.object({
  turnId: z.string().min(1), taskId: z.string().min(1), backtestId: z.string().min(1), kind: z.literal("rule"),
  strategyName: z.string().min(1), symbol: z.string().min(1), timeframeSeconds: z.number().int().positive(),
  summary: z.object({ netProfit: finiteNumber, maxDrawdown: finiteNumber, trades: z.number().int().nonnegative() }).passthrough(),
  exitReasons: z.array(z.object({ reason: z.string(), label: z.string(), count: z.number().int(), net: finiteNumber })),
  equityCurve: z.array(z.object({ time: z.number(), equity: finiteNumber })).max(400),
  trades: z.array(z.object({ id: z.number(), direction: z.enum(["long", "short"]), openTime: z.number(), closeTime: z.number(),
    net: finiteNumber }).passthrough()).max(200),
  coverage: z.object({ tradingDays: z.number().int().nonnegative(), bars: z.number().int().nonnegative() }).passthrough(),
  assumptions: z.array(z.string())
}).passthrough();
const riskCheckSchema = z.object({
  passed: z.boolean(), limit: finiteNumber.nonnegative(), estimatedLoss: finiteNumber.nonnegative(), reason: z.string(),
  contractMultiplier: finiteNumber.positive(), priceTick: z.string().regex(/^\d+(?:\.\d+)?$/),
  policyVersion: z.number().int().nonnegative().optional(),
  checks: z.record(z.string(), z.boolean()).optional()
});
const tradeProposalSchema = z.object({
  turnId: z.string().min(1), proposalId: z.string().min(1), version: z.number().int().positive(),
  symbol: z.string().min(1), name: z.string().min(1),
  side: z.enum(["buy_open", "sell_open", "buy_close", "sell_close"]),
  offset: z.enum(["OPEN", "CLOSE", "CLOSETODAY"]).optional(),
  positionBucket: z.enum(["today", "historical", "all"]).optional(),
  volume: z.number().int().positive(), limitPrice: finiteNumber.positive(), stopPrice: finiteNumber.positive().optional(),
  contractMultiplier: finiteNumber.positive(), updatedAt: z.string().min(1), environment: z.literal("simulation"),
  status: z.enum(["draft", "risk_blocked", "approval_required", "candidate_recorded", "rejected"]),
  accountLabel: z.string().min(1).optional(),
  accountId: z.string().min(1).optional(),
  currency: z.enum(["CNY", "USD"]).optional(),
  riskCheck: riskCheckSchema, executionEligibleAtCreation: z.boolean().optional(), approvalId: z.string().min(1).optional(), auditId: z.string().min(1).optional(),
  voiceConfirmBy: z.string().min(1).nullable().optional(),
  strategySource: z.object({ strategyId: z.string().min(1), name: z.string().min(1), attemptId: z.string().min(1).optional(), barTime: z.string().optional(), add: z.boolean().optional() }).optional(),
  confirmBlocked: z.object({ code: z.string().min(1), reason: z.string().min(1), at: z.string().optional() }).optional(),
  protection: z.object({ stopLossPrice: z.string().min(1).optional(), takeProfitPrice: z.string().min(1).optional() }).optional(),
  remainingProtection: z.lazy(() => remainingProtectionSchema).optional()
});
const remainingLevelSchema = z.object({
  action: z.enum(["set", "remove", "keep"]), price: z.string().min(1).nullable().optional(), atCost: z.boolean().optional()
});
const protectionBucketLevelsSchema = z.object({
  positionBucket: z.enum(["today", "historical", "all"]), volume: z.number().int().nonnegative(),
  stopLoss: z.string().min(1).nullable(), takeProfit: z.string().min(1).nullable()
}).passthrough();
const remainingProtectionSchema = z.object({
  direction: z.enum(["long", "short"]), remainingVolume: z.number().int().positive(),
  stopLoss: remainingLevelSchema, takeProfit: remainingLevelSchema,
  original: z.object({ stopLoss: z.string().nullable().optional(), takeProfit: z.string().nullable().optional() }),
  perBucketOriginal: z.array(protectionBucketLevelsSchema).max(3).nullish(),
  cost: z.string().nullable().optional(),
  status: z.enum(["pending", "awaiting_close", "done", "failed", "skipped"]),
  reason: z.string().nullable().optional(),
  result: z.record(z.string(), z.unknown()).nullable().optional(),
  actions: z.array(z.lazy(() => cardActionSchema)).max(4).optional()
}).passthrough();
const closeBatchOrderSchema = z.object({
  symbol: z.string().min(1), name: z.string().min(1), side: z.enum(["buy_close", "sell_close"]),
  offset: z.enum(["CLOSE", "CLOSETODAY"]), positionBucket: z.enum(["today", "historical", "all"]),
  volume: z.number().int().positive(), limitPrice: finiteNumber.positive(), contractMultiplier: finiteNumber.positive(),
  riskCheck: riskCheckSchema, clientOrderId: z.string().min(1).nullable().optional()
});
const closeBatchProposalSchema = z.object({
  turnId: z.string().min(1), proposalId: z.string().min(1), version: z.number().int().positive(),
  environment: z.literal("simulation"), reason: z.string(), orders: z.array(closeBatchOrderSchema).min(1),
  skipped: z.array(z.record(z.string(), z.unknown())), totalOrders: z.number().int().positive(), totalVolume: z.number().int().positive(),
  riskCheck: z.record(z.string(), z.unknown()), updatedAt: z.string().min(1),
  status: z.enum(["draft", "risk_blocked", "approval_required", "candidate_recorded", "rejected"]),
  accountLabel: z.string().min(1).optional(),
  accountId: z.string().min(1).optional(),
  approvalId: z.string().min(1).nullable().optional(), auditId: z.string().min(1).nullable().optional(),
  voiceConfirmBy: z.string().min(1).nullable().optional(),
  remainingProtection: remainingProtectionSchema.optional(),
  linkedProposalIds: z.array(z.string().min(1)).optional()
});
const spreadLegOrderSchema = z.object({
  symbol: z.string().min(1), name: z.string().min(1),
  side: z.enum(["buy_open", "sell_open", "buy_close", "sell_close"]),
  offset: z.enum(["OPEN", "CLOSE", "CLOSETODAY"]),
  positionBucket: z.enum(["today", "historical", "all"]).nullish(),   // an open leg has none
  volume: z.number().int().positive(), limitPrice: finiteNumber.positive(), contractMultiplier: finiteNumber.positive(),
  riskCheck: riskCheckSchema, clientOrderId: z.string().min(1).nullable().optional()
}).passthrough();
const spreadProposalSchema = z.object({
  turnId: z.string().min(1), proposalId: z.string().min(1), version: z.number().int().positive(),
  environment: z.literal("simulation"), reason: z.string(), accountId: z.string().min(1).optional(),
  spread: z.record(z.string(), z.unknown()), spreadWords: z.string().min(1), legsWords: z.string().min(1),
  action: z.enum(["open_long", "open_short", "close_long", "close_short"]), units: z.number().int().positive(),
  // nullish: a card from before 21e727e+1 omits the unknown ones (it was dropped whole — 线上 09-29).
  currentSpread: finiteNumber.nullish(), entrySpread: finiteNumber.nullish(),
  exitReason: z.enum(["spread_stop", "spread_take"]).nullish(),
  orders: z.array(spreadLegOrderSchema).min(1), totalOrders: z.number().int().positive(),
  totalVolume: z.number().int().positive(), riskCheck: z.record(z.string(), z.unknown()),
  updatedAt: z.string().min(1),
  status: z.enum(["draft", "risk_blocked", "approval_required", "candidate_recorded", "rejected"]),
  approvalId: z.string().min(1).nullable().optional(), auditId: z.string().min(1).nullable().optional(),
  voiceConfirmBy: z.string().min(1).nullable().optional(),
  strategySource: z.object({ strategyId: z.string().min(1), name: z.string().min(1),
    attemptId: z.string().min(1).optional(), barTime: z.string().min(1).optional() }).nullable().optional(),
  simulationOrders: z.array(z.unknown()).optional(), execution: z.record(z.string(), z.unknown()).optional(),
  executionConclusion: z.object({ code: z.string().min(1), text: z.string().min(1) }).nullable().optional(),
  confirmBlocked: z.object({ code: z.string().min(1), reason: z.string().min(1), at: z.string().optional() }).optional()
}).passthrough();
const riskRuleProposalSchema = z.object({
  turnId: z.string().min(1), proposalId: z.string().min(1), version: z.number().int().positive(),
  basePolicyVersion: z.number().int().nonnegative(), policyVersion: z.number().int().positive(),
  allowedInstruments: z.array(z.object({ symbol: z.string().min(1), name: z.string().min(1) })).min(1),
  maxLossPerTrade: z.string().regex(/^\d+(?:\.\d{1,2})?$/),
  maxDailyLoss: z.string().regex(/^\d+(?:\.\d{1,2})?$/),
  noNewPositionsAfter: z.string().regex(/^(?:[01]\d|2[0-3]):[0-5]\d$/),
  timezone: z.literal("Asia/Shanghai"), rulesDigest: z.string().regex(/^[0-9a-f]{64}$/),
  deterministic: z.literal(true), executionEnabled: z.literal(false),
  status: z.enum(["approval_required", "risk_rules_applied", "rejected"]),
  approvalId: z.string().min(1), policyId: z.string().min(1).optional(), auditId: z.string().min(1).optional()
});
const proposalApprovalSchema = z.object({
  proposalId: z.string().min(1), approvalId: z.string().min(1), proposalVersion: z.number().int().positive(),
  proposalType: z.enum(["trade", "risk_rule", "cancel"])
});
const proposalResultSchema = z.object({
  proposalId: z.string().min(1), status: z.enum(["candidate_recorded", "risk_rules_applied", "rejected"]),
  auditId: z.string().min(1), message: z.string(), reason: z.enum(["expired", "superseded"]).nullish()
});
const warningSchema = z.object({
  turnId: z.string().min(1), title: z.string().min(1), message: z.string().min(1),
  severity: z.enum(["warning", "danger"]), deterministic: z.boolean()
});
const errorSchema = z.object({
  turnId: z.string().optional().nullable(), runId: z.string().optional().nullable(),
  code: z.string().min(1), message: z.string().min(1),
  scope: z.enum(["connection", "agent", "task", "validation", "trading"]).default("task")
});
const toolSchema = z.object({
  turnId: z.string().min(1), taskId: z.string().min(1), itemId: z.string().min(1), toolType: z.enum(["command", "function"]),
  status: z.enum(["running", "completed", "failed"]), command: z.string().optional(), cwd: z.string().optional(),
  exitCode: z.number().int().nullable().optional(), outputSummary: z.string().optional()
});
const fileChangeSchema = z.object({ path: z.string().min(1), kind: z.string().min(1), diff: z.string() });
const diffSchema = z.object({
  turnId: z.string().min(1), taskId: z.string().min(1), itemId: z.string().min(1),
  status: z.enum(["running", "completed", "failed"]), changes: z.array(fileChangeSchema)
});
const agentResultSchema = z.object({
  turnId: z.string().min(1), taskId: z.string().min(1), threadId: z.string().min(1),
  status: z.enum(["completed", "failed", "cancelled"]), model: z.string().nullable().optional()
});
const record = z.record(z.string(), z.unknown());
const feedAccountSchema = z.object({ account: record, positions: z.array(record), activity: record, risk: record }).passthrough();
const feedMarketSchema = z.object({
  schemaVersion: z.literal(1), durationSeconds: z.union([z.literal(60), z.literal(180), z.literal(300), z.literal(900), z.literal(1800), z.literal(3600), z.literal(86400), z.literal(604800)]),
  quote: record, bars: z.array(record), liveTrading: z.literal("disabled"),
  // Bar times are epoch nanoseconds, beyond the safe-integer range zod's int() allows: finite numbers.
  barsAfter: z.number().optional(),
  barsWindow: z.object({ first: z.number(), count: z.number().int().min(1), digest: z.string().min(1) }).passthrough().optional()
}).passthrough().refine(value => (value.barsAfter === undefined) === (value.barsWindow === undefined),
  { message: "barsAfter and barsWindow come together" });
const feedMarketPatchSchema = z.object({
  revision: z.string().min(1), base: z.string().min(1),
  durationSeconds: z.union([z.literal(60), z.literal(180), z.literal(300), z.literal(900), z.literal(1800), z.literal(3600), z.literal(86400), z.literal(604800)]),
  changes: record
});
const feedTouchSchema = z.object({
  name: z.enum(["feed.account", "feed.activity", "feed.execution"]),
  stamps: z.array(z.tuple([z.array(z.union([z.string(), z.number()])), z.unknown()]))
});
const feedBoardSchema = z.object({ feedRevision: z.string().min(1), key: z.string(), rows: z.array(record) }).passthrough();
const feedBoardPatchSchema = z.object({
  feedRevision: z.string().min(1), base: z.string().min(1), key: z.string(), meta: record, rows: z.record(z.string(), record)
});
const feedActivitySchema = z.object({
  environment: z.enum(["replay", "simulation"]), symbol: z.string(), orders: z.array(record), fills: z.array(record), strategies: z.array(record)
}).passthrough();
const feedExecutionSchema = z.object({ stops: z.array(record), simulationOnly: z.literal(true), liveTrading: z.literal("disabled") }).passthrough();
const feedUiNudgeSchema = z.object({ reason: z.string().min(1) });
const feedDebateSchema = z.object({ id: z.string().min(1), turn: z.number().int().min(1).max(4).optional(), side: z.enum(["bull", "bear"]).optional(),
  analyst: z.enum(["technical", "supply", "flow", "news"]).optional(), text: z.string() });
const feedStatusSchema = z.object({
  channel: z.enum(["account", "market", "activity", "execution", "board"]), status: z.literal("unavailable"), reason: z.string().min(1)
});
const feedStrategySignalSchema = z.object({
  strategyId: z.string().min(1), name: z.string().min(1), symbol: z.string().min(1),
  action: z.enum(["open_long", "open_short", "close_long", "close_short"]), actionLabel: z.string().min(1),
  volume: z.number().int().nonnegative(), barTime: z.number().nullable(), evaluatedBarTime: z.string().nullable(),
  validated: z.literal(true), annotations: z.array(record), spoken: z.string().min(1),
  executionMode: z.literal("simulation_only"),
});
const feedPriceAlertSchema = z.object({
  id: z.string().min(1), symbol: z.string().min(1), name: z.string().min(1),
  direction: z.enum(["above", "below"]), price: z.string().min(1),
  lastPrice: z.number().nullable(), note: z.string(), text: z.string().min(1),
  triggeredAt: z.string().nullable(),
});

const feedReminderSchema = z.object({
  kind: z.string().min(1), title: z.string().min(1), text: z.string().min(1), at: z.string().min(1),
  // #932 统一提醒：提醒记录 id、新提醒/状态更新、当前交易日未读数。
  id: z.string().min(1).optional(), type: z.enum(["new", "update"]).optional(),
  unread: z.number().int().nonnegative().optional(),
  // #457: 提醒带账户维度——多账户用户的两条提醒各自的账户；单账户/旧 Runtime 不带。
  accountId: z.string().min(1).optional(), accountName: z.string().min(1).optional(),
  // 多 AI 研讨 (2026-10-11): kind research-debate names the debate that ended.
  debateId: z.string().min(1).optional(),
});

// The full simulation-order view rides along (extra fields pass through); only the anchors pin.
const feedOrderSchema = z.object({
  clientOrderId: z.string().min(1), accountId: z.string().min(1), displayStatus: z.string().min(1),
}).passthrough();

const cardActionSchema = z.union([
  z.object({ kind: z.literal("ask"), label: z.string().min(1).max(40), prompt: z.string().min(1).max(200), changes: z.null().optional() }).passthrough(),
  z.object({ kind: z.literal("ui"), label: z.string().min(1).max(40), changes: z.record(z.string(), z.unknown()), prompt: z.null().optional() }).passthrough(),
  z.object({ kind: z.literal("breakeven"), label: z.string().min(1).max(40), breakevenId: z.string().min(1), choice: z.enum(["move", "decline"]),
    prompt: z.null().optional(), changes: z.null().optional() }).passthrough(),
]);
const orderQuestionSchema = z.object({
  turnId: z.string().min(1), cardId: z.string().min(1), kind: z.enum(["ask", "no_card"]),
  title: z.string().min(1), question: z.string().min(1), missing: z.array(z.string()).max(6).optional(),
  options: z.array(z.object({ label: z.string().min(1), prompt: z.string().min(1) })).max(4).optional(),
  reason: z.string().nullable().optional(), userText: z.string().nullable().optional(),
  actions: z.array(cardActionSchema).max(4).optional(), createdAt: z.string().min(1)
}).passthrough();
const judgmentLineSchema = z.object({ text: z.string().min(1), quote: z.string().optional(), source: z.string().optional(), asOf: z.string().nullable().optional() }).passthrough();
const researchJudgmentSchema = z.object({
  turnId: z.string().min(1), cardId: z.string().min(1), symbol: z.string().min(1), productName: z.string().min(1),
  status: z.enum(["pending", "ready", "unavailable"]),
  stance: z.enum(["偏多", "偏空", "震荡", "不明"]).nullable().optional(), confidence: z.enum(["低", "中", "高"]).nullable().optional(),
  headline: z.string().nullable().optional(), facts: z.array(judgmentLineSchema).max(4).optional(),
  invalidation: z.array(judgmentLineSchema).max(3).optional(), generatedAt: z.string().nullable().optional(),
  researchDay: z.string().nullable().optional(), note: z.string().nullable().optional(),
  actions: z.array(cardActionSchema).max(4).optional()
}).passthrough();
const cardFigureSchema = z.object({ label: z.string().min(1), value: z.string().min(1), tone: z.enum(["positive", "warning", "danger"]).nullable().optional() });
const reminderCardSchema = z.object({
  turnId: z.string().min(1), cardId: z.string().min(1),
  kind: z.enum(["price_alert", "risk_ratio", "market_close", "delivery", "protection", "order_unfilled", "market_movement", "daily_review", "breakeven"]),
  title: z.string().min(1), summary: z.string().min(1), figures: z.array(cardFigureSchema).max(8).optional(),
  symbol: z.string().nullable().optional(), at: z.string().min(1), actions: z.array(cardActionSchema).max(4).optional()
}).passthrough();
const answerFigureSchema = z.object({ label: z.string().min(1), value: z.string().min(1), unit: z.string().nullable().optional(),
  dir: z.enum(["up", "down"]).nullable().optional() });
const answerBasisSchema = z.object({ at: z.string().min(1), sources: z.array(z.string()).max(8) });
const answerSideSchema = z.object({ title: z.string().min(1), lines: z.array(z.string()).max(4) });
const answerCardSchema = z.object({
  turnId: z.string().min(1), cardId: z.string().min(1), title: z.string().min(1), conclusion: z.string().min(1),
  figures: z.array(answerFigureSchema).max(6).optional(),
  compare: z.object({ left: answerSideSchema, right: answerSideSchema }).nullable().optional(),
  reasons: z.array(z.string().min(1)).max(3).optional(), risk: z.string().nullable().optional(),
  symbol: z.string().nullable().optional(), basis: answerBasisSchema, actions: z.array(cardActionSchema).max(2).optional(),
  warning: z.string().nullable().optional(), folded: z.array(z.string().min(1)).max(12).optional()
}).passthrough();
const dataCardSchema = z.object({
  turnId: z.string().min(1), cardId: z.string().min(1), kind: z.enum(["positions", "orders", "news", "analysis", "quotes"]),
  title: z.string().min(1), figures: z.array(answerFigureSchema).max(6).optional(),
  rows: z.array(z.object({ primary: z.string().min(1), secondary: z.string().nullable().optional(), value: z.string().nullable().optional(),
    dir: z.enum(["up", "down"]).nullable().optional(), tag: z.string().nullable().optional() })).max(12).optional(),
  note: z.string().nullable().optional(), empty: z.string().nullable().optional(), symbol: z.string().nullable().optional(),
  warning: z.string().nullable().optional(),
  basis: answerBasisSchema, actions: z.array(cardActionSchema).max(2).optional()
}).passthrough();
const executionCardSchema = z.object({
  turnId: z.string().min(1), cardId: z.string().min(1), source: z.enum(["ai", "strategy"]), sourceLabel: z.string().min(1),
  orderKind: z.enum(["order", "stop"]), symbol: z.string().min(1), name: z.string().min(1), sideLabel: z.string().min(1),
  volume: z.number().int().nonnegative(), price: z.string().nullable().optional(), reason: z.string().optional(),
  status: z.enum(["submitted", "partial", "filled", "unfilled", "rejected", "unknown", "armed", "failed"]),
  statusLabel: z.string().min(1), filledVolume: z.number().int().nonnegative().nullable().optional(),
  clientOrderId: z.string().nullable().optional(), detail: z.string().nullable().optional(), updatedAt: z.string().min(1)
}).passthrough();

const routineStepSchema = z.object({
  kind: z.enum(["read", "draw", "backtest", "margin", "card", "alert", "watch", "review"]),
  kindLabel: z.string().min(1).max(8), text: z.string().min(1).max(60), manner: z.enum(["advisory", "conditional"]).nullish()
}).passthrough();
const routineCardSchema = z.object({
  turnId: z.string().min(1), cardId: z.string().min(1), draftId: z.string().min(1), routineId: z.string().nullable().optional(),
  mode: z.enum(["create", "update"]), name: z.string().min(1).max(12), steps: z.array(routineStepSchema).min(1).max(10),
  status: z.enum(["draft", "saved", "refused"]), refusal: z.string().nullable().optional(),
  savedAt: z.string().nullable().optional(), note: z.string().nullable().optional()
}).passthrough();
const routineRunCardSchema = z.object({
  turnId: z.string().min(1), cardId: z.string().min(1), routineId: z.string().min(1), name: z.string().min(1).max(12),
  status: z.enum(["running", "completed", "stopped", "cancelled"]),
  steps: z.array(routineStepSchema.extend({
    status: z.enum(["waiting", "active", "done", "confirm", "failed", "skipped"]), result: z.string().max(120).nullable().optional()
  })).min(1).max(10),
  startedAt: z.string().min(1), finishedAt: z.string().nullable().optional(),
  elapsedSeconds: z.number().finite().nonnegative(), confirmCount: z.number().int().nonnegative()
}).passthrough();

const eventDefinitions: Record<string, { kind: UiEventKind; payload: z.ZodType }> = {
  "session.ready": { kind: "result", payload: z.object({ mode: z.enum(["local", "cloud"]), agentId: z.enum(["native", "claude", "kimi", "dsh", "workbuddy"]) }) },
  "runtime.status": { kind: "result", payload: z.object({
    mode: z.enum(["local", "cloud"]), adapter: z.enum(["fake-agent", "native-agent", "historical-replay"]),
    accountMode: z.enum(["fake", "tqkq-readonly", "paper"]), runtimeConnected: z.boolean(),
    agentConnected: z.boolean(),
    marketDataStatus: z.enum(["simulated", "connecting", "normal", "stale", "unavailable", "backup"]),
    marketDataSource: z.string().optional(),
    marketSessionStatus: z.enum(["trading", "closed", "unknown"]).optional(),
    accountStatus: z.enum(["simulated", "connecting", "normal", "stale", "unavailable"]),
    tradingGatewayStatus: z.enum(["disabled", "read_only", "simulation", "unavailable"]), simulationTrading: z.boolean().optional(),
    dataSource: z.enum(["historical", "tqkq", "paper", "demo"]).optional(),
    executionMode: z.enum(["simulation", "read_only"]).optional(),
    accountLabel: z.string().optional(), executionLabel: z.string().optional(), capabilitySummary: z.string().optional(),
    riskStatus: z.enum(["normal", "warning", "unknown"]), dataErrorCode: z.string().nullable().optional(),
    dataConnectionStage: z.enum(["starting", "loading_sdk", "authenticating", "subscribing", "discovering_instruments", "waiting_snapshot", "waiting_account", "waiting_market", "reconnecting", "ready", "failed"]).nullable().optional(),
    dataInitializationSeconds: z.number().finite().nonnegative().nullable().optional(),
    dataStartupTimings: z.record(z.string(), z.number().finite().nonnegative()).nullable().optional(),
    dataStartupTotalSeconds: z.number().finite().nonnegative().nullable().optional(),
    agentNotice: z.string().nullable().optional(),
    connected: z.boolean(), version: z.string().min(1),
    account: z.object({
      id: z.string().min(1), displayName: z.string(), role: z.string(),
      environment: z.string(), executionOwner: z.boolean(),
      poolStatus: z.enum(["connecting", "queued", "connected", "offline"]).optional(),
      status: z.enum(["connecting", "queued", "connected", "offline"]).optional(),
      currency: z.enum(["CNY", "USD"]).optional(),
      foreignAccountId: z.string().min(1).optional()
    }).optional(),
    // The session's account revision (#932 follow-up): kept, not stripped, so the next switch can name it.
    accountGeneration: z.number().int().nonnegative().optional()
  }) },
  "message.delta": { kind: "message", payload: messageSchema },
  "message.completed": { kind: "message", payload: messageSchema },
  "message.superseded": { kind: "message", payload: messageSupersededSchema },
  "task.started": { kind: "progress", payload: taskSchema },
  "task.progress": { kind: "progress", payload: taskSchema },
  "task.completed": { kind: "progress", payload: taskSchema },
  "card.account": { kind: "artifact", payload: accountSchema },
  "card.market": { kind: "artifact", payload: marketSchema },
  "card.strategy": { kind: "artifact", payload: strategySchema },
  "card.backtest": { kind: "chart", payload: backtestSchema },
  "card.strategy_backtest": { kind: "chart", payload: strategyBacktestSchema },
  "card.order_question": { kind: "artifact", payload: orderQuestionSchema },
  "card.research_judgment": { kind: "artifact", payload: researchJudgmentSchema },
  "card.reminder": { kind: "artifact", payload: reminderCardSchema },
  "card.execution": { kind: "artifact", payload: executionCardSchema },
  "card.answer": { kind: "artifact", payload: answerCardSchema },
  "card.data": { kind: "artifact", payload: dataCardSchema },
  "card.routine": { kind: "artifact", payload: routineCardSchema },
  "card.routine_run": { kind: "artifact", payload: routineRunCardSchema },
  "proposal.trade": { kind: "approval", payload: tradeProposalSchema },
  "proposal.close_batch": { kind: "approval", payload: closeBatchProposalSchema },
  "proposal.spread": { kind: "approval", payload: spreadProposalSchema },
  "proposal.risk_rule": { kind: "approval", payload: riskRuleProposalSchema },
  "trade.approval.required": { kind: "approval", payload: proposalApprovalSchema },
  "risk_rule.approval.required": { kind: "approval", payload: proposalApprovalSchema },
  "proposal.result": { kind: "result", payload: proposalResultSchema },
  "agent.tool.started": { kind: "tool_call", payload: toolSchema },
  "agent.tool.progress": { kind: "tool_call", payload: toolSchema },
  "agent.tool.completed": { kind: "tool_call", payload: toolSchema },
  "agent.diff": { kind: "diff", payload: diffSchema },
  "agent.result": { kind: "result", payload: agentResultSchema },
  "feed.account": { kind: "result", payload: feedAccountSchema },
  "feed.market": { kind: "result", payload: feedMarketSchema },
  "feed.market-patch": { kind: "result", payload: feedMarketPatchSchema },
  "feed.touch": { kind: "result", payload: feedTouchSchema },
  "feed.board": { kind: "result", payload: feedBoardSchema },
  "feed.board-patch": { kind: "result", payload: feedBoardPatchSchema },
  "feed.activity": { kind: "result", payload: feedActivitySchema },
  "feed.execution": { kind: "result", payload: feedExecutionSchema },
  "feed.ui": { kind: "result", payload: feedUiNudgeSchema },
  "feed.debate": { kind: "result", payload: feedDebateSchema },
  "feed.status": { kind: "result", payload: feedStatusSchema },
  "feed.strategy-signal": { kind: "result", payload: feedStrategySignalSchema },
  "feed.price-alert": { kind: "result", payload: feedPriceAlertSchema },
  "feed.reminder": { kind: "result", payload: feedReminderSchema },
  "feed.order": { kind: "result", payload: feedOrderSchema },
  "warning": { kind: "warning", payload: warningSchema },
  "error": { kind: "error", payload: errorSchema }
};

const envelopeSchema = z.object({
  schemaVersion: z.literal(1), id: z.string().min(1), sessionId: z.string().min(1),
  kind: z.enum(["message", "progress", "tool_call", "artifact", "chart", "diff", "approval", "result", "warning", "error"]),
  name: z.string().min(1), timestamp: z.string().min(1), payload: z.unknown()
});

export type UiEventParseResult =
  | { ok: true; event: UiEvent }
  | { ok: false; warning: { code: "invalid_event" | "unknown_event"; message: string } };

export function parseUiEvent(input: unknown): UiEventParseResult {
  const parsed = envelopeSchema.safeParse(input);
  if (!parsed.success) {
    return { ok: false, warning: { code: "invalid_event", message: "Runtime 返回了不符合契约的事件，已忽略。" } };
  }
  const definition = eventDefinitions[parsed.data.name];
  if (!definition) {
    return { ok: false, warning: { code: "unknown_event", message: `Runtime 返回未知事件 ${parsed.data.name}，已忽略。` } };
  }
  if (definition.kind !== parsed.data.kind || !definition.payload.safeParse(parsed.data.payload).success) {
    return { ok: false, warning: { code: "invalid_event", message: `Runtime 事件 ${parsed.data.name} 内容无效，已忽略。` } };
  }
  return { ok: true, event: parsed.data as UiEvent };
}

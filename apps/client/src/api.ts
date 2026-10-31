import {
  parseUiEvent,
  type CloseBatchProposal,
  type OrderEstimate,
  type RuntimeMode,
  type TargetPositionPlan,
  type TradeProposal,
  type UiEvent
} from "@ai-trader/ui-events";
import { configuredRuntimeUrl, resolveRuntimeConnection, isTauriRuntime } from "./runtimeConnection";
import { activeAgentWindow, webDeviceId } from "./nativeAgent";

import { changeLogin, resetReadIdentity, type Brief } from "./runtimeClient";
import { openSseStream } from "./sse";
import { clientHeaderValue, noteRuntimeCompat } from "./clientBuild";
import { activeTradingAccount, setActiveTradingAccount } from "./tradingAccountScope";
import { currentServiceWorkerRegistration } from "./serviceWorkerPolicy";
import { withRequestTimeout, type RequestTimeoutOption } from "./requestTimeout";

const normalizeUrl = (url: string) => url.replace(/\/$/, "");
const idempotencyKey = () => crypto.randomUUID();
const localAuthTokens = new Map<string, string>();

// The account the attached session acts on (X-AI-Trader-Account): see tradingAccountScope.
export { setActiveTradingAccount };

/** The local Runtime token for a URL, for transports that cannot carry an Authorization header (WebSocket). */
export function localAuthTokenFor(url: string): string | undefined {
  for (const [baseUrl, token] of localAuthTokens) {
    const absoluteBase = new URL(baseUrl || "/", window.location.href).href.replace(/\/$/, "");
    if (url === baseUrl || url.startsWith(`${baseUrl}/`) || url === absoluteBase || url.startsWith(`${absoluteBase}/`)) return token;
  }
  return undefined;
}

export function runtimeUrl(mode: RuntimeMode): string {
  return normalizeUrl(configuredRuntimeUrl(mode) || "");
}

export function runtimeHeaders(url: string, input?: HeadersInit) {
  const headers = new Headers(input);
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const localToken = localAuthTokenFor(url);
  if (localToken && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${localToken}`);
  const accountId = activeTradingAccount();
  if (accountId && !headers.has("X-AI-Trader-Account")) headers.set("X-AI-Trader-Account", accountId);
  if (!headers.has("X-AI-Trader-Client")) headers.set("X-AI-Trader-Client", clientHeaderValue());
  return headers;
}

/** Statuses that cannot carry a body (a rebuilt Response with one would throw). */
const NULL_BODY_STATUS = new Set([101, 103, 204, 205, 304]);

/** The generated workbench client's transport; reads get the same default time limit as requestJson.
 *  The body is read inside the timed window and handed back buffered, so a response whose headers
 *  arrived but whose body stalls still ends with the timeout instead of hanging openapi-fetch. */
export async function runtimeFetch(input: RequestInfo | URL, init?: RequestInit & RequestTimeoutOption): Promise<Response> {
  const { timeoutMs, ...rest } = init ?? {};
  const request = new Request(input, rest);
  return withRequestTimeout({ method: request.method, signal: request.signal, timeoutMs }, async signal => {
    const response = await fetch(new Request(request, { credentials: "include", headers: runtimeHeaders(request.url, request.headers), ...(signal ? { signal } : {}) }));
    noteRuntimeCompat(response.headers);
    const body = request.method === "HEAD" || NULL_BODY_STATUS.has(response.status) ? null : await response.arrayBuffer();
    return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
  });
}

export type RequestJsonInit = RequestInit & RequestTimeoutOption;

export async function requestJson<T>(url: string, init?: RequestJsonInit): Promise<T> {
  const { timeoutMs, ...rest } = init ?? {};
  return withRequestTimeout({ method: rest.method, signal: rest.signal, timeoutMs },
    signal => fetchJson<T>(url, signal ? { ...rest, signal } : rest));
}

async function fetchJson<T>(url: string, init: RequestInit): Promise<T> {
  const response = await fetch(url, { ...init, credentials: "include", headers: runtimeHeaders(url, init.headers) });
  noteRuntimeCompat(response.headers);
  if (!response.ok) {
    const detail = await response.text();
    let body: unknown;
    try { body = JSON.parse(detail); } catch { body = undefined; }
    throw Object.assign(new Error(`${response.status} ${detail}`), { status: response.status, detail: body });
  }
  return response.json() as Promise<T>;
}

export async function authenticateLocal(baseUrl: string, token: string) {
  if (!token) throw new Error("本机应用服务未正确启动");
  const normalized = normalizeUrl(baseUrl);
  const result = await requestJson<{ status: string }>(`${normalized}/api/auth/local`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` }
  });
  localAuthTokens.clear();
  localAuthTokens.set(normalized, token);
  resetReadIdentity();
  return result;
}

export async function authenticateCloud(baseUrl: string, token: string) {
  if (!token) {
    const result = await requestJson<{
      authenticated: boolean; authMode: string; authProvider: string;
      tenantId?: string; userId?: string;
    }>(`${baseUrl}/api/auth/cloud/status`);
    if (!result.authenticated) throw new Error("cloud_auth_required");
    resetReadIdentity();
    return result;
  }
  const result = await requestJson<{ status: string; tenantId: string; userId: string }>(`${baseUrl}/api/auth/cloud`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` }
  });
  resetReadIdentity();
  return result;
}

export interface OwnerCredentialBindings {
  profileId: string;
  tqkqConfigured: boolean;
  tqkqMode: "simulation" | "read-only";
  modelProvider: "deepseek" | "moonshot";
  modelCredentialConfigured: boolean;
  /** 本站模拟账户 (2026-10-03): this deployment runs the paper engine. Older Runtimes omit it. */
  paperConfigured?: boolean;
  liveTrading: false;
}

/** How the login sheet offers the TqKq direct login (M1b): enabled only when
 *  the Runtime runs an access pool; the invite code is only for the first login. */
export interface TqkqLoginInfo {
  enabled: boolean;
  inviteRequired: boolean;
  /** TqKq's sign-up page. Still sent for older desktop clients; this client has its own 注册 form (2026-10-05). */
  registerUrl: string;
}

/** M2: the caller's tier feature switches (owner-password Runtime; every
 *  switch true for the owner and for local single-user runtimes). The 1.6.7
 *  clients only read the first three keys; the rest are additive and
 *  `strategyTools` stays as their combined strategy/backtest/replay switch. */
export interface PlanFeatures {
  aiExecution: boolean;
  webResearch: boolean;
  strategyTools: boolean;
  marketBriefs: boolean;
  strategies: boolean;
  backtests: boolean;
  replays: boolean;
  voice: boolean;
}

export interface OwnerAuthStatus {
  authenticated: boolean;
  authMode: "owner-password";
  authProvider: "owner-password";
  username?: string;
  userId?: string;
  role?: string;
  /** 设置 → 账户 (2026-10-21): signed in with a site password (administrator or a site account) — it can
   *  be changed here; false for a TqKq login, whose password lives with TqKq. Absent on an older Runtime. */
  passwordLogin?: boolean;
  plan?: string;
  planLabel?: string;
  marketHistoryTradingDays?: number | null;
  features?: PlanFeatures;
  bindings?: OwnerCredentialBindings;
  tqkqLogin?: TqkqLoginInfo;
  /** 本站模拟盘 (2026-10-03): the Runtime has a paper engine configured — registration
   *  provisions a site-hosted simulation account (TqKq binding optional). Absent on an
   *  older Runtime or when the engine is not configured. */
  paperConfigured?: boolean;
}

export function setOwnerSessionToken(baseUrl: string, token: string) {
  ownerStatusReads.clear();
  if (token) localAuthTokens.set(normalizeUrl(baseUrl), token);
  else localAuthTokens.delete(normalizeUrl(baseUrl));
}

/** The latest owner-status read per Runtime; cleared by every sign-in, sign-out and registration. */
const ownerStatusReads = new Map<string, { at: number; read: Promise<OwnerAuthStatus> }>();

/** `maxAgeMs`: reuse a read that started this recently. Opening the page asked the same question
 *  three times in a row (guest entry, session connect, tier), one serial round trip each. */
export async function getOwnerAuthStatus(baseUrl: string, { maxAgeMs = 0 }: { maxAgeMs?: number } = {}) {
  const key = normalizeUrl(baseUrl);
  const previous = ownerStatusReads.get(key);
  if (previous && Date.now() - previous.at < maxAgeMs) return previous.read;
  const read = requestJson<OwnerAuthStatus>(`${baseUrl}/api/auth/owner/status`);
  ownerStatusReads.set(key, { at: Date.now(), read });
  read.catch(() => { if (ownerStatusReads.get(key)?.read === read) ownerStatusReads.delete(key); });
  return read;
}

/** One registry skill as the current user sees it (GET /api/agent/skills):
 *  `available` plus the machine-readable reason the greyed-out buttons show. */
export interface AgentSkill {
  id: string;
  label: string;
  category: string;
  description: string;
  tierGated: boolean;
  ownerOnly: boolean;
  /** The parent skill this one follows (empty on Runtimes before 2026-09-30): an
   *  unavailable parent blocks its children, and the refusal line names the parent. */
  parent?: string | null;
  available: boolean;
  unavailableReason: "disabled" | "not_in_plan" | "owner_only" | null;
  /** The skill is itself an AI feature: switched off, it stops for the user too. Otherwise the
   *  switch only takes it away from the AI. Missing on Runtimes before 2026-09-27. */
  aiFeature?: boolean;
  phrases: string[];
  uiEntries: string[];
  quickAction: string | null;
}

export async function getAgentSkills(baseUrl: string) {
  return requestJson<{ items: AgentSkill[] }>(`${baseUrl}/api/agent/skills`);
}

/** A sign-out's retirement of the browser's push subscription, until the next 打开 takes it over. */
export interface PushRetirement { done: Promise<void>; superseded: boolean }
let pushRetirement: PushRetirement | undefined;
/** 锁屏提醒 (#521): the pending (or finished) sign-out retirement, handed once to the next 打开 — which
 *  waits for it, marks it superseded (so a late chain no longer touches the new user's subscription)
 *  and does not reuse the subscription it was retiring. */
export function takePushRetirement(): PushRetirement | undefined {
  const taken = pushRetirement;
  pushRetirement = undefined;
  return taken;
}

/** 锁屏提醒 (#521): the browser's own push subscription goes with a sign-out (best effort; the Runtime
 *  drops this device's row anyway). Each step stops once a newer 打开 has taken over. */
function dropBrowserPushSubscription() {
  const retirement: PushRetirement = { done: Promise.resolve(), superseded: false };
  try {
    retirement.done = currentServiceWorkerRegistration()
      .then(registration => retirement.superseded ? undefined : registration?.pushManager?.getSubscription())
      .then(subscription => retirement.superseded ? undefined : subscription?.unsubscribe())
      .then(() => undefined, () => undefined);
  } catch { /* no service worker here */ }
  pushRetirement = retirement;
}

export async function signOutOwner(baseUrl: string) {
  changeLogin();
  resetReadIdentity();
  ownerStatusReads.clear();
  // This device stops taking the signed-out user's lock-screen push; their other devices keep it.
  const device = isTauriRuntime() ? undefined : { deviceId: webDeviceId() };
  if (device) dropBrowserPushSubscription();
  try {
    return await requestJson<{ status: "signed_out" }>(`${baseUrl}/api/auth/owner/logout`, device
      ? { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(device) } : { method: "POST" });
  } finally {
    setOwnerSessionToken(baseUrl, "");
    if (isTauriRuntime()) {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("clear_cloud_connection");
    }
    // Also discard any read that completed while the logout request was pending.
    resetReadIdentity();
  }
}

/** Where a pool-managed account's child is (M1a): the header message and the
 *  settings row read this until it reaches `connected`. */
export type TqkqPoolStatus = "connecting" | "queued" | "connected" | "offline";

/** `/api/auth/tqkq` answers the administrator (or a share-code account) typed into the same
 *  form with the password login's result — `status: "authenticated"` plus `bindings`. */
export type TqkqLoginResult = TqkqPoolLogin | {
  status: "authenticated"; userId: string; username: string; role: string;
  bindings: OwnerCredentialBindings; accessToken?: string;
};

interface TqkqPoolLogin {
  status: "logged_in";
  userId: string;
  username: string;
  role: string;
  accountId: string;
  assembly: TqkqPoolStatus;
  /** The name logged in for the first time (a new TqKq user starts on the site paper account). */
  firstLogin?: boolean;
  authMode: "owner-password";
  authProvider: "owner-password";
  accessToken?: string;
}

/** The one login (owner 2026-10-04): the administrator and site accounts are checked by
 *  password; a TqKq account is verified by the pool, its first login consumes an invite
 *  code, and the session acts on the pool account bound with them. */
export async function authenticateTqkq(
  baseUrl: string, username: string, password: string, inviteCode = "",
) {
  if (!username.trim() || !password) throw new Error("请输入账号和密码");
  const result = await requestJson<TqkqLoginResult>(`${baseUrl}/api/auth/tqkq`, {
    method: "POST",
    body: JSON.stringify({ username: username.trim(), password, inviteCode: inviteCode.trim() })
  });
  ownerStatusReads.clear();
  if (isTauriRuntime()) {
    if (!result.accessToken) throw new Error("云端尚不支持桌面登录，请更新云端服务");
    const { invoke } = await import("@tauri-apps/api/core");
    setOwnerSessionToken(baseUrl, result.accessToken);
    await invoke("save_cloud_connection", { url: baseUrl, token: result.accessToken });
    delete result.accessToken;
  }
  changeLogin();
  resetReadIdentity();
  return result;
}

/** 开通本站模拟账户 (2026-10-03): idempotent; the default account only changes when the user had none. */
export function openPaperAccount(baseUrl: string) {
  return requestJson<{ accountId: string; displayName: string; providerKind: "paper"; isDefault: boolean }>(
    `${baseUrl}/api/accounts/open-paper`, { method: "POST" });
}

/** 设置 → 账户 → 修改密码 (2026-10-21): the server checks the current password (a wrong one counts
 *  toward the login lockout), stores the new one and hands this device a fresh session — every other
 *  device's session ends with the old password. The desktop saves the new token like a login. */
export async function changeOwnerPassword(baseUrl: string, currentPassword: string, newPassword: string) {
  const result = await requestJson<{ status: "password_changed"; accessToken?: string }>(
    `${baseUrl}/api/auth/owner/password`, { method: "POST", body: JSON.stringify({ currentPassword, newPassword }) });
  ownerStatusReads.clear();
  if (isTauriRuntime() && result.accessToken) {
    const { invoke } = await import("@tauri-apps/api/core");
    setOwnerSessionToken(baseUrl, result.accessToken);
    await invoke("save_cloud_connection", { url: baseUrl, token: result.accessToken });
  }
  return { status: result.status };
}

export interface AgentAccountStatus {
  available: boolean;
  authenticated: boolean;
  loginRequired: boolean;
  configurationRequired?: boolean;
  credentialConfigured?: boolean;
  provider?: "deepseek" | "moonshot";
  providerName?: string;
  model?: string;
  authMode?: string | null;
  planType?: string | null;
  error?: string;
}

export async function getAgentAccount(baseUrl: string) {
  return requestJson<AgentAccountStatus>(`${baseUrl}/api/agent/account`);
}

export { resolveRuntimeConnection };

export async function createSession(baseUrl: string, mode: RuntimeMode) {
  return requestJson<{ sessionId: string }>(`${baseUrl}/api/sessions`, {
    method: "POST", body: JSON.stringify({ mode, agentId: "native" })
  });
}

export interface TradingAccountDirectoryEntry {
  id: string;
  displayName: string;
  role: string;
  environment: string;
  executionOwner: boolean;
  providerKind?: string;
  isDefault?: boolean;
  status?: string;
  poolStatus?: TqkqPoolStatus;
  /** CNY, or USD for the 本站模拟 外盘 sub-account (本站模拟 · 美元). */
  currency?: string;
}

export interface TradingAccountDirectory {
  accounts: TradingAccountDirectoryEntry[];
  current: string;
}

/** Where the 对话面板's suggested questions are asked from: the view, or the 策略/持仓 panel opened last. */
export type ChatSuggestionView = "trade" | "board" | "review" | "strategy" | "positions";
export interface ChatSuggestionRequest {
  sessionId?: string;
  symbol?: string;
  view: ChatSuggestionView;
  /** open: the panel opened; turn: an answer just finished; symbol: the contract or view changed. */
  trigger: "open" | "turn" | "symbol";
}

/** 0–2 questions the AI suggests for this moment (the Runtime caches and rate-limits them). */
export function fetchChatSuggestions(baseUrl: string, request: ChatSuggestionRequest) {
  return requestJson<{ items: string[] }>(`${baseUrl}/api/agent/suggestions`, {
    method: "POST", body: JSON.stringify(request)
  });
}

/** The accounts this user may act on (default first) and the account the request resolved to. */
export function fetchAccountDirectory(baseUrl: string) {
  return requestJson<TradingAccountDirectory>(`${baseUrl}/api/accounts`);
}

/** Bind the session to another granted account; the server answers with a runtime.status
 *  carrying the new account — the caller changes nothing on success. `statusAt` is that status's
 *  timestamp (#932 R3; a repeat of the same account publishes one too), absent from an older Runtime.
 *  `expectedGeneration` (#932 follow-up) is the session's accountGeneration the switch was decided on:
 *  a newer switch landed first → 409 account_switch_superseded and nothing changes. */
export function setSessionTradingAccount(baseUrl: string, sessionId: string, accountId: string, expectedGeneration?: number) {
  return requestJson<{ sessionId: string; accountId: string; statusAt?: string; accountGeneration?: number }>(
    `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/account`,
    { method: "PUT", body: JSON.stringify(expectedGeneration === undefined ? { accountId } : { accountId, expectedGeneration }) }
  );
}

export interface FuturesWorkspacePromptContext {
  selectedEvent?: { strategyId?: string | null; eventType: "fill" | "attempt" | "protection" | "order"; eventId: string; eventTime: string };
  /** 对象级 AI (2026-09-25): the object an "AI ▾" menu item asked about. */
  focus?: { kind: "strategy" | "strategy_instance" | "position" | "order" | "strategy_edit"; id: string; label: string };
  view: "watch" | "trade" | "review" | "board";
  rightDock: "ai" | "order" | null;
  symbol: string;
  instrumentName?: string;
  durationSeconds: 60 | 180 | 300 | 900 | 1800 | 3600 | 86400 | 604800;
  selectedPrice?: number;
  quoteDatetime: string;
  capturedAt: string;
  ageSeconds: number;
  dataStatus: MarketDataStatus;
  marketSessionStatus: "trading" | "closed" | "unknown";
  lastPrice?: number;
  bidPrice1?: number;
  askPrice1?: number;
  priceTick?: number;
  positions: Array<{
    symbol: string;
    direction: "long" | "short";
    volume: number;
    todayVolume: number;
    historicalVolume: number;
    frozenToday: number;
    frozenHistorical: number;
    openPrice?: number;
    margin?: number;
    unrealizedPnl: number;
  }>;
  selectedPosition?: FuturesWorkspacePromptContext["positions"][number];
  orderDraft?: {
    side: "buy_open" | "sell_open" | "buy_close" | "sell_close";
    volume: number;
    limitPrice?: number;
    stopPrice?: number;
    source: "voice" | "context-menu" | "three-key";
    transcript?: string;
  };
  activeOrderCount: number;
  activeOrders: Array<{
    clientOrderId: string;
    symbol: string;
    direction: string;
    offset: string;
    volume: number;
    volumeLeft: number;
    filledVolume: number;
    limitPrice: number | null;
    status: string;
    insertDateTime: number;
  }>;
  recentTrades: Array<{
    tradeId: string;
    clientOrderId: string;
    symbol: string;
    direction: string;
    offset: string;
    price: number | null;
    volume: number;
    tradeDateTime: number;
  }>;
  recentActions: Array<{
    action: "view_changed" | "contract_selected" | "duration_changed" | "price_selected"
      | "position_selected" | "order_draft_created" | "manual_order_submitted" | "order_action";
    occurredAt: string;
    symbol: string;
    view?: "watch" | "trade" | "review" | "board";
    durationSeconds?: 60 | 180 | 300 | 900 | 1800 | 3600 | 86400 | 604800;
    price?: number;
    side?: "buy_open" | "sell_open" | "buy_close" | "sell_close";
    volume?: number;
    positionDirection?: "long" | "short";
    orderAction?: "continue" | "abandon" | "query" | "cancel";
    clientOrderId?: string;
  }>;
}

/** What rides with a user message besides its text: `origin: "ptt"` — a push-to-talk message, the
 *  answer carries a spoken summary read out loud; `routineId` — 我的流程's 运行 (#522): the routine
 *  the user picked runs by its id, the text (“跑一下<名称>”) is only what the chat shows. */
export interface MessageExtras { origin?: "ptt"; routineId?: string }

export async function sendMessage(
  baseUrl: string,
  sessionId: string,
  text: string,
  workspaceContext?: FuturesWorkspacePromptContext,
  extras: MessageExtras = {}
) {
  const { origin, routineId } = extras;
  return requestJson<{ status: string; runId: string }>(`${baseUrl}/api/sessions/${sessionId}/messages`, {
    method: "POST", body: JSON.stringify({ text, workspaceContext, uiWindowId: activeAgentWindow(baseUrl),
      ...(origin ? { origin } : {}), ...(routineId ? { routineId } : {}) })
  });
}

export async function adjustProposalRisk(baseUrl: string, proposalId: string, proposalVersion: number) {
  return requestJson(`${baseUrl}/api/proposals/${proposalId}/adjust-risk`, {
    method: "POST", body: JSON.stringify({ proposalVersion, idempotencyKey: idempotencyKey() })
  });
}

export async function confirmProposal(
  baseUrl: string, proposalId: string, approvalId: string, proposalVersion: number, accountId?: string | null
) {
  return requestJson<{
    simulationOrder?: SimulationOrder;
    /** A batch answers with each child order read back from its link, never with what this call tried. */
    simulationOrders?: SimulationOrder[];
    execution?: CloseBatchExecution;
  }>(`${baseUrl}/api/proposals/${proposalId}/confirm`, {
    method: "POST", headers: accountId ? { "X-AI-Trader-Account": accountId } : undefined, body: JSON.stringify({ approvalId, proposalVersion, idempotencyKey: idempotencyKey() })
  });
}

/** How a confirmed batch's child orders actually ended up; the approval is a separate fact. */
export interface CloseBatchExecution {
  total: number;
  sent: number;
  notSent: number;
  unknown: number;
  filled: number;
  text: string;
}

export interface SimulationOrder {
  /** Runtime 委托读数携带所属账户；撤单、查询等操作回到该账户。旧 Runtime 可缺省。 */
  accountId?: string;
  factRevision?: number;
  id: string;
  proposalId: string;
  clientOrderId: string;
  request: {
    symbol: string;
    direction: "BUY" | "SELL";
    offset: "OPEN" | "CLOSE" | "CLOSETODAY";
    volume: number;
    priceType: "LIMIT";
    limitPrice: number;
  };
  submitPhase: "PREPARED" | "CALL_STARTED" | "UNKNOWN" | "CONFIRMED" | "ABANDONED";
  cancelPhase: "NONE" | "CALL_STARTED" | "UNKNOWN" | "CONFIRMED";
  remoteStatus?: string | null;
  filledVolume?: number;
  /** 部分成交 = still working; 部分成交后已结束 = some lots filled, the rest cancelled or expired (#932). */
  displayStatus: "待发送" | "结果未知" | "已受理" | "部分成交" | "部分成交后已结束" | "已成交" | "已撤销" | "已拒绝" | "身份异常" | "回报异常" | "已放弃";
  lastReadError?: string | null;
  lastSyncedAt?: string | null;
  accountLabel: "快期模拟账户" | "本站模拟账户";
  liveTrading: "disabled";
  automaticStopLoss: false;
}

export interface AiExecutionAuthorization {
  authorizationId: string;
  userId: string;
  sessionId: string;
  status: "ACTIVE" | "STOPPED" | "EXPIRED" | "EMERGENCY_STOP";
  allowedSymbols: string[];
  permissions: Array<"open" | "cancel" | "close" | "stop">;
  maxOrderVolume: number;
  maxOrders: number;
  usedOrders: number;
  remainingOrders: number;
  expiresAt: string;
  createdAt: string;
  updatedAt: string;
  environment: "TqKq simulation";
  liveTrading: "disabled";
}

/** 手动止损怎么动 (2026-10-25)：追踪（策略同一算法，只紧不松）与浮盈 1R 后移到成本；都要有止损价。 */
export type TrailingMode = "ticks" | "percent" | "atr_chandelier" | "bar_extreme";
export type StopMotion = {
  trailing?: { mode: TrailingMode; value: number; activateAfter?: number; atrTimeframe?: number };
  breakevenAtR?: boolean;
};

export interface AiStopLoss {
  /** How the stop moves (2026-10-25); an older Runtime sends neither. */
  trailing?: { mode: TrailingMode; value: number; activateAfter?: number; atrTimeframe?: number } | null;
  breakeven?: boolean;
  stopId: string;
  authorizationId: string;
  sessionId: string;
  symbol: string;
  positionDirection: "long" | "short";
  positionBucket: "today" | "historical" | "all";
  volume: number;
  /** Null on a take-profit-only protection (止损/止盈至少一个). */
  triggerPrice: number | null;
  takeProfitPrice: number | null;
  remainingVolume: number;
  strategyId: string | null;
  /** "strategy" = a strategy's own rule armed it; "manual" = the user set it (order card, 持仓面板, AI stop tool). */
  source: "strategy" | "manual";
  priceIntent: "passive" | "aggressive" | "last";
  status: "ARMED" | "TRIGGERING" | "SUBMITTED" | "PARTIAL" | "EXECUTED" | "UNKNOWN" | "CANCELLED" | "FAILED";
  closeProposalId?: string | null;
  closeClientOrderId?: string | null;
  lastError?: string | null;
  triggeredAt?: string | null;
  createdAt: string;
  updatedAt: string;
  liveTrading: "disabled";
}

export interface AiExecutionSnapshot {
  controls?: AiExecutionControls;
  authorization?: AiExecutionAuthorization | null;
  stops: AiStopLoss[];
  simulationOnly: true;
  liveTrading: "disabled";
}

export interface AiExecutionControls {
  stopStatus: "STOPPED" | "HALT_OPENS" | "EMERGENCY_STOP" | null;
  generation: number;
  revision: number;
  maxOrderVolume: number | null;
  maxOrders: number | null;
}

export function updateSimulationLimits(baseUrl: string, body: Pick<AiExecutionControls, "revision" | "maxOrderVolume" | "maxOrders">) {
  return requestJson<AiExecutionControls>(`${baseUrl}/api/ai-execution/limits`, {method: "POST", body: JSON.stringify(body)});
}

/** Tell the Runtime feed which market this session's window shows. */
export async function setFeedFocus(baseUrl: string, sessionId: string, symbol: string, durationSeconds: MarketWorkbenchSnapshot["durationSeconds"],
  /** The last closed bar the browser holds for this chart (marketCache.ts): the first frame carries only the bars after it. */
  since?: number) {
  return requestJson<{ symbol: string | null; durationSeconds: number }>(`${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/feed`, {
    // marketPatches: this client applies feed.market-patch (only changes, not the whole chart, on every tick).
    // feedTouch: an unchanged account / activity / execution frame is refreshed by its stamps only (feed.touch).
    method: "PUT", body: JSON.stringify({ symbol, durationSeconds, marketPatches: true, feedTouch: true, ...(since === undefined ? {} : { since }) })
  });
}

export async function getAiExecution(baseUrl: string) {
  return requestJson<AiExecutionSnapshot>(`${baseUrl}/api/ai-execution`);
}

/** The user's own 恢复执行: also lifts the stop the USD sub-account took with this account (the AI's own resume does not). */
export async function resumeAiExecution(baseUrl: string, sessionId: string, symbol: string) {
  await requestJson(`${baseUrl}/api/ai-execution/resume`, {method: "POST", body: JSON.stringify({sessionId, symbol})});
}

export async function stopAiExecution(baseUrl: string, emergency = false, scope: "all" | "opens" = "all") {
  return requestJson<{
    authorization?: AiExecutionAuthorization | null;
    emergency: boolean;
    scope: "all" | "opens";
    pausedStrategyIds: string[];
    liveTrading: "disabled";
  }>(`${baseUrl}/api/ai-execution/stop`, {
    method: "POST",
    body: JSON.stringify({ emergency, scope })
  });
}

/** 持仓面板的保护表单 (2026-10-02): give an existing position its stop / take (either one alone
 * is fine; the Runtime refuses a wrong direction with the Chinese reason as a 422). */
export async function createAiStop(
  baseUrl: string,
  request: {
    sessionId: string; symbol: string; positionDirection: "long" | "short";
    positionBucket: "today" | "historical" | "all"; volume: number;
    triggerPrice?: number | null; takeProfitPrice?: number | null;
  } & StopMotion
) {
  return requestJson<AiStopLoss>(`${baseUrl}/api/ai-execution/stops`, {
    method: "POST",
    body: JSON.stringify(request)
  });
}

/** 图上拖线改价 (2026-10-24): the user's own stop / take moved in place; `previous` is the level it was dragged from
 *  (a protection changed meanwhile is refused with its Chinese reason, never overwritten). */
export async function repriceAiStop(baseUrl: string, stopId: string, request: { level: "stop" | "take"; previous: number; price: number }) {
  return requestJson<AiStopLoss>(`${baseUrl}/api/ai-execution/stops/${encodeURIComponent(stopId)}/reprice`, {
    method: "POST",
    body: JSON.stringify(request)
  });
}

export async function cancelAiStop(baseUrl: string, stopId: string) {
  return requestJson<AiStopLoss>(`${baseUrl}/api/ai-execution/stops/${encodeURIComponent(stopId)}/cancel`, {
    method: "POST"
  });
}

/** 保本提醒 (2026-10-06): 移到成本 / 不用 for one question (the card's buttons; a second answer reads back the first). */
export async function answerBreakeven(baseUrl: string, breakevenId: string, choice: "move" | "decline") {
  return requestJson<{ id: string; status: "pending" | "moved" | "declined" | "failed"; result: string | null }>(
    `${baseUrl}/api/ai-execution/breakeven/${encodeURIComponent(breakevenId)}/${choice}`, { method: "POST" });
}

/** The account's simulation orders. `accountId` names the account the list is for (#930): a read
 *  without it lands on the user's default account, not the one the session acts on. */
export async function listSimulationOrders(baseUrl: string, recoveryWait = true, accountId?: string | null) {
  return requestJson<{ items: SimulationOrder[] }>(
    `${baseUrl}/api/simulation/orders?recoveryWait=${recoveryWait ? "true" : "false"}`,
    accountId ? { headers: { "X-AI-Trader-Account": accountId } } : undefined
  );
}

/** 下单前的“钱”估算（只读）：开仓的保证金约 / 手续费约 / 可开手数，平仓的释放保证金约。
 * 缺数据的字段为 null 并带 `missing` 原因码；这只是展示，不拦截下单。 */
export async function getOrderEstimate(
  baseUrl: string,
  request: {
    symbol: string; side: "buy" | "sell"; offset: "open" | "close"; volume: number; price?: number;
    /** 平仓桶：平今/平昨分开报的交易所按所选桶估算；省略 = 按账户平今平昨偏好拆。 */
    positionBucket?: "today" | "historical" | "all";
  },
  signal?: AbortSignal
) {
  const query = new URLSearchParams({
    symbol: request.symbol, side: request.side, offset: request.offset, volume: String(request.volume)
  });
  if (request.price !== undefined) query.set("price", String(request.price));
  if (request.positionBucket !== undefined) query.set("positionBucket", request.positionBucket);
  return requestJson<OrderEstimate>(`${baseUrl}/api/simulation/order-estimate?${query}`, { signal });
}

export interface SimulationProposalRequest {
  sessionId: string;
  symbol: string;
  side: "buy_open" | "sell_open" | "buy_close" | "sell_close";
  /** 平仓只带用户选定的平今 / 平昨；不带 = Runtime 的下单规划器按“平今平昨”偏好分配（意图重构第 4 步）。 */
  positionBucket?: "today" | "historical";
  volume: number;
  priceIntent: "passive" | "aggressive" | "last";
  limitPrice?: number;
  reason: string;
  /** 开仓顺带保护 (2026-10-02)：至少一个；成交后按实际成交量自动挂上，撤单不挂。追踪 / 浮盈 1R 后移到成本要有止损价。 */
  protection?: { stopLossPrice?: number; takeProfitPrice?: number } & StopMotion;
  /** 打法本 (2026-10-25)：这一单按哪个打法下；复盘里这笔开平带上这个打法。 */
  playbookId?: string;
}

/** 打法本 (2026-10-25): one of the user's setups — their own entry / exit / stop rules, optionally a linked strategy. */
export interface Playbook {
  id: string;
  name: string;
  entryRules: string;
  exitRules: string;
  stopRules: string;
  strategyId: string | null;
  createdAt: string;
  updatedAt: string;
}
export type PlaybookFields = Pick<Playbook, "name" | "entryRules" | "exitRules" | "stopRules" | "strategyId">;

export function listPlaybooks(baseUrl: string) {
  return requestJson<{ playbooks: Playbook[]; max: number }>(`${baseUrl}/api/playbooks`);
}
export function savePlaybook(baseUrl: string, fields: PlaybookFields, id?: string) {
  return requestJson<{ playbook: Playbook }>(`${baseUrl}/api/playbooks${id ? `/${encodeURIComponent(id)}` : ""}`, {
    method: id ? "PUT" : "POST", body: JSON.stringify(fields) });
}
export function deletePlaybook(baseUrl: string, id: string) {
  return requestJson<{ deleted: string }>(`${baseUrl}/api/playbooks/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** One card per request: a single order, or — a 上期所/能源中心 close the planner split over 今仓 + 昨仓 — one batch card. */
export async function createSimulationProposal(
  baseUrl: string,
  proposal: SimulationProposalRequest,
  accountId?: string | null
) {
  return requestJson<TradeProposal | CloseBatchProposal>(`${baseUrl}/api/simulation/proposals`, {
    method: "POST",
    body: JSON.stringify(proposal),
    headers: accountId ? { "X-AI-Trader-Account": accountId } : undefined
  });
}

export interface TargetPositionProposalRequest {
  sessionId: string;
  symbol: string;
  /** Wanted net position: positive = net long, negative = net short, 0 = flatten. */
  targetNet: number;
  priceIntent?: "passive" | "aggressive" | "last";
  reason: string;
}

/** The answer of the 目标持仓 ticket: the first leg's card (already published to the
 *  conversation), or an honest no-card line when the account is already at the target. */
export interface TargetPositionProposalResponse {
  cardIssued: boolean;
  proposalType?: "proposal.trade" | "proposal.close_batch";
  proposalId?: string;
  /** The card's approval, for a caller that confirms it right away (快捷区反手). */
  approvalId?: string;
  proposalVersion?: number;
  targetPosition?: TargetPositionPlan;
  errorCode?: string | null;
  userFacing?: { candidateStatus?: string; candidateIssues?: string[] };
}

export async function createTargetPositionProposal(
  baseUrl: string,
  request: TargetPositionProposalRequest
) {
  return requestJson<TargetPositionProposalResponse>(`${baseUrl}/api/simulation/target-position/proposals`, {
    method: "POST",
    body: JSON.stringify(request)
  });
}

/** 交割月提醒 (2026-10-02)：个人账户不能持仓进交割月（股指现金交割持到第三个周五）。
 * Runtime 按合约目录计算，合约目录条目与行情快照都带；无需提醒时为 null。
 * tradingDaysLeft 含今天，按交易所日历扣节假日；日历覆盖不到时按工作日估算，estimated 为 true
 * （旧 Runtime 不带这个字段，按估算看）。 */
export interface DeliveryNotice {
  kind: "delivery_month" | "expiry";
  level: "warning" | "danger";
  lastHoldDay: string;
  tradingDaysLeft: number;
  estimated?: boolean;
  text: string;
}

export interface InstrumentCatalogItem {
  symbol: string;
  exchangeId: string;
  instrumentId: string;
  instrumentName: string;
  productId: string;
  priceTick: number | null;
  volumeMultiple: number | null;
  deliveryYear: number;
  deliveryMonth: number;
  expireRestDays: number;
  hasNight: boolean;
  isMain: boolean;
  deliveryNotice?: DeliveryNotice | null;
  /** 能在美元子账户交易的外盘主连在策略适用范围里的写法（KQD.m@COMEX.GC → CME.GCE，外盘三期）。 */
  strategyScope?: string;
}

export interface InstrumentCatalog {
  /** Browser-only display fallback; never treated as catalog membership or execution metadata. */
  cachedNames?: Record<string, string>;
  catalogStatus?: "loading" | "partial" | "ready" | "unavailable";
  schemaVersion: 1;
  capturedAt: string;
  dataStatus: "simulated" | "normal" | "stale" | "unavailable";
  isCached: boolean;
  count: number;
  items: InstrumentCatalogItem[];
}

const foreignScopes = new Map<string, string>();
/** 反过来：策略范围写法（小写）→ 它的外盘图表主连原样代码。 */
const foreignCharts = new Map<string, string>();
/** The catalog listed 海外主连 at least once (so a missing strategyScope means “quote only”, not “not read yet”). */
let foreignCatalogRead = false;
/** 外盘图表主连 → 策略范围写法（KQD.m@COMEX.GC → CME.GCE），取自 Runtime 合约目录的 strategyScope，读目录时记下。 */
export function rememberStrategyScopes(items: { symbol: string; strategyScope?: string }[]): void {
  for (const item of items) {
    if (/^KQD\./i.test(item.symbol)) foreignCatalogRead = true;
    if (!item.strategyScope) continue;
    foreignScopes.set(item.symbol.toLowerCase(), item.strategyScope);
    foreignCharts.set(item.strategyScope.toLowerCase(), item.symbol);
  }
}
export const foreignStrategyScope = (symbol: string): string | undefined => foreignScopes.get(symbol.toLowerCase());
/** 只接了参考行情的外盘主连（伦铜、美棉、布伦特…）：目录里有它、却没有可下单的月份合约（没有 strategyScope）。
 *  目录还没读到时为 false——不凭空把可交易的外盘图表挡住（下单仍由 Runtime 核对）。 */
export const foreignQuoteOnly = (symbol: string | undefined): boolean =>
  foreignCatalogRead && /^KQD\./i.test(symbol ?? "") && foreignStrategyScope(symbol ?? "") === undefined;
/** 美元子账户的月份合约 → 品种范围（CME.GCEZ26 → CME.GCE）；不是外盘月份合约为 undefined。 */
const foreignMonthScope = (symbol: string): string | undefined =>
  /^CME\.[A-Za-z0-9]+[FGHJKMNQUVXZ]\d{2}$/i.test(symbol) ? symbol.slice(0, -3) : undefined;
/** 外盘月份合约画在哪张图上：它品种的快期外盘主连（CME.ENQZ26 → KQD.m@CME.NQ；月份合约本身没有图表）。其余原样。 */
export function chartSymbolOf(symbol: string): string {
  const scope = foreignMonthScope(symbol)?.toLowerCase();
  return (scope && foreignCharts.get(scope)) || symbol;
}
/** 这笔持仓（或委托）的合约是不是图表上这个合约：同一代码，或者外盘主连图表与它品种的月份合约。 */
export function sameContract(symbol: string, chartSymbol: string | undefined): boolean {
  if (!chartSymbol) return false;
  if (symbol.toUpperCase() === chartSymbol.toUpperCase()) return true;
  const scope = foreignMonthScope(symbol);
  return scope !== undefined && foreignStrategyScope(chartSymbol)?.toLowerCase() === scope.toLowerCase();
}
export async function listInstruments(baseUrl: string) {
  const catalog = await requestJson<InstrumentCatalog>(`${baseUrl}/api/instruments`);
  rememberStrategyScopes(catalog.items);
  return catalog;
}

/** One subscribed AI 简报 product: "SHFE.rb", its name and the main contract searched for it. */
export interface BriefProduct { product: string; name: string; symbol: string }
/** AI 简报订阅 (2026-10-05): the products in push order, the tier's cap (null = 不限), whether the
 *  list is still the default (the watchlist) and whether a lowered cap left more than it allows. */
export interface BriefSubscriptions {
  products: BriefProduct[];
  limit: number | null;
  source: "default" | "user";
  overLimit?: boolean;
}

export async function getBriefSubscriptions(baseUrl: string, signal?: AbortSignal) {
  return requestJson<BriefSubscriptions>(`${baseUrl}/api/app/brief-subscriptions`, { signal });
}

/** Replaces the whole list (order = push order); 422 `brief_limit_exceeded` / `brief_product_unknown`. */
export async function saveBriefSubscriptions(baseUrl: string, products: string[]) {
  return requestJson<BriefSubscriptions>(`${baseUrl}/api/app/brief-subscriptions`, { method: "PUT", body: JSON.stringify({ products }) });
}

/** A brief of the multi-product read: the product it was searched for rides on each item. */
export type ProductBrief = Brief & { product?: string; productName?: string; symbol?: string };

/** Every subscribed product's briefs, merged newest first. */
export async function listProductBriefs(baseUrl: string, signal?: AbortSignal) {
  return requestJson<{ items: ProductBrief[] }>(`${baseUrl}/api/app/briefs`, { signal });
}

/** One search round; the Runtime rotates through the subscribed products. */
export async function refreshProductBriefs(baseUrl: string) {
  return requestJson<{ status: "disabled" | "unavailable" | "idle" | "pending" }>(`${baseUrl}/api/app/briefs/refresh`, { method: "POST", body: "{}" });
}

/** One row of the 报价总表: every product's main contract, one quote each. */
export interface MarketBoardRow {
  symbol: string;
  instrumentId: string;
  instrumentName: string;
  /** 品种名: the instrument name without its delivery digits (螺纹钢2610 → 螺纹钢). */
  productName: string;
  exchangeId: string;
  productId: string;
  last: number | null;
  preSettlement: number | null;
  /** 涨跌 / 涨跌幅 vs 昨结算. */
  change: number | null;
  changePercent: number | null;
  volume: number | null;
  openInterest: number | null;
  openInterestChange: number | null;
  high: number | null;
  low: number | null;
  upperLimit: number | null;
  lowerLimit: number | null;
  quoteTime: string;
  /** 买一 / 卖一 (2026-10-09, the phone 报价列表's 买价 / 卖价 columns); absent from an older Runtime. */
  bidPrice1?: number | null;
  askPrice1?: number | null;
  /** normal / market_quote_no_trade（当日无成交）/ market_quote_pre_open（盘前）/ market_quote_unavailable. */
  status: string;
}

/** 只订报价的补充行 (2026-10-09): quote-only rows for contracts the board does not carry (≤ 20 per read).
 *  Each read renews the symbols' 30 s lease; a capped symbol's row carries errorCode market_subscription_limit. */
export interface MarketQuotesSnapshot {
  capturedAt: string;
  dataStatus: MarketDataStatus;
  rows: Array<MarketBoardRow | { symbol: string; errorCode: string }>;
}

export async function getMarketQuotes(baseUrl: string, symbols: string[], signal?: AbortSignal) {
  return requestJson<MarketQuotesSnapshot>(`${baseUrl}/api/market/quotes?symbols=${symbols.map(encodeURIComponent).join(",")}`, { signal });
}

export interface MarketBoardSnapshot {
  capturedAt: string;
  dataStatus: MarketDataStatus;
  /** 新浪（备用） while TqKq is down and the board comes from the backup source. */
  source?: string;
  /** Opaque change marker from the sidecar (`<epoch>:<n>`). */
  revision: string;
  /** True when the sidecar capped the rows (catalog order, MAX_BOARD_QUOTES). */
  truncated: boolean;
  rows: MarketBoardRow[];
}

export async function getMarketBoard(baseUrl: string, signal?: AbortSignal) {
  return requestJson<MarketBoardSnapshot>(`${baseUrl}/api/market/board`, { signal });
}

/** 外盘 (2026-10-18, #976): one foreign product's main series, the whole row from one source (`source`). */
export interface ForeignBoardRow {
  key: string;
  name: string;
  exchange: string;
  currency: string;
  unit: string;
  last: number;
  preSettlement: number | null;
  change: number | null;
  changePercent: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  bidPrice1: number | null;
  askPrice1: number | null;
  volume: number | null;
  openInterest: number | null;
  quoteTime: string | null;
  source: string;
  /** Display precision (the exchange tick). */
  decimals: number;
  /** The month contract the row came from (MT5: GCEZ26); absent for a source that quotes the main series. */
  contract?: string | null;
  /** The chart this row opens (快期外盘主连 symbol, KQD.m@COMEX.GC); absent on an older Runtime (row not clickable). */
  chartSymbol?: string | null;
  /** This row is the last good one: its source failed on this read. */
  stale?: boolean;
  domestic: string[];
}

export interface ForeignBoardSnapshot {
  status: "ok" | "stale" | "unavailable";
  error?: string;
  fetchedAt?: string;
  rows: ForeignBoardRow[];
  /** Source id → display name (新浪外盘 / 快期外盘主连 / MT5). */
  sourceLabels: Record<string, string>;
  note?: string;
}

export async function getForeignBoard(baseUrl: string, signal?: AbortSignal) {
  return requestJson<ForeignBoardSnapshot>(`${baseUrl}/api/market/foreign`, { signal });
}

/** 合约信息卡的四块之源 (2026-10-02)：一手（乘数/每跳）、资金（每手保证金/手续费）、时间、交割。 */
export interface ContractInfo {
  multiplier: number | null;
  priceTick: number | null;
  marginPerLot: number | null;
  commissionPerLot: number | null;
  tradingSchedule: TradingHours | null;
}

/** A contract's trading hours (Beijing "HH:MM"; an end past 24:00 is the next morning). `dayRule: "cme"` (外盘盘点 D):
 *  a tradable 外盘 chart's CME hours (the Runtime's cme_schedule), `cmeHours` which (F2): globex one period from the
 *  美东 18:00 open to the 17:00 close (equity / rates / fx the same hours, apart only on CME holidays), grains (CBOT
 *  谷物) two, livestock (瘦肉猪) a day-only one. */
export interface TradingHours { day: [string, string][]; night: [string, string][]; dayRule?: "cme"; cmeHours?: "globex" | "equity" | "rates" | "fx" | "grains" | "livestock" }

export interface MarketQuoteSnapshot {
  tradingSchedule?: TradingHours | null;
  /** 外盘 (CME) chart shut (marketSessionStatus closed by the product's CME hours): why — the pause between two periods
   *  of one trading day (intraday, CBOT 谷物 芝加哥 07:45–08:30), the daily stop between trading days (break), the
   *  weekend or a CME full-day closure — and the next open (ISO, Beijing). */
  sessionClosedFor?: "intraday" | "break" | "weekend" | "holiday";
  sessionReopensAt?: string;
  /** 合约信息卡 (2026-10-02)：快照带的合约要素与交割提醒，缺数据为 null。 */
  contract?: ContractInfo | null;
  deliveryNotice?: DeliveryNotice | null;
  symbol: string;
  name: string;
  quoteDatetime: string;
  lastPrice: number;
  changePercent: number;
  bidPrice1: number | null;
  bidVolume1: number | null;
  askPrice1: number | null;
  askVolume1: number | null;
  /** 五档盘口的 2–5 档：模拟盘常只有一档，缺档为 null。 */
  bidPrice2?: number | null;
  bidVolume2?: number | null;
  askPrice2?: number | null;
  askVolume2?: number | null;
  bidPrice3?: number | null;
  bidVolume3?: number | null;
  askPrice3?: number | null;
  askVolume3?: number | null;
  bidPrice4?: number | null;
  bidVolume4?: number | null;
  askPrice4?: number | null;
  askVolume4?: number | null;
  bidPrice5?: number | null;
  bidVolume5?: number | null;
  askPrice5?: number | null;
  askVolume5?: number | null;
  open: number | null;
  highest: number | null;
  lowest: number | null;
  volume: number | null;
  openInterest: number | null;
  priceTick: number | null;
  volumeMultiple: number | null;
  upperLimit: number | null;
  lowerLimit: number | null;
  /** 日内开仓限额 (exchange, lots); 0 or absent = none. The ticket caps an open's 手数 by it. */
  openLimit?: number | null;
  /** 昨结价 (2026-10-02): the 分时图's dashed reference line and its change-% baseline. Absent on an
   * older Runtime — the line and the readout's 涨跌幅 simply stay hidden. */
  preSettlement?: number | null;
  marketSessionStatus?: "trading" | "closed" | "unknown";
  /** Raw exchange trading status (TqSdk trade_status): AUCTIONORDERING 集合竞价, CONTINOUS 连续交易,
   *  NOTRADING 非交易时段, AUCTIONTRADE 集合竞价撮合; absent when the quote source has none. */
  tradingStatus?: string;
  /** 每手保证金 / 手续费（元，交易所标准估算）：合约服务没写入或值不成立时为 null。 */
  marginPerLot?: number | null;
  commissionPerLot?: number | null;
  dataStatus?: MarketDataStatus;
  /** 新浪（备用） on a backup quote; “MT5 · GCEZ26” on a 外盘 chart's quote — MT5's main month, the daily and weekly
   *  charts' too (their K-lines are 快期外盘主连's; the packet's own `source` says what drew the bars). */
  source?: string;
  /** `market_quote_pre_open`: no live quote yet (no night session before the next open); `market_quote_no_trade`: the
   *  session is on but nobody has traded the contract today. In both the price is the last bar's close. */
  dataErrorCode?: string | null;
}

export interface MarketKlineBar {
  datetime: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  openInterest: number;
  /** The trading day the bar belongs to (YYYY-MM-DD; a night bar carries the next trading day).
   * An older Runtime may omit it — the 分时图 then falls back to its own calendar rule. */
  tradingDay?: string;
}

/** `backup` (2026-10-07): every TqKq source is down and the shared market shows Sina's quotes — for
 *  watching and research only; the ticket, the stops and the alerts treat it as not current. */
export type MarketDataStatus = "simulated" | "normal" | "stale" | "unavailable" | "backup";

export interface MarketWorkbenchSnapshot {
  schemaVersion: 1;
  durationSeconds: 60 | 180 | 300 | 900 | 1800 | 3600 | 86400 | 604800;
  capturedAt: string;
  ageSeconds: number;
  dataStatus: MarketDataStatus;
  /** 新浪（备用） on a backup packet; “MT5 · GCEZ26” on a 外盘 chart drawn from MT5's main month. */
  source?: string;
  marketSessionStatus?: "trading" | "closed" | "unknown";
  isCached: boolean;
  liveTrading: "disabled";
  quote: MarketQuoteSnapshot;
  bars: MarketKlineBar[];
  /** A guest packet's history limit in trading days (the bars are already cut to it); absent for members (#734). */
  historyTradingDays?: number;
}

export interface AccountSnapshot {
  accountName: string;
  /** 外盘二期：美元子账户为 USD；缺省按人民币。 */
  currency?: "CNY" | "USD";
  capturedAt: string;
  ageSeconds: number;
  dataStatus: "simulated" | "normal" | "stale" | "unavailable";
  equity: number;
  todayPnl: number;
  /** Today's result net of commission, floating P&L included (equity vs. yesterday's settlement). */
  todayNetPnl?: number;
  marginRatio: number;
  /** The “money” a beginner actually asks about (2026-10-02): yuan, from the account itself. */
  available?: number;
  margin?: number;
  /** 保证金占用 ÷ 权益，百分数（与 marginRatio 同口径），风险度越高越接近被强平。 */
  riskRatio?: number;
  positionCount: number;
  /** 本站模拟挂起中的到期结算（#527）：待最后交易日结算价 / 暂估待核；快期账户为空。 */
  expirySettlements?: ExpirySettlementView[];
}

export interface ExpirySettlementView {
  symbol: string;
  finalDay: string;
  status: "pending" | "estimated" | "settled" | "adjusted";
  statusLabel: string;
  since?: string;
  estimate?: { price: number; day?: string; kind?: string };
  official?: { price: number; day?: string };
}

export interface AccountPosition {
  symbol: string;
  direction: "long" | "short";
  volume: number;
  todayVolume?: number;
  historicalVolume?: number;
  frozenToday?: number;
  frozenHistorical?: number;
  openPrice?: number | null;
  /** The position's own latest price (#803); rows of contracts off the chart show it too. */
  lastPrice?: number | null;
  margin?: number | null;
  unrealizedPnl: number;
  /** 外盘仓合并显示 (2026-10-21)：并进人民币账户持仓面板的美元子账户持仓，标着它自己的账户与币种；自己账户的行不带。 */
  accountId?: string;
  currency?: "USD";
  /** 外盘月份合约的中文名（COMEX黄金2612，Runtime 按代码给）；当前账户的合约目录里没有这些月份。 */
  instrumentName?: string;
}

export interface RemoteOrderSnapshot {
  clientOrderId: string;
  symbol: string;
  direction: string;
  offset: string;
  volume: number;
  volumeLeft: number;
  filledVolume: number;
  limitPrice: number | null;
  status: string;
  insertDateTime: number;
  lastMsg: string;
}

export interface RemoteTradeSnapshot {
  tradeId: string;
  clientOrderId: string;
  symbol: string;
  direction: string;
  offset: string;
  price: number | null;
  volume: number;
  tradeDateTime: number;
}

export interface PositionsStatus {
  dataStatus: MarketDataStatus;
  snapshotAvailable: boolean;
  isCached: boolean;
  ageSeconds: number;
}

export interface AccountOverviewSnapshot {
  account: AccountSnapshot;
  positions: AccountPosition[];
  positionsStatus?: PositionsStatus;
  activity: {
    schemaVersion: 1;
    capturedAt: string;
    ageSeconds: number;
    dataStatus: AccountSnapshot["dataStatus"];
    ordersInitialized: boolean;
    orders: RemoteOrderSnapshot[];
    trades: RemoteTradeSnapshot[];
    liveTrading: "disabled";
  };
  risk: RiskBudgetSummary;
  /** 外盘仓合并显示 (2026-10-21)：美元子账户那一半（推送单列，工作区合并后其行才在 positions 里，标着账户与币种）。 */
  foreign?: ForeignHalf;
}

export interface RiskBudgetSummary {
  configured: boolean;
  dataStatus: AccountSnapshot["dataStatus"];
  status: "unconfigured" | "unavailable" | "normal" | "warning" | "blocked";
  message: string;
  policyVersion?: number;
  maxLossPerTrade?: number;
  maxDailyLoss?: number;
  dailyLossUsed?: number | null;
  dailyLossRemaining?: number | null;
  dailyLossPercent?: number | null;
  noNewPositionsAfter?: string;
  allowedSymbols?: string[];
  openingAllowedNow?: boolean | null;
}

export async function getAccountOverview(
  baseUrl: string,
  signal?: AbortSignal
): Promise<AccountOverviewSnapshot> {
  const [account, positions, activity, risk] = await Promise.all([
    requestJson<AccountSnapshot>(`${baseUrl}/api/account/summary`, { signal }),
    requestJson<{ items: AccountPosition[] } & Partial<PositionsStatus>>(`${baseUrl}/api/positions`, { signal }),
    requestJson<AccountOverviewSnapshot["activity"]>(`${baseUrl}/api/account/activity`, { signal }),
    requestJson<RiskBudgetSummary>(`${baseUrl}/api/risk/summary`, { signal })
  ]);
  return { account, positions: positions.items, activity, risk,
    positionsStatus: positions.snapshotAvailable === undefined ? undefined : positions as PositionsStatus };
}

export async function getMarketWorkbench(
  baseUrl: string,
  symbol: string,
  durationSeconds: MarketWorkbenchSnapshot["durationSeconds"],
  signal?: AbortSignal,
  /** The last closed bar the browser holds (marketCache.ts): only the bars after it come back. */
  since?: number
) {
  const encodedSymbol = encodeURIComponent(symbol);
  return requestJson<MarketWorkbenchSnapshot & { barsAfter?: number; barsWindow?: { first: number; count: number; digest: string } }>(
    `${baseUrl}/api/markets/${encodedSymbol}/workbench?duration=${durationSeconds}${since === undefined ? "" : `&since=${since}`}`,
    { signal }
  );
}

/** 外盘仓合并显示 (2026-10-21)：人民币本站模拟账户的美元子账户那一半——由账户推送提供；回退和显式刷新才带子账户头读。 */
export interface ForeignHalf {
  accountId: string;
  account: AccountSnapshot;
  positions: AccountPosition[];
  positionsStatus?: PositionsStatus;
  orders: SimulationOrder[];
}

export async function getForeignHalf(baseUrl: string, accountId: string, signal?: AbortSignal): Promise<ForeignHalf> {
  const headers = { "X-AI-Trader-Account": accountId };
  const [account, positions, orders] = await Promise.all([
    requestJson<AccountSnapshot>(`${baseUrl}/api/account/summary`, { signal, headers }),
    requestJson<{ items: AccountPosition[] } & Partial<PositionsStatus>>(`${baseUrl}/api/positions`, { signal, headers }),
    requestJson<{ items: SimulationOrder[] }>(`${baseUrl}/api/simulation/orders?recoveryWait=false`, { signal, headers }),
  ]);
  return { accountId, account,
    positionsStatus: positions.snapshotAvailable === undefined ? undefined : positions as PositionsStatus,
    positions: positions.items.map(position => ({ ...position, accountId, currency: "USD" as const })),
    orders: orders.items };
}

/** The workspace's account view: the session account's own snapshot with its USD sub-account's positions after its own. */
export function withForeignHalf(overview: AccountOverviewSnapshot | undefined, half: ForeignHalf | undefined): AccountOverviewSnapshot | undefined {
  if (!overview || !half) return overview;
  return { ...overview, positions: [...overview.positions, ...half.positions], foreign: half };
}

export async function mutateSimulationOrder(
  baseUrl: string,
  clientOrderId: string,
  action: "continue" | "abandon" | "query" | "cancel",
  /** The account the order's row was listed from (#930). */
  accountId?: string | null
) {
  return requestJson<SimulationOrder>(
    `${baseUrl}/api/simulation/orders/${encodeURIComponent(clientOrderId)}/${action}`,
    { method: "POST", body: "{}", ...(accountId ? { headers: { "X-AI-Trader-Account": accountId } } : {}) }
  );
}

export interface StrategyMonitorState {
  strategyId: string;
  userId: string;
  tradingDate: string;
  highWatermarkEquity?: string | null;
  lastTodayPnl?: string | null;
  consecutiveLosses: number;
  lastTradeId?: string | null;
  lastExecutedBar?: string | null;
  pauseReason?: string | null;
  lastError?: string | null;
  lastErrorAt?: string | null;
  consecutiveFailures?: number;
  nextRetryAt?: string | null;
  updatedAt: string;
}

export interface StrategyExecutionLeg {
  legId: string;
  legIndex: number;
  positionBucket?: string | null;
  volume: number;
  clientOrderId?: string | null;
  status: "RESERVED" | "SUBMITTED" | "PARTIAL" | "NOT_DISPATCHED" | "REJECTED" | "CANCELLED" | "UNKNOWN" | "FAILED";
  detail?: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface StrategyExecutionAttempt {
  attemptId: string;
  strategyId: string;
  intentId: string;
  signalBarTime: string;
  tradingDay?: string;
  action: string;
  volume: number;
  status: "RESERVED" | "SUBMITTED" | "PARTIAL" | "NOT_DISPATCHED" | "REJECTED" | "CANCELLED" | "SKIPPED" | "BLOCKED" | "UNKNOWN" | "FAILED";
  clientOrderId?: string | null;
  detail?: string | null;
  /** 出卡确认: the cards this attempt issued; SKIPPED with cards = waiting for confirmation. */
  cardProposalIds?: string[];
  /** alert / confirm when a reminder or a card produced the attempt; null = auto. */
  signalAction?: "alert" | "confirm" | null;
  createdAt: string;
  updatedAt: string;
  legs?: StrategyExecutionLeg[];
}

/** Money from the Runtime: a number or a decimal string (null / absent = not known). */
export type Amount = number | string | null | undefined;
/**
 * 按资金比例定手数 (2026-10-09): the Runtime's own calculation for the strategy — 权益 × 比例 − 本策略已占（含挂单）
 * = 剩余额度; 剩余额度 ÷ 每手保证金 → 可开手数 (the final count also within the account's available funds, fees and
 * the user's cap). `unavailableReason` says why it cannot be worked out now (行情 / 账户 / 保证金缺失或过期).
 */
export interface StrategyFunds {
  equity?: Amount; equityPercent?: Amount; usedMargin?: Amount; pendingMargin?: Amount;
  remainingBudget?: Amount; marginPerLot?: Amount; availableLots?: number | null; unavailableReason?: string | null;
}
/**
 * 回撤停机 (2026-10-09): the strategy's own equity since this round started (起始), its peak, now and the fall from
 * the peak. `liquidating` = triggered, new opens stopped, its orders / position still being cancelled or closed;
 * `stopped` = all done, stays stopped until resumed with `resetDrawdown` (only while `canResume`).
 */
export interface StrategyDrawdown {
  status: "inactive" | "running" | "liquidating" | "stopped" | "unavailable";
  initialEquity?: Amount; peakEquity?: Amount; currentEquity?: Amount; drawdownPercent?: Amount; limitPercent?: Amount;
  reason?: string | null; canResume?: boolean;
  /** Why 恢复运行 is not allowed yet (position left, order not final, read failed); null while `canResume`. */
  resumeBlockedReason?: string | null;
  /** 手续费口径 of the strategy's equity: the fills' actual fees, estimated from the fee rates, or not known. */
  feeSource?: "actual" | "estimated" | "unavailable" | null;
  curve?: { time: number | string; equity: Amount }[] | null;
}

export interface StrategyMonitorSnapshot {
  /** 等待行情 (2026-10-12): `waitReason` (source code) / `waitingSince` / `stalled` (≥ 10 min) on status "waiting". */
  decision?: { status: string; detail?: string; checkedAt?: string; waitReason?: string | null; waitingSince?: string | null; stalled?: boolean } | null;
  strategyId: string;
  status: "draft" | "validating" | "ready" | "monitoring" | "paused" | "archived";
  monitorState?: StrategyMonitorState | null;
  /** Today's opens counted toward maxOrdersPerDay (the monitor's own count); null before the
   *  first monitor pass, absent on an older Runtime. */
  opensToday?: number | null;
  /** 执行方式 (2026-09-25): as set, and as it runs now (auto runs as confirm while AI 自主执行 is off). */
  signalAction?: "alert" | "confirm" | "auto";
  effectiveSignalAction?: "alert" | "confirm" | "auto";
  signalSessionId?: string | null;
  /** The account's current trading day, and the instance's figures for it — null when the monitor
   *  state is from another day (the card then shows —). */
  tradingDay?: string | null;
  today?: { tradingDate: string; pnl?: string | null; consecutiveLosses: number } | null;
  /** 停止 only: open positions stay; the notice says so. 停止并平仓 (2026-10-08): the close cards made
   *  for the position the strategy still held (none = nothing to close) and the chat they went to. */
  positionsKept?: boolean;
  notice?: string;
  closeCards?: { proposalId: string; symbol?: string; side?: string; volume?: number }[];
  closeSessionId?: string | null;
  /** 策略卡 (2026-10-08): the instance's own position, today's signals (every attempt of the account's
   *  trading day; null before it is known) and the 今日判断 it goes against (null = no clash). */
  position?: { direction: "long" | "short" | null; volume: number };
  signalsToday?: number | null;
  researchClash?: { stance: string; direction: string; text: string } | null;
  /** 按资金比例 / 回撤停机 (2026-10-09): absent = not used by this strategy (or an older Runtime). */
  funds?: StrategyFunds | null;
  drawdown?: StrategyDrawdown | null;
  recentAttempts: StrategyExecutionAttempt[];
  simulationOnly: true;
  liveTrading: "disabled";
}

export async function getStrategyMonitor(baseUrl: string, strategyId: string) {
  return requestJson<StrategyMonitorSnapshot>(
    `${baseUrl}/api/strategies/${encodeURIComponent(strategyId)}/monitor`
  );
}

/** 停止 / 停止并平仓 (2026-10-08): stopping never sends an order; with closePositions the Runtime puts a
 *  close card for each leg the strategy still holds in its conversation, for the user to confirm. */
export async function stopStrategyMonitoring(baseUrl: string, strategyId: string, closePositions: boolean) {
  return requestJson<StrategyMonitorSnapshot>(
    `${baseUrl}/api/strategies/${encodeURIComponent(strategyId)}/monitor/stop`,
    { method: "POST", body: JSON.stringify({ closePositions }) }
  );
}

export async function setStrategyMonitoring(
  baseUrl: string,
  strategyId: string,
  action: "start" | "pause"
) {
  return requestJson<StrategyMonitorSnapshot>(
    `${baseUrl}/api/strategies/${encodeURIComponent(strategyId)}/monitor/${action}`,
    { method: "POST", body: "{}" }
  );
}

export async function rejectProposal(baseUrl: string, proposalId: string, proposalVersion: number) {
  return requestJson(`${baseUrl}/api/proposals/${proposalId}/reject`, {
    method: "POST", body: JSON.stringify({ proposalVersion, idempotencyKey: idempotencyKey() })
  });
}

export async function cancelRun(baseUrl: string, runId: string) {
  return requestJson(`${baseUrl}/api/runs/${runId}/cancel`, { method: "POST" });
}

export function openEventStream(
  baseUrl: string,
  sessionId: string,
  onEvent: (event: UiEvent) => void,
  onWarning: (message: string) => void,
  onActivity: () => void,
  onConnection: (connected: boolean) => void
) {
  const streamUrl = `${baseUrl}/api/events?sessionId=${encodeURIComponent(sessionId)}`;
  const localToken = localAuthTokenFor(streamUrl);
  const handleData = (data: string) => {
    onActivity();
    let input: unknown;
    try {
      input = JSON.parse(data);
    } catch {
      onWarning("应用服务返回了无法识别的数据，已安全忽略。");
      return;
    }
    const parsed = parseUiEvent(input);
    if (parsed.ok) onEvent(parsed.event);
    else onWarning(parsed.warning.message);
  };

  // One transport for desktop and web (10-08): a native EventSource gives up for good on a single
  // non-200 answer, so the 502 of a blue-green switch left every open web page and phone offline
  // until a manual reload. The fetch stream retries every second and resumes from Last-Event-ID.
  return openSseStream({
    url: streamUrl,
    headers: localToken ? { Authorization: `Bearer ${localToken}` } : {},
    // The Runtime sends a heartbeat about every second: 20 s of silence is a dead stream, reopened before
    // the 30 s freshness check turns the page to 数据恢复中 (#745 P3-D).
    idleMs: 20_000,
    onOpen: () => { onActivity(); onConnection(true); },
    onClose: () => onConnection(false),
    onFrame: (frame) => {
      if (frame.comment || frame.event === "heartbeat") { onActivity(); return; }
      if (frame.data.length && (frame.event === "message" || frame.event === "transient")) handleData(frame.data.join("\n"));
    },
  });
}

export interface ModelCatalogEntry {
  slug: string;
  displayName: string;
  provider: string;
  tier: string;
  configured: boolean;
  selectable: boolean;
  memberOnly: boolean;
  /** M2: false when the user's tier does not include the model. */
  inPlan?: boolean;
}

export interface ModelCatalog {
  current: string | null;
  models: ModelCatalogEntry[];
  /** `dailyTokenLimit` / `voiceSecondsPerDay` are null when the tier is unlimited. */
  quota: {
    dailyTokenLimit: number | null;
    usedTokensToday: number;
    day: string;
    plan: string;
    planLabel: string;
    voiceSecondsToday: number;
    voiceSecondsPerDay: number | null;
  };
}

export async function getModelCatalog(baseUrl: string) {
  return requestJson<ModelCatalog>(`${baseUrl}/api/models`);
}

export async function setModelPreference(baseUrl: string, model: string) {
  return requestJson<ModelCatalog>(`${baseUrl}/api/models/preference`, {
    method: "PUT",
    body: JSON.stringify({ model }),
  });
}

/** One row of the 后台管理 console's 用户 table (M2). */
export interface AdminUser {
  username: string;
  role: string;
  plan: string;
  planLabel: string;
  invitedBy: string | null;
  createdAt: string;
  lastSeenAt: string | null;
  tokensToday: number;
  tokensMonth: number;
  voiceSecondsToday: number;
  voiceSecondsMonth: number;
  estimatedCostToday: number | null;
  disabled: boolean;
  tqkqBound: boolean;
}

/** Owner-only (others get 404); a plan change applies on the user's next request. */
export async function getAdminUsers(baseUrl: string) {
  return requestJson<{ items: AdminUser[]; day: string }>(`${baseUrl}/api/admin/users`);
}

/** One use of an invite code: who spent it and when (newest first in the row). */
export interface AdminInviteCodeUse { username: string; usedAt: string }

/** One row of the owner's invite-code list. `code` is null for codes made
 *  before 2026-10-01 — only their hash was stored, the text is gone. `status`
 *  is server-computed: disabled > expired > used_up > active. */
export interface AdminInviteCode {
  id: string;
  code: string | null;
  uses: "once" | "unlimited";
  useCount: number;
  usedBy: AdminInviteCodeUse[];
  plan: string | null;
  createdBy: string;
  createdAt: string;
  expiresAt: string | null;
  disabledAt: string | null;
  status: "active" | "used_up" | "expired" | "disabled";
}

export async function listInviteCodes(baseUrl: string) {
  return requestJson<AdminInviteCode[]>(`${baseUrl}/api/auth/owner/invite-codes`);
}

/** One grant row of the owner's read-only account check. */
export interface AdminAccountGrant { user: string; role: string }

export interface AdminAccount {
  id: string;
  displayName: string;
  providerKind: string;
  status: string;
  executionOwner: string;
  grants: AdminAccountGrant[];
}

/** Owner-only isolation check (others get 404): every trading account with its
 *  grants as usernames. Read-only — grants change through binding, not here. */
export async function getAdminAccounts(baseUrl: string) {
  return requestJson<{ items: AdminAccount[] }>(`${baseUrl}/api/admin/accounts`);
}

export type AdminServerRange = "1h" | "24h" | "7d";

/** One host's gauges as the infra console's probe reports them. Every field may be
 *  missing (a probe that has not reported resources yet); `diskTotalGB`/`diskUsedGB`
 *  are the system disk, `disks` lists every disk on a multi-disk Windows host. */
export interface AdminServerResources {
  cpuCores?: number; cpuPercent?: number;
  load1?: number; load5?: number; load15?: number;
  memTotalMB?: number; memUsedMB?: number;
  diskTotalGB?: number; diskUsedGB?: number;
  uptimeSeconds?: number; containers?: number;
  disks?: Array<{ name?: string; totalGB?: number; usedGB?: number }>;
}

export interface AdminServerHost {
  id: string;
  displayName: string;
  platform: string | null;
  role: string | null;
  checkedAt: string | null;
  /** "online" when the last 5-minute probe answered, "unknown" when it did not. */
  reachability: string | null;
  resources: AdminServerResources | null;
  series: Array<{ at: string; cpu?: number | null; mem?: number | null; disk?: number | null }>;
  servicesSummary: { total: number; running: number; failed: number };
}

/** `unconfigured` without the console token; `stale` marks the last good copy served
 *  while the console is unreachable. */
export type AdminServersSnapshot =
  | { status: "unconfigured" }
  | { status: "ok"; updatedAt: string; fetchedAt: string; hosts: AdminServerHost[]; stale?: boolean };

/** Owner-only: the project hosts' gauges from the infra console's 5-minute probe. */
export async function getAdminServers(baseUrl: string, range: AdminServerRange) {
  return requestJson<AdminServersSnapshot>(`${baseUrl}/api/admin/servers?range=${range}`);
}

/** 真实用户监测: one distribution's quantiles (bucket-interpolated; null without samples). */
export interface AdminRumFigures {
  n: number; p50: number | null; p75: number | null; p95: number | null;
  /** Quantiles that fell in the open last bucket: their value is a lower bound ("at least"). */
  atLeast?: Array<"p50" | "p75" | "p95">;
}

export interface AdminRumMetric extends AdminRumFigures {
  unit: string;
  byClient: Partial<Record<"web" | "desktop" | "mobile", AdminRumFigures>>;
  series: Array<{ t: number; n: number; p50: number | null; p95: number | null; atLeast?: Array<"p50" | "p95"> }>;
}

export interface AdminRumCounter {
  total: number;
  byClient: Partial<Record<"web" | "desktop" | "mobile", number>>;
  series: Array<{ t: number; n: number }>;
}

/** `GET /api/admin/rum`: the pages' anonymous minute metrics over the range (times in epoch seconds). */
export type AdminRumSnapshot =
  | { status: "unavailable"; enabled: boolean }
  | {
    status: "ok"; enabled: boolean; sampleRate: number; range: AdminServerRange; slotSeconds: number;
    from: number; to: number; retentionDays: number;
    totals: {
      sessions: number; users: number; visibleMinutes: number;
      byClient: Partial<Record<"web" | "desktop" | "mobile", { sessions: number; users: number }>>;
      byNetwork: Record<string, { sessions: number }>;
    };
    metrics: Partial<Record<"lcp" | "inp" | "cls" | "fcp" | "ttfb" | "price_latency" | "kline_first_paint" | "sse_down" | "memory", AdminRumMetric>>;
    counters: { sseReconnect: AdminRumCounter; errors: AdminRumCounter & { byCategory: Record<string, number> } };
  };

/** Owner-only: the 用户体验 summary (p50/p75/p95 per metric, per client, over time). */
export async function getAdminRum(baseUrl: string, range: AdminServerRange) {
  return requestJson<AdminRumSnapshot>(`${baseUrl}/api/admin/rum?range=${range}`);
}

/** One public market source in fill order: its K-line channels against the per-source
 *  cap, the quote symbols it last served, health and the last classified error. */
export interface AdminMarketSource {
  accountId: string;
  position: number;
  providerKind: string;
  health: "healthy" | "full" | "unavailable";
  channels: number;
  channelLimit: number;
  quoteSymbols: number;
  lastError: string | null;
  lastErrorAt: string | null;
  displayName: string | null;
  accountStatus: string;
  /** 行情专用: added here with its TqKq password, feeds the market only (移除 deletes it). */
  marketOnly?: boolean;
}

export interface AdminMarketSourcesSnapshot { items: AdminMarketSource[]; seriesPerSource: number }

/** 添加快期行情账号: the pool verifies the login, the account joins the end of the list. */
export async function addAdminMarketSourceAccount(baseUrl: string, username: string, password: string) {
  return requestJson<AdminMarketSourcesSnapshot & { accountId: string }>(`${baseUrl}/api/admin/market-sources/tqkq`, {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

/** 移除行情专用账号: out of the list, disabled, its sealed password deleted. */
export async function removeAdminMarketSourceAccount(baseUrl: string, accountId: string) {
  return requestJson<AdminMarketSourcesSnapshot>(`${baseUrl}/api/admin/market-sources/${encodeURIComponent(accountId)}`, {
    method: "DELETE",
  });
}

export async function getAdminMarketSources(baseUrl: string) {
  return requestJson<AdminMarketSourcesSnapshot>(`${baseUrl}/api/admin/market-sources`);
}

/** Replace the whole ordered source list (the position is the fill order). */
export async function updateAdminMarketSources(baseUrl: string, accountIds: string[]) {
  return requestJson<AdminMarketSourcesSnapshot>(`${baseUrl}/api/admin/market-sources`, {
    method: "PUT",
    body: JSON.stringify({ accountIds }),
  });
}

/** 后台 → 成员 → 重置密码 (2026-10-21): a site account only; that member's devices are all signed out. */
export function resetAdminUserPassword(baseUrl: string, username: string, newPassword: string) {
  return requestJson<{ status: "password_reset"; username: string }>(
    `${baseUrl}/api/admin/users/${encodeURIComponent(username)}/password`, { method: "POST", body: JSON.stringify({ newPassword }) });
}

export async function updateAdminUser(baseUrl: string, username: string, change: { plan?: string; disabled?: boolean }) {
  return requestJson<AdminUser>(`${baseUrl}/api/admin/users/${encodeURIComponent(username)}`, {
    method: "PUT",
    body: JSON.stringify(change),
  });
}

/** Owner-only: mint one invite code. `code` picks the text (4–32 chars of
 *  letters/digits/-/_, case-sensitive; blank = random); `uses` admits one
 *  login or unlimited; `neverExpires` ignores `daysValid` and the code stays
 *  valid (the response's expiresAt is then null). `plan` names the tier the
 *  first login through it lands on. */
export async function createInviteCode(baseUrl: string, invite: {
  daysValid: number; plan?: string; code?: string; uses?: "once" | "unlimited"; neverExpires?: boolean;
}) {
  const body: Record<string, unknown> = { daysValid: invite.daysValid };
  if (invite.plan) body.plan = invite.plan;
  if (invite.code) body.code = invite.code;
  if (invite.uses) body.uses = invite.uses;
  if (invite.neverExpires) body.neverExpires = invite.neverExpires;
  return requestJson<{ inviteCode: string; expiresAt: string | null; plan: string; uses: "once" | "unlimited" }>(
    `${baseUrl}/api/auth/owner/invite-codes`, { method: "POST", body: JSON.stringify(body) });
}

/** Owner-only kill switch for one code (id = the list row's id); idempotent —
 *  disabling twice returns the same row. */
export async function disableInviteCode(baseUrl: string, id: string) {
  return requestJson<AdminInviteCode>(
    `${baseUrl}/api/auth/owner/invite-codes/${encodeURIComponent(id)}/disable`, { method: "POST" });
}

/** One tier of the owner's 档位 administration (M2). `skills` is the tier's
 *  open tier-gated skills (the editor writes these); `features` is the 1.6.x
 *  derived spelling the old client still reads. `null` limits mean unlimited. */
export interface AdminPlan {
  name: string;
  label: string;
  isDefault: boolean;
  models: string[] | null;
  dailyTokens: number | null;
  voiceSecondsPerDay: number | null;
  marketHistoryTradingDays?: number | null;
  /** AI 简报品种上限 (config `briefProducts`); null/absent = the Runtime's default. */
  briefProducts?: number | null;
  skills: string[];
  features: Record<string, boolean>;
  userCount: number;
}

export interface AdminPlansSnapshot {
  items: AdminPlan[];
  skillCatalog: { id: string; label: string; category: string; description: string }[];
  featureCatalog: { key: string; label: string; description: string }[];
  modelCatalog: { slug: string; label: string }[];
}

/** Owner-only tier administration (others get 404); every change reads back
 *  from the database on the affected user's next request. */
export async function getAdminPlans(baseUrl: string) {
  return requestJson<AdminPlansSnapshot>(`${baseUrl}/api/admin/plans`);
}

export async function createAdminPlan(baseUrl: string, label: string, copyFrom?: string) {
  return requestJson<AdminPlan>(`${baseUrl}/api/admin/plans`, {
    method: "POST",
    body: JSON.stringify(copyFrom ? { label, copyFrom } : { label }),
  });
}

/** Partial update; `null` models/dailyTokens/voiceSecondsPerDay mean "all
 *  models" / "unlimited" (distinguished from "not provided" by key presence).
 *  `skills` merges (ids mentioned flip, the rest keep their state). */
export async function updateAdminPlan(
  baseUrl: string, name: string,
  change: Partial<Pick<AdminPlan, "label" | "isDefault">> & {
    models?: string[] | null; dailyTokens?: number | null; voiceSecondsPerDay?: number | null; marketHistoryTradingDays?: number | null;
    briefProducts?: number | null;
    skills?: Record<string, boolean>;
    features?: Record<string, boolean>;
  },
) {
  return requestJson<AdminPlan>(`${baseUrl}/api/admin/plans/${encodeURIComponent(name)}`, {
    method: "PUT",
    body: JSON.stringify(change),
  });
}

/** One registry skill on the owner's 技能 page: the global switch plus which
 *  tiers open it (the matrix view of one skill row). */
export interface AdminSkill {
  id: string;
  label: string;
  category: string;
  description: string;
  parent: string;
  summary: string;
  tierGated: boolean;
  ownerOnly: boolean;
  enabled: boolean;
  tiers: Record<string, boolean>;
}

export async function getAdminSkills(baseUrl: string) {
  return requestJson<{ items: AdminSkill[] }>(`${baseUrl}/api/admin/skills`);
}

export async function updateAdminSkill(baseUrl: string, id: string, enabled: boolean) {
  return requestJson<{ items: AdminSkill[] }>(`${baseUrl}/api/admin/skills/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify({ enabled }),
  });
}

export async function deleteAdminPlan(baseUrl: string, name: string) {
  return requestJson<{ status: string; name: string }>(`${baseUrl}/api/admin/plans/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

export async function registerOwnerAccount(
  baseUrl: string, username: string, password: string, inviteCode: string,
) {
  if (!username.trim() || !password || !inviteCode.trim()) {
    throw new Error("请完整填写账号、密码和邀请码");
  }
  const result = await requestJson<OwnerAuthStatus & {
    status: "registered";
    accessToken?: string;
  }>(`${baseUrl}/api/auth/owner/register`, {
    method: "POST",
    body: JSON.stringify({ username: username.trim(), password, inviteCode: inviteCode.trim() }),
  });
  ownerStatusReads.clear();
  if (isTauriRuntime()) {
    if (!result.accessToken) throw new Error("云端尚不支持桌面注册，请更新云端服务");
    const { invoke } = await import("@tauri-apps/api/core");
    setOwnerSessionToken(baseUrl, result.accessToken);
    await invoke("save_cloud_connection", { url: baseUrl, token: result.accessToken });
    delete result.accessToken;
  }
  changeLogin();
  resetReadIdentity();
  return result;
}

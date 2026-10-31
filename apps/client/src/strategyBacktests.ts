import { useSyncExternalStore } from "react";
import type { StrategyBacktestResult, StrategyBacktestTrade, StrategyHealthAdvice } from "@ai-trader/ui-events";
import { requestJson } from "./api";
import type { SavedStrategy, StrategyPlan } from "./strategyApi";
import { ruleBacktest, StaleEngine, type FallbackReason } from "./strategyEngine/browserEngine";

/**
 * 规则回测 (策略积木·第一期 B, 2026-09-25): the user's own rules over history, computed in this browser
 * when it can (本机优先, 2026-10-12: strategyEngine/browserEngine.ts) and otherwise by the Runtime
 * (`POST /api/strategies/{id}/backtest`, or `POST /api/strategy-backtests` with the plan). This
 * module keeps, for the window, the strategy selected on 详情 and each strategy's parameters, run
 * state and last result.
 */
export type { StrategyBacktestResult, StrategyBacktestTrade, StrategyHealthAdvice };
export interface BacktestParams { tradingDays: number | null; slippageTicks: number }
/** The Runtime runs 2 backtests at once, 1 per user; a later one waits first come first served. */
export interface BacktestQueueState { ahead: number }
export interface BacktestCard { params: BacktestParams; running: boolean; queue?: BacktestQueueState; result?: StrategyBacktestResult; error?: string; tradesOpen: boolean;
  /** The stored result was asked for once (策略页改版第八批). */
  loaded?: boolean }
export interface BacktestSelection { strategyId: string; name: string }

/** 策略页改版第八批 (2026-10-08): the one 回测 the page offers reads the last 6 months (≈120 trading
 *  days; a 1-minute strategy gets what the history holds and the 结果页 says the range it covered). */
export const DEFAULT_TRADING_DAYS = 120;
const DEFAULT_PARAMS: BacktestParams = { tradingDays: DEFAULT_TRADING_DAYS, slippageTicks: 0 };
let selection: BacktestSelection | null = null;
let cards: Record<string, BacktestCard> = {};
const listeners = new Set<() => void>();
const notify = () => listeners.forEach(listener => listener());
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };

const EMPTY: BacktestCard = { params: DEFAULT_PARAMS, running: false, tradesOpen: false };
export function backtestCard(strategyId: string | undefined): BacktestCard { return (strategyId && cards[strategyId]) || EMPTY; }
export function useBacktestCard(strategyId: string | undefined): BacktestCard {
  return useSyncExternalStore(subscribe, () => backtestCard(strategyId), () => backtestCard(strategyId));
}
export function backtestSelection(): BacktestSelection | null { return selection; }

/** 详情 publishes the strategy it shows; null when nothing is selected. */
export function selectBacktestStrategy(next: BacktestSelection | null) {
  if (selection?.strategyId === next?.strategyId && selection?.name === next?.name) return;
  selection = next;
  notify();
}

function update(strategyId: string, change: Partial<BacktestCard>) {
  cards = { ...cards, [strategyId]: { ...backtestCard(strategyId), ...change } };
  notify();
}

export function setBacktestParams(strategyId: string, params: Partial<BacktestParams>) {
  update(strategyId, { params: { ...backtestCard(strategyId).params, ...params } });
}
export function setBacktestTradesOpen(strategyId: string, open: boolean) { update(strategyId, { tradesOpen: open }); }

export interface BacktestRequest {
  strategyId: string;
  symbol: string;
  /** For strategies that follow the chart period. */
  timeframe?: number;
  /** Replays run the saved plan through /api/strategy-backtests (the child Runtime has no history). */
  plan?: StrategyPlan;
  /** Also post the result card to this conversation. */
  sessionId?: string;
  /** 本机优先 (2026-10-12): "auto" (default) computes in this browser when it can; "server" is the Runtime's
   *  authoritative rerun after a strategy is saved (a browser result is never the basis of a saved strategy). */
  where?: "auto" | "server";
}

/** Map a Runtime refusal to a sentence. */
export function backtestError(error: unknown): string {
  const detail = (error as { detail?: { detail?: unknown } })?.detail?.detail;
  const text = typeof detail === "string" ? detail : "";
  if (text === "strategy_backtest_history_unavailable" || text === "market_series_requires_sidecar") return "当前数据源拿不到历史K线，无法回测";
  if (text === "market_subscription_limit") return "行情订阅已达上限，稍后收盘空闲时会自动重置，再试一次";
  if (text === "symbol_not_found") return "找不到这个合约";
  // The Runtime explains history and rule refusals in Chinese; show them as they are.
  if (/[一-鿿]/.test(text)) return text;
  return text ? `回测未完成（${text}）` : "回测未完成，请检查连接后重试";
}

/** The card's badge while a run is pending: 排队中（前面还有 N 个）in the Runtime's queue, then 回测中…. */
export function backtestStatusLabel(card: Pick<BacktestCard, "running" | "queue">): string | undefined {
  if (!card.running) return undefined;
  return card.queue ? `排队中（前面还有 ${card.queue.ahead} 个）` : "回测中…";
}

export const QUEUE_POLL_MS = 1000;
const newRequestId = () => typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
  ? crypto.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;

/** While the request is pending, read its place in the Runtime's queue (GET /api/strategy-backtests/queue). */
function watchQueue(rootUrl: string, strategyId: string, requestId: string): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const poll = async () => {
    try {
      const reply = await requestJson<{ items?: { requestId: string | null; state: string; ahead: number }[] }>(
        `${rootUrl}/api/strategy-backtests/queue?requestId=${encodeURIComponent(requestId)}`);
      if (stopped) return;
      const item = reply.items?.find(entry => entry.requestId === requestId);
      const queue = item?.state === "queued" ? { ahead: item.ahead } : undefined;
      const card = backtestCard(strategyId);
      if (card.running && card.queue?.ahead !== queue?.ahead) update(strategyId, { queue });
    } catch {
      return;   // a Runtime without the queue endpoint: the card keeps 回测中…
    }
    if (!stopped) timer = setTimeout(() => void poll(), QUEUE_POLL_MS);
  };
  timer = setTimeout(() => void poll(), QUEUE_POLL_MS / 2);
  return () => { stopped = true; clearTimeout(timer); };
}

/** Run with the card's current parameters; the result stays on the card until the next run. */
export async function runStrategyBacktest(rootUrl: string, request: BacktestRequest): Promise<StrategyBacktestResult | undefined> {
  const { strategyId } = request;
  if (backtestCard(strategyId).running) return undefined;
  const { tradingDays, slippageTicks } = backtestCard(strategyId).params;
  update(strategyId, { running: true, queue: undefined, error: undefined });
  const body = { symbol: request.symbol, slippageTicks, ...(tradingDays ? { tradingDays } : {}),
    ...(request.timeframe ? { timeframe: request.timeframe } : {}), ...(request.sessionId ? { sessionId: request.sessionId } : {}) };
  // The Runtime's own run (its queue, watched for 排队中) — what the browser falls back to, and the
  // authoritative rerun after saving (`where: "server"`).
  const onServer = async (fallbackReason: FallbackReason) => {
    const requestId = newRequestId();
    const stopWatching = watchQueue(rootUrl, strategyId, requestId);
    try {
      const sent = JSON.stringify({ ...body, requestId, fallbackReason, ...(request.plan ? { plan: request.plan, strategyId } : {}) });
      return request.plan
        ? await requestJson<StrategyBacktestResult>(`${rootUrl}/api/strategy-backtests`, { method: "POST", body: sent })
        : await requestJson<StrategyBacktestResult>(`${rootUrl}/api/strategies/${encodeURIComponent(strategyId)}/backtest`, { method: "POST", body: sent });
    } finally {
      stopWatching();
    }
  };
  try {
    // 本机优先 (2026-10-12): this browser computes it when it can; the Runtime stores it either way.
    const result = await ruleBacktest<StrategyBacktestResult>(rootUrl,
      request.plan ? { ...body, plan: request.plan, strategyId } : { ...body, strategyId }, onServer, request.where);
    update(strategyId, { running: false, queue: undefined, result, error: undefined });
    return result;
  } catch (error) {
    update(strategyId, { running: false, queue: undefined, error: error instanceof StaleEngine ? error.message : backtestError(error) });
    return undefined;
  }
}

/** 结果页 opened: the strategy's newest stored result (`GET /api/strategy-backtests?strategyId=`, kept
 *  across restarts since 2026-10-08) fills the card — never over a run in flight or a newer result. */
export async function loadStoredBacktest(rootUrl: string, strategyId: string) {
  const card = backtestCard(strategyId);
  if (card.running || card.result || card.loaded) return;
  update(strategyId, { loaded: true });
  try {
    const reply = await requestJson<{ items?: StrategyBacktestResult[] }>(
      `${rootUrl}/api/strategy-backtests?strategyId=${encodeURIComponent(strategyId)}&limit=1`);
    const stored = reply.items?.[0];
    const now = backtestCard(strategyId);
    if (stored?.backtestId && stored.summary && stored.coverage && !now.running && !now.result) update(strategyId, { result: stored });
  } catch {
    // An older Runtime or a lost read: the card offers 回测 like before.
  }
}

/** AI 怎么看: the model's plain sentence for a stored result (`POST …/{id}/verdict`, written once and
 *  stored with it); the card keeps the fact sentences until it arrives, and a reply for a result the
 *  card no longer shows is dropped. */
export async function requestVerdict(rootUrl: string, strategyId: string, backtestId: string) {
  const asking = verdictRequests.get(backtestId);
  if (asking) return asking;
  const request = (async () => {
    try {
      const verdict = await requestJson<NonNullable<StrategyBacktestResult["verdict"]>>(
        `${rootUrl}/api/strategy-backtests/${encodeURIComponent(backtestId)}/verdict`, { method: "POST", body: "{}" });
      const card = backtestCard(strategyId);
      if (card.result?.backtestId === backtestId) update(strategyId, { result: { ...card.result, verdict } });
    } catch {
      // The facts stay on the page.
    } finally { verdictRequests.delete(backtestId); }
  })();
  verdictRequests.set(backtestId, request);
  return request;
}
const verdictRequests = new Map<string, Promise<void>>();

/** 按这个改 (策略体检, 2026-09-26): one suggestion of this strategy's backtest applied to its saved plan and
 * saved as the next version (the original stays). Never trades or starts monitoring. */
export function applyStrategyAdvice(rootUrl: string, strategyId: string, advice: StrategyHealthAdvice) {
  return requestJson<SavedStrategy>(`${rootUrl}/api/strategy-plans/${encodeURIComponent(strategyId)}/apply-advice`,
    { method: "POST", body: JSON.stringify({ advice }) });
}

/** Test helper. */
export function resetStrategyBacktests() { selection = null; cards = {}; notify(); }

export const EXIT_ORDER = ["rule", "stop_loss", "take_profit", "spread_stop", "spread_take", "trailing_stop", "breakeven",
  "close_before_end", "max_hold", "time_take_profit", "scale_out", "end_of_data"];

import { useSyncExternalStore } from "react";
import { requestJson } from "./api";
import { requestStatus, strategyError } from "./strategyApi";
import { computeLab, EngineFallback, StaleEngine, type FallbackReason } from "./strategyEngine/browserEngine";

/**
 * 试几组参数比一比 (策略页改版第八批, 2026-10-08; it replaced the 实验室 tab): the 结果页 compares the
 * strategy with two neighbours of its key parameter — three groups (`POST /api/strategy-plans/{id}/compare`),
 * read back with `GET` of the same path. 本机优先 (2026-10-12): the comparison is created with
 * `runOn: "browser"` and computed in this browser (strategyEngine/browserEngine.ts `computeLab`); when it
 * cannot, `POST /api/strategy-labs/{labId}/run-server` with the reason and the cloud run is followed. One
 * entry per strategy; a reply that belongs to an older request of the same strategy is dropped.
 */
export interface CompareSegment { trades: number; net: number; winRate?: number | null }
export interface CompareRow {
  variantId: string; label: string; current: boolean; error?: string | null;
  summary?: { netProfit: number; maxDrawdown: number; winRate: number | null; trades: number };
  inSample?: CompareSegment; outSample?: CompareSegment;
}
export interface CompareView {
  labId: string; strategyId: string | null; symbol: string; status: "ready" | "running" | "done" | "error" | "none";
  paramLabel: string | null; paramHelp: { what: string; up: string; down: string } | null;
  currentVariant: string | null; rows: CompareRow[]; best: string | null; line: string | null; error?: string | null;
  /** Where it was computed (null before; absent on an older Runtime) and why the cloud did it. */
  source?: "browser" | "server" | null; fallbackReason?: string | null;
}
export interface CompareCard { view?: CompareView; running: boolean; error?: string; loaded: boolean }

let cards: Record<string, CompareCard> = {};
const tokens: Record<string, number> = {};
const listeners = new Set<() => void>();
const notify = () => listeners.forEach(listener => listener());
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
const EMPTY: CompareCard = { running: false, loaded: false };
export const COMPARE_POLL_MS = 1500;

export function compareCard(strategyId: string | undefined): CompareCard { return (strategyId && cards[strategyId]) || EMPTY; }
export function useCompare(strategyId: string | undefined): CompareCard {
  return useSyncExternalStore(subscribe, () => compareCard(strategyId), () => compareCard(strategyId));
}
function update(strategyId: string, change: Partial<CompareCard>) {
  cards = { ...cards, [strategyId]: { ...compareCard(strategyId), ...change } };
  notify();
}
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
const comparePath = (rootUrl: string, strategyId: string) => `${rootUrl}/api/strategy-plans/${encodeURIComponent(strategyId)}/compare`;
const failure = (error: unknown, fallback: string) => error instanceof StaleEngine ? error.message : strategyError(error, fallback);

async function follow(rootUrl: string, strategyId: string, token: number, view: CompareView) {
  let current = view;
  while (current.status === "running" || current.status === "ready") {
    await delay(COMPARE_POLL_MS);
    if (tokens[strategyId] !== token) return;
    current = await requestJson<CompareView>(comparePath(rootUrl, strategyId));
    if (tokens[strategyId] !== token) return;
    update(strategyId, { view: current });
  }
  update(strategyId, { running: false, error: current.status === "error" ? current.error ?? "这次没比出来，请再试一次" : undefined });
}

/** A comparison created for this browser: computed here, else the cloud run started with the reason;
 *  then read (and followed) to its end. */
async function compute(rootUrl: string, strategyId: string, token: number, view: CompareView) {
  let reason: FallbackReason | null = null;
  try {
    await computeLab(rootUrl, view.labId);
  } catch (error) {
    if (error instanceof StaleEngine) throw error;
    reason = error instanceof EngineFallback ? error.reason : "engine_error";
  }
  if (tokens[strategyId] !== token) return;
  if (reason) {
    try {
      await requestJson(`${rootUrl}/api/strategy-labs/${encodeURIComponent(view.labId)}/run-server`,
        { method: "POST", body: JSON.stringify({ fallbackReason: reason }) });
    } catch (error) {
      if (requestStatus(error) !== 409) throw error;   // 409: the cloud is already on it
    }
  }
  const current = await requestJson<CompareView>(comparePath(rootUrl, strategyId));
  if (tokens[strategyId] !== token) return;
  update(strategyId, { view: current });
  await follow(rootUrl, strategyId, token, current);
}

/** The strategy's latest comparison (on opening its 结果页); a running one is followed to its end, one
 *  created for a browser that never computed it (the page went away) is computed now. */
export async function loadCompare(rootUrl: string, strategyId: string) {
  const card = compareCard(strategyId);
  if (card.loaded || card.running) return;
  const token = (tokens[strategyId] ?? 0) + 1;
  tokens[strategyId] = token;
  try {
    const view = await requestJson<CompareView>(comparePath(rootUrl, strategyId));
    if (tokens[strategyId] !== token) return;
    if (view.status === "none" || !view.labId) { update(strategyId, { loaded: true }); return; }   // never compared
    const running = view.status === "running" || view.status === "ready";
    update(strategyId, { view, loaded: true, running });
    if (view.status === "ready") await compute(rootUrl, strategyId, token, view);
    else if (running) await follow(rootUrl, strategyId, token, view);
  } catch (error) {
    if (tokens[strategyId] === token) update(strategyId, { loaded: true, running: false,
      ...(requestStatus(error) === 404 ? {} : { error: failure(error, "上次的比较读不到") }) });   // 404: an older Runtime
  }
}

/** 比一比: three groups of the key parameter on `symbol`, in this browser when it can (else the cloud). */
export async function startCompare(rootUrl: string, strategyId: string, symbol: string, timeframe?: number) {
  if (compareCard(strategyId).running) return;
  const token = (tokens[strategyId] ?? 0) + 1;
  tokens[strategyId] = token;
  update(strategyId, { running: true, error: undefined, loaded: true });
  try {
    const view = await requestJson<CompareView>(comparePath(rootUrl, strategyId), {
      method: "POST", body: JSON.stringify({ symbol, runOn: "browser", ...(timeframe ? { timeframe } : {}) }) });
    if (tokens[strategyId] !== token) return;
    update(strategyId, { view });
    if (view.status === "ready") await compute(rootUrl, strategyId, token, view);
    else await follow(rootUrl, strategyId, token, view);
  } catch (error) {
    if (tokens[strategyId] === token) update(strategyId, { running: false, error: failure(error, "这次没比出来，请再试一次") });
  }
}

/** Test helper. */
export function resetCompare() { cards = {}; for (const key of Object.keys(tokens)) delete tokens[key]; notify(); }

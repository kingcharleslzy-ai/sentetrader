import { requestJson } from "../api";
import { LONG_READ_TIMEOUT_MS } from "../requestTimeout";

/**
 * 策略实验室 (2026-09-30): the lab routes (`/api/strategy-labs…`), the engine bundle and the
 * template parameter page's “对比几组参数” grid. `rootUrl` is the same base the other strategy
 * interfaces use (the connected Runtime); every call goes through the authenticated requestJson.
 */
export interface LabVariant {
  id: string;
  label: string;
  plan: Record<string, unknown>;
  templateId?: string;
  params?: Record<string, number | string>;
  timeframe?: number | null;
}

export interface LabSegment {
  trades: number;
  wins: number;
  winRate: number | null;
  net: number;
  avgNet: number | null;
  maxDrawdown: number;
}

export interface LabResult {
  variantId: string;
  label?: string;
  error?: string | null;
  summary?: Record<string, unknown> | null;
  coverage?: Record<string, unknown> | null;
  inSample?: LabSegment | null;
  outSample?: LabSegment | null;
  splitTime?: number | null;
  /** 手机版回测结果页 (2026-10-06): the equity curve thinned to ≤ 60 points; absent on older labs. */
  equityCurve?: { time: number; equity: number }[] | null;
  tradesUsed?: number | null;
  /** How fees were counted (one fee schedule per lab); `warning` = 未扣手续费，结果偏乐观. */
  feeEstimate?: { source: string; warning: boolean; text: string } | null;
}

export interface LabRanking {
  tried: number;
  best: string | null;
  /** 09-30: no qualified variant made money in sample — nothing is recommended. */
  noProfit?: boolean;
  error?: string;
  items: { variantId: string; isRank: number | null; oosRank: number | null; decay: number | null; flags: string[] }[];
}

export interface Lab {
  id: string;
  symbol: string;
  status: "ready" | "running" | "done" | "error";
  source: "browser" | "server" | null;
  createdAt: string;
  updatedAt: string;
  request: { symbol: string; tradingDays: number | null; slippageTicks: number };
  variants: LabVariant[];
  /** The groups the parameter grid asked for that could not be built (created with skipInvalid). */
  skipped: { index: number; reason: string }[] | null;
  results: LabResult[] | null;
  ranking: LabRanking | null;
  chosen: { variantId: string; strategyId: string; backtestSummary: Record<string, unknown>; at: string } | null;
}

/** The list entry (`GET /api/strategy-labs`): enough to recognise the comparison. */
export interface LabSummary {
  id: string;
  symbol: string;
  status: Lab["status"];
  source: Lab["source"];
  createdAt: string;
  tried: number;
  best: string | null;
  /** 手机版 策略 → 最近的回测 (2026-10-06); absent on an older Runtime. The template's name (else the
   *  first variant's label), the period every variant shares, the last change and the recommended
   *  variant's out-of-sample net (null without one; noProfit = none made money in sample). */
  updatedAt?: string | null;
  name?: string | null;
  timeframeSeconds?: number | null;
  bestOutNet?: number | null;
  noProfit?: boolean;
}

export interface LabEngineBundle {
  digest: string;
  modules: Record<string, string>;
  entry: string;
  pyodide: { version: string; packages: string[] };
}

/** The rows in rank order (errors and unranked ones last), each with its flags and decay. */
export function rankedResults(lab: Lab | null): { result: LabResult; flags: string[]; decay: number | null; isRank: number | null; oosRank: number | null }[] {
  const items = lab?.ranking?.items ?? [];
  return (lab?.results ?? []).slice().sort((left, right) => {
    const a = items.find(item => item.variantId === left.variantId);
    const b = items.find(item => item.variantId === right.variantId);
    return (a?.isRank ?? Infinity) - (b?.isRank ?? Infinity);
  }).map(result => {
    const item = items.find(entry => entry.variantId === result.variantId);
    return { result, flags: item?.flags ?? [], decay: item?.decay ?? null, isRank: item?.isRank ?? null, oosRank: item?.oosRank ?? null };
  });
}

export function getLab(rootUrl: string, labId: string): Promise<Lab> {
  return requestJson<Lab>(`${rootUrl}/api/strategy-labs/${encodeURIComponent(labId)}`);
}

export function listLabs(rootUrl: string, limit = 5): Promise<{ items: LabSummary[] }> {
  return requestJson<{ items: LabSummary[] }>(`${rootUrl}/api/strategy-labs?limit=${limit}`);
}

export function engineBundle(rootUrl: string): Promise<LabEngineBundle> {
  return requestJson<LabEngineBundle>(`${rootUrl}/api/strategy-engine/bundle`, { timeoutMs: LONG_READ_TIMEOUT_MS });
}

/** 存为我的策略: the server re-runs the authoritative backtest first; `serverSummary` rides along
 * when its net differs from the browser's numbers. */
export function chooseLabVariant(rootUrl: string, labId: string, variantId: string, name?: string):
  Promise<{ strategyId: string; backtest: Record<string, unknown>; serverSummary?: Record<string, unknown> }> {
  return requestJson(`${rootUrl}/api/strategy-labs/${encodeURIComponent(labId)}/choose`,
    { method: "POST", body: JSON.stringify({ variantId, ...(name ? { name } : {}) }) });
}


import { useSyncExternalStore } from "react";
import type { StrategyBacktestResult } from "@ai-trader/ui-events";
import type { components } from "./workbench.generated";
import { formatSigned } from "./components/futures/support";
import { sameContract } from "./api";

/**
 * The AI annotation layer's state: surface highlights (a pulse and a bubble over a
 * control the AI is pointing at) and per-contract chart drawings. Both are the AI's
 * opinion for the user to look at; nothing here reaches trading. The workspace
 * reports them in `UiState` and applies the `highlight` / `chartAnnotations` commands.
 */
export type HighlightStyle = "pulse" | "outline";
export interface Highlight { target: string; label: string | null; style: HighlightStyle; until: string }
// The Runtime forwards only the fields the agent set: schema defaults (pane, tone) may be absent.
type ChartAnnotationItem = components["schemas"]["ChartAnnotationItem"];
export type ChartAnnotation = Omit<ChartAnnotationItem, "tone"> & { tone?: ChartAnnotationItem["tone"] };
/** What a command carries: `pane` defaults to the main chart when the agent left it out. */
export type ChartAnnotationInput = Omit<ChartAnnotation, "pane"> & { pane?: ChartAnnotation["pane"] };

export const HIGHLIGHT_MAX = 3;
/** One screen stays readable: six numbered drawings per contract (user feedback on 1.6.5). */
export const CHART_ANNOTATIONS_MAX = 6;

let highlights: Highlight[] = [];
let annotations: Record<string, ChartAnnotation[]> = {};
const listeners = new Set<() => void>();
const timers = new Map<string, ReturnType<typeof setTimeout>>();
function notify() { listeners.forEach(listener => listener()); }
function subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; }

/** Stable ids a `highlight` command may name: agent targets, test ids and chart regions. */
export const HIGHLIGHT_TARGET_SELECTOR = "[data-agent-target], [data-testid], [data-region]";
export function highlightTargetId(element: Element): string | null {
  return element.getAttribute("data-agent-target") ?? element.getAttribute("data-testid") ?? element.getAttribute("data-region");
}
export function resolveHighlightTarget(target: string): Element | null {
  const escaped = CSS.escape(target);
  return document.querySelector(`[data-agent-target="${escaped}"], [data-testid="${escaped}"], [data-region="${escaped}"]`);
}

export function currentHighlights(): Highlight[] { return highlights; }
export function useHighlights(): Highlight[] { return useSyncExternalStore(subscribe, currentHighlights, currentHighlights); }

function dropHighlight(target: string) {
  const timer = timers.get(target);
  if (timer) { clearTimeout(timer); timers.delete(target); }
  if (highlights.some(item => item.target === target)) { highlights = highlights.filter(item => item.target !== target); notify(); }
}

/** Show a highlight for `durationMs`; a fourth one retires the oldest. Throws when the target is not rendered. */
export function showHighlight(input: { target: string; label?: string | null; style?: HighlightStyle; durationMs: number }) {
  if (!resolveHighlightTarget(input.target)) throw new Error("界面上没有这个目标");
  dropHighlight(input.target);
  const until = new Date(Date.now() + input.durationMs).toISOString();
  const next = [...highlights, { target: input.target, label: input.label ?? null, style: input.style ?? "pulse", until }];
  highlights = next.slice(-HIGHLIGHT_MAX);
  timers.set(input.target, setTimeout(() => dropHighlight(input.target), input.durationMs));
  notify();
}

export function clearHighlights(target?: string) {
  if (target) { dropHighlight(target); return; }
  timers.forEach(timer => clearTimeout(timer));
  timers.clear();
  if (highlights.length) { highlights = []; notify(); }
}

// One stable empty array: useSyncExternalStore compares snapshots by identity.
const NONE: ChartAnnotation[] = [];
const bySymbol = (symbol: string) => annotations[symbol.toUpperCase()] ?? NONE;
export function chartAnnotationsFor(symbol: string | undefined): ChartAnnotation[] { return symbol ? bySymbol(symbol) : NONE; }
export function useChartAnnotations(symbol: string | undefined): ChartAnnotation[] {
  return useSyncExternalStore(subscribe, () => chartAnnotationsFor(symbol), () => chartAnnotationsFor(symbol));
}

/**
 * Two horizontal lines at one level on one pane say the same thing: the command is refused so the
 * agent learns it (a silently dropped item would never match its receipt).
 */
function dedupeLevels(items: ChartAnnotation[]): ChartAnnotation[] {
  const seen = new Set<string>();
  for (const item of items) {
    if (item.kind !== "hline") continue;
    const level = `${item.pane ?? "main"}:${item.price}`;
    if (seen.has(level)) throw new Error(`同一价位 ${item.price} 已有一条线，不再重复画`);
    seen.add(level);
  }
  return items;
}

/** `set` replaces, `add` appends (an existing id is replaced in place), `clear` empties the contract's drawings. */
export function applyChartAnnotations(symbol: string, op: "set" | "add" | "clear", input: ChartAnnotationInput[] = []) {
  const items: ChartAnnotation[] = input.map(item => ({ ...item, pane: item.pane ?? "main" }));
  const key = symbol.toUpperCase();
  let next: ChartAnnotation[];
  if (op === "clear") next = [];
  else if (op === "set") next = dedupeLevels(items);
  else {
    const incoming = new Map(items.map(item => [item.id, item]));
    next = dedupeLevels([...bySymbol(key).map(item => incoming.get(item.id) ?? item), ...items.filter(item => !bySymbol(key).some(existing => existing.id === item.id))]);
  }
  if (next.length > CHART_ANNOTATIONS_MAX) throw new Error(`同一屏最多 ${CHART_ANNOTATIONS_MAX} 条 AI 标注，请精简`);
  annotations = { ...annotations, [key]: next };
  if (op === "clear" && backtestMarks && sameContract(backtestMarks.symbol, key)) backtestMarks = null;
  notify();
}

/**
 * 规则回测的买卖点 (2026-09-25): one backtest's opens and closes on one contract and period, drawn
 * as chart markers next to the AI drawings. Not bound by the six-drawing limit (a backtest has
 * dozens of trades); “清除 AI 标注” (`chartAnnotations: clear`) clears them with the drawings.
 */
export interface BacktestMark { id: string; time: number; kind: "open" | "close"; direction: "long" | "short"; price: number; label: string }
export interface BacktestMarks { backtestId: string; symbol: string; durationSeconds: number; items: BacktestMark[] }
let backtestMarks: BacktestMarks | null = null;
export function currentBacktestMarks(): BacktestMarks | null { return backtestMarks; }
export function useBacktestMarks(): BacktestMarks | null { return useSyncExternalStore(subscribe, currentBacktestMarks, currentBacktestMarks); }
export function setBacktestMarks(value: BacktestMarks | null) { backtestMarks = value; notify(); }
/**
 * 在图上标出买卖点: a backtest's opens and closes as chart markers (short labels, the net on the close). `idPrefix`
 * tells the owner apart (策略编辑卡's preview marks are `editor:`, cleared when the card closes).
 */
export function backtestMarksOf(result: StrategyBacktestResult, idPrefix = ""): BacktestMarks {
  return { backtestId: `${idPrefix}${result.backtestId}`, symbol: result.symbol, durationSeconds: result.timeframeSeconds,
    items: result.trades.flatMap(trade => {
      const side = trade.direction === "long" ? "多" : "空";
      return [{ id: `${trade.id}:open`, time: trade.openTime, kind: "open" as const, direction: trade.direction, price: trade.openPrice, label: `开${side}` },
        { id: `${trade.id}:close`, time: trade.closeTime, kind: "close" as const, direction: trade.direction, price: trade.closePrice, label: `平${side} ${formatSigned(trade.net, 0)}` }];
    }) };
}
/** The markers for the chart now showing `symbol` at `durationSeconds`; none for another contract or period. */
export function backtestMarksFor(marks: BacktestMarks | null, symbol: string | undefined, durationSeconds: number): BacktestMark[] {
  return marks && sameContract(marks.symbol, symbol) && marks.durationSeconds === durationSeconds ? marks.items : NONE_MARKS;
}
const NONE_MARKS: BacktestMark[] = [];

/**
 * 清空图上标记 (2026-09-30): per-contract 成交标记清除时间, kept in localStorage so it survives a
 * refresh. Fills at or before the stamp are not drawn; new fills still appear. The workspace reports
 * the current contract's stamp in `UiState.marksCleared` and applies `chartMarks: "clear"`.
 * Keys are per user (`<prefix><workspace scope>:<SYMBOL>`; the scope is the one chat and preferences
 * use) so another login on this browser neither inherits nor hides its own fills; without a scope
 * (guest, not connected) the stamp lives in memory only. The former global keys
 * (`<prefix><SYMBOL>`) are deleted when seen and never inherited.
 */
const CHART_MARKS_CLEARED_PREFIX = "ai-trader:chart-marks-cleared:";
const clearedStamps = new Map<string, string>();
let clearedScope: string | undefined;
/** Load the stamps of the user now signed in (called by the workspace whenever its scope changes). */
export function setChartMarksScope(scope: string | undefined) {
  if (scope === clearedScope) return;
  clearedScope = scope; clearedStamps.clear();
  try {
    const own = scope ? `${CHART_MARKS_CLEARED_PREFIX}${scope}:` : undefined;
    for (let index = localStorage.length - 1; index >= 0; index -= 1) {
      const key = localStorage.key(index);
      if (!key?.startsWith(CHART_MARKS_CLEARED_PREFIX)) continue;
      if (own && key.startsWith(own)) clearedStamps.set(key.slice(own.length), localStorage.getItem(key) ?? "");
      else if (!key.slice(CHART_MARKS_CLEARED_PREFIX.length).includes(":")) localStorage.removeItem(key);
    }
  } catch { /* Storage unavailable. */ }
  notify();
}
export function chartMarksClearedAt(symbol: string | undefined): string | null {
  return symbol ? clearedStamps.get(symbol.toUpperCase()) || null : null;
}
export function useChartMarksClearedAt(symbol: string | undefined): string | null {
  return useSyncExternalStore(subscribe, () => chartMarksClearedAt(symbol), () => chartMarksClearedAt(symbol));
}
/** The right-click's 清空图上标记: clears the AI drawings and backtest marks, and retires the
 *  current contract's trade markers by stamping now. */
export function clearChartMarks(symbol: string) {
  const key = symbol.toUpperCase();
  applyChartAnnotations(key, "clear");
  const stamp = new Date().toISOString();
  clearedStamps.set(key, stamp);
  if (clearedScope) try { localStorage.setItem(`${CHART_MARKS_CLEARED_PREFIX}${clearedScope}:${key}`, stamp); } catch { /* Storage unavailable. */ }
  notify();
}
/** A fill (epoch nanos) shows only when it landed after the contract's clear stamp. */
export function fillVisibleAfterClear(symbol: string | undefined, tradeDateTime: number): boolean {
  const stamp = chartMarksClearedAt(symbol);
  if (!stamp) return true;
  const clearedAt = Date.parse(stamp);
  return Number.isNaN(clearedAt) || tradeDateTime > clearedAt * 1e6;
}

/** Test helper. */
export function resetAiAnnotations() {
  clearHighlights(); annotations = {}; backtestMarks = null; clearedStamps.clear(); clearedScope = undefined;
  try {
    for (let index = localStorage.length - 1; index >= 0; index -= 1) {
      const key = localStorage.key(index);
      if (key?.startsWith(CHART_MARKS_CLEARED_PREFIX)) localStorage.removeItem(key);
    }
  } catch { /* Storage unavailable. */ }
  notify();
}

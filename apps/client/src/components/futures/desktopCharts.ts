import { useCallback, useMemo, useRef, useState } from "react";
import { sameContract } from "../../api";
import type { DesktopChartsPreferences, DurationSeconds, PreferencesDocument } from "../../userPreferences";
import { CELL_DURATIONS } from "../mobile/mobileMarketPrefs";

/**
 * 电脑版多图 (2026-11-02): 单图 / 左右 2 格 / 2×2 四格, kept in the account preference `ui.desktopCharts`. The
 * selected cell (`active`) is the workspace chart itself — its contract and period are the window's — and the
 * other cells keep their own (symbol null = 自动补位). 同步周期 / 同步品种 make every cell show the selected cell's;
 * 同步十字线 lines the cursors up on one time. Pure helpers plus the hook; the receipt rule is the Runtime's
 * `_desktop_charts_matches`.
 */

export type ChartLayout = DesktopChartsPreferences["layout"];
export const CHART_LAYOUTS: { id: ChartLayout; label: string; cells: number }[] = [
  { id: "single", label: "单图", cells: 1 }, { id: "two", label: "左右 2 格", cells: 2 }, { id: "four", label: "2×2 四格", cells: 4 },
];
/** 同合约 4 周期: the four periods of the preset, cell by cell. */
export const PERIODS_PRESET: DurationSeconds[] = [300, 900, 3600, 86400];
const CELLS = 4;
const DURATIONS = new Set<number>(CELL_DURATIONS.map(item => item.duration));

export const DEFAULT_DESKTOP_CHARTS: DesktopChartsPreferences = {
  layout: "single", cells: Array.from({ length: CELLS }, () => ({ symbol: null, duration: 300 })), active: 0,
  syncDuration: false, syncSymbol: false, syncCrosshair: true,
};

export const layoutCells = (layout: ChartLayout) => CHART_LAYOUTS.find(item => item.id === layout)?.cells ?? 1;

/** The stored (possibly older or partial) document as a whole value; mirrors the Runtime's validators. */
export function normalizeDesktopCharts(raw: Partial<DesktopChartsPreferences> | undefined | null): DesktopChartsPreferences {
  const value = raw ?? {};
  const cells = Array.from({ length: CELLS }, (_, index) => {
    const cell = Array.isArray(value.cells) ? value.cells[index] : undefined;
    return { symbol: typeof cell?.symbol === "string" && cell.symbol ? cell.symbol : null,
      duration: DURATIONS.has(Number(cell?.duration)) ? Number(cell?.duration) as DurationSeconds : 300 };
  });
  const flag = (key: "syncDuration" | "syncSymbol" | "syncCrosshair") => typeof value[key] === "boolean" ? value[key] : DEFAULT_DESKTOP_CHARTS[key];
  return {
    layout: CHART_LAYOUTS.some(item => item.id === value.layout) ? value.layout! : "single",
    cells, active: Number.isInteger(value.active) && value.active! >= 0 && value.active! < CELLS ? value.active! : 0,
    syncDuration: flag("syncDuration"), syncSymbol: flag("syncSymbol"), syncCrosshair: flag("syncCrosshair"),
  };
}

export interface ShownCell { index: number; symbol: string | null; duration: DurationSeconds; auto: boolean; active: boolean }
interface Current { symbol?: string; duration?: number }

/** The selected cell's index in this layout (a 四格 selection past 左右 2 格 lands on its last cell). */
export const activeIndex = (charts: DesktopChartsPreferences) => Math.min(charts.active, layoutCells(charts.layout) - 1);

/** What each cell of the layout shows; an automatic cell takes the next of `autoSymbols` not already on screen. */
export function shownCells(charts: DesktopChartsPreferences, current: Current, autoSymbols: readonly string[]): ShownCell[] {
  const count = layoutCells(charts.layout);
  const active = activeIndex(charts);
  const own = { symbol: current.symbol ?? null, duration: (DURATIONS.has(Number(current.duration)) ? current.duration : 300) as DurationSeconds };
  const cells = charts.cells.slice(0, count);
  const taken = [own.symbol, ...cells.filter((_, index) => index !== active).map(cell => cell.symbol)].filter(Boolean) as string[];
  const pool = autoSymbols.filter((symbol, index, list) => list.findIndex(other => sameContract(other, symbol)) === index)
    .filter(symbol => !taken.some(other => sameContract(other, symbol)));
  return cells.map((cell, index) => {
    if (index === active) return { index, ...own, auto: false, active: true };
    const duration = charts.syncDuration ? own.duration : cell.duration;
    if (charts.syncSymbol) return { index, symbol: own.symbol, duration, auto: false, active: false };
    return cell.symbol ? { index, symbol: cell.symbol, duration, auto: false, active: false }
      : { index, symbol: pool.shift() ?? null, duration, auto: true, active: false };
  });
}

export interface DesktopChartsChange {
  preset?: "periods"; layout?: ChartLayout; active?: number;
  cells?: { index: number; symbol?: string | null; duration?: number | null }[];
  syncDuration?: boolean; syncSymbol?: boolean; syncCrosshair?: boolean;
}

/** What one change does: the preference value to write and the chart's own contract / period when the selected
 *  cell moves or changes. Applied in order preset → layout → cells → active → switches; throws a readable refusal. */
export function planDesktopCharts(change: DesktopChartsChange, charts: DesktopChartsPreferences, current: Current,
  autoSymbols: readonly string[], resolveSymbol: (symbol: string) => string | undefined): { charts: DesktopChartsPreferences; symbol?: string; duration?: DurationSeconds } {
  const next: DesktopChartsPreferences = { ...charts, cells: charts.cells.map(cell => ({ ...cell })) };
  const cur: Current = { ...current };
  if (change.preset === "periods") {
    Object.assign(next, { layout: "four", syncSymbol: true, syncDuration: false, active: 0,
      cells: PERIODS_PRESET.map(duration => ({ symbol: null, duration })) });
    cur.duration = PERIODS_PRESET[0];
  } else if (change.preset !== undefined) throw new Error("preset 只有 periods（同合约 4 周期）");
  if (change.layout !== undefined) {
    if (!CHART_LAYOUTS.some(item => item.id === change.layout)) throw new Error("没有这个多图布局");
    next.layout = change.layout;
    if (change.layout !== "single") next.active = activeIndex(next);
  }
  const count = layoutCells(next.layout);
  const placed = new Set<number>();
  for (const cell of change.cells ?? []) {
    if (next.layout === "single") throw new Error("单图没有格子，先切到左右 2 格或四格");
    if (!Number.isInteger(cell.index) || cell.index < 0 || cell.index >= count) throw new Error(`这个布局只有 ${count} 格`);
    if (cell.duration != null && !DURATIONS.has(cell.duration)) throw new Error("没有这个周期");
    const canonical = typeof cell.symbol === "string" ? resolveSymbol(cell.symbol) : undefined;
    if (typeof cell.symbol === "string" && !canonical) throw new Error(`${cell.symbol} 不在合约列表里`);
    if (cell.index === next.active) {
      if (cell.symbol === null) throw new Error("选中格就是图表本身，不能设为自动");
      if (canonical) cur.symbol = canonical;
      if (cell.duration != null) cur.duration = cell.duration;
      continue;
    }
    if (canonical) placed.add(cell.index);
    next.cells[cell.index] = { symbol: "symbol" in cell ? canonical ?? null : next.cells[cell.index].symbol,
      duration: cell.duration != null ? cell.duration as DurationSeconds : next.cells[cell.index].duration };
  }
  if (change.active !== undefined) {
    if (next.layout === "single") throw new Error("单图没有格子，先切到左右 2 格或四格");
    if (!Number.isInteger(change.active) || change.active < 0 || change.active >= count) throw new Error(`这个布局只有 ${count} 格`);
    if (change.active !== next.active) {
      const target = shownCells(next, cur, autoSymbols)[change.active];
      if (!target.symbol) throw new Error("这一格还没有合约");
      // The cell it leaves keeps what it showed.
      next.cells[next.active] = { symbol: cur.symbol ?? null, duration: (cur.duration ?? 300) as DurationSeconds };
      next.active = change.active;
      cur.symbol = target.symbol;
      cur.duration = target.duration;
    }
  }
  for (const key of ["syncDuration", "syncSymbol", "syncCrosshair"] as const) if (change[key] !== undefined) next[key] = Boolean(change[key]);
  if (next.syncSymbol && placed.size) throw new Error("同步品种开着时每格都是选中格的合约；要各看各的，同时给 syncSymbol: false");
  return { charts: next,
    ...(cur.symbol && !sameContract(cur.symbol, current.symbol ?? "") ? { symbol: cur.symbol } : {}),
    ...(cur.duration !== undefined && cur.duration !== current.duration ? { duration: cur.duration as DurationSeconds } : {}) };
}

export interface DesktopChartsHandle {
  charts: DesktopChartsPreferences;
  cells: ShownCell[];
  /** The `desktopCharts` UI command and every click on the grid's controls: throws the reason a change is refused. */
  apply: (change: DesktopChartsChange) => void;
}

/**
 * A member's grid: shown at once, the account preference written behind it; a failed write falls back to the saved
 * document. `select` switches the window's own contract and period (the selected cell).
 */
export function useDesktopCharts({ document, connected, update, current, autoSymbols, resolveSymbol, select }: {
  document: Partial<DesktopChartsPreferences> | undefined;
  connected: boolean;
  update: (patch: Record<string, unknown>) => Promise<PreferencesDocument>;
  current: Current;
  autoSymbols: readonly string[];
  resolveSymbol: (symbol: string) => string | undefined;
  select: (symbol: string | undefined, duration: DurationSeconds | undefined) => void;
}): DesktopChartsHandle {
  const [pending, setPending] = useState<DesktopChartsPreferences>();
  const charts = useMemo(() => pending ?? normalizeDesktopCharts(document), [pending, document]);
  const cells = useMemo(() => shownCells(charts, current, autoSymbols), [charts, current, autoSymbols]);
  const latest = useRef({ charts, current, autoSymbols, resolveSymbol, select });
  latest.current = { charts, current, autoSymbols, resolveSymbol, select };
  const apply = useCallback((change: DesktopChartsChange) => {
    const { charts: before, current: now, autoSymbols: auto, resolveSymbol: resolve, select: choose } = latest.current;
    if (!connected) throw new Error("连接后可设置多图");
    const plan = planDesktopCharts(change, before, now, auto, resolve);
    setPending(plan.charts);
    if (plan.symbol || plan.duration) choose(plan.symbol, plan.duration);
    const settle = () => setPending(shown => shown === plan.charts ? undefined : shown);
    void update({ ui: { desktopCharts: plan.charts } }).then(settle, settle);
  }, [connected, update]);
  return { charts, cells, apply };
}

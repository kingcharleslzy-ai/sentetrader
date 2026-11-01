import type { DurationSeconds, MobileChartCell, MobileMarketPreferences } from "../../userPreferences";
import { SUB_PANE_NAMES, SUB_PANE_TYPES, SUB_PANES_MAX, type SubPaneType } from "../chartIndicators";
import { sameContract } from "../../api";

/**
 * 手机看盘 (2026-10-09): the 看盘 tab's 报价 / 多图 / 单图, kept in the account preference `ui.mobileMarket`
 * (a visitor keeps it on this device). Pure helpers: the defaults and normalizer, the 多图 cell resolution,
 * the `mobileMarket` command's plan and its receipt rule (the same as the Runtime's `_mobile_market_matches`).
 */

export type MarketView = MobileMarketPreferences["view"];
export type QuoteBoard = MobileMarketPreferences["board"];
export type QuoteColumn = MobileMarketPreferences["columns"][number];
export type MultiLayout = MobileMarketPreferences["layout"];

export const MARKET_VIEWS: { id: MarketView; label: string }[] = [
  { id: "quotes", label: "报价" }, { id: "multi", label: "多图" }, { id: "single", label: "单图" },
];
export const QUOTE_BOARDS: { id: QuoteBoard; label: string }[] = [
  { id: "watchlist", label: "自选" }, { id: "main", label: "主力" }, { id: "SHFE", label: "上期所" }, { id: "DCE", label: "大商所" },
  { id: "CZCE", label: "郑商所" }, { id: "CFFEX", label: "中金所" }, { id: "GFEX", label: "广期所" }, { id: "INE", label: "能源中心" },
  { id: "FOREIGN", label: "外盘" },
];
export const QUOTE_COLUMNS: { id: QuoteColumn; label: string }[] = [
  { id: "last", label: "最新" }, { id: "changePercent", label: "涨跌幅" }, { id: "change", label: "涨跌" }, { id: "openInterest", label: "持仓量" },
  { id: "bid", label: "买价" }, { id: "ask", label: "卖价" }, { id: "volume", label: "成交量" }, { id: "openInterestChange", label: "日增仓" },
];
export const QUOTE_COLUMNS_MAX = 4;
export const MULTI_LAYOUTS: { id: MultiLayout; label: string; cells: number }[] = [
  { id: "four", label: "4 合约", cells: 4 }, { id: "periods", label: "同合约 4 周期", cells: 4 }, { id: "six", label: "6 图", cells: 6 },
];
export const CELL_DURATIONS: { duration: DurationSeconds; label: string }[] = [
  { duration: 60, label: "1分" }, { duration: 180, label: "3分" }, { duration: 300, label: "5分" }, { duration: 900, label: "15分" },
  { duration: 1800, label: "30分" }, { duration: 3600, label: "1小时" }, { duration: 86400, label: "日线" }, { duration: 604800, label: "周线" },
];
export const durationLabel = (duration: number) => CELL_DURATIONS.find(item => item.duration === duration)?.label ?? `${duration}秒`;

const MULTI_CELLS = 6;
export const DEFAULT_MOBILE_MARKET: MobileMarketPreferences = {
  view: "single", board: "watchlist", columns: ["last", "changePercent", "openInterest"], layout: "four",
  cells: Array.from({ length: MULTI_CELLS }, () => ({ symbol: null, duration: 300 })), periods: [300, 900, 3600, 86400], subPane: null,
};

const DURATIONS = new Set<number>(CELL_DURATIONS.map(item => item.duration));
const oneOf = <T extends string>(value: unknown, allowed: readonly { id: T }[], fallback: T): T =>
  allowed.some(item => item.id === value) ? value as T : fallback;

/** The stored (possibly older or partial) document as a whole value; mirrors the Runtime's validators. */
export function normalizeMobileMarket(raw: Partial<MobileMarketPreferences> | undefined | null): MobileMarketPreferences {
  const value = raw ?? {};
  const columns = Array.isArray(value.columns)
    ? value.columns.filter((item, index, list) => QUOTE_COLUMNS.some(column => column.id === item) && list.indexOf(item) === index).slice(0, QUOTE_COLUMNS_MAX)
    : [];
  const cells = Array.from({ length: MULTI_CELLS }, (_, index): MobileChartCell => {
    const cell = Array.isArray(value.cells) ? value.cells[index] : undefined;
    return { symbol: typeof cell?.symbol === "string" && cell.symbol ? cell.symbol : null,
      duration: DURATIONS.has(Number(cell?.duration)) ? Number(cell?.duration) as DurationSeconds : 300 };
  });
  const periods = Array.isArray(value.periods) && value.periods.length === 4 && value.periods.every(item => DURATIONS.has(Number(item)))
    ? value.periods.map(Number) as DurationSeconds[] : [...DEFAULT_MOBILE_MARKET.periods];
  return {
    view: oneOf(value.view, MARKET_VIEWS, "single"),
    board: oneOf(value.board, QUOTE_BOARDS, "watchlist"),
    columns: columns.length ? columns : [...DEFAULT_MOBILE_MARKET.columns],
    layout: oneOf(value.layout, MULTI_LAYOUTS, "four"),
    cells, periods,
    subPane: SUB_PANE_TYPES.includes(value.subPane as SubPaneType) ? value.subPane as SubPaneType : null,
  };
}

/** The pane the phone's single chart shows: the remembered one while it is chosen, else the first chosen. */
export function shownSubPane(market: MobileMarketPreferences, panes: readonly SubPaneType[]): SubPaneType | null {
  return market.subPane && panes.includes(market.subPane) ? market.subPane : panes[0] ?? null;
}

export interface ResolvedCell { index: number; symbol: string | null; duration: DurationSeconds; auto: boolean }

/**
 * The cells the 多图 layout shows. 同合约 4 周期: the chart's own contract at the four periods. 4 合约 / 6 图: a
 * chosen cell keeps its contract; an automatic one takes the next of `autoSymbols` (the current contract, 自选,
 * then the busiest main contracts) not already on screen.
 */
export function resolveCells(market: MobileMarketPreferences, current: string | undefined, autoSymbols: readonly string[]): ResolvedCell[] {
  if (market.layout === "periods") return market.periods.map((duration, index) => ({ index, symbol: current ?? null, duration, auto: false }));
  const count = MULTI_LAYOUTS.find(item => item.id === market.layout)?.cells ?? 4;
  const cells = market.cells.slice(0, count);
  const taken = new Set(cells.flatMap(cell => cell.symbol ? [cell.symbol.toUpperCase()] : []));
  const pool = [current, ...autoSymbols].filter((symbol): symbol is string => Boolean(symbol))
    .filter((symbol, index, list) => list.findIndex(other => sameContract(other, symbol)) === index)
    .filter(symbol => !taken.has(symbol.toUpperCase()));
  return cells.map((cell, index) => cell.symbol
    ? { index, symbol: cell.symbol, duration: cell.duration, auto: false }
    : { index, symbol: pool.shift() ?? null, duration: cell.duration, auto: true });
}

export interface MobileMarketChange {
  view?: MarketView; board?: QuoteBoard; columns?: QuoteColumn[]; layout?: MultiLayout;
  cell?: { index: number; symbol?: string | null; duration?: number | null };
  subPane?: SubPaneType; subPanes?: boolean;
}

/** What one change does: the preference values to write, the chart's pane list, a contract to switch to
 *  (同合约 4 周期's contract is the chart's), the 副图设置 sheet. Throws a readable reason for a refusal. */
export interface MobileMarketPlan { market?: Partial<MobileMarketPreferences>; subPaneList?: SubPaneType[]; symbol?: string; sheet?: boolean }

export function planMobileMarketChange(change: MobileMarketChange, market: MobileMarketPreferences, panes: readonly SubPaneType[],
  resolveSymbol: (symbol: string) => string | undefined): MobileMarketPlan {
  const keys = Object.keys(change) as (keyof MobileMarketChange)[];
  if (keys.length !== 1) throw new Error("mobileMarket 一次只改一项");
  if (change.view !== undefined) return { market: { view: oneOf(change.view, MARKET_VIEWS, market.view) } };
  if (change.board !== undefined) {
    if (!QUOTE_BOARDS.some(item => item.id === change.board)) throw new Error("没有这个报价板块");
    return { market: { view: "quotes", board: change.board } };
  }
  if (change.columns !== undefined) {
    const columns = change.columns;
    if (!columns.length || columns.length > QUOTE_COLUMNS_MAX || new Set(columns).size !== columns.length
      || columns.some(item => !QUOTE_COLUMNS.some(column => column.id === item))) throw new Error(`报价列选 1–${QUOTE_COLUMNS_MAX} 个、不重复`);
    return { market: { view: "quotes", columns: [...columns] } };
  }
  if (change.layout !== undefined) {
    if (!MULTI_LAYOUTS.some(item => item.id === change.layout)) throw new Error("没有这个多图布局");
    return { market: { view: "multi", layout: change.layout } };
  }
  if (change.cell !== undefined) {
    const { index, symbol, duration } = change.cell;
    const count = MULTI_LAYOUTS.find(item => item.id === market.layout)?.cells ?? 4;
    if (!Number.isInteger(index) || index < 0 || index >= count) throw new Error(`这个布局只有 ${count} 格`);
    if (duration != null && !DURATIONS.has(duration)) throw new Error("没有这个周期");
    const canonical = typeof symbol === "string" ? resolveSymbol(symbol) : undefined;
    if (typeof symbol === "string" && !canonical) throw new Error("合约不在合约列表里");
    if (market.layout === "periods") {
      if (symbol === null) throw new Error("同合约 4 周期跟着当前合约，不能设为自动");
      const periods = duration != null ? market.periods.map((item, at) => at === index ? duration as DurationSeconds : item) : undefined;
      return { market: { view: "multi", ...(periods ? { periods } : {}) }, ...(canonical ? { symbol: canonical } : {}) };
    }
    const cells = market.cells.map((cell, at) => at !== index ? cell : {
      symbol: "symbol" in change.cell! ? canonical ?? null : cell.symbol,
      duration: duration != null ? duration as DurationSeconds : cell.duration,
    });
    return { market: { view: "multi", cells } };
  }
  if (change.subPane !== undefined) {
    const type = change.subPane;
    if (!SUB_PANE_TYPES.includes(type)) throw new Error("没有这个副图指标");
    if (panes.includes(type)) return { market: { view: "single", subPane: type } };
    // Not chosen yet: it takes the shown pane's place when three are chosen, or joins the list.
    const shown = shownSubPane(market, panes);
    const list = panes.length < SUB_PANES_MAX ? [...panes, type] : panes.map(item => item === shown ? type : item);
    return { market: { view: "single", subPane: type }, subPaneList: list };
  }
  if (change.subPanes !== undefined) return { market: { view: "single" }, sheet: change.subPanes };
  throw new Error("mobileMarket 需要 view / board / columns / layout / cell / subPane / subPanes 之一");
}

/** 副图设置's toggle: a chosen pane leaves the list, a new one joins it (at most three). */
export function toggleSubPane(panes: readonly SubPaneType[], type: SubPaneType): SubPaneType[] {
  if (panes.includes(type)) return panes.filter(item => item !== type);
  if (panes.length >= SUB_PANES_MAX) throw new Error(`最多 ${SUB_PANES_MAX} 个副图，先关掉一个`);
  return [...panes, type];
}

export const subPaneName = (type: SubPaneType) => type === "kd" ? "KDJ" : SUB_PANE_NAMES[type];

/** The receipt rule (mirrors ui_control.py `_mobile_market_matches`): the 看盘 tab, and the account document
 *  the window reports holds the value; a window without the document (a visitor) keeps its own receipt. */
export function mobileMarketMatches(state: Record<string, unknown>, value: MobileMarketChange): boolean {
  if (state.mobileTab !== "market") return false;
  if (value.subPanes !== undefined) {
    const disclosures = state.disclosures as Record<string, boolean> | null | undefined;
    return disclosures == null || Boolean(disclosures["mobile-subpanes"]) === value.subPanes;
  }
  const preferences = state.preferences as { ui?: { mobileMarket?: Partial<MobileMarketPreferences> }; chart?: { subPaneList?: string[] } } | null | undefined;
  const market = preferences?.ui?.mobileMarket;
  if (!market) return true;
  const key = Object.keys(value)[0] as keyof MobileMarketChange;
  const views: Partial<Record<keyof MobileMarketChange, MarketView>> = { board: "quotes", columns: "quotes", layout: "multi", cell: "multi", subPane: "single" };
  if (market.view !== (views[key] ?? value.view)) return false;
  if (key === "board" || key === "layout" || key === "subPane") {
    if (market[key] !== value[key]) return false;
  }
  if (key === "columns" && JSON.stringify(market.columns) !== JSON.stringify(value.columns)) return false;
  if (key === "subPane") return (preferences?.chart?.subPaneList ?? []).includes(value.subPane as string);
  if (key === "cell" && value.cell) {
    const { index, symbol, duration } = value.cell;
    if (market.layout === "periods") {
      if (duration != null && market.periods?.[index] !== duration) return false;
      return !symbol || String(state.symbol ?? "").toUpperCase() === symbol.toUpperCase();
    }
    const cell = market.cells?.[index];
    if ("symbol" in value.cell && String(cell?.symbol ?? "").toUpperCase() !== String(symbol ?? "").toUpperCase()) return false;
    return duration == null || cell?.duration === duration;
  }
  return true;
}

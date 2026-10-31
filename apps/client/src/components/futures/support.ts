import type { AccountPosition, FuturesWorkspacePromptContext, InstrumentCatalog, MarketWorkbenchSnapshot, SimulationOrder } from "../../api";
import type { TradingChartSettings } from "../TradingCharts";
import { DEFAULT_MA_PERIODS } from "../chartIndicators";
import { tickDecimals } from "../../chartDrawingModel";
import { CHART_SETTINGS_STORAGE_KEY } from "../../theme";

export type FuturesWorkspaceView = "watch" | "trade" | "review" | "board";
export type PriceMode = "limit" | "opponent" | "last";
/** 平今 / 平昨 the user picked on the ticket; "auto" (the default) leaves the split to the Runtime's planner
 *  (设置 → 交易 → 平今平昨 over the closable volumes; a close spanning both comes back as one batch card). */
export type CloseBucket = "auto" | "today" | "historical";
/**
 * The one open side panel (右侧栏重设计 v2, 1.6.6): 对话 / 策略 / 新闻 / 下单 / 持仓.
 * "positions" is a real side panel now, not the bottom bar's expanded state. The 价格阶梯 panel
 * ("depth") was removed 2026-10-07; its 五档 live in the 下单面板 and `panel: depth` opens that.
 */
export type RightDock = "ai" | "strategy" | "news" | "reminders" | "order" | "positions" | null;
/** The 策略面板's tabs (策略库/详情/运行中 2026-09-25; 实验室 2026-09-30). */
/** 策略面板 (2026-10-08): 我的策略, 正在运行, and the selected strategy's 结果页 (the retired lab = detail). */
export type StrategyTab = "playbooks" | "library" | "detail" | "running";
export type DockSide = "left" | "right";
/** 设置 → 界面 → AI 形象: K 线音柱 / 灵动双眼 / 像素小牛. */
export type AiAvatar = "kline" | "eyes" | "pixel";
export const AI_AVATAR_OPTIONS: { value: AiAvatar; label: string }[] = [
  { value: "kline", label: "K 线音柱" }, { value: "eyes", label: "灵动双眼" }, { value: "pixel", label: "像素小牛" }];
export type RecentWorkspaceAction = FuturesWorkspacePromptContext["recentActions"][number];

/** 报价总表: the four columns a command may sort by (the Runtime's boardSort key enum). */
export type BoardSortKey = "changePercent" | "volume" | "openInterest" | "openInterestChange";
export type BoardSort = { key: BoardSortKey; order: "asc" | "desc" };
export const DEFAULT_BOARD_SORT: BoardSort = { key: "changePercent", order: "desc" };
const BOARD_SORT_KEYS: ReadonlySet<string> = new Set(["changePercent", "volume", "openInterest", "openInterestChange"]);
/** The board's exchange filter; “all”, “watchlist” (自选), one exchange code or “FOREIGN” (外盘, its own
 *  read: GET /api/market/foreign) — the Runtime's enum. */
export type BoardExchange = "all" | "watchlist" | "SHFE" | "INE" | "DCE" | "CZCE" | "CFFEX" | "GFEX" | "FOREIGN";
export const FOREIGN_BOARD = "FOREIGN";

/** 外盘图表的数据来源 (所有者 2026-10-19 方案 A): “MT5 · GCEZ26” when MT5's main month draws the chart (minute to
 *  hourly, `chartSource` the packet's); “快期主连” for the other 快期外盘主连 charts (daily, weekly, or MT5 unavailable),
 *  with “· 报价 GCEZ26” when the quote beside it is that MT5 month's (外盘盘点 D); nothing for domestic contracts. */
export function foreignChartSource(symbol: string | undefined, quoteSource: string | undefined, chartSource: string | undefined): string | undefined {
  if (chartSource?.startsWith("MT5")) return chartSource;
  if (!symbol?.toUpperCase().startsWith("KQD.")) return undefined;
  return quoteSource?.startsWith("MT5") ? `快期主连 · 报价 ${quoteSource.replace(/^MT5 · /, "")}` : "快期主连";
}
/** 外盘合约（外盘二期）：快期外盘主连（KQD.）或美元子账户的月份合约（CME.）——钱按美元算。 */
export function isForeignSymbol(symbol: string | undefined): boolean {
  return /^(KQD|CME)\./i.test(symbol ?? "");
}
/** 金额的单位字：外盘合约是美元，国内是元。 */
export function moneyUnit(symbol: string | undefined): string {
  return isForeignSymbol(symbol) ? "美元" : "元";
}
/** The exchange chips, shown in this order; only ones present in the catalog render. */
export const BOARD_EXCHANGES: { code: Exclude<BoardExchange, "all" | "watchlist" | "FOREIGN">; label: string }[] = [
  { code: "SHFE", label: "上期所" }, { code: "INE", label: "能源中心" }, { code: "DCE", label: "大商所" },
  { code: "CZCE", label: "郑商所" }, { code: "CFFEX", label: "中金所" }, { code: "GFEX", label: "广期所" },
];
/** The exchange codes a stored filter value may hold (BOARD_EXCHANGES plus “all” and “watchlist”). */
const BOARD_EXCHANGE_CODES: ReadonlySet<string> = new Set(["all", "watchlist", FOREIGN_BOARD, ...BOARD_EXCHANGES.map(entry => entry.code)]);
export function normalizeBoardExchange(value: unknown): BoardExchange {
  return typeof value === "string" && BOARD_EXCHANGE_CODES.has(value) ? value as BoardExchange : "all";
}

// 自选品种 (2026-10-02): the starred products — one account list the board's 自选 filter, the
// drawer's product rows and the contract head share. Entries are "<exchangeId>.<productId>"; a foreign product
// is its 品种目录键 (“COMEX.GC”, the 快期外盘主连's part after “@”, also the 外盘 board row's key) — the Runtime's
// instruments.PRODUCT_KEY_PATTERN is the same rule (外盘代码统一 2026-10-21).
export const WATCHLIST_MAX = 50;
const WATCHLIST_PRODUCT = /^[A-Z]{2,6}\.[A-Za-z0-9]{1,10}$/;
/** 国内交易所（自选键前缀是它们之一的就是国内品种；其余是外盘品种键）。 */
const DOMESTIC_EXCHANGES: ReadonlySet<string> = new Set(["SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX"]);
export const isForeignProductKey = (product: string): boolean => !DOMESTIC_EXCHANGES.has(product.split(".")[0]?.toUpperCase() ?? "");
/** A stored list → valid keys only, first occurrence wins, newest last, capped at 50. */
export function normalizeWatchlist(value: unknown): string[] {
  const items = Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && WATCHLIST_PRODUCT.test(item))
    : [];
  return [...new Set(items)].slice(0, WATCHLIST_MAX);
}
/** A catalog instrument → the watchlist key of its product ("SHFE.rb"); a 海外主连 (exchangeId KQD, productId
 *  “COMEX.GC”) → its foreign product key “COMEX.GC”. */
export function watchlistKey(item: { exchangeId: string; productId: string }): string {
  return item.exchangeId === "KQD" ? item.productId : `${item.exchangeId}.${item.productId}`;
}
/** The contract a starred product opens and lists: its main contract, or for a foreign product its 海外主连
 *  (the catalog has one 快期外盘主连 per product and does not mark it main). */
export function watchlistMainSymbol(items: readonly { exchangeId: string; productId: string; symbol: string; isMain: boolean }[],
  product: string): string | undefined {
  return items.find(item => watchlistKey(item) === product && (item.isMain || item.exchangeId === "KQD"))?.symbol;
}

/** 报价总表列可自定义 (2026-10-02): the selectable columns, in their canonical order. 品种/合约
 *  are fixed first columns and never appear in the list. `sortKey` marks the four a header click
 *  (and the boardSort command) may sort by; the other headers are plain labels. */
export type BoardColumn = "last" | "change" | "changePercent" | "volume" | "openInterest" | "openInterestChange"
  | "high" | "low" | "preSettlement" | "upperLimit" | "lowerLimit" | "quoteTime";
export const BOARD_COLUMN_TYPES: BoardColumn[] = ["last", "change", "changePercent", "volume", "openInterest",
  "openInterestChange", "high", "low", "preSettlement", "upperLimit", "lowerLimit", "quoteTime"];
export const BOARD_COLUMN_LABELS: Record<BoardColumn, string> = {
  last: "最新", change: "涨跌", changePercent: "涨跌幅", volume: "成交量", openInterest: "持仓量",
  openInterestChange: "增仓", high: "最高", low: "最低", preSettlement: "昨结",
  upperLimit: "涨停", lowerLimit: "跌停", quoteTime: "更新时间",
};
export const BOARD_COLUMNS_MAX = 12;
/** The eight columns the fixed-era board always showed (the account default). */
export const DEFAULT_BOARD_COLUMNS: BoardColumn[] = ["last", "change", "changePercent", "volume",
  "openInterest", "openInterestChange", "high", "low"];
/** A stored column list → the display order: unknown ids dropped, duplicates once, capped at 12;
 *  an unusable document falls back to the default eight (an older Runtime sent no marketBoard). */
export function normalizeBoardColumns(value: unknown): BoardColumn[] {
  const ids = Array.isArray(value)
    ? value.filter((id): id is BoardColumn => typeof id === "string" && (BOARD_COLUMN_TYPES as string[]).includes(id))
    : [];
  const unique = [...new Set(ids)].slice(0, BOARD_COLUMNS_MAX);
  return unique.length ? unique : [...DEFAULT_BOARD_COLUMNS];
}
/** A stored sort → one of the four sortable keys with a direction (an unknown key falls back to the default). */
export function normalizeBoardSort(value: unknown): BoardSort {
  if (value && typeof value === "object") {
    const { key, order } = value as { key?: unknown; order?: unknown };
    const known = BOARD_SORT_KEYS.has(key as BoardSortKey);
    return { key: known ? key as BoardSortKey : DEFAULT_BOARD_SORT.key,
             order: order === "asc" ? "asc" : "desc" };
  }
  return { ...DEFAULT_BOARD_SORT };
}

/** 成交量/持仓量 in table cells: ≥1万 as “12.3万”, small counts plain. */
export function formatVolume(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (Math.abs(value) >= 10_000) return `${(value / 10_000).toFixed(1)}万`;
  return formatNumber(value, 0);
}

/** A board price without a tick to derive decimals from: whole numbers stay whole. */
export function formatBoardPrice(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return Number.isInteger(value) ? formatNumber(value, 0) : formatNumber(value, 2);
}

/** The board's 更新时间 column: the quote timestamp's HH:mm:ss part (the feed sends ISO strings). */
export function formatQuoteTime(value: string | null | undefined): string {
  if (!value) return "—";
  return value.slice(11, 19) || value;
}

/** A board row's price cell: the last trade, or — before the first trade of the trading day
 *  (盘前, holidays, 当日无成交) — 昨结算 shown as a fallback the cell greys out and titles. */
export function boardPrice(row: { last: number | null; preSettlement: number | null }): { text: string; fallback: boolean } {
  if (row.last !== null && Number.isFinite(row.last)) return { text: formatBoardPrice(row.last), fallback: false };
  return { text: formatBoardPrice(row.preSettlement), fallback: row.preSettlement !== null && Number.isFinite(row.preSettlement) };
}

export const LAYOUT_STORAGE_KEY = "ai-trader-workspace-layout-v3";

/** The shared width (对话 / 下单 / 持仓) in a window narrower than 1280 px, and the width saved before it followed the window. */
export const DEFAULT_DOCK_WIDTH = 340;
/** Dock widths in px: 280 up to what leaves the chart 480 px (the CSS clamps the track the same way). */
export const MIN_DOCK_WIDTH = 280;
export const MAX_DOCK_WIDTH = 1600;
export function maxDockWidth(): number {
  return typeof window === "undefined" ? MAX_DOCK_WIDTH : Math.max(MIN_DOCK_WIDTH, Math.min(MAX_DOCK_WIDTH, window.innerWidth - 58 - 480));
}
/** 对话栏宽度随窗口 (2026-10-14): the shared panels can be dragged to half the window, still leaving the chart 480 px. */
export function maxSharedDockWidth(): number {
  return typeof window === "undefined" ? MAX_DOCK_WIDTH : Math.max(MIN_DOCK_WIDTH, Math.min(Math.round(window.innerWidth / 2), maxDockWidth()));
}
/** Until the user drags it, the shared width follows the window: a quarter of it, 380 at least and 640 at most
 *  (about 40 Chinese characters a line) — 1440 → 380, 1920 → 480, 2560 and wider → 640. Below 1280 it stays 340. */
export function defaultDockWidth(): number {
  if (typeof window === "undefined") return DEFAULT_DOCK_WIDTH;
  const width = window.innerWidth < 1280 ? DEFAULT_DOCK_WIDTH : clamp(Math.round(window.innerWidth / 4), 380, 640);
  return Math.min(width, maxSharedDockWidth());
}
/** Text-heavy reading panels (策略 2026-09-26, 新闻 2026-09-28, 辩论 2026-10-24) open at half the window and share one width. */
export const WIDE_PANELS: ReadonlySet<string> = new Set(["strategy", "news"]);
/** 策略面板 (2026-09-26): text-heavy, so it opens at half the window until the user drags it. */
export function defaultStrategyWidth(): number {
  return typeof window === "undefined" ? 720 : clamp(Math.round(window.innerWidth / 2), MIN_DOCK_WIDTH, maxDockWidth());
}
/** 策略编辑卡第二版 (2026-10-08): while a strategy is created or edited the panel takes about three quarters of the window
 *  and the chart keeps a quarter (at least EDITOR_CHART_MIN). Never saved: leaving the card restores the panel's own width. */
export const EDITOR_CHART_MIN = 300;
const TOOLS_RAIL = 58;
export function maxEditorWidth(): number {
  return typeof window === "undefined" ? MAX_DOCK_WIDTH : Math.max(MIN_DOCK_WIDTH, Math.min(MAX_DOCK_WIDTH, window.innerWidth - TOOLS_RAIL - EDITOR_CHART_MIN));
}
export function defaultEditorWidth(): number {
  return typeof window === "undefined" ? 1080 : clamp(Math.round(window.innerWidth * 0.75) - TOOLS_RAIL, MIN_DOCK_WIDTH, maxEditorWidth());
}
export const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

/** `dockWidth` / `strategyWidth` null = not set by the user, so the width follows the window. Every window used to save
 *  the shared width on first load, so a saved 340 (the old fixed default, also what 恢复默认布局 wrote) counts as unset. */
export function readSavedLayout(): { dockSide: DockSide; dockWidth: number | null; strategyWidth: number | null } {
  const fallback = {
    dockSide: "right" as DockSide,
    dockWidth: null as number | null,
    strategyWidth: null as number | null
  };
  if (typeof window === "undefined") return fallback;
  try {
    const saved = JSON.parse(window.localStorage.getItem(LAYOUT_STORAGE_KEY) ?? "null") as Partial<typeof fallback> | null;
    return {
      dockSide: saved?.dockSide === "left" ? "left" : "right",
      dockWidth: Number(saved?.dockWidth) && Number(saved!.dockWidth) !== DEFAULT_DOCK_WIDTH ? clamp(Number(saved!.dockWidth), MIN_DOCK_WIDTH, MAX_DOCK_WIDTH) : null,
      strategyWidth: Number(saved?.strategyWidth) ? clamp(Number(saved!.strategyWidth), MIN_DOCK_WIDTH, MAX_DOCK_WIDTH) : null
    };
  } catch {
    return fallback;
  }
}

export function readChartSettings(): TradingChartSettings {
  const fallback: TradingChartSettings = { theme: "dark", volume: true, crosshair: true, lastPriceLine: false };
  if (typeof window === "undefined") return fallback;
  try {
    const saved = JSON.parse(window.localStorage.getItem(CHART_SETTINGS_STORAGE_KEY) ?? "null") as Partial<TradingChartSettings> & {
      /** The pre-configuration shape (kd / macd / maVisible booleans and per-named-pane sizes). */
      kd?: boolean; macd?: boolean; maVisible?: Record<string, boolean>;
      paneSizes?: { main?: number; panes?: number[]; kd?: number; macd?: number };
    } | null;
    return {
      theme: saved?.theme === "light" || saved?.theme === "system" ? saved.theme : "dark",
      volume: saved?.volume !== false,
      crosshair: saved?.crosshair !== false,
      lastPriceLine: saved?.lastPriceLine === true,
      paneSizes: saved?.paneSizes ? {
        main: clamp(Number(saved.paneSizes.main) || 58, 34, 76),
        panes: Array.isArray(saved.paneSizes.panes)
          ? saved.paneSizes.panes.map(share => clamp(Math.round(Number(share) || 19), 12, 36)).slice(0, 3)
          // The stored sizes predate the configurable pane list: they name kd / macd.
          : [clamp(Number(saved.paneSizes.kd) || 19, 12, 36), clamp(Number(saved.paneSizes.macd) || 23, 12, 36)]
      } : undefined
    };
  } catch {
    return fallback;
  }
}

/**
 * 均线开关的旧本机存储 → 账号的 chart.maLines (2026-10-02), once: when the account document has no
 * `maLines` yet, the locally stored `maVisible` switches become the account's lines; the local key
 * is then dropped so it is never read again. Null when there is nothing to migrate (no stored
 * switches, or they equal the default five-on, or storage is unavailable).
 */
export function consumeLegacyMaVisible(): Array<{ period: number; enabled: boolean }> | null {
  if (typeof window === "undefined") return null;
  try {
    const saved = JSON.parse(window.localStorage.getItem(CHART_SETTINGS_STORAGE_KEY) ?? "null") as { maVisible?: Record<string, boolean> } | null;
    const maVisible = saved?.maVisible;
    if (!maVisible || typeof maVisible !== "object") return null;
    const lines = DEFAULT_MA_PERIODS.map(period => ({ period, enabled: maVisible[String(period)] !== false }));
    delete saved.maVisible;
    window.localStorage.setItem(CHART_SETTINGS_STORAGE_KEY, JSON.stringify(saved));
    return lines.some(line => !line.enabled) ? lines : null;
  } catch {
    return null;
  }
}

export interface ManualSimulationRequest {
  side: "buy_open" | "sell_open" | "buy_close" | "sell_close";
  volume: number;
  priceIntent: "passive" | "aggressive" | "last";
  limitPrice?: number;
  /** Only a 平今 / 平昨 the user picked; absent = the Runtime allocates (never computed here). */
  positionBucket?: "today" | "historical";
  /** A close of the chosen position: its own contract (a foreign month on its product's 外盘主连 chart, so the
   *  Runtime does not ask which month). Absent: the chart's contract. */
  symbol?: string;
  source: "three-key" | "context-menu";
  /** 开仓顺带保护 (2026-10-02): the user's own stop / take riding an open order — at least one
   *  key set; the Runtime validates it against the 委托价 and arms it on the actual fill. */
  protection?: { stopLossPrice?: number; takeProfitPrice?: number };
  /** 打法本 (2026-10-25): the setup an open order is placed under; the trade review carries it. */
  playbookId?: string;
}

export interface SimulationLimitSettings {
  enabled: boolean;
  maxOrderVolume: number;
  maxOrders: number;
}

/** The K 线 period buttons, in display order. 30 分 and 周线 joined 2026-10-02 (TqSdk serves both
 * directly); 分时 is a separate view of the same 1-minute data, not a duration — it renders as its
 * own button at the head of the row (MarketCommand). */
export const periods: Array<{ value: MarketWorkbenchSnapshot["durationSeconds"]; label: string }> = [
  { value: 60, label: "1分" },
  { value: 180, label: "3分" },
  { value: 300, label: "5分" },
  { value: 900, label: "15分" },
  { value: 1800, label: "30分" },
  { value: 3600, label: "1小时" },
  { value: 86400, label: "日线" },
  { value: 604800, label: "周线" }
];

export function systemPrefersDark(): boolean {
  return typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export interface TicketDraft {
  id: number;
  side: "buy_open" | "sell_open";
  volume: number;
  limitPrice?: number;
  stopPrice?: number;
  source: "context-menu";
}

export function decimalsForTick(tick: number | null | undefined): number {
  if (!tick || !Number.isFinite(tick)) return 2;
  return tickDecimals(tick);
}

export function formatNumber(value: number | null | undefined, decimals = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString("zh-CN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  });
}

/** A fee in 元: small amounts keep up to 2 decimals (螺纹 0.31 must not read “约 0”). */
export function formatFee(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: Math.abs(value) < 100 ? 2 : 0 });
}

export function formatTime(value: string | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value.slice(11, 19) || value;
  return parsed.toLocaleTimeString("zh-CN", { hour12: false });
}

/** How old a quote is, for reading (#712 C2): 3 秒前 / 12 分钟前 / 24.7 小时前 / 3 天前, never 88813.7 秒. */
export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)} 秒前`;
  if (seconds < 3_600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86_400) return `${(seconds / 3_600).toFixed(1)} 小时前`;
  return `${Math.floor(seconds / 86_400)} 天前`;
}

export function positionKey(position: AccountPosition): string {
  return `${position.symbol}:${position.direction}`;
}

export function positionPromptContext(position: AccountPosition) {
  return {
    symbol: position.symbol,
    direction: position.direction,
    volume: position.volume,
    todayVolume: position.todayVolume ?? 0,
    historicalVolume: position.historicalVolume ?? 0,
    frozenToday: position.frozenToday ?? 0,
    frozenHistorical: position.frozenHistorical ?? 0,
    openPrice: position.openPrice ?? undefined,
    margin: position.margin ?? undefined,
    unrealizedPnl: position.unrealizedPnl
  };
}

/** An order's status as the user reads it: an order that ended after a partial fill says how many
 *  lots filled and whether a cancel or the trading day ended the rest (#932). */
export function orderStatusText(order: { displayStatus: string; filledVolume?: number; cancelPhase?: string }): string {
  if (order.displayStatus !== "部分成交后已结束") return order.displayStatus;
  return `部分成交 ${order.filledVolume ?? 0} 手后${order.cancelPhase === "CONFIRMED" ? "已撤" : "已结束"}`;
}

export function orderActionLabel(order: SimulationOrder): string {
  const action = order.request.offset === "OPEN"
    ? order.request.direction === "BUY" ? "买多" : "卖空"
    : order.request.direction === "BUY" ? "买平" : "卖平";
  const offset = order.request.offset === "CLOSETODAY"
    ? "平今"
    : order.request.offset === "CLOSE" ? "平仓" : "开仓";
  return `${action} · ${offset}`;
}


/**
 * Σ latest price × volume multiple × lots over the account's positions. The
 * latest price is recovered from the position's own open price and unrealized
 * P&L (last = open ± pnl / (multiple × lots)), so no extra quote read is needed.
 * Undefined when any position lacks its multiple or open price.
 */
/** 外盘仓合并显示 (2026-10-21)：当前账户自己的持仓——权益、浮动、市值这些人民币汇总只算它们，并进来的美元子账户行另算。 */
export function ownPositions(positions: AccountPosition[]): AccountPosition[] {
  return positions.filter(position => !position.accountId);
}

export function positionsMarketValue(positions: AccountPosition[], instruments: InstrumentCatalog | undefined): number | undefined {
  let total = 0;
  for (const position of positions) {
    const multiple = instruments?.items.find(item => item.symbol.toUpperCase() === position.symbol.toUpperCase())?.volumeMultiple;
    if (!multiple || position.openPrice === null || position.openPrice === undefined || !Number.isFinite(position.openPrice)) return undefined;
    const notional = position.openPrice * multiple * position.volume;
    total += position.direction === "long" ? notional + position.unrealizedPnl : notional - position.unrealizedPnl;
  }
  return total;
}

/** "+1,650" / "-320" for signed money on the overview line. */
export function formatSigned(value: number, decimals = 0): string {
  return `${value > 0 ? "+" : ""}${formatNumber(value, decimals)}`;
}

/**
 * The one sign-colour rule for money values: >0 red (positive), <0 green (negative),
 * 0 or no value neutral (no class, --ink). Chat cards pass their own pair
 * ("positive-text" / "danger-text"); the class names carry the theme tokens.
 */
export function signClass(value: number | null | undefined, positive = "positive", negative = "negative"): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) return "";
  return value > 0 ? positive : negative;
}

/** ai_stop_orders statuses that still stand. The server's own set for "this position is
 *  already protected" (autonomy.py) is the authority; terminal states are EXECUTED,
 *  CANCELLED and FAILED. UNKNOWN stays counted — its outcome is unresolved. */
export const PROTECTION_ACTIVE_STATUSES: ReadonlySet<string> = new Set(["ARMED", "TRIGGERING", "SUBMITTED", "PARTIAL", "UNKNOWN"]);
export const isProtectionActive = (status: unknown): boolean => PROTECTION_ACTIVE_STATUSES.has(String(status));

/**
 * A contract's Chinese label, e.g. "螺纹2701" for SHFE.rb2701: the catalog's name when the
 * contract is listed, otherwise the product's name (from any listed contract of the same
 * product, delivery digits stripped) plus this contract's digits, so an expired contract keeps
 * a readable name. Falls back to the raw code.
 */
export function contractLabel(symbol: string, instruments?: InstrumentCatalog): string {
  const items = instruments?.items ?? [];
  const exact = items.find(item => item.symbol.toUpperCase() === symbol.toUpperCase());
  if (exact?.instrumentName) return exact.instrumentName;
  const cached = instruments?.cachedNames?.[symbol.toUpperCase()];
  if (cached) return cached;
  if (exact) return exact.instrumentId || symbol;
  const code = symbol.split(".").pop() ?? symbol;
  const product = code.replace(/\d+$/, "").toLowerCase();
  const digits = code.slice(product.length);
  const sibling = items.find(item => item.productId.toLowerCase() === product && item.instrumentName);
  const name = sibling?.instrumentName.replace(/\d+$/, "");
  return name && digits ? `${name}${digits}` : symbol;
}

/** The 目标持仓 ticket's local preview of the difference (same netting as the Runtime's
 *  plan_target_position): net = 多 − 空, dual holdings netted. The card itself is planned
 *  server-side from fresh positions; this line is what the user reads before pressing the button. */
export function targetPlanPreview(
  positions: AccountPosition[],
  symbol: string,
  targetNet: number
): { currentNet: number; summary: string; unchanged: boolean } {
  const wanted = symbol.toUpperCase();
  let longVolume = 0;
  let shortVolume = 0;
  for (const position of positions) {
    if (position.symbol.toUpperCase() !== wanted) continue;
    if (position.direction === "long") longVolume += position.volume;
    if (position.direction === "short") shortVolume += position.volume;
  }
  const currentNet = longVolume - shortVolume;
  const target = Math.trunc(targetNet);
  const fmt = (net: number) => (net === 0 ? "0 手" : `${net > 0 ? "+" : ""}${net} 手`);
  const legText = (label: string, volume: number) => `${label} ${volume} 手`;
  let legs: string[];
  if (target === currentNet) {
    return { currentNet, summary: `目标 ${fmt(target)}，当前 ${fmt(currentNet)} → 已是目标持仓，不用下单`, unchanged: true };
  } else if (target === 0) {
    legs = [legText("平掉全部", longVolume + shortVolume)];
  } else if (currentNet === 0) {
    legs = [legText(target > 0 ? "买开" : "卖开", Math.abs(target))];
  } else if (currentNet > 0 === target > 0) {
    const delta = Math.abs(target) - Math.abs(currentNet);
    legs = delta > 0
      ? [legText(currentNet > 0 ? "买开" : "卖开", delta)]
      : [legText(currentNet > 0 ? "卖平" : "买平", -delta)];
  } else {
    legs = [legText("平掉全部", longVolume + shortVolume), legText(target > 0 ? "买开" : "卖开", Math.abs(target))];
  }
  return { currentNet, summary: `目标 ${fmt(target)}，当前 ${fmt(currentNet)} → ${legs.join("，再")}`, unchanged: false };
}

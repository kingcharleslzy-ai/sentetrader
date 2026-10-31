/**
 * 图表指标可配置 (2026-10-02): the pure indicator math and the normalized account
 * configuration (`preferences.chart`) every chart surface — the price pane, the sub
 * panes, the settings page and the AI's chart context — shares. Everything here is a
 * plain function over bar arrays so the unit tests pin the numbers by hand.
 */

import type { DrawingDefaults } from "../chartDrawingModel";

/** 2026-10-09 (手机看盘第二批): cci and volume (成交量 as its own pane) joined; the phone shows one of the
 *  chosen panes at a time, the computer all of them — one list for both. */
export type SubPaneType = "kd" | "macd" | "rsi" | "oi" | "cci" | "volume";
export const SUB_PANE_TYPES = ["kd", "macd", "rsi", "oi", "cci", "volume"] as const;
export const SUB_PANES_MAX = 3;
export const MA_LINES_MAX = 8;
export const MA_PERIOD_MIN = 2;
export const MA_PERIOD_MAX = 500;
/** The account default (and the pre-configuration chart): five lines, all on. */
export const DEFAULT_MA_PERIODS = [5, 10, 25, 60, 200] as const;

export interface MaLineConfig { period: number; enabled: boolean }
export interface BollConfig { enabled: boolean; period: number; width: number }
export interface KdParams { k: number; d: number; smooth: number }
export interface MacdParams { fast: number; slow: number; signal: number }
export interface RsiParams { period: number }
export interface CciParams { period: number }
/** 抛物线 SAR (2026-10-09): the acceleration starts at `step`, grows by `step` on every new extreme, capped at `max`. */
export interface SarConfig { enabled: boolean; step: number; max: number }

/** What `preferences.chart` carries once the new fields exist (all optional: an older
 * Runtime document predates them, so the normalizer fills the defaults). */
export interface ChartPreferencesLike {
  kdParams?: Partial<KdParams>;
  macdParams?: Partial<MacdParams>;
  maLines?: Array<{ period: number; enabled?: boolean }> | null;
  boll?: Partial<BollConfig> | null;
  subPaneList?: Array<SubPaneType | { type: SubPaneType }> | null;
  /** The pre-configuration switches a 1.6.12 client still writes; derived when `subPaneList` is absent. */
  subPanes?: { kd?: boolean; macd?: boolean } | null;
  rsiParams?: Partial<RsiParams> | null;
  cciParams?: Partial<CciParams> | null;
  sar?: Partial<SarConfig> | null;
  /** 手机“均线”总开关关掉时打开着的周期（再打开时恢复）。 */
  maMuted?: number[] | null;
  /** 画线第二版 (2026-10-09): 磁吸 / 全部锁定 / 全部隐藏 and each tool's default style (null keys = the tool's own look). */
  drawingMagnet?: boolean;
  drawingsLocked?: boolean;
  drawingsHidden?: boolean;
  drawingDefaults?: DrawingDefaults;
}

export interface ChartIndicatorConfig {
  maLines: MaLineConfig[];
  boll: BollConfig;
  subPaneList: SubPaneType[];
  kdParams: KdParams;
  macdParams: MacdParams;
  rsiParams: RsiParams;
  cciParams: CciParams;
  sar: SarConfig;
  maMuted: number[];
}

export const DEFAULT_CHART_INDICATORS: ChartIndicatorConfig = {
  maLines: DEFAULT_MA_PERIODS.map(period => ({ period, enabled: true })),
  boll: { enabled: false, period: 20, width: 2 },
  subPaneList: ["kd", "macd"],
  kdParams: { k: 9, d: 3, smooth: 3 },
  macdParams: { fast: 12, slow: 26, signal: 9 },
  rsiParams: { period: 14 },
  cciParams: { period: 14 },
  sar: { enabled: false, step: 0.02, max: 0.2 },
  maMuted: [],
};

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function whole(value: unknown, fallback: number): number {
  const parsed = Math.round(Number(value));
  return Number.isFinite(parsed) ? parsed : fallback;
}

/**
 * One normalized configuration out of the stored (possibly old or partial) `preferences.chart`.
 * Mirrors the Runtime's validators: periods 2–500 with no duplicates (first one wins), at most
 * 8 lines and 3 unique sub panes, and `subPaneList` derived from the legacy `subPanes` switches
 * only when the list itself is absent.
 */
export function normalizeChartIndicators(prefs: ChartPreferencesLike): ChartIndicatorConfig {
  const givenLines = Array.isArray(prefs.maLines)
    ? prefs.maLines
      .filter((line): line is { period: number; enabled?: boolean } => Number.isFinite(Number(line?.period)))
      .map(line => ({ period: clamp(whole(line.period, 5), MA_PERIOD_MIN, MA_PERIOD_MAX), enabled: line.enabled !== false }))
      .filter((line, index, list) => list.findIndex(other => other.period === line.period) === index)
      .slice(0, MA_LINES_MAX)
    : [];
  const givenPanes = Array.isArray(prefs.subPaneList)
    ? prefs.subPaneList
      .map(item => typeof item === "string" ? item : item?.type)
      .filter((type): type is SubPaneType => SUB_PANE_TYPES.includes(type as SubPaneType))
      .filter((type, index, list) => list.indexOf(type) === index)
      .slice(0, SUB_PANES_MAX)
    : prefs.subPanes
      ? (["kd", "macd"] as const).filter(type => prefs.subPanes?.[type] !== false)
      : null;
  return {
    maLines: givenLines.length ? givenLines : DEFAULT_CHART_INDICATORS.maLines,
    boll: {
      enabled: prefs.boll?.enabled === true,
      period: clamp(whole(prefs.boll?.period, 20), 5, 100),
      width: clamp(Number(prefs.boll?.width) || 2, 1, 4),
    },
    subPaneList: givenPanes ?? DEFAULT_CHART_INDICATORS.subPaneList,
    kdParams: { k: clamp(whole(prefs.kdParams?.k, 9), 2, 100), d: clamp(whole(prefs.kdParams?.d, 3), 1, 30), smooth: clamp(whole(prefs.kdParams?.smooth, 3), 1, 30) },
    macdParams: { fast: clamp(whole(prefs.macdParams?.fast, 12), 2, 100), slow: clamp(whole(prefs.macdParams?.slow, 26), 3, 200), signal: clamp(whole(prefs.macdParams?.signal, 9), 1, 100) },
    rsiParams: { period: clamp(whole(prefs.rsiParams?.period, 14), 2, 100) },
    cciParams: { period: clamp(whole(prefs.cciParams?.period, 14), 2, 100) },
    sar: {
      enabled: prefs.sar?.enabled === true,
      step: clamp(Number(prefs.sar?.step) || 0.02, 0.01, 0.1),
      max: clamp(Number(prefs.sar?.max) || 0.2, 0.1, 0.5),
    },
    maMuted: Array.isArray(prefs.maMuted) ? prefs.maMuted.filter(period => Number.isInteger(period)).slice(0, MA_LINES_MAX) : [],
  };
}

// -- pane geometry ------------------------------------------------------------------

export interface PaneSizes { main: number; panes: number[] }
/** Default sub-pane shares follow the pre-configuration layout (kd 19 / macd 23); a third pane takes 14. */
const DEFAULT_PANE_SHARES = [19, 23, 14];

/** Sizes for the panes actually shown: the stored shares carry over by position, new panes get a default. */
export function normalizePaneSizes(sizes: PaneSizes | undefined, paneCount: number): PaneSizes {
  const main = clamp(whole(sizes?.main, 58), 34, 76);
  const stored = sizes?.panes ?? [];
  return { main, panes: Array.from({ length: paneCount }, (_, index) => clamp(whole(stored[index], DEFAULT_PANE_SHARES[Math.min(index, 2)]), 12, 36)) };
}

export function paneGridRows(sizes: PaneSizes): string {
  return sizes.panes.length ? [sizes.main, ...sizes.panes].map((share, index) => `${index ? "5px " : ""}${share}fr`).join(" ") : "1fr";
}

// -- labels -------------------------------------------------------------------------

export const SUB_PANE_NAMES: Record<SubPaneType, string> = { kd: "KD", macd: "MACD", rsi: "RSI", oi: "持仓量", cci: "CCI", volume: "成交量" };

/** The pane label button's text: `KD 9,3,3` / `MACD 12,26,9` / `RSI 14` / `持仓量`. */
export function subPaneLabel(type: SubPaneType, config: ChartIndicatorConfig): string {
  if (type === "kd") return `KD ${config.kdParams.k},${config.kdParams.d},${config.kdParams.smooth}`;
  if (type === "macd") return `MACD ${config.macdParams.fast},${config.macdParams.slow},${config.macdParams.signal}`;
  if (type === "rsi") return `RSI ${config.rsiParams.period}`;
  if (type === "cci") return `CCI ${config.cciParams.period}`;
  return SUB_PANE_NAMES[type];
}

/** The fewest loaded bars a sub pane needs before its last line has a full window: the longest
 *  configured period of each stage, chained where one line smooths another (each later stage adds its
 *  period − 1). MACD: the longer of fast/slow for DIF, then the signal for DEA; KD: the RSV window,
 *  then K's and D's smoothing; RSI: its period plus the first change; CCI: its period. Volume and 持仓量
 *  read every bar. One rule for every pane (#734): with fewer bars — a guest's three daily or weekly
 *  bars — the pane draws no line and its label reads 数据不足 instead of a misleading value. */
export function subPaneMinBars(type: SubPaneType, config: ChartIndicatorConfig): number {
  const chain = (first: number, ...later: number[]) => later.reduce((total, period) => total + period - 1, first);
  if (type === "kd") return chain(config.kdParams.k, config.kdParams.d, config.kdParams.smooth);
  if (type === "macd") return chain(Math.max(config.macdParams.fast, config.macdParams.slow), config.macdParams.signal);
  if (type === "rsi") return config.rsiParams.period + 1;
  if (type === "cci") return config.cciParams.period;
  return 1;
}

export function subPaneDataShort(type: SubPaneType, config: ChartIndicatorConfig, barCount: number): boolean {
  return barCount < subPaneMinBars(type, config);
}

/** The price-pane label's indicator text: enabled MA periods, plus the BOLL band when on. */
export function maLinesLabel(config: ChartIndicatorConfig): string {
  const periods = config.maLines.filter(line => line.enabled).map(line => line.period);
  const ma = periods.length ? `均线 ${periods.join(" · ")}` : "均线未启用";
  const boll = config.boll.enabled ? ` · 布林 ${config.boll.period},${config.boll.width}` : "";
  return `${ma}${boll}${config.sar.enabled ? ` · SAR ${config.sar.step},${config.sar.max}` : ""}`;
}

// -- series (pure) ------------------------------------------------------------------

export interface IndicatorBar { time: number; open: number; high: number; low: number; close: number; volume: number; openInterest: number }
export interface SeriesPoint { time: number; value: number }

/** Simple moving average; the first `period - 1` bars hold no value. */
export function movingAverage(bars: Array<Pick<IndicatorBar, "time" | "close">>, period: number): SeriesPoint[] {
  const window = Math.max(1, Math.round(period));
  let sum = 0;
  return bars.flatMap((bar, index) => {
    sum += bar.close;
    if (index >= window) sum -= bars[index - window].close;
    return index >= window - 1 ? [{ time: bar.time, value: sum / window }] : [];
  });
}

export function emaSeries(values: number[], period: number): number[] {
  const factor = 2 / (Math.max(1, Math.round(period)) + 1);
  let current = values[0] ?? 0;
  return values.map((value, index) => {
    current = index ? value * factor + current * (1 - factor) : value;
    return current;
  });
}

/** BOLL(period, width): mid = SMA, bands = mid ± width × population standard deviation. */
export function bollingerBands(bars: Array<Pick<IndicatorBar, "time" | "close">>, period: number, width: number): { mid: SeriesPoint[]; top: SeriesPoint[]; bottom: SeriesPoint[] } {
  const window = Math.max(2, Math.round(period));
  const multiplier = Math.max(0, width);
  let sum = 0;
  let squares = 0;
  const mid: SeriesPoint[] = [];
  const top: SeriesPoint[] = [];
  const bottom: SeriesPoint[] = [];
  bars.forEach((bar, index) => {
    sum += bar.close;
    squares += bar.close * bar.close;
    if (index >= window) {
      sum -= bars[index - window].close;
      squares -= bars[index - window].close * bars[index - window].close;
    }
    if (index < window - 1) return;
    const mean = sum / window;
    const deviation = Math.sqrt(Math.max(squares / window - mean * mean, 0)) * multiplier;
    mid.push({ time: bar.time, value: mean });
    top.push({ time: bar.time, value: mean + deviation });
    bottom.push({ time: bar.time, value: mean - deviation });
  });
  return { mid, top, bottom };
}

/** RSI in Wilder's smoothing: the first value lands on bar `period`, gains and losses averaged over
 * the first `period` changes and then smoothed with the 1/period factor. */
export function rsiSeries(bars: Array<Pick<IndicatorBar, "time" | "close">>, period: number): SeriesPoint[] {
  const window = Math.max(2, Math.round(period));
  let averageGain: number | null = null;
  let averageLoss: number | null = null;
  const points: SeriesPoint[] = [];
  const push = (bar: { time: number }, gain: number, loss: number) => {
    points.push({ time: bar.time, value: loss === 0 ? 100 : 100 - 100 / (1 + gain / loss) });
  };
  for (let index = 1; index < bars.length; index += 1) {
    const change = bars[index].close - bars[index - 1].close;
    const gain = Math.max(change, 0);
    const loss = Math.max(-change, 0);
    if (index <= window) {
      averageGain = (averageGain ?? 0) + gain / window;
      averageLoss = (averageLoss ?? 0) + loss / window;
      if (index === window) push(bars[index], averageGain, averageLoss);
    } else {
      averageGain = (averageGain! * (window - 1) + gain) / window;
      averageLoss = (averageLoss! * (window - 1) + loss) / window;
      push(bars[index], averageGain, averageLoss);
    }
  }
  return points;
}

/** KD(n, m1, m2): RSV over `k` bars, K = SMA(RSV, m1) and D = SMA(K, m2) in the Chinese-software sense (weights 1/m). */
export function kdSeries(bars: IndicatorBar[], params: KdParams): Array<{ time: number; k: number; d: number }> {
  const window = Math.max(1, Math.round(params.k));
  const m1 = Math.max(1, Math.round(params.d));
  const m2 = Math.max(1, Math.round(params.smooth));
  let k = 50;
  let d = 50;
  return bars.map((bar, index) => {
    const sample = bars.slice(Math.max(0, index - window + 1), index + 1);
    const high = Math.max(...sample.map(item => item.high));
    const low = Math.min(...sample.map(item => item.low));
    const rsv = high === low ? 50 : ((bar.close - low) / (high - low)) * 100;
    k = ((m1 - 1) * k + rsv) / m1;
    d = ((m2 - 1) * d + k) / m2;
    return { time: bar.time, k, d };
  });
}

export function macdSeries(bars: IndicatorBar[], params: MacdParams): Array<{ time: number; diff: number; dea: number; histogram: number }> {
  const closes = bars.map(bar => bar.close);
  const fast = emaSeries(closes, params.fast);
  const slow = emaSeries(closes, params.slow);
  const diff = fast.map((value, index) => value - slow[index]);
  const dea = emaSeries(diff, params.signal);
  return bars.map((bar, index) => ({ time: bar.time, diff: diff[index], dea: dea[index], histogram: (diff[index] - dea[index]) * 2 }));
}

/** CCI(n) = (TP − SMA(TP, n)) / (0.015 × mean deviation), TP = (high + low + close) / 3; the first value
 *  lands on bar n − 1. A flat window (no deviation) reads 0. */
export function cciSeries(bars: Array<Pick<IndicatorBar, "time" | "high" | "low" | "close">>, period: number): SeriesPoint[] {
  const window = Math.max(2, Math.round(period));
  const typical = bars.map(bar => (bar.high + bar.low + bar.close) / 3);
  const points: SeriesPoint[] = [];
  for (let index = window - 1; index < bars.length; index += 1) {
    // Offsets from the window's first value (#998): summing identical decimals (4123.2 × 14) leaves a rounding
    // residue that the deviation then divides into ±66.7; the offsets of a flat window are exactly 0.
    const base = typical[index - window + 1];
    const sample = typical.slice(index - window + 1, index + 1).map(value => value - base);
    const mean = sample.reduce((sum, value) => sum + value, 0) / window;
    const deviation = sample.reduce((sum, value) => sum + Math.abs(value - mean), 0) / window;
    points.push({ time: bars[index].time, value: deviation === 0 ? 0 : (typical[index] - base - mean) / (0.015 * deviation) });
  }
  return points;
}

/** Parabolic SAR (Wilder): starts long on bar 1 (short when it opens below bar 0's close), the extreme
 *  point and the acceleration reset on every reversal, and the stop never enters the last two bars. */
export function sarSeries(bars: Array<Pick<IndicatorBar, "time" | "high" | "low" | "close">>, step: number, max: number): SeriesPoint[] {
  if (bars.length < 2) return [];
  let long = bars[1].close >= bars[0].close;
  let sar = long ? bars[0].low : bars[0].high;
  let extreme = long ? bars[1].high : bars[1].low;
  let factor = step;
  const points: SeriesPoint[] = [{ time: bars[1].time, value: sar }];
  for (let index = 2; index < bars.length; index += 1) {
    const bar = bars[index];
    let next = sar + factor * (extreme - sar);
    // The stop may not sit inside the previous two bars' range.
    next = long ? Math.min(next, bars[index - 1].low, bars[index - 2].low) : Math.max(next, bars[index - 1].high, bars[index - 2].high);
    if (long ? bar.low < next : bar.high > next) {
      long = !long;
      next = extreme;
      extreme = long ? bar.high : bar.low;
      factor = step;
    } else if (long ? bar.high > extreme : bar.low < extreme) {
      extreme = long ? bar.high : bar.low;
      factor = Math.min(max, factor + step);
    }
    sar = next;
    points.push({ time: bar.time, value: sar });
  }
  return points;
}

/** The 持仓量 pane draws every bar's open interest (手). */
export function openInterestSeries(bars: IndicatorBar[]): SeriesPoint[] {
  return bars.map(bar => ({ time: bar.time, value: bar.openInterest }));
}

// -- colors -------------------------------------------------------------------------

/** Eight fixed line colors by position in `maLines`; the first five are the historical per-period colors. */
export const MA_LINE_COLORS: Record<"light" | "dark", readonly string[]> = {
  dark: ["#f5f7fa", "#f0c84b", "rgba(49,185,123,.60)", "rgba(92,141,255,.60)", "rgba(239,89,100,.65)", "#c9a0ff", "#7fd6d0", "#e8b7d4"],
  light: ["#2c333a", "#c99800", "rgba(22,136,91,.58)", "rgba(53,109,221,.58)", "rgba(216,61,73,.62)", "#8a4fd3", "#0f8a84", "#b0487f"],
};
/** One hue for the three BOLL lines (mid solid, bands dashed). */
export const BOLL_LINE_COLOR: Record<"light" | "dark", string> = { dark: "#a78bfa", light: "#7c5cd6" };
export const RSI_LINE_COLOR: Record<"light" | "dark", string> = { dark: "#5fd4c0", light: "#0f8a84" };
export const OI_LINE_COLOR: Record<"light" | "dark", string> = { dark: "#8fb3c9", light: "#4a7a96" };
export const CCI_LINE_COLOR: Record<"light" | "dark", string> = { dark: "#e8a36b", light: "#b5652a" };
export const SAR_POINT_COLOR: Record<"light" | "dark", string> = { dark: "#d8c37a", light: "#8a7420" };

import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  LineSeries,
  TickMarkType,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LogicalRange,
  type Time,
  type UTCTimestamp
} from "lightweight-charts";
import { Fragment, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { MarketKlineBar, RemoteTradeSnapshot } from "../api";
import type { ChartExtension } from "./AiActivity";
import { openAppHelp } from "./AppHelp";
import type { BacktestMark, ChartAnnotation } from "../aiAnnotations";
import { fillVisibleAfterClear } from "../aiAnnotations";
import { ChartAnnotationsPrimitive, annotationTone, type NumberedAnnotation, type ToneColors } from "./chartAnnotationsPrimitive";
import { UserDrawingsPrimitive, drawingPalette } from "./chartUserDrawings";
import { DrawingInteraction, type DrawingInteractionHooks, type HandleDrag } from "./chartDrawingInteraction";
import { magnetPrice, snapToTick, type Anchor, type ChartDrawing, type DrawingKind } from "../chartDrawingModel";
import { AddPaneChooser, ChartPopover, MaLinesEditor, SubPaneEditor, type ChartIndicatorPatch } from "./chartIndicatorEditor";
import { useDisclosureSurface } from "../uiSurfaces";
import { rumChartPainted } from "../rum";
import { refitWhenWidened } from "./chartRefit";
import { contractPriceFormat, contractTickmarks, formatCciPrice, formatIntegerPrice, formatMacdPrice } from "./chartPriceFormat";
import {
  BOLL_LINE_COLOR, CCI_LINE_COLOR, MA_LINE_COLORS, OI_LINE_COLOR, RSI_LINE_COLOR, SAR_POINT_COLOR, SUB_PANE_NAMES, SUB_PANES_MAX,
  bollingerBands, cciSeries, emaSeries, kdSeries, macdSeries, maLinesLabel, movingAverage, normalizeChartIndicators, normalizePaneSizes,
  openInterestSeries, paneGridRows, rsiSeries, sarSeries, subPaneDataShort, subPaneLabel, subPaneMinBars,
  type ChartIndicatorConfig, type ChartPreferencesLike, type PaneSizes, type SubPaneType
} from "./chartIndicators";
import { decimalsForTick, formatNumber, formatSigned } from "./futures/support";
import "./TradingCharts.css";

/** Account chart preferences (设置 → 图表, `preferences.chart`) the chart draws with. The indicator
 * fields (均线 / 布林带 / 副图清单) are optional: an older Runtime document predates them and the
 * normalizer fills the defaults (图表指标可配置, 2026-10-02). */
export interface ChartPreferences extends ChartPreferencesLike {
  candleStyle: "candle" | "ohlc";
  upDownColors: "cn" | "intl";
  kdParams: { k: number; d: number; smooth: number };
  macdParams: { fast: number; slow: number; signal: number };
}
const NO_BACKTEST_MARKS: BacktestMark[] = [];
/** 到价提醒: the stable empty default keeps a re-render without alerts from touching the chart. */
const NO_ALERT_LINES: Array<{ id: string; price: number }> = [];
export type ProtectionLine = { id: string; kind: "stop" | "take"; price: number;
  /** The user's own armed protection: grabbed and dragged on the chart (图上拖线改价). */
  draggable?: boolean;
  /** A dragged level awaiting its confirm card: drawn dashed beside the line that still holds. */
  pending?: boolean };
const NO_PROTECTION_LINES: ProtectionLine[] = [];
/** 用户画线: the stable empty default keeps a re-render without drawings from touching the chart. */
const NO_USER_DRAWINGS: ChartDrawing[] = [];
export const DEFAULT_CHART_PREFERENCES: ChartPreferences = { candleStyle: "candle", upDownColors: "cn", kdParams: { k: 9, d: 3, smooth: 3 }, macdParams: { fast: 12, slow: 26, signal: 9 } };

export interface TradingChartSettings {
  theme: "light" | "dark" | "system";
  volume: boolean;
  crosshair: boolean;
  lastPriceLine?: boolean;
  paneSizes?: PaneSizes;
}

/** A user drawing hit by a right-click: the drawing menu acts on it (画线第二版: the whole drawing). */
export type ContextDrawing = ChartDrawing;

interface TradingChartsProps {
  helpAvailable?: boolean;
  onHelp?: () => void;
  historyRestricted?: boolean;
  onHistoryRestricted?: () => void;
  /** The basis of a shortened history (a guest's 最近 N 个交易日), shown under the price label (#734). */
  historyNote?: string;
  onTradeSelect?: (ids: string[]) => void;
  selectedTradeIds?: string[];
  onFocusResult?: (message: string) => void;
  tradeFills?: RemoteTradeSnapshot[];
  extensions?: ChartExtension[];
  durationSeconds?: number;
  tradeFocus?: { openTime: number; closeTime: number; direction: string };
  /** The AI's drawings for the displayed contract (a layer of its own above the user's candidate line). */
  annotations?: ChartAnnotation[];
  /** 规则回测的开平仓点 for the displayed contract and period (the card's 在图上标出买卖点). */
  backtestMarks?: BacktestMark[];
  bars: MarketKlineBar[];
  settings: TradingChartSettings;
  preferences?: ChartPreferences;
  resolvedTheme?: "light" | "dark";
  timeZone?: string;
  selectedPrice?: number;
  priceTick?: number | null;
  /** 到价提醒 (2026-10-02): the displayed contract's active alerts — one dashed price line each
   *  (axis label 提醒 价位). A crossing re-lists without the fired alert, so its line drops by itself. */
  alertLines?: Array<{ id: string; price: number }>;
  /** 下单顺带保护 (2026-10-02): this contract's armed stop / take — one thin dotted line each
   *  (axis label 止损/止盈 价位), orange / cyan, apart from the alerts' dashed violet. */
  protectionLines?: ProtectionLine[];
  /** 图上拖线改价 (2026-10-24): a draggable protection line followed to `price`; `done` on the lift (`price` null: cancelled). */
  onProtectionDrag?: (id: string, price: number | null, done: boolean) => void;
  /** The displayed contract: 成交标记 retired by 清空图上标记 stay retired (per-contract stamp). */
  symbol?: string;
  onPriceContext?: (price: number, x: number, y: number, drawing?: ContextDrawing) => void;
  /** 画线第二版 (2026-10-09): a right-click on a sub pane opens that pane's own menu. */
  onPaneContext?: (type: SubPaneType, x: number, y: number) => void;
  /** 用户画线 (2026-10-02): this contract's own server-stored drawings that show on this period —
   *  a layer apart from every AI layer (the annotations' own count and 清空图上标记 never touch it). */
  userDrawings?: ChartDrawing[];
  /** The armed tool: the chart's clicks place it (画线第二版: twenty kinds, see chartDrawingModel). */
  drawingTool?: DrawingKind | null;
  selectedDrawingId?: string | null;
  /** 全部锁定 and 磁吸 (the toolbar's switches). */
  drawingsLocked?: boolean;
  drawingMagnet?: boolean;
  onPlaceDrawing?: (kind: DrawingKind, anchors: Anchor[]) => void;
  onMoveDrawing?: (drawing: ChartDrawing, anchors: Anchor[]) => void;
  onDrawingSelect?: (id: string | null, at?: { clientX: number; clientY: number }, handle?: number | null) => void;
  /** 手机画线: the selected drawing's chosen anchor (drawn filled), a finger dragging one anchor (the loupe), the
   *  placement's point count, and a counter whose every change ends a 折线 in progress (the phone's 完成). */
  selectedDrawingHandle?: number | null;
  onDrawingHandleDrag?: (drag: HandleDrag | null) => void;
  onDrawingDraft?: (count: number) => void;
  drawingFinish?: number;
  onSettingsChange: (settings: TradingChartSettings) => void;
  /** 图表指标就地编辑 (2026-10-02): the label popovers' edits, saved through the account
   * preference path by the workspace (guests keep theirs local). Absent = read-only chart. */
  onIndicatorsChange?: (patch: ChartIndicatorPatch) => void;
  /** The preference save path's inline status (已保存 / 越界原因), shown inside the popovers. */
  indicatorStatus?: string;
}

type ChartBar = { time: UTCTimestamp; open: number; high: number; low: number; close: number; volume: number; openInterest: number };
type CandlestickApi = ISeriesApi<"Candlestick">;
type HistogramApi = ISeriesApi<"Histogram">;
type LineApi = ISeriesApi<"Line">;

type MainChartBundle = {
  chart: IChartApi;
  candles: CandlestickApi;
  volume: HistogramApi;
  /** One line series per configured period; hidden lines stay created with `visible: false`. */
  maLines: Map<number, LineApi>;
  bollMid?: LineApi;
  bollTop?: LineApi;
  bollBottom?: LineApi;
  /** 抛物线 SAR (2026-10-09): point markers only, created while it is on. */
  sar?: LineApi;
  selectedLine?: IPriceLine;
  annotations: ChartAnnotationsPrimitive;
  annotationLines: IPriceLine[];
  /** 到价提醒: the displayed contract's active alerts as dashed price lines. */
  alertLines: IPriceLine[];
  /** 下单顺带保护: this contract's armed stop / take as dotted price lines, each with the item it draws. */
  protectionLines: Array<{ line: IPriceLine; item: ProtectionLine }>;
  /** 用户画线: the user's own lines, drawn and hit-tested in one place. */
  userDrawings: UserDrawingsPrimitive;
};

/** Every pane carries the AI annotation primitive and the price lines it draws its hlines with. */
type AnnotatedPane = { annotations: ChartAnnotationsPrimitive; annotationLines: IPriceLine[] };
type SubPaneBundle = AnnotatedPane & {
  type: SubPaneType;
  chart: IChartApi;
  /** The series hlines attach to (and the primitive rides on). */
  lineSeries?: LineApi;
  k?: LineApi;
  d?: LineApi;
  histogram?: HistogramApi;
  diff?: LineApi;
  dea?: LineApi;
  rsi?: LineApi;
  oi?: LineApi;
  cci?: LineApi;
  vol?: HistogramApi;
};

/** Which popover the chart labels have open (one at a time), anchored to its label button. */
type OpenPopover = { kind: "ma" } | { kind: "add" } | { kind: "pane"; type: SubPaneType };

/** The pure indicator module keeps plain numbers; the chart library wants branded timestamps. */
function lineData(points: Array<{ time: number; value: number }>) {
  return points.map((point) => ({ time: point.time as UTCTimestamp, value: point.value }));
}

/** A sub-pane series laid on every loaded bar: bars before the indicator's first value become
 *  whitespace points. Panes are separate charts kept together by logical (bar-index) range, so a
 *  series that starts late (RSI's first `period` bars) would otherwise shift left of the price pane. */
function onEveryBar(chartBars: ChartBar[], points: Array<{ time: UTCTimestamp; value: number }>) {
  const byTime = new Map(points.map((point) => [point.time, point]));
  return chartBars.map((bar) => byTime.get(bar.time) ?? { time: bar.time });
}

/** Every pane's indicator series for the loaded bars, in the chart library's point shape. A pane with
 *  fewer bars than its look-back (subPaneDataShort) gets no values: its label reads 数据不足 (#734). */
function buildSeriesData(chartBars: ChartBar[], indicators: ChartIndicatorConfig) {
  const short = (type: SubPaneType) => subPaneDataShort(type, indicators, chartBars.length);
  return {
    kd: short("kd") ? [] : kdSeries(chartBars, indicators.kdParams),
    macd: short("macd") ? [] : macdSeries(chartBars, indicators.macdParams),
    rsi: onEveryBar(chartBars, short("rsi") ? [] : lineData(rsiSeries(chartBars, indicators.rsiParams.period))),
    oi: lineData(openInterestSeries(chartBars)),
    cci: onEveryBar(chartBars, short("cci") ? [] : lineData(cciSeries(chartBars, indicators.cciParams.period))),
    sar: indicators.sar.enabled ? lineData(sarSeries(chartBars, indicators.sar.step, indicators.sar.max)) : [],
    boll: (({ mid, top, bottom }) => ({ mid: lineData(mid), top: lineData(top), bottom: lineData(bottom) }))(
      bollingerBands(chartBars, indicators.boll.period, indicators.boll.width)),
    ma: indicators.maLines.map((line) => lineData(movingAverage(chartBars, line.period)))
  };
}

/**
 * Candle colors follow the document's market-direction tokens, so 涨跌颜色 (`html[data-updown]`) and the
 * theme change the chart and the quote panels together. OHLC bars keep the body hollow.
 */
function candleSeriesOptions(style: ChartPreferences["candleStyle"]) {
  const tokens = typeof getComputedStyle === "function" ? getComputedStyle(document.documentElement) : undefined;
  const up = tokens?.getPropertyValue("--market-up").trim() || "#b82e3a";
  const down = tokens?.getPropertyValue("--market-down").trim() || "#087451";
  const hollow = style === "ohlc";
  return { upColor: hollow ? "transparent" : up, downColor: hollow ? "transparent" : down, borderVisible: hollow, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down };
}

function withAlpha(color: string, alpha: number): string {
  const hex = /^#([0-9a-f]{6})$/i.exec(color.trim())?.[1];
  if (!hex) return color;
  const value = parseInt(hex, 16);
  return `rgba(${value >> 16},${(value >> 8) & 255},${value & 255},${alpha})`;
}

/** Rising / falling colours at an alpha, read once per data update (volume .28, MACD bars .62). */
function directionColors(alpha: number) {
  const { borderUpColor, borderDownColor } = candleSeriesOptions("candle");
  return { up: withAlpha(borderUpColor, alpha), down: withAlpha(borderDownColor, alpha) };
}

function chartOptions(theme: "light" | "dark", timeVisible: boolean, indicator = false, timeZone?: string, paneType?: SubPaneType, priceTick?: number | null) {
  const dark = theme === "dark";
  const format = (time: Time, options: Intl.DateTimeFormatOptions) => typeof time === "number"
    ? new Intl.DateTimeFormat("zh-CN", { timeZone, hourCycle: "h23", ...options }).format(new Date(time * 1000))
    : null;
  const indicatorFormatter = paneType === "kd" || paneType === "rsi" ? formatIntegerPrice
    : paneType === "macd" ? formatMacdPrice : paneType === "cci" ? formatCciPrice : undefined;
  return {
    localization: {
      ...(timeZone ? { timeFormatter: (time: Time) => format(time, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) ?? String(time) } : {}),
      ...(indicatorFormatter ? { priceFormatter: indicatorFormatter } : {}),
      // The library exposes a tick-only hook; labels keep their series price format.
      ...(!indicator ? { tickmarksPriceFormatter: contractTickmarks(priceTick) }
        : indicatorFormatter ? { tickmarksPriceFormatter: (values: number[]) => values.map(indicatorFormatter) }
        : {})
    },
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid as const, color: dark ? "#0b0d10" : "#ffffff" },
      textColor: dark ? "#838882" : "#656a64",
      attributionLogo: false
    },
    grid: {
      vertLines: { visible: false },
      horzLines: { color: dark ? "rgba(148,163,184,.07)" : "rgba(15,23,42,.06)", style: 0 as const }
    },
    rightPriceScale: {
      autoScale: true,
      borderVisible: false,
      entireTextOnly: true,
      minimumWidth: 74,
      scaleMargins: indicator ? { top: 0.1, bottom: 0.12 } : { top: 0.07, bottom: 0.13 }
    },
    timeScale: {
      ...(timeZone ? { tickMarkFormatter: (time: Time, type: TickMarkType) => format(time,
        type === TickMarkType.Year ? { year: "numeric" }
          : type === TickMarkType.Month ? { month: "short" }
          : type === TickMarkType.DayOfMonth ? { month: "2-digit", day: "2-digit" }
          : { hour: "2-digit", minute: "2-digit", ...(type === TickMarkType.TimeWithSeconds ? { second: "2-digit" as const } : {}) }) } : {}),
      borderVisible: false,
      timeVisible: true,
      secondsVisible: false,
      visible: timeVisible,
      rightOffset: 4
    },
    crosshair: {
      mode: 0 as const,
      vertLine: { visible: false, labelVisible: false },
      horzLine: {
        color: dark ? "rgba(142,168,255,.55)" : "rgba(47,91,255,.45)",
        width: 1 as const,
        style: 0 as const,
        labelBackgroundColor: dark ? "#24335f" : "#2f5bff"
      }
    }
  };
}

/** Semantic tone colors from the document theme, read where the canvas cannot use CSS variables. */
function toneColors(element: Element | null): ToneColors {
  const style = element ? getComputedStyle(element) : null;
  const read = (name: string, fallback: string) => style?.getPropertyValue(name).trim() || fallback;
  return { info: read("--accent", "#3154d5"), warn: read("--warning", "#925500"), danger: read("--danger", "#b42318"), good: read("--positive", "#18794e") };
}

/** 十字光标停在哪根 K 线（所有者 2026-10-20 样稿）：那根的时间、开高低收、相对前一根收盘的涨跌、量与持仓。
 *  光标不在某根 K 线上（左右空白处）为 null。 */
export function barReadout(bars: readonly { time: number; open: number; high: number; low: number; close: number; volume: number; openInterest: number }[],
  time: number | undefined, { decimals, daily, timeZone }: { decimals: number; daily: boolean; timeZone: string }) {
  if (time === undefined || !bars.length) return null;
  let low = 0, high = bars.length - 1;
  while (low < high) { const mid = (low + high) >> 1; if (bars[mid].time < time) low = mid + 1; else high = mid; }
  const bar = bars[low];
  if (bar.time !== time) return null;
  const prev = low > 0 ? bars[low - 1].close : null;
  const change = prev !== null ? bar.close - prev : null;
  const when = new Intl.DateTimeFormat("zh-CN", { timeZone, ...(daily ? { year: "numeric" } : { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }),
    month: "2-digit", day: "2-digit" }).format(new Date(bar.time * 1000));
  const price = (value: number) => formatNumber(value, decimals);
  return { when, open: price(bar.open), high: price(bar.high), low: price(bar.low), close: price(bar.close),
    change: change === null ? null : `${formatSigned(change, decimals)}${prev ? ` (${formatSigned(change / prev * 100, 2)}%)` : ""}`,
    direction: change === null || change === 0 ? undefined : change > 0 ? "up" : "down",
    volume: formatNumber(bar.volume, 0), openInterest: bar.openInterest > 0 ? formatNumber(bar.openInterest, 0) : null };
}

export function TradingCharts({
  helpAvailable = true,
  onHelp,
  historyRestricted = false,
  onHistoryRestricted,
  historyNote,
  bars,
  settings,
  resolvedTheme = settings.theme === "light" ? "light" : "dark",
  timeZone = "Asia/Shanghai",
  selectedPrice,
  priceTick,
  symbol,
  onPriceContext,
  onSettingsChange,
  onIndicatorsChange,
  indicatorStatus,
  tradeFocus,
  onTradeSelect,
  selectedTradeIds,
  onFocusResult,
  tradeFills = [],
  extensions = [],
  annotations = [],
  backtestMarks = NO_BACKTEST_MARKS,
  alertLines = NO_ALERT_LINES,
  protectionLines = NO_PROTECTION_LINES,
  onProtectionDrag,
  userDrawings = NO_USER_DRAWINGS,
  drawingTool = null,
  selectedDrawingId,
  drawingsLocked = false,
  drawingMagnet = false,
  onPlaceDrawing,
  onMoveDrawing,
  onDrawingSelect,
  selectedDrawingHandle = null,
  onDrawingHandleDrag,
  onDrawingDraft,
  drawingFinish = 0,
  onPaneContext,
  durationSeconds = 60,
  preferences = DEFAULT_CHART_PREFERENCES
}: TradingChartsProps) {
  const chartBars = useMemo<ChartBar[]>(() => bars.map((bar) => ({
    time: Math.floor(bar.datetime / 1_000_000_000) as UTCTimestamp,
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    volume: bar.volume,
    openInterest: bar.openInterest
  })), [bars]);
  // 图表指标可配置 (2026-10-02): every pane, label and popover reads this one normalized config.
  const indicators = useMemo<ChartIndicatorConfig>(() => normalizeChartIndicators(preferences), [preferences]);
  const paneTypes = indicators.subPaneList;
  const paneKey = paneTypes.join(",");
  const editable = Boolean(onIndicatorsChange);
  const changeIndicators = (patch: ChartIndicatorPatch) => onIndicatorsChange?.(patch);
  const seriesData = useMemo(() => buildSeriesData(chartBars, indicators), [chartBars, indicators]);
  const maKey = indicators.maLines.map((line) => line.period).join(",");
  const bollKey = `${indicators.boll.enabled}`;
  const sarKey = `${indicators.sar.enabled}`;
  const latestKd = seriesData.kd.at(-1);
  const latestMacd = seriesData.macd.at(-1);
  const latestRsi = seriesData.rsi.at(-1);
  const latestOi = chartBars.at(-1)?.openInterest;
  const latestCci = seriesData.cci.at(-1);
  const observationData = useRef({ chartBars, seriesData, indicators, paneTypes, durationSeconds });
  observationData.current = { chartBars, seriesData, indicators, paneTypes, durationSeconds };
  const historyAccess = useRef({ restricted: historyRestricted, onAttempt: onHistoryRestricted });
  historyAccess.current = { restricted: historyRestricted, onAttempt: onHistoryRestricted };
  const historyGesture = useRef({ active: false, notified: false, lastWheel: 0 });
  const checkHistoryBoundary = () => {
    const range = mainChartRef.current?.chart.timeScale().getVisibleLogicalRange();
    if (historyAccess.current.restricted && range && range.from <= 0 && !historyGesture.current.notified) {
      historyGesture.current.notified = true;
      historyAccess.current.onAttempt?.();
    }
  };
  const mainRef = useRef<HTMLDivElement>(null);
  const stackRef = useRef<HTMLDivElement>(null);
  const mainChartRef = useRef<MainChartBundle | undefined>(undefined);
  const focusedTrade = useRef<string | undefined>(undefined);
  const previousTradeRange = useRef<LogicalRange | null>(null);
  /** The mounted sub panes by type (types are unique within the list). */
  const paneBundles = useRef(new Map<SubPaneType, SubPaneBundle>());
  // A sub-pane joins the shared span only after it has taken the price pane's span once: until then
  // its own default span (pane width ÷ bar spacing) is not broadcast, so a pane added or re-created
  // while the price pane is still applying its fit cannot drag every pane back (left side empty).
  const alignedPanes = useRef(new WeakSet<IChartApi>());
  const registerPane = useRef((type: SubPaneType, bundle: SubPaneBundle | undefined) => {
    if (!bundle) { paneBundles.current.delete(type); return; }
    paneBundles.current.set(type, bundle);
    // Two frames: the price pane's fitContent/scroll lands on the next frame.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const span = mainChartRef.current?.chart.timeScale().getVisibleLogicalRange();
      if (span && paneBundles.current.get(type) === bundle) {
        syncingRangeRef.current = true;
        bundle.chart.timeScale().setVisibleLogicalRange(span);
        syncingRangeRef.current = false;
      }
      alignedPanes.current.add(bundle.chart);
    }));
  }).current;
  const paneCharts = () => [...paneBundles.current.values()].map((bundle) => bundle.chart);
  const fitKeyRef = useRef("");
  const syncingRangeRef = useRef(false);
  const onPriceContextRef = useRef(onPriceContext);
  onPriceContextRef.current = onPriceContext;
  const onPaneContextRef = useRef(onPaneContext);
  onPaneContextRef.current = onPaneContext;
  // 用户画线 (画线第二版): the pointer interaction subscribes once and reads the latest tool, switches
  // and callbacks through this object, refreshed on every render.
  const drawingToolRef = useRef(drawingTool);
  drawingToolRef.current = drawingTool;
  const chartBarsRef = useRef(chartBars);
  chartBarsRef.current = chartBars;
  const durationRef = useRef(durationSeconds);
  durationRef.current = durationSeconds;
  const interactionRef = useRef<DrawingInteraction | undefined>(undefined);
  const drawingHooks: DrawingInteractionHooks = {
    tool: drawingTool, locked: drawingsLocked, magnet: drawingMagnet,
    toAnchor: (x, y, magnet) => {
      const main = mainChartRef.current;
      const bars = chartBarsRef.current;
      if (!main || !bars.length) return null;
      const raw = main.candles.coordinateToPrice(y);
      const logical = main.chart.timeScale().coordinateToLogical(x);
      if (raw == null || !Number.isFinite(raw) || logical == null) return null;
      const index = Math.round(logical);
      const last = bars.length - 1;
      const bar = bars[Math.max(0, Math.min(last, index))];
      // Past either end the time keeps counting in bars, so a line can reach into the empty future.
      const t = index > last ? bars[last].time + (index - last) * durationRef.current
        : index < 0 ? bars[0].time + index * durationRef.current : bar.time;
      const price = magnet && index >= 0 && index <= last ? magnetPrice(raw, bar) : raw;
      const snapped = snapToTick(price, priceTickRef.current);
      return snapped > 0 ? { t, p: snapped } : null;
    },
    onPlace: (kind, anchors) => onPlaceDrawing?.(kind, anchors),
    onMove: (drawing, anchors) => onMoveDrawing?.(drawing, anchors),
    onSelect: (id, at, handle) => onDrawingSelect?.(id, at, handle),
    onHandleDrag: onDrawingHandleDrag,
    onDraft: count => onDrawingDraft?.(count),
    lineAt: (y, reach) => {
      const main = mainChartRef.current;
      if (!main || !onProtectionDrag) return null;
      const hit = main.protectionLines.find(({ item }) => {
        const at = item.draggable ? main.candles.priceToCoordinate(item.price) : null;
        return at != null && Math.abs(at - y) <= reach;
      });
      return hit?.item.id ?? null;
    },
    onLineDrag: (id, price, done) => {
      const drawn = mainChartRef.current?.protectionLines.find(({ item }) => item.id === id);
      // The line follows the pointer; on the lift it goes back where its protection still is (a confirm card's
      // pending level is drawn as its own dashed line until the protection really moves).
      drawn?.line.applyOptions({ price: done || price === null ? drawn.item.price : price });
      onProtectionDrag?.(id, price, done);
    },
  };
  if (interactionRef.current) interactionRef.current.hooks = drawingHooks;
  const priceTickRef = useRef(priceTick);
  const sizes = normalizePaneSizes(settings.paneSizes, paneTypes.length);
  const setSizes = (paneSizes: PaneSizes) => onSettingsChange({ ...settings, paneSizes });
  const [cursorX, setCursorX] = useState<number>();
  const [cursorTime, setCursorTime] = useState<number>();
  // Clears our synced line and the library's own crosshair on every pane.
  const clearCursor = () => {
    setCursorX(undefined); setCursorTime(undefined);
    [mainChartRef.current?.chart, ...paneCharts()]
      .forEach((chart) => { try { chart?.clearCrosshairPosition(); } catch { /* A disposed chart has nothing to clear. */ } });
  };
  // pointerleave is the normal exit, but a native window drag from the header,
  // an OS overlay (screenshot mode, Mission Control) or a lost focus can end
  // the pointer's stay without it: any pointer move outside the chart stack,
  // a cancelled pointer, the window blurring or the tab hiding clears too.
  useEffect(() => {
    const outside = (event: PointerEvent) => {
      const stack = stackRef.current;
      if (stack && !(event.target instanceof Node && stack.contains(event.target))) clearCursor();
    };
    const hidden = () => { if (document.visibilityState === "hidden") clearCursor(); };
    document.addEventListener("pointermove", outside);
    document.addEventListener("pointercancel", clearCursor);
    document.documentElement.addEventListener("mouseleave", clearCursor);
    window.addEventListener("blur", clearCursor);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      document.removeEventListener("pointermove", outside);
      document.removeEventListener("pointercancel", clearCursor);
      document.documentElement.removeEventListener("mouseleave", clearCursor);
      window.removeEventListener("blur", clearCursor);
      document.removeEventListener("visibilitychange", hidden);
    };
  }, []);
  priceTickRef.current = priceTick;

  const publishChartObservation = (source: IChartApi) => {
    if (!stackRef.current) return;
    const timeRange = source.timeScale().getVisibleRange();
    const logicalRange = source.timeScale().getVisibleLogicalRange();
    const current = observationData.current;
    const visible = logicalRange ? current.chartBars.slice(Math.max(0, Math.floor(logicalRange.from)), Math.max(0, Math.ceil(logicalRange.to) + 1)) : [];
    stackRef.current.dataset.agentChartRange = JSON.stringify(timeRange);
    stackRef.current.dataset.agentChartObservation = JSON.stringify({ timeRange, logicalRange,
      durationSeconds: current.durationSeconds, visibleBarCount: visible.length,
      firstVisibleBar: visible.at(0) ?? null, lastVisibleBar: visible.at(-1) ?? null,
      // Which panes and MA periods the user's chart shows — the AI's chart context (2026-10-02).
      panes: current.paneTypes,
      // 数据不足 (#734): panes whose look-back is longer than the loaded bars, drawn empty.
      shortPanes: current.paneTypes.filter((type) => subPaneDataShort(type, current.indicators, current.chartBars.length)),
      displayedIndicators: { kd: current.seriesData.kd.at(-1) ?? null, macd: current.seriesData.macd.at(-1) ?? null,
        rsi: (() => { const last = current.seriesData.rsi.at(-1); return last && "value" in last ? last : null; })(),
        boll: current.indicators.boll.enabled ? current.seriesData.boll.mid.at(-1) ?? null : null,
        movingAverages: Object.fromEntries(current.indicators.maLines
          .filter((line) => line.enabled)
          .map((line, index) => [line.period, current.seriesData.ma[index]?.at(-1) ?? null])) } });
  };

  useEffect(() => {
    [mainChartRef.current?.chart, ...paneCharts()]
      .forEach(chart => chart?.applyOptions({ timeScale: { fixLeftEdge: historyRestricted } }));
  }, [historyRestricted, paneKey, resolvedTheme]);

  const subscribeRangeSync = (source: IChartApi) => {
    const sync = (range: LogicalRange | null) => {
      if (!range || syncingRangeRef.current) return;
      if (source !== mainChartRef.current?.chart && !alignedPanes.current.has(source)) return;
      if (historyGesture.current.active) checkHistoryBoundary();
      publishChartObservation(source);
      syncingRangeRef.current = true;
      [mainChartRef.current?.chart, ...paneCharts()]
        .filter((chart): chart is IChartApi => Boolean(chart) && chart !== source)
        .forEach((chart) => chart.timeScale().setVisibleLogicalRange(range));
      syncingRangeRef.current = false;
    };
    source.applyOptions({ timeScale: { fixLeftEdge: historyAccess.current.restricted } });
    source.timeScale().subscribeVisibleLogicalRangeChange(sync);
    return () => source.timeScale().unsubscribeVisibleLogicalRangeChange(sync);
  };

  useEffect(() => {
    if (!mainRef.current) return;
    const chart = createChart(
      mainRef.current,
      chartOptions(resolvedTheme, paneTypes.length === 0, false, timeZone, undefined, priceTick)
    );
    const candles = chart.addSeries(CandlestickSeries, {
      priceLineVisible: Boolean(settings.lastPriceLine),
      lastValueVisible: Boolean(settings.lastPriceLine),
      priceFormat: contractPriceFormat(priceTick),
      ...candleSeriesOptions(preferences.candleStyle)
    });
    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
      lastValueVisible: false,
      priceLineVisible: false
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const annotationPrimitive = new ChartAnnotationsPrimitive();
    candles.attachPrimitive(annotationPrimitive);
    const userPrimitive = new UserDrawingsPrimitive();
    candles.attachPrimitive(userPrimitive);
    mainChartRef.current = { chart, candles, volume, maLines: new Map(), annotations: annotationPrimitive, annotationLines: [], alertLines: [], protectionLines: [], userDrawings: userPrimitive };
    const container = mainRef.current;
    const interaction = new DrawingInteraction(container, chart, userPrimitive, () => 0, drawingHooks);
    interactionRef.current = interaction;
    const detachInteraction = interaction.attach();
    const handleContext = (event: MouseEvent) => {
      event.preventDefault();
      event.stopPropagation();
      const rect = container.getBoundingClientRect();
      const y = event.clientY - rect.top;
      if (y < 0 || y >= chart.paneSize(0).height) return;
      // 右键一条自己画的线: the drawing's own menu; a placement in progress never selects anything.
      const drawing = drawingToolRef.current ? undefined : userPrimitive.hitTestDrawing(event.clientX - rect.left, y)?.drawing;
      const raw = candles.coordinateToPrice(y);
      const tick = priceTickRef.current;
      if (raw == null || !Number.isFinite(raw) || !tick || tick <= 0) return;
      const price = snapToTick(raw, tick);
      // The fourth argument rides along only when a line was hit (the plain right-click keeps its
      // three-argument shape).
      if (price > 0 && drawing) onPriceContextRef.current?.(price, event.clientX, event.clientY, drawing);
      else if (price > 0) onPriceContextRef.current?.(price, event.clientX, event.clientY);
    };
    container.addEventListener("contextmenu", handleContext);
    const unsubscribeRange = subscribeRangeSync(chart);
    chart.timeScale().fitContent();
    // A focused trade keeps its own span; otherwise a widened chart refills its width (#751 C).
    const stopRefit = refitWhenWidened(container, chart, () => chart.timeScale().fitContent(), () => focusedTrade.current !== undefined);
    return () => {
      stopRefit();
      unsubscribeRange();
      detachInteraction();
      interactionRef.current = undefined;
      container.removeEventListener("contextmenu", handleContext);
      if (mainChartRef.current?.selectedLine) {
        candles.removePriceLine(mainChartRef.current.selectedLine);
      }
      mainChartRef.current = undefined;
      chart.remove();
    };
    // Created once; the options and data effects below re-apply everything this effect reads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The MA line series follow the configured periods: new periods get a series, removed ones lose
  // theirs; a disabled line keeps its series with `visible: false` (the options effect toggles it).
  useEffect(() => {
    const main = mainChartRef.current;
    if (!main) return;
    const colors = MA_LINE_COLORS[resolvedTheme];
    for (const [period, series] of main.maLines) {
      if (!indicators.maLines.some((line) => line.period === period)) {
        main.chart.removeSeries(series);
        main.maLines.delete(period);
      }
    }
    indicators.maLines.forEach((line, index) => {
      if (main.maLines.has(line.period)) return;
      main.maLines.set(line.period, main.chart.addSeries(LineSeries, {
        color: colors[index % colors.length],
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false
      }));
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [maKey]);

  // The BOLL band exists only while it is on: three lines of one hue, mid solid and bands dashed.
  useEffect(() => {
    const main = mainChartRef.current;
    if (!main) return;
    const color = BOLL_LINE_COLOR[resolvedTheme];
    const lineOptions = { color, lineWidth: 1 as const, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    if (!indicators.boll.enabled) {
      for (const series of [main.bollMid, main.bollTop, main.bollBottom]) if (series) main.chart.removeSeries(series);
      main.bollMid = main.bollTop = main.bollBottom = undefined;
      return;
    }
    if (!main.bollMid || !main.bollTop || !main.bollBottom) {
      main.bollMid = main.chart.addSeries(LineSeries, { ...lineOptions, lineStyle: 0 });
      main.bollTop = main.chart.addSeries(LineSeries, { ...lineOptions, lineStyle: 2 });
      main.bollBottom = main.chart.addSeries(LineSeries, { ...lineOptions, lineStyle: 2 });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bollKey]);

  useEffect(() => {
    const main = mainChartRef.current;
    if (!main) return;
    if (!indicators.sar.enabled) {
      if (main.sar) main.chart.removeSeries(main.sar);
      main.sar = undefined;
    } else if (!main.sar) {
      main.sar = main.chart.addSeries(LineSeries, { color: SAR_POINT_COLOR[resolvedTheme], lineVisible: false, pointMarkersVisible: true,
        pointMarkersRadius: 1.5, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sarKey]);

  useEffect(() => {
    const main = mainChartRef.current;
    if (main) {
      main.candles.setData(chartBars);
      if (chartBars.length) rumChartPainted();
      // Volume follows the candles' direction colours (涨跌颜色 included), faded behind the price.
      const volumeTone = directionColors(.28);
      main.volume.setData(chartBars.map((bar) => ({
        time: bar.time,
        value: bar.volume,
        color: bar.close >= bar.open ? volumeTone.up : volumeTone.down
      })));
      indicators.maLines.forEach((line, index) => {
        main.maLines.get(line.period)?.setData(seriesData.ma[index] ?? []);
      });
      if (indicators.boll.enabled) {
        main.bollMid?.setData(seriesData.boll.mid);
        main.bollTop?.setData(seriesData.boll.top);
        main.bollBottom?.setData(seriesData.boll.bottom);
      }
      main.sar?.setData(seriesData.sar);
    }
    for (const bundle of paneBundles.current.values()) setPaneData(bundle, seriesData, chartBars);
    // A fresh contract/period with fewer bars than the pane can hold: fit them, or the bottom pane's
    // default span (pane width ÷ bar spacing) — synced to every pane — leaves the left side empty.
    // Runs after the sub-panes' own data effects (children first), so the fit is what gets synced.
    const fitKey = `${durationSeconds}:${chartBars[0]?.time ?? ""}`;
    if (main && chartBars.length && fitKeyRef.current !== fitKey && !tradeFocus) {
      fitKeyRef.current = fitKey;
      // The pane may not be measured yet on the first data (autoSize): fall back to its element.
      const width = main.chart.paneSize?.(0)?.width || main.chart.chartElement?.()?.clientWidth || 0;
      const capacity = width / (main.chart.timeScale().options?.().barSpacing || 6);
      if (chartBars.length < capacity) main.chart.timeScale().fitContent();
    }
    if (main) publishChartObservation(main.chart);
    // Direction colours come from the document tokens: re-colour when the theme or 涨跌颜色 changes.
    // durationSeconds/tradeFocus are only read for the one-time fit; new bars arrive with any change of them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartBars, seriesData, indicators, resolvedTheme, preferences.upDownColors]);

  useEffect(() => {
    const main = mainChartRef.current;
    if (!main || !chartBars.length) return;
    if (!tradeFocus) {
      if (focusedTrade.current) {
        if (previousTradeRange.current) main.chart.timeScale().setVisibleLogicalRange(previousTradeRange.current);
        else main.chart.timeScale().fitContent();
      }
      focusedTrade.current = undefined; previousTradeRange.current = null; return;
    }
    const focusKey = `${tradeFocus.openTime}:${tradeFocus.closeTime}:${durationSeconds}`;
    if (focusedTrade.current === focusKey) return;
    const toBar = (time: number) => time / 1e9 < chartBars[0].time || time / 1e9 >= chartBars.at(-1)!.time + durationSeconds ? undefined : [...chartBars].reverse().find(bar => bar.time <= time / 1_000_000_000 && time / 1e9 < bar.time + durationSeconds)?.time;
    let start = toBar(tradeFocus.openTime), end = toBar(tradeFocus.closeTime);
    // The AI's span (chartFocus command) may run past the loaded bars: show what is loaded.
    if (tradeFocus.direction === "agent") { start ??= chartBars[0].time; end ??= chartBars.at(-1)!.time; }
    if (start === undefined || end === undefined) { onFocusResult?.("成交不在当前已加载 K 线范围内，请查看历史回放"); return; }
    if (!focusedTrade.current) previousTradeRange.current = main.chart.timeScale().getVisibleLogicalRange();
    focusedTrade.current = focusKey;
    onFocusResult?.("");
    const first = chartBars.findIndex(bar => bar.time === start), last = chartBars.findIndex(bar => bar.time === end);
    main.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, first - 8), to: last + 8 });
  }, [tradeFocus, chartBars, durationSeconds, onFocusResult]);

  useEffect(() => {
    const main = mainChartRef.current;
    if (!main || !chartBars.length) return;
    const toBar = (time: number) => {
      const seconds = time / 1_000_000_000;
      if (seconds < chartBars[0].time || seconds >= chartBars.at(-1)!.time + durationSeconds) return undefined;
      return [...chartBars].reverse().find(bar => bar.time <= seconds && seconds < bar.time + durationSeconds)?.time;
    };
    const filled = tradeFills.flatMap(fill => {
      // 清空图上标记: fills retired by the clear stamp are not drawn; new fills still appear.
      if (!fillVisibleAfterClear(symbol, fill.tradeDateTime)) return [];
      const time = toBar(fill.tradeDateTime);
      if (time === undefined) return [];
      const buy = fill.direction === "BUY";
      const label = fill.offset === "OPEN" ? buy ? "开多" : "开空" : buy ? "平空" : "平多";
      return [{ id: `trade:${fill.tradeId}`, time, position: buy ? "belowBar" as const : "aboveBar" as const,
        shape: buy ? "arrowUp" as const : "arrowDown" as const,
        color: selectedTradeIds?.includes(fill.tradeId) ? resolvedTheme === "light" ? "#4459bc" : "#cbd2ff" : fill.offset === "OPEN" ? "#8b9dff" : "#d8b96c", text: (selectedTradeIds?.includes(fill.tradeId) ? "选中 " : "AI ") + label + " " + fill.volume }];
    });
    if (tradeFocus && tradeFocus.direction !== "event" && tradeFocus.direction !== "agent") {
      const start = toBar(tradeFocus.openTime), end = toBar(tradeFocus.closeTime);
      if (start !== undefined) filled.push({ id:"review-open", time: start, position: "belowBar", shape: "arrowUp", color: "#cbd2ff", text: "选中开仓" });
      if (end !== undefined) filled.push({ id:"review-close", time: end, position: "aboveBar", shape: "arrowDown", color: "#f5d888", text: "选中平仓" });
    }
    // 规则回测: buys (开多 / 平空) below the bar pointing up, sells above pointing down.
    const style = getComputedStyle(document.documentElement);
    const buyColor = style.getPropertyValue("--market-up").trim() || "#b82e3a";
    const sellColor = style.getPropertyValue("--market-down").trim() || "#087451";
    for (const mark of backtestMarks) {
      const time = toBar(mark.time);
      if (time === undefined) continue;
      const buy = (mark.kind === "open") === (mark.direction === "long");
      filled.push({ id: `backtest:${mark.id}`, time, position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown",
        color: buy ? buyColor : sellColor, text: mark.label });
    }
    const markers = createSeriesMarkers(main.candles, filled.sort((a, b) => Number(a.time) - Number(b.time)));
    const click = (event: {hoveredObjectId?: unknown}) => {
      if (typeof event.hoveredObjectId !== "string" || !event.hoveredObjectId.startsWith("trade:")) return;
      const fill = tradeFills.find(item => `trade:${item.tradeId}` === event.hoveredObjectId);
      const time = fill && toBar(fill.tradeDateTime);
      if (time !== undefined) onTradeSelect?.(tradeFills.filter(item => toBar(item.tradeDateTime) === time).map(item => item.tradeId));
    };
    main.chart.subscribeClick(click);
    return () => { main.chart.unsubscribeClick(click); markers.detach(); };
  }, [tradeFills, backtestMarks, chartBars, tradeFocus, durationSeconds, selectedTradeIds, onTradeSelect, resolvedTheme, symbol]);

  useEffect(() => {
    const main = mainChartRef.current;
    if (!main) return;
    const series: LineApi[] = [];
    for (const extension of extensions.filter(item => item.enabled)) {
      const add = (data: Array<{ time: UTCTimestamp; value: number }>, title: string) => {
        const line = main.chart.addSeries(LineSeries, { color: extension.color, lineWidth: 1, title,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
        line.setData(data); series.push(line);
      };
      if (extension.kind === "sma") add(lineData(movingAverage(chartBars, extension.period)), extension.name);
      else if (extension.kind === "ema") {
        const values = emaSeries(chartBars.map(bar => bar.close), extension.period);
        add(chartBars.flatMap((bar, i) => i >= extension.period - 1 ? [{ time: bar.time, value: values[i] }] : []), extension.name);
      } else {
        for (const high of [true, false]) add(chartBars.flatMap((bar, i) => {
          if (i < extension.period - 1) return [];
          const values = chartBars.slice(i + 1 - extension.period, i + 1).map(item => high ? item.high : item.low);
          return [{ time: bar.time, value: high ? Math.max(...values) : Math.min(...values) }];
        }), extension.name + (high ? "上轨" : "下轨"));
      }
    }
    return () => { for (const line of series) main.chart.removeSeries(line); };
  }, [extensions, chartBars]);

  useEffect(() => {
    const main = mainChartRef.current;
    if (!main) return;
    main.chart.applyOptions(chartOptions(resolvedTheme, paneTypes.length === 0, false, timeZone, undefined, priceTick));
    main.volume.applyOptions({ visible: settings.volume });
    main.candles.applyOptions({ priceLineVisible: Boolean(settings.lastPriceLine), lastValueVisible: Boolean(settings.lastPriceLine), priceFormat: contractPriceFormat(priceTick), ...candleSeriesOptions(preferences.candleStyle) });
    const colors = MA_LINE_COLORS[resolvedTheme];
    indicators.maLines.forEach((line, index) => {
      main.maLines.get(line.period)?.applyOptions({ color: colors[index % colors.length], visible: line.enabled });
    });
    const bollColor = BOLL_LINE_COLOR[resolvedTheme];
    for (const series of [main.bollMid, main.bollTop, main.bollBottom]) series?.applyOptions({ color: bollColor });
    main.sar?.applyOptions({ color: SAR_POINT_COLOR[resolvedTheme] });
    for (const bundle of paneBundles.current.values()) {
      const showsTime = bundle.type === paneTypes.at(-1);
      bundle.chart.applyOptions(chartOptions(resolvedTheme, showsTime, true, timeZone, bundle.type));
      if (bundle.type === "rsi") bundle.rsi?.applyOptions({ color: RSI_LINE_COLOR[resolvedTheme] });
      if (bundle.type === "oi") bundle.oi?.applyOptions({ color: OI_LINE_COLOR[resolvedTheme] });
      if (bundle.type === "cci") bundle.cci?.applyOptions({ color: CCI_LINE_COLOR[resolvedTheme] });
    }
    // `paneKey` stands in for `paneTypes` (a fresh array identity must not re-run the options).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings, indicators, paneKey, resolvedTheme, timeZone, preferences.candleStyle, preferences.upDownColors, priceTick]);

  useEffect(() => {
    const main = mainChartRef.current;
    if (!main) return;
    if (main.selectedLine) {
      main.candles.removePriceLine(main.selectedLine);
      main.selectedLine = undefined;
    }
    if (selectedPrice !== undefined && Number.isFinite(selectedPrice)) {
      main.selectedLine = main.candles.createPriceLine({
        price: selectedPrice,
        color: "#345cff",
        lineWidth: 1,
        lineStyle: 0,
        axisLabelVisible: true,
        title: "候选"
      });
    }
  }, [selectedPrice]);

  // 到价提醒: this contract's active alerts as dashed price lines with an axis label each.
  // Redrawn (not patched) so a crossing or a cancel is just a shorter list; the colour follows
  // the theme's --price-alert pair so the label and the line stay one voice.
  useEffect(() => {
    const main = mainChartRef.current;
    if (!main) return;
    for (const line of main.alertLines) main.candles.removePriceLine(line);
    const token = getComputedStyle(document.documentElement).getPropertyValue("--price-alert").trim();
    const color = token || (resolvedTheme === "dark" ? "#a78bfa" : "#5b21b6");
    main.alertLines = alertLines
      .filter(alert => Number.isFinite(alert.price))
      .map(alert => main.candles.createPriceLine({
        price: alert.price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "提醒"
      }));
    if (stackRef.current) stackRef.current.dataset.agentChartAlerts = String(alertLines.length);
  }, [alertLines, resolvedTheme]);

  // 下单顺带保护: this contract's armed stop / take as thin dotted price lines — 止损 orange, 止盈
  // cyan (the theme's --protection-* pair), dotted so they never read as the alerts' dashed violet.
  useEffect(() => {
    const main = mainChartRef.current;
    if (!main) return;
    for (const { line } of main.protectionLines) main.candles.removePriceLine(line);
    const styles = getComputedStyle(document.documentElement);
    main.protectionLines = protectionLines
      .filter(item => Number.isFinite(item.price))
      .map(item => ({ item, line: main.candles.createPriceLine({
        price: item.price,
        color: styles.getPropertyValue(item.kind === "stop" ? "--protection-stop" : "--protection-take").trim()
          || (item.kind === "stop" ? "#b85000" : "#0e7490"),
        lineWidth: item.pending ? 2 : 1, lineStyle: item.pending ? 2 : 3, axisLabelVisible: true,
        title: item.pending ? "待确认" : item.kind === "stop" ? "止损" : "止盈"
      }) }));
    if (stackRef.current) stackRef.current.dataset.agentChartProtection = String(protectionLines.length);
  }, [protectionLines, resolvedTheme]);

  // 用户画线: a new tool, contract or period drops a placement in progress.
  useEffect(() => { interactionRef.current?.reset(); }, [drawingTool, symbol, durationSeconds]);
  // A line hidden, deleted or locked under a finger: that drag ends unsaved.
  useEffect(() => { interactionRef.current?.sync(userDrawings); }, [userDrawings, drawingsLocked]);
  useEffect(() => { if (drawingFinish) interactionRef.current?.finish(); }, [drawingFinish]);

  // The user-drawing layer itself: this contract's own drawings for this period plus the placement's
  // draft — redrawn as one list; drawing and hit-testing share the geometry.
  useEffect(() => {
    const main = mainChartRef.current;
    if (!main) return;
    main.userDrawings.update({ items: userDrawings, palette: drawingPalette(), selectedId: selectedDrawingId ?? null, activeHandle: selectedDrawingHandle,
      bars: chartBars, duration: durationSeconds, tick: priceTick });
    if (stackRef.current) stackRef.current.dataset.agentUserDrawings = String(userDrawings.length);
  }, [userDrawings, chartBars, durationSeconds, resolvedTheme, selectedDrawingId, selectedDrawingHandle, priceTick]);

  // The AI annotation layer, per pane: horizontal lines as price lines (the primitive draws their
  // numbered label inside the pane, clear of the axis text), everything else in the primitive.
  // Numbers ①②③ follow list order across panes so the reply can refer to them; an item whose pane
  // is not shown simply has no bundle here and is not drawn (the Runtime's receipt says so).
  useEffect(() => {
    const colors = toneColors(stackRef.current);
    const numbered: NumberedAnnotation[] = annotations.map((item, index) => ({ ...item, number: index + 1 }));
    const panes: Array<[string, AnnotatedPane | undefined, ISeriesApi<"Candlestick" | "Line"> | undefined]> = [
      ["main", mainChartRef.current, mainChartRef.current?.candles],
      ...paneTypes.map((type) => {
        const bundle = paneBundles.current.get(type);
        return [type, bundle, bundle?.lineSeries] as [string, AnnotatedPane | undefined, ISeriesApi<"Candlestick" | "Line"> | undefined];
      }),
    ];
    for (const [pane, bundle, series] of panes) {
      if (!bundle || !series) continue;
      const own = numbered.filter(item => (item.pane ?? "main") === pane);
      for (const line of bundle.annotationLines) series.removePriceLine(line);
      bundle.annotationLines = own.filter(item => item.kind === "hline").map(item => series.createPriceLine({
        price: item.price!, color: colors[annotationTone(item)], lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: ""
      }));
      bundle.annotations.update(own, colors, chartBars, durationSeconds);
    }
    if (stackRef.current) stackRef.current.dataset.agentChartAnnotations = String(annotations.length);
    // `paneKey` stands in for `paneTypes` (a fresh array identity must not redraw the drawings).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [annotations, chartBars, durationSeconds, resolvedTheme, paneKey]);

  // The label popovers (就地编辑): one at a time, each a commandable disclosure surface.
  const [popover, setPopover] = useState<{ open: OpenPopover; rect: DOMRect }>();
  const maLabelRef = useRef<HTMLButtonElement>(null);
  const addLabelRef = useRef<HTMLButtonElement>(null);
  const paneLabelRefs = useRef(new Map<SubPaneType, HTMLButtonElement>());
  const anchorOf = (open: OpenPopover): DOMRect | undefined =>
    open.kind === "ma" ? maLabelRef.current?.getBoundingClientRect()
    : open.kind === "add" ? addLabelRef.current?.getBoundingClientRect()
    : paneLabelRefs.current.get(open.type)?.getBoundingClientRect();
  const openPopover = (open: OpenPopover) => {
    const rect = anchorOf(open);
    if (rect) setPopover({ open, rect });
  };
  const closePopover = () => setPopover(undefined);
  useDisclosureSurface("chart-ma-editor", { state: popover?.open.kind === "ma", available: editable, apply: open => (open ? openPopover({ kind: "ma" }) : closePopover()) });
  useDisclosureSurface("chart-pane-add", { state: popover?.open.kind === "add", available: editable, apply: open => (open ? openPopover({ kind: "add" }) : closePopover()) });
  // Four fixed registrations (hook order must not follow the pane list): a pane type not
  // currently shown registers nothing, so its command fails with 该界面当前不可用 — the truth.
  useDisclosureSurface("chart-pane-editor", { id: "kd", available: editable && paneTypes.includes("kd"), state: popover?.open.kind === "pane" && popover.open.type === "kd", apply: open => (open ? openPopover({ kind: "pane", type: "kd" }) : closePopover()) });
  useDisclosureSurface("chart-pane-editor", { id: "macd", available: editable && paneTypes.includes("macd"), state: popover?.open.kind === "pane" && popover.open.type === "macd", apply: open => (open ? openPopover({ kind: "pane", type: "macd" }) : closePopover()) });
  useDisclosureSurface("chart-pane-editor", { id: "rsi", available: editable && paneTypes.includes("rsi"), state: popover?.open.kind === "pane" && popover.open.type === "rsi", apply: open => (open ? openPopover({ kind: "pane", type: "rsi" }) : closePopover()) });
  useDisclosureSurface("chart-pane-editor", { id: "oi", available: editable && paneTypes.includes("oi"), state: popover?.open.kind === "pane" && popover.open.type === "oi", apply: open => (open ? openPopover({ kind: "pane", type: "oi" }) : closePopover()) });
  useDisclosureSurface("chart-pane-editor", { id: "cci", available: editable && paneTypes.includes("cci"), state: popover?.open.kind === "pane" && popover.open.type === "cci", apply: open => (open ? openPopover({ kind: "pane", type: "cci" }) : closePopover()) });
  useDisclosureSurface("chart-pane-editor", { id: "volume", available: editable && paneTypes.includes("volume"), state: popover?.open.kind === "pane" && popover.open.type === "volume", apply: open => (open ? openPopover({ kind: "pane", type: "volume" }) : closePopover()) });
  const addPane = (type: SubPaneType) => {
    if (paneTypes.length >= SUB_PANES_MAX || paneTypes.includes(type)) return;
    changeIndicators({ subPaneList: [...paneTypes, type] });
    closePopover();
  };

  const resizePane = (index: number, event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    const startY = event.clientY;
    const start = { main: sizes.main, panes: [...sizes.panes] };
    const height = stackRef.current?.clientHeight || 600;
    const clampPane = (value: number) => Math.min(36, Math.max(12, value));
    const withPane = (panes: number[], position: number, value: number) => panes.map((share, at) => at === position ? clampPane(value) : share);
    const move = (next: PointerEvent) => {
      const delta = ((next.clientY - startY) / height) * 100;
      if (index === 0) {
        setSizes({ main: Math.min(76, Math.max(34, start.main + delta)), panes: withPane(start.panes, 0, start.panes[0] - delta) });
      } else {
        setSizes({ main: start.main, panes: withPane(withPane(start.panes, index - 1, start.panes[index - 1] + delta), index, start.panes[index] - delta) });
      }
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up, { once: true });
  };

  const rows = paneGridRows(sizes);
  const readout = settings.crosshair && cursorX !== undefined
    ? barReadout(chartBars, cursorTime, { decimals: decimalsForTick(priceTick), daily: durationSeconds >= 86400, timeZone }) : null;
  const editorStatus = editable ? indicatorStatus : undefined;
  const addPaneSlot = paneTypes.length;  // The ＋副图 button sits on the last label row (the price label when there are no panes).
  const addPaneButton = editable && paneTypes.length < SUB_PANES_MAX
    ? <button type="button" className="fdw-ind-label fdw-ind-add-pane" ref={addLabelRef} aria-label="添加副图" onClick={() => popover?.open.kind === "add" ? closePopover() : openPopover({ kind: "add" })}>＋ 副图</button>
    : null;

  return (
    <div
      className={`fdw-trading-charts ${resolvedTheme === "dark" ? "dark" : "light"}`}
      ref={stackRef}
      onPointerDownCapture={() => { historyGesture.current.active = false; historyGesture.current.notified = false; }}
      onPointerUpCapture={() => { historyGesture.current.active = false; }}
      onPointerCancelCapture={() => { historyGesture.current.active = false; }}
      onPointerLeave={() => { historyGesture.current.active = false; clearCursor(); }}
      onPointerMoveCapture={event => { if (event.buttons && event.movementX > 0) { historyGesture.current.active = true; checkHistoryBoundary(); } }}
      onWheelCapture={event => {
        const now = Date.now();
        if (now - historyGesture.current.lastWheel > 500) historyGesture.current.notified = false;
        historyGesture.current.lastWheel = now;
        if (event.deltaY > 0 || event.deltaX < 0) checkHistoryBoundary();
      }}
      style={{ gridTemplateRows: rows }}
      onPointerMove={(event) => {
        const rect = stackRef.current?.getBoundingClientRect();
        if (!rect) return;
        const x = event.clientX - rect.left;
        if (x < 0 || x > rect.width - 75) { setCursorX(undefined); setCursorTime(undefined); return; }
        setCursorX(x);
        const time = mainChartRef.current?.chart.timeScale().coordinateToTime(x);
        setCursorTime(typeof time === "number" ? time : undefined);
      }}
      onPointerCancel={clearCursor}
    >
      {settings.crosshair && cursorX !== undefined ? <span className="fdw-synced-crosshair" style={{ left: cursorX }} /> : null}
      {settings.crosshair && cursorX !== undefined && cursorTime !== undefined ? <output className="fdw-crosshair-time" style={{ left: Math.max(66, Math.min(cursorX, (stackRef.current?.clientWidth ?? 300) - 144)) }}>{new Intl.DateTimeFormat("zh-CN", { timeZone, month: "2-digit", day: "2-digit", ...(durationSeconds >= 86400 ? {} : { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }) }).format(new Date(cursorTime * 1000))}</output> : null}
      <section className="fdw-chart-pane" data-region="chart.main">
        <div className="fdw-chart-pane-label">
          <strong>价格</strong>
          {editable
            ? <button type="button" className="fdw-ind-label" ref={maLabelRef} onClick={() => popover?.open.kind === "ma" ? closePopover() : openPopover({ kind: "ma" })}>{maLinesLabel(indicators)}</button>
            : <span>{maLinesLabel(indicators)}</span>}
          <span>{extensions.filter(item => item.enabled).map(item => " · " + item.name).join("")}</span>
          {addPaneSlot === 0 ? addPaneButton : null}
          {helpAvailable && <button type="button" aria-label="图表说明" onClick={onHelp ?? (() => openAppHelp("chart"))}>?</button>}
          {historyNote ? <span className="fdw-history-note">{historyNote}</span> : null}
          {readout ? <span className="fdw-bar-readout" data-testid="bar-readout">
            <span>{readout.when}</span><span>开 <b>{readout.open}</b></span><span>高 <b>{readout.high}</b></span>
            <span>低 <b>{readout.low}</b></span><span>收 <b data-direction={readout.direction}>{readout.close}</b></span>
            {readout.change ? <span data-direction={readout.direction}>{readout.change}</span> : null}
            <span>量 <b>{readout.volume}</b></span>{readout.openInterest ? <span>持仓 <b>{readout.openInterest}</b></span> : null}
          </span> : null}
        </div>
        <div ref={mainRef} className="fdw-chart-canvas" aria-label="K 线图" />
      </section>
      {paneTypes.map((type, index) => <Fragment key={type}>
        <button type="button" className="fdw-pane-resizer"
          aria-label={index === 0 ? `调整价格图和${SUB_PANE_NAMES[type]}副图高度` : `调整${SUB_PANE_NAMES[paneTypes[index - 1]]}副图和${SUB_PANE_NAMES[type]}副图高度`}
          onPointerDown={(event) => resizePane(index, event)} />
        <section className="fdw-chart-pane" data-region={`chart.${type}`}>
          <div className="fdw-chart-pane-label">
            {editable
              ? <button type="button" className="fdw-ind-label" ref={(element) => { if (element) paneLabelRefs.current.set(type, element); else paneLabelRefs.current.delete(type); }}
                  onClick={() => popover?.open.kind === "pane" && popover.open.type === type ? closePopover() : openPopover({ kind: "pane", type })}>{subPaneLabel(type, indicators)}</button>
              : <strong>{subPaneLabel(type, indicators)}</strong>}
            {subPaneDataShort(type, indicators, chartBars.length)
              ? <span className="fdw-pane-short">数据不足 · 需 {subPaneMinBars(type, indicators)} 根K线</span>
              : <>
            {type === "kd" ? <span>K {latestKd?.k.toFixed(2) ?? "—"} · D {latestKd?.d.toFixed(2) ?? "—"}</span> : null}
            {type === "macd" ? <span>DIF {latestMacd?.diff.toFixed(3) ?? "—"} · DEA {latestMacd?.dea.toFixed(3) ?? "—"} · 柱 {latestMacd?.histogram.toFixed(3) ?? "—"}</span> : null}
            {type === "rsi" ? <span>RSI {latestRsi && "value" in latestRsi ? latestRsi.value.toFixed(1) : "—"}</span> : null}
            {type === "oi" ? <span>持仓量 {latestOi != null ? latestOi.toLocaleString("zh-CN") : "—"}</span> : null}
            {type === "cci" ? <span>CCI {latestCci && "value" in latestCci ? latestCci.value.toFixed(1) : "—"}</span> : null}
            {type === "volume" ? <span>成交量 {chartBars.at(-1)?.volume.toLocaleString("zh-CN") ?? "—"}</span> : null}
              </>}
            {addPaneSlot === index + 1 ? addPaneButton : null}
          </div>
          <SubPaneCanvas type={type} showsTime={type === paneTypes.at(-1)} resolvedTheme={resolvedTheme} timeZone={timeZone}
            seriesData={seriesData} chartBars={chartBars} register={registerPane} subscribe={subscribeRangeSync} onContext={onPaneContextRef}
            mainRange={() => mainChartRef.current?.chart.timeScale().getVisibleLogicalRange() ?? null} />
        </section>
      </Fragment>)}
      {popover ? <ChartPopover
        title={popover.open.kind === "ma" ? "均线与布林带" : popover.open.kind === "add" ? "添加副图" : `${SUB_PANE_NAMES[popover.open.type]} 副图`}
        anchorRect={popover.rect} onClose={closePopover} width={popover.open.kind === "ma" ? 360 : 320}>
        {popover.open.kind === "ma" ? <MaLinesEditor indicators={indicators} onChange={changeIndicators} status={editorStatus} />
          : popover.open.kind === "add" ? <AddPaneChooser indicators={indicators} onAdd={addPane} status={editorStatus} />
          : <SubPaneEditor index={paneTypes.indexOf(popover.open.type)} indicators={indicators} onChange={changeIndicators} status={editorStatus} />}
      </ChartPopover> : null}
    </div>
  );
}

/** Lays one sub pane's series on the loaded bars (the workspace chart's data effect and the pane's own
 *  mount share it). An oscillator short of bars (数据不足) gets whitespace on every bar, so the pane stays
 *  empty but keeps the time axis it may carry; CCI and 成交量 joined 2026-10-09. */
function setPaneData(bundle: SubPaneBundle, seriesData: ReturnType<typeof buildSeriesData>, chartBars: ChartBar[]) {
  const blank = chartBars.map((bar) => ({ time: bar.time }));
  if (bundle.type === "kd") {
    bundle.k?.setData(seriesData.kd.length ? seriesData.kd.map((item) => ({ time: item.time as UTCTimestamp, value: item.k })) : blank);
    bundle.d?.setData(seriesData.kd.length ? seriesData.kd.map((item) => ({ time: item.time as UTCTimestamp, value: item.d })) : blank);
  } else if (bundle.type === "macd") {
    const tone = directionColors(.62);
    bundle.histogram?.setData(seriesData.macd.length ? seriesData.macd.map((item) => ({
      time: item.time as UTCTimestamp,
      value: item.histogram,
      color: item.histogram >= 0 ? tone.up : tone.down
    })) : blank);
    bundle.diff?.setData(seriesData.macd.length ? seriesData.macd.map((item) => ({ time: item.time as UTCTimestamp, value: item.diff })) : blank);
    bundle.dea?.setData(seriesData.macd.length ? seriesData.macd.map((item) => ({ time: item.time as UTCTimestamp, value: item.dea })) : blank);
  } else if (bundle.type === "rsi") {
    bundle.rsi?.setData(seriesData.rsi);
  } else if (bundle.type === "cci") {
    bundle.cci?.setData(seriesData.cci);
  } else if (bundle.type === "volume") {
    const tone = directionColors(.62);
    bundle.vol?.setData(chartBars.map(bar => ({ time: bar.time, value: bar.volume, color: bar.close >= bar.open ? tone.up : tone.down })));
  } else {
    bundle.oi?.setData(seriesData.oi);
  }
}

/** One dynamic sub pane (kd / macd / rsi / oi / cci / volume): its own chart, series and annotation primitive,
 * registered with the workspace chart so range sync, crosshair and the AI's drawings reach it. */
function SubPaneCanvas({ type, showsTime, resolvedTheme, timeZone, seriesData, chartBars, register, subscribe, mainRange, onContext }: {
  type: SubPaneType;
  chartBars: ChartBar[];
  showsTime: boolean;
  resolvedTheme: "light" | "dark";
  timeZone?: string;
  seriesData: ReturnType<typeof buildSeriesData>;
  register: (type: SubPaneType, bundle: SubPaneBundle | undefined) => void;
  subscribe: (chart: IChartApi) => () => void;
  mainRange: () => LogicalRange | null;
  /** 画线第二版: the pane's right-click opens its own menu (换成 / 参数 / 上移下移 / 关闭). */
  onContext?: { current?: (type: SubPaneType, x: number, y: number) => void };
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const open = (event: MouseEvent) => {
      if (!onContext?.current) return;
      event.preventDefault();
      event.stopPropagation();
      onContext.current(type, event.clientX, event.clientY);
    };
    element.addEventListener("contextmenu", open);
    return () => element.removeEventListener("contextmenu", open);
  }, [type, onContext]);
  const bundleRef = useRef<SubPaneBundle | undefined>(undefined);
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, chartOptions(resolvedTheme, showsTime, true, timeZone, type));
    const line = (color: string, priceFormat?: { type: "price"; precision: number; minMove: number }) =>
      chart.addSeries(LineSeries, { color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, ...(priceFormat ? { priceFormat } : {}) });
    const annotations = new ChartAnnotationsPrimitive();
    const bundle: SubPaneBundle = { type, chart, annotations, annotationLines: [] };
    if (type === "kd") {
      const priceFormat = { type: "price" as const, precision: 0, minMove: 1 };
      bundle.k = line("#f2c94c", priceFormat);
      bundle.d = line("#6aa8ff", priceFormat);
      bundle.lineSeries = bundle.k;
    } else if (type === "macd") {
      const priceFormat = { type: "price" as const, precision: 3, minMove: 0.001 };
      bundle.histogram = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false, priceFormat });
      bundle.diff = line("#f2c94c", priceFormat);
      bundle.dea = line("#5f8cff", priceFormat);
      bundle.lineSeries = bundle.diff;
    } else if (type === "rsi") {
      bundle.rsi = line(RSI_LINE_COLOR[resolvedTheme], { type: "price", precision: 0, minMove: 1 });
      for (const level of [70, 30]) {
        bundle.rsi.createPriceLine({ price: level, color: "rgba(130,145,165,.45)", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" });
      }
      bundle.lineSeries = bundle.rsi;
    } else if (type === "cci") {
      bundle.cci = line(CCI_LINE_COLOR[resolvedTheme], { type: "price", precision: 1, minMove: 0.1 });
      for (const level of [100, -100]) {
        bundle.cci.createPriceLine({ price: level, color: "rgba(130,145,165,.45)", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" });
      }
      bundle.lineSeries = bundle.cci;
    } else if (type === "volume") {
      // No line series: the AI draws on the oscillator panes, not on the volume bars.
      bundle.vol = chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false });
    } else {
      bundle.oi = line(OI_LINE_COLOR[resolvedTheme], undefined);
      bundle.oi.applyOptions({ priceFormat: { type: "volume" } });
      bundle.lineSeries = bundle.oi;
    }
    bundle.lineSeries?.attachPrimitive(annotations);
    bundleRef.current = bundle;
    register(type, bundle);
    const unsubscribe = subscribe(chart);
    // A pane added while the chart already shows a span follows it; the mount-time panes all fit.
    const span = mainRange();
    if (span) chart.timeScale().setVisibleLogicalRange(span);
    else chart.timeScale().fitContent();
    return () => {
      unsubscribe();
      register(type, undefined);
      bundleRef.current = undefined;
      chart.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [type]);

  useEffect(() => {
    const bundle = bundleRef.current;
    if (bundle) setPaneData(bundle, seriesData, chartBars);
  }, [seriesData, type, chartBars]);

  return <div ref={containerRef} className="fdw-chart-canvas" />;
}

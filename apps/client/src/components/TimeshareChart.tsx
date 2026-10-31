import {
  ColorType,
  HistogramSeries,
  LineSeries,
  TickMarkType,
  createChart,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type Time,
  type UTCTimestamp
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import type { MarketKlineBar } from "../api";
import { TIMELESS_KINDS, anchorsOf, magnetPrice, pointsFor, snapToTick, type Anchor, type ChartDrawing, type DrawingKind } from "../chartDrawingModel";
import { buildTimeshare, type TimeshareData, type TradingSchedule } from "../timeshare";
import { openAppHelp } from "./AppHelp";
import type { ContextDrawing, TradingChartSettings } from "./TradingCharts";
import { UserDrawingsPrimitive, drawingPalette } from "./chartUserDrawings";
import { DrawingInteraction, type DrawingInteractionHooks, type HandleDrag } from "./chartDrawingInteraction";
import { refitWhenWidened } from "./chartRefit";
import { decimalsForTick, formatVolume } from "./futures/support";
import "./TradingCharts.css";

/**
 * 分时图 (2026-10-02): one trading day of 1-minute closes on a uniform minute axis (夜盘在前、
 * 日盘在后，分钟未到为空白), with the 当日均价 line and the 昨结 dashed reference. A view of the
 * same 60 s serial the 1 分 K 线 uses — no indicators, no sub panes, left price axis only. The
 * synthetic axis means no real times reach the chart, so it publishes no agentChartRange (the
 * AI's "当前可见区间" fallback covers questions about it).
 */
interface TimeshareChartProps {
  bars: MarketKlineBar[];
  schedule?: TradingSchedule | null;
  /** The quote's 昨结价: the dashed line and the readout's 涨跌幅 baseline; absent stays hidden. */
  preSettlement?: number | null;
  priceTick?: number | null;
  settings: TradingChartSettings;
  resolvedTheme?: "light" | "dark";
  symbol?: string;
  onPriceContext?: (price: number, x: number, y: number, drawing?: ContextDrawing) => void;
  helpAvailable?: boolean;
  onHelp?: () => void;
  /** 用户画线 (2026-10-02): this contract's own drawings on the minute axis — every anchor's real
   *  timestamp maps to the matching minute slot, so one drawing reads the same on both charts. */
  userDrawings?: ChartDrawing[];
  drawingTool?: DrawingKind | null;
  selectedDrawingId?: string | null;
  drawingsLocked?: boolean;
  drawingMagnet?: boolean;
  /** Anchors in real bar time (the slots are mapped back here). */
  onPlaceDrawing?: (kind: DrawingKind, anchors: Anchor[]) => void;
  onMoveDrawing?: (drawing: ChartDrawing, anchors: Anchor[]) => void;
  onDrawingSelect?: (id: string | null, at?: { clientX: number; clientY: number }, handle?: number | null) => void;
  /** 手机画线: the selected drawing's chosen anchor (drawn filled), a finger dragging one anchor (the loupe), the
   *  placement's point count, and a counter whose every change ends a 折线 in progress (the phone's 完成). */
  selectedDrawingHandle?: number | null;
  onDrawingHandleDrag?: (drag: HandleDrag | null) => void;
  onDrawingDraft?: (count: number) => void;
  drawingFinish?: number;
}

type Readout = NonNullable<TimeshareData["latest"]>;

const NO_USER_DRAWINGS: ChartDrawing[] = [];
/** The wall clock of a real timestamp ("HH:MM") — the key between real time and the slot table
 *  (the same China-time formatting 分时's own minuteOfDay uses). */
const clockFormatter = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const clockOfSeconds = (seconds: number) => clockFormatter.format(new Date(seconds * 1000));

function withAlpha(color: string, alpha: number): string {
  const hex = /^#([0-9a-f]{6})$/i.exec(color.trim())?.[1];
  if (!hex) return color;
  const value = parseInt(hex, 16);
  return `rgba(${value >> 16},${(value >> 8) & 255},${value & 255},${alpha})`;
}

/** Rising / falling colours from the document's market-direction tokens, like the K 线 volume. */
function directionTone(alpha: number) {
  const tokens = typeof getComputedStyle === "function" ? getComputedStyle(document.documentElement) : undefined;
  const up = tokens?.getPropertyValue("--market-up").trim() || "#b82e3a";
  const down = tokens?.getPropertyValue("--market-down").trim() || "#087451";
  return { up: withAlpha(up, alpha), down: withAlpha(down, alpha) };
}

function token(name: string, fallback: string): string {
  return (typeof getComputedStyle === "function"
    ? getComputedStyle(document.documentElement).getPropertyValue(name).trim()
    : "") || fallback;
}


interface Bundle {
  chart: IChartApi;
  price: ISeriesApi<"Line">;
  average: ISeriesApi<"Line">;
  volume: ISeriesApi<"Histogram">;
  settlementLine?: IPriceLine;
  userDrawings: UserDrawingsPrimitive;
}

export function TimeshareChart({
  bars,
  schedule,
  preSettlement,
  priceTick,
  settings,
  resolvedTheme = settings.theme === "light" ? "light" : "dark",
  symbol,
  onPriceContext,
  helpAvailable = true,
  onHelp,
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
  drawingFinish = 0
}: TimeshareChartProps) {
  const data = useMemo(() => buildTimeshare(bars, schedule), [bars, schedule]);
  // 用户画线 on the synthetic axis: a trendline endpoint's real timestamp becomes the matching
  // slot's time (keyed by clock); a placement's slot becomes the nearest traded minute's real
  // timestamp — one line, honest on both charts.
  const slotByClock = useMemo(() => new Map(data.slots.map(slot => [slot.clock, slot])), [data]);
  const clockToReal = useMemo(() => {
    const map = new Map<string, number>();
    for (const bar of bars) map.set(clockOfSeconds(Math.floor(bar.datetime / 1_000_000_000)), Math.floor(bar.datetime / 1_000_000_000));
    return map; // last write wins: the displayed day's minute beats an older day's same clock
  }, [bars]);
  const slotBars = useMemo(() => data.slots.map(slot => ({ time: slot.time as UTCTimestamp })), [data]);
  const viewDrawings = useMemo(() => userDrawings.flatMap(drawing => {
    if (TIMELESS_KINDS.has(drawing.kind)) return [drawing];
    const anchors = anchorsOf(drawing).map(anchor => ({ ...anchor, t: slotByClock.get(clockOfSeconds(anchor.t))?.time ?? Number.NaN }));
    return anchors.every(anchor => Number.isFinite(anchor.t)) ? [{ ...drawing, points: pointsFor(drawing.kind, anchors, drawing.points) }] : [];
  }), [userDrawings, slotByClock]);
  const containerRef = useRef<HTMLDivElement>(null);
  const bundleRef = useRef<Bundle | undefined>(undefined);
  const userDrawingsRef = useRef(userDrawings);
  userDrawingsRef.current = userDrawings;
  const dataRef = useRef(data);
  dataRef.current = data;
  const priceTickRef = useRef(priceTick);
  priceTickRef.current = priceTick;
  const preSettlementRef = useRef(preSettlement);
  preSettlementRef.current = preSettlement;
  const onPriceContextRef = useRef(onPriceContext);
  onPriceContextRef.current = onPriceContext;
  // 用户画线 (画线第二版): the pointer interaction subscribes once and reads the tool, switches and
  // callbacks through this object. Anchors are slot times on the chart; a placement or a move maps
  // each slot to its minute's real bar time (or the nearest earlier traded minute's).
  const drawingToolRef = useRef(drawingTool);
  drawingToolRef.current = drawingTool;
  const realOfSlotTime = (time: number): number | undefined => {
    const index = data.slots.findIndex(item => item.time === time);
    for (let at = index; at >= 0; at -= 1) {
      const real = clockToReal.get(data.slots[at].clock);
      if (real !== undefined) return real;
    }
    return undefined;
  };
  const toReal = (kind: DrawingKind, anchors: Anchor[]): Anchor[] | null => {
    if (TIMELESS_KINDS.has(kind)) return anchors;
    const mapped = anchors.map(anchor => ({ ...anchor, t: realOfSlotTime(anchor.t) ?? Number.NaN }));
    return mapped.every(anchor => Number.isFinite(anchor.t)) ? mapped : null;
  };
  const interactionRef = useRef<DrawingInteraction | undefined>(undefined);
  const drawingHooks: DrawingInteractionHooks = {
    tool: drawingTool, locked: drawingsLocked, magnet: drawingMagnet,
    toAnchor: (x, y, magnet) => {
      const bundle = bundleRef.current;
      const model = dataRef.current;
      if (!bundle || !model.slots.length) return null;
      const raw = bundle.price.coordinateToPrice(y);
      const logical = bundle.chart.timeScale().coordinateToLogical(x);
      if (raw == null || !Number.isFinite(raw) || logical == null) return null;
      const index = Math.max(0, Math.min(model.slots.length - 1, Math.round(logical)));
      const close = model.price[index]?.value;
      const price = magnet && close !== undefined ? magnetPrice(raw, { open: close, high: close, low: close, close }) : raw;
      const snapped = snapToTick(price, priceTickRef.current);
      return snapped > 0 ? { t: model.slots[index].time, p: snapped } : null;
    },
    onPlace: (kind, anchors) => { const real = toReal(kind, anchors); if (real) onPlaceDrawing?.(kind, real); },
    onMove: (drawing, anchors) => {
      const real = toReal(drawing.kind, anchors);
      const original = userDrawings.find(item => item.id === drawing.id);
      if (real && original) onMoveDrawing?.(original, real);
    },
    onSelect: (id, at, handle) => onDrawingSelect?.(id, at, handle),
    onHandleDrag: onDrawingHandleDrag,
    onDraft: count => onDrawingDraft?.(count),
  };
  if (interactionRef.current) interactionRef.current.hooks = drawingHooks;
  const clockByTimeRef = useRef(new Map<number, string>());
  clockByTimeRef.current = new Map(data.slots.map(slot => [slot.time, slot.clock]));
  const [readout, setReadout] = useState<Readout | null>(null);
  // A new contract or trading day restarts the line and re-spans the axis; ticks must not fight zoom.
  const fitKey = `${symbol ?? ""}:${data.tradingDay ?? ""}:${data.slots.length}`;
  const fittedRef = useRef("");

  // Created once; the options and data effects below re-apply everything this effect reads.
  useEffect(() => {
    if (!containerRef.current) return;
    const clockOf = (time: Time) => (typeof time === "number" ? clockByTimeRef.current.get(time) ?? "" : "");
    const dark = resolvedTheme === "dark";
    const lineColor = { color: dark ? "rgba(142,168,255,.55)" : "rgba(47,91,255,.45)", labelBackgroundColor: dark ? "#24335f" : "#2f5bff" };
    const chart = createChart(containerRef.current, {
      localization: { timeFormatter: (time: Time) => clockOf(time) || "—" },
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: dark ? "#0b0d10" : "#ffffff" },
        textColor: dark ? "#838882" : "#656a64",
        attributionLogo: false
      },
      grid: {
        vertLines: { visible: false },
        horzLines: { color: dark ? "rgba(148,163,184,.07)" : "rgba(15,23,42,.06)", style: 0 as const }
      },
      // 分时 reads its prices on the left; the right side stays clear of the axis entirely.
      leftPriceScale: { visible: true, autoScale: true, borderVisible: false, entireTextOnly: true, minimumWidth: 74, scaleMargins: { top: 0.07, bottom: 0.13 } },
      rightPriceScale: { visible: false },
      timeScale: {
        tickMarkFormatter: (time: Time, type: TickMarkType) => type === TickMarkType.Year || type === TickMarkType.Month || type === TickMarkType.DayOfMonth ? "" : clockOf(time),
        borderVisible: false,
        timeVisible: true,
        secondsVisible: false
      },
      crosshair: {
        mode: 0 as const,
        vertLine: { visible: true, color: lineColor.color, width: 1 as const, style: 0 as const, labelVisible: true, labelBackgroundColor: lineColor.labelBackgroundColor },
        horzLine: { visible: true, color: lineColor.color, width: 1 as const, style: 0 as const, labelVisible: true, labelBackgroundColor: lineColor.labelBackgroundColor }
      }
    });
    // Both lines live on the left scale (the right one is hidden): without priceScaleId they sat on the
    // hidden right scale and the chart showed no price axis at all (#712 B4).
    const price = chart.addSeries(LineSeries, {
      priceScaleId: "left",
      color: token("--accent", dark ? "#8ea8ff" : "#2f5bff"),
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false
    });
    const average = chart.addSeries(LineSeries, {
      priceScaleId: "left",
      color: token("--chart-average", "#a16207"),
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false
    });
    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
      lastValueVisible: false,
      priceLineVisible: false
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const userPrimitive = new UserDrawingsPrimitive();
    price.attachPrimitive(userPrimitive);
    bundleRef.current = { chart, price, average, volume, userDrawings: userPrimitive };
    const interaction = new DrawingInteraction(containerRef.current, chart, userPrimitive, () => chart.priceScale("left").width(), drawingHooks);
    interactionRef.current = interaction;
    const detachInteraction = interaction.attach();
    // The 分时 axis spans the whole slot table at any width (#751 C).
    const stopRefit = refitWhenWidened(containerRef.current, chart, () => {
      const slots = dataRef.current.slots.length;
      if (slots) chart.timeScale().setVisibleLogicalRange({ from: 0, to: slots - 1 });
    });

    const crosshair = (param: { time?: Time }) => {
      if (typeof param.time !== "number") { setReadout(null); return; }
      const model = dataRef.current;
      const index = model.slots.findIndex(slot => slot.time === param.time);
      const slot = model.slots[index];
      const pricePoint = model.price[index];
      if (!slot || !pricePoint || pricePoint.value === undefined) { setReadout(null); return; }
      const barVolume = model.volume.find(item => item.time === slot.time);
      setReadout({ time: slot.time, clock: slot.clock, price: pricePoint.value, average: model.average[index]?.value,
        volume: barVolume?.value ?? 0, up: barVolume?.up ?? true });
    };
    chart.subscribeCrosshairMove(crosshair);

    const container = containerRef.current;
    const handleContext = (event: MouseEvent) => {
      event.preventDefault();
      event.stopPropagation();
      const rect = container.getBoundingClientRect();
      const y = event.clientY - rect.top;
      if (y < 0 || y >= chart.paneSize(0).height) return;
      // 右键一条自己画的线: the media x the primitive hit-tests with starts right of the left
      // price axis (the container's left edge is the axis's), a placement never selects.
      const axis = chart.priceScale("left").width();
      const hitId = drawingToolRef.current ? undefined : userPrimitive.hitTestDrawing(event.clientX - rect.left - axis, y)?.drawing.id;
      // The menu acts on the stored drawing (real times), not its slot-mapped view.
      const drawing: ContextDrawing | undefined = hitId ? userDrawingsRef.current.find(item => item.id === hitId) : undefined;
      const raw = price.coordinateToPrice(y);
      const tick = priceTickRef.current;
      if (raw == null || !Number.isFinite(raw) || !tick || tick <= 0) return;
      const rounded = snapToTick(raw, tick);
      // The fourth argument rides along only when a line was hit (the plain right-click keeps its
      // three-argument shape).
      if (rounded > 0 && drawing) onPriceContextRef.current?.(rounded, event.clientX, event.clientY, drawing);
      else if (rounded > 0) onPriceContextRef.current?.(rounded, event.clientX, event.clientY);
    };
    container.addEventListener("contextmenu", handleContext);
    return () => {
      stopRefit();
      detachInteraction();
      interactionRef.current = undefined;
      container.removeEventListener("contextmenu", handleContext);
      chart.unsubscribeCrosshairMove(crosshair);
      bundleRef.current = undefined;
      chart.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 用户画线: a new tool, contract or trading day drops a placement in progress.
  useEffect(() => { interactionRef.current?.reset(); }, [drawingTool, symbol, data.tradingDay]);
  // A line hidden, deleted or locked under a finger: that drag ends unsaved.
  useEffect(() => { interactionRef.current?.sync(userDrawings); }, [userDrawings, drawingsLocked]);
  useEffect(() => { if (drawingFinish) interactionRef.current?.finish(); }, [drawingFinish]);

  // The user-drawing layer: the clock-mapped drawings on the minute axis; the canvas carries the
  // count for the observation snapshot like the K 线.
  useEffect(() => {
    const bundle = bundleRef.current;
    if (!bundle) return;
    bundle.userDrawings.update({ items: viewDrawings, palette: drawingPalette(), selectedId: selectedDrawingId ?? null, activeHandle: selectedDrawingHandle,
      bars: slotBars, duration: 60, tick: priceTick });
    if (containerRef.current) containerRef.current.dataset.agentUserDrawings = String(viewDrawings.length);
  }, [viewDrawings, slotBars, resolvedTheme, selectedDrawingId, selectedDrawingHandle, priceTick]);

  // Theme and the two chart switches the 分时图 honors (crosshair, volume).
  useEffect(() => {
    const bundle = bundleRef.current;
    if (!bundle) return;
    const dark = resolvedTheme === "dark";
    const guide = dark ? "rgba(142,168,255,.55)" : "rgba(47,91,255,.45)";
    const labelBackground = dark ? "#24335f" : "#2f5bff";
    const line = settings.crosshair
      ? { visible: true, color: guide, width: 1 as const, style: 0 as const, labelVisible: true, labelBackgroundColor: labelBackground }
      : { visible: false, labelVisible: false };
    bundle.chart.applyOptions({
      layout: { background: { type: ColorType.Solid, color: dark ? "#0b0d10" : "#ffffff" }, textColor: dark ? "#838882" : "#656a64" },
      grid: { horzLines: { color: dark ? "rgba(148,163,184,.07)" : "rgba(15,23,42,.06)" } },
      crosshair: { vertLine: line, horzLine: line }
    });
    bundle.price.applyOptions({ color: token("--accent", dark ? "#8ea8ff" : "#2f5bff") });
    bundle.average.applyOptions({ color: token("--chart-average", "#a16207") });
    bundle.volume.applyOptions({ visible: settings.volume });
  }, [resolvedTheme, settings.crosshair, settings.volume]);

  useEffect(() => {
    const bundle = bundleRef.current;
    if (!bundle) return;
    const priceFormat = priceTick && Number.isFinite(priceTick) && priceTick > 0
      ? { type: "price" as const, precision: decimalsForTick(priceTick), minMove: priceTick }
      : undefined;
    for (const series of [bundle.price, bundle.average]) series.applyOptions(priceFormat ? { priceFormat } : {});
  }, [priceTick]);

  // Data: the three series share the slot axis; a fresh trading day starts the line over and
  // spans the day's whole slot table.
  useEffect(() => {
    const bundle = bundleRef.current;
    if (!bundle) return;
    const point = (item: { time: number; value?: number }) =>
      item.value === undefined ? { time: item.time as UTCTimestamp } : { time: item.time as UTCTimestamp, value: item.value };
    bundle.price.setData(data.price.map(point));
    bundle.average.setData(data.average.map(point));
    const tone = directionTone(.28);
    bundle.volume.setData(data.volume.map(item => ({ time: item.time as UTCTimestamp, value: item.value, color: item.up ? tone.up : tone.down })));
    if (fittedRef.current !== fitKey) {
      fittedRef.current = fitKey;
      setReadout(null);
      // fitContent anchors on the newest bar and treats the trailing 未到分钟 as off-screen
      // margin, cutting the day sessions away. The 分时 axis spans the whole slot table instead.
      // No slots (no minute bars yet): nothing to span — an inverted range would throw (#739).
      if (data.slots.length) bundle.chart.timeScale().setVisibleLogicalRange({ from: 0, to: data.slots.length - 1 });
    }
  }, [data, fitKey]);

  // 昨结价: one dashed reference; its absence (older Runtime, no quote yet) just hides it.
  useEffect(() => {
    const bundle = bundleRef.current;
    if (!bundle) return;
    if (bundle.settlementLine) { bundle.price.removePriceLine(bundle.settlementLine); bundle.settlementLine = undefined; }
    if (preSettlement != null && Number.isFinite(preSettlement)) {
      bundle.settlementLine = bundle.price.createPriceLine({
        price: preSettlement,
        color: token("--fdw-faint", "#838882"),
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: "昨结"
      });
    }
  }, [preSettlement, data.tradingDay, resolvedTheme]);

  const shown = readout ?? data.latest;
  const decimals = decimalsForTick(priceTick);
  const changePercent = shown && preSettlement != null && Number.isFinite(preSettlement) && preSettlement !== 0
    ? (shown.price - preSettlement) / preSettlement * 100
    : null;

  return (
    <div className={`fdw-trading-charts fdw-timeshare ${resolvedTheme === "dark" ? "dark" : "light"}`}>
      <section className="fdw-chart-pane" data-region="chart.timeshare">
        <div className="fdw-chart-pane-label">
          <strong>分时</strong>
          {shown ? <span className="fdw-timeshare-readout">
            <i className="fdw-timeshare-swatch fdw-timeshare-price" />{shown.clock}
            <span>{shown.price.toFixed(decimals)}</span>
            <i className="fdw-timeshare-swatch fdw-timeshare-average" />均价 {shown.average !== undefined ? shown.average.toFixed(decimals) : "—"}
            <span>量 {formatVolume(shown.volume)}</span>
            {changePercent !== null ? <span className={changePercent > 0 ? "positive" : changePercent < 0 ? "negative" : ""}>
              {changePercent > 0 ? "+" : ""}{changePercent.toFixed(2)}%
            </span> : null}
          </span> : null}
          {helpAvailable && <button type="button" aria-label="图表说明" onClick={onHelp ?? (() => openAppHelp("chart"))}>?</button>}
        </div>
        <div ref={containerRef} className="fdw-chart-canvas" aria-label="分时图" />
      </section>
    </div>
  );
}

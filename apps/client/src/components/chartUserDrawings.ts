import type { IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, SeriesAttachedParameter, SeriesType, Time, UTCTimestamp } from "lightweight-charts";
import {
  DRAWING_COLOR_TOKENS, anchorClock, anchorsOf, levelPrices, measureText, priceLabel, resolveStyle, toolSpec,
  type Anchor, type ChartDrawing, type DrawingColor, type DrawingKind, type ResolvedStyle
} from "../chartDrawingModel";
import { labelInk } from "./chartAnnotationsPrimitive";

type Target = Parameters<IPrimitivePaneRenderer["draw"]>[0];
interface Bar { time: UTCTimestamp }
type Rect = { left: number; top: number; right: number; bottom: number };
type Point = [number, number];

/** Where a time / price sits on the pane (media pixels), and the pane's size. */
export interface DrawingMapper { x(time: number): number | null; y(price: number): number | null; width: number; height: number }
/** Theme colours the canvas cannot read as CSS variables. */
export interface DrawingPalette { color(color: DrawingColor | undefined): string; up: string; down: string; panel: string;
  /** --price-alert: the 提醒 tag on a line an alert watches. */
  alert?: string }
/** A placement in progress: the tool, the clicks so far and the cursor (the rubber band). */
export interface DrawingDraft { kind: DrawingKind; anchors: Anchor[]; cursor?: Anchor | null }
export interface DrawingHit { drawing: ChartDrawing; handle: number | null }

/**
 * 绘制原语 (画线第二版): one drawing as plain shapes in pane pixels — the canvas draws them and the
 * hit test reads the same list, so a click lands on exactly what is on screen. Pure (no canvas), so
 * the phone chart can reuse it with its own mapper.
 */
export type Prim =
  | { type: "line"; a: Point; b: Point; dash?: boolean }
  | { type: "poly"; points: Point[]; closed: boolean; fill: boolean }
  | { type: "rect"; rect: Rect; fill: boolean; tone?: "up" | "down" }
  | { type: "band"; top: number; bottom: number }
  | { type: "ellipse"; cx: number; cy: number; rx: number; ry: number }
  | { type: "arrowhead"; tip: Point; from: Point }
  | { type: "text"; at: Point; text: string; hit: Rect };
export interface DrawingLabel { text: string; at: Point; align: "left" | "right"; boxed: boolean }
export interface DrawingGeometry { prims: Prim[]; labels: DrawingLabel[]; handles: Point[] }

const FAR = 100_000;
const TEXT_FONT = "13px system-ui, sans-serif";
const LABEL_FONT = "12px system-ui, sans-serif";

/** The segment a→b stretched past a (`start`) and/or past b (`end`) far beyond the pane. */
function extend(a: Point, b: Point, start: boolean, end: boolean): [Point, Point] {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const length = Math.hypot(dx, dy);
  if (length < 0.5) return [a, b];
  const ux = dx / length * FAR, uy = dy / length * FAR;
  return [start ? [a[0] - ux, a[1] - uy] : a, end ? [b[0] + ux, b[1] + uy] : b];
}

/** Left / right extension by x (a ray extends past its second point whichever way it points). */
function extendByStyle(kind: DrawingKind, a: Point, b: Point, style: ResolvedStyle): [Point, Point] {
  if (kind === "ray") return extend(a, b, style.extendLeft, style.extendRight);
  const leftFirst = a[0] <= b[0];
  return extend(a, b, leftFirst ? style.extendLeft : style.extendRight, leftFirst ? style.extendRight : style.extendLeft);
}

function horizontal(x1: number, x2: number, y: number, style: ResolvedStyle, width: number): [Point, Point] {
  const left = Math.min(x1, x2), right = Math.max(x1, x2);
  return [[style.extendLeft ? 0 : left, y], [style.extendRight ? width : right, y]];
}

/** The shapes, labels and anchor handles of one drawing at `anchors` (its own, or a drag's). */
export function drawingGeometry(drawing: Pick<ChartDrawing, "kind" | "points" | "label" | "style">, anchors: Anchor[], map: DrawingMapper,
  options: { tick?: number | null; bars?: ReadonlyArray<{ time: number }> } = {}): DrawingGeometry | null {
  const kind = drawing.kind;
  const style = resolveStyle(drawing);
  const { width, height } = map;
  const tick = options.tick;
  const priceText = (price: number) => priceLabel(price, tick);
  const pts: Point[] = [];
  for (const anchor of anchors) {
    const x = kind === "hline" || kind === "zone" ? width / 2 : map.x(anchor.t);
    const y = kind === "vline" ? height / 2 : map.y(anchor.p);
    if (x === null || y === null) return null;
    pts.push([x, y]);
  }
  if (pts.length < Math.min(toolSpec(kind).min, anchors.length) || !pts.length) return null;
  const prims: Prim[] = [], labels: DrawingLabel[] = [];
  const label = drawing.label;
  const priced = (price: number) => style.showPrice ? (label || priceText(price)) : label;
  const handles = pts;
  switch (kind) {
    case "hline": {
      const y = pts[0][1];
      prims.push({ type: "line", a: [0, y], b: [width, y] });
      const text = priced(anchors[0].p);
      if (text) labels.push({ text, at: [8, y], align: "left", boxed: true });
      break;
    }
    case "zone": {
      const top = Math.min(pts[0][1], pts[1][1]), bottom = Math.max(pts[0][1], pts[1][1]);
      prims.push({ type: "band", top, bottom });
      const low = Math.min(anchors[0].p, anchors[1].p), high = Math.max(anchors[0].p, anchors[1].p);
      const text = style.showPrice ? (label || `${priceText(low)}–${priceText(high)}`) : label;
      if (text) labels.push({ text, at: [8, top + (bottom - top) / 2], align: "left", boxed: true });
      break;
    }
    case "hray": {
      const [a, b] = horizontal(pts[0][0], width, pts[0][1], { ...style, extendRight: true }, width);
      prims.push({ type: "line", a: style.extendLeft ? [0, a[1]] : a, b });
      const text = priced(anchors[0].p);
      if (text) labels.push({ text, at: [pts[0][0] + 8, pts[0][1]], align: "left", boxed: true });
      break;
    }
    case "vline": {
      const x = pts[0][0];
      prims.push({ type: "line", a: [x, 0], b: [x, height] });
      const text = style.showPrice ? (label || anchorClock(anchors[0].t)) : label;
      if (text) labels.push({ text, at: [x + 6, height - 16], align: "left", boxed: true });
      break;
    }
    case "trendline": case "ray": case "extended": {
      if (pts.length < 2) break;
      const [a, b] = extendByStyle(kind, pts[0], pts[1], style);
      prims.push({ type: "line", a, b });
      const end = pts[1];
      const text = priced(anchors[1].p);
      if (text && end[0] >= -4 && end[0] <= width + 4) {
        const right = end[0] > width - 120;
        labels.push({ text, at: [right ? end[0] - 8 : end[0] + 8, end[1]], align: right ? "right" : "left", boxed: true });
      }
      break;
    }
    case "channel": {
      if (pts.length < 2) break;
      const [a, b] = pts;
      let offset = 0;
      if (pts.length >= 3) {
        const dx = b[0] - a[0];
        const along = dx === 0 ? a[1] : a[1] + (b[1] - a[1]) * (pts[2][0] - a[0]) / dx;
        offset = pts[2][1] - along;
      }
      const a2: Point = [a[0], a[1] + offset], b2: Point = [b[0], b[1] + offset];
      prims.push({ type: "poly", points: [a, b, b2, a2], closed: true, fill: true });
      const [e1, e2] = extendByStyle(kind, a, b, style);
      const [f1, f2] = extendByStyle(kind, a2, b2, style);
      prims.push({ type: "line", a: e1, b: e2 }, { type: "line", a: f1, b: f2 });
      if (pts.length >= 3) {
        const [m1, m2] = extendByStyle(kind, [a[0], a[1] + offset / 2], [b[0], b[1] + offset / 2], style);
        prims.push({ type: "line", a: m1, b: m2, dash: true });
      }
      if (label) labels.push({ text: label, at: [b[0] + 8, b[1]], align: "left", boxed: true });
      break;
    }
    case "polyline": {
      prims.push({ type: "poly", points: pts, closed: false, fill: false });
      if (label) labels.push({ text: label, at: [pts.at(-1)![0] + 8, pts.at(-1)![1]], align: "left", boxed: true });
      break;
    }
    case "triangle": {
      prims.push({ type: "poly", points: pts, closed: pts.length >= 3, fill: pts.length >= 3 });
      if (label) labels.push({ text: label, at: [Math.max(...pts.map(p => p[0])) + 8, pts[0][1]], align: "left", boxed: true });
      break;
    }
    case "rect": case "rect_zone": case "measure": {
      if (pts.length < 2) break;
      const left = Math.min(pts[0][0], pts[1][0]), right = Math.max(pts[0][0], pts[1][0]);
      const top = Math.min(pts[0][1], pts[1][1]), bottom = Math.max(pts[0][1], pts[1][1]);
      if (kind === "measure") {
        const up = anchors[1].p >= anchors[0].p;
        prims.push({ type: "rect", rect: { left, top, right, bottom }, fill: true, tone: up ? "up" : "down" });
        const midX = (left + right) / 2;
        prims.push({ type: "line", a: [midX, pts[0][1]], b: [midX, pts[1][1]] }, { type: "arrowhead", tip: [midX, pts[1][1]], from: [midX, pts[0][1]] });
        labels.push({ text: measureText(anchors, options.bars ?? [], tick), at: [midX - 40, (up ? top : bottom) + (up ? -14 : 14)], align: "left", boxed: true });
        break;
      }
      const span = kind === "rect_zone"
        ? { left: style.extendLeft ? 0 : left, right: style.extendRight ? width : right }
        : { left, right };
      prims.push({ type: "rect", rect: { left: span.left, top, right: span.right, bottom }, fill: true });
      if (kind === "rect_zone" && style.showPrice) {
        const high = Math.max(anchors[0].p, anchors[1].p), low = Math.min(anchors[0].p, anchors[1].p);
        labels.push({ text: priceText(high), at: [span.right - 8, top], align: "right", boxed: true },
          { text: priceText(low), at: [span.right - 8, bottom], align: "right", boxed: true });
      }
      if (label) labels.push({ text: label, at: [span.left + 6, top + 12], align: "left", boxed: false });
      break;
    }
    case "ellipse": {
      if (pts.length < 2) break;
      const cx = (pts[0][0] + pts[1][0]) / 2, cy = (pts[0][1] + pts[1][1]) / 2;
      prims.push({ type: "ellipse", cx, cy, rx: Math.abs(pts[1][0] - pts[0][0]) / 2, ry: Math.abs(pts[1][1] - pts[0][1]) / 2 });
      if (label) labels.push({ text: label, at: [cx, cy], align: "left", boxed: false });
      break;
    }
    case "fib": case "percent": case "gann": {
      if (pts.length < 2) break;
      prims.push({ type: "line", a: pts[0], b: pts[1], dash: true });
      for (const level of levelPrices(drawing, anchors)) {
        const y = map.y(level.price);
        if (y === null) continue;
        const [a, b] = horizontal(pts[0][0], pts[1][0], y, style, width);
        prims.push({ type: "line", a, b, dash: kind === "gann" && level.ratio !== 0.5 && level.ratio !== 0 && level.ratio !== 1 });
        const text = style.showPrice ? `${level.text}  ${priceText(level.price)}` : level.text;
        labels.push({ text, at: [a[0] + 4, y - 9], align: "left", boxed: false });
      }
      if (label) labels.push({ text: label, at: [pts[1][0] + 8, pts[1][1]], align: "left", boxed: true });
      break;
    }
    case "text": {
      const text = label || "文字";
      const boxWidth = Math.max(24, [...text].length * 13 + 8);
      const [x, y] = pts[0];
      prims.push({ type: "text", at: [x, y], text, hit: { left: x - 4, top: y - 11, right: x + boxWidth, bottom: y + 11 } });
      break;
    }
    case "arrow": {
      if (pts.length < 2) break;
      prims.push({ type: "line", a: pts[0], b: pts[1] }, { type: "arrowhead", tip: pts[1], from: pts[0] });
      if (label) labels.push({ text: label, at: [pts[0][0] - 8, pts[0][1]], align: "right", boxed: true });
      break;
    }
    case "price_label": {
      const [x, y] = pts[0];
      const text = label ? `${label} ${priceText(anchors[0].p)}` : priceText(anchors[0].p);
      const boxWidth = [...text].length * 8 + 14;
      prims.push({ type: "text", at: [x, y], text, hit: { left: x, top: y - 11, right: x + 10 + boxWidth, bottom: y + 11 } });
      break;
    }
  }
  return { prims, labels, handles };
}

function segmentDistance(px: number, py: number, a: Point, b: Point): number {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const lengthSquared = dx * dx + dy * dy;
  const t = lengthSquared ? Math.max(0, Math.min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / lengthSquared)) : 0;
  return Math.hypot(a[0] + t * dx - px, a[1] + t * dy - py);
}

function insidePolygon(px: number, py: number, points: Point[]): boolean {
  let inside = false;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const [xi, yi] = points[i], [xj, yj] = points[j];
    if ((yi > py) !== (yj > py) && px < (xj - xi) * (py - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** Whether a media point touches the geometry (6 px around lines, inside filled shapes). */
export function geometryHit(geometry: DrawingGeometry, x: number, y: number, tolerance = 6): boolean {
  for (const prim of geometry.prims) {
    if (prim.type === "line" && segmentDistance(x, y, prim.a, prim.b) <= tolerance) return true;
    if (prim.type === "band" && y >= prim.top - tolerance && y <= prim.bottom + tolerance) return true;
    if (prim.type === "rect") {
      const r = prim.rect;
      if (x >= r.left - tolerance && x <= r.right + tolerance && y >= r.top - tolerance && y <= r.bottom + tolerance) return true;
    }
    if (prim.type === "poly") {
      const edges = prim.closed ? [...prim.points, prim.points[0]] : prim.points;
      for (let i = 1; i < edges.length; i += 1) if (segmentDistance(x, y, edges[i - 1], edges[i]) <= tolerance) return true;
      if (prim.fill && prim.points.length >= 3 && insidePolygon(x, y, prim.points)) return true;
    }
    if (prim.type === "ellipse") {
      const rx = Math.max(prim.rx, 1) + tolerance, ry = Math.max(prim.ry, 1) + tolerance;
      if (((x - prim.cx) / rx) ** 2 + ((y - prim.cy) / ry) ** 2 <= 1) return true;
    }
    if (prim.type === "text" && x >= prim.hit.left && x <= prim.hit.right && y >= prim.hit.top && y <= prim.hit.bottom) return true;
  }
  return false;
}

/** The anchor handle under a media point (within 7 px, plus a finger's extra reach), or null. */
export function handleAt(geometry: DrawingGeometry, x: number, y: number, reach = 0): number | null {
  const index = geometry.handles.findIndex(([hx, hy]) => Math.hypot(hx - x, hy - y) <= 7 + reach);
  return index < 0 ? null : index;
}

const DASHES: Record<string, number[]> = { solid: [], dashed: [6, 4], dotted: [1.5, 3] };

/** The drawing colours of the current theme: a preset reads its token (theme.css pairs them for light
 *  and dark), a custom colour is itself; the measure tint follows 涨跌颜色. */
export function drawingPalette(): DrawingPalette {
  const tokens = typeof getComputedStyle === "function" ? getComputedStyle(document.documentElement) : undefined;
  const read = (name: string, fallback: string) => tokens?.getPropertyValue(name).trim() || fallback;
  const fallbacks: Record<string, string> = { "--user-drawing": "#a21caf", "--draw-amber": "#b45309", "--draw-red": "#d92d20",
    "--draw-green": "#15803d", "--draw-blue": "#2563eb", "--ink": "#111310" };
  const cache = new Map<string, string>();
  return {
    color: color => {
      if (color && color.startsWith("#")) return color;
      const token = DRAWING_COLOR_TOKENS[color ?? "user"] ?? "--user-drawing";
      if (!cache.has(token)) cache.set(token, read(token, fallbacks[token] ?? "#a21caf"));
      return cache.get(token)!;
    },
    up: read("--market-up", "#cf291e"), down: read("--market-down", "#0a7a49"), panel: read("--panel", "#ffffff"),
    alert: read("--price-alert", "#5b21b6"),
  };
}

/**
 * The user's own drawings on one chart (用户画线; 画线第二版 2026-10-09): every kind with its own
 * colour, width, dash and extension, apart from every AI layer. Times are the same seconds the bars
 * carry (synthetic slot times on the 分时图 — the caller maps real↔synthetic), interpolated on the
 * logical scale like the AI layer. The selected one is drawn thicker with its anchor handles; a line
 * being dragged draws at its `moving` anchors; the draft previews a placement under the cursor.
 */
export class UserDrawingsPrimitive implements ISeriesPrimitive<Time> {
  private chart?: IChartApi;
  private series?: ISeriesApi<SeriesType>;
  private requestUpdate?: () => void;
  private items: ChartDrawing[] = [];
  private palette: DrawingPalette = { color: () => "#c026d3", up: "#cf291e", down: "#0a7a49", panel: "#ffffff" };
  private selectedId: string | null = null;
  /** The selected drawing's chosen anchor (the phone's ±1 跳 moves only it), drawn filled. */
  private activeHandle: number | null = null;
  private draft: DrawingDraft | null = null;
  private moving: { id: string; anchors: Anchor[] } | null = null;
  private bars: Bar[] = [];
  private duration = 60;
  private tick: number | null | undefined;
  private size = { width: 0, height: 0 };
  private readonly view: IPrimitivePaneView = { zOrder: () => "top", renderer: () => this.renderer };
  private readonly renderer: IPrimitivePaneRenderer = { draw: target => this.draw(target) };

  attached(param: SeriesAttachedParameter<Time>) {
    this.chart = param.chart;
    this.series = param.series;
    this.requestUpdate = param.requestUpdate;
  }
  detached() { this.chart = undefined; this.series = undefined; this.requestUpdate = undefined; }
  paneViews() { return [this.view]; }

  /** The layer's data; `draft` is left as it is unless given (the pointer interaction owns it). */
  update(state: { items: ChartDrawing[]; palette: DrawingPalette; selectedId: string | null; activeHandle?: number | null; draft?: DrawingDraft | null;
    bars: Bar[]; duration: number; tick?: number | null }) {
    this.items = state.items;
    this.palette = state.palette;
    this.selectedId = state.selectedId;
    this.activeHandle = state.activeHandle ?? null;
    if (state.draft !== undefined) this.draft = state.draft;
    this.bars = state.bars;
    this.duration = state.duration;
    this.tick = state.tick;
    this.requestUpdate?.();
  }

  /** A drag in progress (anchors in view time) or none. */
  setMoving(moving: { id: string; anchors: Anchor[] } | null) { this.moving = moving; this.requestUpdate?.(); }
  setDraft(draft: DrawingDraft | null) { this.draft = draft; this.requestUpdate?.(); }

  /** Logical coordinate of a time in seconds, interpolated between bars and extrapolated past the ends. */
  timeToX(time: number): number | null {
    const chart = this.chart;
    if (!chart || !this.bars.length || !Number.isFinite(time)) return null;
    let low = 0, high = this.bars.length - 1;
    while (low < high) { const mid = (low + high + 1) >> 1; if (this.bars[mid].time <= time) low = mid; else high = mid - 1; }
    const index = this.bars[low].time <= time ? low : 0;
    const logical = index + (time - this.bars[index].time) / this.duration;
    const x = chart.timeScale().logicalToCoordinate(logical as never);
    return x === null ? null : x;
  }
  priceToY(price: number): number | null {
    if (!Number.isFinite(price)) return null;
    const y = this.series?.priceToCoordinate(price);
    return y === null || y === undefined ? null : y;
  }

  private mapper(): DrawingMapper {
    // Before the first paint (or without a measured pane) a full-width line still hit-tests along its y.
    const width = this.size.width || this.chart?.paneSize?.(0)?.width || FAR;
    const height = this.size.height || this.chart?.paneSize?.(0)?.height || FAR;
    return { x: time => this.timeToX(time), y: price => this.priceToY(price), width, height };
  }

  private anchorsFor(item: ChartDrawing): Anchor[] {
    return this.moving?.id === item.id ? this.moving.anchors : anchorsOf(item);
  }

  /** The geometry of one drawing as it is on screen now (null when off the loaded data). */
  geometryOf(item: ChartDrawing): DrawingGeometry | null {
    return drawingGeometry(item, this.anchorsFor(item), this.mapper(), { tick: this.tick, bars: this.bars });
  }

  /** The drawing under a media-space point, topmost (last drawn) first; on the selected one an anchor
   *  handle wins. Not the library's own `hitTest` — the click-select / drag / right-click lookup. `reach` widens
   *  both for a finger. */
  hitTestDrawing(x: number, y: number, reach = 0): DrawingHit | null {
    const selected = this.items.find(item => item.id === this.selectedId);
    if (selected) {
      const geometry = this.geometryOf(selected);
      const handle = geometry ? handleAt(geometry, x, y, reach) : null;
      if (geometry && handle !== null) return { drawing: selected, handle };
    }
    for (const item of [...this.items].reverse()) {
      const geometry = this.geometryOf(item);
      if (geometry && geometryHit(geometry, x, y, 6 + reach + resolveStyle(item).width / 2)) return { drawing: item, handle: null };
    }
    return null;
  }

  private draw(target: Target) {
    if (!this.items.length && !this.draft) return;
    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      this.size = { width: mediaSize.width, height: mediaSize.height };
      const width = mediaSize.width, height = mediaSize.height;
      const map = this.mapper();
      // The pane's own header strip sits over the top-left of the canvas: keep labels off it.
      const placed: Rect[] = [{ left: 0, top: 0, right: width, bottom: 24 }];
      context.save();
      context.textBaseline = "middle";
      const overlaps = (a: Rect) => placed.some(b => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top);
      const boxLabel = (text: string, x: number, y: number, align: "left" | "right", color: string) => {
        context.font = LABEL_FONT;
        const padding = 4, boxWidth = context.measureText(text).width + padding * 2, boxHeight = 18;
        const raw = align === "right" ? x - boxWidth : x;
        const left = Math.max(2, Math.min(width - boxWidth - 2, raw));
        let rect: Rect | undefined;
        for (const offset of [0, -22, 22, -44, 44]) {
          const top = Math.max(2, Math.min(height - boxHeight - 2, y - boxHeight / 2 + offset));
          const candidate = { left, top, right: left + boxWidth, bottom: top + boxHeight };
          if (!overlaps(candidate)) { rect = candidate; break; }
        }
        if (!rect) return;
        placed.push(rect);
        context.fillStyle = color; context.globalAlpha = 0.92;
        context.beginPath(); context.roundRect(rect.left, rect.top, boxWidth, boxHeight, 4); context.fill();
        context.globalAlpha = 1; context.fillStyle = labelInk(color); context.textAlign = "left";
        context.fillText(text, rect.left + padding, rect.top + boxHeight / 2);
      };
      const paint = (item: Pick<ChartDrawing, "kind" | "points" | "label" | "style" | "alerted">, anchors: Anchor[], selected: boolean, ghost: boolean) => {
        const geometry = drawingGeometry(item, anchors, map, { tick: this.tick, bars: this.bars });
        if (!geometry) return;
        const style = resolveStyle(item);
        const color = this.palette.color(style.color);
        context.strokeStyle = color; context.fillStyle = color;
        context.lineWidth = style.width + (selected ? 1 : 0);
        context.globalAlpha = ghost ? 0.75 : 1;
        const dash = (DASHES[style.dash] ?? []).map(value => value * Math.max(1, style.width * 0.75));
        for (const prim of geometry.prims) {
          context.setLineDash(prim.type === "line" && prim.dash ? [4, 4] : dash);
          if (prim.type === "line") {
            context.beginPath(); context.moveTo(...prim.a); context.lineTo(...prim.b); context.stroke();
          } else if (prim.type === "band") {
            context.save(); context.globalAlpha *= 0.12; context.fillRect(0, prim.top, width, prim.bottom - prim.top); context.restore();
            context.beginPath(); context.moveTo(0, prim.top); context.lineTo(width, prim.top);
            context.moveTo(0, prim.bottom); context.lineTo(width, prim.bottom); context.stroke();
          } else if (prim.type === "rect") {
            const r = prim.rect;
            const tone = prim.tone === "up" ? this.palette.up : prim.tone === "down" ? this.palette.down : color;
            context.save(); context.fillStyle = tone; context.globalAlpha *= prim.tone ? 0.16 : 0.12;
            context.fillRect(r.left, r.top, r.right - r.left, r.bottom - r.top); context.restore();
            context.save(); if (prim.tone) context.strokeStyle = tone;
            context.strokeRect(r.left, r.top, r.right - r.left, r.bottom - r.top); context.restore();
          } else if (prim.type === "poly") {
            context.beginPath(); context.moveTo(...prim.points[0]);
            for (const point of prim.points.slice(1)) context.lineTo(...point);
            if (prim.closed) context.closePath();
            if (prim.fill) { context.save(); context.globalAlpha *= 0.1; context.fill(); context.restore(); }
            context.stroke();
          } else if (prim.type === "ellipse") {
            context.beginPath(); context.ellipse(prim.cx, prim.cy, Math.max(prim.rx, 0.5), Math.max(prim.ry, 0.5), 0, 0, Math.PI * 2);
            context.save(); context.globalAlpha *= 0.1; context.fill(); context.restore();
            context.stroke();
          } else if (prim.type === "arrowhead") {
            const angle = Math.atan2(prim.tip[1] - prim.from[1], prim.tip[0] - prim.from[0]);
            const size = 8 + style.width * 2;
            context.setLineDash([]);
            context.beginPath(); context.moveTo(...prim.tip);
            context.lineTo(prim.tip[0] - size * Math.cos(angle - 0.45), prim.tip[1] - size * Math.sin(angle - 0.45));
            context.lineTo(prim.tip[0] - size * Math.cos(angle + 0.45), prim.tip[1] - size * Math.sin(angle + 0.45));
            context.closePath(); context.fill();
          } else if (prim.type === "text") {
            context.setLineDash([]);
            if (item.kind === "price_label") {
              const r = prim.hit;
              context.beginPath(); context.moveTo(prim.at[0], prim.at[1]); context.lineTo(r.left + 8, r.top); context.lineTo(r.right, r.top);
              context.lineTo(r.right, r.bottom); context.lineTo(r.left + 8, r.bottom); context.closePath(); context.fill();
              context.font = LABEL_FONT; context.fillStyle = labelInk(color); context.textAlign = "left";
              context.fillText(prim.text, r.left + 12, prim.at[1]);
              placed.push(r);
            } else {
              context.font = TEXT_FONT; context.textAlign = "left";
              context.fillText(prim.text, prim.at[0], prim.at[1]);
              if (selected) { context.save(); context.setLineDash([3, 3]); context.lineWidth = 1;
                context.strokeRect(prim.hit.left, prim.hit.top, prim.hit.right - prim.hit.left, prim.hit.bottom - prim.hit.top); context.restore(); }
            }
          }
        }
        context.setLineDash([]);
        for (const entry of geometry.labels) {
          if (entry.boxed) boxLabel(entry.text, entry.at[0], entry.at[1], entry.align, color);
          else if (entry.at[1] > 30) {   // a plain label stays off the pane's own header strip
            context.font = LABEL_FONT; context.fillStyle = color; context.textAlign = entry.align; context.fillText(entry.text, entry.at[0], entry.at[1]);
          }
        }
        // 碰到这条线就提醒: a line an alert watches wears a 提醒 tag in the alert colour by its last point.
        const end = geometry.handles.at(-1);
        if (item.alerted && end) boxLabel("提醒", end[0] + 8, end[1], "left", this.palette.alert ?? color);
        if (selected) {
          for (const [index, [hx, hy]] of geometry.handles.entries()) {
            if (hx < -6 || hx > width + 6) continue;
            const active = index === this.activeHandle;
            context.beginPath(); context.arc(hx, hy, active ? 6 : 4.5, 0, Math.PI * 2);
            context.fillStyle = active ? color : this.palette.panel; context.fill();
            context.lineWidth = 2; context.strokeStyle = color; context.stroke();
          }
        } else if (item.kind === "trendline" && !ghost) {
          // The first version's endpoint dots stay on a trendline.
          for (const [hx, hy] of geometry.handles) {
            if (hx < -4 || hx > width + 4) continue;
            context.beginPath(); context.arc(hx, hy, 3, 0, Math.PI * 2); context.fill();
          }
        }
        context.globalAlpha = 1;
      };
      for (const item of this.items) paint(item, this.anchorsFor(item), item.id === this.selectedId, false);
      // The draft: the placement so far plus the cursor, so the user sees the line before the last click.
      const draft = this.draft;
      if (draft) {
        const spec = toolSpec(draft.kind);
        const anchors = draft.cursor ? [...draft.anchors, draft.cursor] : draft.anchors;
        const ghost = (kind: DrawingKind, points: Anchor[]) => paint({ kind, points: {}, label: "", style: {} }, points, false, true);
        if (spec.min === 1 && anchors.length) ghost(draft.kind, anchors.slice(-1));
        else if (draft.anchors.length && anchors.length >= Math.min(spec.min, 2)) {
          ghost(anchors.length >= spec.min || draft.kind === "polyline" ? draft.kind : draft.kind === "channel" ? "trendline" : "polyline", anchors);
        } else if (draft.anchors.length) {
          const x = draft.kind === "zone" ? width / 2 : map.x(anchors[0].t), y = map.y(anchors[0].p);
          if (x !== null && y !== null) {
            context.fillStyle = this.palette.color(undefined);
            context.beginPath(); context.arc(x, y, 3.5, 0, Math.PI * 2); context.fill();
          }
        }
      }
      context.restore();
    });
  }
}

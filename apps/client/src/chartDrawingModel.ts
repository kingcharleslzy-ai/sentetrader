/**
 * 用户画线的数据模型 (画线第二版, 2026-10-09) — pure, no React and no chart library, so the desktop
 * charts and the phone's chart (下一期) share one source for the kinds, their anchors, the style
 * fields and the placement rules. It mirrors the Runtime's `chart_drawings.py`:
 *
 * - The three first-version kinds keep their stored shapes (hline {price}, trendline {t1,p1,t2,p2},
 *   zone {low,high}); every other kind stores `{anchors: [{t, p}, …]}` (fib / percent: `levels`).
 *   `anchorsOf` reads any of them as anchors and `pointsFor` writes anchors back in the kind's shape.
 * - Style keys are all optional: an absent key is the kind's own look (`resolveStyle`), so a line
 *   saved before the second version draws exactly as it did.
 * - Times are bar timestamps in seconds; prices are rounded to the contract's tick when placed.
 */

export type DrawingKind =
  | "trendline" | "ray" | "extended" | "hline" | "hray" | "vline" | "channel" | "polyline"
  | "zone" | "rect_zone"
  | "fib" | "percent" | "gann"
  | "rect" | "ellipse" | "triangle"
  | "text" | "arrow" | "price_label"
  | "measure";
export type DrawingGroup = "lines" | "levels" | "fib" | "shapes" | "notes" | "measure";
export type DrawingDash = "solid" | "dashed" | "dotted";
/** A theme-tuned preset (the canvas reads its token in each theme) or a custom #rrggbb. */
export type DrawingColor = "user" | "amber" | "red" | "green" | "blue" | "ink" | string;

export interface DrawingStyle {
  color?: DrawingColor;
  width?: number;
  dash?: DrawingDash;
  extendLeft?: boolean;
  extendRight?: boolean;
  showPrice?: boolean;
  locked?: boolean;
  hidden?: boolean;
  /** false: shown only on `period` (the K 线 seconds it was drawn on, 0 = 分时). */
  allPeriods?: boolean;
  period?: number;
}

/** 存为这类线的默认样式: the per-tool keys a default carries (preferences chart.drawingDefaults; null = the
 *  tool's own look — a save writes every key, so an older default never outlives the new one). */
export type DrawingDefaultStyle = { [K in "color" | "width" | "dash" | "extendLeft" | "extendRight" | "showPrice" | "allPeriods"]?: DrawingStyle[K] | null };
export type DrawingDefaults = Partial<Record<DrawingKind, DrawingDefaultStyle | null>>;

export interface Anchor { t: number; p: number }
export interface DrawingPoints {
  price?: number; t1?: number; p1?: number; t2?: number; p2?: number; low?: number; high?: number;
  anchors?: Array<{ t: number; p?: number }>;
  levels?: number[];
}

export interface ChartDrawing {
  id: string;
  symbol: string;
  kind: DrawingKind;
  points: DrawingPoints;
  label: string;
  /** Absent on a first-version Runtime; every key optional. */
  style?: DrawingStyle;
  createdAt: string;
  updatedAt: string;
}

export interface DrawingToolSpec {
  kind: DrawingKind;
  label: string;
  group: DrawingGroup;
  /** Clicks needed (a polyline takes min…max and ends on a double click or Enter). */
  min: number;
  max: number;
  shortcut?: string;
  /** What to do on the chart while the tool is armed. */
  hint: string;
}

export const MAX_DRAWINGS_PER_SYMBOL = 200;
export const DRAWING_LABEL_MAX = 12;
export const DRAWING_TEXT_MAX = 60;
export const MAX_POLYLINE_POINTS = 20;

export const DRAWING_TOOLS: readonly DrawingToolSpec[] = [
  { kind: "trendline", label: "趋势线", group: "lines", min: 2, max: 2, shortcut: "Alt+T", hint: "点两下：起点、终点" },
  { kind: "ray", label: "射线", group: "lines", min: 2, max: 2, hint: "点两下：起点、方向" },
  { kind: "extended", label: "直线", group: "lines", min: 2, max: 2, hint: "点两下，直线两端延长" },
  { kind: "hline", label: "水平线", group: "lines", min: 1, max: 1, shortcut: "Alt+H", hint: "点一下定价位" },
  { kind: "hray", label: "水平射线", group: "lines", min: 1, max: 1, hint: "点一下，从这里向右" },
  { kind: "vline", label: "垂直线", group: "lines", min: 1, max: 1, shortcut: "Alt+V", hint: "点一下定时间" },
  { kind: "channel", label: "平行通道", group: "lines", min: 3, max: 3, hint: "点两下定一边，第三下定宽度" },
  { kind: "polyline", label: "折线", group: "lines", min: 2, max: MAX_POLYLINE_POINTS, hint: "逐点点击，双击或回车结束" },
  { kind: "zone", label: "价格区间", group: "levels", min: 2, max: 2, hint: "点两下：上沿、下沿" },
  { kind: "rect_zone", label: "矩形区间", group: "levels", min: 2, max: 2, hint: "点两下：对角两点" },
  { kind: "fib", label: "黄金分割", group: "fib", min: 2, max: 2, hint: "点两下：起点、终点" },
  { kind: "percent", label: "百分比线", group: "fib", min: 2, max: 2, hint: "点两下：0% 与 100%" },
  { kind: "gann", label: "江恩八线", group: "fib", min: 2, max: 2, hint: "点两下：低点与高点" },
  { kind: "rect", label: "矩形", group: "shapes", min: 2, max: 2, hint: "点两下：对角两点" },
  { kind: "ellipse", label: "圆或椭圆", group: "shapes", min: 2, max: 2, hint: "点两下：外框对角" },
  { kind: "triangle", label: "三角形", group: "shapes", min: 3, max: 3, hint: "点三下：三个顶点" },
  { kind: "text", label: "文字", group: "notes", min: 1, max: 1, hint: "点一下放文字" },
  { kind: "arrow", label: "箭头", group: "notes", min: 2, max: 2, hint: "点两下：起点、箭头" },
  { kind: "price_label", label: "价格标签", group: "notes", min: 1, max: 1, hint: "点一下标出价格" },
  { kind: "measure", label: "测量", group: "measure", min: 2, max: 2, hint: "点两下量价差、涨跌幅、K 线根数" },
];
export const DRAWING_GROUPS: ReadonlyArray<{ id: DrawingGroup; label: string }> = [
  { id: "lines", label: "线条" }, { id: "levels", label: "价位" }, { id: "fib", label: "分割" },
  { id: "shapes", label: "形状" }, { id: "notes", label: "标注" }, { id: "measure", label: "测量" },
];
/** The right-click 画线 submenu: eight common tools (more on the left toolbar). */
export const QUICK_DRAWING_TOOLS: readonly DrawingKind[] = ["trendline", "hline", "ray", "vline", "channel", "zone", "fib", "text"];
export const QUICK_TOOL_LABELS: Partial<Record<DrawingKind, string>> = { channel: "通道", zone: "区间" };

const SPEC_BY_KIND = new Map(DRAWING_TOOLS.map(spec => [spec.kind, spec]));
export function toolSpec(kind: DrawingKind): DrawingToolSpec {
  return SPEC_BY_KIND.get(kind) ?? DRAWING_TOOLS[0];
}
export function isDrawingKind(value: unknown): value is DrawingKind {
  return typeof value === "string" && SPEC_BY_KIND.has(value as DrawingKind);
}
export function isDrawingGroup(value: unknown): value is DrawingGroup {
  return DRAWING_GROUPS.some(group => group.id === value);
}

/** Kinds without times (they span the whole chart width). */
export const TIMELESS_KINDS: ReadonlySet<DrawingKind> = new Set(["hline", "zone"]);
/** Kinds whose line can be extended left / right (精确设置's 向左延长 / 向右延长). */
export const EXTENDABLE_KINDS: ReadonlySet<DrawingKind> = new Set(["trendline", "ray", "extended", "hray", "channel", "rect_zone", "fib", "percent", "gann"]);
/** Kinds that mark a price level (their right-click / style bar offer 设到价提醒). */
export const ALERTABLE_KINDS: ReadonlySet<DrawingKind> = new Set(["hline", "hray", "price_label", "trendline", "ray", "extended", "zone", "rect_zone"]);

export const DRAWING_COLOR_PRESETS: ReadonlyArray<{ color: DrawingColor; label: string }> = [
  { color: "amber", label: "琥珀" }, { color: "red", label: "红" }, { color: "green", label: "绿" },
  { color: "blue", label: "蓝" }, { color: "ink", label: "墨色" },
];
/** The CSS token each preset reads (theme.css owns the paired light/dark values). */
export const DRAWING_COLOR_TOKENS: Record<string, string> = {
  user: "--user-drawing", amber: "--draw-amber", red: "--draw-red", green: "--draw-green", blue: "--draw-blue", ink: "--ink",
};
export const DEFAULT_LEVELS: Partial<Record<DrawingKind, number[]>> = {
  fib: [0, 0.236, 0.382, 0.5, 0.618, 1],
  percent: [0, 0.25, 0.5, 0.75, 1],
};

/** The style a drawing draws with: its own keys over the kind's original look. */
export interface ResolvedStyle {
  color: DrawingColor;
  /** Line width in CSS pixels (the first version drew a trendline at 1.5). */
  width: number;
  dash: DrawingDash;
  extendLeft: boolean;
  extendRight: boolean;
  showPrice: boolean;
  locked: boolean;
  hidden: boolean;
}

const LINE_KINDS: ReadonlySet<DrawingKind> = new Set(["trendline", "ray", "extended", "hray", "vline", "channel", "polyline", "arrow"]);
const PRICED_KINDS: ReadonlySet<DrawingKind> = new Set(["hline", "zone", "trendline", "ray", "extended", "hray", "vline", "rect_zone", "fib", "percent", "gann", "price_label", "measure"]);

/** A kind's own line width (the first version drew a trendline at 1.5, a 水平线 at 1). */
export function defaultWidth(kind: DrawingKind): number {
  return kind === "hline" || !LINE_KINDS.has(kind) ? 1 : 1.5;
}

export function resolveStyle(drawing: Pick<ChartDrawing, "kind" | "style">): ResolvedStyle {
  const style = drawing.style ?? {};
  const kind = drawing.kind;
  return {
    color: style.color ?? "user",
    width: style.width ?? defaultWidth(kind),
    dash: style.dash ?? "solid",
    extendLeft: style.extendLeft ?? kind === "extended",
    extendRight: style.extendRight ?? (kind === "ray" || kind === "extended" || kind === "hray"),
    showPrice: style.showPrice ?? PRICED_KINDS.has(kind),
    locked: Boolean(style.locked),
    hidden: Boolean(style.hidden),
  };
}

/** The chart a drawing shows on: every period unless it was pinned to the one it was drawn on. */
export function visibleOnPeriod(drawing: Pick<ChartDrawing, "style">, period: number): boolean {
  const style = drawing.style;
  if (!style || style.allPeriods !== false || typeof style.period !== "number") return true;
  return style.period === period;
}

/** Any drawing's points as anchors (first-version shapes converted; t 0 = the kind has no time). */
export function anchorsOf(drawing: Pick<ChartDrawing, "kind" | "points">): Anchor[] {
  const points = drawing.points ?? {};
  if (drawing.kind === "hline") return [{ t: 0, p: Number(points.price) }];
  if (drawing.kind === "zone") return [{ t: 0, p: Number(points.high) }, { t: 0, p: Number(points.low) }];
  if (drawing.kind === "trendline") return [{ t: Number(points.t1), p: Number(points.p1) }, { t: Number(points.t2), p: Number(points.p2) }];
  return (points.anchors ?? []).map(anchor => ({ t: Number(anchor.t), p: anchor.p === undefined ? Number.NaN : Number(anchor.p) }));
}

/** Anchors written back in the kind's stored shape (levels kept from `base`). */
export function pointsFor(kind: DrawingKind, anchors: Anchor[], base?: DrawingPoints): DrawingPoints {
  if (kind === "hline") return { price: anchors[0].p };
  if (kind === "zone") {
    const [low, high] = [anchors[0].p, anchors[1].p].sort((a, b) => a - b);
    return { low, high };
  }
  if (kind === "trendline") return { t1: anchors[0].t, p1: anchors[0].p, t2: anchors[1].t, p2: anchors[1].p };
  const stored = anchors.map(anchor => kind === "vline" || !Number.isFinite(anchor.p) ? { t: anchor.t } : { t: anchor.t, p: anchor.p });
  return base?.levels ? { anchors: stored, levels: base.levels } : { anchors: stored };
}

/** The tick's decimals, capped at 8. */
export function tickDecimals(tick: number | null | undefined): number {
  if (!tick || !(tick > 0)) return 6;
  // toFixed, not String(): 日元 0.0000005 stringifies as "5e-7" and would read as 0 decimals.
  return Math.min(tick.toFixed(10).replace(/0+$/, "").split(".")[1]?.length ?? 0, 8);
}

/** A price on the contract's tick grid (no tick: six decimals). */
export function snapToTick(price: number, tick: number | null | undefined): number {
  if (!tick || !(tick > 0)) return Number(price.toFixed(6));
  return Number((Math.round(price / tick) * tick).toFixed(tickDecimals(tick)));
}

/** 磁吸: the bar's open / high / low / close nearest to the raw price. */
export function magnetPrice(raw: number, bar: { open: number; high: number; low: number; close: number } | undefined): number {
  if (!bar) return raw;
  return [bar.open, bar.high, bar.low, bar.close].reduce((best, value) => Math.abs(value - raw) < Math.abs(best - raw) ? value : best, bar.close);
}

/** One placement's points: every price on the tick grid, in the kind's stored shape. */
export function placementPoints(kind: DrawingKind, anchors: Anchor[], tick: number | null | undefined, levels?: number[]): DrawingPoints {
  const snapped = anchors.map(anchor => ({ t: Math.round(anchor.t), p: snapToTick(anchor.p, tick) }));
  return pointsFor(kind, snapped, levels ? { levels } : undefined);
}

/** Every anchor moved by the same time and price (a timeless kind only moves in price). */
export function shiftAnchors(kind: DrawingKind, anchors: Anchor[], dt: number, dp: number): Anchor[] {
  return anchors.map(anchor => ({ t: TIMELESS_KINDS.has(kind) ? anchor.t : anchor.t + dt, p: Number.isFinite(anchor.p) ? anchor.p + dp : anchor.p }));
}

/** The ratio → price pairs a 黄金分割 (counted from the second point back), 百分比线 or 江恩八线 draws. */
export function levelPrices(drawing: Pick<ChartDrawing, "kind" | "points">, anchors = anchorsOf(drawing)): Array<{ ratio: number; price: number; text: string }> {
  if (!["fib", "percent", "gann"].includes(drawing.kind) || anchors.length < 2) return [];
  const [start, end] = [anchors[0].p, anchors[1].p];
  if (drawing.kind === "gann") {
    return Array.from({ length: 9 }, (_, step) => ({ ratio: step / 8, price: start + (end - start) * step / 8, text: `${step}/8` }));
  }
  const ratios = drawing.points.levels ?? DEFAULT_LEVELS[drawing.kind] ?? [];
  return ratios.map(ratio => ({
    ratio,
    price: drawing.kind === "fib" ? end + (start - end) * ratio : start + (end - start) * ratio,
    text: `${Number((ratio * 100).toFixed(2))}%`,
  }));
}

/** A price printed for the drawing labels: on the tick's decimals, no trailing zeros. */
export function priceLabel(value: number, tick?: number | null): string {
  if (!Number.isFinite(value)) return "—";
  return String(Number(value.toFixed(tick ? tickDecimals(tick) : 2)));
}

/** The 测量 readout: price difference, change percent and bar count between two anchors. */
export function measureText(anchors: Anchor[], bars: ReadonlyArray<{ time: number }>, tick?: number | null): string {
  if (anchors.length < 2) return "";
  const [a, b] = anchors;
  const diff = b.p - a.p;
  const percent = a.p ? diff / a.p * 100 : 0;
  const sign = diff > 0 ? "+" : diff < 0 ? "−" : "";
  const from = Math.min(a.t, b.t), to = Math.max(a.t, b.t);
  const count = bars.filter(bar => bar.time > from && bar.time <= to).length;
  return `${sign}${priceLabel(Math.abs(diff), tick)}（${sign}${Math.abs(percent).toFixed(2)}%）· ${count} 根`;
}

const clockFormat = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
/** "10-08 14:05" in China time. */
export function anchorClock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  return clockFormat.format(new Date(seconds * 1000)).replace(/\//g, "-").replace(/\s+/, " ");
}

/** A drawing's menu header: "趋势线 · 10-08 14:05 → 10-09 10:30", "水平线 · 3070". */
export function drawingTitle(drawing: Pick<ChartDrawing, "kind" | "points" | "label">, tick?: number | null): string {
  const name = toolSpec(drawing.kind).label;
  const anchors = anchorsOf(drawing);
  if (drawing.kind === "hline" || drawing.kind === "hray" || drawing.kind === "price_label") return `${name} · ${priceLabel(anchors[0]?.p, tick)}`;
  if (drawing.kind === "zone") return `${name} · ${priceLabel(Math.min(anchors[0].p, anchors[1].p), tick)}–${priceLabel(Math.max(anchors[0].p, anchors[1].p), tick)}`;
  if (drawing.kind === "vline") return `${name} · ${anchorClock(anchors[0]?.t)}`;
  if (drawing.kind === "text") return `${name} · ${drawing.label || "（空）"}`;
  if (anchors.length >= 2) return `${name} · ${anchorClock(anchors[0].t)} → ${anchorClock(anchors.at(-1)!.t)}`;
  return name;
}

/** The price a line's 设到价提醒 uses: a level's own price, a line's value at `time` (the latest bar),
 *  a zone's upper edge; null for a shape. */
export function alertPrice(drawing: Pick<ChartDrawing, "kind" | "points">, time: number | undefined): number | null {
  const anchors = anchorsOf(drawing);
  if (!ALERTABLE_KINDS.has(drawing.kind) || !anchors.length) return null;
  if (drawing.kind === "zone" || drawing.kind === "rect_zone") return Math.max(...anchors.map(anchor => anchor.p));
  if (anchors.length < 2 || time === undefined) return anchors[0].p;
  const [a, b] = anchors;
  if (b.t === a.t) return b.p;
  return a.p + (b.p - a.p) * (time - a.t) / (b.t - a.t);
}

/** A copy's anchors: shifted down by five ticks so it sits beside the original. */
export function duplicateAnchors(kind: DrawingKind, anchors: Anchor[], tick: number | null | undefined): Anchor[] {
  const step = (tick && tick > 0 ? tick : 1) * 5;
  return shiftAnchors(kind, anchors, 0, -step);
}

import type { IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, SeriesAttachedParameter, SeriesType, Time, UTCTimestamp } from "lightweight-charts";
import type { ChartAnnotation } from "../aiAnnotations";

type Target = Parameters<IPrimitivePaneRenderer["draw"]>[0];

/** Ink that reads on a tone color: the dark theme's tones are light, the light theme's are dark. */
export function labelInk(color: string): string {
  const hex = color.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i)?.[1];
  const rgb = color.match(/rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/i);
  let channels: number[] | undefined;
  if (hex) channels = (hex.length === 3 ? hex.split("").map(c => c + c) : hex.match(/../g)!).map(pair => parseInt(pair, 16));
  else if (rgb) channels = [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])];
  if (!channels) return "#ffffff";
  const [r, g, b] = channels.map(value => { const v = value / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.4 ? "#101827" : "#ffffff";
}
export type ToneColors = Record<"info" | "warn" | "danger" | "good", string>;
interface Bar { time: UTCTimestamp }
/** An annotation with its number on the chart (①②③…, shared across panes, by list order). */
export type NumberedAnnotation = ChartAnnotation & { number: number };
type Rect = { left: number; top: number; right: number; bottom: number };

const CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩";
export const circledNumber = (n: number) => CIRCLED[n - 1] ?? `(${n})`;
/** A point's tone follows its role unless the item names one. */
export function annotationTone(item: ChartAnnotation): keyof ToneColors {
  if (item.tone) return item.tone;
  if (item.kind === "point") return ({ buy: "good", sell: "danger", close: "warn", note: "info" } as const)[item.role ?? "note"];
  return "info";
}

/**
 * Draws the AI's annotations on one pane (main, KD or MACD) as a series primitive: numbered
 * points (entries, exits, notes), zones, trend lines, vertical lines, circles, arrows and text.
 * A horizontal line is the library's price line; only its numbered label is drawn here. Labels
 * carry the number and a short label, sit at the left end of lines/zones and beside points,
 * step aside when they would overlap (with a leader back to the point), and are skipped for
 * items outside the visible range. Times are bar timestamps in seconds; a time between or
 * beyond bars maps by interpolation on the logical scale.
 */
export class ChartAnnotationsPrimitive implements ISeriesPrimitive<Time> {
  private chart?: IChartApi;
  private series?: ISeriesApi<SeriesType>;
  private requestUpdate?: () => void;
  private items: NumberedAnnotation[] = [];
  private colors: ToneColors = { info: "#3154d5", warn: "#925500", danger: "#b42318", good: "#18794e" };
  private bars: Bar[] = [];
  private duration = 60;
  private readonly view: IPrimitivePaneView = { zOrder: () => "top", renderer: () => this.renderer };
  private readonly renderer: IPrimitivePaneRenderer = { draw: target => this.draw(target) };

  attached(param: SeriesAttachedParameter<Time>) {
    this.chart = param.chart;
    this.series = param.series;
    this.requestUpdate = param.requestUpdate;
  }
  detached() { this.chart = undefined; this.series = undefined; this.requestUpdate = undefined; }
  paneViews() { return [this.view]; }

  update(items: NumberedAnnotation[], colors: ToneColors, bars: Bar[], duration: number) {
    this.items = items;
    this.colors = colors; this.bars = bars; this.duration = duration;
    this.requestUpdate?.();
  }

  /** Logical coordinate of a time in seconds, interpolated between bars and extrapolated past the ends. */
  private timeToX(time: number): number | null {
    const chart = this.chart;
    if (!chart || !this.bars.length) return null;
    let low = 0, high = this.bars.length - 1;
    while (low < high) { const mid = (low + high + 1) >> 1; if (this.bars[mid].time <= time) low = mid; else high = mid - 1; }
    const index = this.bars[low].time <= time ? low : 0;
    const logical = index + (time - this.bars[index].time) / this.duration;
    const x = chart.timeScale().logicalToCoordinate(logical as never);
    return x === null ? null : x;
  }
  private priceToY(price: number): number | null {
    const y = this.series?.priceToCoordinate(price);
    return y === null || y === undefined ? null : y;
  }

  private draw(target: Target) {
    if (!this.items.length) return;
    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      const width = mediaSize.width, height = mediaSize.height;
      // The pane's own header (价格 / KD / MACD strip) sits over the top-left of the canvas: keep labels off it.
      const placed: Rect[] = [{ left: 0, top: 0, right: width, bottom: 24 }];
      context.save();
      context.font = "12px system-ui, sans-serif";
      context.textBaseline = "middle";
      const overlaps = (a: Rect) => placed.some(b => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top);
      /**
       * A numbered label near (x, y). Tries the requested spot, then steps up/down; when it had to
       * move away from a point it draws a leader from the anchor to the box.
       */
      const label = (item: NumberedAnnotation, x: number, y: number, color: string, align: "left" | "right", anchor?: { x: number; y: number }) => {
        const text = circledNumber(item.number) + (item.label ? " " + item.label : "");
        const padding = 4, boxWidth = context.measureText(text).width + padding * 2, boxHeight = 18;
        const left = Math.max(2, Math.min(width - boxWidth - 2, align === "left" ? x : x - boxWidth));
        let rect: Rect | undefined;
        for (const offset of [0, -22, 22, -44, 44, -66, 66]) {
          const top = Math.max(2, Math.min(height - boxHeight - 2, y - boxHeight / 2 + offset));
          const candidate = { left, top, right: left + boxWidth, bottom: top + boxHeight };
          if (!overlaps(candidate)) { rect = candidate; break; }
        }
        if (!rect) return;
        placed.push(rect);
        if (anchor && Math.abs(rect.top + boxHeight / 2 - anchor.y) > boxHeight) {
          context.strokeStyle = color; context.lineWidth = 1; context.setLineDash([2, 2]);
          context.beginPath(); context.moveTo(anchor.x, anchor.y); context.lineTo(align === "left" ? rect.left : rect.right, rect.top + boxHeight / 2); context.stroke();
          context.setLineDash([]);
        }
        context.fillStyle = color; context.globalAlpha = 0.92;
        context.beginPath(); context.roundRect(rect.left, rect.top, boxWidth, boxHeight, 4); context.fill();
        context.globalAlpha = 1; context.fillStyle = labelInk(color); context.textAlign = "left";
        context.fillText(text, rect.left + padding, rect.top + boxHeight / 2);
      };
      const onScreen = (x: number | null): x is number => x !== null && x >= 0 && x <= width;
      const sideOf = (x: number) => x > width - 140 ? "right" as const : "left" as const;
      for (const item of this.items) {
        const color = this.colors[annotationTone(item)];
        context.strokeStyle = color; context.fillStyle = color; context.lineWidth = 1.5; context.setLineDash([]);
        if (item.kind === "point") {
          const x = this.timeToX(item.time!), y = this.priceToY(item.price!);
          if (!onScreen(x) || y === null) continue;
          const role = item.role ?? "note", r = 6;
          context.beginPath();
          if (role === "buy") { context.moveTo(x, y - r); context.lineTo(x + r, y + r); context.lineTo(x - r, y + r); context.closePath(); }
          else if (role === "sell") { context.moveTo(x, y + r); context.lineTo(x + r, y - r); context.lineTo(x - r, y - r); context.closePath(); }
          else if (role === "close") context.rect(x - r + 1, y - r + 1, 2 * r - 2, 2 * r - 2);
          else context.arc(x, y, r - 1, 0, Math.PI * 2);
          context.fill();
          context.strokeStyle = labelInk(color); context.lineWidth = 1; context.stroke();
          const side = sideOf(x);
          label(item, side === "left" ? x + r + 4 : x - r - 4, y, color, side, { x, y });
        } else if (item.kind === "hline") {
          const y = this.priceToY(item.price!);
          if (y !== null) label(item, 8, y, color, "left");
        } else if (item.kind === "zone") {
          const y1 = this.priceToY(item.price!), y2 = this.priceToY(item.price2!);
          if (y1 === null || y2 === null) continue;
          const top = Math.min(y1, y2), bottom = Math.max(y1, y2);
          context.globalAlpha = 0.14; context.fillRect(0, top, width, bottom - top); context.globalAlpha = 1;
          context.setLineDash([4, 3]);
          context.beginPath(); context.moveTo(0, top); context.lineTo(width, top); context.moveTo(0, bottom); context.lineTo(width, bottom); context.stroke();
          label(item, 8, top + (bottom - top) / 2, color, "left");
        } else if (item.kind === "trendline") {
          const x1 = this.timeToX(item.time!), y1 = this.priceToY(item.price!), x2 = this.timeToX(item.time2!), y2 = this.priceToY(item.price2!);
          if (x1 === null || y1 === null || x2 === null || y2 === null) continue;
          context.lineWidth = 2;
          context.beginPath(); context.moveTo(x1, y1); context.lineTo(x2, y2); context.stroke();
          if (onScreen(x2)) label(item, x2 > width - 140 ? x2 - 6 : x2 + 6, y2, color, sideOf(x2));
        } else if (item.kind === "vline") {
          const x = this.timeToX(item.time!);
          if (!onScreen(x)) continue;
          context.setLineDash([5, 4]);
          context.beginPath(); context.moveTo(x, 0); context.lineTo(x, height); context.stroke();
          // At the bottom, just above the time axis, so several moments do not stack in the top row.
          label(item, sideOf(x) === "left" ? x + 6 : x - 6, height - 12, color, sideOf(x));
        } else if (item.kind === "circle") {
          const x = this.timeToX(item.time!), y = this.priceToY(item.price!);
          if (!onScreen(x) || y === null) continue;
          context.lineWidth = 2;
          context.beginPath(); context.arc(x, y, 14, 0, Math.PI * 2); context.stroke();
          context.globalAlpha = 0.12; context.fill(); context.globalAlpha = 1;
          label(item, sideOf(x) === "left" ? x + 20 : x - 20, y, color, sideOf(x), { x, y });
        } else if (item.kind === "arrow") {
          const x = this.timeToX(item.time!), y = this.priceToY(item.price!);
          if (!onScreen(x) || y === null) continue;
          const tailX = x + 26, tailY = y - 26;
          context.lineWidth = 2;
          context.beginPath(); context.moveTo(tailX, tailY); context.lineTo(x + 4, y - 4); context.stroke();
          context.beginPath(); context.moveTo(x, y); context.lineTo(x + 11, y - 3); context.lineTo(x + 3, y - 11); context.closePath(); context.fill();
          label(item, sideOf(tailX) === "left" ? tailX + 4 : tailX - 4, tailY, color, sideOf(tailX));
        } else if (item.kind === "text") {
          const x = this.timeToX(item.time!), y = this.priceToY(item.price!);
          if (!onScreen(x) || y === null) continue;
          label(item, x, y, color, sideOf(x));
        }
      }
      context.restore();
    });
  }
}

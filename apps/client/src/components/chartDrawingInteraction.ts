import type { IChartApi } from "lightweight-charts";
import { anchorsOf, resolveStyle, shiftAnchors, toolSpec, type Anchor, type ChartDrawing, type DrawingKind } from "../chartDrawingModel";
import type { UserDrawingsPrimitive } from "./chartUserDrawings";

/** What the chart component tells the interaction on every render (read through this object, so the
 *  listeners subscribe once). Anchors are in the chart's own time (分时: synthetic slot seconds). */
export interface DrawingInteractionHooks {
  tool: DrawingKind | null;
  /** 全部锁定: nothing moves. */
  locked: boolean;
  magnet: boolean;
  /** A pane point → an anchor: the bar's time and the tick-rounded price (the bar's O/H/L/C with 磁吸). */
  toAnchor(x: number, y: number, magnet: boolean): Anchor | null;
  onPlace(kind: DrawingKind, anchors: Anchor[]): void;
  /** `handle`: the anchor index pressed on the selected drawing (null: its body). */
  onSelect(id: string | null, at?: { clientX: number; clientY: number }, handle?: number | null): void;
  onMove(drawing: ChartDrawing, anchors: Anchor[]): void;
  /** Dragging one anchor (null when that drag ends or is cancelled): the phone's loupe above the finger. */
  onHandleDrag?(drag: HandleDrag | null): void;
  /** The points a placement in progress has collected (0 once it is placed, reset or cancelled). */
  onDraft?(count: number): void;
  /** 图上拖线改价 (2026-10-24): the draggable price line (the user's own stop / take) within `reach` px of pane `y`. */
  lineAt?(y: number, reach: number): string | null;
  /** That line dragged to `price` (tick-rounded); `done` on the lift — `price` null there: cancelled or never moved. */
  onLineDrag?(id: string, price: number | null, done: boolean): void;
}

/** One anchor under a finger: where it is on screen and in the chart container, the anchor it would set, and
 *  the chart as it looked when the drag began (a still the loupe magnifies; null where screenshots fail). */
export interface HandleDrag {
  clientX: number; clientY: number;
  /** Container-local CSS pixels (the 分时图's left axis included) and the container's width. */
  x: number; y: number; width: number;
  anchor: Anchor;
  image: HTMLCanvasElement | null;
}

const CLICK_SLOP = 4;
/** A finger wobbles more than a mouse: the tap slop and the extra reach of a line or handle under it. */
const TOUCH_SLOP = 10;
const TOUCH_REACH = 10;
/** How close (px) a mouse must be to a draggable price line to grab it. */
const LINE_REACH = 4;

/**
 * 画线第二版 (2026-10-09): placing and moving the user's drawings with the pointer. One instance per
 * chart canvas. With a tool armed, clicks collect anchors (a rubber band follows the cursor) and the
 * last one creates the drawing; a 折线 ends on a double click or Enter. Without a tool, a press on a
 * drawing selects it and — unless it or every drawing is locked — drags it whole, or one anchor when the
 * press lands on a handle of the selected one; the chart's own pan and zoom pause for that drag. A click
 * on empty space clears the selection. Shared by the K 线 and the 分时图, on the computer and the phone (手机画线
 * 2026-10-09): a finger gets a wider tap slop and reach; a second finger (pinch) or a cancelled touch abandons the
 * tap or the drag without placing or saving anything; the chart's own pan and zoom come back as they were.
 */
export class DrawingInteraction {
  hooks: DrawingInteractionHooks;
  private placed: Anchor[] = [];
  private placing: DrawingKind | null = null;
  private down?: { x: number; y: number; hit: boolean; slop: number };
  private drag?: { drawing: ChartDrawing; handle: number | null; start: Anchor; anchors: Anchor[]; current?: Anchor[]; moved: boolean; pointerId: number;
    /** The chart's own pan / zoom options before the drag paused them (restored exactly, not forced on). */
    restore: { handleScroll: unknown; handleScale: unknown }; image?: HTMLCanvasElement | null };
  /** A price line under the pointer (图上拖线改价): its id, the last price it reached once moved. */
  private line?: { id: string; price: number | null; pointerId: number; restore: { handleScroll: unknown; handleScale: unknown } };
  /** Pointers down now: a second one makes the gesture a pinch until every finger lifts. */
  private pointers = new Set<number>();
  private multi = false;

  constructor(private readonly container: HTMLElement, private readonly chart: IChartApi, private readonly primitive: UserDrawingsPrimitive,
    private readonly paneOffsetX: () => number, hooks: DrawingInteractionHooks) {
    this.hooks = hooks;
  }

  private local(event: { clientX: number; clientY: number }): { x: number; y: number } | null {
    const rect = this.container.getBoundingClientRect();
    const x = event.clientX - rect.left - this.paneOffsetX();
    const y = event.clientY - rect.top;
    const height = this.chart.paneSize?.(0)?.height ?? rect.height;
    if (y < 0 || y >= height || x < 0) return null;
    return { x, y };
  }

  /** The tool, contract or period changed (or Esc): drop the clicks collected so far, and end a drag in progress
   *  without saving — its lift must not write the old line onto the new chart. */
  reset() {
    this.endDrag(false);
    this.down = undefined;
    this.placed = [];
    this.placing = this.hooks.tool;
    this.primitive.setDraft(null);
    this.hooks.onDraft?.(0);
  }

  /** A 折线 in progress ends here (double click / Enter / the phone's 完成) once it has two distinct points;
   *  nothing happens for other tools. */
  finish(): boolean {
    const tool = this.hooks.tool;
    const points = this.placed.filter((anchor, index, list) => index === 0 || anchor.t !== list[index - 1].t || anchor.p !== list[index - 1].p);
    if (tool !== "polyline" || points.length < 2) return false;
    this.reset();
    this.hooks.onPlace(tool, points);
    return true;
  }

  /** The drawings on the chart changed: a drag of one that is gone (全部隐藏, hidden, deleted) or now locked ends
   *  without saving. Other changes (a refreshed list, new bars) leave the drag alone. */
  sync(items: readonly ChartDrawing[]) {
    const drag = this.drag;
    if (!drag) return;
    const now = items.find(item => item.id === drag.drawing.id);
    if (!now || this.hooks.locked || resolveStyle(now).locked) { this.endDrag(false); this.down = undefined; }
  }

  /** A drag ends: the chart's pan / zoom options go back to what they were; only a completed one is saved. */
  private endDrag(commit: boolean) {
    const line = this.line;
    if (line) {
      this.line = undefined;
      this.chart.applyOptions(line.restore as never);
      try { this.container.releasePointerCapture(line.pointerId); } catch { /* Already released. */ }
      this.hooks.onLineDrag?.(line.id, commit ? line.price : null, true);
    }
    const drag = this.drag;
    if (!drag) return;
    this.drag = undefined;
    this.chart.applyOptions(drag.restore as never);
    try { this.container.releasePointerCapture(drag.pointerId); } catch { /* Already released. */ }
    if (commit && drag.moved && drag.current) this.hooks.onMove(drag.drawing, drag.current);
    this.primitive.setMoving(null);
    if (drag.handle !== null) this.hooks.onHandleDrag?.(null);
  }

  /** A pinch or a cancelled touch: no tap, no drag, no save. */
  private abandon() {
    this.endDrag(false);
    this.down = undefined;
  }

  /** The chart's own pan and zoom pause while something is dragged; the press never reaches its mouse handlers. */
  private pause(event: PointerEvent): { handleScroll: unknown; handleScale: unknown } {
    // A copy, not the chart's own objects: applyOptions merges into those in place, so references would read false.
    const options = this.chart.options?.() as { handleScroll?: unknown; handleScale?: unknown } | undefined;
    const restore = structuredClone({ handleScroll: options?.handleScroll ?? true, handleScale: options?.handleScale ?? true });
    this.chart.applyOptions({ handleScroll: false, handleScale: false });
    event.preventDefault();
    event.stopPropagation();
    try { this.container.setPointerCapture(event.pointerId); } catch { /* A synthetic event has no capture. */ }
    return restore;
  }

  attach(): () => void {
    const onDown = (event: PointerEvent) => {
      if (event.button !== 0) return;
      // A primary pointer starts a fresh gesture; a second finger turns it into a pinch the chart owns.
      if (event.isPrimary) { this.pointers.clear(); this.multi = false; }
      this.pointers.add(event.pointerId);
      if (this.pointers.size > 1) { this.multi = true; this.abandon(); return; }
      const point = this.local(event);
      if (!point) return;
      const touch = event.pointerType === "touch";
      if (this.placing !== this.hooks.tool) this.reset();
      if (this.hooks.tool) { this.down = { ...point, hit: false, slop: touch ? TOUCH_SLOP : CLICK_SLOP }; return; }
      const lineId = this.hooks.lineAt?.(point.y, touch ? TOUCH_REACH : LINE_REACH) ?? null;
      if (lineId) {
        this.down = { ...point, hit: true, slop: touch ? TOUCH_SLOP : CLICK_SLOP };
        this.line = { id: lineId, price: null, pointerId: event.pointerId, restore: this.pause(event) };
        return;
      }
      const hit = this.primitive.hitTestDrawing(point.x, point.y, touch ? TOUCH_REACH : 0);
      this.down = { ...point, hit: Boolean(hit), slop: touch ? TOUCH_SLOP : CLICK_SLOP };
      if (!hit) return;
      this.hooks.onSelect(hit.drawing.id, { clientX: event.clientX, clientY: event.clientY }, hit.handle);
      if (this.hooks.locked || resolveStyle(hit.drawing).locked) return;
      const start = this.hooks.toAnchor(point.x, point.y, this.hooks.magnet && hit.handle !== null);
      if (!start) return;
      this.drag = { drawing: hit.drawing, handle: hit.handle, start, anchors: anchorsOf(hit.drawing), moved: false, pointerId: event.pointerId, restore: this.pause(event) };
    };
    const onMove = (event: PointerEvent) => {
      const line = this.line;
      if (line) {
        const rect = this.container.getBoundingClientRect();
        const x = event.clientX - rect.left - this.paneOffsetX(), y = event.clientY - rect.top;
        if (line.price === null && this.down && Math.hypot(x - this.down.x, y - this.down.y) < this.down.slop) return;
        const now = this.hooks.toAnchor(x, y, false);
        if (!now) return;
        line.price = now.p;
        this.hooks.onLineDrag?.(line.id, now.p, false);
        return;
      }
      const drag = this.drag;
      if (drag) {
        const rect = this.container.getBoundingClientRect();
        const x = event.clientX - rect.left - this.paneOffsetX(), y = event.clientY - rect.top;
        const now = this.hooks.toAnchor(x, y, this.hooks.magnet && drag.handle !== null);
        if (!now) return;
        if (!drag.moved && this.down && Math.hypot(x - this.down.x, y - this.down.y) < this.down.slop) return;
        drag.moved = true;
        drag.current = drag.handle !== null
          ? drag.anchors.map((anchor, index) => index === drag.handle ? now : anchor)
          : shiftAnchors(drag.drawing.kind, drag.anchors, now.t - drag.start.t, now.p - drag.start.p);
        this.primitive.setMoving({ id: drag.drawing.id, anchors: drag.current });
        if (drag.handle !== null && this.hooks.onHandleDrag) {
          // The still is taken once, before the line moves on it.
          if (drag.image === undefined) { try { drag.image = this.chart.takeScreenshot?.() ?? null; } catch { drag.image = null; } }
          this.hooks.onHandleDrag({ clientX: event.clientX, clientY: event.clientY, x: x + this.paneOffsetX(), y, width: rect.width, anchor: now, image: drag.image });
        }
        return;
      }
      if (this.multi) return;
      const point = this.local(event);
      const tool = this.hooks.tool;
      if (!point) return;
      if (tool) {
        if (this.placing !== tool) this.reset();
        this.container.style.cursor = "crosshair";
        this.primitive.setDraft({ kind: tool, anchors: this.placed, cursor: this.hooks.toAnchor(point.x, point.y, this.hooks.magnet) });
        return;
      }
      this.container.style.cursor = this.hooks.lineAt?.(point.y, LINE_REACH) ? "ns-resize" : this.primitive.hitTestDrawing(point.x, point.y) ? "pointer" : "";
    };
    const lift = (event: PointerEvent) => {
      this.pointers.delete(event.pointerId);
      if (!this.multi) return false;
      // The pinch ends when the last finger lifts; none of its lifts is a tap.
      if (!this.pointers.size) this.multi = false;
      return true;
    };
    const onUp = (event: PointerEvent) => {
      if (lift(event)) return;
      if (this.drag || this.line) { this.endDrag(true); this.down = undefined; return; }
      const down = this.down;
      this.down = undefined;
      const point = this.local(event);
      if (!down || !point || Math.hypot(point.x - down.x, point.y - down.y) >= down.slop) return;
      const tool = this.hooks.tool;
      if (!tool) { if (!down.hit) this.hooks.onSelect(null); return; }
      const anchor = this.hooks.toAnchor(point.x, point.y, this.hooks.magnet);
      if (!anchor) return;
      this.placing = tool;
      this.placed = [...this.placed, anchor];
      if (this.placed.length >= toolSpec(tool).max) {
        const anchors = this.placed;
        this.reset();
        this.hooks.onPlace(tool, anchors);
        return;
      }
      this.primitive.setDraft({ kind: tool, anchors: this.placed, cursor: anchor });
      this.hooks.onDraft?.(this.placed.length);
    };
    // The browser took the touch over (a page scroll, a system gesture): nothing is placed or saved.
    const onCancel = (event: PointerEvent) => { lift(event); this.abandon(); };
    // While a line is being dragged by a finger the page must not scroll under it.
    const onTouchMove = (event: TouchEvent) => { if ((this.drag || this.line) && event.cancelable) event.preventDefault(); };
    const onDoubleClick = () => this.finish();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && this.line) { this.endDrag(false); this.down = undefined; return; }
      if (event.key === "Enter" && this.placed.length) this.finish();
    };
    const onLeave = () => { if (!this.drag && this.hooks.tool) this.primitive.setDraft(this.placed.length ? { kind: this.hooks.tool, anchors: this.placed } : null); };
    this.container.addEventListener("pointerdown", onDown, true);
    this.container.addEventListener("pointermove", onMove);
    this.container.addEventListener("pointerup", onUp);
    this.container.addEventListener("pointercancel", onCancel);
    this.container.addEventListener("pointerleave", onLeave);
    this.container.addEventListener("touchmove", onTouchMove, { passive: false });
    this.container.addEventListener("dblclick", onDoubleClick);
    document.addEventListener("keydown", onKey);
    return () => {
      this.container.removeEventListener("pointerdown", onDown, true);
      this.container.removeEventListener("pointermove", onMove);
      this.container.removeEventListener("pointerup", onUp);
      this.container.removeEventListener("pointercancel", onCancel);
      this.container.removeEventListener("pointerleave", onLeave);
      this.container.removeEventListener("touchmove", onTouchMove);
      this.endDrag(false);
      this.container.removeEventListener("dblclick", onDoubleClick);
      document.removeEventListener("keydown", onKey);
    };
  }
}

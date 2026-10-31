import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { IChartApi } from "lightweight-charts";
import { useChartDrawings, type ChartDrawing } from "../../hooks/useChartDrawings";
import { applyFeedEvent } from "../../runtimeFeed";
import { resetReadIdentity } from "../../runtimeClient";
import { setActiveTradingAccount } from "../../tradingAccountScope";
import { UserDrawingsPrimitive } from "../chartUserDrawings";
import { DrawingInteraction, type DrawingInteractionHooks } from "../chartDrawingInteraction";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const drawing = (over: Partial<ChartDrawing> = {}): ChartDrawing => ({
  id: "d1", symbol: "SHFE.au2612", kind: "hline", points: { price: 8000 }, label: "",
  createdAt: "2026-10-02T01:00:00Z", updatedAt: "2026-10-02T01:00:00Z", ...over,
});

afterEach(() => { vi.unstubAllGlobals(); });

it("reads everything on connect; create posts the exact body with its style and returns the drawing", async () => {
  const calls: { url: string; method?: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), method: init?.method, body: init?.body ? String(init.body) : undefined });
    if (init?.method === "POST") return json({ drawing: drawing({ id: "d9", kind: "fib" }) }, 201);
    return json({ drawings: [drawing()] });
  }));
  const { result } = renderHook(() => useChartDrawings("http://runtime", true));
  await waitFor(() => expect(result.current.items).toHaveLength(1));
  let made: ChartDrawing | null = null;
  await act(async () => { made = await result.current.create("SHFE.au2612", "fib", { anchors: [{ t: 1000, p: 7980 }, { t: 2000, p: 8100 }] }, "", { color: "amber", period: 300 }); });
  expect(made).toMatchObject({ id: "d9" });
  expect(JSON.parse(calls.find(call => call.method === "POST")!.body!)).toEqual({
    symbol: "SHFE.au2612", kind: "fib", points: { anchors: [{ t: 1000, p: 7980 }, { t: 2000, p: 8100 }] }, style: { color: "amber", period: 300 } });
});

it("a refused create is an inline Chinese line; a refused move rolls back and says why", async () => {
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) =>
    init?.method === "POST" ? json({ detail: "这个合约图上已有 200 条线，达到上限 200 条；先删掉一条再画" }, 422)
      : init?.method === "PUT" ? json({ detail: "这条线已锁定，先解锁再移动" }, 409)
      : json({ drawings: [drawing({ points: { price: 8000 } })] })));
  const { result } = renderHook(() => useChartDrawings("http://runtime", true));
  await waitFor(() => expect(result.current.items).toHaveLength(1));
  await act(async () => { expect(await result.current.create("SHFE.au2612", "hline", { price: 7900 })).toBeNull(); });
  expect(result.current.createError).toBe("这个合约图上已有 200 条线，达到上限 200 条；先删掉一条再画");
  let moved = true;
  await act(async () => { moved = await result.current.update("d1", { points: { price: 8100 } }); });
  expect(moved).toBe(false);
  expect(result.current.items[0].points).toEqual({ price: 8000 });   // back where it was
  expect(result.current.notice).toBe("这条线已锁定，先解锁再移动");
});

it("a style change shows at once (null drops a key), 全部删除 deletes by symbol, and the AI's changes re-read on the drawings nudge", async () => {
  const calls: { url: string; method?: string; body?: string }[] = [];
  let stored = [drawing({ style: { color: "amber", dash: "dashed" } })];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), method: init?.method, body: init?.body ? String(init.body) : undefined });
    if (init?.method === "DELETE") { stored = []; return json({ cleared: 2 }); }
    if (init?.method === "PUT") { stored = [drawing({ style: { color: "red" } })]; return json({ drawing: stored[0] }); }
    return json({ drawings: stored });
  }));
  const { result } = renderHook(() => useChartDrawings("http://runtime", true));
  await waitFor(() => expect(result.current.items).toHaveLength(1));
  await act(async () => { await result.current.update("d1", { style: { color: "red", dash: null } }); });
  expect(JSON.parse(calls.find(call => call.method === "PUT")!.body!)).toEqual({ style: { color: "red", dash: null } });
  expect(result.current.items[0].style).toEqual({ color: "red" });
  act(() => { result.current.clear("SHFE.au2612"); });
  await waitFor(() => expect(result.current.notice).toBe("已删除本合约 2 条画线"));
  expect(calls.find(call => call.method === "DELETE")).toMatchObject({ url: "http://runtime/api/market/drawings?symbol=SHFE.au2612" });
  const reads = calls.filter(call => !call.method).length;
  act(() => { applyFeedEvent("s1", { name: "feed.ui", payload: { reason: "command" } } as never); });
  act(() => { applyFeedEvent("s1", { name: "feed.ui", payload: { reason: "drawings" } } as never); });
  await waitFor(() => expect(calls.filter(call => !call.method).length).toBe(reads + 1));   // only the drawings nudge re-reads
});

it("a guest window never reads or writes drawings", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const { result } = renderHook(() => useChartDrawings(undefined, false));
  expect(result.current.available).toBe(false);
  await act(async () => { expect(await result.current.create("SHFE.au2612", "hline", { price: 7900 })).toBeNull(); });
  expect(fetchMock).not.toHaveBeenCalled();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

it.each([200, 409])("keeps successive edits in request order when the first reply is delayed (%s)", async firstStatus => {
  const first = deferred<Response>();
  let saved = drawing();
  const puts: DrawingPatchForTest[] = [];
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method !== "PUT") return json({ drawings: [saved] });
    const patch = JSON.parse(String(init.body)) as DrawingPatchForTest;
    puts.push(patch);
    if (puts.length === 1) {
      if (firstStatus === 200) saved = { ...saved, points: patch.points! };
      return first.promise;
    }
    saved = { ...saved, style: { ...saved.style, ...patch.style } };
    return json({ drawing: saved });
  }));
  const { result } = renderHook(() => useChartDrawings("http://runtime", true));
  await waitFor(() => expect(result.current.items).toHaveLength(1));
  let move!: Promise<boolean>, color!: Promise<boolean>;
  act(() => { move = result.current.update("d1", { points: { price: 8100 } }); });
  await waitFor(() => expect(puts).toHaveLength(1));
  act(() => { color = result.current.update("d1", { style: { color: "red" } }); });
  await act(async () => { await Promise.resolve(); });
  const requestsBeforeFirstReply = puts.length;
  // A feed nudge can finish before the older HTTP reply; it must not erase pending edits either.
  act(() => { applyFeedEvent("s1", { name: "feed.ui", payload: { reason: "drawings" } } as never); });
  const acceptedFirst = structuredClone(saved);
  await act(async () => {
    first.resolve(firstStatus === 200 ? json({ drawing: acceptedFirst }) : json({ detail: "这条线已锁定，先解锁再移动" }, 409));
    await Promise.all([move, color]);
  });
  await waitFor(() => expect(result.current.items[0]).toMatchObject(saved));
  expect(requestsBeforeFirstReply).toBe(1);
  expect(saved.style?.color).toBe("red");
  expect(saved.points.price).toBe(firstStatus === 200 ? 8100 : 8000);
});

type DrawingPatchForTest = { points?: ChartDrawing["points"]; style?: ChartDrawing["style"] };

it("ignores a GET started before a write, and a refused delete stays recoverable even when its re-read fails", async () => {
  let saved = drawing();
  const stale = deferred<Response>();
  let reads = 0;
  let refuseReads = false;
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "PUT") { saved = { ...saved, points: { price: 8100 } }; return json({ drawing: saved }); }
    if (init?.method === "DELETE") { refuseReads = true; return json({ detail: "删除失败" }, 503); }
    reads += 1;
    if (reads === 2) return stale.promise;
    return refuseReads ? json({ detail: "读取失败" }, 503) : json({ drawings: [saved] });
  }));
  const { result } = renderHook(() => useChartDrawings("http://runtime", true));
  await waitFor(() => expect(result.current.items).toHaveLength(1));
  act(() => result.current.refresh());
  await waitFor(() => expect(reads).toBe(2));
  await act(async () => { await result.current.update("d1", { points: { price: 8100 } }); });
  await act(async () => { stale.resolve(json({ drawings: [drawing()] })); });
  expect(result.current.items[0].points.price).toBe(8100);
  act(() => result.current.remove("d1"));
  await waitFor(() => expect(result.current.notice).toBe("删除失败"));
  expect(result.current.items[0]).toMatchObject(saved);
  refuseReads = false;
  act(() => result.current.refresh());
  await waitFor(() => expect(result.current.items[0]).toMatchObject(saved));
});

it("serializes an edit before deletion and retires pending writes on a Runtime switch", async () => {
  const first = deferred<Response>();
  const methods: string[] = [];
  let saved = drawing();
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (String(url).startsWith("http://other")) return json({ drawings: [drawing({ id: "other" })] });
    if (!init?.method) return json({ drawings: [saved] });
    methods.push(init.method);
    if (init.method === "PUT") return first.promise;
    return json({ drawing: saved });
  }));
  const { result, rerender } = renderHook(({ base }) => useChartDrawings(base, true), { initialProps: { base: "http://runtime" } });
  await waitFor(() => expect(result.current.items).toHaveLength(1));
  let move!: Promise<boolean>;
  act(() => { move = result.current.update("d1", { points: { price: 8100 } }); });
  await waitFor(() => expect(methods).toEqual(["PUT"]));
  act(() => { result.current.remove("d1"); });
  await act(async () => { await Promise.resolve(); });
  const queuedMethods = [...methods];
  rerender({ base: "http://other" });
  await waitFor(() => expect(result.current.items[0].id).toBe("other"));
  await act(async () => { saved = { ...saved, points: { price: 8100 } }; first.resolve(json({ drawing: saved })); await move; });
  expect(queuedMethods).toEqual(["PUT"]);
  expect(methods).toEqual(["PUT"]);
  expect(result.current.items.map(item => item.id)).toEqual(["other"]);
});

it("a saved edit followed by deletion stays deleted after re-reading; a trading-account switch shares the lines", async () => {
  const first = deferred<Response>();
  let stored = [drawing()];
  const writes: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (!init?.method) return json({ drawings: stored });
    writes.push(init.method);
    if (init.method === "PUT") return first.promise;
    stored = [];
    return json({ drawing: drawing() });
  }));
  const { result } = renderHook(() => useChartDrawings("http://runtime", true));
  await waitFor(() => expect(result.current.items).toHaveLength(1));
  let move!: Promise<boolean>;
  act(() => { move = result.current.update("d1", { points: { price: 8100 } }); });
  await waitFor(() => expect(writes).toEqual(["PUT"]));
  act(() => { result.current.remove("d1"); setActiveTradingAccount("second"); });
  expect(result.current.items).toEqual([]);
  await act(async () => { stored = [drawing({ points: { price: 8100 } })]; first.resolve(json({ drawing: stored[0] })); await move; });
  await waitFor(() => expect(writes).toEqual(["PUT", "DELETE"]));
  expect(stored).toEqual([]);
  expect(result.current.items).toEqual([]);
  setActiveTradingAccount(null);
});

it("a new login at the same URL never accepts the previous user's create or sends its queued delete", async () => {
  const creating = deferred<Response>();
  const writes: string[] = [];
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (!init?.method) { reads += 1; return json({ drawings: [drawing({ label: reads === 1 ? "old" : "new" })] }); }
    writes.push(init.method);
    if (init.method === "POST") return creating.promise;
    return json({ drawing: drawing() });
  }));
  const { result } = renderHook(() => useChartDrawings("http://runtime", true));
  await waitFor(() => expect(result.current.items[0].label).toBe("old"));
  let created!: Promise<ChartDrawing | null>;
  act(() => { created = result.current.create("SHFE.au2612", "hline", { price: 7900 }); });
  await waitFor(() => expect(writes).toEqual(["POST"]));
  act(() => { result.current.remove("d1"); resetReadIdentity(); });
  await waitFor(() => expect(result.current.items[0].label).toBe("new"));
  await act(async () => { creating.resolve(json({ drawing: drawing({ id: "old-created" }) }, 201)); expect(await created).toBeNull(); });
  expect(writes).toEqual(["POST"]);
  expect(result.current.items.map(item => item.id)).toEqual(["d1"]);
  expect(result.current.items[0].label).toBe("new");
});

/** The chart's own pan / zoom options as a phone chart sets them (nested axis switches included). */
const CHART_GESTURES = {
  handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
  handleScale: { axisPressedMouseMove: { time: false, price: true }, axisDoubleClickReset: { time: true, price: true }, mouseWheel: true, pinch: true },
};
type Options = Record<string, unknown>;
/** lightweight-charts' option store: `options()` hands out its internal object and `applyOptions` merges into it in
 *  place, a boolean `handleScroll` / `handleScale` filling every leaf — so a kept reference changes under the caller. */
function optionStore() {
  const store = structuredClone(CHART_GESTURES) as Options;
  const fill = (shape: unknown, value: boolean): unknown => shape && typeof shape === "object"
    ? Object.fromEntries(Object.entries(shape).map(([key, inner]) => [key, fill(inner, value)])) : value;
  const merge = (target: Options, patch: Options) => {
    for (const [key, value] of Object.entries(patch)) {
      const next = typeof value === "boolean" && target[key] && typeof target[key] === "object" ? fill(target[key], value) : value;
      if (next && typeof next === "object" && target[key] && typeof target[key] === "object") merge(target[key] as Options, next as Options);
      else target[key] = next;
    }
  };
  return { options: () => store, applyOptions: vi.fn((patch: Options) => merge(store, patch)) };
}

/** A flat identity axis: time t sits at x = t − 1000 (one bar per second), price p at y = 600 − p. */
function axis() {
  const primitive = new UserDrawingsPrimitive();
  const { options, applyOptions } = optionStore();
  const chart = { timeScale: () => ({ logicalToCoordinate: (logical: number) => logical }), paneSize: () => ({ width: 800, height: 600 }), options, applyOptions } as unknown as IChartApi;
  primitive.attached({ chart, series: { priceToCoordinate: (price: number) => 600 - price } as never, requestUpdate: () => undefined } as never);
  const show = (items: ChartDrawing[], selectedId: string | null = null) => primitive.update({ items, palette: { color: () => "#a21caf", up: "#f00", down: "#0f0", panel: "#fff" },
    selectedId, bars: [{ time: 1000 as never }], duration: 1 });
  return { primitive, chart, applyOptions, show };
}

it("hit-test follows each kind's drawn geometry, topmost first, and a handle wins on the selected one", () => {
  const { primitive, show } = axis();
  show([
    drawing({ id: "low", kind: "hline", points: { price: 500 } }),
    drawing({ id: "band", kind: "zone", points: { low: 400, high: 450 } }),
    drawing({ id: "trend", kind: "trendline", points: { t1: 1000, p1: 500, t2: 1060, p2: 400 } }),
    drawing({ id: "box", kind: "rect", points: { anchors: [{ t: 1300, p: 300 }, { t: 1400, p: 200 }] } }),
    drawing({ id: "fib", kind: "fib", points: { anchors: [{ t: 1500, p: 100 }, { t: 1600, p: 200 }] } }),
    drawing({ id: "vertical", kind: "vline", points: { anchors: [{ t: 1700 }] } }),
  ]);
  expect(primitive.hitTestDrawing(200, 95)?.drawing.id).toBe("low");
  expect(primitive.hitTestDrawing(200, 107)).toBeNull();                  // 7 px off a 1 px line
  expect(primitive.hitTestDrawing(200, 175)?.drawing.id).toBe("band");
  expect(primitive.hitTestDrawing(30, 152)?.drawing.id).toBe("trend");    // 2 px off the segment
  expect(primitive.hitTestDrawing(350, 350)?.drawing.id).toBe("box");     // inside the filled rectangle
  expect(primitive.hitTestDrawing(550, 451)?.drawing.id).toBe("fib");     // on the 61.8% level (600 − 138.2·… )
  expect(primitive.hitTestDrawing(700, 20)?.drawing.id).toBe("vertical");
  show([drawing({ id: "under", points: { price: 502 } }), drawing({ id: "top", points: { price: 500 } })]);
  expect(primitive.hitTestDrawing(0, 99)?.drawing.id).toBe("top");         // the later line is drawn on top
  show([drawing({ id: "trend", kind: "trendline", points: { t1: 1000, p1: 500, t2: 1060, p2: 400 } })], "trend");
  expect(primitive.hitTestDrawing(60, 201)).toMatchObject({ handle: 1 });
});

function pointer(type: string, x: number, y: number) {
  return new MouseEvent(type, { clientX: x, clientY: y, button: 0, bubbles: true, cancelable: true });
}

function interaction(hooks: Partial<DrawingInteractionHooks>, items: ChartDrawing[], selectedId: string | null = null) {
  const parts = axis();
  parts.show(items, selectedId);
  const container = document.createElement("div");
  container.getBoundingClientRect = () => ({ left: 0, top: 0, width: 800, height: 600, right: 800, bottom: 600, x: 0, y: 0, toJSON: () => ({}) });
  const calls = { onPlace: vi.fn(), onSelect: vi.fn(), onMove: vi.fn() };
  const control = new DrawingInteraction(container, parts.chart, parts.primitive, () => 0, {
    tool: null, locked: false, magnet: false, toAnchor: (x, y) => ({ t: 1000 + x, p: 600 - y }), ...calls, ...hooks,
  });
  const detach = control.attach();
  return { ...parts, container, calls, control, detach };
}

it("dragging moves a drawing; a locked one (or 全部锁定) is selected but never moves and the chart keeps panning", () => {
  const moving = interaction({}, [drawing({ id: "a", points: { price: 500 } })]);
  moving.container.dispatchEvent(pointer("pointerdown", 300, 100));
  moving.container.dispatchEvent(pointer("pointermove", 300, 80));
  moving.container.dispatchEvent(pointer("pointerup", 300, 80));
  expect(moving.calls.onSelect).toHaveBeenCalledWith("a", { clientX: 300, clientY: 100 }, null);
  expect(moving.calls.onMove).toHaveBeenCalledWith(expect.objectContaining({ id: "a" }), [{ t: 0, p: 520 }]);
  expect(moving.applyOptions).toHaveBeenNthCalledWith(1, { handleScroll: false, handleScale: false });
  expect(moving.chart.options()).toEqual(CHART_GESTURES);
  moving.detach();
  for (const [hooks, style] of [[{}, { locked: true }], [{ locked: true }, {}]] as const) {
    const fixed = interaction(hooks, [drawing({ id: "b", points: { price: 500 }, style })]);
    fixed.container.dispatchEvent(pointer("pointerdown", 300, 100));
    fixed.container.dispatchEvent(pointer("pointermove", 300, 60));
    fixed.container.dispatchEvent(pointer("pointerup", 300, 60));
    expect(fixed.calls.onSelect).toHaveBeenCalledWith("b", expect.anything(), null);
    expect(fixed.calls.onMove).not.toHaveBeenCalled();
    expect(fixed.applyOptions).not.toHaveBeenCalled();
    fixed.detach();
  }
});

it("图上拖线改价: a protection line wins over a drawing under it, follows the pointer, and Esc or a still lift cancels", () => {
  const onLineDrag = vi.fn();
  const lineAt = (y: number, reach: number) => Math.abs(y - 100) <= reach ? "s1:stop" : null;   // the stop at price 500
  const drag = interaction({ lineAt, onLineDrag }, [drawing({ id: "a", points: { price: 500 } })]);
  drag.container.dispatchEvent(pointer("pointerdown", 300, 102));
  drag.container.dispatchEvent(pointer("pointermove", 300, 90));
  drag.container.dispatchEvent(pointer("pointerup", 300, 90));
  expect(onLineDrag.mock.calls).toEqual([["s1:stop", 510, false], ["s1:stop", 510, true]]);
  expect(drag.calls.onSelect).not.toHaveBeenCalled();                    // the drawing under it is left alone
  expect(drag.applyOptions).toHaveBeenNthCalledWith(1, { handleScroll: false, handleScale: false });
  expect(drag.chart.options()).toEqual(CHART_GESTURES);                    // pan / zoom back as they were
  onLineDrag.mockClear();
  drag.container.dispatchEvent(pointer("pointerdown", 300, 100));
  drag.container.dispatchEvent(pointer("pointermove", 300, 80));
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
  expect(onLineDrag.mock.calls.at(-1)).toEqual(["s1:stop", null, true]);
  drag.container.dispatchEvent(pointer("pointerdown", 300, 100));
  drag.container.dispatchEvent(pointer("pointerup", 300, 100));            // never moved: nothing to confirm
  expect(onLineDrag.mock.calls.at(-1)).toEqual(["s1:stop", null, true]);
  drag.detach();
});

it("an armed tool places on clicks (a drag pans instead) and a 折线 ends on a double click", () => {
  const placing = interaction({ tool: "trendline" }, []);
  placing.container.dispatchEvent(pointer("pointerdown", 100, 100));
  placing.container.dispatchEvent(pointer("pointerup", 100, 100));
  placing.container.dispatchEvent(pointer("pointerdown", 150, 150));
  placing.container.dispatchEvent(pointer("pointerup", 190, 150));       // dragged 40 px: a pan, not a click
  expect(placing.calls.onPlace).not.toHaveBeenCalled();
  placing.container.dispatchEvent(pointer("pointerdown", 200, 50));
  placing.container.dispatchEvent(pointer("pointerup", 200, 50));
  expect(placing.calls.onPlace).toHaveBeenCalledWith("trendline", [{ t: 1100, p: 500 }, { t: 1200, p: 550 }]);
  placing.detach();
  const line = interaction({ tool: "polyline" }, []);
  for (const [x, y] of [[10, 10], [20, 30], [40, 20]]) {
    line.container.dispatchEvent(pointer("pointerdown", x, y));
    line.container.dispatchEvent(pointer("pointerup", x, y));
  }
  line.container.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
  expect(line.calls.onPlace).toHaveBeenCalledWith("polyline", [{ t: 1010, p: 590 }, { t: 1020, p: 570 }, { t: 1040, p: 580 }]);
  line.detach();
});

/** A finger: pointer id, primary flag and type ride on the event like a real touch's. */
function touch(type: string, x: number, y: number, id = 1) {
  const event = pointer(type, x, y);
  Object.defineProperties(event, { pointerId: { value: id }, pointerType: { value: "touch" }, isPrimary: { value: id === 1 } });
  return event;
}

it("手机画线: a cancelled touch drag saves nothing and gives the chart back its own pan/zoom options, not forced on", () => {
  const drag = interaction({}, [drawing({ id: "a", points: { price: 500 } })]);
  drag.container.dispatchEvent(touch("pointerdown", 300, 108));          // 8 px off the line: a finger's reach
  expect(drag.chart.options()).toMatchObject({ handleScroll: { horzTouchDrag: false }, handleScale: { pinch: false, axisPressedMouseMove: { price: false } } });
  drag.container.dispatchEvent(touch("pointermove", 300, 60));
  drag.container.dispatchEvent(touch("pointercancel", 300, 60));
  expect(drag.calls.onSelect).toHaveBeenCalledWith("a", expect.anything(), null);
  expect(drag.calls.onMove).not.toHaveBeenCalled();
  expect(drag.chart.options()).toEqual(CHART_GESTURES);
  // A second finger during a drag turns it into a pinch: the drag is abandoned, nothing is saved.
  drag.container.dispatchEvent(touch("pointerdown", 300, 100));
  drag.container.dispatchEvent(touch("pointermove", 300, 70));
  drag.container.dispatchEvent(touch("pointerdown", 500, 300, 2));
  drag.container.dispatchEvent(touch("pointerup", 300, 70));
  drag.container.dispatchEvent(touch("pointerup", 500, 300, 2));
  expect(drag.calls.onMove).not.toHaveBeenCalled();
  expect(drag.chart.options()).toEqual(CHART_GESTURES);
  drag.detach();
});

it("手机画线: a period switch, 全部隐藏, a hidden or locked line mid-drag ends it unsaved; the next drag still saves", () => {
  const line = drawing({ id: "t", kind: "trendline", points: { t1: 1000, p1: 500, t2: 1060, p2: 400 } });
  const onHandleDrag = vi.fn();
  const drag = interaction({ onHandleDrag }, [line], "t");
  const press = () => {
    drag.container.dispatchEvent(touch("pointerdown", 66, 206));
    drag.container.dispatchEvent(touch("pointermove", 80, 150));
    expect(onHandleDrag).toHaveBeenLastCalledWith(expect.objectContaining({ anchor: { t: 1080, p: 450 } }));
  };
  const interrupted = (cut: () => void) => {
    press();
    cut();
    expect(onHandleDrag).toHaveBeenLastCalledWith(null);                  // the loupe goes at once
    expect(drag.chart.options()).toEqual(CHART_GESTURES);
    drag.container.dispatchEvent(touch("pointerup", 80, 150));
    expect(drag.calls.onMove).not.toHaveBeenCalled();
  };
  interrupted(() => drag.control.reset());                               // 切周期 / 合约 / 工具
  interrupted(() => drag.control.sync([]));                              // 全部隐藏, hidden or deleted
  interrupted(() => drag.control.sync([{ ...line, style: { locked: true } }]));
  interrupted(() => { drag.control.hooks = { ...drag.control.hooks, locked: true }; drag.control.sync([line]); });   // 全部锁定
  drag.control.hooks = { ...drag.control.hooks, locked: false };
  press();
  drag.control.sync([{ ...line, label: "刷新后的同一条" }]);              // a refreshed list keeps the drag
  drag.container.dispatchEvent(touch("pointerup", 80, 150));
  expect(drag.calls.onMove).toHaveBeenCalledWith(line, [{ t: 1000, p: 500 }, { t: 1080, p: 450 }]);
  expect(drag.chart.options()).toEqual(CHART_GESTURES);
  drag.detach();
});

it("手机画线: dragging an endpoint reports the finger and the anchor for the loupe, then clears it", () => {
  const onHandleDrag = vi.fn();
  const trend = drawing({ id: "t", kind: "trendline", points: { t1: 1000, p1: 500, t2: 1060, p2: 400 } });
  const drag = interaction({ onHandleDrag }, [trend], "t");
  drag.container.dispatchEvent(touch("pointerdown", 66, 206));            // near the second handle (60, 200)
  drag.container.dispatchEvent(touch("pointermove", 80, 150));
  expect(drag.calls.onSelect).toHaveBeenCalledWith("t", expect.anything(), 1);
  expect(onHandleDrag).toHaveBeenLastCalledWith(expect.objectContaining({ clientX: 80, clientY: 150, x: 80, y: 150, anchor: { t: 1080, p: 450 } }));
  drag.container.dispatchEvent(touch("pointerup", 80, 150));
  expect(onHandleDrag).toHaveBeenLastCalledWith(null);
  expect(drag.calls.onMove).toHaveBeenCalledWith(trend, [{ t: 1000, p: 500 }, { t: 1080, p: 450 }]);
  drag.detach();
});

it("手机画线: a pinch, a pan or a cancelled tap never places a point; a finger's small wobble still does; 折线 finishes at two points", () => {
  const onDraft = vi.fn();
  const placing = interaction({ tool: "trendline", onDraft }, []);
  const tap = (x: number, y: number, dx = 0) => {
    placing.container.dispatchEvent(touch("pointerdown", x, y));
    placing.container.dispatchEvent(touch("pointerup", x + dx, y));
  };
  placing.container.dispatchEvent(touch("pointerdown", 100, 100));
  placing.container.dispatchEvent(touch("pointerdown", 300, 300, 2));    // two fingers: the chart's pinch
  placing.container.dispatchEvent(touch("pointerup", 100, 100));
  placing.container.dispatchEvent(touch("pointerup", 300, 300, 2));
  tap(150, 150, 40);                                                     // a one-finger pan
  placing.container.dispatchEvent(touch("pointerdown", 220, 220));
  placing.container.dispatchEvent(touch("pointercancel", 220, 220));     // the browser took the touch
  placing.container.dispatchEvent(touch("pointerup", 220, 220));
  expect(onDraft).not.toHaveBeenCalledWith(1);
  tap(200, 50, 6);                                                       // 6 px wobble: still a tap
  expect(onDraft).toHaveBeenLastCalledWith(1);
  tap(260, 90);
  expect(placing.calls.onPlace).toHaveBeenCalledWith("trendline", [{ t: 1206, p: 550 }, { t: 1260, p: 510 }]);
  expect(onDraft).toHaveBeenLastCalledWith(0);
  placing.detach();
  const line = interaction({ tool: "polyline" }, []);
  line.container.dispatchEvent(touch("pointerdown", 10, 10));
  line.container.dispatchEvent(touch("pointerup", 10, 10));
  expect(line.control.finish()).toBe(false);                             // one point is not a line
  expect(line.calls.onPlace).not.toHaveBeenCalled();
  line.container.dispatchEvent(touch("pointerdown", 40, 20));
  line.container.dispatchEvent(touch("pointerup", 40, 20));
  expect(line.control.finish()).toBe(true);
  expect(line.calls.onPlace).toHaveBeenCalledWith("polyline", [{ t: 1010, p: 590 }, { t: 1040, p: 580 }]);
  line.detach();
});

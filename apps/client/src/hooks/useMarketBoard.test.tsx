import { act, renderHook } from "@testing-library/react";
import type { UiEvent } from "@ai-trader/ui-events";
import type { MarketBoardSnapshot } from "../api";
import { resendFeedView, resetFeedViewForTests, setFeedViewTarget, unionBoardWant } from "../feedView";
import { applyFeedEvent, markFeedStreamAlive, resetFeed } from "../runtimeFeed";
import { useMarketBoard } from "./useMarketBoard";

const getMarketBoard = vi.hoisted(() => vi.fn());
const requestJson = vi.hoisted(() => vi.fn());
vi.mock("../api", () => ({ getMarketBoard, requestJson }));

const BASE = "https://runtime.test";
const event = (name: string, payload: unknown): UiEvent =>
  ({ schemaVersion: 1, id: name, sessionId: "s", kind: "result", name, timestamp: "now", payload } as unknown as UiEvent);
const row = (symbol: string, exchangeId: string, productId: string, last: number) =>
  ({ symbol, exchangeId, productId, instrumentId: symbol.split(".")[1], instrumentName: productId, productName: productId, last,
    preSettlement: 1, change: 0, changePercent: 0, volume: 1, openInterest: 1, openInterestChange: 0, high: 1, low: 1,
    upperLimit: 2, lowerLimit: 0, quoteTime: "21:00:00", status: "normal" });
const fullBoard = (key: string, capturedAt = "2026-10-12T13:00:01Z") => ({
  schemaVersion: 1, capturedAt, dataStatus: "normal", revision: "r1", truncated: false, feedRevision: "A", key,
  rows: [row("SHFE.au2612", "SHFE", "au", 800), row("DCE.i2601", "DCE", "i", 780)],
});
const viewBodies = () => requestJson.mock.calls.filter(call => String(call[0]).endsWith("/feed/view")).map(call => JSON.parse(call[1].body));

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-12T13:00:00Z"));
  getMarketBoard.mockResolvedValue({ capturedAt: "2026-10-12T12:59:00Z", dataStatus: "normal", revision: "p", truncated: false, rows: [] } satisfies MarketBoardSnapshot);
  requestJson.mockResolvedValue({});
  resetFeed("s");
  setFeedViewTarget(BASE, "s");
});

afterEach(() => {
  resetFeedViewForTests();
  resetFeed();
  vi.useRealTimers();
  vi.clearAllMocks();
});

test("the view names the visible page and the union of the readers' rows, once per change", async () => {
  expect(unionBoardWant([])).toBeUndefined();
  expect(unionBoardWant([{ exchanges: ["dce"] }, { all: true }])?.key).toBe("all");
  const narrow = unionBoardWant([{ exchanges: ["dce"] }, { products: ["SHFE.au", "rb"] }])!;
  expect(narrow).toMatchObject({ all: false, exchanges: ["DCE"], products: ["rb", "shfe.au"], symbols: [] });
  expect(narrow.key).toBe(unionBoardWant([{ products: ["rb", "SHFE.au"] }, { exchanges: ["DCE"] }])!.key);

  markFeedStreamAlive("s");
  const one = renderHook(() => useMarketBoard(BASE, true, 3000, { exchanges: ["DCE"] }));
  const two = renderHook(() => useMarketBoard(BASE, true, 15_000, { products: ["au"] }));
  resendFeedView();
  await act(async () => { await vi.advanceTimersByTimeAsync(60); });
  const sent = viewBodies().at(-1);
  expect(sent).toEqual({ visible: true, board: { all: false, exchanges: ["DCE"], products: ["au"], symbols: [], key: expect.stringMatching(/^w:/) } });
  two.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(60); });
  expect(viewBodies().at(-1).board.products).toEqual([]);
  one.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(60); });
  expect(viewBodies().at(-1)).toEqual({ visible: true, board: null });
});

const streamLive = () => applyFeedEvent("s", event("feed.execution", { stops: [], simulationOnly: true, liveTrading: "disabled" }), Date.now());

test("a pushed board replaces the poll; patches change only their rows; silence hands back to the poll", async () => {
  markFeedStreamAlive("s");
  streamLive();
  const hook = renderHook(() => useMarketBoard(BASE, true));
  resendFeedView();
  await act(async () => { await vi.advanceTimersByTimeAsync(60); });
  act(() => { applyFeedEvent("s", event("feed.board", fullBoard("all")), Date.now()); });
  expect(hook.result.current.snapshot?.rows.map(item => item.symbol)).toEqual(["SHFE.au2612", "DCE.i2601"]);
  const untouched = hook.result.current.snapshot!.rows[0];
  act(() => {
    applyFeedEvent("s", event("feed.board-patch", { feedRevision: "B", base: "A", key: "all",
      meta: { capturedAt: "2026-10-12T13:00:02Z" }, rows: { "DCE.i2601": { last: 781 } } }), Date.now());
  });
  const rows = hook.result.current.snapshot!.rows;
  expect(rows[1].last).toBe(781);
  expect(rows[0]).toBe(untouched);  // an unchanged row keeps its identity
  expect(hook.result.current.snapshot?.capturedAt).toBe("2026-10-12T13:00:02Z");
  // Covered by the push: no read at all, through several poll intervals.
  await act(async () => { await vi.advanceTimersByTimeAsync(8_000); });
  expect(getMarketBoard).not.toHaveBeenCalled();
  // The push goes silent past FEED_STALE_MS: the poll takes over.
  await act(async () => { await vi.advanceTimersByTimeAsync(6_000); });
  expect(getMarketBoard).toHaveBeenCalled();
  hook.unmount();
});

test("a patch that misses its base asks for the view again; an unavailable board polls with its error line", async () => {
  markFeedStreamAlive("s");
  streamLive();
  const hook = renderHook(() => useMarketBoard(BASE, true));
  resendFeedView();
  await act(async () => { await vi.advanceTimersByTimeAsync(60); });
  act(() => { applyFeedEvent("s", event("feed.board", fullBoard("all")), Date.now()); });
  const before = viewBodies().length;
  act(() => {
    applyFeedEvent("s", event("feed.board-patch", { feedRevision: "C", base: "B", key: "all", meta: {}, rows: { "DCE.i2601": { last: 1 } } }), Date.now());
  });
  await act(async () => { await vi.advanceTimersByTimeAsync(10); });
  expect(viewBodies().length).toBe(before + 1);
  expect(hook.result.current.snapshot?.rows[1].last).toBe(780);  // never applied onto the wrong base
  getMarketBoard.mockRejectedValue(new Error("503 market_board_unavailable"));
  act(() => { applyFeedEvent("s", event("feed.status", { channel: "board", status: "unavailable", reason: "market_board_unavailable" }), Date.now()); });
  await act(async () => { await vi.advanceTimersByTimeAsync(3_100); });
  expect(getMarketBoard).toHaveBeenCalled();
  expect(hook.result.current.error).toContain("报价总表读取失败");
  // The last rows stay on screen beside the error line.
  expect(hook.result.current.snapshot?.rows).toHaveLength(2);
  hook.unmount();
});

test("a hidden page says so at once and asks for full frames when it comes back", async () => {
  markFeedStreamAlive("s");
  const hook = renderHook(() => useMarketBoard(BASE, true));
  resendFeedView();
  await act(async () => { await vi.advanceTimersByTimeAsync(60); });
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  document.dispatchEvent(new Event("visibilitychange"));
  await act(async () => { await vi.advanceTimersByTimeAsync(10); });
  expect(viewBodies().at(-1)).toMatchObject({ visible: false, board: { all: true } });
  visibility.mockReturnValue("visible");
  document.dispatchEvent(new Event("visibilitychange"));
  await act(async () => { await vi.advanceTimersByTimeAsync(10); });
  expect(viewBodies().at(-1)).toMatchObject({ visible: true, board: { all: true } });
  visibility.mockRestore();
  hook.unmount();
});

test("an older Runtime without the view route keeps the readers polling and is not asked again", async () => {
  requestJson.mockRejectedValue(Object.assign(new Error("404"), { status: 404, detail: { detail: "Not Found" } }));
  markFeedStreamAlive("s");
  const hook = renderHook(() => useMarketBoard(BASE, true));
  resendFeedView();
  await act(async () => { await vi.advanceTimersByTimeAsync(60); });
  const asked = viewBodies().length;
  expect(asked).toBe(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(getMarketBoard).toHaveBeenCalled();
  resendFeedView();
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(viewBodies().length).toBe(asked);
  hook.unmount();
});

import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { getMarketWorkbench, type InstrumentCatalog, type MarketKlineBar, type MarketWorkbenchSnapshot } from "./api";
import { cacheDb, clearCacheDb, resetCacheDbForTests } from "./localCache";
import { barsDigest, cachedMarket, evict, feedFocusHeldForTests, feedFocusSince, feedMarketFrame, loadCachedMarket, mergeSince, readMarketWorkbench,
  refuseFeedSince, loadInstrumentNames, rememberInstrumentNames, rememberMarket, resetMarketCacheForTests } from "./marketCache";
import { resetFeed } from "./runtimeFeed";

vi.mock("./api", () => ({ getMarketWorkbench: vi.fn() }));
const read = vi.mocked(getMarketWorkbench);

const MINUTE = 60_000_000_000;
const bar = (index: number, close = 3000 + index): MarketKlineBar => ({ datetime: 1_791_800_000_000_000_000 + index * MINUTE,
  open: close - 1, high: close + 1, low: close - 2, close, volume: 100 + index, openInterest: 9000 + index, tradingDay: "2026-10-13" });
const window = (from: number, to: number, change: (index: number) => number | undefined = () => undefined) =>
  Array.from({ length: to - from }, (_, offset) => bar(from + offset, change(from + offset)));
const snapshot = (bars: MarketKlineBar[], extra: Partial<MarketWorkbenchSnapshot> = {}) => ({
  schemaVersion: 1, durationSeconds: 60, dataStatus: "normal", isCached: false, liveTrading: "disabled",
  quote: { symbol: "SHFE.rb2701", lastPrice: bars.at(-1)?.close ?? 0 }, bars, ...extra,
}) as unknown as MarketWorkbenchSnapshot;
/** What the Runtime answers for `since` over its window (shared_market.workbench_since). */
const since = (bars: MarketKlineBar[], after: number) => {
  const index = bars.findIndex(item => item.datetime === after);
  return index < 0 ? snapshot(bars) : { ...snapshot(bars.slice(index + 1)), barsAfter: after,
    barsWindow: { first: bars[0].datetime, count: bars.length, digest: barsDigest(bars.slice(0, index + 1)) } };
};
/** A fake Runtime: answers whole or since-reads from its current bars and counts the bars it sent. */
function runtime(initial: MarketKlineBar[]) {
  const state = { bars: initial, sent: 0, calls: [] as (number | undefined)[] };
  read.mockImplementation(async (_base, _symbol, _duration, _signal, after) => {
    state.calls.push(after);
    const answer = after === undefined ? snapshot(state.bars) : since(state.bars, after);
    state.sent += answer.bars.length;
    return answer as never;
  });
  return state;
}
const settle = () => new Promise(resolve => setTimeout(resolve, 20));

beforeEach(() => {
  globalThis.indexedDB = new IDBFactory();
  resetCacheDbForTests();
  resetMarketCacheForTests();
  read.mockReset();
});
afterEach(() => localStorage.clear());

test("the digest is the Runtime's: the same vector as test_shared_market.py", () => {
  expect(barsDigest([
    { datetime: 1_791_811_500_000_000_000, open: 3087, high: 3087.5, low: 3084, close: 3085.2, volume: 6472, openInterest: 1684859, tradingDay: "2026-10-13" },
    { datetime: 1_791_811_800_000_000_000, open: 812.34, high: 0.1 + 0.2, low: 3084, close: 3085, volume: 1555.0, openInterest: 1685064 },
  ])).toBe("17427239");
  expect(barsDigest([])).toBe("00000000");
});

test("a held chart asks only for the bars after its last closed one and never stores the forming bar", async () => {
  const server = runtime(window(0, 256));
  const first = await readMarketWorkbench("https://rt", "SHFE.rb2701", 60);
  expect(first.bars).toHaveLength(256);
  expect(server.calls).toEqual([undefined]);
  await settle();
  // The forming last bar stayed out of the store: the next read starts after bar 254.
  server.bars = [...window(1, 256), bar(256)]; // the window slid: bar 255 closed, 256 forms, 0 dropped off
  server.sent = 0;
  const second = await readMarketWorkbench("https://rt", "SHFE.rb2701", 60);
  expect(server.calls.at(-1)).toBe(bar(254).datetime);
  expect(server.sent).toBe(2); // bars 255 and 256 only
  expect(second.bars).toEqual(server.bars); // exactly the Runtime's window, trimmed at its first bar
  expect("barsAfter" in second || "barsWindow" in second).toBe(false);
  // A new page (memory gone) reads the same chart back from IndexedDB and still asks for the newer bars only.
  resetMarketCacheForTests();
  const drawn = await loadCachedMarket("https://rt", "shfe.RB2701", 60);
  expect(drawn?.bars.at(-1)?.datetime).toBe(bar(255).datetime);
  expect(drawn?.dataStatus).toBe("stale");
  expect(drawn?.isCached).toBe(true);
  server.sent = 0;
  await readMarketWorkbench("https://rt", "SHFE.rb2701", 60);
  expect(server.calls.at(-1)).toBe(bar(255).datetime);
  expect(server.sent).toBe(1);
});

test("a corrected bar, a changed trading day or a longer window fails the proof and the whole window is read again", async () => {
  const server = runtime(window(0, 100));
  await readMarketWorkbench("https://rt", "SHFE.rb2701", 60);
  await settle();
  server.bars = window(0, 100, index => (index === 40 ? 9999 : undefined)); // the source corrected bar 40
  const corrected = await readMarketWorkbench("https://rt", "SHFE.rb2701", 60);
  expect(server.calls.slice(-2)).toEqual([bar(98).datetime, undefined]);
  expect(corrected.bars[40].close).toBe(9999);
  await settle();
  server.bars = server.bars.map(item => item.datetime === bar(10).datetime ? { ...item, tradingDay: "2026-10-12" } : item);
  expect((await readMarketWorkbench("https://rt", "SHFE.rb2701", 60)).bars[10].tradingDay).toBe("2026-10-12");
  expect(server.calls.at(-1)).toBeUndefined();
  // The Runtime now holds more history than the browser: the held first bar is not the window's first.
  expect(mergeSince(window(5, 99), since(window(0, 100), bar(98).datetime) as never)).toBeUndefined();
});

test("a shorter window (a guest's limit) is never extended from held bars", () => {
  const held = window(0, 100);
  const limited = window(60, 100); // the Runtime's window starts later now
  const merged = mergeSince(held, { ...since(limited, bar(98).datetime), historyTradingDays: 3 } as never);
  expect(merged?.bars).toEqual(limited);
  expect(merged?.bars[0].datetime).toBe(bar(60).datetime);
  expect(merged?.historyTradingDays).toBe(3);
});

test("without IndexedDB (private window) reads stay whole and nothing fails", async () => {
  globalThis.indexedDB = undefined as never;
  resetCacheDbForTests();
  expect(await cacheDb()).toBeUndefined();
  const server = runtime(window(0, 50));
  expect((await readMarketWorkbench("https://rt", "SHFE.rb2701", 60)).bars).toHaveLength(50);
  // This page still holds the chart: the next read is incremental, and drawing it works.
  await readMarketWorkbench("https://rt", "SHFE.rb2701", 60);
  expect(server.calls).toEqual([undefined, bar(48).datetime]);
  expect(cachedMarket("https://rt", "SHFE.rb2701", 60)?.isCached).toBe(true);
  expect(await loadCachedMarket("https://rt", "SHFE.rb2701", 300)).toBeUndefined();
});

test("only live data is kept, per Runtime, symbol and period", () => {
  rememberMarket("r", snapshot(window(0, 3), { dataStatus: "stale" }));
  rememberMarket("r", snapshot(window(0, 3), { isCached: true }));
  expect(cachedMarket("r", "SHFE.rb2701", 60)).toBeUndefined();
  rememberMarket("r", snapshot(window(0, 3)));
  expect(cachedMarket("r", "SHFE.rb2701", 60)?.bars).toHaveLength(2);
  expect(cachedMarket("r", "SHFE.rb2701", 300)).toBeUndefined();
  expect(cachedMarket("other", "SHFE.rb2701", 60)).toBeUndefined();
});

test("the store keeps the most recently used charts within its size", async () => {
  for (const symbol of ["A", "B", "C"]) {
    rememberMarket("r", { ...snapshot(window(0, 50)), quote: { symbol, lastPrice: 1 } } as never, 1_000 + symbol.charCodeAt(0));
    await settle();
  }
  const db = (await cacheDb())!;
  const size = (await db.get("klineUse", "r|A|60"))!.size;
  await evict(size * 2 + 1); // room for two: the oldest (A) goes
  expect(await db.get("klines", "r|A|60")).toBeUndefined();
  expect(await db.get("klines", "r|C|60")).toBeDefined();
  resetMarketCacheForTests();
  expect(await loadCachedMarket("r", "A", 60)).toBeUndefined();
  expect((await loadCachedMarket("r", "B", 60))?.bars).toHaveLength(49);
});

test("the push: a focus offers the held bars as since, its first frame merges to the Runtime's window, a failed proof asks again whole", async () => {
  rememberMarket("https://rt", snapshot(window(0, 120)));
  // The focus offers the last closed bar held (the forming bar 119 was never kept).
  expect(await feedFocusSince("https://rt", "s1", "SHFE.rb2701", 60)).toBe(bar(118).datetime);
  const runtimeBars = [...window(2, 120), bar(120)];
  const merged = feedMarketFrame("s1", { ...since(runtimeBars, bar(118).datetime), revision: "r1" } as unknown as MarketWorkbenchSnapshot);
  expect(merged?.bars).toEqual(runtimeBars);
  expect(merged && ("barsAfter" in merged || "barsWindow" in merged)).toBe(false);
  expect((merged as unknown as { revision?: string }).revision).toBe("r1");
  // A whole-window frame (an older Runtime, an unknown since) is the chart itself.
  const whole = snapshot(runtimeBars);
  expect(feedMarketFrame("s1", whole)).toBe(whole);
  // The Runtime corrected a held bar: the proof fails, and the next focus asks without since.
  expect(await feedFocusSince("https://rt", "s2", "SHFE.rb2701", 60)).toBe(bar(118).datetime);
  const corrected = window(0, 121, index => index === 50 ? 1 : undefined);
  expect(feedMarketFrame("s2", since(corrected, bar(118).datetime) as MarketWorkbenchSnapshot)).toBeUndefined();
  expect(await feedFocusSince("https://rt", "s2", "SHFE.rb2701", 60)).toBeUndefined();
  // That whole-window focus never reached the Runtime: the refusal is kept for the next one.
  refuseFeedSince("https://rt", "SHFE.rb2701", 60);
  expect(await feedFocusSince("https://rt", "s2", "SHFE.rb2701", 60)).toBeUndefined();
  expect(await feedFocusSince("https://rt", "s2", "SHFE.rb2701", 60)).toBe(bar(118).datetime);
  // Nothing held: no since.
  expect(await feedFocusSince("https://rt", "s3", "SHFE.cu2701", 60)).toBeUndefined();
});

test("#850: held bars go once the focus's first frame answered, and when the window moves to another session", async () => {
  rememberMarket("https://rt", snapshot(window(0, 120)));
  expect(await feedFocusSince("https://rt", "s1", "SHFE.rb2701", 60)).toBe(bar(118).datetime);
  feedMarketFrame("s1", since([...window(2, 120), bar(120)], bar(118).datetime) as unknown as MarketWorkbenchSnapshot);
  expect(feedFocusHeldForTests("s1")).toBe(false);
  // Switching conversations: the earlier session's offered bars are let go, the current one's kept.
  await feedFocusSince("https://rt", "old", "SHFE.rb2701", 60);
  await feedFocusSince("https://rt", "new", "SHFE.rb2701", 60);
  resetFeed("new");
  expect(feedFocusHeldForTests("old")).toBe(false);
  expect(feedFocusHeldForTests("new")).toBe(true);
  resetFeed();
  expect(feedFocusHeldForTests("new")).toBe(false);
});


test("contract display names survive a reload, stay runtime-scoped and retain names across partial catalogs", async () => {
  const catalog = (symbol: string, instrumentName: string) => ({ items: [{ symbol, instrumentName }] }) as InstrumentCatalog;
  await rememberInstrumentNames("https://one", catalog("SHFE.rb2701", "螺纹2701"));
  await rememberInstrumentNames("https://two", catalog("SHFE.rb2701", "另一目录名称"));
  await rememberInstrumentNames("https://one", catalog("SHFE.au2612", "沪金2612"));
  const db = await cacheDb();
  db?.close();
  resetCacheDbForTests();
  expect(await loadInstrumentNames("https://one")).toEqual({ "SHFE.RB2701": "螺纹2701", "SHFE.AU2612": "沪金2612" });
  expect(await loadInstrumentNames("https://two")).toEqual({ "SHFE.RB2701": "另一目录名称" });
  expect(await loadInstrumentNames("https://unseen")).toEqual({});
  await clearCacheDb();
  expect(await loadInstrumentNames("https://one")).toEqual({});
});

test("blocked local storage does not prevent live contract names from being displayed", async () => {
  vi.stubGlobal("indexedDB", undefined);
  try {
    const catalog = { items: [{ symbol: "SHFE.rb2701", instrumentName: "螺纹2701" }] } as InstrumentCatalog;
    expect(await rememberInstrumentNames("https://one", catalog)).toEqual({ "SHFE.RB2701": "螺纹2701" });
    expect(await loadInstrumentNames("https://one")).toEqual({});
  } finally { vi.unstubAllGlobals(); }
});

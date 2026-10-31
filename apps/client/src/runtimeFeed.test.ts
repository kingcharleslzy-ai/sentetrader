import { renderHook, act } from "@testing-library/react";
import type { UiEvent } from "@ai-trader/ui-events";
import { applyFeedEvent, feedIsLive, markFeedStreamAlive, onFeedNudge, onMarketResync, onOrderFact, resetFeed, useRuntimeFeed } from "./runtimeFeed";

const event = (name: string, payload: unknown): UiEvent =>
  ({ schemaVersion: 1, id: name, sessionId: "s", kind: "result", name, timestamp: "now", payload } as unknown as UiEvent);
const marketPayload = (symbol = "SHFE.rb2610") =>
  ({ schemaVersion: 1, durationSeconds: 60, quote: { symbol, capturedAt: "2026-09-19T01:00:00Z" }, bars: [], liveTrading: "disabled" });

afterEach(() => resetFeed());

test("feed events land in the session's store and mark their own channel live; other events pass through", () => {
  const view = renderHook(() => useRuntimeFeed("s"));
  expect(view.result.current.channels.account).toBeUndefined();
  act(() => { expect(applyFeedEvent("s", event("feed.account", { account: { equity: 1, capturedAt: "2026-09-19T01:00:00Z" }, positions: [], activity: {}, risk: {} }), 1000)).toBe(true); });
  expect(view.result.current.account?.account.equity).toBe(1);
  expect(view.result.current.channels.account?.sourceAt).toBe("2026-09-19T01:00:00Z");
  expect(feedIsLive(view.result.current, "account", 5000)).toBe(true);
  expect(feedIsLive(view.result.current, "account", 20_000)).toBe(false);
  // An account push is not evidence about the market.
  expect(feedIsLive(view.result.current, "market", 5000)).toBe(false);
  expect(applyFeedEvent("s", event("message.completed", { text: "x" }))).toBe(false);
  // Another session sees nothing.
  expect(renderHook(() => useRuntimeFeed("other")).result.current.account).toBeUndefined();
});

test("a market that stopped 61 s ago is stale even while execution control keeps pushing", () => {
  // The external review's case: one shared "last event" made a 1 s old execution
  // event vouch for a quote nobody had updated for a minute, so the market poll
  // stayed suppressed and the workbench kept showing the old snapshot as current.
  const view = renderHook(() => useRuntimeFeed("s"));
  act(() => { applyFeedEvent("s", event("feed.market", marketPayload()), 0); });
  act(() => { applyFeedEvent("s", event("feed.execution", { stops: [], simulationOnly: true, liveTrading: "disabled" }), 60_000); });
  const now = 61_000;
  expect(feedIsLive(view.result.current, "execution", now)).toBe(true);
  expect(feedIsLive(view.result.current, "market", now)).toBe(false);
  // The last good quote is still there to render; only its freshness claim is gone.
  expect(view.result.current.market?.quote.symbol).toBe("SHFE.rb2610");
});

test("a channel the Runtime cannot read reports unavailable with its reason and stops counting as live", () => {
  const view = renderHook(() => useRuntimeFeed("s"));
  act(() => { applyFeedEvent("s", event("feed.market", marketPayload()), 1000); });
  expect(feedIsLive(view.result.current, "market", 2000)).toBe(true);
  act(() => { applyFeedEvent("s", event("feed.status", { channel: "market", status: "unavailable", reason: "market_quote_not_ready" }), 2000); });
  expect(view.result.current.channels.market).toEqual({ receivedAt: 2000, status: "unavailable", error: "market_quote_not_ready" });
  expect(feedIsLive(view.result.current, "market", 2000)).toBe(false);
  // The snapshot itself is kept so the panel has something to draw while the poll takes over.
  expect(view.result.current.market?.quote.symbol).toBe("SHFE.rb2610");
  act(() => { applyFeedEvent("s", event("feed.market", marketPayload()), 3000); });
  expect(feedIsLive(view.result.current, "market", 3000)).toBe(true);
});

test("a ui nudge wakes listeners without touching snapshots", () => {
  const nudged = vi.fn();
  const stop = onFeedNudge(nudged);
  applyFeedEvent("s", event("feed.market", marketPayload()), 1000);
  const before = renderHook(() => useRuntimeFeed("s")).result.current;
  applyFeedEvent("s", event("feed.ui", { reason: "command" }), 2000);
  expect(nudged).toHaveBeenCalledTimes(1);
  expect(renderHook(() => useRuntimeFeed("s")).result.current).toBe(before);
  stop();
});

test("a watched order's read-back change reaches its own session's listeners only (feed.order)", () => {
  // 2026-10-08: the Runtime pushes the new order view when the watch's read-back changes, so a
  // confirmed card's rows follow fills live. The listener hears which session the fact belongs to.
  const facts: Array<[string, Record<string, unknown>]> = [];
  const stop = onOrderFact((sessionId, payload) => facts.push([sessionId, payload as Record<string, unknown>]));
  const before = renderHook(() => useRuntimeFeed("s")).result.current;
  expect(applyFeedEvent("s", event("feed.order", { clientOrderId: "CO-1", accountId: "acc-1", displayStatus: "已成交", filledVolume: 2 }), 1000)).toBe(true);
  expect(facts).toEqual([["s", { clientOrderId: "CO-1", accountId: "acc-1", displayStatus: "已成交", filledVolume: 2 }]]);
  applyFeedEvent("other", event("feed.order", { clientOrderId: "CO-9", accountId: "acc-1", displayStatus: "已成交" }), 2000);
  expect(facts[1][0]).toBe("other");  // each fact names its session: the hook merges only its own
  expect(renderHook(() => useRuntimeFeed("s")).result.current).toBe(before);  // no snapshot channel: nothing re-rendered
  stop();
  applyFeedEvent("s", event("feed.order", { clientOrderId: "CO-2", accountId: "acc-1", displayStatus: "已成交" }), 3000);
  expect(facts).toHaveLength(2);
});

test("a quiet market stays live while its stream is alive, so nothing polls a closed market", () => {
  // The shared market is pushed on change only; on a closed market (2026-09-27, Sunday) the
  // silence made every window fall back to a full workbench read every 2.5 s.
  const view = renderHook(() => useRuntimeFeed("s"));
  act(() => { applyFeedEvent("s", event("feed.market", marketPayload()), 0); });
  act(() => { applyFeedEvent("s", event("feed.account", { account: { equity: 1 }, positions: [], activity: {}, risk: {} }), 0); });
  markFeedStreamAlive("s", 60_000);
  expect(feedIsLive(view.result.current, "market", 61_000)).toBe(true);
  // Only the change-pushed market: the other channels still need their own refresh.
  expect(feedIsLive(view.result.current, "account", 61_000)).toBe(false);
  // A stream that went quiet (not even heartbeats) no longer vouches for it.
  expect(feedIsLive(view.result.current, "market", 70_000)).toBe(false);
  // A failed read is reported on the channel and is never live.
  act(() => { applyFeedEvent("s", event("feed.status", { channel: "market", status: "unavailable", reason: "market_unavailable" }), 61_000); });
  markFeedStreamAlive("s", 62_000);
  expect(feedIsLive(view.result.current, "market", 62_000)).toBe(false);
});

test("a market patch updates only what changed on the frame it was built on; a gap asks for a full frame", () => {
  const view = renderHook(() => useRuntimeFeed("s"));
  const bar = (datetime: number, close: number) => ({ datetime, open: 1, high: 2, low: 0, close, volume: 1 });
  const resyncs: string[] = [];
  const stop = onMarketResync(id => resyncs.push(id));
  act(() => { applyFeedEvent("s", event("feed.market", { ...marketPayload(), revision: "1", bars: [bar(1, 10), bar(2, 11)] }), 0); });
  act(() => { applyFeedEvent("s", event("feed.market-patch", { revision: "2", base: "1", durationSeconds: 60, changes: {
    quote: { symbol: "SHFE.rb2610", lastPrice: 12, capturedAt: "2026-09-27T01:00:02Z" }, capturedAt: "2026-09-27T01:00:02Z",
    barsPatch: { bars: [bar(2, 12), bar(3, 12)], trimBefore: 2 } } }), 1000); });
  const market = view.result.current.market!;
  expect(market.revision).toBe("2");
  expect(market.quote.lastPrice).toBe(12);
  expect(market.bars.map(item => [item.datetime, item.close])).toEqual([[2, 12], [3, 12]]);
  expect(view.result.current.channels.market?.sourceAt).toBe("2026-09-27T01:00:02Z");
  expect(feedIsLive(view.result.current, "market", 1500)).toBe(true);
  // A patch on a base this client never got (a slow reader keeps only the latest market event).
  act(() => { applyFeedEvent("s", event("feed.market-patch", { revision: "4", base: "3", durationSeconds: 60, changes: { quote: { symbol: "SHFE.rb2610", lastPrice: 99 } } }), 2000); });
  expect(view.result.current.market?.quote.lastPrice).toBe(12);
  expect(resyncs).toEqual(["s"]);
  stop();
});

test("a focus's since frame that this session cannot prove is not drawn: the feed asks for the whole window", () => {
  const view = renderHook(() => useRuntimeFeed("s"));
  const resyncs: string[] = [];
  const stop = onMarketResync(id => resyncs.push(id));
  // No held bars were offered for this session (marketCache.feedFocusSince): nothing to merge with.
  act(() => { applyFeedEvent("s", event("feed.market", { ...marketPayload(), revision: "1",
    bars: [{ datetime: 3, open: 1, high: 2, low: 0, close: 1, volume: 1 }], barsAfter: 2,
    barsWindow: { first: 1, count: 3, digest: "00000000" } }), 0); });
  expect(view.result.current.market).toBeUndefined();
  expect(resyncs).toEqual(["s"]);
  stop();
});

test("a touch refreshes the stamps of the channel's last frame and keeps it live; before any frame it is ignored", () => {
  const view = renderHook(() => useRuntimeFeed("s"));
  const stamps = [[["capturedAt"], "2026-09-28T03:00:05Z"], [["ageSeconds"], 0.3]];
  act(() => { applyFeedEvent("s", event("feed.touch", { name: "feed.activity", stamps }), 0); });
  expect(view.result.current.activity).toBeUndefined();
  const orders = [{ clientOrderId: "o1" }];
  act(() => { applyFeedEvent("s", event("feed.activity", { environment: "simulation", symbol: "SHFE.rb2701", orders, fills: [], strategies: [], capturedAt: "2026-09-28T03:00:00Z", ageSeconds: 4 }), 1000); });
  act(() => { applyFeedEvent("s", event("feed.touch", { name: "feed.activity", stamps }), 20_000); });
  const activity = view.result.current.activity as unknown as Record<string, unknown>;
  expect(activity.capturedAt).toBe("2026-09-28T03:00:05Z");
  expect(activity.ageSeconds).toBe(0.3);
  expect(activity.orders).toEqual(orders);                             // the business content is untouched
  expect(view.result.current.channels.activity?.sourceAt).toBe("2026-09-28T03:00:05Z");
  expect(feedIsLive(view.result.current, "activity", 25_000)).toBe(true);
  // A nested stamp (the account overview's own capture time).
  act(() => { applyFeedEvent("s", event("feed.account", { account: { equity: 1, capturedAt: "a" }, positions: [], activity: {}, risk: {} }), 20_000); });
  act(() => { applyFeedEvent("s", event("feed.touch", { name: "feed.account", stamps: [[["account", "capturedAt"], "b"]] }), 26_000); });
  expect(view.result.current.account?.account.equity).toBe(1);
  expect(view.result.current.channels.account?.sourceAt).toBe("b");
});

import { act, renderHook } from "@testing-library/react";
import * as marketCache from "../marketCache";
import { resetFeedViewForTests } from "../feedView";
import { cachedMarket } from "../marketCache";
import { useRuntimeFeed } from "../runtimeFeed";
import type { MarketWorkbenchSnapshot } from "../api";
import { useMarketWorkbench } from "./useMarketWorkbench";
import { setChartPeriod } from "../strategyApi";
import { useRuntimeSession } from "./useRuntimeSession";
import { getMarketWorkbench, listInstruments, setFeedFocus, getOwnerAuthStatus, openEventStream, requestJson, resolveRuntimeConnection, sendMessage } from "../api";

vi.mock("../api", () => ({
  authenticateCloud: vi.fn(), authenticateLocal: vi.fn(), setOwnerSessionToken: vi.fn(),
  getOwnerAuthStatus: vi.fn(), openEventStream: vi.fn(), resolveRuntimeConnection: vi.fn(),
  getMarketWorkbench: vi.fn(() => new Promise(() => undefined)),
  listInstruments: vi.fn(async () => ({items: [], catalogStatus: "ready"})), setFeedFocus: vi.fn(),
  runtimeUrl: () => "https://runtime.test", sendMessage: vi.fn(), requestJson: vi.fn(),
}));
vi.mock("../runtimeConnection", () => ({setCloudAccessToken: vi.fn()}));

vi.mock("../strategyApi", () => ({setChartPeriod: vi.fn(), strategiesChanged: vi.fn(), strategyError: (error: unknown) => String(error)}));

beforeEach(() => {
  vi.useFakeTimers(); vi.resetAllMocks(); resetFeedViewForTests();
  vi.mocked(getMarketWorkbench).mockImplementation(() => new Promise(() => undefined));
  vi.mocked(listInstruments).mockResolvedValue({items: [], catalogStatus: "ready"} as never);
  vi.mocked(setFeedFocus).mockResolvedValue(undefined as never);
  vi.mocked(resolveRuntimeConnection).mockResolvedValue({baseUrl: "https://runtime.test", token: "",
    authMode: "owner-password", source: "web-env", liveTrading: false});
  vi.mocked(getOwnerAuthStatus).mockResolvedValue({authenticated: true, authMode: "owner-password", authProvider: "owner-password"});
  vi.mocked(requestJson).mockImplementation(async url => url.endsWith("/api/workspace") ? {scope: "owner"} : {sessionId: "restored"});
  vi.mocked(openEventStream).mockImplementation((_url, _session, _event, _warning, _activity, connected) => {
    connected?.(true); return vi.fn();
  });
});
afterEach(() => { resetFeedViewForTests(); vi.restoreAllMocks(); vi.useRealTimers(); });

test("startup transport failure recovers without sending any user operation", async () => {
  vi.mocked(getOwnerAuthStatus).mockRejectedValueOnce(new TypeError("Load failed"));
  const hook = renderHook(() => useRuntimeSession("cloud"));
  await act(async () => {});
  expect(hook.result.current.state.connectionError).toBe("应用服务暂时不可用，正在重试连接");
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(hook.result.current.connection).toBe("connected");
  expect(hook.result.current.sessionId).toBe("restored");
  expect(hook.result.current.state.connectionError).toBeUndefined();
  expect(sendMessage).not.toHaveBeenCalled();
  hook.unmount();
});

test("expired authentication requires login instead of retrying with the old session", async () => {
  vi.mocked(getOwnerAuthStatus).mockRejectedValue(Object.assign(new Error("unauthorized"), {status: 401}));
  const hook = renderHook(() => useRuntimeSession("cloud"));
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(hook.result.current.authRequired).toBe(true);
  expect(getOwnerAuthStatus).toHaveBeenCalledTimes(1);
  expect(openEventStream).not.toHaveBeenCalled();
  hook.unmount();
});

test("leaving a failed connection cancels its scheduled retry", async () => {
  vi.mocked(getOwnerAuthStatus).mockRejectedValue(Object.assign(new Error("unavailable"), {status: 503}));
  const hook = renderHook(() => useRuntimeSession("cloud"));
  await act(async () => {});
  hook.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(getOwnerAuthStatus).toHaveBeenCalledTimes(1);
  expect(openEventStream).not.toHaveBeenCalled();
});

// #710 P2: the real requestJson → withRequestTimeout path; only fetch is controlled.
async function realRequestJson() {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  vi.mocked(requestJson).mockImplementation(actual.requestJson);
}
function controlledRuntime() {
  let healthy = false;
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((resolve, reject) => {
    const signal = init?.signal;
    signal?.addEventListener("abort", () => reject(signal.reason ?? new DOMException("Aborted", "AbortError")));
    if (!healthy) return;   // a service that accepts the connection but never answers
    resolve(Response.json(String(input).endsWith("/api/workspace") ? {scope: "owner"} : {sessionId: "restored"}));
  }));
  vi.stubGlobal("fetch", fetchMock);
  return {fetchMock, recover: () => { healthy = true; }, pending: () => fetchMock.mock.calls.filter(([, init]) => !init?.signal?.aborted).length};
}

test("a read that times out keeps retrying with backoff and reconnects once the service answers again", async () => {
  localStorage.clear();
  await realRequestJson();
  const runtime = controlledRuntime();
  const hook = renderHook(() => useRuntimeSession("cloud"));
  await act(async () => {});
  expect(runtime.fetchMock).toHaveBeenCalledTimes(1);   // GET /api/workspace, hanging
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(hook.result.current.connection).toBe("disconnected");
  expect(hook.result.current.authRequired).toBe(false);
  expect(hook.result.current.state.connectionError).toBe("应用服务暂时没有响应，正在重试连接");
  // Second attempt after 1 s also hangs: never two attempts in flight, the next waits 2 s after it times out.
  await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
  expect(runtime.fetchMock).toHaveBeenCalledTimes(2);
  expect(runtime.pending()).toBe(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000 + 1_999); });
  expect(runtime.fetchMock).toHaveBeenCalledTimes(2);
  runtime.recover();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(runtime.fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? "GET"} ${new URL(String(url)).pathname}`))
    .toEqual(["GET /api/workspace", "GET /api/workspace", "GET /api/workspace", "POST /api/sessions/restore"]);
  expect(hook.result.current.connection).toBe("connected");
  expect(hook.result.current.sessionId).toBe("restored");
  expect(hook.result.current.state.connectionError).toBeUndefined();
  expect(sendMessage).not.toHaveBeenCalled();
  hook.unmount();
  vi.unstubAllGlobals();
});

test("unmounting during a hung or pending attempt sends nothing afterwards", async () => {
  localStorage.clear();
  await realRequestJson();
  const runtime = controlledRuntime();
  const hung = renderHook(() => useRuntimeSession("cloud"));
  await act(async () => {});
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });   // timed out, retry scheduled
  hung.unmount();
  runtime.recover();
  await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
  expect(runtime.fetchMock).toHaveBeenCalledTimes(1);
  // Unmounted while the identity check is still pending: its late answer starts no workspace read.
  let identity!: () => void;
  vi.mocked(getOwnerAuthStatus).mockImplementationOnce(() => new Promise(resolve => {
    identity = () => resolve({authenticated: true, authMode: "owner-password", authProvider: "owner-password"});
  }));
  const pending = renderHook(() => useRuntimeSession("cloud"));
  await act(async () => {});
  pending.unmount();
  await act(async () => { identity(); await vi.advanceTimersByTimeAsync(120_000); });
  expect(runtime.fetchMock).toHaveBeenCalledTimes(1);
  expect(openEventStream).not.toHaveBeenCalled();
  vi.unstubAllGlobals();
});

test("new chat changes the session without deleting the old one and ignores its late stream events", async () => {
  localStorage.clear();
  const hook = renderHook(() => useRuntimeSession("cloud"));
  await act(async () => {});
  const oldEvent = vi.mocked(openEventStream).mock.calls[0][2];
  vi.mocked(requestJson).mockResolvedValueOnce({sessionId:"new-chat"});
  await act(async () => { await hook.result.current.changeChat(); });
  expect(hook.result.current.sessionId).toBe("new-chat");
  act(() => oldEvent({name:"message.completed",payload:{turnId:"late",role:"assistant",text:"旧对话的迟到回复"},timestamp:new Date().toISOString()} as any));
  expect(hook.result.current.state.turns).toHaveLength(0);
  expect(vi.mocked(requestJson).mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  await act(async () => { await hook.result.current.submitMessage("新问题"); });
  expect(sendMessage).toHaveBeenCalledWith("https://runtime.test","new-chat","新问题",undefined);
  hook.unmount();
});

test("a failed new chat preserves the current session and never retries the create", async () => {
  localStorage.clear();
  const hook = renderHook(() => useRuntimeSession("cloud"));
  await act(async () => {});
  vi.mocked(requestJson).mockRejectedValueOnce(new TypeError("offline"));
  await act(async () => { await expect(hook.result.current.changeChat()).rejects.toThrow("offline"); });
  expect(hook.result.current.sessionId).toBe("restored");
  expect(openEventStream).toHaveBeenCalledTimes(1);
  hook.unmount();
});

test("a stream drop that reconnects within three seconds stays invisible", async () => {
  const hook = renderHook(() => useRuntimeSession("cloud"));
  await act(async () => {});
  const reportConnection = vi.mocked(openEventStream).mock.calls[0][5];
  act(() => reportConnection(false));
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(hook.result.current.connection).toBe("connected");
  act(() => reportConnection(true));
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(hook.result.current.connection).toBe("connected");
  hook.unmount();
});

test("a stream drop longer than three seconds announces the outage until recovery", async () => {
  const hook = renderHook(() => useRuntimeSession("cloud"));
  await act(async () => {});
  const reportConnection = vi.mocked(openEventStream).mock.calls[0][5];
  act(() => reportConnection(false));
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(hook.result.current.connection).toBe("disconnected");
  act(() => reportConnection(true));
  expect(hook.result.current.connection).toBe("connected");
  hook.unmount();
});

// The chat and stream callbacks are the formal handle producers; only HTTP/stream boundaries are controlled.
test.each(["chat", "reconnect"].flatMap(transition => ["success", "refused", "response-lost"].map(outcome => ({transition, outcome}))))(
  "$transition preserves actual strategy-write order and resends the latest chart target ($outcome)", async ({transition, outcome}) => {
    localStorage.clear();
    localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
    let storedPeriod = 0;
    const rebind = vi.mocked(setChartPeriod).mockImplementation(async (_url, _symbol, period) => {
      storedPeriod = period; return {strategyIds: []} as never;
    });
    const toast = vi.fn();
    const hook = renderHook(() => {
      const runtime = useRuntimeSession("cloud");
      const market = useMarketWorkbench({handle: {...runtime, setToast: toast} as never,
        marketStatus: "normal" as never, feed: {channels: {}} as never, chartTimeframe: 60});
      return {runtime, market};
    });
    await act(async () => {});
    expect(storedPeriod).toBe(60);
    let finish!: () => void;
    rebind.mockImplementationOnce((_url, _symbol, period) => new Promise((resolve, reject) => {
      finish = () => {
        // Lost transport is not cancellation: this case has committed 300 but lost its reply.
        if (outcome !== "refused") storedPeriod = period;
        if (outcome === "success") resolve({strategyIds: []} as never);
        else reject(outcome === "refused" ? Object.assign(new Error("旧周期被拒绝"), {status: 409}) : new TypeError("Failed to fetch"));
      };
    }));
    let old!: Promise<void> | undefined, queued!: Promise<void> | undefined;
    await act(async () => { old = hook.result.current.market.changeChartPeriod(300); });
    act(() => {queued = hook.result.current.market.changeChartPeriod(900);});
    if (transition === "chat") {
      vi.mocked(requestJson).mockResolvedValueOnce({sessionId: "new-chat"});
      await act(async () => {await hook.result.current.runtime.changeChat();});
      expect(hook.result.current.runtime.sessionId).toBe("new-chat");
    } else {
      const report = vi.mocked(openEventStream).mock.calls[0][5];
      act(() => report(false));
      await act(async () => {await vi.advanceTimersByTimeAsync(3000);});
      act(() => report(true));
      await act(async () => {});
    }
    expect(hook.result.current.runtime.connection).toBe("connected");
    expect(hook.result.current.market.duration).toBe(900);
    expect(rebind.mock.calls.map(([, , period]) => period)).toEqual([60, 300]);
    await act(async () => {finish(); await old; await queued;});
    expect(rebind.mock.calls.map(([, , period]) => period)).toEqual([60, 300, 900]);
    expect(storedPeriod).toBe(900);
    expect(toast).not.toHaveBeenCalled();
    hook.unmount(); localStorage.clear();
});

test("the formal Runtime reconnect retires its feed and never remembers old market under the new Runtime", async () => {
  localStorage.clear(); localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  vi.mocked(setChartPeriod).mockResolvedValue({strategyIds: []} as never);
  const snapshot = (last: number) => ({quote: {symbol: "SHFE.rb2701", lastPrice: last}, bars: [{close: last}],
    dataStatus: "normal", durationSeconds: 60}) as MarketWorkbenchSnapshot;
  const hook = renderHook(({reconnect}) => {
    const runtime = useRuntimeSession("cloud", reconnect);
    const feed = useRuntimeFeed(runtime.sessionId);
    const market = useMarketWorkbench({handle: {...runtime, setToast: vi.fn()} as never,
      marketStatus: "normal" as never, feed, chartTimeframe: 60});
    return {runtime, market};
  }, {initialProps: {reconnect: 0}});
  await act(async () => {});
  const oldEvent = vi.mocked(openEventStream).mock.calls[0][2];
  act(() => oldEvent({name: "feed.market", payload: snapshot(111), timestamp: new Date().toISOString()} as never));
  expect(hook.result.current.market.market?.quote.lastPrice).toBe(111);
  expect(cachedMarket("https://runtime.test", "SHFE.rb2701", 60)?.quote.lastPrice).toBe(111);
  vi.mocked(resolveRuntimeConnection).mockResolvedValueOnce({baseUrl: "https://other-runtime.test", token: "",
    authMode: "owner-password", source: "web-env", liveTrading: false});
  let attach!: () => void;
  vi.mocked(requestJson).mockImplementation(async url => {
    if (url.endsWith("/api/workspace")) return {scope: "owner"};
    if (url.startsWith("https://other-runtime.test")) return new Promise(resolve => {attach = () => resolve({sessionId: "restored"});});
    return {sessionId: "restored"};
  });
  hook.rerender({reconnect: 1}); await act(async () => {});
  // baseUrl changes before restore/attach completes; the old feed is still stored, but inactive.
  expect(hook.result.current.runtime.connection).toBe("connecting");
  expect(hook.result.current.market.market).toBeUndefined();
  expect(cachedMarket("https://other-runtime.test", "SHFE.rb2701", 60)).toBeUndefined();
  await act(async () => {attach();});
  expect(hook.result.current.runtime.baseUrl).toBe("https://other-runtime.test");
  expect(hook.result.current.runtime.connection).toBe("connected");
  expect(hook.result.current.market.market).toBeUndefined();
  // Late old SSE is rejected by the original stream epoch before it can enter the feed.
  act(() => oldEvent({name: "feed.market", payload: snapshot(112), timestamp: new Date().toISOString()} as never));
  expect(hook.result.current.market.market).toBeUndefined();
  expect(cachedMarket("https://other-runtime.test", "SHFE.rb2701", 60)).toBeUndefined();
  const newEvent = vi.mocked(openEventStream).mock.calls[1][2];
  act(() => newEvent({name: "feed.market", payload: snapshot(222), timestamp: new Date().toISOString()} as never));
  expect(hook.result.current.market.market?.quote.lastPrice).toBe(222);
  expect(cachedMarket("https://other-runtime.test", "SHFE.rb2701", 60)?.quote.lastPrice).toBe(222);
  hook.unmount(); localStorage.clear();
});

function focusedSession() {
  localStorage.clear();
  localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  return renderHook(() => {
    const runtime = useRuntimeSession("cloud");
    useMarketWorkbench({handle: {...runtime, setToast: vi.fn()} as never,
      marketStatus: "normal" as never, feed: {channels: {}} as never, chartTimeframe: 60});
    return runtime;
  });
}
const viewCalls = () => vi.mocked(requestJson).mock.calls.filter(([url]) => url.endsWith("/feed/view"));

test("a quick SSE reconnect replays focus and view once while connection stays connected", async () => {
  const hook = focusedSession();
  await act(async () => {});
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(setFeedFocus).toHaveBeenCalledTimes(1);
  expect(viewCalls()).toHaveLength(1);
  const report = vi.mocked(openEventStream).mock.calls[0][5];
  act(() => report(false));
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(hook.result.current.connection).toBe("connected");
  act(() => report(true));
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(setFeedFocus).toHaveBeenCalledTimes(2);
  expect(setFeedFocus).toHaveBeenLastCalledWith("https://runtime.test", "restored", "SHFE.rb2701", 60, undefined);
  expect(viewCalls()).toHaveLength(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(setFeedFocus).toHaveBeenCalledTimes(2);
  expect(viewCalls()).toHaveLength(2);
  expect(hook.result.current.connection).toBe("connected");
  hook.unmount();
});

test("retired stream callbacks and a pending old focus never register the old session", async () => {
  let finishSince!: (since: undefined) => void;
  vi.spyOn(marketCache, "feedFocusSince").mockImplementationOnce(() => new Promise(resolve => { finishSince = resolve; }));
  const hook = focusedSession();
  await act(async () => {});
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  const oldReport = vi.mocked(openEventStream).mock.calls[0][5];
  vi.mocked(requestJson).mockResolvedValueOnce({sessionId: "new-chat"});
  await act(async () => { await hook.result.current.changeChat(); });
  act(() => { oldReport(false); oldReport(true); });
  await act(async () => { finishSince(undefined); await vi.advanceTimersByTimeAsync(2); });
  expect(setFeedFocus).toHaveBeenCalledTimes(1);
  expect(vi.mocked(setFeedFocus).mock.calls[0][1]).toBe("new-chat");
  expect(viewCalls().map(([url]) => url)).toEqual([
    "https://runtime.test/api/sessions/restored/feed/view", "https://runtime.test/api/sessions/new-chat/feed/view"]);
  hook.unmount();
});

test("failed focus and view retry with the existing two-second backoff", async () => {
  const hook = focusedSession();
  await act(async () => {});
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  vi.mocked(setFeedFocus).mockRejectedValueOnce(new TypeError("offline"));
  vi.mocked(requestJson).mockRejectedValueOnce(new TypeError("offline"));
  const report = vi.mocked(openEventStream).mock.calls[0][5];
  act(() => { report(false); report(true); });
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(setFeedFocus).toHaveBeenCalledTimes(2);
  expect(viewCalls()).toHaveLength(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(1998); });
  expect(setFeedFocus).toHaveBeenCalledTimes(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(2); });
  expect(setFeedFocus).toHaveBeenCalledTimes(3);
  expect(viewCalls()).toHaveLength(3);
  hook.unmount();
});

test("a reconnect while registration is in flight cannot be acknowledged by its late reply", async () => {
  const hook = focusedSession();
  await act(async () => {});
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  let finishFocus!: () => void, finishView!: () => void;
  vi.mocked(setFeedFocus).mockImplementationOnce(() => new Promise(resolve => { finishFocus = () => resolve(undefined as never); }));
  vi.mocked(requestJson).mockImplementationOnce(() => new Promise(resolve => { finishView = () => resolve({}); }));
  const report = vi.mocked(openEventStream).mock.calls[0][5];
  act(() => { report(false); report(true); });
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  act(() => { report(false); report(true); });
  await act(async () => { await vi.advanceTimersByTimeAsync(1); finishFocus(); finishView(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(2); });
  expect(setFeedFocus).toHaveBeenCalledTimes(3);
  expect(viewCalls()).toHaveLength(3);
  hook.unmount();
});

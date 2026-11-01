import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { marketErrorMessage, useMarketWorkbench } from "./useMarketWorkbench";
import { resetReadIdentity } from "../runtimeClient";
import { useTradingAccounts } from "./useTradingAccounts";
import { rememberMarket } from "../marketCache";
import type { MarketWorkbenchSnapshot } from "../api";
import { activeTradingAccount, setActiveTradingAccount } from "../tradingAccountScope";

test("a contract's own condition gets its own words; everything else is the generic unavailable message", () => {
  const httpError = (detail: unknown) => Object.assign(new Error("503"), { status: 503, detail });
  expect(marketErrorMessage(httpError({ detail: "market_series_empty" }))).toBe("该合约上市以来没有成交，暂无行情");
  expect(marketErrorMessage(httpError({ detail: "market_quote_not_ready" }))).toBe("该合约暂无报价，等待下次行情更新");
  expect(marketErrorMessage(httpError({ detail: "market_subscription_limit" }))).toBe("订阅数已满，稍后重试");
  expect(marketErrorMessage(httpError({ detail: "sidecar_market_workbench_unavailable" }))).toBe("当前行情暂时不可用");
  expect(marketErrorMessage(httpError(undefined))).toBe("当前行情暂时不可用");
  expect(marketErrorMessage(new TypeError("Failed to fetch"))).toBe("当前行情暂时不可用");
});

// 2026-09-25 (holiday close): a strategy that follows the chart could not rebind on stale quotes, and the
// chart refused to switch to 5 minutes with it. The chart now switches first; the rebind only warns.
vi.mock("../strategyApi", () => ({
  setChartPeriod: vi.fn(),
  strategiesChanged: vi.fn(),
  strategyError: (error: unknown, fallback: string) => (error instanceof Error ? error.message : fallback),
}));
vi.mock("../api", async () => ({
  setActiveTradingAccount: (await import("../tradingAccountScope")).setActiveTradingAccount,
  fetchAccountDirectory: vi.fn(async () => ({accounts: []})), setSessionTradingAccount: vi.fn(),
  getMarketWorkbench: vi.fn(() => new Promise(() => undefined)),
  listInstruments: vi.fn(async () => ({ items: [], catalogStatus: "ready" })),
  setFeedFocus: vi.fn(async () => undefined),
}));
vi.mock("../runtimeFeed", () => ({ feedIsLive: () => false, onMarketResync: () => () => {} }));

test("the chart switches period even when the strategies following it cannot rebind yet", async () => {
  const { setChartPeriod } = await import("../strategyApi");
  const rebind = vi.mocked(setChartPeriod);
  rebind.mockResolvedValue({ strategyIds: [] } as never);
  localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  const setToast = vi.fn();
  const handle = { baseUrl: "http://runtime", connection: "connected", sessionId: "s1", setToast } as never;
  const { result } = renderHook(() => useMarketWorkbench({ handle, marketStatus: "normal" as never, feed: {} as never, chartTimeframe: 60 }));
  await waitFor(() => expect(rebind).toHaveBeenCalled());
  rebind.mockRejectedValueOnce(new Error("周期尚未切换：5 分钟：所需周期行情正在连接或已过期，请刷新行情后重试"));
  await act(async () => { await result.current.changeChartPeriod(300); });
  expect(result.current.duration).toBe(300);
  expect(setToast).toHaveBeenCalledWith(expect.stringMatching(/^图表已切换；跟随图表周期的策略暂未切换：/), "warning");
  localStorage.clear();
});

test("a 30 分 or 周线 view leaves the strategies that follow the chart on their period, without a warning", async () => {
  // 10-02 本机验收：切到周线弹“跟随图表周期的策略暂未切换：无法连接应用服务”——策略不支持该周期。
  const { setChartPeriod } = await import("../strategyApi");
  const rebind = vi.mocked(setChartPeriod);
  rebind.mockResolvedValue({ strategyIds: [] } as never);
  localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  const setToast = vi.fn();
  const handle = { baseUrl: "http://runtime", connection: "connected", sessionId: "s1", setToast } as never;
  const { result } = renderHook(() => useMarketWorkbench({ handle, marketStatus: "normal" as never, feed: {} as never, chartTimeframe: 60 }));
  await waitFor(() => expect(rebind).toHaveBeenCalled());
  rebind.mockClear();
  await act(async () => { await result.current.changeChartPeriod(604800 as never); });
  await act(async () => { await result.current.changeChartPeriod(1800 as never); });
  expect(result.current.duration).toBe(1800);
  expect(rebind).not.toHaveBeenCalled();
  expect(setToast).not.toHaveBeenCalled();
  localStorage.clear();
});

test("a replay's contract is not remembered as the live chart's contract", async () => {
  localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  const handle = { baseUrl: "http://runtime/replay/r1", connection: "connected", sessionId: "s1", replayPath: "/replay/r1", setToast: vi.fn() } as never;
  const { result } = renderHook(() => useMarketWorkbench({ handle, marketStatus: "normal" as never, feed: {} as never, replaySymbol: "SHFE.rb2401", chartTimeframe: 60 }));
  expect(result.current.symbol).toBe("SHFE.rb2401");
  await act(async () => { await Promise.resolve(); });
  expect(localStorage.getItem("ai-trader:selected-instrument")).toBe("SHFE.rb2701");
  localStorage.clear();
});

test("opening another contract and period together uses that contract even behind a pending strategy reply", async () => {
  const { setChartPeriod } = await import("../strategyApi");
  const rebind = vi.mocked(setChartPeriod);
  rebind.mockClear().mockResolvedValue({ strategyIds: [] } as never);
  localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  const handle = { baseUrl: "http://runtime", connection: "connected", sessionId: "s1", setToast: vi.fn() } as never;
  const { result } = renderHook(() => useMarketWorkbench({ handle, marketStatus: "normal" as never, feed: {} as never, chartTimeframe: 60 }));
  await waitFor(() => expect(rebind).toHaveBeenCalled());
  let finish!: (value: never) => void;
  rebind.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  let previous!: Promise<void> | undefined;
  act(() => { previous = result.current.changeChartPeriod(180); });
  await waitFor(() => expect(finish).toBeDefined());
  let opened!: Promise<void> | undefined;
  act(() => {
    result.current.selectSymbol("SHFE.au2612");
    opened = result.current.changeChartPeriod(900);
  });
  // Viewing a new period must not wait for the old contract's strategy response.
  expect(result.current.symbol).toBe("SHFE.au2612");
  expect(result.current.duration).toBe(900);
  await act(async () => { finish({ strategyIds: [] } as never); await previous; await opened; });
  await waitFor(() => expect(rebind).toHaveBeenCalledWith("http://runtime", "SHFE.au2612", 900));
  expect(rebind.mock.calls.filter(([, symbol, duration]) => symbol === "SHFE.au2612" && duration === 900)).toHaveLength(1);
  expect(result.current.duration).toBe(900);
  localStorage.clear();
});

test("an offline period pick explains why it did not change and never writes strategies", async () => {
  const { setChartPeriod } = await import("../strategyApi");
  const rebind = vi.mocked(setChartPeriod);
  rebind.mockClear();
  localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  const setToast = vi.fn();
  const handle = { baseUrl: "http://runtime", connection: "disconnected", setToast } as never;
  const { result } = renderHook(() => useMarketWorkbench({ handle, marketStatus: "normal" as never, feed: {} as never, chartTimeframe: 60 }));
  await act(async () => { await result.current.changeChartPeriod(300); });
  expect(result.current.duration).toBe(60);
  expect(setToast).toHaveBeenCalledWith(expect.stringMatching(/连接.*周期|周期.*连接/), "warning");
  expect(rebind).not.toHaveBeenCalled();
  localStorage.clear();
});

test.each([true, false])("an older period reply can finish in order without notifying the newer target (success=%s)", async success => {
  const { setChartPeriod } = await import("../strategyApi");
  const rebind = vi.mocked(setChartPeriod);
  rebind.mockClear().mockResolvedValue({ strategyIds: [] } as never);
  localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  const setToast = vi.fn();
  const changed = vi.mocked((await import("../strategyApi")).strategiesChanged);
  const handle = { baseUrl: "http://runtime", connection: "connected", sessionId: "s1", setToast } as never;
  const { result } = renderHook(() => useMarketWorkbench({ handle, marketStatus: "normal" as never, feed: {} as never, chartTimeframe: 60 }));
  await waitFor(() => expect(rebind).toHaveBeenCalled());
  changed.mockClear();
  let finish!: () => void;
  rebind.mockImplementationOnce(() => new Promise((resolve, reject) => {
    finish = () => success ? resolve({ strategyIds: [] } as never) : reject(new Error("300 的旧失败"));
  }));
  let first!: Promise<void> | undefined, second!: Promise<void> | undefined;
  act(() => { first = result.current.changeChartPeriod(300); });
  await waitFor(() => expect(finish).toBeDefined());
  act(() => { second = result.current.changeChartPeriod(900); });
  expect(result.current.duration).toBe(900);
  await act(async () => { finish(); await first; await second; });
  expect(rebind.mock.calls.slice(-2).map(([, , duration]) => duration)).toEqual([300, 900]);
  expect(setToast).not.toHaveBeenCalled();
  expect(changed).toHaveBeenCalledTimes(1);
  localStorage.clear();
});

test.each(["account", "login"] as const)("a queued strategy write retires on %s generation change", async owner => {
  const { setChartPeriod } = await import("../strategyApi");
  const rebind = vi.mocked(setChartPeriod);
  rebind.mockClear().mockResolvedValue({ strategyIds: [] } as never);
  setActiveTradingAccount("A");
  localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  const setToast = vi.fn();
  const handle = { baseUrl: "http://runtime", connection: "connected", sessionId: "s1", setToast } as never;
  const { result } = renderHook(() => useMarketWorkbench({ handle, marketStatus: "normal" as never, feed: {} as never, chartTimeframe: 60 }));
  await waitFor(() => expect(rebind).toHaveBeenCalled());
  rebind.mockClear();
  let finish!: () => void;
  rebind.mockImplementationOnce(() => new Promise(resolve => { finish = () => resolve({ strategyIds: [] } as never); }));
  let first!: Promise<void> | undefined, queued!: Promise<void> | undefined;
  act(() => { first = result.current.changeChartPeriod(300); });
  await waitFor(() => expect(finish).toBeDefined());
  act(() => {
    queued = result.current.changeChartPeriod(900);
    if (owner === "account") { setActiveTradingAccount("B"); setActiveTradingAccount("A"); }
    else resetReadIdentity();
  });
  await act(async () => { finish(); await first; await queued; });
  expect(rebind.mock.calls.map(([, , duration]) => duration)).toEqual([300]);
  expect(setToast).not.toHaveBeenCalled();
  setActiveTradingAccount(null);
  localStorage.clear();
});

test.each(["symbol", "period"] as const)("a cached new %s never inherits the previous chart error", async transition => {
  const {getMarketWorkbench} = await import("../api");
  const read = vi.mocked(getMarketWorkbench);
  read.mockRejectedValueOnce(Object.assign(new Error("503"), {detail: "market_series_empty"}));
  const {setChartPeriod} = await import("../strategyApi");
  vi.mocked(setChartPeriod).mockResolvedValue({strategyIds: []} as never);
  localStorage.clear(); localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  const nextSymbol = transition === "symbol" ? "DCE.m2701" : "SHFE.rb2701";
  const nextPeriod = transition === "period" ? 300 : 60;
  rememberMarket("http://cache-error-runtime", {quote: {symbol: nextSymbol, lastPrice: 2900},
    bars: [{close: 2900}], dataStatus: "normal", durationSeconds: nextPeriod} as MarketWorkbenchSnapshot);
  const handle = {baseUrl: "http://cache-error-runtime", connection: "connected", sessionId: "s1", setToast: vi.fn()} as never;
  const hook = renderHook(() => useMarketWorkbench({handle, marketStatus: "normal" as never, feed: {} as never, chartTimeframe: 60}));
  await waitFor(() => expect(hook.result.current.marketError).toMatch(/没有成交/));
  act(() => { if (transition === "symbol") hook.result.current.selectSymbol(nextSymbol);
    else void hook.result.current.changeChartPeriod(nextPeriod); });
  expect(hook.result.current.market?.quote.lastPrice).toBe(2900);
  expect(hook.result.current.marketError).toBeUndefined();
  // A new target's own failure must still be reported, without clearing its cached bars.
  read.mockRejectedValueOnce(Object.assign(new Error("503"), {detail: "market_quote_not_ready"}));
  act(() => hook.result.current.refreshMarket());
  await waitFor(() => expect(hook.result.current.marketError).toMatch(/暂无报价/));
  expect(hook.result.current.market?.quote.lastPrice).toBe(2900);
  hook.unmount(); localStorage.clear();
});

test.each(["account", "runtime"] as const)("independent %s writes proceed while returning to the original owner keeps its pending tail", async transition => {
  const {setChartPeriod} = await import("../strategyApi");
  const rebind = vi.mocked(setChartPeriod);
  const sent: {url: string; account: string | null; period: number}[] = [];
  const stored = new Map<string, number>();
  const owner = (url: string, account: string | null) => `${url}|${account}`;
  rebind.mockClear().mockImplementation(async (url, _symbol, period) => {
    const account = activeTradingAccount(); sent.push({url, account, period});
    stored.set(owner(url, account), period); return {strategyIds: []} as never;
  });
  setActiveTradingAccount("A"); localStorage.clear(); localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  const toast = vi.fn();
  const hook = renderHook(({url}) => useMarketWorkbench({handle: {baseUrl: url, connection: "connected", sessionId: "s1", setToast: toast} as never,
    marketStatus: "normal" as never, feed: {} as never, chartTimeframe: 60}), {initialProps: {url: "http://runtime-a"}});
  await act(async () => {});
  let finish!: () => void;
  rebind.mockImplementationOnce((url, _symbol, period) => new Promise(resolve => {
    const account = activeTradingAccount(); sent.push({url, account, period});
    finish = () => {stored.set(owner(url, account), period); resolve({strategyIds: []} as never);};
  }));
  let pending!: Promise<void> | undefined, retired!: Promise<void> | undefined;
  await act(async () => {pending = hook.result.current.changeChartPeriod(300);});
  act(() => {retired = hook.result.current.changeChartPeriod(900);});
  if (transition === "account") act(() => setActiveTradingAccount("B"));
  else hook.rerender({url: "http://runtime-b"});
  await act(async () => {void hook.result.current.changeChartPeriod(180);});
  const otherUrl = transition === "runtime" ? "http://runtime-b" : "http://runtime-a";
  const otherAccount = transition === "account" ? "B" : "A";
  expect(stored.get(owner(otherUrl, otherAccount))).toBe(180); // A is still unresolved, not timed out or cancelled.
  if (transition === "account") act(() => setActiveTradingAccount("A"));
  else hook.rerender({url: "http://runtime-a"});
  await act(async () => {void hook.result.current.changeChartPeriod(900);});
  expect(sent.filter(call => call.url === "http://runtime-a" && call.account === "A").map(call => call.period)).toEqual([60, 300]);
  await act(async () => {finish(); await pending; await retired;});
  expect(sent.filter(call => call.url === "http://runtime-a" && call.account === "A").map(call => call.period))
    .toEqual(transition === "runtime" ? [60, 300, 180, 900] : [60, 300, 900]);
  // Runtime return synchronizes the then-current 180 before the explicit 900; the old queued 900 retired.
  expect(sent.filter(call => call.url === "http://runtime-a" && call.account === "A" && call.period === 900)).toHaveLength(1);
  expect(stored.get(owner("http://runtime-a", "A"))).toBe(900);
  expect(toast).not.toHaveBeenCalled();
  hook.unmount(); setActiveTradingAccount(null); localStorage.clear();
});

test("changing Runtime synchronously excludes its previous market even while the new read is pending or fails", async () => {
  const {getMarketWorkbench} = await import("../api");
  vi.mocked(getMarketWorkbench).mockResolvedValueOnce({quote: {symbol: "SHFE.rb2701", lastPrice: 111}, bars: [{close: 111}],
    dataStatus: "normal", durationSeconds: 60} as MarketWorkbenchSnapshot);
  localStorage.clear(); localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  const hook = renderHook(({url}) => useMarketWorkbench({handle: {baseUrl: url, connection: "connected", sessionId: "s1", setToast: vi.fn()} as never,
    marketStatus: "normal" as never, feed: {} as never, chartTimeframe: 60}), {initialProps: {url: "http://market-origin-a"}});
  await waitFor(() => expect(hook.result.current.market?.quote.lastPrice).toBe(111));
  hook.rerender({url: "http://market-origin-b"});
  expect(hook.result.current.market).toBeUndefined();
  vi.mocked(getMarketWorkbench).mockRejectedValueOnce(Object.assign(new Error("503"), {detail: "market_quote_not_ready"}));
  act(() => hook.result.current.refreshMarket());
  await waitFor(() => expect(hook.result.current.marketError).toMatch(/暂无报价/));
  expect(hook.result.current.market).toBeUndefined();
  vi.mocked(getMarketWorkbench).mockResolvedValueOnce({quote: {symbol: "SHFE.rb2701", lastPrice: 222}, bars: [{close: 222}],
    dataStatus: "normal", durationSeconds: 60} as MarketWorkbenchSnapshot);
  act(() => hook.result.current.refreshMarket());
  await waitFor(() => expect(hook.result.current.market?.quote.lastPrice).toBe(222));
  expect(hook.result.current.marketError).toBeUndefined();
  hook.unmount(); localStorage.clear();
});

test.each(["unknown", "A"])("an unresolved default-account request and known account wait each other (initial=%s)", async initial => {
  const {setChartPeriod} = await import("../strategyApi");
  const rebind = vi.mocked(setChartPeriod).mockClear().mockResolvedValue({strategyIds: []} as never);
  const handle = {baseUrl: "http://unresolved-account", connection: "connected", sessionId: "s1", setToast: vi.fn()} as never;
  setActiveTradingAccount(null); localStorage.clear(); localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  const hook = renderHook(({id}) => {
    // The original status consumer, including its absent/first-status boundary, owns the header.
    useTradingAccounts("http://unresolved-account", "s1", id ? {account: {id, status: "ready"}} as never : undefined);
    return useMarketWorkbench({handle, marketStatus: "normal" as never, feed: {} as never, chartTimeframe: 60});
  }, {initialProps: {id: initial === "unknown" ? undefined : "A" as string | undefined}});
  await act(async () => {}); rebind.mockClear();
  let finish!: () => void;
  rebind.mockImplementationOnce(() => new Promise(resolve => {finish = () => resolve({strategyIds: []} as never);}));
  let pending!: Promise<void> | undefined;
  await act(async () => {pending = hook.result.current.changeChartPeriod(300);});
  hook.rerender({id: initial === "unknown" ? "A" : undefined});
  let latest!: Promise<void> | undefined;
  await act(async () => {latest = hook.result.current.changeChartPeriod(900);});
  expect(activeTradingAccount()).toBe(initial === "unknown" ? "A" : null);
  expect(rebind.mock.calls.map(([, , period]) => period)).toEqual([300]);
  await act(async () => {finish(); await pending; await latest;});
  expect(rebind.mock.calls.map(([, , period]) => period)).toEqual([300, 900]);
  hook.unmount(); setActiveTradingAccount(null); localStorage.clear();
});


test("a delayed directory uses cached display names and a Runtime switch excludes the former names and late directory", async () => {
  const cache = await import("../marketCache");
  const api = await import("../api");
  const { contractLabel } = await import("../components/futures/support");
  const cached = vi.spyOn(cache, "loadInstrumentNames").mockImplementation(async runtime =>
    ({ "SHFE.RB2701": runtime === "http://one" ? "螺纹2701" : "新目录螺纹2701" }));
  const saved = vi.spyOn(cache, "rememberInstrumentNames").mockResolvedValue({});
  let finishOld: ((value: import("../api").InstrumentCatalog) => void) | undefined;
  const list = vi.mocked(api.listInstruments);
  list.mockImplementation(runtime => runtime === "http://one" ? new Promise(resolve => { finishOld = resolve; }) : new Promise(() => {}));
  localStorage.setItem("ai-trader:selected-instrument", "SHFE.rb2701");
  const { result, rerender, unmount } = renderHook(({ baseUrl }) => useMarketWorkbench({
    handle: { baseUrl, connection: "connected", sessionId: "s", setToast: vi.fn() } as never,
    marketStatus: "normal", feed: {} as never, chartTimeframe: 60,
  }), { initialProps: { baseUrl: "http://one" } });
  await waitFor(() => expect(contractLabel("SHFE.rb2701", result.current.instrumentCatalog)).toBe("螺纹2701"));
  expect(result.current.instrumentCatalog?.items).toEqual([]);
  expect(result.current.symbol).toBe("SHFE.rb2701");
  rerender({ baseUrl: "http://two" });
  await waitFor(() => expect(contractLabel("SHFE.rb2701", result.current.instrumentCatalog)).toBe("新目录螺纹2701"));
  await act(async () => { finishOld?.({ items: [{ symbol: "SHFE.rb2701", instrumentName: "旧目录" }] } as import("../api").InstrumentCatalog); });
  expect(contractLabel("SHFE.rb2701", result.current.instrumentCatalog)).toBe("新目录螺纹2701");
  unmount(); cached.mockRestore(); saved.mockRestore();
  list.mockImplementation(async () => ({ schemaVersion: 1, capturedAt: "", dataStatus: "normal", isCached: false, count: 0, items: [], catalogStatus: "ready" }) as import("../api").InstrumentCatalog);
  localStorage.clear();
});

import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider, onlineManager } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { vi } from "vitest";
import { readQueryClient, resetReadIdentity } from "../runtimeClient";
import { AiOrb, LatestBriefCard, resetBriefSearchStatus, useAppWorkbench, useBriefs } from "./AiActivity";

const config = { showAiActivity: true, showTradeMarkers: true, showMarketBriefs: true, extensions: [] };
const snapshot = { revision: 0, config, canRollback: false };
const activity = (symbol: string) => ({ symbol, orders: [], fills: [], strategies: [], dataStatus: "normal" });
const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={readQueryClient}>{children}</QueryClientProvider>;
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const deferred = () => { let resolve!: (value: Response) => void; const promise = new Promise<Response>(r => { resolve = r; }); return { promise, resolve }; };

beforeEach(() => {
  resetReadIdentity();
  onlineManager.setOnline(true);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); resetReadIdentity(); resetBriefSearchStatus(); onlineManager.setOnline(true); vi.unstubAllGlobals(); });

it("reads the local Runtime without public connectivity and resumes cloud reads on reconnect", async () => {
  const fetch = vi.fn(async (request: Request) => response(request.url.includes("/config") ? snapshot : activity("AU")));
  vi.stubGlobal("fetch", fetch);
  onlineManager.setOnline(false);
  const { result, rerender } = renderHook(({ url }) => useAppWorkbench(url, "AU"), {
    initialProps: { url: "http://127.0.0.1:8000" }, wrapper
  });
  await waitFor(() => expect(result.current.activity?.symbol).toBe("AU"));
  fetch.mockClear();
  rerender({ url: "https://cloud.example.test" });
  expect(result.current.activity).toBeUndefined();
  expect(fetch).not.toHaveBeenCalled();
  await act(async () => { onlineManager.setOnline(true); });
  await waitFor(() => expect(result.current.activity?.symbol).toBe("AU"));
  expect(fetch).toHaveBeenCalledTimes(2);
});

it.each(["account", "replay", "symbol"])("isolates delayed responses after a %s change", async (change) => {
  const late = deferred();
  let activityCalls = 0;
  vi.stubGlobal("fetch", vi.fn((request: Request) => {
    if (request.url.includes("/config")) return Promise.resolve(response(snapshot));
    return ++activityCalls === 1 ? late.promise : Promise.resolve(response(activity("NEW")));
  }));
  const { result, rerender } = renderHook(({ url, symbol }) => useAppWorkbench(url, symbol), {
    initialProps: { url: "http://127.0.0.1:8000", symbol: "AU" }, wrapper
  });
  await waitFor(() => expect(activityCalls).toBe(1));
  if (change === "account") act(resetReadIdentity);
  else rerender({ url: change === "replay" ? "http://127.0.0.1:8000/replays/b" : "http://127.0.0.1:8000", symbol: change === "symbol" ? "AG" : "AU" });
  expect(result.current.activity).toBeUndefined();
  await waitFor(() => expect(result.current.activity?.symbol).toBe("NEW"));
  await act(async () => { late.resolve(response(activity("OLD"))); });
  expect(result.current.activity?.symbol).toBe("NEW");
});

it("#526 切交易账户后不再显示上一个账户推送的操作记录，按新账户读取", async () => {
  const { applyFeedEvent, resetFeed } = await import("../runtimeFeed");
  const { setActiveTradingAccount } = await import("../tradingAccountScope");
  const accounts: (string | null)[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    if (request.url.includes("/config")) return response(snapshot);
    accounts.push(request.headers.get("X-AI-Trader-Account"));
    return response({ ...activity("AU"), orders: [{ clientOrderId: "b-order" }] });
  }));
  try {
    act(() => { setActiveTradingAccount("acct-a"); resetFeed("session-526"); });
    act(() => { applyFeedEvent("session-526", { name: "feed.activity", payload: { ...activity("AU"), orders: [{ clientOrderId: "a-order" }] } } as never); });
    const { result } = renderHook(() => useAppWorkbench("http://127.0.0.1:8000", "AU"), { wrapper });
    expect(result.current.activity?.orders[0]?.clientOrderId).toBe("a-order");
    act(() => setActiveTradingAccount("acct-b"));
    expect(result.current.activity?.orders[0]?.clientOrderId).not.toBe("a-order");
    await waitFor(() => expect(result.current.activity?.orders[0]?.clientOrderId).toBe("b-order"));
    expect(accounts.at(-1)).toBe("acct-b");
    // B's own pushed frame is used again once it arrives.
    act(() => { applyFeedEvent("session-526", { name: "feed.activity", payload: { ...activity("AU"), orders: [{ clientOrderId: "b-pushed" }] } } as never); });
    expect(result.current.activity?.orders[0]?.clientOrderId).toBe("b-pushed");
  } finally {
    act(() => { setActiveTradingAccount(null); resetFeed(); });
  }
});

it("does not retry or optimistically accept a conflicting settings PUT", async () => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    requests.push(request);
    if (request.method === "PUT") return response({ detail: "配置已变化" }, 409);
    return response(request.url.includes("/config") ? snapshot : activity("AU"));
  }));
  const { result } = renderHook(() => useAppWorkbench("http://127.0.0.1:8000", "AU"), { wrapper });
  await waitFor(() => expect(result.current.activity).toBeDefined());
  await act(async () => { await expect(result.current.updateConfig({ showAiActivity: false })).rejects.toThrow(); });
  expect(result.current.config.showAiActivity).toBe(true);
  const writes = requests.filter(request => request.method === "PUT");
  expect(writes).toHaveLength(1);
  expect(await writes[0].json()).toMatchObject({ revision: 0, config: { showAiActivity: false } });
});

it("reads every subscribed product's briefs without a symbol and labels each with its product", async () => {
  const brief = (id: string, product: string, productName: string, text: string) => ({ id, kind: "news", text, source: "fixture", product, productName,
    symbol: `${product}2601`, publishedAt: new Date().toISOString(), expiresAt: new Date(Date.now() + 60000).toISOString() });
  const urls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.url;
    urls.push(`${init?.method ?? (typeof input === "string" ? "GET" : input.method)} ${url}`);
    return response(url.includes("/refresh") ? { status: "idle" } : { items: [brief("rb", "SHFE.rb", "螺纹钢", "螺纹库存回落"), brief("i", "DCE.i", "铁矿石", "港口到货增加")] });
  }));
  const props = { baseUrl: "http://127.0.0.1:8000", replay: false, error: "", config, onConfigChange: vi.fn(), onOpenChat: vi.fn() };
  const { rerender } = render(<AiOrb {...props} symbol="SHFE.au2612" />, { wrapper });
  fireEvent.click(screen.getByRole("button", { name: /AI 动态/ }));
  const rebar = (await screen.findByText("螺纹库存回落")).closest("article")!;
  expect(rebar).toHaveTextContent("螺纹钢");
  expect(screen.getByText("港口到货增加").closest("article")).toHaveTextContent("铁矿石");
  // The briefs no longer follow the chart: another contract keeps the same merged list.
  rerender(<AiOrb {...props} symbol="SHFE.ag2612" />);
  expect(screen.getByText("螺纹库存回落")).toBeInTheDocument();
  expect(urls.filter(line => line.includes("/api/app/briefs"))).not.toHaveLength(0);
  expect(urls.filter(line => line.includes("/api/app/briefs")).every(line => !line.includes("symbol="))).toBe(true);
});

it("rejects settings saves while the authenticated Runtime is unavailable", async () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  const { result } = renderHook(() => useAppWorkbench(undefined, "AU"), { wrapper });
  await act(async () => {
    await expect(result.current.updateConfig({ showMarketBriefs: false })).rejects.toThrow("连接尚未就绪");
  });
  expect(fetch).not.toHaveBeenCalled();
  expect(result.current.config.showMarketBriefs).toBe(true);
});

it("clears display rows without deleting history and shows only compact monitoring status", () => {
  const orders = ["已成交", "结果未知"].map((displayStatus, i) => ({clientOrderId:`clear-${i}`,displayStatus,createdAt:"2026-09-08T13:00:00Z",reason:"closeout-final-stop-a2802d1",source:"ai",request:{direction:"BUY",offset:"OPEN",volume:1,limitPrice:3100}}));
  const data = {...activity("RB"), orders, strategies:[{strategyId:"old",name:"旧策略",status:"paused",monitorState:null,decision:null}, {strategyId:"running",name:"当前策略",status:"monitoring",monitorState:null,decision:{status:"waiting",detail:"等待方向确认"}}]};
  render(<AiOrb baseUrl="http://runtime" symbol="RB" replay={false} activity={data as any} error="" config={{...config,showMarketBriefs:false}} onConfigChange={vi.fn()} onOpenChat={vi.fn()}/>, {wrapper});
  fireEvent.click(screen.getByRole("button",{name:/AI 动态/}));
  expect(screen.getByText("AI 操作中")).toBeVisible();
  expect(screen.queryByText("等待方向确认")).toBeNull();
  expect(screen.queryByText("简讯提醒")).toBeNull();
  expect(screen.queryByText(/closeout-final/)).not.toBeInTheDocument();
  expect(screen.getAllByText(/09\/08 21:00/)).toHaveLength(2);
  fireEvent.click(screen.getByRole("button",{name:"清空显示"}));
  expect(screen.queryByText("已成交")).not.toBeInTheDocument();
  expect(screen.queryByText("结果未知")).toBeNull();
  fireEvent.click(screen.getByRole("button",{name:"查看历史"}));
  expect(screen.getByText("已成交")).toBeVisible();
});

it("does not call a prepared cancellation a sent or completed order", () => {
  const data = {...activity("RB"),orders:[{clientOrderId:"prepared-cancel",submitPhase:"PREPARED",cancelRequestedAt:"2026-09-08T13:58:21Z",displayStatus:"待发送",createdAt:"2026-09-08T13:55:58Z",source:"ai",request:{direction:"BUY",offset:"OPEN",volume:1,limitPrice:3101}}]};
  render(<AiOrb baseUrl="http://runtime" symbol="RB" replay={false} activity={data as any} error="" config={{...config,showMarketBriefs:false}} onConfigChange={vi.fn()} onOpenChat={vi.fn()}/>, {wrapper});
  fireEvent.click(screen.getByRole("button",{name:/AI 动态/}));
  expect(screen.getByText("未发送 · 已请求取消")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"清空显示"}));
  expect(screen.queryByText("未发送 · 已请求取消")).toBeNull();
  fireEvent.click(screen.getByRole("button",{name:"查看历史"}));
  expect(screen.getByText("未发送 · 已请求取消")).toBeVisible();
});

it("checks briefs without window focus and exposes unavailable search", async () => {
  vi.useFakeTimers();
  const focus = vi.spyOn(document,"hasFocus").mockReturnValue(false);
  const fetch = vi.fn(async (_url: string, init?: RequestInit) => response(init?.method === "POST" ? {status:"unavailable"} : {items:[]}));
  vi.stubGlobal("fetch",fetch);
  try {
    const {result,unmount} = renderHook(() => useBriefs("http://127.0.0.1:8000",true,false),{wrapper});
    await act(async () => { await vi.advanceTimersByTimeAsync(15_001); });
    // One search round for every subscribed product: the Runtime rotates them, the body names none.
    const refresh = fetch.mock.calls.find(([, init]) => init?.method === "POST");
    expect(refresh?.[0]).toBe("http://127.0.0.1:8000/api/app/briefs/refresh");
    expect(refresh?.[1]?.body).toBe("{}");
    expect(result.current.status).toBe("资讯检索服务不可用");
    unmount();
  } finally { focus.mockRestore(); vi.useRealTimers(); }
});

it("a failed brief refresh keeps the shown briefs and says so; recovery clears it, expiry still drops them (#516)", async () => {
  const brief = (id: string, ms: number) => ({ id, kind: "news", text: `简讯 ${id}`, source: "fixture",
    publishedAt: "2026-10-06T01:00:00Z", expiresAt: new Date(Date.now() + ms).toISOString() });
  let status = 200;
  let items = [brief("b1", 600_000), brief("gone", 300)];
  vi.stubGlobal("fetch", vi.fn(async () => status === 200 ? response({ items }) : response({ detail: "busy" }, status)));
  const { result, rerender } = renderHook(() => useBriefs("http://127.0.0.1:8000", true, false, false), { wrapper });
  await waitFor(() => expect(result.current.items.map(item => item.id)).toEqual(["b1", "gone"]));
  status = 503;
  await act(async () => { await result.current.reload(); });
  await waitFor(() => expect(result.current.status).toBe("刷新失败，显示上次的 2 条简讯"));
  expect(result.current.items.map(item => item.id)).toEqual(["b1", "gone"]);
  await new Promise(resolve => setTimeout(resolve, 350));  // "gone" expires while the reads keep failing
  rerender();
  expect(result.current.items.map(item => item.id)).toEqual(["b1"]);
  status = 200;
  items = [brief("b2", 600_000)];
  await act(async () => { await result.current.reload(); });
  await waitFor(() => expect(result.current.status).toBe("1 条有效简讯"));
  expect(result.current.items.map(item => item.id)).toEqual(["b2"]);
  // Another login: the old identity's list is never shown, failed read or not.
  status = 503;
  act(() => resetReadIdentity());
  await waitFor(() => expect(result.current.status).toBe("简讯读取失败"));
  expect(result.current.items).toEqual([]);
});

it("a read-only briefs consumer (the pinned card) never adds refresh requests", async () => {
  vi.useFakeTimers();
  const fetch = vi.fn(async (_url: string, init?: RequestInit) => response(init?.method === "POST" ? {status:"pending"} : {items:[]}));
  vi.stubGlobal("fetch",fetch);
  try {
    const owner = renderHook(() => useBriefs("http://127.0.0.1:8000",true,false),{wrapper});
    const reader = renderHook(() => useBriefs("http://127.0.0.1:8000",true,false,false),{wrapper});
    await act(async () => { await vi.advanceTimersByTimeAsync(15_001); });
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    // GETs come from the shared query: the initial read plus the refetch the owner's refresh invalidates, never a per-reader read.
    expect(fetch.mock.calls.filter(([url, init]) => !init?.method && url === "http://127.0.0.1:8000/api/app/briefs").length).toBeLessThanOrEqual(2);
    expect(reader.result.current.items).toEqual([]);
    // The reader sees the owner's search status instead of its own idle placeholder.
    expect(owner.result.current.status).toBe("最近检索请求已提交，暂无可展示简讯");
    expect(reader.result.current.status).toBe("最近检索请求已提交，暂无可展示简讯");
    reader.unmount(); owner.unmount();
  } finally { vi.useRealTimers(); }
});

it("the monitor page brief card renders only when a valid brief exists", async () => {
  const brief = { id: "b1", kind: "news", text: "国家发改委发布钢铁产能置换新规。", source: "ndrc.gov.cn", url: "https://www.ndrc.gov.cn/x",
    publishedAt: "2026-09-14T06:00:00Z", expiresAt: new Date(Date.now() + 600_000).toISOString() };
  let items: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async () => response({ items })));
  const view = render(<LatestBriefCard baseUrl="http://127.0.0.1:8000" enabled replay={false} />, { wrapper });
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)); });
  expect(screen.queryByTestId("latest-brief")).toBeNull();
  expect(document.body).not.toHaveTextContent("等待检索");
  items = [brief];
  await act(async () => { await readQueryClient.invalidateQueries(); });
  await waitFor(() => expect(screen.getByTestId("latest-brief")).toHaveTextContent("重要资讯"));
  expect(screen.getByTestId("latest-brief")).toHaveTextContent("ndrc.gov.cn");
  expect(screen.getByTestId("latest-brief")).toHaveTextContent(brief.text);
  view.unmount();
});

it("外盘合约的平仓盈亏写美元，不写元", async () => {
  const t0 = Date.parse("2026-10-21T09:00:00Z") * 1e6, t1 = t0 + 60e9;
  const order = (id: string, offset: string, direction: string) => ({ clientOrderId: id, displayStatus: "已成交", filledVolume: 1, createdAt: "2026-10-21T09:00:00Z", source: "ai", request: { direction, offset, volume: 1, limitPrice: 29780 } });
  const fill = (id: string, tradeId: string, offset: string, direction: string, price: number, at: number) => ({ clientOrderId: id, tradeId, symbol: "CME.ENQZ26", direction, offset, price, volume: 1, tradeDateTime: at, source: "ai" });
  const data = { ...activity("CME.ENQZ26"), orders: [order("o1", "OPEN", "BUY"), order("o2", "CLOSE", "SELL")], fills: [fill("o1", "f1", "OPEN", "BUY", 29780, t0), fill("o2", "f2", "CLOSE", "SELL", 29790, t1)] };
  const review = { costSource: "account", trades: [{ id: "f1:f2", symbol: "CME.ENQZ26", direction: "long", volume: 1, openTime: t0, closeTime: t1, openPrice: 29780, closePrice: 29790, gross: 200, net: 195, fees: 5 }] };
  vi.stubGlobal("fetch", vi.fn(async (request: Request | string) => response(String(typeof request === "string" ? request : request.url).includes("/api/trade-review") ? review : snapshot)));
  render(<AiOrb baseUrl="http://runtime" symbol="CME.ENQZ26" replay={false} activity={data as any} error="" config={{...config,showMarketBriefs:false}} onConfigChange={vi.fn()} onOpenChat={vi.fn()}/>, {wrapper});
  fireEvent.click(screen.getByRole("button",{name:/AI 动态/}));
  expect(await screen.findByText("净盈亏 +195.00 美元")).toBeVisible();
});

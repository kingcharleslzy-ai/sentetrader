import { act, cleanup, configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import type { FuturesDecisionWorkspace } from "./components/FuturesDecisionWorkspace";
import type { ReplayWorkspaceProps } from "./components/HistoricalReplay";
import type { ReactNode } from "react";
import { resetReadIdentity } from "./runtimeClient";
import { manualOrderReceipt, type ManualOrderOutcome } from "./hooks/useSimulationOrders";

const session = vi.hoisted(() => ({
  baseUrl: "http://127.0.0.1:8101",
  connection: "connected" as "connected" | "connecting" | "disconnected",
  sessionId: "session-a",
  accountGeneration: 1,
  replayPath: "",
  quoteDatetime: undefined as string | undefined,
  /** The chart's contract session (#932 B1: a closed chart contract must not block a card's confirmation). */
  marketSessionStatus: "open",
  account: undefined as { id: string; foreignAccountId: string } | undefined,
  onRuntimeReady: () => undefined
}));
/** What each manual submission returned to the workspace (the ticket's receipt is built from it). */
const outcomes = vi.hoisted(() => [] as unknown[]);
vi.mock("./hooks/useRuntimeSession", () => ({
  useRuntimeSession: () => ({
    ...session, rootBaseUrl: session.baseUrl, workspaceScope: "orders-fixture:local", rootWorkspaceScope: "orders-fixture:local",
    runtimeActivityAt: Date.now(), authRequired: false,
    state: { turns: [], runtime: { account: session.account, accountGeneration: session.accountGeneration, runtimeConnected: true, agentConnected: true, simulationTrading: true,
      tradingGatewayStatus: "available", accountStatus: "normal", marketDataStatus: "normal", riskStatus: "normal" } },
    submitMessage: vi.fn(), showBacktest: vi.fn()
  })
}));
vi.mock("./components/HistoricalReplay", () => ({
  HistoricalReplay: ({ children }: { children: (props: ReplayWorkspaceProps) => ReactNode }) =>
    children({ replayPath: session.replayPath, onRuntimeReady: session.onRuntimeReady })
}));
// Keep App's real request/state/operation paths and expose their workspace contract.
vi.mock("./components/FuturesDecisionWorkspace", () => ({
  FuturesDecisionWorkspace: (props: Parameters<typeof FuturesDecisionWorkspace>[0]) => <>
    <output data-testid="orders">{props.simulationOrders.map(order => `${order.clientOrderId}:${order.displayStatus}`).join(",")}</output>
    <output data-testid="busy">{String(props.tradeBusy)}</output>
    <output data-testid="ticket-blocked">{props.simulationBlockedReason ?? "ready"}</output>
    <button disabled={props.tradeBusy} onClick={() => props.proposalActions.adjustRisk("AIT-proposal", 1)}>调整候选</button>
    <button disabled={props.tradeBusy} onClick={() => props.proposalActions.reject("AIT-proposal", 1)}>取消候选</button>
    <output data-testid="confirm-gate">{props.proposalActions.confirmGate ?? "ready"}</output>
    <button onClick={() => props.proposalActions.confirm({ type: "trade", id: "card-trade", payload: { proposalId: "card",
      approvalId: "approval-card", version: 1, status: "approval_required", executionEligibleAtCreation: true, symbol: "SHFE.au2612",
      name: "沪金2612", side: "buy_open", volume: 1, limitPrice: 800, contractMultiplier: 1000, updatedAt: "2026-10-04T10:00:00+08:00",
      environment: "simulation", turnId: "turn-1", riskCheck: { passed: true, limit: 0, estimatedLoss: 0, reason: "", contractMultiplier: 1000, priceTick: "0.02" } } } as never)}>确认交易卡</button>
    <button disabled={!props.simulationEnabled || props.tradeBusy} onClick={() => void props.onManualSubmit({ side: "buy_open", volume: 1, priceIntent: "last", source: "three-key" }).then(outcome => outcomes.push(outcome))}>提交模拟委托</button>
    <button disabled={!props.simulationEnabled || props.tradeBusy} onClick={() => void props.onManualSubmit({ side: "sell_close", volume: 3, priceIntent: "aggressive", source: "three-key" }).then(outcome => outcomes.push(outcome))}>智能平仓 3 手</button>
    {props.simulationOrders.map(order => <button key={order.clientOrderId} disabled={props.tradeBusy}
      onClick={() => props.onOrderAction(order, "query")}>查询 {order.clientOrderId}</button>)}
    {props.simulationOrders.map(order => <button key={`cancel-${order.clientOrderId}`} disabled={props.tradeBusy}
      onClick={() => props.onOrderAction(order, "cancel")}>撤单 {order.clientOrderId}</button>)}
  </>
}));
import { App } from "./App";

const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const deferred = () => {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>(done => { resolve = done; });
  return { promise, resolve };
};
const order = (id: string, displayStatus = "结果未知") => ({ clientOrderId: id, proposalId: `proposal-${id}`, displayStatus,
  request: { symbol: "SHFE.au2612", direction: "BUY", offset: "OPEN", volume: 1, limitPrice: 800 } });
/**
 * App is usable only once its boot reads (identity, account, positions, risk, the chart's quote) have
 * landed. Every action waits for that state explicitly — a click before it hits a disabled button
 * does nothing (#932 B11: the approval test clicked the sheet's still-disabled 确认 when the boot ran
 * slow under parallel load, and timed out waiting for a confirm that was never sent).
 */
const ticketReady = (name: string) => waitFor(() => expect(screen.getByRole("button", { name })).toBeEnabled());
// The waits are conditions, never sleeps; the bound only covers a cold first App boot (a dozen fetch
// round trips plus module evaluation) on a loaded host, where testing-library's 1 s default ran out
// (reproduced with 30 concurrent runs of this file). A passing wait returns as soon as it holds.
beforeAll(() => configure({ asyncUtilTimeout: 5_000 }));
afterAll(() => configure({ asyncUtilTimeout: 1_000 }));
const proposal = (id: string) => ({ proposalId: id, approvalId: `approval-${id}`, version: 1, status: "approval_required", executionEligibleAtCreation: true });
const list = vi.fn<(url: URL, init?: RequestInit) => Promise<Response>>();
const mutate = vi.fn<(url: URL) => Promise<Response>>();
const create = vi.fn<(url: URL) => Promise<Response>>();
const confirm = vi.fn<(url: URL) => Promise<Response>>();
const proposalMutation = vi.fn<(url: URL) => Promise<Response>>();
const unexpected: string[] = [];

beforeEach(() => {
  resetReadIdentity();
  session.baseUrl = "http://127.0.0.1:8101";
  session.connection = "connected";
  session.sessionId = "session-a";
  session.replayPath = "";
  session.account = undefined;
  session.quoteDatetime = undefined;
  session.marketSessionStatus = "open";
  outcomes.length = 0;
  localStorage.setItem("ai-trader:selected-instrument", "SHFE.au2612");
  session.accountGeneration = 1;
  unexpected.length = 0;
  list.mockReset().mockImplementation(async () => response({ items: [] }));
  mutate.mockReset(); create.mockReset(); confirm.mockReset(); proposalMutation.mockReset();
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    if (url.pathname === "/api/simulation/orders") return list(url, init);
    if (url.pathname.startsWith("/api/simulation/orders/")) return mutate(url);
    if (url.pathname === "/api/simulation/proposals") return create(url);
    if (url.pathname.startsWith("/api/proposals/") && url.pathname.endsWith("/confirm")) return confirm(url);
    if (/^\/api\/proposals\/[^/]+\/(adjust-risk|reject)$/.test(url.pathname)) return proposalMutation(url);
    if (url.pathname === "/api/agent/account") return response({ available: true, authenticated: true });
    if (url.pathname === "/api/workspace") return response({ scope: "orders-fixture" });
    if (url.pathname === "/api/ai-execution") return response({ authorization: null,
      controls: { stopStatus: null, generation: 0, revision: 0, maxOrderVolume: null, maxOrders: null } });
    if (url.pathname === "/api/instruments") return response({ items: [{ symbol: "SHFE.au2612", isMain: true }], catalogStatus: "ready" });
    if (url.pathname === "/api/account/summary") return response({ dataStatus: "normal" });
    if (url.pathname === "/api/positions") return response({ items: [] });
    if (url.pathname === "/api/account/activity") return response({ trades: [], orders: [] });
    if (url.pathname === "/api/risk/summary") return response({ status: "normal" });
    if (url.pathname.endsWith("/workbench")) {
      // Return market data after the initial chart-period acknowledgement clears
      // the prior chart, as a subsequent HTTP quote response would.
      await new Promise(resolve => setTimeout(resolve, 0));
      return response({ quote: { symbol: "SHFE.au2612", quoteDatetime: session.quoteDatetime }, dataStatus: "normal", isCached: false, marketSessionStatus: session.marketSessionStatus, ageSeconds: 0 });
    }
    if (url.pathname === "/api/strategy-plans/chart-period") return response({});
    if (/^\/api\/sessions\/[^/]+\/feed$/.test(url.pathname)) return response({ symbol: "SHFE.au2612", durationSeconds: 60 });
    unexpected.push(url.pathname);
    throw new Error(`Unexpected offline fixture request: ${url.pathname}`);
  }));
});
afterEach(() => {
  cleanup(); resetReadIdentity(); vi.unstubAllGlobals(); localStorage.clear();
  expect(unexpected).toEqual([]);
});

it.each(["runtime", "identity", "disconnect", "account", "account-generation"])("clears visible orders as soon as the %s changes", async boundary => {
  const next = deferred();
  list.mockResolvedValueOnce(response({ items: [order("AIT-old")] })).mockReturnValue(next.promise);
  const app = render(<App />);
  await screen.findByText("AIT-old:结果未知");
  if (boundary === "identity") act(resetReadIdentity);
  else {
    if (boundary === "runtime") session.baseUrl = "http://127.0.0.1:8102";
    else if (boundary === "account") session.account = { id: "B", foreignAccountId: "" };
    else if (boundary === "account-generation") session.accountGeneration += 1;
    else session.connection = "disconnected";
    app.rerender(<App />);
  }
  expect(screen.getByTestId("orders")).toBeEmptyDOMElement();
  if (boundary === "disconnect") {
    expect(list).toHaveBeenCalledTimes(1);
    session.connection = "connected";
    app.rerender(<App />);
  }
  await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  await act(async () => { next.resolve(response({ items: [order("AIT-new")] })); });
  expect(screen.getByTestId("orders")).toHaveTextContent("AIT-new:结果未知");
});

it.each(["runtime", "identity"])("ignores an old order list that arrives after the new %s list", async boundary => {
  const old = deferred();
  list.mockReturnValueOnce(old.promise).mockResolvedValue(response({ items: [order("AIT-new")] }));
  const app = render(<App />);
  await waitFor(() => expect(list).toHaveBeenCalledTimes(1));
  if (boundary === "identity") act(resetReadIdentity);
  else { session.baseUrl = "http://127.0.0.1:8102"; app.rerender(<App />); }
  await screen.findByText("AIT-new:结果未知");
  await act(async () => { old.resolve(response({ items: [order("AIT-old")] })); });
  expect(screen.getByTestId("orders")).toHaveTextContent("AIT-new:结果未知");
  expect(screen.queryByText(/AIT-old/)).not.toBeInTheDocument();
});

it.each([200, 503])("an old query result (%s) cannot change the new list, toast or busy operation", async status => {
  const old = deferred(), next = deferred();
  list.mockResolvedValueOnce(response({ items: [order("AIT-old")] })).mockResolvedValue(response({ items: [order("AIT-new")] }));
  mutate.mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise);
  const app = render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "查询 AIT-old" }));
  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(1));
  session.baseUrl = "http://127.0.0.1:8102";
  app.rerender(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "查询 AIT-new" }));
  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(2));
  await act(async () => { old.resolve(response(status === 200 ? order("AIT-old", "旧环境已受理") : { detail: "old_query_failed" }, status)); });
  expect(screen.getByTestId("orders")).toHaveTextContent("AIT-new:结果未知");
  expect(screen.getByTestId("busy")).toHaveTextContent("true");
  expect(screen.queryByText(/旧环境已受理|old_query_failed/)).not.toBeInTheDocument();
  await act(async () => { next.resolve(response(order("AIT-new", "已受理"))); });
  expect(screen.getByTestId("orders")).toHaveTextContent("AIT-new:已受理");
  expect(screen.getByTestId("busy")).toHaveTextContent("false");
  expect(mutate.mock.calls.map(([url]) => url.origin)).toEqual(["http://127.0.0.1:8101", "http://127.0.0.1:8102"]);
});

it("does not confirm a draft returned after its authenticated identity changes", async () => {
  const old = deferred();
  create.mockReturnValueOnce(old.promise);
  render(<App />);
  await ticketReady("提交模拟委托");
  fireEvent.click(screen.getByRole("button", { name: "提交模拟委托" }));
  await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
  act(resetReadIdentity);
  await act(async () => { old.resolve(response(proposal("old"))); });
  expect(confirm).not.toHaveBeenCalled();
  expect(screen.getByTestId("orders")).toBeEmptyDOMElement();
  expect(screen.getByTestId("busy")).toHaveTextContent("false");
});

it("keeps a new manual order busy when an already sent old confirmation returns", async () => {
  const old = deferred(), next = deferred();
  create.mockResolvedValueOnce(response(proposal("old"))).mockResolvedValueOnce(response(proposal("new")));
  confirm.mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise);
  const app = render(<App />);
  await ticketReady("提交模拟委托");
  fireEvent.click(screen.getByRole("button", { name: "提交模拟委托" }));
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  session.connection = "connecting";
  session.baseUrl = "http://127.0.0.1:8102";
  app.rerender(<App />);
  session.connection = "connected";
  app.rerender(<App />);
  await ticketReady("提交模拟委托");
  fireEvent.click(screen.getByRole("button", { name: "提交模拟委托" }));
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2));
  await act(async () => { old.resolve(response({ simulationOrder: order("AIT-old", "旧环境已受理") })); });
  expect(screen.getByTestId("orders")).toBeEmptyDOMElement();
  expect(screen.getByTestId("busy")).toHaveTextContent("true");
  expect(screen.queryByText(/旧环境已受理/)).not.toBeInTheDocument();
  await act(async () => { next.resolve(response({ simulationOrder: order("AIT-new", "已受理") })); });
  expect(screen.getByTestId("orders")).toHaveTextContent("AIT-new:已受理");
  expect(screen.getByTestId("busy")).toHaveTextContent("false");
  expect(confirm.mock.calls.map(([url]) => `${url.origin}${url.pathname}`)).toEqual([
    "http://127.0.0.1:8101/api/proposals/old/confirm", "http://127.0.0.1:8102/api/proposals/new/confirm"
  ]);
});

it("a ticket close the planner split over 今仓 + 昨仓 comes back as one batch card: confirmed once, both orders listed", async () => {
  const batch = { proposalId: "batch", approvalId: "approval-batch", version: 1, status: "approval_required", totalOrders: 2, totalVolume: 3,
    riskCheck: { passed: true, reason: "" }, orders: [{ positionBucket: "today", volume: 2 }, { positionBucket: "historical", volume: 1 }] };
  create.mockResolvedValueOnce(response(batch, 201));
  confirm.mockResolvedValueOnce(response({ simulationOrders: [{ ...order("AIT-today", "已受理"), id: "1" }, { ...order("AIT-his", "已受理"), id: "2" }],
    execution: { total: 2, sent: 2, notSent: 0, unknown: 0, filled: 0, text: "已确认，2 张已发送" } }));
  render(<App />);
  await ticketReady("智能平仓 3 手");
  fireEvent.click(screen.getByRole("button", { name: "智能平仓 3 手" }));
  await screen.findByText("手工模拟平仓：2 张已发送，逐笔见委托栏");
  expect(screen.getByTestId("orders")).toHaveTextContent("AIT-today:已受理,AIT-his:已受理");
  // The ticket sends only what the user chose: no 平今/平昨 — the Runtime's planner allocated them.
  const sent = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith("/api/simulation/proposals"));
  expect(JSON.parse(String((sent?.[1] as RequestInit).body))).not.toHaveProperty("positionBucket");
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(confirm.mock.calls[0][0].pathname).toBe("/api/proposals/batch/confirm");
  expect(outcomes).toEqual([expect.objectContaining({ confirmed: true, accepted: true, skipped: [] })]);
});

it("#932 A26 a confirmed batch with a refused child and a skipped bucket reports what was placed, never 未确认", async () => {
  // Asked 4: the planner placed 今 2 + 昨 1 and skipped 1 lot (no fresh quote); the exchange refused the 昨 order.
  const batch = { proposalId: "batch", approvalId: "approval-batch", version: 1, status: "approval_required", totalOrders: 2, totalVolume: 3,
    riskCheck: { passed: true, reason: "" }, skipped: [{ symbol: "SHFE.au2612", volume: 1, positionBucket: "historical", reason: "quote_stale" }],
    orders: [{ symbol: "SHFE.au2612", positionBucket: "today", volume: 2 }, { symbol: "SHFE.au2612", positionBucket: "historical", volume: 1 }] };
  const child = (id: string, offset: string, volume: number, displayStatus: string) =>
    ({ ...order(id, displayStatus), id, request: { symbol: "SHFE.au2612", direction: "SELL", offset, volume, limitPrice: 800 } });
  create.mockResolvedValueOnce(response(batch, 201));
  confirm.mockResolvedValueOnce(response({ simulationOrders: [child("AIT-today", "CLOSETODAY", 2, "已受理"), child("AIT-his", "CLOSE", 1, "已拒绝")],
    execution: { total: 2, sent: 2, notSent: 0, unknown: 0, filled: 0, text: "已确认，2 笔已发送" } }));
  render(<App />);
  await ticketReady("智能平仓 3 手");
  fireEvent.click(screen.getByRole("button", { name: "智能平仓 3 手" }));
  await waitFor(() => expect(outcomes).toHaveLength(1));
  const outcome = outcomes[0] as ManualOrderOutcome;
  expect(outcome).toMatchObject({ confirmed: true, accepted: false });
  expect(outcome.orders.map(item => item.clientOrderId)).toEqual(["AIT-today", "AIT-his"]);
  expect(manualOrderReceipt(outcome))
    .toBe("已确认，共 3 手：卖平今 2 手 @ 800 已受理、卖平昨 1 手 @ 800 已拒绝；另 1 手未纳入（au2612 无最新报价）。系统不会自动重提，逐笔见委托分页。");
  expect(confirm).toHaveBeenCalledTimes(1);
});

it("#932 a lost batch confirmation read back with one of two children is not accepted: the missing lots are named", async () => {
  const batch = { proposalId: "batch", approvalId: "approval-batch", version: 1, status: "approval_required", totalOrders: 2, totalVolume: 3,
    riskCheck: { passed: true, reason: "" }, skipped: [],
    orders: [{ symbol: "SHFE.au2612", positionBucket: "today", volume: 2 }, { symbol: "SHFE.au2612", positionBucket: "historical", volume: 1 }] };
  list.mockResolvedValueOnce(response({ items: [] }))
    .mockResolvedValue(response({ items: [{ ...order("AIT-today", "已受理"), id: "1", proposalId: "batch",
      request: { symbol: "SHFE.au2612", direction: "SELL", offset: "CLOSETODAY", volume: 2, limitPrice: 800 } }] }));
  create.mockResolvedValueOnce(response(batch, 201));
  confirm.mockResolvedValueOnce(response({ detail: "response_lost" }, 503));
  render(<App />);
  await ticketReady("智能平仓 3 手");
  fireEvent.click(screen.getByRole("button", { name: "智能平仓 3 手" }));
  await waitFor(() => expect(outcomes).toHaveLength(1));
  const outcome = outcomes[0] as ManualOrderOutcome;
  // Confirmed (never resent), but not accepted — the ticket keeps its draft (it clears only on accepted).
  expect(outcome).toMatchObject({ confirmed: true, accepted: false });
  expect(outcome.orders.map(item => item.clientOrderId)).toEqual(["AIT-today"]);
  expect(manualOrderReceipt(outcome)).toBe("已确认，共 2 手：卖平今 2 手 @ 800 已受理；另 1 手未纳入（1 笔未回读到，先在委托分页查询）。系统不会自动重提，逐笔见委托分页。");
  expect(confirm).toHaveBeenCalledTimes(1);
});

it.each(["none", "runtime", "account", "account-generation"])("keeps original-order recovery but isolates its late result (%s)", async boundary => {
  const changed = boundary !== "none";
  const recovery = deferred();
  list.mockResolvedValueOnce(response({ items: [] })).mockReturnValueOnce(recovery.promise)
    .mockResolvedValue(response({ items: [order("AIT-new")] }));
  create.mockResolvedValueOnce(response(proposal("old")));
  confirm.mockResolvedValueOnce(response({ detail: "response_lost" }, 503));
  const app = render(<App />);
  await ticketReady("提交模拟委托");
  fireEvent.click(screen.getByRole("button", { name: "提交模拟委托" }));
  await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  if (changed) {
    if (boundary === "runtime") session.baseUrl = "http://127.0.0.1:8102";
    else if (boundary === "account") session.account = { id: "B", foreignAccountId: "" };
    else session.accountGeneration += 1;
    app.rerender(<App />);
    await screen.findByText("AIT-new:结果未知");
  }
  const summaryReads = () => vi.mocked(fetch).mock.calls.filter(([input]) => String(input).includes("/api/account/summary")).length;
  const before = summaryReads();
  await act(async () => { recovery.resolve(response({ items: [{ ...order("AIT-original", "已受理"), proposalId: "old" }] })); });
  expect(screen.getByTestId("orders")).toHaveTextContent(changed ? "AIT-new:结果未知" : "AIT-original:已受理");
  if (changed) expect(screen.queryByText(/已回读模拟委托/)).not.toBeInTheDocument();
  else {
    expect(screen.getByText("已回读模拟委托：已受理")).toBeInTheDocument();
    // The recovered order is the confirmed outcome (never a retry), and the account is re-read as after any confirm.
    await waitFor(() => expect(outcomes).toEqual([expect.objectContaining({ confirmed: true, accepted: true })]));
    await waitFor(() => expect(summaryReads()).toBeGreaterThan(before));
  }
  expect(create).toHaveBeenCalledTimes(1);
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(list.mock.calls[1][0].origin).toBe("http://127.0.0.1:8101");
});


it.each([
  ["调整候选", "候选已按单笔风险上限调整", 200],
  ["调整候选", "候选已按单笔风险上限调整", 503],
  ["取消候选", "候选已取消，未执行任何操作", 200],
  ["取消候选", "候选已取消，未执行任何操作", 503]
] as const)("isolates a retired proposal action's toast and busy state (%s, %s, %s)", async (label, message, status) => {
  const old = deferred(), next = deferred();
  proposalMutation.mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise);
  const app = render(<App />);
  fireEvent.click(screen.getByRole("button", { name: label }));
  await waitFor(() => expect(proposalMutation).toHaveBeenCalledTimes(1));
  session.baseUrl = "http://127.0.0.1:8102";
  app.rerender(<App />);
  fireEvent.click(screen.getByRole("button", { name: label }));
  await waitFor(() => expect(proposalMutation).toHaveBeenCalledTimes(2));
  await act(async () => { old.resolve(response(status === 200 ? {} : { detail: "retired-proposal-error" }, status)); });
  expect(screen.queryByText(message)).not.toBeInTheDocument();
  expect(screen.queryByText(/retired-proposal-error/)).not.toBeInTheDocument();
  expect(screen.getByTestId("busy")).toHaveTextContent("true");
  await act(async () => { next.resolve(response({})); });
  expect(screen.getByText(message)).toBeInTheDocument();
  expect(screen.getByTestId("busy")).toHaveTextContent("false");
  expect(proposalMutation.mock.calls.map(([url]) => url.origin)).toEqual(["http://127.0.0.1:8101", "http://127.0.0.1:8102"]);
});

it("ignores the first A response after an A to B to A Runtime round trip", async () => {
  const originalA = deferred();
  list.mockReturnValueOnce(originalA.promise)
    .mockResolvedValueOnce(response({ items: [order("AIT-b")] }))
    .mockResolvedValueOnce(response({ items: [order("AIT-new-a")] }));
  const app = render(<App />);
  await waitFor(() => expect(list).toHaveBeenCalledTimes(1));
  session.baseUrl = "http://127.0.0.1:8102";
  app.rerender(<App />);
  await screen.findByText("AIT-b:结果未知");
  session.baseUrl = "http://127.0.0.1:8101";
  app.rerender(<App />);
  await screen.findByText("AIT-new-a:结果未知");
  await act(async () => { originalA.resolve(response({ items: [order("AIT-original-a")] })); });
  expect(screen.getByTestId("orders")).toHaveTextContent("AIT-new-a:结果未知");
  expect(screen.queryByText(/AIT-original-a/)).not.toBeInTheDocument();
  expect(list.mock.calls.map(([url]) => url.origin)).toEqual([
    "http://127.0.0.1:8101", "http://127.0.0.1:8102", "http://127.0.0.1:8101"
  ]);
});

it("a replay's historical quote stamp does not close the ticket; a live quote 16 s old still does", async () => {
  session.replayPath = "/api/replays/replay-1";
  session.quoteDatetime = "2024-03-01 10:00:00";  // the replayed tick, years behind the wall clock
  const replay = render(<App />);
  await ticketReady("提交模拟委托");
  replay.unmount();
  session.replayPath = "";
  // The wall clock stands still (timers stay real), so the quote is exactly 16 s old however slow the
  // host is — a loaded run once read it as 20 s.
  vi.useFakeTimers({ toFake: ["Date"] });
  try {
    vi.setSystemTime(new Date("2026-10-19T02:00:16Z"));
    session.quoteDatetime = "2026-10-19T02:00:00Z";
    render(<App />);
    // Wait for the quote itself to be judged (no fixed sleep: before the quote lands the ticket is
    // closed for a different reason, which proved nothing about the 16 s rule).
    await waitFor(() => expect(screen.getByTestId("ticket-blocked")).toHaveTextContent("行情已 16 秒无新报价"));
    expect(screen.getByRole("button", { name: "提交模拟委托" })).toBeDisabled();
  } finally { vi.useRealTimers(); }
});

/** Press the trade card's own 确认 (no second dialog, owner 2026-10-19) once the account and gateway can take it. */
const confirmCard = async () => {
  await waitFor(() => expect(screen.getByTestId("confirm-gate")).toHaveTextContent("ready"));
  fireEvent.click(screen.getByRole("button", { name: "确认交易卡" }));
};

it("a trade card whose send was refused after confirmation re-reads the orders, so the card can say 未发送", async () => {
  list.mockResolvedValueOnce(response({ items: [] }))
    .mockResolvedValue(response({ items: [{ ...order("AIT-card", "待发送"), proposalId: "card" }] }));
  confirm.mockResolvedValueOnce(response({ detail: "quote_stale" }, 409));
  render(<App />);
  await waitFor(() => expect(list).toHaveBeenCalledTimes(1));
  await confirmCard();
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  await screen.findByText("AIT-card:待发送");
  expect(list).toHaveBeenCalledTimes(2);
});

it("#932 B1 a card stays confirmable while the chart shows a closed contract; the ticket for that contract does not", async () => {
  session.marketSessionStatus = "closed";
  confirm.mockResolvedValueOnce(response({ simulationOrder: { ...order("AIT-card", "已受理"), proposalId: "card" } }));
  render(<App />);
  await waitFor(() => expect(screen.getByTestId("ticket-blocked")).toHaveTextContent("休市，暂不可下单"));
  expect(screen.getByRole("button", { name: "提交模拟委托" })).toBeDisabled();
  await confirmCard();
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  expect(confirm.mock.calls[0][0].pathname).toBe("/api/proposals/card/confirm");
});

it("#932 B1 the card's confirmation still waits for the account: an account that is not current keeps it closed", async () => {
  session.marketSessionStatus = "closed";
  let summaries = 0;
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    if (url.pathname === "/api/account/summary") { summaries += 1; return response({ dataStatus: "stale" }); }
    return original(input, init);
  });
  render(<App />);
  await waitFor(() => expect(summaries).toBeGreaterThan(0));
  await waitFor(() => expect(screen.getByTestId("confirm-gate")).toHaveTextContent(/账户/));
  fireEvent.click(screen.getByRole("button", { name: "确认交易卡" }));  // the card's button is disabled; a click sends nothing
  expect(confirm).not.toHaveBeenCalled();
});

it("re-reads the merged foreign list immediately after cancelling through App's shared order action", async () => {
  session.account = { id: "paper:p1", foreignAccountId: "paperusd:p1" };
  const usd = { ...order("AIT-usd", "已受理"), accountId: "paperusd:p1", remoteStatus: "ALIVE" };
  list.mockImplementation(async (_url: URL, init?: RequestInit) => response({ items:
    new Headers(init?.headers).get("X-AI-Trader-Account") === "paperusd:p1" ? [{ ...usd }] : [] }));
  mutate.mockImplementation(async () => { Object.assign(usd, { displayStatus: "已撤销", remoteStatus: "FINISHED" }); return response(usd); });
  render(<App />);
  await screen.findByText("AIT-usd:已受理");
  const before = list.mock.calls.length;
  fireEvent.click(screen.getByRole("button", { name: "撤单 AIT-usd" }));
  await screen.findByText("AIT-usd:已撤销");
  expect(list.mock.calls.length).toBeGreaterThan(before);
  expect(mutate).toHaveBeenCalledTimes(1);
});

import { useTradingAccounts } from "./useTradingAccounts";
import { useIdentityFence } from "./useIdentityFence";
import { useProposalBusy } from "./useProposals";
import { useQuickOrders } from "../components/futures/useQuickOrders";
import { createElement } from "react";
import { OrderRows } from "../components/futures/PositionPanels";
import { render, screen, within, fireEvent, renderHook, act, waitFor } from "@testing-library/react";
import type { RuntimeStatus, UiEvent } from "@ai-trader/ui-events";
import { runtimeHeaders, setActiveTradingAccount, type RemoteOrderSnapshot, type SimulationOrder } from "../api";
import type { ProposalBusy } from "./useProposals";
import { manualOrderReceipt, mergeOrderFacts, ordersBehindAccount, useOrdersFollowAccount, useSimulationOrders } from "./useSimulationOrders";
import { applyFeedEvent } from "../runtimeFeed";

const order = (displayStatus: string, filledVolume = 0, factRevision?: number): SimulationOrder =>
  ({ id: "o-1", clientOrderId: "CO-1", proposalId: "p-1", displayStatus, filledVolume, remoteStatus: "ALIVE",
     request: { symbol: "SHFE.rb2701", direction: "SELL", offset: "CLOSETODAY", volume: 2, priceType: "LIMIT", limitPrice: 3074 },
     submitPhase: "CONFIRMED", cancelPhase: "NONE",
     ...(factRevision !== undefined ? { factRevision } : {}) } as SimulationOrder);
const resting = (clientOrderId: string, limitPrice: number, remoteStatus = "ALIVE", displayStatus = "已受理"): SimulationOrder =>
  ({ ...order(displayStatus), id: clientOrderId, clientOrderId, remoteStatus,
     request: { symbol: "SHFE.rb2701", direction: "BUY", offset: "OPEN", volume: 1, priceType: "LIMIT", limitPrice } } as SimulationOrder);
const remote = (clientOrderId: string, status = "ALIVE", filledVolume = 0): RemoteOrderSnapshot =>
  ({ clientOrderId, symbol: "SHFE.rb2701", direction: "BUY", offset: "OPEN", volume: 1, volumeLeft: 1 - filledVolume,
     filledVolume, limitPrice: 3000, status, insertDateTime: 0, lastMsg: "" });

// The real api module runs against a stubbed fetch: each account has its own order links on the Runtime.
const fetchMock = vi.hoisted(() => vi.fn());
let links: Record<string, SimulationOrder[]> = {};
let granted: string[] | null = null;  // the account directory; null = every account with links
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
const accountOf = (init?: RequestInit) => new Headers(init?.headers).get("X-AI-Trader-Account") ?? "(default)";
beforeEach(() => {
  vi.clearAllMocks();
  links = {};
  granted = null;
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const cancel = /\/api\/simulation\/orders\/([^/]+)\/cancel$/.exec(url);
    if (cancel && init?.method === "POST") {
      const row = (links[accountOf(init)] ?? []).find(item => item.clientOrderId === cancel[1]);
      if (!row) return new Response(JSON.stringify({ detail: "simulation_order_not_found" }), { status: 404 });
      Object.assign(row, { remoteStatus: "FINISHED", displayStatus: "已撤销" });
      return json(row);
    }
    if (url.includes("/api/simulation/orders")) return json({ items: (links[accountOf(init)] ?? []).map(item => ({ ...item })) });
    if (url.endsWith("/api/accounts")) return json({ accounts: (granted ?? Object.keys(links)).map(id => ({ id })), current: "" });
    return json({});
  });
});
afterEach(() => { vi.unstubAllGlobals(); setActiveTradingAccount(null); });

const handle = {
  baseUrl: "http://127.0.0.1:1", connection: "connected" as const, sessionId: "s-1", replayPath: "",
  fence: { context: { identity: 1 }, isCurrent: () => true,
           channel: () => ({ noteWrite: () => undefined, beginRead: () => () => true }) },
  orderContext: { identity: 1 }, isCurrentOrderContext: () => true, setToast: () => undefined,
};
const busy: ProposalBusy = { id: undefined, set: () => ({}) };
const ticket = { current: { market: undefined, enabled: false, onAccountChanged: () => undefined } };
const listReads = () => fetchMock.mock.calls.filter(([url, init]) => String(url).includes("/api/simulation/orders?") && !init?.method);

test("mergeOrderFacts keeps the later fact and never regresses a fill", () => {
  const accepted = order("已受理");
  const filled = order("已成交", 2);
  expect(mergeOrderFacts(accepted, filled)).toBe(filled);
  expect(mergeOrderFacts(filled, accepted)).toBe(filled);  // a stale 已受理 cannot un-fill the row
  const older = order("部分成交", 1, 3);
  const newer = order("部分成交", 1, 7);
  expect(mergeOrderFacts(newer, older)).toBe(newer);
});

test("a feed.order fact lands in the orders list: the confirmed card's rows follow the fill live (2026-10-08)", async () => {
  const { result } = renderHook(() => useSimulationOrders({ handle, busy, ticket, accountId: "acc-1" }));
  await waitFor(() => expect(listReads()).toHaveLength(1));
  expect(result.current.simulationOrders).toEqual([]);
  // What the Runtime pushes when the watch's read-back changes: the same view the list serves.
  const fact = { ...order("已成交", 2), remoteStatus: "FINISHED", accountId: "acc-1" };
  const event = { schemaVersion: 1, id: "e1", sessionId: "s-1", kind: "result", name: "feed.order",
    timestamp: "2026-10-08T01:30:00Z", payload: fact } as unknown as UiEvent;
  act(() => { applyFeedEvent("s-1", event); });
  expect(result.current.simulationOrders).toHaveLength(1);
  expect(result.current.simulationOrders[0]).toMatchObject({ clientOrderId: "CO-1", displayStatus: "已成交", filledVolume: 2 });
  // Another session's fact is not this window's order.
  act(() => { applyFeedEvent("s-other", { ...event, sessionId: "s-other", payload: { ...fact, clientOrderId: "CO-2" } } as UiEvent); });
  expect(result.current.simulationOrders).toHaveLength(1);
  // Nor is another account's (#930).
  act(() => { applyFeedEvent("s-1", { ...event, id: "e3", payload: { ...fact, clientOrderId: "CO-3", accountId: "acc-2" } } as UiEvent); });
  expect(result.current.simulationOrders).toHaveLength(1);
});

test("#930 the list is the session's account's: read once the account is known, with it, and again on a switch", async () => {
  links = { "(default)": [resting("TQ-1", 3075, "FINISHED", "已成交")], "paper:p1": [resting("AIT-3000", 3000)], "tqkq:t1": [resting("TQ-2", 3010)] };
  const hook = renderHook((props: { accountId: string | null | undefined }) =>
    useSimulationOrders({ handle, busy, ticket, accountId: props.accountId }), { initialProps: { accountId: undefined as string | null | undefined } });
  // Connected but runtime.status has not named the account yet: a read now would land on the user's
  // default account (the 10-15 reload showed the TqKq list on a 本站模拟 session) — no read.
  await act(async () => { await Promise.resolve(); });
  expect(listReads()).toHaveLength(0);
  hook.rerender({ accountId: "paper:p1" });
  await waitFor(() => expect(hook.result.current.simulationOrders.map(item => item.clientOrderId)).toEqual(["AIT-3000"]));
  expect(accountOf(listReads()[0][1])).toBe("paper:p1");
  // Switching the session's account drops the old rows at once and reads the new account's.
  hook.rerender({ accountId: "tqkq:t1" });
  expect(hook.result.current.simulationOrders).toEqual([]);
  await waitFor(() => expect(hook.result.current.simulationOrders.map(item => item.clientOrderId)).toEqual(["TQ-2"]));
  expect(accountOf(listReads().at(-1)![1])).toBe("tqkq:t1");
});

test("#1005 a card reads its own account's orders: switching A→B keeps A's filled card 已成交 and B's list B's", async () => {
  const filledA = { ...resting("AIT-A", 3000, "FINISHED", "已成交"), proposalId: "card-a" } as SimulationOrder;
  const cancelledB = { ...resting("TQ-B", 3010, "FINISHED", "已撤销"), proposalId: "card-b" } as SimulationOrder;
  links = { "paper:a": [filledA], "tqkq:b": [cancelledB] };
  const cardAccounts = ["paper:a", "tqkq:b"];
  const hook = renderHook((props: { accountId: string }) =>
    useSimulationOrders({ handle, busy, ticket, accountId: props.accountId, cardAccounts }), { initialProps: { accountId: "paper:a" } });
  const status = (account: string | undefined, proposalId: string) =>
    hook.result.current.ordersFor(account)?.find(item => item.proposalId === proposalId)?.displayStatus;
  // On A: A's card reads the current list; B's card reads B's own list (named in the read), not A's.
  await waitFor(() => expect(status("tqkq:b", "card-b")).toBe("已撤销"));
  await waitFor(() => expect(status("paper:a", "card-a")).toBe("已成交"));
  expect(hook.result.current.ordersFor(undefined)).toBe(hook.result.current.simulationOrders);  // no account: as before
  // Switch to B: the current list is B's alone; A's card still reads A's own orders.
  hook.rerender({ accountId: "tqkq:b" });
  await waitFor(() => expect(status("paper:a", "card-a")).toBe("已成交"));
  await waitFor(() => expect(status("tqkq:b", "card-b")).toBe("已撤销"));
  expect(hook.result.current.simulationOrders.map(item => item.clientOrderId)).toEqual(["TQ-B"]);
  expect(listReads().filter(([, init]) => accountOf(init) === "paper:a").length).toBeGreaterThanOrEqual(2);
  // A watched A order's fact pushed while on B reaches A's cards, never B's list.
  const event = { schemaVersion: 1, id: "e-a", sessionId: "s-1", kind: "result", name: "feed.order", timestamp: "2026-10-19T01:00:00Z",
    payload: { ...resting("AIT-A2", 3001, "FINISHED", "已成交"), proposalId: "card-a2", accountId: "paper:a" } } as unknown as UiEvent;
  act(() => { applyFeedEvent("s-1", event); });
  expect(status("paper:a", "card-a2")).toBe("已成交");
  expect(hook.result.current.simulationOrders.map(item => item.clientOrderId)).toEqual(["TQ-B"]);
});

test("#1011 confirming another account's card never puts its order into the current account's list", async () => {
  links = { "tqkq:b": [{ ...resting("TQ-B", 3010), proposalId: "card-b" } as SimulationOrder] };
  const { result } = renderHook(() => useSimulationOrders({ handle, busy, ticket, accountId: "tqkq:b" }));
  await waitFor(() => expect(result.current.simulationOrders.map(item => item.clientOrderId)).toEqual(["TQ-B"]));
  // A strategy's stop-close card on paper:a confirmed while the conversation is on tqkq:b.
  const confirmedA = { ...resting("AIT-A", 3000, "ALIVE", "已受理"), proposalId: "card-a" } as SimulationOrder;
  act(() => { result.current.recordConfirmedOrders("paper:a", [confirmedA]); });
  expect(result.current.simulationOrders.map(item => item.clientOrderId)).toEqual(["TQ-B"]);   // B's list untouched
  expect(result.current.ordersFor("paper:a")?.find(item => item.proposalId === "card-a")?.displayStatus).toBe("已受理");
  // A card on the current account still lands in the current list.
  const confirmedB = { ...resting("TQ-B2", 3011), proposalId: "card-b2" } as SimulationOrder;
  act(() => { result.current.recordConfirmedOrders("tqkq:b", [confirmedB]); });
  expect(result.current.simulationOrders.map(item => item.clientOrderId)).toEqual(["TQ-B2", "TQ-B"]);
});

test("#932 F1 review: refreshing another account's card re-reads that account's list, never the current one's", async () => {
  links = { "tqkq:b": [{ ...resting("TQ-B", 3010), proposalId: "card-b" } as SimulationOrder] };
  const { result } = renderHook(() => useSimulationOrders({ handle, busy, ticket, accountId: "tqkq:b" }));
  await waitFor(() => expect(result.current.simulationOrders.map(item => item.clientOrderId)).toEqual(["TQ-B"]));
  // A's card was confirmed but refused at send: its order now reads 待发送 on A.
  links["paper:a"] = [{ ...resting("AIT-A", 3000, "NOT_FOUND", "待发送"), proposalId: "card-a", factRevision: 3 } as SimulationOrder];
  await act(async () => { await result.current.refreshOrdersFor("paper:a"); });
  expect(result.current.ordersFor("paper:a")?.find(item => item.proposalId === "card-a")?.displayStatus).toBe("待发送");
  expect(result.current.simulationOrders.map(item => item.clientOrderId)).toEqual(["TQ-B"]);
  // An older read of A landing afterwards (the background read started while dispatch was in flight) cannot undo it.
  links["paper:a"] = [{ ...resting("AIT-A", 3000, "ALIVE", "结果未知"), proposalId: "card-a", factRevision: 2 } as SimulationOrder];
  await act(async () => { await result.current.refreshOrdersFor("paper:a"); });
  expect(result.current.ordersFor("paper:a")?.find(item => item.proposalId === "card-a")?.displayStatus).toBe("待发送");
});

test("#930 an order placed from another session shows up through the account read-back and can be cancelled here", async () => {
  links = { "paper:p1": [] };
  const hook = renderHook((props: { remote: RemoteOrderSnapshot[] | undefined }) => {
    const orders = useSimulationOrders({ handle, busy, ticket, accountId: "paper:p1" });
    useOrdersFollowAccount(orders.simulationOrders, props.remote, orders.refreshSimulationOrders);
    return orders;
  }, { initialProps: { remote: [] as RemoteOrderSnapshot[] | undefined } });
  await waitFor(() => expect(listReads()).toHaveLength(1));
  expect(hook.result.current.simulationOrders).toEqual([]);
  // The CLI (its own session) confirms a far limit order: the Runtime records the link, the engine
  // works it, and only the account frame (feed.account → activity.orders) reaches this window.
  links["paper:p1"].push(resting("AIT-3000", 3000));
  hook.rerender({ remote: [remote("AIT-3000")] });
  await waitFor(() => expect(hook.result.current.simulationOrders.map(item => item.clientOrderId)).toEqual(["AIT-3000"]));
  expect(listReads()).toHaveLength(2);
  // An account frame that only re-states what the list already shows reads nothing more.
  hook.rerender({ remote: [remote("AIT-3000")] });
  await act(async () => { await Promise.resolve(); });
  expect(listReads()).toHaveLength(2);
  // 撤单 from this window goes to the same account's order, through the existing cancel route.
  await act(async () => { await hook.result.current.simulationOrderAction(hook.result.current.simulationOrders[0], "cancel"); });
  const cancel = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith("/api/simulation/orders/AIT-3000/cancel") && init?.method === "POST");
  expect(accountOf(cancel![1])).toBe("paper:p1");
  expect(hook.result.current.simulationOrders[0]).toMatchObject({ remoteStatus: "FINISHED", displayStatus: "已撤销" });
});

test("another account's card order cancels with its serialized account, not the current CNY account", async () => {
  const current = "paper:p1", other = "paperusd:p1";
  links = { [current]: [{ ...resting("AIT-cny", 3000), accountId: current }],
    [other]: [{ ...resting("AIT-usd", 3.0701), accountId: other, proposalId: "card-usd" }] };
  function CardOrders() {
    const orders = useSimulationOrders({ handle, busy, ticket, accountId: current, cardAccounts: [other] });
    return createElement(OrderRows, { orders: [...orders.simulationOrders, ...orders.ordersFor(other) ?? []], history: false, busy: false,
      limit: 10, onOrderAction: orders.simulationOrderAction, onAbandon: () => undefined });
  }
  const view = render(createElement(CardOrders));
  await screen.findByText("美元");
  const usd = view.container.querySelector<HTMLElement>('[data-order-id="AIT-usd"]')!;
  const cny = view.container.querySelector<HTMLElement>('[data-order-id="AIT-cny"]')!;
  expect(within(cny).queryByText("美元")).toBeNull();
  fireEvent.click(within(usd).getByRole("button", { name: "撤单" }));
  await waitFor(() => expect(within(usd).queryByRole("button", { name: "撤单" })).toBeNull());
  expect(within(usd).getByText("已撤销")).toBeInTheDocument();
  expect(within(cny).getByRole("button", { name: "撤单" })).toBeInTheDocument();
  const cancels = fetchMock.mock.calls.filter(([url, init]) => String(url).endsWith("/cancel") && init?.method === "POST");
  expect(cancels).toHaveLength(1);
  expect(String(cancels[0][0])).toContain("/api/simulation/orders/AIT-usd/cancel");
  expect(accountOf(cancels[0][1])).toBe(other);
  expect(links[current][0].displayStatus).toBe("已受理");
});

test.each(["cancel", "continue", "abandon"] as const)("409 on %s queries the original account and updates the card without repeating the action", async action => {
  const current = "paper:p1", other = "paperusd:p1";
  const pending = { ...resting("AIT-usd", 3.0701), accountId: other };
  links = { [current]: [resting("AIT-cny", 3000)], [other]: [pending] };
  const setToast = vi.fn();
  const hook = renderHook(() => useSimulationOrders({ handle: { ...handle, setToast }, busy, ticket,
    accountId: current, cardAccounts: [other] }));
  await waitFor(() => expect(hook.result.current.ordersFor(other)).toHaveLength(1));
  const ended = { ...pending, remoteStatus: "FINISHED", displayStatus: "已撤销", factRevision: 3 };
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => String(input).endsWith("/query")
    ? json(ended) : new Response(JSON.stringify({ detail: "already_finished" }), { status: 409 }));
  await act(async () => { await hook.result.current.simulationOrderAction(pending, action); });
  expect(hook.result.current.ordersFor(other)?.[0]).toEqual(ended);
  expect(hook.result.current.simulationOrders.map(item => item.clientOrderId)).toEqual(["AIT-cny"]);
  const writes = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
  expect(writes.map(([url]) => String(url).split("/").at(-1))).toEqual([action, "query"]);
  expect(writes.every(([, init]) => accountOf(init) === other)).toBe(true);
  expect(setToast).toHaveBeenLastCalledWith(expect.stringContaining("这笔委托已经结束（已撤销）"), "warning");
  // A late feed fact cannot undo the queried terminal fact.
  act(() => { applyFeedEvent("s-1", { sessionId: "s-1", name: "feed.order", payload: pending } as unknown as UiEvent); });
  expect(hook.result.current.ordersFor(other)?.[0]).toEqual(ended);
});

test("a 409 whose query fails preserves the row and asks for read-back, never claims the order ended", async () => {
  const pending = { ...resting("AIT-cny", 3000), accountId: "paper:p1" };
  links = { "paper:p1": [pending] };
  const setToast = vi.fn();
  const hook = renderHook(() => useSimulationOrders({ handle: { ...handle, setToast }, busy, ticket, accountId: "paper:p1" }));
  await waitFor(() => expect(hook.result.current.simulationOrders).toHaveLength(1));
  fetchMock.mockImplementation(async () => new Response("{}", { status: 409 }));
  await act(async () => { await hook.result.current.simulationOrderAction(pending, "cancel"); });
  expect(hook.result.current.simulationOrders[0]).toEqual(pending);
  expect(setToast).toHaveBeenLastCalledWith("委托状态已变化，但回读失败，请查询原委托核对", "error");
  expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(2);
});

test("外盘仓合并显示 2026-10-21: a USD sub-account order shown in the CNY list is cancelled on its own account, the CNY list untouched", async () => {
  links = { "paper:p1": [resting("AIT-cny", 3000)], "paperusd:p1": [{ ...resting("AIT-usd", 29900), accountId: "paperusd:p1" } as SimulationOrder] };
  const hook = renderHook(() => useSimulationOrders({ handle, busy, ticket, accountId: "paper:p1" }));
  await waitFor(() => expect(hook.result.current.simulationOrders.map(item => item.clientOrderId)).toEqual(["AIT-cny"]));
  await act(async () => { await hook.result.current.simulationOrderAction({ ...resting("AIT-usd", 29900), accountId: "paperusd:p1" }, "cancel"); });
  const cancel = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith("/api/simulation/orders/AIT-usd/cancel") && init?.method === "POST");
  expect(accountOf(cancel![1])).toBe("paperusd:p1");
  expect(links["paperusd:p1"][0]).toMatchObject({ remoteStatus: "FINISHED" });
  expect(hook.result.current.simulationOrders.map(item => item.clientOrderId)).toEqual(["AIT-cny"]);
});

test("#930 ordersBehindAccount: a missing working order or a stale row, never a finished order the list never had", () => {
  const listed = [resting("A", 3000), resting("B", 3010, "FINISHED", "已撤销")];
  expect(ordersBehindAccount(listed, undefined)).toBe("");
  expect(ordersBehindAccount(listed, [remote("A"), remote("B", "FINISHED")])).toBe("");
  expect(ordersBehindAccount(listed, [remote("A"), remote("C")])).toBe("C:ALIVE:0");
  expect(ordersBehindAccount(listed, [remote("A", "FINISHED", 1)])).toBe("A:FINISHED:1");   // cancelled or filled elsewhere
  expect(ordersBehindAccount(listed, [remote("Z", "FINISHED", 1)])).toBe("");              // history the list never held
});

test("#932 A26 the ticket receipt counts the orders actually placed and never calls a confirmed close unconfirmed", () => {
  const child = (clientOrderId: string, offset: "CLOSETODAY" | "CLOSE", volume: number, displayStatus: string): SimulationOrder =>
    ({ ...order(displayStatus), id: clientOrderId, clientOrderId, request: { ...order(displayStatus).request, offset, volume } } as SimulationOrder);
  const today = child("T", "CLOSETODAY", 2, "已受理"), yesterday = child("H", "CLOSE", 1, "已成交");
  expect(manualOrderReceipt(undefined)).toBe("本次提交未确认，请查看委托回报，不会自动重提。");
  expect(manualOrderReceipt({ confirmed: true, accepted: true, orders: [today], skipped: [] }))
    .toBe("模拟委托已受理，共 2 手：卖平今 2 手 @ 3074 已受理；成交状态见委托分页。");
  // Each accepted leg says its own price and status (one may already be filled).
  expect(manualOrderReceipt({ confirmed: true, accepted: true, orders: [today, yesterday], skipped: [] }))
    .toBe("模拟委托已受理，共 3 手：卖平今 2 手 @ 3074 已受理、卖平昨 1 手 @ 3074 已成交；成交状态见委托分页。");
  // A bucket the planner skipped: 3 asked, 2 placed — the receipt says 2 and names the skipped lot.
  expect(manualOrderReceipt({ confirmed: true, accepted: true, orders: [today], skipped: [{ volume: 1, label: "rb2701 无最新报价" }] }))
    .toBe("模拟委托已受理，共 2 手：卖平今 2 手 @ 3074 已受理；另 1 手未纳入（rb2701 无最新报价）；成交状态见委托分页。");
  // Confirmed, one child refused by the exchange: each child's own status, no hint to submit again.
  const refused = manualOrderReceipt({ confirmed: true, accepted: false, orders: [today, child("H", "CLOSE", 1, "已拒绝")], skipped: [] });
  expect(refused).toBe("已确认，共 3 手：卖平今 2 手 @ 3074 已受理、卖平昨 1 手 @ 3074 已拒绝。系统不会自动重提，逐笔见委托分页。");
  expect(refused).not.toMatch(/未确认/);
  expect(manualOrderReceipt({ confirmed: true, accepted: false, orders: [today, child("H", "CLOSE", 1, "待发送")], skipped: [] }))
    .toBe("已确认，共 3 手：卖平今 2 手 @ 3074 已受理、卖平昨 1 手 @ 3074 待发送。未发送的在委托分页继续发送或放弃，系统不会自动重发。");
  expect(manualOrderReceipt({ confirmed: true, accepted: false, orders: [today, child("H", "CLOSE", 1, "结果未知")], skipped: [] }))
    .toBe("已确认，共 3 手：卖平今 2 手 @ 3074 已受理、卖平昨 1 手 @ 3074 结果未知。结果未知的先在委托分页查询，不要重复下单，系统不会自动重发。");
  // An unknown child and an unsent one: both actions are named.
  expect(manualOrderReceipt({ confirmed: true, accepted: false, orders: [child("T", "CLOSETODAY", 2, "结果未知"), child("H", "CLOSE", 1, "待发送")], skipped: [] }))
    .toBe("已确认，共 3 手：卖平今 2 手 @ 3074 结果未知、卖平昨 1 手 @ 3074 待发送。结果未知的先在委托分页查询，不要重复下单；未发送的在委托分页继续发送或放弃，系统不会自动重发。");
});


test("real account/confirmation hooks bind each send to the accepted account and retire a switched intent", async () => {
  const created: string[] = [], confirmed: string[] = [];
  let answer!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input), target = accountOf(init);
    if (url.includes("/accounts")) return json({ accounts: [], current: "A" });
    if (url.endsWith("/api/simulation/proposals")) {
      created.push(target);
      return new Promise<Response>(resolve => { answer = resolve; });
    }
    if (url.endsWith("/confirm")) { confirmed.push(target); return json({}); }
    return json({ items: [] });
  }));
  const quote = { symbol: "SHFE.au2612" };
  const runtime = (id: string, generation: number) => ({ account: { id }, accountGeneration: generation }) as RuntimeStatus;
  const hook = renderHook(({ status }) => {
    const accounts = useTradingAccounts(handle.baseUrl, "same-session", status);
    const fence = useIdentityFence([handle.baseUrl, "same-session", accounts.currentId, status.accountGeneration]);
    const live = { ...handle, sessionId: "same-session", fence, orderContext: fence.context, isCurrentOrderContext: fence.isCurrent };
    const lane = useProposalBusy(live);
    const orders = useSimulationOrders({ handle: live, busy: lane, accountId: accounts.currentId,
      ticket: { current: { market: { quote } as never, enabled: true, onAccountChanged: () => undefined } } });
    const quick = useQuickOrders({ quote: quote as never, runtimeBaseUrl: handle.baseUrl, contextKey: "same-session",
      accountId: accounts.currentId, accountGeneration: status.accountGeneration, tradeBusy: Boolean(lane.id),
      simulationEnabled: true, submitManualOrder: orders.submitManualSimulationOrder, cancelOrder: () => undefined, skipConfirm: false });
    return { quick, orders };
  }, { initialProps: { status: runtime("A", 1) } });
  const request = { side: "buy_open" as const, volume: 1, priceIntent: "aggressive" as const, source: "three-key" as const };
  act(() => hook.result.current.quick.requestAction({ kind: "orders", requests: [request] }));
  const old = hook.result.current.quick.pending!;
  hook.rerender({ status: runtime("B", 2) });
  await act(async () => { await hook.result.current.quick.submitQuick(old); });
  hook.rerender({ status: runtime("A", 3) });
  await act(async () => { await hook.result.current.quick.submitQuick(old); });
  expect(created).toEqual([]);
  act(() => hook.result.current.quick.requestAction({ kind: "orders", requests: [request] }));
  let sending!: Promise<void>;
  act(() => { sending = hook.result.current.quick.submitQuick(hook.result.current.quick.pending!); });
  await waitFor(() => expect(created).toEqual(["A"]));
  hook.rerender({ status: runtime("B", 4) });
  await act(async () => {
    answer(json({ proposalId: "original-A", approvalId: "approval-A", version: 1, executionEligibleAtCreation: true }));
    await sending;
  });
  expect(confirmed).toEqual([]);  // candidate returned after switch is never confirmed
  expect(hook.result.current.orders.simulationOrders).toEqual([]);
});


test("an accepted 409 account override fails closed until runtime.status catches up", async () => {
  const created: string[] = [];
  const sessionId = "account-override-409";
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/account") && init?.method === "PUT") return new Response(JSON.stringify({
      detail: { code: "account_switch_superseded", accountGeneration: 2, accountId: "B" } }), { status: 409 });
    if (url.endsWith("/api/simulation/proposals")) {
      created.push(accountOf(init));
      return new Response(JSON.stringify({ detail: "fixture_refusal_before_candidate" }), { status: 422 });
    }
    return json(url.endsWith("/api/accounts") ? { current: "B", accounts: [{ id: "A", role: "owner" }, { id: "B", role: "owner" }] } : { items: [] });
  }));
  const runtime = (id: string, accountGeneration: number) => ({ account: { id }, accountGeneration }) as RuntimeStatus;
  const hook = renderHook(({ status }) => {
    const accounts = useTradingAccounts(handle.baseUrl, sessionId, status);
    const fence = useIdentityFence([handle.baseUrl, sessionId, status.account?.id, status.accountGeneration]);
    const live = { ...handle, sessionId, fence, orderContext: fence.context, isCurrentOrderContext: fence.isCurrent };
    const lane = useProposalBusy(live);
    const quote = { symbol: "SHFE.au2612" };
    const orders = useSimulationOrders({ handle: live, busy: lane, accountId: status.account?.id,
      ticket: { current: { market: { quote } as never, enabled: true, onAccountChanged: () => undefined } } });
    const quick = useQuickOrders({ quote: quote as never, sessionId, runtimeBaseUrl: handle.baseUrl,
      contextKey: "same-session", accountId: accounts.currentId, accountGeneration: status.accountGeneration,
      tradeBusy: Boolean(lane.id), simulationEnabled: true, submitManualOrder: orders.submitManualSimulationOrder,
      skipConfirm: false, cancelOrder: () => undefined });
    return { accounts, quick, orders };
  }, { initialProps: { status: runtime("A", 1) } });
  await act(async () => { await hook.result.current.accounts.requestSwitch("B"); });
  await waitFor(() => expect(runtimeHeaders(handle.baseUrl).get("X-AI-Trader-Account")).toBe("B"));
  expect(hook.result.current.accounts.currentId).toBe("B");
  const request = { side: "buy_open" as const, volume: 1, priceIntent: "aggressive" as const, source: "three-key" as const };
  act(() => hook.result.current.quick.requestAction({ kind: "orders", requests: [request] }));
  const old = hook.result.current.quick.pending!;
  await act(async () => {
    await hook.result.current.quick.submitQuick(old);
    await hook.result.current.orders.submitManualSimulationOrder(request);
  });
  expect(created).toEqual([]); // UI/header B and stale runtime A must never create on A.
  hook.rerender({ status: runtime("B", 2) });
  expect(hook.result.current.quick.pending).toBeUndefined();
  act(() => hook.result.current.quick.requestAction({ kind: "orders", requests: [request] }));
  await act(async () => { await hook.result.current.quick.submitQuick(hook.result.current.quick.pending!); });
  expect(created).toEqual(["B"]);
  hook.rerender({ status: runtime("A", 3) });
  await act(async () => { await hook.result.current.quick.submitQuick(old); });
  expect(created).toEqual(["B"]);
});

test("快捷区反手 (2026-10-24): one confirmation = the 目标持仓 route at −net, then that card's ordinary confirm; no client relay", async () => {
  const calls: string[] = [];
  const toasts: string[] = [];
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/simulation/target-position/proposals")) {
      calls.push(`target ${JSON.parse(String(init?.body)).targetNet}`);
      return json({ cardIssued: true, proposalId: "p-rev", approvalId: "ap-rev", proposalVersion: 1,
        targetPosition: { summary: "目标 -2 手，当前 +2 手 → 平多 2 手，成交后自动开空 2 手（确认一次）" } });
    }
    if (url.endsWith("/api/proposals/p-rev/confirm")) {
      const body = JSON.parse(String(init?.body));
      calls.push(`confirm ${body.approvalId} v${body.proposalVersion}`);
      return json({ simulationOrders: [{ ...order("已受理"), id: "o-rev", clientOrderId: "CO-rev" }] });
    }
    if (url.includes("/api/simulation/orders")) return json({ items: [] });
    return json({});
  });
  const quote = { symbol: "SHFE.rb2701" };
  const { result } = renderHook(() => useSimulationOrders({ handle: { ...handle, setToast: (line?: string) => { toasts.push(line ?? ""); } },
    busy, accountId: "acc-1", ticket: { current: { market: { quote } as never, enabled: true, onAccountChanged: () => undefined } } }));
  let confirmed = false;
  await act(async () => { confirmed = await result.current.submitTargetPosition({ symbol: "SHFE.rb2701", targetNet: -2, priceIntent: "aggressive", confirm: true }); });
  expect(confirmed).toBe(true);
  expect(calls).toEqual(["target -2", "confirm ap-rev v1"]);
  expect(result.current.simulationOrders.map(item => item.clientOrderId)).toContain("CO-rev");
  expect(toasts.at(-1)).toContain("反手已确认");
});


test("#1205 an old card's account the user may no longer act on is never read (no 403 on every load)", async () => {
  links = { "paper:a": [], "tqkq:retired": [{ ...resting("TQ-OLD", 3010, "FINISHED", "已成交"), proposalId: "card-old" } as SimulationOrder] };
  granted = ["paper:a"];
  renderHook(() => useSimulationOrders({ handle, busy, ticket, accountId: "paper:a", cardAccounts: ["tqkq:retired"] }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/api/accounts"))).toBe(true));
  await waitFor(() => expect(listReads().some(([, init]) => accountOf(init) === "paper:a")).toBe(true));
  expect(listReads().filter(([, init]) => accountOf(init) === "tqkq:retired")).toEqual([]);
});

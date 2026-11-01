import { beforeEach, describe, expect, test, vi } from "vitest";
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { AccountPosition, MarketQuoteSnapshot, SimulationOrder } from "../../api";
import { resetShortcutStore, setShortcut } from "../../shortcuts";
import { noteAccountRevision, resetAccountRevisions } from "../../accountRevision";
import { applyUiSurface } from "../../uiSurfaces";
import { QuickActions, quickCloseRequests, useTicketQuickActions, QUICK_VOLUME_KEY } from "./QuickActions";
import { useQuickOrders } from "./useQuickOrders";
import { QuickOrderDialog } from "./QuickOrderDialog";

const quote = { symbol: "SHFE.au2612", lastPrice: 824, priceTick: .02, askPrice1: 824.02, bidPrice1: 823.98 } as MarketQuoteSnapshot;
const held = (volume = 4, todayVolume = 2): AccountPosition => ({ symbol: quote.symbol, direction: "long", volume, todayVolume, historicalVolume: volume - todayVolume, unrealizedPnl: 0 });
const order = (id: string, accountId: string, symbol = quote.symbol) => ({ clientOrderId: id, accountId, request: { symbol }, remoteStatus: "ALIVE", cancelPhase: "NONE" }) as SimulationOrder;
const submit = vi.fn(async () => undefined);
const cancel = vi.fn(async (_: SimulationOrder): Promise<void> => undefined);
const target = vi.fn(async (_: { symbol: string; targetNet: number; priceIntent?: "aggressive"; confirm: true }) => true);
function Harness({ symbol = quote.symbol, positions = [held()], orders = [order("a", "first"), order("b", "second")], skip = false, contextKey = "a", accountId = "A", accountGeneration = 1 }: {
  symbol?: string; positions?: AccountPosition[]; orders?: SimulationOrder[]; skip?: boolean; contextKey?: string; accountId?: string; accountGeneration?: number;
}) {
  const q = { ...quote, symbol };
  const quick = useQuickOrders({ quote: q, tradeBusy: false, runtimeBaseUrl: "local", simulationEnabled: true, submitManualOrder: submit, cancelOrder: cancel, submitTargetPosition: target, skipConfirm: skip, contextKey, accountId, accountGeneration });
  const controller = useTicketQuickActions({ quote: q, positions, orders, busy: false, enabled: true, quick });
  return <><input aria-label="typing" /><QuickActions controller={controller} onSettings={() => undefined} />
    <QuickOrderDialog pending={quick.pending} decimals={2} busy={quick.quickBusy} onClose={() => quick.setPending(undefined)} onConfirm={value => void quick.submitQuick(value)} /></>;
}
beforeEach(() => { localStorage.clear(); resetShortcutStore(); resetAccountRevisions(); submit.mockClear(); cancel.mockClear(); target.mockClear(); });

describe("反手 (2026-10-24): one confirmation, the Runtime relays the opening leg", () => {
  test.each([
    ["flat", [] as AccountPosition[]],
    ["long = short", [held(2, 0), { ...held(2, 0), direction: "short" as const }]],
    ["another contract only", [{ ...held(), symbol: "DCE.i2701" }]],
  ])("disabled when %s", (_, positions) => {
    render(<Harness positions={positions} />);
    expect(screen.getByRole("button", { name: "一键反手" })).toBeDisabled();
    fireEvent.keyDown(window, { key: "7", code: "Digit7", altKey: true });
    expect(screen.getByTestId("quick-order-dialog")).not.toHaveAttribute("open");
  });
  test("⌥7 confirms first with both steps stated, then calls 目标持仓 at −net with confirm, once", async () => {
    render(<Harness positions={[held(4, 2)]} />);
    expect(screen.getByRole("button", { name: "一键反手" })).toBeEnabled();
    fireEvent.keyDown(window, { key: "7", code: "Digit7", altKey: true });
    expect(target).not.toHaveBeenCalled();
    expect(screen.getByTestId("quick-order-dialog")).toHaveTextContent("反手：SHFE.au2612 平多 4 手，成交后自动开空 4 手（确认一次）");
    fireEvent.click(screen.getByRole("button", { name: "确认提交" }));
    await waitFor(() => expect(target).toHaveBeenCalledTimes(1));
    expect(target).toHaveBeenCalledWith({ symbol: quote.symbol, targetNet: -4, priceIntent: "aggressive", confirm: true });
    expect(submit).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("反手已确认"));
  });
  test("a short holding reverses to long; skip-confirm sends at once", async () => {
    render(<Harness skip positions={[{ ...held(3, 0), direction: "short" }]} />);
    fireEvent.click(screen.getByRole("button", { name: "一键反手" }));
    await waitFor(() => expect(target).toHaveBeenCalledWith(expect.objectContaining({ targetNet: 3, confirm: true })));
  });
});

describe("quick closes use the existing manual request", () => {
  test.each([[1, 1], [3, 1], [4, 2]])("half %i → %i", (volume, expected) => {
    expect(quickCloseRequests([held(volume, 0)], quote.symbol, "half")).toEqual([{ side: "sell_close", volume: expected, source: "three-key", priceIntent: "aggressive" }]);
  });
  test("today only sends today's available lots, all/half leave allocation to Runtime", () => {
    const position = { ...held(), frozenToday: 1 };
    expect(quickCloseRequests([position], quote.symbol, "today")[0]).toMatchObject({ volume: 1, positionBucket: "today" });
    expect(quickCloseRequests([position], quote.symbol, "all")[0]).toMatchObject({ volume: 3 });
    expect(quickCloseRequests([position], quote.symbol, "half")[0]).not.toHaveProperty("positionBucket");
  });
  test.each([["SHFE.au2612", 2, true], ["INE.sc2612", 2, true], ["DCE.i2701", 2, false], ["SHFE.au2612", 0, false]])("today visibility %s / %i", (symbol, today, visible) => {
    render(<Harness symbol={symbol} positions={[{ ...held(4, today), symbol }]} />);
    expect(Boolean(screen.queryByRole("button", { name: "一键平今" }))).toBe(visible);
  });
});
test("keyboard and click use the same confirmation; input and Cancel Enter never confirm; confirmation button submits once", async () => {
  render(<Harness />);
  fireEvent.keyDown(screen.getByLabelText("typing"), { key: "1", code: "Digit1", altKey: true });
  expect(screen.getByTestId("quick-order-dialog")).not.toHaveAttribute("open");
  fireEvent.click(screen.getByRole("button", { name: "快捷手数 5" }));
  fireEvent.keyDown(window, { key: "¡", code: "Digit1", altKey: true });
  expect(submit).not.toHaveBeenCalled();
  expect(screen.getByTestId("quick-order-dialog")).toHaveTextContent("5 手");
  fireEvent.keyDown(screen.getByRole("button", { name: "取消" }), { key: "Enter" });
  expect(submit).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认提交" }));
  await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
  expect(submit).toHaveBeenCalledWith({ side: "buy_open", volume: 5, priceIntent: "aggressive", source: "three-key" });
});
test("skip-confirm submits directly; stored quick volume independent of the manual ticket", async () => {
  localStorage.setItem(QUICK_VOLUME_KEY, "2");
  render(<Harness skip />);
  fireEvent.click(screen.getByRole("button", { name: "一键卖空" }));
  await waitFor(() => expect(submit).toHaveBeenCalledWith(expect.objectContaining({ side: "sell_open", volume: 2 })));
  expect(screen.getByTestId("quick-order-dialog")).not.toHaveAttribute("open");
});
test("cancel only this contract's cancellable orders, sequentially preserving each account", async () => {
  const orders = [order("a", "first"), order("b", "second"), order("other", "other", "DCE.i2701"), { ...order("done", "first"), remoteStatus: "FINISHED" } as SimulationOrder];
  let finishFirst!: () => void;
  cancel.mockImplementationOnce(() => new Promise<void>(resolve => { finishFirst = resolve; }));
  render(<Harness orders={orders} />);
  fireEvent.keyDown(window, { key: "6", code: "Digit6", altKey: true });
  expect(cancel).not.toHaveBeenCalled();
  expect(screen.getByTestId("quick-order-dialog")).toHaveTextContent("2 笔");
  fireEvent.click(screen.getByRole("button", { name: "确认提交" }));
  await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
  finishFirst();
  await waitFor(() => expect(cancel).toHaveBeenCalledTimes(2));
  expect(cancel.mock.calls.map(([o]) => [o.clientOrderId, o.accountId])).toEqual([["a", "first"], ["b", "second"]]);
});
test("scope switch discards a pending action, UI command changes only local lots", async () => {
  const view = render(<Harness />);
  applyUiSurface("quickVolume", 5);
  await waitFor(() => expect(screen.getByRole("button", { name: "快捷手数 5" })).toHaveAttribute("aria-pressed", "true"));
  fireEvent.click(screen.getByRole("button", { name: "一键平仓" }));
  view.rerender(<Harness contextKey="b" />);
  expect(screen.getByTestId("quick-order-dialog")).not.toHaveAttribute("open");
  expect(submit).not.toHaveBeenCalled();
});
test("custom quick binding rejects conflicts and the changed binding triggers confirmation", () => {
  expect(setShortcut("quickBuy", "Alt+2")).toContain("一键卖空");
  expect(setShortcut("quickBuy", "Alt+9")).toBeNull();
  render(<Harness />);
  fireEvent.keyDown(window, { key: "9", code: "Digit9", altKey: true });
  expect(screen.getByTestId("quick-order-dialog")).toHaveAttribute("open");
});

test("quick buy/sell show the matching opponent prices", () => {
  render(<Harness />);
  expect(screen.getByRole("button", {name:"一键买多"})).toHaveTextContent("824.02");
  expect(screen.getByRole("button", {name:"一键卖空"})).toHaveTextContent("823.98");
});


test("account transitions retire old confirmations including A → B → A", async () => {
  const view = render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "一键买多" }));
  expect(screen.getByTestId("quick-order-dialog")).toHaveAttribute("open");
  view.rerender(<Harness accountId="B" accountGeneration={2} />);
  expect(screen.getByTestId("quick-order-dialog")).not.toHaveAttribute("open");
  view.rerender(<Harness accountId="A" accountGeneration={3} />);
  expect(screen.getByTestId("quick-order-dialog")).not.toHaveAttribute("open");
  expect(submit).not.toHaveBeenCalled();
});

test("a mid-batch account switch stops remaining requests and duplicate confirmation", async () => {
  let finish!: (value: Awaited<ReturnType<typeof submit>>) => void;
  submit.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const hook = renderHook(({ accountId }) => useQuickOrders({ quote, runtimeBaseUrl: "local", accountId,
    tradeBusy: false, simulationEnabled: true, submitManualOrder: submit, cancelOrder: cancel,
    skipConfirm: false }), { initialProps: { accountId: "A" } });
  const request = { side: "buy_open" as const, volume: 1, priceIntent: "aggressive" as const, source: "three-key" as const };
  act(() => hook.result.current.requestAction({ kind: "orders", requests: [request, request] }));
  const old = hook.result.current.pending!;
  let sent!: Promise<void>;
  act(() => { sent = hook.result.current.submitQuick(old); void hook.result.current.submitQuick(old); });
  expect(submit).toHaveBeenCalledTimes(1);
  hook.rerender({ accountId: "B" });
  hook.rerender({ accountId: "A" });
  await act(async () => { finish({ accepted: true } as never); await sent; await hook.result.current.submitQuick(old); });
  expect(submit).toHaveBeenCalledTimes(1);
  expect(hook.result.current.quickMessage).toBe("");
});


test("a newer switch receipt retires the pending intent before runtime.status renders", async () => {
  const hook = renderHook(() => useQuickOrders({ quote, runtimeBaseUrl: "local", accountId: "A", accountGeneration: 1,
    sessionId: "quick-session", tradeBusy: false, simulationEnabled: true, submitManualOrder: submit,
    cancelOrder: cancel, skipConfirm: false }));
  act(() => hook.result.current.requestAction({ kind: "orders", requests: [{ side: "buy_open", volume: 1, priceIntent: "aggressive", source: "three-key" }] }));
  const old = hook.result.current.pending!;
  noteAccountRevision("quick-session", 3); // A → B → A receipt arrived; this render is still generation 1.
  await act(async () => { await hook.result.current.submitQuick(old); });
  expect(submit).not.toHaveBeenCalled();
  expect(hook.result.current.pending).toBeUndefined();
  expect(hook.result.current.quickMessage).toContain("账户状态已变化");
});

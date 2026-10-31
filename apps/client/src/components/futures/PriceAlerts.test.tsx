import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { UiEvent } from "@ai-trader/ui-events";
import { applyFeedEvent, resetFeed } from "../../runtimeFeed";
import { usePriceAlerts, type PriceAlert, type PriceAlertsHandle } from "../../hooks/usePriceAlerts";
import { PriceAlertsControl } from "./PriceAlertsControl";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const alert = (over: Partial<PriceAlert> = {}): PriceAlert => ({
  id: "a1", symbol: "SHFE.au2612", name: "沪金2612", direction: "above", price: "8000",
  note: "", status: "active", via: "ui", sessionId: null, createdAt: "2026-10-02T01:00:00Z",
  triggeredAt: null, triggerPrice: null, ...over,
});

const handle = (over: Partial<PriceAlertsHandle> = {}): PriceAlertsHandle => ({
  available: true, active: [], triggered: [], createError: "", notice: "",
  create: vi.fn(), cancel: vi.fn(), refresh: vi.fn(), ...over,
});

afterEach(() => { vi.unstubAllGlobals(); resetFeed(); });

it("reads the lists on connect; create posts via ui without a direction, then re-reads", async () => {
  const calls: { method?: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    calls.push({ method: init?.method, body: init?.body ? String(init.body) : undefined });
    if (init?.method === "POST") return json({ alert: alert({ id: "a9", note: "破八看多" }) }, 201);
    return json({ active: [alert()], triggered: [] });
  }));
  const { result } = renderHook(() => usePriceAlerts("http://runtime", true));
  await waitFor(() => expect(result.current.active).toHaveLength(1));
  await act(async () => { expect(await result.current.create("SHFE.au2612", 8000, "破八看多")).toBe(true); });
  expect(JSON.parse(calls.at(-2)!.body!)).toEqual({ symbol: "SHFE.au2612", price: 8000, via: "ui", note: "破八看多" });
  expect(result.current.notice).toBe("已设沪金2612 涨到 8000 提醒，备注：破八看多");
  await waitFor(() => expect(calls.filter(call => !call.method)).toHaveLength(2));   // the create's re-read
  await act(async () => { expect(await result.current.create("SHFE.au2612", 7900)).toBe(true); });
  expect(JSON.parse(calls.at(-2)!.body!)).toEqual({ symbol: "SHFE.au2612", price: 7900, via: "ui" });  // no note key, no direction
});

it("a 422 refusal becomes the form's inline Chinese line", async () => {
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) =>
    init?.method === "POST" ? json({ detail: "现价就是 7980，换个不同的价" }, 422) : json({ active: [], triggered: [] })));
  const { result } = renderHook(() => usePriceAlerts("http://runtime", true));
  await waitFor(() => expect(result.current.available).toBe(true));
  await act(async () => { expect(await result.current.create("SHFE.au2612", 7980)).toBe(false); });
  expect(result.current.createError).toBe("现价就是 7980，换个不同的价");
});

it("a crossing the Runtime pushes re-reads the lists", async () => {
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async () => { reads += 1; return json({ active: [], triggered: [] }); }));
  const event = (name: string, payload: unknown): UiEvent =>
    ({ schemaVersion: 1, id: name, sessionId: "s", kind: "result", name, timestamp: "now", payload } as unknown as UiEvent);
  renderHook(() => usePriceAlerts("http://runtime", true));
  await waitFor(() => expect(reads).toBe(1));
  act(() => {
    applyFeedEvent("s", event("feed.price-alert", { id: "a1", symbol: "SHFE.au2612", name: "沪金2612", direction: "above", price: "8000", lastPrice: 8001, note: "", text: "沪金2612 涨到 8000", triggeredAt: null }), 1000);
  });
  await waitFor(() => expect(reads).toBe(2));
});

it("a guest or replay window never reads or writes alerts", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const { result } = renderHook(() => usePriceAlerts(undefined, false));
  expect(result.current.available).toBe(false);
  await act(async () => { expect(await result.current.create("SHFE.au2612", 8000)).toBe(false); });
  expect(fetchMock).not.toHaveBeenCalled();
});

it("the bell counts every active alert and opens the popover", () => {
  render(<PriceAlertsControl alerts={handle({ active: [alert(), alert({ id: "a2", symbol: "DCE.m2701" })] })}
    symbol="SHFE.au2612" name="沪金2612" priceTick={2} lastPrice={7980} />);
  fireEvent.click(screen.getByRole("button", { name: "到价提醒（2 条生效）" }));
  expect(screen.getByText("2")).toBeVisible();   // the badge
  expect(screen.getByRole("dialog", { name: "到价提醒" })).toBeVisible();
});

it("the form defaults to the latest price, steps by one tick and refuses the current price", () => {
  const alerts = handle();
  render(<PriceAlertsControl alerts={alerts} symbol="SHFE.au2612" name="沪金2612" priceTick={2} lastPrice={7980} />);
  fireEvent.click(screen.getByRole("button", { name: "到价提醒（0 条生效）" }));
  const price = screen.getByRole("spinbutton", { name: "提醒价" }) as HTMLInputElement;
  const chip = () => document.querySelector<HTMLElement>(".fdw-alert-direction")!.textContent;
  expect(price.value).toBe("7980");                       // 默认最新价 → 等价，不能直接添加
  expect(chip()).toBe("改成要提醒的价");
  expect(screen.getByRole("button", { name: "添加" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "价格加一跳" }));
  expect(price.value).toBe("7982");
  expect(chip()).toBe("涨到 7982");
  fireEvent.click(screen.getByRole("button", { name: "价格减一跳" }));
  fireEvent.click(screen.getByRole("button", { name: "价格减一跳" }));
  expect(price.value).toBe("7978");
  expect(chip()).toBe("跌到 7978");
  fireEvent.change(price, { target: { value: "7980" } });
  expect(chip()).toBe("改成要提醒的价");
  expect(screen.getByRole("button", { name: "添加" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: "备注" }), { target: { value: "回踩接多" } });
  fireEvent.change(price, { target: { value: "8000" } });
  fireEvent.click(screen.getByRole("button", { name: "添加" }));
  expect(alerts.create).toHaveBeenCalledWith("SHFE.au2612", 8000, "回踩接多");
});

it("the create refusal shows inline under the form", () => {
  render(<PriceAlertsControl alerts={handle({ createError: "现价就是 7980，换个不同的价" })} symbol="SHFE.au2612" priceTick={2} lastPrice={7980} />);
  fireEvent.click(screen.getByRole("button", { name: "到价提醒（0 条生效）" }));
  expect(screen.getByRole("alert")).toHaveTextContent("现价就是 7980，换个不同的价");
});

it("rows split by contract: delete here, switch from 其他合约; a crossing is the 提醒中心's, not listed here", () => {
  const onSwitchContract = vi.fn();
  const alerts = handle({
    active: [
      alert({ id: "mine", note: "破八看多" }),
      alert({ id: "other", symbol: "DCE.m2701", name: "豆粕2701", direction: "below", price: "2980" }),
    ],
    triggered: [alert({ id: "fired", status: "triggered", triggeredAt: "2026-10-02T01:10:00Z", triggerPrice: "8000" })],
  });
  render(<PriceAlertsControl alerts={alerts} symbol="SHFE.au2612" name="沪金2612" priceTick={2} lastPrice={7980} onSwitchContract={onSwitchContract} />);
  fireEvent.click(screen.getByRole("button", { name: "到价提醒（2 条生效）" }));
  const mine = screen.getByText("沪金2612的生效提醒").closest("div")!;
  expect(mine.querySelector(".fdw-alert-line")!.textContent).toContain("涨到 8000");
  expect(within(mine).getByText("破八看多")).toBeVisible();
  expect(screen.queryByText("还没有本合约的提醒")).toBeNull();   // au2612 有提醒时不出现空态

  fireEvent.click(screen.getByRole("button", { name: "其他合约 1 条 ▸" }));
  fireEvent.click(screen.getByRole("button", { name: "豆粕2701 DCE.m2701" }));
  expect(onSwitchContract).toHaveBeenCalledWith("DCE.m2701");

  expect(screen.queryByText("已触发")).toBeNull();   // 2026-10-15: crossings are rows of the day in the 提醒中心
  fireEvent.click(screen.getAllByRole("button", { name: /^删除提醒：/ })[0]);
  expect(alerts.cancel).toHaveBeenCalledWith("mine");
});

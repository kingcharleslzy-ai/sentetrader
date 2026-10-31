import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MyReminders, reminderRows } from "./MyReminders";
import { resetUiSurfaces, uiSurface } from "../../uiSurfaces";
import type { PriceAlert, PriceAlertsHandle } from "../../hooks/usePriceAlerts";
import type { SavedStrategy } from "../../strategyApi";

/** 我的提醒 (改版 2026-10-31): one list — 到某个价 / 碰到我画的线 (price alerts) and 按条件 (只提醒 strategies, run only as 只提醒). */
const strategy = (id: string, extra: Partial<SavedStrategy> & { reminder?: boolean; library?: string } = {}) => ({
  strategyId: id, name: `提醒${id}`, symbol: extra.symbol ?? "*", version: 1, status: extra.status ?? "draft",
  definition: { schemaVersion: 2, ...(extra.library ? { libraryStrategyId: extra.library } : {}),
    plan: { name: id, reminderOnly: extra.reminder ?? true } } }) as unknown as SavedStrategy;

const alert = (id: string, over: Partial<PriceAlert> = {}): PriceAlert => ({ id, symbol: "SHFE.rb2701", name: "螺纹2701", direction: "above",
  price: "3040", note: "", status: "active", via: "ui", sessionId: null, createdAt: "", triggeredAt: null, triggerPrice: null, ...over });
const alerts = (active: PriceAlert[] = []): PriceAlertsHandle => ({ available: true, active, triggered: [], createError: "", notice: "",
  create: vi.fn(async () => true), cancel: vi.fn(), refresh: vi.fn() });

afterEach(() => { vi.unstubAllGlobals(); resetUiSurfaces(); });

test("只列只提醒的策略，带它运行中的实例", () => {
  const rows = reminderRows([strategy("a"), strategy("b"), strategy("s", { reminder: false })],
    [strategy("a-run", { library: "a", status: "monitoring", symbol: "SHFE.rb2701" }), strategy("b-old", { library: "b", status: "archived" })]);
  expect(rows).toEqual([{ id: "a", name: "提醒a", status: "monitoring", symbol: "SHFE.rb2701", runs: [{ runId: "a-run", status: "monitoring" }] },
    { id: "b", name: "提醒b", status: "idle", symbol: null, runs: [] }]);
});

test("到价、画线和按条件在同一张表；条件提醒只按只提醒开停，删除先暂停在跑的再删；新建提醒给出到某个价和按条件", async () => {
  const calls: Array<[string, RequestInit | undefined]> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push([String(url), init]);
    if (String(url).endsWith("/api/strategy-plans") && !init?.method) return new Response(JSON.stringify({
      items: [strategy("a"), strategy("b")], executions: [strategy("a-run", { library: "a", status: "monitoring", symbol: "SHFE.rb2701" })] }));
    if (String(url).endsWith("/validate")) return new Response(JSON.stringify({ strategyId: "b-run" }));
    return new Response("{}");
  }));
  const onNew = vi.fn();
  const handle = alerts([alert("p1", { note: "前高" }), alert("l1", { drawingId: "d1", price: "3010" })]);
  render(<MyReminders baseUrl="https://test" contract="SHFE.au2612" chartTimeframe={300} onNew={onNew} onEdit={vi.fn()}
    alerts={handle} alertContract={{ symbol: "SHFE.rb2701", priceTick: 1, lastPrice: 3020 }} />);
  await waitFor(() => expect(within(screen.getByRole("list")).getAllByRole("listitem")).toHaveLength(4));
  const [price, line, running, idle] = within(screen.getByRole("list")).getAllByRole("listitem");
  expect(price).toHaveTextContent("螺纹2701 · 价格到 3,040前高提醒中删除");
  expect(line).toHaveTextContent("螺纹2701 · 碰到我画的线提醒中删除");
  expect(running).toHaveTextContent("SHFE.rb2701 · 提醒a提醒中暂停编辑删除");
  expect(idle).toHaveTextContent("提醒b未开始开始编辑删除");
  fireEvent.click(within(line).getByRole("button", { name: "删除提醒：螺纹2701 碰到画的线 提醒" }));
  expect(handle.cancel).toHaveBeenCalledWith("l1");
  await waitFor(() => expect(uiSurface("myReminders")?.state).toMatchObject({ status: "ready", items: [{ id: "a", status: "monitoring" }, { id: "b", status: "idle" }] }));
  fireEvent.click(within(idle).getByRole("button", { name: "开始" }));
  await waitFor(() => expect(calls.some(([url]) => url.endsWith("/api/strategies/b-run/monitor/start"))).toBe(true));
  const start = calls.find(([url]) => url.endsWith("/api/strategies/b-run/monitor/start"))!;
  expect(JSON.parse(String(start[1]?.body))).toEqual({ signalAction: "alert" });
  expect(JSON.parse(String(calls.find(([url]) => url.endsWith("/api/strategy-plans/b/validate"))![1]?.body)).symbol).toBe("SHFE.au2612");
  await waitFor(() => expect(within(running).getByRole("button", { name: "删除提醒：提醒a" })).not.toBeDisabled());
  fireEvent.click(within(running).getByRole("button", { name: "删除提醒：提醒a" }));
  await waitFor(() => expect(calls.some(([url, init]) => url.endsWith("/api/strategy-plans/a") && init?.method === "DELETE")).toBe(true));
  const order = calls.map(([url, init]) => `${init?.method ?? "GET"} ${url.replace("https://test", "")}`);
  expect(order.indexOf("POST /api/strategies/a-run/monitor/pause")).toBeLessThan(order.indexOf("DELETE /api/strategy-plans/a"));
  // 新建提醒: 到某个价's form on the chart's contract, 按条件… opens the editor card in 提醒模式.
  fireEvent.click(screen.getByRole("button", { name: "新建提醒" }));
  expect(screen.getByRole("spinbutton", { name: "提醒价" })).toHaveValue(3020);
  fireEvent.click(screen.getByRole("button", { name: "按条件…" }));
  expect(onNew).toHaveBeenCalled();
  expect(screen.queryByRole("spinbutton", { name: "提醒价" })).toBeNull();
});

test("没有编辑卡时（访客的套餐、旧手机）只有到某个价，条件提醒只列出、开停和删除", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [], executions: [] }))));
  render(<MyReminders baseUrl="https://test" chartTimeframe={300} alerts={alerts()} />);
  expect(await screen.findByText("还没有提醒：点“新建提醒”，价格到了或条件满足时提醒你。")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "新建提醒" }));
  expect(screen.getByText("先在图表上选一个合约")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "按条件…" })).toBeNull();
});

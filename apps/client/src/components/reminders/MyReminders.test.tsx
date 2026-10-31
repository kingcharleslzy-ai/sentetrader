import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MyReminders, reminderRows } from "./MyReminders";
import { resetUiSurfaces, uiSurface } from "../../uiSurfaces";
import type { SavedStrategy } from "../../strategyApi";

/** 条件提醒 (第 6 批 2026-10-25): 我的提醒 lists the 只提醒 strategies only, and runs them only as 只提醒. */
const strategy = (id: string, extra: Partial<SavedStrategy> & { reminder?: boolean; library?: string } = {}) => ({
  strategyId: id, name: `提醒${id}`, symbol: extra.symbol ?? "*", version: 1, status: extra.status ?? "draft",
  definition: { schemaVersion: 2, ...(extra.library ? { libraryStrategyId: extra.library } : {}),
    plan: { name: id, reminderOnly: extra.reminder ?? true } } }) as unknown as SavedStrategy;

afterEach(() => { vi.unstubAllGlobals(); resetUiSurfaces(); });

test("只列只提醒的策略，带它运行中的实例", () => {
  const rows = reminderRows([strategy("a"), strategy("b"), strategy("s", { reminder: false })],
    [strategy("a-run", { library: "a", status: "monitoring", symbol: "SHFE.rb2701" }), strategy("b-old", { library: "b", status: "archived" })]);
  expect(rows).toEqual([{ id: "a", name: "提醒a", status: "monitoring", symbol: "SHFE.rb2701", runs: [{ runId: "a-run", status: "monitoring" }] },
    { id: "b", name: "提醒b", status: "idle", symbol: null, runs: [] }]);
});

test("暂停、开始都只按只提醒；删除先暂停在跑的再删；快照列出每条", async () => {
  const calls: Array<[string, RequestInit | undefined]> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push([String(url), init]);
    if (String(url).endsWith("/api/strategy-plans") && !init?.method) return new Response(JSON.stringify({
      items: [strategy("a"), strategy("b")], executions: [strategy("a-run", { library: "a", status: "monitoring", symbol: "SHFE.rb2701" })] }));
    if (String(url).endsWith("/validate")) return new Response(JSON.stringify({ strategyId: "b-run" }));
    return new Response("{}");
  }));
  const onNew = vi.fn();
  render(<MyReminders baseUrl="https://test" contract="SHFE.au2612" chartTimeframe={300} onNew={onNew} onEdit={vi.fn()} />);
  const list = await screen.findByRole("list");
  const [running, idle] = within(list).getAllByRole("listitem");
  expect(running).toHaveTextContent("提醒a提醒中 SHFE.rb2701暂停编辑删除");
  expect(idle).toHaveTextContent("提醒b未开始开始编辑删除");
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
  fireEvent.click(screen.getByRole("button", { name: "新建提醒" }));
  expect(onNew).toHaveBeenCalled();
});

test("手机上没有新建和编辑，只列出、开停和删除", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [], executions: [] }))));
  render(<MyReminders baseUrl="https://test" chartTimeframe={300} />);
  expect(await screen.findByText("还没有条件提醒。")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "新建提醒" })).toBeNull();
});

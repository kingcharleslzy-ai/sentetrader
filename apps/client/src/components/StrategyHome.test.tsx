import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import { StrategyWorkbench } from "./StrategyWorkbench";
import { resetUiSurfaces, uiSurface } from "../uiSurfaces";
import { resetStrategyBacktests } from "../strategyBacktests";
import { resetStrategyEditor } from "../strategyEditorStore";
import { resetCompare } from "../strategyCompare";
import type { StrategyPlan } from "../strategyApi";

/**
 * 策略页改版第八批 (2026-10-08): the main line on 我的策略 — 结果页 → 编辑 (the 策略编辑卡) → 存为草稿 → the
 * new version selected, backtested, its 结果页 open — and the 结果页's 试几组参数比一比 table.
 * Protects: an edit saves a new version of the same strategy (its scope and id as the base) with only the
 * changed field different, and the comparison marks the strategy's current group.
 */
const request = vi.hoisted(() => vi.fn());
vi.mock("../api", () => ({ requestJson: request, getStrategyMonitor: vi.fn(), stopStrategyMonitoring: vi.fn() }));

const plan = (questions: boolean): StrategyPlan => ({ name: "前高突破追单", description: "突破前高就追", direction: "both", timeframeSeconds: 900, maxPosition: 1,
  maxLoss: 1000, stopLossTicks: 20, takeProfitTicks: null, cooldownBars: 0, maxOrdersPerDay: 20, questions: questions ? ["多高算前高？"] : [],
  clarifications: questions ? [{ id: "high", question: "多高算前高？", recommended: 0, options: [{ label: "前 20 根", description: "近一周" }] }] : [],
  rules: { entryLong: null, exitLong: null, entryShort: null, exitShort: null } } as StrategyPlan);
const result = { backtestId: "bt-1", kind: "rule", strategyId: "s1", strategyName: "前高突破追单", symbol: "SHFE.rb2701", timeframeSeconds: 900,
  summary: { netProfit: 12040, returnPercent: 12, maxDrawdown: 3100, maxDrawdownPercent: 3, winRate: 55, payoffRatio: 1.2, trades: 128, averageBarsHeld: 3, fees: 10 },
  exitReasons: [], equityCurve: [], trades: [], assumptions: [],
  coverage: { firstBarTime: 0, lastBarTime: 0, bars: 10, tradingDays: 120, firstTradingDay: "2026-04-08", lastTradingDay: "2026-10-07", requestedTradingDays: 120, limited: false } };

let items: Record<string, unknown>[] = [];
beforeEach(() => {
  localStorage.clear(); vi.clearAllMocks(); resetUiSurfaces(); resetStrategyBacktests(); resetStrategyEditor(); resetCompare();
  items = [{ strategyId: "s0", name: "前高突破追单", symbol: "*", version: 1, status: "draft", planSummary: "收盘高于前 20 根最高价就开多。",
    definition: { schemaVersion: 2, plan: plan(false), instrumentScope: { mode: "selected", symbols: ["SHFE.rb"] } } }];
  request.mockImplementation(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    if (url.endsWith("/api/strategy-plans") && init?.method === "POST") {
      const saved = { strategyId: "s1", name: body.plan.name, symbol: "*", version: 2, status: "draft", planSummary: "收盘高于前 20 根最高价就开多。",
        definition: { schemaVersion: 2, plan: body.plan, instrumentScope: body.instrumentScope } };
      items = [...items, saved];
      return saved;
    }
    if (url.endsWith("/api/strategy-plans")) return { items, executions: [] };
    if (url.endsWith("/api/strategy-backtests")) return new Promise(() => undefined);
    if (url.endsWith("/backtest")) return result;
    if (url.includes("strategy-backtests?strategyId=")) return { items: [] };
    if (url.endsWith("/compare") && init?.method === "POST") return { labId: "lab-1", strategyId: "s1", symbol: "SHFE.rb2701", status: "done", source: "server",
      paramLabel: "追进的幅度", paramHelp: null, currentVariant: "v2", best: "v2", line: "后 2 个月单独检验：2 成那组仍赚 2,100，比较稳。",
      rows: [{ variantId: "v1", label: "1.5 成", current: false, summary: { netProfit: 8300, maxDrawdown: 4200, winRate: 51, trades: 90 } },
        { variantId: "v2", label: "2 成（当前）", current: true, summary: { netProfit: 12040, maxDrawdown: 3100, winRate: 55, trades: 128 } },
        { variantId: "v3", label: "3 成", current: false, summary: { netProfit: 6100, maxDrawdown: 2400, winRate: 58, trades: 60 } }] };
    if (url.endsWith("/compare")) return { labId: null, status: "none", rows: [] };
    if (url.endsWith("/api/strategy-templates")) return { categories: [], items: [] };
    return { conditions: [] };
  });
});

const mount = () => render(<StrategyWorkbench rootUrl="http://main" baseUrl="http://main" symbol="SHFE.rb2701" chartTimeframe={900} replayActive={false}
  workspaceScope="home" onAskAi={vi.fn()} backtestBlocked={null}/>);
const posted = () => request.mock.calls.filter(([, init]) => init?.method === "POST").map(([url]) => String(url).replace("http://main", ""));

test("结果页 → 编辑 → 改止损 → 存为草稿：同一条策略的新版本（只改止损），选中、自动回测、打开结果页；比一比的表标出当前那组", async () => {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "前高突破追单 · 没上线" }));
  fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
  const card = await screen.findByTestId("strategy-editor");
  expect(uiSurface("strategyEditor")!.state).toMatchObject({ open: true, mode: "edit", strategyId: "s0", fields: { stop: { mode: "ticks", value: 20 } } });
  fireEvent.change(within(card).getByRole("spinbutton", { name: "止损" }), { target: { value: "25" } });
  // The plan has no entry condition (a test fixture), so add one before it can be saved.
  fireEvent.click(within(card).getByRole("button", { name: "加一条：突破前高" }));
  fireEvent.click(within(card).getByRole("button", { name: "存为草稿" }));
  expect(await screen.findByText("过去 6 个月按这个做")).toBeVisible();
  const save = request.mock.calls.find(([url, init]) => String(url).endsWith("/api/strategy-plans") && init?.method === "POST")!;
  const body = JSON.parse(String(save[1].body));
  expect(body).toMatchObject({ baseStrategyId: "s0", instrumentScope: { mode: "selected", symbols: ["SHFE.rb"] } });
  expect(body).not.toHaveProperty("templateId");
  const { rules, stopLossTicks, description, ...rest } = body.plan;
  const { rules: oldRules, stopLossTicks: oldStop, description: oldDescription, ...oldRest } = plan(false);
  expect(rest).toEqual(JSON.parse(JSON.stringify(oldRest)));
  expect([stopLossTicks, oldStop, rules.entryLong.conditions[0].label, oldRules.entryShort]).toEqual([25, 20, "收盘价 上穿 前20根最高", null]);
  expect([description, oldDescription]).toEqual(["突破前高就追", "突破前高就追"]);
  expect(posted()).toEqual(["/api/strategy-plans", "/api/strategies/s1/backtest"]);
  // The closed card keeps the strategy it saved: `strategyEditor {save}` is judged by it.
  expect(uiSurface("strategyEditor")!.state).toEqual({ open: false, savedStrategyId: "s1" });
  expect(screen.getByLabelText("回测结果")).toHaveTextContent("一共赚了+12,040");

  // 试几组参数比一比: three groups, the current one marked, the held-out line below.
  fireEvent.click(screen.getByRole("button", { name: "比一比" }));
  const table = await screen.findByRole("table");
  expect(within(table).getAllByRole("row").map(row => row.textContent)).toEqual(["追进的幅度赚最多亏胜率",
    "1.5 成+8,300-4,20051%", "2 成（当前）+1.2 万-3,10055%", "3 成+6,100-2,40058%"]);
  expect(within(table).getByText("2 成（当前）").closest("tr")).toHaveClass("best");
  expect(screen.getByTestId("strategy-compare-line")).toHaveTextContent("后 2 个月单独检验：2 成那组仍赚 2,100，比较稳。");
  expect(uiSurface("strategyLab")!.state).toMatchObject({ labId: "lab-1", status: "done", where: "server",
    rows: [{ label: "1.5 成", current: false }, { label: "2 成（当前）", current: true, netProfit: 12040 }, { label: "3 成", current: false }] });
});

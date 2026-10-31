import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import { StrategyWorkbench } from "./StrategyWorkbench";
import { resetUiSurfaces, uiSurface } from "../uiSurfaces";
import { resetStrategyBacktests } from "../strategyBacktests";
import { resetStrategyEditor } from "../strategyEditorStore";
import { resetStrategyTemplates } from "../strategyTemplates";

/**
 * 我的策略's empty state (2026-10-08). Protects: a new user with no strategy sees how to start — 按我的习惯 stays
 * unclickable until there are enough trades and says how many are missing; a common template's 用这个 opens the
 * 策略编辑卡 from it; the preview figures are computed once per contract; a user with strategies sees the list only.
 */
const request = vi.hoisted(() => vi.fn());
vi.mock("../api", () => ({ requestJson: request, getStrategyMonitor: vi.fn(), stopStrategyMonitoring: vi.fn() }));

const template = (id: string, name: string) => ({ id, name, category: "trend", categoryLabel: "趋势", scenario: "趋势行情", plainSummary: `${name}的一句话`,
  source: { label: "", url: "" }, recommendedTimeframe: 300, approximations: [], params: [], constraints: [] });
const catalog = { categories: [], items: [template("double_ma", "双均线"), template("donchian_turtle", "唐奇安突破"), template("rsi_reversal", "RSI 迟滞反转"),
  template("dual_thrust", "Dual Thrust"), template("r_breaker", "R-Breaker")] };
const plan = { name: "均线", description: "", direction: "long", timeframeSeconds: 300, maxPosition: 1, maxLoss: null, stopLossTicks: 20, takeProfitTicks: null,
  cooldownBars: 1, maxOrdersPerDay: 20, closeBeforeEndScope: "day", allowNightEntries: true, questions: [], rules: { entryLong: null, exitLong: null, entryShort: null, exitShort: null } };
let items: unknown[] = [];
beforeEach(() => {
  vi.clearAllMocks(); resetUiSurfaces(); resetStrategyBacktests(); resetStrategyEditor(); resetStrategyTemplates();
  items = [];
  request.mockImplementation(async (url: string) => {
    if (url.endsWith("/api/strategy-plans")) return { items, executions: [] };
    if (url.endsWith("/api/strategy-plans/prefill")) return { eligible: false, reason: "近 20 个交易日只有 2 笔", sample: { trades: 2, minTrades: 5 }, fields: {}, trades: [] };
    if (url.endsWith("/api/strategy-templates")) return catalog;
    if (url.endsWith("/api/strategy-templates/double_ma")) return { ...catalog.items[0], plan: { ...plan, name: "双均线" } };
    if (url.endsWith("/preview")) return url.includes("rsi_reversal") ? Promise.reject(new Error("500")) : { backtest: { summary: { trades: 23, winRate: 43.4 } } };
    if (url.endsWith("/api/strategy-backtests")) return new Promise(() => undefined);
    return { conditions: [] };
  });
});
const mount = () => render(<StrategyWorkbench rootUrl="http://main" baseUrl="http://main" symbol="SHFE.rb2701" chartTimeframe={300} replayActive={false} workspaceScope="starts"/>);

test("空列表：一行标题（说明在“?”里）、三种开始方式（成交不够时“按我的习惯”不可点并写还差几笔）、四个常用模板带近 60 天回测，“用这个”从模板打开编辑卡", async () => {
  mount();
  const starts = await screen.findByTestId("strategy-starts");
  expect(within(starts).getByRole("heading", { name: "还没有策略，选一种开始" })).toBeVisible();
  // What a strategy is and the four steps are behind the title's "?", not on the screen.
  expect(within(starts).queryByText(/策略就是把你的做法写成规则/)).toBeNull();
  fireEvent.click(within(starts).getByRole("button", { name: "策略是什么的说明" }));
  expect(screen.getByRole("tooltip")).toHaveTextContent("策略就是把你的做法写成规则");
  const habits = within(starts).getByRole("button", { name: "按我的习惯" });
  await waitFor(() => expect(habits).toHaveTextContent("已有 2 笔，还差 3 笔"));
  expect(habits).toBeDisabled();
  const rows = within(starts).getByRole("list", { name: "常用模板" });
  await waitFor(() => expect(rows).toHaveTextContent("双均线双均线的一句话23 笔 · 胜率 43%"));
  expect(within(rows).queryByText("R-Breaker")).toBeNull();
  // A failed preview shows only the sentence; each template is computed once for this contract.
  await waitFor(() => expect(within(rows).getByText("RSI 迟滞反转").closest("li")).not.toHaveTextContent("计算中"));
  expect(within(rows).getByText("RSI 迟滞反转").closest("li")).not.toHaveTextContent("笔 ·");
  const previews = request.mock.calls.filter(([url]) => String(url).endsWith("/preview"));
  expect(previews).toHaveLength(4);
  expect(JSON.parse(String(previews[0][1].body))).toEqual({ symbol: "SHFE.rb2701", tradingDays: 60 });
  fireEvent.click(within(rows).getByRole("button", { name: "用这个：双均线" }));
  await waitFor(() => expect(uiSurface("strategyEditor")!.state).toMatchObject({ open: true, mode: "new", start: "template:double_ma", name: "双均线" }));
});

test("有策略时只列策略，不显示空状态", async () => {
  items = [{ strategyId: "s1", name: "均线", symbol: "*", version: 1, status: "draft", definition: { schemaVersion: 2, plan, instrumentScope: { mode: "any", symbols: [] } } }];
  mount();
  expect((await screen.findAllByRole("button", { name: /^均线/ }))[0]).toBeVisible();
  expect(screen.queryByTestId("strategy-starts")).toBeNull();
  expect(screen.getByRole("combobox", { name: "从模板开始" })).toBeVisible();
});

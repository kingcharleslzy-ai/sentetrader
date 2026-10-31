import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import { StrategyDesigner, StrategyWorkbench } from "./StrategyWorkbench";
import { resetUiSurfaces, uiSurface } from "../uiSurfaces";
import { resetStrategyBacktests } from "../strategyBacktests";
import { resetStrategyTemplates } from "../strategyTemplates";
import { resetStrategyEditor } from "../strategyEditorStore";
import type { SavedStrategy, StrategyPlan } from "../strategyApi";
import type { StrategyBacktestResult } from "@ai-trader/ui-events";

const request = vi.hoisted(() => vi.fn());
vi.mock("../api", () => ({ requestJson: request, getStrategyMonitor: vi.fn(), stopStrategyMonitoring: vi.fn() }));

const param = (key: string, label: string, value: number, min: number, max: number, step: number, unit = "") =>
  ({ key, label, default: value, min, max, step, unit, integer: Number.isInteger(step), targets: [],
    help: { what: `${label}是什么的说明`, up: `${label}调大的后果`, down: `${label}调小的后果` } });
const DUAL_PLAIN = "每天开盘后，价格从开盘价往上或往下冲出前几天的正常波动范围就顺势追进去；反向冲出就先平仓，收盘前一定全部平掉。";
const catalog = {
  categories: [{ id: "trend_breakout", label: "趋势突破" }, { id: "ma_trend", label: "均线趋势" }, { id: "channel_reversion", label: "通道回归" }, { id: "reversal", label: "反转" }, { id: "intraday_range", label: "日内区间" }],
  items: [
    { id: "donchian_turtle", name: "唐奇安突破（海龟简化版）", category: "trend_breakout", categoryLabel: "趋势突破", scenario: "日线或小时线，吃中长趋势",
      source: { label: "TqSdk turtle.py", url: "https://github.com/shinnytech/tqsdk-python" }, recommendedTimeframe: 86400, approximations: ["不加仓"],
      params: [param("entryN", "突破通道", 20, 5, 120, 1, "根")], constraints: [] },
    { id: "double_ma", name: "双均线（基准）", category: "ma_trend", categoryLabel: "均线趋势", scenario: "所有策略的对照基准",
      source: { label: "vn.py DoubleMaStrategy", url: "https://github.com/vnpy/vnpy_ctastrategy" }, recommendedTimeframe: 60, approximations: [],
      params: [param("fast", "快线周期", 10, 2, 100, 1, "根"), param("slow", "慢线周期", 20, 3, 200, 1, "根"),
        { ...param("maxLoss", "当日亏损上限", 0, 100, 1000000, 100, "元"), default: null, optional: true }],
      constraints: [{ less: ["fast", "slow"], message: "快线周期须小于慢线周期" }] },
    { id: "rsi_reversal", name: "RSI 迟滞反转", category: "reversal", categoryLabel: "反转", scenario: "高抛低吸：RSI 超卖做多、超买做空，回到 50 离场",
      source: { label: "QuantConnect LEAN RsiAlphaModel", url: "https://github.com/QuantConnect/Lean" }, recommendedTimeframe: 86400, approximations: ["原版没有离场规则"],
      params: [param("rsiPeriod", "RSI 周期", 14, 2, 60, 1, "根")], constraints: [] },
    { id: "dual_thrust", name: "Dual Thrust", category: "intraday_range", categoryLabel: "日内区间", scenario: "日内突破，每个方向每天最多一次",
      plainSummary: DUAL_PLAIN, level: "进阶", holding: "日内",
      source: { label: "TqSdk dualthrust.py", url: "https://github.com/shinnytech/tqsdk-python" }, recommendedTimeframe: 60, approximations: ["按收盘确认"],
      params: [param("n", "参考天数（N 日振幅）", 5, 1, 30, 1, "日"), param("k1", "向上突破幅度（K1）", 0.2, 0.05, 2, 0.05, "×振幅")], constraints: [] },
  ],
};
const condition = (label: string, timeframe: number, indicator = "close") =>
  ({ label, left: { indicator, period: 5, timeframe, value: 0, barsAgo: 0 }, operator: "gt" as const, right: { indicator: "ma" as const, period: 20, timeframe, value: 0, barsAgo: 0 }, withinBars: 1, consecutive: 1, minCount: 1 });
const dualPlan = (k1: number): StrategyPlan => ({ name: `Dual Thrust N5 K${k1}/0.2`, description: "今开 ± K×Range", direction: "both", timeframeSeconds: 60,
  maxPosition: 1, maxLoss: 5000, stopLossTicks: 100, takeProfitTicks: null, cooldownBars: 0, maxOrdersPerDay: 20, questions: [], clarifications: [],
  closeBeforeEndMinutes: 5, maxEntriesPerDirection: 1,
  rules: { entryLong: { join: "all", sequence: false, conditions: [condition(`收盘突破今开 + ${k1}×5日振幅`, 60)] }, exitLong: null,
    entryShort: { join: "all", sequence: false, conditions: [condition("收盘跌破今开 − 0.2×5日振幅", 60)] }, exitShort: null } } as StrategyPlan);
const backtest = (net: number): StrategyBacktestResult => ({ backtestId: `bt-${net}`, kind: "rule", strategyName: "Dual Thrust", symbol: "SHFE.au2612", timeframeSeconds: 60,
  summary: { netProfit: net, returnPercent: 0, maxDrawdown: 50, maxDrawdownPercent: 0.01, winRate: 50, payoffRatio: 1.2, trades: 4, averageBarsHeld: 3, fees: 0 },
  exitReasons: [], equityCurve: [], trades: [], assumptions: ["按收盘确认、下一根开盘成交。"],
  coverage: { firstBarTime: 0, lastBarTime: 0, bars: 900, tradingDays: 3, firstTradingDay: "2026-09-22", lastTradingDay: "2026-09-24", requestedTradingDays: null, limited: false },
  feeEstimate: { source: "none", warning: true, text: "未扣手续费，结果偏乐观：拿不到这个合约的手续费参考。" } });

let items: SavedStrategy[] = [];
/** A test may hold the next instantiate answer back (#520-D: a late save). */
let holdInstantiate: Promise<void> | null = null;
function routes() {
  request.mockImplementation(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    if (url.includes("/api/strategy-backtests?strategyId=")) return { items: [] };
    if (url.endsWith("/compare") && !init?.method) return { status: "none", labId: null, rows: [] };
    if (url.endsWith("/backtest")) return backtest(1000);
    if (url.endsWith("/api/strategy-plans") && init?.method === "POST") {
      const item: SavedStrategy = { strategyId: `ed-${items.length + 1}`, name: body.plan.name, symbol: "*", version: 1, status: "draft",
        definition: { schemaVersion: 2, plan: body.plan, instrumentScope: body.instrumentScope } };
      items = [item, ...items];
      return item;
    }
    if (url.endsWith("/api/strategy-plans")) return { items, executions: [] };
    if (url.endsWith("/api/strategy-plans/prefill")) return { eligible: false, reason: "成交不够", fields: {}, trades: [] };
    if (url.endsWith("/api/strategy-templates")) return catalog;
    if (url.endsWith("/api/strategy-templates/dual_thrust")) return { ...catalog.items[3], plan: dualPlan(0.2) };
    if (url.endsWith("/api/strategy-backtests") && init?.method === "POST") return new Promise(() => undefined);
    if (url.endsWith("/preview")) return { templateId: "dual_thrust", params: body.params, plan: dualPlan(body.params.k1), backtest: backtest(body.params.k1 * 1000) };
    if (url.endsWith("/instantiate")) {
      if (holdInstantiate) await holdInstantiate;
      const k1 = body.params?.k1 ?? 0.2;
      // The Runtime names it “模板名 · 合约 · 周期”; 恢复模板默认 (baseStrategyId) is the next version of that strategy.
      const base = items.find(item => item.strategyId === body.baseStrategyId);
      const name = base?.name ?? body.name ?? `Dual Thrust · ${body.chartSymbol} · 1分钟 K${k1}`;
      const version = items.filter(item => item.name === name).length + 1;
      const item: SavedStrategy = { strategyId: `tpl-${items.length + 1}`, name, symbol: "*", version, status: "draft", planSummary: DUAL_PLAIN,
        definition: { schemaVersion: 2, plan: dualPlan(k1), instrumentScope: body.instrumentScope ?? { mode: "any", symbols: [] },
          templateSource: { templateId: "dual_thrust", templateName: "Dual Thrust", params: { n: 5, k1 }, defaults: k1 === 0.2 } } };
      items = [item, ...items];
      return item;
    }
    return { conditions: [] };
  });
}
const posts = () => request.mock.calls.filter(([, init]) => init?.method === "POST").map(([url, init]) => [url, JSON.parse(String(init.body))]);

beforeEach(() => { localStorage.clear(); vi.clearAllMocks(); resetUiSurfaces(); resetStrategyBacktests(); resetStrategyTemplates(); resetStrategyEditor(); items = []; holdInstantiate = null; routes(); });

function mount() {
  return render(<StrategyWorkbench rootUrl="http://main" baseUrl="http://runtime" symbol="SHFE.au2612" chartTimeframe={60} replayActive={false}
    workspaceScope="templates" onAskAi={vi.fn()} backtestBlocked={null}/>);
}

test("编辑卡 · 从模板：模板的默认计划填进表单（执行周期取模板的），存为草稿时带 templateId，打开它的结果页并回测", async () => {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "新建策略" }));
  const card = await screen.findByTestId("strategy-editor");
  // Spread templates (a contract parameter) cannot be shown by the card and are not offered.
  await waitFor(() => expect(within(card).getAllByRole("option").map(option => option.textContent)).toEqual(
    ["从模板 ▾", "唐奇安突破（海龟简化版）", "双均线（基准）", "RSI 迟滞反转", "Dual Thrust"]));
  fireEvent.change(within(card).getByRole("combobox", { name: "从模板" }), { target: { value: "dual_thrust" } });
  await waitFor(() => expect(uiSurface("strategyEditor")!.state).toMatchObject({ start: "template:dual_thrust", name: "Dual Thrust N5 K0.2/0.2",
    fields: { timeframe: 60, stop: { mode: "ticks", value: 100 }, entryLong: { conditions: ["收盘突破今开 + 0.2×5日振幅"] } } }));
  // The template’s 每个方向每天最多一次, 当日亏损上限 and 平仓后不等待 are ④'s 更多 since 第二版 (editable, kept on save as they are);
  // its 今开 + 0.2×5日振幅 (系数偏移) stays a read-only row.
  expect(uiSurface("strategyEditor")!.state).toMatchObject({ advanced: [], fields: { sizingMore: { maxEntriesPerDirection: 1, maxLoss: 5000, cooldownBars: 0 } } });
  expect(within(card).queryByText(/高级规则（卡片里不能改）/)).toBeNull();
  fireEvent.click(within(card).getByRole("button", { name: "存为草稿" }));
  await screen.findByText("一共赚了");
  const save = posts().find(([url]) => url === "http://main/api/strategy-plans")!;
  expect(save[1]).toMatchObject({ templateId: "dual_thrust", instrumentScope: { mode: "any", symbols: [] },
    plan: { name: "Dual Thrust N5 K0.2/0.2", maxEntriesPerDirection: 1, maxLoss: 5000, cooldownBars: 0, timeframeSeconds: 60 } });
  expect(save[1]).not.toHaveProperty("baseStrategyId");
});

test("结果页：来自模板的策略在“更多”里列出模板与参数，恢复模板默认另存为新版本并重新回测", async () => {
  items = [{ strategyId: "tpl-1", name: "Dual Thrust · SHFE.au2612 · 1分钟 K0.35", symbol: "*", version: 1, status: "draft", planSummary: DUAL_PLAIN,
    definition: { schemaVersion: 2, plan: dualPlan(0.35), instrumentScope: { mode: "any", symbols: [] },
      templateSource: { templateId: "dual_thrust", templateName: "Dual Thrust", params: { n: 5, k1: 0.35 }, defaults: false } } }];
  mount();
  fireEvent.click(await screen.findByRole("button", { name: /^Dual Thrust · SHFE.au2612 · 1分钟 K0.35 · / }));
  await waitFor(() => expect(uiSurface("strategySummary")!.state).toBe(DUAL_PLAIN));
  act(() => uiSurface("strategyDetails")!.apply!({ section: "rules", open: true }));
  expect(screen.getByText("来自模板：Dual Thrust")).toBeVisible();
  const list = await waitFor(() => { const found = document.querySelector<HTMLElement>("dl.strategy-template-values"); expect(found).not.toBeNull(); return found!; });
  expect(list).toHaveTextContent("向上突破幅度（K1）?0.35 ×振幅");
  await waitFor(() => expect(uiSurface("strategyTemplate")!.state).toMatchObject({ detail: { templateId: "dual_thrust", defaults: false } }));
  expect(() => uiSurface("strategyTemplate")!.apply!({ open: true })).toThrow(/并入策略编辑卡/);
  act(() => uiSurface("strategyTemplate")!.apply!({ restore: true }));
  await screen.findByText("模板默认参数");
  expect(posts().some(([url, body]) => url === "http://main/api/strategy-templates/dual_thrust/instantiate"
    && JSON.stringify(body) === JSON.stringify({ baseStrategyId: "tpl-1", instrumentScope: { mode: "any", symbols: [] } }))).toBe(true);
  // The restored version is selected and backtested again.
  await waitFor(() => expect(posts().at(-1)![0]).toBe("http://main/api/strategies/tpl-2/backtest"));
});

test("填表单：没设当日亏损上限时常用里写“不设”，输入框留空，生成时带 null（不补默认值）", async () => {
  const unset = { ...dualPlan(0.2), maxLoss: null, questions: ["止损多少跳？"] } as StrategyPlan;
  request.mockImplementation(async (_url: string, init?: RequestInit) => init?.method === "POST"
    ? { id: JSON.parse(String(init.body)).id, status: "running", stage: "等待模型" } : { id: "x", status: "running" });
  render(<StrategyDesigner rootUrl="http://main" symbol="SHFE.au2612" workspaceScope="designer" draftKey="cap" initialPlan={unset} onSaved={vi.fn()}/>);
  expect(await screen.findByRole("region", { name: "常用" })).toHaveTextContent("当日亏损上限不设");
  const cap = screen.getByRole("spinbutton", { name: /当日亏损上限/ });
  expect(cap).toHaveValue(null);
  fireEvent.click(screen.getByRole("button", { name: "生成可选建议" }));
  await waitFor(() => expect(posts()).toHaveLength(1));
  expect(posts()[0][1].constraints.maxLoss).toBeNull();
});

test("详情：大周期条件与波动 / 跳空门槛是过滤（虚线），时段与夜盘在开仓限制里", async () => {
  const plan = { ...dualPlan(0.2), name: "布林通道", timeframeSeconds: 900, entryWindows: [{ start: "09:05", end: "14:30" }], allowNightEntries: false,
    rules: { entryLong: { join: "all", sequence: false, conditions: [condition("收盘突破布林上轨", 900), condition("1 小时收盘在 MA20 上方", 3600), condition("ATR 放大", 900, "atr")] },
      exitLong: null, entryShort: null, exitShort: null }, direction: "long" } as StrategyPlan;
  items = [{ strategyId: "boll", name: "布林通道", symbol: "*", version: 1, status: "draft", definition: { schemaVersion: 2, plan, instrumentScope: { mode: "any", symbols: [] } } }];
  mount();
  fireEvent.click(await screen.findByRole("button", { name: /^布林通道 · / }));
  act(() => uiSurface("strategyDetails")!.apply!({ section: "rules", open: true }));   // 完整规则 sit under 更多
  const entry = screen.getByRole("region", { name: "什么时候开仓" });
  // One line per condition: 周期 · 指标 · 动作 · 对象; the user's wording stays on hover.
  expect(within(entry).getByTitle("收盘突破布林上轨")).not.toHaveAttribute("data-kind");
  expect(within(entry).getByTitle("收盘突破布林上轨")).toHaveTextContent("15分收盘价 高于 MA20");
  expect(within(entry).getByTitle("1 小时收盘在 MA20 上方")).toHaveAttribute("data-kind", "filter");
  expect(within(entry).getByTitle("1 小时收盘在 MA20 上方")).toHaveTextContent("1时收盘价 高于 MA20");
  expect(within(entry).getByTitle("ATR 放大")).toHaveAttribute("data-kind", "filter");
  expect(within(entry).getByText("09:05–14:30")).toBeVisible();
  expect(within(entry).getByText("夜盘不开新仓，出场照常")).toBeVisible();
  // 只做多: one side, no 做空 column.
  expect(within(entry).getByRole("heading", { name: /做多/ })).toBeVisible();
  expect(within(entry).queryByRole("heading", { name: /做空/ })).toBeNull();
  expect(screen.queryByRole("button", { name: "恢复模板默认" })).toBeNull();
});

test("外盘策略（外盘三期）的金额按美元写：当日亏损上限、单笔预算都标美元", async () => {
  const plan = { ...dualPlan(0.2), maxLoss: 500, perTradeRisk: 200 } as StrategyPlan;
  render(<StrategyDesigner rootUrl="http://main" symbol="KQD.m@COMEX.GC" workspaceScope="designer" draftKey="foreign" initialPlan={plan} onSaved={vi.fn()}/>);
  const common = await screen.findByRole("region", { name: "常用" });
  expect(common).toHaveTextContent("当日亏损上限500 美元");
  expect(common).toHaveTextContent("单笔预算 200 美元");
  expect(screen.getByLabelText("当日亏损上限 · 美元")).toHaveValue(500);
});

test("填表单：常用积木直接列出，高级积木收在“高级”折叠；策略用了高级积木时折叠自动展开", async () => {
  const plain = { ...dualPlan(0.2), maxEntriesPerDirection: null, closeBeforeEndMinutes: 5 } as StrategyPlan;
  const { unmount } = render(<StrategyDesigner rootUrl="http://main" symbol="SHFE.au2612" workspaceScope="designer" draftKey="plain" initialPlan={plain} onSaved={vi.fn()}/>);
  const common = await screen.findByRole("region", { name: "常用" });
  expect(common).toHaveTextContent("方向多空双向");
  expect(common).toHaveTextContent("开多收盘价 高于 MA20");
  expect(common).toHaveTextContent("收盘前平仓当日最后一个时段收盘前 5 分钟平仓");
  expect(common).toHaveTextContent("最多持仓1 手");
  const fold = screen.getByText(/^高级/).closest("details")!;
  expect(fold.open).toBe(false);
  // 第六批: the fold always carries the two editable blocks (unchecked here), and says the rest is unused.
  expect(within(fold).getByLabelText("浮盈时顺势加仓")).not.toBeChecked();
  expect(within(fold).getByRole("button", { name: "加一档" })).toBeEnabled();
  expect(fold).toHaveTextContent("其余高级积木（复位、连续 N 根、升降、持仓量、枢轴、分段止盈、保本、开仓时段、按 ATR 定手数等）没有用到。");
  expect(uiSurface("disclosure:strategy-advanced")!.state).toBe(false);
  unmount();
  resetUiSurfaces();

  const advanced = { ...plain, maxEntriesPerDirection: 1, breakeven: { triggerTicks: 20, offsetTicks: 0 },
    rules: { ...plain.rules, entryLong: { join: "all", sequence: false, conditions: [condition("收盘突破", 60),
      { ...condition("CCI 连续走强", 60, "cci"), consecutive: 3 }], rearm: [condition("回落复位", 60, "rsi")] } } } as StrategyPlan;
  render(<StrategyDesigner rootUrl="http://main" symbol="SHFE.au2612" workspaceScope="designer" draftKey="advanced" initialPlan={advanced} onSaved={vi.fn()}/>);
  const open = (await screen.findByText(/^高级 · 已用 \d+ 项$/)).closest("details")!;
  expect(open.open).toBe(true);
  expect(open).toHaveTextContent("开多 · 还须满足CCI5 高于 MA20（连续 3 根）");
  expect(open).toHaveTextContent("保本浮盈达 20 跳 后止损移到开仓价");
  expect(open).toHaveTextContent("每方向开仓每个交易日每个方向最多 1 次");
  expect(open).toHaveTextContent("复位开多后须等「RSI5 高于 MA20」成立一次才再开多");
  expect(screen.getByRole("region", { name: "常用" })).toHaveTextContent("开多收盘价 高于 MA20");
  expect(uiSurface("disclosure:strategy-advanced")!.state).toBe(true);
  act(() => uiSurface("disclosure:strategy-advanced")!.apply!(false));
  expect(open.open).toBe(false);
});

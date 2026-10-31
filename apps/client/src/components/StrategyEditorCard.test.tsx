import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import { StrategyWorkbench } from "./StrategyWorkbench";
import { disclosureStates, resetUiSurfaces, uiSurface } from "../uiSurfaces";
import { matches } from "../hooks/useUiControl";
import { resetStrategyBacktests } from "../strategyBacktests";
import { BACKTEST_DEBOUNCE_MS, editDraft, editorState, resetStrategyEditor } from "../strategyEditorStore";
import type { SavedStrategy, StrategyPlan } from "../strategyApi";

/**
 * 策略编辑卡 (2026-10-08) in the strategy panel. Protects: a new user's card is empty (no stop or
 * take-profit made up for them) and cannot be saved or backtested before 开仓条件 and 止损; the AI's
 * prefill is marked with its basis and the mark goes once the user changes the field; 全部清空 really
 * clears; 补一句 sends no user limits and writes the designed plan into the form (not saved), lit once;
 * a draft thrown away never takes a late AI answer.
 */
const request = vi.hoisted(() => vi.fn());
vi.mock("../api", () => ({ requestJson: request, getStrategyMonitor: vi.fn(), stopStrategyMonitoring: vi.fn() }));

const blankPrefill = { eligible: false, reason: "近 20 个交易日只有 2 笔成交，不够 5 笔，先从空白或模板开始。", window: { tradingDays: 20 }, sample: { trades: 2, minTrades: 5 }, fields: {}, trades: [] };
const trade = (id: string, net: number) => ({ id, symbol: "SHFE.rb2701", direction: "long", volume: 1, openTime: 1_780_000_000_000_000_000, closeTime: 1_780_000_600_000_000_000, openPrice: 3050, closePrice: 3060, net });
const habitsPrefill = { eligible: true, window: { tradingDays: 20 }, sample: { trades: 14, minTrades: 5 },
  summary: "依据：近 20 个交易日 14 笔螺纹成交。", notice: "开仓条件不预填：从你的交易归纳开仓规则是下一期的功能，现在要你自己选。",
  fields: { symbol: { value: "SHFE.rb2701", label: "螺纹 主力（rb2701）", basis: "14 笔里 14 笔是螺纹", tradeIds: [] },
    direction: { value: "long", basis: "你近 20 天 14 笔里 11 笔做多", tradeIds: [] },
    stopLossTicks: { value: 15, basis: "你平时亏损单平均 14 跳出场", tradeIds: [] },
    closeBeforeEndMinutes: { value: 5, basis: "你从不留隔夜", tradeIds: [] },
    entryWindow: { value: { start: "09:00", end: "11:30" }, basis: "10 笔在 09:00–11:30 开仓", tradeIds: [] },
    lots: { value: 1, basis: "你每次都开 1 手", tradeIds: [] } },
  trades: [trade("t1", 300), trade("t2", -120)] };
const designed = { name: "突破追多", description: "收盘突破前高就追", direction: "long", timeframeSeconds: 300, maxPosition: 1, maxLoss: null, stopLossTicks: 15,
  takeProfitTicks: null, cooldownBars: 1, maxOrdersPerDay: 20, closeBeforeEndScope: "day", allowNightEntries: true, questions: [],
  rules: { entryLong: { join: "all", sequence: false, conditions: [{ label: "收盘价 上穿 前20根最高", left: { indicator: "close", timeframe: 0, period: 5, value: 0, barsAgo: 0 },
    operator: "cross_up", right: { indicator: "donchian_high", timeframe: 0, period: 20, value: 0, barsAgo: 0 }, withinBars: 1, consecutive: 1, minCount: 1 }] },
  entryShort: null, exitLong: null, exitShort: null } } as unknown as StrategyPlan;

let prefill: unknown = blankPrefill;
let holdDesign: Promise<void> | null = null;
beforeEach(() => {
  localStorage.clear(); vi.clearAllMocks(); resetUiSurfaces(); resetStrategyBacktests(); resetStrategyEditor();
  prefill = blankPrefill; holdDesign = null;
  request.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/api/strategy-plans/prefill")) return prefill;
    if (url.endsWith("/api/strategy-plans")) return { items: [], executions: [] };
    if (url.endsWith("/api/strategy-templates")) return { categories: [], items: [] };
    if (url.endsWith("/design-jobs") && init?.method === "POST") {
      if (holdDesign) await holdDesign;
      return { id: JSON.parse(String(init.body)).id, status: "completed", result: designed };
    }
    if (url.includes("/design-jobs/")) return {};
    if (url.endsWith("/api/strategy-backtests")) return new Promise(() => undefined);
    return { conditions: [] };
  });
});

afterEach(() => vi.useRealTimers());

const mount = () => render(<StrategyWorkbench rootUrl="http://main" baseUrl="http://main" symbol="SHFE.rb2701" chartTimeframe={300} replayActive={false} workspaceScope="home"/>);
const editor = () => uiSurface("strategyEditor")!;
const snapshot = () => editor().state as Record<string, unknown>;

test("新用户留空：没有预填，止损止盈空着，不能回测也不能存；命令 save 被拒并说明缺什么", async () => {
  mount();
  await waitFor(() => expect(uiSurface("strategyEditor")).toBeDefined());
  fireEvent.click(screen.getByRole("button", { name: "新建策略" }));
  expect(await screen.findByText(blankPrefill.reason)).toBeVisible();
  const card = screen.getByTestId("strategy-editor");
  expect(within(card).getByRole("textbox", { name: "合约" })).toHaveValue("SHFE.rb2701");
  expect(within(card).getByRole("spinbutton", { name: "止损" })).toHaveValue(null);
  expect(within(card).getByRole("spinbutton", { name: "止盈" })).toHaveValue(null);
  expect(within(card).getByRole("spinbutton", { name: "每次开仓手数" })).toHaveAttribute("placeholder", "1");
  expect(within(card).getByRole("spinbutton", { name: "每天最多开仓次数" })).toHaveAttribute("placeholder", "20");
  expect(within(card).queryByText(/AI 按你的习惯预填/)).toBeNull();
  expect(within(card).getByLabelText("回测结果")).toHaveTextContent("填好开仓条件和止损后自动回测");
  expect(within(card).getByRole("button", { name: "存为草稿" })).toBeDisabled();
  expect(() => editor().apply!({ save: true })).toThrow(/开仓条件、止损/);
  expect(snapshot()).toMatchObject({ open: true, mode: "new", strategyId: null, start: "blank", aiFields: [], missing: ["开仓条件", "止损"], backtest: null,
    canSave: false, prefill: { eligible: false, reason: blankPrefill.reason, count: 0, trades: 2 }, fields: { stop: { mode: "ticks", value: null }, take: { mode: "ticks", value: null } } });
  // 按我的习惯 with too few trades opens blank (the receipt counts that) and says why.
  act(() => editor().apply!({ start: "habits" }));
  await waitFor(() => expect(snapshot()).toMatchObject({ start: "blank", prefill: { eligible: false } }));
  expect(request.mock.calls.some(([url]) => String(url).endsWith("/api/strategy-backtests"))).toBe(false);
});

test("老用户预填：AI 标记 + 依据 + 总说明里的笔数；改过的字段不再标 AI；全部清空回到空白", async () => {
  prefill = habitsPrefill;
  mount();
  await waitFor(() => expect(uiSurface("strategyEditor")).toBeDefined());
  act(() => editor().apply!({ new: true }));
  const card = await screen.findByTestId("strategy-editor");
  expect(await within(card).findByText(/AI 按你的习惯预填了 6 项/)).toBeVisible();
  expect(within(card).getByRole("button", { name: "查看这 14 笔" })).toBeVisible();
  expect(within(card).getByText("你平时亏损单平均 14 跳出场")).toBeVisible();
  expect(within(card).getByText(/开仓条件不预填/)).toBeVisible();
  expect(within(card).getByRole("spinbutton", { name: "止损" })).toHaveValue(15);
  expect(within(card).getByRole("spinbutton", { name: "止损" }).closest("[data-ai]")).not.toBeNull();
  expect(snapshot()).toMatchObject({ start: "habits", aiFields: ["symbol", "direction", "stop", "closeBeforeEnd", "entryWindows", "lots"],
    prefill: { eligible: true, count: 6, trades: 14 }, fields: { direction: "long", closeBeforeEnd: { on: true, minutes: 5 }, entryWindows: [{ start: "09:00", end: "11:30" }] } });
  fireEvent.click(within(card).getByRole("button", { name: "查看这 14 笔" }));
  expect(within(card).getByRole("table", { name: "预填依据的成交" })).toHaveTextContent("-120");
  fireEvent.change(within(card).getByRole("spinbutton", { name: "止损" }), { target: { value: "20" } });
  expect(within(card).getByRole("spinbutton", { name: "止损" }).closest("[data-ai]")).toBeNull();
  expect(within(card).queryByText("你平时亏损单平均 14 跳出场")).toBeNull();
  expect((snapshot().aiFields as string[])).not.toContain("stop");
  fireEvent.click(within(card).getByRole("button", { name: "全部清空" }));
  await waitFor(() => expect(snapshot()).toMatchObject({ start: "blank", aiFields: [], fields: { stop: { value: null }, direction: null } }));
  expect(within(card).queryByText(/AI 按你的习惯预填/)).toBeNull();
});

test("补一句：表单空时按这句生成整份计划，不带方向 / 手数 / 亏损上限；写进表单不保存，改到的字段亮一次，下一次编辑清掉", async () => {
  mount();
  await waitFor(() => expect(uiSurface("strategyEditor")).toBeDefined());
  act(() => editor().apply!({ new: true }));
  await waitFor(() => expect(snapshot()).toMatchObject({ start: "blank" }));
  act(() => editor().apply!({ ask: "突破前高就追多，止损 15 跳" }));
  expect(snapshot()).toMatchObject({ ask: { text: "突破前高就追多，止损 15 跳" } });
  await waitFor(() => expect(snapshot()).toMatchObject({ ask: { status: "done" } }));
  const job = request.mock.calls.find(([url, init]) => String(url).endsWith("/design-jobs") && init?.method === "POST")!;
  const body = JSON.parse(String(job[1].body));
  expect(body.description).toBe("突破前高就追多，止损 15 跳");
  expect(body.constraints).toEqual({ clarificationRound: 0, clarificationAnswers: [] });
  expect(snapshot()).toMatchObject({ name: "突破追多", missing: [], fields: { direction: "long", stop: { mode: "ticks", value: 15 },
    entryLong: { join: "all", conditions: ["收盘价 上穿 前20根最高"] } } });
  expect(snapshot().changed).toEqual(expect.arrayContaining(["name", "direction", "entryLong", "stop"]));
  const card = screen.getByTestId("strategy-editor");
  expect(within(card).getByRole("group", { name: "做多条件" })).toHaveAttribute("data-changed");
  expect(request.mock.calls.some(([url, init]) => String(url).endsWith("/api/strategy-plans") && init?.method === "POST")).toBe(false);
  fireEvent.change(within(card).getByRole("spinbutton", { name: "止损" }), { target: { value: "18" } });
  expect(snapshot().changed).toEqual([]);
  expect(within(card).getByRole("group", { name: "做多条件" })).not.toHaveAttribute("data-changed");
  // A filled form sends what it has, so only the related fields change.
  act(() => editor().apply!({ ask: "止损改成 ATR 两倍" }));
  await waitFor(() => expect(request.mock.calls.filter(([url, init]) => String(url).endsWith("/design-jobs") && init?.method === "POST")).toHaveLength(2));
  const second = JSON.parse(String(request.mock.calls.filter(([url, init]) => String(url).endsWith("/design-jobs") && init?.method === "POST")[1][1].body));
  expect(second.constraints.previousPlan).toMatchObject({ stopLossTicks: 18, rules: { entryLong: { conditions: [{ label: "收盘价 上穿 前20根最高" }] } } });
  expect(second.constraints.editorRevision).toBe(true);
});

test("补一句改已填的表单：只落设计器真正改的项；它原样带回的空项默认值不填进表单，用户起的名字不被改掉", async () => {
  // 10-08 线上：表单已有止损、手数，说“止损改成 20 跳”后，空着的“每天最多开仓次数”等也被填上、名字被改。
  mount();
  await waitFor(() => expect(uiSurface("strategyEditor")).toBeDefined());
  act(() => editor().apply!({ new: true, ask: "突破前高就追多，止损 15 跳" }));
  await waitFor(() => expect(snapshot()).toMatchObject({ ask: { status: "done" }, name: "突破追多" }));
  const card = screen.getByTestId("strategy-editor");
  fireEvent.change(within(card).getByRole("textbox", { name: "策略名称" }), { target: { value: "我的突破" } });
  fireEvent.change(within(card).getByRole("spinbutton", { name: "每天最多开仓次数" }), { target: { value: "" } });
  expect(snapshot()).toMatchObject({ fields: { maxOrdersPerDay: null } });
  request.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/design-jobs") && init?.method === "POST") {
      const sent = JSON.parse(String(init.body)).constraints.previousPlan as StrategyPlan;
      // The designer echoes what it was sent (the empty 次数 as its default) and renames; it changes the stop.
      return { id: JSON.parse(String(init.body)).id, status: "completed", result: { ...sent, name: "均线突破", stopLossTicks: 20 } };
    }
    if (url.endsWith("/api/strategy-backtests")) return new Promise(() => undefined);
    return { conditions: [] };
  });
  act(() => editor().apply!({ ask: "止损改成 20 跳" }));
  await waitFor(() => expect(snapshot()).toMatchObject({ fields: { stop: { mode: "ticks", value: 20 } } }));
  expect(snapshot()).toMatchObject({ name: "我的突破", fields: { maxOrdersPerDay: null } });
  expect(snapshot().changed).toEqual(["stop"]);
});

test("开仓手数与回撤停机（电脑 ④）：默认固定手数、回撤不启用、数都空；必填空着不能存；补一句改止损不动资金设置；存的是用户设的那两项", async () => {
  mount();
  await waitFor(() => expect(uiSurface("strategyEditor")).toBeDefined());
  act(() => editor().apply!({ new: true, ask: "突破前高就追多，止损 15 跳" }));
  await waitFor(() => expect(snapshot()).toMatchObject({ ask: { status: "done" }, missing: [] }));
  const card = screen.getByTestId("strategy-editor");
  expect(within(card).getByRole("button", { name: "固定手数" })).toHaveAttribute("aria-pressed", "true");
  expect(within(card).getByRole("switch", { name: "回撤停机" })).toHaveAttribute("aria-checked", "false");
  expect(snapshot()).toMatchObject({ fields: { maxPosition: 1, sizingMore: { sizing: null, maxDrawdown: null } } });
  fireEvent.click(within(card).getByRole("button", { name: "按资金比例" }));
  expect(within(card).getByRole("spinbutton", { name: "资金占用上限" })).toHaveValue(null);
  expect(within(card).getByRole("spinbutton", { name: "按资金比例 · 最多手数" })).toHaveAttribute("placeholder", "不限");
  expect(snapshot()).toMatchObject({ missing: ["资金占用上限"], canSave: false, fields: { maxPosition: null } });
  expect(within(card).getByRole("button", { name: "存为草稿" })).toBeDisabled();
  fireEvent.change(within(card).getByRole("spinbutton", { name: "资金占用上限" }), { target: { value: "10" } });
  fireEvent.click(within(card).getByRole("switch", { name: "回撤停机" }));
  expect(snapshot()).toMatchObject({ missing: ["回撤停机的比例"], canSave: false });
  fireEvent.change(within(card).getByRole("spinbutton", { name: "回撤停机 · 回撤比例" }), { target: { value: "5" } });
  const funds = { sizing: { mode: "equity_percent", equityPercent: 10, maxLots: null }, maxDrawdown: { percent: 5 } };
  expect(snapshot()).toMatchObject({ missing: [], canSave: true, fields: { sizingMore: funds } });
  expect((snapshot().flow as { notes: string[] }).notes).toContain("本策略从最高点回撤 5% 停止并平仓");
  // The designer echoes the plan it was sent and changes the stop: the funds settings stay and are not marked changed.
  request.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/design-jobs") && init?.method === "POST") {
      const sent = JSON.parse(String(init.body)).constraints.previousPlan as StrategyPlan;
      return { id: JSON.parse(String(init.body)).id, status: "completed", result: { ...sent, stopLossTicks: 20 } };
    }
    if (url.endsWith("/api/strategy-plans") && init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      return { strategyId: "s-1", name: body.plan.name, symbol: "*", version: 1, status: "draft", definition: { schemaVersion: 2, plan: body.plan } };
    }
    if (url.endsWith("/api/strategy-plans")) return { items: [], executions: [] };
    if (url.endsWith("/api/strategy-backtests")) return new Promise(() => undefined);
    return { conditions: [] };
  });
  act(() => editor().apply!({ ask: "止损改成 20 跳" }));
  await waitFor(() => expect(snapshot()).toMatchObject({ fields: { stop: { mode: "ticks", value: 20 } } }));
  expect(snapshot().changed).toEqual(["stop"]);
  expect(snapshot()).toMatchObject({ fields: { sizingMore: funds } });
  act(() => editor().apply!({ save: true }));
  await waitFor(() => expect(request.mock.calls.some(([url, init]) => String(url).endsWith("/api/strategy-plans") && init?.method === "POST")).toBe(true));
  const save = request.mock.calls.find(([url, init]) => String(url).endsWith("/api/strategy-plans") && init?.method === "POST")!;
  expect(JSON.parse(String(save[1].body)).plan).toMatchObject({ stopLossTicks: 20, maxPosition: 1,
    sizing: { mode: "equity_percent", equityPercent: 10, maxLots: null }, maxDrawdownPercent: 5 });
});

test("放弃之后，迟到的补一句结果不写进任何表单", async () => {
  let release = () => undefined as void;
  holdDesign = new Promise<void>(resolve => { release = resolve; });
  mount();
  await waitFor(() => expect(uiSurface("strategyEditor")).toBeDefined());
  act(() => editor().apply!({ new: true, ask: "突破就追" }));
  await waitFor(() => expect(snapshot()).toMatchObject({ ask: { status: "running" } }));
  fireEvent.click(screen.getByRole("button", { name: "放弃" }));
  expect(snapshot()).toEqual({ open: false });
  act(() => editor().apply!({ new: true }));
  await act(async () => { release(); await new Promise(resolve => setTimeout(resolve, 0)); });
  expect(snapshot()).toMatchObject({ open: true, ask: null, changed: [], fields: { entryLong: null, stop: { value: null } } });
});

// 第二版 (2026-10-08): the receipt each new command gets, judged the way the Runtime and useUiControl judge it.
const applied = (change: Record<string, unknown>) => matches({ disclosures: disclosureStates(), strategyEditor: snapshot() }, change);

test("指标选择器（电脑）：命令打开某条条件一侧，分类里选到 SuperTrend 填倍数写回；持仓四象限只能配“等于”，其余比较禁选", async () => {
  mount();
  await waitFor(() => expect(uiSurface("strategyEditor")).toBeDefined());
  act(() => editor().apply!({ new: true }));
  await waitFor(() => expect(snapshot()).toMatchObject({ start: "blank" }));
  const card = screen.getByTestId("strategy-editor");
  fireEvent.click(within(card).getByRole("button", { name: "加一条：突破前高" }));
  const right = { disclosure: { name: "strategy-editor-picker", id: "entryLong:1:right", open: true } };
  act(() => uiSurface("disclosure:strategy-editor-picker:entryLong:1:right")!.apply!(true));
  expect(applied(right)).toBe(true);
  const picker = within(card).getByRole("group", { name: "改做多第 1 条 · 右边" });
  fireEvent.click(within(picker).getByRole("button", { name: /^趋势/ }));
  fireEvent.click(within(picker).getByRole("button", { name: "SuperTrend 超级趋势" }));
  fireEvent.change(within(picker).getByRole("spinbutton", { name: "倍数" }), { target: { value: "2.5" } });
  fireEvent.click(within(picker).getByRole("button", { name: "好" }));
  expect(within(card).queryByRole("group", { name: "改做多第 1 条 · 右边" })).toBeNull();
  expect(snapshot()).toMatchObject({ fields: { entryLong: { conditions: ["收盘价 上穿 超级趋势(10,2.5)"] } } });
  // The left side: no 数值 there; 持仓四象限 brings 等于 and the other comparisons are disabled (the engine refuses them).
  fireEvent.click(within(card).getByRole("button", { name: "做多第 1 条 · 指标" }));
  const left = within(card).getByRole("group", { name: "改做多第 1 条 · 左边" });
  expect(within(left).queryByRole("button", { name: /^数值/ })).toBeNull();
  fireEvent.change(within(left).getByRole("searchbox", { name: "搜指标" }), { target: { value: "四象限" } });
  fireEvent.click(within(left).getByRole("button", { name: "持仓四象限" }));
  fireEvent.click(within(left).getByRole("button", { name: "好" }));
  const op = within(card).getByRole("combobox", { name: "做多第 1 条 · 比较" });
  expect(op).toHaveValue("eq");
  expect(within(op).getByRole("option", { name: "高于" })).toBeDisabled();
  expect(snapshot()).toMatchObject({ fields: { entryLong: { conditions: ["持仓四象限 = 多头增仓"] } } });
  // 方向确认 (now editable): the group is joined by 全部满足 and its rows read current states only.
  fireEvent.click(within(card).getByRole("button", { name: "加一条做多方向确认" }));
  expect(within(within(card).getByRole("group", { name: "做多条件怎么合并" })).getByRole("button", { name: "任一满足" })).toBeDisabled();
  const align = within(card).getByRole("combobox", { name: "做多方向确认第 1 条 · 比较" });
  expect(within(align).getByRole("option", { name: "上穿" })).toBeDisabled();
  expect(within(align).getByRole("option", { name: "高于" })).toBeEnabled();
  expect(uiSurface("disclosure:strategy-editor-picker:entryLong-align:1:left")).toBeDefined();
});

test("流程图（电脑）：缺开仓条件和止损时那两块待填；点止损方块表单定位并高亮，命令 focus 回执 applied；“?”说明可用命令开合", async () => {
  mount();
  await waitFor(() => expect(uiSurface("strategyEditor")).toBeDefined());
  act(() => editor().apply!({ new: true }));
  await waitFor(() => expect(snapshot()).toMatchObject({ start: "blank" }));
  const card = screen.getByTestId("strategy-editor");
  const flow = within(card).getByTestId("strategy-flow");
  expect(within(flow).getByRole("button", { name: "开仓条件（待填）：在表单里看" })).toHaveAttribute("data-pending");
  expect(within(flow).getByRole("button", { name: "止损（待填）：在表单里看" })).toHaveAttribute("data-pending");
  fireEvent.change(within(card).getByRole("spinbutton", { name: "止损" }), { target: { value: "15" } });
  fireEvent.click(within(flow).getByRole("button", { name: "止损 15 跳：在表单里看" }));
  expect(applied({ strategyEditor: { focus: "stop" } })).toBe(true);
  const row = within(card).getByRole("spinbutton", { name: "止损" }).closest("[data-focus]")!;
  await waitFor(() => expect(row).toHaveAttribute("data-flash"));
  // 保本 sits in ③'s 更多: focusing it opens the fold first.
  act(() => editor().apply!({ focus: "breakeven" }));
  await waitFor(() => expect(within(card).getByRole("switch", { name: "保本" }).closest("[data-focus]")).toHaveAttribute("data-flash"));
  expect(uiSurface("disclosure:strategy-editor-more")!.state).toBe(true);
  // A "?" by command: open shows the explanation, the receipt reads it from state.disclosures.
  const tip = { disclosure: { name: "help-tip", id: "strategy-editor:section-entry", open: true } };
  act(() => uiSurface("disclosure:help-tip:strategy-editor:section-entry")!.apply!(true));
  expect(screen.getByTestId("help-tip-strategy-editor:section-entry")).toHaveTextContent("每行一条条件");
  expect(applied(tip)).toBe(true);
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByTestId("help-tip-strategy-editor:section-entry")).toBeNull();
  expect(applied({ disclosure: { ...tip.disclosure, open: false } })).toBe(true);
});

test("刚由接口建的策略马上能编辑：列表里还没有就读一次策略列表，卡片打开到它；回执等卡片真的显示它才算", async () => {
  mount();
  await waitFor(() => expect(uiSurface("strategyEditor")).toBeDefined());
  await waitFor(() => expect(screen.getByRole("button", { name: "新建策略" })).toBeVisible());
  const made = { strategyId: "s-new", name: "突破追多", symbol: "SHFE.rb2701", version: 1, status: "draft", definition: { schemaVersion: 1, plan: designed } };
  let release: (value: unknown) => void = () => undefined;
  request.mockImplementation(async (url: string) => {
    if (url.endsWith("/api/strategy-plans")) return new Promise(resolve => { release = resolve; });
    if (url.endsWith("/api/strategy-backtests")) return new Promise(() => undefined);
    return {};
  });
  const receipt = () => matches({ strategyEditor: snapshot() }, { strategyEditor: { edit: "s-new" } });
  expect(() => editor().apply!({ edit: "s-new" })).not.toThrow();
  expect(receipt()).toBe(false);
  await act(async () => { release({ items: [made], executions: [] }); });
  await screen.findByTestId("strategy-editor");
  expect(snapshot()).toMatchObject({ open: true, mode: "edit", strategyId: "s-new" });
  expect(receipt()).toBe(true);
});

test("合起来的命令（电脑）等卡片载入那条策略再动：edit+focus 不报先打开编辑卡，edit+ask 不把补一句发给已开的草稿", async () => {
  mount();
  await waitFor(() => expect(screen.getByRole("button", { name: "新建策略" })).toBeVisible());
  const made = { strategyId: "s-new", name: "突破追多", symbol: "SHFE.rb2701", version: 1, status: "draft", definition: { schemaVersion: 1, plan: designed } };
  const releases: ((value: unknown) => void)[] = [];
  const jobs = () => request.mock.calls.filter(([url, init]) => String(url).endsWith("/design-jobs") && (init as RequestInit | undefined)?.method === "POST");
  request.mockImplementation(async (url: string) => {
    if (url.endsWith("/api/strategy-plans")) return new Promise(resolve => { releases.push(resolve); });
    if (url.endsWith("/api/strategy-plans/prefill")) return blankPrefill;
    if (url.endsWith("/api/strategy-backtests") || url.endsWith("/design-jobs")) return new Promise(() => undefined);
    return {};
  });
  expect(() => act(() => editor().apply!({ edit: "s-new", focus: "stop" }))).not.toThrow();
  await act(async () => { releases.shift()!({ items: [made], executions: [] }); });
  expect(snapshot()).toMatchObject({ open: true, strategyId: "s-new", focus: "stop" });
  act(() => editor().apply!({ new: true }));
  await waitFor(() => expect(snapshot()).toMatchObject({ mode: "new", start: "blank" }));
  act(() => editor().apply!({ edit: "s-new", ask: "止损改 20 跳" }));
  expect(jobs()).toHaveLength(0);
  await act(async () => { releases.shift()!({ items: [made], executions: [] }); });
  expect(snapshot()).toMatchObject({ mode: "edit", strategyId: "s-new" });
  expect(jobs()).toHaveLength(1);
});


test("固定回测栏和完整区同步标记上次结果与重算失败，恢复成功才换数字", async () => {
  mount();
  await waitFor(() => expect(uiSurface("strategyEditor")).toBeDefined());
  vi.useFakeTimers();
  const pending: { resolve: (value: unknown) => void; reject: (error: Error) => void }[] = [];
  const fallback = request.getMockImplementation()!;
  request.mockImplementation((url: string, init?: RequestInit) => url.endsWith("/api/strategy-backtests")
    ? new Promise((resolve, reject) => pending.push({ resolve, reject })) : fallback(url, init));
  act(() => editDraft({ strategyId: "saved", definition: { plan: designed } } as SavedStrategy, "SHFE.rb2701"));
  await act(() => vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS));
  const result = (netProfit: number) => ({ summary: { netProfit, maxDrawdown: 10, winRate: 50, trades: 3 }, trades: [], exitReasons: [], equityCurve: [], coverage: { tradingDays: 60 } });
  await act(async () => { pending[0].resolve(result(111)); });
  const card = screen.getByTestId("strategy-editor");
  const bar = within(card.querySelector<HTMLElement>(".se-bar")!);
  expect(bar.getByLabelText("回测汇总")).toHaveTextContent("+111");
  fireEvent.change(within(card).getByRole("spinbutton", { name: "止损" }), { target: { value: "20" } });
  expect(bar.getByRole("status")).toHaveTextContent("上次结果，等待重算");
  await act(() => vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS));
  expect(bar.getByRole("status")).toHaveTextContent("上次结果，正在重算");
  await act(async () => { pending[1].reject(new Error("网络暂不可用")); });
  expect(bar.getByRole("alert")).toHaveTextContent("重算失败，以下为上次结果");
  expect(bar.getByLabelText("回测汇总")).toHaveTextContent("+111");
  expect(bar.getByRole("button", { name: "存为草稿" })).toBeEnabled();
  fireEvent.change(within(card).getByRole("spinbutton", { name: "止损" }), { target: { value: "25" } });
  await act(() => vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS));
  await act(async () => { pending[2].resolve(result(222)); });
  expect(bar.queryByRole("alert")).toBeNull();
  expect(bar.queryByRole("status")).toBeNull();
  expect(bar.getByLabelText("回测汇总")).toHaveTextContent("+222");
  vi.useRealTimers();
});

test("电脑保存旧修订成功时，不关闭保存期间又修改的编辑卡", async () => {
  mount();
  await waitFor(() => expect(uiSurface("strategyEditor")).toBeDefined());
  act(() => editDraft({ strategyId: "saved", definition: { plan: designed } } as SavedStrategy, "SHFE.rb2701"));
  let finish!: (value: unknown) => void;
  const fallback = request.getMockImplementation()!;
  request.mockImplementation((url: string, init?: RequestInit) => url.endsWith("/api/strategy-plans") && init?.method === "POST"
    ? new Promise(resolve => { finish = resolve; }) : fallback(url, init));
  const card = screen.getByTestId("strategy-editor");
  fireEvent.click(within(card).getByRole("button", { name: "存为草稿" }));
  fireEvent.change(within(card).getByRole("spinbutton", { name: "止损" }), { target: { value: "25" } });
  await act(async () => { finish({ strategyId: "version2", name: designed.name, definition: { plan: designed } }); });
  expect(screen.getByTestId("strategy-editor")).toBeVisible();
  expect(editorState()).toMatchObject({ saving: false, savedStrategyId: "version2", form: { stop: { value: 25 } } });
});

test("提醒模式（第 6 批）：只填条件和提醒时段；开始提醒存成只提醒的策略、按只提醒上线，不弹上线对话框，回到我的提醒", async () => {
  const shown = vi.fn();
  render(<StrategyWorkbench rootUrl="http://main" baseUrl="http://main" symbol="SHFE.rb2701" chartTimeframe={300} replayActive={false} workspaceScope="home" onShowReminders={shown}/>);
  await waitFor(() => expect(uiSurface("strategyEditor")).toBeDefined());
  act(() => editor().apply!({ new: true, reminder: true, ask: "突破前高就提醒" }));
  await waitFor(() => expect(snapshot()).toMatchObject({ reminder: true, ask: { status: "done" }, missing: [] }));
  const card = screen.getByTestId("strategy-editor");
  expect(within(card).getByRole("region", { name: "什么时候提醒" })).toBeInTheDocument();
  expect(within(card).getByRole("region", { name: "提醒时段" })).toBeInTheDocument();
  expect(within(card).queryByRole("region", { name: "怎么平仓" })).toBeNull();
  expect(within(card).queryByRole("region", { name: "仓位与时段" })).toBeNull();
  expect(within(card).queryByRole("region", { name: "回测结果" })).toBeNull();
  const saved = { strategyId: "r1", name: "突破追多", symbol: "*", version: 1, status: "draft", definition: { schemaVersion: 2, plan: { ...designed, reminderOnly: true } } };
  request.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/api/strategy-plans") && init?.method === "POST") return saved;
    if (url.endsWith("/api/strategy-plans")) return { items: [saved], executions: [] };
    if (url.endsWith("/api/ai-execution")) return { controls: { stopStatus: "RUNNING" } };
    if (url.endsWith("/validate")) return { strategyId: "r1-run" };
    if (url.endsWith("/monitor/start")) return {};
    if (url.includes("/monitor")) return { status: "monitoring" };
    return { conditions: [] };
  });
  fireEvent.click(within(card).getByRole("button", { name: "开始提醒" }));
  await waitFor(() => expect(shown).toHaveBeenCalled());
  const post = request.mock.calls.find(([url, init]) => String(url).endsWith("/api/strategy-plans") && init?.method === "POST")!;
  expect(JSON.parse(String(post[1].body)).plan.reminderOnly).toBe(true);
  const validate = request.mock.calls.find(([url]) => String(url).endsWith("/api/strategy-plans/r1/validate"))!;
  expect(JSON.parse(String(validate[1].body)).symbol).toBe("SHFE.rb2701");
  const start = request.mock.calls.find(([url]) => String(url).endsWith("/api/strategies/r1-run/monitor/start"))!;
  expect(JSON.parse(String(start[1].body))).toMatchObject({ signalAction: "alert" });
  expect(screen.queryByRole("dialog")).toBeNull();
});

import strategyRules from "../../../services/trader-runtime/src/trader_runtime/strategy_rules.py?raw";
import { vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { advancedRules, changedFields, emptyForm, emptyRow, formFromPlan, missingParts, normalizeTime, operandFor, planFromForm, quickRows, rowContext, rowProblem, withLeft, withOp,
  withSizingMode, type EditorForm, type RowForm } from "./strategyEditor";
import { BACKTEST_DEBOUNCE_MS, answerEditor, askEditor, setEditorAnswer, applyEditorChange, discardDraft, editDraft, editFields, editorSnapshot, editorState, hideDraft, newDraft, openDraft, resetStrategyEditor, saveDraft, setEditorContext,
  needsSave, uniqueName, useStrategyEditor } from "./strategyEditorStore";
import { PICKABLE, indicatorProblem, operatorProblem } from "./strategyIndicators";
import { holdingText, strategyFlow } from "./strategyFlow";
import { designerPlanProblem, type RuleCondition, type SavedStrategy, type StrategyPlan } from "./strategyApi";
import { activeTradingAccount, setActiveTradingAccount } from "./tradingAccountScope";

const request = vi.hoisted(() => vi.fn());
vi.mock("./api", () => ({ requestJson: request }));

/**
 * 策略编辑卡's mappings and its automatic backtest. Protects: what the card cannot show survives an edit
 * byte for byte (a save is a new version of the user's strategy — losing a 条件组合 or a 盘中触价 would
 * change how it trades), and the preview backtest runs once per settled edit, one at a time, never
 * showing an older form's result.
 */
const operand = (indicator: string, extra: Record<string, unknown> = {}) => ({ indicator, timeframe: 0, period: 5, value: 0, barsAgo: 0, ...extra }) as RuleCondition["left"];
const condition = (label: string, left: RuleCondition["left"], operator: RuleCondition["operator"], right: RuleCondition["right"], extra: Partial<RuleCondition> = {}): RuleCondition =>
  ({ label, left, operator, right, withinBars: 1, consecutive: 1, minCount: 1, ...extra });
const rich: StrategyPlan = {
  name: "螺纹回调", description: "原来的想法", direction: "both", timeframeSeconds: 300, maxPosition: 2, maxLoss: 3000, stopLossTicks: 15, takeProfitTicks: 40,
  cooldownBars: 3, maxOrdersPerDay: 6, closeBeforeEndScope: "session", closeBeforeEndMinutes: 5, allowNightEntries: false,
  scaleOut: [{ profitTicks: 20, profitAtr: null, percent: 50 }], sizing: { mode: "atr_risk", riskPercent: 1, atrMultiple: 2, atrPeriod: 14 },
  trailingStop: { mode: "ticks", value: 12, activateAfter: 8, atrPeriod: 14 },
  rules: {
    entryLong: { join: "any", sequence: false, conditions: [condition("收盘价 上穿 MA20", operand("close"), "cross_up", operand("ma", { period: 20 })),
      condition("偏移的", operand("close"), "gt", operand("ma", { period: 20, offset: { factor: 0.5, indicator: "atr", period: 14 } }))],
      branches: [{ join: "all", conditions: [condition("KD", operand("kd_k"), "cross_up", operand("kd_d"))] }],
      rearm: [condition("复位", operand("close"), "lt", operand("ma", { period: 10 }))] },
    entryShort: { join: "all", sequence: false, conditions: [condition("N 根前", operand("close", { barsAgo: 2 }), "lt", operand("ma", { period: 20 })),
      condition("MACD 死叉", operand("macd_dif"), "cross_down", operand("macd_dea"))],
      trigger: { kind: "stop", price: operand("donchian_low", { period: 20 }) } },
    exitLong: null, exitShort: null,
  },
  questions: [],
} as StrategyPlan;

beforeEach(() => { vi.useRealTimers(); request.mockReset(); resetStrategyEditor(); });

test("往返不丢：没改时原样写回；改止损跳数只动止损，条件组合、触价、复位、偏移、N 根前、分批止盈、亏损上限、按 ATR 定手数都原样", () => {
  expect(planFromForm(formFromPlan(rich, "SHFE.rb2701"), rich)).toEqual(rich);
  const form = formFromPlan(rich, "SHFE.rb2701");
  // The group with branches is read-only as a whole; the short side's N 根前 row is editable since 第二版 (the picker has 取 N 根前).
  expect(form.entryLong.locked).toBe(true);
  expect(form.entryShort.rows.map(row => Boolean(row.locked))).toEqual([false, false]);
  form.stop = { ...form.stop, value: 22 };
  expect(planFromForm(form, rich)).toEqual({ ...rich, stopLossTicks: 22 });
  // Editing the short group rewrites only the row that changed; the untouched N 根前 row and the trigger stay as they were.
  form.entryShort = { ...form.entryShort, rows: form.entryShort.rows.map(row => row.locked ? row : { ...row, op: "lt" }) };
  const saved = planFromForm(form, rich);
  expect(saved.rules.entryShort).toMatchObject({ trigger: rich.rules.entryShort!.trigger, conditions: [rich.rules.entryShort!.conditions![0], { operator: "lt" }] });
  expect(saved.rules.entryLong).toEqual(rich.rules.entryLong);
});

test("回调结构的止损只读：改止盈只动止盈", () => {
  const pullback = { ...rich, pullback: { trendPeriod: 60, touchPeriod: 20, guardPeriod: 60, maxBars: 10 } } as StrategyPlan;
  const form = formFromPlan(pullback, "SHFE.rb2701");
  expect(form.stop.mode).toBe("locked");
  form.take = { mode: "ticks", value: 55 };
  expect(planFromForm(form, pullback)).toEqual({ ...pullback, takeProfitTicks: 55 });
});

test("补一句后 AI 还在问的问题不挡回测：表单就是答案，按表单生成的计划不带待澄清问题", () => {
  // 10-08 线上：表单已有止损和手数，补一句后设计器仍问“止损几跳 / 每次几手”，计划带着问题去回测被 Runtime 拒绝。
  const designed = { ...rich, questions: ["止损几跳？"],
    clarifications: [{ id: "stop_loss", question: "价格反向走多少跳止损？", recommended: 0,
      options: [{ label: "15跳", description: "紧" }, { label: "30跳", description: "松" }] }] } as StrategyPlan;
  const plan = planFromForm(formFromPlan(designed, "SHFE.rb2701"), designed);
  expect(plan.questions).toEqual([]);
  expect(plan.clarifications).toEqual([]);
  expect({ ...plan, questions: undefined, clarifications: undefined }).toEqual({ ...designed, questions: undefined, clarifications: undefined });
});

test("新建：开仓条件和止损没填就缺；止盈留空 = 不设（显式 null）；留空字段用默认值，方向按有条件的组推断", () => {
  const form = emptyForm("SHFE.rb2701", 300);
  expect(missingParts(form)).toEqual(["开仓条件", "止损"]);
  form.entryLong = { ...form.entryLong, rows: quickRows("breakout", true) };
  form.stop = { mode: "ticks", value: 15, risk: null };
  expect(missingParts(form)).toEqual([]);
  const plan = planFromForm(form);
  expect(plan).toMatchObject({ name: "未命名策略", direction: "long", timeframeSeconds: 300, maxPosition: 1, maxOrdersPerDay: 20, stopLossTicks: 15, takeProfitTicks: null,
    rules: { entryLong: { join: "all", conditions: [{ label: "收盘价 上穿 前20根最高", operator: "cross_up" }] }, entryShort: null } });
  // The card's rows read the plan's own period (not 0 = whatever chart it is launched on); a new period moves them.
  expect(plan.rules.entryLong!.conditions![0]).toMatchObject({ left: { timeframe: 300 }, right: { timeframe: 300 } });
  expect(planFromForm({ ...formFromPlan(plan, "SHFE.rb2701"), timeframe: 900 }, plan).rules.entryLong!.conditions![0])
    .toMatchObject({ left: { timeframe: 900 }, right: { timeframe: 900 } });
  expect(uniqueName("未命名策略", ["未命名策略", "未命名策略 (2)"])).toBe("未命名策略 (3)");
  // 只做多 folds the short group: its rows stay in the form but not in the plan (the Runtime refuses 开空条件 on a long-only plan).
  const both = { ...form, direction: null, entryShort: { ...form.entryShort, rows: quickRows("breakout", false) } };
  expect(planFromForm(both).rules.entryShort).not.toBeNull();
  expect(planFromForm({ ...both, direction: "long" }).rules.entryShort).toBeNull();
});

test("自动回测：改动停 1.5 秒才算、同时只飞一个、飞的时候又改了就等它回来再算最新的，旧结果不显示", async () => {
  vi.useFakeTimers();
  setEditorContext({ rootUrl: "http://main", chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  const pending: ((value: unknown) => void)[] = [];
  request.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/prefill")) return { eligible: false, reason: "近 20 个交易日只有 2 笔", fields: {}, trades: [] };
    if (url.endsWith("/api/strategy-backtests") && init?.method === "POST") return new Promise(resolve => pending.push(resolve));
    throw new Error(`unexpected ${url}`);
  });
  newDraft();
  await vi.advanceTimersByTimeAsync(0);
  expect(editorState()).toMatchObject({ start: "blank", prefill: { eligible: false } });
  editFields(["entryLong"], form => ({ ...form, entryLong: { ...form.entryLong, rows: quickRows("ma-cross", true) } }));
  editFields(["stop"], form => ({ ...form, stop: { mode: "ticks", value: 10, risk: null } }));
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS - 100);
  editFields(["stop"], form => ({ ...form, stop: { mode: "ticks", value: 12, risk: null } }));
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS);
  const posts = () => request.mock.calls.filter(([url]) => String(url).endsWith("/api/strategy-backtests"));
  expect(posts()).toHaveLength(1);
  expect(JSON.parse(String(posts()[0][1].body))).toMatchObject({ symbol: "SHFE.rb2701", timeframe: 300, tradingDays: 60, preview: true, plan: { stopLossTicks: 12 } });
  // A change while it runs: the late result for 12 跳 is not shown; the 18 跳 one runs after it.
  editFields(["stop"], form => ({ ...form, stop: { mode: "ticks", value: 18, risk: null } }));
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS);
  expect(posts()).toHaveLength(1);
  const result = (net: number) => ({ summary: { netProfit: net, maxDrawdown: 100, winRate: 50, trades: 4 }, trades: [], exitReasons: [], coverage: { tradingDays: 60 }, planSummary: `净 ${net}` });
  pending[0](result(111));
  await vi.advanceTimersByTimeAsync(0);
  expect(posts()).toHaveLength(2);
  expect(JSON.parse(String(posts()[1][1].body)).plan.stopLossTicks).toBe(18);
  expect(editorState().backtest).toMatchObject({ status: "running" });
  expect(editorState().backtest.result).toBeUndefined();
  pending[1](result(222));
  await vi.advanceTimersByTimeAsync(0);
  expect(editorState().backtest).toMatchObject({ status: "done", result: { summary: { netProfit: 222 } }, previous: null });
  // 存为草稿 carries the one-sentence summary of the form it saves.
  request.mockImplementation(async (url: string, init?: RequestInit) => ({ strategyId: "s9", name: JSON.parse(String(init?.body)).plan.name, definition: {} }));
  expect(await saveDraft(["未命名策略"])).toMatchObject({ current: true });
  const body = JSON.parse(String(request.mock.calls.at(-1)![1].body));
  expect(body).toMatchObject({ plan: { name: "未命名策略 (2)", description: "策略编辑卡：净 222" }, instrumentScope: { mode: "any", symbols: [] } });
  expect(body).not.toHaveProperty("baseStrategyId");
  vi.useRealTimers();
});

test("自动回测不重复：回到结果所对应的表单（防抖内改回、重开没改的草稿）不再请求，结果和“上次”原样", async () => {
  vi.useFakeTimers();
  setEditorContext({ rootUrl: "http://main", chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  request.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/prefill")) return { eligible: false, reason: "", fields: {}, trades: [] };
    if (url.endsWith("/api/strategy-backtests") && init?.method === "POST")
      return { summary: { netProfit: 100, maxDrawdown: 10, winRate: 50, trades: 3 }, trades: [], exitReasons: [], coverage: { tradingDays: 60 }, planSummary: "" };
    throw new Error(`unexpected ${url}`);
  });
  const posts = () => request.mock.calls.filter(([url]) => String(url).endsWith("/api/strategy-backtests"));
  newDraft();
  await vi.advanceTimersByTimeAsync(0);
  const stop = (value: number) => editFields(["stop"], form => ({ ...form, stop: { mode: "ticks", value, risk: null } }));
  editFields(["entryLong"], form => ({ ...form, entryLong: { ...form.entryLong, rows: quickRows("ma-cross", true) } }));
  stop(20);
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS);
  expect(posts()).toHaveLength(1);
  const shown = editorState().backtest;
  expect(shown.status).toBe("done");
  // 20 → 21 → 20 inside the debounce: the card goes back to the result it already has.
  stop(21);
  expect(editorState().backtest.status).toBe("waiting");
  stop(20);
  expect(editorState().backtest).toMatchObject({ status: "done", key: shown.key, previous: shown.previous });
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS * 2);
  expect(posts()).toHaveLength(1);
  // ‹ 我的策略 then 继续编辑 with nothing changed: no second request for the same plan.
  hideDraft();
  openDraft();
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS * 2);
  expect(posts()).toHaveLength(1);
  expect(editorState().backtest.status).toBe("done");
  vi.useRealTimers();
});

test("手数、每天开仓次数、止损、止盈越界挡住回测和保存，流程图不画非法值（#755 #759）", () => {
  const form = emptyForm("SHFE.rb2701", 300);
  form.entryLong = { ...form.entryLong, rows: quickRows("breakout", true) };
  form.stop = { mode: "ticks", value: 15, risk: null };
  expect(missingParts(form)).toEqual([]);
  expect(missingParts({ ...form, lots: -1 })).toEqual(["每次开仓手数（1–20 手的整数）"]);
  expect(missingParts({ ...form, lots: 1.5 })).toEqual(["每次开仓手数（1–20 手的整数）"]);
  expect(missingParts({ ...form, maxOrdersPerDay: 0 })).toEqual(["每天最多开仓次数（1–50 次的整数）"]);
  expect(missingParts({ ...form, stop: { mode: "ticks", value: -1, risk: null } })).toEqual(["止损跳数（1–5000 跳的整数）"]);
  expect(missingParts({ ...form, stop: { mode: "atr", value: 0, risk: null } })).toEqual(["止损 ATR 倍数（大于 0、最多 20）"]);
  expect(missingParts({ ...form, take: { mode: "ticks", value: 0 } })).toEqual(["止盈跳数（1–10000 跳的整数）"]);
  // 留空 = 用默认（占位里写着），不算越界
  expect(missingParts({ ...form, lots: null, maxOrdersPerDay: null })).toEqual([]);
  const bad = { ...form, lots: -1, maxOrdersPerDay: 999999, stop: { mode: "ticks" as const, value: -1, risk: null } };
  const flow = strategyFlow(bad, planFromForm(form), undefined, false);
  expect(flow.action.title).toBe("开多 · 手数待改");
  expect(flow.exits[0].title).toBe("止损待改");
  expect(flow.notes).toContain("每天最多开仓次数待改");
});

test("开关开着却没填数的保护（追踪止损、保本、按时间止盈）挡住回测和保存，不会悄悄不带上", () => {
  const form = emptyForm("SHFE.rb2701", 300);
  form.entryLong = { ...form.entryLong, rows: quickRows("breakout", true) };
  form.stop = { mode: "ticks", value: 15, risk: null };
  expect(missingParts(form)).toEqual([]);
  expect(missingParts({ ...form, trailing: { ...form.trailing, on: true, value: null } })).toEqual(["追踪止损的回撤"]);
  expect(missingParts({ ...form, breakeven: { ...form.breakeven, on: true, trigger: null } })).toEqual(["保本的浮盈触发"]);
  expect(missingParts({ ...form, timeTakeProfit: [{ afterBars: 10, unit: "ticks", profit: 5 }, { afterBars: 20, unit: "ticks", profit: null }] }))
    .toEqual(["按时间止盈第 2 档"]);
  // Filled in, they are in the plan.
  const filled = { ...form, trailing: { ...form.trailing, on: true, value: 8 }, breakeven: { ...form.breakeven, on: true, trigger: 10 },
    timeTakeProfit: [{ afterBars: 10, unit: "ticks" as const, profit: 5 }] };
  expect(missingParts(filled)).toEqual([]);
  expect(planFromForm(filled)).toMatchObject({ trailingStop: { value: 8 }, breakeven: { triggerTicks: 10 }, timeTakeProfit: [{ afterBars: 10, profitTicks: 5 }] });
  // 流程图: a blank 方向确认 / 复位 row is not counted as a confirmation.
  const state = { ...form, entryLong: { ...form.entryLong, rows: form.entryLong.rows.map(row => ({ ...row, op: "gt" as const })) } };
  const blankRows = { ...state, entryLong: { ...state.entryLong, alignment: [emptyRow()], rearm: [emptyRow()] } };
  expect(strategyFlow(blankRows, planFromForm(state), undefined, false).entries[0]).toMatchObject({ id: "entryLong", lines: [] });
});

test("返回不留空草稿：按我的习惯的预填不算改动，看一眼就返回等于放弃；改了一项才留下可继续编辑的草稿", async () => {
  resetStrategyEditor();
  setEditorContext({ rootUrl: "http://main", chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  request.mockImplementation(async (url: string) => url.endsWith("/prefill")
    ? { eligible: true, fields: { stopLossTicks: { value: 15, basis: "平均 14 跳" }, lots: { value: 2, basis: "每次 2 手" } }, trades: [] }
    : new Promise(() => undefined));
  newDraft();
  await vi.waitFor(() => expect(editorState()).toMatchObject({ start: "habits", aiFields: ["stop", "lots"] }));
  hideDraft();
  expect(editorState()).toMatchObject({ open: false, draft: false });
  newDraft();
  await vi.waitFor(() => expect(editorState().start).toBe("habits"));
  editFields(["lots"], form => ({ ...form, lots: 3 }));
  hideDraft();
  expect(editorState()).toMatchObject({ open: false, draft: true, form: { lots: 3 } });
});

// 第二版 (2026-10-08): the fields the card now edits, and conditions with every operand parameter and modifier.
const wide = {
  name: "第二版", description: "全部可编辑字段", direction: "both", timeframeSeconds: 300, maxPosition: 3, maxLoss: 5000, stopLossTicks: 12, takeProfitTicks: null,
  cooldownBars: 3, maxOrdersPerDay: 8, maxEntriesPerDirection: 2, closeBeforeEndScope: "session", closeBeforeEndMinutes: 10, allowNightEntries: true,
  trailingStop: { mode: "atr_chandelier", value: 3, activateAfter: 1.5, atrPeriod: 22 },
  breakeven: { triggerTicks: null, triggerAtr: 1.2, offsetTicks: 1 },
  timeTakeProfit: [{ afterBars: 10, profitTicks: 8, profitAtr: null }, { afterBars: 30, profitTicks: null, profitAtr: 0.5 }],
  sizing: { mode: "atr_risk", riskPercent: 1, atrMultiple: 2.5, atrPeriod: 20 },
  rules: {
    entryLong: { join: "all", sequence: false, conditions: [
      condition("收盘价 上穿 肯特纳上轨", operand("close"), "cross_up", operand("keltner_up", { period: 30, k: 2, basis: "ma" })),
      condition("15 分 RSI", operand("rsi", { timeframe: 900, period: 14 }), "gt", operand("constant", { timeframe: 900, value: 55 }), { withinBars: 5, minCount: 3 }),
      condition("ADX 均线上升", operand("adx", { period: 14, average: 5 }), "rising", operand("constant", { value: 2 }), { consecutive: 2 }),
      condition("偏移", operand("close"), "gt", operand("today_open", { offset: { factor: 0.2, indicator: "range_nday", period: 5 } }))] },
    entryShort: { join: "any", sequence: false, conditions: [
      condition("四象限", operand("oi_quadrant", { period: 3 }), "eq", operand("constant", { value: 2 })),
      condition("跌破 S1", operand("close", { barsAgo: 1 }), "cross_down", operand("pivot", { value: -1 }), { withinBars: 3 })] },
    exitLong: { join: "all", sequence: false, conditions: [condition("跌破 10 根最低", operand("low"), "lt", operand("low", { lowest: 10 }))] },
    exitShort: null,
  },
  questions: [],
} as unknown as StrategyPlan;

test("往返不丢（第二版新增的可编辑字段与条件参数）：没改逐字节相同；改哪一项只写哪一项，其余条件对象原样", () => {
  expect(JSON.stringify(planFromForm(formFromPlan(wide, "SHFE.rb2701"), wide))).toBe(JSON.stringify(wide));
  const read = formFromPlan(wide, "SHFE.rb2701");
  // Only the 系数偏移 row stays read-only; every other parameter is in the picker.
  expect([...read.entryLong.rows, ...read.entryShort.rows, ...read.exitLong.rows].map(row => Boolean(row.locked))).toEqual([false, false, false, true, false, false, false]);
  const edits: [string, (form: EditorForm) => void, Partial<StrategyPlan>][] = [
    ["按波动定手数", form => { form.sizing = { ...form.sizing, riskPercent: 2 }; }, { sizing: { ...wide.sizing!, riskPercent: 2 } }],
    ["冷却", form => { form.cooldownBars = 0; }, { cooldownBars: 0 }],
    ["每个方向次数", form => { form.maxEntriesPerDirection = null; }, { maxEntriesPerDirection: null }],
    ["亏损上限", form => { form.maxLoss = null; }, { maxLoss: null }],
    ["追踪起点", form => { form.trailing = { ...form.trailing, activateAfter: 2 }; }, { trailingStop: { ...wide.trailingStop!, activateAfter: 2 } }],
    ["收盘前范围", form => { form.closeBeforeEnd = { ...form.closeBeforeEnd, everySession: false }; }, { closeBeforeEndScope: "day" }],
    ["保本 ATR", form => { form.breakeven = { ...form.breakeven, trigger: 1.5 }; }, { breakeven: { triggerTicks: null, triggerAtr: 1.5, offsetTicks: 1 } }],
    ["按时间止盈 ATR", form => { form.timeTakeProfit = form.timeTakeProfit.map((tier, at) => at === 1 ? { ...tier, profit: 0.8 } : tier); },
      { timeTakeProfit: [wide.timeTakeProfit![0], { afterBars: 30, profitTicks: null, profitAtr: 0.8 }] }],
  ];
  for (const [, edit, expected] of edits) {
    const form = formFromPlan(wide, "SHFE.rb2701");
    edit(form);
    expect(planFromForm(form, wide)).toEqual({ ...wide, ...expected });
  }
  // A modifier changed on one row rewrites that row only (its fixed 15 分 clock kept); the keltner / ADX / offset rows stay as they were.
  const form = formFromPlan(wide, "SHFE.rb2701");
  form.entryLong = { ...form.entryLong, rows: form.entryLong.rows.map((row, at) => at === 1 ? { ...row, minCount: 2 } : row) };
  const saved = planFromForm(form, wide).rules.entryLong!.conditions!;
  expect([saved[0], saved[2], saved[3]]).toEqual([0, 2, 3].map(at => wide.rules.entryLong!.conditions![at]));
  expect(saved[1]).toMatchObject({ left: { indicator: "rsi", timeframe: 900 }, right: { indicator: "constant", value: 55, timeframe: 900 }, withinBars: 5, minCount: 2,
    label: "RSI14 高于 55（5根内至少2根）" });
  // 浮盈加仓's own parameters: 最多持有 follows 每次 + 每次加几手 × 次数 (the engine's first entry = maxPosition − volume × maxAdds).
  const pyramid = { ...skeleton(), maxPosition: 4, pyramiding: { maxAdds: 3, trigger: "signal", step: 0.5, atrPeriod: 20, volume: 1, moveStop: false } } as StrategyPlan;
  const pyramidForm = formFromPlan(pyramid, "SHFE.rb2701");
  expect([pyramidForm.lots, pyramidForm.pyramiding]).toEqual([1, { on: true, maxAdds: 3, trigger: "signal", step: 0.5, atrPeriod: 20, volume: 1, moveStop: false }]);
  expect(planFromForm(pyramidForm, pyramid)).toEqual(pyramid);
  pyramidForm.pyramiding = { ...pyramidForm.pyramiding, maxAdds: 2, volume: 2 };
  expect(planFromForm(pyramidForm, pyramid)).toEqual({ ...pyramid, maxPosition: 5, pyramiding: { ...pyramid.pyramiding!, maxAdds: 2, volume: 2 } });
});
const skeleton = () => ({ name: "加仓", description: "加仓", direction: "long", timeframeSeconds: 300, maxPosition: 1, maxLoss: null, stopLossTicks: 10,
  takeProfitTicks: null, cooldownBars: 1, maxOrdersPerDay: 20, rules: { entryLong: { join: "all", sequence: false,
    conditions: [condition("MA5 上穿 MA20", operand("ma"), "cross_up", operand("ma", { period: 20 }))] }, entryShort: null, exitLong: null, exitShort: null }, questions: [] });

test("开仓手数三选一与回撤停机：默认固定、回撤关、百分比空；必填空着挡保存；按资金比例不被 1 手卡住；按波动的 1–20 上限照旧；往返与来回切换不丢", () => {
  // Protects: no risk number is made up for the user, and a saved strategy's sizing / stop rule is exactly what was set.
  const blank = emptyForm("SHFE.rb2701", 300);
  expect([blank.sizing.mode, blank.sizing.equityPercent, blank.maxDrawdown]).toEqual(["fixed", null, { on: false, percent: null }]);
  const fresh = planFromForm(blank);
  expect(["sizing", "maxDrawdownPercent"].filter(key => key in fresh)).toEqual([]);
  const plan = skeleton() as StrategyPlan;
  const base = formFromPlan(plan, "SHFE.rb2701");
  expect(missingParts({ ...base, sizing: withSizingMode(base.sizing, "equity_percent") })).toEqual(["资金占用上限"]);
  expect(missingParts({ ...base, sizing: withSizingMode(base.sizing, "atr_risk") })).toEqual(["单笔风险占权益的百分比"]);
  expect(missingParts({ ...base, maxDrawdown: { on: true, percent: null } })).toEqual(["回撤停机的比例"]);
  // 按资金比例 writes only its own numbers: maxPosition (1) is left as it was and no 最多 N 手 appears unless the user writes one.
  const ratio = { ...base, sizing: { ...withSizingMode(base.sizing, "equity_percent"), equityPercent: 10 }, maxDrawdown: { on: true, percent: 5 } };
  const saved = planFromForm(ratio, plan);
  expect(saved).toEqual({ ...plan, sizing: { mode: "equity_percent", equityPercent: 10, maxLots: null, atrMultiple: 2, atrPeriod: 14 }, maxDrawdownPercent: 5 });
  expect(designerPlanProblem(saved)).toBeNull();
  expect(designerPlanProblem({ ...saved, sizing: { ...saved.sizing!, equityPercent: 120 } })).toMatch(/最多 100%/);
  expect(designerPlanProblem({ ...saved, sizing: { ...saved.sizing!, maxLots: 1.5 } })).toMatch(/正整数/);
  expect(designerPlanProblem({ ...saved, maxDrawdownPercent: 0 })).toMatch(/回撤停机/);
  expect(strategyFlow(ratio, saved, undefined, false).action.title).toBe("开多 · 按资金比例 10% 权益（手数不另设上限）");
  // 按波动 keeps its required cap: 每次最多 is maxPosition, 1–20.
  const atr = { ...base, lots: 3, sizing: { ...withSizingMode(base.sizing, "atr_risk"), riskPercent: 1 } };
  expect(planFromForm(atr, plan)).toMatchObject({ maxPosition: 3, sizing: { mode: "atr_risk", riskPercent: 1, atrMultiple: 2, atrPeriod: 14 } });
  expect(designerPlanProblem(planFromForm({ ...atr, lots: 25 }, plan))).toMatch(/1–20/);
  // Read back byte for byte; switching to another mode and back, or 回撤 off and on with the same number, is no change at all.
  const reread = formFromPlan(saved, "SHFE.rb2701");
  expect(JSON.stringify(planFromForm(reread, saved))).toBe(JSON.stringify(saved));
  expect(changedFields(reread, { ...reread, sizing: withSizingMode(withSizingMode(reread.sizing, "atr_risk"), "equity_percent"),
    maxDrawdown: { on: true, percent: 5 } })).toEqual([]);
  expect(planFromForm({ ...reread, maxDrawdown: { on: false, percent: 5 } }, saved)).toEqual({ ...saved, maxDrawdownPercent: null });
  expect(planFromForm({ ...reread, sizing: withSizingMode(reread.sizing, "fixed") }, saved)).toEqual({ ...saved, sizing: null });
  // What the card cannot show (条件组合、盘中触价、复位、偏移) stays as it was when only 回撤停机 is added.
  const richForm = formFromPlan(rich, "SHFE.rb2701");
  expect(planFromForm({ ...richForm, maxDrawdown: { on: true, percent: 8 } }, rich)).toEqual({ ...rich, maxDrawdownPercent: 8 });
});

test("按资金比例的额度：按正式接口问当前账户；改比例、换草稿、换账户立刻丢掉旧数字，迟到的旧回答不写回；接口拒绝显示中文原因", async () => {
  // Protects: the 可开手数 shown is always this form's, this account's — a late answer for 10% never stands for 20%,
  // another draft or the previous account, and a loading card shows no figures at all.
  vi.useFakeTimers();
  setEditorContext({ rootUrl: "http://main", chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  const pending: { url: string; resolve: (value: unknown) => void; reject: (error: unknown) => void }[] = [];
  request.mockImplementation((url: string) => {
    if (url.endsWith("/prefill")) return Promise.resolve({ eligible: false, reason: "近 20 个交易日只有 2 笔", fields: {}, trades: [] });
    if (url.includes("/api/strategies/funds-preview?")) return new Promise((resolve, reject) => pending.push({ url, resolve, reject }));
    if (url.endsWith("/api/strategy-backtests")) return new Promise(() => undefined);
    throw new Error(`unexpected ${url}`);
  });
  const funds = (lots: number) => ({ equity: 1026580, equityPercent: 10, usedMargin: 0, pendingMargin: 0, remainingBudget: 102658, marginPerLot: 82350, availableLots: lots, unavailableReason: null });
  const ratio = (equityPercent: number | null, maxLots: number | null = null) => editFields(["sizing"], form =>
    ({ ...form, sizing: { ...withSizingMode(form.sizing, "equity_percent"), equityPercent, maxLots } }));
  const query = (index: number) => Object.fromEntries(new URL(pending[index].url).searchParams);
  newDraft();
  await vi.advanceTimersByTimeAsync(0);
  ratio(10);
  expect(editorState().funds).toMatchObject({ status: "loading" });
  ratio(15);
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS - 100);
  expect(pending).toHaveLength(0);
  await vi.advanceTimersByTimeAsync(100);
  // One request after it settles: GET with the contract and percentage; a new strategy has no strategyId, no cap no maxLots.
  expect(pending).toHaveLength(1);
  expect(pending[0].url.startsWith("http://main/api/strategies/funds-preview?")).toBe(true);
  expect(query(0)).toEqual({ symbol: "SHFE.rb2701", equityPercent: "15" });
  // 20% while 15% is on its way: the 15% figures never appear, also not as "loading" leftovers.
  ratio(20, 3);
  expect(editorState().funds).toEqual({ status: "loading", key: expect.any(String) });
  pending[0].resolve({ funds: funds(1) });
  await vi.advanceTimersByTimeAsync(0);
  expect(editorState().funds).toEqual({ status: "loading", key: expect.any(String) });
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS);
  expect(query(1)).toEqual({ symbol: "SHFE.rb2701", equityPercent: "20", maxLots: "3" });
  pending[1].resolve({ funds: { ...funds(2), equityPercent: 20 } });
  await vi.advanceTimersByTimeAsync(0);
  expect(editorState().funds).toMatchObject({ status: "done", data: { availableLots: 2, equityPercent: 20 }, at: expect.any(Number) });
  expect(editorSnapshot().funds).toMatchObject({ status: "done", availableLots: 2, estimatedAt: expect.any(String) });
  // An unrelated edit keeps this estimate (same key) and asks nothing.
  editFields(["name"], form => ({ ...form, name: "螺纹" }));
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS);
  expect(pending).toHaveLength(2);
  expect(editorState().funds).toMatchObject({ status: "done" });
  // Another account: the previous account's figures go at once and it is asked again; the old account's late answer is dropped.
  setActiveTradingAccount("tqkq-sim:other");
  expect(editorState().funds).toEqual({ status: "loading", key: expect.any(String) });
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS);
  expect(pending).toHaveLength(3);
  setActiveTradingAccount("tqkq-sim:third");
  pending[2].resolve({ funds: funds(9) });
  await vi.advanceTimersByTimeAsync(0);
  expect(editorState().funds).toEqual({ status: "loading", key: expect.any(String) });
  // The Runtime's refusal reads as its Chinese reason.
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS);
  pending[3].reject(Object.assign(new Error("409"), { status: 409, detail: { detail: { code: "funds_preview_unavailable", message: "行情或账户数据不新鲜，暂无法估算预算。" } } }));
  await vi.advanceTimersByTimeAsync(0);
  expect(editorState().funds).toMatchObject({ status: "error", error: "行情或账户数据不新鲜，暂无法估算预算。" });
  // A draft dropped before its timer fires never asks; one dropped in flight never writes into the next draft.
  ratio(30);
  discardDraft();
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS);
  expect(pending).toHaveLength(4);
  newDraft();
  await vi.advanceTimersByTimeAsync(0);
  ratio(30);
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS);
  expect(pending).toHaveLength(5);
  newDraft();
  await vi.advanceTimersByTimeAsync(0);
  ratio(30);
  pending[4].resolve({ funds: funds(7) });
  await vi.advanceTimersByTimeAsync(0);
  expect(editorState().funds).toEqual({ status: "loading", key: expect.any(String) });
  // Editing a saved strategy takes off what it already holds: strategyId goes with the request. Not 按资金比例 = no figures.
  const plan = { ...skeleton(), sizing: { mode: "equity_percent", equityPercent: 12, maxLots: null, atrMultiple: 2, atrPeriod: 14 } } as StrategyPlan;
  editDraft({ strategyId: "s-rb", name: "螺纹", symbol: "SHFE.rb2701", version: 1, status: "draft", definition: { plan } } as unknown as SavedStrategy, "SHFE.rb2701");
  await vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS);
  expect(pending).toHaveLength(6);
  expect(query(5)).toEqual({ symbol: "SHFE.rb2701", equityPercent: "12", strategyId: "s-rb" });
  editFields(["sizing"], form => ({ ...form, sizing: withSizingMode(form.sizing, "fixed") }));
  expect(editorState().funds).toBeNull();
  setActiveTradingAccount(null);
  vi.useRealTimers();
});

test("往返不丢（方向确认、复位、单笔预算、追踪 ATR 周期也可编辑）：没改逐字节相同，改哪条只写哪条；引擎对方向确认的约束照旧", () => {
  const aligned = { ...skeleton(), stopLossTicks: 12, perTradeRisk: 2000, trailingStop: { mode: "atr_chandelier", value: 3, activateAfter: 0, atrPeriod: 20 },
    rules: { entryLong: { join: "all", sequence: false,
      alignmentConditions: [condition("收盘价 高于 MA60", operand("close", { timeframe: 300 }), "gt", operand("ma", { timeframe: 300, period: 60 }))],
      conditions: [condition("收盘价 高于 MA20", operand("close", { timeframe: 300 }), "gt", operand("ma", { timeframe: 300, period: 20 }))],
      rearm: [condition("收盘价 低于 MA10", operand("close", { timeframe: 300 }), "lt", operand("ma", { timeframe: 300, period: 10 }))] },
    entryShort: null, exitLong: null, exitShort: null } } as unknown as StrategyPlan;
  expect(JSON.stringify(planFromForm(formFromPlan(aligned, "SHFE.rb2701"), aligned))).toBe(JSON.stringify(aligned));
  // Nothing of it is in 高级规则 any more.
  expect(advancedRules(aligned)).toEqual([]);
  const entry = aligned.rules.entryLong! as typeof aligned.rules.entryLong & { alignmentConditions: RuleCondition[]; rearm: RuleCondition[] };
  let form = formFromPlan(aligned, "SHFE.rb2701");
  form.entryLong = { ...form.entryLong, alignment: form.entryLong.alignment.map(row => ({ ...row, op: "gte" })) };
  const changed = planFromForm(form, aligned).rules.entryLong! as typeof entry;
  expect([changed.conditions, changed.rearm]).toEqual([entry.conditions, entry.rearm]);
  expect(changed.alignmentConditions[0]).toMatchObject({ operator: "gte", label: "收盘价 不低于 MA60", left: { timeframe: 300 } });
  form = formFromPlan(aligned, "SHFE.rb2701");
  form.entryLong = { ...form.entryLong, rearm: [] };
  expect(planFromForm(form, aligned).rules.entryLong).toEqual({ ...entry, rearm: [] });
  for (const [edit, expected] of [
    [(next: EditorForm) => { next.stop = { ...next.stop, risk: 1500 }; }, { perTradeRisk: 1500 }],
    [(next: EditorForm) => { next.stop = { ...next.stop, risk: null }; }, { perTradeRisk: null }],
    [(next: EditorForm) => { next.trailing = { ...next.trailing, atrPeriod: 30 }; }, { trailingStop: { ...aligned.trailingStop!, atrPeriod: 30 } }],
  ] as [(next: EditorForm) => void, Partial<StrategyPlan>][]) {
    const next = formFromPlan(aligned, "SHFE.rb2701");
    edit(next);
    expect(planFromForm(next, aligned)).toEqual({ ...aligned, ...expected });
  }
  // With 方向确认 the group reads current states on its own period, joined by 全部满足 (RuleGroup.ordered_events, check_executable).
  form = formFromPlan(aligned, "SHFE.rb2701");
  const main = rowContext(form, "entryLong", "rows"), rearm = rowContext(form, "entryLong", "rearm");
  expect(rowProblem({ ...form.entryLong.rows[0], op: "cross_up" }, main)).not.toBeNull();
  expect(rowProblem({ ...form.entryLong.rows[0], within: 3 }, main)).not.toBeNull();
  expect(rowProblem({ ...form.entryLong.rearm[0], op: "cross_down" }, rearm)).toBeNull();
  expect(rowProblem({ ...form.entryLong.rearm[0], left: { ...form.entryLong.rearm[0].left, timeframe: 900 } }, rearm)).not.toBeNull();
  expect(missingParts({ ...form, entryLong: { ...form.entryLong, join: "any" } })).toContain("做多有方向确认时条件要全部满足");
  // 开仓时段 is typed in 24 hours.
  expect(["930", "0930", "9:5", "21", "2130", "25:00", "abc"].map(normalizeTime)).toEqual(["09:30", "09:30", "09:05", "21:00", "21:30", "25:00", "abc"]);
  expect(missingParts({ ...form, entryWindows: [{ start: "9:3", end: "11:30" }] })).toContain("开仓时段（写成 09:30 这样的 24 小时制）");
});

test("指标选择器：引擎的全部操作数都能选到；参数范围和不合法组合的禁用与引擎的校验一致", () => {
  // The rule engine's own list (strategy_rules.py `Indicator = Literal[...]`).
  const engine = strategyRules.match(/^Indicator = Literal\[([\s\S]*?)\]/m)![1].match(/"[a-z_]+"/g)!.map(text => text.slice(1, -1));
  expect(engine.length).toBeGreaterThan(50);
  expect([...PICKABLE].sort()).toEqual([...engine].sort());
  const c = (value: number) => operandFor("constant", { value });
  const row = (left: RowForm["left"], op: RowForm["op"], right: RowForm["right"], extra: Partial<RowForm> = {}): RowForm => ({ ...emptyRow(), left, op, right, ...extra });
  // The engine takes these (strategy_rules.py: Operand / Condition validators and check_executable; mirrored in test_strategy_rules).
  for (const ok of [row(operandFor("adx", { period: 50 }), "gt", c(25)), row(operandFor("oi_quadrant"), "eq", c(4)), row(operandFor("close"), "rising", c(20)),
    row(operandFor("rsi"), "lt", c(30), { within: 5, minCount: 5 }), row(operandFor("close", { transform: "highest", transformN: 2, barsAgo: 50 }), "gt", operandFor("keltner_up", { k: 10, basis: "ma" })),
    row(operandFor("ma", { timeframe: 900 }), "cross_up", operandFor("ma", { period: 60 })), row(operandFor("ma", { timeframe: 300 }), "gt", c(1))]) {
    expect(rowProblem(ok, { planTimeframe: 300 })).toBeNull();
  }
  // …and refuses these; the card says why and keeps them out of a backtest or save.
  for (const refused of [row(operandFor("adx", { period: 51 }), "gt", c(25)), row(operandFor("trix", { period: 61 }), "gt", c(0)), row(operandFor("ma", { period: 1 }), "gt", c(0)),
    row(operandFor("cci", { period: 201 }), "gt", c(0)), row(operandFor("close"), "eq", c(1)), row(operandFor("oi_quadrant"), "gt", c(1)), row(operandFor("oi_quadrant"), "eq", c(5)),
    row(operandFor("close"), "rising", c(21)), row(operandFor("close"), "rising", operandFor("ma")), row(operandFor("close"), "cross_up", operandFor("ma"), { consecutive: 2 }),
    row(operandFor("close"), "gt", operandFor("ma"), { within: 3, minCount: 4 }), row(operandFor("constant", { value: 1 }), "gt", c(0)), row(operandFor("close"), "gt", operandFor("oi_quadrant")),
    row(operandFor("close", { barsAgo: 51 }), "gt", c(0)), row(operandFor("close", { transform: "average", transformN: 1 }), "gt", c(0)), row(operandFor("boll_up", { k: 11 }), "gt", c(0)),
    row(operandFor("pivot", { value: 4 }), "gt", c(0)), row(operandFor("ma", { timeframe: 60 }), "gt", c(0))]) {
    expect(rowProblem(refused, { planTimeframe: 300 })).not.toBeNull();
  }
  // The picker and the comparison list disable the same before anything is sent.
  expect([operatorProblem("gt", "oi_quadrant"), operatorProblem("eq", "close"), operatorProblem("cross_up", "close", 2), indicatorProblem("constant", "left"),
    indicatorProblem("atr", "right", { spread: true })].every(Boolean)).toBe(true);
  expect([operatorProblem("eq", "oi_quadrant"), operatorProblem("gt", "close", 2), indicatorProblem("ma", "right", { spread: true })]).toEqual([null, null, null]);
  // 持仓四象限 brings 等于 and its first kind; 上升 takes 几根前; 交叉 drops 连续 N 根.
  expect(withLeft(row(operandFor("close"), "gt", c(1)), operandFor("oi_quadrant"))).toMatchObject({ op: "eq", right: { indicator: "constant", value: 1 } });
  expect(withOp(row(operandFor("close"), "gt", operandFor("ma")), "rising")).toMatchObject({ right: { indicator: "constant", value: 1 } });
  expect(withOp(row(operandFor("close"), "gt", operandFor("ma"), { consecutive: 3 }), "cross_up").consecutive).toBe(1);
});

test("流程图：次数只取当前表单的预览回测，拿不到不写；缺开仓条件和止损时那两块待填", () => {
  const blank = emptyForm("SHFE.rb2701", 300);
  const pending = strategyFlow(blank, planFromForm(blank), undefined, false);
  expect(pending.entries).toMatchObject([{ pending: true }]);
  expect(pending.exits[0]).toMatchObject({ id: "stop", pending: true, count: null });
  const form = { ...blank, entryLong: { ...blank.entryLong, rows: quickRows("ma-cross", true) }, stop: { mode: "ticks", value: 15, risk: null },
    take: { mode: "ticks", value: 30 }, closeBeforeEnd: { on: true, minutes: 5, everySession: false } } as EditorForm;
  const plan = planFromForm(form);
  const label = plan.rules.entryLong!.conditions![0].label;
  const result = { summary: { trades: 23, averageBarsHeld: 7, netProfit: 1, maxDrawdown: 1, winRate: 50 }, timeframeSeconds: 300, coverage: { tradingDays: 60 }, trades: [],
    exitReasons: [{ reason: "stop_loss", label: "止损", count: 12, net: -900 }, { reason: "take_profit", label: "止盈", count: 8, net: 1500 }],
    diagnosis: { entryLong: { judgedBars: 100, allPass: 23, relaxedCrossPass: null, conditions: [{ label, alone: 61, without: 0, cross: true }] } } } as never;
  const fresh = strategyFlow(form, plan, result, true);
  expect(fresh.basis).toBe("近 60 个交易日");
  expect(fresh.entries[0]).toMatchObject({ id: "entryLong", count: 23, rows: [{ count: 61 }] });
  expect(fresh.action.count).toBe(23);
  expect(fresh.exits.map(box => [box.id, box.count, box.net])).toEqual([["stop", 12, -900], ["take", 8, 1500], ["closeBeforeEnd", 0, null]]);
  expect(fresh.notes).toContain("平均持仓约 35 分钟（7 根）");
  // Long holds read as trading time on the strategy's own period: hours, then trading days by the backtest's own bars per day.
  expect([holdingText(25, 300, { bars: 4500, tradingDays: 60 }), holdingText(3227, 300, { bars: 4500, tradingDays: 60 }), holdingText(4.2, 86400)])
    .toEqual(["平均持仓约 2.1 小时（25 根）", "平均持仓约 43 个交易日（3227 根）", "平均持仓约 4.2 个交易日"]);
  // The form changed since that preview (or it failed): no count at all rather than an old one.
  const stale = strategyFlow(form, plan, result, false);
  expect([stale.basis, stale.entries[0].count, stale.entries[0].rows![0].count, stale.action.count, ...stale.exits.map(box => box.count)]).toEqual([null, null, null, null, null, null, null]);
  expect(stale.notes.some(note => note.startsWith("平均持仓"))).toBe(false);
});

test("流程图定位：strategyEditor {focus} 记进快照（和流程图结构一起），未知的块、关着的卡被拒", async () => {
  setEditorContext({ rootUrl: "http://main", chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  request.mockImplementation(async (url: string) => url.endsWith("/prefill") ? { eligible: false, fields: {}, trades: [] } : new Promise(() => undefined));
  const host = { edit: vi.fn(), save: vi.fn() };
  expect(() => applyEditorChange({ focus: "stop" }, host)).toThrow("先打开编辑卡");
  applyEditorChange({ new: true, focus: "stop" }, host);
  expect(editorSnapshot()).toMatchObject({ focus: "stop", flow: { entries: [{ pending: true }], exits: [{ id: "stop", pending: true }] } });
  expect(() => applyEditorChange({ focus: "nowhere" as never }, host)).toThrow(/没有这一块/);
});


const editable = (name: string) => ({ strategyId: name, name, symbol: "SHFE.rb2701", version: 1, status: "draft", definition: { plan: { ...skeleton(), name } } }) as unknown as SavedStrategy;
const preview = (netProfit: number) => ({ summary: { netProfit, maxDrawdown: 10, winRate: 50, trades: 3 }, trades: [], exitReasons: [], equityCurve: [], coverage: { tradingDays: 60 } });

test.each([false, true])("旧草稿回测结束后交给最新草稿，旧失败=%s", async (failed) => {
  vi.useFakeTimers();
  setEditorContext({ rootUrl: "http://main", chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  const pending: { resolve: (value: unknown) => void; reject: (error: Error) => void }[] = [];
  request.mockImplementation(() => new Promise((resolve, reject) => pending.push({ resolve, reject })));
  const { result } = renderHook(useStrategyEditor);
  act(() => editDraft(editable("A"), "SHFE.rb2701"));
  await act(() => vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS));
  act(() => editDraft(editable("B"), "SHFE.rb2701"));
  await act(() => vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS));
  expect(pending).toHaveLength(1);
  await act(async () => { if (failed) pending[0].reject(new Error("old failure")); else pending[0].resolve(preview(111)); });
  expect(pending).toHaveLength(2);
  expect(JSON.parse(String(request.mock.calls[1][1].body)).plan.name).toBe("B");
  await act(async () => { pending[1].resolve(preview(222)); });
  expect(result.current.backtest).toMatchObject({ status: "done", result: { summary: { netProfit: 222 } } });
  // A subsequent in-flight request cannot wake a discarded editor.
  act(() => editDraft(editable("C"), "SHFE.rb2701"));
  await act(() => vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS));
  act(() => discardDraft());
  await act(async () => { pending[2].resolve(preview(333)); });
  expect(pending).toHaveLength(3);
  expect(result.current.open).toBe(false);
  vi.useRealTimers();
});

test.each(["name", "stop", "revert", "reopen", "unchanged"])("保存回包保留提交后的编辑或重开：%s", async change => {
  vi.useFakeTimers();
  setEditorContext({ rootUrl: "http://main", chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  let finish!: (value: unknown) => void;
  request.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const { result } = renderHook(useStrategyEditor);
  act(() => editDraft(editable("A"), "SHFE.rb2701"));
  act(() => editFields(["stop"], form => ({ ...form, stop: { ...form.stop, value: 20 } })));
  let saving!: ReturnType<typeof saveDraft>;
  act(() => { saving = saveDraft(); });
  expect(JSON.parse(String(request.mock.calls[0][1].body)).plan.stopLossTicks).toBe(20);
  act(() => {
    if (change === "name") editFields(["name"], form => ({ ...form, name: "new name" }));
    if (change === "stop" || change === "revert") editFields(["stop"], form => ({ ...form, stop: { ...form.stop, value: 25 } }));
    if (change === "revert") editFields(["stop"], form => ({ ...form, stop: { ...form.stop, value: 20 } }));
    if (change === "reopen") { hideDraft(); openDraft(); }
  });
  await act(async () => { finish({ ...editable("saved"), definition: { plan: { ...skeleton(), stopLossTicks: 20 } } }); await saving; });
  expect(await saving).toMatchObject({ item: { strategyId: "saved" }, current: change === "unchanged" });
  expect(result.current).toMatchObject({ open: true, saving: false, savedStrategyId: "saved" });
  expect(result.current.form.stop.value).toBe(change === "stop" ? 25 : 20);
  if (change === "name") expect(result.current.form.name).toBe("new name");
  expect(needsSave()).toBe(change === "name" || change === "stop");
  vi.useRealTimers();
});

test.each(["account", "percent", "runtime"])("资金预估往返同值仍拒绝旧成功和旧失败：%s", async scope => {
  vi.useFakeTimers();
  setActiveTradingAccount("A");
  setEditorContext({ rootUrl: "http://main", chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  const pending: { resolve: (value: unknown) => void; reject: (error: Error) => void }[] = [];
  request.mockImplementation((url: string) => url.includes("funds-preview") ? new Promise((resolve, reject) => pending.push({ resolve, reject })) : new Promise(() => undefined));
  const { result } = renderHook(useStrategyEditor);
  act(() => {
    editDraft(editable("A"), "SHFE.rb2701");
    editFields(["sizing"], form => ({ ...form, sizing: { ...withSizingMode(form.sizing, "equity_percent"), equityPercent: 10 } }));
  });
  const change = (back: boolean) => act(() => {
    if (scope === "account") setActiveTradingAccount(back ? "A" : "B");
    if (scope === "percent") editFields(["sizing"], form => ({ ...form, sizing: { ...form.sizing, equityPercent: back ? 10 : 20 } }));
    if (scope === "runtime") setEditorContext({ rootUrl: back ? "http://main" : "http://other", chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  });
  await act(() => vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS));
  change(false); await act(() => vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS));
  change(true); await act(() => vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS));
  expect(pending).toHaveLength(3);
  await act(async () => { pending[2].resolve({ funds: { equity: 200000, availableLots: 2 } }); });
  await act(async () => { pending[0].resolve({ funds: { equity: 1000000, availableLots: 10 } }); pending[1].reject(new Error("old failure")); });
  expect(result.current.funds).toMatchObject({ status: "done", data: { equity: 200000, availableLots: 2 } });
  act(() => editFields(["name"], form => ({ ...form, name: "unrelated edit" })));
  await act(() => vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS));
  expect(pending).toHaveLength(3);
  vi.useRealTimers();
});


test.each([false, true])("回测账户 A→B→A 保留当前账户归属，旧失败=%s", async failed => {
  vi.useFakeTimers();
  setActiveTradingAccount("A");
  setEditorContext({ rootUrl: "http://main", chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  const pending: { account: string | null; resolve: (value: unknown) => void; reject: (error: Error) => void }[] = [];
  request.mockImplementation(() => new Promise((resolve, reject) => pending.push({ account: activeTradingAccount(), resolve, reject })));
  const { result } = renderHook(useStrategyEditor);
  act(() => editDraft(editable("same plan"), "SHFE.rb2701"));
  await act(() => vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS));
  for (const [index, account] of ["B", "A"].entries()) {
    act(() => setActiveTradingAccount(account));
    await act(() => vi.advanceTimersByTimeAsync(BACKTEST_DEBOUNCE_MS));
    await act(async () => { if (failed) pending[index].reject(new Error("old account")); else pending[index].resolve(preview(111)); });
    expect(result.current.backtest.result).toBeUndefined();
    expect(pending).toHaveLength(index + 2);
    expect(pending[index + 1].account).toBe(account);
  }
  await act(async () => { pending[2].resolve(preview(222)); });
  expect(result.current.backtest).toMatchObject({ status: "done", result: { summary: { netProfit: 222 } } });
  vi.useRealTimers();
});


test.each([[false, false], [false, true], [true, false]])("模板保存接续正式名称与版本，切换 Runtime=%s，另改名字=%s", async (moved, renamed) => {
  vi.useFakeTimers();
  setEditorContext({ rootUrl: "http://main", chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  let finish!: (value: unknown) => void;
  request.mockImplementation((url: string) => url.includes("/strategy-templates/")
    ? Promise.resolve({ plan: skeleton() }) : new Promise(resolve => { finish = resolve; }));
  await act(async () => { newDraft("template:breakout"); });
  let saving!: ReturnType<typeof saveDraft>;
  act(() => { saving = saveDraft(); });
  expect(JSON.parse(String(request.mock.calls.at(-1)![1].body))).toMatchObject({ templateId: "breakout" });
  act(() => editFields(["stop"], form => ({ ...form, stop: { ...form.stop, value: 25 } })));
  if (renamed) act(() => editFields(["name"], form => ({ ...form, name: "我新取的名字" })));
  if (moved) setEditorContext({ rootUrl: "http://other", chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  await act(async () => { finish({ ...editable("saved-template"), name: "加仓 (2)" }); await saving; });
  expect((await saving).current).toBe(false);
  expect(editorState().form.stop.value).toBe(25);
  act(() => { saving = saveDraft(); });
  const sent = JSON.parse(String(request.mock.calls.at(-1)![1].body));
  expect(sent.plan.stopLossTicks).toBe(25);
  expect(sent.plan.name).toBe(moved ? "加仓" : renamed ? "我新取的名字" : "加仓 (2)");
  if (moved) {
    expect(sent.templateId).toBe("breakout");
    expect(sent).not.toHaveProperty("baseStrategyId");
  } else {
    expect(sent.baseStrategyId).toBe("saved-template");
    expect(sent).not.toHaveProperty("templateId");
  }
  await act(async () => { finish({ ...editable("saved-template"), version: 2 }); await saving; });
  expect((await saving).current).toBe(true);
  vi.useRealTimers();
});

test.each(["ask", "ask-first", "answer", "command-answer"])("保存过程中补一句或填答案不能关闭编辑卡：%s", async change => {
  vi.useFakeTimers();
  setEditorContext({ rootUrl: "http://main", chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  let finishSave!: (value: unknown) => void, finishDesign!: (value: unknown) => void;
  request.mockImplementation((url: string) => new Promise(resolve => {
    if (url.endsWith("/design-jobs")) finishDesign = resolve;
    else finishSave = resolve;
  }));
  const item = editable("A");
  item.definition.plan = { ...skeleton(), questions: ["止损几跳？"], clarifications: [{ id: "stop_loss", question: "止损几跳？", recommended: 0,
    options: [{ label: "25跳", description: "25跳" }] }] } as StrategyPlan;
  act(() => editDraft(item, "SHFE.rb2701"));
  let saving!: ReturnType<typeof saveDraft>;
  act(() => { saving = saveDraft(); });
  act(() => {
    if (change === "ask" || change === "ask-first") askEditor("止损改成25跳");
    else if (change === "answer") setEditorAnswer("stop_loss", "25跳");
    else answerEditor({ stop_loss: "25跳" });
  });
  if (change === "ask-first") await act(async () => { finishDesign({ status: "completed", result: { ...skeleton(), stopLossTicks: 25 } }); });
  await act(async () => { finishSave(editable("saved")); await saving; });
  expect((await saving).current).toBe(false);
  expect(editorState().open).toBe(true);
  if (change === "answer") expect(editorState().answers.stop_loss).toBe("25跳");
  else {
    if (change !== "ask-first") {
      expect(editorState().ask?.status).toBe("running");
      await act(async () => { finishDesign({ status: "completed", result: { ...skeleton(), stopLossTicks: 25 } }); });
    }
    expect(editorState().form.stop.value).toBe(25);
    expect(editorState().ask?.status).toBe("done");
    expect(needsSave()).toBe(true);
  }
  vi.useRealTimers();
});

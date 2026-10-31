import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import { MobileStrategyEditor, useMobileStrategyEditor } from "./MobileStrategyEditor";
import { useMobileStrategies } from "./MobileStrategy";
import { disclosureStates, resetUiSurfaces, uiSurface } from "../../uiSurfaces";
import { matches } from "../../hooks/useUiControl";
import { resetStrategyBacktests } from "../../strategyBacktests";
import { editDraft, editFields, editorState, hideDraft, openDraft, resetStrategyEditor } from "../../strategyEditorStore";
import { resetStrategyTemplates } from "../../strategyTemplates";
import { emptyForm, planFromForm, quickRows } from "../../strategyEditor";
import type { PushToTalk } from "../futures/PushToTalk";

/**
 * 手机版策略编辑卡 (2026-10-08). Protects: the phone edits the one draft the desktop card edits (strategyEditorStore)
 * — a condition changed in the bottom sheet lands in that draft and its plan; a new user's page is empty and cannot
 * be saved (by button or by command) before 开仓条件 and 止损; the habits prefill is marked with its basis trades;
 * the sections, the sheet and the basis fold are commandable disclosures; the 补一句 microphone's words go to 补一句.
 */
const request = vi.hoisted(() => vi.fn());
vi.mock("../../api", () => ({ requestJson: request, getStrategyMonitor: vi.fn(), setStrategyMonitoring: vi.fn(), stopStrategyMonitoring: vi.fn() }));

const blank = { eligible: false, reason: "近 20 个交易日只有 2 笔成交，不够 5 笔，先从空白或模板开始。", sample: { trades: 2, minTrades: 5 }, fields: {}, trades: [] };
const habits = { eligible: true, sample: { trades: 14, minTrades: 5 }, summary: "依据：近 20 个交易日 14 笔螺纹成交。",
  fields: { symbol: { value: "SHFE.rb2701", label: "螺纹 主力（rb2701）", basis: "14 笔全是螺纹" }, direction: { value: "long", basis: "14 笔里 11 笔做多" },
    stopLossTicks: { value: 15, basis: "亏损单平均 14 跳出场" } },
  trades: [{ id: "t1", symbol: "SHFE.rb2701", direction: "long", volume: 1, openTime: 1_780_000_000_000_000_000, closeTime: 1_780_000_600_000_000_000, openPrice: 3050, closePrice: 3060, net: 300 }] };
let prefill: unknown = blank;
const pressTo = vi.fn();
const showReminders = vi.fn();
const ptt = { status: { state: "idle", level: 0, partial: "", notice: "" }, blockedReason: "", keyLabel: "右 Option", sendOnRelease: true, press: vi.fn(), pressTo } as unknown as PushToTalk;

function Host() {
  const strategies = useMobileStrategies({ baseUrl: "http://main", rootUrl: "http://main", enabled: true, detailed: false, chartSymbol: "SHFE.rb2701", chartTimeframe: 300 });
  const host = useMobileStrategyEditor({ enabled: true, rootUrl: "http://main", baseUrl: "http://main", chartSymbol: "SHFE.rb2701", chartTimeframe: 300, strategies, onShowMine: () => undefined, onShowReminders: showReminders });
  return host.editor.open ? <MobileStrategyEditor key={host.editor.draftId} state={host.editor} chartTimeframe={300} ptt={ptt} {...host.handlers} /> : null;
}

beforeEach(() => {
  vi.clearAllMocks(); resetUiSurfaces(); resetStrategyBacktests(); resetStrategyEditor(); resetStrategyTemplates();
  prefill = blank;
  request.mockImplementation(async (url: string) => {
    if (url.endsWith("/api/strategy-plans/prefill")) return prefill;
    if (url.endsWith("/api/strategy-plans")) return { items: [], executions: [] };
    if (url.endsWith("/api/strategy-templates")) return { categories: [], items: [] };
    if (url.endsWith("/api/strategy-backtests")) return new Promise(() => undefined);
    return {};
  });
});

const command = (change: Record<string, unknown>) => act(() => uiSurface("strategyEditor")!.apply!(change));
const disclosure = (name: string, id: string, open: boolean) => act(() => uiSurface(`disclosure:${name}:${id}`)!.apply!(open));

test("留空：新用户空着开仓条件和止损，存为草稿与上线不可点，命令 save 被拒；补一句的话筒把识别的话送进补一句", async () => {
  render(<Host />);
  command({ new: true });
  const page = await screen.findByTestId("mobile-strategy-editor");
  await waitFor(() => expect(editorState().start).toBe("blank"));
  expect(within(page).queryByText(/AI 预填了/)).toBeNull();
  expect(within(page).getByText("近 20 个交易日只有 2 笔成交，不够 5 笔，先从空白或模板开始。")).toBeVisible();
  expect(within(page).getByRole("button", { name: "存为草稿" })).toBeDisabled();
  expect(within(page).getByRole("button", { name: "上线运行…" })).toBeDisabled();
  expect(within(page).getByText("还差：开仓条件、止损")).toBeVisible();
  expect(() => command({ save: true })).toThrow("还没填好：开仓条件、止损");
  // Folded sections say themselves in one line; ③ says the stop is missing.
  expect(within(page).getByRole("button", { name: "平仓" })).toHaveTextContent("止损没填 · 不设止盈");
  // An empty 固定跳数 asks for a number: “不设” is only the 不设 choice (it read as already decided).
  disclosure("strategy-editor-section", "exit", true);
  expect(within(page).getByRole("spinbutton", { name: "止损" })).toHaveAttribute("placeholder", "填跳数");
  expect(within(page).getByRole("spinbutton", { name: "止盈" })).toHaveAttribute("placeholder", "填跳数");
  fireEvent.click(within(page).getByRole("button", { name: "不设" }));
  expect(within(page).queryByRole("spinbutton", { name: "止盈" })).toBeNull();
  // The microphone beside 补一句 records into 补一句, not the chat.
  fireEvent.pointerDown(within(page).getByRole("button", { name: "按住说话，松开发送" }), { button: 0, clientY: 100 });
  expect(pressTo).toHaveBeenCalledTimes(1);
  expect(ptt.press).not.toHaveBeenCalled();
  // #523: the words come back after another draft opened (released, still being transcribed) — they never ask it.
  const deliver = pressTo.mock.calls[0][0] as (text: string) => void;
  const pressedDraft = editorState().draftId;
  command({ new: true });
  await waitFor(() => expect(editorState().draftId).not.toBe(pressedDraft));
  act(() => deliver("止损十五跳"));
  expect(editorState().ask).toBeNull();
});

test("预填：AI 标记与“依据 · N 笔”（disclosure strategy-editor-trades）；底部弹出面板改条件写回同一个草稿，面板与分段都能用命令开合", async () => {
  prefill = habits;
  render(<Host />);
  command({ new: true });
  const page = await screen.findByTestId("mobile-strategy-editor");
  expect(await within(page).findByText("AI 预填了 3 项")).toBeVisible();
  const basis = within(page).getByRole("button", { name: /依据 · 14 笔/ });
  act(() => uiSurface("disclosure:strategy-editor-trades")!.apply!(true));
  expect(basis).toHaveAttribute("aria-expanded", "true");
  expect(within(page).getByLabelText("预填依据的成交")).toHaveTextContent("SHFE.rb2701 多 1 手+300");
  // ② 开仓 by command: one section at a time.
  disclosure("strategy-editor-section", "entry", true);
  expect(within(page).getByRole("button", { name: "开仓" })).toHaveAttribute("aria-expanded", "true");
  expect(within(page).getByRole("button", { name: "品种与周期" })).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(within(page).getByRole("button", { name: "加一条" }));
  const sheet = screen.getByTestId("mobile-strategy-editor-sheet");
  fireEvent.click(within(sheet).getByRole("button", { name: "均线 MA" }));
  fireEvent.change(within(sheet).getByLabelText("左边 · N"), { target: { value: "5" } });
  fireEvent.click(within(sheet).getByRole("button", { name: /^比较：/ }));
  fireEvent.click(within(sheet).getByRole("button", { name: "上穿" }));
  fireEvent.click(within(sheet).getByRole("button", { name: "均线 MA" }));
  fireEvent.click(within(sheet).getByRole("button", { name: "好" }));
  expect(screen.queryByTestId("mobile-strategy-editor-sheet")).toBeNull();
  expect(editorState().form.entryLong.rows).toHaveLength(1);
  expect(uiSurface("strategyEditor")!.state).toMatchObject({ fields: { entryLong: { conditions: ["MA5 上穿 MA20"] } }, missing: [], canSave: true });
  // The sheet opens again by command on that row and 取消 keeps the row as it was.
  disclosure("strategy-editor-condition", "entryLong:1", true);
  fireEvent.click(within(screen.getByTestId("mobile-strategy-editor-sheet")).getByRole("button", { name: "收盘价" }));
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(uiSurface("strategyEditor")!.state).toMatchObject({ fields: { entryLong: { conditions: ["MA5 上穿 MA20"] } } });
  expect(within(page).getByRole("button", { name: "存为草稿" })).toBeEnabled();
  // The four figures open 回测明细 (the desktop's block, as a sheet); `strategy-editor-backtest` opens it too.
  fireEvent.click(within(page).getByRole("button", { name: "回测明细" }));
  expect(screen.getByTestId("mobile-strategy-editor-backtest")).toHaveTextContent("回测出来后这里列出逐笔。");
  expect(uiSurface("disclosure:strategy-editor-backtest")!.state).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "收起" }));
  expect(screen.queryByTestId("mobile-strategy-editor-backtest")).toBeNull();
  act(() => uiSurface("disclosure:strategy-editor-backtest")!.apply!(true));
  expect(screen.getByTestId("mobile-strategy-editor-backtest")).toBeVisible();
});

test("第二版命令在手机上同样 applied：选择器打开该条的底部面板并停在那一侧（分类是横向标签），focus 展开对应的段，“?”与策略流程块可用命令开合", async () => {
  render(<Host />);
  command({ new: true });
  const page = await screen.findByTestId("mobile-strategy-editor");
  await waitFor(() => expect(editorState().start).toBe("blank"));
  const applied = (change: Record<string, unknown>) => matches({ disclosures: disclosureStates(), strategyEditor: uiSurface("strategyEditor")!.state }, change);
  disclosure("strategy-editor-section", "entry", true);
  fireEvent.click(within(page).getByRole("button", { name: "加一条：均线金叉" }));
  disclosure("strategy-editor-picker", "entryLong:1:right", true);
  expect(applied({ disclosure: { name: "strategy-editor-picker", id: "entryLong:1:right", open: true } })).toBe(true);
  const sheet = screen.getByTestId("mobile-strategy-editor-sheet");
  expect(within(sheet).getByRole("button", { name: /^右边：/ })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(within(within(sheet).getByRole("group", { name: "分类" })).getByRole("button", { name: "趋势" }));
  fireEvent.click(within(sheet).getByRole("button", { name: "指数均线 EMA" }));
  fireEvent.click(within(sheet).getByRole("button", { name: "好" }));
  expect(uiSurface("strategyEditor")!.state).toMatchObject({ fields: { entryLong: { conditions: ["MA5 上穿 EMA20"] } } });
  // 流程图定位: the section the block sits in opens (保本 in ③'s 更多).
  command({ focus: "breakeven" });
  expect(applied({ strategyEditor: { focus: "breakeven" } })).toBe(true);
  expect(within(page).getByRole("button", { name: "平仓" })).toHaveAttribute("aria-expanded", "true");
  expect(within(page).getByRole("switch", { name: "保本" })).toBeVisible();
  disclosure("help-tip", "strategy-editor:section-exit", true);
  expect(applied({ disclosure: { name: "help-tip", id: "strategy-editor:section-exit", open: true } })).toBe(true);
  expect(screen.getByTestId("help-tip-strategy-editor:section-exit")).toHaveTextContent("哪个先到按哪个平");
  disclosure("strategy-editor-section", "flow", true);
  expect(within(page).getByTestId("strategy-flow")).toHaveTextContent("开多 · 1 条");
});

test("加一条后取消：草稿回到加之前（不留空条件挡住回测和保存）；条件没填完时“好”不可点", async () => {
  render(<Host />);
  command({ new: true });
  const page = await screen.findByTestId("mobile-strategy-editor");
  await waitFor(() => expect(editorState().start).toBe("blank"));
  disclosure("strategy-editor-section", "entry", true);
  const long = within(page).getByRole("group", { name: "做多条件" });
  fireEvent.click(within(long).getByRole("button", { name: "加一条" }));
  let sheet = screen.getByTestId("mobile-strategy-editor-sheet");
  expect(editorState().form.entryLong.rows).toHaveLength(1);
  expect(within(sheet).getByRole("button", { name: "好" })).toBeDisabled();
  fireEvent.click(within(sheet).getByRole("button", { name: "均线 MA" }));
  expect(within(sheet).getByRole("button", { name: "好" })).toBeDisabled();   // 比较 and 右边 still empty
  fireEvent.click(within(sheet).getByRole("button", { name: "取消" }));
  expect(screen.queryByTestId("mobile-strategy-editor-sheet")).toBeNull();
  expect(editorState().form.entryLong.rows).toHaveLength(0);
  expect(uiSurface("strategyEditor")!.state).toMatchObject({ missing: ["开仓条件", "止损"] });
  // The same by command: closing a just-added row's sheet puts the group back, 方向确认's forced 全部满足 included.
  fireEvent.click(within(long).getByRole("button", { name: "加一条" }));
  sheet = screen.getByTestId("mobile-strategy-editor-sheet");
  expect(sheet).toBeVisible();
  disclosure("strategy-editor-condition", "entryLong:1", false);
  expect(editorState().form.entryLong.rows).toHaveLength(0);
  // A picker command on the just-added row is another step of the same sheet: 取消 still takes the row out.
  fireEvent.click(within(long).getByRole("button", { name: "加一条" }));
  disclosure("strategy-editor-picker", "entryLong:1:left", true);
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(editorState().form.entryLong.rows).toHaveLength(0);
  // 取消 takes out only that row: a change made meanwhile to the same group (here a 复位 from 补一句) stays.
  fireEvent.click(within(long).getByRole("button", { name: "加一条" }));
  act(() => editFields(["entryLong"], form => ({ ...form, entryLong: { ...form.entryLong, rearm: quickRows("breakout", true) } })));
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(editorState().form.entryLong).toMatchObject({ rows: [], rearm: [expect.anything()] });
  // Moving the sheet by command to another row leaves no unconfirmed blank behind.
  fireEvent.click(within(long).getByRole("button", { name: "加一条" }));
  disclosure("strategy-editor-condition", "entryLong-rearm:1", true);
  expect(editorState().form.entryLong.rows).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(editorState().form.entryLong.rearm).toHaveLength(1);
  // 方向确认 forces 全部满足; 取消 puts 任一满足 back — but not over a 方向确认 that is still there.
  act(() => editFields(["entryLong"], form => ({ ...form, entryLong: { ...form.entryLong, rearm: [], join: "any" } })));
  fireEvent.click(within(long).getByRole("button", { name: "加一条做多方向确认" }));
  expect(editorState().form.entryLong.join).toBe("all");
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(editorState().form.entryLong).toMatchObject({ join: "any", alignment: [] });
  fireEvent.click(within(long).getByRole("button", { name: "加一条做多方向确认" }));
  act(() => editFields(["entryLong"], form => ({ ...form, entryLong: { ...form.entryLong, alignment: [...form.entryLong.alignment, ...quickRows("breakout", true)] } })));
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(editorState().form.entryLong).toMatchObject({ join: "all", alignment: [expect.anything()] });
});

test("刚由接口建的策略马上能编辑：列表里还没有就读一次策略列表，卡片打开到它；回执等卡片真的显示它才算", async () => {
  const form = emptyForm("SHFE.rb2701", 300);
  form.entryLong = { ...form.entryLong, rows: quickRows("breakout", true) };
  form.stop = { mode: "ticks", value: 15, risk: null };
  const made = { strategyId: "s-new", name: "刚建的", symbol: "SHFE.rb2701", version: 1, status: "draft",
    definition: { schemaVersion: 1, plan: { ...planFromForm(form), name: "刚建的" } } };
  let release: (value: unknown) => void = () => undefined;
  render(<Host />);
  await waitFor(() => expect(request).toHaveBeenCalledWith("http://main/api/strategy-plans"));
  request.mockImplementation(async (url: string) => {
    if (url.endsWith("/api/strategy-plans")) return new Promise(resolve => { release = resolve; });
    if (url.endsWith("/api/strategy-backtests")) return new Promise(() => undefined);
    return {};
  });
  const receipt = () => matches({ strategyEditor: uiSurface("strategyEditor")!.state }, { strategyEditor: { edit: "s-new" } });
  expect(() => command({ edit: "s-new" })).not.toThrow();
  expect(receipt()).toBe(false);
  await act(async () => { release({ items: [made], executions: [] }); });
  await screen.findByTestId("mobile-strategy-editor");
  expect(editorState()).toMatchObject({ open: true, mode: "edit", strategyId: "s-new" });
  expect(receipt()).toBe(true);
});

test("合起来的命令等卡片载入那条策略再动：edit+focus、edit+ask 不落到旧草稿；没这条或等待中改了新建，晚到的回答不覆盖", async () => {
  const form = emptyForm("SHFE.rb2701", 300);
  form.entryLong = { ...form.entryLong, rows: quickRows("breakout", true) };
  form.stop = { mode: "ticks", value: 15, risk: null };
  const made = { strategyId: "s-new", name: "刚建的", symbol: "SHFE.rb2701", version: 1, status: "draft",
    definition: { schemaVersion: 1, plan: { ...planFromForm(form), name: "刚建的" } } };
  const releases: ((value: unknown) => void)[] = [];
  const jobs = () => request.mock.calls.filter(([url, init]) => String(url).endsWith("/design-jobs") && (init as RequestInit | undefined)?.method === "POST");
  render(<Host />);
  await waitFor(() => expect(request).toHaveBeenCalledWith("http://main/api/strategy-plans"));
  request.mockImplementation(async (url: string) => {
    if (url.endsWith("/api/strategy-plans")) return new Promise(resolve => { releases.push(resolve); });
    if (url.endsWith("/api/strategy-plans/prefill")) return blank;
    if (url.endsWith("/api/strategy-backtests") || url.endsWith("/design-jobs")) return new Promise(() => undefined);
    return {};
  });
  // Closed card: edit + focus waits (no 先打开编辑卡), then focuses the loaded strategy.
  expect(() => command({ edit: "s-new", focus: "stop" })).not.toThrow();
  expect(editorState()).toMatchObject({ open: false, focus: null });
  await act(async () => { releases.shift()!({ items: [made], executions: [] }); });
  expect(editorState()).toMatchObject({ open: true, strategyId: "s-new", focus: { target: "stop" } });
  // A draft open: edit + ask does not send 补一句 for it; the sentence goes to the loaded strategy.
  command({ new: true });
  await waitFor(() => expect(editorState().start).toBe("blank"));
  command({ edit: "s-new", ask: "止损改 20 跳" });
  expect(editorState()).toMatchObject({ mode: "new", ask: null });
  expect(jobs()).toHaveLength(0);
  await act(async () => { releases.shift()!({ items: [made], executions: [] }); });
  expect(editorState()).toMatchObject({ mode: "edit", strategyId: "s-new", ask: { text: "止损改 20 跳" } });
  expect(jobs()).toHaveLength(1);
  // Not there: the open draft stays as it is and the rest does not run.
  command({ new: true });
  await waitFor(() => expect(editorState().start).toBe("blank"));
  command({ edit: "s-gone", focus: "stop" });
  await act(async () => { releases.shift()!({ items: [made], executions: [] }); });
  expect(editorState()).toMatchObject({ mode: "new", focus: null, error: "策略不存在" });
  // 新建 while it waits: the late answer neither replaces the new draft nor focuses it.
  command({ edit: "s-new", focus: "stop" });
  command({ new: true });
  const fresh = editorState().draftId;
  await act(async () => { releases.shift()!({ items: [made], executions: [] }); });
  expect(editorState()).toMatchObject({ mode: "new", draftId: fresh, strategyId: null, focus: null });
  // Leaving and reopening the same edited draft also cancels the older edit, even though open ends up true again.
  act(() => editFields(["name"], current => ({ ...current, name: "我刚改过" })));
  command({ edit: "s-new", focus: "stop" });
  act(() => { hideDraft(); openDraft(); });
  await act(async () => { releases.shift()!({ items: [made], executions: [] }); });
  expect(editorState()).toMatchObject({ mode: "new", draftId: fresh, strategyId: null, form: { name: "我刚改过" } });
});


test("手机保存完成保留期间新修改，不跳走也不触发保存版本回测", async () => {
  render(<Host />);
  await waitFor(() => expect(uiSurface("strategyEditor")).toBeDefined());
  const form = emptyForm("SHFE.rb2701", 300);
  form.entryLong = { ...form.entryLong, rows: quickRows("breakout", true) };
  form.stop = { ...form.stop, value: 20 };
  const plan = planFromForm(form);
  act(() => editDraft({ strategyId: "saved", definition: { plan } } as Parameters<typeof editDraft>[0], "SHFE.rb2701"));
  let finish!: (value: unknown) => void;
  const fallback = request.getMockImplementation()!;
  request.mockImplementation((url: string, init?: RequestInit) => url.endsWith("/api/strategy-plans") && init?.method === "POST"
    ? new Promise(resolve => { finish = resolve; }) : fallback(url, init));
  fireEvent.click(screen.getByRole("button", { name: "存为草稿" }));
  fireEvent.change(screen.getByRole("textbox", { name: "策略名称" }), { target: { value: "保存后又起的名字" } });
  await act(async () => { finish({ strategyId: "version2", definition: { plan } }); });
  expect(screen.getByTestId("mobile-strategy-editor")).toBeVisible();
  expect(editorState()).toMatchObject({ saving: false, form: { name: "保存后又起的名字" } });
  expect(request.mock.calls.some(([url]) => String(url).includes("/version2/backtest"))).toBe(false);
});

test("提醒模式（手机）：只有品种、什么时候提醒、提醒时段；开始提醒存成只提醒并按只提醒开始，回到我的提醒", async () => {
  request.mockImplementation(async (url: string, init?: { body?: string }) => {
    if (url.endsWith("/api/strategy-plans/prefill")) return prefill;
    if (url.endsWith("/api/strategy-plans") && init?.body) return { strategyId: "r-1", name: "放量提醒", definition: { plan: JSON.parse(init.body).plan } };
    if (url.endsWith("/api/strategy-plans")) return { items: [], executions: [] };
    if (url.endsWith("/validate")) return { strategyId: "run-1" };
    if (url.endsWith("/api/strategy-backtests")) return new Promise(() => undefined);
    return {};
  });
  render(<Host />);
  command({ new: true, reminder: true });
  const page = await screen.findByTestId("mobile-strategy-editor");
  expect(within(page).getByRole("button", { name: "返回我的提醒" })).toBeVisible();
  expect(within(page).getByRole("button", { name: "什么时候提醒" })).toBeVisible();
  expect(within(page).getByRole("button", { name: "提醒时段与响几次" })).toBeVisible();
  expect(within(page).queryByRole("button", { name: "怎么平仓" })).toBeNull();
  expect(within(page).queryByRole("button", { name: "上线运行…" })).toBeNull();
  expect(within(page).getByText(/还差：提醒条件/)).toBeVisible();
  expect(within(page).getByText("还没有提醒条件")).toBeVisible();
  act(() => editFields(["entryLong", "name"], form => ({ ...form, name: "放量提醒", entryLong: { ...form.entryLong, rows: quickRows("breakout", true) } })));
  fireEvent.click(within(page).getByRole("button", { name: "开始提醒" }));
  await waitFor(() => expect(showReminders).toHaveBeenCalled());
  const saved = request.mock.calls.find(([url, init]) => String(url).endsWith("/api/strategy-plans") && init?.body);
  expect(JSON.parse(saved![1].body).plan.reminderOnly).toBe(true);
  const started = request.mock.calls.find(([url]) => String(url).endsWith("/monitor/start"));
  expect(JSON.parse(started![1].body).signalAction).toBe("alert");
  expect(editorState().open).toBe(false);
});

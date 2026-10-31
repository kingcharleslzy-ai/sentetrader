import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, expect, test, vi } from "vitest";
import type { ChartDrawing } from "../../chartDrawingModel";
import type { ChartDrawingsHandle } from "../../hooks/useChartDrawings";
import { disclosureStates, resetUiSurfaces, uiSurface, uiSurfaceStates } from "../../uiSurfaces";
import { ChartStage } from "./ChartStage";

const mocks = vi.hoisted(() => ({ chart: vi.fn(), timeshare: vi.fn(), annotations: vi.fn(() => []), marks: vi.fn(() => []) }));
vi.mock("../TradingCharts", () => ({ TradingCharts: (props: unknown) => { mocks.chart(props); return null; } }));
vi.mock("../TimeshareChart", () => ({ TimeshareChart: (props: unknown) => { mocks.timeshare(props); return null; } }));
vi.mock("../../aiAnnotations", () => ({ useChartAnnotations: mocks.annotations, useBacktestMarks: mocks.marks, backtestMarksFor: vi.fn(() => []), applyChartAnnotations: vi.fn(), clearChartMarks: vi.fn(), fillVisibleAfterClear: () => true }));

type ChartProps = { onPriceContext: (price: number, x: number, y: number, drawing?: ChartDrawing) => void; onPaneContext: (type: string, x: number, y: number) => void;
  drawingTool: string | null; userDrawings: ChartDrawing[]; onDrawingSelect: (id: string | null) => void; selectedDrawingId: string | null;
  onPlaceDrawing: (kind: string, anchors: Array<{ t: number | null; p: number }>) => void };
const toolbar = () => screen.queryByRole("navigation", { name: "画线工具栏" });
const lastChart = () => mocks.chart.mock.calls.at(-1)![0] as ChartProps;

afterEach(() => { resetUiSurfaces(); vi.clearAllMocks(); localStorage.clear(); });

const line = (over: Partial<ChartDrawing> = {}): ChartDrawing => ({
  id: "d1", symbol: "SHFE.rb2610", kind: "trendline", points: { t1: 1790000000, p1: 3000, t2: 1790003600, p2: 3020 }, label: "",
  createdAt: "", updatedAt: "", ...over,
});

function handle(items: ChartDrawing[]): ChartDrawingsHandle {
  return { available: true, items, createError: "", notice: "", create: vi.fn(async () => null), update: vi.fn(async () => true),
    remove: vi.fn(), clear: vi.fn(), refresh: vi.fn() };
}

function baseProps(over: Record<string, unknown> = {}) {
  return {
    workspace: { access: { require: () => true, reason: () => null }, simulationEnabled: true, aiExecution: { stops: [] } },
    market: { quote: { symbol: "SHFE.rb2610", lastPrice: 3010, priceTick: 1 }, bars: [{ datetime: 1_790_003_600_000_000_000 }], durationSeconds: 300 },
    chartView: "kline", decimals: 0, chartSettings: {}, setChartSettings: vi.fn(), chartPreferences: { subPaneList: ["kd", "macd"] }, annotationsVisible: true,
    resolvedTheme: "light", appWorkbench: { config: { showTradeMarkers: false, extensions: [] }, activity: { fills: [] } },
    focus: { selectedTrades: [], setSelectedTrades: vi.fn(), setChartFocusMessage: vi.fn(), clearChartFocus: vi.fn() },
    selectedPrice: undefined, setSelectedPrice: vi.fn(), onSelectDuration: vi.fn(), onSelectChartView: vi.fn(),
    onChartIndicatorsChange: vi.fn(), openFills: vi.fn(), openChat: vi.fn(), setPrompt: vi.fn(),
    alerts: { active: [], create: vi.fn(async () => true) },
    ...over,
  } as unknown as ComponentProps<typeof ChartStage>;
}

test("guest charts never read private overlays; the menu has no order items and blocked AI items never execute", () => {
  const requireAccess = vi.fn(() => false), openChat = vi.fn();
  const props = baseProps({ workspace: { guest: {}, access: { require: requireAccess, reason: () => "登录后可使用此功能" }, simulationEnabled: false },
    appWorkbench: { config: { showTradeMarkers: true, extensions: [{ name: "private" }] }, activity: { fills: [{ tradeId: "private" }] } },
    focus: { selectedTrades: ["private"], tradeFocus: { direction: "long" }, setSelectedTrades: vi.fn(), setChartFocusMessage: vi.fn(), clearChartFocus: vi.fn() },
    alerts: undefined, openChat });
  render(<ChartStage {...props} />);
  expect(mocks.annotations).not.toHaveBeenCalled();
  expect(mocks.marks).not.toHaveBeenCalled();
  expect(mocks.chart).toHaveBeenCalledWith(expect.objectContaining({ tradeFills: [], extensions: [], annotations: [], backtestMarks: [], selectedTradeIds: [], tradeFocus: undefined, userDrawings: [] }));
  expect(screen.queryByRole("navigation", { name: "画线工具栏" })).toBeNull();
  for (const label of ["让 AI 分析这个价", "让 AI 讲讲这一段"]) {
    act(() => lastChart().onPriceContext(3000, 40, 40));
    const menu = screen.getByRole("menu", { name: "图表菜单" });
    for (const gone of ["买入开仓", "卖出开仓", "仅填入草稿价", "图表与下单说明", "画线"]) expect(within(menu).queryByRole("menuitem", { name: gone })).toBeNull();
    fireEvent.click(within(menu).getByRole("menuitem", { name: label }));
  }
  expect(requireAccess).toHaveBeenCalledTimes(2);
  expect(openChat).not.toHaveBeenCalled();
});

test("a guest's K-line chart carries the plan's 最近 N 个交易日 basis; a member's carries none (#734)", () => {
  // The limit comes from the market frame (the bars already cut to it), not the session read at entry.
  render(<ChartStage {...baseProps({ workspace: { guest: { session: { marketHistoryTradingDays: 3 } }, access: { require: () => false, reason: () => null }, simulationEnabled: false },
    market: { quote: { symbol: "SHFE.rb2610", lastPrice: 3010, priceTick: 1 }, bars: [{ datetime: 1_790_003_600_000_000_000 }], durationSeconds: 300, historyTradingDays: 5 } })} />);
  expect(mocks.chart).toHaveBeenLastCalledWith(expect.objectContaining({ historyRestricted: true, historyNote: "游客仅显示最近 5 个交易日 · 登录看完整历史" }));
  render(<ChartStage {...baseProps()} />);
  expect(mocks.chart).toHaveBeenLastCalledWith(expect.objectContaining({ historyRestricted: false, historyNote: undefined }));
});

test("three right-click menus by where the click lands: main area, a drawing, a sub pane", () => {
  const drawings = handle([line()]);
  const props = baseProps({ drawings });
  render(<ChartStage {...props} />);
  // 主图空白处: 画线 ▸ arms a tool; 周期 ▸ switches the chart.
  act(() => lastChart().onPriceContext(3005, 40, 40));
  let menu = screen.getByRole("menu", { name: "图表菜单" });
  expect(menu).toHaveTextContent("主图 · 3,005（光标价）");
  // The pointer always hovers 画线 before clicking it: the hover opens the submenu and the click keeps it open.
  fireEvent.pointerEnter(within(menu).getByRole("menuitem", { name: "画线" }));
  fireEvent.click(within(menu).getByRole("menuitem", { name: "画线" }));
  expect(within(menu).getByRole("menuitem", { name: "画线" })).toHaveAttribute("aria-expanded", "true");
  expect(toolbar()).toBeNull();   // hidden until a tool is armed (owner 2026-10-14)
  fireEvent.click(within(screen.getByRole("menu", { name: "画线（点一个就开始画）" })).getByRole("menuitem", { name: "黄金分割" }));
  expect(lastChart().drawingTool).toBe("fib");
  expect(toolbar()).toBeInTheDocument();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(lastChart().drawingTool).toBeNull();
  expect(toolbar()).toBeNull();
  act(() => lastChart().onPriceContext(3005, 40, 40));
  fireEvent.click(within(screen.getByRole("menu", { name: "图表菜单" })).getByRole("menuitem", { name: /^周期/ }));
  fireEvent.click(within(screen.getByRole("menu", { name: "周期" })).getByRole("menuitemradio", { name: "15 分" }));
  expect(props.onSelectDuration).toHaveBeenCalledWith(900);
  // 一条线上: only that line's actions; 删除 is the danger item.
  act(() => lastChart().onPriceContext(3010, 40, 40, line()));
  menu = screen.getByRole("menu", { name: "图表菜单" });
  expect(menu).toHaveTextContent(/^趋势线 · /);
  for (const item of ["颜色 / 粗细 / 线型", "精确设置…", "这条线被碰到时提醒我", "复制一条", "锁定", "删除这条线"]) expect(within(menu).getByRole("menuitem", { name: item })).toBeInTheDocument();
  expect(within(menu).queryByRole("menuitem", { name: /周期|开仓/ })).toBeNull();
  fireEvent.click(within(menu).getByRole("menuitem", { name: "这条线被碰到时提醒我" }));
  // The alert watches the drawing itself: the Runtime takes the price from the line and follows it.
  expect((props.alerts as unknown as { create: ReturnType<typeof vi.fn> }).create).toHaveBeenCalledWith("SHFE.rb2610", 0, undefined, "d1");
  act(() => lastChart().onPriceContext(3010, 40, 40, line()));
  fireEvent.click(within(screen.getByRole("menu", { name: "图表菜单" })).getByRole("menuitem", { name: "删除这条线" }));
  expect(drawings.remove).toHaveBeenCalledWith("d1");
  // 副图上: 换成 / 参数设置 / 上移下移 / 添加副图 / 关闭.
  act(() => lastChart().onPaneContext("macd", 40, 300));
  menu = screen.getByRole("menu", { name: "图表菜单" });
  expect(within(menu).getByRole("menuitem", { name: "下移" })).toBeDisabled();
  fireEvent.click(within(menu).getByRole("menuitem", { name: "上移" }));
  expect(props.onChartIndicatorsChange).toHaveBeenLastCalledWith({ subPaneList: ["macd", "kd"] });
  act(() => lastChart().onPaneContext("macd", 40, 300));
  fireEvent.click(within(screen.getByRole("menu", { name: "图表菜单" })).getByRole("menuitem", { name: "关闭这个副图" }));
  expect(props.onChartIndicatorsChange).toHaveBeenLastCalledWith({ subPaneList: ["kd"] });
});

test("the 分时图's main menu has no indicators or panes", () => {
  render(<ChartStage {...baseProps({ chartView: "timeshare", drawings: handle([]) })} />);
  const chart = mocks.timeshare.mock.calls.at(-1)![0] as ChartProps;
  act(() => chart.onPriceContext(3005, 40, 40));
  const menu = screen.getByRole("menu", { name: "图表菜单" });
  expect(within(menu).getByRole("menuitem", { name: /^周期/ })).toHaveTextContent("分时");
  expect(within(menu).queryByRole("menuitem", { name: /^主图指标/ })).toBeNull();
  expect(within(menu).queryByRole("menuitem", { name: /^添加副图/ })).toBeNull();
});

test("the toolbar, the style bar, 精确设置 and the menus answer UI commands with the state the snapshot reports", () => {
  const drawings = handle([line(), line({ id: "d2", kind: "hline", points: { price: 3050 }, style: { locked: true } })]);
  const props = baseProps({ drawings });
  render(<ChartStage {...props} />);
  const family = (name: string) => uiSurface(`family:${name}`)!.apply!;
  act(() => family("chart-drawing-tool")({ id: "fib", open: true }));
  expect(lastChart().drawingTool).toBe("fib");
  expect(disclosureStates()).toMatchObject({ "chart-drawing-tool": true, "chart-drawing-tool:fib": true });
  expect(disclosureStates()["chart-drawing-toolbar"]).toBe(true);   // the toolbar shows with the armed tool
  act(() => family("chart-drawing-tool")({ id: "fib", open: false }));
  expect(disclosureStates()["chart-drawing-tool:fib"]).toBeUndefined();   // reported only while open
  expect(disclosureStates()["chart-drawing-toolbar"]).toBe(false);
  expect(() => family("chart-drawing-tool")({ id: "pencil", open: true })).toThrow("没有这种画线工具");
  act(() => family("chart-drawing-group")({ id: "shapes", open: true }));
  expect(screen.getByRole("menu", { name: "形状工具" })).toBeInTheDocument();
  act(() => family("chart-drawing-style")({ id: "d1", open: true }));
  expect(screen.getByRole("toolbar", { name: "趋势线的样式" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("radio", { name: "颜色：红" }));
  expect(drawings.update).toHaveBeenLastCalledWith("d1", { style: { color: "red" } });
  expect(() => family("chart-drawing-style")({ id: "nope", open: true })).toThrow("本合约没有这条线");
  act(() => family("chart-context-menu")({ id: "pane:macd/swap", open: true }));
  expect(screen.getByRole("menu", { name: "换成" })).toBeInTheDocument();
  expect(disclosureStates()["chart-context-menu:pane:macd/swap"]).toBe(true);
  expect(() => family("chart-context-menu")({ id: "pane:rsi", open: true })).toThrow("这个副图没有打开");
  act(() => family("chart-context-menu")({ id: "main", open: false }));
  expect(screen.queryByRole("menu", { name: "图表菜单" })).toBeNull();
  // The snapshot report: toolbar, selection and this contract's drawings by id.
  expect(uiSurfaceStates().chartDrawings).toMatchObject({ symbol: "SHFE.rb2610", count: 2, toolbar: "shown", selected: "d1", locked: false,
    items: [{ id: "d1", kind: "trendline", locked: false }, { id: "d2", kind: "hline", locked: true }] });
  // 全部删除 asks once more, then deletes this contract's drawings only.
  act(() => uiSurface("disclosure:chart-drawing-clear")!.apply!(true));
  fireEvent.click(within(screen.getByRole("dialog", { name: "确认删除全部画线" })).getByRole("button", { name: "删除 2 条" }));
  expect(drawings.clear).toHaveBeenCalledWith("SHFE.rb2610");
  expect(toolbar()).toBeNull();   // no tool armed: the toolbar goes with the confirmation
  // 磁吸 / 全部锁定 / 全部隐藏 are account preferences.
  act(() => uiSurface("disclosure:chart-drawing-lock-all")!.apply!(true));
  expect(props.onChartIndicatorsChange).toHaveBeenLastCalledWith({ drawingsLocked: true });
});

test("the toolbar shows only while drawing: shortcuts arm tools with it, the crosshair (Esc, a finished line, ✕, a click elsewhere) hides it", () => {
  render(<ChartStage {...baseProps({ drawings: handle([]) })} />);
  expect(toolbar()).toBeNull();
  for (const [code, kind] of [["KeyT", "trendline"], ["KeyH", "hline"], ["KeyV", "vline"]]) {
    fireEvent.keyDown(window, { key: "x", code, altKey: true });
    expect(lastChart().drawingTool).toBe(kind);
    expect(toolbar()).toBeInTheDocument();
  }
  // Another tool from the toolbar while drawing; the 线条 button shows the group's last tool (垂直线 now).
  expect(within(toolbar()!).getByRole("button", { name: "垂直线（线条）" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(within(toolbar()!).getByRole("button", { name: "黄金分割（分割）" }));
  expect(lastChart().drawingTool).toBe("fib");
  fireEvent.keyDown(window, { key: "Escape" });
  expect(lastChart().drawingTool).toBeNull();
  expect(toolbar()).toBeNull();
  // A finished line is back at the crosshair, toolbar gone; the line itself is the Runtime's (create).
  fireEvent.keyDown(window, { key: "x", code: "KeyH", altKey: true });
  act(() => lastChart().onPlaceDrawing("hline", [{ t: null, p: 3000 }]));
  expect(lastChart().drawingTool).toBeNull();
  expect(toolbar()).toBeNull();
  // ✕ and the UI command; opened with no tool armed, a click elsewhere hides it again.
  fireEvent.keyDown(window, { key: "x", code: "KeyV", altKey: true });
  fireEvent.click(within(toolbar()!).getByRole("button", { name: "收起画线工具栏" }));
  expect(lastChart().drawingTool).toBeNull();
  expect(toolbar()).toBeNull();
  act(() => uiSurface("disclosure:chart-drawing-toolbar")!.apply!(true));
  expect(uiSurfaceStates().chartDrawings).toMatchObject({ toolbar: "shown", tool: null });
  fireEvent.pointerDown(document.body);
  expect(toolbar()).toBeNull();
  expect(uiSurfaceStates().chartDrawings).toMatchObject({ toolbar: "hidden" });
  expect(localStorage.getItem("ai-trader:drawing-toolbar-collapsed")).toBeNull();
});

test("right-click 画线 ▸ also lists the groups, 磁吸 / 全部锁定 / 全部隐藏 and 全部删除 (asking once more)", () => {
  const drawings = handle([line()]);
  const props = baseProps({ drawings, chartPreferences: { subPaneList: ["kd"], drawingMagnet: true } });
  render(<ChartStage {...props} />);
  const drawMenu = () => {
    act(() => lastChart().onPriceContext(3005, 40, 40));
    fireEvent.click(within(screen.getByRole("menu", { name: "图表菜单" })).getByRole("menuitem", { name: "画线" }));
    return screen.getByRole("menu", { name: "画线（点一个就开始画）" });
  };
  let sub = drawMenu();
  expect(within(sub).getByRole("menuitemcheckbox", { name: "磁吸" })).toHaveAttribute("aria-checked", "true");
  expect(within(sub).getByRole("menuitemcheckbox", { name: "全部锁定" })).toHaveAttribute("aria-checked", "false");
  // The main menu no longer repeats 隐藏全部画线: it lives here.
  expect(within(screen.getByRole("menu", { name: "图表菜单" })).queryByRole("menuitem", { name: /隐藏全部画线|显示全部画线/ })).toBeNull();
  fireEvent.click(within(sub).getByRole("menuitemcheckbox", { name: "全部隐藏" }));
  expect(props.onChartIndicatorsChange).toHaveBeenLastCalledWith({ drawingsHidden: true });
  // A group: the toolbar shows with that group's list.
  sub = drawMenu();
  fireEvent.click(within(sub).getByRole("menuitem", { name: "形状" }));
  expect(within(toolbar()!).getByRole("menu", { name: "形状工具" })).toBeInTheDocument();
  fireEvent.pointerDown(document.body);
  expect(toolbar()).toBeNull();
  // 全部删除 (danger) opens the toolbar's second confirmation; 取消 hides both.
  sub = drawMenu();
  const clear = within(sub).getByRole("menuitem", { name: "全部删除（1 条）…" });
  expect(clear).toHaveClass("danger");
  fireEvent.click(clear);
  fireEvent.click(within(screen.getByRole("dialog", { name: "确认删除全部画线" })).getByRole("button", { name: "取消" }));
  expect(drawings.clear).not.toHaveBeenCalled();
  expect(toolbar()).toBeNull();
});

test("精确设置 rounds typed prices to the tick, saves the style flags and, when asked, the tool's default style", async () => {
  const drawings = handle([line({ style: { color: "amber" } }), line({ id: "locked", style: { locked: true } })]);
  const props = baseProps({ drawings, market: { quote: { symbol: "SHFE.rb2610", lastPrice: 3010, priceTick: 2 }, bars: [], durationSeconds: 300 } });
  render(<ChartStage {...props} />);
  act(() => uiSurface("family:chart-drawing-settings")!.apply!({ id: "d1", open: true }));
  const dialog = screen.getByRole("dialog", { name: /^精确设置 · 趋势线/ });
  fireEvent.change(within(dialog).getByLabelText("终点价格"), { target: { value: "3031.3" } });
  fireEvent.click(within(dialog).getByRole("switch", { name: "在所有周期显示" }));
  fireEvent.click(within(dialog).getByLabelText("存为这类线的默认样式"));
  await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: "确定" })); });
  expect(drawings.update).toHaveBeenCalledWith("d1", {
    points: { t1: 1790000000, p1: 3000, t2: 1790003600, p2: 3032 }, label: "",
    // A line without a period (first version) is pinned to the chart shown, or 在所有周期显示 off would change nothing.
    style: { extendLeft: false, extendRight: false, showPrice: true, allPeriods: false, period: 300 } });
  expect(props.onChartIndicatorsChange).toHaveBeenCalledWith({ drawingDefaults: { trendline: {
    color: "amber", width: null, dash: null, extendLeft: false, extendRight: false, showPrice: true, allPeriods: false } } });
  // A locked line keeps its place: the anchor fields are read-only and say why.
  act(() => uiSurface("family:chart-drawing-settings")!.apply!({ id: "locked", open: true }));
  const locked = screen.getByRole("dialog", { name: /^精确设置/ });
  expect(within(locked).getByLabelText("起点价格")).toBeDisabled();
  expect(locked).toHaveTextContent("已锁定，解锁后才能改位置");
});

test("a hidden line loses its selection, so neither the style bar nor Delete acts on it unseen; 粗细 shows the width drawn", () => {
  const drawings = handle([line()]);
  const props = baseProps({ drawings });
  const { rerender } = render(<ChartStage {...props} />);
  act(() => uiSurface("family:chart-drawing-style")!.apply!({ id: "d1", open: true }));
  // A trendline draws at 1.5 until a width is chosen: that is the checked one, and choosing it again writes the default back.
  expect(screen.getByRole("radio", { name: "粗细 1.5" })).toHaveAttribute("aria-checked", "true");
  fireEvent.click(screen.getByRole("radio", { name: "粗细 2" }));
  expect(drawings.update).toHaveBeenLastCalledWith("d1", { style: { width: 2 } });
  fireEvent.click(screen.getByRole("radio", { name: "粗细 1.5" }));
  expect(drawings.update).toHaveBeenLastCalledWith("d1", { style: { width: null } });
  // 全部隐藏: the bar goes, the snapshot reports no selection, Delete removes nothing.
  rerender(<ChartStage {...baseProps({ drawings, chartPreferences: { subPaneList: ["kd", "macd"], drawingsHidden: true } })} />);
  expect(screen.queryByRole("toolbar", { name: "趋势线的样式" })).toBeNull();
  expect(uiSurfaceStates().chartDrawings).toMatchObject({ selected: null, hidden: true });
  fireEvent.keyDown(window, { key: "Delete" });
  expect(drawings.remove).not.toHaveBeenCalled();
  // Shown again it is not silently re-selected; a line hidden on its own is the same.
  rerender(<ChartStage {...props} />);
  expect(uiSurfaceStates().chartDrawings).toMatchObject({ selected: null });
  act(() => uiSurface("family:chart-drawing-style")!.apply!({ id: "d1", open: true }));
  const hiddenOne = handle([line({ style: { hidden: true } })]);
  rerender(<ChartStage {...baseProps({ drawings: hiddenOne })} />);
  expect(screen.queryByRole("toolbar", { name: "趋势线的样式" })).toBeNull();
  fireEvent.keyDown(window, { key: "Delete" });
  expect(hiddenOne.remove).not.toHaveBeenCalled();
  // A line pinned to another period is not on this chart: the command says so instead of selecting it unseen.
  rerender(<ChartStage {...baseProps({ drawings: handle([line({ style: { allPeriods: false, period: 900 } })]) })} />);
  expect(() => uiSurface("family:chart-drawing-style")!.apply!({ id: "d1", open: true })).toThrow("只在画它的那个周期显示");
});

test("图上拖线改价: the user's own stop line drags into a confirm card that reprices it; a strategy's or a level past the market never asks", async () => {
  const reprice = vi.fn(async () => true);
  const stop = (over: Record<string, unknown>) => ({ stopId: "s1", symbol: "SHFE.rb2610", positionDirection: "long", volume: 2, triggerPrice: 2990,
    takeProfitPrice: 3050, source: "manual", status: "ARMED", priceIntent: "aggressive", ...over });
  const props = baseProps({ workspace: { access: { require: () => true, reason: () => null }, simulationEnabled: true, onRepricePositionStop: reprice,
    aiExecution: { stops: [stop({}), stop({ stopId: "s2", source: "strategy", triggerPrice: 2980, takeProfitPrice: null })] },
    account: { positions: [{ symbol: "SHFE.rb2610", direction: "long", volume: 2, openPrice: 3000, unrealizedPnl: 200 }] } },
    market: { quote: { symbol: "SHFE.rb2610", lastPrice: 3010, priceTick: 1, volumeMultiple: 10 }, bars: [{ datetime: 1_790_003_600_000_000_000 }], durationSeconds: 300 } });
  render(<ChartStage {...props} />);
  type Drag = { protectionLines: Array<{ id: string; draggable?: boolean; pending?: boolean; price: number }>; onProtectionDrag: (id: string, price: number | null, done: boolean) => void };
  const chart = () => mocks.chart.mock.calls.at(-1)![0] as Drag;
  expect(chart().protectionLines.map(item => [item.id, item.draggable])).toEqual([["s1:stop", true], ["s1:take", true], ["s2:stop", false]]);

  // Past the market (long: a stop at/above 3010 fires at once): the hint warns, the lift asks nothing.
  act(() => chart().onProtectionDrag("s1:stop", 3012, false));
  expect(screen.getByRole("status")).toHaveTextContent("止损要低于现价 3,010，松手会弹回");
  act(() => chart().onProtectionDrag("s1:stop", 3012, true));
  expect(screen.getByTestId("protection-reprice-dialog")).not.toHaveAttribute("open");

  act(() => chart().onProtectionDrag("s1:stop", 2995, false));
  // 触发时盈亏 against the open price: (2995 − 3000) × 10 × 2 lots.
  expect(screen.getByRole("status")).toHaveTextContent("止损 2,990 → 2,995 · 触发约 -100 元");
  act(() => chart().onProtectionDrag("s1:stop", 2995, true));
  const dialog = screen.getByTestId("protection-reprice-dialog");
  expect(dialog).toHaveAttribute("open");
  expect(dialog).toHaveTextContent("2,990 → 2,995");
  expect(dialog).toHaveTextContent("2 手");
  expect(dialog).toHaveTextContent("触发时盈亏约 -100 元（按开仓均价）");
  // Until 确认修改 the old line holds; the new level shows as its own pending line.
  expect(chart().protectionLines.find(item => item.pending)).toMatchObject({ price: 2995 });
  await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: "确认修改" })); });
  expect(reprice).toHaveBeenCalledWith("s1", { level: "stop", previous: 2990, price: 2995 });
  expect(chart().protectionLines.some(item => item.pending)).toBe(false);

  // A cancelled drag (Esc, or a lift where it started) changes nothing.
  act(() => chart().onProtectionDrag("s1:take", null, true));
  expect(reprice).toHaveBeenCalledTimes(1);
});

test("在这里设提醒 ▸（第 6 批）：到价提醒按光标价；按打法 / 按条件打开提醒模式的编辑卡；打法本在菜单打开时才读", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ playbooks: [{ id: "p1", name: "开盘突破", entryRules: "突破开盘区间高点", exitRules: "", stopRules: "" }], max: 20 }))));
  const onReminder = vi.fn();
  const props = baseProps({ onReminder, workspace: { access: { require: () => true, reason: () => null }, simulationEnabled: true, aiExecution: { stops: [] }, runtimeBaseUrl: "https://test" } });
  render(<ChartStage {...props} />);
  expect(fetch).not.toHaveBeenCalled();
  act(() => lastChart().onPriceContext(3005, 40, 40));
  const menu = screen.getByRole("menu", { name: "图表菜单" });
  fireEvent.pointerEnter(within(menu).getByRole("menuitem", { name: /^在这里设提醒/ }));
  const sub = screen.getByRole("menu", { name: "在这里设提醒" });
  expect(await within(sub).findByRole("menuitem", { name: "按交易笔记：开盘突破" })).toBeInTheDocument();
  fireEvent.click(within(sub).getByRole("menuitem", { name: "按交易笔记：开盘突破" }));
  expect(onReminder).toHaveBeenCalledWith(expect.objectContaining({ name: "开盘突破", entryRules: "突破开盘区间高点" }));
  act(() => lastChart().onPriceContext(3005, 40, 40));
  fireEvent.pointerEnter(within(screen.getByRole("menu", { name: "图表菜单" })).getByRole("menuitem", { name: /^在这里设提醒/ }));
  fireEvent.click(within(screen.getByRole("menu", { name: "在这里设提醒" })).getByRole("menuitem", { name: /^价格到 .+ 时提醒我$/ }));
  expect((props as unknown as { alerts: { create: ReturnType<typeof vi.fn> } }).alerts.create).toHaveBeenCalledWith("SHFE.rb2610", 3005);
  vi.unstubAllGlobals();
});

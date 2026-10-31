import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { placementPoints, type ChartDrawing } from "../../chartDrawingModel";
import type { ChartDrawingsHandle } from "../../hooks/useChartDrawings";
import { disclosureStates, resetUiSurfaces, uiSurface, uiSurfaceStates } from "../../uiSurfaces";
import { normalizeChartIndicators } from "../chartIndicators";
import { DEFAULT_MOBILE_MARKET } from "./mobileMarketPrefs";
import { MobileMarket } from "./MobileMarket";

const mocks = vi.hoisted(() => ({ chart: vi.fn(), timeshare: vi.fn() }));
vi.mock("../TradingCharts", () => ({ TradingCharts: (props: unknown) => { mocks.chart(props); return <section data-region="chart.main" />; } }));
vi.mock("../TimeshareChart", () => ({ TimeshareChart: (props: unknown) => { mocks.timeshare(props); return <section data-region="chart.timeshare"><div data-testid="timeshare-canvas" /></section>; } }));

type ChartProps = { drawingTool: string | null; userDrawings: ChartDrawing[]; selectedDrawingId: string | null; selectedDrawingHandle: number | null;
  drawingFinish: number; onDrawingSelect: (id: string | null, at?: unknown, handle?: number | null) => void; onDrawingDraft: (count: number) => void;
  onPlaceDrawing: (kind: string, anchors: Array<{ t: number; p: number }>) => void };
const lastChart = () => mocks.chart.mock.calls.at(-1)![0] as ChartProps;

afterEach(() => { resetUiSurfaces(); vi.clearAllMocks(); localStorage.clear(); });

const T1 = 1_790_000_000, T2 = 1_790_003_600;
const trend = (over: Partial<ChartDrawing> = {}): ChartDrawing => ({
  id: "d1", symbol: "SHFE.rb2701", kind: "trendline", points: { t1: T1, p1: 3000, t2: T2, p2: 3020 }, label: "", createdAt: "", updatedAt: "", ...over,
});

function handle(items: ChartDrawing[]): ChartDrawingsHandle {
  return { available: true, items, createError: "", notice: "", create: vi.fn(async () => null), update: vi.fn(async () => true),
    remove: vi.fn(), clear: vi.fn(), refresh: vi.fn() };
}

function props(over: Record<string, unknown> = {}) {
  return {
    market: { quote: { symbol: "SHFE.rb2701", lastPrice: 3076, priceTick: 1, changePercent: 0 }, bars: [{ datetime: T2 * 1e9, open: 1, high: 1, low: 1, close: 1 }], durationSeconds: 300 },
    name: "螺纹钢2701", decimals: 0, chartView: "kline", onSelectDuration: vi.fn(), onSelectChartView: vi.fn(), onOpenContracts: vi.fn(), onSelectSymbol: vi.fn(),
    chartSettings: {}, setChartSettings: vi.fn(), chartPreferences: {}, resolvedTheme: "light", positions: [], watchlist: [], board: undefined, instruments: [],
    onAskBar: vi.fn(), onOrder: vi.fn(),
    mobileMarket: { market: DEFAULT_MOBILE_MARKET, update: vi.fn(), sheetOpen: false, setSheetOpen: vi.fn(), apply: vi.fn() },
    indicators: normalizeChartIndicators({}), onIndicatorsChange: vi.fn(), onOpenChart: vi.fn(),
    ...over,
  } as unknown as ComponentProps<typeof MobileMarket>;
}

test("手机 K 线和电脑同一张图：游客的“最近 N 个交易日”依据照样传给图表（#734）", () => {
  render(<MobileMarket {...props({ guest: true, historyNote: "游客仅显示最近 3 个交易日 · 登录看完整历史" })} />);
  expect(mocks.chart).toHaveBeenLastCalledWith(expect.objectContaining({ historyNote: "游客仅显示最近 3 个交易日 · 登录看完整历史" }));
});

test("手机画线：✎ 打开同一套工具（UI 命令同名、快照同一份）；选工具即收起并开始放点", () => {
  const drawings = handle([trend()]);
  const view = props({ drawings });
  render(<MobileMarket {...view} />);
  expect(uiSurfaceStates().chartDrawings).toMatchObject({ toolbar: "hidden", count: 1, tool: null });
  fireEvent.click(screen.getByRole("button", { name: "画线工具" }));
  const sheet = screen.getByRole("dialog", { name: "画线工具" });
  // The computer's six groups with every tool.
  for (const group of ["线条", "价位", "分割", "形状", "标注", "测量"]) expect(within(sheet).getByRole("group", { name: `${group}工具` })).toBeInTheDocument();
  expect(uiSurfaceStates().chartDrawings).toMatchObject({ toolbar: "shown" });
  fireEvent.click(within(sheet).getByRole("button", { name: "平行通道" }));
  expect(screen.queryByRole("dialog", { name: "画线工具" })).toBeNull();
  expect(lastChart().drawingTool).toBe("channel");
  expect(screen.getByRole("toolbar", { name: "正在画线" })).toHaveTextContent("已点 0/3");
  act(() => lastChart().onDrawingDraft(2));
  expect(screen.getByRole("toolbar", { name: "正在画线" })).toHaveTextContent("已点 2/3");
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(lastChart().drawingTool).toBeNull();
  // The same command the computer answers opens and closes the phone's sheet.
  act(() => uiSurface("disclosure:chart-drawing-toolbar")!.apply!(true));
  expect(screen.getByRole("dialog", { name: "画线工具" })).toBeInTheDocument();
  act(() => uiSurface("disclosure:chart-drawing-toolbar")!.apply!(false));
  expect(screen.queryByRole("dialog", { name: "画线工具" })).toBeNull();
  // The switches are the account's chart preferences, one with the computer.
  fireEvent.click(screen.getByRole("button", { name: "画线工具" }));
  fireEvent.click(screen.getByRole("button", { name: "全部锁定" }));
  expect(view.onIndicatorsChange).toHaveBeenLastCalledWith({ drawingsLocked: true });
});

test("手机画线：折线的“完成”够两点才可按，按下真正结束放置；新线带账号里的默认样式写同一份记录", async () => {
  const drawings = handle([]);
  const view = props({ drawings, chartPreferences: { drawingDefaults: { hline: { color: "red", width: 2 } } } });
  render(<MobileMarket {...view} />);
  act(() => uiSurface("family:chart-drawing-tool")!.apply!({ id: "polyline", open: true }));
  const done = within(screen.getByRole("toolbar", { name: "正在画线" })).getByRole("button", { name: "完成" });
  expect(done).toBeDisabled();
  act(() => lastChart().onDrawingDraft(1));
  expect(done).toBeDisabled();
  act(() => lastChart().onDrawingDraft(2));
  const before = lastChart().drawingFinish;
  fireEvent.click(done);
  expect(lastChart().drawingFinish).toBe(before + 1);
  // A placed 水平线 goes to the computer's drawing records with the account's default style for that tool.
  act(() => uiSurface("family:chart-drawing-tool")!.apply!({ id: "hline", open: true }));
  await act(async () => { lastChart().onPlaceDrawing("hline", [{ t: T2, p: 3050.4 }]); });
  expect(drawings.create).toHaveBeenCalledWith("SHFE.rb2701", "hline", { price: 3050 }, "", { period: 300, color: "red", width: 2 });
});

test("手机画线：±1 跳按真实 tick 动整条线或选定端点；锁定的线不能微调；完成取消选择", () => {
  const drawings = handle([trend(), trend({ id: "d2", kind: "hline", points: { price: 3050 }, style: { locked: true } })]);
  const view = props({ drawings });
  render(<MobileMarket {...view} />);
  act(() => lastChart().onDrawingSelect("d1", { clientX: 10, clientY: 10 }, null));
  const bar = () => screen.getByRole("toolbar", { name: "趋势线的样式" });
  expect(within(bar()).getByTestId("drawing-nudge-target")).toHaveTextContent("整条线");
  fireEvent.click(within(bar()).getByRole("button", { name: "上移 1 跳" }));
  expect(drawings.update).toHaveBeenLastCalledWith("d1", { points: placementPoints("trendline", [{ t: T1, p: 3001 }, { t: T2, p: 3021 }], 1) });
  // The second endpoint pressed (or dragged): only it moves, and the chart draws it filled.
  act(() => lastChart().onDrawingSelect("d1", { clientX: 10, clientY: 10 }, 1));
  expect(lastChart().selectedDrawingHandle).toBe(1);
  expect(within(bar()).getByTestId("drawing-nudge-target")).toHaveTextContent("端点 2 · 3020");
  expect(uiSurfaceStates().chartDrawings).toMatchObject({ selected: "d1", handle: 1 });
  fireEvent.click(within(bar()).getByRole("button", { name: "下移 1 跳" }));
  expect(drawings.update).toHaveBeenLastCalledWith("d1", { points: { t1: T1, p1: 3000, t2: T2, p2: 3019 } });
  fireEvent.click(within(bar()).getByRole("button", { name: "改整条" }));
  expect(within(bar()).getByTestId("drawing-nudge-target")).toHaveTextContent("整条线");
  // Style rows open one at a time and write the same style patch as the computer's bar.
  fireEvent.click(within(bar()).getByRole("button", { name: "颜色" }));
  expect(disclosureStates()).toMatchObject({ "mobile-drawing-style-row": true, "mobile-drawing-style-row:color": true });
  fireEvent.click(screen.getByRole("radio", { name: "颜色：蓝" }));
  expect(drawings.update).toHaveBeenLastCalledWith("d1", { style: { color: "blue" } });
  // The same rows by command (a click can open it, so can a command, and the snapshot sees which).
  const rows = () => uiSurface("family:mobile-drawing-style-row");
  act(() => rows()!.apply!({ id: "dash", open: true }));
  expect(within(bar()).getByRole("button", { name: "线型" })).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("radiogroup", { name: "线型" })).toBeInTheDocument();
  expect(screen.queryByRole("radiogroup", { name: "颜色" })).toBeNull();
  expect(disclosureStates()["mobile-drawing-style-row:color"]).toBeUndefined();
  expect(() => rows()!.apply!({ id: "fill", open: true })).toThrow("color / width / dash");
  act(() => rows()!.apply!({ id: "width", open: true }));
  expect(screen.getByRole("radiogroup", { name: "粗细" })).toBeInTheDocument();
  act(() => rows()!.apply!({ id: "dash", open: false }));                // closing another row leaves this one
  expect(disclosureStates()["mobile-drawing-style-row:width"]).toBe(true);
  // A locked line: ±1 跳 is off and says why. Selecting another line folds the open row.
  const writes = vi.mocked(drawings.update).mock.calls.length;
  act(() => lastChart().onDrawingSelect("d2", undefined, null));
  expect(disclosureStates()["mobile-drawing-style-row"]).toBe(false);
  const locked = screen.getByRole("toolbar", { name: "水平线的样式" });
  expect(within(locked).getByRole("button", { name: "上移 1 跳" })).toBeDisabled();
  expect(locked).toHaveTextContent("已锁定，解锁后才能微调");
  fireEvent.click(within(locked).getByRole("button", { name: "上移 1 跳" }));
  expect(drawings.update).toHaveBeenCalledTimes(writes);
  fireEvent.click(within(locked).getByRole("button", { name: "完成" }));
  expect(screen.queryByRole("toolbar", { name: "水平线的样式" })).toBeNull();
  expect(uiSurfaceStates().chartDrawings).toMatchObject({ selected: null, handle: null });
  // Nothing selected: no rows to open.
  expect(rows()).toBeUndefined();
});

test("手机画线：全部隐藏、切周期清掉放置与选择；分时图也画线，触摸不被副图手势拦下；游客没有 ✎", () => {
  const drawings = handle([trend()]);
  const { rerender } = render(<MobileMarket {...props({ drawings })} />);
  act(() => uiSurface("family:chart-drawing-tool")!.apply!({ id: "trendline", open: true }));
  rerender(<MobileMarket {...props({ drawings, chartPreferences: { drawingsHidden: true } })} />);
  expect(lastChart().drawingTool).toBeNull();
  expect(lastChart().userDrawings).toEqual([]);
  rerender(<MobileMarket {...props({ drawings })} />);
  act(() => lastChart().onDrawingSelect("d1", undefined, 0));
  expect(uiSurfaceStates().chartDrawings).toMatchObject({ selected: "d1", handle: 0 });
  act(() => uiSurface("family:mobile-drawing-style-row")!.apply!({ id: "color", open: true }));
  expect(disclosureStates()["mobile-drawing-style-row:color"]).toBe(true);
  rerender(<MobileMarket {...props({ drawings, chartView: "timeshare" })} />);
  expect(uiSurfaceStates().chartDrawings).toMatchObject({ selected: null, tool: null });
  expect(uiSurface("family:mobile-drawing-style-row")).toBeUndefined();
  const timeshare = mocks.timeshare.mock.calls.at(-1)![0] as ChartProps;
  expect(timeshare.userDrawings.map(item => item.id)).toEqual(["d1"]);
  // A finger on the 分时图 reaches the chart (its drawing taps, pan and pinch), not the sub-pane swipe.
  const reached = vi.fn();
  const canvas = screen.getByTestId("timeshare-canvas");
  canvas.addEventListener("pointerdown", reached);
  const down = new MouseEvent("pointerdown", { bubbles: true, cancelable: true });
  Object.defineProperty(down, "pointerType", { value: "touch" });
  canvas.dispatchEvent(down);
  expect(reached).toHaveBeenCalled();
  rerender(<MobileMarket {...props({ drawings: undefined, guest: true })} />);
  expect(screen.queryByRole("button", { name: "画线工具" })).toBeNull();
  expect(uiSurfaceStates().chartDrawings).toBeFalsy();
});

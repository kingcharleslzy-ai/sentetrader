import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import type { ChartAnnotation } from "../aiAnnotations";
import { TradingCharts, barReadout, type TradingChartSettings } from "./TradingCharts";

const chartMocks = vi.hoisted(() => ({
  createChart: vi.fn(),
  remove: vi.fn(),
  fitContent: vi.fn(),
}));

beforeEach(() => vi.clearAllMocks());

vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  CandlestickSeries: {},
  HistogramSeries: {},
  LineSeries: {},
  createSeriesMarkers: () => ({ detach: vi.fn() }),
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  createChart: chartMocks.createChart,
}));

function makeChart() {
  const series = {
    setData: vi.fn(),
    applyOptions: vi.fn(),
    priceScale: () => ({ applyOptions: vi.fn() }),
    createPriceLine: vi.fn(() => ({})),
    removePriceLine: vi.fn(),
    attachPrimitive: vi.fn(),
    detachPrimitive: vi.fn(),
    coordinateToPrice: vi.fn(() => 821.5),
  };
  const timeScale = {
    fitContent: chartMocks.fitContent,
    getVisibleRange: vi.fn(() => null),
    getVisibleLogicalRange: vi.fn(() => ({ from: 10, to: 20 })),
    setVisibleLogicalRange: vi.fn(),
    subscribeVisibleLogicalRangeChange: vi.fn(),
    unsubscribeVisibleLogicalRangeChange: vi.fn(),
  };
  return {
    paneSize: () => ({height:300,width:600}),
    addSeries: vi.fn((..._args: unknown[]) => ({ ...series })),
    removeSeries: vi.fn(),
    applyOptions: vi.fn(),
    timeScale: () => timeScale,
    subscribeClick: vi.fn(),
    unsubscribeClick: vi.fn(),
    remove: chartMocks.remove,
  };
}

const bars = [{
  datetime: 1_788_192_000_000_000_000,
  open: 820,
  high: 825,
  low: 818,
  close: 824,
  volume: 30,
  openInterest: 100,
}];

const settings: TradingChartSettings = {
  theme: "light",
  volume: true,
  crosshair: true,
};

/** The pre-configuration no-panes setup: one chart, so `createChart` call 0 is the price chart. */
const noPanes = {
  candleStyle: "candle" as const,
  upDownColors: "cn" as const,
  kdParams: { k: 9, d: 3, smooth: 3 },
  macdParams: { fast: 12, slow: 26, signal: 9 },
  subPaneList: [],
};

test("clicking a fill offers all fills in that candle; out-of-range focus does not jump", () => {
  chartMocks.createChart.mockImplementation(makeChart);
  const onSelect=vi.fn(), onFocus=vi.fn();
  const fill={tradeId:"a",clientOrderId:"o",symbol:"SHFE.au2612",direction:"BUY",offset:"OPEN",price:824,volume:1,tradeDateTime:bars[0].datetime};
  const view=render(<TradingCharts bars={bars} settings={settings} preferences={noPanes} onSettingsChange={vi.fn()} tradeFills={[fill,{...fill,tradeId:"b"}]} onTradeSelect={onSelect} onFocusResult={onFocus}/>);
  const chart=chartMocks.createChart.mock.results[0].value;
  for(const [handler] of chart.subscribeClick.mock.calls) handler({hoveredObjectId:"trade:a"});
  expect(onSelect).toHaveBeenCalledWith(["a","b"]);
  view.rerender(<TradingCharts bars={bars} settings={settings} preferences={noPanes} onSettingsChange={vi.fn()} onFocusResult={onFocus} tradeFocus={{openTime:bars[0].datetime+1e12,closeTime:bars[0].datetime+1e12,direction:"event"}}/>);
  expect(onFocus).toHaveBeenCalledWith(expect.stringContaining("不在当前已加载"));
  expect(chart.timeScale().setVisibleLogicalRange).not.toHaveBeenCalled();
});

test("replay labels use Shanghai time without shifting historical candle timestamps", () => {
  chartMocks.createChart.mockImplementation(makeChart);
  render(<TradingCharts bars={bars} settings={settings} preferences={noPanes} timeZone="Asia/Shanghai" onSettingsChange={vi.fn()} />);
  const [, options] = chartMocks.createChart.mock.calls[0];
  const time = Date.parse("2026-09-03T13:00:00Z") / 1000;
  expect(options.timeScale.tickMarkFormatter(time, 3)).toBe("21:00");
  expect(options.localization.timeFormatter(time)).toContain("21:00");
  const chart = chartMocks.createChart.mock.results[0].value;
  expect(chart.addSeries.mock.results[0].value.setData).toHaveBeenCalledWith([
    expect.objectContaining({ time: Math.floor(bars[0].datetime / 1_000_000_000) })
  ]);
});

test("market updates and visual settings keep the existing chart and visible range", () => {
  chartMocks.createChart.mockImplementation(makeChart);
  const view = render(
    <TradingCharts
      bars={bars}
      settings={settings}
      preferences={noPanes}
      selectedPrice={824}
      onSettingsChange={vi.fn()}
    />
  );
  expect(chartMocks.createChart).toHaveBeenCalledTimes(1);
  // Mount fit plus the first-data fit (fewer bars than the 600 px pane holds); updates add none.
  const fitsAfterLoad = chartMocks.fitContent.mock.calls.length;
  expect(fitsAfterLoad).toBeGreaterThanOrEqual(1);

  view.rerender(
    <TradingCharts
      bars={[...bars, { ...bars[0], datetime: bars[0].datetime + 300_000_000_000, close: 825 }]}
      settings={{
        ...settings,
        theme: "dark",
        volume: false,
      }}
      preferences={{ ...noPanes,
        maLines: [{ period: 5, enabled: true }, { period: 10, enabled: true }, { period: 25, enabled: false }] }}
      selectedPrice={825}
      onSettingsChange={vi.fn()}
    />
  );

  expect(chartMocks.createChart).toHaveBeenCalledTimes(1);
  expect(chartMocks.remove).not.toHaveBeenCalled();
  expect(chartMocks.fitContent).toHaveBeenCalledTimes(fitsAfterLoad);
});

test("latest candle line and price label default hidden and can be enabled without rebuilding charts", () => {
  const chart = makeChart(); chartMocks.createChart.mockReturnValue(chart);
  const view = render(<TradingCharts bars={bars} settings={settings} preferences={noPanes} onSettingsChange={vi.fn()}/>);
  expect(chart.addSeries.mock.calls[0][1]).toMatchObject({priceLineVisible: false, lastValueVisible: false});
  const candles = chart.addSeries.mock.results[0].value;
  view.rerender(<TradingCharts bars={bars} settings={{...settings,lastPriceLine:true}} preferences={noPanes} onSettingsChange={vi.fn()}/>);
  expect(candles.applyOptions).toHaveBeenCalledWith(expect.objectContaining({priceLineVisible:true,lastValueVisible:true}));
  expect(chartMocks.createChart).toHaveBeenCalledTimes(1);
});

test("right-click uses chart Y converted to the current tick; the left click no longer picks a price", () => {
  const chart=makeChart(); chartMocks.createChart.mockReturnValue(chart);
  const context=vi.fn();
  render(<TradingCharts bars={bars} settings={settings} preferences={noPanes} selectedPrice={999} priceTick={0.2} onPriceContext={context} onSettingsChange={vi.fn()}/>);
  const candles=chart.addSeries.mock.results[0].value;
  candles.coordinateToPrice.mockReturnValue(821.53);
  fireEvent.contextMenu(screen.getByLabelText("K 线图"), {clientX:100,clientY:125});
  expect(candles.coordinateToPrice).toHaveBeenCalledWith(125);
  expect(context).toHaveBeenCalledWith(821.6,100,125);
  context.mockClear();
  fireEvent.contextMenu(screen.getByLabelText("K 线图"), {clientY:310});
  expect(context).not.toHaveBeenCalled();
});


test("leaving fill focus restores the previous range and ordinary refresh does not trap chart navigation", () => {
  const chart = makeChart(); chartMocks.createChart.mockReturnValue(chart);
  const base = {bars, settings, preferences: noPanes, onSettingsChange: vi.fn()};
  const view = render(<TradingCharts {...base}/>);
  const focus = {openTime: bars[0].datetime, closeTime: bars[0].datetime, direction: "event"};
  view.rerender(<TradingCharts {...base} tradeFocus={focus}/>);
  expect(chart.timeScale().setVisibleLogicalRange).toHaveBeenCalledTimes(1);
  view.rerender(<TradingCharts {...base} bars={[{...bars[0],close:823}]} tradeFocus={focus}/>);
  expect(chart.timeScale().setVisibleLogicalRange).toHaveBeenCalledTimes(1);
  view.rerender(<TradingCharts {...base}/>);
  expect(chart.timeScale().setVisibleLogicalRange).toHaveBeenLastCalledWith({from:10,to:20});
});

test("indicator panes carry no close button; they are switched off in settings or by the chartOption command", () => {
  chartMocks.createChart.mockImplementation(makeChart);
  const onSettingsChange = vi.fn();
  render(<TradingCharts bars={bars} settings={settings} onSettingsChange={onSettingsChange} />);
  expect(screen.getByText(/KD 9,3,3/)).toBeVisible();
  expect(screen.getByText(/MACD 12,26,9/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "关闭" })).toBeNull();
  expect(screen.getByRole("button", { name: "图表说明" })).toBeVisible();
  expect(onSettingsChange).not.toHaveBeenCalled();
});

test("account chart preferences drive the indicator parameters, the candle style and the direction colours", () => {
  chartMocks.createChart.mockImplementation(makeChart);
  document.documentElement.style.setProperty("--market-up", "#111111");
  document.documentElement.style.setProperty("--market-down", "#222222");
  const preferences = { candleStyle: "ohlc" as const, upDownColors: "cn" as const, kdParams: { k: 14, d: 5, smooth: 5 }, macdParams: { fast: 5, slow: 35, signal: 5 } };
  render(<TradingCharts bars={bars} settings={settings} preferences={preferences} onSettingsChange={vi.fn()} />);
  expect(screen.getByText(/KD 14,5,5/)).toBeVisible();
  expect(screen.getByText(/MACD 5,35,5/)).toBeVisible();
  const chart = chartMocks.createChart.mock.results.at(-1)!.value as ReturnType<typeof makeChart>;
  const candleOptions = chart.addSeries.mock.calls[0][1] as Record<string, unknown>;
  // OHLC bars keep a hollow body; the wick/border colours come from the document's market tokens.
  expect(candleOptions).toMatchObject({ upColor: "transparent", downColor: "transparent", borderVisible: true, wickUpColor: "#111111", wickDownColor: "#222222" });
  document.documentElement.style.removeProperty("--market-up");
  document.documentElement.style.removeProperty("--market-down");
});

test("restricted history clamps left edge and prompts only on a boundary gesture", () => {
  chartMocks.createChart.mockImplementation(makeChart);
  const onHistoryRestricted = vi.fn();
  const view = render(<TradingCharts bars={bars} settings={settings} preferences={noPanes} onSettingsChange={vi.fn()} historyRestricted onHistoryRestricted={onHistoryRestricted} />);
  const chart = chartMocks.createChart.mock.results[0].value;
  expect(chart.applyOptions).toHaveBeenCalledWith({ timeScale: { fixLeftEdge: true } });
  chart.timeScale().getVisibleLogicalRange.mockReturnValue({ from: 0, to: 20 });
  for (const [handler] of chart.timeScale().subscribeVisibleLogicalRangeChange.mock.calls) handler({ from: 0, to: 20 });
  expect(onHistoryRestricted).not.toHaveBeenCalled();
  const stack = view.container.querySelector(".fdw-trading-charts")!;
  fireEvent.wheel(stack, { deltaY: 1 });
  fireEvent.wheel(stack, { deltaY: 1 });
  expect(onHistoryRestricted).toHaveBeenCalledOnce();
});

// -- 图表指标可配置 (2026-10-02): the in-place editors on the chart labels. -------------------------

const editablePreferences = (overrides: Record<string, unknown> = {}) => ({
  candleStyle: "candle" as const, upDownColors: "cn" as const,
  kdParams: { k: 9, d: 3, smooth: 3 }, macdParams: { fast: 12, slow: 26, signal: 9 }, ...overrides,
});

test("the labels stay plain text on a read-only chart and open editors only when connected", () => {
  chartMocks.createChart.mockImplementation(makeChart);
  render(<TradingCharts bars={bars} settings={settings} preferences={editablePreferences()} onSettingsChange={vi.fn()} />);
  expect(screen.getByText("均线 5 · 10 · 25 · 60 · 200").tagName).toBe("SPAN");
  expect(screen.queryByRole("button", { name: /均线/ })).toBeNull();
  expect(screen.queryByRole("button", { name: "添加副图" })).toBeNull();
});

test("the price label opens the MA editor and saves edits through the preference path", () => {
  chartMocks.createChart.mockImplementation(makeChart);
  const onIndicatorsChange = vi.fn();
  render(<TradingCharts bars={bars} settings={settings} preferences={editablePreferences()} onSettingsChange={vi.fn()} onIndicatorsChange={onIndicatorsChange} indicatorStatus="已保存" />);
  fireEvent.click(screen.getByRole("button", { name: /均线 5 · 10 · 25 · 60 · 200/ }));
  expect(screen.getByRole("dialog", { name: "均线与布林带" })).toBeVisible();
  expect(screen.getByRole("status")).toHaveTextContent("已保存");
  fireEvent.click(screen.getByLabelText("第 1 条均线周期增加"));
  expect(onIndicatorsChange).toHaveBeenLastCalledWith({ maLines: [
    { period: 6, enabled: true }, { period: 10, enabled: true }, { period: 25, enabled: true }, { period: 60, enabled: true }, { period: 200, enabled: true },
  ] });
  // BOLL stays locked while off; the switch patch carries the whole boll object.
  expect(screen.getByLabelText("布林周期")).toBeDisabled();
  expect(screen.getByLabelText("布林宽度")).toBeDisabled();
  fireEvent.click(screen.getByRole("switch", { name: "布林带" }));
  expect(onIndicatorsChange).toHaveBeenLastCalledWith({ boll: { enabled: true, period: 20, width: 2 } });
});

test("a pane label opens its editor; ＋副图 adds a pane and hides at three", () => {
  chartMocks.createChart.mockImplementation(makeChart);
  const onIndicatorsChange = vi.fn();
  const view = render(<TradingCharts bars={bars} settings={settings} preferences={editablePreferences()} onSettingsChange={vi.fn()} onIndicatorsChange={onIndicatorsChange} />);
  fireEvent.click(screen.getByRole("button", { name: /KD 9,3,3/ }));
  expect(screen.getByRole("dialog", { name: "KD 副图" })).toBeVisible();
  fireEvent.change(screen.getByLabelText("第 1 格指标"), { target: { value: "rsi" } });
  expect(onIndicatorsChange).toHaveBeenLastCalledWith({ subPaneList: ["rsi", "macd"] });
  fireEvent.click(screen.getByRole("button", { name: "移除这一格" }));
  expect(onIndicatorsChange).toHaveBeenLastCalledWith({ subPaneList: ["macd"] });
  fireEvent.click(screen.getByRole("button", { name: "添加副图" }));
  expect(screen.getByRole("dialog", { name: "添加副图" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "RSI副图" }));
  expect(onIndicatorsChange).toHaveBeenLastCalledWith({ subPaneList: ["kd", "macd", "rsi"] });
  // The ＋副图 button sits on the last label row and disappears once the list is full.
  view.rerender(<TradingCharts bars={bars} settings={settings} onSettingsChange={vi.fn()} onIndicatorsChange={onIndicatorsChange}
    preferences={editablePreferences({ subPaneList: [{ type: "kd" }, { type: "macd" }, { type: "rsi" }] })} />);
  expect(screen.queryByRole("button", { name: "添加副图" })).toBeNull();
});

test("annotations land on the pane that shows them; a hidden pane's item is simply not drawn", () => {
  chartMocks.createChart.mockImplementation(makeChart);
  const annotations: ChartAnnotation[] = [
    { id: "1", kind: "hline", pane: "kd", price: 70 },
    { id: "2", kind: "hline", pane: "macd", price: 0.5 },
    { id: "3", kind: "hline", pane: "main", price: 824 },
  ];
  render(<TradingCharts bars={bars} settings={settings} annotations={annotations} onSettingsChange={vi.fn()}
    preferences={editablePreferences({ subPaneList: [{ type: "kd" }] })} />);
  const [kdChart, mainChart] = chartMocks.createChart.mock.results.map(result => result.value);
  // Every series of one chart shares the mocked call log, so counts say what was drawn:
  // one line on the kd pane at 70 and one on the main pane at 824; the macd item's 0.5
  // has no pane showing it and is drawn nowhere.
  expect(kdChart.addSeries.mock.results[0].value.createPriceLine).toHaveBeenCalledTimes(1);
  expect(kdChart.addSeries.mock.results[0].value.createPriceLine).toHaveBeenCalledWith(expect.objectContaining({ price: 70 }));
  expect(mainChart.addSeries.mock.results[0].value.createPriceLine).toHaveBeenCalledTimes(1);
  expect(mainChart.addSeries.mock.results[0].value.createPriceLine).toHaveBeenCalledWith(expect.objectContaining({ price: 824 }));
  const drawnPrices = [kdChart, mainChart].map(chart =>
    chart.addSeries.mock.results[0].value.createPriceLine.mock.calls.map(([options]: [{ price: number }]) => options.price));
  expect(drawnPrices).toEqual([[70], [824]]);
  expect((document.querySelector(".fdw-trading-charts") as HTMLElement).dataset.agentChartAnnotations).toBe("3");
});

// -- #734: a guest's few bars. --------------------------------------------------------------------

const barsOf = (count: number) => Array.from({ length: count }, (_, index) => ({
  datetime: (1_788_192_000 + index * 86_400) * 1_000_000_000, open: 820 + index, high: 826 + index, low: 818 + index, close: 824 + index, volume: 30, openInterest: 100,
}));

test("panes with fewer bars than their look-back draw nothing and read 数据不足; the history basis shows (#734)", () => {
  chartMocks.createChart.mockImplementation(makeChart);
  const view = render(<TradingCharts bars={barsOf(3)} settings={settings} preferences={editablePreferences()} onSettingsChange={vi.fn()}
    historyNote="游客仅显示最近 3 个交易日 · 登录看完整历史" />);
  expect(screen.getByText("游客仅显示最近 3 个交易日 · 登录看完整历史")).toBeVisible();
  expect(screen.getByText("数据不足 · 需 13 根K线")).toBeVisible();
  expect(screen.getByText("数据不足 · 需 34 根K线")).toBeVisible();
  expect(screen.queryByText(/^K .* · D /)).toBeNull();
  // Every series of the KD and MACD panes gets whitespace only: no line from three bars.
  // The panes mount (children first) before the price chart, which is the last chart created.
  const paneCharts = chartMocks.createChart.mock.results.slice(0, -1).map(result => result.value as ReturnType<typeof makeChart>);
  expect(paneCharts).toHaveLength(2);
  for (const chart of paneCharts) {
    for (const series of chart.addSeries.mock.results) {
      const data = (series.value.setData as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0] as Array<Record<string, unknown>>;
      expect(data).toHaveLength(3);
      expect(data.every(point => !("value" in point))).toBe(true);
    }
  }
  const observation = JSON.parse((view.container.querySelector(".fdw-trading-charts") as HTMLElement).dataset.agentChartObservation ?? "{}");
  expect(observation.shortPanes).toEqual(["kd", "macd"]);
  expect(observation.displayedIndicators.kd).toBeNull();
});

test("the same panes draw once the loaded bars cover the look-back (#734)", () => {
  chartMocks.createChart.mockImplementation(makeChart);
  render(<TradingCharts bars={barsOf(40)} settings={settings} preferences={editablePreferences()} onSettingsChange={vi.fn()} />);
  expect(screen.queryByText(/数据不足/)).toBeNull();
  expect(screen.getByText(/^K [\d.]+ · D [\d.]+$/)).toBeVisible();
  expect(screen.queryByText(/游客仅显示/)).toBeNull();
});


test("十字光标停在某根 K 线：开高低收、相对前一根的涨跌、量与持仓；不在 K 线上时没有", () => {
  const bars = [
    { time: 1_792_470_000, open: 4150, high: 4161, low: 4149.5, close: 4160.2, volume: 900, openInterest: 12_000 },
    { time: 1_792_470_300, open: 4160.2, high: 4165, low: 4158.1, close: 4162.5, volume: 1234, openInterest: 12_345 },
  ];
  const row = barReadout(bars, 1_792_470_300, { decimals: 1, daily: false, timeZone: "Asia/Shanghai" })!;
  expect(row).toMatchObject({ open: "4,160.2", high: "4,165.0", low: "4,158.1", close: "4,162.5", direction: "up",
    change: "+2.3 (+0.06%)", volume: "1,234", openInterest: "12,345" });
  expect(row.when).toMatch(/10.20.*\d{2}:\d{2}/);
  expect(barReadout(bars, 1_792_470_000, { decimals: 1, daily: false, timeZone: "Asia/Shanghai" })!.change).toBeNull();
  expect(barReadout(bars, 1_792_470_123, { decimals: 1, daily: false, timeZone: "Asia/Shanghai" })).toBeNull();
  expect(barReadout(bars, undefined, { decimals: 1, daily: false, timeZone: "Asia/Shanghai" })).toBeNull();
});


test("contract changes update tick-only formatting and precise labels without rebuilding the chart", () => {
  const chart = makeChart(); chartMocks.createChart.mockReturnValue(chart);
  const base = { bars, settings, preferences: noPanes, onSettingsChange: vi.fn() };
  const view = render(<TradingCharts {...base} priceTick={0.25} />);
  const candles = chart.addSeries.mock.results[0].value;
  const priceFormat = () => (candles.applyOptions.mock.calls as Array<[{ priceFormat?: { formatter: (value: number) => string } }]>).map(([options]) => options).reverse().find(options => options.priceFormat)!.priceFormat!;
  const localization = () => chart.applyOptions.mock.calls.map(([options]) => options).reverse().find(options => options.localization)!.localization;
  expect(priceFormat().formatter(29474.33)).toBe("29474.25");
  expect(priceFormat().formatter(29474.5)).toBe("29474.50");
  expect(localization().tickmarksPriceFormatter([29600, 29474.5])).toEqual(["29600", "29474.5"]);
  view.rerender(<TradingCharts {...base} priceTick={1} />);
  expect(priceFormat().formatter(29600)).toBe("29600");
  expect(chartMocks.createChart).toHaveBeenCalledTimes(1);
});

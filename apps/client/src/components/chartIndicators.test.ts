import { CHART_SETTINGS_STORAGE_KEY } from "../theme";
import { consumeLegacyMaVisible } from "./futures/support";
import {
  DEFAULT_CHART_INDICATORS, MA_LINES_MAX, SUB_PANES_MAX,
  bollingerBands, cciSeries, kdSeries, macdSeries, maLinesLabel, movingAverage, normalizeChartIndicators, normalizePaneSizes,
  openInterestSeries, paneGridRows, rsiSeries, subPaneDataShort, subPaneLabel, subPaneMinBars, type SubPaneType,
} from "./chartIndicators";

test("one look-back rule decides 数据不足 for every pane, following the configured parameters (#734)", () => {
  // KD: RSV 14, then K and D smoothing (3 − 1 each); MACD: slow 30 for DIF, then the 9-bar DEA.
  const config = normalizeChartIndicators({ kdParams: { k: 14, d: 3, smooth: 3 }, macdParams: { fast: 12, slow: 30, signal: 9 } });
  expect((["kd", "macd", "rsi", "cci", "oi", "volume"] as SubPaneType[]).map(type => subPaneMinBars(type, config)))
    .toEqual([18, 38, config.rsiParams.period + 1, config.cciParams.period, 1, 1]);
  expect(subPaneDataShort("macd", config, 3)).toBe(true);
  expect(subPaneDataShort("macd", config, 37)).toBe(true);
  expect(subPaneDataShort("macd", config, 38)).toBe(false);
  expect(subPaneDataShort("volume", config, 1)).toBe(false);
  // A fast period set longer than the slow one: the longer of the two decides DIF's window.
  const swapped = normalizeChartIndicators({ macdParams: { fast: 40, slow: 20, signal: 5 } });
  expect(swapped.macdParams).toMatchObject({ fast: 40, slow: 20 });
  expect(subPaneMinBars("macd", swapped)).toBe(44);
});

const closes = (values: number[]) => values.map((close, index) => ({ time: index + 100, close }));
/** Roll a running float sum back onto the hand-computed value at 1e-9 precision. */
const round9 = (value: number) => Math.round(value * 1e9) / 1e9;
const bars = (rows: Array<[open: number, high: number, low: number, close: number, volume: number, openInterest: number]>) =>
  rows.map(([open, high, low, close, volume, openInterest], index) => ({ time: index + 100, open, high, low, close, volume, openInterest }));

test("movingAverage averages the trailing window and starts at bar period", () => {
  expect(movingAverage(closes([10, 11, 12, 13, 14]), 3)).toEqual([
    { time: 102, value: 11 }, { time: 103, value: 12 }, { time: 104, value: 13 },
  ]);
});

test("bollingerBands uses the population standard deviation around the SMA mid", () => {
  const deviation = Math.sqrt(2 / 3) * 2; // closes 10,11,12 (and each later window) spread ±1 around the mean
  const { mid, top, bottom } = bollingerBands(closes([10, 11, 12, 13, 14]), 3, 2);
  expect(mid.map(point => point.value)).toEqual([11, 12, 13]);
  expect(top.map(point => round9(point.value))).toEqual([11, 12, 13].map(mean => round9(mean + deviation)));
  expect(bottom.map(point => round9(point.value))).toEqual([11, 12, 13].map(mean => round9(mean - deviation)));
});

test("rsiSeries follows Wilder's smoothing", () => {
  // changes +1, −1, −1, +1 with period 3: first value averages the first three changes,
  // then gain/loss each keep 2/3 of the previous average.
  expect(rsiSeries(closes([10, 11, 10, 9, 10]), 3)).toEqual([
    { time: 103, value: 100 - 100 / 1.5 },
    { time: 104, value: 100 - 100 / (1 + (5 / 9) / (4 / 9)) },
  ]);
  // Only gains: no losses ever, so RSI pins to 100.
  expect(rsiSeries(closes([1, 2, 3, 4]), 3).every(point => point.value === 100)).toBe(true);
});

test("kdSeries smooths RSV with 1/m weights in the Chinese-software sense", () => {
  const series = kdSeries(bars([[20, 30, 10, 20, 1, 1], [30, 30, 20, 30, 1, 1]]), { k: 3, d: 3, smooth: 3 });
  expect(series[0]).toMatchObject({ k: 50, d: 50 }); // one bar: rsv 50 seeds both averages
  expect(series[1].k).toBeCloseTo(200 / 3);
  expect(series[1].d).toBeCloseTo(500 / 3 / 3);
});

test("macdSeries doubles the histogram and openInterestSeries copies the bar values", () => {
  const series = macdSeries(bars([[1, 2, 0, 1, 1, 7], [1, 2, 0, 2, 1, 9], [1, 2, 0, 3, 1, 11], [1, 2, 0, 4, 1, 13]]), { fast: 2, slow: 3, signal: 2 });
  // Hand-computed EMA(2), EMA(3) and signal(2) chains over closes 1..4.
  expect(series[3].diff).toBeCloseTo(85 / 216);
  expect(series[3].dea).toBeCloseTo(37 / 108);
  expect(series[3].histogram).toBeCloseTo(11 / 108);
  expect(openInterestSeries(bars([[1, 2, 0, 1, 1, 7], [1, 2, 0, 2, 1, 9]]))).toEqual([{ time: 100, value: 7 }, { time: 101, value: 9 }]);
});

test("normalizeChartIndicators fills the defaults, repairs stored damage and derives the legacy switches", () => {
  expect(normalizeChartIndicators({})).toEqual(DEFAULT_CHART_INDICATORS);
  // The 1.6.12 kd/macd switches decide the pane list only when the list itself is absent.
  expect(normalizeChartIndicators({ subPanes: { kd: false, macd: true } }).subPaneList).toEqual(["macd"]);
  expect(normalizeChartIndicators({ subPanes: { kd: false, macd: false } }).subPaneList).toEqual([]);
  expect(normalizeChartIndicators({ subPaneList: ["rsi", "oi", "kd"], subPanes: { kd: false, macd: true } }).subPaneList).toEqual(["rsi", "oi", "kd"]);
  // Unknown types and repeats drop out; the cap of three keeps the first three.
  expect(normalizeChartIndicators({ subPaneList: ["rsi", "boll" as SubPaneType, "rsi", "oi", "kd"] }).subPaneList).toEqual(["rsi", "oi", "kd"]);
  // Periods clamp into 2–500, enabled defaults on, repeats keep the first, at most 8 lines.
  expect(normalizeChartIndicators({ maLines: [{ period: 1 }, { period: 600 }, { period: 5, enabled: false }, { period: 5 }] }).maLines).toEqual([
    { period: 2, enabled: true }, { period: 500, enabled: true }, { period: 5, enabled: false },
  ]);
  expect(normalizeChartIndicators({ maLines: Array.from({ length: 10 }, (_, index) => ({ period: index + 2 })) }).maLines).toHaveLength(MA_LINES_MAX);
  expect(normalizeChartIndicators({ maLines: [] }).maLines).toEqual(DEFAULT_CHART_INDICATORS.maLines);
  expect(normalizeChartIndicators({ boll: { enabled: true, period: 2, width: 9 }, kdParams: { k: 1, d: 0, smooth: 999 }, rsiParams: { period: 1 } })).toMatchObject({
    boll: { enabled: true, period: 5, width: 4 },
    kdParams: { k: 2, d: 1, smooth: 30 },
    rsiParams: { period: 2 },
  });
});

test("pane geometry keeps the main share honest and spreads rows with 5px gaps", () => {
  expect(normalizePaneSizes(undefined, 3)).toEqual({ main: 58, panes: [19, 23, 14] });
  expect(normalizePaneSizes({ main: 99, panes: [5, 40] }, 2)).toEqual({ main: 76, panes: [12, 36] });
  // A new pane takes a default share; stored shares carry over by position.
  expect(normalizePaneSizes({ main: 40, panes: [20] }, 3)).toEqual({ main: 40, panes: [20, 23, 14] });
  expect(paneGridRows({ main: 58, panes: [] })).toBe("1fr");
  expect(paneGridRows({ main: 58, panes: [19, 23] })).toBe("58fr 5px 19fr 5px 23fr");
});

test("labels read the normalized configuration", () => {
  expect(maLinesLabel(DEFAULT_CHART_INDICATORS)).toBe("均线 5 · 10 · 25 · 60 · 200");
  expect(maLinesLabel(normalizeChartIndicators({ maLines: [{ period: 5 }, { period: 10, enabled: false }], boll: { enabled: true } }))).toBe("均线 5 · 布林 20,2");
  expect(maLinesLabel(normalizeChartIndicators({ maLines: [{ period: 5, enabled: false }] }))).toBe("均线未启用");
  expect(subPaneLabel("kd", DEFAULT_CHART_INDICATORS)).toBe("KD 9,3,3");
  expect(subPaneLabel("macd", DEFAULT_CHART_INDICATORS)).toBe("MACD 12,26,9");
  expect(subPaneLabel("rsi", DEFAULT_CHART_INDICATORS)).toBe("RSI 14");
  expect(subPaneLabel("oi", DEFAULT_CHART_INDICATORS)).toBe("持仓量");
  expect(SUB_PANES_MAX).toBe(3);
});

test("consumeLegacyMaVisible migrates the stored maVisible switches once", () => {
  window.localStorage.clear();
  expect(consumeLegacyMaVisible()).toBeNull(); // nothing stored
  window.localStorage.setItem(CHART_SETTINGS_STORAGE_KEY, JSON.stringify({ theme: "dark", maVisible: { 5: true, 10: false, 25: true, 60: true, 200: false } }));
  expect(consumeLegacyMaVisible()).toEqual([
    { period: 5, enabled: true }, { period: 10, enabled: false }, { period: 25, enabled: true }, { period: 60, enabled: true }, { period: 200, enabled: false },
  ]);
  // The switches leave the local key, so a second run has nothing to migrate.
  expect(JSON.parse(window.localStorage.getItem(CHART_SETTINGS_STORAGE_KEY)!)).toEqual({ theme: "dark" });
  expect(consumeLegacyMaVisible()).toBeNull();
  // All-on switches equal the default five: the local key is consumed but no preference write happens.
  window.localStorage.setItem(CHART_SETTINGS_STORAGE_KEY, JSON.stringify({ maVisible: { 5: true, 10: true, 25: true, 60: true, 200: true } }));
  expect(consumeLegacyMaVisible()).toBeNull();
  expect(JSON.parse(window.localStorage.getItem(CHART_SETTINGS_STORAGE_KEY)!)).toEqual({});
  window.localStorage.clear();
});

test("a flat window of decimal prices reads CCI 0, not a rounding residue (#998)", () => {
  for (const price of [4123.2, 3.1, 100]) {
    const bars = Array.from({ length: 16 }, (_, index) => ({ time: 100 + index * 60, high: price, low: price, close: price }));
    expect(cciSeries(bars, 14).map(point => point.value)).toEqual([0, 0, 0]);
  }
  const moving = Array.from({ length: 14 }, (_, index) => ({ time: index, high: 10 + index, low: 10 + index, close: 10 + index }));
  expect(cciSeries(moving, 14)[0].value).toBeCloseTo(6.5 / (0.015 * 3.5), 6);
});

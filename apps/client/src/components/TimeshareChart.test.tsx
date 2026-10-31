import { render } from "@testing-library/react";
import { vi } from "vitest";
import { TimeshareChart } from "./TimeshareChart";
import type { MarketKlineBar } from "../api";

const ranges = vi.hoisted(() => [] as Array<{ from: number; to: number }>);
vi.mock("lightweight-charts", () => {
  // Any chart method is a no-op returning another no-op object; the logical range check is the real
  // library's (#739: an inverted range throws "The from index cannot be after the to index").
  const loose = (): unknown => new Proxy(() => undefined, { get: (_target, key) => key === "then" ? undefined : () => loose(), apply: () => loose() });
  const timeScale = {
    setVisibleLogicalRange: (range: { from: number; to: number }) => {
      if (range.from > range.to) throw new Error("The from index cannot be after the to index");
      ranges.push(range);
    },
    subscribeVisibleLogicalRangeChange: () => undefined, unsubscribeVisibleLogicalRangeChange: () => undefined,
    getVisibleLogicalRange: () => null, fitContent: () => undefined, applyOptions: () => undefined,
  };
  return {
    ColorType: { Solid: "solid" }, LineSeries: {}, HistogramSeries: {}, TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2 },
    createChart: () => new Proxy({}, { get: (_target, key) => key === "timeScale" ? () => timeScale : key === "then" ? undefined : () => loose() }),
  };
});

const settings = { theme: "dark", volume: true, crosshair: true, lastPriceLine: true } as never;
const schedule = { night: [], day: [["09:00", "10:15"]] as [string, string][] };
const bar = (minute: number): MarketKlineBar => ({ datetime: Date.UTC(2026, 9, 9, 1, minute) * 1_000_000, open: 100, high: 100, low: 100, close: 100, volume: 1, openInterest: 0, tradingDay: "2026-10-09" });

test("#739 空分时数据不设倒置区间、不抛错；有数据→空→恢复都能画", () => {
  ranges.length = 0;
  const view = render(<TimeshareChart bars={[]} schedule={schedule} settings={settings} priceTick={1} />);
  expect(ranges).toHaveLength(0);
  view.rerender(<TimeshareChart bars={[bar(0), bar(1)]} schedule={schedule} settings={settings} priceTick={1} />);
  expect(ranges.at(-1)).toEqual({ from: 0, to: 74 });
  view.rerender(<TimeshareChart bars={[]} schedule={schedule} settings={settings} priceTick={1} />);
  view.rerender(<TimeshareChart bars={[bar(0)]} schedule={schedule} settings={settings} priceTick={1} />);
  expect(ranges.at(-1)).toEqual({ from: 0, to: 74 });
});

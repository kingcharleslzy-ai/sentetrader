// @vitest-environment node
import { describe, expect, it } from "vitest";
import type { MarketKlineBar } from "./api";
import { TIMESHARE_TIME_BASE, buildTimeshare, type TradingSchedule } from "./timeshare";

// 2026-10-09 is a Friday; 2026-10-12 the Monday after. China wall clock = UTC + 8.
const nanos = (utcYear: number, utcMonth: number, utcDay: number, hour: number, minute: number) =>
  Date.UTC(utcYear, utcMonth, utcDay, hour, minute) * 1_000_000;
const friday = (hour: number, minute: number) => nanos(2026, 9, 9, hour - 8, minute);
const saturday = (hour: number, minute: number) => nanos(2026, 9, 10, hour - 8, minute);

function bar(datetime: number, close: number, volume: number, open = close, tradingDay?: string): MarketKlineBar {
  return { datetime, open, high: close, low: open, close, volume, openInterest: 0, tradingDay };
}

const schedule = (night: [string, string][], day: [string, string][]): TradingSchedule => ({ night, day });
const AU_SCHEDULE = schedule([["21:00", "23:00"]], [["09:00", "10:15"], ["10:30", "11:30"], ["13:30", "15:00"]]);
const values = (points: Array<{ value?: number }>) => points.map(point => point.value);

describe("buildTimeshare 分时数据", () => {
  it("夜盘在前日盘在后铺满全部时段，分钟格连续且钟点正确（345 格）", () => {
    const data = buildTimeshare([bar(friday(9, 0), 100, 1, 100, "2026-10-09")], AU_SCHEDULE);
    expect(data.slots).toHaveLength(345); // 夜 120 + 09:00–10:15 75 + 10:30–11:30 60 + 13:30–15:00 90
    expect(data.slots[0]).toMatchObject({ time: TIMESHARE_TIME_BASE, clock: "21:00" });
    expect(data.slots[59]).toMatchObject({ clock: "21:59" });
    expect(data.slots[60]).toMatchObject({ clock: "22:00" });
    expect(data.slots[120]).toMatchObject({ clock: "09:00", time: TIMESHARE_TIME_BASE + 120 * 60 });
    expect(data.slots[194]).toMatchObject({ clock: "10:14" }); // 第一段结束钟点不留格
    expect(data.slots[195]).toMatchObject({ clock: "10:30" }); // 段间不留空隙
    expect(data.slots.at(-1)).toMatchObject({ clock: "14:59", time: TIMESHARE_TIME_BASE + 344 * 60 });
  });

  it("跨零点夜盘按 (end-start+1440)%1440 展开，钟点回绕", () => {
    const data = buildTimeshare([bar(saturday(0, 30), 100, 1, 100, "2026-10-12")], schedule([["23:00", "01:00"]], [["09:00", "10:00"]]));
    const clocks = data.slots.map(slot => slot.clock);
    expect(data.slots).toHaveLength(180); // 夜 120（23:00…23:59 + 00:00…00:59）+ 日 60
    expect(clocks.slice(0, 60)).toEqual(Array.from({ length: 60 }, (_, i) => `23:${String(i).padStart(2, "0")}`));
    expect(clocks.slice(60, 120)).toEqual(Array.from({ length: 60 }, (_, i) => `00:${String(i).padStart(2, "0")}`));
    expect(clocks[120]).toBe("09:00");
  });

  it("夜盘 bar 归属下一交易日：显示最新交易日，旧日 bar 全部剔除", () => {
    const bars = [
      bar(friday(9, 30), 100, 10, 100, "2026-10-09"),
      bar(friday(21, 0), 200, 10, 200, "2026-10-12"),
      bar(friday(21, 1), 201, 10, 201, "2026-10-12")
    ];
    const data = buildTimeshare(bars, AU_SCHEDULE);
    expect(data.tradingDay).toBe("2026-10-12");
    expect(data.price.filter(point => point.value !== undefined)).toEqual([
      { time: TIMESHARE_TIME_BASE, value: 200 },
      { time: TIMESHARE_TIME_BASE + 60, value: 201 }
    ]);
  });

  it("没有 tradingDay 字段时按钟点兜底：周五夜盘与周六凌晨都落到周一", () => {
    expect(buildTimeshare([bar(friday(21, 0), 1, 1)], AU_SCHEDULE).tradingDay).toBe("2026-10-12");
    expect(buildTimeshare([bar(saturday(0, 30), 1, 1)], AU_SCHEDULE).tradingDay).toBe("2026-10-12");
    expect(buildTimeshare([bar(friday(9, 30), 1, 1)], AU_SCHEDULE).tradingDay).toBe("2026-10-09");
  });

  it("外盘（CME）的 K 线自带 Runtime 给的 CME 交易日（美东 18:00 换日）；时段 06:00–次日 05:00 铺 1380 格", () => {
    const cme: TradingSchedule = { ...schedule([], [["06:00", "29:00"]]), dayRule: "cme" };
    const evening = bar(friday(19, 0), 1, 1, 1, "2026-10-09"); // 北京周五 19:00 = 美东周五 07:00
    expect(buildTimeshare([evening], cme).tradingDay).toBe("2026-10-09");
    expect(buildTimeshare([evening, bar(saturday(4, 30), 1, 1, 1, "2026-10-09")], cme).slots).toHaveLength(1380);
    expect(buildTimeshare([bar(friday(19, 0), 1, 1)], AU_SCHEDULE).tradingDay).toBe("2026-10-12"); // 国内照旧
  });

  it("未到分钟留空白，均价按 Σ(close×volume)÷Σvolume 累计并跨空白携带", () => {
    const data = buildTimeshare([
      bar(friday(9, 0), 100, 10, 99, "2026-10-09"),
      bar(friday(9, 1), 110, 30, 105, "2026-10-09"),
      bar(friday(9, 3), 120, 10, 118, "2026-10-09"),
      bar(friday(9, 4), 130, 0, 125, "2026-10-09")
    ], schedule([], [["09:00", "09:05"]]));
    expect(values(data.price)).toEqual([100, 110, undefined, 120, 130]);
    expect(values(data.average)).toEqual([100, 107.5, 107.5, 110, 110]); // (100×10+110×30)÷40=107.5，再摊 120×10=110；零量不摊
    expect(data.volume).toEqual([
      { time: TIMESHARE_TIME_BASE, value: 10, up: true },
      { time: TIMESHARE_TIME_BASE + 60, value: 30, up: true },
      { time: TIMESHARE_TIME_BASE + 180, value: 10, up: true }
    ]);
    expect(data.latest).toMatchObject({ clock: "09:04", price: 130, average: 110, volume: 0, up: true });
  });

  it("最新成交分钟之后的未到分钟，价格线与均价线都留空白", () => {
    const data = buildTimeshare([
      bar(friday(9, 0), 100, 10, 100, "2026-10-09"),
      bar(friday(9, 1), 110, 30, 105, "2026-10-09")
    ], schedule([], [["09:00", "09:05"]]));
    expect(values(data.price)).toEqual([100, 110, undefined, undefined, undefined]);
    expect(values(data.average)).toEqual([100, 107.5, undefined, undefined, undefined]);
    expect(data.latest).toMatchObject({ clock: "09:01", price: 110 });
  });

  it("没有交易时段时按当日观察到的去重分钟排格", () => {
    const data = buildTimeshare([
      bar(friday(9, 2), 102, 2, 102, "2026-10-09"),
      bar(friday(9, 0), 100, 1, 100, "2026-10-09"),
      bar(friday(9, 1), 101, 1, 101, "2026-10-09")
    ], null);
    expect(data.slots.map(slot => slot.clock)).toEqual(["09:00", "09:01", "09:02"]);
    expect(values(data.price)).toEqual([100, 101, 102]);
  });

  it("空数据与全部无效收盘价都返回空结构", () => {
    const empty = { tradingDay: null, slots: [], price: [], average: [], volume: [], latest: null };
    expect(buildTimeshare([], AU_SCHEDULE)).toEqual(empty);
    expect(buildTimeshare([{ ...bar(friday(9, 0), NaN, 1) }], AU_SCHEDULE)).toEqual(empty);
  });
});

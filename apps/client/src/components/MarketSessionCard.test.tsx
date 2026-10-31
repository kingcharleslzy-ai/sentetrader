import { act, render, screen } from "@testing-library/react";
import { afterEach, test, vi } from "vitest";
import type { MarketWorkbenchSnapshot } from "../api";
import { MarketSessionCard } from "./MarketSessionCard";

/** Beijing wall-clock text the way the TqKq sidecar stamps it ("2026-09-21 22:45:10"). */
const beijingStamp = (secondsAgo: number, zoned = false) => {
  const at = new Date(Date.now() - secondsAgo * 1000 + 8 * 3600_000).toISOString();
  return zoned ? `${at.slice(0, 10)}T${at.slice(11, 19)}+08:00` : `${at.slice(0, 10)} ${at.slice(11, 19)}`;
};

const market = (quote: Partial<MarketWorkbenchSnapshot["quote"]> = {}, over: Partial<MarketWorkbenchSnapshot> = {}): MarketWorkbenchSnapshot => ({
  schemaVersion: 1,
  durationSeconds: 60,
  capturedAt: new Date().toISOString(),
  ageSeconds: 0.3,
  dataStatus: "normal",
  marketSessionStatus: "trading",
  isCached: false,
  liveTrading: "disabled",
  quote: {
    symbol: "SHFE.au2612", name: "沪金2612", quoteDatetime: beijingStamp(1), lastPrice: 824, changePercent: 0.7,
    bidPrice1: null, bidVolume1: null, askPrice1: null, askVolume1: null, open: null, highest: null,
    lowest: null, volume: null, openInterest: null, priceTick: null, volumeMultiple: null,
    upperLimit: null, lowerLimit: null, ...quote
  },
  bars: [],
  ...over
});

afterEach(() => vi.useRealTimers());

test("断流才叫行情待更新：报价停走按秒数说，样式仍是普通交易中", () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-09-21T22:45:30+08:00"));
  // The feed itself is down: the whole snapshot is not normal data.
  const view0 = render(<MarketSessionCard market={market({ quoteDatetime: beijingStamp(30) }, { dataStatus: "stale", ageSeconds: 30, isCached: true })} />);
  const trigger = screen.getByRole("button", { name: "行情待更新，查看交易时间" });
  expect(trigger).toHaveTextContent("行情待更新");
  expect(trigger.className).toContain("stale");
  view0.unmount();

  // Data is normal, only this contract's quote has gone quiet: the label counts by the wall clock.
  const quiet = market({ quoteDatetime: beijingStamp(20) });
  const view = render(<MarketSessionCard market={quiet} />);
  const counting = screen.getByRole("button", { name: "20 秒无新报价，查看交易时间" });
  expect(counting).toHaveTextContent("20 秒无新报价");
  expect(counting.className).toContain("live"); // 普通样式，不借机渲染成断流
  act(() => { vi.advanceTimersByTime(1100); });
  expect(screen.getByRole("button", { name: "21 秒无新报价，查看交易时间" })).toBeTruthy();
  act(() => { vi.advanceTimersByTime(60_000); });
  expect(screen.getByRole("button", { name: "81 秒无新报价，查看交易时间" })).toBeTruthy();
  view.unmount();

  // A current quote shows the session itself, and the zoned ISO stamp parses the same way.
  const view2 = render(<MarketSessionCard market={market({ quoteDatetime: beijingStamp(2, true) })} />);
  expect(screen.getByRole("button", { name: "交易中，查看交易时间" })).toBeTruthy();
  view2.unmount();
});

test("回放里不按墙钟数“N 秒无新报价”：历史报价时间与现在比永远是几天前", () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-04T14:00:00+08:00"));
  const replayQuote = market({ quoteDatetime: "2026-09-29 21:00:59" });
  render(<MarketSessionCard market={replayQuote} replayActive />);
  expect(screen.queryByText(/秒无新报价/)).toBeNull();
});

test("外盘（CME）读快照：时间卡写快照带的 CME 时段不写日盘夜盘，停盘写原因与恢复时间", () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-21T17:19:45+08:00"));
  const hours = { day: [["06:00", "29:00"]] as [string, string][], night: [], dayRule: "cme" as const };
  const daily = market({ symbol: "KQD.m@CME.NQ", name: "迷你纳指主连", source: "MT5 · ENQZ26", quoteDatetime: beijingStamp(1), tradingSchedule: hours });
  const view = render(<MarketSessionCard market={daily} />);
  expect(screen.getByRole("button", { name: "交易中，查看交易时间" })).toBeTruthy();
  const section = screen.getByTestId("market-session-cme");
  expect(section).toHaveTextContent("交易06:00–次日 05:00");
  expect(section).toHaveTextContent("每日停盘05:00–06:00");
  expect(screen.queryByText("夜盘")).toBeNull();
  view.unmount();

  vi.setSystemTime(new Date("2026-10-22T05:10:00+08:00"));
  render(<MarketSessionCard market={market({ symbol: "KQD.m@CME.NQ", name: "迷你纳指主连", source: "MT5 · ENQZ26", tradingSchedule: hours,
    marketSessionStatus: "closed", sessionClosedFor: "break", sessionReopensAt: "2026-10-22T06:00+08:00" }, { marketSessionStatus: "closed" })} />);
  expect(screen.getByRole("button", { name: "CME 每日停盘，查看交易时间" }).className).toContain("closed");
  expect(screen.getByText("CME 每日停盘 · 06:00 恢复")).toBeTruthy();
});

test("外盘谷物（F2）：时间卡写两段交易与盘中休息，休息时标出当前一行", () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-21T20:50:00+08:00"));
  const grains = { day: [["08:00", "20:45"], ["21:30", "26:20"]] as [string, string][], night: [], dayRule: "cme" as const, cmeHours: "grains" as const };
  render(<MarketSessionCard market={market({ symbol: "KQD.m@CBOT.ZS", name: "美豆主连", tradingSchedule: grains,
    marketSessionStatus: "closed", sessionClosedFor: "intraday", sessionReopensAt: "2026-10-21T21:30+08:00" }, { marketSessionStatus: "closed" })} />);
  expect(screen.getByRole("button", { name: "盘中休息，查看交易时间" })).toBeTruthy();
  const section = screen.getByTestId("market-session-cme");
  expect(section).toHaveTextContent("交易08:00–20:45盘中休息20:45–21:30交易21:30–次日 02:20每日停盘02:20–08:00周末休市周六 02:20–周一 08:00");
  expect(screen.getByText("盘中休息", { selector: ".current span" })).toBeTruthy();
});

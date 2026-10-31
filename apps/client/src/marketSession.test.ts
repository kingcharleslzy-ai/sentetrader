import { describe, expect, test } from "vitest";
import type { MarketWorkbenchSnapshot } from "./api";
import { chartQuoteBlockedReason, cmeHours, marketSessionDisplay } from "./marketSession";

const at = (text: string) => new Date(text).getTime();
/** A Beijing wall-clock stamp `seconds` before `now`, the way the TqKq sidecar writes it. */
const naive = (now: number, seconds: number) => new Date(now - seconds * 1000 + 8 * 3600_000).toISOString().slice(0, 19).replace("T", " ");

const market = (now: number, quote: Partial<MarketWorkbenchSnapshot["quote"]>, over: Partial<MarketWorkbenchSnapshot> = {}): MarketWorkbenchSnapshot => ({
  schemaVersion: 1, durationSeconds: 86400, capturedAt: new Date(now).toISOString(), ageSeconds: 0.5,
  dataStatus: "normal", marketSessionStatus: "trading", isCached: false, liveTrading: "disabled",
  quote: {
    symbol: "SHFE.au2612", name: "沪金2612", quoteDatetime: naive(now, 1), lastPrice: 824, changePercent: 0.7,
    bidPrice1: null, bidVolume1: null, askPrice1: null, askVolume1: null, open: null, highest: null, lowest: null,
    volume: null, openInterest: null, priceTick: null, volumeMultiple: null, upperLimit: null, lowerLimit: null, ...quote,
  },
  bars: [], ...over,
});

/** A 外盘 chart's snapshot as the Runtime sends it (foreign_market.cme_chart): CME hours, CME status, MT5's quote. */
const CME = { day: [["06:00", "29:00"]] as [string, string][], night: [], dayRule: "cme" as const };
const cme = (now: number, quote: Partial<MarketWorkbenchSnapshot["quote"]> = {}, over: Partial<MarketWorkbenchSnapshot> = {}) =>
  market(now, { symbol: "KQD.m@CME.NQ", name: "迷你纳指主连", source: "MT5 · ENQZ26", tradingSchedule: CME, ...quote }, over);

describe("外盘（CME）读快照", () => {
  test("时段取自快照：北京时间开盘与收盘", () => {
    expect(cmeHours(CME)).toEqual({ periods: [{ start: "06:00", end: "次日 05:00" }], opens: "06:00", closes: "05:00" });
    expect(cmeHours({ day: [["07:00", "30:00"]], night: [], dayRule: "cme" })).toMatchObject({ opens: "07:00", closes: "06:00" });
    expect(cmeHours({ day: [["00:00", "23:59"]], night: [] })).toBeUndefined(); // 只接了参考行情的外盘主连
    // F2：CBOT 谷物两段（芝加哥 19:00–次日 07:45、08:30–13:20，夏令时北京 08:00–20:45、21:30–次日 02:20）
    expect(cmeHours({ day: [["08:00", "20:45"], ["21:30", "26:20"]], night: [], dayRule: "cme", cmeHours: "grains" })).toEqual({
      periods: [{ start: "08:00", end: "20:45" }, { start: "21:30", end: "次日 02:20" }], opens: "08:00", closes: "02:20" });
  });

  test("谷物盘中休息写原因与恢复时间", () => {
    const now = at("2026-10-21T20:50:00+08:00");
    const grains = { day: [["08:00", "20:45"], ["21:30", "26:20"]] as [string, string][], night: [], dayRule: "cme" as const, cmeHours: "grains" as const };
    const paused = market(now, { symbol: "KQD.m@CBOT.ZS", name: "美豆主连", tradingSchedule: grains, marketSessionStatus: "closed",
      sessionClosedFor: "intraday", sessionReopensAt: "2026-10-21T21:30+08:00" }, { marketSessionStatus: "closed" });
    expect(marketSessionDisplay(paused)).toEqual({ label: "盘中休息", detail: "盘中休息 · 21:30 恢复" });
    expect(chartQuoteBlockedReason(paused, false, now)).toBe("盘中休息，暂不可下单");
  });

  test("停盘原因与恢复时间取自快照", () => {
    const pause = at("2026-10-22T05:10:00+08:00");
    const paused = cme(pause, { marketSessionStatus: "closed", sessionClosedFor: "break", sessionReopensAt: "2026-10-22T06:00+08:00" },
      { marketSessionStatus: "closed" });
    expect(marketSessionDisplay(paused)).toEqual({ label: "CME 每日停盘", detail: "CME 每日停盘 · 06:00 恢复" });
    expect(chartQuoteBlockedReason(paused, false, pause)).toBe("CME 每日停盘，暂不可下单");
    const saturday = at("2026-10-24T12:00:00+08:00");
    const weekend = cme(saturday, { sessionClosedFor: "weekend", sessionReopensAt: "2026-10-26T06:00+08:00" }, { marketSessionStatus: "closed" });
    expect(marketSessionDisplay(weekend).detail).toBe("周末休市 · 周一 06:00 开盘");
    expect(chartQuoteBlockedReason(weekend, false, saturday)).toBe("周末休市，暂不可下单");
    const christmas = at("2026-12-25T12:00:00+08:00");
    const holiday = cme(christmas, { sessionClosedFor: "holiday", sessionReopensAt: "2026-12-28T07:00+08:00" }, { marketSessionStatus: "closed" });
    expect(marketSessionDisplay(holiday)).toEqual({ label: "CME 休市", detail: "CME 节假日休市 · 周一 07:00 开盘" });
    expect(chartQuoteBlockedReason(holiday, false, christmas)).toBe("CME 休市，暂不可下单");
  });
});

describe("下单票的行情条件", () => {
  test("外盘日线的报价是 MT5 月份的：新鲜就不挡，停走照样按秒数挡", () => {
    const now = at("2026-10-21T17:19:45+08:00");
    const daily = cme(now, { quoteDatetime: "2026-10-21T17:19:44+08:00" });
    expect(chartQuoteBlockedReason(daily, false, now)).toBeUndefined();
    expect(marketSessionDisplay(daily).label).toBe("交易中");
    const quiet = cme(now, { quoteDatetime: "2026-10-21T17:19:15+08:00" });
    expect(chartQuoteBlockedReason(quiet, false, now)).toBe("行情已 30 秒无新报价");
  });

  test("国内判定不变", () => {
    const now = at("2026-10-21T17:19:45+08:00");
    expect(chartQuoteBlockedReason(market(now, { quoteDatetime: naive(now, 20) }), false, now)).toBe("行情已 20 秒无新报价");
    expect(chartQuoteBlockedReason(market(now, {}, { marketSessionStatus: "closed" }), false, now)).toBe("休市，暂不可下单");
    expect(chartQuoteBlockedReason(market(now, { symbol: "KQD.m@LME.CA" }, { marketSessionStatus: "closed" }), false, now)).toBe("休市，暂不可下单");
  });
});

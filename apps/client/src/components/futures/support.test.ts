// @vitest-environment node
import { afterEach, expect, test, vi } from "vitest";
import type { InstrumentCatalog } from "../../api";
import { contractLabel, defaultDockWidth, foreignChartSource, formatAge, isProtectionActive, maxSharedDockWidth, orderStatusText, readSavedLayout, signClass } from "./support";

test("signClass: >0 红、<0 绿、0 与无值中性", () => {
  expect(signClass(1.5)).toBe("positive");
  expect(signClass(-0.5)).toBe("negative");
  expect(signClass(0)).toBe("");
  expect(signClass(undefined)).toBe("");
  expect(signClass(null)).toBe("");
  expect(signClass(Number.NaN)).toBe("");
  // Chat cards pass their own token pair; the rule is the same.
  expect(signClass(2, "positive-text", "danger-text")).toBe("positive-text");
  expect(signClass(-2, "positive-text", "danger-text")).toBe("danger-text");
  expect(signClass(0, "positive-text", "danger-text")).toBe("");
});

test("isProtectionActive: 只有仍在生效的状态算活动，EXECUTED 不计入", () => {
  // The server's ai_stop_orders statuses (store/base.py): terminal ones must not count.
  for (const active of ["ARMED", "TRIGGERING", "SUBMITTED", "PARTIAL", "UNKNOWN"]) {
    expect(isProtectionActive(active)).toBe(true);
  }
  expect(isProtectionActive("EXECUTED")).toBe(false);
  expect(isProtectionActive("CANCELLED")).toBe(false);
  expect(isProtectionActive("FAILED")).toBe(false);
  expect(isProtectionActive(undefined)).toBe(false);
});

function stubWindow(innerWidth: number, saved: unknown = null) {
  vi.stubGlobal("window", { innerWidth, localStorage: { getItem: () => saved === null ? null : JSON.stringify(saved) } });
}
afterEach(() => { vi.unstubAllGlobals(); });

test("对话栏默认宽度随窗口：1440→380、1920→480、2560 起 640 封顶，1280 以下仍是 340", () => {
  const cases: [number, number][] = [[1024, 340], [1279, 340], [1280, 380], [1440, 380], [1680, 420], [1920, 480], [2560, 640], [3840, 640]];
  for (const [width, expected] of cases) {
    stubWindow(width);
    expect(defaultDockWidth(), `innerWidth ${width}`).toBe(expected);
  }
  // A window too narrow to leave the chart 480 px gets less than the default, never under 280.
  stubWindow(860);
  expect(defaultDockWidth()).toBe(322);
  stubWindow(700);
  expect(defaultDockWidth()).toBe(280);
});

test("拖动上限：窗口一半，且图表至少留 480px", () => {
  for (const [width, expected] of [[1440, 720], [1920, 960], [2560, 1280], [3840, 1600], [1024, 486], [700, 280]] as const) {
    stubWindow(width);
    expect(maxSharedDockWidth(), `innerWidth ${width}`).toBe(expected);
  }
});

test("保存的宽度：用户拖过的保留（上限 1600，按窗口截断在使用处），没存过或存的是旧默认 340 都走随窗口的默认", () => {
  stubWindow(1920);
  expect(readSavedLayout().dockWidth).toBeNull();
  stubWindow(1920, { dockSide: "left", dockWidth: 340, strategyWidth: 900 });
  expect(readSavedLayout()).toEqual({ dockSide: "left", dockWidth: null, strategyWidth: 900 });
  stubWindow(1920, { dockSide: "right", dockWidth: null, strategyWidth: null });
  expect(readSavedLayout().dockWidth).toBeNull();
  stubWindow(1920, { dockWidth: 452 });
  expect(readSavedLayout().dockWidth).toBe(452);
  stubWindow(1920, { dockWidth: 900 });
  expect(readSavedLayout().dockWidth).toBe(900);
  stubWindow(1920, { dockWidth: 5000 });
  expect(readSavedLayout().dockWidth).toBe(1600);
  stubWindow(1920, { dockWidth: 100 });
  expect(readSavedLayout().dockWidth).toBe(280);
});

test("行情时效读成秒/分钟/小时/天，不再是 88813.7 秒（#712 C2）", () => {
  expect(formatAge(0.3)).toBe("0 秒前");
  expect(formatAge(12.6)).toBe("13 秒前");
  expect(formatAge(725)).toBe("12 分钟前");
  expect(formatAge(88_813.7)).toBe("1 天前");
  expect(formatAge(80_000)).toBe("22.2 小时前");
  expect(formatAge(Number.NaN)).toBe("—");
});

test("orderStatusText: 部分成交后已结束说清成交几手、撤了还是到期失效（#932）", () => {
  expect(orderStatusText({ displayStatus: "部分成交", filledVolume: 3, cancelPhase: "NONE" })).toBe("部分成交");
  expect(orderStatusText({ displayStatus: "部分成交后已结束", filledVolume: 3, cancelPhase: "CONFIRMED" })).toBe("部分成交 3 手后已撤");
  expect(orderStatusText({ displayStatus: "部分成交后已结束", filledVolume: 2, cancelPhase: "NONE" })).toBe("部分成交 2 手后已结束");
});

test("foreignChartSource: 分钟图写 MT5 月份；日线 K 线是快期主连、报价是 MT5 月份时两个都写（外盘盘点 D）", () => {
  expect(foreignChartSource("KQD.m@CME.NQ", "MT5 · ENQZ26", "MT5 · ENQZ26")).toBe("MT5 · ENQZ26");
  expect(foreignChartSource("KQD.m@CME.NQ", "MT5 · ENQZ26", undefined)).toBe("快期主连 · 报价 ENQZ26");
  expect(foreignChartSource("KQD.m@CME.NQ", undefined, undefined)).toBe("快期主连"); // MT5 不通
  expect(foreignChartSource("SHFE.au2612", undefined, undefined)).toBeUndefined();
});


test("an incomplete directory entry uses the cached Chinese label until its fresh name arrives", () => {
  const catalog: InstrumentCatalog = { schemaVersion: 1, capturedAt: "", dataStatus: "normal", isCached: false, count: 1,
    items: [{ symbol: "SHFE.rb2701", exchangeId: "SHFE", instrumentId: "rb2701", instrumentName: "", productId: "rb",
      priceTick: 1, volumeMultiple: 10, deliveryYear: 2027, deliveryMonth: 1, expireRestDays: 90, hasNight: true, isMain: true }],
    cachedNames: { "SHFE.RB2701": "螺纹2701" } };
  expect(contractLabel("SHFE.rb2701", catalog)).toBe("螺纹2701");
  catalog.items[0].instrumentName = "新螺纹2701";
  expect(contractLabel("SHFE.rb2701", catalog)).toBe("新螺纹2701");
  expect(contractLabel("SHFE.rb2701")).toBe("SHFE.rb2701");
});

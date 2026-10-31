// @vitest-environment node
import { expect, test } from "vitest";
import {
  DRAWING_TOOLS, anchorsOf, levelPrices, magnetPrice, measureText, placementPoints, pointsFor, resolveStyle,
  snapToTick, visibleOnPeriod, type ChartDrawing
} from "./chartDrawingModel";

const drawing = (over: Partial<ChartDrawing>): ChartDrawing => ({
  id: "d", symbol: "SHFE.au2612", kind: "hline", points: { price: 826 }, label: "", createdAt: "", updatedAt: "", ...over,
});

test("a first-version line reads and writes back in its own shape and keeps its original look", () => {
  // Protects 旧画线兼容: lines saved before 画线第二版 have no style and the three old point shapes.
  const old = [
    drawing({ kind: "hline", points: { price: 826 } }),
    drawing({ kind: "trendline", points: { t1: 1790000000, p1: 820, t2: 1790003600, p2: 828.5 } }),
    drawing({ kind: "zone", points: { low: 820, high: 824 } }),
  ];
  for (const item of old) expect(pointsFor(item.kind, anchorsOf(item))).toEqual(item.points);
  expect(resolveStyle(old[0])).toMatchObject({ color: "user", width: 1, dash: "solid", extendLeft: false, extendRight: false, showPrice: true, locked: false, hidden: false });
  expect(resolveStyle(old[1])).toMatchObject({ color: "user", width: 1.5, extendLeft: false, extendRight: false, showPrice: true });
  expect(resolveStyle(old[2])).toMatchObject({ width: 1, showPrice: true });
  expect(old.every(item => visibleOnPeriod(item, 300) && visibleOnPeriod(item, 0))).toBe(true);
});

test("every tool's clicks land as its stored points with prices on the tick grid", () => {
  // Protects 每种工具按锚点正确落位，价格按最小变动价位取整 (au ticks by 0.02, rb by 1).
  const clicks = [{ t: 1790000000, p: 817.987 }, { t: 1790000300, p: 821.013 }, { t: 1790000600, p: 819.499 }];
  for (const spec of DRAWING_TOOLS) {
    const anchors = clicks.slice(0, spec.min);
    const points = placementPoints(spec.kind, anchors, 0.02);
    const prices = spec.kind === "hline" ? [points.price!] : spec.kind === "zone" ? [points.low!, points.high!]
      : spec.kind === "trendline" ? [points.p1!, points.p2!] : points.anchors!.flatMap(anchor => anchor.p === undefined ? [] : [anchor.p]);
    for (const price of prices) expect(Math.abs(price / 0.02 - Math.round(price / 0.02))).toBeLessThan(1e-9);
    if (["hline", "zone", "trendline"].includes(spec.kind)) continue;
    expect(points.anchors).toHaveLength(spec.min);
    expect(points.anchors!.map(anchor => anchor.t)).toEqual(anchors.map(anchor => anchor.t));
  }
  expect(placementPoints("hline", [clicks[0]], 0.02)).toEqual({ price: 817.98 });
  expect(placementPoints("zone", [clicks[1], clicks[0]], 0.02)).toEqual({ low: 817.98, high: 821.02 });
  expect(placementPoints("trendline", clicks.slice(0, 2), 1)).toEqual({ t1: 1790000000, p1: 818, t2: 1790000300, p2: 821 });
  expect(placementPoints("vline", [clicks[0]], 1)).toEqual({ anchors: [{ t: 1790000000 }] });   // a 垂直线 keeps only its time
  expect(placementPoints("fib", clicks.slice(0, 2), 1, [0, 0.5, 1])).toEqual({ anchors: [{ t: 1790000000, p: 818 }, { t: 1790000300, p: 821 }], levels: [0, 0.5, 1] });
  expect(snapToTick(4212.93, 0.02)).toBe(4212.94);
  expect(snapToTick(817.98, null)).toBe(817.98);
  // 磁吸: the nearest of the bar's open / high / low / close.
  expect(magnetPrice(823.1, { open: 820, high: 825, low: 818, close: 824 })).toBe(824);
  expect(magnetPrice(818.6, { open: 820, high: 825, low: 818, close: 824 })).toBe(818);
});

test("在所有周期显示 off pins a drawing to the period it was drawn on; the 分时图 is period 0", () => {
  const pinned = drawing({ style: { allPeriods: false, period: 300 } });
  expect(visibleOnPeriod(pinned, 300)).toBe(true);
  expect(visibleOnPeriod(pinned, 900)).toBe(false);
  expect(visibleOnPeriod(drawing({ style: { allPeriods: false, period: 0 } }), 0)).toBe(true);
  expect(visibleOnPeriod(drawing({ style: { allPeriods: true, period: 300 } }), 900)).toBe(true);
});

test("ratios, the measure readout and a line's alert price read the way the Runtime receipt does", () => {
  const fib = drawing({ kind: "fib", points: { anchors: [{ t: 1, p: 800 }, { t: 2, p: 840 }] } });
  expect(levelPrices(fib).map(level => [level.text, Number(level.price.toFixed(2))])).toEqual([
    ["0%", 840], ["23.6%", 830.56], ["38.2%", 824.72], ["50%", 820], ["61.8%", 815.28], ["100%", 800]]);
  expect(levelPrices(drawing({ kind: "gann", points: { anchors: [{ t: 1, p: 800 }, { t: 2, p: 880 }] } }))[4]).toMatchObject({ text: "4/8", price: 840 });
  expect(measureText([{ t: 100, p: 3000 }, { t: 400, p: 3012 }], [{ time: 100 }, { time: 200 }, { time: 300 }, { time: 400 }], 1)).toBe("+12（+0.40%）· 3 根");
});

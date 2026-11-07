import { afterEach, expect, it, vi } from "vitest";
import { applyChartAnnotations, chartAnnotationsFor, chartMarksClearedAt, clearChartMarks, clearHighlights, currentBacktestMarks, currentHighlights, resetAiAnnotations, setBacktestMarks, setChartAnnotationsScope, setChartMarksScope, showHighlight } from "./aiAnnotations";

afterEach(() => { resetAiAnnotations(); document.body.innerHTML = ""; vi.useRealTimers(); });

it("a highlight needs a rendered target, keeps three at most and expires on its own", () => {
  vi.useFakeTimers();
  document.body.innerHTML = '<button data-agent-target="rail:ai"></button><div data-testid="positions-bar"></div><section data-region="chart.main"></section><i data-agent-target="header"></i>';
  expect(() => showHighlight({ target: "nowhere", durationMs: 2000 })).toThrow("界面上没有这个目标");
  showHighlight({ target: "rail:ai", label: "这里", durationMs: 2000 });
  showHighlight({ target: "positions-bar", durationMs: 5000 });
  showHighlight({ target: "chart.main", style: "outline", durationMs: 5000 });
  showHighlight({ target: "header", durationMs: 5000 });
  expect(currentHighlights().map(item => item.target)).toEqual(["positions-bar", "chart.main", "header"]);
  vi.advanceTimersByTime(5001);
  expect(currentHighlights()).toEqual([]);
  showHighlight({ target: "rail:ai", durationMs: 2000 });
  clearHighlights("rail:ai");
  expect(currentHighlights()).toEqual([]);
});

it("chart annotations are per contract: set replaces, add merges by id, clear empties, six at most, one line per level", () => {
  const hline = (id: string, price: number) => ({ id, kind: "hline" as const, price, tone: "info" as const });
  applyChartAnnotations("SHFE.rb2610", "set", [hline("a", 1), hline("b", 2)]);
  applyChartAnnotations("SHFE.rb2610", "add", [hline("b", 3), hline("c", 4)]);
  expect(chartAnnotationsFor("shfe.rb2610").map(item => [item.id, item.price])).toEqual([["a", 1], ["b", 3], ["c", 4]]);
  applyChartAnnotations("SHFE.au2612", "set", [hline("x", 9)]);
  expect(chartAnnotationsFor("SHFE.au2612")).toHaveLength(1);
  // A second line at the same level of the same pane is refused; the same level on MACD is a different line.
  expect(() => applyChartAnnotations("SHFE.rb2610", "add", [hline("dup", 4)])).toThrow("同一价位 4 已有一条线");
  applyChartAnnotations("SHFE.rb2610", "add", [{ ...hline("m", 4), pane: "macd" as const }]);
  expect(chartAnnotationsFor("SHFE.rb2610").map(item => item.id)).toEqual(["a", "b", "c", "m"]);
  expect(() => applyChartAnnotations("SHFE.rb2610", "add", Array.from({ length: 3 }, (_, i) => hline(`n${i}`, 10 + i)))).toThrow("最多 6 条");
  applyChartAnnotations("SHFE.rb2610", "clear");
  expect(chartAnnotationsFor("SHFE.rb2610")).toEqual([]);
  expect(chartAnnotationsFor(undefined)).toEqual([]);
});

it("drawings are per account/replay while clear stamps stay per user, no scope keeps memory only", () => {
  const prefix = "ai-trader:chart-marks-cleared:";
  setChartMarksScope("scope-a");
  setChartAnnotationsScope("scope-a:account-main");
  expect(chartMarksClearedAt("SHFE.au2612")).toBeNull();
  clearChartMarks("SHFE.au2612");
  expect(localStorage.getItem(`${prefix}scope-a:SHFE.AU2612`)).toBeTruthy();
  applyChartAnnotations("SHFE.au2612", "set", [{ id: "main", kind: "hline", price: 800 }]);
  setBacktestMarks({ backtestId: "main", symbol: "SHFE.au2612", durationSeconds: 60, items: [] });
  setChartMarksScope("scope-b");
  setChartAnnotationsScope("scope-a:replay-one");
  expect(chartMarksClearedAt("SHFE.au2612")).toBeNull();
  expect(chartAnnotationsFor("SHFE.au2612")).toEqual([]);
  expect(currentBacktestMarks()).toBeNull();
  applyChartAnnotations("SHFE.au2612", "set", [{ id: "replay", kind: "hline", price: 900 }]);
  setBacktestMarks({ backtestId: "replay", symbol: "SHFE.au2612", durationSeconds: 60, items: [] });
  setChartAnnotationsScope("scope-a:account-main");
  expect(chartAnnotationsFor("SHFE.au2612").map(item => item.id)).toEqual(["main"]);
  expect(currentBacktestMarks()?.backtestId).toBe("main");
  setChartAnnotationsScope("scope-a:replay-one");
  expect(chartAnnotationsFor("SHFE.au2612").map(item => item.id)).toEqual(["replay"]);
  expect(currentBacktestMarks()?.backtestId).toBe("replay");
  clearChartMarks("SHFE.au2612");
  expect(currentBacktestMarks()).toBeNull();
  setChartAnnotationsScope("scope-a:account-main");
  expect(chartAnnotationsFor("SHFE.au2612").map(item => item.id)).toEqual(["main"]);
  expect(currentBacktestMarks()?.backtestId).toBe("main");
  localStorage.removeItem(`${prefix}scope-b:SHFE.AU2612`);
  setChartMarksScope(undefined);
  setChartAnnotationsScope(undefined);
  expect(chartAnnotationsFor("SHFE.au2612")).toEqual([]);
  clearChartMarks("SHFE.au2612");
  expect(chartMarksClearedAt("SHFE.au2612")).toBeTruthy();
  expect(Object.keys(localStorage).filter(key => key.startsWith(prefix))).toEqual([`${prefix}scope-a:SHFE.AU2612`]);
  setChartMarksScope("scope-a");
  expect(chartMarksClearedAt("SHFE.au2612")).toBeTruthy();
});

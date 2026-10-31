import { afterEach, expect, test, vi } from "vitest";
import type { IChartApi } from "lightweight-charts";
import { refitWhenWidened } from "./chartRefit";

afterEach(() => { vi.unstubAllGlobals(); });

test("加宽后第一根 K 线左边空出来时重新铺满；翻在历史里的图不动（#751 C / #759）", () => {
  let notify: () => void = () => undefined;
  vi.stubGlobal("ResizeObserver", class { constructor(callback: () => void) { notify = callback; } observe() {} disconnect() {} });
  vi.stubGlobal("requestAnimationFrame", (run: () => void) => { run(); return 1; });
  vi.stubGlobal("cancelAnimationFrame", () => undefined);
  const container = { clientWidth: 400 } as HTMLElement;
  let range: { from: number; to: number } = { from: -0.5, to: 30 };
  const chart = { timeScale: () => ({ getVisibleLogicalRange: () => range }) } as unknown as IChartApi;
  const refit = vi.fn();
  const stop = refitWhenWidened(container, chart, refit);
  // Narrow → wide with every bar in view: the bars sit on the right, the left is empty.
  (container as { clientWidth: number }).clientWidth = 1200;
  range = { from: -60, to: 30 };
  notify();
  expect(refit).toHaveBeenCalledTimes(1);
  // Scrolled into history (the first bar is off screen): a resize keeps the reader's view.
  (container as { clientWidth: number }).clientWidth = 900;
  range = { from: 120, to: 200 };
  notify();
  expect(refit).toHaveBeenCalledTimes(1);
  // A height-only change is not a widening.
  range = { from: -60, to: 30 };
  notify();
  expect(refit).toHaveBeenCalledTimes(1);
  stop();
});

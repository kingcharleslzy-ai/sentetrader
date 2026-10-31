import type { IChartApi } from "lightweight-charts";

/**
 * lightweight-charts keeps the bar spacing when its width changes, so a chart that showed all of its
 * bars ends up squeezed to the right with the left side empty once the window, the dev tools or the
 * phone → desktop layout widens it (#751 C, #759). After a width change that leaves room left of the
 * first bar, `refit` fills the width again; a chart scrolled into its history is left as it is.
 */
export function refitWhenWidened(container: HTMLElement, chart: IChartApi, refit: () => void, skip?: () => boolean): () => void {
  if (typeof ResizeObserver !== "function") return () => undefined;
  let width = container.clientWidth;
  let frame = 0;
  const observer = new ResizeObserver(() => {
    if (container.clientWidth === width) return;
    width = container.clientWidth;
    cancelAnimationFrame(frame);
    // On the next frame: the chart's own autoSize has applied the new width by then.
    frame = requestAnimationFrame(() => {
      const range = chart.timeScale().getVisibleLogicalRange();
      if (range && range.from < -1 && !skip?.()) refit();
    });
  });
  observer.observe(container);
  return () => { cancelAnimationFrame(frame); observer.disconnect(); };
}

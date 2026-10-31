import { afterEach, expect, it, vi } from "vitest";
import { currentHighlights, resetAiAnnotations } from "./aiAnnotations";
import { nextTourStep, prevTourStep, registerTourFocus, resetTour, startTour, stopTour, tourState, type TourFocus } from "./uiTour";

afterEach(() => { resetTour(); resetAiAnnotations(); document.body.innerHTML = ""; });

it("a tour walks its steps: focus first, then the highlight, with the indicator state; stop or the last next ends it", () => {
  document.body.innerHTML = '<button data-agent-target="rail:order"></button><div data-testid="positions-bar"></div>';
  const focused: TourFocus[] = [];
  registerTourFocus(focus => focused.push(focus));
  startTour([{ target: "rail:order", label: "这里下单", focus: { panel: "order", ticketMode: "open" } }, { target: "positions-bar", label: "底栏" }]);
  expect(tourState()).toMatchObject({ active: true, step: 1, total: 2, current: { target: "rail:order" } });
  expect(focused).toEqual([{ panel: "order", ticketMode: "open" }]);
  expect(currentHighlights().map(item => item.target)).toEqual(["rail:order"]);
  nextTourStep();
  expect(tourState()).toMatchObject({ step: 2, current: { target: "positions-bar" } });
  expect(currentHighlights().map(item => item.target)).toEqual(["positions-bar"]);
  prevTourStep();
  expect(tourState().step).toBe(1);
  expect(() => prevTourStep()).toThrow("已经是第一步");
  nextTourStep(); nextTourStep();  // past the last step the tour ends
  expect(tourState()).toMatchObject({ active: false, step: 0, total: 0, current: null });
  expect(currentHighlights()).toEqual([]);
  expect(() => nextTourStep()).toThrow("当前没有进行中的引导");
  startTour([{ target: "positions-bar", label: "底栏" }]);
  stopTour();
  expect(tourState().active).toBe(false);
});

it("a tour refuses an empty course, more than twenty steps, and a first step whose target is not on screen", () => {
  vi.useFakeTimers();
  expect(() => startTour([])).toThrow("至少要有一步");
  expect(() => startTour(Array.from({ length: 21 }, (_, i) => ({ target: `t${i}`, label: "x" })))).toThrow("最多 20 步");
  expect(() => startTour([{ target: "nowhere", label: "x" }])).toThrow("界面上没有这个目标");
  vi.useRealTimers();
});

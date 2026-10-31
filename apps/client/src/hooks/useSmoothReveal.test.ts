import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { REVEAL_FINISH_MS, REVEAL_WINDOW_MS, useSmoothReveal } from "./useSmoothReveal";

const FRAME = 16;

beforeEach(() => { vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame", "setTimeout", "clearTimeout"] }); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

function stream(initial: string) {
  return renderHook(({ text, streaming }) => useSmoothReveal(text, streaming), { initialProps: { text: initial, streaming: true } });
}

/** Advance frame by frame, returning the shown length after each frame. */
function frames(view: ReturnType<typeof stream>, ms: number): number[] {
  const lengths: number[] = [];
  for (let elapsed = 0; elapsed < ms; elapsed += FRAME) {
    act(() => { vi.advanceTimersByTime(FRAME); });
    lengths.push(view.result.current.length);
  }
  return lengths;
}

test("a reply already complete when it renders (history) shows in full at once", () => {
  const { result } = renderHook(() => useSmoothReveal("螺纹偏多，回踩 3150 可考虑。", false));
  expect(result.current).toBe("螺纹偏多，回踩 3150 可考虑。");
  expect(vi.getTimerCount()).toBe(0);
});

test("under reduced motion a streaming reply shows the full text at once", () => {
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({ matches: query.includes("reduced-motion"), addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  const view = stream("");
  view.rerender({ text: "第一段已经到了", streaming: true });
  expect(view.result.current).toBe("第一段已经到了");
  expect(vi.getTimerCount()).toBe(0);
});

test("each chunk types out monotonically within about one delta interval, and the loop stops when caught up", () => {
  const view = stream("");
  expect(view.result.current).toBe("");
  const first = "螺纹今天偏多，回踩三千一百五十一线可以考虑。";
  view.rerender({ text: first, streaming: true });
  const lengths = frames(view, REVEAL_WINDOW_MS + 3 * FRAME);
  expect(lengths.some(length => length > 0 && length < first.length)).toBe(true);
  lengths.reduce((previous, length) => { expect(length).toBeGreaterThanOrEqual(previous); return length; }, 0);
  expect(view.result.current).toBe(first);
  expect(vi.getTimerCount()).toBe(0);

  const second = `${first}跌破三千一百就作废。`;
  view.rerender({ text: second, streaming: true });
  const more = frames(view, REVEAL_WINDOW_MS + 3 * FRAME);
  expect(Math.min(...more)).toBeGreaterThanOrEqual(first.length);
  expect(view.result.current).toBe(second);
});

test("a large backlog types faster instead of being held back", () => {
  const short = stream("");
  short.rerender({ text: "一".repeat(12), streaming: true });
  const shortShown = frames(short, 5 * FRAME).at(-1)!;
  const long = stream("");
  long.rerender({ text: "一".repeat(600), streaming: true });
  const longShown = frames(long, 5 * FRAME).at(-1)!;
  expect(longShown).toBeGreaterThan(shortShown * 10);
  frames(long, REVEAL_WINDOW_MS);
  expect(long.result.current.length).toBe(600);
});

test("once the reply completes the rest shows within the finish window", () => {
  const view = stream("");
  const text = "结论：偏多。\n为什么：持仓与基差同向。\n风险：跌破 3100 作废。".repeat(8);
  view.rerender({ text, streaming: true });
  frames(view, 2 * FRAME);
  expect(view.result.current.length).toBeLessThan(text.length);
  view.rerender({ text, streaming: false });
  frames(view, REVEAL_FINISH_MS + 2 * FRAME);
  expect(view.result.current).toBe(text);
  expect(vi.getTimerCount()).toBe(0);
});

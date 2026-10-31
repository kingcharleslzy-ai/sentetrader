import { useSyncExternalStore } from "react";
import { clearHighlights, showHighlight } from "./aiAnnotations";
import type { components } from "./workbench.generated";

/**
 * A guided tour (AI annotation layer, step two): an ordered list of steps, each opening a
 * surface (`focus`, a subset of UiChanges applied by the workspace) and highlighting a target
 * with a bubble. The step indicator lets the user go back and forth or end the tour; a click
 * anywhere only closes the bubble. The workspace reports `tour` in UiState and applies the
 * `tour` command; the 新手引导 entry starts the course the manual describes.
 */
export type TourStep = components["schemas"]["TourStep"];
export type TourFocus = NonNullable<TourStep["focus"]>;
export interface TourState { active: boolean; step: number; total: number; current: TourStep | null }
export const TOUR_MAX_STEPS = 20;
/** A tour step's highlight lives until the step changes; the store re-lights it when the user asks. */
const STEP_HIGHLIGHT_MS = 120_000;

const IDLE: TourState = { active: false, step: 0, total: 0, current: null };
let steps: TourStep[] = [];
let index = -1;
let state: TourState = IDLE;
let runFocus: ((focus: TourFocus) => void) | undefined;
const listeners = new Set<() => void>();
const notify = () => listeners.forEach(listener => listener());
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };

export function tourState(): TourState { return state; }
export function useTour(): TourState { return useSyncExternalStore(subscribe, tourState, tourState); }

/** The workspace hands the store the function that applies a step's `focus` (its UiChanges apply). */
export function registerTourFocus(apply: ((focus: TourFocus) => void) | undefined) { runFocus = apply; }

function show(at: number) {
  const step = steps[at];
  if (step.focus && runFocus) runFocus(step.focus);
  clearHighlights();
  // A target that opens with the step's focus may mount after this paint: retry once; without a
  // focus a missing target is the caller's mistake and the step (or the start) is refused.
  try { showHighlight({ target: step.target, label: step.label, style: "pulse", durationMs: STEP_HIGHLIGHT_MS }); }
  catch (error) {
    if (!step.focus) throw error;
    setTimeout(() => { if (index === at && state.active) try { showHighlight({ target: step.target, label: step.label, style: "pulse", durationMs: STEP_HIGHLIGHT_MS }); } catch { /* the indicator still names the step */ } }, 350);
  }
  index = at;
  state = { active: true, step: at + 1, total: steps.length, current: step };
  notify();
}

export function startTour(course: TourStep[]) {
  if (!course.length) throw new Error("引导至少要有一步");
  if (course.length > TOUR_MAX_STEPS) throw new Error(`引导最多 ${TOUR_MAX_STEPS} 步`);
  steps = course;
  index = -1;
  state = { active: true, step: 0, total: course.length, current: null };
  try { show(0); }
  catch (error) { steps = []; index = -1; state = IDLE; notify(); throw error; }
}

export function nextTourStep() {
  if (!state.active) throw new Error("当前没有进行中的引导");
  if (index + 1 >= steps.length) { stopTour(); return; }
  show(index + 1);
}

export function prevTourStep() {
  if (!state.active) throw new Error("当前没有进行中的引导");
  if (index <= 0) throw new Error("已经是第一步");
  show(index - 1);
}

export function stopTour() {
  if (!state.active && !steps.length) return;
  steps = []; index = -1; state = IDLE;
  clearHighlights();
  notify();
}

/** Show the current step's highlight again (the user's click only closed the bubble). */
export function relightTourStep() {
  if (!state.active || index < 0) return;
  try { showHighlight({ target: steps[index].target, label: steps[index].label, style: "pulse", durationMs: STEP_HIGHLIGHT_MS }); } catch { /* the surface moved on */ }
}

/** Test helper. */
export function resetTour() { steps = []; index = -1; state = IDLE; runFocus = undefined; notify(); }

import { useEffect, useRef, useState } from "react";

/** A chunk that just arrived types out in about one Runtime delta interval (deltas coalesce at 0.15 s). */
export const REVEAL_WINDOW_MS = 170;
/** Once the answer is complete, whatever is still hidden shows within this. */
export const REVEAL_FINISH_MS = 160;

const reducedMotion = () => typeof window !== "undefined" && Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);

/** A cut at `length` code units that does not split a surrogate pair. */
function cut(text: string, length: number): string {
  if (length >= text.length) return text;
  const code = text.charCodeAt(length - 1);
  return text.slice(0, code >= 0xd800 && code <= 0xdbff ? length + 1 : length);
}

function commonPrefix(a: string, b: string): number {
  const limit = Math.min(a.length, b.length);
  let index = 0;
  while (index < limit && a.charCodeAt(index) === b.charCodeAt(index)) index += 1;
  return index;
}

/**
 * The part of a streaming reply to show now. The Runtime sends the whole text so far every ~0.15 s;
 * shown as is, the reply jumps a phrase at a time. This walks the shown length toward the latest
 * text on animation frames so each new chunk has typed out by the time the next one is due: the speed
 * is the backlog over the remaining window, so a large backlog types faster and nothing is held back
 * on purpose. When the reply completes, the rest shows within REVEAL_FINISH_MS.
 *
 * Display only — the turn's text in state is untouched. A reply that was already complete when it
 * first rendered (history) and every reply under reduced motion show the full text at once. The frame
 * loop only runs while the shown text lags behind, so only the reply still arriving costs anything.
 */
export function useSmoothReveal(text: string, streaming: boolean): string {
  const [animated] = useState(() => streaming && !reducedMotion());
  const [shown, setShown] = useState(0);
  const motion = useRef({ position: 0, target: 0, deadline: 0, previous: 0, reschedule: true, frame: 0, text: "" });

  useEffect(() => {
    if (!animated) return;
    const state = motion.current;
    // A rewritten reply (rare) keeps only the part that still matches.
    const keep = commonPrefix(state.text, text);
    if (state.position > keep) state.position = keep;
    state.text = text;
    state.target = text.length;
    state.reschedule = true;
    if (state.frame) { cancelAnimationFrame(state.frame); state.frame = 0; }
    if (state.position >= state.target) {
      state.previous = 0;
      setShown(Math.floor(state.position));
      return;
    }
    const step = (time: number) => {
      if (state.reschedule) {
        const due = time + (streaming ? REVEAL_WINDOW_MS : REVEAL_FINISH_MS);
        // Finishing never waits longer than the window already promised.
        state.deadline = streaming || !state.previous ? due : Math.min(state.deadline, due);
        state.reschedule = false;
      }
      const elapsed = state.previous ? Math.max(0, time - state.previous) : 0;
      state.previous = time;
      const remaining = state.deadline - time;
      state.position = remaining <= 0
        ? state.target
        : Math.min(state.target, state.position + (state.target - state.position) * Math.min(1, elapsed / remaining));
      setShown(Math.floor(state.position));
      if (state.position < state.target) state.frame = requestAnimationFrame(step);
      else { state.frame = 0; state.previous = 0; }
    };
    state.frame = requestAnimationFrame(step);
  }, [animated, text, streaming]);

  useEffect(() => () => {
    const state = motion.current;
    if (state.frame) cancelAnimationFrame(state.frame);
    state.frame = 0;
  }, []);

  if (!animated) return text;
  if (!streaming && shown >= text.length) return text;
  return cut(text, Math.min(shown, text.length));
}

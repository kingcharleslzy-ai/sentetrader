import { useCallback, useEffect, useRef, useState } from "react";

/** Finger travel counts half (damping), the content stops at PULL_MAX, and past PULL_ARM a release refreshes. */
export const PULL_DAMPING = 0.5;
export const PULL_MAX = 110;
export const PULL_ARM = 64;
/** Where the content rests while the read runs (the stone spins in the gap). */
const PULL_HOLD = 56;
/** Where it rests while the one-line offline note shows. */
const PULL_NOTE = 28;
/** The longest the content waits for a read before springing back; the read itself goes on (#516). */
export const PULL_TIMEOUT_MS = 8000;
/** How long the offline note stays. */
export const PULL_NOTE_MS = 1500;

export type PullPhase = "idle" | "pulling" | "armed" | "refreshing" | "offline";

const motionMs = (element: Element, token: string) => {
  const value = getComputedStyle(element).getPropertyValue(token).trim();
  return value.endsWith("ms") ? parseFloat(value) : value.endsWith("s") ? parseFloat(value) * 1000 : 0;
};

/**
 * 下拉刷新 (手机版, 2026-10-06) for one scroll region (AI 动态, 调研, 通知): it takes over only a
 * downward drag that starts at scrollTop 0 — a sideways swipe or an ordinary scroll is left alone,
 * and only the real pull calls preventDefault (touchmove is non-passive for that). The pull's
 * distance lives in the element's `--fdm-pull` (px, unitless) so the children follow the finger
 * without re-rendering; a release past PULL_ARM runs `onRefresh` (the page's own reads) and the
 * content springs back when it settles, failed or not — what was shown stays. Offline, a release
 * springs back at once with a short note (the reads resume by themselves on reconnect), and a read
 * still running after PULL_TIMEOUT_MS stops holding the content.
 */
export function usePullToRefresh(onRefresh: () => unknown, enabled = true) {
  const [element, setElement] = useState<HTMLElement | null>(null);
  const [phase, setPhaseState] = useState<PullPhase>("idle");
  const phaseRef = useRef<PullPhase>("idle");
  const refresh = useRef(onRefresh);
  refresh.current = onRefresh;
  const ref = useCallback((node: HTMLElement | null) => setElement(node), []);
  useEffect(() => {
    if (!element || !enabled) return;
    const setPhase = (next: PullPhase) => { if (phaseRef.current !== next) { phaseRef.current = next; setPhaseState(next); } };
    let start: { x: number; y: number } | null = null;
    let active = false;
    let distance = 0;
    let settleTimer = 0;
    let holdTimer = 0;
    let round = 0;
    const place = (value: number) => {
      distance = value;
      element.style.setProperty("--fdm-pull", String(Math.round(value)));
      element.dataset.pull = "";
    };
    const settle = () => {
      round += 1;  // a later end of the read this round was waiting for changes nothing
      window.clearTimeout(holdTimer);
      element.dataset.pullSettling = "";
      place(0);
      setPhase("idle");
      window.clearTimeout(settleTimer);
      settleTimer = window.setTimeout(() => {
        delete element.dataset.pullSettling;
        delete element.dataset.pull;
        element.style.removeProperty("--fdm-pull");
      }, motionMs(element, "--dur-3"));
    };
    // The pull loses its finger (a second one joins, #516): it ends here without refreshing and springs
    // back, so no offset stays behind and the next pull starts clean.
    const abandon = () => {
      start = null;
      if (!active) return;
      active = false;
      delete element.dataset.pulling;
      settle();
    };
    const onStart = (event: TouchEvent) => {
      if (active) { abandon(); return; }
      start = null;
      if (phaseRef.current === "refreshing" || event.touches.length !== 1 || element.scrollTop > 0) return;
      start = { x: event.touches[0].clientX, y: event.touches[0].clientY };
    };
    const onMove = (event: TouchEvent) => {
      if (event.touches.length !== 1) { abandon(); return; }
      if (!start) return;
      const dx = event.touches[0].clientX - start.x;
      const dy = event.touches[0].clientY - start.y;
      if (!active) {
        // Sideways or upward first: an ordinary gesture, not ours.
        if (Math.abs(dx) > Math.abs(dy) || dy < 0) { if (Math.abs(dx) > 6 || dy < -6) start = null; return; }
        if (dy < 6 || element.scrollTop > 0) return;
        active = true;
        round += 1;  // a new pull over the offline note: that note's timer no longer applies
        window.clearTimeout(holdTimer);
        delete element.dataset.pullSettling;
        window.clearTimeout(settleTimer);
        element.dataset.pulling = "";
      }
      event.preventDefault();
      place(Math.min(PULL_MAX, Math.max(0, dy - 6) * PULL_DAMPING));
      setPhase(distance >= PULL_ARM ? "armed" : "pulling");
    };
    const onEnd = () => {
      start = null;
      if (!active) return;
      active = false;
      delete element.dataset.pulling;
      if (distance < PULL_ARM) { settle(); return; }
      element.dataset.pullSettling = "";
      const current = ++round;
      const settleThis = () => { if (round === current) settle(); };
      if (typeof navigator !== "undefined" && navigator.onLine === false) {
        // The reads would only wait for the network: say so and let go now.
        place(PULL_NOTE);
        setPhase("offline");
        void Promise.resolve().then(() => refresh.current()).catch(() => undefined);
        holdTimer = window.setTimeout(settleThis, PULL_NOTE_MS);
        return;
      }
      place(PULL_HOLD);
      setPhase("refreshing");
      holdTimer = window.setTimeout(settleThis, PULL_TIMEOUT_MS);
      Promise.resolve().then(() => refresh.current()).catch(() => undefined).finally(settleThis);
    };
    element.addEventListener("touchstart", onStart, { passive: true });
    element.addEventListener("touchmove", onMove, { passive: false });
    element.addEventListener("touchend", onEnd);
    element.addEventListener("touchcancel", onEnd);
    return () => {
      element.removeEventListener("touchstart", onStart);
      element.removeEventListener("touchmove", onMove);
      element.removeEventListener("touchend", onEnd);
      element.removeEventListener("touchcancel", onEnd);
      round += 1;
      window.clearTimeout(settleTimer);
      window.clearTimeout(holdTimer);
      // Leaving the page mid-pull or mid-read: no offset, lock or spinner stays behind.
      for (const key of ["pull", "pulling", "pullSettling"]) delete element.dataset[key];
      element.style.removeProperty("--fdm-pull");
      phaseRef.current = "idle";
      setPhaseState("idle");
    };
  }, [element, enabled]);
  return { ref, phase };
}

/** The Sente stone above the pulled content: it turns with the distance, spins while reading. */
export function PullIndicator({ phase }: { phase: PullPhase }) {
  return (
    <div className="fdm-ptr" data-phase={phase} aria-hidden={phase === "idle" ? true : undefined} role={phase === "refreshing" || phase === "offline" ? "status" : undefined}>
      <span className="fdm-ptr-stone">
        <svg viewBox="0 0 30 30" aria-hidden="true"><circle cx="13" cy="17" r="8" className="fdm-ptr-big" /><circle cx="23" cy="8" r="4" className="fdm-ptr-small" /></svg>
      </span>
      <small>{phase === "armed" ? "松开刷新" : phase === "refreshing" ? "正在刷新…" : phase === "offline" ? "离线，联网后自动更新" : "下拉刷新"}</small>
    </div>
  );
}

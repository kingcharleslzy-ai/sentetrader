import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type HTMLAttributes, type ReactNode, type RefObject } from "react";

/** A motion token from theme.css in milliseconds (0 under reduced motion, which zeroes the tokens). */
export const motionMs = (token: string) => {
  const value = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  return value.endsWith("ms") ? parseFloat(value) : value.endsWith("s") ? parseFloat(value) * 1000 : 0;
};

/** How a screen gives way to the next: next / prev slide sideways (the new one from the right / left), push slides the new one in
 *  from the right over the old, pop slides the old one out to the right uncovering the new, fade crosses in place. */
export type SwapMotion = "next" | "prev" | "push" | "pop" | "fade";

/**
 * 换场 (动效方案, owner 2026-11-03): `children` shown as `id`. When `id` changes the old screen stays for --dur-2 and leaves while the new
 * one enters (`motion`; none, or reduced motion, swaps at once). The old screen is the element last rendered for it — not rendered again
 * with the new state, so it does not flash empty — keeps its scroll, takes no input and sits over the new one's box. Transform and
 * opacity only (theme.css `[data-swap-*]`).
 */
export function Swap({ id, motion, children, ...props }: { id: string; motion?: SwapMotion; children: ReactNode } & Omit<HTMLAttributes<HTMLDivElement>, "id" | "children">) {
  const last = useRef<{ id: string; node: ReactNode }>({ id, node: children });
  const [leaving, setLeaving] = useState<{ id: string; node: ReactNode; motion: SwapMotion } | null>(null);
  if (last.current.id !== id) {
    const out = motion && motionMs("--dur-2") > 0 ? { id: last.current.id, node: last.current.node, motion } : null;
    if (out || leaving) setLeaving(out);
  }
  last.current = { id, node: children };
  const current = useRef<HTMLDivElement>(null);
  const old = useRef<HTMLDivElement>(null);
  // The leaving screen is taken out of the flow: it keeps the box the new one now has.
  useLayoutEffect(() => {
    const from = current.current, to = old.current;
    if (!leaving || !from || !to) return;
    Object.assign(to.style, { top: `${from.offsetTop}px`, left: `${from.offsetLeft}px`, width: `${from.offsetWidth}px`, height: `${from.offsetHeight}px` });
  }, [leaving]);
  useEffect(() => {
    if (!leaving) return;
    const timer = window.setTimeout(() => setLeaving(null), motionMs("--dur-2"));
    return () => window.clearTimeout(timer);
  }, [leaving]);
  return <>
    {leaving ? <div key={leaving.id} {...props} ref={old} data-swap-exit={leaving.motion} inert aria-hidden="true">{leaving.node}</div> : null}
    <div key={id} {...props} ref={current} data-swap-enter={leaving?.motion}>{children}</div>
  </>;
}

/** The direction a `Swap` between ordered screens (tabs, 板块) takes: next for a later one, prev for an earlier one. */
export function useStepMotion(index: number): SwapMotion | undefined {
  const [step, setStep] = useState<{ index: number; motion?: SwapMotion }>({ index });
  if (step.index !== index) setStep({ index, motion: index > step.index ? "next" : "prev" });
  return step.motion;
}

/** 进出场: whether something that is `open` still shows, and whether it is on its way out (--dur-2 after it closed; none under reduced
 *  motion). For content that can still render from current state while it leaves; `Presence` keeps the last of it otherwise. */
export function usePresence(open: boolean): { shown: boolean; closing: boolean } {
  const [saved, setState] = useState({ open, closing: false });
  // The render that sees `open` change answers with the new state already (its caller may act on it before React renders again).
  const state = saved.open === open ? saved : { open, closing: !open && motionMs("--dur-2") > 0 };
  if (state !== saved) setState(state);
  useEffect(() => {
    if (!state.closing) return;
    const timer = window.setTimeout(() => setState(current => ({ ...current, closing: false })), motionMs("--dur-2"));
    return () => window.clearTimeout(timer);
  }, [state.closing]);
  return { shown: open || state.closing, closing: !open && state.closing };
}

/**
 * 进出场 (动效方案, owner 2026-11-03): `children` as given; when they go (null or false) the last of them stays for --dur-2 marked
 * `data-closing` (taking no input) so CSS plays the way out — theme.css `.ui-presence`, each sheet's own rule. The wrapper has no box
 * (display: contents), so what it holds lays out as before.
 */
const PresenceClosing = createContext(false);
/** Inside a `Presence`: whether it is on its way out — for content it renders elsewhere (a portal), which its CSS cannot reach. */
export const usePresenceClosing = () => useContext(PresenceClosing);

export function Presence({ children }: { children: ReactNode }) {
  const open = Boolean(children);
  const last = useRef<ReactNode>(null);
  if (open) last.current = children;
  const { shown, closing } = usePresence(open);
  if (!shown) last.current = null;
  return shown ? <PresenceClosing.Provider value={closing}>
    <div className="ui-presence" data-closing={closing ? "" : undefined}>{open ? children : last.current}</div>
  </PresenceClosing.Provider> : null;
}

/**
 * 原地进场 (电脑版换场, 2026-11-04): when `key` changes the element plays its way in — next / prev from the side it comes
 * from, fade in place — without remounting what it holds (a chart keeps its canvas; `Swap` would build it again). Transform
 * and opacity only, through the Web Animations API; nothing under reduced motion or on the first key.
 */
export function useEnterMotion(ref: RefObject<HTMLElement | null>, key: string, motion: SwapMotion | undefined) {
  const shown = useRef(key);
  useEffect(() => {
    if (key === shown.current) return;
    shown.current = key;
    const element = ref.current, ms = motionMs("--dur-2");
    if (!element || !ms || !motion || typeof element.animate !== "function") return;
    const from = motion === "next" || motion === "push" ? "translateX(24px)" : motion === "prev" || motion === "pop" ? "translateX(-24px)" : "none";
    const easing = getComputedStyle(document.documentElement).getPropertyValue("--ease-out").trim() || "ease-out";
    element.animate([{ opacity: motion === "fade" ? 0.35 : 0, transform: from }, { opacity: 1, transform: "none" }], { duration: ms, easing });
  }, [key, motion, ref]);
}

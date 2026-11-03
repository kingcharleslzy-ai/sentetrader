import { useEffect, useLayoutEffect, useRef, useState, type HTMLAttributes, type ReactNode } from "react";

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

import { Children, useEffect, useLayoutEffect, useRef, type ReactNode } from "react";
import { useDisclosureSurface } from "../uiSurfaces";

interface CardShellProps {
  icon: ReactNode;
  /** The one line a reader needs; conversation cards put the whole conclusion here. */
  title: string;
  /** The title with styled parts (same text as `title`, which stays the card's accessible name). */
  heading?: ReactNode;
  /** Secondary head line. Conversation cards leave it out and fold context into `details`. */
  meta?: string;
  /** Status pill at the right of the head; omit when the head carries an action instead. */
  badge?: string;
  badgeTone?: "neutral" | "positive" | "warning" | "danger";
  children?: ReactNode;
  actions?: ReactNode;
  /** One text action at the right of the head (expand / history / configure …). */
  headAction?: ReactNode;
  /** Folded "详情": identifiers, provenance and explanations that do not change the decision. */
  details?: ReactNode;
  /** The fold's summary text; "详情" unless the card names it (回答卡 / 数据卡: "依据"). */
  detailsLabel?: string;
  /** Conversation card id; registers the fold as `disclosure: card-details` for UI commands. */
  id?: string;
  testId?: string;
  /** `data-kind` on the card (数据卡: positions / orders / news / analysis), for tests and the coverage audit. */
  kind?: string;
  className?: string;
  /** A proposal card's stage, from its own status (for motion only): waiting, being sent, sent/recorded/applied, or given up. */
  phase?: CardPhase;
}

export type CardPhase = "open" | "busy" | "done" | "dropped";

const motionMs = (element: Element, token: string) => {
  const value = getComputedStyle(element).getPropertyValue(token).trim();
  return value.endsWith("ms") ? parseFloat(value) : value.endsWith("s") ? parseFloat(value) * 1000 : 0;
};

/**
 * A proposal card that settles while on screen: `data-settled="live"` lets CSS play the outcome once
 * (history cards that load settled stay still), and when the settled card is shorter — its buttons
 * are gone — it eases to the new height instead of jumping. Growth is not animated, so the
 * conversation's follow-the-bottom scrolling stays exact.
 */
function useSettleMotion(phase: CardPhase | undefined) {
  const ref = useRef<HTMLElement>(null);
  const mounted = useRef(phase);
  const height = useRef(0);
  const open = phase === "open" || phase === "busy";
  useEffect(() => {
    const element = ref.current;
    if (!open || !element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => { height.current = element.offsetHeight; });
    observer.observe(element);
    return () => observer.disconnect();
  }, [open]);
  useLayoutEffect(() => {
    if (open || !phase) return;
    const element = ref.current;
    const before = height.current;
    height.current = 0;
    if (!element || !before || typeof element.animate !== "function") return;
    const duration = motionMs(element, "--dur-3");
    const after = element.offsetHeight;
    if (!duration || after >= before) return;
    element.animate([{ height: `${before}px` }, { height: `${after}px` }],
      { duration, easing: getComputedStyle(element).getPropertyValue("--ease-out").trim() || "ease-out" });
  }, [open, phase]);
  const settledLive = (phase === "done" || phase === "dropped") && (mounted.current === "open" || mounted.current === "busy");
  return { ref, settledLive };
}

export function CardShell({
  icon,
  title,
  heading,
  meta,
  badge,
  badgeTone = "neutral",
  children,
  actions,
  headAction,
  details,
  detailsLabel = "详情",
  id,
  testId,
  kind,
  className,
  phase
}: CardShellProps) {
  // A collapsed card renders no body at all, so it never leaves a padded blank block.
  const body = Children.toArray(children);
  const detailBody = Children.toArray(details);
  const [open, setOpen] = useDisclosureSurface("card-details", { id, available: Boolean(id && detailBody.length) });
  const { ref, settledLive } = useSettleMotion(phase);
  return (
    <section ref={ref} className={className ? `card ${className}` : "card"} data-testid={testId} aria-label={title}
      data-card-id={id} data-kind={kind} data-phase={phase} data-settled={settledLive ? "live" : undefined}>
      <div className="card-head">
        <div className="card-title">
          <div className="card-icon" aria-hidden="true">
            {icon}
          </div>
          <div>
            <strong>{heading ?? title}</strong>
            {meta ? <small>{meta}</small> : null}
          </div>
        </div>
        {badge ? <span className={`card-badge ${badgeTone}`}>{badge}</span> : null}
        {headAction ? <div className="card-head-action">{headAction}</div> : null}
      </div>
      {body.length ? <div className="card-body">{body}</div> : null}
      {detailBody.length ? (
        <details className="card-details" open={open} onToggle={e => setOpen(e.currentTarget.open)} data-testid={testId ? `${testId}-details` : undefined}>
          <summary>{detailsLabel}</summary>
          <div>{detailBody}</div>
        </details>
      ) : null}
      {actions ? <div className="card-actions">{actions}</div> : null}
    </section>
  );
}

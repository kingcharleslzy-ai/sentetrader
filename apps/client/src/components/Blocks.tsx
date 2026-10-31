import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useDisclosureSurface } from "../uiSurfaces";
import "./Blocks.css";

/**
 * The shared blocks of a reading surface (版面原则, 2026-09-26: 策略详情 approved as the pattern for
 * every module). A surface is a stack of SectionCards — each one a bordered block with its own title
 * and its explanation behind a "?" (2026-10-08: explanations go behind a "?", the screen keeps only what
 * the reader needs while working) — instead of one flowing column; short figures go in StatTiles, rules in RuleRows.
 */
export function SectionCard({ title, hint, helpKey, note, actions, tone, label, testId, className, children }: {
  title: ReactNode;
  /** The block's explanation: the rule that applies to everything inside, said once, behind a "?" beside the title. */
  hint?: ReactNode;
  /** The "?"'s key (`disclosure: help-tip` id); defaults to `section:` + the block's name. */
  helpKey?: string;
  /**
   * A short line that stays visible beside the title: what the reader needs while working — the basis of the
   * figures inside (近 60 个交易日 · 扣费后), a date, a count. Not for explanations (those are `hint`).
   */
  note?: ReactNode;
  actions?: ReactNode;
  /** `warning`: a finding the reader should act on (体检). */
  tone?: "warning";
  /** Accessible name; defaults to the title when it is plain text. */
  label?: string;
  testId?: string;
  className?: string;
  children?: ReactNode;
}) {
  const name = label ?? (typeof title === "string" ? title : undefined);
  return <section className={`section-card${className ? ` ${className}` : ""}`} data-tone={tone} data-testid={testId} aria-label={name}>
    <header className="section-card-head">
      <h3>{title}</h3>
      {hint ? <HelpTip helpKey={helpKey ?? `section:${name ?? "block"}`} label={name ?? "这一块"}>{hint}</HelpTip> : null}
      {note ? <span className="section-card-hint">{note}</span> : null}
      {actions ? <div className="section-card-actions">{actions}</div> : null}
    </header>
    {children}
  </section>;
}

const TIP_WIDTH = 300;
const TIP_GUTTER = 8;
const HOVER_CLOSE_MS = 160;

/**
 * "?" (说明收进问号, 2026-10-08, one shared component for the whole app): a small round button whose popover says
 * what a block, field or control means. Hover or keyboard focus shows it, a click pins it (a second click, Escape
 * or a click elsewhere closes it); touch opens it with the tap. Commandable as `disclosure: help-tip` with the
 * tip's key as `id`, and reported in `state.disclosures` as `help-tip:<key>`. The popover is drawn in the document body
 * (or the modal dialog the "?" sits in, which is drawn above the body) at the "?"'s place on screen (so no scrolling
 * column or contained layout clips it) and closes when its column scrolls.
 */
export function HelpTip({ helpKey, label, children, className }: { helpKey: string; label: string; children: ReactNode; className?: string }) {
  const [open, setOpen] = useDisclosureSurface("help-tip", { id: helpKey });
  const pinned = useRef(false);
  // Escape hands focus back to the "?" without opening it again.
  const quiet = useRef(false);
  const anchor = useRef<HTMLSpanElement>(null);
  const pop = useRef<HTMLSpanElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [place, setPlace] = useState<{ left: number; top: number; width: number; above: boolean }>();
  const popId = `help-tip-${useId().replace(/:/g, "")}`;
  const show = () => { clearTimeout(closeTimer.current); setOpen(true); };
  const hide = () => { clearTimeout(closeTimer.current); closeTimer.current = setTimeout(() => { if (!pinned.current) setOpen(false); }, HOVER_CLOSE_MS); };
  const close = () => { clearTimeout(closeTimer.current); pinned.current = false; setOpen(false); };
  useEffect(() => () => clearTimeout(closeTimer.current), []);
  useEffect(() => { if (!open) pinned.current = false; }, [open]);
  useLayoutEffect(() => {
    if (!open || !button.current) return;
    const box = button.current.getBoundingClientRect();
    const viewWidth = window.innerWidth || document.documentElement.clientWidth || 1024;
    const viewHeight = window.innerHeight || document.documentElement.clientHeight || 768;
    const width = Math.max(160, Math.min(TIP_WIDTH, viewWidth - TIP_GUTTER * 2));
    const left = Math.min(Math.max(TIP_GUTTER, box.left), Math.max(TIP_GUTTER, viewWidth - TIP_GUTTER - width));
    const above = box.bottom + 160 > viewHeight && box.top > 160;
    setPlace({ left, top: above ? box.top - 4 : box.bottom + 4, width, above });
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const inside = (target: EventTarget | null) => Boolean(anchor.current?.contains(target as Node) || pop.current?.contains(target as Node));
    const outside = (event: PointerEvent) => { if (!inside(event.target)) close(); };
    // Escape closes only the popover: a "?" inside a modal dialog (设置) must not close the dialog with it.
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { event.preventDefault(); close(); quiet.current = true; button.current?.focus(); quiet.current = false; } };
    // A scrolling column moves the "?" away from its popover.
    const scrolled = (event: Event) => { if (!inside(event.target)) close(); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    document.addEventListener("scroll", scrolled, true);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); document.removeEventListener("scroll", scrolled, true); };
  }, [open]);   // eslint-disable-line react-hooks/exhaustive-deps
  return <span className={`help-tip${className ? ` ${className}` : ""}`} ref={anchor} onPointerEnter={event => { if (event.pointerType === "mouse") show(); }}
    onPointerLeave={event => { if (event.pointerType === "mouse") hide(); }}>
    <button type="button" ref={button} className="help-tip-button" aria-label={`${label}的说明`} aria-expanded={open} aria-controls={open ? popId : undefined}
      aria-describedby={open ? popId : undefined}
      onFocus={() => { if (!quiet.current) show(); }} onBlur={() => { if (!pinned.current) hide(); }}
      onClick={() => { if (open && pinned.current) close(); else { pinned.current = true; show(); } }}>?</button>
    {open ? createPortal(<span id={popId} ref={pop} role="tooltip" className="help-tip-pop" data-testid={`help-tip-${helpKey}`} data-above={place?.above || undefined}
      style={place ? { left: place.left, top: place.top, width: place.width } : undefined}
      onPointerEnter={event => { if (event.pointerType === "mouse") show(); }} onPointerLeave={event => { if (event.pointerType === "mouse") hide(); }}>{children}</span>,
    button.current?.closest("dialog") ?? document.body) : null}
  </span>;
}

export type StatTile = { label: string; value: ReactNode; note?: ReactNode };

/** Figures in a grid of cells: the label small, the value large (long values wrap at body size). */
export function StatTiles({ items, columns = 4, label }: { items: StatTile[]; columns?: 2 | 3 | 4; label?: string }) {
  if (!items.length) return null;
  return <dl className="stat-tiles" data-columns={columns} aria-label={label}>
    {items.map(item => <div key={item.label} className="stat-tile"
      data-long={typeof item.value === "string" && item.value.length > 10 ? "" : undefined}>
      <dt>{item.label}</dt>
      <dd>{item.value}{item.note ? <small>{item.note}</small> : null}</dd>
    </div>)}
  </dl>;
}

/** Two columns side by side (做多 | 做空, 平多 | 平空); one column fills the width. */
export function SideBySide({ children }: { children: ReactNode }) {
  return <div className="side-by-side">{children}</div>;
}

/**
 * One rule condition in one line: period tag, left operand, the verb highlighted, right operand, and
 * a short note (金叉 / 最近 3 根内). `kind`: `filter` draws it dashed; `flag` marks a condition the
 * health check points at. `reading` is the live status while the strategy is monitored.
 */
export function RuleRow({ period, left, op, right, note, kind, title, reading }: {
  period: string; left: string; op: string; right?: string; note?: string;
  kind?: "filter" | "flag"; title?: string;
  reading?: { passed: boolean; detail: string };
}) {
  return <div className="rule-row" data-kind={kind} title={title}>
    <span className="rule-row-period">{period}</span>
    <span className="rule-row-text">{left} <strong>{op}</strong>{right ? ` ${right}` : ""}{note ? <small>（{note}）</small> : null}
      {reading ? <span className="rule-row-reading" data-passed={reading.passed || undefined}>{reading.passed ? "✓ 满足" : "· 等待"} · {reading.detail}</span> : null}
    </span>
  </div>;
}

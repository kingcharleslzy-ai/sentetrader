import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useDisclosureSurface } from "../uiSurfaces";
import type { ParamHelp as Help } from "../strategyTemplates";
import "./ParamHelp.css";

const GUTTER = 8;
const WIDTH = 300;

/**
 * 说人话 (2026-09-25): the “?” beside a strategy parameter. A click (not a hover, so it works on a
 * phone) opens a small floating note — what the parameter is, what a larger and a smaller value do;
 * “?” again, a click elsewhere or Esc closes it. The note stays inside the nearest
 * `[data-popover-bounds]` (the strategy panel), so a narrow panel never scrolls sideways.
 * Commandable as `disclosure: {name: strategy-param-help, id: <parameter key>}`.
 */
export function ParamHelp({ paramKey, label, help }: { paramKey: string; label: string; help?: Help }) {
  const [open, setOpen] = useDisclosureSurface("strategy-param-help", { id: paramKey, available: Boolean(help) });
  const anchor = useRef<HTMLSpanElement>(null);
  const [place, setPlace] = useState<{ left: number; width: number }>();
  useLayoutEffect(() => {
    if (!open || !anchor.current) return;
    const box = anchor.current.getBoundingClientRect();
    const bounds = (anchor.current.closest("[data-popover-bounds]") ?? document.documentElement).getBoundingClientRect();
    const width = Math.max(0, Math.min(WIDTH, bounds.width - GUTTER * 2));
    let left = 0;
    if (box.left + width > bounds.right - GUTTER) left = bounds.right - GUTTER - width - box.left;
    if (box.left + left < bounds.left + GUTTER) left = bounds.left + GUTTER - box.left;
    setPlace({ left, width });
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!anchor.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [open, setOpen]);
  if (!help) return null;
  const id = `param-help-${paramKey}`;
  return <span className="param-help" ref={anchor}>
    <button type="button" className="param-help-button" aria-label={`${label}是什么`} aria-expanded={open} aria-controls={open ? id : undefined}
      onClick={() => setOpen(value => !value)}>?</button>
    {open && <div id={id} className="param-help-pop" role="group" aria-label={`${label}的说明`} data-testid={id}
      style={place ? { left: place.left, width: place.width } : undefined}>
      <p><b>是什么</b>{help.what}</p>
      {/* A term's “?” (结果页的规则四行, 2026-10-08) has only what it is. */}
      {help.up ? <p><b>调大</b>{help.up}</p> : null}
      {help.down ? <p><b>调小</b>{help.down}</p> : null}
    </div>}
  </span>;
}

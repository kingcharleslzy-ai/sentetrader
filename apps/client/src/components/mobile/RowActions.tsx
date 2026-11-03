import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type CSSProperties, type HTMLAttributes, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Presence, usePresence } from "../motion";
import { useMobileLayout } from "../../hooks/useMobileLayout";
import { useDisclosureSurface } from "../../uiSurfaces";
import "./RowActions.css";

/** The same hold as the 多图 cell and the sub pane (and the slack before a move cancels it). */
export const ROW_HOLD_MS = 550;
const HOLD_SLACK = 10;
/** A swipe past this opens (or, on an open row, closes) the buttons behind it. */
export const ROW_SWIPE_PX = 40;
const DIRECTION_PX = 8;
const BUTTON_PX = 72;
const UNDO_MS = 5000;

export interface RowAction {
  label: string;
  run: () => void;
  /** 删除 / 移出 / 放弃: the danger look; with `confirm`, the first tap only arms it (the button then reads `confirm`). */
  danger?: boolean;
  confirm?: string;
  /** Only in the long-press sheet, not behind the swipe. */
  menuOnly?: boolean;
  /** The swipe button's shorter label (the sheet shows `label`). */
  short?: string;
  disabled?: boolean;
  /** After it ran: a 5 s bar with 撤销 (an action that is undone in one step, like 移出自选). */
  undo?: { text: string; run: () => void };
}

// One row open at a time: opening another closes this one.
let closeOpen: (() => void) | null = null;

// The 撤销 bar outlives the row it came from (a removed 自选 row unmounts), so it lives here and <RowUndo> shows it.
type Undo = { text: string; run: () => void; key: number };
let undo: Undo | null = null;
let undoTimer = 0;
const undoListeners = new Set<() => void>();
const setUndo = (next: Undo | null) => {
  undo = next;
  window.clearTimeout(undoTimer);
  if (next) undoTimer = window.setTimeout(() => setUndo(null), UNDO_MS);
  undoListeners.forEach(listener => listener());
};

/** The 撤销 bar (once, in the phone layout's foot). */
export function RowUndo() {
  const current = useSyncExternalStore(listener => { undoListeners.add(listener); return () => { undoListeners.delete(listener); }; }, () => undo, () => undo);
  return <Presence>{current ? <div className="fdm-row-undo" role="status" data-agent-target="mobile-row-undo" key={current.key}>
    <span>{current.text}</span>
    <button type="button" onClick={() => { setUndo(null); current.run(); }}>撤销</button>
  </div> : null}</Presence>;
}

/**
 * 手机手势 (owner-chosen 2026-11-02): on the phone a list row hides its actions behind a left swipe (the first
 * ones not marked `menuOnly`) and shows all of them in a bottom sheet on a long press (or a right click / the
 * Android context menu). A tap on an open row closes it; a danger action with `confirm` needs a second tap; an
 * action with `undo` leaves a 撤销 bar. Each row is one `row-actions` disclosure (`id`): open = its buttons or its
 * sheet show; a command opens the sheet. Spread `row` on the row element and render `extra` inside it. Off the
 * phone layout it does nothing, so a component shared with the desktop keeps its desktop behaviour.
 */
export function useRowActions({ id, title, subtitle, actions }: { id: string; title: string; subtitle?: string; actions: RowAction[] }) {
  const mobile = useMobileLayout();
  const enabled = mobile && actions.length > 0;
  const [element, setElement] = useState<HTMLElement | null>(null);
  const [tray, setTray] = useState(false);
  const [swiped, setSwiped] = useState(false);
  const [sheet, setSheet] = useState(false);
  const [armed, setArmed] = useState<string | null>(null);
  const suppress = useRef(false);
  const sheetShown = usePresence(sheet);
  const swipeActions = actions.filter(action => !action.menuOnly).slice(0, 2);
  const width = swipeActions.length * BUTTON_PX;
  const close = useCallback(() => {
    setSwiped(false); setSheet(false); setArmed(null);
    if (closeOpen === closeRef.current) closeOpen = null;
  }, []);
  const closeRef = useRef(close);
  closeRef.current = close;
  const claim = () => { if (closeOpen && closeOpen !== closeRef.current) closeOpen(); closeOpen = closeRef.current; };
  const openSheet = useCallback(() => { claim(); setSwiped(false); setArmed(null); setSheet(true); }, []);
  useDisclosureSurface("row-actions", { id, available: enabled, state: swiped || sheet, apply: open => { if (open) openSheet(); else close(); } });
  useEffect(() => () => { if (closeOpen === closeRef.current) closeOpen = null; }, []);
  // The offset follows the finger without re-rendering (like 下拉刷新's --fdm-pull); the tray shows from the first sideways move.
  const geometry = useRef({ width, swiped, swipe: swipeActions.length > 0 });
  geometry.current = { width, swiped, swipe: swipeActions.length > 0 };
  useEffect(() => {
    if (!element || !enabled) return;
    let start: { x: number; y: number; pointer: number } | null = null;
    let sideways = false;
    let hold = 0;
    const place = (x: number) => element.style.setProperty("--row-x", String(Math.round(x)));
    // A released drag that did not open: the tray goes once the face has slid back (unless a new drag began).
    const retire = () => window.setTimeout(() => { if (element.dataset.dragging === undefined && !geometry.current.swiped) setTray(false); }, 300);
    const down = (event: PointerEvent) => {
      if (event.pointerType === "mouse" || !event.isPrimary) return;
      if (event.target instanceof Element && event.target.closest(".fdm-swipe-tray")) return;
      start = { x: event.clientX, y: event.clientY, pointer: event.pointerId };
      sideways = false;
      suppress.current = false;
      window.clearTimeout(hold);
      hold = window.setTimeout(() => { start = null; suppress.current = true; openSheet(); }, ROW_HOLD_MS);
    };
    const move = (event: PointerEvent) => {
      if (!start || event.pointerId !== start.pointer) return;
      const dx = event.clientX - start.x, dy = event.clientY - start.y;
      if (Math.hypot(dx, dy) > HOLD_SLACK) window.clearTimeout(hold);
      if (!sideways) {
        // Up or down first: the list scrolls (touch-action: pan-y), not ours.
        if (Math.abs(dy) > DIRECTION_PX && Math.abs(dy) >= Math.abs(dx)) { start = null; return; }
        if (Math.abs(dx) <= DIRECTION_PX || !geometry.current.swipe) return;
        sideways = true;
        suppress.current = true;
        claim();
        setTray(true);
        element.dataset.dragging = "";
      }
      const { width: full, swiped: open } = geometry.current;
      const x = (open ? -full : 0) + dx;
      // Past the buttons it moves at a third of the finger; it never opens rightwards.
      place(x < -full ? -full + (x + full) / 3 : Math.min(0, x));
    };
    const up = (event: PointerEvent) => {
      if (!start || event.pointerId !== start.pointer) return;
      window.clearTimeout(hold);
      const dx = event.clientX - start.x;
      start = null;
      if (!sideways) return;
      delete element.dataset.dragging;
      const open = geometry.current.swiped ? dx < ROW_SWIPE_PX : dx < -ROW_SWIPE_PX;
      place(open ? -geometry.current.width : 0);
      setSwiped(open);
      setArmed(null);
      if (!open) retire();
    };
    const cancel = () => {
      window.clearTimeout(hold);
      if (sideways) { delete element.dataset.dragging; place(geometry.current.swiped ? -geometry.current.width : 0); retire(); }
      start = null; sideways = false;
    };
    element.addEventListener("pointerdown", down);
    element.addEventListener("pointermove", move);
    element.addEventListener("pointerup", up);
    element.addEventListener("pointercancel", cancel);
    return () => {
      window.clearTimeout(hold);
      element.removeEventListener("pointerdown", down);
      element.removeEventListener("pointermove", move);
      element.removeEventListener("pointerup", up);
      element.removeEventListener("pointercancel", cancel);
    };
  }, [element, enabled, openSheet]);
  // Closed by a command, another row or a tap: the buttons slide back, then the tray goes.
  useEffect(() => {
    if (!element) return;
    if (swiped) { element.style.setProperty("--row-x", String(-width)); return; }
    element.style.setProperty("--row-x", "0");
    const timer = window.setTimeout(() => setTray(false), 300);
    return () => window.clearTimeout(timer);
  }, [element, swiped, width]);
  useEffect(() => { if (!enabled) { setSwiped(false); setSheet(false); setTray(false); } }, [enabled]);

  const pick = (action: RowAction) => {
    if (action.disabled) return;
    if (action.confirm && armed !== action.label) { setArmed(action.label); return; }
    close();
    action.run();
    if (action.undo) setUndo({ ...action.undo, key: Date.now() });
  };
  const button = (action: RowAction, place: "tray" | "sheet") => <button type="button" key={action.label} role={place === "sheet" ? "menuitem" : undefined}
    data-tone={action.danger ? "danger" : undefined} disabled={action.disabled} onClick={event => { event.stopPropagation(); pick(action); }}>
    {armed === action.label ? action.confirm : place === "tray" ? action.short ?? action.label : action.label}</button>;

  if (!enabled) return { row: {}, extra: null };
  const row = {
    ref: setElement,
    "data-swipe": "",
    "data-tray": tray || swiped ? "" : undefined,
    "data-row-actions": id,
    style: { "--row-x": swiped ? -width : 0 } as CSSProperties,
    onClickCapture: (event: ReactMouseEvent) => {
      // The click that ends a hold or a swipe, or a tap on an open row, only closes it (the tray's and the sheet's own
      // buttons excepted — the sheet is a portal, but React still passes its clicks through here).
      const target = event.target as Element;
      if (!element?.contains(target) || target.closest?.(".fdm-swipe-tray")) return;
      if (suppress.current || swiped) { event.preventDefault(); event.stopPropagation(); suppress.current = false; if (swiped) close(); }
    },
    onContextMenu: (event: ReactMouseEvent) => { event.preventDefault(); openSheet(); },
  };
  const extra = <>
    {tray || swiped ? <span className="fdm-swipe-tray" style={{ width }}>
      {swipeActions.map(action => button(action, "tray"))}</span> : null}
    {sheetShown.shown ? createPortal(<div className="fdm-row-sheet" data-closing={sheetShown.closing ? "" : undefined} onClick={event => { event.stopPropagation(); if (event.target === event.currentTarget) close(); }}>
      <section className="fdm-sheet" role="menu" aria-label={`${title} 的操作`} data-agent-target={`row-actions:${id}`}>
        <span className="fdm-sheet-grip" aria-hidden="true" />
        <header className="fdm-row-sheet-head"><strong>{title}</strong>{subtitle ? <small>{subtitle}</small> : null}</header>
        <div className="fdm-row-sheet-list">{actions.map(action => button(action, "sheet"))}</div>
        <button type="button" className="fdm-row-sheet-cancel" onClick={close}>取消</button>
      </section>
    </div>, document.body) : null}
  </>;
  return { row, extra };
}

/** A row element with `useRowActions` on it (`as` the list's own element); off the phone layout it is just that element. */
export function SwipeRow({ as: Tag = "div", id, title, subtitle, actions, children, style, ...props }: {
  as?: "div" | "li" | "article"; id: string; title: string; subtitle?: string; actions: RowAction[]; children: ReactNode;
} & Omit<HTMLAttributes<HTMLElement>, "title">) {
  const { row, extra } = useRowActions({ id, title, subtitle, actions });
  const merged = "style" in row ? { ...style, ...row.style } : style;
  return <Tag {...props} {...row} style={merged}>{children}{extra}</Tag>;
}

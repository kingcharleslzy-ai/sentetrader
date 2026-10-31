import { AccountAccessContext } from "../hooks/useAccountAccess";
import { useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Bot, ChevronDown } from "lucide-react";
import { useDisclosureSurface } from "../uiSurfaces";
import "./AiMenu.css";

/** 对象级 AI (2026-09-25): the object a question is about; rides in the turn's workspaceContext. */
export interface AiFocus {
  /** strategy_edit (2026-10-08): the 结果页 / phone input where the user says how to change the strategy. */
  kind: "strategy" | "strategy_instance" | "position" | "order" | "strategy_edit";
  id: string;
  label: string;
}

export interface AiMenuItem {
  /** What the menu shows. */
  label: string;
  /** The short question sent as the user's own turn. */
  prompt: string;
  /** A registry reason line when the item's skill is unavailable (null/undefined = usable). */
  blocked?: string | null;
  /** Runs instead of sending `prompt` (a direct action; `prompt` is then its tooltip). */
  action?: () => void;
}

export type AskAi = (text: string, focus: AiFocus) => void;

/** The disclosure id of an object's menu (`disclosure: {name: "ai-menu", id}`). */
export function aiMenuId(focus: Pick<AiFocus, "kind" | "id">): string {
  return `${focus.kind === "strategy_instance" ? "instance" : focus.kind}:${focus.id}`;
}

/**
 * "AI ▾" on an object (a strategy, a running instance, a position row, an order row): each item
 * sends one short question as the user's turn with `focus` attached, through the same send path
 * as the composer. Trade suggestions come back as cards; the menu itself never trades.
 */
export function AiMenu({ focus, items, onAsk, disabled, compact }: {
  focus: AiFocus; items: AiMenuItem[]; onAsk?: AskAi; disabled?: boolean; compact?: boolean;
}) {
  const access = useContext(AccountAccessContext);
  const [notice, setNotice] = useState("");
  const id = aiMenuId(focus);
  const [open, setOpen] = useDisclosureSurface("ai-menu", { id, available: Boolean(onAsk) });
  const root = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  // The list hangs from the trigger's right edge; a trigger near its panel's left edge (a running
  // card's AI button) would push it past the panel and get clipped, so it then opens rightwards.
  const [start, setStart] = useState(false);
  useLayoutEffect(() => {
    if (!open) { setStart(false); return; }
    const menu = list.current?.getBoundingClientRect();
    const panel = root.current?.closest("aside, [data-agent-target^='panel:']")?.getBoundingClientRect();
    if (menu && menu.left < (panel?.left ?? 0)) setStart(true);
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open, setOpen]);
  if (!onAsk) return null;
  return <div ref={root} className={`ai-menu${compact ? " compact" : ""}`} data-agent-target={`ai-menu:${id}`}
    onKeyDown={event => { if (event.key === "Escape" && open) { event.stopPropagation(); setOpen(false); } }}>
    <button type="button" className="ai-menu-trigger" aria-haspopup="menu" aria-expanded={open} disabled={disabled}
      aria-label={`AI：${focus.label}`} onClick={() => setOpen(value => !value)}><Bot size={14}/>AI<ChevronDown size={12}/></button>
    {open ? <div ref={list} className={`ai-menu-list${start ? " start" : ""}`} role="menu" aria-label={`问 AI：${focus.label}`}>
      {items.map(item => <button type="button" role="menuitem" key={item.label}
        title={item.blocked ?? (item.action ? item.prompt : `发送：${item.prompt}`)}
        onClick={() => { if (item.blocked) { if (access?.deny) access.deny(item.blocked); else setNotice(item.blocked); return; } setOpen(false); if (item.action) item.action(); else onAsk(item.prompt, focus); }}>{item.label}</button>)}
      {items.some(item => item.blocked) ? <small>{items.find(item => item.blocked)?.blocked}</small> : null}
    </div> : null}
    {notice && <small role="status">{notice}</small>}
  </div>;
}

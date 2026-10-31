import { useEffect, useRef, useState, type ReactNode } from "react";
import { MoreHorizontal, Trash2 } from "lucide-react";
import { useDisclosureSurface } from "../uiSurfaces";

export function StrategyItemActions({id, name, disabled, onDelete, children, hideActions}: {id: string; name: string; disabled: boolean; onDelete: () => Promise<void>; children: ReactNode; hideActions?:boolean}) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = () => {setOpen(false); setConfirming(false);};
  useEffect(() => { setOpen(false); setConfirming(false); }, [id, hideActions]);
  useDisclosureSurface("strategy-actions", { id, available: !hideActions, state: open, apply: value => {
    if (value && disabled) throw new Error("策略正在处理，请稍后再试");
    setOpen(value); setConfirming(false);
  } });
  useDisclosureSurface("strategy-delete-confirm", { id, available: !hideActions, state: open && confirming, apply: value => {
    if (value && disabled) throw new Error("策略正在处理，请稍后再试");
    setOpen(value); setConfirming(value);
  } });
  useEffect(() => {
    if (!open) return;
    root.current?.querySelector<HTMLButtonElement>('[role="menu"] button')?.focus();
    const outside = (event: PointerEvent) => {if (!root.current?.contains(event.target as Node)) close();};
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  return <div ref={root} className={`strategy-library-row${hideActions ? " without-actions" : ""}`} onContextMenu={e => {e.preventDefault(); if (!disabled && !hideActions) {setOpen(true); setConfirming(false);}}} onKeyDown={e => {if (e.key === "Escape" && open) {e.preventDefault(); e.stopPropagation(); close(); trigger.current?.focus();}}}>
    {children}{!hideActions&&<button ref={trigger} type="button" className="strategy-more" aria-label={`${name} 的更多操作`} aria-haspopup="menu" aria-expanded={open} disabled={disabled} onClick={() => {setOpen(v => !v); setConfirming(false);}}><MoreHorizontal size={16}/></button>}
    {open && <div className="strategy-item-menu" role="menu" aria-label={`${name} 的操作`}>
      {confirming ? <><p>删除“{name}”？</p><div><button type="button" disabled={disabled} onClick={close}>取消</button><button type="button" className="app-button danger strategy-delete" disabled={disabled} onClick={() => {close(); void onDelete();}}>确认删除</button></div></> : <button type="button" role="menuitem" disabled={disabled} onClick={() => setConfirming(true)}><Trash2 size={14}/>删除策略</button>}
    </div>}
  </div>;
}

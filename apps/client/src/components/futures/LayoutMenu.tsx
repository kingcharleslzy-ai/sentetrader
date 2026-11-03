import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { useDisclosureSurface } from "../../uiSurfaces";
import { LAYOUTS_MAX, type WorkspaceLayoutChange, type WorkspaceLayoutsHandle } from "./workspaceLayouts";
import { Presence } from "../motion";

/**
 * 自由布局 (2026-11-02): the top bar's 布局 dropdown (`disclosure: workspace-layouts`) — switch between 盘前 / 盘中 /
 * 复盘 and saved layouts, save the screen as a new one, rename or delete. Each action is the `workspaceLayout` command.
 */
export function LayoutMenu({ handle, onError }: { handle: WorkspaceLayoutsHandle; onError: (message: string) => void }) {
  const [open, setOpen] = useDisclosureSurface("workspace-layouts");
  const [renaming, setRenaming] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const box = useRef<HTMLSpanElement>(null);
  const { layouts } = handle;
  const current = layouts.items.find(item => item.id === layouts.current);
  useEffect(() => {
    if (!open) { setRenaming(null); return; }
    const outside = (event: PointerEvent) => { if (!box.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [open, setOpen]);
  const run = (change: WorkspaceLayoutChange) => {
    try { handle.apply(change); return true; } catch (error) { onError(error instanceof Error ? error.message : "布局暂时不能切换"); return false; }
  };
  return (
    <span className="fdw-layout-menu" ref={box}>
      <button type="button" className="fdw-nav-view" aria-haspopup="true" aria-expanded={open} aria-label={`布局：${current?.name ?? ""}`}
        data-agent-target="workspace-layouts" onClick={() => setOpen(!open)}>
        <span>{current?.name ?? "布局"}</span><ChevronDown />
      </button>
      <Presence>{open ? <div className="fdw-layout-list" data-agent-target="workspace-layouts-menu">
        {layouts.items.map(item => <div key={item.id}>
          {renaming === item.id
            ? <form className="fdw-layout-form" onSubmit={event => { event.preventDefault(); if (run({ rename: { id: item.id, name: draft } })) setRenaming(null); }}>
                <input aria-label={`“${item.name}”的新名字`} value={draft} maxLength={12} autoFocus onChange={event => setDraft(event.target.value)} />
                <button type="submit" className="primary-button">改名</button>
              </form>
            : <>
                <button type="button" aria-current={item.id === layouts.current || undefined} onClick={() => { if (run({ select: item.id })) setOpen(false); }}>{item.name}</button>
                <button type="button" className="fdw-layout-action" aria-label={`改名：${item.name}`} onClick={() => { setDraft(item.name); setRenaming(item.id); }}>改名</button>
                <button type="button" className="fdw-layout-action danger" aria-label={`删除布局：${item.name}`} disabled={layouts.items.length === 1}
                  onClick={() => run({ remove: item.id })}>删除</button>
              </>}
        </div>)}
        <form className="fdw-layout-form fdw-layout-new" onSubmit={event => { event.preventDefault(); if (run({ save: draft })) { setDraft(""); setOpen(false); } }}>
          <input aria-label="新布局的名字" placeholder="新布局名" value={renaming ? "" : draft} maxLength={12} disabled={Boolean(renaming)}
            onChange={event => setDraft(event.target.value)} />
          <button type="submit" className="primary-button" disabled={Boolean(renaming) || layouts.items.length >= LAYOUTS_MAX}>另存布局</button>
        </form>
      </div> : null}</Presence>
    </span>
  );
}

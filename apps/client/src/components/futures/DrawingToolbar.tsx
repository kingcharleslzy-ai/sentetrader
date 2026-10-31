import { useEffect, useRef } from "react";
import { ChevronDown, Eye, EyeOff, Lock, LockOpen, Magnet, Trash2, X } from "lucide-react";
import { DRAWING_GROUPS, DRAWING_TOOLS, toolSpec, type DrawingGroup } from "../../chartDrawingModel";
import { DrawingToolIcon } from "./DrawingToolIcon";
import type { DrawingDesk } from "./useDrawingDesk";

const LONG_PRESS_MS = 380;

/**
 * 画线工具栏 (画线第二版 2026-10-09, 样稿 ①; 按需浮现 owner 2026-10-14): a vertical toolbar floating over the
 * chart's left edge, shown only while drawing — a tool armed from the right-click 画线 ▸ submenu, a shortcut
 * or a UI command — and gone again at the crosshair (Esc, a finished line, ✕ or a click elsewhere with no tool
 * armed). ✕, six group buttons (each shows the group's last-used tool; a press arms it, a long press or ▾ lists
 * the group), then 磁吸, 全部锁定, 全部显示/隐藏 and 全部删除 (asks once more, danger). Every button is a UI
 * command too (chart-drawing-toolbar / -group / -tool / -magnet / -lock-all / chart-drawings-hidden /
 * chart-drawing-clear); the switches are account preferences. The lines themselves show either way.
 */
export function DrawingToolbar({ desk, onSwitchError }: { desk: DrawingDesk; onSwitchError: (message: string) => void }) {
  const { tool, setTool, lastUsed, openGroup, setOpenGroup, setToolbarOpen, magnet, locked, hidden, confirmClear, askClear, closeConfirm, items } = desk;
  const rootRef = useRef<HTMLElement>(null);
  const pressTimer = useRef<number | undefined>(undefined);
  const longPressed = useRef(false);
  // A click outside closes the group list and the delete confirmation; with no tool armed the toolbar is idle
  // and goes too.
  useEffect(() => {
    if (tool && !openGroup && !confirmClear) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && rootRef.current?.contains(event.target)) return;
      if (!tool) { setToolbarOpen(false); return; }
      setOpenGroup(null);
      if (confirmClear) closeConfirm();
    };
    document.addEventListener("pointerdown", outside, true);
    return () => document.removeEventListener("pointerdown", outside, true);
  }, [tool, openGroup, confirmClear, setOpenGroup, setToolbarOpen, closeConfirm]);
  useEffect(() => () => window.clearTimeout(pressTimer.current), []);
  const switchPreference = (change: () => void) => { try { change(); } catch (error) { onSwitchError(error instanceof Error ? error.message : "连接后可设置"); } };

  const groupOf = tool ? toolSpec(tool).group : null;
  return (
    <nav className="fdw-draw-toolbar" aria-label="画线工具栏" data-agent-target="chart:drawing-toolbar" ref={rootRef}>
      <button type="button" className="fdw-draw-btn" aria-label="收起画线工具栏" title="收起，回到十字光标（Esc）" onClick={() => setToolbarOpen(false)}>
        <X aria-hidden="true" />
      </button>
      <hr />
      {DRAWING_GROUPS.map(group => {
        const tools = DRAWING_TOOLS.filter(spec => spec.group === group.id);
        const shown = groupOf === group.id && tool ? tool : lastUsed[group.id];
        const spec = toolSpec(shown);
        const many = tools.length > 1;
        const toggleList = (id: DrawingGroup) => setOpenGroup(openGroup === id ? null : id);
        return (
          <div className="fdw-draw-group" key={group.id}>
            <button type="button" className="fdw-draw-btn" aria-pressed={groupOf === group.id}
              aria-label={`${spec.label}（${group.label}）`} title={`${spec.label}${spec.shortcut ? `（${spec.shortcut}）` : ""}${many ? " · 按住或点 ▾ 换同组工具" : ""}`}
              onPointerDown={() => {
                if (!many) return;
                longPressed.current = false;
                window.clearTimeout(pressTimer.current);
                pressTimer.current = window.setTimeout(() => { longPressed.current = true; setOpenGroup(group.id); }, LONG_PRESS_MS);
              }}
              onPointerUp={() => window.clearTimeout(pressTimer.current)}
              onPointerLeave={() => window.clearTimeout(pressTimer.current)}
              onClick={() => {
                if (longPressed.current) { longPressed.current = false; return; }
                setTool(tool === shown ? null : shown);
              }}>
              <DrawingToolIcon kind={shown} />
            </button>
            {many ? <button type="button" className="fdw-draw-more" aria-label={`${group.label}：更多工具`} aria-haspopup="menu" aria-expanded={openGroup === group.id}
              onClick={() => toggleList(group.id)}><ChevronDown aria-hidden="true" /></button> : null}
            {openGroup === group.id ? (
              <div className="fdw-draw-flyout" role="menu" aria-label={`${group.label}工具`}>
                <strong>{group.label}</strong>
                {tools.map(item => (
                  <button type="button" role="menuitemradio" key={item.kind} aria-checked={tool === item.kind}
                    onClick={() => setTool(item.kind)}>
                    <DrawingToolIcon kind={item.kind} /><span>{item.label}</span>{item.shortcut ? <kbd>{item.shortcut}</kbd> : null}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        );
      })}
      <hr />
      <button type="button" className="fdw-draw-btn" aria-pressed={magnet} aria-label="磁吸（贴到开高低收）" title="磁吸：点击贴到这根 K 线的开高低收"
        onClick={() => switchPreference(() => desk.savePreference({ drawingMagnet: !magnet }))}><Magnet aria-hidden="true" /></button>
      <button type="button" className="fdw-draw-btn" aria-pressed={locked} aria-label="全部锁定" title={locked ? "解除全部锁定" : "全部锁定：画好的线都不能拖动"}
        onClick={() => switchPreference(() => desk.savePreference({ drawingsLocked: !locked }))}>{locked ? <Lock aria-hidden="true" /> : <LockOpen aria-hidden="true" />}</button>
      <button type="button" className="fdw-draw-btn" aria-pressed={hidden} aria-label="全部隐藏" title={hidden ? "显示全部画线" : "隐藏全部画线"}
        onClick={() => switchPreference(() => desk.savePreference({ drawingsHidden: !hidden }))}>{hidden ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}</button>
      <div className="fdw-draw-group">
        <button type="button" className="fdw-draw-btn danger" aria-label={`全部删除（本合约 ${items.length} 条）`} title="删除本合约全部画线"
          aria-expanded={confirmClear} disabled={!items.length} onClick={() => { if (confirmClear) closeConfirm(); else askClear(); }}><Trash2 aria-hidden="true" /></button>
        {confirmClear ? (
          <div className="fdw-draw-flyout fdw-draw-confirm" role="dialog" aria-label="确认删除全部画线">
            <p>删除本合约全部 {items.length} 条画线？删除后不能恢复。</p>
            <div>
              <button type="button" className="app-button" onClick={closeConfirm}>取消</button>
              <button type="button" className="app-button danger" onClick={desk.clearAll}>删除 {items.length} 条</button>
            </div>
          </div>
        ) : null}
      </div>
    </nav>
  );
}

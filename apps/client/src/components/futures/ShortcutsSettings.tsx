import { useEffect, useState } from "react";
import { SettingRow, SettingsSection } from "../SettingsPanel";
import { Button } from "../settings/controls";
import { DRAWING_TOOLS } from "../../chartDrawingModel";
import { DEFAULT_SHORTCUTS, SHORTCUT_ACTIONS, SHORTCUT_LABELS, comboOf, shortcutLabel, holdKeyLabel, isHoldAction, resetShortcuts, setShortcut, useShortcuts, type ShortcutAction } from "../../shortcuts";

const keys = (combo: string) => shortcutLabel(combo).split("+").map((part, index) => <kbd key={index}>{part === "Meta" ? "Cmd" : part}</kbd>);

/**
 * 设置 → 快捷键: the workspace shortcuts are recorded per machine (localStorage); Esc and the
 * font-size keys stay fixed. 录制 arms the row, the next keydown becomes the binding, a conflict is
 * refused. 按住说话 records one physical key (a lone modifier such as right Alt, or an ordinary key).
 */
export function ShortcutsSettings() {
  const shortcuts = useShortcuts();
  const [recording, setRecording] = useState<ShortcutAction>();
  const [message, setMessage] = useState<{ action: ShortcutAction; text: string }>();
  useEffect(() => {
    if (!recording) return;
    const onKeyDown = (event: KeyboardEvent) => {
      event.preventDefault(); event.stopPropagation();
      if (event.key === "Escape") { setRecording(undefined); return; }
      const hold = isHoldAction(recording);
      const binding = hold ? event.code : comboOf(event);
      if (!binding) return;
      const error = setShortcut(recording, binding);
      setMessage({ action: recording, text: error ?? `已改为 ${hold ? holdKeyLabel(binding) : binding}` });
      setRecording(undefined);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [recording]);
  const customized = SHORTCUT_ACTIONS.some(action => shortcuts[action] !== DEFAULT_SHORTCUTS[action]);
  const hint = (action: ShortcutAction) => message?.action === action ? <span role="status">{message.text}</span>
    : recording === action ? (isHoldAction(action) ? "按下要按住的那个键（可以是单独的 Alt / Option），Esc 取消" : "按下新的组合键，Esc 取消")
    : isHoldAction(action) ? "窗口在前台时按住说话、松开结束（对话面板会自动打开）；不足 0.3 秒不算" : undefined;
  return <>
    <SettingsSection title="工作区">
      {SHORTCUT_ACTIONS.map(action => (
        <SettingRow key={action} label={SHORTCUT_LABELS[action]} hint={hint(action)}>
          {isHoldAction(action) ? <kbd>{holdKeyLabel(shortcuts[action])}</kbd> : keys(shortcuts[action])}
          <Button onClick={() => { setMessage(undefined); setRecording(current => current === action ? undefined : action); }}>{recording === action ? "按下按键…" : `录制${SHORTCUT_LABELS[action]}`}</Button>
        </SettingRow>
      ))}
      <SettingRow label="关闭菜单 / 抽屉"><kbd>Esc</kbd></SettingRow>
      {/* The chart's fixed drawing keys (#759): listed from the tool table itself, not recorded here. */}
      {DRAWING_TOOLS.filter(tool => tool.shortcut).map(tool => <SettingRow key={tool.kind} label={`画${tool.label}`}>{keys(tool.shortcut!)}</SettingRow>)}
      <SettingRow label="恢复默认" hint={customized ? undefined : "当前均为默认"}><Button disabled={!customized} onClick={() => { resetShortcuts(); setMessage(undefined); }}>恢复默认快捷键</Button></SettingRow>
    </SettingsSection>
  </>;
}

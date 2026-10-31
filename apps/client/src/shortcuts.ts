import { useSyncExternalStore } from "react";

/**
 * Workspace shortcuts (设置 → 快捷键). The bindings live on this machine only (localStorage): a
 * keyboard layout is a device matter, not an account preference. Esc and the Ctrl/Cmd font-size
 * keys stay fixed. The settings page records a new combo, refuses one already taken, and resets.
 *
 * Two kinds: a press action fires on a combo with a modifier ("Alt+B"); a hold action (按住说话)
 * is one physical key held down and released, named by `KeyboardEvent.code` — a lone modifier
 * such as "AltRight" (right Alt / right Option) or an ordinary key such as "F2".
 */
export const SHORTCUT_ACTIONS = ["ai", "buy", "sell", "positions", "ptt", "quickBuy", "quickSell", "quickClose", "quickToday", "quickHalf", "quickCancel", "quickReverse"] as const;
export type ShortcutAction = (typeof SHORTCUT_ACTIONS)[number];
export const HOLD_ACTIONS: readonly ShortcutAction[] = ["ptt"];
export const SHORTCUT_LABELS: Record<ShortcutAction, string> = { ai: "打开 / 收起 对话", buy: "买多草稿", sell: "卖空草稿", positions: "打开 / 收起 持仓面板", ptt: "按住说话", quickBuy: "一键买多", quickSell: "一键卖空", quickClose: "一键平仓", quickToday: "一键平今", quickHalf: "一键平一半", quickCancel: "撤本合约挂单", quickReverse: "一键反手" };
export const DEFAULT_SHORTCUTS: Record<ShortcutAction, string> = { ai: "Alt+A", buy: "Alt+B", sell: "Alt+S", positions: "Alt+P", ptt: "AltRight", quickBuy: "Alt+1", quickSell: "Alt+2", quickClose: "Alt+3", quickToday: "Alt+4", quickHalf: "Alt+5", quickCancel: "Alt+6", quickReverse: "Alt+7" };
const STORAGE_KEY = "ai-trader:shortcuts";
/** Combos the app owns elsewhere; a recording that lands on one is refused. */
const RESERVED = ["Escape", "Ctrl+=", "Ctrl++", "Ctrl+-", "Ctrl+0", "Meta+=", "Meta++", "Meta+-", "Meta+0"];
/** Keys a hold binding may not take: Esc cancels the hold; Enter, Tab, Backspace and Space are typing. */
const RESERVED_HOLD = ["Escape", "Enter", "NumpadEnter", "Tab", "Backspace", "Space"];
const MODIFIER_CODES = ["AltLeft", "AltRight", "ControlLeft", "ControlRight", "ShiftLeft", "ShiftRight", "MetaLeft", "MetaRight"];

export const isHoldAction = (action: ShortcutAction) => HOLD_ACTIONS.includes(action);

function read(): Record<ShortcutAction, string> {
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}") as Partial<Record<ShortcutAction, string>>;
    return { ...DEFAULT_SHORTCUTS, ...Object.fromEntries(SHORTCUT_ACTIONS.filter(action => typeof stored[action] === "string").map(action => [action, stored[action]])) };
  } catch { return { ...DEFAULT_SHORTCUTS }; }
}
let current = read();
const listeners = new Set<() => void>();
const notify = () => listeners.forEach(listener => listener());
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
const persist = () => { try { localStorage.setItem(STORAGE_KEY, JSON.stringify(current)); } catch { /* Local preference only. */ } notify(); };

export function currentShortcuts(): Record<ShortcutAction, string> { return current; }
export function useShortcuts(): Record<ShortcutAction, string> { return useSyncExternalStore(subscribe, currentShortcuts, currentShortcuts); }

/** "Alt+B" for a keydown, or null when only modifiers are down. */
export function comboOf(event: KeyboardEvent): string | null {
  const key = event.key;
  if (["Alt", "Control", "Meta", "Shift"].includes(key)) return null;
  const parts = [event.ctrlKey && "Ctrl", event.metaKey && "Meta", event.altKey && "Alt", event.shiftKey && "Shift"].filter(Boolean) as string[];
  const physicalDigit = event.altKey && /^Digit[0-9]$/.test(event.code) ? event.code.slice(5) : key;
  const name = physicalDigit.length === 1 ? physicalDigit.toUpperCase() : physicalDigit;
  return [...parts, name].join("+");
}

/** Assigns a binding; returns the conflict message instead when another action or the app already uses it. */
export function setShortcut(action: ShortcutAction, combo: string): string | null {
  const hold = isHoldAction(action);
  if (hold) {
    if (!combo || RESERVED_HOLD.includes(combo)) return `${holdKeyLabel(combo)} 不能用作按住键`;
  } else {
    if (RESERVED.includes(combo)) return `${combo} 由软件保留`;
    if (!combo.includes("+")) return "请带上 Alt、Ctrl 或 Cmd 等修饰键";
  }
  const taken = SHORTCUT_ACTIONS.find(other => other !== action && isHoldAction(other) === hold && current[other] === combo);
  if (taken) return `${hold ? holdKeyLabel(combo) : combo} 已用于「${SHORTCUT_LABELS[taken]}」`;
  current = { ...current, [action]: combo };
  persist();
  return null;
}

export function resetShortcuts() { current = { ...DEFAULT_SHORTCUTS }; persist(); }

/** The press action a keydown triggers, if any. */
export function shortcutAction(event: KeyboardEvent): ShortcutAction | undefined {
  const combo = comboOf(event);
  return combo ? SHORTCUT_ACTIONS.find(action => !isHoldAction(action) && current[action] === combo) : undefined;
}

/** The hold action a key (down or up) belongs to, matched on the physical key. */
export function holdAction(event: KeyboardEvent): ShortcutAction | undefined {
  return event.code ? HOLD_ACTIONS.find(action => current[action] === event.code) : undefined;
}

/** A lone modifier works while typing in the composer; an ordinary key only outside text fields. */
export function isModifierCode(code: string): boolean { return MODIFIER_CODES.includes(code); }

const isMac = () => typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

/** "右 Alt" / "右 Option" / "F2" / "V" for a hold binding's `KeyboardEvent.code`. */
export function holdKeyLabel(code: string): string {
  if (isModifierCode(code)) {
    const side = code.endsWith("Right") ? "右 " : "左 ";
    const base = code.replace(/(Left|Right)$/, "");
    const names: Record<string, string> = { Alt: isMac() ? "Option" : "Alt", Meta: isMac() ? "Cmd" : "Win", Control: "Ctrl", Shift: "Shift" };
    return side + names[base];
  }
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit\d$/.test(code)) return code.slice(5);
  return code;
}

/** Test helper. */
export function resetShortcutStore() { current = read(); notify(); }

/** Device-native labels for buttons and the settings recorder. */
export const shortcutLabel = (combo: string) => isMac() ? combo.replace("Alt+", "⌥").replace("Meta+", "⌘").replace("Ctrl+", "⌃").replace("Shift+", "⇧") : combo;

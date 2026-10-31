import { DEFAULT_SHORTCUTS, comboOf, currentShortcuts, holdAction, holdKeyLabel, isModifierCode, resetShortcutStore, resetShortcuts, setShortcut, shortcutAction } from "./shortcuts";

beforeEach(() => { localStorage.clear(); resetShortcutStore(); });

test("defaults, a recorded combo, conflicts and reset", () => {
  expect(currentShortcuts()).toEqual(DEFAULT_SHORTCUTS);
  expect(setShortcut("buy", "Alt+A")).toBe("Alt+A 已用于「打开 / 收起 对话」");
  expect(setShortcut("buy", "Escape")).toBe("Escape 由软件保留");
  expect(setShortcut("buy", "X")).toBe("请带上 Alt、Ctrl 或 Cmd 等修饰键");
  expect(setShortcut("buy", "Ctrl+Shift+B")).toBeNull();
  expect(currentShortcuts().buy).toBe("Ctrl+Shift+B");
  expect(JSON.parse(localStorage.getItem("ai-trader:shortcuts") ?? "{}").buy).toBe("Ctrl+Shift+B");
  resetShortcutStore();  // a fresh window reads the stored map
  expect(currentShortcuts().buy).toBe("Ctrl+Shift+B");
  resetShortcuts();
  expect(currentShortcuts()).toEqual(DEFAULT_SHORTCUTS);
});

test("keydown → combo → action", () => {
  expect(comboOf(new KeyboardEvent("keydown", { key: "Alt" }))).toBeNull();
  expect(comboOf(new KeyboardEvent("keydown", { key: "b", altKey: true }))).toBe("Alt+B");
  expect(shortcutAction(new KeyboardEvent("keydown", { key: "p", altKey: true }))).toBe("positions");
  expect(shortcutAction(new KeyboardEvent("keydown", { key: "p", altKey: true, ctrlKey: true }))).toBeUndefined();
  setShortcut("positions", "Ctrl+Alt+P");
  expect(shortcutAction(new KeyboardEvent("keydown", { key: "p", altKey: true, ctrlKey: true }))).toBe("positions");
});

test("按住说话是“按住”类型：默认右 Alt，按物理键匹配，可录单独修饰键或普通键，不与组合键混用", () => {
  expect(currentShortcuts().ptt).toBe("AltRight");
  const rightAlt = new KeyboardEvent("keydown", { key: "Alt", code: "AltRight", altKey: true });
  expect(holdAction(rightAlt)).toBe("ptt");
  expect(shortcutAction(rightAlt)).toBeUndefined();
  expect(holdAction(new KeyboardEvent("keydown", { key: "Alt", code: "AltLeft", altKey: true }))).toBeUndefined();
  expect(isModifierCode("AltRight")).toBe(true);
  expect(isModifierCode("F2")).toBe(false);
  expect(setShortcut("ptt", "Escape")).toBe("Escape 不能用作按住键");
  expect(setShortcut("ptt", "Space")).toBe("Space 不能用作按住键");
  expect(setShortcut("ptt", "F2")).toBeNull();
  expect(holdAction(new KeyboardEvent("keydown", { key: "F2", code: "F2" }))).toBe("ptt");
  expect(setShortcut("ptt", "ControlRight")).toBeNull();
  expect(JSON.parse(localStorage.getItem("ai-trader:shortcuts") ?? "{}").ptt).toBe("ControlRight");
  expect(holdKeyLabel("ControlRight")).toBe("右 Ctrl");
  expect(holdKeyLabel("KeyV")).toBe("V");
  resetShortcuts();
  expect(currentShortcuts().ptt).toBe("AltRight");
});

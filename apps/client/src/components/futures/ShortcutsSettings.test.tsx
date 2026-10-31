import { fireEvent, render, screen } from "@testing-library/react";
import { ShortcutsSettings } from "./ShortcutsSettings";
import { currentShortcuts, resetShortcutStore } from "../../shortcuts";

beforeEach(() => { localStorage.clear(); resetShortcutStore(); });

test("设置 → 快捷键：按住说话录一个单独的修饰键，显示为“右 Ctrl”，可恢复默认", () => {
  render(<ShortcutsSettings />);
  fireEvent.click(screen.getByRole("button", { name: "录制按住说话" }));
  expect(screen.getByText(/按下要按住的那个键/)).toBeInTheDocument();
  // A lone modifier is a valid hold key (a press action would ignore it and wait for a combo).
  fireEvent.keyDown(window, { key: "Control", code: "ControlRight", ctrlKey: true });
  expect(currentShortcuts().ptt).toBe("ControlRight");
  expect(screen.getByRole("status")).toHaveTextContent("已改为 右 Ctrl");
  // The press actions still record combos.
  fireEvent.click(screen.getByRole("button", { name: "录制买多草稿" }));
  fireEvent.keyDown(window, { key: "Control", code: "ControlLeft", ctrlKey: true });
  expect(currentShortcuts().buy).toBe("Alt+B");
  fireEvent.keyDown(window, { key: "b", code: "KeyB", ctrlKey: true, shiftKey: true });
  expect(currentShortcuts().buy).toBe("Ctrl+Shift+B");
  fireEvent.click(screen.getByRole("button", { name: "恢复默认快捷键" }));
  expect(currentShortcuts().ptt).toBe("AltRight");
});

test("快捷键列表列出图表画线的固定键 Alt+T / Alt+H / Alt+V（#759）", () => {
  render(<ShortcutsSettings />);
  for (const [label, key] of [["画趋势线", "T"], ["画水平线", "H"], ["画垂直线", "V"]]) {
    const row = screen.getByText(label).closest(".app-setting-row") as HTMLElement;
    expect(row).toHaveTextContent(`Alt${key}`);
  }
});

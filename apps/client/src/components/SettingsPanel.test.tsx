import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { SettingRow, SettingsPanel } from "./SettingsPanel";

function Workspace({ entry = true }: { entry?: boolean }) {
  const [open, setOpen] = useState(false);
  return <>
    {entry ? <button type="button" aria-label="设置" onClick={() => setOpen(true)}>设置</button> : null}
    {open ? <SettingsPanel onClose={() => setOpen(false)} pages={[{ id: "ui", label: "界面", content: <button type="button">主题</button> }]} /> : null}
  </>;
}

test("closing settings returns keyboard focus to the button that opened it (#484)", () => {
  render(<Workspace />);
  const entry = screen.getByRole("button", { name: "设置" });
  entry.focus();
  fireEvent.click(entry);
  screen.getByRole("button", { name: "主题" }).focus();
  // Escape on a modal dialog arrives as its cancel event.
  fireEvent(screen.getByRole("dialog"), new Event("cancel"));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(entry);

  fireEvent.click(entry);
  fireEvent.click(screen.getByRole("button", { name: "关闭系统设置" }));
  expect(document.activeElement).toBe(entry);
});

test("closing settings forces no focus when its entry is gone", () => {
  const { rerender } = render(<Workspace />);
  const entry = screen.getByRole("button", { name: "设置" });
  entry.focus();
  fireEvent.click(entry);
  rerender(<Workspace entry={false} />);
  fireEvent(screen.getByRole("dialog"), new Event("cancel"));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(document.body);
});

test("a setting's explanation sits behind its \"?\" and opens inside the modal settings dialog; its status line stays visible", () => {
  render(<SettingsPanel onClose={() => {}} pages={[{ id: "trade", label: "交易", content: <>
    <SettingRow label="本金" helpKey="initial-balance" help="初始资金 + 后续入金，盈利不计"><input aria-label="本金" /></SettingRow>
    <SettingRow label="语音下单免口头确认" hint="仅模拟盘：复述后不等“确认”直接提交；实盘永不适用"><input aria-label="开关" /></SettingRow>
  </> }]} />);
  expect(screen.queryByText("初始资金 + 后续入金，盈利不计")).toBeNull();
  expect(screen.getByText("仅模拟盘：复述后不等“确认”直接提交；实盘永不适用")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "本金的说明" }));
  const tip = screen.getByRole("tooltip");
  expect(tip).toHaveTextContent("初始资金 + 后续入金，盈利不计");
  // A popover in document.body would sit under the modal dialog's top layer, out of sight.
  expect(screen.getByRole("dialog").contains(tip)).toBe(true);
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("tooltip")).toBeNull();
});

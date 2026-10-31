import { fireEvent, render, screen } from "@testing-library/react";
import { Segmented, Stepper, Switch } from "./controls";
import { normalizeSettingsPage } from "../futures/WorkspaceSettings";
import { matches } from "../../hooks/useUiControl";

test("the five settings controls expose their state through roles the snapshot records", () => {
  const onSwitch = vi.fn(); const onSegment = vi.fn(); const onStep = vi.fn();
  render(<>
    <Switch label="平台上限" checked={false} on="已启用" off="未启用" onChange={onSwitch} />
    <Segmented label="界面主题" value="dark" options={[{ value: "dark", label: "深色" }, { value: "light", label: "浅色" }]} onChange={onSegment} />
    <Stepper label="单笔最多手数" value={1} min={1} max={3} unit="手" onChange={onStep} />
  </>);
  const toggle = screen.getByRole("switch", { name: "平台上限" });
  expect(toggle).toHaveAttribute("aria-checked", "false");
  expect(screen.getByText("未启用")).toBeInTheDocument();
  fireEvent.click(toggle);
  expect(onSwitch).toHaveBeenCalledWith(true);
  expect(screen.getByRole("radiogroup", { name: "界面主题" })).toBeInTheDocument();
  expect(screen.getByRole("radio", { name: "深色" })).toHaveAttribute("aria-checked", "true");
  fireEvent.click(screen.getByRole("radio", { name: "浅色" }));
  expect(onSegment).toHaveBeenCalledWith("light");
  // The stepper clamps to its range and disables the button at the edge.
  expect(screen.getByRole("button", { name: "单笔最多手数减少" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "单笔最多手数增加" }));
  expect(onStep).toHaveBeenCalledWith(2);
  fireEvent.change(screen.getByRole("spinbutton", { name: "单笔最多手数" }), { target: { value: "9" } });
  expect(onStep).toHaveBeenLastCalledWith(3);
  fireEvent.change(screen.getByRole("spinbutton", { name: "单笔最多手数" }), { target: { value: "" } });
  expect(onStep).toHaveBeenLastCalledWith(null);
  expect(screen.getByText("手")).toBeInTheDocument();
});

test("1.6.5 settings page names map onto the split pages, in commands and receipts alike", () => {
  expect(normalizeSettingsPage("connection")).toBe("account");
  expect(normalizeSettingsPage("appearance")).toBe("ui");
  expect(normalizeSettingsPage("simulation")).toBe("trade");
  expect(normalizeSettingsPage("fees")).toBe("trade-advanced");
  expect(normalizeSettingsPage("voice")).toBe("voice");
  expect(normalizeSettingsPage(undefined)).toBe("account");
  expect(matches({ settingsPage: "trade-advanced" }, { settingsPage: "fees" })).toBe(true);
  expect(matches({ settingsPage: "trade" }, { settingsPage: "trade" })).toBe(true);
  expect(matches({ settingsPage: "trade" }, { settingsPage: "fees" })).toBe(false);
  expect(matches({ settingsPage: "ui" }, { settingsPage: "chart" })).toBe(false);
});

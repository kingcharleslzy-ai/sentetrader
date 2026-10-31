import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { ToolsRail } from "./ToolsRail";
import { SettingsPanel } from "../SettingsPanel";
import type { RightDock } from "./support";

test("restricted rail entries stay clickable and authorize before private navigation", () => {
  const access = { reason: () => "登录后可使用此功能", require: vi.fn((_key: string) => false) }, onTrade = vi.fn(), setRightDock = vi.fn();
  render(<ToolsRail rightDock={null} setRightDock={setRightDock} onTrade={onTrade} onSettings={vi.fn()} access={access} />);
  for (const label of ["打开对话", "打开策略面板", "打开模拟下单", "打开持仓面板"]) {
    expect(screen.getByRole("button", { name: label })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: label }));
  }
  expect(access.require.mock.calls.map(call => call[0])).toEqual(["ai-chat", "strategy-management", "order-proposal", "account"]);
  expect(onTrade).not.toHaveBeenCalled();
  expect(setRightDock).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "打开调研面板" }));
  expect(setRightDock).toHaveBeenCalledOnce();
});

test("allowed account opens order panel after authorization", () => {
  const access = { reason: () => undefined, require: vi.fn(() => true) }, onTrade = vi.fn(), setRightDock = vi.fn();
  render(<ToolsRail rightDock={null} setRightDock={setRightDock} onTrade={onTrade} onSettings={vi.fn()} access={access} />);
  fireEvent.click(screen.getByRole("button", { name: "打开模拟下单" }));
  expect(onTrade).toHaveBeenCalledOnce();
  expect(setRightDock).toHaveBeenCalledOnce();
  expect(access.require.mock.invocationCallOrder[0]).toBeLessThan(onTrade.mock.invocationCallOrder[0]);
});

test("rail names and popup state describe the panel that is actually open", () => {
  function Rail() {
    const [rightDock, setRightDock] = useState<RightDock>(null);
    const [settingsOpen, setSettingsOpen] = useState(false);
    return <ToolsRail rightDock={rightDock} setRightDock={setRightDock} onTrade={vi.fn()}
      onSettings={() => setSettingsOpen(true)} settingsOpen={settingsOpen} />;
  }
  render(<Rail />);
  fireEvent.click(screen.getByRole("button", { name: "打开调研面板" }));
  expect(screen.getByRole("button", { name: "收起调研面板" })).toHaveAttribute("aria-expanded", "true");
  fireEvent.click(screen.getByRole("button", { name: "收起调研面板" }));
  expect(screen.getByRole("button", { name: "打开调研面板" })).toHaveAttribute("aria-expanded", "false");
  const settings = screen.getByRole("button", { name: "设置" });
  expect(settings).toHaveAttribute("aria-haspopup", "dialog");
  expect(settings).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(settings);
  expect(settings).toHaveAttribute("aria-expanded", "true");
});

test("blocked settings show feedback without mounting private content, even when selected externally", () => {
  const privateMount = vi.fn();
  function Private() { privateMount(); return <div>私人设置</div>; }
  function Example() {
    const [message, setMessage] = useState("");
    return <><span role="status">{message}</span><SettingsPanel selectedPage="account" onClose={vi.fn()} pages={[
      { id: "account", label: "账户与模型", blockedReason: "登录后可使用此功能", onAttempt: () => setMessage("登录后可使用此功能"), content: <Private /> },
      { id: "ui", label: "界面", content: <div>主题设置</div> },
    ]} /></>;
  }
  render(<Example />);
  expect(screen.getByText("主题设置")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "账户与模型" }));
  expect(screen.getByRole("status")).toHaveTextContent("登录后可使用此功能");
  expect(privateMount).not.toHaveBeenCalled();
});

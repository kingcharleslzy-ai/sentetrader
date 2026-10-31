import { fireEvent, render, screen } from "@testing-library/react";
import { XiaomiRemoteSettings, applyRemoteSettings } from "./XiaomiRemoteSettings";
import { xiaomiRemoteStore } from "../../xiaomiRemote/store";

vi.mock("../../xiaomiRemote/store", () => ({
  REMOTE_KEYS: ["up", "down", "left", "right", "ok", "back", "home", "menu", "tv", "volumeUp", "volumeDown", "voice", "power"].map(id => ({ id, label: id })),
  REMOTE_ACTIONS: ["none", "ptt", "chat"].map(id => ({ id, label: id })),
  useXiaomiRemote: () => ({ enabled: true, phase: "ready", supported: true, platform: "web", bindings: { voice: "ptt", power: "none" }, availableKeys: ["voice"], diagnostic: false, frames: 0, level: 0 }),
  xiaomiRemoteStore: { snapshot: vi.fn(() => ({ phase: "ready" })), setEnabled: vi.fn(), setBinding: vi.fn(), resetBindings: vi.fn(), setDiagnostic: vi.fn(), connect: vi.fn(), disconnect: vi.fn() },
}));

afterEach(() => vi.clearAllMocks());

test("all 13 keys remain configurable on Web, with ptt restricted to voice", () => {
  render(<XiaomiRemoteSettings page="remote-keys" />);
  expect(screen.getAllByRole("combobox")).toHaveLength(13);
  expect(screen.getAllByText("浏览器不支持，Mac客户端可用")).toHaveLength(12);
  expect(screen.getByRole("combobox", { name: "up动作" }).querySelector('option[value="ptt"]')).toBeNull();
  fireEvent.change(screen.getByRole("combobox", { name: "power动作" }), { target: { value: "chat" } });
  expect(xiaomiRemoteStore.setBinding).toHaveBeenCalledWith("power", "chat");
  fireEvent.click(screen.getByRole("button", { name: "恢复默认按键" }));
  expect(xiaomiRemoteStore.resetBindings).toHaveBeenCalledOnce();
});

test("invalid command rejects the whole edit before changing local settings", () => {
  expect(() => applyRemoteSettings({ enabled: false, bindings: { power: "ptt" } })).toThrow();
  expect(xiaomiRemoteStore.setEnabled).not.toHaveBeenCalled();
  expect(() => applyRemoteSettings({ connect: true })).toThrow("连接请在页面点击连接");
});

test("diagnostics stop when leaving the page", () => {
  const view = render(<XiaomiRemoteSettings page="remote-diagnostics" />);
  fireEvent.click(screen.getByRole("button", { name: "开始测试" }));
  expect(xiaomiRemoteStore.setDiagnostic).toHaveBeenCalledWith(true);
  view.unmount();
  expect(xiaomiRemoteStore.setDiagnostic).toHaveBeenLastCalledWith(false);
});


test("conflicting batches and disconnected diagnostic starts have no side effects", () => {
  for (const command of [{}, { enabled: null }, { diagnostic: true, enabled: false }, { diagnostic: true, disconnect: true }, { disconnect: true, enabled: true }, { resetBindings: true, bindings: { voice: "ptt" } }]) {
    expect(() => applyRemoteSettings(command)).toThrow();
  }
  expect(xiaomiRemoteStore.setEnabled).not.toHaveBeenCalled();
  expect(xiaomiRemoteStore.setDiagnostic).not.toHaveBeenCalled();
  vi.mocked(xiaomiRemoteStore.snapshot).mockReturnValueOnce({ phase: "idle" } as ReturnType<typeof xiaomiRemoteStore.snapshot>);
  expect(() => applyRemoteSettings({ enabled: true, diagnostic: true })).toThrow("请先连接");
  expect(xiaomiRemoteStore.setEnabled).not.toHaveBeenCalled();
});

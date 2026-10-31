// Protects: a browser that cannot take lock-screen push (Chrome) shows no 显示金额和手数 switch unless another device has push on, and then says where it applies.
import { render, screen } from "@testing-library/react";
import type { PushController } from "../../webPush";
import { LockScreenSection } from "../reminders/ReminderSettings";

const push = (support: PushController["support"]["support"], devices: { deviceId: string; platform: string; createdAt: string }[]) => ({
  support: { support, reason: support === "supported" ? "ok" : "chrome-desktop", label: support === "supported" ? "Mac · Safari" : "Mac · Chrome", device: "这台电脑" },
  status: { configured: true, publicKey: "k", devices }, deviceId: "here", thisDevice: undefined, busy: false, notice: "", fcm: false,
  enable: vi.fn(), disable: vi.fn(), remove: vi.fn(), test: vi.fn(), reload: vi.fn(),
}) as unknown as PushController;
const section = (controller: PushController) => render(<LockScreenSection push={controller} offline={false} devicesOpen={false}
  setDevicesOpen={vi.fn()} amounts onAmounts={vi.fn()} />);
const iphone = [{ deviceId: "phone", platform: "iPhone · 桌面图标", createdAt: "2026-10-06T01:00:00Z" }];

test("Chrome with no device on: the amounts switch is gone", () => {
  section(push("unsupported", []));
  expect(screen.queryByRole("switch", { name: "显示金额和手数" })).toBeNull();
  expect(screen.queryByText("显示金额和手数")).toBeNull();
});

test("Chrome while the iPhone has push on: the switch stays and says it applies to those devices", () => {
  section(push("unsupported", iphone));
  expect(screen.getByText(/对你已开锁屏提醒的设备生效/)).toBeInTheDocument();
});

test("a browser that takes push keeps the switch as before", () => {
  section(push("supported", []));
  expect(screen.getByRole("switch", { name: "显示金额和手数" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "显示金额和手数的说明" })).toBeInTheDocument();
});

test("the desktop app (no web push) keeps only 显示金额和手数, for the devices that have push on", () => {
  render(<LockScreenSection offline={false} devicesOpen={false} setDevicesOpen={vi.fn()} amounts onAmounts={vi.fn()} />);
  expect(screen.getByRole("region", { name: "锁屏推送" })).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "显示金额和手数" })).toBeInTheDocument();
  expect(screen.queryByText(/这台电脑收锁屏提醒/)).toBeNull();
});

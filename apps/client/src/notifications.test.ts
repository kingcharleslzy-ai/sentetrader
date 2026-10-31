import { announceReminder, configureNotifications, notificationPermission } from "./notifications";
import { DEFAULT_PREFERENCES } from "./userPreferences";

// 统一提醒 (#932): which kinds notify is the Runtime's (feed.reminder arrives already gated); the
// window posts what arrives as long as the browser permits it.
test("announceReminder posts what the Runtime delivered while the browser permits it", () => {
  const created: string[] = [];
  class FakeNotification { static permission = "granted"; constructor(title: string) { created.push(title); } }
  vi.stubGlobal("Notification", FakeNotification);
  configureNotifications({ ...DEFAULT_PREFERENCES.notify, sound: false, desktop: { ...DEFAULT_PREFERENCES.notify.desktop, fills: false } });
  announceReminder("成交 SHFE.rb2610");
  expect(created).toEqual(["成交 SHFE.rb2610"]);
  FakeNotification.permission = "default";
  announceReminder("成交 2");
  expect(created).toHaveLength(1);
  expect(notificationPermission()).toBe("default");
  vi.unstubAllGlobals();
  expect(notificationPermission()).toBe("unsupported");
  configureNotifications(undefined);
});

test("on the desktop, announceReminder posts through the shell and the permission comes from the shell's probe", async () => {
  const invoke = vi.fn(async (command: string, args?: Record<string, unknown>) => command === "desktop_notification_permission" ? (args?.request ? "granted" : "default") : undefined);
  vi.doMock("@tauri-apps/api/core", () => ({ invoke }));
  vi.stubGlobal("__TAURI_INTERNALS__", {});
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
  const { announceReminder, configureNotifications, notificationPermission, refreshNotificationPermission, requestNotificationPermission } = await import("./notifications");
  configureNotifications({ ...DEFAULT_PREFERENCES.notify, sound: false });
  expect(await refreshNotificationPermission()).toBe("default");
  expect(notificationPermission()).toBe("default");
  expect(await requestNotificationPermission()).toBe("granted");
  announceReminder("成交 SHFE.rb2610", "买 开 1 手");
  await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith("desktop_notify", { title: "成交 SHFE.rb2610", body: "买 开 1 手" }));
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  vi.unstubAllGlobals(); vi.doUnmock("@tauri-apps/api/core");
  configureNotifications(undefined);
});

// Protects who is offered the 锁屏提醒 switch (never a browser on Google's FCM, an iPhone only from its home-screen icon), that a notification's link opens only the places it may name, and (#521) that a sign-out or another login mid-打开 never saves the old user's subscription, a sign-out names this device, and a late sign-out retirement or 关闭 never unsubscribes or DELETEs the next user's push.
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { signOutOwner, takePushRetirement } from "./api";
import { parseDeepLink } from "./deepLink";
import { webDeviceId } from "./nativeAgent";
import { resetReadIdentity } from "./runtimeClient";
import { applicationServerKey, isFcmEndpoint, pushRowValue, pushSupportFor, usePush } from "./webPush";

const UA = {
  iphoneSafari: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
  wechat: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50",
  ipad: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15",
  android: "Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36",
  huawei: "Mozilla/5.0 (Linux; Android 12; NOH-AN00) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 HuaweiBrowser/15.0 Mobile Safari/537.36",
  chromeMac: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
  edgeWindows: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
  safariMac: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15",
  firefox: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
};
const support = (userAgent: string, extra: Partial<Parameters<typeof pushSupportFor>[0]> = {}) =>
  pushSupportFor({ userAgent, standalone: false, maxTouchPoints: 0, hasPushManager: true, tauri: false, ...extra });

describe("锁屏提醒 support", () => {
  it.each([
    ["iPhone in a Safari tab", support(UA.iphoneSafari, { hasPushManager: false }), "needs-install", "ios-browser"],
    ["iPhone in WeChat", support(UA.wechat), "needs-install", "ios-browser"],
    ["iPhone from its home-screen icon", support(UA.iphoneSafari, { standalone: true }), "supported", "ok"],
    ["iPhone below iOS 16.4 on the home screen", support(UA.iphoneSafari, { standalone: true, hasPushManager: false }), "unsupported", "ios-old"],
    ["iPad (desktop UA, touch) from its icon", support(UA.ipad, { standalone: true, maxTouchPoints: 5 }), "supported", "ok"],
    ["Android Chrome", support(UA.android), "unsupported", "android"],
    ["Android vendor browser", support(UA.huawei), "unsupported", "android"],
    ["desktop Chrome", support(UA.chromeMac), "unsupported", "chrome-desktop"],
    ["Windows Edge", support(UA.edgeWindows), "supported", "ok"],
    ["Mac Safari", support(UA.safariMac), "supported", "ok"],
    ["Firefox", support(UA.firefox), "supported", "ok"],
    ["the desktop app", support(UA.safariMac, { tauri: true }), "unsupported", "desktop-app"],
  ])("%s", (_, info, expected, reason) => {
    expect(info.support).toBe(expected);
    expect(info.reason).toBe(reason);
  });

  it("names the device the way the device list shows it", () => {
    expect(support(UA.iphoneSafari, { standalone: true }).label).toBe("iPhone · 桌面图标");
    expect(support(UA.safariMac).label).toBe("Mac · Safari");
    expect(support(UA.edgeWindows).label).toBe("Windows · Edge");
  });

  it("never uploads an FCM subscription and decodes the server key", () => {
    expect(isFcmEndpoint("https://fcm.googleapis.com/fcm/send/abc")).toBe(true);
    expect(isFcmEndpoint("https://web.push.apple.com/abc")).toBe(false);
    expect(Array.from(applicationServerKey("BAEC_w"))).toEqual([4, 1, 2, 255]);
  });

  it("says on 我的 what this device can do", () => {
    expect(pushRowValue(support(UA.iphoneSafari), false)).toBe("先装到桌面");
    expect(pushRowValue(support(UA.android), false)).toBe("不支持");
    expect(pushRowValue(support(UA.safariMac), true)).toBe("已开");
    expect(pushRowValue(support(UA.safariMac), false)).toBe("未开");
  });
});

describe("a notification's link", () => {
  it("opens only the places it may name", () => {
    expect(parseDeepLink("card:reminder-movement-SHFE.rb2610-123-up")).toEqual({ kind: "card", id: "reminder-movement-SHFE.rb2610-123-up" });
    expect(parseDeepLink("mobilePage:positions")).toEqual({ kind: "positions" });
    expect(parseDeepLink("research")).toEqual({ kind: "research" });
    expect(parseDeepLink("symbol:SHFE.rb2610")).toEqual({ kind: "symbol", symbol: "SHFE.rb2610" });
    // 外盘到价提醒的锁屏推送打开外盘图表（外盘代码统一 2026-10-21）。
    expect(parseDeepLink("symbol:KQD.m@COMEX.GC")).toEqual({ kind: "symbol", symbol: "KQD.m@COMEX.GC" });
    expect(parseDeepLink("symbol:CME.GCEZ26")).toEqual({ kind: "symbol", symbol: "CME.GCEZ26" });
    // 收盘复盘 (2026-10-06): the review's trading day; anything else after review: is ignored.
    expect(parseDeepLink("review:2026-10-09")).toEqual({ kind: "review", day: "2026-10-09" });
    expect(parseDeepLink("review:10-09")).toBeNull();
    for (const bad of ["mobilePage:order", "symbol:<script>", "card:", "settings", null]) expect(parseDeepLink(bad)).toBeNull();
  });
});

describe("锁屏提醒 follows who is signed in (#521)", () => {
  const BASE = "https://runtime.example.test";
  const json = (data: unknown) => new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
  const later = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { promise, resolve }; };

  /** A Safari on a Mac with a service worker, a push manager and a fetch that records each call. */
  function browser() {
    const subscription = { endpoint: "https://web.push.apple.com/device", options: { applicationServerKey: null },
      toJSON: () => ({ endpoint: "https://web.push.apple.com/device", keys: { p256dh: "p", auth: "a" } }), unsubscribe: vi.fn(async () => true) };
    const subscribe = later<typeof subscription>();
    const pushManager = { getSubscription: vi.fn(async () => null), subscribe: vi.fn(() => subscribe.promise) };
    const registration = { pushManager };
    const permission = later<NotificationPermission>();
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(UA.safariMac);
    vi.stubGlobal("PushManager", class {});
    vi.stubGlobal("Notification", { requestPermission: vi.fn(() => permission.promise) });
    Object.defineProperty(navigator, "serviceWorker", { configurable: true,
      value: { ready: Promise.resolve(registration), getRegistration: async () => registration } });
    const calls: { url: string; method: string; body?: string }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: init?.body as string | undefined });
      return json(url.endsWith("/logout") ? { status: "signed_out" } : { configured: true, publicKey: "BAEC_w", devices: [] });
    }));
    return { subscription, subscribe, permission, pushManager, calls };
  }
  afterEach(() => {
    cleanup();
    takePushRetirement();
    vi.useRealTimers();
    resetReadIdentity();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    Reflect.deleteProperty(navigator, "serviceWorker");
  });

  it.each(["permission", "subscribe"] as const)("a sign-out during %s abandons 打开 without saving it", async (stage) => {
    const fake = browser();
    const { result } = renderHook(() => usePush(BASE));
    await act(async () => { await Promise.resolve(); });
    expect(result.current.status?.configured).toBe(true);
    let enabling!: Promise<void>;
    act(() => { enabling = result.current.enable(); });
    if (stage === "subscribe") {
      await act(async () => { fake.permission.resolve("granted"); await vi.waitFor(() => expect(fake.pushManager.subscribe).toHaveBeenCalled()); });
      act(() => resetReadIdentity());  // signed out (and maybe B signed in) while the browser was subscribing
      fake.subscribe.resolve(fake.subscription);
    } else {
      act(() => resetReadIdentity());
      fake.permission.resolve("granted");
    }
    await act(async () => { await enabling; });
    expect(fake.calls.filter(call => call.method === "PUT")).toEqual([]);
    expect(fake.subscription.unsubscribe).toHaveBeenCalledTimes(stage === "subscribe" ? 1 : 0);
    if (stage === "permission") expect(fake.pushManager.subscribe).not.toHaveBeenCalled();
  });

  it("without an identity change 打开 saves this device once, and a second tap meanwhile is dropped", async () => {
    const fake = browser();
    const { result } = renderHook(() => usePush(BASE));
    await act(async () => { await Promise.resolve(); });
    let enabling!: Promise<void>;
    act(() => { enabling = result.current.enable(); void result.current.disable(); });
    fake.permission.resolve("granted");
    fake.subscribe.resolve(fake.subscription);
    await act(async () => { await enabling; });
    expect(fake.calls.filter(call => call.method === "PUT").map(call => JSON.parse(call.body!).deviceId)).toEqual([result.current.deviceId]);
    expect(fake.calls.some(call => call.method === "DELETE")).toBe(false);
  });

  /** A's subscription in the browser, then B's once subscribed; unsubscribing clears the browser's current one. */
  function handover(fake: ReturnType<typeof browser>) {
    const make = (name: string) => {
      const item = { endpoint: `https://web.push.apple.com/${name}`, options: { applicationServerKey: null },
        toJSON: () => ({ endpoint: `https://web.push.apple.com/${name}`, keys: { p256dh: "p", auth: "a" } }),
        unsubscribe: vi.fn(async () => { if (current === item) current = null; return true; }) };
      return item;
    };
    const a = make("a"); const b = make("b");
    let current: typeof a | null = a;
    fake.pushManager.getSubscription.mockImplementation(async () => current as never);
    fake.pushManager.subscribe.mockImplementation(async () => { current = b; return b as never; });
    const registration = later<unknown>();
    const worker = navigator.serviceWorker as unknown as { getRegistration: () => Promise<unknown> };
    const real = worker.getRegistration;
    worker.getRegistration = vi.fn(() => registration.promise);  // the sign-out's chain waits here
    return { a, b, release: async () => { registration.resolve(await real()); } };
  }

  it.each(["released while 打开 waits", "released after 打开 stopped waiting"] as const)(
    "a late sign-out retirement never unsubscribes the next user's new push (%s)", async (when) => {
      const fake = browser();
      const { a, b, release } = handover(fake);
      fake.permission.resolve("granted");
      await signOutOwner(BASE);                                    // A signs out; the chain is still pending
      act(() => resetReadIdentity());                              // B signs in
      if (when !== "released while 打开 waits") vi.useFakeTimers({ toFake: ["setTimeout"] });
      const { result } = renderHook(() => usePush(BASE));
      await act(async () => { await Promise.resolve(); });
    expect(result.current.status?.configured).toBe(true);
      let enabling!: Promise<void>;
      act(() => { enabling = result.current.enable(); });
      if (when === "released while 打开 waits") await act(async () => { await release(); });
      else await act(async () => { await vi.advanceTimersByTimeAsync(2500); await enabling; await release(); });
      await act(async () => { await enabling; await Promise.resolve(); await Promise.resolve(); });
      const put = fake.calls.filter(call => call.method === "PUT").map(call => JSON.parse(call.body!).subscription.endpoint);
      expect(put).toEqual([b.endpoint]);                           // B's server row is B's browser subscription
      expect(b.unsubscribe).not.toHaveBeenCalled();                // and the old chain left it alone
      expect(a.unsubscribe).toHaveBeenCalledTimes(1);              // A's was retired, never reused
      expect(fake.calls.some(call => call.method === "DELETE")).toBe(false);
    });

  it("a 关闭 overtaken by a sign-out and another login neither unsubscribes nor DELETEs for the new user", async () => {
    const fake = browser();
    const { a, release } = handover(fake);
    const { result } = renderHook(() => usePush(BASE));
    await act(async () => { await Promise.resolve(); });
    expect(result.current.status?.configured).toBe(true);
    let disabling!: Promise<void>;
    act(() => { disabling = result.current.disable(); });
    act(() => resetReadIdentity());                                // A signed out, B signed in
    await act(async () => { await release(); await disabling; });
    expect(fake.calls.some(call => call.method === "DELETE")).toBe(false);
    expect(a.unsubscribe).not.toHaveBeenCalled();
  });

  it("a 关闭 with no identity change unsubscribes and removes this device", async () => {
    const fake = browser();
    const { a, release } = handover(fake);
    const { result } = renderHook(() => usePush(BASE));
    await act(async () => { await Promise.resolve(); });
    expect(result.current.status?.configured).toBe(true);
    let disabling!: Promise<void>;
    act(() => { disabling = result.current.disable(); });
    await act(async () => { await release(); await disabling; });
    expect(a.unsubscribe).toHaveBeenCalledTimes(1);
    expect(fake.calls.filter(call => call.method === "DELETE").map(call => call.url)).toEqual([
      `${BASE}/api/app/push/subscription?deviceId=${encodeURIComponent(result.current.deviceId)}`]);
  });

  it("a sign-out names this device and drops the browser's own subscription", async () => {
    const fake = browser();
    fake.pushManager.getSubscription.mockResolvedValue(fake.subscription as never);
    await signOutOwner(BASE);
    const logout = fake.calls.find(call => call.url.endsWith("/api/auth/owner/logout"));
    expect(logout && JSON.parse(logout.body!)).toEqual({ deviceId: webDeviceId() });
    await vi.waitFor(() => expect(fake.subscription.unsubscribe).toHaveBeenCalled());
  });
});

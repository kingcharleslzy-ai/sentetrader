import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { requestJson, takePushRetirement } from "./api";
import { webDeviceId } from "./nativeAgent";
import { currentServiceWorkerRegistration, readyServiceWorkerRegistration } from "./serviceWorkerPolicy";
import { readIdentityGeneration, useReadIdentity } from "./runtimeClient";
import type { components } from "./workbench.generated";

/**
 * 锁屏提醒 (2026-10-06): whether this browser can take lock-screen push, and turning it on or off.
 * The Runtime sends standard Web Push; Google's FCM (Chrome, every Android browser) cannot be
 * reached from there, so those browsers get an explanation instead of a switch. An iPhone takes
 * push only from its home-screen icon (iOS 16.4+). Turning it on must happen inside the user's tap:
 * Notification.requestPermission → pushManager.subscribe → PUT /api/app/push/subscription.
 */
export type PushStatus = components["schemas"]["PushStatus"];
export type PushDevice = components["schemas"]["PushDevice"];
export type PushTestResult = components["schemas"]["PushTestResult"];

export type PushSupport = "supported" | "needs-install" | "unsupported";
export type PushReason = "ok" | "ios-browser" | "ios-old" | "android" | "chrome-desktop" | "desktop-app" | "no-push";
export interface PushSupportInfo {
  support: PushSupport;
  reason: PushReason;
  /** The device's name in the device list, e.g. iPhone · 桌面图标, Mac · Safari. */
  label: string;
  /** iPhone / iPad: the word the page uses for this device. */
  device: string;
}

export interface PushSupportInput {
  userAgent: string;
  standalone: boolean;
  maxTouchPoints: number;
  hasPushManager: boolean;
  tauri: boolean;
}

export function pushSupportFor({ userAgent: ua, standalone, maxTouchPoints, hasPushManager, tauri }: PushSupportInput): PushSupportInfo {
  if (tauri) return { support: "unsupported", reason: "desktop-app", label: "Sente 桌面版", device: "这台电脑" };
  const ipad = /iPad/.test(ua) || (/Macintosh/.test(ua) && maxTouchPoints > 1);
  if (ipad || /iPhone|iPod/.test(ua)) {
    const device = ipad ? "iPad" : "iPhone";
    const label = `${device} · 桌面图标`;
    if (!standalone) return { support: "needs-install", reason: "ios-browser", label, device };
    return hasPushManager ? { support: "supported", reason: "ok", label, device } : { support: "unsupported", reason: "ios-old", label, device };
  }
  if (/Android/i.test(ua)) return { support: "unsupported", reason: "android", label: "安卓手机", device: "这台手机" };
  const os = /Windows/.test(ua) ? "Windows" : /Mac OS X|Macintosh/.test(ua) ? "Mac" : /Linux|CrOS/.test(ua) ? "Linux" : "电脑";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox"
    : /Chrome\/|Chromium\/|CriOS/.test(ua) ? "Chrome" : /Version\/\d+.*Safari\//.test(ua) ? "Safari" : "";
  const label = `${os} · ${browser || "浏览器"}`;
  if (browser === "Chrome") return { support: "unsupported", reason: "chrome-desktop", label, device: "这台电脑" };
  if (!hasPushManager || !browser) return { support: "unsupported", reason: "no-push", label, device: "这台电脑" };
  return { support: "supported", reason: "ok", label, device: "这台电脑" };
}

export function currentPushSupport(): PushSupportInfo {
  if (typeof window === "undefined") return pushSupportFor({ userAgent: "", standalone: false, maxTouchPoints: 0, hasPushManager: false, tauri: false });
  let standalone = (navigator as Navigator & { standalone?: boolean }).standalone === true;
  try { standalone ||= window.matchMedia?.("(display-mode: standalone), (display-mode: fullscreen)").matches === true; } catch { /* old WebKit */ }
  return pushSupportFor({ userAgent: navigator.userAgent, standalone, maxTouchPoints: navigator.maxTouchPoints ?? 0,
    hasPushManager: "PushManager" in window && "serviceWorker" in navigator && typeof Notification !== "undefined",
    tauri: "__TAURI_INTERNALS__" in window });
}

/** Google's push service: unreachable from the Runtime, so such a subscription is never uploaded. */
export function isFcmEndpoint(endpoint: string): boolean {
  try { const host = new URL(endpoint).hostname.toLowerCase(); return host.endsWith(".googleapis.com"); } catch { return false; }
}

export function applicationServerKey(publicKey: string): Uint8Array<ArrayBuffer> {
  const padded = publicKey.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (publicKey.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(new ArrayBuffer(binary.length));
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function sameKey(current: ArrayBuffer | null | undefined, wanted: Uint8Array): boolean {
  if (!current) return false;
  const bytes = new Uint8Array(current);
  return bytes.length === wanted.length && bytes.every((value, index) => value === wanted[index]);
}

/** How long 打开 waits for a sign-out still retiring the previous subscription. */
const RETIREMENT_WAIT_MS = 2000;

function errorText(error: unknown): string {
  const code = ((error as { detail?: { detail?: { code?: string } } }).detail?.detail?.code) ?? (error as Error).message;
  if (code === "push_not_configured") return "服务器还没开通锁屏提醒";
  if (code === "push_endpoint_unsupported") return "这个浏览器走谷歌推送，国内收不到；请换 Safari 或 Edge";
  if (code === "worker_not_ready") return "后台脚本还没就绪，刷新页面后再试";
  if ((error as { name?: string }).name === "NotAllowedError") return "通知被拒绝了：到系统设置 → 通知 → Sente 里打开";
  return "没能完成，请稍后再试";
}

/** What the 锁屏提醒 row on 我的 says on its right. */
export function pushRowValue(support: PushSupportInfo, subscribed: boolean): string {
  if (support.support === "needs-install") return "先装到桌面";
  if (support.support === "unsupported") return "不支持";
  return subscribed ? "已开" : "未开";
}

export interface PushController {
  support: PushSupportInfo;
  status: PushStatus | undefined;
  deviceId: string;
  /** This device's row on the Runtime (it has push on). */
  thisDevice: PushDevice | undefined;
  busy: boolean;
  notice: string;
  /** The browser handed back a Google FCM subscription: shown as "收不到" and never uploaded. */
  fcm: boolean;
  enable: () => Promise<void>;
  disable: () => Promise<void>;
  remove: (deviceId: string) => Promise<void>;
  test: (deviceId?: string) => Promise<void>;
  reload: () => void;
}

/** One device's lock-screen push, read from and written to the connected Runtime. */
export function usePush(baseUrl: string | undefined, enabled = true): PushController {
  const support = useMemo(() => currentPushSupport(), []);
  const deviceId = useMemo(() => webDeviceId(), []);
  const [status, setStatus] = useState<PushStatus>();
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [fcm, setFcm] = useState(false);
  const [readKey, setReadKey] = useState(0);
  // A sign-out or another login: the previous user's device list is not shown, it is read again.
  const identity = useReadIdentity();
  useEffect(() => { setStatus(undefined); }, [baseUrl, identity]);
  useEffect(() => {
    if (!baseUrl || !enabled) { setStatus(undefined); return; }
    let cancelled = false;
    // An older Runtime answers 404 (or something else): no status, so no switch.
    requestJson<PushStatus>(`${baseUrl}/api/app/push`).then(next => { if (!cancelled && Array.isArray(next?.devices)) setStatus(next); }, () => undefined);
    return () => { cancelled = true; };
  }, [baseUrl, enabled, readKey, identity]);
  const reload = useCallback(() => setReadKey(key => key + 1), []);
  // One action at a time: a 关闭 or 移除 tapped while 打开 is still running is dropped, not raced.
  const running = useRef(false);
  const run = useCallback(async (action: () => Promise<void>) => {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    try { await action(); } catch (error) { setNotice(errorText(error)); } finally { running.current = false; setBusy(false); }
  }, []);
  const enable = useCallback(() => run(async () => {
    if (!baseUrl || support.support !== "supported") return;
    if (!status?.configured || !status.publicKey) { setNotice("服务器还没开通锁屏提醒"); return; }
    // #521: the user who tapped 打开 must still be the one signed in at every step; a sign-out or
    // another login meanwhile abandons it, so A's subscription is never saved under B.
    const generation = readIdentityGeneration();
    const stale = () => readIdentityGeneration() !== generation;
    // The permission prompt first: iOS shows it only inside the tap that asked.
    const permission = await Notification.requestPermission();
    if (stale()) return;
    if (permission !== "granted") {
      setNotice(permission === "denied" ? "通知被拒绝了：到系统设置 → 通知 → Sente 里打开" : "没有允许通知，锁屏提醒还没打开");
      return;
    }
    const registration = await readyServiceWorkerRegistration();
    if (stale()) return;
    // A sign-out on this page may still be retiring the previous user's subscription: let it finish
    // (briefly), stop it from touching anything after this, and never reuse what it was retiring.
    const retirement = takePushRetirement();
    if (retirement) {
      await Promise.race([retirement.done, new Promise(resolve => setTimeout(resolve, RETIREMENT_WAIT_MS))]);
      retirement.superseded = true;
      if (stale()) return;
    }
    const key = applicationServerKey(status.publicKey);
    let subscription = await registration.pushManager.getSubscription();
    if (stale()) return;
    if (subscription && (retirement || !sameKey(subscription.options?.applicationServerKey, key))) {
      await subscription.unsubscribe();
      subscription = null;
      if (stale()) return;
    }
    subscription ??= await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    if (stale()) { await subscription.unsubscribe().catch(() => undefined); return; }
    if (isFcmEndpoint(subscription.endpoint)) {
      await subscription.unsubscribe().catch(() => undefined);
      setFcm(true);
      setNotice("这个浏览器走谷歌推送，国内收不到；请换 Safari 或 Edge");
      return;
    }
    const json = subscription.toJSON();
    const next = await requestJson<PushStatus>(`${baseUrl}/api/app/push/subscription`, {
      method: "PUT", headers: { "content-type": "application/json" },
      body: JSON.stringify({ deviceId, platform: support.label,
        subscription: { endpoint: json.endpoint, keys: json.keys, expirationTime: json.expirationTime ?? null } }) });
    if (stale()) return;
    setStatus(next);
    setNotice("");
  }), [baseUrl, deviceId, run, status, support]);
  const remove = useCallback((target: string) => run(async () => {
    if (!baseUrl) return;
    // #521: 关闭/移除 belongs to the user who tapped it; once the identity changes it neither touches
    // the browser's (by then maybe the new user's) subscription nor DELETEs with the new credentials.
    const generation = readIdentityGeneration();
    const stale = () => readIdentityGeneration() !== generation;
    if (target === deviceId) {
      try {
        const subscription = await (await currentServiceWorkerRegistration())?.pushManager.getSubscription();
        if (stale()) return;
        await subscription?.unsubscribe();
      } catch { /* the row goes either way */ }
      if (stale()) return;
    }
    const next = await requestJson<PushStatus>(`${baseUrl}/api/app/push/subscription?deviceId=${encodeURIComponent(target)}`, { method: "DELETE" });
    if (stale()) return;
    setStatus(next);
    setNotice("");
  }), [baseUrl, deviceId, run]);
  const disable = useCallback(() => remove(deviceId), [deviceId, remove]);
  const test = useCallback((target?: string) => run(async () => {
    if (!baseUrl) return;
    const { results } = await requestJson<{ results: PushTestResult[] }>(`${baseUrl}/api/app/push/test`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(target ? { deviceId: target } : {}) });
    const failed = results.filter(item => item.status !== "sent");
    setNotice(!results.length ? "还没有设备开了锁屏提醒" : failed.length
      ? `${failed.map(item => item.platform).join("、")}没收到${failed.some(item => item.status === "removed") ? "（订阅已失效，已移除）" : ""}` : "已发送");
    if (failed.length) reload();
  }), [baseUrl, reload, run]);
  const thisDevice = status?.devices?.find(item => item.deviceId === deviceId);
  return { support, status, deviceId, thisDevice, busy, notice, fcm, enable, disable, remove, test, reload };
}

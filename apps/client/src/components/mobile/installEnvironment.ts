/**
 * 安装到桌面 (2026-10-06): which browser the phone page runs in, what that browser lets us do
 * about it, and when the guide may open by itself. Pure functions plus one small store for the
 * Android `beforeinstallprompt` event (it can fire before React mounts, so main.tsx starts
 * listening at load). Detection rules follow khmyznikov/pwa-install and philfung/add-to-homescreen
 * (MIT); neither knows the Chinese browsers, which are added here.
 */

export type InstallEnvironment =
  | "installed" | "in-app-ios" | "in-app-android" | "ios-safari-26" | "ios-safari-legacy" | "ios-chrome-edge"
  | "ios-other" | "android-prompt" | "android-vendor" | "desktop";

export type InAppHost = "wechat" | "qq" | "dingtalk" | "alipay";
export type AndroidVendor = "huawei" | "honor" | "xiaomi" | "oppo" | "vivo" | "quark" | "uc" | "qq-browser" | "samsung" | "chrome" | "generic";

export interface InstallDetection {
  environment: InstallEnvironment;
  /** in-app-*: the app whose built-in browser this is. */
  app?: InAppHost;
  /** android-vendor: the browser, for its name in the steps. */
  vendor?: AndroidVendor;
  /** ios-other: the browser's name, when we know it. */
  browser?: string;
  /** iPadOS: Safari's buttons sit at the top, not the bottom. */
  ipad?: boolean;
}

export interface InstallEnvironmentInput {
  userAgent: string;
  /** display-mode: standalone, or iOS's navigator.standalone. */
  standalone: boolean;
  maxTouchPoints: number;
  platform?: string;
  /** Android: the browser handed us a `beforeinstallprompt` event. */
  hasInstallPrompt: boolean;
}

const IN_APP: [RegExp, InAppHost][] = [[/MicroMessenger/i, "wechat"], [/\sQQ\/\d/, "qq"], [/DingTalk/i, "dingtalk"], [/AlipayClient/i, "alipay"]];
// iOS browsers that cannot add to the home screen at all (Apple lets only Safari, Chrome and Edge).
const IOS_OTHER: [RegExp, string][] = [[/Quark/i, "夸克"], [/UCBrowser/i, "UC 浏览器"], [/baiduboxapp/i, "百度"], [/MQQBrowser/i, "QQ 浏览器"],
  [/FxiOS/i, "Firefox"], [/OPiOS|OPT\//i, "Opera"], [/DuckDuckGo/i, "DuckDuckGo"], [/SogouMobileBrowser/i, "搜狗浏览器"]];
// Android browsers by their own UA token. The browser tokens come before the phone-brand ones: a
// Quark on a Huawei phone is Quark. Chrome reduces the model to "K", so HONOR there is the browser.
const ANDROID_VENDORS: [RegExp, AndroidVendor][] = [
  [/Quark/i, "quark"], [/UCBrowser/i, "uc"], [/MQQBrowser/i, "qq-browser"], [/SamsungBrowser/i, "samsung"], [/HuaweiBrowser/i, "huawei"],
  [/HonorBrowser|\bHONOR\b/, "honor"], [/MiuiBrowser|XiaoMi/i, "xiaomi"], [/HeyTapBrowser|OppoBrowser/i, "oppo"], [/VivoBrowser/i, "vivo"],
];

export function detectInstallEnvironment({ userAgent: ua, standalone, maxTouchPoints, hasInstallPrompt }: InstallEnvironmentInput): InstallDetection {
  if (standalone) return { environment: "installed" };
  const ipad = /iPad/.test(ua) || (/Macintosh/.test(ua) && maxTouchPoints > 1);
  const ios = ipad || /iPhone|iPod/.test(ua);
  const android = !ios && /Android/i.test(ua);
  const app = IN_APP.find(([pattern]) => pattern.test(ua))?.[1];
  if (app && (ios || android)) return { environment: ios ? "in-app-ios" : "in-app-android", app };
  if (ios) {
    if (/CriOS|EdgiOS/.test(ua)) return { environment: "ios-chrome-edge", ipad: ipad || undefined };
    const other = IOS_OTHER.find(([pattern]) => pattern.test(ua));
    if (other || !/Version\/\d+/.test(ua) || !/Safari\//.test(ua)) return { environment: "ios-other", browser: other?.[1] };
    const version = Number(/Version\/(\d+)/.exec(ua)?.[1] ?? 0);
    return { environment: version >= 26 ? "ios-safari-26" : "ios-safari-legacy", ipad: ipad || undefined };
  }
  if (android) {
    if (hasInstallPrompt) return { environment: "android-prompt" };
    const vendor = ANDROID_VENDORS.find(([pattern]) => pattern.test(ua))?.[1]
      ?? (/Chrome\/\d/.test(ua) && !/Edg|OPR|Firefox|wv\)/.test(ua) ? "chrome" : "generic");
    return { environment: "android-vendor", vendor };
  }
  return { environment: "desktop" };
}

// ── The guide's words ───────────────────────────────────────────────────────────────────────────
// Confirmed names: iOS Safari「分享」「添加到主屏幕」「作为网页 App 打开」「添加」(iOS 26 and earlier),
// iOS Chrome / Edge「分享」「添加到主屏幕」, Android Chrome ⋮「添加到主屏幕」/「安装应用」, 微信「在浏览器打开」
// (Android) /「在默认浏览器中打开」(iOS). Generic wording (not verified per version): the Chinese
// vendor browsers' menus, QQ / 钉钉 / 支付宝's menu items and the shortcut-permission path.

export type GuideGlyph = "more" | "share" | "plus" | "menu" | "vmore";
export type GuideStep = (string | { glyph: GuideGlyph })[];
/** Where the arrow points: the browser button just outside the page. */
export type GuidePointer = "bottom-right" | "bottom-center" | "top-right" | "top-corner";

export interface GuideContent {
  layout: "sheet" | "mask" | "prompt" | "done";
  title: string;
  subtitle: string;
  steps: GuideStep[];
  pointer?: GuidePointer;
  /** A second action beside 以后再说: copy the address for another browser. */
  copy?: string;
}

const APP_NAMES: Record<InAppHost, string> = { wechat: "微信", qq: "QQ", dingtalk: "钉钉", alipay: "支付宝" };
const VENDOR_NAMES: Record<AndroidVendor, string> = {
  huawei: "华为浏览器", honor: "荣耀浏览器", xiaomi: "小米浏览器", oppo: "OPPO 浏览器", vivo: "vivo 浏览器", quark: "夸克",
  uc: "UC 浏览器", "qq-browser": "QQ 浏览器", samsung: "三星浏览器", chrome: "Chrome", generic: "手机浏览器",
};
const VENDOR_SHORTCUT = new Set<AndroidVendor>(["huawei", "honor", "xiaomi", "oppo", "vivo"]);

export function installGuideContent(detection: InstallDetection): GuideContent {
  const { environment } = detection;
  switch (environment) {
    case "installed":
      return { layout: "done", title: "Sente 已在桌面", subtitle: "你现在就是从桌面打开的，不用再装", steps: [] };
    case "ios-safari-26":
      return detection.ipad
        ? { layout: "sheet", title: "把 Sente 放到主屏幕", subtitle: "像 App 一样从桌面打开，全屏显示", pointer: "top-right", steps: [
          ["点顶部的分享 ", { glyph: "share" }, "（找不到就点 ", { glyph: "more" }, " 里的「分享」）"],
          ["往下找到「添加到主屏幕」"], ["保持「作为网页 App 打开」开着，点「添加」"]] }
        : { layout: "sheet", title: "把 Sente 放到主屏幕", subtitle: "像 App 一样从桌面打开，全屏显示", pointer: "bottom-right", steps: [
          ["点右下角 ", { glyph: "more" }], ["点「分享」", { glyph: "share" }, "，往下找到「添加到主屏幕」"], ["保持「作为网页 App 打开」开着，点「添加」"]] };
    case "ios-safari-legacy":
      return { layout: "sheet", title: "把 Sente 放到主屏幕", subtitle: "像 App 一样从桌面打开，全屏显示",
        pointer: detection.ipad ? "top-right" : "bottom-center", steps: [
          [detection.ipad ? "点顶部的分享 " : "点底部中间的分享 ", { glyph: "share" }], ["往下滑，点「添加到主屏幕」", { glyph: "plus" }], ["点右上角「添加」"]] };
    case "ios-chrome-edge":
      return { layout: "sheet", title: "把 Sente 放到主屏幕", subtitle: "Chrome、Edge 也能放", pointer: "top-right", steps: [
        ["点地址栏右边的分享 ", { glyph: "share" }], ["点「添加到主屏幕」"], ["点「添加」"]] };
    case "ios-other":
      return { layout: "sheet", title: "用 Safari 才能放到主屏幕", subtitle: `${detection.browser ?? "这个浏览器"}不能把网页放到主屏幕`, copy: "复制链接", steps: [
        ["点下面的「复制链接」"], ["打开 Safari，粘贴到地址栏打开"], ["按 Safari 里的提示放到主屏幕"]] };
    case "in-app-ios":
    case "in-app-android": {
      const ios = environment === "in-app-ios";
      const option = detection.app === "wechat" ? ios ? "在默认浏览器中打开" : "在浏览器打开" : ios ? "在浏览器打开」或「在 Safari 中打开" : "在浏览器打开";
      const name = APP_NAMES[detection.app ?? "wechat"];
      return { layout: "mask", title: `点右上角 ···\n选「${option}」`, copy: ios ? "没有这个选项？复制链接" : "复制链接", steps: [],
        subtitle: ios ? `${name}里不能放到桌面，在 Safari 打开后会自动教你下一步。` : "换到手机浏览器后，能一键装到桌面。" };
    }
    case "android-prompt":
      return { layout: "prompt", title: "把 Sente 装到桌面", subtitle: "点一下就装好，之后从桌面打开", steps: [] };
    case "android-vendor": {
      const vendor = detection.vendor ?? "generic";
      if (vendor === "chrome") return { layout: "sheet", title: "把 Sente 放到桌面", subtitle: "Chrome", pointer: "top-corner", steps: [
        ["点右上角 ", { glyph: "vmore" }], ["点「添加到主屏幕」或「安装应用」"], ["点「安装」或「添加」"]] };
      return { layout: "sheet", title: "把 Sente 放到桌面", subtitle: VENDOR_NAMES[vendor], copy: "换浏览器打开", steps: [
        ["点浏览器菜单 ", { glyph: "menu" }, " 或 ", { glyph: "vmore" }],
        ["找「添加到桌面」或「添加到主屏幕」"],
        [VENDOR_SHORTCUT.has(vendor) || vendor === "generic"
          ? `桌面上没出现：到手机 设置 → 应用 → ${vendor === "generic" ? "这个浏览器" : VENDOR_NAMES[vendor]} → 权限，允许创建桌面快捷方式`
          : "桌面上没出现：在手机设置里允许这个浏览器创建桌面快捷方式"]] };
    }
    default:
      return { layout: "done", title: "在手机上放到桌面", subtitle: "用手机浏览器打开 qilinshuzhi.com/trader，按提示放到桌面；电脑上请用桌面版。", steps: [] };
  }
}

// ── When it opens by itself ─────────────────────────────────────────────────────────────────────

export const INSTALL_RECORD_KEY = "sente-install-guide-v1";
export const INSTALL_COOLDOWN_MS = 7 * 24 * 60 * 60 * 1000;
export const INSTALL_MAX_DISMISSALS = 3;

/** What this browser remembers: page opens, closes of an auto-opened guide, and whether an order
 *  was confirmed or an AI reply arrived here. */
export interface InstallRecord { opens: number; dismissals: number; lastDismissedAt?: number; engaged?: boolean }

export interface AutoOpenInput {
  /** null: the record could not be read (private mode, storage blocked) — then never. */
  record: InstallRecord | null;
  now: number;
  environment: InstallEnvironment;
  mobileLayout: boolean;
  tauri: boolean;
  signedIn: boolean;
  /** Already opened by itself during this page load. */
  shownThisVisit: boolean;
  /** Something in progress: the guide waits until it ends. */
  holdingToTalk: boolean;
  pageOpen: boolean;
  pendingOrder: boolean;
}

export function shouldAutoOpenInstall(input: AutoOpenInput): boolean {
  const { record } = input;
  if (!record || !input.mobileLayout || input.tauri || !input.signedIn || input.shownThisVisit) return false;
  if (input.environment === "installed" || input.environment === "desktop") return false;
  if (record.dismissals >= INSTALL_MAX_DISMISSALS) return false;
  if (record.lastDismissedAt !== undefined && input.now - record.lastDismissedAt < INSTALL_COOLDOWN_MS) return false;
  if (record.opens < 2 && !record.engaged) return false;
  return !(input.holdingToTalk || input.pageOpen || input.pendingOrder);
}

type KeyValueStore = Pick<Storage, "getItem" | "setItem">;

function storage(): KeyValueStore | null {
  try { return typeof window === "undefined" ? null : window.localStorage; } catch { return null; }
}

export function readInstallRecord(store: KeyValueStore | null = storage()): InstallRecord | null {
  if (!store) return null;
  try {
    const raw = store.getItem(INSTALL_RECORD_KEY);
    if (raw === null) return { opens: 0, dismissals: 0 };
    const value = JSON.parse(raw) as Partial<InstallRecord>;
    return { opens: Number(value.opens) || 0, dismissals: Number(value.dismissals) || 0,
      lastDismissedAt: typeof value.lastDismissedAt === "number" ? value.lastDismissedAt : undefined, engaged: value.engaged === true || undefined };
  } catch { return null; }
}

export function updateInstallRecord(change: (record: InstallRecord) => InstallRecord, store: KeyValueStore | null = storage()): InstallRecord | null {
  const current = readInstallRecord(store);
  if (!current || !store) return null;
  const next = change(current);
  try { store.setItem(INSTALL_RECORD_KEY, JSON.stringify(next)); return next; } catch { return null; }
}

// ── The Android install prompt and the installed flag ──────────────────────────────────────────

interface InstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

let deferredPrompt: InstallPromptEvent | null = null;
let installedNow = false;
let listening = false;
const listeners = new Set<() => void>();
const notify = () => listeners.forEach(listener => listener());

/** Called once from main.tsx: the browser's install offer can arrive before the app renders. */
export function listenForInstallPrompt(target: Window | undefined = typeof window === "undefined" ? undefined : window) {
  if (!target || listening) return;
  listening = true;
  target.addEventListener("beforeinstallprompt", event => {
    event.preventDefault();
    deferredPrompt = event as InstallPromptEvent;
    notify();
  });
  target.addEventListener("appinstalled", () => { installedNow = true; deferredPrompt = null; notify(); });
}

export function subscribeInstallPrompt(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export const hasInstallPrompt = () => deferredPrompt !== null;
export const installedThisVisit = () => installedNow;

/** Show the browser's own install dialog; the event is single-use either way. */
export async function promptInstall(): Promise<"accepted" | "dismissed" | "unavailable"> {
  const event = deferredPrompt;
  if (!event) return "unavailable";
  deferredPrompt = null;
  notify();
  try {
    await event.prompt();
    const { outcome } = await event.userChoice;
    if (outcome === "accepted") { installedNow = true; notify(); }
    return outcome;
  } catch { return "dismissed"; }
}

export function standaloneDisplay(): boolean {
  if (typeof window === "undefined") return false;
  const standalone = (navigator as Navigator & { standalone?: boolean }).standalone === true;
  try { return standalone || window.matchMedia?.("(display-mode: standalone), (display-mode: fullscreen)").matches === true; } catch { return standalone; }
}

/** The environment of this page now (the browser's facts plus the prompt store). */
export function currentInstallDetection(): InstallDetection {
  if (typeof window === "undefined") return { environment: "desktop" };
  return detectInstallEnvironment({ userAgent: navigator.userAgent, standalone: standaloneDisplay() || installedNow,
    maxTouchPoints: navigator.maxTouchPoints ?? 0, platform: navigator.platform, hasInstallPrompt: hasInstallPrompt() });
}

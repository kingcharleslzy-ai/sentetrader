// @vitest-environment node
// Protects the 安装到桌面 guide: each real browser lands on the guide it can actually follow, and the
// guide opens by itself only when the owner-approved timing allows (never on desktop, installed or guest).
import { describe, expect, test } from "vitest";
import {
  INSTALL_COOLDOWN_MS, detectInstallEnvironment, installGuideContent, readInstallRecord, shouldAutoOpenInstall, updateInstallRecord,
  type AutoOpenInput, type InstallDetection,
} from "./installEnvironment";

const UA = {
  iosSafari26: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1",
  iosSafari18: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
  ipadOs: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15",
  criOs: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/138.0.7204.156 Mobile/15E148 Safari/604.1",
  edgiOs: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 EdgiOS/138.3351.83 Mobile/15E148 Safari/605.1.15",
  iosWeChat: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.61(0x18003d2c) NetType/WIFI Language/zh_CN",
  iosQuark: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Quark/7.12.1.2337 Mobile",
  androidChrome: "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36",
  samsung: "Mozilla/5.0 (Linux; Android 14; SAMSUNG SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/27.0 Chrome/125.0.0.0 Mobile Safari/537.36",
  huawei: "Mozilla/5.0 (Linux; Android 12; HarmonyOS; NOH-AN00; HMSCore 6.13.0.302) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.88 HuaweiBrowser/15.0.4.312 Mobile Safari/537.36",
  xiaomi: "Mozilla/5.0 (Linux; U; Android 14; zh-cn; 23127PN0CC Build/UKQ1.230804.001) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/122.0.6261.119 Mobile Safari/537.36 XiaoMi/MiuiBrowser/18.6.70710",
  oppo: "Mozilla/5.0 (Linux; U; Android 14; zh-cn; PJZ110 Build/UKQ1.230924.001) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/115.0.5790.168 Mobile Safari/537.36 HeyTapBrowser/40.8.39.1",
  vivo: "Mozilla/5.0 (Linux; Android 14; V2309A Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36 VivoBrowser/20.6.11.0",
  androidWeChat: "Mozilla/5.0 (Linux; Android 14; 23127PN0CC Build/UKQ1.230804.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/130.0.6723.103 Mobile Safari/537.36 XWEB/1300333 MMWEBSDK/20240802 MicroMessenger/8.0.51.2720(0x28003336) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64",
  androidQqApp: "Mozilla/5.0 (Linux; Android 13; PGT-AN10 Build/HONORPGT-AN10; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/109.0.5414.86 MQQBrowser/6.2 TBS/047601 Mobile Safari/537.36 V1_AND_SQ_9.0.65_6290_YYB_D QQ/9.0.65.17345 NetType/WIFI WebP/0.3.0",
  qqBrowser: "Mozilla/5.0 (Linux; U; Android 13; zh-cn; PGT-AN10 Build/HONORPGT-AN10) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/109.0.5414.86 MQQBrowser/14.8 Mobile Safari/537.36 COVC/046401",
  uc: "Mozilla/5.0 (Linux; U; Android 12; zh-CN; V2148A Build/SP1A.210812.003) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/100.0.4896.58 UCBrowser/16.6.0.1310 Mobile Safari/537.36",
  androidQuark: "Mozilla/5.0 (Linux; U; Android 14; zh-CN; 2211133C Build/UKQ1.230804.001) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/100.0.4896.58 Quark/7.4.5.680 Mobile Safari/537.36",
  desktopChrome: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
};

const detect = (userAgent: string, extra: { standalone?: boolean; maxTouchPoints?: number; hasInstallPrompt?: boolean } = {}) =>
  detectInstallEnvironment({ userAgent, standalone: false, maxTouchPoints: 5, hasInstallPrompt: false, ...extra });

describe("detectInstallEnvironment", () => {
  test.each<[string, string, Parameters<typeof detect>[1], Partial<InstallDetection>]>([
    ["iOS 26 Safari", UA.iosSafari26, {}, { environment: "ios-safari-26" }],
    ["iOS 18 Safari", UA.iosSafari18, {}, { environment: "ios-safari-legacy" }],
    ["iPadOS (desktop-class Safari UA, touch)", UA.ipadOs, {}, { environment: "ios-safari-legacy", ipad: true }],
    ["iOS Chrome", UA.criOs, {}, { environment: "ios-chrome-edge" }],
    ["iOS Edge", UA.edgiOs, {}, { environment: "ios-chrome-edge" }],
    ["iOS 微信", UA.iosWeChat, {}, { environment: "in-app-ios", app: "wechat" }],
    ["iOS 夸克", UA.iosQuark, {}, { environment: "ios-other", browser: "夸克" }],
    ["Android Chrome with the install offer", UA.androidChrome, { hasInstallPrompt: true }, { environment: "android-prompt" }],
    ["Android Chrome before the offer", UA.androidChrome, {}, { environment: "android-vendor", vendor: "chrome" }],
    ["三星浏览器 without the offer", UA.samsung, {}, { environment: "android-vendor", vendor: "samsung" }],
    ["三星浏览器 with the offer", UA.samsung, { hasInstallPrompt: true }, { environment: "android-prompt" }],
    ["华为浏览器", UA.huawei, {}, { environment: "android-vendor", vendor: "huawei" }],
    ["小米浏览器", UA.xiaomi, {}, { environment: "android-vendor", vendor: "xiaomi" }],
    ["OPPO 浏览器", UA.oppo, {}, { environment: "android-vendor", vendor: "oppo" }],
    ["vivo 浏览器", UA.vivo, {}, { environment: "android-vendor", vendor: "vivo" }],
    ["Android 微信", UA.androidWeChat, {}, { environment: "in-app-android", app: "wechat" }],
    ["Android QQ (the app's built-in page)", UA.androidQqApp, {}, { environment: "in-app-android", app: "qq" }],
    ["Android QQ 浏览器 (not the QQ app)", UA.qqBrowser, {}, { environment: "android-vendor", vendor: "qq-browser" }],
    ["UC 浏览器", UA.uc, {}, { environment: "android-vendor", vendor: "uc" }],
    ["Android 夸克", UA.androidQuark, {}, { environment: "android-vendor", vendor: "quark" }],
    ["desktop Chrome", UA.desktopChrome, { maxTouchPoints: 0 }, { environment: "desktop" }],
    ["desktop Safari (no touch)", UA.ipadOs, { maxTouchPoints: 0 }, { environment: "desktop" }],
    ["opened from the home screen", UA.iosSafari26, { standalone: true }, { environment: "installed" }],
  ])("%s", (_name, userAgent, extra, expected) => {
    expect(detect(userAgent, extra)).toMatchObject(expected);
  });

  test("every environment has a guide to show, with the app or brand named where it matters", () => {
    for (const userAgent of Object.values(UA)) {
      for (const extra of [{}, { hasInstallPrompt: true }, { standalone: true }]) {
        const content = installGuideContent(detect(userAgent, extra));
        expect(content.title).not.toBe("");
      }
    }
    expect(installGuideContent(detect(UA.iosWeChat)).subtitle).toContain("微信");
    expect(installGuideContent(detect(UA.iosWeChat)).title).toContain("在默认浏览器中打开");
    expect(installGuideContent(detect(UA.androidWeChat)).title).toContain("在浏览器打开");
    expect(installGuideContent(detect(UA.huawei)).subtitle).toBe("华为浏览器");
    expect(installGuideContent(detect(UA.androidChrome, { hasInstallPrompt: true })).layout).toBe("prompt");
    expect(installGuideContent(detect(UA.iosQuark)).copy).toBe("复制链接");
  });
});

describe("shouldAutoOpenInstall", () => {
  const now = 1_800_000_000_000;
  const base: AutoOpenInput = {
    record: { opens: 2, dismissals: 0 }, now, environment: "ios-safari-26", mobileLayout: true, tauri: false, signedIn: true,
    shownThisVisit: false, holdingToTalk: false, pageOpen: false, pendingOrder: false,
  };
  const open = (change: Partial<AutoOpenInput>) => shouldAutoOpenInstall({ ...base, ...change });

  test("opens from this browser's second open, or after the first order / AI reply here", () => {
    expect(open({})).toBe(true);
    expect(open({ record: { opens: 1, dismissals: 0 } })).toBe(false);
    expect(open({ record: { opens: 1, dismissals: 0, engaged: true } })).toBe(true);
  });

  test("以后再说 waits seven days; after three closes it never opens by itself", () => {
    expect(open({ record: { opens: 5, dismissals: 1, lastDismissedAt: now - INSTALL_COOLDOWN_MS + 60_000 } })).toBe(false);
    expect(open({ record: { opens: 5, dismissals: 1, lastDismissedAt: now - INSTALL_COOLDOWN_MS - 1 } })).toBe(true);
    expect(open({ record: { opens: 9, dismissals: 3, lastDismissedAt: now - 30 * INSTALL_COOLDOWN_MS } })).toBe(false);
  });

  test("waits while holding to talk, a full-screen page or an unconfirmed order card is open", () => {
    expect(open({ holdingToTalk: true })).toBe(false);
    expect(open({ pageOpen: true })).toBe(false);
    expect(open({ pendingOrder: true })).toBe(false);
  });

  test("never on the desktop, from the home screen, for a visitor, in the desktop app, twice a visit or without storage", () => {
    expect(open({ environment: "desktop" })).toBe(false);
    expect(open({ environment: "installed" })).toBe(false);
    expect(open({ signedIn: false })).toBe(false);
    expect(open({ tauri: true })).toBe(false);
    expect(open({ mobileLayout: false })).toBe(false);
    expect(open({ shownThisVisit: true })).toBe(false);
    expect(open({ record: null })).toBe(false);
  });

  test("the record survives a round trip and an unreadable store reads as null", () => {
    const values = new Map<string, string>();
    const store = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
    expect(readInstallRecord(store)).toEqual({ opens: 0, dismissals: 0 });
    updateInstallRecord(record => ({ ...record, opens: record.opens + 1, dismissals: 1, lastDismissedAt: 5 }), store);
    expect(readInstallRecord(store)).toEqual({ opens: 1, dismissals: 1, lastDismissedAt: 5, engaged: undefined });
    const broken = { getItem: () => { throw new Error("blocked"); }, setItem: () => undefined };
    expect(readInstallRecord(broken)).toBeNull();
    expect(readInstallRecord(null)).toBeNull();
  });
});

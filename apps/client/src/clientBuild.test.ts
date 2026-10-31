// @vitest-environment jsdom
// Stays on jsdom: its assertions fail under the default happy-dom (accessible names / text / location differ).
import { afterEach, expect, test, vi } from "vitest";
import {
  CLIENT_BUILD, checkWebUpdateNow, clientHeaderValue, dismissWebUpdateBanner, formatClientHeader, noteRuntimeCompat, noteWebBuild,
  readClientCompat, readWebUpdate, registerWebUpdateBusy, resetClientCompat, shortCommit, watchWebBuild, webPage,
} from "./clientBuild";

afterEach(() => {
  resetClientCompat();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
  sessionStorage.clear();
});

test("client header carries platform, version and build sha; dev builds carry no sha", () => {
  expect(formatClientHeader({ platform: "web", version: "1.6.5", commit: "f8f50e5c0b9d" })).toBe("web/1.6.5+f8f50e5c0b9d");
  expect(formatClientHeader({ platform: "desktop", version: "1.6.6", commit: "deadbeef" })).toBe("desktop/1.6.6+deadbeef");
  expect(formatClientHeader({ platform: "web", version: "1.6.5", commit: "dev" })).toBe("web/1.6.5");
  // vitest builds without AI_TRADER_BUILD_SHA: this build is "dev".
  expect(CLIENT_BUILD.platform).toBe("web");
  expect(clientHeaderValue()).toBe(`web/${CLIENT_BUILD.version}`);
  expect(shortCommit()).toBe("dev");
  expect(shortCommit("f8f50e5c0b9d")).toBe("f8f50e5");
});

test("noteRuntimeCompat records the runtime answer and ignores absent or unknown verdicts", () => {
  noteRuntimeCompat(new Headers({
    "x-ai-trader-client-compat": "outdated",
    "x-ai-trader-min-client": "1.6.6",
    "x-ai-trader-runtime": "1.6.6+deadbee",
  }));
  expect(readClientCompat()).toEqual({ state: "outdated", minVersion: "1.6.6", runtime: "1.6.6+deadbee" });

  noteRuntimeCompat(new Headers({
    "x-ai-trader-client-compat": "ok",
    "x-ai-trader-runtime": "1.6.6+deadbee",
  }));
  expect(readClientCompat()).toEqual({ state: "ok", minVersion: null, runtime: "1.6.6+deadbee" });

  noteRuntimeCompat(new Headers());
  expect(readClientCompat().state).toBe("ok");
  noteRuntimeCompat(new Headers({ "x-ai-trader-client-compat": "nonsense" }));
  expect(readClientCompat().state).toBe("ok");
  // "refresh" is no longer a Runtime verdict — the web build comparison is local, so a stale
  // answer carrying it is ignored rather than adopted.
  noteRuntimeCompat(new Headers({ "x-ai-trader-client-compat": "refresh" }));
  expect(readClientCompat().state).toBe("ok");

  resetClientCompat();
  expect(readClientCompat()).toEqual({ state: "unknown", minVersion: null, runtime: null });
});

test("noteWebBuild flags refresh only when the deployed commit differs from this build", () => {
  noteRuntimeCompat(new Headers({ "x-ai-trader-client-compat": "ok", "x-ai-trader-runtime": "1.6.6+deadbee" }));
  // A dev build never compares itself against version.json: the Runtime verdict stands.
  noteWebBuild("1685e2b0c0ffee");
  expect(readClientCompat().state).toBe("ok");

  const previousCommit = CLIENT_BUILD.commit;
  CLIENT_BUILD.commit = "f8f50e5c0b9d7a2e4d6c8a1b3e5f7d9c0a2b4e6f";
  try {
    noteWebBuild("1685e2b0c0ffee");
    expect(readClientCompat().state).toBe("refresh");
    // A shorter sha of the same build is a prefix; same build either way → no refresh.
    noteWebBuild("f8f50e5");
    expect(readClientCompat().state).toBe("ok");
    noteWebBuild(CLIENT_BUILD.commit);
    expect(readClientCompat().state).toBe("ok");
    // The local refresh flag outranks the Runtime verdict it would otherwise fall back to.
    noteRuntimeCompat(new Headers({ "x-ai-trader-client-compat": "outdated", "x-ai-trader-min-client": "1.6.6" }));
    noteWebBuild("1685e2b0c0ffee");
    expect(readClientCompat().state).toBe("refresh");
  } finally {
    CLIENT_BUILD.commit = previousCommit;
  }
});

test("watchWebBuild stays quiet for dev builds", () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const cancel = watchWebBuild(); // vitest build: web platform, commit "dev"
  document.dispatchEvent(new Event("visibilitychange"));
  cancel();
  expect(fetchMock).not.toHaveBeenCalled();
});

test("watchWebBuild fetches version.json at mount and at most once per five minutes on visibility", async () => {
  const previousCommit = CLIENT_BUILD.commit;
  CLIENT_BUILD.commit = "f8f50e5c0b9d7a2e4d6c8a1b3e5f7d9c0a2b4e6f";
  const fetchMock = vi.fn(async (_url: unknown) => ({
    ok: true, json: async () => ({ version: "1.6.5", commit: "1685e2b0c0ffee0000000000deadbeef1234" }),
  }));
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(webPage, "reload").mockImplementation(() => undefined);
  vi.useFakeTimers({ now: 60_000_000 });
  try {
    const cancel = watchWebBuild();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(new URL("version.json", document.baseURI).toString());
    // The deployed manifest names another commit than this page was built from.
    expect(readClientCompat().state).toBe("refresh");

    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1); // throttled: under five minutes since mount

    await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    cancel();
  } finally {
    CLIENT_BUILD.commit = previousCommit;
  }
});

test("watchWebBuild swallows version.json failures and stops watching once canceled", async () => {
  const previousCommit = CLIENT_BUILD.commit;
  CLIENT_BUILD.commit = "f8f50e5c0b9d7a2e4d6c8a1b3e5f7d9c0a2b4e6f";
  const fetchMock = vi.fn(async (_url: unknown) => ({ ok: false, json: async () => ({ commit: "nope" }) }));
  vi.stubGlobal("fetch", fetchMock);
  vi.useFakeTimers({ now: 60_000_000 });
  try {
    const cancel = watchWebBuild();
    await vi.advanceTimersByTimeAsync(0);
    expect(readClientCompat().state).toBe("unknown"); // nothing recorded from a failed manifest
    cancel();
    await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1); // canceled: the visibility path no longer fetches
  } finally {
    CLIENT_BUILD.commit = previousCommit;
  }
});

const THIS_BUILD = "f8f50e5c0b9d7a2e4d6c8a1b3e5f7d9c0a2b4e6f";
const DEPLOYED = "1685e2b0c0ffee0000000000deadbeef1234";
/** A pipeline web build whose version.json answers `deployed`; reload is a spy. */
function webBuild(deployed: string | (() => string | null) = DEPLOYED) {
  const previous = { ...CLIENT_BUILD };
  CLIENT_BUILD.commit = THIS_BUILD;
  const fetchMock = vi.fn(async (_url: unknown) => {
    const commit = typeof deployed === "string" ? deployed : deployed();
    return commit === null ? { ok: false, json: async () => ({}) } : { ok: true, json: async () => ({ version: "1.7.0", commit }) };
  });
  vi.stubGlobal("fetch", fetchMock);
  const reload = vi.spyOn(webPage, "reload").mockImplementation(() => undefined);
  vi.useFakeTimers({ now: 60_000_000 });
  return { fetchMock, reload, restore: () => Object.assign(CLIENT_BUILD, previous) };
}
const foreground = () => document.dispatchEvent(new Event("visibilitychange"));
function pageShow(persisted: boolean) {
  const event = new Event("pageshow") as PageTransitionEvent;
  Object.defineProperty(event, "persisted", { value: persisted });
  window.dispatchEvent(event);
}

test("新版本 + 空闲：发现即刷新一次；刷新后仍是旧版（CDN 未同步）30 分钟内不再自动刷新，之后再试", async () => {
  const build = webBuild();
  try {
    let cancel = watchWebBuild();
    await vi.advanceTimersByTimeAsync(0);
    expect(build.reload).toHaveBeenCalledTimes(1);

    // The reloaded page is still this build: a fresh page, the same session.
    cancel(); resetClientCompat();
    cancel = watchWebBuild();
    await vi.advanceTimersByTimeAsync(0);
    expect(readWebUpdate()).toEqual({ available: true, banner: true });
    foreground();
    await vi.advanceTimersByTimeAsync(0);
    expect(build.reload).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(30 * 60 * 1000);
    foreground();
    await vi.advanceTimersByTimeAsync(0);
    expect(build.reload).toHaveBeenCalledTimes(2);
    cancel();
  } finally { build.restore(); }
});

test("新版本 + 忙：不刷新而显示提示条；关闭提示条后，下次回到前台且空闲时仍自动刷新", async () => {
  const build = webBuild();
  let busy = true;
  registerWebUpdateBusy(() => busy);
  try {
    const cancel = watchWebBuild();
    await vi.advanceTimersByTimeAsync(0);
    foreground();
    expect(build.reload).not.toHaveBeenCalled();
    expect(readWebUpdate()).toEqual({ available: true, banner: true });

    dismissWebUpdateBanner();
    expect(readWebUpdate()).toEqual({ available: true, banner: false });
    noteWebBuild(DEPLOYED); // the same deployed build stays dismissed
    expect(readWebUpdate().banner).toBe(false);
    noteWebBuild("2222222deadbeef"); // a later deploy shows the bar again
    expect(readWebUpdate().banner).toBe(true);

    busy = false;
    foreground(); // within five minutes: no fetch, the known newer build reloads now
    expect(build.fetchMock).toHaveBeenCalledTimes(1);
    expect(build.reload).toHaveBeenCalledTimes(1);
    cancel();
  } finally { build.restore(); }
});

test("后台发现新版本不刷新，回到前台才刷新；iOS 从缓存恢复（pageshow persisted）立即重新检查", async () => {
  let deployed = THIS_BUILD;
  const build = webBuild(() => deployed);
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  try {
    const cancel = watchWebBuild();
    await vi.advanceTimersByTimeAsync(0);
    expect(readWebUpdate().available).toBe(false);

    deployed = DEPLOYED;
    pageShow(false); // a normal load: nothing extra
    await vi.advanceTimersByTimeAsync(0);
    expect(build.fetchMock).toHaveBeenCalledTimes(1);
    pageShow(true); // restored from the back-forward cache: fetch despite the five-minute spacing
    await vi.advanceTimersByTimeAsync(0);
    expect(build.fetchMock).toHaveBeenCalledTimes(2);
    expect(readWebUpdate().available).toBe(true);
    expect(build.reload).not.toHaveBeenCalled(); // still hidden

    visibility.mockReturnValue("visible");
    foreground();
    expect(build.reload).toHaveBeenCalledTimes(1);
    cancel();
  } finally { build.restore(); }
});

test("sessionStorage 不可用时不自动刷新（防循环记不住），提示条照常", async () => {
  const build = webBuild();
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  try {
    const cancel = watchWebBuild();
    await vi.advanceTimersByTimeAsync(0);
    expect(build.reload).not.toHaveBeenCalled();
    expect(readWebUpdate().banner).toBe(true);
    cancel();
  } finally { build.restore(); }
});

test("检查更新：有新版立即刷新（忙或防循环期内也刷新），无新版答已是最新，读不到答失败", async () => {
  let deployed: string | null = THIS_BUILD;
  const build = webBuild(() => deployed);
  registerWebUpdateBusy(() => true);
  try {
    await expect(checkWebUpdateNow()).resolves.toBe("current");
    deployed = null;
    await expect(checkWebUpdateNow()).resolves.toBe("failed");
    expect(build.reload).not.toHaveBeenCalled();
    deployed = DEPLOYED;
    sessionStorage.setItem("ai-trader-web-auto-reload", JSON.stringify({ at: Date.now(), from: THIS_BUILD }));
    await expect(checkWebUpdateNow()).resolves.toBe("reloading");
    expect(build.reload).toHaveBeenCalledTimes(1);
  } finally { build.restore(); }
});

test("桌面客户端（Tauri）不检查、不刷新", async () => {
  const build = webBuild();
  CLIENT_BUILD.platform = "desktop";
  try {
    const cancel = watchWebBuild();
    foreground();
    pageShow(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(build.fetchMock).not.toHaveBeenCalled();
    expect(build.reload).not.toHaveBeenCalled();
    cancel();
  } finally { build.restore(); }
});

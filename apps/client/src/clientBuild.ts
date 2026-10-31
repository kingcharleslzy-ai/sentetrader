import { useSyncExternalStore } from "react";

/** Who this build is — reported on every Runtime request (`X-AI-Trader-Client`) and in window pulses. */
export interface ClientBuild {
  platform: "desktop" | "web";
  version: string;
  commit: string;
}

export const CLIENT_BUILD: ClientBuild = {
  platform: "__TAURI_INTERNALS__" in window ? "desktop" : "web",
  version: __AI_TRADER_CLIENT_VERSION__,
  commit: __AI_TRADER_BUILD_SHA__,
};

/** `<platform>/<version>[+<sha>]`; local dev builds (`dev`) carry no sha. */
export function formatClientHeader(build: ClientBuild): string {
  return build.commit === "dev" || !build.commit
    ? `${build.platform}/${build.version}`
    : `${build.platform}/${build.version}+${build.commit}`;
}

export function clientHeaderValue(): string {
  return formatClientHeader(CLIENT_BUILD);
}

/** Short build label for display: `dev` locally, the 7-char sha from a pipeline build. */
export function shortCommit(commit: string = CLIENT_BUILD.commit): string {
  return commit === "dev" ? "dev" : commit.slice(0, 7);
}

export type ClientCompatState = "ok" | "outdated" | "refresh" | "unknown";
export interface ClientCompat {
  state: ClientCompatState;
  /** The version the Runtime requires when `state` is "outdated". */
  minVersion: string | null;
  /** What answered: `<runtime version>+<short sha>`; null when the Runtime does not report yet. */
  runtime: string | null;
}

/** What the Runtime answers — it only judges whether this client is too old. */
type RuntimeVerdict = Extract<ClientCompatState, "ok" | "outdated" | "unknown">;
const RUNTIME_VERDICTS: readonly RuntimeVerdict[] = ["ok", "outdated", "unknown"];
/** Whether the deployed web build and this page's build are different commits (sha prefix match). */
function buildsDiffer(current: string, latest: string): boolean {
  return !current.startsWith(latest) && !latest.startsWith(current);
}

/** The two inputs are recorded separately; `state` is derived so either can change it. */
interface CompatSource {
  verdict: RuntimeVerdict;
  minVersion: string | null;
  runtime: string | null;
  webLatestCommit: string | null;
}

function deriveCompat(source: CompatSource): ClientCompat {
  const webBuildLags = source.webLatestCommit !== null && CLIENT_BUILD.commit !== "dev"
    && buildsDiffer(CLIENT_BUILD.commit, source.webLatestCommit);
  return { state: webBuildLags ? "refresh" : source.verdict, minVersion: source.minVersion, runtime: source.runtime };
}

let source: CompatSource = { verdict: "unknown", minVersion: null, runtime: null, webLatestCommit: null };
let compat: ClientCompat = deriveCompat(source);
const listeners = new Set<() => void>();

/** Web only: a newer build is deployed (`available`) and the 新版本已就绪 bar shows (`banner`). */
export interface WebUpdate {
  available: boolean;
  banner: boolean;
}
/** The deployed commit whose bar the user closed; a later deploy shows the bar again. */
let bannerDismissedFor: string | null = null;
function deriveWebUpdate(snapshot: ClientCompat, latest: string | null, dismissed: string | null): WebUpdate {
  const available = snapshot.state === "refresh";
  return { available, banner: available && latest !== dismissed };
}
let webUpdate: WebUpdate = deriveWebUpdate(compat, null, null);

function publish(next: CompatSource, dismissed = bannerDismissedFor): void {
  const snapshot = deriveCompat(next);
  const update = deriveWebUpdate(snapshot, next.webLatestCommit, dismissed);
  const changed = snapshot.state !== compat.state || snapshot.minVersion !== compat.minVersion
    || snapshot.runtime !== compat.runtime || update.available !== webUpdate.available || update.banner !== webUpdate.banner;
  source = next;
  compat = snapshot;
  bannerDismissedFor = dismissed;
  webUpdate = update;
  if (changed) listeners.forEach(listener => listener());
}

/** Record the compat answer from any Runtime response headers (absent/unknown answers are ignored). */
export function noteRuntimeCompat(headers: Headers): void {
  const verdict = headers.get("x-ai-trader-client-compat") as RuntimeVerdict | null;
  if (!verdict || !RUNTIME_VERDICTS.includes(verdict)) return;
  publish({ ...source, verdict, minVersion: headers.get("x-ai-trader-min-client"), runtime: headers.get("x-ai-trader-runtime") });
}

/** Record the deployed web build from version.json (tests and the watcher below). */
export function noteWebBuild(commit: string): void {
  publish({ ...source, webLatestCommit: commit });
}

/** Sync read for non-hook contexts (the pulse's connection context). */
export function readClientCompat(): ClientCompat {
  return compat;
}

export function useClientCompat(): ClientCompat {
  return useSyncExternalStore(
    listener => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    () => compat, () => compat,
  );
}

export function readWebUpdate(): WebUpdate {
  return webUpdate;
}

export function useWebUpdate(): WebUpdate {
  return useSyncExternalStore(
    listener => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    () => webUpdate, () => webUpdate,
  );
}

/** 关闭 on the bar: hidden for this deployed build; the next foreground while idle still reloads. */
export function dismissWebUpdateBanner(): void {
  publish(source, source.webLatestCommit);
}

/** The page reload, behind an object so tests can replace it (jsdom's `location.reload` is fixed). */
export const webPage = { reload: (): void => { window.location.reload(); } };

/**
 * What keeps the page from reloading itself: each check answers "busy" (holding to talk, in a voice
 * call, an unsent chat line, an order page or a confirmation open …). The workspace registers one;
 * with none registered (login screen) the page counts as idle.
 */
const busyChecks = new Set<() => boolean>();
export function registerWebUpdateBusy(check: () => boolean): () => void {
  busyChecks.add(check);
  return () => { busyChecks.delete(check); };
}

/**
 * Loop guard: an automatic reload records which build it left. If the reloaded page is still that
 * build (the CDN has not caught up), it does not reload itself again for 30 minutes. Without a
 * working sessionStorage the guard cannot hold, so the page never reloads itself (the bar stays).
 */
const AUTO_RELOAD_KEY = "ai-trader-web-auto-reload";
const AUTO_RELOAD_PAUSE_MS = 30 * 60 * 1000;
function autoReloadAllowed(): boolean {
  try {
    const raw = sessionStorage.getItem(AUTO_RELOAD_KEY);
    const last = raw ? JSON.parse(raw) as { at?: unknown; from?: unknown } : null;
    if (last && last.from === CLIENT_BUILD.commit && typeof last.at === "number" && Date.now() - last.at < AUTO_RELOAD_PAUSE_MS) return false;
    sessionStorage.setItem(AUTO_RELOAD_KEY, JSON.stringify({ at: Date.now(), from: CLIENT_BUILD.commit }));
    return true;
  } catch {
    return false;
  }
}

/** Reload onto the deployed build when one is newer, the page shows and nothing is in progress. */
function reloadIfIdle(): void {
  if (!webUpdate.available || document.visibilityState !== "visible") return;
  if ([...busyChecks].some(busy => busy()) || !autoReloadAllowed()) return;
  webPage.reload();
}

/** Read the deployed `version.json` beside the page and record its commit; null when unreadable. */
async function fetchDeployedBuild(): Promise<string | null> {
  try {
    const response = await fetch(new URL("version.json", document.baseURI), { cache: "no-store" });
    if (!response.ok) return null;
    const manifest = await response.json() as { commit?: unknown };
    if (typeof manifest.commit !== "string" || !manifest.commit) return null;
    noteWebBuild(manifest.commit);
    return manifest.commit;
  } catch {
    // Offline or an old deployment without version.json: keep the current state.
    return null;
  }
}

export type WebUpdateCheck = "reloading" | "current" | "failed";
/** 检查更新 (我的 / 设置 → 关于): read version.json now; a newer build reloads at once (the user asked). */
export async function checkWebUpdateNow(): Promise<WebUpdateCheck> {
  if (await fetchDeployedBuild() === null) return "failed";
  if (!webUpdate.available) return "current";
  webPage.reload();
  return "reloading";
}

const WEB_BUILD_POLL_MS = 5 * 60 * 1000;

/**
 * Web only: watch the deployed `version.json` beside the page and flag "refresh" when it names
 * another commit. The Runtime cannot make that call — it and the site deploy separately. Checked at
 * mount and whenever the page comes back to the foreground (at most one fetch per five minutes; a
 * page iOS restores from its back-forward cache always fetches). At each of those moments a known
 * newer build reloads the page if it is idle; otherwise the bar offers 刷新. Desktop builds carry
 * their own assets and local dev builds have no sha to compare: both are a no-op.
 * Returns the cancel function.
 */
export function watchWebBuild(): () => void {
  if (CLIENT_BUILD.platform !== "web" || CLIENT_BUILD.commit === "dev") return () => undefined;
  let lastPollAt = 0;
  let canceled = false;
  const check = async (force: boolean): Promise<void> => {
    if (canceled) return;
    const now = Date.now();
    if (force || now - lastPollAt >= WEB_BUILD_POLL_MS) {
      lastPollAt = now;
      await fetchDeployedBuild();
    }
    if (!canceled) reloadIfIdle();
  };
  void check(true);
  const onVisibilityChange = () => {
    if (document.visibilityState === "visible") void check(false);
  };
  const onPageShow = (event: PageTransitionEvent) => {
    if (event.persisted) void check(true);
  };
  document.addEventListener("visibilitychange", onVisibilityChange);
  window.addEventListener("pageshow", onPageShow);
  return () => {
    canceled = true;
    document.removeEventListener("visibilitychange", onVisibilityChange);
    window.removeEventListener("pageshow", onPageShow);
  };
}

/** Restore the pre-answer state between tests. */
export function resetClientCompat(): void {
  source = { verdict: "unknown", minVersion: null, runtime: null, webLatestCommit: null };
  compat = deriveCompat(source);
  bannerDismissedFor = null;
  webUpdate = deriveWebUpdate(compat, null, null);
  busyChecks.clear();
  listeners.forEach(listener => listener());
}

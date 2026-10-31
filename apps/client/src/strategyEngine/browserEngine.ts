import { useSyncExternalStore } from "react";
import workerSrc from "./engineWorker.ts?worker&url";
import { requestJson, runtimeFetch } from "../api";
import { LONG_READ_TIMEOUT_MS } from "../requestTimeout";

/**
 * 回测本机优先 (2026-10-12, owner: 能在本地的尽量在本地): the interface's rule backtests (策略编辑卡's
 * automatic one, the 结果页's 回测) and 比一比 compute in this browser — Pyodide workers running the
 * Runtime's own engine sources (`GET /api/strategy-engine/bundle`) over inputs the Runtime serves
 * (`POST /api/strategy-backtests/inputs`, the lab's `…/inputs`) — and the results go back to the Runtime
 * to be wrapped and stored like its own (`source: browser`). Whenever the browser cannot (no WebAssembly
 * or module workers, a phone, the engine still loading, a failed or slow load, a crash, out of memory, a
 * hidden page) the same request goes to the Runtime's queue instead with the reason (`fallbackReason`);
 * the user sees the same card either way. A result whose engine the Runtime no longer runs (409
 * engine_changed: a deploy between loading and posting) is never mixed in: the page asks for a reload.
 * AI tools, the CLI and external agents call the Runtime's backtest routes directly and never come here.
 */
export const PYODIDE_VERSION = "314.0.7";
/** Loading Pyodide and the engine (the first time ≈ 8 MB, then from the HTTP cache). */
export const INIT_TIMEOUT_MS = 90_000;
/** One rule backtest; the Runtime's own run takes 1–3 s for 10 000 one-minute bars. */
export const RULE_TIMEOUT_MS = 60_000;
/** One 比一比 variant. */
export const VARIANT_TIMEOUT_MS = 120_000;
export const STALE_ENGINE_MESSAGE = "回测引擎已更新，请刷新页面后重跑";

export type FallbackReason = "unsupported" | "device" | "warming" | "init_failed" | "init_timeout" | "memory"
  | "crashed" | "engine_error" | "hidden" | "slow" | "inputs_failed" | "inputs_expired" | "rerun";

/** The browser cannot compute this one: the caller runs it on the Runtime with `reason`. */
export class EngineFallback extends Error {
  constructor(readonly reason: FallbackReason, detail?: string) { super(detail ? `${reason}: ${detail}` : reason); }
}
/** The Runtime's engine changed after this page loaded it: reload, never mix. */
export class StaleEngine extends Error {
  constructor() { super(STALE_ENGINE_MESSAGE); }
}

interface EngineBundle { digest: string; modules: Record<string, string>; entry: string; pyodide: { version: string } }
type Reply = { id: number; ok: true; text?: string; ms: number } | { id: number; ok: false; error: string };

const isTauri = () => typeof window !== "undefined" && Boolean(window.__TAURI_INTERNALS__);

/** Pyodide's files: the build override, else the site's self-hosted copy — same origin on the web (and the
 *  dev server's proxy), the site itself from the desktop app (its nginx answers the tauri origins' CORS). */
export function pyodideUrl(): string {
  const override = import.meta.env.VITE_PYODIDE_URL;
  if (override) return override.endsWith("/") ? override : `${override}/`;
  const path = `/trader/pyodide/v${PYODIDE_VERSION}/`;
  return isTauri() ? `https://qilinshuzhi.com${path}` : `${location.origin}${path}`;
}

/** The worker script. The web build's scripts load from OSS, and a browser refuses a cross-origin worker: the
 *  site serves the same hashed file under its own /trader/assets/, so ask for it by name on this origin. */
export function workerUrl(): URL {
  if (import.meta.env.VITE_ASSET_ORIGIN) return new URL(`${import.meta.env.BASE_URL}assets/${workerSrc.split("/").pop()}`, location.origin);
  return new URL(workerSrc, location.href);
}

/** Why this browser cannot compute at all (null = it can): WebAssembly, module workers, 4+ cores, not a phone. */
export function browserSupport(): FallbackReason | null {
  if (typeof Worker === "undefined" || typeof WebAssembly === "undefined" || typeof document === "undefined") return "unsupported";
  if (typeof HTMLScriptElement === "undefined" || !("noModule" in HTMLScriptElement.prototype)) return "unsupported";
  const cores = navigator.hardwareConcurrency || 0;
  if (cores && cores < 4) return "device";
  return /Mobi|Android|iPhone|iPad/.test(navigator.userAgent) ? "device" : null;
}

const memoryFailure = (text: string) => /MemoryError|out of memory|Out of bounds memory|Array buffer allocation|RangeError/i.test(text);

/** One worker: messages answered by id, a death or a limit fails what is pending. */
class EngineWorker {
  private worker: Worker;
  private pending = new Map<number, { resolve: (reply: Reply) => void; reject: (error: Error) => void }>();
  private sequence = 0;
  dead = false;

  constructor() {
    this.worker = new Worker(workerUrl(), { type: "module" });
    this.worker.onmessage = (event: MessageEvent<Reply>) => {
      const waiting = this.pending.get(event.data.id);
      this.pending.delete(event.data.id);
      waiting?.resolve(event.data);
    };
    this.worker.onerror = event => { event.preventDefault?.(); this.fail(new EngineFallback(memoryFailure(event.message ?? "") ? "memory" : "crashed", event.message)); };
    this.worker.onmessageerror = () => this.fail(new EngineFallback("crashed", "messageerror"));
  }

  private fail(error: Error) {
    this.dead = true;
    queueMicrotask(statusChanged);
    const waiting = [...this.pending.values()];
    this.pending.clear();
    waiting.forEach(entry => entry.reject(error));
    this.worker.terminate();
  }

  /** One request; past `timeoutMs` the worker is ended (it cannot be interrupted) and `onTimeout` thrown. */
  call(message: Record<string, unknown>, timeoutMs: number, onTimeout: FallbackReason): Promise<Reply> {
    if (this.dead) return Promise.reject(new EngineFallback("crashed"));
    const id = ++this.sequence;
    return new Promise<Reply>((resolve, reject) => {
      const timer = setTimeout(() => this.fail(new EngineFallback(onTimeout, `${timeoutMs} ms`)), timeoutMs);
      this.pending.set(id, {
        resolve: reply => { clearTimeout(timer); resolve(reply); },
        reject: error => { clearTimeout(timer); reject(error); },
      });
      this.worker.postMessage({ ...message, id });
    });
  }

  /** Ends the worker; a call in flight fails with `reason`. */
  stop(reason: FallbackReason = "crashed") { if (!this.dead) this.fail(new EngineFallback(reason)); }

  async init(bundle: EngineBundle) {
    const reply = await this.call({ type: "init", pyodideUrl: pyodideUrl(), bundle: { modules: bundle.modules, entry: bundle.entry } },
      INIT_TIMEOUT_MS, "init_timeout");
    if (!reply.ok) throw new EngineFallback(memoryFailure(reply.error) ? "memory" : "init_failed", reply.error);
  }

  /** The engine's JSON text for one message; a Python exception is `engine_error` (the Runtime then gives
   *  the user its own answer for the same request, a data refusal included). */
  async run(message: Record<string, unknown>, timeoutMs: number): Promise<{ text: string; ms: number }> {
    const reply = await this.call(message, timeoutMs, "slow");
    if (!reply.ok) throw new EngineFallback(memoryFailure(reply.error) ? "memory" : "engine_error", reply.error);
    return { text: reply.text ?? "null", ms: reply.ms };
  }
}

/** The kept worker (the editor card's backtest runs every few edits: no reload per run), its bundle digest. */
interface WarmEngine { rootUrl: string; digest: string | null; worker: EngineWorker | null; state: "loading" | "ready" | "failed"; ready: Promise<void>; readyMs?: number }
let warm: WarmEngine | null = null;
/** A failure that will not go away on this page (the load failed, timed out or ran out of memory). */
let disabled: FallbackReason | null = null;
const bundles = new Map<string, Promise<EngineBundle>>();

function engineBundle(rootUrl: string, fresh = false): Promise<EngineBundle> {
  if (fresh) bundles.delete(rootUrl);
  let bundle = bundles.get(rootUrl);
  if (!bundle) {
    bundle = requestJson<EngineBundle>(`${rootUrl}/api/strategy-engine/bundle`, { timeoutMs: LONG_READ_TIMEOUT_MS });
    bundle.catch(() => bundles.delete(rootUrl));
    bundles.set(rootUrl, bundle);
  }
  return bundle;
}

/** A worker with the bundle loaded (a fresh one, not the kept one). */
async function startWorker(bundle: EngineBundle): Promise<EngineWorker> {
  const worker = new EngineWorker();
  try {
    await worker.init(bundle);
    return worker;
  } catch (error) {
    worker.stop();
    throw error;
  }
}

function startWarm(rootUrl: string, fresh = false): WarmEngine {
  warm?.worker?.stop();
  const engine: WarmEngine = { rootUrl, digest: null, worker: null, state: "loading", ready: Promise.resolve() };
  const started = typeof performance !== "undefined" ? performance.now() : Date.now();
  engine.ready = (async () => {
    try {
      const bundle = await engineBundle(rootUrl, fresh);
      if (warm !== engine) return;
      const worker = await startWorker(bundle);
      if (warm !== engine) { worker.stop(); return; }
      Object.assign(engine, { worker, digest: bundle.digest, state: "ready", readyMs: Math.round(performance.now() - started) });
    } catch (error) {
      if (warm !== engine) return;
      engine.state = "failed";
      const reason = error instanceof EngineFallback ? error.reason : "init_failed";
      // A bundle read that failed (a network blip) may succeed next time; a load that failed will not.
      if (reason === "init_failed" || reason === "init_timeout" || reason === "memory") disabled = reason;
    } finally {
      statusChanged();
    }
  })();
  warm = engine;
  statusChanged();
  return engine;
}

/** A kept engine nobody used for this long is ended (a Pyodide worker holds 100–200 MB). */
export const IDLE_MS = 10 * 60_000;
let idleTimer: ReturnType<typeof setTimeout> | undefined;
function touch() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => { warm?.worker?.stop(); warm = null; statusChanged(); }, IDLE_MS);
}

/** Why the next run cannot be local (null = it can), starting the engine when it is not loaded. */
function localBlocker(rootUrl: string): FallbackReason | null {
  const blocked = disabled ?? browserSupport();
  if (blocked) return blocked;
  touch();
  if (document.visibilityState === "hidden") return "hidden";
  if (!warm || warm.rootUrl !== rootUrl || warm.state === "failed" || warm.worker?.dead) startWarm(rootUrl);
  return warm?.state === "ready" ? null : "warming";
}

/** Load the engine ahead of the first backtest (策略编辑卡 / 结果页 opened); nothing when the browser cannot. */
export function prewarmEngine(rootUrl: string) {
  if (!rootUrl) return;
  try { localBlocker(rootUrl); } catch { /* never in the way of the page */ }
}

/** The engine's state for the UI snapshot and diagnosis: off (with why) / idle (not loaded: nothing asked for
 *  it yet, or the kept one was ended) / loading / ready (with how long loading took) / failed. */
export interface EngineStatus { state: "off" | "idle" | "loading" | "ready" | "failed"; reason: FallbackReason | null; readyMs: number | null }
function currentStatus(): EngineStatus {
  const reason = disabled ?? browserSupport();
  const state = reason ? "off" : !warm || warm.worker?.dead ? "idle" : warm.state;
  return { state, reason, readyMs: state === "ready" ? warm?.readyMs ?? null : null };
}
let status: EngineStatus | null = null;
const statusListeners = new Set<() => void>();
function statusChanged() {
  const next = currentStatus();
  if (status && next.state === status.state && next.reason === status.reason && next.readyMs === status.readyMs) return;
  status = next;
  statusListeners.forEach(listener => listener());
}
export function engineStatus(): EngineStatus { return status ?? (status = currentStatus()); }
export function useEngineStatus(): EngineStatus {
  return useSyncExternalStore(listener => { statusListeners.add(listener); return () => { statusListeners.delete(listener); }; },
    engineStatus, engineStatus);
}

/** Runs `work`, ending it as `hidden` when the page is hidden meanwhile (a hidden page computes 17–100× slower:
 *  the Runtime answers sooner than waiting). */
async function whileVisible<T>(work: Promise<T>, onHidden: () => void): Promise<T> {
  let listener: (() => void) | undefined;
  const hidden = new Promise<never>((_, reject) => {
    listener = () => { if (document.visibilityState === "hidden") { onHidden(); reject(new EngineFallback("hidden")); } };
    document.addEventListener("visibilitychange", listener);
    listener();
  });
  try { return await Promise.race([work, hidden]); }
  finally { if (listener) document.removeEventListener("visibilitychange", listener); }
}

async function readText(url: string, init: RequestInit): Promise<string> {
  // A string body would otherwise go out as text/plain (the Request sets it), which the Runtime refuses.
  const response = await runtimeFetch(url, { ...init, headers: { "Content-Type": "application/json" }, timeoutMs: LONG_READ_TIMEOUT_MS });
  const text = await response.text();
  if (!response.ok) {
    let detail: unknown;
    try { detail = JSON.parse(text); } catch { detail = undefined; }
    throw Object.assign(new Error(`${response.status} ${text}`), { status: response.status, detail });
  }
  return text;
}

const detailOf = (error: unknown) => (error as { detail?: { detail?: unknown } })?.detail?.detail;
const statusOf = (error: unknown) => (error as { status?: number })?.status;

/** The Runtime's request body of a rule backtest (the same fields its backtest routes take). */
export interface RuleBacktestBody {
  strategyId?: string; plan?: unknown; symbol?: string; timeframe?: number; tradingDays?: number;
  slippageTicks?: number; preview?: boolean; sessionId?: string;
}

/** One rule backtest computed here, stored by the Runtime; throws EngineFallback or StaleEngine. */
export async function computeRuleBacktest<T>(rootUrl: string, body: RuleBacktestBody): Promise<T> {
  const blocked = localBlocker(rootUrl);
  if (blocked) throw new EngineFallback(blocked);
  const engine = warm!;
  let payload: string;
  try {
    payload = await readText(`${rootUrl}/api/strategy-backtests/inputs`, { method: "POST", body: JSON.stringify(body) });
  } catch (error) {
    throw new EngineFallback("inputs_failed", error instanceof Error ? error.message : String(error));
  }
  const { engineDigest, inputDigest, ticketId } = JSON.parse(payload) as { engineDigest: string; inputDigest: string; ticketId: string };
  if (engineDigest !== engine.digest) {
    startWarm(rootUrl, true);           // the Runtime has a newer engine: load it for the next run
    throw new EngineFallback("warming", "engine digest changed");
  }
  const worker = engine.worker!;
  const run = await whileVisible(worker.run({ type: "rule", inputsJson: payload }, RULE_TIMEOUT_MS), () => worker.stop("hidden"));
  // The report goes back as the engine wrote it: a JavaScript round trip would turn 3500.0 into 3500.
  // The ticket makes the hand-in repeatable: when the reply is lost (no HTTP status: network, timeout) the
  // same report goes in once more and the Runtime answers with the first stored result, never a second one.
  const handIn = `{"inputDigest":${JSON.stringify(inputDigest)},"ticketId":${JSON.stringify(ticketId)},"engineDigest":${JSON.stringify(engineDigest)},"runMs":${run.ms},"result":${run.text}}`;
  for (let attempt = 1; ; attempt++) {
    try {
      return JSON.parse(await readText(`${rootUrl}/api/strategy-backtests/browser-result`, { method: "POST", body: handIn })) as T;
    } catch (error) {
      if (statusOf(error) === 409 && detailOf(error) === "engine_changed") { bundles.delete(rootUrl); throw new StaleEngine(); }
      if (statusOf(error) === undefined && attempt < 2) continue;
      throw new EngineFallback(statusOf(error) === 409 ? "inputs_expired" : "inputs_failed", error instanceof Error ? error.message : String(error));
    }
  }
}

/**
 * A rule backtest the interface asks for: here when possible, else `server(reason)` (the Runtime's own
 * route with `fallbackReason`). `where: "server"` skips the browser (the authoritative rerun after saving).
 */
export async function ruleBacktest<T>(rootUrl: string, body: RuleBacktestBody, server: (reason: FallbackReason) => Promise<T>,
                                      where: "auto" | "server" = "auto"): Promise<T> {
  if (where === "server") return server("rerun");
  try {
    return await computeRuleBacktest<T>(rootUrl, body);
  } catch (error) {
    if (error instanceof StaleEngine) throw error;
    return server(error instanceof EngineFallback ? error.reason : "engine_error");
  }
}

/**
 * 比一比 computed here: the lab's inputs (`GET /api/strategy-labs/{id}/inputs`) shared by
 * clamp(cores − 2, 1, 3) workers — the kept one plus fresh ones loading in parallel (one that fails to load
 * is left out) — and the results posted back (`POST …/results`, re-ranked by the Runtime). Throws
 * EngineFallback (the caller starts the cloud run with the reason) or StaleEngine.
 */
export async function computeLab(rootUrl: string, labId: string, onProgress?: (done: number, total: number) => void): Promise<void> {
  const blocked = localBlocker(rootUrl);
  if (blocked) throw new EngineFallback(blocked);
  const engine = warm!;
  let payload: string;
  try {
    payload = await readText(`${rootUrl}/api/strategy-labs/${encodeURIComponent(labId)}/inputs`, { method: "GET" });
  } catch (error) {
    throw new EngineFallback("inputs_failed", error instanceof Error ? error.message : String(error));
  }
  const { engineDigest, variants } = JSON.parse(payload) as { engineDigest: string; variants: { id: string; label: string }[] };
  if (engineDigest !== engine.digest) {
    startWarm(rootUrl, true);
    throw new EngineFallback("warming", "engine digest changed");
  }
  const count = Math.min(3, Math.max(1, (navigator.hardwareConcurrency || 4) - 2), variants.length);
  const bundle = await engineBundle(rootUrl);
  const extra = await Promise.all(Array.from({ length: count - 1 }, () => startWorker(bundle).catch(() => null)));
  const workers = [engine.worker!, ...extra.filter((worker): worker is EngineWorker => worker !== null)];
  const stopAll = () => workers.forEach(worker => worker.stop("hidden"));
  try {
    const texts = new Array<string>(variants.length);
    let next = 0;
    let done = 0;
    const drain = async (worker: EngineWorker) => {
      while (next < variants.length) {
        const index = next++;
        texts[index] = (await worker.run({ type: "variant", key: labId, inputsJson: payload, variant: variants[index] }, VARIANT_TIMEOUT_MS)).text;
        onProgress?.(++done, variants.length);
      }
    };
    await whileVisible(Promise.all(workers.map(drain)), stopAll);
    try {
      await readText(`${rootUrl}/api/strategy-labs/${encodeURIComponent(labId)}/results`, { method: "POST",
        body: `{"engineDigest":${JSON.stringify(engineDigest)},"results":[${texts.join(",")}]}` });
    } catch (error) {
      const detail = detailOf(error);
      if (statusOf(error) === 409 && typeof detail === "string" && detail.includes("引擎")) { bundles.delete(rootUrl); throw new StaleEngine(); }
      // 409 = the cloud has (or is computing) this comparison: its result stands.
      if (statusOf(error) !== 409) throw new EngineFallback("inputs_failed", error instanceof Error ? error.message : String(error));
    }
  } finally {
    extra.forEach(worker => worker?.stop());
  }
}

/** Test helper: forget the kept engine and any page-long failure. */
export function resetBrowserEngine() { clearTimeout(idleTimer); warm?.worker?.stop(); warm = null; disabled = null; bundles.clear(); status = null; }

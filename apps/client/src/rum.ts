/**
 * 真实用户监测（RUM，所有者 2026-10-13）：this page's anonymous technical metrics, summed per minute
 * and sent once a minute to the Runtime it is signed in to (`POST /api/telemetry/rum`).
 *
 * Collected: page vitals through Google's web-vitals (LCP/INP/CLS/FCP/TTFB); the price's end-to-end
 * latency — the sidecar's capture time of a pushed quote to the frame that paints it, on the server's
 * clock (the offset comes from the Runtime's `serverNow`, the lowest-round-trip answer wins); the
 * K-line chart's first paint after page start; the session stream's drops (how long, how often it
 * reconnected); page errors counted by kind — never their text; visible time and JS heap (Chromium).
 * Each metric leaves as a count, a sum and bucket counts over RUM_BUCKET_BOUNDS, never raw points;
 * nothing carries a symbol, message, URL or account. A hidden or closing page flushes with sendBeacon.
 *
 * Off in unit tests and automated browsers (navigator.webdriver — test runs are not users;
 * localStorage `ai-trader:rum-force` = "1" turns it on for a check). The Runtime decides the rest:
 * AI_TRADER_RUM_ENABLED=0 or an unsampled session answers `enabled: false` and the page stops.
 */
import { onCLS, onFCP, onINP, onLCP, onTTFB, type Metric } from "web-vitals";
import { MOBILE_LAYOUT_QUERY } from "./hooks/useMobileLayout";

/** Upper bounds of the histogram buckets — the same list as the Runtime's rum.BUCKET_BOUNDS (v1). */
export const RUM_BUCKET_BOUNDS = [10, 25, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000, 13000, 20000, 30000, 60000];
export const RUM_FLUSH_MS = 60_000;
/** Minutes kept while there is nowhere to send them (signed out, offline); older ones are dropped. */
const MAX_QUEUED_MINUTES = 10;
/** A price older than this when painted is a stale frame (a tab woken up), not a latency. */
const MAX_PRICE_LATENCY_MS = 60_000;
const MAX_PENDING_PRICE_SAMPLES = 600;

export type RumHistogramMetric = "lcp" | "inp" | "cls" | "fcp" | "ttfb" | "price_latency" | "kline_first_paint" | "sse_down" | "memory";
export type RumErrorKind = "chunk" | "network" | "type" | "reference" | "range" | "syntax" | "security" | "rejection" | "other";

interface Bucketed { n: number; sum: number; buckets?: Record<string, number> }

export interface RumTarget {
  /** The Runtime base URL the page is signed in to. */
  baseUrl: string;
  /** The request headers that carry its login (cookie-only logins add none). */
  headers: () => Headers;
  /** Whose login the headers carry ("guest" / "member"). A page that moves to another identity (a guest
   *  who signs in) starts a new page session and leaves the old identity's minutes behind. */
  identity?: string;
}

export interface RumDeps {
  now: () => number;
  fetch: typeof fetch;
  beacon?: (url: string, body: Blob) => boolean;
  random: () => number;
}

export function bucketOf(value: number): number {
  const index = RUM_BUCKET_BOUNDS.findIndex(bound => value <= bound);
  return index < 0 ? RUM_BUCKET_BOUNDS.length : index;
}

/** A page error's kind from its name and shape only; the message is matched, never kept. */
export function errorKind(reason: unknown, rejection: boolean): RumErrorKind | undefined {
  const name = reason instanceof Error ? reason.name : "";
  const message = reason instanceof Error ? reason.message : typeof reason === "string" ? reason : "";
  if (name === "AbortError") return undefined;   // a cancelled request is not a page error
  if (name === "ChunkLoadError" || /dynamically imported module|Importing a module script failed|Loading chunk/i.test(message)) return "chunk";
  if (name === "TypeError" && /Failed to fetch|NetworkError|Load failed|network connection/i.test(message)) return "network";
  const byName: Record<string, RumErrorKind> = { TypeError: "type", ReferenceError: "reference", RangeError: "range", SyntaxError: "syntax", SecurityError: "security" };
  if (byName[name]) return byName[name];
  return rejection ? "rejection" : "other";
}

function clientKind(): "web" | "desktop" | "mobile" {
  if (typeof window === "undefined") return "web";
  if ("__TAURI_INTERNALS__" in window) return "desktop";
  return typeof window.matchMedia === "function" && window.matchMedia(MOBILE_LAYOUT_QUERY).matches ? "mobile" : "web";
}

function networkKind(): string {
  const connection = (navigator as Navigator & { connection?: { type?: string; effectiveType?: string } }).connection;
  const type = connection?.type;
  if (type === "wifi" || type === "ethernet" || type === "cellular") return type;
  const effective = connection?.effectiveType;
  return effective && ["4g", "3g", "2g", "slow-2g"].includes(effective) ? effective : "unknown";
}

function sessionId(random: () => number): string {
  const bytes = typeof crypto !== "undefined" && crypto.getRandomValues
    ? crypto.getRandomValues(new Uint8Array(16)) : Uint8Array.from({ length: 16 }, () => Math.floor(random() * 256));
  return Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
}

export class RumCollector {
  private pageSession: string;
  private minutes = new Map<number, Map<string, Bucketed>>();
  private target?: RumTarget;
  private stopped = false;
  private offsetMs?: number;
  private bestRoundTrip = Infinity;
  private pendingPrice: number[] = [];
  private pricePending = false;
  private lastCapture?: string;
  /** The newest capture time seen since the last paint callback was scheduled: that callback measures it. */
  private latestCapture = 0;
  /** What the current target is, remembered across the signed-out gap between two targets. */
  private targetKey?: string;
  private downSince?: number;
  private visibleSince?: number;
  private chartPainted = false;
  /** A page that was hidden at any point before its chart drew has no meaningful first paint. */
  private everHidden = false;

  constructor(private readonly deps: RumDeps) {
    this.pageSession = sessionId(deps.random);
  }

  get session() { return this.pageSession; }

  get active() { return !this.stopped; }

  private minuteOf(time = this.deps.now()) { return Math.floor(time / 60_000) * 60; }

  private entry(name: string, minute = this.minuteOf()): Bucketed {
    let metrics = this.minutes.get(minute);
    if (!metrics) {
      metrics = new Map();
      this.minutes.set(minute, metrics);
      while (this.minutes.size > MAX_QUEUED_MINUTES) this.minutes.delete(Math.min(...this.minutes.keys()));
    }
    let value = metrics.get(name);
    if (!value) { value = { n: 0, sum: 0 }; metrics.set(name, value); }
    return value;
  }

  record(metric: RumHistogramMetric, value: number) {
    if (this.stopped || !Number.isFinite(value) || value < 0) return;
    const entry = this.entry(metric);
    const bucket = String(bucketOf(value));
    entry.n += 1;
    entry.sum += Math.round(value);
    entry.buckets = { ...entry.buckets, [bucket]: (entry.buckets?.[bucket] ?? 0) + 1 };
  }

  count(metric: string, n = 1, sum = 0) {
    if (this.stopped) return;
    const entry = this.entry(metric);
    entry.n += n;
    entry.sum += Math.round(sum);
  }

  error(reason: unknown, rejection: boolean) {
    const kind = errorKind(reason, rejection);
    if (kind) this.count(`error.${kind}`);
  }

  /** A pushed market frame carried this capture time: measure it once, at the next paint. */
  notePriceCapture(capturedAt: string | undefined, schedule: (callback: () => void) => void) {
    if (this.stopped || !capturedAt || capturedAt === this.lastCapture) return;
    if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
    const captured = Date.parse(capturedAt);
    if (!Number.isFinite(captured)) return;
    this.lastCapture = capturedAt;
    // Several frames can land before one paint: the state already shows the newest, so that is the one measured.
    this.latestCapture = captured;
    if (this.pricePending) return;
    this.pricePending = true;
    schedule(() => {
      this.pricePending = false;
      const raw = this.deps.now() - this.latestCapture;
      if (this.offsetMs === undefined) {
        if (this.pendingPrice.length < MAX_PENDING_PRICE_SAMPLES) this.pendingPrice.push(raw);
      } else this.recordPrice(raw + this.offsetMs);
    });
  }

  private recordPrice(latency: number) {
    if (latency <= MAX_PRICE_LATENCY_MS) this.record("price_latency", Math.max(0, latency));
  }

  noteChartPainted(sincePageStart: number) {
    if (this.chartPainted) return;
    this.chartPainted = true;
    if (!this.everHidden) this.record("kline_first_paint", sincePageStart);
  }

  /** The stream is down. `since` is when it really went quiet — the last frame, or the start of the failed
   *  attempt — because a silent drop is only noticed after the idle watchdog, long after it began. */
  noteStreamDown(since?: number) {
    if (this.downSince !== undefined) return;
    const now = this.deps.now();
    this.downSince = since === undefined ? now : Math.min(now, since);
  }

  noteStreamUp() {
    if (this.downSince === undefined) return;
    this.record("sse_down", this.deps.now() - this.downSince);
    this.count("sse_reconnect");
    this.downSince = undefined;
  }

  /** The one place visible time is booked: the interval is split on minute boundaries, each part into its
   *  own minute (an interval carried over a gap — hidden tab, no target, a late timer — must not land in a
   *  single bucket). Minutes older than the queue keeps are not worth splitting out. */
  private addVisible(from: number, to: number) {
    let cursor = Math.max(from, to - MAX_QUEUED_MINUTES * 60_000);
    while (cursor < to && !this.stopped) {
      const start = Math.floor(cursor / 60_000) * 60_000;
      const end = Math.min(to, start + 60_000);
      const entry = this.entry("visible", start / 1000);
      entry.n += 1;
      entry.sum += Math.round(end - cursor);
      cursor = end;
    }
  }

  /** Book the visible time up to now and carry on from now (hide, the timer, a restored target, a batch). */
  private rollVisible() {
    if (this.visibleSince === undefined) return;
    const now = this.deps.now();
    this.addVisible(this.visibleSince, now);
    this.visibleSince = now;
  }

  noteVisibility(visible: boolean) {
    const now = this.deps.now();
    if (!visible) this.everHidden = true;
    if (!visible && this.visibleSince !== undefined) {
      this.rollVisible();
      this.visibleSince = undefined;
    } else if (visible && this.visibleSince === undefined) this.visibleSince = now;
  }

  /** The Runtime's clock answer: keep the offset measured with the shortest round trip. */
  noteServerClock(serverNow: unknown, sentAt: number, receivedAt: number) {
    if (typeof serverNow !== "number" || !Number.isFinite(serverNow)) return;
    const roundTrip = receivedAt - sentAt;
    if (roundTrip > this.bestRoundTrip) return;
    this.bestRoundTrip = roundTrip;
    this.offsetMs = serverNow - (sentAt + receivedAt) / 2;
    const pending = this.pendingPrice;
    this.pendingPrice = [];
    pending.forEach(raw => this.recordPrice(raw + this.offsetMs!));
  }

  setTarget(target: RumTarget | undefined) {
    // The outgoing target still holds its own login: hand it the minutes gathered under it before they could
    // be read as the next identity's.
    if (this.target && this.target !== target) void this.flush();
    this.target = target;
    if (!target) return;
    const key = `${target.identity ?? ""}|${target.baseUrl}`;
    if (this.targetKey !== undefined && this.targetKey !== key) {
      // Another identity or Runtime on this page (a guest who signed in): its first batch must not carry the
      // old one's pending minutes, and the visits are two sessions, not one.
      this.minutes.clear();
      this.pendingPrice = [];
      this.pageSession = sessionId(this.deps.random);
    }
    this.targetKey = key;
    this.rollVisible();
    if (!this.stopped) void this.handshake(target);
  }

  private async handshake(target: RumTarget) {
    const sentAt = this.deps.now();
    try {
      const response = await this.deps.fetch(`${target.baseUrl}/api/telemetry/rum`, { credentials: "include", headers: target.headers() });
      if (!response.ok) return;
      const answer = await response.json() as { enabled?: boolean; serverNow?: number; sampleRate?: number };
      this.noteServerClock(answer.serverNow, sentAt, this.deps.now());
      if (answer.enabled === false) this.stop();
    } catch { /* best effort: the next flush tries again */ }
  }

  stop() {
    this.stopped = true;
    this.minutes.clear();
    this.pendingPrice = [];
  }

  /** The batch to send now: every queued minute, the current one included (the Runtime adds a
   *  minute's later batches to it). Empty when there is nothing to say. */
  takeBatch(): string | undefined {
    this.rollVisible();
    const heap = (performance as Performance & { memory?: { usedJSHeapSize?: number } }).memory?.usedJSHeapSize;
    if (heap && typeof document !== "undefined" && document.visibilityState === "visible") this.record("memory", heap / 1_048_576);
    const minutes = [...this.minutes.entries()].sort(([a], [b]) => a - b)
      .map(([t, metrics]) => ({ t, metrics: Object.fromEntries(metrics) }))
      .filter(minute => Object.keys(minute.metrics).length);
    if (!minutes.length || this.stopped) return undefined;
    this.minutes.clear();
    return JSON.stringify({ v: 1, session: this.session, client: clientKind(), network: networkKind(), minutes });
  }

  async flush(unloading = false) {
    this.rollVisible();   // also with nowhere to send: the interval is split on its minute boundaries now
    const target = this.target;
    if (!target || this.stopped) return;
    const body = this.takeBatch();
    if (!body) return;
    const url = `${target.baseUrl}/api/telemetry/rum`;
    const headers = target.headers();
    headers.set("Content-Type", "text/plain");
    // A cookie login rides on a beacon, which outlives the closing page; a bearer login needs headers.
    if (unloading && this.deps.beacon && !headers.has("Authorization")
        && this.deps.beacon(url, new Blob([body], { type: "text/plain" }))) return;
    const sentAt = this.deps.now();
    try {
      const response = await this.deps.fetch(url, { method: "POST", body, headers, credentials: "include", keepalive: true });
      if (!response.ok) return;
      const answer = await response.json() as { enabled?: boolean; serverNow?: number };
      this.noteServerClock(answer.serverNow, sentAt, this.deps.now());
      if (answer.enabled === false) this.stop();
    } catch { /* telemetry is best effort: a lost minute is not retried */ }
  }
}

let collector: RumCollector | undefined;

function allowed(): boolean {
  if (typeof window === "undefined" || import.meta.env.MODE === "test") return false;
  try {
    if (window.localStorage.getItem("ai-trader:rum-force") === "1") return true;
  } catch { /* storage blocked: fall through */ }
  return !navigator.webdriver;
}

/** Start collecting for this page (once, at boot). Sending waits for setRumTarget. */
export function startRum() {
  if (collector || !allowed()) return;
  const rum = collector = new RumCollector({
    now: () => Date.now(),
    fetch: (...args) => fetch(...args),
    beacon: typeof navigator.sendBeacon === "function" ? (url, body) => navigator.sendBeacon(url, body) : undefined,
    random: Math.random,
  });
  const vital = (name: RumHistogramMetric, scale = 1) => (metric: Metric) => rum.record(name, metric.value * scale);
  onLCP(vital("lcp"));
  onINP(vital("inp"));
  onCLS(vital("cls", 1000));
  onFCP(vital("fcp"));
  onTTFB(vital("ttfb"));
  window.addEventListener("error", event => rum.error(event.error ?? event.message, false));
  window.addEventListener("unhandledrejection", event => rum.error(event.reason, true));
  rum.noteVisibility(document.visibilityState === "visible");
  document.addEventListener("visibilitychange", () => {
    const visible = document.visibilityState === "visible";
    rum.noteVisibility(visible);
    if (!visible) void rum.flush(true);
  });
  window.addEventListener("pagehide", () => void rum.flush(true));
  window.setInterval(() => void rum.flush(), RUM_FLUSH_MS);
}

/** Where to send: the Runtime this page is signed in to, or undefined while signed out. */
export function setRumTarget(target: RumTarget | undefined) {
  collector?.setTarget(target);
}

/** A pushed market frame was applied; measure its price at the frame that paints it. */
export function rumPriceFrame(capturedAt: string | undefined) {
  collector?.notePriceCapture(capturedAt, callback => requestAnimationFrame(() => setTimeout(callback, 0)));
}

/** The K-line chart drew its first bars. */
export function rumChartPainted() {
  if (collector) requestAnimationFrame(() => collector?.noteChartPainted(performance.now()));
}

export function rumStreamDown(since?: number) { collector?.noteStreamDown(since); }
export function rumStreamUp() { collector?.noteStreamUp(); }

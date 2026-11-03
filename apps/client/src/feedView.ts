import { useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { requestJson } from "./api";
import { feedReportLiveNow, feedReportRefused, feedStreamAlive, forgetFeedReport, onBoardResync, onMarketResync, useFeedReportEntries } from "./runtimeFeed";

/**
 * 只推看得见的 (2026-10-12): what this window shows, told to the Runtime with
 * PUT /api/sessions/{id}/feed/view — the page's visibility (Page Visibility API) and the 报价总表
 * rows the mounted board readers need (their union). A hidden page gets no chart market and no
 * board push; account, activity, execution and every event (orders, alerts, reminders, AI results)
 * keep coming. Coming back, the Runtime sends one full chart and one full board before patches.
 *
 * The window's session is the identity (no new one): useRuntimeSession names it on attach and the
 * focus and view are sent again on every stream (re)open, after a board patch that missed its base, and on any
 * change. A Runtime without the route (older) is remembered per base URL and never asked again;
 * the board readers then keep polling as before.
 *
 * 通用报告推送 (2026-11-03): the same view names the cloud reports the mounted readers show (`reports`, each a
 * name plus its read's query, e.g. "desk?symbol=SHFE.au2612"); the Runtime pushes each one on change (feed.report).
 * A reader shows what is pushed and reads the report itself only while the push does not serve it
 * (`feedReportServes`); a Runtime that refuses the field (older) is remembered per base URL and never sent it again.
 */
export interface BoardWant {
  /** Every row (the board view's 全部, the contract drawer, the phone 报价 list). */
  all?: boolean;
  exchanges?: readonly string[];
  /** Product ids ("au") or exchange-qualified ("SHFE.au", the watchlist's form). */
  products?: readonly string[];
  symbols?: readonly string[];
}

interface ViewBoard { all: boolean; exchanges: string[]; products: string[]; symbols: string[]; key: string }

const RETRY_MS = 2_000;
const DEBOUNCE_MS = 50;
const wants = new Map<number, BoardWant>();
let nextWant = 0;
let target: { baseUrl: string; sessionId: string; inFlight: boolean; again: boolean } | undefined;
const unsupported = new Set<string>();
const reportWants = new Map<number, string>();
/** When each shown report key entered the view: its first frame is awaited this long before the reader reads. */
const reportSince = new Map<string, number>();
const reportsRefused = new Set<string>();
/** The report keys in the view the Runtime last acknowledged (see flush). */
let heldReports = new Set<string>();
/** Waiting for a report's first push stops after this long: the reader then reads it itself. */
export const REPORT_GRACE_MS = 3_000;
/** How often a reader whose report the push serves looks again whether it still does (no request). */
export const REPORT_RECHECK_MS = 2_000;
let sentBody = "";
let viewRevision = 0;
interface FocusWant {
  baseUrl: string; sessionId: string;
  send: (current: () => boolean) => Promise<void>;
  revision: number; sent: number;
}
let focus: FocusWant | undefined;
let lastMarketResync = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
let currentKey = "";
const listeners = new Set<() => void>();
let visibilityInstalled = false;

function fnv(text: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(36);
}

const clean = (items: Iterable<string>) => [...new Set([...items].map(item => item.trim()).filter(item => /^[A-Za-z0-9_.-]{1,80}$/.test(item)))].sort();

/** The rows every mounted reader needs, as one request; undefined when no board is on screen. */
export function unionBoardWant(items: Iterable<BoardWant>): ViewBoard | undefined {
  const list = [...items];
  if (!list.length) return undefined;
  if (list.some(item => item.all)) return { all: true, exchanges: [], products: [], symbols: [], key: "all" };
  const exchanges = clean(list.flatMap(item => item.exchanges ?? []).map(item => item.toUpperCase()));
  const products = clean(list.flatMap(item => item.products ?? []).map(item => item.toLowerCase()));
  const symbols = clean(list.flatMap(item => item.symbols ?? []).map(item => item.toUpperCase()));
  return { all: false, exchanges, products, symbols, key: `w:${fnv(JSON.stringify([exchanges, products, symbols]))}` };
}

function viewText(baseUrl: string) {
  const board = unionBoardWant(wants.values());
  const visible = typeof document === "undefined" || document.visibilityState !== "hidden";
  const reports = reportsRefused.has(baseUrl) ? [] : [...reportSince.keys()].sort();
  // Without reports the body stays what an older Runtime accepts.
  return JSON.stringify(reports.length ? { visible, board: board ?? null, reports } : { visible, board: board ?? null });
}

function changed() {
  const key = unionBoardWant(wants.values())?.key ?? "";
  if (key !== currentKey) { currentKey = key; listeners.forEach(listener => listener()); }
  const shown = new Set(reportWants.values());
  for (const report of shown) if (!reportSince.has(report)) reportSince.set(report, Date.now());
  for (const report of [...reportSince.keys()]) if (!shown.has(report)) reportSince.delete(report);
  schedule(DEBOUNCE_MS);
}

function schedule(delay: number) {
  if (timer !== undefined) clearTimeout(timer);
  timer = setTimeout(() => { timer = undefined; void flush(); }, delay);
}

async function flush() {
  const current = target;
  if (!current) return;
  if (current.inFlight) { current.again = true; return; }
  const wanted = focus;
  const focusCurrent = () => target === current && focus === wanted;
  const revision = wanted?.revision;
  const text = viewText(current.baseUrl), view = viewRevision;
  const told = new Set(reportsRefused.has(current.baseUrl) ? [] : reportSince.keys());
  current.inFlight = true;
  try {
    await Promise.all([
      (async () => {
        if (!wanted || wanted.baseUrl !== current.baseUrl || wanted.sessionId !== current.sessionId
            || wanted.sent === revision) return;
        try {
          await wanted.send(focusCurrent);
          if (focusCurrent()) wanted.sent = revision!;
        } catch (error) {
          const status = (error as { status?: number }).status;
          if (status !== 401 && status !== 403 && focusCurrent()) schedule(RETRY_MS);
        }
      })(),
      (async () => {
        if (unsupported.has(current.baseUrl) || text === sentBody) return;
        try {
          await requestJson(`${current.baseUrl}/api/sessions/${encodeURIComponent(current.sessionId)}/feed/view`, { method: "PUT", body: text });
          // A reply from before a reconnect cannot acknowledge the new Runtime's subscription.
          if (target === current && viewRevision === view) {
            sentBody = text;
            // A pushed report is dropped only once the Runtime took a view without it: the Runtime forgets what this
            // client holds at that same point, so the key shown again gets its current content once. A key that left
            // and came back between two sends (one panel closing as another opens it) never left the view; it stays.
            for (const report of heldReports) if (!told.has(report)) forgetFeedReport(report);
            heldReports = told;
          }
        } catch (error) {
          const status = (error as { status?: number }).status;
          const detail = (error as { detail?: { detail?: unknown } }).detail?.detail;
          if ((status === 404 && detail === "Not Found") || status === 405) unsupported.add(current.baseUrl);
          // An older Runtime refuses the reports field: send the view without it; its readers keep reading.
          else if (status === 422 && text.includes('"reports"') && !reportsRefused.has(current.baseUrl)) { reportsRefused.add(current.baseUrl); schedule(0); }
          else if (status !== 401 && status !== 403 && target === current) schedule(RETRY_MS);
        }
      })(),
    ]);
  } finally {
    current.inFlight = false;
    if (target === current && current.again) { current.again = false; schedule(0); }
  }
}

/** The mounted chart owns its focus; the session coordinator owns sending and retrying it. */
export function registerFeedFocus(baseUrl: string, sessionId: string, send: FocusWant["send"]) {
  const wanted = { baseUrl, sessionId, send, revision: 0, sent: -1 };
  focus = wanted;
  lastMarketResync = 0;
  schedule(0);
  return () => { if (focus === wanted) focus = undefined; };
}

/** Every stream open replays both subscriptions, independently of the visible connection state. */
export function resendFeedSubscriptions(baseUrl: string, sessionId: string) {
  if (target?.baseUrl !== baseUrl || target.sessionId !== sessionId) return;
  if (focus?.baseUrl === baseUrl && focus.sessionId === sessionId) ++focus.revision;
  resendFeedView();
}

function installVisibility() {
  if (visibilityInstalled || typeof document === "undefined") return;
  visibilityInstalled = true;
  document.addEventListener("visibilitychange", () => schedule(0));
  onBoardResync(sessionId => { if (target?.sessionId === sessionId) resendFeedView(); });
  onMarketResync(sessionId => {
    if (!focus || focus.sessionId !== sessionId || focus.baseUrl !== target?.baseUrl
        || target.sessionId !== sessionId || Date.now() - lastMarketResync < RETRY_MS) return;
    lastMarketResync = Date.now();
    ++focus.revision;
    schedule(0);
  });
}

/** The window attached a session (useRuntimeSession): the view goes to it once its stream opens. */
export function setFeedViewTarget(baseUrl: string, sessionId: string) {
  installVisibility();
  const current = { baseUrl, sessionId, inFlight: false, again: false };
  target = current;
  sentBody = "";
  ++viewRevision;
  return () => {
    if (target === current) {
      target = undefined;
      if (timer !== undefined) clearTimeout(timer);
      timer = undefined;
    }
  };
}

/** The stream (re)opened, or a board patch missed its base: send the view again for full frames. */
export function resendFeedView() {
  // A new stream starts the reports over: their first frames are awaited again before readers read.
  for (const key of reportSince.keys()) reportSince.set(key, Date.now());
  ++viewRevision;
  sentBody = "";
  schedule(0);
}

/** Can the board push serve readers on this Runtime? (The session's Runtime, with the route.) */
export function feedViewServes(baseUrl: string | undefined): boolean {
  return Boolean(baseUrl && target?.baseUrl === baseUrl && !unsupported.has(baseUrl));
}

const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };

/** Register the rows a mounted reader shows (undefined = none now); returns the window's board key. */
export function useBoardWant(want: BoardWant | undefined): string {
  const id = useRef<number>(undefined);
  if (id.current === undefined) id.current = ++nextWant;
  const text = want ? JSON.stringify(want) : "";
  useEffect(() => {
    if (!text) return;
    const slot = id.current!;
    wants.set(slot, JSON.parse(text) as BoardWant);
    changed();
    return () => { wants.delete(slot); changed(); };
  }, [text]);
  return useSyncExternalStore(subscribe, () => currentKey, () => currentKey);
}

/** Report keys: a name and its read's query, in the order given (undefined values left out). */
export function reportKey(name: string, params: Record<string, string | number | null | undefined> = {}): string {
  const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([field, value]) => [field, String(value)])).toString();
  return query ? `${name}?${query}` : name;
}

/**
 * Non-reactive, for a reader's timer: does the push serve this report on this Runtime — delivering it now, or its first
 * frame still due (the key entered the view under REPORT_GRACE_MS ago on a live stream)? While it does, the reader does
 * not read the report itself.
 */
export function feedReportServes(baseUrl: string | undefined, key: string | undefined, now = Date.now()): boolean {
  if (!key || !feedViewServes(baseUrl) || reportsRefused.has(baseUrl!)) return false;
  if (feedReportLiveNow(key, now)) return true;
  if (feedReportRefused(key)) return false;
  const since = reportSince.get(key);
  return since !== undefined && now - since < REPORT_GRACE_MS && feedStreamAlive(now);
}

/**
 * Show these cloud reports in this window's view (none while `keys` is empty) and get what the push sent for each, by
 * key. A value is the report's HTTP answer; the reader applies it like its own read and keeps it when the key goes.
 */
export function useFeedReports<T>(baseUrl: string | undefined, keys: readonly string[]): ReadonlyMap<string, T> {
  const text = baseUrl ? keys.join("\n") : "";
  useEffect(() => {
    if (!text) return;
    const slots = text.split("\n").map(key => { const slot = ++nextWant; reportWants.set(slot, key); return slot; });
    changed();
    return () => { slots.forEach(slot => reportWants.delete(slot)); changed(); };
  }, [text]);
  const entries = useFeedReportEntries();
  return useMemo(() => {
    const found = new Map<string, T>();
    if (text) for (const key of text.split("\n")) if (entries?.[key]?.status === "ok") found.set(key, entries[key].value as T);
    return found;
  }, [entries, text]);
}

/** One report (see useFeedReports); undefined key: none. */
export function useFeedReport<T>(baseUrl: string | undefined, key: string | undefined): T | undefined {
  return useFeedReports<T>(baseUrl, key ? [key] : []).get(key ?? "");
}

/** Tests: forget every reader, target and remembered Runtime. */
export function resetFeedViewForTests() {
  reportWants.clear(); reportSince.clear(); reportsRefused.clear(); heldReports = new Set();
  wants.clear(); target = undefined; focus = undefined; ++viewRevision; unsupported.clear(); sentBody = ""; currentKey = "";
  if (timer !== undefined) clearTimeout(timer);
  timer = undefined;
  listeners.forEach(listener => listener());
}

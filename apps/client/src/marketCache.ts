import { getMarketWorkbench, type InstrumentCatalog, type MarketKlineBar, type MarketWorkbenchSnapshot } from "./api";
import { cacheDb, type KlineRecord } from "./localCache";

/**
 * The charts this browser has seen, kept locally (2026-09-27; IndexedDB + 增量拉取 2026-10-12).
 *
 * Drawing: opening the page or switching back to a symbol/period draws the last known chart at once, marked
 * stale and cached — the order ticket stays "不可下单" and the Runtime still checks every order's quote —
 * until the live market replaces it.
 *
 * Reading: a chart's closed bars stay in IndexedDB (localCache.ts) by Runtime address, symbol and period; the
 * forming last bar never does. `readMarketWorkbench` asks the Runtime only for the bars after the last one
 * held (`since`); the answer proves the held bars still equal the Runtime's window (`barsWindow`: first bar,
 * count and a digest of the bars up to `since`) — a corrected bar, a changed trading day or a source switch
 * fails the proof and the whole window is read again. The merged chart is exactly the Runtime's window, never
 * more: a shorter window (a guest's limit, a Runtime with less history) trims the held bars to it.
 *
 * Public market data only; replays are never cached. Without IndexedDB the charts are kept for this page only.
 */
const MEMORY_CHARTS = 24;
/** The stored charts' total size (JSON characters) before the least recently used go. */
export const STORE_MAX_CHARS = 8 * 1024 * 1024;
const WRITE_EVERY_MS = 15_000;
const TOUCH_EVERY_MS = 60_000;
const LEGACY_KEY = "ai-trader:market-cache:v1";

/** Public display names only. Cached names never supply tradable catalog entries or quote metadata. */
export async function loadInstrumentNames(runtime: string): Promise<Record<string, string>> {
  try { return (await (await cacheDb())?.get("instrumentNames", runtime))?.names ?? {}; }
  catch { return {}; }
}

export async function rememberInstrumentNames(runtime: string, catalog: InstrumentCatalog): Promise<Record<string, string>> {
  const names = Object.fromEntries(catalog.items.filter(item => item.instrumentName)
    .map(item => [item.symbol.toUpperCase(), item.instrumentName]));
  try {
    const db = await cacheDb();
    const previous = await db?.get("instrumentNames", runtime);
    // Partial catalogs retain names of contracts not included in this packet.
    const merged = { ...previous?.names, ...names };
    await db?.put("instrumentNames", { runtime, names: merged });
    return merged;
  } catch { return names; }
}

try { localStorage.removeItem(LEGACY_KEY); } catch { /* storage unavailable */ }

type SinceWindow = { first: number; count: number; digest: string };
type SincePacket = MarketWorkbenchSnapshot & { barsAfter?: number; barsWindow?: SinceWindow };

const memory = new Map<string, KlineRecord>();
const written = new Map<string, { at: number; tail: number | undefined }>();
const touched = new Map<string, number>();

function keyOf(runtime: string, symbol: string, duration: number) {
  return `${runtime}|${symbol.toUpperCase()}|${duration}`;
}

function hold(record: KlineRecord) {
  memory.delete(record.key);
  memory.set(record.key, record);
  while (memory.size > MEMORY_CHARTS) memory.delete(memory.keys().next().value as string);
}

function asStale(record: KlineRecord): MarketWorkbenchSnapshot {
  return { ...record.meta, bars: record.bars, dataStatus: "stale", isCached: true };
}

// ---- digest: the same canonical text as the Runtime's shared_market.bars_digest -----------------------------

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let crc = 0xffffffff;
  for (const byte of bytes) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return ((crc ^ 0xffffffff) >>> 0).toString(16).padStart(8, "0");
}

const number = (value: unknown, divisor = 1, multiplier = 1) =>
  typeof value === "number" && Number.isFinite(value) ? String(Math.floor(value / divisor * multiplier + 0.5)) : "-";

/** CRC-32 of the bars' canonical text: time in whole seconds, OHLC/volume/open interest in 1/10000, trading day. */
export function barsDigest(bars: readonly MarketKlineBar[]): string {
  return crc32(bars.map(bar => [number(bar.datetime, 1e9), number(bar.open, 1, 10_000), number(bar.high, 1, 10_000),
    number(bar.low, 1, 10_000), number(bar.close, 1, 10_000), number(bar.volume, 1, 10_000), number(bar.openInterest, 1, 10_000),
    typeof bar.tradingDay === "string" ? bar.tradingDay : ""].join(",")).join(";"));
}

/**
 * The full chart from held closed bars and a `since` answer, or undefined when the held bars do not prove
 * equal to the Runtime's window (then the caller reads the whole window). A whole-window answer (no
 * `barsAfter`: an older Runtime, or a `since` the window no longer holds) is the chart itself.
 */
export function mergeSince(held: readonly MarketKlineBar[] | undefined, packet: SincePacket): MarketWorkbenchSnapshot | undefined {
  const { barsAfter, barsWindow, ...snapshot } = packet;
  if (barsAfter === undefined) return snapshot;
  if (!held || !barsWindow) return undefined;
  const prefix = held.filter(bar => bar.datetime >= barsWindow.first && bar.datetime <= barsAfter);
  if (!prefix.length || prefix[0].datetime !== barsWindow.first || prefix[prefix.length - 1].datetime !== barsAfter
      || prefix.length + packet.bars.length !== barsWindow.count || barsDigest(prefix) !== barsWindow.digest) return undefined;
  return { ...snapshot, bars: [...prefix, ...packet.bars] };
}

// ---- the store ----------------------------------------------------------------------------------------------

async function stored(key: string): Promise<KlineRecord | undefined> {
  const kept = memory.get(key);
  if (kept) { touch(key); return kept; }
  try {
    const db = await cacheDb();
    const record = await db?.get("klines", key);
    if (!record || !Array.isArray(record.bars) || !record.meta?.quote) return undefined;
    // A newer chart may have arrived while the store was read.
    if (!memory.has(key)) hold(record);
    touch(key);
    return memory.get(key);
  } catch {
    return undefined;
  }
}

function touch(key: string, now = Date.now()) {
  if (now - (touched.get(key) ?? 0) < TOUCH_EVERY_MS) return;
  touched.set(key, now);
  void (async () => {
    try {
      const db = await cacheDb();
      const use = await db?.get("klineUse", key);
      if (db && use) await db.put("klineUse", { ...use, usedAt: now });
    } catch { /* not kept */ }
  })();
}

async function persist(record: KlineRecord, now: number) {
  const db = await cacheDb();
  if (!db) return;
  const size = JSON.stringify(record).length;
  const write = async () => {
    const tx = db.transaction(["klines", "klineUse"], "readwrite");
    await Promise.all([tx.objectStore("klines").put(record), tx.objectStore("klineUse").put({ key: record.key, size, usedAt: now }), tx.done]);
  };
  try {
    await write();
  } catch {
    // Quota: make room and try once more; failing that the chart is simply not kept.
    try { await evict(STORE_MAX_CHARS / 2); await write(); } catch { return; }
  }
  await evict(STORE_MAX_CHARS);
}

/** Drop the least recently used charts until the stored total is within `limit` characters. */
export async function evict(limit: number) {
  try {
    const db = await cacheDb();
    if (!db) return;
    const uses = await db.getAllFromIndex("klineUse", "usedAt");
    let total = uses.reduce((sum, use) => sum + use.size, 0);
    for (const use of uses) {
      if (total <= limit) break;
      const tx = db.transaction(["klines", "klineUse"], "readwrite");
      await Promise.all([tx.objectStore("klines").delete(use.key), tx.objectStore("klineUse").delete(use.key), tx.done]);
      memory.delete(use.key);
      total -= use.size;
    }
  } catch { /* nothing evicted */ }
}

// ---- public -------------------------------------------------------------------------------------------------

/** The last chart for this symbol and period held in this page, as a stale cached snapshot, or undefined. */
export function cachedMarket(runtime: string | undefined, symbol: string | undefined, duration: number): MarketWorkbenchSnapshot | undefined {
  if (!runtime || !symbol) return undefined;
  const record = memory.get(keyOf(runtime, symbol, duration));
  return record ? asStale(record) : undefined;
}

/** The same, from the browser's store when this page has not held it yet (opening the page). */
export async function loadCachedMarket(runtime: string | undefined, symbol: string | undefined, duration: number): Promise<MarketWorkbenchSnapshot | undefined> {
  if (!runtime || !symbol) return undefined;
  const record = await stored(keyOf(runtime, symbol, duration));
  return record ? asStale(record) : undefined;
}

/** Remember a live chart's closed bars (never the forming last one); stored when a bar closes, else at most every 15 s. */
export function rememberMarket(runtime: string | undefined, snapshot: MarketWorkbenchSnapshot | undefined, now = Date.now()) {
  if (!runtime || !snapshot || !["normal", "simulated"].includes(snapshot.dataStatus) || snapshot.isCached
      || !Array.isArray(snapshot.bars) || !snapshot.bars.length || typeof snapshot.quote?.symbol !== "string") return;
  const key = keyOf(runtime, snapshot.quote.symbol, snapshot.durationSeconds);
  const { bars, ...meta } = snapshot;
  const record: KlineRecord = { key, bars: bars.slice(0, -1), meta };
  hold(record);
  const tail = record.bars[record.bars.length - 1]?.datetime;
  const last = written.get(key);
  if (last && last.tail === tail && now - last.at < WRITE_EVERY_MS) return;
  written.set(key, { at: now, tail });
  void persist(record, now);
}

/**
 * Read a chart from the Runtime: only the bars after the held ones when the browser holds this chart, the
 * whole window otherwise or when the held bars fail the proof. The answer is remembered.
 */
export async function readMarketWorkbench(baseUrl: string, symbol: string, duration: MarketWorkbenchSnapshot["durationSeconds"],
  signal?: AbortSignal): Promise<MarketWorkbenchSnapshot> {
  const key = keyOf(baseUrl, symbol, duration);
  // A chart this page holds asks at once; only a first read waits for the browser's store.
  const held = memory.get(key) ?? await stored(key);
  // Retired while the store was read: no request for a chart nobody shows any more.
  if (signal?.aborted) throw new DOMException("The read was retired", "AbortError");
  const since = held?.bars[held.bars.length - 1]?.datetime;
  if (typeof since === "number") {
    const merged = mergeSince(held?.bars, await getMarketWorkbench(baseUrl, symbol, duration, signal, since));
    if (merged) { rememberMarket(baseUrl, merged); return merged; }
  }
  const whole = await getMarketWorkbench(baseUrl, symbol, duration, signal);
  rememberMarket(baseUrl, whole);
  return whole;
}

// ---- the push (2026-10-13): a feed focus carries `since` like the HTTP read ------------------------------------

/** Per session: the held chart its last focus offered as `since`. */
const focusHeld = new Map<string, { key: string; bars: readonly MarketKlineBar[] }>();
/** Charts whose held bars just failed the proof: their next focus asks for the whole window. */
const sinceRefused = new Set<string>();

/**
 * The `since` for a feed focus on this chart (the last closed bar held), remembering for the session which
 * bars it stands for; undefined when nothing is held or these bars just failed the proof.
 */
export async function feedFocusSince(runtime: string, sessionId: string, symbol: string, duration: number): Promise<number | undefined> {
  const key = keyOf(runtime, symbol, duration);
  const held = memory.get(key) ?? await stored(key);
  const since = held?.bars[held.bars.length - 1]?.datetime;
  if (sinceRefused.delete(key) || !held || typeof since !== "number") {
    focusHeld.delete(sessionId);
    return undefined;
  }
  focusHeld.set(sessionId, { key, bars: held.bars });
  return since;
}

/** A focus that went without `since` after a refused proof did not reach the Runtime: the next one again asks whole. */
export function refuseFeedSince(runtime: string, symbol: string, duration: number) {
  sinceRefused.add(keyOf(runtime, symbol, duration));
}

/**
 * A pushed feed.market frame as the whole chart. A frame that answers a focus's `since` (`barsAfter`) is
 * merged with the bars that focus offered; undefined when they fail the proof — the caller asks again and
 * that focus goes without `since`.
 */
export function feedMarketFrame<T extends MarketWorkbenchSnapshot>(sessionId: string, frame: T & { barsAfter?: number; barsWindow?: SinceWindow }): T | undefined {
  if (frame.barsAfter === undefined) return frame;
  const held = focusHeld.get(sessionId);
  const merged = mergeSince(held?.bars, frame);
  // Answered either way: the Runtime spends a focus's `since` on its first frame (#850), so the held
  // bars are not needed again.
  focusHeld.delete(sessionId);
  if (merged) return merged as T;
  if (held) sinceRefused.add(held.key);
  return undefined;
}

/**
 * The window now streams `sessionId` (or nothing): the bars other sessions' focuses offered are let go
 * (#850 — switching conversations kept every earlier session's chart array alive). A frame still on its
 * way for a dropped session fails the proof and reads the whole window, as any refused `since` does.
 */
export function keepFeedFocusOf(sessionId?: string) {
  for (const id of [...focusHeld.keys()]) if (id !== sessionId) focusHeld.delete(id);
}

/** Tests: whether a session still holds bars for its focus. */
export function feedFocusHeldForTests(sessionId: string): boolean {
  return focusHeld.has(sessionId);
}

/** Tests: forget what this page holds. */
export function resetMarketCacheForTests() {
  memory.clear(); written.clear(); touched.clear(); focusHeld.clear(); sinceRefused.clear();
}

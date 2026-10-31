import { openDB, deleteDB, type DBSchema, type IDBPDatabase } from "idb";
import type { MarketKlineBar, MarketWorkbenchSnapshot } from "./api";

/**
 * This browser's public-data cache (2026-10-12, 本地优先): one IndexedDB database (the `idb` wrapper) for the
 * charts' closed K-line bars (marketCache.ts) and the 快讯 rows with their cursor (flashCache.ts). Public market
 * and news data only, keyed by the Runtime address — never accounts, orders or anything private. A browser
 * without IndexedDB (a private window, a blocked or full store) answers `undefined` and every reader falls back
 * to reading the Runtime as before.
 */
export const CACHE_DB = "ai-trader-cache";

export interface KlineRecord {
  key: string;
  /** Closed bars only: the forming last bar of a packet is never stored. */
  bars: MarketKlineBar[];
  /** The packet without its bars, for drawing the chart at once (marked stale and cached when read back). */
  meta: Omit<MarketWorkbenchSnapshot, "bars">;
}
export interface KlineUse { key: string; size: number; usedAt: number }
/** `at`: the row's publication time (ms) — rows past the longest flash window go first. */
export interface FlashRowRecord { key: string; at: number; row: FlashRow }
export type FlashRow = { id: string; rev: number } & Record<string, unknown>;
export interface FlashCursorRecord { runtime: string; cursor: string }

interface CacheSchema extends DBSchema {
  klines: { key: string; value: KlineRecord };
  klineUse: { key: string; value: KlineUse; indexes: { usedAt: number } };
  flashRows: { key: string; value: FlashRowRecord; indexes: { at: number } };
  flashCursor: { key: string; value: FlashCursorRecord };
  instrumentNames: { key: string; value: { runtime: string; names: Record<string, string> } };
}
export type CacheDb = IDBPDatabase<CacheSchema>;

let opening: Promise<CacheDb | undefined> | undefined;
// An open that has not answered within OPEN_TIMEOUT_MS: readers go on without the cache until it does.
let stalled = false;
/** #932 P2: a stuck IndexedDB open (a wedged store, another tab mid-upgrade) left the first chart blank; past this
 *  the chart reads the Runtime as without a cache, and the open keeps going for later reads. */
export const OPEN_TIMEOUT_MS = 1500;

/** The database, or undefined when this browser cannot keep one (then nothing is cached) or its open has not
 *  answered within OPEN_TIMEOUT_MS. */
export function cacheDb(): Promise<CacheDb | undefined> {
  const pending = openCacheDb();
  if (stalled) return Promise.resolve(undefined);
  return new Promise(resolve => {
    const timer = setTimeout(() => { stalled = true; resolve(undefined); }, OPEN_TIMEOUT_MS);
    void pending.then(db => { clearTimeout(timer); resolve(db); });
  });
}

function openCacheDb(): Promise<CacheDb | undefined> {
  if (!opening) {
    stalled = false;
    const attempt = (async () => {
      if (typeof indexedDB === "undefined") return undefined;
      try {
        const db = await openDB<CacheSchema>(CACHE_DB, 2, {
          upgrade(database, oldVersion) {
            if (oldVersion < 1) {
              database.createObjectStore("klines", { keyPath: "key" });
              database.createObjectStore("klineUse", { keyPath: "key" }).createIndex("usedAt", "usedAt");
              database.createObjectStore("flashRows", { keyPath: "key" }).createIndex("at", "at");
              database.createObjectStore("flashCursor", { keyPath: "runtime" });
            }
            if (oldVersion < 2) database.createObjectStore("instrumentNames", { keyPath: "runtime" });
          },
          // Another tab upgrading or deleting the database: let it, and open again on the next read.
          blocking() { db.close(); opening = undefined; },
          terminated() { opening = undefined; }
        });
        return db;
      } catch {
        return undefined;
      }
    })();
    opening = attempt;
    // Answered at last (opened or failed): the next read waits on it again instead of skipping the cache.
    void attempt.then(() => { if (opening === attempt) stalled = false; });
  }
  return opening;
}

/** 清除本地缓存: drop the whole database (the next read starts empty). */
export async function clearCacheDb(): Promise<void> {
  const db = await opening?.catch(() => undefined);
  db?.close();
  opening = undefined;
  try { await deleteDB(CACHE_DB); } catch { /* nothing kept */ }
}

/** Tests: forget the open connection (fake-indexeddb is reset between tests). */
export function resetCacheDbForTests() {
  opening = undefined;
  stalled = false;
}

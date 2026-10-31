import { requestJson } from "./api";
import { cacheDb, type FlashRow } from "./localCache";

/**
 * 快讯游标增量 (2026-10-12): the 快讯 reads ask the Runtime for the compact feed (`since` = this browser's
 * cursor): every item comes as its own view fields plus `id` and `rev`, and only the rows changed after the
 * cursor (new items, a merged source, a translation…) come with their content, once, in `rows`. The rows are
 * kept here (IndexedDB, localCache.ts) by Runtime address, the counter's epoch and id; each item is rebuilt
 * from the row with exactly its `rev`, so a row changed while this browser looked elsewhere is never shown
 * stale — it is missing, and asked for by id (`include`). A Runtime without the cursor answers the full feed,
 * which is used as it is. Without IndexedDB the rows are kept for this page only.
 */
export const ROW_FIELDS = ["source", "sources", "publishedAt", "title", "text", "link", "products", "tags", "origTitle"] as const;
/** A merged event's copy of its headline report, which the compact feed names (`headline`) instead. */
const EVENT_HEADLINE_FIELDS = ["title", "text", "origTitle"] as const;
const INCLUDE_MAX = 100;
/** Rows published longer ago than this are past every flash window (at most 72 h) and leave the store. */
const KEEP_MS = 4 * 24 * 3600_000;
const KEEP_ROWS = 6000;

type Item = { id: string; rev?: number } & Record<string, unknown>;
interface CompactFeed {
  compact?: boolean; cursor?: string; rows?: FlashRow[];
  items?: Item[]; screen?: { dropped?: { items?: Item[] } } & Record<string, unknown>;
  events?: ({ items?: Item[]; headline?: string } & Record<string, unknown>)[];
}

const rows = new Map<string, FlashRow>();
const cursors = new Map<string, string>();
const loading = new Map<string, Promise<void>>();

const epochOf = (cursor: string) => cursor.split(":")[0];
const rowKey = (runtime: string, epoch: string, id: string) => `${runtime}|${epoch}|${id}`;

/**
 * Rebuild a compact feed from held rows: `lookup(id, rev)` answers the row with exactly that rev. Returns the
 * full feed (the compact markers removed) and the ids no held row could rebuild.
 */
export function expandFlash<T>(compact: CompactFeed, lookup: (id: string, rev: number) => FlashRow | undefined): { feed: T; missing: Set<string> } {
  const missing = new Set<string>();
  const rebuild = (item: Item): Item => {
    if (typeof item?.rev !== "number") return item;
    const row = lookup(item.id, item.rev);
    if (!row) { missing.add(item.id); return item; }
    const own = Object.fromEntries(Object.entries(item).filter(([key]) => key !== "rev"));
    return { ...Object.fromEntries(ROW_FIELDS.map(key => [key, row[key]])), ...own } as Item;
  };
  const { compact: _compact, cursor: _cursor, rows: _rows, ...feed } = compact;
  void _compact; void _cursor; void _rows;
  const result: CompactFeed = { ...feed };
  if (Array.isArray(compact.items)) result.items = compact.items.map(rebuild);
  const dropped = compact.screen?.dropped;
  if (compact.screen && dropped && Array.isArray(dropped.items)) result.screen = { ...compact.screen, dropped: { ...dropped, items: dropped.items.map(rebuild) } };
  if (Array.isArray(compact.events)) result.events = compact.events.map(event => {
    if (!Array.isArray(event.items)) return event;
    const { headline, ...own } = event;
    const rebuilt = { ...own, items: event.items.map(rebuild) };
    if (typeof headline !== "string") return rebuilt;
    // The event's title, text and origTitle are its headline report's (named, not repeated).
    const report = event.items.find(item => item.id === headline);
    const row = report && typeof report.rev === "number" ? lookup(headline, report.rev) : undefined;
    if (!row) { missing.add(headline); return rebuilt; }
    return { ...rebuilt, ...Object.fromEntries(EVENT_HEADLINE_FIELDS.map(key => [key, row[key]])) };
  });
  return { feed: result as T, missing };
}

async function load(runtime: string) {
  let pending = loading.get(runtime);
  if (!pending) {
    pending = (async () => {
      try {
        const db = await cacheDb();
        if (!db) return;
        const [kept, cursor] = await Promise.all([
          db.getAll("flashRows", IDBKeyRange.bound(`${runtime}|`, `${runtime}|￿`)), db.get("flashCursor", runtime)]);
        for (const record of kept) if (!rows.has(record.key)) rows.set(record.key, record.row);
        if (cursor && !cursors.has(runtime)) cursors.set(runtime, cursor.cursor);
      } catch { /* nothing kept: the first read brings every row */ }
    })();
    loading.set(runtime, pending);
  }
  await pending;
}

function keep(runtime: string, epoch: string, sent: FlashRow[], cursor: string) {
  for (const row of sent) rows.set(rowKey(runtime, epoch, row.id), row);
  const previous = cursors.get(runtime);
  // Concurrent reads may answer out of order: within one epoch the cursor only moves forward.
  const advanced = !previous || epochOf(previous) !== epoch || Number(previous.split(":")[1]) < Number(cursor.split(":")[1]);
  if (advanced) cursors.set(runtime, cursor);
  if (!advanced && !sent.length) return;
  const now = Date.now();
  void (async () => {
    try {
      const db = await cacheDb();
      if (!db) return;
      const tx = db.transaction(["flashRows", "flashCursor"], "readwrite");
      const store = tx.objectStore("flashRows");
      await Promise.all([
        ...sent.map(row => store.put({ key: rowKey(runtime, epoch, row.id), at: Date.parse(String(row.publishedAt)) || now, row })),
        tx.objectStore("flashCursor").put({ runtime, cursor: cursors.get(runtime) ?? cursor }),
        tx.done]);
      await prune(now);
    } catch { /* not kept: the next read asks for what is missing */ }
  })();
}

/** Drop rows past every window, then the oldest beyond KEEP_ROWS (this page's copy too). */
async function prune(now: number) {
  const db = await cacheDb();
  if (!db) return;
  const tx = db.transaction("flashRows", "readwrite");
  const index = tx.store.index("at");
  let count = await tx.store.count();
  let cursor = await index.openCursor();
  while (cursor && (cursor.value.at < now - KEEP_MS || count > KEEP_ROWS)) {
    rows.delete(cursor.value.key);
    await cursor.delete();
    count -= 1;
    cursor = await cursor.continue();
  }
  await tx.done;
}

/**
 * GET /api/research/flash with `query` (hours, product/products/tag, limit) through the cursor: the full feed
 * the screens use, rebuilt from this browser's rows plus the rows that changed.
 */
export async function readFlashFeed<T>(baseUrl: string, query: string, signal?: AbortSignal): Promise<T> {
  await load(baseUrl);
  const path = `${baseUrl}/api/research/flash?${query}`;
  const read = (since: string, include?: Iterable<string>) => requestJson<CompactFeed>(
    `${path}&since=${encodeURIComponent(since)}${include ? `&include=${encodeURIComponent([...include].join(","))}` : ""}`, { signal });
  let compact = await read(cursors.get(baseUrl) ?? "0");
  for (let attempt = 0; ; attempt += 1) {
    if (!compact.compact || typeof compact.cursor !== "string") return compact as T; // an older Runtime: the full feed
    const epoch = epochOf(compact.cursor);
    // Sent rows are right whatever else this answer lacks (each carries its rev): keep them and the cursor.
    keep(baseUrl, epoch, compact.rows ?? [], compact.cursor);
    const { feed, missing } = expandFlash<T>(compact, (id, rev) => {
      const row = rows.get(rowKey(baseUrl, epoch, id));
      return row?.rev === rev ? row : undefined;
    });
    if (!missing.size) return feed;
    // Rows this browser never held (another view's, evicted) or holds in an older rev: ask for them by id once,
    // then everything; a feed that still cannot be rebuilt is read whole.
    if (attempt === 0) compact = missing.size <= INCLUDE_MAX ? await read(compact.cursor, missing) : await read("0");
    else if (attempt === 1) compact = await read("0");
    else return requestJson<T>(path, { signal });
  }
}

/** Tests: forget this page's rows and cursors. */
export function resetFlashCacheForTests() {
  rows.clear(); cursors.clear(); loading.clear();
}

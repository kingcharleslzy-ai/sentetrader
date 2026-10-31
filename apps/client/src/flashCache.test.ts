import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { beforeEach, expect, test, vi } from "vitest";
import { requestJson } from "./api";
import { ROW_FIELDS, expandFlash, readFlashFeed, resetFlashCacheForTests } from "./flashCache";
import { resetCacheDbForTests } from "./localCache";

vi.mock("./api", () => ({ requestJson: vi.fn() }));
const request = vi.mocked(requestJson);

type Row = { id: string; rev: number } & Record<string, unknown>;
const PUBLISHED = new Date().toISOString();
const row = (id: string, rev: number, title = `标题${id}`): Row => ({ id, rev, source: "新浪", sources: ["新浪"], publishedAt: PUBLISHED,
  title, text: `正文${id}`, link: null, products: ["rb"], tags: [], origTitle: null });
const item = (value: Row, extra: Record<string, unknown> = {}) => ({ ...Object.fromEntries(ROW_FIELDS.map(key => [key, value[key]])), id: value.id, ...extra });

/** A fake Runtime with research_live.compact_flash's contract: a counter, rows by rev, `since` and `include`. */
function runtime(initial: Row[], epoch = "e1") {
  const state = { rows: initial, epoch, counter: Math.max(0, ...initial.map(value => value.rev)), urls: [] as string[], sentRows: 0 };
  request.mockImplementation(async (url: string) => {
    state.urls.push(url);
    const query = new URL(url).searchParams;
    const view = (value: Row) => ({ ai: { direction: "偏多" }, direct: true, ...(value.id === "a" ? { unjudged: undefined } : {}) });
    const full = { status: "ok", items: state.rows.map(value => item(value, view(value))),
      // A merged event: its title, text and origTitle are its first (headline) report's.
      events: [{ id: "event-1", title: state.rows[0].title, text: state.rows[0].text, origTitle: state.rows[0].origTitle, tier: "major",
        items: state.rows.slice(0, 2).map(value => item(value)) }],
      screen: { mode: "ai", pending: 0, dropped: { count: 1, items: [item(state.rows.at(-1)!, { ai: null, dropReason: "无关", droppedBy: "ai" })] } } };
    if (!query.has("since")) return full;
    const [sinceEpoch, number] = (query.get("since") ?? "").split(":");
    const known = sinceEpoch === state.epoch && /^\d+$/.test(number ?? "") ? Number(number) : -1;
    const include = new Set((query.get("include") ?? "").split(",").filter(Boolean));
    const sent = state.rows.filter(value => value.rev > known || include.has(value.id));
    state.sentRows += sent.length;
    const reduce = (value: Record<string, unknown>) => ({ ...Object.fromEntries(Object.entries(value).filter(([key]) => !(ROW_FIELDS as readonly string[]).includes(key))),
      rev: state.rows.find(entry => entry.id === value.id)!.rev });
    return { ...full, compact: true, cursor: `${state.epoch}:${state.counter}`, rows: sent,
      items: full.items.map(reduce),
      events: full.events.map(({ title: _title, text: _text, origTitle: _orig, ...event }) => ({ ...event, headline: event.items[0].id, items: event.items.map(reduce) })),
      screen: { ...full.screen, dropped: { ...full.screen.dropped, items: full.screen.dropped.items.map(reduce) } } };
  });
  const full = async () => {
    const saved = request.getMockImplementation()!;
    const answer = await saved(`https://rt/api/research/flash?hours=24`) as never;
    state.urls.pop();
    return answer;
  };
  return { state, full };
}
const settle = () => new Promise(resolve => setTimeout(resolve, 20));

beforeEach(() => {
  globalThis.indexedDB = new IDBFactory();
  resetCacheDbForTests();
  resetFlashCacheForTests();
  request.mockReset();
});

test("the cursor brings only new and changed rows, and the feed rebuilds exactly", async () => {
  const { state, full } = runtime([row("a", 1), row("b", 2), row("c", 3)]);
  expect(await readFlashFeed("https://rt", "hours=24")).toEqual(await full());
  expect(state.urls[0]).toContain("since=0");
  expect(state.sentRows).toBe(3);
  // Nothing changed: no row content travels.
  state.sentRows = 0;
  expect(await readFlashFeed("https://rt", "hours=24")).toEqual(await full());
  expect(state.urls.at(-1)).toContain(`since=${encodeURIComponent("e1:3")}`);
  expect(state.sentRows).toBe(0);
  // A translation changes row b in place and a new row d arrives: those two only — "changed", not just "new".
  state.counter = 5;
  state.rows = [row("d", 5), row("a", 1), row("b", 4, "译文标题"), row("c", 3)];
  const changed = await readFlashFeed<{ items: { id: string; title: string }[] }>("https://rt", "hours=24");
  expect(state.sentRows).toBe(2);
  expect(changed).toEqual(await full());
  expect(changed.items.find(value => value.id === "b")?.title).toBe("译文标题");
});

test("a row changed while this browser looked elsewhere is asked for by id, never shown stale", async () => {
  const { state, full } = runtime([row("a", 1), row("b", 2)]);
  await readFlashFeed("https://rt", "hours=24");
  // Another view's read moved the cursor past b's change without carrying b.
  state.counter = 9;
  await readFlashFeed("https://rt", "tag=macro").catch(() => undefined);
  state.rows = [row("a", 1), row("b", 7, "改过的")];
  state.urls.length = 0;
  const feed = await readFlashFeed<{ items: { title: string }[] }>("https://rt", "hours=24");
  expect(feed).toEqual(await full());
  expect(feed.items[1].title).toBe("改过的");
  expect(state.urls).toHaveLength(2);
  expect(state.urls[1]).toContain("include=b");
});

test("the rows outlive the page: a reload reads only what changed, a rebuilt table (another epoch) everything", async () => {
  const { state, full } = runtime([row("a", 1), row("b", 2)]);
  await readFlashFeed("https://rt", "hours=24");
  await settle();
  resetFlashCacheForTests(); // a new page: rows and cursor come back from IndexedDB
  state.sentRows = 0;
  expect(await readFlashFeed("https://rt", "hours=24")).toEqual(await full());
  expect(state.sentRows).toBe(0);
  state.epoch = "e2";
  expect(await readFlashFeed("https://rt", "hours=24")).toEqual(await full());
  expect(state.sentRows).toBe(2);
});

test("without IndexedDB, or against a Runtime without the cursor, the feed still reads", async () => {
  globalThis.indexedDB = undefined as never;
  resetCacheDbForTests();
  const { state, full } = runtime([row("a", 1)]);
  expect(await readFlashFeed("https://rt", "hours=24")).toEqual(await full());
  state.sentRows = 0;
  await readFlashFeed("https://rt", "hours=24");
  expect(state.sentRows).toBe(0); // this page still holds the rows
  request.mockResolvedValueOnce({ status: "ok", items: [{ id: "x", title: "旧版 Runtime" }] } as never);
  expect(await readFlashFeed("https://old-rt", "hours=24")).toEqual({ status: "ok", items: [{ id: "x", title: "旧版 Runtime" }] });
});

test("expandFlash reports what it cannot rebuild and leaves full items alone", () => {
  const { feed, missing } = expandFlash<{ items: unknown[] }>({ compact: true, cursor: "e:1", rows: [],
    items: [{ id: "a", rev: 2 }, { id: "b", title: "完整" }] }, () => undefined);
  expect([...missing]).toEqual(["a"]);
  expect(feed.items[1]).toEqual({ id: "b", title: "完整" });
  expect(feed).not.toHaveProperty("cursor");
});

import { afterEach, expect, test, vi } from "vitest";

// A wedged IndexedDB: the open never answers until the test lets it.
let finish: ((db: unknown) => void) | undefined;
vi.mock("idb", () => ({
  openDB: vi.fn(() => new Promise(resolve => { finish = resolve; })),
  deleteDB: vi.fn(async () => undefined),
}));

import { OPEN_TIMEOUT_MS, cacheDb, resetCacheDbForTests } from "./localCache";

afterEach(() => { vi.useRealTimers(); resetCacheDbForTests(); finish = undefined; });

test("a cache open that hangs gives up after 1.5 s and the chart reads on without it (#932 P2)", async () => {
  vi.stubGlobal("indexedDB", {});
  vi.useFakeTimers();
  const first = cacheDb();
  await vi.advanceTimersByTimeAsync(OPEN_TIMEOUT_MS);
  await expect(first).resolves.toBeUndefined();
  // While it still hangs, later reads do not wait another 1.5 s each.
  await expect(cacheDb()).resolves.toBeUndefined();
  // Once it opens, reads use it again.
  const db = { name: "ai-trader-cache" };
  finish?.(db);
  await vi.advanceTimersByTimeAsync(0);
  await expect(cacheDb()).resolves.toBe(db);
  vi.unstubAllGlobals();
});

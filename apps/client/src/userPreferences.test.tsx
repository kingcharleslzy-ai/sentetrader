import { act, renderHook, waitFor } from "@testing-library/react";
import { DEFAULT_PREFERENCES, preferencePatch, preferenceValue, useUserPreferences } from "./userPreferences";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

test("dotted keys become partial documents and read back", () => {
  expect(preferencePatch("notify.desktop.fills", false)).toEqual({ notify: { desktop: { fills: false } } });
  expect(preferenceValue(DEFAULT_PREFERENCES, "chart.kdParams.k")).toBe(9);
  expect(preferenceValue(DEFAULT_PREFERENCES, "chart.nothing.here")).toBeUndefined();
});

test("the hook reads the document on connect, writes with the version lock and serialises writes", async () => {
  let version = 3;
  const calls: { url: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, body: init?.body ? String(init.body) : undefined });
    if (init?.method === "PUT") {
      version += 1;
      return json({ version, preferences: { ...DEFAULT_PREFERENCES, trade: { ...DEFAULT_PREFERENCES.trade, defaultVolume: JSON.parse(String(init.body)).trade.defaultVolume } } });
    }
    return json({ version, preferences: DEFAULT_PREFERENCES });
  }));
  const { result } = renderHook(() => useUserPreferences("http://runtime"));
  expect(result.current.preferences).toBe(DEFAULT_PREFERENCES);  // defaults bridge the read
  await waitFor(() => expect(result.current.document?.version).toBe(3));
  await act(async () => { await Promise.all([result.current.set("trade.defaultVolume", 2), result.current.set("trade.defaultVolume", 5)]); });
  const puts = calls.filter(call => call.body);
  expect(puts.map(call => call.url)).toEqual(["http://runtime/api/preferences?version=3", "http://runtime/api/preferences?version=4"]);
  expect(result.current.document?.version).toBe(5);
  expect(result.current.preferences.trade.defaultVolume).toBe(5);
  expect(result.current.status).toBe("已保存");
  vi.unstubAllGlobals();
});

test("refresh re-reads a document the AI changed on the Runtime (update_preferences)", async () => {
  let pins = ["strategy-signals"];
  vi.stubGlobal("fetch", vi.fn(async () => json({ version: pins.length, preferences: { ...DEFAULT_PREFERENCES, ui: { quickSkills: pins } } })));
  const { result } = renderHook(() => useUserPreferences("http://runtime"));
  await waitFor(() => expect(result.current.preferences.ui?.quickSkills).toEqual(["strategy-signals"]));
  pins = ["strategy-signals", "review-summary"];
  act(() => { result.current.refresh(); });
  await waitFor(() => expect(result.current.preferences.ui?.quickSkills).toEqual(["strategy-signals", "review-summary"]));
  vi.unstubAllGlobals();
});

test("a 409 re-reads the document and reports the conflict; other errors show the Runtime's detail", async () => {
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "PUT") return url.includes("version=1") ? json({ detail: "preferences_version_conflict" }, 409) : json({ detail: [{ msg: "endpointStallSeconds ≤ 3.0" }] }, 422);
    reads += 1;
    return json({ version: reads, preferences: DEFAULT_PREFERENCES });
  }));
  const { result } = renderHook(() => useUserPreferences("http://runtime"));
  await waitFor(() => expect(result.current.document?.version).toBe(1));
  await act(async () => { await result.current.set("voice.speed", "slow").catch(() => undefined); });
  expect(result.current.status).toBe("设置已在别处修改，已刷新，请再改一次");
  expect(result.current.document?.version).toBe(2);
  await act(async () => { await result.current.set("voice.endpointStallSeconds", 9).catch(() => undefined); });
  expect(result.current.status).toBe("endpointStallSeconds ≤ 3.0");
  vi.unstubAllGlobals();
});

test("disconnected: no read, and a write is refused", async () => {
  vi.stubGlobal("fetch", vi.fn());
  const { result } = renderHook(() => useUserPreferences(undefined));
  await expect(result.current.set("voice.speed", "slow")).rejects.toThrow("连接后可设置");
  expect(fetch).not.toHaveBeenCalled();
  vi.unstubAllGlobals();
});

test("a function patch is computed from the latest document when its queued write runs", async () => {
  let version = 1;
  const bodies: string[] = [];
  let puts = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      puts += 1;
      bodies.push(String(init.body));
      version += 1;
      const patch = JSON.parse(String(init.body));
      const response = json({ version, preferences: { ...DEFAULT_PREFERENCES, ...patch } });
      // 第一个保存 50 ms 后才回来：第二个补丁排队时拿不到它的结果。
      if (puts === 1) return new Promise(resolve => setTimeout(() => resolve(response), 50));
      return response;
    }
    return json({ version, preferences: DEFAULT_PREFERENCES });
  }));
  const { result } = renderHook(() => useUserPreferences("http://runtime"));
  await waitFor(() => expect(result.current.document?.version).toBe(1));
  const star = (product: string) => (latest: { preferences: { watchlist?: { products?: string[] } } } | undefined) =>
    ({ watchlist: { products: [...(latest?.preferences.watchlist?.products ?? []), product] } });
  const first = result.current.update(star("SHFE.rb"));
  const second = result.current.update(star("DCE.i"));
  await act(async () => { await Promise.all([first, second]); });
  // 两个 PUT 体是 [rb] 然后 [rb, i]——第二下叠在第一下实际保存的结果上，不丢星。
  expect(bodies.map(body => JSON.parse(body))).toEqual([
    { watchlist: { products: ["SHFE.rb"] } },
    { watchlist: { products: ["SHFE.rb", "DCE.i"] } },
  ]);
  expect(result.current.document?.preferences.watchlist?.products).toEqual(["SHFE.rb", "DCE.i"]);
  vi.unstubAllGlobals();
});

test("returning to the foreground re-reads, throttled to 10 s and skipped while a write is queued", async () => {
  let reads = 0;
  let releasePut: (() => void) | undefined;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      return new Promise(resolve => { releasePut = () => resolve(json({ version: 150, preferences: DEFAULT_PREFERENCES })); });
    }
    reads += 1;
    return json({ version: reads * 100, preferences: DEFAULT_PREFERENCES });
  }));
  const { result } = renderHook(() => useUserPreferences("http://runtime"));
  await waitFor(() => expect(result.current.document?.version).toBe(100));
  // 写入还排着队：回到前台不读（读到旧版本会把队列里的写入顶成 409）。
  const write = result.current.update({ chart: { candleStyle: "ohlc" } });
  act(() => { window.dispatchEvent(new Event("focus")); });
  expect(reads).toBe(1);
  await waitFor(() => expect(releasePut).toBeDefined());  // the queued write reached the wire
  act(() => { releasePut!(); });
  await act(async () => { await write; });
  // 队列空了：回到前台重读一次（AI 在服务端改的偏好回到窗口）。
  act(() => { window.dispatchEvent(new Event("focus")); });
  // 等重读的结果落到文档上，而不只是请求发出（响应体解析慢时断言会读到 PUT 的 150）。
  await waitFor(() => expect(result.current.document?.version).toBe(200));
  expect(reads).toBe(2);
  // 10 秒内的第二次回前台不再读。
  act(() => { window.document.dispatchEvent(new Event("visibilitychange")); });
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(reads).toBe(2);
  expect(result.current.document?.version).toBe(200);
  vi.unstubAllGlobals();
});

test("a read still in flight when a save lands cannot roll the document back to an older version", async () => {
  let reads = 0;
  let releaseStaleRead: () => void = () => undefined;
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "PUT") return json({ version: 2, preferences: { ...DEFAULT_PREFERENCES, trade: { ...DEFAULT_PREFERENCES.trade, defaultVolume: 4 } } });
    reads += 1;
    if (reads === 1) return json({ version: 1, preferences: DEFAULT_PREFERENCES });
    await new Promise<void>(resolve => { releaseStaleRead = resolve; });
    return json({ version: 1, preferences: DEFAULT_PREFERENCES });
  }));
  const { result } = renderHook(() => useUserPreferences("http://runtime"));
  await waitFor(() => expect(result.current.document?.version).toBe(1));
  act(() => { result.current.refresh(); });
  await waitFor(() => expect(reads).toBe(2));
  await act(async () => { await result.current.set("trade.defaultVolume", 4); });
  expect(result.current.document?.version).toBe(2);
  await act(async () => { releaseStaleRead(); await new Promise(resolve => setTimeout(resolve, 0)); });
  expect(result.current.document?.version).toBe(2);
  expect(result.current.preferences.trade.defaultVolume).toBe(4);
  vi.unstubAllGlobals();
});

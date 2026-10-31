import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { UiEvent } from "@ai-trader/ui-events";
import { applyFeedEvent, resetFeed } from "./runtimeFeed";
import { groupReminders, reminderState, sessionGroup, tradingDayLabel, useReminders, type ReminderItem } from "./reminders";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
// Exchange time (UTC+8) as an ISO instant.
const at = (day: string, clock: string) => new Date(`${day}T${clock}:00+08:00`).toISOString();
const item = (id: string, createdAt: string, over: Partial<ReminderItem> = {}): ReminderItem => ({
  id, kind: "market_close", tradingDay: "2026-10-15", createdAt, title: "收盘提醒", body: "两笔持仓要过夜", read: false, ...over });

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); resetFeed(); });

it("groups a trading day by session, newest first: 夜盘 by its evening (after midnight too), 盘前, 上午, 下午", () => {
  expect(sessionGroup(at("2026-10-14", "21:05")).label).toBe("夜盘 · 10-14 晚");
  expect(sessionGroup(at("2026-10-15", "01:30")).label).toBe("夜盘 · 10-14 晚");
  expect(sessionGroup(at("2026-10-15", "08:45")).label).toBe("盘前");
  expect(sessionGroup(at("2026-10-15", "11:12")).label).toBe("日盘 · 上午");
  expect(sessionGroup(at("2026-10-15", "14:50")).label).toBe("日盘 · 下午");
  const groups = groupReminders([item("n", at("2026-10-14", "22:31")), item("a", at("2026-10-15", "10:16")),
    item("p", at("2026-10-15", "14:50")), item("p2", at("2026-10-15", "13:42"))], "session");
  expect(groups.map(group => [group.label, group.items.map(entry => entry.id)])).toEqual([
    ["日盘 · 下午", ["p", "p2"]], ["日盘 · 上午", ["a"]], ["夜盘 · 10-14 晚", ["n"]]]);
  expect(groupReminders([item("x", at("2026-10-14", "15:15"), { tradingDay: "2026-10-14" })], "day")[0].label).toBe("10月14日 交易日");
  expect(tradingDayLabel(null)).toBe("今天");
  expect(reminderState("仍在排队 0/1")).toEqual({ text: "仍在排队 0/1" });
  expect(reminderState({ text: "平仓单 已成交 771.5 ×1", tone: "ok" })).toEqual({ text: "平仓单 已成交 771.5 ×1", tone: "ok" });
  expect(reminderState("")).toBeNull();
});

it("reads the day, marks read on the Runtime (optimistic), reads earlier pages and re-reads on feed.reminder", async () => {
  const calls: { url: string; method?: string; body?: string }[] = [];
  let unread = 2;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, method: init?.method, body: init?.body ? String(init.body) : undefined });
    if (url.endsWith("/api/reminders/read")) { unread = JSON.parse(String(init?.body)).all ? 0 : 1; return json({ unread }); }
    if (url.includes("scope=earlier")) return json({ items: [item("e1", at("2026-10-14", "15:15"), { tradingDay: "2026-10-14", read: true })], unread, tradingDay: "2026-10-15", nextCursor: null });
    return json({ items: [item("r1", at("2026-10-15", "14:50")), item("r2", at("2026-10-15", "14:31"), { kind: "breakeven" })], unread, tradingDay: "2026-10-15", nextCursor: null });
  }));
  const { result } = renderHook(() => useReminders("http://runtime", true));
  await waitFor(() => expect(result.current.today).toHaveLength(2));
  expect(result.current.unread).toBe(2);
  expect(result.current.tradingDay).toBe("2026-10-15");
  expect(result.current.hasMoreEarlier).toBe(true);

  act(() => result.current.markRead(["r1"]));
  expect(result.current.today.find(entry => entry.id === "r1")?.read).toBe(true);
  expect(result.current.unread).toBe(1);
  await waitFor(() => expect(calls.some(call => call.url.endsWith("/api/reminders/read"))).toBe(true));
  expect(JSON.parse(calls.find(call => call.url.endsWith("/api/reminders/read"))!.body!)).toEqual({ ids: ["r1"] });
  // An already-read row posts nothing.
  const posts = calls.length;
  act(() => result.current.markRead(["r1"]));
  expect(calls).toHaveLength(posts);

  await act(async () => { await result.current.loadEarlier(); });
  expect(result.current.earlier.map(entry => entry.id)).toEqual(["e1"]);
  expect(result.current.hasMoreEarlier).toBe(false);

  act(() => result.current.markAllRead());
  expect(result.current.unread).toBe(0);
  await waitFor(() => expect(JSON.parse(calls.at(-1)!.body ?? "null")).toEqual({ all: true }));

  const reads = calls.filter(call => call.url.includes("scope=today")).length;
  act(() => {
    applyFeedEvent("s", { schemaVersion: 1, id: "rm", sessionId: "s", kind: "result", name: "feed.reminder",
      timestamp: "now", payload: { id: "r3", type: "new", kind: "fill", title: "成交", text: "买开 1 手" } } as unknown as UiEvent, 1000);
  });
  await waitFor(() => expect(calls.filter(call => call.url.includes("scope=today")).length).toBe(reads + 1));
});

it("reads every page of today, not only the first 50 (#932 P2: the rest had no way in)", async () => {
  const urls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    urls.push(url);
    if (!url.includes("before=")) return json({ items: [item("p1", at("2026-10-15", "14:50"))], unread: 2, tradingDay: "2026-10-15", nextCursor: "c1" });
    return json({ items: [item("p2", at("2026-10-15", "09:10"))], unread: 2, tradingDay: "2026-10-15", nextCursor: null });
  }));
  const { result } = renderHook(() => useReminders("http://runtime", true));
  await waitFor(() => expect(result.current.today.map(entry => entry.id)).toEqual(["p1", "p2"]));
  expect(urls.filter(url => url.includes("scope=today"))).toHaveLength(2);
  expect(urls[1]).toContain("before=c1");
});

/** A Runtime holding `total` reminders today, paged 50 at a time like GET /api/reminders (cursor = the row index). */
function pagedRuntime(total: number) {
  const urls: string[] = [];
  const rows = Array.from({ length: total }, (_, index) => item(`r${index}`, new Date(Date.parse(at("2026-10-15", "14:50")) - index * 1000).toISOString()));
  const fetch = vi.fn(async (url: string) => {
    urls.push(url);
    const start = Number(new URL(url).searchParams.get("before") ?? 0);
    return json({ items: rows.slice(start, start + 50), unread: total, tradingDay: "2026-10-15", nextCursor: start + 50 < total ? String(start + 50) : null });
  });
  return { fetch, urls };
}

it.each([[0, 1, 0, false], [50, 1, 50, false], [51, 2, 51, false], [501, 10, 500, true]])(
  "a day of %i reminders: %i reads, %i shown, more to read %s (#932 R5: past 10 pages it says so and reads on)",
  async (total, reads, shown, more) => {
    const runtime = pagedRuntime(total);
    vi.stubGlobal("fetch", runtime.fetch);
    const { result } = renderHook(() => useReminders("http://runtime", true));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.today).toHaveLength(shown);
    expect(runtime.urls).toHaveLength(reads);
    expect(result.current.hasMoreToday).toBe(more);
    if (!more) return;
    await act(async () => { await result.current.loadMoreToday(); });
    expect(result.current.today).toHaveLength(total);
    expect(new Set(result.current.today.map(entry => entry.id)).size).toBe(total);
    expect(runtime.urls.at(-1)).toContain("before=500");
    expect(result.current.hasMoreToday).toBe(false);
  });

it("a failed 继续加载当天提醒 keeps the way on and says why; the retry reads on (Codex r4307841904)", async () => {
  const runtime = pagedRuntime(501);
  let fail = false;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => fail ? json({ detail: "busy" }, 503) : runtime.fetch(url)));
  const { result } = renderHook(() => useReminders("http://runtime", true));
  await waitFor(() => expect(result.current.today).toHaveLength(500));
  fail = true;
  await act(async () => { await result.current.loadMoreToday(); });
  expect(result.current.moreError).toBe("提醒读取失败，稍后会再试");
  expect(result.current.hasMoreToday).toBe(true);
  expect(result.current.status).toBe("ready");
  fail = false;
  await act(async () => { await result.current.loadMoreToday(); });
  expect(result.current.moreError).toBe("");
  expect(result.current.today).toHaveLength(501);
});

it("继续加载 waits behind a refresh and never reads on from a cursor the refresh moved (Codex r4308006068)", async () => {
  let total = 501;
  const urls: string[] = [];
  const held: (() => void)[] = [];
  let hold = false;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    urls.push(url);
    if (hold && !url.includes("before=")) await new Promise<void>(resolve => { held.push(resolve); });
    // Newest first; the cursor names the last row of a page (like the Runtime's created_at|id), so a new
    // reminder at the top moves every page boundary.
    const rows = Array.from({ length: total }, (_, index) => item(`r${total - 1 - index}`, new Date(Date.parse(at("2026-10-15", "14:50")) - index * 1000).toISOString()));
    const before = new URL(url).searchParams.get("before");
    const start = before ? rows.findIndex(row => row.id === before) + 1 : 0;
    const page = rows.slice(start, start + 50);
    return json({ items: page, unread: total, tradingDay: "2026-10-15", nextCursor: start + 50 < total ? page.at(-1)!.id : null });
  }));
  const { result } = renderHook(() => useReminders("http://runtime", true));
  await waitFor(() => expect(result.current.today).toHaveLength(500));
  // A new reminder: a refresh starts (and is held), then 继续加载 is clicked on the old cursor.
  total = 502; hold = true;
  act(() => result.current.refresh());
  await waitFor(() => expect(held).toHaveLength(1));
  let outcome: Promise<string> | undefined;
  act(() => { outcome = result.current.loadMoreToday(); });
  hold = false;
  await act(async () => { held[0](); await outcome; });
  expect(await outcome).toBe("当天提醒刚刷新过，再点一次接着读");
  expect(urls.filter(url => url.includes("before=r1&") || url.endsWith("before=r1"))).toHaveLength(0);   // never from the moved cursor
  await waitFor(() => expect(result.current.hasMoreToday).toBe(true));
  await act(async () => { expect(await result.current.loadMoreToday()).toBe(""); });
  expect(new Set(result.current.today.map(entry => entry.id)).size).toBe(502);
  expect(await act(async () => result.current.loadMoreToday())).toBe("当天的提醒已经全部读出，没有更多可加载");
});

it("a new trading day starts 查看更早 over: yesterday's list becomes history with a way in (#932 R6)", async () => {
  let day = "2026-10-15";
  const yesterday = item("y1", at("2026-10-15", "14:50"));
  const older = item("e1", at("2026-10-14", "15:15"), { tradingDay: "2026-10-14", read: true });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.includes("scope=earlier")) return json({ items: day === "2026-10-15" ? [older] : [yesterday, older], unread: 0, tradingDay: day, nextCursor: null });
    return json({ items: day === "2026-10-15" ? [yesterday] : [], unread: 1, tradingDay: day, nextCursor: null });
  }));
  const { result } = renderHook(() => useReminders("http://runtime", true));
  await waitFor(() => expect(result.current.today).toHaveLength(1));
  await act(async () => { await result.current.loadEarlier(); });
  expect(result.current.hasMoreEarlier).toBe(false);
  day = "2026-10-16";
  act(() => result.current.refresh());
  await waitFor(() => expect(result.current.tradingDay).toBe("2026-10-16"));
  expect(result.current.today).toEqual([]);
  expect(result.current.earlier).toEqual([]);
  expect(result.current.earlierShown).toBe(false);
  expect(result.current.hasMoreEarlier).toBe(true);
  await act(async () => { await result.current.loadEarlier(); });
  expect(result.current.earlier.map(entry => entry.id)).toEqual(["y1", "e1"]);
});

it("an older answer never overwrites a newer unread count: 标读, 全部已读 and 更早 answered out of order (#932 R6)", async () => {
  const calls: { url: string; body?: string; resolve: (response: Response) => void }[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => new Promise<Response>(resolve => {
    calls.push({ url, body: init?.body ? String(init.body) : undefined, resolve });
  })));
  const posts = () => calls.filter(call => call.url.endsWith("/api/reminders/read"));
  const { result } = renderHook(() => useReminders("http://runtime", true));
  await waitFor(() => expect(calls).toHaveLength(1));
  await act(async () => { calls[0].resolve(json({ items: [item("r1", at("2026-10-15", "14:50")), item("r2", at("2026-10-15", "14:40")), item("r3", at("2026-10-15", "14:30"))], unread: 3, tradingDay: "2026-10-15", nextCursor: null })); });
  await waitFor(() => expect(result.current.unread).toBe(3));

  act(() => result.current.markRead(["r1"]));
  expect(result.current.unread).toBe(2);
  let earlier: Promise<void> | undefined;
  act(() => { earlier = result.current.loadEarlier(); });
  act(() => result.current.markAllRead());
  expect(result.current.unread).toBe(0);
  await waitFor(() => expect(posts()).toHaveLength(1));
  // The marks go one after another, and the 更早 read waits for the mark already sent.
  expect(calls.some(call => call.url.includes("scope=earlier"))).toBe(false);

  await act(async () => { posts()[0].resolve(json({ unread: 2 })); });   // 标读's answer, overtaken by 全部已读
  expect(result.current.unread).toBe(0);
  await waitFor(() => expect(posts()).toHaveLength(2));
  await waitFor(() => expect(calls.some(call => call.url.includes("scope=earlier"))).toBe(true));
  const earlierCall = calls.find(call => call.url.includes("scope=earlier"))!;
  await act(async () => {
    earlierCall.resolve(json({ items: [item("e1", at("2026-10-14", "15:15"), { tradingDay: "2026-10-14" })], unread: 2, tradingDay: "2026-10-15", nextCursor: null }));
    await earlier;
  });
  expect(result.current.unread).toBe(0);
  // A row the older read returned keeps the 全部已读 made after it started.
  expect(result.current.earlier.map(entry => [entry.id, entry.read])).toEqual([["e1", true]]);
  await act(async () => { posts()[1].resolve(json({ unread: 0 })); });
  expect(JSON.parse(posts()[1].body!)).toEqual({ all: true });
  expect(result.current.unread).toBe(0);
});

it("a mark stalled when the Runtime drops never holds the reads after reconnecting, and its late answer lands nowhere (Codex r4307899295)", async () => {
  let stalled: ((response: Response) => void) | undefined;
  let unread = 2;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (url.endsWith("/api/reminders/read")) return new Promise<Response>(resolve => { stalled = resolve; });
    return Promise.resolve(json({ items: [item("r1", at("2026-10-15", "14:50")), item("r2", at("2026-10-15", "14:40"))], unread, tradingDay: "2026-10-15", nextCursor: null }));
  }));
  const view = renderHook(({ url }: { url: string | undefined }) => useReminders(url, true), { initialProps: { url: "http://runtime" as string | undefined } });
  await waitFor(() => expect(view.result.current.unread).toBe(2));
  act(() => view.result.current.markRead(["r1"]));
  await waitFor(() => expect(stalled).toBeDefined());
  view.rerender({ url: undefined });                 // disconnected with the mark still on its way
  unread = 5;
  view.rerender({ url: "http://runtime" });
  await waitFor(() => expect(view.result.current.unread).toBe(5));
  expect(view.result.current.status).toBe("ready");
  await act(async () => { stalled!(json({ unread: 1 })); });
  expect(view.result.current.unread).toBe(5);
});

it("a guest or a disconnected window has no reminders and reads nothing; an older Runtime says so", async () => {
  const fetch = vi.fn(async () => json({ detail: "Not Found" }, 404));
  vi.stubGlobal("fetch", fetch);
  const off = renderHook(() => useReminders(undefined, true));
  expect(off.result.current.available).toBe(false);
  expect(fetch).not.toHaveBeenCalled();
  const { result } = renderHook(() => useReminders("http://runtime", true));
  await waitFor(() => expect(result.current.status).toBe("error"));
  expect(result.current.error).toBe("服务端还没有提醒记录");
  expect(result.current.unread).toBe(0);
});

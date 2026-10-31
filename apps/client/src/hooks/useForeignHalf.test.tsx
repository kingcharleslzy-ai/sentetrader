import { act, renderHook } from "@testing-library/react";
import type { AccountOverviewSnapshot, ForeignHalf, SimulationOrder } from "../api";
import type { RuntimeFeedState } from "../runtimeFeed";
import { useForeignHalf } from "./useForeignHalf";
import type { WorkspaceHandle } from "./workspaceHandle";

const getForeignHalf = vi.hoisted(() => vi.fn());
vi.mock("../api", async importOriginal => ({ ...await importOriginal<typeof import("../api")>(), getForeignHalf }));

const handle = { baseUrl: "https://runtime.test", connection: "connected", replayPath: "" } as WorkspaceHandle;
const half = (accountId = "USD", busy = false) => ({ accountId, account: { accountName: accountId },
  positions: busy ? [{ symbol: "CME.GCEZ26" }] : [], orders: [] }) as unknown as ForeignHalf;
const live = (foreign = half()): RuntimeFeedState => ({
  channels: { account: { status: "ok", receivedAt: Date.now() } },
  account: { foreign } as AccountOverviewSnapshot,
});

beforeEach(() => {
  vi.useFakeTimers();
  getForeignHalf.mockReset().mockResolvedValue(half());
});
afterEach(() => vi.useRealTimers());

test("matching live account push supplies USD rows without polling; explicit refresh still reads once", async () => {
  const feed = live();
  const hook = renderHook(({ feed }) => useForeignHalf({ handle, foreignAccountId: "USD", feed }),
    { initialProps: { feed } });
  expect(hook.result.current.foreignHalf).toBe(feed.account?.foreign);
  for (let i = 0; i < 6; i++) {
    hook.rerender({ feed: live() });
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
  }
  expect(getForeignHalf).not.toHaveBeenCalled();
  const refreshed = half();
  getForeignHalf.mockResolvedValue(refreshed);
  await act(async () => { hook.result.current.refreshForeignHalf(); });
  expect(getForeignHalf).toHaveBeenCalledTimes(1);
  expect(hook.result.current.foreignHalf).toBe(refreshed);
  hook.unmount();
});

test.each([false, true])("unavailable account push resumes the existing idle/busy polling (busy=%s)", async busy => {
  getForeignHalf.mockResolvedValue(half("USD", busy));
  const hook = renderHook(({ feed }) => useForeignHalf({ handle, foreignAccountId: "USD", feed }),
    { initialProps: { feed: live(half("USD", busy)) } });
  hook.rerender({ feed: { channels: { account: { status: "unavailable", receivedAt: Date.now() } } } });
  await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
  expect(getForeignHalf).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(busy ? 5_000 : 15_000); });
  expect(getForeignHalf).toHaveBeenCalledTimes(2);
  hook.rerender({ feed: live(half("USD", busy)) });
  for (let i = 0; i < 3; i++) {
    hook.rerender({ feed: live(half("USD", busy)) });
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
  }
  expect(getForeignHalf).toHaveBeenCalledTimes(2);
  hook.unmount();
});

test("a different USD id is never displayed; stale requests cannot overwrite an account switch", async () => {
  let finish: (value: ForeignHalf) => void = () => undefined;
  getForeignHalf.mockImplementationOnce(() => new Promise<ForeignHalf>(resolve => { finish = resolve; }));
  const hook = renderHook(({ foreignAccountId, feed }) => useForeignHalf({ handle, foreignAccountId, feed }),
    { initialProps: { foreignAccountId: "USD", feed: live(half("OTHER")) } });
  expect(getForeignHalf).toHaveBeenCalledWith(handle.baseUrl, "USD", expect.any(AbortSignal));
  expect(hook.result.current.foreignHalf).toBeUndefined();
  const switched = half("NEW");
  hook.rerender({ foreignAccountId: "NEW", feed: live(switched) });
  await act(async () => { finish(half()); });
  expect(hook.result.current.foreignHalf).toBe(switched);
  expect(getForeignHalf).toHaveBeenCalledTimes(1);
  hook.unmount();
});

test("a stale feed falls back, and replay or missing sub-account reads nothing", async () => {
  const feed = live();
  feed.channels.account!.receivedAt -= 10_001;
  const hook = renderHook(({ h, id }) => useForeignHalf({ handle: h, foreignAccountId: id, feed }),
    { initialProps: { h: handle, id: "USD" as string | undefined } });
  await act(async () => { await Promise.resolve(); });
  expect(getForeignHalf).toHaveBeenCalledTimes(1);
  hook.rerender({ h: { ...handle, replayPath: "/replay" }, id: "USD" });
  expect(hook.result.current.foreignHalf).toBeUndefined();
  hook.rerender({ h: handle, id: undefined });
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(getForeignHalf).toHaveBeenCalledTimes(1);
  hook.unmount();
});

test("a delayed startup read cannot overwrite push or prevent later fallback polling", async () => {
  let finish: (value: ForeignHalf) => void = () => undefined;
  getForeignHalf.mockImplementationOnce(() => new Promise<ForeignHalf>(resolve => { finish = resolve; }));
  const hook = renderHook(({ feed }) => useForeignHalf({ handle, foreignAccountId: "USD", feed }),
    { initialProps: { feed: { channels: {} } as RuntimeFeedState } });
  const pushed = half();
  hook.rerender({ feed: live(pushed) });
  await act(async () => { finish(half()); });
  expect(hook.result.current.foreignHalf).toBe(pushed);
  hook.rerender({ feed: { channels: {} } });
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
  expect(getForeignHalf).toHaveBeenCalledTimes(2);
  hook.unmount();
});

test("a cached push cannot undo the explicit post-cancel read", async () => {
  const working = { clientOrderId: "one", factRevision: 1, remoteStatus: "ALIVE" } as SimulationOrder;
  const cancelled = { ...working, factRevision: 2, remoteStatus: "FINISHED" } as SimulationOrder;
  const pushed = { ...half(), orders: [working] };
  const hook = renderHook(({ feed }) => useForeignHalf({ handle, foreignAccountId: "USD", feed }),
    { initialProps: { feed: live(pushed) } });
  getForeignHalf.mockResolvedValue({ ...half(), orders: [cancelled] });
  await act(async () => { hook.result.current.refreshForeignHalf(); });
  hook.rerender({ feed: live({ ...pushed }) });
  expect(hook.result.current.foreignHalf?.orders).toEqual([cancelled]);
  hook.unmount();
});

test("a silent account channel resumes polling even without another render", async () => {
  const feed = live();
  const hook = renderHook(() => useForeignHalf({ handle, foreignAccountId: "USD", feed }));
  await act(async () => { await vi.advanceTimersByTimeAsync(9_999); });
  expect(getForeignHalf).not.toHaveBeenCalled();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(getForeignHalf).toHaveBeenCalledTimes(1);
  hook.unmount();
});

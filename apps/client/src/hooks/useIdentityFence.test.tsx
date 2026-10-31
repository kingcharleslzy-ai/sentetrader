import { act, renderHook } from "@testing-library/react";
import { resetReadIdentity } from "../runtimeClient";
import { useIdentityFence } from "./useIdentityFence";

test("a response from before a login switch never lands", async () => {
  const view = renderHook(({ session }) => useIdentityFence([session]), { initialProps: { session: "a" } });
  const before = view.result.current;
  let resolve!: (value: string) => void;
  const slow = new Promise<string>(r => { resolve = r; });
  const landed: string[] = [];
  const request = slow.then(value => { if (before.isCurrent()) landed.push(value); });
  expect(before.isCurrent()).toBe(true);
  act(() => resetReadIdentity());
  resolve("stale account");
  await request;
  expect(landed).toEqual([]);
  expect(view.result.current).not.toBe(before);
  expect(view.result.current.isCurrent()).toBe(true);
});

test("changing the scope retires the previous fence", () => {
  const view = renderHook(({ session }) => useIdentityFence([session]), { initialProps: { session: "a" } });
  const first = view.result.current;
  view.rerender({ session: "b" });
  expect(first.isCurrent()).toBe(false);
  expect(view.result.current.isCurrent()).toBe(true);
  expect(view.result.current.context).not.toBe(first.context);
});

test("a channel discards an older read when a newer read or a write intervened", () => {
  const view = renderHook(() => useIdentityFence([]));
  const orders = view.result.current.channel("orders");
  const older = orders.beginRead();
  const newer = orders.beginRead();
  expect(older()).toBe(false);
  expect(newer()).toBe(true);
  orders.noteWrite();
  expect(newer()).toBe(false);
  const afterWrite = orders.beginRead();
  expect(afterWrite()).toBe(true);
  // Channels are independent of each other.
  expect(view.result.current.channel("execution").beginRead()()).toBe(true);
  expect(afterWrite()).toBe(true);
});

test("nothing is current after the workspace unmounts", () => {
  const view = renderHook(() => useIdentityFence([]));
  const fence = view.result.current;
  expect(fence.isCurrent()).toBe(true);
  view.unmount();
  expect(fence.isCurrent()).toBe(false);
  expect(fence.channel("orders").beginRead()()).toBe(false);
});

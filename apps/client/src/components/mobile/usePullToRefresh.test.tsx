// Protects the phone's 下拉刷新 from hanging (#516): offline it springs back at once with a note, a
// read still running at the time limit stops holding the content (the read goes on), and leaving the
// page mid-read leaves no offset, lock or timer behind; a second finger ends the pull cleanly.
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PULL_NOTE_MS, PULL_TIMEOUT_MS, PullIndicator, usePullToRefresh } from "./usePullToRefresh";

function Page({ onRefresh }: { onRefresh: () => unknown }) {
  const pull = usePullToRefresh(onRefresh);
  return <div ref={pull.ref} data-testid="content"><PullIndicator phase={pull.phase} /><p>内容</p></div>;
}

const indicator = () => document.querySelector(".fdm-ptr")!;
function pullAndRelease() {
  const content = screen.getByTestId("content");
  fireEvent.touchStart(content, { touches: [{ clientX: 10, clientY: 10 }] });
  fireEvent.touchMove(content, { touches: [{ clientX: 10, clientY: 20 }] });
  fireEvent.touchMove(content, { touches: [{ clientX: 10, clientY: 220 }] });
  expect(indicator().getAttribute("data-phase")).toBe("armed");
  fireEvent.touchEnd(content);
  return content;
}

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

it("offline, a release springs back at once with a short note that goes after a moment", async () => {
  vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  const onRefresh = vi.fn(() => new Promise(() => undefined));  // a paused read: never settles offline
  render(<Page onRefresh={onRefresh} />);
  const content = pullAndRelease();
  expect(indicator().getAttribute("data-phase")).toBe("offline");
  expect(screen.getByText("离线，联网后自动更新")).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(onRefresh).toHaveBeenCalledTimes(1);  // asked anyway: the reads resume on reconnect
  await act(async () => { await vi.advanceTimersByTimeAsync(PULL_NOTE_MS); });
  expect(indicator().getAttribute("data-phase")).toBe("idle");
  expect(content.style.getPropertyValue("--fdm-pull")).toBe("0");  // sprung back
  // Pullable again right away.
  pullAndRelease();
  expect(indicator().getAttribute("data-phase")).toBe("offline");
});

it("online, a read past the time limit stops holding the content and its late end changes nothing", async () => {
  let finish!: () => void;
  const onRefresh = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
  render(<Page onRefresh={onRefresh} />);
  const content = pullAndRelease();
  expect(indicator().getAttribute("data-phase")).toBe("refreshing");
  await act(async () => { await vi.advanceTimersByTimeAsync(PULL_TIMEOUT_MS - 1); });
  expect(indicator().getAttribute("data-phase")).toBe("refreshing");
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(indicator().getAttribute("data-phase")).toBe("idle");
  expect(content.style.getPropertyValue("--fdm-pull")).toBe("0");  // sprung back
  const first = finish;
  pullAndRelease();  // a second pull while the first read is still out
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(onRefresh).toHaveBeenCalledTimes(2);
  await act(async () => { first(); await vi.advanceTimersByTimeAsync(0); });
  expect(indicator().getAttribute("data-phase")).toBe("refreshing");  // the old read's end is not this round's
  await act(async () => { finish(); await vi.advanceTimersByTimeAsync(0); });
  expect(indicator().getAttribute("data-phase")).toBe("idle");
});

it("a read that ends in time springs back when it ends", async () => {
  render(<Page onRefresh={() => Promise.resolve()} />);
  pullAndRelease();
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(indicator().getAttribute("data-phase")).toBe("idle");
});

it("leaving the page mid-read leaves no offset, lock or timer", async () => {
  const { unmount } = render(<Page onRefresh={() => new Promise(() => undefined)} />);
  const content = pullAndRelease();
  expect(content.dataset.pull).toBe("");
  unmount();
  expect(content.dataset.pull).toBeUndefined();
  expect(content.dataset.pullSettling).toBeUndefined();
  expect(content.style.getPropertyValue("--fdm-pull")).toBe("");
  expect(vi.getTimerCount()).toBe(0);
});

it("a second finger joining a pull ends it without refreshing; nothing stays and the next pull works", async () => {
  const onRefresh = vi.fn(async () => undefined);
  render(<Page onRefresh={onRefresh} />);
  const content = screen.getByTestId("content");
  fireEvent.touchStart(content, { touches: [{ clientX: 10, clientY: 10 }] });
  fireEvent.touchMove(content, { touches: [{ clientX: 10, clientY: 20 }] });
  fireEvent.touchMove(content, { touches: [{ clientX: 10, clientY: 120 }] });
  expect(indicator().getAttribute("data-phase")).toBe("pulling");
  fireEvent.touchStart(content, { touches: [{ clientX: 10, clientY: 120 }, { clientX: 60, clientY: 120 }] });
  expect(indicator().getAttribute("data-phase")).toBe("idle");
  expect(content.style.getPropertyValue("--fdm-pull")).toBe("0");
  expect(content.dataset.pulling).toBeUndefined();
  fireEvent.touchEnd(content, { touches: [{ clientX: 10, clientY: 120 }] });
  fireEvent.touchCancel(content);
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(onRefresh).not.toHaveBeenCalled();
  expect(content.dataset.pull).toBeUndefined();  // the settle finished: no offset left
  pullAndRelease();
  expect(indicator().getAttribute("data-phase")).toBe("refreshing");
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(onRefresh).toHaveBeenCalledTimes(1);
});

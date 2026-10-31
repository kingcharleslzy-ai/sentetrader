import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ConversationState } from "../state/conversation";
import { SETTLE_RETRY_MS, useProposalSettlement } from "./useProposalSettlement";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
// One order card replayed as 等待确认 whose 10-minute window ran out long ago.
const state = { turns: [{ cards: [{ type: "trade", id: "p1", payload: { status: "approval_required", updatedAt: "2026-10-19T01:00:00Z" } }] }],
  supersededTurnIds: [] } as unknown as ConversationState;
const settled = { items: [{ id: "p1", status: "rejected", payload: { expired: true, auditId: "audit-1" } }] };

type Props = { sessionId: string | undefined; connected: boolean; baseUrl: string };
function mount(dispatch = vi.fn(), initial: Props = { sessionId: "s1", connected: true, baseUrl: "http://runtime" }) {
  const view = renderHook((props: Props) => useProposalSettlement({ state, dispatch, ...props }), { initialProps: initial });
  return { ...view, dispatch };
}
const flush = () => act(async () => { for (let i = 0; i < 6; i += 1) await Promise.resolve(); });

beforeEach(() => { vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] }); vi.setSystemTime(new Date("2026-10-19T02:00:00Z")); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it("a first 5xx is read again after a bounded wait and then settles once; the card is never read again (#932 R4)", async () => {
  const answers = [json({ detail: "busy" }, 503), json(settled)];
  const fetch = vi.fn(async () => answers.shift() ?? json(settled));
  vi.stubGlobal("fetch", fetch);
  const view = mount();
  await flush();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(view.dispatch).not.toHaveBeenCalled();
  view.rerender({ sessionId: "s1", connected: true, baseUrl: "http://runtime" });   // a re-render does not retry early
  await flush();
  expect(fetch).toHaveBeenCalledTimes(1);
  await act(async () => { vi.advanceTimersByTime(SETTLE_RETRY_MS[0]); });
  await flush();
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(view.dispatch).toHaveBeenCalledTimes(1);
  expect(view.dispatch.mock.calls[0][0].event.payload).toMatchObject({ proposalId: "p1", status: "rejected", reason: "expired" });
  view.rerender({ sessionId: "s1", connected: true, baseUrl: "http://runtime" });
  await act(async () => { vi.advanceTimersByTime(600_000); });
  await flush();
  expect(fetch).toHaveBeenCalledTimes(2);
  // Only reads: never a confirm sent again.
  expect(fetch.mock.calls.every(call => !(call as unknown as [string, RequestInit?])[1]?.method)).toBe(true);
});

it("a record still waiting is read again later; a reconnect reads at once; a card the answer leaves out is final", async () => {
  const answers = [json({ items: [{ id: "p1", status: "approval_required", payload: {} }] }), json({}, 502), json(settled)];
  const fetch = vi.fn(async () => answers.shift() ?? json({ items: [] }));
  vi.stubGlobal("fetch", fetch);
  const view = mount();
  await flush();
  await act(async () => { vi.advanceTimersByTime(SETTLE_RETRY_MS[0]); });
  await flush();
  expect(fetch).toHaveBeenCalledTimes(2);           // waiting → read again; this one fails (502)
  view.rerender({ sessionId: "s1", connected: false, baseUrl: "http://runtime" });
  view.rerender({ sessionId: "s1", connected: true, baseUrl: "http://runtime" });
  await flush();
  expect(fetch).toHaveBeenCalledTimes(3);           // the stream came back: no 15 s wait
  expect(view.dispatch).toHaveBeenCalledTimes(1);

  const gone = vi.fn(async () => json({ items: [] }));   // not this user's card, or gone
  vi.stubGlobal("fetch", gone);
  const other = mount(vi.fn(), { sessionId: "s9", connected: true, baseUrl: "http://other" });
  await flush();
  await act(async () => { vi.advanceTimersByTime(SETTLE_RETRY_MS.reduce((sum, wait) => sum + wait, 0)); });
  await flush();
  expect(gone).toHaveBeenCalledTimes(1);
  expect(other.dispatch).not.toHaveBeenCalled();
});

it("an answer arriving after the session or Runtime changed writes nothing; the new session reads its own", async () => {
  let release: (response: Response) => void = () => undefined;
  const fetch = vi.fn((url: string) => url.startsWith("http://runtime/")
    ? new Promise<Response>(resolve => { release = resolve; }) : Promise.resolve(json(settled)));
  vi.stubGlobal("fetch", fetch);
  const view = mount();
  await flush();
  view.rerender({ sessionId: "s2", connected: true, baseUrl: "http://runtime-2" });
  await flush();
  expect(view.dispatch).toHaveBeenCalledTimes(1);   // s2's own read
  expect(view.dispatch.mock.calls[0][0].event.sessionId).toBe("s2");
  await act(async () => { release(json(settled)); });
  await flush();
  expect(view.dispatch).toHaveBeenCalledTimes(1);   // s1's late answer is dropped
});

it("a read stalled when the stream drops is aborted; the reconnect reads again at once (Codex r4307841910)", async () => {
  const signals: (AbortSignal | undefined)[] = [];
  const answers: ((response: Response) => void)[] = [];
  const fetch = vi.fn((_url: string, init?: RequestInit) => {
    signals.push(init?.signal ?? undefined);
    return new Promise<Response>(resolve => { answers.push(resolve); });
  });
  vi.stubGlobal("fetch", fetch);
  const view = mount();
  await flush();
  expect(fetch).toHaveBeenCalledTimes(1);
  view.rerender({ sessionId: "s1", connected: false, baseUrl: "http://runtime" });
  expect(signals[0]?.aborted).toBe(true);
  view.rerender({ sessionId: "s1", connected: true, baseUrl: "http://runtime" });
  await flush();
  expect(fetch).toHaveBeenCalledTimes(2);            // no 30 s read timeout and backoff first
  await act(async () => { answers[0](json(settled)); });
  await flush();
  expect(view.dispatch).not.toHaveBeenCalled();     // the old connection's answer writes nothing
  await act(async () => { answers[1](json(settled)); });
  await flush();
  expect(view.dispatch).toHaveBeenCalledTimes(1);
});

it("gives up after the last wait: the card stays as it was", async () => {
  const fetch = vi.fn(async () => json({}, 503));
  vi.stubGlobal("fetch", fetch);
  mount();
  await flush();
  for (const wait of SETTLE_RETRY_MS) {
    await act(async () => { vi.advanceTimersByTime(wait); });
    await flush();
  }
  expect(fetch).toHaveBeenCalledTimes(SETTLE_RETRY_MS.length + 1);
  await act(async () => { vi.advanceTimersByTime(3_600_000); });
  await flush();
  expect(fetch).toHaveBeenCalledTimes(SETTLE_RETRY_MS.length + 1);
});

it("every due card is read in one request, not one each", async () => {
  const many = { turns: [{ cards: ["p1", "p2", "p3"].map(id => (
    { type: "trade", id, payload: { status: "approval_required", updatedAt: "2026-10-19T01:00:00Z" } })) }],
    supersededTurnIds: [] } as unknown as ConversationState;
  const fetch = vi.fn(async () => json({ items: [
    { id: "p1", status: "rejected", payload: {} }, { id: "p3", status: "candidate_recorded", payload: {} }] }));
  vi.stubGlobal("fetch", fetch);
  const dispatch = vi.fn();
  renderHook(() => useProposalSettlement({ state: many, dispatch, sessionId: "s1", connected: true, baseUrl: "http://runtime" }));
  await flush();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect((fetch.mock.calls[0] as unknown as [string])[0]).toBe("http://runtime/api/proposals?ids=p1&ids=p2&ids=p3");
  expect(dispatch.mock.calls.map(call => call[0].event.payload.proposalId)).toEqual(["p1", "p3"]);
  await act(async () => { vi.advanceTimersByTime(600_000); });
  await flush();
  expect(fetch).toHaveBeenCalledTimes(1);           // p2 (left out) is final too
});

import { act, renderHook, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import type { GuestSession } from "../guestApi";
import { contractCatalogState } from "../components/futures/WorkspaceDrawers";
import { guestCatalogLoading, useGuestAccount } from "./useGuestAccount";

vi.mock("../sse", () => ({ openSseStream: () => vi.fn() }));

const base = "https://guest.test";
const session: GuestSession = { role: "guest", guestId: "g1", expiresAt: "2099-01-01", readOnly: true, voiceEnabled: false, windowId: "w1", accessToken: "guest.t" };
const empty = { schemaVersion: 1, capturedAt: "2026-10-11T00:00:00Z", dataStatus: "unavailable", isCached: false, count: 0, items: [], catalogStatus: "unavailable" };

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });

/** The drawer state the guest workspace derives from the hook (as GuestEntry wires it). */
const drawerState = (guest: ReturnType<typeof useGuestAccount>) => contractCatalogState({ guest: { expired: guest.expired }, connection: "connecting",
  instruments: guest.catalog, instrumentsError: guest.catalogError, instrumentsLoading: guestCatalogLoading(guest) });

test("游客合约目录重试期间是读取中：快照不再可重试，重复重试不叠加请求", async () => {
  let answerRetry: (response: Response) => void = () => undefined;
  let catalogReads = 0;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    if (!String(input).endsWith("/api/guest/instruments")) return Promise.resolve(Response.json({}));
    catalogReads += 1;
    return catalogReads === 1 ? Promise.resolve(Response.json(empty)) : new Promise<Response>(resolve => { answerRetry = resolve; });
  }));
  const { result } = renderHook(() => useGuestAccount(base, session));
  await waitFor(() => expect(result.current.catalogLoading).toBe(false));
  expect(drawerState(result.current)).toEqual({ status: "empty", canRetry: true });
  act(() => { result.current.reloadCatalog(); });
  expect(drawerState(result.current)).toEqual({ status: "loading", canRetry: false });
  act(() => { result.current.reloadCatalog(); });
  await waitFor(() => expect(catalogReads).toBe(2));
  await act(async () => { answerRetry(Response.json({ ...empty, items: [], catalogStatus: "unavailable" })); });
  await waitFor(() => expect(result.current.catalogLoading).toBe(false));
  expect(catalogReads).toBe(2);
  expect(drawerState(result.current)).toEqual({ status: "empty", canRetry: true });
});

test("游客会话失效（401）后目录重试不读也不新建会话", async () => {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    calls.push(String(input));
    return Promise.resolve(new Response("", { status: 401 }));
  }));
  const { result } = renderHook(() => useGuestAccount(base, session));
  await waitFor(() => expect(result.current.expired).toBe(true));
  const before = calls.length;
  act(() => { result.current.reloadCatalog(); });
  await act(async () => { await Promise.resolve(); });
  expect(calls.length).toBe(before);
  expect(calls.some(url => url.endsWith("/api/auth/guest"))).toBe(false);
  expect(drawerState(result.current)).toEqual({ status: "expired", canRetry: false });
});

test("游客冷启动目录还在准备时之后读取失败：显示错误并可重试，不一直转圈", async () => {
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    if (!String(input).endsWith("/api/guest/instruments")) return Promise.resolve(Response.json({}));
    reads += 1;
    return reads === 1 ? Promise.resolve(Response.json({ ...empty, catalogStatus: "loading" })) : Promise.resolve(new Response("", { status: 503 }));
  }));
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const { result } = renderHook(() => useGuestAccount(base, session));
    await waitFor(() => expect(result.current.catalog?.catalogStatus).toBe("loading"));
    expect(drawerState(result.current).status).toBe("loading");
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    await waitFor(() => expect(result.current.catalogError).toBe("游客服务暂时不可用，请重试"));
    expect(drawerState(result.current)).toEqual({ status: "error", canRetry: true });
  } finally {
    vi.useRealTimers();
  }
});

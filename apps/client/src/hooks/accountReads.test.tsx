import { act, cleanup, renderHook } from "@testing-library/react";
import { setActiveTradingAccount } from "../api";
import { useAccountPnl } from "./useAccountPnl";
import { useTradeReview } from "./useTradeReview";

type Options = { baseUrl: string | undefined; accountId?: string | null; enabled?: boolean; symbol?: string };
function useReviewRead(options: Options) {
  const { review, error } = useTradeReview(options);
  return { data: review, error };
}
function usePnlRead(options: Options) {
  const { pnl, error } = useAccountPnl(options);
  return { data: pnl, error };
}
const readers = [{ name: "成交", useRead: useReviewRead }, { name: "账户盈亏", useRead: usePnlRead }];
const baseUrl = "http://127.0.0.1:8000";
const payload = (account: string) => ({ accountName: account, trades: [], equity: account === "A" ? 100 : 200 });
const response = (account: string) => new Response(JSON.stringify(payload(account)), { status: 200 });
function deferred() {
  let resolve!: (value: Response) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<Response>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.useFakeTimers();
  // Explicit read scope must win even before the workspace's global header effect runs.
  setActiveTradingAccount("previous-global-account");
});
afterEach(() => {
  cleanup();
  setActiveTradingAccount(null);
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe.each(readers)("$name 读取的账户归属", ({ useRead }) => {
  it.each(["success", "failure"])("switches immediately and ignores a late old-account %s", async outcome => {
    const lateA = deferred(), pendingB = deferred();
    const fetch = vi.fn().mockResolvedValueOnce(response("A"))
      .mockReturnValueOnce(lateA.promise).mockReturnValueOnce(pendingB.promise);
    vi.stubGlobal("fetch", fetch);
    const { result, rerender } = renderHook(options => useRead(options), {
      initialProps: { baseUrl, accountId: "A" } as Options
    });
    await act(async () => {});
    expect(result.current.data).toEqual(payload("A"));
    await act(async () => { vi.advanceTimersByTime(5000); });
    expect(fetch).toHaveBeenCalledTimes(2);

    rerender({ baseUrl, accountId: "B" });
    expect(result.current.data).toBeUndefined();
    expect(result.current.error).toBe("");
    expect(new Headers(fetch.mock.calls[2][1].headers).get("X-AI-Trader-Account")).toBe("B");
    await act(async () => { pendingB.resolve(response("B")); });
    await act(async () => {
      if (outcome === "success") lateA.resolve(response("A"));
      else lateA.reject(new Error("old account unavailable"));
    });
    expect(result.current).toEqual({ data: payload("B"), error: "" });
  });

  it("pauses for an unknown account, a disabled reader or lost connection, then reads on recovery", async () => {
    for (const pause of [{ accountId: null }, { enabled: false }, { baseUrl: undefined }]) {
      const pendingA = deferred();
      const fetch = vi.fn().mockReturnValueOnce(pendingA.promise).mockResolvedValue(response("B"));
      vi.stubGlobal("fetch", fetch);
      const { result, rerender, unmount } = renderHook(options => useRead(options), {
        initialProps: { baseUrl, accountId: "A", enabled: true } as Options
      });
      rerender({ baseUrl, accountId: "A", enabled: true, ...pause });
      await act(async () => { pendingA.resolve(response("A")); vi.advanceTimersByTime(20_000); });
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(result.current).toEqual({ data: undefined, error: "" });
      rerender({ baseUrl, accountId: "B", enabled: true });
      await act(async () => {});
      expect(result.current.data).toEqual(payload("B"));
      expect(new Headers(fetch.mock.calls[1][1].headers).get("X-AI-Trader-Account")).toBe("B");
      unmount();
    }
  });

  it("keeps legacy reads available when accountId is omitted", async () => {
    setActiveTradingAccount(null);
    const fetch = vi.fn().mockResolvedValue(response("A"));
    vi.stubGlobal("fetch", fetch);
    const { result } = renderHook(() => useRead({ baseUrl, symbol: "SHFE.rb2610" }));
    await act(async () => {});
    expect(result.current.data).toEqual(payload("A"));
    expect(new Headers(fetch.mock.calls[0][1].headers).get("X-AI-Trader-Account")).toBeNull();
    if (useRead === useReviewRead) expect(fetch.mock.calls[0][0]).toBe(`${baseUrl}/api/trade-review?symbol=SHFE.rb2610`);
  });
});

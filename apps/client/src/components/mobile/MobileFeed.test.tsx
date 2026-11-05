import { act, renderHook, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { requestJson } from "../../api";
import type { DeskReport } from "../ResearchDesk";
import { useDeskJudgments } from "./MobileFeed";

vi.mock("../../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../../api")>()), requestJson: vi.fn() }));

const report = (judgment: DeskReport["judgment"]): DeskReport => ({
  symbol: "SHFE.ru2701", productName: "橡胶", mainSymbol: "SHFE.ru2701", generatedAt: "2026-10-06T02:05:00Z",
  sections: [], facts: [], notice: "", judgment,
});
const READY = { status: "ready", today: true, stance: "偏多", confidence: "中", headline: "库存回落", generatedAt: "2026-10-06T02:05:00Z",
  researchDay: "2026-10-06", points: [], risks: [], basis: {} } as unknown as DeskReport["judgment"];

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

it("调研台读数回来前是读取中，读回后带上判断卡", async () => {
  const pending = deferred<DeskReport>();
  vi.mocked(requestJson).mockReturnValueOnce(pending.promise);
  const { result, unmount } = renderHook(() => useDeskJudgments("http://runtime", ["SHFE.ru2701"]));
  expect(result.current.items[0]).toMatchObject({ card: null, loading: true, failed: false });
  await act(async () => { pending.resolve(report(READY)); });
  await waitFor(() => expect(result.current.items[0]!.loading).toBe(false));
  expect(result.current.items[0]!.card).toMatchObject({ status: "ready", stance: "偏多" });
  unmount();
});

it("读完确实没有判断（档位不出判断）是 unavailable；没有连接时不显示读取中", async () => {
  vi.mocked(requestJson).mockResolvedValueOnce(report({ status: "budget_exhausted" } as DeskReport["judgment"]));
  const { result, unmount } = renderHook(() => useDeskJudgments("http://runtime", ["SHFE.ru2701"]));
  expect(result.current.items[0]!.loading).toBe(true);
  await waitFor(() => expect(result.current.items[0]!.loading).toBe(false));
  expect(result.current.items[0]!.card).toMatchObject({ status: "unavailable" });
  unmount();
  const offline = renderHook(() => useDeskJudgments(undefined, ["SHFE.ru2701"]));
  expect(offline.result.current.items[0]).toMatchObject({ loading: false });
  offline.unmount();
});

it("判断生成中时读取失败：不再一直显示“生成中”，说清刚才没读到并在 1 分钟后再查（#516）", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  try {
    vi.mocked(requestJson).mockReset();
    vi.mocked(requestJson).mockResolvedValueOnce(report({ status: "pending" } as DeskReport["judgment"]))
      .mockRejectedValueOnce(new Error("HTTP 502")).mockResolvedValueOnce(report(READY));
    const { result, unmount } = renderHook(() => useDeskJudgments("http://runtime", ["SHFE.ru2701"]));
    await act(async () => { await Promise.resolve(); });
    expect(result.current.items[0]!.card).toMatchObject({ status: "pending" });
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(result.current.items[0]).toMatchObject({ failed: true, card: { status: "pending", note: expect.stringContaining("没读到最新状态") } });
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });  // the next look comes in a minute, not ten
    expect(result.current.items[0]).toMatchObject({ failed: false, card: { status: "ready" } });
    unmount();
  } finally { vi.useRealTimers(); }
});

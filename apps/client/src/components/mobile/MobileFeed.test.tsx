import { act, renderHook, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import type { ResearchJudgmentCard } from "@ai-trader/ui-events";
import { requestJson } from "../../api";
import type { DeskReport } from "../ResearchDesk";
import { buildMobileFeed } from "./feedItems";
import { useDeskJudgments } from "./MobileFeed";
import { judgmentChips } from "./notifyItems";

vi.mock("../../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../../api")>()), requestJson: vi.fn() }));

const report = (judgment: DeskReport["judgment"]): DeskReport => ({
  symbol: "SHFE.ru2701", productName: "橡胶", mainSymbol: "SHFE.ru2701", generatedAt: "2026-10-06T02:05:00Z",
  sections: [], facts: [], notice: "", judgment,
});
const READY = { status: "ready", today: true, stance: "偏多", confidence: "中", headline: "库存回落", generatedAt: "2026-10-06T02:05:00Z",
  researchDay: "2026-10-06", points: [], risks: [], basis: {} } as unknown as DeskReport["judgment"];
const products = [{ product: "SHFE.ru", name: "橡胶" }];
const now = Date.parse("2026-10-06T02:30:00Z");

/** The chip the AI 首页 shows for 橡胶 from one desk state (and the conversation's judgment cards). */
function chip(desk: { card: ResearchJudgmentCard | null; loading: boolean }, conversation: ResearchJudgmentCard[] = []) {
  const turns = conversation.map((card, index) => ({ id: `t${index}`, role: "assistant", text: "", timestamp: card.generatedAt ?? "",
    cards: [{ id: card.cardId, type: "research-judgment", payload: card }] }));
  const items = buildMobileFeed({ turns: turns as never, briefs: [], desks: [desk.card] });
  return judgmentChips(products, items, { loading: new Set(desk.loading ? ["shfe.ru"] : []), now })[0]!.stance;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

it("保护：今日判断胶囊在调研台读数回来前说“读取中”，读回结论变成立场（10-06 线上刚打开显示“暂无”）", async () => {
  const pending = deferred<DeskReport>();
  vi.mocked(requestJson).mockReturnValueOnce(pending.promise);
  const { result, unmount } = renderHook(() => useDeskJudgments("http://runtime", ["SHFE.ru2701"]));
  expect(result.current.items[0]).toMatchObject({ card: null, loading: true, failed: false });
  expect(chip(result.current.items[0]!)).toBe("读取中");
  await act(async () => { pending.resolve(report(READY)); });
  await waitFor(() => expect(result.current.items[0]!.loading).toBe(false));
  expect(chip(result.current.items[0]!)).toBe("偏多");
  unmount();
});

it("读完确实没有判断（档位不出判断）才是“暂无”；没有连接时不显示读取中", async () => {
  vi.mocked(requestJson).mockResolvedValueOnce(report({ status: "budget_exhausted" } as DeskReport["judgment"]));
  const { result, unmount } = renderHook(() => useDeskJudgments("http://runtime", ["SHFE.ru2701"]));
  expect(chip(result.current.items[0]!)).toBe("读取中");
  await waitFor(() => expect(result.current.items[0]!.loading).toBe(false));
  expect(result.current.items[0]!.card).toMatchObject({ status: "unavailable" });
  expect(chip(result.current.items[0]!)).toBe("暂无");
  unmount();
  const offline = renderHook(() => useDeskJudgments(undefined, ["SHFE.ru2701"]));
  expect(offline.result.current.items[0]).toMatchObject({ loading: false });
  offline.unmount();
});

it("对话里已有今天同品种的判断卡时，胶囊不等调研台读数，直接显示它；更早的卡不能顶替", () => {
  const card = (cardId: string, generatedAt: string, researchDay: string): ResearchJudgmentCard => ({ turnId: cardId, cardId, symbol: "SHFE.ru2701",
    productName: "橡胶", status: "ready", stance: "偏多", generatedAt, researchDay });
  expect(chip({ card: null, loading: true }, [card("j1", "2026-10-06T02:05:00Z", "2026-10-06")])).toBe("偏多");
  expect(chip({ card: null, loading: true }, [card("j0", "2026-10-04T02:05:00Z", "2026-10-04")])).toBe("读取中");
  // A pending judgment of the same day does not hide the ready one the conversation already has.
  const generating: ResearchJudgmentCard = { turnId: "d", cardId: "desk:SHFE.ru2701", symbol: "SHFE.ru2701", productName: "橡胶", status: "pending",
    researchDay: "2026-10-06", generatedAt: "2026-10-06T02:20:00Z" };
  expect(chip({ card: generating, loading: false }, [card("j1", "2026-10-06T02:05:00Z", "2026-10-06")])).toBe("偏多");
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

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { ResearchJudgmentCard } from "@ai-trader/ui-events";
import { resetUiSurfaces } from "../../uiSurfaces";
import { MobileResearch } from "./MobileResearch";

afterEach(() => { vi.unstubAllGlobals(); resetUiSurfaces(); });

const instruments = [{ symbol: "SHFE.au2612", productId: "au", exchangeId: "SHFE", instrumentName: "沪金2612", isMain: true,
  deliveryYear: 2026, deliveryMonth: 12 }] as never;

test("就这份判断问 AI 引用页面上这份判断（调研台自己的读数），不是工作台较旧的那份（#516）", async () => {
  const desk = { symbol: "SHFE.au2612", productName: "沪金", mainSymbol: "SHFE.au2612", generatedAt: "2026-10-06T02:05:00Z", sections: [],
    facts: [], notice: "", judgment: { status: "ready", today: true, stance: "偏空", confidence: "中", headline: "美债收益率走高",
      generatedAt: "2026-10-06T03:10:00Z", researchDay: "2026-10-06", points: [], risks: [], basis: {} } };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(
    String(url).includes("/research/desk") ? desk : String(url).includes("/research/flash") ? { status: "ok", items: [] } : { items: [] }))));
  // The workspace's slower read still holds the morning judgment.
  const older = { turnId: "mobile-feed", cardId: "desk:SHFE.au2612", symbol: "SHFE.au2612", productName: "沪金", status: "ready", stance: "偏多",
    headline: "避险买盘", generatedAt: "2026-10-06T00:40:00Z", researchDay: "2026-10-06", facts: [], invalidation: [], actions: [] } as ResearchJudgmentCard;
  const askAi = vi.fn();
  render(<MobileResearch baseUrl="https://test" symbol="SHFE.au2612" productName="沪金" judgment={older} watchlist={[]} instruments={instruments}
    onSelectSymbol={() => undefined} onOpenContracts={() => undefined} handlers={{ askAi } as never} />);
  await screen.findByText("美债收益率走高");
  await act(async () => { await Promise.resolve(); });  // the desk reports what it shows after rendering it
  fireEvent.click(screen.getByRole("button", { name: "就这份判断问 AI" }));
  await waitFor(() => expect(askAi).toHaveBeenCalled());
  expect(askAi.mock.calls[0][0]).toContain("沪金今天这份判断（美债收益率走高）");
});

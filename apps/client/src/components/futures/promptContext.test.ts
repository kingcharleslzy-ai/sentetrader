import { expect, test } from "vitest";
import type { AccountOverviewSnapshot, MarketWorkbenchSnapshot, SimulationOrder } from "../../api";
import { buildPromptContext } from "./promptContext";

test("本站模拟两本账：发给 AI 的界面上下文带美元账（外盘）的在途委托（2026-10-22）", () => {
  const usdOrder = { accountId: "paperusd:a", clientOrderId: "AIT-usd", request: { symbol: "CME.MGCZ26", direction: "BUY", offset: "OPEN",
    volume: 2, priceType: "LIMIT", limitPrice: 4100 }, remoteStatus: "ALIVE", cancelPhase: "NONE", submitPhase: "CONFIRMED",
    filledVolume: 1, displayStatus: "部分成交" } as unknown as SimulationOrder;
  const account = { account: { equity: 1_000_000 }, positions: [],
    activity: { orders: [{ clientOrderId: "AIT-rmb", symbol: "SHFE.rb2701", direction: "BUY", offset: "OPEN", volume: 1, volumeLeft: 1,
      filledVolume: 0, limitPrice: 3000, status: "ALIVE", insertDateTime: 1 }], trades: [] },
    foreign: { accountId: "paperusd:a", account: {}, positions: [], orders: [usdOrder] } } as unknown as AccountOverviewSnapshot;
  const market = { quote: { symbol: "SHFE.rb2701", name: "螺纹2701", lastPrice: 3010 }, capturedAt: "", ageSeconds: 0, dataStatus: "normal" } as unknown as MarketWorkbenchSnapshot;
  const context = buildPromptContext({ market, account, duration: 60, view: "trade", rightDock: null, selectedEvent: undefined,
    explanationSupported: false, selectedPrice: undefined, currentPositions: [], selectedPosition: undefined, ticketDraft: undefined, recentActions: [] })!;
  expect(context.activeOrderCount).toBe(2);
  expect(context.activeOrders.map(order => [order.symbol, order.volumeLeft])).toEqual([["SHFE.rb2701", 1], ["CME.MGCZ26", 1]]);
});

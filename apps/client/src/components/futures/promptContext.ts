import { sameContract, type AccountOverviewSnapshot, type AccountPosition, type FuturesWorkspacePromptContext, type MarketWorkbenchSnapshot } from "../../api";
import type { OperationEvent } from "../../operationEvents";
import { orderIsActive } from "./PositionPanels";
import { positionPromptContext, type FuturesWorkspaceView, type RecentWorkspaceAction, type RightDock, type TicketDraft } from "./support";

export interface PromptContextInput {
  market: MarketWorkbenchSnapshot | undefined;
  account: AccountOverviewSnapshot | undefined;
  duration: MarketWorkbenchSnapshot["durationSeconds"];
  view: FuturesWorkspaceView;
  rightDock: RightDock;
  selectedEvent: OperationEvent | undefined;
  explanationSupported: boolean;
  selectedPrice: number | undefined;
  currentPositions: AccountPosition[];
  selectedPosition: AccountPosition | undefined;
  ticketDraft: TicketDraft | undefined;
  recentActions: RecentWorkspaceAction[];
}

/** The workspace snapshot that rides with a prompt so the model sees what the user sees. */
export function buildPromptContext({ market, account, duration, view, rightDock, selectedEvent, explanationSupported, selectedPrice,
  currentPositions, selectedPosition, ticketDraft, recentActions }: PromptContextInput): FuturesWorkspacePromptContext | undefined {
  const quote = market?.quote;
  if (!market || !quote) return undefined;
  const ownOrders = (account?.activity.orders ?? [])
    .filter((order) => order.status === "ALIVE" && order.volumeLeft > 0)
    .map(({ clientOrderId, symbol, direction, offset, volume, volumeLeft, filledVolume, limitPrice, status, insertDateTime }) => ({
      clientOrderId, symbol, direction, offset, volume, volumeLeft, filledVolume, limitPrice, status, insertDateTime
    }));
  // 本站模拟两本账 (2026-10-22): the 外盘 USD ledger's working orders are on the user's screen too (委托 list), so the
  // model sees them — their CME symbols name the ledger.
  const foreignOrders = (account?.foreign?.orders ?? []).filter(orderIsActive).map(order => {
    const filledVolume = order.filledVolume ?? 0;
    return { clientOrderId: order.clientOrderId, symbol: order.request.symbol, direction: order.request.direction,
      offset: order.request.offset, volume: order.request.volume, volumeLeft: Math.max(0, order.request.volume - filledVolume),
      filledVolume, limitPrice: order.request.limitPrice, status: "ALIVE", insertDateTime: 0 };
  });
  const activeOrders = [...ownOrders, ...foreignOrders].slice(-8);
  const recentTrades = (account?.activity.trades ?? [])
    .filter((trade) => sameContract(trade.symbol, quote.symbol))
    .slice(-8)
    .map(({ tradeId, clientOrderId, symbol, direction, offset, price, volume, tradeDateTime }) => ({
      tradeId, clientOrderId, symbol, direction, offset, price, volume, tradeDateTime
    }));
  const currentOrderDraft = ticketDraft ? {
    side: ticketDraft.side,
    volume: ticketDraft.volume,
    limitPrice: ticketDraft.limitPrice,
    stopPrice: ticketDraft.stopPrice,
    source: ticketDraft.source
  } : undefined;
  return {
    view,
    // The prompt context keeps the three docks the AI reasons about; 调研 / 提醒 / 策略 / 持仓 read as "ai".
    rightDock: rightDock === "news" || rightDock === "reminders" || rightDock === "strategy" || rightDock === "positions" ? "ai" : rightDock,
    ...(explanationSupported && selectedEvent ? {selectedEvent: {strategyId: selectedEvent.strategyId, eventType: selectedEvent.eventType, eventId: selectedEvent.eventId, eventTime: String(selectedEvent.time)}} : {}),
    symbol: quote.symbol,
    instrumentName: quote.name,
    durationSeconds: duration,
    selectedPrice,
    quoteDatetime: quote.quoteDatetime,
    capturedAt: market.capturedAt,
    ageSeconds: market.ageSeconds,
    dataStatus: market.dataStatus,
    marketSessionStatus: market.marketSessionStatus ?? quote.marketSessionStatus ?? "unknown",
    lastPrice: quote.lastPrice,
    bidPrice1: quote.bidPrice1 ?? undefined,
    askPrice1: quote.askPrice1 ?? undefined,
    priceTick: quote.priceTick ?? undefined,
    positions: currentPositions.map(positionPromptContext),
    selectedPosition: selectedPosition ? positionPromptContext(selectedPosition) : undefined,
    orderDraft: currentOrderDraft,
    activeOrderCount: ownOrders.length + foreignOrders.length,
    activeOrders,
    recentTrades,
    recentActions
  };
}

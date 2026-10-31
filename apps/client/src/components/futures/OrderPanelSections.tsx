import { ChevronDown } from "lucide-react";
import { useEffect, type CSSProperties } from "react";
import { sameContract, type AccountPosition, type AiStopLoss, type InstrumentCatalog, type MarketQuoteSnapshot, type SimulationOrder } from "../../api";
import { useDisclosureSurface } from "../../uiSurfaces";
import type { AskAi } from "../AiMenu";
import { OrderRows, PositionRows, orderIsActive, useAbandonOrder, type OrderActionHandler, type PositionStopRequest } from "./PositionPanels";
import { decimalsForTick, formatNumber } from "./support";
import "./OrderPanelSections.css";

/** The 五档 fold is remembered on this machine only. */
export const ORDER_DEPTH_OPEN_KEY = "ai-trader:order-depth-open";

function depthOpenStored(): boolean {
  try { return localStorage.getItem(ORDER_DEPTH_OPEN_KEY) === "1"; } catch { return false; }
}

/** Clicking a level fills the manual limit price; side describes the quoted row. */
export type LadderPick = (price: number, side: "ask" | "last" | "bid") => void;

interface LadderRow { key: string; side: "ask" | "last" | "bid"; level: string; price?: number; volume?: number }

const LEVEL_NAMES = "一二三四五";

/** 卖五→卖一、最新、买一→买五; render only levels the feed actually carries. */
function ladderRows(quote: MarketQuoteSnapshot): LadderRow[] {
  const depth = quote as unknown as Record<string, number | null | undefined>;
  const level = (side: "ask" | "bid", index: number): LadderRow => ({
    key: `${side}-${index}`,
    side,
    level: `${side === "ask" ? "卖" : "买"}${LEVEL_NAMES[index - 1]}`,
    price: depth[`${side}Price${index}`] ?? undefined,
    volume: depth[`${side}Volume${index}`] ?? undefined
  });
  return [
    ...[5, 4, 3, 2, 1].map(index => level("ask", index)),
    { key: "last", side: "last" as const, level: "最新", price: quote.lastPrice ?? undefined },
    ...[1, 2, 3, 4, 5].map(index => level("bid", index))
  ].filter(row => row.price !== undefined);
}

/** True when the feed carries any level beyond the best bid/ask. */
function hasFiveLevels(quote: MarketQuoteSnapshot): boolean {
  return [
    quote.bidPrice2, quote.askPrice2, quote.bidPrice3, quote.askPrice3,
    quote.bidPrice4, quote.askPrice4, quote.bidPrice5, quote.askPrice5
  ].some((price) => price !== null && price !== undefined);
}

/** Valid level buttons fill the manual limit price without submitting. */
function LadderRows({ quote, selectedPrice, onPriceSelect }: { quote: MarketQuoteSnapshot; selectedPrice?: number; onPriceSelect: LadderPick }) {
  const decimals = decimalsForTick(quote.priceTick);
  const rows = ladderRows(quote);
  // Depth bars scale to the deepest shown level.
  const deepest = Math.max(1, ...rows.map(row => row.volume ?? 0));
  return (
    <div className="fdw-ladder-rows">
      {rows.map((row) => {
        const price = row.price;
        const isSelected = price !== undefined && selectedPrice !== undefined && Math.abs(price - selectedPrice) < 1e-8;
        return (
          <button
            type="button"
            key={row.key}
            className={`${row.side} ${isSelected ? "selected" : ""}`}
            disabled={price === undefined}
            style={{ "--depth": (row.volume ?? 0) / deepest } as CSSProperties}
            onClick={() => price !== undefined && onPriceSelect(price, row.side)}
          >
            <span>{row.level}</span>
            <b>{row.side === "bid" ? formatNumber(row.volume) : ""}</b>
            <strong>{formatNumber(price, decimals)}</strong>
            <b>{row.side === "ask" ? formatNumber(row.volume) : ""}</b>
          </button>
        );
      })}
    </div>
  );
}

/**
 * 下单面板 · 五档: a click fills the manual limit price only.
 * Folded, the head keeps 卖一 / 最新 / 买一 on one line.
 */
export function OrderDepth({ quote, selectedPrice, onPriceSelect }: { quote: MarketQuoteSnapshot; selectedPrice?: number; onPriceSelect: LadderPick }) {
  const [open, setOpen] = useDisclosureSurface("order-depth", { initial: depthOpenStored() });
  useEffect(() => {
    try { localStorage.setItem(ORDER_DEPTH_OPEN_KEY, open ? "1" : "0"); } catch { /* Local preference only. */ }
  }, [open]);
  const decimals = decimalsForTick(quote.priceTick);
  return (
    <section className="fdw-order-section fdw-order-depth" aria-label="五档" data-agent-target="order-depth">
      <div className="fdw-order-topline">
        {([ ["ask", "卖一", quote.askPrice1, quote.askVolume1], ["last", "最新", quote.lastPrice, null], ["bid", "买一", quote.bidPrice1, quote.bidVolume1] ] as const).filter(([, , price]) => price != null).map(([side, label, price, volume]) =>
          <button type="button" className={side} key={side} onClick={() => price != null && onPriceSelect(price, side)}>
            <span>{label}{volume != null ? ` · ${formatNumber(volume)}` : ""}</span><strong>{formatNumber(price, decimals)}</strong>
            {side === "last" && quote.changePercent != null ? <small>{quote.changePercent > 0 ? "+" : ""}{formatNumber(quote.changePercent, 2)}%</small> : null}
          </button>)}
      </div>
      {hasFiveLevels(quote) ? <>
        <h3><button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}><ChevronDown aria-hidden />{open ? "收起五档" : "展开五档"}</button></h3>
        {open ? <LadderRows quote={quote} selectedPrice={selectedPrice} onPriceSelect={onPriceSelect} /> : null}
      </> : null}
    </section>
  );
}

/**
 * 下单面板 · 本合约持仓: the current contract's position rows (平仓 / 保护 / AI, as in the 持仓面板)
 * and its working orders (撤单 / 查询 / 继续 / 放弃).
 */
export function OrderPanelPositions({ quote, positions, orders, instruments, busy, onClosePosition, onOrderAction, onAskAi, stops, protectionBusy, onCreateStop, onCancelStop }: {
  quote: MarketQuoteSnapshot;
  /** Already filtered to the current contract. */
  positions: AccountPosition[];
  orders: SimulationOrder[];
  instruments?: InstrumentCatalog;
  busy: boolean;
  onClosePosition: (position: AccountPosition) => void;
  onOrderAction: OrderActionHandler;
  onAskAi?: AskAi;
  stops?: AiStopLoss[];
  protectionBusy?: boolean;
  onCreateStop: (position: AccountPosition, request: PositionStopRequest) => Promise<boolean | void>;
  onCancelStop: (stopId: string) => Promise<boolean | void>;
}) {
  const abandon = useAbandonOrder(busy, onOrderAction);
  const working = orders.filter(order => orderIsActive(order) && sameContract(order.request.symbol, quote.symbol));
  return (
    <section className="fdw-order-section fdw-order-holdings" aria-label="本合约持仓" data-agent-target="order-positions">
      <h3><span>本合约持仓</span>{positions.length ? <small>{positions.length}</small> : null}</h3>
      {positions.length ? <PositionRows positions={positions} instruments={instruments} quote={quote} busy={busy} limit={10} onClosePosition={onClosePosition} onAskAi={onAskAi}
        stops={stops} protectionBusy={protectionBusy} onCreateStop={onCreateStop} onCancelStop={onCancelStop} />
        : <p className="fdw-order-empty">本合约无持仓</p>}
      {working.length ? <OrderRows showShortCode instruments={instruments} orders={working} history={false} busy={busy} limit={10} onOrderAction={onOrderAction} onAbandon={abandon.request} onAskAi={onAskAi} /> : null}
      {abandon.dialog}
    </section>
  );
}

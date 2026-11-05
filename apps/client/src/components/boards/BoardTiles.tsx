import { useState, type ComponentProps } from "react";
import { foreignQuoteOnly, sameContract, type AccountPosition, type InstrumentCatalog, type MarketBoardRow, type MarketWorkbenchSnapshot } from "../../api";
import { TradingCharts } from "../TradingCharts";
import { durationLabel } from "../mobile/mobileMarketPrefs";
import { chartDayQuote, contractLabel, dayQuote, decimalsForTick, formatBoardPrice, formatNumber, formatSigned, signClass, FALLBACK_PRICE_TITLE, type ManualSimulationRequest } from "../futures/support";
import type { useQuickOrders } from "../futures/useQuickOrders";

/**
 * 看板 (2026-11-05): the small and medium layouts of the 第二批 modules, each its own arrangement — a small one keeps
 * only what matters most and is one button that opens the full panel (下单 excepted: its 买多 / 卖空 always ask first).
 * The large ones are the panels themselves (the workspace hands them in).
 */

function DayPrice({ market, big }: { market: MarketWorkbenchSnapshot; big?: boolean }) {
  const day = chartDayQuote(market.quote);
  const change = day.changePercent;
  return <span className={`board-price${big ? " big" : ""}`} data-direction={change ? change > 0 ? "up" : "down" : undefined}
    title={day.fallback ? FALLBACK_PRICE_TITLE : undefined}>
    <strong>{formatNumber(day.price, decimalsForTick(market.quote.priceTick))}</strong>
    <small>{day.note ?? (change === null ? "—" : `${change > 0 ? "+" : ""}${change.toFixed(2)}%`)}</small>
  </span>;
}

/** 图表 · 小: 最新价、涨跌和这一周期的走势小线. */
export function ChartSmall({ market, name, onOpen }: { market: MarketWorkbenchSnapshot; name: string; onOpen: () => void }) {
  const closes = market.bars.slice(-60).map(bar => bar.close);
  const low = Math.min(...closes), high = Math.max(...closes);
  const points = closes.length > 1 ? closes.map((close, index) =>
    `${(index / (closes.length - 1) * 100).toFixed(2)},${(high === low ? 50 : (high - close) / (high - low) * 100).toFixed(2)}`).join(" ") : "";
  const rising = closes.length > 1 && closes.at(-1)! >= closes[0];
  return <button type="button" className="board-tile-open board-chart-small" aria-label={`${name} 走势，打开图表`} onClick={onOpen}>
    <span className="board-tile-head"><strong>{name}</strong><small>{durationLabel(market.durationSeconds)}</small></span>
    <DayPrice market={market} big />
    <svg className="board-spark" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
      {points ? <polyline points={points} data-direction={rising ? "up" : "down"} vectorEffect="non-scaling-stroke" /> : null}
    </svg>
  </button>;
}

export type PassiveChartProps = Omit<ComponentProps<typeof TradingCharts>, "bars" | "symbol" | "priceTick" | "durationSeconds" | "passive">;

/** 图表 · 中: 干净的 K 线，没有工具栏，只标周期. */
export function ChartMedium({ market, name, chart, onOpen }: { market: MarketWorkbenchSnapshot; name: string; chart: PassiveChartProps; onOpen: () => void }) {
  return <div className="board-chart-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label={`${name}，打开完整图表`} onClick={onOpen}>
      <strong>{name}</strong><DayPrice market={market} /><small>{durationLabel(market.durationSeconds)}</small>
    </button>
    <div className="board-chart-body">
      <TradingCharts passive helpAvailable={false} {...chart} bars={market.bars} symbol={market.quote.symbol}
        priceTick={market.quote.priceTick} durationSeconds={market.durationSeconds} />
    </div>
  </div>;
}

const positionName = (position: AccountPosition, instruments?: InstrumentCatalog) => position.instrumentName ?? contractLabel(position.symbol, instruments);
/** Foreign rows are dollars; the totals here are the yuan account's (the full panel shows both). */
const yuan = (positions: AccountPosition[]) => positions.filter(position => position.currency !== "USD");

/** 持仓 · 小: 总浮盈和持仓个数. */
export function PositionsSmall({ positions, onOpen }: { positions: AccountPosition[]; onOpen: () => void }) {
  const own = yuan(positions);
  const total = own.reduce((sum, position) => sum + position.unrealizedPnl, 0);
  const winning = own.filter(position => position.unrealizedPnl > 0).length;
  const losing = own.filter(position => position.unrealizedPnl < 0).length;
  return <button type="button" className="board-tile-open board-positions-small" aria-label={`持仓 ${positions.length} 个，打开持仓面板`} onClick={onOpen}>
    <span className="board-tile-head"><strong>持仓</strong><small>{positions.length} 个</small></span>
    {positions.length ? <>
      <small className="board-caption">浮动盈亏</small>
      <strong className={`board-figure ${signClass(total)}`}>{formatSigned(total, 0)}</strong>
      <span className="board-split"><span className="positive">{winning} 个赚</span><span className="negative">{losing} 个亏</span></span>
    </> : <span className="board-empty">没有持仓</span>}
  </button>;
}

/** 持仓 · 中: 每个持仓一行，不放按钮（点一行打开持仓面板再平）. */
export function PositionsMedium({ positions, instruments, onOpen }: { positions: AccountPosition[]; instruments?: InstrumentCatalog; onOpen: () => void }) {
  const total = yuan(positions).reduce((sum, position) => sum + position.unrealizedPnl, 0);
  return <div className="board-positions-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label="打开持仓面板" onClick={onOpen}>
      <strong>持仓</strong><small>{positions.length}</small><span className="board-grow" />
      {positions.length ? <><small>浮动盈亏</small><strong className={signClass(total)}>{formatSigned(total, 0)}</strong></> : null}
    </button>
    {positions.length ? <ul className="board-rows">
      {positions.map(position => <li key={`${position.accountId ?? ""}:${position.symbol}:${position.direction}`}>
        <button type="button" onClick={onOpen} aria-label={`${positionName(position, instruments)} ${position.direction === "long" ? "多" : "空"} ${position.volume} 手，打开持仓面板`}>
          <strong>{positionName(position, instruments)}</strong>
          <span><i className={`board-side ${position.direction}`}>{position.direction === "long" ? "多" : "空"}</i>{position.volume}手</span>
          <small>均价 {formatNumber(position.openPrice, 2).replace(/\.?0+$/, "")}</small>
          <small>现价 {formatNumber(position.lastPrice, 2).replace(/\.?0+$/, "")}</small>
          <strong className={signClass(position.unrealizedPnl)}>{formatSigned(position.unrealizedPnl, 0)}{position.currency === "USD" ? " 美元" : ""}</strong>
        </button>
      </li>)}
    </ul> : <p className="board-empty">没有持仓</p>}
  </div>;
}

type Quick = ReturnType<typeof useQuickOrders>;
const PRICE_INTENTS: { value: ManualSimulationRequest["priceIntent"]; label: string }[] = [
  { value: "aggressive", label: "对手价" }, { value: "passive", label: "排队价" }, { value: "last", label: "最新价" }];

/** 下单 · 小 / 中: 买多 / 卖空 两个大按钮和手数（中档多价格方式和平仓）. Every click opens the confirm card first. */
export function OrderCompact({ market, name, positions, quick, enabled, blockedReason, closeRequests, medium }: {
  market: MarketWorkbenchSnapshot; name: string; positions: AccountPosition[]; quick: Quick;
  enabled: boolean; blockedReason?: string; closeRequests: ManualSimulationRequest[]; medium?: boolean;
}) {
  const [volume, setVolume] = useState(1);
  const [intent, setIntent] = useState<ManualSimulationRequest["priceIntent"]>("aggressive");
  const quote = market.quote;
  const decimals = decimalsForTick(quote.priceTick);
  const locked = !enabled || quick.quickBusy || Boolean(quick.pending) || foreignQuoteOnly(quote.symbol);
  const tooMany = quote.openLimit != null && quote.openLimit > 0 && volume > quote.openLimit;
  const open = (side: "buy_open" | "sell_open") => void quick.requestAction({ kind: "orders",
    requests: [{ side, volume, priceIntent: shownIntent, source: "three-key" }] }, { confirm: true });
  const held = positions.filter(position => sameContract(position.symbol, quote.symbol));
  const shownIntent = medium ? intent : "aggressive";
  // 对手价 buys at 卖一, 排队价 at 买一 (the other way round for a sell), 最新价 at the last price.
  const price = (side: "buy" | "sell") => formatNumber(shownIntent === "last" ? quote.lastPrice
    : (side === "buy") === (shownIntent === "aggressive") ? quote.askPrice1 : quote.bidPrice1, decimals);
  return <div className={`board-order${medium ? " medium" : ""}`} aria-label={`${name} 下单`}>
    <div className="board-order-controls">
      <span className="board-tile-head"><strong>{name}</strong>{medium ? <><small>卖一 {formatNumber(quote.askPrice1, decimals)}</small><small>买一 {formatNumber(quote.bidPrice1, decimals)}</small></> : null}</span>
      {medium ? <div className="board-segment" role="radiogroup" aria-label="价格方式">
        {PRICE_INTENTS.map(item => <button type="button" role="radio" key={item.value} aria-checked={intent === item.value} onClick={() => setIntent(item.value)}>{item.label}</button>)}
      </div> : null}
      <div className="board-stepper" role="group" aria-label="手数">
        <button type="button" aria-label="少一手" disabled={volume <= 1} onClick={() => setVolume(value => Math.max(1, value - 1))}>−</button>
        <span><strong>{volume}</strong> 手</span>
        <button type="button" aria-label="多一手" disabled={volume >= 99} onClick={() => setVolume(value => Math.min(99, value + 1))}>+</button>
      </div>
    </div>
    <div className="board-order-buttons">
      <button type="button" className="board-buy" disabled={locked || tooMany} onClick={() => open("buy_open")}><strong>买多</strong><small>{price("buy")}</small></button>
      <button type="button" className="board-sell" disabled={locked || tooMany} onClick={() => open("sell_open")}><strong>卖空</strong><small>{price("sell")}</small></button>
      {medium ? <button type="button" className="board-close" disabled={locked || !closeRequests.length}
        onClick={() => quick.requestAction({ kind: "orders", requests: closeRequests }, { confirm: true })}>
        平仓{held.length ? `（${held.map(position => `${position.direction === "long" ? "多" : "空"} ${position.volume} 手`).join("、")}）` : ""}
      </button> : null}
    </div>
    {blockedReason || quick.quickMessage ? <small className="board-note" role="status" title={blockedReason || quick.quickMessage}>{blockedReason || quick.quickMessage}</small> : null}
  </div>;
}

export interface WatchRow { symbol: string; name: string; price: string; changePercent: number | null; note?: string }
/** The 自选 products' main-contract rows from the 报价总表, in the order the user starred them. */
export function watchRows(rows: MarketBoardRow[] | undefined, watchlist: string[]): WatchRow[] {
  const byProduct = new Map((rows ?? []).map(row => [`${row.exchangeId}.${row.productId}`, row]));
  return watchlist.flatMap(product => {
    const row = byProduct.get(product);
    if (!row) return [];
    const day = dayQuote(row);
    return [{ symbol: row.symbol, name: row.productName, price: formatBoardPrice(day.price), changePercent: day.changePercent, note: day.note ?? undefined }];
  });
}
const pct = (row: WatchRow) => row.note ?? (row.changePercent === null ? "—" : `${row.changePercent > 0 ? "+" : ""}${row.changePercent.toFixed(2)}%`);

/** 自选 · 小 (三个品种) / 中 (两栏八个); medium rows switch the contract every module follows. */
export function WatchCompact({ rows, selectedSymbol, onSelect, onOpen, medium, emptyText }: {
  rows: WatchRow[]; selectedSymbol?: string; onSelect: (symbol: string) => void; onOpen: () => void; medium?: boolean; emptyText: string;
}) {
  const shown = rows.slice(0, medium ? 8 : 3);
  const list = shown.map(row => <li key={row.symbol}>{medium
    ? <button type="button" aria-current={sameContract(row.symbol, selectedSymbol ?? "") || undefined} onClick={() => onSelect(row.symbol)}>
      <strong>{row.name}</strong><span className={signClass(row.changePercent)}>{row.price}</span><small className={signClass(row.changePercent)}>{pct(row)}</small></button>
    : <><strong>{row.name}</strong><span className={signClass(row.changePercent)}>{row.price}</span><small className={`board-chip ${signClass(row.changePercent)}`}>{pct(row)}</small></>}</li>);
  if (!medium) return <button type="button" className="board-tile-open board-watch-small" aria-label="自选，打开报价总表" onClick={onOpen}>
    <span className="board-tile-head"><strong>自选</strong></span>
    {shown.length ? <ul className="board-rows">{list}</ul> : <span className="board-empty">{emptyText}</span>}
  </button>;
  return <div className="board-watch-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label="自选，打开报价总表" onClick={onOpen}><strong>自选</strong><small>{rows.length}</small></button>
    {shown.length ? <ul className="board-rows two">{list}</ul> : <p className="board-empty">{emptyText}</p>}
  </div>;
}

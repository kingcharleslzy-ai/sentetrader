import { useEffect, useLayoutEffect, useRef, useState, type ComponentProps, type ReactNode } from "react";
import { normalizePaneSizes, SUB_PANES_MAX, type PaneSizes } from "../chartIndicators";
import { foreignQuoteOnly, sameContract, type AccountPosition, type AccountSnapshot, type InstrumentCatalog, type MarketBoardRow, type MarketWorkbenchSnapshot, type RemoteTradeSnapshot,
  type SimulationOrder } from "../../api";
import { TradingCharts } from "../TradingCharts";
import { ChartContextMenu, type ChartMenuActions, type ChartMenuState } from "../futures/ChartContextMenu";
import { useDisclosureSurface } from "../../uiSurfaces";
import { durationLabel } from "../mobile/mobileMarketPrefs";
import { chartDayQuote, contractLabel, dayQuote, decimalsForTick, formatBoardPrice, formatNumber, formatSigned, signClass, FALLBACK_PRICE_TITLE, type ManualSimulationRequest } from "../futures/support";
import type { useQuickOrders } from "../futures/useQuickOrders";
import type { SheetRow } from "../BattleSheet";

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

/**
 * How many 副图 a passive chart shows (owner 11-06: rather one fewer than labels over each other): each 副图 at its
 * share of the body (less the time axis and splitters) must hold its label row and some plot — two label rows on a
 * narrow chart, where the values wrap.
 */
const panesThatFit = (width: number, height: number, sizes: PaneSizes | undefined, count: number) => {
  const need = width < 440 ? 80 : 56;
  for (let shown = count; shown > 0; shown -= 1) {
    const { main, panes } = normalizePaneSizes(sizes, shown);
    const each = (height - 26 - 5 * shown) / (main + panes.reduce((sum, share) => sum + share, 0));
    if (panes.every(share => share * each >= need)) return shown;
  }
  return 0;
};

/** 图表 · 中: 干净的 K 线，没有工具栏，只标周期; 副图 as many as its height holds (the rest come back when it grows). */
export function ChartMedium({ market, name, chart, onOpen, menuActions }: { market: MarketWorkbenchSnapshot; name: string; chart: PassiveChartProps; onOpen: () => void;
  /** A passive chart's right-click menu (the trade chart's own, acting on this block); absent: no menu of its own. */
  menuActions?: ChartMenuActions }) {
  const body = useRef<HTMLDivElement>(null);
  const [menu, setMenu] = useState<ChartMenuState>();
  useEffect(() => {
    if (!menu) return;
    const outside = (event: PointerEvent) => { if (!(event.target as Element).closest?.(".fdw-context-menu")) setMenu(undefined); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setMenu(undefined); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [menu]);
  // As a UI command: the main menu, opened over the middle of this chart.
  useDisclosureSurface("board-chart-menu", { id: chart.surfaceScope, available: Boolean(menuActions && chart.surfaceScope), state: Boolean(menu), apply: open => {
    const rect = body.current?.getBoundingClientRect();
    setMenu(open ? { target: { area: "main", price: Number(market.quote.lastPrice) || 0 }, x: (rect?.left ?? 200) + (rect?.width ?? 300) / 2, y: (rect?.top ?? 120) + 40, sub: null } : undefined);
  } });
  const [box, setBox] = useState({ width: 0, height: 0 });
  useLayoutEffect(() => {
    const node = body.current;
    if (!node) return;
    const read = () => setBox({ width: node.clientWidth, height: node.clientHeight });
    read();
    const observer = new ResizeObserver(read);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  // (A stored entry may still be the older {type} form.)
  const list = chart.preferences?.subPaneList?.map(pane => typeof pane === "string" ? pane : pane.type);
  const room = box.height ? panesThatFit(box.width, box.height, chart.settings.paneSizes, list?.length ?? 0) : list?.length ?? 0;
  // The latest 副图 show (one just added is seen at once); the older ones give way to the height.
  const hidden = list && list.length > room ? list.slice(0, list.length - room) : [];
  const shown: PassiveChartProps = hidden.length && list ? {
    ...chart, preferences: { ...chart.preferences!, subPaneList: list.slice(list.length - room) },
    // An edit on the shown 副图 keeps the ones this height hides.
    onIndicatorsChange: chart.onIndicatorsChange ? patch => chart.onIndicatorsChange!(patch.subPaneList
      ? { ...patch, subPaneList: [...hidden.filter(pane => !patch.subPaneList!.includes(pane)), ...patch.subPaneList].slice(-SUB_PANES_MAX) } : patch) : undefined,
  } : chart;
  return <div className="board-chart-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label={`${name}，打开完整图表`} onClick={onOpen}>
      <strong>{name}</strong><DayPrice market={market} /><small>{durationLabel(market.durationSeconds)}</small>
    </button>
    <div className="board-chart-body" ref={body}>
      <TradingCharts passive helpAvailable={false} {...shown} bars={market.bars} symbol={market.quote.symbol}
        priceTick={market.quote.priceTick} durationSeconds={market.durationSeconds}
        onPriceContext={menuActions ? (price, x, y) => setMenu({ target: { area: "main", price }, x, y, sub: null }) : undefined}
        onPaneContext={menuActions ? (pane, x, y) => setMenu({ target: { area: "pane", pane }, x, y, sub: null }) : undefined} />
    </div>
    {menu && menuActions ? <ChartContextMenu menu={menu} actions={menuActions} onSub={sub => setMenu(current => current ? { ...current, sub } : current)}
      onClose={() => setMenu(undefined)} /> : null}
  </div>;
}

const positionName = (position: AccountPosition, instruments?: InstrumentCatalog) => position.instrumentName ?? contractLabel(position.symbol, instruments);
/** Foreign rows are dollars; the totals here are the yuan account's (the full panel shows both). */
const yuan = (positions: AccountPosition[]) => positions.filter(position => position.currency !== "USD");

/** 持仓 · 小: 总浮盈和持仓个数. */
/**
 * 有多大地方放多少 (owner 2026-11-06): the account's secondary figures, shown under 持仓 when the block is tall enough
 * (a container query in BoardsView.css; the block's size stays what its room picked).
 */
function AccountFigures({ account, secondary }: { account?: AccountSnapshot; /** Only the figures besides 权益 and 今日 (already shown big). */ secondary?: boolean }) {
  if (!account) return null;
  const money = (value?: number) => value === undefined ? "—" : formatNumber(value, 0);
  const figures: [string, string, string?][] = [
    ["权益", money(account.equity)], ["可用", money(account.available)], ["保证金", money(account.margin)],
    ["风险度", account.riskRatio === undefined ? "—" : `${formatNumber(account.riskRatio, 1)}%`],
    ["今日", formatSigned(account.todayNetPnl ?? account.todayPnl, 0), signClass(account.todayNetPnl ?? account.todayPnl)],
  ];
  const shown = secondary ? figures.filter(([label]) => label !== "权益" && label !== "今日") : figures;
  return <span className="board-figures board-extra" aria-label="账户">{shown.map(([label, value, tone]) =>
    <span key={label}><small>{label}</small><strong className={tone}>{value}</strong></span>)}</span>;
}

export function PositionsSmall({ positions, instruments, account, onOpen }: { positions: AccountPosition[]; instruments?: InstrumentCatalog; account?: AccountSnapshot;
  onOpen: () => void }) {
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
      <AccountFigures account={account} />
      {/* A tall block lists the positions below, as many as the height holds (the rest is cut, never squeezed). */}
      <span className="board-more-rows" aria-hidden="true">{positions.map(position => <span key={`${position.accountId ?? ""}:${position.symbol}:${position.direction}`}>
        <span className="board-ellipsis">{positionName(position, instruments)} <i className={`board-side ${position.direction}`}>{position.direction === "long" ? "多" : "空"}</i>{position.volume}手</span>
        <strong className={signClass(position.unrealizedPnl)}>{formatSigned(position.unrealizedPnl, 0)}</strong>
      </span>)}</span>
    </> : <span className="board-empty">没有持仓</span>}
  </button>;
}

/** 持仓 · 中: 每个持仓一行，不放按钮（点一行打开持仓面板再平）. */
export function PositionsMedium({ positions, instruments, account, onOpen }: { positions: AccountPosition[]; instruments?: InstrumentCatalog; account?: AccountSnapshot;
  onOpen: () => void }) {
  const total = yuan(positions).reduce((sum, position) => sum + position.unrealizedPnl, 0);
  return <div className="board-positions-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label="打开持仓面板" onClick={onOpen}>
      <strong>持仓</strong><small>{positions.length}</small><span className="board-grow" />
      {positions.length ? <><small>浮动盈亏</small><strong className={signClass(total)}>{formatSigned(total, 0)}</strong></> : null}
    </button>
    <AccountFigures account={account} />
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

/** 持仓 · 大 (owner 2026-11-05): the tile's own head (no panel title bar), the positions table with 平仓, then the working
 *  orders (撤单) and today's fills side by side. Every row scrolls inside the tile. */
export function PositionsLarge({ positions, instruments, orders, fills, onOpen, onClose, onCancel }: { positions: AccountPosition[];
  instruments?: InstrumentCatalog; orders: SimulationOrder[]; fills: RemoteTradeSnapshot[]; onOpen: () => void;
  onClose: (position: AccountPosition) => void; onCancel: (order: SimulationOrder) => void }) {
  const total = yuan(positions).reduce((sum, position) => sum + position.unrealizedPnl, 0);
  const price = (value?: number | null) => value == null ? "—" : formatNumber(value, 2).replace(/\.?0+$/, "");
  const time = (ns: number) => new Date(ns / 1e6).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false, hour: "2-digit", minute: "2-digit" });
  const side = (direction: string, offset: string) => `${direction === "BUY" ? "买" : "卖"}${offset === "OPEN" ? "开" : "平"}`;
  return <div className="board-positions-large">
    <div className="board-tile-head board-large-head">
      <strong>持仓</strong><small>{positions.length}</small>
      {positions.length ? <><small>浮动盈亏</small><strong className={signClass(total)}>{formatSigned(total, 0)}</strong></> : null}
      <span className="board-grow" /><button type="button" className="board-text-button" onClick={onOpen}>打开持仓面板</button>
    </div>
    <div className="board-large-scroll">
      {positions.length ? <table className="board-table">
        <thead><tr><th>合约</th><th>方向手数</th><th>均价</th><th>现价</th><th className="num">浮动盈亏</th><th /></tr></thead>
        <tbody>{positions.map(position => <tr key={`${position.accountId ?? ""}:${position.symbol}:${position.direction}`}>
          <td><strong>{positionName(position, instruments)}</strong></td>
          <td><i className={`board-side ${position.direction}`}>{position.direction === "long" ? "多" : "空"}</i>{position.volume}手</td>
          <td>{price(position.openPrice)}</td><td>{price(position.lastPrice)}</td>
          <td className={`num ${signClass(position.unrealizedPnl)}`}><strong>{formatSigned(position.unrealizedPnl, 0)}{position.currency === "USD" ? " 美元" : ""}</strong></td>
          <td className="num"><button type="button" className="app-button" onClick={() => onClose(position)}>平仓</button></td>
        </tr>)}</tbody>
      </table> : <p className="board-empty">没有持仓</p>}
      <div className="board-large-pair">
        <section><h4>挂着的委托<small>{orders.length}</small></h4>
          {orders.length ? orders.map(order => <div key={order.id} className="board-large-line">
            <strong>{contractLabel(order.request.symbol, instruments)}</strong>
            <span className={order.request.direction === "BUY" ? "positive" : "negative"}>{side(order.request.direction, order.request.offset)} {order.request.volume}手</span>
            <small>@{price(order.request.limitPrice)}</small><span className="board-grow" />
            <button type="button" className="app-button" disabled={order.cancelPhase !== "NONE"} onClick={() => onCancel(order)}>撤单</button>
          </div>) : <small className="board-caption">没有挂单</small>}
        </section>
        <section><h4>今日成交<small>{fills.length}</small></h4>
          {fills.length ? fills.slice(-6).reverse().map(fill => <div key={fill.tradeId} className="board-large-line">
            <small>{time(fill.tradeDateTime)}</small><strong>{contractLabel(fill.symbol, instruments)}</strong>
            <span className={fill.direction === "BUY" ? "positive" : "negative"}>{side(fill.direction, fill.offset)} {fill.volume}手</span>
            <span className="board-grow" /><span>{price(fill.price)}</span>
          </div>) : <small className="board-caption">今天还没有成交</small>}
        </section>
      </div>
    </div>
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
  // 最新 and this contract's 持仓: beside the controls at the medium size; below the buttons at the small one, once the
  // block is tall enough for them (the buttons keep a button's height).
  const extra = <span className={`board-more-rows board-order-extra${medium ? "" : " board-order-more"}`} aria-hidden="true">
    <span><small>最新</small><strong>{formatNumber(quote.lastPrice, decimals)}</strong></span>
    {held.length ? held.map(position => <span key={`${position.symbol}:${position.direction}`}>
      <small>持仓</small><span className="board-ellipsis"><i className={`board-side ${position.direction}`}>{position.direction === "long" ? "多" : "空"}</i>{position.volume}手</span>
      <strong className={signClass(position.unrealizedPnl)}>{formatSigned(position.unrealizedPnl, 0)}</strong></span>)
      : <span><small>持仓</small><span className="board-ellipsis">这个合约没有持仓</span></span>}
  </span>;
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
      {medium ? extra : null}
    </div>
    <div className="board-order-buttons">
      <button type="button" className="board-buy" disabled={locked || tooMany} onClick={() => open("buy_open")}><strong>买多</strong><small>{price("buy")}</small></button>
      <button type="button" className="board-sell" disabled={locked || tooMany} onClick={() => open("sell_open")}><strong>卖空</strong><small>{price("sell")}</small></button>
      {medium ? <button type="button" className="board-close" disabled={locked || !closeRequests.length}
        onClick={() => quick.requestAction({ kind: "orders", requests: closeRequests }, { confirm: true })}>
        平仓{held.length ? `（${held.map(position => `${position.direction === "long" ? "多" : "空"} ${position.volume} 手`).join("、")}）` : ""}
      </button> : null}
    </div>
    {medium ? null : extra}
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

/** 自选 · 小 (一栏) / 中 (两栏), as many rows as fit; medium rows switch the contract their colour follows. */
export function WatchCompact({ rows, selectedSymbol, onSelect, onOpen, medium, emptyText }: {
  rows: WatchRow[]; selectedSymbol?: string; onSelect: (symbol: string) => void; onOpen: () => void; medium?: boolean; emptyText: string;
}) {
  // As many rows as the block's height holds (the list is cut at the edge, never squeezed).
  const shown = rows;
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

/** The 作战表 row of the shown contract: its own main contract, else the product it belongs to. */
function sheetRowFor(rows: SheetRow[], symbol: string | undefined): SheetRow | undefined {
  if (!symbol) return undefined;
  const lower = symbol.toLowerCase();
  return rows.find(row => row.symbol && sameContract(row.symbol, symbol))
    ?? rows.find(row => lower.startsWith(row.product.toLowerCase()) && /\d/.test(lower.charAt(row.product.length)));
}
const nearestText = (row: SheetRow) => row.nearest ? `离${row.nearest.label} ${row.nearest.distancePercent}%` : "";

/** 作战表 · 小 (当前品种今日判断一句) / 中 (每个品种一行). Both open the 调研 panel's 作战表 (a row: that 品种's 调研). */
export function SheetCompact({ rows, status, symbol, name, onOpen, onOpenRow, medium }: {
  rows: SheetRow[]; status: string; symbol?: string; name: string; onOpen: () => void; onOpenRow: (symbol: string) => void; medium?: boolean;
}) {
  if (!medium) {
    const row = sheetRowFor(rows, symbol);
    return <button type="button" className="board-tile-open board-sheet-small board-read" aria-label={`${name} 今日判断，打开作战表`} onClick={onOpen}>
      <span className="board-tile-head"><strong>{row?.name ?? name}</strong><small>今日判断</small><span className="board-grow" />
        {row ? <i className="board-priority" data-priority={row.priority}>{row.priority}</i> : null}</span>
      <span className="board-sentence">{row ? row.judgment?.headline ?? "今天还没有判断" : status || "这个品种不在作战表里（作战表排自选品种）"}</span>
      {row && nearestText(row) ? <small className="board-caption">{nearestText(row)}</small> : null}
      {rows.length > 1 ? <span className="board-more-rows" aria-hidden="true">{rows.filter(other => other !== row).map(other => <span key={other.product}>
        <strong>{other.name}</strong><i className="board-priority" data-priority={other.priority}>{other.priority}</i>
        <span className="board-wrap">{other.judgment?.headline ?? "暂无判断"}</span></span>)}</span> : null}
    </button>;
  }
  return <div className="board-sheet-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label="今日作战表，打开作战表" onClick={onOpen}><strong>今日作战表</strong><small>{rows.length || ""}</small></button>
    {rows.length ? <ul className="board-rows board-read">{rows.map(row => <li key={row.product}>
      <button type="button" aria-label={`${row.name}：${row.judgment?.headline ?? "暂无判断"}，看调研`} onClick={() => row.symbol ? onOpenRow(row.symbol) : onOpen()}>
        <strong>{row.name}</strong><i className="board-priority" data-priority={row.priority}>{row.priority}</i>
        <span className="board-wrap">{row.judgment?.headline ?? "暂无判断"}</span><small>{nearestText(row)}</small>
      </button></li>)}</ul> : <p className="board-empty">{status}</p>}
  </div>;
}

export interface WaitingAlert { key: string; name: string; text: string }
/** 提醒 · 小 (在等几个 + 最近响的一个) / 中 (列表). Both open the 提醒 panel. */
export function AlertCompact({ waiting, latest, onOpen, medium }: {
  waiting: WaitingAlert[]; latest?: { title: string; at: string }; onOpen: () => void; medium?: boolean;
}) {
  if (!medium) return <button type="button" className="board-tile-open board-alert-small board-read" aria-label={`提醒在等 ${waiting.length} 个，打开提醒面板`} onClick={onOpen}>
    <span className="board-tile-head"><strong>提醒</strong></span>
    <span className="board-count"><strong className="board-figure">{waiting.length}</strong><small>个在等</small></span>
    {waiting.length ? <span className="board-more-rows" aria-hidden="true">{waiting.map(item => <span key={item.key}>
      <span className="board-wrap"><strong>{item.name}</strong> {item.text}</span></span>)}</span> : null}
    {latest ? <span className="board-latest"><small>{latest.at} 响过</small><strong>{latest.title}</strong></span> : <small className="board-caption">今天还没响过</small>}
  </button>;
  return <div className="board-alert-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label="提醒，打开提醒面板" onClick={onOpen}>
      <strong>提醒</strong><small>{waiting.length} 个在等{latest ? ` · ${latest.at} 响过` : ""}</small></button>
    {waiting.length ? <ul className="board-rows board-read">{waiting.map(item => <li key={item.key}>
      <button type="button" aria-label={`${item.name} ${item.text}，打开提醒面板`} onClick={onOpen}><strong>{item.name}</strong><span className="board-wrap">{item.text}</span><small>在等</small></button>
    </li>)}</ul> : <p className="board-empty">没有在等的提醒：在图表上设到价提醒，或在提醒面板里新建</p>}
  </div>;
}

/** 对话 · 一条 (输入框和发送) / 中 (最近一条回复 + 输入框): what is typed goes to the same conversation as the 对话 panel. */
export function ChatCompact({ turns, busy, blockedReason, onSend, onOpen, medium }: {
  /** The conversation's recent turns, oldest first; the block shows as many of the latest as fit. */
  turns: { key: string; role: "user" | "assistant"; text: string; at: string }[]; busy: boolean; blockedReason?: string;
  onSend: (text: string) => void; onOpen: () => void; medium?: boolean;
}) {
  const reply = [...turns].reverse().find(turn => turn.role === "assistant");
  const [text, setText] = useState("");
  const send = () => { const value = text.trim(); if (!value || busy || blockedReason) return; onSend(value); setText(""); };
  const input = <form className="board-chat-input" onSubmit={event => { event.preventDefault(); send(); }}>
    <input value={text} onChange={event => setText(event.target.value)} placeholder={blockedReason || "问 AI，或说一句要做的事"} aria-label="给 AI 发消息" disabled={Boolean(blockedReason)} />
    <button type="submit" className="app-button primary" disabled={!text.trim() || busy || Boolean(blockedReason)}>{busy ? "回答中" : "发送"}</button>
  </form>;
  if (!medium) return <div className="board-chat-bar">{input}</div>;
  return <div className="board-chat-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label="对话，打开对话面板" onClick={onOpen}><strong>对话</strong>{reply ? <small>AI · {reply.at}</small> : null}</button>
    {turns.length ? <ol className="board-turns board-read" aria-label="最近的对话">{[...turns].reverse().map(turn => <li key={turn.key} data-role={turn.role}>
      <p>{turn.text}</p></li>)}</ol> : <p className="board-reply">还没有对话：在下面问一句</p>}
    {input}
  </div>;
}

/** 账户权益 · 小 (权益和今日) / 中 · 大 (账户的几个数，美元子账户另一行): opens 账户明细. */
export function AccountCompact({ account, foreign, onOpen, medium }: { account?: AccountSnapshot; foreign?: AccountSnapshot; onOpen: () => void; medium?: boolean }) {
  if (!account) return <p className="board-empty" role="status">账户读取中</p>;
  const today = account.todayNetPnl ?? account.todayPnl;
  if (!medium) return <button type="button" className="board-tile-open board-account-small" aria-label={`权益 ${formatNumber(account.equity, 0)}，打开账户明细`} onClick={onOpen}>
    <span className="board-tile-head"><strong>账户权益</strong><small>{account.accountName}</small></span>
    <strong className="board-figure">{formatNumber(account.equity, 0)}</strong>
    <span className="board-split"><small>今日</small><span className={signClass(today)}>{formatSigned(today, 0)}</span></span>
    <AccountFigures account={account} secondary />
  </button>;
  const row = (snapshot: AccountSnapshot, label: string) => {
    const money = (value?: number) => value === undefined ? "—" : formatNumber(value, 0);
    const day = snapshot.todayNetPnl ?? snapshot.todayPnl;
    return <span className="board-figures" aria-label={label}>{([["权益", money(snapshot.equity)], ["可用", money(snapshot.available)], ["保证金", money(snapshot.margin)],
      ["风险度", snapshot.riskRatio === undefined ? "—" : `${formatNumber(snapshot.riskRatio, 1)}%`], ["今日", formatSigned(day, 0), signClass(day)],
      ["持仓", `${snapshot.positionCount} 个`]] as [string, string, string?][]).map(([name, value, tone]) =>
      <span key={name}><small>{name}</small><strong className={tone}>{value}</strong></span>)}</span>;
  };
  return <div className="board-account-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label="账户权益，打开账户明细" onClick={onOpen}><strong>账户权益</strong><small>{account.accountName}</small></button>
    <div className="board-account-rows">{row(account, "人民币")}{foreign ? <><small className="board-caption">美元子账户</small>{row(foreign, "美元")}</> : null}</div>
  </div>;
}

/** 委托与成交 · 小 (挂单几笔、今天成交几笔) / 中 · 大 (挂着的委托带撤单，今日成交): the 持仓 panel's 委托 / 成交 tabs. */
export function OrdersCompact({ orders, fills, instruments, onOpen, onCancel, medium }: { orders: SimulationOrder[]; fills: RemoteTradeSnapshot[];
  instruments?: InstrumentCatalog; onOpen: () => void; onCancel: (order: SimulationOrder) => void; medium?: boolean }) {
  const price = (value?: number | null) => value == null ? "—" : formatNumber(value, 2).replace(/\.?0+$/, "");
  const side = (direction: string, offset: string) => `${direction === "BUY" ? "买" : "卖"}${offset === "OPEN" ? "开" : "平"}`;
  const time = (ns: number) => new Date(ns / 1e6).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false, hour: "2-digit", minute: "2-digit" });
  if (!medium) return <button type="button" className="board-tile-open board-orders-small" aria-label={`挂单 ${orders.length} 笔，今日成交 ${fills.length} 笔，打开委托`} onClick={onOpen}>
    <span className="board-tile-head"><strong>委托与成交</strong></span>
    <span className="board-split"><span><strong className="board-figure">{orders.length}</strong> 笔挂单</span><span><strong className="board-figure">{fills.length}</strong> 笔成交</span></span>
    <span className="board-more-rows" aria-hidden="true">{[...fills].reverse().map(fill => <span key={fill.tradeId}><small>{time(fill.tradeDateTime)}</small>
      <span className="board-ellipsis">{contractLabel(fill.symbol, instruments)} {side(fill.direction, fill.offset)} {fill.volume}手</span><strong>{price(fill.price)}</strong></span>)}</span>
  </button>;
  return <div className="board-orders-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label="委托与成交，打开委托" onClick={onOpen}><strong>委托与成交</strong>
      <small>{orders.length} 笔挂单 · {fills.length} 笔成交</small></button>
    <div className="board-large-pair">
      <section><h4>挂着的委托<small>{orders.length}</small></h4>
        {orders.length ? orders.map(order => <div key={order.id} className="board-large-line">
          <strong>{contractLabel(order.request.symbol, instruments)}</strong>
          <span className={order.request.direction === "BUY" ? "positive" : "negative"}>{side(order.request.direction, order.request.offset)} {order.request.volume}手</span>
          <small>@{price(order.request.limitPrice)}</small><span className="board-grow" />
          <button type="button" className="app-button" disabled={order.cancelPhase !== "NONE"} onClick={() => onCancel(order)}>撤单</button>
        </div>) : <small className="board-caption">没有挂单</small>}
      </section>
      <section><h4>今日成交<small>{fills.length}</small></h4>
        {fills.length ? [...fills].reverse().map(fill => <div key={fill.tradeId} className="board-large-line">
          <small>{time(fill.tradeDateTime)}</small><strong>{contractLabel(fill.symbol, instruments)}</strong>
          <span className={fill.direction === "BUY" ? "positive" : "negative"}>{side(fill.direction, fill.offset)} {fill.volume}手</span>
          <span className="board-grow" /><span>{price(fill.price)}</span>
        </div>) : <small className="board-caption">今天还没有成交</small>}
      </section>
    </div>
  </div>;
}

export interface RunningStrategy { id: string; name: string; status: string; reminderOnly: boolean; note?: string }
const strategyStatus = (status: string) => status === "monitoring" ? "运行中" : status === "paused" ? "已暂停" : status === "stopping" ? "停止中" : status;
/** 运行中的策略 · 小 (几个在跑、几个暂停) / 中 (每个一行): 暂停 / 继续 / 停止并平仓 are the 策略 panel's own (its confirmations). */
export function StrategiesCompact({ items, onOpen, medium }: { items: RunningStrategy[]; onOpen: () => void; medium?: boolean }) {
  const running = items.filter(item => item.status === "monitoring").length;
  if (!medium) return <button type="button" className="board-tile-open board-strategies-small" aria-label={`策略 ${running} 个运行中，打开运行中的策略`} onClick={onOpen}>
    <span className="board-tile-head"><strong>运行中的策略</strong></span>
    <span className="board-count"><strong className="board-figure">{running}</strong><small>个在跑{items.length > running ? ` · ${items.length - running} 个暂停或停止中` : ""}</small></span>
    <span className="board-more-rows" aria-hidden="true">{items.map(item => <span key={item.id}><span className="board-ellipsis">{item.name}</span>
      <small>{item.reminderOnly ? "只提醒" : strategyStatus(item.status)}</small></span>)}</span>
  </button>;
  return <div className="board-strategies-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label="运行中的策略，打开策略面板" onClick={onOpen}><strong>运行中的策略</strong><small>{running} 个在跑</small></button>
    {items.length ? <ul className="board-rows">{items.map(item => <li key={item.id}>
      <button type="button" aria-label={`${item.name} ${strategyStatus(item.status)}，打开策略面板`} onClick={onOpen}>
        <strong>{item.name}</strong><span className="board-ellipsis">{item.note ?? ""}</span><small>{item.reminderOnly ? "只提醒" : strategyStatus(item.status)}</small>
      </button></li>)}</ul> : <p className="board-empty">没有上线的策略：在策略面板里上线一个</p>}
  </div>;
}

/** 快讯 · 小: 今天几条、最新的几条标题 (the full feed, pushed); 中 / 大 are the 调研 panel's 快讯 itself. */
export function FlashSmall({ items, onOpen }: { items?: { id: string; title: string; text: string; publishedAt: string }[]; onOpen: () => void }) {
  const time = (at: string) => new Date(at).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false, hour: "2-digit", minute: "2-digit" });
  return <button type="button" className="board-tile-open board-flash-small board-read" aria-label="快讯，打开调研的快讯" onClick={onOpen}>
    <span className="board-tile-head"><strong>快讯</strong><small>{items ? `近 24 小时 ${items.length} 条` : "正在读取"}</small></span>
    {items?.length ? <span className="board-more-rows" aria-hidden="true">{items.map(item => <span key={item.id}><small>{time(item.publishedAt)}</small>
      <span className="board-wrap">{item.title || item.text}</span></span>)}</span> : items ? <span className="board-empty">还没有快讯</span> : null}
  </button>;
}

/** 复盘盈亏 · 小: 今日、本月平仓、累计和胜率 (the 复盘 overview's own figures); 中 / 大 are the 复盘 page's 账户盈亏 itself. */
export function PnlSmall({ pnl, error, onOpen }: { pnl?: { today: number | null; thisMonth: number; totalPnl: number | null; winRate: number | null; tradeCount: number };
  error?: string; onOpen: () => void }) {
  if (!pnl) return <p className="board-empty" role="status">{error || "复盘读取中"}</p>;
  const signed = (value: number | null) => value === null ? "—" : formatSigned(value, 0);
  return <button type="button" className="board-tile-open board-pnl-small" aria-label="复盘盈亏，打开复盘" onClick={onOpen}>
    <span className="board-tile-head"><strong>复盘盈亏</strong><small>{pnl.tradeCount} 笔</small></span>
    <span className="board-figures" aria-label="盈亏">{([["今日", pnl.today], ["本月平仓", pnl.thisMonth], ["累计", pnl.totalPnl]] as [string, number | null][]).map(([name, value]) =>
      <span key={name}><small>{name}</small><strong className={value === null ? undefined : signClass(value)}>{signed(value)}</strong></span>)}
      <span><small>胜率</small><strong>{pnl.winRate === null ? "—" : `${Math.round(pnl.winRate * 100)}%`}</strong></span></span>
  </button>;
}

const clockOf = (at?: string | null) => at ? new Date(at).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false, hour: "2-digit", minute: "2-digit" }) : "";
const dayOf = (at?: string | null) => at ? new Date(at).toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit" }).replace("/", "-") : "";

/** One debate as the board shows it: the product's latest (summary), and its reports and verdict (the medium size). */
export interface BoardDebate {
  id: string; name: string; status: string; finishedAt: string | null; createdAt: string; stance: string | null; confidence: string | null;
  headline?: string; reports?: { title: string; text: string }[];
}
/** 多 AI 辩论 · 小 (当前品种最近一次的结论和时间) / 中 (加四位分析师的要点和裁判结论); 大 is the 调研 panel's 辩论 page. */
export function DebateCompact({ debate, name, loading, onOpen, medium }: { debate?: BoardDebate | null; name: string; loading: boolean;
  onOpen: () => void; medium?: boolean }) {
  const when = debate ? `${dayOf(debate.finishedAt ?? debate.createdAt)} ${clockOf(debate.finishedAt ?? debate.createdAt)}` : "";
  const verdict = debate ? debate.status === "done" || debate.status === "review" ? `${debate.stance ?? "看不清"}${debate.confidence ? ` · 信心${debate.confidence}` : ""}`
    : debate.status === "running" || debate.status === "queued" ? "正在辩" : "这次没辩完" : "";
  const head = <span className="board-tile-head"><strong>多 AI 辩论</strong><small>{name}{when ? ` · ${when}` : ""}</small></span>;
  if (!medium) return <button type="button" className="board-tile-open board-debate-small board-read" aria-label={`${name}的多 AI 辩论，打开辩论页`} onClick={onOpen}>
    {head}
    {debate ? <><strong className="board-sentence">{verdict}</strong>{debate.headline ? <span className="board-wrap">{debate.headline}</span> : null}</>
      : <span className="board-empty">{loading ? "正在读取" : "这个品种还没辩过：点开发起一场"}</span>}
  </button>;
  return <div className="board-debate-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label={`${name}的多 AI 辩论，打开辩论页`} onClick={onOpen}>
      <strong>多 AI 辩论</strong><small>{name}{when ? ` · ${when}` : ""}</small></button>
    {debate ? <div className="board-debate-body board-read">
      <p className="board-debate-verdict"><strong>{verdict}</strong>{debate.headline ? ` ${debate.headline}` : ""}</p>
      {debate.reports?.length ? <span className="board-more-rows" aria-hidden="true">{debate.reports.map(report => <span key={report.title}>
        <small>{report.title}</small><span className="board-wrap">{report.text}</span></span>)}</span> : null}
    </div> : <p className="board-empty">{loading ? "正在读取" : "这个品种还没辩过：点开辩论页发起一场"}</p>}
  </div>;
}

/** 交易笔记 · 小 (几条、最近一条的名字) / 中 (每条一行); 大 is the 复盘 page's 交易笔记 itself. */
export function NotesCompact({ items, loaded, onOpen, medium }: { items: { id: string; name: string; entryRules: string; updatedAt: string }[]; loaded: boolean;
  onOpen: () => void; medium?: boolean }) {
  const latest = [...items].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  if (!medium) return <button type="button" className="board-tile-open board-notes-small board-read" aria-label={`交易笔记 ${items.length} 条，打开交易笔记`} onClick={onOpen}>
    <span className="board-tile-head"><strong>交易笔记</strong></span>
    <span className="board-count"><strong className="board-figure">{items.length}</strong><small>条</small></span>
    {latest[0] ? <span className="board-ellipsis">最近：{latest[0].name}</span> : <span className="board-empty">{loaded ? "还没有交易笔记：点开新建" : "正在读取"}</span>}
    <span className="board-more-rows" aria-hidden="true">{latest.slice(1).map(item => <span key={item.id}><span className="board-ellipsis">{item.name}</span></span>)}</span>
  </button>;
  return <div className="board-notes-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label="交易笔记，打开交易笔记" onClick={onOpen}><strong>交易笔记</strong><small>{items.length} 条</small></button>
    {latest.length ? <ul className="board-rows board-read">{latest.map(item => <li key={item.id}>
      <button type="button" aria-label={`${item.name}，打开交易笔记`} onClick={onOpen}><strong>{item.name}</strong>
        <span className="board-wrap">{item.entryRules}</span><small>{dayOf(item.updatedAt)}</small></button></li>)}</ul>
      : <p className="board-empty">{loaded ? "还没有交易笔记：点开复盘的交易笔记新建" : "正在读取"}</p>}
  </div>;
}

/** AI 简报 · 小 (订阅了几个品种、最近一条的时间和标题) / 中 (最近几条) / 大 (加“生成简讯”开关, the 设置 → 通知 one). */
export function BriefsCompact({ products, items, onOpen, medium, toggle }: { products?: number; items?: { id: string; text: string; publishedAt: string; productName?: string | null }[];
  onOpen: () => void; medium?: boolean; toggle?: ReactNode }) {
  const latest = items?.[0];
  if (!medium) return <button type="button" className="board-tile-open board-briefs-small board-read" aria-label={`AI 简报，订阅 ${products ?? 0} 个品种，打开通知设置`} onClick={onOpen}>
    <span className="board-tile-head"><strong>AI 简报</strong><small>{products === undefined ? "" : products ? `订阅了 ${products} 个品种` : "还没订阅"}</small></span>
    {latest ? <><small className="board-caption">{latest.productName ? `${latest.productName} · ` : ""}{dayOf(latest.publishedAt)} {clockOf(latest.publishedAt)}</small>
      <span className="board-sentence">{latest.text}</span></> : <span className="board-empty">{items ? "还没有简报" : "正在读取"}</span>}
  </button>;
  return <div className="board-briefs-medium">
    <button type="button" className="board-tile-head board-tile-link" aria-label="AI 简报，打开通知设置" onClick={onOpen}><strong>AI 简报</strong>
      <small>{products === undefined ? "" : products ? `订阅了 ${products} 个品种` : "还没订阅"}</small></button>
    {toggle ? <div className="board-briefs-toggle">{toggle}</div> : null}
    {items?.length ? <ul className="board-rows board-read">{items.map(item => <li key={item.id}><button type="button" aria-label={`${item.productName ?? ""}简报：${item.text.slice(0, 40)}，打开通知设置`} onClick={onOpen}>
      <small>{clockOf(item.publishedAt)}</small>{item.productName ? <strong>{item.productName}</strong> : null}<span className="board-wrap">{item.text}</span>
    </button></li>)}</ul> : <p className="board-empty">{items ? "还没有简报：订阅品种后，AI 会按时写简讯" : "正在读取"}</p>}
  </div>;
}

import { ChevronDown, Search, X } from "lucide-react";
import { Fragment, useMemo } from "react";
import type { InstrumentCatalogItem, MarketBoardSnapshot } from "../../api";
import type { FuturesDecisionWorkspaceProps } from "./props";
import { BOARD_EXCHANGES, boardPrice, decimalsForTick, formatNumber, signClass, watchlistKey } from "./support";
import { deliveryBadgeText } from "./deliveryInfo";
import { WatchlistStar } from "./WatchlistStar";
import "./ContractInfoControl.css";

/** One 品种 in the drawer: its contracts by expiry, the main contract (or the
 *  nearest expiry for a product the exchange has not marked yet) as its face. */
export interface ProductGroup {
  key: string;
  exchangeId: string;
  productId: string;
  productName: string;
  main: InstrumentCatalogItem;
  hasMain: boolean;
  months: InstrumentCatalogItem[];
}

const expiry = (item: InstrumentCatalogItem) => item.deliveryYear * 100 + item.deliveryMonth;

/** The catalog as 品种 groups, by exchange then name (the drawer's first level; AI 简报订阅's picker). */
export function catalogProducts(items: readonly InstrumentCatalogItem[]): ProductGroup[] {
  const byProduct = new Map<string, ProductGroup>();
  for (const item of items) {
    const key = watchlistKey(item);
    let group = byProduct.get(key);
    if (!group) {
      group = { key, exchangeId: item.exchangeId, productId: item.productId,
        productName: (item.instrumentName.replace(/\d+$/, "") || item.productId).toUpperCase(), main: item, hasMain: false, months: [] };
      byProduct.set(key, group);
    }
    group.months.push(item);
    if (item.isMain) { group.main = item; group.hasMain = true; }
  }
  const order = new Map<string, number>(BOARD_EXCHANGES.map((entry, index) => [entry.code, index]));
  return [...byProduct.values()].map(group => {
    group.months.sort((a, b) => expiry(a) - expiry(b));
    // Without an isMain marking the catalog alone picks the face: the nearest expiry.
    if (!group.hasMain) group.main = group.months[0];
    return group;
  }).sort((a, b) => (order.get(a.exchangeId) ?? 90) - (order.get(b.exchangeId) ?? 90)
    || a.productName.localeCompare(b.productName, "zh-CN"));
}

export type ContractCatalogState = {
  status: "loading" | "error" | "ready" | "offline" | "empty" | "expired";
  /** The drawer shows 重试 (the `contractCatalogRetry` command): a failed or empty read. */
  canRetry: boolean;
};

/** Where the drawer's catalog stands — for its status lines, the snapshot (`contractCatalog`) and the
 *  `contractCatalogRetry` command. A cold-start catalog the Runtime reports as loading is loading, not
 *  unavailable; a guest's connection only turns connected with the first market snapshot, so for a guest
 *  an answered but empty catalog is the public market being unavailable, never "service unreachable". */
export function contractCatalogState(workspace: Pick<FuturesDecisionWorkspaceProps, "instruments" | "instrumentsLoading" | "instrumentsError" | "connection"> & { guest?: { expired?: boolean } }): ContractCatalogState {
  const hasItems = Boolean(workspace.instruments?.items.length);
  // A read actually in flight wins; a failed read then beats a retained catalog's own "loading" marker
  // (a cold-start poll that later failed or timed out must offer 重试, not spin forever).
  // An expired guest session is recovered by 重新连接 (a new session), never by a catalog re-read.
  if (workspace.guest?.expired) return { status: hasItems ? "ready" : "expired", canRetry: false };
  if (!hasItems && workspace.instrumentsLoading) return { status: "loading", canRetry: false };
  // A member that lost the service cannot re-read until it reconnects: offline beats a stale read error.
  if (!hasItems && !workspace.guest && workspace.connection !== "connected") return { status: "offline", canRetry: false };
  if (workspace.instrumentsError) return { status: "error", canRetry: true };
  if (hasItems) return { status: "ready", canRetry: false };
  if (workspace.instruments?.catalogStatus === "loading") return { status: "loading", canRetry: false };
  return { status: "empty", canRetry: true };
}

/** Why `contractCatalogRetry` is refused in this state (undefined = allowed). */
export function contractCatalogRetryRefusal(state: ContractCatalogState): string | undefined {
  if (state.canRetry) return undefined;
  return state.status === "loading" ? "合约目录正在读取，不用重试"
    : state.status === "offline" ? "应用服务未连接，连上后会自动读取合约目录"
    : state.status === "expired" ? "游客会话已失效，请用“重新连接”恢复" : "合约目录已读到，不用重试";
}

/** The contract picker: 品种 → 月份 two levels. A product row selects its main
 *  contract (the same entry as a chart tab); “N 个月份” expands the product.
 *  自选 (2026-10-02): each product row carries the same star the board rows do. */
export function ContractDrawer({ workspace, query, onQueryChange, onSelect, onClose, recentSymbols = [], board, product, onProductChange,
  watchlist, onToggleWatchlist, watchlistStatus }: {
  workspace: FuturesDecisionWorkspaceProps;
  query: string;
  /** Newest first; only contracts still in the catalog are offered. */
  recentSymbols?: string[];
  onQueryChange: (value: string) => void;
  onSelect: (symbol: string) => void;
  onClose: () => void;
  /** The board read the product rows' prices come from (absent → name and code only). */
  board: MarketBoardSnapshot | undefined;
  /** The one expanded product ("SHFE.rb"), null when all are collapsed. */
  product: string | null;
  onProductChange: (product: string | null) => void;
  /** The account's watchlist.products ("<交易所>.<品种>"). */
  watchlist: string[];
  /** Present (and the account savable) when the stars should toggle; absent = read-only. */
  onToggleWatchlist?: (product: string) => void;
  /** The watchlist save path's inline status (错误原因). */
  watchlistStatus?: string;
}) {
  const items = useMemo(() => workspace.instruments?.items ?? [], [workspace.instruments]);
  const needle = query.trim().toLowerCase();
  const guest = Boolean(workspace.guest);
  const catalogState = contractCatalogState(workspace);
  const catalogLoading = workspace.instrumentsLoading || catalogState.status === "loading";
  // No catalog to search (not connected, or the read came back empty); a failed read shows its own
  // error with 重试 above the list.
  const catalogMissing = catalogState.status === "offline" || catalogState.status === "empty" || catalogState.status === "expired";
  const starred = useMemo(() => new Set(watchlist), [watchlist]);

  // An exact contract code (rb2701 / SHFE.rb2701) expands its product and highlights the month.
  const exact = needle
    ? items.find(item => item.symbol.toLowerCase() === needle || item.instrumentId.toLowerCase() === needle)
    : undefined;

  const groups = useMemo(() => catalogProducts(items), [items]);

  const visible = needle
    ? groups.filter(group => `${group.productName}${group.productId}`.toLowerCase().includes(needle)
      || group.months.some(item => `${item.instrumentId}${item.symbol}`.toLowerCase().includes(needle)))
    : groups;
  // A product matched by name keeps all its months; a code query shows the matching months only.
  const monthsOf = (group: ProductGroup) => {
    if (!needle) return group.months;
    if (`${group.productName}${group.productId}`.toLowerCase().includes(needle)) return group.months;
    return group.months.filter(item => `${item.instrumentId}${item.symbol}`.toLowerCase().includes(needle));
  };

  const row = (item: InstrumentCatalogItem) => <button type="button" className={item.symbol === workspace.selectedSymbol ? "active" : ""} key={item.symbol} onClick={() => onSelect(item.symbol)}><span><strong>{item.instrumentName || item.instrumentId}</strong><small>{item.symbol}</small></span><span><b>{item.isMain ? "主力" : `${item.deliveryYear}-${String(item.deliveryMonth).padStart(2, "0")}`}</b><small>Tick {formatNumber(item.priceTick, decimalsForTick(item.priceTick))}</small></span></button>;
  // 游客的目录只有螺纹主力，同样按查询过滤：不匹配就是空态，不把固定合约当搜索结果（#476）。
  const guestRows = needle
    ? items.filter(item => `${item.instrumentName}${item.productId}${item.instrumentId}${item.symbol}`.toLowerCase().includes(needle))
    : items;
  // A guest's flat list already is its whole catalog: no 最近 above it, or 螺纹主力 shows twice (#734).
  const recent = query || guest ? [] : recentSymbols.map(symbol => items.find(item => item.symbol === symbol)).filter((item): item is InstrumentCatalogItem => Boolean(item));

  return (
  <div className="fdw-drawer-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
    <aside className="fdw-drawer contracts" role="dialog" aria-label="合约列表">
      <header><strong>合约</strong><button type="button" aria-label="关闭合约列表" onClick={() => onClose()}><X /></button></header>
      <label className="fdw-search"><Search /><input value={query} onChange={(event) => onQueryChange(event.target.value)} placeholder="搜索合约代码或名称" /></label>
      {watchlistStatus ? <div className="fdw-inline-error" role="status">{watchlistStatus}</div> : null}
      {catalogState.status === "error" ? <div className="fdw-inline-error">{workspace.instrumentsError}<button type="button" onClick={workspace.onRetryInstruments}>重试</button></div> : null}
      <div className="fdw-contract-list">
        {recent.length ? <><h4>最近</h4>{recent.map(row)}</> : null}
        {guest ? (
          // 游客 only ever has 螺纹主力: the flat list stays.
          guestRows.map(row)
        ) : visible.map((group, index) => {
          const exchangeHeading = index === 0 || visible[index - 1].exchangeId !== group.exchangeId
            ? BOARD_EXCHANGES.find(item => item.code === group.exchangeId)?.label
              ?? (group.exchangeId === "KQD" ? "海外主连" : group.exchangeId) : null;
          const expanded = exact ? watchlistKey(exact) === group.key : product === group.key;
          const quote = board?.rows.find(item => item.exchangeId === group.exchangeId && item.productId === group.productId);
          const months = monthsOf(group);
          return (
          <Fragment key={group.key}>
          {exchangeHeading ? <h4>{exchangeHeading}</h4> : null}
          <div className="fdw-product">
            <div className="fdw-product-row">
              <button type="button" className={group.main.symbol === workspace.selectedSymbol ? "active" : ""}
                aria-label={`${group.productName} ${group.main.symbol}`} onClick={() => onSelect(group.main.symbol)}>
                <span><strong>{group.productName}</strong><small>{group.main.symbol}{group.exchangeId === "KQD" && !group.main.strategyScope ? <i className="fdw-quote-only">仅行情</i> : null}</small></span>
                {quote ? <span className="fdw-product-quote"><b className={boardPrice(quote).fallback ? "fallback" : undefined} title={boardPrice(quote).fallback ? "还没有成交，显示昨结算价" : undefined}>{boardPrice(quote).text}</b>
                  <small className={signClass(quote.changePercent)}>{quote.changePercent === null || quote.changePercent === undefined ? "—" : `${quote.changePercent > 0 ? "+" : ""}${quote.changePercent.toFixed(2)}%`}</small></span> : null}
              </button>
              {onToggleWatchlist ? <WatchlistStar product={group.key} name={group.productName} starred={starred.has(group.key)} onToggle={onToggleWatchlist} /> : null}
              <button type="button" className="fdw-product-expand" aria-expanded={expanded} aria-label={`${group.months.length} 个月份`}
                onClick={() => onProductChange(expanded ? null : group.key)}><span>{group.months.length} 个月份</span><ChevronDown aria-hidden="true" /></button>
            </div>
            {expanded ? months.map(month => (
              <button type="button" key={month.symbol} aria-label={month.symbol}
                className={`fdw-month-row ${month.symbol === workspace.selectedSymbol ? "active" : ""} ${exact?.symbol === month.symbol ? "hit" : ""}`}
                onClick={() => onSelect(month.symbol)}>
                <span><strong>{month.instrumentId}</strong><small>{month.deliveryYear}-{String(month.deliveryMonth).padStart(2, "0")}</small></span>
                {/* 交割月提醒 (2026-10-02): plain text on the month row (the button's aria-label
                    stays the symbol; the card's ⓘ is the reminder's commandable entry). */}
                {month.deliveryNotice ? <i className="fdw-delivery-chip" data-level={month.deliveryNotice.level}>{deliveryBadgeText(month.deliveryNotice)}</i> : null}
                <b>{month.isMain ? "主力" : `Tick ${formatNumber(month.priceTick, decimalsForTick(month.priceTick))}`}</b>
              </button>
            )) : null}
          </div>
          </Fragment>
          );
        })}
        {/* #712 B2: a catalog that never arrived is not "no match" — and an empty search is not a blank list. */}
        {catalogMissing ? <p className="fdw-contract-empty" role="status">{catalogState.status === "offline"
          ? "合约列表暂时读不到（应用服务未连接）"
          : catalogState.status === "expired" ? "合约列表暂时读不到（游客会话已失效，请重新连接）"
          : <>{guest ? "合约列表暂时读不到（公共行情暂不可用）" : "合约列表暂时读不到"}<button type="button" className="ghost-button" onClick={workspace.onRetryInstruments}>重试</button></>}</p>
          : needle && items.length > 0 && !(guest ? guestRows : visible).length ? <p className="fdw-contract-empty">没有匹配的品种或合约</p> : null}
        {catalogLoading ? <p>正在读取合约目录…</p> : null}
        {workspace.guest ? <p className="fdw-contract-guest">游客只能搜索和查看螺纹主力合约，<button type="button" onClick={() => workspace.onOpenCloudLogin?.()}>登录</button>后可看全部品种</p> : null}
      </div>
    </aside>
  </div>
  );
}

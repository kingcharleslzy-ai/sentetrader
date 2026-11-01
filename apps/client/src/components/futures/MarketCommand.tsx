import { useRef, type ReactNode } from "react";
import { Menu } from "lucide-react";
import { sameContract, type MarketWorkbenchSnapshot } from "../../api";
import { MarketSessionCard } from "../MarketSessionCard";
import type { FuturesDecisionWorkspaceProps } from "./props";
import { foreignChartSource, formatAge, formatNumber, formatTime, isForeignSymbol, signClass } from "./support";
import type { PriceAlertsHandle } from "../../hooks/usePriceAlerts";
import type { ChartDrawingsHandle } from "../../hooks/useChartDrawings";
import { ContractInfoControl } from "./ContractInfoControl";
import { WatchlistStar } from "./WatchlistStar";
import { PeriodSwitch } from "./PeriodSwitch";
import "./MarketCommand.css";

/** The last tick's direction and a counter that remounts the price so its flash restarts. */
function usePriceTick(symbol: string | undefined, price: number | undefined) {
  const last = useRef<{ symbol?: string; price?: number; tick: { dir?: "up" | "down"; n: number } }>({ tick: { n: 0 } });
  const current = last.current;
  if (symbol !== current.symbol) last.current = { symbol, price, tick: { n: current.tick.n } };
  else if (price !== undefined && current.price !== undefined && price !== current.price) {
    last.current = { symbol, price, tick: { dir: price > current.price ? "up" : "down", n: current.tick.n + 1 } };
  } else if (current.price === undefined) last.current = { ...current, price };
  return last.current.tick;
}

interface Props {
  workspace: FuturesDecisionWorkspaceProps;
  market: MarketWorkbenchSnapshot;
  /** Only this symbol's visible header label can stand in for the chart's own name. */
  headerCurrentContract?: string;
  /** 分时图 (2026-10-02): the workspace's chart view — the 分时 button at the head of the period
   *  row is its switch; the period buttons return to the K 线. */
  chartView: "kline" | "timeshare";
  decimals: number;
  onOpenContracts: () => void;
  onSelectDuration: (duration: MarketWorkbenchSnapshot["durationSeconds"]) => void;
  onSelectChartView: (view: "kline" | "timeshare") => void;
  /** 到价提醒 (2026-10-02): the handle the workspace owns, for its receipt line under the bar; absent for a guest.
   *  A replay window (2026-10-03) has one too: its alerts are the replay child's own. */
  alerts?: PriceAlertsHandle;
  /** 提醒中心 (#932, 2026-10-15): the bell right of the period buttons (it opens the 提醒 panel on 提醒记录); absent
   *  for a guest. */
  reminderBell?: ReactNode;
  /** 用户画线: the workspace's handle, only for its receipt line under the bar (清空 / 拒绝的原因);
   *  the tools themselves are the chart's left toolbar since 画线第二版 (2026-10-09). */
  drawings?: ChartDrawingsHandle;
  /** 自选 (2026-10-02): the star beside the contract name — the shown contract's product. */
  watchlistStar?: { product: string; name: string; starred: boolean };
  /** Present (and the account savable) when the star should toggle; absent = read-only. */
  onToggleWatchlist?: (product: string) => void;
  /** The watchlist save path's inline status (错误原因). */
  watchlistStatus?: string;
}

/** The contract name with its month kept whole: when the bar runs tight the product part
 *  ellipsizes ("沪深30…2609") instead of the whole name disappearing. */
function ContractName({ name }: { name?: string }) {
  const month = /\d{3,4}$/.exec(name ?? "")?.[0];
  if (!name || !month || month.length === name.length) return <strong>{name}</strong>;
  return <strong><span>{name.slice(0, -month.length)}</span><span>{month}</span></strong>;
}

/** Instrument, quote summary, period switch and chart tools above the chart. */
export function MarketCommand({ workspace, market, headerCurrentContract, chartView, decimals, onOpenContracts, onSelectDuration, onSelectChartView, alerts, reminderBell, drawings,
  watchlistStar, onToggleWatchlist, watchlistStatus }: Props) {
  const quote = market.quote;
  const tick = usePriceTick(quote?.symbol, quote?.lastPrice);
  const changePositive = (quote.changePercent ?? 0) > 0;
  const foreignSource = foreignChartSource(quote.symbol, quote.source, market.source);
  // The chart is real but there is no quote: none yet before the open (a contract
  // without a night session before Monday), or nobody has traded the contract today.
  const preOpen = quote.dataErrorCode === "market_quote_pre_open";
  const noTrade = quote.dataErrorCode === "market_quote_no_trade";
  // 回放 (#932 X3, owner 2026-10-19): the replay's minute-derived quote carries no 昨结 basis,
  // day open/high/low or day volume — say so instead of a 0.00% / "—" / 0 that reads as data.
  // 持仓 comes with the replay's bars and stays.
  const replay = Boolean(workspace.replayActive);
  const dayStats: Array<[string, string]> = [
    ["开", formatNumber(quote?.open, decimals)], ["高", formatNumber(quote?.highest, decimals)],
    ["低", formatNumber(quote?.lowest, decimals)], ["量", formatNumber(quote?.volume)],
  ];
  return (
    <section className={`fdw-market-command${sameContract(quote.symbol, headerCurrentContract) ? " fdw-current-contract-in-header" : ""}`} data-agent-target="market:command">
          <div className="fdw-market-head">
            <div className="fdw-contract-head">
              {/* The name is split for the ellipsis; the label keeps it whole: "沪金2612 SHFE.au2612". */}
              <button type="button" className="fdw-instrument-button" data-agent-target="market:contract" aria-label={`${quote?.name ?? ""} ${quote?.symbol ?? ""}`.trim()} disabled={workspace.replayActive} onClick={onOpenContracts}><span><ContractName name={quote?.name} /><small>{quote?.symbol}</small></span><Menu /></button>
              {watchlistStar && onToggleWatchlist ? <WatchlistStar product={watchlistStar.product} name={watchlistStar.name}
                starred={watchlistStar.starred} onToggle={onToggleWatchlist} /> : null}
              {/* 合约信息卡 (2026-10-02): the ⓘ (and the delivery badge when a deadline nears) sit
                  beside the contract button; the catalog entry carries the delivery facts. */}
              <ContractInfoControl quote={quote} runtimeUrl={workspace.runtimeBaseUrl}
                item={workspace.instruments?.items.find(entry => entry.symbol.toUpperCase() === quote.symbol.toUpperCase())} />
            </div>
            {preOpen || noTrade
              ? <div className="fdw-pre-open" data-testid="quote-pre-open"><strong>{noTrade ? formatNumber(quote.lastPrice, decimals) : "—"}</strong><span>{noTrade ? "今日暂无成交，显示最近收盘价" : (market.marketSessionStatus ?? quote.marketSessionStatus) === "closed" && !isForeignSymbol(quote.symbol) ? "无夜盘，开盘前无最新行情" : "开盘前无最新行情"}</span></div>
              : <div className={replay ? undefined : signClass(quote.changePercent)}><strong key={tick.n} data-tick={tick.dir}>{formatNumber(quote?.lastPrice, decimals)}</strong>
                {replay ? <span className="fdw-replay-omitted" data-testid="quote-replay-change">涨跌幅 回放不显示</span>
                  : <span>{changePositive ? "+" : ""}{quote?.changePercent.toFixed(2)}%</span>}
                {/* 新浪备用 (2026-10-07): stays beside the price at every width — the session pill hides on narrow bars. */}
                {market.dataStatus === "backup" ? <small className="fdw-backup-badge" data-testid="quote-backup" title="快期行情暂不可用，这是新浪备用行情，可能延迟几秒；只供看盘，不能下单">备用 · 新浪</small> : null}
                {foreignSource ? <small className="fdw-backup-badge" data-testid="quote-foreign-source">{foreignSource}</small> : null}</div>}
            <dl>
              {replay
                ? <div data-testid="quote-replay-stats"><dt>开高低 · 量</dt><dd className="fdw-replay-omitted">回放不显示</dd></div>
                : dayStats.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
              <div><dt>持仓</dt><dd>{formatNumber(quote?.openInterest)}</dd></div>
            </dl>
          </div>
          <div className="fdw-chart-tools">
            <PeriodSwitch chartView={chartView} duration={workspace.duration} onSelectDuration={onSelectDuration} onSelectChartView={onSelectChartView} />
            {reminderBell}
            <div className="fdw-chart-health" title={`${formatTime(quote?.quoteDatetime)} · ${formatAge(market.ageSeconds)} · 最小变动价位 ${formatNumber(quote?.priceTick, decimals)}`}>
              <MarketSessionCard key={`${workspace.runtimeBaseUrl}:${quote?.symbol}:${workspace.replayActive}`} market={market} replayActive={workspace.replayActive}/>
            </div>
          </div>
          {watchlistStatus && <small role="status">{watchlistStatus}</small>}
          {alerts?.notice && <small role="status" data-testid="price-alert-notice">{alerts.notice}</small>}
          {drawings?.notice && <small role="status" data-testid="user-drawing-notice">{drawings.notice}</small>}
        </section>
  );
}

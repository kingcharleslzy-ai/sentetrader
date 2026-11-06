import { useRef, type ReactNode } from "react";
import { Menu } from "lucide-react";
import { sameContract, type MarketWorkbenchSnapshot } from "../../api";
import { MarketSessionCard } from "../MarketSessionCard";
import type { FuturesDecisionWorkspaceProps } from "./props";
import { FALLBACK_PRICE_TITLE, chartDayQuote, foreignChartSource, formatAge, formatNumber, formatTime, isForeignSymbol, signClass } from "./support";
import type { PriceAlertsHandle } from "../../hooks/usePriceAlerts";
import type { ChartDrawingsHandle } from "../../hooks/useChartDrawings";
import { ContractInfoControl } from "./ContractInfoControl";
import { WatchlistStar } from "./WatchlistStar";
import { PeriodSwitch } from "./PeriodSwitch";
import { fitClasses, rowContentOverflows, useFitLevel } from "./useHeaderFit";
import "./MarketCommand.css";
import { useLiveMarket } from "../../hooks/useLiveMarket";

/** 一套自适应 (owner 2026-11-05): the bar stays one line and gives up, as its measured space runs out (the header's
 *  useFitLevel): spacing, the session pill, 开高低·持仓, the uncommon periods into 更多, then 多图 and 铃铛 into the
 *  same 更多, the contract code and percent chip, last the contract button's name when the header shows it.
 *  The contract name and the last price never go. */
const MARKET_FIT_CLASSES = ["fdw-fit-tight", "fdw-fit-hide-session", "fdw-fit-hide-stats", "fdw-fit-fold-periods", "fdw-fit-fold-tools",
  "fdw-fit-hide-code", "fdw-fit-compact-contract"];
const FOLD_PERIODS = 4;
const FOLD_TOOLS = 5;
/** The tool cells that shrink and scroll instead of pushing the row: their overflow counts too. OHLC (dl) is
 *  not probed: it already drops whole items by itself as the tools take its room. */
const MARKET_WHOLE_CELLS = [".fdw-market-head", ".fdw-chart-tools", ".fdw-chart-tools > [role='group']", ".fdw-chart-layout"];
const marketBarOverflows = (bar: HTMLElement) => rowContentOverflows(bar, MARKET_WHOLE_CELLS, "dl");

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
  /** 电脑版多图 (2026-11-02): the layout buttons and sync switches, after the periods. */
  chartLayout?: ReactNode;
  /** The same layout buttons for the 更多 popover when the bar folds them — without the "?" (one help-tip registration). */
  foldedChartLayout?: ReactNode;
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
export function MarketCommand({ workspace, market: shownMarket, headerCurrentContract, chartView, decimals, onOpenContracts, onSelectDuration, onSelectChartView, alerts, reminderBell, chartLayout, foldedChartLayout, drawings,
  watchlistStar, onToggleWatchlist, watchlistStatus }: Props) {
  // 电脑版流畅度 ②: the price shows every pushed patch; the window itself takes them at most once a second.
  const market = useLiveMarket(shownMarket) ?? shownMarket;
  const quote = market.quote;
  const tick = usePriceTick(quote?.symbol, quote?.lastPrice);
  const changePositive = (quote.changePercent ?? 0) > 0;
  const foreignSource = foreignChartSource(quote.symbol, quote.source, market.source);
  // The chart is real but there is no quote: none yet before the open (a contract
  // without a night session before Monday), or nobody has traded the contract today.
  const preOpen = quote.dataErrorCode === "market_quote_pre_open";
  const noTrade = quote.dataErrorCode === "market_quote_no_trade";
  const day = chartDayQuote(quote);
  // 回放 (#932 X3, owner 2026-10-19): the replay's minute-derived quote carries no 昨结 basis,
  // day open/high/low or day volume — say so instead of a 0.00% / "—" / 0 that reads as data.
  // 持仓 comes with the replay's bars and stays.
  const replay = Boolean(workspace.replayActive);
  const dayStats: Array<[string, string]> = [
    ["开", formatNumber(quote?.open, decimals)], ["高", formatNumber(quote?.highest, decimals)],
    ["低", formatNumber(quote?.lowest, decimals)], ["量", formatNumber(quote?.volume)],
  ];
  const barRef = useRef<HTMLElement>(null);
  const inHeader = sameContract(quote.symbol, headerCurrentContract);
  // What changes the bar's content width without resizing it; the price by digit count, not every tick.
  // shortcut: the bell's unread badge growing (9 → 10) is not in the key, the next resize re-fits it.
  const fitKey = [quote.symbol, quote.name, chartView, workspace.duration, replay, preOpen || noTrade, market.dataStatus, foreignSource,
    formatNumber(quote?.lastPrice, decimals).length, market.marketSessionStatus ?? quote.marketSessionStatus, Boolean(chartLayout), Boolean(reminderBell),
    inHeader, Boolean(watchlistStar), workspace.instruments?.items.length, workspace.fontScale, workspace.interfaceDensity].join("|");
  const fit = useFitLevel(barRef, fitKey, MARKET_FIT_CLASSES, marketBarOverflows);
  return (
    <section ref={barRef} className={`fdw-market-command${inHeader ? " fdw-current-contract-in-header" : ""} ${fitClasses(MARKET_FIT_CLASSES, fit)}`}
      data-fit={fit} data-agent-target="market:command">
        <div className="fdw-market-row">
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
              // 价格口径 (2026-11-03, `dayQuote`): 昨结算 greyed and 盘前 / 未成交, as every list and card shows it.
              ? <div className="fdw-pre-open" data-testid="quote-pre-open"><strong data-agent-live title={FALLBACK_PRICE_TITLE}>{formatNumber(day.price, decimals)}</strong>
                <span title={noTrade ? "今日暂无成交" : (market.marketSessionStatus ?? quote.marketSessionStatus) === "closed" && !isForeignSymbol(quote.symbol) ? "无夜盘，开盘前无最新行情" : "开盘前无最新行情"}>{day.note}</span></div>
              : <div className={replay ? undefined : signClass(quote.changePercent)}><strong data-agent-live key={tick.n} data-tick={tick.dir}>{formatNumber(quote?.lastPrice, decimals)}</strong>
                {replay ? <span className="fdw-replay-omitted" data-testid="quote-replay-change">涨跌幅 回放不显示</span>
                  : <span data-agent-live>{changePositive ? "+" : ""}{quote?.changePercent.toFixed(2)}%</span>}
                {/* 新浪备用 (2026-10-07): stays beside the price at every width — the session pill hides on narrow bars. */}
                {market.dataStatus === "backup" ? <small className="fdw-backup-badge" data-testid="quote-backup" title="快期行情暂不可用，这是新浪备用行情，可能延迟几秒；只供看盘，不能下单">备用 · 新浪</small> : null}
                {foreignSource ? <small className="fdw-backup-badge" data-testid="quote-foreign-source">{foreignSource}</small> : null}</div>}
            <dl>
              {replay
                ? <div data-testid="quote-replay-stats"><dt>开高低 · 量</dt><dd className="fdw-replay-omitted">回放不显示</dd></div>
                : dayStats.map(([label, value]) => <div key={label}><dt>{label}</dt><dd data-agent-live>{value}</dd></div>)}
              <div><dt>持仓</dt><dd data-agent-live>{formatNumber(quote?.openInterest)}</dd></div>
            </dl>
          </div>
          <div className="fdw-chart-tools">
            {/* 多图 and 铃铛 stay mounted (the fit pass measures them); folded, the 更多 popover carries a copy. */}
            <PeriodSwitch chartView={chartView} duration={workspace.duration} onSelectDuration={onSelectDuration} onSelectChartView={onSelectChartView}
              folded={fit >= FOLD_PERIODS} tools={fit >= FOLD_TOOLS && (foldedChartLayout || reminderBell) ? <>{foldedChartLayout}{reminderBell}</> : undefined} />
            {chartLayout}
            {reminderBell}
            <div className="fdw-chart-health" title={`${formatTime(quote?.quoteDatetime)} · ${formatAge(market.ageSeconds)} · 最小变动价位 ${formatNumber(quote?.priceTick, decimals)}`}>
              <MarketSessionCard key={`${workspace.runtimeBaseUrl}:${quote?.symbol}:${workspace.replayActive}`} market={market} replayActive={workspace.replayActive}/>
            </div>
          </div>
        </div>
          {watchlistStatus && <small role="status">{watchlistStatus}</small>}
          {alerts?.notice && <small role="status" data-testid="price-alert-notice">{alerts.notice}</small>}
          {drawings?.notice && <small role="status" data-testid="user-drawing-notice">{drawings.notice}</small>}
        </section>
  );
}

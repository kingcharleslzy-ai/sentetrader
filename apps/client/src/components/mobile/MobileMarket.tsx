import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, PenLine, Settings2, Star } from "lucide-react";
import { sameContract, type AccountPosition, type InstrumentCatalogItem, type MarketBoardSnapshot, type MarketWorkbenchSnapshot } from "../../api";
import type { PriceAlertsHandle } from "../../hooks/usePriceAlerts";
import type { ChartDrawingsHandle } from "../../hooks/useChartDrawings";
import type { HandleDrag } from "../chartDrawingInteraction";
import { useDrawingDesk, type DrawingPreferencePatch } from "../futures/useDrawingDesk";
import { DrawingLoupe, MobileDrawingBar, MobileDrawingSettings, MobileDrawingSheet } from "./MobileDrawing";
import type { ChartView, DurationSeconds } from "../../userPreferences";
import { TradingCharts, type ChartPreferences, type TradingChartSettings } from "../TradingCharts";
import { TimeshareChart } from "../TimeshareChart";
import { PriceAlertsControl } from "../futures/PriceAlertsControl";
import { foreignChartSource, formatNumber, watchlistMainSymbol } from "../futures/support";
import type { ChartIndicatorPatch } from "../chartIndicatorEditor";
import type { ChartIndicatorConfig } from "../chartIndicators";
import { usePriceFlash } from "./mobileMotion";
import { MARKET_VIEWS, shownSubPane, subPaneName } from "./mobileMarketPrefs";
import type { MobileMarketHandle } from "./useMobileMarket";
import { MobileQuotes } from "./MobileQuotes";
import { useForeignBoard } from "../../hooks/useForeignBoard";
import { MobileMultiChart } from "./MobileMultiChart";
import { MobileSubPaneSheet } from "./MobileSubPaneSheet";

type Duration = MarketWorkbenchSnapshot["durationSeconds"];

/** The phone's five periods (the desktop row's labels, so commands and help read the same). */
const MOBILE_PERIODS: { label: string; view: ChartView; duration?: Duration }[] = [
  { label: "分时", view: "timeshare" }, { label: "5分", view: "kline", duration: 300 }, { label: "15分", view: "kline", duration: 900 },
  { label: "1小时", view: "kline", duration: 3600 }, { label: "日线", view: "kline", duration: 86400 },
];
const PERIOD_NAMES: Record<number, string> = { 60: "1 分钟", 180: "3 分钟", 300: "5 分钟", 900: "15 分钟", 1800: "30 分钟", 3600: "1 小时", 86400: "日线", 604800: "周线" };

const direction = (change: number | null | undefined) => change === undefined || change === null || !Number.isFinite(change) || change === 0 ? undefined : change > 0 ? "up" : "down";
const percent = (change: number | null | undefined) => change === undefined || change === null || !Number.isFinite(change) ? "—" : `${change > 0 ? "+" : ""}${change.toFixed(2)}%`;
const displayName = (item: InstrumentCatalogItem | undefined, fallback: string) => item?.instrumentName || fallback;

interface WatchRow { symbol: string; name: string; last: number | null; changePercent: number | null; decimals: number; holding: string; starred: boolean }

/** 自选 · 持仓标记: the open positions' contracts first, then the starred products' main contracts. */
export function marketRows({ positions, watchlist, board, instruments, quote }: {
  positions: AccountPosition[]; watchlist: string[]; board: MarketBoardSnapshot | undefined;
  instruments: readonly InstrumentCatalogItem[]; quote: MarketWorkbenchSnapshot["quote"] | undefined;
}): WatchRow[] {
  const rows = new Map<string, WatchRow>();
  const decimalsOf = (tick: number | null | undefined) => tick && tick < 1 ? Math.min(4, String(tick).split(".")[1]?.length ?? 0) : 0;
  const add = (symbol: string, starred: boolean) => {
    const key = symbol.toUpperCase();
    if (rows.has(key)) { if (starred) rows.get(key)!.starred = true; return; }
    const item = instruments.find(entry => entry.symbol.toUpperCase() === key);
    const live = board?.rows.find(row => row.symbol.toUpperCase() === key);
    const current = quote && quote.symbol.toUpperCase() === key ? quote : undefined;
    const held = positions.filter(position => sameContract(position.symbol, key))
      .map(position => `${position.direction === "long" ? "多" : "空"}${position.volume}`).join(" ");
    rows.set(key, { symbol, name: displayName(item, live?.instrumentName ?? symbol), holding: held, starred,
      last: current?.lastPrice ?? live?.last ?? null, changePercent: current?.changePercent ?? live?.changePercent ?? null,
      decimals: decimalsOf(item?.priceTick ?? current?.priceTick) });
  };
  positions.forEach(position => add(position.symbol, false));
  for (const product of watchlist) {
    const live = board?.rows.find(row => `${row.exchangeId}.${row.productId}` === product);
    const main = live?.symbol ?? watchlistMainSymbol(instruments, product);
    if (main) add(main, true);
  }
  return [...rows.values()];
}

/** 多图's automatic cells: 自选 and held contracts, then the busiest main contracts on the board. */
export function autoChartSymbols(rows: readonly WatchRow[], board: MarketBoardSnapshot | undefined): string[] {
  const busiest = [...(board?.rows ?? [])].filter(row => row.last != null).sort((a, b) => (b.volume ?? 0) - (a.volume ?? 0)).map(row => row.symbol);
  return [...rows.map(row => row.symbol), ...busiest];
}

/**
 * 看盘 (手机版第二期 2026-10-04; 第二批 2026-10-09): the top switch 报价 / 多图 / 单图 (it reopens on the last one).
 * 单图 is the contract (tap the title for the contract list), its price, five periods and ⚙ 副图设置, the chart —
 * one finger pans history, two fingers zoom, and one of the chosen sub panes under it (swipe it sideways for the
 * next, hold it for 副图设置) — then 设提醒 / 问 AI 这根K线 / 下单 above the 自选 · 持仓标记 list. 报价 and 多图 are
 * their own components; a row or a cell opens its single chart. 手机画线 (2026-10-09): ✎ beside the periods opens
 * the computer's drawing tools as a sheet; taps place points, the bar under the chart adjusts the selected line.
 */
export function MobileMarket({ market, name, decimals, chartView, onSelectDuration, onSelectChartView, onOpenContracts, onSelectSymbol,
  chartSettings, setChartSettings, chartPreferences, resolvedTheme, alerts, positions, watchlist, board, instruments,
  onAskBar, onOrder, orderBlocked, marketError, onAlertSignIn, mobileMarket, indicators, onIndicatorsChange, indicatorStatus,
  baseUrl, boardError, guest = false, historyNote, onSignIn, onOpenChart, drawings }: {
  market: MarketWorkbenchSnapshot | undefined;
  name: string;
  decimals: number;
  chartView: ChartView;
  onSelectDuration: (duration: Duration) => void;
  onSelectChartView: (view: ChartView) => void;
  /** The title: the contract list (the desktop's 合约抽屉, full screen on the phone). */
  onOpenContracts: () => void;
  onSelectSymbol: (symbol: string) => void;
  chartSettings: TradingChartSettings;
  setChartSettings: (settings: TradingChartSettings) => void;
  chartPreferences: ChartPreferences;
  resolvedTheme: "dark" | "light";
  alerts?: PriceAlertsHandle;
  positions: AccountPosition[];
  watchlist: string[];
  board: MarketBoardSnapshot | undefined;
  instruments: readonly InstrumentCatalogItem[];
  onAskBar: (text: string) => void;
  onOrder: () => void;
  /** Why 下单 cannot open now (no quote yet); undefined when it can. */
  orderBlocked?: string;
  marketError?: string;
  /** A visitor's window: 设提醒 opens the login (a visitor has no alerts of their own). */
  onAlertSignIn?: () => void;
  mobileMarket: MobileMarketHandle;
  /** The account's chart configuration (one with the computer) and its write path (undefined = read-only). */
  indicators: ChartIndicatorConfig;
  onIndicatorsChange?: (patch: ChartIndicatorPatch) => void;
  indicatorStatus?: string;
  baseUrl?: string;
  boardError?: string;
  guest?: boolean;
  /** A visitor's 最近 N 个交易日 basis, shown on the K-line chart (#734). */
  historyNote?: string;
  onSignIn?: () => void;
  /** A 报价 row or a 多图 cell: that contract (and period) on the single chart. */
  onOpenChart: (symbol: string, duration?: DurationSeconds) => void;
  /** 用户画线: the workspace's handle (the computer's records); absent for a visitor or while disconnected — no ✎. */
  drawings?: ChartDrawingsHandle;
}) {
  const { market: prefs, update, sheetOpen, setSheetOpen } = mobileMarket;
  const view = prefs.view;
  const foreign = useForeignBoard(guest ? undefined : baseUrl, view === "quotes" && prefs.board === "FOREIGN");
  const quote = market?.quote;
  const panes = indicators.subPaneList;
  const shown = shownSubPane(prefs, panes);
  // The phone shows the price chart and one chosen sub pane under it (the computer shows them all).
  const preferences = useMemo(() => ({ ...chartPreferences, subPaneList: shown ? [shown] : [] }), [chartPreferences, shown]);
  // A 成交量 sub pane takes over from the volume bars at the price chart's foot.
  const settings = useMemo(() => shown === "volume" && chartSettings.volume ? { ...chartSettings, volume: false } : chartSettings, [chartSettings, shown]);
  // 到价提醒 on this contract stay visible as the chart's dashed lines (as on the desktop).
  const alertLines = useMemo(() => (alerts?.active ?? []).filter(alert => sameContract(alert.symbol, quote?.symbol))
    .map(alert => ({ id: alert.id, price: Number(alert.price) })), [alerts, quote?.symbol]);
  const rows = useMemo(() => marketRows({ positions, watchlist, board, instruments, quote }), [positions, watchlist, board, instruments, quote]);
  const autoSymbols = useMemo(() => autoChartSymbols(rows, board), [rows, board]);
  const candidates = useMemo(() => {
    const names = new Map(instruments.map(item => [item.symbol.toUpperCase(), item.instrumentName]));
    return autoSymbols.filter((symbol, index, list) => list.findIndex(other => sameContract(other, symbol)) === index).slice(0, 40)
      .map(symbol => ({ symbol, name: names.get(symbol.toUpperCase()) ?? board?.rows.find(row => row.symbol === symbol)?.instrumentName ?? symbol }));
  }, [autoSymbols, instruments, board]);
  const tone = direction(quote?.changePercent);
  const flash = usePriceFlash(quote?.lastPrice, quote?.symbol);
  // 手机画线: the computer's desk (same records, switches, defaults, UI commands and snapshot), on the single chart only.
  const savePreferences = useMemo(() => onIndicatorsChange
    ? (patch: DrawingPreferencePatch) => onIndicatorsChange(patch as ChartIndicatorPatch) : undefined, [onIndicatorsChange]);
  const drawingPeriod = chartView === "timeshare" ? 0 : market?.durationSeconds ?? 0;
  const desk = useDrawingDesk({ drawings: view === "single" ? drawings : undefined, symbol: quote?.symbol, period: drawingPeriod, tick: quote?.priceTick,
    preferences: chartPreferences, savePreferences, contextMenu: null, mobile: true });
  const [draftCount, setDraftCount] = useState(0);
  const [finishSignal, setFinishSignal] = useState(0);
  const [loupe, setLoupe] = useState<HandleDrag | null>(null);
  const [drawingError, setDrawingError] = useState("");
  useEffect(() => { if (!drawingError) return; const timer = window.setTimeout(() => setDrawingError(""), 6000); return () => window.clearTimeout(timer); }, [drawingError]);
  const drawingProps = {
    userDrawings: desk.available ? desk.shown : [], drawingTool: desk.available ? desk.tool : null, selectedDrawingId: desk.selected?.id ?? null,
    selectedDrawingHandle: desk.handle, drawingsLocked: desk.locked, drawingMagnet: desk.magnet,
    onPlaceDrawing: desk.place, onMoveDrawing: desk.move, onDrawingSelect: desk.select,
    onDrawingHandleDrag: setLoupe, onDrawingDraft: setDraftCount, drawingFinish: finishSignal,
  };
  // Leaving the single chart (报价 / 多图) ends a placement, the selection and the tool sheet.
  const { setTool: setDrawingTool, select: selectDrawing, setToolbarOpen: setDrawingSheetOpen } = desk;
  useEffect(() => { if (view !== "single") { setDrawingTool(null); selectDrawing(null); setDrawingSheetOpen(false); } }, [view, setDrawingTool, selectDrawing, setDrawingSheetOpen]);
  const drawingNotice = drawingError || desk.notice || drawings?.createError || drawings?.notice;
  const period = chartView === "timeshare" ? "分时" : PERIOD_NAMES[market?.durationSeconds ?? 0] ?? "当前周期";
  const lastBar = market?.bars.at(-1);
  const barQuestion = `讲讲 ${name} ${period}图上最新这根 K 线${lastBar ? `（开 ${formatNumber(lastBar.open, decimals)} 高 ${formatNumber(lastBar.high, decimals)} 低 ${formatNumber(lastBar.low, decimals)} 收 ${formatNumber(lastBar.close, decimals)}）` : ""}说明什么，不创建订单。`;
  // The sub pane under the single chart: a sideways swipe shows the next chosen one, a hold opens 副图设置. Touches
  // that start on it stay with these gestures (the chart's own pan and pinch belong to the price pane).
  const chartBox = useRef<HTMLDivElement>(null);
  const gesture = useRef({ panes, shown, update, open: () => setSheetOpen(true) });
  gesture.current = { panes, shown, update, open: () => setSheetOpen(true) };
  useEffect(() => {
    const box = chartBox.current;
    if (!box || view !== "single") return;
    let start: { x: number; y: number } | null = null;
    let hold: ReturnType<typeof setTimeout> | undefined;
    // The 分时图 is the price chart too (its pan, pinch and drawing taps are its own), not a sub pane.
    const onPane = (event: Event) => event.target instanceof Element
      && Boolean(event.target.closest("[data-region^='chart.']:not([data-region='chart.main']):not([data-region='chart.timeshare'])"));
    const begin = (event: TouchEvent) => {
      if (!onPane(event)) return;
      event.stopPropagation();
      clearTimeout(hold);
      start = event.touches.length === 1 ? { x: event.touches[0].clientX, y: event.touches[0].clientY } : null;
      if (start) hold = setTimeout(() => { start = null; gesture.current.open(); }, 550);
    };
    const move = (event: TouchEvent) => {
      if (!start && !onPane(event)) return;
      event.stopPropagation();
      const touch = event.touches[0];
      if (start && touch && Math.hypot(touch.clientX - start.x, touch.clientY - start.y) > 10) clearTimeout(hold);
    };
    const end = (event: TouchEvent) => {
      if (!start && !onPane(event)) return;
      event.stopPropagation();
      clearTimeout(hold);
      const touch = event.changedTouches[0];
      const from = start;
      start = null;
      if (!from || !touch) return;
      const dx = touch.clientX - from.x, dy = touch.clientY - from.y;
      const { panes: list, shown: current, update: write } = gesture.current;
      if (Math.abs(dx) < 40 || Math.abs(dx) < Math.abs(dy) || list.length < 2 || !current) return;
      const next = list[(list.indexOf(current) + (dx < 0 ? 1 : -1) + list.length) % list.length];
      write({ subPane: next });
    };
    const pointer = (event: PointerEvent) => { if (event.pointerType === "touch" && onPane(event)) event.stopPropagation(); };
    const options = { capture: true } as const;
    box.addEventListener("touchstart", begin, options);
    box.addEventListener("touchmove", move, options);
    box.addEventListener("touchend", end, options);
    box.addEventListener("touchcancel", end, options);
    for (const type of ["pointerdown", "pointermove", "pointerup"] as const) box.addEventListener(type, pointer, options);
    return () => {
      clearTimeout(hold);
      box.removeEventListener("touchstart", begin, options);
      box.removeEventListener("touchmove", move, options);
      box.removeEventListener("touchend", end, options);
      box.removeEventListener("touchcancel", end, options);
      for (const type of ["pointerdown", "pointermove", "pointerup"] as const) box.removeEventListener(type, pointer, options);
    };
  }, [view]);
  const signIn = onSignIn ?? (() => undefined);
  return (
    <section className="fdm-page fdm-market" aria-label="看盘" data-testid="mobile-market" data-agent-target="mobile-market" data-view={view}>
      <header className="fdm-head fdm-market-head">
        {view === "single" ? <div className="fdm-market-id">
          <button type="button" className="fdm-market-title" aria-label={`选择合约：${name}`} onClick={onOpenContracts}>
            <strong>{name || "选择合约"}</strong><ChevronDown aria-hidden="true" />
          </button>
          {quote ? <span className="fdm-market-price" data-direction={tone} data-flash={flash}>
            <b>{formatNumber(quote.lastPrice, decimals)}</b><small>{percent(quote.changePercent)}</small>
            {/* 新浪备用 (2026-10-07): the same mark as the desktop price. */}
            {market?.dataStatus === "backup" ? <em className="fdm-backup-badge" data-testid="quote-backup" title="快期行情暂不可用，这是新浪备用行情，可能延迟几秒；只供看盘，不能下单">备用 · 新浪</em> : null}
            {foreignChartSource(quote.symbol, quote.source, market?.source) ? <em className="fdm-backup-badge" data-testid="quote-foreign-source">{foreignChartSource(quote.symbol, quote.source, market?.source)}</em> : null}
          </span> : null}
        </div> : <strong>看盘</strong>}
        <div className="fdm-seg" role="group" aria-label="看盘方式" data-agent-target="mobile-market-view">
          {MARKET_VIEWS.map(item => <button type="button" key={item.id} aria-pressed={item.id === view} onClick={() => update({ view: item.id })}>{item.label}</button>)}
        </div>
      </header>
      {view === "quotes" ? <MobileQuotes baseUrl={baseUrl} board={board} foreign={foreign} boardError={boardError} boardId={prefs.board} columns={prefs.columns}
        onBoard={board => update({ board })} onColumns={columns => update({ columns })} positions={positions} watchlist={watchlist}
        instruments={instruments} currentSymbol={quote?.symbol} onOpen={symbol => onOpenChart(symbol)} guest={guest} onSignIn={signIn} />
        : view === "multi" ? <MobileMultiChart baseUrl={baseUrl} market={prefs} layout={prefs.layout} onLayout={layout => update({ layout })}
          onCell={(index, change) => mobileMarket.apply({ cell: { index, ...change } })}
          current={{ symbol: quote?.symbol, duration: market?.durationSeconds }} ownMarket={market} ownError={marketError} autoSymbols={autoSymbols}
          candidates={candidates} instruments={instruments} onOpen={(symbol, duration) => onOpenChart(symbol, duration)}
          guest={guest} onSignIn={signIn} active={view === "multi"} />
        : <main className="fdm-content">
        <div className="fdm-periods-row">
          <div className="fdm-chips fdm-periods" role="group" aria-label="周期">
            {MOBILE_PERIODS.map(item => {
              const active = item.view === "timeshare" ? chartView === "timeshare" : chartView === "kline" && market?.durationSeconds === item.duration;
              return <button type="button" key={item.label} aria-pressed={active}
                onClick={() => item.duration ? onSelectDuration(item.duration) : onSelectChartView("timeshare")}>{item.label}</button>;
            })}
          </div>
          {desk.available ? <button type="button" className="fdm-icon-button" aria-label="画线工具" aria-expanded={desk.toolbarOpen}
            data-active={Boolean(desk.tool) || undefined} onClick={() => { setSheetOpen(false); desk.setToolbarOpen(!desk.toolbarOpen); }}><PenLine aria-hidden="true" /></button> : null}
          <button type="button" className="fdm-icon-button" aria-label="副图设置" aria-expanded={sheetOpen} onClick={() => { desk.setToolbarOpen(false); setSheetOpen(!sheetOpen); }}><Settings2 aria-hidden="true" /></button>
        </div>
        <div className="fdm-chart" data-testid="mobile-chart" ref={chartBox} data-sub-pane={chartView === "kline" && shown ? shown : undefined}>
          {!market ? <p className="fdm-note" role={marketError ? "alert" : "status"}>{marketError || "正在读取行情…"}</p>
            : chartView === "timeshare"
              ? <TimeshareChart bars={market.bars} schedule={quote?.tradingSchedule} preSettlement={quote?.preSettlement} priceTick={quote?.priceTick}
                settings={chartSettings} resolvedTheme={resolvedTheme} symbol={quote?.symbol} helpAvailable={false} {...drawingProps} />
              : <TradingCharts bars={market.bars} durationSeconds={market.durationSeconds} settings={settings} onSettingsChange={setChartSettings}
                preferences={preferences} resolvedTheme={resolvedTheme} timeZone="Asia/Shanghai" priceTick={quote?.priceTick} symbol={quote?.symbol}
                alertLines={alertLines} helpAvailable={false} historyNote={historyNote} {...drawingProps} />}
        </div>
        {desk.available ? <MobileDrawingBar desk={desk} draftCount={draftCount} onFinishPlacement={() => setFinishSignal(count => count + 1)} /> : null}
        {drawingNotice && desk.available ? <small className="fdm-draw-notice" role="alert">{drawingNotice}</small> : null}
        {chartView === "kline" && panes.length > 1 ? <div className="fdm-chips fdm-chips-sub fdm-subpane-tabs" role="group" aria-label="显示的副图">
          {panes.map(type => <button type="button" key={type} aria-pressed={type === shown} onClick={() => update({ subPane: type })}>{subPaneName(type)}</button>)}
        </div> : null}
        <div className="fdm-actions">
          {alerts && quote ? <PriceAlertsControl alerts={alerts} symbol={quote.symbol} name={name} priceTick={quote.priceTick} lastPrice={quote.lastPrice}
            onSwitchContract={onSelectSymbol} label="设提醒" /> : <button type="button" disabled={!onAlertSignIn} onClick={onAlertSignIn}>设提醒</button>}
          <button type="button" disabled={!market} onClick={() => onAskBar(barQuestion)}>问 AI 这根K线</button>
          <button type="button" className="fdm-order-button" disabled={Boolean(orderBlocked)} title={orderBlocked} onClick={onOrder}>下单</button>
        </div>
        <section className="fdm-section" aria-label="自选 · 持仓标记" data-agent-target="mobile-market-list">
          <h3>自选 · 持仓标记</h3>
          {rows.length ? <ul className="fdm-watch">
            {rows.map(row => (
              <li key={row.symbol}>
                <button type="button" aria-current={sameContract(row.symbol, quote?.symbol) ? "true" : undefined}
                  aria-label={`看 ${row.name}`} onClick={() => onSelectSymbol(row.symbol)}>
                  <span className="fdm-watch-name">{row.starred ? <Star aria-hidden="true" /> : null}<strong>{row.name}</strong>{row.holding ? <em>{row.holding}</em> : null}</span>
                  <span className="fdm-watch-price" data-direction={direction(row.changePercent)}>
                    <b>{formatNumber(row.last, row.decimals)}</b><small>{percent(row.changePercent)}</small>
                  </span>
                </button>
              </li>
            ))}
          </ul> : <p className="fdm-note">还没有自选或持仓。点上面的合约名，在合约列表里点星标加自选。</p>}
        </section>
      </main>}
      {desk.available && desk.toolbarOpen ? <MobileDrawingSheet desk={desk} onError={setDrawingError} /> : null}
      {desk.available ? <MobileDrawingSettings desk={desk} period={drawingPeriod} /> : null}
      {loupe ? <DrawingLoupe drag={loupe} tick={quote?.priceTick} /> : null}
      {sheetOpen && view === "single" ? <MobileSubPaneSheet indicators={indicators} onChange={onIndicatorsChange} onClose={() => setSheetOpen(false)} status={indicatorStatus} /> : null}
    </section>
  );
}

import { useMemo, type Dispatch, type ReactNode, type SetStateAction } from "react";
import { Columns2, Grid2x2, Square } from "lucide-react";
import type { MarketWorkbenchSnapshot } from "../../api";
import { CursorLinkContext, createCursorLink } from "../chartCursorLink";
import { TradingCharts, type ChartPreferences, type TradingChartSettings } from "../TradingCharts";
import { HelpTip } from "../Blocks";
import { useMultiChartReads } from "../mobile/MobileMultiChart";
import { durationLabel } from "../mobile/mobileMarketPrefs";
import { decimalsForTick, formatNumber } from "./support";
import { CHART_LAYOUTS, type DesktopChartsChange, type DesktopChartsHandle, type ShownCell } from "./desktopCharts";
import "./ChartGrid.css";

const LAYOUT_ICONS = { single: Square, two: Columns2, four: Grid2x2 };
const cellKey = (symbol: string, duration: number) => `${symbol.toUpperCase()}@${duration}`;

/** 电脑版多图: the layout buttons and 同合约 4 周期, beside the period row. */
export function ChartLayoutControls({ handle, onError }: { handle: DesktopChartsHandle; onError: (message: string) => void }) {
  const { charts } = handle;
  const run = (change: DesktopChartsChange) => { try { handle.apply(change); } catch (error) { onError(error instanceof Error ? error.message : "多图暂时不能切换"); } };
  return (
    <div className="fdw-chart-layout" data-agent-target="chart-layout">
      <div className="fdw-chart-layout-group" role="group" aria-label="多图布局">
        {CHART_LAYOUTS.map(item => { const Icon = LAYOUT_ICONS[item.id]; return <button type="button" key={item.id} aria-pressed={charts.layout === item.id}
          aria-label={item.label} title={item.label} onClick={() => run({ layout: item.id })}><Icon aria-hidden="true" /></button>; })}
        <button type="button" title="同合约 4 周期：四格同一合约，5 分 / 15 分 / 1 小时 / 日线" onClick={() => run({ preset: "periods" })}>4 周期</button>
      </div>
      <HelpTip helpKey="chart-layout" label="多图">点哪一格，哪一格就是当前合约：下单、合约标签和 AI 都跟它。其余格每 5 秒刷新一次，不能画线。</HelpTip>
    </div>
  );
}

/** The three sync switches, in the selected cell's header (the period row has no room for them at 1280). */
function SyncSwitches({ handle, onError }: { handle: DesktopChartsHandle; onError: (message: string) => void }) {
  const { charts } = handle;
  const run = (change: DesktopChartsChange) => { try { handle.apply(change); } catch (error) { onError(error instanceof Error ? error.message : "多图暂时不能切换"); } };
  return <div className="fdw-chart-layout-group fdw-chart-sync" role="group" aria-label="多图同步" data-agent-target="chart-sync">
    <span aria-hidden="true">同步</span>
    <button type="button" aria-pressed={charts.syncDuration} aria-label="同步周期" onClick={() => run({ syncDuration: !charts.syncDuration })}>周期</button>
    <button type="button" aria-pressed={charts.syncSymbol} aria-label="同步品种" onClick={() => run({ syncSymbol: !charts.syncSymbol })}>品种</button>
    <button type="button" aria-pressed={charts.syncCrosshair} aria-label="同步十字线" onClick={() => run({ syncCrosshair: !charts.syncCrosshair })}>十字线</button>
  </div>;
}

/**
 * 电脑版多图 (2026-11-02): the selected cell is `children` (the workspace's own chart stage, drawn first so the
 * page's one chart observation and `data-region`s stay its own; the grid places cells by `order`); every other
 * cell is a read-only K 线 of its contract and period, read every 5 s, with the same indicators and sub panes. A
 * click on a cell selects it. 单图 keeps the same wrapper, so switching layouts never rebuilds the selected chart.
 */
export function ChartGrid({ enabled, handle, baseUrl, own, nameOf, chartSettings, setChartSettings, chartPreferences, resolvedTheme, onError, children }: {
  /** No grid (a replay): the chart alone. */
  enabled: boolean;
  handle: DesktopChartsHandle;
  baseUrl: string | undefined;
  own: { market?: MarketWorkbenchSnapshot; error?: string };
  nameOf: (symbol: string) => string;
  chartSettings: TradingChartSettings;
  setChartSettings: Dispatch<SetStateAction<TradingChartSettings>>;
  chartPreferences: ChartPreferences;
  resolvedTheme: "dark" | "light";
  onError: (message: string) => void;
  children: ReactNode;
}) {
  const { charts, cells } = handle;
  const multi = enabled && charts.layout !== "single";
  const link = useMemo(() => createCursorLink(), []);
  const active = cells.find(cell => cell.active) ?? cells[0];
  const reads = useMultiChartReads(baseUrl, cells, multi,
    { symbol: own.market?.quote.symbol, duration: own.market?.durationSeconds, market: own.market, error: own.error });
  if (!enabled) return <>{children}</>;
  const select = (index: number) => { try { handle.apply({ active: index }); } catch (error) { onError(error instanceof Error ? error.message : "这一格暂时不能选"); } };
  return (
    <CursorLinkContext.Provider value={multi && charts.syncCrosshair ? link : null}>
      <div className="fdw-chart-grid" data-layout={charts.layout} data-agent-target={multi ? "chart-grid" : undefined}>
        <div className={`fdw-chart-cell${multi ? " is-active" : ""}`} key="active" style={{ order: active?.index ?? 0 }}
          data-agent-target={multi ? `chart-cell:${active?.index ?? 0}` : undefined} data-state={multi ? "active" : undefined}>
          {multi && active ? <CellHead cell={active} market={own.market} name={active.symbol ? nameOf(active.symbol) : "—"}
            extra={<SyncSwitches handle={handle} onError={onError} />} /> : null}
          {children}
        </div>
        {multi ? cells.filter(cell => !cell.active).map(cell => {
          const read = cell.symbol ? reads.get(cellKey(cell.symbol, cell.duration)) : undefined;
          const name = cell.symbol ? nameOf(cell.symbol) : "空格";
          const error = cell.symbol && !baseUrl ? "行情未连接，连接后读取" : read?.error;
          return <div className="fdw-chart-cell" key={`cell-${cell.index}`} style={{ order: cell.index }}
            data-agent-target={`chart-cell:${cell.index}`} data-state="idle" onClick={() => select(cell.index)}>
            <CellHead cell={cell} market={read?.market} name={name} onSelect={() => select(cell.index)} />
            <div className="fdw-chart-cell-body">
              {read?.market ? <TradingCharts passive helpAvailable={false} bars={read.market.bars} settings={chartSettings}
                onSettingsChange={setChartSettings} preferences={chartPreferences} resolvedTheme={resolvedTheme} timeZone="Asia/Shanghai"
                priceTick={read.market.quote.priceTick} symbol={read.market.quote.symbol} durationSeconds={cell.duration} />
                : !error ? <p className="fdw-chart-cell-note" role="status">{cell.symbol ? "正在读取…" : "没有可补位的合约"}</p> : null}
              {error ? <p className="fdw-chart-cell-note" role={baseUrl ? "alert" : "status"}>{read?.market ? "上次行情；" : ""}{error}</p> : null}
            </div>
          </div>;
        }) : null}
      </div>
    </CursorLinkContext.Provider>
  );
}

function CellHead({ cell, market, name, onSelect, extra }: { cell: ShownCell; market: MarketWorkbenchSnapshot | undefined; name: string; onSelect?: () => void; extra?: ReactNode }) {
  const quote = market?.quote;
  const change = quote?.changePercent;
  const period = durationLabel(cell.duration);
  const content = <>
    <strong>{name}</strong>
    {quote ? <span className="fdw-chart-cell-price" data-direction={change ? change > 0 ? "up" : "down" : undefined}>
      {formatNumber(quote.lastPrice, decimalsForTick(quote.priceTick))}
      <small>{`${(change ?? 0) > 0 ? "+" : ""}${(change ?? 0).toFixed(2)}%`}</small></span> : null}
    <small className="fdw-chart-cell-period">{period}{cell.auto ? " · 自动" : ""}</small>
  </>;
  return onSelect
    ? <button type="button" className="fdw-chart-cell-head" aria-label={`选中第 ${cell.index + 1} 格：${name} ${period}`}
      onClick={event => { event.stopPropagation(); onSelect(); }}>{content}</button>
    : <div className="fdw-chart-cell-head">{content}{extra}</div>;
}

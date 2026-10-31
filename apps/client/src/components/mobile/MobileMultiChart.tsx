import { useEffect, useMemo, useRef, useState } from "react";
import type { InstrumentCatalogItem, MarketKlineBar, MarketWorkbenchSnapshot } from "../../api";
import { readMarketWorkbench } from "../../marketCache";
import { useDisclosureSurface } from "../../uiSurfaces";
import type { DurationSeconds, MobileMarketPreferences } from "../../userPreferences";
import { marketErrorMessage } from "../../hooks/useMarketWorkbench";
import { formatNumber } from "../futures/support";
import { CELL_DURATIONS, MULTI_LAYOUTS, durationLabel, resolveCells, type MultiLayout, type ResolvedCell } from "./mobileMarketPrefs";

type Duration = MarketWorkbenchSnapshot["durationSeconds"];
export interface CellRead { market?: MarketWorkbenchSnapshot; error?: string; limited?: boolean }
const CELL_POLL_MS = 5000;
const cellKey = (symbol: string, duration: number) => `${symbol.toUpperCase()}@${duration}`;

/**
 * One read per distinct (contract, period) on screen, every 5 s while 多图 shows and the page is visible. A
 * cell that is the single chart's own contract and period reuses its data — no second series. Each read keeps
 * that series' 30 s lease on the Runtime; when 多图 closes the reads stop and the leases lapse, so the single
 * chart's series (renewed by its own reads) is never the one pushed out. A read refused for the subscription
 * cap marks its cell `limited` and is retried on the next round.
 */
export function useMultiChartReads(baseUrl: string | undefined, cells: readonly ResolvedCell[], active: boolean,
  own: { symbol?: string; duration?: number; market?: MarketWorkbenchSnapshot; error?: string }): Map<string, CellRead> {
  const [readState, setReadState] = useState<{ baseUrl?: string; reads: Map<string, CellRead> }>(() => ({ baseUrl, reads: new Map() }));
  // Reject the previous Runtime synchronously; effect cleanup only retires its pending reads.
  const ownKey = own.symbol && own.duration ? cellKey(own.symbol, own.duration) : "";
  const keys = useMemo(() => [...new Set(cells.flatMap(cell => cell.symbol ? [`${cell.symbol}@${cell.duration}`] : []))]
    .filter(key => cellKey(key.split("@")[0], Number(key.split("@")[1])) !== ownKey).sort().join("|"), [cells, ownKey]);
  useEffect(() => {
    if (!baseUrl || !active || !keys) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const list = keys.split("|").map(key => { const [symbol, duration] = key.split("@"); return { symbol, duration: Number(duration) as Duration }; });
    const round = async () => {
      if (document.visibilityState !== "hidden") {
        await Promise.all(list.map(async ({ symbol, duration }) => {
          let read: CellRead;
          try {
            read = { market: await readMarketWorkbench(baseUrl, symbol, duration, controller.signal) };
          } catch (error) {
            if (stopped || (error instanceof DOMException && error.name === "AbortError")) return;
            const detail = (error as { detail?: unknown }).detail;
            const code = typeof detail === "string" ? detail : (detail as { detail?: unknown } | undefined)?.detail;
            read = { error: marketErrorMessage(error), limited: code === "market_subscription_limit" };
          }
          if (stopped) return;
          // A failed round keeps the last bars on screen with the reason beside them.
          setReadState(current => {
            const previous = current.baseUrl === baseUrl ? current.reads : new Map<string, CellRead>();
            return { baseUrl, reads: new Map(previous).set(cellKey(symbol, duration), { ...previous.get(cellKey(symbol, duration)), ...read,
              ...(read.market ? { error: undefined, limited: false } : {}) }) };
          });
        }));
      }
      if (!stopped) timer = setTimeout(() => void round(), CELL_POLL_MS);
    };
    void round();
    return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, [baseUrl, active, keys]);
  return useMemo(() => {
    const reads = readState.baseUrl === baseUrl ? readState.reads : new Map<string, CellRead>();
    if (!ownKey || (!own.market && !own.error)) return reads;
    return new Map(reads).set(ownKey, { ...reads.get(ownKey),
      ...(own.market ? { market: own.market } : {}), error: own.error, limited: false });
  }, [readState, baseUrl, ownKey, own.market, own.error]);
}

/** A mini K 线: the last bars as candles in the market direction colours, scaled to the cell. */
export function MiniKline({ bars, label }: { bars: readonly MarketKlineBar[]; label: string }) {
  const shown = bars.slice(-48);
  if (!shown.length) return <svg className="fdm-mini-kline" viewBox="0 0 100 60" aria-label={label} role="img" />;
  const high = Math.max(...shown.map(bar => bar.high));
  const low = Math.min(...shown.map(bar => bar.low));
  const span = high - low || 1;
  const y = (price: number) => 2 + (high - price) / span * 56;
  const step = 100 / shown.length;
  return <svg className="fdm-mini-kline" viewBox="0 0 100 60" preserveAspectRatio="none" role="img" aria-label={label}>
    {shown.map((bar, index) => {
      const x = index * step + step / 2;
      const top = y(Math.max(bar.open, bar.close));
      const body = Math.max(0.6, y(Math.min(bar.open, bar.close)) - top);
      const tone = bar.close >= bar.open ? "up" : "down";
      return <g key={bar.datetime} data-direction={tone}>
        <line x1={x} x2={x} y1={y(bar.high)} y2={y(bar.low)} vectorEffect="non-scaling-stroke" />
        <rect x={x - step * 0.32} y={top} width={step * 0.64} height={body} />
      </g>;
    })}
  </svg>;
}

/** A cell's editor as a commandable surface (`disclosure: mobile-chart-cell`, id = the cell's index). */
function CellSurface({ index, open, onOpen }: { index: number; open: boolean; onOpen: (open: boolean) => void }) {
  useDisclosureSurface("mobile-chart-cell", { id: String(index), state: open, apply: onOpen });
  return null;
}

const decimalsOf = (tick: number | null | undefined) => tick && tick < 1 ? Math.min(4, String(tick).split(".")[1]?.length ?? 0) : 0;

/**
 * 看盘 · 多图 (2026-10-09): 4 合约 (2×2), 同合约 4 周期 (2×2, the chart's own contract — 文华's 多周期同列) or 6 图
 * (2×3). Each cell is a mini K 线 with its contract, latest price and period: tap it for that single chart; tap its
 * title (or hold the cell) to change its contract and period. The layout and cells are account preferences.
 */
export function MobileMultiChart({ baseUrl, market, layout, onLayout, onCell, current, ownMarket, ownError, autoSymbols, candidates, instruments,
  onOpen, guest, onSignIn, active }: {
  baseUrl: string | undefined;
  market: MobileMarketPreferences;
  layout: MultiLayout;
  onLayout: (layout: MultiLayout) => void;
  onCell: (index: number, change: { symbol?: string | null; duration?: DurationSeconds }) => void;
  current: { symbol?: string; duration?: number };
  ownMarket: MarketWorkbenchSnapshot | undefined;
  ownError?: string;
  autoSymbols: readonly string[];
  /** The contracts a cell can switch to (自选 then the main contracts), with their names. */
  candidates: readonly { symbol: string; name: string }[];
  instruments: readonly InstrumentCatalogItem[];
  onOpen: (symbol: string, duration: DurationSeconds) => void;
  guest: boolean;
  onSignIn: () => void;
  active: boolean;
}) {
  const cells = useMemo(() => resolveCells(market, current.symbol, autoSymbols), [market, current.symbol, autoSymbols]);
  const reads = useMultiChartReads(guest ? undefined : baseUrl, cells, active && !guest, { ...current, market: ownMarket, error: ownError });
  const [editing, setEditing] = useState<number | null>(null);
  const items = useMemo(() => new Map(instruments.map(item => [item.symbol.toUpperCase(), item])), [instruments]);
  const hold = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const held = useRef(false);
  useEffect(() => () => clearTimeout(hold.current), []);
  if (guest) {
    return <main className="fdm-content fdm-multi" data-agent-target="mobile-multi">
      <p className="fdm-note">多图要登录后看。</p>
      <button type="button" className="fdm-wide-button" onClick={onSignIn}>登录</button>
    </main>;
  }
  const limited = cells.filter(cell => cell.symbol && reads.get(cellKey(cell.symbol, cell.duration))?.limited).length;
  const editCell = editing === null ? undefined : cells[editing];
  return (
    <main className="fdm-multi" data-agent-target="mobile-multi" data-layout={layout}>
      <div className="fdm-chips fdm-multi-layouts" role="group" aria-label="多图布局">
        {MULTI_LAYOUTS.map(item => <button type="button" key={item.id} aria-pressed={item.id === layout} onClick={() => onLayout(item.id)}>{item.label}</button>)}
      </div>
      {cells.map(cell => <CellSurface key={cell.index} index={cell.index} open={editing === cell.index}
        onOpen={open => setEditing(current => open ? cell.index : current === cell.index ? null : current)} />)}
      {limited ? <p className="fdm-note" role="alert">行情订阅已满，{limited} 格本次读取失败，已有行情保留，稍后自动重试；单图不受影响。</p> : null}
      <div className="fdm-multi-grid">
        {cells.map(cell => {
          const read = cell.symbol ? reads.get(cellKey(cell.symbol, cell.duration)) : undefined;
          const error = cell.symbol && !baseUrl ? "行情未连接，连接后读取" : read?.error;
          const item = cell.symbol ? items.get(cell.symbol.toUpperCase()) : undefined;
          // Without a catalog name the short code (ag2612), never the exchange-prefixed one.
          const quoted = read?.market?.quote.name;
          const name = item?.instrumentName || (quoted && !quoted.includes(".") ? quoted : cell.symbol ? cell.symbol.split(".").pop()! : "空格");
          const quote = read?.market?.quote;
          const tone = quote?.changePercent ? quote.changePercent > 0 ? "up" : "down" : undefined;
          const period = durationLabel(cell.duration);
          return <div className="fdm-multi-cell" key={cell.index} data-testid={`mobile-multi-cell-${cell.index}`}
            onTouchStart={() => { held.current = false; clearTimeout(hold.current); hold.current = setTimeout(() => { held.current = true; setEditing(cell.index); }, 550); }}
            onTouchMove={() => clearTimeout(hold.current)} onTouchEnd={() => clearTimeout(hold.current)}>
            <button type="button" className="fdm-multi-title" aria-label={`改第 ${cell.index + 1} 格：${name} ${period}`} onClick={() => setEditing(cell.index)}>
              <strong>{name}</strong><small>{period}{cell.auto ? " · 自动" : ""}</small>
            </button>
            <button type="button" className="fdm-multi-body" aria-label={`看 ${name} ${period} 单图`} disabled={!cell.symbol}
              onClick={() => { if (held.current) { held.current = false; return; } if (cell.symbol) onOpen(cell.symbol, cell.duration); }}>
              <span className="fdm-multi-price" data-direction={tone}>
                {quote ? formatNumber(quote.lastPrice, decimalsOf(item?.priceTick ?? quote.priceTick)) : "—"}
                {quote ? <small>{`${quote.changePercent > 0 ? "+" : ""}${quote.changePercent.toFixed(2)}%`}</small> : null}
              </span>
              {read?.market ? <MiniKline bars={read.market.bars} label={`${name} ${period} K 线`} />
                : !error ? <span className="fdm-note" role="status">{!cell.symbol ? "没有可补位的合约" : "正在读取…"}</span> : null}
              {error ? <span className="fdm-note" role={baseUrl ? "alert" : "status"}>{read?.market ? "上次行情；" : ""}{error}</span> : null}
            </button>
          </div>;
        })}
      </div>
      {editCell ? <div className="fdm-sheet-backdrop" onClick={event => { if (event.target === event.currentTarget) setEditing(null); }}>
        <section className="fdm-sheet fdm-mm-sheet" role="dialog" aria-label={`第 ${editCell.index + 1} 格`}>
          <div className="fdm-sheet-grip" aria-hidden="true" />
          <header className="fdm-sheet-head"><span><strong>第 {editCell.index + 1} 格</strong></span>
            <button type="button" className="fdm-sheet-done primary-button" onClick={() => setEditing(null)}>完成</button></header>
          <div className="fdm-sheet-body">
            <h4 className="fdm-mm-label">{layout === "periods" ? "合约（四格一起换）" : "合约"}</h4>
            <div className="fdm-mm-grid" role="group" aria-label="这一格的合约">
              {layout !== "periods" ? <button type="button" aria-pressed={editCell.auto} onClick={() => onCell(editCell.index, { symbol: null })}>自动</button> : null}
              {candidates.map(option => <button type="button" key={option.symbol}
                aria-pressed={!editCell.auto && option.symbol.toUpperCase() === editCell.symbol?.toUpperCase()}
                onClick={() => onCell(editCell.index, { symbol: option.symbol })}>{option.name}</button>)}
            </div>
            <h4 className="fdm-mm-label">周期</h4>
            <div className="fdm-mm-grid" role="group" aria-label="这一格的周期">
              {CELL_DURATIONS.map(option => <button type="button" key={option.duration} aria-pressed={option.duration === editCell.duration}
                onClick={() => onCell(editCell.index, { duration: option.duration })}>{option.label}</button>)}
            </div>
          </div>
        </section>
      </div> : null}
    </main>
  );
}

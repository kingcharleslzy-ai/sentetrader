import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Settings2, Star } from "lucide-react";
import { getMarketQuotes, type AccountPosition, type ForeignBoardSnapshot, type InstrumentCatalogItem, type MarketBoardRow,
  type MarketBoardSnapshot } from "../../api";
import { marketErrorMessage } from "../../hooks/useMarketWorkbench";
import { useDisclosureSurface } from "../../uiSurfaces";
import { formatNumber, watchlistKey, watchlistMainSymbol } from "../futures/support";
import { QUOTE_BOARDS, QUOTE_COLUMNS, QUOTE_COLUMNS_MAX, type QuoteBoard, type QuoteColumn } from "./mobileMarketPrefs";

/** One 报价 row: a board row (a product's main contract) or a held month the board does not carry. */
export interface QuoteRow {
  symbol: string; name: string; code: string; decimals: number; starred: boolean; holding: string;
  /** False for a held non-main month: its figures come from the visible-row quote read. */
  onBoard: boolean;
  values: Partial<Record<QuoteColumn, number | null>>;
}

const decimalsOf = (tick: number | null | undefined) => tick && tick < 1 ? Math.min(4, String(tick).split(".")[1]?.length ?? 0) : 0;

function rowValues(row: MarketBoardRow | undefined): QuoteRow["values"] {
  if (!row) return {};
  return { last: row.last, changePercent: row.changePercent, change: row.change, openInterest: row.openInterest,
    bid: row.bidPrice1 ?? null, ask: row.askPrice1 ?? null, volume: row.volume, openInterestChange: row.openInterestChange };
}

/**
 * The rows a 板块 lists. 自选: the open positions' contracts first (a non-main month included), then the starred
 * products' main contracts. 主力: every product's main contract in the board's order. An exchange: its products'
 * main contracts. `extra` holds the figures of held months the board does not carry.
 */
export function quoteRows({ board, boardId, positions, watchlist, instruments, extra }: {
  board: MarketBoardSnapshot | undefined; boardId: QuoteBoard; positions: AccountPosition[]; watchlist: string[];
  instruments: readonly InstrumentCatalogItem[]; extra: ReadonlyMap<string, MarketBoardRow>;
}): QuoteRow[] {
  const boardRows = board?.rows ?? [];
  const bySymbol = new Map(boardRows.map(row => [row.symbol.toUpperCase(), row]));
  const items = new Map(instruments.map(item => [item.symbol.toUpperCase(), item]));
  const starred = new Set(watchlist);
  const make = (symbol: string, holding = ""): QuoteRow => {
    const key = symbol.toUpperCase();
    const live = bySymbol.get(key);
    const item = items.get(key);
    const product = live ? `${live.exchangeId}.${live.productId}` : item ? watchlistKey(item) : "";
    return { symbol: live?.symbol ?? item?.symbol ?? symbol, name: item?.instrumentName || live?.instrumentName || symbol,
      code: live?.instrumentId ?? item?.instrumentId ?? symbol.split(".")[1] ?? symbol, decimals: decimalsOf(item?.priceTick),
      starred: starred.has(product), holding, onBoard: Boolean(live), values: rowValues(live ?? extra.get(key)) };
  };
  if (boardId === "watchlist") {
    const rows = new Map<string, QuoteRow>();
    for (const position of positions) {
      const key = position.symbol.toUpperCase();
      const holding = positions.filter(item => item.symbol.toUpperCase() === key)
        .map(item => `${item.direction === "long" ? "多" : "空"}${item.volume}`).join(" ");
      if (!rows.has(key)) rows.set(key, make(position.symbol, holding));
    }
    for (const product of watchlist) {
      const main = boardRows.find(row => `${row.exchangeId}.${row.productId}` === product)?.symbol
        ?? watchlistMainSymbol(instruments, product);
      if (main && !rows.has(main.toUpperCase())) rows.set(main.toUpperCase(), make(main));
    }
    return [...rows.values()];
  }
  return boardRows.filter(row => boardId === "main" || row.exchangeId === boardId).map(row => make(row.symbol));
}

/** 外盘 板块: the foreign main series in the list's columns; quote-only rows (no single chart yet), the
 *  source named once above them. */
function ForeignRows({ foreign, columns, connected, onOpen }: { foreign?: { snapshot?: ForeignBoardSnapshot; error: string };
  columns: QuoteColumn[]; connected: boolean; onOpen: (symbol: string) => void }) {
  const snapshot = foreign?.snapshot;
  const labels = snapshot?.sourceLabels ?? {};
  const sources = [...new Set((snapshot?.rows ?? []).map(row => labels[row.source] ?? row.source))].join("、");
  return <>
    {foreign?.error ? <p className="fdm-note" role="alert">{snapshot?.rows.length ? "上次报价；" : ""}{foreign.error}</p> : null}
    {!connected ? <p className="fdm-note" role="status">行情未连接，连接后读取外盘</p>
      : !snapshot && !foreign?.error ? <p className="fdm-note" role="status">正在读取外盘…</p> : null}
    {sources ? <p className="fdm-note" role="status">来源 {sources}{snapshot?.status === "stale" ? "（上次取到的价格）" : ""}</p> : null}
    {(snapshot?.rows ?? []).map(row => {
      const decimals = row.decimals;
      const values: Partial<Record<QuoteColumn, number | null>> = { last: row.last, changePercent: row.changePercent, change: row.change,
        openInterest: row.openInterest, bid: row.bidPrice1, ask: row.askPrice1, volume: row.volume, openInterestChange: null };
      // 外盘行可点 (所有者 2026-10-19 方案 A): opens its 快期外盘主连 chart, the same one 合约选择 opens.
      const chart = row.chartSymbol ?? undefined;
      return (
        <div key={row.key} className="fdm-quote-row fdm-quote-foreign" aria-label={`${row.name} 外盘`} data-stale={row.stale || snapshot?.status === "stale" ? "" : undefined}
          role={chart ? "button" : undefined} tabIndex={chart ? 0 : undefined} onClick={chart ? () => onOpen(chart) : undefined}
          onKeyDown={chart ? event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onOpen(chart); } } : undefined}
          title={row.stale || snapshot?.status === "stale" ? "本次没取到，上次的价格" : undefined}>
          <span className="fdm-quote-name"><strong>{row.name}</strong><small>{row.exchange} {row.contract || "主连"} · {row.unit}</small></span>
          {columns.map(column => {
            const value = values[column];
            const tone = TONED.has(column) ? direction(row.changePercent ?? row.change) : undefined;
            return <span key={column} className="fdm-quote-value" data-direction={tone}>{cellText(column, value, decimals)}</span>;
          })}
        </div>
      );
    })}
  </>;
}

const signed = (value: number, text: string) => `${value > 0 ? "+" : ""}${text}`;
/** 持仓量 / 成交量 in 万 / 亿 once they are long. */
function compact(value: number): string {
  const size = Math.abs(value);
  if (size >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (size >= 1e4) return `${(value / 1e4).toFixed(size >= 1e6 ? 0 : 1)}万`;
  return formatNumber(value, 0);
}
function cellText(column: QuoteColumn, value: number | null | undefined, decimals: number): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (column === "changePercent") return signed(value, `${value.toFixed(2)}%`);
  if (column === "change") return signed(value, formatNumber(value, decimals));
  if (column === "openInterestChange") return signed(value, compact(value));
  if (column === "openInterest" || column === "volume") return compact(value);
  return formatNumber(value, decimals);
}
const direction = (value: number | null | undefined) => value === null || value === undefined || !Number.isFinite(value) || value === 0 ? undefined : value > 0 ? "up" : "down";
const TONED = new Set<QuoteColumn>(["last", "change", "changePercent"]);

const QUOTES_POLL_MS = 3000;
const EMPTY_QUOTES: ReadonlyMap<string, MarketBoardRow> = new Map();
const EMPTY_QUOTE_ERRORS: ReadonlyMap<string, string> = new Map();
type VisibleQuoteRead = { baseUrl: string; rows: Map<string, MarketBoardRow>; errors: Map<string, string>; capped: number };

/**
 * Held months the board does not carry are read through the quote-only channel — only the rows on screen
 * (`visible`), at most 20 at a time. A row scrolled away is no longer asked for, so its 30 s lease on the
 * Runtime lapses by itself; nothing here subscribes the whole list.
 */
function useVisibleQuotes(baseUrl: string | undefined, symbols: string[]) {
  const [readState, setReadState] = useState<VisibleQuoteRead>();
  const key = symbols.slice(0, 20).join(",");
  useEffect(() => {
    if (!baseUrl || !key) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const read = async () => {
      if (document.visibilityState !== "hidden") {
        try {
          const result = await getMarketQuotes(baseUrl, key.split(","), controller.signal);
          if (stopped) return;
          const next = new Map<string, MarketBoardRow>();
          // A successful batch may contain only some requested months. A missing row is this
          // round's failed read, not evidence that its last quote is still current.
          const failed = new Map(key.split(",").map(symbol => [symbol.toUpperCase(), "本次未取到报价，等待下次行情更新"]));
          let limited = 0;
          for (const row of result.rows) {
            if ("errorCode" in row) {
              if (row.errorCode === "market_subscription_limit") limited += 1;
              failed.set(row.symbol.toUpperCase(), marketErrorMessage({ detail: row.errorCode }));
              continue;
            }
            next.set(row.symbol.toUpperCase(), row);
            failed.delete(row.symbol.toUpperCase());
          }
          setReadState(current => {
            const own = current?.baseUrl === baseUrl ? current : undefined;
            const errors = new Map(own?.errors);
            next.forEach((_, symbol) => errors.delete(symbol));
            failed.forEach((reason, symbol) => errors.set(symbol, reason));
            return {baseUrl, rows: new Map([...(own?.rows ?? []), ...next]), errors, capped: limited};
          });
        } catch (error) {
          if (stopped) return;
          // Keep each month's last figures while identifying this failed read beside that row.
          setReadState(current => {
            const own = current?.baseUrl === baseUrl ? current : undefined;
            return {baseUrl, rows: own?.rows ?? new Map(), capped: 0,
              errors: new Map([...(own?.errors ?? []), ...key.split(",").map(symbol => [symbol.toUpperCase(), marketErrorMessage(error)] as const)])};
          });
        }
      }
      if (!stopped) timer = setTimeout(() => void read(), QUOTES_POLL_MS);
    };
    void read();
    return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, [baseUrl, key]);
  const own = readState?.baseUrl === baseUrl ? readState : undefined;
  return { rows: own?.rows ?? EMPTY_QUOTES, capped: key ? own?.capped ?? 0 : 0, errors: own?.errors ?? EMPTY_QUOTE_ERRORS };
}

/**
 * 看盘 · 报价 (2026-10-09): 板块 tabs (tap, or swipe the list sideways), a 合约 column and 1–4 value columns the
 * 列 sheet changes, rows in 自选 / 主力 / an exchange's order; tapping a row opens that contract's single chart.
 * The figures come from the shared 报价总表 read (every product's main contract, one Runtime-wide subscription);
 * only held non-main months on screen are read on their own.
 */
export function MobileQuotes({ baseUrl, board, foreign, boardError, boardId, columns, onBoard, onColumns, positions, watchlist, instruments,
  currentSymbol, onOpen, guest, onSignIn }: {
  baseUrl: string | undefined;
  board: MarketBoardSnapshot | undefined;
  /** 外盘 (2026-10-18, #976): the 外盘 板块's own read, present while that 板块 is open. */
  foreign?: { snapshot?: ForeignBoardSnapshot; error: string };
  boardError?: string;
  boardId: QuoteBoard;
  columns: QuoteColumn[];
  onBoard: (board: QuoteBoard) => void;
  onColumns: (columns: QuoteColumn[]) => void;
  positions: AccountPosition[];
  watchlist: string[];
  instruments: readonly InstrumentCatalogItem[];
  currentSymbol: string | undefined;
  onOpen: (symbol: string) => void;
  guest: boolean;
  onSignIn: () => void;
}) {
  const [columnsOpen, setColumnsOpen] = useDisclosureSurface("mobile-quote-columns", { available: !guest });
  const listRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState<string[]>([]);
  // Request membership comes from the board/positions, independently of cached figures.
  const listedRows = useMemo(() => quoteRows({ board, boardId, positions, watchlist, instruments, extra: EMPTY_QUOTES }),
    [board, boardId, positions, watchlist, instruments]);
  const offBoardKey = listedRows.filter(row => !row.onBoard).map(row => row.symbol).join(",");
  const offBoard = useMemo(() => offBoardKey ? offBoardKey.split(",") : [], [offBoardKey]);
  const wanted = useMemo(() => offBoard.filter(symbol => visible.includes(symbol)), [offBoard, visible]);
  const extra = useVisibleQuotes(guest ? undefined : baseUrl, wanted);
  const rows = useMemo(() => listedRows.map(row => row.onBoard ? row : {...row, values: rowValues(extra.rows.get(row.symbol.toUpperCase()))}),
    [listedRows, extra.rows]);
  // Which off-board rows are on screen: the observer follows the list's own scroll.
  useEffect(() => {
    const list = listRef.current;
    if (!list || !offBoard.length || typeof IntersectionObserver === "undefined") { setVisible([]); return; }
    const shown = new Set<string>();
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        const symbol = (entry.target as HTMLElement).dataset.symbol ?? "";
        if (entry.isIntersecting) shown.add(symbol); else shown.delete(symbol);
      }
      setVisible([...shown].sort());
    }, { root: list });
    list.querySelectorAll<HTMLElement>("[data-off-board]").forEach(element => observer.observe(element));
    return () => observer.disconnect();
  }, [offBoard]);   // keyed by the symbols, so the 3 s board poll does not re-create the observer
  // A sideways swipe on the list moves to the next / previous 板块.
  const swipe = useRef<{ x: number; y: number } | null>(null);
  const boardIndex = QUOTE_BOARDS.findIndex(item => item.id === boardId);
  const shift = (step: number) => {
    const next = QUOTE_BOARDS[boardIndex + step];
    if (next) onBoard(next.id);
  };
  const tabsRef = useRef<HTMLDivElement>(null);
  useEffect(() => { tabsRef.current?.querySelector<HTMLElement>("[aria-pressed='true']")?.scrollIntoView?.({ block: "nearest", inline: "center" }); }, [boardId]);
  if (guest) {
    return <main className="fdm-content fdm-quotes" data-agent-target="mobile-quotes">
      <p className="fdm-note">报价列表要登录后看。</p>
      <button type="button" className="fdm-wide-button" onClick={onSignIn}>登录</button>
    </main>;
  }
  const toggleColumn = (column: QuoteColumn) => {
    if (columns.includes(column)) { if (columns.length > 1) onColumns(columns.filter(item => item !== column)); return; }
    if (columns.length < QUOTE_COLUMNS_MAX) onColumns([...columns, column]);
  };
  return (
    <main className="fdm-quotes" data-agent-target="mobile-quotes" style={{ "--fdm-quote-columns": columns.length } as CSSProperties}>
      <div className="fdm-quotes-bar">
        <div className="fdm-chips fdm-quote-boards" ref={tabsRef} role="group" aria-label="报价板块">
          {QUOTE_BOARDS.map(item => <button type="button" key={item.id} aria-pressed={item.id === boardId} onClick={() => onBoard(item.id)}>{item.label}</button>)}
        </div>
        <button type="button" className="fdm-icon-button" aria-label="报价列设置" aria-expanded={columnsOpen} onClick={() => setColumnsOpen(!columnsOpen)}><Settings2 aria-hidden="true" /></button>
      </div>
      <div className="fdm-quote-row fdm-quote-head" aria-hidden="true">
        <span>合约</span>{columns.map(column => <span key={column}>{QUOTE_COLUMNS.find(item => item.id === column)?.label}</span>)}
      </div>
      <div className="fdm-quote-list" ref={listRef}
        onTouchStart={event => { const touch = event.touches[0]; swipe.current = event.touches.length === 1 ? { x: touch.clientX, y: touch.clientY } : null; }}
        onTouchEnd={event => {
          const start = swipe.current;
          swipe.current = null;
          const touch = event.changedTouches[0];
          if (!start || !touch) return;
          const dx = touch.clientX - start.x, dy = touch.clientY - start.y;
          if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) shift(dx < 0 ? 1 : -1);
        }}>
        {boardId === "FOREIGN" ? <ForeignRows foreign={foreign} columns={columns} connected={Boolean(baseUrl)} onOpen={onOpen} /> : null}
        {boardId === "FOREIGN" ? null : boardError ? <p className="fdm-note" role="alert">{board ? "上次报价；" : ""}{boardError}</p> : null}
        {boardId === "FOREIGN" ? null : !baseUrl ? <p className="fdm-note" role="status">{board ? "上次报价；" : ""}行情未连接，连接后读取报价</p>
          : !board && !boardError ? <p className="fdm-note" role="status">正在读取报价…</p> : null}
        {boardId !== "FOREIGN" && board && !rows.length ? <p className="fdm-note">{boardId === "watchlist" ? "还没有自选或持仓。到“主力”或合约列表里点星标加自选。" : "这个板块暂时没有合约。"}</p> : null}
        {extra.capped ? <p className="fdm-note" role="alert">行情订阅已满，{extra.capped} 个持仓月份本次读取失败，已有报价保留，稍后自动重试。</p> : null}
        {boardId === "FOREIGN" ? null : rows.map(row => (
          <button type="button" key={row.symbol} className="fdm-quote-row" data-symbol={row.symbol} data-off-board={row.onBoard ? undefined : ""}
            aria-current={row.symbol.toUpperCase() === currentSymbol?.toUpperCase() ? "true" : undefined}
            aria-label={`看 ${row.name} 单图`} onClick={() => onOpen(row.symbol)}>
            <span className="fdm-quote-name">
              <strong>{row.starred ? <Star aria-hidden="true" /> : null}{row.name}</strong>
              <small>{row.code}{row.holding ? <em>{row.holding}</em> : null}</small>
              {!row.onBoard && extra.errors.get(row.symbol.toUpperCase()) ? <span className="fdm-note" role="alert">{extra.rows.has(row.symbol.toUpperCase()) ? "上次报价；" : ""}{extra.errors.get(row.symbol.toUpperCase())}</span> : null}
            </span>
            {columns.map(column => {
              const value = row.values[column];
              const tone = TONED.has(column) ? direction(row.values.changePercent ?? row.values.change) : column === "openInterestChange" ? direction(value) : undefined;
              return <span key={column} className="fdm-quote-value" data-direction={tone}>{cellText(column, value, row.decimals)}</span>;
            })}
          </button>
        ))}
      </div>
      {columnsOpen ? <div className="fdm-sheet-backdrop" onClick={event => { if (event.target === event.currentTarget) setColumnsOpen(false); }}>
        <section className="fdm-sheet fdm-mm-sheet" role="dialog" aria-label="报价列">
          <div className="fdm-sheet-grip" aria-hidden="true" />
          <header className="fdm-sheet-head"><span><strong>报价列（最多 {QUOTE_COLUMNS_MAX} 个）</strong></span>
            <button type="button" className="fdm-sheet-done" onClick={() => setColumnsOpen(false)}>完成</button></header>
          <div className="fdm-sheet-body">
            <div className="fdm-mm-grid" role="group" aria-label="报价列">
              {QUOTE_COLUMNS.map(item => {
                const on = columns.includes(item.id);
                const blocked = on ? columns.length <= 1 : columns.length >= QUOTE_COLUMNS_MAX;
                return <button type="button" key={item.id} aria-pressed={on} disabled={blocked}
                  title={blocked ? on ? "至少留一列" : `最多 ${QUOTE_COLUMNS_MAX} 列，先去掉一列` : undefined}
                  onClick={() => toggleColumn(item.id)}>{item.label}</button>;
              })}
            </div>
          </div>
        </section>
      </div> : null}
    </main>
  );
}

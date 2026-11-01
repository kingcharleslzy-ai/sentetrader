import { useEffect, useMemo, useRef, useState } from "react";
import { Columns3, Search } from "lucide-react";
import { sameContract, type ForeignBoardSnapshot, type MarketBoardRow, type MarketBoardSnapshot } from "../../api";
import { useDisclosureSurface } from "../../uiSurfaces";
import { ChartPopover } from "../chartIndicatorEditor";
import { BoardColumnsEditor } from "./BoardColumnsEditor";
import { WatchlistStar } from "./WatchlistStar";
import {
  BOARD_COLUMN_LABELS, BOARD_EXCHANGES, FOREIGN_BOARD, boardPrice, formatBoardPrice, formatNumber, formatQuoteTime, formatSigned,
  formatTime, formatVolume, signClass, type BoardColumn, type BoardExchange, type BoardSort, type BoardSortKey
} from "./support";
import "./MarketBoard.css";

/** The columns a header click (and the boardSort command) may sort by; the others are plain labels. */
const SORT_KEYS: ReadonlySet<BoardColumn> = new Set(["changePercent", "volume", "openInterest", "openInterestChange"]);
/** Grid track per column beyond the fixed 品种/合约 pair. */
const GRID_TRACKS: Record<BoardColumn, string> = {
  last: "minmax(76px,1fr)", change: "minmax(76px,1fr)", changePercent: "minmax(76px,1fr)",
  volume: "minmax(86px,1.05fr)", openInterest: "minmax(88px,1.05fr)", openInterestChange: "minmax(80px,.95fr)",
  high: "minmax(76px,.95fr)", low: "minmax(76px,.95fr)", preSettlement: "minmax(76px,.95fr)",
  upperLimit: "minmax(76px,.95fr)", lowerLimit: "minmax(76px,.95fr)", quoteTime: "minmax(84px,.95fr)",
};

/** 当日无成交 / 盘前 / 读不到报价的行：灰显，涨跌幅位置改说原因。 */
function statusNote(row: MarketBoardRow): string | null {
  if (row.status === "market_quote_no_trade") return "未成交";
  if (row.status === "market_quote_pre_open") return "盘前";
  return null;
}

/** 外盘 rows in the board's row shape: the 合约 column says 主连 and the quote unit; no click-through. */
function foreignRows(snapshot: ForeignBoardSnapshot | undefined): MarketBoardRow[] {
  return (snapshot?.rows ?? []).map(row => ({
    symbol: row.key, instrumentId: `${row.contract || "主连"} · ${row.unit}`, instrumentName: row.name, productName: row.name,
    exchangeId: row.exchange, productId: row.key.split(".")[1] ?? row.key, last: row.last, preSettlement: row.preSettlement,
    change: row.change, changePercent: row.changePercent, volume: row.volume, openInterest: row.openInterest,
    openInterestChange: null, high: row.high, low: row.low, upperLimit: null, lowerLimit: null, quoteTime: row.quoteTime ?? "",
    bidPrice1: row.bidPrice1, askPrice1: row.askPrice1, status: "normal",
  }));
}

function quietRow(row: MarketBoardRow): boolean {
  return statusNote(row) !== null || row.status === "market_quote_unavailable";
}

/**
 * 报价总表: every product's main contract in one table — search, an exchange chip row, sortable
 * columns and a click-through to that contract's trade view. Which columns appear (and in what
 * order) is the account's marketBoard preference; the 列 popover edits it in place (2026-10-02).
 * A narrow board folds the tail of the list and says so at the head's end (+N 列已收起).
 * 自选 (2026-10-02): a star before each product name and a 自选 chip in front of the exchanges —
 * the filter keeps a grey 暂无报价 row for a starred product without a quote, so the list never
 * silently drops one.
 * 外盘 (2026-10-18, #976): a 外盘 chip after the exchanges shows the foreign main series (`foreign`, its own
 * read); the head says which source they came from. A 外盘 row opens its chart and carries the same star (its key is
 * the foreign product key, “COMEX.GC”); the 自选 filter lists the starred foreign rows after the domestic ones
 * (外盘代码统一 2026-10-21).
 */
export function MarketBoard({ snapshot, foreign, error, query, onQueryChange, exchange, onExchangeChange, sort, onSortChange,
  columns, onColumnsChange, columnsStatus, watchlist, productNames, onToggleWatchlist, watchlistStatus,
  availableExchanges, selectedSymbol, onSelect }: {
  snapshot: MarketBoardSnapshot | undefined;
  /** The 外盘 group's read (useForeignBoard), present while the 外盘 chip is on (or 自选 holds a foreign product). */
  foreign?: { snapshot?: ForeignBoardSnapshot; error: string };
  error: string;
  query: string;
  onQueryChange: (value: string) => void;
  exchange: BoardExchange;
  onExchangeChange: (value: BoardExchange) => void;
  sort: BoardSort;
  onSortChange: (sort: BoardSort) => void;
  /** The account's marketBoard.columns, in display order; 品种/合约 are fixed first columns. */
  columns: BoardColumn[];
  /** Present (and the account savable) when the 列 popover should offer editing; absent = read-only. */
  onColumnsChange?: (columns: BoardColumn[]) => void;
  /** The preference save path's inline status (已保存 / 错误原因). */
  columnsStatus?: string;
  /** The account's watchlist.products ("<交易所>.<品种>", newest last). */
  watchlist: string[];
  /** Product display names by watchlist key, for starred products the board has no row for. */
  productNames: Record<string, string>;
  /** Present (and the account savable) when the stars should toggle; absent = read-only. */
  onToggleWatchlist?: (product: string) => void;
  /** The watchlist save path's inline status (错误原因). */
  watchlistStatus?: string;
  /** Exchange codes present in the catalog; the chip row only shows these. */
  availableExchanges: string[];
  selectedSymbol: string | undefined;
  onSelect: (symbol: string) => void;
}) {
  const editable = Boolean(onColumnsChange);
  const starChange = onToggleWatchlist;
  const starred = useMemo(() => new Set(watchlist), [watchlist]);
  const chips = useMemo(() =>
    BOARD_EXCHANGES.filter(item => availableExchanges.includes(item.code)), [availableExchanges]);
  const isForeign = exchange === FOREIGN_BOARD;
  const rows = useMemo(() => {
    const abroad = foreignRows(foreign?.snapshot);
    const all = isForeign ? abroad : snapshot?.rows ?? [];
    const scoped = exchange === "all" || isForeign ? all
      : exchange === "watchlist" ? [...all.filter(row => starred.has(`${row.exchangeId}.${row.productId}`)),
        ...abroad.filter(row => starred.has(row.symbol))]
      : all.filter(row => row.exchangeId === exchange);
    const needle = query.trim().toLowerCase();
    const filtered = needle
      ? scoped.filter(row => `${row.productName}${row.productId}${row.instrumentId}${row.symbol}`.toLowerCase().includes(needle))
      : scoped;
    const factor = sort.order === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => {
      const left = a[sort.key], right = b[sort.key];
      if (left === null || left === undefined) return right === null || right === undefined ? 0 : 1;
      if (right === null || right === undefined) return -1;
      return (left - right) * factor || a.productName.localeCompare(b.productName, "zh-CN");
    });
  }, [snapshot, foreign?.snapshot, isForeign, exchange, query, sort, starred]);
  // Starred products the board has no row for (no main-contract quote right now): they stay as
  // grey rows after the quoted ones, so the user sees the product is still in the watchlist.
  const absent = useMemo(() => {
    if (exchange !== "watchlist" || !snapshot) return [] as { product: string; name: string }[];
    const quoted = new Set([...snapshot.rows.map(row => `${row.exchangeId}.${row.productId}`),
      ...(foreign?.snapshot?.rows ?? []).map(row => row.key)]);
    const needle = query.trim().toLowerCase();
    return watchlist
      .filter(product => !quoted.has(product))
      .map(product => ({ product, name: productNames[product] ?? product.split(".")[1] ?? product }))
      .filter(entry => !needle || `${entry.name}${entry.product}`.toLowerCase().includes(needle));
  }, [exchange, snapshot, foreign?.snapshot, watchlist, productNames, query]);
  const shownError = isForeign ? foreign?.error ?? "" : error;
  const updatedAt = isForeign ? foreign?.snapshot?.fetchedAt : snapshot?.capturedAt;
  const labels = foreign?.snapshot?.sourceLabels ?? {};
  const rowSource = new Map((foreign?.snapshot?.rows ?? []).map(row => [row.key, labels[row.source] ?? row.source]));
  const foreignSources = isForeign ? [...new Set(rowSource.values())].join("、") : "";
  const foreignById = new Map((foreign?.snapshot?.rows ?? []).map(row => [row.key, row]));
  // Every source failed: the whole snapshot is the last good one, so every row reads as stale.
  const foreignStale = (key: string) => foreign?.snapshot?.status === "stale" || Boolean(foreignById.get(key)?.stale);
  const sortHeader = (column: BoardColumn) => {
    const label = BOARD_COLUMN_LABELS[column];
    const active = sort.key === column;
    return (
      <button type="button" className={`fdw-board-sort ${active ? "active" : ""}`}
        aria-label={`${label}排序`} onClick={() => onSortChange({ key: column as BoardSortKey, order: active && sort.order === "desc" ? "asc" : "desc" })}>
        {label}<i aria-hidden="true">{active ? (sort.order === "desc" ? "▼" : "▲") : "↕"}</i>
      </button>
    );
  };
  // 列设置 popover: a commandable disclosure surface, fixed-positioned clear of the scroll body.
  const columnsButtonRef = useRef<HTMLButtonElement>(null);
  const [popover, setPopover] = useState<{ rect: DOMRect }>();
  const openColumns = () => {
    const rect = columnsButtonRef.current?.getBoundingClientRect();
    if (rect) setPopover({ rect });
  };
  useDisclosureSurface("board-columns", {
    state: popover !== undefined, available: editable, apply: open => (open ? openColumns() : setPopover(undefined)),
  });
  // Narrow folding: the table keeps its first columns and says how many folded away. Without a
  // ResizeObserver (jsdom) nothing folds, so every configured column renders.
  const boardRef = useRef<HTMLDivElement>(null);
  const [boardWidth, setBoardWidth] = useState<number | undefined>(undefined);
  useEffect(() => {
    const element = boardRef.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(entries => {
      for (const entry of entries) setBoardWidth(entry.contentRect.width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  // As many columns as the width holds (two fixed columns ≈ 196 px with padding, a data column ≈
  // 88 px with its gap); when some fold, the hint (≈ 88 px) must still be visible without scrolling.
  const fitting = (width: number) => Math.floor(width / 88);
  const foldTo = boardWidth === undefined || fitting(boardWidth - 196) >= columns.length
    ? columns.length : fitting(boardWidth - 196 - 88);
  const shownColumns = columns.slice(0, Math.max(1, foldTo));
  const foldedCount = Math.max(0, columns.length - shownColumns.length);
  const gridTemplate = ["minmax(96px,1.25fr)", "minmax(72px,.85fr)",
    ...shownColumns.map(column => GRID_TRACKS[column]),
    ...(foldedCount ? ["auto"] : [])].join(" ");
  const cell = (column: BoardColumn, row: MarketBoardRow) => {
    // 外盘 prices keep their own precision (the exchange tick: NG 3.125, HG 6.5705), not the board's two decimals.
    const foreignRow = foreignById.get(row.symbol);
    if (foreignRow && (column === "last" || column === "high" || column === "low" || column === "preSettlement" || column === "change")) {
      const value = row[column];
      const text = value === null || value === undefined ? "—"
        : column === "change" ? `${value > 0 ? "+" : ""}${formatNumber(value, foreignRow.decimals)}` : formatNumber(value, foreignRow.decimals);
      return <span key={column} className={`fdw-board-cell num ${column === "change" ? signClass(value) : ""}`}>{text}</span>;
    }
    switch (column) {
      case "last": {
        const price = boardPrice(row);
        return <span key={column} className={`fdw-board-cell num${price.fallback ? " fallback" : ""}`}
          title={price.fallback ? "还没有成交，显示昨结算价" : undefined}>{price.text}</span>;
      }
      case "change":
        return <span key={column} className={`fdw-board-cell num ${signClass(row.change)}`}>{row.change === null || row.change === undefined ? "—" : formatSigned(row.change, 2)}</span>;
      case "changePercent": {
        const note = statusNote(row);
        const percent = row.changePercent;
        return <span key={column} className={`fdw-board-cell num ${quietRow(row) ? "" : signClass(percent)}`}>{note ?? (percent === null || percent === undefined ? "—" : `${percent > 0 ? "+" : ""}${percent.toFixed(2)}%`)}</span>;
      }
      case "volume":
        return <span key={column} className="fdw-board-cell num">{formatVolume(row.volume)}</span>;
      case "openInterest":
        return <span key={column} className="fdw-board-cell num">{formatVolume(row.openInterest)}</span>;
      case "openInterestChange":
        return <span key={column} className={`fdw-board-cell num ${signClass(row.openInterestChange)}`}>
          {row.openInterestChange === null || row.openInterestChange === undefined ? "—" : `${row.openInterestChange > 0 ? "+" : ""}${formatVolume(row.openInterestChange)}`}</span>;
      case "quoteTime":
        return <span key={column} className="fdw-board-cell num">{formatQuoteTime(row.quoteTime)}</span>;
      default:
        return <span key={column} className="fdw-board-cell num">{formatBoardPrice(row[column])}</span>;
    }
  };
  return (
    <div className="fdw-quote-board" data-agent-target="board" data-testid="market-board" ref={boardRef}>
      <div className="fdw-board-top">
        <label className="fdw-search fdw-board-search"><Search aria-hidden="true" />
          <input value={query} onChange={event => onQueryChange(event.target.value)} placeholder="搜索品种名或代码" aria-label="搜索品种名或代码" /></label>
        <div className="fdw-board-chips" role="group" aria-label="交易所筛选">
          {watchlist.length ? <button type="button" aria-pressed={exchange === "watchlist"} className={exchange === "watchlist" ? "active" : ""}
            onClick={() => onExchangeChange("watchlist")}>自选</button> : null}
          <button type="button" aria-pressed={exchange === "all"} className={exchange === "all" ? "active" : ""} onClick={() => onExchangeChange("all")}>全部</button>
          {chips.map(item => (
            <button type="button" key={item.code} aria-pressed={exchange === item.code} className={exchange === item.code ? "active" : ""}
              onClick={() => onExchangeChange(item.code)}>{item.label}</button>
          ))}
          <button type="button" aria-pressed={isForeign} className={isForeign ? "active" : ""}
            onClick={() => onExchangeChange(FOREIGN_BOARD)}>外盘</button>
        </div>
        <span className="fdw-board-meta">
          {watchlistStatus ? <span className="fdw-board-error" role="status">{watchlistStatus}</span> : null}
          {shownError ? <span className="fdw-board-error" role="status">{shownError}</span> : null}
          {!isForeign && snapshot?.truncated ? <span className="fdw-board-truncated">品种较多，只显示成交最活跃的部分</span> : null}
          {isForeign && foreignSources ? <span className="fdw-board-updated">来源 {foreignSources}{foreign?.snapshot?.status === "stale" ? "（上次取到的价格）" : ""}</span> : null}
          <span className="fdw-board-updated">{updatedAt ? `更新于 ${formatTime(updatedAt)}` : "正在读取报价…"}</span>
        </span>
        {editable ? <button type="button" className="fdw-board-columns-button" ref={columnsButtonRef} aria-haspopup="dialog"
          title="选择显示哪些列、调整顺序" onClick={openColumns}><Columns3 aria-hidden="true" />列</button> : null}
      </div>
      <div className="fdw-board-head" style={{ gridTemplateColumns: gridTemplate }}>
        <span className="fdw-board-cell name">品种</span>
        <span className="fdw-board-cell">合约</span>
        {shownColumns.map(column => (
          <span className="fdw-board-cell num" key={column}>
            {SORT_KEYS.has(column) ? sortHeader(column) : BOARD_COLUMN_LABELS[column]}
          </span>
        ))}
        {foldedCount ? <button type="button" className="fdw-board-fold" onClick={openColumns}>+{foldedCount} 列已收起</button> : null}
      </div>
      <div className="fdw-board-body">
        {rows.map(row => {
          const quiet = quietRow(row);
          const product = `${row.exchangeId}.${row.productId}`;
          if (foreignById.has(row.symbol)) {
            // 报价页外盘行可点 (所有者 2026-10-19 方案 A): opens the same chart as 合约选择's 海外主连.
            const chart = foreignById.get(row.symbol)?.chartSymbol ?? undefined;
            const open = chart ? () => onSelect(chart) : undefined;
            return (
              <div key={row.symbol} className={`fdw-board-row foreign ${foreignStale(row.symbol) ? "quiet" : ""} ${chart && sameContract(chart, selectedSymbol) ? "active" : ""}`}
                style={{ gridTemplateColumns: gridTemplate }} aria-label={`${row.productName} ${row.exchangeId}`}
                role={open ? "button" : undefined} tabIndex={open ? 0 : undefined} onClick={open}
                onKeyDown={open ? event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } } : undefined}
                title={`来源：${rowSource.get(row.symbol) ?? ""}${foreignStale(row.symbol) ? "（本次没取到，上次的价格）" : ""}`}>
                <span className="fdw-board-cell name">
                  {starChange ? <WatchlistStar product={row.symbol} name={row.productName} starred={starred.has(row.symbol)} onToggle={starChange} /> : null}
                  <strong>{row.productName}</strong><small>{row.exchangeId}</small>
                </span>
                <span className="fdw-board-cell">{row.instrumentId}</span>
                {shownColumns.map(column => cell(column, row))}
              </div>
            );
          }
          return (
            // A div, not a button: the row holds the star button (HTML forbids nesting one button
            // in another); Enter and Space keep the row's own click-through reachable.
            <div role="button" tabIndex={0} key={row.symbol}
              className={`fdw-board-row ${quiet ? "quiet" : ""} ${sameContract(row.symbol, selectedSymbol) ? "active" : ""}`}
              aria-label={`${row.productName} ${row.symbol}`}
              style={{ gridTemplateColumns: gridTemplate }}
              onClick={() => onSelect(row.symbol)}
              onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(row.symbol); } }}>
              <span className="fdw-board-cell name">
                {starChange ? <WatchlistStar product={product} name={row.productName} starred={starred.has(product)} onToggle={starChange} /> : null}
                <strong>{row.productName}</strong><small>{row.productId.toUpperCase()}</small>
              </span>
              <span className="fdw-board-cell">{row.instrumentId}</span>
              {shownColumns.map(column => cell(column, row))}
            </div>
          );
        })}
        {absent.map(entry => (
          <div key={entry.product} className="fdw-board-row quiet absent" style={{ gridTemplateColumns: gridTemplate }}
            aria-label={`${entry.name} 暂无报价`}>
            <span className="fdw-board-cell name">
              {starChange ? <WatchlistStar product={entry.product} name={entry.name} starred onToggle={starChange} /> : null}
              <strong>{entry.name}</strong><small>{entry.product}</small>
            </span>
            <span className="fdw-board-cell">暂无报价</span>
          </div>
        ))}
        {!rows.length && !absent.length ? <p className="fdw-board-empty">{
          exchange === "watchlist" && !watchlist.length ? "点品种前的 ☆ 加入自选"
          : isForeign ? (foreign?.snapshot?.rows.length ? "没有匹配的品种" : shownError ? "外盘暂时不可用" : "正在读取外盘…")
          : snapshot ? (query ? "没有匹配的品种" : "暂无报价") : error ? "报价总表暂时不可用" : "正在读取报价…"}</p> : null}
      </div>
      {popover && onColumnsChange ? (
        <ChartPopover title="列设置" anchorRect={popover.rect} onClose={() => setPopover(undefined)} width={280}>
          <BoardColumnsEditor columns={columns} onChange={onColumnsChange} status={columnsStatus} />
        </ChartPopover>
      ) : null}
    </div>
  );
}

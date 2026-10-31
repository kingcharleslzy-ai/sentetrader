import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { useState } from "react";
import type { MarketBoardRow, MarketBoardSnapshot } from "../../api";
import { BoardColumnsEditor } from "./BoardColumnsEditor";
import { MarketBoard } from "./MarketBoard";
import { DEFAULT_BOARD_COLUMNS, normalizeWatchlist, watchlistKey, type BoardColumn, type BoardExchange, type BoardSort } from "./support";

const row = (over: Partial<MarketBoardRow>): MarketBoardRow => ({
  symbol: "SHFE.rb2610", instrumentId: "rb2610", instrumentName: "螺纹钢2610", productName: "螺纹钢",
  exchangeId: "SHFE", productId: "rb", last: 3118, preSettlement: 3087, change: 31, changePercent: 1,
  volume: 1_523_000, openInterest: 2_017_000, openInterestChange: -34_000, high: 3130, low: 3080,
  upperLimit: 3334, lowerLimit: 2840, quoteTime: "2026-10-01 10:32:05.000000", status: "normal", ...over,
});

const snapshot = {
  schemaVersion: 1, capturedAt: "2026-10-01T02:32:05Z", dataStatus: "normal", revision: "e:1", truncated: false,
  rows: [
    row({}),
    row({ symbol: "SHFE.ag2612", instrumentId: "ag2612", instrumentName: "白银2612", productName: "白银", productId: "ag", changePercent: 2.26, change: 186, volume: 981_000 }),
    row({ symbol: "DCE.m2701", instrumentId: "m2701", instrumentName: "豆粕2701", productName: "豆粕", exchangeId: "DCE", productId: "m", changePercent: -0.76, change: -22, volume: 880_000 }),
    row({ symbol: "DCE.jd2611", instrumentId: "jd2611", instrumentName: "鸡蛋2611", productName: "鸡蛋", exchangeId: "DCE", productId: "jd", last: null, preSettlement: 3402, change: null, changePercent: null, status: "market_quote_no_trade" }),
  ],
} as MarketBoardSnapshot;

function board({ exchange = "all", sort = { key: "changePercent", order: "desc" }, query = "", error = "",
  columns = DEFAULT_BOARD_COLUMNS, onColumnsChange = vi.fn(), watchlist = [], productNames = {},
  onToggleWatchlist = vi.fn() }: {
  exchange?: BoardExchange; sort?: BoardSort; query?: string; error?: string;
  columns?: BoardColumn[]; onColumnsChange?: (columns: BoardColumn[]) => void;
  watchlist?: string[]; productNames?: Record<string, string>;
  onToggleWatchlist?: (product: string) => void;
} = {}) {
  const onSelect = vi.fn(), onSortChange = vi.fn(), onExchangeChange = vi.fn();
  render(<MarketBoard snapshot={snapshot} error={error} query={query} onQueryChange={vi.fn()} exchange={exchange} onExchangeChange={onExchangeChange}
    sort={sort} onSortChange={onSortChange} columns={columns} onColumnsChange={onColumnsChange}
    watchlist={watchlist} productNames={productNames} onToggleWatchlist={onToggleWatchlist}
    availableExchanges={["SHFE", "DCE"]} selectedSymbol="SHFE.rb2610" onSelect={onSelect} />);
  return { onSelect, onSortChange, onColumnsChange, onExchangeChange, onToggleWatchlist };
}

const rows = () => Array.from(document.querySelectorAll<HTMLElement>(".fdw-board-row"));
const order = () => rows().map(item => item.textContent?.match(/[a-z]+\d{4}/)?.[0]);

it("sorts by 涨跌幅 high to low, with a no-trade row last and marked", () => {
  board();
  expect(order()).toEqual(["ag2612", "rb2610", "m2701", "jd2611"]);
  expect(screen.getByText("未成交")).toBeVisible();
});

it("filters by exchange and by search text", () => {
  board({ exchange: "DCE" });
  expect(order()).toEqual(["m2701", "jd2611"]);
  document.body.innerHTML = "";
  board({ query: "白银" });
  expect(order()).toEqual(["ag2612"]);
});

it("a header click asks for that sort; a second click on the active one flips it", () => {
  const { onSortChange } = board();
  fireEvent.click(screen.getByRole("button", { name: /成交量/ }));
  expect(onSortChange).toHaveBeenLastCalledWith({ key: "volume", order: "desc" });
  fireEvent.click(screen.getByRole("button", { name: /涨跌幅/ }));
  expect(onSortChange).toHaveBeenLastCalledWith({ key: "changePercent", order: "asc" });
});

it("clicking a row selects that contract", () => {
  const { onSelect } = board();
  fireEvent.click(rows().find(item => item.textContent?.includes("m2701"))!);
  expect(onSelect).toHaveBeenCalledWith("DCE.m2701");
});

it("a failed refresh keeps the table and shows the error line", () => {
  board({ error: "报价总表暂时不可用，稍后自动重试" });
  expect(screen.getByText("报价总表暂时不可用，稍后自动重试")).toBeVisible();
  expect(rows()).toHaveLength(4);
  expect(within(document.querySelector<HTMLElement>(".fdw-quote-board")!).getByText("白银")).toBeVisible();
});

it("the 自选 chip appears only with a starred product and switches the filter", () => {
  board();
  expect(screen.queryByRole("button", { name: "自选" })).toBeNull();
  document.body.innerHTML = "";
  const starred = board({ watchlist: ["SHFE.rb"] });
  expect(screen.getByRole("button", { name: "自选" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "自选" }));
  expect(starred.onExchangeChange).toHaveBeenCalledWith("watchlist");
});

it("自选 keeps a starred product without a quote as a grey 暂无报价 row", () => {
  board({ exchange: "watchlist", watchlist: ["SHFE.rb", "SHFE.hc"], productNames: { "SHFE.rb": "螺纹钢", "SHFE.hc": "热卷" } });
  expect(order().filter(Boolean)).toEqual(["rb2610"]);   // the grey row carries no contract code
  const absent = rows().find(item => item.textContent?.includes("热卷"))!;
  expect(absent).toHaveClass("absent");
  expect(within(absent).getByText("暂无报价")).toBeVisible();
  expect(within(absent).getByRole("button", { name: "移出自选 热卷" })).toBeInTheDocument();
});

it("a star toggles its product without selecting the row, and shows the current state", () => {
  const { onToggleWatchlist, onSelect } = board({ watchlist: ["SHFE.rb"] });
  fireEvent.click(screen.getByRole("button", { name: "移出自选 螺纹钢" }));
  expect(onToggleWatchlist).toHaveBeenCalledWith("SHFE.rb");
  expect(onSelect).not.toHaveBeenCalled();
  document.body.innerHTML = "";
  board({ watchlist: ["SHFE.rb"] });
  expect(screen.getByRole("button", { name: "加入自选 白银" })).toHaveAttribute("aria-pressed", "false");
});

it("自选 with nothing starred yet explains how to add one", () => {
  board({ exchange: "watchlist" });
  expect(screen.getByText("点品种前的 ☆ 加入自选")).toBeVisible();
});

it("a row without a trade yet shows 昨结算 greyed in the price cell", () => {
  board();
  const quiet = rows().find(item => item.textContent?.includes("jd2611"))!;
  const price = within(quiet).getByText("3,402");
  expect(price).toHaveClass("fallback");
  expect(price).toHaveAttribute("title", "还没有成交，显示昨结算价");
  const live = within(rows().find(item => item.textContent?.includes("rb2610"))!).getByText("3,118");
  expect(live).not.toHaveClass("fallback");
});

it("the 列 editor saves complete lists: uncheck, check, move and 恢复默认", () => {
  const saved: BoardColumn[][] = [];
  const Harness = () => {
    const [columns, setColumns] = useState(DEFAULT_BOARD_COLUMNS);
    const change = (next: BoardColumn[]) => { saved.push(next); setColumns(next); };
    return <BoardColumnsEditor columns={columns} onChange={change} />;
  };
  render(<Harness />);
  fireEvent.click(screen.getByRole("checkbox", { name: "最高" }));
  expect(saved.at(-1)).toEqual(DEFAULT_BOARD_COLUMNS.filter(column => column !== "high"));
  fireEvent.click(screen.getByRole("checkbox", { name: "昨结" }));
  expect(saved.at(-1)).toEqual([...DEFAULT_BOARD_COLUMNS.filter(column => column !== "high"), "preSettlement"]);
  fireEvent.click(screen.getByRole("button", { name: "上移昨结" }));
  expect(saved.at(-1)).toEqual([...DEFAULT_BOARD_COLUMNS.filter(column => column !== "high" && column !== "low"), "preSettlement", "low"]);
  fireEvent.click(screen.getByRole("button", { name: "恢复默认" }));
  expect(saved.at(-1)).toEqual(DEFAULT_BOARD_COLUMNS);
});

it("the last shown column cannot be unchecked away", () => {
  const saved: BoardColumn[][] = [];
  const Harness = () => {
    const [columns, setColumns] = useState<BoardColumn[]>(["last"]);
    const change = (next: BoardColumn[]) => { saved.push(next); setColumns(next); };
    return <BoardColumnsEditor columns={columns} onChange={change} />;
  };
  render(<Harness />);
  expect(screen.getByRole("checkbox", { name: "最新" })).toBeDisabled();   // at least one column stays
  expect(saved).toEqual([]);
});

it("the board's 列 button opens the popover editor; an edit hands the caller the complete list", () => {
  const { onColumnsChange } = board();
  fireEvent.click(screen.getByRole("button", { name: "列" }));
  const editor = within(screen.getByRole("dialog", { name: "列设置" }));
  fireEvent.click(editor.getByRole("checkbox", { name: "昨结" }));
  expect(onColumnsChange).toHaveBeenLastCalledWith([...DEFAULT_BOARD_COLUMNS, "preSettlement"]);
});

it("the configured columns render in order, with the new price columns and 更新时间 formatted", () => {
  board({ columns: ["last", "preSettlement", "upperLimit", "lowerLimit", "quoteTime"] });
  const head = document.querySelector<HTMLElement>(".fdw-board-head")!;
  expect(within(head).getByText("昨结")).toBeVisible();
  expect(within(head).getByText("涨停")).toBeVisible();
  expect(within(head).getByText("跌停")).toBeVisible();
  expect(within(head).getByText("更新时间")).toBeVisible();
  expect(within(head).queryByText("最高")).toBeNull();
  expect(within(head).queryByText("成交量")).toBeNull();
  const live = rows().find(item => item.textContent?.includes("rb2610"))!;
  expect(within(live).getByText("3,087")).toBeVisible();     // 昨结
  expect(within(live).getByText("3,334")).toBeVisible();     // 涨停
  expect(within(live).getByText("2,840")).toBeVisible();     // 跌停
  expect(within(live).getByText("10:32:05")).toBeVisible();  // 更新时间
});

it("a narrow board folds the tail of the column list and the hint opens the popover", () => {
  class ResizeObserverStub {
    private readonly callback: (entries: Array<{ contentRect: { width: number } }>, observer: unknown) => void;
    constructor(callback: (entries: Array<{ contentRect: { width: number } }>, observer: unknown) => void) { this.callback = callback; }
    observe() { this.callback([{ contentRect: { width: 480 } }], this); }
    disconnect() { /* jsdom has nothing to detach. */ }
  }
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  try {
    board();
    const head = document.querySelector<HTMLElement>(".fdw-board-head")!;
    expect(within(head).queryByText("持仓量")).toBeNull();
    expect(within(head).getByText("+6 列已收起")).toBeVisible();
    const live = rows().find(item => item.textContent?.includes("rb2610"))!;
    expect(within(live).getByText("3,118")).toBeVisible();   // 最新
    expect(within(live).queryByText("3,080")).toBeNull();    // 最低 folded away
    fireEvent.click(within(head).getByRole("button", { name: "+6 列已收起" }));
    expect(screen.getByRole("dialog", { name: "列设置" })).toBeVisible();
  } finally {
    vi.unstubAllGlobals();
  }
});

it("the 外盘 chip asks for the foreign group, whose rows only quote and name their source", () => {
  const { onExchangeChange } = board();
  fireEvent.click(screen.getByRole("button", { name: "外盘" }));
  expect(onExchangeChange).toHaveBeenCalledWith("FOREIGN");
  document.body.innerHTML = "";
  const foreign = {
    status: "ok" as const, fetchedAt: "2026-10-17T21:00:00+00:00", sourceLabels: { sina: "新浪外盘", kqd: "快期外盘主连", mt5: "MT5（AMP）" },
    rows: [
      { key: "COMEX.GC", name: "COMEX黄金", exchange: "COMEX", currency: "USD", unit: "美元/盎司", last: 4184.9, preSettlement: 4170.1,
        change: 14.8, changePercent: 0.35, open: 4192.1, high: 4220.4, low: 4167.5, bidPrice1: 4186.5, askPrice1: 4187.1,
        volume: null, openInterest: null, quoteTime: "2026-10-17T04:59:56+08:00", source: "mt5", contract: "GCEZ26", decimals: 1, domestic: ["au"] },
      { key: "LMEOTC.CA", name: "伦铜", exchange: "LME", currency: "USD", unit: "美元/吨", last: 14347, preSettlement: null,
        change: null, changePercent: null, open: null, high: 14500, low: 14279, bidPrice1: null, askPrice1: null,
        volume: 184, openInterest: null, quoteTime: "2026-10-15 23:30:18", source: "kqd", decimals: 1, domestic: ["cu", "bc"] },
      { key: "NYMEX.NG", name: "美国天然气", exchange: "NYMEX", currency: "USD", unit: "美元/百万英热", last: 3.125, preSettlement: 3.1,
        change: 0.025, changePercent: 0.81, open: null, high: 3.2, low: 3.05, bidPrice1: null, askPrice1: null,
        volume: null, openInterest: null, quoteTime: null, source: "sina", decimals: 3, stale: true, domestic: [] },
    ],
  };
  const onSelect = vi.fn();
  render(<MarketBoard snapshot={snapshot} foreign={{ snapshot: foreign, error: "" }} error="" query="" onQueryChange={vi.fn()}
    exchange="FOREIGN" onExchangeChange={vi.fn()} sort={{ key: "changePercent", order: "desc" }} onSortChange={vi.fn()}
    columns={DEFAULT_BOARD_COLUMNS} watchlist={[]} productNames={{}} availableExchanges={["SHFE"]} selectedSymbol={undefined} onSelect={onSelect} />);
  expect(rows().map(item => item.getAttribute("aria-label"))).toEqual(["美国天然气 NYMEX", "COMEX黄金 COMEX", "伦铜 LME"]);
  expect(rows()[0]).toHaveTextContent("3.125");  // the exchange tick's precision, not the board's two decimals
  expect(rows()[0]).toHaveClass("quiet");  // its source failed this read: the last good row, greyed
  expect(screen.getByText("GCEZ26 · 美元/盎司")).toBeVisible();  // MT5 rows name their month contract
  expect(screen.getByText("主连 · 美元/吨")).toBeVisible();
  expect(screen.getByText("来源 MT5（AMP）、快期外盘主连、新浪外盘")).toBeVisible();
  fireEvent.click(rows()[1]);
  expect(onSelect).not.toHaveBeenCalled();  // quote-only: no trade view for a foreign series yet
  expect(within(rows()[2]).getAllByText("—").length).toBeGreaterThan(0);  // 快期 has no 昨结算: no change, never inferred
});


it("a whole 外盘 snapshot served stale greys every row", () => {
  const foreign = { status: "stale" as const, fetchedAt: "2026-10-17T21:00:00+00:00", sourceLabels: { sina: "新浪外盘" }, rows: [
    { key: "COMEX.GC", name: "COMEX黄金", exchange: "COMEX", currency: "USD", unit: "美元/盎司", last: 4184.9, preSettlement: 4170.1,
      change: 14.8, changePercent: 0.35, open: null, high: null, low: null, bidPrice1: null, askPrice1: null, volume: null,
      openInterest: null, quoteTime: null, source: "sina", decimals: 1, domestic: ["au"] }] };
  render(<MarketBoard snapshot={snapshot} foreign={{ snapshot: foreign, error: "" }} error="" query="" onQueryChange={vi.fn()}
    exchange="FOREIGN" onExchangeChange={vi.fn()} sort={{ key: "changePercent", order: "desc" }} onSortChange={vi.fn()}
    columns={DEFAULT_BOARD_COLUMNS} watchlist={[]} productNames={{}} availableExchanges={["SHFE"]} selectedSymbol={undefined} onSelect={vi.fn()} />);
  expect(rows()[0]).toHaveClass("quiet");
});

it("外盘行带星标（自选键是外盘品种键），自选筛选在国内品种后列出星标的外盘行", () => {
  const foreign = { status: "ok" as const, fetchedAt: "2026-10-21T01:00:00+00:00", sourceLabels: { mt5: "MT5（AMP）" }, rows: [
    { key: "COMEX.GC", name: "COMEX黄金", exchange: "COMEX", currency: "USD", unit: "美元/盎司", last: 4184.9, preSettlement: 4170.1,
      change: 14.8, changePercent: 0.35, open: null, high: null, low: null, bidPrice1: null, askPrice1: null, volume: null,
      openInterest: null, quoteTime: null, source: "mt5", contract: "GCEZ26", chartSymbol: "KQD.m@COMEX.GC", decimals: 1, domestic: ["au"] }] };
  const onToggle = vi.fn(), onSelect = vi.fn();
  const view = (exchange: BoardExchange, watchlist: string[]) => render(<MarketBoard snapshot={snapshot} foreign={{ snapshot: foreign, error: "" }}
    error="" query="" onQueryChange={vi.fn()} exchange={exchange} onExchangeChange={vi.fn()} sort={{ key: "changePercent", order: "desc" }}
    onSortChange={vi.fn()} columns={DEFAULT_BOARD_COLUMNS} watchlist={watchlist} productNames={{}} onToggleWatchlist={onToggle}
    availableExchanges={["SHFE"]} selectedSymbol={undefined} onSelect={onSelect} />);
  view("FOREIGN", []);
  fireEvent.click(screen.getByRole("button", { name: "加入自选 COMEX黄金" }));
  expect(onToggle).toHaveBeenCalledWith("COMEX.GC");
  expect(onSelect).not.toHaveBeenCalled();  // the star never opens the row's chart
  document.body.innerHTML = "";
  view("watchlist", ["SHFE.rb", "COMEX.GC"]);
  expect(rows().map(item => item.getAttribute("aria-label"))).toEqual(["螺纹钢 SHFE.rb2610", "COMEX黄金 COMEX"]);
  expect(screen.queryByText("暂无报价")).toBeNull();
  fireEvent.click(rows()[1]);
  expect(onSelect).toHaveBeenCalledWith("KQD.m@COMEX.GC");
});

it("海外主连的自选键是外盘品种键，与总表外盘行同一颗星", () => {
  expect(watchlistKey({ exchangeId: "KQD", productId: "COMEX.GC" })).toBe("COMEX.GC");
  expect(watchlistKey({ exchangeId: "SHFE", productId: "rb" })).toBe("SHFE.rb");
  expect(normalizeWatchlist(["COMEX.GC", "CME.6E", "KQD.CME.NQ", "shfe.rb"])).toEqual(["COMEX.GC", "CME.6E"]);
});

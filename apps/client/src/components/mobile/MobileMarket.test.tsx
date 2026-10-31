import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import type { MarketBoardSnapshot, MarketWorkbenchSnapshot } from "../../api";
import { normalizeChartIndicators } from "../chartIndicators";
import { useMarketBoard } from "../../hooks/useMarketBoard";
import { MobileQuotes } from "./MobileQuotes";
import { MobileMultiChart } from "./MobileMultiChart";
import { MobileSubPaneSheet } from "./MobileSubPaneSheet";
import { DEFAULT_MOBILE_MARKET, mobileMarketMatches, normalizeMobileMarket, planMobileMarketChange, resolveCells } from "./mobileMarketPrefs";

const boardRow = (symbol: string, exchangeId: string, productId: string, name: string, last: number, changePercent: number) => ({
  symbol, exchangeId, productId, instrumentId: symbol.split(".")[1], instrumentName: name, productName: name.replace(/\d+$/, ""),
  last, preSettlement: last, change: 0, changePercent, volume: 1000, openInterest: 213_000, openInterestChange: -240,
  high: last, low: last, upperLimit: null, lowerLimit: null, bidPrice1: last - 1, askPrice1: last + 1, quoteTime: "", status: "normal",
});
const board = { capturedAt: "", dataStatus: "normal", revision: "1", truncated: false,
  rows: [boardRow("SHFE.rb2701", "SHFE", "rb", "螺纹钢2701", 3076, -1.05), boardRow("DCE.m2701", "DCE", "m", "豆粕2701", 2918, 0.41)] } as unknown as MarketBoardSnapshot;
const position = (symbol: string) => ({ symbol, direction: "long" as const, volume: 2, unrealizedPnl: 0 }) as never;

/** IntersectionObserver stand-in: the test says which rows are on screen. */
let observed: { callback: IntersectionObserverCallback; elements: Element[] } | undefined;
class FakeObserver {
  elements: Element[] = [];
  constructor(callback: IntersectionObserverCallback) { observed = { callback, elements: this.elements }; }
  observe(element: Element) { this.elements.push(element); }
  disconnect() { /* nothing to release */ }
}
const scroll = (symbol: string, isIntersecting: boolean) => act(() => {
  const target = observed!.elements.find(element => (element as HTMLElement).dataset.symbol === symbol)!;
  observed!.callback([{ target, isIntersecting } as unknown as IntersectionObserverEntry], {} as IntersectionObserver);
});

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); observed = undefined; });

test("报价：只读屏幕上能看到的、总表之外的行；滚出屏幕就不再读（租约自己到期）", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("IntersectionObserver", FakeObserver);
  const fetch = vi.fn(async (_url: string) => new Response(JSON.stringify({ capturedAt: "", dataStatus: "normal",
    rows: [boardRow("SHFE.rb2705", "SHFE", "rb", "螺纹钢2705", 3050, 0.2)] }), { headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetch);
  const onOpen = vi.fn();
  render(<MobileQuotes baseUrl="https://runtime.test" board={board} boardId="watchlist" columns={["last", "changePercent", "openInterest"]}
    onBoard={vi.fn()} onColumns={vi.fn()} positions={[position("SHFE.rb2705"), position("SHFE.rb2710")]} watchlist={["SHFE.rb"]} instruments={[]}
    currentSymbol={undefined} onOpen={onOpen} guest={false} onSignIn={vi.fn()} />);
  // The starred product's main contract comes from the board: no read of its own.
  expect(screen.getByRole("button", { name: "看 螺纹钢2701 单图" })).toHaveTextContent("3,076");
  await act(async () => { await vi.advanceTimersByTimeAsync(3500); });
  expect(fetch).not.toHaveBeenCalled();
  scroll("SHFE.rb2705", true);
  await act(async () => { await vi.advanceTimersByTimeAsync(10); });
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][0]).toContain("/api/market/quotes?symbols=SHFE.rb2705");
  expect(screen.getByRole("button", { name: "看 SHFE.rb2705 单图" })).toHaveTextContent("3,050");
  scroll("SHFE.rb2705", false);
  await act(async () => { await vi.advanceTimersByTimeAsync(7000); });
  expect(fetch).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "看 螺纹钢2701 单图" }));
  expect(onOpen).toHaveBeenCalledWith("SHFE.rb2701");
});

test("报价：板块按交易所筛主力，左右滑切板块；列 1–4 个", () => {
  const onBoard = vi.fn();
  const onColumns = vi.fn();
  render(<MobileQuotes baseUrl={undefined} board={board} boardId="DCE" columns={["last", "changePercent", "openInterest", "bid"]}
    onBoard={onBoard} onColumns={onColumns} positions={[]} watchlist={[]} instruments={[]} currentSymbol={undefined} onOpen={vi.fn()} guest={false} onSignIn={vi.fn()} />);
  expect(screen.queryByRole("button", { name: /螺纹钢2701/ })).toBeNull();
  const row = screen.getByRole("button", { name: "看 豆粕2701 单图" });
  expect(row).toHaveTextContent("+0.41%");
  expect(row).toHaveTextContent("21.3万");
  const list = row.parentElement!;
  fireEvent.touchStart(list, { touches: [{ clientX: 300, clientY: 100 }] });
  fireEvent.touchEnd(list, { changedTouches: [{ clientX: 180, clientY: 110 }] });
  expect(onBoard).toHaveBeenLastCalledWith("CZCE");
  fireEvent.click(screen.getByRole("button", { name: "报价列设置" }));
  const sheet = screen.getByRole("dialog", { name: "报价列" });
  expect(within(sheet).getByRole("button", { name: "成交量" })).toBeDisabled();
  fireEvent.click(within(sheet).getByRole("button", { name: "买价" }));
  expect(onColumns).toHaveBeenLastCalledWith(["last", "changePercent", "openInterest"]);
});

const workbench = (symbol: string, duration: number, last: number) => ({ capturedAt: "", dataStatus: "normal", durationSeconds: duration,
  quote: { symbol, name: symbol, lastPrice: last, changePercent: 0.5, priceTick: 1 },
  bars: [0, 1, 2].map(index => ({ datetime: (index + 1) * 1e9, open: last, high: last + 2, low: last - 2, close: last + (index % 2 ? 1 : -1), volume: 1, openInterest: 1 })) }) as unknown as MarketWorkbenchSnapshot;

test("多图：失败保留上一份行情并显示本次原因，恢复后清除错误；复用单图也保留错误", async () => {
  vi.useFakeTimers();
  let round = 0;
  vi.stubGlobal("fetch", vi.fn(async () => {
    round += 1;
    return round === 2 ? new Response(JSON.stringify({ detail: "market_subscription_limit" }), { status: 503 })
      : new Response(JSON.stringify(workbench("SHFE.au2612", 300, round === 1 ? 900 : 901)), { headers: { "Content-Type": "application/json" } });
  }));
  render(<MobileMultiChart baseUrl="https://runtime.test" market={normalizeMobileMarket({ view: "multi" })} layout="four" onLayout={vi.fn()} onCell={vi.fn()}
    current={{ symbol: "SHFE.rb2701", duration: 300 }} ownMarket={workbench("SHFE.rb2701", 300, 3076)} ownError="行情重连中"
    autoSymbols={["SHFE.rb2701", "SHFE.au2612"]} candidates={[]} instruments={[]} onOpen={vi.fn()} guest={false} onSignIn={vi.fn()} active />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  const cell = screen.getByTestId("mobile-multi-cell-1");
  expect(within(screen.getByTestId("mobile-multi-cell-0")).getByRole("alert")).toHaveTextContent(/上次行情.*行情重连中/);
  expect(cell).toHaveTextContent("900");
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(cell).toHaveTextContent("900");
  expect(within(cell).getByRole("alert")).toHaveTextContent(/上次行情.*订阅数已满/);
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(cell).toHaveTextContent("901");
  expect(within(cell).queryByRole("alert")).toBeNull();
});

test("报价：总表和持仓月份的旧值旁显示各自本次失败，下一次成功清除对应错误", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("IntersectionObserver", FakeObserver);
  let round = 0;
  vi.stubGlobal("fetch", vi.fn(async () => {
    round += 1;
    if (round === 3) throw new TypeError("Failed to fetch");
    const rows = round === 2 ? [{ symbol: "SHFE.rb2705", errorCode: "market_quote_not_ready" }]
      : [boardRow("SHFE.rb2705", "SHFE", "rb", "螺纹钢2705", round === 1 ? 3050 : 3051, 0.2)];
    return new Response(JSON.stringify({ capturedAt: "", dataStatus: "normal", rows }), { headers: { "Content-Type": "application/json" } });
  }));
  render(<MobileQuotes baseUrl="https://runtime.test" board={board} boardError="报价总表读取失败" boardId="watchlist" columns={["last"]}
    onBoard={vi.fn()} onColumns={vi.fn()} positions={[position("SHFE.rb2705")]} watchlist={["SHFE.rb"]} instruments={[]}
    currentSymbol={undefined} onOpen={vi.fn()} guest={false} onSignIn={vi.fn()} />);
  scroll("SHFE.rb2705", true);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  const row = screen.getByRole("button", { name: "看 SHFE.rb2705 单图" });
  expect(row).toHaveTextContent("3,050");
  expect(screen.getByRole("alert")).toHaveTextContent(/上次报价.*报价总表读取失败/);
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(row).toHaveTextContent("3,050");
  expect(within(row).getByRole("alert")).toHaveTextContent(/上次报价.*暂无报价/);
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(within(row).getByRole("alert")).toHaveTextContent(/上次报价.*暂时不可用/);
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(row).toHaveTextContent("3,051");
  expect(within(row).queryByRole("alert")).toBeNull();
});

test("多图：和单图同一合约周期的格子共用单图数据；订阅满的格子如实提示，其余照常；点格子进单图", async () => {
  const fetch = vi.fn(async (url: string) => {
    const symbol = decodeURIComponent(url.split("/api/markets/")[1].split("/")[0]);
    if (symbol === "DCE.m2701") return new Response(JSON.stringify({ detail: "market_subscription_limit" }), { status: 503 });
    return new Response(JSON.stringify(workbench(symbol, 300, 900)), { headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetch);
  const onOpen = vi.fn();
  const market = normalizeMobileMarket({ view: "multi", layout: "four" });
  render(<MobileMultiChart baseUrl="https://runtime.test" market={market} layout="four" onLayout={vi.fn()} onCell={vi.fn()}
    current={{ symbol: "SHFE.rb2701", duration: 300 }} ownMarket={workbench("SHFE.rb2701", 300, 3076)}
    autoSymbols={["SHFE.rb2701", "SHFE.au2612", "DCE.m2701", "CFFEX.IF2610", "INE.sc2611"]} candidates={[]} instruments={[]}
    onOpen={onOpen} guest={false} onSignIn={vi.fn()} active />);
  expect(await screen.findByText(/行情订阅已满，1 格本次读取失败/)).toBeVisible();
  const asked = fetch.mock.calls.map(call => decodeURIComponent(String(call[0]).split("/api/markets/")[1].split("/")[0]));
  expect(new Set(asked)).toEqual(new Set(["SHFE.au2612", "DCE.m2701", "CFFEX.IF2610"]));
  expect(screen.getByTestId("mobile-multi-cell-0")).toHaveTextContent("3,076");
  expect(screen.getByTestId("mobile-multi-cell-2")).toHaveTextContent("订阅数已满，稍后重试");
  await waitFor(() => expect(screen.getByTestId("mobile-multi-cell-1")).toHaveTextContent("900"));
  fireEvent.click(within(screen.getByTestId("mobile-multi-cell-1")).getByRole("button", { name: /^看 / }));
  expect(onOpen).toHaveBeenCalledWith("SHFE.au2612", 300);
});

test("多图格子：自动补位跳过已选的合约；同合约 4 周期跟着当前合约", () => {
  const market = normalizeMobileMarket({ cells: [{ symbol: null, duration: 300 }, { symbol: "SHFE.au2612", duration: 900 }] });
  expect(resolveCells(market, "SHFE.rb2701", ["SHFE.au2612", "DCE.m2701", "DCE.i2701"]).map(cell => [cell.symbol, cell.duration, cell.auto])).toEqual([
    ["SHFE.rb2701", 300, true], ["SHFE.au2612", 900, false], ["DCE.m2701", 300, true], ["DCE.i2701", 300, true]]);
  expect(resolveCells({ ...market, layout: "periods" }, "SHFE.rb2701", []).map(cell => `${cell.symbol}@${cell.duration}`))
    .toEqual(["SHFE.rb2701@300", "SHFE.rb2701@900", "SHFE.rb2701@3600", "SHFE.rb2701@86400"]);
});

test("副图：命令里没选的副图替换正在显示的那个（满 3 个时）；回执看账号文档（与 Runtime 同一规则）", () => {
  const resolve = (symbol: string) => symbol.toUpperCase() === "SHFE.RB2701" ? "SHFE.rb2701" : undefined;
  const market = { ...DEFAULT_MOBILE_MARKET, subPane: "macd" as const };
  expect(planMobileMarketChange({ subPane: "cci" }, market, ["kd", "macd", "rsi"], resolve))
    .toEqual({ market: { view: "single", subPane: "cci" }, subPaneList: ["kd", "cci", "rsi"] });
  expect(planMobileMarketChange({ subPane: "kd" }, market, ["kd", "macd"], resolve)).toEqual({ market: { view: "single", subPane: "kd" } });
  expect(() => planMobileMarketChange({ cell: { index: 4, duration: 300 } }, market, [], resolve)).toThrow("这个布局只有 4 格");
  expect(() => planMobileMarketChange({ cell: { index: 0, symbol: "SHFE.xx" } }, market, [], resolve)).toThrow("合约不在合约列表里");
  expect(planMobileMarketChange({ cell: { index: 0, symbol: "shfe.rb2701" } }, market, [], resolve).market?.cells?.[0])
    .toEqual({ symbol: "SHFE.rb2701", duration: 300 });
  const state = { mobileTab: "market", preferences: { ui: { mobileMarket: { ...market, view: "single" } }, chart: { subPaneList: ["kd", "macd"] } } };
  expect(mobileMarketMatches(state, { subPane: "macd" })).toBe(true);
  expect(mobileMarketMatches(state, { view: "multi" })).toBe(false);
  expect(mobileMarketMatches({ ...state, mobileTab: "ai" }, { view: "single" })).toBe(false);
  expect(mobileMarketMatches({ mobileTab: "market", disclosures: { "mobile-subpanes": true } }, { subPanes: true })).toBe(true);
});

test("副图设置的“均线”总开关：关掉再打开，原先单独打开的几条均线恢复原样（记在同一份图表设置的 maMuted）", () => {
  const onChange = vi.fn();
  const lines = [{ period: 5, enabled: true }, { period: 10, enabled: false }, { period: 20, enabled: true }, { period: 60, enabled: false }];
  const { rerender } = render(<MobileSubPaneSheet indicators={normalizeChartIndicators({ maLines: lines })} onChange={onChange} onClose={vi.fn()} />);
  const ma = () => within(screen.getByRole("group", { name: "主图指标" })).getByRole("button", { name: "均线" });
  fireEvent.click(ma());
  const off = onChange.mock.lastCall?.[0];
  expect(off.maMuted).toEqual([5, 20]);
  expect(off.maLines.every((line: { enabled: boolean }) => !line.enabled)).toBe(true);
  rerender(<MobileSubPaneSheet indicators={normalizeChartIndicators({ maLines: off.maLines, maMuted: off.maMuted })} onChange={onChange} onClose={vi.fn()} />);
  expect(ma()).toHaveAttribute("aria-pressed", "false");
  fireEvent.click(ma());
  expect(onChange.mock.lastCall?.[0]).toEqual({ maLines: lines, maMuted: [] });
});

test("副图设置：选副图与主图指标写同一份图表设置（电脑同一份）；满 3 个时其余灰显", () => {
  const onChange = vi.fn();
  const indicators = normalizeChartIndicators({ subPaneList: ["kd", "macd", "volume"], boll: { enabled: true } });
  render(<MobileSubPaneSheet indicators={indicators} onChange={onChange} onClose={vi.fn()} />);
  const panes = screen.getByRole("group", { name: "副图指标" });
  expect(within(panes).getByRole("button", { name: "CCI" })).toBeDisabled();
  fireEvent.click(within(panes).getByRole("button", { name: /^MACD/ }));
  expect(onChange).toHaveBeenLastCalledWith({ subPaneList: ["kd", "volume"] });
  const main = screen.getByRole("group", { name: "主图指标" });
  fireEvent.click(within(main).getByRole("button", { name: "SAR" }));
  expect(onChange).toHaveBeenLastCalledWith({ sar: { enabled: true, step: 0.02, max: 0.2 } });
  fireEvent.click(within(main).getByRole("button", { name: "无" }));
  expect(onChange.mock.lastCall?.[0]).toMatchObject({ boll: { enabled: false }, sar: { enabled: false } });
  expect(onChange.mock.lastCall?.[0].maLines.every((line: { enabled: boolean }) => !line.enabled)).toBe(true);
});

test("多图：已读格子成为单图目标但本次失败，仍保留该格行情", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(workbench("SHFE.au2612", 300, 900)),
    {headers: {"Content-Type": "application/json"}})));
  const props = {baseUrl: "https://runtime.test", market: normalizeMobileMarket({view: "multi"}), layout: "four" as const,
    onLayout: vi.fn(), onCell: vi.fn(), autoSymbols: ["SHFE.rb2701", "SHFE.au2612"], candidates: [], instruments: [],
    onOpen: vi.fn(), guest: false, onSignIn: vi.fn(), active: true};
  const hook = render(<MobileMultiChart {...props} current={{symbol: "SHFE.rb2701", duration: 300}}
    ownMarket={workbench("SHFE.rb2701", 300, 3076)} />);
  await act(async () => {await vi.advanceTimersByTimeAsync(0);});
  expect(screen.getByTestId("mobile-multi-cell-1")).toHaveTextContent("900");
  hook.rerender(<MobileMultiChart {...props} current={{symbol: "SHFE.au2612", duration: 300}}
    ownMarket={undefined} ownError="该合约暂无报价，等待下次行情更新" />);
  // Automatic cells move when the current symbol changes: the same already-read key is now cell 0.
  const cell = screen.getByTestId("mobile-multi-cell-0");
  expect(cell).toHaveTextContent("900");
  expect(within(cell).getByRole("alert")).toHaveTextContent(/上次行情.*暂无报价/);
  hook.rerender(<MobileMultiChart {...props} current={{symbol: "SHFE.au2612", duration: 300}}
    ownMarket={workbench("SHFE.au2612", 300, 901)} />);
  expect(cell).toHaveTextContent("901");
  expect(within(cell).queryByRole("alert")).toBeNull();
});

test.each([true, false])("报价：本次200遗漏的可见持仓月份标明失败，恢复清错（有上次报价=%s）", async hasLastGood => {
  vi.useFakeTimers(); vi.stubGlobal("IntersectionObserver", FakeObserver);
  const rb = boardRow("SHFE.rb2705", "SHFE", "rb", "螺纹钢2705", 3050, 0.2);
  const m = boardRow("DCE.m2705", "DCE", "m", "豆粕2705", 2900, 0.2);
  let round = 0;
  const fetch = vi.fn(async () => {
    ++round;
    const rows = round === 1 && hasLastGood || round >= 4 ? [rb, m]
      : round === 2 ? [rb, {symbol: m.symbol, errorCode: "market_subscription_limit"}] : [rb];
    return new Response(JSON.stringify({capturedAt: "", dataStatus: "normal", rows}), {headers: {"Content-Type": "application/json"}});
  });
  vi.stubGlobal("fetch", fetch);
  render(<MobileQuotes baseUrl="https://runtime.test" board={board} boardId="watchlist" columns={["last"]}
    onBoard={vi.fn()} onColumns={vi.fn()} positions={[position(rb.symbol), position(m.symbol)]} watchlist={[]}
    instruments={[]} currentSymbol={undefined} onOpen={vi.fn()} guest={false} onSignIn={vi.fn()} />);
  act(() => {scroll(rb.symbol, true); scroll(m.symbol, true);});
  await act(async () => {await vi.advanceTimersByTimeAsync(0);});
  const row = screen.getByRole("button", {name: `看 ${m.symbol} 单图`});
  if (hasLastGood) expect(row).toHaveTextContent("2,900");
  else expect(within(row).getByRole("alert")).toHaveTextContent(/本次未取到报价/);
  await act(async () => {await vi.advanceTimersByTimeAsync(3000);});
  expect(within(row).getByRole("alert")).toHaveTextContent(/订阅数已满/);
  await act(async () => {await vi.advanceTimersByTimeAsync(3000);});
  expect(within(row).getByRole("alert")).toHaveTextContent(/本次未取到报价/);
  if (hasLastGood) expect(row).toHaveTextContent("2,900");
  expect(within(screen.getByRole("button", {name: `看 ${rb.symbol} 单图`})).queryByRole("alert")).toBeNull();
  await act(async () => {await vi.advanceTimersByTimeAsync(3000);});
  expect(row).toHaveTextContent("2,900"); expect(within(row).queryByRole("alert")).toBeNull();
  act(() => {scroll(rb.symbol, false); scroll(m.symbol, false);});
  const reads = fetch.mock.calls.length;
  await act(async () => {await vi.advanceTimersByTimeAsync(6000);});
  expect(fetch).toHaveBeenCalledTimes(reads);
});

test("多图：换Runtime第一帧不显示旧格缓存，新读取失败也不复活旧Runtime的值", async () => {
  vi.useFakeTimers();
  let roundB = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.startsWith("https://runtime-b.test") && ++roundB === 1) {
      return new Response(JSON.stringify({detail: "market_quote_not_ready"}), {status: 503});
    }
    return new Response(JSON.stringify(workbench("SHFE.au2612", 300, url.startsWith("https://runtime-a.test") ? 900 : 901)),
      {headers: {"Content-Type": "application/json"}});
  }));
  const props = {market: normalizeMobileMarket({view: "multi"}), layout: "four" as const,
    onLayout: vi.fn(), onCell: vi.fn(), autoSymbols: ["SHFE.rb2701", "SHFE.au2612"], candidates: [], instruments: [],
    onOpen: vi.fn(), guest: false, onSignIn: vi.fn(), active: true,
    current: {symbol: "SHFE.rb2701", duration: 300}, ownMarket: workbench("SHFE.rb2701", 300, 3076)};
  const hook = render(<MobileMultiChart {...props} baseUrl="https://runtime-a.test" />);
  await act(async () => {await vi.advanceTimersByTimeAsync(0);});
  const cell = screen.getByTestId("mobile-multi-cell-1"); expect(cell).toHaveTextContent("900");
  hook.rerender(<MobileMultiChart {...props} baseUrl="https://runtime-b.test" />);
  expect(cell).not.toHaveTextContent("900");
  await act(async () => {await vi.advanceTimersByTimeAsync(0);});
  expect(within(cell).getByRole("alert")).toHaveTextContent(/暂无报价/);
  expect(cell).not.toHaveTextContent("上次行情"); expect(cell).not.toHaveTextContent("900");
  await act(async () => {await vi.advanceTimersByTimeAsync(5000);});
  expect(cell).toHaveTextContent("901"); expect(within(cell).queryByRole("alert")).toBeNull();
});

test("报价：换Runtime同步排除旧月份值与遗漏错误，新失败不复活旧报价，恢复后显示新值", async () => {
  vi.useFakeTimers(); vi.stubGlobal("IntersectionObserver", FakeObserver);
  const symbol = "SHFE.rb2705";
  let roundA = 0; let roundB = 0;
  let finishB!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.startsWith("https://runtime-b.test") && ++roundB === 1) {
      return await new Promise<Response>(resolve => { finishB = resolve; });
    }
    const rows = url.startsWith("https://runtime-a.test") && ++roundA === 2 ? []
      : [boardRow(symbol, "SHFE", "rb", "螺纹钢2705", url.startsWith("https://runtime-a.test") ? 3050 : 3051, 0.2)];
    return new Response(JSON.stringify({capturedAt: "", dataStatus: "normal", rows}), {headers: {"Content-Type": "application/json"}});
  }));
  const props = {board, boardId: "watchlist" as const, columns: ["last"] as const,
    onBoard: vi.fn(), onColumns: vi.fn(), positions: [position(symbol)], watchlist: [], instruments: [],
    currentSymbol: undefined, onOpen: vi.fn(), guest: false, onSignIn: vi.fn()};
  const view = render(<MobileQuotes {...props} columns={[...props.columns]} baseUrl="https://runtime-a.test" />);
  scroll(symbol, true);
  await act(async () => {await vi.advanceTimersByTimeAsync(0);});
  const row = screen.getByRole("button", {name: `看 ${symbol} 单图`});
  expect(row).toHaveTextContent("3,050");
  await act(async () => {await vi.advanceTimersByTimeAsync(3000);});
  expect(within(row).getByRole("alert")).toHaveTextContent(/上次报价.*本次未取到报价/);
  view.rerender(<MobileQuotes {...props} columns={[...props.columns]} baseUrl="https://runtime-b.test" />);
  expect(row).not.toHaveTextContent("3,050"); expect(within(row).queryByRole("alert")).toBeNull();
  await act(async () => {finishB(new Response(JSON.stringify({detail: "market_quote_not_ready"}), {status: 503}));});
  expect(within(row).getByRole("alert")).toHaveTextContent(/暂无报价/);
  expect(row).not.toHaveTextContent("上次报价"); expect(row).not.toHaveTextContent("3,050");
  await act(async () => {await vi.advanceTimersByTimeAsync(3000);});
  expect(row).toHaveTextContent("3,051"); expect(within(row).queryByRole("alert")).toBeNull();
});

test("报价：正式总表来源切换不保留旧Runtime的值或错误，新总表失败和恢复各归当前来源", async () => {
  vi.useFakeTimers();
  let roundA = 0; let roundB = 0; let finishB!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.startsWith("https://runtime-a.test") && ++roundA === 2) throw new TypeError("Runtime A offline");
    if (url.startsWith("https://runtime-b.test") && ++roundB === 1) {
      return await new Promise<Response>(resolve => {finishB = resolve;});
    }
    const snapshot = url.startsWith("https://runtime-a.test") ? board
      : {...board, rows: [boardRow("SHFE.rb2701", "SHFE", "rb", "螺纹钢2701", 3100, 0.2)]};
    return new Response(JSON.stringify(snapshot), {headers: {"Content-Type": "application/json"}});
  }));
  // FuturesDecisionWorkspace passes this shared owner directly through MobileMarket to MobileQuotes.
  function QuotesFromBoard({baseUrl}: {baseUrl: string}) {
    const read = useMarketBoard(baseUrl, true);
    return <MobileQuotes baseUrl={baseUrl} board={read.snapshot} boardError={read.error} boardId="main" columns={["last"]}
      onBoard={vi.fn()} onColumns={vi.fn()} positions={[]} watchlist={[]} instruments={[]} currentSymbol={undefined}
      onOpen={vi.fn()} guest={false} onSignIn={vi.fn()} />;
  }
  const view = render(<QuotesFromBoard baseUrl="https://runtime-a.test" />);
  await act(async () => {await vi.advanceTimersByTimeAsync(0);});
  expect(screen.getByRole("button", {name: "看 螺纹钢2701 单图"})).toHaveTextContent("3,076");
  await act(async () => {await vi.advanceTimersByTimeAsync(3000);});
  expect(screen.getByRole("alert")).toHaveTextContent(/上次报价.*Runtime A offline/);
  view.rerender(<QuotesFromBoard baseUrl="https://runtime-b.test" />);
  expect(screen.queryByRole("button", {name: "看 螺纹钢2701 单图"})).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  await act(async () => {finishB(new Response(JSON.stringify({detail: "Runtime B unavailable"}), {status: 503}));});
  expect(screen.getByRole("alert")).toHaveTextContent(/Runtime B unavailable/);
  expect(screen.getByRole("alert")).not.toHaveTextContent(/上次报价|Runtime A/);
  await act(async () => {await vi.advanceTimersByTimeAsync(3000);});
  expect(screen.getByRole("button", {name: "看 螺纹钢2701 单图"})).toHaveTextContent("3,100");
  expect(screen.queryByRole("alert")).toBeNull();
});

test("报价：外盘板块只列外盘主连报价、行不可点，写明来源", () => {
  const onOpen = vi.fn();
  const foreign = { status: "ok" as const, fetchedAt: "", sourceLabels: { sina: "新浪外盘" }, rows: [
    { key: "COMEX.GC", name: "COMEX黄金", exchange: "COMEX", currency: "USD", unit: "美元/盎司", last: 4184.9, preSettlement: 4170.1,
      change: 14.8, changePercent: 0.35, open: null, high: null, low: null, bidPrice1: 4186.5, askPrice1: 4187.1, volume: null,
      openInterest: null, quoteTime: null, source: "sina", decimals: 1, domestic: ["au"] }] };
  render(<MobileQuotes baseUrl="https://runtime.test" board={board} foreign={{ snapshot: foreign, error: "" }} boardId="FOREIGN"
    columns={["last", "changePercent", "bid"]} onBoard={vi.fn()} onColumns={vi.fn()} positions={[]} watchlist={[]} instruments={[]}
    currentSymbol={undefined} onOpen={onOpen} guest={false} onSignIn={vi.fn()} />);
  const row = screen.getByLabelText("COMEX黄金 外盘");
  expect(row).toHaveTextContent("COMEX 主连 · 美元/盎司");
  expect(row).toHaveTextContent("+0.35%");
  expect(screen.getByText("来源 新浪外盘")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /螺纹钢/ })).toBeNull();  // the domestic rows stay on their own 板块
  fireEvent.click(row);
  expect(onOpen).not.toHaveBeenCalled();
});

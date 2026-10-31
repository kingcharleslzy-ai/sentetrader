import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import { groupRounds } from "../accountPnl";
import { applyUiSurface, resetUiSurfaces, uiSurface } from "../uiSurfaces";
import { useChartFocus } from "./futures/useChartFocus";
import { rememberStrategyScopes, setActiveTradingAccount } from "../api";
import { AccountPnl } from "./AccountPnl";

const at = (day: string, hour: number) => Date.parse(`${day}T${String(hour).padStart(2, "0")}:00:00+08:00`) * 1e6;
const round = (id: string, symbol: string, day: string, hour: number, gross: number, extra: Partial<Record<string, unknown>> = {}) => ({
  id, symbol, direction: "short", volume: 3, openTime: at(day, hour - 1), closeTime: at(day, hour), openPrice: 3099, closePrice: 3045,
  gross, fees: null, net: null, approximate: false, tradingDay: day, ...extra });
const trades = [
  round("rb", "SHFE.rb2610", "2026-09-14", 10, 1620, { approximate: true }),
  round("au", "SHFE.au2612", "2026-09-11", 14, -500, { direction: "long", openPrice: 820, closePrice: 819.5, volume: 1 }),
  round("ag", "SHFE.ag2612", "2026-08-20", 11, 900),
];
const pnl = { initialBalance: 1_000_000, equity: 1_036_961.2, totalPnl: 36961.2, today: 1620, thisMonth: 1120, tradeCount: 3, winRate: 0.6667,
  realizedNet: 2020, costSource: "account", dataStatus: "normal", skippedSymbols: [], months: [], days: [], trades,
  accountName: "TqKq 模拟账户", cumulativeCommission: 42.5, commissionSince: "2026-09-14", tradingDay: "2026-09-14" };
const fees = { source: "account", default: { mode: "per_lot", open: 0, closeToday: 0, closeYesterday: 0 }, overrides: [] };
const serve = (url: string) => new Response(JSON.stringify(url.includes("/trading-costs") ? fees : pnl), { status: 200, headers: { "Content-Type": "application/json" } });
const renderPnl = (extra: Partial<React.ComponentProps<typeof AccountPnl>> = {}) => {
  const onFocus = vi.fn(); const onSelectSymbol = vi.fn(); const onInitialBalanceChange = vi.fn();
  render(<AccountPnl baseUrl="http://127.0.0.1:8000" rootUrl="http://127.0.0.1:8000" symbol="SHFE.au2612" onFocus={onFocus} onSelectSymbol={onSelectSymbol}
    initialBalance={null} onInitialBalanceChange={onInitialBalanceChange} {...extra} />);
  return { onFocus, onSelectSymbol, onInitialBalanceChange };
};

afterEach(() => { cleanup(); vi.unstubAllGlobals(); resetUiSurfaces(); setActiveTradingAccount(null); });

it("groups round trips by month, then trading day, newest first", () => {
  const months = groupRounds(trades as never);
  expect(months.map(m => [m.key, m.realizedNet, m.tradeCount, m.wins, m.approximate])).toEqual([
    ["2026-09", 1120, 2, 1, true], ["2026-08", 900, 1, 1, false]]);
  expect(months[0].days.map(d => [d.key, d.realizedNet, d.trades.map(t => t.id)])).toEqual([
    ["2026-09-14", 1620, ["rb"]], ["2026-09-11", -500, ["au"]]]);
});

it("clears the selected trade and chart focus when the account changes at the same Runtime", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => serve(String(input instanceof Request ? input.url : input))));
  const onFocus = vi.fn();
  const view = (accountId: string) => <AccountPnl baseUrl="http://127.0.0.1:8000" rootUrl="http://127.0.0.1:8000"
    symbol="SHFE.rb2610" accountId={accountId} onFocus={onFocus} onSelectSymbol={vi.fn()}
    initialBalance={null} onInitialBalanceChange={vi.fn()} />;
  const { rerender } = render(view("A"));
  fireEvent.click(await screen.findByRole("button", { name: /SHFE.rb2610 · 空 3 手/ }));
  expect(onFocus).toHaveBeenLastCalledWith(expect.objectContaining({ id: "rb" }));
  rerender(view("B"));
  await screen.findByRole("button", { name: /SHFE.rb2610 · 空 3 手/ });
  expect(onFocus).toHaveBeenLastCalledWith(undefined);
  expect(screen.queryByRole("button", { name: "显示全图" })).toBeNull();
});

it("shows the four account metrics and expands month → day → trade without an estimate label", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => serve(String(input instanceof Request ? input.url : input))));
  const { onFocus, onSelectSymbol } = renderPnl();
  const cards = (await screen.findByRole("button", { name: "总盈亏，查看账户明细" })).closest("dl")!;
  const values = () => within(cards).getAllByRole("definition").map(item => item.firstChild?.textContent);
  expect(within(cards).getAllByRole("term").map(term => term.textContent)).toEqual(["总盈亏", "今日", "本月平仓", "胜率 / 笔数"]);
  expect(values()).toEqual(["+36,961", "+1,620", "+1,120", "67% / 3"]);
  // One rule for the whole block: equity-based total versus closed-trade figures.
  expect(within(screen.getByRole("region", { name: "账户" })).getByText("总盈亏按账户权益算，含持仓浮动与手续费；本月平仓和按月、按日只算已平仓的交易")).toBeTruthy();
  expect(within(cards).getByText("已平仓毛收益")).toBeTruthy();
  // The latest month and its latest day open by default; a backfilled leg is not labelled in the rows.
  const months = screen.getByRole("list", { name: "按月" });
  expect(within(months).getAllByRole("button", { expanded: true }).map(b => b.textContent)).toEqual(
    expect.arrayContaining([expect.stringContaining("2026 年 9 月"), expect.stringContaining("9 月 14 日")]));
  expect(screen.queryByText(/按委托补录/)).toBeNull();
  expect(screen.queryByText(/权益 .* − 初始/)).toBeNull();
  expect(screen.queryByText(/8 月 20 日/)).toBeNull();
  // Opening another month shows its days; opening a day lists its trades.
  fireEvent.click(screen.getByRole("button", { name: /2026 年 8 月/ }));
  fireEvent.click(await screen.findByRole("button", { name: /8 月 20 日/ }));
  const dayTrades = screen.getByRole("list", { name: "8 月 20 日成交" });
  expect(within(dayTrades).getByRole("button").textContent).toContain("SHFE.ag2612 · 空 3 手");
  // Selecting a trade focuses the chart and switches the chart to that contract.
  fireEvent.click(within(dayTrades).getByRole("button"));
  expect(onSelectSymbol).toHaveBeenCalledWith("SHFE.ag2612");
  expect(onFocus).toHaveBeenLastCalledWith(expect.objectContaining({ id: "ag", openTime: at("2026-08-20", 10), closeTime: at("2026-08-20", 11) }));
  // The contract filter keeps the per-contract review.
  fireEvent.change(screen.getByRole("combobox", { name: "合约筛选" }), { target: { value: "SHFE.au2612" } });
  await waitFor(() => expect(screen.queryByRole("button", { name: /2026 年 8 月/ })).toBeNull());
  expect(values()[3]).toBe("0% / 1");
});


it("opens the account detail card from 总盈亏 and the fee hint card from the icon, each reaching settings", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => serve(String(input instanceof Request ? input.url : input))));
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  const { onInitialBalanceChange } = renderPnl({ initialBalance: 900_000 });
  fireEvent.click(await screen.findByRole("button", { name: "总盈亏，查看账户明细" }));
  const detail = screen.getByRole("dialog", { name: "账户明细" });
  expect(detail).toHaveAttribute("open");
  expect(within(detail).getAllByRole("term").map(t => t.textContent)).toEqual(["账户", "本金", "总盈亏", "累计手续费", "当前账户总值"]);
  expect(within(detail).getByText("TqKq 模拟账户")).toBeTruthy();
  expect(within(detail).getByText("自 2026-09-14 起累计")).toBeTruthy();
  expect(within(detail).getByText("1,036,961.20")).toBeTruthy();
  expect(within(detail).getByLabelText("本金")).toHaveValue(900_000);
  fireEvent.change(within(detail).getByLabelText("本金"), { target: { value: "1200000" } });
  expect(onInitialBalanceChange).toHaveBeenCalledWith(1_200_000);
  fireEvent.click(within(detail).getByRole("button", { name: "关闭账户明细" }));
  expect(detail).not.toHaveAttribute("open");
  // The fee hint explains the default basis and hands over to settings → 手续费.
  const opened = vi.fn();
  window.addEventListener("ai-trader-open-settings", opened);
  fireEvent.click(screen.getByRole("button", { name: "手续费口径" }));
  const fee = screen.getByRole("dialog", { name: "手续费口径" });
  expect(fee).toHaveAttribute("open");
  await within(fee).findByText("默认手续费为 0，按毛收益显示；账户实际扣费以权益为准。");
  fireEvent.click(within(fee).getByRole("button", { name: "设置手续费" }));
  expect(fee).not.toHaveAttribute("open");
  expect((opened.mock.calls[0][0] as CustomEvent).detail).toBe("fees");
  expect(screen.queryByText(/逐笔手续费未提供，按毛收益显示/)).toBeNull();
});

it("shows 这笔交易 for the selected trade: source, reason, the desk view, and saves or deletes the 补充", async () => {
  const card = round("card", "SHFE.rb2610", "2026-09-14", 11, -2300, { source: "card", reason: "回踩均线企稳，建议试多", holdingMinutes: 95,
    note: null, deskView: { stance: "偏多", confidence: "中", headline: "库存去化，偏多看待", generatedAt: "2026-09-14T08:45:00+08:00", researchDay: "2026-09-14" } });
  const calls: { method: string; url: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    if (url.includes("/trade-notes/")) {
      calls.push({ method: init?.method ?? "GET", url, body: init?.body as string | undefined });
      const body = init?.method === "DELETE" ? { roundId: "card", deleted: true } : { roundId: "card", note: { text: "支撑没守住就该走", via: "typed", updatedAt: "2026-09-14T15:00:00+08:00" } };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return new Response(JSON.stringify(url.includes("/trading-costs") ? fees : { ...pnl, trades: [card, ...trades] }), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  renderPnl();
  await screen.findByRole("button", { name: "总盈亏，查看账户明细" });
  fireEvent.click(screen.getByRole("button", { name: /SHFE.rb2610 · 空 3 手.*-2,300/ }));
  const block = screen.getByRole("region", { name: "这笔交易" });
  expect(block).toHaveTextContent("1 小时 35 分");
  expect(block).toHaveTextContent("AI 建议");
  expect(block).toHaveTextContent("回踩均线企稳，建议试多");
  expect(block).not.toHaveTextContent("信心");  // 第 5 批: the desk view is its sentence only
  expect(block).toHaveTextContent("库存去化，偏多看待");
  const save = within(block).getByRole("button", { name: "保存补充" });
  expect(save).toBeDisabled();
  fireEvent.change(within(block).getByRole("textbox"), { target: { value: "  支撑没守住就该走 " } });
  fireEvent.click(save);
  await within(block).findByText("已保存");
  expect(calls[0]).toEqual({ method: "PUT", url: "http://127.0.0.1:8000/api/account/trade-notes/card", body: JSON.stringify({ text: "支撑没守住就该走", via: "typed" }) });
  await waitFor(() => expect(uiSurface("review")!.state).toMatchObject({ selectedTradeId: "card", selectedNote: "支撑没守住就该走" }));
  // Clearing the text turns the button into 删除补充.
  fireEvent.change(within(block).getByRole("textbox"), { target: { value: "" } });
  fireEvent.click(within(block).getByRole("button", { name: "删除补充" }));
  await within(block).findByText("已删除");
  expect(calls[1].method).toBe("DELETE");
  await waitFor(() => expect(uiSurface("review")!.state).toMatchObject({ selectedNote: null }));
  expect(within(block).getByText("已删除")).toBeVisible();   // the confirmation stays after the note clears
  fireEvent.click(within(block).getByRole("button", { name: "显示全图" }));
  expect(screen.queryByRole("region", { name: "这笔交易" })).toBeNull();
});

it("语音复盘: the button and disclosure voice-review start the review; without voice there is neither", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => serve(String(input instanceof Request ? input.url : input))));
  const onVoiceReview = vi.fn();
  renderPnl({ onVoiceReview });
  fireEvent.click(await screen.findByRole("button", { name: "语音复盘" }));
  expect(onVoiceReview).toHaveBeenCalledTimes(1);
  const surface = uiSurface("disclosure:voice-review")!;
  expect(surface.state).toBe(false);
  act(() => { surface.apply!(true); });
  expect(onVoiceReview).toHaveBeenCalledTimes(2);
  cleanup(); resetUiSurfaces();
  renderPnl({ onVoiceReview, voiceReviewing: true });
  expect(await screen.findByRole("button", { name: "复盘中" })).toBeDisabled();
  expect(uiSurface("disclosure:voice-review")!.state).toBe(true);
  act(() => { uiSurface("disclosure:voice-review")!.apply!(true); });
  expect(onVoiceReview).toHaveBeenCalledTimes(2);   // already reviewing: nothing new
  cleanup(); resetUiSurfaces();
  renderPnl();
  await screen.findByRole("button", { name: "总盈亏，查看账户明细" });
  expect(screen.queryByRole("button", { name: "语音复盘" })).toBeNull();
  expect(uiSurface("disclosure:voice-review")).toBeUndefined();
});

it("answers review commands through the surface registry: expand month/day, filter, select, and the two cards", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => serve(String(input instanceof Request ? input.url : input))));
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  // A command sent before the P&L loaded (the review page opened by the same command) lands after the load.
  applyUiSurface("review", { expand: { day: "2026-09-11" } });
  const { onFocus, onSelectSymbol } = renderPnl();
  expect(uiSurface("review")?.apply).toBeUndefined();
  await screen.findByRole("button", { name: "总盈亏，查看账户明细" });
  const review = () => uiSurface("review")!;
  await waitFor(() => expect(review().state).toEqual({ currency: "CNY", currencies: ["CNY"], month: "2026-09", day: "2026-09-11", filter: "", selectedTradeId: null, selectedNote: null }));
  act(() => { review().apply!({ expand: { day: "2026-09-14" } }); });
  expect(review().state).toEqual({ currency: "CNY", currencies: ["CNY"], month: "2026-09", day: "2026-09-14", filter: "", selectedTradeId: null, selectedNote: null });
  act(() => { review().apply!({ expand: { month: "2026-08" } }); });
  expect(review().state).toMatchObject({ month: "2026-08", day: "2026-08-20" });
  act(() => { review().apply!({ expand: { day: "2026-09-11" } }); });
  expect(review().state).toMatchObject({ month: "2026-09", day: "2026-09-11" });
  expect(() => review().apply!({ expand: { day: "2026-01-01" } })).toThrow("该交易日没有平仓记录");
  act(() => { review().apply!({ select: "ag" }); });
  expect(review().state).toMatchObject({ month: "2026-08", day: "2026-08-20", selectedTradeId: "ag" });
  expect(onSelectSymbol).toHaveBeenCalledWith("SHFE.ag2612");
  expect(onFocus).toHaveBeenLastCalledWith(expect.objectContaining({ id: "ag" }));
  act(() => { review().apply!({ select: null }); });
  expect(review().state).toMatchObject({ selectedTradeId: null });
  expect(onFocus).toHaveBeenLastCalledWith(undefined);
  act(() => { review().apply!({ filter: "SHFE.au2612" }); });
  expect(review().state).toMatchObject({ filter: "SHFE.au2612", month: "2026-09", day: "2026-09-11" });
  expect(() => review().apply!({ filter: "DCE.i2609" })).toThrow("该合约没有平仓记录");
  // Dialog surfaces report the native open state and open/close on command.
  act(() => { uiSurface("dialog:account-detail")!.apply!({ open: true }); });
  expect(screen.getByRole("dialog", { name: "账户明细" })).toHaveAttribute("open");
  await waitFor(() => expect(uiSurface("dialog:account-detail")!.state).toEqual({ open: true }));
  act(() => { uiSurface("dialog:account-detail")!.apply!({ open: false }); });
  await waitFor(() => expect(uiSurface("dialog:account-detail")!.state).toEqual({ open: false }));
  act(() => { uiSurface("dialog:fee-hint")!.apply!({ open: true }); });
  expect(screen.getByRole("dialog", { name: "手续费口径" })).toHaveAttribute("open");
  await waitFor(() => expect(uiSurface("dialog:fee-hint")!.state).toEqual({ open: true }));
});

const curve = [1_004_200, 1_002_400, 1_008_500, 1_011_400, 1_008_000, 1_016_800, 1_018_300, 1_015_700, 1_020_900, 1_028_300, 1_024_200, 1_028_100]
  .map((equity, index) => ({ day: `2026-09-${String(14 + index).padStart(2, "0")}`, equity, pnl: index ? equity - [1_004_200, 1_002_400, 1_008_500, 1_011_400, 1_008_000, 1_016_800, 1_018_300, 1_015_700, 1_020_900, 1_028_300, 1_024_200][index - 1] : null }));

it("本月平仓 without closes this month, the 权益曲线 over every stored day with the principal, and the night-session hint", async () => {
  const data = { ...pnl, thisMonth: 0, thisMonthTradeCount: 0, equityCurve: curve };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input instanceof Request ? input.url : input);
    return new Response(JSON.stringify(url.includes("/trading-costs") ? fees : data), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  renderPnl();
  const cards = (await screen.findByRole("button", { name: "总盈亏，查看账户明细" })).closest("dl")!;
  expect(within(cards).getAllByRole("definition").map(item => item.firstChild?.textContent)).toEqual(["+36,961", "+1,620", "—", "67% / 3"]);
  expect(within(cards).getByText("本月还没有平仓")).toBeTruthy();
  // The curve: one vertex per stored day, the principal as the dashed baseline below the lowest equity.
  const block = screen.getByRole("region", { name: "权益曲线" });
  const chart = within(block).getByRole("img");
  expect(chart.getAttribute("aria-label")).toBe("权益曲线：9 月 14 日 1,004,200 → 9 月 25 日 1,028,100，最高 1,028,300，最低 1,002,400，本金 1,000,000");
  expect(chart.querySelector(".account-equity-line")!.getAttribute("points")!.split(" ")).toHaveLength(12);
  const principal = chart.querySelector(".account-equity-principal")!;
  const lowest = Math.max(...chart.querySelector(".account-equity-line")!.getAttribute("points")!.split(" ").map(point => Number(point.split(",")[1])));
  expect(Number(principal.getAttribute("y1"))).toBeGreaterThan(lowest);
  // The readout shows the latest day until a point is picked.
  const readout = () => block.querySelector(".account-equity-readout")!.textContent;
  expect(readout()).toBe("9 月 25 日权益 1,028,100当日 +3,900");
  const plot = block.querySelector(".account-equity-plot")! as HTMLElement;
  plot.getBoundingClientRect = () => ({ left: 0, width: 1100, top: 0, height: 132, right: 1100, bottom: 132, x: 0, y: 0, toJSON: () => ({}) });
  // jsdom has no PointerEvent; a MouseEvent of the same type carries clientX to React's handler.
  fireEvent(plot, new MouseEvent("pointerdown", { bubbles: true, clientX: 100 }));
  expect(readout()).toBe("9 月 15 日权益 1,002,400当日 -1,800");
  // The closed-trade list says once where night-session fills land; month totals are labelled 平仓.
  const closed = screen.getByRole("region", { name: "平仓记录" });
  expect(within(closed).getByText("按平仓的交易日归类，夜盘成交记在下一个交易日")).toBeTruthy();
  expect(within(closed).getByRole("button", { name: /2026 年 9 月/ }).textContent).toContain("平仓+1,120");
  // The snapshot carries the four tiles and the curve summary.
  await waitFor(() => expect(uiSurface("review")!.state).toMatchObject({
    tiles: [{ label: "总盈亏", value: "+36,961", note: "权益 − 本金" }, { label: "今日", value: "+1,620", note: "账户当日净盈亏" },
      { label: "本月平仓", value: "—", note: "本月还没有平仓" }, { label: "胜率 / 笔数", value: "67% / 3", note: "近 366 天完整配对" }],
    equityCurve: { from: "2026-09-14", to: "2026-09-25", days: 12, startEquity: 1_004_200, endEquity: 1_028_100, high: 1_028_300, low: 1_002_400, principal: 1_000_000 } }));
});

it("权益曲线 needs two stored trading days", async () => {
  const data = { ...pnl, equityCurve: curve.slice(0, 1) };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input instanceof Request ? input.url : input);
    return new Response(JSON.stringify(url.includes("/trading-costs") ? fees : data), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  renderPnl();
  const block = await screen.findByRole("region", { name: "权益曲线" });
  expect(within(block).getByText("至少要两个交易日的日结才有曲线")).toBeTruthy();
  expect(within(block).queryByRole("img")).toBeNull();
  await waitFor(() => expect(uiSurface("review")!.state).toMatchObject({ equityCurve: { days: 1, from: "2026-09-14", to: "2026-09-14" } }));
});

it("未关联的补充: folded at the end with text, source and time, deletes under the old key; none shows nothing", async () => {
  const unlinkedNotes = [
    { roundId: "link:AITsplit:au-close-2", text: "拆单前的补充", via: "voice", updatedAt: "2026-10-02T06:30:00+00:00" },
    { roundId: "link:AITold:rb-close", text: "追高了", via: "typed", updatedAt: "2026-10-01T02:05:00+00:00" },
  ];
  const calls: { method: string; url: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    if (url.includes("/trade-notes/")) {
      calls.push({ method: init?.method ?? "GET", url });
      return new Response(JSON.stringify({ roundId: "x", deleted: true }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return new Response(JSON.stringify(url.includes("/trading-costs") ? fees : { ...pnl, unlinkedNotes }), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  renderPnl();
  const block = await screen.findByRole("region", { name: "未关联的补充" });
  expect(within(block).getByRole("heading").textContent).toBe("未关联的补充（2）");
  expect(within(block).getByText("对应的成交已被真实回报替换，无法确定是哪一笔")).toBeTruthy();
  await waitFor(() => expect(uiSurface("review")!.state).toMatchObject({ unlinkedNotes: 2 }));
  // Folded by default; the button and the disclosure both open it.
  expect(within(block).queryByText("拆单前的补充")).toBeNull();
  expect(uiSurface("disclosure:unlinked-notes")!.state).toBe(false);
  fireEvent.click(within(block).getByRole("button", { name: "展开未关联的补充" }));
  expect(within(block).getByText("拆单前的补充")).toBeTruthy();
  expect(within(block).getByText("语音复盘时说的 · 10/02 14:30")).toBeTruthy();
  expect(within(block).getByText("手写 · 10/01 10:05")).toBeTruthy();
  await waitFor(() => expect(uiSurface("disclosure:unlinked-notes")!.state).toBe(true));
  act(() => { uiSurface("disclosure:unlinked-notes")!.apply!(false); });
  expect(within(block).queryByText("拆单前的补充")).toBeNull();
  act(() => { uiSurface("disclosure:unlinked-notes")!.apply!(true); });
  // 删除 sends the note's original round id; the row goes and the count follows.
  fireEvent.click(within(block).getByRole("button", { name: "删除未关联的补充：拆单前的补充" }));
  await waitFor(() => expect(within(block).queryByText("拆单前的补充")).toBeNull());
  expect(calls).toEqual([{ method: "DELETE", url: "http://127.0.0.1:8000/api/account/trade-notes/link%3AAITsplit%3Aau-close-2" }]);
  expect(within(block).getByRole("heading").textContent).toBe("未关联的补充（1）");
  fireEvent.click(within(block).getByRole("button", { name: "删除未关联的补充：追高了" }));
  await waitFor(() => expect(screen.queryByRole("region", { name: "未关联的补充" })).toBeNull());
  // The disclosure unregisters in the block's unmount cleanup, a tick after the region leaves.
  await waitFor(() => expect(uiSurface("disclosure:unlinked-notes")).toBeUndefined());
});

it("未关联的补充: an empty list renders nothing and registers no disclosure", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input instanceof Request ? input.url : input);
    return new Response(JSON.stringify(url.includes("/trading-costs") ? fees : { ...pnl, unlinkedNotes: [] }), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  renderPnl();
  await screen.findByRole("region", { name: "平仓记录" });
  await waitFor(() => expect(uiSurface("review")!.state).toMatchObject({ unlinkedNotes: 0 }));
  expect(screen.queryByRole("region", { name: "未关联的补充" })).toBeNull();
  expect(screen.queryByText(/未关联/)).toBeNull();
  expect(uiSurface("disclosure:unlinked-notes")).toBeUndefined();
});

// 今日复盘 (2026-10-06): the block shows the day's 笔 with the Runtime's tags, reviewDay switches days, and
// without a review nothing shows. 交易验尸 (2026-10-25): a 笔's checks show under it, its question is answered
// into the round trip's 补充 (the trade-notes route), and 本周最该改的一件事 waits for 记住.
const leg = (id: string, clock: string, action: string, tag: string, tagLabel: string, pnl: number | null) => ({
  id, orderId: id, tradeIds: [id], symbol: "SHFE.rb2701", name: "螺纹钢", time: `2026-10-09T${clock}+08:00`, clock, side: "", volume: 1,
  price: 3150, action, source: "manual", sourceLabel: "手动", tag, tagLabel, reason: "没有记下理由", reminder: null, pnl, pnlBasis: pnl === null ? null : "gross", approximate: false });
const dailyReview = (day: string, observation: Record<string, unknown> | null) => ({
  version: 1, tradingDay: day, accountId: "A", generatedAt: `${day}T07:15:00+00:00`, reason: "auto",
  stats: { legs: 2, planned: 1, remind: 1, adhoc: 0, closes: 1, wins: 1 }, dayPnl: 2100, commission: 12,
  legs: [leg("L1", "09:03", "买开 螺纹钢 RB2701 × 1 @ 3,150", "plan", "计划内", null),
    { ...leg("L2", "10:48", "平多 螺纹钢 RB2701 × 1 @ 3,188", "remind", "提醒后", 860), roundId: "r-1",
      checks: [{ code: "early_exit", label: "提前离场", text: "止损 3,100 都还没到，就手动平了" }], question: "止损和目标都没到，当时为什么先平了？" }],
  summary: { text: "今天两笔里一笔在计划内。", source: "model", basis: "依据这 2 笔成交" }, observation, rules: ["计划内：…", "提醒后：…"] });

it("shows the 今日复盘 block, answers a 笔's question and the week's lesson, and switches days by command", async () => {
  const calls: { url: string; body?: string; method?: string }[] = [];
  const observation = { text: "止损和目标都没到时，不凭感觉先手动平仓", kind: "weekly", code: "early_exit", label: "提前离场", count: 2,
    legs: ["L2"], tradeIds: ["L2"], key: "k", revision: "r1", status: "pending" };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    calls.push({ url, body: init?.body as string | undefined, method: init?.method });
    const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
    if (url.includes("/trade-notes/r-1")) return json({ note: { text: "看到提醒就慌了", via: "typed", updatedAt: "2026-10-09T08:00:00Z" } });
    if (url.includes("/daily-review/observation")) return json({ tradingDay: "2026-10-09", observation: { ...observation, status: "remembered" }, memoryId: "m1" });
    if (url.includes("/daily-review")) {
      const day = url.includes("tradingDay=2026-10-08") ? "2026-10-08" : "2026-10-09";
      return json({ review: dailyReview(day, day === "2026-10-09" ? observation : null), days: ["2026-10-09", "2026-10-08"], generatedAt: null, generateAt: "15:15", rules: [] });
    }
    return serve(url);
  }));
  renderPnl();
  const block = await screen.findByRole("region", { name: "今日复盘" });
  expect(within(block).getByText("今日复盘 · 10-09")).toBeTruthy();
  expect(within(block).getAllByRole("definition").map(item => item.textContent)).toEqual(["+2,100", "2 笔", "1 / 2", "1 / 1"]);
  const legs = within(block).getByRole("list", { name: "今天的每一笔" });
  expect(Array.from(legs.children).map(item => item.textContent)).toEqual([
    expect.stringContaining("计划内"), expect.stringMatching(/平多 螺纹钢 RB2701.*提醒后\+860.*提前离场止损 3,100 都还没到/)]);
  fireEvent.change(within(block).getByRole("textbox", { name: "止损和目标都没到，当时为什么先平了？" }), { target: { value: "看到提醒就慌了" } });
  fireEvent.click(within(block).getByRole("button", { name: "保存回答" }));
  expect(await within(block).findByText("已记到这笔的补充")).toBeTruthy();
  const answered = calls.find(call => call.url.endsWith("/api/account/trade-notes/r-1"));
  expect([answered?.method, answered?.body]).toEqual(["PUT", JSON.stringify({ text: "看到提醒就慌了", via: "typed" })]);
  expect(within(block).getByRole("group", { name: "本周最该改的一件事" }).textContent).toContain("本周 2 笔「提前离场」");
  expect(within(block).queryByRole("button", { name: "不对" })).toBeNull();
  fireEvent.click(within(block).getByRole("button", { name: "记住" }));
  expect(await within(block).findByText(/记住了：止损和目标都没到时/)).toBeTruthy();
  expect(calls.find(call => call.url.endsWith("/api/account/daily-review/observation"))?.body).toBe(JSON.stringify({ tradingDay: "2026-10-09", verdict: "remember", observationKey: "k", revision: "r1" }));
  await waitFor(() => expect((uiSurface("review")?.state as { dailyReview?: unknown }).dailyReview).toEqual({ tradingDay: "2026-10-09", legs: 2, planned: 1, checks: 1, observation: "remembered" }));
  act(() => applyUiSurface("review", { reviewDay: "2026-10-08" }));
  expect(await screen.findByText("今日复盘 · 10-08")).toBeTruthy();
  expect(() => applyUiSurface("review", { reviewDay: "2026-10-01" })).toThrow("这个交易日没有今日复盘");
});

it("an answer to an observation regenerated meanwhile says so and shows the current one (#524)", async () => {
  // Protects: 记住 binds the version on screen; a stale one is refused and the block re-reads the new one.
  let current = { text: "你看到提醒后会先平一半", legs: ["L1", "L2"], tradeIds: ["L1", "L2"], key: "k", revision: "r1", status: "pending" };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input instanceof Request ? input.url : input);
    const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
    if (url.includes("/daily-review/observation")) {
      current = { ...current, text: "你收盘前一小时不再开新仓", key: "k2", revision: "r2" };  // another tab regenerated
      return json({ detail: { code: "observation_stale", message: "这条观察已更新，请看新的一条" } }, 409);
    }
    if (url.includes("/daily-review")) return json({ review: dailyReview("2026-10-09", current), days: ["2026-10-09"], generatedAt: null, generateAt: "15:15", rules: [] });
    return serve(url);
  }));
  renderPnl();
  const block = await screen.findByRole("region", { name: "今日复盘" });
  await within(block).findByText(/你看到提醒后会先平一半/);
  fireEvent.click(within(block).getByRole("button", { name: "记住" }));
  expect(await within(block).findByText("这条观察已更新，请看新的一条")).toBeTruthy();
  expect(await within(block).findByText(/你收盘前一小时不再开新仓/)).toBeTruthy();
  expect(within(block).queryByText(/记住了/)).toBeNull();
});

it("shows no 今日复盘 block before the first review exists", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input instanceof Request ? input.url : input);
    if (url.includes("/daily-review")) return new Response(JSON.stringify({ review: null, days: [], generatedAt: null, generateAt: "15:15", rules: [] }), { status: 200 });
    return serve(url);
  }));
  renderPnl();
  await screen.findByRole("region", { name: "账户" });
  expect(screen.queryByRole("region", { name: "今日复盘" })).toBeNull();
});

it("本站模拟合成一个账户: 人民币 / 美元 side by side, the panel follows the chosen ledger, 账户明细 shows both (2026-10-21)", async () => {
  const usdPnl = { ...pnl, initialBalance: 100_000, equity: 99_832.5, totalPnl: -167.5, today: 160, accountName: "本站模拟 · 美元",
    currency: "USD", trades: [round("mgc", "CME.MGCZ26", "2026-10-21", 11, 120, { direction: "long" })] };
  const seen: (string | null)[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    const account = new Headers(init?.headers).get("X-AI-Trader-Account");
    if (url.includes("/account/pnl")) seen.push(account);
    const body = url.includes("/trading-costs") ? fees : url.includes("/account/pnl") ? (account === "paperusd:u1" ? usdPnl : pnl) : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  renderPnl({ accountId: "paper:u1", foreignAccountId: "paperusd:u1",
    snapshots: { USD: { equity: 99_832.5, todayNetPnl: 160, available: 97_512.5, margin: 2_320 } as never } });
  expect(await screen.findByRole("button", { name: "总盈亏 · 人民币，查看账户明细" })).toHaveTextContent("+36,961");
  expect(await screen.findByRole("button", { name: "总盈亏 · 美元，查看账户明细" })).toHaveTextContent("-168");
  expect(seen).toContain("paperusd:u1");
  expect(uiSurface("review")?.state).toMatchObject({ currency: "CNY", currencies: ["CNY", "USD"] });
  expect(screen.queryByText("CME.MGCZ26 · 多 3 手")).toBeNull();
  await act(async () => { applyUiSurface("review", { currency: "USD" }); });
  expect(uiSurface("review")?.state).toMatchObject({ currency: "USD" });
  expect(await screen.findByText("权益 · 美元")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "总盈亏 · 美元，查看账户明细" }));
  const detail = screen.getByRole("dialog", { name: "账户明细" });
  expect(within(detail).getByLabelText("人民币账")).toBeTruthy();
  const usd = within(detail).getByLabelText("美元账");
  expect(within(usd).getByText("100,000")).toBeTruthy();
  expect(within(usd).getByText("97,513")).toBeTruthy();
  expect(within(usd).getByText("2,320")).toBeTruthy();
});

it("without a 外盘 USD ledger there is no 人民币 / 美元 switch and the USD command is refused", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => serve(String(input instanceof Request ? input.url : input))));
  renderPnl({ accountId: "paper:u1" });
  await screen.findByRole("button", { name: "总盈亏，查看账户明细" });
  expect(screen.queryByRole("radiogroup", { name: "复盘币种" })).toBeNull();
  expect(uiSurface("review")?.state).toMatchObject({ currency: "CNY", currencies: ["CNY"] });
  expect(() => applyUiSurface("review", { currency: "USD" })).toThrow("没有外盘美元账");
});

it("账户明细 asked for before the P&L has loaded opens once it has (顶栏权益, 2026-10-22)", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => serve(String(input instanceof Request ? input.url : input))));
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  applyUiSurface("dialog:account-detail", { open: true });  // the header asks before 复盘 has even read
  renderPnl();
  await waitFor(() => expect(screen.getByRole("dialog", { name: "账户明细" })).toHaveAttribute("open"));
});


it("USD journal and unlinked-note writes name their ledger; deleted:false keeps the note", async () => {
  setActiveTradingAccount("paper:u1");
  const note = { text: "美元补充", via: "typed", updatedAt: "2026-10-23T06:00:00Z" };
  const writes: { method?: string; account: string | null }[] = [];
  let deleted = false;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input), account = new Headers(init?.headers).get("X-AI-Trader-Account");
    if (url.includes("/trade-notes/")) {
      writes.push({ method: init?.method, account });
      return new Response(JSON.stringify(init?.method === "PUT" ? { note } : { deleted }), { status: 200 });
    }
    return new Response(JSON.stringify(url.includes("/account/pnl") ? { ...pnl, trades: [{ ...trades[0], note }],
      unlinkedNotes: [{ ...note, roundId: "orphan" }] } : url.includes("/trading-costs") ? fees : {}), { status: 200 });
  }));
  renderPnl({ accountId: "paper:u1", foreignAccountId: "paperusd:u1" });
  await screen.findByRole("button", { name: "总盈亏 · 人民币，查看账户明细" });
  fireEvent.click(screen.getByRole("radio", { name: "美元" }));
  await waitFor(() => expect(uiSurface("review")?.state).toMatchObject({ currency: "USD" }));
  await act(async () => { applyUiSurface("review", { select: "rb" }); });
  const journal = await screen.findByRole("region", { name: "这笔交易" });
  fireEvent.change(within(journal).getByRole("textbox"), { target: { value: "新美元补充" } });
  fireEvent.click(within(journal).getByRole("button", { name: "保存补充" }));
  await within(journal).findByText("已保存");
  fireEvent.change(within(journal).getByRole("textbox"), { target: { value: "" } });
  fireEvent.click(within(journal).getByRole("button", { name: "删除补充" }));
  await within(journal).findByText("没有保存成功，请重试");
  expect(uiSurface("review")?.state).toMatchObject({ selectedNote: "美元补充" });
  const unlinked = screen.getByRole("region", { name: "未关联的补充" });
  fireEvent.click(within(unlinked).getByRole("button", { name: "展开未关联的补充" }));
  fireEvent.click(within(unlinked).getByRole("button", { name: "删除未关联的补充：美元补充" }));
  await within(unlinked).findByText("没有删除成功，请重试");
  expect(within(unlinked).getByText("美元补充")).toBeVisible();
  deleted = true;
  fireEvent.click(within(unlinked).getByRole("button", { name: "删除未关联的补充：美元补充" }));
  await waitFor(() => expect(screen.queryByRole("region", { name: "未关联的补充" })).toBeNull());
  expect(writes).toEqual(["PUT", "DELETE", "DELETE", "DELETE"].map(method => ({ method, account: "paperusd:u1" })));
});

it("a late USD note response cannot alter the CNY trade with the same id", async () => {
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/trade-notes/")) return new Promise<Response>(resolve => { finish = resolve; });
    return new Response(JSON.stringify(url.includes("/account/pnl") ? { ...pnl, trades: [trades[0]] }
      : url.includes("/trading-costs") ? fees : {}), { status: 200 });
  }));
  renderPnl({ accountId: "paper:u1", foreignAccountId: "paperusd:u1" });
  await screen.findByRole("button", { name: "总盈亏 · 人民币，查看账户明细" });
  fireEvent.click(screen.getByRole("radio", { name: "美元" }));
  await act(async () => { applyUiSurface("review", { select: "rb" }); });
  let journal = await screen.findByRole("region", { name: "这笔交易" });
  fireEvent.change(within(journal).getByRole("textbox"), { target: { value: "USD late" } });
  fireEvent.click(within(journal).getByRole("button", { name: "保存补充" }));
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  fireEvent.click(screen.getByRole("radio", { name: "人民币" }));
  await act(async () => { applyUiSurface("review", { select: "rb" }); });
  journal = await screen.findByRole("region", { name: "这笔交易" });
  await act(async () => { finish(new Response(JSON.stringify({ note: { text: "USD late", via: "typed", updatedAt: "now" } }), { status: 200 })); });
  expect(within(journal).getByRole("textbox")).toHaveValue("");
  expect(within(journal).queryByText("已保存")).toBeNull();
  expect(uiSurface("review")?.state).toMatchObject({ currency: "CNY", selectedNote: null });
});


it("keeps history chart focus on its mapped chart, but clears it across account ABA", () => {
  rememberStrategyScopes([{ symbol: "KQD.m@COMEX.GC", strategyScope: "CME.GCE" }]);
  const { result, rerender } = renderHook(({ scope, symbol }) => useChartFocus(scope, "runtime", symbol, 300),
    { initialProps: { scope: "A", symbol: "SHFE.au2612" } });
  const selected = { symbol: "CME.GCEZ26", openTime: 100, closeTime: 200, direction: "long" };
  act(() => result.current.setTradeFocus(selected));
  rerender({ scope: "A", symbol: "KQD.m@COMEX.GC" });
  expect(result.current.tradeFocus).toEqual(selected);
  rerender({ scope: "B", symbol: "KQD.m@COMEX.GC" });
  expect(result.current.tradeFocus).toBeUndefined();
  rerender({ scope: "A", symbol: "KQD.m@COMEX.GC" });
  expect(result.current.tradeFocus).toBeUndefined();
});

it("习惯对照: 实际 vs 只做计划内, the gap by tag, the holding estimates apart, and a pick selects its trade", async () => {
  const habits = { status: "ok", basis: "gross", days: 90, currency: "CNY", rules: ["只做计划内：只留开仓标为计划内的单"],
    actual: { count: 9, pnl: -720 }, planned: { count: 6, pnl: 1420 }, gap: 2140,
    split: [{ tag: "remind", label: "提醒后", count: 1, pnl: -960 }, { tag: "adhoc", label: "临时", count: 2, pnl: -1180 }],
    holding: { lateLosers: { count: 1, extra: 746.67, limitMinutes: 40 }, earlyWinners: null },
    picks: [{ id: "au", tradingDay: "2026-09-11", symbol: "SHFE.au2612", direction: "long", volume: 1, tag: "adhoc", pnl: -500,
      reason: "计划外亏损" }] };
  let answer: unknown = habits;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input instanceof Request ? input.url : input);
    return url.includes("/api/account/habits") ? new Response(JSON.stringify(answer), { status: 200, headers: { "Content-Type": "application/json" } })
      : serve(url);
  }));
  const { onFocus } = renderPnl();
  const block = await screen.findByTestId("habit-compare");
  expect(block).toHaveTextContent("近 90 天 · 毛收益 · 9 笔平仓");
  expect(block).toHaveTextContent("你实际-720");
  expect(block).toHaveTextContent("只做计划内+1,4206 笔 · 相差 2,140");
  expect(block.querySelector('dl[aria-label="计划外的单"]')).toHaveTextContent("提醒后 1 笔-960临时 2 笔-1,180");
  expect(block.querySelector('dl[aria-label="拿单习惯"]')).toHaveTextContent("亏单拿太久 1 笔多亏约 747");
  expect(within(block).queryByText(/赚单拿太短/)).toBeNull();
  await waitFor(() => expect((uiSurface("review")!.state as { habits?: unknown }).habits).toEqual(
    { status: "ok", actual: { count: 9, pnl: -720 }, planned: { count: 6, pnl: 1420 }, gap: 2140, picks: ["au"] }));
  fireEvent.click(within(block).getByRole("button", { name: /au2612 多 1 手/ }));
  expect(onFocus).toHaveBeenLastCalledWith(expect.objectContaining({ id: "au" }));
  expect((uiSurface("review")!.state as { selectedTradeId: string }).selectedTradeId).toBe("au");
  act(() => { uiSurface("disclosure:habits-how")!.apply!(true); });
  expect(within(block).getByText("只做计划内：只留开仓标为计划内的单")).toBeVisible();
  cleanup(); resetUiSurfaces();
  answer = { status: "insufficient", planned: 2, needed: 5, basis: "net", days: 90, rules: [] };
  renderPnl();
  expect(await screen.findByTestId("habit-compare")).toHaveTextContent("计划内的单还不到 5 笔（现在 2 笔），攒够了再对照");
  expect(uiSurface("disclosure:habits-how")).toBeUndefined();  // no fold without a comparison
});

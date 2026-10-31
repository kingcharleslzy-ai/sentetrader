import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import { FillRows, OrderRows, PositionRows } from "./PositionPanels";
import { resetUiSurfaces } from "../../uiSurfaces";

beforeEach(() => resetUiSurfaces());
import type { RoundTrip, TradeReviewData } from "../../hooks/useTradeReview";

const at = (iso: string) => Date.parse(iso) * 1e6;
const round = (overrides: Partial<RoundTrip>): RoundTrip => ({ id: Math.random().toString(36), symbol: "SHFE.rb2701", direction: "long", volume: 1,
  openTime: at("2026-09-24T01:00:00Z"), closeTime: at("2026-09-24T01:41:10Z"), openPrice: 3405, closePrice: 3407, gross: 20, fees: null, net: null,
  tradingDay: "2026-09-24", source: "manual", ...overrides });
const review = (trades: RoundTrip[]): TradeReviewData => ({ trades, account: { equity: 0, todayNetPnl: 0, commission: 0, positionCount: 0 }, costSource: "account", unmatched: [] });
const instruments = { items: [{ symbol: "SHFE.rb2610", instrumentName: "螺纹2610", instrumentId: "rb2610", productId: "rb", priceTick: 1 }] } as any;

test("成交按交易日分组（日期·星期·当日平仓盈亏），中文合约名，标来源，同一分钟同价同向合并成一行", () => {
  const trades = [
    // Seven identical fills in the same minute (the production screenshot): one line, volumes and P&L added.
    ...Array.from({ length: 7 }, (_, index) => round({ id: `ai-${index}`, direction: "short", openPrice: 3404, closePrice: 3399, gross: 50,
      closeTime: at(`2026-09-24T01:27:${String(10 + index).padStart(2, "0")}Z`), source: "ai" })),
    round({ id: "manual" }),
    round({ id: "strategy", tradingDay: "2026-09-23", closeTime: at("2026-09-23T02:31:00Z"), volume: 3, openPrice: 3151, closePrice: 3151, gross: 0, source: "strategy", strategyName: "双均线" }),
    round({ id: "stop", tradingDay: "2026-09-23", closeTime: at("2026-09-23T02:06:00Z"), direction: "short", volume: 2, openPrice: 3149, closePrice: 3152, gross: -60, source: "stop" }),
  ];
  render(<FillRows review={review(trades)} instruments={instruments} limit={100} grouped />);
  const days = screen.getAllByRole("heading", { level: 4 });
  expect(days.map(day => day.textContent)).toEqual(["9月24日 周四平仓盈亏（毛） +370", "9月23日 周三平仓盈亏（毛） -60"]);
  const first = days[0].closest("section")!;
  const lines = within(first).getAllByRole("listitem");
  expect(lines).toHaveLength(2);
  expect(lines[0]).toHaveTextContent("09:41螺纹2701平多 1 手3,405 → 3,407手动毛 +20");
  expect(lines[1]).toHaveTextContent("09:27螺纹2701平空 7 手3,404 → 3,399AI 自主毛 +350");
  // The Chinese name comes from another listed contract of the product; the raw code rides in the title.
  expect(within(lines[0]).getByTitle("SHFE.rb2701")).toHaveTextContent("螺纹2701");
  const second = within(days[1].closest("section")!).getAllByRole("listitem");
  expect(second[0]).toHaveTextContent("双均线");
  expect(second[1]).toHaveTextContent("止损");
});

test("来源筛选只留该来源；底栏不分组、带日期", () => {
  const trades = [round({ id: "manual" }), round({ id: "stop", closeTime: at("2026-09-24T02:00:00Z"), source: "stop", gross: -60 })];
  const view = render(<FillRows review={review(trades)} instruments={instruments} limit={100} grouped source="stop" />);
  expect(screen.getAllByRole("listitem")).toHaveLength(1);
  expect(screen.getByRole("listitem")).toHaveTextContent("止损");
  view.rerender(<FillRows review={review(trades)} instruments={instruments} limit={100} grouped source="ai" />);
  expect(screen.getByText("近 30 天没有来源为「AI 自主」的成交")).toBeVisible();
  // 本站模拟盘强平 (#1204): its own chip and label.
  view.rerender(<FillRows review={review([...trades, round({ id: "forced", source: "liquidation", gross: -900 })])} instruments={instruments} limit={100} grouped source="liquidation" />);
  expect(screen.getByRole("listitem")).toHaveTextContent("强平");
  view.rerender(<FillRows review={review(trades)} instruments={instruments} limit={3} grouped={false} />);
  expect(screen.queryByRole("heading")).toBeNull();
  expect(screen.getAllByRole("listitem")[0]).toHaveTextContent("09/24 10:00");
});

test("策略来源只显示策略名：行首已有合约，旧 Runtime 给的“名称 · 合约代码”去掉代码", () => {
  render(<FillRows review={review([round({ id: "old", source: "strategy", strategyName: "1分钟双指标确认策略 · SHFE.rb2610" })])} instruments={instruments} limit={100} grouped />);
  const line = screen.getByRole("listitem");
  expect(line).toHaveTextContent("1分钟双指标确认策略");
  expect(line).not.toHaveTextContent("SHFE.rb2610");
});

test("持仓面板每行的 AI ▾：持仓问风险 / 平仓建议，委托问为什么没成交 / 追单建议，都带上这一行", () => {
  const onAskAi = vi.fn();
  const position = { symbol: "SHFE.rb2610", direction: "long", volume: 2, todayVolume: 2, historicalVolume: 0, frozenToday: 0, frozenHistorical: 0, unrealizedPnl: 30 } as any;
  render(<PositionRows positions={[position]} instruments={instruments} busy={false} limit={10} onClosePosition={vi.fn()} onAskAi={onAskAi} />);
  fireEvent.click(screen.getByRole("button", { name: "AI：螺纹2610 多 2 手" }));
  expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual(["这笔持仓的风险", "平仓建议（出卡）"]);
  fireEvent.click(screen.getByRole("menuitem", { name: "平仓建议（出卡）" }));
  expect(onAskAi).toHaveBeenCalledWith("给这笔持仓一个平仓建议，要平就出确认卡", expect.objectContaining({ kind: "position", label: "螺纹2610 多 2 手" }));
  const order = { clientOrderId: "order-1", request: { symbol: "SHFE.rb2610", direction: "buy", offset: "OPEN", volume: 1, limitPrice: 3400 }, remoteStatus: "ALIVE", cancelPhase: "NONE", submitPhase: "SUBMITTED", displayStatus: "已受理" } as any;
  render(<OrderRows orders={[order]} history={false} busy={false} limit={10} onOrderAction={vi.fn()} onAbandon={vi.fn()} onAskAi={onAskAi} />);
  fireEvent.click(screen.getByRole("button", { name: /^AI：SHFE.rb2610/ }));
  fireEvent.click(screen.getByRole("menuitem", { name: "为什么没成交" }));
  expect(onAskAi).toHaveBeenLastCalledWith("这笔委托为什么没成交", expect.objectContaining({ kind: "order", id: "order-1" }));
});

test("没有 onAskAi（底栏）时不显示 AI 菜单", () => {
  const position = { symbol: "SHFE.rb2610", direction: "long", volume: 1, todayVolume: 1, historicalVolume: 0, frozenToday: 0, frozenHistorical: 0, unrealizedPnl: 0 } as any;
  render(<PositionRows positions={[position]} instruments={instruments} busy={false} limit={10} onClosePosition={vi.fn()} />);
  expect(screen.queryByRole("button", { name: /^AI：/ })).toBeNull();
});

test("本站模拟合成一个账户: the 外盘 USD ledger's fills follow under 外盘 · 美元, never added to the RMB day (2026-10-21)", () => {
  const usd = review([round({ id: "mgc", symbol: "CME.MGCZ26", openPrice: 4143.7, closePrice: 4160.1, gross: 164, tradingDay: "2026-10-21",
    closeTime: at("2026-10-21T08:00:00Z") })]);
  render(<FillRows review={review([round({ id: "rb" })])} foreignReview={usd} instruments={instruments} limit={100} grouped />);
  expect(screen.getAllByRole("heading", { level: 4 }).map(day => day.textContent)).toEqual(["9月24日 周四平仓盈亏（毛） +20"]);
  expect(screen.getByTestId("foreign-fills-head")).toHaveTextContent("外盘 · 美元平仓盈亏 +164 美元");
  cleanup();
  render(<FillRows review={review([])} foreignReview={usd} instruments={instruments} limit={3} grouped={false} />);
  expect(screen.getByTestId("foreign-fills-head")).toBeTruthy();
  expect(screen.queryByText(/近 30 天没有平仓成交/)).toBeNull();
});

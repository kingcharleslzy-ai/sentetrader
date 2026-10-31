import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import { MarketCommand } from "./MarketCommand";
import type { MarketWorkbenchSnapshot } from "../../api";

const bars = Array.from({ length: 3 }, (_, i) => ({ datetime: i * 60_000_000_000, open: 1, high: 1, low: 1, close: 1, volume: 1 }));
const market = { durationSeconds: 300, quote: { symbol: "SHFE.au2612" }, bars } as unknown as MarketWorkbenchSnapshot;

/** The period row with its callbacks; `chartView` and `workspace.duration` are the two active-state inputs. */
function renderRow(chartView: "kline" | "timeshare", duration: number) {
  const onSelectDuration = vi.fn(), onSelectChartView = vi.fn();
  const workspace = { duration, runtimeBaseUrl: "http://runtime", replayActive: false, marketLoading: false, onRefreshMarket: vi.fn() } as never;
  const snapshot = { ...market, ageSeconds: 0, quote: { symbol: "SHFE.au2612", name: "沪金", changePercent: 0, lastPrice: 1 } } as unknown as MarketWorkbenchSnapshot;
  render(<MarketCommand workspace={workspace} market={snapshot} chartView={chartView} decimals={2}
    onOpenContracts={vi.fn()} onSelectDuration={onSelectDuration} onSelectChartView={onSelectChartView} />);
  return { group: screen.getByRole("group", { name: "K线周期" }), onSelectDuration, onSelectChartView };
}

test("周期行：行首分时，1 分到周线，顺序与完整周期集固定（2026-10-02 加 30 分与周线）", () => {
  const { group } = renderRow("kline", 300);
  expect(within(group).getAllByRole("button").map(button => button.textContent))
    .toEqual(["分时", "1分", "3分", "5分", "15分", "30分", "1小时", "日线", "周线"]);
});

test("分时按钮：active 看 chartView 不看 duration，点击走 onSelectChartView 并带 timeshare", () => {
  const { group, onSelectChartView } = renderRow("timeshare", 300);
  expect(within(group).getByRole("button", { name: "分时" })).toHaveClass("active");
  expect(within(group).getByRole("button", { name: "5分" })).not.toHaveClass("active"); // duration 300 不点亮
  fireEvent.click(within(group).getByRole("button", { name: "分时" }));
  expect(onSelectChartView).toHaveBeenCalledWith("timeshare");
});

test("周期按钮：active 看 chartView=kline 且 duration 相等，30 分与周线可点并回 onSelectDuration", () => {
  const { group, onSelectDuration } = renderRow("kline", 1800);
  expect(within(group).getByRole("button", { name: "30分" })).toHaveClass("active");
  expect(within(group).getByRole("button", { name: "分时" })).not.toHaveClass("active");
  fireEvent.click(within(group).getByRole("button", { name: "周线" }));
  expect(onSelectDuration).toHaveBeenCalledWith(604800);
  fireEvent.click(within(group).getByRole("button", { name: "分时" }));
  expect(onSelectDuration).toHaveBeenCalledTimes(1); // 分时不走周期回调
});

test("合约名拆成品种与月份：窄栏只省略品种部分、月份保留；按钮标签仍是完整的“名称 代码”", () => {
  const workspace = { duration: 300, runtimeBaseUrl: "http://runtime", replayActive: false, marketLoading: false, onRefreshMarket: vi.fn() } as never;
  const snapshot = { ...market, ageSeconds: 0, quote: { symbol: "CFFEX.IF2609", name: "沪深300股指2609", changePercent: 0, lastPrice: 1 } } as unknown as MarketWorkbenchSnapshot;
  const onOpenContracts = vi.fn();
  const view = render(<MarketCommand workspace={workspace} market={snapshot} chartView="kline" decimals={1}
    onOpenContracts={onOpenContracts} onSelectDuration={vi.fn()} onSelectChartView={vi.fn()} />);
  const button = screen.getByRole("button", { name: "沪深300股指2609 CFFEX.IF2609" });
  // MarketCommand.css ellipsizes the first part only (the 800 px browser check lives in the PR shots).
  expect([...button.querySelectorAll("strong > span")].map(part => part.textContent)).toEqual(["沪深300股指", "2609"]);
  fireEvent.click(button);
  expect(onOpenContracts).toHaveBeenCalledOnce();
  // A name without a month (a continuous contract) stays one piece.
  view.rerender(<MarketCommand workspace={workspace} market={{ ...snapshot, quote: { ...snapshot.quote, name: "螺纹主连", symbol: "KQ.m@SHFE.rb" } } as never} chartView="kline" decimals={1}
    onOpenContracts={onOpenContracts} onSelectDuration={vi.fn()} onSelectChartView={vi.fn()} />);
  expect(screen.getByRole("button", { name: "螺纹主连 KQ.m@SHFE.rb" }).querySelector("strong")).toHaveTextContent("螺纹主连");
  expect(screen.getByRole("button", { name: "螺纹主连 KQ.m@SHFE.rb" }).querySelectorAll("strong > span")).toHaveLength(0);
});

test("回放页头（#932 X3）：涨跌幅与开高低量标“回放不显示”，不显示 0.00% 或空值；持仓照常；非回放不变", () => {
  const quote = { symbol: "CFFEX.IC2612", name: "中证2612", lastPrice: 6950, changePercent: 0, open: null, highest: null, lowest: null, volume: 0, openInterest: 181260 };
  const snapshot = { ...market, ageSeconds: 0, quote } as unknown as MarketWorkbenchSnapshot;
  const props = { chartView: "kline" as const, decimals: 1, onOpenContracts: vi.fn(), onSelectDuration: vi.fn(), onSelectChartView: vi.fn() };
  const replayWorkspace = { duration: 300, runtimeBaseUrl: "http://runtime", replayActive: true, marketLoading: false, onRefreshMarket: vi.fn() } as never;
  const view = render(<MarketCommand workspace={replayWorkspace} market={snapshot} {...props} />);
  const head = view.container.querySelector(".fdw-market-head") as HTMLElement;
  expect(screen.getByTestId("quote-replay-change")).toHaveTextContent("涨跌幅 回放不显示");
  expect(screen.getByTestId("quote-replay-stats")).toHaveTextContent("开高低 · 量回放不显示");
  expect(head).not.toHaveTextContent("0.00%");
  expect(head).not.toHaveTextContent("—");
  expect(within(head).queryByText("开")).toBeNull();
  expect(within(head).getByText("持仓").nextSibling).toHaveTextContent("181,260");

  const liveWorkspace = { duration: 300, runtimeBaseUrl: "http://runtime", replayActive: false, marketLoading: false, onRefreshMarket: vi.fn() } as never;
  view.rerender(<MarketCommand workspace={liveWorkspace} market={{ ...snapshot, quote: { ...quote, changePercent: 1.234, open: 6900, highest: 6990, lowest: 6880, volume: 1200 } } as never} {...props} />);
  expect(screen.queryByTestId("quote-replay-change")).toBeNull();
  expect(screen.queryByTestId("quote-replay-stats")).toBeNull();
  expect(head).toHaveTextContent("+1.23%");
  expect(within(head).getByText("开").nextSibling).toHaveTextContent("6,900.0");
  expect(within(head).getByText("量").nextSibling).toHaveTextContent("1,200");
});

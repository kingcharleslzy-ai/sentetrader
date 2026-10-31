import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { AnswerCard as Answer, DataCard as Data, MarketSnapshot } from "@ai-trader/ui-events";
import { AnswerCard, DataCard } from "./AnswerCard";
import { MarketCard } from "./MarketCard";
import { signedPercent } from "./cardActions";

const answer: Answer = {
  turnId: "turn-a", cardId: "answer-turn-a", title: "沪金咋样", conclusion: "沪金短线偏强，回踩均价附近再看。",
  figures: [
    { label: "最新价", value: "612.34" },
    { label: "涨跌", value: "+1.20", unit: "%", dir: "up" },
    { label: "5 日", value: "-0.80", unit: "%", dir: "down" },
  ],
  compare: { left: { title: "沪金", lines: ["涨 1.2%"] }, right: { title: "沪银", lines: ["跌 0.3%"] } },
  reasons: ["站上 20 日均线", "量比 1.4", "美元走弱", "第四条不显示"],
  risk: "夜盘美国数据公布前波动可能放大。",
  symbol: "SHFE.au2612",
  basis: { at: "2026-10-14T02:05:00Z", sources: ["行情 SHFE.au2612 10:05", "K 线 日线 60 根"] },
  actions: [
    { kind: "ui", label: "看图表", changes: { symbol: "SHFE.au2612", view: "trade" } },
    { kind: "ask", label: "问 AI 怎么办", prompt: "沪金现在怎么办" },
  ],
};

test("回答卡: title with its ?, conclusion, the cells the columns do not show, compare, ≤3 reasons and the risk line in order", () => {
  render(<AnswerCard card={answer} id="answer-turn-a" handlers={{}} />);
  const card = screen.getByTestId("answer-card");
  expect(card).toHaveAttribute("aria-label", "沪金咋样");
  expect(within(card).getByRole("button", { name: "沪金咋样的说明" })).toHaveAttribute("aria-expanded", "false");
  const body = card.querySelector(".card-body")!;
  // 去重 is the Runtime's (a cell the columns show is dropped there); the cells it kept show beside the columns (owner 10-30).
  expect([...body.children].map(child => child.className.split(" ").find(name => name.startsWith("ac-"))))
    .toEqual(["ac-conclusion", "ac-figures", "ac-compare", "ac-reasons", "ac-risk"]);
  expect(within(card).getByLabelText("关键数字")).toHaveTextContent("612.34");
  expect(within(card).getAllByRole("listitem").filter(item => item.closest(".ac-reasons"))).toHaveLength(3);
  expect(card).not.toHaveTextContent("第四条不显示");
  expect(screen.getByTestId("answer-risk")).toHaveTextContent("夜盘美国数据");
  expect(within(card).getByLabelText("沪金")).toHaveTextContent("涨 1.2%");
  expect(within(card).getByLabelText("沪银")).toHaveTextContent("跌 0.3%");
});

test("回答卡 figures: market direction on the cell, unit after the value, one wrapping grid", () => {
  render(<AnswerCard card={{ ...answer, compare: null }} handlers={{}} />);
  const grid = screen.getByLabelText("关键数字");
  expect(grid).toHaveClass("stat-tiles");
  expect(grid.parentElement).toHaveClass("ac-figures");
  const values = [...grid.querySelectorAll(".ac-value")];
  expect(values).toHaveLength(3);
  expect(values[0]).not.toHaveAttribute("data-dir");
  expect(values[1]).toHaveAttribute("data-dir", "up");
  expect(values[2]).toHaveAttribute("data-dir", "down");
  expect(values[1]).toHaveTextContent("+1.20");
  expect(values[1].querySelector("small")).toHaveTextContent("%");
});

test("回答卡 依据: folded by default, lists every source and the read time", () => {
  render(<AnswerCard card={answer} id="answer-turn-a" handlers={{}} />);
  const fold = screen.getByTestId("answer-card-details");
  expect(fold).not.toHaveAttribute("open");
  expect(within(fold).getByText("依据").tagName).toBe("SUMMARY");
  expect(within(fold).getByText("行情 SHFE.au2612 10:05")).toBeInTheDocument();
  expect(within(fold).getByText("K 线 日线 60 根")).toBeInTheDocument();
  expect(fold).toHaveTextContent("读取时间");
  expect(fold).toHaveTextContent("2026");
  expect(within(fold).queryByTestId("answer-folded")).toBeNull();
});

test("回答卡 依据 holds the folded read cards (原卡) under the read time and sources", () => {
  render(<AnswerCard card={answer} id="answer-turn-a" handlers={{}} folded={[<p key="m">行情卡内容</p>]} />);
  const fold = screen.getByTestId("answer-card-details");
  const folded = within(fold).getByRole("group", { name: "原卡" });
  expect(folded).toHaveTextContent("行情卡内容");
  expect(fold.querySelector(".ac-basis")!.compareDocumentPosition(folded) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("回答卡 buttons: secondary only; ask sends its prompt, ui applies its command; disabled without handlers", () => {
  const { rerender } = render(<AnswerCard card={answer} handlers={{}} />);
  expect(screen.getByRole("button", { name: "看图表" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "问 AI 怎么办" })).toBeDisabled();
  const askAi = vi.fn();
  const applyUi = vi.fn();
  rerender(<AnswerCard card={answer} handlers={{ askAi, applyUi }} />);
  const chart = screen.getByRole("button", { name: "看图表" });
  const ask = screen.getByRole("button", { name: "问 AI 怎么办" });
  for (const button of [chart, ask]) expect(button).not.toHaveClass("primary-button");
  fireEvent.click(chart);
  expect(applyUi).toHaveBeenCalledWith({ symbol: "SHFE.au2612", view: "trade" });
  fireEvent.click(ask);
  expect(askAi).toHaveBeenCalledWith("沪金现在怎么办");
});

test("回答卡 with only a conclusion renders no empty blocks", () => {
  render(<AnswerCard card={{ ...answer, figures: [], compare: null, reasons: [], risk: null, actions: [] }} handlers={{}} />);
  const card = screen.getByTestId("answer-card");
  expect(card.querySelector(".card-body")!.children).toHaveLength(1);
  expect(card.querySelector(".card-actions")).toBeNull();
});

const dataCard = (kind: Data["kind"], extra: Partial<Data> = {}): Data => ({
  turnId: "turn-d", cardId: `data-${kind}-turn-d`, kind, title: `${kind} 标题`,
  basis: { at: "2026-10-14T02:05:00Z", sources: ["工具读取"] }, ...extra,
});

test.each([
  ["positions", "持仓"], ["orders", "止损止盈"], ["news", "快讯"], ["analysis", "K 线"], ["quotes", "报价总表"],
] as const)("数据卡 %s: its own icon box, ? text and data-kind", async (kind, helpWord) => {
  render(<DataCard card={dataCard(kind, { rows: [{ primary: "一行" }] })} id={`data-${kind}`} handlers={{}} />);
  const card = screen.getByTestId("data-card");
  expect(card).toHaveAttribute("data-kind", kind);
  const help = within(card).getByRole("button", { name: `${kind} 标题的说明` });
  fireEvent.click(help);
  expect(await screen.findByTestId(`help-tip-card-data:data-${kind}-turn-d`)).toHaveTextContent(helpWord);
  expect(within(screen.getByTestId("data-card-details")).getByText("依据")).toBeInTheDocument();
});

test("数据卡 rows: primary, secondary, coloured value and tag; note under the list", () => {
  render(<DataCard card={dataCard("positions", {
    figures: [{ label: "浮动盈亏", value: "+1,200", dir: "up" }],
    rows: [
      { primary: "沪金2612 多 2 手", secondary: "均价 605.10", value: "+1,460", dir: "up", tag: "多" },
      { primary: "螺纹2610 空 1 手", secondary: "均价 3200", value: "-260", dir: "down" },
    ],
    note: "另有 1 个持仓未列出",
    actions: [{ kind: "ui", label: "打开持仓", changes: { panel: "positions" } }],
  })} handlers={{ applyUi: vi.fn() }} />);
  const rows = screen.getByRole("list", { name: "持仓" });
  const items = within(rows).getAllByRole("listitem");
  expect(items).toHaveLength(2);
  expect(items[0]).toHaveTextContent("沪金2612 多 2 手");
  expect(items[0]).toHaveTextContent("均价 605.10");
  expect(items[0].querySelector(".dc-value")).toHaveAttribute("data-dir", "up");
  expect(items[0].querySelector(".dc-tag")).toHaveTextContent("多");
  expect(items[1].querySelector(".dc-value")).toHaveAttribute("data-dir", "down");
  expect(screen.getByText("另有 1 个持仓未列出")).toHaveClass("dc-note");
  expect(screen.getByLabelText("持仓数字")).toHaveClass("stat-tiles");
  expect(screen.getByRole("button", { name: "打开持仓" })).toBeEnabled();
});

test("数据卡 with no rows shows its empty line; no actions renders no action row", () => {
  render(<DataCard card={dataCard("orders", { rows: [], empty: "没有挂着的委托" })} handlers={{}} />);
  expect(screen.getByText("没有挂着的委托")).toHaveClass("dc-empty");
  expect(screen.queryByRole("list", { name: "挂单与保护" })).toBeNull();
  expect(screen.getByTestId("data-card").querySelector(".card-actions")).toBeNull();
});

test("行情卡 peers: one line per other contract with its direction colour", () => {
  const market: MarketSnapshot = {
    turnId: "turn-m", symbol: "SHFE.au2612", name: "沪金", price: 612.3, changePercent: 1.2, updatedAt: "10:05",
    trend: "up", summary: "", priceCurve: [1, 2], cardId: "market-turn-m",
    peers: [{ symbol: "SHFE.ag2612", name: "沪银", price: 7800, changePercent: -0.3 }, { symbol: "INE.sc2612", name: "原油", price: 520, changePercent: 0 }],
  };
  render(<MarketCard market={market} />);
  const peers = within(screen.getByTestId("market-peers")).getAllByRole("listitem");
  expect(peers).toHaveLength(2);
  expect(peers[0]).toHaveTextContent("沪银SHFE.ag26127800.00-0.30%");
  expect(peers[0].querySelector(".mc-peer-change")).toHaveAttribute("data-dir", "down");
  expect(peers[1].querySelector(".mc-peer-change")).not.toHaveAttribute("data-dir");
  render(<MarketCard market={{ ...market, peers: [] }} />);
  expect(screen.getAllByTestId("market-card")).toHaveLength(2);
  expect(screen.getAllByTestId("market-peers")).toHaveLength(1);
});

test("数据卡 with a stale read shows the warning on the card face", () => {
  render(<DataCard card={dataCard("positions", { rows: [{ primary: "一行" }], warning: "数据陈旧" })} handlers={{}} />);
  expect(screen.getByTestId("data-card").querySelector(".card-badge")).toHaveTextContent("数据陈旧");
  expect(screen.getByTestId("data-card").querySelector(".card-badge")).toHaveClass("warning");
});

test("行情卡涨跌幅统一保留两位小数（同一格式化函数）", () => {
  expect(signedPercent(0.8926578888840929)).toBe("+0.89%");
  expect(signedPercent(-0.22690437601296598)).toBe("-0.23%");
  expect(signedPercent(-0.001)).toBe("0.00%");
  expect(signedPercent(Number.NaN)).toBe("—");
  render(<MarketCard market={{ turnId: "t", symbol: "SHFE.au2612", name: "沪金", price: 823.5, changePercent: 0.8926578888840929,
    updatedAt: "刚刚", trend: "up", summary: "", priceCurve: [1, 2],
    peers: [{ symbol: "SHFE.ag2612", name: "沪银", price: 7500, changePercent: -0.22690437601296598 }] }} />);
  const card = screen.getByTestId("market-card");
  expect(card).toHaveTextContent("+0.89%");
  expect(card).toHaveTextContent("-0.23%");
  expect(card).not.toHaveTextContent("0.8926");
  expect(card.querySelector(".market-change")).toHaveAttribute("data-dir", "up");
});

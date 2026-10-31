import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { ExecutionCard as Execution, OrderQuestionCard as OrderQuestion, ReminderCard as Reminder, ResearchJudgmentCard as Judgment } from "@ai-trader/ui-events";
import { ExecutionCard } from "./ExecutionCard";
import { OrderQuestionCard } from "./OrderQuestionCard";
import { ReminderCard } from "./ReminderCard";
import { ResearchJudgmentCard } from "./ResearchJudgmentCard";
import { BriefCard } from "./BriefCard";
import { pickCardUiChanges } from "./cardActions";

const question: OrderQuestion = {
  turnId: "turn-q", cardId: "question-turn-q", kind: "ask", title: "需要确认：方向", question: "螺纹钢2610 一手，做多还是做空？",
  missing: ["direction"], options: [{ label: "买开（做多）", prompt: "螺纹买开一手" }, { label: "卖开（做空）", prompt: "螺纹卖开一手" }],
  actions: [], createdAt: "2026-10-04T01:30:00Z",
};

test("追问卡: the question, the missing field and options that send their prompt", () => {
  const askAi = vi.fn();
  render(<OrderQuestionCard card={question} id="question-turn-q" handlers={{ askAi }} />);
  const card = screen.getByTestId("order-question-card");
  expect(card).toHaveTextContent("需要确认：方向");
  expect(card).toHaveTextContent("做多还是做空");
  expect(within(screen.getByLabelText("缺少的信息")).getByText("方向")).toHaveClass("sc-tag");
  fireEvent.click(screen.getByRole("button", { name: "回答：卖开（做空）" }));
  expect(askAi).toHaveBeenCalledWith("螺纹卖开一手");
  expect(screen.getByTestId("order-question-card-details")).toHaveTextContent("question-turn-q");
});

test("追问卡 names the planner's slots in words, never dir / product / act (#932 P2)", () => {
  render(<OrderQuestionCard card={{ ...question, missing: ["dir", "product", "act", "vol"] }} id="q2" handlers={{}} />);
  const tags = Array.from(screen.getByLabelText("缺少的信息").querySelectorAll(".sc-tag")).map(tag => tag.textContent);
  expect(tags).toEqual(["方向", "品种", "操作", "手数"]);
  expect(screen.getByTestId("order-question-card-details")).toHaveTextContent("缺少方向、品种、操作、手数");
  expect(screen.getByTestId("order-question-card")).not.toHaveTextContent(/\bdir\b|product|\bact\b/);
});

test("追问卡已回答: the badge shows the chosen option and the buttons no longer answer", () => {
  const askAi = vi.fn();
  const answered: OrderQuestion = { ...question, answered: true, answeredAt: "2026-10-08T03:30:00Z", answer: "买开（做多）" };
  render(<OrderQuestionCard card={answered} id="question-turn-q" handlers={{ askAi }} />);
  const card = screen.getByTestId("order-question-card");
  expect(card).toHaveTextContent("已回答");
  expect(screen.getByTestId("order-question-answered")).toHaveTextContent("已回答：买开（做多）");
  const option = screen.getByRole("button", { name: "回答：卖开（做空）" });
  expect(option).toBeDisabled();
  fireEvent.click(option);
  expect(askAi).not.toHaveBeenCalled();
});

test("没能生成方案卡: reason, the user's words and a UI way forward; without handlers every button is disabled", () => {
  const noCard: OrderQuestion = { ...question, kind: "no_card", title: "没能生成方案卡", question: "这一单没有生成方案卡。", options: [],
    reason: "合约在回放中不可交易", userText: "帮我开一手螺纹", actions: [{ kind: "ui", label: "打开下单台", changes: { panel: "order" } }] };
  const { rerender } = render(<OrderQuestionCard card={noCard} handlers={{}} />);
  expect(screen.getByTestId("order-question-card")).toHaveTextContent("合约在回放中不可交易");
  expect(screen.getByLabelText("你说的")).toHaveTextContent("帮我开一手螺纹");
  expect(screen.getByRole("button", { name: "打开下单台" })).toBeDisabled();
  const applyUi = vi.fn();
  rerender(<OrderQuestionCard card={noCard} handlers={{ applyUi }} />);
  fireEvent.click(screen.getByRole("button", { name: "打开下单台" }));
  expect(applyUi).toHaveBeenCalledWith({ panel: "order" });
});

const judgment: Judgment = {
  turnId: "turn-j", cardId: "judgment-SHFE.rb-2026-10-04", symbol: "SHFE.rb2610", productName: "螺纹钢", status: "ready",
  stance: "偏多", confidence: "中", headline: "库存连续下降，现货走强",
  facts: [{ text: "社会库存环比下降", quote: "库存 520 万吨", source: "钢联", asOf: "2026-10-03" }],
  invalidation: [{ text: "需求回落", quote: "表观消费下滑", source: "钢联", asOf: "2026-10-03" }],
  generatedAt: "2026-10-04T00:46:00+00:00", researchDay: "2026-10-04",
  actions: [{ kind: "ui", label: "打开调研", changes: { news: { tab: "desk" } } }],
};

test("判断卡 pending: a 生成中 badge and the default note", () => {
  render(<ResearchJudgmentCard card={{ ...judgment, status: "pending", stance: null, facts: [], invalidation: [], actions: [] }} handlers={{}} />);
  const card = screen.getByTestId("research-judgment-card");
  expect(card).toHaveTextContent("生成中");
  expect(screen.getByRole("status")).toHaveTextContent("今日判断生成中，完成后会在这里更新");
});

test("判断卡 ready: the sentence without a stance or 置信 label (第 5 批), facts and invalidation with quote and source, 打开调研", () => {
  const applyUi = vi.fn();
  render(<ResearchJudgmentCard card={judgment} id={judgment.cardId} handlers={{ applyUi }} />);
  const card = screen.getByTestId("research-judgment-card");
  expect(card).not.toHaveTextContent("偏多");
  expect(card).not.toHaveTextContent("置信");
  expect(within(card).getByLabelText("关键事实")).toHaveTextContent("社会库存环比下降");
  expect(within(card).getByLabelText("关键事实")).toHaveTextContent("库存 520 万吨");
  expect(within(card).getByLabelText("关键事实")).toHaveTextContent("钢联 · 2026-10-03");
  expect(within(card).getByLabelText("失效条件")).toHaveTextContent("需求回落");
  expect(card).toHaveTextContent(/生成于 \d{2}:\d{2}/);
  fireEvent.click(screen.getByRole("button", { name: "打开调研" }));
  expect(applyUi).toHaveBeenCalledWith({ news: { tab: "desk" } });
});

const reminder: Reminder = {
  turnId: "pricealert-a1", cardId: "reminder-pricealert-a1", kind: "price_alert", title: "到价提醒", summary: "螺纹钢2610 涨到 3200（现 3201）",
  figures: [{ label: "目标价", value: "3200" }, { label: "最新价", value: "3201", tone: "warning" }], symbol: "SHFE.rb2610", at: "2026-10-04T01:30:00Z",
  actions: [{ kind: "ask", label: "问 AI 怎么办", prompt: "螺纹钢2610 到了 3200，现在怎么办？" }],
};

test("提醒卡: summary, toned figures, time and an ask button — no order button", () => {
  const askAi = vi.fn();
  render(<ReminderCard card={reminder} id={reminder.cardId} handlers={{ askAi }} />);
  const card = screen.getByTestId("reminder-card");
  expect(card).toHaveTextContent("螺纹钢2610 涨到 3200（现 3201）");
  expect(within(card).getByText("3201").closest("div")).toHaveAttribute("data-tone", "warning");
  expect(card).toHaveTextContent(/\d{2}:\d{2}/);
  expect(within(card).queryByRole("button", { name: /下单|确认/ })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "问 AI 怎么办" }));
  expect(askAi).toHaveBeenCalledWith("螺纹钢2610 到了 3200，现在怎么办？");
});

const execution: Execution = {
  turnId: "turn-x", cardId: "execution-AIT123", source: "ai", sourceLabel: "AI 自主", orderKind: "order", symbol: "SHFE.rb2610",
  name: "螺纹钢2610", sideLabel: "买开", volume: 1, price: "3200", reason: "突破前高", status: "unknown", statusLabel: "结果未知",
  filledVolume: null, clientOrderId: "AIT123", updatedAt: "2026-10-04T01:30:00Z",
};

test("执行回报卡: unknown shows 结果未知 as a warning, never success; a fill shows the filled volume", () => {
  const { rerender } = render(<ExecutionCard card={execution} id={execution.cardId} />);
  const card = screen.getByTestId("execution-card");
  expect(card).toHaveTextContent("AI 自主 · 买开 螺纹钢2610 1 手 @ 3200");
  expect(within(card).getByText("结果未知")).toHaveClass("card-badge", "warning");
  expect(card).not.toHaveTextContent(/已成交|成功/);
  expect(screen.getByTestId("execution-card-details")).toHaveTextContent("AIT123");
  expect(card).toHaveAttribute("data-phase", "open");
  expect(card.querySelector(".card-outcome")).toBeNull();
  for (const [status, statusLabel] of [["unfilled", "未成交（已撤）"], ["rejected", "已拒绝"]] as const) {
    rerender(<ExecutionCard card={{ ...execution, status, statusLabel }} id={execution.cardId} />);
    const shown = screen.getByTestId("execution-card");
    expect(shown).toHaveAttribute("data-phase", "open");  // never the green settled outcome
    expect(shown.querySelector(".card-outcome")).toBeNull();
    expect(within(shown).getByText(statusLabel)).toHaveClass("card-badge");
  }
  // A fill: the head badge gives way to the green outcome row, and the card settles ("done", live).
  rerender(<ExecutionCard card={{ ...execution, status: "filled", statusLabel: "已成交", filledVolume: 1 }} id={execution.cardId} />);
  const filled = screen.getByTestId("execution-card");
  expect(filled).toHaveAttribute("data-phase", "done");
  expect(filled).toHaveAttribute("data-settled", "live");
  expect(filled.querySelector(".card-badge")).toBeNull();
  expect(filled.querySelector(".card-outcome")).toHaveTextContent("已成交");
  expect(filled).toHaveTextContent("1/1 手");
});

test("判断卡 settles from 生成中 to 完成 on screen; the other new cards carry their phase", () => {
  const pending = { turnId: "t", cardId: "j1", symbol: "SHFE.rb2610", productName: "螺纹钢", status: "pending" as const };
  const { rerender } = render(<ResearchJudgmentCard card={pending} id="j1" handlers={{}} />);
  expect(screen.getByTestId("research-judgment-card")).toHaveAttribute("data-phase", "busy");
  rerender(<ResearchJudgmentCard card={{ ...pending, status: "ready", stance: "偏多", confidence: "中", headline: "库存下降" }} id="j1" handlers={{}} />);
  expect(screen.getByTestId("research-judgment-card")).toHaveAttribute("data-phase", "done");
  expect(screen.getByTestId("research-judgment-card")).toHaveAttribute("data-settled", "live");
});

test("快讯卡: source, contract and a 问 AI question that names the contract", () => {
  const askAi = vi.fn();
  render(<BriefCard item={{ id: "brief:1", kind: "market", time: Date.parse("2026-10-04T01:30:00Z"), title: "行情异动", detail: "5 分钟拉升 1.2%",
    source: "行情监控", symbol: "SHFE.rb2610", symbolName: "螺纹钢2610" }} askAi={askAi} />);
  const card = screen.getByTestId("brief-card");
  expect(card).toHaveTextContent("行情异动");
  expect(card).toHaveTextContent("螺纹钢2610");
  expect(card).toHaveTextContent("来源：行情监控");
  fireEvent.click(screen.getByRole("button", { name: "问 AI 这次异动" }));
  expect(askAi).toHaveBeenCalledWith("螺纹钢26105 分钟拉升 1.2%现在怎么看？");
});

test("a card's UI command keeps only the whitelisted keys", () => {
  expect(pickCardUiChanges({ panel: "order", preference: { key: "x" }, settingsOpen: true, symbol: "SHFE.rb2610", view: "trade" }))
    .toEqual({ panel: "order", symbol: "SHFE.rb2610", view: "trade" });
});

test("the 今日复盘 card shows the day's P&L and tags, and 看复盘 opens the review on that day", () => {
  const applyUi = vi.fn();
  const card: Reminder = { turnId: "t", cardId: "reminder-t", kind: "daily_review", title: "今日复盘 · 10-09", summary: "今天四笔里三笔在计划内。",
    at: "2026-10-09T07:15:00Z", tradingDay: "2026-10-09", pnl: "+2,100",
    tags: [{ tag: "plan", label: "计划内", count: 3 }, { tag: "remind", label: "提醒后", count: 1 }],
    actions: [{ kind: "ui", label: "看复盘", changes: { reviewDay: "2026-10-09" } }] };
  render(<ReminderCard card={card} id={card.cardId} handlers={{ applyUi }} />);
  const shown = screen.getByTestId("reminder-card");
  expect(shown).toHaveTextContent("当日 +2,100");
  expect(shown).toHaveTextContent("计划内 3");
  expect(shown).toHaveTextContent("提醒后 1");
  fireEvent.click(screen.getByRole("button", { name: "看复盘" }));
  expect(applyUi).toHaveBeenCalledWith({ reviewDay: "2026-10-09" });
});

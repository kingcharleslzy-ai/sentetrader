import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { AccountSnapshot } from "../api";
import type { AgentTask, UiEvent } from "@ai-trader/ui-events";
import { conversationReducer, initialConversationState, type ConversationTurn } from "../state/conversation";
import { Conversation, cardSpeaksForTurn, type ProposalActions } from "./Conversation";

const actions: ProposalActions = {
  adjustRisk: vi.fn(),
  confirm: vi.fn(),
  reject: vi.fn(),
  cancelRun: vi.fn(),
  changeStrategyMonitoring: vi.fn(),
  loadStrategyMonitor: vi.fn()
};

const startupTurn: ConversationTurn = {
  id: "startup",
  role: "assistant",
  text: "Codex 已连接到 AI Trader 完整工程区。TqKq 仅提供只读数据，真实下单能力保持关闭。",
  timestamp: "2026-08-29T16:23:00+08:00",
  cards: [{
    id: "account-startup",
    type: "account",
    payload: {
      turnId: "startup",
      accountName: "TqKq 隔离只读账户",
      updatedAt: "尚无有效快照",
      dataStatus: "unavailable",
      equity: 0,
      todayPnl: 0,
      marginRatio: 0,
      positionCount: 0,
      riskBudgetUsed: 0,
      status: "warning",
      insight: "隔离 Sidecar 尚无有效快照；零值不是账户事实。",
      equityCurve: [0]
    }
  }]
};

const connectedAccount: AccountSnapshot = {
  accountName: "TqKq 模拟账户",
  capturedAt: "2026-08-31T09:00:00Z",
  ageSeconds: 12 * 60,
  dataStatus: "stale",
  equity: 1_033_140,
  todayPnl: 0,
  marginRatio: 15.423582476721453,
  positionCount: 1
};

test("startup status keeps the useful account card without a synthetic assistant message", () => {
  render(<Conversation turns={[startupTurn]} actions={actions} />);
  expect(screen.getByTestId("account-card")).toBeVisible();
  expect(screen.queryByText(/Codex 已连接到 AI Trader/)).not.toBeInTheDocument();
  expect(screen.queryByText("Codex")).not.toBeInTheDocument();
  expect(screen.queryByText("AI")).not.toBeInTheDocument();
});

test("每条消息不再重复头像和名字：AI 一行是 Sente 小标 + 时间，你的一行只有时间；发言人留给读屏与快照", () => {
  const turns: ConversationTurn[] = [
    { id: "u1", role: "user", text: "黄金怎么看？", timestamp: "2026-10-05T01:02:00Z", cards: [] },
    { id: "a1", role: "assistant", text: "偏多，回踩再看。", timestamp: "2026-10-05T01:02:30Z", cards: [] },
  ];
  const { container } = render(<Conversation turns={turns} actions={actions} />);
  expect(container.querySelector(".avatar")).toBeNull();
  const [user, assistant] = [...container.querySelectorAll<HTMLElement>(".message")];
  const userLine = user.querySelector<HTMLElement>(".speaker")!;
  const aiLine = assistant.querySelector<HTMLElement>(".speaker")!;
  expect(userLine.querySelector(".brand-mark")).toBeNull();
  expect(aiLine.querySelector(".brand-mark")).not.toBeNull();
  // The name is only the visually hidden text: one per message, nothing else says 你 / AI.
  expect(userLine.querySelector(".visually-hidden")).toHaveTextContent("你");
  expect(aiLine.querySelector(".visually-hidden")).toHaveTextContent("AI");
  expect(within(user).getAllByText("你")).toHaveLength(1);
  expect(within(assistant).getAllByText("AI")).toHaveLength(1);
  expect(userLine.querySelector("time")).not.toBeNull();
  expect(aiLine.querySelector("time")).not.toBeNull();
});

test("startup account card follows the latest account overview", () => {
  render(<Conversation
    turns={[startupTurn]}
    actions={actions}
    accountOverride={connectedAccount}
  />);
  const card = screen.getByTestId("account-card");
  expect(card).toHaveTextContent("¥1,033,140");
  expect(card).toHaveTextContent("15.4%");
  expect(card).toHaveTextContent("12分钟前");
  expect(card).toHaveTextContent("当前数值仅供参考");
  expect(card).not.toHaveTextContent("暂时无法读取");
});

test("assistant source URLs are rendered as safe external links", () => {
  const turn: ConversationTurn = {
    id: "answer-with-source",
    role: "assistant",
    text: "来源：https://example.com/market-report",
    timestamp: "2026-09-03T09:30:00+08:00",
    cards: []
  };
  render(<Conversation turns={[turn]} actions={actions} />);
  const link = screen.getByRole("link", { name: "https://example.com/market-report" });
  expect(link).toHaveAttribute("href", "https://example.com/market-report");
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noreferrer");
});

test("structured futures advice is rendered as a scannable card", () => {
  const turn: ConversationTurn = {
    id: "structured-advice",
    role: "assistant",
    text: [
      "**结论**：先观望，不追涨。",
      "",
      "**为什么**",
      "- 价格还没有走出明确方向。",
      "- 成交没有明显增加。",
      "",
      "**风险/边界**",
      "- 跌破近期低点后，原判断失效。",
      "",
      "**现在怎么做**",
      "- 等突破后再重新判断。",
    ].join("\n"),
    timestamp: "2026-09-03T09:30:00+08:00",
    cards: []
  };
  render(<Conversation turns={[turn]} actions={actions} />);
  const advice = screen.getByTestId("ai-advice");
  expect(advice).toHaveTextContent("先观望，不追涨");
  expect(advice).toHaveTextContent("价格还没有走出明确方向");
  expect(screen.getByRole("heading", { name: "现在怎么做" })).toBeVisible();
  expect(advice).not.toHaveTextContent("**");
});

test("structured futures advice stacks headings above content and collapses sources", () => {
  const turn: ConversationTurn = {
    id: "structured-advice-with-sources",
    role: "assistant",
    text: [
      "结论：先观望。",
      "为什么：方向还不明确。",
      "风险：跌破低点后判断失效。",
      "现在怎么做：等待确认。",
      "来源：今日市场报告，https://example.com/today，2026-09-03 10:00。",
    ].join("\n"),
    timestamp: "2026-09-03T10:01:00+08:00",
    cards: []
  };

  render(<Conversation turns={[turn]} actions={actions} />);

  const advice = screen.getByTestId("ai-advice");
  const sourceSummary = within(advice).getByText("参考依据 · 1 条");
  const sourceDisclosure = sourceSummary.closest("details");
  expect(sourceDisclosure).not.toHaveAttribute("open");
  fireEvent.click(sourceSummary);
  expect(sourceDisclosure).toHaveAttribute("open");
  expect(within(advice).getByRole("link", { name: "https://example.com/today" })).toBeVisible();
});

test("structured futures advice accepts markdown headings from the model", () => {
  const turn: ConversationTurn = {
    id: "markdown-structured-advice",
    role: "assistant",
    text: [
      "## 结论",
      "先等回踩，不追高。",
      "",
      "## 为什么",
      "- 价格已接近短线高点。",
      "- 成交没有明显放大。",
      "",
      "## 风险",
      "跌破近期低点后判断失效。",
      "",
      "## 现在怎么做",
      "回踩企稳后再考虑。",
      "",
      "## 来源",
      "今日市场报告，https://example.com/latest，2026-09-03 13:30。",
    ].join("\n"),
    timestamp: "2026-09-03T13:31:00+08:00",
    cards: []
  };

  render(<Conversation turns={[turn]} actions={actions} />);

  const advice = screen.getByTestId("ai-advice");
  expect(within(advice).getByRole("heading", { name: "结论" })).toBeVisible();
  expect(within(advice).getByRole("heading", { name: "为什么" })).toBeVisible();
  expect(within(advice).getByRole("heading", { name: "现在怎么做" })).toBeVisible();
  expect(within(advice).getByText("参考依据 · 1 条").closest("details")).not.toHaveAttribute("open");
  expect(advice).not.toHaveTextContent("##");
});

test("a plain reply keeps its paragraphs and renders a markdown table as a real table", () => {
  const turn: ConversationTurn = {
    id: "positions",
    role: "assistant",
    text: "结论：你现在只有三个持仓。\n\n| 合约 | 方向 | 手数 | 浮动盈亏 |\n|---|---|---|---|\n| 棉纱2611 | 多 | 1 | -25 |\n| 螺纹2701 | 空 | 1 | +290 |\n\n最近三条相关消息：\n9月17日，进口棉纱外盘报价继续回落。\n9月16日，螺纹现货报 3100 元/吨左右。\n\n来源：\n进口纱外盘报价回落（棉纺织技术新传媒，2026-09-17）\n今日钢价（西南钢铁指数，2026-09-16）",
    timestamp: "2026-09-18T12:23:00+08:00",
    cards: []
  };
  render(<Conversation turns={[turn]} actions={actions} />);
  const table = screen.getByRole("table");
  expect(within(table).getAllByRole("columnheader").map(cell => cell.textContent)).toEqual(["合约", "方向", "手数", "浮动盈亏"]);
  expect(within(table).getAllByRole("row")).toHaveLength(3);
  expect(screen.queryByText(/\|---/)).not.toBeInTheDocument();
  const paragraphs = document.querySelectorAll(".reply-paragraph");
  expect(paragraphs).toHaveLength(2);
  expect(paragraphs[1].querySelectorAll("br")).toHaveLength(2);
  expect(paragraphs[1]).toHaveTextContent("9月17日，进口棉纱外盘报价继续回落。");
  const sources = screen.getByText("参考依据 · 2 条").closest("details");
  expect(sources).not.toHaveAttribute("open");
  expect(sources).toHaveTextContent("今日钢价（西南钢铁指数，2026-09-16）");
});

test("the AI's work shows as a status line while running and folds into 过程 afterwards; warnings are footnotes, not cards", () => {
  const steps = [{ id: "understand", label: "模型正在分析", status: "done" as const, detail: "完成" }, { id: "s1", label: "读取行情", status: "active" as const, detail: "SHFE.rb2701" }];
  const task = { turnId: "turn-run", taskId: "task-1", title: "AI", subtitle: "", progress: 40, status: "running" as const, steps };
  const running: ConversationTurn = {
    id: "turn-run", role: "assistant", text: "正在处理这个请求…", timestamp: "2026-09-18T14:00:00+08:00", completed: false,
    cards: [{ id: "task-1", type: "task", payload: task }]
  };
  const cancelRun = vi.fn();
  const view = render(<Conversation turns={[running]} actions={{ ...actions, cancelRun }} />);
  expect(screen.getByTestId("turn-progress")).toHaveTextContent("正在读取行情…");
  expect(screen.queryByTestId("turn-process")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  expect(cancelRun).toHaveBeenCalledWith("task-1");

  const done: ConversationTurn = {
    ...running, text: "螺纹最新价 3126。", completed: true,
    cards: [
      { id: "task-1", type: "task", payload: { ...task, status: "completed", steps: steps.map(step => ({ ...step, status: "done" as const })) } },
      { id: "risk-1", type: "risk", payload: { turnId: "turn-run", title: "本轮未读取实时数据", message: "价格来自模型自身知识。", severity: "warning", deterministic: true } },
      { id: "risk-2", type: "risk", payload: { turnId: "turn-run", title: "超出策略预算", message: "拦截", severity: "danger", deterministic: true } }
    ]
  };
  view.rerender(<Conversation turns={[done]} actions={{ ...actions, cancelRun }} />);
  expect(screen.queryByTestId("turn-progress")).not.toBeInTheDocument();
  expect(screen.queryByText(/已完成/)).not.toBeInTheDocument();
  const process = screen.getByTestId("turn-process");
  expect(process).not.toHaveAttribute("open");
  expect(process).toHaveTextContent("过程 · 2 步");
  expect(process).toHaveTextContent("读取行情");
  const footnotes = screen.getAllByTestId("turn-footnote");
  expect(footnotes.map(node => node.textContent)).toEqual(["本轮未读取实时数据", "已阻止 · 超出策略预算"]);
  expect(footnotes[0]).toHaveAttribute("title", "价格来自模型自身知识。");
  expect(screen.queryByRole("region", { name: /核验|提示/ })).not.toBeInTheDocument();
});

test("对象级 AI: the user message shows its object above the bubble", () => {
  render(<Conversation actions={actions} turns={[{ id: "asked", role: "user", text: "这笔持仓的风险怎么样", timestamp: "2026-09-25T10:00:00+08:00",
    focus: { kind: "position", label: "螺纹2701 多 2 手" }, cards: [] }]} />);
  expect(screen.getByText("@ 螺纹2701 多 2 手")).toHaveAttribute("data-focus-kind", "position");
  expect(screen.getByText("这笔持仓的风险怎么样")).toBeVisible();
});

test("an order, close or short quote card replaces the model's words in the chat; a longer analysis stays", () => {
  const turn = (text: string, type: string): ConversationTurn => ({
    id: `t-${type}`, role: "assistant", text, timestamp: "2026-09-28T13:41:00+08:00",
    cards: [{ id: `c-${type}`, type, payload: {} } as unknown as ConversationTurn["cards"][number]]
  });
  expect(cardSpeaksForTurn(turn("螺纹2701 卖出平今 1 手，价格 3100，把这一手多单全平掉，确认吗？", "close-batch"))).toBe(true);
  expect(cardSpeaksForTurn(turn("螺纹2701 买开 1 手，对手价 3103，确认吗？", "trade"))).toBe(true);
  expect(cardSpeaksForTurn(turn("做多价差 1 份，两腿同时以对手价发送，确认吗？", "spread"))).toBe(true);
  expect(cardSpeaksForTurn(turn("螺纹现在 3100，涨 0.3%。", "market"))).toBe(true);
  expect(cardSpeaksForTurn(turn("走势分析：".padEnd(200, "。"), "market"))).toBe(false);
  expect(cardSpeaksForTurn({ ...turn("好的", "trade"), role: "user" })).toBe(false);
  expect(cardSpeaksForTurn(turn("回测结果如下", "backtest"))).toBe(false);
});

test("a streaming reply: the placeholder shows as thinking, the words type out, and a reply opening with 结论 keeps the advice layout", () => {
  vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame"] });
  try {
    const base: ConversationTurn = { id: "turn-live", role: "assistant", text: "正在处理这个请求…", timestamp: "2026-10-04T10:00:00+08:00", completed: false, cards: [] };
    const view = render(<Conversation turns={[base]} actions={actions} />);
    expect(screen.getByText("正在处理这个请求…")).toHaveClass("thinking-text");

    const partial = "结论：偏多，回踩 3150 可以考虑。\n为什么：持仓";
    view.rerender(<Conversation turns={[{ ...base, text: partial }]} actions={actions} />);
    act(() => { vi.advanceTimersByTime(48); });
    const typing = document.querySelector(".message .bubble")!;
    expect(typing).toHaveClass("advice-bubble");
    expect(typing.textContent!.length).toBeLessThan(partial.length);
    act(() => { vi.advanceTimersByTime(400); });
    expect(screen.getByTestId("ai-advice")).toHaveTextContent("偏多，回踩 3150 可以考虑。");

    const full = `${partial}前二十多头增仓。\n风险：跌破 3100 作废。`;
    view.rerender(<Conversation turns={[{ ...base, text: full, completed: true }]} actions={actions} />);
    act(() => { vi.advanceTimersByTime(400); });
    expect(screen.getByTestId("ai-advice")).toHaveTextContent("跌破 3100 作废。");
    expect(screen.queryByText("正在处理这个请求…")).not.toBeInTheDocument();
  } finally { vi.useRealTimers(); }
});

// 结构化卡片 (2026-10-04).
const cardTurn = (text: string, cards: ConversationTurn["cards"], id = "turn-s"): ConversationTurn => ({
  id, role: "assistant", text, timestamp: "2026-10-04T09:30:00+08:00", completed: true, cards
});
const judgmentCard: ConversationTurn["cards"][number] = { id: "judgment-1", type: "research-judgment", payload: {
  turnId: "turn-s", cardId: "judgment-1", symbol: "SHFE.rb2610", productName: "螺纹钢", status: "ready", stance: "偏空",
  confidence: "低", headline: "需求走弱", facts: [], invalidation: [],
  actions: [{ kind: "ui", label: "打开调研", changes: { news: { tab: "desk" }, settingsOpen: true } }]
} };
const reminderCard: ConversationTurn["cards"][number] = { id: "reminder-1", type: "reminder", payload: {
  turnId: "turn-s", cardId: "reminder-1", kind: "risk_ratio", title: "风险度提醒", summary: "风险度升到 82%",
  figures: [{ label: "风险度", value: "82%", tone: "danger" }], at: "2026-10-04T01:30:00Z",
  actions: [{ kind: "ask", label: "问 AI 怎么减仓", prompt: "风险度 82%，怎么减仓？" }]
} };
const questionCard: ConversationTurn["cards"][number] = { id: "question-1", type: "order-question", payload: {
  turnId: "turn-s", cardId: "question-1", kind: "ask", title: "需要确认：手数", question: "开几手？", missing: ["volume"],
  options: [{ label: "1 手", prompt: "开 1 手" }, { label: "2 手", prompt: "开 2 手" }], createdAt: "2026-10-04T01:30:00Z"
} };
const executionCard: ConversationTurn["cards"][number] = { id: "execution-1", type: "execution", payload: {
  turnId: "turn-s", cardId: "execution-1", source: "strategy", sourceLabel: "策略自动", orderKind: "order", symbol: "SHFE.rb2610",
  name: "螺纹钢2610", sideLabel: "卖开", volume: 2, price: "3190", status: "unknown", statusLabel: "结果未知", updatedAt: "2026-10-04T01:30:00Z"
} };

test("reminder / order-question / execution cards speak for the turn; the judgment card keeps the text", () => {
  expect(cardSpeaksForTurn(cardTurn("风险度提醒：风险度升到 82%。", [reminderCard]))).toBe(true);
  expect(cardSpeaksForTurn(cardTurn("开几手？", [questionCard]))).toBe(true);
  expect(cardSpeaksForTurn(cardTurn("策略自动卖开 2 手，结果未知。", [executionCard]))).toBe(true);
  expect(cardSpeaksForTurn(cardTurn("螺纹偏空看待。", [judgmentCard]))).toBe(false);
  render(<Conversation turns={[cardTurn("风险度提醒：风险度升到 82%。", [reminderCard], "t1"), cardTurn("策略自动卖开 2 手，结果未知。", [executionCard], "t2")]} actions={actions} />);
  expect(screen.queryByText("风险度提醒：风险度升到 82%。")).not.toBeInTheDocument();
  expect(screen.getByTestId("reminder-card")).toHaveTextContent("风险度升到 82%");
  expect(screen.getByTestId("execution-card")).toHaveTextContent("结果未知");
  expect(screen.queryByText("策略自动卖开 2 手，结果未知。")).not.toBeInTheDocument();
});

test("the judgment card sits above the bubble and the answer continues below it", () => {
  render(<Conversation turns={[cardTurn("螺纹偏空看待，反弹减仓。", [judgmentCard])]} actions={actions} />);
  const card = screen.getByTestId("research-judgment-card");
  const bubble = screen.getByText("螺纹偏空看待，反弹减仓。");
  expect(card.compareDocumentPosition(bubble) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(card).not.toHaveTextContent("偏空");
});

test("a card-only turn (card first, message not here yet) shows the card without an empty bubble", () => {
  const { container } = render(<Conversation turns={[{ ...cardTurn("", [judgmentCard]), completed: undefined }]} actions={actions} />);
  expect(screen.getByTestId("research-judgment-card")).toBeVisible();
  expect(container.querySelector(".bubble")).toBeNull();
});

test("card buttons: options and 问 AI call askAi, UI buttons call applyUi with whitelisted keys only, missing handlers disable them", () => {
  const askAi = vi.fn();
  const applyUi = vi.fn();
  const turns = [cardTurn("开几手？", [questionCard], "q"), cardTurn("风险度提醒", [reminderCard], "r"), cardTurn("偏空", [judgmentCard], "j")];
  const view = render(<Conversation turns={turns} actions={{ ...actions, askAi, applyUi }} />);
  fireEvent.click(screen.getByRole("button", { name: "回答：2 手" }));
  expect(askAi).toHaveBeenCalledWith("开 2 手");
  fireEvent.click(screen.getByRole("button", { name: "问 AI 怎么减仓" }));
  expect(askAi).toHaveBeenLastCalledWith("风险度 82%，怎么减仓？");
  fireEvent.click(screen.getByRole("button", { name: "打开调研" }));
  expect(applyUi).toHaveBeenCalledWith({ news: { tab: "desk" } });
  view.rerender(<Conversation turns={turns} actions={actions} />);
  expect(screen.getByRole("button", { name: "回答：2 手" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "问 AI 怎么减仓" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "打开调研" })).toBeDisabled();
});

test("a brief is a compact card whose 问 AI names the contract", () => {
  const askAi = vi.fn();
  render(<Conversation turns={[]} actions={{ ...actions, askAi }} systemItems={[{ id: "brief:n1", kind: "news", time: Date.parse("2026-10-04T01:00:00Z"),
    title: "重要资讯", detail: "唐山限产加码", source: "财联社", symbol: "SHFE.rb2610", symbolName: "螺纹钢2610" }]} />);
  expect(screen.getByTestId("brief-card")).toHaveTextContent("重要资讯");
  fireEvent.click(screen.getByRole("button", { name: "问 AI 这条资讯" }));
  expect(askAi).toHaveBeenCalledWith("这条资讯对螺纹钢2610有什么影响：唐山限产加码");
});

test("a question-card or refusal turn folds into 过程 live even when a stale running snapshot arrives after task.completed", () => {
  // 2026-10-04 线上：SSE 每批先发持久事件、后发瞬时事件，合并窗口里的 task.progress 晚于 task.completed 到达。
  let seq = 0;
  const event = (name: string, kind: string, payload: unknown) =>
    ({ schemaVersion: 1, id: `e${++seq}`, sessionId: "s", timestamp: "2026-10-04T06:00:00Z", kind, name, payload }) as unknown as UiEvent;
  const base: AgentTask = { turnId: "turn-ask", taskId: "run-ask", title: "AI 正在处理", subtitle: "", progress: 8, status: "running",
    steps: [{ id: "understand", label: "等待模型响应", status: "active", detail: "处理中" }] };
  const asked: AgentTask = { ...base, progress: 88, steps: [{ ...base.steps[0], status: "done", detail: "完成" },
    { id: "fc-1", label: "向你确认下单信息", status: "done", detail: "已提问" }] };
  const arrival = [
    event("message.delta", "message", { turnId: "turn-ask", role: "assistant", text: "正在处理这个请求…", agentId: "native" }),
    event("task.started", "progress", base),
    event("task.progress", "progress", { ...asked, steps: [asked.steps[0], { ...asked.steps[1], status: "active", detail: "处理中" }] }),
    event("card.order_question", "artifact", { turnId: "turn-ask", cardId: "question-turn-ask", kind: "ask", title: "需要确认：方向",
      question: "螺纹钢2610 一手，做多还是做空？", missing: ["direction"], options: [{ label: "买开", prompt: "螺纹买开一手" }], actions: [], createdAt: "2026-10-04T06:00:00Z" }),
    event("task.completed", "progress", { ...asked, progress: 100, status: "completed" }),
    event("message.completed", "message", { turnId: "turn-ask", role: "assistant", text: "螺纹钢2610 一手，做多还是做空？", agentId: "native" }),
    event("task.progress", "progress", asked) // the stale coalesced transient, last in the SSE batch
  ];
  const state = arrival.reduce((current, item) => conversationReducer(current, { type: "event", event: item }), initialConversationState);
  render(<Conversation turns={state.turns} actions={actions} />);
  expect(screen.queryByTestId("turn-progress")).not.toBeInTheDocument();
  expect(screen.queryByText(/等待模型响应…/)).not.toBeInTheDocument();
  expect(screen.getByTestId("turn-process")).toHaveTextContent("过程 · 2 步");
});

// 回答卡去重 (2026-10-14 跟进): “铁矿跟焦炭今天哪个强” showed the quote card, the 走势 card and the answer card side by side.
test("an answer card folds the turn's read cards into its 依据 and its conclusion is not said twice", () => {
  const market: ConversationTurn["cards"][number] = { id: "market-t", type: "market", payload: {
    turnId: "t", cardId: "market-t", symbol: "DCE.i2701", name: "铁矿石2701", price: 674, changePercent: -0.81,
    updatedAt: "16:52", trend: "down", priceCurve: [680, 674], dataStatus: "live",
    peers: [{ symbol: "DCE.j2701", name: "焦炭2701", price: 2000, changePercent: -1.6 }] } as never };
  const analysis: ConversationTurn["cards"][number] = { id: "data-analysis-t", type: "data", payload: {
    turnId: "t", cardId: "data-analysis-t", kind: "analysis", title: "走势 · 2 组", rows: [{ primary: "i2701 · 5 分钟" }],
    basis: { at: "2026-10-14T08:52:00Z", sources: ["走势分析"] } } };
  const news: ConversationTurn["cards"][number] = { id: "data-news-t", type: "data", payload: {
    turnId: "t", cardId: "data-news-t", kind: "news", title: "快讯 · 铁矿", rows: [{ primary: "港口库存回升" }],
    basis: { at: "2026-10-14T08:53:00Z", sources: ["快讯"] } } };
  const answer: ConversationTurn["cards"][number] = { id: "answer-t", type: "answer", payload: {
    turnId: "t", cardId: "answer-t", title: "铁矿 vs 焦炭", conclusion: "铁矿相对抗跌。",
    compare: { left: { title: "铁矿2701", lines: ["收 674，跌 0.81%"] }, right: { title: "焦炭2701", lines: ["收 2000，跌 1.60%"] } },
    reasons: ["焦炭跌幅是铁矿的两倍"], basis: { at: "2026-10-14T08:52:00Z", sources: ["行情快照 i2701"] },
    folded: ["market-t", "data-analysis-t", "card-never-shown"] } };
  // A read after the answer card (the news card) is not on it and stays beside it.
  const turn = cardTurn("铁矿相对抗跌。", [market, analysis, answer, news], "t");
  expect(cardSpeaksForTurn(turn)).toBe(true);
  render(<Conversation turns={[turn]} actions={actions} />);
  const conversation = screen.getByTestId("conversation");
  const topLevel = [...conversation.querySelectorAll(".conversation-card-anchor")].map(anchor => anchor.getAttribute("data-card-id"));
  expect(topLevel).toEqual(["answer-t", "data-news-t"]);
  expect(conversation.querySelector(".bubble")).toBeNull();
  const fold = screen.getByTestId("answer-card-details");
  const folded = within(fold).getByRole("group", { name: "原卡" });
  expect([...folded.querySelectorAll(".ac-folded-card")].map(item => item.getAttribute("data-card-id"))).toEqual(["market-t", "data-analysis-t"]);
  expect(within(folded).getByTestId("market-card")).toHaveTextContent("焦炭2701");
  expect(within(folded).getByTestId("data-card")).toHaveTextContent("走势 · 2 组");
  // The 674 of the quote card is on screen once (the compare), the folded card only after 依据 opens.
  expect(fold).not.toHaveAttribute("open");
  // A longer text than the conclusion stays in its bubble; no answer card leaves the read cards where they were.
  expect(cardSpeaksForTurn(cardTurn("铁矿相对抗跌。焦炭还在找底，先别抄。", [answer], "t2"))).toBe(false);
  // …also when the quote card it folded is short-text-sized: a folded card no longer speaks for the turn (#754).
  expect(cardSpeaksForTurn(cardTurn("铁矿相对抗跌。焦炭还在找底，先别抄。", [market, answer], "t3"))).toBe(false);
});

// @vitest-environment node
import type { AccountSummary, AgentTask, BacktestComparison, DataCard, TradeProposal, UiEvent } from "@ai-trader/ui-events";
import { PROPOSAL_SETTLE_AFTER_MS, conversationReducer, initialConversationState, settledProposalEvent, staleOpenProposals } from "./conversation";

const envelope = <T extends UiEvent>(event: Omit<T, "id" | "sessionId" | "timestamp" | "schemaVersion">): T => ({
  schemaVersion: 1,
  id: crypto.randomUUID(),
  sessionId: "session-test",
  timestamp: "2026-08-25T12:00:00Z",
  ...event
}) as T;

test("a runtime.status older than the session's known account revision is dropped whole; one without it is taken (Codex r4309822257)", async () => {
  const { resetAccountRevisions } = await import("../accountRevision");
  resetAccountRevisions();
  const status = (account: string, accountGeneration?: number) => envelope({ kind: "result", name: "runtime.status",
    payload: { mode: "local", account: { id: account }, ...(accountGeneration === undefined ? {} : { accountGeneration }) } } as unknown as UiEvent);
  let state = conversationReducer(initialConversationState, { type: "event", event: status("A", 2) });
  state = conversationReducer(state, { type: "event", event: status("B", 3) });
  expect(state.runtime?.account?.id).toBe("B");
  // Another slot's status from before the switch to B arrives late: no account change, nothing updated.
  const late = conversationReducer(state, { type: "event", event: status("A", 2) });
  expect(late).toBe(state);
  // A Runtime before the revision reports none: taken as before.
  expect(conversationReducer(state, { type: "event", event: status("C") }).runtime?.account?.id).toBe("C");
});

const message = envelope({
  kind: "message",
  name: "message.completed",
  payload: { turnId: "turn-1", role: "assistant", text: "账户如下", agentId: "native" }
} as UiEvent);

test("assistant turn is marked complete only after the terminal message event", () => {
  const delta = envelope({
    kind: "message",
    name: "message.delta",
    payload: { turnId: "voice-turn", role: "assistant", text: "正在处理", agentId: "native" }
  } as UiEvent);
  const completed = envelope({
    kind: "message",
    name: "message.completed",
    payload: { turnId: "voice-turn", role: "assistant", text: "模拟委托已受理", agentId: "native" }
  } as UiEvent);

  const streaming = conversationReducer(initialConversationState, { type: "event", event: delta });
  expect(streaming.turns[0].completed).toBe(false);
  const final = conversationReducer(streaming, { type: "event", event: completed });
  expect(final.turns[0]).toMatchObject({ completed: true, text: "模拟委托已受理" });
});

test("card events attach backend data to the correct conversation turn", () => {
  const account: AccountSummary = {
    turnId: "turn-1",
    accountName: "假数据模拟账户",
    updatedAt: "14:26",
    capturedAt: "2026-08-25T14:26:00+08:00",
    ageSeconds: 0,
    dataStatus: "simulated",
    equity: 100,
    todayPnl: 3,
    todayNetPnl: 3,
    floatProfit: 3,
    positionProfit: 3,
    closeProfit: 0,
    commission: 0,
    marginRatio: 1,
    positionCount: 0,
    riskBudgetUsed: 0,
    status: "normal",
    insight: "无风险",
    equityCurve: [1, 2]
  };
  let state = conversationReducer(initialConversationState, { type: "event", event: message });
  state = conversationReducer(state, {
    type: "event",
    event: envelope({ kind: "artifact", name: "card.account", payload: account } as UiEvent)
  });
  expect(state.turns).toHaveLength(1);
  expect(state.turns[0].cards[0].type).toBe("account");
});

test("direct historical backtest response is visible immediately and remains deduplicated", () => {
  const card: BacktestComparison = {
    turnId: "backtest-turn",
    taskId: "backtest-task",
    backtestId: "backtest-id",
    artifactDigest: "a".repeat(64),
    strategyDigest: "b".repeat(64),
    dataDigest: "c".repeat(64),
    engineDigest: "d".repeat(64),
    strategyReleaseId: "release-1",
    candidateVariantId: "variant-1",
    candidateConfigDigest: "e".repeat(64),
    candidateParameters: { baselineThreshold: "1.00", candidateThreshold: "0.90" },
    engineVersion: "tqsdk-3.10.2-tqsim-tqbacktest",
    dataSource: "tqsdk:SHFE.au2612:history",
    generatedAt: "2026-08-31T00:00:00Z",
    reproducible: true,
    sourceKind: "tqsdk_history",
    dataPointCount: 30,
    strategyName: "量价趋势研究",
    symbol: "SHFE.au2612",
    oldVersion: "基线 1.00",
    newVersion: "候选 0.90",
    period: "近 30 日",
    metrics: [],
    oldCurve: [1, 1.01],
    newCurve: [1, 1.02],
    conclusion: "候选参数表现更好",
    recommendation: "仅用于研究"
  };
  let state = conversationReducer(initialConversationState, { type: "local-backtest", card });
  expect(state.turns[0].cards).toHaveLength(1);
  expect(state.turns[0].cards[0].id).toBe("backtest-backtest-task");
  state = conversationReducer(state, {
    type: "event",
    event: envelope({ kind: "chart", name: "card.backtest", payload: card } as UiEvent)
  });
  expect(state.turns[0].cards).toHaveLength(1);
});

test("task progress replaces the existing task instead of duplicating cards", () => {
  const started: AgentTask = {
    turnId: "turn-1",
    taskId: "task-1",
    title: "任务",
    subtitle: "回测",
    progress: 10,
    status: "running",
    steps: [{ id: "one", label: "读取", status: "active", detail: "处理中" }]
  };
  let state = conversationReducer(initialConversationState, { type: "event", event: message });
  state = conversationReducer(state, {
    type: "event",
    event: envelope({ kind: "progress", name: "task.started", payload: started } as UiEvent)
  });
  state = conversationReducer(state, {
    type: "event",
    event: envelope({
      kind: "progress",
      name: "task.completed",
      payload: { ...started, progress: 100, status: "completed" }
    } as unknown as UiEvent)
  });
  expect(state.turns[0].cards).toHaveLength(1);
  const card = state.turns[0].cards[0];
  expect(card.type === "task" && card.payload.progress).toBe(100);
});

test("trade proposal update preserves a single proposal card", () => {
  const proposal: TradeProposal = {
    turnId: "turn-1",
    proposalId: "proposal-1",
    version: 1,
    symbol: "AU2612",
    name: "沪金",
    side: "buy_open",
    volume: 1,
    limitPrice: 823.5,
    stopPrice: 819,
    contractMultiplier: 1000,
    updatedAt: "2026-08-25T12:00:00Z",
    environment: "simulation",
    status: "risk_blocked",
    riskCheck: { passed: false, limit: 2000, estimatedLoss: 4500, reason: "超限", contractMultiplier: 1000, priceTick: "0.02" }
  };
  let state = conversationReducer(initialConversationState, { type: "event", event: message });
  state = conversationReducer(state, {
    type: "event",
    event: envelope({ kind: "approval", name: "proposal.trade", payload: proposal } as UiEvent)
  });
  state = conversationReducer(state, {
    type: "event",
    event: envelope({
      kind: "approval",
      name: "proposal.trade",
      payload: {
        ...proposal,
        status: "approval_required",
        stopPrice: 821.5,
        riskCheck: { ...proposal.riskCheck, passed: true, estimatedLoss: 2000 },
        approvalId: "approval-1"
      }
    } as unknown as UiEvent)
  });
  expect(state.turns[0].cards).toHaveLength(1);
  const card = state.turns[0].cards[0];
  expect(card.type === "trade" && card.payload.riskCheck.passed).toBe(true);
});

test("streaming assistant message updates one bubble instead of appending duplicates", () => {
  let state = conversationReducer(initialConversationState, {
    type: "event",
    event: envelope({
      kind: "message", name: "message.delta",
      payload: { turnId: "native-turn", role: "assistant", text: "正在", agentId: "native" }
    } as unknown as UiEvent)
  });
  state = conversationReducer(state, {
    type: "event",
    event: envelope({
      kind: "message", name: "message.completed",
      payload: { turnId: "native-turn", role: "assistant", text: "已经完成", agentId: "native" }
    } as unknown as UiEvent)
  });
  expect(state.turns).toHaveLength(1);
  expect(state.turns[0].text).toBe("已经完成");
});

test("feed snapshots never become conversation content", () => {
  let state = conversationReducer(initialConversationState, { type: "event", event: message });
  for (const [name, payload] of [
    ["feed.account", { account: {}, positions: [], activity: {}, risk: {} }],
    ["feed.market", { schemaVersion: 1, durationSeconds: 60, quote: {}, bars: [], liveTrading: "disabled" }],
    ["feed.activity", { environment: "simulation", symbol: "SHFE.au2612", orders: [], fills: [], strategies: [] }],
    ["feed.execution", { stops: [], simulationOnly: true, liveTrading: "disabled" }],
    ["feed.ui", { reason: "command" }],
  ] as const) {
    state = conversationReducer(state, { type: "event", event: envelope({ kind: "result", name, payload } as unknown as UiEvent) });
  }
  expect(state.turns).toHaveLength(1);
  expect(state.turns[0].cards).toHaveLength(0);
});

test("task errors become cards while only connection errors drive the Runtime banner", () => {
  let state = conversationReducer(initialConversationState, { type: "event", event: message });
  state = conversationReducer(state, {
    type: "event",
    event: envelope({
      kind: "error", name: "error",
      payload: { turnId: "turn-1", code: "tests_failed", message: "测试失败", scope: "task" }
    } as unknown as UiEvent)
  });
  expect(state.connectionError).toBeUndefined();
  expect(state.turns[0].cards[0].type).toBe("task-error");
  state = conversationReducer(state, {
    type: "event",
    event: envelope({
      kind: "error", name: "error",
      payload: { code: "runtime_unreachable", message: "连接失败", scope: "connection" }
    } as unknown as UiEvent)
  });
  expect(state.connectionError).toBe("连接失败");
});

test("agent diff and tool cards remain scoped to their own tasks", () => {
  const secondMessage = envelope({
    kind: "message", name: "message.completed",
    payload: { turnId: "turn-2", role: "assistant", text: "第二个任务", agentId: "native" }
  } as UiEvent);
  let state = conversationReducer(initialConversationState, { type: "event", event: message });
  state = conversationReducer(state, { type: "event", event: secondMessage });
  for (const [turnId, taskId, diff, output] of [
    ["turn-1", "task-1", "+first", "first"],
    ["turn-2", "task-2", "+second", "second"]
  ]) {
    state = conversationReducer(state, {
      type: "event",
      event: envelope({
        kind: "diff", name: "agent.diff",
        payload: {
          turnId, taskId, itemId: "turn-diff", status: "completed",
          changes: [{ path: "strategy.py", kind: "update", diff }]
        }
      } as unknown as UiEvent)
    });
    state = conversationReducer(state, {
      type: "event",
      event: envelope({
        kind: "tool_call", name: "agent.tool.completed",
        payload: {
          turnId, taskId, itemId: "command", toolType: "command",
          status: "completed", outputSummary: output
        }
      } as unknown as UiEvent)
    });
  }
  const firstDiff = state.turns[0].cards.find((card) => card.type === "agent-diff");
  const secondDiff = state.turns[1].cards.find((card) => card.type === "agent-diff");
  const firstTool = state.turns[0].cards.find((card) => card.type === "agent-tool");
  const secondTool = state.turns[1].cards.find((card) => card.type === "agent-tool");
  expect(firstDiff?.id).toBe("diff-task-1-turn-diff");
  expect(secondDiff?.id).toBe("diff-task-2-turn-diff");
  expect(firstDiff?.type === "agent-diff" && firstDiff.payload.changes[0].diff).toBe("+first");
  expect(secondDiff?.type === "agent-diff" && secondDiff.payload.changes[0].diff).toBe("+second");
  expect(firstTool?.id).toBe("tool-task-1-command");
  expect(secondTool?.id).toBe("tool-task-2-command");
});

test("voice turns keep their origin and spoken summary; a batch-close card follows the same confirm/result path as a trade", () => {
  const spokenUser = envelope({
    kind: "message", name: "message.completed",
    payload: { turnId: "v-user", role: "user", text: "全部平仓", origin: "voice" }
  } as UiEvent);
  const streaming = envelope({
    kind: "message", name: "message.delta",
    payload: { turnId: "v-answer", role: "assistant", text: "正在生成", agentId: "native", origin: "voice" }
  } as UiEvent);
  const completed = envelope({
    kind: "message", name: "message.completed",
    payload: { turnId: "v-answer", role: "assistant", text: "已生成批量平仓方案。", agentId: "native", origin: "voice", spoken: "两笔平仓，确认吗？" }
  } as UiEvent);
  const order = { symbol: "SHFE.rb2610", name: "螺纹钢2610", side: "sell_close" as const, offset: "CLOSETODAY" as const, positionBucket: "today" as const,
    volume: 2, limitPrice: 3100, contractMultiplier: 10, riskCheck: { passed: true, limit: 0, estimatedLoss: 0, reason: "平仓", contractMultiplier: 10, priceTick: "1" } };
  const batch = envelope({
    kind: "approval", name: "proposal.close_batch",
    payload: { turnId: "v-answer", proposalId: "batch-1", version: 1, environment: "simulation", reason: "全部平仓", orders: [order, { ...order, positionBucket: "historical", offset: "CLOSE" }],
      skipped: [], totalOrders: 2, totalVolume: 4, riskCheck: { passed: true }, updatedAt: "2026-09-17T12:00:00Z", status: "approval_required", approvalId: null }
  } as unknown as UiEvent);
  const withWindow = envelope({ ...batch, payload: { ...(batch.payload as object), voiceConfirmBy: "2026-09-17T12:00:15Z", approvalId: "approval-1" } } as unknown as UiEvent);
  const result = envelope({
    kind: "result", name: "proposal.result",
    payload: { proposalId: "batch-1", status: "candidate_recorded", auditId: "audit-1", message: "ok" }
  } as UiEvent);

  let state = [spokenUser, streaming, completed, batch].reduce((current, event) => conversationReducer(current, { type: "event", event }), initialConversationState);
  expect(state.turns[0]).toMatchObject({ role: "user", origin: "voice" });
  expect(state.turns[1]).toMatchObject({ role: "assistant", origin: "voice", spoken: "两笔平仓，确认吗？", completed: true });
  expect(state.turns[1].cards).toHaveLength(1);
  expect(state.turns[1].cards[0]).toMatchObject({ type: "close-batch", id: "batch-1" });

  state = conversationReducer(state, { type: "event", event: withWindow });
  expect(state.turns[1].cards).toHaveLength(1);
  expect(state.turns[1].cards[0].payload).toMatchObject({ voiceConfirmBy: "2026-09-17T12:00:15Z", approvalId: "approval-1" });

  state = conversationReducer(state, { type: "event", event: result });
  expect(state.turns[1].cards[0].payload).toMatchObject({ status: "candidate_recorded", auditId: "audit-1", voiceConfirmBy: null });
});

test("a card closed by its window or a newer plan says so, and a replayed card long past its window reads its record (#932 P2)", () => {
  const card = (proposalId: string, updatedAt: string) => envelope({
    kind: "approval", name: "proposal.trade",
    payload: { turnId: "t-77", proposalId, version: 1, symbol: "SHFE.ag2612", name: "沪银2612", side: "buy_open", volume: 1, limitPrice: 7800,
      contractMultiplier: 15, updatedAt, environment: "simulation", status: "approval_required", approvalId: "a",
      riskCheck: { passed: true, limit: 0, estimatedLoss: 0, reason: "ok", contractMultiplier: 15, priceTick: "1" } }
  } as unknown as UiEvent);
  const now = Date.parse("2026-10-16T13:37:00Z");
  let state = [card("old", "2026-10-16T12:00:00Z"), card("fresh", new Date(now - 60_000).toISOString())]
    .reduce((current, event) => conversationReducer(current, { type: "event", event }), initialConversationState);
  // Only the card whose 10-minute window ran out long ago is read back; a live one waits for its own result.
  expect(staleOpenProposals(state, now)).toEqual(["old"]);
  expect(staleOpenProposals(state, Date.parse("2026-10-16T12:00:00Z") + PROPOSAL_SETTLE_AFTER_MS - 1)).toEqual([]);
  expect(settledProposalEvent("s", { id: "old", status: "approval_required", payload: {} })).toBeUndefined();
  const settled = settledProposalEvent("s", { id: "old", status: "rejected", payload: { expired: true, auditId: "audit-9" } })!;
  state = conversationReducer(state, { type: "event", event: settled });
  expect(state.turns[0].cards[0].payload).toMatchObject({ status: "rejected", closedReason: "expired", auditId: "audit-9" });
  expect(staleOpenProposals(state, now)).toEqual([]);
  // A live result says why too: a newer plan replaced the card.
  state = conversationReducer(state, { type: "event", event: envelope({ kind: "result", name: "proposal.result",
    payload: { proposalId: "fresh", status: "rejected", auditId: "audit-10", message: "已作废", reason: "superseded" } } as UiEvent) });
  expect(state.turns[0].cards[1].payload).toMatchObject({ status: "rejected", closedReason: "superseded" });
});

test("a streamed turn whose task ends without a final answer is closed, so the conversation is not blocked for good", () => {
  const task: AgentTask = { turnId: "turn-9", taskId: "run-9", title: "AI 正在处理", subtitle: "", progress: 10, status: "running", steps: [] };
  let state = conversationReducer(initialConversationState, {
    type: "event", event: envelope({ kind: "message", name: "message.delta", payload: { turnId: "turn-9", role: "assistant", text: "正在处理这个请求…", agentId: "native" } } as UiEvent)
  });
  state = conversationReducer(state, { type: "event", event: envelope({ kind: "progress", name: "task.started", payload: task } as UiEvent) });
  expect(state.turns.find(turn => turn.id === "turn-9")?.completed).toBe(false);
  // 停止: the task ends cancelled and no message.completed ever comes (Runtimes before 1.6.6).
  state = conversationReducer(state, { type: "event", event: envelope({ kind: "progress", name: "task.completed", payload: { ...task, status: "cancelled" } } as UiEvent) });
  expect(state.turns.find(turn => turn.id === "turn-9")?.completed).toBe(true);
  // A runtime error on a streamed turn closes it as well.
  let errored = conversationReducer(initialConversationState, {
    type: "event", event: envelope({ kind: "message", name: "message.delta", payload: { turnId: "turn-10", role: "assistant", text: "正在处理这个请求…", agentId: "native" } } as UiEvent)
  });
  errored = conversationReducer(errored, { type: "event", event: envelope({ kind: "error", name: "error", payload: { code: "agent_run_failed", message: "任务执行失败", runId: "run-10", scope: "task", turnId: "turn-10" } } as UiEvent) });
  expect(errored.turns.find(turn => turn.id === "turn-10")?.completed).toBe(true);
});

test("历史里未完成的旧回合被后来的用户回合关闭，不会永久阻塞对话", () => {
  const msg = (turnId: string, role: "user" | "assistant", name: "message.delta" | "message.completed", text: string) =>
    envelope({ kind: "message", name, payload: { turnId, role, text, agentId: "native" } } as UiEvent);
  // 09-18 的语音回合：task 卡先到并已完成，回合只收到 message.delta，没有 message.completed。
  const task: AgentTask = { turnId: "turn-voice", taskId: "run-voice", title: "切换主题", subtitle: "", progress: 100, status: "completed", steps: [] };
  const replay: UiEvent[] = [
    msg("turn-u1", "user", "message.completed", "切成黑色主题"),
    envelope({ kind: "progress", name: "task.completed", payload: task } as UiEvent),
    msg("turn-voice", "assistant", "message.delta", "已经切成黑色主题了，界面已生效。"),
    msg("turn-u2", "user", "message.completed", "现在持仓多少"),
    msg("turn-a2", "assistant", "message.completed", "当前 2 手多单")
  ];
  const state = replay.reduce((current, event) => conversationReducer(current, { type: "event", event }), initialConversationState);
  expect(state.turns.map(turn => turn.id)).toEqual(["turn-u1", "turn-voice", "turn-u2", "turn-a2"]);
  expect(state.turns.find(turn => turn.id === "turn-voice")?.completed).toBe(true);
  expect(state.turns.some(turn => turn.role === "assistant" && turn.completed === false)).toBe(false);

  // 还在streaming的最后一条回合不受影响：没有后续用户回合就仍然是未完成。
  const streaming = conversationReducer(state, { type: "event", event: msg("turn-a3", "assistant", "message.delta", "正在查…") });
  expect(streaming.turns.at(-1)).toMatchObject({ id: "turn-a3", completed: false });
});

test("续句合并：被标记的半句 user 消息和它的已停止收尾一起隐藏，数据保留", () => {
  const msg = (turnId: string, role: "user" | "assistant", name: "message.delta" | "message.completed", text: string, extra: Record<string, unknown> = {}) =>
    envelope({ kind: "message", name, payload: { turnId, role, text, agentId: "native", ...extra } } as unknown as UiEvent);
  const replay: UiEvent[] = [
    msg("run-1-user", "user", "message.completed", "帮我看看", { origin: "voice" }),
    msg("stream-1", "assistant", "message.delta", "正在处理这个请求…"),
    envelope({ kind: "message", name: "message.superseded", payload: { turnId: "run-1-user", supersededBy: "帮我看看沪金的行情走势" } } as unknown as UiEvent),
    // 取消的 run 的收尾可能晚到：位置在被并走的那轮里，也要隐藏。
    msg("stream-1", "assistant", "message.completed", "已停止。", { ended: "cancelled" }),
    msg("run-2-user", "user", "message.completed", "帮我看看沪金的行情走势", { origin: "voice" }),
    msg("stream-2", "assistant", "message.completed", "沪金短线偏强。")
  ];
  const state = replay.reduce((current, event) => conversationReducer(current, { type: "event", event }), initialConversationState);
  expect(state.turns.map(turn => turn.id)).toEqual(["run-1-user", "stream-1", "run-2-user", "stream-2"]);
  const hidden = state.turns.filter(turn => turn.hidden).map(turn => turn.id);
  expect(hidden).toEqual(["run-1-user", "stream-1"]); // 半句问题与其收尾隐藏
  expect(state.turns.find(turn => turn.id === "stream-1")).toMatchObject({ ended: "cancelled", hidden: true });
  expect(state.turns.find(turn => turn.id === "run-2-user")?.hidden).toBeUndefined(); // 合并句正常显示
  // superseded 事件先于 user turn 到达（历史回放乱序）：turn 出现时补标。
  const early = conversationReducer(initialConversationState, {
    type: "event", event: envelope({ kind: "message", name: "message.superseded", payload: { turnId: "run-x-user", supersededBy: "合并句" } } as unknown as UiEvent)
  });
  const late = conversationReducer(early, { type: "event", event: msg("run-x-user", "user", "message.completed", "半句", { origin: "voice" }) });
  expect(late.turns[0]).toMatchObject({ id: "run-x-user", superseded: true, hidden: true });
});

test("对象级 AI: a user message asked from an object's AI menu keeps its focus label", () => {
  const asked = envelope({
    kind: "message",
    name: "message.completed",
    payload: { turnId: "focus-turn", role: "user", text: "这个策略为什么没开仓", focus: { kind: "strategy_instance", label: "双均线 · 螺纹2701" } }
  } as UiEvent);
  const state = conversationReducer(initialConversationState, { type: "event", event: asked });
  expect(state.turns[0].focus).toEqual({ kind: "strategy_instance", label: "双均线 · 螺纹2701" });
});

test("#457: same-day reminders of two accounts stay two turns; a repeat of one replaces itself", () => {
  const reminder = (turnId: string, text: string) => envelope({
    kind: "message",
    name: "message.completed",
    payload: { turnId, role: "assistant", text, agentId: "native" }
  } as UiEvent);
  let state = conversationReducer(initialConversationState, {
    type: "event",
    event: reminder("reminder-market_close_day-tqkq-sim:beta-2026-10-06", "账户乙：收盘提醒：4 分钟后日盘收盘，沪金2612 多 2 手未平。")
  });
  state = conversationReducer(state, {
    type: "event",
    event: reminder("reminder-market_close_day-tqkq-sim:default-2026-10-06", "账户甲：收盘提醒：4 分钟后日盘收盘，沪金2612 多 2 手未平。")
  });
  // 账户维度进 turnId：两条互不顶掉。
  expect(state.turns.map(turn => [turn.id, turn.text])).toEqual([
    ["reminder-market_close_day-tqkq-sim:beta-2026-10-06", "账户乙：收盘提醒：4 分钟后日盘收盘，沪金2612 多 2 手未平。"],
    ["reminder-market_close_day-tqkq-sim:default-2026-10-06", "账户甲：收盘提醒：4 分钟后日盘收盘，沪金2612 多 2 手未平。"],
  ]);
  // 同一账户同一天重发（Runtime 已幂等，这里防回归）只替换自己那条。
  const repeated = conversationReducer(state, {
    type: "event",
    event: reminder("reminder-market_close_day-tqkq-sim:beta-2026-10-06", "账户乙：收盘提醒：4 分钟后日盘收盘，沪金2612 多 2 手未平。")
  });
  expect(repeated.turns).toHaveLength(2);
});

// 结构化卡片与丢卡修复 (2026-10-04).
const assistantMessage = (turnId: string, text: string, name: "message.delta" | "message.completed" = "message.completed") => envelope({
  kind: "message", name, payload: { turnId, role: "assistant", text, agentId: "native" }
} as UiEvent);
const judgmentEvent = (status: "pending" | "ready", turnId = "turn-j") => envelope({
  kind: "artifact", name: "card.research_judgment",
  payload: {
    turnId, cardId: "judgment-SHFE.rb-2026-10-04", symbol: "SHFE.rb2610", productName: "螺纹钢", status,
    ...(status === "ready" ? { stance: "偏多", confidence: "中", headline: "库存连续下降", facts: [], invalidation: [] } : {})
  }
} as UiEvent);
const executionEvent = (status: "submitted" | "unknown") => envelope({
  kind: "artifact", name: "card.execution",
  payload: {
    turnId: "turn-x", cardId: "execution-AIT123", source: "ai", sourceLabel: "AI 自主", orderKind: "order",
    symbol: "SHFE.rb2610", name: "螺纹钢2610", sideLabel: "买开", volume: 1, price: "3200", status,
    statusLabel: status === "unknown" ? "结果未知" : "已报", updatedAt: "2026-10-04T01:30:00Z"
  }
} as UiEvent);

test("a card whose turn does not exist opens an assistant turn; the later message merges into it without blocking", () => {
  let state = conversationReducer(initialConversationState, { type: "event", event: judgmentEvent("pending") });
  expect(state.turns).toHaveLength(1);
  expect(state.turns[0]).toMatchObject({ id: "turn-j", role: "assistant", text: "" });
  // Not "streaming": the fallback turn never blocks sending (only completed === false does).
  expect(state.turns[0].completed).not.toBe(false);
  expect(state.turns[0].cards.map(card => card.type)).toEqual(["research-judgment"]);
  state = conversationReducer(state, { type: "event", event: assistantMessage("turn-j", "螺纹偏多看待", "message.delta") });
  state = conversationReducer(state, { type: "event", event: assistantMessage("turn-j", "螺纹偏多看待，回踩可分批。") });
  expect(state.turns).toHaveLength(1);
  expect(state.turns[0]).toMatchObject({ text: "螺纹偏多看待，回踩可分批。", completed: true });
  expect(state.turns[0].cards).toHaveLength(1);
});

test("#丢卡: a proposal.trade under a turnId with no turn yet stays visible (目标持仓第二段确认卡)", () => {
  const proposal = {
    turnId: "turn-target-2", proposalId: "proposal-2", version: 1, symbol: "SHFE.rb2610", name: "螺纹钢", side: "sell_close",
    volume: 1, limitPrice: 3200, stopPrice: null, contractMultiplier: 10, updatedAt: "2026-10-04T01:30:00Z",
    environment: "simulation", status: "approval_required",
    riskCheck: { passed: true, limit: 0, estimatedLoss: 0, reason: "", contractMultiplier: 10, priceTick: "1" }
  };
  let state = conversationReducer(initialConversationState, { type: "event", event: assistantMessage("turn-target-1", "先平 1 手多单，再开空。") });
  state = conversationReducer(state, { type: "event", event: envelope({ kind: "approval", name: "proposal.trade", payload: proposal } as unknown as UiEvent) });
  expect(state.turns.map(turn => turn.id)).toEqual(["turn-target-1", "turn-target-2"]);
  expect(state.turns[1].cards[0]).toMatchObject({ id: "proposal-2", type: "trade" });
  state = conversationReducer(state, { type: "event", event: assistantMessage("turn-target-2", "第二段：卖开 1 手，确认吗？") });
  expect(state.turns).toHaveLength(2);
  expect(state.turns[1]).toMatchObject({ text: "第二段：卖开 1 手，确认吗？", completed: true });
  expect(state.turns[1].cards).toHaveLength(1);
});

test("the same cardId again replaces the card in place (判断 pending → ready, 执行 submitted → unknown)", () => {
  let state = conversationReducer(initialConversationState, { type: "event", event: judgmentEvent("pending") });
  state = conversationReducer(state, { type: "event", event: judgmentEvent("ready") });
  state = conversationReducer(state, { type: "event", event: executionEvent("submitted") });
  state = conversationReducer(state, { type: "event", event: executionEvent("unknown") });
  const cards = state.turns.flatMap(turn => turn.cards);
  expect(cards).toHaveLength(2);
  const judgment = cards.find(card => card.type === "research-judgment");
  const execution = cards.find(card => card.type === "execution");
  expect(judgment?.type === "research-judgment" && judgment.payload.status).toBe("ready");
  expect(execution?.type === "execution" && execution.payload.statusLabel).toBe("结果未知");
});

test("a stop card republished by the tracker replaces in place: 已挂保护 → 已撤销 without a refresh", () => {
  // 2026-10-08: the protection auto-cancelled when the position closed; the Runtime republishes
  // the same cardId with the row's state, and the conversation shows the new status immediately.
  const stopCard = (status: "armed" | "unfilled") => envelope({
    kind: "artifact", name: "card.execution",
    payload: {
      turnId: "turn-stop", cardId: "execution-stop-ST-1", source: "ai", sourceLabel: "AI 自主", orderKind: "stop",
      symbol: "SHFE.rb2701", name: "螺纹钢2701", sideLabel: "保护多单", volume: 1, price: "止损 3060 / 止盈 3090",
      status, statusLabel: status === "armed" ? "已挂保护" : "已撤销",
      detail: status === "armed" ? "触发后按设定价格方式发平仓单；持仓平掉或撤销后保护失效。" : "持仓已平，保护自动撤销。",
      updatedAt: "2026-10-08T01:30:00Z"
    }
  } as UiEvent);
  let state = conversationReducer(initialConversationState, { type: "event", event: stopCard("armed") });
  const notice = envelope({
    kind: "message", name: "message.completed",
    payload: { turnId: "turn-notice", role: "assistant", agentId: "native", text: "SHFE.rb2701 多 持仓已平，保护自动撤销" }
  } as UiEvent);
  state = conversationReducer(state, { type: "event", event: stopCard("unfilled") });
  state = conversationReducer(state, { type: "event", event: notice });
  const cards = state.turns.flatMap(turn => turn.cards);
  expect(cards).toHaveLength(1);  // the republish did not open a second card
  expect(cards[0]).toMatchObject({ id: "execution-stop-ST-1", type: "execution" });
  expect(cards[0].payload).toMatchObject({ statusLabel: "已撤销", detail: "持仓已平，保护自动撤销。" });
});

test("history replay keeps one turn whichever comes first, the message or the card", () => {
  const reminder = envelope({
    kind: "artifact", name: "card.reminder",
    payload: { turnId: "pricealert-a1", cardId: "reminder-pricealert-a1", kind: "price_alert", title: "到价提醒",
      summary: "螺纹钢2610 涨到 3200", at: "2026-10-04T01:30:00Z" }
  } as UiEvent);
  const text = assistantMessage("pricealert-a1", "到价提醒：螺纹钢2610 涨到 3200。");
  for (const order of [[text, reminder], [reminder, text]]) {
    const state = order.reduce((current, event) => conversationReducer(current, { type: "event", event }), initialConversationState);
    expect(state.turns).toHaveLength(1);
    expect(state.turns[0]).toMatchObject({ id: "pricealert-a1", text: "到价提醒：螺纹钢2610 涨到 3200。" });
    expect(state.turns[0].cards.map(card => card.type)).toEqual(["reminder"]);
    // Replaying the whole stream once more changes nothing.
    const replayed = order.reduce((current, event) => conversationReducer(current, { type: "event", event }), state);
    expect(replayed.turns).toHaveLength(1);
    expect(replayed.turns[0].cards).toHaveLength(1);
  }
});

// 回答卡片化 (2026-10-14): one quote / account card per turn by cardId; 回答卡 and 数据卡 replace in place.
const quoteEvent = (price: number, cardId?: string, peers?: { symbol: string; name: string; price: number; changePercent: number }[]) => envelope({
  kind: "artifact", name: "card.market",
  payload: {
    turnId: "turn-quote", symbol: "SHFE.au2612", name: "沪金", price, changePercent: 0.5, updatedAt: "10:00",
    trend: "up", summary: "", priceCurve: [1, 2], ...(cardId ? { cardId } : {}), ...(peers ? { peers } : {})
  }
} as UiEvent);

test("a quote card with a cardId is replaced in place by the turn's next read (peers ride on it)", () => {
  let state = conversationReducer(initialConversationState, { type: "event", event: assistantMessage("turn-quote", "沪金和沪银") });
  state = conversationReducer(state, { type: "event", event: quoteEvent(612, "market-turn-quote") });
  state = conversationReducer(state, { type: "event", event: quoteEvent(613, "market-turn-quote",
    [{ symbol: "SHFE.ag2612", name: "沪银", price: 7800, changePercent: -0.3 }]) });
  const cards = state.turns[0].cards;
  expect(cards).toHaveLength(1);
  expect(cards[0]).toMatchObject({ id: "market-turn-quote", type: "market" });
  expect(cards[0].payload).toMatchObject({ price: 613, peers: [{ symbol: "SHFE.ag2612" }] });
});

test("quote and account events without a cardId still become one card each (older Runtimes, history)", () => {
  let state = conversationReducer(initialConversationState, { type: "event", event: assistantMessage("turn-quote", "两次报价") });
  state = conversationReducer(state, { type: "event", event: quoteEvent(612) });
  state = conversationReducer(state, { type: "event", event: quoteEvent(613) });
  expect(state.turns[0].cards.map(card => card.type)).toEqual(["market", "market"]);
  expect(state.turns[0].cards.every(card => card.id.startsWith("market-"))).toBe(true);
});

test("an account card with a cardId replaces itself and still updates the latest account", () => {
  const account = (equity: number, cardId?: string) => {
    const payload: AccountSummary = { turnId: "turn-acc", accountName: "模拟", updatedAt: "10:00", equity, todayPnl: 0, marginRatio: 0,
      positionCount: 0, riskBudgetUsed: 0, status: "normal", insight: "", equityCurve: [], ...(cardId ? { cardId } : {}) };
    return envelope({ kind: "artifact", name: "card.account", payload } as UiEvent);
  };
  let state = conversationReducer(initialConversationState, { type: "event", event: account(100, "account-turn-acc") });
  state = conversationReducer(state, { type: "event", event: account(120, "account-turn-acc") });
  expect(state.turns).toHaveLength(1);
  expect(state.turns[0].cards).toHaveLength(1);
  expect(state.turns[0].cards[0].payload).toMatchObject({ equity: 120 });
  expect(state.latestAccount?.equity).toBe(120);
  state = conversationReducer(state, { type: "event", event: account(130) });
  state = conversationReducer(state, { type: "event", event: account(140) });
  expect(state.turns[0].cards).toHaveLength(3);
});

test("回答卡 and 数据卡: same cardId again replaces the card in place; a card ahead of its turn opens it", () => {
  const answer = (conclusion: string) => envelope({
    kind: "artifact", name: "card.answer",
    payload: { turnId: "turn-ans", cardId: "answer-turn-ans", title: "沪金咋样", conclusion, basis: { at: "2026-10-14T02:00:00Z", sources: ["行情"] } }
  } as UiEvent);
  const data = (rows: number) => {
    const payload: DataCard = { turnId: "turn-ans", cardId: "data-news-turn-ans", kind: "news", title: "快讯",
      rows: Array.from({ length: rows }, (_, index) => ({ primary: `快讯 ${index}` })), basis: { at: "2026-10-14T02:00:00Z", sources: [] } };
    return envelope({ kind: "artifact", name: "card.data", payload } as UiEvent);
  };
  let state = conversationReducer(initialConversationState, { type: "event", event: answer("偏强") });
  expect(state.turns).toHaveLength(1);
  state = conversationReducer(state, { type: "event", event: data(1) });
  state = conversationReducer(state, { type: "event", event: assistantMessage("turn-ans", "沪金偏强。") });
  state = conversationReducer(state, { type: "event", event: answer("偏强，回踩再看") });
  state = conversationReducer(state, { type: "event", event: data(2) });
  expect(state.turns).toHaveLength(1);
  expect(state.turns[0].cards.map(card => card.type)).toEqual(["answer", "data"]);
  expect(state.turns[0].cards[0].payload).toMatchObject({ conclusion: "偏强，回踩再看" });
  expect(state.turns[0].cards[1].payload).toMatchObject({ rows: [{ primary: "快讯 0" }, { primary: "快讯 1" }] });
});

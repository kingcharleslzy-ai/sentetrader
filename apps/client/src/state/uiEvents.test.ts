// @vitest-environment node
import fixtures from "../../../../packages/ui-events/fixtures/events.v1.json";
import { parseUiEvent } from "@ai-trader/ui-events";

test("shared v1 fixtures satisfy the TypeScript runtime contract", () => {
  for (const fixture of fixtures) expect(parseUiEvent(fixture).ok).toBe(true);
});

test("unknown and malformed events become warnings instead of exceptions", () => {
  const unknown = { ...fixtures[0], name: "future.card" };
  const malformed = { ...fixtures[0], payload: { role: "assistant" } };
  expect(parseUiEvent(unknown)).toMatchObject({ ok: false, warning: { code: "unknown_event" } });
  expect(parseUiEvent(malformed)).toMatchObject({ ok: false, warning: { code: "invalid_event" } });
});

test("#850: a feed.market since answer carries its whole proof or does not validate", () => {
  const market = fixtures.find(fixture => fixture.name === "feed.market")!;
  const answer = (proof: Record<string, unknown>) => ({ ...market, payload: { ...market.payload, ...proof } });
  const window = { first: 1_791_800_000_000_000_000, count: 10, digest: "17427239" };
  expect(parseUiEvent(answer({ barsAfter: 1_791_800_420_000_000_000, barsWindow: window })).ok).toBe(true);
  expect(parseUiEvent(answer({ barsAfter: 1_791_800_420_000_000_000 })).ok).toBe(false);
  expect(parseUiEvent(answer({ barsAfter: 1_791_800_420_000_000_000, barsWindow: { ...window, count: 0 } })).ok).toBe(false);
  expect(parseUiEvent(answer({ barsWindow: window })).ok).toBe(false);
});

test("nested Codex and task payloads are fully validated", () => {
  const invalidTask = {
    ...fixtures[0],
    kind: "progress",
    name: "task.progress",
    payload: {
      turnId: "turn", taskId: "task", title: "任务", subtitle: "",
      progress: 101, status: "running",
      steps: [{ id: "one", label: "步骤", status: "unknown", detail: "" }]
    }
  };
  expect(parseUiEvent(invalidTask)).toMatchObject({ ok: false, warning: { code: "invalid_event" } });
});

test("phase 1.2 account cards remain readable after freshness fields are added", () => {
  const historical = {
    ...fixtures[0], kind: "artifact", name: "card.account",
    payload: {
      turnId: "legacy-turn", accountName: "历史假账户", updatedAt: "14:26",
      equity: 100, todayPnl: 3, marginRatio: 1, positionCount: 0,
      riskBudgetUsed: 0, status: "normal", insight: "历史卡片", equityCurve: [100]
    }
  };
  expect(parseUiEvent(historical)).toMatchObject({ ok: true });
});

test("a spread card event parses with or without the empty spread fields (线上 09-29：缺字段的卡整张被丢)", () => {
  const leg = { name: "螺纹2701", side: "buy_open", offset: "OPEN", positionBucket: null, volume: 1, limitPrice: 3123,
    contractMultiplier: 10, riskCheck: { passed: true, limit: 0, estimatedLoss: 0, reason: "", contractMultiplier: 10, priceTick: "1" } };
  // Exactly what 21e727e sent: currentSpread present, entrySpread / exitReason left out.
  const payload = { turnId: "strategy-card-1", proposalId: "spread-1", version: 1, environment: "simulation", reason: "价差",
    spread: { kind: "difference" }, spreadWords: "SHFE.rb2701 − SHFE.rb2705", legsWords: "买SHFE.rb2701 1 手、卖SHFE.rb2705 1 手",
    action: "open_long", units: 1, currentSpread: -14,
    orders: [{ ...leg, symbol: "SHFE.rb2701" }, { ...leg, symbol: "SHFE.rb2705", name: "螺纹2705", side: "sell_open" }],
    totalOrders: 2, totalVolume: 2, riskCheck: { passed: true }, updatedAt: "2026-09-29T13:53:00Z",
    status: "approval_required", approvalId: "approval-1", strategySource: { strategyId: "s", name: "价差", attemptId: "a", barTime: "1" } };
  const event = { ...fixtures[0], kind: "approval", name: "proposal.spread", payload };
  expect(parseUiEvent(event).ok).toBe(true);
  expect(parseUiEvent({ ...event, payload: { ...payload, entrySpread: null, exitReason: null } }).ok).toBe(true);
});

test("a spread card event parses with the legs exactly as the Runtime builds them (线上 09-29：开仓腿缺 positionBucket 整张卡被丢)", () => {
  // One leg of the 22:06 production card, verbatim apart from the long snapshot: no positionBucket key.
  const leg = { symbol: "SHFE.rb2701", executionEligibleAtCreation: true, name: "螺纹2701", side: "buy_open", offset: "OPEN",
    volume: 1, limitPrice: 3116.0, contractMultiplier: 10.0, priceTick: "1.0", priceIntent: "aggressive", reason: "价差",
    quoteDatetime: "2026-09-29 22:06:46.000000", marketSnapshot: { price: 3116.0 },
    riskCheck: { passed: true, limit: 0.0, estimatedLoss: 0.0, contractMultiplier: 10.0, priceTick: "1.0", reason: "模拟盘未设置账户交易限制。",
      policyVersion: 0, checks: { lossEstimateAvailable: false, unlimitedPerTradeLoss: true, symbolAllowed: true, perTradeLoss: true, dailyLoss: true, openingTime: true } } };
  const payload = { turnId: "strategy-card-8f968b2986e0", proposalId: "54e55a9c", version: 1, environment: "simulation", reason: "价差",
    spread: { kind: "difference", legs: [] }, spreadWords: "SHFE.rb2701 − SHFE.rb2705", legsWords: "买SHFE.rb2701 1 手、卖SHFE.rb2705 1 手",
    action: "open_long", units: 1, orders: [leg, { ...leg, symbol: "SHFE.rb2705", name: "螺纹2705", side: "sell_open", limitPrice: 3128.0 }],
    totalOrders: 2, totalVolume: 2, riskCheck: { passed: true, reason: "两腿同时" }, updatedAt: "2026-09-29T14:06:46.605050+00:00",
    status: "approval_required", currentSpread: -13.0, entrySpread: null, exitReason: null,
    strategySource: { strategyId: "1c6094f4", name: "价差复测", attemptId: "34f8f60f", barTime: "1790690700000000000" }, approvalId: "989e774c" };
  expect(parseUiEvent({ ...fixtures[0], kind: "approval", name: "proposal.spread", payload }).ok).toBe(true);
});

test("#932 F2: a replayed user message from the strategy editor's 跟 AI 说 (focus strategy_edit) is kept, not dropped", async () => {
  const { conversationReducer, initialConversationState } = await import("./conversation");
  const message = fixtures.find(fixture => fixture.name === "message.completed")!;
  const replayed = { ...message, payload: { turnId: "edit-turn", role: "user", text: "把止损放宽一点再测一次",
    focus: { kind: "strategy_edit", label: "1分钟双均线策略" } } };
  const parsed = parseUiEvent(replayed);
  expect(parsed.ok).toBe(true);
  if (!parsed.ok) return;
  const state = conversationReducer(initialConversationState, { type: "event", event: parsed.event });
  expect(state.turns[0]).toMatchObject({ id: "edit-turn", focus: { kind: "strategy_edit", label: "1分钟双均线策略" } });
});

import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReminderCard as Reminder, RemainingProtection, TradeProposal } from "@ai-trader/ui-events";
import { ReminderCard } from "./ReminderCard";
import { remainingResult, remainingStep } from "./ProtectionSteps";
import { TradeProposalCard } from "./TradeProposalCard";

// 合并卡 (2026-10-06): a close card with the remaining position's new stop reads as two steps, each with
// its own result; a failed second step says the old stop still stands and offers 按现价重新设 / 问 AI.
const plan: RemainingProtection = {
  direction: "short", remainingVolume: 1,
  stopLoss: { action: "set", price: "81420", atCost: true }, takeProfit: { action: "keep", price: "79800" },
  original: { stopLoss: "82100", takeProfit: "79800" }, cost: "81420", status: "pending",
};
const card: TradeProposal = {
  turnId: "t", proposalId: "p-1", version: 1, symbol: "SHFE.cu2612", name: "沪铜2612", side: "buy_close", offset: "CLOSETODAY",
  positionBucket: "today", volume: 1, limitPrice: 81380, contractMultiplier: 5, updatedAt: "2026-10-09T01:00:00Z",
  environment: "simulation", status: "approval_required", approvalId: "a-1",
  riskCheck: { passed: true, limit: 0, estimatedLoss: 0, reason: "仅减少现有持仓", contractMultiplier: 5, priceTick: "10" },
  remainingProtection: plan,
};
const noop = () => undefined;

test("a pending merged card shows both steps with the old levels and one 确认", () => {
  render(<TradeProposalCard proposal={card} busy={false} onAdjustRisk={noop} onConfirm={noop} onReject={noop} />);
  const steps = within(screen.getByTestId("merged-close-steps")).getAllByTestId("card-step");
  expect(steps[0].textContent).toContain("平空 1 手 @ 81,380 限价");
  expect(steps[1].textContent).toContain("剩下 1 手止损 → 81,420（成本）");
  expect(steps[1].textContent).toContain("原止损 82,100，止盈 79,800 不变");
  expect(screen.getByTestId("confirm-trade")).toBeEnabled();
});

test("a finished merged card says both steps are done", () => {
  const done: TradeProposal = { ...card, status: "candidate_recorded", remainingProtection: { ...plan, status: "done",
    result: { remainingVolume: 1, stopLoss: "81420", takeProfit: "79800", previousStopLoss: "82100", filled: 1 } } };
  render(<TradeProposalCard proposal={done} busy={false} onAdjustRisk={noop} onConfirm={noop} onReject={noop} />);
  const block = screen.getByTestId("merged-close-steps");
  expect(within(block).getAllByTestId("card-step")[1].textContent).toContain("原止损 82,100 已撤");
  expect(within(block).getByRole("status").textContent).toBe("两步都完成：平掉 1 手，剩下 1 手止损在 81,420（成本）");
});

test("a failed second step keeps the old stop and offers the two asks", () => {
  const askAi = vi.fn();
  const failed: TradeProposal = { ...card, status: "candidate_recorded", remainingProtection: { ...plan, status: "failed",
    reason: "现价已穿过 81,420", result: { filled: 1, reason: "现价已穿过 81,420" },
    actions: [{ kind: "ask", label: "按现价重新设", prompt: "按现价重新设止损" }, { kind: "ask", label: "问 AI", prompt: "怎么办" }] } };
  render(<TradeProposalCard proposal={failed} busy={false} onAdjustRisk={noop} onConfirm={noop} onReject={noop} onAsk={askAi} />);
  expect(screen.getByText("部分完成")).toBeVisible();
  expect(screen.getByText("没改成：现价已穿过 81,420")).toBeVisible();
  expect(screen.getByText("平仓已完成；本次保护修改未执行")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "问 AI · 按现价重新设" }));
  expect(askAi).toHaveBeenCalledWith("按现价重新设止损");
});

// #528: 今仓 / 昨仓 kept at their own take-profits. Card time reads the pre-close snapshot (今 2 / 昨 2), never
// as a split of the remaining 3 lots; 已撤 / 未动 come only from what the run actually left on each bucket.
test("a keep across buckets with different prices names each bucket's own level", () => {
  const mixed: RemainingProtection = { direction: "long", remainingVolume: 3,
    stopLoss: { action: "set", price: "822.5" }, takeProfit: { action: "keep", price: null },
    original: { stopLoss: "822", takeProfit: null }, status: "pending",
    perBucketOriginal: [{ positionBucket: "historical", volume: 2, stopLoss: "822", takeProfit: "830" },
      { positionBucket: "today", volume: 2, stopLoss: "822", takeProfit: "825" }] };
  const pending = remainingStep(mixed);
  expect(pending.title).toBe("剩下 3 手止损 → 822.5");
  expect(pending.detail).toBe("原止损 822；止盈不变：今仓 825、昨仓 830");

  const done = remainingStep({ ...mixed, status: "done", result: { remainingVolume: 3, filled: 1,
    stopLoss: "822.5", takeProfit: null, previousStopLoss: "822", previousTakeProfit: null, perBucketProtection: [
      { positionBucket: "today", volume: 1, stopLoss: "822.5", takeProfit: "825", previousStopLoss: "822", previousTakeProfit: "825" },
      { positionBucket: "historical", volume: 2, stopLoss: "822.5", takeProfit: "830", previousStopLoss: "822", previousTakeProfit: "830" }] } });
  expect(done.title).toBe("剩下 3 手止损 822.5");
  expect(done.detail).toBe("原止损 822 已撤；止盈未动：今仓 1 手 825、昨仓 2 手 830");
  expect(remainingResult({ ...mixed, status: "done", result: { remainingVolume: 3, filled: 1, stopLoss: "822.5" } }, 1)?.text)
    .toBe("两步都完成：平掉 1 手，剩下 3 手止损在 822.5");

  // Nothing replaced on a bucket that had no stop: 已撤 names only the bucket that really had one.
  const partial = remainingStep({ ...mixed, status: "done", result: { remainingVolume: 3, stopLoss: "822.5", previousStopLoss: null,
    perBucketProtection: [{ positionBucket: "today", volume: 1, stopLoss: "822.5", takeProfit: "825", previousStopLoss: "822", previousTakeProfit: "825" },
      { positionBucket: "historical", volume: 2, stopLoss: "822.5", takeProfit: "830", previousStopLoss: null, previousTakeProfit: "830" }] } });
  expect(partial.detail).toBe("原止损 今仓 822 已撤；止盈未动：今仓 1 手 825、昨仓 2 手 830");

  // Same-bucket prices that can't be stated (null) are not "no stop": the old-price clause is left out.
  const unknown: RemainingProtection = { ...mixed, original: { stopLoss: null, takeProfit: null },
    perBucketOriginal: mixed.perBucketOriginal!.map(row => ({ ...row, stopLoss: null })) };
  expect(remainingStep(unknown).detail).toBe("止盈不变：今仓 825、昨仓 830");
  expect(remainingStep({ ...unknown, status: "done", result: { remainingVolume: 3, stopLoss: "822.5", previousStopLoss: null,
    replacedStopIds: ["s-1"], perBucketProtection: [
      { positionBucket: "today", volume: 1, stopLoss: "822.5", takeProfit: "825", previousStopLoss: null, previousTakeProfit: "825" },
      { positionBucket: "historical", volume: 2, stopLoss: "822.5", takeProfit: "830", previousStopLoss: null, previousTakeProfit: "830" }],
  } as RemainingProtection["result"] }).detail).toBe("止盈未动：今仓 1 手 825、昨仓 2 手 830");

  // A remove with no actual previous stop and nothing replaced says what stands now, never 已撤.
  const removed = remainingStep({ ...unknown, stopLoss: { action: "remove" }, status: "done", result: { remainingVolume: 3,
    stopLoss: null, previousStopLoss: null, replacedStopIds: [], perBucketProtection: [
      { positionBucket: "today", volume: 1, stopLoss: null, takeProfit: null, previousStopLoss: null, previousTakeProfit: null },
      { positionBucket: "historical", volume: 2, stopLoss: null, takeProfit: null, previousStopLoss: null, previousTakeProfit: null }],
  } as RemainingProtection["result"] });
  expect(removed.title).toBe("剩下 3 手未设止损");
  expect(removed.detail).toBe("原来没有挂");

  const failed = { ...mixed, status: "failed" as const, reason: "现价已穿过 822.5", result: { filled: 1, reason: "现价已穿过 822.5" } };
  expect(remainingStep(failed).detail).toBe("没改成：现价已穿过 822.5");
  expect(remainingResult(failed, 1)?.text).toBe("平仓已完成；本次保护修改未执行");
});

test("保本提醒 offers 移到成本 / 不用 through its own handler and shows the outcome", () => {
  const breakeven = vi.fn();
  const reminder: Reminder = {
    turnId: "be-1", cardId: "reminder-be-1", kind: "breakeven", title: "保本提醒 · 螺纹2701",
    summary: "浮盈已超过止损距离：成本 3,145，现价 3,195，止损 3,100。", at: "2026-10-09T02:47:00Z", symbol: "SHFE.rb2701",
    step: { title: "两笔止损 3,100 → 3,145（成本）", detail: "止盈 3,230 不变" },
    breakeven: { id: "be-1", state: "pending", cost: "3145", direction: "long" },
    actions: [{ kind: "breakeven", label: "移到成本", breakevenId: "be-1", choice: "move" },
      { kind: "breakeven", label: "不用", breakevenId: "be-1", choice: "decline" }],
  };
  const { rerender } = render(<ReminderCard card={reminder} id={reminder.cardId} handlers={{ breakeven }} />);
  expect(screen.getByTestId("breakeven-step").textContent).toContain("两笔止损 3,100 → 3,145（成本）");
  fireEvent.click(screen.getByTestId("breakeven-move"));
  expect(breakeven).toHaveBeenCalledWith("be-1", "move");
  rerender(<ReminderCard card={{ ...reminder, actions: [], breakeven: { ...reminder.breakeven!, state: "moved", result: "两笔止损已上移到 3,145" } }}
    id={reminder.cardId} handlers={{ breakeven }} />);
  expect(screen.getByRole("status").textContent).toBe("两笔止损已上移到 3,145");
  expect(screen.queryByTestId("breakeven-move")).toBeNull();
});

test("a position that also has a strategy's stop says so on both cards", () => {
  render(<TradeProposalCard proposal={{ ...card, remainingProtection: { ...plan, strategyNote: "策略「螺纹趋势」的保护不受影响，这笔持仓会同时有两套止损" } }}
    busy={false} onAdjustRisk={noop} onConfirm={noop} onReject={noop} />);
  expect(screen.getByTestId("strategy-protection-note").textContent).toContain("策略「螺纹趋势」的保护不受影响");
});

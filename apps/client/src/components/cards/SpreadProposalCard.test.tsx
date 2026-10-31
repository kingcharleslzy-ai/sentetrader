import { fireEvent, render, screen } from "@testing-library/react";
import type { SpreadLegOrder, SpreadProposal } from "@ai-trader/ui-events";
import { SpreadProposalCard, spreadValueText } from "./SpreadProposalCard";
import { otherAccountNote } from "./confirmGate";

const riskCheck = { passed: true, limit: 0, estimatedLoss: 0, reason: "开仓", contractMultiplier: 10, priceTick: "1" };
const legs: SpreadLegOrder[] = [
  { symbol: "SHFE.rb2701", name: "螺纹2701", side: "buy_open", offset: "OPEN", positionBucket: null,
    volume: 1, limitPrice: 815, contractMultiplier: 10, riskCheck },
  { symbol: "SHFE.rb2705", name: "螺纹2705", side: "sell_open", offset: "OPEN", positionBucket: null,
    volume: 1, limitPrice: 810, contractMultiplier: 10, riskCheck },
];
const proposal: SpreadProposal = {
  turnId: "turn-1", proposalId: "spread-1", version: 1, environment: "simulation", reason: "价差回归",
  spread: { kind: "difference", legs: [{ symbol: "SHFE.rb2701", lots: 1 }, { symbol: "SHFE.rb2705", lots: 1 }], stopLoss: 5 },
  spreadWords: "SHFE.rb2701 − SHFE.rb2705", legsWords: "买螺纹2701 1 手、卖螺纹2705 1 手",
  action: "open_long", units: 1, currentSpread: 4.5, entrySpread: null, exitReason: null,
  orders: legs, totalOrders: 2, totalVolume: 2, riskCheck: { passed: true },
  updatedAt: "2026-09-29T12:00:00Z", status: "approval_required", approvalId: null,
  strategySource: { strategyId: "strategy-1", name: "价差回归测试" },
};

test("spread values print like the Runtime's spread_value_text", () => {
  expect(spreadValueText(4.5)).toBe("4.5");
  expect(spreadValueText(0.1 + 0.2)).toBe("0.3");
  expect(spreadValueText(-0)).toBe("0");
  expect(spreadValueText(null)).toBe("—");
  expect(spreadValueText(Number.NaN)).toBe("—");
});

test("lists both legs with the spread facts, and only confirms with an approval id", () => {
  const onConfirm = vi.fn();
  const { rerender } = render(<SpreadProposalCard proposal={proposal} busy={false} onConfirm={onConfirm} onReject={() => undefined} />);
  const card = screen.getByTestId("spread-proposal-card");
  expect(card).toHaveTextContent("做多价差 1 份 · SHFE.rb2701 − SHFE.rb2705");
  expect(card).toHaveTextContent("一份构成");
  expect(card).toHaveTextContent("买螺纹2701 1 手、卖螺纹2705 1 手");
  expect(card).toHaveTextContent("当前价差");
  expect(card).toHaveTextContent("4.5");
  expect(card).toHaveTextContent("止损 5");
  expect(card).toHaveTextContent("两腿同时按对手价");
  const rows = screen.getAllByRole("row").slice(1);
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveTextContent("螺纹2701");
  expect(rows[0]).toHaveTextContent("买入开仓");
  expect(rows[1]).toHaveTextContent("卖出开仓");
  expect(screen.getByTestId("strategy-source-line")).toHaveTextContent("来自策略：价差回归测试");
  expect(screen.getByTestId("confirm-spread")).toBeDisabled();

  rerender(<SpreadProposalCard proposal={{ ...proposal, approvalId: "approval-1" }} busy={false}
                               onConfirm={onConfirm} onReject={() => undefined} />);
  fireEvent.click(screen.getByTestId("confirm-spread"));
  expect(onConfirm).toHaveBeenCalledOnce();
});

test("a confirmed card reports each leg's own state and never claims a send without facts", () => {
  const confirmed: SpreadProposal = {
    ...proposal, status: "candidate_recorded", approvalId: "approval-1", auditId: "audit-12345678",
    orders: legs.map((leg, index) => ({ ...leg, clientOrderId: `AIT-${index}` })),
    // One leg filled, one outcome unknown: the readback is per leg, never a blanket 已发送.
    simulationOrders: [
      { clientOrderId: "AIT-0", symbol: "SHFE.rb2701", displayStatus: "已成交", submitPhase: "CONFIRMED" },
      { clientOrderId: "AIT-1", symbol: "SHFE.rb2705", displayStatus: "结果未知", submitPhase: "UNKNOWN" },
    ],
  };
  const { unmount } = render(<SpreadProposalCard proposal={confirmed} busy={false} onConfirm={() => undefined} onReject={() => undefined} />);
  const rows = screen.getAllByRole("row").slice(1);
  expect(screen.getAllByRole("columnheader").map(cell => cell.textContent)).toEqual(["合约", "方向", "手数", "价格", "状态"]);
  expect(rows[0]).toHaveTextContent("已成交");
  expect(rows[1]).toHaveTextContent("结果未知");
  expect(screen.getByTestId("spread-execution")).toHaveTextContent("已确认，1 笔已发送、1 笔结果未知");
  expect(screen.getByTestId("spread-execution")).toHaveTextContent("系统不会自动重发");
  expect(screen.getByTestId("spread-proposal-card")).toHaveTextContent("结果待确认");
  expect(screen.queryByTestId("confirm-spread")).not.toBeInTheDocument();

  // Confirmation alone is not a send receipt: without the legs' facts the card only says 已确认.
  unmount();
  render(<SpreadProposalCard proposal={{ ...confirmed, simulationOrders: undefined }} busy={false}
                             onConfirm={() => undefined} onReject={() => undefined} />);
  expect(screen.getByTestId("spread-proposal-card")).toHaveTextContent("已确认");
  expect(screen.getByTestId("spread-execution")).toHaveTextContent("两腿执行中");
});

test("the settled card carries the attempt's conclusion line", () => {
  const settled: SpreadProposal = {
    ...proposal, status: "candidate_recorded", approvalId: "approval-1", auditId: "audit-12345678",
    orders: legs.map((leg, index) => ({ ...leg, clientOrderId: `AIT-${index}` })),
    simulationOrders: [
      { clientOrderId: "AIT-0", symbol: "SHFE.rb2701", displayStatus: "已成交", submitPhase: "CONFIRMED" },
      { clientOrderId: "AIT-1", symbol: "SHFE.rb2705", displayStatus: "已成交", submitPhase: "CONFIRMED" },
    ],
    executionConclusion: { code: "filled_chased", text: "已补单成交" },
  };
  const { unmount } = render(<SpreadProposalCard proposal={settled} busy={false} onConfirm={() => undefined} onReject={() => undefined} />);
  expect(screen.getByTestId("spread-conclusion")).toHaveTextContent("执行结论：已补单成交");

  // 未出结论的卡 (执行中) 不占这一行。
  unmount();
  render(<SpreadProposalCard proposal={{ ...settled, executionConclusion: null }} busy={false}
                             onConfirm={() => undefined} onReject={() => undefined} />);
  expect(screen.queryByTestId("spread-conclusion")).not.toBeInTheDocument();
});

test("a refused card says the Chinese reason instead of ordering", () => {
  render(<SpreadProposalCard
    proposal={{ ...proposal, approvalId: "approval-1",
                confirmBlocked: { code: "strategy_not_monitoring", reason: "策略已暂停或停止，这张价差卡没有发单。" } }}
    busy={false} onConfirm={() => undefined} onReject={() => undefined} />);
  expect(screen.getByTestId("spread-card-blocked")).toHaveTextContent("策略已暂停或停止，这张价差卡没有发单。");
  expect(screen.getByTestId("spread-card-blocked")).toHaveTextContent("未发单");
});

test("after a reload the live order feed still fills each leg's row (线上 09-29：一直“状态读取中”)", () => {
  const confirmed: SpreadProposal = {
    ...proposal, status: "candidate_recorded", approvalId: "approval-1",
    simulationOrders: [
      { clientOrderId: "AIT-0", symbol: "SHFE.rb2701", displayStatus: "已受理" },
      { clientOrderId: "AIT-1", symbol: "SHFE.rb2705", displayStatus: "已受理" },
    ],
  };
  // The live list after a reload: its contract sits under request.symbol, not at the top.
  const live = [
    { clientOrderId: "AIT-0", displayStatus: "已成交", request: { symbol: "SHFE.rb2701" } },
    { clientOrderId: "AIT-1", displayStatus: "已成交", request: { symbol: "SHFE.rb2705" } },
  ] as never;
  render(<SpreadProposalCard proposal={confirmed} orders={live} busy={false} onConfirm={() => undefined} onReject={() => undefined} />);
  const rows = screen.getAllByRole("row").slice(1);
  expect(rows[0]).toHaveTextContent("已成交");
  expect(rows[1]).toHaveTextContent("已成交");
  expect(screen.getByTestId("spread-proposal-card")).not.toHaveTextContent("状态读取中");
});

test("a strategy card for another account than the conversation's names its own account, by id (#1011)", () => {
  // The session is on tqkq-pool:b; the directory names both accounts.
  const names: Record<string, string> = { "tqkq-pool:a": "快期账户甲", "tqkq-pool:b": "快期账户乙" };
  const actions = { onCurrentAccount: (id?: string) => !id || id === "tqkq-pool:b", accountName: (id: string) => names[id] };
  // Two different accounts with the same label (both 快期模拟账户) still get the line, with the card account's name.
  expect(otherAccountNote("tqkq-pool:a", actions, "快期模拟账户")).toBe("下到快期账户甲（不是当前对话的账户）");
  expect(otherAccountNote("tqkq-pool:b", actions, "快期模拟账户")).toBeUndefined();   // the current account
  expect(otherAccountNote(undefined, actions, "快期模拟账户")).toBeUndefined();       // a card without an account
  expect(otherAccountNote("paper:x", actions, "本站模拟账户")).toBe("下到本站模拟账户（不是当前对话的账户）");  // not in the directory
  const { rerender } = render(<SpreadProposalCard proposal={{ ...proposal, accountId: "tqkq-pool:a", accountLabel: "快期模拟账户" }}
    busy={false} onConfirm={() => undefined} onReject={() => undefined}
    accountNote={otherAccountNote("tqkq-pool:a", actions, "快期模拟账户")} />);
  expect(screen.getByTestId("spread-proposal-card")).toHaveTextContent("下到快期账户甲（不是当前对话的账户）");
  rerender(<SpreadProposalCard proposal={{ ...proposal, accountId: "tqkq-pool:b", accountLabel: "快期模拟账户" }}
    busy={false} onConfirm={() => undefined} onReject={() => undefined}
    accountNote={otherAccountNote("tqkq-pool:b", actions, "快期模拟账户")} />);
  expect(screen.getByTestId("spread-proposal-card")).not.toHaveTextContent("不是当前对话的账户");
});

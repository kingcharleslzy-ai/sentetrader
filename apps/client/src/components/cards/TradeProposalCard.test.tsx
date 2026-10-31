import { render, screen } from "@testing-library/react";
import type { TradeProposal } from "@ai-trader/ui-events";
import type { SimulationOrder } from "../../api";
import { TradeProposalCard } from "./TradeProposalCard";

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
  riskCheck: { passed: false, limit: 2000, estimatedLoss: 4500, reason: "风险超限", contractMultiplier: 1000, priceTick: "0.02" }
};

test("a closed card says 已过期 / 已作废 / 已放弃, and the drop button reads 放弃 like the batch card (#932 P2)", () => {
  const open = { ...proposal, status: "approval_required" as const, approvalId: "a", riskCheck: { ...proposal.riskCheck, passed: true } };
  const props = { busy: false, onAdjustRisk: () => undefined, onConfirm: () => undefined, onReject: () => undefined };
  const { rerender } = render(<TradeProposalCard proposal={open} {...props} />);
  expect(screen.getByRole("button", { name: "放弃" })).toHaveClass("danger");
  expect(screen.queryByRole("button", { name: "取消" })).toBeNull();
  for (const [closedReason, label] of [["expired", "已过期"], ["superseded", "已作废"], [undefined, "已放弃"]] as const) {
    rerender(<TradeProposalCard proposal={{ ...open, status: "rejected", closedReason }} {...props} />);
    expect(screen.getByTestId("trade-proposal-card")).toHaveTextContent(label);
    expect(screen.queryByText("等待确认")).toBeNull();
  }
});

test("confirmation remains disabled while deterministic risk is blocked", () => {
  render(
    <TradeProposalCard
      proposal={proposal}
      busy={false}
      onAdjustRisk={() => undefined}
      onConfirm={() => undefined}
      onReject={() => undefined}
    />
  );
  expect(screen.getByTestId("confirm-trade")).toBeDisabled();
  expect(screen.getByRole("button", { name: "按风险上限调整" })).toBeVisible();
});

test("confirmation becomes available only with a passed risk check and approval id", () => {
  render(
    <TradeProposalCard
      proposal={{
        ...proposal,
        status: "approval_required",
        approvalId: "approval-1",
        riskCheck: { ...proposal.riskCheck, passed: true, estimatedLoss: 2000 }
      }}
      busy={false}
      onAdjustRisk={() => undefined}
      onConfirm={() => undefined}
      onReject={() => undefined}
    />
  );
  expect(screen.getByTestId("confirm-trade")).toBeEnabled();
});

test("does not offer stop adjustment when a non-loss risk rule also fails", () => {
  render(
    <TradeProposalCard
      proposal={{
        ...proposal,
        riskCheck: {
          ...proposal.riskCheck,
          checks: {
            symbolAllowed: true,
            perTradeLoss: false,
            dailyLoss: false,
            openingTime: true
          }
        }
      }}
      busy={false}
      onAdjustRisk={() => undefined}
      onConfirm={() => undefined}
      onReject={() => undefined}
    />
  );
  expect(screen.queryByRole("button", { name: "按风险上限调整" })).not.toBeInTheDocument();
});

test("renders a non-loss failure without a negative currency overage", () => {
  render(
    <TradeProposalCard
      proposal={{
        ...proposal,
        riskCheck: {
          ...proposal.riskCheck,
          limit: 5000,
          estimatedLoss: 4500,
          reason: "当前合约不在允许范围内",
          checks: {
            symbolAllowed: false,
            perTradeLoss: true,
            dailyLoss: true,
            openingTime: true
          }
        }
      }}
      busy={false}
      onAdjustRisk={() => undefined}
      onConfirm={() => undefined}
      onReject={() => undefined}
    />
  );
  expect(screen.getByText("规则未通过")).toBeInTheDocument();
  expect(screen.queryByText(/-¥/)).not.toBeInTheDocument();
});

test("a passed candidate is one line, a status pill and the buttons; identifiers fold into 详情", () => {
  render(
    <TradeProposalCard
      id="trade-1"
      proposal={{
        ...proposal,
        symbol: "SHFE.rb2701",
        name: "螺纹钢2701",
        volume: 2,
        limitPrice: 3126,
        status: "approval_required",
        approvalId: "approval-1",
        riskCheck: { ...proposal.riskCheck, passed: true, estimatedLoss: 400, reason: "模拟盘未设置账户交易限制。" }
      }}
      busy={false}
      onAdjustRisk={() => undefined}
      onConfirm={() => undefined}
      onReject={() => undefined}
    />
  );
  const card = screen.getByTestId("trade-proposal-card");
  expect(card).toHaveAccessibleName("买入开仓 螺纹钢2701 2 手 @ 3126 限价");
  expect(card).toHaveTextContent("等待确认");
  expect(screen.queryByTestId("trade-risk-result")).not.toBeInTheDocument();
  const details = screen.getByTestId("trade-proposal-card-details");
  expect(details).not.toHaveAttribute("open");
  expect(details).toHaveTextContent("SHFE.rb2701");
  expect(details).toHaveTextContent("风控参考价");
  expect(details).toHaveTextContent("¥400");
  expect(screen.getByRole("button", { name: "确认" })).toBeEnabled();
});

test("a strategy card refused under the user's stop says why on the card", () => {
  render(
    <TradeProposalCard
      proposal={{
        ...proposal,
        status: "approval_required",
        approvalId: "approval-1",
        riskCheck: { ...proposal.riskCheck, passed: true },
        strategySource: { strategyId: "s1", name: "双均线" },
        confirmBlocked: { code: "ai_execution_stopped", reason: "AI 模拟执行已停止或急停，这张策略开仓卡没有发单；恢复模拟执行后再确认。" }
      }}
      busy={false}
      onAdjustRisk={() => undefined}
      onConfirm={() => undefined}
      onReject={() => undefined}
    />
  );
  expect(screen.getByTestId("strategy-card-blocked")).toHaveTextContent("这张策略开仓卡没有发单");
  expect(screen.getByTestId("strategy-card-blocked")).toHaveTextContent("未发单");
  expect(screen.getByText("来自策略：双均线")).toBeVisible();
});

test("an opening card carries its money estimate line; missing amounts show —", () => {
  render(
    <TradeProposalCard
      proposal={{
        ...proposal,
        status: "approval_required",
        approvalId: "approval-1",
        riskCheck: { ...proposal.riskCheck, passed: true },
        estimate: {
          schemaVersion: 1, symbol: "AU2612", side: "buy", offset: "open", volume: 2,
          price: 823.5, marginPerLot: 82_350, commissionPerLot: 24.71,
          margin: 164_700, fee: 49.42, available: 835_570, maxOpenVolume: 10,
          insufficient: false, releasedMargin: null,
          basis: "保证金按交易所标准估算，手续费按自设费率估算，实际以成交后为准。", missing: []
        }
      }}
      busy={false}
      onAdjustRisk={() => undefined}
      onConfirm={() => undefined}
      onReject={() => undefined}
    />
  );
  const line = screen.getByTestId("estimate-line");
  expect(line).toHaveTextContent("保证金约 ¥164,700");
  // Amounts are thousand-separated integer yuan (49.42 的零头不计).
  expect(line).toHaveTextContent("手续费约 ¥49");
  expect(line).toHaveTextContent("可开 10 手");
  expect(line).toHaveTextContent("按交易所标准估算");
  expect(line.querySelector("strong.estimate-short")).toBeNull();
});

test("an estimate short of available funds says so without blocking the buttons", () => {
  render(
    <TradeProposalCard
      proposal={{
        ...proposal,
        volume: 11,
        status: "approval_required",
        approvalId: "approval-1",
        riskCheck: { ...proposal.riskCheck, passed: true },
        estimate: {
          schemaVersion: 1, symbol: "AU2612", side: "buy", offset: "open", volume: 11,
          price: 823.5, marginPerLot: 82_350, commissionPerLot: null,
          margin: 905_850, fee: null, available: 835_570, maxOpenVolume: 10,
          insufficient: true, releasedMargin: null, basis: "估算", missing: ["fee_schedule_unavailable"]
        }
      }}
      busy={false}
      onAdjustRisk={() => undefined}
      onConfirm={() => undefined}
      onReject={() => undefined}
    />
  );
  const line = screen.getByTestId("estimate-line");
  expect(line).toHaveTextContent("手续费约 —");
  expect(line.querySelector("strong.estimate-short")).toHaveTextContent("可用资金不够开 11 手");
  // A caution, never a rejection: the confirm button stays enabled.
  expect(screen.getByTestId("confirm-trade")).toBeEnabled();
});


test("a confirmed card's pill says what its order is, not a blanket 已发送", () => {
  const confirmed: TradeProposal = {
    ...proposal, status: "candidate_recorded", executionEligibleAtCreation: true,
    riskCheck: { ...proposal.riskCheck, passed: true, estimatedLoss: 400 }
  };
  const order = (displayStatus: SimulationOrder["displayStatus"]) => ({
    id: "order-1", proposalId: "proposal-1", clientOrderId: "AIT-1", displayStatus,
  }) as SimulationOrder;
  const pill = (orders?: SimulationOrder[], payload: TradeProposal = confirmed) => {
    const { unmount } = render(
      <TradeProposalCard proposal={payload} orders={orders} busy={false}
        onAdjustRisk={() => undefined} onConfirm={() => undefined} onReject={() => undefined} />
    );
    const card = screen.getByTestId("trade-proposal-card");
    // Accepted / filled orders show as the green outcome row; anything else stays the head pill.
    const text = { pill: card.querySelector(".card-badge")?.textContent, outcome: card.querySelector(".card-outcome")?.textContent };
    unmount();
    return text;
  };
  expect(pill([order("待发送")])).toEqual({ pill: "未发送", outcome: undefined });
  expect(pill([order("结果未知")])).toEqual({ pill: "结果未知", outcome: undefined });
  expect(pill([order("已拒绝")])).toEqual({ pill: "已拒绝", outcome: undefined });
  expect(pill([order("已受理")])).toEqual({ pill: undefined, outcome: "已受理" });
  expect(pill([order("部分成交")])).toEqual({ pill: undefined, outcome: "部分成交" });
  // No order fact for a card without an account (an older Runtime): the old reading stays.
  expect(pill()).toEqual({ pill: undefined, outcome: "已发送" });
  expect(pill([{ ...order("已成交"), proposalId: "proposal-other" }])).toEqual({ pill: undefined, outcome: "已发送" });
  // #1005: a card naming its account whose order is not read yet says only 已确认 — never 已发送 on a guess.
  const owned = { ...confirmed, accountId: "paper:a" };
  expect(pill(undefined, owned)).toEqual({ pill: "已确认", outcome: undefined });
  expect(pill([{ ...order("已成交"), proposalId: "proposal-other" }], owned)).toEqual({ pill: "已确认", outcome: undefined });
  expect(pill([order("已成交")], owned)).toEqual({ pill: undefined, outcome: "已成交" });
});

test("the card's own 确认 submits it (no second dialog, owner 2026-10-19); when it cannot, it is disabled with the reason beside it", async () => {
  const { fireEvent } = await import("@testing-library/react");
  const { confirmBlocked } = await import("./confirmGate");
  const open = { ...proposal, status: "approval_required" as const, approvalId: "a", executionEligibleAtCreation: true,
    riskCheck: { ...proposal.riskCheck, passed: true } };
  const onConfirm = vi.fn();
  const props = { busy: false, onAdjustRisk: () => undefined, onConfirm, onReject: () => undefined };
  const card = { id: "c", type: "trade" as const, payload: open };
  const { rerender } = render(<TradeProposalCard proposal={open} {...props} blockedReason={confirmBlocked(card, undefined)} />);
  fireEvent.click(screen.getByTestId("confirm-trade"));
  expect(onConfirm).toHaveBeenCalledTimes(1);
  rerender(<TradeProposalCard proposal={open} {...props} blockedReason={confirmBlocked(card, "模拟网关未连接")} />);
  expect(screen.getByTestId("confirm-trade")).toBeDisabled();
  expect(screen.getByRole("note")).toHaveTextContent("模拟下单暂时不可用（模拟网关未连接）");
  const stale = { ...open, executionEligibleAtCreation: false };
  rerender(<TradeProposalCard proposal={stale} {...props}
    blockedReason={confirmBlocked({ ...card, payload: stale }, undefined)} />);
  expect(screen.getByRole("note")).toHaveTextContent("请重新出卡");
});

test("the current account's gate does not disable another account's card (#932 F1)", async () => {
  const { confirmBlocked } = await import("./confirmGate");
  const open = { ...proposal, status: "approval_required" as const, approvalId: "a", executionEligibleAtCreation: true };
  const onCurrentAccount = (id?: string) => !id || id === "tqkq-pool:b";
  const cardOn = (accountId: string | undefined) => ({ id: "c", type: "trade" as const, payload: { ...open, accountId } });
  // The conversation is on B and B's gateway is down: A's card (a strategy instance's) stays confirmable —
  // the Runtime checks A's gateway when it is confirmed — while B's own card and an unscoped card are held.
  expect(confirmBlocked(cardOn("tqkq-pool:a"), "模拟网关未连接", onCurrentAccount)).toBeUndefined();
  expect(confirmBlocked(cardOn("tqkq-pool:b"), "模拟网关未连接", onCurrentAccount)).toContain("模拟网关未连接");
  expect(confirmBlocked(cardOn(undefined), "模拟网关未连接", onCurrentAccount)).toContain("模拟网关未连接");
  // The card's own condition still applies on any account.
  expect(confirmBlocked({ ...cardOn("tqkq-pool:a"), payload: { ...cardOn("tqkq-pool:a").payload, executionEligibleAtCreation: false } },
    undefined, onCurrentAccount)).toContain("请重新出卡");
});

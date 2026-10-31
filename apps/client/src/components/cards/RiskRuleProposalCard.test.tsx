import { render, screen } from "@testing-library/react";
import { RiskRuleProposalCard } from "./RiskRuleProposalCard";

const proposal = {
  turnId: "turn-risk",
  proposalId: "proposal-risk",
  version: 1,
  basePolicyVersion: 0,
  policyVersion: 1,
  allowedInstruments: [
    { symbol: "AU2612", name: "沪金" },
    { symbol: "RB2610", name: "螺纹钢" }
  ],
  maxLossPerTrade: "1000.00",
  maxDailyLoss: "3000.00",
  noNewPositionsAfter: "22:00",
  timezone: "Asia/Shanghai" as const,
  rulesDigest: "a".repeat(64),
  deterministic: true as const,
  executionEnabled: false as const,
  status: "approval_required" as const,
  approvalId: "approval-risk"
};

test("shows a compact deterministic risk rule proposal", () => {
  render(<RiskRuleProposalCard proposal={proposal} busy={false} onConfirm={() => undefined} onReject={() => undefined} />);
  expect(screen.getByText("沪金、螺纹钢")).toBeInTheDocument();
  expect(screen.getByText("¥1,000")).toBeInTheDocument();
  expect(screen.getByText("¥3,000")).toBeInTheDocument();
  expect(screen.getByText("22:00")).toBeInTheDocument();
  expect(screen.getByText(/AI 无法跳过/)).toBeInTheDocument();
});

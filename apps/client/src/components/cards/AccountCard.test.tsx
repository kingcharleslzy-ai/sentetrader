import { render, screen, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { AccountSummary } from "@ai-trader/ui-events";
import { AccountCard } from "./AccountCard";

const unavailableAccount: AccountSummary = {
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
};

test("unavailable account data uses plain language and never presents zeros as facts", () => {
  render(<AccountCard account={unavailableAccount} />);
  const card = screen.getByTestId("account-card");
  expect(within(card).getByText("快期模拟账户 · 暂时无法读取")).toBeVisible();
  expect(within(card).getByText("暂时不可用")).toBeVisible();
  expect(within(card).getAllByText("—")).toHaveLength(4);
  expect(within(card).getByText("暂时无法读取账户数据，请到右侧“系统”中重试连接。")).toBeVisible();
  expect(within(card).queryByText(/Sidecar|快照|零值/)).not.toBeInTheDocument();
});

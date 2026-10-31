// The Runtime's own status sets, read from its source so the two cannot drift apart.
import runtimeStatuses from "../../../../../services/trader-runtime/src/trader_runtime/simulation_orders.py?raw";
import { fireEvent, render, screen } from "@testing-library/react";
import type { CloseBatchProposal } from "@ai-trader/ui-events";
import type { SimulationOrder } from "../../api";
import {
  CloseBatchProposalCard, closeBatchExecution,
  NOT_SENT_STATUSES, SENT_STATUSES, UNKNOWN_STATUSES
} from "./CloseBatchProposalCard";

const order = { symbol: "SHFE.rb2610", name: "螺纹钢2610", side: "sell_close" as const, offset: "CLOSETODAY" as const, positionBucket: "today" as const,
  volume: 2, limitPrice: 3100, contractMultiplier: 10, riskCheck: { passed: true, limit: 0, estimatedLoss: 0, reason: "平仓", contractMultiplier: 10, priceTick: "1" } };
const proposal: CloseBatchProposal = {
  turnId: "turn-1", proposalId: "batch-1", version: 1, environment: "simulation", reason: "全部平仓",
  orders: [order, { ...order, symbol: "SHFE.au2612", name: "沪金2612", side: "buy_close", offset: "CLOSE", positionBucket: "historical", volume: 1, limitPrice: 823.5 }],
  skipped: [{ symbol: "DCE.i2609", reason: "no_position" }], totalOrders: 2, totalVolume: 3, riskCheck: { passed: true },
  updatedAt: "2026-09-17T12:00:00Z", status: "approval_required", approvalId: null
};
/** A confirmed card carries its children's ids; their facts come from the live order list. */
const confirmed: CloseBatchProposal = {
  ...proposal, status: "candidate_recorded", approvalId: "approval-1", auditId: "audit-12345678", voiceConfirmBy: null,
  orders: proposal.orders.map((item, index) => ({ ...item, clientOrderId: `AIT-${index}` }))
};
const fact = (clientOrderId: string, overrides: Partial<SimulationOrder>): SimulationOrder => ({
  id: clientOrderId, proposalId: "child", clientOrderId,
  request: { symbol: "SHFE.rb2610", direction: "SELL", offset: "CLOSETODAY", volume: 2, priceType: "LIMIT", limitPrice: 3100 },
  submitPhase: "CONFIRMED", cancelPhase: "NONE", displayStatus: "已受理",
  accountLabel: "快期模拟账户", liveTrading: "disabled", automaticStopLoss: false, ...overrides
} as SimulationOrder);

test("the card's status buckets are the Runtime's, and no status defaults to 已发送", () => {
  // The same three sets live in simulation_orders.py; compare against that file so the
  // two implementations cannot drift into disagreeing about what counts as "sent".
  const set = (name: string) => {
    // Anchored: "SENT_STATUSES" is a suffix of "NOT_SENT_STATUSES".
    const body = new RegExp(`^${name} = frozenset\\(\\{([^}]*)\\}\\)`, "m").exec(runtimeStatuses)?.[1];
    expect(body).toBeDefined();
    return new Set([...body!.matchAll(/"([^"]+)"/g)].map(match => match[1]));
  };
  expect(NOT_SENT_STATUSES).toEqual(set("NOT_SENT_STATUSES"));
  expect(SENT_STATUSES).toEqual(set("SENT_STATUSES"));
  expect(UNKNOWN_STATUSES).toEqual(set("UNKNOWN_STATUSES"));
  // A display status nobody classified is unknown, never a send receipt.
  const invented = closeBatchExecution([{ displayStatus: "某种新状态" } as unknown as SimulationOrder]);
  expect(invented).toMatchObject({ sent: 0, notSent: 0, unknown: 1, text: "已确认，1 笔结果未知" });
});

test("lists every close order, the skipped items, and only confirms with an approval id", () => {
  const onConfirm = vi.fn();
  const { rerender } = render(<CloseBatchProposalCard proposal={proposal} busy={false} onConfirm={onConfirm} onReject={() => undefined} />);
  const card = screen.getByTestId("close-batch-proposal-card");
  expect(card).toHaveTextContent("全部平仓 2 笔 3 手");
  const rows = screen.getAllByRole("row").slice(1);
  expect(rows).toHaveLength(2);
  // Compact table: no per-row risk column; codes and the reason fold into 详情.
  expect(screen.getAllByRole("columnheader").map(cell => cell.textContent)).toEqual(["合约", "方向", "手数", "价格"]);
  expect(rows[0]).not.toHaveTextContent("SHFE.rb2610");
  expect(screen.getByTestId("close-batch-proposal-card-details")).toHaveTextContent("SHFE.rb2610、SHFE.au2612");
  expect(rows[0]).toHaveTextContent("螺纹钢2610");
  expect(rows[0]).toHaveTextContent("卖出平今");
  expect(rows[1]).toHaveTextContent("买入平昨");
  expect(card).toHaveTextContent("跳过 1 项");
  expect(card).not.toHaveTextContent("no_position");
  expect(screen.getByTestId("close-batch-proposal-card-details")).toHaveTextContent("i2609 无持仓");
  expect(screen.getByTestId("confirm-close-batch")).toBeDisabled();

  rerender(<CloseBatchProposalCard proposal={{ ...proposal, approvalId: "approval-1" }} busy={false} onConfirm={onConfirm} onReject={() => undefined} />);
  fireEvent.click(screen.getByTestId("confirm-close-batch"));
  expect(onConfirm).toHaveBeenCalledOnce();
});

test("shows the spoken confirmation countdown while the call waits, and never claims a send without its orders' facts", () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-09-17T12:00:00Z"));
  const waiting = { ...proposal, approvalId: "approval-1", voiceConfirmBy: "2026-09-17T12:00:12Z" };
  const { rerender } = render(<CloseBatchProposalCard proposal={waiting} busy={false} onConfirm={() => undefined} onReject={() => undefined} />);
  expect(screen.getByTestId("close-batch-proposal-card")).toHaveTextContent("语音确认 12 s");
  rerender(<CloseBatchProposalCard proposal={confirmed} busy={false} onConfirm={() => undefined} onReject={() => undefined} />);
  // Confirmation alone is not a send receipt: without the order facts the card only says 已确认.
  expect(screen.getByTestId("close-batch-proposal-card")).toHaveTextContent("已确认");
  expect(screen.getByTestId("close-batch-execution")).toHaveTextContent("逐笔状态见委托栏");
  expect(screen.queryByTestId("confirm-close-batch")).not.toBeInTheDocument();
  vi.useRealTimers();
});

test("a confirmed batch renders each order's own state and offers continue/abandon for the unsent ones", () => {
  // The external review's case: the confirmation was interrupted, so one order went
  // out, one was refused before the gateway call and one has an unknown outcome.
  const onOrderAction = vi.fn();
  const orders = [
    fact("AIT-0", { displayStatus: "已受理" }),
    fact("AIT-1", { displayStatus: "待发送", submitPhase: "PREPARED", lastReadError: "close_offset_mismatch" })
  ];
  render(<CloseBatchProposalCard proposal={confirmed} orders={orders} onOrderAction={onOrderAction} busy={false}
    onConfirm={() => undefined} onReject={() => undefined} />);
  const card = screen.getByTestId("close-batch-proposal-card");
  expect(screen.getAllByRole("columnheader").map(cell => cell.textContent)).toEqual(["合约", "方向", "手数", "价格", "状态"]);
  expect(screen.getByTestId("close-batch-execution")).toHaveTextContent("已确认，1 笔已发送、1 笔未发送");
  expect(screen.getByTestId("close-batch-execution")).toHaveTextContent("系统不会自动重发");
  expect(card).toHaveTextContent("部分未发送");
  const rows = screen.getAllByRole("row").slice(1);
  expect(rows[0]).toHaveTextContent("已受理");
  expect(rows[1]).toHaveTextContent("未发送 · 可平手数已变化");
  // The unsent order is handled through the ordinary single-order actions, never re-created.
  fireEvent.click(screen.getByRole("button", { name: "继续发送" }));
  expect(onOrderAction).toHaveBeenCalledWith(orders[1], "continue");
});

test("#1005 another account's card shows its unsent order but offers no continue/abandon (they act on the current account)", async () => {
  const { CardRenderer } = await import("../Conversation");
  const orderAction = vi.fn();
  const orders = [fact("AIT-0", { displayStatus: "已受理" }),
    fact("AIT-1", { displayStatus: "待发送", submitPhase: "PREPARED", lastReadError: "close_offset_mismatch" })];
  const card = { id: "batch-1", type: "close-batch" as const, payload: { ...confirmed, accountId: "tqkq:a", accountLabel: "快期模拟账户" } };
  const actions = (current: boolean) => ({ ordersFor: (account?: string) => account === "tqkq:a" ? orders : [],
    onCurrentAccount: () => current, orderAction, confirm: vi.fn(), reject: vi.fn() }) as never;
  const view = render(<CardRenderer card={card} actions={actions(false)} />);
  expect(screen.getAllByRole("row")[2]).toHaveTextContent("未发送 · 可平手数已变化");
  expect(screen.queryByRole("button", { name: "继续发送" })).not.toBeInTheDocument();
  expect(screen.getByTestId("close-batch-execution")).toHaveTextContent("切回快期模拟账户后可继续或放弃");
  // Back on that account: the same card offers them again.
  view.rerender(<CardRenderer card={card} actions={actions(true)} />);
  fireEvent.click(screen.getByRole("button", { name: "继续发送" }));
  expect(orderAction).toHaveBeenCalledWith(orders[1], "continue");
});

test("an unknown outcome is reported as unknown and is never offered a re-send", () => {
  const onOrderAction = vi.fn();
  const orders = [
    fact("AIT-0", { displayStatus: "结果未知", submitPhase: "UNKNOWN" }),
    fact("AIT-1", { displayStatus: "已成交" })
  ];
  render(<CloseBatchProposalCard proposal={confirmed} orders={orders} onOrderAction={onOrderAction} busy={false}
    onConfirm={() => undefined} onReject={() => undefined} />);
  expect(screen.getByTestId("close-batch-execution")).toHaveTextContent("已确认，1 笔已发送、1 笔结果未知");
  expect(screen.getByTestId("close-batch-proposal-card")).toHaveTextContent("结果待确认");
  expect(screen.queryByRole("button", { name: "继续发送" })).not.toBeInTheDocument();
});

test("the price row says how the batch was priced, not always 对手价", () => {
  const price = (patch: Partial<CloseBatchProposal>) => {
    const { unmount } = render(<CloseBatchProposalCard proposal={{ ...proposal, ...patch }} busy={false} onConfirm={() => undefined} onReject={() => undefined} />);
    const text = screen.getByTestId("close-batch-proposal-card-details").textContent ?? "";
    unmount();
    return text;
  };
  expect(price({})).toContain("按对手价生成");
  expect(price({ priceIntent: "aggressive" })).toContain("按对手价生成");
  expect(price({ priceIntent: "passive" })).toContain("按排队价（挂单）生成");
  expect(price({ priceIntent: "last" })).toContain("按最新价生成");
  expect(price({ priceIntent: "aggressive", userLimitPrice: true })).toContain("按你给的限价生成");
});

import { GitCompareArrows } from "lucide-react";
import type { SpreadProposal } from "@ai-trader/ui-events";
import type { SimulationOrder } from "../../api";
import { CardShell } from "../CardShell";
import { closeBatchExecution, closedBadge } from "./CloseBatchProposalCard";
import { useVoiceConfirmCountdown, voiceConfirmLabel } from "./voiceConfirm";

interface SpreadProposalCardProps {
  proposal: SpreadProposal;
  busy: boolean;
  id?: string;
  /** The live simulation orders; a confirmed card refreshes its legs' states from here. */
  orders?: SimulationOrder[];
  onConfirm: () => void;
  onReject: () => void;
  /** Why 确认 cannot be pressed now (account / gateway): shown beside it. */
  blockedReason?: string;
  /** The card's own account when it is not the conversation's current one (#1011), under the title. */
  accountNote?: string;
}

const ACTION_LABEL: Record<SpreadProposal["action"], string> = {
  open_long: "做多价差", open_short: "做空价差",
  close_long: "平掉做多价差", close_short: "平掉做空价差",
};
const BUCKET_LABEL = { today: "平今", historical: "平昨", all: "平仓" } as const;
/** 价差数值：最多 6 位小数，去掉尾零（与 Runtime 的 spread_value_text 同口径）。 */
export function spreadValueText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toFixed(6).replace(/0+$/, "").replace(/\.$/, "") || "0";
}

/** One leg order fact as the confirmed card carries it (the Runtime's simulationOrders readback). */
interface LegFact {
  clientOrderId?: string | null;
  displayStatus?: string;
  submitPhase?: string;
  symbol?: string;
  request?: { symbol?: string };
}

/**
 * 价差确认卡 (2026-09-29 第二期): both legs of one spread action on one card — the spread
 * definition, the current spread, one unit's composition and the spread stop / take. One
 * confirmation dispatches the two legs together at the opponent's price; the confirmed card
 * reports each leg's real order (逐笔真实结果，永不冒充已发送), the same 口径 as the batch card.
 */
export function SpreadProposalCard({ proposal, busy, id, orders, onConfirm, onReject, blockedReason, accountNote }: SpreadProposalCardProps) {
  const recorded = proposal.status === "candidate_recorded";
  const rejected = proposal.status === "rejected";
  const pending = proposal.status === "approval_required";
  const secondsLeft = useVoiceConfirmCountdown(proposal.voiceConfirmBy, pending);
  const riskPassed = proposal.orders.every(order => order.riskCheck.passed);
  const failed = proposal.orders.filter(order => !order.riskCheck.passed);
  // The confirmed card's legs: the readback the executor sent with the card, refreshed by the
  // live order feed when it has the same client order id.
  const facts = ((proposal.simulationOrders ?? []) as LegFact[]).map(fact => {
    const clientId = fact.clientOrderId;
    const live = clientId ? orders?.find(item => item.clientOrderId === clientId) : undefined;
    // A live order keeps its contract under request.symbol, not at the top: the leg row is matched by
    // the readback's own symbol, else the row stays “状态读取中” after a reload (线上 09-29).
    const symbol = fact.symbol ?? (live as { request?: { symbol?: string } } | undefined)?.request?.symbol;
    return { ...(live ?? fact), symbol } as { displayStatus?: string; symbol?: string; clientOrderId?: string | null };
  });
  const execution = proposal.execution && typeof proposal.execution.text === "string"
    ? { text: proposal.execution.text, unknown: Number(proposal.execution.unknown) > 0,
        notSent: Number(proposal.execution.notSent) > 0 }
    : facts.length ? closeBatchExecution(facts as unknown as SimulationOrder[]) : { text: "", unknown: false, notSent: false };
  // The settled attempt's one-line conclusion (成交完成 / 已补单成交 / 已回退并暂停 / …): the card's
  // final state, not a per-order readout.
  const conclusion = proposal.executionConclusion?.text;
  const confirmedBadge = !facts.length ? "已确认"
    : execution.unknown ? "结果待确认" : execution.notSent ? "部分未发送" : "已发送";
  const badge = recorded ? confirmedBadge : rejected ? closedBadge(proposal.closedReason)
    : secondsLeft !== undefined ? voiceConfirmLabel(secondsLeft)
    : riskPassed ? "等待确认" : "风险未通过";
  const spread = proposal.spread as { stopLoss?: number | null; takeProfit?: number | null };
  const exitLabel = proposal.exitReason === "spread_stop" ? "价差止损"
    : proposal.exitReason === "spread_take" ? "价差止盈" : null;
  // Legs and readback rows pair up in order per (合约, 方向): the card's legs are the plan the
  // executor reserved, and the readback is exactly those rows in leg order.
  const factQueues = new Map<string, { displayStatus?: string; clientOrderId?: string | null }[]>();
  for (const fact of facts) {
    const key = String(fact.symbol ?? "");
    factQueues.set(key, [...(factQueues.get(key) ?? []), fact]);
  }
  const factFor = (symbol: string) => factQueues.get(symbol)?.shift();
  const price = (value: number) => (Number.isInteger(value) ? String(value) : value.toFixed(2));
  return (
    <CardShell
      icon={<GitCompareArrows />}
      title={`${ACTION_LABEL[proposal.action]} ${proposal.units} 份 · ${proposal.spreadWords}`}
      meta={accountNote}
      badge={badge}
      badgeTone={recorded ? (execution.unknown || execution.notSent ? "warning" : "positive") : riskPassed ? "positive" : rejected ? "neutral" : "warning"}
      id={id}
      phase={recorded ? "done" : rejected ? "dropped" : busy ? "busy" : "open"}
      testId="spread-proposal-card"
      details={
        <dl className="card-facts">
          {proposal.reason ? <div><dt>原因</dt><dd>{proposal.reason}</dd></div> : null}
          <div><dt>一份构成</dt><dd>{proposal.legsWords}</dd></div>
          {proposal.currentSpread !== null && proposal.currentSpread !== undefined
            ? <div><dt>当前价差</dt><dd>{spreadValueText(proposal.currentSpread)}</dd></div> : null}
          {proposal.entrySpread !== null && proposal.entrySpread !== undefined
            ? <div><dt>开仓价差</dt><dd>{spreadValueText(proposal.entrySpread)}</dd></div> : null}
          {exitLabel ? <div><dt>触发</dt><dd>{exitLabel}</dd></div> : null}
          {spread.stopLoss != null || spread.takeProfit != null
            ? <div><dt>价差止损/止盈</dt><dd>{spread.stopLoss != null ? `止损 ${spreadValueText(spread.stopLoss)}` : "未设止损"}{spread.takeProfit != null ? ` · 止盈 ${spreadValueText(spread.takeProfit)}` : ""}</dd></div>
            : null}
          <div><dt>发送</dt><dd>两腿同时按对手价 · {proposal.updatedAt.slice(11, 19) || "刚刚"}</dd></div>
          {proposal.auditId ? <div><dt>审计记录</dt><dd>{proposal.auditId.slice(0, 8)} · 每腿委托状态以{proposal.accountLabel === "本站模拟账户" ? "本站模拟" : "快期"}查询结果为准</dd></div> : null}
        </dl>
      }
      actions={recorded || rejected ? null : (
        <>
          <button className="ghost-button danger" type="button" onClick={onReject} disabled={busy}>放弃</button>
          <button className="primary-button accent" type="button" onClick={onConfirm} disabled={busy || !riskPassed || !proposal.approvalId || Boolean(blockedReason)} data-testid="confirm-spread">
            确认
          </button>
          {blockedReason ? <small className="card-blocked-reason" role="note">{blockedReason}</small> : null}
        </>
      )}
    >
      {proposal.strategySource ? (
        <p className="target-plan-line" data-testid="strategy-source-line">
          <span>来自策略：{proposal.strategySource.name}</span>
          <small>确认后两腿同时下单；瘸腿自动撤单补一次，仍不成回退并暂停策略</small>
        </p>
      ) : null}
      {proposal.confirmBlocked && pending ? (
        <div className="risk-box" role="alert" data-testid="spread-card-blocked">
          <span>{proposal.confirmBlocked.reason}</span>
          <strong>未发单</strong>
        </div>
      ) : null}
      <table className="close-batch-orders" aria-label="价差两腿委托">
        <thead><tr><th>合约</th><th>方向</th><th>手数</th><th>价格</th>{recorded ? <th>状态</th> : null}</tr></thead>
        <tbody>
          {proposal.orders.map((order, index) => {
            const fact = factFor(order.symbol);
            return (
              <tr key={order.clientOrderId ?? `${order.symbol}-${order.side}-${order.positionBucket}-${index}`} className={order.riskCheck.passed ? undefined : "negative"}>
                <td><strong>{order.name}</strong></td>
                <td>{order.side.startsWith("buy") ? "买入" : "卖出"}{order.side.endsWith("close") ? BUCKET_LABEL[order.positionBucket ?? "all"] : "开仓"}</td>
                <td>{order.volume} 手</td>
                <td>{price(order.limitPrice)}</td>
                {recorded ? <td>{fact ? fact.displayStatus ?? "状态读取中" : "状态读取中"}</td> : null}
              </tr>
            );
          })}
        </tbody>
      </table>
      {recorded ? (
        <>
          {conclusion ? (
            <p className="close-batch-skipped" data-testid="spread-conclusion">
              <strong>执行结论：{conclusion}</strong>
            </p>
          ) : null}
          <p className="close-batch-skipped" data-testid="spread-execution">
            {execution.text || "已确认，两腿执行中；逐笔状态见委托栏"}
            {execution.unknown ? `；结果未知的以${proposal.accountLabel === "本站模拟账户" ? "本站模拟" : "快期"}查询为准，系统不会自动重发` : ""}
          </p>
        </>
      ) : null}
      {failed.length ? (
        <p className="close-batch-skipped">风控未通过 {failed.length} 腿：{failed.map(order => `${order.name} ${order.riskCheck.reason || "未通过"}`).join("；")}</p>
      ) : null}
    </CardShell>
  );
}

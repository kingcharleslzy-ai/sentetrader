import { Layers } from "lucide-react";
import type { CloseBatchProposal, ProposalClosedReason } from "@ai-trader/ui-events";
import type { SimulationOrder } from "../../api";
import { CardActionButtons } from "./cardActions";
import { CardSteps, remainingResult, remainingStep } from "./ProtectionSteps";
import { CardShell } from "../CardShell";
import { useAbandonOrder, type OrderActionHandler } from "../futures/PositionPanels";
import { orderStatusText } from "../futures/support";
import { useVoiceConfirmCountdown, voiceConfirmLabel } from "./voiceConfirm";

interface CloseBatchProposalCardProps {
  proposal: CloseBatchProposal;
  busy: boolean;
  id?: string;
  /** The live simulation orders; a confirmed card reads its children's facts from here. */
  orders?: SimulationOrder[];
  onOrderAction?: OrderActionHandler;
  /** The card belongs to another account than the session's (#1005): its unsent orders are continued or
   *  abandoned after switching back to it, never from here (the per-order actions act on the current account). */
  otherAccount?: boolean;
  onConfirm: () => void;
  /** Why 确认 cannot be pressed now (account / gateway / a card that could never trade): shown beside it. */
  blockedReason?: string;
  onReject: () => void;
  /** 合并卡: a failed second step's buttons send their prompt as the user's message. */
  onAsk?: (text: string) => void;
  /** The card's own account when it is not the conversation's current one (#1011), under the title. */
  accountNote?: string;
}

const BUCKET_LABEL = { today: "平今", historical: "平昨", all: "平仓" } as const;

/** Why a position bucket got no close order, in the user's words (Runtime error codes stay in 详情 only as text). */
const SKIP_REASONS: Record<string, string> = {
  no_position: "无持仓", close_position_not_found: "无持仓", close_volume_unavailable: "可平数量不足",
  quote_stale: "无最新报价", quote_not_current: "无最新报价", quote_age_missing: "无最新报价",
  quote_datetime_missing: "无最新报价", quote_datetime_invalid: "无最新报价", limit_price_unavailable: "无最新报价",
  market_session_closed: "未开盘", price_tick_missing: "合约信息缺失", volume_multiple_missing: "合约信息缺失",
  limit_price_outside_limits: "对手价超出涨跌停", limit_price_not_on_tick: "价格不在最小变动单位上",
  account_not_current: "账户数据未更新", symbol_not_found: "合约不存在"
};
/** Why one confirmed order was not sent, on top of the shared codes above. */
export const NOT_SENT_REASONS: Record<string, string> = {
  ...SKIP_REASONS,
  close_offset_mismatch: "可平手数已变化", risk_check_failed: "确定性风控未通过",
  symbol_identity_mismatch: "合约与行情不一致", strategy_metadata_changed: "合约参数已变化",
  invalid_limit_price: "委托价无效", simulation_gateway_disabled: "模拟网关未连接",
  order_not_prepared: "委托未生成", order_transport_unavailable: "网关连接中断",
  paper_settlement_pending: "本站模拟正在等交易所公布结算价（约 19:00），公布后即可下单",
  gateway_command_expired: "网关排队超时，委托未发出", sidecar_reconnecting: "行情会话重连中，委托未发出",
  sidecar_auth_rejected: "账户连接已更换，委托未发出"
};

/** The user-readable reason for an unsent order, or empty when none is known. */
export function notSentReason(code: unknown): string {
  const key = String(code ?? "").trim();
  return key ? NOT_SENT_REASONS[key] ?? "" : "";
}
export const skipLabel = (item: { symbol?: unknown; reason?: unknown }) => {
  const code = String(item.reason ?? "").trim();
  const reason = SKIP_REASONS[code] ?? (code ? code.replace(/_/g, " ") : "暂时无法生成委托");
  return `${String(item.symbol ?? "").split(".").at(-1) || "—"} ${reason}`;
};

// Mirrors NOT_SENT_STATUSES / SENT_STATUSES / UNKNOWN_STATUSES in simulation_orders.py;
// a test compares these three sets against that file so they cannot drift apart.
export const NOT_SENT_STATUSES = new Set(["待发送", "已放弃"]);
export const SENT_STATUSES = new Set(["已受理", "部分成交", "部分成交后已结束", "已成交", "已拒绝", "已撤销"]);
export const UNKNOWN_STATUSES = new Set(["结果未知", "身份异常", "回报异常"]);

/** How the batch's prices were set — the confirmation must say what the numbers are (cards from before the
 * field carry none and were always 对手价). */
export function batchPriceLabel(proposal: Pick<CloseBatchProposal, "priceIntent" | "userLimitPrice">): string {
  if (proposal.userLimitPrice) return "按你给的限价生成";
  if (proposal.priceIntent === "passive") return "按排队价（挂单）生成";
  if (proposal.priceIntent === "last") return "按最新价生成";
  return "按对手价生成";
}

/**
 * What a confirmed batch actually did, counted from its child orders — the same roll-up
 * the Runtime reports. The card's own `candidate_recorded` only means the user confirmed:
 * a confirmation interrupted before the last send leaves orders that were never sent.
 * "已发送" is never a default: an unlisted status counts as unknown, not as sent.
 */
export function closeBatchExecution(facts: (SimulationOrder | undefined)[]) {
  const statuses = facts.filter((fact): fact is SimulationOrder => fact !== undefined).map(fact => fact.displayStatus);
  const notSent = statuses.filter(status => NOT_SENT_STATUSES.has(status)).length;
  const sent = statuses.filter(status => SENT_STATUSES.has(status)).length;
  const unknown = statuses.length - notSent - sent;
  const parts = ([[sent, "已发送"], [notSent, "未发送"], [unknown, "结果未知"]] as const)
    .filter(([count]) => count > 0).map(([count, label]) => `${count} 笔${label}`);
  return { known: statuses.length, sent, notSent, unknown, text: parts.length ? `已确认，${parts.join("、")}` : "" };
}

/** A closed card's pill (#932 P2): 已过期 when its confirm window ran out, 已作废 when a newer plan replaced it,
 *  已放弃 when the user dropped it — never 等待确认 on a card nothing can confirm any more. */
export function closedBadge(reason: ProposalClosedReason | undefined): string {
  return reason === "expired" ? "已过期" : reason === "superseded" ? "已作废" : "已放弃";
}

/** One card listing every close order of a "全部平仓" request; one confirmation sends them all. */
export function CloseBatchProposalCard({ proposal, busy, id, orders, onOrderAction, otherAccount, onConfirm, onReject, onAsk, blockedReason, accountNote }: CloseBatchProposalCardProps) {
  const recorded = proposal.status === "candidate_recorded";
  const rejected = proposal.status === "rejected";
  const pending = proposal.status === "approval_required";
  const secondsLeft = useVoiceConfirmCountdown(proposal.voiceConfirmBy, pending);
  const failed = proposal.orders.filter(order => !order.riskCheck.passed);
  const riskPassed = failed.length === 0;
  const facts = proposal.orders.map(order =>
    order.clientOrderId ? orders?.find(item => item.clientOrderId === order.clientOrderId) : undefined);
  const execution = closeBatchExecution(facts);
  const abandon = useAbandonOrder(busy, onOrderAction ?? (() => undefined));
  const unsent = facts.filter((fact): fact is SimulationOrder => fact?.submitPhase === "PREPARED");
  // A confirmed card never claims "sent" on its own: it says what its orders are, or,
  // until their facts are loaded, only that the user confirmed.
  const confirmedBadge = !execution.known ? "已确认"
    : execution.unknown ? "结果待确认" : execution.notSent ? "部分未发送" : "已发送";
  const badge = recorded ? confirmedBadge : rejected ? closedBadge(proposal.closedReason) : secondsLeft !== undefined ? voiceConfirmLabel(secondsLeft) : riskPassed ? "等待确认" : "风险未通过";
  const price = (value: number) => (Number.isInteger(value) ? String(value) : value.toFixed(2));
  const plan = proposal.remainingProtection;
  const planResult = plan ? remainingResult(plan, proposal.totalVolume) : null;
  const notSentLabel = (fact: SimulationOrder) => {
    const code = String(fact.lastReadError ?? "").trim();
    return NOT_SENT_REASONS[code] ?? (code ? code.replace(/_/g, " ") : "尚未发送");
  };
  return (
    <CardShell
      icon={<Layers />}
      title={`全部平仓 ${proposal.totalOrders} 笔 ${proposal.totalVolume} 手`}
      meta={accountNote}
      badge={badge}
      badgeTone={recorded ? (execution.unknown || execution.notSent ? "warning" : "positive") : riskPassed ? "positive" : rejected ? "neutral" : "warning"}
      id={id}
      phase={recorded ? "done" : rejected ? "dropped" : busy ? "busy" : "open"}
      testId="close-batch-proposal-card"
      details={
        <dl className="card-facts">
          {proposal.reason ? <div><dt>原因</dt><dd>{proposal.reason}</dd></div> : null}
          <div><dt>合约</dt><dd>{proposal.orders.map(order => order.symbol).join("、")}</dd></div>
          <div><dt>价格</dt><dd>{batchPriceLabel(proposal)} · {proposal.updatedAt.slice(11, 19) || "刚刚"}</dd></div>
          {proposal.skipped.length ? <div><dt>跳过</dt><dd>{proposal.skipped.map(skipLabel).join("；")}</dd></div> : null}
          {proposal.linkedProposalIds?.length ? <div><dt>一起确认</dt><dd>本站模拟两本账的全平：人民币账、美元账（外盘）各一张，确认或放弃这一张，另一张跟着一起</dd></div> : null}
          {proposal.auditId ? <div><dt>审计记录</dt><dd>{proposal.auditId.slice(0, 8)} · 每笔委托状态以{proposal.accountLabel === "本站模拟账户" ? "本站模拟" : "快期"}查询结果为准</dd></div> : null}
        </dl>
      }
      actions={recorded || rejected ? null : (
        <>
          <button className="ghost-button danger" type="button" onClick={onReject} disabled={busy}>放弃</button>
          <button className="primary-button accent" type="button" onClick={onConfirm} disabled={busy || !riskPassed || !proposal.approvalId || Boolean(blockedReason)} data-testid="confirm-close-batch">
            确认
          </button>
          {blockedReason ? <small className="card-blocked-reason" role="note">{blockedReason}</small> : null}
        </>
      )}
    >
      {proposal.targetPosition ? (
        <p className="target-plan-line" data-testid="target-plan-line">
          <span>目标持仓：{proposal.targetPosition.summary}</span>
          {proposal.targetPosition.totalSteps && proposal.targetPosition.totalSteps > 1 ? (
            <small>{proposal.targetPosition.note ?? `第 ${proposal.targetPosition.step ?? 1} 步 / 共 ${proposal.targetPosition.totalSteps} 步`}</small>
          ) : null}
        </p>
      ) : null}
      <table className="close-batch-orders" aria-label="平仓委托">
        <thead><tr><th>合约</th><th>方向</th><th>手数</th><th>价格</th>{recorded ? <th>状态</th> : null}</tr></thead>
        <tbody>
          {proposal.orders.map((order, index) => {
            const fact = facts[index];
            const prepared = fact?.submitPhase === "PREPARED";
            return (
              <tr key={order.clientOrderId ?? `${order.symbol}-${order.side}-${order.positionBucket}-${index}`} className={order.riskCheck.passed ? undefined : "negative"}>
                <td><strong>{order.name}</strong></td>
                <td>{order.side === "buy_close" ? "买入" : "卖出"}{order.offset === "CLOSETODAY" ? "平今" : BUCKET_LABEL[order.positionBucket]}</td>
                <td>{order.volume} 手</td>
                <td>{price(order.limitPrice)}</td>
                {recorded ? (
                  <td>
                    {fact ? prepared ? `未发送 · ${notSentLabel(fact)}` : orderStatusText(fact) : "状态读取中"}
                    {prepared && onOrderAction ? (
                      <span className="fdw-row-actions">
                        <button type="button" disabled={busy} onClick={() => onOrderAction(fact!, "continue")}>继续发送</button>
                        <button type="button" className="app-button danger" disabled={busy} onClick={() => abandon.request(fact!)}>放弃</button>
                      </span>
                    ) : null}
                  </td>
                ) : null}
              </tr>
            );
          })}
        </tbody>
      </table>
      {plan ? (
        // 合并卡 on a 今+昨 split close (2026-10-06): the remaining position's stop / take after the closes.
        <div className="sc-step-block" data-testid="merged-close-steps" data-status={plan.status}>
          <CardSteps label="第二步" steps={[{ ...remainingStep(plan), title: `平仓后：${remainingStep(plan).title}` }]} />
          {planResult ? <p className="sc-step-result" data-tone={planResult.tone} role="status">{planResult.text}</p>
            : recorded ? null : <p className="sc-step-note">先平仓；平仓成交后再改剩下仓位的止损止盈，一次确认两步都做</p>}
          {plan.strategyNote ? <p className="sc-step-note" data-testid="strategy-protection-note">{plan.strategyNote}</p> : null}
          {recorded && plan.status === "failed" && plan.actions?.length
            ? <div className="card-actions"><CardActionButtons actions={plan.actions} handlers={{ askAi: onAsk }} /></div> : null}
        </div>
      ) : null}
      {recorded ? (
        <p className="close-batch-skipped" data-testid="close-batch-execution">
          {execution.text || "已确认，逐笔状态见委托栏"}
          {execution.notSent ? otherAccount ? `；切回${proposal.accountLabel ?? "这张卡的账户"}后可继续或放弃`
            : "；未发送的可在上表继续发送或放弃，系统不会自动重发" : ""}
          {execution.unknown ? `；结果未知的以${proposal.accountLabel === "本站模拟账户" ? "本站模拟" : "快期"}查询为准，系统不会自动重发` : ""}
        </p>
      ) : null}
      {failed.length ? (
        <p className="close-batch-skipped">风控未通过 {failed.length} 笔：{failed.map(order => `${order.name} ${order.riskCheck.reason || "未通过"}`).join("；")}</p>
      ) : null}
      {proposal.skipped.length ? (
        <p className="close-batch-skipped">跳过 {proposal.skipped.length} 项</p>
      ) : null}
      {unsent.length > 0 && onOrderAction ? abandon.dialog : null}
    </CardShell>
  );
}

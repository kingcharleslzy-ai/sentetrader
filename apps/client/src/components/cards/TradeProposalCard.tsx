import { ArrowUpRight, Check } from "lucide-react";
import type { TradeProposal } from "@ai-trader/ui-events";
import type { SimulationOrder } from "../../api";
import { CardShell } from "../CardShell";
import { SENT_STATUSES, closedBadge, notSentReason } from "./CloseBatchProposalCard";
import { useVoiceConfirmCountdown, voiceConfirmLabel } from "./voiceConfirm";
import { formatFee, orderStatusText } from "../futures/support";
import { CardActionButtons } from "./cardActions";
import { CardSteps, priceWords, remainingResult, remainingStep, type CardStep } from "./ProtectionSteps";

const ACCEPTED_STATUSES = new Set(["已受理", "部分成交", "部分成交后已结束", "已成交"]);

const currency = new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: "CNY",
  maximumFractionDigits: 0
});
const dollars = new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0
});

interface TradeProposalCardProps {
  proposal: TradeProposal;
  busy: boolean;
  id?: string;
  /** The live simulation orders; a confirmed card reads its own order (same proposalId) from here. */
  orders?: SimulationOrder[];
  onAdjustRisk: () => void;
  onConfirm: () => void;
  onReject: () => void;
  /** Why 确认 cannot be pressed now (account / gateway / a card that could never trade): shown beside it. */
  blockedReason?: string;
  /** 合并卡: a failed second step's 按现价重新设 / 问 AI send their prompt as the user's message. */
  onAsk?: (text: string) => void;
  /** The card's own account when it is not the conversation's current one (#1011), under the title. */
  accountNote?: string;
}

/** One line — 买入开仓 螺纹钢2701 2 手 @ 3126 限价 — a status pill and the two buttons; the rest folds. */
export function TradeProposalCard({
  proposal,
  busy,
  id,
  orders,
  onAdjustRisk,
  onConfirm,
  onReject,
  onAsk,
  blockedReason,
  accountNote
}: TradeProposalCardProps) {
  const close = proposal.offset && proposal.offset !== "OPEN";
  const actionLabel = proposal.side === "buy_open" ? "买入开仓"
    : proposal.side === "sell_open" ? "卖出开仓"
      : proposal.side === "buy_close" ? "买入平仓" : "卖出平仓";
  const bucketLabel = proposal.offset === "CLOSETODAY" ? "平今"
    : close && ["SHFE", "INE"].includes(proposal.symbol.split(".")[0]) ? "平昨" : "平仓";
  const recorded = proposal.status === "candidate_recorded";
  const rejected = proposal.status === "rejected";
  const voiceSecondsLeft = useVoiceConfirmCountdown(proposal.voiceConfirmBy, proposal.status === "approval_required");
  const riskPassed = proposal.riskCheck.passed;
  const canAdjust = !riskPassed && (
    proposal.riskCheck.checks === undefined
    || (
      proposal.riskCheck.checks.perTradeLoss === false
      && proposal.riskCheck.checks.symbolAllowed
      && proposal.riskCheck.checks.dailyLoss
      && proposal.riskCheck.checks.openingTime
    )
  );
  const lossExceeded = proposal.riskCheck.checks === undefined
    ? proposal.riskCheck.estimatedLoss > proposal.riskCheck.limit
    : proposal.riskCheck.checks.perTradeLoss === false;
  // A confirmed card says what its order actually is (the same 口径 as the close-batch card): a
  // dispatch refused after confirmation leaves it 待发送. The orders are the card's own account's (#1005):
  // a card naming its account without its order read yet says only 已确认 — never 已发送 on a guess;
  // a card without an account (an older Runtime) keeps the old reading.
  const fact = recorded ? orders?.find(order => order.proposalId === proposal.proposalId) : undefined;
  const unread = recorded && !fact && Boolean(proposal.accountId) && proposal.executionEligibleAtCreation === true;
  const badge = recorded
    ? fact ? (fact.displayStatus === "待发送" ? "未发送" : orderStatusText(fact))
      : unread ? "已确认" : proposal.executionEligibleAtCreation ? "已发送" : "已记录"
    : rejected
      ? closedBadge(proposal.closedReason)
      : voiceSecondsLeft !== undefined
        ? voiceConfirmLabel(voiceSecondsLeft)
      : riskPassed
        ? "等待确认"
        : canAdjust ? "需要调整" : "风险未通过";
  // 合并卡 (2026-10-06): a close card carrying the remaining position's new stop / take reads as two
  // steps; the card's state is both steps' (已完成 / 部分完成), each step says its own result.
  const plan = close ? proposal.remainingProtection : undefined;
  const planBadge = plan && recorded
    ? plan.status === "done" ? "已完成" : plan.status === "failed" || plan.status === "skipped" ? "部分完成" : undefined
    : undefined;
  const closeStep: CardStep | undefined = plan ? {
    title: `${proposal.side === "sell_close" ? "平多" : "平空"} ${proposal.volume} 手 @ ${priceWords(proposal.limitPrice)} 限价`,
    detail: recorded ? (fact ? `委托${orderStatusText(fact)}` : "已确认") : "确认后先平仓",
    outcome: recorded && fact ? { label: orderStatusText(fact), tone: fact.displayStatus === "已成交" ? "ok" : SENT_STATUSES.has(fact.displayStatus) ? "wait" : "fail" } : undefined,
  } : undefined;
  const planResult = plan ? remainingResult(plan, proposal.volume) : null;
  // Only an order that went through (or a card without an order fact: the old 已发送 / 已记录) turns into the
  // green outcome row; 未发送 / 结果未知 / 已拒绝 / 已撤销 stay a pill in the head.
  const outcomeOk = recorded && !unread && (!fact || ACCEPTED_STATUSES.has(fact.displayStatus))
    && (!planBadge || planBadge === "已完成");
  const price = Number.isInteger(proposal.limitPrice) ? String(proposal.limitPrice) : proposal.limitPrice.toFixed(2);
  // 外盘二期：美元子账户的卡按美元写。
  const money = proposal.currency === "USD" ? dollars : currency;
  const lossLabel = proposal.riskCheck.checks?.lossEstimateAvailable === false ? "未设置止损" : money.format(proposal.riskCheck.estimatedLoss);
  // The “钱”估算 an opening card carries: display-only, never a rejection condition (missing → —).
  const estimateYuan = (value: number | null | undefined) =>
    value === null || value === undefined ? "—" : money.format(value);

  return (
    <CardShell
      icon={<ArrowUpRight />}
      title={`${actionLabel}${close ? `(${bucketLabel})` : ""} ${proposal.name} ${proposal.volume} 手 @ ${price} 限价`}
      heading={<><span className="trade-side" data-direction={proposal.side.startsWith("buy") ? "buy" : "sell"}>{actionLabel}{close ? `(${bucketLabel})` : ""}</span>
        {" "}{proposal.name} <span className="trade-terms">{proposal.volume} 手 @ {price} 限价</span></>}
      className="trade-proposal-card"
      meta={accountNote}
      badge={outcomeOk ? undefined : planBadge ?? badge}
      badgeTone={planBadge === "部分完成" || (fact && !SENT_STATUSES.has(fact.displayStatus)) ? "warning" : recorded || riskPassed ? "positive" : rejected ? "neutral" : "warning"}
      id={id}
      phase={outcomeOk ? "done" : rejected ? "dropped" : busy ? "busy" : "open"}
      testId="trade-proposal-card"
      details={
        <dl className="card-facts">
          <div><dt>合约</dt><dd>{proposal.symbol}</dd></div>
          {/* 下单顺带保护：用户自己给的止损/止盈顶替风控参考价一行（估算亏损也按用户止损算）。 */}
          {close ? null : proposal.protection ? (
            <div><dt>保护</dt><dd data-testid="trade-protection-line">{
              [
                proposal.protection.stopLossPrice !== undefined ? `止损 ${proposal.protection.stopLossPrice}` : null,
                proposal.protection.takeProfitPrice !== undefined ? `止盈 ${proposal.protection.takeProfitPrice}` : null
              ].filter(Boolean).join(" / ")
            } · 成交后自动挂上</dd></div>
          ) : <div><dt>风控参考价</dt><dd>{proposal.stopPrice?.toFixed(2) ?? "—"}</dd></div>}
          {close ? null : <div><dt>最大预计亏损</dt><dd>{lossLabel}</dd></div>}
          <div><dt>风控</dt><dd>{proposal.riskCheck.reason}</dd></div>
          <div><dt>生成时间</dt><dd>{proposal.updatedAt.slice(11, 19) || "刚刚"}</dd></div>
          {proposal.auditId ? <div><dt>审计记录</dt><dd>{proposal.auditId.slice(0, 8)} · 订单状态以{proposal.accountLabel === "本站模拟账户" ? "本站模拟" : "快期"}查询结果为准</dd></div> : null}
        </dl>
      }
      actions={
        // Sent / recorded: the button row becomes the outcome (the same status text, from the card's own data).
        outcomeOk ? <span className="card-outcome" role="status"><Check aria-hidden="true" />{planBadge ?? badge}</span>
          : recorded && plan?.status === "failed" && plan.actions?.length
            ? <CardActionButtons actions={plan.actions} handlers={{ askAi: onAsk }} />
          : recorded || rejected ? null : (
          <>
            <button className="ghost-button danger" type="button" onClick={onReject} disabled={busy}>放弃</button>
            {canAdjust ? (
              <button className="primary-button" type="button" onClick={onAdjustRisk} disabled={busy}>
                按风险上限调整
              </button>
            ) : null}
            <button
              className="primary-button accent"
              type="button"
              onClick={onConfirm}
              disabled={busy || !riskPassed || !proposal.approvalId || Boolean(blockedReason)}
              data-testid="confirm-trade"
            >
              {busy ? "发送中" : "确认"}
            </button>
            {blockedReason ? <small className="card-blocked-reason" role="note">{blockedReason}</small> : null}
          </>
        )
      }
    >
      {proposal.targetPosition ? (
        <p className="target-plan-line" data-testid="target-plan-line">
          <span>目标持仓：{proposal.targetPosition.summary}</span>
          {proposal.targetPosition.totalSteps && proposal.targetPosition.totalSteps > 1 ? (
            <small>{proposal.targetPosition.note ?? `第 ${proposal.targetPosition.step ?? 1} 步 / 共 ${proposal.targetPosition.totalSteps} 步`}</small>
          ) : null}
        </p>
      ) : null}
      {plan && closeStep ? (
        <div className="sc-step-block" data-testid="merged-close-steps" data-status={plan.status}>
          <CardSteps label="两步" steps={[closeStep, remainingStep(plan)]} />
          {planResult ? <p className="sc-step-result" data-tone={planResult.tone} role="status">{planResult.text}</p>
            : recorded ? null : <p className="sc-step-note">先平仓；平仓成交后再改剩下仓位的止损止盈，一次确认两步都做</p>}
          {plan.strategyNote ? <p className="sc-step-note" data-testid="strategy-protection-note">{plan.strategyNote}</p> : null}
        </div>
      ) : null}
      {proposal.strategySource ? (
        <p className="target-plan-line" data-testid="strategy-source-line">
          <span>来自策略：{proposal.strategySource.name}</span>
          <small>策略信号出的确认卡，确认后才下单</small>
        </p>
      ) : null}
      {close ? null : proposal.estimate ? (
        <p className="target-plan-line" data-testid="estimate-line">
          <span>保证金约 {estimateYuan(proposal.estimate.margin)} · 手续费约 {proposal.estimate.fee === null || proposal.estimate.fee === undefined ? "—" : `${proposal.currency === "USD" ? "$" : "¥"}${formatFee(proposal.estimate.fee)}`}
            {proposal.estimate.maxOpenVolume === null ? "" : ` · 可开 ${proposal.estimate.maxOpenVolume} 手`}</span>
          {proposal.estimate.insufficient
            ? <strong className="estimate-short">可用资金不够开 {proposal.volume} 手</strong>
            : <small>{proposal.estimate.basis}</small>}
        </p>
      ) : null}
      {recorded && fact?.displayStatus === "待发送" && notSentReason(fact.lastReadError) ? (
        <p className="target-plan-line" data-testid="trade-unsent-reason">
          <span>{notSentReason(fact.lastReadError)}</span>
        </p>
      ) : null}
      {proposal.confirmBlocked && proposal.status === "approval_required" ? (
        <div className="risk-box" role="alert" data-testid="strategy-card-blocked">
          <span>{proposal.confirmBlocked.reason}</span>
          <strong>未发单</strong>
        </div>
      ) : null}
      {riskPassed ? null : (
        <div className="risk-box" data-testid="trade-risk-result">
          <span>{proposal.riskCheck.reason}</span>
          <strong>{lossExceeded
            ? `超出 ${currency.format(Math.max(0, proposal.riskCheck.estimatedLoss - proposal.riskCheck.limit))}`
            : "规则未通过"}</strong>
        </div>
      )}
    </CardShell>
  );
}

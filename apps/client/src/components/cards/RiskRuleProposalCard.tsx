import { ShieldCheck } from "lucide-react";
import type { RiskRuleProposal } from "@ai-trader/ui-events";
import { CardShell } from "../CardShell";

interface Props {
  proposal: RiskRuleProposal;
  busy: boolean;
  id?: string;
  onConfirm: () => void;
  /** Why 确认 cannot be pressed now (account / gateway): shown beside it. */
  blockedReason?: string;
  onReject: () => void;
}

const money = (value: string) => `¥${Number(value).toLocaleString("zh-CN")}`;

export function RiskRuleProposalCard({ proposal, busy, id, onConfirm, onReject, blockedReason }: Props) {
  const applied = proposal.status === "risk_rules_applied";
  const rejected = proposal.status === "rejected";
  return (
    <CardShell
      icon={<ShieldCheck />}
      title={`风险规则 v${proposal.basePolicyVersion} → v${proposal.policyVersion}`}
      badge={applied ? "已生效" : rejected ? "已拒绝" : "等待确认"}
      badgeTone={applied ? "positive" : rejected ? "neutral" : "warning"}
      id={id}
      phase={applied ? "done" : rejected ? "dropped" : busy ? "busy" : "open"}
      testId="risk-rule-proposal-card"
      details={
        <dl className="card-facts">
          <div><dt>执行</dt><dd>确认后由应用严格执行，AI 无法跳过或伪造检查结果；实盘关闭，不会提交订单。</dd></div>
          <div><dt>规则摘要</dt><dd>{proposal.rulesDigest.slice(0, 12)} · Asia/Shanghai</dd></div>
          {applied ? <div><dt>审计记录</dt><dd>{proposal.auditId?.slice(0, 8)} · 已用于后续候选复核</dd></div> : null}
        </dl>
      }
      actions={applied || rejected ? null : (
        <>
          <button className="ghost-button" type="button" onClick={onReject} disabled={busy}>拒绝</button>
          <button className="primary-button accent" type="button" onClick={onConfirm} disabled={busy || Boolean(blockedReason)} data-testid="confirm-risk-rule">
            确认规则
          </button>
        </>
      )}
    >
      <div className="proposal-grid risk-rule-grid">
        <div className="proposal-field"><span>允许合约</span><strong>{proposal.allowedInstruments.map((item) => item.name).join("、")}</strong></div>
        <div className="proposal-field"><span>单笔最大亏损</span><strong>{money(proposal.maxLossPerTrade)}</strong></div>
        <div className="proposal-field"><span>每日止损</span><strong>{money(proposal.maxDailyLoss)}</strong></div>
        <div className="proposal-field"><span>停止开仓</span><strong>{proposal.noNewPositionsAfter}</strong></div>
      </div>
    </CardShell>
  );
}

import { useEffect, useState } from "react";
import { ListFilter } from "lucide-react";
import type { StrategyExplanation } from "@ai-trader/ui-events";
import type { StrategyMonitorSnapshot } from "../../api";
import { CardShell } from "../CardShell";

const templateLabels = {
  ma_cross: "均线交叉",
  breakout: "区间突破",
  kd_cross: "KD 交叉",
  macd_cross: "MACD 交叉",
  combined: "组合条件"
} as const;

function statusLabel(status: StrategyExplanation["status"]): string {
  if (status === "draft") return "草案待回测";
  if (status === "validating") return "验证中";
  if (status === "ready") return "验证通过·未启动";
  if (status === "monitoring") return "监控中";
  if (status === "waiting") return "等待信号";
  if (status === "running") return "模拟运行中";
  if (status === "archived") return "已归档";
  return "已暂停";
}

const intentActionLabels = {
  hold: "继续等待",
  open_long: "准备开多",
  open_short: "准备开空",
  close_long: "优先平多",
  close_short: "优先平空",
  reduce_long: "降低多仓",
  reduce_short: "降低空仓"
} as const;

/** The 仓位意图 verb: an add reads 加多 / 加空, a 分批止盈 close reads 分批平多 / 分批平空. */
function intentLabel(strategy: StrategyExplanation) {
  const action = strategy.positionIntent?.action;
  if (!action) return "";
  if (strategy.signalSnapshot?.add && (action === "open_long" || action === "open_short"))
    return action === "open_long" ? "准备加多" : "准备加空";
  if (strategy.signalSnapshot?.exitReason === "scale_out" && (action === "close_long" || action === "close_short"))
    return action === "close_long" ? "分批平多" : "分批平空";
  return intentActionLabels[action];
}

const intentStatusLabels = {
  no_action: "无需动作",
  allowed: "风险检查通过",
  blocked: "已被风险规则拦截",
  reduce_only: "仅允许降低风险"
} as const;

const attemptLabels = {
  RESERVED: "准备中",
  SUBMITTED: "已提交",
  PARTIAL: "部分完成",
  NOT_DISPATCHED: "未发送",
  REJECTED: "已拒绝",
  CANCELLED: "已撤销",
  SKIPPED: "已跳过",
  BLOCKED: "已拦截",
  UNKNOWN: "结果未知",
  FAILED: "失败"
} as const;

const attemptActionLabels: Record<string, string> = {
  open_long: "开多",
  open_short: "开空",
  close_long: "平多",
  close_short: "平空",
  reduce_long: "减多",
  reduce_short: "减空",
  hold: "等待"
};

interface StrategyCardProps {
  strategy: StrategyExplanation;
  busy?: boolean;
  id?: string;
  refreshKey?: string;
  onMonitorAction?: (
    strategyId: string,
    action: "start" | "pause"
  ) => Promise<StrategyMonitorSnapshot>;
  onLoadMonitor?: (strategyId: string) => Promise<StrategyMonitorSnapshot>;
}

export function StrategyCard({
  strategy,
  busy = false,
  id,
  refreshKey,
  onMonitorAction,
  onLoadMonitor
}: StrategyCardProps) {
  const [status, setStatus] = useState(strategy.status);
  const [monitor, setMonitor] = useState<StrategyMonitorSnapshot>();
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [localBusy, setLocalBusy] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    setStatus(strategy.status);
    setMonitor(undefined);
    setDetailsOpen(false);
    setError(undefined);
  }, [strategy.strategyId, strategy.status]);

  useEffect(() => {
    if (!refreshKey || !onLoadMonitor || !["monitoring", "paused"].includes(strategy.status)) return;
    let cancelled = false;
    void onLoadMonitor(strategy.strategyId).then((snapshot) => {
      if (cancelled) return;
      setMonitor(snapshot);
      setStatus(snapshot.status);
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [refreshKey]);

  const changeMonitoring = async () => {
    if (!onMonitorAction || (status !== "ready" && status !== "paused" && status !== "monitoring")) return;
    const action = status === "monitoring" ? "pause" : "start";
    setLocalBusy(true);
    setError(undefined);
    try {
      const next = await onMonitorAction(strategy.strategyId, action);
      setMonitor(next);
      setStatus(next.status);
      setDetailsOpen(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "策略状态更新失败");
    } finally {
      setLocalBusy(false);
    }
  };

  const toggleDetails = async () => {
    const nextOpen = !detailsOpen;
    setDetailsOpen(nextOpen);
    if (!nextOpen || !onLoadMonitor) return;
    setLocalBusy(true);
    setError(undefined);
    try {
      const snapshot = await onLoadMonitor(strategy.strategyId);
      setMonitor(snapshot);
      setStatus(snapshot.status);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "策略状态读取失败");
    } finally {
      setLocalBusy(false);
    }
  };

  const canControl = status === "ready" || status === "paused" || status === "monitoring";
  const actionLabel = status === "monitoring" ? "暂停监控" : status === "paused" ? "继续监控" : "启动模拟监控";

  return (
    <CardShell
      icon={<ListFilter />}
      title={`${strategy.name} · ${strategy.symbol}`}
      badge={statusLabel(status)}
      badgeTone={status === "running" || status === "monitoring" ? "positive" : "warning"}
      id={id}
      testId="strategy-card"
      details={
        <>
          {strategy.template && strategy.timeframeSeconds && strategy.riskLimits ? (
            <div className="strategy-facts">
              <div><span>信号</span><strong>{templateLabels[strategy.template]}</strong></div>
              <div><span>周期</span><strong>{strategy.timeframeSeconds === 86400 ? "日线" : `${strategy.timeframeSeconds / 60} 分钟`}</strong></div>
              <div><span>目标仓位</span><strong>{strategy.riskLimits.maxPosition} 手</strong></div>
              <div><span>策略频率</span><strong>{strategy.riskLimits.maxOrdersPerDay} 次开仓/交易日</strong></div>
            </div>
          ) : null}
          <dl className="card-facts">
            <div><dt>版本</dt><dd>{strategy.version}</dd></div>
            {strategy.definitionDigest ? (
              <div><dt>策略指纹</dt><dd>{strategy.definitionDigest.slice(0, 12)} · 实盘关闭 · {status === "monitoring" || status === "running" ? "仅模拟执行" : "未启动交易"}</dd></div>
            ) : null}
            {strategy.validationReportId ? (
              <div><dt>验证报告</dt><dd>{strategy.validationReportId.slice(0, 8)} · {strategy.validationStatus === "passed" ? "已通过" : strategy.validationStatus === "failed" ? "未通过" : "证据尚未完整"}</dd></div>
            ) : null}
            {strategy.intentId ? (
              <div><dt>意图快照</dt><dd>{strategy.intentId.slice(0, 8)} · 只读预览 · 未创建委托</dd></div>
            ) : null}
          </dl>
        </>
      }
      actions={(canControl && onMonitorAction) || onLoadMonitor ? <>
        {onLoadMonitor && (status === "monitoring" || status === "paused") ? (
          <button className="ghost-button" disabled={busy || localBusy} onClick={() => void toggleDetails()} type="button">
            {detailsOpen ? "收起记录" : "运行记录"}
          </button>
        ) : null}
        {canControl && onMonitorAction ? (
          <button className={status === "monitoring" ? "ghost-button" : "primary-button accent"} disabled={busy || localBusy} onClick={() => void changeMonitoring()} type="button">
            {localBusy ? "处理中…" : actionLabel}
          </button>
        ) : null}
      </> : undefined}
    >
      <p className="card-line">{strategy.summary}</p>
      {strategy.revisionOf && strategy.changes ? (
        <div className="strategy-changes" data-testid="strategy-changes">
          <span>改动对比 · 基于 {strategy.revisionOf.name} {strategy.revisionOf.version}（原版本和运行中的实例都不变）</span>
          {strategy.changes.length ? (
            <ul>{strategy.changes.map(change => <li key={change.field}>{change.label}：{String(change.from ?? "—")} → <strong>{String(change.to ?? "—")}</strong></li>)}</ul>
          ) : <small>参数没有变化</small>}
        </div>
      ) : null}
      <div className="strategy-checks">
        {strategy.conditions.map((condition) => (
          <div className={`condition ${condition.status === "passed" ? "ok" : condition.status === "failed" ? "fail" : "wait"}`} key={condition.label}>
            <span className="state">{condition.status === "passed" ? "✓" : condition.status === "failed" ? "×" : "!"}</span>
            <strong>{condition.label}</strong>
            <small>{condition.detail}</small>
          </div>
        ))}
      </div>
      {strategy.signalSnapshot && strategy.positionIntent && strategy.riskAdjustedIntent ? (
        <div className="strategy-intent-flow" data-testid="strategy-intent-flow">
          <section>
            <span>当前信号</span>
            <strong>{strategy.signalSnapshot.signal === "buy" ? "买入" : strategy.signalSnapshot.signal === "sell" ? "卖出" : "等待"}</strong>
            <small>来自上一根已完成 K 线</small>
          </section>
          <section>
            <span>仓位意图</span>
            <strong>{intentLabel(strategy)} {strategy.positionIntent.requestedVolume ? `${strategy.positionIntent.requestedVolume} 手` : ""}</strong>
            <small>当前 多 {strategy.positionIntent.currentLong} / 空 {strategy.positionIntent.currentShort} → 目标 多 {strategy.positionIntent.targetLong} / 空 {strategy.positionIntent.targetShort}</small>
          </section>
          <section>
            <span>风险裁剪</span>
            <strong>{intentStatusLabels[strategy.riskAdjustedIntent.status]}</strong>
            <small>{strategy.riskAdjustedIntent.reason}</small>
          </section>
        </div>
      ) : null}
      {detailsOpen ? (
        <div className="strategy-monitor-detail" data-testid="strategy-monitor-detail">
          <div className="strategy-monitor-summary">
            <span>保护状态</span>
            <strong>{monitor?.monitorState?.pauseReason || (status === "monitoring" ? "正常监控已完成 K 线" : "当前没有运行")}</strong>
            {monitor?.monitorState ? <small>连续平仓毛亏损 {monitor.monitorState.consecutiveLosses} 笔{monitor.monitorState.lastError ? ` · 最近错误：${monitor.monitorState.lastError}` : ""}</small> : null}
          </div>
          {monitor?.recentAttempts.length ? (
            <div className="strategy-attempts">
              {monitor.recentAttempts.slice(0, 3).map((attempt) => (
                <div key={attempt.attemptId}>
                  <span>{attemptLabels[attempt.status]}</span>
                  <strong>{attemptActionLabels[attempt.action] || "模拟操作"} · {attempt.volume} 手</strong>
                  <small>{attempt.detail || "已保存执行身份"}</small>
                </div>
              ))}
            </div>
          ) : <p className="audit-note">还没有模拟执行记录；没有信号时保持安静。</p>}
        </div>
      ) : null}
      {error ? <p className="strategy-monitor-error" role="alert">{error}</p> : null}
    </CardShell>
  );
}

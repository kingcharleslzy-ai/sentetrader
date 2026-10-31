import { strategyStatusText } from "./strategyApi";

type Monitor = { status: string; decision?: { status: string; action?: string; detail?: string | null; checkedAt?: unknown; waitReason?: string | null; stalled?: boolean } | null; monitorState?: { pauseReason?: string | null; lastError?: string | null; updatedAt?: unknown } | null };
export function monitorDisplay(value: Monitor, unavailable = false) {
  if (unavailable) return { label: "运行状态待确认", detail: "连接中断，正在重新读取运行状态", tone: "warning" as const };
  if (value.status !== "monitoring") return { label: value.status === "paused" ? "已暂停" : "尚未启动", detail: value.status === "paused" ? strategyStatusText(value.monitorState?.pauseReason || (["failed", "blocked", "error", "paused"].includes(value.decision?.status || "") ? value.decision?.detail : null) || "策略已暂停") : "等待启动模拟监控", tone: "neutral" as const };
  const checked = value.decision?.checkedAt;
  if (typeof checked === "string" && Date.now() - Date.parse(checked) > 15_000) return { label: "运行回报已过期", detail: "最近未收到新的监控回报，正在确认运行状态", tone: "warning" as const };
  const decision = value.decision?.status === "paused" ? null : value.decision;
  if (decision?.status === "blocked" && decision.detail === "策略仍在冷却周期。") return {label: "冷却等待", detail: "等待后续已收盘 K 线，冷却结束后再判断交易信号。", tone: "neutral" as const};
  // 等待行情 (2026-10-12): data not ready yet is a wait, not a monitor failure; a long one turns warning.
  if (decision?.status === "waiting" && decision.waitReason && decision.waitReason !== "market_session_closed") return {label: "等待行情", detail: strategyStatusText(decision.detail || "等待行情"), tone: decision.stalled ? "warning" as const : "neutral" as const};
  const label = decision?.detail === "market_session_closed" ? "休市等待" : ["error", "retrying"].includes(decision?.status || "") ? "监控异常，等待恢复" : decision?.status === "blocked" ? "交易被拦截" : "模拟监控中";
  const actionLabel = ({open_long:"开多",open_short:"开空",close_long:"平多",close_short:"平空"} as Record<string,string>)[decision?.action || ""];
  return { label: label === "交易被拦截" ? `${actionLabel || "动作待确认"} · 已拦截` : label, detail: strategyStatusText(decision?.detail || ({submitted: "模拟委托已提交，等待成交回报", partial: "模拟委托部分成交", unknown: "委托结果未知，等待原委托回读", rejected: "模拟委托被拒绝", failed: "本次执行未完成，请查看执行记录"} as Record<string, string>)[decision?.status || ""] || "已启动，等待首次运行回报"), tone: label === "模拟监控中" ? "positive" as const : "warning" as const };
}
export function monitorTime(value?: string | number | null) {
  if (!value) return "尚无回报";
  const date = new Date(typeof value === "number" ? value / 1e6 : value);
  if (!Number.isFinite(date.valueOf())) return "时间待确认";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).format(date);
}

const ACTION_LABELS: Record<string, string> = { open_long: "开多", open_short: "开空", close_long: "平多", close_short: "平空" };
const SPREAD_ACTION_LABELS: Record<string, string> = { open_long: "做多价差", open_short: "做空价差", close_long: "平掉做多价差", close_short: "平掉做空价差" };
/** One attempt's act and size: 价差 counts 份 of the spread, a single contract counts 手. */
export function actionText(action: string, volume: number, spread: boolean, separator = " "): string {
  return spread ? `${SPREAD_ACTION_LABELS[action] || "操作"}${separator}${volume} 份` : `${ACTION_LABELS[action] || "操作"}${separator}${volume} 手`;
}

/** `cards`: how many confirmation cards the attempt issued (出卡确认); SKIPPED with cards = 待确认.
 *  `signalAction` alert: a 只提醒 reminder, never an order. */
export function attemptStatusLabel(status: string, cards = 0, signalAction?: string | null) {
  if (status === "SKIPPED" && signalAction === "alert") return "已提醒（未下单）";
  if (status === "SKIPPED" && cards > 0) return "出卡待确认";
  // 放弃 / 超时作废 of the card: the Runtime settles the waiting attempt as CANCELLED.
  if (status === "CANCELLED" && cards > 0) return "卡未确认，未下单";
  return ({RESERVED:"准备执行",SUBMITTED:"已提交委托",PARTIAL:"部分执行",NOT_DISPATCHED:"未发送",REJECTED:"已拒绝",CANCELLED:"已撤销",SKIPPED:"已跳过",BLOCKED:"已拦截",UNKNOWN:"结果待确认",FAILED:"执行失败"} as Record<string,string>)[status] || "状态待确认";
}

/** The four states a user sees; every Runtime status maps onto one of them, detail keeps the finer word. */
export type StrategyState = "草稿" | "已验证" | "运行中" | "已暂停";
export const STRATEGY_STATE_ORDER: StrategyState[] = ["运行中", "已暂停", "已验证", "草稿"];
export function strategyState(status: string | undefined): { label: StrategyState; detail?: string } {
  switch (status) {
    case "monitoring": return { label: "运行中" };
    case "paused": return { label: "已暂停" };
    case "ready": return { label: "已验证" };
    case "validating": return { label: "草稿", detail: "验证中" };
    default: return { label: "草稿" };
  }
}

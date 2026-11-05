import { Radar } from "lucide-react";
import type { PriceAlert } from "../../hooks/usePriceAlerts";
import type { ActivitySnapshot } from "../../runtimeClient";
import "./AiWatching.css";

export type WatchTarget = "reminders" | "strategies" | "checks";

/**
 * AI 正在盯 (改版第二步, owner-approved mockup 2026-10-31; 2026-11-05 only what still waits to fire): one line under the
 * chart saying what is being watched on this contract — its price / line alerts, the condition reminders and strategies
 * running on it (the activity frame the chart already reads, `reminderOnly` telling the two apart) and, while it is held,
 * the position checks that are on. Each item opens where it is managed (我的提醒, 策略 · 正在运行, 通用提醒); what already
 * fired lives in the bell's 提醒记录. Nothing watched: the line says so, like the phone's.
 */
export function AiWatching({ alerts, strategies, checks, onOpen }: {
  alerts: readonly PriceAlert[];
  strategies: ActivitySnapshot["strategies"];
  /** The position checks that are on ("保本", "盘中异动"…), empty while this contract is not held. */
  checks: readonly string[];
  onOpen: (target: WatchTarget) => void;
}) {
  const running = strategies.filter(item => item.status === "monitoring");
  return <nav className="ai-watching" aria-label="AI 正在盯" data-agent-target="ai-watching">
    <span className="ai-watching-title"><Radar aria-hidden="true" />AI 正在盯</span>
    {alerts.map(alert => <button key={alert.id} type="button" onClick={() => onOpen("reminders")}>
      {alert.drawingId ? "碰到画的线" : `${alert.direction === "above" ? "涨到" : "跌到"} ${alert.price}`}<em>提醒中</em>
    </button>)}
    {running.map(item => <button key={item.strategyId} type="button" onClick={() => onOpen(item.reminderOnly ? "reminders" : "strategies")}>
      {item.reminderOnly ? `按条件 · ${item.name}` : item.name}<em>{item.reminderOnly ? "提醒中" : "运行中"}</em>
    </button>)}
    {checks.length ? <button type="button" onClick={() => onOpen("checks")}>持仓 · {checks.join("、")}<em>检查中</em></button> : null}
    {!alerts.length && !running.length && !checks.length ? <span>{NOTHING_WATCHED}</span> : null}
  </nav>;
}

/** Both layouts' empty state. */
export const NOTHING_WATCHED = "没有在等触发的提醒、策略或持仓检查";

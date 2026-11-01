import { BellRing, Radar } from "lucide-react";
import type { PriceAlert } from "../../hooks/usePriceAlerts";
import type { ActivitySnapshot } from "../../runtimeClient";
import "./AiWatching.css";

export type WatchTarget = "reminders" | "strategies" | "history";

/**
 * AI 正在盯 (改版第二步, owner-approved mockup 2026-10-31): one line under the chart saying what is being watched on this
 * contract — its price / line alerts, the condition reminders and strategies running on it (the activity frame the chart
 * already reads, `reminderOnly` telling the two apart) — and how many reminders went out today. Each item opens where it
 * is managed (我的提醒, 策略 · 正在运行, 提醒记录); nothing here changes anything. No line when nothing is watched.
 */
export function AiWatching({ alerts, strategies, todayReminders, onOpen }: {
  alerts: readonly PriceAlert[];
  strategies: ActivitySnapshot["strategies"];
  todayReminders: number;
  onOpen: (target: WatchTarget) => void;
}) {
  const running = strategies.filter(item => item.status === "monitoring");
  if (!alerts.length && !running.length && !todayReminders) return null;
  return <nav className="ai-watching" aria-label="AI 正在盯" data-agent-target="ai-watching">
    <span className="ai-watching-title"><Radar aria-hidden="true" />AI 正在盯</span>
    {alerts.map(alert => <button key={alert.id} type="button" onClick={() => onOpen("reminders")}>
      {alert.drawingId ? "碰到画的线" : `${alert.direction === "above" ? "涨到" : "跌到"} ${alert.price}`}<em>提醒中</em>
    </button>)}
    {running.map(item => <button key={item.strategyId} type="button" onClick={() => onOpen(item.reminderOnly ? "reminders" : "strategies")}>
      {item.name}<em>{item.reminderOnly ? "提醒中" : "运行中"}</em>
    </button>)}
    {todayReminders ? <button type="button" className="ai-watching-today" onClick={() => onOpen("history")}>
      <BellRing aria-hidden="true" />今天提醒过 {todayReminders} 次
    </button> : null}
  </nav>;
}

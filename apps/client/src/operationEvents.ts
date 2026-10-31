import type { ActivitySnapshot } from "./runtimeClient";
import { attemptStatusLabel } from "./strategyMonitorDisplay";
import { strategyStatusText } from "./strategyApi";
import { orderStatusText } from "./components/futures/support";

export type OperationEvent = {
  key: string; eventType: "fill" | "attempt" | "protection" | "order"; eventId: string;
  strategyId?: string | null; symbol: string; time: string | number; group: string;
  title: string; detail: string; warning: boolean; automatic: boolean; version: string;
  triggerVersion?: string;
  tradeIds?: string[];
};
const action = (direction: string, offset: string) => offset === "OPEN"
  ? direction === "BUY" ? "开多" : "开空" : direction === "BUY" ? "平空" : "平多";
export const eventMillis = (time: string | number) => typeof time === "number" ? time / 1e6 : Date.parse(time);
export const protectionStatus = (status: string) => ({ARMED:"保护已挂起，等待触发",TRIGGERED:"保护已触发，等待执行回报",TRIGGERING:"保护已触发，正在平仓",SUBMITTED:"保护平仓已提交",PARTIAL:"保护平仓部分成交",EXECUTED:"保护平仓已执行",FILLED:"保护平仓已成交",CANCELLED:"保护已取消",FAILED:"保护执行失败",UNKNOWN:"保护结果待确认",DISARMED:"保护已解除",SUSPENDED:"保护已暂停"} as Record<string,string>)[status] || "保护状态待确认";

/** 直接保护本轮没核对上原仓、暂不触发的原因 (#528)；不是这类原因返回 null。 */
export const directStopBlockedText = (lastError?: string | null) => {
  if (!lastError?.startsWith("direct_stop_blocked:")) return null;
  const why = ({activity_unavailable:"成交记录暂时读不到",activity_stale:"成交记录不是最新",positions_unavailable:"持仓暂时读不到",fill_untrusted:"这个合约有成交读不懂"} as Record<string,string>)[lastError.slice(20)] || "暂时核对不了原仓";
  return `${why}，暂不触发；恢复后自动核对`;
};

/** 挂着的保护此刻为什么还没平仓：没核对上原仓 (#528)，或已触发但平仓单没派发出去 (#750)。 */
export const protectionIssueText = (lastError?: string | null) => {
  if (lastError === "paper_settlement_pending") return "已触发，正在等交易所公布结算价；公布后按当时价格平仓";
  if (lastError === "order_not_dispatched") return "已触发，平仓单暂未发出；稍后按当时价格重试";
  return directStopBlockedText(lastError);
};

export function operationEvents(activity?: ActivitySnapshot): OperationEvent[] {
  if (!activity) return [];
  const events: Omit<OperationEvent, "version">[] = activity.fills.map(fill => {
    const order=activity.orders.find(o=>o.clientOrderId===fill.clientOrderId);
    return ({
    key: `fill:${fill.tradeId}`, eventType: "fill", eventId: fill.tradeId, strategyId: fill.strategyId,
    symbol: fill.symbol, time: fill.tradeDateTime, group: fill.clientOrderId,
    title: `${fill.source === "stop" ? "保护" : "AI"} ${action(fill.direction, fill.offset)} · ${fill.volume} 手 · 已成交`,
    detail: (fill.price == null ? "成交价格待确认" : `成交价 ${fill.price.toLocaleString("zh-CN")}`) + (order ? ` · 委托回读 ${order.filledVolume}/${order.request.volume} 手 · ${orderStatusText(order)}` : ""),
    triggerVersion: JSON.stringify([fill.tradeId,fill.tradeDateTime,fill.price,fill.volume]),
    warning: false, automatic: true, tradeIds: [fill.tradeId]
  });});
  for (const order of activity.orders) {
    if(activity.fills.some(fill=>fill.clientOrderId===order.clientOrderId)) continue;
    const status = order.submitPhase === "PREPARED" ? order.cancelRequestedAt ? "未发送 · 已请求取消" : "尚未发送" : orderStatusText(order);
    events.push({key:`order:${order.clientOrderId}`,eventType:"order",eventId:order.clientOrderId,strategyId:order.strategyId,
      symbol:order.request.symbol,time:order.updatedAt,group:order.clientOrderId,
      title:`${action(order.request.direction,order.request.offset)} · ${order.filledVolume}/${order.request.volume} 手 · ${status}`,
      detail:`委托价 ${order.request.limitPrice}`,warning:false,automatic:false});
  }
  for (const strategy of activity.strategies) for (const raw of strategy.recentAttempts) {
    const attempt = raw as {attemptId:string;clientOrderId?:string;action:string;volume:number;status:string;detail?:string;updatedAt:string};
    if (!attempt.attemptId) continue;
    if (["SUBMITTED","PARTIAL"].includes(attempt.status) && (activity.fills.some(fill=>fill.clientOrderId===attempt.clientOrderId) || activity.orders.some(order=>order.clientOrderId===attempt.clientOrderId))) continue;
    const name = ({open_long:"开多",open_short:"开空",close_long:"平多",close_short:"平空"} as Record<string,string>)[attempt.action] || "动作待确认";
    const failed = ["FAILED","REJECTED","BLOCKED"].includes(attempt.status);
    events.push({key:`attempt:${attempt.attemptId}`,eventType:"attempt",eventId:attempt.attemptId,strategyId:strategy.strategyId,
      symbol:activity.symbol,time:attempt.updatedAt,group:attempt.clientOrderId || attempt.attemptId,
      title:`${name} · ${attempt.volume} 手 · ${attemptStatusLabel(attempt.status)}`,
      detail:strategyStatusText(attempt.detail || "等待执行回报"),warning:failed,automatic:failed});
  }
  for (const raw of activity.protections || []) {
    const p = raw as {stopId:string;strategyId?:string;symbol:string;status:string;updatedAt:string;triggerPrice:number;remainingVolume?:number;volume:number;lastError?:string};
    if (!p.stopId) continue;
    events.push({key:`protection:${p.stopId}`,eventType:"protection",eventId:p.stopId,strategyId:p.strategyId,
      symbol:p.symbol,time:p.updatedAt,group:p.stopId,title:protectionStatus(p.status),
      detail:`${p.remainingVolume ?? p.volume} 手 · 触发价 ${p.triggerPrice}${p.lastError ? ` · ${protectionIssueText(p.lastError) ?? strategyStatusText(p.lastError)}` : ""}`,
      warning:["FAILED","SUSPENDED"].includes(p.status),automatic:true});
  }
  return events.map(e => ({...e,version:JSON.stringify([e.title,e.detail,e.eventType === "protection" ? null : e.time])}))
    .sort((a,b) => eventMillis(b.time)-eventMillis(a.time));
}

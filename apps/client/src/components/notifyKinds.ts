import { Activity, ArrowLeftRight, BellRing, CalendarClock, ClipboardCheck, Clock, Crosshair, FlaskConical, Gauge, Hourglass, MessagesSquare, Newspaper,
  OctagonAlert, ShieldAlert, ShieldCheck, ShieldOff, Sunrise, type LucideIcon } from "lucide-react";
import type { UserPreferences } from "../userPreferences";

type Notify = UserPreferences["notify"];

/** A reminder kind's tag colour: the status pairs for 交易与账户 (警示 / 危险 / 成交 / 到价 / AI), a
 *  neutral outline for 信息 (开盘前判断 / 研讨 / 简讯) so it never competes with what needs handling. */
export type ReminderTone = "warning" | "danger" | "positive" | "accent" | "alert" | "info";

export interface ReminderKindInfo { label: string; icon: LucideIcon; tone: ReminderTone }

/**
 * 统一提醒 (#932, 2026-10-15): every reminder kind the Runtime records, as the 提醒记录 tags it. The ids
 * are the Runtime's `reminders.kind`; `protection` (the 提醒卡's older kind for 止损止盈) reads as
 * 止损止盈 and an unknown kind as 提醒.
 */
export const REMINDER_KINDS: Record<string, ReminderKindInfo> = {
  stop_triggered: { label: "止损止盈", icon: ShieldAlert, tone: "warning" },
  protection: { label: "止损止盈", icon: ShieldAlert, tone: "warning" },
  protection_cancelled: { label: "保护撤销", icon: ShieldOff, tone: "warning" },
  order_unfilled: { label: "挂单未成交", icon: Hourglass, tone: "warning" },
  // 目标持仓下一步 (the relay's own notice; governed by 挂单未成交's switch).
  target_plan: { label: "目标持仓", icon: Crosshair, tone: "warning" },
  strategy: { label: "策略出卡", icon: FlaskConical, tone: "accent" },
  fill: { label: "成交", icon: ArrowLeftRight, tone: "positive" },
  cancel: { label: "撤单", icon: ArrowLeftRight, tone: "warning" },
  price_alert: { label: "到价", icon: BellRing, tone: "alert" },
  market_close: { label: "收盘前", icon: Clock, tone: "warning" },
  risk_ratio: { label: "风险度", icon: Gauge, tone: "danger" },
  breakeven: { label: "保本", icon: ShieldCheck, tone: "accent" },
  delivery: { label: "交割到期", icon: CalendarClock, tone: "warning" },
  market_movement: { label: "盘中异动", icon: Activity, tone: "warning" },
  daily_review: { label: "每日复盘", icon: ClipboardCheck, tone: "accent" },
  morning_desk: { label: "今日作战表", icon: Sunrise, tone: "info" },
  research_debate: { label: "研讨完成", icon: MessagesSquare, tone: "info" },
  brief: { label: "AI 简讯", icon: Newspaper, tone: "info" },
  emergency_stop: { label: "急停", icon: OctagonAlert, tone: "danger" },
};
const UNKNOWN_KIND: ReminderKindInfo = { label: "提醒", icon: BellRing, tone: "info" };

export function reminderKind(kind: string | null | undefined): ReminderKindInfo {
  return REMINDER_KINDS[(kind ?? "").replace(/-/g, "_")] ?? UNKNOWN_KIND;
}

/**
 * One reminder kind's switch on 设置 → 提醒: it governs every channel of that kind (对话、桌面通知、
 * 语音播报、锁屏推送 and the 提醒记录 record). `key` is the preference the `preference` command names.
 */
export interface NotifyKind {
  /** The reminder kind whose tag the row shows. */
  kind: string;
  label: string;
  key: string;
  /** What the kind is, behind the row's "?". */
  help: string;
  read: (notify: Notify) => boolean;
  patch: (value: boolean) => Record<string, unknown>;
}

const leaf = (path: string[], value: boolean): Record<string, unknown> =>
  path.reduceRight<Record<string, unknown>>((inner, part, index) => ({ [part]: index === path.length - 1 ? value : inner }), {});
/** A switch over one key; `on` is its reading while the document lacks the key (an older Runtime). */
const simple = (kind: string, label: string, key: string, help: string, on = true): NotifyKind => ({
  kind, label, key, help, patch: value => leaf(key.split("."), value),
  read: notify => {
    const value = key.split(".").slice(1).reduce<unknown>((node, part) => (node && typeof node === "object" ? (node as Record<string, unknown>)[part] : undefined), notify);
    return typeof value === "boolean" ? value : on;
  },
});

/** 交易与账户 · 进对话: a card in the conversation plus a row in the 提醒记录 (成交与撤单 only the row, #938). */
export const TRADE_REMINDERS: NotifyKind[] = [
  simple("stop_triggered", "止损止盈触发", "notify.stopTriggered", "保护单触发后出一张卡，带平仓单的最新状态。"),
  simple("protection_cancelled", "保护单撤销", "notify.protectionCancelled", "原仓平完后，跟着它的止损止盈单自动撤掉时告诉你。"),
  simple("order_unfilled", "挂单未成交", "notify.orderUnfilled", "限价单挂了一段时间还没成交、或目标持仓要做下一步时提醒，点开看委托或那张卡。"),
  simple("strategy", "策略出卡与自动下单", "notify.strategySignalAlerts", "策略发出信号、出确认卡、自主下单、暂停或回撤警告时提醒。"),
  { kind: "fill", label: "成交与撤单", key: "notify.desktop.fills", help: "每笔成交、撤单回报。只进提醒记录、不写进对话，点开去委托。",
    read: notify => notify.desktop.fills, patch: value => ({ notify: { desktop: { fills: value, cancels: value } } }) },
  simple("price_alert", "到价提醒", "notify.desktop.priceAlerts", "你设的价格到了。"),
  simple("market_close", "收盘前持仓", "notify.marketCloseReminder", "日盘、夜盘收盘前 5 分钟列出还没平的持仓。", false),
  simple("risk_ratio", "风险度", "notify.riskReminder", "账户风险度升到 80% 时提醒，每个交易日一次。"),
  simple("breakeven", "保本提醒", "notify.breakEven", "挂了止损的持仓赚到设定的门槛时，问你要不要把止损移到成本。"),
  simple("delivery", "交割到期", "notify.delivery", "持仓合约临近个人户最后平仓日。"),
  simple("market_movement", "盘中异动", "notify.movement", "持仓合约短时间大涨大跌或放量时提醒，每分钟看一次。", false),
  simple("daily_review", "每日复盘", "notify.dailyReview", "有成交的交易日 15:15 出今日复盘；外盘（美元子账户）在 CME 收盘后出外盘复盘（北京时间 05:15，冬令时 06:15）。"),
];

/** 信息 · 只进提醒记录: no conversation message, only the 提醒记录 and the lock screen. */
export const INFO_REMINDERS: NotifyKind[] = [
  simple("morning_desk", "今日作战表", "notify.morningDesk", "开盘前把你的自选品种按优先级排好：关键价位、消息和可能出现的打法。"),
  simple("research_debate", "研讨完成", "notify.researchDebate", "多 AI 研讨跑完，给出结论。"),
  simple("brief", "AI 简讯", "notify.desktop.briefs", "订阅品种的重要消息。", false),
];

export const BREAK_EVEN_KEY = "notify.breakEven";

export const LOCK_SCREEN_AMOUNTS = simple("brief", "显示金额和手数", "notify.lockScreenAmounts", "关掉后锁屏上只说“有一笔成交”，不露金额和手数。");

/** "10-09 开启" from a subscription's creation time. */
export function openedOn(createdAt: string | undefined): string {
  const date = createdAt ? new Date(createdAt) : undefined;
  if (!date || Number.isNaN(date.getTime())) return "";
  return `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} 开启`;
}

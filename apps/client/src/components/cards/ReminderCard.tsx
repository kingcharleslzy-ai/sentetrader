import { Activity, BellRing, CalendarClock, ClipboardCheck, Clock, Gauge, Hourglass, ShieldAlert, ShieldCheck, type LucideIcon } from "lucide-react";
import type { ReminderCard as Reminder } from "@ai-trader/ui-events";
import { CardShell } from "../CardShell";
import { TagChip } from "../DailyReview";
import { signClass } from "../futures/support";
import { CardActionButtons, cardClock, cardDateTime, type CardActionHandlers } from "./cardActions";
import { CardSteps } from "./ProtectionSteps";
import "./StructuredCards.css";

const KIND: Record<Reminder["kind"], { icon: LucideIcon; label: string; tone: "warning" | "danger" | "neutral" }> = {
  price_alert: { icon: BellRing, label: "到价提醒", tone: "neutral" },
  risk_ratio: { icon: Gauge, label: "风险度提醒", tone: "danger" },
  market_close: { icon: Clock, label: "收盘提醒", tone: "warning" },
  delivery: { icon: CalendarClock, label: "交割提醒", tone: "warning" },
  protection: { icon: ShieldAlert, label: "止损止盈", tone: "warning" },
  order_unfilled: { icon: Hourglass, label: "挂单未成交", tone: "warning" },
  // 盘中异动 (2026-10-06): the Runtime's movement watch; 平一半 only asks the AI for a close card.
  market_movement: { icon: Activity, label: "盘中异动", tone: "warning" },
  // 今日复盘 (2026-10-06): 15:15 after a trading day with fills; 看复盘 opens the review on that day.
  daily_review: { icon: ClipboardCheck, label: "今日复盘", tone: "neutral" },
  // 保本提醒 (2026-10-06): 移到成本 / 不用 answer it through its own route (the user's click, never an order).
  breakeven: { icon: ShieldCheck, label: "保本提醒", tone: "neutral" },
};

const BREAKEVEN_STATE: Record<string, { label: string; tone: "ok" | "fail" | "wait" }> = {
  moved: { label: "已移", tone: "ok" }, declined: { label: "不移", tone: "wait" }, failed: { label: "没移", tone: "fail" },
};

/**
 * 提醒卡: 到价 / 风险度 / 收盘前 / 交割 / 止损止盈（触发或原仓平完撤销）/ 挂单未成交 in one shape — what happened in one
 * line, the figures behind it, and buttons that ask the AI or open a panel. No order button.
 */
export function ReminderCard({ card, id, handlers }: { card: Reminder; id?: string; handlers: CardActionHandlers }) {
  const kind = KIND[card.kind] ?? KIND.price_alert;
  const Icon = kind.icon;
  return (
    <CardShell
      icon={<Icon />}
      title={card.title || kind.label}
      badge={cardClock(card.at)}
      // A notice: settled from the start (enters with the conversation's card motion, nothing to wait for).
      phase="done"
      id={id}
      testId="reminder-card"
      className={`sc-card sc-tone-${kind.tone}`}
      details={
        <dl className="card-facts">
          <div><dt>类型</dt><dd>{kind.label}</dd></div>
          {card.symbol ? <div><dt>合约</dt><dd>{card.symbol}</dd></div> : null}
          <div><dt>时间</dt><dd>{cardDateTime(card.at)}</dd></div>
          <div><dt>卡片</dt><dd>{card.cardId}</dd></div>
        </dl>
      }
      actions={card.actions?.length ? <CardActionButtons actions={card.actions} handlers={handlers} /> : undefined}
    >
      <div className="sc-line">{card.summary}</div>
      {card.kind === "breakeven" && card.step ? <div className="sc-step-block" data-testid="breakeven-step" data-state={card.breakeven?.state ?? "pending"}>
        <CardSteps label="保本提醒的一步" steps={[{ title: card.step.title, detail: card.step.detail ?? undefined, stop: true,
          outcome: BREAKEVEN_STATE[card.breakeven?.state ?? ""] }]} />
        {card.breakeven?.result ? <p className="sc-step-result" role="status"
          data-tone={card.breakeven.state === "moved" ? "ok" : "part"}>{card.breakeven.result}</p> : null}
        {card.note ? <p className="sc-step-note" data-testid="strategy-protection-note">{card.note}</p> : null}
      </div> : card.kind === "daily_review" ? <div className="sc-tags">
        {card.pnl ? <strong className={`sc-review-pnl ${signClass(Number(card.pnl.replace(/[^\d.-]/g, "")))}`}>当日 {card.pnl}</strong> : null}
        {(card.tags ?? []).map(tag => <TagChip key={tag.tag} tag={tag.tag} label={`${tag.label} ${tag.count}`} />)}
      </div> : card.figures?.length ? (
        <dl className="sc-figures">
          {card.figures.map((figure, index) => (
            <div key={`${figure.label}-${index}`} data-tone={figure.tone ?? undefined}><dt>{figure.label}</dt><dd>{figure.value}</dd></div>
          ))}
        </dl>
      ) : null}
    </CardShell>
  );
}

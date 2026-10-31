import { Bot, Check, Workflow } from "lucide-react";
import type { ExecutionCard as Execution } from "@ai-trader/ui-events";
import { CardShell } from "../CardShell";
import { cardDateTime } from "./cardActions";
import "./StructuredCards.css";

/** Badge tone per order state. `unknown` is a warning, never success: the label is shown verbatim. */
const TONE: Record<Execution["status"], "positive" | "neutral" | "warning" | "danger"> = {
  filled: "positive", armed: "positive",
  submitted: "neutral", partial: "neutral",
  unfilled: "warning", failed: "warning", unknown: "warning",
  rejected: "danger",
};

/**
 * 执行回报卡: what the AI (自主执行) or a strategy (自动下单) sent, as read back from the account;
 * later fills and cancels replace it in place under the same cardId.
 */
export function ExecutionCard({ card, id }: { card: Execution; id?: string }) {
  const tone = TONE[card.status] ?? "warning";
  // Like a proposal card's outcome rule: only a fill (or an armed stop) turns into the green outcome
  // row and the "done" phase; 结果未知 / 未成交 / 已拒绝 keep their warning or neutral head badge.
  const ok = card.status === "filled" || card.status === "armed";
  const title = `${card.sourceLabel} · ${card.sideLabel} ${card.name} ${card.volume} 手${card.price ? ` @ ${card.price}` : ""}`;
  const showFilled = card.filledVolume !== null && card.filledVolume !== undefined;
  return (
    <CardShell
      icon={card.source === "strategy" ? <Workflow /> : <Bot />}
      title={title}
      badge={ok ? undefined : card.statusLabel}
      badgeTone={tone}
      phase={ok ? "done" : "open"}
      actions={ok ? <span className="card-outcome" role="status"><Check aria-hidden="true" />{card.statusLabel}</span> : undefined}
      id={id}
      testId="execution-card"
      className={`sc-card sc-exec sc-tone-${tone}`}
      details={
        <dl className="card-facts">
          <div><dt>合约</dt><dd>{card.symbol}</dd></div>
          {card.clientOrderId ? <div><dt>委托编号</dt><dd>{card.clientOrderId}</dd></div> : null}
          <div><dt>类型</dt><dd>{card.orderKind === "stop" ? "止损止盈单" : "委托"}</dd></div>
          <div><dt>更新时间</dt><dd>{cardDateTime(card.updatedAt)}</dd></div>
        </dl>
      }
    >
      {card.reason || showFilled || card.detail ? (
        <dl className="card-facts">
          {card.reason ? <div><dt>理由</dt><dd>{card.reason}</dd></div> : null}
          {showFilled ? <div><dt>已成交</dt><dd>{card.filledVolume}/{card.volume} 手</dd></div> : null}
          {card.detail ? <div><dt>说明</dt><dd>{card.detail}</dd></div> : null}
        </dl>
      ) : null}
    </CardShell>
  );
}

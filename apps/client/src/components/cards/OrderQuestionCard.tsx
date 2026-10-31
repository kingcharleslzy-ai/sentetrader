import { CircleHelp, FileX } from "lucide-react";
import type { OrderQuestionCard as OrderQuestion } from "@ai-trader/ui-events";
import { CardShell } from "../CardShell";
import { CardActionButtons, cardDateTime, type CardActionHandlers } from "./cardActions";
import "./StructuredCards.css";

/** The order field a question is missing, in the user's words (unknown names pass through). */
const FIELD_LABELS: Record<string, string> = {
  direction: "方向", side: "方向", volume: "手数", symbol: "合约", contract: "合约", price: "价格",
  limitPrice: "价格", stop: "止损", stopPrice: "止损", take: "止盈", offset: "开平", account: "账户",
  // The planner's slot names (order_planner Ask.missing, #932 P2: shown raw as dir / product / act).
  dir: "方向", vol: "手数", product: "品种", act: "操作",
};
const fieldLabel = (name: string) => FIELD_LABELS[name] ?? name;

/**
 * 下单类回合的结构化结局: `ask` = the order is missing something — each option sends its prompt as
 * the user's reply; `no_card` = the Runtime says no proposal card could be made, with the user's
 * own words quoted and a way forward (e.g. 打开下单台). Never an order button.
 */
export function OrderQuestionCard({ card, id, handlers }: { card: OrderQuestion; id?: string; handlers: CardActionHandlers }) {
  const ask = card.kind === "ask";
  const answered = ask && card.answered === true;
  const missing = card.missing ?? [];
  return (
    <CardShell
      icon={ask ? <CircleHelp /> : <FileX />}
      title={card.title || (ask ? "需要确认" : "没能生成方案卡")}
      badge={answered ? "已回答" : ask ? "待你回答" : "未出卡"}
      badgeTone="warning"
      // Waiting for the user's answer; the republished answer state settles it (motion only).
      phase={answered ? "done" : "open"}
      id={id}
      testId="order-question-card"
      className="sc-card"
      details={
        <dl className="card-facts">
          <div><dt>卡片</dt><dd>{card.cardId}</dd></div>
          {missing.length ? <div><dt>缺少</dt><dd>{missing.map(fieldLabel).join("、")}</dd></div> : null}
          <div><dt>时间</dt><dd>{cardDateTime(answered ? (card.answeredAt ?? card.createdAt) : card.createdAt)}</dd></div>
        </dl>
      }
      actions={card.actions?.length ? <CardActionButtons actions={card.actions} handlers={handlers} /> : undefined}
    >
      <div className="sc-line">{card.question}</div>
      {ask && missing.length ? (
        <div className="sc-tags" aria-label="缺少的信息"><span className="sc-sub">缺少</span>{missing.map(name => <span className="sc-tag" key={name}>{fieldLabel(name)}</span>)}</div>
      ) : null}
      {answered ? (
        <div className="sc-sub" data-testid="order-question-answered">
          {card.answer ? `已回答：${card.answer}` : "已回答"}
        </div>
      ) : null}
      {!ask && card.reason ? <div className="sc-sub">原因：{card.reason}</div> : null}
      {!ask && card.userText ? <blockquote className="sc-quote" aria-label="你说的">{card.userText}</blockquote> : null}
      {ask && card.options?.length ? (
        <div className="sc-options">
          {card.options.map(option => (
            <button type="button" className="sc-option" key={option.label} aria-label={`回答：${option.label}`}
              title={`发送：${option.prompt}`} disabled={answered || !handlers.askAi} onClick={() => handlers.askAi?.(option.prompt)}>
              {option.label}
            </button>
          ))}
        </div>
      ) : null}
    </CardShell>
  );
}

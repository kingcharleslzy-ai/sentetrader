import { Scale } from "lucide-react";
import type { JudgmentLine, ResearchJudgmentCard as Judgment } from "@ai-trader/ui-events";
import { CardShell } from "../CardShell";
import { CardActionButtons, cardClock, cardDateTime, type CardActionHandlers } from "./cardActions";
import "./StructuredCards.css";

function Lines({ title, lines, className }: { title: string; lines?: JudgmentLine[]; className?: string }) {
  if (!lines?.length) return null;
  return (
    <section className="sc-section" aria-label={title}>
      <h4>{title}</h4>
      <ul className={className ? `sc-lines ${className}` : "sc-lines"}>
        {lines.map((line, index) => (
          <li key={`${line.text}-${index}`}>
            <span>{line.text}{line.quote ? <> <q>{line.quote}</q></> : null}</span>
            {line.source || line.asOf ? <small>{[line.source, line.asOf].filter(Boolean).join(" · ")}</small> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * 判断卡: the research desk's judgment of the product today, shown above the model's answer.
 * `pending` is replaced in place (same cardId) once the judgment is ready.
 */
export function ResearchJudgmentCard({ card, id, handlers }: { card: Judgment; id?: string; handlers: CardActionHandlers }) {
  const pending = card.status === "pending";
  const ready = card.status === "ready";
  return (
    <CardShell
      icon={<Scale />}
      title={`${card.productName} · 今日判断`}
      badge={pending ? "生成中" : ready ? "已生成" : "暂无判断"}
      badgeTone={ready || pending ? "neutral" : "warning"}
      // 生成中 → 完成 settles on screen (the badge pops once); an unavailable judgment dims a little.
      phase={pending ? "busy" : ready ? "done" : "dropped"}
      id={id}
      testId="research-judgment-card"
      className="sc-card"
      details={
        <dl className="card-facts">
          <div><dt>合约</dt><dd>{card.symbol}</dd></div>
          {card.researchDay ? <div><dt>调研日</dt><dd>{card.researchDay}</dd></div> : null}
          {card.generatedAt ? <div><dt>生成时间</dt><dd>{cardDateTime(card.generatedAt)}</dd></div> : null}
          <div><dt>卡片</dt><dd>{card.cardId}</dd></div>
        </dl>
      }
      actions={card.actions?.length ? <CardActionButtons actions={card.actions} handlers={handlers} /> : undefined}
    >
      {pending ? (
        <div className="sc-pending" role="status"><i aria-hidden="true" />{card.note || "今日判断生成中，完成后会在这里更新"}</div>
      ) : ready ? <>
        {/* 第 5 批 (owner 2026-10-25): the sentence, not a 偏多 / 偏空 + 置信 label. */}
        {card.headline ? <div className="sc-stance-row"><strong>{card.headline}</strong></div> : null}
        <Lines title="关键事实" lines={card.facts} />
        <Lines title="失效条件" lines={card.invalidation} className="invalidation" />
        {card.generatedAt ? <div className="sc-foot"><span>生成于 {cardClock(card.generatedAt)}</span>{card.note ? <span>{card.note}</span> : null}</div> : null}
      </> : (
        <div className="sc-sub">{card.note || "今日判断暂时没有生成。"}</div>
      )}
    </CardShell>
  );
}

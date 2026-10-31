import type { ReactNode } from "react";
import { Briefcase, ChartLine, ListChecks, ListOrdered, MessageSquareText, Newspaper, TriangleAlert, type LucideIcon } from "lucide-react";
import type { AnswerBasis, AnswerCard as Answer, AnswerFigure, DataCard as Data } from "@ai-trader/ui-events";
import { CardShell } from "../CardShell";
import { HelpTip, SideBySide, StatTiles } from "../Blocks";
import { CardActionButtons, cardClock, cardDateTime, type CardActionHandlers } from "./cardActions";
import "./StructuredCards.css";
import "./AnswerCard.css";

/** Market direction of a figure / row (涨跌色 preference applies through --market-up / --market-down). */
const dirOf = (dir: "up" | "down" | null | undefined) => dir === "up" || dir === "down" ? dir : undefined;

/** Number cells through the shared StatTiles: label small, value large in its market direction colour with the
 *  unit right after it; the card lets the grid wrap to the column's width (AnswerCard.css). */
export function AnswerFigures({ figures, label }: { figures?: AnswerFigure[]; label: string }) {
  if (!figures?.length) return null;
  return (
    <div className="ac-figures">
      <StatTiles label={label} columns={figures.length > 4 ? 3 : 2} items={figures.map(figure => ({
        label: figure.label,
        value: <span className="ac-value" data-dir={dirOf(figure.dir)}>{figure.value}{figure.unit ? <small>{figure.unit}</small> : null}</span>,
      }))} />
    </div>
  );
}

/** 依据 (folded): when the facts were read and from which reads — written by the Runtime, never the model. */
function BasisFacts({ basis, symbol, cardId }: { basis: AnswerBasis; symbol?: string | null; cardId: string }) {
  return (
    <dl className="card-facts ac-basis">
      <div><dt>读取时间</dt><dd>{cardDateTime(basis.at)}</dd></div>
      {basis.sources.length ? (
        <div><dt>来源</dt><dd><ul className="ac-sources">{basis.sources.map((source, index) => <li key={`${source}-${index}`}>{source}</li>)}</ul></dd></div>
      ) : null}
      {symbol ? <div><dt>合约</dt><dd>{symbol}</dd></div> : null}
      <div><dt>卡片</dt><dd>{cardId}</dd></div>
    </dl>
  );
}

function Heading({ title, helpKey, help }: { title: string; helpKey: string; help: string }) {
  return <span className="ac-heading">{title}<HelpTip helpKey={helpKey} label={title}>{help}</HelpTip></span>;
}

const ANSWER_HELP = "AI 把这次回答整理成一张卡：格子里的数字都来自这一轮读到的行情和账户数据，没读到的不填；依据里写着读取时间和来源。";

/**
 * 回答卡 (回答卡片化, 2026-10-14): an analysis / compare / "咋样" answer as one card — one-line conclusion,
 * number cells and/or a two-sided compare (the Runtime drops a cell its columns already show), up to 3 reasons, one risk line, the folded
 * 依据 and at most two secondary buttons (看图表 / 问 AI 怎么办). Same cardId again replaces it in place.
 * `folded`: the turn's read cards (行情 / 走势 / …) the Runtime folded into this card; they open inside 依据.
 */
export function AnswerCard({ card, id, handlers, folded }: { card: Answer; id?: string; handlers: CardActionHandlers; folded?: ReactNode[] }) {
  const reasons = (card.reasons ?? []).slice(0, 3);
  return (
    <CardShell
      icon={<MessageSquareText />}
      title={card.title}
      heading={<Heading title={card.title} helpKey={`card-answer:${card.cardId}`} help={ANSWER_HELP} />}
      badge={card.warning || cardClock(card.basis.at)}
      badgeTone={card.warning ? "warning" : "neutral"}
      phase="done"
      id={id}
      testId="answer-card"
      className="sc-card ac-card"
      detailsLabel="依据"
      details={<>
        <BasisFacts basis={card.basis} symbol={card.symbol} cardId={card.cardId} />
        {folded?.length ? <div className="ac-folded" role="group" aria-label="原卡" data-testid="answer-folded">{folded}</div> : null}
      </>}
      actions={card.actions?.length ? <CardActionButtons actions={card.actions.slice(0, 2)} handlers={handlers} /> : undefined}
    >
      <p className="ac-conclusion">{card.conclusion}</p>
      <AnswerFigures figures={card.figures} label="关键数字" />
      {card.compare ? (
        <div className="ac-compare" data-testid="answer-compare">
          <SideBySide>
            {[card.compare.left, card.compare.right].map((side, index) => (
              <section key={`${side.title}-${index}`} aria-label={side.title}>
                <h4>{side.title}</h4>
                <ul>{side.lines.map((line, lineIndex) => <li key={`${line}-${lineIndex}`}>{line}</li>)}</ul>
              </section>
            ))}
          </SideBySide>
        </div>
      ) : null}
      {reasons.length ? (
        <ul className="sc-lines ac-reasons" aria-label="理由">
          {reasons.map((reason, index) => <li key={`${reason}-${index}`}><span>{reason}</span></li>)}
        </ul>
      ) : null}
      {card.risk ? <p className="ac-risk" data-testid="answer-risk"><TriangleAlert aria-hidden="true" /><span>{card.risk}</span></p> : null}
    </CardShell>
  );
}

const DATA_KIND: Record<Data["kind"], { icon: LucideIcon; label: string; help: string }> = {
  positions: { icon: Briefcase, label: "持仓", help: "这一轮读到的持仓，浮动盈亏按最新价算，以持仓面板为准。" },
  orders: { icon: ListChecks, label: "挂单与保护", help: "这一轮读到的仍挂着的委托和仍生效的止损止盈。" },
  news: { icon: Newspaper, label: "快讯", help: "第三方快讯，未经核实；偏多/偏空是 AI 对快讯的解读，不是交易建议。" },
  quotes: { icon: ListOrdered, label: "报价总表", help: "报价总表按所选排序列出前几名合约的最新涨跌，数据来自行情源。" },
  analysis: { icon: ChartLine, label: "走势", help: "按最近 K 线确定性计算的涨跌、区间、均价和量比，是行情事实摘要，不是预测。" },
};

/**
 * 数据卡 (回答卡片化, 2026-10-14): a read tool's structured result as a card — 持仓 / 挂单与保护 / 快讯（也是调研的消息面）/
 * 报价总表 / 走势.
 * Built by the Runtime from the tool result; buttons only open a panel or the chart.
 */
export function DataCard({ card, id, handlers }: { card: Data; id?: string; handlers: CardActionHandlers }) {
  const kind = DATA_KIND[card.kind] ?? DATA_KIND.analysis;
  const Icon = kind.icon;
  const rows = card.rows ?? [];
  return (
    <CardShell
      icon={<Icon />}
      title={card.title || kind.label}
      heading={<Heading title={card.title || kind.label} helpKey={`card-data:${card.cardId}`} help={kind.help} />}
      badge={card.warning || cardClock(card.basis.at)}
      badgeTone={card.warning ? "warning" : "neutral"}
      phase="done"
      id={id}
      testId="data-card"
      kind={card.kind}
      className="sc-card dc-card"
      detailsLabel="依据"
      details={<BasisFacts basis={card.basis} symbol={card.symbol} cardId={card.cardId} />}
      actions={card.actions?.length ? <CardActionButtons actions={card.actions.slice(0, 2)} handlers={handlers} /> : undefined}
    >
      <AnswerFigures figures={card.figures} label={`${kind.label}数字`} />
      {rows.length ? (
        <ul className="dc-rows" aria-label={kind.label}>
          {rows.map((row, index) => (
            <li key={`${row.primary}-${index}`}>
              <div className="dc-row-main">
                <span className="dc-primary">{row.primary}{row.tag ? <span className="dc-tag" data-dir={dirOf(row.dir)}>{row.tag}</span> : null}</span>
                {row.secondary ? <small>{row.secondary}</small> : null}
              </div>
              {row.value ? <span className="dc-value" data-dir={dirOf(row.dir)}>{row.value}</span> : null}
            </li>
          ))}
        </ul>
      ) : card.empty ? <p className="dc-empty">{card.empty}</p> : null}
      {card.note ? <p className="dc-note">{card.note}</p> : null}
    </CardShell>
  );
}

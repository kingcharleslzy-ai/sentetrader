import { Activity, Newspaper } from "lucide-react";
import { briefQuestion, type FlowItem } from "../futures/aiFlow";
import "./StructuredCards.css";

const briefTime = (value: number) => new Date(value).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });

/** 快讯 / 行情异动 in the conversation: source, time, contract, the one-line summary and 问 AI. */
export function BriefCard({ item, askAi }: { item: FlowItem; askAi?: (text: string) => void }) {
  const Icon = item.kind === "news" ? Newspaper : Activity;
  const question = briefQuestion(item);
  return (
    <article className="sc-brief" data-kind={item.kind} data-tone={item.tone} data-testid="brief-card" data-card-id={item.id} aria-label={item.title}>
      <div className="sc-brief-head">
        <Icon aria-hidden="true" />
        <strong>{item.title}</strong>
        {item.symbolName ? <span className="sc-brief-symbol" title={item.symbol}>{item.symbolName}</span> : null}
        <time>{briefTime(item.time)}</time>
      </div>
      <p>{item.detail}</p>
      <div className="sc-brief-foot">
        <small>来源：{item.source}</small>
        <button type="button" aria-label={item.kind === "news" ? "问 AI 这条资讯" : "问 AI 这次异动"} title={`发送：${question}`}
          disabled={!askAi} onClick={() => askAi?.(question)}>问 AI</button>
      </div>
    </article>
  );
}

import type { ReactNode } from "react";
import { useDisclosureSurface } from "../uiSurfaces";
import "./NewsScreen.css";

/**
 * 新闻 AI 筛选 (2026-10-07): the Runtime recalls a product's news by its words, drops the ones that only
 * share a name (苹果公司 for 苹果) and lets a cheap model judge the rest. Kept items carry one reading
 * line — event · direction · strength · reason — and the dropped ones fold into one counted row that
 * opens to show them, so a misjudged item is never silently lost. Used by the 快讯 and 资讯 tabs and the
 * phone's 调研 page.
 */
export type NewsAi = { event: string; direction: "偏多" | "偏空" | "中性" | string; strength: "弱" | "中" | "强" | string; reason: string; product?: string;
  /** 第六批: how the news reaches the product, and the event name its reports share. */
  transmission?: string; eventLabel?: string | null };
export type ScreenGroup = { reason: string; count: number };
export type NewsScreenState<T> = {
  /** ai: judged by the model (new items may still be pending); keyword: not AI-screened this time. */
  mode: "ai" | "keyword";
  pending: number;
  /** ai mode: shown items the model failed to judge twice — listed on their keywords, no reading line. */
  unscreened?: number;
  note?: string;
  dropped: { count: number; groups: ScreenGroup[]; items: (T & { dropReason: string; droppedBy: "ai" | "keyword" })[] };
};

const STRENGTH = { 弱: 1, 中: 2, 强: 3 } as Record<string, number>;
export const strengthRank = (ai: NewsAi | null | undefined) => (ai ? STRENGTH[ai.strength] ?? 0 : 0);
const tone = (direction: string) => direction === "偏多" ? "up" : direction === "偏空" ? "down" : "flat";

/** 按影响: the stronger readings first, a story about the product itself before a macro one, then newest. */
export function byImpact<T extends { ai?: NewsAi | null; direct?: boolean; publishedAt: string }>(items: readonly T[]): T[] {
  return [...items].sort((a, b) => strengthRank(b.ai) - strengthRank(a.ai)
    || Number(b.direct !== false) - Number(a.direct !== false)
    || b.publishedAt.localeCompare(a.publishedAt));
}

/** The one reading line under a news item. `compact` (phone) drops the AI tag and the strength word. */
export function AiReading({ ai, compact = false }: { ai: NewsAi; compact?: boolean }) {
  const level = strengthRank(ai);
  return <div className="news-ai" data-compact={compact || undefined} aria-label={`AI 解读：${ai.event}，${ai.direction}，强度${ai.strength}，${ai.reason}`}>
    {compact ? null : <span className="news-ai-who" aria-hidden="true">AI</span>}
    <span className="news-ai-pill" data-kind="event">{ai.event}</span>
    <span className="news-ai-pill" data-tone={tone(ai.direction)}>{ai.direction}</span>
    <span className="news-ai-strength" title={`强度 ${ai.strength}`} aria-hidden="true">
      {[1, 2, 3].map(step => <i key={step} data-on={step <= level || undefined} />)}
      {compact ? null : <span>{ai.strength}</span>}
    </span>
    {ai.reason ? <span className="news-ai-reason">{ai.reason}</span> : null}
  </div>;
}

/** What the reader should know about this screening: not AI-screened this time, some items the model
 *  failed to judge (on their keywords), or still judging. */
export function ScreenNote({ screen }: { screen?: NewsScreenState<unknown> }) {
  if (!screen) return null;
  if (screen.mode === "keyword") return <p className="news-screen-note" role="status" data-mode="keyword">{screen.note ?? "AI 筛选暂不可用；以下按关键词列出，未经 AI 筛选。"}</p>;
  const failed = screen.unscreened ?? 0;
  if (failed <= 0 && screen.pending <= 0) return null;
  return <>
    {failed > 0 ? <p className="news-screen-note" role="status" data-mode="unscreened">{screen.note ?? `${failed} 条 AI 判断失败，按关键词显示，未经 AI 筛选。`}</p> : null}
    {screen.pending > 0 ? <p className="news-screen-note" role="status">AI 正在看新到的 {screen.pending} 条，看完再列出</p> : null}
  </>;
}

/**
 * “AI 筛掉了 N 条：…” — one dashed row with the counts by reason; 看筛掉了什么 lists them under their
 * groups. `name` is the disclosure the UI command and the snapshot use (flash-dropped / news-dropped).
 */
export function DroppedFold<T>({ name, screen, compact = false, renderItem }: {
  name: string; screen?: NewsScreenState<T>; compact?: boolean;
  renderItem: (item: T & { dropReason: string; droppedBy: "ai" | "keyword" }) => ReactNode;
}) {
  const dropped = screen?.dropped;
  const count = dropped?.count ?? 0;
  const [open, setOpen] = useDisclosureSurface(name, { available: count > 0 });
  if (!dropped || !count) return null;
  const summary = dropped.groups.map(group => `${group.reason} ${group.count} 条`).join("，");
  const shown = dropped.items;
  return <div className="news-dropped" data-open={open || undefined}>
    <div className="news-dropped-row">
      <span>{screen?.mode === "keyword" ? "按关键词筛掉了" : "AI 筛掉了"} <b>{count}</b> 条{compact ? "无关" : `无关新闻：${summary}`}</span>
      <button type="button" className="news-dropped-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "收起筛掉的" : compact ? "查看" : "看筛掉了什么"}</button>
    </div>
    {open ? <div className="news-dropped-list">
      {dropped.groups.map(group => {
        const items = shown.filter(item => item.dropReason === group.reason);
        return <section key={group.reason} aria-label={`${group.reason} ${group.count} 条`}>
          <h4>{group.reason} <small>{group.count} 条</small></h4>
          <ul>{items.map((item, index) => <li key={index}>{renderItem(item)}</li>)}</ul>
        </section>;
      })}
      {count > shown.length ? <p className="news-screen-note">只列出最近 {shown.length} 条</p> : null}
    </div> : null}
  </div>;
}

/** 来源 N 个：a · b · c — every source the feed reads; `note` (one short line) follows it. */
export function SourcesLine({ sources, note }: { sources?: readonly string[]; note?: string }) {
  if (!sources?.length) return null;
  return <p className="news-sources">来源 <span>{sources.length} 个</span>：{sources.join(" · ")}{note ? <small> · {note}</small> : null}</p>;
}

import type { ReactNode } from "react";
import { useDisclosureSurface } from "../uiSurfaces";
import { byImpact, strengthRank, type NewsAi } from "./NewsScreen";
import "./NewsEvents.css";

/**
 * 新闻改版 (2026-10-07 第六批, approved sample): the Runtime merges the reports of one story into an
 * event (same product, research day and AI event name) and sorts events into 重要 (strength 中/强),
 * 其他 (弱 with a direction) and 次要 (弱 and 中性, folded). The 快讯 tab also gets the AI news analyst's
 * 今日新闻要点 on top and a 今晚 line of the product's own data. Shared by 快讯, 资讯 and the phone's 调研.
 * Commandable: `disclosure: flash-minor / news-minor` (the 次要 fold), `flash-event / news-event`
 * with `id` = event id (one event's reports) and `flash-orig / news-orig` (`id` = the report's — or,
 * on the phone, the event's — id: a translated headline's folded English original).
 */
export type EventReport = { id: string; source: string; sources: string[]; publishedAt: string; title: string; text: string; link: string | null; origTitle?: string | null; tags: string[] };
export type NewsEvent = {
  id: string; eventLabel: string | null; researchDay: string; publishedAt: string; firstAt: string; title: string; text: string;
  /** The headline report's original English title (translated items): folded behind the Chinese one. */
  origTitle?: string | null;
  /** Every tag its reports carry (交易所 notices mark the event in the screened views too).
   * An events payload from a previous Runtime has no tags field (staggered deploy, rollback): read as none. */
  tags?: string[];
  /** The strongest reading of its reports; direction 分歧 when they point both ways. null: not AI-screened (keywords only). */
  ai: (NewsAi & { transmission?: string; eventLabel?: string | null }) | null;
  tier: "major" | "other" | "minor" | "unscreened"; reports: number; sources: string[]; direct: boolean; items: EventReport[];
};
export type NewsLayers = { major: number; other: number; minor: number; unscreened: number; minorReports: number; minorGroups: { label: string; count: number }[] };
export type BriefPoint = { text: string; direction: string; eventLabel: string; eventId: string; reports: number };
/** 今日新闻要点: ready / stale (the last rewrite failed: the previous one, `failedAt`) / writing (none yet) / none. */
export type NewsBrief = { status: "ready" | "stale" | "writing" | "none"; net?: string; take?: string; points?: BriefPoint[]; watch?: string[];
  basedOn?: number; generatedAt?: string; failedAt?: string; writing?: boolean };
export type TonightEvent = { date: string; time: string; datetime: string; region: string; event: string; importance: number };
/** 多 AI 研讨 (2026-10-11): a product an event is about, with its main contract's live change (a click switches to it). */
export type EventChip = { id: string; name: string; symbol?: string; changePercent?: number | null };
/** What a row adds on the 快讯 tab: its products, the 研讨 button (重要 events) and the 已研讨 tag of today's debate. */
export type EventExtras = { chips?: EventChip[]; onChip?: (symbol: string) => void; onDebate?: () => void;
  debated?: { id: string; status: string; stance?: string | null; at?: string | null }; onOpenDebate?: (id: string) => void };

export const tone = (direction: string | undefined) => direction === "偏多" ? "up" : direction === "偏空" ? "down" : direction === "分歧" ? "split" : "flat";
const CLOCK = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
export const clockOf = (value: string | undefined) => {
  const when = value ? new Date(value) : undefined;
  return when && !Number.isNaN(when.getTime()) ? CLOCK.format(when) : "";
};
/** The line to read for an event: a real headline, else its text (a flash title cut from the body is not one). */
export function eventHeadline(event: Pick<NewsEvent, "title" | "text">): string {
  const title = event.title.replace(/…$/, "").trim();
  return title && !event.text.startsWith(title) ? event.title : event.text || event.title;
}
const safeLink = (value: string | null | undefined) => {
  try { const url = value ? new URL(value) : undefined; return url && ["https:", "http:"].includes(url.protocol) ? url.href : undefined; } catch { return undefined; }
};

/** 偏空 ▮▮▯ — the direction word in its market colour and three strength bars. */
export function Direction({ ai, compact = false }: { ai: NewsEvent["ai"]; compact?: boolean }) {
  if (!ai) return null;
  const level = strengthRank(ai);
  return <span className="news-dir" data-tone={tone(ai.direction)} aria-label={`${ai.direction}，强度${ai.strength}`}>
    {ai.direction}{compact ? ` · ${ai.strength}` : null}
    {compact ? null : <span className="news-bars" aria-hidden="true">{[1, 2, 3].map(step => <i key={step} data-on={step <= level || undefined} />)}</span>}
  </span>;
}

/** The AI news analyst's 今日新闻要点. `compact` (phone): no meta line, no 接下来关注. */
export function BriefBlock({ brief, compact = false }: { brief?: NewsBrief; compact?: boolean }) {
  if (!brief || brief.status === "none") return null;
  if (brief.status === "writing" || !brief.take) {
    return <section className="news-brief" aria-label="今日新闻要点" data-state="writing">
      <div className="news-brief-head"><h3>今日新闻要点</h3></div>
      <p className="news-brief-wait" role="status">AI 新闻分析师正在读今天筛过的新闻…</p>
    </section>;
  }
  const net = brief.net ?? "中性";
  const updated = clockOf(brief.generatedAt);
  return <section className="news-brief" aria-label="今日新闻要点" data-state={brief.status}>
    <div className="news-brief-head">
      <h3>今日新闻要点</h3>
      <span className="news-net" data-tone={tone(net)}>{compact ? net : `新闻面${net}`}</span>
      {compact ? null : <span className="news-brief-meta" title="只根据今天筛过的新闻写；引用不存在的事件或数字对不上的要点已去掉；这不是交易建议。">
        AI 新闻分析师 · {brief.status === "stale" ? `上次更新 ${updated}` : `${updated} 更新`} · 依据 {brief.basedOn ?? 0} 条</span>}
    </div>
    <p className="news-take">{brief.take}</p>
    {brief.points?.length ? <ul className="news-points">
      {brief.points.map(point => {
        const cut = point.text.search(/[：:]/);
        return <li key={point.eventId}>
          <span className="news-dot" data-tone={tone(point.direction)} aria-label={point.direction} />
          <span>{cut > 0 ? <><b>{point.text.slice(0, cut)}</b>{point.text.slice(cut)}</> : <b>{point.text}</b>}
            {compact ? null : <span className="news-cite"> · {point.reports} 家报道</span>}</span>
        </li>;
      })}
    </ul> : null}
    {!compact && brief.watch?.length ? <div className="news-watch"><b>接下来关注</b>{brief.watch.map((item, index) => <span key={index}>{index ? `· ${item}` : item}</span>)}</div> : null}
    {compact && brief.status === "stale" ? <p className="news-brief-wait">上次更新 {updated}</p> : null}
  </section>;
}

/** 今晚: the product's own data still to come before the research day turns. */
export function TonightLine({ events }: { events?: TonightEvent[] }) {
  if (!events?.length) return null;
  const today = events[0].date;
  return <p className="news-tonight" aria-label="今晚的相关数据"><b>今晚</b>
    {events.map((event, index) => <span key={`${event.datetime}-${event.event}`}>{index ? "· " : ""}{event.date !== today || event.time < "08:30" ? "次日 " : ""}{event.time} {event.region}{event.event}</span>)}
  </p>;
}

/** The folded original of a translated report: a small 原文 toggle beside the Chinese headline,
 * opened per report (`flash-orig` / `news-orig`, `id` = the report's — or the event's — id). */
function OrigTitle({ report, surface, id }: { report: { title: string; text: string; origTitle?: string | null };
  surface: "flash" | "news"; id: string }) {
  const orig = report.origTitle?.trim();
  const [open, setOpen] = useDisclosureSurface(`${surface}-orig`, { id, available: Boolean(orig) });
  if (!orig || orig === eventHeadline(report).trim()) return null;
  return <>
    <button type="button" className="news-ev-orig-open" aria-expanded={open} aria-label={`原文 ${open ? "收起" : "展开"}`}
      onClick={() => setOpen(!open)}>原文</button>
    {open ? <small className="news-ev-orig">原文：{orig}</small> : null}
  </>;
}

function Reports({ event, surface, timeOf }: { event: NewsEvent; surface: "flash" | "news"; timeOf: (value: string) => string }) {
  return <ul className="news-reports">
    {event.items.map(report => {
      const link = safeLink(report.link);
      return <li key={report.id}>
        <time dateTime={report.publishedAt}>{timeOf(report.publishedAt)}</time>
        <span>{(report.sources.length ? report.sources : [report.source]).join("／")}：{eventHeadline(report)}<OrigTitle report={report} surface={surface} id={report.id} /></span>
        {link ? <a href={link} target="_blank" rel="noopener noreferrer">原文</a> : null}
      </li>;
    })}
  </ul>;
}

/** One event. major (and keyword-only): headline · direction, why, sources with 展开 (every outlet's report
 *  and link); other / minor: one line — the headline links to its report, or “N 家” opens the reports. */
const signed = (value: number) => `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(2)}%`;
const DEBATE_STATUS: Record<string, string> = { queued: "研讨中", running: "研讨中", review: "需复核", failed: "没能完成", cancelled: "已取消" };

/** The row's products (live change, click to switch) and the 已研讨 tag. */
function EventTags({ extras }: { extras: EventExtras }) {
  const { chips = [], debated } = extras;
  if (!chips.length && !debated) return null;
  return <div className="news-ev-tags">
    {chips.map(chip => {
      const change = typeof chip.changePercent === "number" ? chip.changePercent : null;
      const body = <>{chip.name}{change !== null ? <span className="news-ev-change" data-tone={change > 0 ? "up" : change < 0 ? "down" : "flat"}>{signed(change)}</span> : null}</>;
      return chip.symbol && extras.onChip
        ? <button type="button" key={chip.id} className="news-ev-chip" aria-label={`切到 ${chip.name} 主力合约`} title={chip.symbol}
          onClick={() => extras.onChip?.(chip.symbol!)}>{body}</button>
        : <span key={chip.id} className="news-ev-chip">{body}</span>;
    })}
    {debated ? <button type="button" className="news-ev-chip news-ev-debated" aria-label="打开这条快讯的研讨结果" onClick={() => extras.onOpenDebate?.(debated.id)}>
      已研讨 · {DEBATE_STATUS[debated.status] ?? debated.stance ?? "完成"}{debated.at ? ` · ${clockOf(debated.at)}` : ""}</button> : null}
  </div>;
}

export function EventRow({ event, surface, timeOf, compact = false, extras }: { event: NewsEvent; surface: "flash" | "news";
  timeOf: (value: string) => string; compact?: boolean; extras?: EventExtras }) {
  const multi = event.items.length > 1;
  const [open, setOpen] = useDisclosureSurface(`${surface}-event`, { id: event.id, available: multi && !compact });
  const why = event.ai?.transmission || event.ai?.reason || "";
  const single = !multi ? safeLink(event.items[0]?.link) : undefined;
  const sourceNames = event.sources.length ? event.sources : [event.items[0]?.source ?? ""];
  const full = event.tier === "major" || !event.ai;
  const label = `${open ? "收起" : "展开"} ${event.reports} 家报道`;
  const headline = eventHeadline(event);
  // 研讨 (多 AI 研讨): 重要 events only, not yet debated today; on hover with a mouse, always shown on touch.
  const debate = extras?.onDebate && event.tier === "major" && !extras.debated
    ? <button type="button" className="debate-secondary news-ev-debate" aria-label={`研讨：${headline}`} onClick={extras.onDebate}>研讨</button> : null;
  return <li className="news-ev" data-tier={event.tier} data-debatable={debate ? "" : undefined}>
    <time dateTime={event.publishedAt}>{timeOf(event.publishedAt)}</time>
    <div className="news-ev-hl">
      {!full && single && !compact ? <a href={single} target="_blank" rel="noopener noreferrer">{headline}</a> : headline}
      {!full && multi && !compact ? <> <button type="button" className="news-ev-count" aria-expanded={open} aria-label={label}
        onClick={() => setOpen(!open)}>{event.reports} 家</button></> : null}
      {(event.tags ?? []).includes("交易所") ? <span className="news-ev-tag">交易所</span> : null}
      {/* 原始来源（Runtime news_origin 的 `据:` 标签），与 FlashRow 同一小标签。 */}
      {(event.tags ?? []).filter(tag => tag.startsWith("据:")).map(tag => <span key={tag} className="news-ev-tag">据{tag.slice(2)}</span>)}
      {/* A single translated report folds its original here; several fold theirs in the reports —
       * except on the phone (compact), where the reports do not open: the headline's own original. */}
      {!multi ? <OrigTitle report={event.items[0]} surface={surface} id={event.items[0].id} />
        : compact ? <OrigTitle report={{ title: event.title, text: event.text, origTitle: event.origTitle }}
            surface={surface} id={event.id} /> : null}
    </div>
    {debate ? <span className="news-ev-side">{debate}{event.ai ? <Direction ai={event.ai} compact={compact} /> : null}</span>
      : event.ai ? <Direction ai={event.ai} compact={compact} /> : <span />}
    {full && why && !compact ? <div className="news-ev-why">{why}</div> : null}
    {compact ? (multi ? <div className="news-ev-src">{event.reports} 家报道</div> : null)
      : full ? <div className="news-ev-src">
        {multi ? <>{clockOf(event.firstAt) !== clockOf(event.publishedAt) ? `${timeOf(event.firstAt)} 起 ` : ""}{event.reports} 家报道：{sourceNames.slice(0, 4).join("、")}{sourceNames.length > 4 ? "……" : ""}{" "}
          <button type="button" className="news-ev-open" aria-expanded={open} aria-label={label} onClick={() => setOpen(!open)}>{open ? "收起" : "展开"}</button></>
          : <>{sourceNames.join("／")}{single ? <> · <a href={single} target="_blank" rel="noopener noreferrer">原文</a></> : null}</>}
      </div> : null}
    {extras ? <EventTags extras={extras} /> : null}
    {open && multi && !compact ? <div className="news-ev-more">{!full && why ? <p>{why}</p> : null}<Reports event={event} surface={surface} timeOf={timeOf} /></div> : null}
  </li>;
}

function Section({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return <section className="news-sec" aria-label={title}>
    <h4>{title}{hint ? <span>{hint}</span> : null}</h4>
    <ol className="news-ev-list">{children}</ol>
  </section>;
}

/** 收起了 N 条次要消息：欧央行官员表态 4 条、…（影响弱且方向不明）· 展开. */
export function MinorFold({ surface, events, layers, timeOf, compact = false, extra, extrasOf }: { surface: "flash" | "news"; events: NewsEvent[];
  layers?: NewsLayers; timeOf: (value: string) => string; compact?: boolean;
  /** 多 AI 研讨: each row's products, 研讨 button and 已研讨 tag. */
  extrasOf?: (event: NewsEvent) => EventExtras | undefined;
  /** Phone: the counts and rows that fold with it (其他 events, the dropped fold). */
  extra?: { label: string; tail?: string; rows?: NewsEvent[]; after?: ReactNode } }) {
  const minor = events.filter(event => event.tier === "minor");
  const reports = layers?.minorReports ?? minor.reduce((sum, event) => sum + event.items.length, 0);
  const extraCount = extra?.rows?.length ?? 0;
  const [open, setOpen] = useDisclosureSurface(`${surface}-minor`, { available: reports + extraCount > 0 || Boolean(extra?.after) });
  if (!reports && !extraCount && !extra?.after) return null;
  const groups = (layers?.minorGroups ?? []).map(group => `${group.label} ${group.count} 条`).join("、");
  return <div className="news-fold" data-open={open || undefined}>
    <div className="news-fold-row">
      <span>{compact ? [extra?.label, reports ? <>次要 <b>{reports}</b> 条</> : "", extra?.tail].filter(Boolean)
          .map((part, index) => <span key={index}>{index ? " · " : ""}{part}</span>)
        : <>收起了 <b>{reports}</b> 条次要消息{groups ? `：${groups}` : ""}（影响弱且方向不明）</>}</span>
      <button type="button" className="news-fold-open" aria-expanded={open} aria-label={open ? "收起次要消息" : "展开次要消息"}
        onClick={() => setOpen(!open)}>{open ? "收起" : "展开"}</button>
    </div>
    {open ? <>
      {extraCount || minor.length ? <ol className="news-ev-list">
        {[...(extra?.rows ?? []), ...minor].map(event => <EventRow key={event.id} event={event} surface={surface} timeOf={timeOf} compact={compact}
          extras={extrasOf?.(event)} />)}
      </ol> : null}
      {extra?.after}
    </> : null}
  </div>;
}

/** The tiered list: 重要 → 其他 → 未经 AI 筛选 → the folded 次要. `impact` sorts each tier by strength first. */
export function EventLayers({ events, layers, surface, sort = "time", timeOf, keyword = false, extrasOf }: { events: NewsEvent[]; layers?: NewsLayers;
  surface: "flash" | "news"; sort?: "time" | "impact"; timeOf: (value: string) => string;
  /** 多 AI 研讨: each row's products, 研讨 button and 已研讨 tag (the 快讯 tab). */
  extrasOf?: (event: NewsEvent) => EventExtras | undefined;
  /** No model this time: every item on its keywords, one plain list (the note above says so). */
  keyword?: boolean }) {
  const order = (list: NewsEvent[]) => sort === "impact"
    ? [...byImpact(list)].sort((a, b) => strengthRank(b.ai) - strengthRank(a.ai) || b.reports - a.reports) : list;
  const of = (tier: NewsEvent["tier"]) => order(events.filter(event => event.tier === tier));
  const row = (event: NewsEvent) => <EventRow key={event.id} event={event} surface={surface} timeOf={timeOf} extras={extrasOf?.(event)} />;
  if (keyword) return events.length ? <Section title="按关键词列出">{order(events).map(row)}</Section> : null;
  const major = of("major"), other = of("other"), unscreened = of("unscreened");
  return <>
    {major.length ? <Section title="重要" hint="影响中等以上">{major.map(row)}</Section> : null}
    {other.length ? <Section title="其他" hint="影响弱">{other.map(row)}</Section> : null}
    {unscreened.length ? <Section title="未经 AI 筛选" hint="AI 两次判断失败，按关键词列出">{unscreened.map(row)}</Section> : null}
    <MinorFold surface={surface} events={events} layers={layers} timeOf={timeOf} extrasOf={extrasOf} />
  </>;
}

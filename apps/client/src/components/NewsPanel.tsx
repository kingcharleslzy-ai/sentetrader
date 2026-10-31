import { useCallback, useEffect, useRef, useState } from "react";
import { BookOpenText, PanelRightClose } from "lucide-react";
import { guestRequest } from "../guestApi";
import { requestJson, type InstrumentCatalogItem } from "../api";
import { LONG_READ_TIMEOUT_MS } from "../requestTimeout";
import { useUiSurface } from "../uiSurfaces";
import { SectionCard } from "./Blocks";
import { FlashFeed, FLASH_FILTERS, FLASH_SORTS, type FlashFilter, type FlashSort, type FlashState } from "./FlashFeed";
import { EventLayers, eventHeadline, type NewsEvent, type NewsLayers } from "./NewsEvents";
import { AiReading, DroppedFold, ScreenNote, type NewsAi, type NewsScreenState } from "./NewsScreen";
import { BattleSheet, type SheetState } from "./BattleSheet";
import { ResearchCard } from "./ResearchCard";
import { ResearchDesk, type DeskCommand, type DeskState } from "./ResearchDesk";
import { DebateTab, type DebateHost } from "./ResearchDebate";
import "./NewsPanel.css";

type NewsResult = { title: string; link: string; summary: string; media: string; publishDate: string; source?: string; ai?: NewsAi | null };
/** 相关报道 come screened like the 快讯: kept results carry `ai`, `screen` folds the dropped ones. */
type News = { symbol: string; status: "ok" | "stale" | "unavailable"; error?: string; searchedAt?: string; fetchedAt?: string; results: NewsResult[];
  screen?: NewsScreenState<NewsResult>;
  /** 第六批: the kept reports merged by event and tiered like the 快讯 (older Runtimes leave it out). */
  events?: NewsEvent[]; layers?: NewsLayers };
export type NewsTab = "sheet" | "desk" | "debate" | "flash" | "news" | "data";
type Tab = NewsTab;
const NEWS_PENDING_REFRESH_MS = 15_000;
/** 调研 (was 新闻, 2026-10-25): 辩论 moved in from its own rail button, between 品种调研 and 快讯;
 *  今日作战表 (第 5 批) is the first page — the 自选 ranked, for members (guests have no 自选). */
const TABS: [Tab, string][] = [["sheet", "今日作战表"], ["desk", "品种调研"], ["debate", "辩论"], ["flash", "快讯"], ["news", "资讯"], ["data", "研究数据"]];
function newsSource(item: NewsResult) {
  let url: URL | undefined;
  try { const parsed = new URL(item.link); if (["https:", "http:"].includes(parsed.protocol)) url = parsed; } catch { /* Missing or invalid source URL. */ }
  const media = item.media.trim();
  return { url, name: media && !/^ref[_-]?\d+$/i.test(media) ? media : url?.hostname || "智谱搜索",
    linkLabel: url && url.pathname === "/" && !url.search ? "查看来源网站" : "阅读原文" };
}
const BEIJING = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
/** A news publish stamp as one short line: date-only stays "09-26", an instant shows in
 *  Beijing time as "09-30 16:25", anything unparseable is shown as the source gave it. */
export function formatPublishDate(value: string | undefined): string {
  if (!value) return "";
  const date = /^(\d{4})-?(\d{2})-?(\d{2})$/.exec(value);
  if (date) return `${date[2]}-${date[3]}`;
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return value;
  const parts = Object.fromEntries(BEIJING.formatToParts(instant).map(part => [part.type, part.value]));
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}
export type NewsNavigation = { tab?: Tab; pinnedSymbol?: string | null; flashFilter?: FlashFilter; flashSort?: FlashSort;
  /** 调研页 (第七批): press 按最新消息重新判断 / open 资料来源 at one fact id. Both switch to 调研. */
  deskRejudge?: boolean; deskSource?: string };
/** `replay`: news and research are today's, so a replay (looking at a past day) shows none of them. */
export function NewsPanel({ baseUrl, symbol, guest = false, replay = false, onGuestExpired, onClose, watchProducts = [], instruments, onSelectSymbol, debate, tab: shownTab, onTab }: { baseUrl: string | undefined; symbol: string | undefined; guest?: boolean; replay?: boolean;
  /** A guest read answered 401: the session ended; the workspace shows its reconnect prompt. */
  onGuestExpired?: () => void; onClose: () => void;
  /** 快讯 · 持仓与自选: the open positions' and the watchlist's products ("SHFE.au"). */
  watchProducts?: readonly string[];
  /** The contract catalog: a 快讯 product tag names its product and switches the chart to its main contract. */
  instruments?: readonly InstrumentCatalogItem[];
  onSelectSymbol?: (symbol: string) => void;
  /** 多 AI 辩论: the 辩论 tab; a 快讯 row's 研讨 and 今日判断's 让 AI 们辩一轮 open it. Absent: no tab, no 研讨 buttons. */
  debate?: DebateHost;
  /** The shown tab, held by the workspace so a debate started elsewhere can switch to 辩论; absent: the panel's own. */
  tab?: Tab; onTab?: (tab: Tab) => void }) {
  const [ownTab, setOwnTab] = useState<Tab>("desk");
  const tab = shownTab ?? ownTab;
  const setTab = onTab ?? setOwnTab;
  // A tab mounts on its first visit and then stays mounted (hidden), so switching back shows what
  // it already loaded instead of reloading; each tab keeps its own refresh timer.
  const [seen, setSeen] = useState<ReadonlySet<Tab>>(() => new Set<Tab>(["desk", tab]));
  if (!seen.has(tab)) setSeen(new Set([...seen, tab]));
  const newsSeen = seen.has("news");
  const [desk, setDesk] = useState<DeskState>();
  const [sheet, setSheet] = useState<SheetState>();
  const onSheetState = useCallback((state: SheetState) => setSheet(state), []);
  const onDeskState = useCallback((state: DeskState) => setDesk(state), []);
  const [deskCommand, setDeskCommand] = useState<DeskCommand & { nonce: number }>();
  const expired = useRef(onGuestExpired);
  expired.current = onGuestExpired;
  const [pinnedSymbol, setPinnedSymbol] = useState<string | null>(null);
  const readingSymbol = pinnedSymbol ?? symbol;
  const [result, setResult] = useState<News>();
  const [error, setError] = useState("");
  // 快讯 (2026-10-03): guests have no flash route, so the tab is not offered to them; 辩论 needs a member outside a replay.
  const debates = guest || replay ? undefined : debate;
  const offered = (value: Tab) => !(guest && (value === "flash" || value === "sheet")) && (value !== "debate" || Boolean(debates));
  const tabs = TABS.filter(([value]) => offered(value));
  const [flashFilter, setFlashFilter] = useState<FlashFilter>("product");
  const [flashSort, setFlashSort] = useState<FlashSort>("time");
  const [flash, setFlash] = useState<FlashState>();
  const onFlashState = useCallback((state: FlashState) => setFlash(state), []);
  // Never paint the previous product's content while the new effect starts.
  const news = result?.symbol === readingSymbol ? result : undefined;
  const newsState = tab === "news" && !replay && news ? { count: news.results.length, screen: news.screen?.mode ?? null,
    dropped: news.screen?.dropped.count ?? 0, pending: news.screen?.pending ?? 0, unscreened: news.screen?.unscreened ?? 0,
    events: news.events?.length ?? 0, major: news.layers?.major ?? 0, other: news.layers?.other ?? 0, minor: news.layers?.minor ?? 0,
    minorReports: news.layers?.minorReports ?? 0 } : null;
  useUiSurface("news", { state: { tab, pinnedSymbol, symbol: readingSymbol ?? null, replay, flashFilter, flashSort,
    desk: tab === "desk" && !replay ? desk ?? null : null, sheet: tab === "sheet" && !replay ? sheet ?? null : null, flash: tab === "flash" && !replay ? flash ?? null : null, news: newsState }, apply: value => {
    const change = value as NewsNavigation;
    if (change.tab && offered(change.tab)) setTab(change.tab);
    if (change.flashFilter && FLASH_FILTERS.some(([filter]) => filter === change.flashFilter)) {
      setFlashFilter(change.flashFilter);
      if (!change.tab && !guest) setTab("flash");
    }
    if (change.flashSort && FLASH_SORTS.some(([sort]) => sort === change.flashSort)) {
      setFlashSort(change.flashSort);
      if (!change.tab && !guest) setTab("flash");
    }
    if ("pinnedSymbol" in change) setPinnedSymbol(change.pinnedSymbol ?? null);
    if (change.deskRejudge || change.deskSource) {
      setTab("desk");
      setDeskCommand(previous => ({ ...(change.deskSource ? { kind: "source" as const, id: change.deskSource } : { kind: "rejudge" as const }),
        nonce: (previous?.nonce ?? 0) + 1 }));
    }
  } }, [tab, pinnedSymbol, readingSymbol, replay, desk, sheet, flashFilter, flashSort, flash, guest, newsState?.count, newsState?.screen, newsState?.dropped, newsState?.pending, newsState?.unscreened, newsState?.events, newsState?.major,
    newsState?.other, newsState?.minor, newsState?.minorReports]);
  useEffect(() => {
    setResult(undefined); setError("");
    if (!baseUrl || !readingSymbol || !newsSeen || replay) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const path = `/api/${guest ? "guest/" : ""}research/news?symbol=${encodeURIComponent(readingSymbol)}`;
        const next = await (guest ? guestRequest<News>(baseUrl, path, { timeoutMs: LONG_READ_TIMEOUT_MS }) : requestJson<News>(`${baseUrl}${path}`, { timeoutMs: LONG_READ_TIMEOUT_MS }));
        if (!cancelled) { setResult(next); setError(""); }
        // The AI is still reading new reports: read again soon instead of in half an hour.
        if (!cancelled && next.screen?.mode === "ai" && next.screen.pending > 0) { timer = setTimeout(load, NEWS_PENDING_REFRESH_MS); return; }
      } catch (cause) {
        if (cancelled) return;
        if (guest && (cause as { status?: number }).status === 401) { expired.current?.(); return; }
        setError(cause instanceof Error ? cause.message : "新闻加载失败");
      }
      if (!cancelled) timer = setTimeout(load, 30 * 60 * 1000);
    };
    void load();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [baseUrl, readingSymbol, guest, newsSeen, replay]);
  const technicalError = error || news?.error;
  const startEvent = useCallback((event: NewsEvent, eventSymbol?: string) => debates?.start({ eventId: event.id, origin: "flash", title: eventHeadline(event),
    symbol: eventSymbol }), [debates]);
  return <section className="fdw-ai open news-panel" aria-label="调研面板" data-agent-target="panel:news">
    <header><div><BookOpenText /><span><strong>调研</strong></span></div><button type="button" aria-label="关闭调研面板" onClick={onClose}><PanelRightClose /></button></header>
    <div className="news-panel-toolbar"><strong>{readingSymbol ?? "未选择合约"}</strong><button type="button" disabled={!readingSymbol} aria-pressed={Boolean(pinnedSymbol)} onClick={() => setPinnedSymbol(pinnedSymbol ? null : readingSymbol ?? null)}>{pinnedSymbol ? "跟随当前品种" : "固定阅读品种"}</button></div>
    <div role="tablist" aria-label="调研内容">{tabs.map(([value, label]) => <button type="button" role="tab" key={value} aria-selected={tab === value} onClick={() => setTab(value)}>{label}</button>)}</div>
    <div className="news-panel-content" role="tabpanel" aria-label={TABS.find(([value]) => value === tab)?.[1]}>
      {replay ? <p role="status">回放中不显示新闻和研究数据：它们是今天的，会提前透露回放那天之后的行情。</p> : <>
        {seen.has("sheet") && !guest ? <div className="news-panel-tab" hidden={tab !== "sheet"} data-agent-target="news:sheet">
          <BattleSheet baseUrl={baseUrl} visible={tab === "sheet"} onState={onSheetState}
            onOpen={onSelectSymbol ? symbolToOpen => { setPinnedSymbol(null); onSelectSymbol(symbolToOpen); setTab("desk"); } : undefined} />
        </div> : null}
        <div className="news-panel-tab" hidden={tab !== "desk"}>
          {readingSymbol ? <ResearchDesk key={readingSymbol} baseUrl={baseUrl} symbol={readingSymbol} guest={guest} onGuestExpired={onGuestExpired} onState={onDeskState}
            command={deskCommand} onOpenFlash={guest ? undefined : () => { setFlashFilter("product"); setTab("flash"); }}
            onDebate={debates ? () => debates.start({ origin: "desk" }) : undefined} /> : <p>请先选择合约</p>}
        </div>
        {seen.has("debate") && debates ? <div className="news-panel-tab" hidden={tab !== "debate"} data-agent-target="news:debate">
          <DebateTab host={debates} />
        </div> : null}
        {seen.has("flash") && !guest ? <div className="news-panel-tab" hidden={tab !== "flash"} data-agent-target="news:flash">
          <FlashFeed baseUrl={baseUrl} symbol={readingSymbol} visible={tab === "flash"} filter={flashFilter} onFilterChange={setFlashFilter}
            sort={flashSort} onSortChange={setFlashSort}
            watchProducts={watchProducts} instruments={instruments} onSelectSymbol={onSelectSymbol} onState={onFlashState}
            debate={debates ? { onStart: startEvent, onOpen: id => debates.openDebate(id, "flash") } : undefined} />
        </div> : null}
        {seen.has("data") ? <div className="news-panel-tab" hidden={tab !== "data"}>
          <ResearchCard baseUrl={baseUrl} symbol={readingSymbol} replay={false} guest={guest} onGuestExpired={onGuestExpired} />
        </div> : null}
        {newsSeen ? <div className="news-panel-tab" hidden={tab !== "news"}>
          {technicalError ? <p role="alert">{technicalError === "zhipu_mcp_unconfigured" ? "未配置新闻来源" : "新闻暂不可用"}{news?.results.length ? ` · 显示旧值 ${news.searchedAt || news.fetchedAt || "日期未知"}` : ""}</p> : null}
          {!readingSymbol ? <p>请先选择合约</p> : !news && !error ? <p role="status">加载中…</p> : null}
          <ScreenNote screen={news?.screen} />
          {news?.status === "ok" && !news.results.length ? <p>{news.screen?.dropped.count ? "筛过之后没有相关新闻" : "暂无相关新闻"}</p> : null}
          {/* 相关报道 follow the 快讯's tiers: one event per story with every outlet's link. A report shows its
              headline, the AI's “how it reaches the product” and its source — not the summary, which is model input
              (Eastmoney items show the headline and link only, owner decision 2026-09-28). */}
          {news?.events ? <div className="news-events"><EventLayers events={news.events.map(event => ({ ...event, text: "", items: event.items.map(report => ({ ...report, text: "" })),
            sources: event.sources.map(name => newsSource({ media: name, link: "" } as NewsResult).name) }))}
            layers={news.layers} surface="news" timeOf={formatPublishDate} keyword={news.screen?.mode === "keyword"} /></div>
            : news?.results.map((item, index) => { const source = newsSource(item); return <SectionCard key={`${item.link}-${index}`} title={item.title || "未命名新闻"} note={`${item.publishDate ? formatPublishDate(item.publishDate) : "发布时间未提供"} · ${source.name}`}>
              <div className="section-card-body">
                {item.source === "eastmoney" ? null : <p>{item.summary || "来源未提供摘要"}</p>}
                {item.ai ? <AiReading ai={item.ai} /> : null}
                {source.url ? <a href={source.url.href} target="_blank" rel="noopener noreferrer">{source.linkLabel}</a> : null}
              </div>
            </SectionCard>; })}
          <DroppedFold name="news-dropped" screen={news?.screen} renderItem={dropped => <>
            <time>{formatPublishDate(dropped.publishDate)}</time><span>{dropped.title}<small> · {newsSource(dropped).name}</small></span>
          </>} />
        </div> : null}
      </>}
    </div>
  </section>;
}

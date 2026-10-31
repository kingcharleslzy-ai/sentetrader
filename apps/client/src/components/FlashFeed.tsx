import { useEffect, useMemo, useRef, useState } from "react";
import { requestJson, type InstrumentCatalogItem } from "../api";
import { readFlashFeed } from "../flashCache";
import { useMarketBoard } from "../hooks/useMarketBoard";
import { listDebates, researchDayNow, useDebateVersion, type DebateSummary } from "../researchDebate";
import { useDisclosureSurface } from "../uiSurfaces";
import { SectionCard } from "./Blocks";
import { BriefBlock, EventLayers, TonightLine, type EventChip, type EventExtras, type NewsBrief, type NewsEvent, type NewsLayers, type TonightEvent } from "./NewsEvents";
import { DroppedFold, ScreenNote, SourcesLine, type NewsAi, type NewsScreenState } from "./NewsScreen";
import "./FlashFeed.css";

export type FlashFilter = "product" | "watch" | "macro" | "all";
export const FLASH_FILTERS: [FlashFilter, string][] = [["product", "当前品种"], ["watch", "持仓与自选"], ["macro", "宏观"], ["all", "全部"]];
/** 按时间（默认）/ 按影响：AI 判为强、直接讲本品种的在前。 */
export type FlashSort = "time" | "impact";
export const FLASH_SORTS: [FlashSort, string][] = [["time", "按时间"], ["impact", "按影响"]];
/** `ai`: the model's reading for the screened product (null while not judged); `direct`: recalled by the
 *  product's own words (false: a macro item the model kept for it). */
export type FlashItem = { id: string; source: string; sources: string[]; publishedAt: string; title: string; text: string; link: string | null; products: string[]; tags: string[]; origTitle?: string | null;
  ai?: NewsAi | null; direct?: boolean };
export type FlashScreen = NewsScreenState<FlashItem>;
/** The screened views (当前品种 / 持仓与自选) also answer the same items merged into `events` (重要 / 其他 / 次要) with
 *  `layers` counts; 当前品种 adds `brief` (今日新闻要点) and `tonight` (the product's own data before 08:30). */
export type FlashFeedResponse = { status: "ok" | "unavailable"; items: FlashItem[]; error?: string; screen?: FlashScreen; sources?: string[];
  events?: NewsEvent[]; layers?: NewsLayers; brief?: NewsBrief; tonight?: TonightEvent[];
  /** The last background poll (absent until one ran: an empty feed then means “not collected yet”, not “none”). */
  lastPoll?: { at: string; failedSources: string[] } };
/** Why a feed is unavailable, in words: this build has no news at all, or this round's sources all failed (#516). */
export const flashUnavailableText = (error: string | undefined) => error === "research_dependency_missing"
  ? "本机版不提供快讯，云端可用" : "快讯来源这一轮都没读到（抓取失败，不是没有快讯），稍后自动重试";
/** While the model is judging new items, the next read comes this soon instead of in a minute. */
const PENDING_REFRESH_MS = 15_000;
export type CalendarEvent = { date: string; time: string; datetime: string; region: string; event: string; importance: number; actual: number | null; forecast: number | null; previous: number | null };
/** ok: every day of the window answered (no items = a quiet week); stale: some day did not — staleDays
 * show what an earlier round got, missingDays have nothing. */
type CalendarResponse = { status: "ok" | "stale" | "loading" | "unavailable"; items: CalendarEvent[]; missingDays?: string[]; staleDays?: string[];
  /** With `product`: the events of other categories (shown under 宏观 / 全部). */
  otherCount?: number };
/** What the 快讯 tab reports into the news surface's snapshot. */
export type FlashState = { status: "loading" | "ok" | "unavailable" | "error"; count: number; calendarStatus: CalendarResponse["status"] | "error" | null; calendarCount: number;
  /** The last read failed while an earlier one is shown (快讯 / 本周重要数据): the content is not current. */
  readError: boolean; calendarReadError: boolean;
  /** The screened views (当前品种 / 持仓与自选): ai or keyword (未经 AI 筛选), the dropped count, the items still being
   *  judged and the shown items the model failed to judge (on their keywords, 未经 AI 筛选). */
  screen: "ai" | "keyword" | null; dropped: number; pending: number; unscreened: number; sort: FlashSort;
  /** 第六批: merged events by tier (minorReports: the reports folded under 次要), the 今日新闻要点 and the 今晚 count. */
  events: number; major: number; other: number; minor: number; minorReports: number;
  brief: { status: NewsBrief["status"]; net: string | null; generatedAt: string | null; basedOn: number; points: number } | null; tonight: number };

export const FLASH_REFRESH_MS = 60_000;
const SHOWN = 50;
const CALENDAR_FOLDED = 5;

/** "SHFE.au2612" → "SHFE.au": the product key the watchlist and the flash `product` parameter use.
 *  外盘月份合约（CME.GCEZ26）原样：去掉年份会剩下认不出的 CME.GCEZ，Runtime 自己把整个代码映射到品种。 */
export const productOfSymbol = (symbol: string) => /^CME\./i.test(symbol) ? symbol : symbol.replace(/\d+$/, "");
const productId = (product: string) => product.slice(product.lastIndexOf(".") + 1).toLowerCase();
/** 品种和实时涨跌 on the event rows: the 报价总表's main-contract change, re-read this often while the tab shows. */
export const BOARD_MS = 15_000;
const EVENT_CHIPS = 3;

/** A product's main contract in the catalog (else its nearest month) and its short name. */
export function productMain(id: string, instruments: readonly InstrumentCatalogItem[] | undefined) {
  const months = (instruments ?? []).filter(item => item.productId.toLowerCase() === id);
  const main = months.find(item => item.isMain) ?? [...months].sort((a, b) => a.deliveryYear * 100 + a.deliveryMonth - (b.deliveryYear * 100 + b.deliveryMonth))[0];
  return { main, name: main ? main.instrumentName.replace(/\d+$/, "") || main.productId : id.toUpperCase() };
}
const eventOwner = (event: NewsEvent) => event.ai && "product" in event.ai ? String((event.ai as { product?: string }).product ?? "") : "";
/** The contract a row's 研讨 names: the product the row's event was screened for (持仓与自选 lists several), its
 *  main contract — only a pick among the event's products, which the Runtime resolves and checks from the event
 *  id itself (a UI command, voice and agents send the id alone). Undefined: the Runtime decides. */
export const eventSymbol = (event: NewsEvent, instruments: readonly InstrumentCatalogItem[] | undefined) => {
  const owner = eventOwner(event);
  return owner ? productMain(productId(owner), instruments).main?.symbol : undefined;
};
/** 品种和实时涨跌 (多 AI 研讨): an event's products — the one it was screened for, then its reports' own — each with
 *  its main contract and that contract's change on the 报价总表. Shared by the 快讯 tab and the phone's 调研 page. */
export function eventChips(event: NewsEvent, productsOf: ReadonlyMap<string, string[]>, instruments: readonly InstrumentCatalogItem[] | undefined,
  quotes: ReadonlyMap<string, { changePercent?: number | null }>): EventChip[] {
  return eventChipIds(event, productsOf).map(id => {
    const { main, name } = productMain(id, instruments);
    const change = quotes.get(id)?.changePercent;
    return { id, name, symbol: main?.symbol, changePercent: typeof change === "number" ? change : null };
  });
}
function eventChipIds(event: NewsEvent, productsOf: ReadonlyMap<string, string[]>): string[] {
  const owner = eventOwner(event);
  return [...new Set([...(owner ? [productId(owner)] : []), ...event.items.flatMap(item => productsOf.get(item.id) ?? [])])].slice(0, EVENT_CHIPS);
}
/** The products the shown events' chips quote: the only 报价总表 rows the 快讯 asks the push for (2026-10-12). */
export const chipProducts = (events: readonly NewsEvent[], items: readonly { id: string; products: string[] }[]) => {
  const productsOf = new Map(items.map(item => [item.id, item.products]));
  return [...new Set(events.flatMap(event => eventChipIds(event, productsOf)))].sort();
};
/** The 报价总表 rows by product id (lower case), for the event chips. */
export const quotesByProduct = (rows: readonly { productId: string; changePercent?: number | null }[] | undefined) =>
  new Map((rows ?? []).map(row => [String(row.productId).toLowerCase(), row]));

/** Today's debates of the shown events (the 已研讨 tags), re-read when one starts or ends. */
/** Today's debates of the shown events by event id and product key ("au:e123…"): one merged event can concern
 *  several products, and each product's debate is its own (its own 今日判断). */
export const debatedKey = (eventId: string, product: string) => `${product.slice(product.lastIndexOf(".") + 1).toLowerCase()}:${eventId}`;
export function useDebatedEvents(baseUrl: string | undefined, ids: string[], enabled: boolean): Map<string, DebateSummary> {
  const version = useDebateVersion();
  const key = ids.join(",");
  // 08:30 turns the research day while the event ids stay the same (24-hour window): read the tags again (#744).
  // The host re-renders at least every minute (its own refresh), which is when a new day is noticed.
  const day = researchDayNow();
  const [found, setFound] = useState<{ key: string; map: Map<string, DebateSummary> }>();
  useEffect(() => {
    if (!baseUrl || !enabled || !key) return;
    let active = true;
    void listDebates(baseUrl, { events: key.split(","), limit: 60 }).then(result => {
      if (!active) return;
      const map = new Map<string, DebateSummary>();
      for (const item of result.items) {
        const id = item.topic.eventId;
        const slot = id ? debatedKey(id, item.productKey) : "";
        if (id && !map.has(slot) && item.researchDay === result.day && !["failed", "cancelled"].includes(item.status)) map.set(slot, item);
      }
      setFound({ key, map });
    }).catch(() => undefined);
    return () => { active = false; };
  }, [baseUrl, enabled, key, version, day]);
  return found?.key === key ? found.map : new Map();
}

const BEIJING = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", weekday: "short", hourCycle: "h23" });
const beijing = (value: Date) => Object.fromEntries(BEIJING.formatToParts(value).map(part => [part.type, part.value]));
const dayOf = (parts: Record<string, string>) => `${parts.year}-${parts.month}-${parts.day}`;
/** Today's item as "HH:MM", an older one as "MM-DD HH:MM" (Beijing time). */
export function flashTime(publishedAt: string, now = new Date()): string {
  const when = new Date(publishedAt);
  if (Number.isNaN(when.getTime())) return publishedAt;
  const parts = beijing(when);
  const clock = `${parts.hour}:${parts.minute}`;
  return dayOf(parts) === dayOf(beijing(now)) ? clock : `${parts.month}-${parts.day} ${clock}`;
}
function dayLabel(date: string, now: Date): string {
  const parts = beijing(new Date(`${date}T12:00:00+08:00`));
  const today = beijing(now);
  const tomorrow = beijing(new Date(now.getTime() + 86_400_000));
  const prefix = date === dayOf(today) ? "今天 " : date === dayOf(tomorrow) ? "明天 " : "";
  return `${prefix}${parts.month}-${parts.day} ${parts.weekday}`;
}
/** A real headline is shown in bold; a title the server cut from the body itself is not repeated. */
const headline = (item: FlashItem) => {
  const title = item.title.replace(/…$/, "");
  return title && !item.text.startsWith(title) ? item.title : "";
};
/** A title-only source's body is its original title: once translated (origTitle present), the row shows
 * the Chinese headline and the English original stays folded — it is not printed twice as the body. */
const bodyOf = (item: Pick<FlashItem, "text" | "origTitle">) =>
  item.origTitle && item.text.trim() === item.origTitle.trim() ? "" : item.text;
const figure = (value: number | null) => value === null || value === undefined ? "—" : String(value);

export function FlashFeed({ baseUrl, symbol, visible, filter, onFilterChange, sort = "time", onSortChange, watchProducts, instruments, onSelectSymbol, onState, debate }: {
  baseUrl: string | undefined;
  /** The panel's reading contract (the chart's, unless pinned): 当前品种 filters on its product. */
  symbol: string | undefined;
  /** The tab is showing: only then does the feed load and refresh every minute. */
  visible: boolean;
  filter: FlashFilter;
  onFilterChange: (filter: FlashFilter) => void;
  sort?: FlashSort;
  onSortChange?: (sort: FlashSort) => void;
  /** Products of the open positions and the watchlist ("SHFE.au"). */
  watchProducts: readonly string[];
  instruments?: readonly InstrumentCatalogItem[];
  onSelectSymbol?: (symbol: string) => void;
  onState: (state: FlashState) => void;
  /** 多 AI 研讨: start one on a 重要 event, open a debated one. Absent: no 研讨 buttons (guests). */
  debate?: { onStart: (event: NewsEvent, symbol?: string) => void; onOpen: (id: string) => void };
}) {
  const product = symbol ? productOfSymbol(symbol) : undefined;
  const watchIds = useMemo(() => [...new Set(watchProducts.map(productId))].sort(), [watchProducts]);
  const watchKey = watchIds.join(",");
  // One request per filter, all filtered on the server: 当前品种 and 持仓与自选 answer the AI-screened
  // view (kept items with their reading, the dropped ones folded); 宏观 and 全部 are the raw feed.
  const request = filter === "product" ? (product ? `product=${encodeURIComponent(product)}&limit=${SHOWN}` : null)
    : filter === "macro" ? `tag=macro&limit=${SHOWN}`
    : filter === "watch" ? (watchIds.length ? `products=${encodeURIComponent(watchIds.join(","))}&limit=${SHOWN}` : null)
    : `limit=${SHOWN}`;
  const key = `${filter}|${request}|${watchKey}`;
  const [feed, setFeed] = useState<{ key: string; status: FlashFeedResponse["status"]; items: FlashItem[]; fresh: ReadonlySet<string>; screen?: FlashScreen; sources?: string[];
    events?: NewsEvent[]; layers?: NewsLayers; brief?: NewsBrief; tonight?: TonightEvent[]; error?: string; lastPoll?: FlashFeedResponse["lastPoll"] }>();
  const [calendar, setCalendar] = useState<CalendarResponse>();
  const [error, setError] = useState("");
  const [calendarError, setCalendarError] = useState(false);
  const known = useRef(new Map<string, Set<string>>());
  // Two loads of one filter can overlap (the minute timer and the "soon" re-read): only the newest may land (#516).
  const loads = useRef(0);
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    if (!visible || !baseUrl) return;
    let cancelled = false;
    let soon: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      clearTimeout(soon);
      const mine = ++loads.current;
      setNow(new Date());
      const flash = request === null ? Promise.resolve<FlashFeedResponse>({ status: "ok", items: [] })
        : readFlashFeed<FlashFeedResponse>(baseUrl, `hours=24&${request}`);
      // 本周重要数据: 当前品种 lists only the data related to its product; the other views list the whole week.
      const calendarPath = `${baseUrl}/api/research/calendar?days=7${filter === "product" && product ? `&product=${encodeURIComponent(product)}` : ""}`;
      const [flashResult, calendarResult] = await Promise.allSettled([flash, requestJson<CalendarResponse>(calendarPath)]);
      if (cancelled || mine !== loads.current) return;
      if (flashResult.status === "fulfilled") {
        const items = flashResult.value.items.slice(0, SHOWN);
        // A newly arrived item is highlighted briefly; the first read of a filter highlights nothing.
        const before = known.current.get(key);
        known.current.set(key, new Set(items.map(item => item.id)));
        const { screen, events, layers, brief, tonight, sources, error: code, lastPoll } = flashResult.value;
        setFeed({ key, status: flashResult.value.status, items, screen, sources, events, layers, brief, tonight, error: code, lastPoll,
          fresh: new Set(before ? items.filter(item => !before.has(item.id)).map(item => item.id) : []) });
        setError("");
        // The AI is still reading new items, or the analyst is writing the 要点: read again soon.
        if ((screen?.mode === "ai" && screen.pending > 0) || brief?.status === "writing" || brief?.writing) soon = setTimeout(() => { void load(); }, PENDING_REFRESH_MS);
      } else setError(flashResult.reason instanceof Error ? flashResult.reason.message : "快讯加载失败");
      if (calendarResult.status === "fulfilled") { setCalendar(calendarResult.value); setCalendarError(false); } else setCalendarError(true);
    };
    void load();
    const timer = setInterval(() => { void load(); }, FLASH_REFRESH_MS);
    return () => { cancelled = true; clearInterval(timer); clearTimeout(soon); };
    // `key` carries filter, request and the watched products.
  }, [visible, baseUrl, key]); // eslint-disable-line react-hooks/exhaustive-deps

  // Never paint the previous filter's list while the new one loads.
  const current = feed?.key === key ? feed : undefined;
  const unavailable = current?.status === "unavailable";
  const calendarEvents = calendar?.status === "ok" || calendar?.status === "stale" ? calendar.items : [];
  const [calendarOpen, setCalendarOpen] = useDisclosureSurface("flash-calendar");
  const flashStatus: FlashState["status"] = error && !current ? "error" : unavailable ? "unavailable" : current ? "ok" : "loading";
  const calendarStatus: FlashState["calendarStatus"] = calendarError && !calendar ? "error" : calendar?.status ?? null;
  const latest = useRef(onState);
  latest.current = onState;
  const screenMode = current?.screen?.mode ?? null;
  const droppedCount = current?.screen?.dropped.count ?? 0;
  const pending = current?.screen?.pending ?? 0;
  const unscreened = current?.screen?.unscreened ?? 0;
  const layers = current?.layers;
  const brief = current?.brief;
  const briefState = brief && brief.status !== "none" ? JSON.stringify({ status: brief.status, net: brief.net ?? null, generatedAt: brief.generatedAt ?? null,
    basedOn: brief.basedOn ?? 0, points: brief.points?.length ?? 0 }) : "";
  const eventCount = current?.events?.length ?? 0;
  const tonightCount = current?.tonight?.length ?? 0;
  const readError = Boolean(error && current);
  const calendarReadError = Boolean(calendarError && calendar);
  useEffect(() => { latest.current({ status: flashStatus, count: current?.items.length ?? 0, calendarStatus, calendarCount: calendarEvents.length,
    screen: screenMode, dropped: droppedCount, pending, unscreened, sort, events: eventCount, major: layers?.major ?? 0, other: layers?.other ?? 0,
    minor: layers?.minor ?? 0, minorReports: layers?.minorReports ?? 0, brief: briefState ? JSON.parse(briefState) as FlashState["brief"] : null, tonight: tonightCount, readError, calendarReadError }); },
    [flashStatus, current?.items.length, calendarStatus, calendarEvents.length, screenMode, droppedCount, pending, unscreened, sort, eventCount, readError, calendarReadError,
      layers?.major, layers?.other, layers?.minor, layers?.minorReports, briefState, tonightCount]);

  // 多 AI 研讨: the rows' live change and today's 已研讨 tags (hooks stay above the early return).
  const eventsShown = (filter === "product" || filter === "watch") ? current?.events ?? [] : [];
  const board = useMarketBoard(baseUrl, visible && eventsShown.length > 0, BOARD_MS, { products: chipProducts(eventsShown, current?.items ?? []) });
  const debated = useDebatedEvents(baseUrl, eventsShown.map(event => event.id), visible && Boolean(debate));
  // A build without the news sources has no 快讯 and no calendar at all: one line. A cloud round whose sources all
  // failed says so below, and the week's calendar (its own source) still shows (#516).
  if (unavailable && current?.error === "research_dependency_missing") return <p role="status">{flashUnavailableText(current.error)}</p>;

  // Folded: the next events still to come (a day's already-published routine figures would
  // otherwise fill the preview); with nothing left this week, the latest ones.
  const upcoming = calendarEvents.filter(event => new Date(event.datetime).getTime() > now.getTime());
  const shownEvents = calendarOpen ? calendarEvents : (upcoming.length ? upcoming : calendarEvents.slice(-CALENDAR_FOLDED)).slice(0, CALENDAR_FOLDED);
  const days = shownEvents.reduce<[string, CalendarEvent[]][]>((groups, event) => {
    const last = groups.at(-1);
    if (last?.[0] === event.date) last[1].push(event); else groups.push([event.date, [event]]);
    return groups;
  }, []);
  const productName = (id: string) => productMain(id, instruments);
  const screened = filter === "product" || filter === "watch";
  const items = current?.items ?? [];
  const older = items.some(item => flashTime(item.publishedAt, now).length > 5);
  const shortDay = (date: string) => date.slice(5);
  const gaps = calendar?.status === "stale" ? [
    calendar.staleDays?.length ? `${calendar.staleDays.map(shortDay).join("、")} 未刷新，显示上次取到的内容` : "",
    calendar.missingDays?.length ? `${calendar.missingDays.map(shortDay).join("、")} 未取到` : "",
  ].filter(Boolean).join("；") : "";
  const calendarHint = filter === "product" && product ? `未来 7 天 · 与${productName(productId(product)).name}有关的 · 其余在“宏观”` : "未来 7 天 · 中美欧 · 中等重要以上";
  const timeOf = (value: string) => flashTime(value, now);
  const events = current?.events;
  const pendingOnly = screened && current?.screen?.mode === "ai" && !events?.length && (current.screen.pending ?? 0) > 0;
  // An empty answer before any background poll ran is "not collected yet", not "no news".
  const emptyText = current?.lastPoll ? "近 24 小时没有相关快讯" : "快讯还没完成首轮采集，稍后自动刷新";

  const olderEvents = (events ?? []).some(event => timeOf(event.publishedAt).length > 5);
  // 品种和实时涨跌 (多 AI 研讨): each event's products — its reports' own and the product it was screened for.
  const productsOf = new Map((current?.items ?? []).map(item => [item.id, item.products]));
  const quotes = quotesByProduct(board.snapshot?.rows);
  const extrasOf = (event: NewsEvent): EventExtras => {
    const owner = eventOwner(event);
    // The event's own product (else the one being read): another product's debate of the same event is not this row's.
    const summary = debated.get(debatedKey(event.id, owner || product || ""));
    return {
      chips: eventChips(event, productsOf, instruments, quotes),
      onChip: onSelectSymbol,
      // The row's product picks among the event's (the Runtime resolves and checks it from the event id).
      onDebate: debate ? () => debate.onStart(event, eventSymbol(event, instruments)) : undefined,
      debated: summary ? { id: summary.id, status: summary.status, stance: summary.stance, at: summary.finishedAt ?? summary.createdAt } : undefined,
      onOpenDebate: debate?.onOpen,
    };
  };
  return <div className="flash-stack" data-older={olderEvents || undefined}>
    {filter === "product" ? <BriefBlock brief={current?.brief} /> : null}
    <div className="flash-toolbar">
      <div className="flash-filters" role="group" aria-label="快讯筛选">
        {FLASH_FILTERS.map(([value, label]) => <button type="button" key={value} aria-pressed={filter === value} onClick={() => onFilterChange(value)}>{label}</button>)}
      </div>
      {screened ? <div className="flash-sort" role="group" aria-label="快讯排序" title="默认按时间；按影响时强的、报道多的排前面">
        {FLASH_SORTS.map(([value, label]) => <button type="button" key={value} aria-pressed={sort === value} onClick={() => onSortChange?.(value)}>{label}</button>)}
      </div> : null}
    </div>
    {filter === "product" ? <TonightLine events={current?.tonight} /> : null}
    {error ? <p className="flash-note" role="alert">快讯暂不可用{current ? " · 显示上次的内容" : ""}</p> : null}
    {screened && current ? <ScreenNote screen={current.screen} /> : null}
    {filter === "product" && !product ? <p className="flash-note">请先选择合约</p>
      : filter === "watch" && !watchIds.length ? <p className="flash-note">还没有持仓或自选品种</p>
      : !current ? (error ? null : <p className="flash-note" role="status">加载中…</p>)
      // Unavailable is said in its own words; the week's calendar below stays (it has its own source).
      : unavailable ? <p className="flash-note" role="status">{flashUnavailableText(current.error)}</p>
      : screened && events ? (events.length ? <EventLayers events={events} layers={current.layers} surface="flash" sort={sort} timeOf={timeOf}
          keyword={current.screen?.mode === "keyword"} extrasOf={extrasOf} />
        : pendingOnly ? null : <p className="flash-note">{emptyText}</p>)
      : !items.length ? <p className="flash-note">{emptyText}</p>
      : <>
        {screened ? null : <p className="flash-note flash-raw">原样列出，未经 AI 筛选</p>}
        <ol className="flash-list" data-older={older || undefined}>
          {items.map(item => <FlashRow key={item.id} item={item} now={now} fresh={current.fresh.has(item.id)} productName={productName} onSelectSymbol={onSelectSymbol} />)}
        </ol>
      </>}
    {current && screened ? <DroppedFold name="flash-dropped" screen={current.screen} renderItem={dropped => <>
      <time dateTime={dropped.publishedAt}>{flashTime(dropped.publishedAt, now)}</time>
      <span>{headline(dropped) ? <strong>{dropped.title} </strong> : null}{bodyOf(dropped)}
        {!bodyOf(dropped) && dropped.origTitle ? <small className="flash-orig">原文：{dropped.origTitle}</small> : null}
        <small> · {(dropped.sources.length ? dropped.sources : [dropped.source]).join("／")}</small></span>
    </>} /> : null}
    {current?.sources?.length ? <SourcesLine sources={current.sources} note="近 24 小时 · 每分钟更新 · 第三方信息，本软件未核实" /> : null}
    <SectionCard title="本周重要数据" note={calendarHint} className="flash-calendar">
      {calendarStatus === "loading" || (!calendar && !calendarError) ? <p className="flash-note" role="status">正在获取</p>
        : calendarStatus === "error" || calendarStatus === "unavailable" ? <p className="flash-note" role="status">经济日历暂不可用</p>
        : !calendarEvents.length ? <p className="flash-note" role={gaps ? "status" : undefined}>{gaps || (calendar?.otherCount ? `未来 7 天没有与它直接有关的数据（另有 ${calendar.otherCount} 条在“宏观”里）` : "未来 7 天没有中等重要以上的数据")}</p>
        : <div className="flash-calendar-body">
          {calendarError ? <p className="flash-note" role="status">日历刷新失败，显示上次的内容</p> : null}
          {gaps ? <p className="flash-note" role="status">{gaps}</p> : null}
          {days.map(([date, list]) => <section key={date} className="flash-day" aria-label={dayLabel(date, now)}>
            <h4>{dayLabel(date, now)}</h4>
            <ul>{list.map(event => {
              const past = new Date(event.datetime).getTime() <= now.getTime();
              return <li key={`${event.datetime}-${event.region}-${event.event}`} className="flash-event" data-past={past || undefined} data-high={event.importance >= 3 || undefined}>
                <time>{event.time}</time>
                <span className="flash-region">{event.region}</span>
                <span className="flash-event-name">{event.event}</span>
                {event.actual !== null || event.forecast !== null || event.previous !== null ? <span className="flash-event-values">
                  {event.actual !== null ? <strong>公布 {figure(event.actual)}</strong> : null}
                  <span>预期 {figure(event.forecast)} / 前值 {figure(event.previous)}</span>
                </span> : null}
              </li>;
            })}</ul>
          </section>)}
          {calendarEvents.length > CALENDAR_FOLDED ? <button type="button" className="flash-more" aria-expanded={calendarOpen} onClick={() => setCalendarOpen(!calendarOpen)}>
            {calendarOpen ? "收起" : `展开全部 ${calendarEvents.length} 条`}</button> : null}
        </div>}
    </SectionCard>
  </div>;
}

function FlashRow({ item, now, fresh, productName, onSelectSymbol }: { item: FlashItem; now: Date; fresh: boolean;
  productName: (id: string) => { main?: InstrumentCatalogItem; name: string }; onSelectSymbol?: (symbol: string) => void }) {
  // The row's fold (body / 原文): `flash-orig`, `id` = the item's, so a command opens the same fold.
  const [open, setOpen] = useDisclosureSurface("flash-orig", { id: item.id, available: Boolean(item.origTitle) });
  const title = headline(item);
  const body = bodyOf(item);
  let link: URL | undefined;
  try { const parsed = item.link ? new URL(item.link) : undefined; if (parsed && ["https:", "http:"].includes(parsed.protocol)) link = parsed; } catch { /* Not a usable link. */ }
  return <li className="flash-row" data-fresh={fresh || undefined}>
    <time dateTime={item.publishedAt}>{flashTime(item.publishedAt, now)}</time>
    <div className="flash-row-main">
      <div className="flash-row-meta">
        <span className="flash-source">{(item.sources.length ? item.sources : [item.source]).join("／")}</span>
        {item.tags.includes("macro") ? <span className="flash-tag">宏观</span> : null}
        {item.tags.includes("交易所") ? <span className="flash-tag">交易所</span> : null}
        {/* 原始来源（Runtime news_origin：据新华社、国家统计局数据显示…）。 */}
        {item.tags.filter(tag => tag.startsWith("据:")).map(tag => <span key={tag} className="flash-tag">据{tag.slice(2)}</span>)}
        {item.products.map(id => {
          const { main, name } = productName(id);
          return main && onSelectSymbol
            ? <button type="button" key={id} className="flash-chip" aria-label={`切到 ${name} 主力合约`} title={main.symbol} onClick={() => onSelectSymbol(main.symbol)}>{name}</button>
            : <span key={id} className="flash-chip">{name}</span>;
        })}
        {link ? <a href={link.href} target="_blank" rel="noopener noreferrer">原文</a> : null}
      </div>
      <button type="button" className="flash-text" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span>{title ? <strong>{title}</strong> : null}{title && body ? " " : ""}{body}</span>
        {open && item.origTitle ? <small className="flash-orig">原文：{item.origTitle}</small> : null}
      </button>
    </div>
  </li>;
}

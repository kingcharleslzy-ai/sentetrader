import { useEffect, useRef, useState } from "react";
import { Plus, Search } from "lucide-react";
import type { ResearchJudgmentCard as Judgment } from "@ai-trader/ui-events";
import type { InstrumentCatalogItem } from "../../api";
import type { CardActionHandlers } from "../cards/cardActions";
import { FLASH_REFRESH_MS, flashTime, flashUnavailableText, productOfSymbol, type FlashFeedResponse, type FlashItem, type FlashScreen } from "../FlashFeed";
import { BriefBlock, EventRow, eventHeadline, MinorFold, type EventExtras, type NewsBrief, type NewsEvent, type NewsLayers } from "../NewsEvents";
import { BOARD_MS, chipProducts, debatedKey, eventChips, eventSymbol, quotesByProduct, useDebatedEvents } from "../FlashFeed";
import { useMarketBoard } from "../../hooks/useMarketBoard";
import { DebateView, useDebateHost } from "../ResearchDebate";
import { DroppedFold, ScreenNote } from "../NewsScreen";
import { ResearchDesk, type DeskJudgment } from "../ResearchDesk";
import { BattleSheet } from "../BattleSheet";
import { watchlistKey } from "../futures/support";
import { PullIndicator, usePullToRefresh } from "./usePullToRefresh";
import { readFlashFeed } from "../../flashCache";

const FLASH_SHOWN = 20;

/** The product's flash items of the last 24 hours (the 快讯 tab's own read, filtered on the server);
 *  `reload` reads again now (下拉刷新), keeping the shown items if it fails. */
function useProductFlash(baseUrl: string | undefined, product: string | undefined) {
  const [state, setState] = useState<{ product?: string; items: FlashItem[]; screen?: FlashScreen; events?: NewsEvent[]; layers?: NewsLayers;
    brief?: NewsBrief; failed: boolean; unavailable: boolean; error?: string }>({ items: [], failed: false, unavailable: false });
  const reader = useRef<() => Promise<void>>(async () => undefined);
  useEffect(() => {
    reader.current = async () => undefined;
    if (!baseUrl || !product) return;
    let active = true;
    const read = async () => {
      try {
        const result = await readFlashFeed<FlashFeedResponse>(baseUrl, `hours=24&product=${encodeURIComponent(product)}&limit=${FLASH_SHOWN}`);
        if (active) setState({ product, items: result.items ?? [], screen: result.screen, events: result.events, layers: result.layers, brief: result.brief,
          failed: false, unavailable: result.status === "unavailable", error: result.error });
      } catch {
        if (active) setState(current => ({ ...current, product, failed: true }));
      }
    };
    reader.current = read;
    void read();
    const timer = window.setInterval(() => void read(), FLASH_REFRESH_MS);
    return () => { active = false; window.clearInterval(timer); };
  }, [baseUrl, product]);
  const reload = () => reader.current();
  return state.product === product ? { ...state, loading: false, reload } : { items: [], failed: false, unavailable: false, loading: true, reload };
}

/** A starred product's contract to read: its main month, else the nearest delivery. */
export function mainContract(product: string, instruments: readonly InstrumentCatalogItem[]): InstrumentCatalogItem | undefined {
  const months = instruments.filter(item => watchlistKey(item) === product);
  return months.find(item => item.isMain) ?? [...months].sort((a, b) => a.deliveryYear * 100 + a.deliveryMonth - (b.deliveryYear * 100 + b.deliveryMonth))[0];
}

/**
 * 调研 (手机版第二期, 2026-10-04; 第七批 2026-10-07): one product at a time — the current contract's,
 * switched with the chips of the starred products (a chip switches the contract, like the 调研台 follows
 * the chart) — 今日作战表 on top (第 5 批 2026-10-25), then the research desk in its phone form (sentence, the possibly-stale hint with
 * 重新判断, four key figures, the reasons 偏多 / 偏空 stacked, the 新闻面 line; 基本面 / 宏观 / 相关报道 /
 * 资料来源 folded), the product's flash news below (the phone has no 快讯 page: “快讯 →” scrolls here),
 * and one button that asks the AI about this judgment. A visitor reads the desk's public facts (the AI
 * judgment, the 新闻面 line and 快讯 are for signed-in readers; 问 AI opens the login).
 */
export function MobileResearch({ baseUrl, symbol, productName, judgment, watchlist, instruments, onSelectSymbol, onOpenContracts, handlers, guest, onRefresh }: {
  baseUrl: string | undefined;
  /** The shown contract; the page reads its product. */
  symbol: string | undefined;
  productName: string | undefined;
  /** The workspace's read of the shown product's judgment (the “就这份判断问 AI” question names it). */
  judgment: Judgment | null;
  /** The account's starred products ("SHFE.rb"). */
  watchlist: string[];
  instruments: readonly InstrumentCatalogItem[];
  onSelectSymbol: (symbol: string) => void;
  /** “+ 自选”: the contract list, where a star adds the product. */
  onOpenContracts: () => void;
  handlers: CardActionHandlers;
  /** A visitor's window: the guest Runtime the 调研台 facts come from. */
  guest?: { baseUrl: string; onExpired: () => void };
  /** 下拉刷新: read the judgment again (the page reads its 快讯 itself). */
  onRefresh?: () => unknown;
}) {
  const product = symbol ? productOfSymbol(symbol) : undefined;
  const entry = symbol ? instruments.find(item => item.symbol.toUpperCase() === symbol.toUpperCase()) : undefined;
  const current = entry ? watchlistKey(entry) : undefined;
  const chips = [...new Set([...(current ? [current] : []), ...watchlist])].flatMap(key => {
    const main = mainContract(key, instruments);
    return main ? [{ key, main, name: main.instrumentName.replace(/\d+$/, "") || main.productId.toUpperCase() }] : [];
  });
  const flash = useProductFlash(baseUrl, product);
  // 重要 shows in the open (so do keyword-only events: no model this time, or judged twice without an answer).
  const major = (flash.events ?? []).filter(event => event.tier === "major" || event.tier === "unscreened");
  const rest = (flash.events ?? []).filter(event => event.tier === "other");
  const time = (value: string) => flashTime(value);
  const name = productName || judgment?.productName || symbol || "";
  const ready = judgment?.status === "ready";
  // 下拉刷新 reads the desk again too (its own read; the workspace's judgment read feeds AI 首页).
  const [deskReload, setDeskReload] = useState(0);
  const pull = usePullToRefresh(() => { setDeskReload(value => value + 1); return Promise.all([onRefresh?.(), flash.reload()]); }, !guest);
  const flashRef = useRef<HTMLElement>(null);
  // The judgment this page shows is the desk's own read (it re-reads after 重新判断), not the workspace's slower one:
  // the question names what the reader sees (#516).
  const [shown, setShown] = useState<DeskJudgment>();
  const asked = shown ? (shown.status === "ready" ? shown : undefined) : ready ? judgment : undefined;
  // 第 5 批: the judgment is named by its sentence, never by a 偏多 / 偏空 label.
  const question = `${name}今天这份判断${asked?.headline ? `（${asked.headline}）` : ""}怎么看？依据和失效条件可靠吗？`;
  // 多 AI 研讨 (2026-10-11): a full-screen sub-page over this tab; 研讨 on 重要 rows (always shown on touch).
  const debate = useDebateHost({ baseUrl, symbol: entry?.symbol ?? symbol, enabled: !guest, onAsk: handlers.askAi });
  const debated = useDebatedEvents(baseUrl, (flash.events ?? []).map(event => event.id), !guest);
  // 品种和实时涨跌, as on the computer's 快讯 rows (a tap switches to that product).
  const board = useMarketBoard(baseUrl, !guest && Boolean(flash.events?.length), BOARD_MS, { products: chipProducts(flash.events ?? [], flash.items) });
  const quotes = quotesByProduct(board.snapshot?.rows);
  const productsOf = new Map(flash.items.map(item => [item.id, item.products]));
  const extrasOf = (event: NewsEvent): EventExtras | undefined => guest ? undefined : {
    chips: eventChips(event, productsOf, instruments, quotes), onChip: onSelectSymbol,
    onDebate: () => debate.start({ eventId: event.id, origin: "flash", title: eventHeadline(event), symbol: eventSymbol(event, instruments) }),
    debated: (summary => summary ? { id: summary.id, status: summary.status, stance: summary.stance, at: summary.finishedAt ?? summary.createdAt }
      : undefined)(debated.get(debatedKey(event.id, product ?? ""))),
    onOpenDebate: id => debate.openDebate(id, "flash"),
  };
  if (debate.open && !guest) return <section className="fdm-page fdm-debate" aria-label="多 AI 研讨" data-testid="mobile-debate"><DebateView host={debate} compact /></section>;
  return (
    <section className="fdm-page fdm-research" aria-label="调研" data-testid="mobile-research" data-agent-target="mobile-research">
      <header className="fdm-head fdm-head-stack">
        <strong>调研</strong>
        <div className="fdm-chips" role="group" aria-label="调研品种">
          {chips.map(chip => (
            <button type="button" key={chip.key} aria-pressed={chip.key === current} title={chip.main.symbol}
              onClick={() => { if (chip.key !== current) onSelectSymbol(chip.main.symbol); }}>{chip.name}</button>
          ))}
          <button type="button" className="fdm-chip-add" aria-label="添加自选品种" onClick={onOpenContracts}><Plus aria-hidden="true" />自选</button>
        </div>
      </header>
      <main className="fdm-content" ref={pull.ref}>
        {guest ? null : <PullIndicator phase={pull.phase} />}
        {/* 今日作战表 (第 5 批): the page opens on the 自选 ranked; a row's 看调研 switches the desk below to it. */}
        {guest ? null : <BattleSheet baseUrl={baseUrl} visible compact onOpen={onSelectSymbol} />}
        {!symbol ? (
          <div className="fdm-empty"><Search aria-hidden="true" /><strong>还没有选择合约</strong><span>点“+ 自选”挑一个品种。</span></div>
        ) : (
          // The catalog's spelling of the contract: the desk report answers with it (a quote may differ in case).
          <ResearchDesk key={entry?.symbol ?? symbol} compact baseUrl={guest ? guest.baseUrl : baseUrl} symbol={entry?.symbol ?? symbol}
            guest={Boolean(guest)} onGuestExpired={guest?.onExpired} reloadSignal={deskReload} onJudgment={setShown}
            onOpenFlash={guest ? undefined : () => flashRef.current?.scrollIntoView?.({ block: "start", behavior: "smooth" })}
            onDebate={guest ? undefined : () => debate.start({ origin: "desk" })} />
        )}
        {symbol && !guest ? (
          <section className="fdm-section" aria-label="相关快讯" ref={flashRef}>
            <h3>快讯 · 与{name}相关</h3>
            {flash.failed && !flash.items.length ? <p className="fdm-note" role="alert">快讯暂不可用，稍后自动重试。</p>
              : flash.unavailable ? <p className="fdm-note">{flashUnavailableText(flash.error)}</p>
              : flash.loading ? <p className="fdm-note" role="status">正在读取快讯…</p>
              : flash.events ? <>
                {/* 第六批 (2026-10-07): the compact 要点, the 重要 events, and one fold for the rest (其他, 次要, 无关). */}
                <BriefBlock brief={flash.brief} compact />
                <ScreenNote screen={flash.screen} />
                {major.length ? <ol className="news-ev-list" aria-label="重要">
                  {major.map(event => <EventRow key={event.id} event={event} surface="flash" timeOf={time} compact extras={extrasOf(event)} />)}
                </ol> : !rest.length && !flash.screen?.dropped.count && !flash.screen?.pending ? <p className="fdm-note">近 24 小时没有相关快讯。</p> : null}
                <MinorFold surface="flash" events={flash.events} layers={flash.layers} timeOf={time} compact extrasOf={extrasOf}
                  extra={{ label: rest.length ? `其他 ${rest.length} 条` : "", tail: flash.screen?.dropped.count ? `无关 ${flash.screen.dropped.count} 条` : "",
                    rows: rest, after: <DroppedFold name="flash-dropped" compact screen={flash.screen} renderItem={dropped => <>
                      <time dateTime={dropped.publishedAt}>{flashTime(dropped.publishedAt)}</time><span>{dropped.text}</span>
                    </>} /> }} />
              </> : <>
                <ScreenNote screen={flash.screen} />
                {!flash.items.length ? <p className="fdm-note">近 24 小时没有相关快讯。</p>
                : <ol className="fdm-flash">
                  {flash.items.map(item => (
                    <li key={item.id}>
                      <time dateTime={item.publishedAt}>{flashTime(item.publishedAt)}</time>
                      <span>{item.title && !item.text.startsWith(item.title.replace(/…$/, "")) ? <strong>{item.title} </strong> : null}
                        {item.origTitle && item.text.trim() === item.origTitle.trim() ? "" : item.text}
                        <small>{(item.sources.length ? item.sources : [item.source]).join("／")}</small></span>
                    </li>
                  ))}
                </ol>}
                <DroppedFold name="flash-dropped" compact screen={flash.screen} renderItem={dropped => <>
                  <time dateTime={dropped.publishedAt}>{flashTime(dropped.publishedAt)}</time><span>{dropped.text}</span>
                </>} />
              </>}
            {flash.failed && flash.items.length ? <p className="fdm-note" role="status" data-testid="research-flash-refresh-error">快讯刷新失败，显示上次的内容</p> : null}
          </section>
        ) : null}
        {symbol ? (
          <button type="button" className="fdm-wide-button" disabled={!handlers.askAi} onClick={() => handlers.askAi?.(question)}>就这份判断问 AI</button>
        ) : null}
      </main>
    </section>
  );
}

import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Bell, ChevronDown } from "lucide-react";
import { HelpTip } from "../Blocks";
import { NOTHING_WATCHED } from "../futures/AiWatching";
import { FALLBACK_PRICE_TITLE, formatNumber, formatSigned, signClass } from "../futures/support";
import { Sparkline } from "../Sparkline";
import { CardSkeleton } from "./MobileFeed";
import { RollingText } from "./mobileMotion";
import { SwipeRow, type RowAction } from "./RowActions";
import { approvalMinutesLeft, type MobileFeedItem } from "./feedItems";
import { NOTIFY_FILTERS, notifyTime, rowTime, type NotifyFilter, type NotifyRow } from "./notifyItems";

/** AI 正在盯: what still waits to fire, each list already in words (what already fired is the 提醒记录's). */
export interface WatchInfo {
  /** "甲醇空单 6 手" per position. */
  positions: string[];
  /** "螺纹 3,188" per active price alert. */
  alerts: string[];
  /** The running condition reminders' names. */
  conditions: string[];
  /** "均线金叉 · 出卡确认" per running strategy. */
  strategies: string[];
  /** The position checks that are on: "保本", "风险度", "收盘前", "盘中异动". */
  checks: string[];
  /** "14:55 收盘前持仓检查" while there are positions and that check is on. */
  next: string | null;
  /** The market is closed: 休市中，开盘后继续盯. */
  closed: boolean;
}

/** One row of the 今天 page's 持仓 / 自选 lists. */
export interface HomeQuote {
  symbol: string; name: string; code: string; decimals: number;
  last: number | null; changePercent: number | null;
  /** 盘前 / 未成交 in the 涨跌 cell; fallback: `last` is 昨结算 (no trade yet), greyed. */
  note?: string | null; fallback?: boolean;
  /** 持仓: "多1" / "空2"; floating: that contract's 浮动盈亏. */
  holding?: string; floating?: number | null;
  /** 自选: the last closes of its 15 分钟 bars, oldest first (the sparkline). */
  curve?: number[];
}

const NEW_PILL_SCROLL = 40;

/**
 * AI 首页 (手机版新结构, 2026-10-06; 账户驾驶舱 2026-11-02, owner-chosen direction A): 权益 large with 今天概况
 * (浮动盈亏 / 待确认 / 今天提醒, each goes somewhere) under it, AI 正在盯, the cards waiting for 确认 pinned in their own card component until handled or expired, AI 正在盯 (第二轮
 * 2026-11-01: always there, one row of 到价 / 条件提醒 / 策略在跑 chips (2026-11-05: only what still waits to fire) that open where each is managed, the watched
 * items themselves behind its "?"), 持仓 and 自选 as quote rows (自选 with a 15 分钟 sparkline), one 今日作战表 line (owner 2026-11-05: the sheet is 今天's
 * one entry — each row carries its 今日判断 sentence; a tap opens 调研, whose top is the sheet), 最新提醒 (the 提醒中心's two newest, 2026-10-15), then the timeline that used to be
 * 通知 — its filters and the AI 简报 product chips, each row time · title · one line. A new row opens
 * in at the top; scrolled down, it waits behind “↑ N 条新动态”. What is unread is the 提醒中心's (the
 * Runtime's read marks), not this timeline's.
 */
export function MobileHome({ rows, pending, renderCard, quickPending, now, account, today, holdings = [], watchRows = [], holdingActions, watchActions, onOpenSymbol = () => undefined, onQuotes = () => undefined, onPositions, watch, onAlerts, onStrategies, onSheet,
  filter, onFilter, products, product, onProduct, onOpen, onReminders, latest, notice, refreshError, loading = false, focus }: {
  rows: NotifyRow[];
  /** The cards waiting for 确认, newest first. */
  pending: Extract<MobileFeedItem, { kind: "proposal" }>[];
  renderCard: (item: Extract<MobileFeedItem, { kind: "proposal" }>) => ReactNode;
  /** 手机手势 (2026-11-02): what a 持仓 / 自选 row's left swipe and long press offer. */
  holdingActions?: (row: HomeQuote) => RowAction[];
  watchActions?: (row: HomeQuote) => RowAction[];
  /** A 快捷下单 waits for its own confirmation (it counts as one more 待确认). */
  quickPending: boolean;
  now: number;
  /** reminders / unreadReminders: today's rows of the 提醒中心 and how many of all reminders are unread. */
  /** floatingUsd: the 外盘 USD ledger's floating P&L (本站模拟两本账), shown beside the RMB one, never summed; null without USD positions. */
  /** The account's 权益 (人民币账) and, beside it, the 外盘 USD ledger's; null while not read. */
  account?: { name?: string; equity: number | null; equityUsd: number | null };
  today: { positions: number; floating: number | null; floatingUsd?: number | null; reminders: number; unreadReminders: number };
  onPositions: () => void;
  /** 持仓 (one row per held contract) and 自选 (the starred products' main contracts, at most five). */
  holdings?: HomeQuote[];
  watchRows?: HomeQuote[];
  /** A 持仓 / 自选 row: open that contract's chart. */
  onOpenSymbol?: (symbol: string) => void;
  /** 全部自选: 看盘 · 报价's 自选 board. */
  onQuotes?: () => void;
  watch: WatchInfo;
  /** AI 正在盯's chips: 到价 and 条件提醒 → 我的提醒, 策略在跑 → 策略. */
  onAlerts: () => void;
  onStrategies: () => void;
  /** 今日作战表: opens 调研 (the sheet on top). */
  onSheet: () => void;
  filter: NotifyFilter;
  onFilter: (filter: NotifyFilter) => void;
  products: { product: string; name: string }[];
  product: string | null;
  onProduct: (product: string | null) => void;
  onOpen: (row: NotifyRow) => void;
  /** 今天提醒: the full-screen 提醒 page. */
  onReminders: () => void;
  /** 最新提醒 (the 提醒中心's block), shown above the timeline. */
  latest?: ReactNode;
  notice?: string;
  /** The timeline's 简讯 read failed (#516): said beside the kept rows; gone once a read succeeds. */
  refreshError?: string;
  loading?: boolean;
  /** Bumped by the filter commands: the timeline scrolls into view. */
  focus: number;
}) {
  const root = useRef<HTMLDivElement>(null);
  const timeline = useRef<HTMLDivElement>(null);
  const scroller = () => root.current?.closest<HTMLElement>(".fdm-content") ?? null;
  // What was on screen when the page mounted stays still; a row after that is an arrival.
  const seen = useRef<Set<string> | null>(null);
  const reveal = useRef<Set<string>>(new Set());
  const arriving = useRef<Map<string, number>>(new Map());
  if (seen.current === null) seen.current = new Set(rows.map(row => row.id));
  const fresh = rows.filter(row => !seen.current!.has(row.id));
  if (fresh.length) {
    const first = seen.current.size === 0;
    fresh.forEach(row => { seen.current!.add(row.id); if (first) reveal.current.add(row.id); else arriving.current.set(row.id, Date.now()); });
  }
  const arrivals = fresh.length && seen.current.size > fresh.length ? fresh.length : 0;
  const [newCount, setNewCount] = useState(0);
  // More than one card waiting: the newest shows whole, the rest fold into one line until opened
  // (each is confirmed only in its whole card).
  const [moreOpen, setMoreOpen] = useState(false);
  useEffect(() => {
    if (!arrivals) return;
    const element = scroller();
    if (element && element.scrollTop > NEW_PILL_SCROLL) setNewCount(count => count + arrivals);
  }, [arrivals, rows]);
  useEffect(() => {
    const element = scroller();
    if (!element) return;
    const onScroll = () => { if (element.scrollTop <= NEW_PILL_SCROLL) setNewCount(0); };
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => element.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => { if (focus) timeline.current?.scrollIntoView?.({ block: "start", behavior: "smooth" }); }, [focus]);
  const toTop = () => { scroller()?.scrollTo?.({ top: 0, behavior: "smooth" }); setNewCount(0); };
  const toPinned = () => root.current?.querySelector<HTMLElement>(".fdm-pin")?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  const shown = filter === "all" ? rows : rows.filter(row => row.filter === filter && (filter !== "brief" || !product || row.product === product));
  const productName = filter === "brief" && product ? products.find(item => item.product === product)?.name ?? product : null;
  const waiting = pending.length + (quickPending ? 1 : 0);
  const watched = [
    watch.alerts.length ? `${watch.alerts.length} 个到价提醒：${watch.alerts.join("、")}` : "",
    watch.conditions.length ? `${watch.conditions.length} 个条件提醒：${watch.conditions.join("、")}` : "",
    watch.strategies.length ? `${watch.strategies.length} 个策略运行中：${watch.strategies.join("、")}` : "",
    watch.positions.length && watch.checks.length ? `${watch.positions.length} 笔持仓：${watch.positions.join("、")}，查${watch.checks.join("、")}` : "",
  ].filter(Boolean);
  let order = 0;
  return (
    <div className="fdm-home" data-testid="mobile-feed" aria-label="AI 首页" data-agent-target="mobile-ai" ref={root}>
      {newCount ? <button type="button" className="fdm-new-pill" onClick={toTop}>↑ {newCount} 条新动态</button> : null}
      {notice ? <p className="fdm-feed-notice" role="status">{notice}</p> : null}
      <section className="fdm-account" aria-label="账户">
        <div className="fdm-account-head"><span>权益{account?.name ? ` · ${account.name}` : ""}</span>
          {account?.equityUsd != null ? <span>美元账 <b>{formatNumber(account.equityUsd, 0)}</b></span> : null}</div>
        <div className="fdm-equity"><b>{account?.equity != null ? <RollingText text={formatNumber(account.equity, 0)} /> : "—"}</b><span>元</span></div>
        <div className="fdm-today" role="group" aria-label="今天概况">
          <button type="button" onClick={onPositions} aria-label={`持仓 ${today.positions} 笔${today.positions && today.floating !== null ? `，${today.floating >= 0 ? "浮盈" : "浮亏"} ${formatSigned(today.floating, 0)}` : ""}${today.floatingUsd != null ? `，外盘 ${formatSigned(today.floatingUsd, 0)} 美元` : ""}`}><span>浮动盈亏</span>
            {today.positions && today.floating !== null ? <b className={signClass(today.floating)}><RollingText text={formatSigned(today.floating, 0)} /></b> : <b>—</b>}
            <small>{today.floatingUsd != null ? <>美元 <RollingText text={formatSigned(today.floatingUsd, 0)} /></> : `持仓 ${today.positions} 笔`}</small></button>
          <button type="button" onClick={toPinned} disabled={!pending.length}
            aria-label={`待确认 ${waiting} 张${pending.length ? `，还剩 ${approvalMinutesLeft(pending[0], now)} 分钟` : ""}`}><span>待确认</span><b>{waiting ? `${waiting} 张` : "0"}</b>
            <small>{pending.length ? `还剩 ${approvalMinutesLeft(pending[0], now)} 分钟` : " "}</small></button>
          <button type="button" onClick={onReminders}
            aria-label={`今天提醒 ${today.reminders} 条${today.unreadReminders ? `，${today.unreadReminders} 条未读` : ""}`}><span>今天提醒</span><b>{today.reminders ? `${today.reminders} 条` : "0"}</b>
            <small>{today.unreadReminders ? `${today.unreadReminders} 条未读` : " "}</small></button>
        </div>
      </section>
      <section className="fdm-watching" aria-label="AI 正在盯" data-testid="mobile-watch" data-agent-target="mobile-watch" data-closed={watch.closed ? "" : undefined}>
        <h3>AI 正在盯<HelpTip helpKey="mobile-watch" label="AI 正在盯">{watched.length ? `${watched.join("；")}。` : `${NOTHING_WATCHED}。`}点一个胶囊进管它的地方。</HelpTip></h3>
        <div className="fdm-chips" role="group" aria-label="AI 正在盯">
          <button type="button" onClick={onAlerts}>到价 <b>{watch.alerts.length}</b></button>
          <button type="button" onClick={onAlerts}>条件提醒 <b>{watch.conditions.length}</b></button>
          <button type="button" onClick={onStrategies}>策略在跑 <b>{watch.strategies.length}</b></button>
        </div>
        {watch.closed ? <div className="fdm-watch-next"><span>休市中，开盘后继续盯</span></div>
          : watch.next ? <div className="fdm-watch-next"><span>下一次：{watch.next}</span></div> : null}
      </section>
      {pending.slice(0, 1).map(item => <div className="fdm-pin" key={item.id} data-pin-id={item.id}>{renderCard(item)}</div>)}
      {pending.length > 1 ? <button type="button" className="fdm-pin-more" aria-expanded={moreOpen} onClick={() => setMoreOpen(open => !open)}>
        {moreOpen ? "收起其余待确认" : `还有 ${pending.length - 1} 张待确认`}<ChevronDown aria-hidden="true" /></button> : null}
      {moreOpen ? pending.slice(1).map(item => <div className="fdm-pin" key={item.id} data-pin-id={item.id}>{renderCard(item)}</div>) : null}
      {holdings.length ? <section className="fdm-quotes-home" aria-label="持仓" data-agent-target="mobile-home-positions">
        <div className="fdm-sec"><strong>持仓 <span>{holdings.length}</span></strong><button type="button" onClick={onPositions} aria-label="全部持仓">全部</button></div>
        <div className="fdm-hq-head" aria-hidden="true"><span>合约</span><span>现价</span><span>涨跌</span><span>浮动</span></div>
        {holdings.map(row => <SwipeRow key={row.symbol} id={`holding:${row.symbol}`} title={`${row.name} ${row.holding ?? ""}`.trim()} actions={holdingActions?.(row) ?? []}>
          <button type="button" className="fdm-hq" onClick={() => onOpenSymbol(row.symbol)} aria-label={`${row.name} ${row.holding ?? ""}`.trim()}>
          <span className="fdm-hq-name"><b>{row.name}</b>{row.holding ? <em data-side={row.holding.startsWith("多") ? "long" : "short"}>{row.holding}</em> : null}</span>
          <span className={row.fallback ? "fallback" : undefined} title={row.fallback ? FALLBACK_PRICE_TITLE : undefined}>{formatNumber(row.last, row.decimals)}</span>
          <span className={signClass(row.changePercent)}>{row.note ?? (row.changePercent == null ? "—" : `${formatSigned(row.changePercent, 2)}%`)}</span>
          <b className={signClass(row.floating)}>{row.floating == null ? "—" : formatSigned(row.floating, 0)}</b>
        </button></SwipeRow>)}
      </section> : null}
      <section className="fdm-quotes-home" aria-label="自选" data-agent-target="mobile-home-watchlist">
        <div className="fdm-sec"><strong>自选</strong><span>走势 · 近 48 根 15 分钟</span><button type="button" onClick={onQuotes} aria-label="全部自选">全部</button></div>
        {watchRows.length ? watchRows.map(row => <SwipeRow key={row.symbol} id={`watch:${row.symbol}`} title={row.name} subtitle={row.code} actions={watchActions?.(row) ?? []}>
          <button type="button" className="fdm-hq fdm-hq-watch" onClick={() => onOpenSymbol(row.symbol)} aria-label={row.name}>
          <span className="fdm-hq-name"><b>{row.name}</b><small>{row.code}</small></span>
          <span className={`fdm-hq-spark ${signClass(row.changePercent)}`}>{row.curve && row.curve.length > 1 ? <Sparkline values={row.curve} className="fdm-spark" label={`${row.name} 走势`} /> : null}</span>
          <span className={row.fallback ? "fallback" : undefined} title={row.fallback ? FALLBACK_PRICE_TITLE : undefined}>{formatNumber(row.last, row.decimals)}</span>
          <b className={signClass(row.changePercent)}>{row.note ?? (row.changePercent == null ? "—" : `${formatSigned(row.changePercent, 2)}%`)}</b>
        </button></SwipeRow>) : <p className="fdm-note">还没有自选。到看盘 · 报价或合约列表里点星标加。</p>}
      </section>
      <button type="button" className="fdm-sheet-entry" onClick={onSheet} aria-label="今日作战表"><strong>今日作战表</strong><span>价位 · 消息 · 今日判断 ›</span></button>
      {latest}
      <div className="fdm-timeline" ref={timeline} data-agent-target="mobile-notify">
        <div className="fdm-filters">
          <div className="fdm-chips" role="group" aria-label="时间线筛选">
            {NOTIFY_FILTERS.map(([value, label]) => (
              <button type="button" key={value} aria-pressed={filter === value} onClick={() => onFilter(value)}>{label}</button>
            ))}
          </div>
        </div>
        {filter === "brief" && products.length ? <div className="fdm-chips fdm-chips-sub" role="group" aria-label="简报品种" data-agent-target="mobile-notify-products">
          <button type="button" aria-pressed={!product} onClick={() => onProduct(null)}>全部品种</button>
          {products.map(item => (
            <button type="button" key={item.product} aria-pressed={product === item.product} onClick={() => onProduct(item.product)}>{item.name}</button>
          ))}
        </div> : null}
        {refreshError ? <p className="fdm-note" role="status" data-testid="mobile-feed-refresh-error">{refreshError}</p> : null}
        {shown.length ? <ul className="fdm-rows fdm-timeline-list" aria-label="时间线" data-agent-target="mobile-notify-list">
          {shown.map(row => {
            const arrivedAt = arriving.current.get(row.id);
            const arrive = arrivedAt !== undefined && Date.now() - arrivedAt < 1500;
            const revealed = reveal.current.has(row.id);
            const title = row.title.includes(row.tag) ? row.title : `${row.tag} · ${row.title}`;
            return (
              <li key={row.id} className="fdm-feed-item" data-feed-kind={row.item.kind} data-arrive={arrive ? "" : undefined}
                data-reveal={revealed ? "" : undefined} style={revealed ? { "--i": order++ } as CSSProperties : undefined}>
                <button type="button" aria-label={`${row.tag} · ${row.title}`} onClick={() => onOpen(row)}>
                  <time title={notifyTime(row.time, now)}>{rowTime(row.time, now)}</time>
                  <span><b>{title}
                    {row.item.kind === "brief" && row.productName ? <em className="fdm-tag" title={row.product}>{row.productName}</em> : null}</b>
                    {row.summary ? <small>{row.summary}</small> : null}</span>
                </button>
              </li>
            );
          })}
        </ul> : loading && filter === "all" ? <><CardSkeleton label="正在读取 AI 首页…" /><CardSkeleton label="正在读取 AI 首页…" /></> : (
          <div className="fdm-empty"><Bell aria-hidden="true" />{productName
            ? <><strong>{productName}还没有简报</strong><span>有官方公告或行情异动时会推送到这里</span></>
            : <><strong>{filter === "all" ? "AI 还没有新动态" : "这一类还没有"}</strong>
              <span>成交回报、今日判断和简报会按时间出现在这里；提醒在右上角的铃铛里。</span></>}</div>
        )}
      </div>
    </div>
  );
}

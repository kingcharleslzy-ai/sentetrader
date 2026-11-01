import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Bell, ChevronDown } from "lucide-react";
import { HelpTip } from "../Blocks";
import { formatSigned, signClass } from "../futures/support";
import { CardSkeleton } from "./MobileFeed";
import { RollingText } from "./mobileMotion";
import { approvalMinutesLeft, type MobileFeedItem } from "./feedItems";
import { NOTIFY_FILTERS, notifyTime, rowTime, type JudgmentStance, type NotifyFilter, type NotifyRow } from "./notifyItems";

/** One 今日判断 chip: the product, its stance (读取中 while the desk read runs, 暂无 without a ready
 *  judgment) and the contract it opens. */
export interface JudgmentChip { product: string; name: string; stance: JudgmentStance; symbol?: string }

/** AI 正在盯: what the AI watches right now, each list already in words. */
export interface WatchInfo {
  /** "甲醇空单 6 手" per position. */
  positions: string[];
  /** "螺纹 3,188" per active price alert. */
  alerts: string[];
  /** "均线金叉 · 出卡确认" per running strategy. */
  strategies: string[];
  /** 盘中异动 is on (it watches the held contracts). */
  movement: boolean;
  /** "14:55 收盘前持仓检查", "15:15 今日复盘". */
  next: string[];
  /** The market is closed: 休市中，开盘后继续盯. */
  closed: boolean;
}

const STANCE_DATA: Record<JudgmentChip["stance"], string> = { 偏多: "long", 偏空: "short", 震荡: "range", 暂无: "none", 读取中: "loading" };
const NEW_PILL_SCROLL = 40;

/**
 * AI 首页 (手机版新结构, 2026-10-06; the approved mockup): 今天概况 (three tiles that go somewhere),
 * the cards waiting for 确认 pinned in their own card component until handled or expired, AI 正在盯 (第二轮
 * 2026-11-01: always there, one row of 到价 / 策略在跑 / 今日提醒 chips that open where each is managed, the watched
 * items themselves behind its "?"), one row of 今日判断 chips (a tap opens 调研 on
 * that product), 最新提醒 (the 提醒中心's two newest, 2026-10-15), then the timeline that used to be
 * 通知 — its filters and the AI 简报 product chips, each row time · title · one line. A new row opens
 * in at the top; scrolled down, it waits behind “↑ N 条新动态”. What is unread is the 提醒中心's (the
 * Runtime's read marks), not this timeline's.
 */
export function MobileHome({ rows, pending, renderCard, quickPending, now, today, onPositions, watch, onAlerts, onStrategies, onTodayReminders, judgments, onJudgment,
  filter, onFilter, products, product, onProduct, onOpen, onReminders, latest, notice, refreshError, loading = false, focus }: {
  rows: NotifyRow[];
  /** The cards waiting for 确认, newest first. */
  pending: Extract<MobileFeedItem, { kind: "proposal" }>[];
  renderCard: (item: Extract<MobileFeedItem, { kind: "proposal" }>) => ReactNode;
  /** A 快捷下单 waits for its own confirmation (it counts as one more 待确认). */
  quickPending: boolean;
  now: number;
  /** reminders / unreadReminders: today's rows of the 提醒中心 and how many of all reminders are unread. */
  /** floatingUsd: the 外盘 USD ledger's floating P&L (本站模拟两本账), shown beside the RMB one, never summed; null without USD positions. */
  today: { positions: number; floating: number | null; floatingUsd?: number | null; reminders: number; unreadReminders: number };
  onPositions: () => void;
  watch: WatchInfo;
  /** AI 正在盯's chips: 到价 → 我的提醒, 策略在跑 → 策略, 今日提醒 → 提醒记录. */
  onAlerts: () => void;
  onStrategies: () => void;
  onTodayReminders: () => void;
  judgments: JudgmentChip[];
  onJudgment: (chip: JudgmentChip) => void;
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
    watch.positions.length ? `${watch.positions.length} 笔持仓：${watch.positions.join("、")}` : "",
    watch.alerts.length ? `${watch.alerts.length} 个到价提醒：${watch.alerts.join("、")}` : "",
    watch.strategies.length ? `${watch.strategies.length} 个策略运行中：${watch.strategies.join("、")}` : "",
    watch.movement && watch.positions.length ? "盘中异动：持仓合约每分钟查一次" : "",
  ].filter(Boolean);
  let order = 0;
  return (
    <div className="fdm-home" data-testid="mobile-feed" aria-label="AI 首页" data-agent-target="mobile-ai" ref={root}>
      {newCount ? <button type="button" className="fdm-new-pill" onClick={toTop}>↑ {newCount} 条新动态</button> : null}
      {notice ? <p className="fdm-feed-notice" role="status">{notice}</p> : null}
      <div className="fdm-today" role="group" aria-label="今天概况">
        <button type="button" onClick={onPositions} aria-label={`持仓 ${today.positions} 笔${today.positions && today.floating !== null ? `，${today.floating >= 0 ? "浮盈" : "浮亏"} ${formatSigned(today.floating, 0)}` : ""}${today.floatingUsd != null ? `，外盘 ${formatSigned(today.floatingUsd, 0)} 美元` : ""}`}><span>持仓</span><b>{today.positions} 笔</b>
          {today.positions && today.floating !== null ? <small className={signClass(today.floating)}>{today.floating >= 0 ? "浮盈" : "浮亏"} <RollingText text={formatSigned(today.floating, 0)} /></small>
            : today.floatingUsd == null ? <small>&nbsp;</small> : null}
          {today.floatingUsd != null ? <small className={signClass(today.floatingUsd)}>美元 <RollingText text={formatSigned(today.floatingUsd, 0)} /></small> : null}</button>
        <button type="button" onClick={toPinned} disabled={!pending.length}
          aria-label={`待确认 ${waiting} 张${pending.length ? `，还剩 ${approvalMinutesLeft(pending[0], now)} 分钟` : ""}`}><span>待确认</span><b>{waiting ? `${waiting} 张` : "0"}</b>
          <small>{pending.length ? `还剩 ${approvalMinutesLeft(pending[0], now)} 分钟` : " "}</small></button>
        <button type="button" onClick={onReminders}
          aria-label={`今天提醒 ${today.reminders} 条${today.unreadReminders ? `，${today.unreadReminders} 条未读` : ""}`}><span>今天提醒</span><b>{today.reminders ? `${today.reminders} 条` : "0"}</b>
          <small>{today.unreadReminders ? `${today.unreadReminders} 条未读` : " "}</small></button>
      </div>
      <section className="fdm-watching" aria-label="AI 正在盯" data-testid="mobile-watch" data-agent-target="mobile-watch">
        <h3>AI 正在盯<HelpTip helpKey="mobile-watch" label="AI 正在盯">{watched.length ? `${watched.join("；")}。` : "还没有持仓、提醒或策略。"}点一个胶囊进管它的地方。</HelpTip></h3>
        <div className="fdm-chips" role="group" aria-label="AI 正在盯">
          <button type="button" onClick={onAlerts}>到价 <b>{watch.alerts.length}</b></button>
          <button type="button" onClick={onStrategies}>策略在跑 <b>{watch.strategies.length}</b></button>
          <button type="button" onClick={onTodayReminders}>今日提醒 <b>{today.reminders}</b></button>
        </div>
        {watch.closed ? <div className="fdm-watch-next"><span>休市中，开盘后继续盯</span></div>
          : watch.next.length ? <div className="fdm-watch-next">{watch.next.map((line, index) => <span key={line}>{index ? line : `下一次：${line}`}</span>)}</div> : null}
      </section>
      {pending.slice(0, 1).map(item => <div className="fdm-pin" key={item.id} data-pin-id={item.id}>{renderCard(item)}</div>)}
      {pending.length > 1 ? <button type="button" className="fdm-pin-more" aria-expanded={moreOpen} onClick={() => setMoreOpen(open => !open)}>
        {moreOpen ? "收起其余待确认" : `还有 ${pending.length - 1} 张待确认`}<ChevronDown aria-hidden="true" /></button> : null}
      {moreOpen ? pending.slice(1).map(item => <div className="fdm-pin" key={item.id} data-pin-id={item.id}>{renderCard(item)}</div>) : null}
      {judgments.length ? <>
        <div className="fdm-sec"><strong>今日判断</strong><span>点一个看完整调研</span></div>
        <div className="fdm-judges" role="group" aria-label="今日判断">
          {/* 第 5 批 (owner 2026-10-25): no 偏多 / 偏空 on the chip; only whether the judgment is there yet. */}
          {judgments.map(chip => { const waiting = chip.stance === "读取中" || chip.stance === "暂无";
            return <button type="button" key={chip.product} onClick={() => onJudgment(chip)} aria-label={`${chip.name} ${waiting ? chip.stance : "今日判断"}`}>
              <b>{chip.name}</b>{waiting ? <em data-stance={STANCE_DATA[chip.stance]}>{chip.stance}</em> : null}</button>; })}
        </div>
      </> : null}
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

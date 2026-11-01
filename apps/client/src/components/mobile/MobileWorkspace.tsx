import { Bell, ChartCandlestick, ChevronUp, FlaskConical, MessageSquare, Search, Send, Sparkles, Sun, X, type LucideIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type CSSProperties, type FormEvent, type ReactNode } from "react";
import { flushSync } from "react-dom";
import type { MarketQuoteSnapshot } from "../../api";
import type { PushToTalk } from "../futures/PushToTalk";
import { formatNumber } from "../futures/support";
import { AiCommandResult, type AiCommand } from "../futures/useAiCommand";
import { pushToTalkStore, setPttTarget } from "../../pushToTalk";
import "../futures/AiCommandBar.css";
import { HoldToTalk, HoldToTalkLive } from "./HoldToTalk";
import { usePriceFlash, useRiseKey } from "./mobileMotion";
import { PullIndicator, usePullToRefresh } from "./usePullToRefresh";
import { UnreadBadge } from "../reminders/ReminderCenter";
import "./MobileWorkspace.css";

/** 手机版底部标签; `chat` (AI 对话) is the AI tab's second page and lights the AI tab. 2026-10-06
 *  (新结构): 通知 merged into AI 首页's timeline and its place went to 策略 (the old `mobileTab:
 *  notify` command opens AI 首页). 方案 B (owner 2026-11-01): AI 首页 is called 今天, the middle of the bar is the
 *  按住说话 key, and 我的 left the bar for the avatar on 今天 (it still lights 今天). */
export type MobileTab = "ai" | "chat" | "research" | "market" | "strategy" | "me";
/** 手机版第二期: the full-screen pages over the tabs (`mobilePage` command). order / positions / review /
 *  briefs (AI 简报推送) are the phone's own pages; accounts / memory / appearance open 设置 at that page
 *  and help the 使用说明卡, each full screen on the phone. 2026-10-06: install = 安装到桌面引导, push = 锁屏提醒.
 *  2026-10-15: reminders = 提醒 (the 提醒中心, the bell's page). */
export type MobilePage = "order" | "positions" | "review" | "briefs" | "install" | "push" | "lab" | "reminders" | "accounts" | "memory" | "appearance" | "help";

export const MOBILE_TABS: { id: Exclude<MobileTab, "chat" | "me">; label: string; icon: LucideIcon }[] = [
  { id: "ai", label: "今天", icon: Sun },
  { id: "research", label: "调研", icon: Search },
  { id: "market", label: "看盘", icon: ChartCandlestick },
  { id: "strategy", label: "策略", icon: FlaskConical },
];
/** The bar's five columns: two tabs, the 按住说话 key, two tabs. */
const BAR_COLUMN: Record<string, number> = { ai: 0, research: 1, market: 3, strategy: 4 };

/** 通知 → the conversation: once the chat page shows, scroll its card into view and mark it briefly.
 *  A notification's link (2026-10-06) waits longer — the conversation may still be loading — and on a
 *  computer looks in the whole page (`scope` empty). */
export function locateCard(id: string, { scope = ".fdm-chat", tries: limit = 10 }: { scope?: string; tries?: number } = {}) {
  const selector = `${scope ? `${scope} ` : ""}[data-card-id="${id.replace(/["\\]/g, "\\$&")}"]`;
  let tries = 0;
  const find = () => {
    // The page is gone (a test's environment torn down with the search still pending): nothing to find.
    if (typeof document === "undefined") return;
    const card = document.querySelector<HTMLElement>(selector);
    if (!card) { if (tries++ < limit) window.setTimeout(find, 100); return; }
    card.scrollIntoView?.({ block: "center" });
    card.dataset.located = "";
    window.setTimeout(() => { delete card.dataset.located; }, 2000);
  };
  window.setTimeout(find, 50);
}

/** Tab order, for the direction a new page slides in from. */
const TAB_ORDER: MobileTab[] = ["ai", "research", "market", "strategy", "me"];
type ViewMotion = "none" | "next" | "prev" | "push";

const motionMs = (token: string) => {
  const value = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  return value.endsWith("ms") ? parseFloat(value) : value.endsWith("s") ? parseFloat(value) * 1000 : 0;
};

/** Where a drag may close a full-screen page: its head or the sheet's grip, not its content. */
const DRAG_HANDLE = ".fdm-head, .fdm-sheet-head, .fdm-sheet-grip, .fdw-positions-dock > header";
/** A page dragged down this far closes on release; less springs back. */
export const OVERLAY_DRAG_CLOSE_PX = 110;

/**
 * The full-screen page layer: the page slides up over a dimmed backdrop, and closing (the page's
 * own close, `mobilePage: null`, or a drag down from its head) slides it away before it unmounts —
 * the one place pages are kept a moment after they close.
 */
function OverlayLayer({ overlay, onClose }: { overlay: ReactNode; onClose: () => void }) {
  const last = useRef<ReactNode>(null);
  if (overlay) last.current = overlay;
  const [open, setOpen] = useState(Boolean(overlay));
  const [closing, setClosing] = useState(false);
  // Without a closing duration (reduced motion) the page goes at once.
  if (Boolean(overlay) !== open) { setOpen(Boolean(overlay)); setClosing(!overlay && motionMs("--dur-2") > 0); }
  useEffect(() => {
    if (!closing) return;
    const timer = window.setTimeout(() => { setClosing(false); last.current = null; }, motionMs("--dur-2"));
    return () => window.clearTimeout(timer);
  }, [closing]);
  const [layer, setLayer] = useState<HTMLDivElement | null>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!layer) return;
    let start: { x: number; y: number; panel: HTMLElement } | null = null;
    let active = false;
    let distance = 0;
    let dragged = false;
    const panelOf = () => {
      const child = layer.firstElementChild as HTMLElement | null;
      return child?.classList.contains("fdm-sheet-backdrop") ? child.querySelector<HTMLElement>(".fdm-sheet") : child;
    };
    const onStart = (event: TouchEvent) => {
      start = null; active = false; dragged = false;
      const target = event.target instanceof Element ? event.target : null;
      const panel = panelOf();
      if (layer.dataset.closing !== undefined || event.touches.length !== 1 || !panel || !target?.closest(DRAG_HANDLE)) return;
      start = { x: event.touches[0].clientX, y: event.touches[0].clientY, panel };
    };
    const onMove = (event: TouchEvent) => {
      if (!start) return;
      const dx = event.touches[0].clientX - start.x;
      const dy = event.touches[0].clientY - start.y;
      if (!active) {
        if (Math.abs(dx) > Math.abs(dy) || dy < 0) { if (Math.abs(dx) > 6 || dy < -6) start = null; return; }
        if (dy < 6) return;
        active = true;
      }
      event.preventDefault();
      distance = Math.max(0, dy);
      start.panel.style.transition = "none";
      start.panel.style.transform = `translateY(${distance}px)`;
      layer.style.setProperty("--fdm-drag", String(Math.min(1, distance / 400)));
    };
    const onEnd = () => {
      const panel = start?.panel;
      start = null;
      if (!active || !panel) return;
      active = false;
      dragged = true;
      if (distance > OVERLAY_DRAG_CLOSE_PX) { close.current(); return; }
      panel.style.transition = "";
      panel.style.transform = "";
      layer.style.removeProperty("--fdm-drag");
    };
    // A drag that began on the head's back / close button is not a tap on it.
    const onClick = (event: MouseEvent) => { if (dragged) { dragged = false; event.stopPropagation(); event.preventDefault(); } };
    layer.addEventListener("touchstart", onStart, { passive: true });
    layer.addEventListener("touchmove", onMove, { passive: false });
    layer.addEventListener("touchend", onEnd);
    layer.addEventListener("touchcancel", onEnd);
    layer.addEventListener("click", onClick, true);
    return () => {
      layer.removeEventListener("touchstart", onStart);
      layer.removeEventListener("touchmove", onMove);
      layer.removeEventListener("touchend", onEnd);
      layer.removeEventListener("touchcancel", onEnd);
      layer.removeEventListener("click", onClick, true);
    };
  }, [layer]);
  const node = overlay || (closing ? last.current : null);
  if (!node) return null;
  return <div ref={setLayer} className="fdm-layer" data-closing={overlay ? undefined : ""}>{node}</div>;
}

const TITLES: Record<MobileTab, string> = { ai: "今天", chat: "AI 对话", research: "调研", market: "看盘", strategy: "策略", me: "我的" };

/** The 回执条's one line: what the AI did, is doing, or answered. */
function receiptLine(command: AiCommand) {
  if (command.hasCard) return "有一张确认卡，点开去确认";
  if (command.actions.length) return `做了 ${command.actions.length} 件事：${command.actions.map(step => step.action!.text).join("；")}`;
  if (command.working) return "AI 正在处理…";
  return command.answer.split("\n")[0] || command.asked?.text || "";
}

/**
 * 手机版外壳 (2026-10-04; 新结构 2026-10-06; 方案 B 2026-11-01): one page at a time above the bottom bar — 今天
 * (the head's avatar opens 我的, the bubble the conversation, the bell the 提醒 page; the tab and the bell carry the
 * 提醒中心's unread count), 调研, 看盘 and 策略, with the 按住说话 key in the middle of the bar on every page. Held words
 * and typed lines (a tap on the key opens typing) are about the page on screen (`context`, 按哪一页理解): they go out
 * from here without leaving it, and the 回执条 above the bar says what the AI did (the AI 指令栏's result, `command`);
 * tapping it shows each action with 撤销, and 在对话里看全文 opens the AI 对话 page. A full-screen page (下单, 持仓与委托,
 * 复盘, 回测结果) covers the bar until it is closed. Touch targets are at least 44 px; the bottom keeps the safe area.
 */
export function MobileWorkspace({ tab, onTab, onOpenChat, onBell, quote, symbolName, decimals, feed, chat, ptt, command, context, aiBlocked, aiBusy, notices, pages, overlay, overlayTabs = false, onCloseOverlay, onRefreshFeed, unread }: {
  tab: MobileTab;
  onTab: (tab: MobileTab) => void;
  /** Open the AI 对话 page (access-checked; a refusal explains itself). */
  onOpenChat: () => void;
  /** The 今天 head's bell: the 提醒 page (2026-10-15). */
  onBell: () => void;
  quote: MarketQuoteSnapshot | undefined;
  /** The contract's display name from the catalog (the quote's own name, then its code, as fallbacks). */
  symbolName?: string;
  decimals: number;
  /** 今天 (MobileHome). */
  feed: ReactNode;
  /** The AI 对话 page (the 对话面板 in its phone form). */
  chat: ReactNode;
  ptt: PushToTalk;
  /** Where held and typed words go and what came back; absent (a visitor) leaves the key out. */
  command?: AiCommand;
  /** 按哪一页理解: the page the words are about, e.g. 看盘 · 螺纹2701 5分. */
  context: string;
  /** Why the AI cannot be asked now (typing shows it); null = usable. */
  aiBlocked?: string | null;
  /** The AI is still answering: held words wait in the typing sheet. */
  aiBusy?: boolean;
  /** Connection / Runtime notices the desktop shows over the market area. */
  notices?: ReactNode;
  /** The tab pages (each renders its own head and scroll region); a visitor's window also
   *  passes `ai` (登录后使用), which replaces 今天. */
  pages: Record<"research" | "market" | "strategy" | "me", ReactNode> & { ai?: ReactNode };
  /** The open full-screen page; the bottom bar hides under it. */
  overlay?: ReactNode;
  /** The open page keeps the bottom bar on top of it (the 提醒 page, 2026-10-15 mockup); a tab closes it. */
  overlayTabs?: boolean;
  /** A full-screen page dragged down from its head: the same close as its own button. */
  onCloseOverlay?: () => void;
  /** 下拉刷新 on 今天: its own reads again. */
  onRefreshFeed?: () => unknown;
  /** The 提醒中心's unread reminders (the Runtime's count): the 今天 tab and the bell show it. */
  unread: number;
}) {
  const [slideCancel, setSlideCancel] = useState(false);
  const onSlideCancelChange = useCallback((armed: boolean) => setSlideCancel(armed), []);
  const active = tab === "chat" || tab === "me" ? "ai" : tab;
  const change = quote?.changePercent;
  const direction = change === undefined || change === null || !Number.isFinite(change) || change === 0 ? undefined : change > 0 ? "up" : "down";
  const price = quote ? `${formatNumber(quote.lastPrice, decimals)} ${change === undefined || change === null ? "" : `${change > 0 ? "+" : ""}${change.toFixed(2)}%`}`.trim() : "";
  const name = symbolName || quote?.name || quote?.symbol;
  const flash = usePriceFlash(quote?.lastPrice, quote?.symbol);
  const dotKey = useRiseKey(unread);
  const feedPull = usePullToRefresh(onRefreshFeed ?? (() => undefined), Boolean(onRefreshFeed) && tab === "ai");
  // A new tab page slides in from the side of the tab it came from; the chat and 我的 push in from the right.
  const [view, setView] = useState<{ tab: MobileTab; motion: ViewMotion; moved: boolean }>({ tab, motion: "none", moved: false });
  if (view.tab !== tab) {
    const from = TAB_ORDER.indexOf(view.tab === "chat" || view.tab === "me" ? "ai" : view.tab);
    const to = TAB_ORDER.indexOf(tab === "chat" || tab === "me" ? "ai" : tab);
    setView({ tab, moved: true, motion: tab === "chat" || tab === "me" ? "push" : (view.tab === "chat" || view.tab === "me") && tab === "ai" ? "none" : to > from ? "next" : to < from ? "prev" : "none" });
  }
  // 打字: a tap on the key. The sheet is rendered within the tap and its input focused in the same gesture, the only
  // way iOS raises the keyboard for a field that did not exist before the tap.
  const [typing, setTyping] = useState<string | null>(null);
  const typeInput = useRef<HTMLInputElement>(null);
  const openTyping = (text = "") => {
    flushSync(() => setTyping(text));
    typeInput.current?.focus();
  };
  const submitTyping = (event: FormEvent) => {
    event.preventDefault();
    if (command?.send((typing ?? "").trim())) setTyping(null);
  };
  // Held words go out from here (or wait in the typing sheet while the AI is still answering); the hold key does the same.
  const latest = useRef({ command, aiBusy });
  latest.current = { command, aiBusy };
  const heard = useCallback((words: string) => {
    if (latest.current.aiBusy) setTyping(words); else latest.current.command?.send(words.trim(), true);
  }, []);
  const hasCommand = Boolean(command);
  useEffect(() => hasCommand ? setPttTarget(heard) : undefined, [hasCommand, heard]);
  const keyPtt: PushToTalk = { ...ptt, press: () => ptt.pressTo(heard) };
  const notice = ptt.status.state === "idle" ? ptt.status.notice : undefined;
  const receipt = command?.asked && tab !== "chat" ? command : undefined;
  const barHidden = Boolean(overlay) && !overlayTabs;
  return (
    <div className="fdm-body" data-mobile-tab={tab} data-mobile-overlay={overlay ? "" : undefined}>
      <div className="fdm-view" key={tab} data-motion={view.motion === "none" ? undefined : view.motion}>
      {tab === "chat" ? chat : tab !== "ai" || pages.ai ? <>
        {notices}
        {tab === "ai" ? pages.ai : pages[tab]}
      </> : <>
        <header className="fdm-head">
          <button type="button" className="fdm-head-me" aria-label="我的" title="我的" data-agent-target="mobile-me-avatar" onClick={() => onTab("me")}>
            <span className="fdm-avatar" aria-hidden="true">我</span></button>
          <strong>{TITLES[tab]}</strong>
          {quote ? (
            <button type="button" className="fdm-quote" aria-label={`看盘：${name} ${price}`} onClick={() => onTab("market")}>
              <span className="fdm-quote-name">{name}</span><span className="fdm-quote-price" data-direction={direction} data-flash={flash}>{price}</span>
            </button>
          ) : null}
          <button type="button" className="fdm-head-chat" aria-label="AI 对话" title="AI 对话" onClick={onOpenChat}><MessageSquare aria-hidden="true" /></button>
          <button type="button" className="fdm-head-chat fdm-bell" aria-label={unread ? `看提醒（${unread} 条未读）` : "看提醒"} title="看提醒" onClick={onBell}>
            <Bell aria-hidden="true" /><UnreadBadge count={unread} /></button>
        </header>
        {notices}
        <main className="fdm-content" ref={feedPull.ref}><PullIndicator phase={feedPull.phase} />{feed}</main>
      </>}
      </div>
      <OverlayLayer overlay={overlay} onClose={onCloseOverlay ?? (() => undefined)} />
      <footer className="fdm-foot" hidden={barHidden} data-over-page={overlay && overlayTabs ? "" : undefined}>
        <HoldToTalkLive status={ptt.status} slideCancel={slideCancel} context={command ? context : undefined} />
        {notice && tab !== "chat" ? <p className="fdm-talk-notice" role="status" title="点一下关闭" onClick={() => pushToTalkStore.dismiss()}>{notice}</p> : null}
        {command && typing !== null ? <form className="fdm-type" aria-label="对 AI 说" data-agent-target="mobile-ai-type" onSubmit={submitTyping}>
          <div className="fdm-type-row">
            <label htmlFor="fdm-type-input" className="fdm-context">按 {context} 理解</label>
            <button type="button" className="fdm-type-close" aria-label="收起打字" onClick={() => setTyping(null)}><X aria-hidden="true" /></button>
          </div>
          <div className="fdm-type-row">
            <input id="fdm-type-input" ref={typeInput} value={typing} maxLength={500} enterKeyHint="send" disabled={Boolean(aiBlocked)}
              placeholder={aiBlocked ?? "问 AI，或说一句要做的事"} onChange={event => setTyping(event.target.value)}
              onKeyDown={event => { if (event.key === "Escape") setTyping(null); }} />
            <button type="submit" className="fdm-type-send" disabled={!typing.trim() || Boolean(aiBlocked) || aiBusy}><Send aria-hidden="true" />发送</button>
          </div>
        </form> : receipt ? receipt.open
          ? <AiCommandResult command={receipt} className="ai-bar-result fdm-ai-result" target="mobile-ai-result" onOpenChat={onOpenChat} />
          : <div className="fdm-receipt" data-agent-target="mobile-ai-receipt" data-working={receipt.working ? "" : undefined}>
            <button type="button" className="fdm-receipt-main" aria-label={`AI 指令结果：${receiptLine(receipt)}，点开看详情`} onClick={() => receipt.setOpen(true)}>
              <Sparkles aria-hidden="true" /><span>{receiptLine(receipt)}</span><ChevronUp aria-hidden="true" /></button>
            <button type="button" className="fdm-receipt-close" aria-label="关掉回执" onClick={receipt.clear}><X aria-hidden="true" /></button>
          </div> : null}
        <nav className="fdm-tabs" aria-label="主导航" data-agent-target="mobile-tabs" data-moved={view.moved ? "" : undefined} data-key={command ? "" : undefined}>
          <span className="fdm-tab-pill" aria-hidden="true" style={{ "--fdm-tab": command ? BAR_COLUMN[active] ?? 0 : Math.max(0, MOBILE_TABS.findIndex(item => item.id === active)) } as CSSProperties} />
          {MOBILE_TABS.map(item => [
            item.id === "market" && command ? <span key="key" className="fdm-key"><HoldToTalk ptt={keyPtt} onSlideCancelChange={onSlideCancelChange} onTap={() => openTyping()} /></span> : null,
            <button type="button" key={item.id} aria-current={active === item.id ? "page" : undefined}
              aria-label={item.id === "ai" && unread ? `${item.label}（${unread} 条未读）` : undefined}
              onClick={() => onTab(item.id)}>
              <span className="fdm-tab-icon"><item.icon aria-hidden="true" />{item.id === "ai" && unread ? <span key={dotKey} className="rc-badge" data-pop={dotKey ? "" : undefined} data-testid="notify-dot" aria-hidden="true">{unread > 99 ? "99+" : unread}</span> : null}</span>{item.label}
            </button>,
          ])}
        </nav>
      </footer>
    </div>
  );
}

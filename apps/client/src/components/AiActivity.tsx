import { activityTradeRows, activityTime, type TradeReviewSnapshot } from "../activityTrades";
import { listProductBriefs, refreshProductBriefs, requestJson, type ProductBrief } from "../api";
import { LONG_READ_TIMEOUT_MS } from "../requestTimeout";
import { useActivityFresh, useFillFeedback } from "../hooks/useActivityMotion";
import { usePersistentDraft } from "../hooks/usePersistentDraft";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";

/** The trading account the session acts on now; account-scoped reads key on it (#526). */
function useActiveTradingAccount() { return useSyncExternalStore(onTradingAccountChange, activeTradingAccount); }
import { Activity, MessageSquareText, Mic, MicOff, Newspaper, PhoneOff } from "lucide-react";
import { CardShell } from "./CardShell";
import { AiAvatar } from "./AiAvatar";
import { moneyUnit, type AiAvatar as AiAvatarKind } from "./futures/support";
import { useDisclosureSurface, useUiSurface } from "../uiSurfaces";
import { orbLabel, orbState } from "../voiceRealtime";
import { useVoiceCall } from "../voiceCallControls";
import { useQuery } from "@tanstack/react-query";
import { readNetworkMode, readQueryClient, useReadIdentity } from "../runtimeClient";
import { useIdentityFence } from "../hooks/useIdentityFence";
import { feedIsLive, feedLiveNow, useRuntimeFeed } from "../runtimeFeed";
import { feedReportServes, useFeedReport } from "../feedView";
import { activeTradingAccount, onTradingAccountChange } from "../tradingAccountScope";
import { workbenchClient, responseData, type WorkbenchConfig, type ActivitySnapshot } from "../runtimeClient";
export type { ChartExtension } from "../runtimeClient";
import { HelpButton } from "./AppHelp";
import "./AiActivity.css";
import { Presence } from "./motion";
import { createPortal } from "react-dom";
import { menuHost, useMenuPlace } from "./futures/ChartContextMenu";

const defaultConfig: WorkbenchConfig = { showAiActivity: true, showTradeMarkers: true, showMarketBriefs: true, extensions: [] };
export function useAppWorkbench(baseUrl: string | undefined, symbol: string | undefined) {
  const fence = useIdentityFence([baseUrl]);
  const identity = fence.context.identity;
  const client = useMemo(() => workbenchClient(baseUrl ?? ""), [baseUrl]);
  const configKey = ["workbench", identity, baseUrl, "config"] as const;
  const settings = useQuery({ queryKey: configKey, enabled: baseUrl !== undefined, networkMode: readNetworkMode(baseUrl),
    queryFn: async ({ signal }) => responseData(await client.GET("/api/app/config", { signal })),
    refetchInterval: () => feedLiveNow() ? 120_000 : 5000
  }, readQueryClient);
  const feed = useRuntimeFeed();
  const account = useActiveTradingAccount();
  // The pushed activity names no account: a frame counts for the account that was active when it came.
  // After a switch the previous account's frame is not shown; the read below (keyed on the account)
  // fills in until the new account's first frame arrives (#526).
  const frameAccount = useRef<{ frame?: ActivitySnapshot; account: string | null }>({ account });
  if (frameAccount.current.frame !== feed.activity) frameAccount.current = { frame: feed.activity, account };
  const feedActivity = feedIsLive(feed, "activity") && feed.activity?.symbol === symbol && frameAccount.current.account === account
    ? feed.activity : undefined;
  const activity = useQuery({ queryKey: ["workbench", identity, baseUrl, "activity", account, symbol],
    enabled: baseUrl !== undefined && Boolean(symbol), networkMode: readNetworkMode(baseUrl),
    queryFn: async ({ signal }) => responseData(await client.GET("/api/app/activity", { params: { query: { symbol } }, signal })),
    refetchInterval: () => feedLiveNow("activity") ? 60_000 : 1000
  }, readQueryClient);
  const updateConfig = async (patch: Partial<WorkbenchConfig>) => {
    if (baseUrl === undefined) throw new Error("连接尚未就绪，请稍后重试");
    // A settings save is explicit and revision-checked. Never queue/retry it.
    const current = responseData(await client.GET("/api/app/config"));
    if (!fence.isCurrent()) throw new Error("登录身份已变化，请重新操作");
    const saved = responseData(await client.PUT("/api/app/config", { body: { revision: current.revision, config: { ...current.config, ...patch } } }));
    await readQueryClient.cancelQueries({ queryKey: configKey, exact: true });
    if (fence.isCurrent()) readQueryClient.setQueryData(configKey, saved);
  };
  return { config: settings.data?.config ?? defaultConfig, activity: feedActivity ?? activity.data,
    error: activity.isError ? "操作记录连接中断" : settings.isError ? "设置读取失败" : "", updateConfig };
}
function action(direction: string, offset: string) {
  return offset === "OPEN" ? direction === "BUY" ? "开多" : "开空" : direction === "SELL" ? "平多" : "平空";
}
function AiActivity({ activity, error, scope, baseUrl }: { activity?: ActivitySnapshot; error: string; scope: string; baseUrl?:string }) {
  const [hidden, setHidden] = usePersistentDraft<Record<string, string>>(`ai-trader-activity-hidden:${scope}`, {});
  const [showHistory, setShowHistory] = useDisclosureSurface("activity-history", { available: Object.keys(hidden).length > 0 });
  const identity = useReadIdentity();
  const account = useActiveTradingAccount();
  // A new fill changes the key (read once at once); the interval only catches what the feed does not carry.
  const fillKey=activity?.fills.map(fill=>fill.tradeId).join(",");
  const review=useQuery({queryKey:["activity-trade-review",identity,baseUrl,account,activity?.symbol,activity?.environment,activity?.replayId,fillKey],enabled:baseUrl!==undefined&&Boolean(activity?.fills.length),networkMode:readNetworkMode(baseUrl),
    queryFn:({signal})=>requestJson<TradeReviewSnapshot>(`${baseUrl}/api/trade-review?symbol=${encodeURIComponent(activity!.symbol)}`,{signal,timeoutMs:LONG_READ_TIMEOUT_MS}),
    // The previous answer bridges a new fill of the same account, never another account's trades.
    placeholderData:(previous,previousQuery)=>previousQuery?.queryKey[3]===account?previous:undefined,refetchInterval:()=>feedLiveNow("activity")?60_000:5000},readQueryClient);
  const fresh=useActivityFresh(activity,error);
  const changed=useFillFeedback(activity,scope,fresh&&!showHistory);
  const rows=activityTradeRows(activity,review.isError?undefined:review.data);
  const visibleRows=rows.filter(row=>showHistory||hidden[row.id]!==row.revision);
  const [expanded, setExpanded] = useDisclosureSurface("activity-more", { available: visibleRows.length > 3 });
  // 清空显示 as a command: open = hide the finished records; the state says whether anything is left to clear.
  const clearShown = () => { setHidden(Object.fromEntries(rows.map(row => [row.id, row.revision]))); setShowHistory(false); };
  useDisclosureSurface("activity-clear", { state: !visibleRows.length, apply: wanted => {
    if (!wanted) throw new Error("已清空的记录用 activity-history 查看，不能撤销清空");
    if (!visibleRows.length) return;
    clearShown();
  } });
  const stale = Boolean(error || activity && (activity.dataStatus !== "normal" || (activity.ageSeconds ?? 0) > 15));
  return <section className="ai-live-activity" data-help="activity" aria-label="AI实时操作">
    <header><strong>操作记录</strong><small>{activity ? visibleRows.length ? visibleRows.length + " 笔记录" : "暂无新记录" : ""}</small><HelpButton topic="activity" /></header>
    <div className="activity-display-actions"><button type="button" className="app-button danger" disabled={!visibleRows.length} onClick={clearShown}>清空显示</button>{Object.keys(hidden).length > 0 && <button type="button" onClick={() => setShowHistory(value => !value)}>{showHistory ? "返回当前动态" : "查看历史"}</button>}</div>
    {stale && <div className="activity-warning" role="status">{error || "操作记录已过期"}</div>}
    <div className={expanded ? "activity-content expanded" : "activity-content"}>
      {visibleRows.slice(0, expanded ? 100 : 3).map(row => <article className="activity-trade" data-new-fill={changed.has(row.id)||row.closes.some(close=>{const id=JSON.parse(close.id)[0];return activity?.fills.some(fill=>fill.tradeId===id&&changed.has(fill.clientOrderId));})||undefined} key={row.id}>
        <div className="activity-trade-line"><time>{activityTime(row.time)}</time><strong>{action(row.direction,row.offset)}</strong><b>{row.price===null?"—":row.price.toLocaleString("zh-CN",{maximumFractionDigits:2})}</b><span>{row.volume}手</span>{row.status&&<em>{row.status}</em>}</div>
        {row.closes.map(close=><div className="activity-trade-line" key={close.id}><time>{activityTime(close.closeTime)}</time><strong>{row.direction==="BUY"?"平多":"平空"}</strong><b>{close.closePrice.toLocaleString("zh-CN",{maximumFractionDigits:2})}</b><span>{close.volume}手</span></div>)}
        {row.pnl!==undefined&&<div className={`activity-trade-pnl ${row.pnl>=0?"positive":"negative"}`}>{row.net?"净盈亏":"毛盈亏"} {row.pnl>0?"+":""}{row.pnl.toLocaleString("zh-CN",{minimumFractionDigits:2,maximumFractionDigits:2})} {moneyUnit(activity?.symbol)}</div>}
      </article>)}
      {visibleRows.length > 3 && <button type="button" className="activity-more" onClick={() => setExpanded(value => !value)}>{expanded ? "收起记录" : "查看更多操作"}</button>}
      {activity && !visibleRows.length && <p>暂无新的委托记录</p>}
      {!activity && !error && <div className="activity-decision">正在读取操作记录…</div>}
    </div>
  </section>;
}

// The briefs' search status, written by the one instance that owns the refresh loop and read by
// every other consumer (the monitor page, the help text).
const searchStatuses = new Map<string, string>();
const searchListeners = new Set<() => void>();
function publishSearchStatus(key: string, status: string) {
  if (searchStatuses.get(key) === status) return;
  searchStatuses.set(key, status);
  searchListeners.forEach(listener => listener());
}
function subscribeSearchStatus(listener: () => void) { searchListeners.add(listener); return () => { searchListeners.delete(listener); }; }
/** Test helper. */
export function resetBriefSearchStatus() { searchStatuses.clear(); searchListeners.forEach(listener => listener()); }

/**
 * The subscribed products' briefs, merged newest first (AI 简报订阅, 2026-10-05: no longer the
 * chart's contract; each item names its product). Exactly one consumer per window (the floating
 * ball) owns the periodic external search (`ownsRefresh`; the Runtime rotates the products);
 * readers such as the monitor page's card pass `false`, share the cached query and see the
 * owner's status.
 */
export function useBriefs(baseUrl: string | undefined, enabled: boolean, replay: boolean, ownsRefresh = true) {
  const fence = useIdentityFence([baseUrl]);
  const identity = fence.context.identity;
  const active = baseUrl !== undefined && enabled && !replay;
  const statusKey = `${identity}|${baseUrl}`;
  const searchStatus = useSyncExternalStore(subscribeSearchStatus, () => searchStatuses.get(statusKey) ?? "等待检索", () => "等待检索");
  const setSearchStatus = (status: string) => publishSearchStatus(statusKey, status);
  const key = ["workbench", identity, baseUrl, "briefs"] as const;
  // 通用报告推送 (2026-11-03): the list comes over the push (read every 15 s for this user on the Runtime, whatever the
  // number of windows); the query reads it itself only while the push does not serve it.
  const pushed = useFeedReport<{ items: ProductBrief[] }>(baseUrl, active ? "briefs" : undefined);
  useEffect(() => { if (pushed) readQueryClient.setQueryData(key, pushed); }, [pushed]); // eslint-disable-line react-hooks/exhaustive-deps
  const briefs = useQuery({ queryKey: key, enabled: active, networkMode: readNetworkMode(baseUrl),
    queryFn: ({ signal }) => {
      const held = readQueryClient.getQueryData<{ items: ProductBrief[] }>(key);
      return held && feedReportServes(baseUrl, "briefs") ? Promise.resolve(held) : listProductBriefs(baseUrl!, signal);
    },
    // A search runs at most every 10 minutes and invalidates this on completion; the interval catches the rest.
    refetchInterval: () => feedLiveNow() ? 60_000 : 15000
  }, readQueryClient);
  useEffect(() => {
    if (!active || !ownsRefresh) return;
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    setSearchStatus("等待检索");
    let nextSearch = Date.now() + 15_000;
    const refresh = async () => {
      try {
        if (document.visibilityState === "visible" && Date.now() >= nextSearch) {
          nextSearch = Date.now() + 600_000;
          if (!fence.isCurrent()) return;
          const result = await refreshProductBriefs(baseUrl!);
          if (!cancelled) setSearchStatus(result.status === "unavailable" ? "资讯检索服务不可用" : result.status === "pending" ? "最近检索请求已提交，暂无可展示简讯" : "暂未取得可展示简讯");
          if (!cancelled) void readQueryClient.invalidateQueries({ queryKey: key, exact: true });
        }
      } catch { if (!cancelled) setSearchStatus("资讯检索失败，等待下次检查"); }
      finally { if (!cancelled) timer = setTimeout(refresh, 15000); }
    };
    void refresh();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [active, identity, baseUrl, ownsRefresh, statusKey]);
  // A failed read keeps this identity's last list (the query key holds identity and Runtime; expired
  // briefs still drop out) and the status says the refresh failed (#516).
  const items = active ? (briefs.data?.items ?? []).filter(item => Date.parse(item.expiresAt) > Date.now()) : [];
  const failed = active && briefs.isError;
  // reload: the phone's 下拉刷新 reads the list again now.
  const status = !enabled ? "已关闭" : !active ? "连接后可用"
    : failed ? items.length ? `刷新失败，显示上次的 ${items.length} 条简讯` : "简讯读取失败" : items.length ? `${items.length} 条有效简讯` : searchStatus;
  // refreshError: the same failure for a page that shows the list (手机 AI 首页), empty once a read succeeds.
  return {items, reload: briefs.refetch, status, refreshError: !failed ? "" : items.length ? `简讯刷新失败，显示上次的 ${items.length} 条` : "简讯读取失败，稍后自动重试"};
}
const seenBriefIds = new Set<string>();
/** v2 is written only when the user moves the orb; v1 was written on every mount, so its
 *  bottom-right corner ({1,1}) is the old default rather than a chosen place. */
/** Half the orb label's max-width (220px) plus a margin: its centre stays this far inside the window, so a long
 *  state ("AI 在说 · 外放不听") under an orb parked at an edge is not cut off. */
const ORB_LABEL_HALF = 118;
const FLOAT_POSITION_KEY = "ai-trader-ai-float-position-v2";
const LEGACY_POSITION_KEY = "ai-trader-ai-float-position-v1";
/** The orb's place as fractions of the desktop workspace; storage stays local to this device. */
function readOrbPosition(): { x: number; y: number } | undefined {
  try {
    const stored = JSON.parse(localStorage.getItem(FLOAT_POSITION_KEY) ?? "null");
    const legacy = JSON.parse(localStorage.getItem(LEGACY_POSITION_KEY) ?? "null");
    const value = stored ?? (legacy?.x === 1 && legacy?.y === 1 ? null : legacy);
    if (Number.isFinite(value?.x) && Number.isFinite(value?.y)) return { x: Math.max(0, Math.min(1, value.x)), y: Math.max(0, Math.min(1, value.y)) };
  } catch { /* A blocked storage area must not hide the assistant. */ }
  return undefined;
}
export function savedOrbPosition() { return readOrbPosition() ?? { x: 0, y: 1 }; }
/** The product a brief was searched for, as its label (螺纹钢); a pre-subscription Runtime sends none. */
const briefProduct = (item: ProductBrief) => item.productName || item.product || "";
const briefTime = (value: string) => new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(value));
/**
 * The newest still-valid brief on the AI monitor page. Rendered only when there is
 * one: the search runs on its own (the floating ball owns it) and its status is
 * shown there and in the manual, not as a placeholder card here.
 */
export function LatestBriefCard({ baseUrl, enabled, replay }: { baseUrl: string | undefined; enabled: boolean; replay: boolean }) {
  const { items } = useBriefs(baseUrl, enabled, replay, false);
  const latest = items[0];
  if (!enabled || !latest) return null;
  return <CardShell icon={<Newspaper size={18} />} title={latest.kind === "news" ? "重要资讯" : "行情提醒"} meta={[latest.productName, briefTime(latest.publishedAt), latest.source].filter(Boolean).join(" · ")} testId="latest-brief">
    <p className="ai-brief-text">{latest.text}</p>
  </CardShell>;
}

/** How long an automatically shown activity card stays before it folds back into the orb. */
const AUTO_CARD_MS = 8_000;
const LONG_PRESS_MS = 500;

/**
 * The one floating AI orb (2026-09-18: the "AI 动态" ball and the voice orb became one). Draggable
 * with a remembered position shared by all desktop views (default workspace bottom-left).
 * Its look layers what the AI is doing: standby breathing; an arc while a strategy operates or the call's agent request runs; the call's own listening halo and speaking
 * bars; a dot for unread activity. Click connects or hangs up the voice call (retries after an
 * error; opens the activity card when the Runtime has no voice). New activity, a market brief or
 * news pops the card above the orb for a few seconds even mid-call; long-press / right-click opens
 * 静音 / 挂断 / 切文字 / AI 动态. Commandable: `aiActivityCard`, `disclosure: voice-call|voice-mute|voice-menu`.
 */
export function AiOrb({ baseUrl, symbol, replay, activity, error, config, onBriefStatusChange, onOpenChat, sessionId, voiceAvailable, avatar = "stone", size: orb = 56, newMessage = false, beside }: {
  baseUrl?: string; symbol?: string; name?: string; replay: boolean; activity?: ActivitySnapshot; error: string;
  config: typeof defaultConfig; onConfigChange: (patch: Partial<typeof defaultConfig>) => Promise<void>; onOpenChat: () => void; onBriefStatusChange?: (status: string) => void;
  /** The chat session voice turns join. */
  sessionId?: string;
  /** The Runtime offers real-time voice; without it the orb is the activity ball alone. */
  voiceAvailable?: boolean;
  /** 设置 → 界面 → AI 形象. */
  avatar?: AiAvatarKind;
  /** 设置 → 界面 → AI 形象大小: the orb's size in px; bounds, avoidance and the menu follow it. */
  size?: number;
  /** The conversation has an AI reply or card the user has not seen (the workspace decides). */
  newMessage?: boolean;
  /** A short message beside the orb (the AI 指令栏's result), on the side with more room. */
  beside?: ReactNode;
}) {
  const identity = useReadIdentity();
  const area = useRef<HTMLDivElement>(null); const bubble = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLMenuElement>(null);
  const drag = useRef<{ x: number; y: number; left: number; top: number; moved: boolean } | undefined>(undefined);
  const suppressClick = useRef(false);
  const pressTimer = useRef<number | undefined>(undefined);
  const autoTimer = useRef<number | undefined>(undefined);
  const [size, setSize] = useState({ width: 0, height: 0, left: 0, top: 0, leftInset: 0, rightInset: 0, bottomInset: 0, menuHeight: 0, menuWidth: 0 });
  const [marketBars, setMarketBars] = useState<{ left: number; top: number; right: number; bottom: number }[]>([]);
  const [position, setPosition] = useState(readOrbPosition);
  const [open, setOpen] = useState(false); const [closing, setClosing] = useState(false); const [unread, setUnread] = useState(false);
  // "auto": the card showed itself for new activity and folds back on its own; a click or command pins it.
  const [auto, setAuto] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const closeCard = () => { setClosing(true); setOpen(false); setAuto(false); window.clearTimeout(autoTimer.current); bubble.current?.focus(); };
  const showCard = (automatic = false) => {
    setClosing(false); setOpen(true); setAuto(automatic); setMenuOpen(false);
    window.clearTimeout(autoTimer.current);
    if (automatic) autoTimer.current = window.setTimeout(() => { setOpen(false); setClosing(true); setAuto(false); }, AUTO_CARD_MS);
  };
  useUiSurface("aiActivityCard", { state: open ? "open" : "closed", apply: value => { if (value === "open") showCard(); else closeCard(); } }, [open]);
  useEffect(() => { if (!closing) return; const timer=setTimeout(()=>setClosing(false),180); return ()=>clearTimeout(timer); },[closing]);
  useEffect(() => () => { window.clearTimeout(autoTimer.current); window.clearTimeout(pressTimer.current); }, []);
  const {items: briefs, status: briefStatus} = useBriefs(baseUrl, config.showMarketBriefs, replay);
  useEffect(() => { onBriefStatusChange?.(briefStatus); }, [briefStatus, onBriefStatusChange]);
  const fresh = useActivityFresh(activity,error);
  const operating = fresh && activity?.strategies.some(item => item.status === "monitoring");
  const activityKey = JSON.stringify([activity?.orders.map(item => [item.clientOrderId, item.displayStatus]), activity?.fills.map(item => [item.tradeId, item.volume]), activity?.strategies.filter(item => item.monitorState?.pauseReason).map(item => [item.strategyId, item.monitorState?.pauseReason])]);
  const previous = useRef<string | undefined>(undefined); const seenBriefs = useRef(seenBriefIds);
  // New activity or a new brief: the card pops out on its own (about 8 s) and the orb keeps a dot
  // until the user has looked at it.
  useEffect(() => {
    if (!activity) return;
    if (previous.current !== undefined && previous.current !== activityKey) { if (!open) { setUnread(true); showCard(true); } }
    previous.current = activityKey;
  }, [activityKey, activity, open]);
  const briefKey = briefs.map(item => item.id).join("|");
  useEffect(() => {
    let fresh_ = false;
    for (const item of briefs) { if (!seenBriefs.current.has(item.id)) { fresh_ = true; } seenBriefs.current.add(item.id); }
    if (fresh_ && !open) { setUnread(true); showCard(true); }
    // A brief's desktop notification is the Runtime's (feed.reminder, AI 简讯's switch, #932) — not rung here.
  }, [briefKey, open]);
  useEffect(() => {
    if (open && !auto) setUnread(false);
    if (open) { const close = (event: KeyboardEvent) => { if (event.key === "Escape") { closeCard(); } }; window.addEventListener("keydown", close); return () => window.removeEventListener("keydown", close); }
  }, [open, auto]);
  // Rebind after each render: switching views can add/remove the bottom bar without resizing the body.
  useLayoutEffect(() => {
    const host = area.current;
    if (!host) return;
    const bar = host.parentElement?.querySelector('[data-testid="positions-bar"]');
    const rail = host.parentElement?.querySelector(".fdw-tools-rail");
    const commands = [...host.parentElement?.querySelectorAll(".fdw-market-command") ?? []];
    const measure = () => {
      const rect = host.getBoundingClientRect();
      const barRect = bar?.getBoundingClientRect();
      const railRect = rail?.getBoundingClientRect();
      const bars = commands.map(command => command.getBoundingClientRect()).filter(bar => bar.width && bar.height)
        .map(bar => ({ left: bar.left - rect.left, top: bar.top - rect.top, right: bar.right - rect.left, bottom: bar.bottom - rect.top }));
      setMarketBars(old => JSON.stringify(old) === JSON.stringify(bars) ? old : bars);
      const next = { width: rect.width, height: rect.height, left: rect.left, top: rect.top,
        leftInset: railRect?.width && railRect.left < rect.left + rect.width / 2 ? Math.max(0, railRect.right - rect.left + 8) : 0,
        rightInset: railRect?.width && railRect.left >= rect.left + rect.width / 2 ? Math.max(0, rect.right - railRect.left + 8) : 0,
        bottomInset: barRect?.height ? Math.max(0, rect.bottom - (bar!.parentElement!.getBoundingClientRect().bottom - barRect.height) + 8) : 0,
        menuHeight: menu.current?.offsetHeight ?? 0, menuWidth: menu.current?.offsetWidth ?? 0 };
      setSize(old => Object.keys(next).every(key => old[key as keyof typeof old] === next[key as keyof typeof next]) ? old : next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(host);
    if (bar) observer.observe(bar);
    if (rail) observer.observe(rail);
    commands.forEach(command => { observer.observe(command); if (command.parentElement) observer.observe(command.parentElement); });
    if (menu.current) observer.observe(menu.current);
    host.parentElement?.addEventListener("scroll", measure, true);
    return () => { observer.disconnect(); host.parentElement?.removeEventListener("scroll", measure, true); };
  });
  useEffect(() => {
    if (!menuOpen) return;
    menu.current?.querySelector('button')?.focus();
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") { setMenuOpen(false); bubble.current?.focus(); } };
    const outside = (event: PointerEvent) => { if (!menu.current?.contains(event.target as Node) && !bubble.current?.contains(event.target as Node)) setMenuOpen(false); };
    window.addEventListener("keydown", close);
    window.addEventListener("pointerdown", outside);
    return () => { window.removeEventListener("keydown", close); window.removeEventListener("pointerdown", outside); };
  }, [menuOpen]);

  // --- the voice call ---
  // The orb is always mounted, so it owns the `voice-call` / `voice-mute` surfaces; the 对话面板
  // call bar shares the very same store through `useVoiceCall` without registering them again.
  const call = useVoiceCall({ baseUrl, sessionId, voiceAvailable, register: true });
  const { status: callStatus, inCall, available: voice } = call;
  const startCall = call.start;
  const hangUp = () => { call.stop(); setMenuOpen(false); };
  useDisclosureSurface("voice-menu", { available: true, state: menuOpen, apply: setMenuOpen });
  useEffect(() => { if (!inCall && callStatus.state !== "error") setMenuOpen(false); }, [inCall, callStatus.state]);

  const look = inCall || callStatus.state === "connecting" ? orbState(callStatus) : operating ? "busy" : "standby";
  const activityLabel = operating ? "AI 操作中" : fresh ? "AI 待机" : "连接待确认";
  const callLabel = voice ? (inCall || callStatus.state === "error" ? orbLabel(callStatus) : "开始语音通话") : "";
  const ariaLabel = [voice ? (inCall ? `结束语音通话（${callLabel}）` : callLabel) : "AI 动态", activityLabel, unread ? "有新动态" : "", newMessage ? "对话有新消息" : "", "可拖动", "右键更多"].filter(Boolean).join("；");
  // The orb is the voice side of the one AI; the 对话面板 is its text side (same call, same chat).
  const title = voice ? `AI 语音：点一下${inCall ? "挂断" : callStatus.state === "error" ? "重试" : "通话"} · 右键更多` : "AI 动态：点一下查看 · 右键更多";
  // Error looks like offline (grey, owner 2026-11-07); why is said in the menu, not in red.
  const greyReason = callStatus.state === "error" ? orbLabel(callStatus) : !fresh && !inCall ? error || "连接待确认" : "";
  const onPrimary = () => {
    if (menuOpen) { setMenuOpen(false); return; }
    if (!voice) { if (open && !auto) closeCard(); else showCard(); return; }
    if (callStatus.state === "error") { call.retry(); return; }
    if (inCall) hangUp(); else startCall();
  };

  const windowMaxX = Math.max(0, size.width - orb), windowMaxY = Math.max(0, size.height - orb);
  const maxX = Math.max(size.leftInset, windowMaxX - size.rightInset), maxY = Math.max(0, windowMaxY - size.bottomInset);
  const safePosition = (x: number, y: number) => {
    const clamp = (point: { x: number; y: number }) => ({ x: Math.max(size.leftInset, Math.min(maxX, point.x)), y: Math.max(0, Math.min(maxY, point.y)) });
    const wanted = clamp({ x, y });
    // Only the market bars themselves are protected: a board's chart may sit below other content.
    const xs = [wanted.x, ...marketBars.flatMap(bar => [bar.left - orb - 8, bar.right + 8])];
    const ys = [wanted.y, ...marketBars.flatMap(bar => [bar.top - orb - 8, bar.bottom + 8])];
    const candidates = xs.flatMap(x => ys.map(y => clamp({ x, y }))).filter(point => marketBars.every(bar => point.x + orb <= bar.left - 8 || point.x >= bar.right + 8
      || point.y + orb <= bar.top - 8 || point.y >= bar.bottom + 8));
    return candidates.sort((a, b) => (a.x - wanted.x) ** 2 + (a.y - wanted.y) ** 2 - (b.x - wanted.x) ** 2 - (b.y - wanted.y) ** 2)[0] ?? wanted;
  };
  const { x: left, y: top } = safePosition(Math.round((position?.x ?? 0) * windowMaxX), position ? Math.round(position.y * windowMaxY) : maxY);
  const move = (x: number, y: number) => {
    const safe = safePosition(x, y);
    const next = { x: windowMaxX ? safe.x / windowMaxX : 0, y: windowMaxY ? safe.y / windowMaxY : 0 };
    setPosition(next);
    try { localStorage.setItem(FLOAT_POSITION_KEY, JSON.stringify(next)); } catch { /* Optional preference. */ }
  };
  const safeHeight = Math.max(0, size.height - size.bottomInset);
  const cardWidth = Math.min(348, size.width), cardHeight = Math.min(440, safeHeight);
  const cardLeft=Math.max(0,Math.min(left+orb-cardWidth,size.width-cardWidth));
  const cardTop=Math.max(0,Math.min(top-cardHeight-8,safeHeight-cardHeight));
  const besideWidth = Math.min(304, size.width);
  const besideLeft = Math.max(0, Math.min(left + orb / 2 > size.width / 2 ? left - 8 - besideWidth : left + orb + 8, size.width - besideWidth));
  // Bottom-aligned with the orb, growing upwards; an orb near the top grows it downwards instead.
  const besidePlace = top < 120 ? { top } : { bottom: Math.max(0, size.height - top - orb) };
  const menuPlace = useMenuPlace(menu, size.left + left, size.top + top + orb + 8, `${menuOpen}:${size.menuHeight}:${size.menuWidth}`, size.top + top - 8);
  return <div className="ai-floating-area" ref={area}>
    <button type="button" className="ai-orb" data-testid="ai-orb" data-state={look} data-muted={callStatus.muted ? "true" : undefined} data-call={inCall ? "true" : undefined}
      data-operating={operating||undefined} data-connection={fresh?"ready":"unavailable"} data-unread={unread || undefined} data-new-message={newMessage || undefined}
      title={title} ref={bubble} style={{ left, top, width: orb, height: orb, "--voice-level": callStatus.level ?? 0 } as React.CSSProperties}
      aria-label={ariaLabel} aria-pressed={voice ? inCall : undefined} aria-expanded={open} aria-controls="ai-floating-card"
      onPointerDown={event => { if (event.button !== 0) return; suppressClick.current = false; drag.current = { x: event.clientX, y: event.clientY, left, top, moved: false }; event.currentTarget.setPointerCapture(event.pointerId);
        window.clearTimeout(pressTimer.current); pressTimer.current = window.setTimeout(() => { if (!drag.current?.moved) { suppressClick.current = true; setMenuOpen(true); } }, LONG_PRESS_MS); }}
      onPointerMove={event => { const start = drag.current; if (!start) return; const dx = event.clientX - start.x, dy = event.clientY - start.y; if (Math.hypot(dx, dy) > 5) { start.moved = true; window.clearTimeout(pressTimer.current); } if (start.moved) move(start.left + dx, start.top + dy); }}
      onPointerUp={event => { window.clearTimeout(pressTimer.current); suppressClick.current = suppressClick.current || Boolean(drag.current?.moved); drag.current = undefined; if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); }}
      onPointerCancel={() => { window.clearTimeout(pressTimer.current); drag.current = undefined; suppressClick.current = true; }} onLostPointerCapture={() => { window.clearTimeout(pressTimer.current); drag.current = undefined; }}
      onContextMenu={event => { event.preventDefault(); setMenuOpen(value => !value); }}
      onClick={event => { if (event.detail > 0 && suppressClick.current) { suppressClick.current = false; return; } onPrimary(); }}
      onKeyDown={event => { const step = 16; const delta = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[event.key]; if (delta) { event.preventDefault(); move(left + delta[0], top + delta[1]); } if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) { event.preventDefault(); setMenuOpen(true); } }}>
      <AiAvatar kind={avatar} size={orb} level={callStatus.muted ? 0 : callStatus.level ?? 0}
        state={callStatus.state === "error" ? "error" : !fresh && !inCall ? "offline" : look} />
      {inCall ? <span className="ai-orb-badge" aria-hidden="true">{callStatus.muted ? <MicOff /> : <Mic />}</span> : null}
      {unread || newMessage ? <i className="ai-orb-dot" /> : null}
    </button>
    {beside ? <div className="ai-orb-beside" style={{ left: besideLeft, width: besideWidth, ...besidePlace }}>{beside}</div> : null}
    {voice && (inCall || callStatus.state === "error") ? <span className="ai-orb-label" role="status" data-state={callStatus.state} style={{ left: size.width > 2 * ORB_LABEL_HALF ? Math.min(Math.max(left + orb / 2, ORB_LABEL_HALF), size.width - ORB_LABEL_HALF) : left + orb / 2, top: top + orb + 4 > safeHeight - 24 ? top - 26 : top + orb + 4 }}>{callLabel}</span> : null}
    <Presence>{menuOpen ? createPortal(
      <menu ref={menu} className="ai-orb-menu" role="menu" aria-label="AI 球菜单" style={{ left: menuPlace.left, top: menuPlace.top }}>
        {greyReason ? <li role="presentation" className="ai-orb-menu-note">{greyReason}</li> : null}
        {inCall ? <li><button type="button" role="menuitem" onClick={() => { call.mute(!callStatus.muted); setMenuOpen(false); }}>{callStatus.muted ? <><Mic />取消静音</> : <><MicOff />静音</>}</button></li> : null}
        {inCall ? <li><button type="button" role="menuitem" onClick={hangUp}><PhoneOff />挂断</button></li> : null}
        <li><button type="button" role="menuitem" onClick={() => { onOpenChat(); if (inCall) hangUp(); setMenuOpen(false); }}><MessageSquareText />切文字{newMessage ? "（有新消息）" : ""}</button></li>
        <li><button type="button" role="menuitem" onClick={() => showCard()}><Activity />AI 动态{unread ? "（有新）" : ""}</button></li>
      </menu>, menuHost()
    ) : null}</Presence>
    {(open||closing) && <section inert={closing} aria-hidden={closing||undefined} data-closing={closing||undefined} data-auto={auto||undefined} id="ai-floating-card" className="ai-floating-card" role="region" aria-label="AI 动态卡片" style={{ width: cardWidth, maxHeight: cardHeight, left:cardLeft,top:cardTop,transformOrigin:`${left+orb/2-cardLeft}px ${top+orb/2-cardTop}px` }}
      onPointerEnter={() => { if (auto) { window.clearTimeout(autoTimer.current); setAuto(false); setUnread(false); } }}>
      <header><div><strong>AI 动态</strong><small>{operating?"AI 操作中":fresh?"待机":"连接待确认"}</small></div><button type="button" aria-label="收起 AI 动态" onClick={closeCard}>×</button></header>
      <div className="ai-floating-scroll">
        {config.showAiActivity && <AiActivity baseUrl={baseUrl} scope={`${identity}:${baseUrl}:${activity?.environment}:${activity?.replayId}:${symbol}`} activity={activity} error={error} />}
        {briefs.map(item => <article key={item.id} className="ai-market-brief"><header><strong>{item.kind === "news" ? "重要资讯" : "行情提醒"}</strong>{briefProduct(item) ? <em className="ai-brief-product" title={item.symbol}>{briefProduct(item)}</em> : null}<time>{briefTime(item.publishedAt)}</time></header><p>{item.text}</p>{item.url ? <a href={item.url} target="_blank" rel="noopener noreferrer">{item.source} · 查看原文 ↗</a> : <small>{item.source}</small>}</article>)}
      </div>
    </section>}
  </div>;
}

import { contractLabel } from "./futures/support";
import { AccountAccessContext, useAccountAccess, type WorkspaceAccess } from "../hooks/useAccountAccess";
import { GuestAppearanceContext } from "../guestAppearance";
import { GUEST_SETTINGS_PAGES, guestHistoryNote } from "../guestApi";
import { ResumeExecutionDialog } from "./ResumeExecutionDialog";
import { UiNavigationContext, type StrategyNavigation, type StrategyNavigationState } from "../uiNavigation";
import { useModelCatalog } from "../hooks/useModelCatalog";
import { useTradingAccounts, type TradingAccountsView } from "../hooks/useTradingAccounts";
import { useAccountNavigation } from "../hooks/useAccountNavigation";
import { AccountPnl } from "./AccountPnl";
import { TradeReview } from "./TradeReview";
import { ChevronLeft, CircleAlert, GripVertical, PanelRightClose, ReceiptText } from "lucide-react";
import { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { chartSymbolOf, requestJson, sameContract } from "../api";
import type { AccountPosition, MarketWorkbenchSnapshot, SimulationOrder } from "../api";
import { ConnectionProgress } from "./ConnectionProgress";
import { AiOrb, useAppWorkbench } from "./AiActivity";
import { useVoiceRealtimeAvailable, useVoiceUnavailableReason } from "../hooks/useVoiceRealtimeAvailable";
import { useOwnerPlan } from "../hooks/useOwnerPlan";
import { AppHelp, openAppHelp } from "./AppHelp";
import { marketClosedNow, marketSessionDisplay } from "../marketSession";
import "./FuturesDecisionWorkspace.css";
import "./futures/DockPanels.css";
import { decimalsForTick, formatNumber, isProtectionActive, maxSharedDockWidth, EDITOR_CHART_MIN, isForeignSymbol, ownPositions, periods, positionKey, WIDE_PANELS,
  FOREIGN_BOARD, isForeignProductKey, normalizeBoardColumns, normalizeBoardExchange, normalizeBoardSort, normalizeWatchlist, watchlistKey, WATCHLIST_MAX,
  type BoardColumn, type BoardExchange, type BoardSort, type FuturesWorkspaceView, type ManualSimulationRequest,
  type RecentWorkspaceAction, type RightDock, type StrategyTab, type TicketDraft } from "./futures/support";
import type { ChartIndicatorPatch } from "./chartIndicatorEditor";
import { normalizeChartIndicators } from "./chartIndicators";
import { TradeTicket } from "./futures/TradeTicket";
import { MarketBoard } from "./futures/MarketBoard";
import { ActivityPanel, PositionsBar, orderIsActive, type FillsSource, type PositionStopRequest, type PositionsBarTab } from "./futures/PositionPanels";
import { useVoiceCall } from "../voiceCallControls";
import { useBriefs } from "./AiActivity";
import { OrderDepth, OrderPanelPositions, type LadderPick } from "./futures/OrderPanelSections";
import { REMINDER_SETTINGS_PAGE, WorkspaceSettings, normalizeSettingsPage, type SettingsPage } from "./futures/WorkspaceSettings";
import { routineEditPrompt } from "./cards/RoutineCards";
import { routineRunPrompt, type RoutineRef } from "../routines";
import { ContractDrawer } from "./futures/WorkspaceDrawers";
import { AiDock } from "./futures/AiDock";
import { AiCommandBar } from "./futures/AiCommandBar";
import { AiCommandBubble, useAiCommand } from "./futures/useAiCommand";
import { AiWatching } from "./futures/AiWatching";
import { NewsPanel, type NewsTab } from "./NewsPanel";
import { useDebateHost } from "./ResearchDebate";
import { productOfSymbol } from "./FlashFeed";
import { StrategyDock } from "./futures/StrategyDock";
import { applyUiSurface, uiSurface, uiSurfaceStates, useUiSurface } from "../uiSurfaces";
import type { AiFocus } from "./AiMenu";
import { PositionsDock } from "./futures/PositionsDock";
import { MarketCommand } from "./futures/MarketCommand";
import { ChartStage } from "./futures/ChartStage";
import { HighlightLayer } from "./HighlightLayer";
import { TourIndicator } from "./TourIndicator";
import { ToolsRail } from "./futures/ToolsRail";
import { WorkspaceHeader } from "./futures/WorkspaceHeader";
import { buildPromptContext } from "./futures/promptContext";
import { useChartFocus } from "./futures/useChartFocus";
import { useQuickOrderSkipConfirm, useQuickOrders } from "./futures/useQuickOrders";
import { QuickActions, useTicketQuickActions } from "./futures/QuickActions";
import { QuickOrderDialog } from "./futures/QuickOrderDialog";
import { useTradeReview } from "../hooks/useTradeReview";
import { useForeignBoard } from "../hooks/useForeignBoard";
import { useMarketBoard } from "../hooks/useMarketBoard";
import type { BoardWant } from "../feedView";
import { useWorkspaceChat } from "./futures/useWorkspaceChat";
import { useWorkspaceLayout } from "./futures/useWorkspaceLayout";
import { useWorkspaceUiControl } from "./futures/useWorkspaceUiControl";
import { useUiControlHealth } from "../hooks/useUiControl";
import { usePriceAlerts } from "../hooks/usePriceAlerts";
import { useChartDrawings } from "../hooks/useChartDrawings";

import { rememberStartupChartView, rememberStartupDuration, startupChartView, useUserPreferences, DEFAULT_QUICK_SKILLS, preferenceErrorText, type ChartView } from "../userPreferences";
import { useSkills } from "../hooks/useSkills";
import { quickActionSkills, skillBlockedLine, skillUnavailableLine } from "./futures/skillAccess";
import { announceReminder, configureNotifications } from "../notifications";
import { onReminder, onStrategySignal } from "../runtimeFeed";
import { bumpDebates, debateShownNow } from "../researchDebate";
import { shortcutAction } from "../shortcuts";
import { PttButton, PttLive, usePushToTalk } from "./futures/PushToTalk";
import { voiceReply } from "../voiceReply";
import { applyChartAnnotations, setChartMarksScope, useChartAnnotations, type ChartAnnotationInput } from "../aiAnnotations";
import type { FuturesDecisionWorkspaceProps } from "./futures/props";
import { CHAT_PLACE_LABELS, useChatSuggestions } from "../hooks/useChatSuggestions";
import type { ChatSuggestionView } from "../api";
import { useMobileLayout } from "../hooks/useMobileLayout";
import { MobileWorkspace, locateCard, type MobilePage, type MobileTab } from "./mobile/MobileWorkspace";
import { useDeskJudgment, useDeskJudgments } from "./mobile/MobileFeed";
import { MobileHome, type WatchInfo } from "./mobile/MobileHome";
import { MobileStrategy, useMobileStrategies } from "./mobile/MobileStrategy";
import { MobileStrategyEditor, useMobileStrategyEditor } from "./mobile/MobileStrategyEditor";
import { StrategyStarts } from "./StrategyStarts";
import { MobileLab, type MobileLabReport } from "./mobile/MobileLab";
import { CardRenderer } from "./Conversation";
import { signalActionLabel } from "./StrategySignalAction";
import { closeCheckTime } from "../marketSession";
import { MobileResearch } from "./mobile/MobileResearch";
import { MobileMarket } from "./mobile/MobileMarket";
import { useMobileMarket } from "./mobile/useMobileMarket";
import { MobileMe } from "./mobile/MobileMe";
import { WebUpdateBanner } from "./WebUpdate";
import { CLIENT_BUILD, registerWebUpdateBusy } from "../clientBuild";
import { editorBusy, hideDraft } from "../strategyEditorStore";
import { pushToTalkStore } from "../pushToTalk";
import { voiceCallStore } from "../voiceRealtime";
import { MobileOrder } from "./mobile/MobileOrder";
import { MobileSignIn } from "./mobile/MobileSignIn";
import { buildMobileFeed, isPending } from "./mobile/feedItems";
import { judgmentChips, notifyProducts, notifyRow, productKey, type NotifyFilter, type NotifyRow } from "./mobile/notifyItems";
import { useReminders, type ReminderFilter, type ReminderItem } from "../reminders";
import { LatestReminders, MobileReminders, ReminderBell, type ReminderCenterProps } from "./reminders/ReminderCenter";
import { RemindersPanel, RemindersTabBody, RemindersTabs, type RemindersTab } from "./reminders/RemindersPanel";
import type { MyRemindersProps } from "./reminders/MyReminders";
import { ReminderSettings } from "./reminders/ReminderSettings";
import { mainContract } from "./mobile/MobileResearch";
import { MobileBriefs } from "./mobile/MobileBriefs";
import { InstallGuide, useInstallAutoOpen, useInstallDetection } from "./mobile/InstallGuide";
import { MobilePush } from "./mobile/MobilePush";
import { pushRowValue, usePush } from "../webPush";
import { onDeepLinkMessage, takeDeepLink, type DeepLink } from "../deepLink";
import { useBriefSubscriptions } from "./BriefSubscriptions";
import { buildFlowItems } from "./futures/aiFlow";
import { FONT_SCALE_OPTIONS } from "../appearance";

/** Open, these hold an order, a trading confirmation or unsaved strategy edits: no self-reload then. */
const BUSY_DIALOGS = ["quick-order-confirm", "abandon-order", "strategy-launch", "protection-reprice-confirm"];
/** How long after the latest finished answer the preferences are read again (a burst of answers reads once). */
const PREFERENCES_REREAD_MS = 1_500;

export type { FuturesWorkspaceView, ManualSimulationRequest, SimulationLimitSettings } from "./futures/support";

export function FuturesDecisionWorkspace(props: FuturesDecisionWorkspaceProps) {
  const isGuest = Boolean(props.guest);
  const accessRef = useRef<WorkspaceAccess | undefined>(undefined);
  const [accessNotice, setAccessNotice] = useState("");
  useEffect(() => { if (!accessNotice) return; const timer = setTimeout(() => setAccessNotice(""), 5000); return () => clearTimeout(timer); }, [accessNotice]);
  const [view, setView] = useState<FuturesWorkspaceView>("trade");
  // 分时图 (2026-10-02): the workspace's own chart view — 分时 is a view of the 60 s serial, not a
  // duration, so it never travels through props.duration (the replay's chartTimeframe stays a period).
  const [chartView, setChartView] = useState<ChartView>(startupChartView);
  const uiControlHealth = useUiControlHealth();
  const connectedRuntimeUrl = !props.guest && props.connection === "connected" ? props.runtimeBaseUrl : undefined;
  const reviewAccountId = props.replayActive ? undefined
    : props.connection === "connected" ? props.state.runtime?.account?.id ?? null : null;
  // 本站模拟合成一个账户 (2026-10-21): the 外盘 USD ledger behind the session's RMB 本站模拟 account — read beside it in
  // 复盘 and 成交, never switched to. Only while that account's own reads run (no replay, connected).
  const foreignReviewAccountId = reviewAccountId ? props.state.runtime?.account?.foreignAccountId ?? undefined : undefined;
  const ledgerSnapshots = { CNY: props.account?.account, USD: props.account?.foreign?.account };
  // 设置重做第 2 步: the account preference document and its consumers in this window. Since
  // 图表指标可配置 (2026-10-02) it also owns the chart's indicator configuration; a guest window
  // (no document) keeps the same defaults plus a local override instead of server writes.
  const prefs = useUserPreferences(connectedRuntimeUrl);
  // 到价提醒 (2026-10-02): the user's own alerts on the Runtime. A guest has no Runtime URL, so
  // it stays without the bell and the chart lines. A replay window (2026-10-03) keeps both: its
  // runtimeBaseUrl already points at the replay child, so the bell, the lines and the crossings
  // all live inside that replay and vanish with it.
  const alerts = usePriceAlerts(connectedRuntimeUrl, !isGuest);
  // 提醒中心 (#932, 2026-10-15): the Runtime's reminders and their read marks — the toolbar bell's
  // popover, the phone's 提醒 page and the unread badge. A replay window keeps its own alerts in the
  // bell but has no reminder record.
  const reminders = useReminders(connectedRuntimeUrl, !isGuest && !props.replayActive);
  // 提醒 panel (2026-10-25)'s tab: 我的提醒 / 通用提醒 (was 设置 → 提醒) / 提醒记录 (was the bell's 提醒中心 popover).
  const [remindersTab, setRemindersTab] = useState<RemindersTab>("history");
  const openReminderSettingsRef = useRef<() => void>(() => undefined);
  const [reminderFilter, setReminderFilter] = useState<ReminderFilter>("all");
  // 用户画线 (2026-10-02): the user's own chart drawings on the Runtime, with the alerts'
  // availability (connected member; a replay window's lines live in the replay child since
  // 2026-10-03). 画线第二版: the chart stage owns the toolbar, the tool and the selection.
  const drawings = useChartDrawings(connectedRuntimeUrl, !isGuest);
  const [guestChartPatch, setGuestChartPatch] = useState<ChartIndicatorPatch>({});
  const chartPreferences = useMemo(() => isGuest ? { ...prefs.preferences.chart, ...guestChartPatch } : prefs.preferences.chart, [isGuest, prefs.preferences.chart, guestChartPatch]);
  const chartIndicators = useMemo(() => normalizeChartIndicators(chartPreferences), [chartPreferences]);
  // 图表指标就地编辑 (2026-10-02): the chart labels' and the settings page's edits go through the
  // account preference path; a guest window keeps them locally (no server write, no error thrown).
  const updatePreferences = prefs.update;
  const updateChartPreferences = useCallback((patch: ChartIndicatorPatch) => {
    if (isGuest) { setGuestChartPatch(current => ({ ...current, ...patch })); return; }
    if (!connectedRuntimeUrl) throw new Error("连接后可设置");
    void updatePreferences({ chart: patch }).catch(() => undefined);
  }, [isGuest, connectedRuntimeUrl, updatePreferences]);
  // While a member window is disconnected the surfaces stay read-only (the same rule as the
  // settings rows); commands still reach the throwing path above and report 连接后可设置.
  const chartIndicatorsChange = isGuest || connectedRuntimeUrl ? updateChartPreferences : undefined;
  const layout = useWorkspaceLayout(props, chartPreferences);
  const { chartSettings, setChartSettings, dockSide, setDockSide, panelWidth, panelMaxWidth, editorPanel, positionsBar, setPositionsBar, recentSymbols, pinnedSymbols, pinSymbol, unpinSymbol, initialBalance, setInitialBalance, resolvedTheme, preferenceStatus, resetLayout, startDockResize, resizeDockWithKeyboard } = layout;
  const [positionsTab, setPositionsTab] = useState<PositionsBarTab>("positions");
  const [headerCurrentContract, setHeaderCurrentContract] = useState<string>();
  // 成交 · 来源 (持仓面板) and the 策略面板's tab; both are UI commands (`fillsSource`, `strategyTab`).
  const [fillsSource, setFillsSource] = useState<FillsSource>("all");
  const [strategyTab, setStrategyTab] = useState<StrategyTab>("library");
  const togglePositionsBar = () => setPositionsBar(positionsBar === "expanded" ? "collapsed" : "expanded");
  const [contractOpen, setContractOpen] = useState(false);
  // 合约抽屉两级 (2026-10-01): the one expanded product; reported while the drawer is open.
  const [contractProduct, setContractProduct] = useState<string | null>(null);
  // 报价总表列可自定义 (2026-10-02): which columns show (order = display order), the sort and the
  // exchange filter live in the account's marketBoard preference, so the board opens as it was left.
  // The document is the source of truth: local state mirrors the normalized values while a click is
  // pending its write-back (500 ms debounce — rapid header toggles become one save). The column list
  // itself is discrete, so it saves on every edit; a guest window keeps a local patch instead.
  const [guestBoardPatch, setGuestBoardPatch] = useState<{ columns?: string[]; sort?: BoardSort; exchange?: BoardExchange }>({});
  const boardPreference = useMemo(
    () => isGuest ? { ...prefs.preferences.marketBoard, ...guestBoardPatch } : prefs.preferences.marketBoard,
    [isGuest, prefs.preferences.marketBoard, guestBoardPatch]);
  const boardColumns = useMemo(() => normalizeBoardColumns(boardPreference?.columns), [boardPreference]);
  const documentSort = useMemo(() => normalizeBoardSort(boardPreference?.sort), [boardPreference]);
  const documentExchange = normalizeBoardExchange(boardPreference?.exchange);
  const [boardExchange, setBoardExchangeState] = useState<BoardExchange>(documentExchange);
  const [boardSort, setBoardSortState] = useState<BoardSort>(documentSort);
  const pendingBoard = useRef<{ exchange?: BoardExchange; sort?: BoardSort }>({});
  const documentBoard = useRef({ exchange: documentExchange, sort: documentSort });
  documentBoard.current = { exchange: documentExchange, sort: documentSort };
  // A write that did not land (409/422/offline) gives the value back to the document, unless a newer
  // click already took that field over (#522).
  const abandonBoardWrite = useCallback((patch: { exchange?: BoardExchange; sort?: BoardSort }) => {
    const pending = pendingBoard.current;
    if (patch.exchange !== undefined && pending.exchange === patch.exchange) {
      pending.exchange = undefined;
      setBoardExchangeState(documentBoard.current.exchange);
    }
    if (patch.sort !== undefined && pending.sort === patch.sort) {
      pending.sort = undefined;
      setBoardSortState(documentBoard.current.sort);
    }
  }, []);
  useEffect(() => {
    const pending = pendingBoard.current;
    if (pending.exchange !== undefined && pending.exchange === documentExchange) pending.exchange = undefined;
    if (pending.sort !== undefined && pending.sort.key === documentSort.key && pending.sort.order === documentSort.order) pending.sort = undefined;
    // Only values with no write in flight follow the document (an AI write lands here too).
    if (pending.exchange === undefined) setBoardExchangeState(documentExchange);
    if (pending.sort === undefined) setBoardSortState(documentSort);
  }, [documentExchange, documentSort]);
  const boardWriteTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(boardWriteTimer.current), []);
  // The debounce carries every change made inside its window, so a sort click followed by a
  // filter click becomes one save holding both.
  const queuedBoard = useRef<{ exchange?: BoardExchange; sort?: BoardSort }>({});
  const flushBoard = useCallback(() => {
    const patch = queuedBoard.current;
    queuedBoard.current = {};
    if (patch.exchange === undefined && patch.sort === undefined) return;
    pendingBoard.current = { ...pendingBoard.current, ...patch };
    if (isGuest) { setGuestBoardPatch(current => ({ ...current, ...patch })); return; }
    if (!connectedRuntimeUrl) { abandonBoardWrite(patch); return; }
    void updatePreferences({ marketBoard: patch }).catch(() => abandonBoardWrite(patch));
  }, [isGuest, connectedRuntimeUrl, updatePreferences, abandonBoardWrite]);
  const queueBoardWrite = useCallback((patch: { exchange?: BoardExchange; sort?: BoardSort }) => {
    queuedBoard.current = { ...queuedBoard.current, ...patch };
    window.clearTimeout(boardWriteTimer.current);
    boardWriteTimer.current = window.setTimeout(flushBoard, 500);
  }, [flushBoard]);
  const setBoardExchange = useCallback((value: BoardExchange) => {
    setBoardExchangeState(value);
    queueBoardWrite({ exchange: value });
  }, [queueBoardWrite]);
  const setBoardSort = useCallback((value: BoardSort) => {
    setBoardSortState(value);
    queueBoardWrite({ sort: value });
  }, [queueBoardWrite]);
  const updateBoardColumns = useCallback((columns: BoardColumn[]) => {
    if (isGuest) { setGuestBoardPatch(current => ({ ...current, columns })); return; }
    if (!connectedRuntimeUrl) throw new Error("连接后可设置");
    void updatePreferences({ marketBoard: { columns } }).catch(() => undefined);
  }, [isGuest, connectedRuntimeUrl, updatePreferences]);
  const boardColumnsChange = isGuest || connectedRuntimeUrl ? updateBoardColumns : undefined;
  const [boardQuery, setBoardQuery] = useState("");
  // 自选品种 (2026-10-02): the starred products ("<交易所>.<品种>", newest last) — one account
  // list the board's 自选 filter, the contract drawer's product rows and the contract head share
  // (the same star in all three). A star writes the whole list; a guest window keeps a local
  // patch, like the board's own, and the reason a write failed shows beside the surface that
  // made it.
  const [guestWatchlist, setGuestWatchlist] = useState<string[]>([]);
  const watchlist = useMemo(() => normalizeWatchlist(isGuest ? guestWatchlist : prefs.preferences.watchlist?.products),
    [isGuest, guestWatchlist, prefs.preferences.watchlist]);
  const [watchlistStatus, setWatchlistStatus] = useState("");
  const toggleWatchlist = useCallback((product: string) => {
    setWatchlistStatus("");
    if (isGuest) {
      // Same functional update: a second star while the first still renders lands on the new list.
      setGuestWatchlist(current => {
        const list = normalizeWatchlist(current);
        if (list.includes(product)) return list.filter(item => item !== product);
        if (list.length >= WATCHLIST_MAX) { setWatchlistStatus(`自选最多 ${WATCHLIST_MAX} 个，先移出一些再加`); return list; }
        return [...list, product];
      });
      return;
    }
    if (!connectedRuntimeUrl) { setWatchlistStatus("连接后可设置"); return; }
    // The list is computed when the queued write runs, off the latest saved document — two quick
    // stars both land even if the first save has not returned (and the 50-limit is judged there).
    void updatePreferences(latest => {
      const list = normalizeWatchlist(latest?.preferences.watchlist?.products);
      const removing = list.includes(product);
      if (!removing && list.length >= WATCHLIST_MAX) throw new Error(`自选最多 ${WATCHLIST_MAX} 个，先移出一些再加`);
      return { watchlist: { products: removing ? list.filter(item => item !== product) : [...list, product] } };
    }).catch(error => setWatchlistStatus(preferenceErrorText(error)));
  }, [isGuest, connectedRuntimeUrl, updatePreferences]);
  const watchlistChange = isGuest || connectedRuntimeUrl ? toggleWatchlist : undefined;
  // "+" on the symbol tabs opens the same drawer; the chosen contract is pinned as well as selected.
  const [contractPinMode, setContractPinMode] = useState(false);
  // 委托"历史" lives here so the bottom bar and the 持仓面板 share one switch (and one command).
  const [ordersHistory, setOrdersHistory] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [desktopVersion, setDesktopVersion] = useState<string>();
  useEffect(() => {
    let active = true;
    void import("@tauri-apps/api/app").then(({ getVersion }) => getVersion())
      .then(version => {
        if (active) setDesktopVersion(version);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);
  const [settingsPage, setSettingsPage] = useState<SettingsPage>(props.guest ? "ui" : "account");
  const [strategyUi, setStrategyUi] = useState<StrategyNavigationState | null>(null);
  const strategyNavigation = useRef<((changes: StrategyNavigation) => void) | undefined>(undefined);
  const navigation = useMemo(() => ({ publish: setStrategyUi, handler: strategyNavigation }), []);
  const [rightDock, setRightDock] = useState<RightDock>(null);
  // 调研面板's tab, held here so a debate started from 快讯 / 今日判断 / a command can switch to 辩论; the panel
  // opens on 品种调研 again once it is closed.
  // 今日作战表 (第 5 批) opens first for a member; a guest has no 自选 and starts on 品种调研.
  const firstNewsTab: NewsTab = props.guest ? "desk" : "sheet";
  const [newsTab, setNewsTab] = useState<NewsTab>(firstNewsTab);
  useEffect(() => { if (rightDock !== "news") setNewsTab(firstNewsTab); }, [rightDock, firstNewsTab]);
  // 手机版 (2026-10-04): a phone held upright shows the phone layout (outside a replay); its AI 对话
  // page is the 对话面板 itself, so `chat` is the ai dock and the other tabs live here. A visitor's
  // window opens on 看盘 (AI / 策略 / 我的 are 登录后使用); after login the member's opens on AI.
  const mobile = useMobileLayout() && !props.replayActive;
  const [mobilePage, setMobilePage] = useState<Exclude<MobileTab, "chat">>(isGuest ? "market" : "ai");
  const mobileTab: MobileTab = rightDock === "ai" ? "chat" : mobilePage;
  // 手机版第二期: the phone's own full-screen pages (下单 / 持仓与委托 / 复盘) and the AI 首页 timeline's
  // filter (原通知页). 2026-10-06: install = 安装到桌面引导; lab = 回测结果页 (its comparison: null = latest).
  // 2026-10-15: reminders = 提醒 (the 提醒中心 the bell opens).
  const [mobileSub, setMobileSub] = useState<"order" | "positions" | "review" | "briefs" | "install" | "push" | "lab" | "reminders" | null>(null);
  const [notifyFilter, setNotifyFilter] = useState<NotifyFilter>("all");
  const [notifyProduct, setNotifyProduct] = useState<string | null>(null);
  const [mobileLabId, setMobileLabId] = useState<string | null>(null);
  // 手机版 策略 (2026-10-08): its two tabs (`mobileStrategyTab`).
  const [mobileStrategyTab, setMobileStrategyTab] = useState<"running" | "mine">("running");
  // 方案 B (2026-11-01): what is said or typed on 策略 is about its selected strategy.
  const [mobileStrategyFocus, setMobileStrategyFocus] = useState<AiFocus | undefined>(undefined);
  const [mobileLab, setMobileLab] = useState<MobileLabReport | null>(null);
  // Bumped to scroll AI 首页 to its timeline (the filter commands, "看看时间线").
  const [timelineFocus, setTimelineFocus] = useState(0);
  const focus = useChartFocus(props.workspaceScope, props.runtimeBaseUrl, props.selectedSymbol, props.duration);
  const { setTradeFocus, activeStrategy, selectedEvent, explanationSupported } = focus;
  useEffect(() => {
    const open = () => { if (!accessRef.current?.require("ai-chat")) return; setSettingsOpen(false); setRightDock("ai"); };
    // Panels that cannot reach the settings drawer directly (strategy panel, P&L page) ask for a page by event.
    const openSettings = (event: Event) => { if ((event as CustomEvent<string>).detail === REMINDER_SETTINGS_PAGE) { openReminderSettingsRef.current(); return; }
      const page = normalizeSettingsPage((event as CustomEvent<string>).detail); if (isGuest && !GUEST_SETTINGS_PAGES.includes(page) && !accessRef.current?.require("preferences")) return; setSettingsPage(page); setSettingsOpen(true); };
    window.addEventListener("ai-trader-open-ai", open);
    window.addEventListener("ai-trader-open-settings", openSettings);
    return () => { window.removeEventListener("ai-trader-open-ai", open); window.removeEventListener("ai-trader-open-settings", openSettings); };
  }, [isGuest]);
  useEffect(() => { if (props.replayEnded) { setView("review"); setRightDock(null); } }, [props.replayEnded]);
  // 右侧栏重设计 v2: 对话 / 策略 / 新闻 / 下单 / 持仓 are sibling panels. Since 2026-09-25 the
  // 对话 panel holds the conversation only; fills live in 持仓 → 成交.
  const openChat = useCallback(() => { if (accessRef.current?.require("ai-chat")) setRightDock("ai"); }, []);
  // 建议问题 follow where the user just was: the view, or the 策略/持仓 panel they opened last
  // (the 对话 panel replaces it in the same dock, so the panel is remembered, not looked up).
  const [examplesFrom, setExamplesFrom] = useState<ChatSuggestionView>("trade");
  useEffect(() => { setExamplesFrom(view === "board" || view === "review" ? view : "trade"); }, [view]);
  useEffect(() => { if (rightDock === "strategy" || rightDock === "positions") setExamplesFrom(rightDock); }, [rightDock]);
  // A trade marker picked on the chart: the 持仓面板's 成交 tab.
  const openFills = useCallback(() => { if (!accessRef.current?.require("account")) return; setPositionsTab("fills"); setRightDock("positions"); }, []);
  const [query, setQuery] = useState("");
  const [selectedPrice, setSelectedPrice] = useState<number>();
  const [selectedPositionKey, setSelectedPositionKey] = useState<string>();
  const [ticketDraft, setTicketDraft] = useState<TicketDraft>();
  const [closeRequest, setCloseRequest] = useState<{ id: number; direction: "long" | "short" }>();
  const [quickSkipConfirm, setQuickSkipConfirm] = useQuickOrderSkipConfirm(props.preferencesScope);
  useEffect(() => { setChartMarksScope(props.preferencesScope); }, [props.preferencesScope]);
  const [resumeOpen, setResumeOpen] = useState(false);
  const [emergencyOpen, setEmergencyOpen] = useState(false);
  const [recentActions, setRecentActions] = useState<RecentWorkspaceAction[]>([]);
  const [volume, setVolume] = useState(1);
  const market = useMemo(() => {
    if (!props.market) return undefined;
    const name = contractLabel(props.market.quote.symbol, props.instruments);
    return name === props.market.quote.symbol ? props.market
      : { ...props.market, quote: { ...props.market.quote, name } };
  }, [props.market, props.instruments]);
  const quote = market?.quote;
  const model = useModelCatalog(props.mode === "cloud" ? connectedRuntimeUrl : undefined, 0);
  // 4.4b: the session's trading accounts — the switcher in the header and 设置 → 账户,
  // and the `tradingAccount` UI command; the account id follows runtime.status alone.
  const accountsView = useTradingAccounts(connectedRuntimeUrl, props.chatSessionId, props.state.runtime);
  // An account's reminder opens on its own account once the window shows it (#932 R3).
  const reminderNavigation = useAccountNavigation({ currentId: accountsView.currentId, statusAt: props.state.runtimeUpdatedAt,
    sessionId: props.chatSessionId, runtimeUrl: connectedRuntimeUrl, requestSwitch: accountsView.requestSwitch, onNotice: setAccessNotice,
    known: accountId => Boolean(accountsView.directory?.accounts.some(account => account.id === accountId)) });
  const navigateOnAccount = reminderNavigation.navigate;
  // Every other switch (the selector, the `tradingAccount` command) goes through the same account-write lane: it
  // abandons a waiting reminder navigation and is sent after the write before it settled (Codex r4308219648).
  const tradingAccounts: TradingAccountsView = { ...accountsView, switchAccount: reminderNavigation.switchAccount };
  const appWorkbench = useAppWorkbench(connectedRuntimeUrl, quote?.symbol);
  const voiceAvailable = useVoiceRealtimeAvailable(connectedRuntimeUrl) === true;
  const voiceUnavailableReason = useVoiceUnavailableReason(connectedRuntimeUrl);
  // The signed-in tier supplies permission feedback; a local single-user Runtime
  // has no tiers, so the read stays cloud-only.
  const ownerPlan = useOwnerPlan(props.mode === "cloud" ? connectedRuntimeUrl : undefined);
  const planFeatures = ownerPlan.status?.features;
  // The skill registry as the current user sees it: the skill-gated buttons read this instead of
  // the legacy feature switches (planFeatures stays as the fallback for a pre-registry Runtime).
  const skills = useSkills(connectedRuntimeUrl, props.state.runtime).skills;
  const access = useAccountAccess({ guest: Boolean(props.guest), skills, status: ownerPlan.status,
    onLogin: props.onOpenCloudLogin, onNotice: setAccessNotice });
  accessRef.current = access;
  // A card made for another account than the session's (a strategy's card after the conversation switched, #1011)
  // names its own account under its title: the display name from the account directory.
  const directoryAccounts = accountsView.directory?.accounts;
  const proposalActions = useMemo(() => ({ ...props.proposalActions,
    accountName: (accountId: string) => directoryAccounts?.find(entry => entry.id === accountId)?.displayName }),
  [props.proposalActions, directoryAccounts]);
  // The same names on 提醒中心's rows, only while there is more than one account to tell apart (#932 F7).
  const reminderAccountName = (directoryAccounts?.length ?? 0) > 1 ? proposalActions.accountName : undefined;
  const workspace = { ...props, access, proposalActions };
  // 后台管理 (2026-09-30) lives on its own page at /trader/admin; the workspace itself
  // no longer hosts the console — `isOwner` only gates the owner-only help topics.
  const isOwner = ownerPlan.status?.role === "owner";
  // The registry's unified reason lines (null = usable) — the same wordings the
  // Runtime's unavailable_line gives voice and typed requests.
  const replayBlocked = skillBlockedLine("market-replay", skills, planFeatures, "行情回放");
  const backtestBlocked = skillBlockedLine("strategy-backtests", skills, planFeatures, "回测");
  const strategyBlocked = skillBlockedLine("strategy-management", skills, planFeatures, "策略");
  // One call for the whole window: the orb owns its `voice-call` / `voice-mute` surfaces, the
  // 对话面板 call bar drives the same store.
  const call = useVoiceCall({ baseUrl: connectedRuntimeUrl, sessionId: props.chatSessionId, voiceAvailable });
  // 设置重做第 2 步: the account preference document (read before the layout hook — it owns the
  // chart indicator configuration the presentation document derives its old switches from).
  const { preferences, document: preferenceDocument } = prefs;
  const preferencesLoaded = preferenceDocument !== undefined;
  // The AI changes preferences on the Runtime (update_preferences, e.g. 「把复盘放到快捷栏」): re-read
  // the document once per finished assistant answer so the window follows without a reload. One read for a burst:
  // the replayed history finishes hundreds of answers in a moment when the page opens (it read 25 times).
  const lastAnswer = props.state.turns.findLast(turn => turn.role === "assistant" && turn.completed)?.id;
  const refreshPreferences = prefs.refresh;
  useEffect(() => {
    if (!lastAnswer) return;
    const timer = window.setTimeout(refreshPreferences, PREFERENCES_REREAD_MS);
    return () => window.clearTimeout(timer);
  }, [lastAnswer, refreshPreferences]);
  useEffect(() => { configureNotifications(preferences.notify); }, [preferences.notify]);
  useEffect(() => {
    if (preferences.chart.upDownColors === "intl") document.documentElement.dataset.updown = "intl";
    else delete document.documentElement.dataset.updown;
  }, [preferences.chart.upDownColors]);
  // Account defaults applied once the document arrives: chart period (unless replay pins one or the user already
  // picked), 副图 panes, and the ticket's default volume.
  const guestAppearance = useContext(GuestAppearanceContext);
  const durationTouched = useRef(guestAppearance?.duration !== undefined);
  // The layout hook and the ref above have captured the guest's view; later mounts use the account's.
  useEffect(() => { guestAppearance?.consume?.(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  // 快捷技能栏: the pinned ids resolve against the registry (a pin the registry no
  // longer knows, or an id without a quick action, drops out); an unavailable pin
  // stays visible and explains its reason when clicked.
  const quickSkillChips = useMemo(() => {
    const pinned = preferences.ui?.quickSkills ?? DEFAULT_QUICK_SKILLS;
    const candidates = quickActionSkills(skills);
    return pinned.flatMap(id => {
      const skill = candidates.find(item => item.id === id);
      return skill ? [{
        id: skill.id,
        label: skill.label,
        prompt: skill.quickAction?.slice("prompt:".length) ?? "",
        blockedLine: skill.available ? null : skillUnavailableLine(skill),
      }] : [];
    });
  }, [skills, preferences.ui?.quickSkills]);
  const { defaultDuration, defaultView, aiAnnotationsAutoClearMinutes } = preferences.chart;
  useEffect(() => {
    if (!preferencesLoaded) return;
    // 分时 also pins the remembered duration to 60 s, so the next launch's first subscription is
    // already the one the 分时图 needs; the remembered view follows `chart.defaultView`.
    rememberStartupDuration(defaultView === "timeshare" ? 60 : defaultDuration);
    rememberStartupChartView(defaultView ?? "kline");
    if (props.replayActive || durationTouched.current) return;
    if (defaultView === "timeshare") {
      setChartView("timeshare");
      if (props.duration !== 60) props.onDurationChange(60);
    } else if (props.duration !== defaultDuration) {
      props.onDurationChange(defaultDuration);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preferencesLoaded, defaultDuration, defaultView]);
  useEffect(() => { setVolume(preferences.trade.defaultVolume); }, [preferences.trade.defaultVolume]);
  // AI 标注自动清除: a fresh set of drawings on the current contract clears itself after the configured minutes.
  const symbolAnnotations = useChartAnnotations(props.selectedSymbol);
  useEffect(() => {
    if (isGuest || !aiAnnotationsAutoClearMinutes || !symbolAnnotations.length || !props.selectedSymbol) return;
    const symbol = props.selectedSymbol;
    const timer = window.setTimeout(() => applyChartAnnotations(symbol, "clear"), aiAnnotationsAutoClearMinutes * 60_000);
    return () => window.clearTimeout(timer);
  }, [isGuest, symbolAnnotations, aiAnnotationsAutoClearMinutes, props.selectedSymbol]);
  // 活动保护: only statuses that still stand (the server's own protected-position set);
  // EXECUTED / CANCELLED / FAILED are terminal and no longer count as active.
  const activeProtections = appWorkbench.activity?.protections?.filter(p => (!activeStrategy || p.strategyId === activeStrategy) && isProtectionActive(p.status));
  // 主动提醒: a monitoring strategy's signal changed — the same drawings explain_strategy_signal
  // composes, plus one light notice over the market area. The notify preference was already read
  // by the Runtime; a client without an open feed simply never sees the event.
  const [signalNotice, setSignalNotice] = useState<string>();
  useEffect(() => isGuest ? undefined : onStrategySignal(payload => {
    if (payload.symbol && Array.isArray(payload.annotations) && payload.annotations.length) {
      try { applyChartAnnotations(payload.symbol, "set", payload.annotations as ChartAnnotationInput[]); }
      catch { /* Duplicate levels: the notice still shows; the drawings stay as they were. */ }
    }
    setSignalNotice(String(payload.spoken || `策略提醒：${payload.name ?? "策略"} 刚满足${payload.actionLabel ?? ""}条件`));
  }), [isGuest]);
  useEffect(() => {
    if (!signalNotice) return;
    const timer = window.setTimeout(() => setSignalNotice(undefined), 12_000);
    return () => window.clearTimeout(timer);
  }, [signalNotice]);
  // 统一提醒 (#932, 2026-10-15): every reminder the Runtime delivers — 成交与撤单, 急停, 到价, 收盘前,
  // 止损止盈 … — arrives as feed.reminder, already gated on that kind's own switch. This window shows
  // the desktop notification (titled by the payload) and the same light in-app notice as a strategy
  // signal; an update of a reminder's state (type update) only re-reads the 提醒中心. The fills,
  // cancels and 急停 this window used to judge from its own activity snapshot are the Runtime's now.
  const refreshAlerts = alerts.refresh;
  useEffect(() => isGuest ? undefined : onReminder(payload => {
    if (payload.kind === "price_alert") refreshAlerts();
    if (payload.type === "update") return;
    // 多 AI 研讨: the sub-view showing this debate already shows its result; any other debate's end still gets
    // the light notice (12 s).
    if (payload.kind === "research-debate" || payload.kind === "research_debate") {
      // Another debate ended while this window shows a different one (or none): its 已研讨 tag and the desk re-read.
      bumpDebates();
      if (debateShownNow(payload.debateId)) return;
    }
    announceReminder(String(payload.title || "提醒"), payload.text);
    setSignalNotice(String(payload.text || payload.title || "提醒"));
  }), [isGuest, refreshAlerts]);
  // A failed protection warns even though it is no longer active — the user should look at it.
  const protectionWarning = appWorkbench.activity?.protections?.some(p => (!activeStrategy || p.strategyId === activeStrategy) && ["FAILED","SUSPENDED"].includes(String(p.status)));
  const recordAction = (
    action: Omit<RecentWorkspaceAction, "occurredAt" | "symbol"> & { symbol?: string }
  ) => {
    const symbol = action.symbol ?? quote?.symbol;
    if (!symbol) return;
    setRecentActions((current) => [
      ...current,
      { ...action, symbol, occurredAt: new Date().toISOString() }
    ].slice(-12));
  };
  const applyOrderDraft = (draft: TicketDraft) => {
    setTicketDraft(draft);
    recordAction({
      action: "order_draft_created",
      side: draft.side,
      volume: draft.volume,
      price: draft.limitPrice
    });
  };
  const apiSetupRequired = Boolean(
    props.agentAccount?.configurationRequired || ["deepseek", "moonshot"].includes(props.agentAccount?.provider ?? "")
  );
  const apiConnectionFailed = Boolean(
    ["deepseek", "moonshot"].includes(props.agentAccount?.provider ?? "")
    && props.agentAccount?.credentialConfigured
    && !props.agentAccount?.authenticated
  );
  const apiProviderName = props.agentAccount?.provider === "moonshot" ? "Kimi" : "DeepSeek";
  const agentModelLabel = props.agentAccount?.provider === "moonshot" ? "Kimi K3.1 Pro · 低思考" : props.agentAccount?.provider === "deepseek"
    ? `DeepSeek · ${props.agentAccount.model === "deepseek-v4-pro" ? "V4 Pro" : "V4 Flash"}`
    : "模型未就绪";
  const currentPositions = useMemo(
    // A foreign chart (快期外盘主连) holds its product's USD sub-account month positions (外盘仓合并显示 2026-10-21).
    () => (props.account?.positions ?? []).filter((position) => sameContract(position.symbol, quote?.symbol)),
    [props.account?.positions, quote?.symbol]
  );
  // 底栏自动隐藏 (2026-09-30): with no positions and no working orders anywhere in the
  // account, the collapsed bar is not rendered and the chart takes the space.
  const autoHidePositionsBar = preferences.ui?.autoHidePositionsBar ?? true;
  const positionsBarAutoHidden = positionsBar === "collapsed" && autoHidePositionsBar
    && !(props.account?.positions?.length) && !props.simulationOrders.some(orderIsActive);
  // Only the chosen row: a lone position is chosen once when the rows change (below), so 取消选择 (the
  // selectPosition: null command) stays cleared instead of re-choosing it on every render (#932 P2: the command
  // never read as applied and the window answered 409 to everything after it).
  const selectedPosition = currentPositions.find((position) => positionKey(position) === selectedPositionKey);
  const decisionTurns = useMemo(() => props.state.turns
    .filter((turn) => !turn.hidden)
    .map((turn) => ({
      ...turn,
      cards: turn.cards.filter((card) => [
        "trade", "close-batch", "spread", "risk", "risk-rule", "backtest", "market", "strategy", "task-error",
        "task", "order-question", "research-judgment", "reminder", "execution", "routine", "routine-run", "answer", "data"
      ].includes(card.type) && !(card.type === "risk" && card.payload.title.startsWith("Codex"))
        // 回答卡片化 (2026-10-14): an account card a reply read (it carries a cardId) is shown; the session's
        // opening account snapshot (no cardId) stays out of the conversation as before.
        || (card.type === "account" && Boolean(card.payload.cardId)))
    }))
    .filter((turn) => !(
      turn.role === "assistant"
      && turn.cards.length === 0
      && (/Codex 已连接|Runtime 已就绪|Sente 已就绪|只读账户快照|内置假数据/.test(turn.text)
        // A card-only turn whose card this panel does not show (e.g. an account card) has nothing left.
        || (!turn.text.trim() && turn.completed !== false))
    )), [props.state.turns]);

  useEffect(() => {
    setSelectedPrice(undefined);
  }, [props.selectedSymbol, props.runtimeBaseUrl, props.workspaceScope]);


  // The rows themselves (not every account refresh): a lone position is preselected when it appears.
  const positionRows = currentPositions.map(positionKey).join("\n");
  useEffect(() => {
    const rows = positionRows ? positionRows.split("\n") : [];
    if (rows.length === 1) setSelectedPositionKey(rows[0]);
    if (!rows.length) setSelectedPositionKey(undefined);
  }, [positionRows]);


  const choosePrice = (price: number | undefined) => {
    setSelectedPrice(price);
    if (price === undefined) return;
    applyUiSurface("ticketPrice", price);
    recordAction({ action: "price_selected", price, view });
  };
  const selectView = (nextView: FuturesWorkspaceView) => {
    if (nextView === "review" && !access.require("review-summary")) return;
    setView(nextView);
    if (nextView !== "trade" && rightDock !== "ai") setRightDock(null);
    recordAction({ action: "view_changed", view: nextView });
  };
  const selectDuration = (duration: MarketWorkbenchSnapshot["durationSeconds"]) => {
    durationTouched.current = true;
    // Any period pick is the K 线 view — including the AI's `duration` command and the 1 分
    // button clicked straight out of 分时 (no re-subscribe needed for that one).
    setChartView("kline");
    props.onDurationChange(duration);
    recordAction({ action: "duration_changed", durationSeconds: duration, view });
  };
  // 分时图: a view of the same 60 s serial, not a duration — it pins the data period to 60 s and
  // leaves `duration` as the K 线 view's period.
  const selectChartView = (nextView: ChartView) => {
    durationTouched.current = true;
    setChartView(nextView);
    if (nextView === "timeshare" && props.duration !== 60) {
      props.onDurationChange(60);
      recordAction({ action: "duration_changed", durationSeconds: 60, view });
    }
  };
  const selectContract = (symbol: string) => {
    props.onSelectSymbol(symbol);
    if (contractPinMode) pinSymbol(symbol);
    setContractOpen(false);
    setContractPinMode(false);
    recordAction({ action: "contract_selected", symbol, view });
  };
  // A board row is that contract's trade workbench: select it and go back to the trade view.
  // 快讯 · 持仓与自选 (2026-10-03): the open positions' products plus the watchlist.
  const flashWatchProducts = useMemo(() => [...new Set([...(props.account?.positions ?? []).map(position => productOfSymbol(chartSymbolOf(position.symbol))), ...watchlist])],
    [props.account?.positions, watchlist]);
  const selectBoardContract = (symbol: string) => {
    selectContract(symbol);
    if (view !== "trade") selectView("trade");
  };
  // One board read serves the board view and the open 合约抽屉 (pushed while either shows, 2026-10-12):
  // the board view alone on one exchange or 自选 asks only for those rows; the drawer and the phone's
  // 报价 / 多图 (its automatic cells rank the whole board) need every row.
  const phoneMarket = mobile && mobileTab === "market" && !mobileSub;
  // 手机看盘 (2026-10-09): 报价 / 多图 / 单图 and their settings (account preference ui.mobileMarket; a visitor's
  // stay on this device), the single chart's sub pane from the shared chart.subPaneList, the `mobileMarket` command.
  const mobileMarket = useMobileMarket({ guest: isGuest, connected: Boolean(connectedRuntimeUrl), document: prefs.preferences.ui?.mobileMarket,
    update: updatePreferences, panes: chartIndicators.subPaneList, updateChartIndicators: chartIndicatorsChange,
    resolveSymbol: symbol => props.instruments?.items.find(item => item.symbol.toUpperCase() === symbol.toUpperCase())?.symbol,
    selectSymbol: symbol => selectContract(symbol), available: mobile && mobileTab === "market" && !mobileSub });
  // The phone's 报价 · 外盘 板块 has its own read too: only that board open means no domestic board.
  const phoneForeignOnly = phoneMarket && mobileMarket.market.view === "quotes" && mobileMarket.market.board === "FOREIGN";
  const boardWant = useMemo<BoardWant>(() => contractOpen || phoneMarket || boardExchange === "all" ? { all: true }
    : boardExchange === "watchlist" ? { products: watchlist } : { exchanges: [boardExchange] },
  [contractOpen, phoneMarket, boardExchange, watchlist]);
  // The 外盘 group has its own read: a window showing only it does not keep the domestic board subscribed.
  const board = useMarketBoard(connectedRuntimeUrl,
    (!mobile && view === "board" && boardExchange !== FOREIGN_BOARD) || contractOpen || (phoneMarket && !phoneForeignOnly), undefined, boardWant);
  // The desktop board is not on screen in the phone layout: neither of its reads runs there.
  // 外盘 rows are read for the 外盘 chip, and for 自选 when it holds a foreign product (外盘代码统一 2026-10-21).
  const foreignBoard = useForeignBoard(connectedRuntimeUrl, !mobile && view === "board"
    && (boardExchange === FOREIGN_BOARD || (boardExchange === "watchlist" && watchlist.some(isForeignProductKey))));
  const boardExchanges = useMemo(
    () => [...new Set((props.instruments?.items ?? []).map(item => item.exchangeId))], [props.instruments]);
  // A starred product without a board row still needs its display name (the grey 暂无报价 row).
  const boardProductNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const item of props.instruments?.items ?? []) {
      names[watchlistKey(item)] ??= (item.instrumentName.replace(/\d+$/, "") || item.productId).toUpperCase();
    }
    return names;
  }, [props.instruments]);
  // The contract head's star marks the shown contract's product (换月自动跟随 comes for free).
  const quoteEntry = quote ? props.instruments?.items.find(item => item.symbol.toUpperCase() === quote.symbol.toUpperCase()) : undefined;
  const watchlistHead = quoteEntry ? {
    product: watchlistKey(quoteEntry),
    name: (quoteEntry.instrumentName.replace(/\d+$/, "") || quoteEntry.productId).toUpperCase(),
    starred: watchlist.includes(watchlistKey(quoteEntry)),
  } : undefined;
  // A contract tab is that contract's trade workbench: switch the view and the contract together.
  const selectContractTab = (symbol: string) => {
    if (view !== "trade") selectView("trade");
    if (!sameContract(symbol, quote?.symbol)) { props.onSelectSymbol(symbol); recordAction({ action: "contract_selected", symbol, view: "trade" }); }
  };
  // Removing the current contract's tab moves to its neighbour; the last tab stays.
  const unpinContractTab = (symbol: string) => {
    if (pinnedSymbols.length <= 1) return;
    const index = pinnedSymbols.findIndex(item => sameContract(item, symbol));
    if (sameContract(symbol, quote?.symbol)) {
      const neighbour = pinnedSymbols[index + 1] ?? pinnedSymbols[index - 1];
      if (neighbour) selectContractTab(neighbour);
    }
    unpinSymbol(symbol);
  };
  // "平仓" on a bar row: the ticket's close page for that position, on that contract; the ticket still confirms.
  const closePosition = (position: AccountPosition) => {
    setSelectedPositionKey(positionKey(position));
    // A foreign month has no chart of its own: its product's 外盘主连 chart closes it (the ticket sends the row's own
    // month, which the Runtime routes to the USD sub-account).
    if (!sameContract(position.symbol, quote?.symbol)) props.onSelectSymbol(chartSymbolOf(position.symbol));
    if (mobile) setMobileSub("order");
    else { setView("trade"); setRightDock("order"); }
    setCloseRequest({ id: Date.now(), direction: position.direction });
    recordAction({ action: "position_selected", symbol: position.symbol, positionDirection: position.direction, view: "trade" });
  };
  // UI-control channel: reported state and the agent's commands for every user-operable surface.
  const promptContext = () => buildPromptContext({ market, account: props.account, duration: props.duration, view, rightDock, selectedEvent,
    explanationSupported, selectedPrice, currentPositions, selectedPosition, ticketDraft, recentActions });
  const chat = useWorkspaceChat({ workspace: props, promptContext });
  // 建议问题 (2026-10-05): the AI's own 0–2 questions, asked only while the 对话面板 shows (the phone's
  // chat page is the same dock) and the conversation can actually be used.
  const suggestions = useChatSuggestions({
    baseUrl: connectedRuntimeUrl,
    enabled: !isGuest && rightDock === "ai" && !props.replayActive
      && !(props.agentAccount && (!props.agentAccount.authenticated || props.agentAccount.configurationRequired))
      && !access.reason("ai-chat"),
    sessionId: props.chatSessionId, accountId: props.state.runtime?.account?.id,
    symbol: quote?.symbol ?? props.selectedSymbol, view: examplesFrom,
    busy: chat.sendingPrompt || props.state.turns.some(turn => turn.role === "assistant" && turn.completed === false),
    lastAnswerId: lastAnswer,
  });
  const { setPrompt } = chat;
  // 按住说话: one hold key for the whole window. On the desktop the AI 指令栏 takes the words (it registers as the hold
  // target); elsewhere pressing it opens the 对话面板 and starts recording at once; the released text is an ordinary
  // user message (the typed path) or goes into the input.
  const fillPrompt = (text: string) => setPrompt(current => current.trim() ? `${current.trimEnd()} ${text}` : text);
  // 我的流程 (2026-10-06) in 设置 → 记忆: 运行 is the typed path (“跑一下<名称>” as the user's own message,
  // metered like any turn) carrying the routine's id, so the Runtime runs the one picked (#522);
  // 改 opens the chat with “改一下『名称』：” for the user to finish.
  const routineActions = {
    run: ({ id, name }: RoutineRef) => {
      if (!access.require("ai-chat")) return;
      setSettingsOpen(false); setMobileSub(null); setRightDock("ai");
      void chat.sendQuickPrompt(routineRunPrompt(name), undefined, { routineId: id });
    },
    edit: (name: string) => {
      if (!access.require("ai-chat")) return;
      setSettingsOpen(false); setMobileSub(null); setRightDock("ai");
      setPrompt(routineEditPrompt(name));
    },
  };
  const ptt = usePushToTalk({
    baseUrl: props.guest?.baseUrl ?? connectedRuntimeUrl,
    guest: Boolean(props.guest),
    blockedReason: props.guest ? (props.guest.expired ? "游客会话已失效，请重新连接" : props.guest.loginOpen ? "请先完成或关闭登录" : !props.guest.session.voiceEnabled ? "游客语音暂不可用" : "") : !connectedRuntimeUrl ? "连接后可用" : call.inCall ? "通话中不能按住说话，先结束通话"
      : !call.available ? voiceUnavailableReason || "语音暂不可用"
      : props.agentAccount && !props.agentAccount.authenticated ? "模型尚未就绪，请在设置里选择模型" : "",
    // The phone's hold button always sends (its page has no input to fill but the chat's).
    sendOnRelease: Boolean(props.guest) || mobile || preferences.voice.pttSendOnRelease !== false,
    onSend: text => {
      if (props.guest) { void props.guest.voice(text); return; }
      if (props.chatBusy || chat.sendingPrompt) { fillPrompt(text); return; }
      // Held words are answered out loud (09-28): the reply's spoken summary is read in the call's voice.
      if (connectedRuntimeUrl) voiceReply.expect(connectedRuntimeUrl, props.state.turns.map(turn => turn.id));
      void chat.sendQuickPrompt(text, undefined, { origin: "ptt" });
    },
    onFill: fillPrompt,
    onKeyPress: props.guest ? undefined : openChat,
    chatSessionId: props.chatSessionId });
  // 手机版 方案 B (owner 2026-11-01): the bar's 按住说话 key and its typing send from the page on screen, without opening
  // the AI 对话 page; the 回执条 shows what came back (the AI 指令栏's result). On the desktop the same line comes from
  // the AI 指令栏 and its result shows beside the AI 球 (which is on every desktop page for it).
  const aiBlocked = !connectedRuntimeUrl ? "连接后可以问 AI" : props.agentAccount && !props.agentAccount.authenticated ? "模型尚未就绪，请在设置里选择模型"
    : accessRef.current?.reason("ai-chat") || null;
  const aiCommand = useAiCommand({ baseUrl: connectedRuntimeUrl, turns: props.state.turns, busy: Boolean(props.chatBusy || chat.sendingPrompt),
    blockedReason: aiBlocked, available: !isGuest,
    onSend: (text, spoken) => {
      if (spoken && connectedRuntimeUrl) voiceReply.expect(connectedRuntimeUrl, props.state.turns.map(turn => turn.id));
      return chat.sendQuickPrompt(text, mobile && mobileTab === "strategy" ? mobileStrategyFocus : undefined, spoken ? { origin: "ptt" } : undefined);
    } });
  useEffect(() => { voiceReply.offer(props.state.turns); }, [props.state.turns]);
  useEffect(() => { if (ptt.status.state === "recording" || call.inCall) voiceReply.stop(); }, [ptt.status.state, call.inCall]);
  // Another chat is shown: its older answers must not be read out as the awaited spoken reply.
  useEffect(() => { voiceReply.stop(); }, [props.chatSessionId]);
  useEffect(() => () => voiceReply.stop(), []);
  // The flow's 简讯 entries read the same cached briefs query the floating orb owns.
  const flowBriefs = useBriefs(connectedRuntimeUrl, appWorkbench.config.showMarketBriefs, Boolean(props.replayActive), false);
  // 手机版 AI 动态: the same briefs as the conversation's 快讯/异动 cards, and why a card's button did nothing.
  const mobileBriefs = useMemo(() => buildFlowItems({ briefs: flowBriefs.items, symbol: quote?.symbol, symbolName: quoteEntry?.instrumentName || quote?.name }),
    [flowBriefs.items, quote?.symbol, quoteEntry?.instrumentName, quote?.name]);
  const [mobileNotice, setMobileNotice] = useState("");
  // 我的 → AI 简报推送 shows the subscribed count (the page itself shares this read, so a save there
  // shows here at once); 通知 lists the pushed ones (the first `limit`) as AI 简报 products.
  const briefSubscriptions = useBriefSubscriptions(connectedRuntimeUrl, mobile && !isGuest);
  const subscribedProducts = useMemo(() => {
    const data = briefSubscriptions.data;
    return data ? data.products.slice(0, data.limit ?? undefined) : [];
  }, [briefSubscriptions.data]);
  // Compared case-blind: a contract may come as "SHFE.AU2612" for the subscribed "SHFE.au".
  const quoteProduct = quote?.symbol ? productKey(quote.symbol).toLowerCase() : undefined;
  // 手机版 AI 首页 (新结构 2026-10-06: 通知 merged in) reads one stream: the desk's judgment of the shown
  // product and of every pushed subscribed product, the conversation's cards and the briefs. The
  // shown product's judgment is the subscribed read when it is subscribed (one read per product).
  const subscribedReads = useDeskJudgments(mobile ? connectedRuntimeUrl : undefined, useMemo(() => subscribedProducts.map(item => item.symbol), [subscribedProducts]));
  const subscribedDesks = subscribedReads.items;
  const quoteSubscribed = subscribedProducts.findIndex(item => item.product.toLowerCase() === quoteProduct);
  const quoteDesk = useDeskJudgment(mobile && quoteSubscribed < 0 ? connectedRuntimeUrl : undefined, quote?.symbol);
  const desk = quoteSubscribed < 0 ? quoteDesk : subscribedDesks[quoteSubscribed];
  const notifyItems = useMemo(() => mobile ? buildMobileFeed({ turns: props.state.turns, briefs: mobileBriefs,
    desks: [desk.card, ...subscribedDesks.filter((_, index) => index !== quoteSubscribed).map(item => item.card)] }) : [],
  [mobile, props.state.turns, mobileBriefs, desk.card, subscribedDesks, quoteSubscribed]);
  // The clock the 确认 windows and 今天 are judged by (a pinned card leaves at its expiry).
  const [mobileNow, setMobileNow] = useState(() => Date.now());
  useEffect(() => {
    if (!mobile) return;
    const timer = window.setInterval(() => setMobileNow(Date.now()), 20_000);
    return () => window.clearInterval(timer);
  }, [mobile]);
  const now = mobileNow;
  // Cards waiting for 确认 are pinned; everything else (handled and expired cards in their place) is the timeline.
  const pendingCards = useMemo(() => notifyItems.filter((item): item is Extract<typeof item, { kind: "proposal" }> => isPending(item, now)), [notifyItems, now]);
  const notifyRows = useMemo(() => notifyItems.filter(item => !isPending(item, now)).map(notifyRow), [notifyItems, now]);
  // 下拉刷新 (2026-10-06): each page reads its own sources again; the shown content stays on a failure.
  const reloadQuoteDesk = quoteSubscribed < 0 ? quoteDesk.reload : subscribedReads.reload;
  const refreshFeed = () => Promise.allSettled([quoteDesk.reload(), subscribedReads.reload(), flowBriefs.reload(), briefSubscriptions.reload()]);
  // The timeline's AI 简报 narrowed to one product: a pushed subscribed one or one the rows carry.
  const notifyProductList = useMemo(() => notifyProducts(notifyRows, subscribedProducts, boardProductNames), [notifyRows, subscribedProducts, boardProductNames]);
  const shownNotifyProduct = notifyFilter === "brief" && notifyProduct && notifyProductList.some(item => item.product === notifyProduct) ? notifyProduct : null;
  // Back on AI 首页 the subscriptions are read again (another device may have changed them).
  const reloadBriefSubscriptions = briefSubscriptions.reload;
  useEffect(() => { if (mobile && mobileTab === "ai" && connectedRuntimeUrl && !isGuest) void reloadBriefSubscriptions(); },
    [mobile, mobileTab, connectedRuntimeUrl, isGuest, reloadBriefSubscriptions]);
  // 策略 (2026-10-06): the running instances (also 它正在盯着's) and, while the tab shows, their monitors and the labs.
  const mobileRootUrl = connectedRuntimeUrl ? props.rootBaseUrl ?? connectedRuntimeUrl : undefined;
  const mobileStrategies = useMobileStrategies({ baseUrl: connectedRuntimeUrl, rootUrl: mobileRootUrl,
    enabled: mobile && !isGuest && !strategyBlocked, detailed: mobileTab === "strategy" && !mobileSub,
    chartSymbol: quote?.symbol ?? props.selectedSymbol, chartTimeframe: props.duration, sessionId: props.chatSessionId, backtestBlocked });
  // 手机编辑卡 (2026-10-08): the 策略编辑卡's draft and its `strategyEditor` command on the phone; the page is full screen.
  const mobileEditorEnabled = mobile && !isGuest && !strategyBlocked && Boolean(connectedRuntimeUrl);
  const mobileEditor = useMobileStrategyEditor({ enabled: mobileEditorEnabled, rootUrl: mobileRootUrl,
    baseUrl: connectedRuntimeUrl, chartSymbol: quote?.symbol ?? props.selectedSymbol ?? "", chartTimeframe: props.duration, instruments: props.instruments, strategies: mobileStrategies,
    onShowMine: () => { setMobilePage("strategy"); setRightDock(null); setMobileSub(null); setMobileStrategyTab("mine"); },
    onShowReminders: () => showMyReminders() });
  const mobileEditorOpen = mobile && mobileEditor.editor.open;
  // The page covers whatever full-screen page was open (a command may open it from anywhere).
  useEffect(() => { if (mobileEditorOpen) setMobileSub(null); }, [mobileEditorOpen]);
  // A tab tap / `mobileTab` command: chat opens the 对话面板 (access-checked like every other
  // opening), the other tabs close it; any open full-screen page closes with the move.
  const selectMobileTab = (tab: MobileTab) => {
    if (tab === "chat") { const blocked = accessRef.current?.reason("ai-chat"); if (blocked) throw new Error(blocked); setRightDock("ai"); setMobileSub(null); return; }
    setMobilePage(tab);
    setRightDock(null);
    setMobileSub(null);
  };
  // The 看盘 tab's single chart: a card's 看图表, a link, a running strategy's chart, 到价提醒 (2026-10-09).
  const showMobileChart = () => {
    selectMobileTab("market");
    if (mobileMarket.market.view !== "single") mobileMarket.update({ view: "single" });
  };
  // `mobilePage` / a 我的 row: the phone's own pages, or 设置 at a page / the 使用说明卡 full screen;
  // null closes whichever is open. The same access checks as the desktop panels.
  const openMobilePage = (page: MobilePage | null) => {
    const capability = page === "order" ? "order-proposal" : page === "positions" ? "account" : page === "review" ? "review-summary"
      : page === "help" ? "help" : page === "push" ? "preferences" : undefined;
    const blocked = capability ? accessRef.current?.reason(capability) : undefined;
    if (blocked) throw new Error(blocked);
    if (page === "order" && !market) throw new Error("行情尚未就绪");
    if (page === "lab" && strategyBlocked) throw new Error(strategyBlocked);
    if (page === "lab") setMobileLabId(null);
    if (page === "reminders" && !reminders.available) throw new Error("提醒记录要登录并连上服务后才能看");
    setMobileSub(page === "order" || page === "positions" || page === "review" || page === "briefs" || page === "install" || page === "push" || page === "lab" || page === "reminders" ? page : null);
    const settings = page === "accounts" ? "account" : page === "memory" ? "memory" : page === "appearance" ? "ui" : undefined;
    if (settings) setSettingsPage(settings);
    setSettingsOpen(Boolean(settings));
    const help = uiSurface("dialog:help");
    if (page === "help") openAppHelp("mobile");
    else if ((help?.state as { open?: boolean } | undefined)?.open) help?.apply?.({ open: false });
  };
  // 回测结果页 of one comparison (策略 → 最近的回测, `mobileLab`).
  const openMobileLab = (labId: string) => {
    if (strategyBlocked) throw new Error(strategyBlocked);
    setMobileLabId(labId);
    setMobileSub("lab");
    setSettingsOpen(false);
  };
  const focusTimeline = () => setTimelineFocus(value => value + 1);
  // 提醒 (2026-10-25): the right rail's 提醒 panel on the computer, the 提醒 page on the phone, each on one of three tabs.
  // 提醒记录 (#932's 提醒中心) is open when that tab shows: the `reminderCenter` command, the bell and a tab click reach
  // the same state; the snapshot reports it with the counts.
  const remindersShown = mobile ? mobileSub === "reminders" : rightDock === "reminders";
  const reminderShown = remindersShown && remindersTab === "history";
  const openReminders = (tab: RemindersTab) => {
    setRemindersTab(tab);
    if (mobile) openMobilePage("reminders"); else setRightDock("reminders");
  };
  const showReminderCenter = (open: boolean) => {
    if (open) openReminders("history");
    else if (reminderShown) { if (mobile) setMobileSub(null); else setRightDock(null); }
  };
  const reminderApply = useRef<(value: unknown) => void>(() => undefined);
  reminderApply.current = value => {
    const change = (value ?? {}) as { open?: boolean; filter?: ReminderFilter; more?: boolean };
    // 继续加载当天提醒 (#932 R5): the same read the button makes; nothing left to read is refused, not faked.
    if (change.more && !reminders.hasMoreToday) throw new Error("当天的提醒已经全部读出，没有更多可加载");
    if (change.filter) setReminderFilter(change.filter);
    showReminderCenter(change.open ?? true);
    if (change.more) {
      // The command's outcome rides the snapshot (Codex r4308006076): asked = this command's number, done once its read
      // finished, error = why nothing was loaded — the receipt is applied only on a load that landed.
      const asked = moreAsk.current + 1;
      moreAsk.current = asked;
      setMoreCommand(current => ({ ...current, asked }));
      void reminders.loadMoreToday().then(reason => setMoreCommand(current => current.asked === asked ? { asked, done: asked, error: reason } : current));
    }
  };
  const moreAsk = useRef(0);
  const [moreCommand, setMoreCommand] = useState({ asked: 0, done: 0, error: "" });
  const reminderAvailable = reminders.available || (!mobile && alerts.available);
  // moreToday only past 500 a day, more* only after a `more` command (#932 R5).
  useUiSurface("reminderCenter", { state: { open: reminderShown, filter: reminderFilter, unread: reminders.unread,
    today: reminders.today.length, ...(reminders.hasMoreToday ? { moreToday: true } : {}),
    ...(moreCommand.asked ? { moreAsked: moreCommand.asked, moreDone: moreCommand.done, moreError: moreCommand.error } : {}),
    earlier: reminders.earlierShown }, apply: value => reminderApply.current(value) },
  [reminderShown, reminderFilter, reminders.unread, reminders.today.length, reminders.hasMoreToday, moreCommand, reminders.earlierShown], reminderAvailable);
  // 安装到桌面引导 (2026-10-06): the browser this page runs in (Android's install offer and the
  // display mode followed live); the guide's own timing is set up below, after the order state.
  const installDetection = useInstallDetection();
  // 锁屏提醒 (2026-10-06): this phone's lock-screen push (the 我的 row, the push page and the snapshot).
  const push = usePush(connectedRuntimeUrl, mobile && !isGuest && CLIENT_BUILD.platform === "web");
  const pushSupport = push.fcm ? "unsupported" as const : push.support.support;
  // 多 AI 辩论: the 调研面板's 辩论 tab (its own rail page until 2026-10-25); a 快讯 row's 研讨, 今日判断's 让 AI 们辩一轮
  // and the researchDebate command open it, and closing it goes back to the tab it came from. The phone's lives in its
  // 调研 tab (MobileResearch).
  const debateBack = useRef<NewsTab>("desk");
  const openDebatePage = useCallback(() => { setView("trade"); setRightDock("news"); setNewsTab("debate"); }, [setView]);
  const debate = useDebateHost({ baseUrl: connectedRuntimeUrl, symbol: quote?.symbol, enabled: !isGuest && !props.replayActive && !mobile,
    onAsk: text => { if (!access.require("ai-chat")) return; setRightDock("ai"); fillPrompt(text); },
    view: [rightDock === "news" && newsTab === "debate", open => { if (open) openDebatePage(); else setNewsTab(tab => tab === "debate" ? debateBack.current : tab); }] });
  debateBack.current = debate.origin === "flash" ? "flash" : "desk";
  const uiControl = useWorkspaceUiControl({ workspace, connectedRuntimeUrl, market, focus, selectedPrice, setSelectedPrice: choosePrice, view, setView, chartView, selectChartView, selectDuration, rightDock, setRightDock, openDebatePage,
    reminders: { shown: remindersShown, tab: remindersTab, open: openReminders, openSettings: () => openReminderSettingsRef.current() }, settingsOpen: settingsOpen && !props.guest?.loginOpen, setSettingsOpen,
    positionsBarAutoHidden, mobile: mobile ? { tab: mobileTab, select: selectMobileTab, page: mobileSub, openPage: openMobilePage,
      strategyTab: mobileStrategyTab, setStrategyTab: setMobileStrategyTab,
      focusTimeline, lab: mobileLab, openLab: openMobileLab,
      notifyFilter, setNotifyFilter, unread: reminders.unread,
      notifyProduct: shownNotifyProduct, notifyProducts: notifyProductList.map(item => item.product), setNotifyProduct,
      installEnvironment: installDetection.environment, push: { support: pushSupport, subscribed: Boolean(push.thisDevice) },
      applyMarket: mobileMarket.apply,
      marketState: { view: mobileMarket.market.view, subPane: mobileMarket.market.subPane, subPanes: mobileMarket.sheetOpen } } : null,
    tradingAccounts,
    settingsPage, setSettingsPage, layout, strategyUi, strategyNavigation, appWorkbench, preferences: prefs,
    chartIndicators, updateChartIndicators: updateChartPreferences,
    surfaces: {
      pinSymbol, unpinSymbol: unpinContractTab, positionsTab, setPositionsTab, fillsSource, setFillsSource, strategyTab, setStrategyTab,
      ordersHistory, setOrdersHistory,
      boardExchange, setBoardExchange, boardSort, setBoardSort, boardWatchlistCount: watchlist.length, contractProduct, setContractProduct,
      chatSessionId: props.chatSessionId, chatHistory: chat.chatHistory, selectChat: chat.selectChat, chatBlocked: chat.chatBlocked,
      quickSkipConfirm, setQuickSkipConfirm,
      currentPositions: props.account?.positions ?? [], selectedPosition,
      selectPosition: position => { if (position) closePosition(position); else { setSelectedPositionKey(undefined); setCloseRequest(undefined); } },
      dialogs: {
        "contract-drawer": { open: contractOpen, set: open => { setContractOpen(open); if (!open) setContractPinMode(false); }, available: !props.replayActive },
        "resume-execution": { open: resumeOpen, set: setResumeOpen, available: Boolean(props.onResumeAiExecution) },
        "emergency-stop": { open: emergencyOpen, set: setEmergencyOpen, available: !props.replayActive },
      },
    } });
  // Conversation cards' UI buttons go through the same apply a UI command uses (stable for AiDock's memo).
  const uiApply = useRef(uiControl.apply);
  uiApply.current = uiControl.apply;
  const applyCardUi = useCallback((changes: Record<string, unknown>) => uiApply.current(changes as Parameters<typeof uiControl.apply>[0]), []);
  // 锁屏提醒的去处 (2026-10-06): a tapped notification opens the app at `?open=…` (read once, then
  // removed from the address bar) or, with a window already open, the service worker hands it over.
  const deepLinkRef = useRef<(link: DeepLink) => void>(() => undefined);
  deepLinkRef.current = link => {
    try {
      if (link.kind === "card" || link.kind === "chat") {
        if (mobile) { setMobileSub(null); setSettingsOpen(false); }
        setRightDock("ai");
        if (link.kind === "card") locateCard(link.id, { scope: mobile ? ".fdm-chat" : "", tries: 40 });
      } else if (link.kind === "positions") {
        if (mobile) openMobilePage("positions"); else setRightDock("positions");
      } else if (link.kind === "review") {
        // A 外盘复盘 push names its USD sub-account: open on that account, as the reminder center does (#932).
        const day = link.day;
        navigateOnAccount(link.accountId ?? null, () => applyCardUi({ reviewDay: day }));
      } else if (link.kind === "research") {
        if (mobile) selectMobileTab("research"); else applyCardUi({ news: { tab: link.page ?? "desk" } });
      } else {
        applyCardUi({ symbol: chartSymbolOf(link.symbol) });  // a foreign month (CME.GCEZ26) opens its 外盘主连 chart
        if (mobile) showMobileChart(); else setView("trade");
      }
    } catch { /* The surface is not available to this account: the app simply stays where it opened. */ }
  };
  // 提醒中心的一行 (#932): read on every device, then where it points — the conversation's card (its
  // session first), 委托, 复盘 on that day, 调研 or 新闻; the phone's page closes behind it (the computer's panel gives
  // way to the panel the row opens, or stays).
  const openReminder = (item: ReminderItem) => {
    reminders.markRead([item.id]);
    const target = item.target;
    if (mobile) setMobileSub(null);
    if (!target) return;
    // An account's reminder (成交 / 撤单 / 挂单未成交 / 复盘) opens on its own account (#932 P2): the target opens only
    // once runtime.status shows that account; a refused or unfinished switch says so and stays put (#932 R3).
    const account = target.type === "orders" || target.type === "review" ? item.accountId : null;
    navigateOnAccount(account, () => openReminderTarget(target));
  };
  const openReminderTarget = (target: NonNullable<ReminderItem["target"]>) => {
    try {
      if (target.type === "chat") {
        if (!accessRef.current?.require("ai-chat")) return;
        if (target.sessionId && target.sessionId !== props.chatSessionId) applyCardUi({ chatSession: { sessionId: target.sessionId } });
        if (mobile) setSettingsOpen(false);
        setRightDock("ai");
        if (target.cardId) locateCard(target.cardId, { scope: mobile ? ".fdm-chat" : "", tries: 40 });
      } else if (target.type === "orders") {
        if (mobile) openMobilePage("positions"); else setRightDock("positions");
        setPositionsTab("orders");
      } else if (target.type === "review") {
        if (target.tradingDay) applyCardUi({ reviewDay: target.tradingDay });
        else if (mobile) openMobilePage("review"); else applyCardUi({ view: "review" });
      } else if (target.type === "research" || target.type === "news") {
        if (target.symbol && !sameContract(target.symbol, props.selectedSymbol)) applyCardUi({ symbol: target.symbol });
        if (mobile) selectMobileTab("research"); else applyCardUi({ news: { tab: target.type === "news" ? "flash" : target.page ?? "desk" } });
      }
    } catch (error) { setAccessNotice(error instanceof Error ? error.message : "这个界面现在打不开"); }
  };
  // 提醒设置 (was 设置 → 提醒): the 提醒 panel's / page's 通用提醒 tab since 2026-10-25.
  const openReminderSettings = () => {
    if (!(accessRef.current?.require("preferences") ?? true)) return;
    setSettingsOpen(false);
    openReminders("general");
  };
  openReminderSettingsRef.current = openReminderSettings;
  const reminderCenter: ReminderCenterProps = { reminders, alerts: alerts.available ? alerts : undefined,
    contract: quote ? { symbol: quote.symbol, name: quote.name, priceTick: quote.priceTick, lastPrice: quote.lastPrice } : undefined,
    filter: reminderFilter, onFilter: setReminderFilter, onOpen: openReminder, onBreakeven: props.proposalActions?.breakeven,
    onSwitchContract: symbol => { selectContract(symbol); if (mobile) { setMobileSub(null); showMobileChart(); } },
    accountName: reminderAccountName };
  const deepLinkReady = Boolean(connectedRuntimeUrl) && !isGuest;
  useEffect(() => {
    if (!deepLinkReady) return;
    const link = takeDeepLink();
    if (link) deepLinkRef.current(link);
    return onDeepLinkMessage(next => deepLinkRef.current(next));
  }, [deepLinkReady]);
  const submitManualOrder = async (request: ManualSimulationRequest) => {
    const outcome = await props.onManualSubmit(request);
    if (!outcome?.accepted) return outcome;
    setTicketDraft(undefined);
    setCloseRequest(undefined);
    recordAction({
      action: "manual_order_submitted",
      side: request.side,
      // The lots actually placed (a batch may carry fewer than asked when the planner skipped a bucket).
      volume: outcome.orders.reduce((sum, order) => sum + order.request.volume, 0),
      price: request.limitPrice,
      view: "trade"
    });
    return outcome;
  };
  const handleOrderAction = (
    order: SimulationOrder,
    action: "continue" | "abandon" | "query" | "cancel"
  ) => {
    recordAction({
      action: "order_action",
      symbol: order.request.symbol,
      orderAction: action,
      clientOrderId: order.clientOrderId,
      view
    });
    return props.onOrderAction(order, action);
  };
  const quick = useQuickOrders({ sessionId: props.chatSessionId, accountId: accountsView.currentId, accountGeneration: props.state.runtime?.accountGeneration, contextKey: `${props.workspaceScope}:${props.preferencesScope}:${props.connection}:${props.chatSessionId}`, quote, runtimeBaseUrl: connectedRuntimeUrl, tradeBusy: props.tradeBusy, simulationEnabled: props.simulationEnabled, submitManualOrder, skipConfirm: quickSkipConfirm, cancelOrder: order => handleOrderAction(order, "cancel"), submitTargetPosition: props.onTargetPosition });
  const quickActions = useTicketQuickActions({ quote, positions: currentPositions, orders: props.simulationOrders, busy: props.tradeBusy, enabled: props.simulationEnabled, quick });
  // 网页新版本 (2026-10-05): the page reloads itself onto a newer deployed build only while nothing
  // is in progress — holding to talk, a voice call, an unsent chat line, an order page or a
  // confirmation open, settings open (unsaved edits) or a replay. Read at the moment it would reload.
  const webUpdateBusy = useRef(false);
  webUpdateBusy.current = Boolean(chat.prompt.trim()) || mobileSub === "order" || (rightDock === "order" && view === "trade") || Boolean(quick.pending)
    || settingsOpen || Boolean(props.replayActive)
    // A card's 确认 in flight (no dialog stands for it since 2026-10-19): a reload must not cut the confirmation.
    || Boolean(props.proposalActions.busyProposalId);
  // 策略编辑卡 (2026-10-08) is no dialog: an open card or a save in flight counts (a reload drops its draft).
  useEffect(() => registerWebUpdateBusy(() => webUpdateBusy.current || pushToTalkStore.snapshot().state !== "idle" || voiceCallStore.inCall()
    || editorBusy() || BUSY_DIALOGS.some(name => (uiSurfaceStates()[`dialog:${name}`] as { open?: boolean } | undefined)?.open === true)), []);
  // The guide opens by itself from this browser's second open, or after its first confirmed order /
  // AI reply here — and waits while a hold, a full-screen page or an unconfirmed order card is open.
  const mountedAt = useRef(Date.now());
  const [orderedHere, setOrderedHere] = useState(false);
  const turns = props.state.turns;
  const installSignals = useMemo(() => {
    const recent = (time: string) => Date.parse(time) > mountedAt.current;
    return {
      engaged: turns.some(turn => (turn.role === "assistant" && turn.completed === true && recent(turn.timestamp))
        || turn.cards.some(card => card.type === "trade" && card.payload.status === "candidate_recorded" && recent(card.payload.updatedAt))),
      pendingCard: turns.some(turn => Date.now() - Date.parse(turn.timestamp) < 15 * 60_000
        && turn.cards.some(card => (card.type === "trade" || card.type === "close-batch" || card.type === "spread") && card.payload.status === "approval_required")),
    };
  }, [turns]);
  useInstallAutoOpen({ enabled: mobile && !("__TAURI_INTERNALS__" in window), detection: installDetection, signedIn: !isGuest,
    engaged: orderedHere || installSignals.engaged, holdingToTalk: ptt.status.state !== "idle",
    pageOpen: Boolean(mobileSub && mobileSub !== "install") || settingsOpen,
    pendingOrder: installSignals.pendingCard || Boolean(quick.pending), installPageOpen: mobileSub === "install", open: () => setMobileSub("install") });
  // Account-wide round trips for the bar's and the 持仓面板's 成交; polled only while one of them shows them.
  const fillsShown = !props.replayActive
    && ((positionsBar === "expanded" && positionsTab === "fills") || rightDock === "positions" || (mobile && mobileSub === "positions"));
  const tradeReview = useTradeReview({ baseUrl: connectedRuntimeUrl, accountId: reviewAccountId, enabled: fillsShown });
  const foreignTradeReview = useTradeReview({ baseUrl: connectedRuntimeUrl, accountId: foreignReviewAccountId ?? null,
    enabled: fillsShown && Boolean(foreignReviewAccountId) });
  // 对象级 AI (2026-09-25): an object's "AI ▾" item sends its short question as the user's turn with
  // the object as `focus`; the conversation opens to show the answer. Items of an unavailable skill
  // are greyed with the registry's reason line (strategy signals, backtests).
  const signalsBlocked = skillBlockedLine("strategy-signals", skills, planFeatures, "策略信号解读");
  const askAi = (text: string, focus: AiFocus) => { if (!access.require("ai-chat")) return; setRightDock("ai"); void chat.sendQuickPrompt(text, focus); };
  // Ladder clicks and selectedPrice commands share the same fill-only entry.
  const pickLadderPrice: LadderPick = price => choosePrice(price);
  // A position row's 保护 form (持仓面板 and the 下单面板's 本合约持仓).
  const createPositionStop = async (position: AccountPosition, request: PositionStopRequest) => props.onCreatePositionStop?.({
    symbol: position.symbol, positionDirection: position.direction,
    positionBucket: request.positionBucket, volume: request.volume,
    triggerPrice: request.triggerPrice, takeProfitPrice: request.takeProfitPrice,
    trailing: request.trailing, breakevenAtR: request.breakevenAtR
  }) ?? false;
  const cancelPositionStop = async (stopId: string) => props.onCancelPositionStop?.(stopId) ?? false;
  // 暂停 on the 对话面板's strategy banner: the same monitor pause the strategy card runs.
  const pauseStrategy = useCallback(async (strategyId: string) => {
    if (!connectedRuntimeUrl) return;
    try { await requestJson(`${connectedRuntimeUrl}/api/strategies/${strategyId}/monitor/pause`, { method: "POST", body: "{}" }); }
    catch { /* The strategy card and the next activity read report the real state. */ }
  }, [connectedRuntimeUrl]);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target?.closest("dialog[open]")) return;
      const editing = target?.tagName === "INPUT" || target?.tagName === "TEXTAREA" || target?.isContentEditable;
      if (event.key === "Escape") {
        setContractOpen(false);
        setSettingsOpen(false);
        return;
      }
      if (editing) return;
      const action = shortcutAction(event);
      if (!action || action.startsWith("quick")) return;
      if (!accessRef.current?.require(action === "ai" ? "ai-chat" : action === "positions" ? "account" : "order-proposal")) { event.preventDefault(); return; }
      if (action === "ai") {
        event.preventDefault();
        setRightDock(current => current === "ai" ? null : "ai");
      } else if (action === "buy" || action === "sell") {
        event.preventDefault();
        setView("trade");
        setRightDock("order");
        applyOrderDraft({
          id: Date.now(),
          side: action === "buy" ? "buy_open" : "sell_open",
          volume: 1,
          limitPrice: selectedPrice,
          source: "context-menu"
        });
      } else if (action === "positions") {
        event.preventDefault();
        setRightDock(current => current === "positions" ? null : "positions");
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [selectedPrice]);
  const dataLive = market?.dataStatus === "normal" && !market.isCached;
  const closed = marketClosedNow(market);
  const sessionLabel = marketSessionDisplay(market, props.replayActive).label;
  const decimals = decimalsForTick(quote?.priceTick);
  // 交割月提醒 (2026-10-02): the ticket's open mode reads the same catalog entry the head badge does.
  const ticketDeliveryNotice = quote && props.instruments?.items.find(
    item => item.symbol.toUpperCase() === quote.symbol.toUpperCase())?.deliveryNotice;
  const aiAuthorization = props.aiExecution?.authorization;
  const [briefStatus, setBriefStatus] = useState("等待检索");
  const [briefSaving, setBriefSaving] = useState(false);
  const [briefError, setBriefError] = useState("");
  // 通用提醒 (was 设置 → 提醒): the 提醒 panel's / page's tab body.
  // 条件提醒 (第 6 批): 新建 / 编辑 open the strategy panel's editor card in 提醒模式 (the same `strategyEditor` command the
  // AI sends); back, 放弃 and 开始提醒 there bring 我的提醒 back. The phone opens its full-screen editor page in 提醒模式.
  const openReminderEditor = (change: Record<string, unknown>) => { if (!mobile) setRightDock("strategy"); applyUiSurface("strategyEditor", change); };
  const reminderEditor = !mobile || mobileEditorEnabled;
  const showMyReminders = () => { setRemindersTab("mine"); if (mobile) openMobilePage("reminders"); else setRightDock("reminders"); };
  const myReminders: MyRemindersProps = { baseUrl: connectedRuntimeUrl, contract: quote?.symbol, chartTimeframe: market?.durationSeconds ?? props.duration,
    onNew: reminderEditor ? () => openReminderEditor({ new: true, reminder: true }) : undefined, onEdit: reminderEditor ? id => openReminderEditor({ edit: id }) : undefined };
  const reminderSettings = <ReminderSettings workspace={workspace} connectedRuntimeUrl={connectedRuntimeUrl} preferences={prefs} appWorkbench={appWorkbench}
    brief={{ status: briefStatus, saving: briefSaving, error: briefError, setSaving: setBriefSaving, setError: setBriefError }}
    skills={skills} ownerStatus={ownerPlan.status} shown={remindersShown && remindersTab === "general"} />;
  useEffect(() => { setResumeOpen(false); setEmergencyOpen(false); }, [props.runtimeBaseUrl, props.workspaceScope, props.chatSessionId, props.connection]);
  const executionStopped = !props.replayActive && props.aiExecution?.controls?.stopStatus;
  const recoveryButton = <button type="button" className="fdw-ai-safety-stop" disabled={props.aiExecutionBusy || props.connection !== "connected" || !props.onResumeAiExecution} onClick={() => setResumeOpen(true)}>{executionStopped === "EMERGENCY_STOP" ? "AI 已急停 · 恢复" : executionStopped === "HALT_OPENS" ? "已只停新开仓 · 恢复" : "AI 已停止 · 恢复"}</button>;
  const aiExecutionActive = aiAuthorization?.status === "ACTIVE";
  const armedStopCount = props.aiExecution?.stops.filter((item) => isProtectionActive(item.status)).length ?? 0;
  const dockWidth = panelWidth(rightDock);
  const workspaceStyle = {
    "--fdw-right-dock": `${dockWidth}px`,
    "--fdw-dock-max": rightDock && WIDE_PANELS.has(rightDock) ? "1600px" : `${maxSharedDockWidth()}px`,
    // 策略编辑卡第二版: the chart keeps a quarter of the window (300px at least) while the strategy panel edits.
    ...(editorPanel(rightDock) ? { "--fdw-chart-min": `${EDITOR_CHART_MIN}px` } : {})
  } as CSSProperties;
  // The floating AI orb rides inside the chart area, so it only ever covers the chart —
  // never the 复盘 list or a side panel. Without a quote (no chart) it floats over the
  // whole market area; its key changes with the symbol, so the move remounts nothing extra.
  const aiOrb = !props.guest ? <AiOrb key={String(props.runtimeBaseUrl) + quote?.symbol} baseUrl={connectedRuntimeUrl} symbol={quote?.symbol} name={quote?.name} replay={Boolean(props.replayActive)} activity={appWorkbench.activity} error={appWorkbench.error} config={appWorkbench.config} onConfigChange={appWorkbench.updateConfig} onBriefStatusChange={setBriefStatus} onOpenChat={openChat} sessionId={props.chatSessionId} voiceAvailable={voiceAvailable} avatar={preferences.ui?.aiAvatar ?? "kline"}
    beside={aiCommand.asked ? <AiCommandBubble key={`${aiCommand.asked.after}:${aiCommand.asked.text}`} command={aiCommand} failed={chat.promptError || undefined} onOpenChat={openChat} /> : undefined} /> : null;

  // Shared by both layouts: the confirmations, help and tour overlays before the body, the drawers,
  // settings and notices after it, and the 对话面板 (the phone's AI 对话 page passes `mobileChat`).
  const dialogs = <>
    <ResumeExecutionDialog action="stop" open={emergencyOpen} busy={props.aiExecutionBusy} onClose={() => setEmergencyOpen(false)} onConfirm={() => { setEmergencyOpen(false); props.onStopAiExecution(true); }}/>
    <ResumeExecutionDialog open={resumeOpen} busy={props.aiExecutionBusy} onClose={() => setResumeOpen(false)} onConfirm={() => { void props.onResumeAiExecution?.().then(ok => { if (ok) setResumeOpen(false); }); }}
      pausedStrategies={(props.pausedStrategyIds ?? []).map(id => ({ id, name: strategyUi?.items.find(item => item.strategyId === id)?.name }))}
      onOpenStrategy={props.strategyPanel ? (id) => {
        setResumeOpen(false);
        setRightDock("strategy");
        // The strategy panel mounts with the dock; hand it the selection once it is there.
        window.setTimeout(() => strategyNavigation.current?.({ selectedStrategyId: id, tab: "library" }), 0);
      } : undefined}/>
    <QuickOrderDialog pending={quick.pending} decimals={decimals} busy={quick.quickBusy} onClose={() => quick.setPending(undefined)} onConfirm={(order) => void quick.submitQuick(order)} />
    {!props.guest && <><AppHelp baseUrl={props.runtimeBaseUrl} isOwner={isOwner} /><HighlightLayer /><TourIndicator /></>}
  </>;
  const trailing = <>
    {contractOpen ? <ContractDrawer workspace={workspace} query={query} onQueryChange={setQuery} onSelect={selectContract} onClose={() => { setContractOpen(false); setContractPinMode(false); }} recentSymbols={recentSymbols}
      board={board.snapshot} product={contractProduct} onProductChange={setContractProduct}
      watchlist={watchlist} onToggleWatchlist={watchlistChange} watchlistStatus={watchlistStatus || undefined} /> : null}

    {settingsOpen && !props.guest?.loginOpen ? <WorkspaceSettings workspace={workspace} page={settingsPage} onPageChange={setSettingsPage} onClose={() => setSettingsOpen(false)}
      model={model} tradingAccounts={tradingAccounts} connectedRuntimeUrl={connectedRuntimeUrl} desktopVersion={desktopVersion}
      agentModelLabel={agentModelLabel} apiSetupRequired={apiSetupRequired} apiProviderName={apiProviderName} preferenceStatus={preferenceStatus}
      chartSettings={chartSettings} setChartSettings={setChartSettings} chartIndicators={chartIndicators} onChartIndicatorsChange={chartIndicatorsChange} chartIndicatorStatus={prefs.status}
      boardColumns={boardColumns} onBoardColumnsChange={boardColumnsChange} boardColumnStatus={prefs.status}
      dockSide={dockSide} setDockSide={setDockSide} resetLayout={resetLayout}
      closed={closed} sessionLabel={sessionLabel} aiExecutionActive={aiExecutionActive} executionStopped={executionStopped}
      recoveryButton={recoveryButton} armedStopCount={armedStopCount} onEmergencyStop={() => setEmergencyOpen(true)}
      onHaltOpens={() => props.onStopAiExecution(false, "opens")}
      quickSkipConfirm={quickSkipConfirm} setQuickSkipConfirm={setQuickSkipConfirm} quickSkipConfirmScoped={Boolean(props.preferencesScope)} initialBalance={initialBalance} setInitialBalance={setInitialBalance} voiceAvailable={voiceAvailable}
      ownerStatus={ownerPlan.status} preferences={prefs} skills={skills} routines={routineActions} /> : null}

    {accessNotice && (!settingsOpen || props.guest?.loginOpen) ? <div className="toast" role="status">{accessNotice}</div> : null}
    {signalNotice ? <p className="fdw-signal-notice" role="status" data-testid="strategy-signal-notice">{signalNotice}</p> : null}
    {!props.guest && !uiControlHealth.registered ? <div className="fdw-global-error" role="status" data-agent-target="ui-control-health">界面命令不可用：登记失败（{uiControlHealth.detail.slice(0, 160) || "/api/ui/pulse 422"}）</div> : null}
  </>;
  const aiDock = (mobileChat?: Parameters<typeof AiDock>[0]["mobile"]) => <AiDock workspace={workspace} chat={chat} focus={focus} appWorkbench={appWorkbench}
      call={call} voiceUnavailableReason={voiceUnavailableReason}
      onClose={() => setRightDock(null)} onPauseStrategy={pauseStrategy}
      briefs={flowBriefs.items}
      quote={quote} decimals={decimals} selectedPrice={selectedPrice}
      currentPositions={currentPositions} decisionTurns={decisionTurns}
      aiExecutionActive={aiExecutionActive} apiSetupRequired={apiSetupRequired} apiConnectionFailed={apiConnectionFailed} apiProviderName={apiProviderName} view={view} sendKey={preferences.chat.sendKey}
      quickSkills={quickSkillChips} onQuickPrompt={text => { void chat.sendQuickPrompt(text); }}
      suggestions={{ label: CHAT_PLACE_LABELS[examplesFrom], items: suggestions }}
      ptt={ptt} avatar={preferences.ui?.aiAvatar ?? "kline"} applyUi={applyCardUi} symbolName={quoteEntry?.instrumentName} mobile={mobileChat} />;

  const blockingMarketState = props.marketError && !market && (props.state.runtime?.dataConnectionStage === "failed" || (props.state.runtime?.marketDataStatus === "normal" && !props.marketLoading))
    ? <div className="fdw-blocking-state" data-contract-condition={props.marketError !== "当前行情暂时不可用" || undefined}><CircleAlert /><strong>{props.marketError}</strong><button type="button" className={props.marketError !== "当前行情暂时不可用" ? "secondary" : ""} onClick={props.onRefreshMarket}>重新读取</button></div> : null;

  if (mobile) {
    const currentAccount = tradingAccounts.directory?.accounts.find(entry => entry.id === (tradingAccounts.currentId ?? tradingAccounts.directory?.current));
    // 外盘仓合并显示 (2026-10-21): a foreign chart's ticket orders on the USD sub-account — its name and its dollars.
    const accountLabel = currentAccount?.displayName ?? props.account?.account.accountName ?? "交易账户";
    const foreignTicket = isForeignSymbol(quote?.symbol) ? props.account?.foreign : undefined;
    const ticketAccountLabel = foreignTicket ? foreignTicket.account.accountName || "本站模拟 · 美元" : accountLabel;
    const contractName = quoteEntry?.instrumentName || quote?.name || quote?.symbol || "";
    // A 我的 row or 下单 tapped by the user: a refusal (tier, no quote yet) shows as the toast.
    const tapPage = (page: MobilePage) => { try { openMobilePage(page); } catch (error) { setAccessNotice(error instanceof Error ? error.message : "这个界面现在打不开"); } };
    // 方案 B: a card's 问 AI stays on the page; the 回执条 shows the answer (the chat while the AI is still answering).
    const askMobile = (text: string) => {
      if (!access.require("ai-chat")) return;
      setMobileNotice(""); setMobileSub(null);
      if (!aiCommand.send(text)) { setRightDock("ai"); void chat.sendQuickPrompt(text); }
    };
    const researchProduct = quoteEntry ? quoteEntry.instrumentName.replace(/\d+$/, "") || quoteEntry.productId.toUpperCase() : undefined;
    const marketView = mobileMarket.market.view;
    const mobileContext = mobileSub === "reminders" ? "提醒" : mobileTab === "research" ? `调研${researchProduct ? ` · ${researchProduct}` : ""}`
      : mobileTab === "market" ? marketView === "quotes" ? "看盘 · 报价" : marketView === "multi" ? "看盘 · 多图"
        : `看盘 · ${contractName} ${chartView === "kline" ? periods.find(item => item.value === props.duration)?.label ?? "" : "分时"}`.trim()
      : mobileTab === "strategy" ? mobileStrategyFocus ? `策略 · ${mobileStrategyFocus.label}` : "策略"
      : mobileTab === "chat" ? "对话" : mobileTab === "me" ? "我的" : "今天";
    // A card's buttons on the phone: 问 AI opens the chat and sends; 打开调研 / 看图表 / 打开持仓 /
    // 打开委托 / 打开下单台 land on their tab or page; anything else goes through the UI-command
    // path (a desktop-only panel says so).
    const mobileCardHandlers = {
      askAi: askMobile,
      // 保本提醒's 移到成本 / 不用 on the phone: the same business route as on the computer.
      breakeven: props.proposalActions?.breakeven,
      applyUi: (changes: Record<string, unknown>) => {
        setMobileNotice("");
        try {
          if ("news" in changes) { selectMobileTab("research"); return; }
          if ("view" in changes && Object.keys(changes).every(key => key === "view" || key === "symbol")) {
            if (typeof changes.symbol === "string") applyCardUi({ symbol: changes.symbol });
            showMobileChart();
            return;
          }
          if (changes.panel === "positions" || changes.panel === "order") { openMobilePage(changes.panel); return; }
          applyCardUi(changes);
        } catch (error) { setMobileNotice(error instanceof Error ? error.message : "这个界面现在打不开"); }
      },
    };
    // A timeline row → where the item lives: a fill opens 持仓与委托 at 成交, a desk judgment 调研 of its
    // product, the rest (the conversation's 判断, 简报, 确认卡, 回测) the conversation, scrolled to that card.
    const openNotify = (row: NotifyRow) => {
      if (row.item.kind === "execution") { tapPage("positions"); setPositionsTab("fills"); return; }
      if (row.item.kind === "judgment" && row.id.startsWith("desk:")) {
        const symbol = row.item.card.symbol;
        if (symbol && productKey(symbol).toLowerCase() !== quoteProduct) selectContract(symbol);
        selectMobileTab("research");
        return;
      }
      if (!access.require("ai-chat")) return;
      setMobileSub(null);
      setRightDock("ai");
      locateCard(row.id);
    };
    // The bell and 今天提醒: the 提醒 page (2026-10-15).
    const showReminders = () => tapPage("reminders");
    const briefsBlocked = skillBlockedLine("market-briefs", skills, planFeatures, "行情简讯") ?? undefined;
    const toggleBriefs = (value: boolean) => { if (access.require("market-briefs")) void appWorkbench.updateConfig({ showMarketBriefs: value }).catch(() => setAccessNotice("简讯设置未保存，请重试")); };
    // AI 首页's 今天概况, 今日判断 and 它正在盯着 — all from what the window already reads.
    const positions = props.account?.positions ?? [];
    // The product's name (螺纹钢2610 → 螺纹钢); a contract the catalog does not list keeps its code (ag2612).
    const shortName = (symbol: string, name?: string) => {
      const known = name || props.instruments?.items.find(item => item.symbol.toUpperCase() === symbol.toUpperCase())?.instrumentName;
      return known ? known.replace(/\d+$/, "") || known : symbol.split(".").pop() ?? symbol;
    };
    const heldShown = Boolean(quote) && positions.some(position => sameContract(position.symbol, quote!.symbol));
    const closeCheck = heldShown ? closeCheckTime(quote?.tradingSchedule, now) : null;
    const watch: WatchInfo = {
      positions: positions.map(position => `${shortName(position.symbol)}${position.direction === "long" ? "多单" : "空单"} ${position.volume} 手`),
      alerts: alerts.active.map(alert => `${shortName(alert.symbol, alert.name)} ${alert.price}`),
      strategies: mobileStrategies.runs.filter(run => run.status === "monitoring").map(run => `${run.name} · ${signalActionLabel(run.signalAction)}`),
      movement: preferences.notify.movement === true,
      next: [positions.length && preferences.notify.marketCloseReminder ? closeCheck ? `${closeCheck} 收盘前持仓检查` : "收盘前 5 分钟检查持仓" : "",
        preferences.notify.dailyReview !== false ? "15:15 今日复盘" : "",
        // 外盘仓（美元子账户）的复盘在 CME 收盘后出（北京清晨），不是 15:15。
        preferences.notify.dailyReview !== false && props.account?.foreign?.positions.length ? "CME 收盘后 外盘复盘" : ""].filter(Boolean),
      closed: marketClosedNow(market),
    };
    // 读取中 per product until its desk read returns (the shown product's own read when not subscribed).
    // Until the subscriptions are read the chips are the products the timeline carries, whose desk
    // reads have not even started: every one is 读取中, not 暂无 (a failed read falls back as before).
    const subscriptionsReading = mobile && !isGuest && Boolean(connectedRuntimeUrl) && !briefSubscriptions.data && !briefSubscriptions.failed;
    const readingProducts = new Set(subscriptionsReading ? notifyProductList.map(item => item.product.toLowerCase())
      : subscribedProducts.filter((_, index) => subscribedDesks[index]?.loading).map(item => item.product.toLowerCase()));
    if (quoteSubscribed < 0 && quoteProduct && quoteDesk.loading) readingProducts.add(quoteProduct);
    const chips = judgmentChips(notifyProductList, notifyItems, { loading: readingProducts, now });
    const openJudgment = (chip: (typeof chips)[number]) => {
      const symbol = chip.symbol ?? mainContract(chip.product, props.instruments?.items ?? [])?.symbol;
      if (symbol && productKey(symbol).toLowerCase() !== quoteProduct) selectContract(symbol);
      selectMobileTab("research");
    };
    const pinnedActions = { ...proposalActions, askAi: askMobile, applyUi: mobileCardHandlers.applyUi };
    // A visitor reads 看盘 and 调研; AI, 策略 and 我的 are one 登录后使用 page each, whose 登录 opens the
    // login dialog — as do 下单, 设提醒 and 问 AI (the access check opens it for a visitor).
    const signIn = (title: string, text: string) => <MobileSignIn title={title} text={text} onLogin={props.onOpenCloudLogin} />;
    const overlay = mobileEditorOpen ? (
      <MobileStrategyEditor key={mobileEditor.editor.draftId} state={mobileEditor.editor} chartTimeframe={props.duration} instruments={props.instruments}
        ptt={ptt} {...mobileEditor.handlers} />
    ) : mobileSub === "order" && market ? (
      <MobileOrder name={contractName} accountLabel={ticketAccountLabel} available={(foreignTicket?.account ?? props.account?.account)?.available} orders={props.simulationOrders}
        onClose={() => setMobileSub(null)}
        ticket={{ volume, setVolume, snapshot: market, runtimeUrl: connectedRuntimeUrl, deliveryNotice: ticketDeliveryNotice, selectedPrice, selectedPosition,
          simulationEnabled: props.simulationEnabled, simulationBlockedReason: props.simulationBlockedReason, busy: props.tradeBusy, draft: ticketDraft,
          closeRequest, currentPositions, onTargetPosition: props.onTargetPosition,
          // A confirmed order here also counts for the 安装到桌面 guide's timing.
          onSubmit: async request => { const outcome = await submitManualOrder(request); if (outcome?.accepted) setOrderedHere(true); return outcome; } }} />
    ) : mobileSub === "positions" ? (
      <div className="fdm-overlay" data-testid="mobile-positions">
        <PositionsDock account={props.account} instruments={props.instruments} quote={quote} simulationOrders={props.simulationOrders}
          review={tradeReview.review} foreignReview={foreignTradeReview.review} reviewError={tradeReview.error} busy={props.tradeBusy}
          tab={positionsTab} onTabChange={setPositionsTab} history={ordersHistory} onHistoryChange={setOrdersHistory}
          fillsSource={fillsSource} onFillsSourceChange={setFillsSource}
          onOrderAction={handleOrderAction} onClosePosition={closePosition} onRefresh={props.onRefreshAccount}
          onClose={() => setMobileSub(null)} activeProtections={activeProtections} protectionWarning={protectionWarning} onAskAi={askAi}
          stops={props.aiExecution?.stops} protectionBusy={props.aiExecutionBusy}
          onCreateStop={createPositionStop} onCancelStop={cancelPositionStop} onReview={() => tapPage("review")} />
      </div>
    ) : mobileSub === "briefs" ? (
      <MobileBriefs baseUrl={connectedRuntimeUrl} enabled={appWorkbench.config.showMarketBriefs} blocked={briefsBlocked} offline={!connectedRuntimeUrl}
        onToggle={toggleBriefs} instruments={props.instruments?.items ?? []} watchlist={watchlist} onClose={() => setMobileSub(null)} />
    ) : mobileSub === "install" ? (
      <InstallGuide detection={installDetection} onClose={() => setMobileSub(null)}
        onOpenPush={pushSupport === "supported" && !push.thisDevice && CLIENT_BUILD.platform === "web" ? () => setMobileSub("push") : undefined} />
    ) : mobileSub === "push" ? (
      <MobilePush push={push} offline={!connectedRuntimeUrl} onOpenSettings={openReminderSettings} onOpenInstall={() => setMobileSub("install")} onClose={() => setMobileSub(null)} />
    ) : mobileSub === "reminders" ? (
      <MobileReminders onClose={() => setMobileSub(null)} tabs={<RemindersTabs tab={remindersTab} onTab={setRemindersTab} />}><RemindersTabBody tab={remindersTab} center={reminderCenter} settings={reminderSettings} mine={myReminders} mobile /></MobileReminders>
    ) : mobileSub === "lab" ? (
      <MobileLab rootUrl={connectedRuntimeUrl ? props.rootBaseUrl ?? connectedRuntimeUrl : undefined} baseUrl={connectedRuntimeUrl} labId={mobileLabId}
        instruments={props.instruments} chartTimeframe={props.duration} sessionId={props.chatSessionId}
        onClose={() => setMobileSub(null)} onAskAi={askMobile} onReport={setMobileLab} />
    ) : mobileSub === "review" ? (
      <section className="fdm-overlay fdm-page" aria-label="复盘摘要" data-testid="mobile-review" data-agent-target="mobile-review">
        <header className="fdm-head"><button type="button" className="fdm-back" aria-label="返回" onClick={() => setMobileSub(null)}><ChevronLeft /></button><strong>复盘摘要</strong></header>
        <main className="fdm-content">
          {props.runtimeBaseUrl !== undefined ? <AccountPnl baseUrl={props.runtimeBaseUrl} rootUrl={props.rootBaseUrl ?? props.runtimeBaseUrl} symbol={quote?.symbol ?? props.selectedSymbol ?? ""} chartSymbols={(props.instruments?.items ?? []).map(item => item.symbol)} chartAvailable={Boolean(quote)} accountId={reviewAccountId} foreignAccountId={foreignReviewAccountId} snapshots={ledgerSnapshots} onFocus={setTradeFocus}
            onSelectSymbol={props.onSelectSymbol} initialBalance={initialBalance} onInitialBalanceChange={setInitialBalance} compact />
            : <p className="fdm-note" role="status">连接行情后可看复盘。</p>}
        </main>
      </section>
    ) : null;
    return (
      <AccountAccessContext.Provider value={{ ...access, message: accessNotice }}><UiNavigationContext.Provider value={navigation}><div
        className={`fdm-shell app-theme-${resolvedTheme}`}
        data-testid="mobile-workspace"
        data-theme-preference={chartSettings.theme}
        data-connection={props.connection}
        data-font-scale={props.fontScale}
        data-interface-density={props.interfaceDensity}
      >
        {dialogs}
        <WebUpdateBanner />
        <MobileWorkspace tab={mobileTab} onTab={selectMobileTab} onOpenChat={openChat} quote={quote} symbolName={quoteEntry?.instrumentName} decimals={decimals} ptt={ptt}
          command={isGuest ? undefined : aiCommand} context={mobileContext} aiBlocked={aiBlocked} aiBusy={Boolean(props.chatBusy || chat.sendingPrompt)}
          notices={<>
            {!props.guest && !props.replayActive ? <ConnectionProgress connection={props.connection} runtime={props.state.runtime} agent={props.agentAccount} hasMarket={Boolean(market)} runtimeUsable={props.status.runtimeUsable} onSettings={() => setSettingsOpen(true)} /> : null}
            {executionStopped ? <div className="fdm-stop-banner">{recoveryButton}</div> : null}
            {props.state.connectionError || props.state.runtime?.agentNotice ? <div className="fdw-runtime-notice" role={props.state.connectionError ? "alert" : "status"}>{props.state.connectionError || props.state.runtime?.agentNotice}</div> : null}
          </>}
          feed={<MobileHome rows={notifyRows} pending={pendingCards} quickPending={Boolean(quick.pending)} now={now}
            renderCard={item => <CardRenderer card={item.card} actions={pinnedActions} />}
            today={{ positions: positions.length, floating: ownPositions(positions).length ? ownPositions(positions).reduce((sum, position) => sum + (Number(position.unrealizedPnl) || 0), 0) : null,
              // 本站模拟两本账: the 外盘 USD ledger's floating P&L beside the RMB one, never added to it.
              floatingUsd: positions.some(position => position.accountId)
                ? positions.filter(position => position.accountId).reduce((sum, position) => sum + (Number(position.unrealizedPnl) || 0), 0) : null,
              reminders: reminders.today.length, unreadReminders: reminders.unread }}
            onPositions={() => tapPage("positions")} watch={watch} judgments={chips} onJudgment={openJudgment}
            filter={notifyFilter} onFilter={setNotifyFilter}
            products={notifyProductList} product={shownNotifyProduct} onProduct={setNotifyProduct}
            onOpen={openNotify} onReminders={showReminders}
            latest={<LatestReminders reminders={reminders} onOpen={openReminder} onBreakeven={props.proposalActions?.breakeven} accountName={reminderAccountName} onAll={showReminders} />}
            notice={mobileNotice} refreshError={flowBriefs.refreshError} focus={timelineFocus}
            loading={desk.loading && Boolean(quote?.symbol)} />}
          chat={aiDock({ onBack: () => selectMobileTab("ai"), accountLabel: currentAccount?.displayName })}
          onBell={showReminders} unread={reminders.unread} overlay={overlay} overlayTabs={mobileSub === "reminders" && !mobileEditorOpen} onCloseOverlay={() => { if (mobileEditorOpen) hideDraft(); else setMobileSub(null); }} onRefreshFeed={props.guest ? undefined : refreshFeed}
          pages={{
            ai: props.guest ? signIn("今天", "登录后 AI 帮你盯盘、给出判断，按住就能和它说话。") : undefined,
            research: <MobileResearch baseUrl={connectedRuntimeUrl} symbol={quote?.symbol ?? props.selectedSymbol}
              productName={researchProduct}
              judgment={desk.card} watchlist={watchlist} instruments={props.instruments?.items ?? []}
              onSelectSymbol={selectContract} onOpenContracts={() => setContractOpen(true)} handlers={mobileCardHandlers}
              guest={props.guest && { baseUrl: props.guest.baseUrl, onExpired: props.guest.reportExpired }} onRefresh={reloadQuoteDesk} />,
            market: <MobileMarket market={market} name={contractName} decimals={decimals} chartView={chartView}
              onSelectDuration={selectDuration} onSelectChartView={selectChartView} onOpenContracts={() => setContractOpen(true)} onSelectSymbol={selectContract}
              chartSettings={chartSettings} setChartSettings={setChartSettings} chartPreferences={chartPreferences} resolvedTheme={resolvedTheme}
              alerts={alerts.available ? alerts : undefined} positions={props.account?.positions ?? []} watchlist={watchlist} board={board.snapshot}
              instruments={props.instruments?.items ?? []} onAskBar={askMobile}
              onOrder={() => { if (isGuest) access.require("order-proposal"); else tapPage("order"); }}
              onAlertSignIn={isGuest ? () => access.require("price-alerts") : undefined}
              orderBlocked={market ? undefined : "行情尚未就绪"} marketError={props.marketError}
              mobileMarket={mobileMarket} indicators={chartIndicators} onIndicatorsChange={chartIndicatorsChange} indicatorStatus={isGuest ? undefined : prefs.status}
              baseUrl={connectedRuntimeUrl} boardError={board.error} guest={isGuest} onSignIn={props.onOpenCloudLogin}
              historyNote={props.guest ? guestHistoryNote(market?.historyTradingDays) : undefined}
              drawings={drawings.available ? drawings : undefined}
              onOpenChart={(symbol, duration) => {
                if (!sameContract(symbol, quote?.symbol)) selectContract(symbol);
                if (duration && (duration !== props.duration || chartView !== "kline")) selectDuration(duration);
                if (mobileMarket.market.view !== "single") mobileMarket.update({ view: "single" });
              }} />,
            strategy: props.guest ? signIn("策略", "登录后看运行中的策略、暂停或继续，看我的策略。") : <MobileStrategy strategies={mobileStrategies} instruments={props.instruments}
              blocked={strategyBlocked} tab={mobileStrategyTab} onTabChange={setMobileStrategyTab}
              draftName={mobileEditor.editor.draft && !mobileEditor.editor.open ? mobileEditor.editor.form.name.trim() || "未命名策略" : null}
              onNew={connectedRuntimeUrl ? mobileEditor.onNew : undefined} onResumeDraft={mobileEditor.onResumeDraft} onEdit={mobileEditor.onEdit}
              starts={mobileRootUrl ? <StrategyStarts compact rootUrl={mobileRootUrl} symbol={quote?.symbol ?? props.selectedSymbol ?? ""}
                symbolLabel={contractName} onStart={start => mobileEditor.onNew(start)} /> : undefined}
              onFocusChange={setMobileStrategyFocus}
              stop={props.replayActive ? undefined : { status: props.aiExecution?.controls?.stopStatus,
                disabled: Boolean(props.aiExecutionBusy) || props.connection !== "connected" || !props.simulationGatewayAvailable,
                onHaltOpens: () => props.onStopAiExecution(false, "opens"), onEmergency: () => setEmergencyOpen(true) }}
              onChart={run => {
                if (!sameContract(run.symbol, quote?.symbol)) selectContract(run.symbol);
                if (run.timeframeSeconds && run.timeframeSeconds !== props.duration && [60, 180, 300, 900, 1800, 3600, 86400, 604800].includes(run.timeframeSeconds)) {
                  selectDuration(run.timeframeSeconds as MarketWorkbenchSnapshot["durationSeconds"]);
                }
                showMobileChart();
              }} />,
            me: props.guest ? signIn("我的", "登录后看账户与持仓，改通知和设置。") : <MobileMe userName={ownerPlan.status?.username ?? ""} accountLabel={accountLabel}
              equity={`${formatNumber(props.account?.account.equity, 0)}${ledgerSnapshots.USD ? ` · 美元 ${formatNumber(ledgerSnapshots.USD.equity, 0)}` : ""}`}
              alertCount={alerts.active.length}
              appearance={`${{ dark: "深色", light: "浅色", system: "跟随系统" }[chartSettings.theme]} · ${FONT_SCALE_OPTIONS.find(option => option.value === props.fontScale)?.label ?? "标准"}`}
              onOpenPage={tapPage}
              onOpenSettings={page => { setSettingsPage(page); setSettingsOpen(true); }}
              onOpenAlerts={() => { showMobileChart(); applyUiSurface("disclosure:price-alerts", true); }}
              installed={installDetection.environment === "installed"}
              pushValue={pushRowValue(push.fcm ? { ...push.support, support: "unsupported" } : push.support, Boolean(push.thisDevice))}
              notify={{ briefs: appWorkbench.config.showMarketBriefs, briefCount: briefSubscriptions.data?.products.length }}
              onOpenReminderSettings={openReminderSettings} onBack={() => selectMobileTab("ai")} />,
          }} />
        {trailing}
      </div></UiNavigationContext.Provider></AccountAccessContext.Provider>
    );
  }

  return (
    <AccountAccessContext.Provider value={{ ...access, message: accessNotice }}><UiNavigationContext.Provider value={navigation}><div
      className={`fdw-shell fdw-${view} ${rightDock ? "dock-open" : "dock-closed"} app-theme-${resolvedTheme}`}
      data-right-dock={rightDock ?? "closed"}
      data-dock-side={dockSide}
      data-theme-preference={chartSettings.theme}
      data-testid="futures-decision-workspace"
      data-connection={props.connection}
      data-replay-controls={Boolean(props.replayControls)}
      data-font-scale={props.fontScale}
      data-interface-density={props.interfaceDensity}
      style={workspaceStyle}
    >
      <WorkspaceHeader workspace={workspace} view={view} onSelectView={selectView} recoveryButton={executionStopped ? recoveryButton : null} tradingAccounts={tradingAccounts}
        onVisibleCurrentContractChange={setHeaderCurrentContract}
        pinnedSymbols={pinnedSymbols} onSelectContract={selectContractTab} onPinContract={() => { setContractPinMode(true); setContractOpen(true); }} onUnpinContract={unpinContractTab} replayBlocked={replayBlocked}
        guestActions={props.guest ? <PttButton ptt={ptt} /> : undefined}
        aiBar={props.guest || mobile ? undefined : <AiCommandBar busy={Boolean(props.chatBusy || chat.sendingPrompt)} chatOpen={rightDock === "ai"}
          blockedReason={aiBlocked} send={aiCommand.send} ptt={ptt} />} />

      {dialogs}
      <WebUpdateBanner />
      {props.replayControls}

      <div className="fdw-body">
        <main className="fdw-market" data-testid="market-workspace">
          {(market || (!props.guest && view === "review")) && view !== "board" ? null : aiOrb}
          {!props.guest && !props.replayActive ? <ConnectionProgress connection={props.connection} runtime={props.state.runtime} agent={props.agentAccount} hasMarket={Boolean(market)} runtimeUsable={props.status.runtimeUsable} onSettings={() => setSettingsOpen(true)} /> : null}
          {props.guest && (props.guest.status || props.guest.notice || ptt.status.notice || ptt.status.partial) ? <div className="fdw-runtime-notice" role="status">
            {props.guest.status || props.guest.notice}<PttLive status={ptt.status} />
            {props.guest.status && <button type="button" onClick={() => void props.guest?.reconnect()}>重新连接</button>}
          </div> : null}
          {props.state.connectionError || (!props.replayActive && props.state.runtime?.agentNotice) ? (
            <div
              className="fdw-runtime-notice"
              role={props.state.connectionError ? "alert" : "status"}
              data-testid={props.state.runtime?.agentNotice ? "cloud-agent-notice" : undefined}
            >
              {props.state.connectionError || props.state.runtime?.agentNotice}
            </div>
          ) : null}
          {view !== "review" ? blockingMarketState : null}
          {market || (!props.guest && view === "review") ? (
            <>
              {!market || view === "board" ? null : <MarketCommand workspace={workspace} market={market} headerCurrentContract={headerCurrentContract} chartView={chartView} decimals={decimals} onOpenContracts={() => setContractOpen(true)}
                onSelectDuration={selectDuration} onSelectChartView={selectChartView}
                alerts={alerts.available ? alerts : undefined}
                reminderBell={reminderAvailable ? <ReminderBell open={reminderShown} onOpenChange={showReminderCenter} unread={reminders.unread} /> : undefined}
                drawings={drawings.available ? drawings : undefined}
                watchlistStar={watchlistHead} onToggleWatchlist={watchlistChange} watchlistStatus={watchlistStatus || undefined}
 />}

              <section className={`fdw-market-core ${view} ${view === "review" ? "side-open" : "side-closed"}`}>
                {view === "board" ? <MarketBoard snapshot={board.snapshot} foreign={foreignBoard} error={board.error} query={boardQuery} onQueryChange={setBoardQuery}
                  exchange={boardExchange} onExchangeChange={setBoardExchange} sort={boardSort} onSortChange={setBoardSort}
                  columns={boardColumns} onColumnsChange={boardColumnsChange} columnsStatus={prefs.status}
                  availableExchanges={boardExchanges} selectedSymbol={props.selectedSymbol} onSelect={selectBoardContract}
                  watchlist={watchlist} productNames={boardProductNames} onToggleWatchlist={watchlistChange} watchlistStatus={watchlistStatus || undefined} /> : (
                <div className="fdw-chart-area">
                {blockingMarketState}
                {market && <ChartStage workspace={workspace} market={market} chartView={chartView} decimals={decimals} chartSettings={chartSettings} setChartSettings={setChartSettings}
                  chartPreferences={chartPreferences} onChartIndicatorsChange={updateChartPreferences} chartIndicatorStatus={prefs.status}
                  annotationsVisible={preferences.chart.aiAnnotationsVisible} resolvedTheme={resolvedTheme} appWorkbench={appWorkbench} focus={focus}
                  alerts={alerts.available ? alerts : undefined}
                  drawings={drawings.available ? drawings : undefined}
                  selectedPrice={selectedPrice} setSelectedPrice={setSelectedPrice}
                  onSelectDuration={selectDuration} onSelectChartView={selectChartView}
                  openFills={openFills} openChat={openChat} setPrompt={setPrompt}
                  onReminder={mobile || props.guest ? undefined : playbook => openReminderEditor({ new: true, reminder: true,
                    ...(playbook ? { ask: `按我的交易笔记「${playbook.name}」设提醒：${playbook.entryRules.trim() || playbook.name}`.slice(0, 500) } : {}) })} />}
                {market && !props.guest && !mobile && !props.replayActive ? <AiWatching
                  alerts={alerts.active.filter(alert => sameContract(alert.symbol, quote?.symbol))} strategies={appWorkbench.activity?.strategies ?? []}
                  todayReminders={reminders.today.length} onOpen={target => {
                    if (target === "strategies") { setStrategyTab("running"); setRightDock("strategy"); }
                    else { setRemindersTab(target === "history" ? "history" : "mine"); setRightDock("reminders"); }
                  }} /> : null}
                {aiOrb}
                </div>)}
                {!props.guest && view === "review" ? <aside className="fdw-market-side">{props.runtimeBaseUrl !== undefined ? props.replayActive ? <TradeReview baseUrl={props.runtimeBaseUrl} symbol={props.selectedSymbol ?? ""} onFocus={setTradeFocus}/> : <AccountPnl baseUrl={props.runtimeBaseUrl} rootUrl={props.rootBaseUrl ?? props.runtimeBaseUrl} symbol={quote?.symbol ?? props.selectedSymbol ?? ""} chartSymbols={(props.instruments?.items ?? []).map(item => item.symbol)} chartAvailable={Boolean(quote)} accountId={reviewAccountId} foreignAccountId={foreignReviewAccountId} snapshots={ledgerSnapshots} onFocus={setTradeFocus} onSelectSymbol={props.onSelectSymbol} initialBalance={initialBalance} onInitialBalanceChange={setInitialBalance} onVoiceReview={call.available ? call.review : undefined} voiceReviewing={Boolean(call.status.review)} /> : <ActivityPanel account={props.account} simulationOrders={props.simulationOrders} busy={props.tradeBusy} onOrderAction={handleOrderAction} />}</aside> : null}
              </section>
              {market && !props.guest && (!props.replayActive || view === "trade") ? <PositionsBar account={props.account} instruments={props.instruments} quote={quote} simulationOrders={props.simulationOrders} busy={props.tradeBusy}
                review={tradeReview.review} foreignReview={foreignTradeReview.review} reviewError={tradeReview.error}
                collapsed={positionsBar === "collapsed"} autoHide={autoHidePositionsBar} tab={positionsTab} onTabChange={setPositionsTab} onToggle={togglePositionsBar}
                onOrderAction={handleOrderAction} onClosePosition={closePosition} onOpenAll={() => setRightDock("positions")}
                history={ordersHistory} onHistoryChange={setOrdersHistory} /> : null}
            </>
          ) : null}
        </main>

        {rightDock ? <button type="button" className="fdw-dock-resizer" role="separator" aria-label="调整辅助区宽度" aria-orientation="vertical" aria-valuemin={280} aria-valuemax={panelMaxWidth(rightDock)} aria-valuenow={dockWidth} onPointerDown={event => startDockResize(event, rightDock)} onKeyDown={event => resizeDockWithKeyboard(event, rightDock)}><GripVertical /></button> : null}

        {rightDock ? <aside className="fdw-side-workbench" aria-label="AI 与交易辅助工作区">
          <div className={`fdw-side-workbench-layout dock-${rightDock}`}>
        {!props.guest && rightDock === "ai" ? aiDock() : null}
        {rightDock === "news" ? <NewsPanel baseUrl={props.guest?.baseUrl ?? connectedRuntimeUrl} guest={Boolean(props.guest)} replay={Boolean(props.replayActive)} onGuestExpired={props.guest?.reportExpired} symbol={quote?.symbol} onClose={() => setRightDock(null)}
          watchProducts={flashWatchProducts} instruments={props.instruments?.items} onSelectSymbol={selectContract}
          debate={props.guest ? undefined : debate} tab={newsTab} onTab={setNewsTab} /> : null}
        {!props.guest && rightDock === "reminders" ? <RemindersPanel tab={remindersTab} onTab={setRemindersTab} onClose={() => setRightDock(null)}
          center={reminderCenter} settings={reminderSettings} mine={myReminders} /> : null}
        {!props.guest && rightDock === "strategy" ? <StrategyDock workspace={workspace} quote={quote}
          onClose={() => setRightDock(null)} tab={strategyTab} onTabChange={setStrategyTab}
          onStrategySelected={focus.setActiveStrategy} autonomy={preferencesLoaded ? preferences.trade.aiAutonomy : undefined}
          onAskAi={askAi} explainBlocked={signalsBlocked} backtestBlocked={backtestBlocked}
          onShowReminders={showMyReminders}
          onShowChart={(symbol, duration) => {
            if (!sameContract(symbol, quote?.symbol)) props.onSelectSymbol(symbol);
            if (duration !== props.duration) selectDuration(duration as MarketWorkbenchSnapshot["durationSeconds"]);
          }} /> : null}
        {!props.guest && rightDock === "positions" ? <PositionsDock account={props.account} instruments={props.instruments} quote={quote} simulationOrders={props.simulationOrders}
          review={tradeReview.review} foreignReview={foreignTradeReview.review} reviewError={tradeReview.error} busy={props.tradeBusy}
          tab={positionsTab} onTabChange={setPositionsTab} history={ordersHistory} onHistoryChange={setOrdersHistory}
          fillsSource={fillsSource} onFillsSourceChange={setFillsSource}
          onOrderAction={handleOrderAction} onClosePosition={closePosition} onRefresh={props.onRefreshAccount}
          onClose={() => setRightDock(null)} activeProtections={activeProtections} protectionWarning={protectionWarning} onAskAi={askAi}
          stops={props.aiExecution?.stops} protectionBusy={props.aiExecutionBusy}
          onCreateStop={createPositionStop} onCancelStop={cancelPositionStop} /> : null}
        {!props.guest && rightDock === "order" && view === "trade" && market ? (
          <section className="fdw-order-panel" aria-label="固定下单面板" data-agent-target="panel:order">
            <header><div><ReceiptText /><span><strong>模拟交易</strong><small className={closed || !dataLive ? "blocked" : "live"}>{quote?.name ?? quote?.symbol} · {closed ? sessionLabel : dataLive ? "行情正常" : "不可下单"}</small></span></div><button type="button" aria-label="收起模拟下单" onClick={() => setRightDock(null)}><PanelRightClose /></button></header>
            {/* 五档 → 下单票 → 本合约持仓 in one scroll region (2026-10-03). */}
            <div className="fdw-order-scroll">
              <OrderDepth quote={market.quote} selectedPrice={selectedPrice} onPriceSelect={pickLadderPrice} />
              <TradeTicket volume={volume} setVolume={setVolume} snapshot={market} runtimeUrl={connectedRuntimeUrl} deliveryNotice={ticketDeliveryNotice} selectedPrice={selectedPrice} selectedPosition={selectedPosition} simulationEnabled={props.simulationEnabled} simulationBlockedReason={props.simulationBlockedReason} busy={props.tradeBusy} draft={ticketDraft} closeRequest={closeRequest} currentPositions={currentPositions} onSubmit={submitManualOrder} onTargetPosition={props.onTargetPosition} />
              <OrderPanelPositions quote={market.quote} positions={currentPositions} orders={props.simulationOrders} instruments={props.instruments} busy={props.tradeBusy}
                onClosePosition={closePosition} onOrderAction={handleOrderAction} onAskAi={askAi}
                stops={props.aiExecution?.stops} protectionBusy={props.aiExecutionBusy} onCreateStop={createPositionStop} onCancelStop={cancelPositionStop} />
              <QuickActions controller={quickActions} onSettings={() => { setSettingsPage("shortcuts"); setSettingsOpen(true); }} />
            </div>
          </section>
        ) : null}
          </div>
        </aside> : null}
        <ToolsRail access={access} rightDock={rightDock} setRightDock={setRightDock} onTrade={() => setView("trade")} onSettings={() => setSettingsOpen(true)} settingsOpen={settingsOpen} strategyBlocked={strategyBlocked} />
      </div>

      {trailing}
    </div></UiNavigationContext.Provider></AccountAccessContext.Provider>
  );
}

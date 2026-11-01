import { useGuestUiControl } from "../../hooks/useGuestUiControl";
import type { RefObject } from "react";
import type { UiChanges, UiState } from "../../hooks/useUiControl";
import type { AccountPosition, MarketWorkbenchSnapshot } from "../../api";
import { useUiControl } from "../../hooks/useUiControl";
import { applyChartAnnotations, clearChartMarks, clearHighlights, showHighlight, useChartAnnotations, useChartMarksClearedAt, useHighlights } from "../../aiAnnotations";
import { nextTourStep, prevTourStep, registerTourFocus, startTour, stopTour, useTour, type TourFocus } from "../../uiTour";
import { useEffect, useSyncExternalStore } from "react";
import { CARD_DISCLOSURES, DISCLOSURE_ALIASES, currentSurface, ID_REQUIRED_DISCLOSURES, SPARSE_DISCLOSURES, applyUiSurface, disclosureStates, latestCardDisclosure, uiSurface, uiSurfaceStates, useUiSurfaceVersion } from "../../uiSurfaces";
import type { StrategyNavigation, StrategyNavigationState } from "../../uiNavigation";
import type { useAppWorkbench } from "../AiActivity";
import { ACCOUNT_STATE_LOADING, type TradingAccountsView } from "../../hooks/useTradingAccounts";
import type { FuturesDecisionWorkspaceProps } from "./props";
import { switchableAccounts } from "./TradingAccountSelect";
import type { BoardExchange, BoardSort, FuturesWorkspaceView, RightDock, StrategyTab } from "./support";
import { watchlistKey } from "./support";
import type { FillsSource, PositionsBarTab } from "./PositionPanels";
import type { useWorkspaceLayout } from "./useWorkspaceLayout";
import type { ChartFocus } from "./useChartFocus";
import { REMINDER_SETTINGS_PAGE, normalizeSettingsPage, type SettingsPage } from "./WorkspaceSettings";
import type { RemindersTab } from "../reminders/RemindersPanel";
import { contractCatalogRetryRefusal, contractCatalogState } from "./WorkspaceDrawers";
import type { ChartView, PreferencesHandle } from "../../userPreferences";
import { MA_LINES_MAX, SUB_PANES_MAX, type ChartIndicatorConfig } from "../chartIndicators";
import type { ChartIndicatorPatch } from "../chartIndicatorEditor";
import { useShortcuts } from "../../shortcuts";
import { usePushToTalkStatus } from "../../pushToTalk";
import { voiceCallStore } from "../../voiceRealtime";
import { CLIENT_BUILD, useClientCompat, useWebUpdate } from "../../clientBuild";
import type { MobilePage, MobileTab } from "../mobile/MobileWorkspace";
import type { NotifyFilter } from "../mobile/notifyItems";
import type { InstallEnvironment } from "../mobile/installEnvironment";
import type { MobileMarketChange } from "../mobile/mobileMarketPrefs";

interface Input {
  workspace: FuturesDecisionWorkspaceProps;
  connectedRuntimeUrl: string | undefined;
  market: MarketWorkbenchSnapshot | undefined;
  /** Chart focus (`chartFocus` command): the AI's span rides on the review focus with direction "agent". */
  focus: ChartFocus;
  /** Candidate price (`selectedPrice` command): what a chart click, the ladder or 右键 sets. */
  selectedPrice: number | undefined;
  setSelectedPrice: (price: number | undefined) => void;
  view: FuturesWorkspaceView;
  setView: (view: FuturesWorkspaceView) => void;
  /** 分时图 (2026-10-02): the workspace's chart view and its switch — the `chartView` command
   *  lands here; a `duration` command goes through the same selector a period button uses, so it
   *  returns the window to the K 线 exactly like a click. */
  chartView: ChartView;
  selectChartView: (view: ChartView) => void;
  selectDuration: (duration: MarketWorkbenchSnapshot["durationSeconds"]) => void;
  rightDock: RightDock;
  setRightDock: (dock: RightDock) => void;
  /** 多 AI 辩论 (2026-10-25): `panel: debate` opens the 调研面板 on its 辩论 tab (it had its own rail page). */
  openDebatePage: () => void;
  /** 提醒 (2026-10-25): the right rail's panel / the phone's page and its tab (`reminders` command, snapshot
   *  `reminders`); `openSettings` is where the old `settingsPage: notify` lands (the 通用提醒 tab). */
  reminders: { shown: boolean; tab: RemindersTab; open: (tab: RemindersTab) => void; openSettings: () => void };
  settingsOpen: boolean;
  setSettingsOpen: (open: boolean) => void;
  settingsPage: SettingsPage;
  setSettingsPage: (page: SettingsPage) => void;
  layout: ReturnType<typeof useWorkspaceLayout>;
  /** 底栏自动隐藏 (2026-09-30): true when the collapsed bar is not rendered (no positions, no working orders). */
  positionsBarAutoHidden: boolean;
  /** The session's account directory (`tradingAccount` command; snapshot `tradingAccount`). */
  tradingAccounts: TradingAccountsView;
  strategyUi: StrategyNavigationState | null;
  strategyNavigation: RefObject<((changes: StrategyNavigation) => void) | undefined>;
  appWorkbench: ReturnType<typeof useAppWorkbench>;
  /** Account preferences (`preference` command; snapshot `preferences`). */
  preferences: PreferencesHandle;
  /** 图表指标可配置 (2026-10-02): the normalized chart indicator configuration and its edit path —
   *  the `chartOption` command's kd / macd / maN switches land on `subPaneList` / `maLines`. */
  chartIndicators: ChartIndicatorConfig;
  updateChartIndicators: (patch: ChartIndicatorPatch) => void;
  /** Workspace-owned surfaces (round eleven): tabs, drawers, confirmations and selections. */
  surfaces: WorkspaceSurfaces;
  /** 手机版 (2026-10-04): the bottom tab shown and its switch while the window is in the phone
   *  layout (`mobileTab` command; snapshot `mobileLayout` / `mobileTab`); null on the desktop layout.
   *  第二期: the phone's own full-screen page and its opener (`mobilePage`; 设置 pages and the help
   *  card are read from their own state), the AI 首页 timeline's filter (原通知页) and the unread count.
   *  AI 简报订阅: the product the timeline's AI 简报 is narrowed to (`mobileNotifyProduct`, null = all)
   *  and the products it can take. 新结构 (2026-10-06): `focusTimeline` scrolls AI 首页 to its timeline;
   *  `lab` is the 回测结果页's comparison (`mobileLab`) and `openLab` opens one.
   *  安装到桌面 (2026-10-06): the browser environment the install guide (`mobilePage: install`) teaches.
   *  锁屏提醒 (2026-10-06): what this browser can do about push and whether this device has it on.
   *  统一提醒 (2026-10-15): `unread` is the 提醒中心's unread count (the Runtime's read marks). */
  mobile: { tab: MobileTab; select: (tab: MobileTab) => void; page: "order" | "positions" | "review" | "briefs" | "install" | "push" | "lab" | "reminders" | null;
    /** 手机版 策略 (2026-10-08): its two tabs (`mobileStrategyTab`; reported while the 策略 tab shows). */
    strategyTab: "running" | "mine"; setStrategyTab: (tab: "running" | "mine") => void;
    focusTimeline: () => void; lab: { labId: string; chosen: string | null } | null; openLab: (labId: string) => void;
    installEnvironment: InstallEnvironment;
    push: { support: "supported" | "needs-install" | "unsupported"; subscribed: boolean };
    openPage: (page: MobilePage | null) => void; notifyFilter: NotifyFilter; setNotifyFilter: (filter: NotifyFilter) => void; unread: number;
    notifyProduct: string | null; notifyProducts: string[]; setNotifyProduct: (product: string | null) => void;
    /** 手机看盘 (2026-10-09): the `mobileMarket` command (报价 / 多图 / 单图 and their settings); the values are
     *  reported in `preferences.ui.mobileMarket` and the 副图设置 sheet in `disclosures.mobile-subpanes`. */
    applyMarket: (change: MobileMarketChange) => void;
    /** What a visitor's snapshot reports for 看盘 (a member's is in `preferences`). */
    marketState: { view: string; subPane: string | null; subPanes: boolean } } | null;
}

export interface WorkspaceSurfaces {
  pinSymbol: (symbol: string) => void;
  unpinSymbol: (symbol: string) => void;
  /** 策略面板 tab (`strategyTab` command; snapshot `strategyTab`). */
  strategyTab: StrategyTab;
  setStrategyTab: (tab: StrategyTab) => void;
  /** Chat session switching (`chatSession` command) and the 历史对话 list while it is open. */
  chatSessionId: string | undefined;
  chatHistory: { sessionId: string; createdAt: string }[] | undefined;
  selectChat: (sessionId?: string) => Promise<void>;
  chatBlocked: boolean;
  positionsTab: PositionsBarTab;
  setPositionsTab: (tab: PositionsBarTab) => void;
  /** 成交 · 来源 filter in the 持仓面板 (`fillsSource` command; snapshot `fillsSource`). */
  fillsSource: FillsSource;
  setFillsSource: (source: FillsSource) => void;
  /** 委托"历史"; the bottom bar and the 持仓面板 share it. */
  ordersHistory: boolean;
  setOrdersHistory: (history: boolean) => void;
  /** 报价总表 filter and sort (`boardExchange` / `boardSort` commands; reported while the view is on). */
  boardExchange: BoardExchange;
  setBoardExchange: (exchange: BoardExchange) => void;
  boardSort: BoardSort;
  setBoardSort: (sort: BoardSort) => void;
  /** 自选 (2026-10-02): how many products the account stars (snapshot `boardWatchlistCount`). */
  boardWatchlistCount: number;
  /** 合约抽屉两级: the expanded product (`contractProduct` command; reported while the drawer is open). */
  contractProduct: string | null;
  setContractProduct: (product: string | null) => void;
  quickSkipConfirm: boolean;
  setQuickSkipConfirm: (value: boolean) => void;
  currentPositions: AccountPosition[];
  selectedPosition: AccountPosition | undefined;
  selectPosition: (position: AccountPosition | null) => void;
  dialogs: Record<"contract-drawer" | "resume-execution" | "emergency-stop", { open: boolean; set: (open: boolean) => void; available?: boolean }>;
}

/** Commands that open or change a desktop-only surface (side panels, views, the bottom bar, the
 *  floating AI 动态 card): a phone-layout window has none of them, so it refuses instead of
 *  reporting a change nobody can see. `panel` itself is judged by its value (ai = the chat tab).
 *  `strategyEditor` (2026-10-08) is the phone's full-screen 策略编辑卡 page, not refused. */
const PHONE_REFUSED = new Set(["view", "news", "monitorCard", "strategy", "strategyTab", "strategyBacktest",
  "strategyTemplate", "strategyLab", "strategyLaunch", "positionsBar", "boardExchange", "boardSort", "aiActivityCard"]);
/** Trading controls a UI command never changes (#932): the Runtime refuses them, the window too. */
const TRADE_CONTROL_PREFERENCES = new Set(["trade.aiAutonomy", "trade.voiceOrderConfirm"]);
/** 设置 pages a 我的 row opens, as the phone page the snapshot reports. */
const MOBILE_SETTINGS_PAGES: Partial<Record<SettingsPage, MobilePage>> = { account: "accounts", memory: "memory", "memory-advanced": "memory", ui: "appearance" };

const REGISTRY_DIALOGS = ["account-detail", "fee-hint", "market-session", "help", "replay-setup", "quick-order-confirm", "abandon-order", "update-dialog", "strategy-launch", "change-password", "protection-reprice-confirm"] as const;

/**
 * Registers this window with the Runtime's UI-control channel: reports the
 * presentation state and applies the agent's commands to it. Trading and
 * credentials are never reachable from here.
 */
export function useWorkspaceUiControl({ workspace, connectedRuntimeUrl, market, focus, selectedPrice, setSelectedPrice, view, setView, chartView, selectChartView, selectDuration,
  rightDock, setRightDock, openDebatePage, reminders, settingsOpen, setSettingsOpen,
  settingsPage, setSettingsPage, layout, positionsBarAutoHidden, tradingAccounts, strategyUi, strategyNavigation, appWorkbench, preferences,
  chartIndicators, updateChartIndicators, surfaces, mobile }: Input) {
  const shortcuts = useShortcuts();
  const ptt = usePushToTalkStatus();
  // 没有收到麦克风声音 (10-14): a boolean selector, so the call's 10 Hz level updates cause no re-render.
  const micSilent = useSyncExternalStore(voiceCallStore.subscribe, () => voiceCallStore.snapshot().micSilent === true, () => false);
  const micLabel = useSyncExternalStore(voiceCallStore.subscribe, () => voiceCallStore.snapshot().micLabel, () => undefined);
  const micSuggestion = useSyncExternalStore(voiceCallStore.subscribe, () => voiceCallStore.snapshot().micSuggestion, () => undefined);
  const compat = useClientCompat();
  const webUpdate = useWebUpdate();
  const { chartSettings, setChartSettings, dockSide, setDockSide, panelWidth, panelMaxWidth, setPanelWidth, positionsBar, setPositionsBar, pinnedSymbols, resetLayout } = layout;
  // The old chart switch names, derived from the configured lines / pane list for the snapshot.
  const maOn = (period: number) => chartIndicators.maLines.some(line => line.period === period && line.enabled);
  const paneShare = (type: "kd" | "macd") => {
    const at = chartIndicators.subPaneList.indexOf(type);
    return at >= 0 ? chartSettings.paneSizes?.panes[at] ?? [19, 23, 14][Math.min(at, 2)] : [19, 23, 14][type === "kd" ? 0 : 1];
  };
  const quote = market?.quote;
  useUiSurfaceVersion();
  const highlights = useHighlights();
  const chartAnnotations = useChartAnnotations(workspace.selectedSymbol);
  const marksCleared = useChartMarksClearedAt(workspace.selectedSymbol);
  const agentFocus = focus.tradeFocus?.direction === "agent" ? { from: Math.round(focus.tradeFocus.openTime / 1e9), to: Math.round(focus.tradeFocus.closeTime / 1e9) } : null;
  const tour = useTour();
  const registered = uiSurfaceStates();
  const dialogState = (name: string): boolean | undefined => {
    if (name in surfaces.dialogs) return surfaces.dialogs[name as keyof typeof surfaces.dialogs].open;
    const entry = registered[`dialog:${name}`] as { open?: boolean } | undefined;
    return entry?.open;
  };
  const openDialog = [...Object.keys(surfaces.dialogs), ...REGISTRY_DIALOGS].find(name => dialogState(name)) ?? null;
  const review = registered.review as { month: string | null; day: string | null; filter: string; selectedTradeId: string | null;
    currency?: "CNY" | "USD"; currencies?: ("CNY" | "USD")[];
    selectedNote?: string | null; tiles?: { label: string; value: string; note: string }[]; equityCurve?: Record<string, unknown> | null;
    dailyReview?: Record<string, unknown> | null; habits?: Record<string, unknown> | null } | undefined;
  const apply = (changes: UiChanges) => {
    if (changes.remoteSettings != null) {
      if (workspace.guest) throw new Error("请登录后设置遥控器");
      const remote = uiSurface("remoteSettings");
      if (!remote?.apply || !settingsOpen) throw new Error("请先打开语音、按键设置或测试与诊断页");
      remote.apply(changes.remoteSettings);
    }
    // 对话栏宽度随窗口 (2026-10-14): a width this window cannot show is refused with its limit, before anything changes.
    const width = changes.dockWidth ?? changes.presentation?.dockWidth;
    if (width != null && width > panelMaxWidth(rightDock)) throw new Error(`这个窗口里辅助区最宽 ${panelMaxWidth(rightDock)}px`);
    // A presentation bundle cannot contain navigation, strategy or trading.
    if (changes.presentation) {
      const { reset, ...rest } = changes.presentation;
      if (reset) resetLayout();
      changes = { ...rest } as typeof changes;
    }
    if (changes.mobileTab != null) {
      if (!mobile) throw new Error("这个窗口是电脑布局，没有手机标签");
      // 2026-10-06: the retired 通知 tab is AI 首页's timeline (its filter kept).
      if (changes.mobileTab === "notify") { mobile.select("ai"); mobile.focusTimeline(); }
      else mobile.select(changes.mobileTab);
    }
    if (mobile && (Object.keys(changes).some(key => PHONE_REFUSED.has(key)) || (changes.panel != null && changes.panel !== "ai"))) {
      throw new Error("手机版没有这个界面，请在电脑上打开");
    }
    if ("mobilePage" in changes) {
      if (!mobile) throw new Error("这个窗口是电脑布局，没有手机子页");
      mobile.openPage(changes.mobilePage ?? null);
    }
    if (changes.mobileStrategyTab != null) {
      if (!mobile) throw new Error("这个窗口是电脑布局，没有手机策略页");
      mobile.select("strategy");
      mobile.setStrategyTab(changes.mobileStrategyTab);
    }
    if (changes.mobileMarket != null) {
      if (!mobile) throw new Error("这个窗口是电脑布局，没有手机看盘页");
      mobile.applyMarket(changes.mobileMarket as MobileMarketChange);
      mobile.select("market");
    }
    if (changes.mobileLab != null) {
      if (!mobile) throw new Error("这个窗口是电脑布局，没有手机回测结果页");
      mobile.openLab(changes.mobileLab.labId);
    }
    // 提醒中心 (2026-10-15): the bell's popover on a computer, the 提醒 page on a phone — one surface the
    // workspace registers while the window has it (a member's connected window).
    if (changes.reminderCenter != null) {
      const center = uiSurface("reminderCenter");
      if (!center?.apply) throw new Error("提醒记录要登录并连上服务后才能打开");
      center.apply(changes.reminderCenter);
    }
    // The timeline's retired 提醒 filter: the 提醒 page (2026-10-15).
    if (changes.mobileNotifyFilter === "reminder") {
      if (!mobile) throw new Error("这个窗口是电脑布局，没有手机 AI 首页");
      mobile.openPage("reminders");
    } else if (changes.mobileNotifyFilter != null) {
      if (!mobile) throw new Error("这个窗口是电脑布局，没有手机 AI 首页");
      mobile.setNotifyFilter(changes.mobileNotifyFilter);
      mobile.select("ai");
      mobile.focusTimeline();
    }
    if ("mobileNotifyProduct" in changes) {
      if (!mobile) throw new Error("这个窗口是电脑布局，没有手机 AI 首页");
      const product = changes.mobileNotifyProduct ?? null;
      if (product && !mobile.notifyProducts.includes(product)) throw new Error("没有订阅这个品种，时间线里也没有它");
      mobile.setNotifyFilter("brief");
      mobile.setNotifyProduct(product);
      mobile.select("ai");
      mobile.focusTimeline();
    }
    // 手机版第二期: the 持仓 tabs, the ticket and the review have no side panel or view on the phone —
    // they land on its 持仓与委托 / 下单 / 复盘摘要 pages (opened first, with the same access checks).
    if (mobile && (changes.positionsTab != null || changes.fillsSource != null || changes.ordersHistory != null)) {
      mobile.openPage("positions");
      if (changes.positionsTab != null) surfaces.setPositionsTab(currentSurface("positionsTabs", changes.positionsTab));
      if (changes.fillsSource != null) { surfaces.setPositionsTab("fills"); surfaces.setFillsSource(changes.fillsSource); }
      if (changes.ordersHistory != null) { surfaces.setPositionsTab("orders"); surfaces.setOrdersHistory(changes.ordersHistory); }
      return;
    }
    if (mobile && changes.ticketMode != null) { mobile.openPage("order"); applyUiSurface("ticketMode", changes.ticketMode); return; }
    // 今日复盘 (2026-10-06): 复盘 (the phone's 复盘摘要) opened on that trading day's review.
    if (changes.reviewDay != null) {
      if (workspace.replayActive) throw new Error("回放中没有账户复盘页");
      if (mobile) mobile.openPage("review");
      else {
        if (workspace.access?.reason("review-summary")) throw new Error(workspace.access.reason("review-summary"));
        setView("review");
        if (rightDock !== "ai") setRightDock(null);
      }
      applyUiSurface("review", { reviewDay: changes.reviewDay });
      return;
    }
    if (mobile && (changes.reviewExpand != null || changes.reviewFilter != null || changes.reviewCurrency != null || "reviewSelect" in changes)) {
      mobile.openPage("review");
      applyUiSurface("review", changes.reviewCurrency != null ? { currency: changes.reviewCurrency } : changes.reviewExpand != null ? { expand: changes.reviewExpand } : changes.reviewFilter != null ? { filter: changes.reviewFilter } : { select: changes.reviewSelect ?? null });
      return;
    }
    if ("selectedPrice" in changes) {
      if (changes.selectedPrice == null) { setSelectedPrice(undefined); return; }
      if (!market) throw new Error("行情尚未就绪");
      if (mobile) mobile.openPage("order");
      else { setView("trade"); setRightDock("order"); }
      setSelectedPrice(changes.selectedPrice);
    }
    if (changes.symbol != null) {
      if (workspace.replayActive || !workspace.instruments?.items.some(item => item.symbol === changes.symbol)) {
        throw new Error("当前环境不可切换到该合约");
      }
      workspace.onSelectSymbol(changes.symbol);
    }
    if (changes.duration != null) selectDuration(changes.duration);
    if (changes.chartView != null) selectChartView(changes.chartView);
    if (changes.view === "review" && workspace.access?.reason("review-summary")) throw new Error(workspace.access.reason("review-summary"));
    if (changes.view === "board" && workspace.guest) throw new Error("游客暂无报价总表");
    if (changes.view != null) {
      if (workspace.replayEnded && changes.view !== "review") throw new Error("回放已结束");
      setView(changes.view);
      if (changes.view !== "trade" && rightDock !== "ai") setRightDock(null);
    }
    if ("panel" in changes) {
      // The 价格阶梯 panel was removed 2026-10-07: an older Runtime, AI or saved command's
      // `panel: depth` opens the 下单面板, whose 五档 replaced it.
      const panel = changes.panel == null ? null : currentSurface("panels", changes.panel);
      const capability = panel === "ai" ? "ai-chat" : panel === "strategy" ? "strategy-management" : panel === "order" ? "order-proposal" : panel === "positions" ? "account" : undefined;
      if (capability && workspace.access?.reason(capability)) throw new Error(workspace.access.reason(capability));
      if (panel === "strategy" && !workspace.strategyPanel) throw new Error("当前环境没有策略面板");
      // 多 AI 辩论: a member's, never in a replay (news and research are today's); since 2026-10-25 the 调研面板's
      // 辩论 tab — the snapshot then reads panel "news", news.tab "debate".
      if (panel === "debate" && workspace.guest) throw new Error("登录后才能用多 AI 辩论");
      if (panel === "debate" && workspace.replayActive) throw new Error("回放中不能用多 AI 辩论");
      if (panel === "reminders" && workspace.guest) throw new Error("登录后才能用提醒");
      if (panel === "order" || panel === "strategy") {
        if (workspace.replayEnded) throw new Error("回放已结束");
        setView("trade");
      }
      if (panel === "debate") openDebatePage();
      else setRightDock(panel);
    }
    if (changes.positionsBar != null) setPositionsBar(changes.positionsBar);
    if (changes.settingsOpen != null) setSettingsOpen(changes.settingsOpen);
    if (changes.theme != null) setChartSettings(current => ({ ...current, theme: changes.theme! }));
    if (changes.dockSide != null) setDockSide(changes.dockSide);
    // The open panel's width: 策略面板 keeps its own (up to what leaves the chart 480 px), the others share one.
    if (changes.dockWidth != null) setPanelWidth(rightDock, changes.dockWidth);
    // 设置 → 提醒 is the 提醒 panel's / page's 通用提醒 tab since 2026-10-25.
    if (changes.settingsPage === REMINDER_SETTINGS_PAGE) { if (workspace.guest) throw new Error("登录后才能设置提醒"); reminders.openSettings(); }
    else if (changes.settingsPage != null) { setSettingsPage(normalizeSettingsPage(changes.settingsPage)); setSettingsOpen(true); }
    if (changes.reminders != null) {
      if (workspace.guest) throw new Error("登录后才能用提醒");
      reminders.open(changes.reminders.tab ?? reminders.tab);
    }
    if (changes.preference != null) {
      // Written through PUT /api/preferences; the receipt follows once the returned document reports the value.
      // AI 自主执行 / 语音下单免口头确认 are trading controls (#932): only PUT /api/preferences itself (the settings
      // switch, the AI's update_preferences) changes them — the Runtime refuses such a command, and so does the window.
      if (TRADE_CONTROL_PREFERENCES.has(changes.preference.key)) throw new Error("AI 自主执行与语音下单免口头确认不经界面命令修改");
      if (!connectedRuntimeUrl) throw new Error("连接后可设置");
      void preferences.set(changes.preference.key, changes.preference.value).catch(() => undefined);
    }
    if (changes.tradingAccount != null) {
      // Written through PUT /api/sessions/{id}/account; the receipt follows once the
      // runtime.status the switch publishes updates the reported account id.
      const directory = tradingAccounts.directory;
      if (!directory || !directory.accounts.some(account => account.id === changes.tradingAccount)) {
        throw new Error("该交易账户不在目录里或未授权");
      }
      // 本站模拟合成一个账户 (2026-10-21): the 外盘 USD ledger is read with reviewCurrency, not switched to.
      if (!switchableAccounts(directory, tradingAccounts.currentId).some(account => account.id === changes.tradingAccount)) {
        throw new Error("美元账已并入本站模拟：外盘下单不用切账户，看美元复盘用 reviewCurrency: USD");
      }
      // Before the session's first status nothing may be switched (#932 follow-up): the same reason the selector shows.
      if (!tradingAccounts.accountReady) throw new Error(ACCOUNT_STATE_LOADING);
      if (changes.tradingAccount !== tradingAccounts.currentId) void tradingAccounts.switchAccount(changes.tradingAccount);
    }
    if (changes.fontScale != null) workspace.onFontScaleChange(changes.fontScale);
    if (changes.density != null) workspace.onInterfaceDensityChange(changes.density);
    if (changes.chartSizes != null) {
      // The command keeps the old {main, kd, macd} shape (the Runtime validates it); each named
      // share lands on the pane that owns it, other panes keep theirs.
      const { main, kd, macd } = changes.chartSizes;
      setChartSettings(current => ({ ...current, paneSizes: {
        main: Math.min(76, Math.max(34, main)),
        panes: chartIndicators.subPaneList.map((type, index) => type === "kd" ? Math.min(36, Math.max(12, kd))
          : type === "macd" ? Math.min(36, Math.max(12, macd))
          : current.paneSizes?.panes[index] ?? [19, 23, 14][Math.min(index, 2)])
      } }));
    }
    if (changes.chartOption != null) {
      const { name, visible } = changes.chartOption;
      if (name === "kd" || name === "macd") {
        // 副图开关 now edits the pane list (chart.subPaneList); a full list refuses a new pane.
        const list = chartIndicators.subPaneList;
        const next = visible ? list.includes(name) ? list : list.length >= SUB_PANES_MAX ? list : [...list, name]
          : list.filter(type => type !== name);
        if (next !== list) updateChartIndicators({ subPaneList: next });
      } else if (name.startsWith("ma")) {
        // The old maN switch names any period now: it toggles that line when it exists and adds it when there is room.
        const period = Number(name.slice(2));
        const lines = chartIndicators.maLines;
        if (Number.isFinite(period) && period > 0) {
          const existing = lines.find(line => line.period === period);
          const next = existing ? lines.map(line => line.period === period ? { ...line, enabled: visible } : line)
            : visible && lines.length < MA_LINES_MAX ? [...lines, { period, enabled: true }] : lines;
          if (next !== lines) updateChartIndicators({ maLines: next });
        }
      } else setChartSettings(current => ({ ...current, [name]: visible }));
    }
    if (changes.strategy != null) {
      if (!strategyNavigation.current) throw new Error("请先打开策略面板");
      strategyNavigation.current(changes.strategy as StrategyNavigation);
    }
    // Round eleven: every user-operable surface answers a command.
    const registry = (name: string) => {
      const surface = uiSurface(name);
      if (!surface?.apply) throw new Error("该界面当前不可用");
      return surface.apply;
    };
    if (changes.pinnedSymbols != null) {
      const { add, remove } = changes.pinnedSymbols;
      if (workspace.replayActive) throw new Error("回放中不能改钉住的合约");
      if (add) {
        if (!workspace.instruments?.items.some(item => item.symbol === add)) throw new Error("合约不存在");
        surfaces.pinSymbol(add);
      }
      if (remove) {
        if (!pinnedSymbols.some(item => item.toUpperCase() === remove.toUpperCase())) throw new Error("该合约没有钉住");
        if (pinnedSymbols.length <= 1) throw new Error("最后一个合约标签不能移除");
        surfaces.unpinSymbol(remove);
      }
    }
    if (changes.chatSession != null) {
      if (!workspace.onChangeChat) throw new Error("当前环境不能切换对话");
      if (surfaces.chatBlocked) throw new Error("正在切换对话，请稍等");
      const wanted = changes.chatSession.sessionId ?? undefined;
      if (wanted && wanted !== surfaces.chatSessionId && !surfaces.chatHistory?.some(item => item.sessionId === wanted)) {
        throw new Error("该会话不在历史对话列表里，先打开 chat-history 读取");
      }
      setRightDock("ai");
      if (wanted !== surfaces.chatSessionId || !wanted) void surfaces.selectChat(wanted);
    }
    // Retired commands, still accepted: the AI dock has no inner page (1.6.6) and no flow filter
    // (2026-09-25); both open the 对话面板.
    if (changes.aiFilter != null || changes.aiPage != null) setRightDock("ai");
    // 交易笔记 (was the 策略 panel's 打法 tab, 改版第一步 2026-10-31) lives on 复盘; the old tab id opens it there.
    if (changes.strategyTab === "playbooks") {
      if (workspace.replayActive) throw new Error("回放中没有账户复盘页");
      if (workspace.access?.reason("review-summary")) throw new Error(workspace.access.reason("review-summary"));
      setView("review");
      if (rightDock !== "ai") setRightDock(null);
    } else if (changes.strategyTab != null) {
      if (!workspace.strategyPanel) throw new Error("当前环境没有策略面板");
      if (workspace.replayEnded) throw new Error("回放已结束");
      setView("trade");
      setRightDock("strategy");
      // 2026-10-08: the retired 实验室 tab is the 结果页's 试几组参数比一比.
      surfaces.setStrategyTab(currentSurface("strategyTabs", changes.strategyTab));
    }
    if (changes.aiActivityCard != null) registry("aiActivityCard")(changes.aiActivityCard);
    // The tab and the 历史 switch are shared by the bottom bar and the 持仓面板. Expand the bar only
    // when the panel is not the one showing them: the user watching the panel does not want the bar
    // pushed open under the chart.
    const showRecords = () => { if (rightDock !== "positions") setPositionsBar("expanded"); };
    // `trades` (平仓记录) is the 成交 tab's old name.
    if (changes.positionsTab != null) { showRecords(); surfaces.setPositionsTab(currentSurface("positionsTabs", changes.positionsTab)); }
    // The 来源 chips live in the 持仓面板's 成交 tab.
    if (changes.fillsSource != null) { setRightDock("positions"); surfaces.setPositionsTab("fills"); surfaces.setFillsSource(changes.fillsSource); }
    if (changes.ordersHistory != null) { showRecords(); surfaces.setPositionsTab("orders"); surfaces.setOrdersHistory(changes.ordersHistory); }
    // 报价总表 (2026-10-01): the filter and sort land on the board view, which opens first (like reviewExpand).
    if (changes.boardExchange != null) { if (view !== "board") setView("board"); surfaces.setBoardExchange(changes.boardExchange); }
    if (changes.boardSort != null) { if (view !== "board") setView("board"); surfaces.setBoardSort({ key: changes.boardSort.key, order: changes.boardSort.order }); }
    // 合约抽屉两级: the expanded product is the drawer's own fold — it needs the drawer open.
    if ("contractProduct" in changes) {
      if (!surfaces.dialogs["contract-drawer"].open) throw new Error("请先打开合约抽屉");
      const wanted = changes.contractProduct ?? null;
      if (wanted != null && !workspace.instruments?.items.some(item => watchlistKey(item) === wanted)) throw new Error("该品种不在合约目录里");
      surfaces.setContractProduct(wanted);
    }
    // The drawer's 重试 after a failed or empty catalog read: the same check the visible button follows.
    if (changes.contractCatalogRetry) {
      if (!surfaces.dialogs["contract-drawer"].open) throw new Error("请先打开合约抽屉");
      const refusal = contractCatalogRetryRefusal(contractCatalogState(workspace));
      if (refusal) throw new Error(refusal);
      workspace.onRetryInstruments();
    }
    if (changes.ticketMode != null) {
      if (workspace.replayEnded) throw new Error("回放已结束");
      setView("trade"); setRightDock("order");
      applyUiSurface("ticketMode", changes.ticketMode);
    }
    if (changes.quickVolume != null) applyUiSurface("quickVolume", changes.quickVolume);
    if (changes.quickOrderConfirm != null) surfaces.setQuickSkipConfirm(!changes.quickOrderConfirm);
    if (changes.reviewExpand != null || changes.reviewFilter != null || changes.reviewCurrency != null || "reviewSelect" in changes) {
      if (workspace.replayActive) throw new Error("回放中没有账户复盘页");
      setView("review");
      const value = changes.reviewCurrency != null ? { currency: changes.reviewCurrency } : changes.reviewExpand != null ? { expand: changes.reviewExpand } : changes.reviewFilter != null ? { filter: changes.reviewFilter } : { select: changes.reviewSelect ?? null };
      applyUiSurface("review", value);
    }
    if (changes.dialog != null) {
      const { name, open, topic } = changes.dialog;
      if (name === "positions-drawer") {
        // 1.6.6: the 持仓抽屉 became the 持仓 side panel; the old dialog name stays as its alias.
        setRightDock(open ? "positions" : rightDock === "positions" ? null : rightDock);
      } else if (name in surfaces.dialogs) {
        const dialog = surfaces.dialogs[name as keyof typeof surfaces.dialogs];
        if (open && dialog.available === false) throw new Error("该对话框当前不可用");
        dialog.set(open);
      } else if (name === "strategy-designer") {
        // The retired 策略设计窗 (2026-10-08) is the 策略编辑卡 now: open = edit the selected strategy (none: new).
        if (strategyNavigation.current) strategyNavigation.current({ designerOpen: open });
        else if (mobile) registry("strategyEditor")(open ? { open: true } : { close: true });
        else if (open) { setView("trade"); setRightDock("strategy"); applyUiSurface("strategyEditor", { open: true }); }
      } else registry(`dialog:${name}`)({ open, topic });
    }
    // 使用说明卡 (2026-09-25): topic / group / search / folds; the card opens itself first.
    if (changes.help != null) registry("help")(changes.help);
    if (changes.strategyDetails != null) registry("strategyDetails")(changes.strategyDetails);
    // 侧栏重设计第三期 (2026-09-25): the open 上线 dialog's fields, and a running card's 执行方式.
    if (changes.strategyLaunch != null) registry("strategyLaunch")(changes.strategyLaunch);
    // 规则回测 (2026-09-25): the result card on the selected strategy's 详情; opens it first.
    if (changes.strategyBacktest != null) {
      if (!uiSurface("strategyBacktest")) { setView("trade"); setRightDock("strategy"); surfaces.setStrategyTab("detail"); }
      applyUiSurface("strategyBacktest", changes.strategyBacktest);
    }
    // 策略模板库 (2026-09-25): the gallery / parameter page in 策略库, 恢复模板默认 on 详情; opens the panel first.
    if (changes.strategyTemplate != null) {
      if (!uiSurface("strategyTemplate")) { setView("trade"); setRightDock("strategy"); surfaces.setStrategyTab(changes.strategyTemplate.restore ? "detail" : "library"); }
      applyUiSurface("strategyTemplate", changes.strategyTemplate);
    }
    // 策略编辑卡 (2026-10-08): new / edit / 起点 / 补一句 / 存为草稿 / close; opens the strategy panel first.
    // On the phone the card is a full-screen page over the tabs; its host is mounted whenever strategies are usable.
    if (changes.strategyEditor != null && mobile) {
      if (!uiSurface("strategyEditor")) throw new Error(workspace.guest ? "登录后才能新建或修改策略" : "策略编辑卡现在打不开：先连接 Runtime，或当前档位不含策略");
      registry("strategyEditor")(changes.strategyEditor);
    } else if (changes.strategyEditor != null) {
      if (!uiSurface("strategyEditor")) { setView("trade"); setRightDock("strategy"); }
      applyUiSurface("strategyEditor", changes.strategyEditor);
    }
    // 试几组参数比一比 (2026-10-08; was the 实验室 tab): the 结果页's comparison; opens the panel first.
    if (changes.strategyLab != null) {
      if (!uiSurface("strategyLab")) { setView("trade"); setRightDock("strategy"); surfaces.setStrategyTab("detail"); }
      applyUiSurface("strategyLab", changes.strategyLab);
    }
    // The admin view renders no dock: an acknowledged news command must also show it.
    if (changes.news != null) { setView("trade"); setRightDock("news"); applyUiSurface("news", changes.news); }
    // 多 AI 辩论 (2026-10-24): the 辩论 page (computer: the workspace's host opens its rail page itself) or the 调研
    // tab's sub-page (phone).
    if (changes.researchDebate != null) {
      if (workspace.guest) throw new Error("登录后才能用多 AI 辩论");
      // A replay shows no news or research (they are today's): refused, never queued for after the replay.
      if (workspace.replayActive) throw new Error("回放中不能用多 AI 辩论");
      if (mobile) mobile.select("research");
      else setView("trade");
      applyUiSurface("researchDebate", changes.researchDebate);
    }
    if (changes.monitorCard != null) {
      setView("trade"); setRightDock("news"); applyUiSurface("news", { tab: "data" });
      applyUiSurface(`monitorCard:${changes.monitorCard.name}`, changes.monitorCard.expanded);
    }
    if (changes.disclosure != null) {
      const { open, id } = changes.disclosure;
      const name = DISCLOSURE_ALIASES[changes.disclosure.name] ?? changes.disclosure.name;
      let key = `disclosure:${name}`;
      if (!id && ID_REQUIRED_DISCLOSURES[name]) throw new Error(ID_REQUIRED_DISCLOSURES[name]);
      if (SPARSE_DISCLOSURES.has(name)) {
        const family = uiSurface(`family:${name}`);
        if (!family?.apply) throw new Error("该界面当前不可用");
        family.apply({ id, open });
      } else {
        if (CARD_DISCLOSURES.has(name)) {
          const latest = id ? `disclosure:${name}:${id}` : latestCardDisclosure(name);
          if (!latest) throw new Error("当前没有这种卡片");
          key = latest;
        }
        registry(key)(open);
      }
    }
    if ("selectPosition" in changes) {
      if (changes.selectPosition == null) { surfaces.selectPosition(null); return; }
      const wanted = changes.selectPosition;
      const position = surfaces.currentPositions.find(item => item.symbol.toUpperCase() === wanted.symbol.toUpperCase() && item.direction === wanted.direction);
      if (!position) throw new Error("没有这个持仓");
      surfaces.selectPosition(position);
    }
    // AI annotation layer: pointing and drawing are the AI's opinion; they never touch orders.
    if (changes.highlight != null) {
      // The Runtime forwards only the fields the agent set; defaults match HighlightChange.
      const { target, label, style = "pulse", durationMs = 6000, clear } = changes.highlight;
      if (clear || !target) clearHighlights();
      else if (durationMs === 0) clearHighlights(target);
      else showHighlight({ target, label, style, durationMs });
    }
    if (changes.chartAnnotations != null) {
      const { op, items, symbol } = changes.chartAnnotations;
      const contract = symbol ?? workspace.selectedSymbol;
      if (!contract) throw new Error("当前没有合约图表");
      if (symbol && !workspace.instruments?.items.some(item => item.symbol === symbol) && symbol !== workspace.selectedSymbol) throw new Error("合约不存在");
      applyChartAnnotations(contract, op, items ?? []);
    }
    // 清空图上标记 (2026-09-30): the same right-click action — drawings, backtest marks and
    // the contract's trade-marker stamp.
    if (changes.chartMarks != null) {
      if (!workspace.selectedSymbol) throw new Error("当前没有合约图表");
      clearChartMarks(workspace.selectedSymbol);
    }
    if (changes.tour != null) {
      const { op, steps } = changes.tour;
      if (op === "start") startTour(steps ?? []);
      else if (op === "next") nextTourStep();
      else if (op === "prev") prevTourStep();
      else stopTour();
    }
    if ("chartFocus" in changes) {
      if (changes.chartFocus == null) { if (focus.tradeFocus?.direction === "agent") focus.clearChartFocus(); return; }
      const { from, to } = changes.chartFocus;
      const bars = market?.bars ?? [];
      if (!bars.length) throw new Error("图表还没有 K 线");
      const first = bars[0].datetime / 1e9, last = bars[bars.length - 1].datetime / 1e9 + (market?.durationSeconds ?? 60);
      if (to < first || from >= last) throw new Error("这段时间不在当前已加载的 K 线范围内");
      // The chart clamps an "agent" span to the loaded bars; the reported focus keeps the requested times.
      focus.setTradeFocus({ openTime: from * 1e9, closeTime: to * 1e9, direction: "agent" });
    }
  };
  // A tour step's focus is a UiChanges subset applied through the same path as a command.
  useEffect(() => { if (workspace.guest) return; registerTourFocus((focus: TourFocus) => { for (const [key, value] of Object.entries(focus)) if (value !== undefined) apply({ [key]: value } as UiChanges); }); return () => registerTourFocus(undefined); });
  const state: UiState = {
    ...(registered.remoteSettings ? { remoteSettings: registered.remoteSettings as Record<string, unknown> } : {}),
    symbol: workspace.selectedSymbol ?? null, duration: workspace.duration, view, panel: rightDock,
    // 分时图: the chart view next to its data period, like `duration` itself (an older Runtime
    // that predates the field rejects the heartbeat — the client ships with its Runtime).
    chartView,
    settingsOpen, theme: chartSettings.theme, dockSide, dockWidth: Math.round(panelWidth(rightDock)),
    replay: Boolean(workspace.replayActive), settingsPage, fontScale: workspace.fontScale, density: workspace.interfaceDensity,
    // The chart switches keep their pre-configuration names (an older Runtime's UiState forbids
    // unknown fields); they are derived from the pane list and MA lines (图表指标可配置, 2026-10-02).
    chart: { kd: chartIndicators.subPaneList.includes("kd"), macd: chartIndicators.subPaneList.includes("macd"),
      volume: chartSettings.volume, crosshair: chartSettings.crosshair, lastPriceLine: Boolean(chartSettings.lastPriceLine),
      ma5: maOn(5), ma10: maOn(10), ma25: maOn(25), ma60: maOn(60), ma200: maOn(200) },
    chartSizes: { main: chartSettings.paneSizes?.main ?? 58,
      kd: paneShare("kd"), macd: paneShare("macd") }, strategy: strategyUi, positionsBar,
    pinnedSymbols, aiActivityCard: (registered.aiActivityCard as "open" | "closed" | undefined) ?? null,
    positionsTab: surfaces.positionsTab, fillsSource: surfaces.fillsSource, strategyTab: surfaces.strategyTab, ordersHistory: surfaces.ordersHistory,
    // 报价总表 / 合约抽屉两级: reported only while the surface is on screen, so an older
    // Runtime (extra="forbid") accepts the whole heartbeat.
    ...(view === "board" ? { boardExchange: surfaces.boardExchange, boardSort: surfaces.boardSort, boardWatchlistCount: surfaces.boardWatchlistCount } : {}),
    ...(openDialog === "contract-drawer" ? { contractProduct: surfaces.contractProduct, contractCatalog: contractCatalogState(workspace) } : {}),
    ticketMode: (registered.ticketMode as "open" | "close" | "target" | undefined) ?? null, quickOrderConfirm: !surfaces.quickSkipConfirm, quickVolume: (registered.quickVolume as 1 | 2 | 5 | undefined) ?? 1,
    // 账户 tiles and the curve summary only from a Runtime that sends the curve (an older one forbids them).
    review: review ? { month: review.month, day: review.day, filter: review.filter, selectedTradeId: review.selectedTradeId,
      ...(review.currency !== undefined ? { currency: review.currency, currencies: review.currencies } : {}),
      ...(review.selectedNote !== undefined ? { selectedNote: review.selectedNote } : {}),
      ...(review.equityCurve !== undefined ? { tiles: review.tiles, equityCurve: review.equityCurve } : {}),
      // 今日复盘 (2026-10-06): only from a Runtime that answers the route (an older one forbids the field).
      ...(review.dailyReview !== undefined ? { dailyReview: review.dailyReview } : {}),
      // 习惯对照 (2026-10-25): only once the block has read GET /api/account/habits (an older Runtime forbids the field).
      ...(review.habits !== undefined ? { habits: review.habits } : {}) } : null,
    chatSessionId: surfaces.chatSessionId ?? null, chatHistory: surfaces.chatHistory ?? null,
    preferences: (preferences.document?.preferences as Record<string, unknown> | undefined) ?? null,
    // Reported only once known: an older Runtime (extra="forbid") must not reject the whole state.
    ...(tradingAccounts.currentId ? { tradingAccount: tradingAccounts.currentId } : {}),
    selectedPosition: surfaces.selectedPosition ? { symbol: surfaces.selectedPosition.symbol, direction: surfaces.selectedPosition.direction } : null,
    dialog: openDialog,
    strategyDetails: (registered.strategyDetails as Record<string, boolean> | undefined) ?? null,
    // 说人话: 详情's one sentence; only while the strategy panel reports it (an older Runtime forbids unknown fields).
    ...(registered.strategySummary !== undefined ? { strategySummary: registered.strategySummary as string | null } : {}),
    // The 上线 dialog and the 运行中 instances (执行方式 per instance); reported while the panel shows them.
    ...(registered.strategyLaunch ? { strategyLaunch: registered.strategyLaunch as Record<string, unknown> } : {}),
    ...(Object.keys(registered).some(key => key.startsWith("strategyInstance:"))
      ? { strategyInstances: Object.entries(registered).filter(([key]) => key.startsWith("strategyInstance:")).map(([, state]) => state as Record<string, unknown>) }
      : {}),
    ...(registered.strategyBacktest ? { strategyBacktest: registered.strategyBacktest as Record<string, unknown> } : {}),
    ...(registered.strategyTemplate ? { strategyTemplate: registered.strategyTemplate as Record<string, unknown> } : {}),
    ...(registered.strategyEditor ? { strategyEditor: registered.strategyEditor as Record<string, unknown> } : {}),
    ...(registered.strategyLab ? { strategyLab: registered.strategyLab as Record<string, unknown> } : {}),
    ...(registered.help ? { help: registered.help as Record<string, unknown> } : {}),
    // 设置 → 账户 → 实盘 (2026-10-08): off and why, only while that page shows (an older Runtime forbids the field).
    ...(registered.liveTrading ? { liveTrading: registered.liveTrading as { state: "off"; disabledReason: string } } : {}),
    // 提醒中心 (2026-10-15): open, filter and the counts, while the window has one (an older Runtime forbids the field).
    ...(registered.reminderCenter ? { reminderCenter: registered.reminderCenter as { open: boolean; filter: "unread" | "all"; unread: number; today: number; moreToday: boolean; moreAsked: number; moreDone: number; moreError: string; earlier: boolean } } : {}),
    // 设置 → AI 与记忆 (AI 审计 2026-10-13): memory on/off with each layer's count, and 我的流程, while the page shows.
    ...(registered.memory ? { memory: registered.memory as { enabled: boolean; counts: Record<string, number> } } : {}),
    ...(registered.myReminders ? { myReminders: registered.myReminders as Record<string, unknown> } : {}),
    ...(registered.routines ? { routines: registered.routines as { count: number; items: { id: string; name: string; steps: string[]; lastRunAt?: string | null }[] } } : {}),
    news: registered["news"] as Record<string, unknown> ?? null,
    reminders: reminders.shown ? { tab: reminders.tab } : null,
    // 多 AI 研讨: reported while a host is mounted (an older Runtime forbids the field).
    ...(registered.researchDebate ? { researchDebate: registered.researchDebate as Record<string, unknown> } : {}),
    monitorCards: registered["monitorCard:research"] !== undefined ? { research: registered["monitorCard:research"] as "expanded" | "collapsed" } : null,
    disclosures: disclosureStates(),
    selectedPrice: selectedPrice ?? null,
    ticketPrice: (registered.ticketPrice as { price?: number | null } | undefined)?.price ?? null,
    ticketPriceMode: (registered.ticketPrice as { priceMode?: "limit" | "opponent" | "last" } | undefined)?.priceMode ?? null,
    // Reported as plain records: the Runtime accepts the layer state as-is (a report, not a command).
    highlights: highlights as unknown as Record<string, unknown>[], chartAnnotations: chartAnnotations as unknown as Record<string, unknown>[], chartFocus: agentFocus,
    // 用户画线 (画线第二版): the chart stage registers its own report — the contract's drawings, the
    // toolbar, the armed tool, the selection, the switches and the open menu — only while it has
    // drawings (a guest window has none).
    ...(registered.chartDrawings ? { chartDrawings: registered.chartDrawings as Record<string, unknown> } : {}),
    // 清空图上标记 / 底栏自动隐藏 (2026-09-30): reported only when set, so an older Runtime
    // (extra="forbid") accepts the whole state.
    ...(marksCleared ? { marksCleared } : {}),
    ...(positionsBarAutoHidden ? { positionsBarAutoHidden: true } : {}),
    // 手机版: reported only while the phone layout shows, so an older Runtime accepts a desktop heartbeat.
    ...(mobile ? { mobileLayout: true, mobileTab: mobile.tab,
      mobilePage: mobile.page ?? (settingsOpen ? MOBILE_SETTINGS_PAGES[settingsPage] ?? null : dialogState("help") ? "help" : null),
      mobileNotifyFilter: mobile.notifyFilter, mobileUnread: mobile.unread,
      ...(mobile.notifyProduct ? { mobileNotifyProduct: mobile.notifyProduct } : {}),
      ...(mobile.tab === "strategy" ? { mobileStrategyTab: mobile.strategyTab } : {}),
      // 回测结果页 (2026-10-06): its comparison and the chosen variant, only while it shows.
      ...(mobile.page === "lab" && mobile.lab ? { mobileLab: mobile.lab } : {}),
      // Only while the guide shows (the environment it teaches), so other heartbeats stay as they were.
      ...(mobile.page === "install" ? { installEnvironment: mobile.installEnvironment } : {}),
      ...(mobile.page === "push" ? { pushSupport: mobile.push.support, pushSubscribed: mobile.push.subscribed } : {}) } : {}),
    tour: { active: tour.active, step: tour.step, total: tour.total, current: tour.current },
  };
  useGuestUiControl(workspace.guest, state, apply, mobile ? { mobileMarket: mobile.marketState } : undefined);
  useUiControl(connectedRuntimeUrl, workspace.workspaceScope, state, apply, {
    market: market ? { symbol: quote?.symbol, lastPrice: quote?.lastPrice, capturedAt: market.capturedAt,
      duration: market.durationSeconds, barCount: market.bars.length, lastBar: market.bars.at(-1),
      dataStatus: workspace.marketError ? "stale" : market.dataStatus,
      // 新浪备用: where a `backup` packet comes from (新浪（备用）); null for TqKq.
      source: market.source ?? null,
      isCached: Boolean(workspace.marketError || market.isCached), readError: workspace.marketError ?? null,
      marketSessionStatus: market.marketSessionStatus } : null,
    settings: { appConfig: appWorkbench.config, simulationLimits: workspace.simulationLimits,
      simulationLimitsStatus: workspace.simulationLimitsStatus, executionControls: workspace.aiExecution?.controls,
      preferencesVersion: preferences.document?.version ?? null, preferencesStatus: preferences.status || null,
      shortcuts, notificationPermission: typeof Notification === "undefined" ? "unsupported" : Notification.permission },
    // 按住说话 is the user's physical input (no command): its state and hold key are observable only.
    // micSilent: the live call is listening, unmuted, and its microphone has delivered no sound for ~4 s.
    voice: { ptt: ptt.state, pttShortcut: shortcuts.ptt,
      ...(micSilent ? { micSilent: true, ...(micLabel ? { micLabel } : {}), ...(micSuggestion ? { micSuggestion } : {}) } : {}) },
    connection: { mode: workspace.mode, status: workspace.connection, modelConnected: workspace.agentAccount?.authenticated,
      version: workspace.updater.info?.currentVersion, build: CLIENT_BUILD.commit, compat: compat.state },
    // 网页新版本 (web only): a newer deployed build (`available`) and whether the 新版本已就绪 bar shows;
    // the page reloads itself when idle, 刷新 / 检查更新 are the user's (no command).
    ...(CLIENT_BUILD.platform === "web" ? { webUpdate: { available: webUpdate.available, banner: webUpdate.banner } } : {}),
    // The new-version update window (desktop): open/version/phase for the snapshot; opening it is
    // `dialog: {name: "update-dialog"}` — downloading and installing stay user actions.
    update: { open: (registered["dialog:update-dialog"] as { open?: boolean } | undefined)?.open === true,
      version: workspace.updater.info?.version ?? null, phase: workspace.updater.phase },
    operationRouting: { simulationLimits: "/api/ai-execution/limits", appConfig: "/api/app/config",
      strategies: "/api/strategy-plans", fees: "/api/trading-costs",
      credentials: "用户直接输入，Agent不读取或代填", updates: "受保护核心，使用原更新流程",
      preferences: "/api/preferences（preference 命令或 update_preferences 工具）",
      shortcuts: "本机快捷键，用户在设置→快捷键录制；快照 settings.shortcuts 可见（ptt 为按住说话的按住键，KeyboardEvent.code）",
      pushToTalk: "按住说话是用户的物理输入，没有命令；状态在 context.voice.ptt（idle/recording/transcribing）", colors: "主题通过theme，涨跌颜色通过 preference chart.upDownColors，图表扩展颜色通过appConfig" }
  });
  // The same path a UI command takes: conversation cards' buttons (打开调研 / 打开下单台 …) use it.
  return { apply };
}

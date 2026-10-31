import { useEffect, useLayoutEffect, useRef, useSyncExternalStore } from "react";
import { createUiObserver } from "../uiObservation";
import { DISCLOSURE_ALIASES, SPARSE_DISCLOSURES } from "../uiSurfaces";
import { requestJson } from "../api";
import { logDesktop } from "../frontendLog";
import { preferenceValue } from "../userPreferences";
import { mobileMarketMatches, type MobileMarketChange } from "../components/mobile/mobileMarketPrefs";
import { feedLiveNow, onFeedNudge } from "../runtimeFeed";
import { useIdentityFence } from "./useIdentityFence";
import type { components } from "../workbench.generated";
import { agentConnectionInfo, captureNativeWindow, registerAgentWindow, forgetAgentWindow, type AgentDevice } from "../nativeAgent";

export type UiState = components["schemas"]["UiState"];
export type UiChanges = components["schemas"]["UiChanges"];
/** Server treats a window as offline after 75 s without a pulse; 30 s keeps it online with slack for one
 *  failed request. Commands and captures do not wait for it: feed.ui makes the window pulse at once. */
const IDLE_PULSE_MS = 30_000;
/** A drag (dock width, chart pane sizes) changes state on every pointer move; one pulse after it settles. */
const STATE_CHANGE_DEBOUNCE_MS = 250;
type Command = { commandId: string; expectedRevision: number; changes: UiChanges };
type Receipt = { commandId: string; status: "applied" | "rejected" | "unknown" };
/**
 * Whether the committed state already reflects a command. Most commands set the
 * state field of the same name; the round-eleven surface commands map onto the
 * state they change (a dialog name, a review selection, a pinned list …).
 */
/** A command whose work finished without doing it: rejected now, its reason in the snapshot — today only
 *  reminderCenter {more: true} whose read loaded nothing (state.reminderCenter.moreError, #932 R5). */
export function refused(state: unknown, changes: unknown): boolean {
  const more = (changes as { reminderCenter?: { more?: boolean } } | null)?.reminderCenter?.more;
  const center = (state as { reminderCenter?: Record<string, unknown> } | null)?.reminderCenter;
  return Boolean(more && center && Number(center.moreAsked) > 0 && center.moreDone === center.moreAsked && center.moreError);
}

export function matches(state: unknown, changes: unknown): boolean {
  if (!changes || typeof changes !== "object") return state === changes;
  if (!state || typeof state !== "object") return false;
  const current = state as Record<string, unknown>;
  const review = (current.review ?? {}) as Record<string, unknown>;
  return Object.entries(changes).every(([key, value]) => {
    if (key === "remoteSettings") {
      const remote = current.remoteSettings as Record<string, unknown> | undefined;
      if (!remote || !value || typeof value !== "object") return false;
      const patch = value as { enabled?: boolean; diagnostic?: boolean; disconnect?: boolean; resetBindings?: boolean; bindings?: Record<string, string> };
      return (patch.enabled === undefined || remote.enabled === patch.enabled)
        && (patch.diagnostic === undefined || remote.diagnostic === patch.diagnostic)
        && (!patch.disconnect || remote.phase === "idle")
        && (!patch.resetBindings || remote.bindingsDefault === true)
        && Object.entries(patch.bindings ?? {}).every(([button, action]) => (remote.bindings as Record<string, unknown> | undefined)?.[button] === action);
    }
    if (key === "selectedPrice") return current.selectedPrice === value && (value == null || (current.ticketPrice === value && current.ticketPriceMode === "limit"));
    if (key === "quickVolume") return current.quickVolume === value;
    if (key === "presentation") {
      const { reset, ...rest } = value as { reset?: boolean } & Record<string, unknown>;
      // 恢复默认布局 puts the dock back on the right with the bar collapsed. The width is not judged
      // (same as the Runtime): the reported width is the open panel's own, e.g. 590 for a wide panel.
      if (reset && !(current.dockSide === "right" && current.positionsBar === "collapsed")) return false;
      return matches(current, rest);
    }
    // 2026-10-08: the retired 实验室 tab opens the 结果页 (detail); an older client reports lab.
    if (key === "strategyTab") return (current.strategyTab === "lab" ? "detail" : current.strategyTab) === (value === "lab" ? "detail" : value);
    // 1.6.6: 持仓 is a side panel again (1.4.8–1.6.5 mapped it onto the bottom bar).
    if (key === "panel" && value === "positions") return current.panel === "positions";
    // 2026-10-07: the 价格阶梯 panel is gone; `panel: depth` opens the 下单面板.
    if (key === "panel" && value === "depth") return current.panel === "order";
    // 2026-10-25: the 辩论 page is the 调研面板's 辩论 tab (same rule as the Runtime's changes_match).
    if (key === "panel" && value === "debate") return current.panel === "news" && (current.news as { tab?: string } | null)?.tab === "debate";
    // Retired commands: the AI dock lost its inner page (1.6.6) and its flow filter (2026-09-25);
    // every value of either means "the 对话 panel is open".
    if (key === "aiPage" || key === "aiFilter") return current.panel === "ai";
    // `trades` (平仓记录) is the 成交 tab's old name.
    if (key === "positionsTab") return (current.positionsTab === "trades" ? "fills" : current.positionsTab) === (value === "trades" ? "fills" : value);
    if (key === "chartOption") {
      const option = value as { name: string; visible: boolean };
      return (current.chart as Record<string, boolean>)[option.name] === option.visible;
    }
    if (key === "pinnedSymbols") {
      const { add, remove } = value as { add?: string; remove?: string };
      const pinned = ((current.pinnedSymbols as string[] | null) ?? []).map(item => item.toUpperCase());
      return add ? pinned.includes(add.toUpperCase()) : !pinned.includes(String(remove).toUpperCase());
    }
    if (key === "reminderCenter" && (value as { more?: boolean }).more) {
      // 继续加载当天提醒 (#932 R5): applied once this command's read landed — done caught up with asked and no error
      // (mirrors the Runtime; a client before these fields reports none and its open center stands).
      const { more: _more, ...rest } = value as { more?: boolean } & Record<string, unknown>;
      const center = current.reminderCenter as Record<string, unknown> | undefined;
      if (center?.open !== true || !matches(center, rest)) return false;
      if (center.moreAsked === undefined) return true;
      return Number(center.moreAsked) > 0 && center.moreDone === center.moreAsked && !center.moreError;
    }
    if (key === "reviewExpand") {
      const { month, day } = value as { month?: string; day?: string };
      return month ? review.month === month : review.day === day;
    }
    // 2026-10-25: 设置 → 提醒 is the 提醒 panel's / page's 通用提醒 tab (same rule as the Runtime's changes_match).
    if (key === "settingsPage" && value === "notify") return current.settingsPage === "notify" || (current.reminders as { tab?: string } | null)?.tab === "general";
    if (key === "reminders") {
      const tab = (value as { tab?: string } | null)?.tab;
      return Boolean(current.reminders) && (!tab || (current.reminders as { tab?: string }).tab === tab);
    }
    if (key === "settingsPage") {
      // 1.6.5 page names (connection/appearance/simulation/fees) map onto the split pages —
      // fees lands on the trade-advanced second-level page (2026-09-30); an old client's
      // reported state maps the same way, so normalize both sides (mirrors the Runtime).
      const legacy: Record<string, string> = { connection: "account", appearance: "ui", simulation: "trade", fees: "trade-advanced" };
      const normalize = (page: unknown) => legacy[String(page)] ?? String(page);
      return normalize(current.settingsPage) === normalize(value);
    }
    if (key === "strategy" && (value as { tab?: string }).tab === "fees") {
      // 策略的“手续费” opens 设置 → 高级交易设置; the panel stays on 我的策略 (mirrors the Runtime, AI 审计 2026-10-13).
      return Boolean(current.settingsOpen) && ["fees", "trade-advanced"].includes(String(current.settingsPage));
    }
    if (key === "preference") {
      // Mirrors the Runtime: a client before the settings rows reports no document and its receipt stands.
      const { key: path, value: wanted } = value as { key: string; value: unknown };
      if (current.preferences == null) return true;
      return preferenceValue(current.preferences, path) === wanted;
    }
    if (key === "chatSession") {
      const { sessionId } = value as { sessionId?: string; new?: boolean };
      return sessionId ? current.chatSessionId === sessionId : true;
    }
    if (key === "reviewFilter") return review.filter === value;
    // 本站模拟合成一个账户 (2026-10-21): no field = an older client (mirrors the Runtime).
    if (key === "reviewCurrency") return !("currency" in review) || review.currency === value;
    if (key === "reviewSelect") return (review.selectedTradeId ?? null) === value;
    // 今日复盘: applied once the block shows that day. Sent from another view the review page is still loading
    // (no field yet) — reporting it applied then made the Runtime, which reads the missing field as null, refuse it.
    if (key === "reviewDay") return (review.dailyReview as { tradingDay?: string } | null | undefined)?.tradingDay === value;
    // 报价总表 / 合约抽屉两级 (2026-10-01): judged by the state the board view and the open
    // drawer report; a client not on the surface reports no field and its receipt stands.
    if (key === "boardExchange") return current.boardExchange == null || current.boardExchange === value;
    if (key === "boardSort") {
      const currentSort = current.boardSort as { key: string; order: string } | null | undefined;
      if (currentSort == null) return true;
      const wanted = value as { key: string; order: string };
      return currentSort.key === wanted.key && currentSort.order === wanted.order;
    }
    if (key === "contractProduct") return current.contractProduct === undefined || current.contractProduct === value;
    if (key === "contractCatalogRetry") {
      const catalog = current.contractCatalog as { status?: string } | null | undefined;
      return catalog == null || catalog.status !== "offline";
    }
    if (key === "dialog") {
      const { name, open } = value as { name: string; open: boolean };
      // 1.6.6 alias: the 持仓抽屉 is the 持仓 side panel now.
      if (name === "positions-drawer") return open ? current.panel === "positions" : current.panel !== "positions";
      // The retired 策略设计窗 (2026-10-08) opens and closes the 策略编辑卡: judged by the card.
      if (name === "strategy-designer" && typeof current.strategyEditor === "object" && current.strategyEditor !== null)
        return Boolean((current.strategyEditor as EditorCard).open) === open;
      return open ? current.dialog === name : current.dialog !== name;
    }
    if (key === "strategyDetails") {
      const { section, open } = value as { section: string; open: boolean };
      return ((current.strategyDetails as Record<string, boolean> | null) ?? {})[section] === open;
    }
    if (key === "news") {
      const news = (current.news as Record<string, unknown> | null) ?? {};
      const desk = (news.desk as { rejudge?: { status?: string } | null; highlight?: string | null } | null) ?? {};
      // 调研页 (第七批): deskRejudge = the page sent the refresh (its answer follows in rejudge.message);
      // deskSource = 资料来源 shows that fact highlighted. Same rule as the Runtime's _surface_matches.
      return current.panel === "news" && Object.entries(value as Record<string, unknown>).every(([name, wanted]) =>
        name === "deskRejudge" ? news.tab === "desk" && Boolean(desk.rejudge?.status)
          : name === "deskSource" ? news.tab === "desk" && desk.highlight === wanted
          : news[name] === wanted);
    }
    if (key === "researchDebate") {
      // 多 AI 研讨 (mirrors the Runtime's _debate_matches): close by the view gone, open by its id, start /
      // rerun once the view took the request (its result is the debate's own, later).
      const card = current.researchDebate as { open?: boolean; id?: string | null; request?: { kind?: string; status?: string; eventId?: string } | null } | undefined;
      const wanted = value as { start?: string; open?: string; rerun?: boolean; close?: boolean };
      if (wanted.close) return !card?.open;
      if (!card?.open) return false;
      if (wanted.open !== undefined) return card.id === wanted.open;
      const request = card.request ?? {};
      if (wanted.rerun) return request.kind === "rerun" && Boolean(request.status);
      return request.kind === "start" && Boolean(request.status) && (wanted.start === "desk" || wanted.start === "event" || request.eventId === wanted.start);
    }
    if (key === "monitorCard") {
      const { name, expanded } = value as { name: string; expanded: boolean };
      return ((current.monitorCards as Record<string, string> | null) ?? {})[name] === (expanded ? "expanded" : "collapsed");
    }
    if (key === "disclosure") {
      const { name, open, id } = value as { name: string; open: boolean; id?: string | null };
      const reported = (current.disclosures as Record<string, boolean> | null) ?? {};
      // 画线第二版: one entry per drawing / tool / menu, reported only while open; the old popover
      // name is the toolbar. The Runtime's _surface_matches judges them the same way.
      if (SPARSE_DISCLOSURES.has(name)) return (reported[`${name}:${id ?? ""}`] ?? false) === open;
      // An old name (画线 popover → toolbar, 查看完整规则 → 高级): the new surface reports it; an older client the old one.
      const alias = DISCLOSURE_ALIASES[name];
      if (alias) return (reported[id ? `${alias}:${id}` : alias] ?? reported[id ? `${name}:${id}` : name]) === open;
      return reported[id ? `${name}:${id}` : name] === open;
    }
    if (key === "strategyBacktest") {
      // 规则回测 card: judged by the parameters, marks and fold the card reports (mirrors the Runtime).
      const card = current.strategyBacktest as { running?: boolean; summary?: unknown; marked?: boolean; tradesOpen?: boolean;
        params?: { tradingDays?: number | null; slippageTicks?: number } } | undefined;
      if (!card) return false;
      const wanted = value as { tradingDays?: number; slippageTicks?: number; run?: boolean; mark?: boolean; trades?: boolean };
      return (wanted.tradingDays === undefined || card.params?.tradingDays === wanted.tradingDays)
        && (wanted.slippageTicks === undefined || card.params?.slippageTicks === wanted.slippageTicks)
        && (wanted.mark === undefined || card.marked === wanted.mark)
        && (wanted.trades === undefined || card.tradesOpen === wanted.trades)
        && (!wanted.run || Boolean(card.running || card.summary));
    }
    if (key === "strategyTemplate") {
      // 恢复模板默认 is the one key left (2026-10-08; the gallery is the 编辑卡's 从模板): judged by the 结果页's template source.
      const card = current.strategyTemplate as { detail?: { defaults?: boolean } | null } | undefined;
      if (!card) return false;
      return !(value as { restore?: boolean }).restore || Boolean(card.detail?.defaults);
    }
    if (key === "strategyEditor") return editorMatches(current.strategyEditor as EditorCard | undefined, value as EditorChange);
    if (key === "strategyLab") {
      // 试几组参数比一比 (mirrors the Runtime): open by the 结果页, run by a status past none.
      const card = current.strategyLab as { status?: string; labId?: string | null; rows?: { variantId: string }[] } | undefined;
      const wanted = value as { open?: boolean; run?: boolean; labId?: string; choose?: string };
      if (wanted.open && !["detail", "lab"].includes(String(current.strategyTab))) return false;
      if (!card) return !wanted.run && !wanted.choose && wanted.labId === undefined;
      if (wanted.labId !== undefined && card.labId !== wanted.labId) return false;
      if (wanted.run) return ["running", "done", "error"].includes(card.status ?? "");
      if (wanted.choose !== undefined) return (card.rows ?? []).some(row => row.variantId === wanted.choose);
      return true;
    }
    if (key === "help") {
      // 使用说明卡 (mirrors the Runtime): the open card's topic, search and folds.
      const card = current.help as { open?: boolean; topic?: string | null; group?: string | null; query?: string; detailOpen?: boolean; faqOpen?: number[] } | undefined;
      if (!card) return false;
      const wanted = value as { topic?: string; group?: string; query?: string; detail?: boolean; faq?: { index: number; open: boolean } };
      return Boolean(card.open)
        && (wanted.group === undefined || card.group === wanted.group)
        && (wanted.query === undefined || (card.query ?? "") === wanted.query)
        && (wanted.detail === undefined || Boolean(card.detailOpen) === wanted.detail)
        && (wanted.faq === undefined || (card.faqOpen ?? []).includes(wanted.faq.index) === wanted.faq.open);
    }
    if (key === "selectPosition") {
      const selected = current.selectedPosition as { symbol: string; direction: string } | null;
      if (value === null) return selected === null;
      const wanted = value as { symbol: string; direction: string };
      return Boolean(selected && selected.symbol.toUpperCase() === wanted.symbol.toUpperCase() && selected.direction === wanted.direction);
    }
    // AI annotation layer: judged by the highlights / drawings / focus the state reports.
    if (key === "highlight") {
      const { target, durationMs, clear } = value as { target?: string; durationMs?: number; clear?: boolean };
      const targets = ((current.highlights as { target: string }[] | null) ?? []).map(item => item.target);
      if (clear) return targets.length === 0;
      return durationMs === 0 ? !targets.includes(String(target)) : targets.includes(String(target));
    }
    if (key === "chartAnnotations") {
      const { op, items, symbol } = value as { op: string; items?: { id: string }[]; symbol?: string };
      if (symbol && current.symbol !== symbol) return true;  // drawn on another contract, not in this snapshot
      const ids = ((current.chartAnnotations as { id: string }[] | null) ?? []).map(item => item.id);
      if (op === "clear") return ids.length === 0;
      const wanted = (items ?? []).map(item => item.id);
      return wanted.every(id => ids.includes(id)) && (op === "add" || ids.every(id => wanted.includes(id)));
    }
    if (key === "chartMarks") {
      // Mirrors the Runtime: applied once the state reports the new clear stamp.
      return "marksCleared" in current ? Boolean(current.marksCleared) : true;
    }
    if (key === "tour") {
      const { op, steps } = value as { op: string; steps?: unknown[] };
      const tour = current.tour as { active: boolean; step: number; total: number } | undefined;
      if (!tour) return false;
      if (op === "start") return tour.active && tour.step === 1 && tour.total === (steps?.length ?? 0);
      if (op === "stop") return !tour.active;
      return true;
    }
    // The timeline's AI 简报 product is reported only while one is chosen (absent = every product).
    if (key === "mobileNotifyProduct") return (current.mobileNotifyProduct ?? null) === value;
    // 2026-10-15: the timeline's retired 提醒 filter opens the 提醒 page (mirrors the Runtime).
    if (key === "mobileNotifyFilter" && value === "reminder") return current.mobilePage === "reminders";
    // 2026-10-06: the retired 通知 tab opens AI 首页, which this window then reports.
    if (key === "mobileTab" && value === "notify") return current.mobileTab === "ai";
    // 手机看盘 (2026-10-09): judged by the 看盘 tab, the account document and the 副图设置 sheet (mirrors the Runtime).
    if (key === "mobileMarket") return mobileMarketMatches(current, value as MobileMarketChange);
    if (key === "chartFocus") {
      const focus = current.chartFocus as { from: number; to: number } | null;
      if (value === null) return focus === null;
      const wanted = value as { from: number; to: number };
      return Boolean(focus && focus.from === wanted.from && focus.to === wanted.to);
    }
    return matches(current[key], value);
  });
}

type EditorCard = { open?: boolean; strategyId?: string | null; start?: string; prefill?: { eligible?: boolean } | null; loading?: string | null; focus?: string | null;
  ask?: { text?: string; status?: string } | null; saving?: boolean; savedStrategyId?: string | null; error?: string | null; reminder?: boolean };
type EditorChange = { open?: boolean; new?: boolean; reminder?: boolean; edit?: string; start?: string; answers?: Record<string, string>; ask?: string; focus?: string;
  save?: boolean; close?: boolean };
/** 策略编辑卡 (2026-10-08): the same rule as the Runtime's `_editor_matches` (ui_control.py) — keep the two in step. */
export function editorMatches(card: EditorCard | undefined, value: EditorChange): boolean {
  if (card === undefined) return true;   // a client before the card: its receipt stands
  if (value.close) return !card.open;
  // A save closes the card on success; the saved strategy stays in the closed snapshot.
  if (value.save && card.savedStrategyId) return true;
  if (!card.open) return false;
  // new / edit / start / open land once the prefill or template is in the form (2026-10-08 线上验收: a command sent
  // right after `new` was refused as stale while the prefill still changed the window's state).
  if ((value.new || value.open || value.edit !== undefined || value.start !== undefined) && card.loading) return false;
  if (value.edit !== undefined && card.strategyId !== value.edit) return false;
  if (value.new && value.start === undefined && card.strategyId) return false;
  // 新建提醒 (第 6 批): the card opened in 提醒模式, not an ordinary new strategy.
  if (value.reminder && !card.reminder) return false;
  if (value.start !== undefined && card.start !== value.start && !(value.start === "habits" && card.prefill?.eligible === false)) return false;
  if (value.ask !== undefined && card.ask?.text !== value.ask) return false;
  if (value.answers !== undefined && !card.ask?.status) return false;
  // 流程图定位 (第二版): a card before it reports no focus, and its client's receipt stands.
  if (value.focus !== undefined && "focus" in card && card.focus !== value.focus) return false;
  if (value.save) return Boolean(card.saving || card.savedStrategyId || card.error);
  return true;
}

/** Whether this window's heartbeat is accepted; false after three consecutive 422s (shown in the workspace). */
export interface UiControlHealth { registered: boolean; detail: string }
let health: UiControlHealth = { registered: true, detail: "" };
const healthListeners = new Set<() => void>();
function setUiControlHealth(next: UiControlHealth) { health = next; healthListeners.forEach(listener => listener()); }
export function useUiControlHealth(): UiControlHealth {
  return useSyncExternalStore(listener => { healthListeners.add(listener); return () => { healthListeners.delete(listener); }; }, () => health, () => health);
}

/** A fresh consumer per authenticated workspace; receipt uses committed React state. */
export function useUiControl(baseUrl: string | undefined, workspace: string | undefined,
  state: UiState, apply: (changes: UiChanges) => void, context: Record<string, unknown>) {
  const fence = useIdentityFence([baseUrl, workspace]);
  const latest = useRef({ state, apply, revision: 0, baseUrl, workspace, context });
  // A state change (user action or applied command) pulses shortly after it
  // settles, so the Runtime's window registry never lags behind by the 10 s
  // idle heartbeat; a drag collapses into one pulse.
  const wakeRef = useRef<() => void>(() => undefined);
  const settleTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useLayoutEffect(() => {
    const previous = latest.current;
    const changed = JSON.stringify(previous.state) !== JSON.stringify(state);
    latest.current = { state, apply, baseUrl, workspace, context, revision: previous.revision + (changed ? 1 : 0) };
    if (changed) {
      clearTimeout(settleTimer.current);
      settleTimer.current = setTimeout(() => wakeRef.current(), STATE_CHANGE_DEBOUNCE_MS);
    }
  });
  useEffect(() => {
    if (baseUrl === undefined || !workspace) return;
    const windowId = crypto.randomUUID();
    const observation = createUiObserver();
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let device: AgentDevice | undefined;
    let captureReceipt: components["schemas"]["CaptureReceipt"] | undefined;
    let capturing: Promise<void> | undefined;
    let pending: { command: Command; started: number; rejected: boolean } | undefined;
    const current = () => !stopped && fence.isCurrent();
    let inFlight = false;
    let rejected = 0;
    const pulse = async () => {
      if (!current() || inFlight) return;
      inFlight = true;
      clearTimeout(timer);
      let receipt: Receipt | undefined;
      if (pending) {
        if (pending.rejected || refused(latest.current.state, pending.command.changes)) {
          receipt = { commandId: pending.command.commandId, status: "rejected" };
        }
        else if (matches(latest.current.state, pending.command.changes)) {
          receipt = { commandId: pending.command.commandId, status: "applied" };
        } else if (Date.now() - pending.started > 7000) {
          receipt = { commandId: pending.command.commandId, status: "unknown" };
        }
      }
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 3000);
      try {
        const result = await requestJson<{ command: Command | null }>(`${baseUrl}/api/ui/pulse`, {
          method: "POST", signal: controller.signal,
          // visible (锁屏推送, 2026-10-06): a device whose page is in front gets no lock-screen push.
          body: JSON.stringify({ windowId, revision: latest.current.revision, state: latest.current.state, receipt,
            device, captureReceipt, observation: observation.read(latest.current.context),
            visible: typeof document === "undefined" || document.visibilityState === "visible" })
        });
        if (!current()) return;
        if (rejected >= 3) setUiControlHealth({ registered: true, detail: "" });
        rejected = 0;
        registerAgentWindow(baseUrl, windowId);
        captureReceipt = undefined;
        if (receipt) pending = undefined;
        if (result.command && !pending) {
          const command = result.command;
          pending = { command, started: Date.now(), rejected: false };
          if (command.expectedRevision !== latest.current.revision) pending.rejected = true;
          else {
            try { latest.current.apply(command.changes); }
            catch { pending.rejected = true; }
          }
        }
        const capture = (result as components["schemas"]["UiPulseResult"]).capture;
        if (capture && !pending && !capturing && current()) {
          // The capture runs beside the heartbeat, never inside it: an occluded
          // WebView may not paint for a long time, and the window must stay
          // registered meanwhile. The receipt rides on a later pulse.
          capturing = captureNativeWindow(capture, () => latest.current.revision, () => latest.current.context)
            .then(receiptValue => { captureReceipt = receiptValue; }, () => undefined)
            .finally(() => { capturing = undefined; wake(); });
        }
      } catch (error) {
        const status = (error as { status?: number }).status;
        // Older clients/servers are supported without repeated failing requests.
        if (status === 401 || status === 403 || status === 404 || status === 410) stopped = true;
        // A Runtime that does not know a receipt field rejects the whole heartbeat; drop the
        // receipt rather than repeat the same 422 (the capture then expires server-side).
        if (status === 422) {
          captureReceipt = undefined;
          // Three in a row means the window is not registered at all: say so on screen and in the
          // desktop log instead of failing silently (2026-09-18: a client with v1 drawings did this for 40 min).
          rejected += 1;
          if (rejected === 3) {
            const detail = String((error as { detail?: unknown }).detail !== undefined ? JSON.stringify((error as { detail?: unknown }).detail) : (error as Error).message).slice(0, 2000);
            setUiControlHealth({ registered: false, detail });
            logDesktop("ui-pulse", "界面命令登记失败：/api/ui/pulse 连续 422", detail);
          }
        } else rejected = 0;
      } finally {
        clearTimeout(timeout);
        inFlight = false;
        // While the Runtime feed is live it nudges us (feed.ui) as soon as a command or
        // capture waits, so the idle heartbeat only has to keep the window registered.
        const soon = pending || captureReceipt || dirty;
        dirty = false;
        if (current()) timer = setTimeout(() => void pulse(), soon ? 16 : feedLiveNow() ? IDLE_PULSE_MS : 500);
      }
    };
    let dirty = false;
    const wake = () => {
      if (!current()) return;
      if (inFlight) { dirty = true; return; }  // carried by the pulse right after the current one
      void pulse();
    };
    wakeRef.current = wake;
    const stopNudge = onFeedNudge(wake);
    void agentConnectionInfo().then(info => {
      if (!current()) return;
      const { deviceId, platform, version, build, nativeCapture } = info;
      device = { deviceId, platform, version, build, nativeCapture };
    }).catch(() => {}).finally(() => { if (current()) void pulse(); });
    const stop = () => {
      observation.close();
      forgetAgentWindow(windowId);
      if (stopped) return;
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
      // Keep the authenticated close request alive across reload/navigation.
      // Omit document content to stay below the browser keepalive payload limit.
      void requestJson(`${baseUrl}/api/ui/pulse`, {
        method: "POST", keepalive: true,
        body: JSON.stringify({ windowId, revision: latest.current.revision, state: latest.current.state,
          closing: true, observation: { capturedAt: new Date().toISOString(),
            viewport: { width: window.innerWidth, height: window.innerHeight }, text: "",
            truncated: false, regions: [], controls: [], context: {} } })
      }).catch(() => undefined);
    };
    const pagehide = (event: PageTransitionEvent) => { if (!event.persisted) stop(); };
    window.addEventListener("pagehide", pagehide);
    // The page going to the background or coming back is reported at once, so a push is not held
    // back by a stale "visible" for the 30-second idle heartbeat.
    const visibility = () => wake();
    document.addEventListener("visibilitychange", visibility);
    return () => { window.removeEventListener("pagehide", pagehide); document.removeEventListener("visibilitychange", visibility);
      clearTimeout(settleTimer.current); wakeRef.current = () => undefined; stopNudge(); stop(); };
  }, [baseUrl, workspace, fence]);
}

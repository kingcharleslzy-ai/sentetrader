import { useEffect, useRef, useState } from "react";
import type { GuestAccountController } from "./useGuestAccount";
import type { UiChanges, UiState } from "./useUiControl";
import { GUEST_SETTINGS_PAGES, guestRequest } from "../guestApi";
import { observeUi } from "../uiObservation";
import { usePushToTalkStatus } from "../pushToTalk";
import { applyUiSurface, useUiSurfaceVersion } from "../uiSurfaces";

const PUBLIC_CHANGES = new Set("symbol duration theme newsOpen loginOpen kd macd volume news paneSizes researchExpanded panel settingsOpen settingsPage dockSide dockWidth fontScale density chartSizes chartOption pinnedSymbols dialog disclosure presentation mobileTab mobilePage mobileMarket contractCatalogRetry".split(" "));
/** 手机看盘 (2026-10-09): a visitor switches 报价 / 多图 / 单图 and the single chart's sub pane and sheet only. */
const PUBLIC_MARKET_KEYS = new Set(["view", "subPane", "subPanes"]);

/** The guest channel drives the normal UI command handler; private UI registration
 * stays off. Quote ticks never trigger HTTP observation or per-reader UI polling. `extra` joins the snapshot
 * (the phone's 看盘 state, which a member reports through the account preferences). */
export function useGuestUiControl(guest: GuestAccountController | undefined, state: UiState, apply: (changes: UiChanges) => void,
  extra?: Record<string, unknown>) {
  const enabled = Boolean(guest);
  const baseUrl = guest?.baseUrl, windowId = guest?.session.windowId, expired = guest?.expired;
  const ptt = usePushToTalkStatus();
  const surfaceVersion = useUiSurfaceVersion();
  const [contentVersion, setContentVersion] = useState(0);
  if (guest) guest.applyChanges.current = changes => {
    const disclosure = changes.disclosure;
    if (Object.keys(changes).some(key => !PUBLIC_CHANGES.has(key))
      || (changes.panel != null && changes.panel !== "news")
      || (changes.settingsPage != null && !GUEST_SETTINGS_PAGES.includes(changes.settingsPage))
      || (changes.dialog && !["contract-drawer", "market-session"].includes(changes.dialog.name))
      || ("disclosure" in changes && (!disclosure || typeof disclosure !== "object"
        || Object.keys(disclosure).length !== 2 || disclosure.name !== "feedback-diagnostics" || typeof disclosure.open !== "boolean"))
      // A visitor's only full-screen page is the 安装到桌面 guide.
      || ("mobilePage" in changes && changes.mobilePage != null && changes.mobilePage !== "install")
      || (changes.mobileMarket != null && Object.keys(changes.mobileMarket).some(key => !PUBLIC_MARKET_KEYS.has(key)))) {
      throw new Error("游客不可执行此界面操作");
    }
    const { loginOpen, newsOpen, paneSizes, researchExpanded, kd, macd, volume, ...normal } = changes;
    apply({ ...normal, ...(newsOpen !== undefined ? { panel: newsOpen ? "news" : null } : {}),
      ...(paneSizes ? { chartSizes: paneSizes } : {}) } as UiChanges);
    for (const [name, visible] of Object.entries({ kd, macd, volume })) {
      if (typeof visible === "boolean") apply({ chartOption: { name: name as "kd" | "macd" | "volume", visible } });
    }
    if (typeof researchExpanded === "boolean") {
      apply({ news: { ...changes.news, tab: "data" } });
      applyUiSurface("monitorCard:research", researchExpanded);
    }
    if (typeof loginOpen === "boolean") guest.setLoginOpen(loginOpen);
  };
  useEffect(() => {
    if (!enabled) return;
    const observer = new MutationObserver(records => {
      // Async news and dialogs matter; the frequently changing chart and quote ladder do not.
      if (records.some(record => {
        const element = record.target instanceof Element ? record.target : record.target.parentElement;
        return element?.closest(".fdw-side-workbench.dock-news, .news-panel, dialog[open], .fdw-contract-drawer");
      })) setContentVersion(value => value + 1);
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    return () => observer.disconnect();
  }, [enabled]);
  const snapshot = {
    surface: "workspace", role: "guest", symbol: state.symbol, duration: state.duration,
    theme: state.theme, panel: state.panel, settingsOpen: state.settingsOpen, settingsPage: state.settingsPage,
    disclosures: Object.fromEntries(["feedback-diagnostics", "mobile-subpanes"]
      .filter(name => typeof state.disclosures?.[name] === "boolean").map(name => [name, state.disclosures?.[name]])),
    fontScale: state.fontScale, density: state.density, dockSide: state.dockSide, dockWidth: state.dockWidth,
    chart: state.chart, chartSizes: state.chartSizes, pinnedSymbols: state.pinnedSymbols, dialog: state.dialog, contractCatalog: state.contractCatalog,
    news: state.news, monitorCards: state.monitorCards, newsOpen: state.panel === "news",
    // 手机版: only a phone-layout window reports these (ai / strategy / me show 登录后使用 to a visitor).
    mobileLayout: state.mobileLayout, mobileTab: state.mobileTab, mobilePage: state.mobilePage, installEnvironment: state.installEnvironment,
    kd: state.chart?.kd, macd: state.chart?.macd, volume: state.chart?.volume, paneSizes: state.chartSizes,
    loginOpen: guest?.loginOpen, appliedRevision: guest?.reportedRevision,
    // The limit the latest market frame carries (#734), so an administrator's change shows here too.
    marketHistoryTradingDays: guest?.market?.historyTradingDays ?? guest?.session.marketHistoryTradingDays,
    status: guest?.status, notice: guest?.notice, voiceState: ptt.state,
    ...extra,
  };
  const latest = useRef({ guest, snapshot });
  latest.current = { guest, snapshot };
  const key = JSON.stringify([snapshot, surfaceVersion, contentVersion, ptt.partial, ptt.notice]);
  useEffect(() => {
    if (!baseUrl || !windowId || expired) return;
    let stopped = false;
    const timer = setTimeout(() => {
      const current = latest.current;
      void guestRequest(baseUrl, "/api/guest/ui", { method: "POST", body: JSON.stringify({
        windowId, snapshot: { ...current.snapshot, observation: observeUi({ role: "guest" }) },
      }) }).catch(error => { if (!stopped && error.status === 401) current.guest?.reportExpired(); });
    }, 250);
    return () => { stopped = true; clearTimeout(timer); };
  }, [baseUrl, windowId, expired, key]);
}

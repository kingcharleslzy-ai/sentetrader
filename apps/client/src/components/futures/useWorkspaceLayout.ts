import { useEffect, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { useAccountPreferences } from "../../hooks/useAccountPreferences";
import { applyTheme, CHART_SETTINGS_STORAGE_KEY } from "../../theme";
import type { TradingChartSettings } from "../TradingCharts";
import { normalizeChartIndicators, type ChartPreferencesLike, type PaneSizes } from "../chartIndicators";
import type { FuturesDecisionWorkspaceProps } from "./props";
import { useStrategyEditor } from "../../strategyEditorStore";
import { clamp, defaultDockWidth, defaultEditorWidth, defaultStrategyWidth, LAYOUT_STORAGE_KEY, maxDockWidth, maxEditorWidth, maxSharedDockWidth, MIN_DOCK_WIDTH, readChartSettings, readSavedLayout, systemPrefersDark, WIDE_PANELS, type DockSide } from "./support";

/** The pane shares the old `chartSizes` shape names (kd / macd); a third pane keeps its own. */
const PANE_SHARE_DEFAULTS = [19, 23, 14];

/**
 * Dock side/width, chart settings and the resolved theme: persisted locally,
 * mirrored to the account preferences when connected, applied to the document.
 * `chartPreferences` (the settings document's chart section, a guest's merged view included) is
 * where the pane list and MA lines live since 图表指标可配置 (2026-10-02); this hook only derives
 * the presentation document's old switches from it.
 */
export function useWorkspaceLayout(workspace: FuturesDecisionWorkspaceProps, chartPreferences: ChartPreferencesLike) {
  const [savedLayout] = useState(readSavedLayout);
  const [chartSettings, setChartSettings] = useState<TradingChartSettings>(readChartSettings);
  const [systemDark, setSystemDark] = useState(systemPrefersDark);
  const [dockSide, setDockSide] = useState<DockSide>(savedLayout.dockSide);
  // The shared width (null = follow the window, see defaultDockWidth); a saved width is kept but never wider than half the window.
  const [savedDockWidth, setDockWidth] = useState<number | null>(savedLayout.dockWidth);
  const dockWidth = savedDockWidth === null ? defaultDockWidth() : Math.min(savedDockWidth, maxSharedDockWidth());
  // The wide reading panels (策略, 新闻) keep their own width (null = half the window); the rest share dockWidth.
  const [strategyWidth, setStrategyWidth] = useState<number | null>(savedLayout.strategyWidth);
  const [, setViewport] = useState(0);
  useEffect(() => {
    const update = () => setViewport(window.innerWidth);
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);
  // 策略编辑卡第二版 (2026-10-08): the strategy panel takes about three quarters while the card is open; a drag then
  // moves this window's editor width only (never saved), so leaving the card brings back the panel's own width.
  const editing = useStrategyEditor().open;
  const [editorWidth, setEditorWidth] = useState<number | null>(null);
  useEffect(() => { if (!editing) setEditorWidth(null); }, [editing]);
  const editorPanel = (panel: string | null) => editing && panel === "strategy";
  const panelWidth = (panel: string | null) => editorPanel(panel) ? Math.min(editorWidth ?? defaultEditorWidth(), maxEditorWidth())
    : panel && WIDE_PANELS.has(panel) ? Math.min(strategyWidth ?? defaultStrategyWidth(), maxDockWidth())
    : dockWidth;
  /** The widest the panel can be in this window: the strategy editor leaves the chart 300 px, 策略 / 新闻 480 px, the rest half the window. */
  const panelMaxWidth = (panel: string | null) => editorPanel(panel) ? maxEditorWidth()
    : panel && WIDE_PANELS.has(panel) ? maxDockWidth() : maxSharedDockWidth();
  const setPanelWidth = (panel: string | null, width: number) => {
    const next = clamp(Math.round(width), MIN_DOCK_WIDTH, panelMaxWidth(panel));
    if (editorPanel(panel)) setEditorWidth(next);
    else if (panel && WIDE_PANELS.has(panel)) setStrategyWidth(next);
    else setDockWidth(next);
  };
  const [positionsBar, setPositionsBar] = useState<"expanded" | "collapsed">("collapsed");
  const [recentSymbols, setRecentSymbols] = useState<string[]>([]);
  // Header contract tabs. An empty list pins the current contract so the bar is never blank.
  const [pinnedSymbols, setPinnedSymbols] = useState<string[]>([]);
  // Starting equity for the account P&L page; null keeps the Runtime default (1,000,000).
  const [initialBalance, setInitialBalance] = useState<number | null>(null);
  useEffect(() => {
    const symbol = workspace.selectedSymbol;
    if (!symbol || workspace.replayActive) return;
    setRecentSymbols(current => current[0] === symbol ? current : [symbol, ...current.filter(item => item !== symbol)].slice(0, 5));
    setPinnedSymbols(current => current.length ? current : [symbol]);
  }, [workspace.selectedSymbol, workspace.replayActive]);
  const pinSymbol = (symbol: string) => setPinnedSymbols(current =>
    current.some(item => item.toUpperCase() === symbol.toUpperCase()) ? current : [...current, symbol].slice(-8));
  const unpinSymbol = (symbol: string) => setPinnedSymbols(current => current.filter(item => item.toUpperCase() !== symbol.toUpperCase()));

  // The pane list / MA lines (settings document) as the presentation document's old switches.
  const indicators = normalizeChartIndicators(chartPreferences);
  const paneShare = (type: "kd" | "macd") => {
    const at = indicators.subPaneList.indexOf(type);
    return at >= 0 ? chartSettings.paneSizes?.panes[at] ?? PANE_SHARE_DEFAULTS[Math.min(at, 2)] : PANE_SHARE_DEFAULTS[type === "kd" ? 0 : 1];
  };
  const maOn = (period: number) => indicators.maLines.some(line => line.period === period && line.enabled);
  const preferenceStatus = useAccountPreferences(
    workspace.connection === "connected" ? workspace.preferencesBaseUrl : undefined, workspace.preferencesScope,
    { theme: chartSettings.theme, fontScale: workspace.fontScale, density: workspace.interfaceDensity, dockSide,
      chart: { kd: indicators.subPaneList.includes("kd"), macd: indicators.subPaneList.includes("macd"), volume: chartSettings.volume,
        crosshair: chartSettings.crosshair, lastPriceLine: Boolean(chartSettings.lastPriceLine),
        ma5: maOn(5), ma10: maOn(10), ma25: maOn(25), ma60: maOn(60), ma200: maOn(200) },
      chartSizes: { main: chartSettings.paneSizes?.main ?? 58, kd: paneShare("kd"), macd: paneShare("macd") },
      positionsBar, recentSymbols, pinnedSymbols, initialBalance },
    preference => {
      workspace.onFontScaleChange(preference.fontScale);
      workspace.onInterfaceDensityChange(preference.density);
      setDockSide(preference.dockSide);
      setPositionsBar(preference.positionsBar ?? "collapsed");
      setRecentSymbols(preference.recentSymbols ?? []);
      setPinnedSymbols(preference.pinnedSymbols?.length ? preference.pinnedSymbols : workspace.selectedSymbol ? [workspace.selectedSymbol] : []);
      setInitialBalance(preference.initialBalance ?? null);
      // The pane and MA switches stay owned by the settings document; only this hook's own
      // settings (volume / crosshair / latest-price line, pane heights, theme) come across.
      const sizes = preference.chartSizes;
      setChartSettings(current => ({
        theme: preference.theme,
        volume: preference.chart.volume,
        crosshair: preference.chart.crosshair,
        lastPriceLine: preference.chart.lastPriceLine,
        // chartSizes names kd / macd: each share lands back on the pane that owns it; a pane
        // the old shape cannot name keeps the share this machine already has.
        paneSizes: {
          main: clamp(sizes.main, 34, 76),
          panes: indicators.subPaneList.map((type, index) => type === "kd" ? clamp(sizes.kd, 12, 36)
            : type === "macd" ? clamp(sizes.macd, 12, 36)
            : current.paneSizes?.panes[index] ?? PANE_SHARE_DEFAULTS[Math.min(index, 2)])
        } satisfies PaneSizes
      }));
    });

  useEffect(() => {
    window.localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify({ dockSide, dockWidth: savedDockWidth, strategyWidth }));
  }, [dockSide, savedDockWidth, strategyWidth]);

  useEffect(() => {
    window.localStorage.setItem(CHART_SETTINGS_STORAGE_KEY, JSON.stringify(chartSettings));
  }, [chartSettings]);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const update = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    setSystemDark(query.matches);
    query.addEventListener?.("change", update);
    return () => query.removeEventListener?.("change", update);
  }, []);

  const resolvedTheme = chartSettings.theme === "system"
    ? systemDark ? "dark" : "light"
    : chartSettings.theme;
  useEffect(() => {
    applyTheme(resolvedTheme);
  }, [resolvedTheme]);

  const resetLayout = () => {
    setDockSide("right");
    setDockWidth(null);
    setStrategyWidth(null);
    setPositionsBar("collapsed");
  };
  const startDockResize = (event: ReactPointerEvent<HTMLButtonElement>, panel: string | null) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = panelWidth(panel);
    const onMove = (moveEvent: PointerEvent) => {
      const delta = moveEvent.clientX - startX;
      setPanelWidth(panel, startWidth + (dockSide === "left" ? delta : -delta));
    };
    const stop = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", stop, { once: true });
  };
  const resizeDockWithKeyboard = (event: ReactKeyboardEvent<HTMLButtonElement>, panel: string | null) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const separatorDelta = event.key === "ArrowRight" ? 16 : -16;
    setPanelWidth(panel, panelWidth(panel) + (dockSide === "left" ? separatorDelta : -separatorDelta));
  };

  return { chartSettings, setChartSettings, dockSide, setDockSide, dockWidth, setDockWidth, panelWidth, panelMaxWidth, setPanelWidth, editorPanel, positionsBar, setPositionsBar, recentSymbols, pinnedSymbols, pinSymbol, unpinSymbol, initialBalance, setInitialBalance,
    resolvedTheme, preferenceStatus, resetLayout, startDockResize, resizeDockWithKeyboard };
}

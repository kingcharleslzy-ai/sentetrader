import { CircleCheck, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { sameContract, type MarketWorkbenchSnapshot } from "../../api";
import { ALERTABLE_KINDS, snapToTick, toolSpec, type ChartDrawing } from "../../chartDrawingModel";
import type { ChartDrawingsHandle } from "../../hooks/useChartDrawings";
import type { PriceAlertsHandle } from "../../hooks/usePriceAlerts";
import { backtestMarksFor, clearChartMarks, fillVisibleAfterClear, useBacktestMarks, useChartAnnotations } from "../../aiAnnotations";
import { openAppHelp } from "../AppHelp";
import { guestHistoryNote } from "../../guestApi";
import { TradingCharts, type ChartPreferences, type ProtectionLine, type TradingChartSettings } from "../TradingCharts";
import { TimeshareChart } from "../TimeshareChart";
import type { ChartIndicatorPatch } from "../chartIndicatorEditor";
import { normalizeChartIndicators, type SubPaneType } from "../chartIndicators";
import type { useAppWorkbench } from "../AiActivity";
import { uiSurface, useDisclosureFamily } from "../../uiSurfaces";
import type { FuturesDecisionWorkspaceProps } from "./props";
import { formatNumber, formatSigned, isProtectionActive, ownPositions } from "./support";
import type { ChartFocus } from "./useChartFocus";
import { ChartContextMenu, menuId, menuSubs, type ChartMenuActions, type ChartMenuState, type MenuTarget } from "./ChartContextMenu";
import { DrawingToolbar } from "./DrawingToolbar";
import { DrawingStyleBar } from "./DrawingStyleBar";
import { DrawingSettingsDialog } from "./DrawingSettingsDialog";
import { ProtectionRepriceDialog, type PendingReprice } from "./ProtectionRepriceDialog";
import { useDrawingDesk, type DrawingPreferencePatch } from "./useDrawingDesk";
import { usePlaybooks } from "../playbooks/PlaybookPage";
import "./ChartDrawing.css";

export { snapToTick } from "../../chartDrawingModel";

interface Props {
  workspace: FuturesDecisionWorkspaceProps;
  market: MarketWorkbenchSnapshot;
  /** 分时图 (2026-10-02): which chart the stage shows — the workspace's own view state. */
  chartView: "kline" | "timeshare";
  decimals: number;
  chartSettings: TradingChartSettings;
  setChartSettings: Dispatch<SetStateAction<TradingChartSettings>>;
  /** 设置 → 图表 (account preferences): candle style, up/down colors, indicator parameters, AI annotation
   *  visibility; 画线第二版 also 磁吸 / 全部锁定 / 全部隐藏 and each tool's default style. */
  chartPreferences: ChartPreferences;
  /** 图表指标就地编辑 (2026-10-02): the chart labels' edits; undefined (a member window while
   *  disconnected) leaves the chart read-only. Guests edit locally. 画线第二版: the drawing switches
   *  and defaults travel the same chart preference path. */
  onChartIndicatorsChange?: (patch: ChartIndicatorPatch) => void;
  /** The preference save path's inline status for the chart label popovers. */
  chartIndicatorStatus?: string;
  annotationsVisible: boolean;
  resolvedTheme: "dark" | "light";
  appWorkbench: ReturnType<typeof useAppWorkbench>;
  focus: ChartFocus;
  selectedPrice: number | undefined;
  setSelectedPrice: (price: number | undefined) => void;
  /** 周期 ▸ in the right-click menu: the same switches as the period row. */
  onSelectDuration: (duration: MarketWorkbenchSnapshot["durationSeconds"]) => void;
  onSelectChartView: (view: "kline" | "timeshare") => void;
  /** 到价提醒 (2026-10-02): the workspace's handle; absent for a guest — no alert lines and
   *  no menu item there. A replay window (2026-10-03) has one too: its alerts are the replay
   *  child's own. */
  alerts?: PriceAlertsHandle;
  /** 用户画线 (2026-10-02): the workspace's handle; absent (a guest or disconnected) — no toolbar,
   *  no drawing layer and no drawing menu there. A replay window (2026-10-03) has one too: its
   *  lines are the replay child's own. */
  drawings?: ChartDrawingsHandle;
  /** 选中成交: opens the 持仓面板's 成交 tab. */
  openFills: () => void;
  openChat: () => void;
  setPrompt: (text: string) => void;
  /** 在这里设提醒 ▸ 按打法 / 按条件 (第 6 批): opens the editor card in 提醒模式; absent where it cannot (guest, phone). */
  onReminder?: (playbook: { name: string; entryRules: string } | null) => void;
}

export function ChartStage(props: Props) {
  return props.workspace.guest ? <ChartStageContent {...props} annotations={[]} backtestMarks={[]} alertLines={[]} protectionLines={[]} /> : <MemberChartStage {...props} />;
}

function MemberChartStage(props: Props) {
  const annotations = useChartAnnotations(props.market.quote?.symbol);
  const marks = backtestMarksFor(useBacktestMarks(), props.market.quote?.symbol, props.market.durationSeconds);
  // 到价提醒: the displayed contract's active alerts — a new array only when the list or the
  // contract actually changed, so ticks never redraw the lines. A 外盘主连 chart shows its product's month alerts
  // (外盘到价提醒挂在 MT5 月份合约上).
  const alertLines = useMemo(() => (props.alerts?.active ?? [])
    .filter(alert => !alert.drawingId && sameContract(alert.symbol, props.market.quote?.symbol))
    .map(alert => ({ id: alert.id, price: Number(alert.price) })), [props.alerts, props.market.quote?.symbol]);
  // 下单顺带保护: the displayed contract's armed stop / take rows, one dotted line per level.
  const repriceable = Boolean(props.workspace.onRepricePositionStop);
  const protectionLines = useMemo(() => {
    const rows: ProtectionLine[] = [];
    for (const stop of props.workspace.aiExecution?.stops ?? []) {
      if (!isProtectionActive(stop.status) || !sameContract(stop.symbol, props.market.quote?.symbol)) continue;
      // 图上拖线改价: only the user's own armed protection; a strategy's is changed in the strategy.
      const draggable = repriceable && stop.source === "manual" && stop.status === "ARMED";
      if (stop.triggerPrice !== null) rows.push({ id: `${stop.stopId}:stop`, kind: "stop", price: stop.triggerPrice, draggable });
      if (stop.takeProfitPrice !== null) rows.push({ id: `${stop.stopId}:take`, kind: "take", price: stop.takeProfitPrice, draggable });
    }
    return rows;
  }, [props.workspace.aiExecution, props.market.quote?.symbol, repriceable]);
  return <ChartStageContent {...props} annotations={annotations} backtestMarks={marks} alertLines={alertLines} protectionLines={protectionLines} />;
}

const STYLE_BAR_WIDTH = 420;

/** The chart, its drawing toolbar (shown only while drawing) and style bar, and the chart-only right-click menu (画线第二版). */
function ChartStageContent({ workspace, market, chartView, decimals, chartSettings, setChartSettings, chartPreferences, onChartIndicatorsChange, chartIndicatorStatus, annotationsVisible, resolvedTheme, appWorkbench, focus,
  selectedPrice, setSelectedPrice, onSelectDuration, onSelectChartView, openFills, openChat, setPrompt, onReminder, alerts, drawings, annotations, backtestMarks, alertLines, protectionLines }: Props & { annotations: ReturnType<typeof useChartAnnotations>; backtestMarks: ReturnType<typeof backtestMarksFor>; alertLines: Array<{ id: string; price: number }>; protectionLines: ProtectionLine[] }) {
  const quote = market.quote;
  const tick = quote?.priceTick;
  const { tradeFocus, selectedTrades, setSelectedTrades, setChartFocusMessage, clearChartFocus } = focus;
  const allowed = (key: string) => workspace.access?.require(key) ?? true;
  const bodyRef = useRef<HTMLDivElement>(null);
  const [menu, setMenu] = useState<ChartMenuState>();
  // 在这里设提醒 ▸ 按打法: the 打法本 is read once a menu opens, not with every chart.
  const playbooks = usePlaybooks(menu && onReminder ? workspace.runtimeBaseUrl : undefined);
  const [stageNotice, setStageNotice] = useState("");
  const reprice = useProtectionDrag(workspace, quote?.lastPrice, quote?.volumeMultiple, decimals, setStageNotice);
  const shownProtection = useMemo(() => reprice.pending
    ? [...protectionLines, { id: "reprice:pending", kind: reprice.pending.level, price: reprice.pending.to, pending: true }] : protectionLines,
  [protectionLines, reprice.pending]);
  useEffect(() => { if (!stageNotice) return; const timer = window.setTimeout(() => setStageNotice(""), 5000); return () => window.clearTimeout(timer); }, [stageNotice]);
  const period = chartView === "timeshare" ? 0 : market.durationSeconds;
  const savePreferences = useMemo(() => onChartIndicatorsChange
    ? (patch: DrawingPreferencePatch) => onChartIndicatorsChange(patch as ChartIndicatorPatch) : undefined, [onChartIndicatorsChange]);
  const desk = useDrawingDesk({ drawings, symbol: quote?.symbol, period, tick, preferences: chartPreferences, savePreferences, contextMenu: menuId(menu) });
  const indicators = useMemo(() => normalizeChartIndicators(chartPreferences), [chartPreferences]);
  // 碰到这条线就提醒: the drawings an active alert watches — they wear a 提醒 tag instead of a price line of their own.
  const alertedDrawings = useMemo(() => new Set((alerts?.active ?? []).flatMap(alert => alert.drawingId ? [alert.drawingId] : [])), [alerts]);
  const shownDrawings = useMemo(() => desk.shown.map(item => alertedDrawings.has(item.id) ? { ...item, alerted: true } : item), [desk.shown, alertedDrawings]);

  // The menu closes on a click elsewhere, Esc, and a contract / period / view change.
  useEffect(() => { setMenu(undefined); }, [quote?.symbol, period, workspace.runtimeBaseUrl]);
  useEffect(() => {
    if (!menu) return;
    const outside = (event: PointerEvent) => { if (!(event.target as Element).closest?.(".fdw-context-menu")) setMenu(undefined); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setMenu(undefined); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [menu]);
  const openMenu = useCallback((target: MenuTarget, x: number, y: number) => {
    if (target.area === "drawing") desk.select(target.drawing.id);
    setMenu({ target, x, y, sub: null });
  }, [desk]);
  // 右键菜单 as a UI command: the same three menus (and their submenus), opened over their own area.
  useDisclosureFamily("chart-context-menu", { open: menu ? [menuId(menu)!] : [], apply: (id, open) => {
    // One menu at a time: closing any of them closes the one that is open.
    if (!open) { setMenu(undefined); return; }
    const [base, sub = null] = id.split("/");
    const stage = bodyRef.current?.getBoundingClientRect();
    let target: MenuTarget;
    let at = { x: (stage?.left ?? 200) + (stage?.width ?? 400) / 2, y: (stage?.top ?? 120) + 60 };
    if (base === "main") {
      if (!quote) throw new Error("当前没有合约图表");
      target = { area: "main", price: snapToTick(Number(quote.lastPrice), tick) };
    } else if (base.startsWith("drawing:")) {
      const drawing = desk.items.find(item => item.id === base.slice("drawing:".length));
      if (!drawing) throw new Error("本合约没有这条线（state.chartDrawings.items 里是本合约的线）");
      target = { area: "drawing", drawing, price: snapToTick(Number(quote?.lastPrice), tick) };
    } else if (base.startsWith("pane:")) {
      const pane = base.slice("pane:".length) as SubPaneType;
      if (chartView !== "kline" || !indicators.subPaneList.includes(pane)) throw new Error("这个副图没有打开");
      const rect = document.querySelector(`[data-region="chart.${pane}"]`)?.getBoundingClientRect();
      if (rect) at = { x: rect.left + rect.width / 2, y: rect.top + 12 };
      target = { area: "pane", pane };
    } else throw new Error("菜单 id 是 main、drawing:<线 id> 或 pane:<kd|macd|rsi|oi>");
    if (sub && !menuSubs(target, chartView).includes(sub)) throw new Error(`这个菜单没有子菜单 ${sub}`);
    if (target.area === "drawing" && !drawings) throw new Error("该界面当前不可用");
    if (target.area === "drawing") desk.select(target.drawing.id);
    setMenu({ target, x: at.x, y: at.y, sub });
  } });

  // 清空图上标记 is offered while anything is drawn: AI annotations, backtest marks or trade markers
  // (the ones not already retired by an earlier clear on this contract).
  const visibleTradeMarkers = (!workspace.guest && appWorkbench.config.showTradeMarkers ? appWorkbench.activity?.fills ?? [] : [])
    .filter(fill => fillVisibleAfterClear(quote?.symbol, fill.tradeDateTime));
  const hasChartMarks = annotations.length > 0 || backtestMarks.length > 0 || visibleTradeMarkers.length > 0;
  // "让 AI 讲讲这一段": the visible span (bar timestamps in seconds) becomes the question's context.
  const askAboutVisible = () => {
    if (!allowed("ai-chat")) return;
    const stage = document.querySelector<HTMLElement>("[data-agent-chart-range]");
    let range: { from?: number; to?: number } | null;
    try { range = JSON.parse(stage?.dataset.agentChartRange ?? "null"); } catch { range = null; }
    const format = (seconds: number) => new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(seconds * 1000));
    const span = range?.from && range?.to ? `${format(range.from)} 到 ${format(range.to)}（时间戳 ${range.from}–${range.to}）` : "当前可见区间";
    openChat();
    setPrompt(`讲讲 ${quote?.symbol ?? "当前合约"} ${chartView === "timeshare" ? "分时" : durationLabel(market.durationSeconds)} 图上 ${span} 这段走势：先在图上画出关键位置再解释，不创建订单。`);
  };
  const changeChart = (patch: ChartIndicatorPatch) => {
    if (!onChartIndicatorsChange) { setStageNotice("连接后可设置"); return; }
    try { onChartIndicatorsChange(patch); } catch (error) { setStageNotice(error instanceof Error ? error.message : "连接后可设置"); }
  };
  const openSurface = (key: string) => { try { uiSurface(key)?.apply?.(true); } catch { /* The popover reports its own state. */ } };
  // 这条线被碰到时提醒我 (改版 2026-10-31): the alert watches the drawing itself (the Runtime follows a sloped line and
  // drops the alert with the line); a second press cancels it.
  const lineAlert = (drawing: ChartDrawing) => {
    if (!alerts || !ALERTABLE_KINDS.has(drawing.kind)) return;
    const existing = alerts.active.find(alert => alert.drawingId === drawing.id);
    if (existing) alerts.cancel(existing.id);
    else void alerts.create(drawing.symbol, 0, undefined, drawing.id);
  };
  const menuActions: ChartMenuActions = {
    chartView, duration: market.durationSeconds, decimals, tick,
    indicators: chartView === "kline" && onChartIndicatorsChange ? indicators : null,
    drawingsAvailable: desk.available,
    // #712 B6: a member window without the drawings handle is disconnected; 画线 stays in the menu, greyed with why.
    drawingsBlocked: !desk.available && !workspace.guest && !drawings ? "应用服务未连接，连上后可画线" : undefined,
    hiddenAll: desk.hidden, hiddenCount: desk.hiddenCount, locked: desk.locked, magnet: desk.magnet,
    drawingCount: desk.items.length, hasChartMarks,
    onAlert: alerts && quote ? price => { void alerts.create(quote.symbol, price); } : undefined,
    onReminder, playbooks: playbooks.items,
    onTool: kind => desk.setTool(kind),
    onGroup: group => desk.openGroupList(group),
    onPeriod: value => { if (value === "timeshare") onSelectChartView("timeshare"); else onSelectDuration(value as MarketWorkbenchSnapshot["durationSeconds"]); },
    onIndicatorToggle: which => changeChart(which === "ma"
      ? { maLines: indicators.maLines.map(line => ({ ...line, enabled: !indicators.maLines.some(item => item.enabled) })) }
      : { boll: { ...indicators.boll, enabled: !indicators.boll.enabled } }),
    onIndicatorSettings: () => openSurface("disclosure:chart-ma-editor"),
    onAddPane: type => { if (!indicators.subPaneList.includes(type)) changeChart({ subPaneList: [...indicators.subPaneList, type] }); },
    onChartSettings: () => window.dispatchEvent(new CustomEvent("ai-trader-open-settings", { detail: "chart" })),
    onAskPrice: price => { if (!allowed("ai-chat")) return; openChat(); setPrompt(`分析价格 ${formatNumber(price, decimals)} 附近的机会与风险，不创建订单。`); },
    onAskSpan: askAboutVisible,
    onDrawingSwitch: patch => { try { desk.savePreference(patch); } catch (error) { setStageNotice(error instanceof Error ? error.message : "连接后可设置"); } },
    onClearDrawings: desk.askClear,
    onShowHidden: desk.showHidden,
    onClearMarks: () => { if (quote) clearChartMarks(quote.symbol); },
    onPatch: (drawing, patch) => { void desk.patch(drawing.id, patch); },
    onSettings: drawing => desk.setSettingsId(drawing.id),
    onLineAlert: lineAlert,
    lineAlerted: drawing => alertedDrawings.has(drawing.id),
    onDuplicate: desk.duplicate,
    onDelete: drawing => desk.remove(drawing.id),
    onSwapPane: (from, to) => changeChart({ subPaneList: indicators.subPaneList.map(item => item === from ? to : item) }),
    onPaneSettings: type => openSurface(`disclosure:chart-pane-editor:${type}`),
    onMovePane: (type, direction) => {
      const list = [...indicators.subPaneList];
      const at = list.indexOf(type), to = at + direction;
      if (at < 0 || to < 0 || to >= list.length) return;
      [list[at], list[to]] = [list[to], list[at]];
      changeChart({ subPaneList: list });
    },
    onClosePane: type => changeChart({ subPaneList: indicators.subPaneList.filter(item => item !== type) }),
  };

  const priceContext = (price: number, x: number, y: number, drawing?: ChartDrawing) => {
    if (!quote) return;
    openMenu(drawing && desk.available ? { area: "drawing", drawing, price } : { area: "main", price }, x, y);
  };
  const paneContext = (pane: SubPaneType, x: number, y: number) => openMenu({ area: "pane", pane }, x, y);
  const chartDrawingProps = {
    userDrawings: desk.available ? shownDrawings : [],
    drawingTool: desk.available ? desk.tool : null,
    selectedDrawingId: desk.selected?.id ?? null,
    drawingsLocked: desk.locked,
    drawingMagnet: desk.magnet,
    onPlaceDrawing: desk.place,
    onMoveDrawing: desk.move,
    onDrawingSelect: desk.select,
    selectedDrawingHandle: desk.handle,
  };

  // The style bar sits above where the drawing was clicked (top-centre when the AI selected it).
  const stylePosition = (() => {
    const rect = bodyRef.current?.getBoundingClientRect();
    const width = rect?.width ?? 800;
    const at = desk.selectedAt;
    const left = at && rect ? at.clientX - rect.left - STYLE_BAR_WIDTH / 2 : (width - STYLE_BAR_WIDTH) / 2;
    const above = at && rect ? at.clientY - rect.top - 56 : 34;
    return { left: Math.max(8, Math.min(left, width - STYLE_BAR_WIDTH - 8)), top: above < 30 && at && rect ? at.clientY - rect.top + 18 : Math.max(30, above) };
  })();
  const selected = desk.selected;
  const settingsDrawing = desk.items.find(item => item.id === desk.settingsId);
  const alertBlocked = !selected ? null : !alerts ? "这里没有到价提醒" : !ALERTABLE_KINDS.has(selected.kind) ? "形状和标注不是价位，不能设提醒" : null;
  const toolHint = desk.tool ? `${toolSpec(desk.tool).label}：${toolSpec(desk.tool).hint}` : "";

  return (
    <div className="fdw-chart-stage" onContextMenu={event => event.preventDefault()}>
      <div className="fdw-chart-body" ref={bodyRef} data-drawing-tool={desk.tool ?? undefined}>
        {desk.available && desk.toolbarOpen ? <DrawingToolbar desk={desk} onSwitchError={setStageNotice} /> : null}
        {(tradeFocus?.direction === "event" || tradeFocus?.direction === "agent") && <button type="button" className="fdw-chart-return" onClick={clearChartFocus}>返回原图</button>}
        {chartView === "timeshare"
          ? <TimeshareChart bars={market.bars} schedule={quote?.tradingSchedule} preSettlement={quote?.preSettlement} priceTick={quote?.priceTick} settings={chartSettings} resolvedTheme={resolvedTheme} symbol={quote?.symbol}
              onHelp={() => { if (allowed("help")) openAppHelp("chart"); }} onPriceContext={priceContext} {...chartDrawingProps} />
          : <TradingCharts historyRestricted={Boolean(workspace.guest)} onHistoryRestricted={() => { allowed("market-history"); }}
              historyNote={workspace.guest ? guestHistoryNote(market.historyTradingDays) : undefined} onHelp={() => { if (allowed("help")) openAppHelp("chart"); }}
              onTradeSelect={ids => { if (!allowed("account")) return; setSelectedTrades(ids); openFills(); }} onFocusResult={setChartFocusMessage}
              selectedTradeIds={workspace.guest ? [] : selectedTrades} tradeFills={!workspace.guest && appWorkbench.config.showTradeMarkers ? appWorkbench.activity?.fills : []}
              extensions={workspace.guest ? [] : appWorkbench.config.extensions} durationSeconds={market.durationSeconds} tradeFocus={workspace.guest ? undefined : tradeFocus}
              annotations={annotationsVisible ? annotations : []} backtestMarks={backtestMarks} bars={market.bars} settings={chartSettings} preferences={chartPreferences}
              resolvedTheme={resolvedTheme} timeZone="Asia/Shanghai" selectedPrice={selectedPrice} priceTick={quote?.priceTick} symbol={quote?.symbol}
              onPriceContext={priceContext} onPaneContext={paneContext} onSettingsChange={setChartSettings} onIndicatorsChange={onChartIndicatorsChange} indicatorStatus={chartIndicatorStatus}
              alertLines={alertLines} protectionLines={shownProtection} onProtectionDrag={reprice.drag} {...chartDrawingProps} />}
        {reprice.dragging ? (
          <div className={`fdw-draw-hint${reprice.dragging.issue ? " is-blocked" : ""}`} role="status" data-agent-target="chart:protection-drag">
            <span>{reprice.dragging.word} {formatNumber(reprice.dragging.from, decimals)} → {formatNumber(reprice.dragging.to, decimals)}
              {reprice.dragging.pnl !== undefined ? ` · 触发约 ${formatSigned(reprice.dragging.pnl)} 元` : null}</span>
            {reprice.dragging.issue ? <span>{reprice.dragging.issue}，松手会弹回</span> : <span>松手后确认 · Esc 取消</span>}
          </div>
        ) : null}
        <ProtectionRepriceDialog pending={reprice.pending} decimals={decimals} busy={reprice.busy} onClose={reprice.cancel} onConfirm={reprice.confirm} />
        {toolHint || desk.measure ? (
          <div className="fdw-draw-hint" role="status" data-agent-target="chart:drawing-hint">
            {toolHint ? <span>{toolHint} · Esc 取消</span> : null}
            {desk.measure && !desk.tool ? <><span>测量结果不保存</span>
              <button type="button" className="app-button" onClick={desk.keepMeasure}>保留</button>
              <button type="button" className="app-button" onClick={desk.clearMeasure}>清除</button></> : null}
          </div>
        ) : null}
        {desk.notice || drawings?.createError || stageNotice ? <small className="fdw-draw-notice" role="alert">{stageNotice || desk.notice || drawings?.createError}</small> : null}
        {selected && !desk.tool && !settingsDrawing && !menu ? (
          <DrawingStyleBar drawing={selected} position={stylePosition} onPatch={patch => { void desk.patch(selected.id, patch); }}
            onSettings={() => desk.setSettingsId(selected.id)} onAlert={() => lineAlert(selected)} alertBlocked={alertBlocked} alerted={alertedDrawings.has(selected.id)}
            onDelete={() => desk.remove(selected.id)} />
        ) : null}
        {settingsDrawing ? (
          <DrawingSettingsDialog key={settingsDrawing.id} drawing={settingsDrawing} tick={tick} period={period} onClose={() => desk.setSettingsId(null)}
            onSave={(patch, defaults) => desk.saveSettings(settingsDrawing, patch, defaults)} />
        ) : null}
      </div>
      {selectedPrice !== undefined ? <div className="fdw-selected-price"><CircleCheck />候选价 {formatNumber(selectedPrice, decimals)}<button type="button" aria-label="清除候选价" onClick={() => setSelectedPrice(undefined)}><X /></button></div> : null}
      {menu ? <ChartContextMenu menu={menu} actions={menuActions} onSub={sub => setMenu(current => current ? { ...current, sub } : current)} onClose={() => setMenu(undefined)} /> : null}
    </div>
  );
}

const DURATION_LABELS: Record<number, string> = { 60: "1 分钟", 180: "3 分钟", 300: "5 分钟", 900: "15 分钟", 1800: "30 分钟", 3600: "1 小时", 86400: "日线", 604800: "周线" };
function durationLabel(seconds: number): string { return DURATION_LABELS[seconds] ?? `${seconds} 秒`; }

/** 图上拖线改价 (2026-10-24): a dragged protection line becomes a confirm card; the same side rule as the 持仓面板
 *  (多仓 止损<现价<止盈, 空仓相反) refuses a level past the market before any card. */
function useProtectionDrag(workspace: FuturesDecisionWorkspaceProps, last: number | null | undefined, multiple: number | null | undefined,
  decimals: number, notice: (text: string) => void) {
  const [dragging, setDragging] = useState<{ word: string; from: number; to: number; pnl?: number; issue?: string }>();
  const [pending, setPending] = useState<PendingReprice>();
  const [busy, setBusy] = useState(false);
  const stops = workspace.aiExecution?.stops;
  const positions = workspace.account?.positions;
  const drag = useCallback((id: string, price: number | null, done: boolean) => {
    const [stopId, level] = id.split(":") as [string, "stop" | "take"];
    const stop = stops?.find(item => item.stopId === stopId);
    const from = level === "stop" ? stop?.triggerPrice : stop?.takeProfitPrice;
    if (!stop || from == null) { setDragging(undefined); return; }
    const word = level === "stop" ? "止损" : "止盈";
    const below = (level === "stop") === (stop.positionDirection === "long");
    const issue = price === null || last == null || !Number.isFinite(last) ? undefined
      : below ? (price < last ? undefined : `${word}要低于现价 ${formatNumber(last, decimals)}`)
        : (price > last ? undefined : `${word}要高于现价 ${formatNumber(last, decimals)}`);
    // 触发时盈亏 against the position's open price; own (RMB) positions only — a 外盘 USD row has another currency.
    const open = ownPositions(positions ?? []).find(item => sameContract(item.symbol, stop.symbol) && item.direction === stop.positionDirection)?.openPrice;
    const pnl = price === null || open == null || !Number.isFinite(open) || !multiple ? undefined
      : (price - open) * multiple * stop.volume * (stop.positionDirection === "long" ? 1 : -1);
    if (!done) { if (price !== null) setDragging({ word, from, to: price, pnl, issue }); return; }
    setDragging(undefined);
    if (price === null || price === from) return;
    if (issue) { notice(issue); return; }
    setPending({ stopId, level, from, to: price, volume: stop.volume, priceIntent: stop.priceIntent, pnl });
  }, [stops, positions, last, multiple, decimals, notice]);
  const confirm = useCallback(async (target: PendingReprice) => {
    setBusy(true);
    try {
      await workspace.onRepricePositionStop?.(target.stopId, { level: target.level, previous: target.from, price: target.to });
    } finally {
      setBusy(false);
      setPending(undefined);
    }
  }, [workspace]);
  return { dragging, pending, busy, drag, confirm, cancel: () => setPending(undefined) };
}


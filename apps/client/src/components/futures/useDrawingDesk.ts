import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DRAWING_GROUPS, DRAWING_TOOLS, anchorsOf, duplicateAnchors, isDrawingGroup, isDrawingKind, placementPoints, resolveStyle, shiftAnchors, toolSpec, visibleOnPeriod,
  type Anchor, type ChartDrawing, type DrawingDefaultStyle, type DrawingDefaults, type DrawingGroup, type DrawingKind, type DrawingStyle
} from "../../chartDrawingModel";
import type { ChartDrawingsHandle, DrawingPatch } from "../../hooks/useChartDrawings";
import { useDisclosureFamily, useDisclosureSurface, useUiSurface } from "../../uiSurfaces";

export type { DrawingDefaultStyle } from "../../chartDrawingModel";
export interface DrawingPreferencePatch {
  drawingMagnet?: boolean; drawingsLocked?: boolean; drawingsHidden?: boolean;
  drawingDefaults?: DrawingDefaults;
}

const LAST_TOOLS_KEY = "ai-trader:drawing-last-tools";
/** The 测量 result: drawn until the next click, Esc or another tool — never saved unless 保留. */
export const MEASURE_ID = "measure:current";

function readLastTools(): Record<DrawingGroup, DrawingKind> {
  const first = Object.fromEntries(DRAWING_GROUPS.map(group => [group.id, DRAWING_TOOLS.find(spec => spec.group === group.id)!.kind])) as Record<DrawingGroup, DrawingKind>;
  try {
    const stored = JSON.parse(localStorage.getItem(LAST_TOOLS_KEY) ?? "{}") as Record<string, unknown>;
    for (const group of DRAWING_GROUPS) {
      const kind = stored[group.id];
      if (isDrawingKind(kind) && toolSpec(kind).group === group.id) first[group.id] = kind;
    }
  } catch { /* Local convenience only. */ }
  return first;
}

interface Input {
  /** Absent for a guest or a disconnected window: no toolbar, no layer, no surfaces. */
  drawings?: ChartDrawingsHandle;
  symbol?: string;
  /** The chart shown: K 线 seconds, 0 = 分时 (在所有周期显示 off pins a drawing to one). */
  period: number;
  tick?: number | null;
  preferences: { drawingMagnet?: boolean; drawingsLocked?: boolean; drawingsHidden?: boolean; drawingDefaults?: DrawingDefaults };
  /** The account preference write (a guest's or a disconnected window's throws "连接后可设置"). */
  savePreferences?: (patch: DrawingPreferencePatch) => void;
  /** The right-click menu open now (`main`, `drawing:<id>`, `pane:<type>`, with "/<submenu>"), for the snapshot. */
  contextMenu: string | null;
  /** 手机画线: `chart-drawing-toolbar` is the ✎ tool sheet, which closes once a tool is picked; the armed tool
   *  does not open or close it. */
  mobile?: boolean;
}

/** Why ±1 跳 cannot move the selected drawing now (null: it can). */
export function nudgeBlocked(drawing: ChartDrawing, locked: boolean, tick: number | null | undefined): string | null {
  if (locked || resolveStyle(drawing).locked) return "已锁定，解锁后才能微调";
  if (drawing.kind === "vline") return "垂直线只有时间，没有价格可调";
  if (!tick || tick <= 0) return "还没有这个合约的最小变动价位";
  return null;
}

/**
 * 画线第二版 (2026-10-09): the chart stage's drawing state in one place — the toolbar (shown or not,
 * the open group, the last tool of each group), the armed tool, the selection and its style bar, the
 * 精确设置 dialog, the 全部删除 confirmation, the account switches (磁吸 / 全部锁定 / 全部隐藏) — plus
 * every UI command surface over them and the snapshot report (`state.chartDrawings`). The chart
 * components only draw and hit-test; everything a click can do here a command can do too.
 */
export function useDrawingDesk({ drawings, symbol, period, tick, preferences, savePreferences, contextMenu, mobile = false }: Input) {
  const available = Boolean(drawings?.available && symbol);
  const [tool, setToolState] = useState<DrawingKind | null>(null);
  const toolRef = useRef(tool);
  toolRef.current = tool;
  const [lastUsed, setLastUsed] = useState(readLastTools);
  const [openGroup, setOpenGroup] = useState<DrawingGroup | null>(null);
  // 画线工具栏按需浮现 (owner 2026-10-14): hidden until a tool is armed (right-click 画线 ▸, a shortcut, a UI
  // command) and hidden again once the chart is back to the crosshair.
  const [toolbarOpen, setToolbarOpenState] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  /** The anchor last pressed or dragged on a drawing (±1 跳 moves only it while that drawing stays selected). */
  const [pressed, setPressed] = useState<{ id: string; index: number } | null>(null);
  const [selectedAt, setSelectedAt] = useState<{ clientX: number; clientY: number } | null>(null);
  const [settingsId, setSettingsId] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const [measure, setMeasure] = useState<Anchor[] | null>(null);
  const [notice, setNotice] = useState("");
  /** Back to the crosshair. On the computer the toolbar goes with it (and its group list and 全部删除 confirmation);
   *  the phone's ✎ sheet is closed while a tool is armed anyway. */
  const disarm = useCallback(() => {
    setToolState(null);
    if (mobile) return;
    setToolbarOpenState(false);
    setOpenGroup(null);
    setConfirmClear(false);
  }, [mobile]);
  const magnet = Boolean(preferences.drawingMagnet);
  const locked = Boolean(preferences.drawingsLocked);
  const hidden = Boolean(preferences.drawingsHidden);

  const items = useMemo(() => (drawings?.items ?? []).filter(item => item.symbol.toUpperCase() === symbol?.toUpperCase()),
    [drawings?.items, symbol]);
  const hiddenCount = items.filter(item => item.style?.hidden).length;
  // What the chart draws: this period's, not individually hidden, nothing while 全部隐藏 — plus the
  // unsaved 测量.
  const shown = useMemo(() => {
    const list = hidden ? [] : items.filter(item => !item.style?.hidden && visibleOnPeriod(item, period));
    if (!measure) return list;
    const temporary: ChartDrawing = { id: MEASURE_ID, symbol: symbol ?? "", kind: "measure", points: { anchors: measure }, label: "", style: {}, createdAt: "", updatedAt: "" };
    return [...list, temporary];
  }, [items, hidden, period, measure, symbol]);
  // Only a line on the chart can be selected: 全部隐藏, 单独隐藏 or another period drops the selection,
  // so neither the style bar nor Delete can act on a line the user cannot see.
  const selected = shown.find(item => item.id === selectedId && item.id !== MEASURE_ID) ?? null;

  useEffect(() => { if (!notice) return; const timer = window.setTimeout(() => setNotice(""), 6000); return () => window.clearTimeout(timer); }, [notice]);
  // A new contract or period starts clean: no placement, selection, menu or measurement carries over.
  useEffect(() => { disarm(); setSelectedId(null); setSettingsId(null); setConfirmClear(false); setMeasure(null); setOpenGroup(null); }, [symbol, period, disarm]);
  // 全部隐藏 drops a placement in progress too: nothing is drawn onto a chart that shows no lines.
  useEffect(() => { if (hidden) { disarm(); setMeasure(null); } }, [hidden, disarm]);
  // Any other selection (or none) works on the whole drawing.
  const handle = selected && pressed?.id === selected.id ? pressed.index : null;
  const setHandle = useCallback((index: number | null) => setPressed(current => index === null ? null : current && { ...current, index }), []);
  useEffect(() => { if (selectedId && !shown.some(item => item.id === selectedId)) setSelectedId(null); }, [shown, selectedId]);
  useEffect(() => { if (settingsId && !items.some(item => item.id === settingsId)) setSettingsId(null); }, [items, settingsId]);
  useEffect(() => { if (confirmClear && !items.length) setConfirmClear(false); }, [confirmClear, items.length]);

  /** Arms a tool (the computer's toolbar shows with it, so the next tool is one click away), or null: back to
   *  the crosshair, which hides the toolbar. */
  const setTool = useCallback((kind: DrawingKind | null) => {
    setOpenGroup(null);
    if (!kind) { disarm(); return; }
    setToolState(kind);
    if (!mobile) setToolbarOpenState(true);
    setSelectedId(null);
    setMeasure(null);
    setLastUsed(current => {
      const next = { ...current, [toolSpec(kind).group]: kind };
      try { localStorage.setItem(LAST_TOOLS_KEY, JSON.stringify(next)); } catch { /* Local convenience only. */ }
      return next;
    });
  }, [disarm, mobile]);
  /** Shows or hides the toolbar (the phone's ✎ sheet). Hiding it on the computer is going back to the crosshair. */
  const setToolbarOpen = useCallback((open: boolean) => {
    if (!open && !mobile) { disarm(); return; }
    setToolbarOpenState(open);
    if (!open) setOpenGroup(null);
  }, [disarm, mobile]);
  /** A group's tool list (right-click 画线 ▸ a group, or the UI command): the toolbar shows with it open. */
  const openGroupList = useCallback((group: DrawingGroup) => { setToolbarOpenState(true); setOpenGroup(group); }, []);
  /** 全部删除's second confirmation hangs off the toolbar (the phone's sheet), which shows for it. */
  const askClear = useCallback(() => { setToolbarOpenState(true); setConfirmClear(true); }, []);
  /** The confirmation closes (取消, 删除 or a click elsewhere); with no tool armed the toolbar goes with it. */
  const closeConfirm = useCallback(() => {
    setConfirmClear(false);
    if (!mobile && !toolRef.current) setToolbarOpenState(false);
  }, [mobile]);
  const select = useCallback((id: string | null, at?: { clientX: number; clientY: number }, index?: number | null) => {
    if (id === MEASURE_ID) return;
    setSelectedId(id);
    setSelectedAt(at ?? null);
    setPressed(id && typeof index === "number" ? { id, index } : null);
    if (id) setMeasure(null);
  }, []);
  const savePreference = useCallback((patch: DrawingPreferencePatch) => {
    if (!savePreferences) throw new Error("连接后可设置");
    savePreferences(patch);
  }, [savePreferences]);

  const place = useCallback((kind: DrawingKind, anchors: Anchor[]) => {
    disarm();
    if (kind === "measure") { setMeasure(anchors); return; }
    if (!drawings || !symbol) return;
    const defaults = preferences.drawingDefaults?.[kind] ?? {};
    const style: DrawingStyle = { period };
    for (const [key, value] of Object.entries(defaults)) if (value !== null && value !== undefined) (style as Record<string, unknown>)[key] = value;
    void drawings.create(symbol, kind, placementPoints(kind, anchors, tick), "", style).then(made => {
      if (!made) return;
      setSelectedId(made.id);
      setSelectedAt(null);
      // 文字 needs its words: the 精确设置 opens on the new drawing.
      if (kind === "text") setSettingsId(made.id);
    });
  }, [drawings, symbol, period, tick, preferences.drawingDefaults, disarm]);
  /** 保留 the 测量: saved as a drawing like any other. */
  const keepMeasure = useCallback(() => {
    if (!measure || !drawings || !symbol) return;
    const anchors = measure;
    setMeasure(null);
    void drawings.create(symbol, "measure", placementPoints("measure", anchors, tick), "", { period });
  }, [measure, drawings, symbol, tick, period]);
  const move = useCallback((drawing: ChartDrawing, anchors: Anchor[]) => {
    if (!drawings || drawing.id === MEASURE_ID) { if (drawing.id === MEASURE_ID) setMeasure(anchors); return; }
    if (locked || resolveStyle(drawing).locked) return;
    void drawings.update(drawing.id, { points: placementPoints(drawing.kind, anchors, tick, drawing.points.levels) });
  }, [drawings, locked, tick]);
  const patch = useCallback((id: string, change: DrawingPatch) => drawings ? drawings.update(id, change) : Promise.resolve(false), [drawings]);
  /** ±1 跳 (手机画线): the chosen anchor, or the whole drawing, one price tick up or down — real prices on the
   *  contract's tick, times untouched. A locked line, a 垂直线 or a contract without a tick does not move. */
  const nudge = useCallback((direction: 1 | -1) => {
    if (!drawings || !selected || nudgeBlocked(selected, locked, tick) || !tick) return;
    const anchors = anchorsOf(selected);
    const step = direction * tick;
    const next = handle !== null && handle < anchors.length
      ? anchors.map((anchor, index) => index === handle ? { ...anchor, p: anchor.p + step } : anchor)
      : shiftAnchors(selected.kind, anchors, 0, step);
    if (next.some(anchor => Number.isFinite(anchor.p) && anchor.p <= 0)) return;
    void drawings.update(selected.id, { points: placementPoints(selected.kind, next, tick, selected.points.levels) });
  }, [drawings, selected, locked, tick, handle]);
  /** 精确设置's 确定: the drawing's patch, then (when ticked) this kind's default style for both computer and phone. */
  const saveSettings = useCallback(async (drawing: ChartDrawing, change: DrawingPatch, defaults: DrawingDefaultStyle | null) => {
    const saved = await patch(drawing.id, change);
    if (saved && defaults) {
      // A partial document: the Runtime merges it, so the other tools' defaults stay as they are.
      try { savePreference({ drawingDefaults: { [drawing.kind]: defaults } }); }
      catch (error) { setNotice(error instanceof Error ? error.message : "默认样式没有保存"); }
    }
    return saved;
  }, [patch, savePreference]);
  const remove = useCallback((id: string) => {
    if (!drawings) return;
    drawings.remove(id);
    setSelectedId(current => current === id ? null : current);
    setSettingsId(current => current === id ? null : current);
  }, [drawings]);
  const duplicate = useCallback((drawing: ChartDrawing) => {
    if (!drawings || !symbol) return;
    const anchors = duplicateAnchors(drawing.kind, anchorsOf(drawing), tick);
    const { locked: _locked, ...style } = drawing.style ?? {};
    void drawings.create(symbol, drawing.kind, placementPoints(drawing.kind, anchors, tick, drawing.points.levels), drawing.label, style)
      .then(made => { if (made) { setSelectedId(made.id); setSelectedAt(null); } });
  }, [drawings, symbol, tick]);
  const clearAll = useCallback(() => {
    closeConfirm();
    if (drawings && symbol) drawings.clear(symbol);
    setSelectedId(null);
  }, [drawings, symbol, closeConfirm]);
  /** 显示单独隐藏的线: the individually hidden ones on this contract come back. */
  const showHidden = useCallback(() => {
    for (const item of items) if (item.style?.hidden) void drawings?.update(item.id, { style: { hidden: null } });
  }, [items, drawings]);

  // Keys: Alt+T / Alt+H / Alt+V arm 趋势线 / 水平线 / 垂直线 (physical keys, so Option on a Mac works; the
  // toolbar shows with the tool), Esc steps back (placement and its toolbar → group list → 全部删除
  // confirmation → the toolbar → 精确设置 → measurement → selection), Delete removes the selected line unless
  // it is locked. A keystroke inside a text field belongs to the field.
  const keyState = useRef({ tool, openGroup, confirmClear, toolbarOpen, settingsId, selectedId, selected, locked, measure });
  keyState.current = { tool, openGroup, confirmClear, toolbarOpen, settingsId, selectedId, selected, locked, measure };
  useEffect(() => {
    if (!available) return;
    const onKey = (event: KeyboardEvent) => {
      const element = event.target instanceof HTMLElement ? event.target : document.activeElement as HTMLElement | null;
      if (element && (element.tagName === "INPUT" || element.tagName === "TEXTAREA" || element.tagName === "SELECT" || element.isContentEditable)) return;
      if (element?.closest?.("dialog[open]")) return;
      const state = keyState.current;
      if (event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey) {
        const kind = event.code === "KeyT" ? "trendline" : event.code === "KeyH" ? "hline" : event.code === "KeyV" ? "vline" : null;
        if (kind) { event.preventDefault(); setTool(state.tool === kind ? null : kind); return; }
      }
      if (event.key === "Escape") {
        if (state.tool) disarm();
        else if (state.openGroup) setOpenGroup(null);
        else if (state.confirmClear) closeConfirm();
        else if (state.toolbarOpen && !mobile) disarm();
        else if (state.settingsId) setSettingsId(null);
        else if (state.measure) setMeasure(null);
        else if (state.selectedId) setSelectedId(null);
        return;
      }
      if ((event.key === "Delete" || event.key === "Backspace") && state.selected && !state.locked && !resolveStyle(state.selected).locked) {
        event.preventDefault();
        remove(state.selected.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [available, setTool, remove, disarm, closeConfirm, mobile]);

  // ── UI command surfaces (AI operability): everything above is commandable and observable. ──
  const known = (id: string) => {
    const drawing = items.find(item => item.id === id);
    if (!drawing) throw new Error("本合约没有这条线（state.chartDrawings.items 里是本合约的线）");
    return drawing;
  };
  useDisclosureSurface("chart-drawing-toolbar", { available, state: toolbarOpen, apply: setToolbarOpen });
  useDisclosureFamily("chart-drawing-group", { available, open: openGroup && toolbarOpen ? [openGroup] : [], apply: (id, open) => {
    if (!isDrawingGroup(id)) throw new Error("没有这组工具：lines / levels / fib / shapes / notes / measure");
    if (open) openGroupList(id); else setOpenGroup(current => current === id ? null : current);
  } });
  useDisclosureFamily("chart-drawing-tool", { available, open: tool ? [tool] : [], apply: (id, open) => {
    if (!isDrawingKind(id)) throw new Error("没有这种画线工具");
    if (open) setTool(id);
    else {
      if (toolRef.current === id) disarm();
      if (id === "measure") setMeasure(null);   // 清除: the unsaved measurement goes too
    }
  } });
  useDisclosureSurface("chart-drawing-magnet", { available, state: magnet, apply: open => savePreference({ drawingMagnet: open }) });
  useDisclosureSurface("chart-drawing-lock-all", { available, state: locked, apply: open => savePreference({ drawingsLocked: open }) });
  useDisclosureSurface("chart-drawings-hidden", { available, state: hidden, apply: open => savePreference({ drawingsHidden: open }) });
  useDisclosureSurface("chart-drawing-clear", { available, state: confirmClear, apply: open => {
    if (open && !items.length) throw new Error("本合约没有画线");
    if (open) askClear(); else closeConfirm();
  } });
  useDisclosureFamily("chart-drawing-style", { available, open: selected ? [selected.id] : [], apply: (id, open) => {
    if (open) {
      const drawing = known(id);
      if (drawing.style?.hidden || hidden) throw new Error("这条线隐藏着，先显示再选");
      if (!visibleOnPeriod(drawing, period)) throw new Error("这条线只在画它的那个周期显示，先切过去再选");
      select(id);
    }
    else setSelectedId(current => current === id ? null : current);
  } });
  useDisclosureFamily("chart-drawing-settings", { available, open: settingsId ? [settingsId] : [], apply: (id, open) => {
    if (open) { known(id); setSettingsId(id); } else setSettingsId(current => current === id ? null : current);
  } });
  const report = available ? {
    symbol, count: items.length, tool, toolbar: toolbarOpen ? "shown" : "hidden", group: openGroup,
    selected: selected?.id ?? null, handle, settings: settingsId, magnet, locked, hidden, contextMenu,
    measuring: Boolean(measure),
    items: items.slice(0, 50).map(item => ({ id: item.id, kind: item.kind, label: item.label, locked: Boolean(item.style?.locked), hidden: Boolean(item.style?.hidden) })),
  } : null;
  useUiSurface("chartDrawings", { state: report }, [JSON.stringify(report)], available);

  return {
    available, items, shown, hiddenCount, selected, selectedAt, handle, setHandle, tool, setTool, lastUsed, openGroup, setOpenGroup, openGroupList,
    toolbarOpen, setToolbarOpen, settingsId, setSettingsId, confirmClear, setConfirmClear, askClear, closeConfirm, magnet, locked, hidden, measure, notice, setNotice, tick,
    select, place, move, patch, nudge, saveSettings, remove, duplicate, clearAll, showHidden, keepMeasure, clearMeasure: () => setMeasure(null), savePreference,
  };
}

export type DrawingDesk = ReturnType<typeof useDrawingDesk>;


import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DesktopChartsPreferences, PreferencesDocument } from "../../userPreferences";
import type { RightDock } from "./support";
import { DEFAULT_DESKTOP_CHARTS, activeIndex } from "./desktopCharts";

/**
 * 自由布局 (2026-11-02): 盘前 / 盘中 / 复盘 and the layouts the user saves, in the account preference
 * `ui.workspaceLayouts` (mirrors the Runtime's WorkspaceLayoutsPreferences). A layout holds the open side panel
 * (one at a time since 2026-11-05) and the 多图 (cells[active] = the chart's own contract and period). Whatever changes on
 * screen is saved into the current layout a moment later; switching restores the other one. A new layout starts
 * blank (owner 2026-11-04): no side panel, one chart on the contract and period shown now.
 */
export interface WorkspaceLayout {
  id: string; name: string;
  dock: { active: RightDock };
  charts: DesktopChartsPreferences | null;
}
export interface WorkspaceLayouts { current: string; items: WorkspaceLayout[] }
export interface WorkspaceLayoutChange { select?: string; save?: string; rename?: { id: string; name: string }; remove?: string }
export type LayoutCapture = Pick<WorkspaceLayout, "dock" | "charts">;

export const LAYOUTS_MAX = 11;
const NAME_MAX = 12;
const EMPTY_DOCK = { active: null };
const DOCK_PANELS = new Set(["ai", "order", "strategy", "positions", "news", "reminders"]);
export const DEFAULT_LAYOUTS: WorkspaceLayouts = { current: "live", items: [
  { id: "pre", name: "盘前", dock: EMPTY_DOCK, charts: null }, { id: "live", name: "盘中", dock: EMPTY_DOCK, charts: null },
  { id: "review", name: "复盘", dock: EMPTY_DOCK, charts: null }] };

export function normalizeLayouts(raw: Partial<WorkspaceLayouts> | undefined | null): WorkspaceLayouts {
  const items = (Array.isArray(raw?.items) ? raw.items : []).flatMap(item => {
    if (!item || typeof item.id !== "string" || typeof item.name !== "string") return [];
    const active = item.dock?.active && DOCK_PANELS.has(item.dock.active) ? item.dock.active : null;
    return [{ id: item.id, name: item.name, dock: { active }, charts: item.charts ?? null }];
  }).slice(0, LAYOUTS_MAX);
  if (!items.length) return DEFAULT_LAYOUTS;
  return { current: items.some(item => item.id === raw?.current) ? raw!.current! : items[0].id, items };
}

const same = (a: LayoutCapture, b: LayoutCapture) => JSON.stringify(a) === JSON.stringify(b);

/** What one change does to the list (pure; throws a readable refusal). `restore` names the layout to put on screen. */
export function planLayoutChange(change: WorkspaceLayoutChange, layouts: WorkspaceLayouts, now: LayoutCapture): { layouts: WorkspaceLayouts; restore?: WorkspaceLayout } {
  // The one on screen keeps what it shows before anything else happens.
  const items = layouts.items.map(item => item.id === layouts.current ? { ...item, ...now } : item);
  const named = (name: string) => {
    const clean = name.trim();
    if (!clean || clean.length > NAME_MAX) throw new Error(`布局名 1–${NAME_MAX} 个字`);
    return clean;
  };
  if (change.select !== undefined) {
    const target = items.find(item => item.id === change.select);
    if (!target) throw new Error("没有这个布局");
    return { layouts: { current: target.id, items }, ...(target.id !== layouts.current ? { restore: target } : {}) };
  }
  if (change.save !== undefined) {
    const name = named(change.save);
    if (items.length >= LAYOUTS_MAX) throw new Error(`最多存 ${LAYOUTS_MAX} 个布局，先删一个`);
    if (items.some(item => item.name === name)) throw new Error(`已经有叫“${name}”的布局`);
    const id = `u${Date.now().toString(36)}`;
    const shown = now.charts?.cells[activeIndex(now.charts)];  // single keeps the old 1–3 active: the shown cell is 0
    const blank = { id, name, dock: EMPTY_DOCK,
      charts: { ...DEFAULT_DESKTOP_CHARTS, cells: DEFAULT_DESKTOP_CHARTS.cells.map((cell, index) => index === 0 && shown ? shown : cell) } };
    return { layouts: { current: id, items: [...items, blank] }, restore: blank };
  }
  if (change.rename !== undefined) {
    const name = named(change.rename.name);
    if (!items.some(item => item.id === change.rename!.id)) throw new Error("没有这个布局");
    if (items.some(item => item.name === name && item.id !== change.rename!.id)) throw new Error(`已经有叫“${name}”的布局`);
    return { layouts: { ...layouts, items: items.map(item => item.id === change.rename!.id ? { ...item, name } : item) } };
  }
  if (change.remove !== undefined) {
    if (!items.some(item => item.id === change.remove)) throw new Error("没有这个布局");
    if (items.length === 1) throw new Error("至少留一个布局");
    const rest = items.filter(item => item.id !== change.remove);
    if (layouts.current !== change.remove) return { layouts: { ...layouts, items: rest } };
    return { layouts: { current: rest[0].id, items: rest }, restore: rest[0] };
  }
  throw new Error("workspaceLayout 一次只做一件：select / save / rename / remove");
}

export interface WorkspaceLayoutsHandle {
  layouts: WorkspaceLayouts;
  apply: (change: WorkspaceLayoutChange) => void;
}

/** A member's layouts. `capture` reads the screen now, `restore` puts a layout on it; `changeKey` changes whenever the
 *  screen does (the save into the current layout waits for a quiet second). */
export function useWorkspaceLayouts({ document, connected, update, capture, restore, changeKey, enabled }: {
  document: Partial<WorkspaceLayouts> | undefined;
  connected: boolean;
  update: (patch: Record<string, unknown>) => Promise<PreferencesDocument>;
  capture: () => LayoutCapture;
  /** `opening`: the page just loaded — the side panel comes back, the 多图 and contract stay as this window has them. */
  restore: (layout: WorkspaceLayout, opening?: boolean) => void;
  changeKey: string;
  enabled: boolean;
}): WorkspaceLayoutsHandle {
  const [pending, setPending] = useState<WorkspaceLayouts>();
  const layouts = useMemo(() => pending ?? normalizeLayouts(document), [pending, document]);
  const latest = useRef({ layouts, capture, restore });
  latest.current = { layouts, capture, restore };
  const write = useCallback((next: WorkspaceLayouts) => {
    setPending(next);
    const settle = () => setPending(shown => shown === next ? undefined : shown);
    void update({ ui: { workspaceLayouts: next } }).then(settle, settle);
  }, [update]);
  const apply = useCallback((change: WorkspaceLayoutChange) => {
    if (!connected) throw new Error("连接后可切换布局");
    const plan = planLayoutChange(change, latest.current.layouts, latest.current.capture());
    write(plan.layouts);
    if (plan.restore) latest.current.restore(plan.restore);
  }, [connected, write]);
  // Opening the page puts the current layout's side panel back before anything is saved into it.
  const loaded = document !== undefined;
  const [opened, setOpened] = useState(false);
  useEffect(() => {
    if (!enabled || !loaded || opened) return;
    const { layouts: shown, restore: put } = latest.current;
    const current = shown.items.find(item => item.id === shown.current);
    if (current) put(current, true);
    setOpened(true);
  }, [enabled, loaded, opened]);
  // Every change on screen lands in the current layout once things are quiet for a second.
  useEffect(() => {
    if (!enabled || !connected || !opened) return;
    const timer = window.setTimeout(() => {
      const { layouts: shown, capture: read } = latest.current;
      const current = shown.items.find(item => item.id === shown.current);
      const now = read();
      if (!current || same({ dock: current.dock, charts: current.charts }, now)) return;
      write({ ...shown, items: shown.items.map(item => item.id === shown.current ? { ...item, ...now } : item) });
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [changeKey, enabled, connected, opened, write]);
  return { layouts, apply };
}

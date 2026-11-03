import { createContext, useContext, useEffect, useImperativeHandle, useRef, useState, type ReactNode, type Ref } from "react";
import { X } from "lucide-react";
import { DockviewReact, type DockviewApi, type IDockviewPanelHeaderProps, type IDockviewPanelProps, type SerializedDockview } from "dockview-react";
import "dockview-react/dist/styles/dockview.css";
import type { RightDock } from "./support";
import "./SideDock.css";

/**
 * 自由布局 (2026-11-02): the right side holds several panels at once in dockview (MIT, owner-approved design). Opening
 * another panel adds it as a tab of the focused group; dragging a tab to an edge puts it beside or under the others.
 * One panel alone shows no tab strip, so the side looks as before. The workspace owns which panels are open and
 * which is focused (`state`, the `panel` command's state); dockview owns the arrangement (`grid`, saved with a layout).
 */
export type DockPanel = Exclude<RightDock, null>;
export interface DockState { panels: DockPanel[]; active: DockPanel | null }
export const NO_DOCK: DockState = { panels: [], active: null };
export const DOCK_LABELS: Record<DockPanel, string> = { ai: "对话", news: "调研", strategy: "策略", reminders: "提醒", order: "下单", positions: "持仓" };
export type DockPlace = "tab" | "left" | "right" | "above" | "below";

export interface SideDockHandle {
  /** The `dock` command: put `panel` (opening it when needed) beside / under / as a tab of `of` (default: focused). */
  place: (panel: DockPanel, to: DockPlace, of?: DockPanel) => void;
}

const RenderPanel = createContext<(panel: DockPanel) => ReactNode>(() => null);
function Slot({ api }: IDockviewPanelProps) {
  const render = useContext(RenderPanel);
  // A panel behind another tab is off screen (dockview takes its content out of the page): it keeps what it last showed
  // instead of rendering again on every workspace update, and catches up the moment its tab shows (电脑版流畅度 ①,
  // 2026-11-04). Whether it is on screen is read from the page itself — dockview's own visibility event misses a panel
  // that another newly opened tab covers.
  const [visible, setVisible] = useState(true);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = box.current;
    if (!element || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(entries => setVisible(entries[entries.length - 1].isIntersecting));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const shown = useRef<ReactNode>(null);
  if (visible || shown.current === null) shown.current = render(api.id as DockPanel);
  return <div className="fdw-dock-slot" ref={box}>{shown.current}</div>;
}
const COMPONENTS = { slot: Slot };
/** The tab (dockview makes it draggable): the panel's name and a close for just this panel, in Chinese. */
function Tab({ api }: IDockviewPanelHeaderProps) {
  return <div className="fdw-dock-tab">
    <span>{api.title}</span>
    <button type="button" aria-label={`关闭${api.title ?? ""}`} onPointerDown={event => event.stopPropagation()}
      onClick={event => { event.stopPropagation(); api.close(); }}><X aria-hidden="true" /></button>
  </div>;
}
const THEME = { name: "sente", className: "fdw-dock-theme" };
const POSITION = { left: "left", right: "right", above: "top", below: "bottom", tab: "center" } as const;

const sameDock = (a: DockState, b: DockState) => a.active === b.active && a.panels.length === b.panels.length && a.panels.every((panel, index) => panel === b.panels[index]);
const gridPanels = (grid: SerializedDockview) => Object.keys(grid.panels ?? {}).sort().join(",");

export function SideDock({ state, grid, render, onState, onGrid, handle }: {
  state: DockState;
  /** The arrangement to start from (a layout's), used when it holds exactly the open panels. */
  grid: SerializedDockview | null;
  render: (panel: DockPanel) => ReactNode;
  onState: (state: DockState, groups: DockPanel[][]) => void;
  onGrid: (grid: SerializedDockview) => void;
  handle?: Ref<SideDockHandle>;
}) {
  const apiRef = useRef<DockviewApi | undefined>(undefined);
  const latest = useRef({ state, onState, onGrid });
  latest.current = { state, onState, onGrid };
  const syncing = useRef(false);
  const report = () => {
    const api = apiRef.current;
    if (!api || syncing.current) return;
    const next: DockState = { panels: api.panels.map(panel => panel.id as DockPanel), active: (api.activePanel?.id as DockPanel | undefined) ?? null };
    latest.current.onState(next, api.groups.map(group => group.panels.map(panel => panel.id as DockPanel)));
    latest.current.onGrid(api.toJSON());
  };
  const reconcile = (api: DockviewApi) => {
    const want = latest.current.state;
    syncing.current = true;
    try {
      for (const panel of [...api.panels]) if (!want.panels.includes(panel.id as DockPanel)) panel.api.close();
      for (const id of want.panels) {
        if (api.getPanel(id)) continue;
        api.addPanel({ id, component: "slot", title: DOCK_LABELS[id], ...(api.activeGroup ? { position: { referenceGroup: api.activeGroup } } : {}) });
      }
      if (want.active && api.activePanel?.id !== want.active) api.getPanel(want.active)?.api.setActive();
    } finally { syncing.current = false; }
    report();
  };
  useEffect(() => { if (apiRef.current) reconcile(apiRef.current); }, [state]);   // eslint-disable-line react-hooks/exhaustive-deps
  useImperativeHandle(handle, () => ({
    place: (panel, to, of) => {
      const api = apiRef.current;
      if (!api) throw new Error("右侧没有打开的面板");
      const target = api.getPanel(of ?? api.activePanel?.id ?? "");
      if (!target || target.id === panel) throw new Error(of ? `${DOCK_LABELS[of]}没有打开` : "先打开另一个面板");
      const existing = api.getPanel(panel);
      if (existing) existing.api.moveTo({ group: target.api.group, position: POSITION[to] });
      else api.addPanel({ id: panel, component: "slot", title: DOCK_LABELS[panel],
        position: to === "tab" ? { referencePanel: target.id, direction: "within" } : { referencePanel: target.id, direction: to } });
      api.getPanel(panel)?.api.setActive();
      report();
    },
  }), []);   // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <RenderPanel.Provider value={render}>
      <div className="fdw-side-dock" data-count={state.panels.length} data-agent-target="side-dock">
        <DockviewReact components={COMPONENTS} defaultTabComponent={Tab} theme={THEME} disableFloatingGroups onReady={({ api }) => {
          apiRef.current = api;
          if (grid && gridPanels(grid) === [...latest.current.state.panels].sort().join(",")) {
            try { api.fromJSON(grid); } catch { api.clear(); }
          }
          api.onDidActivePanelChange(report);
          api.onDidAddPanel(report);
          api.onDidRemovePanel(report);
          api.onDidLayoutChange(report);
          reconcile(api);
        }} />
      </div>
    </RenderPanel.Provider>
  );
}

/** The workspace's `rightDock` setter over the dock: a panel opens (as a tab of the focused group) and takes focus;
 *  null closes the whole side, as it always did. */
export function nextDock(current: DockState, value: RightDock): DockState {
  if (value === null) return current.panels.length ? NO_DOCK : current;
  const next = { panels: current.panels.includes(value) ? current.panels : [...current.panels, value], active: value };
  return sameDock(current, next) ? current : next;
}

/** One panel's own close: the others stay; focus goes to the last one left. */
export function withoutPanel(current: DockState, panel: DockPanel): DockState {
  if (!current.panels.includes(panel)) return current;
  const panels = current.panels.filter(item => item !== panel);
  return { panels, active: current.active === panel ? panels.at(-1) ?? null : current.active };
}

export { sameDock };

import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Bell, Bot, ChevronRight, Copy, Eraser, Eye, EyeOff, Lock, LockOpen, Magnet, Ruler, Settings2, Trash2, X } from "lucide-react";
import {
  ALERTABLE_KINDS, DRAWING_GROUPS, QUICK_DRAWING_TOOLS, QUICK_TOOL_LABELS, drawingTitle, resolveStyle, toolSpec,
  type ChartDrawing, type DrawingGroup, type DrawingKind
} from "../../chartDrawingModel";
import type { DrawingPatch } from "../../hooks/useChartDrawings";
import type { DrawingPreferencePatch } from "./useDrawingDesk";
import { SUB_PANE_NAMES, SUB_PANE_TYPES, SUB_PANES_MAX, subPaneLabel, type ChartIndicatorConfig, type SubPaneType } from "../chartIndicators";
import { DrawingToolIcon } from "./DrawingToolIcon";
import { DrawingStyleControls } from "./DrawingStyleBar";

export type MenuTarget =
  | { area: "main"; price: number }
  | { area: "drawing"; drawing: ChartDrawing; price: number }
  | { area: "pane"; pane: SubPaneType };
export interface ChartMenuState { target: MenuTarget; x: number; y: number; sub: string | null }

/** The menu's command id: main / drawing:<id> / pane:<type>, plus "/<submenu>" (the snapshot and the UI command use it). */
export function menuId(menu: ChartMenuState | undefined): string | null {
  if (!menu) return null;
  const base = menu.target.area === "main" ? "main" : menu.target.area === "drawing" ? `drawing:${menu.target.drawing.id}` : `pane:${menu.target.pane}`;
  return menu.sub ? `${base}/${menu.sub}` : base;
}
/** The submenus each menu has (the 分时图 has no indicators or panes). */
export function menuSubs(target: MenuTarget, chartView: "kline" | "timeshare"): string[] {
  if (target.area === "main") return chartView === "kline" ? ["draw", "alert", "period", "indicator", "add-pane"] : ["draw", "alert", "period"];
  if (target.area === "drawing") return ["style"];
  return ["swap", "add-pane"];
}

const PERIODS: ReadonlyArray<{ value: number | "timeshare"; label: string }> = [
  { value: "timeshare", label: "分时" }, { value: 60, label: "1 分" }, { value: 180, label: "3 分" }, { value: 300, label: "5 分" },
  { value: 900, label: "15 分" }, { value: 1800, label: "30 分" }, { value: 3600, label: "1 小时" }, { value: 86400, label: "日线" }, { value: 604800, label: "周线" },
];
/** Every sub pane the chart can show (换成 / 添加副图 follow the shared list as it grows). */
const PANE_TYPES: readonly SubPaneType[] = SUB_PANE_TYPES;

export interface ChartMenuActions {
  chartView: "kline" | "timeshare";
  duration: number;
  decimals: number;
  tick?: number | null;
  /** Null on the 分时图 (no indicators, no sub panes) or a read-only chart. */
  indicators: ChartIndicatorConfig | null;
  /** The drawing actions exist only with drawings (a member window). */
  drawingsAvailable: boolean;
  /** Why drawing is unavailable here (a disconnected member window): 画线 shows greyed with it. */
  drawingsBlocked?: string;
  hiddenAll: boolean;
  hiddenCount: number;
  locked: boolean;
  magnet: boolean;
  /** This contract's drawings (全部删除 is off without any). */
  drawingCount: number;
  hasChartMarks: boolean;
  /** Absent: no alerts here (a guest). */
  onAlert?: (price: number) => void;
  /** 在这里设提醒 ▸ (第 6 批): a 条件提醒 in the strategy editor card's 提醒模式 — after one of the user's 打法 (its entry rules
   *  become the first 补一句), or blank (null). Absent where the card cannot open (a guest, the phone). */
  onReminder?: (playbook: { name: string; entryRules: string } | null) => void;
  playbooks?: readonly { id: string; name: string; entryRules: string }[];
  onTool: (kind: DrawingKind) => void;
  /** A group's whole tool list: the drawing toolbar shows with it open. */
  onGroup: (group: DrawingGroup) => void;
  onPeriod: (value: number | "timeshare") => void;
  onIndicatorToggle: (which: "ma" | "boll") => void;
  onIndicatorSettings: () => void;
  onAddPane: (type: SubPaneType) => void;
  onChartSettings: () => void;
  onAskPrice: (price: number) => void;
  onAskSpan: () => void;
  /** 磁吸 / 全部锁定 / 全部隐藏 (account preferences). */
  onDrawingSwitch: (patch: DrawingPreferencePatch) => void;
  onShowHidden: () => void;
  /** 全部删除: the toolbar's second confirmation shows. */
  onClearDrawings: () => void;
  onClearMarks: () => void;
  onPatch: (drawing: ChartDrawing, patch: DrawingPatch) => void;
  onSettings: (drawing: ChartDrawing) => void;
  /** 这条线被碰到时提醒我 (改版 2026-10-31): sets the line's alert, or cancels it when `lineAlerted` says it has one. */
  onLineAlert: (drawing: ChartDrawing) => void;
  lineAlerted?: (drawing: ChartDrawing) => boolean;
  onDuplicate: (drawing: ChartDrawing) => void;
  onDelete: (drawing: ChartDrawing) => void;
  onSwapPane: (from: SubPaneType, to: SubPaneType) => void;
  onPaneSettings: (type: SubPaneType) => void;
  onMovePane: (type: SubPaneType, direction: -1 | 1) => void;
  onClosePane: (type: SubPaneType) => void;
}

function Item({ children, onClick, disabled, danger, title, checked }: { children: ReactNode; onClick: () => void; disabled?: boolean; danger?: boolean; title?: string; checked?: boolean }) {
  return <button type="button" role={checked === undefined ? "menuitem" : "menuitemcheckbox"} aria-checked={checked} className={danger ? "danger" : undefined}
    disabled={disabled} title={title} onClick={onClick}>{children}</button>;
}

/**
 * 画线第二版 (2026-10-09, 样稿 ③): the chart's right-click menu serves the chart only — three kinds by
 * where the click lands: 主图空白处 (画线 ▸, 在这里设提醒 ▸, 测量, 周期 ▸, 主图指标 ▸, 添加副图 ▸, 图表设置…,
 * the two AI questions, 清空图上标记), 一条线上 (颜色/粗细/线型 ▸, 精确设置…, 这条线被碰到时提醒我,
 * 复制一条, 锁定, 删除 danger) and 副图上 (换成 ▸, 参数设置…, 上移/下移, 添加副图 ▸, 关闭 danger). Ordering
 * stays in the order panel and the shortcuts. Each menu and submenu is a UI command
 * (`disclosure: chart-context-menu {id}`).
 */
export function ChartContextMenu({ menu, actions, onSub, onClose }: { menu: ChartMenuState; actions: ChartMenuActions; onSub: (sub: string | null) => void; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState({ left: menu.x, top: menu.y, flip: false });
  useLayoutEffect(() => {
    const height = ref.current?.offsetHeight ?? 320;
    const width = window.innerWidth || 1024;
    const left = Math.max(8, Math.min(menu.x, width - 248));
    setPlace({ left, top: Math.max(8, Math.min(menu.y, (window.innerHeight || 768) - height - 8)), flip: left + 248 + 268 > width });
  }, [menu.x, menu.y, menu.target]);
  const run = (action: () => void) => { action(); onClose(); };
  const target = menu.target;
  // Hover or click opens a submenu. A click never closes it: the pointer has always just hovered the item, so a
  // toggle would shut the submenu the hover opened (10-14). Another submenu, Esc or a click elsewhere closes it.
  const subToggle = (sub: string) => ({ "aria-haspopup": "menu" as const, "aria-expanded": menu.sub === sub, onClick: () => onSub(sub), onPointerEnter: () => onSub(sub) });
  const price = target.area === "pane" ? 0 : target.price;
  const priceText = price ? price.toLocaleString("zh-CN", { minimumFractionDigits: actions.decimals, maximumFractionDigits: actions.decimals }) : "";
  const indicators = actions.indicators;

  let header: string;
  let body: ReactNode;
  if (target.area === "main") {
    header = `主图 · ${priceText}（光标价）`;
    const maOn = indicators?.maLines.some(line => line.enabled) ?? false;
    body = <>
      {actions.drawingsAvailable ? (
        <div className="fdw-cm-wrap">
          <button type="button" role="menuitem" {...subToggle("draw")}><span>画线</span><ChevronRight aria-hidden="true" /></button>
          {menu.sub === "draw" ? (
            <div className="fdw-context-menu fdw-cm-sub" data-flip={place.flip} role="menu" aria-label="画线（点一个就开始画）">
              <strong>画线（点一个就开始画）</strong>
              <div className="fdw-cm-grid">
                {QUICK_DRAWING_TOOLS.map(kind => (
                  <button type="button" role="menuitem" key={kind} onClick={() => run(() => actions.onTool(kind))}>
                    <DrawingToolIcon kind={kind} /><span>{QUICK_TOOL_LABELS[kind] ?? toolSpec(kind).label}</span>
                  </button>
                ))}
              </div>
              <strong>全部工具</strong>
              <div className="fdw-cm-groups">
                {DRAWING_GROUPS.map(group => (
                  <button type="button" role="menuitem" key={group.id} title={`打开画线工具栏的“${group.label}”组`}
                    onClick={() => run(() => actions.onGroup(group.id))}>{group.label}</button>
                ))}
              </div>
              <hr />
              <Item checked={actions.magnet} onClick={() => run(() => actions.onDrawingSwitch({ drawingMagnet: !actions.magnet }))}><Magnet aria-hidden="true" /><span>磁吸</span></Item>
              <Item checked={actions.locked} onClick={() => run(() => actions.onDrawingSwitch({ drawingsLocked: !actions.locked }))}><Lock aria-hidden="true" /><span>全部锁定</span></Item>
              <Item checked={actions.hiddenAll} onClick={() => run(() => actions.onDrawingSwitch({ drawingsHidden: !actions.hiddenAll }))}><EyeOff aria-hidden="true" /><span>全部隐藏</span></Item>
              {actions.hiddenCount && !actions.hiddenAll ? <Item onClick={() => run(actions.onShowHidden)}><Eye aria-hidden="true" /><span>显示单独隐藏的 {actions.hiddenCount} 条线</span></Item> : null}
              <hr />
              <Item danger disabled={!actions.drawingCount} onClick={() => run(actions.onClearDrawings)}><Trash2 aria-hidden="true" /><span>全部删除（{actions.drawingCount} 条）…</span></Item>
            </div>
          ) : null}
        </div>
      ) : actions.drawingsBlocked ? (
        <Item disabled title={actions.drawingsBlocked} onClick={() => undefined}><span>画线</span><small>{actions.drawingsBlocked}</small></Item>
      ) : null}
      {actions.onAlert ? <div className="fdw-cm-wrap">
        <button type="button" role="menuitem" {...subToggle("alert")}><Bell aria-hidden="true" /><span>在这里设提醒</span><small>{priceText}</small><ChevronRight aria-hidden="true" /></button>
        {menu.sub === "alert" ? (
          <div className="fdw-context-menu fdw-cm-sub" data-flip={place.flip} role="menu" aria-label="在这里设提醒">
            <Item onClick={() => run(() => actions.onAlert!(price))}><span>价格到 {priceText} 时提醒我</span></Item>
            {actions.onReminder ? <>
              {actions.playbooks?.length ? <hr /> : null}
              {actions.playbooks?.map(playbook => <Item key={playbook.id} onClick={() => run(() => actions.onReminder!(playbook))}><span>按交易笔记：{playbook.name}</span></Item>)}
              <hr />
              <Item onClick={() => run(() => actions.onReminder!(null))}><span>按条件新建提醒…</span></Item>
            </> : null}
          </div>
        ) : null}
      </div> : null}
      {actions.drawingsAvailable ? <Item onClick={() => run(() => actions.onTool("measure"))}><Ruler aria-hidden="true" /><span>测量区间</span></Item> : null}
      <hr />
      <div className="fdw-cm-wrap">
        <button type="button" role="menuitem" {...subToggle("period")}><span>周期</span><small>{actions.chartView === "timeshare" ? "分时" : PERIODS.find(item => item.value === actions.duration)?.label}</small><ChevronRight aria-hidden="true" /></button>
        {menu.sub === "period" ? (
          <div className="fdw-context-menu fdw-cm-sub" data-flip={place.flip} role="menu" aria-label="周期">
            {PERIODS.map(item => {
              const current = item.value === "timeshare" ? actions.chartView === "timeshare" : actions.chartView === "kline" && actions.duration === item.value;
              return <button type="button" role="menuitemradio" key={String(item.value)} aria-checked={current} onClick={() => run(() => actions.onPeriod(item.value))}>{item.label}</button>;
            })}
          </div>
        ) : null}
      </div>
      {indicators ? <>
        <div className="fdw-cm-wrap">
          <button type="button" role="menuitem" {...subToggle("indicator")}><span>主图指标</span><small>{[maOn ? "均线" : "", indicators.boll.enabled ? "布林" : ""].filter(Boolean).join(" · ") || "无"}</small><ChevronRight aria-hidden="true" /></button>
          {menu.sub === "indicator" ? (
            <div className="fdw-context-menu fdw-cm-sub" data-flip={place.flip} role="menu" aria-label="主图指标">
              <Item checked={maOn} onClick={() => run(() => actions.onIndicatorToggle("ma"))}>均线</Item>
              <Item checked={indicators.boll.enabled} onClick={() => run(() => actions.onIndicatorToggle("boll"))}>布林带</Item>
              <hr />
              <Item onClick={() => run(actions.onIndicatorSettings)}>均线与布林带设置…</Item>
            </div>
          ) : null}
        </div>
        <AddPane indicators={indicators} open={menu.sub === "add-pane"} flip={place.flip} toggle={subToggle("add-pane")} onAdd={type => run(() => actions.onAddPane(type))} />
      </> : null}
      <Item onClick={() => run(actions.onChartSettings)}><Settings2 aria-hidden="true" /><span>图表设置…</span></Item>
      <hr />
      <Item onClick={() => run(() => actions.onAskPrice(price))}><Bot aria-hidden="true" /><span>让 AI 分析这个价</span></Item>
      <Item onClick={() => run(actions.onAskSpan)}><Bot aria-hidden="true" /><span>让 AI 讲讲这一段</span></Item>
      <hr />
      <Item disabled={!actions.hasChartMarks} onClick={() => run(actions.onClearMarks)}><Eraser aria-hidden="true" /><span>清空图上标记</span></Item>
    </>;
  } else if (target.area === "drawing") {
    const drawing = target.drawing;
    const style = resolveStyle(drawing);
    header = drawingTitle(drawing, actions.tick);
    const alertable = ALERTABLE_KINDS.has(drawing.kind) && Boolean(actions.onAlert);
    body = <>
      <div className="fdw-cm-wrap">
        <button type="button" role="menuitem" {...subToggle("style")}><span>颜色 / 粗细 / 线型</span><ChevronRight aria-hidden="true" /></button>
        {menu.sub === "style" ? (
          <div className="fdw-context-menu fdw-cm-sub fdw-cm-style" data-flip={place.flip} role="menu" aria-label="颜色 / 粗细 / 线型">
            <DrawingStyleControls drawing={drawing} compact onPatch={patch => actions.onPatch(drawing, patch)} />
          </div>
        ) : null}
      </div>
      <Item onClick={() => run(() => actions.onSettings(drawing))}><Settings2 aria-hidden="true" /><span>精确设置…</span></Item>
      {actions.onAlert ? <Item disabled={!alertable} title={alertable ? undefined : "形状和标注不是价位，不能设提醒"} onClick={() => run(() => actions.onLineAlert(drawing))}><Bell aria-hidden="true" />
        <span>{actions.lineAlerted?.(drawing) ? "取消这条线的提醒" : "这条线被碰到时提醒我"}</span></Item> : null}
      <Item onClick={() => run(() => actions.onDuplicate(drawing))}><Copy aria-hidden="true" /><span>复制一条</span></Item>
      <Item onClick={() => run(() => actions.onPatch(drawing, { style: { locked: !style.locked } }))}>{style.locked ? <LockOpen aria-hidden="true" /> : <Lock aria-hidden="true" />}<span>{style.locked ? "解锁" : "锁定"}</span></Item>
      <hr />
      <Item danger onClick={() => run(() => actions.onDelete(drawing))}><Trash2 aria-hidden="true" /><span>删除这条线</span></Item>
    </>;
  } else {
    const type = target.pane;
    const list = indicators?.subPaneList ?? [];
    const index = list.indexOf(type);
    header = `副图 · ${indicators ? subPaneLabel(type, indicators) : SUB_PANE_NAMES[type]}`;
    body = <>
      <div className="fdw-cm-wrap">
        <button type="button" role="menuitem" {...subToggle("swap")}><span>换成</span><small>{SUB_PANE_NAMES[type]}</small><ChevronRight aria-hidden="true" /></button>
        {menu.sub === "swap" ? (
          <div className="fdw-context-menu fdw-cm-sub" data-flip={place.flip} role="menu" aria-label="换成">
            {PANE_TYPES.map(other => <button type="button" role="menuitemradio" key={other} aria-checked={other === type}
              disabled={other !== type && list.includes(other)} onClick={() => run(() => { if (other !== type) actions.onSwapPane(type, other); })}>{SUB_PANE_NAMES[other]}</button>)}
          </div>
        ) : null}
      </div>
      <Item onClick={() => run(() => actions.onPaneSettings(type))}><Settings2 aria-hidden="true" /><span>参数设置…</span></Item>
      <div className="fdw-cm-row" role="group" aria-label="上移 / 下移">
        <button type="button" role="menuitem" disabled={index <= 0} onClick={() => run(() => actions.onMovePane(type, -1))}>上移</button>
        <button type="button" role="menuitem" disabled={index < 0 || index >= list.length - 1} onClick={() => run(() => actions.onMovePane(type, 1))}>下移</button>
      </div>
      {indicators ? <AddPane indicators={indicators} open={menu.sub === "add-pane"} flip={place.flip} toggle={subToggle("add-pane")} onAdd={added => run(() => actions.onAddPane(added))} /> : null}
      <hr />
      <Item danger onClick={() => run(() => actions.onClosePane(type))}><X aria-hidden="true" /><span>关闭这个副图</span></Item>
    </>;
  }
  return (
    <div className="fdw-context-menu fdw-chart-menu" role="menu" aria-label="图表菜单" data-agent-target="chart:context-menu" ref={ref} style={{ position: "fixed", left: place.left, top: place.top }}
      onKeyDown={event => {
        if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
        event.preventDefault();
        const items = [...(ref.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
        const at = items.indexOf(document.activeElement as HTMLButtonElement);
        items[(at + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
      }}>
      <strong>{header}</strong>
      {body}
    </div>
  );
}

function AddPane({ indicators, open, flip, toggle, onAdd }: { indicators: ChartIndicatorConfig; open: boolean; flip: boolean;
  toggle: Record<string, unknown>; onAdd: (type: SubPaneType) => void }) {
  const full = indicators.subPaneList.length >= SUB_PANES_MAX;
  return (
    <div className="fdw-cm-wrap">
      <button type="button" role="menuitem" {...toggle}><span>添加副图</span>{full ? <small>已满 {SUB_PANES_MAX} 个</small> : null}<ChevronRight aria-hidden="true" /></button>
      {open ? (
        <div className="fdw-context-menu fdw-cm-sub" data-flip={flip} role="menu" aria-label="添加副图">
          {PANE_TYPES.map(type => <button type="button" role="menuitem" key={type} disabled={full || indicators.subPaneList.includes(type)}
            title={indicators.subPaneList.includes(type) ? "已经显示" : full ? `副图最多 ${SUB_PANES_MAX} 个` : undefined} onClick={() => onAdd(type)}>{SUB_PANE_NAMES[type]}</button>)}
        </div>
      ) : null}
    </div>
  );
}

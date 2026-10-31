import { useEffect, useRef, useState } from "react";
import { Eye, EyeOff, Lock, LockOpen, Magnet, Settings2, Trash2 } from "lucide-react";
import { DRAWING_COLOR_TOKENS, DRAWING_GROUPS, DRAWING_TOOLS, anchorsOf, priceLabel, resolveStyle, toolSpec, type ChartDrawing } from "../../chartDrawingModel";
import { HelpTip } from "../Blocks";
import type { HandleDrag } from "../chartDrawingInteraction";
import { DrawingToolIcon } from "../futures/DrawingToolIcon";
import { DrawingStyleControls } from "../futures/DrawingStyleBar";
import { DrawingSettingsDialog } from "../futures/DrawingSettingsDialog";
import { nudgeBlocked, type DrawingDesk } from "../futures/useDrawingDesk";
import { useDisclosureFamily } from "../../uiSurfaces";
import "../futures/ChartDrawing.css";
import "./MobileDrawing.css";

/**
 * 手机画线 (2026-10-09, 样稿 ④ 第 1 屏): the phone face of the computer's drawing desk — the same drawings, tools,
 * account switches, default styles, UI commands and snapshot (`useDrawingDesk`), only laid out for a finger.
 * ✎ beside the periods opens this sheet (`chart-drawing-toolbar`): the six groups with every tool, then 磁吸 /
 * 全部锁定 / 全部隐藏 / 全部删除. Picking a tool closes it; the chart's taps then place the points.
 */
export function MobileDrawingSheet({ desk, onError }: { desk: DrawingDesk; onError: (message: string) => void }) {
  const { tool, setTool, setToolbarOpen, openGroup, magnet, locked, hidden, hiddenCount, items, confirmClear, setConfirmClear } = desk;
  const groupRefs = useRef(new Map<string, HTMLElement>());
  // `chart-drawing-group {id}` opens the sheet on that group.
  useEffect(() => { if (openGroup) groupRefs.current.get(openGroup)?.scrollIntoView?.({ block: "nearest" }); }, [openGroup]);
  const close = () => setToolbarOpen(false);
  const pick = (kind: Parameters<DrawingDesk["setTool"]>[0]) => { setTool(kind); setToolbarOpen(false); };
  const switchPreference = (change: () => void) => { try { change(); } catch (error) { onError(error instanceof Error ? error.message : "连接后可设置"); } };
  return (
    <div className="fdm-sheet-backdrop" onClick={event => { if (event.target === event.currentTarget) close(); }}>
      <section className="fdm-sheet fdm-draw-sheet" role="dialog" aria-label="画线工具" data-agent-target="chart:drawing-toolbar">
        <div className="fdm-sheet-grip" aria-hidden="true" />
        <header className="fdm-sheet-head">
          <span><strong>画线工具<HelpTip helpKey="mobile-drawing" label="手机画线">
            选一个工具，在图上点出它需要的点（趋势线两下、水平线一下、通道三下；折线逐点点完按“完成”）。点一条线选中它，下面的条可以 ±1 跳微调、换颜色粗细线型、锁定或删除；
            按住线的端点拖动时手指上方有放大镜。电脑和手机是同一份画线和默认样式。
          </HelpTip></strong></span>
          <button type="button" className="fdm-sheet-done primary-button" onClick={close}>完成</button>
        </header>
        <div className="fdm-sheet-body">
          {tool ? <button type="button" className="app-button fdm-draw-stop" onClick={() => pick(null)}>不画了（{toolSpec(tool).label}）</button> : null}
          {DRAWING_GROUPS.map(group => (
            <section key={group.id} className="fdm-draw-group" data-open={openGroup === group.id || undefined}
              ref={element => { if (element) groupRefs.current.set(group.id, element); else groupRefs.current.delete(group.id); }}>
              <h4>{group.label}</h4>
              <div className="fdm-draw-tools" role="group" aria-label={`${group.label}工具`}>
                {DRAWING_TOOLS.filter(spec => spec.group === group.id).map(spec => (
                  <button type="button" key={spec.kind} aria-pressed={tool === spec.kind} onClick={() => pick(tool === spec.kind ? null : spec.kind)}>
                    <DrawingToolIcon kind={spec.kind} /><span>{spec.label}</span>
                  </button>
                ))}
              </div>
            </section>
          ))}
          <div className="fdm-draw-switches" role="group" aria-label="全部画线">
            <button type="button" aria-pressed={magnet} onClick={() => switchPreference(() => desk.savePreference({ drawingMagnet: !magnet }))}>
              <Magnet aria-hidden="true" />磁吸</button>
            <button type="button" aria-pressed={locked} onClick={() => switchPreference(() => desk.savePreference({ drawingsLocked: !locked }))}>
              {locked ? <Lock aria-hidden="true" /> : <LockOpen aria-hidden="true" />}全部锁定</button>
            <button type="button" aria-pressed={hidden} onClick={() => switchPreference(() => desk.savePreference({ drawingsHidden: !hidden }))}>
              {hidden ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}全部隐藏</button>
            {hiddenCount ? <button type="button" onClick={desk.showHidden}><Eye aria-hidden="true" />显示单独隐藏的 {hiddenCount} 条</button> : null}
          </div>
          {confirmClear ? (
            <div className="fdm-draw-confirm" role="dialog" aria-label="确认删除全部画线">
              <p>删除本合约全部 {items.length} 条画线？删除后不能恢复。</p>
              <div>
                <button type="button" className="app-button" onClick={() => setConfirmClear(false)}>取消</button>
                <button type="button" className="app-button danger" onClick={desk.clearAll}>删除 {items.length} 条</button>
              </div>
            </div>
          ) : <button type="button" className="app-button danger fdm-draw-clear" disabled={!items.length} onClick={() => setConfirmClear(true)}>
            <Trash2 aria-hidden="true" />全部删除（本合约 {items.length} 条）</button>}
        </div>
      </section>
    </div>
  );
}

const STYLE_ROWS = ["color", "width", "dash"] as const;
type StyleRow = typeof STYLE_ROWS[number];

/**
 * The bar under the chart while drawing: a placement's hint and point count (取消; a 折线's 完成 once it has two
 * points), the unsaved 测量's 保留 / 清除, or the selected line's ±1 跳 (the anchor last pressed or dragged, else the
 * whole line — the title says which), 颜色 / 粗细 / 线型, 锁定, 精确设置, 删除 and 完成. The style row open
 * (`disclosure: mobile-drawing-style-row {id: color|width|dash}`) closes with the selection.
 */
export function MobileDrawingBar({ desk, draftCount, onFinishPlacement }: {
  desk: DrawingDesk;
  /** Points the placement in progress has (from the chart). */
  draftCount: number;
  onFinishPlacement: () => void;
}) {
  const [row, setRow] = useState<StyleRow | null>(null);
  const selected = desk.selected;
  useEffect(() => { setRow(null); }, [selected?.id]);
  useDisclosureFamily("mobile-drawing-style-row", { available: Boolean(selected) && !desk.tool, open: row ? [row] : [], apply: (id, open) => {
    if (!(STYLE_ROWS as readonly string[]).includes(id)) throw new Error("没有这一排：color / width / dash");
    setRow(current => open ? id as StyleRow : current === id ? null : current);
  } });
  if (desk.tool) {
    const spec = toolSpec(desk.tool);
    const many = spec.kind === "polyline";
    return (
      <div className="fdm-draw-bar" role="toolbar" aria-label="正在画线" data-agent-target="chart:drawing-hint">
        <div className="fdm-draw-bar-head">
          <span className="fdm-draw-bar-title"><b>{spec.label}</b>
            <small>{spec.kind === "polyline" ? "逐点点击，点够两点后按完成" : spec.hint}{spec.max > 1 ? ` · 已点 ${draftCount}${many ? "" : `/${spec.max}`}` : ""}</small></span>
          <button type="button" className="app-button" onClick={() => desk.setTool(null)}>取消</button>
          {many ? <button type="button" className="app-button primary" disabled={draftCount < spec.min} onClick={onFinishPlacement}>完成</button> : null}
        </div>
      </div>
    );
  }
  if (desk.measure && !selected) {
    return (
      <div className="fdm-draw-bar" role="toolbar" aria-label="测量结果" data-agent-target="chart:drawing-hint">
        <div className="fdm-draw-bar-head">
          <span className="fdm-draw-bar-title"><b>测量</b><small>结果不保存</small></span>
          <button type="button" className="app-button danger" onClick={desk.clearMeasure}>清除</button>
          <button type="button" className="app-button primary" onClick={desk.keepMeasure}>保留</button>
        </div>
      </div>
    );
  }
  if (!selected) return null;
  return <SelectedBar desk={desk} drawing={selected} row={row} setRow={setRow} />;
}

function SelectedBar({ desk, drawing, row, setRow }: { desk: DrawingDesk; drawing: ChartDrawing; row: StyleRow | null; setRow: (row: StyleRow | null) => void }) {
  const style = resolveStyle(drawing);
  const blocked = nudgeBlocked(drawing, desk.locked, desk.tick);
  const handle = desk.handle;
  const anchorPrice = handle !== null ? anchorsOf(drawing)[handle]?.p : undefined;
  const target = handle !== null ? `端点 ${handle + 1}${anchorPrice !== undefined && Number.isFinite(anchorPrice) ? ` · ${priceLabel(anchorPrice, desk.tick)}` : ""}` : "整条线";
  const patch = (change: Parameters<DrawingDesk["patch"]>[1]) => { void desk.patch(drawing.id, change); };
  const swatch = style.color.startsWith("#") ? style.color : `var(${DRAWING_COLOR_TOKENS[style.color] ?? "--user-drawing"})`;
  const toggle = (next: StyleRow) => setRow(row === next ? null : next);
  return (
    <div className="fdm-draw-bar" role="toolbar" aria-label={`${toolSpec(drawing.kind).label}的样式`} data-agent-target="chart:drawing-style">
      <div className="fdm-draw-bar-head">
        <span className="fdm-draw-bar-title"><b>{toolSpec(drawing.kind).label}</b>
          <small data-testid="drawing-nudge-target">±1 跳调：{target}</small></span>
        {handle !== null ? <button type="button" className="app-button" onClick={() => desk.setHandle(null)}>改整条</button> : null}
        <button type="button" className="app-button fdm-draw-icon" aria-label="精确设置" onClick={() => desk.setSettingsId(drawing.id)}><Settings2 aria-hidden="true" /></button>
        <button type="button" className="app-button primary" onClick={() => desk.select(null)}>完成</button>
      </div>
      {row ? <div className="fdm-draw-style-row"><DrawingStyleControls drawing={drawing} onPatch={patch} compact only={row} /></div> : null}
      <div className="fdm-draw-bar-tools">
        <button type="button" className="app-button" aria-label="下移 1 跳" disabled={Boolean(blocked)} onClick={() => desk.nudge(-1)}>−1跳</button>
        <button type="button" className="app-button" aria-label="上移 1 跳" disabled={Boolean(blocked)} onClick={() => desk.nudge(1)}>＋1跳</button>
        <button type="button" className="app-button fdm-draw-icon" aria-label="颜色" aria-expanded={row === "color"} onClick={() => toggle("color")}>
          <i className="fdm-draw-swatch" style={{ background: swatch }} /></button>
        <button type="button" className="app-button" aria-label="粗细" aria-expanded={row === "width"} onClick={() => toggle("width")}>粗细</button>
        <button type="button" className="app-button" aria-label="线型" aria-expanded={row === "dash"} onClick={() => toggle("dash")}>线型</button>
        <button type="button" className="app-button fdm-draw-icon" aria-pressed={style.locked} aria-label={style.locked ? "解锁" : "锁定"}
          onClick={() => patch({ style: { locked: !style.locked } })}>{style.locked ? <Lock aria-hidden="true" /> : <LockOpen aria-hidden="true" />}</button>
        <button type="button" className="app-button danger fdm-draw-icon" aria-label="删除这条线" onClick={() => desk.remove(drawing.id)}><Trash2 aria-hidden="true" /></button>
      </div>
      {blocked ? <small className="fdm-draw-reason">{blocked}</small> : null}
    </div>
  );
}

const LOUPE = 112;
const ZOOM = 2;

/** The loupe above a finger dragging one anchor: the chart around the finger at twice the size (from the still taken
 *  as the drag began), a crosshair on the exact spot and the anchor's real price. */
export function DrawingLoupe({ drag, tick }: { drag: HandleDrag; tick?: number | null }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const target = canvas.current;
    const context = target?.getContext?.("2d");
    if (!target || !context) return;
    const ratio = window.devicePixelRatio || 1;
    target.width = LOUPE * ratio;
    target.height = LOUPE * ratio;
    context.clearRect(0, 0, target.width, target.height);
    const image = drag.image;
    if (image && drag.width > 0) {
      const scale = image.width / drag.width;
      const span = (LOUPE / ZOOM) * scale;
      context.drawImage(image, drag.x * scale - span / 2, drag.y * scale - span / 2, span, span, 0, 0, target.width, target.height);
    }
  }, [drag]);
  const top = Math.max(8, drag.clientY - LOUPE - 36);
  const left = Math.min(Math.max(8, drag.clientX - LOUPE / 2), (typeof window === "undefined" ? 390 : window.innerWidth) - LOUPE - 8);
  return (
    <div className="fdm-draw-loupe" role="status" aria-label={`端点 ${priceLabel(drag.anchor.p, tick)}`} data-testid="drawing-loupe" style={{ top, left }}>
      <canvas ref={canvas} aria-hidden="true" />
      <i className="fdm-draw-loupe-cross" aria-hidden="true" />
      <b>{priceLabel(drag.anchor.p, tick)}</b>
    </div>
  );
}

/** 精确设置 on the phone: the computer's dialog, laid over the page as a sheet. */
export function MobileDrawingSettings({ desk, period }: { desk: DrawingDesk; period: number }) {
  const drawing = desk.items.find(item => item.id === desk.settingsId);
  if (!drawing) return null;
  return (
    <div className="fdm-sheet-backdrop fdm-draw-settings-layer" onClick={event => { if (event.target === event.currentTarget) desk.setSettingsId(null); }}>
      <DrawingSettingsDialog key={drawing.id} drawing={drawing} tick={desk.tick} period={period} onClose={() => desk.setSettingsId(null)}
        onSave={(patch, defaults) => desk.saveSettings(drawing, patch, defaults)} />
    </div>
  );
}

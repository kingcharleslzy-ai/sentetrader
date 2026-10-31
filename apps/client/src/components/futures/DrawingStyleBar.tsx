import { useEffect, useRef } from "react";
import { Bell, Lock, LockOpen, MoveHorizontal, Plus, Settings2, Trash2 } from "lucide-react";
import { DRAWING_COLOR_PRESETS, EXTENDABLE_KINDS, defaultWidth, resolveStyle, toolSpec, type ChartDrawing, type DrawingDash, type DrawingStyle } from "../../chartDrawingModel";
import type { DrawingPatch } from "../../hooks/useChartDrawings";

export const DASH_OPTIONS: ReadonlyArray<{ value: DrawingDash; label: string; glyph: string }> = [
  { value: "solid", label: "实线", glyph: "—" }, { value: "dashed", label: "虚线", glyph: "- -" }, { value: "dotted", label: "点线", glyph: "···" },
];
export const WIDTH_OPTIONS: readonly number[] = [1, 2, 3, 4];
/** 1–4 plus the kind's own width (a 趋势线 draws at 1.5), so the checked width is the one drawn; the own
 *  width is written as null (the Runtime stores whole widths only). */
function widthOptions(drawing: ChartDrawing): number[] {
  return [...new Set([...WIDTH_OPTIONS, defaultWidth(drawing.kind)])].sort((a, b) => a - b);
}

/** The colour / width / dash controls, shared by the style bar, the drawing menu's 颜色 / 粗细 / 线型 submenu and the
 *  phone's bottom bar (one row at a time, `only`). */
export function DrawingStyleControls({ drawing, onPatch, compact, only }: { drawing: ChartDrawing; onPatch: (patch: DrawingPatch) => void; compact?: boolean;
  only?: "color" | "width" | "dash" }) {
  const style = resolveStyle(drawing);
  const own: DrawingStyle = drawing.style ?? {};
  const custom = own.color?.startsWith("#") ? own.color : "";
  // The colour picker reports every step of a drag: write the last one.
  const pickTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(pickTimer.current), []);
  const pick = (color: string) => {
    window.clearTimeout(pickTimer.current);
    pickTimer.current = window.setTimeout(() => onPatch({ style: { color } }), 250);
  };
  return (
    <>
      {only && only !== "color" ? null : <span className="fdw-swatches" role="radiogroup" aria-label="颜色">
        {DRAWING_COLOR_PRESETS.map(preset => (
          <button type="button" role="radio" key={preset.color} className="fdw-swatch" data-color={preset.color} aria-checked={style.color === preset.color}
            aria-label={`颜色：${preset.label}`} title={preset.label} onClick={() => onPatch({ style: { color: preset.color } })} />
        ))}
        <label className="fdw-swatch fdw-swatch-custom" data-checked={Boolean(custom)} title="自定义颜色" style={custom ? { background: custom } : undefined}>
          {custom ? null : <Plus aria-hidden="true" />}
          <input type="color" aria-label="自定义颜色" defaultValue={custom || "#8ea8ff"} onChange={event => pick(event.target.value)} />
        </label>
      </span>}
      {compact || only ? null : <span className="fdw-style-sep" aria-hidden="true" />}
      {only && only !== "width" ? null : <span className="fdw-style-seg" role="radiogroup" aria-label="粗细">
        {widthOptions(drawing).map(width => (
          <button type="button" role="radio" key={width} aria-checked={style.width === width}
            aria-label={`粗细 ${width}`} onClick={() => onPatch({ style: { width: width === defaultWidth(drawing.kind) ? null : width } })}>{width}</button>
        ))}
      </span>}
      {only && only !== "dash" ? null : <span className="fdw-style-seg" role="radiogroup" aria-label="线型">
        {DASH_OPTIONS.map(option => (
          <button type="button" role="radio" key={option.value} aria-checked={style.dash === option.value} aria-label={option.label} title={option.label}
            onClick={() => onPatch({ style: { dash: option.value } })}>{option.glyph}</button>
        ))}
      </span>}
    </>
  );
}

/**
 * 画线第二版 (2026-10-09, 样稿 ①): the floating bar over a selected drawing — 5 preset colours and a
 * custom one, width 1–4, solid / dashed / dotted, 延长, 精确设置, 设到价提醒, 锁定 and 删除 (danger).
 * Opened by a click on the drawing or `disclosure: chart-drawing-style {id}`.
 */
export function DrawingStyleBar({ drawing, position, onPatch, onSettings, onAlert, alertBlocked, onDelete }: {
  drawing: ChartDrawing;
  position: { left: number; top: number };
  onPatch: (patch: DrawingPatch) => void;
  onSettings: () => void;
  onAlert: () => void;
  /** Why 设到价提醒 is off for this drawing (a shape, no alerts here), or null. */
  alertBlocked: string | null;
  onDelete: () => void;
}) {
  const style = resolveStyle(drawing);
  const extendable = EXTENDABLE_KINDS.has(drawing.kind);
  const extended = style.extendLeft && style.extendRight;
  return (
    <div className="fdw-style-bar" role="toolbar" aria-label={`${toolSpec(drawing.kind).label}的样式`} data-agent-target="chart:drawing-style" style={{ left: position.left, top: position.top }}
      onPointerDown={event => event.stopPropagation()}>
      <DrawingStyleControls drawing={drawing} onPatch={onPatch} />
      <span className="fdw-style-sep" aria-hidden="true" />
      {extendable ? <button type="button" className="fdw-style-btn" aria-pressed={extended} aria-label="向两端延长" title="向两端延长"
        onClick={() => onPatch({ style: { extendLeft: !extended, extendRight: !extended } })}><MoveHorizontal aria-hidden="true" /></button> : null}
      <button type="button" className="fdw-style-btn" aria-label="精确设置" title="精确设置：直接输入时间和价格" onClick={onSettings}><Settings2 aria-hidden="true" /></button>
      <button type="button" className="fdw-style-btn" aria-label="设到价提醒" title={alertBlocked ?? "在这条线设到价提醒"} disabled={Boolean(alertBlocked)} onClick={onAlert}><Bell aria-hidden="true" /></button>
      <button type="button" className="fdw-style-btn" aria-pressed={style.locked} aria-label={style.locked ? "解锁" : "锁定"} title={style.locked ? "解锁：可以拖动" : "锁定：不能拖动"}
        onClick={() => onPatch({ style: { locked: !style.locked } })}>{style.locked ? <Lock aria-hidden="true" /> : <LockOpen aria-hidden="true" />}</button>
      <button type="button" className="fdw-style-btn danger" aria-label="删除这条线" title="删除这条线" onClick={onDelete}><Trash2 aria-hidden="true" /></button>
    </div>
  );
}

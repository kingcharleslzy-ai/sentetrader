import { useEffect, useRef, useState } from "react";
import {
  DEFAULT_LEVELS, DRAWING_LABEL_MAX, DRAWING_TEXT_MAX, EXTENDABLE_KINDS, TIMELESS_KINDS, anchorsOf, drawingTitle, placementPoints, resolveStyle,
  tickDecimals, type Anchor, type ChartDrawing, type DrawingDefaultStyle
} from "../../chartDrawingModel";
import type { DrawingPatch } from "../../hooks/useChartDrawings";
import { Switch } from "../settings/controls";

const CHINA_OFFSET = 8 * 3600;
/** Seconds → "YYYY-MM-DDTHH:MM" in China time (a datetime-local value). */
export function toChinaInput(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  return new Date((seconds + CHINA_OFFSET) * 1000).toISOString().slice(0, 16);
}
/** A datetime-local value read as China time → seconds, or null. */
export function fromChinaInput(value: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value.trim());
  if (!match) return null;
  const [, y, m, d, h, mi] = match.map(Number);
  return Date.UTC(y, m - 1, d, h, mi) / 1000 - CHINA_OFFSET;
}

function anchorNames(drawing: ChartDrawing, count: number): string[] {
  switch (drawing.kind) {
    case "hline": return ["价格"];
    case "zone": return ["上沿", "下沿"];
    case "vline": return ["时间"];
    case "hray": case "text": case "price_label": return ["位置"];
    case "channel": return ["第 1 点", "第 2 点", "宽度点"];
    case "triangle": return ["顶点 1", "顶点 2", "顶点 3"];
    case "polyline": return Array.from({ length: count }, (_, index) => `第 ${index + 1} 点`);
    default: return ["起点", "终点"];
  }
}

/**
 * 画线第二版 (2026-10-09, 样稿 ②): a drawing's 精确设置 — each anchor's time and price typed in (prices
 * land on the tick grid), the label or a 文字's words, a 黄金分割 / 百分比线's ratios, 向左 / 向右延长,
 * 显示价格标签, 在所有周期显示, and 存为这类线的默认样式 (account preferences, per tool). A locked
 * drawing keeps its place: its anchor fields are read-only until it is unlocked.
 */
export function DrawingSettingsDialog({ drawing, tick, period, onSave, onClose }: {
  drawing: ChartDrawing;
  tick?: number | null;
  /** The chart shown: 在所有周期显示 off pins a drawing without one (a first-version line) here. */
  period: number;
  onSave: (patch: DrawingPatch, defaults: DrawingDefaultStyle | null) => Promise<boolean>;
  onClose: () => void;
}) {
  const style = resolveStyle(drawing);
  const anchors = anchorsOf(drawing);
  const timeless = TIMELESS_KINDS.has(drawing.kind);
  const decimals = tick ? tickDecimals(tick) : 2;
  const [rows, setRows] = useState(() => anchors.map(anchor => ({ time: toChinaInput(anchor.t), price: Number.isFinite(anchor.p) ? anchor.p.toFixed(decimals) : "" })));
  const [label, setLabel] = useState(drawing.label);
  const levels = drawing.kind === "fib" || drawing.kind === "percent" ? drawing.points.levels ?? DEFAULT_LEVELS[drawing.kind] ?? [] : null;
  const [levelText, setLevelText] = useState(levels ? levels.map(level => Number((level * 100).toFixed(2))).join(", ") : "");
  const [extendLeft, setExtendLeft] = useState(style.extendLeft);
  const [extendRight, setExtendRight] = useState(style.extendRight);
  const [showPrice, setShowPrice] = useState(style.showPrice);
  const [allPeriods, setAllPeriods] = useState(drawing.style?.allPeriods !== false);
  const [saveDefault, setSaveDefault] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const names = anchorNames(drawing, anchors.length);
  const locked = style.locked;
  const textKind = drawing.kind === "text";
  // The chart re-renders on every tick: focus once when the dialog opens, read the latest onClose by ref.
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    ref.current?.querySelector<HTMLInputElement>(textKind ? "input[data-field=label]" : "input:not([disabled])")?.focus();
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") { event.stopPropagation(); closeRef.current(); } };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [textKind]);

  const submit = async () => {
    const next: Anchor[] = [];
    for (const [index, row] of rows.entries()) {
      // The field shows minutes: an untouched time keeps the bar's own timestamp.
      const t = timeless ? 0 : row.time === toChinaInput(anchors[index].t) ? anchors[index].t : fromChinaInput(row.time);
      const p = drawing.kind === "vline" ? anchors[index].p : Number(row.price);
      if (t === null) { setError(`${names[index]}的时间看不懂，写成 年-月-日 时:分`); return; }
      if (drawing.kind !== "vline" && !(Number.isFinite(p) && p > 0)) { setError(`${names[index]}的价格要是大于 0 的数`); return; }
      next.push({ t, p });
    }
    let parsedLevels: number[] | undefined;
    if (levels) {
      const values = levelText.split(/[,，\s]+/).filter(Boolean).map(Number);
      if (!values.length || values.some(value => !Number.isFinite(value) || Math.abs(value) > 500)) { setError("比例写成百分数，用逗号隔开，如 0, 38.2, 61.8, 100"); return; }
      parsedLevels = [...new Set(values.map(value => Number((value / 100).toFixed(6))))].sort((a, b) => a - b);
    }
    const max = textKind ? DRAWING_TEXT_MAX : DRAWING_LABEL_MAX;
    if ([...label.trim()].length > max) { setError(`${textKind ? "文字" : "标签"}最多 ${max} 个字`); return; }
    const pin = !allPeriods && typeof drawing.style?.period !== "number" ? { period } : {};
    const styleChange = { extendLeft, extendRight, showPrice, allPeriods, ...pin };
    const moved = next.some((anchor, index) => anchor.t !== anchors[index].t || (Number.isFinite(anchor.p) && Math.abs(anchor.p - anchors[index].p) > 1e-9))
      || (parsedLevels && JSON.stringify(parsedLevels) !== JSON.stringify(levels));
    const patch: DrawingPatch = {
      ...(moved && !locked ? { points: placementPoints(drawing.kind, next, tick, parsedLevels ?? drawing.points.levels) } : {}),
      label: label.trim(),
      style: EXTENDABLE_KINDS.has(drawing.kind) ? styleChange : { showPrice, allPeriods, ...pin },
    };
    const own = drawing.style ?? {};
    // Every key written (null = the tool's own look), so an older default never outlives this one.
    const defaults: DrawingDefaultStyle | null = saveDefault ? {
      color: own.color ?? null, width: own.width ?? null, dash: own.dash ?? null,
      extendLeft: EXTENDABLE_KINDS.has(drawing.kind) ? extendLeft : null, extendRight: EXTENDABLE_KINDS.has(drawing.kind) ? extendRight : null,
      showPrice, allPeriods,
    } : null;
    setBusy(true);
    setError("");
    const saved = await onSave(patch, defaults);
    setBusy(false);
    if (saved) onClose(); else setError("没有保存，原因见图表上方的提示");
  };

  return (
    <div className="fdw-draw-settings" role="dialog" aria-label={`精确设置 · ${drawingTitle(drawing, tick)}`} data-agent-target="chart:drawing-settings" ref={ref}
      onPointerDown={event => event.stopPropagation()}
      onKeyDown={event => { if (event.key === "Enter" && (event.target as HTMLElement).tagName === "INPUT") { event.preventDefault(); void submit(); } }}>
      <header><strong>精确设置 · {drawingTitle(drawing, tick)}</strong><button type="button" aria-label="关闭" onClick={onClose}>×</button></header>
      <div className="fdw-draw-settings-body">
        {rows.map((row, index) => (
          <div className="fdw-draw-field" key={index}>
            <span>{names[index]}</span>
            <span className="fdw-draw-inputs">
              {timeless ? null : <input type="datetime-local" aria-label={`${names[index]}时间`} value={row.time} disabled={locked}
                onChange={event => setRows(list => list.map((item, at) => at === index ? { ...item, time: event.target.value } : item))} />}
              {drawing.kind === "vline" ? null : <input type="number" inputMode="decimal" aria-label={`${names[index]}价格`} value={row.price} disabled={locked}
                step={tick && tick > 0 ? tick : "any"} onChange={event => setRows(list => list.map((item, at) => at === index ? { ...item, price: event.target.value } : item))} />}
            </span>
          </div>
        ))}
        {locked ? <small className="fdw-draw-reason">已锁定，解锁后才能改位置</small> : null}
        <div className="fdw-draw-field">
          <span>{textKind ? "文字" : "标签"}</span>
          <input type="text" data-field="label" aria-label={textKind ? "文字内容" : "标签"} value={label} maxLength={textKind ? DRAWING_TEXT_MAX : DRAWING_LABEL_MAX}
            placeholder={textKind ? "写点什么" : "可不填"} onChange={event => setLabel(event.target.value)} />
        </div>
        {levels ? (
          <div className="fdw-draw-field">
            <span>比例（%）</span>
            <input type="text" aria-label="分割比例" value={levelText} onChange={event => setLevelText(event.target.value)} />
          </div>
        ) : null}
        {EXTENDABLE_KINDS.has(drawing.kind) ? <>
          <div className="fdw-draw-field"><span>向左延长</span><Switch label="向左延长" checked={extendLeft} onChange={setExtendLeft} /></div>
          <div className="fdw-draw-field"><span>向右延长</span><Switch label="向右延长" checked={extendRight} onChange={setExtendRight} /></div>
        </> : null}
        <div className="fdw-draw-field"><span>显示价格标签</span><Switch label="显示价格标签" checked={showPrice} onChange={setShowPrice} /></div>
        <div className="fdw-draw-field"><span>在所有周期显示</span><Switch label="在所有周期显示" checked={allPeriods} onChange={setAllPeriods} /></div>
        <label className="fdw-draw-check"><input type="checkbox" checked={saveDefault} onChange={event => setSaveDefault(event.target.checked)} />存为这类线的默认样式</label>
        {error ? <small className="fdw-draw-error" role="alert">{error}</small> : null}
      </div>
      <footer>
        <button type="button" className="app-button" onClick={onClose}>取消</button>
        <button type="button" className="app-button primary" disabled={busy} onClick={() => void submit()}>{busy ? "保存中…" : "确定"}</button>
      </footer>
    </div>
  );
}

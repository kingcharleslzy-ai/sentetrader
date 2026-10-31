import { useEffect, useRef, type ReactNode } from "react";
import { Stepper, Switch, Select } from "./settings/controls";
import type { DrawingDefaults } from "../chartDrawingModel";
import {
  MA_LINES_MAX, MA_PERIOD_MAX, MA_PERIOD_MIN, SUB_PANE_NAMES, SUB_PANE_TYPES, SUB_PANES_MAX,
  type BollConfig, type ChartIndicatorConfig, type KdParams, type MacdParams, type MaLineConfig, type SarConfig, type SubPaneType
} from "./chartIndicators";

/**
 * 图表指标可配置 (2026-10-02): the indicator editors. The chart's label popovers and the
 * 设置 → 图表 → 指标参数与均线 page render the same components, so a change works the same
 * wherever the user makes it: every edit emits a complete `preferences.chart` sub-object
 * (`maLines`, `boll`, `subPaneList`, `kdParams`, `macdParams`, `rsiParams`) that the caller
 * saves through the account preference path (guests keep it local).
 */

/** What an editor hands its caller: complete sub-objects of `preferences.chart`. */
/** One write to the account's chart preferences: the indicators (就地编辑, since 手机看盘第二批
 *  also SAR / CCI / 均线静音) and, since 画线第二版, the drawing switches and per-tool default styles. */
export type ChartIndicatorPatch = Partial<{ maLines: MaLineConfig[]; boll: BollConfig; sar: SarConfig; maMuted: number[]; subPaneList: SubPaneType[]; kdParams: KdParams; macdParams: MacdParams;
  rsiParams: { period: number }; cciParams: { period: number };
  drawingMagnet: boolean; drawingsLocked: boolean; drawingsHidden: boolean; drawingDefaults: DrawingDefaults }>;

interface EditorProps {
  indicators: ChartIndicatorConfig;
  onChange: (patch: ChartIndicatorPatch) => void;
  /** The save path's inline status (已保存 / 越界原因); the settings page and popovers share it. */
  status?: string;
}

/** The settings controls carry only aria labels; this gives each one its visible word. */
function Labeled({ label, children }: { label: string; children: ReactNode }) {
  return <span className="fdw-ind-field"><span className="fdw-ind-field-label">{label}</span>{children}</span>;
}

/**
 * 均线与布林带: one row per MA line (period stepper, 显示 switch, delete), an add button
 * capped at eight lines, and the BOLL row (switch, period, width).
 */
export function MaLinesEditor({ indicators, onChange, status }: EditorProps) {
  const lines = indicators.maLines;
  const setLines = (maLines: MaLineConfig[]) => onChange({ maLines });
  return <div className="fdw-ind-editor">
    {/* One line per MA (均线 N · period · on/off · delete) keeps 布林带 in view without scrolling. */}
    {lines.map((line, index) => <div className="fdw-ind-row fdw-ind-ma-row" key={index}>
      <span className="fdw-ind-row-label">均线 {index + 1}</span>
      <Stepper label={`第 ${index + 1} 条均线周期`} value={line.period} min={MA_PERIOD_MIN} max={MA_PERIOD_MAX} unit="根"
        onChange={value => { if (value != null) setLines(lines.map((item, position) => position === index ? { ...item, period: Math.round(value) } : item)); }} />
      <Switch label={`显示第 ${index + 1} 条均线`} checked={line.enabled}
        onChange={enabled => setLines(lines.map((item, position) => position === index ? { ...item, enabled } : item))} />
      <button type="button" className="fdw-ind-remove" disabled={lines.length <= 1} title={lines.length <= 1 ? "至少保留一条均线；不想看可以关掉显示" : "删除这条均线"}
        onClick={() => setLines(lines.filter((_, position) => position !== index))}>删除</button>
    </div>)}
    <button type="button" className="fdw-ind-add" disabled={lines.length >= MA_LINES_MAX}
      title={lines.length >= MA_LINES_MAX ? `最多 ${MA_LINES_MAX} 条均线` : undefined}
      onClick={() => setLines([...lines, { period: 120, enabled: true }])}>＋ 加一条均线{lines.length >= MA_LINES_MAX ? `（最多 ${MA_LINES_MAX} 条）` : ""}</button>
    <div className="fdw-ind-row">
      <span className="fdw-ind-row-label">布林带</span>
      <Labeled label="显示"><Switch label="布林带" checked={indicators.boll.enabled} onChange={enabled => onChange({ boll: { ...indicators.boll, enabled } })} /></Labeled>
      <Labeled label="周期"><Stepper label="布林周期" value={indicators.boll.period} min={5} max={100} unit="根" disabled={!indicators.boll.enabled}
        onChange={value => { if (value != null) onChange({ boll: { ...indicators.boll, period: Math.round(value) } }); }} /></Labeled>
      <Labeled label="宽度"><Stepper label="布林宽度" value={indicators.boll.width} min={1} max={4} step={0.5} disabled={!indicators.boll.enabled}
        onChange={value => { if (value != null) onChange({ boll: { ...indicators.boll, width: value } }); }} /></Labeled>
    </div>
    <SarRow indicators={indicators} onChange={onChange} />
    {status ? <span className="fdw-ind-status" role="status">{status}</span> : null}
  </div>;
}

/** 抛物线 SAR (2026-10-09): the switch and its two factors, the BOLL row's shape. */
export function SarRow({ indicators, onChange }: Omit<EditorProps, "status">) {
  const sar = indicators.sar;
  return <div className="fdw-ind-row">
    <span className="fdw-ind-row-label">SAR</span>
    <Labeled label="显示"><Switch label="抛物线 SAR" checked={sar.enabled} onChange={enabled => onChange({ sar: { ...sar, enabled } })} /></Labeled>
    <Labeled label="步长"><Stepper label="SAR 步长" value={sar.step} min={0.01} max={0.1} step={0.01} disabled={!sar.enabled}
      onChange={value => { if (value != null) onChange({ sar: { ...sar, step: Math.round(value * 100) / 100 } }); }} /></Labeled>
    <Labeled label="上限"><Stepper label="SAR 上限" value={sar.max} min={0.1} max={0.5} step={0.05} disabled={!sar.enabled}
      onChange={value => { if (value != null) onChange({ sar: { ...sar, max: Math.round(value * 100) / 100 } }); }} /></Labeled>
  </div>;
}

/** One sub-pane slot: which indicator it shows (types used elsewhere are disabled and say where),
 * that indicator's parameters, and the slot's removal. */
export function SubPaneEditor({ index, indicators, onChange, status }: EditorProps & { index: number }) {
  const type = indicators.subPaneList[index];
  const setList = (subPaneList: SubPaneType[]) => onChange({ subPaneList });
  const setKd = (patch: Partial<KdParams>) => onChange({ kdParams: { ...indicators.kdParams, ...patch } });
  const setMacd = (patch: Partial<MacdParams>) => onChange({ macdParams: { ...indicators.macdParams, ...patch } });
  return <div className="fdw-ind-editor">
    <div className="fdw-ind-row">
      <span className="fdw-ind-row-label">指标</span>
      <Select label={`第 ${index + 1} 格指标`} value={type}
        options={SUB_PANE_TYPES.map(candidate => {
          // The slot's own type stays selectable; a type another slot shows says where it lives.
          const usedAt = indicators.subPaneList.indexOf(candidate);
          const elsewhere = usedAt >= 0 && usedAt !== index;
          return { value: candidate, label: elsewhere ? `${SUB_PANE_NAMES[candidate]}（已在第 ${usedAt + 1} 格）` : SUB_PANE_NAMES[candidate], disabled: elsewhere };
        })}
        onChange={value => setList(indicators.subPaneList.map((item, position) => position === index ? value as SubPaneType : item))} />
      <button type="button" className="fdw-ind-remove" onClick={() => setList(indicators.subPaneList.filter((_, position) => position !== index))}>移除这一格</button>
    </div>
    {type === "kd" ? <div className="fdw-ind-row">
      <Labeled label="周期"><Stepper label="KD 周期" value={indicators.kdParams.k} min={2} max={100} unit="根" onChange={value => { if (value != null) setKd({ k: Math.round(value) }); }} /></Labeled>
      <Labeled label="平滑 K"><Stepper label="KD 平滑 K" value={indicators.kdParams.d} min={1} max={30} onChange={value => { if (value != null) setKd({ d: Math.round(value) }); }} /></Labeled>
      <Labeled label="平滑 D"><Stepper label="KD 平滑 D" value={indicators.kdParams.smooth} min={1} max={30} onChange={value => { if (value != null) setKd({ smooth: Math.round(value) }); }} /></Labeled>
    </div> : null}
    {type === "macd" ? <div className="fdw-ind-row">
      <Labeled label="快线"><Stepper label="MACD 快线" value={indicators.macdParams.fast} min={2} max={100} onChange={value => { if (value != null) setMacd({ fast: Math.round(value) }); }} /></Labeled>
      <Labeled label="慢线"><Stepper label="MACD 慢线" value={indicators.macdParams.slow} min={3} max={200} onChange={value => { if (value != null) setMacd({ slow: Math.round(value) }); }} /></Labeled>
      <Labeled label="信号"><Stepper label="MACD 信号" value={indicators.macdParams.signal} min={1} max={100} onChange={value => { if (value != null) setMacd({ signal: Math.round(value) }); }} /></Labeled>
    </div> : null}
    {type === "rsi" ? <div className="fdw-ind-row">
      <Labeled label="周期"><Stepper label="RSI 周期" value={indicators.rsiParams.period} min={2} max={100} unit="根" onChange={value => { if (value != null) onChange({ rsiParams: { period: Math.round(value) } }); }} /></Labeled>
    </div> : null}
    {type === "cci" ? <div className="fdw-ind-row">
      <Labeled label="周期"><Stepper label="CCI 周期" value={indicators.cciParams.period} min={2} max={100} unit="根" onChange={value => { if (value != null) onChange({ cciParams: { period: Math.round(value) } }); }} /></Labeled>
    </div> : null}
    {status ? <span className="fdw-ind-status" role="status">{status}</span> : null}
  </div>;
}

/** The ＋副图 chooser: every type not already shown; the list explains where a used one lives. */
export function AddPaneChooser({ indicators, onAdd, status }: Omit<EditorProps, "onChange"> & { onAdd: (type: SubPaneType) => void }) {
  const full = indicators.subPaneList.length >= SUB_PANES_MAX;
  return <div className="fdw-ind-editor">
    {SUB_PANE_TYPES.map(type => {
      const usedAt = indicators.subPaneList.indexOf(type);
      return <button type="button" className="fdw-ind-add" key={type} disabled={usedAt >= 0 || full}
        title={usedAt >= 0 ? `已在第 ${usedAt + 1} 格` : full ? `最多 ${SUB_PANES_MAX} 格副图` : undefined}
        onClick={() => onAdd(type)}>
        {SUB_PANE_NAMES[type]}副图{usedAt >= 0 ? `（已在第 ${usedAt + 1} 格）` : ""}
      </button>;
    })}
    {status ? <span className="fdw-ind-status" role="status">{status}</span> : null}
  </div>;
}

/**
 * The fixed-position popover the chart labels open. `position: fixed` (like the chart's context
 * menu) keeps it clear of the panes' `overflow: hidden`; Esc or a pointer down outside closes it.
 */
export function ChartPopover({ title, anchorRect, onClose, children, width = 340 }: {
  title: string; anchorRect: DOMRect; onClose: () => void; children: ReactNode; width?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && ref.current?.contains(event.target)) return;
      onClose();
    };
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("pointerdown", outside, true); document.removeEventListener("keydown", onKey); };
  }, [onClose]);
  // `.fdw-ind-popover` has min-width: min(360px, 100vw - 16px); clamp with the width it really renders at.
  const rendered = Math.max(width, Math.min(360, window.innerWidth - 16));
  const left = Math.max(8, Math.min(anchorRect.left, window.innerWidth - rendered - 8));
  const top = Math.min(anchorRect.bottom + 6, Math.max(8, window.innerHeight - 240));
  return (
    <div className="fdw-ind-popover" role="dialog" aria-label={title} style={{ position: "fixed", left, top, width }} ref={ref}>
      <div className="fdw-ind-popover-title"><strong>{title}</strong><button type="button" aria-label="关闭" onClick={onClose}>×</button></div>
      <div className="fdw-ind-popover-body">{children}</div>
    </div>
  );
}

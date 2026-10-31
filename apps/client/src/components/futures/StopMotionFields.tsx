import type { StopMotion, TrailingMode } from "../../api";
import { HelpTip } from "../Blocks";

const MODES: { value: "" | TrailingMode; label: string; unit: string }[] = [
  { value: "", label: "固定", unit: "" },
  { value: "ticks", label: "追踪 · 按跳数", unit: "跳" },
  { value: "percent", label: "追踪 · 按百分比", unit: "%" },
  { value: "atr_chandelier", label: "追踪 · ATR 吊灯", unit: "倍 ATR" },
  { value: "bar_extreme", label: "追踪 · 前 N 根 K 线", unit: "根 K 线" },
];
const DEFAULT_VALUE: Record<TrailingMode, number> = { ticks: 10, percent: 1, atr_chandelier: 3, bar_extreme: 3 };

/**
 * 手动止损怎么动 (2026-10-25 第 4 批), shared by the 下单票 and a position row's 保护 form: 固定, or 追踪 (the
 * strategy's own trailing — tightened on every fresh price, never loosened; the ATR chandelier and 前 N 根 K 线
 * read the chart's period), and 浮盈 1R 后移到成本. Shown once a stop price is filled in; both need one.
 */
export function StopMotionFields({ value, onChange, atrTimeframe }: {
  value: StopMotion;
  onChange: (next: StopMotion) => void;
  /** The chart's period in seconds, which the ATR chandelier measures on. */
  atrTimeframe: number;
}) {
  const mode = value.trailing?.mode ?? "";
  const unit = MODES.find(item => item.value === mode)?.unit ?? "";
  return <div className="fdw-stop-motion" data-testid="stop-motion">
    <label>
      <span>止损方式<HelpTip helpKey="ticket:stop-motion" label="止损方式说明">
        追踪：价格往有利方向走，止损按这里的距离跟着收紧，只紧不松，盘中每个新价都会算。前 N 根 K 线：多单止损跟到最近 N 根收完 K 线的最低价，空单跟到最高价，按图表周期算。浮盈 1R 后移到成本：1R 是开仓价到止损的距离，浮盈到这么多时把止损移到开仓价。</HelpTip></span>
      <select aria-label="止损方式" value={mode} onChange={event => {
        const next = event.target.value as "" | TrailingMode;
        onChange({ ...value, trailing: next ? { mode: next, value: DEFAULT_VALUE[next], atrTimeframe } : undefined });
      }}>
        {MODES.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
      </select>
    </label>
    {value.trailing ? <label>
      <span>{value.trailing.mode === "bar_extreme" ? "前几根" : "追踪距离"}</span>
      <span className="fdw-stop-motion-value">
        <input aria-label="追踪距离" type="number" inputMode="decimal" min="0" step={value.trailing.mode === "ticks" || value.trailing.mode === "bar_extreme" ? 1 : 0.1}
          value={value.trailing.value} onChange={event => {
            const next = Number(event.target.value);
            if (Number.isFinite(next) && next > 0) onChange({ ...value, trailing: { ...value.trailing!, value: next } });
          }} />
        <small>{unit}</small>
      </span>
    </label> : null}
    <label className="fdw-stop-motion-check">
      <input type="checkbox" aria-label="浮盈 1R 后移到成本" checked={Boolean(value.breakevenAtR)}
        onChange={event => onChange({ ...value, breakevenAtR: event.target.checked || undefined })} />
      <span>浮盈 1R 后移到成本</span>
    </label>
  </div>;
}

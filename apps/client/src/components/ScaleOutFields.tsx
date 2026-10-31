import type { ReactNode } from "react";
import type { StrategyPlan } from "../strategyApi";

type ScaleTier = NonNullable<StrategyPlan["scaleOut"]>[number];
export const SCALE_OUT_NOTE = "按持仓均价收盘判断，每档每笔持仓只平一次；各档由近到远、单位一致，剩下的照原止损止盈走。";

/** 分批止盈: up to three tiers (盈利跳数 or ATR 倍数, 平掉 %), used by the designer's 高级 fold and the 策略编辑卡's 更多. */
export function ScaleOutFields({ tiers, disabled, onChange, help }: { tiers: ScaleTier[]; disabled: boolean; onChange: (tiers: ScaleTier[]) => void;
  /** The explanation as a "?" beside the title (策略编辑卡) instead of the grey line under the tiers. */
  help?: ReactNode }) {
  const patchTier = (index: number, values: Partial<ScaleTier>) => onChange(tiers.map((tier, at) => at === index ? { ...tier, ...values } : tier));
  const addTier = () => {
    const last = tiers.at(-1);
    onChange([...tiers, last?.profitTicks != null ? { profitTicks: last.profitTicks + 20, profitAtr: null, percent: 25 }
      : { profitAtr: (last?.profitAtr ?? 0) + 1, profitTicks: null, percent: tiers.length ? 25 : 50 }]);
  };
  return <fieldset className="strategy-block-edit" aria-label="分批止盈">
    <legend>分批止盈{help}</legend>
    {tiers.map((tier, index) => <div className="strategy-tier-row" key={index}>
      <label>第 {index + 1} 档 · 目标<select value={tier.profitTicks != null ? "ticks" : "atr"} disabled={disabled}
        onChange={event => patchTier(index, event.target.value === "ticks" ? { profitTicks: 20, profitAtr: null } : { profitAtr: 1, profitTicks: null })}>
        <option value="ticks">盈利跳数</option>
        <option value="atr">盈利 ATR 倍数</option>
      </select></label>
      <label>第 {index + 1} 档 · 目标值
        <input type="number" min="1" value={tier.profitTicks ?? tier.profitAtr ?? ""} disabled={disabled}
          onChange={event => patchTier(index, tier.profitTicks != null ? { profitTicks: Number(event.target.value) } : { profitAtr: Number(event.target.value) })}/></label>
      <label>第 {index + 1} 档 · 平掉 %
        <input type="number" min="1" max="99" value={tier.percent} disabled={disabled} onChange={event => patchTier(index, { percent: Number(event.target.value) })}/></label>
      <button type="button" disabled={disabled} onClick={() => onChange(tiers.filter((_, at) => at !== index))}>删档</button>
    </div>)}
    {tiers.length < 3 && <button type="button" className="strategy-tier-add" disabled={disabled} onClick={addTier}>加一档</button>}
    {help ? null : <small>{SCALE_OUT_NOTE}</small>}
  </fieldset>;
}

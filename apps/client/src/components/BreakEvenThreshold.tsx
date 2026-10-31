import { BREAK_EVEN_AMOUNT_MAX, DEFAULT_BREAK_EVEN_RULE, type BreakEvenRule, type UserPreferences } from "../userPreferences";
import { SettingRow } from "./SettingsPanel";
import { Select, Stepper } from "./settings/controls";

/** 保本提醒的阈值 (2026-10-06): 倍数 = 浮盈达到止损距离的几倍（默认 1，0.5–5），金额 = 浮盈达到几元。
 *  设置 → 提醒 shows it as one dropdown on the 保本提醒 row (2026-10-15); writes notify.breakEvenRule. */
export function readBreakEvenRule(notify: UserPreferences["notify"]): BreakEvenRule {
  return { ...DEFAULT_BREAK_EVEN_RULE, ...(notify.breakEvenRule ?? {}) };
}

const MULTIPLES = [0.5, 1, 1.5, 2, 3];
const AMOUNTS = [500, 1000, 2000, 5000];
const ruleValue = (rule: BreakEvenRule) => rule.mode === "amount" ? `amount:${rule.amount}` : `multiple:${rule.multiple}`;
const ruleLabel = (rule: BreakEvenRule) => rule.mode === "amount" ? `浮盈 ${rule.amount.toLocaleString("zh-CN")} 元` : `赚到 ${rule.multiple} 倍风险`;

/** The threshold as presets (倍数 and 金额); a value set elsewhere (the AI, an older client) stays listed as it is. */
export function BreakEvenSelect({ notify, disabled, onPatch }: {
  notify: UserPreferences["notify"]; disabled?: boolean; onPatch: (patch: Record<string, unknown>) => void;
}) {
  const rule = readBreakEvenRule(notify);
  const presets: BreakEvenRule[] = [...MULTIPLES.map(multiple => ({ ...rule, mode: "multiple" as const, multiple })),
    ...AMOUNTS.map(amount => ({ ...rule, mode: "amount" as const, amount }))];
  const options = presets.some(item => ruleValue(item) === ruleValue(rule)) ? presets : [rule, ...presets];
  return <Select label="保本提醒门槛" value={ruleValue(rule)} disabled={disabled}
    options={options.map(item => ({ value: ruleValue(item), label: ruleLabel(item) }))}
    onChange={value => {
      const [mode, raw] = value.split(":");
      const amount = Number(raw);
      if (!Number.isFinite(amount)) return;
      onPatch({ notify: { breakEvenRule: mode === "amount" ? { ...rule, mode: "amount", amount } : { ...rule, mode: "multiple", multiple: amount } } });
    }} />;
}

export const BREAK_EVEN_USD_LABEL = "外盘（美元）";

/** 外盘美元子账户的金额门槛 (所有者 2026-10-21): shown under 保本提醒 only in the amount mode; writes
 *  notify.breakEvenRule.amountUsd (whole 美元). Empty = null: the foreign positions keep the multiple. */
export function BreakEvenUsdRow({ notify, disabled, onPatch }: {
  notify: UserPreferences["notify"]; disabled?: boolean; onPatch: (patch: Record<string, unknown>) => void;
}) {
  const rule = readBreakEvenRule(notify);
  if (rule.mode !== "amount") return null;
  return <SettingRow label={BREAK_EVEN_USD_LABEL} helpKey="breakeven-usd"
    help={`外盘持仓记在美元子账户，盈亏是美元，不和上面的人民币门槛比，也不换算汇率。填了就按这里的美元浮盈判断；留空则外盘持仓按 ${rule.multiple} 倍风险判断。`}>
    <Stepper label={BREAK_EVEN_USD_LABEL} value={rule.amountUsd ?? null} min={1} max={BREAK_EVEN_AMOUNT_MAX} step={100} unit="美元"
      placeholder="按倍数" disabled={disabled}
      onChange={value => onPatch({ notify: { breakEvenRule: { ...rule, amountUsd: value == null ? null : Math.max(1, Math.trunc(value)) } } })} />
  </SettingRow>;
}

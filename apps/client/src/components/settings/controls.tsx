import type { ReactNode, Ref } from "react";
// The controls own their styles: a surface outside the settings drawer (the
// standalone /trader/admin page) that imports these components gets the same look
// without importing SettingsPanel itself.
import "../SettingsPanel.css";

/**
 * The settings controls (设置重做 step 1). Every setting row uses exactly one of these, so the
 * drawer reads the same everywhere and the observation snapshot sees the same roles: a Switch is a
 * `role=switch` button (`aria-checked`), a Segmented is a `radiogroup` of `radio` buttons
 * (`aria-checked`), a Select is a native `<select>`, a Stepper is a number input between −/+ buttons,
 * a TextInput is a single-line text input, and a Button is a plain button with its status text beside it.
 */

export function Switch({ label, checked, onChange, disabled, on, off }: {
  label: string; checked: boolean; onChange: (value: boolean) => void; disabled?: boolean;
  /** Optional state words shown next to the knob (e.g. 已启用 / 未启用). */
  on?: string; off?: string;
}) {
  return <span className="app-switch-wrap">
    <button type="button" role="switch" aria-checked={checked} aria-label={label} className="app-switch" disabled={disabled}
      onClick={() => onChange(!checked)}><i aria-hidden="true" /></button>
    {on || off ? <span className="app-switch-state">{checked ? on ?? "" : off ?? ""}</span> : null}
  </span>;
}

export interface SegmentedOption<T extends string> { value: T; label: string; disabled?: boolean }

export function Segmented<T extends string>({ label, value, options, onChange, disabled }: {
  label: string; value: T | undefined; options: readonly SegmentedOption<T>[]; onChange: (value: T) => void; disabled?: boolean;
}) {
  return <div className="app-segmented" role="radiogroup" aria-label={label} data-count={options.length}>
    {options.map(option => (
      <button type="button" role="radio" key={option.value} aria-checked={value === option.value} disabled={disabled || option.disabled}
        onClick={() => onChange(option.value)}>{option.label}</button>
    ))}
  </div>;
}

export interface SelectOption { value: string; label: string; disabled?: boolean }

/** A native select; `groups` renders the options under `<optgroup>` headings instead of a flat list. */
export function Select({ label, value, options = [], groups, onChange, disabled }: {
  label: string; value: string; options?: readonly SelectOption[];
  groups?: readonly { label: string; options: readonly SelectOption[] }[];
  onChange: (value: string) => void; disabled?: boolean;
}) {
  const item = (option: SelectOption) => <option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>;
  return <select className="app-select" aria-label={label} value={value} disabled={disabled} onChange={event => onChange(event.target.value)}>
    {groups ? groups.map(group => <optgroup key={group.label} label={group.label}>{group.options.map(item)}</optgroup>) : options.map(item)}
  </select>;
}

export function Stepper({ label, value, onChange, min, max, step = 1, unit, placeholder, disabled }: {
  label: string; value: number | null; onChange: (value: number | null) => void;
  min?: number; max?: number; step?: number; unit?: string; placeholder?: string; disabled?: boolean;
}) {
  const clamp = (next: number) => Math.min(max ?? Number.POSITIVE_INFINITY, Math.max(min ?? Number.NEGATIVE_INFINITY, next));
  const nudge = (direction: 1 | -1) => onChange(clamp((value ?? min ?? 0) + direction * step));
  return <span className="app-stepper">
    <button type="button" aria-label={`${label}减少`} disabled={disabled || (value != null && min != null && value <= min)} onClick={() => nudge(-1)}>−</button>
    <input type="number" aria-label={label} inputMode="decimal" min={min} max={max} step={step} placeholder={placeholder} disabled={disabled}
      value={value ?? ""} onChange={event => {
        const raw = event.target.value;
        if (raw === "") { onChange(null); return; }
        const next = Number(raw);
        if (Number.isFinite(next)) onChange(clamp(next));
      }} />
    <button type="button" aria-label={`${label}增加`} disabled={disabled || (value != null && max != null && value >= max)} onClick={() => nudge(1)}>+</button>
    {unit ? <span className="app-stepper-unit">{unit}</span> : null}
  </span>;
}

/** A single-line text value (后台管理: tier display names). */
/** `password`: a secret the field never shows (type=password); `autoComplete` steers the browser's fill. */
export function TextInput({ label, value, onChange, placeholder, maxLength, disabled, password, autoComplete, inputRef }: {
  label: string; value: string; onChange: (value: string) => void;
  placeholder?: string; maxLength?: number; disabled?: boolean;
  password?: boolean; autoComplete?: string; inputRef?: Ref<HTMLInputElement>;
}) {
  return <input ref={inputRef} type={password ? "password" : "text"} className="app-text-input" aria-label={label} value={value}
    placeholder={placeholder} maxLength={maxLength} disabled={disabled} autoComplete={autoComplete}
    onChange={event => onChange(event.target.value)} />;
}

/** `primary`: the one main action of the surface; `danger`: a destructive or irreversible action (删除, 急停, 清空, 放弃). */
export function Button({ children, onClick, disabled, primary, danger, status, testId }: {
  children: ReactNode; onClick?: () => void; disabled?: boolean; primary?: boolean; danger?: boolean; status?: ReactNode; testId?: string;
}) {
  return <>
    <button type="button" className={primary ? "app-button primary" : danger ? "app-button danger" : "app-button"} disabled={disabled} onClick={onClick} data-agent-target={testId}>{children}</button>
    {status ? <span className="app-setting-status" role="status">{status}</span> : null}
  </>;
}

import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Search, X } from "lucide-react";
import { TIMEFRAMES, operandFor, operandProblem, timeframeLabel, type OperandForm, type RowForm, type Transform } from "../strategyEditor";
import { CATEGORIES, MAX_BARS_AGO, MODIFIER_MAX, OI_QUADRANTS, PICKER_ITEMS, PIVOT_LEVELS, defaultK, indicatorProblem, isCross, itemOf, periodLabel,
  periodRange, usesBasis, usesK, usesPeriod, type CategoryId, type IndicatorName, type Operator, type PickerItem } from "../strategyIndicators";
import "./IndicatorPicker.css";

/**
 * 指标选择器 (策略编辑卡第二版, 2026-10-08; picker.html): one side of a condition chosen from every operand of the rule
 * engine — eight categories with their counts, a search over names, plain lines and English names, each item with one
 * line of what it is; the chosen item's parameters (N, 倍数, 中轨, 档位, 取 N 根均线 / 最高 / 最低, 取 N 根前, 固定周期) at
 * the bottom with the engine's ranges, and 好 / 取消. An operand the engine would refuse on this side is disabled with
 * the reason. The desktop shows it under the row; the phone in the condition's bottom sheet with the categories as a
 * sliding row of chips.
 */
export function PickerPanel({ side, value, op, planTimeframe, spread, ownClock, layout, title, onDone, onCancel, onChange }: {
  side: "left" | "right"; value: OperandForm; op: Operator | ""; planTimeframe: number; spread: boolean;
  /** 方向确认 / 复位 and a group with 方向确认: only the strategy's own period (no 固定周期 choice). */
  ownClock?: boolean;
  layout: "desktop" | "mobile"; title: string;
  /** The desktop's 好 / 取消 (the panel keeps its own draft until 好). */
  onDone?: (value: OperandForm) => void; onCancel?: () => void;
  /** The phone's sheet: every choice goes straight to the sheet's row (its own 好 / 取消 write or drop the row). */
  onChange?: (value: OperandForm) => void;
}) {
  const [own, setOwn] = useState<OperandForm>(value);
  const draft = onChange ? value : own;
  const setDraft = onChange ?? setOwn;
  const current = itemOf(draft.indicator);
  const categories = CATEGORIES.filter(category => side === "right" || category.id !== "number");
  const [category, setCategory] = useState<CategoryId>(current?.categories[0] ?? "common");
  const [query, setQuery] = useState("");
  const search = useRef<HTMLInputElement>(null);
  useEffect(() => { if (layout === "desktop") search.current?.focus({ preventScroll: true }); }, [layout]);
  /** An item is disabled (with the reason) only when none of its lines can go on this side. */
  const problemOf = (item: PickerItem) => {
    const problems = item.lines.map(line => indicatorProblem(line.indicator, side, { spread, op }));
    return problems.every(Boolean) ? problems[0] : null;
  };
  const words = query.trim().toLowerCase();
  const items = useMemo(() => PICKER_ITEMS.filter(item => side === "right" || item.id !== "constant").filter(item => words
    ? [item.label, item.desc, item.keywords ?? "", ...item.lines.map(line => line.label)].join(" ").toLowerCase().includes(words)
    : item.categories.includes(category)), [words, category, side]);
  const pick = (item: PickerItem) => {
    if (current?.id === item.id) return;
    const line = item.lines.find(entry => !indicatorProblem(entry.indicator, side, { spread, op })) ?? item.lines[0];
    setDraft(operandFor(line.indicator, { timeframe: draft.timeframe, value: line.indicator === "constant" ? draft.value ?? 0 : line.indicator === "pivot" ? 0 : null }));
  };
  const problem = draft.indicator ? operandProblem(draft) ?? indicatorProblem(draft.indicator, side, { spread, op }) : "先选一个";
  const done = () => { if (!problem) onDone?.(ownClock && side === "left" ? { ...draft, timeframe: null } : draft); };
  const keys = (event: KeyboardEvent) => {
    if (event.key === "Escape" && onCancel) { event.preventDefault(); event.stopPropagation(); onCancel(); }
  };
  const list = <div className="ip-items" role="group" aria-label={words ? "搜索结果" : CATEGORIES.find(item => item.id === category)?.label}>
    {items.length ? items.map(item => {
      const reason = problemOf(item);
      const on = current?.id === item.id;
      return <button type="button" key={item.id} className="ip-item" aria-pressed={on} aria-label={item.label} disabled={Boolean(reason)} title={reason ?? item.desc}
        onClick={() => pick(item)}>
        <b>{item.label}</b><span>{reason ?? item.desc}</span>
      </button>;
    }) : <p className="ip-empty">没有找到，换个说法，比如“均线”“突破”“持仓”。</p>}
  </div>;
  // A group, not a dialog: it sits inline under its row (a dialog would root the window snapshot at it).
  return <section className="ip" data-layout={layout} role="group" aria-label={title} data-testid="indicator-picker" onKeyDown={keys}>
    {onCancel ? <header className="ip-head"><b>{title}</b>
      <button type="button" className="se-x" aria-label="关闭选择器" onClick={onCancel}><X size={12}/></button></header> : null}
    <label className="ip-search"><Search size={14} aria-hidden="true"/>
      <input ref={search} type="search" aria-label="搜指标" value={query} placeholder="搜指标：CCI、SAR、持仓量、阳线…"
        onChange={event => setQuery(event.target.value)}
        onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); const first = items.find(item => !problemOf(item)); if (first) pick(first); } }}/></label>
    {layout === "mobile" ? <>
      <div className="ip-chips" role="group" aria-label="分类">{categories.map(item => <button type="button" key={item.id} aria-pressed={!words && category === item.id}
        onClick={() => { setQuery(""); setCategory(item.id); }}>{item.label}</button>)}</div>
      {list}
    </> : <div className="ip-grid">
      <div className="ip-cats" role="group" aria-label="分类">{categories.map(item => <button type="button" key={item.id} aria-pressed={!words && category === item.id}
        onClick={() => { setQuery(""); setCategory(item.id); }}>{item.label}<small>{PICKER_ITEMS.filter(entry => entry.categories.includes(item.id)).length}</small></button>)}</div>
      {list}
    </div>}
    <div className="ip-params" aria-label="参数">
      {current ? <OperandParams value={draft} item={current} side={side} planTimeframe={planTimeframe} spread={spread} op={op} ownClock={ownClock} onChange={setDraft}/>
        : <span className="ip-none">先在上面选一个</span>}
      {problem && current ? <span className="ip-problem" role="status">{problem}</span> : null}
      {onDone ? <span className="ip-actions">
        <button type="button" className="se-btn" onClick={onCancel}>取消</button>
        <button type="button" className="se-btn primary" disabled={Boolean(problem)} onClick={done}>好</button>
      </span> : null}
    </div>
  </section>;
}

const num = (raw: string): number | null => raw.trim() === "" || !Number.isFinite(Number(raw)) ? null : Number(raw);

function Num({ label, value, onChange, min, max, step = 1, placeholder, prefix, unit }: { label: string; value: number | null; onChange: (value: number | null) => void;
  min?: number; max?: number; step?: number | "any"; placeholder?: string; prefix?: string; unit?: string }) {
  return <span className="se-ctl ip-num">{prefix ? <span>{prefix}</span> : null}
    <input type="number" aria-label={label} value={value ?? ""} min={min} max={max} step={step} placeholder={placeholder} onChange={event => onChange(num(event.target.value))}/>
    {unit ? <span>{unit}</span> : null}</span>;
}

/** The chosen item's parameters (the picker's bottom row). */
function OperandParams({ value, item, side, planTimeframe, spread, op, ownClock, onChange }: { value: OperandForm; item: PickerItem; side: "left" | "right";
  planTimeframe: number; spread: boolean; op: Operator | ""; ownClock?: boolean; onChange: (value: OperandForm) => void }) {
  const indicator = value.indicator as IndicatorName;
  const patch = (change: Partial<OperandForm>) => onChange({ ...value, ...change });
  const range = periodRange(indicator);
  const words = periodLabel(indicator);
  const fixed = TIMEFRAMES.filter(entry => !planTimeframe || entry.value > planTimeframe);
  return <>
    <b className="ip-chosen">{item.label}</b>
    {item.lines.length > 1 ? <span className="se-seg" role="group" aria-label={`${item.label} · 哪一条`}>
      {item.lines.map(line => {
        const reason = indicatorProblem(line.indicator, side, { spread, op });
        return <button type="button" key={line.indicator} aria-pressed={indicator === line.indicator} disabled={Boolean(reason)} title={reason ?? undefined}
          onClick={() => {
            if (indicator === line.indicator) return;   // re-picking the line already on is a no-op, not a reset
            onChange({ ...operandFor(line.indicator, { timeframe: value.timeframe, transform: value.transform, transformN: value.transformN, barsAgo: value.barsAgo }),
              period: usesPeriod(line.indicator) && value.period != null ? value.period : operandFor(line.indicator).period,
              k: usesK(line.indicator) ? value.k : null,
              // switching within the family keeps the mid-line algorithm the user chose (a rebuild would silently go back to EMA)
              basis: usesBasis(line.indicator) ? value.basis : null }) }}>{line.label}</button>;
      })}</span> : null}
    {indicator === "constant" ? <Num label={`${side === "left" ? "左边" : "右边"} · 数值`} value={value.value} step="any" onChange={number => patch({ value: number })}/> : null}
    {usesPeriod(indicator) ? <Num label={`${side === "left" ? "左边" : "右边"} · N`} value={value.period} min={range.min} max={range.max} prefix={words.prefix} unit={words.unit}
      onChange={period => patch({ period })}/> : null}
    {usesK(indicator) ? <Num label="倍数" value={value.k} step={0.1} min={0.1} max={10} prefix="倍数" placeholder={String(defaultK(indicator))} onChange={k => patch({ k })}/> : null}
    {usesBasis(indicator) ? <span className="se-seg" role="group" aria-label="中轨">
      <button type="button" aria-pressed={value.basis !== "ma"} onClick={() => patch({ basis: null })}>EMA 中轨</button>
      <button type="button" aria-pressed={value.basis === "ma"} onClick={() => patch({ basis: "ma" })}>MA 中轨</button></span> : null}
    {indicator === "pivot" ? <select className="se-select" aria-label="枢轴档位" value={value.value ?? 0} onChange={event => patch({ value: Number(event.target.value) })}>
      {PIVOT_LEVELS.map(level => <option key={level.value} value={level.value}>{level.label}</option>)}</select> : null}
    {indicator !== "constant" ? <>
      <span className="se-ctl ip-num"><span>取</span>
        <select aria-label="取值方式" value={value.transform} onChange={event => {
          const transform = event.target.value as Transform;
          patch({ transform, transformN: transform === "none" ? null : value.transformN ?? 5 });
        }}><option value="none">这根的值</option><option value="average">N 根均线</option><option value="highest">N 根最高</option><option value="lowest">N 根最低</option></select>
        {value.transform !== "none" ? <><input type="number" aria-label="取几根" min={2} max={200} value={value.transformN ?? ""} onChange={event => patch({ transformN: num(event.target.value) })}/><span>根</span></> : null}
      </span>
      <Num label="取几根前" value={value.barsAgo || null} min={0} max={MAX_BARS_AGO} placeholder="0" unit="根前" onChange={barsAgo => patch({ barsAgo: barsAgo ?? 0 })}/>
      {side === "left" && !ownClock ? <span className="se-ctl ip-num"><span>周期</span>
        <select aria-label="按哪个周期算" value={value.timeframe ?? 0} onChange={event => patch({ timeframe: Number(event.target.value) ? Number(event.target.value) as OperandForm["timeframe"] : null })}>
          <option value={0}>跟随策略{planTimeframe ? `（${timeframeLabel(planTimeframe)}）` : ""}</option>
          {fixed.map(entry => <option key={entry.value} value={entry.value}>固定 {entry.label}</option>)}
          {value.timeframe && !fixed.some(entry => entry.value === value.timeframe) ? <option value={value.timeframe}>固定 {timeframeLabel(value.timeframe)}（比策略周期快，不能用）</option> : null}
        </select></span> : <span className="ip-none">{side === "left" ? "用策略自己的周期" : "周期同左边"}</span>}
    </> : null}
  </>;
}

/** 上升 / 下降's right side: 和几根前比 (1–20). */
export function TrendRight({ label, value, onChange }: { label: string; value: number | null; onChange: (value: number | null) => void }) {
  return <Num label={label} value={value} min={1} max={20} prefix="较" unit="根前" onChange={onChange}/>;
}
/** 持仓四象限's right side: which of the four. */
export function QuadrantRight({ label, value, onChange }: { label: string; value: number | null; onChange: (value: number) => void }) {
  return <select className="se-select" aria-label={label} value={value ?? 1} onChange={event => onChange(Number(event.target.value))}>
    {OI_QUADRANTS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select>;
}

/**
 * A 修饰's number: typed freely and written only once it is a whole number in range, so the "1" of 12 does not
 * turn the 修饰 off; leaving the box shows the number in force again.
 */
function ModifierBox({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (value: number) => void }) {
  const [text, setText] = useState<string | null>(null);
  return <input type="number" aria-label={label} min={min} max={max} value={text ?? value} onBlur={() => setText(null)}
    onChange={event => {
      setText(event.target.value);
      const typed = num(event.target.value);
      if (typed != null && Number.isInteger(typed) && typed >= min && typed <= max) onChange(typed);
    }}/>;
}

/**
 * A condition's 修饰 (picker.html's chip row): N 根内发生 (withinBars), 连续 N 根 (consecutive), N 根内至少 M 根
 * (minCount) — the chosen ones as chips with their numbers and ✕, the rest behind ＋修饰; 交叉 cannot take 连续 N 根.
 */
export function ModifierChips({ row, name, onChange, disabled }: { row: RowForm; name: string; onChange: (row: RowForm) => void;
  /** Why no 修饰 may be added here (方向确认); chosen ones still show so they can be removed. */
  disabled?: string }) {
  const [adding, setAdding] = useState(false);
  const within = row.within > 1 && row.minCount <= 1;
  const atLeast = row.minCount > 1;
  const streak = row.consecutive > 1;
  const crossing = isCross(row.op);
  const box = (label: string, value: number, change: (value: number) => void, min = 1, max = MODIFIER_MAX) =>
    <ModifierBox label={`${name} · ${label}`} value={value} min={min} max={max} onChange={change}/>;
  if (disabled && !within && !atLeast && !streak) return null;
  return <div className="se-mods" role="group" aria-label={`${name} · 修饰`}>
    {within ? <span className="se-mod" data-on="">{box("N 根内", row.within, value => onChange({ ...row, within: value }), 2)}根内发生
      <button type="button" className="se-x" aria-label={`去掉${name}的 N 根内发生`} onClick={() => onChange({ ...row, within: 1 })}><X size={11}/></button></span> : null}
    {atLeast ? <span className="se-mod" data-on="">{box("N 根内", row.within, value => onChange({ ...row, within: value, minCount: Math.min(row.minCount, value) }), 2)}根内至少
      {box("至少 M 根", row.minCount, value => onChange({ ...row, minCount: Math.min(value, row.within) }), 2, row.within)}根
      <button type="button" className="se-x" aria-label={`去掉${name}的至少 M 根`} onClick={() => onChange({ ...row, within: 1, minCount: 1 })}><X size={11}/></button></span> : null}
    {streak ? <span className="se-mod" data-on="">连续{box("连续 N 根", row.consecutive, value => onChange({ ...row, consecutive: value }), 2)}根
      <button type="button" className="se-x" aria-label={`去掉${name}的连续 N 根`} onClick={() => onChange({ ...row, consecutive: 1 })}><X size={11}/></button></span> : null}
    {adding ? <>
      {!within && !atLeast ? <button type="button" className="se-mod" onClick={() => { onChange({ ...row, within: 3, minCount: 1 }); setAdding(false); }}>N 根内发生</button> : null}
      {!streak ? <button type="button" className="se-mod" disabled={crossing} title={crossing ? "交叉是一次性事件，不能要求连续 N 根" : undefined}
        onClick={() => { onChange({ ...row, consecutive: 2 }); setAdding(false); }}>连续 N 根</button> : null}
      {!atLeast ? <button type="button" className="se-mod" onClick={() => { onChange({ ...row, within: Math.max(row.within, 5), minCount: 3 }); setAdding(false); }}>N 根内至少 M 根</button> : null}
      <button type="button" className="se-link" onClick={() => setAdding(false)}>收起</button>
    </> : !disabled && (!(within || atLeast) || !streak) ? <button type="button" className="se-link se-mod-add" aria-label={`给${name}加修饰`} onClick={() => setAdding(true)}>＋修饰</button> : null}
  </div>;
}

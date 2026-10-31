import { createContext, useContext, useEffect, useRef, useState, type Dispatch, type ReactNode, type RefObject, type SetStateAction } from "react";
import { ArrowLeft, Plus, Sparkles, X } from "lucide-react";
import { conditionParts } from "../strategyApi";
import { DIRECTIONS, DEFAULTS, LIST_WORDS, OPERATORS, normalizeTime, QUICK_ADDS, REPEAT_CHOICES, SIDE_WORDS, SIZING_MODES, TIMEFRAMES, UNTIL_CHOICES, advancedRules, withSizingMode, emptyRow, listKey, maxPositionOf, missingParts, operandText, rowContext,
  planFromForm, quickAddLabel, quickRows, rowParts, timeframeLabel, usedEntryGroups, withLeft, withOp, LOTS_RULE, MAX_ORDERS_RULE, TAKE_TICKS_RULE, stopRule, type EditorForm, type FieldName, type GroupForm, type GroupKey,
  type OperandForm, type PyramidForm, type RowContext, type RowForm, type RowList, type ScaleTier } from "../strategyEditor";
import { isTrend, operatorProblem, type Operator } from "../strategyIndicators";
import { focusPlace, type FlowTarget, type FocusTarget } from "../strategyFlow";
import { backtestProgress, chooseEditor, editFields, editorFlow, focusEditor, groupHits, setEditorAnswer, regenerate, PREVIEW_TRADING_DAYS, type EditorStart, type EditorState } from "../strategyEditorStore";
import { AUTO_BLOCKED, SIGNAL_ACTIONS } from "./StrategySignalAction";
import { backtestMarksOf, currentBacktestMarks, setBacktestMarks } from "../aiAnnotations";
import { useTemplateCatalog } from "../strategyTemplates";
import { useDisclosureSurface } from "../uiSurfaces";
import { HelpTip, SectionCard, StatTiles, RuleRow } from "./Blocks";
import { StrategyQuestions } from "./StrategyQuestions";
import { ScaleOutFields } from "./ScaleOutFields";
import { ModifierChips, PickerPanel, QuadrantRight, TrendRight } from "./IndicatorPicker";
import { StrategyFlow } from "./StrategyFlow";
import { contractLabel, formatNumber, formatSigned, moneyUnit, signClass } from "./futures/support";
import { EquityCurve, FundsLines, RISK_HELP } from "./StrategyRiskBlocks";
import type { InstrumentCatalog } from "../api";
import { numberProblem, type NumberRule } from "../numberField";
import type { StrategyPlan } from "../strategyApi";
import "./StrategyEditorCard.css";

/**
 * 策略编辑卡 (2026-10-08; 第二版 the same day, the owner approved wide.html and picker.html): new and edited strategies
 * use this one card in the strategy panel, which then takes about three quarters of the window. The head has 返回、名称、
 * 补一句 and 放弃; the left column the form — 起点 with the AI's prefill, ① 品种周期方向, ② 开仓 (conditions picked from
 * every indicator of the engine, with their 修饰), ③ 平仓 and ④ 仓位与时段 side by side; the right column (about 460px) the
 * 策略流程图 and the preview backtest (four figures, the equity curve, 回测明细 / 存为草稿 / 上线运行…). A narrow panel puts
 * the two blocks under the form. Every explanation sits behind a "?". The state and every request live in
 * strategyEditorStore; this only draws and calls back.
 */
type Props = {
  state: EditorState;
  chartTimeframe: number;
  instruments?: InstrumentCatalog;
  /** 设置 → 交易 → AI 自主执行 (false = off: 自动下单 cannot be the default). */
  autonomy?: boolean;
  onBack: () => void;
  onDiscard: () => void;
  onStart: (start: EditorStart) => void;
  onAsk: (text: string) => void;
  onSave: () => void;
  onLaunch: () => void;
};

const CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫";
const num = (raw: string): number | null => raw.trim() === "" || !Number.isFinite(Number(raw)) ? null : Number(raw);

/** The editor's "?" texts (说明收进问号, 2026-10-08): what a block or field means; the screen keeps only what is needed while working. */
export const EDITOR_HELP = {
  basics: "在哪个合约、看几分钟的 K 线、做哪个方向。合约默认当前图表合约；方向留空时按开仓条件推断（只填了做多条件就只做多）。",
  entry: "每行一条条件，K 线收盘时判断；组内选全部满足或任一满足。行右边是近 60 个交易日这一条单独满足的次数，组头是同时满足的次数，条件太严一眼看得出。点指标打开选择器（全部指标、可搜索）；“＋修饰”可要求 N 根内发生、连续 N 根或 N 根内至少 M 根。",
  exit: "止损、止盈、追踪、保本……哪个先到按哪个平；都没到时按条件平仓或收盘前平。",
  sizing: "一次开几手、一天最多开几次、在哪些时段开新仓，以及这条策略自己的回撤停机。",
  trailing: "浮盈从最高（空单最低）回撤这么多就平，只收紧不放松；可以设浮盈达到多少才开始追踪。",
  closeBeforeEnd: "收盘前 N 分钟把持仓平掉。选“每个时段”时日盘、夜盘每段收盘前都平，否则只在当天最后一段。",
  breakeven: "浮盈达到这么多后，把止损移到开仓价（或开仓价外几跳），只收紧不放松。",
  scaleOut: "按持仓均价收盘判断，每档每笔持仓只平一次；各档由近到远、单位一致，剩下的照原止损止盈走。",
  timeTakeProfit: "持仓满 N 根起，收盘浮盈达到目标就平；每笔持仓只触发一次，各档的持仓根数不能重复。",
  exitRules: "满足这些条件就平掉对应方向的持仓（和开仓条件一样选）。",
  alignment: "先要这些条件全部成立（方向对了），才看上面的开仓条件；方向被打断才开始新的一段，每段最多开一次。用了方向确认，这一组的条件都只看当前状态（高于 / 低于 / 不低于 / 不高于）、全部满足、不加修饰，都用策略自己的周期。",
  rearm: "这个方向开过仓后，要等这些条件成立一次才允许再开（用策略自己的周期）。",
  perTradeRisk: "每笔最多亏这么多钱：按开仓价到止损的距离限制手数，不足一手时这次不开。用“前低”止损时必须填，在止损那一行。",
  trailingAtr: "按 ATR 倍数追踪时用的 ATR 周期。",
  night: "关掉后夜盘不开新仓，已有持仓的止损、止盈和平仓照常。",
  pyramiding: "只在赚钱时同向加仓，不摊低成本；和保本、追踪止损、按时间止盈不能同时用。最多持有 = 每次手数 + 每次加仓手数 × 次数。",
  sizingMode: "固定手数：每次开同样的手数（原来的默认）。按资金比例：按账户权益的一个比例给本策略划一块保证金额度，每次开仓前算还能开几手。"
    + "按波动：手数 = 权益 × 风险% ÷（合约乘数 × ATR 倍数 × ATR），不超过“每次最多”。",
  equityPercent: "本策略持仓和挂单的保证金合计，最多占账户当前权益的这个比例。每次开仓前算还能开几手，向下取整；不足 1 手这次不开，可用资金不够也不开。"
    + "这里的额度是按当前账户最近一次的估算，真正开仓时按那一刻的行情和账户重新算。"
    + "权益变化只影响之后的新开仓；额度变小不会主动平掉已有持仓。",
  maxLots: "可选。不填就只按比例算，不额外限制；填了取两者中较小的（本策略已有持仓和挂单也算在内）。",
  atrCap: "原有必填上限：按波动算出的手数不超过这里，算出不足 1 手这次不开。",
  drawdown: RISK_HELP.drawdown,
  cooldown: "平仓后等几根 K 线才允许再开仓；0 表示下一根就可以。",
  maxLoss: "当天净亏损（含持仓浮亏和手续费）达到这个数后不再开新仓，平仓照常。不填就不设。"
    + "和回撤停机不同：这里按当天亏损只停新开仓，不平仓、第二天照常；回撤停机按从最高点的回落停止并平仓，要手动恢复。",
  advanced: "这些规则卡片里改不了，保存时原样带上；要改，用上面的“补一句”说给 AI。",
  flow: "按表单实时画出策略怎么跑：每根 K 线收盘先过时段和方向，再看开仓条件；开仓后哪种平仓先到按哪种走。方块里的次数来自下面的预览回测，没有就不写；点方块，表单跳到对应的地方。",
  reminder: "只在这些时段里判断条件（按 K 线收盘时刻）；不填就整个交易时段都提醒。条件满足时发一条提醒（对话、桌面通知和锁屏推送，按通用提醒的开关），不出卡、不下单。",
  repeat: "只响一次：响过这条提醒就自动停。条件再满足再响：同一个信号只响一次，条件不再满足、之后又满足才再响。每根 K 线最多一次：条件一直满足就每根 K 线收盘响一次。"
    + "到期：今天收盘＝开始提醒的那个交易日收盘后停（夜盘开始算下一个交易日），本周＝这周最后一个交易日收盘后停；到期自动停并在对话里说一声，要再提醒就重新开始。",
  scope: "整张自选表：开始提醒时按自选表里每个品种的主力合约各开一份，每个品种分别判断、分别提醒；开始之后自选表再增减不会跟着变，要跟上就重新开始。近 60 个交易日的次数按上面这个合约数。",
  signalAction: "这里选的是“上线运行”对话框默认选中的执行方式，上线仍要你在对话框里点“验证并上线”，那里还能改。只提醒：信号来了提醒一句，不出卡、不下单；出卡确认：出一张确认卡，你确认才下单；自动下单：信号通过检查后直接下模拟单，要先在 设置 → 交易 打开 AI 自主执行。",
  backtest: "只用已收盘的 K 线，按收盘确认、下一根开盘成交，扣手续费；改动后约 1.5 秒自动重算，和上次改动比。图上标出了每笔开平（图表是同一合约和周期时）。",
} as const;
const Help = ({ id, label }: { id: keyof typeof EDITOR_HELP; label: string }) => <HelpTip helpKey={`strategy-editor:${id}`} label={label}>{EDITOR_HELP[id]}</HelpTip>;

/** Segmented choice (起点 / 周期 / 方向 / 止损方式 …): one pressed button. */
function Seg<T extends string | number>({ label, options, value, onChange, disabled }: {
  label: string; options: { value: T; label: string; reason?: string | null }[]; value: T | null; onChange: (value: T) => void; disabled?: boolean }) {
  return <span className="se-seg" role="group" aria-label={label}>
    {options.map(option => <button type="button" key={String(option.value)} aria-pressed={value === option.value} disabled={disabled || Boolean(option.reason)}
      title={option.reason ?? undefined} onClick={() => onChange(option.value)}>{option.label}</button>)}
  </span>;
}

function Switch({ label, checked, onChange, disabled }: { label: string; checked: boolean; onChange: (on: boolean) => void; disabled?: boolean }) {
  return <button type="button" role="switch" className="se-switch" aria-checked={checked} aria-label={label} disabled={disabled} onClick={() => onChange(!checked)}/>;
}

function NumberBox({ label, value, onChange, unit, prefix, placeholder = "—", min = 0, max, step = 1, disabled, ai, changed, rule }: {
  label: string; value: number | null; onChange: (value: number | null) => void; unit?: string; prefix?: string; placeholder?: string;
  min?: number; max?: number; step?: number; disabled?: boolean; ai?: boolean; changed?: boolean;
  /** The count's rule (numberField.ts, shared with the order ticket): a value outside it is marked here and listed in 还差. */
  rule?: NumberRule }) {
  const problem = rule ? numberProblem(value, rule) : null;
  return <span className="se-ctl" data-ai={ai || undefined} data-changed={changed || undefined} data-empty={value == null || undefined}
    data-invalid={problem ? true : undefined} title={problem ?? undefined}>
    {prefix ? <span>{prefix}</span> : null}
    <input type="number" aria-label={label} value={value ?? ""} placeholder={placeholder} min={min} max={max} step={step} disabled={disabled}
      aria-invalid={problem ? true : undefined} onChange={event => onChange(num(event.target.value))}/>
    {unit ? <span>{unit}</span> : null}
    {ai ? <AiTag/> : null}
  </span>;
}

export const AiTag = () => <span className="se-aitag" title="AI 按你的习惯预填">AI</span>;

/**
 * A 24-hour HH:MM box (开仓时段): a plain text field, so no browser language turns it into 09:00 AM. Digits are read
 * on leaving it (930 → 09:30, 21 → 21:00); what still is not a time stays as typed and the card says 还差.
 */
function TimeBox({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return <input type="text" inputMode="numeric" className="se-time" aria-label={label} value={value} maxLength={5} placeholder="09:00" spellCheck={false}
    onChange={event => onChange(event.target.value.replace(/[^\d:：]/g, "").replace("：", ":"))} onBlur={event => onChange(normalizeTime(event.target.value))}/>;
}

// -- conditions --------------------------------------------------------------------------------------

/** The desktop card's one open picker (`disclosure: strategy-editor-picker`, id 组:第几条:左/右) and what it needs to know. */
type PickerState = { open: string | null; set: (key: string | null) => void; spread: boolean };
const PickerContext = createContext<PickerState | null>(null);

/** A row side's picker as a commandable disclosure; renders nothing. */
function PickerSurface({ id }: { id: string }) {
  const picker = useContext(PickerContext);
  useDisclosureSurface("strategy-editor-picker", { id, available: Boolean(picker), state: picker?.open === id,
    apply: open => { if (open) picker?.set(id); else if (picker?.open === id) picker.set(null); } });
  return null;
}

function OperandButton({ label, value, placeholder, open, onOpen, timeframe }: { label: string; value: OperandForm; placeholder: string; open: boolean; onOpen: () => void; timeframe: number }) {
  const text = operandText(value, timeframe);
  return <button type="button" className="se-ctl se-operand" data-empty={!value.indicator || undefined} aria-label={label} aria-expanded={open} aria-haspopup="true"
    onClick={onOpen}><span className="se-operand-text">{text || placeholder}</span><span aria-hidden="true">▾</span></button>;
}

function ConditionRow({ row, index, gkey, list, context, hits, timeframe, onChange, onDelete }: { row: RowForm; index: number; gkey: GroupKey; list: RowList;
  context: RowContext; hits?: number | null; timeframe: number; onChange: (row: RowForm) => void; onDelete: () => void }) {
  const picker = useContext(PickerContext);
  const side = SIDE_WORDS[gkey];
  const name = `${side}${LIST_WORDS[list]}第 ${index + 1} 条`;
  const mark = CIRCLED[index] ?? `${index + 1}.`;
  const key = `${listKey(gkey, list)}:${index + 1}`;
  const rightPicker = !isTrend(row.op) && row.op !== "eq";
  const openSide = picker?.open === `${key}:left` ? "left" : picker?.open === `${key}:right` ? "right" : null;
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (openSide) ref.current?.scrollIntoView?.({ block: "nearest" }); }, [openSide]);
  if (row.locked && row.source) {
    const parts = conditionParts(row.source);
    return <div className="se-cond" data-locked="">
      <span className="se-i">{mark}</span>
      <div className="se-cond-locked"><RuleRow period="只读" left={parts.left} op={parts.op} right={parts.right} note={parts.note} title="卡片里改不了这条（系数偏移），原样保留；可以删"/></div>
      <span className="se-tail"><span className="se-hits">{hits == null ? "" : `${formatNumber(hits, 0)} 次`}</span>
        <button type="button" className="se-x" aria-label={`删除${name}`} onClick={onDelete}><X size={12}/></button></span>
    </div>;
  }
  const toggle = (which: "left" | "right") => picker?.set(openSide === which ? null : `${key}:${which}`);
  return <div className="se-cond" ref={ref}>
    <PickerSurface id={`${key}:left`}/>{rightPicker ? <PickerSurface id={`${key}:right`}/> : null}
    <span className="se-i">{mark}</span>
    <OperandButton label={`${name} · 指标`} value={row.left} placeholder="选指标" open={openSide === "left"} timeframe={timeframe} onOpen={() => toggle("left")}/>
    <select className="se-op" aria-label={`${name} · 比较`} value={row.op} onChange={event => onChange(withOp(row, event.target.value as Operator))}>
      <option value="" disabled>比较</option>
      {OPERATORS.map(item => {
        const reason = row.left.indicator || context.stateOnly ? operatorProblem(item.value, row.left.indicator, row.consecutive, context.stateOnly) : null;
        return <option key={item.value} value={item.value} disabled={Boolean(reason)} title={reason ?? undefined}>{item.label}</option>;
      })}
    </select>
    {isTrend(row.op) ? <TrendRight label={`${name} · 和几根前比`} value={row.right.value} onChange={value => onChange({ ...row, right: { ...row.right, indicator: "constant", value } })}/>
      : row.op === "eq" ? <QuadrantRight label={`${name} · 哪一种`} value={row.right.value} onChange={value => onChange({ ...row, right: { ...row.right, indicator: "constant", value } })}/>
        : <OperandButton label={`${name} · 对象`} value={row.right} placeholder="数值或指标" open={openSide === "right"} timeframe={timeframe} onOpen={() => toggle("right")}/>}
    <span className="se-tail"><span className="se-hits">{hits == null ? "" : `${formatNumber(hits, 0)} 次`}</span>
      <button type="button" className="se-x" aria-label={`删除${name}`} onClick={onDelete}><X size={12}/></button></span>
    <ModifierChips row={row} name={name} onChange={onChange} disabled={context.stateOnly ? "有方向确认时条件只看当前状态，不加修饰" : undefined}/>
    {openSide && picker ? <div className="se-picker"><PickerPanel key={`${key}:${openSide}`} side={openSide} layout="desktop" title={`改${name} · ${openSide === "left" ? "左边" : "右边"}`}
      value={row[openSide]} op={row.op} planTimeframe={timeframe} spread={picker.spread} ownClock={context.ownClock}
      onCancel={() => picker.set(null)} onDone={value => { onChange(openSide === "left" ? withLeft(row, value) : { ...row, right: value }); picker.set(null); }}/></div> : null}
  </div>;
}

/** 手机编辑卡: a condition as one tappable line (the sheet edits it); hits and ✕ as on the desktop row. */
function TapRow({ row, index, side, hits, onOpen, onDelete }: { row: RowForm; index: number; side: string; hits?: number | null;
  onOpen: () => void; onDelete: () => void }) {
  // `side` already names the list (做多 / 做多方向确认 / 做多复位).
  const name = `${side}第 ${index + 1} 条`;
  const mark = CIRCLED[index] ?? `${index + 1}.`;
  const parts = rowParts(row);
  const text = parts.left || parts.op || parts.right ? `${parts.left || "选左边"} ${parts.op || "比较"} ${parts.right}`.trim() : "没填，点这里选";
  return <div className="se-cond se-tap" data-empty={!parts.left || !parts.op || (!parts.right && !isTrend(row.op)) || undefined}>
    <button type="button" className="se-tap-main" aria-label={`改${name}：${text}`} onClick={onOpen}>
      <span className="se-i">{mark}</span>
      <span className="se-tap-text">{parts.left || parts.op || parts.right ? <>{parts.left || "选左边"} <b>{parts.op || "比较"}</b> {parts.right || (isTrend(row.op) ? "" : "选右边")}</> : "没填，点这里选"}</span>
    </button>
    <span className="se-tail"><span className="se-hits">{hits == null ? "" : `近 60 天 ${formatNumber(hits, 0)} 次`}</span>
      <button type="button" className="se-x" aria-label={`删除${name}`} onClick={onDelete}><X size={14}/></button></span>
  </div>;
}

/** One condition group (做多 / 做空 / 平多 / 平空). `onOpenRow` (the phone) shows each row as one tappable line
 *  whose sheet edits it, and 加一条 opens the sheet on the new row; without it the row is edited in place. */
/** The phone's condition sheet for a row; `added` is the group before 加一条 made the row, so 取消 can put it back. */
type OpenRow = (key: GroupKey, rowId: string, added?: GroupForm) => void;
function GroupEditor({ state, gkey, entry, onOpenRow }: { state: EditorState; gkey: GroupKey; entry?: boolean; onOpenRow?: OpenRow }) {
  const group: GroupForm = state.form[gkey];
  const side = SIDE_WORDS[gkey];
  const { hits, together } = entry ? groupHits(state, gkey as "entryLong" | "entryShort") : { hits: [], together: null };
  const edit = (change: (group: GroupForm) => GroupForm) => editFields([gkey], form => ({ ...form, [gkey]: change(form[gkey]) }));
  const changed = state.changed.includes(gkey);
  const long = gkey === "entryLong" || gkey === "exitLong";
  const aligned = entry && group.alignment.length > 0;
  // 提醒 (改版第一步 2026-10-31): one group, no 做多 / 做空 words; 常用 lists both the up and the down form.
  const single = state.reminder && entry && usedEntryGroups(state.form).length === 1;
  const quick = state.reminder ? [true, false].flatMap(up => QUICK_ADDS.map(item => ({ item, up }))) : QUICK_ADDS.map(item => ({ item, up: long }));
  const spread = Boolean(state.base?.spread);
  const rowsOf = (list: RowList, hitsOf: (number | null)[] = []) => group[list].map((row, index) => {
    const remove = () => edit(current => ({ ...current, [list]: current[list].filter(item => item.id !== row.id) }));
    return onOpenRow && !row.locked ? <TapRow key={row.id} row={row} index={index} side={`${side}${LIST_WORDS[list]}`} hits={hitsOf[index]} onOpen={() => onOpenRow(gkey, row.id)} onDelete={remove}/>
      : <ConditionRow key={row.id} row={row} index={index} gkey={gkey} list={list} context={rowContext(state.form, gkey, list, spread)} hits={hitsOf[index]}
        timeframe={state.form.timeframe} onChange={next => edit(current => ({ ...current, [list]: current[list].map(item => item.id === row.id ? next : item) }))} onDelete={remove}/>;
  });
  const add = (list: RowList) => {
    const row = emptyRow();
    // 方向确认 needs the group joined by 全部满足.
    edit(current => ({ ...current, [list]: [...current[list], row], join: list === "alignment" ? "all" : current.join }));
    onOpenRow?.(gkey, row.id, group);
  };
  return <div className="se-group" data-changed={changed || undefined} aria-label={`${side}条件`} role="group" data-focus={gkey}>
    <div className="se-group-h">
      {single ? null : <span className="se-dir" data-side={long ? "long" : "short"}>{side}</span>}
      {group.locked ? <span className="se-hint">用了条件组合 / 先后顺序，卡片里改不了，原样保留</span>
        : <Seg label={`${side}条件怎么合并`} value={group.join} options={[{ value: "all", label: "全部满足" },
          { value: "any", label: "任一满足", reason: aligned ? "有方向确认时条件要全部满足" : null }]} onChange={join => edit(current => ({ ...current, join }))}/>}
      {entry && together != null ? <span className="se-hint se-together">{group.join === "any" ? "任一满足" : "同时满足"} {formatNumber(together, 0)} 次</span> : null}
    </div>
    {group.locked ? group.rows.map((row, index) => row.source ? <RuleRow key={row.id} period="只读" {...conditionParts(row.source)} title={`${side}第 ${index + 1} 条`}/> : null)
      : rowsOf("rows", hits)}
    {group.locked ? null : <div className="se-add">
      <button type="button" className="se-link" onClick={() => add("rows")}><Plus size={12}/>加一条</button>
      {entry ? <><span className="se-hint">常用：</span>{quick.map(({ item, up }) => <button type="button" key={`${item.id}-${up}`} className="se-link"
        aria-label={`加一条：${quickAddLabel(item.label, up)}`}
        onClick={() => edit(current => ({ ...current, rows: [...current.rows.filter(row => row.left.indicator || row.locked), ...quickRows(item.id, up)] }))}>
        {quickAddLabel(item.label, up)}</button>)}</> : null}
    </div>}
    {entry && !group.locked ? <div className="se-sublists">{(["alignment", "rearm"] as const).filter(list => !state.reminder || group[list].length).map(list => {
      const addButton = group[list].length < (list === "rearm" ? 4 : 12) ? <button type="button" className="se-link" aria-label={`加一条${side}${LIST_WORDS[list]}`}
        onClick={() => add(list)}><Plus size={12}/>{group[list].length ? `加一条${LIST_WORDS[list]}` : LIST_WORDS[list]}</button> : null;
      const help = <HelpTip helpKey={`strategy-editor:${list}`} label={LIST_WORDS[list]}>{EDITOR_HELP[list]}</HelpTip>;
      // Unused: one short “＋方向确认 ?” beside the other; in use: its own block with its rows.
      return group[list].length ? <div key={list} className="se-sublist" data-focus={`${gkey}-${list}`}>
        <span className="se-sub">{LIST_WORDS[list]}{help}</span>{rowsOf(list)}{addButton}
      </div> : <span key={list} className="se-inline" data-focus={`${gkey}-${list}`}>{addButton}{help}</span>;
    })}</div> : null}
  </div>;
}

/** 依据: the sentence beside an AI-prefilled field. */
const Basis = ({ state, field }: { state: EditorState; field: FieldName }) =>
  state.aiFields.includes(field) && state.basis[field] ? <span className="se-hint se-basis">{state.basis[field]}</span> : null;

function Row({ label, children, focus, help }: { label: string; children: ReactNode; focus?: FlowTarget; help?: ReactNode }) {
  return <div className="se-row" data-focus={focus}><span className="se-lbl">{label}{help}</span>{children}</div>;
}

function timeAgo(at: number | undefined, now: number) {
  if (!at) return "";
  const seconds = Math.max(0, Math.round((now - at) / 1000));
  return seconds < 60 ? `${seconds} 秒前` : `${Math.round(seconds / 60)} 分钟前`;
}

/** 上次 +3,120 ↑ under a tile. */
function versus(now: number | null | undefined, before: number | null | undefined, text: (value: number) => string) {
  if (now == null || before == null) return undefined;
  return `上次 ${text(before)} ${now > before ? "↑" : now < before ? "↓" : "="}`;
}

/** What every field section reads: the form, its AI marks and 补一句 highlights, and the edit that clears them. */
function fieldKit(state: EditorState) {
  const ai = (field: FieldName) => state.aiFields.includes(field);
  const changed = (field: FieldName) => state.changed.includes(field);
  const edit = <K extends keyof EditorForm>(field: K & FieldName, value: EditorForm[K]) => editFields([field], current => ({ ...current, [field]: value }));
  return { form: state.form, ai, changed, edit };
}
type Fold = [boolean, Dispatch<SetStateAction<boolean>>];

/** ① 品种与周期 (the desktop block's body and the phone's first section). */
export function BasicsFields({ state, chartTimeframe, instruments }: { state: EditorState; chartTimeframe: number; instruments?: InstrumentCatalog }) {
  const { form, ai, changed, edit } = fieldKit(state);
  const symbolText = state.symbolLabel && ai("symbol") ? state.symbolLabel : contractLabel(form.symbol, instruments);
  const timeframeNote = form.timeframe === 0 ? `现在跟随图表（${timeframeLabel(chartTimeframe)}）` : "";
  return <div className="se-basics">
    <Row label="合约">
      <span className="se-ctl se-symbol" data-ai={ai("symbol") || undefined} data-changed={changed("symbol") || undefined}>
        <input aria-label="合约" title={symbolText} value={form.symbol} spellCheck={false} onChange={event => edit("symbol", event.target.value.trim())}/>
        {ai("symbol") ? <AiTag/> : null}
      </span>
      {symbolText !== form.symbol ? <span className="se-hint se-symbol-name">{symbolText}</span> : null}<Basis state={state} field="symbol"/>
    </Row>
    {state.reminder ? <Row label="范围" help={<Help id="scope" label="整张自选表"/>}>
      <span data-changed={changed("reminderOptions") || undefined} className="se-mark">
        <Seg label="盯哪个品种" value={form.reminderOptions.watchlist ? "watchlist" : "contract"} onChange={value => chooseEditor({ watchlist: value === "watchlist" })}
          options={[{ value: "contract", label: "只盯这个合约" }, { value: "watchlist", label: "整张自选表（每个品种分别判断）" }]}/></span>
    </Row> : null}
    <Row label="周期">
      <span data-changed={changed("timeframe") || undefined} className="se-mark"><Seg label="周期" value={form.timeframe || null} options={TIMEFRAMES.map(item => ({ ...item, label: item.label.replace("1小时", "1时").replace("日线", "日") }))} onChange={value => edit("timeframe", value)}/></span>
      {timeframeNote ? <span className="se-hint">{timeframeNote}</span> : null}
    </Row>
    {state.reminder ? null : <Row label="方向">
      <span data-ai={ai("direction") || undefined} data-changed={changed("direction") || undefined} className="se-mark">
        <Seg label="方向" value={form.direction} options={DIRECTIONS} onChange={value => edit("direction", value)}/>{ai("direction") ? <AiTag/> : null}</span>
      <Basis state={state} field="direction"/>
    </Row>}
  </div>;
}

/** ② 开仓 (the entry groups the direction uses; the other side folded). `onOpenRow`: the phone's sheet. */
export function EntryFields({ state, onOpenRow }: { state: EditorState; onOpenRow?: OpenRow }) {
  const used = usedEntryGroups(state.form);
  return <>
    {used.includes("entryLong") ? <GroupEditor state={state} gkey="entryLong" entry onOpenRow={onOpenRow}/> : state.reminder ? null : <p className="se-folded">做多条件（当前只做空，已收起）</p>}
    {used.includes("entryShort") ? <GroupEditor state={state} gkey="entryShort" entry onOpenRow={onOpenRow}/> : state.reminder ? null : <p className="se-folded">做空条件（当前只做多，已收起）</p>}
  </>;
}

/** ③ 平仓, with its 更多 and 高级规则 folds (their state is the host's: each host registers the disclosures). */
export function ExitFields({ state, plan, advanced, more, advancedFold, onOpenRow }: { state: EditorState; plan: StrategyPlan; advanced: string[];
  more: Fold; advancedFold: Fold; onOpenRow?: OpenRow }) {
  const { form, ai, changed, edit } = fieldKit(state);
  const [moreOpen, setMoreOpen] = more;
  const [advancedOpen, setAdvancedOpen] = advancedFold;
  const stopUnit = form.stop.mode === "atr" ? "倍 ATR" : form.stop.mode === "lookback" ? "根低点 / 高点外一跳" : "跳";
  const trailingUnit = form.trailing.mode === "percent" ? "%" : form.trailing.mode === "ticks" ? "跳" : "倍 ATR";
  return <>
    <Row label="止损" focus="stop">
      {form.stop.mode === "locked" ? <span className="se-hint">止损由{plan.spread ? "价差止损点数" : "回调低点 / 高点"}决定（见高级规则），原样保留</span> : <>
        <span data-changed={changed("stop") || undefined} className="se-mark">
          <Seg label="止损方式" value={form.stop.mode} options={[{ value: "ticks", label: "固定跳数" }, { value: "atr", label: "ATR 倍数" }, { value: "lookback", label: "前低" }]}
            onChange={mode => edit("stop", { mode, value: mode === form.stop.mode ? form.stop.value : null, risk: form.stop.risk })}/></span>
        <NumberBox label="止损" value={form.stop.value} prefix={form.stop.mode === "lookback" ? "前" : undefined} unit={stopUnit}
          placeholder={form.stop.mode === "atr" ? "填倍数" : form.stop.mode === "lookback" ? "填根数" : "填跳数"}
          step={form.stop.mode === "atr" ? 0.5 : 1} ai={ai("stop")} changed={changed("stop")} rule={stopRule(form.stop.mode)} onChange={value => edit("stop", { ...form.stop, value })}/>
        {form.stop.mode === "lookback" ? <NumberBox label="单笔预算" value={form.stop.risk} prefix="单笔预算" unit={moneyUnit(form.symbol)} onChange={risk => edit("stop", { ...form.stop, risk })}/> : null}
        <Basis state={state} field="stop"/></>}
    </Row>
    <Row label="止盈" focus="take">
      <span data-changed={changed("take") || undefined} className="se-mark">
        <Seg label="止盈方式" value={form.take.mode} options={[{ value: "ticks", label: "固定跳数" }, { value: "none", label: "不设" }]}
          onChange={mode => edit("take", { mode, value: mode === "none" ? null : form.take.value })}/></span>
      {form.take.mode === "ticks" ? <NumberBox label="止盈" value={form.take.value} unit="跳" placeholder="填跳数" changed={changed("take")} rule={TAKE_TICKS_RULE}
        onChange={value => edit("take", { ...form.take, value })}/> : null}
    </Row>
    <Row label="追踪止损" focus="trailing" help={<Help id="trailing" label="追踪止损"/>}>
      <Switch label="追踪止损" checked={form.trailing.on} onChange={on => edit("trailing", { ...form.trailing, on })}/>
      {form.trailing.on ? <>
        <select aria-label="追踪止损 · 单位" className="se-select" value={form.trailing.mode} onChange={event => edit("trailing", { ...form.trailing, mode: event.target.value as EditorForm["trailing"]["mode"] })}>
          <option value="ticks">跳</option><option value="percent">%</option><option value="atr_chandelier">倍 ATR</option></select>
        <NumberBox label="追踪止损 · 回撤" value={form.trailing.value} prefix="回撤" changed={changed("trailing")} step={form.trailing.mode === "ticks" ? 1 : 0.1}
          onChange={value => edit("trailing", { ...form.trailing, value })}/>
        <NumberBox label="追踪止损 · 浮盈多少后开始" value={form.trailing.activateAfter} prefix="浮盈" unit={`${trailingUnit}后开始`} placeholder="0" step={form.trailing.mode === "ticks" ? 1 : 0.1}
          onChange={activateAfter => edit("trailing", { ...form.trailing, activateAfter })}/>
        {form.trailing.mode === "atr_chandelier" ? <NumberBox label="追踪止损 · ATR 周期" value={form.trailing.atrPeriod} prefix="ATR" unit="根" min={2} max={200} placeholder="14"
          onChange={atrPeriod => edit("trailing", { ...form.trailing, atrPeriod })}/> : null}</> : null}
    </Row>
    <Row label="收盘前平仓" focus="closeBeforeEnd" help={<Help id="closeBeforeEnd" label="收盘前平仓"/>}>
      <Switch label="收盘前平仓" checked={form.closeBeforeEnd.on} onChange={on => edit("closeBeforeEnd", { ...form.closeBeforeEnd, on, minutes: on ? form.closeBeforeEnd.minutes ?? DEFAULTS.closeBeforeEndMinutes : null })}/>
      {form.closeBeforeEnd.on ? <>
        <NumberBox label="收盘前平仓 · 提前分钟" value={form.closeBeforeEnd.minutes} prefix="提前" unit="分钟" ai={ai("closeBeforeEnd")} changed={changed("closeBeforeEnd")} min={1} max={120}
          placeholder={String(DEFAULTS.closeBeforeEndMinutes)} onChange={minutes => edit("closeBeforeEnd", { ...form.closeBeforeEnd, minutes })}/>
        <Seg label="收盘前平仓 · 哪些收盘" value={form.closeBeforeEnd.everySession ? "session" : "day"} options={[{ value: "day", label: "当天最后" }, { value: "session", label: "每个时段" }]}
          onChange={value => edit("closeBeforeEnd", { ...form.closeBeforeEnd, everySession: value === "session" })}/></> : null}
      <Basis state={state} field="closeBeforeEnd"/>
    </Row>
    <details className="se-more" open={moreOpen} onToggle={event => { const open = event.currentTarget.open; if (open !== moreOpen) setMoreOpen(open); }}>
      <summary>更多：保本、分批止盈、按时间止盈、最长持仓、按条件平仓</summary>
      {/* Rendered only while open: a closed <details> still lays its content out in Chrome, and its
          controls would count as on screen while the snapshot leaves them out. */}
      {moreOpen ? <div className="se-more-body">
        <Row label="保本" focus="breakeven" help={<Help id="breakeven" label="保本"/>}>
          <Switch label="保本" checked={form.breakeven.on} onChange={on => edit("breakeven", { ...form.breakeven, on })}/>
          {form.breakeven.on ? <>
            <select aria-label="保本 · 触发单位" className="se-select" value={form.breakeven.unit}
              onChange={event => edit("breakeven", { ...form.breakeven, unit: event.target.value as "ticks" | "atr", trigger: null })}>
              <option value="ticks">按跳数</option><option value="atr">按 ATR 倍数</option></select>
            <NumberBox label="保本 · 浮盈触发" prefix="浮盈" unit={form.breakeven.unit === "ticks" ? "跳后" : "倍 ATR 后"} value={form.breakeven.trigger} changed={changed("breakeven")}
              step={form.breakeven.unit === "ticks" ? 1 : 0.1} onChange={trigger => edit("breakeven", { ...form.breakeven, trigger })}/>
            <NumberBox label="保本 · 偏移跳数" prefix="止损移到开仓价外" unit="跳" placeholder="0" value={form.breakeven.offsetTicks}
              onChange={offsetTicks => edit("breakeven", { ...form.breakeven, offsetTicks })}/></> : null}
        </Row>
        <div data-changed={changed("scaleOut") || undefined} data-focus="scaleOut">
          <ScaleOutFields tiers={form.scaleOut} disabled={false} help={<Help id="scaleOut" label="分批止盈"/>} onChange={(tiers: ScaleTier[]) => edit("scaleOut", tiers)}/></div>
        <Row label="按时间止盈" focus="timeTakeProfit" help={<Help id="timeTakeProfit" label="按时间止盈"/>}>
          <span className="se-stack" data-changed={changed("timeTakeProfit") || undefined}>
            {form.timeTakeProfit.map((tier, index) => {
              const patch = (change: Partial<typeof tier>) => edit("timeTakeProfit", form.timeTakeProfit.map((item, at) => at === index ? { ...item, ...change } : item));
              return <span className="se-inline" key={index}>
                <NumberBox label={`按时间止盈第 ${index + 1} 档 · 持仓根数`} prefix="持仓满" unit="根起" value={tier.afterBars} onChange={afterBars => patch({ afterBars })}/>
                <select aria-label={`按时间止盈第 ${index + 1} 档 · 单位`} className="se-select" value={tier.unit}
                  onChange={event => patch({ unit: event.target.value as "ticks" | "atr", profit: null })}><option value="ticks">跳</option><option value="atr">倍 ATR</option></select>
                <NumberBox label={`按时间止盈第 ${index + 1} 档 · 浮盈目标`} prefix="浮盈 ≥" unit={tier.unit === "ticks" ? "跳就平" : "倍 ATR 就平"} value={tier.profit}
                  step={tier.unit === "ticks" ? 1 : 0.1} onChange={profit => patch({ profit })}/>
                <button type="button" className="se-x" aria-label={`删除按时间止盈第 ${index + 1} 档`}
                  onClick={() => edit("timeTakeProfit", form.timeTakeProfit.filter((_, at) => at !== index))}><X size={12}/></button>
              </span>;
            })}
            {form.timeTakeProfit.length < 6 ? <button type="button" className="se-link" onClick={() => edit("timeTakeProfit", [...form.timeTakeProfit, { afterBars: null, unit: "ticks", profit: null }])}>
              <Plus size={12}/>加一档</button> : null}
          </span>
        </Row>
        <Row label="最长持仓" focus="maxHold">
          <NumberBox label="最长持仓" value={form.maxHoldBars} unit="根K线就平" placeholder="不限" min={1} max={200} changed={changed("maxHoldBars")} onChange={value => edit("maxHoldBars", value)}/>
        </Row>
        <div className="se-exit-rules" data-focus="exitRules">
          <span className="se-sub">按条件平仓<Help id="exitRules" label="按条件平仓"/></span>
          {form.direction !== "short" ? <GroupEditor state={state} gkey="exitLong" onOpenRow={onOpenRow}/> : null}
          {form.direction !== "long" ? <GroupEditor state={state} gkey="exitShort" onOpenRow={onOpenRow}/> : null}
        </div>
      </div> : null}
    </details>
    {advanced.length ? <details className="se-more" open={advancedOpen} onToggle={event => { const open = event.currentTarget.open; if (open !== advancedOpen) setAdvancedOpen(open); }}>
      <summary>高级规则（卡片里不能改）· {advanced.length} 项</summary>
      {advancedOpen ? <><ul className="se-advanced">{advanced.map((line, index) => <li key={index}>{line}</li>)}</ul>
        <span className="se-sub">怎么改<Help id="advanced" label="高级规则"/></span></> : null}
    </details> : null}
  </>;
}

/**
 * ④ 仓位与时段 (2026-10-09, the approved risk-controls mockup): 开仓手数 is one of 固定手数 (the old default) / 按资金比例 /
 * 按波动, each with its own numbers under it; 按资金比例 shows the Runtime's two-line calculation for this form. 回撤停机
 * (off by default) sits after 开仓时段; its 更多 keeps 浮盈加仓、冷却、每个方向每天次数、单笔预算、当日亏损上限.
 */
/** 开仓时段 and 夜盘: the sizing block's rows, and the whole ④ of a 提醒 (its 提醒时段 / 夜盘也提醒). */
export function WindowFields({ state }: { state: EditorState }) {
  const { form, ai, changed, edit } = fieldKit(state);
  const words = state.reminder ? "提醒" : "开仓";
  const night = state.reminder ? "夜盘也提醒" : "夜盘开新仓";
  return <>
    <Row label={`${words}时段`}>
      <span className="se-stack" data-changed={changed("entryWindows") || undefined}>
        {form.entryWindows.map((window, index) => <span key={index} className="se-ctl se-window" data-ai={ai("entryWindows") || undefined}>
          <TimeBox label={`${words}时段 ${index + 1} · 开始`} value={window.start}
            onChange={start => edit("entryWindows", form.entryWindows.map((item, at) => at === index ? { ...item, start } : item))}/>–
          <TimeBox label={`${words}时段 ${index + 1} · 结束`} value={window.end}
            onChange={end => edit("entryWindows", form.entryWindows.map((item, at) => at === index ? { ...item, end } : item))}/>
          {ai("entryWindows") ? <AiTag/> : null}
          <button type="button" className="se-x" aria-label={`删除${words}时段 ${index + 1}`} onClick={() => edit("entryWindows", form.entryWindows.filter((_, at) => at !== index))}><X size={12}/></button>
        </span>)}
        <span className="se-inline">
          {!form.entryWindows.length ? <span className="se-hint">不限</span> : null}
          {form.entryWindows.length < 6 ? <button type="button" className="se-link" onClick={() => edit("entryWindows", [...form.entryWindows, { start: "09:00", end: "11:30" }])}><Plus size={12}/>加时段</button> : null}
          <Basis state={state} field="entryWindows"/>
        </span>
      </span>
    </Row>
    <Row label={night} help={state.reminder ? undefined : <Help id="night" label={night}/>}>
      <Switch label={night} checked={form.allowNightEntries} onChange={on => edit("allowNightEntries", on)}/>
    </Row>
  </>;
}

/** 响几次 and 到期 (提醒, 改版第一步 2026-10-31): the desktop's ④ and the phone's 提醒时段 section. */
export function RepeatFields({ state }: { state: EditorState }) {
  const { form, changed } = fieldKit(state);
  return <>
    <Row label="响几次">
      <span data-changed={changed("reminderOptions") || undefined} className="se-mark">
        <Seg label="响几次" value={form.reminderOptions.repeat} options={REPEAT_CHOICES} onChange={repeat => chooseEditor({ repeat })}/></span>
    </Row>
    <Row label="到期">
      <span data-changed={changed("reminderOptions") || undefined} className="se-mark">
        <Seg label="到期" value={form.reminderOptions.until} options={UNTIL_CHOICES} onChange={until => chooseEditor({ until })}/></span>
    </Row>
  </>;
}

/** 信号来了怎么办 (策略, 改版第一步): the 上线运行 dialog's preselected 执行方式; 自动下单 only while AI 自主执行 is on. */
function SignalActionFields({ state, autonomy }: { state: EditorState; autonomy?: boolean }) {
  const { form, changed } = fieldKit(state);
  const blocked = autonomy === false ? AUTO_BLOCKED : null;
  return <div className="se-inline">
    <span data-changed={changed("signalAction") || undefined} className="se-mark">
      <Seg label="信号来了怎么办" value={form.signalAction ?? "confirm"} onChange={signalAction => chooseEditor({ signalAction }, blocked)}
        options={SIGNAL_ACTIONS.map(option => ({ value: option.value, label: option.label, reason: option.value === "auto" ? blocked : null }))}/></span>
    {blocked ? <span className="se-hint">{blocked}</span> : null}
  </div>;
}

export function SizingFields({ state, more }: { state: EditorState; more?: Fold }) {
  const { form, ai, changed, edit } = fieldKit(state);
  const [ownMore, setOwnMore] = useState(false);
  const [moreOpen, setMoreOpen] = more ?? [ownMore, setOwnMore];
  const pyramid = form.pyramiding;
  const setPyramid = (change: Partial<PyramidForm>) => edit("pyramiding", { ...pyramid, ...change });
  const most = maxPositionOf(form);
  const sizing = form.sizing;
  const setSizing = (change: Partial<EditorForm["sizing"]>) => edit("sizing", { ...sizing, ...change });
  const drawdown = form.maxDrawdown;
  const lotsBox = (label: string, prefix: string, help?: ReactNode) => <span className="se-inline">
    <NumberBox label={label} value={form.lots} prefix={prefix} unit="手" min={1} max={20} placeholder={String(DEFAULTS.lots)} ai={ai("lots")} changed={changed("lots")} rule={LOTS_RULE}
      onChange={lots => edit("lots", lots)}/>{help}
    {most !== (form.lots ?? DEFAULTS.lots) ? <span className="se-hint">最多持有 {most} 手</span> : null}
    <Basis state={state} field="lots"/></span>;
  return <>
    <Row label="开仓手数" focus="action" help={<Help id="sizingMode" label="开仓手数"/>}>
      <span data-changed={changed("sizing") || undefined} className="se-mark">
        <Seg label="开仓手数" value={sizing.mode} options={SIZING_MODES} onChange={mode => edit("sizing", withSizingMode(sizing, mode))}/></span>
    </Row>
    <div className="se-pane" data-pane={sizing.mode}>
      {sizing.mode === "fixed" ? lotsBox("每次开仓手数", "每次") : null}
      {sizing.mode === "equity_percent" ? <>
        <span className="se-inline">
          <NumberBox label="资金占用上限" value={sizing.equityPercent} prefix="资金占用上限" unit="% 权益" step={1} min={0.1} max={100} placeholder="填 %"
            changed={changed("sizing")} onChange={equityPercent => setSizing({ equityPercent })}/>
          <Help id="equityPercent" label="资金占用上限"/></span>
        <span className="se-inline">
          <NumberBox label="按资金比例 · 最多手数" value={sizing.maxLots} prefix="最多" unit="手" min={1} placeholder="不限" onChange={maxLots => setSizing({ maxLots })}/>
          <Help id="maxLots" label="最多手数"/></span>
        <FundsLines funds={state.funds?.data} loading={state.funds?.status === "loading"} error={state.funds?.error} at={state.funds?.at} unit={moneyUnit(form.symbol)}/>
      </> : null}
      {sizing.mode === "atr_risk" ? <>
        <NumberBox label="单笔风险占权益" value={sizing.riskPercent} prefix="每笔冒" unit="% 权益" step={0.1} min={0.1} max={10} placeholder="填 %" changed={changed("sizing")}
          onChange={riskPercent => setSizing({ riskPercent })}/>
        <span className="se-inline">
          <NumberBox label="按波动 · ATR 倍数" value={sizing.atrMultiple} prefix="止损按" unit="倍 ATR" step={0.5} min={0.5} max={20} placeholder="2"
            onChange={atrMultiple => setSizing({ atrMultiple })}/>
          <NumberBox label="按波动 · ATR 周期" value={sizing.atrPeriod} prefix="ATR" unit="根" min={2} max={200} placeholder="14"
            onChange={atrPeriod => setSizing({ atrPeriod })}/></span>
        {lotsBox("按波动 · 每次最多手数", "每次最多", <Help id="atrCap" label="按波动 · 每次最多手数"/>)}
      </> : null}
    </div>
    <Row label="每天" focus="limits">
      <NumberBox label="每天最多开仓次数" value={form.maxOrdersPerDay} prefix="最多开" unit="次" min={1} max={50} placeholder={String(DEFAULTS.maxOrdersPerDay)} changed={changed("maxOrdersPerDay")} rule={MAX_ORDERS_RULE}
        onChange={value => edit("maxOrdersPerDay", value)}/>
    </Row>
    <WindowFields state={state}/>
    <Row label="回撤停机" help={<Help id="drawdown" label="回撤停机"/>}>
      <span data-changed={changed("maxDrawdown") || undefined} className="se-mark">
        <Switch label="回撤停机" checked={drawdown.on} onChange={on => edit("maxDrawdown", { ...drawdown, on })}/></span>
      {drawdown.on ? null : <span className="se-hint">不启用</span>}
    </Row>
    {drawdown.on ? <div className="se-pane">
      <span className="se-inline">
        <NumberBox label="回撤停机 · 回撤比例" value={drawdown.percent} prefix="从最高点回撤" unit="%" step={0.5} min={0.1} max={100} placeholder="填 %"
          changed={changed("maxDrawdown")} onChange={percent => edit("maxDrawdown", { ...drawdown, percent })}/>时，停止并平仓</span>
      <span className="se-acts" aria-label="触发后做什么"><span>撤销本策略挂单</span><span>平掉本策略持仓</span><span>保持停止，手动恢复</span></span>
    </div> : null}
    <details className="se-more" open={moreOpen} onToggle={event => { const open = event.currentTarget.open; if (open !== moreOpen) setMoreOpen(open); }}>
      <summary>更多：浮盈加仓、冷却、每个方向次数、亏损上限</summary>
      {moreOpen ? <div className="se-more-body">
        <Row label="浮盈加仓" help={<Help id="pyramiding" label="浮盈加仓"/>}>
          <Switch label="浮盈加仓" checked={pyramid.on} onChange={on => setPyramid(on ? { on, volume: pyramid.volume ?? form.lots ?? DEFAULTS.lots } : { on })}/>
          {pyramid.on ? <>
            <select aria-label="浮盈加仓 · 什么时候加" className="se-select" value={pyramid.trigger}
              onChange={event => { const trigger = event.target.value as PyramidForm["trigger"]; setPyramid({ trigger, step: trigger === "profit_ticks" ? 10 : trigger === "profit_atr" ? 0.5 : pyramid.step }); }}>
              <option value="profit_atr">每盈利 N 倍 ATR</option><option value="profit_ticks">每盈利 N 跳</option><option value="signal">同向信号再成立</option></select>
            {pyramid.trigger !== "signal" ? <NumberBox label="浮盈加仓 · 步长" value={pyramid.step} prefix="每盈利" unit={pyramid.trigger === "profit_ticks" ? "跳" : "倍 ATR"}
              step={pyramid.trigger === "profit_ticks" ? 1 : 0.1} onChange={step => setPyramid({ step })}/> : null}
            {pyramid.trigger === "profit_atr" ? <NumberBox label="浮盈加仓 · ATR 周期" value={pyramid.atrPeriod} prefix="ATR" unit="根" min={2} max={200} onChange={atrPeriod => setPyramid({ atrPeriod })}/> : null}
            <NumberBox label="浮盈加仓 · 每次加几手" value={pyramid.volume} prefix="每次加" unit="手" min={1} max={20} placeholder="跟首笔" onChange={volume => setPyramid({ volume })}/>
            <NumberBox label="浮盈加仓 · 最多加几次" value={pyramid.maxAdds} prefix="最多加" unit="次" min={1} max={5} onChange={maxAdds => setPyramid({ maxAdds })}/>
            <label className="se-check"><input type="checkbox" checked={pyramid.moveStop} onChange={event => setPyramid({ moveStop: event.target.checked })}/>加仓后按最新成交重算止损止盈</label>
          </> : null}
        </Row>
        <Row label="平仓后冷却" focus="limits" help={<Help id="cooldown" label="平仓后冷却"/>}>
          <NumberBox label="平仓后冷却根数" value={form.cooldownBars} prefix="等" unit="根再开" min={0} max={100} placeholder={String(DEFAULTS.cooldownBars)} changed={changed("cooldownBars")}
            onChange={value => edit("cooldownBars", value)}/>
        </Row>
        <Row label="每个方向">
          <NumberBox label="每个方向每天最多开仓次数" value={form.maxEntriesPerDirection} prefix="每天最多开" unit="次" min={1} max={50} placeholder="不限"
            changed={changed("maxEntriesPerDirection")} onChange={value => edit("maxEntriesPerDirection", value)}/>
        </Row>
        {form.stop.mode !== "lookback" ? <Row label="单笔预算" help={<Help id="perTradeRisk" label="单笔预算"/>}>
          <NumberBox label="单笔预算" value={form.stop.risk} prefix="每笔最多亏" unit={moneyUnit(form.symbol)} min={1} placeholder="不设" onChange={risk => edit("stop", { ...form.stop, risk })}/>
        </Row> : null}
        <Row label="当日亏损上限" help={<Help id="maxLoss" label="当日亏损上限"/>}>
          <NumberBox label="当日亏损上限" value={form.maxLoss} prefix="亏到" unit={`${moneyUnit(form.symbol)}停开新仓`} min={1} placeholder="不设" changed={changed("maxLoss")}
            onChange={value => edit("maxLoss", value)}/>
        </Row>
      </div> : null}
    </details>
  </>;
}

/**
 * 起点 (空白 / 从模板 ▾ / 按我的习惯), the desktop card's and the phone page's: the segment stays short ("模板 ▾" once one
 * is chosen) and the chosen template's full name is its own line under it, free to wrap.
 */
export function StartChoice({ state, busy, onStart }: { state: EditorState; busy: boolean; onStart: (start: EditorStart) => void }) {
  const catalog = useTemplateCatalog();
  const templates = (catalog?.items ?? []).filter(item => !(item.params ?? []).some(param => param.type === "contract"));
  const templateId = state.start.startsWith("template:") ? state.start.slice("template:".length) : null;
  const name = templateId ? catalog?.items.find(item => item.id === templateId)?.name ?? templateId : null;
  return <>
    <span className="se-seg" role="group" aria-label="起点">
      <button type="button" aria-pressed={state.start === "blank"} disabled={busy} onClick={() => onStart("blank")}>空白</button>
      {/* Always shows its own label: a chosen template's long name would be cut off inside the segment. */}
      <select aria-label="从模板" className={templateId ? "on" : ""} disabled={busy} value=""
        onChange={event => event.target.value && onStart(`template:${event.target.value}`)}>
        <option value="">{templateId ? "模板 ▾" : "从模板 ▾"}</option>
        {templates.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
      <button type="button" aria-pressed={state.start === "habits"} disabled={busy} onClick={() => onStart("habits")}>按我的习惯</button>
    </span>
    {name ? <span className="se-start-name">起点：{name}</span> : null}
  </>;
}

/** 回测明细 (the desktop card's block and the phone page's sheet): exit reasons and every trade of the last preview. */
export function backtestDetailHint(state: EditorState, instruments?: InstrumentCatalog) {
  const result = state.backtest.result;
  return result ? `${contractLabel(result.symbol, instruments)} · 近 ${result.coverage.tradingDays} 个交易日 · ${result.feeEstimate?.text ?? "已扣手续费"}` : undefined;
}
export function BacktestDetail({ state }: { state: EditorState }) {
  const result = state.backtest.result;
  if (!result) return <div className="section-card-body"><p className="se-hint">{missingParts(state.form).length ? "填好开仓条件和止损后自动回测。" : "回测出来后这里列出逐笔。"}</p></div>;
  return <div className="section-card-body">
    {result.exitReasons.length ? <StatTiles columns={3} label="出场原因" items={result.exitReasons.map(item => ({ label: item.label, value: `${item.count} 笔`, note: formatSigned(item.net, 0) }))}/> : null}
    {result.trades.length ? <table className="se-trades" aria-label="逐笔">
      <thead><tr><th scope="col">开仓</th><th scope="col">多空</th><th scope="col">开 / 平</th><th scope="col">出场</th><th scope="col">净盈亏</th></tr></thead>
      <tbody>{result.trades.slice(0, 200).map(trade => <tr key={trade.id}>
        <td>{new Date(trade.openTime / 1e6).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}</td>
        <td>{trade.direction === "long" ? "多" : "空"} {trade.volume}</td>
        <td>{formatNumber(trade.openPrice, 2)} / {formatNumber(trade.closePrice, 2)}</td><td>{trade.exitLabel}</td>
        <td className={signClass(trade.net)}>{formatSigned(trade.net, 0)}</td></tr>)}</tbody>
    </table> : <p className="se-hint">这段时间一笔也没开，看看每条条件的次数哪条太严。</p>}
  </div>;
}

/** 流程图 → 表单: the card follows the store's `focus` — opens the fold the field sits in, scrolls there and lights it. */
export function useFocusFollow(state: EditorState, root: RefObject<HTMLElement | null>, openFold: (target: FocusTarget) => void) {
  const seq = state.focus?.seq;
  // A focus from before this card mounted (the draft was hidden and opened again) is not replayed.
  const seen = useRef(seq);
  useEffect(() => {
    const target = state.focus?.target;
    if (!target || seq === seen.current) return;
    seen.current = seq;
    openFold(target);
    // After the fold the field sits in has rendered.
    const timer = setTimeout(() => {
      const element = root.current?.querySelector<HTMLElement>(`[data-focus~="${target}"]`) ?? root.current?.querySelector<HTMLElement>(`[data-section="${focusPlace(target).section}"]`);
      if (!element) return;
      element.scrollIntoView?.({ block: "center", behavior: "smooth" });
      element.setAttribute("data-flash", "");
      setTimeout(() => element.removeAttribute("data-flash"), 1400);
    }, 60);
    return () => clearTimeout(timer);
  }, [seq]);   // eslint-disable-line react-hooks/exhaustive-deps
}

export function StrategyEditorCard({ state, chartTimeframe, instruments, autonomy, onBack, onDiscard, onStart, onAsk, onSave, onLaunch }: Props) {
  const { form, backtest } = state;
  const { changed, edit } = fieldKit(state);
  const prefill = state.prefill;
  const eligible = Boolean(prefill?.eligible);
  const [tradesOpen, setTradesOpen] = useDisclosureSurface("strategy-editor-trades", { available: eligible && Boolean(prefill?.trades.length) });
  const [moreOpen, setMoreOpen] = useDisclosureSurface("strategy-editor-more");
  const [sizingMoreOpen, setSizingMoreOpen] = useDisclosureSurface("strategy-editor-sizing-more");
  const plan = planFromForm(form, state.base);
  const advanced = advancedRules(plan);
  const [advancedOpen, setAdvancedOpen] = useDisclosureSurface("strategy-editor-advanced", { available: advanced.length > 0 });
  const [detailOpen, setDetailOpen] = useDisclosureSurface("strategy-editor-backtest");
  const [picker, setPicker] = useState<string | null>(null);
  const [askText, setAskText] = useState("");
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { if (backtest.status !== "done") return; setNow(Date.now()); const id = setInterval(() => setNow(Date.now()), 10_000); return () => clearInterval(id); }, [backtest.status, backtest.at]);
  const detailRef = useRef<HTMLDivElement>(null);
  useEffect(() => { if (detailOpen) detailRef.current?.scrollIntoView?.({ block: "nearest" }); }, [detailOpen]);
  const root = useRef<HTMLElement>(null);
  useFocusFollow(state, root, target => {
    const place = focusPlace(target);
    if (place.more && place.section === "exit") setMoreOpen(true);
  });
  const result = backtest.result;
  // 回测的开平点 on the chart (the chart's own contract and period only): replaced by every new preview, cleared when the card goes.
  useEffect(() => { if (result && backtest.status === "done") setBacktestMarks(backtestMarksOf(result, "editor:")); }, [result, backtest.status]);
  useEffect(() => () => { if (currentBacktestMarks()?.backtestId.startsWith("editor:")) setBacktestMarks(null); }, []);
  const reminder = state.reminder;
  // 提醒模式 (第 6 批): stop and lots are not asked; the conditions are the 提醒条件.
  const missing = missingParts(form).map(part => reminder ? part.replace("开仓条件", "提醒条件") : part);
  const asking = state.ask?.status === "running";
  const busy = state.saving || Boolean(state.loading);
  const remindHits = reminder ? usedEntryGroups(form).map(key => groupHits(state, key).together).filter((hits): hits is number => hits != null) : [];
  const wins = result ? result.trades.filter(trade => trade.net > 0).length : 0;
  const tiles = result ? [
    { label: "交易", value: formatNumber(result.summary.trades, 0), note: `近 ${result.coverage.tradingDays} 个交易日` },
    { label: "胜率", value: result.summary.winRate == null ? "—" : `${Math.round(result.summary.winRate)}%`, note: `${wins} 赢 ${result.trades.length - wins} 亏` },
    { label: "扣费后净收益", value: <span className={signClass(result.summary.netProfit)}>{formatSigned(result.summary.netProfit, 0)}</span>,
      note: versus(result.summary.netProfit, backtest.previous?.netProfit, value => formatSigned(value, 0)) },
    { label: "最大回撤", value: result.summary.maxDrawdown ? `−${formatNumber(result.summary.maxDrawdown, 0)}` : "0",
      note: versus(-result.summary.maxDrawdown, backtest.previous ? -backtest.previous.maxDrawdown : null, value => value ? `−${formatNumber(-value, 0)}` : "0") },
  ] : [];
  const status = backtest.status === "done" ? `改动后自动重算 · ${timeAgo(backtest.at, now)}` : backtestProgress(backtest);
  const flow = editorFlow(state, chartTimeframe);
  const submitAsk = () => { const text = askText.trim(); if (!text || asking) return; onAsk(text); setAskText(""); };

  return <section className="se-card se-desk" aria-label="策略编辑卡" data-agent-target="strategy-editor" data-testid="strategy-editor" ref={root}>
    <header className="se-head">
      <button type="button" className="se-back" aria-label={reminder ? "返回我的提醒" : "返回我的策略"} onClick={onBack}><ArrowLeft size={14}/>{reminder ? "我的提醒" : "我的策略"}</button>
      <input className="se-name" aria-label={reminder ? "提醒名称" : "策略名称"} value={form.name} placeholder={reminder ? "未命名提醒" : "未命名策略"} maxLength={80}
        data-changed={changed("name") || undefined} onChange={event => edit("name", event.target.value)}/>
      {reminder ? <span className="se-grow"/> : <form className="se-ask" aria-label="补一句" onSubmit={event => { event.preventDefault(); submitAsk(); }}>
        <Sparkles size={14} aria-hidden="true"/>
        <input aria-label="补一句" value={askText} maxLength={500} disabled={asking}
          placeholder="说或打一句要改的，比如“止损改成 ATR 两倍”" onChange={event => setAskText(event.target.value)}/>
        <button type="submit" className="se-btn" disabled={asking || !askText.trim()}>{asking ? "AI 在改…" : "改"}</button>
      </form>}
      <button type="button" className="se-btn danger" onClick={onDiscard}>放弃</button>
    </header>

    <div className="se-cols">
      <div className="se-form">
        {/* 提醒 (改版第一步): the sentence is the first block — the AI fills the four below from it. */}
        {reminder ? <form className="se-say" aria-label="补一句" onSubmit={event => { event.preventDefault(); submitAsk(); }}>
          <label htmlFor="se-say-input">说一句什么时候提醒你</label>
          <span className="se-ask">
            <Sparkles size={14} aria-hidden="true"/>
            <input id="se-say-input" aria-label="补一句" value={askText} maxLength={500} disabled={asking}
              placeholder="比如“5 分钟线放量突破开盘区间高点提醒我”" onChange={event => setAskText(event.target.value)}/>
            <button type="submit" className="se-btn primary" disabled={asking || !askText.trim()}>{asking ? "AI 在填…" : "填好"}</button>
          </span>
          {state.ask?.status === "done" ? <span className="se-say-done" role="status">AI 已经把下面几项填好，看一眼再开始</span> : null}
        </form> : null}
        {state.ask?.status === "running" ? <p className="se-status" role="status">“{state.ask.text}” · {state.ask.stage ?? "AI 正在改"}</p> : null}
        {!reminder && state.ask?.status === "done" && state.changed.length ? <p className="se-status" role="status">已按“{state.ask.text}”改好，改到的地方标了出来，还没保存。</p> : null}
        {state.ask?.status === "failed" ? <p className="se-error" role="alert">{state.ask.error}</p> : null}
        {state.questions.length ? <div className="se-questions">
          <span className="se-hint">AI 还有几处要问你，答完再改一遍：</span>
          <StrategyQuestions questions={state.questions} answers={state.answers} round={state.history.length} busy={asking}
            onChange={(id, answer) => setEditorAnswer(id, answer)} onContinue={() => { try { regenerate(); } catch { /* the button stays disabled until answered */ } }}/>
        </div> : null}
        {state.error ? <p className="se-error" role="alert">{state.error}</p> : null}

        {!reminder && (state.mode === "new" || (state.aiFields.length > 0 && eligible)) ? <div className="se-start">
          {state.mode === "new" ? <StartChoice state={state} busy={busy} onStart={onStart}/> : null}
          {state.loading ? <span className="se-hint" role="status">{state.loading}</span>
            : state.aiFields.length > 0 && eligible ? <div className="se-ainote" role="note">
              <span><b>AI 按你的习惯预填了 {state.aiFields.length} 项</b> <AiTag/></span>
              <span className="se-act">
                {prefill?.trades.length ? <button type="button" className="se-btn ghost" aria-expanded={tradesOpen} onClick={() => setTradesOpen(open => !open)}>
                  查看这 {prefill.sample?.trades ?? prefill.trades.length} 笔</button> : null}
                <button type="button" className="se-btn ghost" onClick={() => onStart("blank")}>全部清空</button>
              </span>
              {prefill?.summary || prefill?.notice ? <span className="se-src">{prefill.summary ?? ""}{prefill.notice ? ` ${prefill.notice}` : ""}</span> : null}
            </div>
              : prefill && !prefill.eligible && prefill.reason ? <span className="se-hint">{prefill.reason}</span> : null}
        </div> : null}
        {tradesOpen && prefill?.trades.length ? <table className="se-trades" aria-label="预填依据的成交">
          <thead><tr><th scope="col">时间</th><th scope="col">合约</th><th scope="col">多空</th><th scope="col">手数</th><th scope="col">净盈亏</th></tr></thead>
          <tbody>{prefill.trades.map(trade => <tr key={trade.id}>
            <td>{new Date(trade.openTime / 1e6).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}</td>
            <td>{trade.symbol}</td><td>{trade.direction === "long" ? "多" : "空"}</td><td>{trade.volume}</td>
            <td className={signClass(trade.net)}>{formatSigned(trade.net, 0)}</td></tr>)}</tbody>
        </table> : null}

        <div data-section="basics" data-focus="basics trigger">
          <SectionCard title={<><span className="se-num">1</span>{reminder ? "盯哪个品种" : "品种、周期与方向"}</>} label="品种与周期" hint={EDITOR_HELP.basics} helpKey="strategy-editor:section-basics">
            <div className="section-card-body"><BasicsFields state={state} chartTimeframe={chartTimeframe} instruments={instruments}/></div>
          </SectionCard>
        </div>

        <div data-section="entry" data-focus="entry">
          <SectionCard title={<><span className="se-num">2</span>{reminder ? "什么时候提醒" : "什么时候开仓"}</>} label={reminder ? "什么时候提醒" : "什么时候开仓"} hint={EDITOR_HELP.entry} helpKey="strategy-editor:section-entry">
            <div className="section-card-body">
              <PickerContext.Provider value={{ open: picker, set: setPicker, spread: Boolean(plan.spread) }}><EntryFields state={state}/></PickerContext.Provider>
            </div>
          </SectionCard>
        </div>

        {reminder ? <div className="se-pair" data-section="sizing" data-focus="sizing">
          <SectionCard title={<><span className="se-num">3</span>提醒时段</>} label="提醒时段" hint={EDITOR_HELP.reminder} helpKey="strategy-editor:section-reminder">
            <div className="section-card-body"><WindowFields state={state}/></div>
          </SectionCard>
          <SectionCard title={<><span className="se-num">4</span>响几次</>} label="响几次" hint={EDITOR_HELP.repeat} helpKey="strategy-editor:section-repeat">
            <div className="section-card-body"><RepeatFields state={state}/></div>
          </SectionCard>
        </div> : <><div className="se-pair">
          <div data-section="exit" data-focus="exit">
            <SectionCard title={<><span className="se-num">3</span>怎么平仓</>} label="怎么平仓" hint={EDITOR_HELP.exit} helpKey="strategy-editor:section-exit">
              <div className="section-card-body">
                <PickerContext.Provider value={{ open: picker, set: setPicker, spread: Boolean(plan.spread) }}>
                  <ExitFields state={state} plan={plan} advanced={advanced} more={[moreOpen, setMoreOpen]} advancedFold={[advancedOpen, setAdvancedOpen]}/>
                </PickerContext.Provider>
              </div>
            </SectionCard>
          </div>
          <div data-section="sizing" data-focus="sizing">
            <SectionCard title={<><span className="se-num">4</span>仓位与时段</>} label="仓位与时段" hint={EDITOR_HELP.sizing} helpKey="strategy-editor:section-sizing">
              <div className="section-card-body"><SizingFields state={state} more={[sizingMoreOpen, setSizingMoreOpen]}/></div>
            </SectionCard>
          </div>
        </div>
        <SectionCard title="信号来了怎么办" label="信号来了怎么办" hint={EDITOR_HELP.signalAction} helpKey="strategy-editor:section-signal">
          <div className="section-card-body"><SignalActionFields state={state} autonomy={autonomy}/></div>
        </SectionCard></>}
      </div>

      <aside className="se-side" aria-label={reminder ? "开始提醒" : "流程与回测"}>
        {reminder ? <div className="se-bar se-remind">
          {missing.length ? <p className="se-oneline se-missing">还差：{missing.join("、")}</p>
            : <p className="se-oneline" role="status">{remindHits.length ? `近 ${PREVIEW_TRADING_DAYS} 个交易日${form.reminderOptions.watchlist ? `${contractLabel(form.symbol, instruments)}上` : ""}条件满足 ${formatNumber(remindHits.reduce((sum, hits) => sum + hits, 0), 0)} 次` : backtestProgress(backtest) || "正在数近 60 个交易日满足几次…"}</p>}
          <div className="se-footrow">
            <button type="button" className="se-btn" disabled={missing.length > 0 || busy} onClick={onSave}>{state.saving ? "保存中…" : "先存着不开始"}</button>
            <button type="button" className="se-btn primary" disabled={missing.length > 0 || busy} onClick={onLaunch}>开始提醒</button>
          </div>
        </div> : <>
        <SectionCard className="se-flow-card" title="策略流程" label="策略流程" hint={EDITOR_HELP.flow} helpKey="strategy-editor:flow" note={flow.basis ?? undefined}>
          <div className="section-card-body"><StrategyFlow flow={flow} onFocus={target => focusEditor(target)}/></div>
        </SectionCard>

        {detailOpen ? <div ref={detailRef} className="se-detail"><SectionCard title="回测明细" note={backtestDetailHint(state, instruments)}>
          <BacktestDetail state={state}/>
        </SectionCard></div> : null}

        <section className="se-foot section-card" aria-label="回测结果">
          <header className="section-card-head"><h3>回测</h3><HelpTip helpKey="strategy-editor:backtest" label="回测">{EDITOR_HELP.backtest}</HelpTip>
            <span className="section-card-hint se-meta" role="status">{missing.length ? "" : status}</span></header>
          {missing.length ? <p className="se-blank" role="status">填好开仓条件和止损后自动回测。<span className="se-missing">还差：{missing.join("、")}</span></p> : <>
            {result?.planSummary ? <p className="se-summary" title={result.planSummary}><span>一句话：</span>{result.planSummary}</p> : null}
            {tiles.length ? <StatTiles columns={4} label="实时回测" items={tiles}/> : <p className="se-blank" role="status">{backtest.status === "error" ? "" : "正在回测最近 60 个交易日…"}</p>}
            {result ? <EquityCurve points={result.equityCurve}/> : null}
            {backtest.status === "error" && backtest.error ? <p className="se-error" role="alert">{backtest.error}</p> : null}
          </>}
        </section>
        {/* The buttons sit in their own bar so a narrow card can keep just this bar (with one line of the four figures)
            at the bottom while the full 回测 block scrolls with the form; a wide card shows it as the block's last row. */}
        <div className="se-bar">
          {!missing.length && backtestProgress(backtest) ? <p className={`se-oneline${backtest.status === "error" ? " se-error" : ""}`} role={backtest.status === "error" ? "alert" : "status"}>
            {backtestProgress(backtest)}{backtest.status === "error" && backtest.error ? `：${backtest.error}` : ""}
          </p> : null}
          {missing.length ? <p className="se-oneline se-missing">还差：{missing.join("、")}</p>
            : result ? <p className="se-oneline" aria-label="回测汇总">
              <span className="se-oneline-basis">回测 · 扣费后（{moneyUnit(result.symbol)}）</span>
              <span>净收益 <b className={signClass(result.summary.netProfit)}>{formatSigned(result.summary.netProfit, 0)}</b></span>
              <span>最大回撤 <b>{result.summary.maxDrawdown ? `−${formatNumber(result.summary.maxDrawdown, 0)}` : "0"}</b></span>
              <span>胜率 <b>{result.summary.winRate == null ? "—" : `${Math.round(result.summary.winRate)}%`}</b></span>
              <span><b>{formatNumber(result.summary.trades, 0)}</b> 笔</span>
            </p> : null}
          <div className="se-footrow">
            <button type="button" className="se-btn" aria-expanded={detailOpen} onClick={() => setDetailOpen(open => !open)}>回测明细</button>
            <button type="button" className="se-btn" disabled={missing.length > 0 || busy} onClick={onSave}>{state.saving ? "保存中…" : "存为草稿"}</button>
            <button type="button" className="se-btn primary" disabled={missing.length > 0 || busy} onClick={onLaunch}>上线运行…</button>
          </div>
        </div>
        </>}
      </aside>
    </div>
  </section>;
}


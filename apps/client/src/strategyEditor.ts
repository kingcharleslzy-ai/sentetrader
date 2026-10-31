import { numberProblem, rangeHint, type NumberRule } from "./numberField";
import { conditionText, operandLabel, type Operand, type RuleCondition, type RuleGroup, type SizingMode, type StrategyPlan } from "./strategyApi";
import { MAX_BARS_AGO, MODIFIER_MAX, OPERATORS, PICKABLE, defaultPeriod, indicatorProblem, isCross, isTrend, operatorProblem, periodRange,
  usesBasis, usesK, usesPeriod, type IndicatorName, type Operator } from "./strategyIndicators";

export { OPERATORS, defaultPeriod, type Operator } from "./strategyIndicators";

/**
 * 策略编辑卡 (2026-10-08; 第二版 the same day: 宽版式、流程图、完整指标选择器): the form behind the one card that
 * creates or edits a strategy, and its two mappings — `formFromPlan` reads what the card can show, `planFromForm`
 * writes it back. Only fields the user changed are written: a save starts from the original plan (deep copy) and
 * overwrites the fields whose form value differs from `formFromPlan(original)`, and an unchanged condition row is
 * its original condition object — so everything the card cannot express (条件组合、先后顺序、
 * 盘中触价、回调、价差、系数偏移) and every field the user did not touch stays byte for byte.
 */
export type Timeframe = Exclude<StrategyPlan["timeframeSeconds"], 0>;
export type Direction = StrategyPlan["direction"];
export type GroupKey = "entryLong" | "entryShort" | "exitLong" | "exitShort";
export type ScaleTier = NonNullable<StrategyPlan["scaleOut"]>[number];
export type TrailingMode = NonNullable<StrategyPlan["trailingStop"]>["mode"];
export type PyramidTrigger = NonNullable<StrategyPlan["pyramiding"]>["trigger"];

export const TIMEFRAMES: { value: Timeframe; label: string }[] = [
  { value: 60, label: "1分" }, { value: 180, label: "3分" }, { value: 300, label: "5分" },
  { value: 900, label: "15分" }, { value: 3600, label: "1小时" }, { value: 86400, label: "日线" }];
export const timeframeLabel = (seconds: number) => TIMEFRAMES.find(item => item.value === seconds)?.label ?? `${seconds / 60}分`;
export const DIRECTIONS: { value: Direction; label: string }[] = [
  { value: "long", label: "只做多" }, { value: "short", label: "只做空" }, { value: "both", label: "双向" }];

/** 取: the operand itself, or its N 根均线 / N 根最高 / N 根最低 (the engine allows one of the three). */
export type Transform = "none" | "average" | "highest" | "lowest";
/** One side of a condition row: an operand with every parameter the engine reads. */
export interface OperandForm {
  indicator: IndicatorName | "";
  /** N (周期 / N 日 / 开盘 N 根 / 比 N 根前) — only for operands that read it. */
  period: number | null;
  /** 数值 (constant), 枢轴位的档位 (pivot). */
  value: number | null;
  /** 倍数 of 布林 / 肯特纳上下轨 and SuperTrend; null = the engine's default. */
  k: number | null;
  /** 肯特纳中轨: ema (null, the default) or ma. */
  basis: "ema" | "ma" | null;
  transform: Transform; transformN: number | null;
  /** 取 N 根前 (0 = this bar). */
  barsAgo: number;
  /** null = the strategy's own period (跟随); a fixed period otherwise. The right side always reads the left's. */
  timeframe: Timeframe | null;
}
export interface RowForm {
  id: string;
  left: OperandForm; op: Operator | ""; right: OperandForm;
  /** N 根内发生 (withinBars), N 根内至少 M 根 (minCount), 连续 N 根 (consecutive). */
  within: number; minCount: number; consecutive: number;
  /** The plan's condition this row came from: an unchanged row writes it back as it was. */
  source?: RuleCondition;
  /** Beyond the selector (系数偏移): shown read-only, can be deleted. */
  locked?: boolean;
}
export interface GroupForm {
  join: "all" | "any"; rows: RowForm[];
  /**
   * Entry groups only: 方向确认 (alignmentConditions — every one holds before `rows` are looked at; then the whole group
   * reads current states on the strategy's own period, joined by 全部满足) and 复位 (rearm — after an entry, these must
   * hold once before the side opens again; the strategy's own period).
   */
  alignment: RowForm[]; rearm: RowForm[];
  /** 条件组合 / 先后顺序: the whole group is read-only and kept as it is. */
  locked: boolean;
}
/** Which list of a group a row sits in. */
export type RowList = "rows" | "alignment" | "rearm";
export type StopMode = "ticks" | "atr" | "lookback" | "locked";
export interface PyramidForm {
  on: boolean; maxAdds: number | null; trigger: PyramidTrigger; step: number | null; atrPeriod: number | null;
  /** 每次加几手; null = 跟首笔. */
  volume: number | null; moveStop: boolean;
}
export interface EditorForm {
  name: string;
  /** The contract the card backtests on (context, not part of the plan). */
  symbol: string;
  /** 0 = follow the chart's period (an older plan); a new plan gets the chart's period. */
  timeframe: Timeframe | 0;
  /** null = 留空: inferred from the entry groups that have conditions. */
  direction: Direction | null;
  entryLong: GroupForm; entryShort: GroupForm; exitLong: GroupForm; exitShort: GroupForm;
  /** `risk` is the plan's 单笔预算 (perTradeRisk): required with 前低, optional otherwise (④ 更多). */
  stop: { mode: StopMode; value: number | null; risk: number | null };
  take: { mode: "ticks" | "none"; value: number | null };
  trailing: { on: boolean; mode: TrailingMode; value: number | null; activateAfter: number | null; atrPeriod: number | null };
  closeBeforeEnd: { on: boolean; minutes: number | null; everySession: boolean };
  /** 每次开仓手数 (按波动定手数时是上限; 按资金比例时不用). */
  lots: number | null; maxOrdersPerDay: number | null;
  entryWindows: { start: string; end: string }[];
  allowNightEntries: boolean;
  pyramiding: PyramidForm;
  breakeven: { on: boolean; unit: "ticks" | "atr"; trigger: number | null; offsetTicks: number | null };
  scaleOut: ScaleTier[];
  timeTakeProfit: { afterBars: number | null; unit: "ticks" | "atr"; profit: number | null }[];
  maxHoldBars: number | null;
  /**
   * 开仓手数 (2026-10-09): 固定手数 (`lots`, the old default) / 按资金比例 (`equityPercent` of the equity, `maxLots`
   * optional, none = no cap) / 按波动 (ATR, `lots` its required cap). Each mode keeps its own numbers while another is chosen.
   */
  sizing: { mode: SizingMode; riskPercent: number | null; atrMultiple: number | null; atrPeriod: number | null; equityPercent: number | null; maxLots: number | null };
  /** 回撤停机: off and blank by default; on with a blank percent blocks the save. */
  maxDrawdown: { on: boolean; percent: number | null };
  cooldownBars: number | null;
  maxEntriesPerDirection: number | null;
  maxLoss: number | null;
}
export type FieldName = Exclude<keyof EditorForm, never>;
export const FIELD_NAMES = ["name", "symbol", "timeframe", "direction", "entryLong", "entryShort", "stop", "take", "trailing", "closeBeforeEnd",
  "lots", "maxOrdersPerDay", "entryWindows", "allowNightEntries", "pyramiding", "breakeven", "scaleOut", "timeTakeProfit",
  "maxHoldBars", "exitLong", "exitShort", "sizing", "maxDrawdown", "cooldownBars", "maxEntriesPerDirection", "maxLoss"] as const satisfies readonly FieldName[];

/** 开仓时段's HH:MM (24 hours; the engine's EntryWindow pattern). */
export const isTime = (value: string) => /^([01]\d|2[0-3]):[0-5]\d$/.test(value);
/** What a 时段 box holds after typing: 930 / 0930 / 9:30 → 09:30, 21 → 21:00; anything else as typed. */
export function normalizeTime(raw: string): string {
  const text = raw.trim().replace("：", ":");
  const [hours, minutes] = text.includes(":") ? text.split(":") : text.length <= 2 ? [text, "0"] : [text.slice(0, -2), text.slice(-2)];
  if (!/^\d{1,2}$/.test(hours ?? "") || !/^\d{1,2}$/.test(minutes ?? "")) return text;
  const time = `${hours.padStart(2, "0")}:${minutes.padStart(2, "0")}`;
  return isTime(time) ? time : text;
}

/** Schema defaults of the fields a user may leave empty (the inputs' placeholders say them). */
export const DEFAULTS = { lots: 1, maxOrdersPerDay: 20, stopLossTicks: 20, closeBeforeEndMinutes: 5, cooldownBars: 1 } as const;
export const DEFAULT_NAME = "未命名策略";
const SIZING_FIXED: EditorForm["sizing"] = { mode: "fixed", riskPercent: null, atrMultiple: null, atrPeriod: null, equityPercent: null, maxLots: null };
export const SIZING_MODES: { value: SizingMode; label: string }[] = [
  { value: "fixed", label: "固定手数" }, { value: "equity_percent", label: "按资金比例" }, { value: "atr_risk", label: "按波动" }];
/** 开仓手数 switched: the other modes' numbers stay in the form; 按波动 shows its schema defaults (ATR 2 倍, 14 根). */
export function withSizingMode(sizing: EditorForm["sizing"], mode: SizingMode): EditorForm["sizing"] {
  return mode === "atr_risk" ? { ...sizing, mode, atrMultiple: sizing.atrMultiple ?? 2, atrPeriod: sizing.atrPeriod ?? 14 } : { ...sizing, mode };
}
const PYRAMID_OFF: PyramidForm = { on: false, maxAdds: 1, trigger: "profit_atr", step: 0.5, atrPeriod: 20, volume: null, moveStop: true };

let rowSequence = 0;
const rowId = () => `row-${++rowSequence}`;
export const emptyOperand = (): OperandForm => ({ indicator: "", period: null, value: null, k: null, basis: null, transform: "none", transformN: null, barsAgo: 0, timeframe: null });
/** An operand freshly picked: its defaults, the number / period kept when the same kind stays. */
export function operandFor(indicator: IndicatorName, extra: Partial<OperandForm> = {}): OperandForm {
  return { ...emptyOperand(), indicator, period: defaultPeriod(indicator), value: indicator === "constant" || indicator === "pivot" ? 0 : null, ...extra };
}
export const emptyRow = (): RowForm => ({ id: rowId(), left: emptyOperand(), op: "", right: emptyOperand(), within: 1, minCount: 1, consecutive: 1 });
const emptyGroup = (): GroupForm => ({ join: "all", rows: [], alignment: [], rearm: [], locked: false });

/** A new strategy: everything empty (no stop, no take-profit), the chart's contract and period. */
export function emptyForm(symbol: string, timeframe: number): EditorForm {
  return {
    name: "", symbol, timeframe: TIMEFRAMES.some(item => item.value === timeframe) ? timeframe as Timeframe : 300, direction: null,
    entryLong: emptyGroup(), entryShort: emptyGroup(), exitLong: emptyGroup(), exitShort: emptyGroup(),
    stop: { mode: "ticks", value: null, risk: null }, take: { mode: "ticks", value: null },
    trailing: { on: false, mode: "ticks", value: null, activateAfter: null, atrPeriod: null }, closeBeforeEnd: { on: false, minutes: null, everySession: false },
    lots: null, maxOrdersPerDay: null, entryWindows: [], allowNightEntries: true, pyramiding: { ...PYRAMID_OFF },
    breakeven: { on: false, unit: "ticks", trigger: null, offsetTicks: null }, scaleOut: [], timeTakeProfit: [], maxHoldBars: null,
    sizing: { ...SIZING_FIXED }, maxDrawdown: { on: false, percent: null }, cooldownBars: null, maxEntriesPerDirection: null, maxLoss: null,
  };
}

/** The plan a new strategy starts from: the schema defaults, with 止盈 explicitly 不设 (the schema's is 40). */
export function skeletonPlan(): StrategyPlan {
  return { name: DEFAULT_NAME, description: `策略编辑卡：${DEFAULT_NAME}`, direction: "both", timeframeSeconds: 0, maxPosition: 1, maxLoss: null,
    stopLossTicks: DEFAULTS.stopLossTicks, takeProfitTicks: null, cooldownBars: 1, maxOrdersPerDay: DEFAULTS.maxOrdersPerDay,
    closeBeforeEndScope: "day", allowNightEntries: true, rules: { entryLong: null, exitLong: null, entryShort: null, exitShort: null }, questions: [] };
}

// -- conditions ------------------------------------------------------------------------------------

const PICKABLE_SET = new Set<string>(PICKABLE);

function operandForm(operand: Operand, planTimeframe: number): OperandForm | null {
  if (operand.offset || !PICKABLE_SET.has(operand.indicator)) return null;
  const indicator = operand.indicator;
  const transform: Transform = operand.average ? "average" : operand.highest ? "highest" : operand.lowest ? "lowest" : "none";
  return {
    indicator,
    period: usesPeriod(indicator) ? operand.period ?? defaultPeriod(indicator) : null,
    value: indicator === "constant" || indicator === "pivot" ? operand.value ?? 0 : null,
    k: usesK(indicator) ? operand.k ?? null : null,
    basis: usesBasis(indicator) ? operand.basis ?? null : null,
    transform, transformN: operand.average ?? operand.highest ?? operand.lowest ?? null,
    barsAgo: indicator === "constant" ? 0 : operand.barsAgo ?? 0,
    timeframe: indicator === "constant" || !operand.timeframe || operand.timeframe === planTimeframe ? null : operand.timeframe as Timeframe,
  };
}

/** A plan condition as a card row; a condition beyond the selector (系数偏移) becomes a locked (read-only) row. */
export function rowFromCondition(condition: RuleCondition, planTimeframe: number): RowForm {
  const left = operandForm(condition.left, planTimeframe);
  const right = operandForm(condition.right, planTimeframe);
  const modifiers = { within: condition.withinBars ?? 1, minCount: condition.minCount ?? 1, consecutive: condition.consecutive ?? 1 };
  // The two sides of one comparison read one clock (the engine refuses anything else): a row that does not is kept as it is.
  const sameClock = condition.right.indicator === "constant" || (condition.left.timeframe ?? 0) === (condition.right.timeframe ?? 0);
  if (!left || !right || !sameClock) return { id: rowId(), left: emptyOperand(), op: "", right: emptyOperand(), ...modifiers, source: condition, locked: true };
  return { id: rowId(), left, op: condition.operator as Operator, right, ...modifiers, source: condition };
}

const whole = (value: number | null | undefined, min: number, max: number) => value != null && Number.isInteger(value) && value >= min && value <= max;

/** One operand's parameters against the engine's ranges (null = fine); the picker's 好 waits for it. */
export function operandProblem(operand: OperandForm): string | null {
  const indicator = operand.indicator;
  if (!indicator) return "还没选";
  if (indicator === "constant") return operand.value == null || !Number.isFinite(operand.value) ? "填一个数" : null;
  const range = periodRange(indicator);
  if (usesPeriod(indicator) && !whole(operand.period, range.min, range.max)) return `N：${range.min}–${range.max}`;
  if (usesK(indicator) && operand.k != null && !(operand.k > 0 && operand.k <= 10)) return "倍数：大于 0、最多 10";
  if (indicator === "pivot" && !whole(operand.value, -3, 3)) return "选一个枢轴档位";
  if (operand.transform !== "none" && !whole(operand.transformN, 2, 200)) return "取几根：2–200";
  if (!whole(operand.barsAgo, 0, MAX_BARS_AGO)) return `取几根前：0–${MAX_BARS_AGO}`;
  return null;
}

/** A new left side: 持仓四象限 brings 等于 (and its first kind); leaving it drops 等于. */
export function withLeft(row: RowForm, left: OperandForm): RowForm {
  if (left.indicator === "oi_quadrant") return { ...row, left, op: "eq", right: row.op === "eq" ? row.right : operandFor("constant", { value: 1 }) };
  return row.op === "eq" ? { ...row, left, op: "", right: emptyOperand() } : { ...row, left };
}
/** A new comparison: 上升 / 下降 take “几根前” (a number 1–20) on the right, 交叉 drops 连续 N 根. */
export function withOp(row: RowForm, op: Operator): RowForm {
  let right = row.right;
  if (isTrend(op) && !(right.indicator === "constant" && whole(right.value, 1, 20))) right = operandFor("constant", { value: 1 });
  else if (!isTrend(op) && op !== "eq" && (isTrend(row.op) || row.op === "eq")) right = emptyOperand();
  return { ...row, op, right, consecutive: isCross(op) ? 1 : row.consecutive };
}

/**
 * Why a row cannot be written yet, in the card's words (null = it is complete and the engine accepts it): the same
 * checks the Runtime's Operand / Condition validators and `check_executable` run on one condition.
 */
export interface RowContext {
  planTimeframe?: number; spread?: boolean;
  /** 方向确认 rows, and every row of a group that has some: current states only (高于 / 低于 / 不低于 / 不高于), no 修饰. */
  stateOnly?: boolean;
  /** 方向确认 / 复位 rows, and every row of a group with 方向确认: the strategy's own period (no fixed one). */
  ownClock?: boolean;
}
/** What a row of this list in this group may use (the engine's RuleGroup / EntryGroup checks). */
export function rowContext(form: EditorForm, key: GroupKey, list: RowList, spread = false): RowContext {
  const aligned = key.startsWith("entry") && form[key].alignment.length > 0;
  return { planTimeframe: form.timeframe, spread, stateOnly: list === "alignment" || (aligned && list === "rows"),
    ownClock: list !== "rows" || aligned };
}
export const STATE_OPERATORS: Operator[] = ["gt", "gte", "lt", "lte"];

export function rowProblem(row: RowForm, context: RowContext = {}): string | null {
  if (row.locked) return null;
  const { left, right, op } = row;
  if (!left.indicator) return "选左边的指标";
  if (!op) return "选比较方式";
  if (!right.indicator) return isTrend(op) ? "填和几根前比" : op === "eq" ? "选哪一种四象限" : "选右边的数值或指标";
  const leftProblem = indicatorProblem(left.indicator, "left", { spread: context.spread, op })
    ?? operatorProblem(op, left.indicator, row.consecutive, context.stateOnly) ?? indicatorProblem(right.indicator, "right", { spread: context.spread, op });
  if (leftProblem) return leftProblem;
  if (isTrend(op) && !whole(right.value, 1, 20)) return "上升 / 下降和几根前比：1–20";
  if (op === "eq" && !whole(right.value, 1, 4)) return "选哪一种四象限";
  if (right.indicator === "constant" && (right.value == null || !Number.isFinite(right.value))) return "填右边的数值";
  for (const [side, operand] of [["左边", left], ["右边", right]] as const) {
    const problem = operandProblem(operand);
    if (problem) return `${side} · ${problem}`;
  }
  const planTimeframe = context.planTimeframe ?? 0;
  if (left.timeframe && planTimeframe && left.timeframe < planTimeframe) return "固定周期不能比策略周期短";
  if (left.timeframe && context.ownClock && left.timeframe !== planTimeframe) return "方向确认、复位和有方向确认的条件只用策略自己的周期";
  if (!whole(row.within, 1, MODIFIER_MAX) || !whole(row.consecutive, 1, MODIFIER_MAX) || !whole(row.minCount, 1, MODIFIER_MAX)) return `根数：1–${MODIFIER_MAX}`;
  if (row.minCount > row.within) return "“N 根内至少 M 根”的 M 不能大于 N";
  if (row.consecutive > 1 && isCross(op)) return "交叉是一次性事件，不能要求连续 N 根";
  if (context.stateOnly && (row.within > 1 || row.consecutive > 1 || row.minCount > 1)) return "有方向确认时条件只看当前状态，不加修饰";
  return null;
}
export const rowComplete = (row: RowForm, context?: RowContext) => !rowProblem(row, context);

function operandOf(form: OperandForm, timeframe: Operand["timeframe"]): Operand {
  const indicator = form.indicator as IndicatorName;
  const operand: Operand = { indicator, timeframe, period: usesPeriod(indicator) ? form.period ?? defaultPeriod(indicator) ?? 5 : 5,
    value: indicator === "constant" || indicator === "pivot" ? form.value ?? 0 : 0, barsAgo: indicator === "constant" ? 0 : form.barsAgo };
  if (usesK(indicator) && form.k != null) operand.k = form.k;
  if (usesBasis(indicator) && form.basis) operand.basis = form.basis;
  if (indicator !== "constant" && form.transform !== "none" && form.transformN) operand[form.transform] = form.transformN;
  return operand;
}

/**
 * A row as a plan condition: an unchanged row returns its source as it was. A new or changed row reads the plan's
 * execution period (0 only when the plan itself follows the chart) unless it chose a fixed one, so a saved strategy
 * does not start following whatever chart it is launched on.
 */
export function conditionFromRow(row: RowForm, planTimeframe: number, note?: string): RuleCondition {
  if (row.source && row.locked) return row.source;
  if (row.source && rowKey(row) === rowKey(rowFromCondition(row.source, planTimeframe))) return row.source;
  const timeframe = (row.left.timeframe ?? planTimeframe) as Operand["timeframe"];
  const condition: RuleCondition = { label: "", left: operandOf(row.left, timeframe), operator: row.op as Operator,
    right: operandOf(row.right, timeframe), withinBars: row.within, consecutive: row.consecutive, minCount: row.minCount };
  condition.label = `${conditionText(condition)}${note ? `（${note}）` : ""}`;
  return condition;
}

/** The row's own words, the same as the condition it writes (a locked row: its source's). */
export function rowLabel(row: RowForm, planTimeframe = 0): string {
  if (row.locked && row.source) return row.source.label || conditionText(row.source);
  if (!rowComplete(row, { planTimeframe })) return "（没填完）";
  return conditionFromRow(row, planTimeframe).label;
}

function operandKey(operand: OperandForm) {
  const indicator = operand.indicator;
  return [indicator, usesPeriod(indicator) ? operand.period : null, indicator === "constant" || indicator === "pivot" ? operand.value : null,
    usesK(indicator) ? operand.k : null, usesBasis(indicator) ? operand.basis : null, operand.transform === "none" ? null : [operand.transform, operand.transformN],
    indicator === "constant" ? 0 : operand.barsAgo, indicator === "constant" ? null : operand.timeframe];
}
function rowKey(row: RowForm): string {
  if (row.locked) return JSON.stringify(["locked", row.source ? conditionText(row.source) : ""]);
  return JSON.stringify([operandKey(row.left), row.op, operandKey(row.right), row.within, row.minCount, row.consecutive]);
}
function groupKey(group: GroupForm) {
  return JSON.stringify([group.join, group.locked, group.rows.map(rowKey), group.alignment.map(rowKey), group.rearm.map(rowKey)]);
}

function groupForm(group: RuleGroup | null | undefined, planTimeframe: number): GroupForm {
  if (!group) return emptyGroup();
  const locked = Boolean(group.branches?.length || group.sequence);
  const rows = (list: RuleCondition[] | undefined) => (list ?? []).map(condition => rowFromCondition(condition, planTimeframe));
  const entry = group as RuleGroup & { alignmentConditions?: RuleCondition[]; rearm?: RuleCondition[] };
  return { join: group.join, locked, rows: rows(group.conditions), alignment: rows(entry.alignmentConditions), rearm: rows(entry.rearm) };
}
/** Where a row id sits in a group: its list and index (the phone's sheet edits rows of every list). */
export function findRow(group: GroupForm, rowId: string): { list: RowList; index: number; row: RowForm } | null {
  for (const list of ["rows", "alignment", "rearm"] as const) {
    const index = group[list].findIndex(row => row.id === rowId);
    if (index >= 0) return { list, index, row: group[list][index] };
  }
  return null;
}
/** The disclosure id part of a list: entryLong, entryLong-align, entryLong-rearm. */
export const listKey = (key: GroupKey, list: RowList) => list === "rows" ? key : `${key}-${list === "alignment" ? "align" : "rearm"}`;
export const LIST_WORDS: Record<RowList, string> = { rows: "", alignment: "方向确认", rearm: "复位" };

/** 常用: one click adds a familiar condition (the short side gets the mirror image). */
export const QUICK_ADDS = [
  { id: "ma-cross", label: "均线金叉" }, { id: "breakout", label: "突破前高" },
  { id: "macd-cross", label: "MACD 金叉" }, { id: "kd-low-cross", label: "KD 低位金叉" },
] as const;
/** A 常用 button's words on the short side: 金叉 → 死叉, 突破前高 → 跌破前低, 低位 → 高位. */
export const quickAddLabel = (label: string, long: boolean) => long ? label : label.replace("金叉", "死叉").replace("突破前高", "跌破前低").replace("低位", "高位");
export function quickRows(id: (typeof QUICK_ADDS)[number]["id"], long: boolean): RowForm[] {
  const operand = (indicator: IndicatorName, period: number | null = null, value: number | null = null) => operandFor(indicator, { period, value });
  const row = (left: OperandForm, op: Operator, right: OperandForm): RowForm => ({ ...emptyRow(), left, op, right });
  const cross: Operator = long ? "cross_up" : "cross_down";
  if (id === "ma-cross") return [row(operand("ma", 5), cross, operand("ma", 20))];
  if (id === "breakout") return [long ? row(operand("close"), "cross_up", operand("donchian_high", 20)) : row(operand("close"), "cross_down", operand("donchian_low", 20))];
  if (id === "macd-cross") return [row(operand("macd_dif"), cross, operand("macd_dea"))];
  return [row(operand("kd_k"), cross, operand("kd_d")), row(operand("kd_k"), long ? "lt" : "gt", operand("constant", null, long ? 30 : 70))];
}

// -- plan → form ------------------------------------------------------------------------------------

/** 最多持有: 每次的手数, plus every 加仓 (the engine's first entry is maxPosition − 加仓手数 × 次数). */
export function maxPositionOf(form: EditorForm): number {
  const lots = form.lots ?? DEFAULTS.lots;
  const pyramid = form.pyramiding;
  // 跟首笔 (no volume) counts one lot per 加仓, as the engine's cap does (strategy_engine: maxPosition − (volume or 1) × maxAdds).
  return pyramid.on ? lots + (pyramid.volume ?? 1) * (pyramid.maxAdds ?? 1) : lots;
}

/** What the card shows of a plan. `symbol` is context (the plan has no contract). */
export function formFromPlan(plan: StrategyPlan, symbol: string): EditorForm {
  const tf = plan.timeframeSeconds;
  const pyramid = plan.pyramiding;
  const first = pyramid ? Math.max(1, plan.maxPosition - (pyramid.volume || 1) * pyramid.maxAdds) : plan.maxPosition;
  const risk = plan.perTradeRisk ?? null;
  const stop: EditorForm["stop"] = plan.pullback || plan.spread ? { mode: "locked", value: null, risk }
    : plan.stopLookbackBars != null ? { mode: "lookback", value: plan.stopLookbackBars, risk }
    : plan.atrStop ? { mode: "atr", value: plan.atrStop.multiple, risk }
    : { mode: "ticks", value: plan.stopLossTicks, risk };
  const trailing = plan.trailingStop;
  const breakeven = plan.breakeven;
  const sizing = plan.sizing;
  return {
    name: plan.name, symbol, timeframe: tf, direction: plan.direction,
    entryLong: groupForm(plan.rules.entryLong, tf), entryShort: groupForm(plan.rules.entryShort, tf),
    exitLong: groupForm(plan.rules.exitLong, tf), exitShort: groupForm(plan.rules.exitShort, tf),
    stop, take: plan.takeProfitTicks == null ? { mode: "none", value: null } : { mode: "ticks", value: plan.takeProfitTicks },
    trailing: trailing ? { on: true, mode: trailing.mode, value: trailing.value, activateAfter: trailing.activateAfter || null, atrPeriod: trailing.atrPeriod ?? 14 }
      : { on: false, mode: "ticks", value: null, activateAfter: null, atrPeriod: null },
    closeBeforeEnd: { on: Boolean(plan.closeBeforeEndMinutes), minutes: plan.closeBeforeEndMinutes || null, everySession: plan.closeBeforeEndScope === "session" },
    lots: first, maxOrdersPerDay: plan.maxOrdersPerDay,
    entryWindows: (plan.entryWindows ?? []).map(window => ({ start: window.start, end: window.end })),
    allowNightEntries: plan.allowNightEntries !== false,
    pyramiding: pyramid ? { on: true, maxAdds: pyramid.maxAdds, trigger: pyramid.trigger ?? "profit_atr", step: pyramid.step ?? 0.5,
      atrPeriod: pyramid.atrPeriod ?? 20, volume: pyramid.volume ?? null, moveStop: pyramid.moveStop !== false } : { ...PYRAMID_OFF },
    breakeven: breakeven ? { on: true, unit: breakeven.triggerTicks != null ? "ticks" : "atr", trigger: breakeven.triggerTicks ?? breakeven.triggerAtr ?? null,
      offsetTicks: breakeven.offsetTicks ?? 0 } : { on: false, unit: "ticks", trigger: null, offsetTicks: null },
    scaleOut: (plan.scaleOut ?? []).map(tier => ({ ...tier })),
    timeTakeProfit: (plan.timeTakeProfit ?? []).map(tier => ({ afterBars: tier.afterBars, unit: tier.profitTicks != null ? "ticks" as const : "atr" as const,
      profit: tier.profitTicks ?? tier.profitAtr ?? null })),
    maxHoldBars: plan.maxHoldBars ?? null,
    sizing: sizing?.mode === "atr_risk" ? { ...SIZING_FIXED, mode: "atr_risk", riskPercent: sizing.riskPercent ?? null, atrMultiple: sizing.atrMultiple ?? 2, atrPeriod: sizing.atrPeriod ?? 14 }
      : sizing?.mode === "equity_percent" ? { ...SIZING_FIXED, mode: "equity_percent", equityPercent: sizing.equityPercent ?? null, maxLots: sizing.maxLots ?? null }
      : { ...SIZING_FIXED },
    maxDrawdown: plan.maxDrawdownPercent != null ? { on: true, percent: plan.maxDrawdownPercent } : { on: false, percent: null },
    cooldownBars: plan.cooldownBars ?? DEFAULTS.cooldownBars,
    maxEntriesPerDirection: plan.maxEntriesPerDirection ?? null,
    maxLoss: plan.maxLoss ?? null,
  };
}

/** What a 开仓手数 choice writes: only the chosen mode's own numbers (another mode's leftovers are not a change). */
function sizingKey(sizing: EditorForm["sizing"]) {
  return JSON.stringify(sizing.mode === "atr_risk" ? [sizing.mode, sizing.riskPercent, sizing.atrMultiple, sizing.atrPeriod]
    : sizing.mode === "equity_percent" ? [sizing.mode, sizing.equityPercent, sizing.maxLots] : [sizing.mode]);
}
/** Whether a field's form value differs (rows compare by content, not by row id or source object). */
export function fieldDiffers(name: FieldName, a: EditorForm, b: EditorForm): boolean {
  const value = (form: EditorForm) => {
    const field = form[name];
    if (name === "entryLong" || name === "entryShort" || name === "exitLong" || name === "exitShort") return groupKey(field as GroupForm);
    if (name === "sizing") return sizingKey(form.sizing);
    // 回撤停机 off writes nothing, whatever number is left in the box.
    if (name === "maxDrawdown") return JSON.stringify(form.maxDrawdown.on ? form.maxDrawdown.percent : "off");
    return JSON.stringify(field);
  };
  return value(a) !== value(b);
}
export const changedFields = (a: EditorForm, b: EditorForm) => FIELD_NAMES.filter(name => fieldDiffers(name, a, b));

/** Clear an optional block without adding a key the original plan did not have (round trips stay equal). */
function clear<K extends keyof StrategyPlan>(plan: StrategyPlan, key: K) { if (plan[key] != null) (plan as Record<K, unknown>)[key] = null; }

// -- form → plan ------------------------------------------------------------------------------------

/** 方向留空: inferred from the entry groups that have conditions (only 做多 → long). */
export function effectiveDirection(form: EditorForm): Direction {
  if (form.direction) return form.direction;
  const long = form.entryLong.rows.length > 0 || form.entryLong.locked, short = form.entryShort.rows.length > 0 || form.entryShort.locked;
  return long && !short ? "long" : short && !long ? "short" : "both";
}

function writeGroup(plan: StrategyPlan, key: GroupKey, group: GroupForm, was: RuleGroup | null | undefined) {
  if (group.locked && was) return;
  const planTimeframe = plan.timeframeSeconds;
  const entry = key.startsWith("entry");
  const aligned = entry && group.alignment.length > 0;
  const written = (list: RowList) => group[list].filter(row => rowComplete(row, { planTimeframe, stateOnly: list === "alignment" || (aligned && list === "rows"),
    ownClock: list !== "rows" || aligned })).map(row => conditionFromRow(row, planTimeframe));
  const conditions = written("rows");
  if (!conditions.length) { plan.rules[key] = null; return; }
  const base: RuleGroup = was ? structuredClone(was) : { join: "all", sequence: false };
  const next = { ...base, join: group.join, conditions } as RuleGroup & { alignmentConditions?: RuleCondition[]; rearm?: RuleCondition[] };
  if (entry) {
    // Written when there are some, or when the plan had the key (an emptied list stays an empty list).
    const alignment = written("alignment"), rearm = written("rearm");
    if (alignment.length || "alignmentConditions" in base) next.alignmentConditions = alignment;
    if (rearm.length || "rearm" in base) next.rearm = rearm;
  }
  plan.rules[key] = next;
}

/**
 * The plan to save or backtest. `base` is the plan being edited (a template's or the AI's for a new one);
 * without it the form is written over the schema defaults. Only fields that differ from
 * `formFromPlan(base)` are written. `description` replaces the plan's text when given.
 */
export function planFromForm(form: EditorForm, base?: StrategyPlan | null, description?: string): StrategyPlan {
  const plan = structuredClone(base ?? skeletonPlan());
  const was = base ? formFromPlan(base, form.symbol) : null;
  const changed = (name: FieldName) => !was || fieldDiffers(name, form, was);
  if (changed("name")) plan.name = form.name.trim() || DEFAULT_NAME;
  if (changed("timeframe")) plan.timeframeSeconds = form.timeframe;
  if (changed("direction") || changed("entryLong") || changed("entryShort")) plan.direction = effectiveDirection(form);
  // A new execution period moves the card's own following rows onto it (fixed-period and read-only rows keep theirs).
  for (const key of ["entryLong", "entryShort", "exitLong", "exitShort"] as const) if (changed(key) || changed("timeframe")) writeGroup(plan, key, form[key], base?.rules[key]);
  // 只做多 / 只做空: the folded side's entries are not part of the plan (the Runtime refuses a long-only plan with 开空条件).
  if (plan.direction === "long" && plan.rules.entryShort != null) plan.rules.entryShort = null;
  if (plan.direction === "short" && plan.rules.entryLong != null) plan.rules.entryLong = null;
  // The stop itself and 单笔预算 are written apart: a changed budget leaves the stop's own keys untouched.
  if ((!was || form.stop.mode !== was.stop.mode || form.stop.value !== was.stop.value) && form.stop.mode !== "locked") {
    const value = form.stop.value;
    if (form.stop.mode === "ticks") { plan.stopLossTicks = value ?? DEFAULTS.stopLossTicks; clear(plan, "atrStop"); clear(plan, "stopLookbackBars"); }
    else if (form.stop.mode === "atr") { plan.atrStop = { multiple: value ?? 2, period: base?.atrStop?.period ?? 14 }; clear(plan, "stopLookbackBars"); }
    else { plan.stopLookbackBars = value ?? 5; clear(plan, "atrStop"); }
  }
  if (was ? form.stop.risk !== was.stop.risk : form.stop.risk != null) {
    if (form.stop.risk != null) plan.perTradeRisk = form.stop.risk; else clear(plan, "perTradeRisk");
  }
  if (changed("take")) plan.takeProfitTicks = form.take.mode === "ticks" && form.take.value != null ? form.take.value : null;
  if (changed("trailing")) {
    if (form.trailing.on && form.trailing.value != null) plan.trailingStop = { ...(base?.trailingStop ?? {}),
      mode: form.trailing.mode, value: form.trailing.value, activateAfter: form.trailing.activateAfter ?? 0, atrPeriod: form.trailing.atrPeriod ?? 14 };
    else clear(plan, "trailingStop");
  }
  if (changed("closeBeforeEnd")) {
    if (form.closeBeforeEnd.on) plan.closeBeforeEndMinutes = form.closeBeforeEnd.minutes ?? DEFAULTS.closeBeforeEndMinutes;
    else clear(plan, "closeBeforeEndMinutes");
    // Written only when it differs from the plan's own scope, so a plan without the key keeps none.
    if (form.closeBeforeEnd.everySession !== (base?.closeBeforeEndScope === "session")) plan.closeBeforeEndScope = form.closeBeforeEnd.everySession ? "session" : "day";
  }
  if (changed("lots") || changed("pyramiding")) {
    const lots = form.lots ?? DEFAULTS.lots;
    const pyramid = form.pyramiding;
    if (pyramid.on) {
      plan.maxPosition = maxPositionOf(form);
      plan.pyramiding = { maxAdds: pyramid.maxAdds ?? 1, trigger: pyramid.trigger, step: pyramid.step ?? (pyramid.trigger === "profit_ticks" ? 10 : 0.5),
        atrPeriod: pyramid.atrPeriod ?? 20, volume: pyramid.volume, moveStop: pyramid.moveStop };
    } else { plan.maxPosition = lots; clear(plan, "pyramiding"); }
  }
  if (changed("maxOrdersPerDay")) plan.maxOrdersPerDay = form.maxOrdersPerDay ?? DEFAULTS.maxOrdersPerDay;
  if (changed("entryWindows")) plan.entryWindows = form.entryWindows.filter(window => isTime(window.start) && isTime(window.end)).map(window => ({ ...window }));
  if (changed("allowNightEntries")) plan.allowNightEntries = form.allowNightEntries;
  if (changed("breakeven")) {
    const breakeven = form.breakeven;
    if (breakeven.on && breakeven.trigger != null) plan.breakeven = { triggerTicks: breakeven.unit === "ticks" ? breakeven.trigger : null,
      triggerAtr: breakeven.unit === "atr" ? breakeven.trigger : null, offsetTicks: breakeven.offsetTicks ?? 0 };
    else clear(plan, "breakeven");
  }
  if (changed("scaleOut")) plan.scaleOut = form.scaleOut.map(tier => ({ ...tier }));
  if (changed("timeTakeProfit")) plan.timeTakeProfit = form.timeTakeProfit.filter(tier => tier.afterBars != null && tier.profit != null)
    .map(tier => ({ afterBars: tier.afterBars!, profitTicks: tier.unit === "ticks" ? tier.profit : null, profitAtr: tier.unit === "atr" ? tier.profit : null }));
  if (changed("maxHoldBars")) plan.maxHoldBars = form.maxHoldBars;
  if (changed("sizing")) {
    const sizing = form.sizing;
    if (sizing.mode === "atr_risk") plan.sizing = { mode: "atr_risk", riskPercent: sizing.riskPercent, atrMultiple: sizing.atrMultiple ?? 2, atrPeriod: sizing.atrPeriod ?? 14 };
    // 按资金比例 never reads maxPosition: its only cap is the optional 最多 N 手 (no default).
    else if (sizing.mode === "equity_percent") plan.sizing = { mode: "equity_percent", equityPercent: sizing.equityPercent, maxLots: sizing.maxLots,
      atrMultiple: base?.sizing?.atrMultiple ?? 2, atrPeriod: base?.sizing?.atrPeriod ?? 14 };
    else clear(plan, "sizing");
  }
  if (changed("maxDrawdown")) {
    if (form.maxDrawdown.on && form.maxDrawdown.percent != null) plan.maxDrawdownPercent = form.maxDrawdown.percent;
    else clear(plan, "maxDrawdownPercent");
  }
  if (changed("cooldownBars")) plan.cooldownBars = form.cooldownBars ?? DEFAULTS.cooldownBars;
  if (changed("maxEntriesPerDirection")) { if (form.maxEntriesPerDirection != null) plan.maxEntriesPerDirection = form.maxEntriesPerDirection; else clear(plan, "maxEntriesPerDirection"); }
  if (changed("maxLoss")) { if (form.maxLoss != null) plan.maxLoss = form.maxLoss; else clear(plan, "maxLoss"); }
  if (description !== undefined) plan.description = description;
  // The form is the answer: the designer's open questions stay on the card as optional prompts, but the
  // plan built from the form carries none (the Runtime refuses to backtest or save a plan that has some).
  if (plan.questions?.length) plan.questions = [];
  if (plan.clarifications?.length) plan.clarifications = [];
  return plan;
}

// -- what still blocks a backtest / save, and the read-only rules ----------------------------------

export const SIDE_WORDS: Record<GroupKey, string> = { entryLong: "做多", entryShort: "做空", exitLong: "平多", exitShort: "平空" };
/** The entry groups the direction uses (留空 = both). */
export function usedEntryGroups(form: EditorForm): ("entryLong" | "entryShort")[] {
  return form.direction === "long" ? ["entryLong"] : form.direction === "short" ? ["entryShort"] : ["entryLong", "entryShort"];
}
/**
 * The card's typed counts (2026-10-13, #755 / #759): the server's StrategyPlan limits, read by the same rule the
 * order ticket uses (numberField.ts). Empty = the default (placeholder); a value outside the rule blocks the
 * backtest, 存为草稿 and 上线 (它进“还差”), is marked on its box and never drawn in the flow chart.
 */
export const LOTS_RULE: NumberRule = { label: "每次开仓手数", min: 1, max: 20, integer: true, unit: "手", optional: true };
export const MAX_ORDERS_RULE: NumberRule = { label: "每天最多开仓次数", min: 1, max: 50, integer: true, unit: "次", optional: true };
export const TAKE_TICKS_RULE: NumberRule = { label: "止盈跳数", min: 1, max: 10000, integer: true, unit: "跳", optional: true };
export function stopRule(mode: EditorForm["stop"]["mode"]): NumberRule {
  if (mode === "atr") return { label: "止损 ATR 倍数", min: 0, minExclusive: true, max: 20, optional: true };
  if (mode === "lookback") return { label: "止损前低根数", min: 2, max: 200, integer: true, unit: "根", optional: true };
  return { label: "止损跳数", min: 1, max: 5000, integer: true, unit: "跳", optional: true };
}
/** Each ruled count with its rule, as the form holds it now. */
function ruledCounts(form: EditorForm): [number | null, NumberRule][] {
  return [
    ...(form.sizing.mode !== "equity_percent" ? [[form.lots, LOTS_RULE] as [number | null, NumberRule]] : []),
    [form.maxOrdersPerDay, MAX_ORDERS_RULE],
    ...(form.stop.mode !== "locked" ? [[form.stop.value, stopRule(form.stop.mode)] as [number | null, NumberRule]] : []),
    ...(form.take.mode === "ticks" ? [[form.take.value, TAKE_TICKS_RULE] as [number | null, NumberRule]] : []),
  ];
}
export function missingParts(form: EditorForm): string[] {
  const missing: string[] = [];
  const used = usedEntryGroups(form);
  const ready = used.some(key => form[key].locked || form[key].rows.some(row => rowComplete(row, rowContext(form, key, "rows"))));
  if (!ready) missing.push("开仓条件");
  for (const key of ["entryLong", "entryShort", "exitLong", "exitShort"] as const) {
    if (!key.startsWith("exit") && !used.includes(key as "entryLong")) continue;
    for (const list of ["rows", "alignment", "rearm"] as const) {
      const index = form[key][list].findIndex(row => !rowComplete(row, rowContext(form, key, list)));
      if (index >= 0) missing.push(`${SIDE_WORDS[key]}${LIST_WORDS[list]}第 ${index + 1} 条条件`);
    }
    if (form[key].alignment.length && form[key].join === "any") missing.push(`${SIDE_WORDS[key]}有方向确认时条件要全部满足`);
  }
  if (form.stop.mode !== "locked" && form.stop.value == null) missing.push("止损");
  if (form.stop.mode === "lookback" && form.stop.risk == null) missing.push("单笔预算");
  if (form.sizing.mode === "atr_risk" && form.sizing.riskPercent == null) missing.push("单笔风险占权益的百分比");
  if (form.sizing.mode === "equity_percent" && form.sizing.equityPercent == null) missing.push("资金占用上限");
  if (form.maxDrawdown.on && form.maxDrawdown.percent == null) missing.push("回撤停机的比例");
  // A switch turned on with its number blank is not silently dropped from the plan: it blocks until filled or turned off.
  if (form.trailing.on && form.trailing.value == null) missing.push("追踪止损的回撤");
  if (form.breakeven.on && form.breakeven.trigger == null) missing.push("保本的浮盈触发");
  const tier = form.timeTakeProfit.findIndex(item => item.afterBars == null || item.profit == null);
  if (tier >= 0) missing.push(`按时间止盈第 ${tier + 1} 档`);
  if (form.entryWindows.some(window => !isTime(window.start) || !isTime(window.end))) missing.push("开仓时段（写成 09:30 这样的 24 小时制）");
  for (const [value, rule] of ruledCounts(form)) if (numberProblem(value, rule)) missing.push(`${rule.label}（${rangeHint(rule)}）`);
  return missing;
}

/** 近 60 个交易日满足次数 of the last preview: per row (by its label) and the group's together (同时 / 任一). */
export type Diagnosis = { join?: "all" | "any"; anyPass?: number; allPass: number; conditions: { label: string; alone: number }[] };
export function hitsOf(form: EditorForm, key: "entryLong" | "entryShort", result: { diagnosis?: Partial<Record<"entryLong" | "entryShort", Diagnosis>> } | undefined):
  { hits: (number | null)[]; together: number | null } {
  const diagnosis = result?.diagnosis?.[key];
  const group = form[key];
  const context = { planTimeframe: form.timeframe };
  const hits = group.rows.map(row => {
    if (!diagnosis || !rowComplete(row, context)) return null;
    const label = rowLabel(row, form.timeframe);
    return diagnosis.conditions.find(item => item.label === label)?.alone ?? null;
  });
  const together = diagnosis ? (diagnosis.join ?? group.join) === "any" ? diagnosis.anyPass ?? null : diagnosis.allPass : null;
  return { hits, together };
}

// -- 手机编辑卡 (2026-10-08): its folded sections and its condition rows in words ----------------------

/** One operand in words (the Runtime's describe_operand), the right side on the left's clock. */
export function operandText(form: OperandForm, timeframe = 0): string {
  if (!form.indicator) return "";
  return operandLabel(operandOf(form, (form.timeframe ?? timeframe) as Operand["timeframe"]));
}
/** A row's three parts in words (left / 比较 / right; "" while unchosen) — the phone's one-line row and its sheet's steps. */
export function rowParts(row: RowForm): { left: string; op: string; right: string } {
  const op = OPERATORS.find(item => item.value === row.op)?.label ?? "";
  const left = operandText(row.left);
  if (isTrend(row.op)) return { left, op: `较 ${row.right.value ?? "?"} 根前${op}`, right: "" };
  if (row.op === "eq") return { left, op: "等于", right: quadrantLabel(row.right.value) };
  return { left, op, right: operandText(row.right) };
}
const quadrantLabel = (value: number | null) => ({ 1: "多头增仓", 2: "空头增仓", 3: "空头减仓", 4: "多头减仓" } as Record<number, string>)[value ?? 0] ?? "";
/** The row's modifiers in words (连续 2 根 · 5 根内 …), shown after the row. */
export function rowModifiers(row: RowForm): string[] {
  const words: string[] = [];
  if (row.minCount > 1) words.push(`${row.within} 根内至少 ${row.minCount} 根`);
  else if (row.within > 1) words.push(`${row.within} 根内发生`);
  if (row.consecutive > 1) words.push(`连续 ${row.consecutive} 根`);
  return words;
}

export type EditorSection = "basics" | "entry" | "exit" | "sizing";
export const EDITOR_SECTIONS: { id: EditorSection; title: string }[] = [
  { id: "basics", title: "品种与周期" }, { id: "entry", title: "开仓" }, { id: "exit", title: "平仓" }, { id: "sizing", title: "仓位与时段" }];
/** The section a condition group's rows are edited in. */
export const sectionOfGroup = (key: GroupKey): EditorSection => key.startsWith("entry") ? "entry" : "exit";

/**
 * A folded section's one line on the phone: 品种 · 周期 · 方向 / each side's 条件数, how they join and the
 * 同时满足次数 of the last preview / 止损 · 止盈 · 收盘前平仓 / 每次手数 · 开仓时段.
 */
export function sectionSummary(section: EditorSection, form: EditorForm, context: { symbolText: string; chartTimeframe?: number;
  together?: Partial<Record<"entryLong" | "entryShort", number | null>> }): string {
  if (section === "basics") {
    const period = form.timeframe ? timeframeLabel(form.timeframe) : "跟随图表";
    return [context.symbolText || "合约没填", period, DIRECTIONS.find(item => item.value === form.direction)?.label ?? "方向留空"].join(" · ");
  }
  if (section === "entry") {
    const sides = usedEntryGroups(form).filter(key => form[key].locked || form[key].rows.length).map(key => {
      const group = form[key];
      if (group.locked) return `${SIDE_WORDS[key]} · 条件组合（只读）`;
      const together = context.together?.[key];
      return `${SIDE_WORDS[key]} · ${group.rows.length} 条${group.join === "any" ? "任一" : "全部"}满足${together != null ? ` · ${together} 次` : ""}`;
    });
    return sides.join("；") || "还没有开仓条件";
  }
  if (section === "exit") return [stopText(form), form.take.mode === "ticks" && form.take.value != null ? numberProblem(form.take.value, TAKE_TICKS_RULE) ? "止盈待改" : `止盈 ${form.take.value} 跳` : "不设止盈",
    form.closeBeforeEnd.on ? `收盘前 ${form.closeBeforeEnd.minutes ?? DEFAULTS.closeBeforeEndMinutes} 分平` : ""].filter(Boolean).join(" · ");
  const windows = form.entryWindows.filter(window => window.start && window.end).map(window => `${window.start}–${window.end}`);
  return [sizingText(form), form.maxDrawdown.on && form.maxDrawdown.percent != null ? `回撤 ${form.maxDrawdown.percent}% 停机` : "",
    windows.length ? windows.join("、") : "时段不限"].filter(Boolean).join(" · ");
}

/** 开仓手数 in a few words (the phone's summary line and the flow chart's 开仓 block). */
export function sizingText(form: EditorForm): string {
  const sizing = form.sizing;
  if (sizing.mode === "equity_percent") return `按资金比例 ${sizing.equityPercent ?? "?"}% 权益${sizing.maxLots ? ` · 最多 ${sizing.maxLots} 手` : ""}`;
  if (numberProblem(form.lots, LOTS_RULE)) return "手数待改";
  if (sizing.mode === "atr_risk") return `按波动定手数 · 最多 ${maxPositionOf(form)} 手`;
  return `每次 ${form.lots ?? DEFAULTS.lots} 手`;
}

/** 止损 in a few words (the phone's summary line and the flow chart's block). */
export function stopText(form: EditorForm, spread = false): string {
  const stop = form.stop;
  return stop.mode === "locked" ? spread ? "价差止损（见高级规则）" : "回调结构止损（见高级规则）" : stop.value == null ? "止损没填"
    : numberProblem(stop.value, stopRule(stop.mode)) ? "止损待改"
    : stop.mode === "atr" ? `止损 ${stop.value} 倍 ATR` : stop.mode === "lookback" ? `止损 前 ${stop.value} 根低点` : `止损 ${stop.value} 跳`;
}

/**
 * 高级规则（卡片里不能改）: what the plan has that the card cannot edit, each as one plain line — 条件组合、先后顺序、
 * 盘中触价、回调结构与价差.
 */
export function advancedRules(plan: StrategyPlan | null | undefined): string[] {
  if (!plan) return [];
  const rows: string[] = [];
  for (const key of ["entryLong", "entryShort", "exitLong", "exitShort"] as const) {
    const group = plan.rules[key];
    if (!group) continue;
    const side = SIDE_WORDS[key];
    if (group.branches?.length) rows.push(`${side} · 条件组合：${group.branches.map(branch => `（${branch.conditions.map(conditionText).join(branch.join === "all" ? " 且 " : " 或 ")}）`).join(" ")}`);
    if (group.sequence) rows.push(`${side} · 按先后顺序全部满足`);
    if ("trigger" in group && group.trigger) rows.push(`${side} · 盘中触价：收盘成立后挂「${operandLabel(group.trigger.price)}」，下一根K线内触及才开仓`);
  }
  if (plan.pullback) rows.push(`回调结构：MA${plan.pullback.trendPeriod} 顺势回调到 MA${plan.pullback.touchPeriod}，守住 MA${plan.pullback.guardPeriod}，最多等 ${plan.pullback.maxBars} 根`);
  if (plan.spread) rows.push(`价差：${plan.spread.legs.map(leg => leg.symbol).join(plan.spread.kind === "ratio" ? " ÷ " : " − ")}（止损 ${plan.spread.stopLoss ?? "不设"} 点、止盈 ${plan.spread.takeProfit ?? "不设"} 点）`);
  return rows;
}

export type PrefillResponse = {
  eligible: boolean; reason?: string;
  window?: { tradingDays: number; from?: string; to?: string };
  sample?: { trades: number; minTrades: number };
  summary?: string;
  fields: {
    symbol?: { value: string; label?: string; basis: string; tradeIds?: string[] };
    direction?: { value: "long" | "short"; basis: string; tradeIds?: string[] };
    stopLossTicks?: { value: number; basis: string; tradeIds?: string[] };
    closeBeforeEndMinutes?: { value: number; basis: string; tradeIds?: string[] };
    entryWindow?: { value: { start: string; end: string }; basis: string; tradeIds?: string[] };
    lots?: { value: number; basis: string; tradeIds?: string[] };
  };
  trades: { id: string; symbol: string; direction: "long" | "short"; volume: number; openTime: number; closeTime: number; openPrice: number; closePrice: number; net: number }[];
  notice?: string;
};

/** 按我的习惯: the prefill's fields written over a blank form; returns the fields marked AI and their basis. */
export function applyPrefill(form: EditorForm, prefill: PrefillResponse): { form: EditorForm; aiFields: FieldName[]; basis: Partial<Record<FieldName, string>>; symbolLabel?: string } {
  const next = structuredClone(form);
  const aiFields: FieldName[] = [];
  const basis: Partial<Record<FieldName, string>> = {};
  const { fields } = prefill;
  if (fields.symbol) { next.symbol = fields.symbol.value; aiFields.push("symbol"); basis.symbol = fields.symbol.basis; }
  if (fields.direction) { next.direction = fields.direction.value; aiFields.push("direction"); basis.direction = fields.direction.basis; }
  if (fields.stopLossTicks) { next.stop = { mode: "ticks", value: fields.stopLossTicks.value, risk: null }; aiFields.push("stop"); basis.stop = fields.stopLossTicks.basis; }
  if (fields.closeBeforeEndMinutes) { next.closeBeforeEnd = { on: true, minutes: fields.closeBeforeEndMinutes.value, everySession: false }; aiFields.push("closeBeforeEnd"); basis.closeBeforeEnd = fields.closeBeforeEndMinutes.basis; }
  if (fields.entryWindow) { next.entryWindows = [{ ...fields.entryWindow.value }]; aiFields.push("entryWindows"); basis.entryWindows = fields.entryWindow.basis; }
  if (fields.lots) { next.lots = fields.lots.value; aiFields.push("lots"); basis.lots = fields.lots.basis; }
  return { form: next, aiFields, basis, symbolLabel: fields.symbol?.label };
}
